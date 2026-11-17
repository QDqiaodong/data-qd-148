"""Acceptance tests for clipped microdata delivery and its recovery boundaries."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import clip_batch, clip_change, clip_config, clip_row, run_clip_cli

from clip_oracle import Oracle, canonical, digest, expected_clip, rows_sha

sys.path.insert(0, str(support.SRC))

from privacy_budget import clip_api, clip_model  # noqa: E402


def _packet_error(packet, field, fragment=None):
    """Mutate a copy of a packet so its self digest stays valid."""
    body = dict(packet)
    body.pop("packet_sha256", None)
    body[field] = fragment
    body["packet_sha256"] = digest(body)
    return body


class ClipHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "producer.db"
        self.sink = self.base / "consumer.db"
        self.config_path = support.write_json(self.base / "config.json", clip_config())
        result = run_clip_cli("init", "--db", self.db, "--source", self.config_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_clip_cli("sink-init", "--db", self.sink, "--source", self.config_path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        clip_api.set_fault_hook(None)
        self.tmp.cleanup()

    # helpers ---------------------------------------------------------------
    def apply(self, batch, name="batch.json", expect=0):
        source = support.write_json(self.base / name, batch)
        output = self.base / (Path(name).stem + "-packet.json")
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return json.loads(output.read_text()) if expect == 0 else result

    def consume(self, packet, name="packet.json", expect=0):
        source = support.write_json(self.base / name, packet)
        output = self.base / (Path(name).stem + "-receipt.json")
        result = run_clip_cli(
            "consume", "--db", self.sink, "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return json.loads(output.read_text()) if expect == 0 else result

    def snapshot(self, db, role):
        output = self.base / ("snap-%s-%s.json" % (role, db.stem))
        result = run_clip_cli(
            "snapshot", "--db", db, "--role", role, "--output", output
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output.read_text())

    def producer_snapshot(self):
        return self.snapshot(self.db, "producer")

    def consumer_snapshot(self):
        return self.snapshot(self.sink, "consumer")


class AcceptanceScenario(ClipHarness):
    def test_partition_fallback_and_replay(self):
        first = clip_batch(
            "s", 1,
            [
                clip_change("a", None, clip_row("a", "U", "p", 1, 10)),
                clip_change("b", None, clip_row("b", "U", "p", 2, 20)),
                clip_change("c", None, clip_row("c", "U", "q", 3, 30)),
            ],
        )
        packet1 = self.apply(first, "b1.json")
        self.assertEqual([change["id"] for change in packet1["changes"]], ["a"])

        second = clip_batch(
            "s", 2,
            [
                clip_change(
                    "a",
                    clip_row("a", "U", "p", 1, 10),
                    clip_row("a", "V", "p", 1, 10),
                ),
                clip_change("b", clip_row("b", "U", "p", 2, 20), None),
            ],
        )
        packet2 = self.apply(second, "b2.json")
        self.assertEqual(
            [change["id"] for change in packet2["changes"]], ["a", "c"]
        )
        self.assertEqual(
            packet2["changes"][0]["after"],
            clip_row("a", "V", "p", 1, 10),
        )
        self.assertIsNone(packet2["changes"][1]["before"])

        producer = self.producer_snapshot()
        self.assertEqual(producer["sequence"], 2)
        self.assertEqual(
            {row["id"]: row for row in producer["rows"]},
            {"a": clip_row("a", "V", "p", 1, 10), "c": clip_row("c", "U", "q", 3, 30)},
        )

        receipt = self.consume(packet1, "p1.json")
        receipt2 = self.consume(packet2, "p2.json")
        self.assertEqual(receipt2["after_rows_sha256"], producer["rows_sha256"])
        consumer = self.consumer_snapshot()
        self.assertEqual(consumer["rows_sha256"], producer["rows_sha256"])
        self.assertEqual(consumer["sequence"], 2)
        self.assertEqual(consumer["last_packet_sha256"], packet2["packet_sha256"])

        replay = self.consume(packet2, "p2-replay.json")
        self.assertEqual(replay, receipt2)
        self.assertEqual(self.consumer_snapshot()["sequence"], 2)
        # An older, already applied packet also returns its original receipt.
        older = self.consume(packet1, "p1-replay.json")
        self.assertEqual(older, receipt)
        self.assertEqual(self.consumer_snapshot()["sequence"], 2)

    def test_empty_net_diff_is_still_committed_and_delivered(self):
        first = clip_batch(
            "s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]
        )
        packet1 = self.apply(first, "b1.json")
        second = clip_batch(
            "s", 2,
            [
                clip_change("b", None, clip_row("b", "U", "r", 9)),
            ],
        )
        packet2 = self.apply(second, "b2.json")
        self.assertEqual(packet2["changes"], [])
        self.assertEqual(packet2["before_rows_sha256"], packet2["after_rows_sha256"])
        self.assertNotEqual(packet2["packet_sha256"], packet1["packet_sha256"])
        self.assertEqual(self.producer_snapshot()["sequence"], 2)
        replay = self.consume(packet1, "p1.json")
        receipt2 = self.consume(packet2, "p2.json")
        self.assertEqual(receipt2["after_rows_sha256"], packet2["after_rows_sha256"])
        self.assertEqual(self.consumer_snapshot()["sequence"], 2)


class OracleFuzz(ClipHarness):
    def test_random_batches_match_independent_oracle(self):
        import random

        rng = random.Random(20261117)
        config = {"stream_id": "s", "max_partitions": 2, "max_rows": 2}
        base = self.base / "fuzz"
        base.mkdir()
        db = base / "producer.db"
        sink = base / "consumer.db"
        config_path = support.write_json(base / "config.json", config)
        self.assertEqual(
            run_clip_cli("init", "--db", db, "--source", config_path).returncode, 0
        )
        self.assertEqual(
            run_clip_cli("sink-init", "--db", sink, "--source", config_path).returncode,
            0,
        )
        oracle = Oracle("s", 2, 2)
        units = ["U", "V", "W"]
        partitions = ["p", "q", "r"]
        priorities = [-5, 0, 1, 2, 2, 7]
        live = {}
        packets = []
        for sequence in range(1, 16):
            changes = []
            known = list(live)
            rng.shuffle(known)
            for ident in known[: rng.randint(0, 2)]:
                roll = rng.random()
                if roll < 0.3:
                    changes.append(clip_change(ident, live[ident], None))
                    live.pop(ident)
                elif roll < 0.6:
                    updated = dict(live[ident])
                    updated["value"] = rng.randint(-1000, 1000)
                    changes.append(clip_change(ident, live[ident], updated))
                    live[ident] = updated
                else:
                    updated = dict(live[ident])
                    updated["partition"] = rng.choice(partitions)
                    updated["unit"] = rng.choice(units)
                    updated["priority"] = rng.choice(priorities)
                    changes.append(clip_change(ident, live[ident], updated))
                    live[ident] = updated
            for _ in range(rng.randint(0, 2)):
                ident = "n%d-%d" % (sequence, rng.randint(0, 10 ** 6))
                if ident in live:
                    continue
                row = clip_row(
                    ident,
                    rng.choice(units),
                    rng.choice(partitions),
                    rng.choice(priorities),
                    rng.randint(-1000, 1000),
                )
                changes.append(clip_change(ident, None, row))
                live[ident] = row
            rng.shuffle(changes)
            batch = clip_batch("s", sequence, changes)
            source = support.write_json(base / ("b%d.json" % sequence), batch)
            out = base / ("p%d.json" % sequence)
            result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(out.read_text())
            expected = oracle.apply_batch(batch)
            self.assertEqual(packet["changes"], expected["changes"], "seq %d" % sequence)
            self.assertEqual(packet["packet_sha256"], expected["packet_sha256"])
            self.assertEqual(
                packet["before_rows_sha256"], expected["before_rows_sha256"]
            )
            self.assertEqual(packet["after_rows_sha256"], expected["after_rows_sha256"])
            packets.append(packet)
        producer = self.snapshot(db, "producer")
        self.assertEqual(producer["sequence"], 15)
        self.assertEqual(producer["rows"], [oracle.clip[key] for key in sorted(oracle.clip)])
        self.assertEqual(producer["rows_sha256"], rows_sha(oracle.clip))
        for packet in packets:
            source = support.write_json(base / ("consume-%d.json" % packet["sequence"]), packet)
            result = run_clip_cli("consume", "--db", sink, "--source", source)
            self.assertEqual(result.returncode, 0, result.stderr)
        consumer = self.snapshot(sink, "consumer")
        self.assertEqual(consumer["rows_sha256"], producer["rows_sha256"])
        self.assertEqual(consumer["sequence"], 15)
        self.assertEqual(consumer["last_packet_sha256"], oracle.consumer_last_packet)


class ClippingSemantics(ClipHarness):
    def _seed(self, rows, max_partitions=1, max_rows=1):
        config = clip_config(max_partitions=max_partitions, max_rows=max_rows)
        path = support.write_json(self.base / "cfg2.json", config)
        db = self.base / ("db-%d-%d.db" % (max_partitions, max_rows))
        self.assertEqual(run_clip_cli("init", "--db", db, "--source", path).returncode, 0)
        changes = [clip_change(row["id"], None, row) for row in rows]
        batch = clip_batch("s", 1, changes)
        source = support.write_json(self.base / "seed.json", batch)
        out = self.base / "seed-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.base / "snap-producer-seed.json"
        result = run_clip_cli("snapshot", "--db", db, "--role", "producer", "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        return db, json.loads(output.read_text())

    def test_priority_update_switches_partition_and_evicts(self):
        db, snapshot = self._seed(
            [
                clip_row("a", "U", "p", 1),
                clip_row("b", "U", "p", 2),
                clip_row("c", "U", "q", 3),
                clip_row("d", "U", "q", 4),
            ],
            max_partitions=1,
            max_rows=2,
        )
        self.assertEqual([row["id"] for row in snapshot["rows"]], ["a", "b"])
        updated = clip_row("a", "U", "q", 0)
        batch = clip_batch(
            "s",
            2,
            [
                clip_change("a", clip_row("a", "U", "p", 1), updated),
            ],
        )
        source = support.write_json(self.base / "switch.json", batch)
        out = self.base / "switch-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(out.read_text())
        # p loses every selected row; q now supplies its first max_rows rows.
        self.assertEqual([change["id"] for change in packet["changes"]], ["a", "b", "c"])
        self.assertEqual(packet["changes"][0]["after"], updated)
        self.assertIsNone(packet["changes"][1]["after"])
        self.assertEqual(packet["changes"][2]["before"], None)
        output = self.base / "snap-switch.json"
        run_clip_cli("snapshot", "--db", db, "--role", "producer", "--output", output)
        rows = json.loads(output.read_text())["rows"]
        self.assertEqual([row["id"] for row in rows], ["a", "c"])

    def test_unselected_row_can_change_partition_first_key(self):
        db, snapshot = self._seed(
            [
                clip_row("a", "U", "p", 5),
                clip_row("z", "U", "q", 9),
            ]
        )
        # q wins: 9 < ... no, p's first key is 5 so a is selected.
        self.assertEqual([row["id"] for row in snapshot["rows"]], ["a"])
        batch = clip_batch(
            "s",
            2,
            [
                clip_change(
                    "z", clip_row("z", "U", "q", 9), clip_row("z", "U", "q", 1)
                ),
            ],
        )
        source = support.write_json(self.base / "unselected.json", batch)
        out = self.base / "unselected-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(out.read_text())
        self.assertEqual(
            [(change["id"], change["before"], change["after"]) for change in packet["changes"]],
            [("a", clip_row("a", "U", "p", 5), None), ("z", None, clip_row("z", "U", "q", 1))],
        )

    def test_equal_priority_rows_break_by_id(self):
        db, snapshot = self._seed(
            [
                clip_row("b", "U", "p", 1),
                clip_row("a", "U", "p", 1),
            ],
            max_partitions=1,
            max_rows=1,
        )
        self.assertEqual([row["id"] for row in snapshot["rows"]], ["a"])

    def test_deleting_last_selected_row_empties_snapshot(self):
        db, snapshot = self._seed([clip_row("a", "U", "p", 1)])
        self.assertEqual([row["id"] for row in snapshot["rows"]], ["a"])
        batch = clip_batch(
            "s", 2, [clip_change("a", clip_row("a", "U", "p", 1), None)]
        )
        source = support.write_json(self.base / "drop.json", batch)
        out = self.base / "drop-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(out.read_text())
        self.assertEqual(
            packet["changes"],
            [{"id": "a", "before": clip_row("a", "U", "p", 1), "after": None}],
        )
        self.assertEqual(packet["after_rows_sha256"], clip_model.empty_rowset_sha256())
        empty = self.base / "snap-drop.json"
        run_clip_cli("snapshot", "--db", db, "--role", "producer", "--output", empty)
        self.assertEqual(json.loads(empty.read_text())["rows"], [])
        # A later batch replacing the last selected row is an insert after eviction.
        batch = clip_batch(
            "s", 3, [clip_change("b", None, clip_row("b", "U", "p", 2))]
        )
        source = support.write_json(self.base / "refill.json", batch)
        out2 = self.base / "refill-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", source, "--output", out2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(out2.read_text())["changes"],
            [{"id": "b", "before": None, "after": clip_row("b", "U", "p", 2)}],
        )
        output = self.base / "snap-refill.json"
        run_clip_cli("snapshot", "--db", db, "--role", "producer", "--output", output)
        self.assertEqual(
            json.loads(output.read_text())["rows"], [clip_row("b", "U", "p", 2)]
        )

    def test_partition_name_breaks_priority_ties(self):
        db, snapshot = self._seed(
            [
                clip_row("a", "U", "p", 1),
                clip_row("b", "U", "q", 1),
            ],
            max_partitions=1,
            max_rows=5,
        )
        self.assertEqual([row["id"] for row in snapshot["rows"]], ["a"])


class RecoveryAndFailure(ClipHarness):
    def _batch(self, sequence=1):
        return clip_batch(
            "s", sequence,
            [
                clip_change("a", None, clip_row("a", "U", "p", 1)),
                clip_change("b", None, clip_row("b", "U", "p", 2)),
            ],
        )

    def test_export_after_delivery_failure_is_recoverable(self):
        batch = self._batch()
        source = support.write_json(self.base / "batch.json", batch)
        blocker = self.base / "blocked"
        blocker.write_text("not a directory")
        output = blocker / "packet.json"
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, 2)
        snapshot = self.producer_snapshot()
        self.assertEqual(snapshot["sequence"], 1)
        recovered = self.base / "recovered.json"
        result = run_clip_cli(
            "export", "--db", self.db, "--sequence", 1, "--output", recovered
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [change["id"] for change in json.loads(recovered.read_text())["changes"]],
            ["a"],
        )
        # Identical retry returns the original packet and does not advance.
        retried = self.apply(batch, "retry.json")
        self.assertEqual(json.loads(recovered.read_text()), retried)
        self.assertEqual(self.producer_snapshot()["sequence"], 1)

    def test_producer_crash_between_commit_and_delivery(self):
        batch = self._batch()
        source = support.write_json(self.base / "batch.json", batch)
        output = self.base / "packet.json"

        def hook(point):
            if point == "producer_committed":
                raise RuntimeError("simulated crash before delivery")

        clip_api.set_fault_hook(hook)
        with self.assertRaises(RuntimeError):
            clip_api.apply(str(self.db), str(source), str(output))
        clip_api.set_fault_hook(None)
        self.assertFalse(output.exists())
        self.assertEqual(self.producer_snapshot()["sequence"], 1)
        recovered = self.base / "recovered.json"
        result = run_clip_cli(
            "export", "--db", self.db, "--sequence", 1, "--output", recovered
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [change["id"] for change in json.loads(recovered.read_text())["changes"]],
            ["a"],
        )

    def test_delivered_packet_bytes_are_within_consumable_limit(self):
        batch = self._batch()
        source = support.write_json(self.base / "batch.json", batch)
        output = self.base / "packet.json"
        _, _ = clip_api.apply(str(self.db), str(source), str(output))
        raw = output.read_bytes()
        self.assertLessEqual(len(raw), 8_000_000)
        # The file is canonical, so its digest input is exactly what consume reads.
        self.assertEqual(raw, clip_model.packet_bytes(clip_model.parse_packet(json.loads(raw))))

    def test_consumer_crash_before_and_after_commit(self):
        packet = self.apply(self._batch(), "b1.json")
        source = support.write_json(self.base / "packet.json", packet)

        def before_commit(point):
            if point == "consumer_precommit":
                raise RuntimeError("crash before commit")

        clip_api.set_fault_hook(before_commit)
        with self.assertRaises(RuntimeError):
            clip_api.consume(str(self.sink), str(source))
        clip_api.set_fault_hook(None)
        self.assertEqual(self.consumer_snapshot()["sequence"], 0)
        self.assertEqual(self.consumer_snapshot()["rows"], [])

        def after_commit(point):
            if point == "consumer_committed":
                raise RuntimeError("crash after commit")

        clip_api.set_fault_hook(after_commit)
        with self.assertRaises(RuntimeError):
            clip_api.consume(str(self.sink), str(source))
        clip_api.set_fault_hook(None)
        consumer = self.consumer_snapshot()
        self.assertEqual(consumer["sequence"], 1)
        self.assertEqual(consumer["last_packet_sha256"], packet["packet_sha256"])
        self.assertEqual(consumer["rows"], [clip_row("a", "U", "p", 1)])
        # Retry after the crash returns the stored receipt and keeps state fixed.
        receipt, replay = clip_api.consume(str(self.sink), str(source))
        self.assertTrue(replay)
        self.assertEqual(receipt["packet_sha256"], packet["packet_sha256"])
        self.assertEqual(self.consumer_snapshot(), consumer)

    def test_consumer_rejects_wrong_before_and_chain(self):
        packet1 = self.apply(self._batch(1), "b1.json")
        packet2 = self.apply(
            clip_batch(
                "s",
                2,
                [
                    clip_change(
                        "a", clip_row("a", "U", "p", 1), clip_row("a", "U", "p", 9)
                    )
                ],
            ),
            "b2.json",
        )
        bad = _packet_error(
            packet1,
            "changes",
            [
                {
                    "id": "a",
                    "before": clip_row("a", "U", "p", 4),
                    "after": None,
                }
            ],
        )
        # The before row set digest still matches the empty consumer state, so
        # the failure is specifically the per-change before mismatch.
        result = self.consume(bad, "bad.json", expect=2)
        self.assertIn("before row does not match", result.stderr)
        self.assertEqual(self.consumer_snapshot()["sequence"], 0)

        skipped = dict(packet2)
        result = self.consume(skipped, "skip.json", expect=2)
        self.assertIn("chain", result.stderr)
        self.assertEqual(self.consumer_snapshot()["sequence"], 0)

        self.assertEqual(self.consume(packet1, "p1.json")["sequence"], 1)
        self.assertEqual(self.consume(packet2, "p2.json")["sequence"], 2)

    def test_same_sequence_different_packet_rejected(self):
        packet1 = self.apply(self._batch(1), "b1.json")
        self.consume(packet1, "p1.json")
        forged = dict(packet1)
        forged["changes"] = []
        forged = _packet_error(forged, "changes", [])
        result = self.consume(forged, "forged.json", expect=2)
        self.assertIn("different packet", result.stderr)
        self.assertEqual(self.consumer_snapshot()["sequence"], 1)

    def test_history_is_immutable_after_progress(self):
        first = self._batch(1)
        packet1 = self.apply(first, "b1.json")
        self.apply(
            clip_batch(
                "s", 2, [clip_change("a", clip_row("a", "U", "p", 1), clip_row("a", "U", "p", 0, 5))]
            ),
            "b2.json",
        )
        conflicted = self._batch(1)
        conflicted["changes"][0]["after"]["value"] = 99
        result = self.apply(conflicted, "conflict.json", expect=2)
        self.assertIn("different batch", result.stderr)
        out = self.base / "export1.json"
        result = run_clip_cli("export", "--db", self.db, "--sequence", 1, "--output", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text()), packet1)

    def test_oversized_packet_rejected_before_commit(self):
        """A canonical packet above 8 MB is refused before the source advances."""
        db = self.base / "big.db"
        config = {"stream_id": "s", "max_partitions": 100, "max_rows": 150}
        config_path = support.write_json(self.base / "big-config.json", config)
        self.assertEqual(run_clip_cli("init", "--db", db, "--source", config_path).returncode, 0)

        pad = "x" * 100
        units = "u" + pad
        partitions = ["p%03d%s" % (index, pad) for index in range(200)]
        identifiers = {
            (index, slot): "r%03d-%03d%s" % (index, slot, pad)
            for index in range(200)
            for slot in range(150)
        }

        def row(index, slot, priority):
            return clip_row(identifiers[(index, slot)], units, partitions[index], priority, 0)

        sequence = 0
        pending = []
        for index in range(200):
            for slot in range(150):
                pending.append(
                    clip_change(identifiers[(index, slot)], None, row(index, slot, index))
                )
                if len(pending) == 1000:
                    sequence += 1
                    payload, _ = clip_api.apply(
                        str(db),
                        str(support.write_json(self.base / "big.json", clip_batch("s", sequence, pending))),
                        str(self.base / "big-packet.json"),
                    )
                    pending = []
        if pending:
            sequence += 1
            clip_api.apply(
                str(db),
                str(support.write_json(self.base / "big.json", clip_batch("s", sequence, pending))),
                str(self.base / "big-packet.json"),
            )
        self.assertEqual(sequence, 30)
        snapshot = self.snapshot(db, "producer")
        self.assertEqual(snapshot["sequence"], 30)
        self.assertEqual(len(snapshot["rows"]), 100 * 150)

        # Each unselected partition gets a new first key ahead of every selected
        # one, so the full 15,000-row selection is evicted and replaced.
        rotate = [
            clip_change(
                identifiers[(index, 0)],
                row(index, 0, index),
                row(index, 0, -1),
            )
            for index in range(100, 200)
        ]
        oversize = support.write_json(
            self.base / "oversize.json", clip_batch("s", 31, rotate)
        )
        out = self.base / "oversize-packet.json"
        result = run_clip_cli("apply", "--db", db, "--source", oversize, "--output", out)
        self.assertEqual(result.returncode, 2)
        self.assertIn("8 MB", result.stderr)
        self.assertFalse(out.exists())
        self.assertEqual(self.snapshot(db, "producer")["sequence"], 30)
        self.assertEqual(self.snapshot(db, "producer")["rows_sha256"], snapshot["rows_sha256"])
        result = run_clip_cli(
            "export", "--db", db, "--sequence", 31, "--output", self.base / "p31.json"
        )
        self.assertEqual(result.returncode, 2)


class PathsAndContracts(ClipHarness):
    def test_unknown_fields_and_bool_integers_rejected(self):
        bad = clip_config()
        bad["extra"] = 1
        path = support.write_json(self.base / "bad.json", bad)
        db = self.base / "other.db"
        result = run_clip_cli("init", "--db", db, "--source", path)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(db.exists())

        config = clip_config()
        config["max_rows"] = True
        path = support.write_json(self.base / "bool.json", config)
        result = run_clip_cli("init", "--db", db, "--source", path)
        self.assertEqual(result.returncode, 2)

    def test_config_immutable_and_role_bound(self):
        same = support.write_json(self.base / "same.json", clip_config())
        result = run_clip_cli("init", "--db", self.db, "--source", same)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"created": false', result.stdout)
        changed = support.write_json(
            self.base / "changed.json", clip_config(max_partitions=2)
        )
        result = run_clip_cli("init", "--db", self.db, "--source", changed)
        self.assertEqual(result.returncode, 2)
        self.assertIn("immutable", result.stderr)
        result = run_clip_cli("init", "--db", self.db, "--source", same)
        self.assertEqual(result.returncode, 0)
        result = run_clip_cli("sink-init", "--db", self.db, "--source", same)
        self.assertEqual(result.returncode, 2)

    def test_cannot_reuse_release_catalog(self):
        sys.path.insert(0, str(support.SRC))
        from privacy_budget import release_api

        limits = support.write_json(
            self.base / "limits.json",
            {
                "limits": [
                    {
                        "dataset": "d",
                        "principal": "p",
                        "epsilon": support.frac(1),
                        "delta": support.frac(0),
                    }
                ]
            },
        )
        catalog = self.base / "catalog.db"
        release_api.init(str(catalog), str(limits))
        result = run_clip_cli("init", "--db", catalog, "--source", self.config_path)
        self.assertEqual(result.returncode, 2)

    def test_symlink_and_aliasing_inputs_rejected(self):
        linked_db = self.base / "linked.db"
        linked_db.symlink_to(self.db)
        result = run_clip_cli(
            "snapshot", "--db", linked_db, "--role", "producer",
            "--output", self.base / "snap.json",
        )
        self.assertEqual(result.returncode, 2)

        aliased = self.base / "aliased-packet.json"
        batch = clip_batch("s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))])
        source = support.write_json(self.base / "batch.json", batch)
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", source, "--output", source
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.producer_snapshot()["sequence"], 0)

        # An input symlink is readable, but aliasing the output is refused.
        benign = self.base / "benign-link.json"
        benign.symlink_to(source)
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", benign,
            "--output", self.base / "fresh-out.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        link_out = self.base / "link-out.json"
        link_out.symlink_to(self.base / "target.json")
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", source, "--output", link_out
        )
        self.assertEqual(result.returncode, 2)
        # The idle replay of sequence 1 does not advance the cursor.
        self.assertEqual(self.producer_snapshot()["sequence"], 1)

    def test_failed_apply_does_not_overwrite_old_output(self):
        good = clip_batch(
            "s", 1,
            [clip_change("a", None, clip_row("a", "U", "p", 1))],
        )
        packet = self.apply(good, "b1.json")
        output = self.base / "b1-packet.json"
        before = output.read_bytes()
        bad = clip_batch(
            "s", 3,
            [clip_change("b", None, clip_row("b", "U", "p", 2))],
        )
        result = self.apply(bad, "b3.json", expect=2)
        self.assertIn("contiguous", result.stderr)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.producer_snapshot()["sequence"], 1)


class ChainIntegrity(ClipHarness):
    def test_producer_rejects_gap_and_cross_stream(self):
        batch = clip_batch(
            "s", 2, [clip_change("a", None, clip_row("a", "U", "p", 1))]
        )
        result = self.apply(batch, "gap.json", expect=2)
        self.assertIn("contiguous", result.stderr)
        other = clip_batch(
            "other", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]
        )
        result = self.apply(other, "stream.json", expect=2)
        self.assertIn("stream_id", result.stderr)
        self.assertEqual(self.producer_snapshot()["sequence"], 0)

    def test_consumer_rejects_disconnected_previous_digest(self):
        packet = self.apply(
            clip_batch("s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]),
            "b1.json",
        )
        body = dict(packet)
        body.pop("packet_sha256")
        body["previous_packet_sha256"] = "1" * 64
        body["packet_sha256"] = digest(body)
        result = self.consume(body, "bad.json", expect=2)
        self.assertIn("chain", result.stderr)
        self.assertEqual(self.consumer_snapshot()["sequence"], 0)
        self.assertEqual(self.consumer_snapshot()["rows"], [])

    def test_consumer_rejects_packet_for_other_stream(self):
        packet = self.apply(
            clip_batch("s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]),
            "b1.json",
        )
        body = dict(packet)
        body.pop("packet_sha256")
        body["stream_id"] = "other"
        body["packet_sha256"] = digest(body)
        result = self.consume(body, "stream.json", expect=2)
        self.assertIn("stream_id", result.stderr)
        self.assertEqual(self.consumer_snapshot()["sequence"], 0)


class Concurrency(ClipHarness):
    def test_independent_processes_yield_serial_sequences(self):
        batches = []
        for sequence in (1, 2):
            batch = clip_batch(
                "s",
                sequence,
                [clip_change("r%d" % sequence, None, clip_row("r%d" % sequence, "U", "p", sequence))],
            )
            batches.append(support.write_json(self.base / ("c%d.json" % sequence), batch))
        env = {"PYTHONPATH": str(support.SRC)}
        processes = []
        for sequence, source in enumerate(batches, start=1):
            out = self.base / ("out%d.json" % sequence)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "privacy_budget.clip_cli", "apply",
                        "--db", str(self.db), "--source", str(source), "--output", str(out),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    cwd=str(support.ROOT),
                )
            )
        results = [process.communicate() + (process.returncode,) for process in processes]
        codes = sorted(code for _, _, code in results)
        self.assertIn(codes, ([0, 2], [0, 0]))
        snapshot = self.producer_snapshot()
        self.assertGreaterEqual(snapshot["sequence"], 1)
        if codes[0] == 0 and codes[1] == 0:
            self.assertEqual(snapshot["sequence"], 2)
        # Whichever sequence succeeded must be exportable and chain-consistent.
        for sequence, source in enumerate(batches, start=1):
            out = self.base / ("export%d.json" % sequence)
            result = run_clip_cli(
                "export", "--db", self.db, "--sequence", sequence, "--output", out
            )
            if sequence <= snapshot["sequence"]:
                self.assertEqual(result.returncode, 0, result.stderr)
                packet = json.loads(out.read_text())
                self.assertEqual(packet["sequence"], sequence)
                if sequence == 1:
                    self.assertEqual(packet["previous_packet_sha256"], "0" * 64)
            else:
                self.assertEqual(result.returncode, 2)

    def test_adjacent_batches_in_parallel_form_a_valid_chain(self):
        first = support.write_json(
            self.base / "n1.json",
            clip_batch("s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]),
        )
        second = support.write_json(
            self.base / "n2.json",
            clip_batch(
                "s", 2,
                [
                    clip_change(
                        "a", clip_row("a", "U", "p", 1), clip_row("a", "U", "p", 3)
                    )
                ],
            ),
        )
        env = {"PYTHONPATH": str(support.SRC)}
        outputs = [self.base / "n1-p.json", self.base / "n2-p.json"]
        processes = []
        for source, out in zip((second, first), reversed(outputs)):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "privacy_budget.clip_cli", "apply",
                        "--db", str(self.db), "--source", str(source), "--output", str(out),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    cwd=str(support.ROOT),
                )
            )
        codes = sorted(process.wait() for process in processes)
        snapshot = self.producer_snapshot()
        sequence = snapshot["sequence"]
        if sequence == 1:
            # Only batch 1 committed; batch 2 was serialised behind it.
            self.assertEqual(codes, [0, 2])
            self.assertTrue(outputs[0].exists())
            packet1 = json.loads(outputs[0].read_text())
            self.assertEqual(packet1["previous_packet_sha256"], "0" * 64)
            # The rejected adjacent batch can be retried and then chains.
            result = run_clip_cli(
                "apply", "--db", self.db, "--source", second, "--output", outputs[1]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet2 = json.loads(outputs[1].read_text())
            self.assertEqual(packet2["previous_packet_sha256"], packet1["packet_sha256"])
        else:
            self.assertEqual(sequence, 2)
            self.assertEqual(codes, [0, 0])
            packet1 = json.loads(outputs[0].read_text())
            packet2 = json.loads(outputs[1].read_text())
            self.assertEqual(packet2["previous_packet_sha256"], packet1["packet_sha256"])
        producer = self.producer_snapshot()
        for packet in (packet1, packet2):
            source = support.write_json(
                self.base / ("pkt%d.json" % packet["sequence"]), packet
            )
            result = run_clip_cli("consume", "--db", self.sink, "--source", source)
            self.assertEqual(result.returncode, 0, result.stderr)
        consumer = self.consumer_snapshot()
        self.assertEqual(consumer["rows_sha256"], producer["rows_sha256"])
        self.assertEqual(consumer["sequence"], producer["sequence"])

    def test_concurrent_duplicate_consume_is_idempotent(self):
        packet = self.apply(
            clip_batch("s", 1, [clip_change("a", None, clip_row("a", "U", "p", 1))]),
            "b1.json",
        )
        source = support.write_json(self.base / "packet.json", packet)
        env = {"PYTHONPATH": str(support.SRC)}
        processes = [
            subprocess.Popen(
                [
                    sys.executable, "-m", "privacy_budget.clip_cli", "consume",
                    "--db", str(self.sink), "--source", str(source),
                    "--output", str(self.base / ("r%d.json" % index)),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=str(support.ROOT),
            )
            for index in range(2)
        ]
        results = [process.communicate() + (process.returncode,) for process in processes]
        self.assertEqual([code for _, _, code in results], [0, 0])
        consumer = self.consumer_snapshot()
        self.assertEqual(consumer["sequence"], 1)
        self.assertEqual(consumer["rows"], [clip_row("a", "U", "p", 1)])
        first = json.loads((self.base / "r0.json").read_text())
        second = json.loads((self.base / "r1.json").read_text())
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
