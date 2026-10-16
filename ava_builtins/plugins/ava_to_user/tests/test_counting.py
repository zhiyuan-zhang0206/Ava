from __future__ import annotations

import pytest

from ava_builtins.plugins.ava_to_user.counting import count_panel_words

_CHINESE = "\u65e5\u5e38\u62a5\u544a\u5df2\u66f4\u65b0\u3002"
_ENGLISH = "The daily report is updated and ready for review."
_MIXED = "\u4eca\u5929\u5b8c\u6210\u4e86 3 \u9879\u4efb\u52a1, see report.md for details."


def test_pure_english_counts_by_word() -> None:
    assert count_panel_words(_ENGLISH) == 9


def test_pure_chinese_counts_by_character() -> None:
    assert count_panel_words(_CHINESE) == 7


def test_mixed_chinese_and_english_sums_both() -> None:
    assert count_panel_words(_MIXED) == 14


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("   \n\t  ", 0),
        ("..., --- !!!", 0),
        ("123456", 1),
        ("v2.1 shipped", 3),
        ("multi\nline\nwords", 3),
        ("\u3042\u3044\u3046\u3048\u304a", 5),
        ("\ud55c\uad6d\uc5b4", 3),
        ("caf\u00e9 r\u00e9sum\u00e9", 2),
        ("## heading\n\n- item one\n- item two", 5),
    ],
)
def test_edge_shapes(text: str, expected: int) -> None:
    assert count_panel_words(text) == expected
