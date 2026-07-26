"""Discord routing, rendering, and delivery tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from unittest.mock import MagicMock, patch

import requests

from noctalia_i18n_watcher.discord import DiscordNotifier
from noctalia_i18n_watcher.messages import Embed, MergePage, MessageValues
from noctalia_i18n_watcher.models import (
    Action,
    StateSummary,
    WatcherError,
)

from .factories import (
    delivery,
    delivery_policy,
    discord_options,
    discord_route,
    merge_policy,
)
from .fixtures import fixture_change


class _PagedMerge:
    def __init__(self, page_size: int, page_chars: int = 0) -> None:
        self._page_size = page_size
        self._page_chars = page_chars

    def render(
        self,
        values: Sequence[MessageValues],
        /,
    ) -> tuple[MergePage, ...]:
        pages: list[MergePage] = []
        for start in range(0, len(values), self._page_size):
            page = values[start : start + self._page_size]
            description = (
                "\n".join(value["key"] for value in page) + "x" * self._page_chars
            )
            pages.append(MergePage(len(page), {"description": description}))
        return tuple(pages)


class DiscordTests(unittest.TestCase):
    def test_batch_above_route_threshold_uses_one_locale_merge(self) -> None:
        route = discord_route(delivery=delivery_policy(merge=merge_policy(threshold=2)))
        items = tuple(
            delivery(id=f"change-{index}", key=f"fixture.entry.{index}")
            for index in range(3)
        )

        embeds = DiscordNotifier(
            (route,),
            discord_options(),
        ).render(route.id, items)

        self.assertEqual(len(embeds), 1)
        self.assertEqual(
            embeds[0].get("description"),
            "fixture.entry.0\nfixture.entry.1\nfixture.entry.2",
        )

    def test_merge_pages_are_delivered_without_losing_acknowledgements(self) -> None:
        route = discord_route(
            merge_message=_PagedMerge(2, page_chars=3500),
            delivery=delivery_policy(merge=merge_policy(threshold=0)),
        )
        items = tuple(
            delivery(id=f"change-{index}", key=f"fixture.entry.{index}")
            for index in range(4)
        )
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        acknowledged: list[str] = []
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )

        notifier.send(
            route.id,
            items,
            lambda sent: acknowledged.extend(item.change.id for item in sent),
        )

        self.assertGreater(session.post.call_count, 1)
        self.assertEqual(acknowledged, [f"change-{index}" for index in range(4)])

    def test_failed_merge_part_leaves_only_unsent_entries_unacknowledged(
        self,
    ) -> None:
        route = discord_route(
            merge_message=_PagedMerge(2, page_chars=3500),
            delivery=delivery_policy(merge=merge_policy(threshold=0)),
        )
        items = tuple(
            delivery(id=f"change-{index}", key=f"fixture.entry.{index}")
            for index in range(4)
        )
        failed = MagicMock(status_code=404)
        failed.raise_for_status.side_effect = requests.HTTPError()
        session = MagicMock()
        session.post.side_effect = [MagicMock(status_code=204), failed]
        acknowledged: list[str] = []
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )

        with self.assertRaises(WatcherError):
            notifier.send(
                route.id,
                items,
                lambda sent: acknowledged.extend(item.change.id for item in sent),
            )

        self.assertTrue(acknowledged)
        self.assertEqual(
            acknowledged,
            [item.change.id for item in items[: len(acknowledged)]],
        )
        self.assertLess(len(acknowledged), len(items))

    def test_merge_groups_interleaved_locales_by_first_appearance(self) -> None:
        route = discord_route(
            locales=frozenset({"*"}),
            delivery=delivery_policy(merge=merge_policy(threshold=0)),
        )
        items = (
            delivery(id="de-1", key="de.1", locale="de"),
            delivery(id="fr-1", key="fr.1", locale="fr"),
            delivery(id="de-2", key="de.2", locale="de"),
            delivery(id="fr-2", key="fr.2", locale="fr"),
        )

        options = discord_options()
        embeds = DiscordNotifier((route,), options).render(route.id, items)

        self.assertEqual(
            [embed.get("description") for embed in embeds],
            [
                "de.1\nde.2",
                "fr.1\nfr.2",
            ],
        )

    def test_threshold_is_applied_to_each_locale_group(self) -> None:
        route = discord_route(
            locales=frozenset({"*"}),
            delivery=delivery_policy(merge=merge_policy(threshold=2)),
        )
        items = tuple(
            delivery(id=f"{locale}-{index}", locale=locale)
            for locale in ("de", "fr", "ja")
            for index in range(2)
        )

        options = discord_options()
        embeds = DiscordNotifier((route,), options).render(route.id, items)

        self.assertEqual(len(embeds), 6)

    def test_unmerged_interleaved_changes_keep_chronological_order(self) -> None:
        route = discord_route(
            locales=frozenset({"*"}),
            delivery=delivery_policy(merge=merge_policy(threshold=2)),
        )
        items = (
            delivery(id="de-1", key="de.1", locale="de", url=None),
            delivery(id="fr-1", key="fr.1", locale="fr", url=None),
            delivery(id="de-2", key="de.2", locale="de", url=None),
        )

        embeds = DiscordNotifier(
            (route,),
            discord_options(),
        ).render(route.id, items)

        self.assertEqual(
            [embed.get("description") for embed in embeds],
            [
                "de:de.1:modified",
                "fr:fr.1:modified",
                "de:de.2:modified",
            ],
        )

    def test_route_switch_disables_merging(self) -> None:
        route = discord_route(
            delivery=delivery_policy(merge=merge_policy(enabled=False, threshold=0))
        )
        items = tuple(delivery(id=f"change-{index}") for index in range(3))

        options = discord_options()
        embeds = DiscordNotifier((route,), options).render(route.id, items)

        self.assertEqual(len(embeds), 3)

    def test_route_ids_must_be_unique_at_the_adapter_boundary(self) -> None:
        route = discord_route()
        with self.assertRaisesRegex(ValueError, "route IDs must be unique"):
            DiscordNotifier((route, route), discord_options())

    def test_render_has_no_delivery_side_effects(self) -> None:
        session = MagicMock()
        route = discord_route()
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )

        embeds = notifier.render(route.id, (delivery(),))

        self.assertEqual(len(embeds), 1)
        session.post.assert_not_called()

    def test_payload_keeps_identity_and_disables_mentions(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(username="Noctalia i18n Watcher")
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        notifier.send(route.id, (delivery(),), lambda _batch: None)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["username"], "Noctalia i18n Watcher")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_empty_identity_uses_the_discord_webhook_profile(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(username="", avatar_url="")
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )

        notifier.send(route.id, (delivery(),))

        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("username", payload)
        self.assertNotIn("avatar_url", payload)

    def test_route_matches_locale_and_action_filters(self) -> None:
        route = discord_route(
            locales=frozenset({"zh-Hans"}),
            actions=frozenset({"modified"}),
        )
        self.assertTrue(route.matches(fixture_change()))
        self.assertFalse(
            route.matches(
                fixture_change(action="deleted", new_value=None, old_value="old")
            )
        )

    def test_wildcard_route_accepts_an_unconfigured_project_locale(self) -> None:
        changed = fixture_change(locale="sr-Latn-RS")

        self.assertTrue(discord_route(locales=frozenset({"*"})).matches(changed))
        self.assertFalse(discord_route(locales=frozenset({"zh-Hans"})).matches(changed))

    def test_delivery_batches_and_acknowledges_successful_requests(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(merge_message=None)
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        deliveries = [delivery(id=f"change-{index}") for index in range(21)]
        acknowledged: list[list[str]] = []
        notifier.send(
            route.id,
            deliveries,
            lambda batch: acknowledged.append([item.change.id for item in batch]),
        )
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(
            acknowledged,
            [
                [f"change-{index}" for index in range(10)],
                [f"change-{index}" for index in range(10, 20)],
                ["change-20"],
            ],
        )

    def test_embed_must_fit_discord_batch_limit(self) -> None:
        class OversizedMessage:
            def render(
                self,
                _action: Action,
                _values: Mapping[str, object],
            ) -> Embed:
                return {"description": "x" * 6001}

        route = discord_route(target_message=OversizedMessage(), merge_message=None)
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=MagicMock(),
        )
        with self.assertRaisesRegex(WatcherError, "exceeding"):
            notifier.send(route.id, (delivery(),), lambda _batch: None)

    def test_baseline_embed_summarizes_route_configuration(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(
            id="primary",
            monitor_id="watcher",
            project="official-plugins",
        )
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        notifier.send_baseline(route.id, 5, 2172)
        embed = session.post.call_args.kwargs["json"]["embeds"][0]
        fields = embed["fields"]
        text = "\n".join(field["value"] for field in fields)

        self.assertTrue(embed.get("title"))
        self.assertEqual(
            [field.get("inline", False) for field in fields],
            [True, True, False],
        )
        for fact in (
            "5 changes",
            "`en` (source) · `zh-Hans`",
            "2,172 keys",
            "`added` · `modified` · `deleted`",
            "`4m`",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, text)
        footer = embed["footer"]["text"]
        for identifier in ("watcher", "official-plugins", "primary"):
            with self.subTest(identifier=identifier):
                self.assertIn(identifier, footer)

    def test_baseline_embed_handles_wildcard_and_individual_delivery(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(
            locales=frozenset({"*"}),
            actions=frozenset[Action]({"modified"}),
            delivery=delivery_policy(
                quiet_seconds=0,
                max_wait_seconds=0,
                latest_only=False,
                merge=merge_policy(enabled=False),
            ),
        )
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        notifier.send_baseline(route.id, 1, 1)
        embed = session.post.call_args.kwargs["json"]["embeds"][0]
        text = "\n".join(field["value"] for field in embed["fields"])
        self.assertIn("`*` (all)", text)
        self.assertIn("`modified`", text)
        for fact in ("No inactivity delay", "`0s` maximum", "Every change"):
            self.assertIn(fact, embed["fields"][-1]["value"])

    def test_status_embed_summarizes_state_and_route(self) -> None:
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route(locales=frozenset({"*"}))
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        summary = StateSummary(
            initialized=True,
            updated_at="2026-07-18T04:05:06+00:00",
            source_texts=2221,
            deliveries=125,
            baseline_notifications=1,
            pending_deliveries=1,
            pending_routes=1,
        )

        notifier.send_status(route.id, summary)

        embed = session.post.call_args.kwargs["json"]["embeds"][0]
        fields = embed["fields"]
        text = "\n".join(field["value"] for field in fields)
        self.assertTrue(embed.get("title"))
        self.assertEqual(
            [field.get("inline", False) for field in fields],
            [True, True, False],
        )
        for fact in (
            "Ready",
            "`*` (all)",
            "125 recorded",
            "2,221 keys",
            "1 delivery across 1 route",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, text)
        self.assertNotIn("timestamp", embed)

    def test_proxy_wait_and_timeout_are_applied_by_the_adapter(self) -> None:
        proxy = "http://proxy.invalid:8080"
        session = MagicMock()
        session.proxies = {}
        session.post.return_value = MagicMock(status_code=204)
        route = discord_route()
        notifier = DiscordNotifier(
            (route,),
            discord_options(proxy_url=proxy),
            session=session,
        )
        notifier.send(route.id, (delivery(),))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {"http": proxy, "https": proxy})
        self.assertEqual(
            session.post.call_args.args[0],
            f"{route.url}?wait=true",
        )
        self.assertEqual(session.post.call_args.kwargs["timeout"], 30.0)

    def test_transient_network_failure_uses_retry_policy(self) -> None:
        session = MagicMock()
        session.post.side_effect = [
            requests.ConnectionError("temporary"),
            MagicMock(status_code=204),
        ]
        route = discord_route()
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        with (
            patch("noctalia_i18n_watcher.discord.time.sleep") as sleep,
            self.assertLogs("noctalia_i18n_watcher.discord", level="WARNING"),
        ):
            notifier.send(route.id, (delivery(),))
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_final_errors_never_expose_webhook_tokens(self) -> None:
        route = discord_route(url="https://discord.com/api/webhooks/123/SECRET_TOKEN")
        response = MagicMock(status_code=404)
        response.raise_for_status.side_effect = requests.HTTPError("SECRET_TOKEN")
        session = MagicMock()
        session.post.return_value = response
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        with self.assertRaises(WatcherError) as raised:
            notifier.send(route.id, (delivery(),))
        self.assertEqual(
            str(raised.exception), f"Discord webhook {route.id!r} returned HTTP 404"
        )

    def test_rate_limit_delay_is_finite_and_bounded(self) -> None:
        limited = MagicMock(status_code=429)
        limited.json.return_value = {"retry_after": float("inf")}
        session = MagicMock()
        session.post.side_effect = [limited, MagicMock(status_code=204)]
        route = discord_route()
        notifier = DiscordNotifier(
            (route,),
            discord_options(),
            session=session,
        )
        with (
            patch("noctalia_i18n_watcher.discord.time.sleep") as sleep,
            self.assertLogs("noctalia_i18n_watcher.discord", level="WARNING"),
        ):
            notifier.send(route.id, (delivery(),))
        sleep.assert_called_once_with(1)
