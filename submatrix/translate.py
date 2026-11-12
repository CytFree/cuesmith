"""翻译编排（FR-5）：术语表 + 段落上下文 + 译后精简 + 质量报告。

**本模块不是翻译引擎**：引擎一律走外部渠道（PRD 锁定"不自建 MT"）。这里做的是收费
字幕软件在"翻译这一层"真正干的事：

1. **源文清洗**：ASR 残留的 `,and`、标点前空格、句首小写会直接拉低机翻质量；
2. **整句化**：半句碎片不翻，先拼成整句；
3. **段落上下文**：一次翻 2–4 条相邻 cue（代词、省略、时态需要上下文才准），
   译完按字数比例把目标语句子分回各 cue；
4. **术语表**：翻译前占位保护、翻译后写回指定译法（见 ``glossary.py``）；
5. **译后精简**：目标 CPS 超限时按 ``condense.py`` 压缩（唯一真能降 CPS 的手段）；
6. **质量指标**：CPS 超限条数、术语丢失、回退条数全部写进 ``translate.json``。

渠道：

- ``microsoft``：免费端点，无需 key（默认，适合联调和预算为零的场景）
- ``openai``：OpenAI 兼容的 chat/completions（上下文翻译和译后精简质量最好，需要 key）
- ``command``：外部命令模板，``{input}`` / ``{output}`` 都是 JSON 数组文件
- ``echo``：回显 ``[lang] 原文``，只用于离线跑通管线（CI / 回归）
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence, cast

from . import atomic
from . import __version__
from .clean import is_degenerate_text
from .condense import CondenseResult, condense_text, estimate_max_chars
from .glossary import Glossary, Term, glossary_fingerprint
from .langs import LangProfile, profile as lang_profile
from .report import file_digest
from .srt import Cue, Subtitle, dump_srt, load_srt
from .textutil import char_count

__all__ = [
    "EchoTranslator",
    "LlmConfig",
    "MicrosoftTranslator",
    "OpenAITranslator",
    "ResilientTranslator",
    "TranslateError",
    "TranslateOptions",
    "TranslateResult",
    "Translator",
    "build_translator",
    "run_translate",
]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2

MICROSOFT_ENDPOINT = (
    "https://edge.microsoft.com/translate/translatetext"
    "?from={src}&to={tgt}&isEnterpriseClient=false"
)
MICROSOFT_LANG = {"zh": "zh-Hans", "en": "en"}
_JOINERS = {"zh": "", "en": " "}
_SENTENCE_SPLIT_LATIN = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_SPLIT_CJK = re.compile(r"(?<=[。！？…!?])")
_PUNCT_BEFORE_SPACE = re.compile(r"\s+([,.;:!?])")
_COMMA_GLUE = re.compile(r"([,;:])(?=[A-Za-z0-9])")
_LONE_I = re.compile(r"(?<![A-Za-z])i(?![A-Za-z])")
_CJK_CHARS = r"\u3400-\u4dbf\u4e00-\u9fff"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK_CHARS}])\s+(?=[{_CJK_CHARS}])")
_SPACE_BEFORE_CJK_PUNCT = re.compile(r"\s+(?=[，。！？；：、])")
# 口吃式重复：3 字及以上的片段连续出现两次以上 → 折叠成一次（两字重复是正常表达，不动）
_CJK_REPEAT = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff]{3,12}?)\1+")
# 中文 ASR 常见半角标点（含标点后的空格）
_CJK_PUNCT_MAP = {",": "，", "?": "？", "!": "！", ";": "；", ":": "："}
_CJK_PUNCT_SPACE = re.compile(r"[,?!;:]\s*")


def normalize_cjk_spacing(text: str) -> str:
    """中文译文去掉多余空格（占位符还原、LLM 输出都可能留下 "一个 演示文稿"）。"""

    cleaned = _SPACE_BETWEEN_CJK.sub("", text)
    cleaned = _SPACE_BEFORE_CJK_PUNCT.sub("", cleaned)
    return " ".join(cleaned.split())


def _normalize_texts(texts: Sequence[str | None]) -> list[str | None]:
    """整批清一遍中文间距；空串/None 原样保留（没译出来的条目不能变成空串）。

    初译后、审校后、精简后各需要一次。以前是三处一模一样的推导式：``if text else text``
    这一层是防 None 的，抄漏一次就会在中文链路上炸 ``TypeError``。
    """

    return [normalize_cjk_spacing(text) if text else text for text in texts]

Transport = Callable[[str, bytes, dict[str, str], float], object]


class TranslateError(Exception):
    """翻译渠道不可用 / 返回不可信。"""


@dataclass(frozen=True, slots=True)
class TranslateHints:
    """给 LLM 渠道的额外上下文（免费 MT 渠道忽略）。

    - ``glossary``：``(占位符, 原文, 应输出)``。占位符对机翻是"不可翻译的符号"，
      但 LLM 只要知道它代表什么，就能写出语法正确的上下文——这是术语表在
      LLM 渠道上比免费端点更准的关键。
    - ``budgets``：每行最多多少个目标语言字符（按该句所在 cue 的时长 × 目标 CPS
      折算）。收费软件都按"这条要读多久"约束译文长度，从源头减少事后截断。
    """

    glossary: tuple[tuple[str, str, str], ...] = ()
    budgets: tuple[int, ...] | None = None

    def __bool__(self) -> bool:
        return bool(self.glossary) or bool(self.budgets)


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """OpenAI 兼容端点配置；全部可从环境变量取，避免把 key 写进命令行历史。

    默认服务商：检测到 `DEEPSEEK_API_KEY` 时用 DeepSeek（`https://api.deepseek.com/v1`，
    模型 `deepseek-chat`）；否则用 OpenAI 默认值。可用 `SUBMATRIX_LLM_BASE_URL` 覆盖。
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    model: str = "gpt-4o-mini"
    timeout_s: float = 120.0

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> "LlmConfig":
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        resolved_key = (
            api_key
            or os.environ.get("SUBMATRIX_LLM_API_KEY")
            or deepseek_key
            or os.environ.get("OPENAI_API_KEY")
            or None
        )
        env_base = os.environ.get("SUBMATRIX_LLM_BASE_URL")
        if base_url:
            resolved_base = base_url.rstrip("/")
        elif env_base:
            resolved_base = env_base.rstrip("/")
        elif deepseek_key:
            resolved_base = "https://api.deepseek.com/v1"
        else:
            resolved_base = "https://api.openai.com/v1"
        default_model = "deepseek-chat" if "deepseek" in resolved_base else "gpt-4o-mini"
        resolved_model = model or os.environ.get("SUBMATRIX_LLM_MODEL") or default_model
        return cls(base_url=resolved_base, api_key=resolved_key, model=resolved_model)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def _llm_content(data: object) -> object:
    """从 OpenAI 兼容响应里取 ``choices[0].message.content``。

    形状不对时保持原来的行为（抛 KeyError/TypeError/IndexError，由上层重试/回退），
    这里只负责把嵌套的 ``object`` 收窄成可索引的类型。
    """

    payload = cast("dict[str, object]", data)
    choices = cast("list[dict[str, object]]", payload["choices"])
    message = cast("dict[str, object]", choices[0]["message"])
    return message["content"]


# ---------------------------------------------------------------------- 渠道


class Translator(Protocol):
    name: str

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]: ...


_HTTP_LOCAL = threading.local()
_HTTP_KEY = "_submatrix_http_connection"


def _drop_connection() -> None:
    """丢掉当前线程的 keep-alive 连接（服务端关闭/网络抖动后重建）。"""

    state = getattr(_HTTP_LOCAL, _HTTP_KEY, None)
    connection = state[1] if isinstance(state, tuple) else state
    if connection is not None:
        try:
            connection.close()
        except (OSError, http.client.HTTPException):
            pass
    setattr(_HTTP_LOCAL, _HTTP_KEY, None)


def _connection_for(parts, timeout: float):
    """本线程复用的 HTTPS 连接：同一端点只握手一次（OPT-10）。

    免费 MT 每个请求都要新建 TLS 连接时，实测单请求 1.74s；复用连接后 0.53s
    （TLS 0.77s 只付一次）——10min 素材 translate 13.56s → 5.14s。
    """

    key = (parts.scheme, parts.hostname, parts.port, timeout)
    state = getattr(_HTTP_LOCAL, _HTTP_KEY, None)
    if isinstance(state, tuple) and state[0] == key and state[1] is not None:
        return state[1]
    if parts.scheme != "https":  # 端点只有 https；http 留给测试/代理
        connection = http.client.HTTPConnection(
            parts.hostname or "", parts.port or 80, timeout=timeout
        )
    else:
        connection = http.client.HTTPSConnection(
            parts.hostname or "",
            parts.port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    setattr(_HTTP_LOCAL, _HTTP_KEY, (key, connection))
    return connection


def _request_once(connection, path: str, payload: bytes, headers: dict[str, str]) -> object:
    connection.request("POST", path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    return json.loads(data.decode("utf-8"))


def _default_transport(url: str, payload: bytes, headers: dict[str, str], timeout: float) -> object:
    """HTTP POST（复用连接）。连接级错误（服务端关空闲连接等）重连一次再试。"""

    parts = urllib.parse.urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        return _request_once(_connection_for(parts, timeout), path, payload, headers)
    except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
        _drop_connection()
        try:
            return _request_once(_connection_for(parts, timeout), path, payload, headers)
        except (http.client.HTTPException, OSError, ssl.SSLError) as retry_exc:
            _drop_connection()
            raise urllib.error.URLError(f"{type(retry_exc).__name__}: {retry_exc}") from exc


class MicrosoftTranslator:
    """微软免费翻译端点（pyVideoTrans 同款），批量 POST，失败自动二分重试。"""

    name = "microsoft"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        timeout: float = 120.0,
        retries: int = 3,
        batch_size: int = 20,
    ) -> None:
        self._transport = transport or _default_transport
        self._timeout = timeout
        self._retries = max(0, retries)
        self._batch_size = max(1, batch_size)

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]:
        results: list[str] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = list(texts[start : start + self._batch_size])
            results.extend(self._batch(chunk, src, tgt))
        return results

    def _batch(self, texts: list[str], src: str, tgt: str) -> list[str]:
        error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._request(texts, src, tgt)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
                error = exc
                if attempt < self._retries:
                    time.sleep(min(1.0 * (2**attempt), 8.0))
        if len(texts) > 1:
            middle = len(texts) // 2
            return self._batch(texts[:middle], src, tgt) + self._batch(texts[middle:], src, tgt)
        raise TranslateError(f"翻译请求失败：{error}") from error

    def _request(self, texts: list[str], src: str, tgt: str) -> list[str]:
        url = MICROSOFT_ENDPOINT.format(
            src=MICROSOFT_LANG.get(src, src), tgt=MICROSOFT_LANG.get(tgt, tgt)
        )
        payload = json.dumps(texts, ensure_ascii=False).encode("utf-8")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
            "Content-Type": "application/json",
        }
        data = self._transport(url, payload, headers, self._timeout)
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError(f"翻译返回条数不对：期望 {len(texts)}，得到 {type(data).__name__}")
        return [str(item["translations"][0]["text"]).strip() for item in data]


class CommandTranslator:
    """外部命令渠道：``{input}`` 与 ``{output}`` 是同长度的 JSON 数组文件。"""

    name = "command"

    def __init__(self, template: str, *, timeout: float = 300.0) -> None:
        if "{input}" not in template or "{output}" not in template:
            raise TranslateError("命令模板必须同时包含 {input} 和 {output}（JSON 数组文件）")
        self._template = template
        self._timeout = timeout

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="submatrix-mt-") as tmp:
            input_path = Path(tmp) / "input.json"
            output_path = Path(tmp) / "output.json"
            input_path.write_text(json.dumps(list(texts), ensure_ascii=False), encoding="utf-8")
            command = self._template.format(
                input=input_path, output=output_path, src=src, tgt=tgt
            )
            completed = subprocess.run(  # noqa: S602
                command, shell=True, capture_output=True, text=True, timeout=self._timeout
            )
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip()[-500:]
                raise TranslateError(f"翻译命令失败（退出码 {completed.returncode}）：{tail}")
            if not output_path.is_file():
                raise TranslateError("翻译命令没有写出 {output} 文件")
            data = json.loads(output_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or len(data) != len(texts):
            raise TranslateError(f"翻译命令返回条数不对：期望 {len(texts)}，得到 {data!r}"[:300])
        return [str(item).strip() for item in data]


class OpenAITranslator:
    """OpenAI 兼容 chat/completions：上下文翻译 + 术语占位 + 精简共用一套端点。"""

    name = "openai"

    def __init__(
        self,
        config: LlmConfig,
        *,
        transport: Transport | None = None,
        retries: int = 2,
    ) -> None:
        if not config.api_key:
            raise TranslateError(
                "openai 渠道需要 API key：设置环境变量 SUBMATRIX_LLM_API_KEY（或 OPENAI_API_KEY），"
                "或用 --provider microsoft"
            )
        self._config = config
        self._transport = transport or _default_transport
        self._retries = max(0, retries)
        # 账单透明：把 token 用量累计进报告（LLM 渠道按 token 计费）
        self.usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]:
        system = self._translate_system(src, tgt, hints)
        return self._translate_block(list(texts), system, hints)

    def _translate_block(
        self, texts: list[str], system: str, hints: TranslateHints | None
    ) -> list[str]:
        """整批翻；模型把多行并成一行、且重试也纠正不过来时对半拆开重试。"""

        payload: dict[str, object] = {"lines": texts}
        if hints is not None and hints.glossary:
            # 占位符对免费 MT 只是"不可翻译的符号"，对 LLM 则要告诉它代表什么，
            # 模型才能写出语法正确的上下文（这是 LLM 渠道术语准确率更高的原因）。
            payload["glossary"] = [
                {"token": token, "source": source, "output": output}
                for token, source, output in hints.glossary
            ]
        if hints is not None and hints.budgets:
            payload["max_chars"] = list(hints.budgets)
        try:
            return self._translate_lines(
                system, json.dumps(payload, ensure_ascii=False), len(texts)
            )
        except TranslateError:
            if len(texts) <= 1:
                raise
            middle = len(texts) // 2
            return self._translate_block(
                texts[:middle], system, _slice_hints(hints, 0, middle)
            ) + self._translate_block(
                texts[middle:], system, _slice_hints(hints, middle, len(texts))
            )

    def rewrite(self, text: str, max_chars: int, *, tgt: str, context: str = "") -> str:
        """译后精简：把一句译文压到 ``max_chars`` 个字符以内。"""

        system = (
            "You are a subtitle editor. Compress the line to at most "
            f"{max_chars} characters in {tgt} while keeping its core meaning. "
            "Keep proper nouns, numbers, names and facts unchanged. "
            "Do not delete whole sentences or clauses; shorten wording and merge phrasing instead. "
            "Keep the original tone and punctuation style; do not add quotes or explanations. "
            "If the limit cannot be met without losing information, return a complete shorter version."
        )
        user = text if not context else f"Context: {context}\nLine to compress: {text}"
        return self._chat(system, user).strip()

    def review(self, pairs: Sequence[tuple[str, str]], *, src: str, tgt: str) -> list[str]:
        """第二遍审校：只改明显错误（误译、代词指代、术语、生硬），不改对的地方。"""

        system = (
            "You are a senior subtitle reviewer. Each item gives one source line and its "
            f"current {tgt} subtitle. Verify the subtitle against the source, then return the "
            f"final {tgt} subtitle for every item, in the same order and the same count. "
            "Fix only real errors: wrong meaning or negation (not/never/no), wrong subject "
            "(I/we/you/it/they), wrong tense, wrong pronoun reference, numbers or names or "
            "terminology changed, literal fillers that obscure the meaning, or a fragment "
            "that lost its predicate. Do not merely polish style, do not make lines longer "
            "than the source needs, and if an item is already accurate and natural, return it "
            "unchanged character for character. Do not merge or split items. "
            "Do not add explanations. Return a JSON array of strings."
        )
        payload = json.dumps(
            {"lines": [{"source": source, "translation": translation} for source, translation in pairs]},
            ensure_ascii=False,
        )
        return self._translate_lines(system, payload, len(pairs))

    def _translate_system(
        self, src: str, tgt: str, hints: TranslateHints | None = None
    ) -> str:
        budget_note = ""
        if hints is not None and hints.budgets:
            budget_note = (
                " The payload has a max_chars array: the character budget for the same "
                f"position in lines. Aim to stay within each budget by choosing concise, "
                f"natural {tgt} wording. Never drop information to hit the budget; a "
                "slightly longer line is better than an incomplete one."
            )
        glossary_note = ""
        if hints is not None and hints.glossary:
            glossary_note = (
                " The payload has a glossary array: each token is a placeholder that already "
                "stands for a fixed term. Context: token = source term -> required output. "
                "Write the surrounding sentence so the restored term reads naturally."
            )
        return (
            "You are a professional subtitle translator. Translate every line from "
            f"{src} to {tgt}. Keep the same number of lines and the original order. "
            f"The lines are consecutive subtitles from the same video, so keep pronouns, "
            "tense and terminology consistent across them. "
            f"Write natural, idiomatic {tgt} subtitles: avoid word-for-word translation, "
            "do not translate discourse fillers literally (e.g. 'the fact that'), "
            "and drop spoken-language redundancy when the meaning is preserved. "
            f"Keep the claim exact: negation (not/never/no), subject (I/we/you/it/they), "
            f"tense and numbers must match the source; do not soften or strengthen it. "
            "Preserve placeholder tokens like {{G0}} exactly as they are, in place. "
            "Do not add explanations. Return a JSON array of strings only."
            f"{budget_note}{glossary_note}"
        )

    def _translate_lines(self, system: str, user: str, count: int) -> list[str]:
        """要 N 行拿 N 行：第一次不合规就在同一请求上明确强调条数，再不合规就报错。"""

        first = self._parse_array(self._chat(system, user, expect_json=True))
        if len(first) == count:
            return [str(item).strip() for item in first]
        reminder = (
            f"CRITICAL: the previous answer had {len(first)} items but {count} are required. "
            f"Return exactly {count} items, one per input line, as a JSON array of {count} strings."
        )
        second = self._parse_array(
            self._chat(f"{system}\n{reminder}", user, expect_json=True)
        )
        if len(second) == count:
            return [str(item).strip() for item in second]
        raise TranslateError(
            f"LLM 返回条数不对：期望 {count}，得到 {len(first)}/{len(second)}"
        )

    @property
    def info(self) -> dict[str, object]:
        return {
            "base_url": self._config.base_url,
            "model": self._config.model,
            "usage": dict(self.usage),
        }

    def _chat(self, system: str, user: str, *, expect_json: bool = False) -> str:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": self._config.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                data = self._transport(url, payload, headers, self._config.timeout_s)
                content = str(_llm_content(data))
                usage = data.get("usage") if isinstance(data, dict) else None
                self.usage["calls"] += 1
                if isinstance(usage, dict):
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        value = usage.get(key)
                        if isinstance(value, int):
                            self.usage[key] += value
                return content
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
                error = exc
                if attempt < self._retries:
                    time.sleep(min(1.5 * (2**attempt), 8.0))
        raise TranslateError(f"LLM 请求失败（{self._config.base_url}）：{error}") from error

    @staticmethod
    def _parse_array(content: str) -> list[object]:
        text = content.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text.removeprefix("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end <= start:
                raise TranslateError(f"LLM 返回不是 JSON 数组：{content[:200]!r}") from None
            data = json.loads(text[start : end + 1])
        return list(data) if isinstance(data, list) else [data]


class EchoTranslator:
    """离线管线测试用：``[tgt] 原文``。绝不用于真实交付。"""

    name = "echo"

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]:
        return [f"[{tgt}] {text}" for text in texts]


class ResilientTranslator:
    """多渠道路由（OPT-3）：当前渠道报错就切到下一个，并**记住**，后面不再重试坏渠道。

    设计要点：
    - **不整批重来**：切渠道只影响还没翻的调用；已经翻好的句子保持不动；
    - **粘性**：一次失败后固定走备用渠道（每次调用先重试三遍再切会白等 1+2+4s）；
    - 备用渠道也失败时继续往下切，全部失败才抛 :class:`TranslateError`（错误里带上链路）；
    - 线程安全：并发翻译时用锁保护"切换"动作（OPT-5 的并发翻译会用到）。
    """

    def __init__(self, primary: Translator, alternatives: Sequence[Translator] = ()) -> None:
        self._active: Translator = primary
        self._alternatives = list(alternatives)
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._active.name

    @property
    def active(self) -> Translator:
        """当前实际在用的渠道（报告里写它，成本才按对渠道算）。"""

        return self._active

    def translate(
        self,
        texts: Sequence[str],
        *,
        src: str,
        tgt: str,
        hints: TranslateHints | None = None,
    ) -> list[str]:
        while True:
            with self._lock:
                translator = self._active
            try:
                return translator.translate(texts, src=src, tgt=tgt, hints=hints)
            except TranslateError as exc:
                with self._lock:
                    if translator is not self._active:
                        continue  # 别的线程已经切过了，直接用新渠道重试这一批
                    if not self._alternatives:
                        raise
                    following = self._alternatives.pop(0)
                    self.events.append(
                        {
                            "from": translator.name,
                            "to": following.name,
                            "error": str(exc)[:300],
                        }
                    )
                    self._active = following


def _provider_name(options: "TranslateOptions") -> str:
    return (options.provider or os.environ.get("SUBMATRIX_TRANSLATE_PROVIDER") or "microsoft").lower()


def _fallback_chain(
    options: "TranslateOptions", primary: Translator
) -> list[Translator]:
    """备用渠道链（OPT-3）：免费 MT 失败 → 已配置的 LLM → 外部命令模板。

    只在**主渠道是免费微软端点**时启用（LLM 主渠道已有"退化输出回退微软"的兜底，
    命令渠道是用户自己接的，回退没有意义）；``--translate-fallback off`` 可关闭。
    """

    if options.fallback_provider == "off" or _provider_name(options) != "microsoft":
        return []
    chain: list[Translator] = []
    config = options.llm or LlmConfig.from_env()
    if config.configured:
        chain.append(OpenAITranslator(config, transport=options.transport, retries=options.retries))
    if options.command_template:
        chain.append(CommandTranslator(options.command_template, timeout=options.timeout))
    return chain


def build_translator(options: "TranslateOptions") -> Translator:
    provider = (options.provider or os.environ.get("SUBMATRIX_TRANSLATE_PROVIDER") or "microsoft").lower()
    if provider == "microsoft":
        return MicrosoftTranslator(
            transport=options.transport,
            timeout=options.timeout,
            retries=options.retries,
            batch_size=options.batch_size,
        )
    if provider == "command":
        if not options.command_template:
            raise TranslateError("command 渠道需要 --command-template")
        return CommandTranslator(options.command_template, timeout=options.timeout)
    if provider == "openai":
        config = options.llm or LlmConfig.from_env()
        return OpenAITranslator(config, transport=options.transport, retries=options.retries)
    if provider == "echo":
        return EchoTranslator()
    raise TranslateError(f"不支持的翻译渠道：{provider!r}（microsoft / openai / command / echo）")


# ---------------------------------------------------------------------- 清洗


def _flatten(text: str) -> str:
    return " ".join(text.replace("\\N", " ").replace("\n", " ").split())


def clean_source(text: str, *, cjk: bool) -> str:
    """清 ASR 排版噪声；这些噪声会直接拉低机翻质量。

    中文 ASR 的两个高频问题（真实采访素材实测）：

    1. **口吃式重复**："对我们对我们对我们来说"——重复片段（≥3 字）原地折叠成一次；
       两个字的重叠（看看/试试/非常非常）是正常表达，不动；
    2. **半角标点 + 标点后空格**："不会, 因为"——统一成中文标点并去掉空格，
       机翻不会把 `,` 当成新句子。
    """

    cleaned = _flatten(text)
    if not cleaned:
        return ""
    if cjk:
        for _ in range(3):  # 同一处可能重复 3 次以上，折叠一次后仍是相邻重复，循环到稳定
            folded = _CJK_REPEAT.sub(r"\1", cleaned)
            if folded == cleaned:
                break
            cleaned = folded
        cleaned = _CJK_PUNCT_SPACE.sub(lambda match: _CJK_PUNCT_MAP[match.group(0)[0]], cleaned)
        cleaned = _SPACE_BETWEEN_CJK.sub("", cleaned)
        return cleaned.strip()
    cleaned = _COMMA_GLUE.sub(r"\1 ", cleaned)
    cleaned = _PUNCT_BEFORE_SPACE.sub(r"\1", cleaned)
    cleaned = _LONE_I.sub("I", cleaned)
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def split_sentences(text: str, *, cjk: bool) -> list[str]:
    """按句末标点切整句；英文里下一句以小写开头时并回上一句（ASR 句号的误判）。"""

    if not text:
        return []
    if cjk:
        parts = [part.strip() for part in _SENTENCE_SPLIT_CJK.split(text) if part.strip()]
        return parts or [text]
    raw = [part.strip() for part in _SENTENCE_SPLIT_LATIN.split(text) if part.strip()]
    sentences: list[str] = []
    for part in raw:
        if sentences and part[:1].islower():
            sentences[-1] = f"{sentences[-1]} {part}"
        else:
            sentences.append(part)
    return sentences or [text]


def _join(parts: Iterable[str], *, joiner: str) -> str:
    return joiner.join(part for part in parts if part)


def _group_paragraphs(cues: Sequence[Cue], options: "TranslateOptions") -> list[list[int]]:
    """把相邻 cue 编成段落：上下文翻译的基本单位。"""

    groups: list[list[int]] = []
    current: list[int] = []
    for index, cue in enumerate(cues):
        if not current:
            current = [index]
            continue
        previous = cues[current[-1]]
        gap_ms = cue.timing.start_ms - previous.timing.end_ms
        text_len = sum(len(cues[i].text) for i in current) + len(cue.text)
        too_many = len(current) >= options.paragraph_max_cues
        too_long = text_len > options.paragraph_max_chars
        too_far = gap_ms > options.paragraph_gap_ms
        if too_many or too_long or too_far:
            groups.append(current)
            current = [index]
        else:
            current.append(index)
    if current:
        groups.append(current)
    return groups


def _source_sentences(
    cues: Sequence[Cue], group: Sequence[int], *, cjk: bool
) -> list[tuple[int, str]]:
    """段落内每条 cue 的源文句子（按顺序）。

    段落上下文翻译以**句子**为最小单位：一个请求里带上整段的所有句子（模型有上下文），
    返回同样多的句子，再按"句子属于哪条 cue"拼回去——对齐是精确的，不靠字数比例猜。
    """

    sentences: list[tuple[int, str]] = []
    for cue_index in group:
        for sentence in split_sentences(_flatten(cues[cue_index].text), cjk=cjk):
            sentences.append((cue_index, sentence))
    return sentences


def _ensure_en_terminal(text: str, *, continues: bool) -> str:
    """英文译文的句末标点：ASR 源文经常没有句号，直译出来就是一串没有结尾的词。

    ``continues=True`` 表示源文这条本身以逗号/分号收尾（话没说完），保持原样不动；
    否则补句号，避免成片字幕每条都像半句话。
    """

    stripped = text.rstrip()
    if not stripped or continues:
        return stripped
    if stripped[-1] in ".!?…\"')]}":
        return stripped
    if stripped[-1] in ",;:":
        return stripped[:-1].rstrip() + "."
    return stripped + "."


def _translate_units(
    translator: Translator,
    units: Sequence[str],
    *,
    src: str,
    tgt: str,
    glossary: Glossary | None,
    batch_size: int,
    budgets: Sequence[int] | None = None,
    fallback: Translator | None = None,
    notes: list[str] | None = None,
    pool: ThreadPoolExecutor | None = None,
) -> tuple[list[str], list[Term]]:
    """翻译一组文本（已是最小翻译单位），术语占位保护 + 还原 + LLM 提示。

    LLM 渠道额外有兜底：疑似音乐/噪声的条目**不送模型**；模型输出退化/非 JSON
    拆到单条仍失败时，回退免费 MT（``fallback``），避免一条噪声毁掉整条链路。

    ``pool``（OPT-5）：传入 ``ThreadPoolExecutor`` 时批与批并发，结果仍按原顺序拼回。
    """

    protected = [glossary.protect(unit) for unit in units] if glossary else None
    payload = [item.text for item in protected] if protected else list(units)
    size = max(1, batch_size)
    ranges = [(start, min(start + size, len(payload))) for start in range(0, len(payload), size)]

    def run(start: int, stop: int) -> list[str]:
        return _translate_chunk(
            translator,
            payload[start:stop],
            protected[start:stop] if protected else None,
            budgets[start:stop] if budgets is not None else None,
            src=src,
            tgt=tgt,
            fallback=fallback,
            notes=notes,
        )

    if pool is not None and len(ranges) > 1:
        chunks = list(pool.map(lambda pair: run(*pair), ranges))
    else:
        chunks = [run(start, stop) for start, stop in ranges]
    translated: list[str] = []
    for chunk in chunks:
        translated.extend(chunk)
    if len(translated) != len(payload):
        raise TranslateError(f"渠道返回条数不对：期望 {len(payload)}，得到 {len(translated)}")
    results: list[str] = []
    lost: list[Term] = []
    for index, text in enumerate(translated):
        if protected is None or glossary is None:
            # 没有术语表就没有占位符（protected 非空但 glossary 为空是内部矛盾，按无占位符兜底）
            results.append(_flatten(text))
            continue
        restored, missing = glossary.unprotect(text, protected[index])
        results.append(_flatten(restored))
        lost.extend(missing)
    return results, lost


def _fallback_lines(
    fallback: Translator,
    chunk: Sequence[str],
    *,
    src: str,
    tgt: str,
    notes: list[str] | None,
    reason: str,
) -> list[str]:
    """LLM 不可用时回退免费 MT；再失败就保留原文（宁可怪一点，不整条失败）。"""

    try:
        return fallback.translate(chunk, src=src, tgt=tgt)
    except TranslateError:
        if notes is not None:
            notes.append(f"{len(chunk)} 条{reason}，免费 MT 也失败，已保留原文")
        return list(chunk)


def _noise_fallback(
    fallback: Translator,
    chunk: Sequence[str],
    *,
    src: str,
    tgt: str,
    notes: list[str] | None,
) -> list[str]:
    if notes is not None:
        notes.append("1 条疑似音乐/噪声文本未送 LLM，已回退免费 MT")
    return _fallback_lines(fallback, chunk, src=src, tgt=tgt, notes=notes, reason="疑似噪声")


def _translate_chunk(
    translator: Translator,
    chunk: Sequence[str],
    protected: Sequence[object] | None,
    budgets: Sequence[int] | None,
    *,
    src: str,
    tgt: str,
    fallback: Translator | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    """翻一个批次；LLM 把两行并成一行时自动对半重试，而不是整批失败。"""

    # 疑似音乐/噪声（如「咪咪咪…」）不送 LLM：模型会跟着退化，即使翻译成功也没有意义
    if fallback is not None and len(chunk) == 1 and is_degenerate_text(chunk[0]):
        return _noise_fallback(fallback, chunk, src=src, tgt=tgt, notes=notes)

    if fallback is not None and len(chunk) > 1:
        noisy = [index for index, text in enumerate(chunk) if is_degenerate_text(text)]
        if noisy:
            index = noisy[0]
            if index == 0:
                head = _noise_fallback(fallback, chunk[:1], src=src, tgt=tgt, notes=notes)
                return head + _translate_chunk(
                    translator,
                    chunk[1:],
                    protected[1:] if protected else None,
                    budgets[1:] if budgets else None,
                    src=src,
                    tgt=tgt,
                    fallback=fallback,
                    notes=notes,
                )
            return _translate_chunk(
                translator,
                chunk[:index],
                protected[:index] if protected else None,
                budgets[:index] if budgets else None,
                src=src,
                tgt=tgt,
                fallback=fallback,
                notes=notes,
            ) + _translate_chunk(
                translator,
                chunk[index:],
                protected[index:] if protected else None,
                budgets[index:] if budgets else None,
                src=src,
                tgt=tgt,
                fallback=fallback,
                notes=notes,
            )

    hints = _chunk_hints(protected, budgets)
    try:
        return translator.translate(chunk, src=src, tgt=tgt, hints=hints)
    except TranslateError:
        if len(chunk) > 1:
            middle = len(chunk) // 2
            return _translate_chunk(
                translator,
                chunk[:middle],
                protected[:middle] if protected else None,
                budgets[:middle] if budgets else None,
                src=src,
                tgt=tgt,
                fallback=fallback,
                notes=notes,
            ) + _translate_chunk(
                translator,
                chunk[middle:],
                protected[middle:] if protected else None,
                budgets[middle:] if budgets else None,
                src=src,
                tgt=tgt,
                fallback=fallback,
                notes=notes,
            )
        if fallback is not None:
            if notes is not None:
                notes.append("LLM 翻译失败（输出退化/非 JSON），已回退免费 MT")
            return _fallback_lines(
                fallback, chunk, src=src, tgt=tgt, notes=notes, reason="LLM 翻译失败"
            )
        raise


def _chunk_hints(
    protected: Sequence[object] | None, budgets: Sequence[int] | None
) -> TranslateHints | None:
    entries: list[tuple[str, str, str]] = []
    if protected is not None:
        seen: set[str] = set()
        for item in protected:
            for token, term in getattr(item, "entries", ()):  # ProtectedText.entries
                if token in seen:
                    continue
                seen.add(token)
                entries.append((token, term.source, term.replacement))
    if not entries and not budgets:
        return None
    return TranslateHints(
        glossary=tuple(entries),
        budgets=tuple(int(value) for value in budgets) if budgets else None,
    )


def _slice_hints(
    hints: TranslateHints | None, start: int, stop: int
) -> TranslateHints | None:
    """拆批时只切字数预算；术语对照表是全批共用的（多带没坏处）。"""

    if hints is None:
        return None
    return TranslateHints(
        glossary=hints.glossary,
        budgets=hints.budgets[start:stop] if hints.budgets is not None else None,
    )


# ---------------------------------------------------------------------- 选项


@dataclass(frozen=True, slots=True)
class TranslateOptions:
    src: Path
    out: Path
    src_lang: str = "en"
    tgt_lang: str = "zh"
    out_name: str | None = None
    provider: str | None = None
    command_template: str | None = None
    batch_size: int = 20
    retries: int = 3
    timeout: float = 120.0
    context: str = "paragraph"  # paragraph | sentence | off
    paragraph_max_cues: int = 4
    paragraph_max_chars: int = 480
    paragraph_gap_ms: int = 1500
    glossary: Glossary | None = None
    condense: bool = True  # 默认对超 CPS 条目精简（--no-condense 关闭）
    condense_mode: str = "auto"  # rule | trim | llm | auto
    target_cps: float | None = None
    # 渠道回退（OPT-3）：auto = 免费 MT 网络失败后自动切备用渠道（已配置的 LLM / 命令模板）；
    # off = 只重试、不换渠道（失败即整条链路 failed，便于排查）
    fallback_provider: str = "auto"
    # 分块并发（OPT-5）：段落/批次并行请求，结果按原顺序拼回；1 = 串行（默认，可复现）
    concurrency: int = 1
    # 配音槽位容量（字/秒，已含提语速/atempo 余量）：给了就把精简预算放宽到
    # 「该槽位最多能念多少字」——字幕能读完 ≠ TTS 能在同一个槽位里说完。
    # 只当保险丝用：实测按裸语速（6 字/秒）压会 trim 掉 20% 的句子，得不偿失。
    dub_cps_budget: float | None = None
    llm: LlmConfig | None = None
    llm_review: bool = False  # LLM 第二遍审校（只改明显错误，成本约翻倍）
    llm_review_passes: int = 2  # 审校轮数（第二轮的输入是第一轮的成品，能拦住漏网的误译）
    generated_at: datetime | None = None
    transport: Transport | None = None  # 测试注入


@dataclass(frozen=True, slots=True)
class TranslateResult:
    status: str
    exit_code: int
    report: dict[str, object]
    report_path: Path
    output_path: Path | None
    cues: int


def _profile(code: str):
    """语言 profile（阈值唯一真相源在 :mod:`submatrix.langs`）；错误按翻译层契约抛。"""

    try:
        return lang_profile(code)
    except ValueError as exc:
        raise TranslateError(str(exc)) from None


def _cps(value_chars: int, duration_ms: int) -> float:
    return value_chars / max(duration_ms, 1) * 1000


def _cps_stats(cues: Sequence[Cue], texts: Sequence[str], *, cjk: bool, limit: float) -> dict[str, object]:
    values = [
        _cps(char_count(text, cjk=cjk), cue.timing.duration_ms)
        for cue, text in zip(cues, texts)
    ]
    over = sum(1 for value in values if value > limit)
    return {
        "limit": limit,
        "mean": round(statistics.fmean(values), 2) if values else 0.0,
        "max": round(max(values), 2) if values else 0.0,
        "over_limit": over,
    }


# ---------------------------------------------------------------------- 主流程


def _cue_budgets(cues: Sequence[Cue], target_cps: float) -> list[int]:
    """每条 cue 按"时长 × 目标 CPS"折算的译文字符预算。"""

    return [estimate_max_chars(cue.timing.duration_ms, target_cps) for cue in cues]


def _slot_budgets(cues: Sequence[Cue], cps: float) -> list[int]:
    """按"配音可用时间 × 容量字/秒"折算的字符预算。

    可用时间取「本条开始到下一条开始」（最后一条没有下一条，退回槽位时长）：
    配音的语音可以借用句间静音，用槽位算会把本来能念完的句子误判成放不下。
    """

    budgets: list[int] = []
    for index, cue in enumerate(cues):
        if index + 1 < len(cues):
            room_ms = max(cue.timing.duration_ms, cues[index + 1].timing.start_ms - cue.timing.start_ms)
        else:
            room_ms = cue.timing.duration_ms
        budgets.append(estimate_max_chars(room_ms, cps))
    return budgets


def _sentence_budgets(
    units: Sequence[str], owners: Sequence[int], cue_budgets: Sequence[int], sources: Sequence[str]
) -> list[int]:
    """句子预算 = 所属 cue 的预算按源文字数占比分摊（同一 cue 多句时按比例）。"""

    budgets: list[int] = []
    for unit, owner in zip(units, owners):
        total = len(sources[owner]) or 1
        budgets.append(max(4, round(cue_budgets[owner] * len(unit) / total)))
    return budgets


def run_translate(options: TranslateOptions) -> TranslateResult:
    """把一条 SRT 翻成目标语言，写 ``<tgt>.srt`` 与 ``translate.json``。

    四个阶段各自独立可读：翻译（三种上下文策略）→ LLM 审校 → 译后精简 → 落盘与报告。
    阶段之间只通过 :class:`_TranslationSession` 交接，不再共用一大片局部变量。
    """

    session = _TranslationSession.start(options)
    _translate_cues(session)
    _review_translations(session)
    _condense_translations(session)
    return _write_outputs(session)


@dataclass(slots=True)
class _TranslationSession:
    """一次翻译的现场：渠道、逐条译文与过程统计。

    这些字段以前全是 ``run_translate`` 的局部变量 —— 299 行里四个阶段共用一个
    命名空间，``texts`` 先被翻译写入、再被审校覆盖、又被精简覆盖一遍，读的时候
    得先记住"现在走到哪一步"。收成一个记录之后，每个阶段只声明自己读写哪几个字段。
    """

    options: TranslateOptions
    src_profile: LangProfile
    tgt_profile: LangProfile
    subtitle: Subtitle
    encoding: str
    primary: Translator
    working: Translator
    fallback: Translator | None
    fallback_notes: list[str]
    glossary: Glossary | None
    cleaned: list[str]
    joiner_tgt: str
    started: float
    target_cps: float
    cue_budgets: list[int]
    dub_budgets: list[int] | None
    condense_budgets: list[int]
    texts: list[str | None]
    rewriter: Callable[[str, int], str] | None = None
    sentences_total: int = 0
    paragraphs_total: int = 0
    fallback_cues: list[int] = field(default_factory=list)
    lost_terms: list[Term] = field(default_factory=list)
    review_changed: int = 0
    review_changes: list[dict[str, object]] = field(default_factory=list)
    review_passes: int = 0
    review_failed: int = 0
    pre_condense: list[str] = field(default_factory=list)
    condense_results: list[CondenseResult] = field(default_factory=list)
    final_texts: list[str] = field(default_factory=list)
    wall: float = 0.0

    @classmethod
    def start(cls, options: TranslateOptions) -> "_TranslationSession":
        """校验参数、读字幕、建渠道、算三种预算——翻译开始前的一次性准备。"""

        if options.fallback_provider not in {"auto", "off"}:
            raise TranslateError("渠道回退只支持 auto / off")
        src_profile = _profile(options.src_lang)
        tgt_profile = _profile(options.tgt_lang)
        subtitle, encoding = load_srt(options.src)
        if len(subtitle) == 0:
            raise TranslateError("输入字幕里没有可用 cue")

        primary = build_translator(options)
        # OPT-3：主渠道是免费 MT 时，网络类失败自动切到备用渠道（不再整条链路 failed）
        alternatives = _fallback_chain(options, primary)
        working: Translator = (
            ResilientTranslator(primary, alternatives) if alternatives else primary
        )
        # LLM 渠道的兜底：输出退化/非 JSON 时回退免费 MT（单条噪声不应毁掉整条链路）
        fallback = MicrosoftTranslator() if isinstance(primary, OpenAITranslator) else None
        cleaned = [clean_source(cue.text, cjk=src_profile.cjk) for cue in subtitle.cues]
        target_cps = options.target_cps or tgt_profile.cps
        cue_budgets = _cue_budgets(subtitle.cues, target_cps)
        # 精简预算：字幕阅读预算与配音槽位容量预算**取严的那个**——两个约束都得满足。
        # 默认语速下容量预算比阅读预算宽（中文 9.57 vs 9、英文 29.4 vs 20），所以实际由阅读
        # 预算把关（字幕可读性不能被牺牲）；只有 `--tts-rate` 调低到 TTS 根本念不完时，
        # 容量预算才会变成更严的那个，提前精简掉口播放不下的内容。
        condense_budgets = list(cue_budgets)
        dub_budgets: list[int] | None = None
        if options.dub_cps_budget and options.dub_cps_budget > 0:
            dub_budgets = _slot_budgets(subtitle.cues, float(options.dub_cps_budget))
            condense_budgets = [
                min(reading, dub) for reading, dub in zip(cue_budgets, dub_budgets)
            ]

        return cls(
            options=options,
            src_profile=src_profile,
            tgt_profile=tgt_profile,
            subtitle=subtitle,
            encoding=encoding,
            primary=primary,
            working=working,
            fallback=fallback,
            fallback_notes=[],
            glossary=options.glossary,
            cleaned=cleaned,
            joiner_tgt=_JOINERS.get(options.tgt_lang, " "),
            started=time.monotonic(),
            target_cps=target_cps,
            cue_budgets=cue_budgets,
            dub_budgets=dub_budgets,
            condense_budgets=condense_budgets,
            texts=[None] * len(cleaned),
        )


def _translate_cues(session: _TranslationSession) -> None:
    """阶段一：按 ``--context`` 把 cue 翻成译文（含分块并发与退化回退）。

    OPT-5：分块/分段落并发（结果按原顺序拼回；并发只影响墙钟，不影响对齐）。
    """

    workers = max(1, int(session.options.concurrency))
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        mode = session.options.context
        if mode == "off":
            _translate_by_cue(session, pool)
        elif mode == "sentence":
            _translate_by_sentence(session, pool)
        else:
            _translate_by_paragraph(session, pool)
    finally:
        if pool is not None:
            pool.shutdown(wait=False)

    if any(text is None for text in session.texts):
        raise TranslateError("内部错误：有 cue 没拿到译文")
    if session.tgt_profile.cjk:
        session.texts = _normalize_texts(session.texts)


def _translate_by_cue(session: _TranslationSession, pool: ThreadPoolExecutor | None) -> None:
    """``--context off``：一条 cue 就是一个翻译单位，没有上下文。"""

    options = session.options
    translated, lost = _translate_units(
        session.working,
        session.cleaned,
        src=options.src_lang,
        tgt=options.tgt_lang,
        glossary=session.glossary,
        batch_size=options.batch_size,
        budgets=session.cue_budgets,
        fallback=session.fallback,
        notes=session.fallback_notes,
        pool=pool,
    )
    session.texts = list(translated)
    session.sentences_total = len(session.cleaned)
    session.lost_terms.extend(lost)


def _translate_by_sentence(session: _TranslationSession, pool: ThreadPoolExecutor | None) -> None:
    """``--context sentence``：按句子翻，译完按"句子属于哪条 cue"拼回去。"""

    options = session.options
    units: list[str] = []
    owners: list[int] = []
    for index, text in enumerate(session.cleaned):
        for sentence in split_sentences(text, cjk=session.src_profile.cjk):
            units.append(sentence)
            owners.append(index)
    translated, lost = _translate_units(
        session.working,
        units,
        src=options.src_lang,
        tgt=options.tgt_lang,
        glossary=session.glossary,
        batch_size=options.batch_size,
        budgets=_sentence_budgets(units, owners, session.cue_budgets, session.cleaned),
        fallback=session.fallback,
        notes=session.fallback_notes,
        pool=pool,
    )
    session.lost_terms.extend(lost)
    session.sentences_total = len(units)
    grouped: dict[int, list[str]] = {}
    for owner, sentence in zip(owners, translated):
        grouped.setdefault(owner, []).append(sentence)
    for index in range(len(session.cleaned)):
        session.texts[index] = _join(grouped.get(index, []), joiner=session.joiner_tgt)


def _translate_by_paragraph(session: _TranslationSession, pool: ThreadPoolExecutor | None) -> None:
    """``--context paragraph``：整段（2–4 条相邻 cue 的所有句子）一次请求。

    对齐是精确的（不靠字数比例猜）：返回同样多的句子，再按"句子属于哪条 cue"
    拼回去，同时模型能看到整段的上下文。拼回来为空的 cue 收集起来再补翻一次。
    """

    options = session.options
    paragraphs = _group_paragraphs(session.subtitle.cues, options)
    session.paragraphs_total = len(paragraphs)
    jobs: list[tuple[list[int], list[tuple[int, str]], list[str], list[int]]] = []
    for group in paragraphs:
        source_sentences = _source_sentences(
            session.subtitle.cues, group, cjk=session.src_profile.cjk
        )
        units = [sentence for _, sentence in source_sentences]
        owners = [cue_index for cue_index, _ in source_sentences]
        jobs.append((group, source_sentences, units, owners))

    def run_group(job):  # noqa: ANN001, ANN202
        _group, _source, units, owners = job
        return _translate_units(
            session.working,
            units,
            src=options.src_lang,
            tgt=options.tgt_lang,
            glossary=session.glossary,
            batch_size=len(units) or 1,
            budgets=_sentence_budgets(units, owners, session.cue_budgets, session.cleaned),
            fallback=session.fallback,
            notes=session.fallback_notes,
        )

    if pool is not None and len(jobs) > 1:
        results = list(pool.map(run_group, jobs))
    else:
        results = [run_group(job) for job in jobs]

    missing: list[tuple[int, str]] = []
    for (group, source_sentences, _units, _owners), (translated, lost) in zip(jobs, results):
        session.lost_terms.extend(lost)
        session.sentences_total += len(translated)
        buckets: dict[int, list[str]] = {cue_index: [] for cue_index in group}
        for (cue_index, _), sentence in zip(source_sentences, translated):
            buckets[cue_index].append(sentence)
        for cue_index in group:
            text = _join(buckets[cue_index], joiner=session.joiner_tgt)
            if text:
                session.texts[cue_index] = text
            else:
                missing.append((cue_index, session.cleaned[cue_index]))
                session.fallback_cues.append(cue_index)

    if missing:
        translated, lost = _translate_units(
            session.working,
            [text for _, text in missing],
            src=options.src_lang,
            tgt=options.tgt_lang,
            glossary=session.glossary,
            batch_size=options.batch_size,
            budgets=[session.cue_budgets[cue_index] for cue_index, _ in missing],
            fallback=session.fallback,
            notes=session.fallback_notes,
            pool=pool,
        )
        session.lost_terms.extend(lost)
        for (cue_index, _), text in zip(missing, translated):
            session.texts[cue_index] = text


def _review_translations(session: _TranslationSession) -> None:
    """阶段二（可选）：LLM 第二遍审校，只改明显错误；多轮时上一轮成品进下一轮。

    审校是加分项：单段失败不毁整批（记 ``review_failed``，保留初译并如实报告）。
    """

    options = session.options
    if not options.llm_review:
        return
    if not isinstance(session.primary, OpenAITranslator):
        raise TranslateError("--llm-review 需要 LLM 渠道（--provider openai 或配置 LLM 端点）")

    for pass_index in range(1, max(1, options.llm_review_passes) + 1):
        pass_changed = 0
        for group in _group_paragraphs(session.subtitle.cues, options):
            items = [
                (index, session.cleaned[index], session.texts[index] or "")
                for index in group
                if session.texts[index]
            ]
            if not items:
                continue
            try:
                reviewed = session.primary.review(
                    [(source, translation) for _, source, translation in items],
                    src=options.src_lang,
                    tgt=options.tgt_lang,
                )
            except TranslateError:
                session.review_failed += 1
                continue
            for (index, _source, before), after in zip(items, reviewed):
                fixed = _flatten(after)
                if fixed and fixed != before:
                    pass_changed += 1
                    session.review_changed += 1
                    session.review_changes.append(
                        {"cue": index + 1, "pass": pass_index, "before": before, "after": fixed}
                    )
                if fixed:
                    session.texts[index] = fixed
        session.review_passes = pass_index
        if pass_changed == 0:
            break  # 收敛：上一轮一条都没改，没必要再花 token
    if session.tgt_profile.cjk:
        session.texts = _normalize_texts(session.texts)


def _condense_translations(session: _TranslationSession) -> None:
    """阶段三：译后精简（默认开），只碰超过目标 CPS 的条目。

    术语译法是硬约束：精简时不许把它当措辞删掉（宁可超 CPS）。
    """

    options = session.options
    keep_terms = [term.replacement for term in session.glossary] if session.glossary else []
    session.pre_condense = [text or "" for text in session.texts]
    session.rewriter = _build_rewriter(session.primary, options)
    for cue, text in zip(session.subtitle.cues, session.pre_condense):
        if not options.condense or not text:
            session.condense_results.append(CondenseResult(text, "none", 0, 0, False))
            continue
        session.condense_results.append(
            condense_text(
                text,
                session.condense_budgets[len(session.condense_results)],
                mode=options.condense_mode,
                cjk=session.tgt_profile.cjk,
                rewriter=session.rewriter,
                keep=keep_terms,
            )
        )
    final_texts = [result.text for result in session.condense_results]
    if session.tgt_profile.cjk:
        final_texts = _normalize_texts(final_texts)
    else:
        # 英文目标：ASR 源文常缺句号，补上（源文以逗号收尾=话没说完，不补）
        final_texts = [
            _ensure_en_terminal(
                text,
                continues=session.cleaned[index]
                .rstrip()
                .endswith(("，", ",", "；", ";", "：", ":")),
            ).strip()
            for index, text in enumerate(final_texts)
        ]
    session.final_texts = final_texts


def _write_outputs(session: _TranslationSession) -> TranslateResult:
    """阶段四：写译文 SRT + ``translate.json``，返回命令行契约。"""

    options = session.options
    session.wall = time.monotonic() - session.started
    out_dir = Path(options.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = options.out_name or f"{options.tgt_lang}.srt"
    output_path = out_dir / out_name
    cues = [
        Cue(timing=cue.timing, text=text)
        for cue, text in zip(session.subtitle.cues, session.final_texts)
    ]
    output_path.write_text(
        dump_srt(Subtitle(cues=tuple(cues))), encoding="utf-8", newline="\n"
    )

    actions: dict[str, int] = {}
    for result in session.condense_results:
        actions[result.action] = actions.get(result.action, 0) + 1
    rewriter_kind = "llm" if session.rewriter is not None else "rules"

    outcome = _Outcome(
        subtitle=session.subtitle,
        encoding=session.encoding,
        cues=cues,
        final_texts=session.final_texts,
        pre_condense=session.pre_condense,
        condense_results=session.condense_results,
        actions=actions,
        rewriter_kind=rewriter_kind,
        sentences=session.sentences_total,
        paragraphs=session.paragraphs_total,
        fallback_cues=session.fallback_cues,
        lost_terms=session.lost_terms,
        review_changed=session.review_changed,
        review_changes=session.review_changes,
        review_passes=session.review_passes,
        review_failed=session.review_failed,
        fallback_notes=session.fallback_notes,
        working=session.working,
        primary=session.primary,
        output_path=output_path,
        target_cps=session.target_cps,
        cue_budgets=session.cue_budgets,
        dub_budgets=session.dub_budgets,
        wall=session.wall,
    )
    warnings, dub_summary, cps_stats = _translate_warnings(
        options, outcome, cjk=session.tgt_profile.cjk
    )
    report = _translate_report(
        options,
        outcome,
        warnings=warnings,
        dub_summary=dub_summary,
        cps=cps_stats,
        cjk=session.tgt_profile.cjk,
    )
    report_path = out_dir / "translate.json"
    atomic.write_json(report_path, report)

    return TranslateResult(
        status=str(report["status"]),
        exit_code=EXIT_OK,
        report=report,
        report_path=report_path.resolve(),
        output_path=output_path.resolve(),
        cues=len(cues),
    )







@dataclass(slots=True)
class _Outcome:
    """一次翻译的成果与过程统计——写 ``translate.json`` 所需的全部结论。

    以前这些是 ``run_translate`` 尾部的局部变量，报告字面量（165 行）直接伸手抓它们，
    于是"翻译逻辑"和"报告契约"挤在同一屏里，改一个告警措辞得先分清哪些变量是翻译
    过程、哪些只是报告字段。收成一个记录之后，报告组装就是纯数据变换。
    """

    subtitle: Subtitle
    encoding: str
    cues: list[Cue]
    final_texts: list[str]
    pre_condense: list[str]
    condense_results: list[CondenseResult]
    actions: dict[str, int]
    rewriter_kind: str
    sentences: int
    paragraphs: int
    fallback_cues: list[int]
    lost_terms: list[Term]
    review_changed: int
    review_changes: list[dict[str, object]]
    review_passes: int
    review_failed: int
    fallback_notes: list[str]
    working: Translator
    primary: Translator
    output_path: Path
    target_cps: float
    cue_budgets: list[int]
    dub_budgets: list[int] | None
    wall: float


def _translate_warnings(
    options: TranslateOptions, outcome: _Outcome, *, cjk: bool
) -> tuple[list[str], dict[str, object] | None, dict[str, object]]:
    """把"这次翻译有什么需要用户注意的"汇总成告警。

    返回 ``(告警, summary.dub_budget, CPS 统计)``：后两项和对应告警出自同一次测量，
    分开各算一遍会让报告与告警口径漂移。
    """

    target_cps = outcome.target_cps
    warnings: list[str] = list(dict.fromkeys(outcome.fallback_notes))
    # OPT-3：渠道回退要如实写进告警（用户得知道这次不是免费 MT 翻的）
    for event in getattr(outcome.working, "events", []):
        warnings.append(
            f"翻译渠道 {event['from']} 失败（{str(event['error'])[:120]}），"
            f"已自动回退到 {event['to']}；前面已翻好的句子保留"
        )
    if outcome.lost_terms:
        unique = sorted({term.source for term in outcome.lost_terms})
        warnings.append(
            f"机翻吃掉了 {len(outcome.lost_terms)} 处术语占位符：{'、'.join(unique)}"
        )
    after_stats = _cps_stats(
        outcome.cues, outcome.final_texts, cjk=cjk, limit=target_cps
    )
    # 配音槽位容量：只统计"字幕能读完但 TTS 念不完"的条目（这才是配音溢出的根因）
    dub_summary: dict[str, object] | None = None
    if outcome.dub_budgets is not None:
        over_capacity = [
            index
            for index, (text, budget) in enumerate(
                zip(outcome.final_texts, outcome.dub_budgets), start=1
            )
            if char_count(text or "", cjk=cjk) > budget
        ]
        narrowed = sum(
            1
            for reading, dub in zip(outcome.cue_budgets, outcome.dub_budgets)
            if dub < reading
        )
        dub_summary = {
            "capacity_cps": round(float(options.dub_cps_budget or 0.0), 2),
            "binds": "dubbing" if narrowed else "reading",
            "cues_narrowed": narrowed,
            "over_capacity": len(over_capacity),
            "over_capacity_cues": over_capacity,
        }
        if over_capacity:
            warnings.append(
                f"{len(over_capacity)} 条译文超出配音槽位容量"
                f"（{options.dub_cps_budget:g} 字/秒，含提语速+atempo 余量）："
                "配音时仍放不下会转人工（见 dub_report.json 的 manual）"
            )
    if after_stats["over_limit"]:
        warnings.append(
            f"{after_stats['over_limit']}/{len(outcome.cues)} 条超过目标 {target_cps:g} CPS："
            "源语速快时只能靠精简译文（condense）或延长显示时间"
        )
    if options.condense:
        trimmed = outcome.actions.get("trim", 0)
        if trimmed:
            warnings.append(
                f"{trimmed} 条为压 CPS 被截断（trim 会丢信息，见 translate.json 的 needs_review）"
            )
        if outcome.actions.get("unchanged", 0) and options.condense_mode in {"llm", "auto"}:
            warnings.append("有条目精简未生效，已如实保留原译文（不硬压）")
    else:
        warnings.append("已用 --no-condense 关闭精简：超 CPS 条目原样保留")
    if outcome.review_failed:
        warnings.append(
            f"{outcome.review_failed} 段审校失败，已保留初译（未经二遍校对）"
        )
    return warnings, dub_summary, after_stats


def _translate_report(
    options: TranslateOptions,
    outcome: _Outcome,
    *,
    warnings: list[str],
    dub_summary: dict[str, object] | None,
    cps: dict[str, object],
    cjk: bool,
) -> dict[str, object]:
    """组装 ``translate.json``：输入指纹、生效配置、逐条结论统计。

    ``status`` 由告警决定：有告警就是 warn（译文可用，但用户需要知道发生了什么）。
    """

    target_cps = outcome.target_cps
    # trim / llm 都改写了句子，必须有人看一眼；rule 只删口头禅，无需抽检
    needs_review = [
        {
            "cue": index + 1,
            "action": result.action,
            "before_chars": result.before_chars,
            "after_chars": result.after_chars,
            "text": result.text,
        }
        for index, result in enumerate(outcome.condense_results)
        if result.action in {"trim", "llm"}
    ]
    fallback_events = list(getattr(outcome.working, "events", []))
    active: Translator = (
        outcome.working.active
        if isinstance(outcome.working, ResilientTranslator)
        else outcome.primary
    )
    generated_at = options.generated_at or datetime.now().astimezone()
    size, digest = file_digest(Path(options.src))
    return {
        "schema_version": 1,
        "tool": "submatrix",
        "version": __version__,
        "command": "translate",
        "generated_at": generated_at.isoformat(),
        "status": "warn" if warnings else "ok",
        "input": {
            "path": str(Path(options.src).resolve()),
            "encoding": outcome.encoding,
            "cues": len(outcome.subtitle),
            "lang": options.src_lang,
            "bytes": size,
            "sha256": digest,
        },
        "config": {
            # provider 记**实际干活**的渠道（回退到 LLM 时成本才按 token 算），primary 另记
            "provider": active.name,
            "provider_primary": outcome.primary.name,
            "provider_fallback": {
                "allowed": options.fallback_provider,
                "events": fallback_events,
            },
            "src_lang": options.src_lang,
            "tgt_lang": options.tgt_lang,
            "context": options.context,
            "batch_size": options.batch_size,
            "concurrency": max(1, int(options.concurrency)),
            # OPT-10：默认 transport 复用 HTTP 连接（TLS 只握手一次）；测试注入的 transport 记 custom
            "http_keepalive": options.transport is None,
            "paragraph_max_cues": options.paragraph_max_cues,
            "paragraph_max_chars": options.paragraph_max_chars,
            "paragraph_gap_ms": options.paragraph_gap_ms,
            "glossary": {
                "path": str(options.glossary.path)
                if options.glossary and options.glossary.path
                else None,
                "terms": len(options.glossary) if options.glossary else 0,
                # 内容指纹：改了同一个术语表文件里的译法（条数不变）也要让缓存失效
                "fingerprint": glossary_fingerprint(options.glossary),
            },
            "condense": {
                "enabled": options.condense,
                "mode": options.condense_mode if options.condense else None,
                "rewriter": outcome.rewriter_kind if options.condense else None,
                "target_cps": target_cps,
                "dub_cps_budget": float(options.dub_cps_budget)
                if options.dub_cps_budget
                else None,
            },
            "llm": {
                **(active.info if isinstance(active, OpenAITranslator) else {}),
                "review": options.llm_review,
                "review_passes": options.llm_review_passes if options.llm_review else 0,
            }
            if isinstance(active, OpenAITranslator)
            else {"review": options.llm_review, "review_passes": 0},
        },
        "outputs": [
            {
                "kind": "translation_srt",
                "path": str(outcome.output_path.resolve()),
                "cues": len(outcome.cues),
                "bytes": outcome.output_path.stat().st_size,
            }
        ],
        "summary": {
            "cues": len(outcome.cues),
            "sentences": outcome.sentences,
            "paragraphs": outcome.paragraphs,
            "fallback_cues": len(outcome.fallback_cues),
            "provider_fallback": len(fallback_events),
            "glossary_terms": len(options.glossary) if options.glossary else 0,
            "placeholder_lost": len(outcome.lost_terms),
            "condense": {
                "triggered": sum(
                    1
                    for result in outcome.condense_results
                    if result.action not in {"none", "unchanged"}
                ),
                "by_action": dict(sorted(outcome.actions.items())),
                "saved_chars": sum(result.saved for result in outcome.condense_results),
                "needs_review": needs_review,
            },
            "review": {
                "enabled": options.llm_review,
                "passes": outcome.review_passes,
                "cues_changed": outcome.review_changed,
                "changes": outcome.review_changes,
            },
            "cps_before_condense": _cps_stats(
                outcome.cues, outcome.pre_condense, cjk=cjk, limit=target_cps
            ),
            "cps": cps,
            "dub_budget": dub_summary,
            "wall_s": round(outcome.wall, 1),
        },
        "warnings": warnings,
    }


def _build_rewriter(
    translator: Translator, options: TranslateOptions
) -> Callable[[str, int], str] | None:
    if not options.condense or options.condense_mode not in {"llm", "auto"}:
        return None
    if isinstance(translator, OpenAITranslator):
        label = _profile(options.tgt_lang).label

        def rewrite(text: str, max_chars: int) -> str:
            return translator.rewrite(text, max_chars, tgt=label)

        return rewrite
    if options.condense_mode == "llm":
        raise TranslateError("--condense-mode llm 需要 --provider openai（或配置 LLM 端点）")
    return None
