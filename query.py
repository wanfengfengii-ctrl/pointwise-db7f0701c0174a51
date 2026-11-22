"""Frozen research query; ownership is input metadata, not a query filter."""
KINDS = ("events", "assignments", "holds")


def evaluate(tables):
    latest = {}
    for event in tables["events"]:
        key = (event["device"], event["at"] // 86400)
        if key not in latest or (event["at"], event["id"]) > (
                latest[key]["at"], latest[key]["id"]):
            latest[key] = event
    blocked = {(r["device"], r["day"]) for r in tables["holds"]}
    groups = {}
    for (device, day), event in sorted(latest.items()):
        if (device, day) in blocked:
            continue
        eligible = [a for a in tables["assignments"]
                    if a["device"] == device and a["at"] <= event["at"]]
        assignment = max(eligible, key=lambda a: (a["at"], a["id"]),
                         default=None)
        region = assignment["region"] if assignment else "__UNASSIGNED__"
        group = groups.setdefault((day, region), {"devices": set(), "values": []})
        group["devices"].add(device)
        group["values"].append(event["amount"])
    return [{"day": day, "region": region, "devices": len(group["devices"]),
             "total": sum(group["values"]), "maximum": max(group["values"])}
            for (day, region), group in sorted(groups.items())]


def validate(tables):
    if set(tables) != set(KINDS):
        raise ValueError("TABLES")
    for kind in KINDS:
        seen = set()
        for row in tables[kind]:
            if not isinstance(row, dict):
                raise ValueError("ROW")
            expected = {"id", "owners", "device"}
            expected |= {"day"} if kind == "holds" else {"at"}
            expected |= {"amount"} if kind == "events" else set()
            expected |= {"region"} if kind == "assignments" else set()
            if set(row) != expected:
                raise ValueError("FIELDS")
            if any(not isinstance(row[k], str) or not row[k]
                   for k in ("id", "device")):
                raise ValueError("IDENTITY")
            if row["id"] in seen:
                raise ValueError("DUPLICATE_ID")
            seen.add(row["id"])
            owners = row["owners"]
            if (not isinstance(owners, list) or not owners
                    or any(not isinstance(v, str) or not v for v in owners)
                    or len(set(owners)) != len(owners)):
                raise ValueError("OWNERS")
            numeric = ("day",) if kind == "holds" else ("at",)
            if kind == "events":
                numeric += ("amount",)
            if any(type(row[k]) is not int for k in numeric):
                raise ValueError("INTEGER")
            if kind == "assignments" and (
                    not isinstance(row["region"], str) or not row["region"]):
                raise ValueError("REGION")
