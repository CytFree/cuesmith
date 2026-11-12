"""report.json 的构建：纯函数，便于测试与复跑比对。

报告是这一层的**唯一交付凭证**——它同时承担：
- 可复跑：输入文件 sha256 + 阈值配置全部落盘，换台机器也能复现同一结论
- 可追责：每条违规都有 cue 号、指标、实测值、阈值
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .srt import Subtitle
from .validate import Limits, ValidationResult

__all__ = [
    "SCHEMA_VERSION",
    "InputMeta",
    "OutputFile",
    "build_report",
    "describe_input",
    "file_digest",
    "section",
]

SCHEMA_VERSION = 1


def section(report: Mapping[str, object], key: str) -> dict[str, object]:
    """取报告里的一段（不是 dict 就给空 dict）。

    报告是 JSON，边界处的类型只有 ``dict[str, object]``；于是每个读点要么写
    ``(report.get("x") or {}).get("y")`` 再按行挂一条抑制，要么散着写
    ``x.get(k) if isinstance(x, dict) else None``。取段收成一处，两种写法都不再需要。

    对损坏的报告（该是 dict 却是别的）：原先是"假值 → 空 dict、真值 → AttributeError"，
    现在统一返回空 dict —— 与那些 ``isinstance`` 守卫本来的意图一致，读出来就是 None。
    参数用 ``Mapping`` 是为了让调用方不必把自己手里的 dict 复制一份。
    """

    value = report.get(key)
    return value if isinstance(value, dict) else {}



@dataclass(frozen=True, slots=True)
class InputMeta:
    path: Path
    encoding: str
    sha256: str
    size: int
    cues: int
    lang: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "lang": self.lang,
            "encoding": self.encoding,
            "cues": self.cues,
            "bytes": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class OutputFile:
    kind: str
    path: Path
    cues: int
    size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "cues": self.cues,
            "bytes": self.size,
        }


def file_digest(path: Path | str) -> tuple[int, str]:
    """返回 ``(字节数, sha256)``；两个报告（矩阵/清单）共用同一口径。"""

    data = Path(path).read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def describe_input(path: Path | str, subtitle: Subtitle, *, lang: str, encoding: str) -> InputMeta:
    resolved = Path(path).resolve()
    size, digest = file_digest(resolved)
    return InputMeta(
        path=resolved,
        encoding=encoding,
        sha256=digest,
        size=size,
        cues=len(subtitle),
        lang=lang,
    )


def build_report(
    *,
    src_meta: InputMeta,
    tgt_meta: InputMeta,
    limits: Limits,
    result: ValidationResult,
    outputs: tuple[OutputFile, ...],
    generated_at: datetime,
    notes: tuple[str, ...] = (),
    layout: bool = False,
) -> dict[str, object]:
    by_code: dict[str, int] = {}
    for finding in result.findings:
        by_code[finding.code] = by_code.get(finding.code, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "cuesmith",
        "version": __version__,
        "command": "build",
        "generated_at": generated_at.isoformat(),
        "status": result.status,
        "inputs": {
            "source": src_meta.to_dict(),
            "target": tgt_meta.to_dict(),
        },
        "config": {
            "src_lang": src_meta.lang,
            "tgt_lang": tgt_meta.lang,
            "cps_src": limits.cps_src,
            "cps_tgt": limits.cps_tgt,
            "width_src": limits.width_src,
            "width_tgt": limits.width_tgt,
            "max_lines": limits.max_lines,
            # 排版开关会改变产物（折行/切条），必须进报告才能当缓存判据
            "layout": bool(layout),        },
        "summary": {
            "cues": src_meta.cues,
            "errors": result.errors,
            "warnings": result.warnings,
            "outputs": len(outputs),
            "by_code": dict(sorted(by_code.items())),
        },
        "outputs": [output.to_dict() for output in outputs],
        "findings": [finding.to_dict() for finding in result.findings],
        "notes": list(notes),
    }
