"""Message rendering tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from noctalia_i18n_watcher.diff import AnsiStyle, format_ansi_diff
from noctalia_i18n_watcher.messages import MessageValues, load_merge, load_message

_OLD_STYLE = AnsiStyle("red", True, False)
_NEW_STYLE = AnsiStyle("green", True, False)


def _values(key: str, value: str) -> MessageValues:
    return MessageValues(
        monitor_id="monitor",
        project="noctalia",
        key=key,
        source="English",
        old_value="old",
        new_value=value,
        locale="zh-Hans",
        actor="translator",
        actor_url=None,
        actor_avatar_url=None,
        action="modified",
        change_url=None,
        timestamp="2026-07-30T08:00:00Z",
        unix_time=1785398400,
    )


class MessageTests(unittest.TestCase):
    def test_all_built_in_messages_are_valid(self) -> None:
        root = Path(__file__).parents[1] / "config" / "messages"
        for scope in ("source", "target"):
            directory = root / scope
            for path in directory.glob("*.yaml"):
                with self.subTest(scope=scope, name=path.stem):
                    load_message(path.stem, directory)
        directory = root / "merge"
        for path in directory.glob("*.yaml"):
            with self.subTest(scope="merge", name=path.stem):
                load_merge(path.stem, directory)

    def test_ansi_diff_preserves_dense_text_and_escapes_control_input(self) -> None:
        old, new = format_ansi_diff(
            "绘制边框",
            "绘制轮廓 ``` \x1b[31m",
            old_style=_OLD_STYLE,
            new_style=_NEW_STYLE,
        )

        self.assertIn("\x1b[1;31m边框", old)
        self.assertIn("\x1b[1;32m轮廓", new)
        self.assertIn("``\u200b`", new)
        self.assertIn("\\x1b[31m", new)

    def test_custom_messages_render_supported_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(
                "\n".join(
                    (
                        'added: {description: "{new_value:truncate=6}"}',
                        (
                            'modified: {description: "{new_value:truncate=6}", '
                            'url: "{change_url}"}'
                        ),
                        'deleted: {description: "{old_value:truncate=6}"}',
                    )
                ),
                encoding="utf-8",
            )
            message = load_message("custom", path.parent)

        values = _values("key", "abcdefgh")
        values["change_url"] = "https://example.com/change"
        embed = message.render("modified", values)

        self.assertEqual(embed.description, "abcde…")
        self.assertEqual(embed.url, "https://example.com/change")

    def test_merged_messages_split_without_losing_entries(self) -> None:
        directory = Path(__file__).parents[1] / "config" / "messages" / "merge"
        pages = load_merge("english", directory).render(
            (
                _values("first", "a" * 2100),
                _values("second", "b" * 2100),
            )
        )

        self.assertEqual([page.count for page in pages], [1, 1])

    def test_merged_author_links_a_single_actor_only(self) -> None:
        directory = Path(__file__).parents[1] / "config" / "messages" / "merge"
        merge = load_merge("english", directory)

        single = _values("first", "a")
        single["actor_url"] = "https://github.com/translator"
        single["actor_avatar_url"] = "https://github.com/translator.png"
        page = merge.render((single,))[0]
        self.assertEqual(page.embed.author.url, "https://github.com/translator")
        self.assertEqual(
            page.embed.author.icon_url,
            "https://github.com/translator.png",
        )

        first = _values("first", "a")
        second = _values("second", "b")
        second["actor"] = "editor"
        page = merge.render((first, second))[0]
        self.assertIsNone(page.embed.author.url)
        self.assertEqual(
            page.embed.author.icon_url,
            "https://github.com/noctalia-dev.png",
        )
