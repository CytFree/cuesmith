"""命令行入口：``python -m submatrix build|probe|asr|translate|merge|diarize|dub|burn|mark|pipeline|cost|gui ...``

退出码契约（给脚本/CI 用）：
- ``0`` 通过（允许只有 warning；probe 的"需要 ASR"也算成功）
- ``1`` 用法错误、文件读写失败、SRT/ffprobe 解析失败、翻译/ASR 环境不可用
- ``2`` 内容判定不通过（字幕结构校验失败 / 图形字幕不支持 / 没识别出语音）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .asr import AsrError, AsrOptions, AsrResult, run_asr
from .burn import PRESETS, BurnError, BurnOptions, BurnResult, run_burn
from .cost import (
    CostError,
    build_cost,
    format_cost_line,
    load_pricing,
    render_cost,
    reprice,
)
from .diarize import (
    DiarizeError,
    DiarizeOptions,
    DiarizeResult,
    build_speaker_voice_map,
    parse_speaker_voices,
    run_diarize,
)
from .dub import DubError, DubOptions, DubResult, run_dub
from .glossary import Glossary, GlossaryError
from .history import (
    HISTORY_HEADERS,
    STATUS_LABELS,
    HistoryError,
    HistoryStore,
    history_rows,
)
from .mark import (
    POSITIONS,
    SELF_USE_NOTICE,
    MarkError,
    MarkOptions,
    MarkResult,
    run_mark,
)
from .matrix import MatrixOptions, MatrixResult, run_matrix
from .merge import MergeError, MergeOptions, MergeResult, run_merge
from .pipeline import (
    STAGES,
    PipelineError,
    PipelineOptions,
    PipelineResult,
    run_pipeline,
)
from .probe import ProbeError, ProbeOptions, ProbeResult, run_probe
from .srt import SrtParseError
from .translate import (
    LlmConfig,
    TranslateError,
    TranslateOptions,
    TranslateResult,
    run_translate,
)
from .validate import LANG_PROFILES, Limits

__all__ = ["main"]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2

_LANGUAGES = sorted(LANG_PROFILES)


class UsageError(Exception):
    """参数错误：交给 main 统一转成退出码 1，而不是让 argparse 抛 SystemExit。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def build_parser() -> _Parser:
    """构建 CLI 解析器：每个子命令的参数在各自的 ``_add_*_args`` 里。

    这里只负责三件事：建根解析器、挂子命令分发、按顺序把每个子命令的参数表接上来。
    """

    parser = _Parser(
        prog="submatrix",
        description=(
            "视频字幕工具（FR-1 探测与内封字幕直出 / FR-2 字幕文件矩阵 + CPS 校验"
            " / FR-3 配音编排 / FR-4 AIGC 标识）"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    _add_build_args(subparsers)
    _add_probe_args(subparsers)
    _add_mark_args(subparsers)
    _add_asr_args(subparsers)
    _add_translate_args(subparsers)
    _add_merge_args(subparsers)
    _add_diarize_args(subparsers)
    _add_dub_args(subparsers)
    _add_burn_args(subparsers)
    _add_pipeline_args(subparsers)
    _add_cost_args(subparsers)
    _add_gui_args(subparsers)
    _add_history_args(subparsers)

    return parser


def _add_build_args(subparsers: argparse._SubParsersAction) -> None:
    """``build``：校验一对字幕并导出单语/双语 SRT、ASS、VTT、TXT（只报不改）。"""

    build = subparsers.add_parser(
        "build",
        help="校验一对字幕并导出 单语SRT / 双语SRT / ASS / VTT / TXT",
        description="校验一对字幕并导出 5 种格式，最后写 report.json。只报不改。",
    )
    build.add_argument("--src", required=True, type=Path, help="源语言 SRT（一期为中文）")
    build.add_argument("--tgt", required=True, type=Path, help="目标语言 SRT（一期为英文）")
    build.add_argument("--out", required=True, type=Path, help="输出目录（不存在会自动创建）")
    build.add_argument(
        "--src-lang", default="zh", choices=_LANGUAGES, help="源语言（默认 zh）"
    )
    build.add_argument(
        "--tgt-lang", default="en", choices=_LANGUAGES, help="目标语言（默认 en）"
    )
    build.add_argument("--cps-src", type=float, default=None, help="源语言 CPS 上限")
    build.add_argument("--cps-tgt", type=float, default=None, help="目标语言 CPS 上限")
    build.add_argument("--width-src", type=int, default=None, help="源语言单行字符上限")
    build.add_argument("--width-tgt", type=int, default=None, help="目标语言单行字符上限")
    build.add_argument("--max-lines", type=int, default=2, help="单条最多行数（默认 2）")
    build.add_argument(
        "--wrap",
        action="store_true",
        help="开启排版：自动折行 + 超过行数就切条（默认只报不改）",
    )


def _add_probe_args(subparsers: argparse._SubParsersAction) -> None:
    """``probe``：探测视频流与字幕轨，内封文本字幕直出 ``source.srt``。"""

    probe = subparsers.add_parser(
        "probe",
        help="探测视频的流与字幕轨，直出内封字幕 SRT（或判定走 ASR）",
        description=(
            "探测视频流；文本字幕轨直接抽成 source.srt，图形字幕轨明确报错，"
            "无字幕则给出『需要 ASR』路由结论。全过程写入 manifest.json。"
        ),
    )
    probe.add_argument("--src", required=True, type=Path, help="视频文件（mkv/mp4/...）")
    probe.add_argument("--out", required=True, type=Path, help="输出目录（不存在会自动创建）")
    probe.add_argument("--src-lang", default="zh", choices=_LANGUAGES, help="期望的字幕语言（默认 zh）")
    probe.add_argument(
        "--no-extract",
        action="store_true",
        help="只探测与判定，不抽字幕（大文件先看路由结论时用）",
    )


def _add_mark_args(subparsers: argparse._SubParsersAction) -> None:
    """``mark``：AIGC 标识——起始画面显式标识 + 文件元数据隐式标识。"""

    mark = subparsers.add_parser(
        "mark",
        help="给视频打 AIGC 标识：起始画面显式标识 + 文件元数据隐式标识",
        description=(
            "按《人工智能生成合成内容标识办法》给成品视频打标：起始画面烧显著文字标识（默认右上角"
            "「AI 生成」、前 3 秒），同时写入 comment/artist/copyright/content_id 元数据，并自动核验。"
            "标识默认开启；除 --self-use（仅自用、写留痕日志）外没有移除途径。"
        ),
    )
    mark.add_argument("--src", required=True, type=Path, help="成品视频（mp4/mkv 等）")
    mark.add_argument("--out", required=True, type=Path, help="输出目录（不存在会自动创建）")
    mark.add_argument("--label-text", default=None, help="显式标识文案（默认「AI 生成」）")
    mark.add_argument(
        "--label-seconds", type=float, default=None, help="显式标识时长（秒，不得短于 3）"
    )
    mark.add_argument(
        "--position", default=None, choices=POSITIONS, help="标识位置（默认 top-right）"
    )
    mark.add_argument("--provider", default=None, help="服务提供者名称（写入 artist/copyright）")
    mark.add_argument("--comment", default=None, help="生成合成属性说明（写入 comment）")
    mark.add_argument("--copyright", dest="copyright_text", default=None, help="版权/提供者信息")
    mark.add_argument("--font-file", type=Path, default=None, help="指定中文字体文件 .ttf/.ttc")
    mark.add_argument(
        "--self-use",
        action="store_true",
        help="仅自用模式：不打标、写留痕日志（不得公开发布）",
    )


def _add_asr_args(subparsers: argparse._SubParsersAction) -> None:
    """``asr``：语音识别（词级时间戳 + 热词偏置 + 按语义切条）。"""

    asr = subparsers.add_parser(
        "asr",
        help="语音识别：词级时间戳 + 热词偏置 + 按语义切条",
        description=(
            "调用底座 venv 里的 faster-whisper 做识别（--asr-python 指定解释器），拿到"
            "词级时间戳后按句末标点/长停顿/超长切条，产出 source.srt + words.json + asr.json。"
            "专有名词用 --hotwords / --hotwords-file 做偏置。"
        ),
    )
    asr.add_argument("--src", required=True, type=Path, help="音视频文件（mp4/mkv/wav/...）")
    asr.add_argument("--out", required=True, type=Path, help="输出目录（不存在会自动创建）")
    asr.add_argument("--language", default="auto", help="语言代码，如 en / zh；默认自动识别")
    asr.add_argument("--model", default=None, help="faster-whisper 模型目录或名字（默认按源语言：中文用 medium，其余 small）")
    asr.add_argument(
        "--asr-python", default=None, help="装有 faster-whisper 的 Python（默认找底座 venv）"
    )
    asr.add_argument("--hotwords", default=None, help="热词，逗号分隔（产品名/人名/术语）")
    asr.add_argument("--hotwords-file", type=Path, default=None, help="热词文件或术语表（一行一个）")
    asr.add_argument("--initial-prompt", default=None, help="初始提示词（风格/上下文引导）")
    asr.add_argument("--beam-size", type=int, default=5, help="beam size（默认 5）")
    asr.add_argument("--compute-type", default="int8", help="计算精度（CPU 默认 int8）")
    asr.add_argument("--device", default="cpu", help="设备（默认 cpu）")
    asr.add_argument("--cpu-threads", type=int, default=0, help="CPU 线程数（默认自动）")
    asr.add_argument("--no-vad", action="store_true", help="关闭 VAD 静音过滤")
    asr.add_argument("--max-duration", type=float, default=7.0, help="单条时长上限（秒）")
    asr.add_argument("--max-chars", type=int, default=200, help="单条字符上限")
    asr.add_argument("--pause", type=float, default=0.5, help="长停顿切条阈值（秒）")
    asr.add_argument(
        "--min-duration", type=float, default=0.8, help="单条最短时长（秒，过短会并入相邻条）"
    )
    asr.add_argument("--keep-raw", action="store_true", help="保留 worker 原始输出（排查用）")


def _add_translate_args(subparsers: argparse._SubParsersAction) -> None:
    """``translate``：翻译编排（术语表 + 段落上下文 + 译后精简 + CPS 报告）。"""

    translate = subparsers.add_parser(
        "translate",
        help="翻译编排：术语表 + 段落上下文 + 译后精简 + CPS 报告",
        description=(
            "把 SRT 译成目标语言。默认走微软免费端点（无需 key），也可换 OpenAI 兼容 LLM 或"
            "外部命令。术语表做占位保护（禁译/统一译名），段落上下文提升代词与省略的准确度，"
            "译文超过目标 CPS 时默认自动精简（--no-condense 关闭）。质量指标写入 translate.json。"
        ),
    )
    translate.add_argument("--src", required=True, type=Path, help="源语言 SRT")
    translate.add_argument("--out", required=True, type=Path, help="输出目录")
    translate.add_argument("--src-lang", default="en", choices=_LANGUAGES, help="源语言（默认 en）")
    translate.add_argument("--tgt-lang", default="zh", choices=_LANGUAGES, help="目标语言（默认 zh）")
    translate.add_argument("--out-name", default=None, help="输出文件名（默认 <tgt-lang>.srt）")
    translate.add_argument(
        "--provider",
        default=None,
        choices=["microsoft", "openai", "command", "echo"],
        help="翻译渠道（默认 microsoft；echo 仅用于离线管线测试）",
    )
    translate.add_argument("--command-template", default=None, help="command 渠道的命令模板")
    translate.add_argument(
        "--context",
        default="paragraph",
        choices=["paragraph", "sentence", "off"],
        help="上下文粒度：paragraph 整段翻译（默认）/ sentence 整句 / off 逐条",
    )
    translate.add_argument("--batch-size", type=int, default=20, help="每次请求的条数上限")
    translate.add_argument(
        "--concurrency", type=int, default=4, help="分块并发数（默认 4；免费 MT 别开太大，会被限流）"
    )
    translate.add_argument("--retries", type=int, default=3, help="请求失败重试次数")
    translate.add_argument("--timeout", type=float, default=120.0, help="单次请求超时（秒）")
    translate.add_argument("--glossary", type=Path, default=None, help="术语表 .json/.tsv/.txt")
    translate.add_argument(
        "--glossary-reverse",
        action="store_true",
        help="反向使用术语表（zh→en 必开：把'英文→中文'翻成'中文→英文'）",
    )
    translate.add_argument(
        "--condense",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="目标 CPS 超限时精简译文（默认开启；--no-condense 关闭，原样保留）",
    )
    translate.add_argument(
        "--condense-mode",
        default="auto",
        choices=["auto", "rule", "trim", "llm"],
        help="精简策略：rule 只删口头禅 / trim 截分句 / llm 重写 / auto 有 LLM 用 LLM（默认）",
    )
    translate.add_argument("--target-cps", type=float, default=None, help="目标 CPS（默认按语言规范）")
    translate.add_argument(
        "--llm-review",
        action="store_true",
        help="LLM 审校：只改明显错误，逐段比对并报告改动条数（token 约翻倍，精确交付建议开）",
    )
    translate.add_argument(
        "--llm-review-passes",
        type=int,
        default=2,
        help="审校轮数（默认 2：上一轮成品进下一轮，能拦住漏网误译；一轮没改动会提前停）",
    )
    translate.add_argument("--llm-base-url", default=None, help="LLM 端点（OpenAI 兼容）")
    translate.add_argument("--llm-api-key", default=None, help="LLM key（建议用环境变量）")
    translate.add_argument("--llm-model", default=None, help="LLM 模型名")
    translate.add_argument(
        "--fallback-provider",
        default="auto",
        choices=["auto", "off"],
        help="渠道回退（默认 auto）：免费 MT 网络失败自动切备用渠道（已配置的 LLM/命令模板）；off 只重试不换渠道",
    )


def _add_merge_args(subparsers: argparse._SubParsersAction) -> None:
    """``merge``：把 ASR 的碎片 cue 合并成句子级 cue（配音前必做）。"""

    merge = subparsers.add_parser(
        "merge",
        help="ASR 碎片句级合并（配音前必做）",
        description=(
            "把 Whisper 产出的 2–3 秒碎片 cue 合并成句子级 cue：按句末标点切句、"
            "单条上限 12 秒、相邻间隔 ≤1 秒。不合并会让配音译文撑爆槽位。"
        ),
    )
    merge.add_argument("--src", required=True, type=Path, help="ASR 产出的 SRT")
    merge.add_argument("--out", required=True, type=Path, help="输出目录")
    merge.add_argument("--max-duration", type=float, default=12.0, help="单条时长上限（秒）")
    merge.add_argument("--max-gap", type=float, default=1.0, help="相邻碎片最大间隔（秒）")


def _add_diarize_args(subparsers: argparse._SubParsersAction) -> None:
    """``diarize``：说话人分离（多人声），产出说话人分段与字幕归属。"""

    diarize = subparsers.add_parser(
        "diarize",
        help="说话人分离（多人声）：识别谁在什么时候说话",
        description=(
            "用 sherpa-onnx 离线说话人分离（pyannote 分割 + 3D-Speaker/NeMo 声纹，CPU）"
            "识别片中的说话人分段；给了 --srt 时还会把每条字幕归到对应说话人，产出"
            " speakers.json 供 dub 分派音色。模型只跑在进程边界，主包不依赖 torch。"
        ),
    )
    diarize.add_argument("--src", required=True, type=Path, help="音/视频文件")
    diarize.add_argument("--out", required=True, type=Path, help="输出目录")
    diarize.add_argument("--srt", type=Path, default=None, help="可选：字幕，顺带做条→说话人归属")
    diarize.add_argument(
        "--src-lang", default=None, choices=_LANGUAGES, help="源语言（zh/en，决定默认声纹模型）"
    )
    diarize.add_argument("--python", default=None, help="装有 sherpa-onnx 的 Python")
    diarize.add_argument("--segmentation-model", default=None, help="pyannote 分割模型 .onnx")
    diarize.add_argument("--embedding-model", default=None, help="声纹模型 .onnx")
    diarize.add_argument(
        "--vad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="VAD 预过滤：音乐/掌声/环境声段不进聚类（默认开；需 models/vad/silero_vad.onnx）",
    )
    diarize.add_argument("--vad-model", default=None, help="silero VAD 模型 .onnx（给了就启用）")
    diarize.add_argument(
        "--vad-threshold", type=float, default=0.5, help="VAD 阈值（默认 0.5，越大越严格）"
    )
    diarize.add_argument(
        "--clustering",
        choices=["auto", "sherpa", "embedding"],
        default="auto",
        help="聚类方式：auto=自动判人数时用声纹二次聚类（只并拆）、显式人数用 sherpa",
    )
    diarize.add_argument(
        "--merge-alpha",
        type=float,
        default=None,
        help="二次聚类阈值系数（默认按输入模式：原始音频 0.55 / VAD 拼接音频 0.70）",
    )
    diarize.add_argument("--speakers", type=int, default=0, help="已知说话人数（0 = 自动判断）")
    diarize.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="自动聚类阈值（越大说话人越少；默认中文 0.8、英文 0.95）",
    )
    diarize.add_argument(
        "--keep-minor-speakers",
        action="store_true",
        help="不做短簇合并：保留每一个原始簇（短发言者如广告人声不会被并进主簇）",
    )
    diarize.add_argument("--min-duration-on", type=float, default=0.3)
    diarize.add_argument("--min-duration-off", type=float, default=0.5)
    diarize.add_argument("--threads", type=int, default=0, help="推理线程数（0 = 1）")


def _add_dub_args(subparsers: argparse._SubParsersAction) -> None:
    """``dub``：配音编排（并发合成 + 重试 + 时长校验 + 溢出吸收 + 可选 BGM）。"""

    dub = subparsers.add_parser(
        "dub",
        help="配音编排：并发合成 + 重试 + 时长校验 + 溢出吸收",
        description=(
            "逐条调用 TTS 合成配音（默认 Edge-TTS，可换外部命令模板），并发执行、单条失败重试、"
            "按槽位铺轨、放不下的条目先提语速重合成、再 atempo ≤1.4x 兜底，"
            "产出 dubbed.wav / 可选 dubbed.mp4 与 dub_report.json。"
        ),
    )
    dub.add_argument("--srt", required=True, type=Path, help="译文字幕（时间轴即配音槽位）")
    dub.add_argument("--out", required=True, type=Path, help="输出目录")
    dub.add_argument("--video", type=Path, default=None, help="可选：把配音并进这个视频")
    dub.add_argument("--voice", default=None, help="音色（默认 en-US-AriaNeural）")
    dub.add_argument(
        "--speakers-json",
        type=Path,
        default=None,
        help="说话人归属文件（diarize 的 speakers.json）：按说话人分派多音色",
    )
    dub.add_argument(
        "--speaker-voices",
        default=None,
        help="多人声色：SPEAKER_00=音色,SPEAKER_01=音色 或 音色池（音色1,音色2，按出场顺序分配）",
    )
    dub.add_argument(
        "--tts-rate", default=None, help="TTS 语速，如 +30%%（Edge-TTS 语法，默认原速）"
    )
    dub.add_argument("--concurrency", type=int, default=6, help="并发数（默认 6）")
    dub.add_argument("--retries", type=int, default=2, help="单条失败重试次数（默认 2）")
    dub.add_argument(
        "--tempo-limit", type=float, default=1.4, help="atempo 压缩上限（默认 1.4）"
    )
    dub.add_argument(
        "--adaptive-rate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="放不下的条目提语速重合成（默认开；--no-adaptive-rate 关掉、只靠 atempo）",
    )
    dub.add_argument(
        "--tts-command",
        default=None,
        help="外部 TTS 命令模板（{text_file} / {out} / {voice_id}；阿里云示例见 tools/tts_dashscope.py）",
    )
    dub.add_argument(
        "--tts-provider",
        choices=["edge", "local"],
        default="edge",
        help="TTS 主渠道：edge=免费在线 Edge-TTS（默认）；local=本地 sherpa-onnx（离线，需模型）",
    )
    dub.add_argument(
        "--tts-fallback",
        choices=["local", "off"],
        default="local",
        help="Edge 失败时是否用本地 TTS 兜底（默认 local；off=只重试）",
    )
    dub.add_argument(
        "--local-tts-model", default=None, help="本地 TTS 模型目录（默认自动找 spike/local-tts/*）"
    )
    dub.add_argument(
        "--local-tts-kind", choices=["auto", "kokoro", "vits"], default="auto", help="本地 TTS 类型"
    )
    dub.add_argument(
        "--local-tts-voice",
        default="",
        help="本地音色：kokoro 用 47/kokoro:47/名字，vits 用 0；空=按目标语言默认（zh→47/en→3）",
    )
    dub.add_argument("--local-tts-python", default=None, help="装有 sherpa-onnx 的 Python")
    dub.add_argument("--local-tts-threads", type=int, default=8, help="本地 TTS 推理线程（默认 8）")
    dub.add_argument(
        "--local-tts-speed",
        type=float,
        default=None,
        help="本地 TTS 基础倍速（默认 1.0，再叠加 --tts-rate；0.5–2.0）",
    )
    dub.add_argument(
        "--tgt-lang",
        choices=["zh", "en"],
        default="zh",
        help="目标语言（只用于挑本地 TTS 默认音色；默认 zh）",
    )
    dub.add_argument(
        "--tts-python",
        default=None,
        help="装有 edge-tts 的 Python 解释器（默认自动探测：当前解释器 → spike/.venv311 → pyvideotrans venv）",
    )
    dub.add_argument(
        "--regenerate-tts",
        action="store_true",
        help="忽略内容指纹缓存、强制重新合成（云端 TTS 会重新计费；默认复用已合成的音频）",
    )
    dub.add_argument(
        "--keep-bgm",
        action="store_true",
        help="保留原片背景音乐：拆人声 → 伴奏随配音自动让路 → 混进成片（收费软件同款）",
    )
    dub.add_argument(
        "--bgm-mode",
        default="auto",
        choices=["auto", "separate", "center", "duck"],
        help="BGM 策略：auto 有 demucs 用 demucs 否则相位抵消 / separate 必须 demucs / "
        "center 相位抵消 / duck 原声压低当背景",
    )
    dub.add_argument("--bgm-gain", type=float, default=-2.0, help="伴奏增益 dB（默认 -2）")
    dub.add_argument("--bgm-duck", type=float, default=-12.0, help="配音时伴奏压低 dB（默认 -12）")
    dub.add_argument(
        "--bgm-source", type=Path, default=None, help="取哪条音轨做伴奏（默认 --video 的音轨）"
    )
    dub.add_argument(
        "--separator-python", default=None, help="装有 demucs 的 Python（默认找 spike/demucs 环境）"
    )


def _add_burn_args(subparsers: argparse._SubParsersAction) -> None:
    """``burn``：把字幕烧进画面，出带硬字幕的成片。"""

    burn = subparsers.add_parser(
        "burn",
        help="把字幕烧进视频（出带硬字幕的成片）",
        description=(
            "用 ffmpeg 把 .ass/.srt 字幕烧进画面，输出可直接上传的带字幕成片；"
            "烧录后自动比对画面底部像素差核验（字体缺失/滤镜失效会报出来）。"
        ),
    )
    burn.add_argument("--video", required=True, type=Path, help="输入视频（通常先跑 dub）")
    burn.add_argument(
        "--subtitle", required=True, type=Path, help="字幕：.ass（双语/单语）或 .srt（单语）"
    )
    burn.add_argument("--out", required=True, type=Path, help="输出目录")
    burn.add_argument("--crf", type=int, default=18, help="视频质量（默认 18，越小越清晰）")
    burn.add_argument(
        "--preset", default="veryfast", choices=list(PRESETS), help="x264 preset（默认 veryfast）"
    )
    burn.add_argument(
        "--position",
        default="bottom",
        choices=["bottom", "top"],
        help="字幕位置：bottom 底部（默认）/ top 顶部（源片自带硬字幕时用来避让）",
    )
    burn.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="烧录后比对像素差核验（默认开）",
    )


def _add_pipeline_args(subparsers: argparse._SubParsersAction) -> None:
    """``pipeline``：一条命令跑完整链路（本工具的主入口）。"""

    pipeline = subparsers.add_parser(
        "pipeline",
        help="一条命令跑完整链路：探测/ASR →（说话人分离）→ 翻译 → 排版 → 配音 → 硬字幕 → AIGC 标识",
        description=(
            "把 probe/asr/diarize/merge/translate/build/dub/burn/mark 串成一条命令：输入一条视频，"
            "输出发布字幕（单语 + 双语 5 格式）、配音成片、打完标识的成品。"
            "每阶段产物落盘，默认复用已完成阶段（--force 强制重跑）；--stop-after 可只跑到某一阶段。"
        ),
    )
    pipeline.add_argument("--src", required=True, type=Path, help="输入视频（mp4/mkv/webm/...）")
    pipeline.add_argument("--out", required=True, type=Path, help="输出根目录（各阶段子目录）")
    pipeline.add_argument(
        "--src-lang",
        default="auto",
        choices=["auto", *_LANGUAGES],
        help="源语言；auto 用 ASR 识别结果（内封字幕路线必须显式指定）",
    )
    pipeline.add_argument("--tgt-lang", default="zh", choices=_LANGUAGES, help="目标语言（默认 zh）")
    pipeline.add_argument(
        "--llm",
        action="store_true",
        help="LLM 翻译渠道（DeepSeek，需 DEEPSEEK_API_KEY；默认开双轮审校）",
    )
    pipeline.add_argument(
        "--provider",
        default=None,
        choices=["microsoft", "openai", "command", "echo"],
        help="显式翻译渠道（默认 microsoft；--llm 时 openai）",
    )
    pipeline.add_argument("--glossary", type=Path, default=None, help="术语表 .json/.tsv/.txt")
    pipeline.add_argument(
        "--glossary-reverse", action="store_true", help="反向使用术语表（zh→en 必开）"
    )
    pipeline.add_argument(
        "--condense",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="超 CPS 自动精简译文（默认开；--no-condense 关闭）",
    )
    pipeline.add_argument(
        "--condense-mode", default="auto", choices=["auto", "rule", "trim", "llm"]
    )
    pipeline.add_argument(
        "--translate-fallback",
        default="auto",
        choices=["auto", "off"],
        help="渠道回退（默认 auto）：免费 MT 网络失败自动切备用渠道（已配置的 LLM/命令模板）",
    )
    pipeline.add_argument("--asr-python", default=None, help="装有 faster-whisper 的 Python")
    pipeline.add_argument(
        "--asr-model", default=None, help="faster-whisper 模型（默认按源语言：中文 medium）"
    )
    pipeline.add_argument("--hotwords", default=None, help="ASR 热词，逗号分隔")
    pipeline.add_argument("--hotwords-file", type=Path, default=None, help="热词文件（一行一个）")
    pipeline.add_argument("--initial-prompt", default=None, help="ASR 初始提示词")
    pipeline.add_argument(
        "--merge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ASR 后按句合并碎片 cue（默认开；避免半句翻译。内封字幕路线自动跳过）",
    )
    pipeline.add_argument(
        "--merge-max-duration", type=float, default=15.0, help="合并后单条时长上限（秒，默认 15）"
    )
    pipeline.add_argument(
        "--merge-max-gap", type=float, default=1.0, help="可合并的最大间隔（秒，默认 1）"
    )
    pipeline.add_argument(
        "--diarize",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="多人声：先做说话人分离，再按说话人分派不同音色（默认关；给 --speaker-voices 自动开）",
    )
    pipeline.add_argument("--speakers", type=int, default=0, help="已知说话人数（0 = 自动判断）")
    pipeline.add_argument(
        "--diarization-threshold",
        type=float,
        default=None,
        help="说话人聚类阈值（越大说话人越少；默认中文 0.8、英文 0.95）",
    )
    pipeline.add_argument(
        "--keep-minor-speakers",
        action="store_true",
        help="不做短簇合并：保留每一个原始簇（短发言者不会被并进主簇）",
    )
    pipeline.add_argument("--diarize-python", default=None, help="装有 sherpa-onnx 的 Python")
    pipeline.add_argument("--segmentation-model", default=None, help="说话人分割模型 .onnx")
    pipeline.add_argument("--embedding-model", default=None, help="声纹模型 .onnx")
    pipeline.add_argument(
        "--vad",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="VAD 预过滤：音乐/掌声段不进说话人聚类（默认开；需 models/vad/silero_vad.onnx）",
    )
    pipeline.add_argument("--vad-model", default=None, help="silero VAD 模型 .onnx")
    pipeline.add_argument(
        "--vad-threshold", type=float, default=0.5, help="VAD 阈值（默认 0.5，越大越严格）"
    )
    pipeline.add_argument(
        "--clustering",
        choices=["auto", "sherpa", "embedding"],
        default="auto",
        help="说话人聚类方式：auto=自动判人数时用声纹二次聚类（只并拆），显式人数用 sherpa",
    )
    pipeline.add_argument(
        "--merge-alpha",
        type=float,
        default=None,
        help="二次聚类阈值系数（默认按输入模式：原始音频 0.55 / VAD 拼接音频 0.70）",
    )
    pipeline.add_argument(
        "--speaker-voices",
        default=None,
        help="多人声色：SPEAKER_00=音色,SPEAKER_01=音色 或 音色池（音色1,音色2）",
    )
    pipeline.add_argument("--voice", default=None, help="TTS 音色（默认按目标语言）")
    pipeline.add_argument("--tts-rate", default=None, help="TTS 语速（默认按目标语言）")
    pipeline.add_argument(
        "--tts-python",
        default=None,
        help="装有 edge-tts 的 Python（默认自动探测：当前解释器 → spike/.venv311 → pyvideotrans venv）",
    )
    pipeline.add_argument(
        "--tts-command",
        default=None,
        help="外部 TTS 命令模板（{text_file} / {out} / {voice_id}；阿里云示例见 tools/tts_dashscope.py）",
    )
    pipeline.add_argument(
        "--tts-provider",
        choices=["edge", "local"],
        default="edge",
        help="TTS 主渠道：edge=免费在线 Edge-TTS（默认）；local=本地 sherpa-onnx（离线，需模型）",
    )
    pipeline.add_argument(
        "--tts-fallback",
        choices=["local", "off"],
        default="local",
        help="Edge 失败时是否用本地 TTS 兜底（默认 local；off=只重试）",
    )
    pipeline.add_argument("--local-tts-model", default=None, help="本地 TTS 模型目录（默认自动找）")
    pipeline.add_argument(
        "--local-tts-kind", choices=["auto", "kokoro", "vits"], default="auto", help="本地 TTS 类型"
    )
    pipeline.add_argument(
        "--local-tts-voice", default="", help="本地音色（kokoro 47/kokoro:47/名字；vits 0；空=按目标语言）"
    )
    pipeline.add_argument("--local-tts-python", default=None, help="装有 sherpa-onnx 的 Python")
    pipeline.add_argument("--local-tts-threads", type=int, default=8, help="本地 TTS 推理线程（默认 8）")
    pipeline.add_argument(
        "--local-tts-speed", type=float, default=None, help="本地 TTS 基础倍速（0.5–2.0，默认 1.0）"
    )
    pipeline.add_argument(
        "--regenerate-tts",
        action="store_true",
        help="忽略内容指纹缓存、强制重新合成（云端 TTS 会重新计费；默认复用已合成的音频）",
    )
    pipeline.add_argument("--concurrency", type=int, default=10, help="配音并发数（默认 10）")
    pipeline.add_argument(
        "--translate-concurrency",
        type=int,
        default=4,
        help="翻译分块并发（默认 4；免费 MT 别开太大，会被限流）",
    )
    pipeline.add_argument("--tempo-limit", type=float, default=1.4, help="atempo 压缩上限")
    pipeline.add_argument(
        "--adaptive-rate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="放不下的条目提语速重合成（默认开；--no-adaptive-rate 关掉、只靠 atempo）",
    )
    pipeline.add_argument("--keep-bgm", action="store_true", help="保留原片背景音乐")
    pipeline.add_argument(
        "--bgm-mode", default="auto", choices=["auto", "separate", "center", "duck"]
    )
    pipeline.add_argument("--bgm-source", type=Path, default=None)
    pipeline.add_argument("--separator-python", default=None)
    pipeline.add_argument(
        "--burn-subtitles",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="把发布字幕烧进成片（默认开，出带硬字幕的成片；--no-burn-subtitles 只留外挂字幕）",
    )
    pipeline.add_argument(
        "--burn-mode",
        default="target",
        choices=["target", "bilingual"],
        help="烧录哪套字幕：target 只烧译文（默认）/ bilingual 中英双语",
    )
    pipeline.add_argument("--burn-crf", type=int, default=18, help="烧录重编码质量（默认 18）")
    pipeline.add_argument(
        "--burn-position",
        default="bottom",
        choices=["bottom", "top"],
        help="烧录字幕位置：bottom 底部（默认）/ top 顶部（源片自带硬字幕时用来避让）",
    )
    pipeline.add_argument(
        "--mark",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="AIGC 标识（默认开；只有自用素材才考虑 --no-mark）",
    )
    pipeline.add_argument("--force", action="store_true", help="忽略已存在产物，强制重跑所有阶段")
    pipeline.add_argument(
        "--stop-after", default=None, choices=list(STAGES), help="只跑到指定阶段就停"
    )
    pipeline.add_argument(
        "--pricing",
        type=Path,
        default=None,
        help="成本价表 JSON（覆盖内置示例价；深合并）",
    )


def _add_cost_args(subparsers: argparse._SubParsersAction) -> None:
    """``cost``：按价表折算一次 pipeline 的外部服务成本。"""

    cost_parser = subparsers.add_parser(
        "cost",
        help="汇总一次 pipeline 产物的外部服务成本（读 pipeline.json 的 cost 块）",
        description=(
            "产物累计口径：LLM token + TTS 字符按价表折算，分币种小计并给展示金额。"
            "缓存重跑数字不变；加 --pricing 可换价表重算。"
        ),
    )
    cost_parser.add_argument(
        "--work", required=True, type=Path, help="pipeline 输出根目录（含 pipeline.json）"
    )
    cost_parser.add_argument(
        "--pricing", type=Path, default=None, help="价表 JSON（覆盖内置示例价）"
    )
    cost_parser.add_argument(
        "--tts-command",
        default=None,
        help="TTS 命令模板（旧产物没有 cost 块时，用它识别 dashscope 等付费渠道）",
    )
    cost_parser.add_argument("--json", action="store_true", help="输出 cost 块 JSON")


def _add_gui_args(subparsers: argparse._SubParsersAction) -> None:
    """``gui``：启动本地 Web GUI（可选依赖 gradio）。"""

    gui_parser = subparsers.add_parser(
        "gui",
        help="启动本地 Web GUI（Gradio，可选依赖；默认只监听本机）",
        description=(
            "薄壳：表单拼成 pipeline 参数，子进程跑 CLI，实时日志 + 产物下载 + 成本表；"
            "音色下拉/试听、多人声按说话人分配并可重配。"
            "需要 gradio；仓库内可用 spike/gui/.venv（缺依赖时会自动重入）。"
        ),
    )
    gui_parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    gui_parser.add_argument("--port", type=int, default=7860)
    gui_parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    gui_parser.add_argument(
        "--allowed-path",
        action="append",
        default=None,
        help="额外允许预览/下载的目录（可多次；默认仓库目录 + 用户主目录）",
    )


def _add_history_args(subparsers: argparse._SubParsersAction) -> None:
    """``history``：查看/清理 GUI 任务历史（sqlite）。"""

    history_parser = subparsers.add_parser(
        "history",
        help="查看/清理 GUI 任务历史（sqlite，默认 work/history.db）",
        description=(
            "GUI 每次运行都会把源文件/输出目录/参数/状态/成本写进 sqlite；"
            "这里可以不开页面查看，或删掉某条记录。"
        ),
    )
    history_parser.add_argument("--db", type=Path, default=None, help="历史库路径（默认 work/history.db）")
    history_parser.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")
    history_parser.add_argument("--json", action="store_true", help="输出 JSON 数组")
    history_parser.add_argument("--delete", type=int, default=None, metavar="ID", help="删除指定 id 的记录")


def _print_summary(result: MatrixResult) -> None:
    report = result.report
    inputs = report["inputs"]
    summary = report["summary"]

    print(
        f"原文：{inputs['source']['path']}"
        f"（{inputs['source']['cues']} 条，{inputs['source']['lang']}）"
    )
    print(
        f"译文：{inputs['target']['path']}"
        f"（{inputs['target']['cues']} 条，{inputs['target']['lang']}）"
    )
    print(f"校验：{report['status']}（error {summary['errors']} / warning {summary['warnings']}）")

    for finding in report["findings"]:
        if finding["severity"] == "error":
            print(f"[error] {finding['code']}: {finding['message']}", file=sys.stderr)

    if report["outputs"]:
        print("产物：")
        for output in report["outputs"]:
            print(f"  {Path(output['path']).name}（{output['bytes']} 字节）")
    for note in report["notes"]:
        print(f"注意：{note}", file=sys.stderr)

    print(f"报告：{result.report_path}")


def _run_build(args: argparse.Namespace) -> int:
    limits = Limits.from_languages(
        args.src_lang,
        args.tgt_lang,
        cps_src=args.cps_src,
        cps_tgt=args.cps_tgt,
        width_src=args.width_src,
        width_tgt=args.width_tgt,
        max_lines=args.max_lines,
    )
    result = run_matrix(
        MatrixOptions(
            src=args.src,
            tgt=args.tgt,
            out=args.out,
            src_lang=args.src_lang,
            tgt_lang=args.tgt_lang,
            limits=limits,
            layout=args.wrap,
        )
    )
    _print_summary(result)
    return result.exit_code


def _run_probe(args: argparse.Namespace) -> int:
    result = run_probe(
        ProbeOptions(
            src=args.src,
            out=args.out,
            src_lang=args.src_lang,
            extract=not args.no_extract,
        )
    )
    _print_probe_summary(result)
    return result.exit_code


def _print_probe_summary(result: ProbeResult) -> None:
    manifest = result.manifest
    source = manifest["input"]
    streams = manifest["streams"]
    decision = manifest["decision"]

    duration = source["duration_s"]
    duration_text = f"{duration:.2f}s" if isinstance(duration, (int, float)) else "未知时长"
    print(f"输入：{source['path']}（{source['format_name']}，{duration_text}）")
    print(
        f"流：视频 {len(streams['video'])} / 音频 {len(streams['audio'])}"
        f" / 字幕 {len(streams['subtitle'])}"
    )
    for info in streams["subtitle"]:
        flags = []
        if info["forced"]:
            flags.append("forced")
        if info["default"]:
            flags.append("default")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"  字幕 #{info['index']} {info['codec']} "
            f"语言={info['language'] or '未标记'}{suffix}"
        )

    print(f"决策：{decision['kind']} —— {decision['reason']}")
    for warning in manifest["warnings"]:
        print(f"[warn] {warning}", file=sys.stderr)
    if result.subtitle_path is not None:
        print(f"字幕：{result.subtitle_path}（{result.cues} 条）")
    print(f"清单：{result.manifest_path}")


def _run_asr(args: argparse.Namespace) -> int:
    hotwords = [word.strip() for word in (args.hotwords or "").split(",") if word.strip()]
    if args.hotwords_file is not None:
        hotwords.extend(Glossary.from_file(args.hotwords_file).sources)
    seen: set[str] = set()
    hotwords = [word for word in hotwords if not (word in seen or seen.add(word))]

    result = run_asr(
        AsrOptions(
            src=args.src,
            out=args.out,
            language=args.language,
            model=args.model,
            python=args.asr_python,
            hotwords=tuple(hotwords),
            initial_prompt=args.initial_prompt,
            beam_size=args.beam_size,
            compute_type=args.compute_type,
            device=args.device,
            cpu_threads=args.cpu_threads,
            vad=not args.no_vad,
            max_duration_ms=round(args.max_duration * 1000),
            max_chars=args.max_chars,
            pause_ms=round(args.pause * 1000),
            min_duration_ms=round(args.min_duration * 1000),
            keep_raw=args.keep_raw,
        )
    )
    _print_asr_summary(result)
    return result.exit_code


def _print_asr_summary(result: AsrResult) -> None:
    report = result.report
    summary = report["summary"]
    config = report["config"]

    print(
        f"输入：{report['input']['path']}"
        f"（{report['input']['duration_s']}s，语言 {summary['language']}"
        f" p={summary['language_probability']}）"
    )
    print(
        f"识别：{summary['words']} 词 / {summary['raw_segments']} 个模型段"
        f" → {summary['cues']} 条（平均 {summary['avg_cue_s']}s，语音 {summary['speech_s']}s）"
    )
    if config["hotwords"]:
        print(f"热词：{len(config['hotwords'])} 个 —— {'、'.join(config['hotwords'][:8])}")
    if summary["rtf"] is not None:
        print(f"耗时：{summary['wall_s']}s（RTF {summary['rtf']}x）")
    if result.srt_path is not None:
        print(f"字幕：{result.srt_path}")
    print(f"词级：{result.words_path}")
    for warning in report["warnings"]:
        print(f"[warn] {warning}", file=sys.stderr)
    print(f"报告：{result.report_path}")


def _run_translate(args: argparse.Namespace) -> int:
    glossary = Glossary.from_file(args.glossary) if args.glossary is not None else None
    if glossary is not None and args.glossary_reverse:
        glossary = glossary.reversed()
    llm = LlmConfig.from_env(
        base_url=args.llm_base_url, api_key=args.llm_api_key, model=args.llm_model
    )
    result = run_translate(
        TranslateOptions(
            src=args.src,
            out=args.out,
            src_lang=args.src_lang,
            tgt_lang=args.tgt_lang,
            out_name=args.out_name,
            provider=args.provider,
            command_template=args.command_template,
            batch_size=args.batch_size,
            retries=args.retries,
            timeout=args.timeout,
            context=args.context,
            glossary=glossary,
            condense=args.condense,
            condense_mode=args.condense_mode,
            target_cps=args.target_cps,
            fallback_provider=args.fallback_provider,
            concurrency=args.concurrency,
            llm=llm if llm.configured else None,
            llm_review=args.llm_review,
            llm_review_passes=args.llm_review_passes,
        )
    )
    _print_translate_summary(result)
    return result.exit_code


def _print_translate_summary(result: TranslateResult) -> None:
    report = result.report
    summary = report["summary"]
    config = report["config"]
    cps = summary["cps"]
    before = summary["cps_before_condense"]

    print(
        f"翻译：{summary['cues']} 条（{config['src_lang']}→{config['tgt_lang']}，"
        f"{config['provider']}，上下文 {config['context']}）"
    )
    if config["context"] == "paragraph":
        print(
            f"段落：{summary['paragraphs']} 段；回退逐条：{summary['fallback_cues']} 条"
        )
    if config["glossary"]["terms"]:
        print(
            f"术语：{config['glossary']['terms']} 个；占位符丢失：{summary['placeholder_lost']} 处"
        )
    if config["condense"]["enabled"]:
        actions = summary["condense"]["by_action"]
        detail = "、".join(f"{key} {value}" for key, value in actions.items() if key != "none")
        print(
            f"精简：{summary['condense']['triggered']} 条（{detail or '无'}；"
            f"{config['condense']['rewriter']}），省 {summary['condense']['saved_chars']} 字；"
            f"需抽检 {len(summary['condense']['needs_review'])} 条"
        )
    review = summary.get("review") or {}
    if config.get("llm", {}).get("review"):
        print(
            f"审校：{review.get('passes', 0)} 轮，改动 {review.get('cues_changed', 0)} 条"
        )
    print(
        f"CPS：{before['mean']} → {cps['mean']}（上限 {cps['limit']:g}，"
        f"超限 {cps['over_limit']}/{summary['cues']} 条，峰值 {cps['max']}）"
    )
    print(f"耗时：{summary['wall_s']}s")
    if result.output_path is not None:
        print(f"产物：{result.output_path}")
    for warning in report["warnings"]:
        print(f"[warn] {warning}", file=sys.stderr)
    print(f"报告：{result.report_path}")


def _run_mark(args: argparse.Namespace) -> int:
    provided = {
        "label_text": args.label_text,
        "label_seconds": args.label_seconds,
        "position": args.position,
        "provider": args.provider,
        "comment": args.comment,
        "copyright_text": args.copyright_text,
        "font_file": args.font_file,
    }
    kwargs = {key: value for key, value in provided.items() if value is not None}
    result = run_mark(
        MarkOptions(src=args.src, out=args.out, self_use=args.self_use, **kwargs)
    )
    _print_mark_summary(result)
    return result.exit_code


def _print_mark_summary(result: MarkResult) -> None:
    report = result.report
    output = report["output"]
    duration = report["input"]["duration_s"]

    if report["status"] == "self_use":
        print(f"输入：{report['input']['path']}（{duration:.2f}s）")
        print("模式：自用（未写入 AIGC 标识）")
        print(f"产物：{output['path']}（{output['bytes']} 字节）")
        print(f"留痕：{result.report_path.parent / 'self_use_notice.json'}")
        print(f"报告：{result.report_path}")
        print(SELF_USE_NOTICE, file=sys.stderr)
        return

    config = report["config"]
    verification = report["verification"]
    label_ok = bool(verification["explicit_label"]["ok"])
    metadata_ok = bool(verification["metadata_roundtrip"]["ok"])
    missing = verification["metadata_roundtrip"]["missing"]

    print(f"输入：{report['input']['path']}（{duration:.2f}s）")
    print(
        f"标识：显式（{config['position']}「{config['label_text']}」前 "
        f"{config['label_seconds']:g} 秒）+ 隐式 {len(report['metadata'])} 字段"
    )
    print(f"产物：{output['path']}（{output['bytes']} 字节）")
    detail = "" if metadata_ok else f"（缺 {'、'.join(missing)}）"
    print(f"核验：显式 {'✅' if label_ok else '❌'}；隐式 {'✅' if metadata_ok else '❌'}{detail}")
    if result.label_frame is not None:
        print(f"证物：{result.label_frame}")
    print(f"报告：{result.report_path}")

    if report["status"] != "marked":
        print("核验未通过：请检查标识是否真的烧上、元数据是否写进容器", file=sys.stderr)


def _run_merge(args: argparse.Namespace) -> int:
    options = MergeOptions(
        max_duration_ms=round(args.max_duration * 1000),
        max_gap_ms=round(args.max_gap * 1000),
    )
    result = run_merge(args.src, args.out, options)
    _print_merge_summary(result)
    return result.exit_code


def _print_merge_summary(result: MergeResult) -> None:
    report = result.report
    summary = report["summary"]
    config = report["config"]
    print(f"输入：{report['input']['path']}（{summary['before']} 条，{report['input']['encoding']}）")
    print(
        f"合并：{summary['before']} → {summary['after']} 条"
        f"（单条上限 {config['max_duration_ms'] / 1000:g}s / "
        f"间隔上限 {config['max_gap_ms'] / 1000:g}s）"
    )
    print(f"产物：{result.merged_path}")
    print(f"报告：{result.report_path}")


def _run_diarize(args: argparse.Namespace) -> int:
    result = run_diarize(
        DiarizeOptions(
            src=args.src,
            out=args.out,
            srt=args.srt,
            language=args.src_lang,
            python=args.python,
            segmentation_model=args.segmentation_model,
            embedding_model=args.embedding_model,
            num_speakers=args.speakers,
            threshold=args.threshold,
            min_speaker_speech_s=0.0 if args.keep_minor_speakers else 3.0,
            min_speaker_share=0.0 if args.keep_minor_speakers else 0.01,
            vad=args.vad,
            vad_model=args.vad_model,
            vad_threshold=args.vad_threshold,
            clustering=args.clustering,
            merge_alpha=args.merge_alpha,
            min_duration_on=args.min_duration_on,
            min_duration_off=args.min_duration_off,
            num_threads=args.threads,
        )
    )
    _print_diarize_summary(result)
    return result.exit_code


def _print_diarize_summary(result: DiarizeResult) -> None:
    report = result.report
    summary = report["summary"]
    print(f"输入：{report['input']['path']}（{report['input']['duration_s']}s）")
    raw = summary.get("speakers_raw")
    merged = summary.get("merged_clusters") or []
    merged_note = f"（原始 {raw} 簇，并入 {len(merged)} 个短簇）" if merged else ""
    print(
        f"说话人：{summary['speakers']} 个 / {summary['turns']} 段，"
        f"语音 {summary['speech_s']}s（覆盖 {summary['coverage_pct']}%）{merged_note}"
    )
    for item in summary["speaker_stats"]:
        line = f"  {item['speaker']}：{item['turns']} 段 / {item['speech_s']}s"
        if item.get("cues"):
            line += f" / {item['cues']} 条字幕（{item['chars']} 字）"
        print(line)
    vad = summary.get("vad")
    if isinstance(vad, dict):
        print(
            f"VAD 预过滤：语音 {vad.get('speech_pct')}%（{vad.get('segments')} 段，"
            f"丢弃非语音 {vad.get('removed_s')}s，耗时 {vad.get('elapsed_s')}s）"
        )
    elif report.get("config", {}).get("vad", {}).get("skipped"):
        print(f"VAD 预过滤：未启用（{report['config']['vad']['skipped']}）")
    clustering = summary.get("clustering")
    if isinstance(clustering, dict) and clustering.get("clusters_out") is not None:
        print(
            f"声纹二次聚类：{clustering.get('clusters_in')} → {clustering.get('clusters_out')} 人"
            f"（τ={clustering.get('tau')}，同人相似度 {clustering.get('s_same')}）"
        )
    if result.speakers_path is not None:
        print(f"归属：{result.speakers_path}")
    print(f"报告：{result.report_path}")
    for warning in report.get("warnings") or []:
        print(f"[warn] {warning}", file=sys.stderr)


def _run_dub(args: argparse.Namespace) -> int:
    default_voice = args.voice or "en-US-AriaNeural"
    voice_by_speaker: tuple[tuple[str, str], ...] = ()
    if args.speaker_voices and args.speakers_json is None:
        raise DubError("--speaker-voices 需要配合 --speakers-json（先跑 diarize 得到归属）")
    if args.speakers_json is not None:
        speakers_file = Path(args.speakers_json)
        if not speakers_file.is_file():
            raise DubError(f"说话人归属文件不存在：{speakers_file}")
        payload = json.loads(speakers_file.read_text(encoding="utf-8"))
        speakers = [str(item) for item in payload.get("speakers") or []]
        explicit, pool = parse_speaker_voices(args.speaker_voices)
        mapping = build_speaker_voice_map(
            speakers, explicit=explicit, pool=pool, default=default_voice
        )
        voice_by_speaker = tuple(sorted(mapping.items()))
    options = DubOptions(
        srt=args.srt,
        out=args.out,
        video=args.video,
        voice=default_voice,
        voice_by_speaker=voice_by_speaker,
        speakers_path=args.speakers_json,
        tts_rate=args.tts_rate,
        concurrency=args.concurrency,
        retries=args.retries,
        tempo_limit=args.tempo_limit,
        adaptive_rate=args.adaptive_rate,
        tts_command=args.tts_command,
        tts_python=args.tts_python,
        tts_provider=args.tts_provider,
        tts_fallback=args.tts_fallback,
        local_tts_model=args.local_tts_model,
        local_tts_kind=args.local_tts_kind,
        local_tts_voice=args.local_tts_voice,
        local_tts_python=args.local_tts_python,
        local_tts_threads=args.local_tts_threads,
        local_tts_speed=args.local_tts_speed,
        tgt_lang=args.tgt_lang,
        reuse_tts=not args.regenerate_tts,
        keep_bgm=args.keep_bgm,
        bgm_mode=args.bgm_mode,
        bgm_gain_db=args.bgm_gain,
        bgm_duck_db=args.bgm_duck,
        bgm_source=args.bgm_source,
        separator_python=args.separator_python,
    )
    result = run_dub(options)
    _print_dub_summary(result)
    return result.exit_code


def _print_dub_summary(result: DubResult) -> None:
    report = result.report
    summary = report["summary"]
    config = report["config"]
    outputs = report["outputs"]

    provider = config.get("tts_provider") or "edge"
    local_config = config.get("local_tts") or {}
    if provider == "local":
        local_summary = summary.get("local_tts") or {}
        model_name = Path(str(local_config.get("model") or "")).name
        print(
            f"配音：{summary['cues']} 条（本地 TTS {local_config.get('kind')} / 音色 "
            f"{local_summary.get('voices')} / 模型 {model_name}，并发 {config['concurrency']}）"
        )
    else:
        print(f"配音：{summary['cues']} 条（{config['voice']}，并发 {config['concurrency']}）")
    fallback = summary.get("tts_fallback") or {}
    events = fallback.get("events") or []
    if events:
        print(f"兜底：{len(events)} 条 Edge-TTS 失败已改用本地 TTS（音色 {events[0].get('voice')}）")
    if summary.get("speakers"):
        voices = summary.get("voices") or []
        print(
            f"多人声：{summary['speakers']} 个说话人 / {len(voices)} 种音色（"
            + "；".join(
                f"{item['voice']} × {item['cues']}" for item in voices if isinstance(item, dict)
            )
            + "）"
        )
    print(
        f"合成：{summary['synthesized']}/{summary['cues']} 成功；"
        f"溢出 >10%：{summary['overflow_over_10pct']} 条；"
        f"吸收：{summary['absorbed']}；atempo：{summary['atempo']}；人工：{summary['manual']}"
    )
    print(f"音频：{outputs['audio']}")
    bgm = summary.get("bgm") or {}
    if bgm:
        layout = "立体声" if bgm.get("mix_channels") == 2 else "单声道"
        print(
            f"BGM：{bgm['mode']}（{bgm['separator']}），混音 {bgm['wall_s']}s；"
            f"混音格式 {bgm.get('mix_sample_rate')}Hz/{layout}；成片音轨：{outputs['mixed']}"
        )
    if outputs["video"]:
        print(f"视频：{outputs['video']}")
    print(f"耗时：{summary['wall_s']}s")
    print(f"报告：{result.report_path}")

    for warning in report.get("warnings") or []:
        print(f"[warn] {warning}", file=sys.stderr)

    for failure in report["failures"]:
        print(f"[error] 第 {failure['cue']} 条配音失败：{failure['error']}", file=sys.stderr)
    if result.status == "partial":
        print("部分条目配音失败：已记录在 dub_report.json，其余条目照常产出", file=sys.stderr)


def _run_burn(args: argparse.Namespace) -> int:
    result = run_burn(
        BurnOptions(
            video=args.video,
            subtitle=args.subtitle,
            out=args.out,
            crf=args.crf,
            preset=args.preset,
            verify=args.verify,
            position=args.position,
        )
    )
    _print_burn_summary(result)
    return result.exit_code


def _print_burn_summary(result: BurnResult) -> None:
    report = result.report
    verification = report.get("verification") or {}
    output = report["output"]
    print(f"输入：{report['input']['video']['path']}（{report['input']['video']['duration_s']}s）")
    print(f"字幕：{report['input']['subtitle']['path']}（位置 {report['config']['position']}）")
    print(f"产物：{output['path']}（{output['bytes']} 字节，{output['duration_s']}s）")
    if verification:
        mark = "✅" if verification["ok"] else "❌"
        print(
            f"核验：字幕烧录 {mark}（{verification['at_ms'] / 1000:.1f}s 处 "
            f"{verification['region']} 区域像素差 {verification['region_pixel_diff']}，"
            f"强变化像素 {verification['strong_pixel_ratio'] * 100:.2f}%）"
        )
    print(f"报告：{result.report_path}")
    for warning in report.get("warnings") or []:
        print(f"[warn] {warning}", file=sys.stderr)


def _run_pipeline(args: argparse.Namespace) -> int:
    glossary = Glossary.from_file(args.glossary) if args.glossary is not None else None
    if glossary is not None and args.glossary_reverse:
        glossary = glossary.reversed()
    hotwords = [word.strip() for word in (args.hotwords or "").split(",") if word.strip()]
    if args.hotwords_file is not None:
        hotwords.extend(Glossary.from_file(args.hotwords_file).sources)
    llm = LlmConfig.from_env()
    result = run_pipeline(
        PipelineOptions(
            src=args.src,
            out=args.out,
            src_lang=args.src_lang,
            tgt_lang=args.tgt_lang,
            provider=args.provider,
            llm=args.llm,
            llm_review=True,
            glossary=glossary,
            condense=args.condense,
            condense_mode=args.condense_mode,
            translate_fallback=args.translate_fallback,
            asr_python=args.asr_python,
            asr_model=args.asr_model,
            hotwords=tuple(hotwords),
            initial_prompt=args.initial_prompt,
            merge=args.merge,
            merge_max_duration=args.merge_max_duration,
            merge_max_gap=args.merge_max_gap,
            diarize=args.diarize,
            speakers=args.speakers,
            diarization_threshold=args.diarization_threshold,
            keep_minor_speakers=args.keep_minor_speakers,
            diarize_python=args.diarize_python,
            segmentation_model=args.segmentation_model,
            embedding_model=args.embedding_model,
            vad=args.vad,
            vad_model=args.vad_model,
            vad_threshold=args.vad_threshold,
            clustering=args.clustering,
            merge_alpha=args.merge_alpha,
            speaker_voices=args.speaker_voices,
            voice=args.voice,
            tts_rate=args.tts_rate,
            tts_python=args.tts_python,
            tts_command=args.tts_command,
            tts_provider=args.tts_provider,
            tts_fallback=args.tts_fallback,
            local_tts_model=args.local_tts_model,
            local_tts_kind=args.local_tts_kind,
            local_tts_voice=args.local_tts_voice,
            local_tts_python=args.local_tts_python,
            local_tts_threads=args.local_tts_threads,
            local_tts_speed=args.local_tts_speed,
            regenerate_tts=args.regenerate_tts,
            concurrency=args.concurrency,
            translate_concurrency=args.translate_concurrency,
            tempo_limit=args.tempo_limit,
            adaptive_rate=args.adaptive_rate,
            keep_bgm=args.keep_bgm,
            bgm_mode=args.bgm_mode,
            bgm_source=args.bgm_source,
            separator_python=args.separator_python,
            burn_subtitles=args.burn_subtitles,
            burn_mode=args.burn_mode,
            burn_crf=args.burn_crf,
            burn_position=args.burn_position,
            mark=args.mark,
            force=args.force,
            stop_after=args.stop_after,
            pricing=str(args.pricing) if args.pricing is not None else None,
            llm_config=llm if llm.configured else None,
        )
    )
    _print_pipeline_summary(result)
    return result.exit_code


def _print_pipeline_summary(result: PipelineResult) -> None:
    report = result.report
    config = report["config"]
    print(
        f"全链路：{report['input']['path']}（{config['src_lang']} → {config['tgt_lang']}，"
        f"{config['provider']}）"
    )
    for stage in report["stages"]:
        line = f"  [{stage['status']:>7}] {stage['stage']:<9} {stage['wall_s']:>6.1f}s"
        summary = stage.get("summary") or {}
        if stage["stage"] == "asr" and summary:
            line += f"  {summary.get('model') or ''} {summary.get('cues') or ''} 条"
        if stage["stage"] == "merge" and summary:
            if stage["status"] == "skipped":
                line += f"  {summary.get('reason') or ''}"
            else:
                line += f"  合并 {summary.get('before')} → {summary.get('after')} 条"
        if stage["stage"] == "diarize" and summary:
            if stage["status"] == "skipped":
                line += f"  {summary.get('reason') or ''}"
            elif summary.get("speakers") is not None:
                line += f"  说话人 {summary.get('speakers')} 个 / {summary.get('turns')} 段"
                voices = summary.get("speaker_voices") or {}
                if voices:
                    line += f"  音色 {len(voices)} 种"
            elif stage.get("error"):
                line += "  失败（已退回单音色）"
        if stage["stage"] == "translate" and summary:
            line += (
                f"  CPS {summary.get('cps')}（超限 {summary.get('cps_over_limit')}）"
                f" 精简 {summary.get('condense_triggered')}"
                f" 审校 {summary.get('review_changes')}"
                f" token {summary.get('tokens')}"
            )
        if stage["stage"] == "build" and summary:
            line += f"  排版 {summary.get('cues_before')} → {summary.get('cues_after')} 条"
        if stage["stage"] == "dub" and summary:
            line += (
                f"  {summary.get('synthesized')}/{summary.get('cues')} 成功"
                f" 人工 {summary.get('manual')}"
                + (f" BGM {summary.get('bgm_mode')}" if summary.get("bgm_mode") else "")
            )
        if stage["stage"] == "burn" and summary:
            if stage["status"] == "skipped":
                line += f"  {summary.get('reason') or ''}"
            else:
                line += f"  烧录 {summary.get('cues')} 条 核验 {summary.get('verified')}"
                if summary.get("position"):
                    line += f" 位置 {summary.get('position')}"
                if summary.get("pixel_diff") is not None:
                    line += f" 像素差 {summary.get('pixel_diff')}"
        if stage["stage"] == "mark" and summary.get("explicit") is not None:
            line += f"  显式 {summary.get('explicit')} 隐式 {summary.get('implicit')}"
        if stage.get("error"):
            line += f"  {stage['error']}"
        print(line)
    outputs = report["outputs"]
    print(f"状态：{report['status']}（耗时 {report['wall_s']}s）")
    cost = report.get("cost")
    if isinstance(cost, dict):
        print(format_cost_line(cost))
    for label, key in (
        ("发布字幕", "publish_srt"),
        ("双语字幕", "bilingual_srt"),
        ("配音成片", "dub_video"),
        ("带字幕成片", "burned_video"),
        ("最终成品", "final_video"),
    ):
        if outputs.get(key):
            print(f"{label}：{outputs[key]}")
    print(f"报告：{result.report_path}")
    for warning in report.get("warnings") or []:
        print(f"[warn] {warning}", file=sys.stderr)
    if result.status == "failed":
        print("链路失败：修好失败阶段后重跑（已完成阶段会自动复用）", file=sys.stderr)


def _run_cost(args: argparse.Namespace) -> int:
    report_path = Path(args.work) / "pipeline.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise CostError(f"读不到 {report_path}：{exc}") from exc
    except ValueError as exc:
        raise CostError(f"{report_path} 不是合法 JSON：{exc}") from exc
    if not isinstance(report, dict):
        raise CostError(f"{report_path} 顶层不是对象")
    stored = report.get("cost")
    if args.tts_command:
        # 指定了 TTS 模板：重新从子报告抽量（旧产物没有 provider 时靠它识别计费渠道）
        cost = build_cost(
            args.work,
            tts_command=args.tts_command,
            pricing=load_pricing(args.pricing),
            pricing_path=args.pricing,
        )
    elif args.pricing is not None:
        pricing = load_pricing(args.pricing)
        if isinstance(stored, dict) and isinstance(stored.get("quantities"), dict):
            cost = reprice(stored, pricing, pricing_path=args.pricing)
        else:
            cost = build_cost(args.work, pricing=pricing, pricing_path=args.pricing)
    elif isinstance(stored, dict):
        cost = stored
    else:
        print(
            "提示：pipeline.json 没有 cost 块（旧产物），已按子报告重算；"
            "命令渠道 TTS 可能按 0 元计",
            file=sys.stderr,
        )
        cost = build_cost(args.work)
    if args.json:
        print(json.dumps(cost, ensure_ascii=False, indent=2))
    else:
        print(render_cost(cost))
    return EXIT_OK


def _run_gui(args: argparse.Namespace) -> int:
    spec = importlib.util.find_spec("gradio")
    if spec is None or spec.loader is None:
        # 仓库自带的 GUI venv：从系统 Python 启动时自动重入，免去手动换解释器
        # （spec.loader 为空说明只是同名目录/命名空间包，不能当 gradio 用）
        repo_root = Path(__file__).resolve().parent.parent
        venv_python = repo_root / "spike" / "gui" / ".venv" / "Scripts" / "python.exe"
        if venv_python.is_file() and not os.environ.get("SUBMATRIX_GUI_REEXEC"):
            command = [
                str(venv_python),
                "-m",
                "submatrix",
                "gui",
                "--host",
                args.host,
                "--port",
                str(args.port),
            ]
            if args.no_browser:
                command.append("--no-browser")
            for allowed in args.allowed_path or []:
                command += ["--allowed-path", str(allowed)]
            env = dict(os.environ, SUBMATRIX_GUI_REEXEC="1")
            return subprocess.call(command, cwd=str(repo_root), env=env)
        print(
            "GUI 需要 gradio：先执行 `pip install gradio`，"
            "或准备好 spike\\gui\\.venv（见 M13 文档）",
            file=sys.stderr,
        )
        return EXIT_USAGE
    from .gui import serve

    return serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        allowed_paths=args.allowed_path,
    )


def _run_history(args: argparse.Namespace) -> int:
    """查看/清理 GUI 任务历史（不开页面也能对账）。"""

    try:
        store = HistoryStore(args.db)
        if args.delete is not None:
            removed = store.delete_task(args.delete)
            if not removed:
                print(f"没有 id={args.delete} 的记录", file=sys.stderr)
                return EXIT_USAGE
            print(f"已删除记录 id={args.delete}（产物文件不受影响）")
            return EXIT_OK
        tasks = store.list_tasks(limit=args.limit)
    except HistoryError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not tasks:
        print(f"还没有任务记录（数据库：{store.path}）")
        return EXIT_OK
    print(f"任务历史（{store.path}，最近 {len(tasks)} 条）")
    widths = [8, 16, 28, 40, 8, 6, 10, 9]
    print("  ".join(header.ljust(width) for header, width in zip(HISTORY_HEADERS, widths)))
    for task in tasks:
        row = history_rows([task])[0]
        row[4] = STATUS_LABELS.get(str(task.get("status") or ""), row[4])
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.command == "build":
            return _run_build(args)
        if args.command == "probe":
            return _run_probe(args)
        if args.command == "asr":
            return _run_asr(args)
        if args.command == "translate":
            return _run_translate(args)
        if args.command == "mark":
            return _run_mark(args)
        if args.command == "merge":
            return _run_merge(args)
        if args.command == "diarize":
            return _run_diarize(args)
        if args.command == "dub":
            return _run_dub(args)
        if args.command == "burn":
            return _run_burn(args)
        if args.command == "pipeline":
            return _run_pipeline(args)
        if args.command == "cost":
            return _run_cost(args)
        if args.command == "gui":
            return _run_gui(args)
        if args.command == "history":
            return _run_history(args)
    except SrtParseError as exc:
        print(f"字幕解析失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except MarkError as exc:
        print(f"打标失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except DubError as exc:
        print(f"配音失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except ProbeError as exc:
        print(f"探测失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except AsrError as exc:
        print(f"识别失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except TranslateError as exc:
        print(f"翻译失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except PipelineError as exc:
        print(f"全链路失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except MergeError as exc:
        print(f"合并失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except DiarizeError as exc:
        print(f"说话人分离失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except BurnError as exc:
        print(f"烧录失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except GlossaryError as exc:
        print(f"术语表错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except CostError as exc:
        print(f"成本汇总失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"未知子命令：{args.command}", file=sys.stderr)
    return EXIT_USAGE
