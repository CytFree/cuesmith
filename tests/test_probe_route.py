"""FR-1 路由判定与流解析（纯逻辑，不碰磁盘）。

对应验收：
- AC-2 路由判定在样本集上 100% 正确，含 PGS、forced 轨、无字幕流
- AC-3 图形字幕流必须明确报错（decision=unsupported），不得静默转 ASR

说明：真实 PGS 文件本地造不出来（ffmpeg 只能解码图形字幕、不能编码），
所以这一层用**模拟的 ffprobe 输出**覆盖判定逻辑；真实媒体文件的端到端
测试见 test_probe_media.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cuesmith.probe import (
    GRAPHIC_SUBTITLE_CODECS,
    TEXT_SUBTITLE_CODECS,
    MediaProbe,
    ProbeError,
    RouteDecision,
    choose_subtitle,
    classify_subtitle,
    ffprobe_json,
    parse_probe,
)

PATH = Path("video.mkv")


def stream(
    index: int,
    codec_type: str,
    codec_name: str,
    *,
    lang: str | None = None,
    title: str | None = None,
    forced: bool = False,
    default: bool = False,
    attached_pic: bool = False,
    **extra: object,
) -> dict:
    payload: dict = {
        "index": index,
        "codec_type": codec_type,
        "codec_name": codec_name,
        "disposition": {"forced": forced, "default": default, "attached_pic": attached_pic},
    }
    tags: dict[str, str] = {}
    if lang is not None:
        tags["language"] = lang
    if title is not None:
        tags["title"] = title
    if tags:
        payload["tags"] = tags
    payload.update(extra)
    return payload


def payload(*streams: dict, duration: str = "41.332000", size: str = "12345678") -> dict:
    return {
        "streams": list(streams),
        "format": {
            "format_name": "matroska,webm",
            "duration": duration,
            "size": size,
        },
    }


def probe_of(*streams: dict, **kwargs: object) -> MediaProbe:
    return parse_probe(PATH, payload(*streams, **kwargs))


def text_stream(index: int, codec: str = "subrip", **kwargs: object) -> dict:
    return stream(index, "subtitle", codec, **kwargs)


# ---------------------------------------------------------------- 解析


def test_parses_stream_groups() -> None:
    probe = probe_of(
        stream(0, "video", "h264", width=1920, height=1080),
        stream(1, "audio", "aac", lang="zho", channels=2, sample_rate="48000"),
        text_stream(2, "subrip", lang="chi", title="简体中文", default=True),
    )

    assert len(probe.video) == 1
    assert len(probe.audio) == 1
    assert len(probe.subtitle) == 1


def test_parses_stream_fields() -> None:
    probe = probe_of(
        stream(0, "video", "h264", width=1920, height=1080),
        text_stream(2, "subrip", lang="chi", title="简体中文", forced=True, default=False),
    )

    video = probe.video[0]
    assert (video.width, video.height) == (1920, 1080)

    subtitle = probe.subtitle[0]
    assert subtitle.index == 2
    assert subtitle.codec_name == "subrip"
    assert subtitle.language == "chi"
    assert subtitle.title == "简体中文"
    assert subtitle.forced is True
    assert subtitle.default is False


def test_parses_format_metadata() -> None:
    probe = probe_of(stream(0, "video", "h264"))

    assert probe.format_name == "matroska,webm"
    assert probe.duration_s == pytest.approx(41.332)
    assert probe.size_bytes == 12345678


def test_tolerates_missing_format_metadata() -> None:
    probe = parse_probe(PATH, {"streams": [], "format": {"format_name": "mp4"}})

    assert probe.duration_s is None
    assert probe.size_bytes is None


def test_ignores_attached_picture_with_warning() -> None:
    probe = probe_of(
        stream(0, "video", "h264", width=1920, height=1080),
        stream(1, "video", "mjpeg", attached_pic=True),
        text_stream(2, "subrip"),
    )

    assert len(probe.video) == 1
    assert any("封面" in warning for warning in probe.warnings)


def test_parses_without_tags() -> None:
    probe = probe_of(text_stream(0, "subrip"))

    assert probe.subtitle[0].language is None
    assert probe.subtitle[0].title is None


# ---------------------------------------------------------------- 编码分类


@pytest.mark.parametrize("codec", sorted(TEXT_SUBTITLE_CODECS))
def test_classify_text_codecs(codec: str) -> None:
    assert classify_subtitle(codec) == "text"


@pytest.mark.parametrize("codec", sorted(GRAPHIC_SUBTITLE_CODECS))
def test_classify_graphic_codecs(codec: str) -> None:
    assert classify_subtitle(codec) == "graphic"


def test_classify_unknown_codec() -> None:
    assert classify_subtitle("eia_608") == "unknown"


# ---------------------------------------------------------------- 路由：抽取


@pytest.mark.parametrize("codec", ["subrip", "ass", "ssa", "mov_text", "webvtt"])
def test_route_extracts_text_track(codec: str) -> None:
    decision = choose_subtitle(probe_of(text_stream(2, codec)), "zh")

    assert decision.kind == "extract"
    assert decision.stream_index == 2
    assert decision.subtitle_ordinal == 0
    assert decision.codec == codec
    assert decision.reason


def test_route_ordinal_counts_within_subtitle_streams() -> None:
    """`-map 0:s:N` 的 N 是字幕流序号，不是全局流序号。"""

    probe = probe_of(
        stream(0, "video", "h264"),
        stream(3, "audio", "aac"),
        text_stream(5, "subrip", lang="eng"),
        text_stream(7, "ass", lang="chi"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.stream_index == 7
    assert decision.subtitle_ordinal == 1


# ---------------------------------------------------------------- 路由：ASR


def test_route_without_subtitle_streams_goes_to_asr() -> None:
    probe = probe_of(stream(0, "video", "h264"), stream(1, "audio", "aac"))

    decision = choose_subtitle(probe, "zh")

    assert decision.kind == "route_to_asr"
    assert decision.stream_index is None
    assert "ASR" in decision.reason


# ---------------------------------------------------------------- 路由：图形字幕（AC-3）


def test_route_pgs_only_is_unsupported() -> None:
    decision = choose_subtitle(probe_of(text_stream(2, "hdmv_pgs_subtitle")), "zh")

    assert decision.kind == "unsupported"
    assert "图形" in decision.reason
    assert decision.codec == "hdmv_pgs_subtitle"


def test_route_dvd_subtitle_only_is_unsupported() -> None:
    decision = choose_subtitle(probe_of(text_stream(2, "dvd_subtitle")), "zh")

    assert decision.kind == "unsupported"
    assert "图形" in decision.reason


def test_unsupported_never_silently_falls_back_to_asr() -> None:
    """AC-3 的核心：图形字幕必须终止，不能悄悄转 ASR。"""

    decision = choose_subtitle(
        probe_of(stream(0, "video", "h264"), text_stream(2, "hdmv_pgs_subtitle")), "zh"
    )

    assert decision.kind == "unsupported"
    assert decision.kind != "route_to_asr"


def test_unknown_codec_is_unsupported_with_explicit_message() -> None:
    decision = choose_subtitle(probe_of(text_stream(2, "eia_608")), "zh")

    assert decision.kind == "unsupported"
    assert "eia_608" in decision.reason


def test_graphic_track_alongside_text_only_warns() -> None:
    probe = probe_of(
        text_stream(2, "hdmv_pgs_subtitle"),
        text_stream(3, "subrip", lang="chi"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.kind == "extract"
    assert decision.stream_index == 3
    assert any("图形" in warning for warning in decision.warnings)


# ---------------------------------------------------------------- 路由：forced 轨


def test_route_skips_forced_track_and_picks_normal_one() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="chi", forced=True),
        text_stream(3, "subrip", lang="chi"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.kind == "extract"
    assert decision.stream_index == 3
    assert any("forced" in warning for warning in decision.warnings)


def test_route_only_forced_track_falls_back_to_asr() -> None:
    probe = probe_of(stream(0, "video", "h264"), text_stream(2, "subrip", forced=True))

    decision = choose_subtitle(probe, "zh")

    assert decision.kind == "route_to_asr"
    assert any("forced" in warning for warning in decision.warnings)


# ---------------------------------------------------------------- 路由：语言优先级


def test_route_prefers_matching_language_over_stream_order() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="eng"),
        text_stream(3, "subrip", lang="chi"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.stream_index == 3
    assert "chi" in decision.reason


@pytest.mark.parametrize("tag", ["chi", "zho", "zh", "zh-CN", "zh_cn", "cmn", "chi-Hans"])
def test_route_matches_language_tag_variants(tag: str) -> None:
    probe = probe_of(text_stream(2, "subrip", lang="eng"), text_stream(3, "subrip", lang=tag))

    assert choose_subtitle(probe, "zh").stream_index == 3


def test_route_english_source_matches_eng_tag() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="chi"),
        text_stream(3, "subrip", lang="eng"),
    )

    assert choose_subtitle(probe, "en").stream_index == 3


def test_route_prefers_unmarked_track_over_other_language() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="eng"),
        text_stream(3, "subrip"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.stream_index == 3
    assert "未标记" in decision.reason


def test_route_falls_back_to_first_text_with_warning() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="eng"),
        text_stream(3, "subrip", lang="fre"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.stream_index == 2
    assert any("不匹配" in warning for warning in decision.warnings)


def test_route_ignores_undetermined_language_tag() -> None:
    probe = probe_of(
        text_stream(2, "subrip", lang="eng"),
        text_stream(3, "subrip", lang="und"),
    )

    decision = choose_subtitle(probe, "zh")

    assert decision.stream_index == 3
    assert "未标记" in decision.reason


# ---------------------------------------------------------------- ffprobe 调用


def test_ffprobe_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ProbeError):
        ffprobe_json(tmp_path / "nope.mkv")


def test_route_decision_serialises() -> None:
    decision = RouteDecision(
        kind="extract",
        reason="字幕流 #2 语言标签 chi 匹配源语言 zh",
        stream_index=2,
        subtitle_ordinal=0,
        codec="subrip",
        language="chi",
        warnings=("w",),
    )

    assert decision.to_dict() == {
        "kind": "extract",
        "stream_index": 2,
        "subtitle_ordinal": 0,
        "codec": "subrip",
        "language": "chi",
        "reason": "字幕流 #2 语言标签 chi 匹配源语言 zh",
    }
