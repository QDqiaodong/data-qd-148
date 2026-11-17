"""Randomized differential testing against the physical-deletion SQLite oracle.

Small random schemas, units (including public NULL rows), NULL join keys and
random plans covering filter / inner / left / anti joins and every aggregate
metric are generated with a fixed seed.  The production report's per-unit
``after`` values must equal an oracle that physically deletes each unit and
reruns the query in SQL.
"""
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from privacy_budget.influence_api import audit_influence, finalize_report
from influence_oracle import ReferenceOracle

SCHEMA_TWO = """
CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
"""
UNITS = ["U", "V", "W", None]
KEYS = ["x", "y", None]


def make_data(path, rng):
    connection = sqlite3.connect(str(path))
    connection.executescript(SCHEMA_TWO)
    for table, prefix in (("l", "l"), ("r", "r")):
        n = rng.randint(1, 8)
        rows = []
        for i in range(n):
            rows.append((
                "%s%d" % (prefix, i),
                rng.choice(UNITS),
                rng.choice(KEYS),
                rng.choice([None, -5, -1, 0, 1, 7]),
            ))
        connection.executemany(
            "INSERT INTO " + table + " VALUES (?,?,?,?)", rows
        )
    connection.commit()
    connection.close()


def random_plan(rng):
    join_op = rng.choice(["inner", "left", "anti"])
    use_filter = rng.choice([True, False])
    filter_pred = rng.choice(["eq", "is_null"])
    nodes = [
        {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
        {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
        {
            "id": "j", "op": join_op, "left": "sl", "right": "sr",
            "on": [{"left_column": "a.k", "right_column": "b.k"}],
        },
    ]
    join_output = "j"
    if use_filter:
        if filter_pred == "eq":
            column = rng.choice(["a.k", "a.v"])
            value = "x" if column == "a.k" else rng.choice([-5, 1, 7])
            node = {
                "id": "f", "op": "filter", "input": "j",
                "column": column, "predicate": "eq", "value": value,
            }
        else:
            column = rng.choice(["a.k", "b.k", "b.v"] if join_op != "anti"
                                else ["a.k", "a.v"])
            node = {
                "id": "f", "op": "filter", "input": "j",
                "column": column, "predicate": "is_null", "value": None,
            }
        nodes.append(node)
        join_output = "f"
    metrics = [
        {"name": "n", "op": "count_rows", "column": None},
        {"name": "cnt", "op": "count", "column": "a.v"},
        {"name": "sm", "op": "sum", "column": "a.v"},
        {"name": "dist", "op": "count_distinct", "column": "a.k"},
    ]
    rng.shuffle(metrics)
    keys = rng.choice([[], ["a.k"], ["a.v"]])
    if join_op == "anti":
        # Right-side columns are out of scope after anti.
        metrics = [m for m in metrics if not m["column"] or m["column"].startswith("a.")]
        keys = [key for key in keys if key.startswith("a.")]
    nodes.append({
        "id": "g", "op": "aggregate", "input": join_output,
        "keys": keys, "metrics": metrics,
    })
    return {
        "tables": [
            {"name": "l", "id_column": "id", "unit_column": "unit",
             "columns": {"k": "text", "v": "integer"}},
            {"name": "r", "id_column": "id", "unit_column": "unit",
             "columns": {"k": "text", "v": "integer"}},
        ],
        "nodes": nodes,
        "root": "g",
    }


class FuzzAgainstOracle(unittest.TestCase):
    def test_many_random_plans(self):
        rng = random.Random(20261117)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for case in range(60):
                db = base / ("case%d.db" % case)
                make_data(db, rng)
                query = random_plan(rng)
                query_path = base / ("case%d.json" % case)
                query_path.write_text(json.dumps(query), encoding="utf-8")
                report = finalize_report(audit_influence(str(db), str(query_path)))
                units = [unit["unit"] for unit in report["units"]]
                oracle = ReferenceOracle(str(db), query)
                try:
                    expected = oracle.run_all_units(units)
                finally:
                    oracle.close()
                metric_names = report["baseline"]["metric_names"]
                baseline = {
                    tuple(group["group_key"]): dict(group["metrics"])
                    for group in report["baseline"]["groups"]
                }
                for unit in report["units"]:
                    after = {key: dict(metrics) for key, metrics in baseline.items()}
                    for cell in unit["changed_cells"]:
                        key = tuple(cell["group_key"])
                        after.setdefault(key, {name: 0 for name in metric_names})
                        after[key][cell["metric"]] = cell["after"]
                    expected_after = expected[unit["unit"]]
                    all_keys = set(after) | set(expected_after)
                    for key in all_keys:
                        for name in metric_names:
                            self.assertEqual(
                                after.get(key, {}).get(name, 0),
                                expected_after.get(key, {}).get(name, 0),
                                "case %d unit %s key %s metric %s\nquery=%s"
                                % (case, unit["unit"], key, name, json.dumps(query)),
                            )


if __name__ == "__main__":
    unittest.main()
