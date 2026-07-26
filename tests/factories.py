"""Shared object factories for unit tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from noctalia_i18n_watcher.config import (
    Config,
    HTTPSettings,
    MessageSelection,
    MonitorConfig,
    Settings,
    SourceSettings,
    Webhook,
)
from noctalia_i18n_watcher.discord import DiscordOptions, DiscordRoute
from noctalia_i18n_watcher.messages import Embed, MergePage, MessageValues
from noctalia_i18n_watcher.models import (
    ACTIONS,
    Action,
    Delivery,
    DeliveryPolicy,
    MergePolicy,
)

from .fixtures import (
    FIXTURE_SOURCE,
    RECORDED_SOURCE,
    fixture_change,
    recorded_change,
)

_HTTP = HTTPSettings(proxy_url="", request_timeout=30.0)
_MESSAGES = MessageSelection(source="english", target="english", merge="english")


class _Message:
    def render(self, action: Action, values: Mapping[str, object]) -> Embed:
        return {"description": f"{values['locale']}:{values['key']}:{action}"}


class _MergeMessage:
    def render(self, values: Sequence[MessageValues], /) -> tuple[MergePage, ...]:
        description = "\n".join(value["key"] for value in values)
        return (MergePage(len(values), {"description": description}),)


def merge_policy(**overrides: Any) -> MergePolicy:
    base = MergePolicy(enabled=True, threshold=5)
    return replace(base, **overrides)


def delivery_policy(**overrides: Any) -> DeliveryPolicy:
    base = DeliveryPolicy(
        quiet_seconds=240,
        max_wait_seconds=900,
        latest_only=True,
        merge=merge_policy(),
    )
    return replace(base, **overrides)


def delivery(**overrides: Any) -> Delivery:
    changed = fixture_change(**overrides)
    return Delivery.from_change(changed, {changed.key: FIXTURE_SOURCE})


def recorded_delivery(**overrides: Any) -> Delivery:
    changed = recorded_change(**overrides)
    return Delivery.from_change(changed, {changed.key: RECORDED_SOURCE})


def discord_route(**overrides: Any) -> DiscordRoute:
    selected = webhook_config()
    base = DiscordRoute(
        id=selected.id,
        monitor_id="noctalia",
        project="noctalia",
        url=selected.url,
        username=selected.username,
        avatar_url=selected.avatar_url,
        locales=selected.locales,
        actions=selected.actions,
        source_message=_Message(),
        target_message=_Message(),
        merge_message=_MergeMessage(),
        delivery=selected.delivery,
        notify_baseline=selected.notify_baseline,
    )
    return replace(base, **overrides)


def webhook_config(**overrides: Any) -> Webhook:
    base = Webhook(
        id="default",
        url="https://discord.com/api/webhooks/123/token",
        username="",
        avatar_url="",
        locales=frozenset({"en", "zh-Hans"}),
        actions=frozenset(ACTIONS),
        messages=_MESSAGES,
        delivery=delivery_policy(),
        notify_baseline=True,
    )
    return replace(base, **overrides)


def monitor_config(*webhooks: Webhook, **overrides: Any) -> MonitorConfig:
    base = MonitorConfig(
        id="noctalia",
        source=SourceSettings(
            project="noctalia",
            adapter="web",
            options={"max_pages": 20},
        ),
        webhooks=webhooks or (webhook_config(),),
    )
    return replace(base, **overrides)


def settings(**overrides: Any) -> Settings:
    base = Settings(http=_HTTP, retention_days=180, log_level="INFO")
    return replace(base, **overrides)


def discord_options(**overrides: Any) -> DiscordOptions:
    app_settings = settings()
    base = DiscordOptions(
        proxy_url=app_settings.http.proxy_url,
        request_timeout=app_settings.http.request_timeout,
    )
    return replace(base, **overrides)


def app_config(*monitors: MonitorConfig, **setting_overrides: object) -> Config:
    return Config(
        settings=settings(**setting_overrides),
        monitors=monitors or (monitor_config(),),
    )
