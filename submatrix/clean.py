"""过滤「音乐/噪声字幕」：ASR 把片尾音乐、掌声、环境声听成重复音节。

真实案例（CCTV 科普 154s，medium ASR）：片尾音乐被听成
``咪咪咪咪咪…``（一条 200+ 字的重复行），合并后送进 LLM 翻译，模型跟着退化成
``["Mi mi mi mi …``（不是 JSON 数组），translate 阶段整批失败、链路 partial。

这类 cue 没有翻译/配音价值，应在**进入翻译前**丢弃，并如实告警；同时把
``speakers.merged.json`` 的 cue 编号重排，保证多人声映射不错位。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import atomic
from .srt import Subtitle, dump_srt, load_srt

__all__ = ["CleanResult", "cleanup_srt", "cleanup_speakers", "is_degenerate_text"]

_PUNCT = set("，。！？、；：,.!?;:\"'“”‘’（）()[]【】{}<>…—–-~～·《》「」『』 \t\r\n")
_MIN_LEN = 6
_TOP_CHAR_RATIO = 0.7


def _normalized(text: str) -> str:
    return "".join(char for char in text if not char.isspace() and char not in _PUNCT)


def is_degenerate_text(text: str) -> bool:
    """单个字符/短音节高度重复 → 判为音乐或噪声，而不是台词。"""

    stripped = _normalized(text)
    if len(stripped) < _MIN_LEN:
        return False
    top = Counter(stripped).most_common(1)[0][1]
    if top / len(stripped) >= _TOP_CHAR_RATIO:
        return True
    # 2–4 字符的短模式整段循环（如「啦啦啦」「哈哈」重复）
    for period in range(1, 5):
        if len(stripped) < period * 4:
            continue
        unit = stripped[:period]
        repeated = (unit * (len(stripped) // period)) + unit[: len(stripped) % period]
        if repeated == stripped:
            return True
    return False


@dataclass(frozen=True, slots=True)
class CleanResult:
    srt_path: Path
    dropped: tuple[int, ...]  # 原始 1-based cue 序号
    kept: int


def cleanup_srt(src: str | Path, out: str | Path) -> CleanResult:
    """丢弃疑似音乐/噪声 cue；没有可丢的就原样返回源路径（不写文件）。

    除了重复音节本身，还会顺带丢掉紧挨着它的 1–2 字短 cue（同一批字符）——
    实测「咪咪咪…」前面常跟着一个孤零零的「咪」，不丢会译成 ``Mi.``。
    """

    source = Path(src)
    subtitle, _encoding = load_srt(source)
    texts = [cue.text for cue in subtitle.cues]
    dropped: set[int] = set()
    degenerate_chars: list[set[str]] = []
    for index, text in enumerate(texts, start=1):
        if is_degenerate_text(text):
            dropped.add(index)
            degenerate_chars.append(set(_normalized(text)))
    for index in list(dropped):
        normalized_neighbors = set()
        for neighbor in (index - 1, index + 1):
            if not 1 <= neighbor <= len(texts) or neighbor in dropped:
                continue
            short = _normalized(texts[neighbor - 1])
            chars = set().union(*degenerate_chars) if degenerate_chars else set()
            if short and len(short) <= 2 and set(short) <= chars:
                normalized_neighbors.add(neighbor)
        dropped |= normalized_neighbors

    kept = [cue for index, cue in enumerate(subtitle.cues, start=1) if index not in dropped]
    dropped_list = sorted(dropped)
    if not dropped_list:
        return CleanResult(source, (), len(kept))
    cleaned = Subtitle(cues=tuple(kept))
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_srt(cleaned), encoding="utf-8", newline="\n")
    return CleanResult(target, tuple(dropped_list), len(kept))


def cleanup_speakers(
    speakers_json: str | Path,
    dropped: tuple[int, ...],
    out: str | Path,
) -> Path | None:
    """按被丢弃的 cue 过滤说话人映射并重编号；没有映射变化时返回原路径。"""

    source = Path(speakers_json)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    cues = payload.get("cues")
    if not isinstance(cues, list):
        return None
    dropped_set = set(dropped)
    kept = [item for item in cues if int(item.get("cue") or 0) not in dropped_set]
    if len(kept) == len(cues):
        return source
    for new_index, item in enumerate(kept, start=1):
        item["cue"] = new_index
    payload["cues"] = kept
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(target, payload)
    return target
