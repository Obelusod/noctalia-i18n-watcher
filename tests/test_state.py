"""SQLite state contract tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from noctalia_i18n_core import Change

from noctalia_i18n_watcher.monitor import Checkpoint, Delivery
from noctalia_i18n_watcher.state import SQLiteState


def _change(identifier: str) -> Change:
    return Change(
        id=identifier,
        key=f"settings.{identifier}",
        locale="zh-Hans",
        action="modified",
        actor="API",
        old_value="旧",
        new_value="新",
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        translation_url="https://i18n.noctalia.dev/projects/noctalia/translate",
    )


class StateTests(unittest.TestCase):
    def test_checkpoint_and_outbox_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with closing(SQLiteState(path)) as state:
                checkpoint = Checkpoint("noctalia", "head", {"key": "English"})
                state.save(checkpoint)
                delivery = Delivery(_change("one"), "English")
                state.collect(checkpoint, {"route": (delivery,)}, datetime.now(UTC))
                pending = state.pending("route")

            with closing(SQLiteState(path, read_only=True)) as state:
                self.assertEqual(state.load(), checkpoint)
                self.assertEqual(state.pending("route"), pending)

    def test_collection_rolls_back_checkpoint_and_queue_together(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            initial = Checkpoint("noctalia", "one", {})
            state.save(initial)
            invalid = Delivery(_change("two"), None, ("two", "folded"))
            with self.assertRaisesRegex(ValueError, "raw deliveries"):
                state.collect(
                    Checkpoint("noctalia", "two", {}),
                    {"route": (invalid,)},
                    datetime.now(UTC),
                )

            self.assertEqual(state.load(), initial)
            self.assertFalse(state.pending("route"))

    def test_partial_acknowledgement_removes_only_completed_deliveries(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            checkpoint = Checkpoint("noctalia", "head", {})
            first = Delivery(_change("one"), None)
            second = Delivery(_change("two"), None)
            state.collect(
                checkpoint,
                {"route": (first, second)},
                datetime.now(UTC),
            )
            state.acknowledge("route", (first,))

            pending = state.pending("route")
            summary = state.summary()

        self.assertEqual(tuple(item.delivery for item in pending), (second,))
        self.assertEqual(summary.delivery_receipts, 1)

    def test_full_reset_clears_receipts_and_queue(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            first = Checkpoint("noctalia", "one", {})
            delivery = Delivery(_change("one"), None)
            state.collect(first, {"route": (delivery,)}, datetime.now(UTC))
            state.acknowledge("route", (delivery,))
            state.reset("full", Checkpoint("noctalia", "two", {}), ())

            summary = state.summary()
            self.assertFalse(state.pending("route"))

        self.assertEqual(summary.delivery_receipts, 0)
        self.assertEqual(summary.baseline_receipts, 0)

    def test_baseline_reset_preserves_delivery_state(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            first = Checkpoint("noctalia", "one", {})
            delivery = Delivery(_change("one"), None)
            state.collect(first, {"route": (delivery,)}, datetime.now(UTC))
            state.acknowledge("route", (delivery,))
            state.acknowledge_baseline("route")
            state.reset("baseline", Checkpoint("noctalia", "two", {}), ("other",))

            checkpoint = state.load()
            summary = state.summary()

        self.assertEqual(checkpoint, Checkpoint("noctalia", "two", {}))
        self.assertEqual(summary.delivery_receipts, 1)
        self.assertEqual(summary.baseline_receipts, 2)

    def test_read_only_opening_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            with closing(SQLiteState(path, read_only=True)) as state:
                self.assertIsNone(state.load())
            self.assertFalse(path.exists())

    def test_receipt_pruning_keeps_recent_rows(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            checkpoint = Checkpoint("noctalia", "head", {})
            delivery = Delivery(_change("one"), None)
            state.collect(checkpoint, {"route": (delivery,)}, datetime.now(UTC))
            state.acknowledge("route", (delivery,))
            path = Path(directory) / "state.sqlite3"
            with sqlite3.connect(path) as database:
                database.execute(
                    "UPDATE delivery_receipts SET delivered_at = ?",
                    ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
                )
            state.prune(1)

            self.assertEqual(state.summary().delivery_receipts, 0)
