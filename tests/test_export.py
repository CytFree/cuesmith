"""5 种字幕格式导出（FR-2 行为 1）。

对应验收：
- AC-4 5 种格式均可被 ffmpeg / VLC / Subtitle Edit 正常解析（见 test_export_ffmpeg.py）
- AC-6 双语 SRT 的原文与译文在时间轴上逐条对应（同一 cue 内两行）
"""

from __future__ import annotations

import pytest

from cuesmith.export import dump_ass, dump_bilingual_srt, dump_txt, dump_vtt
from cuesmith.srt import Cue, Subtitle, Timing, parse_srt


def sub(*pairs: tuple[int, int, str]) -> Subtitle:
    return Subtitle(cues=tuple(Cue(timing=Timing(start, end), text=text) for start, end, text in pairs))


SRC = sub((1000, 2000, "你好"), (2500, 4000, "再见"))
TGT = sub((1000, 2000, "Hello"), (2500, 4000, "Bye"))


# ---------------------------------------------------------------- 双语 SRT


def test_bilingual_srt_pairs_source_and_target_in_same_cue() -> None:
    parsed = parse_srt(dump_bilingual_srt(SRC, TGT))

    assert [cue.text for cue in parsed.cues] == ["你好\nHello", "再见\nBye"]


def test_bilingual_srt_uses_source_timeline() -> None:
    parsed = parse_srt(dump_bilingual_srt(SRC, TGT))

    assert [cue.timing for cue in parsed.cues] == [c.timing for c in SRC]


def test_bilingual_srt_keeps_multiline_source_above_target() -> None:
    src = sub((0, 1000, "第一行\n第二行"))
    tgt = sub((0, 1000, "Line one"))

    parsed = parse_srt(dump_bilingual_srt(src, tgt))

    assert parsed.cues[0].text == "第一行\n第二行\nLine one"


def test_bilingual_srt_omits_line_for_empty_translation() -> None:
    """空译已由校验层报 error；双语文件里只保留原文，不塞占位符改写内容。"""

    tgt = sub((1000, 2000, ""), (2500, 4000, "Bye"))

    parsed = parse_srt(dump_bilingual_srt(SRC, tgt))

    assert [cue.text for cue in parsed.cues] == ["你好", "再见\nBye"]


def test_bilingual_srt_rejects_cue_count_mismatch() -> None:
    with pytest.raises(ValueError, match="条数"):
        dump_bilingual_srt(SRC, sub((1000, 2000, "Hello")))


# ---------------------------------------------------------------- ASS


def test_ass_contains_required_sections() -> None:
    out = dump_ass(SRC, TGT)

    assert out.startswith("[Script Info]")
    assert "[V4+ Styles]" in out
    assert "[Events]" in out
    assert "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text" in out


def test_ass_dialogue_lines_use_centisecond_timestamps() -> None:
    out = dump_ass(sub((1239, 4567, "Hello")), sub((1239, 4567, "Hello")))

    assert "Dialogue: 0,0:00:01.23,0:00:04.56," in out


def test_ass_puts_source_and_target_on_two_lines() -> None:
    out = dump_ass(SRC, TGT)

    assert r"你好\NHello" in out
    assert r"再见\NBye" in out


def test_ass_converts_existing_newlines_to_backslash_n() -> None:
    src = sub((0, 1000, "第一行\n第二行"))
    tgt = sub((0, 1000, "Line one"))

    out = dump_ass(src, tgt)

    assert r"第一行\N第二行\NLine one" in out


def test_ass_strips_html_tags_that_ass_cannot_render() -> None:
    src = sub((0, 1000, "<i>你好</i>"))
    tgt = sub((0, 1000, "Hello"))

    out = dump_ass(src, tgt)

    assert "<i>" not in out
    assert r"你好\NHello" in out


def test_ass_escapes_unmatched_brace() -> None:
    """未闭合的 { 在 ASS 里会被当成覆写块开头，必须转义，否则吞掉整行。"""

    out = dump_ass(sub((0, 1000, "a{b")), sub((0, 1000, "a{b")))

    assert r"a\{b" in out


def test_ass_has_single_dialogue_per_cue() -> None:
    out = dump_ass(SRC, TGT)

    assert out.count("Dialogue: ") == len(SRC)


# ---------------------------------------------------------------- VTT


def test_vtt_starts_with_header_and_blank_line() -> None:
    assert dump_vtt(SRC, TGT).startswith("WEBVTT\n\n")


def test_vtt_uses_dot_milliseconds() -> None:
    out = dump_vtt(sub((1000, 2500, "你好")), sub((1000, 2500, "Hello")))

    assert "00:00:01.000 --> 00:00:02.500" in out


def test_vtt_puts_source_and_target_on_two_lines() -> None:
    out = dump_vtt(SRC, TGT)

    assert "你好\nHello" in out


def test_vtt_escapes_markup_characters() -> None:
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, "R&D <3 >_>"))

    out = dump_vtt(src, tgt)

    assert "R&amp;D" in out
    assert "&lt;3" in out
    assert "&gt;_&gt;" in out


def test_vtt_keeps_italic_markup_that_vtt_supports() -> None:
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, "<i>Hello</i>"))

    out = dump_vtt(src, tgt)

    assert "<i>Hello</i>" in out


# ---------------------------------------------------------------- TXT


def test_txt_keeps_one_line_per_cue() -> None:
    parsed_lines = dump_txt(TGT).splitlines()

    assert parsed_lines == ["Hello", "Bye"]


def test_txt_flattens_multiline_translation_into_one_line() -> None:
    tgt = sub((0, 1000, "Line one\nLine two"))

    assert dump_txt(tgt) == "Line one Line two\n"


def test_txt_strips_markup() -> None:
    tgt = sub((0, 1000, "<i>Hello</i>"))

    assert dump_txt(tgt) == "Hello\n"


def test_txt_skips_empty_cues_instead_of_blank_lines() -> None:
    tgt = sub((0, 1000, "Hello"), (1000, 2000, ""), (2000, 3000, "Bye"))

    assert dump_txt(tgt) == "Hello\nBye\n"


def test_txt_of_empty_subtitle_is_empty_string() -> None:
    assert dump_txt(Subtitle()) == ""
