"""Command-line interface tests."""

from __future__ import annotations

import logging
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from noctalia_i18n_watcher.cli import main
from noctalia_i18n_watcher.models import StateSummary, WatcherError

from .fixtures import recorded_change


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._handlers = list(root.handlers)
        self._level = root.level

    def tearDown(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        root.handlers.extend(self._handlers)
        root.setLevel(self._level)

    def test_invalid_command_lines_exit_with_usage(self) -> None:
        cases = (
            [],
            ["--scheduled", "deliver"],
            ["reset"],
            ["history", "0"],
        )
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_dry_run_delegates_to_the_application(self) -> None:
        application = MagicMock(log_level="INFO")
        application.preview.return_value = ({"route_id": "default"},)
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main(["deliver", "--dry-run"]), 0)
        application.preview.assert_called_once_with(None)
        self.assertIn('"route_id": "default"', output.getvalue())

    def test_commands_dispatch_to_the_application_interface(self) -> None:
        cases = (
            (["--scheduled"], "run", (), {}),
            (["deliver"], "run", (None,), {"flush": True}),
            (
                ["deliver", "--monitor", "plugins"],
                "run",
                ("plugins",),
                {"flush": True},
            ),
            (
                ["reset", "baseline"],
                "reset",
                ("baseline", None),
                {"notify": False},
            ),
            (
                ["reset", "state"],
                "reset",
                ("state", None),
                {"notify": False},
            ),
            (
                ["reset", "baseline", "--monitor", "plugins", "--notify"],
                "reset",
                ("baseline", "plugins"),
                {"notify": True},
            ),
            (
                ["reset", "state", "--notify"],
                "reset",
                ("state", None),
                {"notify": True},
            ),
        )
        for arguments, method, positional, keywords in cases:
            application = MagicMock(log_level="INFO")
            with (
                self.subTest(arguments=arguments),
                patch(
                    "noctalia_i18n_watcher.cli.Application.load",
                    return_value=application,
                ),
            ):
                self.assertEqual(main(arguments), 0)
            getattr(application, method).assert_called_once_with(
                *positional,
                **keywords,
            )

    def test_validate_reports_success(self) -> None:
        application = MagicMock(log_level="INFO")
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main(["validate", "--monitor", "plugins"]), 0)
        application.validate.assert_called_once_with("plugins")
        self.assertIn("valid", output.getvalue().lower())

    def test_status_serializes_the_summary_and_honors_notify(self) -> None:
        cases = (
            (["status"], None, False),
            (["status", "--monitor", "plugins", "--notify"], "plugins", True),
        )
        for arguments, monitor, notify in cases:
            application = MagicMock(log_level="INFO")
            application.status.return_value = {
                "noctalia": StateSummary(
                    initialized=True,
                    updated_at=None,
                    source_texts=0,
                    deliveries=0,
                    baseline_notifications=0,
                    pending_deliveries=0,
                    pending_routes=0,
                )
            }
            with (
                self.subTest(arguments=arguments),
                patch(
                    "noctalia_i18n_watcher.cli.Application.load",
                    return_value=application,
                ),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(main(arguments), 0)
            application.status.assert_called_once_with(monitor, notify=notify)
            self.assertIn('"noctalia"', output.getvalue())
            self.assertIn('"initialized": true', output.getvalue())

    def test_history_serializes_application_data(self) -> None:
        application = MagicMock(log_level="INFO")
        application.history.return_value = (recorded_change(),)
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main(["history", "2", "--monitor", "plugins"]), 0)
        application.history.assert_called_once_with(2, "plugins")
        self.assertIn('"locale": "zh-Hans"', output.getvalue())
        self.assertIn('"occurred_at": "2026-07-17T09:51:04.838Z"', output.getvalue())

    def test_expected_errors_return_status_one(self) -> None:
        application = MagicMock(log_level="INFO")
        application.run.side_effect = WatcherError("safe failure")
        with (
            patch(
                "noctalia_i18n_watcher.cli.Application.load",
                return_value=application,
            ),
            self.assertLogs(level="ERROR") as logs,
        ):
            self.assertEqual(main(["deliver"]), 1)
        self.assertIn("safe failure", logs.output[0])
