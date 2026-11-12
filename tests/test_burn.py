"""字幕烧录（M10）：把外挂字幕变成"带硬字幕的成片"。

不测 libass 的渲染细节（那是 ffmpeg 的事），测的是契约：
输入校验、SRT→ASS 转换、输出可播放（h264+aac、时长不变）、**像素差核验真的能判断
字幕有没有烧上去**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from submatrix.burn import BurnError, BurnOptions, run_burn
from submatrix.export import dump_ass_single
from submatrix.srt import Cue, Subtitle, Timing, dump_srt, load_srt
from tests.support import build_test_video, requires_media_tools

pytestmark = requires_media_tools


def make_video(path: Path) -> Path:
    """本文件用的测试片：3 秒 640×360 深灰。"""
    return build_test_video(path, seconds=3, color="#303030", size="640x360")


def write_srt(path: Path, start: int, end: int, text: str) -> Path:
    subtitle = Subtitle(cues=(Cue(timing=Timing(start, end), text=text),))
    path.write_text(dump_srt(subtitle), encoding="utf-8", newline="\n")
    return path


def test_burn_srt_produces_playable_video(tmp_path) -> None:
    video = make_video(tmp_path / "in.mp4")
    srt = write_srt(tmp_path / "en.srt", 500, 2500, "This is a burned subtitle")

    result = run_burn(
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "out")
    )

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.video_path.is_file()
    assert result.ass_path.is_file()  # SRT 先转成了带样式的 ASS
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["command"] == "burn"
    assert report["input"]["subtitle"]["cues"] == 1
    assert report["output"]["codecs"] == {"video": "h264", "audio": "aac"}
    assert abs(report["output"]["duration_delta_ms"]) <= 100
    # 核验：字幕确实把画面底部改了
    assert report["verification"]["ok"] is True
    assert report["verification"]["region"] == "bottom"
    assert report["verification"]["region_pixel_diff"] > 0


def test_burn_accepts_ass_input(tmp_path) -> None:
    video = make_video(tmp_path / "in.mp4")
    subtitle = load_srt(write_srt(tmp_path / "en.srt", 500, 2500, "Hello"))[0]
    ass = tmp_path / "sub.ass"
    ass.write_text(dump_ass_single(subtitle), encoding="utf-8")

    result = run_burn(BurnOptions(video=video, subtitle=ass, out=tmp_path / "out"))

    assert result.status == "ok"
    # .ass 输入也会被重写成带位置样式的副本，而不是直接用它
    assert result.ass_path == (tmp_path / "out" / "subtitles.ass").resolve()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["input"]["subtitle"]["cues"] is None  # ASS 不数 cue
    assert report["verification"]["ok"] is True


def test_burn_can_skip_verification(tmp_path) -> None:
    video = make_video(tmp_path / "in.mp4")
    srt = write_srt(tmp_path / "en.srt", 500, 2500, "Hello")

    result = run_burn(
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "out", verify=False)
    )

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["verification"] is None
    assert result.status == "ok"


def test_burn_top_position_rewrites_ass_style(tmp_path) -> None:
    """源片自带硬字幕时把译文烧到顶部避让：样式要对齐到 8（顶部居中）。"""

    video = make_video(tmp_path / "in.mp4")
    srt = write_srt(tmp_path / "en.srt", 500, 2500, "This is a burned subtitle")

    result = run_burn(
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "out", position="top")
    )

    ass_text = result.ass_path.read_text(encoding="utf-8")
    style = next(line for line in ass_text.splitlines() if line.startswith("Style:"))
    fields = style.split(",")
    assert fields[18] == "8"  # Alignment：顶部居中（含 "Style: Default" 前缀，索引 +1）
    assert fields[21] == "72"  # MarginV 顶部边距
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["config"]["position"] == "top"
    assert report["verification"]["region"] == "top"
    assert report["verification"]["ok"] is True


def test_burn_rejects_bad_inputs(tmp_path) -> None:
    video = make_video(tmp_path / "in.mp4")
    srt = write_srt(tmp_path / "en.srt", 0, 1000, "Hello")

    with pytest.raises(BurnError, match="不存在"):
        run_burn(BurnOptions(video=tmp_path / "nope.mp4", subtitle=srt, out=tmp_path / "o"))
    with pytest.raises(BurnError, match="不存在"):
        run_burn(BurnOptions(video=video, subtitle=tmp_path / "nope.srt", out=tmp_path / "o"))
    with pytest.raises(BurnError, match="ass 或 .srt"):
        BurnOptions(video=video, subtitle=tmp_path / "x.vtt", out=tmp_path / "o").validate()
    with pytest.raises(BurnError, match="CRF"):
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "o", crf=99).validate()
    with pytest.raises(BurnError, match="preset"):
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "o", preset="turbo").validate()
    with pytest.raises(BurnError, match="字幕位置"):
        BurnOptions(video=video, subtitle=srt, out=tmp_path / "o", position="left").validate()
