"""Offline organisation-level privacy-budget policy compiler.

The compiler takes a finite catalog of scopes, each with its configured
non-negative epsilon/delta allowance, together with organisation-level cap
rules whose dataset/principal selectors are either an exact identifier or the
``"*"`` wildcard.  It is deliberately free of I/O and never creates releases,
measurements or reservations: the v1 one-shot verification and the v2 joint
release planner are untouched.

Two independent governance obligations are enforced:

1. *Per-scope truncation.* A scope's effective allowance is the pairwise
   minimum of its configured allowance and the epsilon/delta caps of every
   rule that matches it.  A wildcard rule is always a hard cap; an exact rule
   can only match the single named scope and can never relax the wildcard.
   Every rule whose cap reaches the minimum is retained as a source, so
   equal-value ties are never collapsed to one winner.
2. *Overlap aggregation.* Each rule caps the *sum* of the effective
   allowances of all scopes it matches, separately for epsilon and delta.
   Passing every scope individually does not imply the aggregate passes;
   when overlapping rules interact, a violation is reported with the
   offending rule and its scope details rather than silently trimming an
   allowance or ignoring a rule.

All arithmetic is :class:`fractions.Fraction`.  Every rendered collection is
sorted, so input ordering and JSON object key ordering cannot change the
normalised report or its SHA256.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json

from .common import require

SCHEMA = "privacy-budget-policy-report-v1"
WILDCARD = "*"
MAX_ABS = 10 ** 12
MAX_SCOPES = 1000
MAX_RULES = 1000
IDENT_LIMIT = 128


class PolicyError(ValueError):
    """The policy document violates the strict contract."""


@dataclass(frozen=True)
class Scope:
    dataset: str
    principal: str
    epsilon: Fraction
    delta: Fraction


@dataclass(frozen=True)
class Rule:
    id: str
    dataset_selector: str
    principal_selector: str
    epsilon: Fraction
    delta: Fraction


# ---------------------------------------------------------------------------
# strict scalar parsing


def _ident(value, label):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > IDENT_LIMIT
        or any(ord(c) < 32 for c in value)
    ):
        raise PolicyError("expected nonblank identifier <=128 chars: " + label)
    return value


def _selector(value, label, known):
    """A selector is an exact identifier from the catalog or ``"*"``."""
    _ident(value, label)
    if value == WILDCARD:
        return value
    if value not in known:
        raise PolicyError("selector references unknown " + label + ": " + value)
    return value


def _int_field(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PolicyError("integer outside allowed bounds: " + label)
    return value


def _rational(value, label):
    require(value, "numerator denominator")
    numerator = _int_field(value["numerator"], 0, MAX_ABS, label + ".numerator")
    denominator = _int_field(value["denominator"], 1, MAX_ABS, label + ".denominator")
    return Fraction(numerator, denominator)


def _bounded_array(value, limit, label, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise PolicyError(
            "expected array of length %d..%d: %s" % (minimum, limit, label)
        )
    return value


def parse_policy(data):
    """Validate the strict contract and return ``(scopes, rules)`` tuples.

    Rejects undefined fields, duplicate scope keys, duplicate rule IDs,
    missing/illegal values, selectors that name unknown datasets/principals,
    and negative or non-finite numbers.  Duplicate JSON keys are rejected
    earlier by the strict loader in :mod:`privacy_budget.common`.
    """
    require(data, "scopes rules")
    scopes = []
    scope_keys = set()
    datasets = set()
    principals = set()
    for index, item in enumerate(_bounded_array(data["scopes"], MAX_SCOPES, "scopes", 1)):
        require(item, "dataset principal epsilon delta")
        dataset = _ident(item["dataset"], "scopes[%d].dataset" % index)
        principal = _ident(item["principal"], "scopes[%d].principal" % index)
        key = (dataset, principal)
        if key in scope_keys:
            raise PolicyError(
                "duplicate scope key: %s/%s" % (dataset, principal)
            )
        scope_keys.add(key)
        datasets.add(dataset)
        principals.add(principal)
        scopes.append(
            Scope(
                dataset=dataset,
                principal=principal,
                epsilon=_rational(item["epsilon"], "scopes[%d].epsilon" % index),
                delta=_rational(item["delta"], "scopes[%d].delta" % index),
            )
        )

    rules = []
    rule_ids = set()
    for index, item in enumerate(_bounded_array(data["rules"], MAX_RULES, "rules")):
        require(item, "id dataset_selector principal_selector epsilon delta")
        rule_id = _ident(item["id"], "rules[%d].id" % index)
        if rule_id in rule_ids:
            raise PolicyError("duplicate rule id: " + rule_id)
        rule_ids.add(rule_id)
        rules.append(
            Rule(
                id=rule_id,
                dataset_selector=_selector(
                    item["dataset_selector"],
                    "rules[%d].dataset_selector" % index,
                    datasets,
                ),
                principal_selector=_selector(
                    item["principal_selector"],
                    "rules[%d].principal_selector" % index,
                    principals,
                ),
                epsilon=_rational(item["epsilon"], "rules[%d].epsilon" % index),
                delta=_rational(item["delta"], "rules[%d].delta" % index),
            )
        )
    return tuple(scopes), tuple(rules)


# ---------------------------------------------------------------------------
# matching and compilation


def matches(rule, scope):
    """Selectors are exact values or ``"*"``; nothing else is permitted."""
    return (
        rule.dataset_selector in (WILDCARD, scope.dataset)
        and rule.principal_selector in (WILDCARD, scope.principal)
    )


def _fraction(value):
    return str(Fraction(value))


def _scope_payload(scope, matched, effective_epsilon, effective_delta):
    """Render one scope with every rule that reaches each minimum."""

    def sources(attribute, value):
        # A rule is a source when its cap reaches the resulting minimum;
        # equal-value ties are retained in full and only matching rules
        # qualify.  When no matching rule binds this coordinate the
        # configured allowance itself is the sole source of the minimum.
        ids = sorted(
            rule.id for rule in matched if getattr(rule, attribute) == value
        )
        return ids if ids else ["configured"]

    return {
        "dataset": scope.dataset,
        "principal": scope.principal,
        "configured": {
            "epsilon": _fraction(scope.epsilon),
            "delta": _fraction(scope.delta),
        },
        "effective": {
            "epsilon": _fraction(effective_epsilon),
            "delta": _fraction(effective_delta),
        },
        "epsilon_sources": sources("epsilon", effective_epsilon),
        "delta_sources": sources("delta", effective_delta),
        "matching_rule_ids": sorted(rule.id for rule in matched),
    }


def compile_policy(scopes, rules, source_sha256):
    """Compile parsed scopes/rules into the auditable report structure.

    Returns a plain JSON-ready dict whose ``status`` is ``"ok"`` or
    ``"violation"``.  A violation never truncates an allowance and never
    drops a rule: every offending rule and every scope contributing to its
    aggregate excess is listed.
    """
    scope_records = []
    effective = {}
    for scope in scopes:
        matched = tuple(rule for rule in rules if matches(rule, scope))
        if matched:
            effective_epsilon = min(
                [scope.epsilon] + [rule.epsilon for rule in matched]
            )
            effective_delta = min([scope.delta] + [rule.delta for rule in matched])
        else:
            effective_epsilon, effective_delta = scope.epsilon, scope.delta
        effective[scope] = (effective_epsilon, effective_delta)
        scope_records.append(
            _scope_payload(scope, matched, effective_epsilon, effective_delta)
        )
    scope_records.sort(key=lambda item: (item["dataset"], item["principal"]))

    # A selector may only use names present in the catalog, but an exact
    # dataset/principal pair can still name a combination no scope owns.
    # Such a rule matches nothing and the whole policy is rejected rather
    # than silently carrying a dead cap.
    unmatched = sorted(
        rule.id
        for rule in rules
        if not any(matches(rule, scope) for scope in scopes)
    )
    if unmatched:
        raise PolicyError("rule matches no scope: " + ", ".join(unmatched))

    rule_checks = []
    violations = []
    for rule in rules:
        hit = tuple(scope for scope in scopes if matches(rule, scope))
        total_epsilon = sum((effective[scope][0] for scope in hit), Fraction(0))
        total_delta = sum((effective[scope][1] for scope in hit), Fraction(0))
        # Unmatched rules were rejected above, so ``hit`` is always non-empty.
        excess_epsilon = max(Fraction(0), total_epsilon - rule.epsilon)
        excess_delta = max(Fraction(0), total_delta - rule.delta)
        details = [
            {
                "dataset": scope.dataset,
                "principal": scope.principal,
                "effective_epsilon": _fraction(effective[scope][0]),
                "effective_delta": _fraction(effective[scope][1]),
            }
            for scope in sorted(hit, key=lambda scope: (scope.dataset, scope.principal))
        ]
        check = {
            "rule_id": rule.id,
            "dataset_selector": rule.dataset_selector,
            "principal_selector": rule.principal_selector,
            "matched_scopes": [
                {"dataset": item["dataset"], "principal": item["principal"]}
                for item in details
            ],
            "matched_scope_count": len(hit),
            "epsilon_cap": _fraction(rule.epsilon),
            "delta_cap": _fraction(rule.delta),
            "epsilon_sum": _fraction(total_epsilon),
            "delta_sum": _fraction(total_delta),
            "epsilon_excess": _fraction(excess_epsilon),
            "delta_excess": _fraction(excess_delta),
        }
        rule_checks.append(check)
        if excess_epsilon > 0 or excess_delta > 0:
            violations.append(
                {
                    "rule_id": rule.id,
                    "dataset_selector": rule.dataset_selector,
                    "principal_selector": rule.principal_selector,
                    "epsilon_cap": _fraction(rule.epsilon),
                    "delta_cap": _fraction(rule.delta),
                    "epsilon_sum": _fraction(total_epsilon),
                    "delta_sum": _fraction(total_delta),
                    "epsilon_excess": _fraction(excess_epsilon),
                    "delta_excess": _fraction(excess_delta),
                    "scopes": details,
                }
            )
    rule_checks.sort(key=lambda item: item["rule_id"])
    violations.sort(key=lambda item: item["rule_id"])

    body = {
        "schema": SCHEMA,
        "source_sha256": source_sha256,
        "status": "violation" if violations else "ok",
        "scopes": scope_records,
        "rule_checks": rule_checks,
        # Reaching this point means every rule matched at least one scope;
        # the field is retained in the schema so consumers see the guarantee.
        "unmatched_rule_ids": [],
        "violations": violations,
    }
    body["report_sha256"] = report_digest(body)
    return body


def report_digest(body):
    """SHA256 of the canonical report, excluding the digest fields."""
    canonical = {
        key: value
        for key, value in body.items()
        if key not in ("report_sha256",)
    }
    encoded = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def violation_message(body):
    """Deterministic human-readable summary of every aggregate violation."""
    parts = []
    for violation in body["violations"]:
        coordinates = []
        if Fraction(violation["epsilon_excess"]) > 0:
            coordinates.append(
                "epsilon sum %s exceeds cap %s by %s"
                % (
                    violation["epsilon_sum"],
                    violation["epsilon_cap"],
                    violation["epsilon_excess"],
                )
            )
        if Fraction(violation["delta_excess"]) > 0:
            coordinates.append(
                "delta sum %s exceeds cap %s by %s"
                % (
                    violation["delta_sum"],
                    violation["delta_cap"],
                    violation["delta_excess"],
                )
            )
        scopes = ", ".join(
            "%s/%s" % (item["dataset"], item["principal"])
            for item in violation["scopes"]
        )
        parts.append(
            "rule %s: %s; scopes: %s" % (violation["rule_id"], "; ".join(coordinates), scopes)
        )
    return "organisation policy aggregate violation: " + " | ".join(parts)
