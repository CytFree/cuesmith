"""文本指标工具：去掉标记、拆行、字符计数。

CPS / 行宽都必须在"真正显示出来的字符"上计算，否则 <i> 或 {\\an8} 会被算进长度。
"""

from __future__ import annotations

from submatrix.textutil import char_count, split_text_lines, visible_text


def test_visible_text_strips_ass_override_tags() -> None:
    assert visible_text(r"{\an8}Hello") == "Hello"


def test_visible_text_strips_html_tags() -> None:
    assert visible_text("<i>Hello</i> <b>world</b>") == "Hello world"


def test_visible_text_turns_literal_backslash_n_into_newline() -> None:
    assert visible_text(r"Line one\NLine two") == "Line one\nLine two"


def test_visible_text_keeps_plain_text_untouched() -> None:
    assert visible_text("你好，世界") == "你好，世界"


def test_split_text_lines_strips_and_drops_blank_lines() -> None:
    assert split_text_lines("  A  \n\n\tB\t") == ["A", "B"]


def test_split_text_lines_splits_literal_newline_escape() -> None:
    assert split_text_lines(r"A\NB") == ["A", "B"]


def test_char_count_english_counts_spaces() -> None:
    assert char_count("Hello world", cjk=False) == 11


def test_char_count_english_ignores_line_breaks_and_edges() -> None:
    assert char_count("Hello\nworld ", cjk=False) == 10


def test_char_count_cjk_ignores_spaces() -> None:
    assert char_count("你好 世界", cjk=True) == 4


def test_char_count_ignores_markup() -> None:
    assert char_count(r"<i>Hello</i>", cjk=False) == 5
