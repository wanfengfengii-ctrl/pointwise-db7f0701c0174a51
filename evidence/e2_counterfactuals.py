"""E2: single-cause counterfactuals proving each required mechanism.

Every table is accepted through the REAL business path (Lake.prepare ->
query.validate, lake.py:66-80; publish lake.py:82-109). Expected values are
ORACLE outputs (filter whole rows, then frozen evaluate). Each case changes
exactly one cause so the mechanism is attributable.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

from lake import Lake
from query import evaluate
from common import (oracle, filter_tables, diff_groups, row_key,
                    write_report)


def publish_tables(lake, base, tables):
    req = "case-%03d" % publish_tables.serial
    publish_tables.serial += 1
    plan = lake.prepare(base, {k: [v] for k, v in tables.items()})
    return lake.publish(req, plan)


publish_tables.serial = 0


def case(lake, name, tables, withdrawn, base=None):
    if base is None:
        base = lake.head()
    gen = publish_tables(lake, base, tables)
    raw = lake.raw(gen)
    before = evaluate(raw)
    after = oracle(raw, withdrawn)
    return {"case": name, "generation": gen, "withdrawn": withdrawn,
            "observed_before": before, "oracle_after": after,
            "group_diff": diff_groups(before, after),
            "filtered_rows": {
                k: {"before": len(raw[k]), "after": len(filter_tables(
                    raw, withdrawn)[k])} for k in raw}}


def main():
    lake = Lake()
    report = []

    # W1: latest-event winner rolls back to an earlier event (latest/at,id
    # rule query.py:7-11). Whole-row deletion changes total AND maximum.
    report.append(case(lake, "W1_winner_fallback", {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 10,
             "amount": 4},
            {"id": "e2", "owners": ["alice"], "device": "d1", "at": 20,
             "amount": 9}],
        "assignments": [
            {"id": "a1", "owners": ["dana"], "device": "d1", "at": 0,
             "region": "east"}],
        "holds": []}, ["alice"]))

    # W2: sole winning event deleted -> device contributes nothing; the
    # group can shrink/disappear even though assignments still exist
    # (query.py:14 device-without-event rule).
    report.append(case(lake, "W2_winner_vanishes", {
        "events": [
            {"id": "e1", "owners": ["alice"], "device": "d1", "at": 10,
             "amount": 9}],
        "assignments": [
            {"id": "a1", "owners": ["bob"], "device": "d1", "at": 0,
             "region": "west"}],
        "holds": []}, ["alice"]))

    # A1: winner event is neutral; deleting alice's latest assignment moves
    # the device to an earlier assignment's region (query.py:17-21).
    report.append(case(lake, "A1_attribution_changes_region", {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 20,
             "amount": 5}],
        "assignments": [
            {"id": "a1", "owners": ["bob"], "device": "d1", "at": 0,
             "region": "east"},
            {"id": "a2", "owners": ["alice"], "device": "d1", "at": 15,
             "region": "west"}],
        "holds": []}, ["alice"]))

    # A2: no eligible assignment remains -> __UNASSIGNED__ bucket appears.
    report.append(case(lake, "A2_becomes_unassigned", {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 20,
             "amount": 5}],
        "assignments": [
            {"id": "a1", "owners": ["alice"], "device": "d1", "at": 10,
             "region": "east"}],
        "holds": []}, ["alice"]))

    # A3: deleting a neutral (bob) latest assignment falls back onto an
    # older alice assignment - historical assignment timeline is required
    # context, not just current winner's chosen row.
    report.append(case(lake, "A3_older_assignment_becomes_decisive", {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 20,
             "amount": 5}],
        "assignments": [
            {"id": "a1", "owners": ["alice"], "device": "d1", "at": 5,
             "region": "east"},
            {"id": "a2", "owners": ["bob"], "device": "d1", "at": 15,
             "region": "west"}],
        "holds": []}, ["bob"]))

    # H1: anti-join - deleting the hold releases the device (query.py:15-16);
    # a brand new group appears in output although no event changed.
    report.append(case(lake, "H1_hold_removal_creates_output", {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d3", "at": 10,
             "amount": 7},
            {"id": "e2", "owners": ["cara"], "device": "d2", "at": 10,
             "amount": 3}],
        "assignments": [
            {"id": "a1", "owners": ["cara"], "device": "d2", "at": 0,
             "region": "east"}],
        "holds": [
            {"id": "h1", "owners": ["alice"], "device": "d3", "day": 0}]},
        ["alice"]))

    # D1: shared row (two owners) deleted for either withdrawal; cumulative
    # union semantics, order independent.
    d1_tables = {
        "events": [
            {"id": "e5", "owners": ["alice", "bob"], "device": "d4",
             "at": 15, "amount": 11},
            {"id": "e3", "owners": ["cara"], "device": "d2", "at": 15,
             "amount": 7}],
        "assignments": [
            {"id": "a5", "owners": ["bob"], "device": "d4", "at": 0,
             "region": "east"},
            {"id": "a3", "owners": ["cara"], "device": "d2", "at": 0,
             "region": "east"}],
        "holds": []}
    d1_gen = publish_tables(lake, lake.head(), d1_tables)
    d1_raw = lake.raw(d1_gen)
    d1 = {"case": "D1_shared_row_double_withdrawal_union",
          "generation": d1_gen,
          "without_alice": oracle(d1_raw, ["alice"]),
          "without_bob": oracle(d1_raw, ["bob"]),
          "without_both": oracle(d1_raw, ["alice", "bob"]),
          "union_order_independent":
              oracle(d1_raw, ["alice", "bob"]) ==
              oracle(d1_raw, ["bob", "alice"]),
          "shared_row_key": row_key(d1_raw["events"][0])}
    assert d1["without_alice"] == d1["without_bob"] == d1["without_both"]
    report.append(d1)

    # L1: masked (non-winning) alice event is absent from current output;
    # then a block replacement removes the winner and the masked row becomes
    # decisive - proof that positive output lineage alone is insufficient.
    l1_gen1 = publish_tables(lake, lake.head(), {
        "events": [
            {"id": "e1", "owners": ["dana"], "device": "d1", "at": 20,
             "amount": 6},
            {"id": "e2", "owners": ["alice"], "device": "d1", "at": 10,
             "amount": 9}],
        "assignments": [
            {"id": "a1", "owners": ["dana"], "device": "d1", "at": 0,
             "region": "east"}],
        "holds": []})
    masked_raw = lake.raw(l1_gen1)
    masked_key = row_key(masked_raw["events"][1])
    before_masked = evaluate(masked_raw)
    # gen2: replace ONLY the events block; assignments/holds inherit roots.
    gen2 = lake.publish("g_masked2", lake.prepare(l1_gen1, {"events": [[
        {"id": "e2", "owners": ["alice"], "device": "d1", "at": 10,
         "amount": 9}]]}))
    promoted_raw = lake.raw(gen2)
    l1 = {"case": "L1_masked_event_promoted_after_replacement",
          "masked_row_key": masked_key,
          "gen1_output_alice_absent_yet_row_stored":
              all(c["region"] != "__UNASSIGNED__" for c in before_masked)
              and any(r["id"] == "e2" for r in masked_raw["events"]),
          "gen1_withdraw_alice_unchanged":
              oracle(masked_raw, ["alice"]) == before_masked,
          "gen2_observed_promoted": evaluate(promoted_raw),
          "gen2_oracle_withdraw_alice": oracle(promoted_raw, ["alice"]),
          "same_content_row_key": row_key(promoted_raw["events"][0])
          == masked_key}
    assert l1["gen1_withdraw_alice_unchanged"]
    assert l1["gen2_oracle_withdraw_alice"] == []
    report.append(l1)

    # R1: re-added rows after withdrawal. Observed: current business code
    # stores and serves them (no protection exists). Oracle: still filtered.
    r1_gen1 = publish_tables(lake, lake.head(), {
        "events": [
            {"id": "e1", "owners": ["alice"], "device": "d1", "at": 10,
             "amount": 9}],
        "assignments": [
            {"id": "a1", "owners": ["bob"], "device": "d1", "at": 0,
             "region": "east"}],
        "holds": []})
    re_add = {"id": "e9", "owners": ["alice"], "device": "d9", "at": 10,
              "amount": 2}
    r1_gen2 = lake.publish("g_readd", lake.prepare(r1_gen1, {"events": [[
        re_add,
        {"id": "e8", "owners": ["cara"], "device": "d8", "at": 10,
         "amount": 1}]]}))
    r1_raw = lake.raw(r1_gen2)
    r1 = {"case": "R1_readded_owner_rows_stay_filtered",
          "generation": r1_gen2,
          "observed_current_code_still_serves_alice":
              any("alice" in r["owners"] for r in r1_raw["events"]),
          "oracle_filters_new_alice_row":
              all("alice" not in r["owners"]
                  for r in filter_tables(r1_raw, ["alice"])["events"]),
          "oracle_result": oracle(r1_raw, ["alice"]),
          "readd_row_key": row_key(re_add)}
    assert r1["observed_current_code_still_serves_alice"]
    assert r1["oracle_filters_new_alice_row"]
    report.append(r1)
    lake.close()

    # Assertions for the single-cause expectations.
    w1, w2, a1, a2, a3, h1 = report[:6]
    assert w1["oracle_after"][0]["total"] == 4
    assert w1["group_diff"]["changed"] == [(0, "east")]
    assert w2["oracle_after"] == [] and w2["group_diff"]["removed"]
    assert a1["group_diff"] == {"added": [(0, "east")],
                                "removed": [(0, "west")], "changed": []}
    assert a2["oracle_after"][0]["region"] == "__UNASSIGNED__"
    assert a3["oracle_after"][0]["region"] == "east"
    assert (0, "__UNASSIGNED__") in h1["group_diff"]["added"]
    assert h1["observed_before"] != h1["oracle_after"]

    path = write_report("e2_counterfactuals.json",
                        {"layer": "OBSERVED+ORACLE", "cases": report})
    print(json.dumps({"report": os.path.relpath(path),
                      "cases": len(report),
                      "H1_added": h1["group_diff"]["added"],
                      "L1_promoted_then_filtered":
                          l1["gen2_oracle_withdraw_alice"]},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
