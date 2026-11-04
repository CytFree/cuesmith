"""FR-1：探测与内封字幕直出。

三件事，按顺序：
1. **探测**：ffprobe 拿流清单（视频/音轨/字幕，含语言与 forced 标记）
2. **判定**：文本轨 → 抽 SRT；图形轨 → 明确报错；无字幕 → 路由到 ASR
3. **直出**：`ffmpeg -map 0:s:N -c:s srt` 出 `source.srt`，并把全过程写进 `manifest.json`

设计原则：
- **不静默降级**（AC-3）。图形字幕轨只能 OCR，一期不做，就明确失败，绝不悄悄转 ASR。
- **forced 轨一律跳过并告警**：forced 只覆盖对白之外的"招牌/字幕卡"，拿它当正片字幕会漏掉 90% 内容。
- **所有结局都落 manifest**：路由结论本身就是交付物，脚本要能据此决定下一步。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .report import OutputFile, file_digest
from .srt import load_srt

__all__ = [
    "GRAPHIC_SUBTITLE_CODECS",
    "TEXT_SUBTITLE_CODECS",
    "ExtractionError",
    "MediaProbe",
    "ProbeError",
    "ProbeOptions",
    "ProbeResult",
    "RouteDecision",
    "StreamInfo",
    "build_manifest",
    "choose_subtitle",
    "classify_subtitle",
    "extract_to_srt",
    "ffprobe_json",
    "parse_probe",
    "probe_media",
    "run_probe",
]

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# 能被 ffmpeg 直接转成 SRT 的文本型字幕
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "ass", "ssa", "mov_text", "webvtt", "text"})
# 位图字幕：要 OCR 才能变文本，一期明确不做
GRAPHIC_SUBTITLE_CODECS = frozenset(
    {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvdsub"}
)

# ffprobe 的 language 标签混用 ISO 639-1/2T/2B 与 BC P 47，统一收进来比对
LANGUAGE_ALIASES: dict[str, frozenset[str]] = {
    "zh": frozenset(
        {"zh", "zho", "chi", "cmn", "chs", "cht", "zh-cn", "zh-hans", "zh-hant", "chi-simplified"}
    ),
    "en": frozenset({"en", "eng"}),
}

# 等于"没标语言"
UNDETERMINED_LANGUAGES = frozenset({"", "und", "unknown", "none", "mul"})


class ProbeError(RuntimeError):
    """ffprobe / 文件层面失败（不是内容判定问题）。"""


class ExtractionError(ProbeError):
    """ffmpeg 抽出字幕失败。"""


@dataclass(frozen=True, slots=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    language: str | None = None
    title: str | None = None
    forced: bool = False
    default: bool = False
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    attached_pic: bool = False

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "index": self.index,
            "codec": self.codec_name,
            "language": self.language,
            "title": self.title,
            "forced": self.forced,
            "default": self.default,
        }
        if self.codec_type == "video":
            payload["width"] = self.width
            payload["height"] = self.height
        elif self.codec_type == "audio":
            payload["channels"] = self.channels
            payload["sample_rate"] = self.sample_rate
        return payload


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: Path
    format_name: str
    duration_s: float | None
    size_bytes: int | None
    video: tuple[StreamInfo, ...] = ()
    audio: tuple[StreamInfo, ...] = ()
    subtitle: tuple[StreamInfo, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    kind: str  # "extract" | "route_to_asr" | "unsupported"
    reason: str
    stream_index: int | None = None
    subtitle_ordinal: int | None = None
    codec: str | None = None
    language: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stream_index": self.stream_index,
            "subtitle_ordinal": self.subtitle_ordinal,
            "codec": self.codec,
            "language": self.language,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProbeOptions:
    src: Path
    out: Path
    src_lang: str = "zh"
    extract: bool = True
    generated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: str
    exit_code: int
    manifest: dict[str, object]
    manifest_path: Path
    subtitle_path: Path | None
    cues: int


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    try:
        return round(float(str(value)), 3)
    except (TypeError, ValueError):
        return None


def classify_subtitle(codec: str) -> str:
    key = (codec or "").strip().lower()
    if key in TEXT_SUBTITLE_CODECS:
        return "text"
    if key in GRAPHIC_SUBTITLE_CODECS:
        return "graphic"
    return "unknown"


def _normalize_language(tag: str | None) -> str:
    return (tag or "").strip().lower().replace("_", "-")


def _language_matches(tag: str | None, lang: str) -> bool:
    normalized = _normalize_language(tag)
    if normalized in UNDETERMINED_LANGUAGES:
        return False
    aliases = LANGUAGE_ALIASES.get(lang, frozenset({lang}))
    return normalized in aliases or normalized.split("-")[0] in aliases


def _is_unmarked(tag: str | None) -> bool:
    return _normalize_language(tag) in UNDETERMINED_LANGUAGES


def _stream_from_raw(raw: dict) -> StreamInfo:
    disposition = raw.get("disposition") or {}
    tags = raw.get("tags") or {}
    return StreamInfo(
        index=_to_int(raw.get("index")) or 0,
        codec_type=str(raw.get("codec_type") or ""),
        codec_name=_clean(raw.get("codec_name")) or "unknown",
        language=_clean(tags.get("language")),
        title=_clean(tags.get("title")),
        forced=bool(disposition.get("forced")),
        default=bool(disposition.get("default")),
        attached_pic=bool(disposition.get("attached_pic")),
        width=_to_int(raw.get("width")),
        height=_to_int(raw.get("height")),
        channels=_to_int(raw.get("channels")),
        sample_rate=_to_int(raw.get("sample_rate")),
    )


def parse_probe(path: Path | str, payload: dict) -> MediaProbe:
    """把 ``ffprobe -show_format -show_streams -of json`` 的输出转成 :class:`MediaProbe`。"""

    fmt = payload.get("format") or {}
    groups: dict[str, list[StreamInfo]] = {"video": [], "audio": [], "subtitle": []}
    warnings: list[str] = []

    for raw in payload.get("streams") or ():
        info = _stream_from_raw(raw)
        if info.attached_pic:
            warnings.append(f"已忽略封面图（流 #{info.index}，attached_pic）")
            continue
        if info.codec_type in groups:
            groups[info.codec_type].append(info)

    return MediaProbe(
        path=Path(path),
        format_name=_clean(fmt.get("format_name")) or "unknown",
        duration_s=_to_float(fmt.get("duration")),
        size_bytes=_to_int(fmt.get("size")),
        video=tuple(groups["video"]),
        audio=tuple(groups["audio"]),
        subtitle=tuple(groups["subtitle"]),
        warnings=tuple(warnings),
    )


def ffprobe_json(path: Path) -> dict:
    try:
        completed = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError("找不到 ffprobe，请确认已装 ffmpeg 并加入 PATH") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ProbeError(f"ffprobe 读不了 {path}：{detail or '未知错误'}")

    try:
        return json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON：{exc}") from exc


def probe_media(path: Path) -> MediaProbe:
    return parse_probe(path, ffprobe_json(path))


def _ordinal_of(subtitles: tuple[StreamInfo, ...], stream_index: int) -> int:
    for ordinal, info in enumerate(subtitles):
        if info.index == stream_index:
            return ordinal
    raise ProbeError(f"字幕流 #{stream_index} 不在探测结果里")


def _extract_decision(
    chosen: StreamInfo, subtitles: tuple[StreamInfo, ...], reason: str, warnings: list[str]
) -> RouteDecision:
    return RouteDecision(
        kind="extract",
        reason=reason,
        stream_index=chosen.index,
        subtitle_ordinal=_ordinal_of(subtitles, chosen.index),
        codec=chosen.codec_name,
        language=chosen.language,
        warnings=tuple(warnings),
    )


def choose_subtitle(probe: MediaProbe, src_lang: str) -> RouteDecision:
    """路由判定（AC-2 的全部逻辑都在这里，纯函数）。"""

    subtitles = probe.subtitle
    warnings: list[str] = []

    if not subtitles:
        return RouteDecision(
            kind="route_to_asr", reason="没有字幕流，需要 ASR（交底座 /recogn）"
        )

    for info in subtitles:
        if info.forced:
            warnings.append(f"字幕流 #{info.index}（{info.codec_name}）标记为 forced，按规则跳过")

    text_tracks = [info for info in subtitles if classify_subtitle(info.codec_name) == "text"]
    graphic_tracks = [
        info for info in subtitles if classify_subtitle(info.codec_name) == "graphic"
    ]
    unknown_tracks = [
        info for info in subtitles if classify_subtitle(info.codec_name) == "unknown"
    ]
    for info in unknown_tracks:
        warnings.append(f"字幕流 #{info.index} 编码 {info.codec_name} 无法识别，已忽略")

    candidates = [info for info in text_tracks if not info.forced]

    if not candidates:
        if graphic_tracks:
            detail = "、".join(f"#{info.index} {info.codec_name}" for info in graphic_tracks)
            return RouteDecision(
                kind="unsupported",
                reason=f"只有图形字幕轨（{detail}），需要 OCR 才能拿到文本，一期明确不做",
                codec=graphic_tracks[0].codec_name,
                warnings=tuple(warnings),
            )
        if unknown_tracks:
            detail = "、".join(f"#{info.index} {info.codec_name}" for info in unknown_tracks)
            return RouteDecision(
                kind="unsupported",
                reason=f"字幕轨编码无法识别（{detail}），拒绝静默降级",
                codec=unknown_tracks[0].codec_name,
                warnings=tuple(warnings),
            )
        return RouteDecision(
            kind="route_to_asr",
            reason="唯一的文本字幕轨标记为 forced，已跳过，改走 ASR（交底座 /recogn）",
            warnings=tuple(warnings),
        )

    if graphic_tracks:
        detail = "、".join(f"#{info.index} {info.codec_name}" for info in graphic_tracks)
        warnings.append(f"存在图形字幕轨（{detail}），不支持抽取，已跳过")

    matched = [info for info in candidates if _language_matches(info.language, src_lang)]
    if matched:
        chosen = matched[0]
        return _extract_decision(
            chosen,
            subtitles,
            f"字幕流 #{chosen.index} 语言标签 {chosen.language} 匹配源语言 {src_lang}",
            warnings,
        )

    unmarked = [info for info in candidates if _is_unmarked(info.language)]
    if unmarked:
        chosen = unmarked[0]
        return _extract_decision(
            chosen,
            subtitles,
            f"没有匹配源语言 {src_lang} 的语言标签，选取第一条未标记语言的文本轨（流 #{chosen.index}）",
            warnings,
        )

    chosen = candidates[0]
    tags = "、".join(f"#{info.index}={info.language}" for info in candidates)
    warnings.append(f"所有文本轨语言标签都不匹配源语言 {src_lang}（{tags}），已退化为第一条")
    return _extract_decision(
        chosen,
        subtitles,
        f"语言标签均不匹配源语言 {src_lang}，退化为第一条文本轨（流 #{chosen.index}）",
        warnings,
    )


def extract_to_srt(probe: MediaProbe, decision: RouteDecision, out_path: Path) -> None:
    if decision.subtitle_ordinal is None:
        raise ExtractionError("路由判定没有给出字幕流序号，无法抽取")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                FFMPEG,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(probe.path),
                "-map",
                f"0:s:{decision.subtitle_ordinal}",
                "-c:s",
                "srt",
                "-f",
                "srt",
                str(out_path),
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ExtractionError("找不到 ffmpeg，请确认已装 ffmpeg 并加入 PATH") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ExtractionError(f"ffmpeg 抽取字幕失败：{detail or '未知错误'}")


def build_manifest(
    *,
    probe: MediaProbe,
    decision: RouteDecision,
    src_lang: str,
    status: str,
    outputs: tuple[OutputFile, ...],
    warnings: tuple[str, ...],
    generated_at: datetime,
) -> dict[str, object]:
    try:
        size, digest = file_digest(probe.path)
    except OSError:
        size, digest = probe.size_bytes, None

    return {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "probe",
        "generated_at": generated_at.isoformat(),
        "status": status,
        "input": {
            "path": str(Path(probe.path).resolve()),
            "bytes": size,
            "sha256": digest,
            "format_name": probe.format_name,
            "duration_s": probe.duration_s,
        },
        "src_lang": src_lang,
        "streams": {
            "video": [info.to_dict() for info in probe.video],
            "audio": [info.to_dict() for info in probe.audio],
            "subtitle": [info.to_dict() for info in probe.subtitle],
        },
        "decision": decision.to_dict(),
        "warnings": list(warnings),
        "outputs": [output.to_dict() for output in outputs],
    }


def _exit_code(status: str) -> int:
    if status in {"extracted", "extract", "route_to_asr"}:
        return 0
    if status == "unsupported":
        return 2
    return 1


def run_probe(
    options: ProbeOptions,
    *,
    prober: Callable[[Path], MediaProbe] = probe_media,
    extractor: Callable[[MediaProbe, RouteDecision, Path], None] = extract_to_srt,
) -> ProbeResult:
    probe = prober(Path(options.src))
    decision = choose_subtitle(probe, options.src_lang)

    out_dir = Path(options.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    warnings = list(probe.warnings) + list(decision.warnings)
    generated_at = options.generated_at or datetime.now().astimezone()

    def write_manifest(status: str, outputs: tuple[OutputFile, ...]) -> dict[str, object]:
        manifest = build_manifest(
            probe=probe,
            decision=decision,
            src_lang=options.src_lang,
            status=status,
            outputs=outputs,
            warnings=tuple(warnings),
            generated_at=generated_at,
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return manifest

    status = decision.kind
    subtitle_path: Path | None = None
    cues = 0
    outputs: tuple[OutputFile, ...] = ()

    if decision.kind == "extract" and options.extract:
        target = out_dir / "source.srt"
        try:
            extractor(probe, decision, target)
        except ExtractionError as exc:
            warnings.append(f"抽取失败：{exc}")
            write_manifest("extract_failed", ())
            raise

        extracted, _ = load_srt(target)
        if len(extracted) == 0:
            message = "抽取结果为空（0 条 cue），按失败处理，不静默成功"
            warnings.append(message)
            write_manifest("extract_failed", ())
            raise ExtractionError(message)

        cues = len(extracted)
        subtitle_path = target.resolve()
        status = "extracted"
        outputs = (
            OutputFile(
                kind="source_srt",
                path=subtitle_path,
                cues=cues,
                size=target.stat().st_size,
            ),
        )

    manifest = write_manifest(status, outputs)
    return ProbeResult(
        status=status,
        exit_code=_exit_code(status),
        manifest=manifest,
        manifest_path=manifest_path.resolve(),
        subtitle_path=subtitle_path,
        cues=cues,
    )
