"""Application composition and use cases."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config, MonitorConfig, load_config
from .discord import DiscordNotifier, DiscordOptions, DiscordRoute
from .messages import load_merge, load_message
from .models import Change, JsonValue, ResetScope, StateSummary, WatcherError
from .monitor import Monitor, RunResult
from .sources import HistorySource, Source, build_source
from .state import SQLiteState, execution_lock

_LOGGER = logging.getLogger(__name__)
_CONFIG_DIR = Path("/etc/noctalia-i18n-watcher")
_STATE_DIR = Path("/var/lib/noctalia-i18n-watcher")


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
        credential_dir = values.get("CREDENTIALS_DIRECTORY", "").strip()
        state_directory = values.get("STATE_DIRECTORY", "").strip()
        state_dir = Path(state_directory) if state_directory else _STATE_DIR
        return cls(
            config=(
                Path(credential_dir) / "config.json"
                if credential_dir
                else _CONFIG_DIR / "config.json"
            ),
            messages=_CONFIG_DIR / "messages",
            state=state_dir,
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

        selected = self._selected(monitor_id)
        failures: list[tuple[str, WatcherError]] = []
        with execution_lock(self._paths.state / "watcher.lock"):
            for config in selected:
                _LOGGER.info("Running monitor %s", config.id)
                try:
                    with self._monitor(config) as monitor:
                        monitor.run(flush=flush)
                except WatcherError as exc:
                    failures.append((config.id, exc))
        self._raise_failures("Run", failures)

    def preview(self, monitor_id: str | None = None) -> RunResult:
        """Return pending deliveries without sending or changing state."""

        output: list[dict[str, JsonValue]] = []
        failures: list[tuple[str, WatcherError]] = []
        for config in self._selected(monitor_id):
            try:
                with self._monitor(config, read_only=True) as monitor:
                    for item in monitor.run("dry-run"):
                        output.append({**item, "monitor_id": config.id})
            except WatcherError as exc:
                failures.append((config.id, exc))
        self._raise_failures("Preview", failures)
        return tuple(output)

    def reset(
        self,
        scope: ResetScope,
        monitor_id: str | None = None,
        *,
        notify: bool = False,
    ) -> None:
        """Reset the selected monitoring state and establish a new baseline."""

        selected = self._selected(monitor_id)
        failures: list[tuple[str, WatcherError]] = []
        with execution_lock(self._paths.state / "watcher.lock"):
            for config in selected:
                _LOGGER.info("Resetting monitor %s", config.id)
                try:
                    with self._monitor(config) as monitor:
                        monitor.reset(scope, notify=notify)
                except WatcherError as exc:
                    failures.append((config.id, exc))
        self._raise_failures("Reset", failures)

    def status(
        self, monitor_id: str | None = None, *, notify: bool = False
    ) -> dict[str, StateSummary]:
        """Return persisted monitoring state and optionally notify Discord routes."""

        summaries: dict[str, StateSummary] = {}
        failures: list[tuple[str, WatcherError]] = []
        for config in self._selected(monitor_id):
            try:
                with closing(
                    SQLiteState(self._database(config.id), read_only=True)
                ) as state:
                    summary = state.summary()
                summaries[config.id] = summary
                if notify:
                    with closing(self._notifier(config)) as notifier:
                        for route in notifier.routes:
                            notifier.send_status(route.id, summary)
            except WatcherError as exc:
                failures.append((config.id, exc))
        self._raise_failures("Status", failures)
        return summaries

    def validate(self, monitor_id: str | None = None) -> None:
        """Construct every configured component without network access."""

        failures: list[tuple[str, WatcherError]] = []
        for config in self._selected(monitor_id):
            try:
                with (
                    closing(
                        SQLiteState(self._database(config.id), read_only=True)
                    ) as state,
                    closing(self._source(config)),
                    closing(self._notifier(config)),
                ):
                    state.summary()
            except WatcherError as exc:
                failures.append((config.id, exc))
        self._raise_failures("Validation", failures)

    def history(self, page: int, monitor_id: str | None = None) -> tuple[Change, ...]:
        """Read a historical change page when the source supports it."""

        config = self._single(monitor_id, "history")
        with closing(self._source(config)) as source:
            if not isinstance(source, HistorySource):
                raise WatcherError("The configured source does not expose history")
            return source.history(page)

    @contextmanager
    def _monitor(
        self, config: MonitorConfig, *, read_only: bool = False
    ) -> Generator[Monitor, None, None]:
        with (
            closing(
                SQLiteState(self._database(config.id), read_only=read_only)
            ) as state,
            closing(self._source(config)) as source,
            closing(self._notifier(config)) as notifier,
        ):
            yield Monitor(
                source,
                state,
                notifier,
                retention_days=self._config.settings.retention_days,
            )

    def _source(self, config: MonitorConfig) -> Source:
        http = self._config.settings.http
        return build_source(
            config.source.adapter,
            config.source.options,
            project=config.source.project,
            timeout=http.request_timeout,
            proxy=http.proxy_url,
        )

    def _notifier(self, config: MonitorConfig) -> DiscordNotifier:
        source_names = {
            webhook.messages.source
            for webhook in config.webhooks
            if webhook.uses_source_message
        }
        target_names = {
            webhook.messages.target
            for webhook in config.webhooks
            if webhook.uses_target_message
        }
        merge_names = {
            webhook.messages.merge
            for webhook in config.webhooks
            if webhook.uses_merge_message
        }
        source_messages = {
            name: load_message(name, self._paths.messages / "source")
            for name in sorted(source_names)
        }
        target_messages = {
            name: load_message(name, self._paths.messages / "target")
            for name in sorted(target_names)
        }
        merge_messages = {
            name: load_merge(name, self._paths.messages / "merge")
            for name in sorted(merge_names)
        }
        routes = tuple(
            DiscordRoute(
                id=webhook.id,
                monitor_id=config.id,
                project=config.source.project,
                url=webhook.url,
                username=webhook.username,
                avatar_url=webhook.avatar_url,
                locales=webhook.locales,
                actions=webhook.actions,
                source_message=(
                    source_messages[webhook.messages.source]
                    if webhook.uses_source_message
                    else None
                ),
                target_message=(
                    target_messages[webhook.messages.target]
                    if webhook.uses_target_message
                    else None
                ),
                merge_message=(
                    merge_messages[webhook.messages.merge]
                    if webhook.uses_merge_message
                    else None
                ),
                delivery=webhook.delivery,
                notify_baseline=webhook.notify_baseline,
            )
            for webhook in config.webhooks
        )
        http = self._config.settings.http
        return DiscordNotifier(
            routes,
            DiscordOptions(
                proxy_url=http.proxy_url,
                request_timeout=http.request_timeout,
            ),
        )

    def _database(self, monitor_id: str) -> Path:
        return self._paths.state / f"{monitor_id}.sqlite3"

    def _selected(self, monitor_id: str | None) -> tuple[MonitorConfig, ...]:
        if monitor_id is None:
            return self._config.monitors
        selected = tuple(
            config for config in self._config.monitors if config.id == monitor_id
        )
        if not selected:
            raise WatcherError(f"Unknown monitor: {monitor_id!r}")
        return selected

    def _single(self, monitor_id: str | None, operation: str) -> MonitorConfig:
        selected = self._selected(monitor_id)
        if len(selected) != 1:
            raise WatcherError(
                f"{operation} requires --monitor when multiple monitors are configured"
            )
        return selected[0]

    @staticmethod
    def _raise_failures(
        operation: str, failures: Sequence[tuple[str, WatcherError]]
    ) -> None:
        if not failures:
            return
        details = "; ".join(
            f"{monitor_id}: {failure}" for monitor_id, failure in failures
        )
        raise WatcherError(f"{operation} failed for {details}")
