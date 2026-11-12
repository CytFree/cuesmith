"""结构校验 + 阅读性校验（FR-2 行为 2、3）。

对应验收：
- AC-5 构造的违规样本（超 CPS、超行宽、空译、条数不符）100% 被报出，零漏报
- 违规项进 report.json：cue 号、指标、实测值、阈值；**不自动改**

严重度约定：
- error（硬约束，不可交付）：条数不符、时间轴不一致、空译、漏译、零时长
- warning（可交付但需人工判断）：CPS 超限、行宽超限、行数超限
"""

from __future__ import annotations

import pytest

from cuesmith.srt import Cue, Subtitle, Timing
from cuesmith.validate import (
    LANG_PROFILES,
    Finding,
    Limits,
    ValidationResult,
    validate,
)

LIMITS = Limits()


def sub(*pairs: tuple[int, int, str]) -> Subtitle:
    return Subtitle(cues=tuple(Cue(timing=Timing(start, end), text=text) for start, end, text in pairs))


def codes(result: ValidationResult) -> list[str]:
    return [finding.code for finding in result.findings]


# ---------------------------------------------------------------- 干净样本


def test_clean_pair_has_no_findings() -> None:
    src = sub((0, 2000, "你好世界"))
    tgt = sub((0, 2000, "Hello world"))

    result = validate(src, tgt, limits=LIMITS)

    assert result.findings == ()
    assert result.status == "ok"
    assert result.paired is True


# ---------------------------------------------------------------- 硬约束


def test_detects_cue_count_mismatch() -> None:
    src = sub((0, 1000, "你好"), (2000, 3000, "再见"))
    tgt = sub((0, 1000, "Hello"))

    result = validate(src, tgt, limits=LIMITS)

    assert codes(result) == ["cue_count_mismatch"]
    assert result.status == "fail"
    assert result.paired is False


def test_count_mismatch_skips_per_cue_checks() -> None:
    """条数不对时按索引比对只会产生一堆噪声，先让人把条数修对。"""

    src = sub((0, 1000, "你好"), (2000, 3000, "再见"), (4000, 5000, "第三句"))
    tgt = sub((0, 1000, "Hello"))

    assert len(validate(src, tgt, limits=LIMITS).findings) == 1


def test_detects_timeline_mismatch() -> None:
    src = sub((0, 1000, "你好"), (2000, 3000, "再见"))
    tgt = sub((0, 1000, "Hello"), (2500, 3000, "Bye"))

    result = validate(src, tgt, limits=LIMITS)

    assert codes(result) == ["timeline_mismatch"]
    assert result.findings[0].cue == 2
    assert result.findings[0].severity == "error"


def test_detects_empty_translation() -> None:
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, "   "))

    result = validate(src, tgt, limits=LIMITS)

    assert codes(result) == ["empty_translation"]
    assert result.findings[0].cue == 1


def test_detects_untranslated_identical_text() -> None:
    src = sub((0, 2000, "你好世界"))
    tgt = sub((0, 2000, "你好世界"))

    assert codes(validate(src, tgt, limits=LIMITS)) == ["untranslated"]


def test_detects_untranslated_when_target_has_no_latin() -> None:
    src = sub((0, 2000, "你好世界"))
    tgt = sub((0, 2000, "……"))

    assert codes(validate(src, tgt, limits=LIMITS)) == ["untranslated"]


def test_identical_short_latin_text_is_not_untranslated() -> None:
    """原文 "OK"、译文 "OK" 不是漏译，别误报。"""

    src = sub((0, 1000, "OK"))
    tgt = sub((0, 1000, "OK"))

    assert validate(src, tgt, limits=LIMITS).findings == ()


def test_detects_zero_duration() -> None:
    src = sub((1000, 1000, "你好"))
    tgt = sub((1000, 1000, "Hello"))

    result = validate(src, tgt, limits=LIMITS)

    assert "nonpositive_duration" in codes(result)
    assert result.findings[0].severity == "error"


# ---------------------------------------------------------------- 阅读性：CPS


def test_detects_cps_overflow_on_target() -> None:
    tgt_text = "abcdefghij klmnopqrstuvwxyz"  # 27 字符 / 1s = 27 CPS
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, tgt_text))

    result = validate(src, tgt, limits=LIMITS)

    cps_finding = next(f for f in result.findings if f.code == "cps")
    assert cps_finding.lang == "en"
    assert cps_finding.cue == 1
    assert cps_finding.metric == "cps"
    assert cps_finding.value == pytest.approx(27.0)
    assert cps_finding.threshold == pytest.approx(20.0)
    assert result.status == "warn"


def test_cps_exactly_at_limit_is_ok() -> None:
    text = "abcdefghij klmnopqrs"  # 正好 20 字符 / 1s
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, text))

    assert [f for f in validate(src, tgt, limits=LIMITS).findings if f.code == "cps"] == []


def test_detects_cps_overflow_on_source() -> None:
    src = sub((0, 1000, "一二三四五六七八九十"))  # 10 字 / 1s > 9
    tgt = sub((0, 1000, "Hi"))

    cps_finding = next(f for f in validate(src, tgt, limits=LIMITS).findings if f.code == "cps")
    assert cps_finding.lang == "zh"
    assert cps_finding.threshold == pytest.approx(9.0)


def test_cps_excludes_markup() -> None:
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, "<i>abcdefghij</i>"))  # 可见 10 字符，标记不算

    assert [f for f in validate(src, tgt, limits=LIMITS).findings if f.code == "cps"] == []


# ---------------------------------------------------------------- 阅读性：行宽 / 行数


def test_detects_line_width_overflow() -> None:
    tgt = sub((0, 2000, "a" * 43))

    result = validate(sub((0, 2000, "你好")), tgt, limits=LIMITS)

    width_finding = next(f for f in result.findings if f.code == "line_width")
    assert width_finding.cue == 1
    assert width_finding.value == 43
    assert width_finding.threshold == 42
    assert width_finding.lang == "en"


def test_line_width_exactly_at_limit_is_ok() -> None:
    tgt = sub((0, 2000, "a" * 42))

    assert [f for f in validate(sub((0, 2000, "你好")), tgt, limits=LIMITS).findings
            if f.code == "line_width"] == []


def test_detects_too_many_lines() -> None:
    tgt = sub((0, 3000, "one\ntwo\nthree"))

    result = validate(sub((0, 3000, "你好")), tgt, limits=LIMITS)

    line_finding = next(f for f in result.findings if f.code == "line_count")
    assert line_finding.value == 3
    assert line_finding.threshold == 2


def test_two_lines_are_ok() -> None:
    tgt = sub((0, 3000, "one\ntwo"))

    assert [f for f in validate(sub((0, 3000, "你好")), tgt, limits=LIMITS).findings
            if f.code == "line_count"] == []


def test_line_width_measured_on_longest_line() -> None:
    tgt = sub((0, 3000, "short\n" + "b" * 50))

    width_finding = next(f for f in validate(sub((0, 3000, "你好")), tgt, limits=LIMITS).findings
                         if f.code == "line_width")
    assert width_finding.value == 50


# ---------------------------------------------------------------- 可配置 / 结果对象


def test_thresholds_are_configurable() -> None:
    src = sub((0, 1000, "你好"))
    tgt = sub((0, 1000, "abcdefghij"))  # 10 字符 / 1s = 10 CPS

    assert validate(src, tgt, limits=Limits(cps_tgt=20.0)).findings == ()
    assert "cps" in codes(validate(src, tgt, limits=Limits(cps_tgt=5.0)))


def test_limits_from_languages_uses_profile_defaults() -> None:
    limits = Limits.from_languages("zh", "en")

    assert limits == Limits(cps_src=9.0, cps_tgt=20.0, width_src=16, width_tgt=42)


def test_limits_from_languages_accepts_overrides() -> None:
    limits = Limits.from_languages("zh", "en", cps_tgt=18.0, width_src=20)

    assert limits.cps_tgt == 18.0
    assert limits.width_src == 20
    assert limits.cps_src == 9.0


def test_unknown_language_is_rejected() -> None:
    with pytest.raises(ValueError, match="不支持的语言"):
        Limits.from_languages("ja", "en")


def test_language_profiles_cover_chinese_and_english() -> None:
    assert LANG_PROFILES["zh"].cjk is True
    assert LANG_PROFILES["en"].cjk is False
    assert LANG_PROFILES["zh"].cps == 9.0
    assert LANG_PROFILES["en"].cps == 20.0


def test_result_exposes_counts_and_status() -> None:
    src = sub((0, 1000, "你好"), (2000, 3000, "再见"))
    tgt = sub((0, 1000, "a" * 50), (2000, 3000, ""))

    result = validate(src, tgt, limits=LIMITS)

    assert result.errors >= 1
    assert result.warnings >= 1
    assert result.status == "fail"


def test_finding_carries_report_fields() -> None:
    finding = Finding(
        severity="warning",
        code="cps",
        message="CPS 超限",
        cue=3,
        lang="en",
        metric="cps",
        value=23.4,
        threshold=20.0,
    )

    assert finding.to_dict() == {
        "severity": "warning",
        "code": "cps",
        "cue": 3,
        "lang": "en",
        "metric": "cps",
        "value": 23.4,
        "threshold": 20.0,
        "message": "CPS 超限",
    }
