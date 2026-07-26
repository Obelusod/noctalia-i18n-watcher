"""Explicit live contracts for upstream services."""

from __future__ import annotations

import os
import unittest
from contextlib import closing
from pathlib import Path

from noctalia_i18n_watcher.config import load_config
from noctalia_i18n_watcher.discord import DiscordNotifier
from noctalia_i18n_watcher.sources import HistorySource, build_source

from .factories import discord_options, discord_route, recorded_delivery
from .fixtures import CONFIG_PATH

_WEBHOOK_URL = os.environ.get("NOCTALIA_TEST_WEBHOOK_URL", "")
_CONFIG_PATH = Path(os.environ.get("NOCTALIA_LIVE_CONFIG", str(CONFIG_PATH)))


class LiveSourceTests(unittest.TestCase):
    def test_configured_source_satisfies_the_public_contract(self) -> None:
        config = load_config(_CONFIG_PATH)
        for monitor in config.monitors:
            with (
                self.subTest(monitor=monitor.id),
                closing(
                    build_source(
                        monitor.source.adapter,
                        monitor.source.options,
                        project=monitor.source.project,
                        timeout=config.settings.http.request_timeout,
                        proxy=config.settings.http.proxy_url,
                    )
                ) as source,
            ):
                changes = (
                    source.history(1) if isinstance(source, HistorySource) else None
                )
                baseline = source.poll(None)
                followup = source.poll(baseline.cursor)

                if changes is not None:
                    self.assertEqual(
                        len({change.id for change in changes}),
                        len(changes),
                    )
                self.assertEqual(baseline.changes, ())
                self.assertIsNotNone(baseline.source_texts)
                if followup.changes:
                    self.assertIsNotNone(followup.source_texts)


@unittest.skipUnless(
    _WEBHOOK_URL,
    "NOCTALIA_TEST_WEBHOOK_URL is required for the Discord live contract",
)
class LiveDiscordTests(unittest.TestCase):
    def test_discord_accepts_a_rendered_notification(self) -> None:
        route = discord_route(url=_WEBHOOK_URL)
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
        )
        try:
            notifier.send(route.id, (recorded_delivery(),))
        finally:
            notifier.close()


if __name__ == "__main__":
    unittest.main()
