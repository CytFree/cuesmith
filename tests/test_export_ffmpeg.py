"""AC-4 实测：5 种格式必须能被真正的字幕解析器读回来。

机器上没有 VLC / Subtitle Edit 的无人值守接口，所以用 ffmpeg（libavformat + libass）
做**最严格的可解析性判据**：
1. ffprobe 能认出字幕流的 codec（subrip / ass / webvtt）
2. ffmpeg 能把文件解成 SRT，且 cue 数、时间轴、文本逐条一致

VLC / Subtitle Edit 的人工核对仍作为交付前的一次性检查（见 M1 实现说明）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from submatrix.export import dump_ass, dump_bilingual_srt, dump_txt, dump_vtt
from submatrix.srt import Cue, Subtitle, Timing, dump_srt, parse_srt
from submatrix.textutil import visible_text
from tests.support import FFMPEG, FFPROBE, requires_media_tools

pytestmark = requires_media_tools

SRC = Subtitle(
    cues=(
        Cue(timing=Timing(1000, 2500), text="你好世界"),
        Cue(timing=Timing(3000, 5000), text="再见"),
    )
)
TGT = Subtitle(
    cues=(
        Cue(timing=Timing(1000, 2500), text="Hello world"),
        Cue(timing=Timing(3000, 5000), text="Bye"),
    ),
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def mux_to_srt(path: Path) -> str:
    completed = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-i", str(path), "-f", "srt", "-"],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout.decode("utf-8")


def probe_codec(path: Path) -> str:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout.decode("utf-8").strip().splitlines()[0]


@pytest.mark.parametrize(
    ("filename", "expected_codec"),
    [
        ("en.srt", "subrip"),
        ("bilingual.srt", "subrip"),
        ("bilingual.ass", "ass"),
        ("bilingual.vtt", "webvtt"),
    ],
)
def test_ffprobe_recognises_subtitle_stream(tmp_path, filename: str, expected_codec: str) -> None:
    path = write(tmp_path / filename, _content_for(filename))

    assert probe_codec(path) == expected_codec


def _content_for(filename: str) -> str:
    if filename == "en.srt":
        return dump_srt(TGT)
    if filename == "bilingual.srt":
        return dump_bilingual_srt(SRC, TGT)
    if filename == "bilingual.ass":
        return dump_ass(SRC, TGT)
    if filename == "bilingual.vtt":
        return dump_vtt(SRC, TGT)
    raise AssertionError(filename)


@pytest.mark.parametrize("filename", ["en.srt", "bilingual.srt", "bilingual.ass", "bilingual.vtt"])
def test_ffmpeg_decodes_all_formats_without_loss(tmp_path, filename: str) -> None:
    path = write(tmp_path / filename, _content_for(filename))

    parsed = parse_srt(mux_to_srt(path))

    expected_texts = (
        ["Hello world", "Bye"]
        if filename == "en.srt"
        else ["你好世界\nHello world", "再见\nBye"]
    )
    # ffmpeg 把 ASS 转 SRT 时会补一层 <font face=..> 以保样式，属正常表现，
    # 所以按"观众可见文本"比对（这也正是 CPS / 行宽的计算口径）。
    assert [visible_text(cue.text) for cue in parsed.cues] == expected_texts
    assert [cue.timing for cue in parsed.cues] == [Timing(1000, 2500), Timing(3000, 5000)]


def test_txt_is_plain_utf8_text(tmp_path) -> None:
    path = write(tmp_path / "en.txt", dump_txt(TGT))

    text = path.read_text(encoding="utf-8")

    assert text == "Hello world\nBye\n"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
