"""Oracle cross-checks for planning: feasibility, exact frontier, dedupe."""
import itertools
import json
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import oracle
import support
from support import check, frac, fresh, query, request


class PlanAgainstOracle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "catalog.db"
        support.write_json(
            self.base / "limits.json",
            {
                "limits": [
                    {
                        "dataset": "d",
                        "principal": "p",
                        "epsilon": frac(50),
                        "delta": frac(1, 10),
                    }
                ]
            },
        )
        result = support.run_cli("init", "--db", self.db, "--source", self.base / "limits.json")
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, payload, name="req.json"):
        source = support.write_json(self.base / name, payload)
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    # -- fixed scenarios -------------------------------------------------

    def test_single_query_basis_selection(self):
        payload = request(
            "r1",
            ["a", "b"],
            fresh=[
                fresh("u", [1, 0]),
                fresh("v", [0, 1]),
                fresh("w", [1, 1]),
            ],
            queries=[query("q", [2, 3])],
            checks=[check("k", {"q": 1}, 100)],
        )
        plan = self.plan(payload)
        entries = {tuple(c["basis"]): c for c in plan["choices"]}
        # {v, w} reconstructs q = 2x1 + 3x2 with variance 1*1 + 4*1 = 5 while
        # {u, v} costs the same privacy budget but variance 13, so it is
        # Pareto dominated and must not appear.
        self.assertEqual(list(entries), [("fresh:v", "fresh:w")])
        self.assertEqual(Fraction(entries[("fresh:v", "fresh:w")]["check_variances"][0]), Fraction(5))

    def test_correlated_reuse_lineage_not_independent(self):
        # Publish A = x1 + n1 and B = x1 + x2 + n1 + n2 with unit variance.
        setup = request(
            "setup",
            ["x1", "x2"],
            fresh=[fresh("n1", [1, 0]), fresh("n2", [0, 1])],
            queries=[query("A", [1, 0]), query("B", [1, 1])],
            checks=[check("k", {"A": 1, "B": 1}, 1000)],
        )
        plan = self.plan(setup, "setup.json")
        choice = plan["choices"][0]["choice_id"]
        source = self.base / "setup.json"
        published = support.run_cli(
            "publish", "--db", self.db, "--source", source, "--choice", choice,
            "--output", self.base / "setup-report.json",
        )
        self.assertEqual(published.returncode, 0, published.stderr)

        payload = request(
            "r2",
            ["x1", "x2"],
            reuse=[
                {"release_id": "setup", "query_id": "A"},
                {"release_id": "setup", "query_id": "B"},
            ],
            queries=[query("S", [2, 1])],
            checks=[check("k", {"S": 1}, 100)],
        )
        plan = self.plan(payload, "r2.json")
        entries = {tuple(c["basis"]): c for c in plan["choices"]}
        # A + B shares n1, so Var(A + B) = Var(n1*2 + n2) = 4 + 1 = 5.
        cached = entries[("reuse:setup/A", "reuse:setup/B")]
        self.assertEqual(Fraction(cached["check_variances"][0]), Fraction(5))
        self.assertEqual(Fraction(cached["new_epsilon"]), Fraction(0))

    def test_covariance_shared_noise(self):
        payload = request(
            "r3",
            ["x"],
            fresh=[fresh("g", [1])],
            queries=[query("q1", [1]), query("q2", [2])],
            checks=[check("k", {"q1": 1, "q2": 1}, 100)],
        )
        plan = self.plan(payload)
        covariance = plan["choices"][0]["covariance"]
        self.assertEqual(
            [[Fraction(v) for v in row] for row in covariance],
            [[Fraction(1), Fraction(2)], [Fraction(2), Fraction(4)]],
        )
        # w = (1, 1) -> 1 + 2*2 + 4 = 9.
        self.assertEqual(Fraction(plan["choices"][0]["check_variances"][0]), Fraction(9))

    def test_equal_metrics_dedupe_lexicographic(self):
        # Two fresh measurements with the same vector: identical metrics, the
        # dedupe must retain the lexicographically smaller basis key list.
        payload = request(
            "r4",
            ["x"],
            fresh=[fresh("b", [1]), fresh("a", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 100)],
        )
        plan = self.plan(payload)
        bases = [tuple(c["basis"]) for c in plan["choices"]]
        self.assertEqual(bases, [("fresh:a",)])

    def test_empty_basis_only_serves_zero_query(self):
        payload = request(
            "r5",
            ["x"],
            fresh=[fresh("u", [1])],
            queries=[query("zero", [0])],
            checks=[check("k", {"zero": 1}, 0)],
        )
        plan = self.plan(payload)
        bases = {tuple(c["basis"]) for c in plan["choices"]}
        self.assertIn((), bases)
        empty = next(c for c in plan["choices"] if tuple(c["basis"]) == ())
        self.assertEqual(Fraction(empty["check_variances"][0]), Fraction(0))

    def test_infeasible_span_is_rejected(self):
        payload = request(
            "r6",
            ["x", "y"],
            fresh=[fresh("u", [1, 0])],
            queries=[query("q", [0, 1])],
            checks=[check("k", {"q": 1}, 100)],
        )
        source = support.write_json(self.base / "r6.json", payload)
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no feasible basis", result.stderr)

    # -- randomised oracle ----------------------------------------------

    def test_random_small_against_oracle(self):
        random.seed(20261117)
        for trial in range(12):
            width = random.choice([1, 1, 2, 2, 3])
            raw = 3
            vectors = [
                [random.randint(-2, 2) for _ in range(width)] for _ in range(raw)
            ]
            candidates = []
            fresh_items = []
            for index, vector in enumerate(vectors):
                if all(v == 0 for v in vector):
                    vector = [1] + [0] * (width - 1)
                raw_id = "m%d" % index
                epsilon = Fraction(random.randint(1, 3), 2)
                delta = Fraction(random.randint(0, 1), 10)
                candidates.append(
                    {
                        "key": "fresh:" + raw_id,
                        "vector": [Fraction(v) for v in vector],
                        "lineage": {raw_id: Fraction(1)},
                        "new_id": raw_id,
                        "epsilon": epsilon,
                        "delta": delta,
                    }
                )
                fresh_items.append(
                    fresh(
                        raw_id,
                        vector,
                        value=random.randint(-3, 3),
                        variance=random.randint(1, 2),
                        epsilon=frac(epsilon.numerator, epsilon.denominator),
                        delta=frac(delta.numerator, delta.denominator),
                    )
                )
            queries = []
            query_items = []
            for index in range(random.randint(1, 2)):
                vector = [random.randint(-2, 2) for _ in range(width)]
                queries.append(
                    {"id": "q%d" % index, "vector": [Fraction(v) for v in vector]}
                )
                query_items.append(query("q%d" % index, vector))
            checks = [{"id": "k", "weights": {"q0": Fraction(1)}, "max_variance": Fraction(6)}]
            check_items = [check("k", {"q0": 1}, 6)]
            variance = {item["id"]: Fraction(item["variance"]["numerator"], item["variance"]["denominator"]) for item in fresh_items}
            expected = oracle.frontier(
                candidates,
                queries,
                checks,
                variance,
                retired=frozenset(),
                budget=(Fraction(0), Fraction(0)),
                epsilon=Fraction(50),
                delta=Fraction(1, 10),
            )
            payload = request(
                "rand%d" % trial,
                ["x%d" % i for i in range(width)],
                fresh=fresh_items,
                queries=query_items,
                checks=check_items,
            )
            try:
                plan = self.plan(payload, "rand%d.json" % trial)
            except AssertionError:
                if not expected:
                    continue
                raise
            actual = [
                {
                    "basis": tuple(c["basis"]),
                    "metrics": (
                        Fraction(c["new_epsilon"]),
                        Fraction(c["new_delta"]),
                        *(Fraction(v) for v in c["check_variances"]),
                    ),
                }
                for c in plan["choices"]
            ]
            expected_compact = [
                {"basis": item["basis"], "metrics": item["metrics"]} for item in expected
            ]
            self.assertEqual(actual, expected_compact, "trial %d" % trial)
            # covariance must match the oracle's quadratic form
            for item, record in zip(expected, plan["choices"]):
                self.assertEqual(
                    [[Fraction(v) for v in row] for row in record["covariance"]],
                    item["covariance"],
                    "trial %d" % trial,
                )


if __name__ == "__main__":
    unittest.main()


class Performance(unittest.TestCase):
    def test_worst_case_contract_bounds_complete(self):
        import time

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db = base / "c.db"
            support.write_json(
                base / "limits.json",
                {"limits": [{"dataset": "d", "principal": "p", "epsilon": frac(100), "delta": frac(0)}]},
            )
            self.assertEqual(
                support.run_cli("init", "--db", db, "--source", base / "limits.json").returncode, 0
            )
            fresh_items = []
            for index in range(16):
                fresh_items.append(
                    fresh(
                        "m%02d" % index,
                        [index % 3, index % 5, index % 7, 1, index % 2, index % 4],
                        variance=1,
                        epsilon=frac(1, 3),
                    )
                )
            payload = request(
                "bounds",
                ["a", "b", "c", "d", "e", "f"],
                fresh=fresh_items,
                queries=[query("q%d" % i, [i % 3, 1, i % 5, 0, 1, i % 4]) for i in range(8)],
                checks=[check("k%d" % i, {"q%d" % (i % 8): 1}, 1000) for i in range(16)],
            )
            source = support.write_json(base / "r.json", payload)
            started = time.monotonic()
            result = support.run_cli("plan", "--db", db, "--source", source)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 45, "contract-bound planning regressed")


class ReuseAgainstOracle(unittest.TestCase):
    """Cross-check reuse/retirement planning against the exhaustive oracle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "c.db"
        support.write_json(
            self.base / "limits.json",
            {"limits": [{"dataset": "d", "principal": "p", "epsilon": frac(100), "delta": frac(1, 10)}]},
        )
        self.assertEqual(
            support.run_cli("init", "--db", self.db, "--source", self.base / "limits.json").returncode,
            0,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, payload, name="r.json"):
        source = support.write_json(self.base / name, payload)
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        return result

    def publish(self, payload, name="p.json"):
        source = support.write_json(self.base / name, payload)
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        choice = json.loads(result.stdout)["choices"][0]["choice_id"]
        output = self.base / (name + ".out")
        result = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", output,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output.read_text())

    def test_multi_hop_with_fresh_and_retirement(self):
        # Publish a correlated pair: A = x1 + n1, B = x1 + x2 + n1 + n2.
        seed = request(
            "seed", ["x1", "x2"],
            fresh=[
                fresh("n1", [1, 0], value=1, variance=1, epsilon=frac(1, 2)),
                fresh("n2", [0, 1], value=2, variance=1, epsilon=frac(1, 2)),
            ],
            queries=[query("A", [1, 0]), query("B", [1, 1])],
            checks=[check("k", {"A": 1, "B": 1}, 1000)],
        )
        self.publish(seed, "seed.json")
        # Publish the difference alias D = B - A = x2 (lineage n2 only).
        diff = request(
            "diff", ["x1", "x2"],
            reuse=[
                {"release_id": "seed", "query_id": "A"},
                {"release_id": "seed", "query_id": "B"},
            ],
            queries=[query("D", [0, 1])],
            checks=[check("k", {"D": 1}, 2)],
        )
        self.publish(diff, "diff.json")
        support.run_cli(
            "retire", "--db", self.db, "--dataset", "d", "--principal", "p",
            "--snapshot", "s", "--measurement", "n1",
        )
        payload = request(
            "combo", ["x1", "x2"],
            fresh=[fresh("F", [1, 1], variance=1, epsilon=frac(1, 2))],
            reuse=[
                {"release_id": "diff", "query_id": "D"},
                {"release_id": "seed", "query_id": "B"},
            ],
            queries=[query("q", [1, 1]), query("r", [0, 2])],
            checks=[check("k0", {"q": 1}, 10), check("k1", {"q": 1, "r": 1}, 10)],
        )
        result = self.plan(payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = support.parse_plan(json.loads(result.stdout))

        # Oracle inputs mirror the published catalog at this point.
        candidates = [
            {
                "key": "fresh:F",
                "vector": [Fraction(1), Fraction(1)],
                "lineage": {"F": Fraction(1)},
                "new_id": "F",
                "epsilon": Fraction(1, 2),
                "delta": Fraction(0),
            },
            {
                "key": "reuse:diff/D",
                "vector": [Fraction(0), Fraction(1)],
                "lineage": {"n2": Fraction(1)},
                "new_id": None,
                "epsilon": Fraction(0),
                "delta": Fraction(0),
            },
            {
                "key": "reuse:seed/B",
                "vector": [Fraction(1), Fraction(1)],
                "lineage": {"n1": Fraction(1), "n2": Fraction(1)},
                "new_id": None,
                "epsilon": Fraction(0),
                "delta": Fraction(0),
            },
        ]
        queries = [
            {"id": "q", "vector": [Fraction(1), Fraction(1)]},
            {"id": "r", "vector": [Fraction(0), Fraction(2)]},
        ]
        checks = [
            {"id": "k0", "weights": {"q": Fraction(1)}, "max_variance": Fraction(10)},
            {
                "id": "k1",
                "weights": {"q": Fraction(1), "r": Fraction(1)},
                "max_variance": Fraction(10),
            },
        ]
        variance = {"F": Fraction(1), "n1": Fraction(1), "n2": Fraction(1)}
        expected = oracle.frontier(
            candidates, queries, checks, variance,
            retired=frozenset({"n1"}),
            budget=(Fraction(1), Fraction(0)),
            epsilon=Fraction(100), delta=Fraction(1, 10),
        )
        compact = [
            {"basis": item["basis"], "metrics": item["metrics"],
             "covariance": item["covariance"]}
            for item in expected
        ]
        actual_compact = [
            {"basis": item["basis"], "metrics": item["metrics"],
             "covariance": [list(row) for row in item["covariance"]]}
            for item in actual
        ]
        self.assertEqual(actual_compact, compact)
        # The retired-only basis (both aliases) and F/D must both be present.
        bases = {item["basis"] for item in actual}
        self.assertIn(("fresh:F", "reuse:diff/D"), bases)
        self.assertNotIn(("reuse:diff/D", "reuse:seed/B"), bases)


class Algebra(unittest.TestCase):
    def test_solver_reconstruction_randomised(self):
        import random
        from privacy_budget.release_model import solve_all

        random.seed(148)
        for _ in range(400):
            width = random.randint(0, 4)
            size = random.randint(0, 5)
            basis = [
                [Fraction(random.randint(-3, 3)) for _ in range(width)]
                for _ in range(size)
            ]
            targets = [
                [Fraction(random.randint(-3, 3)) for _ in range(width)]
                for _ in range(random.randint(1, 3))
            ]
            solved = solve_all(targets, basis, [str(i) for i in range(size)])
            expected = [oracle.expand(target, basis) for target in targets]
            if any(item is None for item in expected):
                self.assertIsNone(solved)
                continue
            self.assertIsNotNone(solved)
            for position, target in enumerate(targets):
                weights = [solved[position][str(i)] for i in range(size)]
                reconstructed = [
                    sum((weights[i] * basis[i][r] for i in range(size)), Fraction(0))
                    for r in range(width)
                ]
                self.assertEqual(reconstructed, list(target))
