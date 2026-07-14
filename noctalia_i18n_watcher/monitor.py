"""Monitoring policy and its infrastructure contracts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from .models import (
    Change,
    Checkpoint,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    PollResult,
    QueuedDelivery,
    ResetScope,
    WatcherError,
    validate_json,
)

type RunMode = Literal["normal", "dry-run"]
type RunResult = tuple[dict[str, JsonValue], ...]

_LOGGER = logging.getLogger(__name__)


class ChangeSource(Protocol):
    """Polling boundary consumed by the monitor."""

    def poll(self, cursor: JsonValue | None, /) -> PollResult: ...


class StateStore(Protocol):
    """Checkpoint, outbox, and delivery persistence boundary."""

    def load(self) -> Checkpoint | None: ...

    def save(self, checkpoint: Checkpoint, /) -> None: ...

    def reset(
        self,
        scope: ResetScope,
        checkpoint: Checkpoint,
        notified_routes: Sequence[str],
        /,
    ) -> None:
        """Apply one reset scope and establish the supplied baseline."""

        ...

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
        /,
    ) -> None:
        """Atomically advance the checkpoint and enqueue route deliveries."""

        ...

    def pending(self, route_id: str, /) -> tuple[QueuedDelivery, ...]: ...

    def acknowledge(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def discard(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def baseline_notified(self, route_id: str, /) -> bool: ...

    def record_baseline(self, route_id: str, /) -> None: ...

    def prune(self, retention_days: int, /) -> None: ...


class Route(Protocol):
    """Change subscription and delivery policy for one target."""

    @property
    def id(self) -> str: ...

    @property
    def delivery(self) -> DeliveryPolicy: ...

    @property
    def notify_baseline(self) -> bool: ...

    def accepts_locale(self, locale: str, /) -> bool: ...

    def matches(self, change: Change, /) -> bool: ...


class Notifier(Protocol):
    """Route-aware rendering and delivery boundary."""

    @property
    def routes(self) -> Sequence[Route]: ...

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None],
        /,
    ) -> None:
        """Deliver a route batch and acknowledge each successful request."""

        ...

    def render(
        self, route_id: str, deliveries: Sequence[Delivery], /
    ) -> Sequence[Mapping[str, object]]:
        """Render a route batch without delivery or persistence side effects."""

        ...

    def send_baseline(
        self, route_id: str, changes: int, source_texts: int, /
    ) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _count(value: int, singular: str, plural: str | None = None) -> str:
    return f"{value} {singular if value == 1 else plural or singular + 's'}"


def _change_ids(deliveries: Sequence[Delivery]) -> tuple[str, ...]:
    return tuple(
        change_id for delivery in deliveries for change_id in delivery.change_ids
    )


def _fold(
    deliveries: Sequence[Delivery],
) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
    """Fold each locale/key history into its net delivery and discarded IDs."""

    groups: dict[tuple[str, str], list[Delivery]] = {}
    for delivery in deliveries:
        change = delivery.change
        groups.setdefault((change.locale, change.key), []).append(delivery)

    selected: list[Delivery] = []
    discarded: list[str] = []
    absent = object()
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.change.occurred_at)
        first, last = ordered[0], ordered[-1]
        initial: object = (
            absent if first.change.action == "added" else first.change.old_value
        )
        final: object = (
            absent if last.change.action == "deleted" else last.change.new_value
        )
        ids = _change_ids(ordered)

        if (initial is absent and final is absent) or initial == final:
            discarded.extend(ids)
            continue
        if initial is absent:
            action = "added"
            old_value = None
            new_value = str(final)
        elif final is absent:
            action = "deleted"
            old_value = str(initial)
            new_value = None
        else:
            action = "modified"
            old_value = str(initial)
            new_value = str(final)

        change = replace(
            last.change,
            action=action,
            old_value=old_value,
            new_value=new_value,
        )
        if change.is_source:
            source_text = old_value if action == "deleted" else new_value
        else:
            source_text = next(
                (
                    item.source_text
                    for item in reversed(ordered)
                    if item.source_text is not None
                ),
                None,
            )
        selected.append(Delivery(change, source_text, ids))

    selected.sort(key=lambda item: item.change.occurred_at)
    return tuple(selected), tuple(discarded)


class Monitor:
    """Collect changes durably, then deliver mature route batches."""

    def __init__(
        self,
        source: ChangeSource,
        state: StateStore,
        notifier: Notifier,
        *,
        retention_days: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._source: ChangeSource = source
        self._state: StateStore = state
        self._notifier: Notifier = notifier
        self._retention_days: int = retention_days
        self._clock: Callable[[], datetime] = clock

    def run(
        self,
        mode: RunMode = "normal",
        *,
        flush: bool = False,
    ) -> RunResult:
        """Run or preview one monitoring cycle."""

        dry_run = mode == "dry-run"
        checkpoint = self._state.load()
        result = self._source.poll(None if checkpoint is None else checkpoint.cursor)
        if checkpoint is None:
            return self._baseline(result, dry_run)
        if result.changes and result.source_texts is None:
            raise WatcherError("Source poll with changes did not include source texts")

        if result.changes:
            _LOGGER.info("Found %s", _count(len(result.changes), "new change"))
        current = (
            result.source_texts
            if result.source_texts is not None
            else checkpoint.source_texts
        )
        observed_at = self._clock()
        queued = self._route_deliveries(
            result.changes,
            current,
            checkpoint.source_texts,
        )
        if dry_run:
            return self._preview(queued, observed_at)

        self._send_baselines(result.scanned, len(current))
        self._state.collect(Checkpoint(result.cursor, current), queued, observed_at)
        self._flush(observed_at, force=flush)
        self._state.prune(self._retention_days)
        collected = sum(len(items) for items in queued.values())
        if collected:
            _LOGGER.info(
                "Collected %s",
                _count(collected, "route delivery", "route deliveries"),
            )
        else:
            _LOGGER.debug("Collection completed without routed changes")
        return ()

    def reset(self, scope: ResetScope, *, notify: bool = False) -> None:
        """Establish a fresh baseline under the selected reset scope."""

        result = self._source.poll(None)
        texts = result.source_texts
        if texts is None:
            raise WatcherError("Initial source poll did not include source texts")
        routes = self._reset_routes(result.scanned, len(texts), notify)
        self._state.reset(
            scope,
            Checkpoint(result.cursor, texts),
            routes,
        )
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "%s reset created a baseline from %s and %s",
            scope.capitalize(),
            _count(result.scanned, "change"),
            _count(len(texts), "source text"),
        )

    def _baseline(self, result: PollResult, dry_run: bool) -> RunResult:
        texts = result.source_texts
        if texts is None:
            raise WatcherError("Initial source poll did not include source texts")
        if dry_run:
            return (
                {
                    "baseline": True,
                    "scanned": result.scanned,
                    "source_texts": len(texts),
                },
            )

        self._send_baselines(result.scanned, len(texts))
        self._state.save(Checkpoint(result.cursor, texts))
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "Baseline created from %s and %s",
            _count(result.scanned, "change"),
            _count(len(texts), "source text"),
        )
        return ()

    def _reset_routes(
        self,
        changes: int,
        source_texts: int,
        notify: bool,
    ) -> tuple[str, ...]:
        routes: list[str] = []
        for route in self._notifier.routes:
            if not route.notify_baseline:
                continue
            if notify:
                self._notifier.send_baseline(route.id, changes, source_texts)
            routes.append(route.id)
        return tuple(routes)

    def _send_baselines(self, changes: int, source_texts: int) -> None:
        for route in self._notifier.routes:
            if not route.notify_baseline or self._state.baseline_notified(route.id):
                continue
            self._notifier.send_baseline(route.id, changes, source_texts)
            self._state.record_baseline(route.id)

    def _route_deliveries(
        self,
        changes: Sequence[Change],
        current: Mapping[str, str],
        previous: Mapping[str, str],
    ) -> dict[str, list[Delivery]]:
        routes = self._notifier.routes
        queued: dict[str, list[Delivery]] = {route.id: [] for route in routes}
        for change in changes:
            matching = tuple(
                route
                for route in routes
                if route.accepts_locale(change.locale)
                and (route.delivery.latest_only or route.matches(change))
            )
            if not matching:
                continue
            delivery = Delivery.from_change(change, current, previous)
            for route in matching:
                queued[route.id].append(delivery)
        return queued

    def _prepare(
        self, route: Route, queued: Sequence[QueuedDelivery]
    ) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
        """Fold histories before filtering because folding can change the action."""

        raw = tuple(item.delivery for item in queued)
        if route.delivery.latest_only:
            deliveries, discarded = _fold(raw)
        else:
            deliveries, discarded = raw, ()
        selected: list[Delivery] = []
        ignored = list(discarded)
        for delivery in deliveries:
            if route.matches(delivery.change):
                selected.append(delivery)
            else:
                ignored.extend(delivery.change_ids)
        return tuple(selected), tuple(ignored)

    def _preview(
        self, collected: Mapping[str, Sequence[Delivery]], observed_at: datetime
    ) -> RunResult:
        output: list[dict[str, JsonValue]] = []
        for route in self._notifier.routes:
            queued = list(self._state.pending(route.id))
            queued.extend(
                QueuedDelivery(delivery, observed_at)
                for delivery in collected.get(route.id, ())
            )
            deliveries, _ = self._prepare(route, queued)
            for embed in self._notifier.render(route.id, deliveries):
                rendered = validate_json(dict(embed), "Rendered notification")
                if not isinstance(rendered, dict):
                    raise TypeError("Rendered notification must be a JSON object")
                output.append({"route_id": route.id, "embed": rendered})
        _LOGGER.info("Dry run completed without changing state")
        return tuple(output)

    def _flush(self, now: datetime, *, force: bool) -> None:
        for route in self._notifier.routes:
            queued = self._state.pending(route.id)
            if not queued or (
                not force and not self._ready(queued, route.delivery, now)
            ):
                continue
            deliveries, discarded = self._prepare(route, queued)
            self._state.discard(route.id, discarded)
            if not deliveries:
                continue

            def acknowledge(sent: Sequence[Delivery], route_id: str = route.id) -> None:
                self._state.acknowledge(route_id, _change_ids(sent))

            self._notifier.send(route.id, deliveries, acknowledge)
            _LOGGER.info(
                "Delivered %s to route %s",
                _count(len(deliveries), "change"),
                route.id,
            )

    @staticmethod
    def _ready(
        queued: Sequence[QueuedDelivery], policy: DeliveryPolicy, now: datetime
    ) -> bool:
        """Return whether inactivity or the maximum wait has matured the batch."""

        first = min(item.queued_at for item in queued)
        last = max(item.queued_at for item in queued)
        quiet_at = last + timedelta(seconds=policy.quiet_seconds)
        forced_at = first + timedelta(seconds=policy.max_wait_seconds)
        return now >= quiet_at or now >= forced_at
