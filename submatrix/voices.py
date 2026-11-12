"""音色目录 + 试听（M16）：给 GUI 提供「下拉选音色 / 点一下听效果」的薄层。

为什么需要这一层：

- 默认音色是 ``zh-CN-XiaoxiaoNeural`` / ``en-US-AriaNeural``，但用户要换音色时
  只能手敲 Edge-TTS 的 voice id——记不住、拼错要到配音阶段才报错；
- 收费软件（剪映/Descript）都是「音色列表 + 分类 + 试听」，这部分是纯前端体验，
  不该拖进编排层。

做法：

1. **全量目录**来自本机 ``edge-tts``（``edge_tts.list_voices()``，322 条），只保留
   中/英与多语言音色，缓存到 ``work/voices/edge-voices.json``（默认 30 天）；
2. **离线兜底**是一张精选静态表（中英常用音色），没装/没网时下拉仍然可用；
3. 试听直接复用 :class:`submatrix.tts.EdgeTtsEngine` 合成一句样音，按音色+文本指纹
   缓存，重复点不重复合成。

这一层不碰配音编排：``dub``/``pipeline`` 仍然只认 voice id 字符串。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import atomic
from .tts import EdgeTtsEngine, TtsEngine, TtsError, find_edge_tts_python

__all__ = [
    "DEFAULT_LABEL",
    "PREVIEW_TEXTS",
    "Voice",
    "VoicesError",
    "default_voice",
    "discover_voices",
    "preview_voice",
    "voice_choices",
    "voice_id",
]

EXIT_OK = 0
CACHE_TTL_S = 30 * 24 * 3600.0

DEFAULT_LABEL = "默认（按目标语言）"

PREVIEW_TEXTS = {
    "zh": "你好，这是配音试听。这句话用当前选择的音色朗读，用来判断音色是否合适。",
    "en": "Hello, this is a voice preview. This sentence is read by the selected voice.",
}

# 精选音色：中英常用 + 多语言音色。只是离线兜底的「顺序与昵称」，
# 真实可用列表以 edge-tts 返回为准（缺了会自然消失，多了会排在后面）。
_CURATED: tuple[tuple[str, str, str, str], ...] = (
    # (voice id, gender, locale, 中文昵称)
    ("zh-CN-XiaoxiaoNeural", "Female", "zh-CN", "晓晓"),
    ("zh-CN-YunxiNeural", "Male", "zh-CN", "云希"),
    ("zh-CN-YunjianNeural", "Male", "zh-CN", "云健"),
    ("zh-CN-XiaoyiNeural", "Female", "zh-CN", "晓伊"),
    ("zh-CN-YunxiaNeural", "Male", "zh-CN", "云夏"),
    ("zh-CN-YunyangNeural", "Male", "zh-CN", "云扬"),
    ("zh-CN-liaoning-XiaobeiNeural", "Female", "zh-CN", "晓北"),
    ("zh-CN-shaanxi-XiaoniNeural", "Female", "zh-CN", "晓妮"),
    ("zh-TW-HsiaoChenNeural", "Female", "zh-TW", "曉臻"),
    ("zh-TW-HsiaoYuNeural", "Female", "zh-TW", "曉雨"),
    ("zh-TW-YunJheNeural", "Male", "zh-TW", "雲哲"),
    ("zh-HK-HiuGaaiNeural", "Female", "zh-HK", "曉佳"),
    ("zh-HK-HiuMaanNeural", "Female", "zh-HK", "曉曼"),
    ("zh-HK-WanLungNeural", "Male", "zh-HK", "雲龍"),
    ("en-US-AriaNeural", "Female", "en-US", "Aria"),
    ("en-US-GuyNeural", "Male", "en-US", "Guy"),
    ("en-US-JennyNeural", "Female", "en-US", "Jenny"),
    ("en-US-AvaNeural", "Female", "en-US", "Ava"),
    ("en-US-EmmaNeural", "Female", "en-US", "Emma"),
    ("en-US-MichelleNeural", "Female", "en-US", "Michelle"),
    ("en-US-AnaNeural", "Female", "en-US", "Ana（童声）"),
    ("en-US-AndrewNeural", "Male", "en-US", "Andrew"),
    ("en-US-BrianNeural", "Male", "en-US", "Brian"),
    ("en-US-ChristopherNeural", "Male", "en-US", "Christopher"),
    ("en-US-EricNeural", "Male", "en-US", "Eric"),
    ("en-US-RogerNeural", "Male", "en-US", "Roger"),
    ("en-US-SteffanNeural", "Male", "en-US", "Steffan"),
    ("en-US-AvaMultilingualNeural", "Female", "en-US", "Ava"),
    ("en-US-EmmaMultilingualNeural", "Female", "en-US", "Emma"),
    ("en-US-AndrewMultilingualNeural", "Male", "en-US", "Andrew"),
    ("en-US-BrianMultilingualNeural", "Male", "en-US", "Brian"),
    ("en-GB-SoniaNeural", "Female", "en-GB", "Sonia"),
    ("en-GB-LibbyNeural", "Female", "en-GB", "Libby"),
    ("en-GB-MaisieNeural", "Female", "en-GB", "Maisie"),
    ("en-GB-RyanNeural", "Male", "en-GB", "Ryan"),
    ("en-GB-ThomasNeural", "Male", "en-GB", "Thomas"),
    ("en-AU-NatashaNeural", "Female", "en-AU", "Natasha"),
    ("en-AU-WilliamMultilingualNeural", "Male", "en-AU", "William"),
    ("en-CA-ClaraNeural", "Female", "en-CA", "Clara"),
    ("en-CA-LiamNeural", "Male", "en-CA", "Liam"),
    ("en-IN-NeerjaNeural", "Female", "en-IN", "Neerja"),
    ("en-IN-PrabhatNeural", "Male", "en-IN", "Prabhat"),
    ("en-IE-EmilyNeural", "Female", "en-IE", "Emily"),
    ("en-IE-ConnorNeural", "Male", "en-IE", "Connor"),
    ("en-NZ-MollyNeural", "Female", "en-NZ", "Molly"),
    ("en-NZ-MitchellNeural", "Male", "en-NZ", "Mitchell"),
    ("en-SG-LunaNeural", "Female", "en-SG", "Luna"),
    ("en-SG-WayneNeural", "Male", "en-SG", "Wayne"),
    ("en-ZA-LeahNeural", "Female", "en-ZA", "Leah"),
    ("en-ZA-LukeNeural", "Male", "en-ZA", "Luke"),
)

_CURATED_INDEX = {item[0]: index for index, item in enumerate(_CURATED)}
_NICKS = {item[0]: item[3] for item in _CURATED}
_GENDER_CN = {"Female": "女声", "Male": "男声"}

_CODE = (
    "import asyncio, json, edge_tts; "
    "data = asyncio.run(edge_tts.list_voices()); "
    "print(json.dumps(data, ensure_ascii=False))"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class VoicesError(RuntimeError):
    """音色目录不可用 / 试听合成失败。"""


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    gender: str
    locale: str

    @property
    def label(self) -> str:
        parts = [_display_name(self.id), _GENDER_CN.get(self.gender, self.gender or "未知")]
        if "Multilingual" in self.id:
            parts.append("多语言")
        return f"{self.id} · {'·'.join(part for part in parts if part)}"


def _display_name(voice: str) -> str:
    if voice in _NICKS:
        return _NICKS[voice]
    # en-US-JennyNeural → Jenny；zh-CN-XiaoxiaoNeural 没登记昵称时也退回英文名
    tail = voice.split("-")[-1]
    for suffix in ("MultilingualNeural", "Neural"):
        if tail.endswith(suffix) and tail != suffix:
            return tail[: -len(suffix)]
    return tail


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cache_path() -> Path:
    configured = os.environ.get("SUBMATRIX_VOICE_CACHE")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return _repo_root() / "work" / "voices" / "edge-voices.json"


def _static_voices() -> list[Voice]:
    return [Voice(id=item[0], gender=item[1], locale=item[2]) for item in _CURATED]


def _normalize(raw: Iterable[dict]) -> list[Voice]:
    """只留中/英与多语言音色，按 id 去重。"""

    voices: list[Voice] = []
    seen: set[str] = set()
    for item in raw:
        voice_id_value = str(item.get("ShortName") or item.get("id") or "").strip()
        locale = str(item.get("Locale") or item.get("locale") or "")
        gender = str(item.get("Gender") or item.get("gender") or "")
        if not voice_id_value or voice_id_value in seen:
            continue
        if not (locale.startswith("zh") or locale.startswith("en")):
            continue
        seen.add(voice_id_value)
        voices.append(Voice(id=voice_id_value, gender=gender, locale=locale))
    return voices


def _read_cache(path: Path) -> tuple[list[Voice], float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], 0.0
    if not isinstance(payload, dict):
        return [], 0.0
    fetched = float(payload.get("fetched_at") or 0.0)
    raw = payload.get("voices")
    if not isinstance(raw, list):
        return [], 0.0
    return _normalize(item for item in raw if isinstance(item, dict)), fetched


def _write_cache(path: Path, voices: Sequence[Voice]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "fetched_at": time.time(),
            "source": "edge_tts.list_voices",
            "voices": [
                {"ShortName": voice.id, "Gender": voice.gender, "Locale": voice.locale}
                for voice in voices
            ],
        }
        atomic.write_json(path, payload)
    except OSError:
        pass  # 缓存写不了不影响本次使用


def discover_voices(
    python: str | None = None,
    *,
    refresh: bool = False,
    cache_path: Path | None = None,
    ttl_s: float = CACHE_TTL_S,
    runner: Runner | None = None,
    now: Callable[[], float] = time.time,
) -> list[Voice]:
    """取中/英音色目录：优先本机 edge-tts，其次缓存，最后静态精选表。

    ``runner`` 只为测试注入；真实调用是 ``python -c "import edge_tts ..."``。
    """

    path = Path(cache_path) if cache_path is not None else _cache_path()
    cached, fetched_at = _read_cache(path)
    fresh = cached and (now() - fetched_at) < ttl_s
    if fresh and not refresh:
        return cached

    command = [python or find_edge_tts_python(), "-c", _CODE]
    # 30s 上限：拉列表是「锦上添花」，网络卡住时宁可回退静态表也别让 GUI 卡住
    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
    try:
        completed = run(command)
    except (OSError, subprocess.SubprocessError):
        return cached or _static_voices()
    if getattr(completed, "returncode", 1) != EXIT_OK:
        return cached or _static_voices()
    try:
        raw = json.loads((completed.stdout or "").strip() or "[]")
    except ValueError:
        return cached or _static_voices()
    voices = _normalize(item for item in raw if isinstance(item, dict))
    if not voices:
        return cached or _static_voices()
    _write_cache(path, voices)
    return voices


def _sorted_for(language: str, voices: Iterable[Voice]) -> list[Voice]:
    lang = (language or "en").lower()
    selected = [
        voice
        for voice in voices
        if voice.locale.lower().startswith(lang) or "Multilingual" in voice.id
    ]
    return sorted(
        selected,
        key=lambda voice: (
            0 if voice.id in _CURATED_INDEX else 1,
            _CURATED_INDEX.get(voice.id, len(_CURATED)),
            voice.id,
        ),
    )


def voice_choices(
    language: str = "en",
    *,
    voices: Sequence[Voice] | None = None,
) -> list[str]:
    """GUI 下拉的标签列表：第一项是「默认（按目标语言）」。"""

    catalog = list(voices) if voices is not None else discover_voices()
    return [DEFAULT_LABEL, *(voice.label for voice in _sorted_for(language, catalog))]


def voice_id(value: str | None) -> str:
    """下拉标签 / 手输值 → Edge-TTS voice id；「默认」返回空串。"""

    if value is None:
        return ""
    text = str(value).strip()
    if not text or text == DEFAULT_LABEL or text.startswith("默认（"):
        return ""
    head = text.split("·", 1)[0].strip()
    if head != text:
        return head
    # 手输可能带说明（"zh-CN-XiaoxiaoNeural 女声"），抓第一个 voice id 形态
    parts = text.replace("，", ",").split()
    for part in parts:
        stripped = part.strip(",")
        if "-" in stripped and stripped.lower().endswith("neural"):
            return stripped
    return text


def default_voice(language: str | None) -> str:
    from .pipeline import DEFAULT_VOICES

    return DEFAULT_VOICES.get((language or "").lower()) or "en-US-AriaNeural"


def preview_voice(
    voice: str | None,
    language: str = "en",
    *,
    out_dir: Path | str | None = None,
    text: str | None = None,
    rate: str | None = None,
    python: str | None = None,
    engine: TtsEngine | None = None,
) -> Path:
    """合成一句试听音；同一音色+文本复用缓存文件。"""

    language = (language or "en").lower()
    voice_value = voice_id(voice) or default_voice(language)
    sample = text or PREVIEW_TEXTS.get(language) or PREVIEW_TEXTS["en"]
    base = Path(out_dir) if out_dir is not None else _repo_root() / "work" / "gui" / "preview"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VoicesError(f"试听目录不可用：{base}（{exc}）") from exc
    digest = hashlib.sha256(f"{voice_value}|{rate or ''}|{sample}".encode("utf-8")).hexdigest()[:10]
    out_path = base / f"{voice_value}-{digest}.mp3"
    if out_path.is_file() and out_path.stat().st_size > 0:
        return out_path

    tts = engine or EdgeTtsEngine(
        voice=voice_value,
        python=find_edge_tts_python(python),
        rate=rate,
    )
    try:
        tts.synthesize(sample, out_path)
    except (TtsError, OSError) as exc:
        raise VoicesError(f"试听合成失败（{voice_value}）：{exc}") from exc
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise VoicesError(f"试听文件没有生成：{out_path}")
    return out_path
