"""Single entry point for the design evidence package.

Usage:
  python3 evidence/run.py verify   # original regression + all evidence +
                                   # report/DESIGN cross-checks (nonzero exit)
  python3 evidence/run.py demo     # real baseline results + withdrawal
                                   # counterfactuals on the sample data
  python3 evidence/run.py evidence # regenerate evidence/out/*.json only
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(ROOT, "evidence")
sys.path.insert(0, ROOT)
sys.path.insert(0, EVID)

MODULES = ["e1_baseline", "e2_counterfactuals", "e3_closure", "e4_timelines"]


def run(cmd):
    print("\n=== $ %s ===" % " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def run_evidence():
    for mod in MODULES:
        run([sys.executable, os.path.join("evidence", mod + ".py")])


def cross_check():
    """Verify saved reports against fresh ORACLE recomputation and check
    DESIGN.md line references against the real sources."""
    from verify_reports import main as verify_main
    verify_main()


def demo():
    import probe
    print(json.dumps(probe.observe(), ensure_ascii=False, sort_keys=True,
                     indent=2))
    from e1_baseline import main as e1
    e1()
    from e2_counterfactuals import main as e2
    e2()
    # Real, visible difference driven through the unmodified business path:
    # controlled reads equal "delete whole owners-rows then frozen evaluate".
    from lake import Lake
    from samples import seeded
    from common import oracle, diff_groups
    lake = seeded(Lake())
    print("\nReal before -> after-withdrawal differences (sample data):")
    for gen in (1, 2):
        raw = lake.raw(gen)
        from query import evaluate
        before = evaluate(raw)
        after = oracle(raw, ["alice"])
        print(json.dumps({
            "generation": gen,
            "before": before,
            "after_alice_withdrawn": after,
            "group_diff": diff_groups(before, after),
        }, ensure_ascii=False, sort_keys=True))
    lake.close()
    print("\nDemo complete: baseline above is the unmodified business output;")
    print("oracle_after fields in evidence/out are the required post-withdrawal")
    print("results (whole-row owners filter, then frozen query.evaluate).")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if target == "verify":
        run([sys.executable, "checks.py"])        # original regression first
        run_evidence()
        cross_check()
        print("\nVERIFY_OK: regression, evidence and report checks passed.")
    elif target == "evidence":
        run_evidence()
        print("\nEVIDENCE_OK")
    elif target == "demo":
        demo()
    else:
        raise SystemExit("unknown target: %s" % target)


if __name__ == "__main__":
    main()
