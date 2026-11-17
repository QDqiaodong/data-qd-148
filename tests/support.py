"""Shared helpers for the joint release planner test suites."""
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def frac(numerator, denominator=1):
    return {"numerator": numerator, "denominator": denominator}


def run_cli(*args, env=None):
    environment = {"PYTHONPATH": str(SRC)}
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "privacy_budget.release_cli", *map(str, args)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
    )


def run_policy_cli(*args, env=None):
    """Drive the v1 entry point with the ``policy`` subcommand."""
    environment = {"PYTHONPATH": str(SRC)}
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "privacy_budget.cli", *map(str, args)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
    )


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return Path(path)


def request(
    release_id,
    dimensions,
    fresh=(),
    reuse=(),
    queries=(),
    checks=(),
    dataset="d",
    principal="p",
    snapshot="s",
):
    return {
        "release_id": release_id,
        "dataset": dataset,
        "principal": principal,
        "snapshot": snapshot,
        "dimensions": list(dimensions),
        "fresh": list(fresh),
        "reuse": list(reuse),
        "queries": list(queries),
        "checks": list(checks),
    }


def fresh(ident, coefficients, value=0, variance=1, epsilon=1, delta=0):
    return {
        "id": ident,
        "coefficients": [frac(c) if isinstance(c, int) else c for c in coefficients],
        "value": frac(value) if isinstance(value, int) else value,
        "variance": frac(variance) if isinstance(variance, int) else variance,
        "epsilon": frac(epsilon) if isinstance(epsilon, int) else epsilon,
        "delta": frac(delta) if isinstance(delta, int) else delta,
    }


def query(ident, coefficients):
    return {
        "id": ident,
        "coefficients": [frac(c) if isinstance(c, int) else c for c in coefficients],
    }


def check(ident, weights, max_variance):
    return {
        "id": ident,
        "weights": {
            key: frac(value) if isinstance(value, int) else value
            for key, value in weights.items()
        },
        "max_variance": frac(max_variance) if isinstance(max_variance, int) else max_variance,
    }


def check_public_weights(ident, weights, max_variance):
    return check(ident, weights, max_variance)


def parse_plan(payload):
    """Turn a plan payload into comparable Python structures."""
    choices = []
    for record in payload["choices"]:
        choices.append(
            {
                "basis": tuple(record["basis"]),
                "new_epsilon": Fraction(record["new_epsilon"]),
                "new_delta": Fraction(record["new_delta"]),
                "check_variances": tuple(
                    Fraction(value) for value in record["check_variances"]
                ),
                "metrics": (
                    Fraction(record["metrics"]["new_epsilon"]),
                    Fraction(record["metrics"]["new_delta"]),
                    *(
                        Fraction(record["metrics"]["check_variances"][key])
                        for key in sorted(record["metrics"]["check_variances"])
                    ),
                ),
                "covariance": tuple(
                    tuple(Fraction(value) for value in row)
                    for row in record["covariance"]
                ),
            }
        )
    return choices
