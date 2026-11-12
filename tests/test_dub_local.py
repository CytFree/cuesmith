"""本地 TTS / 兜底（OPT-11）：dub 的 provider=local 与 Edge 失败自动切本地。

真模型不在这里跑（真实冒烟见 `work/local-tts-eval/worker-smoke/`）；这里用假引擎
验证编排契约：缓存指纹、报告字段、兜底事件与告警、失败时的行为。
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest

from cuesmith import dub as dub_module
from cuesmith import localtts as localtts_module
from cuesmith.dub import DubError, DubOptions, run_dub
from cuesmith.tts import TtsError
from tests.support import requires_media_tools

pytestmark = requires_media_tools

SRT_THREE = (
    "1\n00:00:00,000 --> 00:00:02,000\nFirst line here\n"
    "\n"
    "2\n00:00:02,000 --> 00:00:04,000\nSecond line here\n"
    "\n"
    "3\n00:00:04,000 --> 00:00:06,000\nThird line here\n"
)


def write_srt(path: Path) -> Path:
    path.write_text(SRT_THREE, encoding="utf-8", newline="\n")
    return path


def write_audio(path: Path, duration_ms: int = 800) -> None:
    frames = int(24000 * duration_ms / 1000)
    samples = array.array(
        "h", (int(8000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(frames))
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(samples.tobytes())


class FakeEngine:
    """可编程假引擎：指定文本必定失败，或全部成功。"""

    def __init__(self, *, fail_texts: tuple[str, ...] = (), duration_ms: int = 800) -> None:
        self.fail_texts = set(fail_texts)
        self.duration_ms = duration_ms
        self.calls: list[str] = []

    def synthesize(self, text: str, out_path: Path) -> None:
        self.calls.append(text)
        if text in self.fail_texts:
            raise TtsError("模拟网络失败")
        write_audio(out_path, self.duration_ms)


@pytest.fixture()
def fake_local(monkeypatch, tmp_path: Path):
    """把本地 TTS 的模型解析与引擎都换成假的（不加载真模型）。"""

    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"fake")
    engines: list[object] = []

    class FakeLocalEngine:
        def __init__(self, options, voice="", *, language=None, speed=1.0):  # noqa: ANN001
            self.options = options
            self.voice = voice
            self.language = language
            self.speed = speed
            self.resolved_voice = voice or "47"
            engines.append(self)

        def synthesize(self, text: str, out_path: Path) -> None:
            write_audio(out_path, 700)

    monkeypatch.setattr(
        dub_module, "_local_model_info", lambda options: (model_dir, "kokoro")
    )
    monkeypatch.setattr(dub_module, "_cached_local_python", lambda explicit: "python")
    monkeypatch.setattr(localtts_module, "LocalTtsEngine", FakeLocalEngine)
    monkeypatch.setattr(
        localtts_module, "default_local_voice", lambda model, language: "47"
    )
    monkeypatch.setattr(localtts_module, "close_all_workers", lambda: None)
    return engines


def test_dub_local_provider_uses_local_engine(tmp_path, fake_local) -> None:
    srt = write_srt(tmp_path / "en.srt")
    out = tmp_path / "out"

    result = run_dub(
        DubOptions(
            srt=srt,
            out=out,
            tts_provider="local",
            local_tts_voice="",
            tgt_lang="zh",
            retry_delay_s=0.0,
        )
    )

    assert result.status == "ok"
    config = result.report["config"]
    assert config["tts_provider"] == "local"
    assert config["tts_fallback"] == "off"  # local 是主渠道，不再兜底
    assert config["local_tts"]["kind"] == "kokoro"
    summary = result.report["summary"]
    assert summary["local_tts"]["used"] is True
    assert summary["local_tts"]["voices"] == ["47"]  # Edge 默认音色 → 语言默认本地音色
    entries = result.report["cues"]
    assert all(entry["provider"] == "local" for entry in entries)
    assert all(entry["fallback"] is False for entry in entries)
    # 本地 TTS 的缓存文件是 wav，且与 Edge 的 mp3 缓存分开
    cached = sorted((out / "tts").glob("*.wav"))
    assert len(cached) == 3
    assert not list((out / "tts").glob("*.mp3"))


def test_edge_failure_falls_back_to_local(tmp_path, fake_local) -> None:
    srt = write_srt(tmp_path / "en.srt")
    out = tmp_path / "out"
    edge = FakeEngine(fail_texts=("Second line here",))

    result = run_dub(
        DubOptions(
            srt=srt,
            out=out,
            tts_provider="edge",
            tts_fallback="local",
            tgt_lang="zh",
            retry_delay_s=0.0,
        ),
        engine=edge,
    )

    assert result.status == "ok"
    fallback = result.report["summary"]["tts_fallback"]
    assert fallback["allowed"] == "local"
    assert [event["cue"] for event in fallback["events"]] == [2]
    assert fallback["events"][0]["from"] == "edge"
    assert fallback["events"][0]["to"] == "local"
    assert fallback["events"][0]["voice"] == "47"
    entries = result.report["cues"]
    assert entries[1]["fallback"] is True and entries[1]["provider"] == "local"
    assert entries[0]["provider"] == "edge" and entries[0]["fallback"] is False
    assert any("本地 TTS 兜底" in warning for warning in result.report["warnings"])
    # Edge 与本地缓存分开存
    assert any((out / "tts").glob("*.mp3"))
    assert any((out / "tts").glob("*.wav"))


def test_fallback_off_keeps_original_failure(tmp_path, fake_local) -> None:
    srt = write_srt(tmp_path / "en.srt")
    edge = FakeEngine(fail_texts=("Second line here",))

    result = run_dub(
        DubOptions(
            srt=srt,
            out=tmp_path / "out",
            tts_provider="edge",
            tts_fallback="off",
            retry_delay_s=0.0,
        ),
        engine=edge,
    )

    assert result.status == "partial"
    assert fake_local == []  # 没构造本地引擎
    assert result.report["summary"]["tts_fallback"]["allowed"] == "off"
    assert result.report["summary"]["tts_fallback"]["events"] == []
    assert [item["cue"] for item in result.report["failures"]] == [2]


def test_fallback_unavailable_keeps_failure_and_warns(tmp_path, monkeypatch) -> None:
    srt = write_srt(tmp_path / "en.srt")
    edge = FakeEngine(fail_texts=("Second line here",))

    def boom(options):  # noqa: ANN001
        raise DubError("没有本地模型")

    monkeypatch.setattr(dub_module, "_local_model_info", boom)
    monkeypatch.setattr(localtts_module, "close_all_workers", lambda: None)

    result = run_dub(
        DubOptions(
            srt=srt,
            out=tmp_path / "out",
            tts_provider="edge",
            tts_fallback="local",
            retry_delay_s=0.0,
        ),
        engine=edge,
    )

    assert result.status == "partial"
    assert [item["cue"] for item in result.report["failures"]] == [2]
    assert any("本地兜底不可用" in warning for warning in result.report["warnings"])


def test_local_provider_voice_mapping(tmp_path, fake_local) -> None:
    """显式本地音色/多人声映射：SPEAKER_00=50,SPEAKER_01=kokoro:47。"""

    srt = tmp_path / "en.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nFirst line here\n"
        "\n2\n00:00:02,000 --> 00:00:04,000\nSecond line here\n",
        encoding="utf-8",
        newline="\n",
    )
    speakers = tmp_path / "speakers.json"
    speakers.write_text(
        '{"speakers": ["SPEAKER_00", "SPEAKER_01"], "cues": ['
        '{"cue": 1, "start_ms": 0, "end_ms": 2000, "speaker": "SPEAKER_00"},'
        '{"cue": 2, "start_ms": 2000, "end_ms": 4000, "speaker": "SPEAKER_01"}]}',
        encoding="utf-8",
    )

    result = run_dub(
        DubOptions(
            srt=srt,
            out=tmp_path / "out",
            tts_provider="local",
            voice_by_speaker=(("SPEAKER_00", "50"), ("SPEAKER_01", "kokoro:47")),
            speakers_path=speakers,
            tgt_lang="zh",
            retry_delay_s=0.0,
        )
    )

    voices = [engine.voice for engine in fake_local]
    assert "50" in voices and "kokoro:47" in voices
    assert [entry["voice"] for entry in result.report["cues"]] == ["50", "kokoro:47"]


def test_local_options_validation(tmp_path) -> None:
    base = dict(srt=tmp_path / "a.srt", out=tmp_path / "o")
    with pytest.raises(DubError, match="tts-provider"):
        DubOptions(**base, tts_provider="magic").validate()
    with pytest.raises(DubError, match="tts-fallback"):
        DubOptions(**base, tts_fallback="edge").validate()
    with pytest.raises(DubError, match="不能同时"):
        DubOptions(**base, tts_provider="local", tts_command="ffmpeg").validate()
    with pytest.raises(DubError, match="local-tts-kind"):
        DubOptions(**base, local_tts_kind="piper").validate()
    with pytest.raises(DubError, match="local-tts-speed"):
        DubOptions(**base, local_tts_speed=2.5).validate()
    with pytest.raises(DubError, match="local-tts-threads"):
        DubOptions(**base, local_tts_threads=0).validate()
