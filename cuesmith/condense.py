"""译后精简（condense）：把译文压到目标 CPS 以内。

这是"收费软件能把快语速播客做成可读字幕"的最后一招，也是唯一真能降 CPS 的一招：
CPS = 字数 / 时长，**切条不会降低 CPS**（按字数比例分时间后每条 CPS 不变，M4 已实测证伪）。

分三档，风险递增、可显式选择：

- ``rule``：只删语气词/口头禅（那么、其实、基本上、就是说、你知道……），不改语义；
- ``trim``：rule 之后仍超长，就从最后一个分句往前截断——**会丢信息**，所以报告里逐条标出；
- ``llm``：交给外部 LLM 重写（OpenAI 兼容端点），效果最好、成本和不可控性也最高。

默认不开启（``translate`` 不加 ``--condense`` 就不动译文），因为"少几个字"和"改错意思"
在自动流程里没人能替你签字。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .textutil import char_count

__all__ = ["CondenseResult", "condense_text", "estimate_max_chars"]

# 句首/分句首的语气词与口头禅：删掉不影响命题内容
_FILLER = re.compile(
    r"(?:^|(?<=[，。！？；：、]))"
    r"(?:那么|其实|基本上|就是说|你知道|你看|怎么说呢|呃|嗯|啊|哎呀|说实话)"
    r"[，,、]?"
)
# 英文侧口头禅（en 目标：中文口头禅那套正则一个都匹配不上，等于没精简）
_FILLER_EN = re.compile(
    r"(?:^|(?<=[.!?]\s))"
    r"(?:(?:you know|i mean|basically|actually|honestly|to be honest|kind of|sort of)"
    r"[,\s]+)",
    re.IGNORECASE,
)
# 句尾反问式填充（"对吧""是吧"），删除不改变陈述内容（连它后面的问号一起删）
_TAIL_TAG = re.compile(
    r"(?:[，,]\s*)?(?:对吧|是吧|对不对|你知道吗)[。！？…!?]?"
)
_SPACES = re.compile(r"\s+")
_EMPTY_PUNCT = re.compile(r"([，,、；;])\1+")
_SENTENCE_MARK = re.compile(r"[。！？!?…]")


def _sentence_count(text: str) -> int:
    return max(1, len(_SENTENCE_MARK.findall(text)))
CLAUSE_SPLIT = re.compile(r"(?<=[，,；;、])")


@dataclass(frozen=True, slots=True)
class CondenseResult:
    text: str
    action: str  # none | fillers | trim | llm | unchanged
    before_chars: int
    after_chars: int
    over_limit: bool

    @property
    def saved(self) -> int:
        return max(0, self.before_chars - self.after_chars)


def estimate_max_chars(duration_ms: int, cps: float) -> int:
    """按目标阅读速度算一条字幕最多能放多少字（至少给 2 个字，避免 0 字符目标）。"""

    seconds = max(duration_ms, 0) / 1000
    return max(2, int(cps * seconds))


def condense_text(
    text: str,
    max_chars: int,
    *,
    mode: str = "rule",
    cjk: bool = True,
    rewriter: Callable[[str, int], str] | None = None,
    keep: Sequence[str] = (),
) -> CondenseResult:
    """把 ``text`` 压到 ``max_chars`` 以内；压不到就如实返回 ``over_limit``。

    ``keep`` 是不许删的词（术语表的指定译法/禁译名）：压缩优先删别的分句，
    实在删不动就宁可超限——**术语被截掉比超 CPS 严重**（信息不可恢复）。
    """

    original = _SPACES.sub(" ", text).strip()
    before = char_count(original, cjk=cjk)
    if not original or max_chars <= 0 or before <= max_chars:
        return CondenseResult(original, "none", before, before, before > max_chars)

    if mode not in {"rule", "trim", "auto", "llm"}:
        raise ValueError(f"不认识的精简模式：{mode!r}（rule / trim / auto / llm）")
    keep = tuple(term for term in keep if term and term in original)

    if mode in {"llm", "auto"} and rewriter is not None:
        try:
            rewritten = _SPACES.sub(" ", rewriter(original, max_chars)).strip()
        except Exception:  # 外部改写失败不该毁掉整批翻译，退回保守规则
            rewritten = ""
        after = char_count(rewritten, cjk=cjk) if rewritten else 0
        # 拒绝"把整句删掉"式压缩：实测 LLM 会为了达标直接丢一句（"这里有个主意。" 被删）
        if rewritten and _sentence_count(rewritten) < _sentence_count(original) >= 2:
            rewritten = ""
            after = 0
        # 拒绝"把术语改掉/删掉"式压缩：术语是硬约束，不是措辞
        if rewritten and any(term not in rewritten for term in keep):
            rewritten = ""
            after = 0
        if rewritten and 0 < after < before:
            return CondenseResult(rewritten, "llm", before, after, after > max_chars)
        if mode == "llm":
            # 显式要了 LLM 却没成功：退回规则，并在 action 里保留真相
            result = _rule_condense(original, max_chars, cjk=cjk)
            return CondenseResult(
                result.text, "unchanged", before, result.after_chars, result.after_chars > max_chars
            )

    result = _rule_condense(original, max_chars, cjk=cjk)
    if mode in {"trim", "auto"} and result.after_chars > max_chars:
        result = _trim_condense(result.text, max_chars, cjk=cjk, base_action=result.action, keep=keep)
    return result


def _rule_condense(text: str, max_chars: int, *, cjk: bool) -> CondenseResult:
    before = char_count(text, cjk=cjk)
    cleaned = text
    for _ in range(6):
        if cjk:
            updated = _TAIL_TAG.sub("", cleaned)
            updated = _FILLER.sub("", updated)
        else:
            updated = _FILLER_EN.sub("", cleaned)
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = _EMPTY_PUNCT.sub(r"\1", cleaned)
    cleaned = _SPACES.sub(" ", cleaned).strip()
    if not cjk and cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    # 删到句首标点/空串就说明这条没有实质内容，保留原文更安全
    if not cleaned or cleaned[0] in "，,。.、；;：:":
        return CondenseResult(text, "unchanged", before, before, before > max_chars)
    if cleaned[-1] in "，,、；;":
        cleaned = cleaned[:-1] + ("。" if cjk else ".")
    elif cleaned[-1] not in "。！？…!?.":
        cleaned += "。" if cjk else "."
    after = char_count(cleaned, cjk=cjk)
    if after >= before:
        return CondenseResult(text, "unchanged", before, before, before > max_chars)
    return CondenseResult(cleaned, "fillers", before, after, after > max_chars)


def _trim_condense(
    text: str, max_chars: int, *, cjk: bool, base_action: str, keep: Sequence[str] = ()
) -> CondenseResult:
    """从最后一个分句往前截断，尽量保留句子开头的核心命题。

    带术语的分句不参与截断：宁可超 CPS，也不能把术语/禁译名删掉。
    """

    before = char_count(text, cjk=cjk)
    clauses = [part for part in CLAUSE_SPLIT.split(text) if part]
    if len(clauses) <= 1:
        return CondenseResult(text, base_action, before, before, True)

    trimmed_clauses = list(clauses)
    while (
        len(trimmed_clauses) > 1
        and char_count("".join(trimmed_clauses), cjk=cjk) > max_chars
        and not any(term in trimmed_clauses[-1] for term in keep)
    ):
        trimmed_clauses.pop()
    if trimmed_clauses == clauses:
        # 最后一个分句带术语，删不得：如实超限
        return CondenseResult(text, base_action, before, before, True)
    trimmed = "".join(trimmed_clauses).rstrip("，,、；; ")
    if trimmed and trimmed[-1] not in "。！？…!?.":
        trimmed += "。" if cjk else "."
    after = char_count(trimmed, cjk=cjk)
    action = "trim" if after < before else base_action
    return CondenseResult(trimmed, action, before, after, after > max_chars)
