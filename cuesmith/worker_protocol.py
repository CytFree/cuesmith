"""worker 子进程协议：主包与"跑在各自 venv 里的 worker 脚本"之间的唯一约定。

背景
----
ASR / 说话人分离 / 本地 TTS 三个 worker 分别跑在装了重依赖（faster-whisper、
sherpa-onnx）的独立 venv 里，主包只通过**子进程 + JSON** 跟它们说话。协议只有三件事：
怎么交输入、怎么回结果、怎么报错——但以前三个 worker 各写一套：payload 键名不同、
traceback 截断长度不同、有的写进度有的不写，父进程侧的解析代码还逐字复制了两遍。
这个模块把这三件事收成一份实现。

两种形态
--------
1. **一次性**（asr / diarize）：worker 把结果写进 ``--output`` 指定的 JSON 文件，
   父进程用 :func:`run_once` 读回来。
2. **常驻**（tts）：worker 在 stdout 上按行读写 JSONL（模型只加载一次、合成多次），
   父进程客户端在 :mod:`cuesmith.localtts`；两侧共用 :func:`error_payload` /
   :func:`describe_error` 的失败形状。

统一的 payload 形状::

    {"ok": true,  ...业务字段}
    {"ok": false, "error": "<异常类型>: <消息>", "traceback": "<截断后的栈>"}

为什么这个文件不能有 ``from .`` 相对导入
----------------------------------------
worker 是 ``python cuesmith/asr_worker.py`` 直接执行的，``sys.path[0]`` 是
``cuesmith/``，只能 ``import worker_protocol``；主包侧则走
``from .worker_protocol import ...``。两条导入路径都成立的前提是本文件只用标准库。
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

__all__ = [
    "STDERR_TAIL_CHARS",
    "TRACEBACK_CHARS",
    "TextStream",
    "describe_error",
    "emit_progress",
    "error_payload",
    "ok_payload",
    "run_once",
    "subprocess_runner",
    "write_line",
    "write_payload",
]

# traceback 只留尾巴（真实栈深可能几百行，全塞进报告没意义）；父进程错误里同样只留一段
TRACEBACK_CHARS = 2000
STDERR_TAIL_CHARS = 600


# ---------------------------------------------------------------------- worker 侧


def ok_payload(**fields: object) -> dict[str, object]:
    """成功 payload：``{"ok": true, ...}``。"""

    return {"ok": True, **fields}


def error_payload(exc: BaseException, **extra: object) -> dict[str, object]:
    """失败 payload：``{"ok": false, "error", "traceback"}`` + 业务上下文。

    worker 里所有 ``except Exception`` 都应该走这里——**失败也要落一份 JSON**，
    否则父进程只能拿到"进程非 0 退出 + 一段 stderr"，出错原因就丢了。
    """

    return {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc()[-TRACEBACK_CHARS:],
        **extra,
    }


class TextStream(Protocol):
    """协议通道只要"能写一行文本并 flush"：``sys.stdout`` / ``StringIO`` / 管道都满足。

    以前这里标的是 ``object``，于是 ``stream.write`` 每行都得压一条告警；标成协议之后，
    调用方传什么由类型本身说明白，也不影响运行时。
    """

    def write(self, data: str) -> object: ...

    def flush(self) -> object: ...


def write_line(stream: TextStream, payload: Mapping[str, object]) -> None:
    """把 payload 作为一行 JSON 写到给定流并 flush（常驻 worker 的协议通道）。"""

    stream.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
    stream.flush()


def write_payload(
    path: Path | str | None,
    payload: Mapping[str, object],
    *,
    indent: int | None = None,
) -> None:
    """把 payload 落成 JSON 文件（一次性 worker）或一行 stdout（``path=None``）。

    一律 UTF-8 + ``\\n`` 结尾，父进程按 ``utf-8-sig`` 读，BOM 与否都能吃。
    """

    text = json.dumps(dict(payload), ensure_ascii=False, indent=indent) + "\n"
    if path is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def emit_progress(message: str) -> None:
    """往 stderr 打一行进度。

    为什么是 stderr：stdout 在常驻 worker 里是协议通道（一行一个 JSON），进度混进去就
    解析失败；而父进程把这些 stderr 原样转给用户，长任务才不是"卡住了"。
    """

    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------- 父进程侧


def describe_error(payload: Mapping[str, object]) -> str:
    """把失败 payload 拼成可读的一段（错误 + traceback 尾巴）。"""

    error = str(payload.get("error") or "未知错误")
    tail = str(payload.get("traceback") or "").strip()[-STDERR_TAIL_CHARS:]
    return f"{error}\n{tail}" if tail else error


def subprocess_runner(timeout_s: float) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    """造一个"跑子进程并抓输出"的 runner（各 worker 的超时不同，其余一致）。"""

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - 命令由本包自己拼装，不经过 shell
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
        )

    return run


def run_once(
    command: Sequence[str],
    output_path: Path | str,
    *,
    error_cls: type[Exception],
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    label: str,
    failure_label: str | None = None,
) -> dict[str, object]:
    """跑一次一次性 worker，返回它的 payload；任何失败都翻成 ``error_cls``。

    "失败"包括三种常见形态，父进程不该各写一遍：进程超时、没产出结果文件、
    以及产出了但 ``ok=false``（worker 内部异常）。

    ``label`` 用于"进程"层面的措辞（如 ``"ASR"``），``failure_label`` 用于
    业务失败（如 ``"ASR 识别"``）；不传则复用 ``label``。
    """

    try:
        completed = runner(command)
    except subprocess.TimeoutExpired as exc:
        raise error_cls(f"{label}进程超时：{exc}") from exc

    path = Path(output_path)
    if not path.is_file():
        tail = (getattr(completed, "stderr", "") or "").strip()[-STDERR_TAIL_CHARS:]
        raise error_cls(f"{label}进程没有产出结果（退出码 {completed.returncode}）：{tail}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise error_cls(f"{label}进程写出的结果不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise error_cls(f"{label}进程写出的结果不是 JSON 对象")

    if not payload.get("ok"):
        raise error_cls(f"{failure_label or label}失败：{describe_error(payload)}")
    return payload
