"""TTS 引擎适配层。

为什么不做"纯标准库 Edge-TTS 客户端"：当前 edge-tts 的 `Sec-MS-GEC` 令牌是一套高度混淆的
自研校验（CS64 + 定制 SHA-256），跟着它重写既不可维护也不该做。所以配音引擎做成**可替换**：

- :class:`EdgeTtsEngine`：调用本机已安装的 `edge-tts`（`python -m edge_tts`），
  解释器可用 ``--tts-python`` 指定（例如指向已装好 edge-tts 的虚拟环境）
- :class:`CommandTtsEngine`：任意外部命令模板（``{text_file}`` / ``{out}``），
  给云端克隆 API（MiniMax / Fish Audio / 火山 / CosyVoice）留插槽

编排层（并发 / 重试 / 时长校验）只依赖 ``synthesize(text, out_path)`` 这一个方法，
所以换引擎不影响 FR-3 的任何逻辑。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from .interpreters import (
    MISSING_CURRENT,
    Requirement,
    bool_probe,
    resolve,
    subprocess_probe,
)

__all__ = [
    "BASE_SPEECH_CPS",
    "CommandTtsEngine",
    "EdgeTtsEngine",
    "TtsEngine",
    "TtsError",
    "find_edge_tts_python",
    "format_rate",
    "parse_rate",
    "speech_capacity_cps",
    "speech_cps",
]

# 实测语速（Edge-TTS，2026-11-02 真实素材）：
#   中文 zh-CN-XiaoxiaoNeural 在 +30% 下 86 条平均 5.99 字/秒（中位 6.12）；
#   英文 en-US-AriaNeural 在 +20% 下 19 条平均 18.40 字符/秒（含空格）。
# 换算成"原速"：zh ≈ 5.99/1.3 = 4.6 字/秒，en ≈ 18.40/1.2 = 15.3 字符/秒。
# 用途：配音前估算「一个槽位能念多少字」，而不是拿字幕阅读速度（9/20 CPS）冒充。
BASE_SPEECH_CPS = {"zh": 4.6, "en": 15.3}


def parse_rate(rate: str | None) -> float:
    """``"+30%"`` → ``0.30``（``"-10%"`` → ``-0.10``）；空/不认识的格式按 0 处理。"""

    if not rate:
        return 0.0
    text = rate.strip().replace("%", "").replace("＋", "+").replace("－", "-")
    try:
        return float(text) / 100
    except ValueError:
        return 0.0


def format_rate(value: float) -> str:
    """``0.45`` → ``"+45%"``（Edge-TTS 的语法）。"""

    return f"{round(value * 100):+d}%"


def speech_cps(language: str | None, rate: str | None = None) -> float | None:
    """该语言在该语速下每秒能念多少字（未知语言返回 None）。"""

    base = BASE_SPEECH_CPS.get((language or "").lower())
    if base is None:
        return None
    return base * (1.0 + parse_rate(rate))


def speech_capacity_cps(
    language: str | None, rate: str | None = None, *, headroom: float = 1.6
) -> float | None:
    """配音槽位的**可压缩余量**折算后的字/秒上限。

    ``headroom`` 是「提语速 + atempo」还能再挤出多少倍：
    实测可把基准语速再提约 1.27 倍（+30% → +65%）、atempo 再压 1.4 倍，
    合计约 1.78×；这里取 1.6 保守值，只当"怎么都放不下"的保险丝。
    """

    value = speech_cps(language, rate)
    if value is None:
        return None
    return value * max(headroom, 1.0)


class TtsError(RuntimeError):
    """单条合成失败（可重试）。"""


class TtsEngine(Protocol):
    def synthesize(self, text: str, out_path: Path) -> None: ...


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _candidate_pythons() -> list[str]:
    """仓库里可能装了 edge-tts 的解释器（按优先级）。"""

    root = _repo_root()
    bases = [
        root / "spike" / ".venv311",
        root / "spike" / "pyvideotrans" / ".venv",
        root / ".venv",
        root / "venv",
    ]
    candidates: list[str] = []
    for base in bases:
        for relative in (Path("Scripts") / "python.exe", Path("bin") / "python"):
            path = base / relative
            if path.is_file():
                candidates.append(str(path))
    return candidates


_EDGE_TTS_PROBE = subprocess_probe("edge_tts", timeout_s=30.0)


def _probe_edge_tts(python: str) -> bool:
    """``[python, -c, "import edge_tts"]`` 探针（本体在 ``interpreters.subprocess_probe``）。

    超时沿用原实现的 30s；本地 TTS / demucs 的候选目录更大，那边用 60s。
    """

    return _EDGE_TTS_PROBE(python)[0]


_EDGE_TTS_PYTHON = Requirement(
    label="edge-tts",
    module="edge_tts",
    parameter="--tts-python",
    env_var="CUESMITH_TTS_PYTHON",
    hint="或先建好 spike/.venv311 并 pip install edge-tts==7.3.1。",
)


def find_edge_tts_python(
    explicit: str | None = None,
    *,
    candidates: list[str] | None = None,
    probe=None,
) -> str:
    """找第一个能 ``import edge_tts`` 的解释器；找不到退回当前解释器。

    顺序：显式 ``--tts-python`` → 环境变量 ``CUESMITH_TTS_PYTHON`` →
    当前解释器 → 仓库已知 venv（``spike/.venv311``、``pyvideotrans/.venv`` 等）。
    显式值与环境变量不做探测（尊重用户选择，真跑不通会在合成时报错）。
    """

    check = probe or _probe_edge_tts
    ordered = [sys.executable, *(candidates if candidates is not None else _candidate_pythons())]
    found = resolve(
        _EDGE_TTS_PYTHON,
        explicit,
        candidates=ordered,
        probe=bool_probe(check),
        on_missing=MISSING_CURRENT,
        trust_explicit=True,
        trust_env=True,
        error_cls=TtsError,
    )
    # MISSING_CURRENT 时 resolve 自己就退回 sys.executable；这里再兜一次是同值防御
    return found or sys.executable


@lru_cache(maxsize=1)
def _default_edge_tts_python() -> str:
    return find_edge_tts_python()


@dataclass(frozen=True, slots=True)
class EdgeTtsEngine:
    """免费 Edge-TTS 引擎（走 ``python -m edge_tts`` 子进程，避免引入 import 依赖）。"""

    voice: str = "en-US-AriaNeural"
    python: str | None = None
    rate: str | None = None
    timeout_s: float = 120.0

    def synthesize(self, text: str, out_path: Path) -> None:
        interpreter = self.python or sys.executable
        command = [
            interpreter,
            "-m",
            "edge_tts",
            "--voice",
            self.voice,
            "--text",
            text,
            "--write-media",
            str(out_path),
        ]
        if self.rate:
            command += ["--rate", self.rate]

        try:
            completed = subprocess.run(command, capture_output=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise TtsError(f"edge-tts 合成超时（{self.timeout_s:g} 秒）") from exc
        except FileNotFoundError as exc:
            raise TtsError(f"找不到 Python 解释器：{interpreter}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1] if detail else "未知错误"
            raise TtsError(
                f"edge-tts 合成失败：{reason}"
                "（当前解释器里可能没装 edge-tts：pip install edge-tts，"
                "或用 --tts-python 指向已装好的环境）"
            )


@dataclass(frozen=True, slots=True)
class CommandTtsEngine:
    """外部命令模板引擎：``{text_file}`` 是文案文件，``{out}`` 是音频输出路径。

    ``{voice_id}`` / ``{voice}`` 在构造引擎时就被替换成当前音色（``dub`` 按说话人分别建
    引擎），所以命令里可以直接接云端复刻音色，例如：

        python tools/tts_dashscope.py --text-file {text_file} --out {out} --voice {voice_id}

    命令经 shell 执行（Windows 下是 cmd），适合接各家云端 TTS/克隆 CLI。
    """

    command: str
    timeout_s: float = 120.0

    def synthesize(self, text: str, out_path: Path) -> None:
        text_file = out_path.with_suffix(".txt")
        text_file.write_text(text, encoding="utf-8", newline="\n")
        rendered = (
            self.command.replace("{text_file}", str(text_file))
            .replace("{out}", str(out_path))
            .replace("{text}", text)
        )
        try:
            completed = subprocess.run(
                rendered, capture_output=True, timeout=self.timeout_s, shell=True
            )
        except subprocess.TimeoutExpired as exc:
            raise TtsError(f"外部 TTS 命令超时（{self.timeout_s:g} 秒）") from exc

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1] if detail else "未知错误"
            raise TtsError(f"外部 TTS 命令失败（退出码 {completed.returncode}）：{reason}")
