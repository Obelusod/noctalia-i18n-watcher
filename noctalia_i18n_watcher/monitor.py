"""Monitoring policy and durable delivery preparation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from noctalia_i18n_core import SOURCE_LOCALE, Change, ChangeAction, ChangePage

_LOGGER = logging.getLogger(__name__)
type ResetMode = Literal["baseline", "full"]


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Accumulation and folding behavior for one route."""

    quiet_seconds: int
    max_wait_seconds: int
    fold_changes: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("quiet_seconds", self.quiet_seconds),
            ("max_wait_seconds", self.max_wait_seconds),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"Delivery {name} must be non-negative")
        if self.max_wait_seconds < self.quiet_seconds:
            raise ValueError(
                "Delivery max_wait_seconds must not be less than quiet_seconds"
            )
        if type(self.fold_changes) is not bool:
            raise ValueError("Delivery fold_changes must be a boolean")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """The latest source change and its matching English snapshot."""

    project: str
    change_id: str
    source_texts: dict[str, str]

    def __post_init__(self) -> None:
        if type(self.project) is not str or not self.project.strip():
            raise ValueError("Checkpoint project must be non-empty")
        if type(self.change_id) is not str:
            raise ValueError("Checkpoint change_id must be a string")
        if self.change_id and not self.change_id.strip():
            raise ValueError("Checkpoint change_id must contain non-whitespace text")
        if any(
            type(key) is not str or not key or type(value) is not str
            for key, value in self.source_texts.items()
        ):
            raise ValueError("Checkpoint source_texts must map keys to strings")
        object.__setattr__(self, "source_texts", dict(self.source_texts))


@dataclass(frozen=True, slots=True)
class Delivery:
    """A change enriched with its corresponding English source text."""

    change: Change
    source_text: str | None
    change_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_text is not None and type(self.source_text) is not str:
            raise ValueError("Delivery source_text must be a string or None")
        identifiers = self.change_ids or (self.change.id,)
        if any(type(item) is not str or not item for item in identifiers):
            raise ValueError("Delivery change IDs must be non-empty strings")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Delivery change IDs must be unique")
        object.__setattr__(self, "change_ids", identifiers)

    @classmethod
    def from_change(
        cls,
        change: Change,
        source_texts: Mapping[str, str],
        previous_source_texts: Mapping[str, str] | None = None,
    ) -> Delivery:
        if change.locale == SOURCE_LOCALE:
            source_text = (
                change.old_value if change.action == "deleted" else change.new_value
            )
        else:
            source_text = source_texts.get(change.key)
            if source_text is None and previous_source_texts is not None:
                source_text = previous_source_texts.get(change.key)
        return cls(change, source_text)


@dataclass(frozen=True, slots=True)
class QueuedDelivery:
    """A delivery retained until its route is ready."""

    delivery: Delivery
    queued_at: datetime

    def __post_init__(self) -> None:
        if type(self.queued_at) is not datetime or self.queued_at.utcoffset() is None:
            raise ValueError("Queued delivery time must include a timezone")
        object.__setattr__(self, "queued_at", self.queued_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """Changes and baseline notices ready for application delivery."""

    baseline: bool
    scanned: int
    source_texts: int
    baseline_routes: tuple[str, ...] = ()
    deliveries: Mapping[str, tuple[Delivery, ...]] = field(
        default_factory=dict[str, tuple[Delivery, ...]]
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "deliveries", dict(self.deliveries))


class Route(Protocol):
    """Change subscription and delivery policy for one route."""

    @property
    def id(self) -> str: ...

    @property
    def delivery(self) -> DeliveryPolicy: ...

    @property
    def notify_baseline(self) -> bool: ...

    def accepts_locale(self, locale: str, /) -> bool: ...

    def matches(self, change: Change, /) -> bool: ...


class _ChangeClient(Protocol):
    """Change-stream capabilities required by monitoring policy."""

    @property
    def slug(self) -> str: ...

    def change_pages(self) -> Iterator[ChangePage]: ...

    def iter_changes(self, *, after: str | None = None) -> Iterator[Change]: ...

    def catalog(self) -> dict[str, dict[str, str]]: ...


class _MonitorState(Protocol):
    """Persistence capabilities required by monitoring policy."""

    def load(self) -> Checkpoint | None: ...

    def save(self, checkpoint: Checkpoint, /) -> None: ...

    def reset(
        self,
        mode: ResetMode,
        checkpoint: Checkpoint,
        acknowledged_routes: Sequence[str],
        /,
    ) -> None: ...

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
        /,
    ) -> None: ...

    def pending(self, route_id: str, /) -> tuple[QueuedDelivery, ...]: ...

    def discard(self, route_id: str, change_ids: Sequence[str], /) -> None: ...

    def baseline_acknowledged(self, route_id: str, /) -> bool: ...

    def prune(self, retention_days: int, /) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def iso_timestamp(value: datetime) -> str:
    """Format a timezone-aware datetime as a UTC ISO 8601 timestamp."""

    if value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    microsecond = value.microsecond
    timespec = (
        "microseconds"
        if microsecond % 1000
        else "milliseconds"
        if microsecond
        else "seconds"
    )
    return value.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def _count(value: int, singular: str, plural: str | None = None) -> str:
    return f"{value} {singular if value == 1 else plural or singular + 's'}"


def _source_texts(catalog: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    return {
        key: translations[SOURCE_LOCALE]
        for key, translations in catalog.items()
        if SOURCE_LOCALE in translations
    }


def _advance_source_texts(
    previous: Mapping[str, str], changes: Sequence[Change]
) -> dict[str, str]:
    current = dict(previous)
    for change in changes:
        if change.locale != SOURCE_LOCALE:
            continue
        if change.action == "deleted":
            current.pop(change.key, None)
        elif change.new_value is None:
            raise RuntimeError("Source change does not contain its current text")
        else:
            current[change.key] = change.new_value
    return current


def _change_ids(deliveries: Sequence[Delivery]) -> tuple[str, ...]:
    return tuple(
        change_id for delivery in deliveries for change_id in delivery.change_ids
    )


def _fold(
    deliveries: Sequence[Delivery],
) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
    """Fold each locale/key history into its net delivery."""

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
            action: ChangeAction = "added"
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
        if change.locale == SOURCE_LOCALE:
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
    """Collect changes durably and prepare mature route batches."""

    def __init__(
        self,
        client: _ChangeClient,
        state: _MonitorState,
        routes: Sequence[Route],
        *,
        retention_days: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError("retention_days must be a non-negative integer")
        self._client = client
        self._state = state
        self._routes = self._validate_routes(routes)
        self._retention_days = retention_days
        self._clock = clock

    def run(self, *, flush: bool = False) -> MonitorResult:
        """Collect one cycle and return batches ready for delivery."""

        checkpoint = self._state.load()
        if checkpoint is None:
            return self._create_baseline()
        changes = self._changes(checkpoint)
        current = _advance_source_texts(checkpoint.source_texts, changes)
        observed_at = self._observed_at()
        queued = self._route_deliveries(
            changes,
            current,
            checkpoint.source_texts,
        )
        next_id = changes[-1].id if changes else checkpoint.change_id
        self._state.collect(
            Checkpoint(self._client.slug, next_id, current),
            queued,
            observed_at,
        )
        deliveries = self._ready_deliveries(observed_at, force=flush)
        self._state.prune(self._retention_days)
        collected = sum(len(items) for items in queued.values())
        if collected:
            _LOGGER.info(
                "Collected %s",
                _count(collected, "route delivery", "route deliveries"),
            )
        else:
            _LOGGER.debug("Collection completed without routed changes")
        return MonitorResult(
            False,
            len(changes),
            len(current),
            self._baseline_routes(),
            deliveries,
        )

    def preview(self, *, flush: bool = False) -> MonitorResult:
        """Prepare one monitoring cycle without changing state."""

        checkpoint = self._state.load()
        if checkpoint is None:
            scanned, source_texts, _ = self._baseline_data()
            return MonitorResult(
                True,
                scanned,
                len(source_texts),
                self._baseline_routes(),
            )
        changes = self._changes(checkpoint)
        current = _advance_source_texts(checkpoint.source_texts, changes)
        observed_at = self._observed_at()
        collected = self._route_deliveries(
            changes,
            current,
            checkpoint.source_texts,
        )
        deliveries: dict[str, tuple[Delivery, ...]] = {}
        for route in self._routes:
            queued = list(self._state.pending(route.id))
            queued.extend(
                QueuedDelivery(delivery, observed_at)
                for delivery in collected.get(route.id, ())
            )
            if not queued or (
                not flush and not self._ready(queued, route.delivery, observed_at)
            ):
                continue
            selected, _ = self._prepare(
                route,
                tuple(item.delivery for item in queued),
            )
            if selected:
                deliveries[route.id] = selected
        return MonitorResult(
            False,
            len(changes),
            len(current),
            self._baseline_routes(),
            deliveries,
        )

    def reset(self, mode: ResetMode, *, notify: bool = False) -> MonitorResult:
        """Establish a fresh baseline under the selected reset mode."""

        scanned, source_texts, change_id = self._baseline_data()
        baseline_routes = tuple(
            route.id for route in self._routes if route.notify_baseline
        )
        self._state.reset(
            mode,
            Checkpoint(self._client.slug, change_id, source_texts),
            () if notify else baseline_routes,
        )
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "%s reset created a baseline from %s and %s",
            mode.capitalize(),
            _count(scanned, "change"),
            _count(len(source_texts), "source text"),
        )
        return MonitorResult(
            True, scanned, len(source_texts), baseline_routes if notify else ()
        )

    def _create_baseline(self) -> MonitorResult:
        scanned, source_texts, change_id = self._baseline_data()
        self._state.save(Checkpoint(self._client.slug, change_id, source_texts))
        self._state.prune(self._retention_days)
        _LOGGER.info(
            "Baseline created from %s and %s",
            _count(scanned, "change"),
            _count(len(source_texts), "source text"),
        )
        return MonitorResult(
            True,
            scanned,
            len(source_texts),
            self._baseline_routes(),
        )

    def _baseline_data(self) -> tuple[int, dict[str, str], str]:
        scanned = 0
        change_id = ""
        for page in self._client.change_pages():
            scanned += len(page.changes)
            if page.changes:
                change_id = page.changes[0].id
                break
        source_texts = _source_texts(self._client.catalog())
        return scanned, source_texts, change_id

    def _changes(self, checkpoint: Checkpoint) -> tuple[Change, ...]:
        if checkpoint.project != self._client.slug:
            raise RuntimeError(
                "Stored checkpoint belongs to another project; reset baseline"
            )
        if checkpoint.change_id:
            newest_first = tuple(self._client.iter_changes(after=checkpoint.change_id))
        else:
            newest_first = tuple(self._client.iter_changes())
        changes = tuple(reversed(newest_first))
        if changes:
            _LOGGER.info("Found %s", _count(len(changes), "new change"))
        return changes

    def _baseline_routes(self) -> tuple[str, ...]:
        return tuple(
            route.id
            for route in self._routes
            if route.notify_baseline and not self._state.baseline_acknowledged(route.id)
        )

    def _route_deliveries(
        self,
        changes: Sequence[Change],
        current: Mapping[str, str],
        previous: Mapping[str, str],
    ) -> dict[str, list[Delivery]]:
        queued: dict[str, list[Delivery]] = {route.id: [] for route in self._routes}
        for change in changes:
            matching = tuple(
                route
                for route in self._routes
                if (
                    route.accepts_locale(change.locale)
                    if route.delivery.fold_changes
                    else route.matches(change)
                )
            )
            if not matching:
                continue
            delivery = Delivery.from_change(change, current, previous)
            for route in matching:
                queued[route.id].append(delivery)
        return queued

    def _prepare(
        self, route: Route, deliveries: Sequence[Delivery]
    ) -> tuple[tuple[Delivery, ...], tuple[str, ...]]:
        if route.delivery.fold_changes:
            prepared, discarded = _fold(deliveries)
        else:
            prepared, discarded = tuple(deliveries), ()
        selected: list[Delivery] = []
        ignored = list(discarded)
        for delivery in prepared:
            if route.matches(delivery.change):
                selected.append(delivery)
            else:
                ignored.extend(delivery.change_ids)
        return tuple(selected), tuple(ignored)

    def _ready_deliveries(
        self,
        now: datetime,
        *,
        force: bool,
    ) -> dict[str, tuple[Delivery, ...]]:
        ready: dict[str, tuple[Delivery, ...]] = {}
        for route in self._routes:
            queued = self._state.pending(route.id)
            if not queued or (
                not force and not self._ready(queued, route.delivery, now)
            ):
                continue
            deliveries, discarded = self._prepare(
                route, tuple(item.delivery for item in queued)
            )
            self._state.discard(route.id, discarded)
            if not deliveries:
                continue
            ready[route.id] = deliveries
            _LOGGER.info(
                "Prepared %s for route %s",
                _count(len(deliveries), "change"),
                route.id,
            )
        return ready

    def _observed_at(self) -> datetime:
        observed_at = self._clock()
        if observed_at.utcoffset() is None:
            raise ValueError("Monitor clock must return a timezone-aware datetime")
        return observed_at

    @staticmethod
    def _validate_routes(routes: Sequence[Route]) -> tuple[Route, ...]:
        snapshot = tuple(routes)
        identifiers: set[str] = set()
        for route in snapshot:
            route_id = route.id
            if type(route_id) is not str or not route_id.strip():
                raise ValueError("Route id must be a non-empty string")
            if route_id in identifiers:
                raise ValueError(f"Route id must be unique: {route_id!r}")
            identifiers.add(route_id)
        return snapshot

    @staticmethod
    def _ready(
        queued: Sequence[QueuedDelivery], policy: DeliveryPolicy, now: datetime
    ) -> bool:
        first = min(item.queued_at for item in queued)
        last = max(item.queued_at for item in queued)
        quiet_at = last + timedelta(seconds=policy.quiet_seconds)
        forced_at = first + timedelta(seconds=policy.max_wait_seconds)
        return now >= quiet_at or now >= forced_at
