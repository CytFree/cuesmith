"""成本汇总：抽量、定价、币种折算、CLI 输出（M13）。

不联网：价表是本地 JSON，量来自假的阶段报告。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from submatrix.cli import main
from submatrix.cost import (
    CostError,
    DEFAULT_PRICING,
    build_cost,
    collect_quantities,
    cost_rows,
    format_amount,
    format_cost_line,
    load_pricing,
    price_quantities,
    render_cost,
    reprice,
)


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def translate_report(
    *,
    provider: str = "openai",
    model: str = "deepseek-chat",
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    calls: int = 2,
) -> dict:
    return {
        "config": {
            "provider": provider,
            "llm": {
                "model": model,
                "base_url": "https://api.deepseek.com/v1",
                "usage": {
                    "calls": calls,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        }
    }


def dub_report(engine: str = "CommandTtsEngine", entries: list[dict] | None = None) -> dict:
    return {
        "config": {"engine": engine},
        "summary": {"cues": 3, "synthesized": 2, "reused": 1, "failed": 1},
        "cues": entries
        if entries is not None
        else [
            {"cue": 1, "chars": 100, "reused": False, "action": "none"},
            {"cue": 2, "chars": 50, "reused": True, "action": "atempo"},
            {"cue": 3, "chars": 20, "reused": False, "action": "failed"},
        ],
    }


def make_work(root: Path, *, translate: dict | None, dub: dict | None) -> Path:
    if translate is not None:
        write_json(root / "translate" / "translate.json", translate)
    if dub is not None:
        write_json(root / "dub" / "dub_report.json", dub)
    return root


def dashscope_work(root: Path) -> Path:
    return make_work(root, translate=translate_report(), dub=dub_report())


def tts_item(priced: dict) -> dict:
    return next(item for item in priced["items"] if item["category"] == "tts")


# ---------------------------------------------------------------- 抽量


def test_collect_quantities_splits_llm_and_tts() -> None:
    quantities = collect_quantities(
        translate_report(),
        dub_report(),
        tts_command=r"tools\tts_dashscope.py",
    )

    llm = quantities["llm"]
    assert llm["provider"] == "openai"
    assert llm["model"] == "deepseek-chat"
    assert (llm["prompt_tokens"], llm["completion_tokens"], llm["total_tokens"]) == (1000, 500, 1500)
    tts = quantities["tts"]
    assert tts["engine"] == "CommandTtsEngine"
    assert tts["provider"] == "dashscope"
    assert (tts["cues"], tts["chars"], tts["reused_chars"], tts["failed_cues"]) == (3, 150, 50, 1)


def test_collect_quantities_without_reports_is_zero() -> None:
    quantities = collect_quantities(None, None)
    assert quantities["llm"]["total_tokens"] == 0
    assert quantities["llm"]["provider"] is None
    assert quantities["tts"]["chars"] == 0
    assert quantities["tts"]["provider"] is None


def test_edge_engine_detected_from_engine_name() -> None:
    quantities = collect_quantities(None, dub_report(engine="EdgeTtsEngine"))
    assert quantities["tts"]["provider"] == "edge"


# ---------------------------------------------------------------- 定价


def test_free_channels_price_zero() -> None:
    quantities = collect_quantities(
        translate_report(provider="microsoft"), dub_report(engine="EdgeTtsEngine")
    )
    priced = price_quantities(quantities, DEFAULT_PRICING)

    # 微软翻译不出 token 条目；Edge-TTS 仍然记录字符数，但单价 0
    assert [item["category"] for item in priced["items"]] == ["tts"]
    assert priced["items"][0]["unit_price"] == 0.0
    assert priced["totals"] == {"CNY": 0.0}
    assert priced["display"] == {"currency": "CNY", "amount": 0.0, "converted": True}
    assert any("microsoft" in note for note in priced["notes"])


def test_deepseek_tokens_priced_per_million() -> None:
    quantities = collect_quantities(
        translate_report(prompt_tokens=1_000_000, completion_tokens=500_000),
        None,
        tts_command="",
    )
    priced = price_quantities(quantities, DEFAULT_PRICING)

    by_label = {item["label"]: item for item in priced["items"]}
    assert by_label["输入 tokens"]["amount"] == pytest.approx(2.0)
    assert by_label["输出 tokens"]["amount"] == pytest.approx(1.5)
    assert priced["totals"] == {"CNY": pytest.approx(3.5)}
    assert priced["display"]["amount"] == pytest.approx(3.5)


def test_dashscope_chars_priced_in_usd_and_converted_for_display() -> None:
    quantities = collect_quantities(
        None,
        dub_report(
            entries=[{"cue": 1, "chars": 10_000, "reused": False, "action": "none"}]
        ),
        tts_command="python tools/tts_dashscope.py",
    )
    priced = price_quantities(quantities, DEFAULT_PRICING)

    item = priced["items"][0]
    assert item["category"] == "tts"
    assert (item["unit"], item["unit_price"], item["currency"]) == ("字符", 0.14335, "USD")
    assert item["amount"] == pytest.approx(0.14335)
    assert priced["totals"] == {"USD": pytest.approx(0.14335)}
    assert priced["display"]["currency"] == "CNY"
    assert priced["display"]["amount"] == pytest.approx(0.14335 * 7.2, rel=1e-6)
    assert priced["display"]["converted"] is True


def test_unknown_tts_provider_is_zero_with_note() -> None:
    quantities = collect_quantities(
        None,
        dub_report(engine="CommandTtsEngine"),
        tts_command=r"C:\tools\my_tts.exe",
    )
    priced = price_quantities(quantities, DEFAULT_PRICING)

    assert quantities["tts"]["provider"] == "unknown"
    assert priced["items"][0]["amount"] == 0.0
    assert any("未识别的 TTS 渠道" in note for note in priced["notes"])


def test_missing_fx_keeps_currency_unconverted_and_notes() -> None:
    quantities = collect_quantities(
        None,
        dub_report(entries=[{"cue": 1, "chars": 10_000, "reused": False, "action": "none"}]),
        tts_command="tts_dashscope",
    )
    pricing = dict(DEFAULT_PRICING)
    pricing["display_currency"] = "GBP"
    priced = price_quantities(quantities, pricing)

    assert priced["display"]["converted"] is False
    assert any("缺少汇率" in note for note in priced["notes"])


def test_llm_usage_without_tokens_is_noted() -> None:
    report = translate_report(prompt_tokens=0, completion_tokens=0, calls=3)
    quantities = collect_quantities(report, None)
    priced = price_quantities(quantities, DEFAULT_PRICING)

    assert priced["items"] == []
    assert any("未返回 token 用量" in note for note in priced["notes"])


# ---------------------------------------------------------------- 价表


def test_pricing_override_deep_merges_defaults(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "pricing.json",
        {"llm": {"deepseek": {"input_per_million": 4.0}}},
    )
    pricing = load_pricing(path)

    # 只改输入价，输出价保留内置默认
    assert pricing["llm"]["deepseek"]["input_per_million"] == 4.0
    assert pricing["llm"]["deepseek"]["output_per_million"] == 3.0
    assert pricing["tts"]["dashscope"]["per_10k_chars"] == 0.14335


def test_pricing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(CostError, match="读不到价表"):
        load_pricing(tmp_path / "missing.json")

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CostError, match="不是合法 JSON"):
        load_pricing(bad)

    wrong = write_json(tmp_path / "wrong.json", {"llm": [1, 2]})
    with pytest.raises(CostError, match="必须是对象"):
        load_pricing(wrong)


# ---------------------------------------------------------------- 成本块


def test_build_cost_artifact_basis_and_reuse_note(tmp_path: Path) -> None:
    work = dashscope_work(tmp_path)
    cost = build_cost(work, tts_command=r"tools\tts_dashscope.py")

    assert cost["basis"] == "artifact"
    assert cost["pricing"]["source"] == "builtin"
    assert cost["quantities"]["tts"]["chars"] == 150
    assert cost["quantities"]["tts"]["reused_chars"] == 50
    assert any("复用已有音频" in note for note in cost["notes"])
    assert cost["display"]["amount"] > 0


def test_build_cost_without_reports_is_empty(tmp_path: Path) -> None:
    cost = build_cost(tmp_path / "empty")

    assert cost["items"] == []
    assert cost["totals"] == {}
    assert cost["display"]["amount"] == 0.0
    assert "无外部计费调用" in render_cost(cost)


def test_pricing_path_is_recorded(tmp_path: Path) -> None:
    work = dashscope_work(tmp_path)
    path = write_json(tmp_path / "pricing.json", {"tts": {"dashscope": {"per_10k_chars": 1.0}}})
    cost = build_cost(work, tts_command="tts_dashscope", pricing_path=path)

    assert cost["pricing"]["source"] == str(path.resolve())
    assert tts_item(cost)["unit_price"] == 1.0


def test_reprice_keeps_quantities_and_swaps_prices(tmp_path: Path) -> None:
    work = dashscope_work(tmp_path)
    cost = build_cost(work, tts_command="tts_dashscope")
    pricing = load_pricing()
    pricing["tts"]["dashscope"]["per_10k_chars"] = 1.0

    repriced = reprice(cost, pricing)

    assert repriced["quantities"] == cost["quantities"]
    assert tts_item(repriced)["unit_price"] == 1.0
    assert repriced["totals"]["USD"] == pytest.approx(0.015)


def test_format_cost_line_and_rows(tmp_path: Path) -> None:
    cost = build_cost(dashscope_work(tmp_path), tts_command="tts_dashscope")

    line = format_cost_line(cost)
    assert line.startswith("成本：≈¥")
    assert "LLM 1,500 tokens" in line
    assert "TTS 150 字符" in line
    assert format_amount(cost) == f"¥{cost['display']['amount']:.4g}"

    rows = cost_rows(cost)
    labels = [row["项目"] for row in rows]
    assert any(label.startswith("输入 tokens") for label in labels)
    assert any(row["类别"] == "合计" for row in rows)
    assert any(row["类别"] == "折算" for row in rows)
    assert cost_rows(None) == []


# ---------------------------------------------------------------- CLI


def _write_pipeline(work: Path, cost: dict | None) -> Path:
    payload: dict = {"status": "ok", "cost": cost} if cost is not None else {"status": "ok"}
    return write_json(work / "pipeline.json", payload)


def test_cli_cost_prints_stored_block(tmp_path: Path, capsys) -> None:
    work = dashscope_work(tmp_path)
    cost = build_cost(work, tts_command="tts_dashscope")
    _write_pipeline(work, cost)

    code = main(["cost", "--work", str(work)])
    output = capsys.readouterr().out
    assert code == 0
    assert "成本汇总" in output
    assert "合计" in output


def test_cli_cost_json_and_reprice(tmp_path: Path, capsys) -> None:
    work = dashscope_work(tmp_path)
    _write_pipeline(work, build_cost(work, tts_command="tts_dashscope"))
    pricing = write_json(
        tmp_path / "pricing.json", {"tts": {"dashscope": {"per_10k_chars": 1.0}}}
    )

    assert main(["cost", "--work", str(work), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["basis"] == "artifact"
    assert tts_item(payload)["unit_price"] == 0.14335

    assert main(["cost", "--work", str(work), "--json", "--pricing", str(pricing)]) == 0
    repriced = json.loads(capsys.readouterr().out)
    assert tts_item(repriced)["unit_price"] == 1.0
    assert repriced["pricing"]["source"].endswith("pricing.json")


def test_cli_cost_backfills_old_pipeline(tmp_path: Path, capsys) -> None:
    work = dashscope_work(tmp_path)
    _write_pipeline(work, None)

    code = main(["cost", "--work", str(work)])
    captured = capsys.readouterr()
    assert code == 0
    assert "没有 cost 块" in captured.err
    assert "成本汇总" in captured.out


def test_cost_rows_empty_cost_shows_free_note() -> None:
    rows = cost_rows({"items": [], "totals": {}, "display": {"currency": "CNY", "amount": 0.0}})

    assert rows[0]["项目"].startswith("无外部计费调用")
    assert rows[-1]["类别"] == "折算"


def test_cli_cost_tts_command_identifies_provider(tmp_path: Path, capsys) -> None:
    work = make_work(
        tmp_path,
        translate=None,
        dub=dub_report(
            entries=[{"cue": 1, "chars": 10_000, "reused": False, "action": "none"}]
        ),
    )
    _write_pipeline(work, None)

    assert (
        main(["cost", "--work", str(work), "--json", "--tts-command", "tools/tts_dashscope.py"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["quantities"]["tts"]["provider"] == "dashscope"
    assert tts_item(payload)["unit_price"] == 0.14335


def test_cli_cost_missing_pipeline_returns_usage(tmp_path: Path, capsys) -> None:
    assert main(["cost", "--work", str(tmp_path / "nope")]) == 1
    assert "成本汇总失败" in capsys.readouterr().err


def test_cli_cost_bad_pricing_returns_usage(tmp_path: Path, capsys) -> None:
    work = dashscope_work(tmp_path)
    _write_pipeline(work, build_cost(work, tts_command="tts_dashscope"))
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")

    assert main(["cost", "--work", str(work), "--pricing", str(bad)]) == 1
    assert "成本汇总失败" in capsys.readouterr().err
