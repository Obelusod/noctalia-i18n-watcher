"""Discord application message tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from noctalia_i18n_core import Action, DeliveryPolicy, StateSummary

from noctalia_i18n_watcher.config import MonitorConfig, WebhookConfig
from noctalia_i18n_watcher.discord import baseline_embed, send_status, status_embed


class _Sender:
    def __init__(self) -> None:
        self.target_ref = ""
        self.payload: Mapping[str, object] | None = None

    def send(self, target_ref: str, payload: Mapping[str, object], /) -> None:
        self.target_ref = target_ref
        self.payload = payload


def _webhook() -> WebhookConfig:
    return WebhookConfig(
        id="primary",
        url="https://discord.com/api/webhooks/id/token",
        username="Watcher",
        avatar_url="https://example.com/avatar.png",
        locales=frozenset({"en", "zh-Hans"}),
        actions=frozenset[Action]({"modified"}),
        source_message="english",
        target_message="english",
        merge_message="english",
        delivery=DeliveryPolicy(240, 900, True, 5),
        notify_baseline=True,
    )


class DiscordTests(unittest.TestCase):
    def test_application_embeds_include_route_context(self) -> None:
        monitor = MonitorConfig("watcher", "official-plugins", ())
        webhook = _webhook()
        summary = StateSummary(True, None, 2221, 125, 1, 1, 1)
        embeds = (
            baseline_embed(monitor, webhook, 5, 2172),
            status_embed(monitor, webhook, summary),
        )

        for embed in embeds:
            with self.subTest(title=embed.get("title")):
                text = str(embed)
                for value in (
                    "watcher",
                    "official-plugins",
                    "primary",
                    "zh-Hans",
                    "modified",
                ):
                    self.assertIn(value, text)

    def test_status_uses_webhook_identity_and_disables_mentions(self) -> None:
        sender = _Sender()
        monitor = MonitorConfig("watcher", "noctalia", ())
        webhook = _webhook()
        summary = StateSummary(True, None, 2221, 125, 1, 1, 1)
        send_status(sender, monitor, webhook, summary)

        payload = sender.payload
        if payload is None:
            self.fail("Status payload was not sent")
        self.assertEqual(sender.target_ref, "primary")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["username"], "Watcher")
        self.assertEqual(payload["avatar_url"], "https://example.com/avatar.png")
