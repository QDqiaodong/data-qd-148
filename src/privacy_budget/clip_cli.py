"""Command line entry point for clipped microdata delivery.

``python -m privacy_budget.clip_cli`` supports ``init``, ``apply``, ``export``,
``sink-init``, ``consume`` and ``snapshot``.  ``init``/``sink-init``/``consume``
may write their result to stdout.  Every failure exits 2 without changing the
producer/consumer database or overwriting a pre-existing output file.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import clip_api, clip_store
from .clip_model import ClipError, MAX_SEQUENCE

DESCRIPTION = (
    "Deterministic clipped microdata delivery with source batches, immutable "
    "net-difference packets, and a separate consumer receipt chain"
)


def _guard_output(output, inputs, database):
    if output is None or str(output) == "-":
        return
    clip_store.check_paths(database, inputs=inputs, outputs=[output])


def _emit(payload, output):
    if output is not None and str(output) != "-":
        clip_api.deliver(output, payload)
        return
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def _body(args):
    if args.command == "init":
        _guard_output(args.output, [args.source], args.db)
        return clip_api.init(args.db, args.source), args.output, False
    if args.command == "apply":
        _guard_output(args.output, [args.source], args.db)
        # apply delivers the packet to --output itself; nothing goes to stdout.
        _, replay = clip_api.apply(args.db, args.source, args.output)
        return None, None, replay
    if args.command == "export":
        _guard_output(args.output, [], args.db)
        if type(args.sequence) is not int or not 1 <= args.sequence <= MAX_SEQUENCE:
            raise ClipError("sequence outside allowed bounds")
        # export delivers the stored packet to the file only, never to stdout.
        clip_api.export(args.db, args.sequence, args.output)
        return None, None, False
    if args.command == "sink-init":
        _guard_output(args.output, [args.source], args.db)
        return clip_api.sink_init(args.db, args.source), args.output, False
    if args.command == "consume":
        _guard_output(args.output, [args.source], args.db)
        payload, replay = clip_api.consume(args.db, args.source)
        return payload, args.output, replay
    if args.command == "snapshot":
        _guard_output(args.output, [], args.db)
        if args.role == "producer":
            payload = clip_api.producer_snapshot(args.db)
        else:
            payload = clip_api.consumer_snapshot(args.db)
        return payload, args.output, False
    raise ClipError("unknown command")


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--db", required=True)
    init_parser.add_argument("--source", required=True)
    init_parser.add_argument("--output")

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--db", required=True)
    apply_parser.add_argument("--source", required=True)
    apply_parser.add_argument("--output", required=True)

    export_parser = sub.add_parser("export")
    export_parser.add_argument("--db", required=True)
    export_parser.add_argument("--sequence", required=True, type=int)
    export_parser.add_argument("--output", required=True)

    sink_parser = sub.add_parser("sink-init")
    sink_parser.add_argument("--db", required=True)
    sink_parser.add_argument("--source", required=True)
    sink_parser.add_argument("--output")

    consume_parser = sub.add_parser("consume")
    consume_parser.add_argument("--db", required=True)
    consume_parser.add_argument("--source", required=True)
    consume_parser.add_argument("--output")

    snapshot_parser = sub.add_parser("snapshot")
    snapshot_parser.add_argument("--db", required=True)
    snapshot_parser.add_argument("--role", required=True, choices=["producer", "consumer"])
    snapshot_parser.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    try:
        payload, output, replayed = _body(args)
        if payload is not None:
            _emit(payload, output)
        if replayed:
            print("sequence already committed: replaying stored result", file=sys.stderr)
        return 0
    except (
        ClipError,
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
