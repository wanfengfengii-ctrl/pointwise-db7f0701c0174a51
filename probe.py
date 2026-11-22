"""Counterfactuals use the existing full evaluator, not an online eraser."""
import json
from lake import Lake, Reader
from query import evaluate
from samples import seeded, initial


def without(tables, withdrawn):
    return {kind: [row for row in rows
                   if not set(row["owners"]) & set(withdrawn)]
            for kind, rows in tables.items()}


def observe():
    lake = seeded(Lake())
    reader = Reader(lake)
    old = reader.pin(1)
    image = lake.checkpoint()
    histories = []
    for generation in (1, 2):
        raw = lake.raw(generation)
        histories.append({
            "generation": generation,
            "roots": lake.snapshot(generation)["roots"],
            "before": reader.export(reader.pin(generation)),
            "without_alice": evaluate(without(raw, ["alice"])),
            "without_bob": evaluate(without(raw, ["bob"])),
            "without_both": evaluate(without(raw, ["alice", "bob"]))
        })
    # A prepared replacement can still publish after an arbitrary time gap.
    plan = lake.prepare(2, {"holds": [[]]})
    lake.publish("third", plan)
    replay = lake.publish("first", lake.prepare(0, initial()))
    head_before_restore = lake.head()
    lake.restore(image)
    result = {
        "histories": histories,
        "pinned_after_publish": reader.export(old),
        "image_generation": image.head(),
        "head_before_restore": head_before_restore,
        "head_after_restore": lake.head(),
        "replayed_generation": replay
    }
    image.close()
    lake.close()
    return result


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, sort_keys=True, indent=2))
