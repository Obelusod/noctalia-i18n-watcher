"""Application composition tests."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch

from noctalia_i18n_core import (
    Change,
    Checkpoint,
    DiscordWebhookSender,
    NoctaliaSource,
    PollResult,
    SQLiteState,
)

from noctalia_i18n_watcher.app import Application, RuntimePaths


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
        embed = preview[0]["embed"]
        if not isinstance(embed, dict):
            self.fail("preview embed must be an object")
        fields = embed.get("fields")
        if not isinstance(fields, list):
            self.fail("preview embed fields must be an array")
        field = fields[0]
        if not isinstance(field, dict):
            self.fail("preview embed field must be an object")
        self.assertEqual(
            field.get("value"),
            (
                "[settings.example]"
                "(https://i18n.noctalia.dev/projects/noctalia/translate)"
            ),
        )
        with closing(SQLiteState(database, read_only=True)) as state:
            self.assertEqual(
                state.load(), Checkpoint("previous", {change.key: "English"})
            )

    def test_run_persists_a_baseline(self) -> None:
        result = PollResult((), "cursor", 2, {"settings.example": "English"})
        with (
            patch.object(NoctaliaSource, "poll", return_value=result),
            patch.object(DiscordWebhookSender, "send"),
        ):
            Application.load(self._paths).run()

        with closing(
            SQLiteState(self._paths.state / "noctalia.sqlite3", read_only=True)
        ) as state:
            self.assertEqual(
                state.load(),
                Checkpoint("cursor", {"settings.example": "English"}),
            )

    def test_status_reads_persisted_state_and_can_notify(self) -> None:
        database = self._paths.state / "noctalia.sqlite3"
        with closing(SQLiteState(database)) as state:
            state.save(Checkpoint("cursor", {"key": "English"}))

        with patch.object(DiscordWebhookSender, "send") as send:
            summary = Application.load(self._paths).status(notify=True)["noctalia"]

        self.assertTrue(summary.initialized)
        self.assertEqual(summary.source_texts, 1)
        send.assert_called_once()

    def test_history_reads_the_selected_source(self) -> None:
        expected = (_change(),)
        with patch.object(NoctaliaSource, "history", return_value=expected):
            actual = Application.load(self._paths).history(1)

        self.assertEqual(actual, expected)
