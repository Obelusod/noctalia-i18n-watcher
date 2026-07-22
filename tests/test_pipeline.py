"""End-to-end monitoring pipeline tests with local infrastructure."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import requests

from noctalia_i18n_watcher.discord import DiscordNotifier
from noctalia_i18n_watcher.models import Checkpoint, JsonValue, PollResult, WatcherError
from noctalia_i18n_watcher.monitor import Monitor
from noctalia_i18n_watcher.state import SQLiteState

from .factories import discord_options, webhook
from .fixtures import RECORDED_SOURCE, RUN_AT, recorded_change


class _Source:
    def __init__(self, result: PollResult) -> None:
        self.result = result
        self.cursors: list[JsonValue | None] = []

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        self.cursors.append(cursor)
        return self.result


def _monitor(
    source: _Source,
    state: SQLiteState,
    notifier: DiscordNotifier,
    now: datetime = RUN_AT,
) -> Monitor:
    return Monitor(
        source,
        state,
        notifier,
        retention_days=180,
        clock=lambda: now,
    )


class MonitoringPipelineTests(unittest.TestCase):
    def test_successful_delivery_advances_state_and_acknowledges_the_outbox(
        self,
    ) -> None:
        changed = recorded_change()
        source = _Source(
            PollResult(
                (changed,),
                "next",
                1,
                {changed.key: RECORDED_SOURCE},
            )
        )
        route = webhook()
        session = MagicMock(spec=requests.Session)
        session.post.return_value = MagicMock(status_code=204)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with (
                closing(SQLiteState(path)) as state,
                closing(
                    DiscordNotifier(
                        (route,),
                        discord_options(),
                        session=session,
                    )
                ) as notifier,
            ):
                state.save(Checkpoint("previous", {changed.key: "Previous English"}))
                state.record_baseline(route.id)

                _monitor(source, state, notifier).run(flush=True)

                self.assertEqual(source.cursors, ["previous"])
                self.assertEqual(
                    state.load(),
                    Checkpoint("next", {changed.key: RECORDED_SOURCE}),
                )
                self.assertEqual(state.pending(route.id), ())
                self.assertEqual(state.summary().deliveries, 1)

        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(
            [field["value"] for field in payload["embeds"][0]["fields"]],
            [
                (
                    f"[`{changed.key}`]"
                    "(https://i18n.noctalia.dev/projects/noctalia/translate?search="
                    f"{changed.key}&locale=zh-Hans)"
                ),
                f"`{RECORDED_SOURCE}`",
                (
                    "```ansi\n"
                    "− 在 Toast 通知周围绘制\x1b[1;31m边框\x1b[0m\n"
                    "+ 在 Toast 通知周围绘制\x1b[1;32m轮廓\x1b[0m\n"
                    "```"
                ),
            ],
        )

    def test_failed_delivery_keeps_the_checkpoint_and_pending_entry_for_retry(
        self,
    ) -> None:
        changed = recorded_change()
        source = _Source(
            PollResult(
                (changed,),
                "next",
                1,
                {changed.key: RECORDED_SOURCE},
            )
        )
        route = webhook()
        failed = MagicMock(status_code=404)
        failed.raise_for_status.side_effect = requests.HTTPError()
        session = MagicMock(spec=requests.Session)
        session.post.return_value = failed

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with (
                closing(SQLiteState(path)) as state,
                closing(
                    DiscordNotifier(
                        (route,),
                        discord_options(),
                        session=session,
                    )
                ) as notifier,
            ):
                state.save(Checkpoint("previous", {changed.key: "Previous English"}))
                state.record_baseline(route.id)

                with self.assertRaises(WatcherError):
                    _monitor(source, state, notifier).run(flush=True)

                self.assertEqual(
                    state.load(),
                    Checkpoint("next", {changed.key: RECORDED_SOURCE}),
                )
                pending = state.pending(route.id)

        self.assertEqual([item.delivery.change.id for item in pending], [changed.id])
