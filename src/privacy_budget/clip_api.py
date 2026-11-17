"""Transactional API for clipped microdata delivery.

Producer side
-------------
``init`` / ``apply`` / ``export`` / ``snapshot``.  ``apply`` recomputes the
clipping from the *complete* post-batch source state, then commits the source
rows, the clipped rows, the sequence cursor and the immutable packet in one
``BEGIN IMMEDIATE`` transaction.  Delivery to a file happens only after the
commit, so a failed write never rolls back a committed batch; ``export``
replays the stored packet after any restart.

Consumer side
-------------
``sink_init`` / ``consume`` / ``snapshot``.  The consumer database is separate;
it never reads producer state, never re-clips, and only validates a frozen
packet against its own config, chain cursor, before row set and digests before
committing the whole diff and a receipt atomically.

The fault-injection hook is an explicit, test-only callback installed through
:func:`set_fault_hook`.  It is called *after* the durable commit but before the
caller writes the output file, so a test can crash the process at that exact
boundary and verify recovery from committed state rather than from a
self-reported flag.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import clip_store
from .clip_model import (
    MAX_PACKET_BYTES,
    ClipConfig,
    ClipError,
    Packet,
    apply_changes,
    batch_sha256,
    build_packet,
    check_before,
    clip_rows,
    config_sha256,
    packet_bytes,
    parse_batch,
    parse_config,
    parse_packet,
    rowset_sha256,
    rows_payload,
)
from .common import read

PRODUCER = "producer"
CONSUMER = "consumer"

_fault_hook = None


def set_fault_hook(hook):
    """Install (or clear) the test-only post-commit/pre-delivery hook."""
    global _fault_hook
    _fault_hook = hook


def _fault(point):
    if _fault_hook is not None:
        _fault_hook(point)


# ---------------------------------------------------------------------------
# output delivery


def _atomic_bytes(destination, data):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, delete=False) as stream:
            handle = stream.name
            stream.write(data)
        os.replace(handle, destination)
    finally:
        if handle and os.path.exists(handle):
            os.unlink(handle)


def deliver(output, payload):
    """Atomically write readable JSON; refuse symlinked outputs."""
    destination = Path(output)
    if clip_store._symlink_in_chain(destination):
        raise ClipError("output path cannot traverse a symlink")
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    _atomic_bytes(destination, text.encode("utf-8"))


def deliver_packet(output, packet):
    """Write the packet in the exact canonical form the consumer reads.

    Compact canonical bytes (no trailing newline, no indentation) keep the
    delivered file byte-for-byte equal to the digest input, so the 8 MB
    consumable limit checked by :func:`apply` applies to the file as well.
    """
    destination = Path(output)
    if clip_store._symlink_in_chain(destination):
        raise ClipError("output path cannot traverse a symlink")
    data = packet_bytes(packet)
    if len(data) > MAX_PACKET_BYTES:
        raise ClipError("canonical packet exceeds the 8 MB consumable limit")
    _atomic_bytes(destination, data)


# ---------------------------------------------------------------------------
# producer


def _config_from_meta(meta):
    return ClipConfig(
        stream_id=meta["stream_id"],
        max_partitions=meta["max_partitions"],
        max_rows=meta["max_rows"],
    )


def _stored_config(meta):
    return {
        "stream_id": meta["stream_id"],
        "max_partitions": meta["max_partitions"],
        "max_rows": meta["max_rows"],
        "config_sha256": meta["config_sha256"],
    }


def init(db, source):
    """Create the immutable producer config and empty initial state."""
    clip_store.check_paths(db, inputs=[source])
    data, digest = read(source)
    config = parse_config(data)
    created = clip_store.initialise(db, PRODUCER, config)
    return {
        "schema": "privacy-budget-clip-init-v1",
        "role": PRODUCER,
        "source_sha256": digest,
        "created": created,
        **_stored_config(
            {"stream_id": config.stream_id, "max_partitions": config.max_partitions,
             "max_rows": config.max_rows, "config_sha256": config_sha256(config)}
        ),
    }


def _decode_packet(stored):
    """Rebuild a stored packet, converting its stored change objects."""
    from .clip_model import Change, Row

    def row(value):
        return None if value is None else Row(**value)

    changes = tuple(
        Change(item["id"], row(item["before"]), row(item["after"]))
        for item in stored["changes"]
    )
    return Packet(
        stream_id=stored["stream_id"],
        config_sha256=stored["config_sha256"],
        sequence=stored["sequence"],
        previous_packet_sha256=stored["previous_packet_sha256"],
        before_rows_sha256=stored["before_rows_sha256"],
        after_rows_sha256=stored["after_rows_sha256"],
        batch_sha256=stored["batch_sha256"],
        changes=changes,
        packet_sha256=stored["packet_sha256"],
    )


def apply(db, source, output):
    """Commit one source batch and its net-diff packet, then deliver."""
    clip_store.check_paths(db, inputs=[source], outputs=[output])
    data, _ = read(source)
    connection = clip_store.connect(db, PRODUCER)
    packet = None
    replay = False
    committed = False
    try:
        meta = clip_store.load_meta(connection)
        config = _config_from_meta(meta)
        batch = parse_batch(data, config.stream_id)
        batch_digest = batch_sha256(batch)
        connection.execute("BEGIN IMMEDIATE")
        try:
            meta = clip_store.load_meta(connection)
            previous = connection.execute(
                "SELECT packet_json FROM packets WHERE sequence=?", (batch.sequence,)
            ).fetchone()
            if previous is not None:
                stored = json.loads(previous["packet_json"])
                if (
                    stored["stream_id"] != batch.stream_id
                    or stored.get("batch_sha256") != batch_digest
                ):
                    raise ClipError(
                        "sequence is already bound to a different batch"
                    )
                packet = _decode_packet(stored)
                # Identical retry: return the original packet without re-checking
                # any historical before values or re-clipping.
                connection.execute("COMMIT")
                committed = True
                replay = True
            else:
                expected = meta["sequence"] + 1
                if batch.sequence != expected:
                    raise ClipError(
                        "sequence must be contiguous: expected %d, got %d"
                        % (expected, batch.sequence)
                    )
                source_rows = clip_store.load_source(connection)
                check_before(source_rows, batch.changes)
                after_source = apply_changes(source_rows, batch.changes)
                before_clip = clip_store.load_rows(connection)
                after_clip = clip_rows(config, after_source)
                packet = build_packet(
                    config,
                    batch.sequence,
                    meta["last_packet_sha256"],
                    before_clip,
                    after_clip,
                    batch_digest,
                )
                if len(packet_bytes(packet)) > MAX_PACKET_BYTES:
                    raise ClipError(
                        "canonical packet exceeds the 8 MB consumable limit"
                    )
                _fault("producer_prepare")
                clip_store.replace_rows(connection, "source_rows", after_source)
                clip_store.replace_rows(connection, "clip_rows", after_clip)
                connection.execute(
                    "UPDATE clip_meta SET sequence=?, last_packet_sha256=? WHERE id=1",
                    (batch.sequence, packet.packet_sha256),
                )
                connection.execute(
                    "INSERT INTO packets(sequence,batch_sha256,packet_sha256,packet_json)"
                    " VALUES (?,?,?,?)",
                    (
                        batch.sequence,
                        batch_digest,
                        packet.packet_sha256,
                        json.dumps(packet.payload(), sort_keys=True, ensure_ascii=False),
                    ),
                )
                connection.execute("COMMIT")
                committed = True
        except BaseException:
            if not committed:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    assert packet is not None
    _fault("producer_committed")
    deliver_packet(output, packet)
    return packet, replay


def _packet_from_storage(row):
    return _decode_packet(json.loads(row["packet_json"]))


def export(db, sequence, output):
    """Return the committed packet for a sequence; never rewrite history."""
    clip_store.check_paths(db, outputs=[output])
    connection = clip_store.open_readonly(db, PRODUCER)
    try:
        row = connection.execute(
            "SELECT packet_json FROM packets WHERE sequence=?", (sequence,)
        ).fetchone()
        meta = clip_store.load_meta(connection)
    finally:
        connection.close()
    if row is None:
        raise ClipError("no committed packet for sequence %d" % sequence)
    packet = _packet_from_storage(row)
    deliver_packet(output, packet)
    return packet


def producer_snapshot(db):
    connection = clip_store.open_readonly(db, PRODUCER)
    try:
        meta = clip_store.load_meta(connection)
        rows = clip_store.load_rows(connection)
    finally:
        connection.close()
    return {
        "stream_id": meta["stream_id"],
        "config_sha256": meta["config_sha256"],
        "sequence": meta["sequence"],
        "rows": rows_payload(rows),
        "rows_sha256": rowset_sha256(rows),
    }


# ---------------------------------------------------------------------------
# consumer


def sink_init(db, source):
    """Create the separate consumer config and empty initial state."""
    clip_store.check_paths(db, inputs=[source])
    data, digest = read(source)
    config = parse_config(data)
    created = clip_store.initialise(db, CONSUMER, config)
    return {
        "schema": "privacy-budget-clip-sink-init-v1",
        "role": CONSUMER,
        "source_sha256": digest,
        "created": created,
        **_stored_config(
            {"stream_id": config.stream_id, "max_partitions": config.max_partitions,
             "max_rows": config.max_rows, "config_sha256": config_sha256(config)}
        ),
    }


def _receipt_payload(meta, packet):
    return {
        "stream_id": packet.stream_id,
        "sequence": packet.sequence,
        "packet_sha256": packet.packet_sha256,
        "after_rows_sha256": packet.after_rows_sha256,
        "config_sha256": packet.config_sha256,
        "previous_packet_sha256": packet.previous_packet_sha256,
        "last_packet_sha256": packet.packet_sha256,
    }


def consume(db, source):
    """Apply one frozen packet to the consumer, atomically with its receipt."""
    clip_store.check_paths(db, inputs=[source])
    data, _ = read(source)
    packet = parse_packet(data)
    connection = clip_store.connect(db, CONSUMER)
    receipt = None
    replay = False
    committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            meta = clip_store.load_meta(connection)
            if packet.stream_id != meta["stream_id"]:
                raise ClipError("packet stream_id does not match the consumer config")
            if packet.config_sha256 != meta["config_sha256"]:
                raise ClipError("packet config_sha256 does not match the consumer config")
            stored = connection.execute(
                "SELECT receipt_json FROM receipts WHERE sequence=?", (packet.sequence,)
            ).fetchone()
            if stored is not None:
                original = json.loads(stored["receipt_json"])
                if original["packet_sha256"] != packet.packet_sha256:
                    raise ClipError(
                        "sequence is already bound to a different packet"
                    )
                # Same sequence and same packet: return the original receipt even
                # if the consumer has since advanced.
                connection.execute("COMMIT")
                committed = True
                receipt = original
                replay = True
            else:
                if packet.sequence != meta["sequence"] + 1:
                    raise ClipError(
                        "packet breaks the receipt chain: expected sequence %d, got %d"
                        % (meta["sequence"] + 1, packet.sequence)
                    )
                if packet.previous_packet_sha256 != meta["last_packet_sha256"]:
                    raise ClipError("packet previous_packet_sha256 does not chain")
                if packet.sequence == 1:
                    if packet.previous_packet_sha256 != clip_store.ZERO_DIGEST:
                        raise ClipError("first packet must start at the zero digest")
                rows = clip_store.load_rows(connection)
                if rowset_sha256(rows) != packet.before_rows_sha256:
                    raise ClipError("packet before_rows_sha256 does not match consumer rows")
                check_before(rows, packet.changes)
                after = apply_changes(rows, packet.changes)
                if rowset_sha256(after) != packet.after_rows_sha256:
                    raise ClipError("applied rows do not match after_rows_sha256")
                _fault("consumer_applying")
                clip_store.replace_rows(connection, "clip_rows", after)
                connection.execute(
                    "UPDATE clip_meta SET sequence=?, last_packet_sha256=? WHERE id=1",
                    (packet.sequence, packet.packet_sha256),
                )
                receipt = _receipt_payload(meta, packet)
                connection.execute(
                    "INSERT INTO receipts(sequence,packet_sha256,after_rows_sha256,receipt_json)"
                    " VALUES (?,?,?,?)",
                    (
                        packet.sequence,
                        packet.packet_sha256,
                        packet.after_rows_sha256,
                        json.dumps(receipt, sort_keys=True, ensure_ascii=False),
                    ),
                )
                _fault("consumer_precommit")
                connection.execute("COMMIT")
                committed = True
        except BaseException:
            if not committed:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    _fault("consumer_committed")
    return receipt, replay


def consumer_snapshot(db):
    connection = clip_store.open_readonly(db, CONSUMER)
    try:
        meta = clip_store.load_meta(connection)
        rows = clip_store.load_rows(connection)
    finally:
        connection.close()
    return {
        "stream_id": meta["stream_id"],
        "config_sha256": meta["config_sha256"],
        "sequence": meta["sequence"],
        "last_packet_sha256": meta["last_packet_sha256"],
        "rows": rows_payload(rows),
        "rows_sha256": rowset_sha256(rows),
    }
