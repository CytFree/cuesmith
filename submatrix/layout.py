"""字幕排版：按宽度折行 + 超过行数就切条。

这是 en→zh 字幕"能不能直接发布"的关键一层（收费字幕软件都会做）：
1. **按宽度折行**：英文按词断、中文按字断，优先在标点后断；
2. **单条 ≤ max_lines 行**：超过就把这条切成多条，时间轴按文字长度比例分配；
3. **最短时长保护**：切得太碎没法读，宁可少切。

切条粒度**以译文句子为单位**（M10）：观众读的是译文，英文显示条不能出现
"Salt has even sparked" / "wars before." 这种半句。原文侧优先按自己的句子边界配对；
两侧句数不一致时，原文按译文的段落进度分数切（时间轴仍严格对齐）。

它是**显式开启的可选改写**（`build --wrap`）：默认行为仍是 FR-2 的"只报不改"。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from .langs import LANG_PROFILES
from .srt import Cue, Subtitle, Timing

if TYPE_CHECKING:  # 只为类型：运行时不把排版层绑到校验层
    from .validate import Limits

__all__ = [
    "LayoutOptions",
    "LayoutStats",
    "layout_pair",
    "wrap_text",
]

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 折行/切条时优先在这些字符之后断开
BREAK_AFTER = set("，。！？、；：,.!?;:")
MAX_PARTS = 12
# 英文切在虚词后面会让下一条以 "over the" / "with" 这类残句开头（M10 实测）
FUNCTION_WORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "be", "been", "being", "but", "by", "can", "could",
        "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "his", "if",
        "in", "into", "is", "it", "its", "may", "might", "must", "not", "of", "on", "or",
        "over", "per", "she", "should", "so", "than", "that", "the", "their", "then",
        "there", "these", "they", "this", "those", "through", "to", "under", "via", "was",
        "we", "were", "when", "where", "which", "while", "who", "will", "with", "would",
        "you",
    }
)


# LayoutOptions 空构造的默认方向（en→zh）：排版层的历史默认，直接调用/测试依赖它。
# pipeline 走 ``from_limits()`` 显式传值，所以这里的方向不影响成品链路。
DEFAULT_SRC_LANG = "en"
DEFAULT_TGT_LANG = "zh"


@dataclass(frozen=True, slots=True)
class LayoutOptions:
    """排版参数。默认值从 ``LANG_PROFILES`` 推导，不另存一份字面量。"""

    src_width: int = field(default_factory=lambda: LANG_PROFILES[DEFAULT_SRC_LANG].width)
    tgt_width: int = field(default_factory=lambda: LANG_PROFILES[DEFAULT_TGT_LANG].width)
    max_lines: int = 2
    min_duration_ms: int = 400
    # 每种语言"每秒能读多少字"。时间分配必须按**语速**折算：
    # 英文 42 字符/行、中文 16 字/行，同样内容英文的字符数约是中文的 3 倍，
    # 只按字符数分时间会让中文 CPS 忽高忽低（实测 50/57 条误报超限）。
    src_cps: float = field(default_factory=lambda: LANG_PROFILES[DEFAULT_SRC_LANG].cps)
    tgt_cps: float = field(default_factory=lambda: LANG_PROFILES[DEFAULT_TGT_LANG].cps)

    @classmethod
    def from_limits(cls, limits: "Limits") -> "LayoutOptions":
        """按校验阈值构造排版参数。

        同一对字幕的"合规线"（validate）和"排版线"（layout）必须同源，
        否则会出现"校验说超宽、排版却按另一套折行"。matrix 走这一条。
        """

        return cls(
            src_width=limits.width_src,
            tgt_width=limits.width_tgt,
            max_lines=limits.max_lines,
            src_cps=limits.cps_src,
            tgt_cps=limits.cps_tgt,
        )


@dataclass(frozen=True, slots=True)
class LayoutStats:
    cues_before: int
    cues_after: int
    split_cues: int


def _is_cjk(text: str) -> bool:
    return bool(CJK_PATTERN.search(text))


def wrap_text(text: str, width: int) -> list[str]:
    """把一条字幕折成若干行（自动判断中/英）。"""

    flattened = " ".join(text.split())
    if not flattened:
        return []
    if width <= 0:
        return [flattened]
    return _wrap_cjk(flattened, width) if _is_cjk(flattened) else _wrap_latin(flattened, width)


def _wrap_latin(text: str, width: int) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)

    # 单个词比整行还长时只能硬切（罕见）
    output: list[str] = []
    for line in lines:
        while len(line) > width:
            output.append(line[:width])
            line = line[width:]
        if line:
            output.append(line)
    return output


_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[’'\-\.][A-Za-z0-9]+)*|.", re.DOTALL)
_LATIN_CHAR = re.compile(r"[A-Za-z0-9]")


def _avoid_word_split(text: str, index: int) -> int:
    """把切点从拉丁词内部挪到词边界（``Blen|der`` 这种切法在字幕里是硬伤）。"""

    if not (0 < index < len(text)):
        return index
    if not (_LATIN_CHAR.match(text[index - 1]) and _LATIN_CHAR.match(text[index])):
        return index
    start = index
    while start > 0 and _LATIN_CHAR.match(text[start - 1]):
        start -= 1
    end = index
    while end < len(text) and _LATIN_CHAR.match(text[end]):
        end += 1
    if start == 0:
        return end  # 词在开头：整词留给下一段
    if end == len(text):
        return start
    return start if index - start <= end - index else end


CJK_END_CHARS = set("了的地得个些吗呢吧啊呀哦嘛与和及或而就也都很在是有对从到把被让给为以过们他说着")
CJK_START_CHARS = set("我你他她它这那谁什怎但而因所如虽即此其上下中前后里外又还也才再最更没不")


def _avoid_cjk_word_split(text: str, cut: int, window: int = 6) -> int:
    """中文没有词边界，切条时尽量别落在词中间（"其|他动物" → "通过|其他动物"）。

    用一张很小的"词尾/词头高频字"表给候选切点打分：切在"了/的/过/是"等后面、
    或"其/这/我/他"等前面更像词边界；离原切点越远扣分越少。
    """

    if not (0 < cut < len(text)):
        return cut
    if not (_is_cjk(text[cut - 1]) and _is_cjk(text[cut])):
        return cut
    best_position = cut
    best_score = -1.0
    for position in range(max(1, cut - window), min(len(text), cut + window + 1)):
        left = text[position - 1]
        right = text[position]
        score = 0.0
        if left in BREAK_AFTER:
            score += 1.2
        if left in CJK_END_CHARS:
            score += 0.8
        if right in CJK_START_CHARS:
            score += 0.7
        score -= abs(position - cut) * 0.05
        if score > best_score:
            best_score = score
            best_position = position
    return best_position


def _wrap_cjk(text: str, width: int) -> list[str]:
    """中文（含夹杂英文专名）折行：按显示宽度，不拆拉丁词，优先在标点后断。"""

    tokens = _TOKEN.findall(text)
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            lines.append("".join(current))
            current = []

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not current:
            if len(token) > width:  # 超长拉丁词只能硬切
                lines.extend(token[offset : offset + width] for offset in range(0, len(token), width))
                index += 1
                continue
            current = [token]
            index += 1
            continue

        if sum(len(part) for part in current) + len(token) <= width:
            current.append(token)
            index += 1
            continue

        # 溢出：优先在已有标点后断行——但只有当标点靠近行尾时才用，
        # 否则一个开头的逗号（"其实，"）会让整行只剩 3 个字（M10 实测 3 行残行）。
        cut = max(
            (position for position, part in enumerate(current[:-1]) if part in BREAK_AFTER),
            default=None,
        )
        if cut is not None and len(current) - 1 - cut <= 4:
            lines.append("".join(current[: cut + 1]))
            current = current[cut + 1 :]
            continue
        flush()
    flush()

    # 别让标点独占一行、也别为了合并标点把上一行撑超宽：
    # 从上一行尾部借字，保证每行都 ≤ width 且不在行首出现标点
    balanced: list[str] = []
    for line in lines:
        if not balanced:
            balanced.append(line)
            continue
        orphan = all(char in BREAK_AFTER for char in line) or len(line) < 2
        if orphan and len(balanced[-1]) + len(line) <= width:
            balanced[-1] += line
            continue
        if orphan:
            need = 2 - len(line)
            budget = max(0, len(balanced[-1]) - 1)
            take = min(max(need, 0), budget)
            if take:
                line = balanced[-1][-take:] + line
                balanced[-1] = balanced[-1][:-take]
        balanced.append(line)
    return balanced


SENTENCE_UNIT = re.compile(r"(?<=[。！？.!?…])\s*")
CLAUSE_UNIT = re.compile(r"(?<=[，、；：,;:])\s*")


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _units(text: str, pattern: re.Pattern[str]) -> list[str]:
    parts = [part.strip() for part in pattern.split(text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    last = left[-1]
    first = right[0]
    if last.isspace():
        return left + right
    if last in "。！？…，、；：":
        return left + right
    if last in ".!?,;:":
        # 英文标点后接英文/数字要补空格（否则会出现 "Park,there's" 这种粘连）
        return f"{left} {right}" if first.isascii() and first.isalnum() else left + right
    if first.isascii() and first.isalnum() and not _is_cjk(last + first):
        return f"{left} {right}"
    return left + right


def _split_chars(text: str, parts: int) -> list[str]:
    """没有标点可用时的兜底：中文按字、英文按词均分；词数不够时硬切。"""

    if _is_cjk(text):
        length = math.ceil(len(text) / parts)
        pieces = [text[i * length : (i + 1) * length].strip() for i in range(parts)]
    else:
        words = text.split()
        base, extra = divmod(len(words), parts)
        pieces = []
        cursor = 0
        for index in range(parts):
            size = base + (1 if index < extra else 0)
            if size:
                pieces.append(" ".join(words[cursor : cursor + size]))
            cursor += size

    pieces = [piece for piece in pieces if piece]
    # 词数/字数不够时硬切最长的一段，保证段数与调用方一致
    while len(pieces) < parts:
        longest = max(range(len(pieces)), key=lambda index: len(pieces[index]))
        piece = pieces[longest]
        if len(piece) < 2:
            break
        middle = len(piece) // 2
        pieces[longest : longest + 1] = [piece[:middle].strip(), piece[middle:].strip()]
    return pieces[:parts] or [text]


def _chunk_unit(unit: str, capacity: int) -> list[str]:
    """一个单元本身超过容量时，按词/字切成不超过容量的块。"""

    if capacity <= 0:
        return [unit]
    if _is_cjk(unit):
        return [unit[index : index + capacity] for index in range(0, len(unit), capacity)]
    words = unit.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= capacity or not current:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _pack(units: list[str], capacity: int) -> list[str]:
    """把单元贪心装进"每段 ≤ capacity 字符"的组里。"""

    groups: list[str] = []
    current = ""
    for unit in units:
        candidate = _join(current, unit)
        if current and len(candidate) > capacity:
            groups.append(current)
            current = unit
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups or [""]


def _adjust(groups: list[str], target: int) -> list[str]:
    """把分组数调整到 target：多了合并相邻最短的，少了切最长的一段。"""

    while len(groups) > target:
        best = min(
            range(len(groups) - 1), key=lambda index: len(groups[index]) + len(groups[index + 1])
        )
        groups[best : best + 2] = [_join(groups[best], groups[best + 1])]
    while len(groups) < target:
        longest = max(range(len(groups)), key=lambda index: len(groups[index]))
        piece = groups[longest]
        if len(piece) < 2:
            break
        middle = len(piece) // 2
        while 0 < middle < len(piece) and piece[middle - 1] not in " ，。！？.!?、；：,;:":
            middle += 1
        if middle >= len(piece):
            middle = len(piece) // 2
        middle = _avoid_word_split(piece, middle)
        if not (0 < middle < len(piece)):
            # 整段就是一个不可拆的长词：只能硬切（保证条数与时间轴对得上）
            middle = len(piece) // 2
        groups[longest : longest + 1] = [piece[:middle].strip(), piece[middle:].strip()]
    return groups


def _allocate_timings(
    timing: Timing, weights: Sequence[float], *, min_duration_ms: int
) -> list[Timing]:
    """在一条 cue 的内部按权重分时间，并保证每段 ≥ min_duration、总和 == 原时长。

    先给每段留一个最小值，再把剩余时间按权重分——"先乘比例再抬到最小值"会把被抬高的
    那段时间从别的段里硬扣出来，实测出现过 276ms/11 字的 39.9 CPS 极端条。
    """

    parts = len(weights)
    span = timing.end_ms - timing.start_ms
    if parts <= 1 or span <= 0:
        return [timing]

    floor = min(min_duration_ms, span // parts)
    remaining = span - floor * parts
    total_weight = sum(weights) or 1.0
    durations = [floor + round(remaining * weight / total_weight) for weight in weights]
    difference = span - sum(durations)
    if difference:
        richest = max(range(parts), key=lambda index: durations[index])
        durations[richest] += difference
    for index in range(parts):
        if durations[index] < 1:
            durations[index] = 1

    timings: list[Timing] = []
    cursor = timing.start_ms
    for index, duration in enumerate(durations):
        end = timing.end_ms if index == parts - 1 else cursor + duration
        timings.append(Timing(cursor, max(cursor, end)))
        cursor = max(cursor, end)
    return timings


def _cut_positions(text: str, parts: int) -> list[int]:
    """按"时间进度"在 text 上找 ``parts`` 段的切点。

    两侧（原文/译文）用**同一组进度分数**切各自的文字，这是排版配对的关键：
    以前两侧各按自己的容量折行，第 i 段英文和第 i 段中文覆盖的内容可能完全不是同一句，
    时间一分配就会出现"这条中文 14 CPS、下一条 3 CPS"。按进度切等于假设语速均匀，
    每条 CPS 保持在全片均值附近，同时优先落在标点后、不拆拉丁词。
    """

    total = len(text)
    if parts <= 1 or total == 0:
        return [0, total]
    punctuation = {index + 1 for index, char in enumerate(text) if char in BREAK_AFTER}
    spaces = {index + 1 for index, char in enumerate(text) if char == " "}
    # 中文没有词边界，断行只能尽量落在标点上，容忍窗口给大一些；
    # 英文单词边界清楚，窗口收紧，否则一个远处的逗号会把首段撑成巨块。
    factor = 0.6 if _is_cjk(text) else 0.35
    tolerance = max(3.0, total / parts * factor)
    positions = [0]
    for part in range(1, parts):
        target = total * part / parts
        low = positions[-1] + 1
        high = total - (parts - part)
        if low > high:
            break
        # 只接受"离目标进度不远"的标点：否则一个很远的逗号会把第一段撑成巨块
        near_punct = [
            cut for cut in punctuation if low <= cut <= high and abs(cut - target) <= tolerance
        ]
        if near_punct:
            cut = min(near_punct, key=lambda position: abs(position - target))
        else:
            near_space = [cut for cut in spaces if low <= cut <= high and abs(cut - target) <= tolerance]
            if near_space:
                cut = min(near_space, key=lambda position: abs(position - target))
            else:
                cut = min(max(round(target), low), high)
                cut = _avoid_word_split(text, cut)
                if _is_cjk(text):
                    cut = _avoid_cjk_word_split(text, cut)
                cut = min(max(cut, low), high)
        positions.append(cut)
    positions.append(total)
    return positions


def _slice_text(text: str, parts: int) -> list[str]:
    positions = _cut_positions(text, parts)
    return [text[start:end].strip() for start, end in zip(positions, positions[1:])]


def _best_cut(text: str) -> int | None:
    """在文本中部找一个可切的位置：优先标点/空格，且不切在英文虚词后面。

    单纯"离正中最近的空格"会把句子切成 ``… over the`` / ``salt lake…`` 这种残句；
    这里给"切点在虚词后"加罚分，让切点落到实词后面（``… and Chiyou`` / ``over the salt…``）。
    """

    total = len(text)
    if total < 2:
        return None
    middle = total / 2
    is_cjk_text = _is_cjk(text)
    best_score: float | None = None
    best_cut: int | None = None
    for index in range(1, total):
        char = text[index - 1]
        left = text[:index].strip()
        right = text[index:].strip()
        if not left or not right:
            continue
        after_punct = char in BREAK_AFTER
        at_space = char == " "
        if not is_cjk_text and not (after_punct or at_space):
            continue
        distance = abs(index - middle) / total
        if distance > 0.4:
            continue
        penalty = 0.0
        if after_punct:
            penalty -= 0.35
        elif at_space and not is_cjk_text:
            penalty += 0.05
        score = distance + penalty
        if not is_cjk_text and not after_punct:
            # 只有"切在空格后"才检查虚词；切在逗号/句号后是天然短语边界，
            # 再看最后一个词会把 "…carnivores do," 误判成虚词结尾（M10 实测）。
            last_word = re.split(r"\s+", left)[-1].strip(".,;:!?…\"'()").lower()
            if last_word in FUNCTION_WORDS:
                penalty += 0.8
            first_word = re.split(r"\s+", right)[0].strip(".,;:!?…\"'()").lower()
            if first_word in FUNCTION_WORDS:
                penalty += 0.15
            score = distance + penalty
        if best_score is None or score < best_score:
            best_score = score
            best_cut = index
    if best_cut is None:
        cut = _avoid_word_split(text, total // 2)
        if _is_cjk(text):
            cut = _avoid_cjk_word_split(text, cut)
        return cut if 0 < cut < total else None
    adjusted = _avoid_word_split(text, best_cut)
    if is_cjk_text:
        adjusted = _avoid_cjk_word_split(text, adjusted)
    return adjusted if 0 < adjusted < total else None


def _sentence_groups(text: str, parts: int) -> list[str]:
    """把文本切成恰好 ``parts`` 段，**优先整句**：句数不够再把长句按分句/词边界拆开，
    句数超了就合并相邻最短的句子。

    返回段的先后顺序与原文本一致，拼回去不会丢字（拼接靠 ``_join``）。
    """

    units = [unit for unit in _units(text, SENTENCE_UNIT) if unit]
    if parts <= 1 or not units:
        return [text]

    guard = 0
    while len(units) < parts and guard < 128:
        guard += 1
        index = max(range(len(units)), key=lambda position: len(units[position]))
        piece = units[index]
        if len(piece) < 2:
            break
        cut = _best_cut(piece)
        if cut is None:
            break
        units[index : index + 1] = [piece[:cut].strip(), piece[cut:].strip()]

    while len(units) > parts:
        # 合并相邻最短的两段：短句先粘在一起，避免把一句拆开
        index = min(
            range(len(units) - 1),
            key=lambda position: len(units[position]) + len(units[position + 1]),
        )
        units[index : index + 2] = [_join(units[index], units[index + 1])]

    return units


def _slice_at_fractions(text: str, fractions: Sequence[float]) -> list[str]:
    """按给定的累积进度分数（0…1）切文本；用于原文跟随译文的段落划分。

    与 ``_slice_text`` 的区别：切点比例不是均分的，而是译文每段的字符占比——
    这样原文的切点会落在译文句子边界对应的时间位置附近，再吸附到最近的标点/词边界。
    """

    total = len(text)
    # fractions 含首尾（0.0 … 1.0），段数 = 分数个数 - 1
    parts = len(fractions) - 1
    if parts <= 1 or total == 0:
        return [text]

    punctuation = {index + 1 for index, char in enumerate(text) if char in BREAK_AFTER}
    spaces = {index + 1 for index, char in enumerate(text) if char == " "}
    positions = [0]
    for index in range(1, parts):
        target = total * min(max(fractions[index], 0.0), 1.0)
        low = positions[-1] + 1
        high = total - (parts - index)
        if low > high:
            break
        tolerance = max(3.0, (high - low) * (0.6 if _is_cjk(text) else 0.35))
        near_punct = [
            cut for cut in punctuation if low <= cut <= high and abs(cut - target) <= tolerance
        ]
        if near_punct:
            cut = min(near_punct, key=lambda position: abs(position - target))
        else:
            near_space = [
                cut for cut in spaces if low <= cut <= high and abs(cut - target) <= tolerance
            ]
            if near_space:
                cut = min(near_space, key=lambda position: abs(position - target))
            else:
                cut = min(max(round(target), low), high)
                cut = _avoid_word_split(text, cut)
                if _is_cjk(text):
                    cut = _avoid_cjk_word_split(text, cut)
                cut = min(max(cut, low), high)
        positions.append(cut)
    positions.append(total)

    pieces = [text[start:end].strip() for start, end in zip(positions, positions[1:])]
    pieces = [piece for piece in pieces if piece]
    while len(pieces) < parts:
        longest = max(range(len(pieces)), key=lambda position: len(pieces[position]))
        piece = pieces[longest]
        if len(piece) < 2:
            break
        middle = len(piece) // 2
        pieces[longest : longest + 1] = [piece[:middle].strip(), piece[middle:].strip()]
    return pieces[:parts] or [text]


def layout_pair(
    src: Subtitle, tgt: Subtitle, *, options: LayoutOptions | None = None
) -> tuple[Subtitle, Subtitle, LayoutStats]:
    """对一对字幕做折行/切条，返回同时间轴的两条新字幕。"""

    settings = options or LayoutOptions()
    if len(src) != len(tgt):
        raise ValueError(f"原文与译文条数不一致：{len(src)} vs {len(tgt)}，无法排版")

    laid_src: list[Cue] = []
    laid_tgt: list[Cue] = []
    split_cues = 0

    for src_cue, tgt_cue in zip(src.cues, tgt.cues):
        span = src_cue.timing.duration_ms
        max_parts = max(1, min(MAX_PARTS, span // settings.min_duration_ms or 1))
        src_capacity = settings.src_width * settings.max_lines
        tgt_capacity = settings.tgt_width * settings.max_lines
        # 拍平：whisper/机翻可能带换行或连续空格，切片前不归一会出现空段
        src_text = " ".join(src_cue.text.split())
        tgt_text = " ".join(tgt_cue.text.split())

        # 段数估算：
        # 1) 两侧各自"容量需要几段"（字数 / 每段容量）——这是硬需求；
        # 2) "一句一条"的理想粒度（取两侧句子数的较小值，避免把一长句切成标点碎片）。
        # 之前直接数"分句单元"会把一条 6.8s/40 字的译文切成 5 条逗号碎片。
        src_units = _units(src_text, SENTENCE_UNIT)
        tgt_units = _units(tgt_text, SENTENCE_UNIT)
        needs_src = math.ceil(len(src_text) / max(src_capacity, 1)) if src_text else 1
        needs_tgt = math.ceil(len(tgt_text) / max(tgt_capacity, 1)) if tgt_text else 1
        sentences = min(len(src_units), len(tgt_units))
        parts = min(max(needs_src, needs_tgt, sentences, 1), max_parts)
        parts = max(1, min(parts, len(src_text) or 1, len(tgt_text) or 1))

        while True:
            # 译文按"自己的句子边界"切：观众读的是译文，显示条必须收在句末。
            tgt_parts = _sentence_groups(tgt_text, parts)
            if len(tgt_parts) != parts:
                tgt_parts = _slice_text(tgt_text, parts)
            if len(src_units) == parts:
                # 两侧句数一致（翻译的常态）：原文也一句一条，双语块内容严格对应
                src_parts = list(src_units)
            else:
                # 句数不一致：原文按译文各段的字符占比切，切点吸附到自己的标点/词边界
                total_tgt = sum(len(part) for part in tgt_parts) or 1
                cumulative = 0
                fractions = [0.0]
                for part in tgt_parts[:-1]:
                    cumulative += len(part)
                    fractions.append(cumulative / total_tgt)
                fractions.append(1.0)
                src_parts = _slice_at_fractions(src_text, fractions)
                if len(src_parts) != parts:
                    src_parts = _slice_text(src_text, parts)
            src_fits = all(
                len(wrap_text(part, settings.src_width)) <= settings.max_lines for part in src_parts
            )
            tgt_fits = all(
                len(wrap_text(part, settings.tgt_width)) <= settings.max_lines for part in tgt_parts
            )
            if (src_fits and tgt_fits) or parts >= max_parts:
                break
            parts += 1

        if parts > 1:
            split_cues += 1

        timings = _allocate_timings(
            src_cue.timing,
            # 时间按**两侧阅读时长需求的较大者**分配：观众读译文，但双语字幕的原文
            # 也不能一闪而过（M10 实测：只按译文分，原文会出现 13.4 CPS/2s 的条）。
            [
                max(
                    len(tgt_part) / max(settings.tgt_cps, 0.1),
                    len(src_part) / max(settings.src_cps, 0.1),
                    1e-6,
                )
                for src_part, tgt_part in zip(src_parts, tgt_parts)
            ],
            min_duration_ms=settings.min_duration_ms,
        )
        for src_part, tgt_part, timing in zip(src_parts, tgt_parts, timings):
            laid_src.append(
                Cue(timing=timing, text="\n".join(wrap_text(src_part, settings.src_width)))
            )
            laid_tgt.append(
                Cue(timing=timing, text="\n".join(wrap_text(tgt_part, settings.tgt_width)))
            )

    return (
        Subtitle(cues=tuple(laid_src)),
        Subtitle(cues=tuple(laid_tgt)),
        LayoutStats(
            cues_before=len(src), cues_after=len(laid_src), split_cues=split_cues
        ),
    )
