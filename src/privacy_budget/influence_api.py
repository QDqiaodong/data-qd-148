"""Read-only privacy-unit influence audit: public API and report assembly.

The audit is strictly read-only:

* the source database is opened with ``mode=ro`` and never modified, created,
  attached to a trigger, or queried with user supplied SQL,
* one explicit deferred read transaction supplies the schema, every source
  row, the unit list, the baseline and every counterfactual,
* no noise is generated and no privacy budget is charged; the reported local
  influences are *not* claimed to be DP global sensitivities.

The function :func:`audit_influence` is the testable in-process entry point;
the ``privacy-budget influence`` CLI subcommand is a thin wrapper around it.
"""
from __future__ import annotations

import json

from .common import read
from .influence_engine import InfluenceEngine
from .influence_model import parse_query, query_sha256
from .influence_snapshot import close_snapshot, open_snapshot_connection, read_snapshot

REPORT_SCHEMA = "privacy-budget-influence-report-v1"


def load_query(source):
    """Read and strictly validate a query document."""
    data, source_digest = read(source)
    query = parse_query(data)
    return query, query_sha256(query), source_digest


def _canonical_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _group_key_payload(group_key):
    return list(group_key)


def _baseline_payload(relation, node):
    metric_names = [metric["name"] for metric in node["metrics"]]
    key_columns = list(node["keys"])
    groups = []
    for state in relation.rows.values():
        key_values = list(state.values[: len(key_columns)])
        metric_values = state.values[len(key_columns):]
        groups.append(
            {
                "group_key": key_values,
                "metrics": {
                    name: int(value) for name, value in zip(metric_names, metric_values)
                },
            }
        )
    groups.sort(key=lambda item: _canonical_json_bytes(item["group_key"]))
    return {"key_columns": key_columns, "metric_names": metric_names, "groups": groups}


def _aggregate_after_removal(engine, node, deltas):
    """Counterfactual aggregate values: baseline plus signed metric deltas."""
    baseline = engine.relations[node["id"]]
    delta = deltas[node["id"]]
    key_columns = list(node["keys"])
    metric_names = [metric["name"] for metric in node["metrics"]]
    result = {}
    for state in baseline.rows.values():
        key = tuple(state.values[: len(key_columns)])
        metrics = state.values[len(key_columns):]
        result[key] = {name: int(value) for name, value in zip(metric_names, metrics)}
    for delta_row in delta.rows.values():
        key = tuple(delta_row.values[: len(key_columns)])
        metric_deltas = delta_row.values[len(key_columns):]
        bucket = result.setdefault(key, {name: 0 for name in metric_names})
        for name, change in zip(metric_names, metric_deltas):
            bucket[name] = int(bucket.get(name, 0)) + int(change)
    return result, metric_names, key_columns


def audit_influence(db_path, query_source):
    """Run the full audit against a read-only snapshot.

    Returns a report dict.  Raises :class:`InfluenceError` (a ``ValueError``
    subclass) on any contract, snapshot or execution failure.
    """
    query, canonical_digest, source_digest = load_query(query_source)
    connection = open_snapshot_connection(db_path)
    try:
        snapshot, logical_digest = read_snapshot(connection, query["tables"])
        engine = InfluenceEngine(query, snapshot)
        baseline_relation = engine.evaluate_baseline()
        root = query["nodes"][query["root"]]
        baseline = _baseline_payload(baseline_relation, root)

        unit_reports = []
        max_l1 = 0
        max_linf = 0
        l1_witnesses = []
        linf_witnesses = []
        for unit in sorted(engine.unit_set):
            deltas = engine.propagate_unit(unit)
            after, metric_names, key_columns = _aggregate_after_removal(
                engine, root, deltas
            )
            before = {
                tuple(group["group_key"]): dict(group["metrics"])
                for group in baseline["groups"]
            }
            cells = _diff_groups(before, after, metric_names)
            l1 = sum(abs(cell["delta"]) for cell in cells)
            linf = max((abs(cell["delta"]) for cell in cells), default=0)
            unit_reports.append(
                {
                    "unit": unit,
                    "changed_cells": cells,
                    "l1": l1,
                    "linfinity": linf,
                }
            )
            if l1 > max_l1:
                max_l1, l1_witnesses = l1, [unit]
            elif l1 == max_l1 and l1 > 0:
                l1_witnesses.append(unit)
            if linf > max_linf:
                max_linf, linf_witnesses = linf, [unit]
            elif linf == max_linf and linf > 0:
                linf_witnesses.append(unit)
        # All units tie at zero when nothing changes: every unit witnesses.
        if max_l1 == 0:
            l1_witnesses = sorted(engine.unit_set)
        if max_linf == 0:
            linf_witnesses = sorted(engine.unit_set)

        diagnostics = {
            "source_scans": engine.scan_count,
            "full_node_evaluations": engine.full_evaluations,
            "delta_node_evaluations": engine.delta_evaluations,
            "node_count": len(engine.nodes),
            "table_count": len(engine.tables),
            "unit_count": len(engine.unit_set),
            "per_unit_full_evaluations": 0,
            "per_unit_source_scans": 0,
        }
        report = {
            "schema": REPORT_SCHEMA,
            "status": "ok",
            "query_sha256": canonical_digest,
            "query_source_sha256": source_digest,
            "logical_snapshot_sha256": logical_digest,
            "root": query["root"],
            "baseline": baseline,
            "units": unit_reports,
            "max_l1": max_l1,
            "max_linfinity": max_linf,
            "max_l1_witness_units": sorted(l1_witnesses),
            "max_linfinity_witness_units": sorted(linf_witnesses),
            "diagnostics": diagnostics,
            "dp_notice": (
                "local remove-one-unit influence on this snapshot only; "
                "not a DP global sensitivity; no noise generated and no budget charged"
            ),
        }
        # The read transaction stays open until every result is assembled.
        return report
    finally:
        close_snapshot(connection)


def _diff_groups(before, after, metric_names):
    cells = []
    keys = set(before) | set(after)
    for key in keys:
        before_metrics = before.get(key)
        after_metrics = after.get(key)
        for metric in metric_names:
            before_value = before_metrics.get(metric, 0) if before_metrics else 0
            after_value = after_metrics.get(metric, 0) if after_metrics else 0
            delta = after_value - before_value
            if delta != 0:
                cells.append(
                    {
                        "group_key": list(key),
                        "metric": metric,
                        "before": int(before_value),
                        "after": int(after_value),
                        "delta": int(delta),
                    }
                )
    cells.sort(
        key=lambda cell: (
            _canonical_json_bytes(cell["group_key"]),
            cell["metric"].encode("utf-8"),
        )
    )
    return cells


def report_sha256(report):
    """Stable content digest over the canonical report.

    The digest binds everything except ``query_source_sha256`` (which tracks
    the exact input bytes, including incidental key order) so the same logical
    query and snapshot always produce the same report content and digest.
    """
    import hashlib

    identity = {
        key: value
        for key, value in report.items()
        if key not in ("query_source_sha256", "report_sha256")
    }
    return hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()


def finalize_report(report):
    """Attach the content digest; keep the payload JSON-deterministic."""
    report["report_sha256"] = report_sha256(report)
    return report
