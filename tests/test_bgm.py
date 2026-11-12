"""BGM 保留（AC-21）：人声分离编排 + 侧链混音 + 并进成片。

分离模型本身不测（那是外部引擎），测的是：命令/滤镜图对不对、降级路径是否如实标注、
混音里伴奏有没有真的"给配音让路"、以及保持既有配音链路不被破坏。
"""

from __future__ import annotations

import array
import json
import math
import subprocess
import wave
from pathlib import Path

import pytest

from cuesmith import bgm as bgm_module
from cuesmith import dub as dub_module
from cuesmith.bgm import (
    BgmError,
    BgmOptions,
    BgmResult,
    demucs_candidates,
    find_demucs_python,
    mix_filter,
    mix_voice_and_bgm,
    run_bgm,
)
from cuesmith.dub import DubError, DubOptions, run_dub
from tests.support import FFMPEG, FFPROBE, build_test_video, requires_media_tools

pytestmark = requires_media_tools

SAMPLE_RATE = 24000


# ---------------------------------------------------------------- 工具


def write_wav(
    path: Path,
    seconds: float,
    *,
    channels: int = 1,
    freq: float = 440.0,
    amplitude: int = 8000,
    silent_until: float = 0.0,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    frames = int(sample_rate * seconds)
    samples = array.array("h")
    for index in range(frames):
        moment = index / sample_rate
        value = int(amplitude * math.sin(2 * math.pi * freq * moment)) if moment >= silent_until else 0
        samples.extend([value] * channels)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return path


def read_wav(path: Path) -> tuple[array.array, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    return array.array("h", frames), rate, channels


def tone_energy(path: Path, freq: float, at_s: float, window_s: float = 0.2) -> float:
    """Goertzel：量某段里指定频率的幅度（用来验"伴奏被压下去了"）。"""

    samples, rate, channels = read_wav(path)
    if channels > 1:
        samples = array.array("h", samples[0::channels])
    start = int(at_s * rate)
    window = samples[start : start + int(window_s * rate)]
    if not window:
        return 0.0
    omega = 2 * math.pi * freq / rate
    coeff = 2 * math.cos(omega)
    s_prev = s_prev2 = 0.0
    for value in window:
        s = value + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    power = s_prev2**2 + s_prev**2 - coeff * s_prev * s_prev2
    return math.sqrt(max(power, 0.0)) / len(window) * 2


def peak(path: Path) -> int:
    samples, _, _ = read_wav(path)
    return max((abs(value) for value in samples), default=0)


def make_video(path: Path) -> Path:
    """本文件用的测试片：2 秒 320×240 深灰。"""
    return build_test_video(path, seconds=2, color="#202020", size="320x240")


# ---------------------------------------------------------------- 滤镜图


def test_mix_filter_ducks_bgm_and_normalizes_loudness(tmp_path) -> None:
    options = BgmOptions(source=tmp_path / "in.mp4", out_dir=tmp_path / "o")

    chain = mix_filter(options)

    assert "sidechaincompress" in chain
    assert "volume=-2dB" in chain  # 默认伴奏增益
    assert "amix=inputs=2" in chain
    assert "loudnorm=I=-16" in chain  # 成片统一响度
    assert "[0:a]" in chain and "[1:a]" in chain
    # 两条输入先统一到 44.1k 立体声混音总线，伴奏不能被 TTS 的 24k 单声道带塌
    assert "aformat=sample_rates=44100:channel_layouts=stereo" in chain
    assert chain.count("aformat=") == 2


def test_mix_filter_can_skip_ducking(tmp_path) -> None:
    options = BgmOptions(source=tmp_path / "in.mp4", out_dir=tmp_path / "o", duck_db=0)

    chain = mix_filter(options)

    assert "sidechaincompress" not in chain
    assert "amix=inputs=2" in chain


# ---------------------------------------------------------------- 环境探测


def test_demucs_candidates_order_and_dedup(monkeypatch) -> None:
    monkeypatch.setenv("CUESMITH_DEMUCS_PYTHON", r"C:\env\python.exe")

    candidates = demucs_candidates(r"C:\explicit\python.exe", None, r"C:\explicit\python.exe")

    assert candidates[0] == r"C:\explicit\python.exe"
    assert candidates[1] == r"C:\env\python.exe"
    assert len(candidates) == len(set(candidates))
    assert any("spike" in item and "demucs" in item for item in candidates)


def test_find_demucs_python_picks_first_importable(monkeypatch) -> None:
    monkeypatch.setattr(bgm_module, "_can_import_demucs", lambda python: python == "second")

    assert find_demucs_python("first", "second") == "second"


def test_find_demucs_python_returns_none_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(bgm_module, "_can_import_demucs", lambda python: False)

    assert find_demucs_python("missing") is None


# ---------------------------------------------------------------- 分离（相位抵消兜底）


def test_center_cancel_removes_identical_channels(tmp_path) -> None:
    """左右声道完全一样（人声居中）时，c0-c1 应该抵消成接近静音。"""

    source = write_wav(tmp_path / "same.wav", 1.0, channels=2, freq=1000, amplitude=9000)
    out = bgm_module._separate_center(source, tmp_path / "out" / "center.wav")

    assert out.is_file()
    assert peak(out) < 500  # 只允许量化噪声


def test_center_cancel_keeps_side_content(tmp_path) -> None:
    """左右有差异（伴奏在两侧）时不能被一起抹掉。"""

    left = write_wav(tmp_path / "left.wav", 1.0, freq=300, amplitude=9000)
    right = write_wav(tmp_path / "right.wav", 1.0, freq=600, amplitude=9000)
    stereo = tmp_path / "stereo.wav"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y", "-i", str(left), "-i", str(right),
            "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[out]",
            "-map", "[out]", str(stereo),
        ],
        capture_output=True,
        check=True,
    )
    out = bgm_module._separate_center(stereo, tmp_path / "out" / "center.wav")

    assert peak(out) > 5000


# ---------------------------------------------------------------- 混音


def test_mix_audio_ducks_bgm_under_voice(tmp_path) -> None:
    """配音一响，伴奏必须被压下去；配音停下，伴奏回弹。"""

    voice = write_wav(tmp_path / "voice.wav", 2.0, freq=1000, silent_until=1.0)
    instrumental = write_wav(tmp_path / "bgm.wav", 2.0, freq=300)
    options = BgmOptions(source=tmp_path / "in.mp4", out_dir=tmp_path / "o", bgm_gain_db=0)
    mixed = mix_voice_and_bgm(voice, instrumental, tmp_path / "out" / "mixed.wav", options)

    full = tone_energy(mixed, 300, 0.4)  # 配音还没开始
    ducked = tone_energy(mixed, 300, 1.5)  # 配音正在说

    assert full > 500
    assert ducked < full * 0.6  # 默认压 12dB，留足容差
    assert tone_energy(mixed, 1000, 1.5) > 200  # 配音本身还在


def test_mix_output_is_44k_stereo(tmp_path) -> None:
    """混音成片是 44.1kHz 立体声：伴奏的频宽/声场不能被配音轨的 24k 单声道带塌。"""

    voice = write_wav(tmp_path / "voice.wav", 1.0, freq=1000)
    instrumental = write_wav(tmp_path / "bgm.wav", 1.0, channels=2, freq=300)
    options = BgmOptions(source=tmp_path / "in.mp4", out_dir=tmp_path / "o")
    mixed = mix_voice_and_bgm(voice, instrumental, tmp_path / "out" / "mixed.wav", options)

    _, rate, channels = read_wav(mixed)

    assert rate == 44100
    assert channels == 2


def test_mix_format_can_be_downgraded(tmp_path) -> None:
    """仍保留显式降级能力（例如交付要求单声道）。"""

    voice = write_wav(tmp_path / "voice.wav", 1.0, freq=1000)
    instrumental = write_wav(tmp_path / "bgm.wav", 1.0, freq=300)
    options = BgmOptions(
        source=tmp_path / "in.mp4", out_dir=tmp_path / "o", mix_sample_rate=24000, mix_channels=1
    )
    mixed = mix_voice_and_bgm(voice, instrumental, tmp_path / "out" / "mixed.wav", options)

    _, rate, channels = read_wav(mixed)

    assert rate == 24000
    assert channels == 1


# ---------------------------------------------------------------- 主流程与降级


def test_run_bgm_duck_mode_uses_original_audio(tmp_path) -> None:
    source = write_wav(tmp_path / "source.wav", 1.0, freq=220)
    voice = write_wav(tmp_path / "voice.wav", 1.0, freq=440)

    result = run_bgm(
        BgmOptions(source=source, out_dir=tmp_path / "o", mode="duck"),
        voice=voice,
    )

    assert result.mode == "duck"
    assert result.separator == "original"
    assert result.mixed.is_file()
    assert any("对白" in note for note in result.notes)
    # duck 模式同样按 44.1k 立体声混音输出
    assert (result.mix_sample_rate, result.mix_channels) == (44100, 2)


def test_run_bgm_auto_falls_back_to_center(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bgm_module, "find_demucs_python", lambda *args: None)
    source = write_wav(tmp_path / "source.wav", 1.0, channels=2, freq=220)
    voice = write_wav(tmp_path / "voice.wav", 1.0, freq=440)

    result = run_bgm(BgmOptions(source=source, out_dir=tmp_path / "o"), voice=voice)

    assert result.mode == "center"
    assert result.separator == "center"
    assert any("相位抵消" in note for note in result.notes)
    # 左右声道完全相同的素材没有可保留的伴奏：必须如实说出来，而不是假装做了 BGM
    assert any("没有伴奏" in note for note in result.notes)


def test_run_bgm_separate_mode_requires_demucs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bgm_module, "find_demucs_python", lambda *args: None)
    source = write_wav(tmp_path / "source.wav", 0.5, channels=2)
    voice = write_wav(tmp_path / "voice.wav", 0.5)

    with pytest.raises(BgmError, match="demucs"):
        run_bgm(BgmOptions(source=source, out_dir=tmp_path / "o", mode="separate"), voice=voice)


def test_run_bgm_reuses_separated_stems(tmp_path, monkeypatch) -> None:
    """源片没变时复用上次 demucs 的伴奏轨，不再白跑几分钟。"""

    source = write_wav(tmp_path / "source.wav", 1.0, channels=2, freq=220)
    voice = write_wav(tmp_path / "voice.wav", 1.0, freq=440)
    out_dir = tmp_path / "bgm"
    stems = out_dir / "demucs" / "htdemucs" / "source"
    stems.mkdir(parents=True)
    write_wav(stems / "vocals.wav", 1.0, freq=300)
    write_wav(stems / "no_vocals.wav", 1.0, freq=120)
    (out_dir / "demucs-source.sha256").write_text(
        bgm_module._source_digest(source), encoding="utf-8"
    )

    monkeypatch.setattr(bgm_module, "find_demucs_python", lambda *args: "fake-python")

    def boom(*args, **kwargs):
        raise AssertionError("源片未变时不应重新跑 demucs")

    monkeypatch.setattr(bgm_module, "_separate_with_demucs", boom)

    result = run_bgm(BgmOptions(source=source, out_dir=out_dir), voice=voice)

    assert (result.mode, result.separator) == ("separate", "demucs")
    assert any("复用" in note for note in result.notes)
    assert result.mixed.is_file()


def test_run_bgm_ignores_stale_stems_when_source_changes(tmp_path, monkeypatch) -> None:
    source = write_wav(tmp_path / "source.wav", 1.0, channels=2)
    voice = write_wav(tmp_path / "voice.wav", 1.0)
    out_dir = tmp_path / "bgm"
    stems = out_dir / "demucs" / "htdemucs" / "source"
    stems.mkdir(parents=True)
    write_wav(stems / "vocals.wav", 1.0)
    write_wav(stems / "no_vocals.wav", 1.0)
    (out_dir / "demucs-source.sha256").write_text("stale-digest", encoding="utf-8")

    monkeypatch.setattr(bgm_module, "find_demucs_python", lambda *args: "fake-python")
    calls: list[object] = []

    def fake_separate(audio, out, python, model, timeout):
        calls.append(audio)
        target = Path(out) / "no_vocals.wav"
        write_wav(target, 1.0, channels=2, freq=120)
        return target

    monkeypatch.setattr(bgm_module, "_separate_with_demucs", fake_separate)

    result = run_bgm(BgmOptions(source=source, out_dir=out_dir), voice=voice)

    assert calls, "源片变了必须重新分离"
    assert result.separator == "demucs"


def test_bgm_options_validate(tmp_path) -> None:
    with pytest.raises(BgmError):
        BgmOptions(source=tmp_path / "a", out_dir=tmp_path / "o", mode="magic").validate()
    with pytest.raises(BgmError):
        BgmOptions(source=tmp_path / "a", out_dir=tmp_path / "o", duck_db=-99).validate()
    with pytest.raises(BgmError):
        BgmOptions(source=tmp_path / "a", out_dir=tmp_path / "o", bgm_gain_db=-99).validate()
    with pytest.raises(BgmError):
        BgmOptions(source=tmp_path / "a", out_dir=tmp_path / "o", mix_channels=6).validate()
    with pytest.raises(BgmError):
        BgmOptions(source=tmp_path / "a", out_dir=tmp_path / "o", mix_sample_rate=100).validate()


def test_run_bgm_rejects_missing_source(tmp_path) -> None:
    with pytest.raises(BgmError, match="不存在"):
        run_bgm(BgmOptions(source=tmp_path / "nope.mp4", out_dir=tmp_path / "o"), voice=tmp_path / "v.wav")


# ---------------------------------------------------------------- 接进配音链路


class _FakeEngine:
    """1 秒正弦假 TTS，只测编排。"""

    def synthesize(self, text: str, out_path: Path) -> None:
        write_wav(Path(out_path), 1.0, freq=440)


def _write_srt(path: Path, text: str = "First line here") -> Path:
    path.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n", encoding="utf-8", newline="\n")
    return path


def test_keep_bgm_requires_video_or_source(tmp_path) -> None:
    options = DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", keep_bgm=True)

    with pytest.raises(DubError, match="keep-bgm"):
        options.validate()


def test_keep_bgm_mixes_and_muxes(tmp_path, monkeypatch) -> None:
    srt = _write_srt(tmp_path / "zh.srt")
    video = make_video(tmp_path / "in.mp4")
    mixed = write_wav(tmp_path / "mixed.wav", 2.0, freq=330)

    def fake_run_bgm(options, *, voice):
        assert options.source == video
        return BgmResult(
            mode="separate",
            source=video,
            instrumental=tmp_path / "inst.wav",
            mixed=mixed,
            separator="demucs",
            notes=("测试用的分离结果",),
            wall_s=1.5,
        )

    monkeypatch.setattr(dub_module, "run_bgm", fake_run_bgm)
    result = run_dub(
        DubOptions(srt=srt, out=tmp_path / "out", video=video, keep_bgm=True, retry_delay_s=0.0),
        engine=_FakeEngine(),
    )

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["config"]["bgm"]["enabled"] is True
    assert report["config"]["bgm"]["mix_sample_rate"] == 44100
    assert report["config"]["bgm"]["mix_channels"] == 2
    assert report["summary"]["bgm"] == {
        "mode": "separate",
        "separator": "demucs",
        "wall_s": 1.5,
        "mix_sample_rate": 44100,
        "mix_channels": 2,
    }
    assert report["outputs"]["mixed"] == str(mixed.resolve())
    assert report["warnings"] == ["测试用的分离结果"]
    assert result.video_path is not None and result.video_path.is_file()

    # 并进视频的必须是混音轨（带伴奏），不是纯配音
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(result.video_path)],
        capture_output=True,
        check=True,
    )
    codecs = {s["codec_type"]: s["codec_name"] for s in json.loads(probe.stdout)["streams"]}
    assert codecs["audio"] == "aac"


def test_bgm_failure_is_reported_as_dub_error(tmp_path, monkeypatch) -> None:
    srt = _write_srt(tmp_path / "zh.srt")
    video = make_video(tmp_path / "in.mp4")

    def boom(options, *, voice):
        raise BgmError("分离炸了")

    monkeypatch.setattr(dub_module, "run_bgm", boom)

    with pytest.raises(DubError, match="BGM 处理失败"):
        run_dub(
            DubOptions(srt=srt, out=tmp_path / "out", video=video, keep_bgm=True, retry_delay_s=0.0),
            engine=_FakeEngine(),
        )
