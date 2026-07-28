"""Strict loading of runtime configuration."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard
from urllib.parse import urlparse

from noctalia_i18n_core import SOURCE_LOCALE, Action, DeliveryPolicy

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROJECT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
_WEBHOOK_HOSTS = frozenset({"discord.com", "canary.discord.com", "ptb.discord.com"})
_WEBHOOK_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/[^/]+/[^/]+$")
_USERNAME_MAX = 80


@dataclass(frozen=True, slots=True)
class Settings:
    """Process-wide settings."""

    proxy_url: str
    request_timeout: float
    retention_days: int
    log_level: str


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """One Discord endpoint and its change subscription."""

    id: str
    url: str
    username: str
    avatar_url: str
    locales: frozenset[str]
    actions: frozenset[Action]
    source_message: str
    target_message: str
    merge_message: str
    delivery: DeliveryPolicy
    notify_baseline: bool

    @property
    def uses_source_message(self) -> bool:
        """Whether this route subscribes to source-language changes."""

        return "*" in self.locales or SOURCE_LOCALE in self.locales

    @property
    def uses_target_message(self) -> bool:
        """Whether this route subscribes to target-language changes."""

        return "*" in self.locales or any(
            locale != SOURCE_LOCALE for locale in self.locales
        )

    @property
    def uses_merge_message(self) -> bool:
        """Whether this route has merged delivery enabled."""

        return self.delivery.merge_threshold is not None


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """One independently stateful source and its Discord routes."""

    id: str
    project: str
    webhooks: tuple[WebhookConfig, ...]


@dataclass(frozen=True, slots=True)
class Config:
    """Validated inputs used by the composition root."""

    settings: Settings
    monitors: tuple[MonitorConfig, ...]


def _is_object(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _object(value: object, label: str) -> dict[str, object]:
    if not _is_object(value):
        raise ValueError(f"{label} must be a JSON object")
    output: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} must use string keys")
        output[key] = item
    return output


def _array(value: object, label: str) -> list[object]:
    if not _is_array(value):
        raise ValueError(f"{label} must be an array")
    return value


def _exact_keys(
    value: dict[str, object],
    required: frozenset[str],
    label: str,
) -> None:
    problems: list[str] = []
    if missing := required - value.keys():
        problems.append(f"missing required fields: {sorted(missing)}")
    if unknown := value.keys() - required:
        problems.append(f"fields invalid in this scope: {sorted(unknown)}")
    if problems:
        raise ValueError(f"{label} is invalid: {'; '.join(problems)}")


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        expected = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{label} must be {expected}")
    return value.strip()


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        raise ValueError(f"{label} must be {'an integer' if integer else 'a number'}")
    try:
        finite = math.isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{label} must be finite")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    return int(_number(value, label, minimum=minimum, maximum=maximum, integer=True))


def _float(value: object, label: str, minimum: float, maximum: float) -> float:
    return float(_number(value, label, minimum=minimum, maximum=maximum))


def _proxy_url(value: object, label: str) -> str:
    url = _string(value, label, allow_empty=True)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is malformed") from exc
    if parsed.scheme.lower() not in _PROXY_SCHEMES or not hostname or port is None:
        raise ValueError(
            f"{label} must be an absolute HTTP(S) or SOCKS proxy URL with a port"
        )
    return url


def _webhook_url(value: object, label: str) -> str:
    url = _string(value, label)
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or hostname not in _WEBHOOK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not _WEBHOOK_PATH.fullmatch(parsed.path)
    ):
        raise ValueError(f"{label} must be a standard HTTPS Discord webhook URL")
    return url


def _avatar_url(value: object, label: str) -> str:
    url = _string(value, label, allow_empty=True)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{label} is malformed") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")
    return url


def _unique_strings(value: object, label: str) -> list[str]:
    items = _array(value, label)
    if not items:
        raise ValueError(f"{label} must be a non-empty string array")
    strings = [_string(item, label) for item in items]
    if len(strings) != len(set(strings)):
        raise ValueError(f"{label} must not contain duplicate values")
    return strings


def _name(value: object, label: str) -> str:
    name = _string(value, label)
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{label} must match {_NAME_PATTERN.pattern!r}")
    return name


def _action(value: str, label: str) -> Action:
    match value:
        case "added":
            return "added"
        case "modified":
            return "modified"
        case "deleted":
            return "deleted"
        case _:
            raise ValueError(f"{label} contains unsupported value: {value!r}")


def _delivery(value: object, label: str) -> DeliveryPolicy:
    raw = _object(value, label)
    _exact_keys(
        raw,
        frozenset(
            {
                "quiet_seconds",
                "max_wait_seconds",
                "fold_changes",
                "merge_threshold",
            }
        ),
        label,
    )
    threshold = raw["merge_threshold"]
    merge_threshold = (
        None
        if threshold is None
        else _integer(threshold, f"{label}.merge_threshold", 0, 100)
    )
    try:
        return DeliveryPolicy(
            quiet_seconds=_integer(
                raw["quiet_seconds"],
                f"{label}.quiet_seconds",
                0,
                86400,
            ),
            max_wait_seconds=_integer(
                raw["max_wait_seconds"],
                f"{label}.max_wait_seconds",
                0,
                604800,
            ),
            fold_changes=_boolean(
                raw["fold_changes"],
                f"{label}.fold_changes",
            ),
            merge_threshold=merge_threshold,
        )
    except ValueError as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc


def _settings(raw: dict[str, object]) -> Settings:
    _exact_keys(raw, frozenset({"http", "retention_days", "log_level"}), "settings")

    http = _object(raw["http"], "settings.http")
    _exact_keys(http, frozenset({"proxy_url", "request_timeout"}), "settings.http")
    log_level = _string(raw["log_level"], "settings.log_level").upper()
    if log_level not in _LOG_LEVELS:
        raise ValueError(f"settings.log_level is unknown: {log_level}")

    return Settings(
        proxy_url=_proxy_url(http["proxy_url"], "settings.http.proxy_url"),
        request_timeout=_float(
            http["request_timeout"],
            "settings.http.request_timeout",
            0.001,
            300,
        ),
        retention_days=_integer(
            raw["retention_days"],
            "settings.retention_days",
            1,
            3650,
        ),
        log_level=log_level,
    )


def _webhook(raw: dict[str, object], label: str) -> WebhookConfig:
    _exact_keys(
        raw,
        frozenset(
            {
                "id",
                "url",
                "locales",
                "actions",
                "messages",
                "delivery",
                "username",
                "avatar_url",
                "notify_baseline",
            }
        ),
        label,
    )

    route_id = _name(raw["id"], f"{label}.id")
    url = _webhook_url(raw["url"], f"{label}.url")
    username = _string(raw["username"], f"{label}.username", allow_empty=True)
    if len(username) > _USERNAME_MAX:
        raise ValueError(f"{label}.username must not exceed {_USERNAME_MAX} characters")
    avatar_url = _avatar_url(raw["avatar_url"], f"{label}.avatar_url")

    raw_locales = _unique_strings(raw["locales"], f"{label}.locales")
    if "*" in raw_locales and len(raw_locales) != 1:
        raise ValueError(
            f"{label}.locales cannot combine '*' with explicit locale identifiers"
        )

    action_label = f"{label}.actions"
    actions: frozenset[Action] = frozenset(
        _action(action, action_label)
        for action in _unique_strings(raw["actions"], action_label)
    )
    message_label = f"{label}.messages"
    raw_messages = _object(raw["messages"], message_label)
    _exact_keys(raw_messages, frozenset({"source", "target", "merge"}), message_label)
    source_message = _string(raw_messages["source"], f"{message_label}.source")
    target_message = _string(raw_messages["target"], f"{message_label}.target")
    merge_message = _string(raw_messages["merge"], f"{message_label}.merge")
    delivery = _delivery(raw["delivery"], f"{label}.delivery")
    notify_baseline = _boolean(raw["notify_baseline"], f"{label}.notify_baseline")

    return WebhookConfig(
        id=route_id,
        url=url,
        username=username,
        avatar_url=avatar_url,
        locales=frozenset(raw_locales),
        actions=actions,
        source_message=source_message,
        target_message=target_message,
        merge_message=merge_message,
        delivery=delivery,
        notify_baseline=notify_baseline,
    )


def _monitor(raw: dict[str, object], index: int) -> MonitorConfig:
    label = f"monitors[{index}]"
    _exact_keys(raw, frozenset({"id", "project", "webhooks"}), label)
    monitor_id = _name(raw["id"], f"{label}.id")
    project = _string(raw["project"], f"{label}.project")
    if not _PROJECT_PATTERN.fullmatch(project):
        raise ValueError(f"{label}.project must be a lowercase project identifier")

    raw_webhooks = _array(raw["webhooks"], f"{label}.webhooks")
    if not raw_webhooks:
        raise ValueError(f"{label}.webhooks must be a non-empty array")
    webhooks = tuple(
        _webhook(
            _object(item, f"{label}.webhooks[{webhook_index}]"),
            f"{label}.webhooks[{webhook_index}]",
        )
        for webhook_index, item in enumerate(raw_webhooks)
    )
    duplicates = sorted(
        route_id
        for route_id, count in Counter(webhook.id for webhook in webhooks).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"{label} webhook IDs must be unique: {duplicates}")

    return MonitorConfig(
        id=monitor_id,
        project=project,
        webhooks=webhooks,
    )


def load_config(path: Path) -> Config:
    """Load and validate one runtime configuration file."""

    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load configuration {path}: {exc}") from exc
    root = _object(raw, "configuration")
    _exact_keys(
        root,
        frozenset({"settings", "monitors"}),
        "configuration",
    )
    settings = _settings(_object(root["settings"], "settings"))

    raw_monitors = _array(root["monitors"], "monitors")
    if not raw_monitors:
        raise ValueError("monitors must be a non-empty array")
    monitors = tuple(
        _monitor(_object(item, f"monitors[{index}]"), index)
        for index, item in enumerate(raw_monitors)
    )
    duplicates = sorted(
        monitor_id
        for monitor_id, count in Counter(monitor.id for monitor in monitors).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"Monitor IDs must be unique: {duplicates}")

    return Config(
        settings=settings,
        monitors=monitors,
    )
