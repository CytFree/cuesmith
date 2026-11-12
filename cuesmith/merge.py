"""FR-3 前置：ASR 句级合并。

动机（M0 实测）：Whisper 产出 2–3 秒的碎片 cue，直接拿去翻译+配音，译文时长必然撑爆槽位：
不合并时 10/10 条溢出、最大 +66%；按句合并后降到 5/6、+22%，端到端 80s → 52s。

规则（PRD §3 FR-3 前置）：
- 按句末标点切句（。！？.!?…）；英文句点后紧跟数字/字母不算句末（3.5、U.S. 不切）
- 单条时长上限 12 秒
- 相邻碎片间隔 ≤ 1 秒才合并

**只合并不拆分**：一条 ASR cue 里的时间轴是"这段语音真实占用的时间"，把它按句子拆开只能靠猜
（按字数比例分配），实测在英文播客上把 27 条拆成 48 条，导致 42/48 条配音需要人工重写——
槽位被切碎后再准确的 TTS 也装不下。多句的 ASR 段落保持整条，字幕显示为多行即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__, atomic
from .report import file_digest
from .srt import Cue, Subtitle, Timing, dump_srt, load_srt

__all__ = [
    "CONTINUATION_WORDS",
    "MergeError",
    "MergeOptions",
    "MergeResult",
    "join_texts",
    "merge_cues",
    "merge_cues_with_speakers",
    "run_merge",
]

SENTENCE_END = set("。！？.!?…")

# 英文里"这句还没说完"的常见起头词（ASR 段尾被加了句号时用来兜底判续句）
CONTINUATION_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "because", "but", "by", "can", "could", "do", "does",
        "did", "for", "from", "had", "has", "have", "he", "if", "in", "is", "it", "like",
        "not", "of", "on", "or", "she", "so", "than", "that", "the", "then", "there",
        "these", "they", "this", "those", "to", "was", "we", "were", "when", "which",
        "while", "who", "will", "with", "would", "you",
    }
)


def _looks_like_continuation(previous: str, following: str) -> bool:
    """上一条以 ASCII 句号收尾，但下一条其实是同一句的后半截。

    这是 ASR 的典型产物：whisper 按静音切段，段尾不一定真是句子边界；如果上游又给每段
    补了句号（pyVideoTrans 的 ``--fix_punc`` 就会这么干），断句和翻译都会被带歪。
    """

    prev = previous.rstrip()
    if not prev or not prev.endswith(".") or prev.endswith(".."):
        return False
    nxt = following.lstrip()
    if not nxt:
        return False
    first = nxt[0]
    if first.isascii() and first.islower():
        return True
    head = re.split(r"[\s,.;:!?…]+", nxt, maxsplit=1)[0].lower()
    return head in CONTINUATION_WORDS


def _flatten(text: str) -> str:
    """whisper 的换行是按字符宽度折的、不是语义换行，合并前必须拍平。"""

    return " ".join(text.split())


def _drop_false_period(text: str) -> str:
    """把"其实是半句"的那个句号去掉，直接续上（交给翻译当一整句处理）。"""

    return text[:-1].rstrip() if text.endswith(".") and not text.endswith("..") else text


@dataclass(frozen=True, slots=True)
class MergeOptions:
    max_duration_ms: int = 12000
    max_gap_ms: int = 1000
    # 多人声（M11）：与输入 cue 一一对应的说话人；非空时**不同说话人不合并**。
    # 否则两个人的台词会被并成一条字幕、再被同一个音色读完（实测 CCTV 之外的
    # 对话素材一开始就是这样翻车的）。
    speakers: tuple[str, ...] = ()
    speakers_path: Path | None = None  # 报告里记 sha256 用（缓存判据）


def join_texts(left: str, right: str) -> str:
    """拼接相邻片段：后一片段以拉丁字母/数字开头时补空格，中文之间不补。"""

    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace():
        return left + right
    first = right[0]
    if first.isascii() and first.isalnum():
        return f"{left} {right}"
    return left + right


def _ends_sentence(text: str) -> bool:
    if not text:
        return False
    if text[-1] not in SENTENCE_END:
        return False
    # 句号后没内容了才算句末；"3.5" 这种数字场景靠上游切分规避
    return True


class MergeError(RuntimeError):
    """合并不可继续（字幕读不出来 / 没有可用 cue）。"""


@dataclass(frozen=True, slots=True)
class MergeResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    merged_path: Path
    cues_before: int
    cues_after: int


def merge_cues(subtitle: Subtitle, options: MergeOptions | None = None) -> Subtitle:
    """把碎片 cue 合并成句子级 cue；时间轴取首条开始～末条结束。"""

    merged, _speakers = merge_cues_with_speakers(subtitle, options)
    return merged


def merge_cues_with_speakers(
    subtitle: Subtitle, options: MergeOptions | None = None
) -> tuple[Subtitle, tuple[str, ...]]:
    """合并并保留每条合并结果的说话人（没给说话人时就是空串列表）。"""

    settings = options or MergeOptions()
    speakers = settings.speakers
    if speakers and len(speakers) != len(subtitle.cues):
        raise MergeError(
            f"说话人数量和字幕条数对不上（{len(speakers)} vs {len(subtitle.cues)}）"
        )
    merged: list[Cue] = []
    merged_speakers: list[str] = []
    items = [
        (cue, speakers[index] if speakers else "")
        for index, cue in enumerate(subtitle.cues)
        if cue.text.strip()
    ]

    buffer_text = ""
    buffer_start: int | None = None
    buffer_end = 0
    buffer_speaker = ""

    def flush() -> None:
        nonlocal buffer_text, buffer_start, buffer_end, buffer_speaker
        if buffer_text and buffer_start is not None:
            merged.append(
                Cue(timing=Timing(buffer_start, max(buffer_end, buffer_start)), text=buffer_text)
            )
            merged_speakers.append(buffer_speaker)
        buffer_text = ""
        buffer_start = None
        buffer_end = 0
        buffer_speaker = ""

    for index, (cue, speaker) in enumerate(items):
        text = _flatten(cue.text)
        if not text:
            continue

        if buffer_start is not None:
            too_long = cue.timing.end_ms - buffer_start > settings.max_duration_ms
            too_far = cue.timing.start_ms - buffer_end > settings.max_gap_ms
            changed_speaker = speaker != buffer_speaker
            if too_long or too_far or changed_speaker:
                flush()

        if buffer_start is None:
            buffer_start = cue.timing.start_ms
            buffer_speaker = speaker
        buffer_text = join_texts(buffer_text, text)
        buffer_end = max(buffer_end, cue.timing.end_ms)

        if _ends_sentence(text):
            following = items[index + 1] if index + 1 < len(items) else None
            continues = (
                following is not None
                and following[1] == speaker
                and following[0].timing.start_ms - cue.timing.end_ms <= settings.max_gap_ms
                and _looks_like_continuation(text, following[0].text)
            )
            if continues:
                # 假句号：去掉后继续攒同一句
                buffer_text = _drop_false_period(buffer_text)
            else:
                flush()

    flush()
    return Subtitle(cues=tuple(merged)), tuple(merged_speakers)


def run_merge(
    src: Path,
    out: Path,
    options: MergeOptions | None = None,
    *,
    generated_at: datetime | None = None,
) -> MergeResult:
    """读 ASR 字幕 → 句级合并 → 写 ``merged.srt`` + ``merge.json``。

    ``merge.json`` 的 ``input.sha256`` 是缓存判据：上游 ASR 字幕被人工校对过之后，
    pipeline 能据此判断缓存已过期，自动重算而不是拿旧结果往下游走。
    """

    src_path = Path(src)
    if not src_path.is_file():
        raise MergeError(f"字幕文件不存在：{src_path}")
    try:
        subtitle, encoding = load_srt(src_path)
    except (OSError, ValueError) as exc:
        raise MergeError(f"字幕读取失败（{src_path.name}）：{exc}") from exc
    if len(subtitle) == 0:
        raise MergeError("输入字幕里没有可用 cue")

    settings = options or MergeOptions()
    merged, merged_speakers = merge_cues_with_speakers(subtitle, settings)

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_path = out_dir / "merged.srt"
    merged_path.write_text(dump_srt(merged), encoding="utf-8", newline="\n")

    size, digest = file_digest(src_path)
    speakers_meta: dict[str, object] | None = None
    if settings.speakers and settings.speakers_path is not None:
        speaker_size, speaker_digest = file_digest(Path(settings.speakers_path))
        speakers_meta = {
            "path": str(Path(settings.speakers_path).resolve()),
            "bytes": speaker_size,
            "sha256": speaker_digest,
        }
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "cuesmith",
        "version": __version__,
        "command": "merge",
        "generated_at": (generated_at or datetime.now().astimezone()).isoformat(),
        "status": "ok",
        "input": {
            "path": str(src_path.resolve()),
            "encoding": encoding,
            "cues": len(subtitle),
            "bytes": size,
            "sha256": digest,
            "speakers": speakers_meta,
        },
        "config": {
            "max_duration_ms": settings.max_duration_ms,
            "max_gap_ms": settings.max_gap_ms,
            "speaker_aware": bool(settings.speakers),
        },
        "summary": {
            "before": len(subtitle),
            "after": len(merged),
            "merged_ratio": round(len(merged) / len(subtitle), 3),
            "merged_away": len(subtitle) - len(merged),
            "speakers": len({speaker for speaker in merged_speakers if speaker})
            if settings.speakers
            else None,
        },
        "speaker_map": [
            {"cue": index, "speaker": speaker}
            for index, speaker in enumerate(merged_speakers, start=1)
        ]
        if settings.speakers
        else [],
        "outputs": [{"kind": "merged_srt", "path": str(merged_path.resolve())}],
    }
    report_path = out_dir / "merge.json"
    atomic.write_json(report_path, report)

    return MergeResult(
        status="ok",
        exit_code=0,
        report=report,
        report_path=report_path.resolve(),
        merged_path=merged_path.resolve(),
        cues_before=len(subtitle),
        cues_after=len(merged),
    )
