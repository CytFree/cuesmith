"""SRT 解析与序列化。

设计要点：
- 时间轴一律用**整数毫秒**，不引入浮点误差（AC-5 要求时间轴容差 0）。
- 解析宽容：BOM / CRLF / 缺序号 / 点号毫秒 / 坐标后缀 / 少数位小数；
  但结构坏掉的块**必须报错**，不能静默丢条（否则"零漏报"无从谈起）。
- 序列化统一重编号为 1..N。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Cue",
    "SrtParseError",
    "Subtitle",
    "Timing",
    "dump_srt",
    "format_srt_time",
    "load_srt",
    "parse_srt",
]


class SrtParseError(ValueError):
    """SRT 结构不合法。``block`` 是 1 起的非空块序号，便于定位。"""

    def __init__(self, message: str, *, block: int | None = None) -> None:
        super().__init__(message)
        self.block = block


@dataclass(frozen=True, slots=True)
class Timing:
    """一条字幕的时间轴（毫秒，左闭右开不强制）。"""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms < 0:
            raise ValueError("时间轴不能为负")
        if self.end_ms < self.start_ms:
            raise ValueError("结束时间早于开始时间")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0


@dataclass(frozen=True, slots=True)
class Cue:
    timing: Timing
    text: str


@dataclass(frozen=True, slots=True)
class Subtitle:
    cues: tuple[Cue, ...] = ()

    def __len__(self) -> int:
        return len(self.cues)

    def __iter__(self):
        return iter(self.cues)


# 时间轴行：`0:00:01,000 --> 0:00:02,000 [X1:.. Y2:.. | align:start position:0%]`
_TIMING_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
    r"(?:\s+\S.*)?\s*$"
)

_BLANK_SEP = re.compile(r"\n[ \t\r]*\n")


def _to_ms(hours: str, minutes: str, seconds: str, frac: str, *, block: int) -> int:
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise SrtParseError(f"第 {block} 块时间轴里的分/秒越界", block=block)
    # ",5" => 500ms，",250" => 250ms
    millis = int((frac + "000")[:3])
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + millis


def format_srt_time(ms: int) -> str:
    """毫秒 → ``HH:MM:SS,mmm``（小时可超过两位，长视频不会溢出）。"""

    if ms < 0:
        raise ValueError("时间轴不能为负")
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(text: str) -> Subtitle:
    """把 SRT 文本解析成 :class:`Subtitle`。

    空文本返回空字幕；结构坏掉的块抛 :class:`SrtParseError`。
    """

    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

    cues: list[Cue] = []
    block_no = 0
    for raw_block in _BLANK_SEP.split(normalized):
        lines = [line.strip() for line in raw_block.split("\n")]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            continue

        block_no += 1
        head = 0
        if lines[0].isdigit():
            head = 1
        if head >= len(lines):
            raise SrtParseError(f"第 {block_no} 块只有序号、没有时间轴", block=block_no)

        match = _TIMING_RE.match(lines[head])
        if match is None:
            raise SrtParseError(
                f"第 {block_no} 块时间轴不合法：{lines[head]!r}", block=block_no
            )

        start_ms = _to_ms(*match.group(1, 2, 3, 4), block=block_no)
        end_ms = _to_ms(*match.group(5, 6, 7, 8), block=block_no)
        if end_ms < start_ms:
            raise SrtParseError(
                f"第 {block_no} 块结束时间早于开始时间："
                f"{format_srt_time(start_ms)} --> {format_srt_time(end_ms)}",
                block=block_no,
            )

        text_lines = [line for line in lines[head + 1 :] if line]
        cues.append(Cue(timing=Timing(start_ms, end_ms), text="\n".join(text_lines)))

    return Subtitle(cues=tuple(cues))


def dump_srt(subtitle: Subtitle) -> str:
    """序列化为 SRT；序号统一重编为 1..N，块间空一行，文件以换行结尾。"""

    blocks: list[str] = []
    for number, cue in enumerate(subtitle.cues, start=1):
        blocks.append(
            f"{number}\n"
            f"{format_srt_time(cue.timing.start_ms)} --> "
            f"{format_srt_time(cue.timing.end_ms)}\n"
            f"{cue.text}"
        )
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


_ENCODINGS: tuple[tuple[str, str], ...] = (
    ("utf-8-sig", "utf-8"),
    ("gb18030", "gb18030"),
)


def load_srt(path: Path | str) -> tuple[Subtitle, str]:
    """读取 SRT 文件，返回 ``(字幕, 实际编码)``。

    先按 UTF-8（含 BOM）解码，失败再按 GB18030——Windows 上老工具产出的
    中文 SRT 常见 GBK，直接崩掉会挡住真实素材。
    """

    raw = Path(path).read_bytes()
    for codec, label in _ENCODINGS:
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        return parse_srt(text), label
    raise SrtParseError(f"无法解码 {path}：既不是 UTF-8 也不是 GB18030")
