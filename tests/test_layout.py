"""字幕排版（第 ④ 层）：自动折行 + 超行切条。

对应 en→zh 最缺的一块：翻译出来的句子往往 80–120 字符，直接挂到屏幕上就是 4–6 行。
收费字幕软件（EZTitles / Ooona / Subtitle Edit）都做三件事，这里对齐：
1. **按宽度折行**（英文按词、中文按字，优先在标点处断）
2. **单条 ≤2 行**：超过就把这条按比例切成多条（时间轴按文字长度分配）
3. **最短时长保护**（切的太碎没法读）

注意：这是**显式开启的可选改写**（`build --wrap`），默认行为仍是"只报不改"（PRD FR-2）。
"""

from __future__ import annotations

import pytest

from cuesmith.layout import (
    FUNCTION_WORDS,
    LayoutOptions,
    layout_pair,
    wrap_text,
)
from cuesmith.srt import Cue, Subtitle, Timing


def sub(*pairs: tuple[int, int, str]) -> Subtitle:
    return Subtitle(
        cues=tuple(Cue(timing=Timing(start, end), text=text) for start, end, text in pairs)
    )


# ---------------------------------------------------------------- 折行


def test_wrap_english_respects_width_and_word_boundaries() -> None:
    text = "the quick brown fox jumps over the lazy dog and keeps running far away"

    lines = wrap_text(text, 30)

    assert all(len(line) <= 30 for line in lines)
    assert " ".join(lines) == text  # 不丢词、不改词


def test_wrap_english_does_not_break_words() -> None:
    lines = wrap_text("extraordinarily complicated sentence structure here", 20)

    for line in lines:
        assert not line.endswith("-")
        assert all(word in "extraordinarily complicated sentence structure here".split() for word in line.split())


def test_wrap_chinese_counts_characters() -> None:
    text = "这是一句比较长的中文字幕内容需要按每行十六个字符折行显示"

    lines = wrap_text(text, 16)

    assert all(len(line) <= 16 for line in lines)
    assert "".join(lines) == text


def test_wrap_prefers_punctuation_break() -> None:
    text = "第一句话到这里结束，第二句话从这里开始"

    lines = wrap_text(text, 12)

    assert len(lines) == 2
    assert lines[0].endswith("，")  # 在逗号后断，而不是硬切在第 12 个字


def test_wrap_short_text_stays_single_line() -> None:
    assert wrap_text("short", 40) == ["short"]


def test_wrap_empty_text() -> None:
    assert wrap_text("   ", 40) == []


def test_wrap_flattens_existing_newlines() -> None:
    assert wrap_text("line one\nline two", 40) == ["line one line two"]


# ---------------------------------------------------------------- 切条


def test_long_cue_is_split_so_each_fits_two_lines() -> None:
    src = sub((0, 6000, "This is a fairly long English sentence that definitely needs more than two lines of subtitles."))
    tgt = sub((0, 6000, "这是一句相当长的中文字幕内容，肯定需要超过两行的空间来完整显示，所以要切成多条。"))

    laid_src, laid_tgt, stats = layout_pair(
        src, tgt, options=LayoutOptions(src_width=42, tgt_width=16, max_lines=2)
    )

    assert len(laid_src) == len(laid_tgt) == stats.cues_after > 1
    for cue in laid_src.cues:
        assert len(cue.text.split("\n")) <= 2
        assert max(len(line) for line in cue.text.split("\n")) <= 42
    for cue in laid_tgt.cues:
        assert len(cue.text.split("\n")) <= 2


def test_split_preserves_total_timing_and_has_no_gaps() -> None:
    src = sub((1000, 7000, "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"))
    tgt = sub((1000, 7000, "一二三四五六七八九十十一十二十三十四十五十六十七十八"))

    laid_src, laid_tgt, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=30, tgt_width=10, max_lines=2)
    )

    assert laid_src.cues[0].timing.start_ms == 1000
    assert laid_src.cues[-1].timing.end_ms == 7000
    assert [cue.timing for cue in laid_src.cues] == [cue.timing for cue in laid_tgt.cues]
    for earlier, later in zip(laid_src.cues, laid_src.cues[1:]):
        assert later.timing.start_ms == earlier.timing.end_ms


def test_split_text_is_preserved_without_loss() -> None:
    src = sub((0, 4000, "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"))
    tgt = sub((0, 4000, "一二三四五六七八九十十一十二十三十四十五十六"))

    laid_src, _, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=20, tgt_width=8, max_lines=1)
    )

    joined = " ".join(cue.text.replace("\n", " ") for cue in laid_src.cues)
    assert joined.split() == "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()


def test_short_cues_are_not_split() -> None:
    src = sub((0, 2000, "Short one."))
    tgt = sub((0, 2000, "很短。"))

    laid_src, laid_tgt, stats = layout_pair(
        src, tgt, options=LayoutOptions(src_width=42, tgt_width=16, max_lines=2)
    )

    assert stats.cues_after == stats.cues_before == 1
    assert laid_src.cues[0].text == "Short one."


def test_min_duration_is_respected() -> None:
    src = sub((0, 3000, "word " * 40))
    tgt = sub((0, 3000, "字" * 100))

    laid, _, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=30, tgt_width=10, max_lines=1, min_duration_ms=800)
    )

    assert all(cue.timing.duration_ms >= 800 for cue in laid.cues)


def test_layout_requires_same_cue_count() -> None:
    with pytest.raises(ValueError, match="条数"):
        layout_pair(
            sub((0, 1000, "a")),
            sub((0, 1000, "b"), (1000, 2000, "c")),
            options=LayoutOptions(),
        )


def test_layout_stats_counts_split_cues() -> None:
    src = sub((0, 4000, "a" * 120), (4000, 6000, "short"))
    tgt = sub((0, 4000, "字" * 60), (4000, 6000, "短"))

    _, _, stats = layout_pair(
        src, tgt, options=LayoutOptions(src_width=30, tgt_width=10, max_lines=2)
    )

    assert stats.cues_before == 2
    assert stats.split_cues == 1
    assert stats.cues_after > stats.cues_before


def test_splitting_does_not_reduce_cps() -> None:
    """切条只解决"行数/行宽"，救不了 CPS——CPS 是字数/时长，切多少份比例都一样。

    这条测试是刻意的"负向说明"：CPS 超标只能靠精简译文或延长时间解决（见 M4 文档）。
    """

    src = sub((0, 6000, "short english line"))
    tgt = sub((0, 6000, "字" * 60))

    _, laid_tgt, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=42, tgt_width=16, max_lines=1)
    )

    before_cps = 60 / 6.0
    after_cps = [
        len(cue.text.replace("\n", "")) / (cue.timing.duration_ms / 1000) for cue in laid_tgt.cues
    ]
    assert all(abs(cps - before_cps) < 0.5 for cps in after_cps)


def test_split_prefers_sentence_boundaries() -> None:
    """切条优先落在句号边界，而不是从句子中间切开（收费字幕软件都这么做）。"""

    src = sub((0, 8000, "First one. Second one. Third one. Fourth one."))
    tgt = sub((0, 8000, "第一句内容。第二句内容。第三句内容。第四句内容。"))

    _, laid_tgt, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=12, tgt_width=6, max_lines=1, min_duration_ms=100)
    )

    assert len(laid_tgt) == 4
    assert [cue.text for cue in laid_tgt.cues] == [
        "第一句内容。",
        "第二句内容。",
        "第三句内容。",
        "第四句内容。",
    ]


def test_split_falls_back_to_words_without_punctuation() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta"
    src = sub((0, 4000, text))
    tgt = sub((0, 4000, text))

    laid, _, _ = layout_pair(
        src, tgt, options=LayoutOptions(src_width=20, tgt_width=20, max_lines=1)
    )

    assert len(laid) > 1
    for cue in laid.cues:
        for line in cue.text.split("\n"):
            assert len(line) <= 20


def test_each_sentence_becomes_its_own_cue() -> None:
    """一句一条是字幕的理想粒度：三条短句不该粘成一条，也不该切得更碎。"""

    src = sub((0, 9000, "First one. Second one. Third one."))
    tgt = sub((0, 9000, "第一句。第二句。第三句。"))

    laid_src, laid_tgt, stats = layout_pair(src, tgt, options=LayoutOptions())

    assert stats.cues_after == 3
    assert [cue.text for cue in laid_tgt.cues] == ["第一句。", "第二句。", "第三句。"]
    assert [cue.text for cue in laid_src.cues] == ["First one.", "Second one.", "Third one."]


def test_wrap_never_leaves_punctuation_alone_on_a_line() -> None:
    text = "这是一句刚刚好超过一行宽度的中文内容，句号会掉到第二行。"  # 长度正好让标点落单

    lines = wrap_text(text, 15)

    assert len(lines) <= 2
    assert all(not all(char in "，。！？、；：,.!?;:" for char in line) for line in lines)


def test_split_does_not_break_latin_words_across_cues() -> None:
    """切条时不能把专名切到两条 cue 里（'Blen' / 'der'）。"""

    src = sub((0, 6000, "I think that we're at a point now where Astra is so good at Blender and 3D modeling."))
    tgt = sub((0, 6000, "我认为现在Astra在Blender和3D建模方面都非常出色。"))

    _, laid_tgt, _ = layout_pair(src, tgt, options=LayoutOptions())

    assert len(laid_tgt) > 1
    assert any("Blender" in cue.text for cue in laid_tgt.cues)


def test_wrap_does_not_split_latin_proper_nouns() -> None:
    """中英混排时不能把 Blender 切成 "Blend" / "er"（专名断行是硬伤）。"""

    text = "我认为现在Astra在Blender和3D建模方面都非常出色。"

    lines = wrap_text(text, 16)

    assert all(len(line) <= 16 for line in lines)
    assert "".join(lines) == text
    assert any("Blender" in line for line in lines)


def test_wrap_keeps_orphan_punctuation_with_content() -> None:
    text = "所以这其实是我们一直在探索的事情。"  # 17 字，宽度 16：不能出现 17 字行

    lines = wrap_text(text, 16)

    assert all(len(line) <= 16 for line in lines)
    assert all(not all(char in "，。！？、；：,.!?;:" for char in line) for line in lines)
    assert "".join(lines) == text


def test_layout_options_defaults() -> None:
    defaults = LayoutOptions()

    assert defaults.max_lines == 2
    assert defaults.min_duration_ms == 400


# ---------------------------------------------------------------- 按句切分（M10）


EN_LAYOUT = LayoutOptions(src_width=16, tgt_width=42, max_lines=2, src_cps=9.0, tgt_cps=20.0)


def test_target_sentences_are_kept_whole_when_lengths_differ() -> None:
    """英文三句长短悬殊时，不能按等分比例切（会把句子拦腰截断）——必须一句一条。"""

    src = sub((0, 9000, "第一句比较短。第二句稍长一些内容。第三句收尾。"))
    tgt = sub(
        (
            0,
            9000,
            "The first sentence is fairly long and wordy, with several clauses. "
            "Short one. And the third sentence closes it all.",
        )
    )

    _, laid_tgt, _ = layout_pair(src, tgt, options=EN_LAYOUT)

    sentences = [cue.text.replace("\n", " ") for cue in laid_tgt.cues]
    assert sentences == [
        "The first sentence is fairly long and wordy, with several clauses.",
        "Short one.",
        "And the third sentence closes it all.",
    ]


def test_long_target_sentence_is_split_at_clause_not_at_function_word() -> None:
    """单句超长必须切时，切点要落在分句/实词后，不能留下 'over the' 这种残句。"""

    text = (
        "In the early morning the elephants walk a long way to reach the cave where they dig "
        "for salt with their tusks and trunks and then slowly walk back again."
    )
    src = sub((0, 12000, "清晨大象走很远的路到洞里用象牙和鼻子挖盐然后再慢慢走回去"))
    tgt = sub((0, 12000, text))

    _, laid_tgt, _ = layout_pair(src, tgt, options=EN_LAYOUT)

    assert len(laid_tgt) > 1
    for cue in laid_tgt.cues[:-1]:
        last = cue.text.replace("\n", " ").split()[-1].strip(".,;:!?").lower()
        assert last not in FUNCTION_WORDS
    joined = " ".join(cue.text.replace("\n", " ") for cue in laid_tgt.cues)
    assert joined.split() == text.split()


def test_bilingual_sides_pair_sentence_by_sentence() -> None:
    """原文/译文句数一致时，双语显示块要一一对应（内容不串行）。"""

    src = sub((0, 6000, "第一句话说完了。第二句话也结束了。"))
    tgt = sub((0, 6000, "The first sentence is done. The second sentence is over too."))

    laid_src, laid_tgt, _ = layout_pair(src, tgt, options=EN_LAYOUT)

    assert [cue.text for cue in laid_src.cues] == ["第一句话说完了。", "第二句话也结束了。"]
    assert [cue.text for cue in laid_tgt.cues] == [
        "The first sentence is done.",
        "The second sentence is over too.",
    ]


def test_target_text_is_preserved_after_sentence_split() -> None:
    src = sub((0, 10000, "第一句。第二句。第三句。"))
    tgt = sub(
        (
            0,
            10000,
            "First one. Second, a much longer sentence that keeps going for a while. Third.",
        )
    )

    _, laid_tgt, _ = layout_pair(src, tgt, options=EN_LAYOUT)

    joined = " ".join(cue.text.replace("\n", " ") for cue in laid_tgt.cues)
    assert joined.replace("  ", " ").strip() == (
        "First one. Second, a much longer sentence that keeps going for a while. Third."
    ).replace("  ", " ").strip()


def test_cjk_cut_avoids_splitting_words() -> None:
    """原文侧跟随译文进度切时，中文也不要切在词中间（'其|他' → '通过|其他'）。"""

    src = sub((0, 8000, "草食动物无法像食肉动物那样通过其他动物的血肉补充自己体内的盐分"))
    tgt = sub(
        (
            0,
            8000,
            "Herbivores can't replenish their salt the way carnivores do, through the flesh "
            "and blood of other animals.",
        )
    )

    laid_src, _, _ = layout_pair(src, tgt, options=EN_LAYOUT)

    joined = "".join(cue.text.replace("\n", "") for cue in laid_src.cues)
    assert joined == "草食动物无法像食肉动物那样通过其他动物的血肉补充自己体内的盐分"

