"""术语表（第 ⑥ 层）：禁译保护 + 译名统一。

收费字幕软件都有的两条能力：
- 禁译表：``Blender`` / 人名 / 缩写不能翻；
- 术语表：同一个词全片只能有一种译法。

实现方式是"翻译前占位、翻译后换回"，因为直接对译文做字符串替换只能命中"恰好翻成 X"，
机翻换个说法就漏。占位符形态 ``{{G0}}`` 是在微软免费端点上实测能原样保住的写法。
"""

from __future__ import annotations

import json

import pytest

from submatrix.glossary import Glossary, GlossaryError, Term, glossary_fingerprint, parse_terms


def write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------- 读取格式


def test_json_terms_list(tmp_path) -> None:
    path = write(
        tmp_path,
        "g.json",
        json.dumps(
            {
                "terms": [
                    {"source": "Blender", "keep": True},
                    {"source": "large language model", "target": "大语言模型"},
                    {"source": "AI", "target": "AI"},
                ]
            },
            ensure_ascii=False,
        ),
    )

    glossary = Glossary.from_file(path)

    assert glossary.sources == ("Blender", "large language model", "AI")
    assert [term.replacement for term in glossary] == ["Blender", "大语言模型", "AI"]
    assert glossary.path == path


def test_json_mapping_form(tmp_path) -> None:
    path = write(
        tmp_path, "g.json", json.dumps({"podcast": "播客", "OpenAI": "OpenAI"}, ensure_ascii=False)
    )

    glossary = Glossary.from_file(path)

    assert [term.replacement for term in glossary] == ["播客", "OpenAI"]


def test_json_string_list_is_keep_list(tmp_path) -> None:
    path = write(tmp_path, "g.json", json.dumps(["Blender", "PyTorch"]))

    glossary = Glossary.from_file(path)

    assert [(term.source, term.keep) for term in glossary] == [
        ("Blender", True),
        ("PyTorch", True),
    ]


def test_tsv_with_header(tmp_path) -> None:
    path = write(
        tmp_path,
        "g.tsv",
        "source\ttarget\nBlender\tBlender\nlarge language model\t大语言模型\n",
    )

    glossary = Glossary.from_file(path)

    assert glossary.sources == ("Blender", "large language model")
    assert glossary.terms[1].target == "大语言模型"
    assert glossary.terms[0].keep is True


def test_txt_one_term_per_line(tmp_path) -> None:
    path = write(tmp_path, "g.txt", "# 品牌名\nBlender\n\nPyTorch\n")

    glossary = Glossary.from_file(path)

    assert glossary.sources == ("Blender", "PyTorch")
    assert all(term.keep for term in glossary)


def test_empty_glossary_is_rejected(tmp_path) -> None:
    path = write(tmp_path, "g.txt", "# 只有注释\n")

    with pytest.raises(GlossaryError, match="空"):
        Glossary.from_file(path)


def test_broken_json_is_rejected(tmp_path) -> None:
    path = write(tmp_path, "g.json", "{not json")

    with pytest.raises(GlossaryError, match="JSON"):
        Glossary.from_file(path)


def test_keep_with_different_target_is_rejected() -> None:
    with pytest.raises(GlossaryError, match="keep"):
        Term(source="AI", target="人工智能", keep=True)


def test_missing_file_is_rejected(tmp_path) -> None:
    with pytest.raises(GlossaryError, match="读不到"):
        Glossary.from_file(tmp_path / "nope.json")


def test_parse_terms_helper() -> None:
    glossary = parse_terms(["AI", "", " Blender "])

    assert glossary.sources == ("AI", "Blender")


# ---------------------------------------------------------------- 占位/还原


def make_glossary(*terms: Term) -> Glossary:
    return Glossary(terms=terms)


def test_protect_replaces_and_unprotect_restores() -> None:
    glossary = make_glossary(
        Term(source="Blender", keep=True),
        Term(source="rendering", target="渲染"),
    )

    protected = glossary.protect("I used Blender for rendering")

    assert "Blender" not in protected.text
    assert "rendering" not in protected.text
    assert "{{G0}}" in protected.text and "{{G1}}" in protected.text

    restored, lost = glossary.unprotect("我用 {{G0}} 做 {{G1}}", protected)

    assert restored == "我用 Blender 做 渲染"
    assert lost == []


def test_longest_term_wins() -> None:
    glossary = make_glossary(
        Term(source="language model", target="语言模型"),
        Term(source="large language model", target="大语言模型"),
    )

    protected = glossary.protect("a large language model")

    restored, _ = glossary.unprotect(protected.text, protected)

    assert restored == "a 大语言模型"


def test_latin_term_only_matches_whole_word() -> None:
    glossary = make_glossary(Term(source="AI", keep=True))

    protected = glossary.protect("The said AI-powered tool is not a said-AI")

    assert protected.text.count("{{G0}}") == 1


def test_latin_term_is_case_insensitive_by_default() -> None:
    glossary = make_glossary(Term(source="OpenAI", keep=True))

    protected = glossary.protect("openai released it")

    assert "{{G0}}" in protected.text


def test_case_sensitive_term() -> None:
    glossary = make_glossary(Term(source="AI", keep=True, case_sensitive=True))

    protected = glossary.protect("ai is lower, AI is upper")

    assert protected.text.count("{{G0}}") == 1


def test_cjk_term_matches_substring() -> None:
    glossary = make_glossary(Term(source="大模型", target="大模型"))

    protected = glossary.protect("这是一个大模型应用")

    assert "大模型" not in protected.text
    restored, _ = glossary.unprotect(protected.text, protected)
    assert restored == "这是一个大模型应用"


def test_kept_term_restores_canonical_spelling() -> None:
    glossary = make_glossary(Term(source="OpenAI", keep=True))

    protected = glossary.protect("openai released")
    restored, _ = glossary.unprotect(protected.text, protected)

    # 禁译只保证"不被翻掉"，还原时写回术语表里的规范写法
    assert restored == "OpenAI released"


def test_lost_placeholder_is_reported() -> None:
    glossary = make_glossary(Term(source="Blender", keep=True))

    protected = glossary.protect("I used Blender")
    restored, lost = glossary.unprotect("我用它渲染", protected)

    assert restored == "我用它渲染"
    assert [term.source for term in lost] == ["Blender"]


def test_placeholder_with_spaces_is_tolerated() -> None:
    glossary = make_glossary(Term(source="Blender", keep=True))

    protected = glossary.protect("I used Blender")
    restored, lost = glossary.unprotect("我用 {{ G0 }} 渲染", protected)

    assert restored == "我用 Blender 渲染"
    assert lost == []


def test_no_glossary_is_a_noop() -> None:
    glossary = Glossary()

    protected = glossary.protect("hello")

    assert protected.text == "hello"
    assert glossary.unprotect(protected.text, protected) == ("hello", [])


def test_reversed_swaps_targets_for_zh_to_en() -> None:
    glossary = make_glossary(
        Term(source="presentation", target="演示文稿"),
        Term(source="OpenAI", keep=True),
    )

    reversed_glossary = glossary.reversed()

    assert [(term.source, term.target, term.keep) for term in reversed_glossary] == [
        ("演示文稿", "presentation", False),
        ("OpenAI", None, True),
    ]


def test_reversed_glossary_protects_chinese_source() -> None:
    glossary = make_glossary(Term(source="presentation", target="演示文稿"))

    protected = glossary.reversed().protect("我做一个演示文稿。")

    assert "演示文稿" not in protected.text
    assert "{{G0}}" in protected.text


def test_reverse_keep_term_matches_both_sides() -> None:
    glossary = make_glossary(Term(source="AI", keep=True))

    protected = glossary.reversed().protect("我们用 AI 写软件。")

    assert "{{G0}}" in protected.text


def test_term_written_directly_by_llm_is_not_reported_as_lost() -> None:
    """LLM 渠道会看到"占位符=应输出"的对照，可能直接写术语而不用占位符：不算丢失。"""

    glossary = make_glossary(Term(source="刘翔", target="Liu Xiang"))
    protected = glossary.protect("刘翔是中国田径运动员")

    restored, lost = glossary.unprotect("Liu Xiang is a Chinese hurdler", protected)

    assert restored == "Liu Xiang is a Chinese hurdler"
    assert lost == []


def test_missing_term_still_reported_when_absent() -> None:
    glossary = make_glossary(Term(source="刘翔", target="Liu Xiang"))
    protected = glossary.protect("刘翔是中国田径运动员")

    _, lost = glossary.unprotect("He is a Chinese hurdler", protected)

    assert [term.source for term in lost] == ["刘翔"]


# ---------------------------------------------------------------- 内容指纹（缓存判据）


def test_fingerprint_ignores_term_order_and_file_path() -> None:
    """指纹按**内容**算：同一批术语换个顺序、换个文件路径都不算变。"""

    a = Glossary.from_terms(
        [Term(source="Blender", keep=True), Term(source="model", target="模型")]
    )
    b = Glossary.from_terms(
        [Term(source="model", target="模型"), Term(source="Blender", keep=True)]
    )

    assert glossary_fingerprint(a) == glossary_fingerprint(b)


def test_fingerprint_tracks_target_change_with_same_term_count() -> None:
    """条数不变、只改译法也必须换指纹——这正是旧判据（路径 + 条数）漏掉的场景。"""

    before = Glossary.from_terms([Term(source="model", target="模型")])
    after = Glossary.from_terms([Term(source="model", target="模型（AI）")])

    assert len(before) == len(after)
    assert glossary_fingerprint(before) != glossary_fingerprint(after)


def test_fingerprint_tracks_keep_and_case_sensitivity() -> None:
    plain = Glossary.from_terms([Term(source="AI")])
    keep = Glossary.from_terms([Term(source="AI", keep=True)])
    cases = Glossary.from_terms([Term(source="AI", case_sensitive=True)])

    assert glossary_fingerprint(plain) != glossary_fingerprint(keep)
    assert glossary_fingerprint(plain) != glossary_fingerprint(cases)


def test_fingerprint_is_none_without_terms() -> None:
    assert glossary_fingerprint(None) is None
    assert glossary_fingerprint(Glossary()) is None
