"""Deterministic source and domain fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from noctalia_i18n_watcher.models import Change

ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "config.json"
MESSAGE_DIR = ROOT / "config" / "messages"
MERGE_MESSAGE_DIR = MESSAGE_DIR / "merge"
SOURCE_MESSAGE_DIR = MESSAGE_DIR / "source"
TARGET_MESSAGE_DIR = MESSAGE_DIR / "target"
RUN_AT = datetime(2026, 7, 17, 9, 51, 4, 838000, tzinfo=UTC)

RECORDED_ID = "fb14adc9-7b0b-4261-92c3-e9366ee7a1f1"
RECORDED_KEY = "settings.schema.notifications.border.description"
RECORDED_SOURCE = "Draw an outline around notification toasts"
RECORDED_OLD_VALUE = "在 Toast 通知周围绘制边框"
RECORDED_NEW_VALUE = "在 Toast 通知周围绘制轮廓"
RECORDED_ACTOR = "Obelusod"

FIXTURE_KEY = "fixture.translation.description"
FIXTURE_SOURCE = "Fixture source text"


def _quote(value: str | None) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def hydration_html(
    *changes: tuple[str, str, str, str | None, int, str | None, str | None],
    page: int = 1,
    total_pages: int = 1,
) -> str:
    """Build Svelte records matching the live Recent Changes data."""

    records = []
    for change_id, key, locale, actor, created_at, old_value, new_value in changes:
        records.append(
            f"{{id:{_quote(change_id)},"
            f'translation_id:"translation-{change_id}",project_id:"project",'
            f"translation_key:{_quote(key)},locale:{_quote(locale)},"
            f'text:{_quote(old_value)},status:"published",'
            f"created_by:{_quote('user' if actor else None)},created_at:{created_at},"
            f"new_text:{_quote(new_value)},created_by_login:{_quote(actor)}}}"
        )
    return (
        f"<script>recentChanges:[{','.join(records)}],changesPage:{page},"
        f"changesTotalPages:{total_pages}</script>"
    )


# Exact rows captured from Recent Changes page 3 on 2026-07-18.
RECENT_CHANGES_HTML = """
<section class="recent-changes">
  <h2>Recent Changes</h2>
  <div class="change-row">
    <span class="change-time" title="7/17/2026, 5:51:04 AM">6h ago</span>
    <a class="change-key"
       href="/projects/noctalia/translate?search=settings.schema.notifications.border.description">
      settings.schema.notifications.border.description
    </a>
    <span class="change-locale">zh-Hans</span><span class="change-user">Obelusod</span>
    <span class="change-diff">
      <span class="diff-old">在 Toast 通知周围绘制边框</span>
      <span>→</span><span class="diff-new">在 Toast 通知周围绘制轮廓</span>
    </span>
  </div>
  <div class="change-row">
    <span class="change-time" title="7/17/2026, 1:40:07 AM">10h ago</span>
    <a class="change-key"
       href="/projects/noctalia/translate?search=settings.schema.notifications.border.description">
      settings.schema.notifications.border.description
    </a>
    <span class="change-locale">de</span><span class="change-user">notiant</span>
    <span class="change-diff">
      <span class="diff-old">(new)</span><span>→</span>
      <span class="diff-new">Zeichne einen Umriss um Benachrichtigungs-Toasts</span>
    </span>
  </div>
</section>
""" + hydration_html(
    (
        RECORDED_ID,
        RECORDED_KEY,
        "zh-Hans",
        RECORDED_ACTOR,
        1784281864838,
        RECORDED_OLD_VALUE,
        RECORDED_NEW_VALUE,
    ),
    (
        "60edf3d8-157e-438c-9c39-0ffbd7bb73a2",
        RECORDED_KEY,
        "de",
        "notiant",
        1784266807486,
        None,
        "Zeichne einen Umriss um Benachrichtigungs-Toasts",
    ),
)


def recorded_change(**overrides: Any) -> Change:
    base = Change(
        id=RECORDED_ID,
        key=RECORDED_KEY,
        locale="zh-Hans",
        actor=RECORDED_ACTOR,
        old_value=RECORDED_OLD_VALUE,
        new_value=RECORDED_NEW_VALUE,
        action="modified",
        occurred_at=RUN_AT,
        url=(
            "https://i18n.noctalia.dev/projects/noctalia/translate?search="
            "settings.schema.notifications.border.description"
        ),
        actor_url="https://github.com/Obelusod",
        actor_avatar_url="https://github.com/Obelusod.png",
    )
    if "event_time" in overrides:
        value = str(overrides.pop("event_time"))
        overrides["occurred_at"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    occurred_at = overrides.get("occurred_at", base.occurred_at)
    overrides.setdefault(
        "id", f"{overrides.get('key', base.key)}@{occurred_at.isoformat()}"
    )
    return replace(base, **overrides)


def fixture_change(**overrides: Any) -> Change:
    """Build an explicitly synthetic change for behavior-only tests."""

    base = Change(
        id="fixture.translation.description@2026-07-17T09:51:04.838000+00:00",
        key=FIXTURE_KEY,
        locale="zh-Hans",
        actor="fixture-editor",
        old_value="Fixture original value",
        new_value="Fixture modified value",
        action="modified",
        occurred_at=RUN_AT,
        url="https://fixtures.invalid/changes/fixture.translation.description",
        actor_url=None,
        actor_avatar_url=None,
    )
    if "event_time" in overrides:
        value = str(overrides.pop("event_time"))
        overrides["occurred_at"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    occurred_at = overrides.get("occurred_at", base.occurred_at)
    overrides.setdefault(
        "id", f"{overrides.get('key', base.key)}@{occurred_at.isoformat()}"
    )
    return replace(base, **overrides)
