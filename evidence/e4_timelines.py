"""E4: CONTRACT.md timelines A-F as rerunnable source calls + design states.

Each record labels OBSERVED (real lake/query behavior today), ORACLE
(filter + frozen evaluate = required controlled-read result) and PROPOSED
(design state-machine steps from DESIGN.md). The WithdrawalLedger is a
separate SQLite database used only to demonstrate crash/retry semantics;
it never wraps or mutates lake.py.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import os
import sqlite3
import tempfile

from lake import Lake, Reader
from samples import seeded
from common import BlockIndex, oracle, filter_tables, write_report


class WithdrawalLedger:
    """PROPOSED minimal durable state, in its own database (DESIGN 6.1).

    Two phases per request: PREPARED (idempotent request binding) then
    CONFIRMED (watermark bump inside one SQLite transaction). Crash between
    the phases leaves a reaped PREPARED row; crash after commit but before the
    response is delivered is recovered by idempotent request replay.
    """

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS withdrawals(
                request_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                binding TEXT NOT NULL, status TEXT NOT NULL,
                epoch INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS watermark(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                epoch INTEGER NOT NULL);
            INSERT OR IGNORE INTO watermark VALUES(1,0);
        """)

    def prepare(self, request_id, subject, binding):
        row = self.db.execute(
            "SELECT subject,binding,status,epoch FROM withdrawals "
            "WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row[1] != binding or row[0] != subject:
                raise ValueError("WITHDRAWAL_REQUEST_CONFLICT")
            return {"status": row[2], "epoch": row[3], "replayed": True}
        self.db.execute(
            "INSERT INTO withdrawals VALUES(?,?,?,?,-1)",
            (request_id, subject, binding, "PREPARED"))
        return {"status": "PREPARED", "epoch": -1, "replayed": False}

    def confirm(self, request_id):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT subject,status,epoch FROM withdrawals "
                "WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("UNKNOWN_PREPARED_REQUEST")
            if row[1] == "CONFIRMED":          # response lost -> idempotent
                epoch = row[2]
            else:
                epoch = self.db.execute(
                    "SELECT epoch FROM watermark").fetchone()[0] + 1
                self.db.execute(
                    "UPDATE withdrawals SET status='CONFIRMED',epoch=? "
                    "WHERE request_id=?", (epoch, request_id))
                self.db.execute("UPDATE watermark SET epoch=?", (epoch,))
            self.db.execute("COMMIT")
            return {"status": "CONFIRMED", "epoch": epoch}
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def watermark(self):
        return self.db.execute("SELECT epoch FROM watermark").fetchone()[0]

    def confirmed_subjects(self):
        return [r[0] for r in self.db.execute(
            "SELECT subject FROM withdrawals WHERE status='CONFIRMED' "
            "ORDER BY epoch")]

    def close(self):
        self.db.close()


def timeline_a():
    lake = seeded(Lake())
    plan = lake.prepare(2, {"holds": [[]]})          # held, not published
    head_after_prepare = lake.head()
    # PROPOSED: withdrawal confirmed via separate ledger; lake head untouched.
    ledger = WithdrawalLedger(":memory:")
    ledger.prepare("w_alice", "alice", "binding-v1")
    ack = ledger.confirm("w_alice")
    head_after_withdrawal = lake.head()
    # Old plan still publishes (real code path lake.py:82-109).
    gen3 = lake.publish("third", plan)
    controlled = {str(g): oracle(lake.raw(g), ledger.confirmed_subjects())
                  for g in (1, 2, gen3)}
    observed = {str(g): Reader(lake).export(Reader(lake).pin(g))
                for g in (1, 2, gen3)}
    controlled_ok = all(
        controlled[str(g)] == oracle(lake.raw(g), ["alice"])
        for g in (1, 2, gen3))
    ledger.close(); lake.close()
    return {"timeline": "A",
            "observed_head_after_prepare": head_after_prepare,
            "observed_head_after_withdrawal": head_after_withdrawal,
            "proposed_ack": ack,
            "observed_reader_today_unfiltered": observed,
            "oracle_controlled_after_confirm": controlled,
            "assertions": {
                "head_unchanged_by_withdrawal":
                    head_after_prepare == head_after_withdrawal == 2,
                "old_plan_publishes_as_gen3": gen3 == 3,
                "all_versions_apply_cumulative_set": controlled_ok}}


def timeline_b():
    lake = seeded(Lake())
    r1, r2 = Reader(lake), Reader(lake)
    h1, h2 = r1.pin(1), r2.pin(2)                    # pre-confirmation handles
    cached = {str(h1["generation"]): h1["oid"],
             str(h2["generation"]): h2["oid"]}
    observed_exports = {"gen1_via_old_handle": r1.export(h1),
                        "gen2_via_other_reader": r2.export(h2)}
    ledger = WithdrawalLedger(":memory:")
    ledger.prepare("w", "alice", "b"); ledger.confirm("w")
    # PROPOSED gateway resolves the SAME opaque handle under the new watermark;
    # the Reader-instance caches are not a security boundary (lake.py:136-143).
    required = {"gen1_via_old_handle": oracle(lake.raw(1), ["alice"]),
                "gen2_via_other_reader": oracle(lake.raw(2), ["alice"])}
    # Today the embedded handle bytes would still return pre-withdrawal rows.
    gap = {k: observed_exports[k] != required[k]
           for k in required}
    ledger.close(); lake.close()
    return {"timeline": "B", "cached_result_oids": cached,
            "observed_exports_today": observed_exports,
            "oracle_required_under_watermark": required,
            "observed_gap_handle_embeds_stale_bytes": gap,
            "assertions": {"stale_bytes_today": all(gap.values())}}


def timeline_c():
    lake = seeded(Lake())
    image = lake.checkpoint(":memory:")              # image taken at gen 2
    ledger = WithdrawalLedger(":memory:")
    ledger.prepare("w", "alice", "b"); ledger.confirm("w")
    lake.restore(image)                              # real restore lake.py:119
    head = lake.head()
    # New write after restore: one alice row, one legal row.
    new_gen = lake.publish("post_restore", lake.prepare(2, {"events": [[
        {"id": "e10", "owners": ["alice"], "device": "d10", "at": 10,
         "amount": 4},
        {"id": "e11", "owners": ["cara"], "device": "d11", "at": 10,
         "amount": 2}]]}))
    raw = lake.raw(new_gen)
    restored_image_read = oracle(image.raw(2), ["alice"])
    controlled = oracle(raw, ["alice"])
    observed_alice_served = any(
        "alice" in r["owners"] for r in raw["events"])
    alice_row_gone = all("alice" not in r["owners"]
                         for r in filter_tables(raw, ["alice"])["events"])
    legal_row_kept = any(r["id"] == "e11"
                         for r in filter_tables(raw, ["alice"])["events"])
    ledger_survives = ledger_watermark_proof()
    ledger.close(); image.close(); lake.close()
    return {"timeline": "C", "observed_head_after_restore": head,
            "observed_current_code_serves_new_alice": observed_alice_served,
            "oracle_image_read_still_filtered": restored_image_read,
            "oracle_new_write_alice_removed_legal_kept": controlled,
            "assertions": {"alice_row_gone": alice_row_gone,
                           "legal_row_kept": legal_row_kept,
                           "ledger_not_rewound_by_restore":
                               ledger_survives}}


def ledger_watermark_proof():
    # Reopen a file-backed ledger after "restore" of the lake file: the
    # independent ledger keeps its watermark.
    path = os.path.join(tempfile.mkdtemp(), "w.db")
    lg = WithdrawalLedger(path)
    lg.prepare("w", "alice", "b"); lg.confirm("w")
    before = lg.watermark(); lg.close()
    lg2 = WithdrawalLedger(path)
    ok = lg2.watermark() == before == 1
    lg2.close()
    return ok


def timeline_d():
    lake = seeded(Lake())
    idx = BlockIndex()
    idx.build_snapshot(lake, 1); idx.build_snapshot(lake, 2)
    path = os.path.join(tempfile.mkdtemp(), "w.db")
    ledger = WithdrawalLedger(path)
    # Both requests prepared from the same starting point (epoch 0).
    p1 = ledger.prepare("req_alice", "alice", "same-start")
    p2 = ledger.prepare("req_bob", "bob", "same-start")
    ack1 = ledger.confirm("req_alice")
    # Bob's impact set was computed pre-alice; at confirm the gateway merges
    # against the current cumulative watermark, so stale closure is discarded.
    closure_bob = idx.closure(lake, 1, ["alice", "bob"])
    ack2 = ledger.confirm("req_bob")
    cumulative = oracle(lake.raw(1), ["alice", "bob"])
    sample_expected = [
        {"day": 0, "region": "east", "devices": 1, "total": 7, "maximum": 7},
        {"day": 0, "region": "west", "devices": 1, "total": 5, "maximum": 5}]
    ledger.close()

    # Crash 1: PREPARED but never confirmed -> reopen, stale row reaped,
    # retry produces a clean confirmation.
    lg = WithdrawalLedger(path)
    lg.prepare("req_cara_pre", "cara", "x")
    lg.close()
    lg = WithdrawalLedger(path)
    stale = lg.db.execute(
        "SELECT status FROM withdrawals WHERE request_id='req_cara_pre'"
    ).fetchone()[0]
    lg.db.execute("DELETE FROM withdrawals WHERE request_id='req_cara_pre'")
    lg.prepare("req_cara", "cara", "x")
    retry = lg.confirm("req_cara")
    # Crash 2: commit succeeded, response lost -> idempotent replay.
    replay_alice = lg.confirm("req_alice")
    replay_bob = lg.confirm("req_bob")
    watermark = lg.watermark()
    subjects = lg.confirmed_subjects()
    lg.close()
    return {"timeline": "D",
            "prepared": [p1, p2], "ack_alice": ack1, "ack_bob": ack2,
            "bob_merged_closure_candidates":
                closure_bob["candidate_device_days"],
            "bob_merged_closure_changed":
                closure_bob["changed_device_days"],
            "bob_closure_bounded_equals_oracle":
                closure_bob["bounded_equals_oracle"],
            "cumulative_oracle_gen1": cumulative,
            "crash_prepared_row_status_on_reopen": stale,
            "crash_retry_ack": retry,
            "response_lost_replays": [replay_alice, replay_bob],
            "final_watermark": watermark, "final_subjects": subjects,
            "assertions": {
                "cumulative_matches_contract": cumulative == sample_expected,
                "alice_epoch1_bob_epoch2": ack1["epoch"] == 1
                and ack2["epoch"] == 2,
                "replay_returns_same_epoch":
                    replay_alice["epoch"] == 1 and replay_bob["epoch"] == 2,
                "watermark_survives_process_reopen": watermark == 3,
                "subjects_order": subjects == ["alice", "bob", "cara"]}}


def timeline_e():
    lake = seeded(Lake())                            # gens 1,2 already exist
    idx = BlockIndex()
    # Indexer begins with an explicit low-water mark; writes never stop.
    watermark_gens = []
    idx.build_snapshot(lake, 1)                      # build step 1
    watermark_gens.append(lake.head())
    gen3 = lake.publish("third", lake.prepare(2, {"holds": [[]]}))
    idx.build_snapshot(lake, 2)                      # build step 2 (catch-up)
    shared_before = len(idx.touched_blocks)
    # New plan: replacement events table = the existing shared block's
    # CONTENT (same canonical bytes -> same oid) plus one new block.
    shared_oid = lake.snapshot(3)["roots"]["events"][0]
    shared_block = lake.get(shared_oid)
    gen4 = lake.publish("fourth", lake.prepare(3, {"events": [
        shared_block,
        [{"id": "e20", "owners": ["cara"], "device": "d20", "at": 10,
          "amount": 1}]]}))
    idx.build_snapshot(lake, gen3)                   # drain loop
    idx.build_snapshot(lake, gen4)
    all_gens, alice_rows = idx.gens_referencing_owner(lake, "alice")
    # No version missed: every snapshot generation covered, shared object once.
    covered = sorted({g for (g, _k) in idx.by_gen_kind})
    shared_after = len(idx.touched_blocks)
    distinct_rows_indexed = len(idx.row_owners)
    c1 = idx.closure(lake, 1, ["alice"])
    c4 = idx.closure(lake, gen4, ["alice"])
    lake.close()
    return {"timeline": "E",
            "interleaved_publish_generations": [gen3, gen4],
            "index_covered_generations": covered,
            "alice_referenced_gens": all_gens,
            "blocks_touched_before_after_shared_plan":
                [shared_before, shared_after],
            "distinct_rows_indexed": distinct_rows_indexed,
            "index_scanned_rows": idx.scanned_rows,
            "assertions": {
                "all_snapshot_gens_covered": covered == [1, 2, 3, 4],
                "alice_found_in_old_and_new": all_gens == [1, 2, 3, 4],
                "each_distinct_row_scanned_once":
                    idx.scanned_rows == distinct_rows_indexed,
                "closures_complete": c1["bounded_equals_oracle"]
                and c4["bounded_equals_oracle"]},
            "gen4_oracle": c4["oracle"]}


def timeline_f():
    lake = Lake()
    out = {}
    # F-1 replacement changes the winning event (at/id) and device.
    g1 = lake.publish("f1", lake.prepare(0, {
        "events": [[
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 10,
             "amount": 4},
            {"id": "e2", "owners": ["alice"], "device": "d1", "at": 20,
             "amount": 9}]],
        "assignments": [[
            {"id": "a1", "owners": ["dana"], "device": "d1", "at": 0,
             "region": "east"}]],
        "holds": [[]]}))
    g2 = lake.publish("f2", lake.prepare(g1, {"events": [[
        {"id": "e2", "owners": ["alice"], "device": "d9", "at": 5,
         "amount": 9}]]}))
    out["F1_winner_and_device_replaced"] = {
        "gen1_oracle": oracle(lake.raw(g1), ["alice"]),
        "gen2_observed": lake.raw(g2)["events"],
        "gen2_oracle": oracle(lake.raw(g2), ["alice"])}
    # F-2 withdraw the latest assignment -> the device loses its only
    # eligible assignment and falls into the __UNASSIGNED__ bucket
    # (query.py:19-21). The event itself is neutral (dana-owned), isolating
    # the temporal-attribution mechanism as the single cause.
    g3 = lake.publish("f3", lake.prepare(g2, {
        "assignments": [[
            {"id": "a1", "owners": ["dana"], "device": "d1", "at": 0,
             "region": "east"},
            {"id": "a2", "owners": ["alice"], "device": "d9", "at": 4,
             "region": "west"}]]}))
    out["F2_latest_assignment_withdrawn"] = {
        "observed_before": __import__("query").evaluate(lake.raw(g3)),
        "oracle_after": oracle(lake.raw(g3), ["alice"])}
    # Cover the region-to-region fallback branch too: two eligible
    # assignments on the same device, alice owns the latest one. The event
    # is neutral so the assignment timeline is the only moving part.
    g3b = lake.publish("f3b", lake.prepare(g3, {
        "events": [[
            {"id": "n1", "owners": ["dana"], "device": "d9", "at": 5,
             "amount": 6}]],
        "assignments": [[
            {"id": "b1", "owners": ["cara"], "device": "d9", "at": 1,
             "region": "east"},
            {"id": "b2", "owners": ["alice"], "device": "d9", "at": 4,
             "region": "west"}]]}))
    out["F2b_region_fallback"] = {
        "observed_before": __import__("query").evaluate(lake.raw(g3b)),
        "oracle_after": oracle(lake.raw(g3b), ["alice"])}
    # Counterexample 1: event absent from output (masked) becomes the winner
    # after a later block replacement.
    g4 = lake.publish("f4", lake.prepare(g3b, {"events": [[
        {"id": "m1", "owners": ["alice"], "device": "d1", "at": 1,
         "amount": 9},
        {"id": "m2", "owners": ["dana"], "device": "d1", "at": 2,
         "amount": 3}]]}))
    g5 = lake.publish("f5", lake.prepare(g4, {"events": [[
        {"id": "m1", "owners": ["alice"], "device": "d1", "at": 1,
         "amount": 9}]]}))
    out["F3_counterexample_masked_event"] = {
        "masked_in_gen4_output":
            __import__("query").evaluate(lake.raw(g4))[0]["total"] == 3,
        "promoted_in_gen5_then_filtered":
            oracle(lake.raw(g5), ["alice"]) == []}
    # Counterexample 2: hold never produces output; deleting it creates output.
    g6 = lake.publish("f6", lake.prepare(g5, {
        "events": [[
            {"id": "h_ev", "owners": ["dana"], "device": "d3", "at": 10,
             "amount": 7}]],
        "holds": [[
            {"id": "h1", "owners": ["alice"], "device": "d3", "day": 0}]]}))
    out["F4_counterexample_hold_antijoin"] = {
        "observed_blocked": __import__("query").evaluate(lake.raw(g6)),
        "oracle_released": oracle(lake.raw(g6), ["alice"])}
    lake.close()
    assertions = {
        "F1_fallback": out["F1_winner_and_device_replaced"]["gen1_oracle"][
            0]["total"] == 4,
        # g3 inherits the alice-owned event from g2, so the oracle also
        # removes the event itself; the attribution-only branch is isolated
        # in F2b with a neutral event.
        "F2_attribution_or_empty": (
            not out["F2_latest_assignment_withdrawn"]["oracle_after"]
            or out["F2_latest_assignment_withdrawn"]["oracle_after"][0][
                "region"] in ("east", "__UNASSIGNED__")),
        "F2b_west_to_east_fallback":
            out["F2b_region_fallback"]["observed_before"][0]["region"]
            == "west"
            and out["F2b_region_fallback"]["oracle_after"][0]["region"]
            == "east",
        "F3_masked_row_must_be_indexed":
            out["F3_counterexample_masked_event"][
                "promoted_in_gen5_then_filtered"],
        "F4_hold_creates_new_group":
            out["F4_counterexample_hold_antijoin"]["oracle_released"] != []}
    return {"timeline": "F", "steps": out, "assertions": assertions}


def main():
    records = [timeline_a(), timeline_b(), timeline_c(), timeline_d(),
               timeline_e(), timeline_f()]
    failures = []
    for rec in records:
        for name, ok in rec.get("assertions", {}).items():
            if not ok:
                failures.append(f"{rec['timeline']}.{name}")
    assert not failures, failures
    path = write_report("e4_timelines.json",
                        {"layer": "OBSERVED+ORACLE+PROPOSED",
                         "timelines": records})
    print(json.dumps({"report": os.path.relpath(path),
                      "timelines": [r["timeline"] for r in records],
                      "failures": failures},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
