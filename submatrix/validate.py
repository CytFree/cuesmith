"""字幕对校验：结构硬约束 + 阅读性软指标（FR-2 行为 2、3）。

设计原则（对应 PRD 的"报出不改"）：
- 只读不写。这里没有任何改时间轴、删条、改文案的路径。
- error 与 warning 分开：error 意味着这批字幕不可交付（条数/时间轴/空译/漏译），
  warning 意味着"人眼需要看一眼"（CPS / 行宽 / 行数）。
- 条数不一致时**不做逐条比对**：索引已经错位，逐条比只会刷出上百条噪声。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .langs import LANG_PROFILES, LangProfile
from .langs import profile as _profile
from .srt import Cue, Subtitle, format_srt_time
from .textutil import char_count, split_text_lines

__all__ = [
    "LANG_PROFILES",
    "Finding",
    "LangProfile",
    "Limits",
    "ValidationResult",
    "validate",
]

# Limits 空构造的默认方向（zh→en）：与 pipeline 的产品默认一致（源中文、译英文）。
# 真正决定阈值的还是 LANG_PROFILES；这里只是选哪两个语言码当默认。
DEFAULT_SRC_LANG = "zh"
DEFAULT_TGT_LANG = "en"


@dataclass(frozen=True, slots=True)
class Limits:
    """可配置阈值。源/目标各一套，因为两种语言的计量口径不同。

    默认值从 :mod:`submatrix.langs` 的 ``LANG_PROFILES`` 推导，不再各存一份字面量——
    改阈值只需要改那一张表。
    """

    cps_src: float = field(default_factory=lambda: LANG_PROFILES[DEFAULT_SRC_LANG].cps)
    cps_tgt: float = field(default_factory=lambda: LANG_PROFILES[DEFAULT_TGT_LANG].cps)
    width_src: int = field(default_factory=lambda: LANG_PROFILES[DEFAULT_SRC_LANG].width)
    width_tgt: int = field(default_factory=lambda: LANG_PROFILES[DEFAULT_TGT_LANG].width)
    max_lines: int = 2

    @classmethod
    def from_languages(
        cls,
        src_lang: str,
        tgt_lang: str,
        *,
        cps_src: float | None = None,
        cps_tgt: float | None = None,
        width_src: int | None = None,
        width_tgt: int | None = None,
        max_lines: int = 2,
    ) -> Limits:
        src_profile = _profile(src_lang)
        tgt_profile = _profile(tgt_lang)
        return cls(
            cps_src=src_profile.cps if cps_src is None else cps_src,
            cps_tgt=tgt_profile.cps if cps_tgt is None else cps_tgt,
            width_src=src_profile.width if width_src is None else width_src,
            width_tgt=tgt_profile.width if width_tgt is None else width_tgt,
            max_lines=max_lines,
        )


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str
    cue: int | None = None
    lang: str | None = None
    metric: str | None = None
    value: float | int | str | None = None
    threshold: float | int | str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "cue": self.cue,
            "lang": self.lang,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    findings: tuple[Finding, ...] = ()
    paired: bool = True

    @property
    def errors(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "warning")

    @property
    def status(self) -> str:
        if self.errors:
            return "fail"
        return "warn" if self.warnings else "ok"

    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _has_latin(text: str) -> bool:
    return bool(_LATIN_RE.search(text))


def _flatten(text: str) -> str:
    return " ".join(split_text_lines(text))


def _looks_untranslated(src_text: str, tgt_text: str) -> bool:
    """把"漏译"做得保守一点，避免把 "OK"→"OK" 这类误报成漏译。"""

    src_flat = _flatten(src_text)
    tgt_flat = _flatten(tgt_text)
    if not src_flat or not tgt_flat:
        return False
    if not _has_cjk(src_flat):
        return False
    if src_flat.casefold() == tgt_flat.casefold():
        return True
    return not _has_latin(tgt_flat)


def _readability_findings(
    cue_no: int,
    cue: Cue,
    *,
    lang: str,
    profile: LangProfile,
    cps_limit: float,
    width_limit: int,
    max_lines: int,
) -> list[Finding]:
    findings: list[Finding] = []

    lines = split_text_lines(cue.text)
    if not lines:
        return findings

    longest = max(range(len(lines)), key=lambda index: len(lines[index]))
    longest_length = len(lines[longest])
    if longest_length > width_limit:
        findings.append(
            Finding(
                severity="warning",
                code="line_width",
                cue=cue_no,
                lang=lang,
                metric="line_width",
                value=longest_length,
                threshold=width_limit,
                message=(
                    f"{profile.label}第 {longest + 1} 行 {longest_length} 字符，"
                    f"超过上限 {width_limit}"
                ),
            )
        )

    if len(lines) > max_lines:
        findings.append(
            Finding(
                severity="warning",
                code="line_count",
                cue=cue_no,
                lang=lang,
                metric="line_count",
                value=len(lines),
                threshold=max_lines,
                message=f"{profile.label}占 {len(lines)} 行，超过上限 {max_lines} 行",
            )
        )

    duration_s = cue.timing.duration_s
    if duration_s <= 0:
        return findings

    chars = char_count(cue.text, cjk=profile.cjk)
    cps = chars / duration_s
    if cps > cps_limit:
        findings.append(
            Finding(
                severity="warning",
                code="cps",
                cue=cue_no,
                lang=lang,
                metric="cps",
                value=round(cps, 2),
                threshold=cps_limit,
                message=(
                    f"{profile.label} {chars} 字符 / {duration_s:.2f}s = "
                    f"{cps:.2f} CPS，超过上限 {cps_limit}"
                ),
            )
        )

    return findings


def validate(
    src: Subtitle,
    tgt: Subtitle,
    *,
    limits: Limits,
    src_lang: str = "zh",
    tgt_lang: str = "en",
) -> ValidationResult:
    """校验原文/译文是否可逐条配对、是否可读。**不改任何东西。**"""

    src_profile = _profile(src_lang)
    tgt_profile = _profile(tgt_lang)

    if len(src) != len(tgt):
        return ValidationResult(
            findings=(
                Finding(
                    severity="error",
                    code="cue_count_mismatch",
                    cue=None,
                    metric="cue_count",
                    value=len(tgt),
                    threshold=len(src),
                    message=(
                        f"原文 {len(src)} 条、译文 {len(tgt)} 条，条数不一致，"
                        "已跳过逐条比对（索引已错位）"
                    ),
                ),
            ),
            paired=False,
        )

    findings: list[Finding] = []
    for cue_no, (src_cue, tgt_cue) in enumerate(zip(src.cues, tgt.cues), start=1):
        if src_cue.timing != tgt_cue.timing:
            findings.append(
                Finding(
                    severity="error",
                    code="timeline_mismatch",
                    cue=cue_no,
                    metric="timeline",
                    value=(
                        f"{format_srt_time(tgt_cue.timing.start_ms)} --> "
                        f"{format_srt_time(tgt_cue.timing.end_ms)}"
                    ),
                    threshold=(
                        f"{format_srt_time(src_cue.timing.start_ms)} --> "
                        f"{format_srt_time(src_cue.timing.end_ms)}"
                    ),
                    message="译文时间轴与原文不一致（容差 0）",
                )
            )

        if src_cue.timing.duration_ms <= 0:
            findings.append(
                Finding(
                    severity="error",
                    code="nonpositive_duration",
                    cue=cue_no,
                    metric="duration_ms",
                    value=src_cue.timing.duration_ms,
                    threshold=1,
                    message="该条时长为 0（或为负），无法作为字幕槽位",
                )
            )

        if not split_text_lines(tgt_cue.text):
            findings.append(
                Finding(
                    severity="error",
                    code="empty_translation",
                    cue=cue_no,
                    metric="text_length",
                    value=0,
                    threshold=1,
                    message="译文为空",
                )
            )
        elif _looks_untranslated(src_cue.text, tgt_cue.text):
            findings.append(
                Finding(
                    severity="error",
                    code="untranslated",
                    cue=cue_no,
                    metric="text",
                    value=_flatten(tgt_cue.text),
                    threshold=_flatten(src_cue.text),
                    message="译文疑似未翻译（与原文相同，或完全不含拉丁字母）",
                )
            )

        findings.extend(
            _readability_findings(
                cue_no,
                src_cue,
                lang=src_lang,
                profile=src_profile,
                cps_limit=limits.cps_src,
                width_limit=limits.width_src,
                max_lines=limits.max_lines,
            )
        )
        findings.extend(
            _readability_findings(
                cue_no,
                tgt_cue,
                lang=tgt_lang,
                profile=tgt_profile,
                cps_limit=limits.cps_tgt,
                width_limit=limits.width_tgt,
                max_lines=limits.max_lines,
            )
        )

    return ValidationResult(findings=tuple(findings), paired=True)
