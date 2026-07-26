"""Application boundary tests."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import call, patch

from noctalia_i18n_watcher.app import Application, RuntimePaths
from noctalia_i18n_watcher.config import MessageSelection
from noctalia_i18n_watcher.discord import DiscordNotifier
from noctalia_i18n_watcher.models import (
    Change,
    Checkpoint,
    JsonValue,
    PollResult,
    WatcherError,
)
from noctalia_i18n_watcher.state import SQLiteState

from .factories import (
    app_config,
    delivery_policy,
    merge_policy,
    monitor_config,
    webhook_config,
)
from .fixtures import RECORDED_SOURCE, recorded_change


class _StubSource:
    """Controllable source implementing the public acquisition capabilities."""

    def __init__(
        self,
        result: PollResult,
        history: tuple[tuple[Change, ...], ...] = (),
    ) -> None:
        self.result = result
        self.history_pages = history
        self.cursors: list[JsonValue | None] = []
        self.history_requests: list[int] = []
        self.closed = False

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        self.cursors.append(cursor)
        return self.result

    def history(self, page: int, /) -> tuple[Change, ...]:
        self.history_requests.append(page)
        return self.history_pages[page - 1]

    def close(self) -> None:
        self.closed = True


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1]
        self.messages = self.root / "config" / "messages"

    def _paths(self, state: Path) -> RuntimePaths:
        return RuntimePaths(
            config=self.root / "config" / "config.json",
            messages=self.messages,
            state=state,
        )

    def test_repository_runtime_inputs_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(Path(directory))
            Application.load(paths).validate()

    def test_runtime_paths_follow_service_environment(self) -> None:
        paths = RuntimePaths.from_environment(
            {
                "CREDENTIALS_DIRECTORY": "/run/credentials/service",
                "STATE_DIRECTORY": "/var/lib/service",
            }
        )

        self.assertEqual(
            paths,
            RuntimePaths(
                config=Path("/run/credentials/service/config.json"),
                messages=Path("/etc/noctalia-i18n-watcher/messages"),
                state=Path("/var/lib/service"),
            ),
        )
        self.assertEqual(
            RuntimePaths.from_environment({}).state,
            Path("/var/lib/noctalia-i18n-watcher"),
        )

    def test_preview_runs_the_real_read_only_pipeline(self) -> None:
        changed = recorded_change()
        previous = Checkpoint("previous", {changed.key: "Previous source"})
        source = _StubSource(
            PollResult(
                (changed,),
                "next",
                1,
                {changed.key: RECORDED_SOURCE},
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = state_dir / "noctalia.sqlite3"
            with closing(SQLiteState(database)) as state:
                state.save(previous)
            paths = self._paths(state_dir)

            with patch(
                "noctalia_i18n_watcher.app.build_source",
                return_value=source,
            ):
                result = Application(app_config(), paths).preview()

            with closing(SQLiteState(database, read_only=True)) as state:
                self.assertEqual(state.load(), previous)

        self.assertTrue(source.closed)
        self.assertEqual(source.cursors, ["previous"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["monitor_id"], "noctalia")
        self.assertEqual(result[0]["route_id"], "default")
        embed = result[0]["embed"]
        if not isinstance(embed, dict):
            self.fail("Preview did not return an embed object")
        self.assertTrue(embed)

    def test_validation_ignores_messages_outside_the_route_scope(self) -> None:
        source_only = webhook_config(
            locales=frozenset({"en"}),
            messages=MessageSelection(
                source="english",
                target="../unused-target",
                merge="../unused-merge",
            ),
            delivery=delivery_policy(merge=merge_policy(enabled=False)),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(Path(directory))
            Application(
                app_config(monitor_config(source_only)),
                paths,
            ).validate()

    def test_status_reads_persisted_state_without_modifying_it(self) -> None:
        checkpoint = Checkpoint({"cursor": "one"}, {"key": "English"})
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = state_dir / "noctalia.sqlite3"
            with closing(SQLiteState(database)) as state:
                state.save(checkpoint)
            paths = self._paths(state_dir)

            summary = Application(app_config(), paths).status()["noctalia"]

            with closing(SQLiteState(database, read_only=True)) as state:
                self.assertEqual(state.load(), checkpoint)

        self.assertTrue(summary.initialized)
        self.assertEqual(summary.source_texts, 1)
        self.assertEqual(summary.pending_deliveries, 0)

    def test_status_can_notify_every_configured_route(self) -> None:
        first = webhook_config(id="first")
        second = webhook_config(id="second")
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(Path(directory))
            with closing(SQLiteState(paths.state / "noctalia.sqlite3")):
                pass
            application = Application(
                app_config(monitor_config(first, second)),
                paths,
            )
            with patch.object(DiscordNotifier, "send_status") as send_status:
                summary = application.status(notify=True)["noctalia"]

        send_status.assert_has_calls([call("first", summary), call("second", summary)])

    def test_history_exposes_paged_source_data_and_closes_the_source(self) -> None:
        expected = (recorded_change(),)
        source = _StubSource(PollResult((), "cursor", 0), (expected,))

        with patch(
            "noctalia_i18n_watcher.app.build_source",
            return_value=source,
        ):
            changes = Application(
                app_config(),
                self._paths(Path("unused")),
            ).history(1)

        self.assertEqual(changes, expected)
        self.assertEqual(source.history_requests, [1])
        self.assertTrue(source.closed)

    def test_status_keeps_monitor_state_isolated(self) -> None:
        second = monitor_config(
            id="community-plugins",
            source=replace(
                monitor_config().source,
                project="community-plugins",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with closing(SQLiteState(state_dir / "noctalia.sqlite3")) as state:
                state.save(Checkpoint("first", {"one": "English"}))
            with closing(SQLiteState(state_dir / "community-plugins.sqlite3")) as state:
                state.save(
                    Checkpoint(
                        "second",
                        {"one": "English", "two": "More English"},
                    )
                )

            summaries = Application(
                app_config(monitor_config(), second),
                self._paths(state_dir),
            ).status()

        self.assertEqual(set(summaries), {"noctalia", "community-plugins"})
        self.assertEqual(summaries["noctalia"].source_texts, 1)
        self.assertEqual(summaries["community-plugins"].source_texts, 2)

    def test_monitor_selection_limits_source_access(self) -> None:
        second = monitor_config(
            id="community-plugins",
            source=replace(
                monitor_config().source,
                project="community-plugins",
            ),
        )
        source = _StubSource(PollResult((), "cursor", 0, {}))
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with closing(SQLiteState(state_dir / "community-plugins.sqlite3")) as state:
                state.save(Checkpoint("cursor", {}))
            with patch(
                "noctalia_i18n_watcher.app.build_source",
                return_value=source,
            ) as build:
                Application(
                    app_config(monitor_config(), second),
                    self._paths(state_dir),
                ).preview("community-plugins")

        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.kwargs["project"], "community-plugins")

    def test_expected_failure_does_not_skip_later_monitors(self) -> None:
        second = monitor_config(
            id="community-plugins",
            source=replace(
                monitor_config().source,
                project="community-plugins",
            ),
        )
        source = _StubSource(PollResult((), "cursor", 0, {}))
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            for monitor_id in ("noctalia", "community-plugins"):
                with closing(SQLiteState(state_dir / f"{monitor_id}.sqlite3")) as state:
                    state.save(Checkpoint("cursor", {}))
                    state.record_baseline("default")
            with (
                patch(
                    "noctalia_i18n_watcher.app.build_source",
                    side_effect=(WatcherError("unavailable"), source),
                ),
                self.assertRaisesRegex(
                    WatcherError,
                    "Run failed for noctalia: unavailable",
                ),
            ):
                Application(
                    app_config(monitor_config(), second),
                    self._paths(state_dir),
                ).run()

        self.assertEqual(source.cursors, ["cursor"])
        self.assertTrue(source.closed)

    def test_history_requires_selection_with_multiple_monitors(self) -> None:
        with self.assertRaisesRegex(WatcherError, "requires --monitor"):
            Application(
                app_config(
                    monitor_config(),
                    monitor_config(id="community-plugins"),
                ),
                self._paths(Path("unused")),
            ).history(1)
