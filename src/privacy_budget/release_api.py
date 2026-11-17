"""Transactional operations behind the release CLI.

``init``, ``plan``, ``publish``, ``retire`` and ``catalog`` are exposed as
plain functions so they can be tested without a subprocess.  ``publish`` is
the only mutating planning call; it re-derives the frontier from the catalog
and remaining budget at commit time, refuses stale or dominated choices, and
writes the catalog mutation, budget charge, full report, and newly published
query aliases in one SQLite transaction.
"""
from __future__ import annotations

import json
import os
import tempfile
from fractions import Fraction
from pathlib import Path

from . import store
from .release_model import (
    ReleaseConflictError,
    ReleaseValidationError,
    StaleChoiceError,
    parse_request,
    plan_frontier,
    request_digest,
)
from .common import read


def _atomic_write(destination, text):
    """Atomic replacement, preserving the no-symlink output contract."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("output cannot be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as stream:
            handle = stream.name
            stream.write(text)
        os.replace(handle, destination)
    finally:
        if handle and os.path.exists(handle):
            os.unlink(handle)


# ---------------------------------------------------------------------------
# rendering


def _fraction(value):
    return str(Fraction(value))


def _lineage_payload(lineage):
    return [{"id": raw_id, "coefficient": _fraction(value)} for raw_id, value in lineage]


def _record_payload(record, request):
    return {
        "choice_id": record.choice_id,
        "feasible": True,
        "basis": list(record.basis),
        "new_measurement_ids": list(record.new_ids),
        "new_epsilon": _fraction(record.new_epsilon),
        "new_delta": _fraction(record.new_delta),
        "weights": {
            query_id: {key: _fraction(value) for key, value in sorted(weights.items())}
            for query_id, weights in sorted(record.weights.items())
        },
        "lineages": {
            query_id: _lineage_payload(lineage)
            for query_id, lineage in sorted(record.lineages.items())
        },
        "covariance": [[_fraction(value) for value in row] for row in record.covariance],
        "check_variances": [
            _fraction(value) for value in record.check_variances
        ],
        "metrics": {
            "new_epsilon": _fraction(record.new_epsilon),
            "new_delta": _fraction(record.new_delta),
            "check_variances": {
                check.id: _fraction(record.check_variances[index])
                for index, check in enumerate(request.checks)
            },
        },
    }


def render_plan(request, frontier, candidates, budget):
    return {
        "schema": "privacy-budget-release-plan-v1",
        "release_id": request.release_id,
        "request_sha256": request.request_sha256,
        "dataset": request.dataset,
        "principal": request.principal,
        "snapshot": request.snapshot,
        "dimensions": list(request.dimensions),
        "query_ids": [query.id for query in request.queries],
        "check_ids": [check.id for check in request.checks],
        "candidate_keys": [candidate.key for candidate in candidates],
        "budget": _budget_payload(budget),
        "choices": [_record_payload(record, request) for record in frontier],
        "feasible": bool(frontier),
    }


def _budget_payload(budget):
    return {
        "epsilon_limit": _fraction(budget.max_epsilon),
        "delta_limit": _fraction(budget.max_delta),
        "epsilon_spent": _fraction(budget.spent_epsilon),
        "delta_spent": _fraction(budget.spent_delta),
        "epsilon_remaining": _fraction(budget.max_epsilon - budget.spent_epsilon),
        "delta_remaining": _fraction(budget.max_delta - budget.spent_delta),
    }


def render_catalog(view, dataset, principal, snapshot):
    return {
        "schema": "privacy-budget-catalog-v1",
        "dataset": dataset,
        "principal": principal,
        "snapshot": snapshot,
        "dimensions": list(view.dimensions) if view.dimensions else [],
        "measurements": [
            {
                "id": raw_id,
                "coefficients": [_fraction(value) for value in measurement.coefficients],
                "value": _fraction(measurement.value),
                "variance": _fraction(measurement.variance),
                "epsilon": _fraction(measurement.epsilon),
                "delta": _fraction(measurement.delta),
                "retired": raw_id in view.retired,
            }
            for raw_id, measurement in sorted(view.measurements.items())
        ],
        "queries": [
            {
                "release_id": release_id,
                "query_id": query.id,
                "coefficients": [_fraction(value) for value in query.coefficients],
                "value": _fraction(query.value),
                "lineage": _lineage_payload(query.lineage),
                "retired_ancestors": sorted(
                    raw_id for raw_id, _ in query.lineage if raw_id in view.retired
                ),
            }
            for (release_id, _), query in sorted(view.queries.items())
        ],
    }


# ---------------------------------------------------------------------------
# request loading


def prepare_request(data):
    """Parse a request and bind it to its canonical identity digest."""
    from dataclasses import replace

    request = parse_request(data, "")
    return replace(request, request_sha256=request_digest(request))

# ---------------------------------------------------------------------------
# init


def parse_limits(data):
    from .common import rows
    from .release_model import rational, strict_ident

    if not isinstance(data, dict) or set(data) != {"limits"}:
        raise ReleaseValidationError("init input must contain exactly 'limits'")
    limits = {}
    for index, item in enumerate(rows(data["limits"], 1000)):
        if not isinstance(item, dict) or set(item) != {"dataset", "principal", "epsilon", "delta"}:
            raise ReleaseValidationError("invalid limit row fields")
        key = (
            strict_ident(item["dataset"], "limits.dataset"),
            strict_ident(item["principal"], "limits.principal"),
        )
        if key in limits:
            raise ReleaseValidationError("duplicate budget limit")
        epsilon = rational(item["epsilon"], "limits.epsilon", allow_negative=False)
        delta = rational(item["delta"], "limits.delta", allow_negative=False)
        limits[key] = (epsilon, delta)
    return limits


def init(db, source):
    data, digest = read(source)
    limits = parse_limits(data)
    connection = store.connect(db)
    try:
        created = store.initialise(connection, limits)
    finally:
        connection.close()
    return {
        "schema": "privacy-budget-init-v1",
        "source_sha256": digest,
        "created": created,
    }


# ---------------------------------------------------------------------------
# plan


def _reject_cross_scope_alias(connection, request, view):
    for item in request.fresh:
        if item.id in view.measurements:
            continue
        elsewhere = store.find_measurement_elsewhere(
            connection, request.dataset, request.principal, request.snapshot, item.id
        )
        if elsewhere is not None:
            raise ReleaseValidationError(
                "measurement belongs to a different snapshot: " + item.id
            )
    for item in request.reuse:
        if (item.release_id, item.query_id) in view.queries:
            continue
        elsewhere = store.find_alias_elsewhere(
            connection,
            item.release_id,
            item.query_id,
            request.dataset,
            request.principal,
            request.snapshot,
        )
        if elsewhere is not None:
            raise ReleaseValidationError(
                "alias belongs to a different snapshot/scope: %s/%s"
                % (item.release_id, item.query_id)
            )


def plan(db, source):
    data, _ = read(source)
    request = prepare_request(data)
    connection = store.open_readonly(db)
    try:
        view = store.load_view(
            connection, request.dataset, request.principal, request.snapshot
        )
        _reject_cross_scope_alias(connection, request, view)
        budget = store.load_budget(connection, request.dataset, request.principal)
        if budget is None:
            raise ReleaseValidationError("scope has no declared budget")
        frontier, candidates = plan_frontier(request, view, budget)
    finally:
        connection.close()
    return render_plan(request, frontier, candidates, budget)


# ---------------------------------------------------------------------------
# publish


def _check_choice(frontier, choice_id):
    for record in frontier:
        if record.choice_id == choice_id:
            return record
    raise StaleChoiceError("choice is not on the current frontier")


def _candidate_values(request, view):
    """Resolve each candidate key to its trusted estimate before the commit."""
    values = {}
    for item in request.fresh:
        published = view.measurements.get(item.id)
        values["fresh:" + item.id] = (
            published.value if published is not None else item.value
        )
    for item in request.reuse:
        values["reuse:%s/%s" % (item.release_id, item.query_id)] = view.queries[
            (item.release_id, item.query_id)
        ].value
    return values


def _commit(connection, request, record, view, budget):
    """Insert the measurement, query, release rows in an open transaction."""
    values = _candidate_values(request, view)
    for raw_id in record.new_ids:
        fresh = next(item for item in request.fresh if item.id == raw_id)
        connection.execute(
            "INSERT INTO measurements"
            "(dataset, principal, snapshot, id, coefficients, value, variance, epsilon, delta, retired)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            (
                request.dataset,
                request.principal,
                request.snapshot,
                raw_id,
                store.encode_vector(fresh.coefficients),
                _fraction(fresh.value),
                _fraction(fresh.variance),
                _fraction(fresh.epsilon),
                _fraction(fresh.delta),
            ),
        )
    domain_row = connection.execute(
        "SELECT dimensions FROM domains WHERE dataset=? AND principal=? AND snapshot=?",
        (request.dataset, request.principal, request.snapshot),
    ).fetchone()
    if domain_row is None:
        connection.execute(
            "INSERT INTO domains(dataset, principal, snapshot, dimensions) VALUES (?,?,?,?)",
            (
                request.dataset,
                request.principal,
                request.snapshot,
                store.encode_names(request.dimensions),
            ),
        )
    for query in request.queries:
        connection.execute(
            "INSERT INTO queries"
            "(dataset, principal, snapshot, release_id, query_id, coefficients, value, lineage)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                request.dataset,
                request.principal,
                request.snapshot,
                request.release_id,
                query.id,
                store.encode_vector(query.coefficients),
                _fraction(
                    sum(
                        (
                            weight * values[key]
                            for key, weight in record.weights[query.id].items()
                        ),
                        Fraction(0),
                    )
                ),
                store.encode_lineage(record.lineages[query.id]),
            ),
        )
    report = render_publish_report(request, record, values, budget)
    connection.execute(
        "INSERT INTO releases"
        "(release_id, dataset, principal, snapshot, request_sha256, choice_id, report)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            request.release_id,
            request.dataset,
            request.principal,
            request.snapshot,
            request.request_sha256,
            record.choice_id,
            json.dumps(report, sort_keys=True, ensure_ascii=False),
        ),
    )
    return report


def render_publish_report(request, record, values, budget):
    committed_epsilon = budget.spent_epsilon + record.new_epsilon
    committed_delta = budget.spent_delta + record.new_delta
    estimates = {}
    for query in request.queries:
        estimates[query.id] = sum(
            (
                weight * values[key]
                for key, weight in record.weights[query.id].items()
            ),
            Fraction(0),
        )
    return {
        "schema": "privacy-budget-release-report-v1",
        "release_id": request.release_id,
        "request_sha256": request.request_sha256,
        "choice_id": record.choice_id,
        "dataset": request.dataset,
        "principal": request.principal,
        "snapshot": request.snapshot,
        "dimensions": list(request.dimensions),
        "basis": list(record.basis),
        "new_measurement_ids": list(record.new_ids),
        "new_epsilon": _fraction(record.new_epsilon),
        "new_delta": _fraction(record.new_delta),
        "committed_epsilon": _fraction(committed_epsilon),
        "committed_delta": _fraction(committed_delta),
        "epsilon_limit": _fraction(budget.max_epsilon),
        "delta_limit": _fraction(budget.max_delta),
        "epsilon_remaining": _fraction(budget.max_epsilon - committed_epsilon),
        "delta_remaining": _fraction(budget.max_delta - committed_delta),
        "queries": [
            {
                "id": query.id,
                "coefficients": [_fraction(value) for value in query.coefficients],
                "weights": {
                    key: _fraction(value)
                    for key, value in sorted(record.weights[query.id].items())
                },
                "estimate": _fraction(estimates[query.id]),
                "lineage": _lineage_payload(record.lineages[query.id]),
            }
            for query in request.queries
        ],
        "covariance": [[_fraction(value) for value in row] for row in record.covariance],
        "checks": [
            {
                "id": check.id,
                "max_variance": _fraction(check.max_variance),
                "actual_variance": _fraction(record.check_variances[index]),
            }
            for index, check in enumerate(request.checks)
        ],
    }


def publish(db, source, choice_id):
    data, _ = read(source)
    request = prepare_request(data)
    from .release_model import strict_ident

    choice_id = strict_ident(choice_id, "choice_id")
    connection = store.connect(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = store.load_release(connection, request.release_id)
            if existing is not None:
                if (
                    existing["request_sha256"] != request.request_sha256
                    or existing["choice_id"] != choice_id
                ):
                    raise ReleaseConflictError(
                        "release_id is already bound to a different request/choice"
                    )
                connection.execute("COMMIT")
                return json.loads(existing["report"]), True
            view = store.load_view(
                connection, request.dataset, request.principal, request.snapshot
            )
            _reject_cross_scope_alias(connection, request, view)
            budget = store.load_budget(connection, request.dataset, request.principal)
            if budget is None:
                raise ReleaseValidationError("scope has no declared budget")
            frontier, _ = plan_frontier(request, view, budget)
            record = _check_choice(frontier, choice_id)
            report = _commit(connection, request, record, view, budget)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return report, False


# ---------------------------------------------------------------------------
# retire / catalog


def retire(db, dataset, principal, snapshot, measurement):
    from .release_model import strict_ident

    dataset = strict_ident(dataset, "dataset")
    principal = strict_ident(principal, "principal")
    snapshot = strict_ident(snapshot, "snapshot")
    measurement = strict_ident(measurement, "measurement")
    if not Path(db).exists():
        raise ReleaseValidationError("catalog database does not exist")
    connection = store.connect(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT retired FROM measurements WHERE dataset=? AND principal=? AND snapshot=? AND id=?",
                (dataset, principal, snapshot, measurement),
            ).fetchone()
            if row is None:
                raise ReleaseValidationError("unknown raw measurement")
            if not row["retired"]:
                connection.execute(
                    "UPDATE measurements SET retired=1 WHERE dataset=? AND principal=? AND snapshot=? AND id=?",
                    (dataset, principal, snapshot, measurement),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return {
        "schema": "privacy-budget-retire-v1",
        "dataset": dataset,
        "principal": principal,
        "snapshot": snapshot,
        "measurement": measurement,
        "retired": True,
    }


def catalog(db, dataset, principal, snapshot):
    connection = store.open_readonly(db)
    try:
        view = store.load_view(connection, dataset, principal, snapshot)
        budget = store.load_budget(connection, dataset, principal)
    finally:
        connection.close()
    payload = render_catalog(view, dataset, principal, snapshot)
    if budget is not None:
        payload["budget"] = _budget_payload(budget)
    return payload
