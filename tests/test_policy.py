"""Acceptance tests for the offline organisation policy compiler.

Covers selector parsing, equal-value source tracking, per-scope effective
allowances, overlapping aggregate constraints, the linked wildcard scenario,
report stability and the v1-compatible CLI safety contract.
"""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import support
from support import frac

from privacy_budget.policy_model import (
    PolicyError,
    compile_policy,
    parse_policy,
    report_digest,
)

ROOT = support.ROOT


def scope(dataset, principal, epsilon, delta=0):
    return {
        "dataset": dataset,
        "principal": principal,
        "epsilon": frac(*epsilon) if isinstance(epsilon, tuple) else frac(epsilon),
        "delta": frac(*delta) if isinstance(delta, tuple) else frac(delta),
    }


def rule(rule_id, dataset_selector, principal_selector, epsilon, delta=0):
    return {
        "id": rule_id,
        "dataset_selector": dataset_selector,
        "principal_selector": principal_selector,
        "epsilon": frac(*epsilon) if isinstance(epsilon, tuple) else frac(epsilon),
        "delta": frac(*delta) if isinstance(delta, tuple) else frac(delta),
    }


def policy(scopes=(), rules=()):
    return {"scopes": list(scopes), "rules": list(rules)}


def compile_payload(payload):
    scopes, rules = parse_policy(payload)
    return compile_policy(scopes, rules, "0" * 64)


class SelectorParsing(unittest.TestCase):
    def setUp(self):
        self.base_payload = policy(
            scopes=[scope("d1", "p1", 1), scope("d2", "p2", 1)],
            rules=[rule("r1", "d1", "*", 2), rule("r2", "*", "p2", 2)],
        )

    def test_exact_and_wildcard_selectors_match(self):
        report = compile_payload(self.base_payload)
        checks = {item["rule_id"]: item for item in report["rule_checks"]}
        self.assertEqual(
            [s["dataset"] for s in checks["r1"]["matched_scopes"]], ["d1"]
        )
        self.assertEqual(
            [s["dataset"] for s in checks["r2"]["matched_scopes"]], ["d2"]
        )

    def test_unknown_dataset_selector_rejected(self):
        payload = copy.deepcopy(self.base_payload)
        payload["rules"][0]["dataset_selector"] = "missing"
        with self.assertRaises(PolicyError) as caught:
            parse_policy(payload)
        self.assertIn("unknown", str(caught.exception))

    def test_unknown_principal_selector_rejected(self):
        payload = copy.deepcopy(self.base_payload)
        payload["rules"][1]["principal_selector"] = "nobody"
        with self.assertRaises(PolicyError) as caught:
            parse_policy(payload)
        self.assertIn("unknown", str(caught.exception))

    def test_blank_and_non_string_selectors_rejected(self):
        for bad in ("", " ", 3, None):
            payload = copy.deepcopy(self.base_payload)
            payload["rules"][0]["dataset_selector"] = bad
            with self.assertRaises(PolicyError):
                parse_policy(payload)

    def test_duplicate_scope_key_rejected(self):
        payload = policy(
            scopes=[scope("d1", "p1", 1), scope("d1", "p1", 2)],
        )
        with self.assertRaises(PolicyError) as caught:
            parse_policy(payload)
        self.assertIn("duplicate scope", str(caught.exception))

    def test_duplicate_rule_id_rejected(self):
        payload = policy(
            scopes=[scope("d1", "p1", 1)],
            rules=[rule("same", "*", "*", 1), rule("same", "d1", "p1", 1)],
        )
        with self.assertRaises(PolicyError) as caught:
            parse_policy(payload)
        self.assertIn("duplicate rule", str(caught.exception))

    def test_missing_budget_rejected(self):
        payload = self.base_payload
        del payload["scopes"][0]["epsilon"]
        with self.assertRaises((PolicyError, ValueError, KeyError)):
            parse_policy(payload)

    def test_negative_allowance_rejected(self):
        payload = copy.deepcopy(self.base_payload)
        payload["scopes"][0]["epsilon"] = frac(-1)
        with self.assertRaises(PolicyError):
            parse_policy(payload)

    def test_zero_allowance_allowed(self):
        payload = policy(scopes=[scope("d", "p", 0, 0)])
        report = compile_payload(payload)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["scopes"][0]["effective"],
                         {"epsilon": "0", "delta": "0"})

    def test_undefined_field_rejected(self):
        payload = copy.deepcopy(self.base_payload)
        payload["scopes"][0]["extra"] = 1
        with self.assertRaises(ValueError):
            parse_policy(payload)

    def test_empty_scopes_rejected(self):
        with self.assertRaises(PolicyError):
            parse_policy(policy(scopes=[]))

    def test_rule_matching_no_scope_rejected_at_compilation(self):
        # Both selector names exist in the catalog, but never co-occur.
        payload = policy(
            scopes=[scope("d1", "p1", 1), scope("d2", "p2", 1)],
            rules=[rule("orphan", "d1", "p2", 5)],
        )
        scopes, rules = parse_policy(payload)
        with self.assertRaises(PolicyError) as caught:
            compile_policy(scopes, rules, "0" * 64)
        self.assertIn("orphan", str(caught.exception))

    def test_malformed_rational_rejected(self):
        payload = policy(scopes=[scope("d", "p", 1)])
        payload["scopes"][0]["delta"] = {"numerator": 1, "denominator": 0}
        with self.assertRaises(PolicyError):
            parse_policy(payload)


class EffectiveAllowances(unittest.TestCase):
    def test_effective_is_pairwise_minimum(self):
        payload = policy(
            scopes=[scope("d", "p", 4, (3, 2))],
            rules=[rule("cap", "d", "p", 5, 1)],
        )
        report = compile_payload(payload)
        record = report["scopes"][0]
        # epsilon: min(4, 5) = 4 (configured binds); delta: min(3/2, 1) = 1.
        self.assertEqual(record["effective"], {"epsilon": "4", "delta": "1"})
        self.assertEqual(record["epsilon_sources"], ["configured"])
        self.assertEqual(record["delta_sources"], ["cap"])

    def test_wildcard_is_hard_cap_exact_rule_cannot_relax(self):
        payload = policy(
            scopes=[scope("d", "p", 6)],
            rules=[
                rule("wild", "*", "*", 4),
                rule("loose-exact", "d", "p", 9),
            ],
        )
        report = compile_payload(payload)
        record = report["scopes"][0]
        self.assertEqual(record["effective"]["epsilon"], "4")
        self.assertEqual(record["epsilon_sources"], ["wild"])
        self.assertNotIn("loose-exact", record["epsilon_sources"])

    def test_equal_value_tie_keeps_all_sources(self):
        payload = policy(
            scopes=[scope("d", "p", 4, (1, 20))],
            rules=[
                rule("wild", "*", "*", 4, (1, 20)),
                rule("exact", "d", "p", 4, (1, 20)),
            ],
        )
        report = compile_payload(payload)
        record = report["scopes"][0]
        self.assertEqual(record["epsilon_sources"], ["exact", "wild"])
        self.assertEqual(record["delta_sources"], ["exact", "wild"])

    def test_scope_without_matching_rule_keeps_configured(self):
        payload = policy(
            scopes=[scope("d1", "p", 3), scope("d2", "p", 2)],
            rules=[rule("only-d1", "d1", "p", 1)],
        )
        report = compile_payload(payload)
        records = {(s["dataset"]): s for s in report["scopes"]}
        self.assertEqual(records["d1"]["effective"]["epsilon"], "1")
        self.assertEqual(records["d1"]["epsilon_sources"], ["only-d1"])
        self.assertEqual(records["d2"]["effective"]["epsilon"], "2")
        self.assertEqual(records["d2"]["epsilon_sources"], ["configured"])
        self.assertEqual(records["d2"]["matching_rule_ids"], [])

    def test_separate_epsilon_and_delta_minima(self):
        payload = policy(
            scopes=[scope("d", "p", 4, 4)],
            rules=[
                rule("e-cap", "d", "p", 2, 9),
                rule("d-cap", "d", "p", 9, 1),
            ],
        )
        record = compile_payload(payload)["scopes"][0]
        self.assertEqual(record["effective"], {"epsilon": "2", "delta": "1"})
        self.assertEqual(record["epsilon_sources"], ["e-cap"])
        self.assertEqual(record["delta_sources"], ["d-cap"])


class OverlapAggregation(unittest.TestCase):
    def test_linked_wildcard_scenario_fails_on_aggregate(self):
        payload = policy(
            scopes=[
                scope("dataset-a", "analyst-a", 4, (1, 20)),
                scope("dataset-b", "analyst-a", 3, (1, 20)),
            ],
            rules=[rule("org-wildcard", "*", "*", 5, (1, 10))],
        )
        report = compile_payload(payload)
        # Each scope is individually below 5; only the aggregate (4+3=7) fails.
        self.assertEqual(report["status"], "violation")
        check = report["rule_checks"][0]
        self.assertEqual(Fraction(check["epsilon_sum"]), Fraction(7))
        self.assertEqual(Fraction(check["epsilon_excess"]), Fraction(2))
        self.assertEqual(Fraction(check["delta_sum"]), Fraction(1, 10))
        self.assertEqual(Fraction(check["delta_excess"]), Fraction(0))
        violation = report["violations"][0]
        self.assertEqual(violation["rule_id"], "org-wildcard")
        self.assertEqual(
            sorted((s["dataset"], s["principal"]) for s in violation["scopes"]),
            [("dataset-a", "analyst-a"), ("dataset-b", "analyst-a")],
        )

    def test_linked_scenario_passes_after_reducing_b(self):
        payload = policy(
            scopes=[
                scope("dataset-a", "analyst-a", 4, (1, 20)),
                scope("dataset-b", "analyst-a", 1, (1, 20)),
            ],
            rules=[rule("org-wildcard", "*", "*", 5, (1, 10))],
        )
        report = compile_payload(payload)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["violations"], [])
        check = report["rule_checks"][0]
        self.assertEqual(Fraction(check["epsilon_sum"]), Fraction(5))
        self.assertEqual(Fraction(check["delta_sum"]), Fraction(1, 10))

    def test_passing_scope_sources_include_matching_wildcard_and_tied_exact(self):
        payload = policy(
            scopes=[
                scope("dataset-a", "analyst-a", 4, (1, 20)),
                scope("dataset-b", "analyst-a", 1, (1, 20)),
            ],
            rules=[
                rule("org-wildcard", "*", "*", 5, (1, 10)),
                rule("exact-a", "dataset-a", "analyst-a", 4, (1, 20)),
            ],
        )
        report = compile_payload(payload)
        self.assertEqual(report["status"], "ok")
        record_a = next(s for s in report["scopes"] if s["dataset"] == "dataset-a")
        # The effective epsilon 4 is the pairwise minimum of configured 4,
        # the exact cap 4 and the wildcard hard cap 5.  The exact rule ties
        # at the minimum and is retained as a source; the wildcard cap (5)
        # does not reach the minimum but stays a binding hard cap and is
        # always listed among the matching rules.
        self.assertEqual(record_a["effective"]["epsilon"], "4")
        self.assertEqual(record_a["epsilon_sources"], ["exact-a"])
        self.assertEqual(record_a["delta_sources"], ["exact-a"])
        self.assertEqual(
            record_a["matching_rule_ids"], ["exact-a", "org-wildcard"]
        )
        self.assertLessEqual(
            Fraction(record_a["effective"]["epsilon"]), Fraction(5)
        )
        self.assertLessEqual(
            Fraction(record_a["effective"]["delta"]), Fraction(1, 10)
        )

    def test_wildcard_tied_at_minimum_is_kept_as_source(self):
        # When the wildcard cap itself reaches the minimum alongside an exact
        # rule of the same value, both IDs must survive as equal-value sources.
        payload = policy(
            scopes=[scope("d", "p", 4, (1, 20))],
            rules=[
                rule("wild", "*", "*", 4, (1, 20)),
                rule("exact", "d", "p", 4, (1, 20)),
            ],
        )
        record = compile_payload(payload)["scopes"][0]
        self.assertEqual(record["epsilon_sources"], ["exact", "wild"])
        self.assertEqual(record["delta_sources"], ["exact", "wild"])

    def test_overlapping_rules_intersection_violation(self):
        # Rule r-w covers A,B (cap 5 total); rule r-ab covers only A with a
        # generous exact cap.  A=4, B=1 passes both.  Raise B to 3 and the
        # wildcard aggregate fails although every per-scope value is <= 5.
        payload = policy(
            scopes=[
                scope("A", "p", 4),
                scope("B", "p", 3),
            ],
            rules=[
                rule("r-w", "*", "*", 5),
                rule("r-a", "A", "p", 4),
            ],
        )
        report = compile_payload(payload)
        by_id = {c["rule_id"]: c for c in report["rule_checks"]}
        self.assertEqual(Fraction(by_id["r-w"]["epsilon_sum"]), Fraction(7))
        self.assertEqual(Fraction(by_id["r-w"]["epsilon_excess"]), Fraction(2))
        self.assertEqual(Fraction(by_id["r-a"]["epsilon_excess"]), Fraction(0))
        self.assertEqual([v["rule_id"] for v in report["violations"]], ["r-w"])
        offending = report["violations"][0]
        self.assertEqual(
            sorted(s["dataset"] for s in offending["scopes"]), ["A", "B"]
        )

    def test_epsilon_and_delta_violations_reported_separately(self):
        payload = policy(
            scopes=[
                scope("A", "p", 3, (1, 5)),
                scope("B", "p", 3, (1, 5)),
            ],
            rules=[rule("r", "*", "*", 5, (1, 5))],
        )
        report = compile_payload(payload)
        violation = report["violations"][0]
        self.assertEqual(Fraction(violation["epsilon_excess"]), Fraction(1))
        # Two scopes at 1/5 each sum to 2/5 against a 1/5 cap: excess 1/5.
        self.assertEqual(Fraction(violation["delta_excess"]),
                         Fraction(1, 5))

    def test_delta_only_violation(self):
        payload = policy(
            scopes=[
                scope("A", "p", 1, (1, 5)),
                scope("B", "p", 1, (1, 5)),
            ],
            rules=[rule("r", "*", "*", 5, (1, 10))],
        )
        report = compile_payload(payload)
        violation = report["violations"][0]
        self.assertEqual(Fraction(violation["epsilon_excess"]), Fraction(0))
        self.assertGreater(Fraction(violation["delta_excess"]), Fraction(0))

    def test_aggregate_uses_effective_not_configured(self):
        # The exact rule truncates A from 9 to 2; the wildcard aggregate must
        # sum effective values (2 + 2 = 4), never configured ones (9 + 2).
        payload = policy(
            scopes=[scope("A", "p", 9), scope("B", "p", 2)],
            rules=[
                rule("r-a", "A", "p", 2),
                rule("r-w", "*", "*", 5),
            ],
        )
        report = compile_payload(payload)
        self.assertEqual(report["status"], "ok")
        wildcard = next(c for c in report["rule_checks"]
                        if c["rule_id"] == "r-w")
        self.assertEqual(Fraction(wildcard["epsilon_sum"]), Fraction(4))


class ReportStability(unittest.TestCase):
    def payload(self):
        return policy(
            scopes=[
                scope("dataset-b", "analyst-a", 1, (1, 20)),
                scope("dataset-a", "analyst-a", 4, (1, 20)),
            ],
            rules=[
                rule("exact-a", "dataset-a", "analyst-a", 4, (1, 20)),
                rule("org-wildcard", "*", "*", 5, (1, 10)),
            ],
        )

    def test_input_order_does_not_change_report(self):
        first = compile_payload(self.payload())
        shuffled = self.payload()
        shuffled["scopes"].reverse()
        shuffled["rules"].reverse()
        second = compile_payload(shuffled)
        self.assertEqual(first, second)

    def test_report_sha256_covers_full_canonical_body(self):
        report = compile_payload(self.payload())
        self.assertEqual(report["report_sha256"], report_digest(report))
        tampered = copy.deepcopy(report)
        tampered["scopes"][0]["effective"]["epsilon"] = "400"
        self.assertNotEqual(report_digest(tampered), report["report_sha256"])

    def test_digest_is_key_order_independent(self):
        report = compile_payload(self.payload())
        encoded_one = json.dumps(report, sort_keys=True, ensure_ascii=False)
        encoded_two = json.dumps(
            report, sort_keys=False, ensure_ascii=False,
            indent=4, separators=(",", ": "),
        )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    {k: v for k, v in json.loads(encoded_one).items()
                     if k != "report_sha256"},
                    sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            hashlib.sha256(
                json.dumps(
                    {k: v for k, v in json.loads(encoded_two).items()
                     if k != "report_sha256"},
                    sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, payload):
        path = self.base / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_policy_command_passes_and_writes_report(self):
        source = self.write("in.json", policy(
            scopes=[scope("d", "p", 1)],
            rules=[rule("r", "*", "*", 2)],
        ))
        output = self.base / "report.json"
        result = support.run_policy_cli(
            "policy", "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema"], "privacy-budget-policy-report-v1")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(json.loads(result.stdout)["report_sha256"],
                         report["report_sha256"])

    def test_policy_command_rejects_aggregate_violation(self):
        source = ROOT / "fixtures" / "policy-violation.json"
        output = self.base / "report.json"
        result = support.run_policy_cli(
            "policy", "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("aggregate violation", result.stderr)
        self.assertIn("org-wildcard", result.stderr)
        self.assertIn("dataset-a", result.stderr)
        self.assertIn("dataset-b", result.stderr)
        self.assertFalse(output.exists())

    def test_failure_preserves_previous_report(self):
        good = self.write("good.json", policy(scopes=[scope("d", "p", 1)]))
        output = self.base / "report.json"
        first = support.run_policy_cli(
            "policy", "--source", good, "--output", output
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        prior = output.read_bytes()
        bad = self.write("bad.json", policy(
            scopes=[scope("A", "p", 4), scope("B", "p", 3)],
            rules=[rule("r", "*", "*", 5)],
        ))
        second = support.run_policy_cli(
            "policy", "--source", bad, "--output", output
        )
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), prior)

    def test_input_output_same_path_rejected(self):
        source = self.write("in.json", policy(scopes=[scope("d", "p", 1)]))
        result = support.run_policy_cli(
            "policy", "--source", source, "--output", source
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("differ", result.stderr)

    def test_symlink_output_rejected(self):
        source = self.write("in.json", policy(scopes=[scope("d", "p", 1)]))
        target = self.base / "target.json"
        link = self.base / "link.json"
        link.symlink_to(target)
        result = support.run_policy_cli(
            "policy", "--source", source, "--output", link
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        self.assertFalse(target.exists())

    def test_duplicate_json_key_rejected(self):
        source = self.base / "dup.json"
        source.write_text('{"scopes":[],"scopes":[],"rules":[]}')
        result = support.run_policy_cli(
            "policy", "--source", source, "--output", self.base / "o.json"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_legacy_command_contract_unchanged(self):
        source = ROOT / "fixtures" / "input.json"
        output = self.base / "legacy.json"
        result = subprocess.run(
            [sys.executable, "-m", "privacy_budget.cli",
             "--source", str(source), "--output", str(output)],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(support.SRC)}, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("result", report)


if __name__ == "__main__":
    unittest.main()
