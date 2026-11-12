"""外部服务成本汇总（M13）。

只做三件事，不联网、不加依赖：

1. **抽量**：从阶段报告里读事实——`translate/translate.json` 的 token 用量、
   `dub/dub_report.json` 每条字幕的字符数（复用/失败分开记）；
2. **定价**：内置一份示例价表（可用 ``--pricing 价表.json`` 深合并覆盖），
   按渠道名做子串匹配（deepseek / edge / dashscope / default）；
3. **出账**：分项金额 + 按币种小计 + 可选汇率折算成展示币种。

口径是 **artifact（产物累计）**：以当前产物报告为准，缓存重跑数字不变，
方便回答"这条视频花了多少钱"；重复 ``--force``/``--regenerate-tts`` 重跑不累计历史账单。
金额是估算，不是服务商账单。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CostError",
    "DEFAULT_PRICING",
    "build_cost",
    "collect_quantities",
    "cost_rows",
    "format_amount",
    "format_cost_line",
    "load_pricing",
    "price_quantities",
    "render_cost",
    "reprice",
]

DEFAULT_PRICING: dict[str, Any] = {
    # 展示币种与汇率：fx 表示「1 单位货币 = fx[货币] 单位 fx_base」，
    # 只用于把多种货币折算成一行给用户看，不改写分项金额
    "display_currency": "CNY",
    "fx_base": "CNY",
    "fx": {"USD": 7.2},
    "llm": {
        # 示例价（DeepSeek 官网价会调整，请按需覆盖）：CNY / 百万 token
        "deepseek": {
            "input_per_million": 2.0,
            "output_per_million": 3.0,
            "currency": "CNY",
        },
        "default": {"input_per_million": 0.0, "output_per_million": 0.0, "currency": "CNY"},
    },
    "tts": {
        "edge": {"per_10k_chars": 0.0, "currency": "CNY"},
        # 阿里云百炼 CosyVoice（M12 实测文档价）：USD / 万字符
        "dashscope": {"per_10k_chars": 0.14335, "currency": "USD"},
        "default": {"per_10k_chars": 0.0, "currency": "CNY"},
    },
}

_PRICING_NOTE = "金额为按价表的估算，不是服务商账单；用 --pricing 覆盖价目表"
_CURRENCY_SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥"}


class CostError(ValueError):
    """价表读取/格式错误（交给 CLI 统一转退出码 1）。"""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        key: (dict(value) if isinstance(value, Mapping) else value) for key, value in base.items()
    }
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """内置价表 + 用户价表深合并；``None`` 只用内置价表。"""

    pricing = copy.deepcopy(DEFAULT_PRICING)
    if path is None:
        return pricing
    pricing_path = Path(path)
    try:
        raw = pricing_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CostError(f"读不到价表：{pricing_path}") from exc
    try:
        override = json.loads(raw)
    except ValueError as exc:
        raise CostError(f"价表不是合法 JSON：{pricing_path}（{exc}）") from exc
    if not isinstance(override, dict):
        raise CostError(f"价表顶层必须是对象：{pricing_path}")
    for section in ("llm", "tts"):
        if section in override and not isinstance(override[section], dict):
            raise CostError(f"价表 {section} 必须是对象：{pricing_path}")
    return _deep_merge(pricing, override)


def _detect_tts_provider(engine: str | None, command: str | None) -> str | None:
    text = f"{engine or ''} {command or ''}".lower()
    if not text.strip():
        return None
    if any(key in text for key in ("dashscope", "cosyvoice", "bailian", "aliyun")):
        return "dashscope"
    if "edge" in text:
        return "edge"
    return "unknown"


def collect_quantities(
    translate_report: Mapping[str, Any] | None,
    dub_report: Mapping[str, Any] | None,
    *,
    tts_command: str | None = None,
) -> dict[str, Any]:
    """从两份阶段报告里抽出可计量的量，不判断价格。"""

    llm: dict[str, Any] = {
        "provider": None,
        "model": None,
        "base_url": None,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if translate_report:
        config = translate_report.get("config")
        if isinstance(config, Mapping):
            llm["provider"] = config.get("provider")
            info = config.get("llm")
            if isinstance(info, Mapping):
                llm["model"] = info.get("model")
                llm["base_url"] = info.get("base_url")
                usage = info.get("usage")
                if isinstance(usage, Mapping):
                    for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
                        value = usage.get(key)
                        if isinstance(value, int):
                            llm[key] = value
        if llm["total_tokens"] == 0:
            llm["total_tokens"] = llm["prompt_tokens"] + llm["completion_tokens"]

    tts: dict[str, Any] = {
        "engine": None,
        "provider": None,
        "cues": 0,
        "chars": 0,
        "reused_chars": 0,
        "failed_cues": 0,
    }
    if dub_report:
        config = dub_report.get("config")
        engine = config.get("engine") if isinstance(config, Mapping) else None
        tts["engine"] = engine if isinstance(engine, str) else None
        tts["provider"] = _detect_tts_provider(tts["engine"], tts_command)
        entries = dub_report.get("cues")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                tts["cues"] += 1
                if entry.get("action") == "failed":
                    tts["failed_cues"] += 1
                    continue
                chars = entry.get("chars")
                if not isinstance(chars, int) or chars < 0:
                    continue
                tts["chars"] += chars
                if entry.get("reused"):
                    tts["reused_chars"] += chars

    return {"llm": llm, "tts": tts}


def _match_rule(table: Mapping[str, Any], hints: list[str | None]) -> Mapping[str, Any]:
    """按"渠道关键字出现在 model/base_url/engine 里"匹配价表；匹配不到用 default。"""

    normalized = [str(hint).lower() for hint in hints if hint]
    for key, rule in table.items():
        if key == "default" or not isinstance(rule, Mapping):
            continue
        if any(key.lower() in hint for hint in normalized):
            return rule
    fallback = table.get("default")
    return fallback if isinstance(fallback, Mapping) else {}


def _rate(rule: Mapping[str, Any], key: str) -> tuple[float, str]:
    currency = str(rule.get("currency") or "CNY")
    try:
        return float(rule.get(key) or 0.0), currency
    except (TypeError, ValueError) as exc:
        raise CostError(f"价表字段 {key} 不是数字：{rule.get(key)!r}") from exc


def _add_item(
    items: list[dict[str, Any]],
    *,
    category: str,
    label: str,
    detail: str | None,
    quantity: int,
    unit: str,
    unit_price: float,
    price_unit: str,
    currency: str,
    divisor: float,
) -> None:
    amount = round(quantity / divisor * unit_price, 6)
    items.append(
        {
            "category": category,
            "label": label,
            "detail": detail,
            "quantity": quantity,
            "unit": unit,
            "unit_price": unit_price,
            "price_unit": price_unit,
            "currency": currency,
            "amount": amount,
        }
    )


def price_quantities(quantities: Mapping[str, Any], pricing: Mapping[str, Any]) -> dict[str, Any]:
    """把量按价表折算成分项、按币种小计、以及折算后的展示金额。"""

    items: list[dict[str, Any]] = []
    notes: list[str] = []
    llm = quantities.get("llm") or {}
    tts = quantities.get("tts") or {}

    provider = str(llm.get("provider") or "")
    if provider == "openai":
        model = llm.get("model")
        rule = _match_rule(
            _table(pricing, "llm"),
            [model if isinstance(model, str) else None, llm.get("base_url"), provider],
        )
        rate_in, currency = _rate(rule, "input_per_million")
        rate_out, _ = _rate(rule, "output_per_million")
        detail = str(model) if model else "openai"
        prompt_tokens = int(llm.get("prompt_tokens") or 0)
        completion_tokens = int(llm.get("completion_tokens") or 0)
        if prompt_tokens:
            _add_item(
                items,
                category="llm",
                label="输入 tokens",
                detail=detail,
                quantity=prompt_tokens,
                unit="token",
                unit_price=rate_in,
                price_unit="1M token",
                currency=currency,
                divisor=1_000_000,
            )
        if completion_tokens:
            _add_item(
                items,
                category="llm",
                label="输出 tokens",
                detail=detail,
                quantity=completion_tokens,
                unit="token",
                unit_price=rate_out,
                price_unit="1M token",
                currency=currency,
                divisor=1_000_000,
            )
        if not prompt_tokens and not completion_tokens:
            if int(llm.get("calls") or 0) > 0:
                notes.append("LLM 渠道调用未返回 token 用量，费用未计入")
    elif provider:
        notes.append(f"翻译渠道 {provider} 不按 token 计费（0 元）")

    tts_provider = str(tts.get("provider") or "")
    chars = int(tts.get("chars") or 0)
    if chars:
        rule = _match_rule(
            _table(pricing, "tts"), [tts_provider or None, tts.get("engine")]
        )
        rate, currency = _rate(rule, "per_10k_chars")
        _add_item(
            items,
            category="tts",
            label="TTS 字符",
            detail=tts_provider or "unknown",
            quantity=chars,
            unit="字符",
            unit_price=rate,
            price_unit="1万字符",
            currency=currency,
            divisor=10_000,
        )
        if tts_provider == "unknown":
            notes.append("未识别的 TTS 渠道，默认按 0 元计；可用 --pricing 覆盖")
    reused = int(tts.get("reused_chars") or 0)
    if reused:
        notes.append(f"TTS 字符含复用已有音频 {reused:,} 字符（产物累计口径，表示当初已生成）")
    if int(tts.get("failed_cues") or 0):
        notes.append(f"{int(tts['failed_cues'])} 条配音失败，未计入字符数")

    totals: dict[str, float] = {}
    for item in items:
        currency = item["currency"]
        totals[currency] = round(totals.get(currency, 0.0) + float(item["amount"]), 6)

    display_currency = str(pricing.get("display_currency") or "CNY").upper()
    fx_base = str(pricing.get("fx_base") or "CNY").upper()
    raw_fx = pricing.get("fx")
    fx: dict[str, float] = {}
    if isinstance(raw_fx, Mapping):
        for key, value in raw_fx.items():
            try:
                fx[str(key).upper()] = float(value)
            except (TypeError, ValueError) as exc:
                raise CostError(f"价表汇率不是数字：{key}={value!r}") from exc
    # 先全部折成 fx_base，再折算成展示币种；缺汇率就如实标注，不硬编
    base_amount = 0.0
    missing: list[str] = []
    for currency, value in totals.items():
        if currency == fx_base:
            base_amount += value
        elif currency in fx:
            base_amount += value * fx[currency]
        else:
            missing.append(currency)
    if display_currency == fx_base:
        converted = base_amount
    elif display_currency in fx and fx[display_currency] > 0:
        converted = base_amount / fx[display_currency]
    else:
        converted = 0.0
        missing.append(display_currency)
    if missing:
        notes.append(f"缺少汇率，未折算：{'/'.join(dict.fromkeys(missing))}")

    display = {
        "currency": display_currency,
        "amount": round(converted, 6),
        "converted": bool(totals) and not missing,
    }
    notes.append(_PRICING_NOTE)
    return {"items": items, "totals": totals, "display": display, "notes": notes}


def _build_block(
    quantities: Mapping[str, Any],
    pricing: Mapping[str, Any],
    pricing_path: str | Path | None,
) -> dict[str, Any]:
    priced = price_quantities(quantities, pricing)
    source = str(Path(pricing_path).resolve()) if pricing_path is not None else "builtin"
    return {
        "schema_version": 1,
        "basis": "artifact",
        "pricing": {
            "source": source,
            "display_currency": priced["display"]["currency"],
            "fx_base": str(pricing.get("fx_base") or "CNY"),
            "fx": dict(pricing.get("fx") or {}),
        },
        "quantities": copy.deepcopy(dict(quantities)),
        "items": priced["items"],
        "totals": priced["totals"],
        "display": priced["display"],
        "notes": list(dict.fromkeys(priced["notes"])),
    }


def build_cost(
    work_dir: str | Path,
    *,
    tts_command: str | None = None,
    pricing: Mapping[str, Any] | None = None,
    pricing_path: str | Path | None = None,
) -> dict[str, Any]:
    """读 ``<work>/translate/translate.json`` + ``<work>/dub/dub_report.json`` 出账。"""

    root = Path(work_dir)
    translate_report = _read_json(root / "translate" / "translate.json")
    dub_report = _read_json(root / "dub" / "dub_report.json")
    quantities = collect_quantities(translate_report, dub_report, tts_command=tts_command)
    effective_pricing = pricing if pricing is not None else load_pricing(pricing_path)
    return _build_block(quantities, effective_pricing, pricing_path)


def reprice(
    cost: Mapping[str, Any], pricing: Mapping[str, Any], *, pricing_path: str | Path | None = None
) -> dict[str, Any]:
    """用新价表重算一份已有 cost 块（量不变，只换价）。"""

    quantities = cost.get("quantities")
    if not isinstance(quantities, Mapping):
        raise CostError("cost 块缺少 quantities，无法重算")
    return _build_block(quantities, pricing, pricing_path)


def _money(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) < 0.0001:
        return f"{value:.6f}"
    return f"{value:.4f}"


def _symbol(currency: str) -> str:
    return _CURRENCY_SYMBOL.get(currency, f"{currency} ")


def format_amount(cost: Mapping[str, Any]) -> str:
    """只给金额（带币种符号），给批量结果表用。"""

    display = cost.get("display") or {}
    amount = float(display.get("amount") or 0.0)
    currency = str(display.get("currency") or "CNY")
    return f"{_symbol(currency)}{amount:.4g}"


def format_cost_line(cost: Mapping[str, Any]) -> str:
    """给 pipeline 摘要/日志用的一行。"""

    display = cost.get("display") or {}
    amount = float(display.get("amount") or 0.0)
    currency = str(display.get("currency") or "CNY")
    quantities = cost.get("quantities") or {}
    llm = quantities.get("llm") or {}
    tts = quantities.get("tts") or {}
    parts: list[str] = []
    total_tokens = int(llm.get("total_tokens") or 0)
    if total_tokens:
        parts.append(f"LLM {total_tokens:,} tokens")
    chars = int(tts.get("chars") or 0)
    if chars:
        parts.append(f"TTS {chars:,} 字符")
    detail = "，".join(parts) if parts else "无外部计费调用"
    return f"成本：≈{_symbol(currency)}{amount:.4g}（{detail}；按价表估算）"


def cost_rows(cost: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """给 GUI 表格用的行（中文字段名，金额含币种）。"""

    if not cost:
        return []
    rows: list[dict[str, Any]] = []
    items = cost.get("items") or []
    if not items:
        rows.append(
            {
                "类别": "—",
                "项目": "无外部计费调用（本地 ASR/分离/BGM + 免费渠道）",
                "数量": None,
                "单位": None,
                "单价": None,
                "币种": "",
                "金额": "0",
            }
        )
    for item in items:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "")
        detail = item.get("detail")
        if detail:
            label = f"{label}（{detail}）"
        rows.append(
            {
                "类别": str(item.get("category") or ""),
                "项目": label,
                "数量": int(item.get("quantity") or 0),
                "单位": str(item.get("unit") or ""),
                "单价": f"{float(item.get('unit_price') or 0):g} {item.get('currency')}/"
                f"{item.get('price_unit')}",
                "币种": str(item.get("currency") or ""),
                "金额": _money(float(item.get("amount") or 0)),
            }
        )
    totals = cost.get("totals") or {}
    for currency, value in totals.items():
        rows.append(
            {
                "类别": "合计",
                "项目": "本产物累计",
                "数量": None,
                "单位": None,
                "单价": None,
                "币种": str(currency),
                "金额": _money(float(value)),
            }
        )
    display = cost.get("display") or {}
    if display:
        rows.append(
            {
                "类别": "折算",
                "项目": "按汇率折算",
                "数量": None,
                "单位": None,
                "单价": None,
                "币种": str(display.get("currency") or ""),
                "金额": _money(float(display.get("amount") or 0)),
            }
        )
    return rows


def render_cost(cost: Mapping[str, Any]) -> str:
    """给 CLI 的多行文本。"""

    lines = [f"成本汇总（口径：{cost.get('basis') or 'artifact'}）"]
    items = cost.get("items") or []
    if not items:
        lines.append("  无外部计费调用（本地 ASR/分离/BGM + 免费翻译渠道 + 免费 TTS）")
    for item in items:
        label = str(item.get("label") or "")
        detail = item.get("detail")
        if detail:
            label = f"{label}（{detail}）"
        lines.append(
            f"  {label}：{int(item.get('quantity') or 0):,} {item.get('unit')} × "
            f"{float(item.get('unit_price') or 0):g} {item.get('currency')}/{item.get('price_unit')}"
            f" = {item.get('currency')} {_money(float(item.get('amount') or 0))}"
        )
    totals = cost.get("totals") or {}
    if totals:
        joined = " + ".join(f"{currency} {_money(float(value))}" for currency, value in totals.items())
        display = cost.get("display") or {}
        if display.get("converted"):
            joined += (
                f" ≈ {_symbol(str(display.get('currency')))}"
                f"{float(display.get('amount') or 0):.4g}"
            )
        lines.append(f"  合计：{joined}")
    for note in cost.get("notes") or []:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def _table(source: Mapping[str, object], key: str) -> Mapping[str, object]:
    """取价表里的一段（不是映射就当作空表）：``pricing.get("llm") or {}`` 的收窄版。"""

    value = source.get(key)
    return value if isinstance(value, Mapping) else {}
