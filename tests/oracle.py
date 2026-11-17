"""Independent small-scale oracle for the joint release planner.

This module intentionally re-implements ranking, span reconstruction, lineage
algebra, the covariance quadratic form and Pareto filtering from scratch so
that the planner can be compared against a second opinion.  It deliberately
uses a different elimination style (bareiss-free, reverse substitution) and
a naive subset enumeration.
"""
from fractions import Fraction
from itertools import combinations


def _rref(vectors):
    """Row reduce a list of vectors returning (reduced rows, pivot columns)."""
    rows = [[Fraction(value) for value in vector] for vector in vectors]
    width = max((len(row) for row in rows), default=0)
    for row in rows:
        row.extend([Fraction(0)] * (width - len(row)))
    pivots = []
    r = 0
    for c in range(width):
        chosen = None
        for i in range(r, len(rows)):
            if rows[i][c] != 0:
                chosen = i
                break
        if chosen is None:
            continue
        rows[r], rows[chosen] = rows[chosen], rows[r]
        pivot = rows[r][c]
        rows[r] = [value / pivot for value in rows[r]]
        for i in range(len(rows)):
            if i != r and rows[i][c] != 0:
                factor = rows[i][c]
                rows[i] = [a - factor * b for a, b in zip(rows[i], rows[r])]
        pivots.append(c)
        r += 1
    return rows[:r], pivots


def independent(vectors):
    rows, pivots = _rref(vectors)
    return len(rows) == len(vectors)


def expand(target, basis):
    """Reconstruct ``target`` on ``basis`` using exact arithmetic.

    A redundant basis is reduced to an independent spanning subset first, so
    the returned weights are still a valid (if non-unique) reconstruction;
    outside the span the return value is ``None``.
    """
    n = len(basis)
    if n == 0:
        return {} if all(value == 0 for value in target) else None
    if not independent(basis):
        for size in range(n - 1, -1, -1):
            for subset in combinations(range(n), size):
                reduced = [basis[i] for i in subset]
                if not independent(reduced):
                    continue
                solution = expand(target, reduced)
                if solution is not None:
                    full = {index: Fraction(0) for index in range(n)}
                    for position, index in enumerate(subset):
                        full[index] = solution[position]
                    return full
        return None
    width = max(len(target), max((len(v) for v in basis), default=0))
    pad = lambda v: list(v) + [Fraction(0)] * (width - len(v))
    target = pad(target)
    basis = [pad(v) for v in basis]
    # Solve the overdetermined system by Gaussian elimination on the
    # augmented coordinate matrix (columns = unknowns).
    a = [[basis[j][i] for j in range(n)] + [target[i]] for i in range(width)]
    pivot_row = 0
    for column in range(n):
        pick = None
        for i in range(pivot_row, width):
            if a[i][column] != 0:
                pick = i
                break
        if pick is None:
            return None
        a[pivot_row], a[pick] = a[pick], a[pivot_row]
        scale = a[pivot_row][column]
        a[pivot_row] = [value / scale for value in a[pivot_row]]
        for i in range(width):
            if i != pivot_row and a[i][column] != 0:
                factor = a[i][column]
                a[i] = [x - factor * y for x, y in zip(a[i], a[pivot_row])]
        pivot_row += 1
    for i in range(width):
        if all(a[i][c] == 0 for c in range(n)) and a[i][n] != 0:
            return None
    solution = [Fraction(0)] * n
    for i in range(pivot_row):
        for c in range(n):
            if a[i][c] != 0:
                solution[c] = a[i][n]
                break
    return solution


def merge(terms):
    result = {}
    for lineage, weight in terms:
        if weight == 0:
            continue
        for raw_id, coefficient in lineage.items():
            result[raw_id] = result.get(raw_id, Fraction(0)) + weight * coefficient
    return {raw_id: value for raw_id, value in result.items() if value != 0}


def frontier(candidates, queries, checks, variance, retired, budget, epsilon, delta):
    """Enumerate every basis; return the expected Pareto frontier.

    ``candidates`` is a list of dicts with keys ``key``, ``vector``,
    ``lineage``, ``new_id`` (raw ID if unpublished else ``None``),
    ``epsilon`` and ``delta``.
    """
    results = []
    keys = [c["key"] for c in candidates]
    by_key = {c["key"]: c for c in candidates}
    for size in range(len(keys) + 1):
        for subset in combinations(keys, size):
            basis = [by_key[key] for key in subset]
            if not independent([c["vector"] for c in basis]):
                continue
            weight_maps = {}
            lineages = {}
            ok = True
            for q in queries:
                solution = expand(q["vector"], [c["vector"] for c in basis])
                if solution is None:
                    ok = False
                    break
                weight_maps[q["id"]] = dict(zip(subset, solution))
                lineages[q["id"]] = merge(
                    [(by_key[key]["lineage"], weight) for key, weight in zip(subset, solution)]
                )
                if any(raw_id in retired for raw_id in lineages[q["id"]]):
                    ok = False
                    break
            if not ok:
                continue
            new_ids = sorted(
                by_key[key]["new_id"] for key in subset if by_key[key]["new_id"] is not None
            )
            new_epsilon = sum((by_key["fresh:" + r]["epsilon"] for r in new_ids), Fraction(0))
            new_delta = sum((by_key["fresh:" + r]["delta"] for r in new_ids), Fraction(0))
            if new_epsilon + budget[0] > epsilon or new_delta + budget[1] > delta:
                continue
            query_ids = [q["id"] for q in queries]
            covariance = []
            for a in query_ids:
                row = []
                for b in query_ids:
                    total = Fraction(0)
                    for raw_id, coefficient in lineages[a].items():
                        other = lineages[b].get(raw_id)
                        if other is not None:
                            total += coefficient * other * variance[raw_id]
                    row.append(total)
                covariance.append(row)
            variances = []
            feasible = True
            for ck in checks:
                total = Fraction(0)
                for a, wa in ck["weights"].items():
                    for b, wb in ck["weights"].items():
                        total += wa * wb * covariance[query_ids.index(a)][query_ids.index(b)]
                if total > ck["max_variance"]:
                    feasible = False
                    break
                variances.append(total)
            if not feasible:
                continue
            results.append(
                {
                    "basis": tuple(subset),
                    "new_ids": tuple(new_ids),
                    "metrics": (new_epsilon, new_delta, *variances),
                    "covariance": covariance,
                }
            )
    # Keep the smallest representative per identical metric vector.
    groups = {}
    for item in results:
        groups.setdefault(item["metrics"], []).append(item)
    kept = []
    for group in groups.values():
        kept.append(min(group, key=lambda item: (len(item["new_ids"]), len(item["basis"]), item["basis"])))
    pareto = []
    for item in kept:
        dominated = False
        for other in kept:
            if other is item:
                continue
            no_worse = all(a <= b for a, b in zip(other["metrics"], item["metrics"]))
            strict = any(a < b for a, b in zip(other["metrics"], item["metrics"]))
            if no_worse and strict:
                dominated = True
                break
        if not dominated:
            pareto.append(item)
    pareto.sort(key=lambda item: (item["metrics"], item["basis"]))
    return pareto
