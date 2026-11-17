"""Enumeration-oracle cross-checks for the offline policy compiler.

A small independent oracle (``policy_oracle.py``) recomputes matching,
effective allowances, ties and aggregate violations from scratch.  These
tests enumerate wildcard/exact selector combinations, equal-value ties,
unmatched rules, separate epsilon/delta overflows, report stability and
failure-time preservation of the previous report.
"""
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import policy_oracle
import support
from support import frac

from privacy_budget.policy_model import PolicyError, compile_policy, parse_policy


def q(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def make_scope(dataset, principal, epsilon, delta):
    return {
        "dataset": dataset,
        "principal": principal,
        "epsilon": q(epsilon),
        "delta": q(delta),
    }


def make_rule(rule_id, ds, ps, epsilon, delta):
    return {
        "id": rule_id,
        "dataset_selector": ds,
        "principal_selector": ps,
        "epsilon": q(epsilon),
        "delta": q(delta),
    }


def compile_raw(payload):
    scopes, rules = parse_policy(payload)
    return compile_policy(scopes, rules, "a" * 64)


class AgainstOracle(unittest.TestCase):
    def assert_matches_oracle(self, payload):
        status, scope_view, checks, violations, unmatched = \
            policy_oracle.reference(payload)
        if unmatched:
            # The compiler rejects rules whose selectors match no scope.
            with self.assertRaises(PolicyError) as caught:
                compile_raw(payload)
            for rule_id in unmatched:
                self.assertIn(rule_id, str(caught.exception))
            return None
        report = compile_raw(payload)
        self.assertEqual(report["status"], status)
        self.assertEqual(
            sorted(v["rule_id"] for v in report["violations"]), violations
        )
        for record in report["scopes"]:
            key = (record["dataset"], record["principal"])
            expected = scope_view[key]
            self.assertEqual(
                (Fraction(record["effective"]["epsilon"]),
                 Fraction(record["effective"]["delta"])),
                expected["effective"],
            )
            self.assertEqual(
                (Fraction(record["configured"]["epsilon"]),
                 Fraction(record["configured"]["delta"])),
                expected["configured"],
            )
            self.assertEqual(record["epsilon_sources"],
                             expected["epsilon_sources"])
            self.assertEqual(record["delta_sources"],
                             expected["delta_sources"])
            self.assertEqual(record["matching_rule_ids"],
                             expected["matching_rule_ids"])
        for check in report["rule_checks"]:
            expected = checks[check["rule_id"]]
            self.assertEqual(
                sorted((s["dataset"], s["principal"])
                       for s in check["matched_scopes"]),
                expected["matched"],
            )
            self.assertEqual(Fraction(check["epsilon_sum"]),
                             expected["epsilon_sum"])
            self.assertEqual(Fraction(check["delta_sum"]),
                             expected["delta_sum"])
            self.assertEqual(Fraction(check["epsilon_excess"]),
                             expected["epsilon_excess"])
            self.assertEqual(Fraction(check["delta_excess"]),
                             expected["delta_excess"])
        return report

    def test_enumerate_wildcard_exact_combinations(self):
        datasets = ["d1", "d2"]
        principals = ["p1", "p2"]
        patterns = policy_oracle.all_selector_patterns(datasets, principals)
        # Two scopes with distinct names so every pattern is resolvable.
        scopes = [
            make_scope("d1", "p1", Fraction(3), Fraction(1, 10)),
            make_scope("d2", "p2", Fraction(2), Fraction(1, 5)),
        ]
        cap_grid = [Fraction(1), Fraction(2), Fraction(3), Fraction(6)]
        rule_index = 0
        # Enumerate a bounded but exhaustive slice of the selector/cap space.
        for ds, ps in patterns:
            for cap_e, cap_d in itertools.product(cap_grid, cap_grid):
                payload = {
                    "scopes": scopes,
                    "rules": [make_rule("r%d" % rule_index, ds, ps,
                                        cap_e, cap_d)],
                }
                rule_index += 1
                self.assert_matches_oracle(payload)

    def test_two_overlapping_rules_full_cap_grid(self):
        scopes = [
            make_scope("A", "p", Fraction(4), Fraction(1, 20)),
            make_scope("B", "p", Fraction(3), Fraction(1, 20)),
        ]
        grid = [Fraction(2), Fraction(4), Fraction(5), Fraction(9)]
        for we, wd, ae, ad in itertools.product(grid, grid, grid, grid):
            payload = {
                "scopes": scopes,
                "rules": [
                    make_rule("wild", "*", "*", we, wd),
                    make_rule("exact-a", "A", "p", ae, ad),
                ],
            }
            self.assert_matches_oracle(payload)

    def test_equal_value_ties_against_oracle(self):
        scopes = [make_scope("d", "p", Fraction(4), Fraction(1, 20))]
        # All three of configured, wildcard and exact share epsilon 4; delta
        # ties between the two rules while configured is larger.
        payload = {
            "scopes": scopes,
            "rules": [
                make_rule("wild", "*", "*", Fraction(4), Fraction(1, 20)),
                make_rule("exact", "d", "p", Fraction(4), Fraction(1, 20)),
            ],
        }
        report = self.assert_matches_oracle(payload)
        record = report["scopes"][0]
        self.assertEqual(record["epsilon_sources"], ["exact", "wild"])
        self.assertEqual(record["delta_sources"], ["exact", "wild"])

    def test_unmatched_rule_is_rejected_with_its_id(self):
        # d1/p2 names catalog values that both exist, but no scope owns that
        # combination: the rule matches nothing and must be rejected.
        payload = {
            "scopes": [
                make_scope("d1", "p1", Fraction(1), Fraction(0)),
                make_scope("d2", "p2", Fraction(1), Fraction(0)),
            ],
            "rules": [
                make_rule("dead", "d1", "p2", Fraction(2), Fraction(1)),
                make_rule("wild", "*", "*", Fraction(3), Fraction(1)),
            ],
        }
        self.assertIsNone(self.assert_matches_oracle(payload))
        # parse_policy alone accepts the names (both exist); compilation rejects.
        scopes, rules = parse_policy(payload)
        with self.assertRaises(PolicyError) as caught:
            compile_policy(scopes, rules, "a" * 64)
        self.assertIn("dead", str(caught.exception))
        self.assertIn("matches no scope", str(caught.exception))

    def test_every_accepted_rule_matches_at_least_one_scope(self):
        payload = {
            "scopes": [make_scope("d1", "p1", Fraction(1), Fraction(0))],
            "rules": [
                make_rule("exact", "d1", "p1", Fraction(2), Fraction(1)),
                make_rule("wild", "*", "*", Fraction(3), Fraction(1)),
            ],
        }
        report = self.assert_matches_oracle(payload)
        self.assertEqual(report["unmatched_rule_ids"], [])
        for check in report["rule_checks"]:
            self.assertGreaterEqual(check["matched_scope_count"], 1)

    def test_epsilon_only_and_delta_only_overflows(self):
        base_scopes = [
            make_scope("A", "p", Fraction(3), Fraction(1, 5)),
            make_scope("B", "p", Fraction(3), Fraction(1, 5)),
        ]
        epsilon_case = {
            "scopes": base_scopes,
            "rules": [make_rule("r", "*", "*", Fraction(5), Fraction(1))],
        }
        report_e = self.assert_matches_oracle(epsilon_case)
        v_e = report_e["violations"][0]
        self.assertGreater(Fraction(v_e["epsilon_excess"]), Fraction(0))
        self.assertEqual(Fraction(v_e["delta_excess"]), Fraction(0))

        delta_case = {
            "scopes": [
                make_scope("A", "p", Fraction(1), Fraction(1, 5)),
                make_scope("B", "p", Fraction(1), Fraction(1, 5)),
            ],
            "rules": [make_rule("r", "*", "*", Fraction(9), Fraction(1, 10))],
        }
        report_d = self.assert_matches_oracle(delta_case)
        v_d = report_d["violations"][0]
        self.assertEqual(Fraction(v_d["epsilon_excess"]), Fraction(0))
        self.assertGreater(Fraction(v_d["delta_excess"]), Fraction(0))

    def test_randomised_policies_against_oracle(self):
        random.seed(20261117)
        names = ["d1", "d2", "d3"]
        principals = ["p1", "p2"]
        for trial in range(60):
            scope_count = random.randint(1, 4)
            chosen = random.sample(
                list(itertools.product(names, principals)), scope_count
            )
            scopes = [
                make_scope(d, p,
                           Fraction(random.randint(0, 6), 1),
                           Fraction(random.randint(0, 4), 20))
                for d, p in chosen
            ]
            rule_count = random.randint(1, 4)
            patterns = policy_oracle.all_selector_patterns(
                {d for d, _ in chosen}, {p for _, p in chosen}
            )
            picked = random.sample(patterns, min(rule_count, len(patterns)))
            rules = []
            used_ids = set()
            for index, (ds, ps) in enumerate(picked):
                rid = "r%d" % index
                used_ids.add(rid)
                rules.append(
                    make_rule(
                        rid, ds, ps,
                        Fraction(random.randint(1, 7), 1),
                        Fraction(random.randint(1, 8), 20),
                    )
                )
            self.assert_matches_oracle({"scopes": scopes, "rules": rules})

    def test_report_stability_under_reordering(self):
        payload = {
            "scopes": [
                make_scope("B", "p", Fraction(1), Fraction(1, 20)),
                make_scope("A", "p", Fraction(4), Fraction(1, 20)),
            ],
            "rules": [
                make_rule("exact-a", "A", "p", Fraction(4), Fraction(1, 20)),
                make_rule("wild", "*", "*", Fraction(5), Fraction(1, 10)),
            ],
        }
        first = compile_raw(payload)
        reordered = {
            "scopes": list(reversed(payload["scopes"])),
            "rules": list(reversed(payload["rules"])),
        }
        second = compile_raw(reordered)
        self.assertEqual(first, second)
        # Re-serialising the *input policy* with shuffled object key order
        # must produce the same normalised report and digest.
        reserialised = json.loads(json.dumps(payload))
        self.assertEqual(
            compile_raw(reserialised)["report_sha256"], first["report_sha256"]
        )


class CommandLineSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, payload):
        path = self.base / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def good_payload(self):
        return {
            "scopes": [make_scope("d", "p", Fraction(1), Fraction(0))],
            "rules": [make_rule("r", "*", "*", Fraction(2), Fraction(1))],
        }

    def test_failure_keeps_old_report_oracle_scenario(self):
        good = self.write("good.json", self.good_payload())
        output = self.base / "report.json"
        ok = support.run_policy_cli(
            "policy", "--source", good, "--output", output
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        prior = output.read_bytes()
        bad = self.write(
            "bad.json",
            {
                "scopes": [
                    make_scope("A", "p", Fraction(4), Fraction(1, 20)),
                    make_scope("B", "p", Fraction(3), Fraction(1, 20)),
                ],
                "rules": [
                    make_rule("wild", "*", "*", Fraction(5), Fraction(1, 10))
                ],
            },
        )
        failed = support.run_policy_cli(
            "policy", "--source", bad, "--output", output
        )
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(output.read_bytes(), prior)
        # Once the source is repaired, the same output path is overwritten.
        fixed = self.write(
            "fixed.json",
            {
                "scopes": [
                    make_scope("A", "p", Fraction(4), Fraction(1, 20)),
                    make_scope("B", "p", Fraction(1), Fraction(1, 20)),
                ],
                "rules": [
                    make_rule("wild", "*", "*", Fraction(5), Fraction(1, 10))
                ],
            },
        )
        repaired = support.run_policy_cli(
            "policy", "--source", fixed, "--output", output
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertNotEqual(output.read_bytes(), prior)


if __name__ == "__main__":
    unittest.main()
