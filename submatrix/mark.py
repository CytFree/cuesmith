"""FR-4：AIGC 标识（显式烧帧 + 隐式元数据）。

依据《人工智能生成合成内容标识办法》（2025-09-01 施行）：
- 第四条：**显式标识**要出现在视频起始画面，且随文件一起导出（不能只在外壳页面提示）
- 第五条：**隐式标识**要写进文件元数据
- 第十条：禁止删除、篡改标识 → 所以本模块只提供"默认开启 + 自用模式留痕"，不提供移除开关

本机实测出来的三个坑（都已固化在实现/测试里）：
1. 自定义元数据键（`content_id`）不加 `-movflags use_metadata_tags` 会被 MP4 **静默丢弃**；
2. 标识文案走 `textfile=`，避免过滤器转义（含 `:` `,` `[` `%` 的文案照样完整渲染）；
3. 字体路径不存在时 ffmpeg 不是报错而是**访问违例崩溃**，所以先自己 `is_file()` 校验。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import atomic
from . import __version__
from . import media
from .probe import MediaProbe, probe_media
from .report import file_digest

__all__ = [
    "DEFAULT_COMMENT",
    "DEFAULT_LABEL",
    "DEFAULT_POSITION",
    "DEFAULT_PROVIDER",
    "DEFAULT_SECONDS",
    "FONT_CANDIDATES",
    "METADATA_FIELDS",
    "MIN_LABEL_SECONDS",
    "POSITIONS",
    "LabelGeometry",
    "MarkError",
    "MarkOptions",
    "MarkResult",
    "build_drawtext",
    "build_metadata",
    "escape_filter_path",
    "label_geometry",
    "new_content_id",
    "resolve_font",
    "run_mark",
    "verify_output",
]

DEFAULT_LABEL = "AI 生成"
DEFAULT_SECONDS = 3.0
DEFAULT_POSITION = "top-right"
DEFAULT_PROVIDER = "submatrix"
DEFAULT_COMMENT = "AI 生成内容：AI 翻译 + AI 配音"
# PRD 要求显式标识 ≥3 秒；低于这个值直接拒绝（比事后补救便宜）
MIN_LABEL_SECONDS = 3.0

POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left")
METADATA_FIELDS = ("comment", "artist", "copyright", "content_id")

# 中文字形覆盖优先：微软雅黑 > 雅黑粗体 > 黑体 > 宋体；后面两个是给非 Windows 环境留的
FONT_CANDIDATES: tuple[Path, ...] = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
)

# MP4 能直接 copy 的音轨编码；其余转 AAC
MP4_AUDIO_CODECS = frozenset({"aac", "mp3", "mp2", "ac3", "eac3", "alac"})

# 自用模式的留痕文案（AC-12：唯一可以不打标的途径，且必须留证）
SELF_USE_NOTICE = (
    "本文件未写入 AIGC 标识，仅限自用，不得公开发布。"
    "《人工智能生成合成内容标识办法》第十条禁止删除、篡改、伪造标识；"
    "如需发布，请重新执行 mark 命令生成带标识的版本。"
)


class MarkError(RuntimeError):
    """参数、字体、编码等不可继续的错误。"""


@dataclass(frozen=True, slots=True)
class LabelGeometry:
    """显式标识的版式：字号随分辨率缩放，位置用 ffmpeg 表达式表达。"""

    font_size: int
    margin: int
    padding: int
    x_expr: str
    y_expr: str


def escape_filter_path(path: Path | str) -> str:
    """把路径塞进 ffmpeg 过滤器字符串。

    ffmpeg 的滤镜描述要过两层解析（滤镜图 / 选项），盘符冒号因此要写成 ``\\\\:``；
    实测这条规则在含空格的路径上同样成立，所以不必额外加引号。
    """

    return str(path).replace("\\", "/").replace(":", r"\\:")


def label_geometry(width: int, height: int, position: str = DEFAULT_POSITION) -> LabelGeometry:
    if position not in POSITIONS:
        raise ValueError(f"不支持的位置：{position}（可选 {'、'.join(POSITIONS)}）")

    font_size = max(20, round(height * 0.05))
    margin = max(12, round(height * 0.02))
    padding = max(8, round(font_size / 3))

    y_expr = str(margin) if position.startswith("top") else f"h-th-{margin}"
    x_expr = f"w-tw-{margin}" if position.endswith("right") else str(margin)
    return LabelGeometry(
        font_size=font_size, margin=margin, padding=padding, x_expr=x_expr, y_expr=y_expr
    )


def build_drawtext(
    *,
    textfile: Path,
    font_file: Path,
    geometry: LabelGeometry,
    seconds: float,
) -> str:
    """拼 drawtext 过滤器。

    文案走 ``textfile=``：用户改文案（含冒号/逗号/方括号）也不用担心过滤器转义。
    """

    options = [
        f"textfile={escape_filter_path(textfile)}",
        "expansion=none",
        f"fontfile={escape_filter_path(font_file)}",
        f"fontsize={geometry.font_size}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.45",
        f"boxborderw={geometry.padding}",
        f"x={geometry.x_expr}",
        f"y={geometry.y_expr}",
        f"enable='between(t,0,{seconds:.3f})'",
    ]
    return "drawtext=" + ":".join(options)


def resolve_font(
    explicit: Path | str | None = None,
    *,
    candidates: tuple[Path, ...] | None = None,
) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise MarkError(f"字体文件不存在：{path}（请用 --font-file 指定一个 .ttf/.ttc）")
        return path

    for candidate in FONT_CANDIDATES if candidates is None else candidates:
        candidate_path = Path(candidate)
        if candidate_path.is_file():
            return candidate_path

    raise MarkError("找不到可用的中文字体：请用 --font-file 指定一个 .ttf/.ttc 字体文件")


def new_content_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class MarkOptions:
    src: Path
    out: Path
    label_text: str = DEFAULT_LABEL
    label_seconds: float = DEFAULT_SECONDS
    position: str = DEFAULT_POSITION
    provider: str = DEFAULT_PROVIDER
    comment: str = DEFAULT_COMMENT
    copyright_text: str | None = None
    font_file: Path | None = None
    content_id: str | None = None  # 测试注入用；CLI 永远现生成
    self_use: bool = False
    generated_at: datetime | None = None

    def validate(self) -> None:
        if not self.label_text.strip():
            raise MarkError("标识文案不能为空")
        if self.label_seconds < MIN_LABEL_SECONDS:
            raise MarkError(
                f"显式标识时长不得短于 {MIN_LABEL_SECONDS:g} 秒（当前 {self.label_seconds:g} 秒）"
            )
        if self.position not in POSITIONS:
            raise MarkError(f"不支持的位置：{self.position}（可选 {'、'.join(POSITIONS)}）")
        if not self.provider.strip():
            raise MarkError("服务提供者名称不能为空")


@dataclass(frozen=True, slots=True)
class MarkResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    video_path: Path
    label_frame: Path | None


def build_metadata(options: MarkOptions, content_id: str) -> dict[str, str]:
    return {
        "comment": options.comment,
        "artist": options.provider,
        "copyright": options.copyright_text or options.provider,
        "content_id": content_id,
    }


# ---------------------------------------------------------------- 核验


def _format_info(path: Path) -> dict:
    payload = media.ffprobe_json(
        path, error_cls=MarkError, streams=False, what=f"ffprobe 读不了 {path}"
    )
    info = payload.get("format")
    return info if isinstance(info, dict) else {}


def _frame_gray(path: Path, at: float) -> bytes:
    completed = media.run_ffmpeg(
        [
            "-i",
            str(path),
            "-ss",
            f"{at:.3f}",
            "-frames:v",
            "1",
            "-vf",
            "format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        error_cls=MarkError,
        what="抽帧",
        # 抽帧失败要按"有没有取到画面"判定，退出码交给下面的检查
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        detail = media.stderr_text(completed)
        raise MarkError(f"抽帧失败（{path} @ {at}s）：{detail or '没有取到画面'}")
    return completed.stdout


def _frame_diff(source: Path, output: Path, at: float, *, pixel_threshold: int = 40) -> float:
    first = _frame_gray(source, at)
    second = _frame_gray(output, at)
    if len(first) != len(second):
        raise MarkError("源片与产物的分辨率不一致，无法核验标识")
    changed = sum(1 for a, b in zip(first, second) if abs(a - b) > pixel_threshold)
    return changed / len(first)


LABEL_DIFF_THRESHOLD = 0.0005


def verify_output(
    *,
    source: Path,
    output: Path,
    expected_metadata: dict[str, str],
    label_seconds: float,
) -> dict[str, object]:
    """产物的双重核验：元数据能被 ffprobe 读回 + 标识真的画在画面起始几秒里。"""

    info = _format_info(output)
    tags = info.get("tags") or {}
    fields = (
        {field: tags.get(field) == expected_metadata[field] for field in METADATA_FIELDS}
        if expected_metadata
        else {}
    )
    missing = [field for field, ok in fields.items() if not ok]
    metadata_ok = not missing if expected_metadata else True

    duration = float(info["duration"]) if info.get("duration") else None
    after_at = label_seconds + 0.5
    if duration is not None and after_at > duration - 0.2:
        after_at = max(0.1, duration - 0.2)

    during_points = sorted(
        {round(min(1.0, label_seconds / 2), 2), round(max(0.3, label_seconds - 0.5), 2)}
    )
    samples: list[dict[str, object]] = [
        {"at": at, "expected": "label", "diff": round(_frame_diff(source, output, at), 5)}
        for at in during_points
    ]
    samples.append(
        {"at": round(after_at, 2), "expected": "none", "diff": round(_frame_diff(source, output, after_at), 5)}
    )

    labeled = [sample["diff"] for sample in samples if sample["expected"] == "label"]
    after_diff = samples[-1]["diff"]
    label_ok = all(diff >= LABEL_DIFF_THRESHOLD for diff in labeled) and after_diff < max(
        LABEL_DIFF_THRESHOLD, 0.2 * min(labeled)
    )

    return {
        "metadata_roundtrip": {"ok": metadata_ok, "fields": fields, "missing": missing},
        "explicit_label": {
            "ok": label_ok,
            "samples": samples,
            "threshold": LABEL_DIFF_THRESHOLD,
        },
    }


# ---------------------------------------------------------------- 执行


def _run_ffmpeg(args: list[str], *, what: str) -> None:
    media.run_ffmpeg(args, error_cls=MarkError, what=what)


def _video_size(probe: MediaProbe) -> tuple[int, int]:
    if not probe.video:
        raise MarkError("输入文件里没有视频流")
    info = probe.video[0]
    if not info.width or not info.height:
        raise MarkError("读不到视频分辨率，无法确定标识版式")
    return info.width, info.height


def _audio_args(probe: MediaProbe) -> list[str]:
    if all(info.codec_name in MP4_AUDIO_CODECS for info in probe.audio):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def run_mark(
    options: MarkOptions,
    *,
    verifier: Callable[..., dict[str, object]] = verify_output,
) -> MarkResult:
    """在成片上打 AIGC 显式标识 + 写隐式元数据，并自动回读核验。

    两条路：``self_use`` 只转封装 + 落免责声明；正常模式烧标识、写元数据、抽证物帧
    并回读核验（核验不过 → ``verify_failed``，退出码 2）。
    """

    session = _MarkSession.start(options)
    if session.options.self_use:
        _mark_self_use(session)
    else:
        _mark_labeled(session, verifier=verifier)
    return _write_outputs(session)


@dataclass(slots=True)
class _MarkSession:
    """一次标识的现场：探测结果、路径、核验结论与告警。"""

    options: MarkOptions
    source: Path
    out_dir: Path
    video_path: Path
    report_path: Path
    generated_at: datetime
    content_id: str
    probe: MediaProbe
    duration_s: float | None
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    label_frame: Path | None = None
    font: Path | None = None
    geometry: LabelGeometry | None = None
    verification: dict[str, object] = field(default_factory=dict)
    status: str = "marked"

    @classmethod
    def start(cls, options: MarkOptions) -> "_MarkSession":
        """校验参数、探测源片、准备路径与元数据——开工前的一次性准备。"""

        options.validate()

        source = Path(options.src).resolve()
        if not source.is_file():
            raise MarkError(f"输入文件不存在：{source}")

        out_dir = Path(options.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        generated_at = options.generated_at or datetime.now().astimezone()
        content_id = options.content_id or new_content_id()

        probe = probe_media(source)
        duration_s = probe.duration_s
        notes: list[str] = []
        if duration_s is not None and duration_s < options.label_seconds + 1:
            notes.append(
                f"输入只有 {duration_s:.2f} 秒，短于标识时长 + 1 秒，核验点已按片长裁剪"
            )

        return cls(
            options=options,
            source=source,
            out_dir=out_dir,
            video_path=out_dir / "marked.mp4",
            report_path=out_dir / "mark.json",
            generated_at=generated_at,
            content_id=content_id,
            probe=probe,
            duration_s=duration_s,
            notes=notes,
            metadata=build_metadata(options, content_id),
        )


def _mark_self_use(session: _MarkSession) -> None:
    """自用模式：只转封装保证可播，不烧标识；落一份免责声明并跳过核验。"""

    _run_ffmpeg(
        [
            "-i",
            str(session.source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(session.video_path),
        ],
        what="自用模式转封装",
    )
    notice = {
        "schema_version": 1,
        "tool": "submatrix",
        "command": "mark",
        "mode": "self_use",
        "generated_at": session.generated_at.isoformat(),
        "notice": SELF_USE_NOTICE,
        "input": {
            "path": str(session.source),
            "bytes": session.source.stat().st_size,
            "sha256": file_digest(session.source)[1],
        },
        "output": {"path": str(session.video_path.resolve())},
    }
    atomic.write_json(session.out_dir / "self_use_notice.json", notice)
    session.status = "self_use"
    session.verification = {
        "metadata_roundtrip": {"ok": True, "skipped": True, "fields": {}, "missing": []},
        "explicit_label": {"ok": True, "skipped": True, "samples": [], "threshold": 0.0},
    }
    session.metadata = {}


def _mark_labeled(
    session: _MarkSession,
    *,
    verifier: Callable[..., dict[str, object]],
) -> None:
    """正常模式：烧显式标识 + 写隐式元数据，抽一张证物帧并回读核验。"""

    options = session.options
    session.font = resolve_font(options.font_file)
    width, height = _video_size(session.probe)
    session.geometry = label_geometry(width, height, options.position)

    label_file = session.out_dir / "label.txt"
    label_file.write_text(options.label_text, encoding="utf-8", newline="\n")
    filter_text = build_drawtext(
        textfile=label_file.resolve(),
        font_file=session.font,
        geometry=session.geometry,
        seconds=options.label_seconds,
    )

    metadata_args: list[str] = []
    for key, value in session.metadata.items():
        metadata_args += ["-metadata", f"{key}={value}"]

    _run_ffmpeg(
        [
            "-i",
            str(session.source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            filter_text,
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            *_audio_args(session.probe),
            "-movflags",
            "+faststart+use_metadata_tags",
            *metadata_args,
            str(session.video_path),
        ],
        what="烧标识",
    )

    session.label_frame = session.out_dir / "label_frame.png"
    _run_ffmpeg(
        [
            "-i",
            str(session.video_path),
            "-ss",
            f"{min(1.0, options.label_seconds / 2):.3f}",
            "-frames:v",
            "1",
            str(session.label_frame),
        ],
        what="抽取标识证物帧",
    )

    session.verification = verifier(
        source=session.source,
        output=session.video_path,
        expected_metadata=session.metadata,
        label_seconds=options.label_seconds,
    )
    verified = bool(session.verification["metadata_roundtrip"]["ok"]) and bool(
        session.verification["explicit_label"]["ok"]
    )
    session.status = "marked" if verified else "verify_failed"
    if not verified:
        session.notes.append("自动核验未通过：请检查标识是否真的烧上、元数据是否写进容器")


def _write_outputs(session: _MarkSession) -> MarkResult:
    """写 ``mark.json`` 并返回命令行契约（核验不过 → 退出码 2）。"""

    options = session.options
    output_info = _format_info(session.video_path)
    output_size, output_sha = file_digest(session.video_path)
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "mark",
        "generated_at": session.generated_at.isoformat(),
        "status": session.status,
        "input": {
            "path": str(session.source),
            "bytes": session.source.stat().st_size,
            "sha256": file_digest(session.source)[1],
            "duration_s": session.duration_s,
            "format_name": session.probe.format_name,
        },
        "config": {
            "label_text": options.label_text,
            "label_seconds": options.label_seconds,
            "position": options.position,
            "font_file": str(session.font) if session.font is not None else None,
            "font_size": session.geometry.font_size if session.geometry is not None else None,
            "provider": options.provider,
            "self_use": options.self_use,
        },
        "metadata": session.metadata,
        "output": {
            "path": str(session.video_path.resolve()),
            "bytes": output_size,
            "sha256": output_sha,
            "duration_s": float(output_info["duration"]) if output_info.get("duration") else None,
        },
        "verification": session.verification,
        "label_frame": str(session.label_frame) if session.label_frame is not None else None,
        "notes": session.notes,
    }
    atomic.write_json(session.report_path, report)

    return MarkResult(
        status=session.status,
        exit_code=0 if session.status in {"marked", "self_use"} else 2,
        report=report,
        report_path=session.report_path.resolve(),
        video_path=session.video_path.resolve(),
        label_frame=(
            session.label_frame.resolve() if session.label_frame is not None else None
        ),
    )
