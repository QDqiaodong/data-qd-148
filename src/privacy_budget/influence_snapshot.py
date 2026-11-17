"""Read-only SQLite microdata snapshot for influence auditing.

The auditor never executes user supplied SQL.  Every referenced table is read
exactly once, through generated ``SELECT`` statements that name only declared
columns, inside one explicit deferred read transaction.  That transaction is
held open for the whole audit so the schema, every source row, the unit list
and the final results all share the same snapshot -- including committed WAL
frames.

The ``logical_snapshot_sha256`` binds the *declared* schema together with the
full selected column values, ordered by table name and row id.  It is a hash
of logical contents, not of the main database file, so a WAL-only database is
covered correctly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .influence_model import (
    MAX_ROWS_PER_TABLE,
    InfluenceError,
    INTEGER_LIMIT,
    TEXT_LIMIT,
)

# Columns that every declared table must physically expose.
_PHYSICAL_KIND = {"id_column": "TEXT", "unit_column": "TEXT"}


def open_snapshot_connection(db_path):
    """Open an existing database strictly read-only; never create files."""
    path = Path(db_path)
    if not path.exists():
        raise InfluenceError("source database does not exist")
    if path.is_symlink():
        raise InfluenceError("source database cannot be a symlink")
    if path.resolve() == Path(".").resolve():
        raise InfluenceError("source database path is invalid")
    connection = sqlite3.connect(
        "file:" + str(path.resolve()) + "?mode=ro",
        uri=True,
        timeout=60,
        isolation_level=None,
    )
    # Refuse to run any attached virtual table machinery / extensions.
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.row_factory = sqlite3.Row
    return connection


def _physical_columns(connection, table_name):
    # PRAGMA table_info inside a read-only connection only reads schema state.
    rows = connection.execute('PRAGMA table_info("' + table_name + '")').fetchall()
    if not rows:
        raise InfluenceError("source table does not exist: " + table_name)
    return {row["name"]: (row["type"], row["notnull"], row["pk"]) for row in rows}


def _table_kind(connection, table_name):
    row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table_name,),
    ).fetchone()
    if row is None:
        raise InfluenceError("source table does not exist: " + table_name)
    if row["type"] != "table":
        raise InfluenceError("only ordinary SQLite tables are allowed: " + table_name)
    sql = (row["sql"] or "").upper()
    if "VIRTUAL" in sql or "USING FTS" in sql or "CREATE VIRTUAL TABLE" in sql:
        raise InfluenceError("virtual tables are not allowed: " + table_name)
    # Shadow tables backing a virtual module have a NULL CREATE statement and
    # a name of the form ``module_name_suffix``.  Ordinary declared tables
    # always carry a CREATE statement.
    if row["sql"] is None:
        raise InfluenceError("virtual/shadow tables are not allowed: " + table_name)


def validate_physical_schema(connection, tables):
    for table in tables.values():
        _table_kind(connection, table["name"])
        physical = _physical_columns(connection, table["name"])
        expected = {table["id_column"], table["unit_column"], *table["columns"]}
        if set(physical) != expected:
            raise InfluenceError(
                "physical columns of table %s must match its declaration exactly" % table["name"]
            )
        for column, kind in table["columns"].items():
            declared_type = physical[column][0].upper()
            wanted = "INTEGER" if kind == "integer" else "TEXT"
            if declared_type != wanted:
                raise InfluenceError(
                    "column %s.%s has storage type %s, expected %s"
                    % (table["name"], column, declared_type or "(none)", wanted)
                )
        for special in (table["id_column"], table["unit_column"]):
            if physical[special][0].upper() != "TEXT":
                raise InfluenceError(
                    "column %s.%s must be declared TEXT" % (table["name"], special)
                )


def _check_stored_value(kind, value, label):
    if value is None:
        return
    if kind == "integer":
        if type(value) is not int or isinstance(value, bool):
            raise InfluenceError(label + " must be an integer value")
        if not -INTEGER_LIMIT <= value <= INTEGER_LIMIT:
            raise InfluenceError(label + " is outside the allowed integer range")
    else:
        if not isinstance(value, str):
            raise InfluenceError(label + " must be a text value")
        if not value:
            raise InfluenceError(label + " must be a non-empty text value")
        if len(value) > TEXT_LIMIT or any(ord(ch) < 32 for ch in value):
            raise InfluenceError(label + " exceeds text length/content limits")


def read_snapshot(connection, tables):
    """Read every declared table once inside a single read transaction.

    Returns a mapping ``table_name -> list[dict]`` where each dict carries the
    row id, the unit and the declared data columns.  The connection is left
    with the transaction open; callers own ``END``/``ROLLBACK`` via
    :func:`close_snapshot`.
    """
    connection.execute("BEGIN")
    try:
        validate_physical_schema(connection, tables)
        snapshot = {}
        digest = hashlib.sha256()
        ordered_tables = sorted(tables.values(), key=lambda item: item["name"])
        for table in ordered_tables:
            columns = [table["id_column"], table["unit_column"], *sorted(table["columns"])]
            quoted = ", ".join('"' + column + '"' for column in columns)
            rows = connection.execute(
                'SELECT ' + quoted + ' FROM "' + table["name"] + '"'
            ).fetchall()
            if len(rows) > MAX_ROWS_PER_TABLE:
                raise InfluenceError(
                    "table %s exceeds the %d row limit" % (table["name"], MAX_ROWS_PER_TABLE)
                )
            records = []
            seen_ids = set()
            digest.update(("table:" + table["name"]).encode("utf-8"))
            digest.update(
                json.dumps(
                    {
                        "name": table["name"],
                        "id_column": table["id_column"],
                        "unit_column": table["unit_column"],
                        "columns": table["columns"],
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for row in rows:
                row_id = row[table["id_column"]]
                if not isinstance(row_id, str) or not row_id:
                    raise InfluenceError(
                        "id column %s.%s must be a non-empty text value"
                        % (table["name"], table["id_column"])
                    )
                if row_id in seen_ids:
                    raise InfluenceError("duplicate id in table %s: %s" % (table["name"], row_id))
                seen_ids.add(row_id)
                unit = row[table["unit_column"]]
                if unit is not None and (not isinstance(unit, str) or not unit):
                    raise InfluenceError(
                        "unit column %s.%s must be non-empty text or NULL"
                        % (table["name"], table["unit_column"])
                    )
                record = {"_id": row_id, "_unit": unit}
                for column, kind in table["columns"].items():
                    value = row[column]
                    _check_stored_value(
                        kind, value, "value %s.%s" % (table["name"], column)
                    )
                    record[column] = value
                records.append(record)
            # The logical hash binds rows ordered by table name then row id,
            # independent of physical storage / fetch order.
            for record in sorted(records, key=lambda item: item["_id"]):
                digest.update(
                    json.dumps(
                        [record["_id"], record["_unit"]]
                        + [record[column] for column in sorted(table["columns"])],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            snapshot[table["name"]] = sorted(records, key=lambda item: item["_id"])
        return snapshot, digest.hexdigest()
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def close_snapshot(connection):
    """End the read transaction without writing anything."""
    try:
        if connection.in_transaction:
            connection.execute("END")
    finally:
        connection.close()
