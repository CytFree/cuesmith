"""FR-1 真实媒体端到端：真 ffmpeg 造片 → probe → 抽 SRT → 比 ground truth。

对应验收：
- AC-1 对含文本字幕流的 MKV/MP4，抽取结果与源字幕流逐条一致（100%）
- AC-2 路由判定在 PGS / forced / 无字幕流 三种边界上正确
- AC-3 图形字幕明确报错：用**真实 PGS 流**（ffmpeg 官方 FATE 样本 sub/pgs_sub.sup，
  用 `-c copy` 塞进 MKV）验证决策 → 清单 → 退出码全链路。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cuesmith.cli import main
from cuesmith.export import dump_ass
from cuesmith.probe import ExtractionError, ProbeOptions, run_probe
from cuesmith.srt import parse_srt
from cuesmith.textutil import visible_text
from tests.support import FFMPEG, requires_ffmpeg

pytestmark = requires_ffmpeg

# 真实 PGS 样本：https://fate-suite.ffmpeg.org/sub/pgs_sub.sup（49 KB）
PGS_SUP = Path(__file__).resolve().parents[1] / "spike" / "samples" / "pgs_sub.sup"
# 真实 VobSub（dvd_subtitle）样本：https://fate-suite.ffmpeg.org/sub/vobsub.idx + .sub
VOBSUB_IDX = Path(__file__).resolve().parents[1] / "spike" / "samples" / "vobsub.idx"

# 3 条，含一条双行 —— 用来验证换行不被压平
GT_SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\n你好世界\n"
    "\n"
    "2\n00:00:01,500 --> 00:00:02,500\n第二行上\n第二行下\n"
    "\n"
    "3\n00:00:03,000 --> 00:00:04,000\n增长 37%\n"
)

EN_SRT = (
    "1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
    "\n"
    "2\n00:00:01,500 --> 00:00:02,500\nLine one\nLine two\n"
    "\n"
    "3\n00:00:03,000 --> 00:00:04,000\nUp 37%\n"
)

GROUND_TRUTH = parse_srt(GT_SRT)


def ffmpeg(*args: str) -> None:
    completed = subprocess.run(
        [FFMPEG, "-v", "error", "-y", *args], capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("m2-media")
    (directory / "gt.srt").write_text(GT_SRT, encoding="utf-8", newline="\n")
    (directory / "en.srt").write_text(EN_SRT, encoding="utf-8", newline="\n")
    (directory / "bilingual.ass").write_text(
        dump_ass(parse_srt(GT_SRT), parse_srt(EN_SRT)), encoding="utf-8", newline="\n"
    )

    color = ["-f", "lavfi", "-i", "color=c=black:s=320x240:d=4:r=25"]
    sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    video_only = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]

    # 1) MKV + subrip
    ffmpeg(
        *color, *sine, "-i", str(directory / "gt.srt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        *video_only, "-c:a", "aac", "-c:s", "srt",
        "-metadata:s:s:0", "language=chi",
        str(directory / "softsub.mkv"),
    )
    # 2) MP4 + mov_text
    ffmpeg(
        *color, *sine, "-i", str(directory / "gt.srt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        *video_only, "-c:a", "aac", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=chi",
        str(directory / "softsub.mp4"),
    )
    # 3) MKV + ASS（双语）
    ffmpeg(
        *color, *sine, "-i", str(directory / "bilingual.ass"),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        *video_only, "-c:a", "aac", "-c:s", "ass",
        "-metadata:s:s:0", "language=chi",
        str(directory / "asssub.mkv"),
    )
    # 4) 多字幕轨：先 eng 后 chi
    ffmpeg(
        *color, "-i", str(directory / "en.srt"), "-i", str(directory / "gt.srt"),
        "-map", "0:v", "-map", "1:s", "-map", "2:s",
        *video_only, "-c:s", "srt",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=chi",
        str(directory / "multitrack.mkv"),
    )
    # 5) 唯一字幕轨标记 forced
    ffmpeg(
        *color, "-i", str(directory / "gt.srt"),
        "-map", "0:v", "-map", "1:s",
        *video_only, "-c:s", "srt", "-disposition:s:0", "forced",
        str(directory / "forced.mkv"),
    )
    # 6) 无字幕流
    ffmpeg(
        *color, *sine, "-map", "0:v", "-map", "1:a",
        *video_only, "-c:a", "aac",
        str(directory / "nosub.mp4"),
    )
    return directory


def manifest_of(result) -> dict:
    return json.loads(result.manifest_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- AC-1 抽取一致性


def test_ac1_mkv_subrip_extracts_exactly(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "softsub.mkv", out=tmp_path / "out"))

    assert result.status == "extracted"
    assert result.exit_code == 0
    assert result.subtitle_path is not None
    assert result.subtitle_path.name == "source.srt"
    assert parse_srt(result.subtitle_path.read_text(encoding="utf-8")) == GROUND_TRUTH


def test_ac1_mp4_mov_text_extracts_exactly(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "softsub.mp4", out=tmp_path / "out"))

    assert parse_srt(result.subtitle_path.read_text(encoding="utf-8")) == GROUND_TRUTH


def test_ac1_ass_track_keeps_text_and_timeline(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "asssub.mkv", out=tmp_path / "out"))
    extracted = parse_srt(result.subtitle_path.read_text(encoding="utf-8"))

    # ASS 转 SRT 会补 <font> 标签（样式按 SRT 能力降级），文本与时间轴必须一致
    assert [visible_text(cue.text) for cue in extracted.cues] == [
        "你好世界\nHello world",
        "第二行上\n第二行下\nLine one\nLine two",
        "增长 37%\nUp 37%",
    ]
    assert [cue.timing for cue in extracted.cues] == [cue.timing for cue in GROUND_TRUTH]


def test_extracted_srt_is_utf8_lf(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "softsub.mkv", out=tmp_path / "out"))
    raw = result.subtitle_path.read_bytes()

    assert b"\r\n" not in raw
    assert "你好世界".encode() in raw


# ---------------------------------------------------------------- AC-2 路由边界


def test_ac2_forced_only_track_routes_to_asr(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "forced.mkv", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert result.status == "route_to_asr"
    assert result.exit_code == 0
    assert result.subtitle_path is None
    assert any("forced" in warning for warning in manifest["warnings"])


def test_ac2_multi_track_prefers_source_language(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "multitrack.mkv", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert manifest["decision"]["language"] == "chi"
    assert manifest["decision"]["subtitle_ordinal"] == 1
    assert parse_srt(result.subtitle_path.read_text(encoding="utf-8")) == GROUND_TRUTH


def test_ac2_no_subtitle_stream_routes_to_asr(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "nosub.mp4", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert result.status == "route_to_asr"
    assert result.subtitle_path is None
    assert manifest["decision"]["kind"] == "route_to_asr"
    assert "ASR" in manifest["decision"]["reason"]
    assert manifest["outputs"] == []


def test_manifest_is_written_in_every_outcome(media, tmp_path) -> None:
    for index, name in enumerate(["softsub.mkv", "forced.mkv", "nosub.mp4"]):
        result = run_probe(ProbeOptions(src=media / name, out=tmp_path / f"out{index}"))
        assert result.manifest_path.is_file()


# ---------------------------------------------------------------- 清单内容


def test_manifest_records_probe_and_decision(media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=media / "softsub.mkv", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert manifest["schema_version"] == 1
    assert manifest["tool"] == "cuesmith"
    assert manifest["command"] == "probe"
    assert manifest["status"] == "extracted"
    assert manifest["input"]["bytes"] == (media / "softsub.mkv").stat().st_size
    assert len(manifest["input"]["sha256"]) == 64
    assert manifest["input"]["format_name"].startswith("matroska")
    assert manifest["input"]["duration_s"] == pytest.approx(4.0, abs=0.2)

    assert manifest["streams"]["video"][0]["codec"] == "h264"
    assert manifest["streams"]["audio"][0]["codec"] == "aac"
    assert manifest["streams"]["subtitle"][0]["codec"] == "subrip"
    assert manifest["streams"]["subtitle"][0]["language"] == "chi"

    assert manifest["decision"]["kind"] == "extract"
    assert manifest["decision"]["codec"] == "subrip"
    assert manifest["decision"]["subtitle_ordinal"] == 0

    assert manifest["outputs"] == [
        {
            "kind": "source_srt",
            "path": str(result.subtitle_path),
            "cues": 3,
            "bytes": result.subtitle_path.stat().st_size,
        }
    ]


def test_dry_run_writes_no_subtitle_file(media, tmp_path) -> None:
    result = run_probe(
        ProbeOptions(src=media / "softsub.mkv", out=tmp_path / "out", extract=False)
    )

    assert result.status == "extract"
    assert result.subtitle_path is None
    assert not (tmp_path / "out" / "source.srt").exists()
    assert manifest_of(result)["outputs"] == []


# ---------------------------------------------------------------- AC-3 图形字幕（真实 PGS）


@pytest.fixture(scope="module")
def pgs_media(media, tmp_path_factory) -> Path:
    """把官方 FATE 的 PGS 流 `-c copy` 塞进 MKV：造出只有图形轨 / 文本+PGS 并存两种片子。"""

    if not PGS_SUP.is_file():
        pytest.skip(f"缺少 PGS 样本：{PGS_SUP}（可从 fate-suite.ffmpeg.org/sub/pgs_sub.sup 下载）")

    directory = tmp_path_factory.mktemp("m2-pgs")
    ffmpeg(
        "-i", str(media / "softsub.mkv"), "-i", str(PGS_SUP),
        "-map", "0", "-map", "1", "-c", "copy",
        str(directory / "mixed.mkv"),
    )
    ffmpeg(
        "-i", str(media / "softsub.mkv"), "-i", str(PGS_SUP),
        "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c", "copy",
        str(directory / "pgs_only.mkv"),
    )
    return directory


def test_ac3_real_pgs_only_is_rejected(pgs_media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=pgs_media / "pgs_only.mkv", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert result.status == "unsupported"
    assert result.exit_code == 2
    assert result.subtitle_path is None
    assert not (tmp_path / "out" / "source.srt").exists()
    assert manifest["decision"]["codec"] == "hdmv_pgs_subtitle"
    assert "图形" in manifest["decision"]["reason"]
    assert manifest["outputs"] == []


def test_ac3_real_pgs_alongside_text_extracts_text_and_warns(pgs_media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=pgs_media / "mixed.mkv", out=tmp_path / "out"))

    assert result.status == "extracted"
    assert parse_srt(result.subtitle_path.read_text(encoding="utf-8")) == GROUND_TRUTH
    assert any("图形" in warning for warning in manifest_of(result)["warnings"])


def test_cli_probe_real_pgs_returns_2(pgs_media, tmp_path, capsys) -> None:
    code = main(
        ["probe", "--src", str(pgs_media / "pgs_only.mkv"), "--out", str(tmp_path / "out")]
    )

    assert code == 2
    assert "图形" in capsys.readouterr().out


@pytest.fixture(scope="module")
def dvd_media(media, tmp_path_factory) -> Path:
    """VobSub（dvd_subtitle）：第二种图形字幕编码，`-c copy` 塞进 MKV。"""

    if not VOBSUB_IDX.is_file():
        pytest.skip(f"缺少 VobSub 样本：{VOBSUB_IDX}（可从 fate-suite.ffmpeg.org/sub/ 下载）")

    directory = tmp_path_factory.mktemp("m2-dvd")
    ffmpeg(
        "-i", str(media / "softsub.mkv"), "-i", str(VOBSUB_IDX),
        "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c", "copy",
        str(directory / "dvd_only.mkv"),
    )
    return directory


def test_ac3_real_dvd_subtitle_is_rejected(dvd_media, tmp_path) -> None:
    result = run_probe(ProbeOptions(src=dvd_media / "dvd_only.mkv", out=tmp_path / "out"))
    manifest = manifest_of(result)

    assert result.status == "unsupported"
    assert result.exit_code == 2
    assert manifest["decision"]["codec"] == "dvd_subtitle"
    assert "图形" in manifest["decision"]["reason"]


def test_extraction_failure_is_recorded_then_raised(media, tmp_path) -> None:
    def boom(*args, **kwargs):
        raise ExtractionError("模拟 ffmpeg 抽字幕失败")

    with pytest.raises(ExtractionError):
        run_probe(ProbeOptions(src=media / "softsub.mkv", out=tmp_path / "out"), extractor=boom)

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "extract_failed"
    assert any("抽取失败" in warning for warning in manifest["warnings"])
    assert manifest["outputs"] == []


# ---------------------------------------------------------------- CLI


def test_cli_probe_extracts_and_prints_paths(media, tmp_path, capsys) -> None:
    code = main(["probe", "--src", str(media / "softsub.mkv"), "--out", str(tmp_path / "out")])

    captured = capsys.readouterr()
    assert code == 0
    assert "source.srt" in captured.out
    assert "manifest.json" in captured.out
    assert "subrip" in captured.out


def test_cli_probe_asr_route_returns_0(media, tmp_path, capsys) -> None:
    code = main(["probe", "--src", str(media / "nosub.mp4"), "--out", str(tmp_path / "out")])

    assert code == 0
    assert "ASR" in capsys.readouterr().out


def test_cli_probe_forced_warns_on_stderr(media, tmp_path, capsys) -> None:
    code = main(["probe", "--src", str(media / "forced.mkv"), "--out", str(tmp_path / "out")])

    assert code == 0
    assert "forced" in capsys.readouterr().err


def test_cli_probe_no_extract_flag(media, tmp_path) -> None:
    code = main(
        [
            "probe",
            "--src",
            str(media / "softsub.mkv"),
            "--out",
            str(tmp_path / "out"),
            "--no-extract",
        ]
    )

    assert code == 0
    assert not (tmp_path / "out" / "source.srt").exists()


def test_cli_probe_missing_file_returns_1(tmp_path) -> None:
    code = main(["probe", "--src", str(tmp_path / "nope.mkv"), "--out", str(tmp_path / "out")])

    assert code == 1
