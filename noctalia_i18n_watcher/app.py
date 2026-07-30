"""Application composition and use cases."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import override

import requests
from noctalia_i18n_core import (
    Change,
    Monitor,
    MonitorResult,
    NoctaliaSource,
    ResetMode,
    SQLiteState,
    StateSummary,
)

from .config import Config, MonitorConfig, load_config
from .discord import (
    DiscordNotifier,
    DiscordRoute,
    DiscordWebhookClient,
    baseline_embed,
    send_status,
)
from .messages import load_merge, load_message

_LOGGER = logging.getLogger(__name__)
_CONFIG_DIR = Path("/etc/noctalia-i18n-watcher")
_STATE_DIR = Path("/var/lib/noctalia-i18n-watcher")
type Preview = tuple[dict[str, object], ...]


class _Session(requests.Session):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Locations of runtime configuration, message files, and state."""

    config: Path
    messages: Path
    state: Path

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> RuntimePaths:
        values = os.environ if environment is None else environment
        state_directory = values.get("STATE_DIRECTORY", "").strip()
        return cls(
            config=_CONFIG_DIR / "config.json",
            messages=_CONFIG_DIR / "messages",
            state=Path(state_directory) if state_directory else _STATE_DIR,
        )


@dataclass(frozen=True, slots=True)
class Application:
    """Configured application facade."""

    _config: Config
    _paths: RuntimePaths

    @classmethod
    def load(cls, paths: RuntimePaths | None = None) -> Application:
        selected = paths or RuntimePaths.from_environment()
        return cls(load_config(selected.config), selected)

    @property
    def log_level(self) -> str:
        return self._config.settings.log_level

    def run(self, monitor_id: str | None = None, *, flush: bool = False) -> None:
        """Run one monitoring cycle, optionally flushing immature deliveries."""

        failures: list[tuple[str, ValueError | RuntimeError]] = []
        for config in self._selected(monitor_id):
            _LOGGER.info("Running monitor %s", config.id)
            try:
                with self._runtime(config) as (monitor, state, notifier):
                    self._deliver(
                        monitor.run(flush=flush),
                        state,
                        notifier,
                    )
            except (ValueError, RuntimeError) as exc:
                failures.append((config.id, exc))
        self._raise_failures("Run", failures)

    def preview(self, monitor_id: str | None = None) -> Preview:
        """Return rendered changes without sending or changing state."""

        output: list[dict[str, object]] = []
        failures: list[tuple[str, ValueError | RuntimeError]] = []
        for config in self._selected(monitor_id):
            try:
                with self._runtime(config, read_only=True) as (
                    monitor,
                    _,
                    notifier,
                ):
                    output.extend(
                        self._preview(
                            config.id,
                            monitor.preview(flush=True),
                            notifier,
                        )
                    )
            except (ValueError, RuntimeError) as exc:
                failures.append((config.id, exc))
        self._raise_failures("Preview", failures)
        return tuple(output)

    def reset(
        self,
        mode: ResetMode,
        monitor_id: str | None = None,
        *,
        notify: bool = False,
    ) -> None:
        """Reset selected monitoring state and establish a new baseline."""

        failures: list[tuple[str, ValueError | RuntimeError]] = []
        for config in self._selected(monitor_id):
            _LOGGER.info("Resetting monitor %s", config.id)
            try:
                with self._runtime(config) as (monitor, state, notifier):
                    self._deliver(
                        monitor.reset(mode, notify=notify),
                        state,
                        notifier,
                    )
            except (ValueError, RuntimeError) as exc:
                failures.append((config.id, exc))
        self._raise_failures("Reset", failures)

    def status(
        self, monitor_id: str | None = None, *, notify: bool = False
    ) -> dict[str, StateSummary]:
        """Return persisted state and optionally notify configured routes."""

        summaries: dict[str, StateSummary] = {}
        failures: list[tuple[str, ValueError | RuntimeError]] = []
        for config in self._selected(monitor_id):
            try:
                with closing(
                    SQLiteState(self._database(config.id), read_only=True)
                ) as state:
                    summary = state.summary()
                summaries[config.id] = summary
                if notify:
                    self._send_status(config, summary)
            except (ValueError, RuntimeError) as exc:
                failures.append((config.id, exc))
        self._raise_failures("Status", failures)
        return summaries

    def validate(self, monitor_id: str | None = None) -> None:
        """Construct every configured component without network access."""

        failures: list[tuple[str, ValueError | RuntimeError]] = []
        for config in self._selected(monitor_id):
            try:
                with self._runtime(config, read_only=True):
                    pass
            except (ValueError, RuntimeError) as exc:
                failures.append((config.id, exc))
        self._raise_failures("Validation", failures)

    def history(self, page: int, monitor_id: str | None = None) -> tuple[Change, ...]:
        """Read one source-native history page."""

        config = self._single(monitor_id, "history")
        with closing(self._session()) as session:
            return self._source(config, session).history(page)

    @contextmanager
    def _runtime(
        self, config: MonitorConfig, *, read_only: bool = False
    ) -> Generator[tuple[Monitor, SQLiteState, DiscordNotifier], None, None]:
        with (
            closing(self._session()) as session,
            closing(
                SQLiteState(self._database(config.id), read_only=read_only)
            ) as state,
        ):
            notifier = self._notifier(config, self._client(config, session))
            yield (
                Monitor(
                    self._source(config, session),
                    state,
                    notifier.routes,
                    retention_days=self._config.settings.retention_days,
                ),
                state,
                notifier,
            )

    def _session(self) -> requests.Session:
        session = _Session(self._config.settings.request_timeout)
        session.trust_env = False
        if proxy_url := self._config.settings.proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
        return session

    def _source(
        self,
        config: MonitorConfig,
        session: requests.Session,
    ) -> NoctaliaSource:
        return NoctaliaSource(
            config.project,
            self._config.settings.request_timeout,
            session,
        )

    def _notifier(
        self,
        config: MonitorConfig,
        client: DiscordWebhookClient,
    ) -> DiscordNotifier:
        source_names = {
            webhook.source_message
            for webhook in config.webhooks
            if webhook.uses_source_message
        }
        target_names = {
            webhook.target_message
            for webhook in config.webhooks
            if webhook.uses_target_message
        }
        merge_names = {
            webhook.merge_message
            for webhook in config.webhooks
            if webhook.uses_merge_message
        }
        source_templates = {
            name: load_message(name, self._paths.messages / "source")
            for name in sorted(source_names)
        }
        target_templates = {
            name: load_message(name, self._paths.messages / "target")
            for name in sorted(target_names)
        }
        merge_templates = {
            name: load_merge(name, self._paths.messages / "merge")
            for name in sorted(merge_names)
        }
        return DiscordNotifier(
            tuple(
                DiscordRoute(
                    id=webhook.id,
                    monitor_id=config.id,
                    project=config.project,
                    locales=webhook.locales,
                    actions=webhook.actions,
                    delivery=webhook.delivery,
                    merge_threshold=webhook.merge_threshold,
                    source_template=(
                        source_templates[webhook.source_message]
                        if webhook.uses_source_message
                        else None
                    ),
                    target_template=(
                        target_templates[webhook.target_message]
                        if webhook.uses_target_message
                        else None
                    ),
                    merge_template=(
                        merge_templates[webhook.merge_message]
                        if webhook.uses_merge_message
                        else None
                    ),
                    baseline_renderer=(
                        partial(baseline_embed, config, webhook)
                        if webhook.notify_baseline
                        else None
                    ),
                    username=webhook.username,
                    avatar_url=webhook.avatar_url,
                )
                for webhook in config.webhooks
            ),
            client,
        )

    @staticmethod
    def _client(
        config: MonitorConfig,
        session: requests.Session,
    ) -> DiscordWebhookClient:
        return DiscordWebhookClient(
            session,
            {webhook.id: webhook.url for webhook in config.webhooks},
        )

    def _send_status(self, config: MonitorConfig, summary: StateSummary) -> None:
        with closing(self._session()) as session:
            client = self._client(config, session)
            for webhook in config.webhooks:
                send_status(client, config, webhook, summary)

    def _database(self, monitor_id: str) -> Path:
        return self._paths.state / f"{monitor_id}.sqlite3"

    def _selected(self, monitor_id: str | None) -> tuple[MonitorConfig, ...]:
        if monitor_id is None:
            return self._config.monitors
        selected = tuple(
            config for config in self._config.monitors if config.id == monitor_id
        )
        if not selected:
            raise ValueError(f"Unknown monitor: {monitor_id!r}")
        return selected

    def _single(self, monitor_id: str | None, operation: str) -> MonitorConfig:
        selected = self._selected(monitor_id)
        if len(selected) != 1:
            raise ValueError(
                f"{operation} requires --monitor when multiple monitors are configured"
            )
        return selected[0]

    @staticmethod
    def _preview(
        monitor_id: str,
        preview: MonitorResult,
        notifier: DiscordNotifier,
    ) -> Preview:
        if preview.baseline:
            return (
                {
                    "monitor_id": monitor_id,
                    "baseline": True,
                    "scanned": preview.scanned,
                    "source_texts": preview.source_texts,
                },
            )
        return tuple(
            {
                "monitor_id": monitor_id,
                "route_id": route_id,
                "embed": embed.to_dict(),
            }
            for route_id, deliveries in preview.deliveries.items()
            for embed in notifier.render(route_id, deliveries)
        )

    @staticmethod
    def _deliver(
        result: MonitorResult,
        state: SQLiteState,
        notifier: DiscordNotifier,
    ) -> None:
        for route_id in result.baseline_routes:
            notifier.send_baseline(route_id, result.scanned, result.source_texts)
            state.acknowledge_baseline(route_id)
        for route_id, deliveries in result.deliveries.items():
            notifier.send(
                route_id,
                deliveries,
                partial(state.acknowledge, route_id),
            )
            _LOGGER.info(
                "Delivered %d %s to route %s",
                len(deliveries),
                "change" if len(deliveries) == 1 else "changes",
                route_id,
            )

    @staticmethod
    def _raise_failures(
        operation: str,
        failures: Sequence[tuple[str, ValueError | RuntimeError]],
    ) -> None:
        if failures:
            details = "; ".join(
                f"{monitor_id}: {failure}" for monitor_id, failure in failures
            )
            raise RuntimeError(f"{operation} failed for {details}")
