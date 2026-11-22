import json
from lake import Lake, Reader
from probe import observe
from samples import initial, seeded


def cell(region, devices, total, maximum):
    return {"day": 0, "region": region, "devices": devices,
            "total": total, "maximum": maximum}


def main():
    result = observe()
    first, second = result["histories"]
    assert first["before"] == [cell("east", 2, 18, 11), cell("west", 1, 30, 30)]
    assert second["before"] == [cell("east", 2, 20, 11), cell("west", 1, 30, 30)]
    assert first["without_alice"] == [
        cell("east", 2, 17, 10), cell("west", 1, 5, 5)]
    assert second["without_alice"] == [
        cell("east", 2, 19, 10), cell("west", 1, 5, 5)]
    assert first["without_both"] == [
        cell("east", 1, 7, 7), cell("west", 1, 5, 5)]
    assert second["without_both"] == [
        cell("east", 1, 9, 9), cell("west", 1, 5, 5)]
    assert first["roots"]["events"][0] == second["roots"]["events"][0]
    assert first["roots"]["holds"] == second["roots"]["holds"]
    assert result["pinned_after_publish"] == first["before"]
    assert result["head_before_restore"] == 3
    assert result["head_after_restore"] == result["image_generation"] == 2
    assert result["replayed_generation"] == 1
    lake = seeded(Lake())
    plan = lake.prepare(1, {"holds": [[]]})
    before = Reader(lake).export(Reader(lake).pin())
    try:
        lake.publish("stale", plan)
    except ValueError as error:
        assert str(error) == "STALE_HEAD"
    else:
        raise AssertionError("stale plan accepted")
    assert lake.head() == 2
    assert Reader(lake).export(Reader(lake).pin()) == before
    plan = lake.prepare(0, initial())
    assert lake.publish("first", plan) == 1
    try:
        lake.publish("first", lake.prepare(0, {"holds": [[]]}))
    except ValueError as error:
        assert str(error) == "REQUEST_CONFLICT"
    else:
        raise AssertionError("changed request accepted")
    lake.close()
    print(json.dumps({"marker": "NORMAL_REGRESSION_OK",
                      "first": first["before"],
                      "counterfactual": first["without_alice"]},
                     sort_keys=True))


if __name__ == "__main__":
    main()
