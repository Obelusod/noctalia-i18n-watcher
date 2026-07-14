"""Discord rendering and webhook delivery."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Required, TypedDict

import requests

from .messages import (
    Embed,
    EmbedField,
    EmbedFooter,
    MergePage,
    MessageValues,
    embed_size,
)
from .models import (
    ACTIONS,
    SOURCE_LOCALE,
    Action,
    Change,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    StateSummary,
    WatcherError,
    validate_json,
)

_LOGGER = logging.getLogger(__name__)
_MAX_EMBEDS = 10
_MAX_BATCH_CHARS = 6000
_RETRIES = 5
_BACKOFF_MAX = 20.0
_BASELINE_COLOR = 0x5865F2
_STATUS_COLOR = 0x57F287


class Message(Protocol):
    """Message renderer used by a Discord route."""

    def render(self, action: Action, values: Mapping[str, object]) -> Embed: ...


class MergeMessage(Protocol):
    """Renderer for one locale batch."""

    def render(self, values: Sequence[MessageValues], /) -> Sequence[MergePage]: ...


@dataclass(frozen=True, slots=True)
class DiscordOptions:
    """Discord networking policy."""

    proxy_url: str
    request_timeout: float


@dataclass(frozen=True, slots=True)
class DiscordRoute:
    """One webhook subscription and its message renderers."""

    id: str
    monitor_id: str
    project: str
    url: str
    username: str
    avatar_url: str
    locales: frozenset[str]
    actions: frozenset[Action]
    source_message: Message | None
    target_message: Message | None
    merge_message: MergeMessage | None
    delivery: DeliveryPolicy
    notify_baseline: bool

    def accepts_locale(self, locale: str) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale) and change.action in self.actions

    def render(self, delivery: Delivery) -> Embed:
        change = delivery.change
        message = self.source_message if change.is_source else self.target_message
        if message is None:
            raise WatcherError(f"Discord route {self.id!r} has no matching message")
        return message.render(change.action, self._values(delivery))

    def render_merge(self, deliveries: Sequence[Delivery]) -> Sequence[MergePage]:
        if not deliveries:
            raise ValueError("A Discord merge requires at least one delivery")
        locale = deliveries[0].change.locale
        if any(item.change.locale != locale for item in deliveries):
            raise ValueError("A Discord merge cannot mix locales")
        if self.merge_message is None:
            raise WatcherError(f"Discord route {self.id!r} has no merge message")
        return self.merge_message.render(
            tuple(self._values(item) for item in deliveries),
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


def _duration(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _route_locales(route: DiscordRoute) -> str:
    if "*" in route.locales:
        return "`*` (all)"
    return " · ".join(
        f"`{locale}` (source)" if locale == SOURCE_LOCALE else f"`{locale}`"
        for locale in sorted(
            route.locales,
            key=lambda locale: (locale != SOURCE_LOCALE, locale.casefold()),
        )
    )


def _route_delivery(route: DiscordRoute) -> str:
    policy = route.delivery
    delivery = [
        (
            "No inactivity delay"
            if policy.quiet_seconds == 0
            else f"After `{_duration(policy.quiet_seconds)}` inactivity"
        ),
        f"`{_duration(policy.max_wait_seconds)}` maximum",
        "Latest change per locale and key" if policy.latest_only else "Every change",
    ]
    if policy.merge.enabled and route.merge_message is not None:
        threshold = policy.merge.threshold
        delivery.append(
            "Merge every locale batch"
            if threshold == 0
            else f"Merge locale batches above `{threshold}`"
        )
    else:
        delivery.append("Individual messages")
    return " · ".join(delivery)


def _route_actions(route: DiscordRoute) -> str:
    return " · ".join(f"`{action}`" for action in ACTIONS if action in route.actions)


def _route_footer(route: DiscordRoute) -> EmbedFooter:
    return {
        "text": (
            f"Monitor · {route.monitor_id} · Project · {route.project} · "
            f"Route · {route.id}"
        )
    }


def _baseline_embed(
    route: DiscordRoute,
    changes: int,
    source_texts: int,
) -> Embed:
    fields: list[EmbedField] = [
        {
            "name": "RECENT HISTORY",
            "value": (
                f"{changes:,} changes recorded\n\n"
                f"**SUBSCRIBED LOCALES**\n{_route_locales(route)}"
            ),
            "inline": True,
        },
        {
            "name": "ENGLISH SNAPSHOT",
            "value": (
                f"{source_texts:,} keys\n\n**WATCHED ACTIONS**\n{_route_actions(route)}"
            ),
            "inline": True,
        },
        {
            "name": "DELIVERY",
            "value": _route_delivery(route),
        },
    ]
    return {
        "title": "Baseline Created",
        "description": (
            "Monitoring starts from this point; existing history will not be replayed."
        ),
        "color": _BASELINE_COLOR,
        "fields": fields,
        "footer": _route_footer(route),
    }


def _status_embed(route: DiscordRoute, summary: StateSummary) -> Embed:
    deliveries = (
        f"{summary.pending_deliveries:,} "
        f"{'delivery' if summary.pending_deliveries == 1 else 'deliveries'}"
    )
    routes = (
        f"{summary.pending_routes:,} "
        f"{'route' if summary.pending_routes == 1 else 'routes'}"
    )
    fields: list[EmbedField] = [
        {
            "name": "MONITORING STATE",
            "value": (
                f"{'Ready' if summary.initialized else 'Baseline not created'}\n\n"
                f"**SUBSCRIBED LOCALES**\n{_route_locales(route)}\n\n"
                f"**DELIVERY RECEIPTS**\n{summary.deliveries:,} recorded"
            ),
            "inline": True,
        },
        {
            "name": "ENGLISH SNAPSHOT",
            "value": (
                f"{summary.source_texts:,} keys\n\n"
                f"**WATCHED ACTIONS**\n{_route_actions(route)}\n\n"
                f"**PENDING QUEUE**\n{deliveries} across {routes}"
            ),
            "inline": True,
        },
        {
            "name": "DELIVERY",
            "value": _route_delivery(route),
        },
    ]
    return {
        "title": "Watcher Status",
        "description": "Current persisted monitoring state and route configuration.",
        "color": _STATUS_COLOR,
        "fields": fields,
        "footer": _route_footer(route),
    }


@dataclass(frozen=True, slots=True)
class _Rendered:
    deliveries: tuple[Delivery, ...]
    embed: Embed


class _AllowedMentions(TypedDict):
    parse: list[str]


class _WebhookPayload(TypedDict, total=False):
    allowed_mentions: Required[_AllowedMentions]
    username: str
    avatar_url: str
    embeds: list[Embed]


def _payload(route: DiscordRoute, embeds: list[Embed]) -> dict[str, JsonValue]:
    payload = _WebhookPayload(allowed_mentions={"parse": []})
    if route.username:
        payload["username"] = route.username
    if route.avatar_url:
        payload["avatar_url"] = route.avatar_url
    payload["embeds"] = embeds
    value = validate_json(payload, "Discord webhook payload")
    if not isinstance(value, dict):
        raise TypeError("Discord webhook payload must be a JSON object")
    return value


def _batches(
    items: Iterable[_Rendered],
) -> Iterator[tuple[_Rendered, ...]]:
    batch: list[_Rendered] = []
    characters = 0
    for item in items:
        size = embed_size(item.embed)
        if size > _MAX_BATCH_CHARS:
            raise WatcherError(
                f"Discord embed contains {size} characters, exceeding the "
                f"{_MAX_BATCH_CHARS}-character limit"
            )
        if batch and (
            len(batch) >= _MAX_EMBEDS or characters + size > _MAX_BATCH_CHARS
        ):
            yield tuple(batch)
            batch = []
            characters = 0
        batch.append(item)
        characters += size
    if batch:
        yield tuple(batch)


def _retry_after(response: requests.Response, maximum: float) -> float:
    try:
        body: object = response.json()
        data = body if isinstance(body, dict) else {}
        raw_delay = data.get("retry_after", 1)
        delay = float(raw_delay) if isinstance(raw_delay, (int, float, str)) else 1
    except (TypeError, ValueError):
        delay = 1
    if not math.isfinite(delay):
        delay = 1
    return min(max(delay, 0.25), maximum)


class DiscordNotifier:
    """Render and deliver Discord webhook messages."""

    def __init__(
        self,
        routes: tuple[DiscordRoute, ...],
        options: DiscordOptions,
        *,
        session: requests.Session | None = None,
    ) -> None:
        route_ids = tuple(route.id for route in routes)
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("Discord route IDs must be unique")
        self._route_list: tuple[DiscordRoute, ...] = routes
        self._routes: dict[str, DiscordRoute] = {route.id: route for route in routes}
        self._options: DiscordOptions = options
        self._session: requests.Session = session or requests.Session()
        self._session.trust_env = False
        if options.proxy_url:
            self._session.proxies.update(
                {"http": options.proxy_url, "https": options.proxy_url}
            )

    @property
    def routes(self) -> tuple[DiscordRoute, ...]:
        return self._route_list

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None] | None = None,
    ) -> None:
        """Deliver changes, acknowledging each successful request when requested."""

        route = self._route(route_id)
        rendered = self._render(route, deliveries)
        for batch in _batches(rendered):
            self._post(
                route,
                _payload(route, [item.embed for item in batch]),
            )
            if acknowledge is not None:
                acknowledge(
                    tuple(delivery for item in batch for delivery in item.deliveries)
                )

    def render(
        self, route_id: str, deliveries: Sequence[Delivery]
    ) -> tuple[Embed, ...]:
        """Render deliveries exactly as this route would send them."""

        route = self._route(route_id)
        rendered = self._render(route, deliveries)
        return tuple(item.embed for item in rendered)

    def send_baseline(self, route_id: str, changes: int, source_texts: int) -> None:
        route = self._route(route_id)
        self._post(
            route,
            _payload(route, [_baseline_embed(route, changes, source_texts)]),
        )

    def send_status(self, route_id: str, summary: StateSummary) -> None:
        route = self._route(route_id)
        self._post(
            route,
            _payload(route, [_status_embed(route, summary)]),
        )

    def close(self) -> None:
        self._session.close()

    def _route(self, route_id: str) -> DiscordRoute:
        try:
            return self._routes[route_id]
        except KeyError:
            raise WatcherError(f"Unknown Discord webhook: {route_id!r}") from None

    def _render(
        self, route: DiscordRoute, deliveries: Sequence[Delivery]
    ) -> Iterator[_Rendered]:
        merge = route.delivery.merge
        if not merge.enabled or route.merge_message is None:
            for delivery in deliveries:
                yield _Rendered((delivery,), route.render(delivery))
            return

        # Preserve first-seen locale order while collecting interleaved changes.
        groups: dict[str, list[Delivery]] = {}
        for delivery in deliveries:
            groups.setdefault(delivery.change.locale, []).append(delivery)

        merged: set[str] = set()
        for delivery in deliveries:
            locale = delivery.change.locale
            group = groups[locale]
            if len(group) <= merge.threshold:
                yield _Rendered((delivery,), route.render(delivery))
                continue
            if locale in merged:
                continue
            merged.add(locale)
            offset = 0
            for page in route.render_merge(group):
                end = offset + page.count
                yield _Rendered(tuple(group[offset:end]), page.embed)
                offset = end
            if offset != len(group):
                raise RuntimeError("Merge renderer did not consume every delivery")

    def _post(self, route: DiscordRoute, payload: Mapping[str, JsonValue]) -> None:
        # Wait for Discord to complete the request before recording its deliveries.
        url = f"{route.url}{'&' if '?' in route.url else '?'}wait=true"
        for attempt in range(1, _RETRIES + 1):
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    timeout=self._options.request_timeout,
                )
            except requests.RequestException:
                if attempt == _RETRIES:
                    raise WatcherError(
                        f"Discord webhook {route.id!r} had a network failure"
                    ) from None
                _LOGGER.warning("Discord webhook %s failed; retrying", route.id)
                time.sleep(min(2**attempt, _BACKOFF_MAX))
                continue

            if response.status_code == 429:
                delay = _retry_after(response, _BACKOFF_MAX)
                if attempt == _RETRIES:
                    raise WatcherError(
                        f"Discord webhook {route.id!r} remained rate limited"
                    )
                _LOGGER.warning(
                    "Discord webhook %s was rate limited; retrying in %.2fs",
                    route.id,
                    delay,
                )
                time.sleep(delay)
                continue
            if 500 <= response.status_code < 600:
                if attempt == _RETRIES:
                    raise WatcherError(
                        f"Discord webhook {route.id!r} returned "
                        f"HTTP {response.status_code}"
                    )
                _LOGGER.warning(
                    "Discord webhook %s returned HTTP %s; retrying",
                    route.id,
                    response.status_code,
                )
                time.sleep(min(2**attempt, _BACKOFF_MAX))
                continue
            try:
                response.raise_for_status()
            except requests.RequestException:
                raise WatcherError(
                    f"Discord webhook {route.id!r} returned HTTP {response.status_code}"
                ) from None
            return
