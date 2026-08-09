"""Discord integration tests."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from discord import Embed
from noctalia_i18n_core import Change

from noctalia_i18n_watcher.config import MonitorConfig, WebhookConfig
from noctalia_i18n_watcher.discord import (
    DiscordNotifier,
    DiscordRoute,
    DiscordWebhookClient,
    baseline_embed,
    send_status,
    status_embed,
)
from noctalia_i18n_watcher.messages import load_merge, load_message
from noctalia_i18n_watcher.monitor import Delivery, DeliveryPolicy
from noctalia_i18n_watcher.state import StateSummary


class _Sender:
    def __init__(self, *, fail_on_call: int = 0) -> None:
        self.route_id = ""
        self.embeds: tuple[Embed, ...] = ()
        self.username = ""
        self.avatar_url = ""
        self.calls = 0
        self.fail_on_call = fail_on_call

    def send(
        self,
        route_id: str,
        *,
        embeds: Sequence[Embed],
        username: str = "",
        avatar_url: str = "",
    ) -> None:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("delivery failed")
        self.route_id = route_id
        self.embeds = tuple(embeds)
        self.username = username
        self.avatar_url = avatar_url


def _webhook() -> WebhookConfig:
    return WebhookConfig(
        id="primary",
        url="https://discord.com/api/webhooks/id/token",
        username="Watcher",
        avatar_url="https://example.com/avatar.png",
        locales=frozenset({"en", "zh-Hans"}),
        actions=frozenset({"modified"}),
        source_message="english",
        target_message="english",
        merge_message="english",
        delivery=DeliveryPolicy(240, 900, True),
        merge_threshold=5,
        notify_baseline=True,
    )


def _deliveries(count: int) -> tuple[Delivery, ...]:
    return tuple(
        Delivery(
            Change(
                id=f"change-{index}",
                key=f"key.{index}",
                locale="zh-Hans",
                actor_login="fixture",
                old_value="old",
                new_value="new",
                action="modified",
                occurred_at=datetime(2026, 7, 30, index, tzinfo=UTC),
                translation_url="https://i18n.noctalia.dev/projects/noctalia/translate",
            ),
            "English",
        )
        for index in range(count)
    )


class DiscordTests(unittest.TestCase):
    def test_application_embeds_include_route_context(self) -> None:
        monitor = MonitorConfig("watcher", "official-plugins", ())
        webhook = _webhook()
        summary = StateSummary(True, None, 2221, 125, 1, 1, 1)

        for embed in (
            baseline_embed(monitor, webhook, 5, 2172),
            status_embed(monitor, webhook, summary),
        ):
            with self.subTest(title=embed.title):
                text = str(embed.to_dict())
                for value in (
                    "watcher",
                    "official-plugins",
                    "primary",
                    "zh-Hans",
                    "modified",
                ):
                    self.assertIn(value, text)

    def test_status_uses_the_route_identity(self) -> None:
        sender = _Sender()
        monitor = MonitorConfig("watcher", "noctalia", ())
        webhook = _webhook()
        summary = StateSummary(True, None, 2221, 125, 1, 1, 1)

        send_status(sender, monitor, webhook, summary)

        self.assertEqual(sender.route_id, "primary")
        self.assertEqual(sender.username, "Watcher")
        self.assertEqual(sender.avatar_url, "https://example.com/avatar.png")
        self.assertEqual(sender.embeds[0].title, "Watcher Status")

    def test_webhook_client_disables_mentions(self) -> None:
        webhook = Mock()
        with (
            requests.Session() as session,
            patch(
                "noctalia_i18n_watcher.discord.SyncWebhook.from_url",
                return_value=webhook,
            ),
        ):
            client = DiscordWebhookClient(
                session,
                {"primary": "https://discord.com/api/webhooks/id/token"},
            )
            client.send("primary", embeds=(Embed(description="@everyone"),))

        arguments = webhook.send.call_args.kwargs
        self.assertEqual(arguments["allowed_mentions"].to_dict(), {"parse": []})
        self.assertNotIn("username", arguments)
        self.assertNotIn("avatar_url", arguments)

    def test_webhook_errors_hide_credentials(self) -> None:
        webhook = Mock()
        webhook.send.side_effect = requests.ConnectionError(
            "https://discord.com/api/webhooks/id/SECRET"
        )
        with (
            requests.Session() as session,
            patch(
                "noctalia_i18n_watcher.discord.SyncWebhook.from_url",
                return_value=webhook,
            ),
        ):
            client = DiscordWebhookClient(
                session,
                {"primary": "https://discord.com/api/webhooks/id/token"},
            )
            with self.assertRaises(RuntimeError) as raised:
                client.send("primary", embeds=(Embed(description="test"),))

        self.assertEqual(str(raised.exception), "Cannot reach Discord: network failure")

    def test_notifier_preserves_acknowledgements_after_a_partial_failure(
        self,
    ) -> None:
        sender = _Sender(fail_on_call=2)
        message = load_message(
            "compact",
            Path(__file__).parents[1] / "config" / "messages" / "target",
        )
        route = DiscordRoute(
            id="primary",
            monitor_id="monitor",
            project="noctalia",
            locales=frozenset({"zh-Hans"}),
            actions=frozenset({"modified"}),
            delivery=DeliveryPolicy(0, 0, False),
            merge_threshold=None,
            source_template=None,
            target_template=message,
            merge_template=None,
            baseline_renderer=None,
            username="",
            avatar_url="",
        )
        deliveries = _deliveries(11)
        acknowledged: list[Delivery] = []

        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            DiscordNotifier((route,), sender).send(
                route.id,
                deliveries,
                acknowledged.extend,
            )

        self.assertEqual(sender.calls, 2)
        self.assertEqual(acknowledged, list(deliveries[:10]))

    def test_notifier_merges_each_locale_above_its_threshold(self) -> None:
        root = Path(__file__).parents[1] / "config" / "messages"
        message = load_message("compact", root / "source")
        route = DiscordRoute(
            id="primary",
            monitor_id="monitor",
            project="noctalia",
            locales=frozenset({"en", "zh-Hans"}),
            actions=frozenset({"modified"}),
            delivery=DeliveryPolicy(0, 0, False),
            merge_threshold=1,
            source_template=message,
            target_template=load_message("compact", root / "target"),
            merge_template=load_merge("english", root / "merge"),
            baseline_renderer=None,
            username="",
            avatar_url="",
        )
        first, source, last = _deliveries(3)
        source = replace(source, change=replace(source.change, locale="en"))

        embeds = DiscordNotifier((route,), _Sender()).render(
            route.id,
            (first, source, last),
        )

        self.assertEqual(len(embeds), 2)
        self.assertIn("key.0", str(embeds[0].to_dict()))
        self.assertIn("key.2", str(embeds[0].to_dict()))
        self.assertIn("key.1", str(embeds[1].to_dict()))
