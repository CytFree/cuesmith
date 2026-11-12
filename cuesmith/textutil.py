"""文本指标工具：先去掉不可见标记，再算字符数与行。

CPS / 行宽必须在"真正显示出来的字符"上计算——``<i>``、``{\\an8}``、``\\N``
都不该被算进长度，否则合规阈值形同虚设。
"""

from __future__ import annotations

import re

__all__ = ["char_count", "split_text_lines", "visible_text"]

# ASS 覆写块：{\an8}、{\pos(10,20)}、{\i1}
_ASS_TAG = re.compile(r"\{[^{}]*\}")
# SRT/VTT 内联 HTML：<i> </i> <font color="red">
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def visible_text(text: str) -> str:
    """还原"观众看到的文本"：去标记、把 ``\\N`` 变成真换行。"""

    normalized = text.replace("\\N", "\n").replace("\\n", "\n")
    normalized = _ASS_TAG.sub("", normalized)
    normalized = _HTML_TAG.sub("", normalized)
    return normalized


def split_text_lines(text: str) -> list[str]:
    """按显示行拆分，去掉行首尾空白与空行。"""

    return [line for line in (part.strip() for part in visible_text(text).split("\n")) if line]


def char_count(text: str, *, cjk: bool) -> int:
    """字数统计口径。

    - 拉丁文本（``cjk=False``）：计所有可见字符**含空格**，不含换行与首尾空白。
    - 中日韩文本（``cjk=True``）：只计非空白字符（标点算字）。
    """

    cleaned = visible_text(text)
    if cjk:
        return sum(1 for ch in cleaned if not ch.isspace())
    return len(cleaned.replace("\n", "").replace("\r", "").strip())
