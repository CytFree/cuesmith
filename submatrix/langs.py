"""语言 profile：阅读阈值（CPS / 行宽）与 CJK 判定的**唯一真相源**。

为什么单独一层：这些数字以前散在三个地方各写一份——
``validate.LANG_PROFILES``、``layout.LayoutOptions`` 的字段默认值、``translate`` 里
自己的一份 profile 查找；而且方向还相反（validate 默认 zh→en、layout 默认 en→zh）。
改一个阈值要记得改三处，漏一处就会出现"校验说合规、排版按另一套折行"的自相矛盾。

这里只放**不随实现变**的业务常量：语速上限、行宽、是否按字计长。
派生值仍由各模块自己算（``validate.Limits`` / ``layout.LayoutOptions``），
但它们现在都从这张表推导，不再各存一份字面量。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LANG_PROFILES", "SUPPORTED_LANGS", "LangProfile", "profile"]


@dataclass(frozen=True, slots=True)
class LangProfile:
    """一种语言的阅读阈值与计量口径。"""

    code: str
    label: str
    cjk: bool
    cps: float  # 阅读速度上限：CJK 按字/秒，拉丁按字符/秒（含空格）
    width: int  # 单行字符上限


# 阈值出处：英文 20 CPS / 42 字符每行是 Netflix、BBC 等字幕规范的常用上限；
# 中文 9 字每秒 ≈ 中文阅读速度的经验值。实测同一段素材平均 15.8 CPS。
LANG_PROFILES: dict[str, LangProfile] = {
    "zh": LangProfile(code="zh", label="中文", cjk=True, cps=9.0, width=16),
    "en": LangProfile(code="en", label="英文", cjk=False, cps=20.0, width=42),
}

SUPPORTED_LANGS: tuple[str, ...] = tuple(sorted(LANG_PROFILES))


def profile(code: str) -> LangProfile:
    """按语言码取 profile；不支持的语言报错时列出当前支持哪些。"""

    try:
        return LANG_PROFILES[code]
    except KeyError:
        raise ValueError(
            f"不支持的语言：{code!r}（当前支持 {'、'.join(SUPPORTED_LANGS)}）"
        ) from None
