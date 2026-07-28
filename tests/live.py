"""Explicit opt-in source and Discord checks."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import requests
from noctalia_i18n_core import DiscordWebhookSender

from noctalia_i18n_watcher.app import Application, RuntimePaths


class LiveSourceTests(unittest.TestCase):
    def test_history(self) -> None:
        value = os.environ.get("NOCTALIA_LIVE_CONFIG")
        if not value:
            self.skipTest("Set NOCTALIA_LIVE_CONFIG to opt into live source checks")
        config = Path(value)
        paths = RuntimePaths(config, config.parent / "messages", Path("unused"))

        self.assertTrue(Application.load(paths).history(1))


class LiveDiscordTests(unittest.TestCase):
    def test_webhook(self) -> None:
        url = os.environ.get("NOCTALIA_TEST_WEBHOOK_URL")
        if not url:
            self.skipTest("Set NOCTALIA_TEST_WEBHOOK_URL to opt into Discord checks")
        session = requests.Session()
        self.addCleanup(session.close)

        DiscordWebhookSender(session, {"test": url}, 30).send(
            "test",
            {
                "allowed_mentions": {"parse": []},
                "content": "Noctalia i18n watcher live transport check.",
            },
        )
