"""Runtime configuration tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from noctalia_i18n_core import JsonValue

from noctalia_i18n_watcher.config import load_config


def _payload() -> dict[str, JsonValue]:
    root = Path(__file__).parents[1]
    value: JsonValue = json.loads(
        (root / "config" / "config.json").read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise TypeError("Test configuration must be an object")
    return value


def _load(payload: dict[str, JsonValue]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        load_config(path)


def _monitor(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    monitors = payload["monitors"]
    if not isinstance(monitors, list) or not monitors:
        raise TypeError("Test configuration must contain monitors")
    monitor = monitors[0]
    if not isinstance(monitor, dict):
        raise TypeError("Test monitor must be an object")
    return monitor


def _webhook(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    webhooks = _monitor(payload)["webhooks"]
    if not isinstance(webhooks, list) or not webhooks:
        raise TypeError("Test monitor must contain webhooks")
    webhook = webhooks[0]
    if not isinstance(webhook, dict):
        raise TypeError("Test webhook must be an object")
    return webhook


class ConfigTests(unittest.TestCase):
    def test_repository_configuration_loads_delivery_policy(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config" / "config.json")

        webhook = config.monitors[0].webhooks[0]
        self.assertEqual(webhook.delivery.merge_threshold, 5)
        self.assertTrue(webhook.uses_source_message)
        self.assertTrue(webhook.uses_target_message)
        self.assertTrue(webhook.uses_merge_message)

    def test_null_threshold_does_not_select_a_merge_template(self) -> None:
        payload = _payload()
        delivery = _webhook(payload)["delivery"]
        if not isinstance(delivery, dict):
            self.fail("Test delivery must be an object")
        delivery["merge_threshold"] = None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            webhook = load_config(path).monitors[0].webhooks[0]

        self.assertIsNone(webhook.delivery.merge_threshold)
        self.assertFalse(webhook.uses_merge_message)

    def test_locale_identifiers_are_opaque(self) -> None:
        payload = _payload()
        _webhook(payload)["locales"] = ["custom_locale"]
        _load(payload)

    def test_rejects_invalid_configuration_boundaries(self) -> None:
        cases: tuple[tuple[str, JsonValue], ...] = (
            ("actions", ["renamed"]),
            ("locales", ["*", "en"]),
            ("url", "https://example.com/api/webhooks/id/token"),
        )
        for field, value in cases:
            payload = _payload()
            _webhook(payload)[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                _load(payload)

    def test_rejects_unknown_fields(self) -> None:
        payload = _payload()
        _monitor(payload)["unexpected"] = True

        with self.assertRaisesRegex(ValueError, "unexpected"):
            _load(payload)

    def test_reports_missing_and_invalid_fields_together(self) -> None:
        payload = _payload()
        settings = payload["settings"]
        if not isinstance(settings, dict):
            self.fail("Test settings must be an object")
        del settings["retention_days"]
        settings["retention"] = 180

        with self.assertRaises(ValueError) as captured:
            _load(payload)

        message = str(captured.exception)
        self.assertIn("missing required fields: ['retention_days']", message)
        self.assertIn("fields invalid in this scope: ['retention']", message)

    def test_rejects_an_invalid_project(self) -> None:
        payload = _payload()
        _monitor(payload)["project"] = "Invalid Project"

        with self.assertRaisesRegex(ValueError, "lowercase project identifier"):
            _load(payload)
