"""Discord routing, rendering, and webhook delivery."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict

import requests
from discord import AllowedMentions, Embed, HTTPException, SyncWebhook
from noctalia_i18n_core import (
    SOURCE_LOCALE,
    Action,
    Change,
    Delivery,
    DeliveryPolicy,
    StateSummary,
)

from .config import MonitorConfig, WebhookConfig
from .messages import MergePage, MergeTemplate, MessageTemplate, MessageValues

_BASELINE_COLOR = 0x5865F2
_STATUS_COLOR = 0x57F287
_MAX_EMBEDS_PER_MESSAGE = 10
_MAX_EMBED_TEXT_PER_MESSAGE = 6000
_NO_MENTIONS = AllowedMentions.none()


class _WebhookIdentity(TypedDict, total=False):
    username: str
    avatar_url: str


class DiscordSender(Protocol):
    """Discord embed delivery boundary."""

    def send(
        self,
        route_id: str,
        *,
        embeds: Sequence[Embed],
        username: str = "",
        avatar_url: str = "",
    ) -> None: ...


class DiscordWebhookClient:
    """Send Discord embeds through caller-owned HTTP resources."""

    def __init__(
        self,
        session: requests.Session,
        webhook_urls: Mapping[str, str],
    ) -> None:
        if any(
            type(route_id) is not str
            or not route_id.strip()
            or type(url) is not str
            or not url.strip()
            for route_id, url in webhook_urls.items()
        ):
            raise ValueError(
                "webhook_urls must map non-empty route IDs to webhook URLs"
            )
        self._session = session
        self._webhook_urls = dict(webhook_urls)

    def send(
        self,
        route_id: str,
        *,
        embeds: Sequence[Embed],
        username: str = "",
        avatar_url: str = "",
    ) -> None:
        try:
            url = self._webhook_urls[route_id]
        except KeyError:
            raise ValueError(f"Unknown Discord route: {route_id!r}") from None
        try:
            webhook = SyncWebhook.from_url(url, session=self._session)
            identity: _WebhookIdentity = {}
            if username:
                identity["username"] = username
            if avatar_url:
                identity["avatar_url"] = avatar_url
            webhook.send(
                embeds=list(embeds),
                allowed_mentions=_NO_MENTIONS,
                **identity,
            )
        except requests.RequestException:
            raise RuntimeError("Cannot reach Discord: network failure") from None
        except HTTPException as exc:
            raise RuntimeError(
                f"Discord rejected the webhook request: HTTP {exc.status}"
            ) from None
        except (TypeError, ValueError):
            raise ValueError("Invalid Discord webhook request") from None


@dataclass(frozen=True, slots=True)
class DiscordRoute:
    """One Discord endpoint and its rendering policy."""

    id: str
    monitor_id: str
    project: str
    locales: frozenset[str]
    actions: frozenset[Action]
    delivery: DeliveryPolicy
    merge_threshold: int | None
    source_template: MessageTemplate | None
    target_template: MessageTemplate | None
    merge_template: MergeTemplate | None
    baseline_renderer: Callable[[int, int], Embed] | None
    username: str
    avatar_url: str

    @property
    def notify_baseline(self) -> bool:
        return self.baseline_renderer is not None

    def accepts_locale(self, locale: str, /) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, change: Change, /) -> bool:
        return self.accepts_locale(change.locale) and change.action in self.actions

    def render(self, delivery: Delivery) -> Embed:
        change = delivery.change
        template = self.source_template if change.is_source else self.target_template
        if template is None:
            raise RuntimeError(f"Route {self.id!r} cannot render {change.locale!r}")
        return template.render(change.action, self._values(delivery))

    def render_merge(self, deliveries: Sequence[Delivery]) -> Sequence[MergePage]:
        if self.merge_template is None:
            raise RuntimeError(f"Route {self.id!r} has no merged message")
        return self.merge_template.render(
            tuple(self._values(delivery) for delivery in deliveries)
        )

    def _values(self, delivery: Delivery) -> MessageValues:
        change = delivery.change
        return MessageValues(
            monitor_id=self.monitor_id,
            project=self.project,
            key=change.key,
            source=delivery.source_text,
            old_value=change.old_value,
            new_value=change.new_value,
            locale=change.locale,
            actor=change.actor,
            actor_url=change.actor_url,
            actor_avatar_url=change.actor_avatar_url,
            action=change.action,
            change_url=change.url,
            timestamp=change.iso_timestamp,
            unix_time=int(change.occurred_at.timestamp()),
        )


@dataclass(frozen=True, slots=True)
class _RenderedEmbed:
    deliveries: tuple[Delivery, ...]
    embed: Embed


def _message_batches(
    items: Iterable[_RenderedEmbed],
) -> Iterator[tuple[_RenderedEmbed, ...]]:
    batch: list[_RenderedEmbed] = []
    characters = 0
    for item in items:
        size = len(item.embed)
        if size > _MAX_EMBED_TEXT_PER_MESSAGE:
            raise ValueError("Discord embed exceeds the 6000-character limit")
        if batch and (
            len(batch) >= _MAX_EMBEDS_PER_MESSAGE
            or characters + size > _MAX_EMBED_TEXT_PER_MESSAGE
        ):
            yield tuple(batch)
            batch = []
            characters = 0
        batch.append(item)
        characters += size
    if batch:
        yield tuple(batch)


class DiscordNotifier:
    """Render route deliveries and send them through Discord webhooks."""

    def __init__(
        self,
        routes: Sequence[DiscordRoute],
        client: DiscordSender,
    ) -> None:
        identifiers = [route.id for route in routes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Route IDs must be unique")
        self._routes = tuple(routes)
        self._routes_by_id = {route.id: route for route in routes}
        self._client = client

    @property
    def routes(self) -> tuple[DiscordRoute, ...]:
        return self._routes

    def render(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        /,
    ) -> tuple[Embed, ...]:
        return tuple(
            item.embed for item in self._render(self._route(route_id), deliveries)
        )

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None],
        /,
    ) -> None:
        route = self._route(route_id)
        for batch in _message_batches(self._render(route, deliveries)):
            self._client.send(
                route.id,
                embeds=tuple(item.embed for item in batch),
                username=route.username,
                avatar_url=route.avatar_url,
            )
            acknowledge(
                tuple(delivery for item in batch for delivery in item.deliveries)
            )

    def send_baseline(
        self,
        route_id: str,
        changes: int,
        source_texts: int,
        /,
    ) -> None:
        route = self._route(route_id)
        if route.baseline_renderer is None:
            raise RuntimeError(f"Route {route.id!r} has no baseline message")
        self._client.send(
            route.id,
            embeds=(route.baseline_renderer(changes, source_texts),),
            username=route.username,
            avatar_url=route.avatar_url,
        )

    def _route(self, route_id: str) -> DiscordRoute:
        try:
            return self._routes_by_id[route_id]
        except KeyError:
            raise ValueError(f"Unknown route: {route_id!r}") from None

    @staticmethod
    def _render(
        route: DiscordRoute,
        deliveries: Sequence[Delivery],
    ) -> Iterator[_RenderedEmbed]:
        threshold = route.merge_threshold
        if threshold is None:
            for delivery in deliveries:
                yield _RenderedEmbed((delivery,), route.render(delivery))
            return

        groups: dict[str, list[Delivery]] = {}
        for delivery in deliveries:
            groups.setdefault(delivery.change.locale, []).append(delivery)
        merged: set[str] = set()
        for delivery in deliveries:
            group = groups[delivery.change.locale]
            if len(group) <= threshold:
                yield _RenderedEmbed((delivery,), route.render(delivery))
                continue
            if delivery.change.locale in merged:
                continue
            merged.add(delivery.change.locale)
            offset = 0
            for page in route.render_merge(group):
                end = offset + page.count
                yield _RenderedEmbed(tuple(group[offset:end]), page.embed)
                offset = end
            if offset != len(group):
                raise RuntimeError("Merged message did not consume every delivery")


def _duration(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _locales(webhook: WebhookConfig) -> str:
    if "*" in webhook.locales:
        return "`*`"
    return " · ".join(
        f"`{locale}`"
        for locale in sorted(
            webhook.locales,
            key=lambda locale: (locale != SOURCE_LOCALE, locale.casefold()),
        )
    )


def _actions(webhook: WebhookConfig) -> str:
    return " · ".join(
        f"`{action}`"
        for action in ("added", "modified", "deleted")
        if action in webhook.actions
    )


def _delivery(webhook: WebhookConfig) -> str:
    policy = webhook.delivery
    details = [
        (
            "No inactivity delay"
            if policy.quiet_seconds == 0
            else f"After `{_duration(policy.quiet_seconds)}` inactivity"
        ),
        f"`{_duration(policy.max_wait_seconds)}` maximum",
        ("Latest change per locale and key" if policy.fold_changes else "Every change"),
    ]
    if webhook.merge_threshold is None:
        details.append("Individual messages")
    elif webhook.merge_threshold == 0:
        details.append("Merge every locale batch")
    else:
        details.append(f"Merge locale batches above `{webhook.merge_threshold}`")
    return " · ".join(details)


def _context(monitor: MonitorConfig, webhook: WebhookConfig) -> str:
    return f"Monitor: {monitor.id} · Project: {monitor.project} · Route: {webhook.id}"


def baseline_embed(
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    changes: int,
    source_texts: int,
) -> Embed:
    """Render the baseline notification."""

    embed = Embed(
        title="Baseline Created",
        description=(
            "Monitoring starts from this point; existing history will not be replayed."
        ),
        color=_BASELINE_COLOR,
    )
    embed.add_field(
        name="RECENT HISTORY",
        value=(
            f"{changes:,} changes recorded\n\n"
            f"**SUBSCRIBED LOCALES**\n{_locales(webhook)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="ENGLISH SNAPSHOT",
        value=(f"{source_texts:,} keys\n\n**WATCHED ACTIONS**\n{_actions(webhook)}"),
        inline=True,
    )
    embed.add_field(name="DELIVERY", value=_delivery(webhook), inline=False)
    embed.set_footer(text=_context(monitor, webhook))
    return embed


def status_embed(
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    summary: StateSummary,
) -> Embed:
    """Render the persisted monitor status."""

    deliveries = (
        f"{summary.pending_deliveries:,} "
        f"{'delivery' if summary.pending_deliveries == 1 else 'deliveries'}"
    )
    routes = (
        f"{summary.pending_routes:,} "
        f"{'route' if summary.pending_routes == 1 else 'routes'}"
    )
    embed = Embed(
        title="Watcher Status",
        description="Current persisted monitoring state and route configuration.",
        color=_STATUS_COLOR,
    )
    embed.add_field(
        name="MONITORING STATE",
        value=(
            f"{'Ready' if summary.initialized else 'Baseline not created'}\n\n"
            f"**SUBSCRIBED LOCALES**\n{_locales(webhook)}\n\n"
            f"**DELIVERY RECEIPTS**\n{summary.delivery_receipts:,} recorded"
        ),
        inline=True,
    )
    embed.add_field(
        name="ENGLISH SNAPSHOT",
        value=(
            f"{summary.source_texts:,} keys\n\n"
            f"**WATCHED ACTIONS**\n{_actions(webhook)}\n\n"
            f"**PENDING QUEUE**\n{deliveries} across {routes}"
        ),
        inline=True,
    )
    embed.add_field(name="DELIVERY", value=_delivery(webhook), inline=False)
    embed.set_footer(text=_context(monitor, webhook))
    return embed


def send_status(
    client: DiscordSender,
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    summary: StateSummary,
) -> None:
    """Send one status notification."""

    client.send(
        webhook.id,
        embeds=(status_embed(monitor, webhook, summary),),
        username=webhook.username,
        avatar_url=webhook.avatar_url,
    )
