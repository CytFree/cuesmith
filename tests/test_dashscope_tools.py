"""阿里云百炼（DashScope）接入：同步 TTS worker + 声音复刻 CLI。

不联网：用假的 ``requests.post/get`` 验证请求形状、音频写盘、错误透出、复刻轮询；
真接口等有 ``DASHSCOPE_API_KEY`` 后按 M11 文档的步骤单独跑（见 work/dashscope/README.md）。
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import tts_dashscope as ds  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = content
        self.text = json.dumps(self._payload)[:300] if payload is not None else ""

    def json(self) -> object:
        return self._payload


def make_wav(path: Path, seconds: float, sample_rate: int = 24000) -> Path:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x01\x00" * frames)
    return path


# ---------------------------------------------------------------- 接口地址 / 密钥


def test_resolve_base_url_prefers_explicit() -> None:
    url, source = ds.resolve_base_url("https://custom/api/v1", "ws")

    assert url == "https://custom/api/v1"
    assert "显式" in source


def test_resolve_base_url_uses_workspace(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    url, source = ds.resolve_base_url(workspace_id="ws123")

    assert url == "https://ws123.cn-beijing.maas.aliyuncs.com/api/v1"
    assert "业务空间" in source


def test_resolve_base_url_falls_back_to_compat_domain(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)

    url, _source = ds.resolve_base_url()

    assert url == "https://dashscope.aliyuncs.com/api/v1"


def test_missing_api_key_has_actionable_message(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(ds.DashScopeError, match="DASHSCOPE_API_KEY"):
        ds.synthesize("你好", Path("x.wav"), voice="longanyang")


# ---------------------------------------------------------------- 合成


def test_synthesize_downloads_audio_url(tmp_path, monkeypatch) -> None:
    posted: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        posted.update(url=url, headers=headers, payload=json)
        return FakeResponse(
            200,
            {
                "request_id": "req-1",
                "output": {"audio": {"url": "https://oss.example/a.wav"}},
            },
        )

    monkeypatch.setattr(ds.requests, "post", fake_post)
    monkeypatch.setattr(
        ds.requests, "get", lambda url, timeout=None: FakeResponse(200, content=b"RIFFDATA")
    )
    out = tmp_path / "cue.wav"

    result = ds.synthesize(
        "你好，世界。",
        out,
        voice="longanyang",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/api/v1",
    )

    assert out.read_bytes() == b"RIFFDATA"
    assert result["request_id"] == "req-1"
    assert result["bytes"] == 8
    payload = posted["payload"]
    assert payload["model"] == ds.DEFAULT_MODEL
    assert payload["input"] == {
        "text": "你好，世界。",
        "voice": "longanyang",
        "format": "wav",
        "sample_rate": 24000,
    }
    assert posted["url"].endswith("/services/audio/tts/SpeechSynthesizer")
    assert posted["headers"]["Authorization"] == "Bearer sk-test"


def test_synthesize_passes_instruction(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured.update(json)
        return FakeResponse(200, {"output": {"audio": {"data": base64.b64encode(b"x").decode()}}})

    monkeypatch.setattr(ds.requests, "post", fake_post)

    ds.synthesize(
        "慢一点说", Path("x.wav"), voice="v", api_key="k", instruction="请用缓慢语速"
    )

    assert captured["input"]["instruction"] == "请用缓慢语速"


def test_synthesize_accepts_inline_base64(tmp_path, monkeypatch) -> None:
    data = base64.b64encode(b"WAVDATA").decode()
    monkeypatch.setattr(
        ds.requests, "post", lambda *a, **k: FakeResponse(200, {"output": {"audio": {"data": data}}})
    )
    out = tmp_path / "x.wav"

    ds.synthesize("hi", out, voice="v", api_key="k")

    assert out.read_bytes() == b"WAVDATA"


def test_synthesize_surfaces_api_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        ds.requests,
        "post",
        lambda *a, **k: FakeResponse(
            401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."}
        ),
    )

    with pytest.raises(ds.DashScopeError, match="InvalidApiKey"):
        ds.synthesize("hi", tmp_path / "x.wav", voice="v", api_key="bad")


def test_synthesize_network_error_is_wrapped(tmp_path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise ds.requests.ConnectionError("connection refused")

    monkeypatch.setattr(ds.requests, "post", boom)

    with pytest.raises(ds.DashScopeError, match="网络失败"):
        ds.synthesize("hi", tmp_path / "x.wav", voice="v", api_key="k")


def test_synthesize_rejects_empty_text(tmp_path) -> None:
    with pytest.raises(ds.DashScopeError, match="文本为空"):
        ds.synthesize("   ", tmp_path / "x.wav", voice="v", api_key="k")


# ---------------------------------------------------------------- 参考音频 / 复刻


def test_encode_audio_data_uri_roundtrip(tmp_path) -> None:
    audio = make_wav(tmp_path / "ref.wav", 1.0)

    uri = ds.encode_audio_data_uri(audio)

    assert uri.startswith("data:audio/wav;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == audio.read_bytes()


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不在 PATH")
def test_normalize_reference_rejects_too_short(tmp_path) -> None:
    audio = make_wav(tmp_path / "short.wav", 2.0)

    with pytest.raises(ds.DashScopeError, match="太短"):
        ds.normalize_reference(audio, tmp_path / "out.wav")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不在 PATH")
def test_normalize_reference_converts_to_mono_24k(tmp_path) -> None:
    audio = make_wav(tmp_path / "long.wav", 12.0)

    normalized = ds.normalize_reference(audio, tmp_path / "out.wav")

    with wave.open(str(normalized), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 24000
    assert normalized.stat().st_size < ds.MAX_REFERENCE_BYTES


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不在 PATH")
def test_create_voice_and_wait_ready(tmp_path, monkeypatch) -> None:
    audio = make_wav(tmp_path / "long.wav", 12.0)
    calls: list[dict] = []
    statuses = iter(["DEPLOYING", "OK"])

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(json)
        action = json["input"]["action"]
        if action == "create_voice":
            assert json["input"]["audio"].startswith("data:audio/wav;base64,")
            return FakeResponse(200, {"output": {"voice_id": "voice-abc"}})
        assert action == "query_voice"
        return FakeResponse(200, {"output": {"status": next(statuses)}})

    monkeypatch.setattr(ds.requests, "post", fake_post)

    voice_id = ds.create_voice(
        audio, prefix="spkA", target_model="cosyvoice-v3-flash", api_key="k"
    )
    output = ds.wait_voice_ready("voice-abc", api_key="k", interval_s=0.0)

    assert voice_id == "voice-abc"
    assert output["status"] == "OK"
    assert calls[0]["input"]["target_model"] == "cosyvoice-v3-flash"
    assert calls[0]["input"]["prefix"] == "spkA"
    assert [call["input"]["action"] for call in calls] == [
        "create_voice",
        "query_voice",
        "query_voice",
    ]


def test_create_voice_rejects_bad_prefix(tmp_path) -> None:
    with pytest.raises(ds.DashScopeError, match="prefix"):
        ds.create_voice(tmp_path / "a.wav", prefix="有中文", api_key="k")


def test_wait_voice_ready_reports_failed_status(monkeypatch) -> None:
    monkeypatch.setattr(
        ds.requests, "post", lambda *a, **k: FakeResponse(200, {"output": {"status": "FAILED"}})
    )

    with pytest.raises(ds.DashScopeError, match="状态异常"):
        ds.wait_voice_ready("v", api_key="k", interval_s=0.0)


def test_list_voices_parses_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured.update(json)
        return FakeResponse(
            200, {"output": {"voice_list": [{"voice_id": "v1"}, {"voice_id": "v2"}]}}
        )

    monkeypatch.setattr(ds.requests, "post", fake_post)

    voices = ds.list_voices(prefix="spk", api_key="k")

    assert [item["voice_id"] for item in voices] == ["v1", "v2"]
    assert captured["input"]["action"] == "list_voice"  # 实测 HTTP 动作名
    assert captured["input"]["prefix"] == "spk"


def test_delete_voice_posts_action(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured.update(json)
        return FakeResponse(200, {"output": {}})

    monkeypatch.setattr(ds.requests, "post", fake_post)

    ds.delete_voice("v1", api_key="k")

    assert captured["input"] == {"action": "delete_voice", "voice_id": "v1"}


# ---------------------------------------------------------------- CLI 契约


def test_cli_writes_audio_and_returns_zero(tmp_path, monkeypatch) -> None:
    text = tmp_path / "cue.txt"
    text.write_text("你好", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(
        ds.requests,
        "post",
        lambda *a, **k: FakeResponse(
            200, {"request_id": "r", "output": {"audio": {"data": base64.b64encode(b"abc").decode()}}}
        ),
    )
    out = tmp_path / "cue.wav"

    code = ds.main(
        [
            "--text-file", str(text),
            "--out", str(out),
            "--voice", "longanyang",
        ]
    )

    assert code == 0
    assert out.read_bytes() == b"abc"


def test_cli_reports_failure(tmp_path, monkeypatch) -> None:
    text = tmp_path / "cue.txt"
    text.write_text("你好", encoding="utf-8")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    code = ds.main(["--text-file", str(text), "--out", str(tmp_path / "o.wav"), "--voice", "v"])

    assert code == 1
