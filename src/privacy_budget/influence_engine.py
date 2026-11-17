"""Relational execution and per-unit counterfactual influence propagation.

The engine evaluates the query once into *bag relations* (SQL bag semantics:
duplicate values survive through explicit multiplicities) and then answers
each counterfactual "remove every source row of unit u" question by localized
delta propagation over the shared, cached baseline state.  It never rescans
the source database, never re-evaluates a full node for a unit, and never
reruns the DAG from a scan.

Every derived row additionally carries

* ``idmult`` -- a mapping ``(table, row_id) -> multiplicity`` recording which
  physical source rows compose it (a source row used twice in a self-join
  contributes multiplicity two),
* ``units``  -- a mapping ``unit -> multiplicity`` of the same composition;
  public rows (NULL unit) simply do not appear.

The per-unit state is a signed bag of :class:`DeltaRow`.  Bag joins on values
preserve multiplicity automatically; per-row match support counters built in
the baseline pass decide when left-join null extensions or anti rows newly
appear, and distinct metrics are diffed from *remaining value support*, not
from derived row counts.
"""
from __future__ import annotations

import json

from .influence_model import MAX_NODE_ROWS, InfluenceError


class RowState:
    """One distinct value tuple in a baseline relation bag."""

    __slots__ = ("values", "mult", "idmult", "units")

    def __init__(self, values, mult, idmult, units):
        self.values = values
        self.mult = mult
        self.idmult = idmult
        self.units = units


class Relation:
    """A bag relation: value tuple -> RowState, with a fixed column order."""

    __slots__ = ("columns", "rows")

    def __init__(self, columns, rows=None):
        self.columns = tuple(columns)
        self.rows = rows if rows is not None else {}

    def size(self):
        return sum(state.mult for state in self.rows.values())


class DeltaRow:
    """One distinct value tuple in a per-unit signed delta bag."""

    __slots__ = ("values", "delta", "idmult", "units")

    def __init__(self, values, delta, idmult=None, units=None):
        self.values = values
        self.delta = delta
        self.idmult = idmult if idmult is not None else {}
        self.units = units if units is not None else {}


class DeltaRelation:
    __slots__ = ("columns", "rows")

    def __init__(self, columns, rows=None):
        self.columns = tuple(columns)
        self.rows = rows if rows is not None else {}


def add_dict(target, source, scale=1):
    for key, amount in source.items():
        target[key] = target.get(key, 0) + amount * scale
    return target


def merge_identity(left_idmult, right_idmult, left_mult, right_mult):
    """Identity multiset of a join output: cartesian composition of both sides."""
    result = {key: amount * right_mult for key, amount in left_idmult.items()}
    for key, amount in right_idmult.items():
        result[key] = result.get(key, 0) + amount * left_mult
    return result


def join_key(values, columns, on_columns):
    return tuple(values[columns.index(column)] for column in on_columns)


def group_sort_key(key):
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class InfluenceEngine:
    """Baseline evaluation once, then localized per-unit delta propagation."""

    def __init__(self, query, snapshot):
        self.query = query
        self.tables = query["tables"]
        self.nodes = query["nodes"]
        self.root = query["root"]
        self.snapshot = snapshot
        # Bypass-verifiable diagnostics.  Source scans and full node
        # evaluations happen only while building the baseline; per-unit work
        # increments ``delta_evaluations`` instead.
        self.scan_count = 0
        self.full_evaluations = 0
        self.delta_evaluations = 0
        self.relations = {}
        self.indexes = {}
        self.order = self._topological_order()
        self.unit_set = self._collect_units()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _topological_order(self):
        order = []
        seen = set()

        def visit(node_id):
            if node_id in seen:
                return
            node = self.nodes[node_id]
            if node["op"] == "filter":
                visit(node["input"])
            elif node["op"] in ("inner", "left", "anti"):
                visit(node["left"])
                visit(node["right"])
            elif node["op"] == "aggregate":
                visit(node["input"])
            seen.add(node_id)
            order.append(node_id)

        visit(self.root)
        return order

    def _collect_units(self):
        units = set()
        for records in self.snapshot.values():
            for record in records:
                if record["_unit"] is not None:
                    units.add(record["_unit"])
        return units

    def _enforce_limit(self, relation, node_id, count=None):
        size = relation.size() if count is None else count
        if size > MAX_NODE_ROWS:
            raise InfluenceError(
                "node %s produces %d rows, above the %d row limit"
                % (node_id, size, MAX_NODE_ROWS)
            )

    # ------------------------------------------------------------------
    # baseline evaluation (full, exactly once per node)
    # ------------------------------------------------------------------

    def evaluate_baseline(self):
        for node_id in self.order:
            self.relations[node_id] = self.evaluate_node(node_id)
        return self.relations[self.root]

    def evaluate_node(self, node_id):
        self.full_evaluations += 1
        node = self.nodes[node_id]
        return getattr(self, "_baseline_" + node["op"])(node)

    def _scan_columns(self, node):
        table = self.tables[node["table"]]
        return [node["alias"] + "." + col for col in (
            [table["id_column"], table["unit_column"]] + sorted(table["columns"])
        )]

    def _baseline_scan(self, node):
        # The only point where source microdata enters the engine.
        self.scan_count += 1
        table = self.tables[node["table"]]
        relation = Relation(self._scan_columns(node))
        for record in self.snapshot[node["table"]]:
            values = (record["_id"], record["_unit"]) + tuple(
                record[column] for column in sorted(table["columns"])
            )
            identity = {(node["table"], record["_id"]): 1}
            units = {} if record["_unit"] is None else {record["_unit"]: 1}
            relation.rows[values] = RowState(values, 1, identity, units)
        self._enforce_limit(relation, node["id"])
        return relation

    @staticmethod
    def _filter_accepts(value, node):
        if node["predicate"] == "is_null":
            return value is None
        return value is not None and value == node["value"]

    def _baseline_filter(self, node):
        source = self.relations[node["input"]]
        position = source.columns.index(node["column"])
        relation = Relation(source.columns)
        for state in source.rows.values():
            if self._filter_accepts(state.values[position], node):
                relation.rows[state.values] = RowState(
                    state.values, state.mult, dict(state.idmult), dict(state.units)
                )
        self._enforce_limit(relation, node["id"])
        return relation

    @staticmethod
    def _null_in_key(values, columns, on_columns):
        return any(values[columns.index(column)] is None for column in on_columns)

    def _build_hash_index(self, relation, on_columns):
        index = {}
        positions = [relation.columns.index(column) for column in on_columns]
        for state in relation.rows.values():
            key = tuple(state.values[index] for index in positions)
            index.setdefault(key, []).append(state)
        return index

    def _baseline_join(self, node, kind):
        left = self.relations[node["left"]]
        right = self.relations[node["right"]]
        on_left = [condition["left_column"] for condition in node["on"]]
        on_right = [condition["right_column"] for condition in node["on"]]
        right_index = self._build_hash_index(right, on_right)
        # Cached shared state reused by every unit's delta propagation.
        self.indexes[node["id"]] = {
            "right_index": right_index,
            "on_left": on_left,
            "on_right": on_right,
            "left_index": self._build_hash_index(left, on_left),
        }
        columns = left.columns if kind == "anti" else left.columns + right.columns
        relation = Relation(columns)
        null_extension = tuple(None for _ in right.columns)
        for left_state in left.rows.values():
            key = join_key(left_state.values, left.columns, on_left)
            if self._null_in_key(left_state.values, left.columns, on_left):
                matches = []
            else:
                matches = [
                    state
                    for state in right_index.get(key, ())
                    if not self._null_in_key(state.values, right.columns, on_right)
                ]
            if kind == "anti":
                if not matches:
                    relation.rows[left_state.values] = RowState(
                        left_state.values, left_state.mult,
                        dict(left_state.idmult), dict(left_state.units),
                    )
                continue
            if not matches:
                if kind == "left":
                    values = left_state.values + null_extension
                    relation.rows[values] = RowState(
                        values, left_state.mult,
                        dict(left_state.idmult), dict(left_state.units),
                    )
                continue
            for right_state in matches:
                mult = left_state.mult * right_state.mult
                values = left_state.values + right_state.values
                idmult = merge_identity(
                    left_state.idmult, right_state.idmult,
                    left_state.mult, right_state.mult,
                )
                units = self._rescale_units(
                    left_state.units, right_state.units,
                    left_state.mult, right_state.mult,
                )
                existing = relation.rows.get(values)
                if existing is None:
                    relation.rows[values] = RowState(values, mult, idmult, units)
                else:
                    existing.mult += mult
                    add_dict(existing.idmult, idmult)
                    add_dict(existing.units, units)
        self._enforce_limit(relation, node["id"])
        return relation

    def _baseline_inner(self, node):
        return self._baseline_join(node, "inner")

    def _baseline_left(self, node):
        return self._baseline_join(node, "left")

    def _baseline_anti(self, node):
        return self._baseline_join(node, "anti")

    @staticmethod
    def _rescale_units(left_units, right_units, left_mult, right_mult):
        units = {unit: amount * right_mult for unit, amount in left_units.items()}
        for unit, amount in right_units.items():
            units[unit] = units.get(unit, 0) + amount * left_mult
        return units

    def _baseline_aggregate(self, node):
        source = self.relations[node["input"]]
        key_positions = [source.columns.index(column) for column in node["keys"]]
        metric_specs = node["metrics"]
        columns = list(node["keys"]) + [metric["name"] for metric in metric_specs]
        groups = self._group_baseline(source, node, key_positions, metric_specs)
        relation = Relation(columns)
        key_columns = list(node["keys"])
        for group_key in sorted(groups, key=group_sort_key):
            bucket = groups[group_key]
            values = tuple(list(group_key) + [
                self._metric_value(metric, bucket, source) for metric in metric_specs
            ])
            mult = 0
            idmult = {}
            units = {}
            for state, copies in bucket["rows"]:
                mult += state.mult * copies
                add_dict(idmult, state.idmult, copies)
                add_dict(units, state.units, copies)
            relation.rows[values] = RowState(values, mult, idmult, units)
        if not groups and not node["keys"]:
            values = (tuple(None for _ in key_positions)
                      + tuple(0 for _ in metric_specs))
            relation.rows[values] = RowState(values, 1, {}, {})
        self._enforce_limit(relation, node["id"])
        return relation

    def _group_baseline(self, source, node, key_positions, metric_specs):
        groups = {}
        distinct = {
            metric["name"]: source.columns.index(metric["column"])
            for metric in metric_specs
            if metric["op"] == "count_distinct"
        }
        for state in source.rows.values():
            group_key = tuple(state.values[index] for index in key_positions)
            bucket = groups.setdefault(
                group_key, {"rows": [], "distinct": {name: {} for name in distinct}}
            )
            bucket["rows"].append((state, 1))
            for name, position in distinct.items():
                value = state.values[position]
                if value is None:
                    continue
                # Support is the value's bag multiplicity: the number of
                # derived row copies carrying it (self-join copies included).
                support = bucket["distinct"][name].setdefault(value, 0)
                bucket["distinct"][name][value] = support + state.mult
        return groups

    def _metric_value(self, metric, bucket, source):
        states = bucket["rows"]
        if metric["op"] == "count_rows":
            return sum(state.mult * copies for state, copies in states)
        position = source.columns.index(metric["column"])
        if metric["op"] == "count":
            return sum(
                state.mult * copies
                for state, copies in states
                if state.values[position] is not None
            )
        if metric["op"] == "sum":
            return sum(
                state.mult * copies * state.values[position]
                for state, copies in states
                if state.values[position] is not None
            )
        return len(bucket["distinct"][metric["name"]])

    # ------------------------------------------------------------------
    # per-unit delta propagation
    # ------------------------------------------------------------------

    def propagate_unit(self, unit):
        """Signed delta relation at every node for one removed unit."""
        self.delta_evaluations += 1
        deltas = {}
        for node_id in self.order:
            node = self.nodes[node_id]
            if node["op"] == "scan":
                deltas[node_id] = self._delta_scan(node, unit)
            elif node["op"] == "filter":
                deltas[node_id] = self._delta_filter(node, deltas[node["input"]])
            elif node["op"] == "inner":
                deltas[node_id] = self._delta_inner(node, deltas)
            elif node["op"] == "left":
                deltas[node_id] = self._delta_left(node, unit, deltas)
            elif node["op"] == "anti":
                deltas[node_id] = self._delta_anti(node, unit, deltas)
            else:
                deltas[node_id] = self._delta_aggregate(node, unit, deltas[node["input"]])
            self._check_delta_limit(node_id, deltas[node_id])
        return deltas

    def _check_delta_limit(self, node_id, delta):
        total = sum(abs(row.delta) for row in delta.rows.values())
        if total > MAX_NODE_ROWS:
            raise InfluenceError(
                "node %s counterfactual exceeds the %d row limit" % (node_id, MAX_NODE_ROWS)
            )

    def _delta_scan(self, node, unit):
        relation = self.relations[node["id"]]
        rows = {}
        for state in relation.rows.values():
            amount = state.units.get(unit, 0)
            if amount:
                rows[state.values] = DeltaRow(
                    state.values, -amount,
                    {key: -value for key, value in state.idmult.items()},
                    {unit: -amount},
                )
        return DeltaRelation(relation.columns, rows)

    def _delta_filter(self, node, source_delta):
        source = self.relations[node["input"]]
        position = source.columns.index(node["column"])
        rows = {}
        for delta in source_delta.rows.values():
            if self._filter_accepts(delta.values[position], node):
                rows[delta.values] = DeltaRow(
                    delta.values, delta.delta, dict(delta.idmult), dict(delta.units)
                )
        return DeltaRelation(source.columns, rows)

    def _right_matches(self, node, key):
        info = self.indexes[node["id"]]
        right = self.relations[node["right"]]
        if any(part is None for part in key):
            return []
        return [
            state
            for state in info["right_index"].get(key, ())
            if not self._null_in_key(state.values, right.columns, info["on_right"])
        ]

    def _left_matches(self, node, key):
        info = self.indexes[node["id"]]
        left = self.relations[node["left"]]
        if any(part is None for part in key):
            return []
        return [
            state
            for state in info["left_index"].get(key, ())
            if not self._null_in_key(state.values, left.columns, info["on_left"])
        ]

    @staticmethod
    def _emit(rows, values, change, idmult, units):
        existing = rows.get(values)
        if existing is None:
            rows[values] = DeltaRow(values, change, dict(idmult), dict(units))
        else:
            existing.delta += change
            add_dict(existing.idmult, idmult)
            add_dict(existing.units, units)

    def _delta_inner(self, node, deltas):
        left = self.relations[node["left"]]
        right = self.relations[node["right"]]
        info = self.indexes[node["id"]]
        on_left, on_right = info["on_left"], info["on_right"]
        left_delta = deltas[node["left"]]
        right_delta = deltas[node["right"]]
        rows = {}

        # (L + dL)(R + dR) - LR = dL*R + L*dR + dL*dR, applied per distinct
        # value tuple with multiplicity carried inside each delta.
        for ldelta in left_delta.rows.values():
            key = join_key(ldelta.values, left.columns, on_left)
            for right_state in self._right_matches(node, key):
                values = ldelta.values + right_state.values
                change = ldelta.delta * right_state.mult
                idmult = merge_identity(
                    ldelta.idmult, right_state.idmult, ldelta.delta, right_state.mult
                )
                units = self._rescale_units(
                    ldelta.units, right_state.units, ldelta.delta, right_state.mult
                )
                self._emit(rows, values, change, idmult, units)
        for rdelta in right_delta.rows.values():
            key = join_key(rdelta.values, right.columns, on_right)
            for left_state in self._left_matches(node, key):
                values = left_state.values + rdelta.values
                change = left_state.mult * rdelta.delta
                idmult = merge_identity(
                    left_state.idmult, rdelta.idmult, left_state.mult, rdelta.delta
                )
                units = self._rescale_units(
                    left_state.units, rdelta.units, left_state.mult, rdelta.delta
                )
                self._emit(rows, values, change, idmult, units)
        for ldelta in left_delta.rows.values():
            left_key = join_key(ldelta.values, left.columns, on_left)
            if any(part is None for part in left_key):
                continue
            for rdelta in right_delta.rows.values():
                right_key = join_key(rdelta.values, right.columns, on_right)
                if left_key != right_key or any(part is None for part in right_key):
                    continue
                values = ldelta.values + rdelta.values
                change = ldelta.delta * rdelta.delta
                idmult = merge_identity(
                    ldelta.idmult, rdelta.idmult, ldelta.delta, rdelta.delta
                )
                units = self._rescale_units(
                    ldelta.units, rdelta.units, ldelta.delta, rdelta.delta
                )
                self._emit(rows, values, change, idmult, units)
        return DeltaRelation(left.columns + right.columns, {
            key: row for key, row in rows.items() if row.delta != 0
        })

    def _match_mult_after_removal(self, node, unit, left_values):
        """Total right match multiplicity for one left row after removing u."""
        info = self.indexes[node["id"]]
        left = self.relations[node["left"]]
        right = self.relations[node["right"]]
        key = join_key(left_values, left.columns, info["on_left"])
        total = 0
        for state in self._right_matches(node, key):
            remaining = state.mult - state.units.get(unit, 0)
            if remaining < 0:
                raise InfluenceError("internal error: negative right multiplicity")
            total += remaining
        return total

    def _delta_left(self, node, unit, deltas):
        inner = self._delta_inner(node, deltas)
        rows = dict(inner.rows)
        left = self.relations[node["left"]]
        right = self.relations[node["right"]]
        info = self.indexes[node["id"]]
        left_delta = deltas[node["left"]]
        right_delta = deltas[node["right"]]
        null_extension = tuple(None for _ in right.columns)

        affected = set(left_delta.rows)
        for rdelta in right_delta.rows.values():
            key = join_key(rdelta.values, right.columns, info["on_right"])
            for left_state in self._left_matches(node, key):
                affected.add(left_state.values)

        for left_values in affected:
            left_state = left.rows.get(left_values)
            left_change = left_delta.rows.get(left_values)
            left_mult_delta = left_change.delta if left_change else 0
            baseline_left_mult = left_state.mult if left_state is not None else 0
            new_left_mult = baseline_left_mult + left_mult_delta
            if new_left_mult < 0:
                raise InfluenceError("internal error: negative left multiplicity")
            key = join_key(left_values, left.columns, info["on_left"])
            baseline_match = sum(
                state.mult for state in self._right_matches(node, key)
            )
            remaining_match = self._match_mult_after_removal(node, unit, left_values)
            baseline_null = baseline_left_mult if baseline_match == 0 else 0
            new_null = new_left_mult if remaining_match == 0 else 0
            change = new_null - baseline_null
            if change:
                values = left_values + null_extension
                if left_state is not None:
                    idmult = dict(left_state.idmult)
                else:
                    idmult = dict(left_change.idmult)
                self._emit(rows, values, change, idmult, {unit: change})
        return DeltaRelation(left.columns + right.columns, {
            key: row for key, row in rows.items() if row.delta != 0
        })

    def _delta_anti(self, node, unit, deltas):
        left = self.relations[node["left"]]
        right = self.relations[node["right"]]
        info = self.indexes[node["id"]]
        left_delta = deltas[node["left"]]
        right_delta = deltas[node["right"]]
        rows = {}

        affected = set(left_delta.rows)
        for rdelta in right_delta.rows.values():
            key = join_key(rdelta.values, right.columns, info["on_right"])
            for left_state in self._left_matches(node, key):
                affected.add(left_state.values)

        for left_values in affected:
            left_state = left.rows.get(left_values)
            left_change = left_delta.rows.get(left_values)
            left_mult_delta = left_change.delta if left_change else 0
            baseline_left_mult = left_state.mult if left_state is not None else 0
            new_left_mult = baseline_left_mult + left_mult_delta
            if new_left_mult < 0:
                raise InfluenceError("internal error: negative left multiplicity")
            key = join_key(left_values, left.columns, info["on_left"])
            baseline_match = sum(
                state.mult for state in self._right_matches(node, key)
            )
            remaining_match = self._match_mult_after_removal(node, unit, left_values)
            baseline_anti = baseline_left_mult if baseline_match == 0 else 0
            new_anti = new_left_mult if remaining_match == 0 else 0
            change = new_anti - baseline_anti
            if change:
                idmult = (
                    dict(left_state.idmult)
                    if left_state is not None
                    else dict(left_change.idmult)
                )
                self._emit(rows, left_values, change, idmult, {unit: change})
        return DeltaRelation(left.columns, {
            key: row for key, row in rows.items() if row.delta != 0
        })

    def _delta_aggregate(self, node, unit, source_delta):
        source = self.relations[node["input"]]
        key_positions = [source.columns.index(column) for column in node["keys"]]
        metric_specs = node["metrics"]
        changes_by_group = {}

        def bucket(group_key):
            return changes_by_group.setdefault(
                group_key, {metric["name"]: 0 for metric in metric_specs}
            )

        # Additive metrics: sum the signed derived-row delta directly.
        for delta in source_delta.rows.values():
            group_key = tuple(delta.values[index] for index in key_positions)
            changes = bucket(group_key)
            for metric in metric_specs:
                op = metric["op"]
                if op == "count_rows":
                    changes[metric["name"]] += delta.delta
                elif op in ("count", "sum"):
                    position = source.columns.index(metric["column"])
                    if delta.values[position] is None:
                        continue
                    if op == "count":
                        changes[metric["name"]] += delta.delta
                    else:
                        changes[metric["name"]] += delta.delta * delta.values[position]

        # count_distinct: diff remaining value support, never derived rows.
        for metric in metric_specs:
            if metric["op"] != "count_distinct":
                continue
            position = source.columns.index(metric["column"])
            affected_groups = set(changes_by_group)
            for delta in source_delta.rows.values():
                if delta.values[position] is not None:
                    affected_groups.add(
                        tuple(delta.values[index] for index in key_positions)
                    )
            for group_key in affected_groups:
                bucket(group_key)[metric["name"]] += self._distinct_delta(
                    node, unit, group_key, metric["name"], position,
                    key_positions, source_delta,
                )

        rows = {}
        columns = list(node["keys"]) + [metric["name"] for metric in metric_specs]
        for group_key, changes in changes_by_group.items():
            if not any(changes.values()):
                continue
            values = tuple(list(group_key) + [
                changes[metric["name"]] for metric in metric_specs
            ])
            rows[values] = DeltaRow(values, 1, {}, {unit: 1})
        return DeltaRelation(columns, rows)

    def _distinct_delta(self, node, unit, group_key, metric_name, position,
                        key_positions, source_delta):
        source = self.relations[node["input"]]
        # Support is a value's bag multiplicity.  The delta side is signed,
        # so a value loses existence exactly when baseline + delta reaches 0.
        baseline_support = {}
        for state in source.rows.values():
            if tuple(state.values[index] for index in key_positions) != group_key:
                continue
            value = state.values[position]
            if value is None:
                continue
            baseline_support[value] = baseline_support.get(value, 0) + state.mult
        delta_support = {}
        for delta in source_delta.rows.values():
            if tuple(delta.values[index] for index in key_positions) != group_key:
                continue
            value = delta.values[position]
            if value is None:
                continue
            delta_support[value] = delta_support.get(value, 0) + delta.delta
        change = 0
        for value in set(baseline_support) | set(delta_support):
            remaining = baseline_support.get(value, 0) + delta_support.get(value, 0)
            if remaining < 0:
                raise InfluenceError("internal error: negative distinct support")
            existed = baseline_support.get(value, 0) > 0
            survives = remaining > 0
            if existed and not survives:
                change -= 1
            elif not existed and survives:
                change += 1
        return change
