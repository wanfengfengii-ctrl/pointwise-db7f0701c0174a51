"""Existing immutable-snapshot lake. No withdrawal implementation exists."""
import hashlib
import json
import sqlite3
from query import KINDS, evaluate, validate


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


class Lake:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects(
                oid TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(
                generation INTEGER PRIMARY KEY, roots TEXT NOT NULL,
                result_oid TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS head(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                generation INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(
                request_id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                generation INTEGER NOT NULL);
            INSERT OR IGNORE INTO head VALUES(1,0);
        """)
        if not self.db.execute("SELECT 1 FROM snapshots").fetchone():
            empty = self.put([])
            roots = {kind: [empty] for kind in KINDS}
            self.db.execute("INSERT INTO snapshots VALUES(0,?,?)",
                            (canonical(roots), empty))

    def put(self, value):
        payload = canonical(value)
        oid = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO objects VALUES(?,?)",
                        (oid, payload))
        return oid

    def get(self, oid):
        row = self.db.execute(
            "SELECT payload FROM objects WHERE oid=?", (oid,)).fetchone()
        if row is None:
            raise KeyError(oid)
        return json.loads(row[0])

    def head(self):
        return self.db.execute("SELECT generation FROM head").fetchone()[0]

    def snapshot(self, generation):
        row = self.db.execute(
            "SELECT roots,result_oid FROM snapshots WHERE generation=?",
            (generation,)).fetchone()
        if row is None:
            raise KeyError(generation)
        return {"generation": generation, "roots": json.loads(row[0]),
                "result_oid": row[1]}

    def materialize(self, roots):
        return {kind: [row for oid in roots[kind] for row in self.get(oid)]
                for kind in KINDS}

    def prepare(self, expected, replacements):
        # Object writes deliberately happen outside the metadata transaction.
        base = self.snapshot(expected)
        roots = dict(base["roots"])
        if not set(replacements) <= set(KINDS):
            raise ValueError("TABLES")
        for kind, blocks in replacements.items():
            if not isinstance(blocks, list) or any(
                    not isinstance(block, list) for block in blocks):
                raise ValueError("BLOCKS")
            roots[kind] = [self.put(block) for block in blocks]
        tables = self.materialize(roots)
        validate(tables)
        return {"base": expected, "roots": roots,
                "result_oid": self.put(evaluate(tables))}

    def publish(self, request_id, plan):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("REQUEST_ID")
        binding = canonical(plan)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT binding,generation FROM receipts WHERE request_id=?",
                (request_id,)).fetchone()
            if prior:
                if prior[0] != binding:
                    raise ValueError("REQUEST_CONFLICT")
                result = prior[1]
            else:
                if self.head() != plan["base"]:
                    raise ValueError("STALE_HEAD")
                result = plan["base"] + 1
                self.db.execute("INSERT INTO snapshots VALUES(?,?,?)",
                                (result, canonical(plan["roots"]),
                                 plan["result_oid"]))
                self.db.execute("UPDATE head SET generation=?", (result,))
                self.db.execute("INSERT INTO receipts VALUES(?,?,?)",
                                (request_id, binding, result))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def raw(self, generation):
        return self.materialize(self.snapshot(generation)["roots"])

    def checkpoint(self, path=":memory:"):
        image = Lake(path)
        self.db.backup(image.db)
        return image

    def restore(self, image):
        image.db.backup(self.db)

    def close(self):
        self.db.close()


class Reader:
    def __init__(self, lake):
        self.lake = lake
        self.cache = {}

    def pin(self, generation=None):
        if generation is None:
            generation = self.lake.head()
        snap = self.lake.snapshot(generation)
        oid = snap["result_oid"]
        if oid not in self.cache:
            self.cache[oid] = self.lake.get(oid)
        # Simulates a queued export holding both metadata and cached bytes.
        return {"generation": generation, "oid": oid,
                "rows": canonical(self.cache[oid])}

    def export(self, handle):
        return json.loads(handle["rows"])
