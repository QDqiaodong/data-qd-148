"""Strict contract parsing and canonical identity for influence queries.

The query is a small, fixed relational plan:

* ``tables``  -- at most four declared source tables with typed columns,
* ``nodes``   -- at most twelve operators forming an acyclic DAG,
* ``root``    -- the single terminal aggregate node.

The parser accepts *only* the documented field sets (unknown fields are
rejected) and produces a canonical, order-insensitive representation whose
SHA-256 is part of every report.  No I/O happens here.
"""
from __future__ import annotations

import hashlib
import json

from .common import ident

MAX_TABLES = 4
MAX_NODES = 12
MAX_ROWS_PER_TABLE = 20_000
MAX_NODE_ROWS = 200_000
INTEGER_LIMIT = 10 ** 12
TEXT_LIMIT = 1024
ALLOWED_TYPES = ("integer", "text")
PREDICATES = ("eq", "is_null")
JOIN_OPS = ("inner", "left", "anti")
AGG_OPS = ("count_rows", "count", "count_distinct", "sum")


class InfluenceError(ValueError):
    """Raised for every contract, snapshot or execution failure."""


def _exact_fields(obj, fields, label):
    if not isinstance(obj, dict):
        raise InfluenceError("%s must be an object" % label)
    if set(obj) != set(fields):
        raise InfluenceError("%s must contain exactly fields: %s" % (label, ", ".join(fields)))


def _bounded_list(value, minimum, maximum, label):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise InfluenceError("%s must be an array of length %d..%d" % (label, minimum, maximum))
    return value


def _text_value(value, label):
    # bool is a subclass of int; reject it explicitly everywhere.
    if not isinstance(value, str) or isinstance(value, bool) or not value:
        raise InfluenceError("%s must be a non-empty text value" % label)
    if len(value) > TEXT_LIMIT or any(ord(ch) < 32 for ch in value):
        raise InfluenceError("%s exceeds text length/content limits" % label)
    return value


def _integer_value(value, label):
    if type(value) is not int or not -INTEGER_LIMIT <= value <= INTEGER_LIMIT:
        raise InfluenceError("%s must be an integer within +-%d" % (label, INTEGER_LIMIT))
    return value


def _ident(value):
    try:
        return ident(value)
    except ValueError as error:
        raise InfluenceError(str(error))


def _ident(value):
    try:
        return ident(value)
    except ValueError as error:
        raise InfluenceError(str(error))


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def parse_tables(raw_tables):
    _bounded_list(raw_tables, 1, MAX_TABLES, "tables")
    tables = {}
    for raw in raw_tables:
        _exact_fields(raw, ("name", "id_column", "unit_column", "columns"), "table entry")
        name = _ident(raw["name"])
        if name in tables:
            raise InfluenceError("duplicate table name: " + name)
        id_column = _ident(raw["id_column"])
        unit_column = _ident(raw["unit_column"])
        if id_column == unit_column:
            raise InfluenceError("id_column and unit_column must differ in table " + name)
        columns = raw["columns"]
        if not isinstance(columns, dict) or not columns:
            raise InfluenceError("table %s must declare at least one column" % name)
        declared = {}
        for column, kind in columns.items():
            column = _ident(column)
            if column in (id_column, unit_column):
                raise InfluenceError(
                    "declared column %s collides with id/unit column in table %s" % (column, name)
                )
            if kind not in ALLOWED_TYPES:
                raise InfluenceError("column type must be integer or text: " + column)
            declared[column] = kind
        if len(set(declared)) != len(columns):
            raise InfluenceError("duplicate column declaration in table " + name)
        tables[name] = {
            "name": name,
            "id_column": id_column,
            "unit_column": unit_column,
            "columns": dict(sorted(declared.items())),
        }
    return tables


def canonical_tables(tables):
    return [
        {
            "name": table["name"],
            "id_column": table["id_column"],
            "unit_column": table["unit_column"],
            "columns": {column: table["columns"][column] for column in sorted(table["columns"])},
        }
        for table in sorted(tables.values(), key=lambda item: item["name"])
    ]


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def _unique_name_list(value, label, minimum=1):
    _bounded_list(value, minimum, MAX_NODES, label)
    seen = set()
    for item in value:
        name = _ident(item)
        if name in seen:
            raise InfluenceError("duplicate name in " + label)
        seen.add(name)
    return list(value)


def parse_nodes(raw_nodes, tables):
    _bounded_list(raw_nodes, 1, MAX_NODES, "nodes")
    nodes = {}
    seen_ids = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict) or "op" not in raw:
            raise InfluenceError("each node must be an object with an op")
        node_id = _ident(raw["id"]) if isinstance(raw.get("id"), str) else None
        if node_id is None:
            raise InfluenceError("node id must be a non-blank identifier")
        if node_id in seen_ids:
            raise InfluenceError("duplicate node id: " + node_id)
        seen_ids.add(node_id)
        op = raw["op"]
        if op == "scan":
            _exact_fields(raw, ("id", "op", "table", "alias"), "scan node")
            table = _ident(raw["table"])
            if table not in tables:
                raise InfluenceError("scan references undeclared table: " + table)
            alias = _ident(raw["alias"])
            nodes[node_id] = {"id": node_id, "op": "scan", "table": table, "alias": alias}
        elif op == "filter":
            _exact_fields(raw, ("id", "op", "input", "column", "predicate", "value"), "filter node")
            node_id = _ident(raw["id"])
            predicate = raw["predicate"]
            if predicate not in PREDICATES:
                raise InfluenceError("filter predicate must be eq or is_null")
            nodes[node_id] = {
                "id": node_id,
                "op": "filter",
                "input": _ident(raw["input"]),
                "column": _ident(raw["column"]),
                "predicate": predicate,
                "value": raw["value"],
            }
        elif op in JOIN_OPS:
            _exact_fields(raw, ("id", "op", "left", "right", "on"), op + " join node")
            on = _bounded_list(raw["on"], 1, MAX_NODES, "join on list")
            conditions = []
            for condition in on:
                _exact_fields(condition, ("left_column", "right_column"), "join condition")
                conditions.append(
                    {
                        "left_column": _ident(condition["left_column"]),
                        "right_column": _ident(condition["right_column"]),
                    }
                )
            nodes[_ident(raw["id"])] = {
                "id": _ident(raw["id"]),
                "op": op,
                "left": _ident(raw["left"]),
                "right": _ident(raw["right"]),
                "on": conditions,
            }
        elif op == "aggregate":
            _exact_fields(raw, ("id", "op", "input", "keys", "metrics"), "aggregate node")
            node_id = _ident(raw["id"])
            keys = _unique_name_list(raw["keys"], "aggregate keys", minimum=0) if raw["keys"] else []
            metrics_raw = _bounded_list(raw["metrics"], 1, MAX_NODES, "aggregate metrics")
            metrics = []
            seen_metric = set()
            for metric in metrics_raw:
                _exact_fields(metric, ("name", "op", "column"), "metric")
                name = _ident(metric["name"])
                if name in seen_metric:
                    raise InfluenceError("duplicate metric output name: " + name)
                seen_metric.add(name)
                metric_op = metric["op"]
                if metric_op not in AGG_OPS:
                    raise InfluenceError("unsupported aggregate op: " + str(metric_op))
                column = metric["column"]
                if column is not None:
                    column = _ident(column)
                metrics.append({"name": name, "op": metric_op, "column": column})
            nodes[node_id] = {
                "id": node_id,
                "op": "aggregate",
                "input": _ident(raw["input"]),
                "keys": list(keys),
                "metrics": metrics,
            }
        else:
            raise InfluenceError("unknown node op: " + str(op))
    return nodes


def canonical_nodes(nodes):
    canonical = []
    for node in sorted(nodes.values(), key=lambda item: item["id"]):
        if node["op"] == "scan":
            canonical.append(
                {"id": node["id"], "op": "scan", "table": node["table"], "alias": node["alias"]}
            )
        elif node["op"] == "filter":
            canonical.append(
                {
                    "id": node["id"],
                    "op": "filter",
                    "input": node["input"],
                    "column": node["column"],
                    "predicate": node["predicate"],
                    "value": node["value"],
                }
            )
        elif node["op"] in JOIN_OPS:
            canonical.append(
                {
                    "id": node["id"],
                    "op": node["op"],
                    "left": node["left"],
                    "right": node["right"],
                    "on": sorted(
                        node["on"],
                        key=lambda c: (c["left_column"], c["right_column"]),
                    ),
                }
            )
        else:
            canonical.append(
                {
                    "id": node["id"],
                    "op": "aggregate",
                    "input": node["input"],
                    "keys": list(node["keys"]),
                    "metrics": [
                        {"name": m["name"], "op": m["op"], "column": m["column"]}
                        for m in sorted(node["metrics"], key=lambda m: m["name"])
                    ],
                }
            )
    return canonical


# ---------------------------------------------------------------------------
# semantic validation (schemas, DAG, unambiguous references)
# ---------------------------------------------------------------------------


def validate_dag(tables, nodes, root):
    if root not in nodes:
        raise InfluenceError("root must reference a declared node id")
    if nodes[root]["op"] != "aggregate":
        raise InfluenceError("root must be the terminal aggregate node")
    aggregate_nodes = [node_id for node_id, node in nodes.items() if node["op"] == "aggregate"]
    if len(aggregate_nodes) != 1 or aggregate_nodes[0] != root:
        raise InfluenceError("only the root may aggregate")

    # Aliases and schemas, resolved in topological order.
    aliases = {}
    schemas = {}
    visited = {}

    def inputs_of(node):
        if node["op"] == "scan":
            return ()
        if node["op"] == "filter":
            return (node["input"],)
        if node["op"] in JOIN_OPS:
            return (node["left"], node["right"])
        return (node["input"],)

    def visit(node_id):
        if node_id not in nodes:
            raise InfluenceError("unknown node reference: " + node_id)
        state = visited.get(node_id)
        if state == "done":
            return
        if state == "busy":
            raise InfluenceError("node graph must be acyclic")
        visited[node_id] = "busy"
        node = nodes[node_id]
        for child in inputs_of(node):
            visit(child)
        if node["op"] == "scan":
            if node["alias"] in aliases:
                raise InfluenceError("duplicate scan alias: " + node["alias"])
            aliases[node["alias"]] = node["table"]
            table = tables[node["table"]]
            schema = {node["alias"] + "." + col: col for col in (
                [table["id_column"], table["unit_column"]] + sorted(table["columns"])
            )}
            schemas[node_id] = schema
        elif node["op"] == "filter":
            schema = dict(schemas[node["input"]])
            if node["column"] not in schema:
                raise InfluenceError("filter column is not in scope: " + node["column"])
            kind = _column_type(tables, nodes, schemas, node["input"], node["column"])
            value = node["value"]
            if node["predicate"] == "eq":
                if value is None:
                    raise InfluenceError("eq filter value must not be null")
                _check_literal_type(kind, value, node["column"])
            elif value is not None:
                raise InfluenceError("is_null filter value must be null")
            schemas[node_id] = schema
        elif node["op"] in JOIN_OPS:
            left_schema = schemas[node["left"]]
            right_schema = schemas[node["right"]]
            overlap = set(left_schema) & set(right_schema)
            if overlap:
                raise InfluenceError(
                    "join input columns must not overlap: " + ", ".join(sorted(overlap))
                )
            for condition in node["on"]:
                if condition["left_column"] not in left_schema:
                    raise InfluenceError("join left column not in scope: " + condition["left_column"])
                if condition["right_column"] not in right_schema:
                    raise InfluenceError("join right column not in scope: " + condition["right_column"])
                left_kind = _column_type(tables, nodes, schemas, node["left"], condition["left_column"])
                right_kind = _column_type(
                    tables, nodes, schemas, node["right"], condition["right_column"]
                )
                if left_kind != right_kind:
                    raise InfluenceError("join columns must have matching declared types")
            schema = dict(left_schema)
            if node["op"] != "anti":
                schema.update(right_schema)
            schemas[node_id] = schema
        else:  # aggregate
            input_schema = schemas[node["input"]]
            for key in node["keys"]:
                if key not in input_schema:
                    raise InfluenceError("aggregate key not in scope: " + key)
            for metric in node["metrics"]:
                if metric["op"] == "count_rows":
                    if metric["column"] is not None:
                        raise InfluenceError("count_rows metric must have null column")
                else:
                    if metric["column"] is None:
                        raise InfluenceError(metric["op"] + " metric must name a column")
                    if metric["column"] not in input_schema:
                        raise InfluenceError("metric column not in scope: " + metric["column"])
                    if metric["op"] == "sum":
                        kind = _column_type(
                            tables, nodes, schemas, node["input"], metric["column"]
                        )
                        if kind != "integer":
                            raise InfluenceError("sum supports integer columns only")
            if set(node["keys"]) & {m["name"] for m in node["metrics"]}:
                raise InfluenceError("metric output names must not shadow group keys")
            schemas[node_id] = {key: key for key in node["keys"]}
        visited[node_id] = "done"

    visit(root)
    if set(visited) != set(nodes):
        raise InfluenceError("every node must be reachable from the root")

    # Every declared table must be scanned exactly once per... at least scanned.
    scanned = {nodes[nid]["table"] for nid, node in nodes.items() if node["op"] == "scan"}
    missing = set(tables) - scanned
    if missing:
        raise InfluenceError("declared table never scanned: " + ", ".join(sorted(missing)))
    scan_count = {}
    for node in nodes.values():
        if node["op"] == "scan":
            scan_count[node["table"]] = scan_count.get(node["table"], 0) + 1
    return schemas, scan_count


def _table_of_column(nodes, schemas, node_id, qualified):
    alias = qualified.split(".", 1)[0]
    # Walk back to a scan carrying this alias.
    found = []

    def walk(current):
        node = nodes[current]
        if node["op"] == "scan":
            if node["alias"] == alias:
                found.append(node["table"])
            return
        if node["op"] == "filter":
            walk(node["input"])
        elif node["op"] in JOIN_OPS:
            walk(node["left"])
            if node["op"] != "anti":
                walk(node["right"])
        else:
            walk(node["input"])

    walk(node_id)
    if len(found) != 1:
        raise InfluenceError("column reference is ambiguous: " + qualified)
    return found[0]


def _column_type(tables, nodes, schemas, node_id, qualified):
    schema = schemas[node_id]
    if qualified not in schema:
        raise InfluenceError("column not in scope: " + qualified)
    source_column = schema[qualified]
    table_name = _table_of_column(nodes, schemas, node_id, qualified)
    table = tables[table_name]
    if source_column == table["id_column"]:
        return "text"
    if source_column == table["unit_column"]:
        return "text"
    return table["columns"][source_column]


def _check_literal_type(kind, value, label):
    if kind == "integer":
        _integer_value(value, "literal for " + label)
    else:
        _text_value(value, "literal for " + label)


# ---------------------------------------------------------------------------
# top-level query
# ---------------------------------------------------------------------------


def parse_query(data):
    _exact_fields(data, ("tables", "nodes", "root"), "query")
    tables = parse_tables(data["tables"])
    nodes = parse_nodes(data["nodes"], tables)
    root = _ident(data["root"])
    validate_dag(tables, nodes, root)
    return {"tables": tables, "nodes": nodes, "root": root}


def canonical_query(query):
    return {
        "tables": canonical_tables(query["tables"]),
        "nodes": canonical_nodes(query["nodes"]),
        "root": query["root"],
    }


def query_sha256(query):
    canonical = canonical_query(query)
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()
