"""Monitoring-policy tests using in-memory adapters."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from noctalia_i18n_watcher.models import (
    Action,
    Change,
    Checkpoint,
    Delivery,
    DeliveryPolicy,
    JsonValue,
    MergePolicy,
    PollResult,
    QueuedDelivery,
    ResetScope,
    WatcherError,
)
from noctalia_i18n_watcher.monitor import Monitor

from .fixtures import RUN_AT, fixture_change


@dataclass(frozen=True, slots=True)
class FakeRoute:
    id: str
    locales: frozenset[str] = frozenset({"en", "zh-Hans"})
    actions: frozenset[Action] = frozenset({"added", "modified", "deleted"})
    notify_baseline: bool = True
    delivery: DeliveryPolicy = DeliveryPolicy(
        0,
        0,
        True,
        MergePolicy(enabled=True, threshold=5),
    )

    def accepts_locale(self, locale: str) -> bool:
        return "*" in self.locales or locale in self.locales

    def matches(self, changed: Change) -> bool:
        return self.accepts_locale(changed.locale) and changed.action in self.actions


class FakeSource:
    def __init__(self, result: PollResult, source_texts: dict[str, str] | None) -> None:
        self.result = result
        self.source_texts = source_texts
        self.polled_with: list[JsonValue | None] = []
        self.source_text_reads = 0

    def poll(self, cursor: JsonValue | None) -> PollResult:
        self.polled_with.append(cursor)
        include_texts = cursor is None or bool(self.result.changes)
        if include_texts:
            self.source_text_reads += 1
        return replace(
            self.result,
            source_texts=self.source_texts if include_texts else None,
        )


class MemoryState:
    def __init__(self, checkpoint: Checkpoint | None = None) -> None:
        self.checkpoint = checkpoint
        self.receipts: set[tuple[str, str]] = set()
        self.notified_routes: set[str] = set()
        self.outbox: dict[str, list[QueuedDelivery]] = {}
        self.pruned: list[int] = []

    def load(self) -> Checkpoint | None:
        return self.checkpoint

    def save(self, checkpoint: Checkpoint) -> None:
        self.checkpoint = checkpoint

    def reset(
        self,
        scope: ResetScope,
        checkpoint: Checkpoint,
        notified_routes: Sequence[str],
    ) -> None:
        if scope == "state":
            self.receipts.clear()
            self.notified_routes.clear()
            self.outbox.clear()
        self.checkpoint = checkpoint
        self.notified_routes.update(notified_routes)

    def collect(
        self,
        checkpoint: Checkpoint,
        deliveries: Mapping[str, Sequence[Delivery]],
        observed_at: datetime,
    ) -> None:
        self.checkpoint = checkpoint
        active_routes = set(deliveries)
        self.outbox = {
            route_id: queued
            for route_id, queued in self.outbox.items()
            if route_id in active_routes
        }
        for route_id, items in deliveries.items():
            queued = self.outbox.setdefault(route_id, [])
            known = {item.delivery.change.id for item in queued}
            for delivery in items:
                if (
                    delivery.change.id not in known
                    and (route_id, delivery.change.id) not in self.receipts
                ):
                    queued.append(QueuedDelivery(delivery, observed_at))

    def pending(self, route_id: str) -> tuple[QueuedDelivery, ...]:
        return tuple(self.outbox.get(route_id, ()))

    def acknowledge(self, route_id: str, change_ids: Sequence[str]) -> None:
        self.receipts.update((route_id, change_id) for change_id in change_ids)
        self._remove(route_id, change_ids)

    def discard(self, route_id: str, change_ids: Sequence[str]) -> None:
        self._remove(route_id, change_ids)

    def baseline_notified(self, route_id: str) -> bool:
        return route_id in self.notified_routes

    def record_baseline(self, route_id: str) -> None:
        self.notified_routes.add(route_id)

    def prune(self, retention_days: int) -> None:
        self.pruned.append(retention_days)

    def _remove(self, route_id: str, change_ids: Sequence[str]) -> None:
        removed = set(change_ids)
        self.outbox[route_id] = [
            item
            for item in self.outbox.get(route_id, ())
            if item.delivery.change.id not in removed
        ]


class FakeNotifier:
    def __init__(self, routes: tuple[FakeRoute, ...], fail_route: str = "") -> None:
        self._routes = routes
        self.fail_route = fail_route
        self.sent: list[tuple[str, list[Delivery]]] = []
        self.baselines: list[tuple[str, int, int]] = []

    @property
    def routes(self) -> tuple[FakeRoute, ...]:
        return self._routes

    def send(
        self,
        route_id: str,
        deliveries: Sequence[Delivery],
        acknowledge: Callable[[Sequence[Delivery]], None],
    ) -> None:
        self.sent.append((route_id, list(deliveries)))
        if route_id == self.fail_route:
            raise WatcherError("simulated delivery failure")
        acknowledge(deliveries)

    def render(
        self, route_id: str, deliveries: Sequence[Delivery]
    ) -> tuple[dict[str, JsonValue], ...]:
        return tuple(
            {"description": f"{route_id}: {item.change.id}"} for item in deliveries
        )

    def send_baseline(
        self, route_id: str, change_count: int, source_text_count: int
    ) -> None:
        if route_id == self.fail_route:
            raise WatcherError("simulated baseline failure")
        self.baselines.append((route_id, change_count, source_text_count))


def _policy(
    quiet_seconds: int = 0,
    max_wait_seconds: int = 0,
    *,
    latest_only: bool = True,
) -> DeliveryPolicy:
    return DeliveryPolicy(
        quiet_seconds,
        max_wait_seconds,
        latest_only,
        MergePolicy(enabled=True, threshold=5),
    )


def _make_monitor(
    source: FakeSource,
    state: MemoryState,
    notifier: FakeNotifier,
    now: datetime = RUN_AT,
) -> Monitor:
    return Monitor(
        source,
        state,
        notifier,
        retention_days=180,
        clock=lambda: now,
    )


class MonitorTests(unittest.TestCase):
    def test_routes_apply_their_delivery_windows_independently(self) -> None:
        changed = fixture_change(id="changed")
        routes = (
            FakeRoute("immediate", delivery=_policy()),
            FakeRoute("delayed", delivery=_policy(180, 900)),
        )
        state = MemoryState(Checkpoint("old", {}))

        first = FakeNotifier(routes)
        _make_monitor(
            FakeSource(PollResult((changed,), "next", 1), {changed.key: "English"}),
            state,
            first,
        ).run()

        self.assertEqual([route_id for route_id, _ in first.sent], ["immediate"])
        self.assertEqual(
            [item.delivery.change.id for item in state.pending("delayed")],
            ["changed"],
        )

        second = FakeNotifier(routes)
        _make_monitor(
            FakeSource(PollResult((), "later", 0), {}),
            state,
            second,
            RUN_AT + timedelta(seconds=180),
        ).run()

        self.assertEqual([route_id for route_id, _ in second.sent], ["delayed"])

    def test_quiet_window_delays_delivery_until_activity_stops(self) -> None:
        changed = fixture_change(id="changed")
        policy = _policy(180, 900)
        route = FakeRoute("main", delivery=policy)
        state = MemoryState(Checkpoint("old", {}))

        first = FakeNotifier((route,))
        _make_monitor(
            FakeSource(PollResult((changed,), "next", 1), {changed.key: "English"}),
            state,
            first,
        ).run()
        self.assertEqual(first.sent, [])

        second = FakeNotifier((route,))
        _make_monitor(
            FakeSource(PollResult((), "later", 0), {}),
            state,
            second,
            RUN_AT + timedelta(seconds=180),
        ).run()
        self.assertEqual([item.change.id for item in second.sent[0][1]], ["changed"])

    def test_maximum_wait_forces_delivery_during_continuous_activity(self) -> None:
        policy = _policy(180, 240)
        route = FakeRoute("main", delivery=policy)
        state = MemoryState(Checkpoint("old", {}))
        first = fixture_change(id="first", old_value="A", new_value="B")
        second = fixture_change(
            id="second",
            old_value="B",
            new_value="C",
            occurred_at=first.occurred_at + timedelta(seconds=120),
        )
        third = fixture_change(
            id="third",
            old_value="C",
            new_value="D",
            occurred_at=first.occurred_at + timedelta(seconds=220),
        )
        batches = (
            (RUN_AT, PollResult((first,), "one", 1)),
            (RUN_AT + timedelta(seconds=120), PollResult((second,), "two", 1)),
            (RUN_AT + timedelta(seconds=220), PollResult((third,), "three", 1)),
        )
        for now, batch in batches:
            notifier = FakeNotifier((route,))
            _make_monitor(
                FakeSource(batch, {first.key: "English"}),
                state,
                notifier,
                now,
            ).run()
            self.assertEqual(notifier.sent, [])

        notifier = FakeNotifier((route,))
        _make_monitor(
            FakeSource(PollResult((), "four", 0), {}),
            state,
            notifier,
            RUN_AT + timedelta(seconds=240),
        ).run()
        sent = notifier.sent[0][1][0]
        self.assertEqual((sent.change.old_value, sent.change.new_value), ("A", "D"))
        self.assertEqual(sent.change_ids, ("first", "second", "third"))

    def test_forced_run_flushes_an_immature_batch(self) -> None:
        changed = fixture_change(id="changed")
        policy = _policy(180, 900)
        route = FakeRoute("main", delivery=policy)
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier((route,))

        _make_monitor(
            FakeSource(PollResult((changed,), "next", 1), {changed.key: "English"}),
            state,
            notifier,
        ).run(flush=True)

        self.assertEqual([item.change.id for item in notifier.sent[0][1]], ["changed"])

    def test_changes_collected_across_runs_are_folded(self) -> None:
        policy = _policy(60, 600)
        route = FakeRoute("main", delivery=policy)
        state = MemoryState(Checkpoint("old", {}))
        first = fixture_change(id="first", old_value="A", new_value="B")
        second = fixture_change(
            id="second",
            old_value="B",
            new_value="C",
            occurred_at=first.occurred_at + timedelta(seconds=30),
        )

        _make_monitor(
            FakeSource(PollResult((first,), "one", 1), {first.key: "English"}),
            state,
            FakeNotifier((route,)),
        ).run()
        _make_monitor(
            FakeSource(PollResult((second,), "two", 1), {first.key: "English"}),
            state,
            FakeNotifier((route,)),
            RUN_AT + timedelta(seconds=30),
        ).run()
        notifier = FakeNotifier((route,))
        _make_monitor(
            FakeSource(PollResult((), "three", 0), {}),
            state,
            notifier,
            RUN_AT + timedelta(seconds=90),
        ).run()

        sent = notifier.sent[0][1][0]
        self.assertEqual((sent.change.old_value, sent.change.new_value), ("A", "C"))
        self.assertEqual(sent.change_ids, ("first", "second"))

    def test_folding_resolves_net_action_transitions(self) -> None:
        cases = (
            (
                fixture_change(
                    id="added", action="added", old_value=None, new_value="A"
                ),
                fixture_change(id="modified", old_value="A", new_value="B"),
                ("added", None, "B"),
            ),
            (
                fixture_change(id="modified", old_value="A", new_value="B"),
                fixture_change(
                    id="deleted", action="deleted", old_value="B", new_value=None
                ),
                ("deleted", "A", None),
            ),
            (
                fixture_change(
                    id="deleted", action="deleted", old_value="A", new_value=None
                ),
                fixture_change(
                    id="added", action="added", old_value=None, new_value="B"
                ),
                ("modified", "A", "B"),
            ),
        )
        for first, second, expected in cases:
            second = replace(
                second, occurred_at=first.occurred_at + timedelta(seconds=1)
            )
            with self.subTest(actions=(first.action, second.action)):
                source = FakeSource(
                    PollResult((first, second), "next", 2),
                    {first.key: "English"},
                )
                notifier = FakeNotifier((FakeRoute("main"),))
                _make_monitor(
                    source, MemoryState(Checkpoint("old", {})), notifier
                ).run()
                changed = notifier.sent[0][1][0].change
                self.assertEqual(
                    (changed.action, changed.old_value, changed.new_value), expected
                )

    def test_added_then_deleted_is_discarded_as_no_change(self) -> None:
        added = fixture_change(
            id="added", action="added", old_value=None, new_value="A"
        )
        deleted = fixture_change(
            id="deleted",
            action="deleted",
            old_value="A",
            new_value=None,
            occurred_at=added.occurred_at + timedelta(seconds=1),
        )
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier((FakeRoute("main"),))

        _make_monitor(
            FakeSource(PollResult((added, deleted), "next", 2), {added.key: "English"}),
            state,
            notifier,
        ).run()

        self.assertEqual(notifier.sent, [])
        self.assertEqual(state.pending("main"), ())

    def test_action_filter_is_applied_to_the_folded_change(self) -> None:
        added = fixture_change(
            id="added", action="added", old_value=None, new_value="A"
        )
        modified = fixture_change(
            id="modified",
            old_value="A",
            new_value="B",
            occurred_at=added.occurred_at + timedelta(seconds=1),
        )
        notifier = FakeNotifier((FakeRoute("main", actions=frozenset({"added"})),))

        _make_monitor(
            FakeSource(
                PollResult((added, modified), "next", 2), {added.key: "English"}
            ),
            MemoryState(Checkpoint("old", {})),
            notifier,
        ).run()

        self.assertEqual(notifier.sent[0][1][0].change.action, "added")

    def test_repeated_modifications_keep_only_the_latest_event(self) -> None:
        first = fixture_change(id="first", old_value="A", new_value="B")
        unrelated = fixture_change(
            id="unrelated",
            key="other.key",
            old_value="C",
            new_value="D",
            occurred_at=first.occurred_at + timedelta(minutes=1),
        )
        latest = fixture_change(
            id="latest",
            old_value="B",
            new_value="E",
            occurred_at=first.occurred_at + timedelta(minutes=2),
        )
        source = FakeSource(
            PollResult((first, unrelated, latest), "next", 3),
            {first.key: "English", unrelated.key: "Other"},
        )
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier((FakeRoute("main"),))

        _make_monitor(source, state, notifier).run()

        self.assertEqual(
            [item.change.id for item in notifier.sent[0][1]],
            ["unrelated", "latest"],
        )

    def test_repeated_modifications_can_be_delivered_individually(self) -> None:
        first = fixture_change(id="first", old_value="A", new_value="B")
        latest = fixture_change(
            id="latest",
            old_value="B",
            new_value="C",
            occurred_at=first.occurred_at + timedelta(minutes=1),
        )
        source = FakeSource(
            PollResult((first, latest), "next", 2), {first.key: "English"}
        )
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier(
            (FakeRoute("main", delivery=_policy(latest_only=False)),)
        )

        _make_monitor(source, state, notifier).run()

        self.assertEqual(
            [item.change.id for item in notifier.sent[0][1]],
            ["first", "latest"],
        )

    def test_individual_delivery_filters_actions_before_queueing(self) -> None:
        accepted = fixture_change(id="accepted")
        ignored = fixture_change(
            id="ignored",
            key="other.key",
            action="added",
            old_value=None,
            occurred_at=accepted.occurred_at + timedelta(seconds=1),
        )
        source = FakeSource(
            PollResult((accepted, ignored), "next", 2),
            {accepted.key: "English", ignored.key: "Other"},
        )
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier(
            (
                FakeRoute(
                    "main",
                    actions=frozenset({"modified"}),
                    delivery=_policy(180, 900, latest_only=False),
                ),
            )
        )

        _make_monitor(source, state, notifier).run()

        self.assertEqual(
            [item.delivery.change.id for item in state.pending("main")],
            ["accepted"],
        )

    def test_folding_keeps_locales_separate_and_resolves_actions(self) -> None:
        english = fixture_change(id="en", locale="en")
        chinese = fixture_change(id="zh")
        added = fixture_change(id="added", action="added", old_value=None)
        deleted = fixture_change(id="deleted", action="deleted", new_value=None)
        source = FakeSource(
            PollResult((english, chinese, added, deleted), "next", 4),
            {english.key: "English"},
        )
        state = MemoryState(Checkpoint("old", {}))
        notifier = FakeNotifier((FakeRoute("main"),))

        _make_monitor(source, state, notifier).run()

        self.assertEqual(
            [item.change.id for item in notifier.sent[0][1]],
            ["en", "deleted"],
        )
        self.assertEqual(
            notifier.sent[0][1][1].change_ids,
            ("zh", "added", "deleted"),
        )

    def test_baseline_uses_only_the_source_contract(self) -> None:
        source = FakeSource(
            PollResult((), {"opaque": "cursor"}, 125), {"key": "English"}
        )
        state = MemoryState()
        notifier = FakeNotifier((FakeRoute("first"), FakeRoute("second")))
        _make_monitor(source, state, notifier).run()
        self.assertEqual(source.polled_with, [None])
        self.assertEqual(
            notifier.baselines,
            [("first", 125, 1), ("second", 125, 1)],
        )
        self.assertEqual(
            state.checkpoint, Checkpoint({"opaque": "cursor"}, {"key": "English"})
        )
        self.assertIn("first", state.notified_routes)

    def test_source_must_include_texts_when_changes_are_returned(self) -> None:
        changed = fixture_change(id="changed")
        source = FakeSource(PollResult((changed,), "next", 1), None)
        state = MemoryState(Checkpoint("old", {}))

        with self.assertRaisesRegex(WatcherError, "did not include source texts"):
            _make_monitor(
                source,
                state,
                FakeNotifier((FakeRoute("main"),)),
            ).run()

    def test_baseline_failure_keeps_checkpoint_and_retries_only_missing_route(
        self,
    ) -> None:
        source = FakeSource(PollResult((), "cursor", 1), {"key": "English"})
        state = MemoryState()
        routes = (FakeRoute("first"), FakeRoute("second"))
        with self.assertRaisesRegex(WatcherError, "baseline"):
            _make_monitor(
                source, state, FakeNotifier(routes, fail_route="second")
            ).run()
        self.assertIsNone(state.checkpoint)
        self.assertIn("first", state.notified_routes)

        retry = FakeNotifier(routes)
        _make_monitor(source, state, retry).run()
        self.assertEqual(retry.baselines, [("second", 1, 1)])

    def test_new_route_receives_one_notice_after_the_global_baseline(self) -> None:
        checkpoint = Checkpoint("old", {"key": "English"})
        source = FakeSource(PollResult((), "next", 2), {"key": "unused"})
        state = MemoryState(checkpoint)
        state.notified_routes.add("existing")
        routes = (FakeRoute("existing"), FakeRoute("new"))

        first = FakeNotifier(routes)
        _make_monitor(source, state, first).run()
        self.assertEqual(first.baselines, [("new", 2, 1)])

        second = FakeNotifier(routes)
        _make_monitor(source, state, second).run()
        self.assertEqual(second.baselines, [])

    def test_changes_are_routed_without_exposing_source_cursor_shape(self) -> None:
        chinese = fixture_change(id="zh")
        english = fixture_change(
            id="en",
            locale="en",
            action="added",
            old_value=None,
            new_value="Example",
            occurred_at=chinese.occurred_at.replace(minute=8),
        )
        source = FakeSource(
            PollResult((english, chinese), {"api_token": "next"}, 2),
            {chinese.key: "Current English"},
        )
        state = MemoryState(Checkpoint({"api_token": "old"}, {chinese.key: "Previous"}))
        notifier = FakeNotifier(
            (
                FakeRoute("zh", frozenset({"zh-Hans"})),
                FakeRoute("all"),
                FakeRoute("deleted", actions=frozenset({"deleted"})),
            )
        )
        _make_monitor(source, state, notifier).run()
        self.assertEqual(source.polled_with, [{"api_token": "old"}])
        self.assertEqual(
            [
                (route_id, [item.change.id for item in deliveries])
                for route_id, deliveries in notifier.sent
            ],
            [("zh", ["zh"]), ("all", ["en", "zh"])],
        )
        self.assertEqual(
            [item.source_text for item in notifier.sent[1][1]],
            ["Example", "Current English"],
        )
        self.assertEqual(
            state.checkpoint,
            Checkpoint(
                {"api_token": "next"},
                {chinese.key: "Current English"},
            ),
        )

    def test_partial_failure_advances_checkpoint_and_retains_the_outbox(self) -> None:
        changed = fixture_change(id="changed")
        source = FakeSource(PollResult((changed,), "next", 1), {changed.key: "English"})
        initial = Checkpoint("old", {changed.key: "Previous"})
        state = MemoryState(initial)
        routes = (FakeRoute("first"), FakeRoute("second"))
        state.notified_routes.update(route.id for route in routes)
        with self.assertRaisesRegex(WatcherError, "simulated"):
            _make_monitor(
                source, state, FakeNotifier(routes, fail_route="second")
            ).run()
        self.assertEqual(
            state.checkpoint,
            Checkpoint("next", {changed.key: "English"}),
        )
        self.assertIn(("first", changed.id), state.receipts)
        self.assertNotIn(("second", changed.id), state.receipts)
        self.assertEqual(
            [item.delivery.change.id for item in state.pending("second")],
            [changed.id],
        )

        retry = FakeNotifier(routes)
        _make_monitor(source, state, retry).run()
        self.assertEqual(
            [
                (route_id, [item.change.id for item in deliveries])
                for route_id, deliveries in retry.sent
            ],
            [("second", [changed.id])],
        )
        self.assertEqual(
            state.checkpoint,
            Checkpoint("next", {changed.key: "English"}),
        )

    def test_dry_run_has_no_state_or_notification_side_effects(self) -> None:
        changed = fixture_change(id="changed")
        initial = Checkpoint("old", {changed.key: "Previous"})
        source = FakeSource(PollResult((changed,), "next", 1), {changed.key: "Current"})
        state = MemoryState(initial)
        notifier = FakeNotifier((FakeRoute("main"),))
        result = _make_monitor(source, state, notifier).run("dry-run")
        self.assertEqual(state.checkpoint, initial)
        self.assertEqual(state.receipts, set())
        self.assertEqual(notifier.sent, [])
        self.assertEqual(result[0]["route_id"], "main")

    def test_dry_run_reports_a_missing_baseline_without_creating_it(self) -> None:
        source = FakeSource(PollResult((), "cursor", 7), {"key": "English"})
        state = MemoryState()
        notifier = FakeNotifier((FakeRoute("main"),))

        result = _make_monitor(source, state, notifier).run("dry-run")

        self.assertEqual(
            result,
            ({"baseline": True, "scanned": 7, "source_texts": 1},),
        )
        self.assertIsNone(state.checkpoint)
        self.assertEqual(notifier.baselines, [])

    def test_reset_baseline_never_passes_the_stored_cursor_to_source(self) -> None:
        source = FakeSource(PollResult((), "fresh", 3), {})
        state = MemoryState(Checkpoint("old", {}))
        state.receipts.add(("main", "delivered"))
        notifier = FakeNotifier((FakeRoute("main"),))
        _make_monitor(source, state, notifier).reset("baseline")
        self.assertEqual(source.polled_with, [None])
        self.assertEqual(notifier.baselines, [])
        self.assertEqual(state.receipts, {("main", "delivered")})
        self.assertEqual(state.notified_routes, {"main"})
        self.assertEqual(state.checkpoint, Checkpoint("fresh", {}))

    def test_reset_baseline_can_force_a_new_notification(self) -> None:
        source = FakeSource(PollResult((), "fresh", 3), {})
        state = MemoryState(Checkpoint("old", {}))
        state.notified_routes.add("main")
        notifier = FakeNotifier((FakeRoute("main"),))

        _make_monitor(source, state, notifier).reset("baseline", notify=True)

        self.assertEqual(notifier.baselines, [("main", 3, 0)])
        self.assertEqual(state.checkpoint, Checkpoint("fresh", {}))

    def test_reset_state_clears_delivery_state(self) -> None:
        source = FakeSource(PollResult((), "fresh", 3), {})
        state = MemoryState(Checkpoint("old", {}))
        state.receipts.add(("old", "delivered"))
        state.notified_routes.add("old")
        state.outbox["old"] = [
            QueuedDelivery(Delivery(fixture_change(), "Source"), RUN_AT)
        ]
        notifier = FakeNotifier((FakeRoute("main"),))

        _make_monitor(source, state, notifier).reset("state")

        self.assertEqual(state.checkpoint, Checkpoint("fresh", {}))
        self.assertEqual(state.receipts, set())
        self.assertEqual(state.notified_routes, {"main"})
        self.assertEqual(state.outbox, {})

    def test_source_text_falls_back_to_the_previous_snapshot(self) -> None:
        changed = fixture_change(id="changed")
        source = FakeSource(PollResult((changed,), "next", 1), {})
        state = MemoryState(Checkpoint("old", {changed.key: "Previous English"}))
        notifier = FakeNotifier((FakeRoute("main"),))
        _make_monitor(source, state, notifier).run()
        self.assertEqual(notifier.sent[0][1][0].source_text, "Previous English")

    def test_unrouted_source_change_still_refreshes_the_source_snapshot(self) -> None:
        changed = fixture_change(
            id="source",
            locale="en",
            old_value="Old",
            new_value="New",
        )
        source = FakeSource(PollResult((changed,), "next", 1), {changed.key: "New"})
        state = MemoryState(Checkpoint("old", {changed.key: "Old"}))
        notifier = FakeNotifier((FakeRoute("target", frozenset({"zh-Hans"})),))

        _make_monitor(source, state, notifier).run()

        self.assertEqual(source.source_text_reads, 1)
        self.assertEqual(notifier.sent, [])
        self.assertEqual(state.checkpoint, Checkpoint("next", {changed.key: "New"}))

    def test_empty_poll_reuses_the_previous_source_snapshot(self) -> None:
        checkpoint = Checkpoint("old", {"key": "English"})
        source = FakeSource(PollResult((), "next", 0), {"key": "unused"})
        state = MemoryState(checkpoint)

        _make_monitor(source, state, FakeNotifier((FakeRoute("main"),))).run()

        self.assertEqual(source.source_text_reads, 0)
        self.assertEqual(state.checkpoint, Checkpoint("next", checkpoint.source_texts))
