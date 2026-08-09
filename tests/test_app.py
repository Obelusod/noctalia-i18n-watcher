"""Application composition tests."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from noctalia_i18n_core import Change, ChangePage

from noctalia_i18n_watcher.app import Application, RuntimePaths
from noctalia_i18n_watcher.config import load_config
from noctalia_i18n_watcher.discord import DiscordWebhookClient
from noctalia_i18n_watcher.monitor import Checkpoint
from noctalia_i18n_watcher.state import SQLiteState


class _Client:
    slug = "noctalia"

    def __init__(self, changes: tuple[Change, ...] = ()) -> None:
        self.changes = changes

    def change_pages(self, *, start_page: int = 1) -> Iterator[ChangePage]:
        if start_page > 1:
            return
        yield ChangePage(
            number=1,
            total_pages=1,
            changes=self.changes,
            cursor=self.changes[-1].id if self.changes else None,
        )

    def iter_changes(self, *, after: str | None = None) -> Iterator[Change]:
        yield from reversed(self.changes)

    def catalog(self) -> dict[str, dict[str, str]]:
        return {
            "settings.example": {"en": "English", "zh-Hans": "中文"},
            "settings.other": {"en": "Other"},
        }


def _change() -> Change:
    return Change(
        id="change-1",
        key="settings.example",
        locale="zh-Hans",
        action="modified",
        old_value="旧",
        new_value="新",
        occurred_at=datetime(2026, 7, 26, 8, 0, tzinfo=UTC),
        translation_url="https://i18n.noctalia.dev/projects/noctalia/translate",
        actor_login="fixture",
    )


class ApplicationTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(__file__).parents[1]
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

    def test_preview_baseline_does_not_create_state(self) -> None:
        with patch(
            "noctalia_i18n_watcher.app.NoctaliaClient",
            return_value=_Client(),
        ):
            preview = Application.load(self._paths).preview()

        self.assertEqual(
            preview,
            (
                {
                    "monitor_id": "noctalia",
                    "baseline": True,
                    "scanned": 0,
                    "source_texts": 2,
                },
            ),
        )
        self.assertFalse((self._paths.state / "noctalia.sqlite3").exists())

    def test_run_persists_baseline_and_notifies(self) -> None:
        with (
            patch(
                "noctalia_i18n_watcher.app.NoctaliaClient",
                return_value=_Client(),
            ),
            patch.object(DiscordWebhookClient, "send") as send,
        ):
            Application.load(self._paths).run()

        with closing(
            SQLiteState(self._paths.state / "noctalia.sqlite3", read_only=True)
        ) as state:
            self.assertEqual(
                state.load(),
                Checkpoint(
                    "noctalia",
                    "",
                    {"settings.example": "English", "settings.other": "Other"},
                ),
            )
            self.assertTrue(state.baseline_acknowledged("default"))
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
        with (
            patch(
                "noctalia_i18n_watcher.app.NoctaliaClient",
                side_effect=(RuntimeError("source unavailable"), _Client()),
            ) as client,
            patch.object(DiscordWebhookClient, "send"),
            self.assertRaisesRegex(RuntimeError, "first: source unavailable"),
        ):
            application.run()

        self.assertEqual(client.call_count, 2)
        with closing(
            SQLiteState(self._paths.state / "second.sqlite3", read_only=True)
        ) as state:
            self.assertIsNotNone(state.load())

    def test_run_delivers_and_acknowledges_changes(self) -> None:
        change = _change()
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("noctalia", "previous", {change.key: "English"}))
            state.acknowledge_baseline("default")

        with (
            patch(
                "noctalia_i18n_watcher.app.NoctaliaClient",
                return_value=_Client((change,)),
            ),
            patch.object(DiscordWebhookClient, "send") as send,
        ):
            Application.load(self._paths).run(flush=True)

        with closing(SQLiteState(database, read_only=True)) as state:
            summary = state.summary()
            checkpoint = state.load()
            if checkpoint is None:
                self.fail("checkpoint was not saved")
            self.assertEqual(checkpoint.change_id, "change-1")
        self.assertEqual(summary.pending_deliveries, 0)
        self.assertEqual(summary.delivery_receipts, 1)
        send.assert_called_once()

    def test_preview_renders_a_change_without_writing_state(self) -> None:
        change = _change()
        database = self._paths.state / "noctalia.sqlite3"
        checkpoint = Checkpoint("noctalia", "previous", {change.key: "English"})
        with closing(SQLiteState(database)) as state:
            state.save(checkpoint)
            state.acknowledge_baseline("default")

        with patch(
            "noctalia_i18n_watcher.app.NoctaliaClient",
            return_value=_Client((change,)),
        ):
            preview = Application.load(self._paths).preview()

        self.assertEqual(preview[0]["monitor_id"], "noctalia")
        self.assertEqual(preview[0]["route_id"], "default")
        self.assertIn("settings.example", str(preview[0]["embed"]))
        with closing(SQLiteState(database, read_only=True)) as state:
            self.assertEqual(state.load(), checkpoint)

    def test_status_reads_persisted_state_and_can_notify(self) -> None:
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("noctalia", "cursor", {"key": "English"}))

        with patch.object(DiscordWebhookClient, "send") as send:
            summary = Application.load(self._paths).status(notify=True)["noctalia"]

        self.assertTrue(summary.initialized)
        self.assertEqual(summary.source_texts, 1)
        send.assert_called_once()

    def test_history_reads_a_core_page(self) -> None:
        expected = (_change(),)
        client = _Client(expected)
        with patch(
            "noctalia_i18n_watcher.app.NoctaliaClient",
            return_value=client,
        ):
            actual = Application.load(self._paths).history(1)

        self.assertEqual(actual, expected)

    def test_history_returns_empty_when_page_is_beyond_the_source(self) -> None:
        client = _Client()
        with patch(
            "noctalia_i18n_watcher.app.NoctaliaClient",
            return_value=client,
        ):
            actual = Application.load(self._paths).history(2)

        self.assertEqual(actual, ())


if __name__ == "__main__":
    unittest.main()
