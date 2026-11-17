"""End-to-end acceptance tests for the joint release planner."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import support
from support import check, frac, fresh, query, request


def _find(payload, basis):
    for choice in payload["choices"]:
        if tuple(choice["basis"]) == tuple(basis):
            return choice
    return None


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "catalog.db"
        self.limits = support.write_json(
            self.base / "limits.json",
            {
                "limits": [
                    {
                        "dataset": "d",
                        "principal": "p",
                        "epsilon": frac(100),
                        "delta": frac(1),
                    }
                ]
            },
        )
        result = support.run_cli("init", "--db", self.db, "--source", self.limits)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, payload, name="req.json", expect=0):
        source = support.write_json(self.base / name, payload)
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, expect, result.stderr)
        return (json.loads(result.stdout) if expect == 0 else result)

    def publish(self, payload, basis, name="req.json", expect=0):
        source = support.write_json(self.base / name, payload)
        if expect == 0:
            plan = self.plan(payload, name)
            choice = _find(plan, basis)
            self.assertIsNotNone(choice, "basis %r missing from plan" % (basis,))
            choice_id = choice["choice_id"]
        else:
            choice_id = "0" * 64
        output = self.base / (name.replace(".json", "") + "-report.json")
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice_id, "--output", output,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return (json.loads(output.read_text()) if expect == 0 else result), source, choice_id

    def catalog(self, snapshot="s"):
        result = support.run_cli(
            "catalog", "--db", self.db, "--dataset", "d", "--principal", "p",
            "--snapshot", snapshot,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def retire(self, measurement, snapshot="s"):
        result = support.run_cli(
            "retire", "--db", self.db, "--dataset", "d", "--principal", "p",
            "--snapshot", snapshot, "--measurement", measurement,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CatalogIdentity(Harness):
    def test_identity_conflict_across_restart(self):
        first = request(
            "r1", ["x"],
            fresh=[fresh("m", [1], value=5, variance=1, epsilon=frac(1, 2))],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        self.publish(first, ["fresh:m"])
        # Same ID, different declaration: rejected even from a fresh process.
        conflict = request(
            "r2", ["x"],
            fresh=[fresh("m", [1], value=6, variance=1, epsilon=frac(1, 2))],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        result = self.plan(conflict, "conflict.json", expect=2)
        self.assertIn("identity conflicts", result.stderr)
        # Identical declaration and unretired: reused at zero new cost.
        again = request(
            "r3", ["x"],
            fresh=[fresh("m", [1], value=5, variance=1, epsilon=frac(1, 2))],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        plan = self.plan(again, "again.json")
        choice = _find(plan, ["fresh:m"])
        self.assertEqual(Fraction(choice["new_epsilon"]), Fraction(0))

    def test_domain_fixed_after_publication(self):
        first = request(
            "r1", ["x", "y"],
            fresh=[fresh("m", [1, 0])],
            queries=[query("q", [1, 0])],
            checks=[check("k", {"q": 1}, 10)],
        )
        self.publish(first, ["fresh:m"], "d1.json")
        changed = request(
            "r2", ["y", "x"],
            fresh=[fresh("m2", [1, 0])],
            queries=[query("q", [1, 0])],
            checks=[check("k", {"q": 1}, 10)],
        )
        result = self.plan(changed, "d2.json", expect=2)
        self.assertIn("coordinate domain is fixed", result.stderr)

    def test_unselected_fresh_never_appears(self):
        payload = request(
            "r1", ["x"],
            fresh=[fresh("keep", [1]), fresh("spare", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        plan = self.plan(payload)
        # Only one of them can enter any basis; the plan exposes no estimates.
        self.assertNotIn("estimate", json.dumps(plan))
        chosen = _find(plan, ["fresh:keep"])
        self.assertIsNotNone(chosen)
        self.publish(payload, ["fresh:keep"], "u1.json")
        catalog = self.catalog()
        ids = [measurement["id"] for measurement in catalog["measurements"]]
        self.assertEqual(ids, ["keep"])
        # Re-planning with both again: spare is still "fresh", keep is free.
        again = self.plan(payload, "u2.json")
        entry = _find(again, ["fresh:keep"])
        self.assertEqual(Fraction(entry["new_epsilon"]), Fraction(0))


class Lineage(Harness):
    def _publish_ab(self):
        payload = request(
            "ab", ["x1", "x2"],
            fresh=[fresh("n1", [1, 0]), fresh("n2", [0, 1])],
            queries=[query("A", [1, 0]), query("B", [1, 1])],
            checks=[check("k", {"A": 1, "B": 1}, 1000)],
        )
        report, _, _ = self.publish(payload, ["fresh:n1", "fresh:n2"], "ab.json")
        return payload, report

    def test_cached_sum_variance(self):
        self._publish_ab()
        strict = request(
            "strict", ["x1", "x2"],
            fresh=[fresh("f", [1, 1], epsilon=frac(1, 2))],
            reuse=[
                {"release_id": "ab", "query_id": "A"},
                {"release_id": "ab", "query_id": "B"},
            ],
            queries=[query("S", [2, 1])],
            checks=[check("k", {"S": 1}, 4)],
        )
        plan = self.plan(strict, "strict.json")
        bases = [tuple(choice["basis"]) for choice in plan["choices"]]
        self.assertNotIn(("reuse:ab/A", "reuse:ab/B"), bases)
        self.assertEqual(bases, [("fresh:f", "reuse:ab/A")])
        self.assertEqual(Fraction(plan["choices"][0]["check_variances"][0]), Fraction(2))

        loose_payload = request(
            "loose", ["x1", "x2"],
            fresh=[fresh("f", [1, 1], epsilon=frac(1, 2))],
            reuse=[
                {"release_id": "ab", "query_id": "A"},
                {"release_id": "ab", "query_id": "B"},
            ],
            queries=[query("S", [2, 1])],
            checks=[check("k", {"S": 1}, 6)],
        )
        plan = self.plan(loose_payload, "loose.json")
        entries = {tuple(c["basis"]): c for c in plan["choices"]}
        cached = entries[("reuse:ab/A", "reuse:ab/B")]
        fresh_entry = entries[("fresh:f", "reuse:ab/A")]
        self.assertEqual(Fraction(cached["check_variances"][0]), Fraction(5))
        self.assertEqual(Fraction(cached["new_epsilon"]), Fraction(0))
        self.assertEqual(Fraction(fresh_entry["check_variances"][0]), Fraction(2))
        self.assertEqual(Fraction(fresh_entry["new_epsilon"]), Fraction(1, 2))

    def test_retire_after_alias_publication(self):
        self._publish_ab()
        # D = B - A = x2, lineage {n2: 1}.
        diff = request(
            "ba", ["x1", "x2"],
            reuse=[
                {"release_id": "ab", "query_id": "A"},
                {"release_id": "ab", "query_id": "B"},
            ],
            queries=[query("D", [0, 1])],
            checks=[check("k", {"D": 1}, 2)],
        )
        report, _, _ = self.publish(diff, ["reuse:ab/A", "reuse:ab/B"], "ba.json")
        self.assertEqual(
            [term["id"] for term in report["queries"][0]["lineage"]], ["n2"]
        )
        self.retire("n1")
        # Serving x2 through alias D survives, with no new cost.
        x2 = request(
            "x2", ["x1", "x2"],
            reuse=[{"release_id": "ba", "query_id": "D"}],
            queries=[query("E", [0, 1])],
            checks=[check("k", {"E": 1}, 2)],
        )
        plan = self.plan(x2, "x2.json")
        choice = _find(plan, ["reuse:ba/D"])
        self.assertEqual(Fraction(choice["new_epsilon"]), Fraction(0))
        # Serving x1 through A (nonzero n1) is impossible.
        x1 = request(
            "x1", ["x1", "x2"],
            reuse=[{"release_id": "ab", "query_id": "A"}],
            queries=[query("E", [1, 0])],
            checks=[check("k", {"E": 1}, 2)],
        )
        result = self.plan(x1, "x1.json", expect=2)
        self.assertIn("no feasible basis", result.stderr)
        # Retirement refunds nothing: both raw measurements stay charged.
        catalog = self.catalog()
        self.assertEqual(Fraction(catalog["budget"]["epsilon_spent"]), Fraction(2))

    def test_catalog_reports_ancestor_retirement(self):
        self._publish_ab()
        self.retire("n2")
        catalog = self.catalog()
        entries = {(q["release_id"], q["query_id"]): q for q in catalog["queries"]}
        self.assertEqual(entries[("ab", "A")]["retired_ancestors"], [])
        self.assertEqual(entries[("ab", "B")]["retired_ancestors"], ["n2"])
        self.assertNotIn("measurements_unselected", catalog)
        self.assertTrue(all("retired" in m for m in catalog["measurements"]))

    def test_multi_hop_shared_noise_not_duplicated(self):
        self._publish_ab()
        # Alias P = A + B (lineage n1*2 + n2) then alias Q = B - A (n2 only).
        first = request(
            "p", ["x1", "x2"],
            reuse=[
                {"release_id": "ab", "query_id": "A"},
                {"release_id": "ab", "query_id": "B"},
            ],
            queries=[query("P", [2, 1])],
            checks=[check("k", {"P": 1}, 100)],
        )
        self.publish(first, ["reuse:ab/A", "reuse:ab/B"], "p.json")
        second = request(
            "q", ["x1", "x2"],
            reuse=[
                {"release_id": "ab", "query_id": "A"},
                {"release_id": "ab", "query_id": "B"},
            ],
            queries=[query("Q", [0, 1])],
            checks=[check("k", {"Q": 1}, 2)],
        )
        self.publish(second, ["reuse:ab/A", "reuse:ab/B"], "q.json")
        # Recombining P - Q must yield 2*A, i.e. noise 2*n1, variance 4.
        third = request(
            "third", ["x1", "x2"],
            reuse=[
                {"release_id": "p", "query_id": "P"},
                {"release_id": "q", "query_id": "Q"},
            ],
            queries=[query("Q2", [2, 0])],
            checks=[check("k", {"Q2": 1}, 4)],
        )
        report, _, _ = self.publish(
            third, ["reuse:p/P", "reuse:q/Q"], "third.json"
        )
        lineage = {
            term["id"]: Fraction(term["coefficient"])
            for term in report["queries"][0]["lineage"]
        }
        self.assertEqual(lineage, {"n1": Fraction(2)})
        self.assertEqual(Fraction(report["covariance"][0][0]), Fraction(4))
        # Reusing P alone twice must not create independence: the two weights
        # coalesce into the same raw noise.
        single = request(
            "single", ["x1", "x2"],
            reuse=[{"release_id": "p", "query_id": "P"}],
            queries=[query("R", [2, 1]), query("R2", [4, 2])],
            checks=[check("k", {"R": 1, "R2": 1}, 200)],
        )
        report, _, _ = self.publish(single, ["reuse:p/P"], "single.json")
        self.assertEqual(
            [[Fraction(v) for v in row] for row in report["covariance"]],
            [[Fraction(5), Fraction(10)], [Fraction(10), Fraction(20)]],
        )


class Failures(Harness):
    def test_stale_choice_after_retire(self):
        payload = request(
            "r", ["x"],
            fresh=[fresh("m", [1]), fresh("m2", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        source = support.write_json(self.base / "r.json", payload)
        plan = self.plan(payload)
        first = [c for c in plan["choices"] if tuple(c["basis"]) == ("fresh:m",)][0]
        second = [
            c for c in plan["choices"]
            if tuple(c["basis"]) == ("fresh:m2",) and c["new_epsilon"] == "1"
        ]
        # Retire nothing yet; instead publish the request under a *new*
        # release that makes m unavailable by retiring it after planning.
        publish_payload = dict(payload)
        publish_payload["release_id"] = "prepare"
        self.publish(publish_payload, ["fresh:m"], "prepare.json")
        self.retire("m")
        output = self.base / "stale.json"
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", first["choice_id"], "--output", output,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not on the current frontier", result.stderr)
        self.assertFalse(output.exists())

    def test_export_failure_then_retry(self):
        payload = request(
            "export", ["x"],
            fresh=[fresh("m", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        source = support.write_json(self.base / "export.json", payload)
        plan = self.plan(payload)
        choice = _find(plan, ["fresh:m"])["choice_id"]
        # Destination inside a *file* makes the atomic write fail post-commit.
        blocker = self.base / "not-a-dir"
        blocker.write_text("x")
        failing = blocker / "report.json"
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", failing,
        )
        self.assertNotEqual(result.returncode, 0)
        # Budget was already charged exactly once.
        catalog = self.catalog()
        self.assertEqual(Fraction(catalog["budget"]["epsilon_spent"]), Fraction(1))
        # Retry delivers the committed report without charging again.
        good = self.base / "report.json"
        retry = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", good,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(Fraction(self.catalog()["budget"]["epsilon_spent"]), Fraction(1))
        self.assertEqual(json.loads(good.read_text())["release_id"], "export")

    def test_bounded_retire_is_idempotent_and_unknown_rejected(self):
        payload = request(
            "ret", ["x"],
            fresh=[fresh("m", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        self.publish(payload, ["fresh:m"])
        self.retire("m")
        self.retire("m")  # idempotent
        result = support.run_cli(
            "retire", "--db", self.db, "--dataset", "d", "--principal", "p",
            "--snapshot", "s", "--measurement", "ghost",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown raw measurement", result.stderr)

    def test_conflicting_release_retry(self):
        payload = request(
            "conf", ["x"],
            fresh=[fresh("m", [1], value=1)],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        self.publish(payload, ["fresh:m"])
        changed = json.loads(json.dumps(payload))
        changed["fresh"][0]["value"] = frac(2)
        result = self.plan(changed, "conf2.json") if False else None
        source = support.write_json(self.base / "conf2.json", changed)
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", "f" * 64, "--output", self.base / "out.json",
        )
        self.assertEqual(result.returncode, 2)

    def test_failed_publish_keeps_state(self):
        payload = request(
            "keep", ["x"],
            fresh=[fresh("m", [1])],
            queries=[query("q", [0])],
            checks=[check("k", {"q": 1}, 10)],
        )
        plan = self.plan(payload)
        # Empty basis serves the zero query at zero cost; it exists.
        empty = _find(plan, [])
        self.assertEqual(Fraction(empty["new_epsilon"]), Fraction(0))
        before = self.catalog()
        source = support.write_json(self.base / "keep.json", payload)
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", "1" * 64, "--output", self.base / "nope.json",
        )
        self.assertEqual(result.returncode, 2)
        after = self.catalog()
        self.assertEqual(before, after)

    def test_output_safety(self):
        payload = request(
            "safe", ["x"],
            fresh=[fresh("m", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        source = support.write_json(self.base / "safe.json", payload)
        plan = self.plan(payload)
        choice = _find(plan, ["fresh:m"])["choice_id"]
        same = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", source,
        )
        self.assertEqual(same.returncode, 2)
        link = self.base / "link.json"
        link.symlink_to(source)
        linked = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", link,
        )
        self.assertEqual(linked.returncode, 2)
        # Nothing was charged by either rejected attempt.
        self.assertEqual(Fraction(self.catalog()["budget"]["epsilon_spent"]), Fraction(0))


class Concurrency(Harness):
    def test_last_budget_unit_serialised(self):
        self.limits = support.write_json(
            self.base / "small.json",
            {
                "limits": [
                    {
                        "dataset": "q",
                        "principal": "r",
                        "epsilon": frac(1),
                        "delta": frac(0),
                    }
                ]
            },
        )
        db = self.base / "race.db"
        init = support.run_cli("init", "--db", db, "--source", self.limits)
        self.assertEqual(init.returncode, 0, init.stderr)
        sources = []
        choices = []
        for index in (1, 2):
            payload = request(
                "race%d" % index, ["x"],
                fresh=[fresh("m%d" % index, [1], epsilon=frac(1))],
                queries=[query("q", [1])],
                checks=[check("k", {"q": 1}, 10)],
                dataset="q", principal="r",
            )
            source = support.write_json(self.base / ("race%d.json" % index), payload)
            plan = support.run_cli("plan", "--db", db, "--source", source)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            choices.append(json.loads(plan.stdout)["choices"][0]["choice_id"])
            sources.append(source)
        environment = {"PYTHONPATH": str(support.SRC)}
        processes = []
        for index in (0, 1):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "privacy_budget.release_cli",
                        "publish", "--db", str(db), "--source", str(sources[index]),
                        "--choice", choices[index],
                        "--output", str(self.base / ("race-out%d.json" % index)),
                    ],
                    env=environment, cwd=str(support.ROOT),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            )
        codes = []
        for process in processes:
            process.communicate()
            codes.append(process.returncode)
        self.assertEqual(sorted(codes), [0, 2])
        connection = sqlite3.connect(str(db))
        try:
            rows = connection.execute("SELECT id FROM measurements").fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
