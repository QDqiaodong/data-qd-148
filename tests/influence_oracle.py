"""Independent SQLite reference oracle for influence audits (tests only).

The oracle re-implements the query plan by issuing generated SQL against a
*private copy* of the source database and, for every unit, physically deleting
that unit's rows, rerunning the query, and restoring the copy.  It is slow and
destructive by construction; it must never be used on the real source
database or by production code.

Every reference to a node is rendered as its own CTE with a fresh local name,
so a node shared by two join inputs -- including a self join scanning one
table twice -- gets independent materialisations and never a circular WITH
reference.  Aggregate answers are computed in Python from fetched rows so
SQLite integer ``SUM`` overflow never enters the comparison.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


class ReferenceOracle:
    def __init__(self, db_path, query):
        self.query = query
        self.tables = {table["name"]: table for table in query["tables"]}
        self.nodes = {node["id"]: node for node in query["nodes"]}
        self.tmpdir = tempfile.mkdtemp()
        self.copy_path = str(Path(self.tmpdir) / "oracle.db")
        shutil.copy(db_path, self.copy_path)
        self.connection = sqlite3.connect(self.copy_path)
        self.connection.row_factory = sqlite3.Row

    def close(self):
        self.connection.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # SQL generation with fresh CTE labels per node occurrence
    # ------------------------------------------------------------------

    def _fresh_name(self, cte):
        label = "m_%d" % len(cte)
        cte[label] = None
        return label

    def _render(self, node_id, cte, params):
        """Return the CTE label holding this node occurrence's rows."""
        node = self.nodes[node_id]
        label = self._fresh_name(cte)
        if node["op"] == "scan":
            table = self.tables[node["table"]]
            columns = [
                table["id_column"], table["unit_column"], *sorted(table["columns"])
            ]
            select = ", ".join(
                _quote(column) + " AS " + _quote(node["alias"] + "." + column)
                for column in columns
            )
            cte[label] = "SELECT " + select + " FROM " + _quote(node["table"])
            return label
        if node["op"] == "filter":
            input_label = self._render(node["input"], cte, params)
            condition = self._filter_condition(node, params)
            cte[label] = (
                "SELECT * FROM " + _quote(input_label) + " WHERE " + condition
            )
            return label
        if node["op"] in ("inner", "left", "anti"):
            left_label = self._render(node["left"], cte, params)
            right_label = self._render(node["right"], cte, params)
            left_columns = self.output_columns(node["left"])
            right_columns = self.output_columns(node["right"])
            left_ref, right_ref = _quote(left_label), _quote(right_label)
            conditions = " AND ".join(
                left_ref + "." + _quote(c["left_column"]) + " = "
                + right_ref + "." + _quote(c["right_column"])
                for c in node["on"]
            )
            if node["op"] == "anti":
                select_cols = ", ".join(
                    left_ref + "." + _quote(c) for c in left_columns
                )
                cte[label] = (
                    "SELECT " + select_cols + " FROM " + left_ref
                    + " WHERE NOT EXISTS (SELECT 1 FROM " + right_ref
                    + " WHERE " + conditions + ")"
                )
            else:
                join_kind = "LEFT JOIN" if node["op"] == "left" else "INNER JOIN"
                select_cols = ", ".join(
                    [left_ref + "." + _quote(c) for c in left_columns]
                    + [right_ref + "." + _quote(c) for c in right_columns]
                )
                cte[label] = (
                    "SELECT " + select_cols + " FROM " + left_ref + " "
                    + join_kind + " " + right_ref + " ON " + conditions
                )
            return label
        raise AssertionError("aggregate nodes are evaluated separately")

    @staticmethod
    def _filter_condition(node, params):
        column = _quote(node["column"])
        if node["predicate"] == "is_null":
            return column + " IS NULL"
        params.append(node["value"])
        return column + " IS ?"

    def output_columns(self, node_id):
        node = self.nodes[node_id]
        if node["op"] == "scan":
            table = self.tables[node["table"]]
            names = [
                table["id_column"], table["unit_column"], *sorted(table["columns"])
            ]
            return [node["alias"] + "." + name for name in names]
        if node["op"] == "filter":
            return self.output_columns(node["input"])
        if node["op"] == "anti":
            return self.output_columns(node["left"])
        if node["op"] in ("inner", "left"):
            return self.output_columns(node["left"]) + self.output_columns(node["right"])
        return list(node["keys"]) + [metric["name"] for metric in node["metrics"]]

    # ------------------------------------------------------------------
    # aggregate evaluation in Python from fetched rows
    # ------------------------------------------------------------------

    def evaluate_aggregate(self, node_id):
        node = self.nodes[node_id]
        cte = {}
        params = []
        input_label = self._render(node["input"], cte, params)
        cte_defs = [
            _quote(name) + " AS (" + body + ")" for name, body in cte.items()
        ]
        sql = "WITH " + ", ".join(cte_defs) + " SELECT * FROM " + _quote(input_label)
        rows = self.connection.execute(sql, params).fetchall()
        input_columns = self.output_columns(node["input"])
        key_positions = [input_columns.index(key) for key in node["keys"]]
        groups = {}
        for row in rows:
            key = tuple(row[position] for position in key_positions)
            groups.setdefault(key, []).append(row)
        result = {}
        if not groups and not node["keys"]:
            groups[()] = []
        for key, bucket_rows in groups.items():
            result[key] = {
                metric["name"]: self._metric(metric, bucket_rows, input_columns)
                for metric in node["metrics"]
            }
        return result

    @staticmethod
    def _metric(metric, rows, input_columns):
        position = (
            input_columns.index(metric["column"])
            if metric["column"] is not None
            else None
        )
        if metric["op"] == "count_rows":
            return len(rows)
        if metric["op"] == "count":
            return sum(1 for row in rows if row[position] is not None)
        if metric["op"] == "sum":
            return sum(row[position] for row in rows if row[position] is not None)
        return len({row[position] for row in rows if row[position] is not None})

    def baseline(self):
        return self.evaluate_aggregate(self.query["root"])

    def remove_unit(self, unit):
        for table in self.tables.values():
            self.connection.execute(
                "DELETE FROM " + _quote(table["name"])
                + " WHERE " + _quote(table["unit_column"]) + " = ?",
                (unit,),
            )
        self.connection.commit()

    def restore(self):
        self.connection.close()
        shutil.copy(self._original_path, self.copy_path)
        self.connection = sqlite3.connect(self.copy_path)
        self.connection.row_factory = sqlite3.Row

    def run_all_units(self, units):
        self._original_path = self.copy_path + ".orig"
        shutil.copy(self.copy_path, self._original_path)
        answers = {}
        for unit in sorted(units):
            self.remove_unit(unit)
            answers[unit] = self.evaluate_aggregate(self.query["root"])
            self.restore()
        Path(self._original_path).unlink(missing_ok=True)
        return answers
