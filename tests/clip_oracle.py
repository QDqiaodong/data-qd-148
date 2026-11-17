"""Independent full-reclip oracle for the microdata clipping pipeline.

The oracle deliberately does not import or call the production clipping
functions.  Each batch it rebuilds the complete expected clipped row set from
the full source state with its own selection code, then checks the delivered
packet, the producer snapshot and the consumer state.
"""
from __future__ import annotations

import hashlib
import json


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def row_key(row):
    return (row["priority"], row["id"])


def expected_clip(rows, max_partitions, max_rows):
    """Full re-rank from the complete source state; no production helpers."""
    units = {}
    for row in rows.values():
        units.setdefault(row["unit"], {}).setdefault(row["partition"], []).append(row)
    chosen = {}
    for unit_name, partitions in units.items():
        ranking = []
        for partition_name, members in partitions.items():
            first = sorted((row["priority"], row["id"]) for row in members)[0]
            ranking.append((first[0], first[1], partition_name))
        ranking.sort()
        for _, _, partition_name in ranking[:max_partitions]:
            members = sorted(
                partitions[partition_name], key=lambda row: (row["priority"], row["id"])
            )
            for row in members[:max_rows]:
                chosen[row["id"]] = dict(row)
    return chosen


def rows_sha(rows):
    return digest([rows[key] for key in sorted(rows)])


def net_changes(before, after):
    result = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        result.append({"id": key, "before": old, "after": new})
    return result


class Oracle:
    """Track expected source, clipped rows, cursor and packet chain."""

    def __init__(self, stream_id, max_partitions, max_rows):
        self.stream_id = stream_id
        self.max_partitions = max_partitions
        self.max_rows = max_rows
        self.config_sha256 = digest(
            {
                "stream_id": stream_id,
                "max_partitions": max_partitions,
                "max_rows": max_rows,
            }
        )
        self.source = {}
        self.clip = {}
        self.sequence = 0
        self.last_packet = "0" * 64
        self.consumer_rows = {}
        self.consumer_sequence = 0
        self.consumer_last_packet = "0" * 64
        self.packets = []

    def apply_batch(self, batch):
        before_clip = dict(self.clip)
        for change in batch["changes"]:
            key = change["id"]
            current = self.source.get(key)
            if change["before"] is None:
                assert current is None, "oracle insert over existing id"
            else:
                assert current == change["before"], "oracle before mismatch"
            if change["after"] is None:
                self.source.pop(key, None)
            else:
                self.source[key] = dict(change["after"])
        self.sequence += 1
        self.clip = expected_clip(self.source, self.max_partitions, self.max_rows)
        changes = net_changes(before_clip, self.clip)
        ordered = sorted(batch["changes"], key=lambda change: change["id"])
        packet = {
            "stream_id": self.stream_id,
            "config_sha256": self.config_sha256,
            "sequence": self.sequence,
            "previous_packet_sha256": self.last_packet,
            "before_rows_sha256": rows_sha(before_clip),
            "after_rows_sha256": rows_sha(self.clip),
            "batch_sha256": digest(
                {
                    "stream_id": self.stream_id,
                    "sequence": self.sequence,
                    "changes": ordered,
                }
            ),
            "changes": changes,
        }
        self.last_packet = digest(packet)
        packet["packet_sha256"] = self.last_packet
        self.packets.append(packet)
        self.consumer_rows = dict(self.clip)
        self.consumer_sequence = self.sequence
        self.consumer_last_packet = self.last_packet
        return packet
