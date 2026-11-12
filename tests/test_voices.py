"""音色目录 / 试听（M16）纯函数测试：不联网、不装 edge-tts。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cuesmith import voices
from cuesmith.voices import DEFAULT_LABEL, Voice, VoicesError


def _completed(stdout: str, code: int = 0):
    class Result:
        returncode = code

    result = Result()
    result.stdout = stdout  # type: ignore[attr-defined]
    return result


def _payload() -> str:
    return json.dumps(
        [
            {"ShortName": "zh-CN-XiaoxiaoNeural", "Gender": "Female", "Locale": "zh-CN"},
            {"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"},
            {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female", "Locale": "fr-FR"},
        ]
    )


def test_voice_id_parses_labels_and_raw_ids() -> None:
    assert voices.voice_id(DEFAULT_LABEL) == ""
    assert voices.voice_id("") == ""
    assert voices.voice_id(None) == ""
    assert voices.voice_id(" zh-CN-XiaoxiaoNeural ") == "zh-CN-XiaoxiaoNeural"
    assert voices.voice_id("zh-CN-XiaoxiaoNeural · 晓晓·女声") == "zh-CN-XiaoxiaoNeural"
    assert voices.voice_id("en-US-AriaNeural 女声") == "en-US-AriaNeural"


def test_voice_choices_filter_language_and_default_first() -> None:
    catalog = [
        Voice("en-US-AriaNeural", "Female", "en-US"),
        Voice("zh-CN-XiaoxiaoNeural", "Female", "zh-CN"),
        Voice("en-US-EmmaMultilingualNeural", "Female", "en-US"),
    ]

    zh = voices.voice_choices("zh", voices=catalog)
    en = voices.voice_choices("en", voices=catalog)

    assert zh[0] == en[0] == DEFAULT_LABEL
    assert "zh-CN-XiaoxiaoNeural · 晓晓·女声" in zh
    assert all("en-US-AriaNeural" not in item for item in zh)
    assert any("Aria" in item for item in en)
    # 多语言音色两种目标语言都能选
    assert any("EmmaMultilingualNeural" in item for item in zh)
    assert any("EmmaMultilingualNeural" in item for item in en)


def test_discover_voices_runs_edge_tts_and_caches(tmp_path: Path) -> None:
    cache = tmp_path / "voices.json"
    calls: list[list[str]] = []

    def runner(command):
        calls.append(list(command))
        return _completed(_payload())

    found = voices.discover_voices(
        "python.exe", cache_path=cache, runner=runner, now=lambda: 1000.0
    )

    assert [voice.id for voice in found] == ["zh-CN-XiaoxiaoNeural", "en-US-AriaNeural"]
    assert calls and calls[0][0] == "python.exe" and calls[0][1] == "-c"
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["voices"][0]["ShortName"] == "zh-CN-XiaoxiaoNeural"


def test_discover_reuses_fresh_cache(tmp_path: Path) -> None:
    cache = tmp_path / "voices.json"

    def fail(_command):
        raise AssertionError("新鲜缓存不应再调 edge-tts")

    voices.discover_voices("python.exe", cache_path=cache, runner=lambda cmd: _completed(_payload()))
    found = voices.discover_voices("python.exe", cache_path=cache, runner=fail, now=lambda: 2000.0)

    assert len(found) == 2


def test_discover_refresh_bypasses_cache(tmp_path: Path) -> None:
    cache = tmp_path / "voices.json"
    calls: list[int] = []

    def runner(_command):
        calls.append(1)
        return _completed(_payload())

    voices.discover_voices("python.exe", cache_path=cache, runner=runner)
    voices.discover_voices("python.exe", cache_path=cache, runner=runner, refresh=True)

    assert len(calls) == 2


def test_discover_failure_falls_back_to_static_catalog(tmp_path: Path) -> None:
    cache = tmp_path / "voices.json"
    found = voices.discover_voices(
        "python.exe", cache_path=cache, runner=lambda _cmd: _completed("", code=1)
    )

    assert any(voice.id == "zh-CN-XiaoxiaoNeural" for voice in found)
    assert any(voice.id == "en-US-AriaNeural" for voice in found)
    assert not cache.exists()


def test_discover_bad_json_falls_back(tmp_path: Path) -> None:
    found = voices.discover_voices(
        "python.exe", cache_path=tmp_path / "v.json", runner=lambda _cmd: _completed("not json")
    )

    assert found  # 静态表兜底


def test_discover_caches_only_zh_en(tmp_path: Path) -> None:
    cache = tmp_path / "voices.json"
    found = voices.discover_voices(
        "python.exe", cache_path=cache, runner=lambda _cmd: _completed(_payload())
    )

    assert {voice.id for voice in found} == {"zh-CN-XiaoxiaoNeural", "en-US-AriaNeural"}


def test_default_voice_matches_pipeline_defaults() -> None:
    assert voices.default_voice("zh") == "zh-CN-XiaoxiaoNeural"
    assert voices.default_voice("en") == "en-US-AriaNeural"
    assert voices.default_voice("xx") == "en-US-AriaNeural"


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def synthesize(self, text: str, out_path: Path) -> None:
        self.calls.append((text, out_path))
        out_path.write_bytes(b"mp3")


def test_preview_voice_synthesizes_and_caches(tmp_path: Path) -> None:
    engine = FakeEngine()

    first = voices.preview_voice(
        "zh-CN-XiaoxiaoNeural · 晓晓", "zh", out_dir=tmp_path / "preview", engine=engine
    )
    second = voices.preview_voice(
        "zh-CN-XiaoxiaoNeural · 晓晓", "zh", out_dir=tmp_path / "preview", engine=engine
    )

    assert first == second and first.is_file()
    assert len(engine.calls) == 1
    assert engine.calls[0][0] == voices.PREVIEW_TEXTS["zh"]
    assert first.name.startswith("zh-CN-XiaoxiaoNeural-")


def test_preview_voice_blank_uses_language_default(tmp_path: Path) -> None:
    engine = FakeEngine()

    path = voices.preview_voice("", "en", out_dir=tmp_path / "preview", engine=engine)

    assert path.name.startswith("en-US-AriaNeural-")
    assert engine.calls[0][0] == voices.PREVIEW_TEXTS["en"]


def test_preview_voice_wraps_engine_failure(tmp_path: Path) -> None:
    class Boom(FakeEngine):
        def synthesize(self, text: str, out_path: Path) -> None:
            raise voices.TtsError("没有网络")

    with pytest.raises(VoicesError, match="试听合成失败"):
        voices.preview_voice(
            "en-US-AriaNeural", "en", out_dir=tmp_path / "preview", engine=Boom()
        )
