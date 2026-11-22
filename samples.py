from copy import deepcopy


INITIAL = {
    "events": [[
        {"id": "e1", "owners": ["bob"], "device": "d1", "at": 10, "amount": 10},
        {"id": "e2", "owners": ["alice"], "device": "d1", "at": 20, "amount": 30},
        {"id": "e3", "owners": ["cara"], "device": "d2", "at": 15, "amount": 7},
        {"id": "e4", "owners": ["dana"], "device": "d3", "at": 15, "amount": 5},
        {"id": "e5", "owners": ["alice", "bob"], "device": "d4", "at": 15, "amount": 11}
    ]],
    "assignments": [[
        {"id": "a1", "owners": ["bob"], "device": "d1", "at": 0, "region": "east"},
        {"id": "a2", "owners": ["alice"], "device": "d1", "at": 12, "region": "west"},
        {"id": "a3", "owners": ["cara"], "device": "d2", "at": 0, "region": "east"},
        {"id": "a4", "owners": ["dana"], "device": "d3", "at": 0, "region": "west"},
        {"id": "a5", "owners": ["bob"], "device": "d4", "at": 0, "region": "east"}
    ]],
    "holds": [[
        {"id": "h1", "owners": ["alice"], "device": "d3", "day": 0}
    ]]
}


def initial():
    return deepcopy(INITIAL)


def next_events():
    # Keep the old block; append a new immutable block, not a row mutation.
    return {"events": deepcopy(INITIAL["events"]) + [[
        {"id": "e6", "owners": ["cara"], "device": "d2", "at": 30, "amount": 9}
    ]]}


def seeded(lake):
    lake.publish("first", lake.prepare(0, initial()))
    lake.publish("second", lake.prepare(1, next_events()))
    return lake
