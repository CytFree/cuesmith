"""把字幕烧进视频（M10）：收费软件"一键成片"的最后一步。

导出的 SRT/ASS 是"外挂字幕"——播放器里能挂，但发到社交平台就是没字幕的裸视频。
收费软件默认会给你一条**带硬字幕的成片**，这里补上这一步：

1. 输入视频 + 字幕（``.ass`` 直接烧；``.srt`` 先转成带样式的 ASS 再烧）；
2. 只用 ffmpeg（libass + libx264），不自建渲染；
3. **自动核验**：在字幕时长内的某一帧上比对"烧录前/后"画面底部区域的像素差，
   差值过小说明字幕没烧上（字体缺失/滤镜路径错等），如实报错而不是假装成功。

报告 ``burn.json`` 记录输入 sha256、ASS 路径、CRF/preset、输出信息与核验结果；
"""  # noqa: E501

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from . import media
from .export import dump_ass_single
from .report import file_digest
from .srt import load_srt

__all__ = ["BurnError", "BurnOptions", "BurnResult", "run_burn"]

PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
DEFAULT_VERIFY_THRESHOLD = 0.05  # 底部区域平均像素差（0–255）
STRONG_PIXEL_DELTA = 24  # 单像素差超过它算"字幕笔画"（重编码噪声一般 < 20）
DEFAULT_STRONG_RATIO = 0.001  # 底部区域里"强变化像素"占比阈值（"Hello" 实测 0.0024）
POSITIONS = ("bottom", "top")
TOP_MARGIN_V = 72  # 顶部模式的有效边距（PlayRes 1080 下约 24px@360p）


class BurnError(RuntimeError):
    """烧录不可继续（输入缺失/ffmpeg 失败/字幕解析失败）。"""


@dataclass(frozen=True, slots=True)
class BurnOptions:
    video: Path
    subtitle: Path  # .ass（双语/单语）或 .srt（单语译文）
    out: Path
    crf: int = 18
    preset: str = "veryfast"
    verify: bool = True
    verify_threshold: float = DEFAULT_VERIFY_THRESHOLD
    strong_ratio_threshold: float = DEFAULT_STRONG_RATIO
    position: str = "bottom"  # bottom | top（源片自带硬字幕时用 top 避让）
    generated_at: datetime | None = None

    def validate(self) -> None:
        if not 0 <= self.crf <= 51:
            raise BurnError("CRF 必须在 0–51 之间（越大越糊/越小越清晰）")
        if self.preset not in PRESETS:
            raise BurnError(f"不认识的 preset：{self.preset!r}（{' / '.join(PRESETS)}）")
        if self.position not in POSITIONS:
            raise BurnError(f"不认识的字幕位置：{self.position!r}（{' / '.join(POSITIONS)}）")
        suffix = Path(self.subtitle).suffix.lower()
        if suffix not in {".ass", ".srt"}:
            raise BurnError("字幕只支持 .ass 或 .srt（.srt 会先转成带样式的 ASS）")


@dataclass(frozen=True, slots=True)
class BurnResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    video_path: Path
    ass_path: Path


# ---------------------------------------------------------------------- 工具


def _filter_path(path: Path) -> str:
    """ffmpeg filtergraph 里的路径：正斜杠 + 转义冒号，再整体用单引号包住。

    Windows 盘符里的 ``C:`` 不转义会被当成 filter 选项分隔符（经典坑）。
    """

    text = str(Path(path).resolve()).replace("\\", "/")
    text = text.replace("'", r"\'").replace(":", r"\:")
    return f"'{text}'"


def _probe(path: Path) -> dict[str, object]:
    return media.ffprobe_json(
        path,
        error_cls=BurnError,
        streams=True,
        what=f"读不出媒体信息（{path.name}）",
    )


def _duration_ms(info: dict[str, object]) -> int:
    fmt = info.get("format") or {}
    duration = fmt.get("duration") if isinstance(fmt, dict) else None
    if not duration:
        raise BurnError("读不出时长")
    return round(float(duration) * 1000)


def _best_ass_cue_ms(ass_text: str) -> int | None:
    """从 ASS 文本里取文字最多的那条 Dialogue 的中间时刻（取帧核验用，信号最强）。"""

    pattern = re.compile(
        r"^Dialogue:\s*\d+,(\d+):(\d{2}):(\d{2})\.(\d{2}),(\d+):(\d{2}):(\d{2})\.(\d{2}),(.*)$"
    )

    def to_ms(hours: int, minutes: int, seconds: int, centis: int) -> int:
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + centis * 10

    best: tuple[int, int, int] | None = None  # (text_len, start, end)
    for line in ass_text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        start = to_ms(*(int(part) for part in match.groups()[:4]))
        end = to_ms(*(int(part) for part in match.groups()[4:8]))
        text = match.group(9).strip()
        if not text:
            continue
        if best is None or len(text) > best[0]:
            best = (len(text), start, end)
    if best is None:
        return None
    return (best[1] + best[2]) // 2


_STYLE_LINE = re.compile(r"^Style:\s*Default,(.*)$", re.MULTILINE)


def _apply_position(ass_text: str, position: str) -> str:
    """把 Default 样式改成底部居中（2）或顶部居中（8）并重写 MarginV。

    源片本身带硬字幕时（本工具不做字幕擦除，PRD 明确排除 OCR/inpaint），
    把译文烧到顶部避让是成本最低、效果最稳的做法。
    """

    def replace(match: re.Match[str]) -> str:
        fields = match.group(1).split(",")
        if len(fields) < 22:
            return match.group(0)
        if position == "top":
            fields[17] = "8"  # Alignment
            fields[20] = str(TOP_MARGIN_V)  # MarginV
        else:
            fields[17] = "2"
            fields[20] = "48"
        return "Style: Default," + ",".join(fields)

    return _STYLE_LINE.sub(replace, ass_text, count=1)


def _region_grayscale(video: Path, at_ms: int, position: str = "bottom") -> bytes:
    """取一帧，裁出字幕所在区域（底部或顶部 40%）并缩小成灰度图。"""

    crop = "crop=iw:ih*2/5:0:ih*3/5" if position == "bottom" else "crop=iw:ih*2/5:0:0"
    completed = media.run_ffmpeg(
        [
            "-ss",
            f"{at_ms / 1000:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"{crop},scale=160:64,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        error_cls=BurnError,
        what="截取核验画面",
        # 像素级核验：抽帧失败时返回空帧由调用方判定，这里不因退出码中断
        check=False,
    )
    return completed.stdout


def _region_diff(
    before: Path, after: Path, at_ms: int, position: str = "bottom"
) -> tuple[float, float]:
    """返回（平均像素差, 强变化像素占比）。

    只看平均值会被"短字幕 + 重编码噪声"干扰；强变化像素占比用来抓字幕的白色笔画
    （单像素差 > 24），"Hello" 这种极短字幕也有 0.24% 的强变化像素。
    """

    first = _region_grayscale(before, at_ms, position)
    second = _region_grayscale(after, at_ms, position)
    if not first or len(first) != len(second):
        return 0.0, 0.0
    differences = [abs(left - right) for left, right in zip(first, second)]
    mean = sum(differences) / len(differences)
    strong = sum(1 for delta in differences if delta > STRONG_PIXEL_DELTA) / len(differences)
    return mean, strong


# ---------------------------------------------------------------------- 主流程


def run_burn(options: BurnOptions) -> BurnResult:
    """把外挂字幕烧进画面，并用像素差回读核验"字幕真的上屏了"。

    三步：准备（解析字幕、改写位置、探测源片）→ 烧录并探测产物 → 核验与报告。
    """

    session = _BurnSession.start(options)
    _burn_video(session)
    _verify_burn(session)
    return _write_outputs(session)


@dataclass(slots=True)
class _BurnSession:
    """一次烧录的现场：输入探测、产物探测、核验结论与告警。"""

    options: BurnOptions
    video: Path
    subtitle: Path
    out_dir: Path
    started: float
    ass_path: Path
    cue_count: int | None
    verify_at_ms: int | None
    encoding: str
    video_info: dict[str, object]
    input_duration: int
    out_video: Path
    output_info: dict[str, object] = field(default_factory=dict)
    output_duration: int = 0
    stream_kinds: dict[str, str] = field(default_factory=dict)
    verification: dict[str, object] | None = None
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"

    @classmethod
    def start(cls, options: BurnOptions) -> "_BurnSession":
        """校验输入、解析字幕并改写位置、探测源片时长——开工前的一次性准备。"""

        options.validate()
        video = Path(options.video)
        subtitle = Path(options.subtitle)
        if not video.is_file():
            raise BurnError(f"视频不存在：{video}")
        if not subtitle.is_file():
            raise BurnError(f"字幕不存在：{subtitle}")

        out_dir = Path(options.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        cue_count: int | None = None
        verify_at_ms: int | None = None
        encoding = ""
        if subtitle.suffix.lower() == ".srt":
            try:
                parsed, encoding = load_srt(subtitle)
            except (OSError, ValueError) as exc:
                raise BurnError(f"字幕读取失败（{subtitle.name}）：{exc}") from exc
            if len(parsed) == 0:
                raise BurnError("字幕里没有任何 cue，无法烧录")
            cue_count = len(parsed)
            ass_text = dump_ass_single(parsed)
            if options.verify:
                target = max(
                    parsed.cues, key=lambda cue: (len(cue.text), cue.timing.duration_ms)
                )
                verify_at_ms = (target.timing.start_ms + target.timing.end_ms) // 2
        else:
            try:
                ass_text = subtitle.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise BurnError(f"字幕读取失败（{subtitle.name}）：{exc}") from exc
            if options.verify:
                verify_at_ms = _best_ass_cue_ms(ass_text)

        # 位置统一在这里改写（.ass 输入也重写一份）：底部居中 / 顶部居中
        ass_path = out_dir / "subtitles.ass"
        ass_path.write_text(
            _apply_position(ass_text, options.position), encoding="utf-8", newline="\n"
        )

        video_info = _probe(video)
        input_duration = _duration_ms(video_info)
        return cls(
            options=options,
            video=video,
            subtitle=subtitle,
            out_dir=out_dir,
            started=started,
            ass_path=ass_path,
            cue_count=cue_count,
            verify_at_ms=verify_at_ms,
            encoding=encoding,
            video_info=video_info,
            input_duration=input_duration,
            out_video=out_dir / "burned.mp4",
        )


def _burn_video(session: _BurnSession) -> None:
    """烧录一次并探测产物（时长/流类型）：这是唯一的重活。"""

    options = session.options
    # 全局参数（-v error -y）由 media.run_ffmpeg 统一前置，这里只给输入/滤镜/编码参数
    media.run_ffmpeg(
        [
            "-i",
            str(session.video),
            "-vf",
            f"ass={_filter_path(session.ass_path)}",
            "-c:v",
            "libx264",
            "-crf",
            str(options.crf),
            "-preset",
            options.preset,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(session.out_video),
        ],
        error_cls=BurnError,
        what="字幕烧录",
    )

    session.output_info = _probe(session.out_video)
    session.output_duration = _duration_ms(session.output_info)
    session.stream_kinds = {
        str(stream.get("codec_type")): str(stream.get("codec_name") or "")
        for stream in (session.output_info.get("streams") or [])
        if isinstance(stream, dict)
    }


def _verify_burn(session: _BurnSession) -> None:
    """像素差核验：字幕位置上的区域确实变了才算烧上去。"""

    options = session.options
    if options.verify and session.verify_at_ms is not None:
        difference, strong_ratio = _region_diff(
            session.video, session.out_video, session.verify_at_ms, options.position
        )
        ok = (
            difference >= options.verify_threshold
            or strong_ratio >= options.strong_ratio_threshold
        )
        session.verification = {
            "at_ms": session.verify_at_ms,
            "region": options.position,
            "region_pixel_diff": round(difference, 3),
            "strong_pixel_ratio": round(strong_ratio, 5),
            "threshold": options.verify_threshold,
            "strong_ratio_threshold": options.strong_ratio_threshold,
            "ok": ok,
        }
    if session.verification is not None and not session.verification["ok"]:
        session.warnings.append(
            "字幕烧录核验未通过（画面底部像素差过小），请检查 ASS 字体/滤镜是否生效"
        )
    if session.stream_kinds.get("audio") is None:
        session.warnings.append("输出没有音频流：输入可能本身无音轨")
    if session.verification is not None and not session.verification["ok"]:
        session.status = "warn"


def _write_outputs(session: _BurnSession) -> BurnResult:
    """写 ``burn.json`` 并返回命令行契约（核验不过 → 退出码 2）。"""

    options = session.options
    video_size, video_sha = file_digest(session.video)
    subtitle_size, subtitle_sha = file_digest(session.subtitle)
    out_size, out_sha = file_digest(session.out_video)
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "burn",
        "generated_at": (
            options.generated_at or datetime.now().astimezone()
        ).isoformat(),
        "status": session.status,
        "input": {
            "video": {
                "path": str(session.video.resolve()),
                "bytes": video_size,
                "sha256": video_sha,
                "duration_s": round(session.input_duration / 1000, 3),
            },
            "subtitle": {
                "path": str(session.subtitle.resolve()),
                "bytes": subtitle_size,
                "sha256": subtitle_sha,
                "encoding": session.encoding or None,
                "cues": session.cue_count,
            },
        },
        "config": {
            "crf": options.crf,
            "preset": options.preset,
            "position": options.position,
            "verify": options.verify,
            "verify_threshold": options.verify_threshold,
            "strong_ratio_threshold": options.strong_ratio_threshold,
            "ass": str(session.ass_path.resolve()),
        },
        "output": {
            "path": str(session.out_video.resolve()),
            "bytes": out_size,
            "sha256": out_sha,
            "duration_s": round(session.output_duration / 1000, 3),
            "codecs": session.stream_kinds,
            "duration_delta_ms": session.output_duration - session.input_duration,
        },
        "verification": session.verification,
        "warnings": session.warnings,
        "wall_s": round(time.monotonic() - session.started, 2),
    }
    report_path = session.out_dir / "burn.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return BurnResult(
        status=session.status,
        exit_code=0 if session.status == "ok" else 2,
        report=report,
        report_path=report_path.resolve(),
        video_path=session.out_video.resolve(),
        ass_path=session.ass_path.resolve(),
    )
