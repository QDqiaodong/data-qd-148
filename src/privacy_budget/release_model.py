"""Exact-rational joint release planning.

This module is intentionally free of I/O.  It parses the strict release
request contract, validates it against a catalog snapshot, enumerates every
linearly independent candidate basis, expands reconstruction weights to raw
noise lineage, and returns the budget/error Pareto frontier.

All arithmetic is :class:`fractions.Fraction`; nothing here samples noise or
checks a differential-privacy proof.  Upstream noise declarations are trusted
inputs, exactly as the task contract specifies.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from typing import List, Mapping, Optional, Tuple

from .common import require

MAX_ABS = 10 ** 12
MAX_DIMENSIONS = 6
MAX_CANDIDATES = 16
MAX_QUERIES = 8
MAX_CHECKS = 16
IDENT_LIMIT = 128


class ReleaseValidationError(ValueError):
    """The request, catalog binding, or budget declaration is invalid."""


class CatalogConflictError(ReleaseValidationError):
    """A reused identity or coordinate domain conflicts with the catalog."""


class NoFeasibleBasisError(ReleaseValidationError):
    """No legal basis satisfies span, retirement, error, and budget rules."""


class StaleChoiceError(ReleaseValidationError):
    """The requested choice is not on the freshly recomputed frontier."""


class ReleaseConflictError(ReleaseValidationError):
    """A release_id is already bound to a different request/choice."""


# ---------------------------------------------------------------------------
# strict scalar parsing


def strict_ident(value, label="identifier"):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > IDENT_LIMIT
        or any(ord(c) < 32 for c in value)
        or ":" in value
        or "/" in value
    ):
        raise ReleaseValidationError(
            "expected nonblank identifier <=128 chars without ':' or '/': " + label
        )
    return value


def _int_field(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReleaseValidationError("integer outside allowed bounds: " + label)
    return value


def rational(value, label="rational", allow_negative=True, allow_zero=True):
    """Parse the canonical ``{numerator, denominator}`` rational form."""
    require(value, "numerator denominator")
    low = -MAX_ABS if allow_negative else 0
    numerator = _int_field(value["numerator"], low, MAX_ABS, label + ".numerator")
    denominator = _int_field(value["denominator"], 1, MAX_ABS, label + ".denominator")
    if numerator == 0 and not allow_zero:
        raise ReleaseValidationError("zero not allowed: " + label)
    return Fraction(numerator, denominator)


def _bounded_array(value, limit, label, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise ReleaseValidationError(
            "expected array of length %d..%d: %s" % (minimum, limit, label)
        )
    return value


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseValidationError("duplicate JSON key: " + key)
        result[key] = value
    return result


def decode_request(raw):
    """Decode request bytes with strict JSON rules; never accepts duplicates."""
    try:
        return json.loads(
            raw, object_pairs_hook=_unique_keys, parse_constant=_bad_constant
        )
    except ReleaseValidationError:
        raise
    except (ValueError, UnicodeDecodeError) as error:
        raise ReleaseValidationError("invalid JSON: " + str(error))


def _bad_constant(value):
    raise ReleaseValidationError("nonfinite JSON number: " + str(value))


# ---------------------------------------------------------------------------
# intra-snapshot catalog view


@dataclass(frozen=True)
class Measurement:
    """A published raw measurement: an immutable identity and declaration."""

    id: str
    coefficients: Tuple[Fraction, ...]
    value: Fraction
    variance: Fraction
    epsilon: Fraction
    delta: Fraction


@dataclass(frozen=True)
class PublishedQuery:
    id: str
    coefficients: Tuple[Fraction, ...]
    value: Fraction
    lineage: Tuple[Tuple[str, Fraction], ...]


@dataclass(frozen=True)
class SnapshotView:
    """Everything the planner needs about one (dataset, principal, snapshot)."""

    measurements: Mapping[str, Measurement]
    retired: frozenset
    queries: Mapping[Tuple[str, str], PublishedQuery]
    dimensions: Optional[Tuple[str, ...]] = None


# ---------------------------------------------------------------------------
# linear algebra over Fraction


def cleanup(vector):
    return tuple(value for value in vector if value != 0)


def solve_all(targets, basis_vectors, labels):
    """Reconstruct several targets in one exact elimination.

    Returns one dict per target (label -> signed coefficient), or ``None`` if
    any target lies outside the span.  The basis is expected to be linearly
    independent, but the routine also copes with a redundant basis by falling
    back to the span-preserving pivot rows (weights on dependent columns are
    then zero, which is still a valid reconstruction).
    """
    targets = [tuple(target) for target in targets]
    columns = len(basis_vectors)
    count = len(targets)
    width = max(
        [len(vector) for vector in basis_vectors]
        + [len(target) for target in targets]
        + [0]
    )
    padded_basis = [
        list(vector) + [Fraction(0)] * (width - len(vector)) for vector in basis_vectors
    ]
    padded_targets = [
        list(target) + [Fraction(0)] * (width - len(target)) for target in targets
    ]
    # Rows are coordinates; columns 0..columns-1 are basis unknowns, the last
    # ``count`` columns are the target right-hand sides.
    augmented = [
        [padded_basis[c][r] for c in range(columns)]
        + [padded_targets[j][r] for j in range(count)]
        for r in range(width)
    ]
    pivot_columns: List[int] = []
    pivot_row = 0
    for column in range(columns):
        pivot = next(
            (r for r in range(pivot_row, len(augmented)) if augmented[r][column] != 0),
            None,
        )
        if pivot is None:
            continue
        augmented[pivot_row], augmented[pivot] = augmented[pivot], augmented[pivot_row]
        scale = augmented[pivot_row][column]
        augmented[pivot_row] = [value / scale for value in augmented[pivot_row]]
        head = augmented[pivot_row]
        for other in range(len(augmented)):
            if other != pivot_row and augmented[other][column] != 0:
                factor = augmented[other][column]
                augmented[other] = [
                    augmented[other][i] - factor * head[i]
                    for i in range(columns + count)
                ]
        pivot_columns.append(column)
        pivot_row += 1
        if pivot_row == len(augmented):
            break

    # Any row with no remaining basis coordinate but a nonzero RHS shows a
    # target outside the span.
    for row in augmented:
        if any(row[c] != 0 for c in range(columns)):
            continue
        if any(row[columns + j] != 0 for j in range(count)):
            return None

    solutions = [{label: Fraction(0) for label in labels} for _ in range(count)]
    for row_index, column in enumerate(pivot_columns):
        row = augmented[row_index]
        for j in range(count):
            solutions[j][labels[column]] = row[columns + j]
    return solutions


def solve_combination(target, basis_vectors, labels):
    """Reconstruct a single target; ``None`` when it lies outside the span."""
    result = solve_all([target], basis_vectors, labels)
    return None if result is None else result[0]


def noise_gram(candidates, variance_by_id):
    """Pairwise raw-noise inner products over candidates.

    ``S[a][b] = sum_raw coeff_a(raw) * coeff_b(raw) * var(raw)`` lets the
    covariance of any basis be assembled as ``w_i^T S w_j`` without
    re-expanding the full lineage for every query pair.
    """
    sparse = [dict(candidate.lineage) for candidate in candidates]
    size = len(candidates)
    gram = [[Fraction(0)] * size for _ in range(size)]
    for a in range(size):
        for b in range(a, size):
            total = Fraction(0)
            left = sparse[a]
            right = sparse[b]
            for raw_id, coefficient in left.items():
                partner = right.get(raw_id)
                if partner is not None:
                    total += coefficient * partner * variance_by_id[raw_id]
            gram[a][b] = total
            gram[b][a] = total
    return gram


class Span:
    """Incrementally maintain a reduced basis of the span of added vectors."""

    __slots__ = ("width", "rows")

    def __init__(self, width, rows=()):
        self.width = width
        self.rows = list(rows)

    def clone(self):
        return Span(self.width, self.rows)

    def add(self, vector):
        """Add a vector; return False when it is linearly dependent."""
        candidate = list(vector) + [Fraction(0)] * (self.width - len(vector))
        for pivot, row in self.rows:
            if candidate[pivot] != 0:
                factor = candidate[pivot]
                candidate = [a - factor * b for a, b in zip(candidate, row)]
        pivot = next((i for i, value in enumerate(candidate) if value != 0), None)
        if pivot is None:
            return False
        scale = candidate[pivot]
        self.rows.append((pivot, [value / scale for value in candidate]))
        return True


# ---------------------------------------------------------------------------
# request contract


@dataclass(frozen=True)
class Fresh:
    id: str
    coefficients: Tuple[Fraction, ...]
    value: Fraction
    variance: Fraction
    epsilon: Fraction
    delta: Fraction


@dataclass(frozen=True)
class Reuse:
    release_id: str
    query_id: str


@dataclass(frozen=True)
class Query:
    id: str
    coefficients: Tuple[Fraction, ...]


@dataclass(frozen=True)
class Check:
    id: str
    weights: Mapping[str, Fraction]
    max_variance: Fraction


@dataclass(frozen=True)
class ReleaseRequest:
    release_id: str
    dataset: str
    principal: str
    snapshot: str
    dimensions: Tuple[str, ...]
    fresh: Tuple[Fresh, ...]
    reuse: Tuple[Reuse, ...]
    queries: Tuple[Query, ...]
    checks: Tuple[Check, ...]
    request_sha256: str = ""


def _coefficients(value, width, label):
    if not isinstance(value, list) or len(value) != width:
        raise ReleaseValidationError(
            "coefficient vector must have %d entries: %s" % (width, label)
        )
    return tuple(rational(item, "%s[%d]" % (label, i)) for i, item in enumerate(value))


def parse_request(data, digest):
    require(
        data,
        "release_id dataset principal snapshot dimensions fresh reuse queries checks",
    )
    release_id = strict_ident(data["release_id"], "release_id")
    dataset = strict_ident(data["dataset"], "dataset")
    principal = strict_ident(data["principal"], "principal")
    snapshot = strict_ident(data["snapshot"], "snapshot")
    raw_dimensions = _bounded_array(
        data["dimensions"], MAX_DIMENSIONS, "dimensions", minimum=1
    )
    dimensions = tuple(strict_ident(name, "dimensions") for name in raw_dimensions)
    if len(set(dimensions)) != len(dimensions):
        raise ReleaseValidationError("dimension names must be unique")
    width = len(dimensions)

    fresh = []
    for index, item in enumerate(_bounded_array(data["fresh"], MAX_CANDIDATES, "fresh")):
        require(item, "id coefficients value variance epsilon delta")
        epsilon = rational(item["epsilon"], "fresh[%d].epsilon" % index, allow_negative=False)
        delta = rational(item["delta"], "fresh[%d].delta" % index, allow_negative=False)
        if epsilon <= 0 or delta >= 1:
            raise ReleaseValidationError("fresh mechanism requires epsilon>0 and delta<1")
        fresh.append(
            Fresh(
                id=strict_ident(item["id"], "fresh.id"),
                coefficients=_coefficients(item["coefficients"], width, "fresh.coefficients"),
                value=rational(item["value"], "fresh.value"),
                variance=rational(item["variance"], "fresh.variance", allow_negative=False),
                epsilon=epsilon,
                delta=delta,
            )
        )

    reuse = []
    for index, item in enumerate(_bounded_array(data["reuse"], MAX_CANDIDATES, "reuse")):
        require(item, "release_id query_id")
        reuse.append(
            Reuse(
                release_id=strict_ident(item["release_id"], "reuse.release_id"),
                query_id=strict_ident(item["query_id"], "reuse.query_id"),
            )
        )
    if len(fresh) + len(reuse) > MAX_CANDIDATES:
        raise ReleaseValidationError("fresh and reuse total exceeds 16")

    keys = ["fresh:" + item.id for item in fresh]
    keys += ["reuse:%s/%s" % (item.release_id, item.query_id) for item in reuse]
    if len(set(keys)) != len(keys):
        raise ReleaseValidationError("duplicate candidate ID")

    queries = []
    for index, item in enumerate(_bounded_array(data["queries"], MAX_QUERIES, "queries", minimum=1)):
        require(item, "id coefficients")
        queries.append(
            Query(
                id=strict_ident(item["id"], "queries.id"),
                coefficients=_coefficients(item["coefficients"], width, "queries.coefficients"),
            )
        )
    query_ids = [item.id for item in queries]
    if len(set(query_ids)) != len(query_ids):
        raise ReleaseValidationError("duplicate query ID")

    checks = []
    for index, item in enumerate(_bounded_array(data["checks"], MAX_CHECKS, "checks", minimum=1)):
        require(item, "id weights max_variance")
        weights_obj = item["weights"]
        if not isinstance(weights_obj, dict):
            raise ReleaseValidationError("check weights must be an object")
        weights = {}
        for query_id, value in weights_obj.items():
            if query_id not in query_ids:
                raise ReleaseValidationError("check weight references unknown query: " + query_id)
            weights[query_id] = rational(value, "checks[%d].weights" % index)
        checks.append(
            Check(
                id=strict_ident(item["id"], "checks.id"),
                weights=weights,
                max_variance=rational(
                    item["max_variance"], "checks.max_variance", allow_negative=False
                ),
            )
        )
    check_ids = [item.id for item in checks]
    if len(set(check_ids)) != len(check_ids):
        raise ReleaseValidationError("duplicate check ID")

    return ReleaseRequest(
        release_id=release_id,
        dataset=dataset,
        principal=principal,
        snapshot=snapshot,
        dimensions=dimensions,
        fresh=tuple(sorted(fresh, key=lambda item: item.id)),
        reuse=tuple(sorted(reuse, key=lambda item: (item.release_id, item.query_id))),
        queries=tuple(sorted(queries, key=lambda item: item.id)),
        checks=tuple(sorted(checks, key=lambda item: item.id)),
        request_sha256=digest,
    )


# ---------------------------------------------------------------------------
# canonical identity


def _fraction_string(value):
    return str(value)


def to_fraction_string(value):
    """Canonical reduced ``str(Fraction)`` representation."""
    return str(Fraction(value))


def parse_fraction_string(text, label="fraction"):
    if not isinstance(text, str) or not text:
        raise ReleaseValidationError("expected fraction string: " + label)
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ReleaseValidationError("invalid fraction string: " + label)


def canonical_request(request):
    return {
        "release_id": request.release_id,
        "dataset": request.dataset,
        "principal": request.principal,
        "snapshot": request.snapshot,
        "dimensions": list(request.dimensions),
        "fresh": [
            {
                "id": item.id,
                "coefficients": [_fraction_string(v) for v in item.coefficients],
                "value": _fraction_string(item.value),
                "variance": _fraction_string(item.variance),
                "epsilon": _fraction_string(item.epsilon),
                "delta": _fraction_string(item.delta),
            }
            for item in request.fresh
        ],
        "reuse": [
            {"release_id": item.release_id, "query_id": item.query_id}
            for item in request.reuse
        ],
        "queries": [
            {
                "id": item.id,
                "coefficients": [_fraction_string(v) for v in item.coefficients],
            }
            for item in request.queries
        ],
        "checks": [
            {
                "id": item.id,
                "weights": {q: _fraction_string(v) for q, v in item.weights.items()},
                "max_variance": _fraction_string(item.max_variance),
            }
            for item in request.checks
        ],
    }


def canonical_digest(payload):
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_digest(request):
    return canonical_digest(canonical_request(request))


def choice_digest(request_sha256, basis_keys):
    return canonical_digest(
        {"request_sha256": request_sha256, "basis": sorted(basis_keys)}
    )


# ---------------------------------------------------------------------------
# catalog resolution


@dataclass(frozen=True)
class Candidate:
    key: str
    coefficients: Tuple[Fraction, ...]
    value: Fraction
    lineage: Tuple[Tuple[str, Fraction], ...]
    fresh_id: Optional[str]
    epsilon: Fraction
    delta: Fraction
    is_new: bool


@dataclass(frozen=True)
class ScopeBudget:
    max_epsilon: Fraction
    max_delta: Fraction
    spent_epsilon: Fraction
    spent_delta: Fraction


def merge_lineage(terms):
    """Signed merge over raw measurement IDs, dropping exact zeros."""
    accumulator = {}
    for lineage, scale in terms:
        if scale == 0:
            continue
        for raw_id, coefficient in lineage:
            accumulator[raw_id] = accumulator.get(raw_id, Fraction(0)) + scale * coefficient
    return tuple(sorted((raw_id, value) for raw_id, value in accumulator.items() if value != 0))


def resolve_candidates(request, view):
    width = len(request.dimensions)
    if view.dimensions is not None and tuple(view.dimensions) != tuple(request.dimensions):
        raise CatalogConflictError(
            "coordinate domain is fixed for this scope/snapshot"
        )
    wide = (
        view.dimensions is not None
        and len(view.dimensions) != len(request.dimensions)
    )
    if wide:
        raise CatalogConflictError("coordinate domain width is fixed")
    candidates = []
    for item in request.fresh:
        published = view.measurements.get(item.id)
        if published is not None:
            declared = (
                item.coefficients,
                item.value,
                item.variance,
                item.epsilon,
                item.delta,
            )
            stored = (
                published.coefficients,
                published.value,
                published.variance,
                published.epsilon,
                published.delta,
            )
            if declared != stored:
                raise CatalogConflictError(
                    "fresh measurement identity conflicts with catalog: " + item.id
                )
            if len(published.coefficients) != width:
                raise CatalogConflictError(
                    "measurement dimension conflicts with request: " + item.id
                )
            candidates.append(
                Candidate(
                    key="fresh:" + item.id,
                    coefficients=published.coefficients,
                    value=published.value,
                    lineage=((item.id, Fraction(1)),),
                    fresh_id=item.id,
                    epsilon=published.epsilon,
                    delta=published.delta,
                    is_new=False,
                )
            )
        else:
            candidates.append(
                Candidate(
                    key="fresh:" + item.id,
                    coefficients=item.coefficients,
                    value=item.value,
                    lineage=((item.id, Fraction(1)),),
                    fresh_id=item.id,
                    epsilon=item.epsilon,
                    delta=item.delta,
                    is_new=True,
                )
            )
    for item in request.reuse:
        published = view.queries.get((item.release_id, item.query_id))
        if published is None:
            raise CatalogConflictError(
                "unknown published alias in this snapshot: %s/%s"
                % (item.release_id, item.query_id)
            )
        if len(published.coefficients) != width:
            raise CatalogConflictError(
                "alias dimension conflicts with request: %s/%s"
                % (item.release_id, item.query_id)
            )
        candidates.append(
            Candidate(
                key="reuse:%s/%s" % (item.release_id, item.query_id),
                coefficients=published.coefficients,
                value=published.value,
                lineage=published.lineage,
                fresh_id=None,
                epsilon=Fraction(0),
                delta=Fraction(0),
                is_new=False,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.key)


# ---------------------------------------------------------------------------
# basis enumeration and frontier


@dataclass(frozen=True)
class PlanRecord:
    basis: Tuple[str, ...]
    new_ids: Tuple[str, ...]
    new_epsilon: Fraction
    new_delta: Fraction
    weights: Mapping[str, Mapping[str, Fraction]]
    lineages: Mapping[str, Tuple[Tuple[str, Fraction], ...]]
    covariance: Tuple[Tuple[Fraction, ...], ...]
    check_variances: Tuple[Fraction, ...]
    choice_id: str = ""

    def metrics(self):
        return (self.new_epsilon, self.new_delta) + self.check_variances

    def dedupe_key(self):
        return (len(self.new_ids), len(self.basis), self.basis)


def _evaluate(
    request,
    chosen,
    candidate_by_key,
    measurement_variance,
    retired,
    budget,
):
    """Evaluate one candidate basis; return a record or None if infeasible."""
    basis_vectors = [candidate_by_key[key].coefficients for key in chosen]
    solved = solve_all(
        [query.coefficients for query in request.queries], basis_vectors, chosen
    )
    if solved is None:
        return None
    weights = {}
    lineages = {}
    for query, solution in zip(request.queries, solved):
        weights[query.id] = solution
        lineage = merge_lineage(
            [
                (candidate_by_key[key].lineage, solution[key])
                for key in chosen
            ]
        )
        if any(raw_id in retired for raw_id, _ in lineage):
            return None
        lineages[query.id] = lineage

    new_ids = tuple(
        sorted(
            candidate_by_key[key].fresh_id
            for key in chosen
            if candidate_by_key[key].is_new
        )
    )
    new_epsilon = sum(
        (candidate_by_key["fresh:" + raw_id].epsilon for raw_id in new_ids),
        Fraction(0),
    )
    new_delta = sum(
        (candidate_by_key["fresh:" + raw_id].delta for raw_id in new_ids),
        Fraction(0),
    )
    if budget.spent_epsilon + new_epsilon > budget.max_epsilon:
        return None
    if budget.spent_delta + new_delta > budget.max_delta:
        return None

    # Checks only need the quadratic form, which is far cheaper than the full
    # covariance matrix.  The matrix itself is materialised later for the
    # frontier records alone (see ``with_covariance``).
    check_variances = []
    for check in request.checks:
        combined = {}
        for query_id, weight in check.weights.items():
            if weight == 0:
                continue
            for raw_id, coefficient in lineages[query_id]:
                combined[raw_id] = combined.get(raw_id, Fraction(0)) + weight * coefficient
        total = Fraction(0)
        for raw_id, coefficient in combined.items():
            if coefficient != 0:
                total += coefficient * coefficient * measurement_variance[raw_id]
        if total > check.max_variance:
            return None
        check_variances.append(total)

    return PlanRecord(
        basis=tuple(chosen),
        new_ids=new_ids,
        new_epsilon=new_epsilon,
        new_delta=new_delta,
        weights=weights,
        lineages=lineages,
        covariance=(),
        check_variances=tuple(check_variances),
    )


def with_covariance(record, request, gram, position_by_key):
    """Materialise the joint covariance for one selected plan record."""
    positions = [position_by_key[key] for key in record.basis]
    covariance = []
    for left in request.queries:
        left_weights = record.weights[left.id]
        row = []
        for right in request.queries:
            right_weights = record.weights[right.id]
            total = Fraction(0)
            for a_index, a_key in enumerate(record.basis):
                left_value = left_weights[a_key]
                if left_value == 0:
                    continue
                gram_row = gram[positions[a_index]]
                for b_index, b_key in enumerate(record.basis):
                    right_value = right_weights[b_key]
                    if right_value == 0:
                        continue
                    total += left_value * right_value * gram_row[positions[b_index]]
            row.append(total)
        covariance.append(tuple(row))
    return PlanRecord(
        basis=record.basis,
        new_ids=record.new_ids,
        new_epsilon=record.new_epsilon,
        new_delta=record.new_delta,
        weights=record.weights,
        lineages=record.lineages,
        covariance=tuple(covariance),
        check_variances=record.check_variances,
        choice_id=record.choice_id,
    )


def _dominates(left, right):
    left_metrics = left.metrics()
    right_metrics = right.metrics()
    no_worse = all(a <= b for a, b in zip(left_metrics, right_metrics))
    strictly_better = any(a < b for a, b in zip(left_metrics, right_metrics))
    return no_worse and strictly_better


def plan_frontier(request, view, budget):
    candidates = resolve_candidates(request, view)
    if budget is None:
        raise ReleaseValidationError("scope has no declared budget")
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    # Variance comes from the trusted declaration for every raw measurement
    # reachable through this request (already-published or still fresh).
    variance_by_id = {
        ident: measurement.variance for ident, measurement in view.measurements.items()
    }
    for item in request.fresh:
        variance_by_id[item.id] = item.variance

    records = []
    order = [candidate.key for candidate in candidates]
    width = len(request.dimensions)

    def search(start, chosen, span):
        record = _evaluate(
            request,
            tuple(chosen),
            candidate_by_key,
            variance_by_id,
            view.retired,
            budget,
        )
        if record is not None:
            records.append(record)
        # Only independent bases are legal; once the span is saturated no
        # superset can be independent either, which prunes the enumeration.
        if len(span.rows) == width:
            return
        for position in range(start, len(order)):
            key = order[position]
            extended = span.clone()
            if not extended.add(candidate_by_key[key].coefficients):
                continue
            chosen.append(key)
            search(position + 1, chosen, extended)
            chosen.pop()

    search(0, [], Span(width))

    if not records:
        raise NoFeasibleBasisError("no feasible basis satisfies the request")

    # Deduplicate equal metric vectors to the lexicographically smallest
    # (new raw ID count, basis size, sorted basis keys) representative.
    groups = {}
    for record in records:
        groups.setdefault(record.metrics(), []).append(record)
    kept = []
    for records_with_metrics in groups.values():
        kept.append(min(records_with_metrics, key=lambda record: record.dedupe_key()))

    gram = noise_gram(candidates, variance_by_id)
    position_by_key = {key: index for index, key in enumerate(order)}
    frontier = []
    ordered = sorted(kept, key=lambda record: record.metrics())
    for index, record in enumerate(ordered):
        # ``ordered`` is ascending in every metric, so a dominator must appear
        # earlier; any later record is worse on some coordinate.
        if any(_dominates(other, record) for other in ordered[:index]):
            continue
        frontier.append(
            with_covariance(
                PlanRecord(
                    basis=record.basis,
                    new_ids=record.new_ids,
                    new_epsilon=record.new_epsilon,
                    new_delta=record.new_delta,
                    weights=record.weights,
                    lineages=record.lineages,
                    covariance=(),
                    check_variances=record.check_variances,
                    choice_id=choice_digest(request.request_sha256, record.basis),
                ),
                request,
                gram,
                position_by_key,
            )
        )
    frontier.sort(key=lambda record: (record.metrics(), record.basis))
    return frontier, candidates
