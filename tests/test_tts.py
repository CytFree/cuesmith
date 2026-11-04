"""TTS 引擎层：解释器探测 + 语速换算（GUI venv 里通常没装 edge-tts，必须自动找到装了的 venv）。

语速换算（``speech_cps`` / ``speech_capacity_cps``）是 OPT-2 的基础：
翻译层要用它判断"这一句在配音槽位里念不念得完"，而不是拿字幕阅读速度冒充。
"""

from __future__ import annotations

import sys

import pytest

from submatrix.tts import (
    BASE_SPEECH_CPS,
    find_edge_tts_python,
    format_rate,
    parse_rate,
    speech_capacity_cps,
    speech_cps,
)


def test_explicit_python_wins_without_probing(monkeypatch) -> None:
    def probe(python: str) -> bool:  # noqa: ARG001
        raise AssertionError("显式指定不应探测")

    assert find_edge_tts_python("explicit", probe=probe) == "explicit"


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUBMATRIX_TTS_PYTHON", "env-python")

    assert find_edge_tts_python(candidates=["other"], probe=lambda python: True) == "env-python"


def test_probe_picks_first_importable(monkeypatch) -> None:
    monkeypatch.delenv("SUBMATRIX_TTS_PYTHON", raising=False)
    attempts: list[str] = []

    def probe(python: str) -> bool:
        attempts.append(python)
        return python == "good"

    assert find_edge_tts_python(candidates=["bad", "good"], probe=probe) == "good"
    # 当前解释器也在候选里；一旦命中 good 就停
    assert attempts[-1] == "good"
    assert sys.executable in attempts


def test_falls_back_to_current_interpreter(monkeypatch) -> None:
    monkeypatch.delenv("SUBMATRIX_TTS_PYTHON", raising=False)

    assert find_edge_tts_python(candidates=["bad"], probe=lambda python: False) == sys.executable


# ---------------------------------------------------------------- 语速换算


def test_parse_rate_handles_edge_syntax() -> None:
    assert parse_rate("+30%") == pytest.approx(0.30)
    assert parse_rate("-10%") == pytest.approx(-0.10)
    assert parse_rate("30%") == pytest.approx(0.30)
    assert parse_rate("+ 30%") == pytest.approx(0.0)  # 不认识的格式按 0，不抛
    assert parse_rate(None) == pytest.approx(0.0)
    assert parse_rate("") == pytest.approx(0.0)


def test_format_rate_round_trips() -> None:
    assert format_rate(0.45) == "+45%"
    assert format_rate(-0.1) == "-10%"
    assert format_rate(parse_rate("+65%")) == "+65%"


def test_speech_cps_matches_measured_fixture() -> None:
    """实测基准：中文 +30% ≈ 6 字/秒、英文 +20% ≈ 18.4 字符/秒。"""

    assert speech_cps("zh", "+30%") == pytest.approx(5.98, abs=0.05)
    assert speech_cps("en", "+20%") == pytest.approx(18.36, abs=0.05)
    assert speech_cps("zh", None) == pytest.approx(BASE_SPEECH_CPS["zh"])
    assert speech_cps("ja", "+30%") is None


def test_speech_capacity_adds_headroom() -> None:
    plain = speech_capacity_cps("zh", "+30%", headroom=1.0)
    widened = speech_capacity_cps("zh", "+30%")
    assert plain == pytest.approx(speech_cps("zh", "+30%"))
    assert widened == pytest.approx(plain * 1.6, rel=1e-6)
    # headroom < 1 没有意义（不允许比实测语速更紧）
    assert speech_capacity_cps("zh", "+30%", headroom=0.5) == pytest.approx(plain)
    assert speech_capacity_cps("ja", "+30%") is None
