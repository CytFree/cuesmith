"""SRT 解析与序列化（FR-2 的输入格式）。

对应 AC：AC-4（产物可被标准工具解析）、AC-5（不可静默吞错）、AC-6（逐条对应依赖可靠的解析）。
"""

from __future__ import annotations

import pytest

from cuesmith.srt import (
    Cue,
    SrtParseError,
    Subtitle,
    Timing,
    dump_srt,
    format_srt_time,
    load_srt,
    parse_srt,
)


def cue(start_ms: int, end_ms: int, text: str) -> Cue:
    return Cue(timing=Timing(start_ms, end_ms), text=text)


def test_parse_simple_two_cues() -> None:
    raw = (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "Hello\n"
        "\n"
        "2\n"
        "00:00:04,000 --> 00:00:05,250\n"
        "World\n"
    )

    sub = parse_srt(raw)

    assert sub.cues == (cue(1000, 3500, "Hello"), cue(4000, 5250, "World"))


def test_parse_tolerates_bom_and_crlf() -> None:
    raw = "\ufeff1\r\n00:00:00,000 --> 00:00:01,000\r\nHello\r\n"

    assert parse_srt(raw).cues == (cue(0, 1000, "Hello"),)


def test_parse_without_index_numbers() -> None:
    raw = (
        "00:00:01,000 --> 00:00:02,000\nHello\n"
        "\n"
        "00:00:03,000 --> 00:00:04,000\nWorld\n"
    )

    assert parse_srt(raw).cues == (cue(1000, 2000, "Hello"), cue(3000, 4000, "World"))


def test_parse_accepts_dot_as_millisecond_separator() -> None:
    raw = "1\n00:00:01.000 --> 00:00:02.500\nHello\n"

    assert parse_srt(raw).cues == (cue(1000, 2500, "Hello"),)


def test_parse_pads_short_fraction_to_milliseconds() -> None:
    """部分工具写成 ,5 / ,25，含义是 500ms / 250ms。"""

    raw = "1\n00:00:00,5 --> 00:00:02,25\nHello\n"

    assert parse_srt(raw).cues == (cue(500, 2250, "Hello"),)


def test_parse_keeps_multiline_text() -> None:
    raw = "1\n00:00:01,000 --> 00:00:02,000\nLine one\nLine two\n"

    assert parse_srt(raw).cues == (cue(1000, 2000, "Line one\nLine two"),)


def test_parse_strips_text_line_whitespace() -> None:
    raw = "1\n00:00:01,000 --> 00:00:02,000\n  Hello  \n\tWorld\t\n"

    assert parse_srt(raw).cues == (cue(1000, 2000, "Hello\nWorld"),)


def test_parse_ignores_trailing_position_suffix() -> None:
    """ffmpeg / 老工具会输出 X1:..Y2:.. 或 align:start 之类的坐标后缀。"""

    raw = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000 X1:100 X2:200 Y1:300 Y2:400\n"
        "Hello\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000 align:start position:0%\n"
        "World\n"
    )

    assert parse_srt(raw).cues == (cue(1000, 2000, "Hello"), cue(3000, 4000, "World"))


def test_parse_ignores_leading_blank_lines() -> None:
    raw = "\n\n1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    assert parse_srt(raw).cues == (cue(1000, 2000, "Hello"),)


def test_parse_empty_text_returns_no_cues() -> None:
    assert parse_srt("   \n\n  ").cues == ()


def test_parse_rejects_end_before_start() -> None:
    with pytest.raises(SrtParseError) as excinfo:
        parse_srt("1\n00:00:02,000 --> 00:00:01,000\nHi\n")

    assert excinfo.value.block == 1
    assert "结束时间早于开始时间" in str(excinfo.value)


def test_parse_rejects_block_without_timing_line() -> None:
    with pytest.raises(SrtParseError) as excinfo:
        parse_srt("1\n00:00:01,000 --> 00:00:02,000\nok\n\n2\nnot a timing line\nHi\n")

    assert excinfo.value.block == 2


def test_parse_rejects_unparsable_timestamp() -> None:
    with pytest.raises(SrtParseError):
        parse_srt("1\n00:00:xx,000 --> 00:00:02,000\nHi\n")


def test_format_srt_time() -> None:
    assert format_srt_time(0) == "00:00:00,000"
    assert format_srt_time(3723456) == "01:02:03,456"
    assert format_srt_time(360_000_000) == "100:00:00,000"


def test_dump_srt_roundtrip() -> None:
    sub = Subtitle(
        cues=(
            cue(0, 1500, "第一行\n第二行"),
            cue(2000, 4250, "Hello, world"),
        )
    )

    assert parse_srt(dump_srt(sub)) == sub


def test_dump_srt_renumbers_cues() -> None:
    sub = Subtitle(cues=(cue(0, 1000, "a"), cue(1000, 2000, "b")))

    assert dump_srt(sub).startswith("1\n")
    assert "\n2\n" in dump_srt(sub)


def test_subtitle_len_and_iter() -> None:
    sub = Subtitle(cues=(cue(0, 1000, "a"), cue(1000, 2000, "b")))

    assert len(sub) == 2
    assert [c.text for c in sub] == ["a", "b"]


def test_load_srt_reads_utf8(tmp_path) -> None:
    path = tmp_path / "zh.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    sub, encoding = load_srt(path)

    assert sub.cues == (cue(1000, 2000, "你好"),)
    assert encoding == "utf-8"


def test_load_srt_reads_utf8_bom(tmp_path) -> None:
    path = tmp_path / "zh.srt"
    path.write_text("\ufeff1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    sub, _ = load_srt(path)

    assert sub.cues == (cue(1000, 2000, "你好"),)


def test_load_srt_falls_back_to_gbk(tmp_path) -> None:
    """Windows 下老工具产出的中文 SRT 常见 GBK 编码，不能直接崩。"""

    path = tmp_path / "gbk.srt"
    path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n你好世界\n".encode("gbk"))

    sub, encoding = load_srt(path)

    assert sub.cues == (cue(1000, 2000, "你好世界"),)
    assert encoding in {"gbk", "gb18030"}
