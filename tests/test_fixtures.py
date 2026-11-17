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


if __name__ == "__main__":
    unittest.main()
