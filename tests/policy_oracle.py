"""Independent small-scale oracle for the offline policy compiler.

This is a deliberately separate, naive re-implementation of selector
matching, per-scope minima, equal-value source tracking and aggregate cap
checks.  It works directly on plain JSON-style payloads (rational values as
``{"numerator", "denominator"}``) so that it never imports the code under
test; cross-checking it against :mod:`privacy_budget.policy_model` gives a
second opinion on every governance decision.
"""
from fractions import Fraction


def _q(value):
    return Fraction(value["numerator"], value["denominator"])


def _hit(rule, scope):
    """Exact-or-wildcard matching, written independently of the package."""
    ds = rule["dataset_selector"]
    ps = rule["principal_selector"]
    if ds != "*" and ds != scope["dataset"]:
        return False
    if ps != "*" and ps != scope["principal"]:
        return False
    return True


def reference(payload):
    """Return ``(status, scopes, checks)`` computed by brute force.

    ``scopes`` maps ``(dataset, principal)`` to configured/effective rationals
    and source ID lists; ``checks`` maps rule id to its aggregate totals and
    excess.  Selector/duplicate validation is assumed to have run already.
    """
    scopes = payload["scopes"]
    rules = payload["rules"]

    effective = {}
    scope_view = {}
    for scope in scopes:
        key = (scope["dataset"], scope["principal"])
        cfg_e, cfg_d = _q(scope["epsilon"]), _q(scope["delta"])
        matched = [r for r in rules if _hit(r, scope)]
        e_candidates = [("configured", cfg_e)]
        d_candidates = [("configured", cfg_d)]
        for r in matched:
            e_candidates.append((r["id"], _q(r["epsilon"])))
            d_candidates.append((r["id"], _q(r["delta"])))
        eff_e = min(value for _, value in e_candidates)
        eff_d = min(value for _, value in d_candidates)
        e_sources = sorted(
            name for name, value in e_candidates
            if value == eff_e and not (name == "configured" and
                                       any(v == eff_e for n, v in e_candidates
                                           if n != "configured"))
        )
        d_sources = sorted(
            name for name, value in d_candidates
            if value == eff_d and not (name == "configured" and
                                       any(v == eff_d for n, v in d_candidates
                                           if n != "configured"))
        )
        effective[key] = (eff_e, eff_d)
        scope_view[key] = {
            "configured": (cfg_e, cfg_d),
            "effective": (eff_e, eff_d),
            "epsilon_sources": e_sources,
            "delta_sources": d_sources,
            "matching_rule_ids": sorted(r["id"] for r in matched),
        }

    checks = {}
    violations = []
    unmatched = []
    for r in rules:
        hit = [s for s in scopes if _hit(r, s)]
        if not hit:
            unmatched.append(r["id"])
        sum_e = sum((effective[(s["dataset"], s["principal"])][0]
                     for s in hit), Fraction(0))
        sum_d = sum((effective[(s["dataset"], s["principal"])][1]
                     for s in hit), Fraction(0))
        cap_e, cap_d = _q(r["epsilon"]), _q(r["delta"])
        excess_e = max(Fraction(0), sum_e - cap_e)
        excess_d = max(Fraction(0), sum_d - cap_d)
        checks[r["id"]] = {
            "matched": sorted((s["dataset"], s["principal"]) for s in hit),
            "epsilon_sum": sum_e,
            "delta_sum": sum_d,
            "epsilon_excess": excess_e,
            "delta_excess": excess_d,
        }
        if excess_e > 0 or excess_d > 0:
            violations.append(r["id"])

    status = "violation" if violations else "ok"
    return status, scope_view, checks, sorted(violations), sorted(unmatched)


def all_selector_patterns(datasets, principals):
    """Every legal selector pair over the given catalog names."""
    d_choices = ["*"] + sorted(datasets)
    p_choices = ["*"] + sorted(principals)
    return [(d, p) for d in d_choices for p in p_choices]
