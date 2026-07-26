"""Source adapter selection tests."""

from __future__ import annotations

import sys
import unittest
from types import ModuleType
from unittest.mock import patch

from noctalia_i18n_watcher.models import (
    JsonValue,
    PollResult,
    WatcherError,
)
from noctalia_i18n_watcher.sources import build_source

from .contracts import PollScenario, assert_poll_contract
from .fixtures import FIXTURE_SOURCE, fixture_change


def _module(name: str, factory: object) -> ModuleType:
    module = ModuleType(name)
    setattr(module, "create", factory)
    return module


class _FixtureSource:
    def __init__(self) -> None:
        changed = fixture_change(id="fixture-change")
        source_texts = {changed.key: FIXTURE_SOURCE}
        self._results = iter(
            (
                PollResult((), {"fixture": 1}, 1, source_texts),
                PollResult((changed,), {"fixture": 2}, 1, source_texts),
                PollResult((), {"fixture": 2}, 0),
            )
        )
        self.closed = False

    def poll(self, _cursor: JsonValue | None, /) -> PollResult:
        return next(self._results)

    def close(self) -> None:
        self.closed = True


class SourceSelectionTests(unittest.TestCase):
    def test_adapter_factory_receives_project_options_and_shared_http_settings(
        self,
    ) -> None:
        module_name = "noctalia_i18n_watcher.sources.fixture_adapter"
        options = {"fixture_option": "value"}
        adapter = _FixtureSource()
        received: list[tuple[object, str, float, str]] = []

        def create(
            values: object,
            *,
            project: str,
            timeout: float,
            proxy: str,
        ) -> object:
            received.append((values, project, timeout, proxy))
            return adapter

        with patch.dict(sys.modules, {module_name: _module(module_name, create)}):
            source = build_source(
                "fixture_adapter",
                options,
                project="community-plugins",
                timeout=30,
                proxy="",
            )

        self.assertIs(source, adapter)
        self.assertEqual(received, [(options, "community-plugins", 30, "")])
        assert_poll_contract(
            self,
            source,
            PollScenario(
                source_texts={fixture_change().key: FIXTURE_SOURCE},
                changes=(fixture_change(id="fixture-change"),),
                baseline_scanned=1,
                changed_scanned=1,
                unchanged_scanned=0,
            ),
        )
        self.assertTrue(adapter.closed)

    def test_unregistered_adapter_fails_before_monitoring(self) -> None:
        with self.assertRaisesRegex(WatcherError, "Unsupported source"):
            build_source(
                "missing_for_test",
                {},
                project="noctalia",
                timeout=30,
                proxy="",
            )

    def test_adapter_must_return_the_source_contract(self) -> None:
        module_name = "noctalia_i18n_watcher.sources.invalid"
        with (
            patch.dict(
                sys.modules,
                {
                    module_name: _module(
                        module_name,
                        lambda _options, **_network: object(),
                    )
                },
            ),
            self.assertRaisesRegex(WatcherError, "implement Source"),
        ):
            build_source(
                "invalid",
                {},
                project="noctalia",
                timeout=30,
                proxy="",
            )
