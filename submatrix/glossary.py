"""术语表：保护不该翻的词、强制统一译名（对齐收费软件的 glossary / 禁译表）。

收费字幕软件（EZTitles、Ooona、Subtitle Edit）都有两条硬需求，这里一次做完：

1. **禁译表**：产品名、品牌、人名、缩写不能翻（``Blender`` 不能变成"搅拌机"）。
   做法是翻译前把术语换成占位符 ``{{G0}}``，翻译后换回来——占位符既不参与翻译，
   也不会被机翻语序重排带走。
2. **术语表**：同一个词全片必须一种译法（``large language model`` → ``大语言模型``），
   做法同上：翻译前占位，翻译后写入指定译法。

为什么必须"先占位再翻译"：拿译文做字符串替换只能命中"恰好被翻成 X"的情况，
机翻换个说法就漏；而且术语被翻成错的意思以后，原文信息已经丢了。

占位符选 ``{{G0}}`` 是在微软免费翻译端点上实测过的（``[[1]]`` 会被改成 ``[[[1]]``，
``⟦1⟧`` 能过但属于生僻符号，``\\ue000`` 会被换成空格）：只留 ``{{G0}}`` 这一种形态。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "Glossary",
    "GlossaryError",
    "ProtectedText",
    "Term",
    "glossary_fingerprint",
]

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 占位符：{{G0}}，容忍机翻插空格（{{ G0 }}）
PLACEHOLDER = re.compile(r"\{\{\s*G(\d+)\s*\}\}")
# 术语两侧若是拉丁字母/数字，就不能算独立词（AI 不该命中 said）
_LATIN_EDGE = re.compile(r"[A-Za-z0-9]")


class GlossaryError(Exception):
    """术语表读不了 / 格式不对。"""


@dataclass(frozen=True, slots=True)
class Term:
    source: str
    target: str | None = None
    keep: bool = False
    case_sensitive: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise GlossaryError("术语表里有空术语")
        if self.keep and self.target not in (None, "", self.source):
            raise GlossaryError(
                f"术语 {self.source!r} 同时标了 keep 和不同译法，二选一"
            )

    @property
    def replacement(self) -> str:
        """占位符该换回什么：禁译就是原文，术语表就是指定位译法。"""

        return self.source if self.target in (None, "") else self.target

    @property
    def is_cjk(self) -> bool:
        return bool(CJK_PATTERN.search(self.source))

    def matches(self, text: str) -> list[re.Match[str]]:
        return list(_term_pattern(self.source, self.case_sensitive, self.is_cjk).finditer(text))


@lru_cache(maxsize=512)
def _term_pattern(source: str, case_sensitive: bool, is_cjk: bool) -> re.Pattern[str]:
    escaped = re.escape(source)
    if not is_cjk:
        # 只拦拉丁字母/数字边界："(OpenAI)"、"OpenAI's" 都要命中
        escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(escaped, flags)


@dataclass(frozen=True, slots=True)
class ProtectedText:
    """占位后的文本 + 还原所需的对照表。"""

    text: str
    entries: tuple[tuple[str, Term], ...] = ()

    @property
    def terms(self) -> tuple[Term, ...]:
        return tuple(term for _, term in self.entries)


@dataclass(frozen=True, slots=True)
class Glossary:
    terms: tuple[Term, ...] = ()
    path: Path | None = None

    def __bool__(self) -> bool:
        return bool(self.terms)

    def __len__(self) -> int:
        return len(self.terms)

    def __iter__(self):
        return iter(self.terms)

    # ------------------------------------------------------------------ 读取

    @classmethod
    def from_file(cls, path: str | Path) -> "Glossary":
        """支持 .json / .tsv / .csv / .txt（一行一个禁译词）。"""

        source = Path(path)
        try:
            raw = source.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise GlossaryError(f"读不到术语表 {source}：{exc}") from exc
        suffix = source.suffix.lower()
        if suffix == ".json":
            terms = _parse_json(raw, source)
        elif suffix in {".tsv", ".csv", ".txt"} or suffix == "":
            terms = _parse_lines(raw, source)
        else:
            raise GlossaryError(f"不支持的术语表格式：{suffix}（用 .json/.tsv/.csv/.txt）")
        if not terms:
            raise GlossaryError(f"术语表是空的：{source}")
        return cls(terms=tuple(terms), path=source)

    @classmethod
    def from_terms(cls, terms: Iterable[Term]) -> "Glossary":
        return cls(terms=tuple(terms))

    def reversed(self) -> "Glossary":
        """反向术语表：zh→en 时把"英文原词→中文译法"翻成"中文→英文原词"。

        - 有指定译法的术语：``presentation→演示文稿`` 反向成 ``演示文稿→presentation``；
        - 禁译名（keep）：两侧同一个词，原样保留；
        - 只写译法没有原文的（target 为空）：保留原样。

        实测：zh→en 若直接用原表，`protect()` 拿英文词去匹配中文字幕，一处都命中不了，
        术语表等于没生效——所以反向必须显式可要求，不能指望自动猜对。
        """

        terms: list[Term] = []
        for term in self.terms:
            if term.target:
                terms.append(
                    Term(
                        source=term.target,
                        target=term.source,
                        case_sensitive=term.case_sensitive,
                        note=term.note,
                    )
                )
            else:
                terms.append(term)
        return Glossary(terms=tuple(terms), path=self.path)

    # ------------------------------------------------------------------ 查询

    @property
    def sources(self) -> tuple[str, ...]:
        """术语原文列表（可直接喂给 ASR 当热词）。"""

        return tuple(term.source for term in self.terms)

    def find(self, text: str) -> list[Term]:
        """按出现顺序返回命中的术语；重叠时长者优先（"large language model" 先于 "model"）。"""

        found: list[tuple[int, int, Term]] = []
        for term in self.terms:
            for match in term.matches(text):
                found.append((match.start(), match.end(), term))
        found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        result: list[Term] = []
        end = -1
        hits: set[str] = set()
        for start, stop, term in found:
            if start < end:
                continue
            end = stop
            key = f"{term.source}|{term.target}|{term.keep}"
            if key not in hits:
                hits.add(key)
                result.append(term)
        return result

    # ------------------------------------------------------------------ 占位

    def protect(self, text: str) -> ProtectedText:
        if not text or not self.terms:
            return ProtectedText(text=text)

        found: list[tuple[int, int, Term]] = []
        for term in self.terms:
            for match in term.matches(text):
                found.append((match.start(), match.end(), term))
        found.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        entries: list[tuple[str, Term]] = []
        pieces: list[str] = []
        last = 0
        for start, stop, term in found:
            if start < last:
                continue
            token = f"{{{{G{len(entries)}}}}}"
            pieces.append(text[last:start])
            pieces.append(token)
            entries.append((token, term))
            last = stop
        pieces.append(text[last:])
        return ProtectedText(text="".join(pieces), entries=tuple(entries))

    def unprotect(self, text: str, protected: ProtectedText) -> tuple[str, list[Term]]:
        """把占位符换回译法；机翻把占位符吃掉时记录丢失术语。"""

        if not protected.entries:
            return text, []

        mapping = {token: term for token, term in protected.entries}
        lost: list[Term] = []
        used: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            token = f"{{{{G{match.group(1)}}}}}"
            term = mapping.get(token)
            if term is None:
                return match.group(0)
            used.add(token)
            return term.replacement

        restored = PLACEHOLDER.sub(replace, text)
        for token, term in protected.entries:
            if token in used:
                continue
            # LLM 渠道会看到"占位符 = 应输出"的对照，可能直接把术语写出来而不用占位符——
            # 译文里已经有正确译法就不算丢失（否则每次都会误报）。
            if term.replacement and term.replacement in restored:
                continue
            lost.append(term)
        return restored, lost

    def missing(self, translated: str) -> list[Term]:
        """检查译文里是否真的用上了指定译法（没有占位符时的兜底核查）。"""

        return [
            term
            for term in self.terms
            if term.replacement and term.replacement not in translated
        ]


def glossary_fingerprint(glossary: Glossary | None) -> str | None:
    """术语表**内容**指纹，给阶段缓存当判据（没有术语表返回 ``None``）。

    为什么要这个：translate 阶段原来的缓存判据只比输入 sha 和精简预算，术语表根本没进
    判据——改了术语表（哪怕只是同一个文件里换个译法、条数不变）重跑，拿到的还是旧译文，
    用户以为术语表生效了，其实一个词都没换。指纹按内容算，路径改不改、条数变不变都不
    影响正确性。
    """

    if glossary is None or not glossary.terms:
        return None
    lines = []
    for term in sorted(glossary.terms, key=lambda item: (item.source, item.target or "")):
        lines.append(
            "\t".join(
                [
                    term.source,
                    term.target or "",
                    "keep" if term.keep else "-",
                    "cs" if term.case_sensitive else "-",
                ]
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------- 解析


def _parse_json(raw: str, source: Path) -> list[Term]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GlossaryError(f"术语表 JSON 解析失败（{source}）：{exc}") from None

    if isinstance(data, dict) and "terms" in data:
        data = data["terms"]
    if isinstance(data, dict):
        # {"Blender": "Blender", "podcast": "播客"}
        return [_term_from_mapping(key, value) for key, value in data.items()]
    if isinstance(data, list):
        terms: list[Term] = []
        for item in data:
            if isinstance(item, str):
                terms.append(Term(source=item, keep=True))
            elif isinstance(item, dict):
                terms.append(_term_from_mapping(item.get("source"), item))
            else:
                raise GlossaryError(f"术语表条目类型不对：{item!r}")
        return terms
    raise GlossaryError(f"术语表 JSON 结构不对（{source}）：顶层应为对象或数组")


def _term_from_mapping(source: object, value: object) -> Term:
    if not isinstance(source, str):
        raise GlossaryError(f"术语缺少 source：{value!r}")
    if isinstance(value, str):
        keep = value.strip() == source.strip()
        return Term(source=source.strip(), target=None if keep else value.strip(), keep=keep)
    if not isinstance(value, dict):
        raise GlossaryError(f"术语 {source!r} 的值类型不对：{value!r}")
    keep = bool(value.get("keep", False))
    target = value.get("target")
    if target is not None and not isinstance(target, str):
        raise GlossaryError(f"术语 {source!r} 的 target 应为字符串")
    return Term(
        source=source.strip(),
        target=target.strip() if isinstance(target, str) and target.strip() else None,
        keep=keep,
        case_sensitive=bool(value.get("case_sensitive", False)),
        note=str(value.get("note", "") or ""),
    )


def _parse_lines(raw: str, source: Path) -> list[Term]:
    terms: list[Term] = []
    for number, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 表头（source target ...）跳过
        if number == 1 and stripped.lower().split()[0] in {"source", "原文", "术语"}:
            continue
        if "\t" in stripped:
            columns = [column.strip() for column in stripped.split("\t")]
        elif "," in stripped:
            columns = [column.strip() for column in stripped.split(",")]
        else:
            columns = [stripped]
        origin = columns[0]
        if not origin:
            continue
        target = columns[1] if len(columns) > 1 and columns[1] else None
        flags = {column.lower() for column in columns[2:]}
        keep = "keep" in flags or target is None or target == origin
        terms.append(Term(source=origin, target=None if keep else target, keep=keep))
    return terms


def load_glossary(spec: str | Path | Glossary | None) -> Glossary | None:
    """CLI 传参用：None → 空；Glossary → 原样；路径 → 读文件。"""

    if spec is None:
        return None
    if isinstance(spec, Glossary):
        return spec
    return Glossary.from_file(spec)


def parse_terms(texts: Sequence[str]) -> Glossary:
    """一行一个禁译词（ASR 热词复用同一个列表）。"""

    return Glossary(
        terms=tuple(Term(source=text.strip(), keep=True) for text in texts if text.strip())
    )
