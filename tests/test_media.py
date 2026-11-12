"""ffmpeg / ffprobe 调用层（P1-3）：进程边界上唯一一处。

以前 9 个调用点各拼各的命令、各解各的 stderr、各写各的"找不到 ffmpeg"提示。
这里钉住的是**失败翻译规则**——因为它决定用户看到的错误到底有没有用。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cuesmith import media


class Boom(RuntimeError):
    """占位异常：验证 media 会把失败翻成调用方给的异常类型。"""


def fake_completed(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


# ---------------------------------------------------------------- exec_tool


def test_exec_tool_returns_output_on_success() -> None:
    completed = media.exec_tool(
        [sys.executable, "-c", "print('hi')"], error_cls=Boom, what="测试"
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == b"hi"


def test_exec_tool_translates_missing_binary() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool(["definitely-not-a-real-binary-xyz"], error_cls=Boom, what="测试")

    assert "找不到 definitely-not-a-real-binary-xyz" in str(excinfo.value)
    assert "PATH" in str(excinfo.value)


def test_exec_tool_supports_custom_missing_hint() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool(
            ["definitely-not-a-real-binary-xyz"],
            error_cls=Boom,
            what="测试",
            missing_hint="找不到 {tool}，请确认已安装 ffmpeg / demucs",
        )

    assert "ffmpeg / demucs" in str(excinfo.value)


def test_exec_tool_translates_nonzero_exit_with_stderr() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom detail'); sys.exit(3)"],
            error_cls=Boom,
            what="抽取音轨",
        )

    message = str(excinfo.value)
    assert message.startswith("抽取音轨失败：")
    assert "boom detail" in message


def test_exec_tool_reports_exit_code_when_no_output() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool([sys.executable, "-c", "raise SystemExit(7)"], error_cls=Boom, what="测试")

    assert "退出码 7" in str(excinfo.value)


def test_exec_tool_falls_back_to_stdout_when_stderr_empty() -> None:
    """volumedetect 之类把结果写 stderr，但也有工具只写 stdout——失败信息不能是空的。"""

    with pytest.raises(Boom) as excinfo:
        media.exec_tool(
            [sys.executable, "-c", "print('stdout detail'); raise SystemExit(2)"],
            error_cls=Boom,
            what="测试",
        )

    assert "stdout detail" in str(excinfo.value)


def test_exec_tool_check_false_does_not_raise() -> None:
    completed = media.exec_tool(
        [sys.executable, "-c", "raise SystemExit(9)"],
        error_cls=Boom,
        what="测试",
        check=False,
    )

    assert completed.returncode == 9


def test_exec_tool_translates_timeout() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            error_cls=Boom,
            what="混音",
            timeout_s=0.2,
        )

    message = str(excinfo.value)
    assert "混音超时" in message
    assert "0.2" in message


def test_exec_tool_truncates_long_stderr() -> None:
    with pytest.raises(Boom) as excinfo:
        media.exec_tool(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x' * 5000); raise SystemExit(1)",
            ],
            error_cls=Boom,
            what="测试",
        )

    assert len(str(excinfo.value)) < 5000 + media.STDERR_TAIL_CHARS


# ---------------------------------------------------------------- 便捷封装


def test_run_ffmpeg_prepends_global_flags(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_exec(argv, **kwargs):  # noqa: ANN001, ARG001
        captured.append(list(argv))
        return fake_completed()

    monkeypatch.setattr(media, "exec_tool", fake_exec)

    media.run_ffmpeg(["-i", "in.mp4", "out.mp4"], error_cls=Boom)

    assert captured[0] == [media.FFMPEG, "-v", "error", "-y", "-i", "in.mp4", "out.mp4"]


def test_run_ffmpeg_supports_global_flag_override(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_exec(argv, **kwargs):  # noqa: ANN001, ARG001
        captured.append(list(argv))
        return fake_completed()

    monkeypatch.setattr(media, "exec_tool", fake_exec)

    media.run_ffmpeg(
        ["-i", "in.mkv"], error_cls=Boom, global_flags=("-nostdin", "-v", "error", "-y")
    )

    assert captured[0][:3] == [media.FFMPEG, "-nostdin", "-v"]


def test_run_ffprobe_prepends_binary(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_exec(argv, **kwargs):  # noqa: ANN001, ARG001
        captured.append(list(argv))
        return fake_completed()

    monkeypatch.setattr(media, "exec_tool", fake_exec)

    media.run_ffprobe(["-show_format", "x.mp4"], error_cls=Boom)

    assert captured[0] == [media.FFPROBE, "-show_format", "x.mp4"]


# ---------------------------------------------------------------- ffprobe_json / duration_ms


def make_ffprobe(payload, monkeypatch):
    captured: list[list[str]] = []

    def fake_run_ffprobe(args, **kwargs):  # noqa: ANN001, ARG001
        captured.append(list(args))
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return fake_completed(raw.encode("utf-8"))

    monkeypatch.setattr(media, "run_ffprobe", fake_run_ffprobe)
    return captured


def test_ffprobe_json_requests_format_and_streams(monkeypatch) -> None:
    captured = make_ffprobe({"format": {"duration": "1.5"}, "streams": []}, monkeypatch)

    payload = media.ffprobe_json(Path("a.mp4"), error_cls=Boom, streams=True)

    assert payload["format"]["duration"] == "1.5"
    assert "-show_format" in captured[0]
    assert "-show_streams" in captured[0]
    assert captured[0][-1] == "a.mp4"


def test_ffprobe_json_omits_streams_by_default(monkeypatch) -> None:
    captured = make_ffprobe({"format": {}}, monkeypatch)

    media.ffprobe_json(Path("a.mp4"), error_cls=Boom)

    assert "-show_streams" not in captured[0]


def test_ffprobe_json_rejects_invalid_json(monkeypatch) -> None:
    make_ffprobe("{not json", monkeypatch)

    with pytest.raises(Boom, match="不是合法 JSON"):
        media.ffprobe_json(Path("a.mp4"), error_cls=Boom)


def test_ffprobe_json_rejects_non_object(monkeypatch) -> None:
    make_ffprobe("[]", monkeypatch)

    with pytest.raises(Boom, match="不是 JSON 对象"):
        media.ffprobe_json(Path("a.mp4"), error_cls=Boom)


def test_duration_ms_reads_format_duration(monkeypatch) -> None:
    make_ffprobe({"format": {"duration": "12.3456"}}, monkeypatch)

    assert media.duration_ms(Path("a.mp4"), error_cls=Boom) == 12346


def test_duration_ms_raises_when_missing(monkeypatch) -> None:
    make_ffprobe({"format": {}}, monkeypatch)

    with pytest.raises(Boom, match="读不出时长"):
        media.duration_ms(Path("a.mp4"), error_cls=Boom)


def test_duration_ms_against_real_ffmpeg(tmp_path: Path) -> None:
    """真 ffmpeg 生成 1.5s 静音，验端到端（ffprobe 不在 PATH 时跳过）。"""

    if __import__("shutil").which("ffmpeg") is None:
        pytest.skip("ffmpeg 不在 PATH")

    wav = tmp_path / "tone.wav"
    media.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.5",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        error_cls=Boom,
        what="生成测试音频",
    )

    duration = media.duration_ms(wav, error_cls=Boom)

    assert 1400 <= duration <= 1600
