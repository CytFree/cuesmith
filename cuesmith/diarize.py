"""多人声（说话人分离）编排：模型只跑在进程边界，产物给配音层分派音色。

收费配音软件（剪映"多人配音"、Descript、阿里云智能配音）都有这个能力：一条片子里
有两个以上说话人时，自动识别"谁在什么时候说话"，再给每个说话人分配不同音色，
否则全片一个声音读所有人，访谈/对话类素材根本不能用。

**不自建分离模型**（与 ASR/MT/TTS/BGM 同一口径），只管编排：

- ``sherpa-onnx`` 离线说话人分离（pyannote segmentation-3.0 + 3D-Speaker/NeMo 声纹，
  ONNX / CPU）：纯 CPU 可跑，模型合计约 82MB，只装在 ``spike/diarization/.venv``；
- 模型只通过子进程调用（:mod:`cuesmith.diarize_worker`），主包保持"标准库 + ffmpeg"。

产出两份东西：

1. ``diarize.json``：分段清单 + 各说话人的条数/时长 + 报告（可复跑、可追责）；
2. ``speakers.json``：**字幕条 → 说话人**的映射，配音层按它给每条字幕选音色。

一个关键工程细节：字幕条是句子级的（merge 之后），和分离模型输出的"连续说话段"
不是一一对应。这里用**时间重叠投票**归属，再做一次"孤岛平滑"——某一条字幕的说话人
和前后都不同、且前后一致时，判定为模型抖动，回填邻居，避免同一个人中途换音色。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, atomic, media
from .interpreters import (
    Requirement,
    ordered_candidates,
    python_in_venv,
    resolve,
    runner_probe,
)
from .report import OutputFile, file_digest, section
from .srt import load_srt
from .worker_protocol import run_once, subprocess_runner

__all__ = [
    "DEFAULT_EMBEDDING_MODELS",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_VAD_THRESHOLD",
    "SEGMENTS_NAME",
    "VAD_MODEL",
    "ClusterMerge",
    "DiarizeError",
    "DiarizeOptions",
    "DiarizeResult",
    "SpeakerTurn",
    "assign_speakers",
    "attach_speakers",
    "build_speaker_voice_map",
    "default_threshold",
    "find_diarization_python",
    "find_models",
    "find_vad_model",
    "load_segments",
    "merge_minor_speakers",
    "parse_speaker_voices",
    "run_diarize",
    "smooth_speakers",
    "write_segments",
]

EXIT_OK = 0
EXIT_INVALID = 2

SAMPLE_RATE = 16000  # pyannote segmentation-3.0 要求 16k 单声道
# 中英文各一个声纹模型：中文用 3D-Speaker（eres2net），英文用 NeMo（titanet-small，更快）
DEFAULT_EMBEDDING_MODELS = {
    "zh": "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
    "en": "nemo_en_titanet_small.onnx",
}
SEGMENTATION_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
MAX_SPEAKERS = 8  # 超过这个数基本是聚类散了，多音色没有意义
# 分段产物（OPT-5）：与 ASR 并行时先落这个，ASR 完成后只做"字幕→说话人"归属（毫秒级）
SEGMENTS_NAME = "segments.json"

# 各语言的默认聚类阈值。**两个声纹模型的簇距离分布不一样**：
# - 中文 3D-Speaker（eres2net）在真实素材上 0.8 就能稳定分出 3–4 人；
# - 英文 NeMo titanet-small 偏"碎"：同一条 10min 播客 auto 0.8 → 15 人、0.95 → 10 人、
#   0.99 → 8 人（实测 2026-11-02），0.8 会把笑声/广告人声拆成独立簇。
# 所以英文默认 0.95；显式 --threshold / --diarization-threshold 仍优先。
DEFAULT_THRESHOLDS = {"zh": 0.8, "en": 0.95}
DEFAULT_THRESHOLD = 0.8
# 短簇合并阈值：簇总语音不足 3s（或占比不足 1%）视为聚类碎片，并入相邻主簇。
# 实测英文 10min 播客 8 个簇里 5 个语音 0.56–2.31s，两个主簇 330s/256s。
MIN_SPEAKER_SPEECH_S = 3.0
MIN_SPEAKER_SHARE = 0.01

# VAD 预过滤（OPT-6）：silero VAD 先切出语音区间，非语音置零后再做分离。
# 音乐/掌声里的"人声"原本会被分割模型当成说话人，聚出没有台词归属的伪簇。
VAD_MODEL = "silero_vad.onnx"
VAD_MODEL_DIR = "vad"
DEFAULT_VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_S = 0.25
VAD_MIN_SPEECH_S = 0.25
VAD_MAX_SPEECH_S = 30.0

# 声纹 embedding 二次聚类（OPT-7）：自动判人数时在 sherpa 原始簇上做"只并拆"的
# 二次聚类（turn 级 average-linkage，阈值 τ = alpha × 同人相似度参考 s_same）。
# 显式 --speakers 已经准确，不需要二次聚类（也会尊重用户指定的人数）。
CLUSTERING_MODES = ("auto", "sherpa", "embedding")
# 合并阈值系数 τ = alpha × 同人相似度参考 s_same。**两种输入模式要两个 alpha**：
# - 原始音频（未拼语音）：0.55（6 个真实样本校准，报告 work/opt7-exp/REPORT.md）；
# - VAD 拼接音频（compact）：拼接让 turn 变长/变杂，跨簇平均成对相似度整体抬高
#   10–50%，同样的 alpha 会过并（3p→2、press→2），实测 0.70 才准。
DEFAULT_MERGE_ALPHA_NORMAL = 0.55
DEFAULT_MERGE_ALPHA_COMPACT = 0.70

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Extractor = Callable[[Path, Path], None]


def default_threshold(language: str | None) -> float:
    """该语言的默认聚类阈值（未知语言退回 0.8）。"""

    return DEFAULT_THRESHOLDS.get((language or "").lower(), DEFAULT_THRESHOLD)


class DiarizeError(RuntimeError):
    """环境不可用 / 模型缺失 / 分离失败。"""


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker: str

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class DiarizeOptions:
    src: Path  # 音/视频（内部抽 16k 单声道）
    out: Path
    srt: Path | None = None  # 有字幕时顺带做"字幕条 → 说话人"归属
    language: str | None = None  # zh/en：决定默认声纹模型
    python: str | None = None
    segmentation_model: str | Path | None = None
    embedding_model: str | Path | None = None
    num_speakers: int = 0  # 0 = 自动聚类
    # None = 按语言取默认（见 DEFAULT_THRESHOLDS）；显式值优先。
    # 越大说话人越少（0.8 偏保守：宁可少分，避免给同一个人多分音色）
    threshold: float | None = None
    min_duration_on: float = 0.3
    min_duration_off: float = 0.5
    # 短簇合并：簇总语音 < 该值（秒）或占比 < min_speaker_share 时并入相邻主簇；
    # 0 = 关闭合并（--keep-minor-speakers，保留每一个原始簇）
    min_speaker_speech_s: float = MIN_SPEAKER_SPEECH_S
    min_speaker_share: float = MIN_SPEAKER_SHARE
    # VAD 预过滤（OPT-6）：默认开启，模型不在时自动跳过（只是优化，不是必需件）；
    # 显式 vad_model 时模型缺失直接报错，避免"以为开了其实没开"。
    vad: bool = True
    vad_model: str | Path | None = None
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    # 二次聚类（OPT-7）：auto = 自动判人数时用声纹 embedding 只并拆，显式人数时用 sherpa
    clustering: str = "auto"
    # None = 按 VAD 模式取默认（原始音频 0.55 / 拼接音频 0.70，见上面常量说明）
    merge_alpha: float | None = None
    num_threads: int = 0
    keep_raw: bool = False
    generated_at: datetime | None = None
    runner: Runner | None = None  # 测试注入
    extractor: Extractor | None = None  # 测试注入

    def validate(self) -> None:
        if self.num_speakers < 0:
            raise DiarizeError("说话人数不能为负（0 = 自动判断）")
        if self.threshold is not None and not 0.0 <= self.threshold <= 1.0:
            raise DiarizeError("聚类阈值必须在 0–1 之间")
        if self.min_duration_on <= 0 or self.min_duration_off < 0:
            raise DiarizeError("分段时长参数不合法（min_duration_on > 0，min_duration_off ≥ 0）")
        if self.min_speaker_speech_s < 0 or not 0.0 <= self.min_speaker_share <= 1.0:
            raise DiarizeError("短簇合并参数不合法（min_speaker_speech_s ≥ 0，min_speaker_share 0–1）")
        if not 0.0 < self.vad_threshold < 1.0:
            raise DiarizeError("VAD 阈值必须在 0–1 之间（越大越严格）")
        if self.clustering not in CLUSTERING_MODES:
            raise DiarizeError(f"聚类方式只能是：{' / '.join(CLUSTERING_MODES)}")
        if self.merge_alpha is not None and not 0.0 < self.merge_alpha <= 1.0:
            raise DiarizeError("二次聚类阈值系数 merge_alpha 必须在 (0, 1] 之间")

    @property
    def resolved_threshold(self) -> float:
        """实际生效的聚类阈值（未显式给出时按语言取默认）。"""

        return self.threshold if self.threshold is not None else default_threshold(self.language)

    @property
    def resolved_clustering(self) -> str:
        """实际生效的聚类方式：auto 时"自动判人数"才值得做二次聚类。"""

        if self.clustering != "auto":
            return self.clustering
        return "embedding" if self.num_speakers <= 0 else "sherpa"

    def merge_alpha_for(self, vad_enabled: bool) -> float:
        """实际生效的合并阈值系数：显式优先，否则按 VAD 输入模式取默认。"""

        if self.merge_alpha is not None:
            return float(self.merge_alpha)
        return DEFAULT_MERGE_ALPHA_COMPACT if vad_enabled else DEFAULT_MERGE_ALPHA_NORMAL


@dataclass(frozen=True, slots=True)
class DiarizeResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    speakers_path: Path | None
    segments: tuple[SpeakerTurn, ...]


def _as_int(value: object) -> int:
    """把 JSON 里的数字收窄成 int（非数字抛 TypeError，交给调用方的 except 处理）。"""

    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"不是数字：{value!r}")


# ---------------------------------------------------------------------- 环境


_default_runner = subprocess_runner(7200)  # 长视频 + 声纹二次聚类可能很久


def _resolve_threads(num_threads: int) -> int:
    """0 = 自动：CPU 核数的一半（上限 8）。实测 4 线程比单线程快一倍多。"""

    if num_threads > 0:
        return num_threads
    return max(1, min(8, (os.cpu_count() or 2) // 2))


_DIARIZE_PYTHON = Requirement(
    label="sherpa-onnx",
    module="sherpa_onnx",
    parameter="--diarize-python",
    env_var="CUESMITH_DIARIZATION_PYTHON",
    hint="先建隔离环境：python -m venv spike/diarization/.venv + "
    "pip install sherpa-onnx numpy==1.26.4。",
)


def _search_pythons() -> list[str]:
    """仓库里值得一看的解释器（显式参数与环境变量由 resolve 统一排在最前面）。"""

    project = Path(__file__).resolve().parent.parent
    return ordered_candidates(
        python_in_venv(project / "spike" / "diarization" / ".venv"),
        (sys.executable,),
    )


def find_diarization_python(
    explicit: str | None = None, *, runner: Runner | None = None
) -> str:
    """找一个真的能 ``import sherpa_onnx`` 的解释器（策略见 interpreters）。"""

    run = runner or _default_runner
    found = resolve(
        _DIARIZE_PYTHON,
        explicit,
        candidates=_search_pythons(),
        probe=runner_probe(run, _DIARIZE_PYTHON.module),
        error_cls=DiarizeError,
    )
    if found is None:  # 契约上走不到：on_missing=raise 要么返回解释器、要么已抛错
        raise DiarizeError("内部错误：解释器探测既没返回结果也没报错")
    return found


def _models_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "spike" / "diarization" / "models"


def _onnx_files() -> list[Path]:
    root = _models_dir()
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.onnx") if path.is_file())


def _is_segmentation(path: Path) -> bool:
    name = str(path).lower()  # 目录名也算：sherpa-onnx-pyannote-segmentation-3-0/model.onnx
    return "segmentation" in name or "pyannote" in name or "reverb" in name


def _is_vad(path: Path) -> bool:
    """VAD 模型不是声纹模型：不能让它被 find_models 当成 embedding 候选。"""

    return "vad" in path.name.lower()


def find_vad_model(explicit: str | Path | None = None) -> Path | None:
    """定位 silero VAD 模型（OPT-6）；没装就返回 None（VAD 只是优化，不是必需件）。

    优先级：显式参数（不存在则报错）> ``CUESMITH_DIARIZATION_VAD``（存在才用）
    > 项目内 ``spike/diarization/models/vad/silero_vad.onnx``。
    """

    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise DiarizeError(f"VAD 模型文件不存在：{path}")
        return path
    env = os.environ.get("CUESMITH_DIARIZATION_VAD")
    if env:
        path = Path(env)
        if path.is_file():
            return path
    default = _models_dir() / VAD_MODEL_DIR / VAD_MODEL
    return default if default.is_file() else None


def find_models(
    *,
    segmentation: str | Path | None = None,
    embedding: str | Path | None = None,
    language: str | None = None,
) -> tuple[Path, Path]:
    """定位分割模型 + 声纹模型；显式参数 > 环境变量 > 项目内默认目录。"""

    candidates = _onnx_files()
    segmentation_path = _first_model(
        segmentation or os.environ.get("CUESMITH_DIARIZATION_SEGMENTATION"),
        candidates,
        want_segmentation=True,
    )
    if segmentation_path is None:
        raise DiarizeError(
            "没找到说话人分割模型（sherpa-onnx-pyannote-segmentation-3-0/model.onnx）。"
            f"把它放到 {_models_dir()} 下，或用 --segmentation-model 指定。"
        )

    preferred = DEFAULT_EMBEDDING_MODELS.get((language or "").lower())
    ordered: list[Path | str | None] = [
        embedding,
        os.environ.get("CUESMITH_DIARIZATION_EMBEDDING"),
    ]
    if preferred:
        ordered.append(_models_dir() / preferred)
    embedding_path = _first_model(
        next((item for item in ordered if item), None), candidates, want_segmentation=False
    )
    if embedding_path is None:
        raise DiarizeError(
            "没找到说话人声纹模型（3dspeaker / nemo 的 .onnx）。"
            f"放到 {_models_dir()} 下，或用 --embedding-model 指定。"
        )
    return segmentation_path, embedding_path


def _first_model(
    explicit: str | Path | None, candidates: Sequence[Path], *, want_segmentation: bool
) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise DiarizeError(f"模型文件不存在：{path}")
        return path
    for candidate in candidates:
        if _is_vad(candidate):
            continue
        if _is_segmentation(candidate) == want_segmentation:
            return candidate
    return None


# ---------------------------------------------------------------------- 归属


def _overlap_ms(start: int, end: int, turn: SpeakerTurn) -> int:
    return max(0, min(end, turn.end_ms) - max(start, turn.start_ms))


def _nearest_speaker(start: int, end: int, turns: Sequence[SpeakerTurn]) -> str | None:
    if not turns:
        return None
    midpoint = (start + end) // 2
    best: SpeakerTurn | None = None
    best_distance = float("inf")
    for turn in turns:
        if turn.start_ms <= midpoint <= turn.end_ms:
            distance = 0
        else:
            distance = min(abs(midpoint - turn.start_ms), abs(midpoint - turn.end_ms))
        if distance < best_distance:
            best, best_distance = turn, distance
    return best.speaker if best is not None else None


def assign_speakers(
    timings: Sequence[tuple[int, int]], turns: Sequence[SpeakerTurn]
) -> list[str]:
    """把每条字幕归到"重叠时间最长"的说话人。

    没有重叠时用中点最近的段兜底；完全无法判定（没有分段）返回空串。
    平票时优先沿用上一条的说话人，减少边界抖动导致的换声。
    """

    if not turns:
        return ["" for _ in timings]

    order: list[str] = []
    for turn in turns:
        if turn.speaker not in order:
            order.append(turn.speaker)

    result: list[str] = []
    previous = ""
    for start, end in timings:
        overlaps: dict[str, int] = {}
        for turn in turns:
            value = _overlap_ms(start, end, turn)
            if value > 0:
                overlaps[turn.speaker] = overlaps.get(turn.speaker, 0) + value
        if overlaps:
            best = max(
                overlaps.items(),
                key=lambda item: (item[1], item[0] == previous, -order.index(item[0])),
            )
            speaker = best[0]
        else:
            speaker = _nearest_speaker(start, end, turns) or ""
        result.append(speaker)
        previous = speaker
    return result


def smooth_speakers(
    speakers: Sequence[str],
    timings: Sequence[tuple[int, int]] | None = None,
    *,
    max_island_ms: int = 2500,
) -> list[str]:
    """孤岛平滑：前后说话人一致、自己不同且时长很短的条，回填成邻居。

    真实素材里模型抖动常表现为"一句 1 秒的插话被判给另一个人"，回填比保留更稳。
    """

    smoothed = list(speakers)
    if len(smoothed) < 3:
        return smoothed
    for index in range(1, len(smoothed) - 1):
        if smoothed[index] == smoothed[index - 1] or smoothed[index - 1] != smoothed[index + 1]:
            continue
        duration = 0
        if timings is not None:
            duration = max(0, timings[index][1] - timings[index][0])
        if timings is None or duration <= max_island_ms:
            smoothed[index] = smoothed[index - 1]
    return smoothed


@dataclass(frozen=True, slots=True)
class ClusterMerge:
    """短簇合并的结果：合并后的段、被并掉的簇明细、合并前后的说话人清单。"""

    turns: tuple[SpeakerTurn, ...]
    merged: tuple[dict[str, object], ...]
    speakers_before: tuple[str, ...]
    speakers_after: tuple[str, ...]
    skipped: str | None = None  # disabled | single-main（合并后只剩 1 人，宁多不并）

    @property
    def changed(self) -> bool:
        return bool(self.merged)


def _nearest_main_speaker(
    index: int, turns: Sequence[SpeakerTurn], main: set[str]
) -> str | None:
    """离第 ``index`` 段最近的主簇说话人（按时间间隙，不按序号）。"""

    turn = turns[index]
    previous: SpeakerTurn | None = None
    for candidate in reversed(turns[:index]):
        if candidate.speaker in main:
            previous = candidate
            break
    following: SpeakerTurn | None = None
    for candidate in turns[index + 1 :]:
        if candidate.speaker in main:
            following = candidate
            break
    if previous is None:
        return following.speaker if following is not None else None
    if following is None:
        return previous.speaker
    gap_before = turn.start_ms - previous.end_ms  # 可能为负（时间重叠）
    gap_after = following.start_ms - turn.end_ms
    return previous.speaker if gap_before <= gap_after else following.speaker


def merge_minor_speakers(
    turns: Sequence[SpeakerTurn],
    *,
    min_speech_s: float = MIN_SPEAKER_SPEECH_S,
    min_share: float = MIN_SPEAKER_SHARE,
) -> ClusterMerge:
    """把"总语音太短/占比太小"的簇并入时间上相邻的主簇（聚类碎片清理）。

    背景：英文 NeMo 声纹在真实播客上会把笑声、广告人声、音乐人声拆成独立簇——
    实测一条 10min 播客自动聚出 15 人，其中 5 个簇语音 <3s（0.56–2.31s）。
    这类簇没有任何配音意义（分不到足够台词），却会把音色池耗光、让报告失真。

    规则：
    - 主簇 = 语音 ≥ ``min_speech_s`` 且 占比 ≥ ``min_share`` 的簇；
    - 短簇并入"时间上最近的主簇"（按每条分段投票，多段取多数）；
    - **宁多不并**：如果合并后只剩 1 个说话人，保留原结果（那可能是真的一人分饰/停顿被拆）。

    返回 :class:`ClusterMerge`；``min_speech_s<=0`` 时只做占比判定，两者都为 0 视为关闭。
    """

    speaker_order = _speaker_order(turns)
    unchanged = ClusterMerge(
        turns=tuple(turns),
        merged=(),
        speakers_before=tuple(speaker_order),
        speakers_after=tuple(speaker_order),
    )
    if not turns or len(speaker_order) < 2:
        return unchanged
    if min_speech_s <= 0 and min_share <= 0:
        return ClusterMerge(
            turns=tuple(turns),
            merged=(),
            speakers_before=tuple(speaker_order),
            speakers_after=tuple(speaker_order),
            skipped="disabled",
        )

    speech_ms: dict[str, int] = {speaker: 0 for speaker in speaker_order}
    for turn in turns:
        speech_ms[turn.speaker] += turn.duration_ms
    total_ms = sum(speech_ms.values())
    if total_ms <= 0:
        return unchanged

    main = {
        speaker
        for speaker in speaker_order
        if speech_ms[speaker] >= round(min_speech_s * 1000)
        and speech_ms[speaker] / total_ms >= min_share
    }
    if not main or len(main) == len(speaker_order):
        return unchanged
    if len(main) == 1:
        return ClusterMerge(
            turns=tuple(turns),
            merged=(),
            speakers_before=tuple(speaker_order),
            speakers_after=tuple(speaker_order),
            skipped="single-main",
        )

    # 逐段投票：每个短簇归到它出现次数最多的邻居主簇
    votes: dict[str, dict[str, int]] = {}
    for index, turn in enumerate(turns):
        if turn.speaker in main:
            continue
        target = _nearest_main_speaker(index, turns, main)
        if target is None:  # 理论不可达：main 非空
            continue
        votes.setdefault(turn.speaker, {})
        votes[turn.speaker][target] = votes[turn.speaker].get(target, 0) + 1
    target_of = {
        speaker: max(counter.items(), key=lambda item: (item[1], -speaker_order.index(item[0])))[0]
        for speaker, counter in votes.items()
    }

    # 合并后按出场顺序重新编号（不留 SPEAKER_03 空洞，音色池也按顺序分配）
    relabel: dict[str, str] = {}
    merged_turns: list[SpeakerTurn] = []
    for turn in turns:
        speaker = target_of.get(turn.speaker, turn.speaker)
        if speaker not in relabel:
            relabel[speaker] = f"SPEAKER_{len(relabel):02d}"
        merged_turns.append(
            SpeakerTurn(start_ms=turn.start_ms, end_ms=turn.end_ms, speaker=relabel[speaker])
        )

    merged: list[dict[str, object]] = []
    for speaker in speaker_order:
        if speaker in main:
            continue
        target = target_of.get(speaker)
        if target is None:
            continue  # 没有邻居可并：原样保留（不会出现在这里，保险）
        merged.append(
            {
                "speaker": speaker,
                "into": relabel[target],
                "speech_s": round(speech_ms[speaker] / 1000, 2),
                "share_pct": round(speech_ms[speaker] / total_ms * 100, 2),
                "turns": sum(1 for turn in turns if turn.speaker == speaker),
            }
        )
    speakers_after: list[str] = []
    for turn in merged_turns:
        if turn.speaker not in speakers_after:
            speakers_after.append(turn.speaker)
    if len(speakers_after) < 2:
        # 宁多不并：合并把所有人都并进了一个簇，说明"短"其实是素材本身
        return ClusterMerge(
            turns=tuple(turns),
            merged=(),
            speakers_before=tuple(speaker_order),
            speakers_after=tuple(speaker_order),
            skipped="single-main",
        )
    return ClusterMerge(
        turns=tuple(merged_turns),
        merged=tuple(merged),
        speakers_before=tuple(speaker_order),
        speakers_after=tuple(speakers_after),
    )


# ---------------------------------------------------------------------- 音色


def parse_speaker_voices(
    spec: str | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """解析 ``--speaker-voices``。

    两种写法（不能混用）：

    - 显式映射：``SPEAKER_00=zh-CN-YunxiNeural,SPEAKER_01=zh-CN-XiaoxiaoNeural``
    - 音色池：``zh-CN-YunxiNeural,zh-CN-XiaoxiaoNeural``（按说话人出场的先后分配）

    返回 ``(显式映射, 音色池)``。
    """

    if not spec:
        return {}, ()
    items = [item.strip() for item in spec.split(",") if item.strip()]
    if not items:
        return {}, ()
    explicit = [item for item in items if "=" in item]
    if explicit and len(explicit) != len(items):
        raise DiarizeError(
            "--speaker-voices 的两种写法不能混用：要么全部 SPEAKER_00=音色，要么全部只写音色"
        )
    if explicit:
        mapping: dict[str, str] = {}
        for item in explicit:
            speaker, _, voice = item.partition("=")
            speaker, voice = speaker.strip(), voice.strip()
            if not speaker or not voice:
                raise DiarizeError(f"--speaker-voices 条目格式不对：{item!r}")
            mapping[speaker] = voice
        return mapping, ()
    return {}, tuple(items)


def build_speaker_voice_map(
    speakers: Sequence[str],
    *,
    explicit: dict[str, str] | None = None,
    pool: Sequence[str] = (),
    default: str = "",
) -> dict[str, str]:
    """按说话人出场顺序分配音色：显式映射优先，其余从音色池轮转，最后落到默认音色。"""

    mapping: dict[str, str] = dict(explicit or {})
    index = 0
    for speaker in speakers:
        if speaker in mapping:
            continue
        if pool:
            mapping[speaker] = pool[index % len(pool)]
            index += 1
        else:
            mapping[speaker] = default
    return mapping


# ---------------------------------------------------------------------- 主流程


def _extract_audio(source: Path, out_wav: Path) -> None:
    """抽 16k / 16bit / 单声道 WAV（分离模型的要求）。"""

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    media.run_ffmpeg(
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        ],
        error_cls=DiarizeError,
        what="抽取音轨",
    )


def _wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return round(handle.getnframes() * 1000 / handle.getframerate())


def _normalize_turns(
    raw_segments: Sequence[dict[str, object]],
) -> tuple[list[SpeakerTurn], dict[int, str]]:
    """把 worker 的整数 speaker 归一成 ``SPEAKER_00``（按出场先后），时间转 ms。"""

    ordered = sorted(
        raw_segments,
        key=lambda item: (float(item.get("start") or 0.0), float(item.get("end") or 0.0)),
    )
    mapping: dict[int, str] = {}
    turns: list[SpeakerTurn] = []
    for item in ordered:
        try:
            start_s = float(item["start"])
            end_s = float(item["end"])
            speaker_id = int(item["speaker"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_s < start_s:
            start_s, end_s = end_s, start_s
        if speaker_id not in mapping:
            mapping[speaker_id] = f"SPEAKER_{len(mapping):02d}"
        turns.append(
            SpeakerTurn(
                start_ms=round(start_s * 1000),
                end_ms=round(max(end_s, start_s) * 1000),
                speaker=mapping[speaker_id],
            )
        )
    return turns, mapping


def _speaker_order(turns: Sequence[SpeakerTurn]) -> list[str]:
    order: list[str] = []
    for turn in turns:
        if turn.speaker not in order:
            order.append(turn.speaker)
    return order


def _speaker_stats(
    turns: Sequence[SpeakerTurn], assignments: Sequence[str], texts: Sequence[str]
) -> list[dict[str, object]]:
    stats: dict[str, dict[str, object]] = {}
    for turn in turns:
        entry = stats.setdefault(
            turn.speaker, {"speaker": turn.speaker, "turns": 0, "speech_s": 0.0, "cues": 0, "chars": 0}
        )
        entry["turns"] = int(entry["turns"]) + 1
        entry["speech_s"] = round(float(entry["speech_s"]) + turn.duration_ms / 1000, 2)
    for speaker, text in zip(assignments, texts):
        if not speaker:
            continue
        entry = stats.setdefault(
            speaker, {"speaker": speaker, "turns": 0, "speech_s": 0.0, "cues": 0, "chars": 0}
        )
        entry["cues"] = int(entry["cues"]) + 1
        entry["chars"] = int(entry["chars"]) + len(text)
    return sorted(stats.values(), key=lambda item: str(item["speaker"]))


# ------------------------------------------------------------------ 分段 / 归属（OPT-5）


def write_segments(
    path: Path,
    *,
    turns: Sequence[SpeakerTurn],
    source_path: Path,
    duration_s: float,
    config: dict[str, object],
    summary: dict[str, object],
    warnings: Sequence[str] = (),
) -> Path:
    """落一份"只有说话人分段、没有字幕归属"的产物，供 ASR 并行/后续复用。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    size, digest = file_digest(Path(source_path))
    payload = {
        "schema_version": 1,
        "tool": "cuesmith",
        "version": __version__,
        "source": {
            "path": str(Path(source_path).resolve()),
            "bytes": size,
            "sha256": digest,
        },
        "duration_s": round(float(duration_s), 2),
        "config": dict(config),
        "summary": dict(summary),
        "turns": [
            {"start_ms": turn.start_ms, "end_ms": turn.end_ms, "speaker": turn.speaker}
            for turn in turns
        ],
        "warnings": list(warnings),
    }
    atomic.write_json(path, payload)
    return path


def load_segments(path: Path) -> tuple[list[SpeakerTurn], dict[str, object]]:
    """读回 :func:`write_segments` 的产物（找不到/坏了当空）。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    if not isinstance(payload, dict):
        return [], {}
    turns: list[SpeakerTurn] = []
    for item in payload.get("turns") or []:
        try:
            turns.append(
                SpeakerTurn(
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    speaker=str(item["speaker"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return turns, payload


def attach_speakers(
    turns: Sequence[SpeakerTurn],
    srt: Path,
    out_dir: Path,
    *,
    report: dict[str, object] | None = None,
    generated_at: datetime | None = None,
) -> tuple[Path, dict[str, object]]:
    """把「字幕条 → 说话人」补到**已经跑好的分段**上（ASR 并行路线的第二阶段）。

    只做时间重叠投票 + 孤岛平滑 + 写 ``speakers.json`` / 更新 ``diarize.json``，
    不碰模型，毫秒级。这样 diarize 的分段（几十秒）可以与 ASR 并行跑，
    等 ASR 出字幕后再补归属——长视频能省掉 min(ASR, diarize) 的墙钟时间。
    """

    subtitle, _encoding = load_srt(Path(srt))
    timings = [(cue.timing.start_ms, cue.timing.end_ms) for cue in subtitle.cues]
    texts = [cue.text for cue in subtitle.cues]
    assignments = smooth_speakers(assign_speakers(timings, turns), timings)
    speakers = _speaker_order(turns)
    missed = sum(1 for speaker in assignments if not speaker)

    warnings = [str(item) for item in (report or {}).get("warnings") or []]
    if missed:
        warnings.append(f"有 {missed} 条字幕没有可归属的说话人分段（已按空值兜底）")
    warnings = list(dict.fromkeys(warnings))

    cues_payload = [
        {"cue": index, "start_ms": start, "end_ms": end, "speaker": speaker}
        for index, ((start, end), speaker) in enumerate(zip(timings, assignments), start=1)
    ]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    speakers_path = out_dir / "speakers.json"
    srt_path = Path(srt)
    srt_size, srt_sha = file_digest(srt_path)
    atomic.write_json(speakers_path, {
                "schema_version": 1,
                "tool": "cuesmith",
                "version": __version__,
                "srt": str(srt_path.resolve()),
                "speakers": speakers,
                "cues": cues_payload,
            })

    merged = dict(report or {})
    merged["schema_version"] = 1
    merged["tool"] = "cuesmith"
    merged["version"] = __version__
    merged["command"] = "diarize"
    merged["generated_at"] = (generated_at or datetime.now().astimezone()).isoformat()
    merged["status"] = "ok" if turns else "empty"
    inputs = dict(merged.get("input") or {})
    inputs["srt"] = {
        "path": str(srt_path.resolve()),
        "bytes": srt_size,
        "sha256": srt_sha,
        "cues": len(assignments),
    }
    merged["input"] = inputs
    summary = dict(merged.get("summary") or {})
    summary.setdefault("speakers", len(speakers))
    summary["turns"] = len(turns)
    summary["cues"] = len(assignments)
    summary["assigned_cues"] = sum(1 for speaker in assignments if speaker)
    summary["speaker_stats"] = _speaker_stats(turns, assignments, texts)
    merged["summary"] = summary
    merged["segments"] = [
        {"start_ms": turn.start_ms, "end_ms": turn.end_ms, "speaker": turn.speaker}
        for turn in turns
    ]
    merged["cues"] = cues_payload
    outputs = [
        item
        for item in (merged.get("outputs") or [])
        if isinstance(item, dict) and item.get("kind") != "speaker_assignments"
    ]
    outputs.append(
        OutputFile(
            kind="speaker_assignments",
            path=speakers_path.resolve(),
            cues=len(cues_payload),
            size=speakers_path.stat().st_size,
        ).to_dict()
    )
    merged["outputs"] = outputs
    merged["warnings"] = warnings
    report_path = out_dir / "diarize.json"
    atomic.write_json(report_path, merged)
    return speakers_path.resolve(), merged


def run_diarize(options: DiarizeOptions) -> DiarizeResult:
    """把音频/视频分出说话人分段；给了字幕就顺带做"字幕条 → 说话人"归属。

    五个阶段各自独立可读：准备 → 跑 worker → 归簇 → 收告警 → 归属与落盘。
    阶段之间只通过 :class:`_DiarizeSession` 交接。
    """

    session = _DiarizeSession.start(options)
    _run_worker(session)
    _cluster_turns(session)
    _collect_warnings(session)
    _assign_speakers(session)
    return _write_outputs(session)


@dataclass(slots=True)
class _DiarizeSession:
    """一次说话人分离的现场：环境、worker 结果、逐阶段结论。

    以前这些全是 327 行 ``run_diarize`` 的局部变量 —— 从"找解释器"到"写
    speakers.json"共用同一个命名空间，改告警措辞要先分清哪些变量是过程、哪些只是
    报告字段。收成记录之后，每个阶段只读写自己那几个字段。
    """

    options: DiarizeOptions
    source: Path
    srt: Path | None
    run: Runner
    python: str
    segmentation: Path
    embedding: Path
    threshold: float
    vad_path: Path | None
    vad_skipped: str | None
    effective_alpha: float
    out_dir: Path
    audio_path: Path
    raw_path: Path
    started: float
    duration_ms: int
    payload: dict[str, object] = field(default_factory=dict)
    turns: list[SpeakerTurn] = field(default_factory=list)
    vad_summary: dict[str, object] | None = None
    clustering_summary: dict[str, object] | None = None
    clusters: ClusterMerge | None = None
    speakers: list[str] = field(default_factory=list)
    wall: float = 0.0
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"
    assignments: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    cues_payload: list[dict[str, object]] | None = None
    speakers_path: Path | None = None
    outputs: list[OutputFile] = field(default_factory=list)

    @classmethod
    def start(cls, options: DiarizeOptions) -> _DiarizeSession:
        """校验参数、定位解释器与模型、抽音频 —— 开工前的一次性准备。"""

        options.validate()
        source = Path(options.src)
        if not source.is_file():
            raise DiarizeError(f"输入文件不存在：{source}")
        srt = Path(options.srt) if options.srt is not None else None
        if srt is not None and not srt.is_file():
            raise DiarizeError(f"字幕文件不存在：{srt}")

        run = options.runner or _default_runner
        python = find_diarization_python(options.python, runner=run)
        segmentation, embedding = find_models(
            segmentation=options.segmentation_model,
            embedding=options.embedding_model,
            language=options.language,
        )
        threshold = options.resolved_threshold

        # OPT-6：VAD 预过滤。显式给了 --vad-model 就必须存在（否则报错）；
        # 只是默认开启但本机没装模型时安静跳过，报告里写清 skipped 原因。
        vad_path: Path | None = None
        vad_skipped: str | None = None
        if options.vad_model is not None:
            vad_path = find_vad_model(options.vad_model)
        elif options.vad:
            vad_path = find_vad_model(None)
            if vad_path is None:
                vad_skipped = "model-missing"
        effective_alpha = options.merge_alpha_for(vad_path is not None)

        out_dir = Path(options.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        audio_path = out_dir / "source.wav"
        raw_path = out_dir / "diarization-worker.json"

        started = time.monotonic()
        extractor = options.extractor or _extract_audio
        extractor(source, audio_path)
        duration_ms = _wav_duration_ms(audio_path)

        return cls(
            options=options,
            source=source,
            srt=srt,
            run=run,
            python=python,
            segmentation=segmentation,
            embedding=embedding,
            threshold=threshold,
            vad_path=vad_path,
            vad_skipped=vad_skipped,
            effective_alpha=effective_alpha,
            out_dir=out_dir,
            audio_path=audio_path,
            raw_path=raw_path,
            started=started,
            duration_ms=duration_ms,
        )


def _worker_command(session: _DiarizeSession) -> list[str]:
    """派给 worker 的完整命令行（参数口径就是 diarize_worker 的 CLI 契约）。"""

    options = session.options
    command = [
        session.python,
        str(Path(__file__).with_name("diarize_worker.py")),
        "--audio",
        str(session.audio_path),
        "--segmentation",
        str(session.segmentation),
        "--embedding",
        str(session.embedding),
        "--output",
        str(session.raw_path),
        "--num-speakers",
        str(options.num_speakers if options.num_speakers > 0 else -1),
        "--threshold",
        f"{session.threshold:g}",
        "--min-duration-on",
        f"{options.min_duration_on:g}",
        "--min-duration-off",
        f"{options.min_duration_off:g}",
        "--num-threads",
        str(_resolve_threads(options.num_threads)),
    ]
    if session.vad_path is not None:
        command.extend(
            [
                "--vad-model",
                str(session.vad_path),
                "--vad-threshold",
                f"{options.vad_threshold:g}",
                "--vad-min-silence",
                f"{VAD_MIN_SILENCE_S:g}",
                "--vad-min-speech",
                f"{VAD_MIN_SPEECH_S:g}",
                "--vad-max-speech",
                f"{VAD_MAX_SPEECH_S:g}",
            ]
        )
    command.extend(
        [
            "--clustering",
            options.resolved_clustering,
            "--merge-alpha",
            f"{session.effective_alpha:g}",
            "--merge-min-speech",
            f"{options.min_speaker_speech_s:g}",
            "--merge-min-share",
            f"{options.min_speaker_share:g}",
        ]
    )
    return command


def _run_worker(session: _DiarizeSession) -> None:
    """跑一次 worker（独立解释器 + 子进程），拿回原始分段与两段诊断摘要。"""

    session.payload = run_once(
        _worker_command(session),
        session.raw_path,
        error_cls=DiarizeError,
        runner=session.run,
        label="说话人分离",
    )


def _cluster_turns(session: _DiarizeSession) -> None:
    """把 worker 的整数说话人归一，再把短簇并回主簇（避免"15 人"式假人数）。"""

    options = session.options
    turns, _id_map = _normalize_turns(session.payload.get("segments") or [])
    raw_vad = session.payload.get("vad")
    session.vad_summary = dict(raw_vad) if isinstance(raw_vad, dict) else None
    raw_clustering = session.payload.get("clustering")
    session.clustering_summary = (
        dict(raw_clustering) if isinstance(raw_clustering, dict) else None
    )
    # 聚类碎片清理：短簇（<3s 或 <1%）并入相邻主簇
    session.clusters = merge_minor_speakers(
        turns,
        min_speech_s=options.min_speaker_speech_s,
        min_share=options.min_speaker_share,
    )
    session.turns = list(session.clusters.turns)
    session.speakers = _speaker_order(session.turns)
    session.wall = time.monotonic() - session.started


def _clusters_of(session: _DiarizeSession) -> ClusterMerge:
    """取聚类结果；没跑过聚类说明内部顺序错了，按不变量违反报出来。"""

    if session.clusters is None:
        raise DiarizeError("内部错误：聚类结果缺失")
    return session.clusters


def _collect_warnings(session: _DiarizeSession) -> None:
    """把"这次分离有什么需要用户注意的"汇总成告警，并定 status。"""

    options = session.options
    clusters = _clusters_of(session)

    if session.clustering_summary is not None:
        merged_in = session.clustering_summary.get("clusters_in")
        merged_out = session.clustering_summary.get("clusters_out")
        try:
            if _as_int(merged_in) > _as_int(merged_out):
                session.warnings.append(
                    f"按声纹相似度把短簇/同人簇并回主簇：{merged_in} → {merged_out} 人"
                    f"（阈值 τ={session.clustering_summary.get('tau')}，"
                    f"同人相似度参考 {session.clustering_summary.get('s_same')}）"
                )
        except (TypeError, ValueError):
            pass
    if session.vad_summary is not None and session.vad_summary.get("skipped") == "no-speech":
        session.warnings.append(
            "VAD 没有检测到语音（整段像音乐/静音），已按原音频跑分离，结果为 0 人属正常"
        )
    if not session.turns:
        session.warnings.append("没有识别出任何说话人分段（可能是纯音乐/静音素材）")
        session.status = "empty"
    else:
        session.status = "ok"
    if clusters.merged:
        detail = "、".join(
            f"{item['speaker']}({item['speech_s']}s)→{item['into']}" for item in clusters.merged
        )
        session.warnings.append(
            f"已把 {len(clusters.merged)} 个短簇（语音 <{options.min_speaker_speech_s:g}s "
            f"或占比 <{options.min_speaker_share * 100:g}%）并入相邻主簇："
            f"{len(clusters.speakers_before)} 人 → {len(clusters.speakers_after)} 人（{detail}）"
        )
    elif clusters.skipped == "single-main":
        session.warnings.append(
            f"有 {len(clusters.speakers_before)} 个簇但只有 1 个达到主簇门槛；"
            "合并会只剩 1 个说话人，已按「宁多不并」保留原结果（可用 --speakers 显式指定人数）"
        )
    if len(session.speakers) == 1:
        session.warnings.append("只识别出 1 个说话人（单人口播素材）：多音色配音不会生效")
    if options.num_speakers and len(session.speakers) != options.num_speakers:
        session.warnings.append(
            f"指定了 {options.num_speakers} 个说话人，实际分离出 {len(session.speakers)} 个"
            "（素材本身人数不符或阈值不合适）"
        )
    if len(session.speakers) > MAX_SPEAKERS:
        session.warnings.append(
            f"分离出 {len(session.speakers)} 个说话人，超过工程上限 {MAX_SPEAKERS}，"
            "建议指定 --speakers"
        )


def _assign_speakers(session: _DiarizeSession) -> None:
    """把每条字幕归到说话人（按时间重叠 + 平滑），写 ``speakers.json``。"""

    if session.srt is None:
        return
    subtitle, _encoding = load_srt(session.srt)
    timings = [(cue.timing.start_ms, cue.timing.end_ms) for cue in subtitle.cues]
    session.texts = [cue.text for cue in subtitle.cues]
    session.assignments = smooth_speakers(assign_speakers(timings, session.turns), timings)
    missed = sum(1 for speaker in session.assignments if not speaker)
    if missed:
        session.warnings.append(f"有 {missed} 条字幕没有可归属的说话人分段（已按空值兜底）")
    session.cues_payload = [
        {
            "cue": index,
            "start_ms": timing[0],
            "end_ms": timing[1],
            "speaker": speaker,
        }
        for index, (timing, speaker) in enumerate(zip(timings, session.assignments), start=1)
    ]
    session.speakers_path = session.out_dir / "speakers.json"
    atomic.write_json(session.speakers_path, {
                "schema_version": 1,
                "tool": "cuesmith",
                "version": __version__,
                "srt": str(session.srt.resolve()),
                "speakers": session.speakers,
                "cues": session.cues_payload,
            })
    session.outputs.append(
        OutputFile(
            kind="speaker_assignments",
            path=session.speakers_path.resolve(),
            cues=len(session.cues_payload),
            size=session.speakers_path.stat().st_size,
        )
    )


def _write_outputs(session: _DiarizeSession) -> DiarizeResult:
    """写 ``diarize.json`` 与 ``segments.json``，清掉中间产物，返回命令行契约。"""

    options = session.options
    clusters = _clusters_of(session)
    speech_ms = sum(turn.duration_ms for turn in session.turns)
    source_size, source_sha = file_digest(session.source)
    input_srt: dict[str, object] | None = None
    if session.srt is not None:
        srt_size, srt_sha = file_digest(session.srt)
        input_srt = {
            "path": str(session.srt.resolve()),
            "bytes": srt_size,
            "sha256": srt_sha,
            "cues": len(session.assignments),
        }

    generated_at = options.generated_at or datetime.now().astimezone()
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "cuesmith",
        "version": __version__,
        "command": "diarize",
        "generated_at": generated_at.isoformat(),
        "status": session.status,
        "input": {
            "path": str(session.source.resolve()),
            "bytes": source_size,
            "sha256": source_sha,
            "duration_s": round(session.duration_ms / 1000, 2),
            "srt": input_srt,
        },
        "config": {
            "python": session.python,
            "segmentation_model": str(session.segmentation.resolve()),
            "embedding_model": str(session.embedding.resolve()),
            "language": options.language,
            "num_speakers_requested": options.num_speakers or None,
            "threshold": session.threshold,
            "threshold_source": (
                "explicit" if options.threshold is not None else "language-default"
            ),
            "min_speaker_speech_s": options.min_speaker_speech_s,
            "min_speaker_share": options.min_speaker_share,
            "min_duration_on": options.min_duration_on,
            "min_duration_off": options.min_duration_off,
            "num_threads": _resolve_threads(options.num_threads),
            "sample_rate": SAMPLE_RATE,
            # VAD 预过滤（OPT-6）：requested 是用户意图，enabled 是实际是否用了模型
            "vad": {
                "requested": bool(options.vad or options.vad_model is not None),
                "enabled": session.vad_path is not None,
                "model": str(session.vad_path.resolve()) if session.vad_path is not None else None,
                "threshold": options.vad_threshold,
                "skipped": session.vad_skipped,
                # compact = 只把语音段拼起来送模型，再把时间轴映射回原视频
                "mode": "compact" if session.vad_path is not None else None,
            },
            # 二次聚类（OPT-7）
            "clustering": options.clustering,
            "clustering_effective": options.resolved_clustering,
            "merge_alpha": session.effective_alpha,
            "merge_alpha_source": (
                "explicit"
                if options.merge_alpha is not None
                else ("vad-default" if session.vad_path is not None else "default")
            ),
        },
        "summary": {
            "speakers": len(session.speakers),
            "speakers_raw": len(clusters.speakers_before),
            "merged_clusters": list(clusters.merged),
            "cluster_merge_skipped": clusters.skipped,
            "vad": session.vad_summary,
            "clustering": session.clustering_summary,
            "turns": len(session.turns),
            "speech_s": round(speech_ms / 1000, 2),
            "coverage_pct": (
                round(speech_ms / session.duration_ms * 100, 1) if session.duration_ms else 0.0
            ),
            "cues": len(session.assignments) if session.srt is not None else None,
            "assigned_cues": (
                sum(1 for speaker in session.assignments if speaker)
                if session.srt is not None
                else None
            ),
            "speaker_stats": _speaker_stats(session.turns, session.assignments, session.texts),
            "rtf": (
                round(session.duration_ms / 1000 / session.wall, 3) if session.wall > 0 else None
            ),
            "wall_s": round(session.wall, 1),
        },
        "segments": [
            {"start_ms": turn.start_ms, "end_ms": turn.end_ms, "speaker": turn.speaker}
            for turn in session.turns
        ],
        "cues": session.cues_payload,
        "outputs": [output.to_dict() for output in session.outputs],
        "warnings": session.warnings,
    }
    report_path = session.out_dir / "diarize.json"
    atomic.write_json(report_path, report)
    # 分段单独落一份（OPT-5）：ASR 在跑的时候可以先算分段，之后只补字幕归属；
    # 也方便下次换字幕/重跑 ASR 时复用分段（分段只跟音频和聚类参数有关）。
    summary_block = section(report, "summary")
    write_segments(
        session.out_dir / SEGMENTS_NAME,
        turns=session.turns,
        source_path=session.source,
        duration_s=session.duration_ms / 1000,
        config=section(report, "config"),
        summary={
            "speakers": summary_block.get("speakers"),
            "speakers_raw": summary_block.get("speakers_raw"),
            "merged_clusters": summary_block.get("merged_clusters"),
            "vad": summary_block.get("vad"),
            "clustering": summary_block.get("clustering"),
            "turns": summary_block.get("turns"),
            "speech_s": summary_block.get("speech_s"),
            "rtf": summary_block.get("rtf"),
            "wall_s": summary_block.get("wall_s"),
        },
        warnings=session.warnings,
    )
    if not options.keep_raw:
        session.raw_path.unlink(missing_ok=True)

    return DiarizeResult(
        status=session.status,
        exit_code=EXIT_OK if session.status == "ok" else EXIT_INVALID,
        report=report,
        report_path=report_path.resolve(),
        speakers_path=session.speakers_path.resolve() if session.speakers_path is not None else None,
        segments=tuple(session.turns),
    )
