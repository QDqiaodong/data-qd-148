"""Pure contract, clipping and canonical-packet logic for microdata delivery.

Nothing in this module performs I/O.  It parses the strict configuration,
source-batch and delivery-packet contracts, computes the deterministic
per-unit partition/row clipping from the *complete* current source state, and
builds the net-difference packet that is committed by the producer store.

The clipping policy is a deterministic selection rule only.  It does not
compute a privacy budget, add noise, evaluate a counterfactual influence, or
claim that clipping makes a release differentially private.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional
import hashlib
import json

from .common import ident

MAX_PARTITIONS = 100
MAX_ROWS = 1000
MAX_ABS_INTEGER = 10 ** 12
MAX_SEQUENCE = 2 ** 63 - 1
MAX_PACKET_BYTES = 8_000_000

ROW_FIELDS = "id unit partition priority value"
CHANGE_FIELDS = "id before after"
BATCH_FIELDS = "stream_id sequence changes"
CONFIG_FIELDS = "stream_id max_partitions max_rows"
PACKET_FIELDS = (
    "stream_id config_sha256 sequence previous_packet_sha256 "
    "before_rows_sha256 after_rows_sha256 batch_sha256 changes packet_sha256"
)


class ClipError(ValueError):
    """A clip configuration, batch, packet, or state binding is invalid."""


@dataclass(frozen=True)
class Row:
    id: str
    unit: str
    partition: str
    priority: int
    value: int


@dataclass(frozen=True)
class ClipConfig:
    stream_id: str
    max_partitions: int
    max_rows: int


@dataclass(frozen=True)
class Change:
    id: str
    before: Optional[Row]
    after: Optional[Row]


@dataclass(frozen=True)
class Batch:
    stream_id: str
    sequence: int
    changes: tuple


@dataclass(frozen=True)
class Packet:
    stream_id: str
    config_sha256: str
    sequence: int
    previous_packet_sha256: str
    before_rows_sha256: str
    after_rows_sha256: str
    batch_sha256: str
    changes: tuple
    packet_sha256: str

    def payload(self):
        """Canonical packet object, including the self digest field."""
        return {
            "stream_id": self.stream_id,
            "config_sha256": self.config_sha256,
            "sequence": self.sequence,
            "previous_packet_sha256": self.previous_packet_sha256,
            "before_rows_sha256": self.before_rows_sha256,
            "after_rows_sha256": self.after_rows_sha256,
            "batch_sha256": self.batch_sha256,
            "changes": [change_payload(change) for change in self.changes],
            "packet_sha256": self.packet_sha256,
        }


# ---------------------------------------------------------------------------
# strict field parsing


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ClipError("expected object fields: " + expected + " (" + label + ")")
    return value


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ClipError("integer outside allowed bounds: " + label)
    return value


def _digest(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ClipError("expected 64 lowercase hexadecimal digest: " + label)
    return value


def _row(value, label):
    _fields(value, ROW_FIELDS, label)
    return Row(
        id=ident(value["id"]),
        unit=ident(value["unit"]),
        partition=ident(value["partition"]),
        priority=_integer(value["priority"], -MAX_ABS_INTEGER, MAX_ABS_INTEGER, label + ".priority"),
        value=_integer(value["value"], -MAX_ABS_INTEGER, MAX_ABS_INTEGER, label + ".value"),
    )


def parse_config(data):
    _fields(data, CONFIG_FIELDS, "config")
    return ClipConfig(
        stream_id=ident(data["stream_id"]),
        max_partitions=_integer(data["max_partitions"], 1, MAX_PARTITIONS, "max_partitions"),
        max_rows=_integer(data["max_rows"], 1, MAX_ROWS, "max_rows"),
    )


def parse_batch(data, expected_stream):
    """Parse a source batch and bind it to the configured stream."""
    _fields(data, BATCH_FIELDS, "batch")
    stream_id = ident(data["stream_id"])
    if stream_id != expected_stream:
        raise ClipError("batch stream_id does not match the configured stream")
    sequence = _integer(data["sequence"], 1, MAX_SEQUENCE, "sequence")
    raw_changes = data["changes"]
    if not isinstance(raw_changes, list):
        raise ClipError("changes must be an array")
    seen = set()
    changes = []
    for index, item in enumerate(raw_changes):
        _fields(item, CHANGE_FIELDS, "changes[%d]" % index)
        change_id = ident(item["id"])
        if change_id in seen:
            raise ClipError("duplicate id within batch: " + change_id)
        seen.add(change_id)
        before = None if item["before"] is None else _row(item["before"], "changes[%d].before" % index)
        after = None if item["after"] is None else _row(item["after"], "changes[%d].after" % index)
        if before is None and after is None:
            raise ClipError("change must have at least one side: " + change_id)
        if before is not None and before.id != change_id:
            raise ClipError("before row id does not match change id: " + change_id)
        if after is not None and after.id != change_id:
            raise ClipError("after row id does not match change id: " + change_id)
        changes.append(Change(change_id, before, after))
    changes.sort(key=lambda change: change.id)
    return Batch(stream_id, sequence, tuple(changes))


def parse_packet(data):
    """Parse and fully canonicalise a delivery packet."""
    _fields(data, PACKET_FIELDS, "packet")
    stream_id = ident(data["stream_id"])
    config_sha256 = _digest(data["config_sha256"], "config_sha256")
    sequence = _integer(data["sequence"], 1, MAX_SEQUENCE, "sequence")
    previous = _digest(data["previous_packet_sha256"], "previous_packet_sha256")
    before_sha = _digest(data["before_rows_sha256"], "before_rows_sha256")
    after_sha = _digest(data["after_rows_sha256"], "after_rows_sha256")
    batch_sha = _digest(data["batch_sha256"], "batch_sha256")
    packet_sha = _digest(data["packet_sha256"], "packet_sha256")
    raw_changes = data["changes"]
    if not isinstance(raw_changes, list):
        raise ClipError("packet changes must be an array")
    changes = []
    seen = set()
    for index, item in enumerate(raw_changes):
        _fields(item, CHANGE_FIELDS, "packet.changes[%d]" % index)
        change_id = ident(item["id"])
        if change_id in seen:
            raise ClipError("duplicate id in packet changes: " + change_id)
        seen.add(change_id)
        before = None if item["before"] is None else _row(item["before"], "packet.changes[%d].before" % index)
        after = None if item["after"] is None else _row(item["after"], "packet.changes[%d].after" % index)
        if before is None and after is None:
            raise ClipError("packet change must have at least one side: " + change_id)
        if before is not None and before.id != change_id:
            raise ClipError("packet before row id mismatch: " + change_id)
        if after is not None and after.id != change_id:
            raise ClipError("packet after row id mismatch: " + change_id)
        if before is not None and after is not None and before == after:
            raise ClipError("equal packet change ends must not appear: " + change_id)
        changes.append(Change(change_id, before, after))
    # Key order and change order never affect canonical identity.
    changes.sort(key=lambda change: change.id)
    packet = Packet(
        stream_id=stream_id,
        config_sha256=config_sha256,
        sequence=sequence,
        previous_packet_sha256=previous,
        before_rows_sha256=before_sha,
        after_rows_sha256=after_sha,
        batch_sha256=batch_sha,
        changes=tuple(changes),
        packet_sha256=packet_sha,
    )
    if packet_digest(packet) != packet_sha:
        raise ClipError("packet_sha256 does not match canonical packet content")
    return packet


# ---------------------------------------------------------------------------
# canonical JSON and digests


def canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def row_payload(row):
    return {
        "id": row.id,
        "unit": row.unit,
        "partition": row.partition,
        "priority": row.priority,
        "value": row.value,
    }


def change_payload(change):
    return {
        "id": change.id,
        "before": None if change.before is None else row_payload(change.before),
        "after": None if change.after is None else row_payload(change.after),
    }


def config_payload(config):
    return {
        "stream_id": config.stream_id,
        "max_partitions": config.max_partitions,
        "max_rows": config.max_rows,
    }


def config_sha256(config):
    return sha256_bytes(canonical_bytes(config_payload(config)))


def batch_payload(batch):
    return {
        "stream_id": batch.stream_id,
        "sequence": batch.sequence,
        "changes": [change_payload(change) for change in batch.changes],
    }


def batch_sha256(batch):
    return sha256_bytes(canonical_bytes(batch_payload(batch)))


def rows_payload(rows: Mapping[str, Row]):
    return [row_payload(rows[key]) for key in sorted(rows)]


def rowset_sha256(rows: Mapping[str, Row]):
    return sha256_bytes(canonical_bytes(rows_payload(rows)))


def empty_rowset_sha256():
    return rowset_sha256({})


def packet_digest(packet):
    payload = packet.payload()
    del payload["packet_sha256"]
    return sha256_bytes(canonical_bytes(payload))


def packet_bytes(packet):
    return canonical_bytes(packet.payload())


# ---------------------------------------------------------------------------
# clipping


def clip_rows(config: ClipConfig, rows: Mapping[str, Row]) -> Dict[str, Row]:
    """Select partitions and rows from the complete current source rows.

    Per unit the partition order key is ``(min(priority), min-id-at-that-priority,
    partition_name)``.  The first ``max_partitions`` partitions are selected,
    then within each selected partition the first ``max_rows`` rows by
    ``(priority, id)`` are retained.  ``value`` never participates.
    """
    by_unit: Dict[str, Dict[str, list]] = {}
    for row in rows.values():
        by_unit.setdefault(row.unit, {}).setdefault(row.partition, []).append(row)
    selected: Dict[str, Row] = {}
    for unit in sorted(by_unit):
        partitions = by_unit[unit]
        order = []
        for partition_name, members in partitions.items():
            first = min(members, key=lambda row: (row.priority, row.id))
            order.append(((first.priority, first.id, partition_name), partition_name))
        order.sort(key=lambda item: item[0])
        for _, partition_name in order[: config.max_partitions]:
            members = sorted(partitions[partition_name], key=lambda row: (row.priority, row.id))
            for row in members[: config.max_rows]:
                selected[row.id] = row
    return selected


# ---------------------------------------------------------------------------
# diffs


def apply_changes(rows: Mapping[str, Row], changes: Iterable[Change]) -> Dict[str, Row]:
    """Apply already-validated changes; before values are checked by caller."""
    updated = dict(rows)
    for change in changes:
        if change.after is None:
            updated.pop(change.id, None)
        else:
            updated[change.id] = change.after
    return updated


def check_before(rows: Mapping[str, Row], changes: Iterable[Change]):
    """Verify every before against the pre-batch source state."""
    for change in changes:
        current = rows.get(change.id)
        if change.before is None:
            if current is not None:
                raise ClipError("insert requires an absent id: " + change.id)
        elif current != change.before:
            raise ClipError("before row does not match current source: " + change.id)


def net_diff(before: Mapping[str, Row], after: Mapping[str, Row]):
    """Sorted full-row net difference between two clipped row sets."""
    result = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        result.append(Change(key, old, new))
    return tuple(result)


def build_packet(
    config: ClipConfig,
    sequence: int,
    previous_packet_sha256: str,
    before_clip: Mapping[str, Row],
    after_clip: Mapping[str, Row],
    batch_sha: str,
) -> Packet:
    packet = Packet(
        stream_id=config.stream_id,
        config_sha256=config_sha256(config),
        sequence=sequence,
        previous_packet_sha256=previous_packet_sha256,
        before_rows_sha256=rowset_sha256(before_clip),
        after_rows_sha256=rowset_sha256(after_clip),
        batch_sha256=batch_sha,
        changes=net_diff(before_clip, after_clip),
        packet_sha256="",
    )
    return Packet(
        stream_id=packet.stream_id,
        config_sha256=packet.config_sha256,
        sequence=packet.sequence,
        previous_packet_sha256=packet.previous_packet_sha256,
        before_rows_sha256=packet.before_rows_sha256,
        after_rows_sha256=packet.after_rows_sha256,
        batch_sha256=packet.batch_sha256,
        changes=packet.changes,
        packet_sha256=packet_digest(packet),
    )
