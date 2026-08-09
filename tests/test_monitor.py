"""Monitoring policy tests independent of transport and website parsing."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from noctalia_i18n_core import Change, ChangeAction, ChangePage

from noctalia_i18n_watcher.monitor import Checkpoint, DeliveryPolicy, Monitor
from noctalia_i18n_watcher.state import SQLiteState

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _change(
    identifier: str,
    action: ChangeAction = "modified",
    *,
    key: str = "settings.example",
    locale: str = "zh-Hans",
    offset: int = 0,
) -> Change:
    old = None if action == "added" else f"old-{identifier}"
    new = None if action == "deleted" else f"new-{identifier}"
    if action == "modified":
        old, new = f"old-{identifier}", f"new-{identifier}"
    return Change(
        id=identifier,
        key=key,
        locale=locale,
        action=action,
        old_value=old,
        new_value=new,
        occurred_at=_BASE + timedelta(seconds=offset),
        translation_url="https://i18n.noctalia.dev/projects/noctalia/translate",
        actor_login=None,
    )


@dataclass(frozen=True, slots=True)
class _Route:
    id: str
    locales: frozenset[str]
    actions: frozenset[ChangeAction]
    delivery: DeliveryPolicy
    notify_baseline: bool = False

    def accepts_locale(self, locale: str) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, change: Change) -> bool:
        return self.accepts_locale(change.locale) and change.action in self.actions


class _Client:
    slug = "noctalia"

    def __init__(self, changes: tuple[Change, ...] = ()) -> None:
        self.changes = changes
        self.iter_calls = 0
        self.anchor_error = False

    def change_pages(self) -> Iterator[ChangePage]:
        yield ChangePage(
            number=1,
            total_pages=1,
            changes=tuple(reversed(self.changes)),
            cursor=self.changes[0].id if self.changes else None,
        )

    def iter_changes(self, *, after: str | None = None) -> Iterator[Change]:
        self.iter_calls += 1
        if self.anchor_error:
            raise RuntimeError("change anchor was not found")
        yield from reversed(self.changes)

    def catalog(self) -> dict[str, dict[str, str]]:
        return {
            "settings.example": {"en": "English", "zh-Hans": "中文"},
            "settings.target-only": {"zh-Hans": "仅目标语言"},
        }


class MonitorTests(unittest.TestCase):
    def test_baseline_does_not_replay_and_keeps_only_english(self) -> None:
        client = _Client((_change("latest"),))
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            result = Monitor(
                client,
                state,
                (),
                retention_days=180,
            ).run()
            checkpoint = state.load()

        self.assertTrue(result.baseline)
        self.assertEqual(result.scanned, 1)
        if checkpoint is None:
            self.fail("checkpoint was not saved")
        self.assertEqual(checkpoint.source_texts, {"settings.example": "English"})
        self.assertEqual(client.iter_calls, 0)

    def test_project_mismatch_does_not_write_state(self) -> None:
        client = _Client()
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            checkpoint = Checkpoint("other-project", "head", {"key": "English"})
            state.save(checkpoint)
            with self.assertRaisesRegex(RuntimeError, "another project"):
                Monitor(
                    client,
                    state,
                    (),
                    retention_days=180,
                ).run()
            self.assertEqual(state.load(), checkpoint)

    def test_missing_anchor_does_not_write_state(self) -> None:
        client = _Client()
        client.anchor_error = True
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            checkpoint = Checkpoint("noctalia", "missing", {"key": "English"})
            state.save(checkpoint)
            with self.assertRaisesRegex(RuntimeError, "anchor"):
                Monitor(
                    client,
                    state,
                    (),
                    retention_days=180,
                ).run()
            self.assertEqual(state.load(), checkpoint)

    def test_core_newest_first_stream_is_queued_oldest_first(self) -> None:
        changes = (_change("old", offset=1), _change("new", offset=2))
        client = _Client(changes)
        route = _Route(
            "route",
            frozenset({"zh-Hans"}),
            frozenset({"modified"}),
            DeliveryPolicy(0, 0, False),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            state.save(
                Checkpoint("noctalia", "anchor", {"settings.example": "English"})
            )
            result = Monitor(
                client,
                state,
                (route,),
                retention_days=180,
            ).run(flush=True)

        self.assertEqual(
            tuple(item.change.id for item in result.deliveries["route"]),
            ("old", "new"),
        )

    def test_maximum_wait_matures_a_queued_batch(self) -> None:
        client = _Client((_change("one"),))
        route = _Route(
            "route",
            frozenset({"zh-Hans"}),
            frozenset({"modified"}),
            DeliveryPolicy(20, 20, False),
        )
        clock = [_BASE]
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            state.save(
                Checkpoint("noctalia", "anchor", {"settings.example": "English"})
            )
            monitor = Monitor(
                client,
                state,
                (route,),
                retention_days=180,
                clock=lambda: clock[0],
            )
            self.assertFalse(monitor.run().deliveries)
            client.changes = ()
            clock[0] += timedelta(seconds=21)
            result = monitor.run()

        self.assertEqual(tuple(result.deliveries), ("route",))

    def test_folding_preserves_raw_ids_and_filters_after_folding(self) -> None:
        changes = (
            _change("delete", "deleted", offset=1),
            _change("add", "added", offset=2),
        )
        client = _Client(changes)
        route = _Route(
            "route",
            frozenset({"zh-Hans"}),
            frozenset({"modified"}),
            DeliveryPolicy(0, 0, True),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            state.save(
                Checkpoint("noctalia", "anchor", {"settings.example": "English"})
            )
            result = Monitor(
                client,
                state,
                (route,),
                retention_days=180,
            ).run(flush=True)

        delivery = result.deliveries["route"][0]
        self.assertEqual(delivery.change.action, "modified")
        self.assertEqual(delivery.change_ids, ("delete", "add"))

    def test_preview_does_not_advance_checkpoint_or_outbox(self) -> None:
        client = _Client((_change("new"),))
        route = _Route(
            "route",
            frozenset({"zh-Hans"}),
            frozenset({"modified"}),
            DeliveryPolicy(0, 0, False),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            closing(SQLiteState(Path(directory) / "state.sqlite3")) as state,
        ):
            checkpoint = Checkpoint(
                "noctalia", "anchor", {"settings.example": "English"}
            )
            state.save(checkpoint)
            result = Monitor(
                client,
                state,
                (route,),
                retention_days=180,
            ).preview(flush=True)
            self.assertEqual(state.load(), checkpoint)
            self.assertFalse(state.pending("route"))

        self.assertEqual(len(result.deliveries["route"]), 1)
