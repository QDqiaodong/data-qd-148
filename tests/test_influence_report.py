"""Production-path guarantees: shared baseline, no per-unit rescans, snapshot
isolation under concurrent writes, deterministic reports, row-limit rejection
and CLI output safety."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import support
from privacy_budget.influence_api import audit_influence, finalize_report
from privacy_budget.influence_engine import InfluenceEngine
from privacy_budget.influence_model import (
    InfluenceError,
    parse_query,
    query_sha256,
)
from privacy_budget.influence_snapshot import (
    close_snapshot,
    open_snapshot_connection,
    read_snapshot,
)

SCHEMA = "CREATE TABLE t (id TEXT NOT NULL UNIQUE, unit TEXT, g TEXT, v INTEGER)"


def write_query(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def grouped_query():
    return {
        "tables": [
            {"name": "t", "id_column": "id", "unit_column": "unit",
             "columns": {"g": "text", "v": "integer"}},
        ],
        "nodes": [
            {"id": "s", "op": "scan", "table": "t", "alias": "a"},
            {"id": "g", "op": "aggregate", "input": "s", "keys": ["a.g"],
             "metrics": [
                 {"name": "n", "op": "count_rows", "column": None},
                 {"name": "sm", "op": "sum", "column": "a.v"},
             ]},
        ],
        "root": "g",
    }


class SharedComputation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "source.db"
        units = ["U%03d" % i for i in range(40)]
        connection = sqlite3.connect(str(self.db))
        connection.execute(SCHEMA)
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("id-%d" % i, unit, "g" + str(i % 5), i) for i, unit in enumerate(units)],
        )
        connection.commit()
        connection.close()
        self.query_path = self.base / "query.json"
        write_query(self.query_path, grouped_query())

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_per_unit_rescan_or_full_evaluation(self):
        parsed = parse_query(json.loads(self.query_path.read_text()))
        connection = open_snapshot_connection(self.db)
        try:
            snapshot, _ = read_snapshot(connection, parsed["tables"])
        finally:
            close_snapshot(connection)

        full_calls = []
        scan_calls = []

        class Instrumented(InfluenceEngine):
            def evaluate_node(self, node_id):
                full_calls.append(node_id)
                return super().evaluate_node(node_id)

            def _baseline_scan(self, node):
                scan_calls.append(node["id"])
                return super()._baseline_scan(node)

        engine = Instrumented(parsed, snapshot)
        engine.evaluate_baseline()
        # One full evaluation per node and one scan, regardless of units.
        self.assertEqual(sorted(full_calls), sorted(engine.order))
        self.assertEqual(scan_calls, ["s"])
        for unit in sorted(engine.unit_set):
            engine.propagate_unit(unit)
        self.assertEqual(len(full_calls), len(engine.order))
        self.assertEqual(scan_calls, ["s"])
        self.assertEqual(engine.delta_evaluations, len(engine.unit_set))
        self.assertEqual(engine.scan_count, 1)
        self.assertEqual(engine.full_evaluations, len(engine.order))

    def test_diagnostics_exposed_in_report(self):
        report = finalize_report(audit_influence(str(self.db), str(self.query_path)))
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["source_scans"], 1)
        self.assertEqual(diagnostics["full_node_evaluations"], 2)
        self.assertEqual(diagnostics["delta_node_evaluations"], 40)
        self.assertEqual(diagnostics["per_unit_full_evaluations"], 0)
        self.assertEqual(diagnostics["per_unit_source_scans"], 0)
        self.assertEqual(diagnostics["unit_count"], 40)


class DeterminismAndSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "source.db"
        connection = sqlite3.connect(str(self.db))
        connection.execute(SCHEMA)
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("i1", "U", "x", 1), ("i2", "V", "x", 2), ("i3", None, "y", 3)],
        )
        connection.commit()
        connection.close()
        self.query_path = self.base / "query.json"
        write_query(self.query_path, grouped_query())

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_stable_under_json_key_reordering(self):
        first = finalize_report(audit_influence(str(self.db), str(self.query_path)))
        payload = json.loads(self.query_path.read_text())
        reordered = {
            "root": payload["root"],
            "nodes": list(reversed(payload["nodes"])),
            "tables": list(reversed(payload["tables"])),
        }
        reordered_path = self.base / "reordered.json"
        write_query(reordered_path, reordered)
        second = finalize_report(audit_influence(str(self.db), str(reordered_path)))
        self.assertEqual(first["query_sha256"], second["query_sha256"])
        self.assertEqual(
            first["logical_snapshot_sha256"], second["logical_snapshot_sha256"]
        )
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        # Everything except the raw source-byte digest (which intentionally
        # binds the exact input bytes) is byte-identical.
        stable_fields = [
            key for key in first if key != "query_source_sha256"
        ]
        for key in stable_fields:
            self.assertEqual(first[key], second[key], key)

    def test_snapshot_ignores_concurrent_write_after_open(self):
        # WAL lets a second connection commit while the read transaction is open.
        setup = sqlite3.connect(str(self.db))
        setup.execute("PRAGMA journal_mode=WAL")
        setup.close()
        parsed = parse_query(json.loads(self.query_path.read_text()))
        connection = open_snapshot_connection(self.db)
        connection.execute("PRAGMA busy_timeout=60000")
        try:
            snapshot, first_digest = read_snapshot(connection, parsed["tables"])
            # A concurrent writer commits new rows and a new unit after the
            # read snapshot was established.
            writer = sqlite3.connect(str(self.db), timeout=60)
            writer.execute(
                "INSERT INTO t VALUES (?,?,?,?)", ("i9", "LATE", "z", 99)
            )
            writer.commit()
            writer.close()
            engine = InfluenceEngine(parsed, snapshot)
            engine.evaluate_baseline()
            self.assertNotIn("LATE", engine.unit_set)
            for unit in sorted(engine.unit_set):
                engine.propagate_unit(unit)
        finally:
            close_snapshot(connection)
        # Reopening sees the write, proving the held transaction isolated us.
        connection = open_snapshot_connection(self.db)
        try:
            later_snapshot, later_digest = read_snapshot(connection, parsed["tables"])
        finally:
            close_snapshot(connection)
        self.assertNotEqual(first_digest, later_digest)
        self.assertTrue(any(record["_id"] == "i9" for record in later_snapshot["t"]))

    def test_undeclared_columns_never_leak(self):
        connection = sqlite3.connect(str(self.db))
        connection.execute("ALTER TABLE t ADD COLUMN secret TEXT")
        connection.execute("UPDATE t SET secret = 'shh'")
        connection.commit()
        connection.close()
        # Physical schema now differs from the declaration: must be rejected.
        with self.assertRaises(InfluenceError):
            audit_influence(str(self.db), str(self.query_path))

    def test_node_row_limit_enforced(self):
        big_db = self.base / "big.db"
        connection = sqlite3.connect(str(big_db))
        connection.execute(SCHEMA)
        # Each source row becomes one aggregate group; force the root past
        # the 200_000 derived-row ceiling while staying under the per-table
        # 20_000 source-row contract is impossible for a plain scan, so here
        # we instead cross the per-table limit and expect the snapshot guard.
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("b%06d" % i, "U", "g" + str(i), i) for i in range(20_001)],
        )
        connection.commit()
        connection.close()
        with self.assertRaises(InfluenceError) as caught:
            audit_influence(str(big_db), str(self.query_path))
        self.assertIn("20000 row limit", str(caught.exception))

    def test_derived_node_row_limit_enforced(self):
        # A self join of two 450-row scans produces 202_500 join rows.
        join_db = self.base / "join.db"
        connection = sqlite3.connect(str(join_db))
        connection.execute(SCHEMA)
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("j%04d" % i, "U", "same", i) for i in range(450)],
        )
        connection.commit()
        connection.close()
        query = {
            "tables": [
                {"name": "t", "id_column": "id", "unit_column": "unit",
                 "columns": {"g": "text", "v": "integer"}},
            ],
            "nodes": [
                {"id": "s1", "op": "scan", "table": "t", "alias": "a"},
                {"id": "s2", "op": "scan", "table": "t", "alias": "b"},
                {"id": "j", "op": "inner", "left": "s1", "right": "s2",
                 "on": [{"left_column": "a.g", "right_column": "b.g"}]},
                {"id": "g", "op": "aggregate", "input": "j", "keys": [],
                 "metrics": [{"name": "n", "op": "count_rows", "column": None}]},
            ],
            "root": "g",
        }
        query_path = self.base / "join-query.json"
        write_query(query_path, query)
        with self.assertRaises(InfluenceError) as caught:
            audit_influence(str(join_db), str(query_path))
        self.assertIn("200000 row limit", str(caught.exception))


class CommandLineContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "source.db"
        connection = sqlite3.connect(str(self.db))
        connection.execute(SCHEMA)
        connection.executemany(
            "INSERT INTO t VALUES (?,?,?,?)",
            [("i1", "U", "x", 1), ("i2", "U", "x", 2)],
        )
        connection.commit()
        connection.close()
        self.query = self.base / "query.json"
        write_query(self.query, grouped_query())

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_success_and_failure_preserves_old_report(self):
        output = self.base / "report.json"
        result = support.run_influence_cli(self.db, self.query, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        first = output.read_bytes()
        report = json.loads(first)
        self.assertEqual(report["schema"], "privacy-budget-influence-report-v1")
        self.assertIn("report_sha256", report)
        # A second identical run produces byte-identical output.
        again = support.run_influence_cli(self.db, self.query, output)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(output.read_bytes(), first)
        # Invalid query fails with exit 2 and leaves the old report intact.
        bad = self.base / "bad.json"
        bad.write_text('{"tables":[],"nodes":[],"root":"g"}')
        failed = support.run_influence_cli(self.db, bad, output)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(output.read_bytes(), first)

    def test_inputs_must_differ_from_each_other(self):
        result = support.run_influence_cli(self.db, self.query, self.query)
        self.assertEqual(result.returncode, 2)
        self.assertIn("differ", result.stderr)
        db_result = support.run_influence_cli(self.db, self.query, self.db)
        self.assertEqual(db_result.returncode, 2)
        self.assertIn("differ", db_result.stderr)
        # The database and the query document must also be distinct addresses.
        same = support.run_influence_cli(self.db, self.db, self.base / "o.json")
        self.assertEqual(same.returncode, 2)
        self.assertIn("differ", same.stderr)

    def test_symlink_output_refused(self):
        link = self.base / "link.json"
        target = self.base / "target.json"
        target.write_text("prior")
        link.symlink_to(target)
        result = support.run_influence_cli(self.db, self.query, link)
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(target.read_text(), "prior")

    def test_legacy_entry_points_unchanged(self):
        # The v1 no-subcommand contract and the policy subcommand still parse.
        legacy = support.run_policy_cli(
            "policy", "--source", "fixtures/policy-pass.json",
            "--output", self.base / "policy.json",
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        v1 = subprocess_run(
            ["--source", "fixtures/input.json", "--output", str(self.base / "v1.json")]
        )
        self.assertEqual(v1.returncode, 0, v1.stderr)


def subprocess_run(args):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-m", "privacy_budget.cli", *map(str, args)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(support.SRC)},
        cwd=str(support.ROOT),
    )


if __name__ == "__main__":
    unittest.main()
