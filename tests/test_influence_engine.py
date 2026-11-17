"""Engine semantics: NULLs, bag multiplicity, self joins, left/anti, and the
exact per-unit counterfactual diffs, cross-checked with an independent SQLite
oracle that physically deletes each unit's rows."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from privacy_budget.influence_api import audit_influence, finalize_report
from privacy_budget.influence_engine import InfluenceEngine
from privacy_budget.influence_model import parse_query
from privacy_budget.influence_snapshot import (
    close_snapshot,
    open_snapshot_connection,
    read_snapshot,
)
from influence_oracle import ReferenceOracle


def make_database(path, schema_sql, inserts):
    connection = sqlite3.connect(str(path))
    connection.executescript(schema_sql)
    for statement, rows in inserts:
        connection.executemany(statement, rows)
    connection.commit()
    connection.close()


def build_engine(db_path, query):
    parsed = parse_query(query)
    connection = open_snapshot_connection(db_path)
    try:
        snapshot, _ = read_snapshot(connection, parsed["tables"])
    finally:
        close_snapshot(connection)
    engine = InfluenceEngine(parsed, snapshot)
    engine.evaluate_baseline()
    return parsed, engine


def oracle_answers(db_path, query, units):
    oracle = ReferenceOracle(str(db_path), query)
    try:
        return oracle.baseline(), oracle.run_all_units(units)
    finally:
        oracle.close()


def metric_map(report):
    return {
        tuple(group["group_key"]): dict(group["metrics"])
        for group in report["baseline"]["groups"]
    }


class EngineSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _query(self, payload):
        self.query_path = self.base / "query.json"
        self.query_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _audit(self, db_name="source.db"):
        return finalize_report(
            audit_influence(str(self.base / db_name), str(self.query_path))
        )

    def test_null_join_keys_never_match_and_bag_multiplicity(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [
                    ("l1", "U", None, 1), ("l2", "U", "x", 1), ("l3", "U", "x", 1),
                ]),
                ("INSERT INTO r VALUES (?,?,?,?)", [
                    ("r1", "V", "x", 2), ("r2", "V", "x", 2),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
                {"id": "j", "op": "inner", "left": "sl", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [
                     {"name": "n", "op": "count_rows", "column": None},
                     {"name": "s", "op": "sum", "column": "b.v"},
                 ]},
            ],
            "root": "g",
        })
        parsed, engine = build_engine(db, query)
        baseline = engine.relations["g"]
        values = next(iter(baseline.rows.values())).values
        # Two left 'x' rows x two right rows = four matches; NULL key never joins.
        self.assertEqual(values, (4, 8))
        report = self._audit("s.db")
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V"])
        self.assertEqual(metric_map(report)[()], oracle_baseline[()])
        self._check_oracle(report, oracle_units)

    def test_left_null_extension_and_filter_eq_null(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [("l0", None, "x", 1)]),
                ("INSERT INTO r VALUES (?,?,?,?)", [
                    ("r1", "U", "x", 2), ("r2", "U", "x", 2),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
                {"id": "j", "op": "left", "left": "sl", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "f", "op": "filter", "input": "j", "column": "b.id",
                 "predicate": "is_null", "value": None},
                {"id": "g", "op": "aggregate", "input": "f", "keys": [],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # The headline linkage example: baseline 0, removing U yields 1.
        self.assertEqual(metric_map(report)[()]["n"], 0)
        unit_report = next(item for item in report["units"] if item["unit"] == "U")
        self.assertEqual(unit_report["changed_cells"][0]["delta"], 1)
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U"])
        self._check_oracle(report, oracle_units)

    def test_partial_match_removal_does_not_create_extension(self):
        # Two distinct right rows, same join value, owned by different units.
        # Removing just one still leaves a match: no null extension may appear.
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [("l0", None, "x", 1)]),
                ("INSERT INTO r VALUES (?,?,?,?)", [
                    ("r1", "U", "x", 2), ("r2", "V", "x", 2),
                ]),
            ],
        )
        query = {
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
                {"id": "j", "op": "left", "left": "sl", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "f", "op": "filter", "input": "j", "column": "b.id",
                 "predicate": "is_null", "value": None},
                {"id": "g", "op": "aggregate", "input": "f", "keys": [],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        }
        self._query(query)
        report = self._audit("s.db")
        u_report = next(item for item in report["units"] if item["unit"] == "U")
        v_report = next(item for item in report["units"] if item["unit"] == "V")
        # Each single-unit removal leaves the other row matching: no change.
        self.assertEqual(u_report["changed_cells"], [])
        self.assertEqual(v_report["changed_cells"], [])
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V"])
        self._check_oracle(report, oracle_units)

    def test_anti_join_then_further_join(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE m (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [
                    ("l1", "U", "x", 1), ("l2", "V", "y", 1),
                ]),
                ("INSERT INTO m VALUES (?,?,?,?)", [
                    ("m1", "U", "x", 9),
                ]),
                ("INSERT INTO r VALUES (?,?,?,?)", [
                    ("r1", "W", "x", 2), ("r2", "W", "z", 2),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "m", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sm", "op": "scan", "table": "m", "alias": "b"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "c"},
                {"id": "anti", "op": "anti", "left": "sl", "right": "sm",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "j", "op": "inner", "left": "anti", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "c.k"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # Baseline: only l2 ('y') survives anti, and it finds no r match: 0 rows.
        self.assertEqual(metric_map(report)[()]["n"], 0)
        # Removing U deletes l1 and m1: l1 already anti-rejected, so no change;
        # removing V removes l2 itself.
        u_report = next(item for item in report["units"] if item["unit"] == "U")
        self.assertEqual(u_report["changed_cells"], [])
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V", "W"])
        self._check_oracle(report, oracle_units)

    def test_self_join_removes_source_row_once(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO t VALUES (?,?,?,?)", [
                    ("t1", "U", "x", 1), ("t2", "V", "x", 1),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "s1", "op": "scan", "table": "t", "alias": "a"},
                {"id": "s2", "op": "scan", "table": "t", "alias": "b"},
                {"id": "j", "op": "inner", "left": "s1", "right": "s2",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # Baseline 2x2 = 4. Remove U (t1): only t2 x t2 = 1, delta -3.
        self.assertEqual(metric_map(report)[()]["n"], 4)
        u_report = next(item for item in report["units"] if item["unit"] == "U")
        self.assertEqual(u_report["l1"], 3)
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V"])
        self._check_oracle(report, oracle_units)

    def test_distinct_last_support_and_shared_support(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, g TEXT, tag TEXT);
            """,
            [
                ("INSERT INTO t VALUES (?,?,?,?)", [
                    ("t1", "U", "x", "z"),
                    ("t2", "V", "x", "z"),
                    ("t3", "U", "x", "q"),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"g": "text", "tag": "text"}},
            ],
            "nodes": [
                {"id": "s", "op": "scan", "table": "t", "alias": "a"},
                {"id": "g", "op": "aggregate", "input": "s", "keys": ["a.g"],
                 "metrics": [
                     {"name": "d", "op": "count_distinct", "column": "a.tag"},
                     {"name": "n", "op": "count_rows", "column": None},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # Distinct tags {z, q} = 2. Removing U: z still supported by V, q lost
        # => distinct goes 2 -> 1, delta -1 (not -2).
        u_report = next(item for item in report["units"] if item["unit"] == "U")
        distinct_cell = next(
            cell for cell in u_report["changed_cells"] if cell["metric"] == "d"
        )
        self.assertEqual((distinct_cell["before"], distinct_cell["after"]), (2, 1))
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V"])
        self._check_oracle(report, oracle_units)

    def test_negative_sum_and_group_disappearance(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, g TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO t VALUES (?,?,?,?)", [
                    ("t1", "U", "x", -5), ("t2", "U", "x", 2),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"g": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "s", "op": "scan", "table": "t", "alias": "a"},
                {"id": "g", "op": "aggregate", "input": "s", "keys": ["a.g"],
                 "metrics": [
                     {"name": "sm", "op": "sum", "column": "a.v"},
                     {"name": "n", "op": "count_rows", "column": None},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        u_report = report["units"][0]
        cells = {cell["metric"]: cell for cell in u_report["changed_cells"]}
        self.assertEqual(cells["sm"]["before"], -3)
        self.assertEqual(cells["sm"]["after"], 0)
        self.assertEqual(cells["sm"]["delta"], 3)
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U"])
        self._check_oracle(report, oracle_units)

    def test_zero_influence_unit_listed_with_zero_norms(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, g TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO t VALUES (?,?,?,?)", [
                    ("t1", "U", "x", 1),
                    ("t2", "Z", "x", 1),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"g": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "s", "op": "scan", "table": "t", "alias": "a"},
                {"id": "f", "op": "filter", "input": "s", "column": "a.v",
                 "predicate": "eq", "value": 99},
                {"id": "g", "op": "aggregate", "input": "f", "keys": ["a.g"],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        for unit in report["units"]:
            self.assertEqual(unit["changed_cells"], [])
            self.assertEqual((unit["l1"], unit["linfinity"]), (0, 0))
        self.assertEqual(report["max_l1_witness_units"], ["U", "Z"])

    def test_empty_input_no_keys_yields_zero_row(self):
        db = self.base / "s.db"
        make_database(
            db,
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, g TEXT, v INTEGER);",
            [
                ("INSERT INTO t VALUES (?,?,?,?)", [
                    ("t1", "U", "x", 5),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"g": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "s", "op": "scan", "table": "t", "alias": "a"},
                {"id": "f", "op": "filter", "input": "s", "column": "a.g",
                 "predicate": "eq", "value": "nope"},
                {"id": "g", "op": "aggregate", "input": "f", "keys": [],
                 "metrics": [
                     {"name": "n", "op": "count_rows", "column": None},
                     {"name": "sm", "op": "sum", "column": "a.v"},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        self.assertEqual(metric_map(report)[()], {"n": 0, "sm": 0})
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U"])
        self.assertEqual(oracle_baseline[()], {"n": 0, "sm": 0})
        self._check_oracle(report, oracle_units)

    def test_public_left_rows_gain_null_extensions_when_match_support_hits_zero(self):
        # Public (NULL-unit) left rows cannot be removed, but they still gain
        # fresh left null extensions once the only right support disappears.
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [
                    ("l%04d" % i, None, "x", 1) for i in range(20)
                ]),
                ("INSERT INTO r VALUES (?,?,?,?)", [("r1", "U", "x", 7)]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
                {"id": "j", "op": "left", "left": "sl", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [
                     {"name": "n", "op": "count_rows", "column": None},
                     {"name": "buyers", "op": "count", "column": "b.id"},
                     {"name": "sm", "op": "sum", "column": "b.v"},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # count_rows stays 20 (matched rows turn into null extensions); the
        # right-side metrics collapse to zero for every public left row.
        u_report = report["units"][0]
        cells = {cell["metric"]: cell for cell in u_report["changed_cells"]}
        self.assertNotIn("n", cells)
        self.assertEqual(cells["buyers"]["delta"], -20)
        self.assertEqual(cells["sm"]["delta"], -140)
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U"])
        self._check_oracle(report, oracle_units)

    def test_anti_then_left_propagates_newly_created_rows(self):
        # An anti join can create a row only after a unit is removed; a later
        # left join must route that brand-new left row through its own null
        # extension / match logic rather than skipping tuples absent at baseline.
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE a (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE b (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE c (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO a VALUES (?,?,?,?)", [
                    ("a1", "U", "y", -2), ("a2", "V", "z", 3),
                ]),
                # The blocker is a different unit: removing it (not a1 itself)
                # lets a1 survive the anti join for the first time.
                ("INSERT INTO b VALUES (?,?,?,?)", [("b1", "B", "y", 9)]),
                ("INSERT INTO c VALUES (?,?,?,?)", [("c1", "W", "z", 5)]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "a", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "b", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "c", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sa", "op": "scan", "table": "a", "alias": "a"},
                {"id": "sb", "op": "scan", "table": "b", "alias": "b"},
                {"id": "sc", "op": "scan", "table": "c", "alias": "c"},
                {"id": "j1", "op": "anti", "left": "sa", "right": "sb",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "j2", "op": "left", "left": "j1", "right": "sc",
                 "on": [{"left_column": "a.k", "right_column": "c.k"}]},
                {"id": "g", "op": "aggregate", "input": "j2", "keys": [],
                 "metrics": [
                     {"name": "n", "op": "count_rows", "column": None},
                     {"name": "sm", "op": "sum", "column": "a.v"},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # Baseline: only a2 survives anti, joins c on z -> 1 matched row, sum 3.
        self.assertEqual(metric_map(report)[()], {"n": 1, "sm": 3})
        # Removing the blocker unit B lets a1 survive anti; a1 has no c match
        # and becomes a fresh left null extension. Rows: a2(matched) + a1(null).
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V", "W", "B"])
        self.assertEqual(oracle_baseline[()], {"n": 1, "sm": 3})
        self.assertEqual(oracle_units["B"][()], {"n": 2, "sm": 1})
        self._check_oracle(report, oracle_units)

    def test_unit_spanning_multiple_tables_removed_everywhere(self):
        db = self.base / "s.db"
        make_database(
            db,
            """
            CREATE TABLE l (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            CREATE TABLE r (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER);
            """,
            [
                ("INSERT INTO l VALUES (?,?,?,?)", [
                    ("l1", "U", "x", 1), ("l2", "V", "x", 1),
                ]),
                ("INSERT INTO r VALUES (?,?,?,?)", [
                    ("r1", "U", "x", 10), ("r2", "W", "x", 20),
                ]),
            ],
        )
        query = self._query({
            "tables": [
                {"name": "l", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
                {"name": "r", "id_column": "id", "unit_column": "unit",
                 "columns": {"k": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "sl", "op": "scan", "table": "l", "alias": "a"},
                {"id": "sr", "op": "scan", "table": "r", "alias": "b"},
                {"id": "j", "op": "inner", "left": "sl", "right": "sr",
                 "on": [{"left_column": "a.k", "right_column": "b.k"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [
                     {"name": "n", "op": "count_rows", "column": None},
                     {"name": "sm", "op": "sum", "column": "b.v"},
                 ]},
            ],
            "root": "g",
        })
        report = self._audit("s.db")
        # 2 x 2 = 4 join rows; right sum appears per left row -> 60.
        self.assertEqual(metric_map(report)[()], {"n": 4, "sm": 60})
        # Unit U owns rows in BOTH tables; one counterfactual removes both.
        oracle_baseline, oracle_units = oracle_answers(db, query, ["U", "V", "W"])
        self._check_oracle(report, oracle_units)

    def _check_oracle(self, report, oracle_units):
        baseline = metric_map(report)
        metric_names = report["baseline"]["metric_names"]
        for unit in report["units"]:
            expected = oracle_units[unit["unit"]]
            keys = set(baseline) | set(expected)
            actual_after = {}
            for key in keys:
                before = baseline.get(key, {name: 0 for name in metric_names})
                after = dict(before)
                for cell in unit["changed_cells"]:
                    if tuple(cell["group_key"]) == key:
                        after[cell["metric"]] = cell["after"]
                actual_after[key] = after
            for key in keys:
                for name in metric_names:
                    self.assertEqual(
                        actual_after[key].get(name, 0),
                        expected.get(key, {}).get(name, 0),
                        "unit %s key %s metric %s" % (unit["unit"], key, name),
                    )


if __name__ == "__main__":
    unittest.main()
