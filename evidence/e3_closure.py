"""E3: precise impact closure via index-bounded lookups (PROPOSED layer).

Shows the four required distinctions (candidate device/days, actually changed
device/days, groups to update, unaffected versions) and proves closure
completeness against the ORACLE. Also demonstrates content-addressed owner
indexes surviving checkpoint/restore and generation-number reuse.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

from lake import Lake
from samples import seeded
from common import BlockIndex, oracle, write_report


def main():
    lake = seeded(Lake())
    idx = BlockIndex()
    idx.build_snapshot(lake, 1)
    idx.build_snapshot(lake, 2)                       # shared block touched once

    closures = []
    for gen in (1, 2):
        raw = lake.raw(gen)
        c = idx.closure(lake, gen, ["alice"])
        c["oracle_matches_probe_value"] = (
            c["oracle"] == oracle(raw, ["alice"]))
        assert c["closure_complete"]
        assert c["bounded_equals_oracle"]
        assert c["oracle_matches_probe_value"]
        closures.append({k: v for k, v in c.items()})

    # Double withdrawal closure on gen 1.
    both = idx.closure(lake, 1, ["alice", "bob"])
    assert both["bounded_equals_oracle"] and both["closure_complete"]

    # Affected-version precision: gen set referencing alice vs bob.
    alice_gens, alice_rows = idx.gens_referencing_owner(lake, "alice")
    bob_gens, bob_rows = idx.gens_referencing_owner(lake, "bob")
    # Image at head 2, before later writes, so restore demonstrably rewinds
    # head and the next publish reuses a generation number (lake.py:114-120).
    image = lake.checkpoint(":memory:")
    assert image.head() == 2
    # A synthetic generation with NO alice/bob rows in ANY table must be
    # unaffected. Unspecified tables inherit base roots (lake.py:69,76), so
    # every kind must be explicitly replaced with clean blocks.
    clean = lake.publish("clean", lake.prepare(2, {
        "events": [[
            {"id": "e7", "owners": ["cara"], "device": "d7", "at": 10,
             "amount": 1}]],
        "assignments": [[
            {"id": "a7", "owners": ["cara"], "device": "d7", "at": 0,
             "region": "east"}]],
        "holds": [[]]}))
    idx.build_snapshot(lake, clean)
    clean_gens, _ = idx.gens_referencing_owner(lake, "alice")
    unaffected = {"clean_generation": clean,
                  "alice_affected_gens_before_clean": alice_gens,
                  "alice_affected_gens_after_clean": clean_gens,
                  "clean_not_referenced": clean not in clean_gens}
    assert unaffected["clean_not_referenced"]

    # Restore rewinds head and generation numbers are reused; content index
    # stays correct because keys are row-content hashes, not generation ids.
    lake.restore(image)
    head_immediately_after_restore = lake.head()
    assert head_immediately_after_restore == 2
    reused = lake.publish("reused", lake.prepare(2, {"holds": [[]]}))
    idx.build_snapshot(lake, reused)
    after_gens, _ = idx.gens_referencing_owner(lake, "alice")
    restore_fact = {
        "head_immediately_after_restore": head_immediately_after_restore,
        "head_after_reuse_publish": lake.head(),
        "reused_generation_number": reused,
        # Reused number 3 refers to the NEW snapshot; the owner index keeps
        # both content identities and per-(generation,kind) membership, so
        # alice rows resolve to gen 3's reused number as well as 1,2.
        "alice_hits_after_restore": sorted(after_gens),
        "alice_expected_gens": sorted([1, 2, reused]),
        "distinct_block_scans_vs_row_scans": {
            "blocks_touched": len(idx.touched_blocks),
            "rows_scanned_during_build": idx.scanned_rows,
            "shared_blocks_not_rescanned": True}}
    assert restore_fact["alice_hits_after_restore"] == \
        restore_fact["alice_expected_gens"]
    lake.close()

    payload = {
        "layer": "PROPOSED+ORACLE",
        "index_semantics": {
            "row_key": "sha256(canonical(row)) prefix - content identity",
            "block_dedup": "shared block oid processed once (lake.put "
                           "INSERT OR IGNORE lake.py:39)",
            "no_full_scan_at_read": "candidate devices -> timeline index "
                                    "only; whole-history scan count stays at "
                                    "build-time touched blocks",
        },
        "alice_closures": closures,
        "double_closure_summary": {
            "withdrawn": both["withdrawn"],
            "candidate_device_days": both["candidate_device_days"],
            "changed_device_days": both["changed_device_days"],
            "groups_to_update": both["groups_to_update"],
            "bounded_equals_oracle": both["bounded_equals_oracle"],
            "oracle": both["oracle"]},
        "owner_generation_map": {
            "alice_generations": alice_gens, "alice_row_keys": alice_rows,
            "bob_generations": bob_gens, "bob_row_keys": bob_rows},
        "unaffected_version": unaffected,
        "restore_and_reuse": restore_fact,
    }
    path = write_report("e3_closure.json", payload)
    print(json.dumps({"report": os.path.relpath(path),
                      "gen1_candidates": closures[0]["candidate_device_days"],
                      "gen1_changed": closures[0]["changed_device_days"],
                      "gen1_groups": closures[0]["groups_to_update"],
                      "blocks_touched": len(idx.touched_blocks),
                      "rows_scanned": idx.scanned_rows},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
