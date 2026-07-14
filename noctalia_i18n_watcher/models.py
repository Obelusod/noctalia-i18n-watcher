"""Noctalia domain models shared across acquisition and delivery adapters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeGuard

type Action = Literal["added", "modified", "deleted"]
type ResetScope = Literal["baseline", "state"]
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)

ACTIONS: tuple[Action, ...] = ("added", "modified", "deleted")
# Noctalia uses English for source text and as the fallback mapping.
SOURCE_LOCALE = "en"
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LOCALE_ID_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class WatcherError(RuntimeError):
    """An expected operational failure that is safe to report without a traceback."""


class ValidationError(WatcherError):
    """Invalid external configuration or persisted data."""


def validate_json(value: object, label: str) -> JsonValue:
    """Validate and normalize a value to the supported JSON data model."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            validate_json(item, f"{label}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} must use string mapping keys")
            output[key] = validate_json(item, f"{label}.{key}")
        return output
    raise ValueError(f"{label} contains a non-JSON value")


def is_action(value: str) -> TypeGuard[Action]:
    """Narrow a string to one supported change action."""

    return value in ACTIONS


@dataclass(frozen=True, slots=True)
class Change:
    """One normalized Noctalia translation change."""

    id: str
    key: str
    locale: str
    actor: str
    old_value: str | None
    new_value: str | None
    action: Action
    occurred_at: datetime
    url: str | None
    actor_url: str | None = None
    actor_avatar_url: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("key", self.key),
            ("actor", self.actor),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"Change {name} must be a non-empty string")
        for name, value in (
            ("url", self.url),
            ("actor_url", self.actor_url),
            ("actor_avatar_url", self.actor_avatar_url),
        ):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"Change {name} must be null or a non-empty string")
        locale: object = self.locale
        if type(locale) is not str or not LOCALE_ID_PATTERN.fullmatch(locale):
            raise ValueError(f"Invalid change locale identifier: {self.locale!r}")
        if self.action not in ACTIONS:
            raise ValueError(f"Invalid change action: {self.action!r}")
        occurred_at: object = self.occurred_at
        if type(occurred_at) is not datetime or occurred_at.utcoffset() is None:
            raise ValueError("Change occurred_at must include a timezone")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))

        old, new = self.old_value, self.new_value
        valid = {
            "added": old is None and isinstance(new, str),
            "modified": isinstance(old, str) and isinstance(new, str),
            "deleted": isinstance(old, str) and new is None,
        }
        if not valid[self.action]:
            raise ValueError(f"Change values do not match action {self.action!r}")

    @property
    def is_source(self) -> bool:
        """Whether this change belongs to Noctalia's source language."""

        return self.locale == SOURCE_LOCALE

    @property
    def iso_timestamp(self) -> str:
        """Return a UTC ISO 8601 timestamp without discarding available precision."""

        microsecond = self.occurred_at.microsecond
        if microsecond % 1000:
            timespec = "microseconds"
        elif microsecond:
            timespec = "milliseconds"
        else:
            timespec = "seconds"
        return self.occurred_at.isoformat(timespec=timespec).replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PollResult:
    """One source poll with normalized changes and optional source texts."""

    changes: tuple[Change, ...]
    cursor: JsonValue
    scanned: int
    source_texts: dict[str, str] | None = None

    def __post_init__(self) -> None:
        cursor = validate_json(self.cursor, "Poll result cursor")
        if cursor is None:
            raise ValueError("Poll result cursor must not be null")
        object.__setattr__(self, "cursor", cursor)
        scanned: object = self.scanned
        if type(scanned) is not int:
            raise ValueError("Poll result scanned count must be an integer")
        if self.scanned < len(self.changes):
            raise ValueError("Poll result scanned count is too small")
        if self.source_texts is not None and not isinstance(self.source_texts, dict):
            raise ValueError("Poll result source texts must be a dictionary or null")
        ids = [change.id for change in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("Poll result change IDs must be unique")
        if any(
            older.occurred_at > newer.occurred_at
            for older, newer in zip(self.changes, self.changes[1:], strict=False)
        ):
            raise ValueError("Poll result changes must be ordered oldest first")
        if self.source_texts is not None:
            for key, value in self.source_texts.items():
                if type(key) is not str or not key or type(value) is not str:
                    raise ValueError(
                        "Poll result source texts must map non-empty strings to strings"
                    )
            object.__setattr__(self, "source_texts", dict(self.source_texts))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Opaque acquisition cursor and its matching source-language snapshot."""

    cursor: JsonValue
    source_texts: dict[str, str]


@dataclass(frozen=True, slots=True)
class StateSummary:
    """Small operational view of persisted monitoring state."""

    initialized: bool
    updated_at: str | None
    source_texts: int
    deliveries: int
    baseline_notifications: int
    pending_deliveries: int
    pending_routes: int


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """One route's selection policy for merging locale batches."""

    enabled: bool
    threshold: int

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Merge enabled must be a boolean")
        if type(self.threshold) is not int or self.threshold < 0:
            raise ValueError("Merge threshold must be non-negative")


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """One route's accumulation, folding, and merge policy."""

    quiet_seconds: int
    max_wait_seconds: int
    latest_only: bool
    merge: MergePolicy

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
        if type(self.latest_only) is not bool:
            raise ValueError("Delivery latest_only must be a boolean")
        if not isinstance(self.merge, MergePolicy):
            raise ValueError("Delivery merge must be a MergePolicy")


@dataclass(frozen=True, slots=True)
class Delivery:
    """A change enriched with its source-language text."""

    change: Change
    source_text: str | None
    change_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = self.change_ids or (self.change.id,)
        if any(type(change_id) is not str or not change_id for change_id in ids):
            raise ValueError("Delivery change IDs must be non-empty strings")
        if len(ids) != len(set(ids)):
            raise ValueError("Delivery change IDs must be unique")
        object.__setattr__(self, "change_ids", ids)

    @classmethod
    def from_change(
        cls,
        change: Change,
        source_texts: Mapping[str, str],
        previous_source_texts: Mapping[str, str] | None = None,
    ) -> Delivery:
        """Attach the source-language text needed to render a change."""

        if change.is_source:
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
    """A delivery retained in the outbox until its route is ready."""

    delivery: Delivery
    queued_at: datetime

    def __post_init__(self) -> None:
        if type(self.queued_at) is not datetime or self.queued_at.utcoffset() is None:
            raise ValueError("Queued delivery time must include a timezone")
        object.__setattr__(self, "queued_at", self.queued_at.astimezone(UTC))
