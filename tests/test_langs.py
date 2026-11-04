"""语言阈值单一真相源（P0）：数字只许有一份，方向和派生都要钉住。

背景：阈值以前在三个地方各写一份——``validate.LANG_PROFILES``、
``layout.LayoutOptions`` 的字段默认值、``translate`` 自己的 profile 查找，
而且方向还相反（validate 默认 zh→en、layout 默认 en→zh）。改一处忘一处，
就会出现"校验说合规、排版按另一套折行"这种自相矛盾。

这些用例的职责：谁想再"顺手抄一份字面量"，就会在这里变红。
"""

from __future__ import annotations

import pytest

from submatrix.langs import LANG_PROFILES, SUPPORTED_LANGS, profile
from submatrix.layout import LayoutOptions
from submatrix.validate import Limits


def test_profile_lookup_returns_the_table_entry() -> None:
    assert profile("zh") is LANG_PROFILES["zh"]
    assert profile("en") is LANG_PROFILES["en"]


def test_unknown_language_lists_supported_codes() -> None:
    with pytest.raises(ValueError) as excinfo:
        profile("ja")

    message = str(excinfo.value)
    assert "ja" in message
    assert all(code in message for code in SUPPORTED_LANGS)


def test_limits_defaults_are_the_zh_to_en_projection() -> None:
    """``Limits()`` 的默认值必须等于按表推导的 zh→en（不许另存字面量）。"""

    zh, en = LANG_PROFILES["zh"], LANG_PROFILES["en"]
    limits = Limits()

    assert (limits.cps_src, limits.width_src) == (zh.cps, zh.width)
    assert (limits.cps_tgt, limits.width_tgt) == (en.cps, en.width)
    assert limits == Limits.from_languages("zh", "en")


def test_layout_defaults_are_the_en_to_zh_projection() -> None:
    """排版层默认方向是 en→zh（历史默认）；数字同样只来自 ``LANG_PROFILES``。"""

    en, zh = LANG_PROFILES["en"], LANG_PROFILES["zh"]
    defaults = LayoutOptions()

    assert (defaults.src_width, defaults.src_cps) == (en.width, en.cps)
    assert (defaults.tgt_width, defaults.tgt_cps) == (zh.width, zh.cps)
    assert defaults == LayoutOptions.from_limits(Limits.from_languages("en", "zh"))


@pytest.mark.parametrize(("src_lang", "tgt_lang"), [("zh", "en"), ("en", "zh")])
def test_layout_from_limits_agrees_with_check_line(src_lang: str, tgt_lang: str) -> None:
    """matrix 走 ``from_limits``：同一对字幕的"合规线"和"排版线"必须完全一致。"""

    limits = Limits.from_languages(src_lang, tgt_lang)
    layout = LayoutOptions.from_limits(limits)

    assert layout.src_width == limits.width_src
    assert layout.tgt_width == limits.width_tgt
    assert (layout.src_cps, layout.tgt_cps) == (limits.cps_src, limits.cps_tgt)
    assert layout.max_lines == limits.max_lines


def test_every_profile_is_usable_by_both_layers() -> None:
    """表里新增一门语言时，两层都要能直接吃下去（不能各有各的白名单）。"""

    for code in SUPPORTED_LANGS:
        limits = Limits.from_languages(code, code)
        layout = LayoutOptions.from_limits(limits)
        assert layout.src_width == LANG_PROFILES[code].width
