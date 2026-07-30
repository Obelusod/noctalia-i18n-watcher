"""Application composition tests."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from noctalia_i18n_core import (
    Change,
    Checkpoint,
    NoctaliaSource,
    PollResult,
    SQLiteState,
)

from noctalia_i18n_watcher.app import Application, RuntimePaths
from noctalia_i18n_watcher.config import load_config
from noctalia_i18n_watcher.discord import DiscordWebhookClient


def _change() -> Change:
    return Change(
        id="change-1",
        key="settings.example",
        locale="zh-Hans",
        actor="translator",
        old_value="旧",
        new_value="新",
        action="modified",
        occurred_at=datetime(2026, 7, 26, 8, 0, tzinfo=UTC),
        url="https://i18n.noctalia.dev/projects/noctalia/translate",
    )


class ApplicationTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(__file__).parents[1]
        self._root = root
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self._paths = RuntimePaths(
            config=root / "config" / "config.json",
            messages=root / "config" / "messages",
            state=Path(self._temporary.name),
        )

    def test_repository_inputs_are_valid(self) -> None:
        Application.load(self._paths).validate()

    def test_validation_ignores_inactive_message_resources(self) -> None:
        config = load_config(self._paths.config)
        monitor = config.monitors[0]
        webhook = replace(
            monitor.webhooks[0],
            locales=frozenset({"en"}),
            target_message="missing",
            merge_message="missing",
            merge_threshold=None,
        )
        application = Application(
            replace(
                config,
                monitors=(replace(monitor, webhooks=(webhook,)),),
            ),
            self._paths,
        )

        application.validate()

    def test_baseline_preview_does_not_create_state(self) -> None:
        result = PollResult((), "cursor", 3, {"settings.example": "English"})
        with patch.object(NoctaliaSource, "poll", return_value=result):
            preview = Application.load(self._paths).preview()

        self.assertEqual(
            preview,
            (
                {
                    "monitor_id": "noctalia",
                    "baseline": True,
                    "scanned": 3,
                    "source_texts": 1,
                },
            ),
        )
        self.assertFalse((self._paths.state / "noctalia.sqlite3").exists())

    def test_preview_renders_a_linked_change_without_writing_state(self) -> None:
        change = _change()
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("previous", {change.key: "English"}))
        result = PollResult(
            (change,),
            "next",
            1,
            {change.key: "English"},
        )

        with patch.object(NoctaliaSource, "poll", return_value=result):
            preview = Application.load(self._paths).preview()

        self.assertEqual(preview[0]["monitor_id"], "noctalia")
        self.assertEqual(preview[0]["route_id"], "default")
        self.assertIn(
            (
                "**[`settings.example`]"
                "(https://i18n.noctalia.dev/projects/noctalia/translate)**"
            ),
            str(preview[0]["embed"]),
        )
        with closing(SQLiteState(database, read_only=True)) as state:
            self.assertEqual(
                state.load(), Checkpoint("previous", {change.key: "English"})
            )

    def test_run_persists_a_baseline(self) -> None:
        result = PollResult((), "cursor", 2, {"settings.example": "English"})
        with (
            patch.object(NoctaliaSource, "poll", return_value=result),
            patch.object(DiscordWebhookClient, "send") as send,
        ):
            Application.load(self._paths).run()

        with closing(
            SQLiteState(self._paths.state / "noctalia.sqlite3", read_only=True)
        ) as state:
            self.assertEqual(
                state.load(),
                Checkpoint("cursor", {"settings.example": "English"}),
            )
            self.assertTrue(state.baseline_acknowledged("default"))
        send.assert_called_once()

    def test_run_delivers_and_acknowledges_changes(self) -> None:
        change = _change()
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("previous", {change.key: "English"}))
            state.acknowledge_baseline("default")
        result = PollResult((change,), "next", 1, {change.key: "English"})

        with (
            patch.object(NoctaliaSource, "poll", return_value=result),
            patch.object(DiscordWebhookClient, "send") as send,
        ):
            Application.load(self._paths).run(flush=True)

        with closing(SQLiteState(database, read_only=True)) as state:
            summary = state.summary()
            self.assertEqual(state.load(), Checkpoint("next", {change.key: "English"}))
        self.assertEqual(summary.pending_deliveries, 0)
        self.assertEqual(summary.delivery_receipts, 1)
        send.assert_called_once()

    def test_run_continues_after_a_monitor_fails(self) -> None:
        config = load_config(self._paths.config)
        monitor = config.monitors[0]
        application = Application(
            replace(
                config,
                monitors=(
                    replace(monitor, id="first"),
                    replace(monitor, id="second"),
                ),
            ),
            self._paths,
        )
        baseline = PollResult((), "cursor", 1, {"key": "English"})

        with (
            patch.object(
                NoctaliaSource,
                "poll",
                side_effect=(RuntimeError("source unavailable"), baseline),
            ) as poll,
            patch.object(DiscordWebhookClient, "send"),
            self.assertRaisesRegex(RuntimeError, "first: source unavailable"),
        ):
            application.run()

        self.assertEqual(poll.call_count, 2)
        with closing(
            SQLiteState(self._paths.state / "second.sqlite3", read_only=True)
        ) as state:
            self.assertEqual(state.load(), Checkpoint("cursor", {"key": "English"}))

    def test_status_reads_persisted_state_and_can_notify(self) -> None:
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("cursor", {"key": "English"}))

        with patch.object(DiscordWebhookClient, "send") as send:
            summary = Application.load(self._paths).status(notify=True)["noctalia"]

        self.assertTrue(summary.initialized)
        self.assertEqual(summary.source_texts, 1)
        send.assert_called_once()

    def test_history_reads_the_selected_source(self) -> None:
        expected = (_change(),)
        with patch.object(NoctaliaSource, "history", return_value=expected):
            actual = Application.load(self._paths).history(1)

        self.assertEqual(actual, expected)
