"""worker 协议（主包 ↔ 各自 venv 里的 worker 脚本）。

以前三个 worker 各写一套 payload/错误壳：traceback 截断长度不同、进度有的写有的不写，
父进程侧解析代码逐字复制两遍。这里把协议的每条约定都钉住，免得再漂回去。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cuesmith import worker_protocol as protocol

WORKER_SCRIPTS = ("asr_worker.py", "diarize_worker.py", "tts_worker.py")


# ---------------------------------------------------------------- worker 侧


def test_ok_payload_marks_success_and_keeps_fields() -> None:
    payload = protocol.ok_payload(language="zh", wall_s=1.25)

    assert payload == {"ok": True, "language": "zh", "wall_s": 1.25}


def test_error_payload_shape_includes_type_and_traceback() -> None:
    try:
        raise ValueError("模型没找到")
    except ValueError as exc:
        payload = protocol.error_payload(exc, stage="load")

    assert payload["ok"] is False
    assert payload["error"].startswith("ValueError: ")
    assert "模型没找到" in str(payload["error"])
    assert "Traceback" in str(payload["traceback"])
    assert payload["stage"] == "load"


def test_error_payload_truncates_long_traceback() -> None:
    deep = "x" * (protocol.TRACEBACK_CHARS * 3)

    class Boom(Exception):
        pass

    try:
        raise Boom(deep)
    except Boom as exc:
        payload = protocol.error_payload(exc)

    # 只留尾巴：完整栈可能有几百行，塞进报告没有意义
    assert len(str(payload["traceback"])) <= protocol.TRACEBACK_CHARS


def test_write_payload_creates_parents_and_ends_with_newline(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "worker.json"

    protocol.write_payload(target, {"ok": True, "text": "你好"})

    raw = target.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == {"ok": True, "text": "你好"}
    assert "你好" in raw  # 不转义成 \uXXXX，报告要人眼可读


def test_write_payload_supports_indent(tmp_path: Path) -> None:
    target = tmp_path / "pretty.json"

    protocol.write_payload(target, {"ok": True}, indent=2)

    assert target.read_text(encoding="utf-8").splitlines()[1].startswith("  ")


def test_write_payload_without_path_writes_one_line_to_stdout(capsys) -> None:
    protocol.write_payload(None, {"ok": True, "ready": True})

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == {"ok": True, "ready": True}


def test_emit_progress_goes_to_stderr_only(capsys) -> None:
    protocol.emit_progress("… 25 段 / 300 词")

    captured = capsys.readouterr()
    assert "25 段" in captured.err
    assert captured.out == ""  # stdout 是常驻 worker 的协议通道，不能被进度污染


def test_worker_scripts_can_import_protocol_in_script_mode() -> None:
    """worker 是 ``python cuesmith/xxx_worker.py`` 直跑的：sys.path[0] 是 cuesmith/，
    此时只能 ``import worker_protocol``。跑一次 --help 就是这条导入路径的回归测试。
    """

    package_dir = Path(protocol.__file__).parent
    for name in WORKER_SCRIPTS:
        completed = subprocess.run(
            [sys.executable, str(package_dir / name), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert completed.returncode == 0, f"{name} 脚本直跑失败：{completed.stderr[-500:]}"


# ---------------------------------------------------------------- 父进程侧


def test_describe_error_includes_traceback_tail() -> None:
    text = protocol.describe_error(
        {"ok": False, "error": "RuntimeError: boom", "traceback": "line1\nline2"}
    )

    assert text.startswith("RuntimeError: boom")
    assert "line2" in text


def test_describe_error_handles_missing_fields() -> None:
    assert protocol.describe_error({}) == "未知错误"
    assert protocol.describe_error({"error": "E"}) == "E"


def test_describe_error_truncates_long_traceback() -> None:
    text = protocol.describe_error({"error": "E", "traceback": "y" * 5000})

    assert len(text) <= len("E\n") + protocol.STDERR_TAIL_CHARS


def test_subprocess_runner_captures_output() -> None:
    run = protocol.subprocess_runner(30)

    completed = run([sys.executable, "-c", "print('hi')"])

    assert completed.returncode == 0
    assert completed.stdout.strip() == "hi"


def test_subprocess_runner_enforces_timeout() -> None:
    run = protocol.subprocess_runner(0.2)

    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, "-c", "import time; time.sleep(5)"])


def make_runner(payload: dict[str, object] | str | None, *, stderr: str = "", code: int = 0):
    """假 runner：把 payload 写到命令里的 ``--output``（与真 worker 同契约）。"""

    calls: list[list[str]] = []

    def run(command):
        calls.append(list(command))
        if payload is not None:
            output = Path(command[command.index("--output") + 1])
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            output.write_text(raw, encoding="utf-8")
        return subprocess.CompletedProcess(command, code, "", stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_run_once_returns_payload(tmp_path: Path) -> None:
    out = tmp_path / "w.json"
    runner = make_runner({"ok": True, "segments": []})

    payload = protocol.run_once(
        ["py", "worker.py", "--output", str(out)],
        out,
        error_cls=RuntimeError,
        runner=runner,
        label="测试",
    )

    assert payload["ok"] is True
    assert payload["segments"] == []
    assert runner.calls  # type: ignore[attr-defined]


def test_run_once_reports_missing_output_with_stderr_tail(tmp_path: Path) -> None:
    out = tmp_path / "w.json"
    runner = make_runner(None, stderr="ModuleNotFoundError: No module named 'x'", code=1)

    with pytest.raises(RuntimeError) as excinfo:
        protocol.run_once(
            ["py", "worker.py", "--output", str(out)],
            out,
            error_cls=RuntimeError,
            runner=runner,
            label="ASR",
        )

    message = str(excinfo.value)
    assert "ASR进程没有产出结果" in message
    assert "退出码 1" in message
    assert "ModuleNotFoundError" in message


def test_run_once_translates_worker_failure(tmp_path: Path) -> None:
    out = tmp_path / "w.json"
    runner = make_runner(
        {"ok": False, "error": "RuntimeError: 模型加载失败", "traceback": "  at load\n"}
    )

    with pytest.raises(RuntimeError) as excinfo:
        protocol.run_once(
            ["py", "worker.py", "--output", str(out)],
            out,
            error_cls=RuntimeError,
            runner=runner,
            label="ASR",
            failure_label="ASR 识别",
        )

    message = str(excinfo.value)
    assert message.startswith("ASR 识别失败：")
    assert "模型加载失败" in message
    assert "at load" in message


def test_run_once_rejects_invalid_json(tmp_path: Path) -> None:
    out = tmp_path / "w.json"
    runner = make_runner("{not json")

    with pytest.raises(RuntimeError, match="不是合法 JSON"):
        protocol.run_once(
            ["py", "worker.py", "--output", str(out)],
            out,
            error_cls=RuntimeError,
            runner=runner,
            label="测试",
        )


def test_run_once_rejects_non_object_payload(tmp_path: Path) -> None:
    out = tmp_path / "w.json"
    runner = make_runner("[]")

    with pytest.raises(RuntimeError, match="不是 JSON 对象"):
        protocol.run_once(
            ["py", "worker.py", "--output", str(out)],
            out,
            error_cls=RuntimeError,
            runner=runner,
            label="测试",
        )


def test_run_once_translates_timeout(tmp_path: Path) -> None:
    def runner(command):  # noqa: ANN001, ARG001
        raise subprocess.TimeoutExpired(str(tmp_path), 1)

    with pytest.raises(RuntimeError, match="测试进程超时"):
        protocol.run_once(
            ["py", "worker.py", "--output", str(tmp_path / "w.json")],
            tmp_path / "w.json",
            error_cls=RuntimeError,
            runner=runner,
            label="测试",
        )
