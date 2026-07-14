"""Runtime configuration tests."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from noctalia_i18n_watcher.config import load_config
from noctalia_i18n_watcher.models import ValidationError

from .fixtures import CONFIG_PATH


def _valid_payload() -> dict[str, Any]:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Repository configuration must be a JSON object")
    return data


def _monitor(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["monitors"][0]


def _webhook(payload: dict[str, Any]) -> dict[str, Any]:
    return _monitor(payload)["webhooks"][0]


class ConfigTests(unittest.TestCase):
    def _load(self, payload: object):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path)

    def test_source_options_are_owned_by_the_selected_adapter(self) -> None:
        payload = _valid_payload()
        _monitor(payload)["source"] = {
            "project": "community-plugins",
            "adapter": "api",
            "options": {
                "endpoint": "https://example.invalid/changes",
                "headers": {"Authorization": "Bearer token"},
            },
        }

        source = self._load(payload).monitors[0].source

        self.assertEqual(source.project, "community-plugins")
        self.assertEqual(source.adapter, "api")
        self.assertEqual(
            source.options,
            {
                "endpoint": "https://example.invalid/changes",
                "headers": {"Authorization": "Bearer token"},
            },
        )

    def test_source_project_is_a_valid_identifier(self) -> None:
        payload = _valid_payload()
        _monitor(payload)["source"]["project"] = "Community Plugins"

        with self.assertRaisesRegex(ValidationError, r"monitors\[0\]\.source\.project"):
            self._load(payload)

    def test_repository_configuration_is_explicit(self) -> None:
        config = load_config(CONFIG_PATH)
        settings = config.settings
        (monitor,) = config.monitors
        (webhook,) = monitor.webhooks

        self.assertEqual(settings.http.request_timeout, 30.0)
        self.assertEqual(settings.retention_days, 180)
        self.assertEqual(monitor.id, "noctalia")
        self.assertEqual(monitor.source.project, "noctalia")
        self.assertEqual(monitor.source.options, {"max_pages": 20})
        self.assertEqual(webhook.id, "default")
        self.assertEqual(webhook.locales, frozenset({"en", "zh-Hans"}))
        self.assertEqual(
            (
                webhook.delivery.quiet_seconds,
                webhook.delivery.max_wait_seconds,
                webhook.delivery.latest_only,
                webhook.delivery.merge.enabled,
                webhook.delivery.merge.threshold,
            ),
            (240, 900, True, True, 5),
        )
        self.assertEqual(
            (
                webhook.messages.source,
                webhook.messages.target,
                webhook.messages.merge,
            ),
            ("english", "english", "english"),
        )

    def test_required_fields_are_explicit(self) -> None:
        paths = (
            ("settings",),
            ("monitors",),
            ("settings", "http"),
            ("settings", "retention_days"),
            ("settings", "log_level"),
            ("settings", "http", "proxy_url"),
            ("settings", "http", "request_timeout"),
            ("monitors", 0, "id"),
            ("monitors", 0, "source"),
            ("monitors", 0, "webhooks"),
            ("monitors", 0, "source", "project"),
            ("monitors", 0, "source", "adapter"),
            ("monitors", 0, "source", "options"),
            ("monitors", 0, "webhooks", 0, "id"),
            ("monitors", 0, "webhooks", 0, "url"),
            ("monitors", 0, "webhooks", 0, "username"),
            ("monitors", 0, "webhooks", 0, "avatar_url"),
            ("monitors", 0, "webhooks", 0, "locales"),
            ("monitors", 0, "webhooks", 0, "actions"),
            ("monitors", 0, "webhooks", 0, "messages"),
            ("monitors", 0, "webhooks", 0, "delivery"),
            ("monitors", 0, "webhooks", 0, "notify_baseline"),
            ("monitors", 0, "webhooks", 0, "messages", "source"),
            ("monitors", 0, "webhooks", 0, "messages", "target"),
            ("monitors", 0, "webhooks", 0, "messages", "merge"),
            ("monitors", 0, "webhooks", 0, "delivery", "quiet_seconds"),
            ("monitors", 0, "webhooks", 0, "delivery", "max_wait_seconds"),
            ("monitors", 0, "webhooks", 0, "delivery", "latest_only"),
            ("monitors", 0, "webhooks", 0, "delivery", "merge"),
            ("monitors", 0, "webhooks", 0, "delivery", "merge", "enabled"),
            ("monitors", 0, "webhooks", 0, "delivery", "merge", "threshold"),
        )
        for path in paths:
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            del target[path[-1]]
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValidationError, str(path[-1])),
            ):
                self._load(payload)

    def test_message_usage_follows_route_scope(self) -> None:
        cases = (
            (
                ["en"],
                {
                    "source": "english",
                    "target": "../inactive-target",
                    "merge": "../inactive-merge",
                },
                False,
                (True, False, False),
            ),
            (
                ["de"],
                {
                    "source": "../inactive-source",
                    "target": "english",
                    "merge": "../inactive-merge",
                },
                False,
                (False, True, False),
            ),
            (
                ["en", "de"],
                {
                    "source": "english",
                    "target": "english",
                    "merge": "english",
                },
                True,
                (True, True, True),
            ),
            (
                ["*"],
                {
                    "source": "english",
                    "target": "english",
                    "merge": "english",
                },
                True,
                (True, True, True),
            ),
        )
        for locales, messages, merge_enabled, expected in cases:
            with self.subTest(locales=locales, merge_enabled=merge_enabled):
                payload = _valid_payload()
                webhook = _webhook(payload)
                webhook["locales"] = locales
                webhook["messages"] = messages
                webhook["delivery"]["merge"]["enabled"] = merge_enabled

                selected = self._load(payload).monitors[0].webhooks[0]

                self.assertEqual(
                    (
                        selected.uses_source_message,
                        selected.uses_target_message,
                        selected.uses_merge_message,
                    ),
                    expected,
                )

    def test_every_message_selection_must_be_a_non_empty_string(self) -> None:
        for field in ("source", "target", "merge"):
            for value in (None, ""):
                payload = _valid_payload()
                webhook = _webhook(payload)
                webhook["messages"][field] = value
                webhook["delivery"]["merge"]["enabled"] = False
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValidationError, field),
                ):
                    self._load(payload)

    def test_webhook_ids_must_be_unique(self) -> None:
        payload = _valid_payload()
        monitor = _monitor(payload)
        monitor["webhooks"].append(dict(monitor["webhooks"][0]))

        with self.assertRaisesRegex(ValidationError, "IDs must be unique"):
            self._load(payload)

    def test_monitor_ids_must_be_unique(self) -> None:
        payload = _valid_payload()
        payload["monitors"].append(dict(payload["monitors"][0]))

        with self.assertRaisesRegex(ValidationError, "IDs must be unique"):
            self._load(payload)

    def test_monitor_and_webhook_lists_must_not_be_empty(self) -> None:
        for path in (("monitors",), ("monitors", 0, "webhooks")):
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = []
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValidationError, "non-empty"),
            ):
                self._load(payload)

    def test_webhook_ids_are_scoped_to_their_monitor(self) -> None:
        payload = _valid_payload()
        second = json.loads(json.dumps(payload["monitors"][0]))
        second["id"] = "community-plugins"
        second["source"]["project"] = "community-plugins"
        payload["monitors"].append(second)

        config = self._load(payload)

        self.assertEqual(
            [monitor.webhooks[0].id for monitor in config.monitors],
            ["default", "default"],
        )

    def test_locale_and_action_arrays_are_strict(self) -> None:
        cases = (
            ("locales", [], "non-empty"),
            ("locales", ["*", "de"], "cannot combine"),
            ("locales", ["invalid_locale"], "invalid locale"),
            ("actions", [], "non-empty"),
            ("actions", ["modified", "modified"], "duplicate"),
            ("actions", ["changed"], "unsupported"),
        )
        for field, value, error in cases:
            payload = _valid_payload()
            _webhook(payload)[field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(ValidationError, error),
            ):
                self._load(payload)

    def test_unknown_fields_fail_in_every_scope(self) -> None:
        paths = (
            (),
            ("settings",),
            ("monitors", 0),
            ("monitors", 0, "source"),
            ("monitors", 0, "webhooks", 0),
            ("monitors", 0, "webhooks", 0, "messages"),
            ("monitors", 0, "webhooks", 0, "delivery"),
            ("monitors", 0, "webhooks", 0, "delivery", "merge"),
        )
        for path in paths:
            payload = _valid_payload()
            target: Any = payload
            for key in path:
                target = target[key]
            target["unexpected"] = True
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValidationError, "unexpected"),
            ):
                self._load(payload)

    def test_urls_are_semantically_validated(self) -> None:
        cases = (
            (("settings", "http", "proxy_url"), "socks5://localhost"),
            (
                ("monitors", 0, "webhooks", 0, "url"),
                "https://example.com/api/webhooks/1/token",
            ),
            (("monitors", 0, "webhooks", 0, "avatar_url"), "relative.png"),
        )
        for path, value in cases:
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self._load(payload)

    def test_numeric_bounds_reject_invalid_values(self) -> None:
        cases = (
            (("settings", "http", "request_timeout"), math.inf),
            (("monitors", 0, "webhooks", 0, "delivery", "quiet_seconds"), 86401),
            (
                ("monitors", 0, "webhooks", 0, "delivery", "max_wait_seconds"),
                604801,
            ),
            (
                ("monitors", 0, "webhooks", 0, "delivery", "merge", "threshold"),
                -1,
            ),
            (("settings", "retention_days"), 0),
        )
        for path, value in cases:
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self._load(payload)

        payload = _valid_payload()
        _webhook(payload)["delivery"]["max_wait_seconds"] = 239
        with self.assertRaisesRegex(ValidationError, "max_wait_seconds"):
            self._load(payload)

    def test_delivery_booleans_are_strict(self) -> None:
        cases = (
            (("monitors", 0, "webhooks", 0, "delivery", "latest_only"), 1),
            (
                ("monitors", 0, "webhooks", 0, "delivery", "merge", "enabled"),
                1,
            ),
        )
        for path, value in cases:
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValidationError, "boolean"),
            ):
                self._load(payload)

    def test_webhook_identity_and_metadata_are_validated(self) -> None:
        cases = (
            (("monitors", 0, "id"), "invalid/id"),
            (("monitors", 0, "webhooks", 0, "id"), "invalid/id"),
            (("monitors", 0, "webhooks", 0, "username"), "x" * 81),
        )
        for path, value in cases:
            payload = _valid_payload()
            target: Any = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self._load(payload)


if __name__ == "__main__":
    unittest.main()
