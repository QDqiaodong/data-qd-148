"""Strict contract tests for microdata clip config, batches, packets and paths."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import support
from support import clip_batch, clip_change, clip_config, clip_row, run_clip_cli

sys.path.insert(0, str(support.SRC))

from privacy_budget import clip_model  # noqa: E402


def canonical_batch(stream="s", sequence=1, changes=()):
    return clip_batch(stream, sequence, changes)


class ContractHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db = self.base / "producer.db"
        self.sink = self.base / "consumer.db"
        self.config_path = support.write_json(self.base / "config.json", clip_config())

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, config=None, name="config.json"):
        path = support.write_json(self.base / name, config or clip_config())
        return run_clip_cli("init", "--db", self.db, "--source", path)

    def assert_init_rejected(self, config, fragment=None, output="bad-config.json"):
        path = support.write_json(self.base / output, config)
        db = self.base / "rejected.db"
        result = run_clip_cli("init", "--db", db, "--source", path)
        self.assertEqual(result.returncode, 2, result.stdout)
        if fragment:
            self.assertIn(fragment, result.stderr)
        self.assertFalse(db.exists())


class ConfigContract(ContractHarness):
    def test_field_set_is_exact(self):
        payload = clip_config()
        payload["extra"] = 1
        self.assert_init_rejected(payload, "expected object fields")

    def test_missing_field(self):
        payload = clip_config()
        del payload["max_rows"]
        self.assert_init_rejected(payload, "expected object fields")

    def test_limits(self):
        self.assert_init_rejected(clip_config(max_partitions=0), "max_partitions")
        self.assert_init_rejected(clip_config(max_partitions=101), "max_partitions")
        self.assert_init_rejected(clip_config(max_rows=0), "max_rows")
        self.assert_init_rejected(clip_config(max_rows=1001), "max_rows")

    def test_booleans_and_floats_are_not_integers(self):
        payload = clip_config()
        payload["max_partitions"] = True
        self.assert_init_rejected(payload, "max_partitions")
        payload = clip_config()
        payload["max_rows"] = 1.0
        self.assert_init_rejected(payload, "max_rows")

    def test_identifiers(self):
        for stream in ("", " leading", "trail ", "has\x01control"):
            self.assert_init_rejected(clip_config(stream_id=stream), output="id.json")

    def test_init_is_idempotent_with_same_config(self):
        self.assertEqual(self.init().returncode, 0)
        same = support.write_json(self.base / "same.json", clip_config())
        result = run_clip_cli("init", "--db", self.db, "--source", same)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["created"], False)
        changed = support.write_json(self.base / "changed.json", clip_config(max_rows=2))
        result = run_clip_cli("init", "--db", self.db, "--source", changed)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(run_clip_cli("init", "--db", self.db, "--source", same).returncode, 0)

    def test_init_refuses_unrelated_database(self):
        import sqlite3

        other = self.base / "other.db"
        connection = sqlite3.connect(str(other))
        connection.execute("CREATE TABLE notes(x TEXT)")
        connection.commit()
        connection.close()
        path = support.write_json(self.base / "c.json", clip_config())
        result = run_clip_cli("init", "--db", other, "--source", path)
        self.assertEqual(result.returncode, 2)


class BatchContract(ContractHarness):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.init().returncode, 0)

    def apply(self, batch, name="batch.json", expect=2):
        source = support.write_json(self.base / name, batch)
        output = self.base / "packet.json"
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", source, "--output", output
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_batches_are_applied_simultaneously(self):
        row = clip_row("a", "U", "p", 1)
        # A single batch may raise one id and delete another while a third moves.
        batch = clip_batch(
            "s",
            1,
            [
                clip_change("a", None, row),
                clip_change("b", None, clip_row("b", "V", "p", 5)),
                clip_change("c", None, clip_row("c", "V", "q", 1)),
            ],
        )
        source = support.write_json(self.base / "b1.json", batch)
        out = self.base / "p1.json"
        self.assertEqual(
            run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out).returncode,
            0,
        )
        second = clip_batch(
            "s",
            2,
            [
                # Re-parent a and re-key b in the same batch.
                clip_change("a", row, clip_row("a", "W", "p", 1)),
                clip_change("b", clip_row("b", "V", "p", 5), clip_row("b", "V", "p", 0)),
            ],
        )
        source = support.write_json(self.base / "b2.json", second)
        out2 = self.base / "p2.json"
        self.assertEqual(
            run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out2).returncode,
            0,
        )
        changes = json.loads(out2.read_text())["changes"]
        # Unit W gains a; unit V re-keys p to b and drops q's c.
        self.assertEqual([change["id"] for change in changes], ["a", "b", "c"])
        snapshot = self.base / "snap.json"
        run_clip_cli("snapshot", "--db", self.db, "--role", "producer", "--output", snapshot)
        rows = {row["id"]: row for row in json.loads(snapshot.read_text())["rows"]}
        self.assertEqual(rows["a"], clip_row("a", "W", "p", 1))
        self.assertEqual(rows["b"], clip_row("b", "V", "p", 0))
        self.assertNotIn("c", rows)

    def test_batch_field_set(self):
        bad = clip_batch("s", 1, [])
        bad["extra"] = 1
        self.apply(bad, "bad1.json")
        bad = {"stream_id": "s", "sequence": 1}
        self.apply(bad, "bad2.json")

    def test_sequence_bounds(self):
        for sequence in (0, -1, 2 ** 63, True, 1.0):
            batch = clip_batch("s", sequence, [])
            self.apply(batch, "seq.json")

    def test_change_field_set_and_requirements(self):
        self.apply(
            clip_batch(
                "s", 1,
                [{"id": "a", "before": None, "after": None, "extra": 1}],
            ),
            "c1.json",
        )
        self.apply(
            clip_batch("s", 1, [{"id": "a", "before": None, "after": None}]),
            "c2.json",
        )
        self.apply(
            clip_batch(
                "s", 1,
                [
                    clip_change(
                        "a",
                        clip_row("b", "U", "p", 1),
                        clip_row("a", "U", "p", 1),
                    )
                ],
            ),
            "c3.json",
        )

    def test_duplicate_batch_id(self):
        row = clip_row("a", "U", "p", 1)
        self.apply(
            clip_batch(
                "s", 1,
                [clip_change("a", None, row), clip_change("a", None, row)],
            ),
            "dupe.json",
        )

    def test_unknown_row_fields(self):
        row = clip_row("a", "U", "p", 1)
        row["extra"] = 1
        self.apply(
            clip_batch("s", 1, [clip_change("a", None, row)]), "row.json"
        )

    def test_row_integers_and_identifiers(self):
        for priority in (10 ** 12 + 1, -(10 ** 12) - 1, True, 1.5):
            row = clip_row("a", "U", "p", priority)
            self.apply(clip_batch("s", 1, [clip_change("a", None, row)]), "prio.json")
        row = clip_row("a", "", "p", 1)
        self.apply(clip_batch("s", 1, [clip_change("a", None, row)]), "unit.json")
        row = clip_row("a", "U", None, 1)
        self.apply(clip_batch("s", 1, [clip_change("a", None, row)]), "part.json")

    def test_stream_mismatch(self):
        self.apply(clip_batch("other", 1, []), "stream.json")

    def test_before_mismatch(self):
        self.apply(
            clip_batch(
                "s", 1,
                [clip_change("a", clip_row("a", "U", "p", 1), None)],
            ),
            "before.json",
        )
        self.apply(
            clip_batch(
                "s", 1,
                [clip_change("a", None, clip_row("a", "U", "p", 1))],
            ),
            "ok.json",
            expect=0,
        )
        self.apply(
            clip_batch(
                "s", 2,
                [clip_change("a", None, clip_row("a", "U", "p", 1))],
            ),
            "insert-existing.json",
        )
        self.apply(
            clip_batch(
                "s", 2,
                [clip_change("a", clip_row("a", "U", "p", 9), None)],
            ),
            "wrong-before.json",
        )
        # The failed batches never advanced the cursor.
        snapshot = self.base / "snap.json"
        run_clip_cli("snapshot", "--db", self.db, "--role", "producer", "--output", snapshot)
        self.assertEqual(json.loads(snapshot.read_text())["sequence"], 1)

    def test_same_id_changes_apply_simultaneously(self):
        # Two changes for the same id in one batch are a duplicate.
        self.apply(
            clip_batch(
                "s", 1,
                [
                    clip_change("a", None, clip_row("a", "U", "p", 1)),
                    clip_change("a", None, clip_row("a", "U", "p", 2)),
                ],
            ),
            "simul.json",
        )

    def test_empty_changes_and_order_insensitivity(self):
        first = clip_batch(
            "s", 1,
            [
                clip_change("a", None, clip_row("a", "U", "p", 1)),
                clip_change("b", None, clip_row("b", "U", "p", 2)),
            ],
        )
        source = support.write_json(self.base / "one.json", first)
        out = self.base / "one-packet.json"
        self.assertEqual(
            run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out).returncode,
            0,
        )
        reordered = clip_batch("s", 1, list(reversed(first["changes"])))
        source = support.write_json(self.base / "two.json", reordered)
        out2 = self.base / "two-packet.json"
        result = run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(out.read_text()), json.loads(out2.read_text())
        )
        empty = clip_batch("s", 2, [])
        source = support.write_json(self.base / "empty.json", empty)
        out3 = self.base / "empty-packet.json"
        result = run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out3.read_text())["changes"], [])


class PacketContract(ContractHarness):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(
            run_clip_cli("sink-init", "--db", self.sink, "--source", self.config_path).returncode,
            0,
        )
        batch = clip_batch(
            "s", 1,
            [
                clip_change("a", None, clip_row("a", "U", "p", 1)),
                clip_change("b", None, clip_row("b", "U", "p", 2)),
            ],
        )
        source = support.write_json(self.base / "b1.json", batch)
        out = self.base / "p1.json"
        self.assertEqual(
            run_clip_cli("apply", "--db", self.db, "--source", source, "--output", out).returncode,
            0,
        )
        self.packet = json.loads(out.read_text())

    def consume(self, packet, expect=2):
        source = support.write_json(self.base / "packet.json", packet)
        return run_clip_cli(
            "consume", "--db", self.sink, "--source", source,
            "--output", self.base / "receipt.json",
        )

    def test_packet_field_set(self):
        bad = dict(self.packet)
        bad["extra"] = 1
        self.consume(bad)
        for key in clip_model.PACKET_FIELDS.split():
            bad = dict(self.packet)
            del bad[key]
            self.consume(bad)

    def test_packet_digest_verification(self):
        bad = dict(self.packet)
        bad["sequence"] = 2
        self.assertEqual(self.consume(bad).returncode, 2)
        bad = dict(self.packet)
        bad["changes"] = []
        result = self.consume(bad)
        self.assertEqual(result.returncode, 2)
        self.assertIn("packet_sha256", result.stderr)

    def test_digest_format(self):
        bad = dict(self.packet)
        bad["config_sha256"] = "A" * 64
        self.consume(bad)
        bad = dict(self.packet)
        bad["packet_sha256"] = "0" * 63
        self.consume(bad)

    def test_equal_change_ends_rejected(self):
        body = dict(self.packet)
        body.pop("packet_sha256")
        row = clip_row("q", "U", "p", 1)
        body["changes"] = [{"id": "q", "before": row, "after": row}]
        body["packet_sha256"] = clip_model.sha256_bytes(clip_model.canonical_bytes(body))
        result = self.consume(body)
        self.assertEqual(result.returncode, 2)
        self.assertIn("equal packet change", result.stderr)

    def test_duplicate_packet_ids_rejected(self):
        body = dict(self.packet)
        body.pop("packet_sha256")
        body["changes"] = [self.packet["changes"][0], self.packet["changes"][0]]
        body["packet_sha256"] = clip_model.sha256_bytes(clip_model.canonical_bytes(body))
        result = self.consume(body)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate id", result.stderr)

    def test_configuration_must_match(self):
        other = support.write_json(self.base / "other.json", clip_config(max_rows=2))
        sink_other = self.base / "sink-other.db"
        self.assertEqual(
            run_clip_cli("sink-init", "--db", sink_other, "--source", other).returncode, 0
        )
        source = support.write_json(self.base / "packet.json", self.packet)
        result = run_clip_cli(
            "consume", "--db", sink_other, "--source", source,
            "--output", self.base / "r.json",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("config_sha256", result.stderr)

    def test_consumer_rejects_non_sink_database(self):
        source = support.write_json(self.base / "packet.json", self.packet)
        result = run_clip_cli(
            "consume", "--db", self.db, "--source", source,
            "--output", self.base / "r.json",
        )
        self.assertEqual(result.returncode, 2)


class PathContract(ContractHarness):
    def test_input_limits_and_duplicate_keys(self):
        path = self.base / "dup.json"
        path.write_text('{"stream_id":"s","stream_id":"s","max_partitions":1,"max_rows":1}')
        result = run_clip_cli("init", "--db", self.base / "db", "--source", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_eight_megabyte_input_limit(self):
        path = self.base / "big.json"
        path.write_bytes(b"{" + b'"stream_id":"' + b"x" * 8_000_001 + b'"}')
        result = run_clip_cli("init", "--db", self.base / "db", "--source", path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("8 MB", result.stderr)

    def test_unknown_top_level_json_shapes(self):
        path = support.write_json(self.base / "arr.json", [])
        result = run_clip_cli("init", "--db", self.base / "db", "--source", path)
        self.assertEqual(result.returncode, 2)

    def test_database_must_exist_for_apply_and_snapshot(self):
        batch = support.write_json(self.base / "b.json", clip_batch("s", 1, []))
        result = run_clip_cli(
            "apply", "--db", self.base / "missing.db", "--source", batch,
            "--output", self.base / "p.json",
        )
        self.assertEqual(result.returncode, 2)
        result = run_clip_cli(
            "snapshot", "--db", self.base / "missing.db", "--role", "producer",
            "--output", self.base / "s.json",
        )
        self.assertEqual(result.returncode, 2)

    def test_snapshot_role_is_role_bound(self):
        self.assertEqual(self.init().returncode, 0)
        result = run_clip_cli(
            "snapshot", "--db", self.db, "--role", "consumer",
            "--output", self.base / "s.json",
        )
        self.assertEqual(result.returncode, 2)
        result = run_clip_cli(
            "snapshot", "--db", self.db, "--role", "producer",
            "--output", self.base / "s.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((self.base / "s.json").read_text())
        self.assertEqual(
            set(payload),
            {"stream_id", "config_sha256", "sequence", "rows", "rows_sha256"},
        )

    def test_snapshot_never_leaks_unselected_rows(self):
        self.assertEqual(self.init().returncode, 0)
        batch = clip_batch(
            "s", 1,
            [
                clip_change("a", None, clip_row("a", "U", "p", 1)),
                clip_change("hidden", None, clip_row("hidden", "U", "p", 2)),
            ],
        )
        source = support.write_json(self.base / "b.json", batch)
        self.assertEqual(
            run_clip_cli(
                "apply", "--db", self.db, "--source", source, "--output", self.base / "p.json"
            ).returncode,
            0,
        )
        packet = json.loads((self.base / "p.json").read_text())
        self.assertNotIn("hidden", json.dumps(packet))
        snapshot = self.base / "s.json"
        run_clip_cli("snapshot", "--db", self.db, "--role", "producer", "--output", snapshot)
        self.assertNotIn("hidden", snapshot.read_text())

    def test_export_requires_committed_sequence(self):
        self.assertEqual(self.init().returncode, 0)
        result = run_clip_cli(
            "export", "--db", self.db, "--sequence", 1,
            "--output", self.base / "p.json",
        )
        self.assertEqual(result.returncode, 2)
        result = run_clip_cli(
            "export", "--db", self.db, "--sequence", 0,
            "--output", self.base / "p.json",
        )
        self.assertEqual(result.returncode, 2)

    def test_output_symlinks_and_parent_symlinks_rejected(self):
        self.assertEqual(self.init().returncode, 0)
        target = self.base / "target.json"
        target.write_text("keep")
        link = self.base / "link.json"
        link.symlink_to(target)
        batch = support.write_json(self.base / "b.json", clip_batch("s", 1, []))
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", batch, "--output", link
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(), "keep")
        folder = self.base / "real"
        folder.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(folder)
        result = run_clip_cli(
            "apply", "--db", self.db, "--source", batch, "--output", alias / "p.json"
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
