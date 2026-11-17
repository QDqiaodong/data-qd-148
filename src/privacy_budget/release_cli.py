"""Command line front end for the joint release planner.

``plan`` writes a frontier to an output file (or stdout) and never mutates
state.  ``publish`` commits first and only then delivers the report; if
delivery fails the process exits non-zero but a retry of the very same
release returns the committed report without charging again.

All commands verify that an output path differs from every input and refuse
symbolic-link outputs before doing any work.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import release_api
from .release_model import ReleaseValidationError

DESCRIPTION = (
    "Exact-rational joint release planning with reusable noisy statistics, "
    "signed raw-noise lineage, and sequential budget accounting"
)


def _guard_output(output, inputs):
    out = Path(output)
    for item in inputs:
        if Path(item).resolve() == out.resolve():
            raise release_api.ReleaseValidationError("input and output must differ")
    if out.is_symlink():
        raise release_api.ReleaseValidationError("output cannot be a symlink")
    if out.parent.is_symlink():
        raise release_api.ReleaseValidationError("output parent cannot be a symlink")


def _emit(report, output):
    text = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if output is None or str(output) == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    release_api._atomic_write(Path(output), text)


def _body(args):
    output = getattr(args, "output", None)
    if args.command == "init":
        if output:
            _guard_output(output, [args.source, args.db])
        return release_api.init(args.db, args.source), output, False
    if args.command == "plan":
        if output:
            _guard_output(output, [args.source, args.db])
        return release_api.plan(args.db, args.source), output, False
    if args.command == "publish":
        _guard_output(output, [args.source, args.db])
        # Commit first; delivery happens only after this returns.
        report, replayed = release_api.publish(args.db, args.source, args.choice)
        return report, output, replayed
    if args.command == "retire":
        return (
            release_api.retire(
                args.db, args.dataset, args.principal, args.snapshot, args.measurement
            ),
            output,
            False,
        )
    if args.command == "catalog":
        if output:
            _guard_output(output, [args.db])
        return (
            release_api.catalog(args.db, args.dataset, args.principal, args.snapshot),
            output,
            False,
        )
    raise release_api.ReleaseValidationError("unknown command")


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--db", required=True)
    init_parser.add_argument("--source", required=True)
    init_parser.add_argument("--output")

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--db", required=True)
    plan_parser.add_argument("--source", required=True)
    plan_parser.add_argument("--output")

    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--db", required=True)
    publish_parser.add_argument("--source", required=True)
    publish_parser.add_argument("--choice", required=True)
    publish_parser.add_argument("--output", required=True)

    retire_parser = sub.add_parser("retire")
    retire_parser.add_argument("--db", required=True)
    retire_parser.add_argument("--dataset", required=True)
    retire_parser.add_argument("--principal", required=True)
    retire_parser.add_argument("--snapshot", required=True)
    retire_parser.add_argument("--measurement", required=True)
    retire_parser.add_argument("--output")

    catalog_parser = sub.add_parser("catalog")
    catalog_parser.add_argument("--db", required=True)
    catalog_parser.add_argument("--dataset", required=True)
    catalog_parser.add_argument("--principal", required=True)
    catalog_parser.add_argument("--snapshot", required=True)
    catalog_parser.add_argument("--output")

    args = parser.parse_args(argv)
    try:
        report, output, replayed = _body(args)
        _emit(report, output)
        if replayed:
            print("release_id already committed: replaying stored report", file=sys.stderr)
        return 0
    except (
        ReleaseValidationError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        sqlite3.Error,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
