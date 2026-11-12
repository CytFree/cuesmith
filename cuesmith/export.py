"""5 种字幕格式导出（FR-2 行为 1）。

一句话：**同一批 cue，导成 5 个能直接交付的文件**——
单语英文 SRT、双语 SRT、ASS、VTT、TXT。

约束：
- 双语类格式（SRT/ASS/VTT）一律"原文一行 + 译文一行"，且必须与源字幕逐条配对，
  条数不一致直接抛错，绝不按索引硬配对（会错位）。
- 时间轴只信**源字幕**（校验层已保证两侧时间轴一致）。
- 导出只做格式转换，**不改文案、不删条、不调时间**。
"""

from __future__ import annotations

import re

from .srt import Cue, Subtitle, dump_srt, format_srt_time
from .textutil import split_text_lines

__all__ = [
    "ASS_FONT",
    "dump_ass",
    "dump_ass_single",
    "dump_bilingual_srt",
    "dump_txt",
    "dump_vtt",
    "format_ass_time",
]

# Arial 没有中文字形；Microsoft YaHei 中英文都覆盖，且 Windows 自带
ASS_FONT = "Microsoft YaHei"


def _require_same_length(src: Subtitle, tgt: Subtitle) -> None:
    if len(src) != len(tgt):
        raise ValueError(
            f"原文与译文条数不一致：{len(src)} vs {len(tgt)}，无法逐条配对，拒绝导出"
        )


def _bilingual_text(src_cue: Cue, tgt_cue: Cue) -> str:
    """原文一行 + 译文一行；空的一侧不输出空行（空译由校验层报 error）。"""

    parts = [part for part in (src_cue.text.strip(), tgt_cue.text.strip()) if part]
    return "\n".join(parts)


# ---------------------------------------------------------------- 双语 SRT


def dump_bilingual_srt(src: Subtitle, tgt: Subtitle) -> str:
    """双语 SRT：同一 cue 内先原文、后译文（AC-6）。"""

    _require_same_length(src, tgt)
    cues = tuple(
        Cue(timing=src_cue.timing, text=_bilingual_text(src_cue, tgt_cue))
        for src_cue, tgt_cue in zip(src.cues, tgt.cues)
    )
    return dump_srt(Subtitle(cues=cues))


# ---------------------------------------------------------------- ASS


def format_ass_time(ms: int) -> str:
    """ASS 用厘秒（2 位），且小时只有 1 位。向下取整避免相邻 cue 叠压。"""

    if ms < 0:
        raise ValueError("时间轴不能为负")
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


_ASS_HEADER = f"""[Script Info]
Title: cuesmith
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{ASS_FONT},54,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_text(cue: Cue) -> str:
    """ASS 只认它自己的标记：清掉 HTML/旧标签，真换行转 ``\\N``，未闭合花括号转义。"""

    lines = split_text_lines(cue.text)
    escaped = [line.replace("{", r"\{").replace("}", r"\}") for line in lines]
    return r"\N".join(escaped)


def dump_ass(src: Subtitle, tgt: Subtitle) -> str:
    """双语 ASS：带一套基础样式（白字 + 黑边 + 底部居中），可直接给 ffmpeg 烧进视频。"""

    _require_same_length(src, tgt)
    dialogues: list[str] = []
    for src_cue, tgt_cue in zip(src.cues, tgt.cues):
        parts = [part for part in (_ass_text(src_cue), _ass_text(tgt_cue)) if part]
        body = r"\N".join(parts)
        dialogues.append(
            "Dialogue: 0,"
            f"{format_ass_time(src_cue.timing.start_ms)},"
            f"{format_ass_time(src_cue.timing.end_ms)},"
            f"Default,,0,0,0,,{body}"
        )
    return _ASS_HEADER + "\n".join(dialogues) + ("\n" if dialogues else "")


def dump_ass_single(subtitle: Subtitle) -> str:
    """单语 ASS（烧录用）：只显示译文，样式与双语 ASS 相同。"""

    dialogues: list[str] = []
    for cue in subtitle.cues:
        body = _ass_text(cue)
        if not body:
            continue
        dialogues.append(
            "Dialogue: 0,"
            f"{format_ass_time(cue.timing.start_ms)},"
            f"{format_ass_time(cue.timing.end_ms)},"
            f"Default,,0,0,0,,{body}"
        )
    return _ASS_HEADER + "\n".join(dialogues) + ("\n" if dialogues else "")


# ---------------------------------------------------------------- VTT

# WebVTT 认识这些内联标记，保留原样；其余 < > 必须转义
_VTT_TAG = re.compile(r"</?(?:i|b|u|c|v|lang|ruby|rt)(?:[.\s][^<>]*)?>", re.IGNORECASE)


def _vtt_escape_specials(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _vtt_escape(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _VTT_TAG.finditer(text):
        pieces.append(_vtt_escape_specials(text[cursor : match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_vtt_escape_specials(text[cursor:]))
    return "".join(pieces)


def dump_vtt(src: Subtitle, tgt: Subtitle) -> str:
    """双语 WebVTT（网页播放器/YouTube 用）。"""

    _require_same_length(src, tgt)
    blocks: list[str] = []
    for number, (src_cue, tgt_cue) in enumerate(zip(src.cues, tgt.cues), start=1):
        body = _vtt_escape(_bilingual_text(src_cue, tgt_cue))
        blocks.append(
            f"{number}\n"
            f"{format_srt_time(src_cue.timing.start_ms).replace(',', '.')} --> "
            f"{format_srt_time(src_cue.timing.end_ms).replace(',', '.')}\n"
            f"{body}"
        )
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


# ---------------------------------------------------------------- TXT


def dump_txt(tgt: Subtitle) -> str:
    """纯文本译文稿：一条一行、去标记、多行拍平，方便直接粘到文案里。"""

    lines: list[str] = []
    for cue in tgt.cues:
        line = " ".join(split_text_lines(cue.text))
        if line:
            lines.append(line)
    return "".join(line + "\n" for line in lines)
