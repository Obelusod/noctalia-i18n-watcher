"""Web source contract tests with deterministic HTTP responses."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from html import escape
from unittest.mock import MagicMock, patch

import requests

from noctalia_i18n_watcher.models import Change, ValidationError, WatcherError
from noctalia_i18n_watcher.sources.web import WebSettings, WebSource, create

from .contracts import PollScenario, assert_poll_contract
from .fixtures import (
    FIXTURE_SOURCE,
    RECENT_CHANGES_HTML,
    RECORDED_ACTOR,
    RECORDED_ID,
    RECORDED_KEY,
    RECORDED_NEW_VALUE,
    RECORDED_OLD_VALUE,
    hydration_html,
)

type _Record = tuple[str, str, str | None, int, str | None, str | None]


def _page(
    *records: _Record,
    project: str = "noctalia",
    page: int = 1,
    total_pages: int = 1,
) -> str:
    rows: list[str] = []
    for key, locale, actor, _created_at, old_value, new_value in records:
        old = "(new)" if old_value is None or old_value == "" else old_value
        new = "(deleted)" if new_value is None or new_value == "" else new_value
        rows.append(
            '<div class="change-row">'
            '<a class="change-key" '
            f'href="/projects/{project}/translate?search={escape(key)}'
            f'&amp;locale={escape(locale)}">'
            f"{escape(key)}</a>"
            f'<span class="change-locale">{escape(locale)}</span>'
            f'<span class="change-user">{escape(actor or "API")}</span>'
            '<span class="change-diff">'
            f'<span class="diff-old">{escape(old)}</span>'
            '<span class="diff-arrow">→</span>'
            f'<span class="diff-new">{escape(new)}</span>'
            "</span></div>"
        )
    html = (
        '<section class="recent-changes">'
        + ("".join(rows) if rows else '<p class="empty">No changes recorded yet.</p>')
        + "</section>"
        + hydration_html(
            *((f"{record[0]}@{record[3]}", *record) for record in records),
            page=page,
            total_pages=total_pages,
        )
    )
    return html


def _html_response(
    html: str,
    status: int = 200,
    *,
    project: str = "noctalia",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = html.encode()
    response.url = f"https://i18n.noctalia.dev/projects/{project}"
    return response


def _json_response(payload: object, *, project: str = "noctalia") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode()
    response.headers["Content-Type"] = "application/json"
    response.url = f"https://i18n.noctalia.dev/api/projects/{project}/pull"
    return response


def _session(*responses: requests.Response | requests.RequestException) -> MagicMock:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    session.proxies = {}
    session.get.side_effect = responses
    return session


def _source(
    *responses: requests.Response | requests.RequestException,
    project: str = "noctalia",
    max_pages: int = 5,
) -> tuple[WebSource, MagicMock]:
    session = _session(*responses)
    settings = WebSettings(project=project, timeout=30, max_pages=max_pages)
    return WebSource(settings, session), session


def _records(
    prefix: str, count: int, *, start: int = 1784300000000
) -> tuple[_Record, ...]:
    return tuple(
        (
            f"{prefix}.{index}",
            "de",
            "fixture-editor",
            start - index * 1000,
            f"old-{index}",
            f"new-{index}",
        )
        for index in range(count)
    )


def _baseline_responses(
    payload: object,
    records: tuple[_Record, ...] | None = None,
    *,
    project: str = "noctalia",
) -> tuple[requests.Response, ...]:
    baseline = records or _records("fixture.baseline", 5)
    pages = tuple(
        _html_response(
            _page(
                record,
                project=project,
                page=number,
                total_pages=len(baseline),
            ),
            project=project,
        )
        for number, record in enumerate(baseline, start=1)
    )
    return (*pages, _json_response(payload, project=project))


def _expected_change(record: _Record) -> Change:
    key, locale, actor, created_at, old_value, new_value = record
    action = (
        "added"
        if old_value in {None, ""}
        else "deleted"
        if new_value in {None, ""}
        else "modified"
    )
    return Change(
        id=f"{key}@{created_at}",
        key=key,
        locale=locale,
        actor=actor or "API",
        old_value=None if action == "added" else old_value,
        new_value=None if action == "deleted" else new_value,
        action=action,
        occurred_at=datetime.fromtimestamp(created_at / 1000, UTC),
        url=(
            f"https://i18n.noctalia.dev/projects/noctalia/translate?search={key}"
            f"&locale={locale}"
        ),
        actor_url=(f"https://github.com/{actor}" if actor is not None else None),
        actor_avatar_url=(
            f"https://github.com/{actor}.png" if actor is not None else None
        ),
    )


class WebSourceTests(unittest.TestCase):
    def test_page_parses_the_recorded_changes_without_rewriting_them(self) -> None:
        source, _ = _source(_html_response(RECENT_CHANGES_HTML))

        changes = source.history(1)

        self.assertEqual(len(changes), 2)
        self.assertEqual(
            [
                (
                    change.id,
                    change.key,
                    change.locale,
                    change.actor,
                    change.action,
                    change.old_value,
                    change.new_value,
                    change.iso_timestamp,
                )
                for change in changes
            ],
            [
                (
                    RECORDED_ID,
                    RECORDED_KEY,
                    "zh-Hans",
                    RECORDED_ACTOR,
                    "modified",
                    RECORDED_OLD_VALUE,
                    RECORDED_NEW_VALUE,
                    "2026-07-17T09:51:04.838Z",
                ),
                (
                    "60edf3d8-157e-438c-9c39-0ffbd7bb73a2",
                    RECORDED_KEY,
                    "de",
                    "notiant",
                    "added",
                    None,
                    "Zeichne einen Umriss um Benachrichtigungs-Toasts",
                    "2026-07-17T05:40:07.486Z",
                ),
            ],
        )

    def test_page_uses_exact_data_when_visible_text_is_shortened(self) -> None:
        record: _Record = (
            "fixture.long.description",
            "en",
            "fixture-editor",
            1782655740598,
            "A complete original value",
            "A complete modified value",
        )
        html = _page(record)
        html = html.replace("A complete original value", "original…", 1)
        html = html.replace("A complete modified value", "modified…", 1)
        source, _ = _source(_html_response(html))

        changed = source.history(1)[0]

        self.assertEqual(changed.old_value, "A complete original value")
        self.assertEqual(changed.new_value, "A complete modified value")

    def test_page_accepts_api_editors_and_new_project_locales(self) -> None:
        record: _Record = (
            "fixture.api.key",
            "sr-Latn-RS",
            None,
            1782655740000,
            "old",
            "new",
        )
        source, _ = _source(_html_response(_page(record)))

        changed = source.history(1)[0]

        self.assertEqual(changed.locale, "sr-Latn-RS")
        self.assertEqual(changed.actor, "API")
        self.assertIsNone(changed.actor_url)
        self.assertIsNone(changed.actor_avatar_url)

    def test_page_normalizes_a_fixture_deletion(self) -> None:
        source, _ = _source(
            _html_response(
                _page(
                    (
                        "fixture.deleted",
                        "de",
                        "fixture-editor",
                        1784300000000,
                        "Fixture former value",
                        None,
                    )
                )
            )
        )

        changed = source.history(1)[0]

        self.assertEqual(
            (changed.action, changed.old_value, changed.new_value),
            ("deleted", "Fixture former value", None),
        )

    def test_page_returns_an_empty_out_of_range_history_page(self) -> None:
        source, _ = _source(_html_response(_page(page=2, total_pages=1)))

        self.assertEqual(source.history(2), ())

    def test_page_rejects_incomplete_or_inconsistent_upstream_data(self) -> None:
        cases = (
            ("redesigned", "<html><body>redesigned</body></html>"),
            ("missing records", RECENT_CHANGES_HTML.split("<script>", maxsplit=1)[0]),
            (
                "mismatched key",
                RECENT_CHANGES_HTML.replace(
                    f'translation_key:"{RECORDED_KEY}"',
                    'translation_key:"different.key"',
                    1,
                ),
            ),
            (
                "foreign link",
                RECENT_CHANGES_HTML.replace(
                    'href="/projects/noctalia/translate?',
                    'href="https://example.invalid/projects/noctalia/translate?',
                    1,
                ),
            ),
            (
                "mismatched link locale",
                RECENT_CHANGES_HTML.replace(
                    "&amp;locale=zh-Hans",
                    "&amp;locale=fr",
                    1,
                ),
            ),
            (
                "duplicate change ID",
                RECENT_CHANGES_HTML.replace(
                    'id:"60edf3d8-157e-438c-9c39-0ffbd7bb73a2"',
                    f'id:"{RECORDED_ID}"',
                    1,
                ),
            ),
        )
        for name, html in cases:
            source, _ = _source(_html_response(html))
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(WatcherError, "Recent Changes"),
            ):
                source.history(1)

    def test_page_ignores_links_outside_recent_changes(self) -> None:
        html = (
            RECENT_CHANGES_HTML
            + '<a href="/projects/noctalia/translate?search=unrelated">unrelated</a>'
        )
        source, _ = _source(_html_response(html))

        self.assertEqual(len(source.history(1)), 2)

    def test_source_satisfies_the_shared_polling_contract(self) -> None:
        baseline = _records("fixture.baseline", 5)
        newer: _Record = (
            "fixture.newer",
            "de",
            "fixture-editor",
            1784300010000,
            "B",
            "C",
        )
        older: _Record = (
            "fixture.older",
            "de",
            "fixture-editor",
            1784300009000,
            "A",
            "B",
        )
        source_payload = {"en": {"fixture": {"source": {"text": FIXTURE_SOURCE}}}}
        responses = (
            *_baseline_responses(source_payload, baseline),
            _html_response(_page(newer, older, *baseline)),
            _json_response(source_payload),
            _html_response(_page(newer, older, *baseline)),
        )
        source, _ = _source(*responses)

        assert_poll_contract(
            self,
            source,
            PollScenario(
                source_texts={"fixture.source.text": FIXTURE_SOURCE},
                changes=(_expected_change(older), _expected_change(newer)),
                baseline_scanned=5,
                changed_scanned=7,
                unchanged_scanned=7,
            ),
        )

    def test_poll_tolerates_stable_duplicates_at_a_moving_page_boundary(
        self,
    ) -> None:
        newest, older = _records("fixture.moving", 2)
        source_payload = {"en": {"fixture": {"source": "Source"}}}
        source, _ = _source(
            _html_response(_page(newest, total_pages=2)),
            _html_response(_page(newest, older, page=2, total_pages=2)),
            _json_response(source_payload),
        )

        baseline = source.poll(None)

        self.assertEqual(baseline.scanned, 2)
        self.assertEqual(baseline.changes, ())

    def test_empty_history_establishes_a_cursor_and_collects_the_first_change(
        self,
    ) -> None:
        added: _Record = (
            "fixture.first",
            "fr",
            "fixture-editor",
            1784300010000,
            None,
            "Première traduction",
        )
        source_payload = {"en": {"fixture": {"first": "First translation"}}}
        source, _ = _source(
            _html_response(_page(total_pages=0)),
            _json_response(source_payload),
            _html_response(_page(added)),
            _json_response(source_payload),
            _html_response(_page(added)),
        )

        assert_poll_contract(
            self,
            source,
            PollScenario(
                source_texts={"fixture.first": "First translation"},
                changes=(_expected_change(added),),
                baseline_scanned=0,
                changed_scanned=1,
                unchanged_scanned=1,
            ),
        )

    def test_invalid_or_expired_cursor_never_rebuilds_the_baseline(self) -> None:
        source, _ = _source(*_baseline_responses({"en": {"key": "English"}}))
        with self.assertRaisesRegex(WatcherError, "another source"):
            source.poll({"foreign": "cursor"})

        cursor = source.poll(None).cursor
        other_project, _ = _source(project="official-plugins")
        with self.assertRaisesRegex(WatcherError, "another web project"):
            other_project.poll(cursor)

        responses = (
            *_baseline_responses({"en": {"key": "English"}}),
            _html_response(_page(*_records("new-page-one", 5), total_pages=2)),
            _html_response(_page(*_records("new-page-two", 5), page=2, total_pages=2)),
        )
        source, _ = _source(*responses, max_pages=2)
        cursor = source.poll(None).cursor
        with self.assertRaisesRegex(WatcherError, "not found within 2 pages"):
            source.poll(cursor)

    def test_poll_rejects_invalid_source_exports(self) -> None:
        cases = (
            ({"de": {}}, "does not contain 'en'"),
            ({"en": {"settings": {"enabled": False}}}, "non-string"),
            ({"en": {}}, "empty mapping"),
            ({"en": {"": "value"}}, "invalid mapping key"),
            (
                {"en": {"settings": {"label": "nested"}, "settings.label": "flat"}},
                "duplicate key",
            ),
        )
        for payload, error in cases:
            source, _ = _source(*_baseline_responses(payload))
            with self.subTest(error=error), self.assertRaisesRegex(WatcherError, error):
                source.poll(None)

        invalid_json = requests.Response()
        invalid_json.status_code = 200
        invalid_json._content = b"{"
        source, _ = _source(
            *(_html_response(_page(record)) for record in _records("base", 5)),
            invalid_json,
        )
        with self.assertRaisesRegex(WatcherError, "not valid JSON"):
            source.poll(None)

    def test_factory_validates_options_without_network_access(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.proxies = {}
        with patch(
            "noctalia_i18n_watcher.sources.web.requests.Session",
            return_value=session,
        ):
            source = create(
                {"max_pages": 20},
                project="official-plugins",
                timeout=30,
                proxy="",
            )

        session.get.assert_not_called()
        source.close()
        session.close.assert_called_once_with()

        invalid = (
            {},
            {"max_pages": 0},
            {"max_pages": 20, "endpoint": "unexpected"},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                create(options, project="noctalia", timeout=30, proxy="")

    def test_http_boundary_applies_proxy_timeout_and_page_number(self) -> None:
        proxy = "http://proxy.invalid:8080"
        project = "official-plugins"
        record = _records("fixture.project", 1)[0]
        session = _session(
            _html_response(
                _page(record, project=project),
                project=project,
            )
        )
        source = WebSource(
            WebSettings(
                project=project,
                timeout=30,
                max_pages=20,
                proxy=proxy,
            ),
            session,
        )

        source.history(1)

        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {"http": proxy, "https": proxy})
        session.get.assert_called_once_with(
            "https://i18n.noctalia.dev/projects/official-plugins",
            params={"changesPage": 1},
            timeout=30,
        )
        with self.assertRaisesRegex(ValueError, "at least 1"):
            source.history(0)

    def test_project_selects_its_export_endpoint(self) -> None:
        project = "community-plugins"
        responses = _baseline_responses(
            {"en": {"color_picker": {"label": "Color Picker"}}},
            project=project,
        )
        source, session = _source(*responses, project=project)

        result = source.poll(None)

        self.assertEqual(result.source_texts, {"color_picker.label": "Color Picker"})
        session.get.assert_called_with(
            "https://i18n.noctalia.dev/api/projects/community-plugins/pull",
            headers={"Accept": "application/json"},
            timeout=30,
        )

    def test_network_failures_are_stable_and_do_not_expose_credentials(self) -> None:
        error = requests.ConnectionError(
            "proxy http://user:SECRET@proxy.invalid failed"
        )
        source, _ = _source(error)

        with self.assertRaises(WatcherError) as raised:
            source.history(1)

        self.assertEqual(
            str(raised.exception),
            "Cannot retrieve Recent Changes: network failure",
        )
