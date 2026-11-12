"""ASR 直连（FR-5）：词级时间戳 + 热词偏置 + 按语义切条。

为什么要绕到底座之外再做一个 ASR 入口（而不是直接用底座的 SRT）：

1. **词级时间戳**：底座只给段级 SRT，切句只能靠"段尾有没有句号"猜；有词级时间戳才能
   在停顿/句末标点处切，而不是在 12 秒硬上限处把句子拦腰砍断（M4 实测：merge 后仍有
   cue 以 "and it could" 结尾，就是 12 秒上限切的）。
2. **热词偏置**：产品名、人名、术语通过 ``hotwords`` / ``initial_prompt`` 喂给模型，
   专有名词识别率明显上升（收费软件的叫法是 hotword boosting / custom vocabulary）。
3. **模型隔离**：faster-whisper 的 ctranslate2 版本很敏感（4.8.x 在 Windows 上访问违例），
   所以模型只跑在底座 venv 里（``--asr-python``），主包保持"标准库 + ffmpeg"。

切条规则与 ``merge`` 一致：**只在真句末、长停顿、超长/超宽时切**，不为了凑条数切。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from . import atomic
from . import __version__
from .interpreters import (
    Requirement,
    ordered_candidates,
    python_in_venv,
    resolve,
    runner_probe,
)
from .report import OutputFile, file_digest
from .srt import Cue, Subtitle, Timing, dump_srt
from .worker_protocol import run_once, subprocess_runner

__all__ = [
    "AsrError",
    "AsrOptions",
    "AsrResult",
    "Segment",
    "find_model",
    "find_python",
    "run_asr",
    "segment_words",
]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2

SENTENCE_END = set("。！？.!?…")
_BRACKETED = re.compile(r"^[\[\(（【][^\]\)）】]*[\]\)）】]$")
_NON_SPEECH_WORDS = {
    "music", "applause", "laughter", "silence", "noise", "音乐", "掌声", "笑声", "静音", "噪音",
}
_HAS_CONTENT = re.compile(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_EDGE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LONE_I = re.compile(r"(?<![A-Za-z])i(?![A-Za-z])")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class AsrError(Exception):
    """ASR 环境不可用 / 模型缺失 / 识别失败。"""


@dataclass(frozen=True, slots=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str
    word_count: int = 0


@dataclass(frozen=True, slots=True)
class AsrOptions:
    src: Path
    out: Path
    language: str = "auto"
    model: str | None = None
    python: str | None = None
    hotwords: tuple[str, ...] = ()
    initial_prompt: str | None = None
    beam_size: int = 5
    compute_type: str = "int8"
    device: str = "cpu"
    cpu_threads: int = 0
    vad: bool = True
    max_duration_ms: int = 7000
    max_chars: int = 200
    pause_ms: int = 500
    min_duration_ms: int = 800  # 字幕行业惯例：避免一闪而过的短条（过短并到相邻条）
    srt_name: str = "source.srt"
    keep_raw: bool = False
    generated_at: datetime | None = None
    runner: Runner | None = None  # 测试注入


@dataclass(frozen=True, slots=True)
class AsrResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    srt_path: Path | None
    words_path: Path | None


# ---------------------------------------------------------------------- 环境


_default_runner = subprocess_runner(3600)  # 长视频 medium 模型可能跑很久

_ASR_PYTHON = Requirement(
    label="faster-whisper",
    module="faster_whisper",
    parameter="--asr-python",
    env_var="SUBMATRIX_ASR_PYTHON",
    hint="例如 spike/pyvideotrans/.venv/Scripts/python.exe（或自建 venv 后用 --asr-python 指过去）。",
)


def _search_pythons() -> list[str]:
    """仓库里值得一看的解释器（显式参数与环境变量由 resolve 统一排在最前面）。"""

    root = Path(__file__).resolve().parents[1]
    return ordered_candidates(
        python_in_venv(root / "spike" / "pyvideotrans" / ".venv"),
        python_in_venv(root / "spike" / "asr" / ".venv"),
        (sys.executable,),
    )


def find_python(explicit: str | None, *, runner: Runner | None = None) -> str:
    """找一个真的能 ``import faster_whisper`` 的解释器（策略见 interpreters）。"""

    run = runner or _default_runner
    found = resolve(
        _ASR_PYTHON,
        explicit,
        candidates=_search_pythons(),
        probe=runner_probe(run, _ASR_PYTHON.module),
        error_cls=AsrError,
    )
    if found is None:  # 契约上走不到：on_missing=raise 要么返回解释器、要么已抛错
        raise AsrError("内部错误：解释器探测既没返回结果也没报错")
    return found


def local_models() -> dict[str, Path]:
    """常见模型名 → 项目内本地目录（体积大，不放在 venv 缓存里；离线可用）。

    small 用底座 venv 里的平铺目录（CTranslate2 要求 model.bin 直接在目录下）；
    medium 是本项目从 ModelScope 下的平铺目录。
    """

    root = Path(__file__).resolve().parents[1]
    return {
        "small": root / "spike" / "pyvideotrans" / "models" / "models--Systran--faster-whisper-small",
        "medium": root / "spike" / "whisper-models" / "faster-whisper-medium",
    }


def _model_dir_ok(directory: Path) -> Path | None:
    """校验一个"模型目录"真的能加载：平铺目录，或 HF 缓存的 snapshots/<hash>/。"""

    if (directory / "model.bin").is_file():
        return directory
    snapshots = directory / "snapshots"
    if snapshots.is_dir():
        found = sorted(snapshots.glob("*/model.bin"))
        if found:
            return found[0].parent
    return None


# 中日韩语音用 small 明显不够：真实中文采访实测 small 把「刘翔」识别成「流向」，
# medium 识别正确。默认按源语言选模型（本地没有 medium 时自动退回 small）。
DEFAULT_MODEL_BY_LANGUAGE = {"zh": "medium", "yue": "medium", "ja": "medium", "ko": "medium"}


def preferred_model(language: str) -> str | None:
    """按源语言推荐模型名；本地没这个模型就不推荐（调用方退回 small）。"""

    name = DEFAULT_MODEL_BY_LANGUAGE.get((language or "").lower())
    if not name:
        return None
    directory = local_models().get(name)
    return name if directory is not None and _model_dir_ok(directory) else None


def model_variant(model: str) -> str:
    """从模型路径/名字里认出 small / medium / large / tiny（报告用）。"""

    lowered = str(model).lower()
    for name in ("large", "medium", "small", "tiny"):
        if name in lowered:
            return name
    return "custom"


def find_model(explicit: str | None) -> str:
    """定位本地 faster-whisper 模型目录；显式给的名字允许交给 faster-whisper 自己解析。"""

    import os

    root = Path(__file__).resolve().parents[1]
    models = local_models()

    if explicit:
        path = Path(explicit)
        resolved = _model_dir_ok(path)
        if resolved is not None:
            return str(resolved)
        if explicit in models:
            resolved = _model_dir_ok(models[explicit])
            if resolved is not None:
                return str(resolved)
        # 显式给的是模块名（small / medium），交给 faster-whisper；下载失败会报在 worker 里
        return explicit

    candidates = [
        os.environ.get("SUBMATRIX_ASR_MODEL"),
        str(models["small"]),
        str(root / "spike" / "whisper-models" / "models--Systran--faster-whisper-small"),
        str(root / "models" / "faster-whisper-small"),
    ]
    for candidate in candidates:
        if candidate:
            resolved = _model_dir_ok(Path(candidate))
            if resolved is not None:
                return str(resolved)
    raise AsrError(
        "没找到本地 faster-whisper 模型。用 --model 指定模型目录"
        "（例如 spike/pyvideotrans/models/models--Systran--faster-whisper-small）"
    )


# ---------------------------------------------------------------------- 切条


def _word_text(words: Sequence[dict[str, object]]) -> str:
    raw = "".join(str(word.get("word", "")) for word in words)
    return " ".join(raw.split())


def _join_segment_text(previous: str, following: str) -> str:
    """合并两条时中文不加空格（"向"+"他靠近" 拼回 "向他靠近"，而不是 "向 他靠近"）。"""

    if (
        previous
        and following
        and _CJK_EDGE.search(previous[-1])
        and _CJK_EDGE.search(following[0])
    ):
        return previous + following
    return f"{previous} {following}".strip()


def _tidy(text: str) -> str:
    """whisper 的首词常是小写（段首空格被 strip 后），字幕要按句首大写显示。"""

    if not text:
        return text
    if text[0].isascii() and text[0].islower():
        text = text[0].upper() + text[1:]
    return _LONE_I.sub("I", text)


def _is_non_speech(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not _HAS_CONTENT.search(stripped):
        return True
    lowered = stripped.strip("[]()（）【】：: ").lower()
    return lowered in _NON_SPEECH_WORDS


def _ends_sentence(word: dict[str, object]) -> bool:
    token = str(word.get("word", "")).strip()
    return bool(token) and token[-1] in SENTENCE_END


def segment_words(
    words: Sequence[dict[str, object]],
    *,
    max_duration_ms: int = 7000,
    max_chars: int = 200,
    pause_ms: int = 500,
    min_duration_ms: int = 400,
) -> list[Segment]:
    """把词级时间戳切成字幕条：句末标点 / 长停顿 / 超长 / 超宽才切。"""

    segments: list[Segment] = []
    current: list[dict[str, object]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = _tidy(_word_text(current))
        if not text:
            current = []
            return
        start_ms = round(float(current[0]["start"]) * 1000)
        end_ms = round(float(current[-1]["end"]) * 1000)
        segments.append(Segment(start_ms, max(end_ms, start_ms + 1), text, len(current)))
        current = []

    for word in words:
        if "start" not in word or "end" not in word:
            continue
        if not current:
            current = [word]
        else:
            gap_ms = (float(word["start"]) - float(current[-1]["end"])) * 1000
            duration_ms = (float(word["end"]) - float(current[0]["start"])) * 1000
            projected = len(_word_text([*current, word]))
            if gap_ms >= pause_ms or duration_ms > max_duration_ms or projected > max_chars:
                flush()
                current = [word]
            else:
                current.append(word)
        if _ends_sentence(word):
            flush()
    flush()

    filtered = [segment for segment in segments if not _is_non_speech(segment.text)]
    return _merge_tiny(
        filtered,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        max_chars=max_chars,
    )


def _merge_tiny(
    segments: Sequence[Segment], *, min_duration_ms: int, max_duration_ms: int, max_chars: int
) -> list[Segment]:
    """过短的条并到相邻条上（不超过单条上限）；避免一闪而过的字幕。

    合并时允许比 ``max_duration_ms`` 超出一个小容差（≤1s）：不给容差的话，长句末尾
    那几个字会单独留成 0.5s 的闪条（真实中文素材实测：39.28–39.82s "他靠近"）。
    """

    merge_limit_ms = max_duration_ms + max(min_duration_ms, 1000)
    merged: list[Segment] = []
    for segment in segments:
        if merged:
            previous = merged[-1]
            previous_long = previous.end_ms - previous.start_ms < min_duration_ms
            combined_text = _join_segment_text(previous.text, segment.text)
            fits = (
                segment.end_ms - previous.start_ms <= merge_limit_ms
                and len(combined_text) <= max_chars
            )
            if previous_long and fits:
                merged[-1] = Segment(
                    previous.start_ms, segment.end_ms, combined_text,
                    previous.word_count + segment.word_count,
                )
                continue
        merged.append(segment)
    # 末尾仍过短的条并回上一条
    if len(merged) > 1 and merged[-1].end_ms - merged[-1].start_ms < min_duration_ms:
        tail = merged.pop()
        previous = merged[-1]
        if tail.end_ms - previous.start_ms <= merge_limit_ms:
            merged[-1] = Segment(
                previous.start_ms, tail.end_ms, _join_segment_text(previous.text, tail.text),
                previous.word_count + tail.word_count,
            )
        else:
            merged.append(tail)
    return merged


# ---------------------------------------------------------------------- 主流程


def run_asr(options: AsrOptions) -> AsrResult:
    """把音/视频送进 ASR worker，按句切条出 SRT + 词级时间戳 + 报告。

    四个阶段：准备（解释器/模型）→ 跑 worker → 切条与判读 → 落盘与报告。
    """

    session = _AsrSession.start(options)
    _run_worker(session)
    _build_cues(session)
    return _write_outputs(session)


@dataclass(slots=True)
class _AsrSession:
    """一次 ASR 的现场：解释器/模型、worker 原始结果、切条结论与统计。"""

    options: AsrOptions
    source: Path
    runner: Runner
    python: str
    model: str
    out_dir: Path
    raw_path: Path
    started: float
    payload: dict[str, object] = field(default_factory=dict)
    words: list[dict[str, object]] = field(default_factory=list)
    raw_segments: list[object] = field(default_factory=list)
    segments: list = field(default_factory=list)
    cues: tuple[Cue, ...] = ()
    wall: float = 0.0
    duration_s: float = 0.0
    speech_s: float = 0.0
    status: str = "ok"
    warnings: list[str] = field(default_factory=list)
    outputs: list[OutputFile] = field(default_factory=list)
    srt_path: Path | None = None
    words_path: Path | None = None

    @classmethod
    def start(cls, options: AsrOptions) -> "_AsrSession":
        """校验输入、定位解释器与模型、建输出目录——开工前的一次性准备。"""

        source = Path(options.src)
        if not source.is_file():
            raise AsrError(f"输入文件不存在：{source}")

        runner = options.runner or _default_runner
        python = find_python(options.python, runner=runner)
        # 显式 --model 优先；否则按源语言挑（中文默认 medium，本地没有就退回 small）
        model = find_model(options.model or preferred_model(options.language))

        out_dir = Path(options.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / "asr-worker.json"
        return cls(
            options=options,
            source=source,
            runner=runner,
            python=python,
            model=model,
            out_dir=out_dir,
            raw_path=raw_path,
            started=0.0,
        )


def _asr_command(session: _AsrSession) -> list[str]:
    """派给 asr_worker 的完整命令行（可选开关按需追加）。"""

    options = session.options
    command = [
        session.python,
        str(Path(__file__).with_name("asr_worker.py")),
        "--audio",
        str(session.source),
        "--model",
        session.model,
        "--output",
        str(session.raw_path),
        "--beam-size",
        str(options.beam_size),
        "--compute-type",
        options.compute_type,
        "--device",
        options.device,
        "--cpu-threads",
        str(options.cpu_threads),
    ]
    if options.language and options.language != "auto":
        command.extend(["--language", options.language])
    if options.hotwords:
        command.extend(["--hotwords", ", ".join(options.hotwords)])
    if options.initial_prompt:
        command.extend(["--initial-prompt", options.initial_prompt])
    if not options.vad:
        command.append("--no-vad")
    return command


def _run_worker(session: _AsrSession) -> None:
    """跑一次 ASR worker（独立解释器 + 子进程），拿回词级时间戳与语言判定。"""

    session.started = time.monotonic()
    session.payload = run_once(
        _asr_command(session),
        session.raw_path,
        error_cls=AsrError,
        runner=session.runner,
        label="ASR",
        failure_label="ASR 识别",
    )


def _build_cues(session: _AsrSession) -> None:
    """把词级时间戳切成字幕条，并按语言判定/空结果给出状态与告警。"""

    options = session.options
    session.words = list(session.payload.get("words") or [])
    session.raw_segments = list(session.payload.get("segments") or [])
    session.segments = segment_words(
        session.words,
        max_duration_ms=options.max_duration_ms,
        max_chars=options.max_chars,
        pause_ms=options.pause_ms,
        min_duration_ms=options.min_duration_ms,
    )
    session.wall = time.monotonic() - session.started
    session.duration_s = float(session.payload.get("duration") or 0.0)
    session.speech_s = (
        sum(segment.end_ms - segment.start_ms for segment in session.segments) / 1000
    )

    probability = session.payload.get("language_probability")
    if not session.segments:
        session.status = "empty"
        session.warnings.append("没有识别出任何语音")
    if isinstance(probability, (int, float)) and probability < 0.6:
        session.warnings.append(
            f"语言识别置信度只有 {probability:.2f}，建议用 --language 指定语言"
        )
    if options.language not in ("auto", None) and session.payload.get("language") != options.language:
        session.warnings.append(
            f"识别语言是 {session.payload.get('language')}，与指定语言 {options.language} 不一致"
        )

    session.cues = tuple(
        Cue(timing=Timing(segment.start_ms, segment.end_ms), text=segment.text)
        for segment in session.segments
    )


def _write_outputs(session: _AsrSession) -> AsrResult:
    """写 SRT、``words.json`` 与 ``asr.json``，清掉中间产物，返回命令行契约。"""

    options = session.options
    payload = session.payload
    probability = payload.get("language_probability")
    session.srt_path = session.out_dir / options.srt_name
    session.words_path = session.out_dir / "words.json"
    if session.cues:
        session.srt_path.write_text(
            dump_srt(Subtitle(cues=session.cues)), encoding="utf-8", newline="\n"
        )
        session.outputs.append(
            OutputFile(
                kind="asr_srt",
                path=session.srt_path.resolve(),
                cues=len(session.cues),
                size=session.srt_path.stat().st_size,
            )
        )
    atomic.write_json(session.words_path, {
                "language": payload.get("language"),
                "language_probability": probability,
                "duration_s": session.duration_s,
                "words": session.words,
            })
    session.outputs.append(
        OutputFile(
            kind="word_timestamps",
            path=session.words_path.resolve(),
            cues=len(session.words),
            size=session.words_path.stat().st_size,
        )
    )

    size, digest = file_digest(session.source)
    generated_at = options.generated_at or datetime.now().astimezone()
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "asr",
        "generated_at": generated_at.isoformat(),
        "status": session.status,
        "input": {
            "path": str(session.source.resolve()),
            "bytes": size,
            "sha256": digest,
            "duration_s": round(session.duration_s, 2),
        },
        "config": {
            "model": session.model,
            "model_variant": model_variant(session.model),
            "python": session.python,
            "device": options.device,
            "compute_type": options.compute_type,
            "beam_size": options.beam_size,
            "language": options.language,
            "vad": options.vad,
            "hotwords": list(options.hotwords),
            "initial_prompt": options.initial_prompt,
            "max_duration_ms": options.max_duration_ms,
            "max_chars": options.max_chars,
            "pause_ms": options.pause_ms,
            "min_duration_ms": options.min_duration_ms,
        },
        "summary": {
            "language": payload.get("language"),
            "language_probability": probability,
            "duration_s": round(session.duration_s, 2),
            "speech_s": round(session.speech_s, 2),
            "words": len(session.words),
            "raw_segments": len(session.raw_segments),
            "cues": len(session.cues),
            "avg_cue_s": (
                round(session.speech_s / len(session.cues), 2) if session.cues else 0.0
            ),
            "rtf": round(session.duration_s / session.wall, 2) if session.wall > 0 else None,
            "wall_s": round(session.wall, 1),
        },
        "outputs": [output.to_dict() for output in session.outputs],
        "warnings": session.warnings,
    }
    report_path = session.out_dir / "asr.json"
    atomic.write_json(report_path, report)
    if not options.keep_raw:
        session.raw_path.unlink(missing_ok=True)

    return AsrResult(
        status=session.status,
        exit_code=EXIT_OK if session.cues else EXIT_INVALID,
        report=report,
        report_path=report_path.resolve(),
        srt_path=session.srt_path.resolve() if session.cues else None,
        words_path=session.words_path.resolve(),
    )
