"""Verify the shipped release fixtures stay executable and coherent."""
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import support


class Fixtures(unittest.TestCase):
    def test_documented_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "catalog.db"
            root = support.ROOT / "fixtures"
            init = support.run_cli("init", "--db", db, "--source", root / "release-limits.json")
            self.assertEqual(init.returncode, 0, init.stderr)
            plan = support.run_cli("plan", "--db", db, "--source", root / "release-seed.json")
            self.assertEqual(plan.returncode, 0, plan.stderr)
            choice = json.loads(plan.stdout)["choices"][0]["choice_id"]
            published = support.run_cli(
                "publish", "--db", db, "--source", root / "release-seed.json",
                "--choice", choice, "--output", base / "seed.json",
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            combined = support.run_cli(
                "plan", "--db", db, "--source", root / "release-request.json"
            )
            self.assertEqual(combined.returncode, 0, combined.stderr)
            entries = {
                tuple(item["basis"]): item
                for item in json.loads(combined.stdout)["choices"]
            }
            cached = entries[("reuse:seed-ab/A", "reuse:seed-ab/B")]
            independent = entries[("fresh:F", "reuse:seed-ab/A")]
            self.assertEqual(Fraction(cached["check_variances"][0]), Fraction(5))
            self.assertEqual(Fraction(cached["new_epsilon"]), Fraction(0))
            self.assertEqual(Fraction(independent["check_variances"][0]), Fraction(2))
            self.assertEqual(Fraction(independent["new_epsilon"]), Fraction(1, 2))


class PolicyFixtures(unittest.TestCase):
    """The shipped policy fixtures follow the documented linked scenario."""

    def test_fixtures_are_executable_and_coherent(self):
        import support
        from fractions import Fraction as F

        root = support.ROOT / "fixtures"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            rejected = support.run_policy_cli(
                "policy", "--source", root / "policy-violation.json",
                "--output", base / "violation.json",
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("org-wildcard", rejected.stderr)
            self.assertIn("dataset-a", rejected.stderr)
            self.assertIn("dataset-b", rejected.stderr)
            self.assertFalse((base / "violation.json").exists())

            passed = support.run_policy_cli(
                "policy", "--source", root / "policy-pass.json",
                "--output", base / "pass.json",
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            report = json.loads((base / "pass.json").read_text())
            self.assertEqual(report["status"], "ok")
            check = report["rule_checks"][0]
            self.assertEqual(F(check["epsilon_sum"]), F(5))
            self.assertEqual(F(check["delta_sum"]), F(1, 10))

            tied = support.run_policy_cli(
                "policy", "--source", root / "policy-tie.json",
                "--output", base / "tie.json",
            )
            self.assertEqual(tied.returncode, 0, tied.stderr)
            tie_report = json.loads((base / "tie.json").read_text())
            record_a = next(
                s for s in tie_report["scopes"] if s["dataset"] == "dataset-a"
            )
            self.assertEqual(record_a["epsilon_sources"], ["exact-a"])
            self.assertEqual(
                record_a["matching_rule_ids"], ["exact-a", "org-wildcard"]
            )


if __name__ == "__main__":
    unittest.main()
