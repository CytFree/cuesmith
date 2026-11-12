"""pipeline：一条命令跑完整链路（probe → asr → diarize → merge → translate → build → dub → burn → mark）。

收费软件的"一键成片"就是这条路径：丢进视频，出来「发布字幕 + 带硬字幕的配音成片 + 合规标识」。
本模块只做**编排与缓存**，每一步仍调用各自模块（probe/asr/merge/translate/matrix/dub/burn/mark），
每步产物落盘，任意一步失败都能单独重跑，不必从头再来。

设计要点：

- **流程表说了算**：阶段顺序只写在 `_STEPS`（`_Step(阶段名, 函数)`）这一处，每个阶段
  一个 `_stage_*` 函数，自负三件事：判缓存（`_xxx_cache_hit`）→ 跑生产函数 → 写阶段条目。
  不在阶段清单里的衔接动作也是 `_Step`，但 `stage=None`（合并后重建说话人映射、翻译前
  过滤噪声 cue）——它们占流程位置，但不受 `--stop-after` 控制。
- **阶段缓存**：默认复用 `out/<stage>/` 里已存在的产物（`--force` 强制重跑）。
  改字幕重跑时，前面已完成的重活（ASR/翻译）不会白跑；缓存还要校验上游文件 sha256，
  **人工校对过的字幕会让下游阶段自动失效重跑**，而不是拿旧结果硬走。
- **自动分流**：先 `probe`。有文本字幕流 → 直出 `source.srt`（不跑 ASR）；
  图形字幕 → 明确报错；无字幕 → 交给 `asr`（语言 `auto` 时用识别结果决定翻译语言）。
- **ASR 后合并**：ASR 为了显示按 7 秒切条，天然产出半句碎片（"为了生存，它们展开" /
  "一场盐的争夺战。"），直接翻译会出现英文半句。ASR 路线默认先 `merge` 成句子级
  cue（上限 15s = 两个 7s 碎片；内封字幕是人工断句，默认跳过；`--no-merge` 可关）。
  合并只影响翻译/配音的句子单位，发布字幕仍由排版层按宽度/CPS 切成多条。
- **中文默认 medium**：`src-lang` 是中文时 ASR 默认用 medium（本地没装就退回 small），
  这是 M7 实测的结论（small 会把"刘翔"识别成"流向"）。
- **多人声**：`--diarize` / `--speaker-voices` 开启后，先做说话人分离（sherpa-onnx，模型只跑在
  进程边界），再**按说话人分段合并**（不同人的相邻台词不并成一条），最后按 `speakers.merged.json`
  给每条字幕分派不同音色。默认关闭，单人口播行为与 M10 完全一致。
- **两条时间轴**：配音用句子级译文（`translate/<tgt>.srt`），发布字幕用排版产物
  （`matrix/<tgt>.srt` + 双语 srt/ass/vtt），两者解耦。
- **成片带字幕**：默认把发布字幕烧进成片（`burn` 阶段，target=只烧译文 / bilingual=双语），
  再打 AIGC 标识；`--no-burn-subtitles` 则只出外挂字幕。烧录后自动核验像素差。
- **失败可诊断**：`pipeline.json` 记录每个阶段的状态、耗时、产物路径、错误摘要；
  退出码 0=全部成功、2=部分失败（配音失败/标识核验不过等）、1=参数/输入错误。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sized
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import atomic
from . import __version__
from .asr import AsrError, AsrOptions, model_variant, preferred_model, run_asr
from .burn import BurnError, BurnOptions, run_burn
from .clean import cleanup_speakers, cleanup_srt
from .cost import CostError, build_cost, load_pricing
from .diarize import (
    CLUSTERING_MODES,
    DEFAULT_MERGE_ALPHA_COMPACT,
    DEFAULT_MERGE_ALPHA_NORMAL,
    DEFAULT_VAD_THRESHOLD,
    MIN_SPEAKER_SPEECH_S,
    SEGMENTS_NAME,
    DiarizeError,
    DiarizeOptions,
    SpeakerTurn,
    assign_speakers,
    attach_speakers,
    build_speaker_voice_map,
    default_threshold,
    load_segments,
    parse_speaker_voices,
    run_diarize,
    smooth_speakers,
)
from .dub import DubError, DubOptions, run_dub
from .glossary import Glossary, glossary_fingerprint
from .mark import MarkError, MarkOptions, run_mark
from .tts import speech_capacity_cps
from .matrix import MatrixOptions, run_matrix
from .merge import MergeError, MergeOptions, run_merge
from .probe import ProbeError, ProbeOptions, run_probe
from .report import file_digest, section
from .srt import load_srt
from .translate import (
    LlmConfig,
    TranslateError,
    TranslateOptions,
    run_translate,
)
from .validate import LANG_PROFILES, Limits

__all__ = [
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_USAGE",
    "STAGES",
    "PipelineError",
    "PipelineOptions",
    "PipelineResult",
    "run_pipeline",
]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PARTIAL = 2
STAGES = ("probe", "asr", "diarize", "merge", "translate", "build", "dub", "burn", "mark")
DEFAULT_VOICES = {"zh": "zh-CN-XiaoxiaoNeural", "en": "en-US-AriaNeural"}
# 多人声默认音色池（按说话人出场顺序分配）：第一个和单音色默认值不同，保证"一听就是两个人"
DEFAULT_SPEAKER_VOICES = {
    # 3–4 人只用这份默认池时，每人一个不同音色；超过 4 人会复用并告警。
    # en 池原来第 4 个是 en-US-DavisNeural——微软已下线（真实调用 NoAudioReceived），
    # M16 真实测试换成在线的 en-US-AndrewNeural（男声）。
    "zh": ("zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural", "zh-CN-YunjianNeural", "zh-CN-XiaoyiNeural"),
    "en": ("en-US-GuyNeural", "en-US-AriaNeural", "en-US-JennyNeural", "en-US-AndrewNeural"),
}
# 中文 TTS 相对偏慢、英文偏快：这两个默认值来自真实素材实测（M5/M7）
DEFAULT_TTS_RATE = {"zh": "+30%", "en": "+20%"}


class PipelineError(RuntimeError):
    """编排层不可继续（输入/参数/前置阶段失败）。"""


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    """一次 pipeline 的全部参数（CLI 与 GUI 共用）。

    **新增一个参数要动的地方**（漏一处就是"参数写了但没生效"，历史上踩过）：
    ① ``cli._add_*_args`` 的参数表；② 这里的字段（同名即可，``_pipeline_options`` 用
    ``**vars(args)`` 透传，不用再手抄一行）；③ ``_report_config``（``pipeline.json``
    的审计字段）；④ 若该参数属于某阶段，还要转到对应 ``XOptions``（如 dub 的 30 个）；
    ⑤ GUI 表单（``gui.py``，可选）。
    """

    src: Path
    out: Path
    src_lang: str = "auto"  # auto = 用 ASR 识别结果（内封字幕路线要求显式）
    tgt_lang: str = "zh"
    provider: str | None = None  # 默认 microsoft；llm=True 时 openai
    llm: bool = False
    llm_review: bool = True  # 仅 llm=True 时生效
    glossary: Glossary | None = None
    condense: bool = True
    condense_mode: str = "auto"
    # 渠道回退（OPT-3）：auto = 免费 MT 网络失败后切备用渠道（已配置的 LLM / 命令模板）
    translate_fallback: str = "auto"
    # 翻译分块并发（OPT-5）：免费 MT 保守 4（太快会被限流），LLM 可调到 6–8
    translate_concurrency: int = 4
    # ASR
    asr_python: str | None = None
    asr_model: str | None = None
    hotwords: tuple[str, ...] = ()
    initial_prompt: str | None = None
    # ASR 之后、翻译之前的句级合并（内封字幕路线自动跳过）
    # 单条上限 15s 而不是 merge 模块默认的 12s：ASR 自己按"显示 7s"切条，
    # 两个相邻 7s 碎片合起来就有 14s；上限设 12 会导致"半句"永远合不回去（CCTV 实测）。
    # 发布字幕不会因此变长——排版层会按宽度/CPS 再切成多条显示。
    merge: bool = True
    merge_max_duration: float = 15.0
    merge_max_gap: float = 1.0
    # 多人声（M11）：说话人分离 + 按说话人分派音色。默认关闭（单人口播不需要多花这份时间），
    # 给了 --speaker-voices / --speakers 就自动开启。
    diarize: bool = False
    speakers: int = 0  # 0 = 自动聚类
    # None = 按源语言取默认（中文 0.8 / 英文 0.95，见 diarize.DEFAULT_THRESHOLDS）
    diarization_threshold: float | None = None
    # 保留每一个原始簇（关闭短簇合并）：真实短片段的发言人（如 8s 广告人声）不会被并掉
    keep_minor_speakers: bool = False
    diarize_python: str | None = None
    segmentation_model: str | None = None
    embedding_model: str | None = None
    # VAD 预过滤（OPT-6）：音乐/掌声/环境声段不进聚类，减少伪说话人
    vad: bool = True
    vad_model: str | None = None
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    # 二次聚类（OPT-7）：auto = 自动判人数时用声纹 embedding 只并拆；显式人数用 sherpa
    clustering: str = "auto"
    # None = 按 VAD 输入模式取默认（原始音频 0.55 / 拼接音频 0.70）
    merge_alpha: float | None = None
    speaker_voices: str | None = None  # "SPEAKER_00=音色,..." 或 "音色1,音色2"
    # 排版/配音/标识
    wrap: bool = True
    voice: str | None = None
    tts_rate: str | None = None
    tts_python: str | None = None
    tts_command: str | None = None
    # 本地 TTS（OPT-11）：默认仍用 Edge；--tts-provider local 可离线；
    # --tts-fallback local 让 Edge 失败（网络/端点）的条目用本地 TTS 兜底。
    tts_provider: str = "edge"
    tts_fallback: str = "local"
    local_tts_model: str | None = None
    local_tts_kind: str = "auto"
    local_tts_voice: str = ""
    local_tts_python: str | None = None
    local_tts_threads: int = 8
    local_tts_speed: float | None = None
    regenerate_tts: bool = False  # 忽略 TTS 内容指纹缓存（云端 TTS 会重新计费）
    concurrency: int = 6
    tempo_limit: float = 1.4
    adaptive_rate: bool = True  # 放不下的条目提语速重合成（--no-adaptive-rate 关）
    keep_bgm: bool = False
    bgm_mode: str = "auto"
    bgm_source: Path | None = None
    separator_python: str | None = None
    # 成片硬字幕（收费软件默认给"带字幕的成片"）：target=只烧译文 / bilingual=中英双语
    burn_subtitles: bool = True
    burn_mode: str = "target"
    burn_crf: int = 18
    burn_position: str = "bottom"  # bottom | top（源片自带硬字幕时用 top 避让）
    mark: bool = True
    force: bool = False
    stop_after: str | None = None
    pricing: str | None = None  # 成本价表 JSON（默认用 submatrix/cost.py 内置示例价）
    llm_config: LlmConfig | None = None
    generated_at: datetime | None = None

    def validate(self) -> None:
        if self.src_lang != "auto" and self.src_lang not in LANG_PROFILES:
            raise PipelineError(f"不支持的源语言：{self.src_lang}")
        if self.tgt_lang not in LANG_PROFILES:
            raise PipelineError(f"不支持的目标语言：{self.tgt_lang}")
        if self.src_lang == self.tgt_lang and self.src_lang != "auto":
            raise PipelineError("源语言和目标语言相同，没有翻译的必要")
        if self.speakers < 0:
            raise PipelineError("--speakers 不能为负（0 = 自动判断）")
        if self.diarization_threshold is not None and not 0.0 <= self.diarization_threshold <= 1.0:
            raise PipelineError("--diarization-threshold 必须在 0–1 之间")
        if not 0.0 < self.vad_threshold < 1.0:
            raise PipelineError("--vad-threshold 必须在 0–1 之间（越大越严格）")
        if self.clustering not in CLUSTERING_MODES:
            raise PipelineError(f"--clustering 只能是：{' / '.join(CLUSTERING_MODES)}")
        if self.merge_alpha is not None and not 0.0 < self.merge_alpha <= 1.0:
            raise PipelineError("--merge-alpha 必须在 (0, 1] 之间")
        if self.tts_provider not in {"edge", "local"}:
            raise PipelineError("--tts-provider 只能是 edge（默认）或 local")
        if self.tts_fallback not in {"local", "off"}:
            raise PipelineError("--tts-fallback 只能是 local（默认）或 off")
        if self.tts_provider == "local" and self.tts_command:
            raise PipelineError("--tts-provider local 与 --tts-command 不能同时使用")
        if self.local_tts_kind not in {"auto", "kokoro", "vits"}:
            raise PipelineError("--local-tts-kind 只能是 auto / kokoro / vits")
        if self.local_tts_threads < 1:
            raise PipelineError("--local-tts-threads 必须 ≥ 1")
        if self.local_tts_speed is not None and not 0.5 <= self.local_tts_speed <= 2.0:
            raise PipelineError("--local-tts-speed 必须在 0.5–2.0 之间")
        try:
            parse_speaker_voices(self.speaker_voices)
        except DiarizeError as exc:
            raise PipelineError(str(exc)) from exc
        if self.burn_mode not in {"target", "bilingual"}:
            raise PipelineError("--burn-mode 只能是 target（只烧译文）或 bilingual（中英双语）")
        if not 0 <= self.burn_crf <= 51:
            raise PipelineError("--burn-crf 必须在 0–51 之间")
        if self.burn_position not in {"bottom", "top"}:
            raise PipelineError("--burn-position 只能是 bottom（底部）或 top（顶部）")
        if self.stop_after is not None and self.stop_after not in STAGES:
            raise PipelineError(f"--stop-after 只能是：{' / '.join(STAGES)}")
        if self.translate_fallback not in {"auto", "off"}:
            raise PipelineError("--translate-fallback 只能是 auto（失败自动换渠道）或 off（只重试）")
        if self.translate_concurrency < 1:
            raise PipelineError("翻译并发数必须 ≥ 1")


@dataclass(frozen=True, slots=True)
class PipelineResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    outputs: dict[str, str]


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str:
    try:
        return file_digest(Path(path))[1]
    except OSError:
        return ""


def _asr_model_matches(report: dict[str, object], options: PipelineOptions) -> bool:
    """ASR 模型是否和旧报告一致；不一致就不能复用旧 ASR。

    两种情况都要管：显式 ``--asr-model`` 变了（旧报告可能是 small），以及**没显式指定**
    时默认模型的归属又取决于源语言和"本机装了哪个模型"——把 ``--asr-model medium`` 去掉
    重跑，默认会回落到 small/medium 中的另一个，旧报告不能原样当缓存。
    """

    config = report.get("config") or {}
    if not isinstance(config, dict):
        return False
    current = str(config.get("model_variant") or "")

    if options.asr_model:
        return bool(current) and current == model_variant(str(options.asr_model))

    # 没显式指定：默认模型按源语言挑（auto 时用旧报告识别出的语言反推"当时该用哪个"）
    language = options.src_lang
    if language == "auto":
        language = str(section(report, "summary").get("language") or "")
    if not language:
        return True
    expected = model_variant(preferred_model(language) or "small")
    return bool(current) and current == expected


def _report_sha_matches(
    report: dict[str, object], path: Path, *keys: str
) -> bool:
    """报告里记录的输入 sha256 是否等于当前文件——缓存判据，防止拿旧产物硬走。

    典型场景：ASR 跑完 → 人工校对 ``asr/source.srt`` → 重跑。此时 translate/merge
    的产物虽然存在，但它们的输入已经变了，必须重算（否则字幕永远停在旧译文）。
    """

    node: object = report
    for key in keys:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    current = _sha256(path)
    return bool(current) and isinstance(node, str) and node == current


def _config_matches(
    report: dict[str, object],
    expected: Mapping[str, object],
    *,
    tolerance: float = 0.0,
) -> bool:
    """报告里记录的配置是否与本次请求一致；不一致就**不能复用**旧产物。

    阶段缓存的第一原则：**已知会改变产物的参数都必须进判据**。以前每个阶段各写一套
    比较函数，漏掉一个字段就是"改了参数却拿到旧产物"（换视频复用上一条视频的 ASR、
    换术语表复用旧译文，都是真实踩过的坑）。这里把规则收成声明式：判据就是
    「报告里这些键要等于这些值」。

    - 嵌套 dict 递归比较，两侧都必须是 dict；
    - 数字按 ``tolerance`` 容差比较（``None`` 只与 ``None`` 相等，NaN 永不相等）；
    - 序列按**无序**比较，但元素类型也要一致（``18`` 不等于 ``"18"``）；
    - **报告里缺字段＝不一致**：老版本报告没有该字段时宁可重跑一次，
      也不能拿旧产物冒充新结果。
    """

    return _subset_matches(report, expected, tolerance=tolerance)


def _subset_matches(
    recorded: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    tolerance: float,
) -> bool:
    """``expected`` 里的每一项都要在 ``recorded`` 里出现且相等（缺字段算不相等）。"""

    for key, want in expected.items():
        if key not in recorded:
            return False
        got = recorded[key]
        if isinstance(want, Mapping):
            if not isinstance(got, Mapping) or not _subset_matches(
                got, want, tolerance=tolerance
            ):
                return False
        elif want is None:
            if got is not None:
                return False
        elif isinstance(want, bool):
            if not isinstance(got, bool) or got is not want:
                return False
        elif isinstance(want, (int, float)):
            # bool 是 int 的子类，上面已单独处理；这里只接真数字
            if isinstance(got, bool) or not isinstance(got, (int, float)):
                return False
            if math.isnan(float(want)) or math.isnan(float(got)):
                return False
            if abs(float(got) - float(want)) > max(tolerance, 1e-9):
                return False
        elif isinstance(want, (list, tuple)):
            if not isinstance(got, (list, tuple)):
                return False
            # 无序比较，但用 repr 保留类型：18 与 "18" 不该算相等
            if sorted(repr(item) for item in got) != sorted(repr(item) for item in want):
                return False
        elif isinstance(want, str):
            if not isinstance(got, str) or got != want:
                return False
        elif got != want:
            return False
    return True


def _translate_config_matches(
    report: dict[str, object],
    options: PipelineOptions,
    *,
    src_lang: str,
    provider: str,
) -> bool:
    """translate 缓存判据：凡是会改变译文的参数变了都必须重跑。

    踩过的坑：以前只比输入 sha + 精简预算，于是换 ``--glossary`` / ``--provider`` /
    ``--llm-model`` 仍然复用旧译文——用户以为术语表生效了，其实一个词都没换。
    """

    llm_expected: dict[str, object] = {
        "review": options.llm_review if provider == "openai" else False
    }
    if provider == "openai":
        # 换模型/换端点会换译文。没配 key 时不比：离线状态下复用已有译文是合理的，
        # 不该因为环境里缺 key 就把翻好的结果当垃圾。
        config = options.llm_config or LlmConfig.from_env()
        if config.configured:
            llm_expected["model"] = config.model
            llm_expected["base_url"] = config.base_url

    expected: dict[str, object] = {
        # 记的是"主渠道"（provider_primary），不是回退后实际干活的那个
        "provider_primary": provider,
        "src_lang": src_lang,
        "tgt_lang": options.tgt_lang,
        "glossary": {"fingerprint": glossary_fingerprint(options.glossary)},
        "condense": {
            "enabled": options.condense,
            "mode": options.condense_mode if options.condense else None,
            # 配音槽位容量：``--tts-rate`` 调低后预算收紧，旧的精简结果不能复用
            "dub_cps_budget": _dub_capacity_cps(options),
        },
        "llm": llm_expected,
    }
    return _config_matches(report, {"config": expected}, tolerance=0.01)


def _translation_summary(report: dict[str, object]) -> dict[str, object]:
    summary = section(report, "summary")
    cps = section(summary, "cps")
    condense = section(summary, "condense")
    llm = section(section(report, "config"), "llm")
    config = section(report, "config")
    return {
        "cues": summary.get("cues"),
        "paragraphs": summary.get("paragraphs"),
        "fallback_cues": summary.get("fallback_cues"),
        "provider": config.get("provider"),
        "provider_fallback": summary.get("provider_fallback"),
        "cps": cps.get("mean"),
        "cps_over_limit": cps.get("over_limit"),
        "condense_triggered": condense.get("triggered"),
        "review_changes": section(summary, "review").get("cues_changed"),
        "tokens": section(llm, "usage").get("total_tokens"),
    }


def _report_config(
    options: PipelineOptions, stages: Sequence[dict[str, object]]
) -> dict[str, object]:
    """``pipeline.json`` 的 config 段：只写"实际生效"的值，关掉的开关写 None。

    这一段是用来复盘"这次到底拿什么参数跑出来的"，所以宁可累赘也不省略；写错字段的
    表现是看报告时以为参数没生效。
    """

    needs_diarize = _needs_diarize(options)
    capacity = _dub_capacity_cps(options)
    return {
        "src_lang": options.src_lang,
        "tgt_lang": options.tgt_lang,
        "provider": _resolve_provider(options),
        "llm_review": options.llm_review if options.llm else False,
        "glossary_terms": len(options.glossary) if options.glossary else 0,
        "condense": options.condense,
        "condense_mode": options.condense_mode if options.condense else None,
        "translate_fallback": options.translate_fallback,
        "translate_concurrency": options.translate_concurrency,
        "merge": options.merge,
        "merge_max_duration": options.merge_max_duration if options.merge else None,
        "merge_max_gap": options.merge_max_gap if options.merge else None,
        "diarize": needs_diarize,
        "speakers": options.speakers or None,
        "diarization_threshold": _effective_diarize_threshold(options, stages)
        if needs_diarize
        else None,
        "keep_minor_speakers": options.keep_minor_speakers if needs_diarize else None,
        "vad": options.vad if needs_diarize else None,
        "vad_threshold": options.vad_threshold if needs_diarize and options.vad else None,
        "clustering": options.clustering if needs_diarize else None,
        "merge_alpha": options.merge_alpha if needs_diarize else None,
        "speaker_voices": options.speaker_voices,
        "wrap": options.wrap,
        "voice": options.voice or DEFAULT_VOICES.get(options.tgt_lang),
        "tts_rate": _tts_rate(options),
        "dub_capacity_cps": round(capacity, 2) if capacity is not None else None,
        "tts_command": options.tts_command,
        "tts_provider": "command" if options.tts_command else options.tts_provider,
        "tts_fallback": options.tts_fallback
        if options.tts_provider == "edge" and not options.tts_command
        else "off",
        "local_tts": {
            "model": options.local_tts_model,
            "kind": options.local_tts_kind,
            "voice": options.local_tts_voice or None,
            "speed": options.local_tts_speed,
            "threads": options.local_tts_threads,
            "fingerprint": _local_tts_fingerprint(options),
        },
        "regenerate_tts": options.regenerate_tts,
        "tempo_limit": options.tempo_limit,
        "adaptive_rate": options.adaptive_rate,
        "keep_bgm": options.keep_bgm,
        "bgm_mode": options.bgm_mode if options.keep_bgm else None,
        "burn_subtitles": options.burn_subtitles,
        "burn_mode": options.burn_mode if options.burn_subtitles else None,
        "burn_crf": options.burn_crf if options.burn_subtitles else None,
        "burn_position": options.burn_position if options.burn_subtitles else None,
        "mark": options.mark,
        "force": options.force,
        "stop_after": options.stop_after,
        "pricing": options.pricing,
    }


@dataclass(slots=True)
class _Run:
    """一次 pipeline 运行的账本：阶段清单、告警、产物、总体状态，以及"怎么收尾"。

    以前这些是 ``run_pipeline`` 的局部变量，每个失败分支都要把 9 个变量原样转交给
    ``_finish``（一共 17 处），少传一个是 bug、传错顺序更难看出来。收进对象之后，
    失败是一句 ``return run.fail(stage, exc)``，收尾是一句 ``run.finish()``。
    """

    options: PipelineOptions
    source: Path
    out_dir: Path
    started: float
    started_at: datetime
    stages: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    status: str = "ok"
    _source_sha: str | None = None

    def source_sha256(self) -> str:
        """源文件 sha256 只算一次：probe / asr 的缓存判据都要它，长视频不能重读一遍。"""

        if self._source_sha is None:
            self._source_sha = _sha256(self.source)
        return self._source_sha

    def cached(self, *paths: Path) -> bool:
        """这些产物齐了、且没加 ``--force``，才允许走缓存分支。"""

        return not self.options.force and all(Path(path).is_file() for path in paths)

    def record(
        self,
        stage: str,
        state: str,
        *,
        wall: float = 0.0,
        artifacts: Sequence[Path] = (),
        summary: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        """往 ``pipeline.json`` 的 stages 里追加一条；``wall`` 只记真正干了活的时间。"""

        entry: dict[str, object] = {
            "stage": stage,
            "status": state,
            "wall_s": round(wall, 2),
            "artifacts": [str(Path(item).resolve()) for item in artifacts if item],
        }
        if summary:
            entry["summary"] = summary
        if error:
            entry["error"] = error
        self.stages.append(entry)

    def note(self, message: str) -> None:
        """记一条告警：成片仍然可用，但用户需要知道发生了什么。"""

        self.warnings.append(message)

    def carry_warnings(self, report: Mapping[str, object]) -> None:
        """把生产函数报告里的 warnings 原样带进 ``pipeline.json``。"""

        self.warnings.extend(str(item) for item in report.get("warnings") or [])

    def fail(self, stage: str, error: Exception) -> bool:
        """记失败并返回 False：阶段函数 ``return`` 它，主循环就在这里收尾。"""

        self.status = "failed"
        self.record(stage, "failed", error=str(error))
        return False

    def entry(self, stage: str) -> dict[str, object] | None:
        """该阶段最近一条清单项——阶段跑完之后回填摘要用（例如合并后重建的说话人）。"""

        for item in reversed(self.stages):
            if item.get("stage") == stage:
                return item
        return None

    def finish(self) -> PipelineResult:
        """折算成本、写 ``pipeline.json``、按状态给退出码；成功/失败/半成功都走这里。"""

        options = self.options
        try:
            pricing = load_pricing(options.pricing)
        except CostError as exc:
            raise PipelineError(str(exc)) from exc
        # 产物累计口径：从各阶段报告抽量再折算；缓存重跑数字不变
        cost = build_cost(
            self.out_dir,
            tts_command=options.tts_command,
            pricing=pricing,
            pricing_path=options.pricing,
        )
        report_path = self.out_dir / "pipeline.json"
        self.outputs["pipeline_report"] = str(report_path.resolve())
        size, digest = file_digest(self.source)
        report: dict[str, object] = {
            "schema_version": 1,
            "tool": "submatrix",
            "version": __version__,
            "command": "pipeline",
            "generated_at": self.started_at.isoformat(),
            "status": self.status,
            "input": {
                "path": str(self.source.resolve()),
                "bytes": size,
                "sha256": digest,
            },
            "config": _report_config(options, self.stages),
            "stages": self.stages,
            "cost": cost,
            "outputs": self.outputs,
            "warnings": list(dict.fromkeys(self.warnings)),
            "wall_s": round(time.monotonic() - self.started, 1),
        }
        atomic.write_json(report_path, report)
        exit_code = EXIT_OK if self.status == "ok" else EXIT_PARTIAL
        return PipelineResult(
            status=self.status,
            exit_code=exit_code,
            report=report,
            report_path=report_path.resolve(),
            outputs=self.outputs,
        )


@dataclass(slots=True)
class _Track:
    """阶段之间传递的产物链：每个阶段读上一段、写下一段。

    ``subtitle_path`` 与 ``working_srt`` 刻意分开：前者是 ASR/内封直出的**原始**字幕，
    后者是"当前这一版"（merge 合并、clean 过滤都会换成新文件）。合并之后仍然要用原始
    字幕重建说话人映射，所以两者不能合并成一个变量。
    """

    src_lang: str
    probe_status: str = ""
    subtitle_path: Path | None = None
    working_srt: Path | None = None
    # 原始字幕每条 cue 的说话人（合并后重映射要用）
    cue_speakers: tuple[str, ...] = ()
    speakers_path: Path | None = None
    speaker_voice_map: dict[str, str] = field(default_factory=dict)
    translated_srt: Path | None = None
    dubbed_video: Path | None = None
    mark_input: Path | None = None
    # OPT-5：ASR 期间后台预取的 diarize 分段（None = 没有预取）
    diarize_prefetch: object | None = None


@dataclass(frozen=True, slots=True)
class _Step:
    """流程里的一步。``stage`` 为 None 表示"衔接钩子"。

    钩子不在 ``--stop-after`` 清单里，也不一定产出阶段条目（clean 就是把结论回填到
    merge 的摘要上）。但它们必须在流程里占一个位置，而不能偷偷塞进相邻阶段的末尾：
    ``--stop-after merge`` 要停在"合并完成"那一刻，不能连"合并后的说话人重建"也顺手做掉。
    """

    stage: str | None
    fn: Callable[[_Run, _Track], bool]


def run_pipeline(options: PipelineOptions) -> PipelineResult:
    options.validate()
    source = Path(options.src)
    if not source.is_file():
        raise PipelineError(f"输入文件不存在：{source}")
    out_dir = Path(options.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run = _Run(
        options=options,
        source=source,
        out_dir=out_dir,
        started=time.monotonic(),
        started_at=options.generated_at or datetime.now().astimezone(),
    )
    track = _Track(src_lang=options.src_lang)
    for step in _STEPS:
        if not step.fn(run, track):
            break
        # `--stop-after` 的含义是"跑完这个阶段就停"，所以判定必须在阶段跑完之后
        if step.stage is not None and options.stop_after == step.stage:
            break
    return run.finish()


# ----------------------------------------------------------------------------
# 阶段缓存的判据（每阶段一个函数；"缺字段＝重跑"的口径见 `_config_matches`）
# ----------------------------------------------------------------------------


def _probe_src_lang(options: PipelineOptions) -> str:
    """probe 探测用的源语言：``auto`` 统一按 zh（与 ``ProbeOptions`` 的默认口径一致）。

    内封字幕路线要求显式源语言——字幕流本身没有可靠的语言标记。
    """

    return options.src_lang if options.src_lang != "auto" else "zh"


def _probe_cache_hit(run: _Run, manifest: dict[str, object]) -> bool:
    """probe 缓存判据：必须绑到"这个视频 + 这个源语言"。

    只看 ``manifest.json`` 是否存在时，换一个视频复用同一个 ``--out`` 会直接输出上一条
    视频的字幕（真实翻车），所以输入 sha256 与源语言都要对得上。``--force`` 仍然要先挡掉：
    空 manifest（读不出来）一律当没缓存。
    """

    probe_dir = run.out_dir / "probe"
    return (
        run.cached(probe_dir / "manifest.json")
        and bool(manifest)
        and _config_matches(
            manifest,
            {
                "input": {"sha256": run.source_sha256()},
                "src_lang": _probe_src_lang(run.options),
            },
        )
    )


def _asr_cache_hit(run: _Run, report: dict[str, object], *, hotwords: Sequence[str]) -> bool:
    """ASR 缓存判据：换视频 / 改源语言 / 改热词 / 改 initial prompt / 换模型都要重跑。

    模型那一项单独由 ``_asr_model_matches`` 判（要兼顾"没显式指定时的默认模型归属"）。
    """

    options = run.options
    asr_dir = run.out_dir / "asr"
    return (
        run.cached(asr_dir / "asr.json", asr_dir / "source.srt")
        and _asr_model_matches(report, options)
        and _config_matches(
            report,
            {
                "input": {"sha256": run.source_sha256()},
                "config": {
                    "language": options.src_lang,
                    "hotwords": list(hotwords),
                    "initial_prompt": options.initial_prompt,
                },
            },
        )
    )


def _speaker_by_cue(speakers_path: Path | None, srt: Path) -> tuple[str, ...]:
    """``speakers.json`` → 原始字幕每条 cue 的说话人。

    返回的元组长度必须和字幕条数对齐：合并阶段靠这个下标把"哪句话是谁说的"带到新的
    时间轴上。读不出来就返回空元组（退回单音色，不让整条链路因此失败）。
    """

    if speakers_path is None:
        return ()
    assignments = _read_json(Path(speakers_path))
    by_cue = {
        int(item["cue"]): str(item.get("speaker") or "")
        for item in assignments.get("cues") or []
        if isinstance(item, dict) and "cue" in item
    }
    try:
        source_cues = load_srt(srt)[0]
    except (OSError, ValueError):
        return ()
    return tuple(by_cue.get(index, "") for index in range(1, len(source_cues.cues) + 1))


def _merge_cache_hit(
    run: _Run, track: _Track, report: dict[str, object], *, speaker_aware: bool
) -> bool:
    """merge 缓存判据：上游字幕 sha + 合并窗口 + 说话人归属文件。

    ``speaker_aware`` 翻转时两侧都不能复用：断句依据从"只看时间"变成"按人分开"，
    合并结果天然不是同一份。
    """

    options = run.options
    merge_dir = run.out_dir / "merge"
    if not (
        run.cached(merge_dir / "merge.json", merge_dir / "merged.srt")
        and _report_sha_matches(report, track.working_srt, "input", "sha256")
        # 合并窗口变了，合并结果就不是同一份字幕
        and _config_matches(
            report,
            {
                "config": {
                    "max_duration_ms": round(options.merge_max_duration * 1000),
                    "max_gap_ms": round(options.merge_max_gap * 1000),
                }
            },
        )
    ):
        return False
    if speaker_aware:
        return _report_sha_matches(
            report, run.out_dir / "diarize" / "speakers.json", "input", "speakers", "sha256"
        )
    return not section(report, "input").get("speakers")


def _translate_cache_hit(
    run: _Run,
    track: _Track,
    report: dict[str, object],
    *,
    translated_srt: Path,
    provider: str,
) -> bool:
    """translate 缓存判据：上游字幕 sha + 所有会改变译文的参数（见
    ``_translate_config_matches``）。"""

    return (
        run.cached(translated_srt.parent / "translate.json", translated_srt)
        and _report_sha_matches(report, track.working_srt, "input", "sha256")
        and _translate_config_matches(
            report, run.options, src_lang=track.src_lang, provider=provider
        )
    )


def _build_cache_hit(
    run: _Run,
    track: _Track,
    report: dict[str, object],
    *,
    publish_srt: Path,
    limits: Limits,
) -> bool:
    """build 缓存判据：两条字幕的 sha + 语向阈值 + 排版开关（都会改折行/切条）。"""

    options = run.options
    return (
        run.cached(publish_srt.parent / "report.json", publish_srt)
        and _report_sha_matches(report, track.working_srt, "inputs", "source", "sha256")
        and _report_sha_matches(report, track.translated_srt, "inputs", "target", "sha256")
        and _config_matches(
            report,
            {
                "config": {
                    "src_lang": track.src_lang,
                    "tgt_lang": options.tgt_lang,
                    "cps_src": limits.cps_src,
                    "cps_tgt": limits.cps_tgt,
                    "width_src": limits.width_src,
                    "width_tgt": limits.width_tgt,
                    "max_lines": limits.max_lines,
                    "layout": bool(options.wrap),
                }
            },
        )
    )


def _dub_cache_hit(
    run: _Run, track: _Track, report: dict[str, object], *, voice: str, rate: str | None
) -> bool:
    """dub 缓存判据：字幕 sha + 音色/语速/TTS 引擎 + 说话人 + BGM + 上次是否成功。

    任何一项漏掉的表现都是"改了参数却拿到旧配音"——听感上完全看不出来，
    用户只会觉得"怎么设了没生效"。
    """

    options = run.options
    dub_dir = run.out_dir / "dub"
    return (
        run.cached(dub_dir / "dub_report.json", dub_dir / "dubbed.mp4")
        # --regenerate-tts 明确要求重新合成，不能拿旧配音当缓存（云端 TTS 会重新计费）
        and not options.regenerate_tts
        and _report_sha_matches(report, track.translated_srt, "input", "srt", "sha256")
        and _speakers_cache_matches(report, track.speakers_path)
        and _dub_tts_cache_matches(
            report,
            voice=voice or "",
            rate=rate,
            voice_map=track.speaker_voice_map,
            tts_command=options.tts_command,
            tempo_limit=options.tempo_limit,
            # 报告里记的是"实际生效"值（外部命令引擎不支持提语速），比较时也要用生效值
            adaptive_rate=options.adaptive_rate and not options.tts_command,
            tts_provider="command" if options.tts_command else options.tts_provider,
            tts_fallback=options.tts_fallback
            if options.tts_provider == "edge" and not options.tts_command
            else "off",
            local_fingerprint=_local_tts_fingerprint(options),
        )
        # BGM：开没开、用什么模式、从哪条音轨取，都改产物；只看字幕 sha 会导致
        # "勾了保留 BGM 但成片没变"这种静默失效。（增益/闪避 pipeline 不暴露，
        # 恒为 DubOptions 默认值，故不进判据。）
        and _config_matches(
            report,
            {
                "config": {
                    "bgm": {
                        "enabled": bool(options.keep_bgm),
                        "mode": options.bgm_mode if options.keep_bgm else None,
                        "source": str(Path(options.bgm_source or run.source).resolve())
                        if options.keep_bgm
                        else None,
                    }
                }
            },
            tolerance=0.01,
        )
        # 上次 partial（例如 TTS 解释器没装 edge-tts、云端临时失败）不能算缓存，
        # 否则用户重跑永远拿不到配音，只能手动删目录；旧报告没有 status 字段时按 ok 处理
        and str(report.get("status") or "ok") == "ok"
    )


def _burn_cache_hit(
    run: _Run, track: _Track, report: dict[str, object], *, subtitle: Path
) -> bool:
    """burn 缓存判据：配音成片与要烧的字幕都不能变，码率/位置也改产物。"""

    burn_dir = run.out_dir / "burn"
    return (
        run.cached(burn_dir / "burn.json", burn_dir / "burned.mp4")
        and _report_sha_matches(report, track.dubbed_video, "input", "video", "sha256")
        and _report_sha_matches(report, subtitle, "input", "subtitle", "sha256")
        and _config_matches(
            report,
            {"config": {"crf": run.options.burn_crf, "position": run.options.burn_position}},
        )
    )


def _mark_cache_hit(run: _Run, report: dict[str, object], *, src: Path) -> bool:
    """mark 缓存判据：标识打在哪个视频上，看输入 sha 就够（这一步没有别的参数）。"""

    final_dir = run.out_dir / "final"
    return run.cached(final_dir / "mark.json", final_dir / "marked.mp4") and _report_sha_matches(
        report, src, "input", "sha256"
    )


# ----------------------------------------------------------------------------
# 各阶段：判缓存 → 跑生产函数 → 写阶段条目。返回 False 表示已失败并记过账，
# 主循环随即停下交给 `run.finish()`。
# ----------------------------------------------------------------------------


def _stage_probe(run: _Run, track: _Track) -> bool:
    """1. probe —— 判字幕路线：内封文本直出 / 图形字幕报错 / 无字幕交给 ASR。"""

    probe_dir = run.out_dir / "probe"
    src_lang = _probe_src_lang(run.options)
    stage_start = time.monotonic()
    try:
        manifest = _read_json(probe_dir / "manifest.json")
        if _probe_cache_hit(run, manifest):
            track.probe_status = str(manifest.get("status") or "")
            if track.probe_status == "extracted" and (probe_dir / "source.srt").is_file():
                track.subtitle_path = probe_dir / "source.srt"
            run.record("probe", "cached", summary={"status": track.probe_status})
        else:
            probe = run_probe(ProbeOptions(src=run.source, out=probe_dir, src_lang=src_lang))
            track.probe_status = probe.status
            track.subtitle_path = probe.subtitle_path
            run.carry_warnings(probe.manifest)
            run.record(
                "probe",
                "ok",
                wall=time.monotonic() - stage_start,
                artifacts=[probe.manifest_path]
                + ([track.subtitle_path] if track.subtitle_path else []),
                summary={"status": track.probe_status, "cues": probe.cues},
            )
        if track.probe_status == "unsupported":
            raise PipelineError(
                "输入只有图形字幕轨（PGS/VobSub），本工具不做硬字幕 OCR，无法继续"
            )
    except (ProbeError, PipelineError, OSError) as exc:
        return run.fail("probe", exc)
    return True


def _stage_asr(run: _Run, track: _Track) -> bool:
    """2. asr —— 没字幕就识别；``auto`` 语言在这里解析成具体语言。"""

    options = run.options
    asr_dir = run.out_dir / "asr"
    if track.probe_status == "extracted":
        if track.subtitle_path is None:
            track.subtitle_path = run.out_dir / "probe" / "source.srt"
        # 内封字幕是人工断句，没有"识别出语言"这一步——拿不到语言就没法定翻译方向
        if options.src_lang == "auto":
            run.status = "failed"
            run.record(
                "asr",
                "failed",
                error="内封字幕路线需要显式 --src-lang（字幕流无法可靠判断语言）",
            )
            return False
        run.record("asr", "skipped", summary={"reason": "内封字幕直出，无需 ASR"})
        run.outputs["source_srt"] = str(track.subtitle_path.resolve())
        return True

    stage_start = time.monotonic()
    # 热词与术语表原文既影响 ASR 结果，也必须进缓存判据——所以先算好一份，两处共用
    hotwords = list(options.hotwords)
    if options.glossary:
        hotwords.extend(options.glossary.sources)
    asr_hotwords = list(dict.fromkeys(hotwords))
    try:
        report = _read_json(asr_dir / "asr.json")
        if _asr_cache_hit(run, report, hotwords=asr_hotwords):
            summary = section(report, "summary")
            detected = str(summary.get("language") or "")
            if options.src_lang == "auto":
                track.src_lang = _map_language(detected)
            track.subtitle_path = asr_dir / "source.srt"
            # 报告里的 cue 数是 ASR 当时的；字幕被人工校对（并过）之后以文件为准
            current_cues = summary.get("cues")
            try:
                current_cues = len(load_srt(track.subtitle_path)[0])
            except (OSError, ValueError):
                pass
            run.record(
                "asr",
                "cached",
                artifacts=[asr_dir / "asr.json", track.subtitle_path],
                summary={"language": detected, "cues": current_cues},
            )
        else:
            # OPT-5：分段只跟音频和聚类参数有关，与 ASR 无关——趁 ASR 在跑先算掉。
            # `auto` 源语言此时还没解析出具体语言（要等 ASR 报告），无法选声纹模型，故不预取。
            if _needs_diarize(options) and options.src_lang != "auto":
                track.diarize_prefetch = _start_diarize_segments(
                    options, run.source, run.out_dir / "diarize", options.src_lang
                )
            asr = run_asr(
                AsrOptions(
                    src=run.source,
                    out=asr_dir,
                    language=options.src_lang,
                    model=options.asr_model,
                    python=options.asr_python,
                    hotwords=tuple(asr_hotwords),
                    initial_prompt=options.initial_prompt,
                )
            )
            track.subtitle_path = asr.srt_path
            summary = section(asr.report, "summary")
            config = section(asr.report, "config")
            detected = str(summary.get("language") or "")
            if options.src_lang == "auto":
                track.src_lang = _map_language(detected)
            run.carry_warnings(asr.report)
            run.record(
                "asr",
                "ok",
                wall=time.monotonic() - stage_start,
                artifacts=[
                    item for item in (asr.srt_path, asr.words_path, asr.report_path) if item
                ],
                summary={
                    "language": detected,
                    "cues": summary.get("cues"),
                    "words": summary.get("words"),
                    "model": config.get("model_variant"),
                    "rtf": summary.get("rtf"),
                },
            )
        if track.subtitle_path is None or not Path(track.subtitle_path).is_file():
            raise PipelineError("ASR 没有产出字幕文件")
        run.outputs["source_srt"] = str(Path(track.subtitle_path).resolve())
    except (AsrError, PipelineError, OSError) as exc:
        return run.fail("asr", exc)
    return True


def _stage_diarize(run: _Run, track: _Track) -> bool:
    """说话人分离 + 按说话人分派音色（M11）。

    默认关闭的原因是单人口播不需要多花这份时间，行为与 M10 完全一致；给了
    `--speaker-voices` / `--speakers` 也算开启（少敲一个开关）。

    **顺序很关键**：先分离说话人，再合并句子。合并只看时间的话会把两个人的台词并成
    一条字幕，之后无论怎么分派音色都只能一个人读（对话素材一开始就是这样翻车的）。

    OPT-5：分段（diarize 的重活）在 ASR 期间已经在后台并行算完了，这里只补"字幕条 →
    说话人"的归属（毫秒级），省掉 min(ASR, diarize) 的墙钟。

    分离失败**不中断**整条链路：记 partial + 告警，退回单音色配音，成片仍然可用。
    """

    options = run.options
    # 从这里开始下游一律读 working_srt（merge 合并、clean 过滤都会换成新文件）
    track.working_srt = track.subtitle_path
    if not _needs_diarize(options):
        run.record("diarize", "skipped", summary={"reason": "未启用多人声（默认单音色）"})
        return True

    diarize_dir = run.out_dir / "diarize"
    stage_start = time.monotonic()
    try:
        report = _read_json(diarize_dir / "diarize.json")
        prefetched_turns, prefetched_report, prefetch = _diarize_prefetch(track)
        if _diarize_from_cache(run, track, options, diarize_dir, report, prefetched_turns):
            pass
        elif prefetch is not None:
            _diarize_attach(
                run,
                track,
                diarize_dir,
                turns=prefetched_turns,
                report=prefetched_report,
                stage_start=stage_start,
                extra={"parallel_with_asr": True},
            )
        elif not _diarize_from_segments(run, track, options, diarize_dir, stage_start):
            _diarize_fresh(run, track, options, diarize_dir, stage_start)
        _map_speaker_voices(run, track, options)
    except (DiarizeError, OSError) as exc:
        _diarize_fallback(run, track, exc)
    return True


def _diarize_prefetch(
    track: _Track,
) -> tuple[list[SpeakerTurn] | None, dict[str, object] | None, object]:
    """OPT-5：取 ASR 期间后台算好的分段（可能还在跑，也可能是现成元组）。"""

    prefetched_turns: list[SpeakerTurn] | None = None
    prefetched_report: dict[str, object] | None = None
    prefetch = track.diarize_prefetch
    if isinstance(prefetch, _BackgroundJob):
        outcome = prefetch.result()
        # 不要求严格是 DiarizeResult：凡是带 segments/report 的结果都接（便于测试注入）
        segments = getattr(outcome, "segments", None)
        if segments is not None:
            prefetched_turns = list(segments)
            prefetched_report = dict(getattr(outcome, "report", {}) or {})
    elif prefetch is not None:
        prefetched_turns, prefetched_report = prefetch
    return prefetched_turns, prefetched_report, prefetch


def _diarize_from_cache(
    run: _Run,
    track: _Track,
    options: PipelineOptions,
    diarize_dir: Path,
    report: dict[str, object],
    prefetched_turns: Sequence[SpeakerTurn] | None,
) -> bool:
    """四条判据全过才复用上一次的归属结果：产物齐、源片/字幕 sha 未变、配置未变。"""

    report_path = diarize_dir / "diarize.json"
    if prefetched_turns is not None:
        return False
    if not run.cached(report_path, diarize_dir / "speakers.json"):
        return False
    if not _report_sha_matches(report, track.working_srt, "input", "srt", "sha256"):
        return False
    if not _report_sha_matches(report, run.source, "input", "sha256"):
        return False
    if not _diarize_config_matches(report, options, track.src_lang):
        return False
    track.speakers_path = diarize_dir / "speakers.json"
    run.record(
        "diarize",
        "cached",
        artifacts=[report_path, track.speakers_path],
        summary=_diarize_summary(report),
    )
    run.carry_warnings(report)
    return True


def _diarize_attach(
    run: _Run,
    track: _Track,
    diarize_dir: Path,
    *,
    turns: Sequence[SpeakerTurn] | None,
    report: dict[str, object] | None,
    stage_start: float,
    extra: dict[str, object],
) -> None:
    """分段已就绪（并行算完或从 segments.json 复用）→ 只补字幕归属，毫秒级。"""

    if turns is None:
        raise DiarizeError("说话人分段没有产出结果")
    speakers_file, attached_report = attach_speakers(
        turns, track.working_srt, diarize_dir, report=report
    )
    track.speakers_path = speakers_file
    run.carry_warnings(attached_report)
    run.record(
        "diarize",
        "ok" if attached_report.get("status") == "ok" else "partial",
        wall=time.monotonic() - stage_start,
        artifacts=[
            diarize_dir / "diarize.json",
            diarize_dir / SEGMENTS_NAME,
            speakers_file,
        ],
        summary={**_diarize_summary(attached_report), **extra},
    )


def _diarize_from_segments(
    run: _Run,
    track: _Track,
    options: PipelineOptions,
    diarize_dir: Path,
    stage_start: float,
) -> bool:
    """OPT-5：分段已存在（源文件/聚类参数没变）→ 只补归属，不再跑分离模型。"""

    cached_turns, cached_payload = load_segments(diarize_dir / SEGMENTS_NAME)
    if not cached_turns or options.force:
        return False
    if not _segments_matches(cached_payload, options, run.source, track.src_lang):
        return False
    _diarize_attach(
        run,
        track,
        diarize_dir,
        turns=cached_turns,
        report=_segments_base_report(cached_payload),
        stage_start=stage_start,
        extra={"reused_segments": True},
    )
    return True


def _diarize_fresh(
    run: _Run,
    track: _Track,
    options: PipelineOptions,
    diarize_dir: Path,
    stage_start: float,
) -> None:
    """什么都没有可复用的 → 真跑一次分离（最慢的一条路）。"""

    diarization = run_diarize(
        _diarize_options(
            options,
            source=run.source,
            out_dir=diarize_dir,
            srt=track.working_srt,
            language=track.src_lang,
        )
    )
    track.speakers_path = diarization.speakers_path
    run.carry_warnings(diarization.report)
    run.record(
        "diarize",
        "ok" if diarization.status == "ok" else "partial",
        wall=time.monotonic() - stage_start,
        artifacts=[diarization.report_path]
        + ([diarization.speakers_path] if diarization.speakers_path else []),
        summary=_diarize_summary(diarization.report),
    )


def _map_speaker_voices(run: _Run, track: _Track, options: PipelineOptions) -> None:
    """按说话人数分派音色；只有 1 个说话人时退回单音色（多音色没有意义）。"""

    if track.speakers_path is None:
        return
    run.outputs["speakers_json"] = str(track.speakers_path.resolve())
    assignments = _read_json(track.speakers_path)
    speakers = [str(item) for item in assignments.get("speakers") or []]
    explicit, pool = parse_speaker_voices(options.speaker_voices)
    if not explicit and not pool:
        pool = DEFAULT_SPEAKER_VOICES.get(options.tgt_lang, ())
    default_voice = (
        options.voice or DEFAULT_VOICES.get(options.tgt_lang) or "en-US-AriaNeural"
    )
    if len(speakers) < 2:
        track.speakers_path = None
        run.note("只识别到 1 个说话人，已退回单音色配音")
        return
    track.speaker_voice_map = build_speaker_voice_map(
        speakers, explicit=explicit, pool=pool, default=default_voice
    )
    entry = run.entry("diarize")
    if entry is not None:
        summary = entry.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["speaker_voices"] = dict(sorted(track.speaker_voice_map.items()))
    if not explicit and pool and len(speakers) > len(pool):
        run.note(
            f"检测到 {len(speakers)} 个说话人，但只配置了 {len(pool)} 个音色，"
            "多出来的说话人会复用前面的音色"
        )


def _diarize_fallback(run: _Run, track: _Track, exc: Exception) -> None:
    """分离失败 → 诚实告警 + 退回单音色，不让整条链路白跑（成片仍然可用）。"""

    run.status = "partial"
    run.record("diarize", "failed", error=str(exc))
    run.note(f"说话人分离失败，已退回单音色配音：{exc}")
    track.speakers_path = None
    track.speaker_voice_map = {}


def _stage_merge(run: _Run, track: _Track) -> bool:
    """4. merge —— 把 ASR 的碎句并成句子级 cue（多人声时必须按说话人分段）。

    ASR 按停顿切段，天然产出"半句碎片"（"为了生存，它们展开" / "一场盐的争夺战。"），
    直接翻译会得到英文半句（"To survive, they wage." / "a battle for salt."），
    句子读不通、配音槽位也被切碎。
    """

    options = run.options
    merge_dir = run.out_dir / "merge"
    merged_srt = merge_dir / "merged.srt"
    track.cue_speakers = _speaker_by_cue(track.speakers_path, track.working_srt)
    speaker_aware = len({speaker for speaker in track.cue_speakers if speaker}) >= 2

    if track.probe_status == "extracted":
        # 内封字幕是人工断句，合并只会破坏人家切好的句子
        run.record("merge", "skipped", summary={"reason": "内封字幕是人工断句，跳过合并"})
        return True
    if not options.merge:
        run.record("merge", "skipped", summary={"reason": "已用 --no-merge 关闭合并"})
        return True

    stage_start = time.monotonic()
    try:
        report = _read_json(merge_dir / "merge.json")
        if _merge_cache_hit(run, track, report, speaker_aware=speaker_aware):
            track.working_srt = merged_srt
            run.record(
                "merge",
                "cached",
                artifacts=[merge_dir / "merge.json", merged_srt],
                summary=_merge_summary(report),
            )
        else:
            merged = run_merge(
                track.working_srt,
                merge_dir,
                MergeOptions(
                    max_duration_ms=round(options.merge_max_duration * 1000),
                    max_gap_ms=round(options.merge_max_gap * 1000),
                    speakers=track.cue_speakers if speaker_aware else (),
                    speakers_path=run.out_dir / "diarize" / "speakers.json"
                    if speaker_aware
                    else None,
                ),
            )
            track.working_srt = merged.merged_path
            run.record(
                "merge",
                "ok",
                wall=time.monotonic() - stage_start,
                artifacts=[merged.report_path, merged.merged_path],
                summary=_merge_summary(merged.report),
            )
        run.outputs["merged_srt"] = str(track.working_srt.resolve())
    except (MergeError, OSError) as exc:
        return run.fail("merge", exc)
    return True


def _remap_speakers_after_merge(run: _Run, track: _Track) -> bool:
    """合并会改变 cue 的条数/时间轴，说话人映射要跟着搬到合并后的字幕上（配音按条选音色）。"""

    if track.speakers_path is None or not track.cue_speakers:
        return True
    if track.working_srt == track.subtitle_path:
        return True
    try:
        remapped = _remap_speaker_assignments(
            track.subtitle_path,
            track.working_srt,
            track.cue_speakers,
            run.out_dir / "diarize" / "speakers.merged.json",
        )
    except (OSError, ValueError) as exc:
        run.note(f"合并后说话人映射重建失败，已退回单音色配音：{exc}")
        track.speakers_path = None
        track.speaker_voice_map = {}
        return True
    if remapped is not None:
        track.speakers_path = remapped
        run.outputs["speakers_json"] = str(remapped.resolve())
    return True


def _clean_degenerate_cues(run: _Run, track: _Track) -> bool:
    """翻译前的过滤：丢掉片尾音乐/掌声/环境声被听成的重复音节。

    ASR 会把这类声音听成重复音节（CCTV 实测「咪咪咪…」一条 200+ 字），送到 LLM 会让
    模型跟着退化成 ``Mi mi mi…``（不是 JSON）→ 整批翻译失败。内封字幕是人工断句，不做
    这种猜测。这一步不单独占一个阶段，结论回填到 merge 的摘要上（``degenerate_dropped``）。
    """

    if track.probe_status == "extracted":
        return True
    merge_dir = run.out_dir / "merge"
    cleaned = cleanup_srt(track.working_srt, merge_dir / "clean.srt")
    if not cleaned.dropped:
        return True
    track.working_srt = cleaned.srt_path
    run.outputs["clean_srt"] = str(cleaned.srt_path.resolve())
    run.note(
        "已过滤 %d 条疑似音乐/噪声字幕（cue %s），不参与翻译与配音"
        % (len(cleaned.dropped), "、".join(str(index) for index in cleaned.dropped))
    )
    if track.speakers_path is not None:
        try:
            cleaned_speakers = cleanup_speakers(
                track.speakers_path, cleaned.dropped, merge_dir / "speakers.clean.json"
            )
        except (OSError, ValueError):
            cleaned_speakers = None
        if cleaned_speakers is not None:
            track.speakers_path = cleaned_speakers
            run.outputs["speakers_json"] = str(cleaned_speakers.resolve())
    entry = run.entry("merge")
    if entry is not None:
        summary = entry.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["degenerate_dropped"] = len(cleaned.dropped)
    return True


def _stage_translate(run: _Run, track: _Track) -> bool:
    """5. translate —— 逐条译成目标语言（可精简、可 LLM 复查）。"""

    options = run.options
    translate_dir = run.out_dir / "translate"
    translated_srt = translate_dir / f"{options.tgt_lang}.srt"
    stage_start = time.monotonic()
    try:
        provider = _resolve_provider(options)
        report = _read_json(translate_dir / "translate.json")
        if _translate_cache_hit(
            run, track, report, translated_srt=translated_srt, provider=provider
        ):
            run.record(
                "translate",
                "cached",
                artifacts=[translate_dir / "translate.json", translated_srt],
                summary=_translation_summary(report),
            )
            run.carry_warnings(report)
        else:
            llm_config: LlmConfig | None = None
            if provider == "openai":
                llm_config = options.llm_config or LlmConfig.from_env()
                if not llm_config.configured:
                    raise PipelineError(
                        "--llm 需要 LLM key：设置环境变量 DEEPSEEK_API_KEY（或 OPENAI_API_KEY）"
                    )
            translate = run_translate(
                TranslateOptions(
                    src=track.working_srt,
                    out=translate_dir,
                    src_lang=track.src_lang,
                    tgt_lang=options.tgt_lang,
                    provider=provider,
                    glossary=options.glossary,
                    condense=options.condense,
                    condense_mode=options.condense_mode,
                    dub_cps_budget=_dub_capacity_cps(options),
                    fallback_provider=options.translate_fallback,
                    concurrency=options.translate_concurrency,
                    llm=llm_config,
                    llm_review=options.llm_review if provider == "openai" else False,
                )
            )
            translated_srt = Path(translate.output_path or translated_srt)
            run.carry_warnings(translate.report)
            run.record(
                "translate",
                "ok",
                wall=time.monotonic() - stage_start,
                artifacts=[translate.report_path, translated_srt],
                summary=_translation_summary(translate.report),
            )
        track.translated_srt = translated_srt
        run.outputs["translated_srt"] = str(translated_srt.resolve())
    except (TranslateError, PipelineError, OSError) as exc:
        return run.fail("translate", exc)
    return True


def _stage_build(run: _Run, track: _Track) -> bool:
    """6. build —— 排版出发布字幕（按宽度/CPS 折行切条），校验不过整条链路失败。"""

    options = run.options
    matrix_dir = run.out_dir / "matrix"
    publish_srt = matrix_dir / f"{options.tgt_lang}.srt"
    # 阈值由语向推出；排版开关会改折行/切条——两样都必须在报告里对得上才能复用
    limits = Limits.from_languages(track.src_lang, options.tgt_lang)
    stage_start = time.monotonic()
    try:
        report = _read_json(matrix_dir / "report.json")
        if _build_cache_hit(run, track, report, publish_srt=publish_srt, limits=limits):
            run.record(
                "build",
                "cached",
                artifacts=[matrix_dir / "report.json", publish_srt],
                summary=_matrix_summary(report),
            )
        else:
            matrix = run_matrix(
                MatrixOptions(
                    src=track.working_srt,
                    tgt=track.translated_srt,
                    out=matrix_dir,
                    src_lang=track.src_lang,
                    tgt_lang=options.tgt_lang,
                    limits=limits,
                    layout=options.wrap,
                )
            )
            if matrix.status == "error":
                raise PipelineError("字幕矩阵校验失败（见 matrix/report.json）")
            run.record(
                "build",
                "ok",
                wall=time.monotonic() - stage_start,
                artifacts=[matrix.report_path, *matrix.outputs],
                summary=_matrix_summary(matrix.report),
            )
        run.outputs["publish_srt"] = str(publish_srt.resolve())
        bilingual = matrix_dir / "bilingual.srt"
        if bilingual.is_file():
            run.outputs["bilingual_srt"] = str(bilingual.resolve())
    except (PipelineError, OSError) as exc:
        return run.fail("build", exc)
    return True


def _stage_dub(run: _Run, track: _Track) -> bool:
    """7. dub —— 按句子级译文合成配音、对轨、混回成片。

    配音失败意味着"成片没声音"，但外挂字幕照旧可用，所以这里记 partial 而不是整体失败。
    """

    options = run.options
    dub_dir = run.out_dir / "dub"
    dubbed_video = dub_dir / "dubbed.mp4"
    voice = options.voice or DEFAULT_VOICES.get(options.tgt_lang)
    rate = _tts_rate(options)
    stage_start = time.monotonic()
    try:
        report = _read_json(dub_dir / "dub_report.json")
        if _dub_cache_hit(run, track, report, voice=voice or "", rate=rate):
            run.record(
                "dub",
                "cached",
                artifacts=[dub_dir / "dub_report.json", dubbed_video],
                summary=_dub_summary(report),
            )
        else:
            dub = run_dub(
                DubOptions(
                    srt=track.translated_srt,
                    out=dub_dir,
                    video=run.source,
                    voice=voice or "en-US-AriaNeural",
                    voice_by_speaker=tuple(sorted(track.speaker_voice_map.items())),
                    speakers_path=track.speakers_path,
                    tts_rate=rate,
                    concurrency=options.concurrency,
                    tempo_limit=options.tempo_limit,
                    adaptive_rate=options.adaptive_rate,
                    tts_command=options.tts_command,
                    tts_python=options.tts_python,
                    tts_provider=options.tts_provider,
                    tts_fallback=options.tts_fallback,
                    local_tts_model=options.local_tts_model,
                    local_tts_kind=options.local_tts_kind,
                    local_tts_voice=options.local_tts_voice,
                    local_tts_python=options.local_tts_python,
                    local_tts_threads=options.local_tts_threads,
                    local_tts_speed=options.local_tts_speed,
                    tgt_lang=options.tgt_lang,
                    reuse_tts=not options.regenerate_tts,
                    keep_bgm=options.keep_bgm,
                    bgm_mode=options.bgm_mode,
                    bgm_source=options.bgm_source,
                    separator_python=options.separator_python,
                    retry_delay_s=0.0 if options.tts_command else 1.0,
                )
            )
            dubbed_video = dub.video_path or dubbed_video
            run.carry_warnings(dub.report)
            run.record(
                "dub",
                "ok" if dub.status == "ok" else "partial",
                wall=time.monotonic() - stage_start,
                artifacts=[dub.report_path, dub.audio_path]
                + ([dub.video_path] if dub.video_path else []),
                summary=_dub_summary(dub.report),
            )
            if dub.status != "ok":
                run.status = "partial"
        track.dubbed_video = dubbed_video
        run.outputs["dub_video"] = str(dubbed_video.resolve())
        run.outputs["dub_audio"] = str((dub_dir / "dubbed.wav").resolve())
    except (DubError, OSError) as exc:
        return run.fail("dub", exc)
    return True


def _stage_burn(run: _Run, track: _Track) -> bool:
    """8. burn —— 把发布字幕烧进成片，让字幕跟着画面走。

    关掉外挂字幕、直接出"带字幕的成片"（收费软件默认行为）：发布到不认外挂字幕的平台
    也不会丢。默认烧译文，``--burn-mode bilingual`` 烧中英双语。
    """

    options = run.options
    matrix_dir = run.out_dir / "matrix"
    publish_srt = matrix_dir / f"{options.tgt_lang}.srt"
    burn_dir = run.out_dir / "burn"
    burned_video = burn_dir / "burned.mp4"
    # 默认把标识打在配音成片上；烧过字幕就打在烧录产物上
    track.mark_input = track.dubbed_video
    if not options.burn_subtitles:
        run.record("burn", "skipped", summary={"reason": "已用 --no-burn-subtitles 关闭"})
        return True

    burn_subtitle = (
        matrix_dir / "bilingual.ass" if options.burn_mode == "bilingual" else publish_srt
    )
    if not burn_subtitle.is_file():
        run.note(f"要烧录的字幕不存在（{burn_subtitle.name}），已改用单语字幕")
        burn_subtitle = publish_srt
    stage_start = time.monotonic()
    try:
        report = _read_json(burn_dir / "burn.json")
        if _burn_cache_hit(run, track, report, subtitle=burn_subtitle):
            run.record(
                "burn",
                "cached",
                artifacts=[burn_dir / "burn.json", burned_video],
                summary=_burn_summary(report),
            )
            run.carry_warnings(report)
        else:
            burn = run_burn(
                BurnOptions(
                    video=track.dubbed_video,
                    subtitle=burn_subtitle,
                    out=burn_dir,
                    crf=options.burn_crf,
                    position=options.burn_position,
                )
            )
            burned_video = burn.video_path
            run.carry_warnings(burn.report)
            run.record(
                "burn",
                "ok" if burn.status == "ok" else "partial",
                wall=time.monotonic() - stage_start,
                artifacts=[burn.report_path, burned_video, burn.ass_path],
                summary=_burn_summary(burn.report),
            )
            if burn.status != "ok":
                run.status = "partial"
                run.note("字幕烧录核验未通过，发布前请人工看一眼成片")
        run.outputs["burned_video"] = str(burned_video.resolve())
        track.mark_input = burned_video
    except (BurnError, OSError) as exc:
        return run.fail("burn", exc)
    return True


def _stage_mark(run: _Run, track: _Track) -> bool:
    """9. mark —— 打 AIGC 标识并核验（显式标识 + 元数据往返都要能读回来）。"""

    options = run.options
    final_dir = run.out_dir / "final"
    marked_video = final_dir / "marked.mp4"
    if not options.mark:
        run.record("mark", "skipped", summary={"reason": "已用 --no-mark 关闭 AIGC 标识"})
        return True

    mark_src = track.mark_input
    if mark_src is None:
        # 只有"上一段没有产出成片"时才会到这儿（配音失败会直接收尾），此处只防御：
        # 与其让 MarkOptions(src=None) 抛一个看不懂的 TypeError，不如记成可读的失败。
        return run.fail("mark", PipelineError("没有可打标识的成片"))
    stage_start = time.monotonic()
    try:
        report = _read_json(final_dir / "mark.json")
        if _mark_cache_hit(run, report, src=mark_src):
            run.record(
                "mark",
                "cached",
                artifacts=[final_dir / "mark.json", marked_video],
                summary={"verified": True},
            )
        else:
            mark = run_mark(MarkOptions(src=mark_src, out=final_dir))
            marked_video = mark.video_path or marked_video
            verification = section(mark.report, "verification")
            explicit = section(verification, "explicit_label").get("ok")
            implicit = section(verification, "metadata_roundtrip").get("ok")
            run.record(
                "mark",
                "ok" if mark.exit_code == 0 else "partial",
                wall=time.monotonic() - stage_start,
                artifacts=[mark.report_path, marked_video],
                summary={"explicit": explicit, "implicit": implicit},
            )
            if mark.exit_code != 0:
                run.status = "partial"
                run.note("AIGC 标识核验未通过，禁止发布")
        run.outputs["final_video"] = str(marked_video.resolve())
    except (MarkError, OSError) as exc:
        return run.fail("mark", exc)
    return True


_STEPS: tuple[_Step, ...] = (
    _Step("probe", _stage_probe),
    _Step("asr", _stage_asr),
    _Step("diarize", _stage_diarize),
    _Step("merge", _stage_merge),
    # 合并换了时间轴 → 说话人映射要按新字幕重建（是衔接钩子，不受 --stop-after 控制）
    _Step(None, _remap_speakers_after_merge),
    # 翻译前滤掉音乐/噪声糊出来的 cue（结论回填到 merge 的摘要上）
    _Step(None, _clean_degenerate_cues),
    _Step("translate", _stage_translate),
    _Step("build", _stage_build),
    _Step("dub", _stage_dub),
    _Step("burn", _stage_burn),
    _Step("mark", _stage_mark),
)


def _map_language(detected: str) -> str:
    """faster-whisper 语言码 → 本工具语言 profile（只支持 zh / en）。"""

    code = (detected or "").lower()
    if code.startswith("zh") or code in {"yue", "cmn"}:
        return "zh"
    if code.startswith("en"):
        return "en"
    raise PipelineError(f"ASR 识别到的语言是 {detected!r}，本工具目前只支持 zh / en")


def _resolve_provider(options: PipelineOptions) -> str:
    if options.provider:
        return options.provider
    return "openai" if options.llm else "microsoft"


def _tts_rate(options: PipelineOptions) -> str | None:
    """实际使用的基础语速：显式 ``--tts-rate`` 优先，否则按目标语言默认。"""

    if options.tts_rate is not None:
        return options.tts_rate
    return DEFAULT_TTS_RATE.get(options.tgt_lang)


def _dub_capacity_cps(options: PipelineOptions) -> float | None:
    """配音槽位每秒能念多少字（含提语速 + atempo 余量），给 translate 当精简预算。"""

    return speech_capacity_cps(options.tgt_lang, _tts_rate(options))


def _needs_diarize(options: PipelineOptions) -> bool:
    """给了 --speaker-voices / --speakers 也算开启（少敲一个开关）。"""

    return options.diarize or bool(options.speaker_voices) or options.speakers > 0


class _BackgroundJob:
    """极简后台任务（OPT-5）：daemon 线程跑一个函数，需要时 join 取结果/异常。

    为什么不用 ThreadPoolExecutor：pipeline 有很多早退分支（阶段返回 False 就地收尾），
    线程池不 shutdown 会拖住进程退出；daemon 线程没这个问题。
    """

    def __init__(self, function: Callable[[], object]) -> None:
        self._result: object | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(function,), daemon=True)
        self._thread.start()

    def _run(self, function: Callable[[], object]) -> None:
        try:
            self._result = function()
        except BaseException as exc:  # noqa: BLE001 - 原样抛给等待方
            self._error = exc

    def result(self) -> object:
        self._thread.join()
        if self._error is not None:
            raise self._error
        return self._result


def _segments_matches(
    payload: dict[str, object], options: PipelineOptions, source: Path, language: str
) -> bool:
    """``segments.json`` 是否可复用：源文件 sha + 聚类参数/语言/模型都没变。"""

    if not payload:
        return False
    recorded = section(payload, "source").get("sha256")
    if not recorded or str(recorded) != _sha256(source):
        return False
    config = payload.get("config")
    if not isinstance(config, dict):
        return False
    return _diarize_config_matches({"config": config}, options, language)


def _segments_base_report(payload: dict[str, object]) -> dict[str, object]:
    """把 ``segments.json`` 还原成一份"还没有字幕归属"的 diarize 报告底座。"""

    source_block = section(payload, "source")
    base: dict[str, object] = {
        # 这两段原样带过去：它们本来就没有读取方（只在下游被 section() 读取），
        # 保持"坏值照拷"比在这里悄悄改写成 {} 更好追。
        "config": payload.get("config") or {},
        "summary": payload.get("summary") or {},
        "warnings": payload.get("warnings") or [],
        "input": {
            "path": source_block.get("path"),
            "bytes": source_block.get("bytes"),
            "sha256": source_block.get("sha256"),
            "duration_s": payload.get("duration_s"),
        },
    }
    return base


def _diarize_options(
    options: PipelineOptions,
    *,
    source: Path,
    out_dir: Path,
    srt: Path | None,
    language: str,
) -> DiarizeOptions:
    """一次 diarize 的完整参数：正式分离与后台预取**只差 ``srt``**。

    16 个字段里 12 个直接来自 ``PipelineOptions``；两处各写一遍时，新增一个
    ``--vad-*`` 之类的开关很容易只改到一处，于是"预取跑出来的分段"和"正式分离跑出来的
    分段"参数不同，两边产物对不上还很难看出来。
    """

    return DiarizeOptions(
        src=source,
        out=out_dir,
        srt=srt,
        language=language,
        python=options.diarize_python,
        segmentation_model=options.segmentation_model,
        embedding_model=options.embedding_model,
        vad=options.vad,
        vad_model=options.vad_model,
        vad_threshold=options.vad_threshold,
        clustering=options.clustering,
        merge_alpha=options.merge_alpha,
        num_speakers=options.speakers,
        threshold=options.diarization_threshold,
        # 短簇合并开关：关掉时把两道门槛都归零，聚类后不再丢弃小簇
        min_speaker_speech_s=0.0 if options.keep_minor_speakers else MIN_SPEAKER_SPEECH_S,
        min_speaker_share=0.0 if options.keep_minor_speakers else 0.01,
    )


def _start_diarize_segments(
    options: PipelineOptions, source: Path, diarize_dir: Path, language: str
) -> tuple[list[SpeakerTurn], dict[str, object]] | _BackgroundJob:
    """尽量提前把"说话人分段"算掉：能复用就复用，否则后台跑（与 ASR 并行）。"""

    turns, payload = load_segments(diarize_dir / SEGMENTS_NAME)
    if turns and not options.force and _segments_matches(payload, options, source, language):
        return turns, _segments_base_report(payload)

    def run() -> object:
        # 只跑分段（srt=None）；字幕归属等 ASR 出来再补
        return run_diarize(
            _diarize_options(
                options, source=source, out_dir=diarize_dir, srt=None, language=language
            )
        )

    return _BackgroundJob(run)


def _diarize_threshold(options: PipelineOptions, language: str) -> float:
    """实际生效的聚类阈值：显式参数优先，否则按源语言默认（zh 0.8 / en 0.95）。"""

    if options.diarization_threshold is not None:
        return float(options.diarization_threshold)
    return default_threshold(language)


def _effective_diarize_threshold(
    options: PipelineOptions, stages: Sequence[dict[str, object]]
) -> float:
    """报告里写「实际用了多少」：优先取 diarize 阶段记录（含语言默认值）。"""

    if options.diarization_threshold is not None:
        return float(options.diarization_threshold)
    for entry in stages:
        if entry.get("stage") != "diarize":
            continue
        value = section(entry, "summary").get("threshold")
        if isinstance(value, (int, float)):
            return float(value)
        break
    return default_threshold(None if options.src_lang == "auto" else options.src_lang)


def _diarize_config_matches(
    report: dict, options: PipelineOptions, language: str
) -> bool:
    """diarize 缓存判据：人数/阈值/语言/模型变了必须重跑。

    M16 真实测试踩到的坑：只按输入 sha 判缓存，改 `--speakers 3` 后仍然复用
    「自动聚出 15 人」的旧 `diarize.json`，配音按 15 人分配音色——必须让人数与
    阈值进入缓存判据。
    """

    config = report.get("config")
    if not isinstance(config, dict):
        return False
    requested = int(options.speakers) if options.speakers else None
    if config.get("num_speakers_requested") != requested:
        return False
    try:
        threshold = float(config.get("threshold"))
    except (TypeError, ValueError):
        return False
    if abs(threshold - _diarize_threshold(options, language)) > 1e-9:
        return False
    # 短簇合并参数变了也要重跑（关掉合并会改变人数与音色分派）
    recorded_min = config.get("min_speaker_speech_s", MIN_SPEAKER_SPEECH_S)
    try:
        current_min = 0.0 if options.keep_minor_speakers else MIN_SPEAKER_SPEECH_S
        if abs(float(recorded_min) - current_min) > 1e-9:
            return False
    except (TypeError, ValueError):
        return False
    if str(config.get("language") or "").lower() != str(language or "").lower():
        return False
    # VAD 预过滤（OPT-6）：开关/阈值变了分段会不同。老缓存没有 vad 字段（OPT-6 之前
    # 的产物）按"未记录"放过，不因此强制重跑；新产物一定带该字段。
    vad_config = config.get("vad")
    recorded_vad_enabled = False
    if isinstance(vad_config, dict):
        requested = bool(vad_config.get("requested"))
        if requested != bool(options.vad or options.vad_model):
            return False
        recorded_vad_enabled = bool(vad_config.get("enabled"))
        try:
            recorded_vad_threshold = float(vad_config.get("threshold", DEFAULT_VAD_THRESHOLD))
        except (TypeError, ValueError):
            return False
        if abs(recorded_vad_threshold - float(options.vad_threshold)) > 1e-9:
            return False
        if options.vad_model and vad_config.get("model"):
            try:
                same_vad = Path(str(vad_config["model"])).resolve() == Path(
                    str(options.vad_model)
                ).resolve()
            except OSError:
                same_vad = Path(str(vad_config["model"])).name == Path(str(options.vad_model)).name
            if not same_vad:
                return False
    # 二次聚类（OPT-7）：方式/阈值变了人数会变，必须重跑。
    # 老缓存没有 clustering 字段 = OPT-7 之前跑的（等价 sherpa），自动判人数时应重跑。
    effective_clustering = (
        "embedding"
        if options.clustering == "auto" and not options.speakers
        else ("sherpa" if options.clustering == "auto" else options.clustering)
    )
    if str(config.get("clustering_effective") or "sherpa") != effective_clustering:
        return False
    expected_alpha = (
        float(options.merge_alpha)
        if options.merge_alpha is not None
        else (DEFAULT_MERGE_ALPHA_COMPACT if recorded_vad_enabled else DEFAULT_MERGE_ALPHA_NORMAL)
    )
    try:
        recorded_alpha = float(config.get("merge_alpha", expected_alpha))
    except (TypeError, ValueError):
        return False
    if abs(recorded_alpha - expected_alpha) > 1e-9:
        return False
    for key, override in (
        ("segmentation_model", options.segmentation_model),
        ("embedding_model", options.embedding_model),
    ):
        if not override:
            continue
        recorded = config.get(key)
        if not recorded:
            return False
        try:
            same = Path(str(recorded)).resolve() == Path(str(override)).resolve()
        except OSError:
            same = Path(str(recorded)).name == Path(str(override)).name
        if not same:
            return False
    return True


def _remap_speaker_assignments(
    source_srt: Path,
    target_srt: Path,
    speakers: Sequence[str],
    out_path: Path,
) -> Path | None:
    """把「源字幕条 → 说话人」搬到合并后的时间轴上（按时间重叠投票 + 孤岛平滑）。"""

    source_subtitle, _encoding = load_srt(source_srt)
    target_subtitle, _encoding = load_srt(target_srt)
    if not len(target_subtitle) or len(speakers) < len(source_subtitle.cues):
        return None
    turns = [
        SpeakerTurn(cue.timing.start_ms, cue.timing.end_ms, speaker)
        for cue, speaker in zip(source_subtitle.cues, speakers)
        if speaker
    ]
    if not turns:
        return None
    timings = [(cue.timing.start_ms, cue.timing.end_ms) for cue in target_subtitle.cues]
    assignments = smooth_speakers(assign_speakers(timings, turns), timings)
    payload = {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "srt": str(target_srt.resolve()),
        "speakers": sorted({speaker for speaker in assignments if speaker}),
        "cues": [
            {"cue": index, "start_ms": timing[0], "end_ms": timing[1], "speaker": speaker}
            for index, (timing, speaker) in enumerate(zip(timings, assignments), start=1)
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(out_path, payload)
    return out_path


def _speakers_cache_matches(report: dict[str, object], speakers_path: Path | None) -> bool:
    """配音缓存判据：说话人归属文件变了（重跑了分离）就必须重配。"""

    recorded = section(report, "input").get("speakers")
    if speakers_path is None:
        return recorded is None
    if not isinstance(recorded, dict):
        return False
    return bool(recorded.get("sha256")) and recorded.get("sha256") == _sha256(speakers_path)


def _local_tts_fingerprint(options: "PipelineOptions") -> str | None:
    """本地 TTS 配置指纹（模型名 + 类型 + 音色 + 目标语言），进 dub 缓存判据。"""

    try:
        from . import localtts

        model = localtts.find_local_tts_model(options.local_tts_model)
        if model is None:
            return None
        kind = (
            options.local_tts_kind
            if options.local_tts_kind != "auto"
            else localtts.detect_local_tts_kind(model)
        )
    except Exception:  # noqa: BLE001 - 配置不可用不应让缓存判据崩
        return None
    return f"{model.name}:{kind}:{options.local_tts_voice}:{options.tgt_lang}"


def _dub_tts_cache_matches(
    report: dict[str, object],
    *,
    voice: str,
    rate: str | None,
    voice_map: dict[str, str],
    tts_command: str | None,
    tempo_limit: float = 1.4,
    adaptive_rate: bool = True,
    tts_provider: str = "edge",
    tts_fallback: str = "local",
    local_fingerprint: str | None = None,
) -> bool:
    """配音缓存判据：换音色 / 换 TTS 命令 / 换语速都必须重新配音。

    否则多人声里改了 ``--speaker-voices`` 或把 Edge-TTS 换成云端复刻，pipeline 会认为
    "字幕没变 → 配音没变"，直接复用旧音频（听感上就是"换了音色没生效"，还会白省掉
    本该发生的合成调用）。同理 ``--tempo-limit`` / ``--no-adaptive-rate`` 改了也要重配，
    否则旧报告里的时长处理方式会被原样复用。
    """

    config = report.get("config") or {}
    if not isinstance(config, dict):
        return False
    if config.get("voice") != voice:
        return False
    if config.get("tts_rate") != rate:
        return False
    if dict(config.get("voice_by_speaker") or {}) != dict(voice_map):
        return False
    recorded_tempo = config.get("tempo_limit", 1.4)
    try:
        if abs(float(recorded_tempo) - float(tempo_limit)) > 1e-9:
            return False
    except (TypeError, ValueError):
        return False
    if bool(config.get("adaptive_rate", True)) != bool(adaptive_rate):
        return False
    # 本地 TTS（OPT-11）：主渠道/兜底开关变了必须重跑；老报告没有该字段时按"edge+local 兜底"算
    if str(config.get("tts_provider") or "edge") != tts_provider:
        return False
    if str(config.get("tts_fallback") or "local") != tts_fallback:
        return False
    recorded_fingerprint = section(config, "local_tts").get("fingerprint")
    if (
        recorded_fingerprint is not None
        and local_fingerprint is not None
        and str(recorded_fingerprint) != local_fingerprint
    ):
        return False
    expected = (
        hashlib.sha256(tts_command.encode("utf-8")).hexdigest() if tts_command else None
    )
    return config.get("tts_command_sha256") == expected


def _diarize_summary(report: dict[str, object]) -> dict[str, object]:
    summary = section(report, "summary")
    stats = summary.get("speaker_stats") or []
    merged = summary.get("merged_clusters")
    config = section(report, "config")
    return {
        "speakers": summary.get("speakers"),
        "speakers_raw": summary.get("speakers_raw"),
        "merged_clusters": len(merged) if isinstance(merged, Sized) else 0,
        "threshold": config.get("threshold"),
        "vad": summary.get("vad"),
        "clustering": summary.get("clustering"),
        "turns": summary.get("turns"),
        "speech_s": summary.get("speech_s"),
        "cues": summary.get("cues"),
        "rtf": summary.get("rtf"),
        "speaker_stats": [
            {
                "speaker": item.get("speaker"),
                "cues": item.get("cues"),
                "speech_s": item.get("speech_s"),
            }
            for item in stats
            if isinstance(item, dict)
        ],
    }


def _merge_summary(report: dict[str, object]) -> dict[str, object]:
    summary = section(report, "summary")
    return {
        "before": summary.get("before"),
        "after": summary.get("after"),
        "merged_away": summary.get("merged_away"),
        "speakers": summary.get("speakers"),
    }


def _burn_summary(report: dict[str, object]) -> dict[str, object]:
    verification = section(report, "verification")
    output = section(report, "output")
    config = section(report, "config")
    subtitle = section(section(report, "input"), "subtitle")
    return {
        "cues": subtitle.get("cues"),
        "position": config.get("position"),
        "verified": verification.get("ok"),
        "pixel_diff": verification.get("region_pixel_diff"),
        "duration_delta_ms": output.get("duration_delta_ms"),
    }


def _matrix_summary(report: dict[str, object]) -> dict[str, object]:
    layout = section(report, "layout")
    summary = section(report, "summary")
    return {
        "cues": summary.get("cues"),
        "warnings": summary.get("warnings"),
        "cues_before": layout.get("cues_before"),
        "cues_after": layout.get("cues_after"),
        "layout_enabled": layout.get("enabled"),
    }


def _dub_summary(report: dict[str, object]) -> dict[str, object]:
    summary = section(report, "summary")
    bgm = section(summary, "bgm")
    return {
        "cues": summary.get("cues"),
        "synthesized": summary.get("synthesized"),
        "failed": summary.get("failed"),
        "manual": summary.get("manual"),
        "atempo": summary.get("atempo"),
        "absorbed": summary.get("absorbed"),
        "rate_escalated": summary.get("rate_escalated"),
        "played_overflow_over_10pct": summary.get("played_overflow_over_10pct"),
        "bgm_mode": bgm.get("mode"),
        "bgm_separator": bgm.get("separator"),
        "speakers": summary.get("speakers"),
        "tts_fallback": summary.get("tts_fallback"),
        "local_tts": summary.get("local_tts"),
    }
