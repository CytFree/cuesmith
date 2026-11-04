"""FR-3 配音编排：并发合成 / 重试 / 时长校验 / atempo 溢出吸收 / 铺轨 / mux。

设计要点（都对应 PRD 条款）：
- **并发**：云 TTS 每条一次网络往返（本机实测 ~11s 握手延迟），串行 100 条不可接受。
  实测 6 并发把 66.9s 压到 12.9s（5.2×）。
- **重试**：单条失败重试 2 次；最终失败**只记录不阻塞**其余条目（AC-9）。
- **溢出策略**（AC-8）：`atempo ≤1.3x` → 溢出的部分自然落进后一条之前的静音（"静音段吸收"）
  → 仍溢出则标 `manual` 交人工。**不默认顺延**（顺延会让后面全部错位）。
- **铺轨**：直接在 PCM 上按槽位起点写入（不再顺延、不拼接成一条流水音轨），
  这样每条都对着自己的时间轴，溢出/空缺一目了然。
"""

from __future__ import annotations

import array
import hashlib
import json
import subprocess
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import __version__
from . import media
from .bgm import BgmError, BgmOptions, BgmResult, run_bgm
from .report import file_digest
from .srt import load_srt
from .tts import (
    CommandTtsEngine,
    EdgeTtsEngine,
    TtsEngine,
    TtsError,
    _default_edge_tts_python,
    format_rate,
    parse_rate,
)

__all__ = [
    "DubError",
    "DubOptions",
    "DubResult",
    "build_engine",
    "run_dub",
]

DEFAULT_SAMPLE_RATE = 24000
OVERFLOW_MANUAL_THRESHOLD_PCT = 10.0
# 提语速档位（OPT-2）：在基础语速之上加多少个百分点，逐级尝试。
# 中文基础 +30% → 候选 +45% / +55% / +65%；英文基础 +20% → +35% / +45% / +55%。
# 为什么不用 atempo 直接硬压：TTS 自己加速会保留语调、只把话说快，听感明显好于
# ffmpeg 的 atempo（后者是均匀变速，压多了会有金属感）。atempo 只当最后一道兜底。
RATE_LADDER_PP = (15, 25, 35)
MAX_RATE_PCT = 80  # 再高就明显失真（Edge-TTS 上限 +100%，实测 +80% 已接近不可用）
# 提语速后留给 atempo 的余量：只留 ≤15% 给 atempo，其余尽量靠 TTS 自己说快。
# 依据：同一条要把总时长压到某比例时，"TTS 提速 + 少量 atempo" 比"原速 + 大 atempo"好听。
ESCALATION_ATEMPO_HEADROOM = 1.15
# 音色不可用的错误特征（OPT-4）：微软下线音色时返回 NoAudioReceived / 无音频，
# 也有端点返回 invalid voice / voice not found。命中就降级到默认音色重合成一次。
VOICE_ERROR_HINTS = (
    "noaudioreceived",
    "no audio",
    "no_audio",
    "invalid voice",
    "voice not found",
    "voice is not",
    "not a valid voice",
    "unsupported voice",
    "voice not supported",
)


def _looks_like_voice_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(hint in text for hint in VOICE_ERROR_HINTS)


class DubError(RuntimeError):
    """编排层不可继续的错误（参数/输入/铺轨失败）。"""


@dataclass(frozen=True, slots=True)
class DubOptions:
    srt: Path
    out: Path
    video: Path | None = None
    voice: str = "en-US-AriaNeural"
    # 多人声（M11）：说话人 → 音色。来自 diarize 的 speakers.json；
    # 空 = 全片单音色（单人口播 / 没做说话人分离时行为完全不变）
    voice_by_speaker: tuple[tuple[str, str], ...] = ()
    speakers_path: Path | None = None  # cue → speaker 的归属文件
    tts_rate: str | None = None  # Edge-TTS 语速，如 "+30%"
    concurrency: int = 10
    retries: int = 2
    tempo_limit: float = 1.4
    retry_delay_s: float = 1.0
    # 自适应提语速（OPT-2）：放不下的条目用同一音色提一档语速重合成，代替硬 atempo。
    # 外部命令模板（--tts-command）不支持改语速，此时自动跳过。
    adaptive_rate: bool = True
    tts_command: str | None = None
    tts_python: str | None = None
    # 本地 TTS（OPT-11）：默认仍是 Edge；`tts_provider=local` 可完全离线；
    # `tts_fallback=local` 让 Edge 失败（网络/端点）的条目改用本地 TTS 兜底。
    tts_provider: str = "edge"  # edge | local
    tts_fallback: str = "local"  # local | off（只在 primary=edge 时生效）
    local_tts_model: str | None = None
    local_tts_kind: str = "auto"  # auto | kokoro | vits
    local_tts_voice: str = ""  # 本地音色（如 kokoro:47；空 = 按目标语言默认）
    local_tts_python: str | None = None
    local_tts_threads: int = 8
    local_tts_speed: float | None = None  # 基础倍速（None = 1.0；再乘 tts_rate）
    tgt_lang: str = "zh"  # 只用于挑本地 TTS 的默认音色
    # 内容指纹命中的 TTS 音频直接复用（云端 TTS 按字符计费，重跑不重复扣费）；
    # False = --regenerate-tts 强制重新合成
    reuse_tts: bool = True
    sample_rate: int = DEFAULT_SAMPLE_RATE
    # BGM 保留（收费软件默认能力）：拆出伴奏 → 与配音侧链混音 → 并进视频
    keep_bgm: bool = False
    bgm_mode: str = "auto"  # auto | separate | center | duck
    bgm_gain_db: float = -2.0
    bgm_duck_db: float = -12.0
    bgm_source: Path | None = None  # 默认取 --video 的音轨
    separator_python: str | None = None
    generated_at: datetime | None = None

    def validate(self) -> None:
        if self.concurrency < 1:
            raise DubError("并发数必须 ≥ 1")
        if not 1.0 <= self.tempo_limit <= 2.0:
            raise DubError("atempo 上限必须在 1.0–2.0 之间（超过 2.0 音质不可接受）")
        if self.retries < 0:
            raise DubError("重试次数不能为负")
        if self.sample_rate < 8000:
            raise DubError("采样率过低（至少 8000）")
        if self.keep_bgm and self.bgm_mode not in {"auto", "separate", "center", "duck"}:
            raise DubError(f"不认识的 BGM 模式：{self.bgm_mode!r}")
        if self.keep_bgm and self.bgm_source is None and self.video is None:
            raise DubError("--keep-bgm 需要 --video（取原片音轨）或 --bgm-source 指定素材")
        if self.tts_provider not in {"edge", "local"}:
            raise DubError("--tts-provider 只能是 edge（默认）或 local")
        if self.tts_fallback not in {"local", "off"}:
            raise DubError("--tts-fallback 只能是 local（默认）或 off")
        if self.tts_provider == "local" and self.tts_command:
            raise DubError("--tts-provider local 与 --tts-command 不能同时使用")
        if self.local_tts_kind not in {"auto", "kokoro", "vits"}:
            raise DubError("--local-tts-kind 只能是 auto / kokoro / vits")
        if self.local_tts_threads < 1:
            raise DubError("--local-tts-threads 必须 ≥ 1")
        if self.local_tts_speed is not None and not 0.5 <= self.local_tts_speed <= 2.0:
            raise DubError("--local-tts-speed 必须在 0.5–2.0 之间")


@dataclass(frozen=True, slots=True)
class DubResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    audio_path: Path
    video_path: Path | None


@lru_cache(maxsize=4)
def _cached_local_python(explicit: str | None) -> str:
    """本地 TTS 的 sherpa 解释器探测结果缓存（探测一次要起子进程，别每条都探）。"""

    from . import localtts

    return localtts.find_local_tts_python(explicit)


def _local_model_info(options: DubOptions) -> tuple[Path, str]:
    """解析本地 TTS 模型目录与类型；不配置就抛 DubError（不静默降级）。"""

    from . import localtts

    model = localtts.find_local_tts_model(options.local_tts_model)
    if model is None:
        raise DubError(
            "没有找到本地 TTS 模型：用 --local-tts-model 指定模型目录"
            "（例如 spike/local-tts/kokoro-multi-lang-v1_0），"
            "或设置 SUBMATRIX_LOCAL_TTS_MODEL"
        )
    kind = (
        options.local_tts_kind
        if options.local_tts_kind != "auto"
        else localtts.detect_local_tts_kind(model)
    )
    return model, kind


def _local_speed(options: DubOptions, rate: str | None) -> float:
    """本地 TTS 的 speed 参数：基础倍速 × (1 + 语速%)，夹在 0.5–2.0。"""

    base = options.local_tts_speed if options.local_tts_speed is not None else 1.0
    return max(0.5, min(2.0, base * (1.0 + parse_rate(rate))))


def _is_edge_voice(value: str) -> bool:
    """粗判 Edge 音色 id（``xx-XX-XxxNeural``）；本地 spec 不会长这样。"""

    return bool(value) and value.endswith("Neural") and "-" in value


def _local_voice_spec(options: DubOptions, voice: str | None) -> str:
    """本地音色：显式 --local-tts-voice 优先；Edge 风格 id/空值交给引擎回落到语言默认。"""

    if options.local_tts_voice and (not voice or _is_edge_voice(voice)):
        return options.local_tts_voice
    if not voice or _is_edge_voice(voice):
        return ""
    return voice


def _local_engine(options: DubOptions, voice: str | None, rate: str | None):
    """构造本地 TTS 引擎（共享常驻 worker，模型只加载一次）。"""

    from . import localtts

    model, kind = _local_model_info(options)
    engine_options = localtts.LocalTtsOptions(
        model=model,
        kind=kind,
        python=_cached_local_python(options.local_tts_python),
        threads=options.local_tts_threads,
    )
    return localtts.LocalTtsEngine(
        engine_options,
        voice=_local_voice_spec(options, voice),
        language=options.tgt_lang,
        speed=_local_speed(options, rate),
    )


def engine_tag(
    options: DubOptions, voice: str, rate: str | None, *, local_info: tuple[Path, str] | None = None
) -> tuple[str, str]:
    """TTS 缓存指纹：返回 (engine_tag, suffix)。Edge 与本地/命令引擎互不串用缓存。"""

    if options.tts_command:
        return "cmd", ".mp3"
    if options.tts_provider == "local":
        model, kind = local_info or _local_model_info(options)
        return f"local:{model.name}:{kind}:{_local_speed(options, rate):.3f}", ".wav"
    return "edge", ".mp3"


def build_engine(
    options: DubOptions, voice: str | None = None, *, rate: str | None = None
) -> TtsEngine:
    """构造 TTS 引擎；``voice`` / ``rate`` 给定时用它（多人声按说话人、提语速按档位）。

    外部命令模板里的 ``{voice_id}`` / ``{voice}`` 会在建引擎时替换成当前音色：
    多人声每换一个说话人就换一个声纹 ID（例如阿里云百炼声音复刻返回的 voice_id）。
    ``--tts-provider local`` 时构造本地 sherpa-onnx 引擎（OPT-11）。
    """

    selected = voice or options.voice
    effective_rate = options.tts_rate if rate is None else rate
    if options.tts_command:
        command = options.tts_command.replace("{voice_id}", selected).replace("{voice}", selected)
        return CommandTtsEngine(command=command)
    if options.tts_provider == "local":
        return _local_engine(options, selected, effective_rate)
    return EdgeTtsEngine(
        voice=selected,
        python=options.tts_python or _default_edge_tts_python(),
        rate=effective_rate,
    )


def _cue_audio_path(
    cache_dir: Path,
    index: int,
    *,
    text: str,
    voice: str,
    rate: str | None,
    command: str | None,
    engine: str = "edge",
    suffix: str = ".mp3",
) -> Path:
    """TTS 缓存文件名带上「引擎 + 文本 + 音色 + 语速」指纹。

    多人声/换音色重跑时，若沿用 ``cue_0001.mp3`` 这种固定名，会把旧音色的音频当缓存
    直接复用（听感上"换了音色但没生效"）。按内容指纹命名最省事也最安全。
    ``engine``（OPT-11）把 Edge 与本地 TTS 的音频彻底分开，互不串用。
    """

    tag = hashlib.sha1(
        f"{engine}|{voice}|{rate or ''}|{command or ''}|{text}".encode("utf-8")
    ).hexdigest()[:10]
    return cache_dir / f"cue_{index:04d}_{tag}{suffix}"


def _load_speaker_assignments(path: Path) -> tuple[dict[int, str], dict[str, str], list[str]]:
    """读 diarize 的 ``speakers.json``：返回 (cue→speaker, 说话人顺序, 警告)。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[int, str] = {}
    speakers: list[str] = [str(item) for item in payload.get("speakers") or []]
    for item in payload.get("cues") or []:
        try:
            mapping[int(item["cue"])] = str(item.get("speaker") or "")
        except (KeyError, TypeError, ValueError):
            continue
    warnings: list[str] = []
    if not mapping:
        warnings.append(f"说话人归属文件里没有可用的 cue 映射：{path.name}")
    return mapping, speakers, warnings


def _run(command: list[str], *, what: str) -> "subprocess.CompletedProcess[bytes]":
    return media.run_ffmpeg(command, error_cls=DubError, what=what)


def _media_duration_ms(path: Path) -> int:
    """音/视频通吃：读容器里的时长（ffprobe）。"""

    return media.duration_ms(path, error_cls=DubError, what=f"读不出时长（{path.name}）")


def _synth_with_retries(
    engine: TtsEngine,
    text: str,
    out_path: Path,
    *,
    retries: int,
    retry_delay_s: float,
) -> int:
    """返回实际尝试次数；失败抛最后一次的异常。"""

    attempts = 0
    while True:
        attempts += 1
        try:
            if out_path.exists():
                out_path.unlink()
            engine.synthesize(text, out_path)
            if not out_path.is_file() or out_path.stat().st_size == 0:
                raise TtsError("合成没有产出音频文件")
            return attempts
        except Exception as exc:  # noqa: BLE001 - 引擎异常类型不可控，统一按可重试处理
            if attempts > retries:
                raise DubError(f"第 {attempts} 次尝试仍失败：{exc}") from exc
            if retry_delay_s:
                time.sleep(retry_delay_s)


def _decode_to_pcm(path: Path, tempo: float, sample_rate: int) -> array.array:
    """把任意音频解成 16bit 单声道 PCM（需要变速时走 atempo）。"""

    target = path.with_suffix(".pcm.wav")
    command: list[str] = ["-i", str(path)]
    if abs(tempo - 1.0) > 1e-6:
        command += ["-filter:a", f"atempo={tempo:.4f}"]
    command += ["-ar", str(sample_rate), "-ac", "1", "-f", "wav", str(target)]
    _run(command, what=f"解码 {path.name}")

    with wave.open(str(target), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    target.unlink(missing_ok=True)
    return array.array("h", frames)


def _write_wav(path: Path, samples: array.array, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def _video_codec(path: Path) -> str:
    payload = media.ffprobe_json(
        path,
        error_cls=DubError,
        streams=True,
        show_format=False,
        what=f"读不出视频流信息（{path.name}）",
    )
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        disposition = stream.get("disposition") or {}
        attached_pic = isinstance(disposition, dict) and disposition.get("attached_pic")
        if stream.get("codec_type") == "video" and not attached_pic:
            return str(stream.get("codec_name") or "unknown")
    raise DubError(f"输入里没有视频流：{path}")


def _plan_action(
    audio_ms: int, slot_ms: int, room_ms: int, tempo_limit: float
) -> tuple[str, float]:
    """溢出三级策略。

    ``room_ms`` 是"这条开始到下一条开始之间的可用时间"（最后一条用视频长度）——
    比槽位宽出来的部分是真实可用的静音，能吸收就不该去压语速。
    """

    if slot_ms <= 0 or audio_ms <= slot_ms:
        return "none", 1.0
    room = max(room_ms, slot_ms)
    if audio_ms <= room:
        return "absorb", 1.0
    ratio = audio_ms / room
    if ratio <= tempo_limit:
        return "atempo", ratio
    return "manual", tempo_limit


def rate_ladder(base_rate: str | None) -> list[str]:
    """基础语速之上的候选档位（Edge-TTS 语法，已按 ``MAX_RATE_PCT`` 截断）。"""

    base_pct = parse_rate(base_rate) * 100
    ladder: list[str] = []
    for step in RATE_LADDER_PP:
        pct = min(base_pct + step, MAX_RATE_PCT)
        value = format_rate(pct / 100)
        if pct > base_pct + 1e-9 and value not in ladder:
            ladder.append(value)
    return ladder


def _choose_escalated_rate(
    audio_ms: int, slot_ms: int, room_ms: int, base_rate: str | None
) -> str | None:
    """选一个能把这条压进可用时间（留 ≤15% 给 atempo）的最小档位；不够就给最大档。"""

    target = max(room_ms, slot_ms)
    if audio_ms <= 0 or target <= 0:
        return None
    needed = audio_ms / target / ESCALATION_ATEMPO_HEADROOM  # 需要 TTS 自己再快多少倍
    base_mult = 1.0 + parse_rate(base_rate)
    candidates = rate_ladder(base_rate)
    if not candidates:
        return None
    fallback: str | None = None
    for rate in candidates:
        relative = (1.0 + parse_rate(rate)) / base_mult
        if relative >= needed:
            return rate
        fallback = rate
    return fallback


def run_dub(
    options: DubOptions,
    *,
    engine: TtsEngine | None = None,
    engines: dict[str, TtsEngine] | None = None,
) -> DubResult:
    options.validate()
    srt_path = Path(options.srt)
    if not srt_path.is_file():
        raise DubError(f"字幕文件不存在：{srt_path}")

    subtitle, _encoding = load_srt(srt_path)
    if len(subtitle) == 0:
        raise DubError("字幕里没有任何 cue，无可配音内容")

    if options.video is not None and not Path(options.video).is_file():
        raise DubError(f"视频文件不存在：{options.video}")

    out_dir = Path(options.out)
    cache_dir = out_dir / "tts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    video_ms = _media_duration_ms(Path(options.video)) if options.video is not None else None

    cue_list = list(subtitle.cues)
    warnings: list[str] = []
    # 多人声：cue → 说话人 → 音色。没给 speakers_path 时全片走 default voice，行为与 M10 完全一致。
    voice_map = {speaker: voice for speaker, voice in options.voice_by_speaker}
    speaker_by_cue: dict[int, str] = {}
    if options.speakers_path is not None:
        speakers_file = Path(options.speakers_path)
        if not speakers_file.is_file():
            raise DubError(f"说话人归属文件不存在：{speakers_file}")
        speaker_by_cue, ordered_speakers, load_warnings = _load_speaker_assignments(speakers_file)
        warnings.extend(load_warnings)
        for speaker in ordered_speakers:
            if speaker and speaker not in voice_map:
                warnings.append(f"说话人 {speaker} 没有分配音色，已用默认音色 {options.voice}")
    cue_speakers = [speaker_by_cue.get(index, "") for index in range(1, len(cue_list) + 1)]
    cue_voices = [voice_map.get(speaker, options.voice) for speaker in cue_speakers]

    # 每个音色 × 语速一个引擎（Edge-TTS 的音色/语速都是构造参数，不能在一个实例里换；
    # 提语速重合成时要拿"同音色、更高语速"的引擎）
    default_engine = engine or build_engine(options)
    engines_by_key: dict[tuple[str, str | None], TtsEngine] = {}
    for voice, injected in (engines or {}).items():
        engines_by_key[(voice, options.tts_rate)] = injected
    if engine is not None:
        engines_by_key[(options.voice, options.tts_rate)] = engine

    # 本地 TTS（OPT-11）：模型/类型只解析一次（探测 python 要起子进程）
    local_info: tuple[Path, str] | None = None

    def ensure_local_info() -> tuple[Path, str]:
        nonlocal local_info
        if local_info is None:
            local_info = _local_model_info(options)
        return local_info

    # 本地 TTS 做主渠道时，把音色统一解析成本地 spec（Edge 风格 id 落到语言默认音色），
    # 否则报告/缓存指纹里会一直显示用不了的 Edge 音色。
    if options.tts_provider == "local":
        _local_model, _local_kind = ensure_local_info()
        from . import localtts as _localtts

        _default_local = _localtts.default_local_voice(_local_model, options.tgt_lang)
        cue_voices = [
            _local_voice_spec(options, voice) or _default_local for voice in cue_voices
        ]
        if options.voice_by_speaker and len({voice for voice in cue_voices if voice}) <= 1:
            warnings.append(
                "本地 TTS 下没有可用的本地音色映射：所有说话人会共用同一个本地音色"
                "（多人声请用 --speaker-voices \"SPEAKER_00=50,SPEAKER_01=47\" 指定本地音色）"
            )

    def engine_for(voice: str, rate: str | None) -> TtsEngine:
        key = (voice, rate)
        if key not in engines_by_key:
            engines_by_key[key] = build_engine(options, voice, rate=rate)
        return engines_by_key[key]

    def audio_path(index: int, rate: str | None, *, voice: str | None = None) -> Path:
        selected = cue_voices[index - 1] if voice is None else voice
        tag, suffix = engine_tag(options, selected, rate, local_info=local_info)
        return _cue_audio_path(
            cache_dir,
            index,
            text=cue_list[index - 1].text,
            voice=selected,
            rate=rate,
            command=options.tts_command,
            engine=tag,
            suffix=suffix,
        )

    # 每条最终使用的音频路径（Edge 或本地兜底都记这里；后续量时长/解码都用它）
    final_paths: dict[int, Path] = {}

    def room_of(index: int) -> int:
        """这条开始到下一条开始之间的可用时间（最后一条用视频长度兜底）。"""

        cue = cue_list[index - 1]
        slot = cue.timing.duration_ms
        if index < len(cue_list):
            return max(slot, cue_list[index].timing.start_ms - cue.timing.start_ms)
        return max(slot, (video_ms or 0) - cue.timing.start_ms)

    # 1) 并发合成（AC-7）：每条独立重试，失败不影响其他条目（AC-9）
    #    内容指纹命中的音频直接复用（云端 TTS 按字符计费，重跑不该重复扣费）；
    #    `--regenerate-tts` 时强制重新合成。
    outcomes: dict[int, tuple[int, str | None]] = {}
    # (cue, 语速) → 是否命中内容指纹缓存（提语速后那条是重新合成的，不能算复用）
    cache_hits: dict[tuple[int, str | None], bool] = {}

    def synth(index: int, rate: str | None) -> tuple[int, str | None]:
        path = audio_path(index, rate)
        if options.reuse_tts and path.is_file() and path.stat().st_size > 0:
            cache_hits[(index, rate)] = True
            final_paths[index] = path
            return 0, None
        cache_hits[(index, rate)] = False
        try:
            attempts = _synth_with_retries(
                engine_for(cue_voices[index - 1], rate),
                cue_list[index - 1].text,
                path,
                retries=options.retries,
                retry_delay_s=options.retry_delay_s,
            )
            final_paths[index] = path
            return attempts, None
        except Exception as exc:  # noqa: BLE001 - 引擎异常类型不可控，按单条失败处理
            return 0, str(exc)

    def synth_batch(
        indexes: list[int], rate_of: dict[int, str | None], *, workers: int | None = None
    ) -> None:
        if not indexes:
            return
        with ThreadPoolExecutor(max_workers=workers or options.concurrency) as pool:
            futures = {
                pool.submit(synth, index, rate_of.get(index)): index for index in indexes
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcomes[index] = future.result()
                except Exception as exc:  # noqa: BLE001
                    outcomes[index] = (0, str(exc))

    indexes = list(range(1, len(cue_list) + 1))
    rates: dict[int, str | None] = {index: options.tts_rate for index in indexes}
    synth_batch(indexes, rates)

    # 1.05) 二次补跑（OPT-5）：高并发下 Edge-TTS 偶发连接错误（实测并发 10 时 86 条里有
    #      9 条 `Cannot connect to host speech.platform.bing.com`）。这类是网络抖动、
    #      不是配置问题：降并发再补一轮，能救回就不该标 failed。
    failed_after_first = [index for index in indexes if outcomes[index][1] is not None]
    if failed_after_first:
        synth_batch(
            failed_after_first,
            {index: options.tts_rate for index in failed_after_first},
            workers=max(1, min(3, len(failed_after_first))),
        )

    # 1.1) 音色下线降级（OPT-4）：NoAudioReceived / 无效音色 → 该条改用默认音色重合成一次。
    #      微软下线音色是真实发生过的（en-US-DavisNeural），不能因为多音色里有一个坏音色
    #      就让整条链路 partial；降级要如实写进告警和报告。
    downgrades: list[dict[str, object]] = []
    if options.voice_by_speaker:
        retry_indexes = [
            index
            for index in indexes
            if outcomes[index][1] is not None
            and cue_voices[index - 1] != options.voice
            and _looks_like_voice_error(outcomes[index][1])
        ]
        if retry_indexes:
            for index in retry_indexes:
                downgrades.append(
                    {
                        "cue": index,
                        "from": cue_voices[index - 1],
                        "to": options.voice,
                        "error": str(outcomes[index][1])[:300],
                    }
                )
                cue_voices[index - 1] = options.voice
            synth_batch(retry_indexes, rates)
            grouped: dict[tuple[str, str], list[int]] = {}
            for item in downgrades:
                grouped.setdefault((str(item["from"]), str(item["to"])), []).append(
                    int(item["cue"])
                )
            for (source_voice, target_voice), cues in grouped.items():
                warnings.append(
                    f"音色 {source_voice} 不可用（NoAudioReceived/无效音色），已把第 "
                    f"{'、'.join(str(cue) for cue in cues)} 条降级为默认音色 {target_voice}"
                    "（其余条目不受影响）"
                )

    # 1.2) 本地 TTS 兜底（OPT-11）：Edge 走网络失败（端点不可达/SSL 抖动/断网）时，
    #      用本地 sherpa-onnx 把该条重新合成——宁可音色差一点，也不因为网络让整片 partial。
    fallback_events: list[dict[str, object]] = []
    fallback_set: set[int] = set()
    if options.tts_fallback == "local" and options.tts_provider != "local":
        pending_fallback = [index for index in indexes if outcomes[index][1] is not None]
        if pending_fallback:
            fallback_engine = None
            local_voice = ""
            local_tag = ""
            try:
                from . import localtts as _localtts

                _model, _kind = ensure_local_info()
                local_voice = (
                    options.local_tts_voice
                    or _localtts.default_local_voice(_model, options.tgt_lang)
                )
                fallback_speed = _local_speed(options, options.tts_rate)
                local_tag = f"local:{_model.name}:{_kind}:{fallback_speed:.3f}"
                fallback_engine = _localtts.LocalTtsEngine(
                    _localtts.LocalTtsOptions(
                        model=_model,
                        kind=_kind,
                        python=_cached_local_python(options.local_tts_python),
                        threads=options.local_tts_threads,
                    ),
                    voice=local_voice,
                    language=options.tgt_lang,
                    speed=fallback_speed,
                )
            except Exception as exc:  # noqa: BLE001 - 兜底不可用不能拖垮主流程
                warnings.append(f"Edge-TTS 失败且本地兜底不可用（{exc}），失败条目按失败处理")
                fallback_engine = None
            if fallback_engine is not None:
                for index in pending_fallback:
                    path = _cue_audio_path(
                        cache_dir,
                        index,
                        text=cue_list[index - 1].text,
                        voice=local_voice,
                        rate=options.tts_rate,
                        command=None,
                        engine=local_tag,
                        suffix=".wav",
                    )
                    previous_error = str(outcomes[index][1])
                    try:
                        attempts = _synth_with_retries(
                            fallback_engine,
                            cue_list[index - 1].text,
                            path,
                            retries=0,
                            retry_delay_s=0.0,
                        )
                    except Exception as exc:  # noqa: BLE001
                        outcomes[index] = (
                            outcomes[index][0],
                            f"{previous_error}；本地兜底也失败：{exc}",
                        )
                        continue
                    final_paths[index] = path
                    fallback_set.add(index)
                    cue_voices[index - 1] = local_voice
                    outcomes[index] = (outcomes[index][0] + attempts, None)
                    fallback_events.append(
                        {
                            "cue": index,
                            "from": "edge",
                            "to": "local",
                            "voice": local_voice,
                            "error": previous_error[:300],
                        }
                    )
                if fallback_events:
                    listed = "、".join(str(item["cue"]) for item in fallback_events[:10])
                    more = "…" if len(fallback_events) > 10 else ""
                    warnings.append(
                        f"Edge-TTS 失败 {len(fallback_events)} 条，已用本地 TTS 兜底"
                        f"（音色 {local_voice}）；第 {listed} 条{more}"
                        "（本地音色与 Edge 不同属正常）"
                    )
                    if options.voice_by_speaker:
                        warnings.append(
                            "本地兜底时多人声统一用同一个本地音色（不同说话人不再区分）"
                        )

    # 2) 量时长（原始语速下真实说多久）
    audio_ms: dict[int, int] = {}
    for index in indexes:
        error = outcomes[index][1]
        if error is not None:
            continue
        try:
            audio_ms[index] = _media_duration_ms(final_paths[index])
        except DubError as exc:
            outcomes[index] = (outcomes[index][0], str(exc))

    # 2.1) 自适应提语速（OPT-2）：怎么都放不进可用时间的条目，用同一音色提一档语速重合成。
    #      TTS 自然加速（保留语调、只是说得快）比 atempo 硬压好听，所以优先走这条；
    #      只对"连 atempo 上限都救不回来"的条目做，避免白白多花 TTS 调用。
    escalated: dict[int, str] = {}
    escalation_notes: list[str] = []
    if options.adaptive_rate and not options.tts_command:
        targets: list[tuple[int, str]] = []
        for index in indexes:
            if outcomes[index][1] is not None or index not in audio_ms:
                continue
            cue = cue_list[index - 1]
            action, _tempo = _plan_action(
                audio_ms[index], cue.timing.duration_ms, room_of(index), options.tempo_limit
            )
            if action != "manual":
                continue
            rate = _choose_escalated_rate(
                audio_ms[index],
                cue.timing.duration_ms,
                room_of(index),
                options.tts_rate,
            )
            if rate:
                targets.append((index, rate))
        if targets:
            failure_backup = {index: outcomes[index] for index, _ in targets}
            synth_batch(
                [index for index, _ in targets], {index: rate for index, rate in targets}
            )
            for index, rate in targets:
                _attempts, error = outcomes[index]
                if error is not None:
                    # 提语速失败：原来那条基准语速音频还在，照旧用它，只如实告警
                    outcomes[index] = failure_backup[index]
                    escalation_notes.append(
                        f"第 {index} 条提语速到 {rate} 失败，保留原速音频：{error}"
                    )
                    continue
                try:
                    audio_ms[index] = _media_duration_ms(final_paths[index])
                except DubError as exc:
                    outcomes[index] = failure_backup[index]
                    escalation_notes.append(f"第 {index} 条提语速后读不出时长，保留原速音频：{exc}")
                    continue
                rates[index] = rate
                escalated[index] = rate
            if escalation_notes:
                warnings.extend(escalation_notes)

    # 3) 逐条算时长与动作（AC-8：覆盖率 100%，溢出 >10% 必须列出）
    entries: list[dict[str, object]] = []
    placements: list[tuple[int, array.array]] = []
    failures: list[dict[str, object]] = []
    longest_end_ms = 0

    for index, cue in enumerate(cue_list, start=1):
        attempts, error = outcomes[index]
        slot_ms = cue.timing.duration_ms
        room_ms = room_of(index)

        entry: dict[str, object] = {
            "cue": index,
            "text": cue.text,
            "chars": len(cue.text),
            "reused": cache_hits.get((index, rates[index]), False),
            "speaker": cue_speakers[index - 1] or None,
            "voice": cue_voices[index - 1],
            "provider": "local"
            if options.tts_provider == "local" or index in fallback_set
            else ("command" if options.tts_command else "edge"),
            "fallback": index in fallback_set,
            "rate": rates[index],
            "rate_escalated": index in escalated,
            "slot_ms": slot_ms,
            "room_ms": room_ms,
            "audio_ms": 0,
            "played_ms": 0,
            "overflow_pct": 0.0,
            "played_overflow_pct": 0.0,
            "action": "none",
            "atempo": 1.0,
            "attempts": attempts,
            "error": error,
        }

        if error is not None:
            entry["action"] = "failed"
            entries.append(entry)
            failures.append({"cue": index, "error": error})
            continue

        final_audio_ms = audio_ms[index]
        action, tempo = _plan_action(final_audio_ms, slot_ms, room_ms, options.tempo_limit)
        overflow_pct = (final_audio_ms - slot_ms) / slot_ms * 100 if slot_ms > 0 else 0.0
        played_ms = final_audio_ms if abs(tempo - 1.0) < 1e-9 else round(final_audio_ms / tempo)
        played_overflow = (played_ms - slot_ms) / slot_ms * 100 if slot_ms > 0 else 0.0

        entry.update(
            audio_ms=final_audio_ms,
            played_ms=played_ms,
            overflow_pct=round(overflow_pct, 1),
            played_overflow_pct=round(played_overflow, 1),
            action=action,
            atempo=round(tempo, 3),
        )
        entries.append(entry)

        pcm = _decode_to_pcm(final_paths[index], tempo, options.sample_rate)
        start_ms = cue.timing.start_ms
        placements.append((start_ms, pcm))
        longest_end_ms = max(
            longest_end_ms,
            cue.timing.end_ms,
            start_ms + int(len(pcm) * 1000 / options.sample_rate),
        )

    # 3) 铺轨：每条按自己的槽位起点写入，不拼接、不顺延
    #    视频比字幕长时，音轨要补到视频长度，否则 -shortest 会把画面截短（AC-14）
    if options.video is not None:
        longest_end_ms = max(longest_end_ms, video_ms or 0)
    total_samples = int(longest_end_ms * options.sample_rate / 1000) + options.sample_rate // 10
    master = array.array("h", bytes(2 * total_samples))
    for start_ms, pcm in placements:
        offset = int(start_ms * options.sample_rate / 1000)
        end = min(len(master), offset + len(pcm))
        if end <= offset:
            continue
        master[offset:end] = pcm[: end - offset]

    audio_path = out_dir / "dubbed.wav"
    _write_wav(audio_path, master, options.sample_rate)

    # 4) 可选：保留 BGM（AC-21）——拆伴奏 → 侧链混音（配音一响伴奏让路）→ 并进视频
    bgm_result: BgmResult | None = None
    mixed_path: Path | None = None
    # 配音轨是 24k 单声道；混音要按音乐规格走 44.1k 立体声（M9 实测：24k 单声道会把伴奏砍闷）
    bgm_sample_rate = max(44100, options.sample_rate)
    if options.keep_bgm:
        source = Path(options.bgm_source or options.video)
        try:
            bgm_result = run_bgm(
                BgmOptions(
                    source=source,
                    out_dir=out_dir / "bgm",
                    mode=options.bgm_mode,
                    separator_python=options.separator_python,
                    bgm_gain_db=options.bgm_gain_db,
                    duck_db=options.bgm_duck_db,
                    mix_sample_rate=bgm_sample_rate,
                    mix_channels=2,
                ),
                voice=audio_path,
            )
        except BgmError as exc:
            raise DubError(f"BGM 处理失败：{exc}") from exc
        mixed_path = bgm_result.mixed
        warnings.extend(bgm_result.notes)
    mux_audio = mixed_path or audio_path

    # 5) 可选：直接并进视频（AC-14：h264 + aac，时长差 ≤0.5s）
    video_path: Path | None = None
    video_transcoded = False
    if options.video is not None:
        video_path = out_dir / "dubbed.mp4"
        head = [
            "-i",
            str(options.video),
            "-i",
            str(mux_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
        ]
        try:
            if _video_codec(Path(options.video)) == "h264":
                _run([*head, "-c:v", "copy", str(video_path)], what="合成配音视频")
            else:
                raise DubError("源视频不是 h264，需要转码")
        except DubError:
            # VP9/HEVC/ProRes 等：统一转 h264，保证"h264 + aac"的成品口径（AC-14）
            video_path.unlink(missing_ok=True)
            video_transcoded = True
            _run(
                [
                    *head,
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    str(video_path),
                ],
                what="合成配音视频（视频转码）",
            )

    wall_s = time.monotonic() - started_at
    overflow_listed = [entry for entry in entries if float(entry["overflow_pct"]) > OVERFLOW_MANUAL_THRESHOLD_PCT]
    played_overflow_listed = [
        entry
        for entry in entries
        if float(entry["played_overflow_pct"]) > OVERFLOW_MANUAL_THRESHOLD_PCT
    ]
    manual = [entry for entry in entries if entry["action"] == "manual"]
    atempo = [entry for entry in entries if entry["action"] == "atempo"]
    absorbed = [entry for entry in entries if entry["action"] == "absorb"]
    fits_room = [
        entry
        for entry in entries
        if entry["action"] != "failed" and int(entry["played_ms"]) <= int(entry["room_ms"])
    ]
    status = "ok" if not failures else "partial"

    voice_stats: list[dict[str, object]] = []
    for voice in dict.fromkeys(cue_voices):
        index_list = [index for index, item in enumerate(cue_voices) if item == voice]
        voice_stats.append(
            {
                "voice": voice,
                "cues": len(index_list),
                "speakers": sorted(
                    {cue_speakers[index] for index in index_list if cue_speakers[index]}
                ),
            }
        )

    srt_size, srt_sha = file_digest(srt_path)
    speakers_meta: dict[str, object] | None = None
    if options.speakers_path is not None:
        speakers_file = Path(options.speakers_path)
        speakers_size, speakers_sha = file_digest(speakers_file)
        speakers_meta = {
            "path": str(speakers_file.resolve()),
            "bytes": speakers_size,
            "sha256": speakers_sha,
            "cues": len(speaker_by_cue),
        }
    generated_at = options.generated_at or datetime.now().astimezone()
    report: dict[str, object] = {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "dub",
        "generated_at": generated_at.isoformat(),
        "status": status,
        "input": {
            "srt": {"path": str(srt_path.resolve()), "bytes": srt_size, "sha256": srt_sha, "cues": len(cue_list)},
            "speakers": speakers_meta,
            "video": str(Path(options.video).resolve()) if options.video is not None else None,
        },
        "config": {
            "voice": options.voice,
            "voice_by_speaker": dict(voice_map),
            "speakers_path": str(Path(options.speakers_path).resolve())
            if options.speakers_path is not None
            else None,
            "tts_rate": options.tts_rate,
            "adaptive_rate": options.adaptive_rate and not options.tts_command,
            "rate_ladder": rate_ladder(options.tts_rate)
            if options.adaptive_rate and not options.tts_command
            else [],
            "max_rate_pct": MAX_RATE_PCT,
            "tts_command_sha256": hashlib.sha256(options.tts_command.encode("utf-8")).hexdigest()
            if options.tts_command
            else None,
            "concurrency": options.concurrency,
            "retries": options.retries,
            "tempo_limit": options.tempo_limit,
            "sample_rate": options.sample_rate,
            "engine": type(default_engine).__name__,
            "tts_python": getattr(default_engine, "python", None),
            # 本地 TTS（OPT-11）：provider=local 时是主渠道；primary=edge 时 fallback 决定是否兜底
            "tts_provider": "command"
            if options.tts_command
            else options.tts_provider,
            "tts_fallback": options.tts_fallback
            if options.tts_provider == "edge" and not options.tts_command
            else "off",
            "local_tts": {
                "model": str(local_info[0]) if local_info is not None else None,
                "kind": local_info[1] if local_info is not None else None,
                "voice": options.local_tts_voice or None,
                "speed": _local_speed(options, options.tts_rate)
                if local_info is not None
                else None,
                "threads": options.local_tts_threads if local_info is not None else None,
                "fingerprint": (
                    f"{local_info[0].name}:{local_info[1]}:"
                    f"{options.local_tts_voice}:{options.tgt_lang}"
                )
                if local_info is not None
                else None,
            },
            "tgt_lang": options.tgt_lang,
            "bgm": {
                "enabled": options.keep_bgm,
                "mode": options.bgm_mode if options.keep_bgm else None,
                # 从哪条音轨取 BGM：换源文件混出来的就是另一份音频，必须进报告（缓存判据）
                "source": str(Path(options.bgm_source or options.video).resolve())
                if options.keep_bgm and (options.bgm_source or options.video)
                else None,
                "gain_db": options.bgm_gain_db,
                "duck_db": options.bgm_duck_db,
                "mix_sample_rate": bgm_sample_rate if options.keep_bgm else None,
                "mix_channels": 2 if options.keep_bgm else None,
            },
        },
        "summary": {
            "cues": len(cue_list),
            "synthesized": len(cue_list) - len(failures),
            "reused": sum(
                1 for index in indexes if cache_hits.get((index, rates[index]), False)
            ),
            "failed": len(failures),
            "overflow_over_10pct": len(overflow_listed),
            "played_overflow_over_10pct": len(played_overflow_listed),
            "fits_room": len(fits_room),
            "rate_escalated": len(escalated),
            "voice_downgrades": downgrades,
            "tts_fallback": {
                "allowed": options.tts_fallback
                if options.tts_provider == "edge" and not options.tts_command
                else "off",
                "events": fallback_events,
            },
            "local_tts": {
                "used": bool(local_info is not None),
                "kind": local_info[1] if local_info is not None else None,
                "model": str(local_info[0]) if local_info is not None else None,
                "voices": sorted({str(item["voice"]) for item in fallback_events})
                if fallback_events
                else (
                    sorted({voice for voice in cue_voices if voice})
                    if options.tts_provider == "local"
                    else []
                ),
            },
            "manual": len(manual),
            "atempo": len(atempo),
            "absorbed": len(absorbed),
            "speakers": len({speaker for speaker in cue_speakers if speaker}),
            "voices": voice_stats,
            "total_audio_ms": longest_end_ms,
            "bgm": {
                "mode": bgm_result.mode,
                "separator": bgm_result.separator,
                "wall_s": bgm_result.wall_s,
                "mix_sample_rate": bgm_result.mix_sample_rate,
                "mix_channels": bgm_result.mix_channels,
            }
            if bgm_result is not None
            else None,
            "wall_s": round(wall_s, 2),
        },
        "cues": entries,
        "failures": failures,
        "warnings": warnings,
        "outputs": {
            "audio": str(audio_path.resolve()),
            "mixed": str(mixed_path.resolve()) if mixed_path is not None else None,
            "video": str(video_path.resolve()) if video_path is not None else None,
            "video_transcoded": video_transcoded,
        },
    }
    # 收尾：关掉本地 TTS 常驻 worker（常驻进程能省模型加载，但跑完要释放内存）
    try:
        from . import localtts as _localtts

        _localtts.close_all_workers()
    except Exception:  # noqa: BLE001 - 清理失败不影响成品
        pass

    report_path = out_dir / "dub_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    return DubResult(
        status=status,
        exit_code=0 if status == "ok" else 2,
        report=report,
        report_path=report_path.resolve(),
        audio_path=audio_path.resolve(),
        video_path=video_path.resolve() if video_path is not None else None,
    )
