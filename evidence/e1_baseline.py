"""E1: observed business baseline + oracle counterfactuals on samples.py.

Everything here is OBSERVED (probe.observe / real Reader output) or ORACLE
(filter + frozen evaluate). No withdrawal engine is introduced.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

from lake import Lake, Reader
from probe import observe
from samples import seeded
from common import oracle, write_report


def main():
    observed = observe()                       # identical code path to demo
    lake = seeded(Lake())
    oracle_rows = {}
    for gen in (1, 2):
        raw = lake.raw(gen)
        oracle_rows[str(gen)] = {
            "before": Reader(lake).export(Reader(lake).pin(gen)),
            "without_alice": oracle(raw, ["alice"]),
            "without_bob": oracle(raw, ["bob"]),
            "without_both": oracle(raw, ["alice", "bob"]),
        }
    lake.close()

    # ORACLE must exactly reproduce probe's offline without_* values.
    checks = []
    for i, gen in enumerate((1, 2)):
        h = observed["histories"][i]
        for tag in ("without_alice", "without_bob", "without_both"):
            ok = h[tag] == oracle_rows[str(gen)][tag]
            checks.append({"check": f"gen{gen}.{tag}", "ok": ok})
            assert ok
        checks.append({
            "check": f"gen{gen}.before_matches_reader",
            "ok": h["before"] == oracle_rows[str(gen)]["before"]})
        assert h["before"] == oracle_rows[str(gen)]["before"]

    payload = {
        "layer": "OBSERVED+ORACLE",
        "observed_summary": {
            "head_before_restore": observed["head_before_restore"],
            "head_after_restore": observed["head_after_restore"],
            "image_generation": observed["image_generation"],
            "replayed_generation": observed["replayed_generation"],
            "pinned_after_publish": observed["pinned_after_publish"],
        },
        "oracle_counterfactuals": oracle_rows,
        "checks": checks,
        "note": "oracle = whole-row owners filter then frozen query.evaluate; "
                "identical to probe.without values by construction.",
    }
    path = write_report("e1_baseline.json", payload)
    print(json.dumps({"report": os.path.relpath(path),
                      "checks_passed": len(checks),
                      "gen1_without_alice":
                          oracle_rows["1"]["without_alice"]},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
