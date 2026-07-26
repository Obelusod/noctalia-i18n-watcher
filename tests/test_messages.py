"""YAML message loading and rendering tests."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

from noctalia_i18n_watcher.messages import MessageValues, load_merge, load_message
from noctalia_i18n_watcher.models import ACTIONS, Change, ValidationError

from .fixtures import (
    FIXTURE_KEY,
    FIXTURE_SOURCE,
    MERGE_MESSAGE_DIR,
    RECORDED_SOURCE,
    SOURCE_MESSAGE_DIR,
    TARGET_MESSAGE_DIR,
    fixture_change,
    recorded_change,
)


def _message_values(
    changed: Change,
    source: str,
    **overrides: Any,
) -> MessageValues:
    values: Any = MessageValues(
        monitor_id="noctalia",
        project="noctalia",
        key=changed.key,
        source=source,
        old_value=changed.old_value,
        new_value=changed.new_value,
        locale=changed.locale,
        actor=changed.actor,
        actor_url=changed.actor_url,
        actor_avatar_url=changed.actor_avatar_url,
        action=changed.action,
        change_url=changed.url,
        timestamp=changed.iso_timestamp,
        unix_time=int(changed.occurred_at.timestamp()),
    )
    values.update(overrides)
    return values


def _recorded_values(**overrides: Any) -> MessageValues:
    return _message_values(recorded_change(), RECORDED_SOURCE, **overrides)


def _fixture_values(**overrides: Any) -> MessageValues:
    return _message_values(fixture_change(), FIXTURE_SOURCE, **overrides)


class MessageTests(unittest.TestCase):
    @staticmethod
    def _write_message(root: Path, name: str) -> Path:
        path = root / f"{name}.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                added:
                  description: "{key}: {new_value}"
                modified:
                  description: "{key}: {old_value} -> {new_value}"
                deleted:
                  description: "{key}: {old_value}"
                """
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _write_merge(root: Path, name: str) -> Path:
        path = root / f"{name}.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                source:
                  description: "{entries}"
                target:
                  description: "{entries}"
                entries:
                  separator: "\\n"
                  added: "{key}: {new_value}"
                  modified: "{key}: {old_value} -> {new_value}"
                  deleted: "{key}: {old_value}"
                """
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _replace_action(path: Path, action: str, value: object) -> None:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload[action] = value
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _set_diff_styles(
        path: Path,
        *,
        old: dict[str, object],
        new: dict[str, object],
    ) -> None:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["diff"] = {"old": old, "new": new}
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def test_messages_can_place_derived_ansi_differences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "ansi")
            self._replace_action(
                path,
                "modified",
                {"description": "{old_diff}\n{new_diff}"},
            )
            self._set_diff_styles(
                path,
                old={"color": "red", "bold": True, "underline": False},
                new={"color": "green", "bold": True, "underline": False},
            )
            message = load_message("ansi", Path(directory))

        embed = message.render("modified", _recorded_values())

        self.assertEqual(
            embed.get("description"),
            (
                "在 Toast 通知周围绘制\x1b[1;31m边框\x1b[0m\n"
                "在 Toast 通知周围绘制\x1b[1;32m轮廓\x1b[0m"
            ),
        )

    def test_repository_message_files_are_valid(self) -> None:
        values: dict[str, object] = dict(_fixture_values())
        for directory in (SOURCE_MESSAGE_DIR, TARGET_MESSAGE_DIR):
            for path in sorted(directory.glob("*.yaml")):
                message = load_message(path.stem, directory)
                for action in ACTIONS:
                    with self.subTest(
                        scope=directory.name,
                        message=path.stem,
                        action=action,
                    ):
                        embed = message.render(action, values | {"action": action})
                        self.assertTrue(embed)

    def test_compact_modified_link_has_a_stable_markdown_boundary(self) -> None:
        expected = (
            f"**[`{FIXTURE_KEY}`](https://fixtures.invalid/changes/{FIXTURE_KEY})**"
        )
        for directory in (SOURCE_MESSAGE_DIR, TARGET_MESSAGE_DIR):
            with self.subTest(scope=directory.name):
                embed = load_message("compact", directory).render(
                    "modified",
                    _fixture_values(),
                )

                self.assertEqual(
                    embed.get("description", "").splitlines()[0],
                    expected,
                )

    def test_empty_detail_urls_are_omitted_and_avatar_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "optional-urls")
            self._replace_action(
                path,
                "modified",
                {
                    "url": "{change_url}",
                    "author": {
                        "name": "{actor}",
                        "url": "{actor_url}",
                        "icon_url": (
                            "{actor_avatar_url:"
                            "fallback=https://fixtures.invalid/fallback.png}"
                        ),
                    },
                    "fields": [
                        {
                            "name": "key",
                            "value": "{key_link}",
                            "inline": False,
                        }
                    ],
                },
            )
            message = load_message("optional-urls", Path(directory))

        embed = message.render(
            "modified",
            _fixture_values(change_url="", actor_url="", actor_avatar_url=""),
        )

        self.assertNotIn("url", embed)
        self.assertEqual(
            embed.get("fields", [])[0]["value"],
            f"`{FIXTURE_KEY}`",
        )
        self.assertEqual(
            embed.get("author"),
            {
                "name": "fixture-editor",
                "icon_url": "https://fixtures.invalid/fallback.png",
            },
        )

    def test_repository_merge_messages_are_valid(self) -> None:
        values = tuple(
            _fixture_values(
                key=f"fixture.entry.{index}",
                change_url=f"https://fixtures.invalid/changes/{index}",
                actor="fixture-editor" if index < 2 else "fixture-reviewer",
                actor_url=f"https://fixtures.invalid/users/{index}",
                action=action,
                old_value=None if action == "added" else f"Fixture old value {index}",
                new_value=None if action == "deleted" else f"Fixture new value {index}",
                timestamp=f"2026-06-28T14:09:0{index}Z",
                unix_time=1782655740 + index,
            )
            for index, action in enumerate(ACTIONS)
        )
        for path in sorted(MERGE_MESSAGE_DIR.glob("*.yaml")):
            with self.subTest(message=path.stem):
                pages = load_merge(path.stem, MERGE_MESSAGE_DIR).render(values)

                self.assertEqual(sum(page.count for page in pages), len(values))

    def test_merge_author_uses_one_avatar_or_the_configured_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_merge(Path(directory), "authors")
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            author = {
                "name": "{actors}",
                "icon_url": (
                    "{actor_avatar_url:fallback=https://fixtures.invalid/fallback.png}"
                ),
            }
            payload["source"]["author"] = author
            payload["target"]["author"] = author
            path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            message = load_merge("authors", Path(directory))

        single = message.render(
            (_fixture_values(actor_avatar_url="https://fixtures.invalid/editor.png"),)
        )
        multiple = message.render(
            (
                _fixture_values(actor="first"),
                _fixture_values(actor="second"),
            )
        )

        self.assertEqual(
            single[0].embed.get("author", {}).get("icon_url"),
            "https://fixtures.invalid/editor.png",
        )
        self.assertEqual(
            multiple[0].embed.get("author", {}).get("icon_url"),
            "https://fixtures.invalid/fallback.png",
        )

    def test_message_diff_styles_control_color_and_emphasis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_merge(Path(directory), "custom")
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            payload["entries"]["modified"] = "{old_diff}\n{new_diff}"
            path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            self._set_diff_styles(
                path,
                old={"color": "yellow", "bold": False, "underline": True},
                new={"color": "blue", "bold": False, "underline": False},
            )
            message = load_merge("custom", Path(directory))

        description = message.render((_recorded_values(),))[0].embed.get(
            "description", ""
        )

        self.assertIn("\x1b[4;33m边框\x1b[0m", description)
        self.assertIn("\x1b[34m轮廓\x1b[0m", description)

    def test_merge_splits_at_embed_limits_without_omitting_entries(self) -> None:
        values = tuple(
            _fixture_values(
                key=f"fixture.entry.{index}",
                old_value=f"old-{index}-" + "A" * 1400,
                new_value=f"new-{index}-" + "B" * 1400,
                unix_time=1782655740 + index,
            )
            for index in range(4)
        )

        with tempfile.TemporaryDirectory() as directory:
            self._write_merge(Path(directory), "sized")
            message = load_merge("sized", Path(directory))

        pages = message.render(values)
        rendered = "\n".join(page.embed.get("description", "") for page in pages)

        self.assertGreater(len(pages), 1)
        self.assertEqual(sum(page.count for page in pages), len(values))
        for index in range(4):
            self.assertEqual(rendered.count(f"fixture.entry.{index}"), 1)

    def test_merge_rejects_mixed_locales(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_merge(Path(directory), "locales")
            message = load_merge("locales", Path(directory))

        with self.assertRaisesRegex(ValueError, "mix locales"):
            message.render((_fixture_values(locale="de"), _fixture_values(locale="fr")))

    def test_merge_rejects_mixed_monitor_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_merge(Path(directory), "contexts")
            message = load_merge("contexts", Path(directory))

        with self.assertRaisesRegex(ValueError, "mix monitor contexts"):
            message.render(
                (
                    _fixture_values(),
                    _fixture_values(
                        monitor_id="plugins",
                        project="community-plugins",
                    ),
                )
            )

    def test_arbitrarily_named_custom_message_is_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "release-notes")
            self._replace_action(
                path,
                "added",
                {
                    "title": "{monitor_id} · {project}",
                    "description": "{new_value}\n{key}",
                },
            )
            message = load_message("release-notes", Path(directory))

        self.assertEqual(
            message.render(
                "added",
                {
                    "monitor_id": "plugins",
                    "project": "community-plugins",
                    "new_value": "value",
                    "key": "key",
                },
            ),
            {
                "title": "plugins · community-plugins",
                "description": "value\nkey",
            },
        )

    def test_unknown_or_unsafe_message_name_is_rejected(self) -> None:
        for name in ("missing", "../english"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValidationError, "[Mm]essage"),
            ):
                load_message(name, TARGET_MESSAGE_DIR)
            with (
                self.subTest(merge=name),
                self.assertRaisesRegex(ValidationError, "[Mm]erge|[Mm]essage"),
            ):
                load_merge(name, MERGE_MESSAGE_DIR)

    def test_text_placeholders_support_optional_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "truncated")
            self._replace_action(
                path,
                "added",
                {"description": "{key:truncate=8} | {new_value:truncate=1}"},
            )
            message = load_message("truncated", Path(directory))

        self.assertEqual(
            message.render("added", {"key": "short", "new_value": "value"}),
            {"description": "short | …"},
        )
        self.assertEqual(
            message.render("added", {"key": "fixture.entry", "new_value": ""}),
            {"description": "fixture… | "},
        )

    def test_truncation_accounts_for_wide_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "wide")
            self._replace_action(
                path,
                "added",
                {"description": "{new_value:truncate=5}"},
            )
            message = load_message("wide", Path(directory))

        self.assertEqual(
            message.render("added", {"new_value": "中文测试"}),
            {"description": "中文…"},
        )

    def test_merge_embed_and_entries_support_optional_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_merge(Path(directory), "truncated")
            path.write_text(
                """source:
  description: |-
    {actors:truncate=6}
    {entries}
target:
  description: |-
    {actors:truncate=6}
    {entries}
entries:
  separator: ""
  added: "{new_value:truncate=6}"
  modified: "{new_value:truncate=6}"
  deleted: "{old_value:truncate=6}"
""",
                encoding="utf-8",
            )
            message = load_merge("truncated", Path(directory))

        result = message.render(
            (
                _fixture_values(
                    action="added",
                    old_value=None,
                    new_value="Translated value",
                ),
            )
        )
        self.assertEqual(result[0].embed, {"description": "fixtu…\nTrans…"})

    def test_invalid_yaml_embed_fails_before_delivery(self) -> None:
        cases = (
            ("unknown placeholder", {"title": "{unknown}"}),
            ("unsupported format", {"title": "{new_value:shorten=10}"}),
            ("zero truncation", {"title": "{new_value:truncate=0}"}),
            ("oversized truncation", {"title": "{new_value:truncate=6001}"}),
            ("numeric truncation", {"title": "{unix_time:truncate=10}"}),
            ("numeric fallback", {"title": "{unix_time:fallback=unknown}"}),
            ("nested fallback", {"title": "{actor:fallback={unknown}}"}),
            ("unknown Discord field", {"heading": "text"}),
            ("invalid structure", {"fields": "text"}),
            ("templated color", {"color": "{unix_time}"}),
            ("empty title", {"title": ""}),
        )
        for case, template in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = self._write_message(Path(directory), "invalid")
                self._replace_action(path, "modified", template)
                with self.assertRaisesRegex(ValidationError, "message 'invalid'"):
                    load_message("invalid", Path(directory))

    def test_duplicate_yaml_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.yaml"
            path.write_text(
                textwrap.dedent(
                    """\
                    added:
                      title: one
                      title: two
                    modified:
                      title: modified
                    deleted:
                      title: deleted
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "duplicate key"):
                load_message("invalid", Path(directory))

    def test_missing_action_fails_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_message(Path(directory), "incomplete")
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            del payload["deleted"]
            path.write_text(yaml.safe_dump(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "deleted"):
                load_message("incomplete", Path(directory))

    def test_invalid_merge_fails_before_delivery(self) -> None:
        cases = (
            (
                "unknown merge placeholder",
                """source:
  title: "{unknown}"
target:
  title: "{count}"
entries:
  separator: ""
  added: "{key}"
  modified: "{key}"
  deleted: "{key}"
""",
            ),
            (
                "missing action entry",
                """source:
  title: "{count}"
target:
  title: "{count}"
entries:
  separator: ""
  added: "{key}"
  modified: "{key}"
""",
            ),
            (
                "missing diff styles",
                """source:
  title: "{count}"
target:
  title: "{count}"
entries:
  separator: ""
  added: "{key}"
  modified: "{old_diff}"
  deleted: "{key}"
""",
            ),
            (
                "invalid diff color",
                """source:
  title: "{count}"
target:
  title: "{count}"
diff:
  old: {color: orange, bold: true, underline: false}
  new: {color: green, bold: true, underline: false}
entries:
  separator: ""
  added: "{key}"
  modified: "{old_diff}"
  deleted: "{key}"
""",
            ),
        )
        for case, template in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = self._write_merge(Path(directory), "invalid")
                path.write_text(template, encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "merge"):
                    load_merge("invalid", Path(directory))


if __name__ == "__main__":
    unittest.main()
