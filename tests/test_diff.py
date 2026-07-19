"""Multilingual Discord difference formatting tests."""

from __future__ import annotations

import unittest

from noctalia_i18n_watcher.diff import format_ansi_diff

from .fixtures import RECORDED_NEW_VALUE, RECORDED_OLD_VALUE

_RESET = "\x1b[0m"
_REMOVED = "\x1b[1;31m"
_ADDED = "\x1b[1;32m"


class DiffTests(unittest.TestCase):
    def test_dense_script_changes_are_highlighted_at_word_boundaries(self) -> None:
        old, new = format_ansi_diff(RECORDED_OLD_VALUE, RECORDED_NEW_VALUE)

        self.assertEqual(old, f"在 Toast 通知周围绘制{_REMOVED}边框{_RESET}")
        self.assertEqual(new, f"在 Toast 通知周围绘制{_ADDED}轮廓{_RESET}")

    def test_single_spaces_connect_adjacent_changed_tokens(self) -> None:
        old, new = format_ansi_diff("value; old phrase", "value: new phrase")

        self.assertEqual(old, f"value{_REMOVED}; old{_RESET} phrase")
        self.assertEqual(new, f"value{_ADDED}: new{_RESET} phrase")

    def test_canonical_unicode_and_emoji_clusters_remain_intact(self) -> None:
        old, new = format_ansi_diff("cafe\u0301 👩\u200d💻", "café 👩\u200d🔧")

        self.assertEqual(old, f"cafe\u0301 {_REMOVED}👩\u200d💻{_RESET}")
        self.assertEqual(new, f"café {_ADDED}👩\u200d🔧{_RESET}")

    def test_missing_side_marks_the_complete_existing_value(self) -> None:
        added = format_ansi_diff(None, "Added value")
        deleted = format_ansi_diff("Deleted value", None)

        self.assertEqual(added, ("", f"{_ADDED}Added value{_RESET}"))
        self.assertEqual(deleted, (f"{_REMOVED}Deleted value{_RESET}", ""))

    def test_upstream_text_cannot_inject_ansi_or_close_the_code_fence(self) -> None:
        _, new = format_ansi_diff(None, "```ansi \x1b[31m")

        self.assertEqual(
            new,
            f"{_ADDED}``\u200b`ansi \\x1b[31m{_RESET}",
        )
