"""Discord application message tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from noctalia_i18n_core import StateSummary

from noctalia_i18n_watcher.discord import send_status


class _Sender:
    def __init__(self) -> None:
        self.payload: Mapping[str, object] | None = None

    def send(self, _target_ref: str, payload: Mapping[str, object], /) -> None:
        self.payload = payload


class DiscordTests(unittest.TestCase):
    def test_status_uses_webhook_identity_and_disables_mentions(self) -> None:
        sender = _Sender()
        summary = StateSummary(False, None, 0, 0, 0, 0, 0)
        send_status(
            sender,
            "route",
            summary,
            username="Watcher",
            avatar_url="https://example.com/avatar.png",
        )

        payload = sender.payload
        if payload is None:
            self.fail("Status payload was not sent")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["username"], "Watcher")
        self.assertEqual(payload["avatar_url"], "https://example.com/avatar.png")
