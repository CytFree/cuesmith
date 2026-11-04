"""FR-4 真实媒体端到端：烧显式标识 + 写隐式元数据 + 回读核验。

对应验收：
- AC-10 输出视频起始 3 秒内可见显式标识（抽帧 + 像素差值核验）
- AC-11 `ffprobe -show_format` 可读出全部隐式标识字段
- AC-12 除 `--self-use`（写留痕日志）外，没有任何参数能移除标识

本机的坑（已实测，写进测试防回归）：
- 自定义元数据键 `content_id` 不加 `-movflags use_metadata_tags` 会被 MP4 **静默丢弃**
- 字体路径不存在时 ffmpeg 不是报错，而是**访问违例崩溃**（0xC0000005）→ 必须先自己校验
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from submatrix.cli import main
from submatrix.mark import (
    DEFAULT_COMMENT,
    METADATA_FIELDS,
    MarkError,
    MarkOptions,
    run_mark,
    verify_output,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg / ffprobe 不在 PATH"
)

LABEL_SECONDS = 3.0


def ffmpeg(*args: str) -> None:
    completed = subprocess.run(
        [FFMPEG, "-v", "error", "-y", *args], capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")


def probe_json(path: Path, *extra: str) -> dict:
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", *extra, str(path)],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return json.loads(completed.stdout)


def tags_of(path: Path) -> dict[str, str]:
    return probe_json(path, "-show_format")["format"].get("tags", {})


def duration_of(path: Path) -> float:
    return float(probe_json(path, "-show_format")["format"]["duration"])


def frame_gray(path: Path, at: float) -> bytes:
    completed = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{at:.3f}",
            "-frames:v",
            "1",
            "-vf",
            "format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout


def diff_ratio(a: bytes, b: bytes, threshold: int = 40) -> float:
    assert a and len(a) == len(b)
    return sum(1 for x, y in zip(a, b) if abs(x - y) > threshold) / len(a)


def source_minus_output(source: Path, output: Path, at: float) -> float:
    return diff_ratio(frame_gray(source, at), frame_gray(output, at))


# ---------------------------------------------------------------- 素材


@pytest.fixture(scope="module")
def short_video(tmp_path_factory) -> Path:
    """6 秒 640x360 纯色片 + 440Hz 正弦音：抽帧差值可判定、编码快。"""

    directory = tmp_path_factory.mktemp("m3-media")
    path = directory / "src.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=#141414:s=640x360:d=6:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(path),
    )
    return path


@pytest.fixture(scope="module")
def mkv_video(short_video, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("m3-mkv") / "src.mkv"
    ffmpeg("-i", str(short_video), "-c", "copy", str(path))
    return path


def marked(video: Path, out: Path, **kwargs) -> object:
    return run_mark(MarkOptions(src=video, out=out, **kwargs))


# ---------------------------------------------------------------- AC-10 / AC-11


def test_marks_label_and_metadata(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert result.status == "marked"
    assert result.exit_code == 0
    assert result.video_path.is_file()
    assert result.video_path.name == "marked.mp4"
    assert report["command"] == "mark"
    assert report["status"] == "marked"
    assert report["config"]["label_text"] == "AI 生成"
    assert report["config"]["label_seconds"] == LABEL_SECONDS
    assert report["config"]["position"] == "top-right"


def test_ac11_metadata_is_readable_by_ffprobe(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    tags = tags_of(result.video_path)

    expected = report["metadata"]
    assert set(expected) == set(METADATA_FIELDS)
    for field, value in expected.items():
        assert tags.get(field) == value, f"{field} 应可被 ffprobe 读出"

    assert tags["comment"] == DEFAULT_COMMENT
    assert tags["artist"] == "submatrix"


def test_ac11_content_id_is_actually_inside_the_container(short_video, tmp_path) -> None:
    """自定义键要靠 mdta 原子写进容器；不加 use_metadata_tags 会被静默丢弃。"""

    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["metadata"]["content_id"].encode() in result.video_path.read_bytes()


def test_ac10_label_is_visible_in_first_seconds_and_gone_after(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    during = source_minus_output(short_video, result.video_path, 0.5)
    late = source_minus_output(short_video, result.video_path, 3.8)

    assert during > 0.0005, f"起始画面应有标识（差值 {during:.4f}）"
    assert late < during / 3, f"标识结束后画面应与源片一致（差值 {late:.4f}）"
    assert report["verification"]["explicit_label"]["ok"] is True


def test_ac10_verification_report_has_sample_points(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    explicit = report["verification"]["explicit_label"]

    labeled = [sample for sample in explicit["samples"] if sample["expected"] == "label"]
    unlabeled = [sample for sample in explicit["samples"] if sample["expected"] == "none"]

    assert len(labeled) == 2
    assert len(unlabeled) == 1
    assert all(sample["diff"] >= explicit["threshold"] for sample in labeled)
    assert unlabeled[0]["diff"] < explicit["threshold"]


def test_label_frame_evidence_is_written(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")

    assert result.label_frame is not None
    assert result.label_frame.is_file()
    raw = result.label_frame.read_bytes()
    assert raw.startswith(b"\x89PNG")
    assert result.label_frame.stat().st_size > 1000


def test_output_keeps_duration_and_audio(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    streams = probe_json(result.video_path, "-show_streams")["streams"]

    assert abs(duration_of(result.video_path) - duration_of(short_video)) <= 0.15
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "44100"


def test_output_is_faststart(short_video, tmp_path) -> None:
    """moov 原子在前：边下边播，也是"文件本身自带标识"的可分享前提。"""

    result = marked(short_video, tmp_path / "out")
    raw = result.video_path.read_bytes()

    assert raw.index(b"moov") < raw.index(b"mdat")


def test_mkv_input_is_accepted(short_video, mkv_video, tmp_path) -> None:
    result = marked(mkv_video, tmp_path / "out")

    assert result.status == "marked"
    assert probe_json(result.video_path, "-show_format")["format"]["format_name"].startswith("mov")


def test_label_text_with_special_characters(short_video, tmp_path) -> None:
    """文案走 textfile，不经过滤器解析：冒号/逗号/方括号/百分号都不会炸。"""

    label = "AI: 生成, 100% [测试]"
    result = marked(short_video, tmp_path / "out", label_text=label)

    assert result.status == "marked"
    assert (tmp_path / "out" / "label.txt").read_text(encoding="utf-8") == label
    assert source_minus_output(short_video, result.video_path, 0.5) > 0.0005


def test_content_id_differs_per_run(short_video, tmp_path) -> None:
    first = marked(short_video, tmp_path / "a")
    second = marked(short_video, tmp_path / "b")

    assert (
        json.loads(first.report_path.read_text(encoding="utf-8"))["metadata"]["content_id"]
        != json.loads(second.report_path.read_text(encoding="utf-8"))["metadata"]["content_id"]
    )


def test_report_records_input_hash_and_output(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["input"]["sha256"] == hashlib.sha256(short_video.read_bytes()).hexdigest()
    assert report["output"]["sha256"] == hashlib.sha256(result.video_path.read_bytes()).hexdigest()
    assert report["output"]["bytes"] == result.video_path.stat().st_size
    assert report["verification"]["metadata_roundtrip"]["ok"] is True


# ---------------------------------------------------------------- AC-12 自用模式


def test_self_use_writes_notice_and_adds_no_markers(short_video, tmp_path) -> None:
    result = marked(short_video, tmp_path / "out", self_use=True)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    notice = json.loads((tmp_path / "out" / "self_use_notice.json").read_text(encoding="utf-8"))

    assert result.status == "self_use"
    assert result.exit_code == 0
    assert report["status"] == "self_use"
    assert "仅限自用" in notice["notice"]
    assert "第十条" in notice["notice"]
    assert notice["input"]["sha256"] == hashlib.sha256(short_video.read_bytes()).hexdigest()
    assert notice["generated_at"]

    tags = tags_of(result.video_path)
    assert tags.get("comment") != DEFAULT_COMMENT
    assert "content_id" not in tags
    # 没有烧标识：与源片像素一致
    assert source_minus_output(short_video, result.video_path, 0.5) < 0.001


def test_verifier_detects_missing_label(short_video, tmp_path) -> None:
    """核验逻辑必须真的能发现"标识没烧上"，否则 AC-10 就是自欺欺人。"""

    result = marked(short_video, tmp_path / "out", self_use=True)
    verification = verify_output(
        source=short_video,
        output=result.video_path,
        expected_metadata={},
        label_seconds=LABEL_SECONDS,
    )

    assert verification["explicit_label"]["ok"] is False


def test_verify_failure_yields_exit_code_2(short_video, tmp_path) -> None:
    def failing_verifier(**kwargs) -> dict:
        return {
            "metadata_roundtrip": {"ok": True, "fields": {}, "missing": []},
            "explicit_label": {"ok": False, "samples": [], "threshold": 0.0005},
        }

    result = run_mark(
        MarkOptions(src=short_video, out=tmp_path / "out"), verifier=failing_verifier
    )
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert result.status == "verify_failed"
    assert result.exit_code == 2
    assert report["status"] == "verify_failed"


def test_missing_font_file_is_rejected_before_ffmpeg_runs(short_video, tmp_path) -> None:
    with pytest.raises(MarkError, match="字体"):
        marked(short_video, tmp_path / "out", font_file=tmp_path / "nope.ttf")

    assert not (tmp_path / "out" / "marked.mp4").exists()


# ---------------------------------------------------------------- CLI


def test_cli_mark_ok(short_video, tmp_path, capsys) -> None:
    code = main(["mark", "--src", str(short_video), "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()

    assert code == 0
    assert "marked.mp4" in captured.out
    assert "核验" in captured.out


def test_cli_self_use_warns_on_stderr(short_video, tmp_path, capsys) -> None:
    code = main(
        ["mark", "--src", str(short_video), "--out", str(tmp_path / "out"), "--self-use"]
    )

    assert code == 0
    assert "仅限自用" in capsys.readouterr().err


def test_cli_accepts_label_options(short_video, tmp_path) -> None:
    code = main(
        [
            "mark",
            "--src", str(short_video),
            "--out", str(tmp_path / "out"),
            "--label-text", "AI 生成内容",
            "--label-seconds", "5",
            "--position", "bottom-left",
            "--provider", "某工作室",
            "--comment", "AI 翻译 + AI 配音",
        ]
    )
    report = json.loads((tmp_path / "out" / "mark.json").read_text(encoding="utf-8"))

    assert code == 0
    assert report["config"]["label_text"] == "AI 生成内容"
    assert report["config"]["label_seconds"] == 5.0
    assert report["config"]["position"] == "bottom-left"
    assert report["metadata"]["artist"] == "某工作室"
    assert report["metadata"]["comment"] == "AI 翻译 + AI 配音"


@pytest.mark.parametrize(
    "flag", ["--no-label", "--no-metadata", "--strip-metadata", "--unmarked", "--remove-label"]
)
def test_ac12_no_removal_flags_exist(flag: str, short_video, tmp_path) -> None:
    """AC-12：除自用模式外，不提供任何移除标识的途径（未知参数一律拒绝）。"""

    code = main(["mark", "--src", str(short_video), "--out", str(tmp_path / "o"), flag])

    assert code == 1


def test_ac12_help_exposes_only_self_use_escape(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["mark", "--help"])

    help_text = capsys.readouterr().out
    assert "--self-use" in help_text
    assert "--no-label" not in help_text
    assert "--strip" not in help_text


def test_cli_missing_input_returns_1(tmp_path) -> None:
    code = main(["mark", "--src", str(tmp_path / "nope.mp4"), "--out", str(tmp_path / "out")])

    assert code == 1


def test_cli_short_label_seconds_returns_1(short_video, tmp_path) -> None:
    code = main(
        [
            "mark",
            "--src", str(short_video),
            "--out", str(tmp_path / "out"),
            "--label-seconds", "1",
        ]
    )

    assert code == 1
