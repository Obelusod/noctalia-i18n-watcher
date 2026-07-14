"""Shared object factories for unit tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from noctalia_i18n_watcher.config import (
    Config,
    MonitorConfig,
    Settings,
    Webhook,
    load_config,
)
from noctalia_i18n_watcher.discord import DiscordOptions, DiscordRoute
from noctalia_i18n_watcher.messages import load_merge, load_message
from noctalia_i18n_watcher.models import Delivery, DeliveryPolicy, MergePolicy

from .fixtures import (
    CONFIG_PATH,
    FIXTURE_SOURCE,
    MERGE_MESSAGE_DIR,
    RECORDED_SOURCE,
    SOURCE_MESSAGE_DIR,
    TARGET_MESSAGE_DIR,
    fixture_change,
    recorded_change,
)

_CONFIG = load_config(CONFIG_PATH)
_MONITOR = _CONFIG.monitors[0]


def merge_policy(**overrides: Any) -> MergePolicy:
    base = _MONITOR.webhooks[0].delivery.merge
    return replace(base, **overrides)


def delivery_policy(**overrides: Any) -> DeliveryPolicy:
    base = _MONITOR.webhooks[0].delivery
    return replace(base, **overrides)


def delivery(**overrides: Any) -> Delivery:
    changed = fixture_change(**overrides)
    return Delivery.from_change(changed, {changed.key: FIXTURE_SOURCE})


def recorded_delivery(**overrides: Any) -> Delivery:
    changed = recorded_change(**overrides)
    return Delivery.from_change(changed, {changed.key: RECORDED_SOURCE})


def webhook(**overrides: Any) -> DiscordRoute:
    selected = webhook_config()
    base = DiscordRoute(
        id=selected.id,
        monitor_id=_MONITOR.id,
        project=_MONITOR.source.project,
        url=selected.url,
        username=selected.username,
        avatar_url=selected.avatar_url,
        locales=selected.locales,
        actions=selected.actions,
        source_message=load_message(selected.messages.source, SOURCE_MESSAGE_DIR),
        target_message=load_message(selected.messages.target, TARGET_MESSAGE_DIR),
        merge_message=load_merge(selected.messages.merge, MERGE_MESSAGE_DIR),
        delivery=selected.delivery,
        notify_baseline=selected.notify_baseline,
    )
    return replace(base, **overrides)


def webhook_config(**overrides: Any) -> Webhook:
    base = _MONITOR.webhooks[0]
    return replace(base, **overrides)


def monitor_config(*webhooks: Webhook, **overrides: Any) -> MonitorConfig:
    base = replace(_MONITOR, webhooks=webhooks or _MONITOR.webhooks)
    return replace(base, **overrides)


def settings(**overrides: Any) -> Settings:
    base = _CONFIG.settings
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
        monitors=monitors or (_MONITOR,),
    )
