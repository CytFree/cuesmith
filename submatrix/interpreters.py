"""解释器探测：找"装了某个重依赖的 Python"的唯一策略。

背景
----
主包只依赖标准库，ASR / 说话人分离 / 本地 TTS / demucs 各自要一个装了重依赖的解释器。
以前每个模块各写一套探测循环，于是同一个问题有 5 种答案：

==========  ==============================  ==================================
组件        找不到时                        显式值/环境变量
==========  ==============================  ==================================
ASR         抛 AsrError（带已尝试列表）      探测
说话人分离   抛 DiarizeError（带已尝试列表）  探测
本地 TTS     抛 LocalTtsError（多行清单）     不探测（尊重用户）
Edge-TTS    **静默退回当前解释器**           不探测（尊重用户）
demucs      **返回 None**                    探测
==========  ==============================  ==================================

"静默退回 sys.executable"最坑：下游直到合成时才报 `ModuleNotFoundError`，用户看到的是
"配音没声音"，而不是"你没装 edge-tts"。这里把策略显式化：为什么退、退到哪、怎么报，
都由调用方声明，实现只此一份。

分工边界
--------
- **本模块**：候选去重与顺序、是否信任显式值/环境变量、探测、探测失败时怎么报、
  找不到时是抛错/返回 None/退回当前解释器。
- **各组件**：候选路径（哪些 venv 值得看）、探针（`import xxx`）、给用户的安装提示。
  这些是真正的领域数据，不该被抽象掉。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

__all__ = [
    "MISSING_CURRENT",
    "MISSING_NONE",
    "MISSING_RAISE",
    "Probe",
    "Requirement",
    "bool_probe",
    "missing_message",
    "ordered_candidates",
    "python_in_venv",
    "resolve",
    "runner_probe",
]

MISSING_RAISE = "raise"  # 抛 error_cls（组件缺了就跑不了）
MISSING_NONE = "none"  # 返回 None（组件可选，比如 demucs 只在 --keep-bgm 时用）
MISSING_CURRENT = "current"  # 退回 sys.executable（尽力而为，真跑不通让 worker 报错）

MissingPolicy = Literal["raise", "none", "current"]

# 探针结果：(是否可用, 失败细节)。细节只用于"已尝试：..."，失败时别丢，成功时为空。
Probe = Callable[[str], "tuple[bool, str]"]


@dataclass(frozen=True, slots=True)
class Requirement:
    """一个"需要特定依赖的解释器"的声明。"""

    label: str  # 人类可读："faster-whisper"
    module: str  # import 探针用："faster_whisper"
    parameter: str  # CLI 参数："--asr-python"
    env_var: str = ""  # 环境变量："SUBMATRIX_ASR_PYTHON"
    hint: str = ""  # 额外怎么装/建 venv 的提示


def python_in_venv(venv: Path, *, windows_only: bool = False) -> list[str]:
    """venv 目录 → 里头的解释器路径（Windows 优先，Linux/mac 也要能找）。"""

    layouts = (Path("Scripts") / "python.exe",)
    if not windows_only:
        layouts = (*layouts, Path("bin") / "python")
    return [str(venv / relative) for relative in layouts]


def ordered_candidates(*groups: Sequence[str | None]) -> list[str]:
    """按给定顺序拼接、去掉空值与重复，保留优先级。"""

    seen: dict[str, None] = {}
    for group in groups:
        for item in group:
            if item:
                seen.setdefault(str(item), None)
    return list(seen)


def bool_probe(check: Callable[[str], bool]) -> Probe:
    """把现有的 ``check(python) -> bool`` 探针适配成 :data:`Probe`。"""

    def probe(python: str) -> tuple[bool, str]:
        return (True, "") if check(python) else (False, "import 失败")

    return probe


def runner_probe(
    runner: Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"], module: str
) -> Probe:
    """用可注入的 runner 做 ``import <module>`` 探针（asr / diarize 的注入缝）。

    先跳过不存在的路径，避免为拼错的路径起子进程；失败时带上 stderr 最后一行，
    否则用户只看到"已尝试：a；b"却不知道为什么。
    """

    def probe(python: str) -> tuple[bool, str]:
        if not Path(python).is_file():
            return False, "路径不存在"
        try:
            completed = runner([python, "-c", f"import {module}"])
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)[:160]
        if completed.returncode == 0:
            return True, ""
        lines = (getattr(completed, "stderr", "") or "").strip().splitlines()
        return False, (lines[-1] if lines else "import 失败")[:160]

    return probe


def missing_message(requirement: Requirement, attempts: Sequence[tuple[str, str]]) -> str:
    """探测全失败时的报错文案：干什么、怎么指定、已尝试过什么。"""

    tried = "；".join(
        f"{python}: {detail}" if detail else python for python, detail in attempts
    )
    how = f"用 {requirement.parameter} 指定解释器"
    if requirement.env_var:
        how += f"，或设置环境变量 {requirement.env_var}"
    parts = [
        f"找不到装有 {requirement.label} 的 Python"
        f"（import {requirement.module} 失败）。{how}。",
    ]
    if requirement.hint:
        parts.append(requirement.hint)
    parts.append(f"（已尝试：{tried or '没有可用的候选解释器'}）")
    return "".join(parts)


def resolve(
    requirement: Requirement,
    explicit: str | None,
    *,
    candidates: Sequence[str | None],
    probe: Probe,
    on_missing: MissingPolicy = MISSING_RAISE,
    trust_explicit: bool = False,
    trust_env: bool = False,
    error_cls: type[Exception] = RuntimeError,
) -> str | None:
    """按策略找一个能 ``import <module>`` 的解释器。

    顺序固定为「显式参数 → 环境变量 → 各组件给的候选」，去重后逐个探测；
    ``trust_explicit`` / ``trust_env`` 为真时直接采信、不探测（尊重用户配置，
    真跑不通时由 worker 启动错误给出 ``ModuleNotFoundError``）。

    ``on_missing`` 决定一个都找不到时怎么办——这是以前各组件自行其是的地方：
    ``raise`` 抛 ``error_cls``、``none`` 返回 None、``current`` 退回当前解释器。
    """

    if explicit and trust_explicit:
        return explicit
    env_value = os.environ.get(requirement.env_var) if requirement.env_var else None
    if env_value and trust_env:
        return env_value

    ordered = ordered_candidates((explicit,), (env_value,), candidates)
    attempts: list[tuple[str, str]] = []
    for python in ordered:
        ok, detail = probe(python)
        if ok:
            return python
        attempts.append((python, detail))

    if on_missing == MISSING_NONE:
        return None
    if on_missing == MISSING_CURRENT:
        return sys.executable
    raise error_cls(missing_message(requirement, attempts))
