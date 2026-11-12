"""译后精简（condense）：唯一真能降 CPS 的一层。

M4 已实测证伪"切条降 CPS"：CPS = 字数/时长，按字数比例分时间后每条 CPS 不变。
收费软件的做法是**重写压缩**，本模块分三档：rule（删口头禅）/ trim（截分句）/ llm（重写）。
"""

from __future__ import annotations

import pytest

from cuesmith.condense import CondenseResult, condense_text, estimate_max_chars


def test_estimate_max_chars_uses_cps_and_duration() -> None:
    assert estimate_max_chars(2000, 9) == 18
    assert estimate_max_chars(500, 9) == 4
    assert estimate_max_chars(0, 9) == 2  # 不返回 0，避免"压到空"
    assert estimate_max_chars(-100, 9) == 2


def test_within_limit_is_untouched() -> None:
    result = condense_text("这是一个很短的句子。", 20)

    assert result.action == "none"
    assert result.text == "这是一个很短的句子。"
    assert result.over_limit is False


def test_zero_limit_is_untouched() -> None:
    result = condense_text("内容", 0)

    assert result.action == "none"
    assert result.text == "内容"


def test_removes_sentence_initial_fillers() -> None:
    result = condense_text("那么其实这个功能非常有用。", 8)

    assert result.text == "这个功能非常有用。"
    assert result.action == "fillers"
    assert result.before_chars == 13
    assert result.after_chars == 9


def test_removes_tail_tag() -> None:
    result = condense_text("这个功能很有用，对吧？", 9)

    assert result.text == "这个功能很有用。"
    assert result.action == "fillers"


def test_filler_only_text_is_kept_as_is() -> None:
    # 删完只剩标点/空串时必须保留原文，不能交白卷
    result = condense_text("那么，其实。", 2)

    assert result.text == "那么，其实。"
    assert result.action == "unchanged"


def test_rule_mode_reports_still_over_limit() -> None:
    result = condense_text("这个功能非常有用而且速度也很快。", 5, mode="rule")

    assert result.over_limit is True
    assert result.after_chars > 5


def test_trim_mode_drops_trailing_clauses() -> None:
    result = condense_text(
        "这个功能非常有用，而且速度也很快，还能省下不少时间。", 10, mode="trim"
    )

    assert result.action == "trim"
    assert result.after_chars <= 10
    assert result.text.startswith("这个功能非常有用")


def test_trim_keeps_at_least_one_clause() -> None:
    result = condense_text("一个特别长的单句没有任何分句可以截断。", 5, mode="trim")

    assert result.text
    assert result.over_limit is True


def test_llm_rewriter_is_used_when_available() -> None:
    calls: list[tuple[str, int]] = []

    def rewriter(text: str, limit: int) -> str:
        calls.append((text, limit))
        return "精简版。"

    result = condense_text("那么其实这个功能非常有用。", 8, mode="llm", rewriter=rewriter)

    assert calls
    assert result.action == "llm"
    assert result.text == "精简版。"


def test_llm_failure_falls_back_to_rule() -> None:
    def broken(text: str, limit: int) -> str:
        raise RuntimeError("API down")

    result = condense_text("那么其实这个功能非常有用。", 8, mode="auto", rewriter=broken)

    assert result.action == "fillers"
    assert result.text == "这个功能非常有用。"


def test_llm_mode_without_rewriter_falls_back() -> None:
    result = condense_text("那么其实这个功能非常有用。", 8, mode="llm")

    assert result.action == "fillers"


def test_llm_rewrite_that_drops_a_sentence_is_rejected() -> None:
    """LLM 为了达字数直接删掉一整句时必须拒绝：宁可超限，不悄悄丢信息。"""

    def dropping(text: str, limit: int) -> str:
        return "这里有个主意。"

    result = condense_text(
        "这里有个主意。我可以做一个演示文稿，一个电子表格。",
        10,
        mode="llm",
        rewriter=dropping,
    )

    assert result.action == "unchanged"
    assert result.text.startswith("这里有个主意")
    assert result.over_limit is True


def test_llm_shortening_worse_than_original_is_rejected() -> None:
    def longer(text: str, limit: int) -> str:
        return text * 2

    result = condense_text("那么其实这个功能非常有用。", 8, mode="auto", rewriter=longer)

    assert result.action == "fillers"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="精简模式"):
        condense_text("内容足够长足够长", 2, mode="magic")


def test_trim_never_drops_a_glossary_term() -> None:
    """术语译法是硬约束：最后一个分句带术语时宁可超限，也不能截掉它。"""

    text = "这里有个主意。我可以做一个演示文稿，一个电子表格。"

    result = condense_text(text, 10, mode="trim", keep=["电子表格"])

    assert "电子表格" in result.text
    assert result.over_limit is True


def test_trim_drops_other_clauses_when_term_is_earlier() -> None:
    text = "这个电子表格很好用，还能离线运行，而且速度很快。"

    result = condense_text(text, 12, mode="trim", keep=["电子表格"])

    assert "电子表格" in result.text
    assert result.after_chars < result.before_chars


def test_llm_rewrite_that_drops_a_glossary_term_is_rejected() -> None:
    def dropping(text: str, limit: int) -> str:
        return "这里有个主意。"

    result = condense_text(
        "这里有个主意。我可以做一个演示文稿，一个电子表格。",
        10,
        mode="llm",
        rewriter=dropping,
        keep=["电子表格"],
    )

    assert result.action == "unchanged"
    assert "电子表格" in result.text


def test_llm_rewrite_keeping_term_is_accepted() -> None:
    def rewriting(text: str, limit: int) -> str:
        return "用电子表格做演示文稿。"

    result = condense_text(
        "我可以做一个演示文稿，用一个电子表格来做。",
        12,
        mode="auto",
        rewriter=rewriting,
        keep=["电子表格"],
    )

    assert result.action == "llm"
    assert "电子表格" in result.text


def test_saved_chars_helper() -> None:
    assert CondenseResult("x", "fillers", 10, 6, False).saved == 4


def test_english_fillers_are_removed_for_en_target() -> None:
    """en 目标时中文口头禅正则一个都匹配不上，必须走英文那套。"""

    result = condense_text(
        "You know, I mean, it's fine.", 20, mode="rule", cjk=False
    )

    assert result.text == "It's fine."
    assert result.action == "fillers"


def test_english_trim_uses_period() -> None:
    result = condense_text(
        "This is the first clause, and this is the second one.", 20,
        mode="trim", cjk=False,
    )

    assert result.text.endswith(".")
    assert result.after_chars < result.before_chars
