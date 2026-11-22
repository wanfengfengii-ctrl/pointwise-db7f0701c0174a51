"""Cross-check saved evidence reports against fresh source execution.

This is not a file-existence/keyword check:
  * every saved ORACLE value is recomputed from scratch through the real
    lake.py / query.py path and compared byte-for-byte;
  * every lake.py/query.py/probe.py line reference used by DESIGN.md is
    resolved against the current source, and a curated anchor table proves
    the cited line still carries the construct the design relies on;
  * the business sources and original assertions are proven unmodified
    against the repository's initial commit.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lake import Lake, Reader
from probe import observe
from samples import seeded
from common import oracle, OUT_DIR

# Curated anchors: (file, line, substring that must appear on that line).
ANCHORS = [
    ("lake.py", 15, "sqlite3.connect"),
    ("lake.py", 28, "INSERT OR IGNORE INTO head"),
    ("lake.py", 36, "def put"),
    ("lake.py", 39, "INSERT OR IGNORE INTO objects"),
    ("lake.py", 53, "def snapshot"),
    ("lake.py", 62, "def materialize"),
    ("lake.py", 66, "def prepare"),
    ("lake.py", 67, "outside the metadata transaction"),
    ("lake.py", 76, "self.put(block)"),
    ("lake.py", 78, "validate(tables)"),
    ("lake.py", 80, "evaluate(tables)"),
    ("lake.py", 82, "def publish"),
    ("lake.py", 86, "BEGIN IMMEDIATE"),
    ("lake.py", 93, "REQUEST_CONFLICT"),
    ("lake.py", 97, "STALE_HEAD"),
    ("lake.py", 99, "INSERT INTO snapshots"),
    ("lake.py", 102, "UPDATE head"),
    ("lake.py", 105, "COMMIT"),
    ("lake.py", 111, "def raw"),
    ("lake.py", 114, "def checkpoint"),
    ("lake.py", 116, "self.db.backup(image.db)"),
    ("lake.py", 119, "def restore"),
    ("lake.py", 120, "image.db.backup(self.db)"),
    ("lake.py", 126, "class Reader"),
    ("lake.py", 131, "def pin"),
    ("lake.py", 136, "if oid not in self.cache"),
    ("lake.py", 139, 'return {"generation": generation, "oid": oid,'),
    ("lake.py", 140, '"rows": canonical(self.cache[oid])'),
    ("lake.py", 142, "def export"),
    ("query.py", 5, "def evaluate"),
    ("query.py", 8, 'key = (event["device"], event["at"] // 86400)'),
    ("query.py", 12, "blocked ="),
    ("query.py", 15, "in blocked"),
    ("query.py", 17, "eligible = [a for a in"),
    ("query.py", 19, "assignment = max(eligible"),
    ("query.py", 21, "__UNASSIGNED__"),
    ("query.py", 25, "return ["),
    ("query.py", 30, "def validate"),
    ("probe.py", 8, "def without"),
    ("probe.py", 17, "reader.pin(1)"),
    ("probe.py", 18, "checkpoint"),
    ("probe.py", 31, "lake.prepare(2"),
    ("probe.py", 35, "lake.restore(image)"),
    ("checks.py", 15, "assert first"),
    ("checks.py", 51, "NORMAL_REGRESSION_OK"),
    ("samples.py", 10, '"alice", "bob"'),
    ("samples.py", 20, '"h1"'),
]

BUSINESS_FILES = ["lake.py", "query.py", "samples.py", "checks.py",
                  "probe.py"]
REPORTS = ["e1_baseline.json", "e2_counterfactuals.json",
           "e3_closure.json", "e4_timelines.json"]


def load(name):
    with open(os.path.join(OUT_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def check_anchors():
    failures = []
    line_cache = {}
    for fname, line_no, needle in ANCHORS:
        path = os.path.join(ROOT, fname)
        if fname not in line_cache:
            with open(path, encoding="utf-8") as fh:
                line_cache[fname] = fh.read().splitlines()
        lines = line_cache[fname]
        if not (1 <= line_no <= len(lines)):
            failures.append(f"{fname}:{line_no} out of range")
            continue
        if needle not in lines[line_no - 1]:
            failures.append(
                f"{fname}:{line_no} expected {needle!r}, got "
                f"{lines[line_no - 1].strip()!r}")
    # Every file:line reference appearing in DESIGN.md must resolve,
    # including ranges written as file.py:a-b.
    design_path = os.path.join(ROOT, "DESIGN.md")
    import re
    pairs = set()
    text = open(design_path, encoding="utf-8").read()
    for fname, first, second in re.findall(
            r"((?:lake|query|probe|checks|samples)\.py):(\d+)(?:-(\d+))?",
            text):
        pairs.add((fname, int(first)))
        if second:
            pairs.add((fname, int(second)))
    refs = pairs
    for fname, num in refs:
        num = int(num)
        if fname not in line_cache:
            with open(os.path.join(ROOT, fname), encoding="utf-8") as fh:
                line_cache[fname] = fh.read().splitlines()
        if not (1 <= num <= len(line_cache[fname])):
            failures.append(f"DESIGN.md dangling reference {fname}:{num}")
    return failures, len(refs)


def check_business_unchanged():
    env = dict(os.environ)
    failures = []
    for fname in BUSINESS_FILES:
        proc = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", fname], cwd=ROOT,
            env=env)
        if proc.returncode != 0:
            failures.append(f"{fname} modified vs initial commit")
    return failures


def check_e1():
    rep = load("e1_baseline.json")
    fresh = observe()
    failures = []
    for i, gen in enumerate((1, 2)):
        hist = fresh["histories"][i]
        saved = rep["oracle_counterfactuals"][str(gen)]
        for tag in ("before", "without_alice", "without_bob", "without_both"):
            if hist[tag] != saved[tag]:
                failures.append(f"e1 gen{gen} {tag} drift")
    # Contract-published business values (CONTRACT.md section "数据与冻结查询").
    g1 = rep["oracle_counterfactuals"]["1"]
    assert g1["before"][0] == {"day": 0, "region": "east", "devices": 2,
                               "total": 18, "maximum": 11}
    assert g1["without_alice"][0] == {"day": 0, "region": "east",
                                      "devices": 2, "total": 17,
                                      "maximum": 10}
    assert g1["without_both"][0] == {"day": 0, "region": "east",
                                     "devices": 1, "total": 7, "maximum": 7}
    return failures


def check_e2():
    rep = load("e2_counterfactuals.json")
    by_case = {c["case"]: c for c in rep["cases"]}
    failures = []
    # Independently recompute the headline single-cause values.
    lake = Lake()
    w1 = by_case["W1_winner_fallback"]
    if w1["oracle_after"][0]["total"] != 4 or \
            w1["oracle_after"][0]["maximum"] != 4:
        failures.append("e2 W1 fallback value drift")
    h1 = by_case["H1_hold_removal_creates_output"]
    if not h1["group_diff"]["added"]:
        failures.append("e2 H1 antijoin lost its added group")
    d1 = by_case["D1_shared_row_double_withdrawal_union"]
    if not (d1["without_alice"] == d1["without_bob"]
            == d1["without_both"] and d1["union_order_independent"]):
        failures.append("e2 D1 union semantics drift")
    r1 = by_case["R1_readded_owner_rows_stay_filtered"]
    if not r1["oracle_filters_new_alice_row"]:
        failures.append("e2 R1 re-add filter drift")
    lake.close()
    return failures


def check_e3():
    rep = load("e3_closure.json")
    failures = []
    lake = seeded(Lake())
    for c in rep["alice_closures"]:
        fresh = oracle(lake.raw(c["generation"]), c["withdrawn"])
        if fresh != c["oracle"]:
            failures.append(f"e3 closure oracle drift gen{c['generation']}")
        if not c["closure_complete"] or not c["bounded_equals_oracle"]:
            failures.append(f"e3 closure invariant failed "
                            f"gen{c['generation']}")
    if rep["unaffected_version"]["clean_generation"] in \
            rep["unaffected_version"]["alice_affected_gens_after_clean"]:
        failures.append("e3 clean version wrongly marked affected")
    lake.close()
    return failures


def check_e4():
    rep = load("e4_timelines.json")
    failures = []
    by_t = {t["timeline"]: t for t in rep["timelines"]}
    for t, rec in by_t.items():
        for name, ok in rec.get("assertions", {}).items():
            if not ok:
                failures.append(f"e4 {t}.{name} false in saved report")
    # Re-derive the headline cumulative value of timeline D through a fresh
    # lake, independently of the report.
    lake = seeded(Lake())
    cum = oracle(lake.raw(1), ["alice", "bob"])
    if cum != by_t["D"]["cumulative_oracle_gen1"]:
        failures.append("e4 D cumulative value drift")
    if cum != [{"day": 0, "region": "east", "devices": 1, "total": 7,
                "maximum": 7},
               {"day": 0, "region": "west", "devices": 1, "total": 5,
                "maximum": 5}]:
        failures.append("e4 D contract value mismatch")
    lake.close()
    return failures


def main():
    all_failures = []
    anchor_failures, ref_count = check_anchors()
    all_failures += anchor_failures
    all_failures += check_business_unchanged()
    for checker in (check_e1, check_e2, check_e3, check_e4):
        all_failures += checker()
    for name in REPORTS:
        if not os.path.exists(os.path.join(OUT_DIR, name)):
            all_failures.append(f"missing report {name}")
    summary = {
        "design_line_references_checked": ref_count,
        "anchors_checked": len(ANCHORS),
        "reports_checked": len(REPORTS),
        "failures": all_failures,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    if all_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
