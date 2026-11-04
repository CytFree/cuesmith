"""一次 matrix 运行的编排：加载 → 校验 → 导出 → 落报告。

产物全部落在 ``--out`` 目录，任一步失败不必从头重跑。条数不一致时**只写报告、
不写字幕文件**——错位的双语字幕比没有更危险。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .export import dump_ass, dump_bilingual_srt, dump_txt, dump_vtt
from .layout import LayoutOptions, layout_pair
from .report import OutputFile, build_report, describe_input
from .srt import dump_srt, load_srt
from .validate import Limits, validate

__all__ = ["MatrixOptions", "MatrixResult", "run_matrix"]

EXIT_OK = 0
EXIT_INVALID = 2


@dataclass(frozen=True, slots=True)
class MatrixOptions:
    src: Path
    tgt: Path
    out: Path
    src_lang: str = "zh"
    tgt_lang: str = "en"
    limits: Limits | None = None
    layout: bool = False
    generated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatrixResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    outputs: tuple[Path, ...]


def _count_codes(result) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in result.findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return dict(sorted(counts.items()))


def run_matrix(options: MatrixOptions) -> MatrixResult:
    src_sub, src_encoding = load_srt(options.src)
    tgt_sub, tgt_encoding = load_srt(options.tgt)

    limits = options.limits or Limits.from_languages(options.src_lang, options.tgt_lang)
    result = validate(
        src_sub,
        tgt_sub,
        limits=limits,
        src_lang=options.src_lang,
        tgt_lang=options.tgt_lang,
    )

    out_dir = Path(options.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    outputs: list[OutputFile] = []
    notes: list[str] = []
    layout_info: dict[str, object] | None = None
    export_src, export_tgt = src_sub, tgt_sub
    result_for_report = result

    if result.paired and options.layout:
        laid_src, laid_tgt, stats = layout_pair(
            src_sub,
            tgt_sub,
            options=LayoutOptions(
                src_width=limits.width_src,
                tgt_width=limits.width_tgt,
                max_lines=limits.max_lines,
                src_cps=limits.cps_src,
                tgt_cps=limits.cps_tgt,
            ),
        )
        after = validate(laid_src, laid_tgt, limits=limits, src_lang=options.src_lang, tgt_lang=options.tgt_lang)
        layout_info = {
            "enabled": True,
            "cues_before": stats.cues_before,
            "cues_after": stats.cues_after,
            "split_cues": stats.split_cues,
            "input_validation": {
                "errors": result.errors,
                "warnings": result.warnings,
                "by_code": _count_codes(result),
            },
            "violations_after": {
                "errors": after.errors,
                "warnings": after.warnings,
                "by_code": _count_codes(after),
            },
        }
        export_src, export_tgt = laid_src, laid_tgt
        # 报告描述"交付的文件"，而不是排版前的中间结果
        result_for_report = after

    if not result.paired:
        notes.append("译文条数与原文不一致，未导出任何字幕文件")
    else:
        exports = (
            ("mono_srt", out_dir / f"{options.tgt_lang}.srt", dump_srt(export_tgt)),
            ("bilingual_srt", out_dir / "bilingual.srt", dump_bilingual_srt(export_src, export_tgt)),
            ("ass", out_dir / "bilingual.ass", dump_ass(export_src, export_tgt)),
            ("vtt", out_dir / "bilingual.vtt", dump_vtt(export_src, export_tgt)),
            ("txt", out_dir / f"{options.tgt_lang}.txt", dump_txt(export_tgt)),
        )
        for kind, path, text in exports:
            # newline="\n"：产物一律 LF，避免跨平台 diff 噪声
            path.write_text(text, encoding="utf-8", newline="\n")
            outputs.append(
                OutputFile(
                    kind=kind,
                    path=path.resolve(),
                    cues=len(export_tgt),
                    size=path.stat().st_size,
                )
            )

    generated_at = options.generated_at or datetime.now().astimezone()
    report = build_report(
        src_meta=describe_input(
            options.src, src_sub, lang=options.src_lang, encoding=src_encoding
        ),
        tgt_meta=describe_input(
            options.tgt, tgt_sub, lang=options.tgt_lang, encoding=tgt_encoding
        ),
        limits=limits,
        result=result_for_report,
        outputs=tuple(outputs),
        generated_at=generated_at,
        notes=tuple(notes),
        layout=options.layout,
    )
    if layout_info is not None:
        report["layout"] = layout_info
    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    return MatrixResult(
        status=result.status,
        exit_code=EXIT_OK if result.status in {"ok", "warn"} else EXIT_INVALID,
        report=report,
        report_path=report_path.resolve(),
        outputs=tuple(output.path for output in outputs),
    )
