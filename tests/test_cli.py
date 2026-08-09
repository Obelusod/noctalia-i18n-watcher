"""Command-line boundary tests."""

from __future__ import annotations

import json
import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from typing import override
from unittest.mock import MagicMock, patch

from noctalia_i18n_core import Change

from noctalia_i18n_watcher.cli import main
from noctalia_i18n_watcher.state import StateSummary


class CliTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = logging.getLogger()
        self._handlers = list(root.handlers)
        self._level = root.level

    @override
    def tearDown(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        root.handlers.extend(self._handlers)
        root.setLevel(self._level)

    def test_invalid_command_line_exits_with_usage(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main([])

        self.assertEqual(raised.exception.code, 2)

    def test_dry_run_prints_application_preview(self) -> None:
        application = MagicMock(log_level="INFO")
        application.preview.return_value = ({"route_id": "default"},)
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            result = main(["deliver", "--dry-run"])

        self.assertEqual(result, 0)
        application.preview.assert_called_once_with(None)
        self.assertIn('"route_id": "default"', output.getvalue())

    def test_full_reset_is_forwarded_to_application(self) -> None:
        application = MagicMock(log_level="INFO")
        with patch(
            "noctalia_i18n_watcher.cli.Application.load",
            return_value=application,
        ):
            result = main(["reset", "full"])

        self.assertEqual(result, 0)
        application.reset.assert_called_once_with("full", None, notify=False)

    def test_status_prints_summary(self) -> None:
        application = MagicMock(log_level="INFO")
        updated_at = datetime(2026, 7, 29, 5, 2, 3, tzinfo=UTC)
        application.status.return_value = {
            "noctalia": StateSummary(True, updated_at, 1, 0, 0, 0, 0)
        }
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            result = main(["status"])

        self.assertEqual(result, 0)
        summary = json.loads(output.getvalue())["noctalia"]
        self.assertEqual(summary["updated_at"], updated_at.isoformat())
        self.assertEqual(summary["source_texts"], 1)

    def test_history_preserves_the_command_output_contract(self) -> None:
        application = MagicMock(log_level="INFO")
        application.history.return_value = (
            Change(
                id="change-1",
                key="settings.example",
                locale="en",
                action="modified",
                old_value="Old",
                new_value="New",
                occurred_at=datetime(2026, 7, 29, 5, 2, 3, 120000, tzinfo=UTC),
                translation_url="https://i18n.noctalia.dev/change",
                actor_login=None,
            ),
        )
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            result = main(["history", "1"])

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            [
                {
                    "id": "change-1",
                    "key": "settings.example",
                    "locale": "en",
                    "actor": "API",
                    "old_value": "Old",
                    "new_value": "New",
                    "action": "modified",
                    "occurred_at": "2026-07-29T05:02:03.120Z",
                    "url": "https://i18n.noctalia.dev/change",
                    "actor_url": None,
                    "actor_avatar_url": None,
                }
            ],
        )

    def test_operational_error_is_reported_without_traceback(self) -> None:
        application = MagicMock(log_level="INFO")
        application.run.side_effect = RuntimeError("safe failure")
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            self.assertLogs(level="ERROR") as logs,
        ):
            result = main(["deliver"])

        self.assertEqual(result, 1)
        self.assertIn("safe failure", logs.output[0])
