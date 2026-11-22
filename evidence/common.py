"""Shared helpers for the design evidence package.

Layer labels (kept explicit in every report):
  OBSERVED  - values returned by the unmodified business sources lake.py /
              query.py / samples.py.
  ORACLE    - the discussion-only expectation: filter whole source tables by
              owners then run the FROZEN query.evaluate. This is the exact
              "remove rows then run the original query" semantics required by
              CONTRACT.md; it is deliberately a full-evaluate reference and
              must never be presented as an online maintenance algorithm.
  PROPOSED  - state-machine / closure reasoning about the design in DESIGN.md;
              uses the index helpers below only to demonstrate index-bounded
              work, never to implement a withdrawal engine inside the lake.

No business source is imported for mutation; lake/query/samples stay byte
identical to the initial commit.
"""
import hashlib
import json
import os

from lake import canonical
from query import KINDS, evaluate

EVIDENCE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(EVIDENCE_DIR, "out")
DAY = 86400


def row_key(row):
    """Content address of one row; stable across generations and restores
    because it depends only on canonical row content (cf. lake.put lake.py:36).
    """
    payload = canonical(row)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def filter_tables(tables, withdrawn):
    """ORACLE step 1: delete whole rows whose owners intersect the withdrawn
    set. Shared rows (multiple owners) are deleted as whole rows - owners are
    never edited. Mirrors probe.without (probe.py:8-11)."""
    banned = set(withdrawn)
    return {kind: [row for row in tables[kind]
                   if not (set(row["owners"]) & banned)]
            for kind in KINDS}


def oracle(tables, withdrawn):
    """ORACLE: exact required result for a given source version + withdrawn
    set, obtained via the frozen evaluator only."""
    return evaluate(filter_tables(tables, withdrawn))


def group_index(rows):
    """Map (day, region) -> aggregation row, for set-equality comparison."""
    return {(r["day"], r["region"]): r for r in rows}


def diff_groups(before, after):
    """Groups added / removed / changed between two frozen-query outputs."""
    b, a = group_index(before), group_index(after)
    return {
        "added": [k for k in sorted(a) if k not in b],
        "removed": [k for k in sorted(b) if k not in a],
        "changed": [k for k in sorted(b) if k in a and b[k] != a[k]],
    }


def device_day_of(row, kind):
    if kind == "holds":
        return row["device"], row["day"]
    return row["device"], row["at"] // DAY


# ---------------------------------------------------------------------------
# PROPOSED-layer index simulation. Every lookup is keyed by row content /
# device, never by a full scan; counts are exposed so evidence can show that
# work is bounded by candidate devices, not by total history size.
# ---------------------------------------------------------------------------
class BlockIndex:
    """In-memory stand-in for the persistent owner/row indexes of DESIGN.md
    sections 6.2/7. Built from snapshot roots exactly as an online builder
    would build them, including a low-water-mark handoff under live writes."""

    def __init__(self):
        self.row_owners = {}                 # row_key -> sorted owners
        self.by_gen_kind = {}                # (gen, kind) -> [row_key]
        self.by_device = {}                  # device -> {row_key}
        self.touched_blocks = set()          # block oids actually processed
        self.block_keys = {}                 # oid -> [row_key], read once
        self.scanned_rows = 0
        self.build_gens = []                 # build order (for E handoff)

    def add_block(self, lake, gen, kind, oid):
        # Version membership comes from snapshot roots metadata (snapshots.
        # roots, lake.py:20) and is ALWAYS recorded, even for shared blocks,
        # so generation reuse / restore stay resolvable. Object payloads are
        # read at most once: row-level index work is deduplicated by content
        # (lake.put INSERT OR IGNORE lake.py:39).
        is_new = oid not in self.touched_blocks
        if is_new:
            self.touched_blocks.add(oid)
            keys = []
            for row in lake.get(oid):
                rk = row_key(row)
                keys.append(rk)
                self.scanned_rows += 1
                self.row_owners[rk] = sorted(row["owners"])
                self.by_device.setdefault(row["device"], set()).add(rk)
            self.block_keys[oid] = keys
        self.by_gen_kind.setdefault((gen, kind), []).extend(
            self.block_keys[oid])
        return is_new

    def build_snapshot(self, lake, gen):
        """Index one snapshot generation; reusable under the build watermark."""
        roots = lake.snapshot(gen)["roots"]
        for kind in KINDS:
            for oid in roots[kind]:
                self.add_block(lake, gen, kind, oid)
        self.build_gens.append(gen)

    def rows_of(self, gen, kind):
        return [rk for rk in self.by_gen_kind.get((gen, kind), [])]

    def gens_referencing_owner(self, lake, subject):
        """Exact set of generations referencing subject - via owner index,
        no table scan. Content addressing makes this valid across restore /
        generation-number reuse."""
        hit_rows = {rk for rk, owners in self.row_owners.items()
                    if subject in owners}
        gens = set()
        for (gen, kind), keys in self.by_gen_kind.items():
            if hit_rows & set(keys):
                gens.add(gen)
        return sorted(gens), sorted(hit_rows)

    def closure(self, lake, gen, withdrawn):
        """PROPOSED precise impact closure (DESIGN.md section 5).

        Four levels, all derived from index lookups plus per-candidate
        timeline reads (never a whole-history scan):
          C1 candidates      - every device/day touched by a deleted row;
          C2 changed keys    - subset of C1 whose winner/contribution actually
                               changes (winner fallback, attribution, release);
          C3 groups          - aggregation groups that must be rewritten;
          C4 untouched gens  - versions with no indexed row of the subject.
        A full ORACLE run cross-checks both that every changed key lies in
        C1 and that the bounded recomputation equals the oracle exactly.
        """
        banned = set(withdrawn)
        deleted = {kind: [] for kind in KINDS}
        for kind in KINDS:
            for rk in self.rows_of(gen, kind):
                if set(self.row_owners[rk]) & banned:
                    deleted[kind].append(rk)
        key_to_row = {rk: row for kind in KINDS
                      for row in self._materialize(lake, gen, kind)
                      for rk in [row_key(row)]}

        # Level 1: candidate device/days (superset to inspect).
        candidates = set()
        for kind in KINDS:
            for rk in deleted[kind]:
                candidates.add(device_day_of(key_to_row[rk], kind))

        # Index-bounded timeline reads for candidate devices only.
        tables = self._materialize_all(lake, gen)
        filtered = filter_tables(tables, withdrawn)

        def winners(ts):
            latest = {}
            for event in ts["events"]:
                k = (event["device"], event["at"] // DAY)
                if k not in latest or (event["at"], event["id"]) > (
                        latest[k]["at"], latest[k]["id"]):
                    latest[k] = event
            blocked = {(r["device"], r["day"]) for r in ts["holds"]}
            contrib = {}
            for (device, day), event in latest.items():
                if (device, day) in blocked:
                    continue
                eligible = [a for a in ts["assignments"]
                            if a["device"] == device and a["at"] <= event["at"]]
                asg = max(eligible, key=lambda a: (a["at"], a["id"]),
                          default=None)
                region = asg["region"] if asg else "__UNASSIGNED__"
                contrib[(device, day)] = (event, region)
            return contrib

        before_c, after_c = winners(tables), winners(filtered)
        all_keys = before_c.keys() | after_c.keys()
        # Level 2: changed keys, provably contained in the candidate set.
        changed_keys = sorted(k for k in all_keys
                              if k in candidates
                              and before_c.get(k) != after_c.get(k))
        # Completeness: no change may occur outside the candidate set.
        outside = sorted(k for k in all_keys
                         if k not in candidates
                         and before_c.get(k) != after_c.get(k))
        # Level 3: groups to update.
        groups = sorted(set(
            [before_c[k][1] for k in changed_keys if k in before_c] +
            [after_c[k][1] for k in changed_keys if k in after_c]))

        # Cross-check: bounded recomputation equals the full-table oracle.
        oracle_rows = evaluate(filtered)
        bounded_rows = self._render(after_c)
        return {
            "generation": gen,
            "withdrawn": sorted(withdrawn),
            "deleted_row_keys": {kind: deleted[kind] for kind in KINDS},
            "candidate_device_days": sorted(candidates),
            "changed_device_days": changed_keys,
            "groups_to_update": groups,
            "keys_changed_outside_candidates": outside,
            "closure_complete": not outside,
            "bounded_equals_oracle": bounded_rows == oracle_rows,
            "oracle": oracle_rows,
        }

    # --- internal materialization helpers (index-bounded) ---
    def _materialize(self, lake, gen, kind):
        rows = []
        for oid in lake.snapshot(gen)["roots"][kind]:
            rows.extend(lake.get(oid))
        return rows

    def _materialize_all(self, lake, gen):
        return {kind: self._materialize(lake, gen, kind) for kind in KINDS}

    @staticmethod
    def _render(contrib):
        groups = {}
        for (device, day), (event, region) in contrib.items():
            g = groups.setdefault((day, region),
                                  {"devices": set(), "values": []})
            g["devices"].add(device)
            g["values"].append(event["amount"])
        return [{"day": day, "region": region,
                 "devices": len(g["devices"]), "total": sum(g["values"]),
                 "maximum": max(g["values"])}
                for (day, region), g in sorted(groups.items())]


def write_report(name, payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
    return path
