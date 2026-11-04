"""翻译编排（第 ③④⑥ 层）：上下文翻译 + 术语表 + 译后精简 + CPS 报告。

这里不测"翻译质量"（那要看真实素材），只测编排契约：
- 段落上下文翻译后能按比例把译文放回每个 cue，且不丢字；
- 术语表占位/还原、丢失要如实报；
- 精简只在超 CPS 时触发；
- 各渠道（microsoft / openai / command / echo）的输入输出契约与失败行为。
"""

from __future__ import annotations

import json
import sys
import urllib.error

import pytest

from submatrix import translate as translate_module
from submatrix.glossary import Glossary, Term
from submatrix.srt import Cue, Subtitle, Timing, dump_srt, load_srt
from submatrix.translate import (
    CommandTranslator,
    LlmConfig,
    MicrosoftTranslator,
    OpenAITranslator,
    TranslateError,
    TranslateOptions,
    build_translator,
    clean_source,
    normalize_cjk_spacing,
    run_translate,
)
from submatrix.tts import speech_capacity_cps


def write_srt(path, *cues: tuple[int, int, str]):
    subtitle = Subtitle(
        cues=tuple(Cue(timing=Timing(start, end), text=text) for start, end, text in cues)
    )
    path.write_text(dump_srt(subtitle), encoding="utf-8", newline="\n")
    return path


class FakeTranslator:
    """记录调用、按函数返回译文。"""

    name = "fake"

    def __init__(self, fn):
        self.fn = fn
        self.calls: list[list[str]] = []
        self.hints: list[object] = []

    def translate(self, texts, *, src, tgt, hints=None):
        self.calls.append(list(texts))
        self.hints.append(hints)
        return [self.fn(text) for text in texts]


def use_fake(monkeypatch, translator: FakeTranslator) -> FakeTranslator:
    monkeypatch.setattr(translate_module, "build_translator", lambda options: translator)
    return translator


# ---------------------------------------------------------------- 纯函数


def test_clean_source_fixes_asr_noise() -> None:
    assert clean_source("actually,i could do", cjk=False) == "Actually, I could do"
    assert clean_source("hello ,world", cjk=False) == "Hello, world"
    assert clean_source("多  余 空格", cjk=True) == "多余空格"  # 中文字间空格是 ASR 噪声


def test_normalize_cjk_spacing() -> None:
    assert normalize_cjk_spacing("一个 演示文稿，一个 电子表格。") == "一个演示文稿，一个电子表格。"
    assert normalize_cjk_spacing("和 AI 对话") == "和 AI 对话"  # 中英之间的空格保留
    assert normalize_cjk_spacing("内容 。") == "内容。"


# ---------------------------------------------------------------- 编排


def test_paragraph_request_carries_all_sentences_as_context(tmp_path, monkeypatch) -> None:
    """段落模式的上下文 = 整段句子放进同一个请求；返回后按句子归属精确拼回 cue。"""

    src = write_srt(
        tmp_path / "en.srt",
        (0, 2000, "First part."),
        (2000, 4000, "Second part."),
        (4000, 6000, "Third part."),
    )
    mapping = {
        "First part.": "第一部分。",
        "Second part.": "第二部分。",
        "Third part.": "第三部分。",
    }
    fake = use_fake(monkeypatch, FakeTranslator(lambda text: mapping[text]))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", src_lang="en", tgt_lang="zh")
    )

    assert fake.calls == [["First part.", "Second part.", "Third part."]]
    cues = load_srt(result.output_path)[0].cues
    assert [cue.text for cue in cues] == ["第一部分。", "第二部分。", "第三部分。"]
    assert result.report["summary"]["paragraphs"] == 1


def test_paragraph_sentences_map_back_to_their_own_cue(tmp_path, monkeypatch) -> None:
    """一句对一句：中间短 cue 必须拿到它自己那句短译文（不靠字数比例猜）。"""

    src = write_srt(
        tmp_path / "en.srt",
        (0, 6000, "A long sentence number one that keeps going for a while."),
        (6000, 7500, "Short."),
        (7500, 13000, "Another long sentence that also keeps going for a while."),
    )
    mapping = {
        "A long sentence number one that keeps going for a while.": "第一段很长的中文译文。",
        "Short.": "短句。",
        "Another long sentence that also keeps going for a while.": "第二段很长的中文译文。",
    }
    use_fake(monkeypatch, FakeTranslator(lambda text: mapping[text]))

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out"))

    cues = load_srt(result.output_path)[0].cues
    assert [cue.text for cue in cues] == [
        "第一段很长的中文译文。",
        "短句。",
        "第二段很长的中文译文。",
    ]


def test_multi_sentence_cue_joins_its_sentences(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 4000, "First one. Second one."))
    mapping = {"First one.": "第一句。", "Second one.": "第二句。"}
    use_fake(monkeypatch, FakeTranslator(lambda text: mapping[text]))

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out"))

    assert load_srt(result.output_path)[0].cues[0].text == "第一句。第二句。"


def test_context_off_translates_each_cue(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "One."), (1000, 2000, "Two."))
    fake = use_fake(monkeypatch, FakeTranslator(lambda text: f"译:{text}"))

    run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", context="off")
    )

    assert fake.calls == [["One.", "Two."]]


def test_sentence_mode_splits_into_sentences(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 3000, "Hello there. How are you?"))
    fake = use_fake(monkeypatch, FakeTranslator(lambda text: "你好。" if text.startswith("Hello") else "你好吗？"))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", context="sentence")
    )

    assert fake.calls == [["Hello there.", "How are you?"]]
    assert load_srt(result.output_path)[0].cues[0].text == "你好。你好吗？"


def test_lowercase_sentence_start_is_merged(tmp_path, monkeypatch) -> None:
    """"... capable of. and it could" 这种 ASR 假句号不能切成两句。"""

    src = write_srt(tmp_path / "en.srt", (0, 2000, "Capable of. and it could."))
    fake = use_fake(monkeypatch, FakeTranslator(lambda text: f"译:{text}"))

    run_translate(TranslateOptions(src=src, out=tmp_path / "out", context="sentence"))

    assert fake.calls[0] == ["Capable of. and it could."]


# ---------------------------------------------------------------- 术语表


def test_glossary_placeholders_are_restored(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 2000, "I used Blender for rendering."))
    glossary = Glossary(terms=(Term(source="Blender", keep=True),))
    seen: list[str] = []

    def fake_translate(text: str) -> str:
        seen.append(text)
        return "我用 {{G0}} 做渲染。"

    use_fake(monkeypatch, FakeTranslator(fake_translate))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", glossary=glossary)
    )

    assert "Blender" not in seen[0]  # 送进机翻前已被占位
    assert "{{G0}}" in seen[0]
    cues = load_srt(result.output_path)[0].cues
    assert cues[0].text == "我用 Blender 做渲染。"
    assert result.report["summary"]["placeholder_lost"] == 0


def test_lost_placeholder_is_warned(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 2000, "I used Blender."))
    glossary = Glossary(terms=(Term(source="Blender", keep=True),))
    use_fake(monkeypatch, FakeTranslator(lambda text: "我用它渲染。"))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", glossary=glossary)
    )

    assert result.report["summary"]["placeholder_lost"] == 1
    assert any("占位符" in warning for warning in result.report["warnings"])


# ---------------------------------------------------------------- 精简


def test_condense_triggers_by_default(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "This is useful."))
    use_fake(monkeypatch, FakeTranslator(lambda text: "那么其实这个功能非常有用。"))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", condense_mode="rule", target_cps=9.0)
    )

    text = load_srt(result.output_path)[0].cues[0].text
    assert text == "这个功能非常有用。"
    assert result.report["summary"]["condense"]["triggered"] == 1
    assert result.report["summary"]["condense"]["by_action"]["fillers"] == 1
    assert result.report["config"]["condense"]["enabled"] is True


def test_condense_disabled_keeps_translation(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "This is useful."))
    use_fake(monkeypatch, FakeTranslator(lambda text: "那么其实这个功能非常有用。"))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", condense=False, target_cps=9.0)
    )

    assert load_srt(result.output_path)[0].cues[0].text == "那么其实这个功能非常有用。"
    assert result.report["summary"]["condense"]["triggered"] == 0
    assert result.report["config"]["condense"]["mode"] is None
    assert any("--no-condense" in warning for warning in result.report["warnings"])


# ---------------------------------------------------------------- 配音槽位预算（OPT-2）


def test_slot_budgets_borrow_gap_between_cues() -> None:
    """可用时间 = 本条开始到下一条开始（句间静音也算），最后一条退回槽位时长。"""

    cues = [
        Cue(timing=Timing(0, 1000), text="a"),  # 槽位 1s，但下一条 3s 才开始 → 3s
        Cue(timing=Timing(3000, 5000), text="b"),  # 最后一条 → 2s
    ]

    budgets = translate_module._slot_budgets(cues, 10.0)

    assert budgets == [30, 20]


def test_dub_budget_keeps_reading_limit_as_the_stricter_one(tmp_path, monkeypatch) -> None:
    """默认语速下容量预算比阅读预算宽：字幕可读性不能被牺牲，精简结果和以前一致。"""

    src = write_srt(tmp_path / "zh.srt", (0, 2000, "这句话挺长的。"))
    english = "This sentence is long enough, it needs trimming here."  # 53 字符
    use_fake(monkeypatch, FakeTranslator(lambda text: english))

    plain = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "plain",
            src_lang="zh",
            tgt_lang="en",
            condense_mode="trim",
        )
    )
    budgeted = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "budgeted",
            src_lang="zh",
            tgt_lang="en",
            condense_mode="trim",
            dub_cps_budget=speech_capacity_cps("en", "+20%"),
        )
    )

    trimmed = load_srt(plain.output_path)[0].cues[0].text
    kept = load_srt(budgeted.output_path)[0].cues[0].text
    assert len(trimmed) < len(english)  # 阅读 CPS 20 × 2s = 40 字符 → 截断
    assert kept == trimmed  # 容量预算更宽，不改变结果（仍按阅读上限截断）
    summary = budgeted.report["summary"]["dub_budget"]
    assert summary["binds"] == "reading"
    assert summary["cues_narrowed"] == 0
    assert summary["over_capacity"] == 0
    assert budgeted.report["config"]["condense"]["dub_cps_budget"] == pytest.approx(
        speech_capacity_cps("en", "+20%"), abs=0.01
    )


def test_dub_budget_tightens_when_tts_rate_is_slow(tmp_path, monkeypatch) -> None:
    """`--tts-rate` 调低后 TTS 根本念不完：容量预算变成更严的那个，提前精简。"""

    src = write_srt(tmp_path / "en.srt", (0, 2000, "This is a fairly long sentence."))
    chinese = "这个句子相当长，精简一下才念得完。"  # 18 字，刚好卡在 9 CPS × 2s
    use_fake(monkeypatch, FakeTranslator(lambda text: chinese))

    plain = run_translate(
        TranslateOptions(src=src, out=tmp_path / "plain", tgt_lang="zh", condense_mode="trim")
    )
    slow = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "slow",
            tgt_lang="zh",
            condense_mode="trim",
            # 原速念（+0%）+ 提语速余量 ≈ 7.4 字/秒 < 阅读上限 9 → 由配音容量把关
            dub_cps_budget=7.36,
        )
    )

    assert load_srt(plain.output_path)[0].cues[0].text == chinese
    shortened = load_srt(slow.output_path)[0].cues[0].text
    assert len(shortened) < len(chinese)
    summary = slow.report["summary"]["dub_budget"]
    assert summary["binds"] == "dubbing"
    assert summary["cues_narrowed"] == 1


def test_dub_budget_reports_over_capacity_cues(tmp_path, monkeypatch) -> None:
    """字幕能读完、但配音怎么都念不完的条目要如实报出来（转人工预警）。"""

    src = write_srt(tmp_path / "zh.srt", (0, 1000, "短。"))
    english = "This line is far too long for a one second slot, honestly."
    use_fake(monkeypatch, FakeTranslator(lambda text: english))

    result = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "out",
            src_lang="zh",
            tgt_lang="en",
            condense=False,
            dub_cps_budget=speech_capacity_cps("en", "+20%"),
        )
    )

    summary = result.report["summary"]["dub_budget"]
    assert summary["over_capacity"] == 1
    assert summary["over_capacity_cues"] == [1]
    assert any("超出配音槽位容量" in warning for warning in result.report["warnings"])


def test_no_dub_budget_keeps_legacy_report(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 2000, "Hello."))
    use_fake(monkeypatch, FakeTranslator(lambda text: "你好。"))

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out"))

    assert result.report["summary"]["dub_budget"] is None
    assert result.report["config"]["condense"]["dub_cps_budget"] is None


# ---------------------------------------------------------------- 渠道


def test_microsoft_translator_batches_and_parses() -> None:
    requests: list[list[str]] = []

    def transport(url, payload, headers, timeout):
        texts = json.loads(payload.decode("utf-8"))
        requests.append(texts)
        return [{"translations": [{"text": f"译:{text}"}]} for text in texts]

    translator = MicrosoftTranslator(transport=transport, batch_size=2)

    output = translator.translate(["a", "b", "c"], src="en", tgt="zh")

    assert output == ["译:a", "译:b", "译:c"]
    assert requests == [["a", "b"], ["c"]]


def test_microsoft_translator_splits_failed_batch() -> None:
    calls: list[list[str]] = []

    def transport(url, payload, headers, timeout):
        texts = json.loads(payload.decode("utf-8"))
        calls.append(texts)
        if len(texts) > 1:
            raise urllib.error.URLError("boom")
        return [{"translations": [{"text": f"译:{texts[0]}"}]}]

    translator = MicrosoftTranslator(transport=transport, batch_size=2, retries=0)

    assert translator.translate(["a", "b"], src="en", tgt="zh") == ["译:a", "译:b"]
    assert calls == [["a", "b"], ["a"], ["b"]]


def test_microsoft_translator_rejects_bad_response() -> None:
    translator = MicrosoftTranslator(
        transport=lambda url, payload, headers, timeout: [], retries=0
    )

    with pytest.raises(TranslateError, match="失败"):
        translator.translate(["a"], src="en", tgt="zh")


def test_microsoft_language_mapping() -> None:
    seen: list[str] = []

    def transport(url, payload, headers, timeout):
        seen.append(url)
        return [{"translations": [{"text": "x"}]}]

    MicrosoftTranslator(transport=transport).translate(["a"], src="en", tgt="zh")

    assert "from=en" in seen[0]
    assert "to=zh-Hans" in seen[0]


def test_openai_requires_api_key() -> None:
    with pytest.raises(TranslateError, match="API key"):
        OpenAITranslator(LlmConfig(api_key=None))


def test_openai_translator_parses_json_array() -> None:
    def transport(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": '["一","二"]'}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k", model="m"), transport=transport)

    assert translator.translate(["a", "b"], src="en", tgt="zh") == ["一", "二"]


def test_openai_rewrite_returns_single_line() -> None:
    def transport(url, payload, headers, timeout):
        return {"choices": [{"message": {"content": "精简后的句子。"}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)

    assert translator.rewrite("一段很长的句子。", 5, tgt="中文") == "精简后的句子。"


def test_openai_usage_is_accumulated() -> None:
    def transport(url, payload, headers, timeout):
        return {
            "choices": [{"message": {"content": '["一"]'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)

    translator.translate(["a"], src="en", tgt="zh")
    translator.rewrite("一段很长的句子。", 5, tgt="中文")

    assert translator.usage == {
        "calls": 2,
        "prompt_tokens": 20,
        "completion_tokens": 6,
        "total_tokens": 26,
    }
    assert translator.info["model"] == "gpt-4o-mini"


def test_llm_config_detects_deepseek(monkeypatch) -> None:
    for name in ("SUBMATRIX_LLM_BASE_URL", "SUBMATRIX_LLM_API_KEY", "SUBMATRIX_LLM_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    config = LlmConfig.from_env()

    assert config.base_url == "https://api.deepseek.com/v1"
    assert config.model == "deepseek-chat"
    assert config.api_key == "sk-test"
    assert config.configured is True


def test_llm_config_explicit_values_win(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    config = LlmConfig.from_env(base_url="https://example.com/v1", model="custom")

    assert config.base_url == "https://example.com/v1"
    assert config.model == "custom"


def test_report_contains_llm_usage(tmp_path, monkeypatch) -> None:
    def transport(url, payload, headers, timeout):
        return {
            "choices": [{"message": {"content": '["译文"]'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)
    monkeypatch.setattr(translate_module, "build_translator", lambda options: translator)
    src = write_srt(tmp_path / "en.srt", (0, 2000, "Hello."))

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out", provider="openai"))

    llm = result.report["config"]["llm"]
    assert llm["model"] == "gpt-4o-mini"
    assert llm["usage"]["calls"] == 1
    assert llm["usage"]["total_tokens"] == 7


def test_llm_receives_glossary_legend_and_budget_hint(tmp_path, monkeypatch) -> None:
    """LLM 渠道要比免费 MT 多拿到两样东西：术语对照（占位符代表什么）和字数预算。"""

    seen: list[dict] = []
    systems: list[str] = []

    def transport(url, payload, headers, timeout):
        body = json.loads(payload)
        systems.append(body["messages"][0]["content"])
        seen.append(json.loads(body["messages"][1]["content"]))
        count = len(seen[-1]["lines"])
        return {"choices": [{"message": {"content": json.dumps(["我用 {{G0}} 做渲染。"] * count)}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)
    monkeypatch.setattr(translate_module, "build_translator", lambda options: translator)
    glossary = Glossary(terms=(Term(source="Blender", keep=True),))
    src = write_srt(
        tmp_path / "en.srt", (0, 4000, "I used Blender for rendering."),
        (4000, 8000, "It works."),
    )

    run_translate(
        TranslateOptions(
            src=src, out=tmp_path / "out", provider="openai", glossary=glossary,
            condense=False,
        )
    )

    payload = seen[0]
    assert payload["glossary"] == [{"token": "{{G0}}", "source": "Blender", "output": "Blender"}]
    assert payload["max_chars"] == [36, 36]  # 4s × 9 CPS
    assert "max_chars" in systems[0] and "glossary" in systems[0]


def test_llm_retries_when_line_count_mismatches() -> None:
    calls = {"n": 0}

    def transport(url, payload, headers, timeout):
        calls["n"] += 1
        # 第一次把两行并成一行（模型常见失误），第二次按提醒返回两条
        content = '["合并的一行"]' if calls["n"] == 1 else '["一","二"]'
        return {"choices": [{"message": {"content": content}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)

    assert translator.translate(["a", "b"], src="en", tgt="zh") == ["一", "二"]
    assert calls["n"] == 2


def test_llm_splits_batch_when_count_keeps_mismatching() -> None:
    calls = {"n": 0}

    def transport(url, payload, headers, timeout):
        calls["n"] += 1
        body = json.loads(payload)
        requested = len(json.loads(body["messages"][1]["content"])["lines"])
        # 永远少返回一条：两行 → 一行；单行 → 一行
        return {"choices": [{"message": {"content": json.dumps(["一"] * max(1, requested - 1))}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)
    out = translator.translate(["a", "b", "c"], src="en", tgt="zh")

    assert out == ["一", "一", "一"]
    # 3 行整批失败 → 拆 1+2 → 2 行再拆成 1+1
    assert calls["n"] >= 5


def test_llm_review_fixes_lines_and_reports_count(tmp_path, monkeypatch) -> None:
    systems: list[str] = []

    def transport(url, payload, headers, timeout):
        body = json.loads(payload)
        systems.append(body["messages"][0]["content"])
        items = json.loads(body["messages"][1]["content"])
        if "reviewer" in body["messages"][0]["content"]:
            lines = [item["translation"].replace("错译", "正确") for item in items["lines"]]
        else:
            lines = ["错译"] * len(items["lines"])
        return {"choices": [{"message": {"content": json.dumps(lines, ensure_ascii=False)}}]}

    translator = OpenAITranslator(LlmConfig(api_key="k"), transport=transport)
    monkeypatch.setattr(translate_module, "build_translator", lambda options: translator)
    src = write_srt(tmp_path / "en.srt", (0, 4000, "Hello."), (4000, 8000, "Bye."))

    result = run_translate(
        TranslateOptions(
            src=src, out=tmp_path / "out", provider="openai",
            llm_review=True, condense=False,
        )
    )

    texts = [cue.text for cue in load_srt(result.output_path)[0].cues]
    assert texts == ["正确", "正确"]
    assert result.report["summary"]["review"]["cues_changed"] == 2
    assert result.report["summary"]["review"]["passes"] == 2  # 第二轮无改动 → 收敛退出
    changes = result.report["summary"]["review"]["changes"]
    assert [(item["cue"], item["before"], item["after"]) for item in changes] == [
        (1, "错译", "正确"),
        (2, "错译", "正确"),
    ]
    assert result.report["config"]["llm"]["review"] is True
    assert any("reviewer" in system for system in systems)


def test_llm_review_requires_llm_provider(tmp_path, monkeypatch) -> None:
    use_fake(monkeypatch, FakeTranslator(lambda text: "译文"))
    src = write_srt(tmp_path / "en.srt", (0, 2000, "Hello."))

    with pytest.raises(TranslateError, match="--llm-review"):
        run_translate(
            TranslateOptions(src=src, out=tmp_path / "out", llm_review=True, condense=False)
        )


def test_trim_is_flagged_for_human_review(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 2000, "This is useful."))
    use_fake(
        monkeypatch,
        FakeTranslator(lambda text: "这个功能非常有用，而且速度也很快，还能离线运行。"),
    )

    result = run_translate(
        TranslateOptions(
            src=src, out=tmp_path / "out", condense_mode="auto", target_cps=9.0
        )
    )

    needs_review = result.report["summary"]["condense"]["needs_review"]
    assert needs_review and needs_review[0]["action"] == "trim"
    assert needs_review[0]["cue"] == 1
    assert any("needs_review" in warning for warning in result.report["warnings"])


def test_cli_condense_is_on_by_default(tmp_path) -> None:
    """用户口径：auto 就是默认模式；要"一个字都不许少"才显式 --no-condense。"""

    from submatrix.cli import build_parser

    parser = build_parser()
    base = ["translate", "--src", "a.srt", "--out", "out"]

    args = parser.parse_args(base)
    assert args.condense is True
    assert args.condense_mode == "auto"
    assert args.llm_review is False

    args = parser.parse_args([*base, "--no-condense", "--llm-review", "--condense-mode", "llm"])
    assert args.condense is False
    assert args.llm_review is True
    assert args.condense_mode == "llm"


def test_command_translator_roundtrip(tmp_path) -> None:
    script = tmp_path / "mt.py"
    script.write_text(
        "import json, sys\n"
        "src, out = sys.argv[1], sys.argv[2]\n"
        "data = json.load(open(src, encoding='utf-8'))\n"
        "json.dump(['CMD:' + item for item in data], open(out, 'w', encoding='utf-8'),"
        " ensure_ascii=False)\n",
        encoding="utf-8",
    )
    template = f'"{sys.executable}" "{script}" {{input}} {{output}}'

    translator = CommandTranslator(template)

    assert translator.translate(["a", "b"], src="en", tgt="zh") == ["CMD:a", "CMD:b"]


def test_command_template_requires_placeholders() -> None:
    with pytest.raises(TranslateError, match="模板"):
        CommandTranslator("{input}")


def test_build_translator_rejects_unknown_provider() -> None:
    with pytest.raises(TranslateError, match="渠道"):
        build_translator(TranslateOptions(src="a.srt", out="out", provider="magic"))


# ---------------------------------------------------------------- 报告


def test_echo_provider_runs_offline(tmp_path) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 2000, "Hello."))

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", provider="echo")
    )

    assert load_srt(result.output_path)[0].cues[0].text == "[zh] Hello."
    assert result.report["config"]["provider"] == "echo"
    assert result.exit_code == 0


def test_report_contains_quality_metrics(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "Hello there."), (1000, 3000, "Bye."))
    use_fake(monkeypatch, FakeTranslator(lambda text: "这是一句很长很长的中文译文内容。"))

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out"))

    summary = result.report["summary"]
    assert summary["cps"]["limit"] == 9.0
    assert summary["cps"]["over_limit"] >= 1
    assert summary["cps"]["max"] > 9.0
    assert result.report["status"] == "warn"
    assert (tmp_path / "out" / "translate.json").is_file()


def test_clean_source_collapses_chinese_stutter_and_normalizes_punct() -> None:
    """真实采访素材实测：口吃重复和半角标点会明显拉低 zh→en 译文质量。"""

    assert clean_source("其实他对我们来说对我们对我们对我们来说都非常好", cjk=True) == (
        "其实他对我们来说都非常好"
    )
    assert clean_source("不会, 因为他一般", cjk=True) == "不会，因为他一般"
    # 两字重复是正常表达（看看/试试/非常非常），不能当口吃删掉
    assert clean_source("非常非常好，看看，试试", cjk=True) == "非常非常好，看看，试试"


def test_english_target_gets_terminal_punctuation() -> None:
    """ASR 源文没句号时，英文译文每条都要有句末标点（源文逗号收尾的除外）。"""

    from submatrix.translate import _ensure_en_terminal

    assert _ensure_en_terminal("He wants to train the next Liu Xiang", continues=False) == (
        "He wants to train the next Liu Xiang."
    )
    assert _ensure_en_terminal("No, because he generally,", continues=True) == (
        "No, because he generally,"
    )
    assert _ensure_en_terminal("Already fine!", continues=False) == "Already fine!"


def test_clean_source_removes_spaces_between_cjk() -> None:
    """ASR 合并中文片段可能留下空格（"向 他靠近"），送进机翻前必须去掉。"""

    assert clean_source("他靠近 我 们", cjk=True) == "他靠近我们"


# ---------------------------------------------------------------- LLM 兜底


def test_translate_units_skips_noise_and_uses_fallback() -> None:
    """疑似音乐/噪声（咪咪咪…）不送 LLM，直接回退免费 MT，且保持顺序。"""

    primary = FakeTranslator(lambda text: f"LLM:{text}")
    fallback = FakeTranslator(lambda text: f"FB:{text}")
    notes: list[str] = []

    results, lost = translate_module._translate_units(
        primary,
        ["盐也会引发战争吗", "咪咪咪咪咪咪咪咪咪咪", "你知道是什么吸引它们吗"],
        src="zh",
        tgt="en",
        glossary=None,
        batch_size=8,
        fallback=fallback,
        notes=notes,
    )

    assert lost == []
    assert results == ["LLM:盐也会引发战争吗", "FB:咪咪咪咪咪咪咪咪咪咪", "LLM:你知道是什么吸引它们吗"]
    assert primary.calls == [["盐也会引发战争吗"], ["你知道是什么吸引它们吗"]]
    assert fallback.calls == [["咪咪咪咪咪咪咪咪咪咪"]]
    assert any("音乐/噪声" in note for note in notes)


def test_translate_units_llm_failure_falls_back_to_free_mt() -> None:
    """LLM 输出退化/非 JSON、拆到单条仍失败时，回退免费 MT 而不是整批报错。"""

    class FailingTranslator(FakeTranslator):
        def translate(self, texts, *, src, tgt, hints=None):
            self.calls.append(list(texts))
            raise TranslateError("LLM 返回不是 JSON 数组：'[\"Mi mi mi…'")

    primary = FailingTranslator(lambda text: text)
    fallback = FakeTranslator(lambda text: f"FB:{text}")
    notes: list[str] = []

    results, _lost = translate_module._translate_units(
        primary,
        ["盐也会引发战争吗"],
        src="zh",
        tgt="en",
        glossary=None,
        batch_size=8,
        fallback=fallback,
        notes=notes,
    )

    assert results == ["FB:盐也会引发战争吗"]
    assert any("回退免费 MT" in note for note in notes)


def test_translate_units_fallback_failure_keeps_source() -> None:
    """免费 MT 也失败时保留原文并告警，绝不因一条噪声让整条链路失败。"""

    class FailingTranslator(FakeTranslator):
        def translate(self, texts, *, src, tgt, hints=None):
            self.calls.append(list(texts))
            raise TranslateError("boom")

    notes: list[str] = []
    results, _lost = translate_module._translate_units(
        FailingTranslator(lambda text: text),
        ["咪咪咪咪咪咪咪咪咪咪"],
        src="zh",
        tgt="en",
        glossary=None,
        batch_size=8,
        fallback=FailingTranslator(lambda text: text),
        notes=notes,
    )

    assert results == ["咪咪咪咪咪咪咪咪咪咪"]
    assert any("保留原文" in note for note in notes)


def test_translate_units_without_fallback_still_raises() -> None:
    """没有兜底（command 等渠道）时保持原契约：失败要显式报错。"""

    class FailingTranslator(FakeTranslator):
        def translate(self, texts, *, src, tgt, hints=None):
            self.calls.append(list(texts))
            raise TranslateError("boom")

    with pytest.raises(TranslateError, match="boom"):
        translate_module._translate_units(
            FailingTranslator(lambda text: text),
            ["正常一句话"],
            src="zh",
            tgt="en",
            glossary=None,
            batch_size=8,
        )


# ---------------------------------------------------------------- 渠道回退（OPT-3）


class BrokenTranslator:
    """模拟"免费微软端点挂了"：每次调用都抛网络类错误。"""

    name = "microsoft"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, texts, *, src, tgt, hints=None):  # noqa: ARG002
        self.calls += 1
        raise TranslateError("SSL: UNEXPECTED_EOF_WHILE_READING")


def _llm_transport(*, record: list | None = None):
    """假 LLM 端点：把请求里的 lines 原样标号返回，并带 token 用量。"""

    def transport(url, payload, headers, timeout):  # noqa: ARG001
        body = json.loads(payload.decode("utf-8"))
        data = json.loads(body["messages"][1]["content"])
        count = len(data.get("lines") or [])
        if record is not None:
            record.append(count)
        return {
            "choices": [{"message": {"content": json.dumps([f"LLM{i}" for i in range(count)])}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return transport


def test_network_failure_falls_back_to_configured_llm(tmp_path, monkeypatch) -> None:
    """OPT-3：免费 MT 网络失败 → 自动切到已配置的 LLM，整条链路不再 failed。"""

    src = write_srt(tmp_path / "en.srt", (0, 1000, "Hello."), (1000, 2000, "World."))
    broken = BrokenTranslator()
    monkeypatch.setattr(translate_module, "build_translator", lambda options: broken)
    config = LlmConfig(base_url="http://llm", api_key="k", model="m")

    result = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "out",
            context="off",
            llm=config,
            transport=_llm_transport(),
        )
    )

    report = result.report
    assert result.report["status"] in {"ok", "warn"}
    texts = [cue.text for cue in load_srt(result.output_path)[0].cues]
    assert texts == ["LLM0", "LLM1"]
    # 实际干活的渠道记成 openai（成本才按 token 算），主渠道另记
    assert report["config"]["provider"] == "openai"
    assert report["config"]["provider_primary"] == "microsoft"
    assert report["summary"]["provider_fallback"] == 1
    event = report["config"]["provider_fallback"]["events"][0]
    assert event["from"] == "microsoft" and event["to"] == "openai"
    assert "UNEXPECTED_EOF" in str(event["error"])
    assert report["config"]["llm"]["usage"]["total_tokens"] == 15
    assert any("已自动回退到 openai" in warning for warning in report["warnings"])


def test_fallback_is_sticky_and_keeps_finished_chunks(tmp_path, monkeypatch) -> None:
    """切过渠道后不再重试坏渠道（否则每批白等 1+2+4s），失败前的成果保留。"""

    src = write_srt(
        tmp_path / "en.srt",
        (0, 1000, "One."),
        (1000, 2000, "Two."),
        (2000, 3000, "Three."),
    )
    broken = BrokenTranslator()
    monkeypatch.setattr(translate_module, "build_translator", lambda options: broken)
    config = LlmConfig(base_url="http://llm", api_key="k", model="m")

    result = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "out",
            context="off",
            batch_size=1,  # 3 次调用：第 1 次失败切渠道，后两次直接走 LLM
            llm=config,
            transport=_llm_transport(),
        )
    )

    assert broken.calls == 1
    assert [cue.text for cue in load_srt(result.output_path)[0].cues] == [
        "LLM0",
        "LLM0",
        "LLM0",
    ]
    assert len(result.report["config"]["provider_fallback"]["events"]) == 1


def test_fallback_can_be_disabled(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "Hello."))
    monkeypatch.setattr(translate_module, "build_translator", lambda options: BrokenTranslator())
    config = LlmConfig(base_url="http://llm", api_key="k", model="m")

    with pytest.raises(TranslateError, match="UNEXPECTED_EOF"):
        run_translate(
            TranslateOptions(
                src=src,
                out=tmp_path / "out",
                context="off",
                llm=config,
                transport=_llm_transport(),
                fallback_provider="off",
            )
        )


def test_fallback_chain_empty_without_llm_key(tmp_path, monkeypatch) -> None:
    """没有可用备用渠道时保持原行为：报错（不能假装成功）。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    src = write_srt(tmp_path / "en.srt", (0, 1000, "Hello."))
    monkeypatch.setattr(translate_module, "build_translator", lambda options: BrokenTranslator())

    with pytest.raises(TranslateError, match="UNEXPECTED_EOF"):
        run_translate(TranslateOptions(src=src, out=tmp_path / "out", context="off"))


def test_fallback_to_all_failing_channels_reports_last_error(tmp_path, monkeypatch) -> None:
    src = write_srt(tmp_path / "en.srt", (0, 1000, "Hello."))
    monkeypatch.setattr(translate_module, "build_translator", lambda options: BrokenTranslator())
    config = LlmConfig(base_url="http://llm", api_key="k", model="m")

    def dying_transport(url, payload, headers, timeout):  # noqa: ARG001
        raise urllib.error.URLError("network down")

    with pytest.raises(TranslateError, match="LLM 请求失败"):
        run_translate(
            TranslateOptions(
                src=src,
                out=tmp_path / "out",
                context="off",
                llm=config,
                transport=dying_transport,
            )
        )


# ---------------------------------------------------------------- 翻译并发（OPT-5）


class SlowTranslator:
    """记录并发度的假渠道：每次调用睡一小会儿，返回带序号的译文。"""

    name = "fake"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[list[str]] = []
        self.lock = __import__("threading").Lock()

    def translate(self, texts, *, src, tgt, hints=None):  # noqa: ARG002
        with self.lock:
            self.calls.append(list(texts))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            __import__("time").sleep(self.delay)
            return [f"译{text}" for text in texts]
        finally:
            with self.lock:
                self.active -= 1


def _paragraph_srt(tmp_path, count: int = 4):
    cues = []
    for index in range(count):
        start = index * 4000
        cues.append((start, start + 1000, f"Line {index}."))
    return write_srt(tmp_path / "en.srt", *cues)


def test_translate_runs_paragraphs_concurrently(tmp_path, monkeypatch) -> None:
    src = _paragraph_srt(tmp_path)
    translator = SlowTranslator()
    use_fake(monkeypatch, translator)

    result = run_translate(
        TranslateOptions(src=src, out=tmp_path / "out", concurrency=4)
    )

    assert translator.max_active > 1  # 真的并发
    texts = [cue.text for cue in load_srt(result.output_path)[0].cues]
    # 结果仍按原顺序拼回（并发不影响对齐）
    assert texts == [f"译Line {index}." for index in range(4)]


def test_translate_concurrency_one_is_sequential(tmp_path, monkeypatch) -> None:
    src = _paragraph_srt(tmp_path)
    translator = SlowTranslator()
    use_fake(monkeypatch, translator)

    result = run_translate(TranslateOptions(src=src, out=tmp_path / "out", concurrency=1))

    assert translator.max_active == 1
    assert [cue.text for cue in load_srt(result.output_path)[0].cues] == [
        f"译Line {index}." for index in range(4)
    ]

# ---------------------------------------------------------------- OPT-10 HTTP 连接复用


@pytest.fixture()
def clean_http_connection():
    translate_module._drop_connection()
    yield
    translate_module._drop_connection()


class FakeHttpResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeHttpConnection:
    """假的 HTTPSConnection：记录创建/请求，可按需让第一次请求失败。"""

    instances: list["FakeHttpConnection"] = []

    def __init__(self, host, port=None, *, timeout=None, context=None) -> None:  # noqa: ANN001, ARG002
        self.host = host
        self.port = port
        self.requests: list[tuple[str, str, bytes, dict]] = []
        self.closed = False
        self.fail_requests = 0
        FakeHttpConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None) -> None:  # noqa: ANN001
        if self.fail_requests > 0:
            self.fail_requests -= 1
            raise translate_module.http.client.RemoteDisconnected("server closed")
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self) -> FakeHttpResponse:
        return FakeHttpResponse('["你好"]'.encode())

    def close(self) -> None:
        self.closed = True


def _fake_https(monkeypatch) -> None:
    FakeHttpConnection.instances = []
    monkeypatch.setattr(
        translate_module.http.client, "HTTPSConnection", FakeHttpConnection
    )


def test_http_transport_reuses_one_connection(monkeypatch, clean_http_connection) -> None:
    """OPT-10：同一线程连续请求复用一条 TLS 连接（省掉每次 ~0.77s 握手）。"""

    _fake_https(monkeypatch)
    url = translate_module.MICROSOFT_ENDPOINT.format(src="en", tgt="zh-Hans")
    headers = {"Content-Type": "application/json"}

    first = translate_module._default_transport(url, b'["a"]', headers, 30)
    second = translate_module._default_transport(url, b'["b"]', headers, 30)

    assert first == ["\u4f60\u597d"] and second == ["\u4f60\u597d"]
    assert len(FakeHttpConnection.instances) == 1  # 只建了一条连接
    connection = FakeHttpConnection.instances[0]
    assert connection.host == "edge.microsoft.com"
    assert len(connection.requests) == 2
    method, path, body, sent_headers = connection.requests[0]
    assert method == "POST"
    assert path.startswith("/translate/translatetext?")
    assert "from=en" in path and "to=zh-Hans" in path
    assert body == b'["a"]'
    assert sent_headers["Content-Type"] == "application/json"


def test_http_transport_reconnects_after_server_close(monkeypatch, clean_http_connection) -> None:
    """服务端关掉空闲连接时要自动重连一次，而不是让整批翻译失败。"""

    _fake_https(monkeypatch)
    url = translate_module.MICROSOFT_ENDPOINT.format(src="en", tgt="zh-Hans")

    FakeHttpConnection.instances = []
    # 第一条连接第一次请求就报 RemoteDisconnected
    original_init = FakeHttpConnection.__init__

    def init(self, host, port=None, *, timeout=None, context=None):  # noqa: ANN001
        original_init(self, host, port, timeout=timeout, context=context)
        if len(FakeHttpConnection.instances) == 1:
            self.fail_requests = 1

    monkeypatch.setattr(FakeHttpConnection, "__init__", init)

    result = translate_module._default_transport(url, b'["a"]', {}, 30)

    assert result == ["\u4f60\u597d"]
    assert len(FakeHttpConnection.instances) == 2  # 重建了一条
    assert FakeHttpConnection.instances[0].closed is True


def test_http_transport_wraps_network_error(monkeypatch, clean_http_connection) -> None:
    """两次都失败时抛 URLError，交给上层重试/渠道回退逻辑处理。"""

    class DeadConnection(FakeHttpConnection):
        def request(self, method, path, body=None, headers=None) -> None:  # noqa: ANN001
            raise OSError("boom")

    FakeHttpConnection.instances = []
    monkeypatch.setattr(translate_module.http.client, "HTTPSConnection", DeadConnection)
    url = translate_module.MICROSOFT_ENDPOINT.format(src="en", tgt="zh-Hans")

    with pytest.raises(urllib.error.URLError, match="boom"):
        translate_module._default_transport(url, b"[]", {}, 30)


def test_http_transport_is_thread_local(monkeypatch, clean_http_connection) -> None:
    """并发线程各用各的连接（不能两个线程同时读写同一个 socket）。"""

    import threading

    _fake_https(monkeypatch)
    url = translate_module.MICROSOFT_ENDPOINT.format(src="en", tgt="zh-Hans")

    threads = [
        threading.Thread(target=lambda: translate_module._default_transport(url, b"[]", {}, 30))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(FakeHttpConnection.instances) == 2


def test_translate_report_records_keepalive_flag(tmp_path, monkeypatch) -> None:
    """报告里记 http_keepalive：默认 transport=True、测试注入 transport=False。"""

    fake = FakeTranslator(lambda text: f"T:{text}")
    src = _paragraph_srt(tmp_path, 2)

    use_fake(monkeypatch, fake)
    default_result = run_translate(TranslateOptions(src=src, out=tmp_path / "a"))
    assert default_result.report["config"]["http_keepalive"] is True

    custom_result = run_translate(
        TranslateOptions(
            src=src,
            out=tmp_path / "b",
            transport=lambda url, payload, headers, timeout: ["T:x"],  # noqa: ARG005, E731
        )
    )
    assert custom_result.report["config"]["http_keepalive"] is False
