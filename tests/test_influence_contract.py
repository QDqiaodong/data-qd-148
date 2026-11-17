"""Strict contract tests for the influence query parser and snapshot reader."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support
from privacy_budget.influence_model import (
    InfluenceError,
    canonical_query,
    parse_query,
    query_sha256,
)
from privacy_budget.influence_snapshot import (
    close_snapshot,
    open_snapshot_connection,
    read_snapshot,
)


def table(name="t", id_column="id", unit_column="unit", columns=None):
    return {
        "name": name,
        "id_column": id_column,
        "unit_column": unit_column,
        "columns": columns if columns is not None else {"k": "text", "v": "integer"},
    }


def scan(node_id, table_name="t", alias="a"):
    return {"id": node_id, "op": "scan", "table": table_name, "alias": alias}


def aggregate(node_id, input_node, keys=None, metrics=None):
    return {
        "id": node_id,
        "op": "aggregate",
        "input": input_node,
        "keys": keys if keys is not None else [],
        "metrics": metrics if metrics is not None else [
            {"name": "n", "op": "count_rows", "column": None}
        ],
    }


def simple_query(**overrides):
    query = {
        "tables": [table()],
        "nodes": [scan("s"), aggregate("g", "s")],
        "root": "g",
    }
    query.update(overrides)
    return query


class QueryContract(unittest.TestCase):
    def assert_rejected(self, payload, fragment=""):
        with self.assertRaises(InfluenceError) as caught:
            parse_query(payload)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_minimal_query_accepted(self):
        parsed = parse_query(simple_query())
        self.assertEqual(parsed["root"], "g")

    def test_unknown_top_level_field(self):
        payload = simple_query()
        payload["extra"] = 1
        self.assert_rejected(payload, "exactly fields")

    def test_unknown_nested_field(self):
        payload = simple_query()
        payload["nodes"][0]["bogus"] = 1
        self.assert_rejected(payload, "exactly fields")

    def test_table_count_bounds(self):
        self.assert_rejected(simple_query(tables=[]), "length 1..4")
        payload = simple_query(tables=[table("t%d" % i) for i in range(5)])
        self.assert_rejected(payload, "length 1..4")

    def test_node_count_bounds(self):
        self.assert_rejected(simple_query(nodes=[]), "length 1..12")

    def test_duplicate_table_and_node_ids(self):
        self.assert_rejected(
            simple_query(tables=[table("t"), table("t")]), "duplicate table"
        )
        self.assert_rejected(
            simple_query(nodes=[scan("s"), scan("s"), aggregate("g", "s")]),
            "duplicate node id",
        )

    def test_id_and_unit_column_rules(self):
        bad = table(id_column="unit", unit_column="unit")
        self.assert_rejected(simple_query(tables=[bad]), "must differ")
        bad = table(columns={"id": "integer"})
        self.assert_rejected(simple_query(tables=[bad]), "collides")
        bad = table(columns={"v": "float"})
        self.assert_rejected(simple_query(tables=[bad]), "integer or text")

    def test_identifier_rules(self):
        self.assert_rejected(
            simple_query(tables=[table(name=" bad name")]), "identifier"
        )

    def test_root_must_be_aggregate(self):
        payload = simple_query(root="s")
        self.assert_rejected(payload, "terminal aggregate")

    def test_only_root_aggregates(self):
        nodes = [
            scan("s"),
            aggregate("g1", "s"),
            {"id": "f", "op": "filter", "input": "g1", "column": "a.k",
             "predicate": "is_null", "value": None},
            aggregate("g2", "f"),
        ]
        self.assert_rejected(simple_query(nodes=nodes, root="g2"), "only the root")

    def test_unknown_node_reference_and_cycle(self):
        nodes = [scan("s"), aggregate("g", "missing")]
        self.assert_rejected(simple_query(nodes=nodes), "unknown node reference")
        cyclic = [
            {"id": "j", "op": "inner", "left": "j", "right": "s2",
             "on": [{"left_column": "a.k", "right_column": "b.k"}]},
            scan("s"), scan("s2", alias="b"),
            aggregate("g", "j"),
        ]
        self.assert_rejected(simple_query(nodes=cyclic), "acyclic")

    def test_unreachable_node_rejected(self):
        nodes = [
            scan("s"),
            scan("orphan", alias="o"),
            aggregate("g", "s"),
        ]
        self.assert_rejected(simple_query(nodes=nodes), "reachable")

    def test_filter_predicate_contract(self):
        good = {
            "id": "f", "op": "filter", "input": "s", "column": "a.v",
            "predicate": "eq", "value": 3,
        }
        parse_query(simple_query(nodes=[scan("s"), good, aggregate("g", "f")]))
        bad_type = dict(good, value="text")
        with self.assertRaises(InfluenceError):
            parse_query(simple_query(nodes=[scan("s"), bad_type, aggregate("g", "f")]))
        bad_null = dict(good, value=None)
        self.assert_rejected(
            simple_query(nodes=[scan("s"), bad_null, aggregate("g", "f")]),
            "must not be null",
        )
        bad_pred = dict(good, predicate="gt")
        self.assert_rejected(
            simple_query(nodes=[scan("s"), bad_pred, aggregate("g", "f")]),
            "eq or is_null",
        )
        is_null = dict(good, predicate="is_null", column="a.k", value=None)
        parse_query(simple_query(nodes=[scan("s"), is_null, aggregate("g", "f")]))
        is_null_bad = dict(is_null, value="x")
        self.assert_rejected(
            simple_query(nodes=[scan("s"), is_null_bad, aggregate("g", "f")]),
            "must be null",
        )

    def test_join_column_overlap_and_unknown_columns(self):
        # Reusing one scan on both sides makes the qualified column names
        # overlap; self joins need two scans with distinct aliases.
        nodes = [
            scan("s", alias="a"),
            {"id": "j", "op": "inner", "left": "s", "right": "s",
             "on": [{"left_column": "a.k", "right_column": "a.k"}]},
            aggregate("g", "j"),
        ]
        self.assert_rejected(simple_query(nodes=nodes), "overlap")
        nodes = [
            scan("l", alias="a"), scan("r", table_name="t", alias="b"),
            {"id": "j", "op": "inner", "left": "l", "right": "r",
             "on": [{"left_column": "a.missing", "right_column": "b.k"}]},
            aggregate("g", "j"),
        ]
        self.assert_rejected(simple_query(nodes=nodes), "not in scope")

    def test_anti_keeps_left_columns(self):
        nodes = [
            scan("l", alias="a"), scan("r", table_name="t", alias="b"),
            {"id": "j", "op": "anti", "left": "l", "right": "r",
             "on": [{"left_column": "a.k", "right_column": "b.k"}]},
            aggregate("g", "j"),
        ]
        # Parsing and full validation succeed; the anti output references only
        # left-side columns inside the aggregate input scope.
        parsed = parse_query(simple_query(nodes=nodes))
        self.assertEqual(parsed["nodes"]["j"]["op"], "anti")

    def test_metric_contract(self):
        bad_rows = aggregate("g", "s", metrics=[
            {"name": "n", "op": "count_rows", "column": "a.v"}
        ])
        self.assert_rejected(simple_query(nodes=[scan("s"), bad_rows]), "null column")
        bad_sum = aggregate("g", "s", metrics=[
            {"name": "n", "op": "sum", "column": "a.k"}
        ])
        self.assert_rejected(simple_query(nodes=[scan("s"), bad_sum]), "integer")
        good_sum = aggregate("g", "s", metrics=[
            {"name": "n", "op": "sum", "column": "a.v"}
        ])
        parse_query(simple_query(nodes=[scan("s"), good_sum]))
        duplicate = aggregate("g", "s", keys=["a.k"], metrics=[
            {"name": "a.k", "op": "count_rows", "column": None}
        ])
        self.assert_rejected(simple_query(nodes=[scan("s"), duplicate]), "shadow")

    def test_canonical_form_is_order_insensitive(self):
        first = parse_query(simple_query())
        reordered = simple_query()
        reordered["tables"] = list(reversed(reordered["tables"]))
        reordered["nodes"] = list(reversed(reordered["nodes"]))
        second = parse_query(reordered)
        self.assertEqual(canonical_query(first), canonical_query(second))
        self.assertEqual(query_sha256(first), query_sha256(second))

    def test_literal_bounds(self):
        big = {
            "id": "f", "op": "filter", "input": "s", "column": "a.v",
            "predicate": "eq", "value": 10 ** 12 + 1,
        }
        self.assert_rejected(
            simple_query(nodes=[scan("s"), big, aggregate("g", "f")]),
            "within +-",
        )


class SnapshotContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "source.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _connection(self):
        return sqlite3.connect(str(self.db))

    def _build(self, ddl, rows=None, table_name="t"):
        connection = self._connection()
        connection.execute(ddl)
        if rows:
            connection.executemany(
                "INSERT INTO " + table_name + " VALUES (?,?,?,?)", rows
            )
        connection.commit()
        connection.close()

    def _read(self, query):
        parsed = parse_query(query)
        connection = open_snapshot_connection(self.db)
        try:
            snapshot, digest = read_snapshot(connection, parsed["tables"])
        finally:
            close_snapshot(connection)
        return snapshot, digest

    def test_happy_path_and_identical_values_distinct_ids(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)",
            [("i1", "U", "x", 1), ("i2", "U", "x", 1), ("i3", None, "y", None)],
        )
        snapshot, digest = self._read(simple_query())
        self.assertEqual(len(snapshot["t"]), 3)
        # Same values, different identities stay distinct rows.
        self.assertEqual(snapshot["t"][0]["_id"], "i1")
        self.assertEqual(snapshot["t"][1]["_id"], "i2")
        self.assertIsNotNone(digest)

    def test_missing_table(self):
        self._build(
            "CREATE TABLE other (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)"
        )
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_view_rejected(self):
        connection = self._connection()
        connection.execute(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)"
        )
        connection.execute("CREATE VIEW vw AS SELECT * FROM t")
        connection.commit()
        connection.close()
        query = simple_query(tables=[table(name="vw")])
        with self.assertRaises(InfluenceError):
            self._read(query)

    def test_virtual_table_rejected(self):
        connection = self._connection()
        connection.executescript(
            "CREATE VIRTUAL TABLE ft USING fts4(content);"
        )
        connection.commit()
        connection.close()
        query = simple_query(tables=[
            {"name": "ft", "id_column": "id", "unit_column": "unit",
             "columns": {"k": "text", "v": "integer"}}
        ])
        with self.assertRaises(InfluenceError):
            self._read(query)

    def test_empty_text_value_rejected(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)",
            [("i1", "U", "", 1)],
        )
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_integer_range_enforced(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)",
            [("i1", "U", "x", 10 ** 12 + 1)],
        )
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_storage_type_mismatch(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v TEXT)"
        )
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_duplicate_id_rejected(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL, unit TEXT, k TEXT, v INTEGER)",
            [("dup", "U", "x", 1), ("dup", "V", "y", 2)],
        )
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_null_unit_is_public(self):
        self._build(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)",
            [("i1", None, "x", 1)],
        )
        snapshot, _ = self._read(simple_query())
        self.assertIsNone(snapshot["t"][0]["_unit"])

    def test_row_limit(self):
        connection = self._connection()
        connection.execute(
            "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, k TEXT, v INTEGER)"
        )
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("i%05d" % i, "U", "k", i) for i in range(20_001)],
        )
        connection.commit()
        connection.close()
        with self.assertRaises(InfluenceError):
            self._read(simple_query())

    def test_readonly_does_not_create_database(self):
        missing = self.base / "never.db"
        with self.assertRaises(InfluenceError):
            open_snapshot_connection(missing)
        self.assertFalse(missing.exists())

    def test_duplicate_json_key_rejected(self):
        source = self.base / "dup.json"
        source.write_text(
            '{"tables":[],"nodes":[],"root":"g","root":"g2"}'
        )
        result = support.run_influence_cli(self.db, source, self.base / "out.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)


if __name__ == "__main__":
    unittest.main()
