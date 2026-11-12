"""ffmpeg / ffprobe 调用层：进程边界上唯一一处。

背景
----
ffmpeg 调用以前散在 6 个模块的 9 个函数里，每个都自己拼 ``-v error -y``、自己
``decode("utf-8", "replace")``、自己拼"找不到 ffmpeg"的提示，异常类型各用各的
（ProbeError / ExtractionError / BurnError / MarkError / DiarizeError / DubError / BgmError）：
同一个失败在不同阶段文案不同，stderr 截断还有 500 / 800 / 不截断三种，而且
``FileNotFoundError``（没装 ffmpeg）只在部分调用点被翻译成人话。

这里只统一**怎么起进程、怎么把失败翻译成可读错误**；抛什么异常、文案主语是什么，
仍由调用方通过 ``error_cls`` / ``what`` 决定——那是各阶段的业务语义，不该被抽象掉。

约定
----
- stdout/stderr 保持 **bytes**（有的调用点要 rawvideo 原始帧，不能强转文本）；
- 失败信息优先取 stderr，没有就取 stdout（volumedetect 之类把结果写 stderr）；
- ``check=False`` 用于"调用方自己判断退出码/输出"的场景（抽帧、电平检测）。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

__all__ = [
    "DEFAULT_FFMPEG_FLAGS",
    "FFMPEG",
    "FFPROBE",
    "STDERR_TAIL_CHARS",
    "duration_ms",
    "exec_tool",
    "ffprobe_json",
    "run_ffmpeg",
    "run_ffprobe",
    "stderr_text",
]

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
STDERR_TAIL_CHARS = 800
# ffmpeg 的全局参数（安静 + 覆盖输出）：只在 run_ffmpeg 的默认值和各模块的转发里引用一次
DEFAULT_FFMPEG_FLAGS = ("-v", "error", "-y")

DEFAULT_MISSING_HINT = "找不到 {tool}，请确认已装 ffmpeg 并加入 PATH"


def _text(raw: object) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def stderr_text(completed: subprocess.CompletedProcess[bytes]) -> str:
    """子进程 stderr 解成字符串（调用方要自己拼错误文案时用）。"""

    return _text(getattr(completed, "stderr", b"")).strip()


def _failure_detail(completed: subprocess.CompletedProcess[bytes]) -> str:
    detail = _text(getattr(completed, "stderr", b"")).strip()
    if not detail:
        detail = _text(getattr(completed, "stdout", b"")).strip()
    return detail[-STDERR_TAIL_CHARS:]


def exec_tool(
    argv: Sequence[str],
    *,
    error_cls: type[Exception],
    what: str,
    check: bool = True,
    timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
    missing_hint: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """跑一个外部工具（``argv[0]`` 是可执行文件名），失败翻成 ``error_cls``。

    ``missing_hint`` 用于工具不止一个组件的场景（bgm 会同时调 ffmpeg 和 demucs），
    默认提示是"没装 ffmpeg"。
    """

    commands = list(argv)
    try:
        completed = subprocess.run(  # noqa: S603 - 命令由本包自己拼装，不经过 shell
            commands,
            capture_output=True,
            timeout=timeout_s,
            env=None if env is None else dict(env),
        )
    except FileNotFoundError as exc:
        hint = missing_hint or DEFAULT_MISSING_HINT
        raise error_cls(hint.format(tool=commands[0])) from exc
    except subprocess.TimeoutExpired as exc:
        limit = "" if timeout_s is None else f"（>{timeout_s:g}s）"
        raise error_cls(f"{what}超时{limit}") from exc
    except OSError as exc:
        raise error_cls(f"启动 {commands[0]} 失败：{exc}") from exc

    if check and completed.returncode != 0:
        detail = _failure_detail(completed) or f"退出码 {completed.returncode}"
        raise error_cls(f"{what}失败：{detail}")
    return completed


def run_ffmpeg(
    args: Sequence[str],
    *,
    error_cls: type[Exception],
    what: str = "ffmpeg 处理",
    check: bool = True,
    timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
    global_flags: Sequence[str] = DEFAULT_FFMPEG_FLAGS,
    missing_hint: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """跑 ffmpeg：默认前置 ``-v error -y``，需要 ``-nostdin`` 等全局参数时传 ``global_flags``。

    ``global_flags`` 与缺工具提示都只在这里定义：调用方永远只给"输入/滤镜/编码"参数，
    免得各模块又各拼一份 ``[FFMPEG, "-v", "error", "-y", ...]``。
    """

    return exec_tool(
        [FFMPEG, *global_flags, *args],
        error_cls=error_cls,
        what=what,
        check=check,
        timeout_s=timeout_s,
        env=env,
        missing_hint=missing_hint,
    )


def run_ffprobe(
    args: Sequence[str],
    *,
    error_cls: type[Exception],
    what: str = "ffprobe 探测",
    check: bool = True,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return exec_tool(
        [FFPROBE, *args], error_cls=error_cls, what=what, check=check, timeout_s=timeout_s
    )


def ffprobe_json(
    path: Path | str,
    *,
    error_cls: type[Exception],
    what: str | None = None,
    streams: bool = False,
    show_format: bool = True,
) -> dict[str, object]:
    """ffprobe 输出解析成 dict；``streams`` 控制是否带 ``-show_streams``。"""

    args = ["-v", "error", "-print_format", "json"]
    if show_format:
        args.append("-show_format")
    if streams:
        args.append("-show_streams")
    args.append(str(path))
    label = what or f"ffprobe 读 {Path(path).name}"
    completed = run_ffprobe(args, error_cls=error_cls, what=label)
    try:
        payload = json.loads(_text(completed.stdout) or "{}")
    except json.JSONDecodeError as exc:
        raise error_cls(f"ffprobe 输出不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise error_cls("ffprobe 输出不是 JSON 对象")
    return payload


def duration_ms(
    path: Path | str, *, error_cls: type[Exception], what: str | None = None
) -> int:
    """读容器时长（音/视频通吃），毫秒。"""

    payload = ffprobe_json(path, error_cls=error_cls, what=what)
    info = payload.get("format") or {}
    duration = info.get("duration") if isinstance(info, dict) else None
    if not duration:
        raise error_cls(f"读不出时长（{Path(path).name}）")
    return round(float(duration) * 1000)
