"""Web adapter for the Noctalia translation platform."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ..models import (
    LOCALE_ID_PATTERN,
    SOURCE_LOCALE,
    Action,
    Change,
    JsonValue,
    PollResult,
    ValidationError,
    WatcherError,
)

_BASE_URL = "https://i18n.noctalia.dev"
_BASE_ORIGIN = urlparse(_BASE_URL)
_USER_AGENT = "Noctalia-i18n-Watcher/1.0.0 (unofficial)"
_CURSOR_TYPE = "noctalia-web"
# Initial cursors retain several pages of IDs without replaying their changes.
_BASELINE_PAGES = 5
_CURSOR_MATCH = 5

_JS_STRING = r'"(?:\\.|[^"\\])*"'
_JS_OPTIONAL_STRING = rf"(?:{_JS_STRING}|null)"
# Exact values live in Svelte data; the visible diff identifies the action.
_HYDRATED_CHANGE = re.compile(
    rf"\{{id:(?P<id>{_JS_STRING}),translation_id:{_JS_STRING},project_id:{_JS_STRING},"
    rf"translation_key:(?P<key>{_JS_STRING}),locale:(?P<locale>{_JS_STRING}),"
    rf"text:(?P<old>{_JS_OPTIONAL_STRING}),status:{_JS_STRING},"
    rf"created_by:{_JS_OPTIONAL_STRING},created_at:(?P<created_at>\d+),"
    rf"new_text:(?P<new>{_JS_OPTIONAL_STRING}),"
    rf"created_by_login:(?P<actor>{_JS_OPTIONAL_STRING})\}}"
)
_CURRENT_PAGE = re.compile(r"\bchangesPage:(\d+)")
_TOTAL_PAGES = re.compile(r"\bchangesTotalPages:(\d+)")
_ADDED_MARKERS = frozenset({"(new)", "new"})
_DELETED_MARKERS = frozenset({"(deleted)", "(removed)", "deleted", "removed"})


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Validated web-adapter settings."""

    project: str
    max_pages: int
    timeout: float
    proxy: str = ""

    @classmethod
    def from_config(
        cls,
        values: Mapping[str, JsonValue],
        *,
        project: str,
        timeout: float,
        proxy: str,
    ) -> WebSettings:
        """Validate adapter-owned options and attach shared HTTP settings."""

        fields = frozenset({"max_pages"})
        if missing := fields - values.keys():
            raise ValueError(
                f"source.options is missing required fields: {sorted(missing)}"
            )
        if unknown := values.keys() - fields:
            raise ValueError(
                f"source.options contains invalid web fields: {sorted(unknown)}"
            )
        max_pages = values["max_pages"]
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError(
                "source.options.max_pages must be an integer between 1 and 100"
            )
        return cls(
            project=project,
            max_pages=max_pages,
            timeout=timeout,
            proxy=proxy,
        )


@dataclass(frozen=True, slots=True)
class _PageChange:
    id: str
    key: str
    locale: str
    old_value: str | None
    new_value: str | None
    actor: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class _Page:
    changes: tuple[Change, ...]
    number: int
    total_pages: int


def _required_string(match: re.Match[str], group: str) -> str:
    value = json.loads(match.group(group))
    if not isinstance(value, str) or not value:
        raise WatcherError(f"Recent Changes contains an invalid {group}")
    return value


def _optional_string(match: re.Match[str], group: str) -> str | None:
    value = json.loads(match.group(group))
    if value is not None and not isinstance(value, str):
        raise WatcherError(f"Recent Changes contains an invalid {group}")
    return value


def _timestamp(milliseconds: str) -> datetime:
    try:
        seconds, remainder = divmod(int(milliseconds), 1000)
        return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)
    except (ValueError, OSError, OverflowError) as exc:
        raise WatcherError("Recent Changes contains an invalid created_at") from exc


def _page_changes(html: str) -> list[_PageChange]:
    changes: list[_PageChange] = []
    ids: set[str] = set()
    for match in _HYDRATED_CHANGE.finditer(html):
        change_id = _required_string(match, "id")
        if change_id in ids:
            raise WatcherError(
                f"Recent Changes contains a duplicate change ID: {change_id!r}"
            )
        ids.add(change_id)
        locale = _required_string(match, "locale")
        if not LOCALE_ID_PATTERN.fullmatch(locale):
            raise WatcherError(f"Recent Changes contains an invalid locale: {locale!r}")
        changes.append(
            _PageChange(
                id=change_id,
                key=_required_string(match, "key"),
                locale=locale,
                old_value=_optional_string(match, "old"),
                new_value=_optional_string(match, "new"),
                actor=_optional_string(match, "actor"),
                occurred_at=_timestamp(match.group("created_at")),
            )
        )
    return changes


def _page_number(html: str, pattern: re.Pattern[str], label: str) -> int:
    matches = pattern.findall(html)
    if len(matches) != 1:
        raise WatcherError(f"Recent Changes {label} is missing or ambiguous")
    return int(matches[0])


def _select_one(row: Tag, selector: str) -> Tag:
    nodes = row.select(selector)
    if len(nodes) != 1:
        raise WatcherError(f"Recent Changes row must contain one {selector!r} element")
    return nodes[0]


def _action(row: Tag) -> Action:
    old = _select_one(row, ".diff-old").get_text(" ", strip=True).lower()
    new = _select_one(row, ".diff-new").get_text(" ", strip=True).lower()
    if old in _ADDED_MARKERS:
        return "added"
    if new in _DELETED_MARKERS or not new:
        return "deleted"
    return "modified"


def _values(change: _PageChange, action: Action) -> tuple[str | None, str | None]:
    if action == "added":
        if change.old_value not in {None, ""} or change.new_value is None:
            raise WatcherError("Added row conflicts with the website data")
        return None, change.new_value
    if action == "deleted":
        if change.old_value is None or change.new_value not in {None, ""}:
            raise WatcherError("Deleted row conflicts with the website data")
        return change.old_value, None
    if change.old_value is None or change.new_value is None:
        raise WatcherError("Modified row conflicts with the website data")
    return change.old_value, change.new_value


def _github_urls(login: str | None) -> tuple[str | None, str | None]:
    if login is None:
        return None, None
    path = quote(login, safe="")
    return f"https://github.com/{path}", f"https://github.com/{path}.png"


def _parse_page(html: str, project: str) -> _Page:
    """Parse a page while requiring its DOM and Svelte data to agree."""

    soup = BeautifulSoup(html, "html.parser")
    page_changes = _page_changes(html)
    number = _page_number(html, _CURRENT_PAGE, "current page")
    total_pages = _page_number(html, _TOTAL_PAGES, "page count")
    rows = soup.select(".recent-changes .change-row")
    change_path = f"/projects/{project}/translate"
    links = tuple(_select_one(row, "a.change-key") for row in rows)
    empty = soup.select(".recent-changes .empty")
    if not page_changes:
        if (
            rows
            or links
            or len(empty) != 1
            or empty[0].get_text(" ", strip=True) != "No changes recorded yet."
        ):
            raise WatcherError("Recent Changes empty state is inconsistent")
        return _Page((), number, total_pages)
    if (
        empty
        or total_pages < 1
        or len(rows) != len(page_changes)
        or len(links) != len(rows)
    ):
        raise WatcherError("Recent Changes rows do not match the website data")

    output: list[Change] = []
    for row, link, data in zip(rows, links, page_changes, strict=True):
        url = urljoin(_BASE_URL + "/", str(link.get("href")))
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        keys = query.get("search", [])
        locales = query.get("locale", [])
        locale = _select_one(row, ".change-locale").get_text(" ", strip=True)
        actor = _select_one(row, ".change-user").get_text(" ", strip=True).lstrip("@")
        if (
            (parsed.scheme, parsed.netloc) != (_BASE_ORIGIN.scheme, _BASE_ORIGIN.netloc)
            or parsed.path != change_path
            or keys != [data.key]
            or locales != [data.locale]
            or locale != data.locale
            or not actor
        ):
            raise WatcherError(
                "Recent Changes row identity does not match the website data"
            )
        if data.actor is not None and actor != data.actor:
            raise WatcherError(
                "Recent Changes row actor does not match the website data"
            )
        action = _action(row)
        old, new = _values(data, action)
        actor_url, actor_avatar_url = _github_urls(data.actor)
        try:
            output.append(
                Change(
                    id=data.id,
                    key=data.key,
                    locale=data.locale,
                    actor=actor,
                    old_value=old,
                    new_value=new,
                    action=action,
                    occurred_at=data.occurred_at,
                    url=url,
                    actor_url=actor_url,
                    actor_avatar_url=actor_avatar_url,
                )
            )
        except ValueError as exc:
            raise WatcherError(
                f"Recent Changes contains an invalid record: {exc}"
            ) from exc
    return _Page(tuple(output), number, total_pages)


def _overlap(current: list[str], previous: list[str]) -> int | None:
    """Find the previous cursor using several adjacent IDs to avoid false matches."""

    required = min(_CURSOR_MATCH, len(previous))
    expected = previous[:required]
    for start in range(len(current) - required + 1):
        if current[start : start + required] == expected:
            return start
    return None


def _flatten(value: object, prefix: tuple[str, ...] = ()) -> dict[str, str]:
    if isinstance(value, dict):
        if not value:
            location = ".".join(prefix) or "<root>"
            raise WatcherError(
                f"Translation export contains an empty mapping at {location!r}"
            )
        output: dict[str, str] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise WatcherError("Translation export contains an invalid mapping key")
            flattened = _flatten(child, (*prefix, key))
            if duplicates := output.keys() & flattened.keys():
                duplicate = min(duplicates)
                raise WatcherError(
                    f"Translation export contains a duplicate key: {duplicate!r}"
                )
            output.update(flattened)
        return output
    if not isinstance(value, str):
        raise WatcherError(
            f"Translation export contains a non-string value at {'.'.join(prefix)!r}"
        )
    return {".".join(prefix): value}


def _request_error(resource: str, error: requests.RequestException) -> WatcherError:
    response = error.response
    detail = (
        f"HTTP {response.status_code}" if response is not None else "network failure"
    )
    return WatcherError(f"Cannot retrieve {resource}: {detail}")


class WebSource:
    """Acquire Noctalia changes from the translation platform website."""

    def __init__(
        self, settings: WebSettings, session: requests.Session | None = None
    ) -> None:
        self._settings: WebSettings = settings
        self._project_url = f"{_BASE_URL}/projects/{settings.project}"
        self._export_url = f"{_BASE_URL}/api/projects/{settings.project}/pull"
        self._session: requests.Session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"
                ),
            }
        )
        if settings.proxy:
            self._session.proxies.update(
                {"http": settings.proxy, "https": settings.proxy}
            )

    def poll(self, cursor: JsonValue | None, /) -> PollResult:
        """Collect changes newer than an opaque web cursor."""

        previous = None if cursor is None else self._parse_cursor(cursor)
        maximum = _BASELINE_PAGES if previous is None else self._settings.max_pages
        first = self._fetch_page(1)
        limit = min(max(first.total_pages, 1), maximum)
        changes: list[Change] = list(first.changes)
        ids = [change.id for change in first.changes]
        known = {change.id: change for change in first.changes}
        found = _overlap(ids, previous) if previous else None
        for page in range(2, limit + 1):
            if found is not None:
                break
            page_changes = self._fetch_page(page).changes
            # A concurrent insertion can repeat a stable record at the page boundary.
            for change in page_changes:
                existing = known.get(change.id)
                if existing is not None:
                    if existing != change:
                        raise WatcherError(
                            "Recent Changes pages contain a conflicting change ID"
                        )
                    continue
                known[change.id] = change
                changes.append(change)
                ids.append(change.id)
            if previous:
                found = _overlap(ids, previous)

        if previous is not None and not previous and first.total_pages <= maximum:
            found = len(changes)

        cursor_ids: list[JsonValue] = list(ids)
        next_cursor: dict[str, JsonValue] = {
            "type": _CURSOR_TYPE,
            "project": self._settings.project,
            "ids": cursor_ids,
        }
        if previous is None:
            return self._poll_result(
                (),
                next_cursor,
                len(changes),
                include_texts=True,
            )
        if found is None:
            raise WatcherError(
                f"Previous cursor was not found within {limit} pages. "
                "No state was advanced. "
                "Increase source.options.max_pages or reset the baseline."
            )
        collected = tuple(reversed(changes[:found]))
        return self._poll_result(
            collected,
            next_cursor,
            len(changes),
            include_texts=bool(collected),
        )

    def history(self, page: int, /) -> tuple[Change, ...]:
        """Fetch and parse one Recent Changes page."""

        if page < 1:
            raise ValueError("page number must be at least 1")
        return self._fetch_page(page).changes

    def close(self) -> None:
        self._session.close()

    def _fetch_page(self, number: int) -> _Page:
        try:
            response = self._session.get(
                self._project_url,
                params={"changesPage": number},
                timeout=self._settings.timeout,
            )
            response.raise_for_status()
            html = response.content.decode("utf-8")
        except requests.RequestException as exc:
            raise _request_error("Recent Changes", exc) from None
        except UnicodeDecodeError as exc:
            raise WatcherError("Recent Changes is not valid UTF-8") from exc
        parsed = _parse_page(html, self._settings.project)
        if parsed.number != number:
            raise WatcherError("Recent Changes returned an unexpected page")
        if (number <= parsed.total_pages) != bool(parsed.changes):
            raise WatcherError(
                "Recent Changes records do not match the reported page count"
            )
        return parsed

    def _fetch_source_texts(self) -> dict[str, str]:
        try:
            response = self._session.get(
                self._export_url,
                headers={"Accept": "application/json"},
                timeout=self._settings.timeout,
            )
            response.raise_for_status()
            payload: object = response.json()
        except requests.JSONDecodeError:
            raise WatcherError("Translation export is not valid JSON") from None
        except requests.RequestException as exc:
            raise _request_error("the translation export", exc) from None
        if not isinstance(payload, dict) or SOURCE_LOCALE not in payload:
            raise WatcherError(f"Translation export does not contain {SOURCE_LOCALE!r}")
        source = payload[SOURCE_LOCALE]
        if not isinstance(source, dict):
            raise WatcherError(f"Invalid translation data for {SOURCE_LOCALE!r}")
        return _flatten(source)

    def _poll_result(
        self,
        changes: tuple[Change, ...],
        cursor: dict[str, JsonValue],
        scanned: int,
        *,
        include_texts: bool,
    ) -> PollResult:
        try:
            return PollResult(
                changes=changes,
                cursor=cursor,
                scanned=scanned,
                source_texts=(self._fetch_source_texts() if include_texts else None),
            )
        except ValueError as exc:
            raise WatcherError(
                f"Recent Changes produced an invalid poll result: {exc}"
            ) from exc

    def _parse_cursor(self, cursor: JsonValue) -> list[str]:
        if not isinstance(cursor, dict) or cursor.get("type") != _CURSOR_TYPE:
            raise WatcherError("Stored cursor belongs to another source; reset state")
        if cursor.get("project") != self._settings.project:
            raise WatcherError(
                "Stored cursor belongs to another web project; reset state"
            )
        ids = cursor.get("ids")
        if not isinstance(ids, list):
            raise WatcherError("Stored web cursor is invalid")
        output: list[str] = []
        for item in ids:
            if not isinstance(item, str) or not item:
                raise WatcherError("Stored web cursor is invalid")
            output.append(item)
        if len(output) != len(set(output)):
            raise WatcherError("Stored web cursor is invalid")
        return output


def create(
    options: Mapping[str, JsonValue],
    *,
    project: str,
    timeout: float,
    proxy: str,
) -> WebSource:
    """Build the web source from its adapter options and shared HTTP settings."""

    try:
        settings = WebSettings.from_config(
            options,
            project=project,
            timeout=timeout,
            proxy=proxy,
        )
        return WebSource(settings)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
