"""Strict data-contract tests for requests, limits and canonical identity."""
import hashlib
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import support
from support import check, frac, fresh, query, request


def canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "catalog.db"
        self.limits = support.write_json(
            self.base / "limits.json",
            {"limits": [{"dataset": "d", "principal": "p", "epsilon": frac(10), "delta": frac(0)}]},
        )
        self.assertEqual(
            support.run_cli("init", "--db", self.db, "--source", self.limits).returncode, 0
        )

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, payload, name="req.json"):
        source = support.write_json(self.base / name, payload)
        return support.run_cli("plan", "--db", self.db, "--source", source)

    def base_payload(self, **overrides):
        payload = request(
            "r", ["x"],
            fresh=[fresh("m", [1])],
            queries=[query("q", [1])],
            checks=[check("k", {"q": 1}, 10)],
        )
        payload.update(overrides)
        return payload

    def assertRejected(self, payload, fragment):
        result = self.plan(payload)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn(fragment, result.stderr)

    def test_undefined_top_level_field(self):
        payload = self.base_payload()
        payload["extra"] = 1
        self.assertRejected(payload, "expected object fields")

    def test_undefined_nested_field(self):
        payload = self.base_payload()
        payload["fresh"][0]["extra"] = 1
        self.assertRejected(payload, "expected object fields")

    def test_duplicate_identifiers(self):
        payload = self.base_payload()
        payload["fresh"].append(fresh("m", [2]))
        self.assertRejected(payload, "duplicate candidate ID")
        payload = self.base_payload()
        payload["queries"].append(query("q", [2]))
        self.assertRejected(payload, "duplicate query ID")
        payload = self.base_payload()
        payload["checks"].append(check("k", {"q": 2}, 10))
        self.assertRejected(payload, "duplicate check ID")

    def test_identifier_charset(self):
        for bad in ("a:b", "a/b", " leading", "trailing "):
            payload = self.base_payload()
            payload["fresh"][0]["id"] = bad
            self.assertRejected(payload, "without ':' or '/'")

    def test_dimensions(self):
        payload = self.base_payload(dimensions=[])
        self.assertRejected(payload, "expected array of length")
        payload = self.base_payload(dimensions=["a"] * 7)
        self.assertRejected(payload, "expected array of length")
        payload = self.base_payload(dimensions=["a", "a"])
        self.assertRejected(payload, "dimension names must be unique")
        payload = self.base_payload(dimensions=["a", "b"])
        self.assertRejected(payload, "coefficient vector must have 2 entries")

    def test_bounded_collections(self):
        payload = self.base_payload()
        payload["queries"] = []
        self.assertRejected(payload, "expected array of length")
        payload = self.base_payload()
        payload["fresh"] = [fresh("m%d" % i, [1]) for i in range(17)]
        self.assertRejected(payload, "expected array of length")
        payload = self.base_payload()
        payload["checks"] = [check("k%d" % i, {"q": 1}, 1) for i in range(17)]
        self.assertRejected(payload, "expected array of length")

    def test_numeric_bounds(self):
        payload = self.base_payload()
        payload["fresh"][0]["value"] = frac(10 ** 12 + 1)
        self.assertRejected(payload, "integer outside allowed bounds")
        payload = self.base_payload()
        payload["fresh"][0]["coefficients"] = [frac(-(10 ** 12 + 1))]
        self.assertRejected(payload, "integer outside allowed bounds")
        payload = self.base_payload()
        payload["fresh"][0]["coefficients"] = [frac(1, 0)]
        self.assertRejected(payload, "integer outside allowed bounds")
        payload = self.base_payload()
        payload["fresh"][0]["variance"] = frac(-1)
        self.assertRejected(payload, "integer outside allowed bounds")
        payload = self.base_payload()
        payload["checks"][0]["max_variance"] = frac(-1)
        self.assertRejected(payload, "integer outside allowed bounds")

    def test_negative_numbers_allowed(self):
        payload = self.base_payload()
        payload["fresh"][0]["coefficients"] = [frac(-3)]
        payload["fresh"][0]["value"] = frac(-5)
        payload["queries"][0]["coefficients"] = [frac(-2)]
        payload["checks"][0]["weights"] = {"q": frac(-1, 2)}
        result = self.plan(payload)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mechanism_parameters(self):
        payload = self.base_payload()
        payload["fresh"][0]["epsilon"] = frac(0)
        self.assertRejected(payload, "epsilon>0")
        payload = self.base_payload()
        payload["fresh"][0]["delta"] = frac(1)
        self.assertRejected(payload, "delta<1")

    def test_unknown_query_weight(self):
        payload = self.base_payload()
        payload["checks"][0]["weights"] = {"nope": frac(1)}
        self.assertRejected(payload, "unknown query")

    def test_duplicate_json_key(self):
        source = self.base / "dup.json"
        source.write_text('{"release_id":"a","release_id":"b"}')
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_nonfinite(self):
        source = self.base / "nan.json"
        source.write_text('{"release_id":NaN}')
        result = support.run_cli("plan", "--db", self.db, "--source", source)
        self.assertEqual(result.returncode, 2)

    def test_init_immutable(self):
        again = support.write_json(
            self.base / "again.json",
            {"limits": [{"dataset": "d", "principal": "p", "epsilon": frac(11), "delta": frac(0)}]},
        )
        result = support.run_cli("init", "--db", self.db, "--source", again)
        self.assertEqual(result.returncode, 2)
        self.assertIn("immutable", result.stderr)
        same = support.run_cli("init", "--db", self.db, "--source", self.limits)
        self.assertEqual(same.returncode, 0)

    def test_init_rejects_duplicate_limits(self):
        duplicate = support.write_json(
            self.base / "dupe.json",
            {
                "limits": [
                    {"dataset": "d", "principal": "p", "epsilon": frac(1), "delta": frac(0)},
                    {"dataset": "d", "principal": "p", "epsilon": frac(2), "delta": frac(0)},
                ]
            },
        )
        db = self.base / "fresh.db"
        result = support.run_cli("init", "--db", db, "--source", duplicate)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)

    def test_canonical_digest_is_order_insensitive(self):
        first = self.base_payload()
        second = json.loads(json.dumps(first))
        second["queries"] = list(reversed(second["queries"]))
        second["checks"] = list(reversed(second["checks"]))
        second["reuse"] = []
        a = self.plan(first, "a.json")
        b = self.plan(second, "b.json")
        self.assertEqual(json.loads(a.stdout)["request_sha256"], json.loads(b.stdout)["request_sha256"])

    def test_choice_id_encoding(self):
        payload = self.base_payload()
        result = self.plan(payload)
        plan = json.loads(result.stdout)
        request_digest = hashlib.sha256(
            canonical(
                {
                    "release_id": "r",
                    "dataset": "d",
                    "principal": "p",
                    "snapshot": "s",
                    "dimensions": ["x"],
                    "fresh": [
                        {
                            "id": "m",
                            "coefficients": ["1"],
                            "value": "0",
                            "variance": "1",
                            "epsilon": "1",
                            "delta": "0",
                        }
                    ],
                    "reuse": [],
                    "queries": [{"id": "q", "coefficients": ["1"]}],
                    "checks": [
                        {"id": "k", "weights": {"q": "1"}, "max_variance": "10"}
                    ],
                }
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(plan["request_sha256"], request_digest)
        expected_choice = hashlib.sha256(
            canonical({"request_sha256": request_digest, "basis": ["fresh:m"]}).encode("utf-8")
        ).hexdigest()
        self.assertEqual(plan["choices"][0]["choice_id"], expected_choice)

    def test_same_release_id_requires_identical_request(self):
        payload = self.base_payload()
        plan = json.loads(self.plan(payload).stdout)
        choice = plan["choices"][0]["choice_id"]
        output = self.base / "report.json"
        first = support.run_cli(
            "publish", "--db", self.db, "--source", self.base / "req.json",
            "--choice", choice, "--output", output,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        # Whitespace-only differences are the same canonical request: replay.
        source = self.base / "pretty.json"
        source.write_text(json.dumps(payload, indent=2))
        replay = support.run_cli(
            "publish", "--db", self.db, "--source", source,
            "--choice", choice, "--output", self.base / "other.json",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(
            json.loads((self.base / "other.json").read_text())["request_sha256"],
            json.loads(output.read_text())["request_sha256"],
        )
        # A semantic change is a different request bound to the same ID.
        changed = json.loads(json.dumps(payload))
        changed["checks"][0]["max_variance"] = frac(11)
        different = support.write_json(self.base / "changed.json", changed)
        second = support.run_cli(
            "publish", "--db", self.db, "--source", different,
            "--choice", choice, "--output", self.base / "third.json",
        )
        self.assertEqual(second.returncode, 2)
        self.assertIn("different request", second.stderr)

    def test_fraction_normalisation_round_trip(self):
        payload = self.base_payload()
        payload["fresh"][0]["value"] = frac(6, 4)
        payload["fresh"][0]["epsilon"] = frac(4, 8)
        payload["queries"][0]["coefficients"] = [frac(6, 2)]
        plan = json.loads(self.plan(payload).stdout)
        choice = plan["choices"][0]
        self.assertEqual(choice["new_epsilon"], "1/2")
        output = self.base / "report.json"
        support.run_cli(
            "publish", "--db", self.db, "--source", self.base / "req.json",
            "--choice", choice["choice_id"], "--output", output,
        )
        report = json.loads(output.read_text())
        # q = 3*x; the single measurement m = x has weight 3.
        self.assertEqual(report["queries"][0]["estimate"], "9/2")
        self.assertEqual(report["queries"][0]["weights"]["fresh:m"], "3")


if __name__ == "__main__":
    unittest.main()
