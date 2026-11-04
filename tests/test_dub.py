"""FR-3 配音编排（AC-7 / AC-8 / AC-9 / AC-14）。

用**假 TTS 引擎**跑：真引擎要联网且时长不可控，而这里要验的是编排逻辑
（并发 / 重试 / 时长报告 / atempo / 音频铺轨 / mux）。真引擎单独做联网集成测试。
"""

from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

from submatrix import dub as dub_module
from submatrix.dub import (
    DubError,
    DubOptions,
    build_engine,
    run_dub,
)
from submatrix.tts import CommandTtsEngine, EdgeTtsEngine, TtsError, parse_rate

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg / ffprobe 不在 PATH"
)

SRT_3 = (
    "1\n00:00:00,000 --> 00:00:02,000\nFirst line here\n"
    "\n"
    "2\n00:00:02,000 --> 00:00:04,000\nSecond line here\n"
    "\n"
    "3\n00:00:04,000 --> 00:00:06,000\nThird line here\n"
)

SRT_WITH_GAP = (
    "1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
    "\n"
    "2\n00:00:03,000 --> 00:00:04,000\nBye now\n"
)


def write_srt(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_audio(path: Path, duration_ms: int, *, freq: int = 440, sample_rate: int = 24000) -> None:
    frames = int(sample_rate * duration_ms / 1000)
    if freq:
        samples = array.array(
            "h", (int(8000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(frames))
        )
    else:
        samples = array.array("h", bytes(2 * frames))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def wav_energy(path: Path, at_ms: int, window_ms: int = 200) -> int:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = array.array("h", frames)
    start = at_ms * rate // 1000
    end = min(len(samples), start + window_ms * rate // 1000)
    return max((abs(value) for value in samples[start:end]), default=0)


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return int(handle.getnframes() * 1000 / handle.getframerate())


class FakeEngine:
    """可编程的假引擎：控制时长、失败、延迟，并统计并发度。"""

    def __init__(
        self,
        *,
        durations: dict[str, int] | None = None,
        default_ms: int = 1000,
        fail_texts: tuple[str, ...] = (),
        fail_first: int = 0,
        delay_s: float = 0.0,
        tone: bool = True,
    ) -> None:
        self.durations = durations or {}
        self.default_ms = default_ms
        self.fail_texts = set(fail_texts)
        self.fail_first = fail_first
        self.delay_s = delay_s
        self.tone = tone
        self.attempts: dict[str, int] = {}
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def synthesize(self, text: str, out_path: Path) -> None:
        with self._lock:
            self.attempts[text] = self.attempts.get(text, 0) + 1
            attempt = self.attempts[text]
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            if text in self.fail_texts and attempt <= self.fail_first:
                raise TtsError("模拟合成失败")
            write_audio(out_path, self.durations.get(text, self.default_ms), freq=440 if self.tone else 0)
        finally:
            with self._lock:
                self._active -= 1


def dub(srt_path: Path, out: Path, engine: FakeEngine, **kwargs) -> object:
    options = DubOptions(srt=srt_path, out=out, retry_delay_s=0.0, **kwargs)
    return run_dub(options, engine=engine)


class RateAwareEngine:
    """时长随语速缩放的假引擎（速率越高音频越短），用来验 OPT-2 提语速兜底。

    ``base_ms`` 是该引擎在 ``base_rate`` 下的时长；换一个 rate 重建时按
    ``(1+base_rate)/(1+rate)`` 缩放，跟真实 Edge-TTS 的语速语义一致。
    """

    def __init__(self, base_ms: int, rate: str | None, base_rate: str | None = "+30%") -> None:
        self.base_ms = base_ms
        self.rate = rate
        self.base_rate = base_rate
        self.calls: list[str | None] = []

    def synthesize(self, text: str, out_path: Path) -> None:  # noqa: ARG002
        self.calls.append(self.rate)
        ratio = (1.0 + parse_rate(self.base_rate)) / (1.0 + parse_rate(self.rate))
        write_audio(out_path, max(50, round(self.base_ms * ratio)))

    def engine_for(self, options, voice=None, *, rate=None):  # noqa: ARG002
        return RateAwareEngine(self.base_ms, rate, self.base_rate)


def report_of(result) -> dict:
    return json.loads(result.report_path.read_text(encoding="utf-8"))


def cue_reports(report: dict) -> dict[int, dict]:
    return {entry["cue"]: entry for entry in report["cues"]}


# ---------------------------------------------------------------- 报告覆盖（AC-8）


def test_report_lists_every_cue(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    result = dub(srt, tmp_path / "out", FakeEngine())

    report = report_of(result)
    assert report["command"] == "dub"
    assert report["status"] == "ok"
    assert len(report["cues"]) == 3
    assert [entry["cue"] for entry in report["cues"]] == [1, 2, 3]
    for entry in report["cues"]:
        assert entry["slot_ms"] > 0
        assert entry["audio_ms"] > 0
        assert entry["action"] in {"none", "absorb", "atempo", "manual"}
        assert entry["overflow_pct"] == pytest.approx(
            (entry["audio_ms"] - entry["slot_ms"]) / entry["slot_ms"] * 100, abs=0.1
        )


def test_report_has_config_and_input_hash(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    result = dub(srt, tmp_path / "out", FakeEngine(), voice="en-US-AriaNeural")

    report = report_of(result)
    assert report["config"]["voice"] == "en-US-AriaNeural"
    assert report["config"]["concurrency"] == 10
    assert len(report["input"]["srt"]["sha256"]) == 64
    assert report["summary"]["cues"] == 3


def test_overflow_over_ten_percent_is_listed(tmp_path) -> None:
    srt = write_srt(
        tmp_path / "en.srt",
        "1\n00:00:00,000 --> 00:00:02,000\nShort slot\n"
        "\n"
        "2\n00:00:00,000 --> 00:00:04,000\nLong slot\n",
    )
    engine = FakeEngine(durations={"Short slot": 2000, "Long slot": 4800})
    result = dub(srt, tmp_path / "out", engine)

    report = report_of(result)
    listed = [entry for entry in report["cues"] if entry["overflow_pct"] > 10]
    assert [entry["cue"] for entry in listed] == [2]
    assert report["summary"]["overflow_over_10pct"] == 1


# ---------------------------------------------------------------- 溢出策略


def test_short_audio_needs_no_action(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:04,000\nFits fine\n")
    result = dub(srt, tmp_path / "out", FakeEngine(default_ms=2000))

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "none"
    assert entry["atempo"] == 1.0


def test_small_overflow_uses_atempo(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nSlightly long\n")
    result = dub(srt, tmp_path / "out", FakeEngine(default_ms=2400))  # 1.2x

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "atempo"
    assert entry["atempo"] == pytest.approx(1.2, abs=0.01)


def test_large_overflow_uses_tempo_limit_and_is_marked_manual(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nWay too long\n")
    result = dub(
        srt, tmp_path / "out", FakeEngine(default_ms=6000), adaptive_rate=False
    )  # 3x，关掉提语速

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "manual"
    assert entry["atempo"] == pytest.approx(1.4, abs=0.001)  # OPT-2：兜底上限 1.3 → 1.4
    assert report_of(result)["summary"]["manual"] == 1


def test_large_overflow_respects_custom_tempo_limit(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nWay too long\n")
    result = dub(
        srt, tmp_path / "out", FakeEngine(default_ms=6000), adaptive_rate=False, tempo_limit=1.3
    )

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "manual"
    assert entry["atempo"] == pytest.approx(1.3, abs=0.001)


def test_overflow_within_gap_is_absorbed(tmp_path) -> None:
    """槽位只有 1s，但下一条 4s 才开始：多出来的 0.5s 应该被静音吸收，不该压语速。"""

    srt = write_srt(
        tmp_path / "en.srt",
        "1\n00:00:00,000 --> 00:00:01,000\nShort slot\n"
        "\n"
        "2\n00:00:04,000 --> 00:00:05,000\nNext one\n",
    )
    engine = FakeEngine(durations={"Short slot": 1500, "Next one": 800})
    result = dub(srt, tmp_path / "out", engine)

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "absorb"
    assert entry["atempo"] == 1.0
    assert entry["room_ms"] == 4000
    assert report_of(result)["summary"]["absorbed"] == 1


def test_atempo_ratio_uses_available_room_not_slot(tmp_path) -> None:
    """按槽位算会判成 2.2x→manual；按可用空间算只有 1.1x，用 atempo 就够。"""

    srt = write_srt(
        tmp_path / "en.srt",
        "1\n00:00:00,000 --> 00:00:01,000\nFirst\n"
        "\n"
        "2\n00:00:02,000 --> 00:00:03,000\nSecond\n",
    )
    engine = FakeEngine(durations={"First": 2200, "Second": 800})
    result = dub(srt, tmp_path / "out", engine)

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "atempo"
    assert entry["atempo"] == pytest.approx(1.1, abs=0.01)


def test_last_cue_can_use_video_tail_as_room(tmp_path) -> None:
    """最后一条的可用空间到视频结尾为止。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:01,000\nOnly one\n")
    video = make_video(tmp_path / "in.mp4")  # 6 秒
    engine = FakeEngine(durations={"Only one": 3000})
    result = dub(srt, tmp_path / "out", engine, video=video)

    entry = cue_reports(report_of(result))[1]
    assert entry["action"] == "absorb"
    assert entry["room_ms"] == pytest.approx(6000, abs=200)


# ---------------------------------------------------------------- 自适应提语速（OPT-2）


def test_rate_ladder_scales_with_base_and_caps() -> None:
    assert dub_module.rate_ladder(None) == ["+15%", "+25%", "+35%"]
    assert dub_module.rate_ladder("+30%") == ["+45%", "+55%", "+65%"]
    # 已经很快的语速只留一个不越界档位
    assert dub_module.rate_ladder("+70%") == ["+80%"]


def test_choose_escalated_rate_picks_smallest_sufficient() -> None:
    # 2.6s 音频 / 2.0s 可用：需要再快 2.6/2.0/1.15=1.13 倍 → +45%(1.115) 不够，+55% 够
    assert dub_module._choose_escalated_rate(2600, 2000, 2000, "+30%") == "+55%"
    # 怎么都快不进去：给最大档，剩下交给 atempo/转人工
    assert dub_module._choose_escalated_rate(5000, 2000, 2000, "+30%") == "+65%"
    # 没有语速基准（外部命令引擎）也不该炸：按 +15% 起算
    assert dub_module._choose_escalated_rate(2600, 2000, 2000, None) == "+15%"


def test_adaptive_rate_escalates_instead_of_manual(tmp_path, monkeypatch) -> None:
    """放不下的条目提语速重合成，别硬 atempo 到 1.4 再转人工。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nToo long\n")
    base_engine = RateAwareEngine(3200, "+30%")

    def fake_build(options, voice=None, *, rate=None):  # noqa: ARG001
        return RateAwareEngine(3200, rate)

    monkeypatch.setattr(dub_module, "build_engine", fake_build)
    result = dub(
        srt, tmp_path / "out", base_engine, tts_rate="+30%", concurrency=2
    )

    report = report_of(result)
    entry = cue_reports(report)[1]
    assert entry["rate_escalated"] is True
    assert entry["rate"] == "+65%"  # 1.6x 溢出，最大档才够
    assert entry["action"] == "atempo"
    assert entry["atempo"] <= 1.4
    assert entry["played_ms"] <= entry["room_ms"]
    assert report["summary"]["manual"] == 0
    assert report["summary"]["rate_escalated"] == 1
    assert report["config"]["rate_ladder"] == ["+45%", "+55%", "+65%"]
    assert report["config"]["adaptive_rate"] is True
    # 基准语速 + 提语速各合成一次：第二次用的就是新档位
    assert "+30%" in base_engine.calls


def test_adaptive_rate_can_be_disabled(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nToo long\n")
    result = dub(
        srt,
        tmp_path / "out",
        FakeEngine(default_ms=3200),
        tts_rate="+30%",
        adaptive_rate=False,
    )

    report = report_of(result)
    entry = cue_reports(report)[1]
    assert entry["rate_escalated"] is False
    assert entry["action"] == "manual"
    assert report["summary"]["rate_escalated"] == 0
    assert report["config"]["adaptive_rate"] is False


def test_escalation_failure_keeps_base_audio_and_warns(tmp_path, monkeypatch) -> None:
    """提语速合成失败时不能把整片搞挂：保留原速音频、如实告警、仍标 manual。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nToo long\n")

    class BrokenEscalation(RateAwareEngine):
        def synthesize(self, text, out_path) -> None:  # noqa: ARG002
            if self.rate != self.base_rate:
                raise TtsError("模拟提语速失败")
            super().synthesize(text, out_path)

    base_engine = BrokenEscalation(3200, "+30%")

    def fake_build(options, voice=None, *, rate=None):  # noqa: ARG001
        return BrokenEscalation(3200, rate)

    monkeypatch.setattr(dub_module, "build_engine", fake_build)
    result = dub(srt, tmp_path / "out", base_engine, tts_rate="+30%")

    report = report_of(result)
    entry = cue_reports(report)[1]
    assert report["status"] == "ok"
    assert report["summary"]["failed"] == 0
    assert entry["rate_escalated"] is False
    assert entry["action"] == "manual"
    assert any("提语速" in warning for warning in report["warnings"])


def test_tts_command_skips_adaptive_rate(tmp_path) -> None:
    """外部命令模板不支持改语速，提语速自动跳过（配置里记 adaptive_rate=False）。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nToo long\n")
    result = dub(
        srt,
        tmp_path / "out",
        FakeEngine(default_ms=6000),
        tts_rate="+30%",
        tts_command="python tools/tts_dashscope.py --text-file {text_file} --out {out}",
    )

    report = report_of(result)
    assert report["config"]["adaptive_rate"] is False
    assert report["config"]["rate_ladder"] == []
    assert report["summary"]["rate_escalated"] == 0
    assert cue_reports(report)[1]["action"] == "manual"


# ---------------------------------------------------------------- 失败与重试（AC-9）


def test_failed_cue_does_not_block_others(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    engine = FakeEngine(fail_texts=("Second line here",), fail_first=99)
    result = dub(srt, tmp_path / "out", engine)

    report = report_of(result)
    assert result.status == "partial"
    assert report["summary"]["failed"] == 1
    assert [entry["cue"] for entry in report["failures"]] == [2]
    assert report["failures"][0]["error"]
    assert len([entry for entry in report["cues"] if entry["audio_ms"] > 0]) == 2


def test_retry_then_success(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nRetry me\n")
    engine = FakeEngine(fail_texts=("Retry me",), fail_first=2)  # 前两次失败，第三次成功
    result = dub(srt, tmp_path / "out", engine, retries=2)

    entry = cue_reports(report_of(result))[1]
    assert entry["attempts"] == 3
    assert report_of(result)["summary"]["failed"] == 0
    assert result.status == "ok"


def test_retry_exhaustion_records_failure(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nAlways fails\n")
    engine = FakeEngine(fail_texts=("Always fails",), fail_first=99)
    result = dub(srt, tmp_path / "out", engine, retries=2)

    # 首轮 3 次（1 + 重试 2）+ 二次补跑降并发 3 次 = 6；仍失败才记 failure
    assert engine.attempts["Always fails"] == 6
    assert report_of(result)["failures"][0]["cue"] == 1


# ---------------------------------------------------------------- 并发（AC-7）


def test_synthesis_runs_concurrently(tmp_path) -> None:
    lines = "".join(
        f"{index}\n00:00:0{index},000 --> 00:00:0{index + 1},000\nLine {index}\n\n"
        for index in range(1, 9)
    ).strip()
    srt = write_srt(tmp_path / "en.srt", lines + "\n")
    engine = FakeEngine(default_ms=500, delay_s=0.05)

    dub(srt, tmp_path / "out", engine, concurrency=4)

    assert engine.max_active >= 2


# ---------------------------------------------------------------- 音频铺轨


def test_audio_is_placed_at_slot_start(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_WITH_GAP)
    result = dub(srt, tmp_path / "out", FakeEngine(default_ms=800, tone=True))

    assert wav_energy(result.audio_path, 100) > 1000
    assert wav_energy(result.audio_path, 3100) > 1000
    assert wav_energy(result.audio_path, 2000) == 0
    assert wav_duration_ms(result.audio_path) >= 4000


# ---------------------------------------------------------------- 与视频 mux（AC-14）


def make_video(path: Path) -> Path:
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=#202020:s=320x240:d=6:r=25",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path


def test_muxes_audio_into_video(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    video = make_video(tmp_path / "in.mp4")
    result = dub(srt, tmp_path / "out", FakeEngine(), video=video)

    assert result.video_path is not None and result.video_path.is_file()
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(result.video_path)],
        capture_output=True,
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in streams}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "aac"

    duration = float(
        json.loads(
            subprocess.run(
                [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", str(result.video_path)],
                capture_output=True,
                check=True,
            ).stdout
        )["format"]["duration"]
    )
    assert duration == pytest.approx(6.0, abs=0.5)


def test_video_longer_than_subtitles_is_not_truncated(tmp_path) -> None:
    """AC-14：视频比字幕长时，音轨要补到视频长度，否则 -shortest 会把画面截短。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nShort one\n")
    video = make_video(tmp_path / "in.mp4")  # 6 秒
    result = dub(srt, tmp_path / "out", FakeEngine(default_ms=1000), video=video)

    duration = float(
        json.loads(
            subprocess.run(
                [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", str(result.video_path)],
                capture_output=True,
                check=True,
            ).stdout
        )["format"]["duration"]
    )
    assert duration == pytest.approx(6.0, abs=0.5)


def test_tts_audio_is_reused_across_runs(tmp_path) -> None:
    """云端 TTS 按字符计费：同一文本+音色重跑必须复用音频，不重复合成。"""

    srt = write_srt(tmp_path / "en.srt", SRT_3)
    out = tmp_path / "out"
    engine = FakeEngine(default_ms=800)

    first = dub(srt, out, engine)
    second = dub(srt, out, engine)
    third = dub(srt, out, engine, reuse_tts=False)

    # 注意：三次共用同一个 out 目录（报告路径相同），要读运行时的 report 而不是磁盘上的旧文件
    assert first.report["summary"]["reused"] == 0  # type: ignore[index]
    assert second.report["summary"]["reused"] == 3  # type: ignore[index]
    assert second.report["summary"]["synthesized"] == 3  # type: ignore[index]
    assert third.report["summary"]["reused"] == 0  # type: ignore[index]
    # 第二次没有调用引擎，第三次强制重合成又各调用一次
    assert {text: attempts for text, attempts in engine.attempts.items()} == {
        "First line here": 2,
        "Second line here": 2,
        "Third line here": 2,
    }


def test_tts_cache_key_changes_with_voice(tmp_path) -> None:
    """文本不变、只换音色 → 不能复用旧音频（新指纹文件名 + 重新合成）。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nSame line\n")
    out = tmp_path / "out"
    engine_a, engine_b = FakeEngine(), FakeEngine()

    run_dub(
        DubOptions(srt=srt, out=out, voice="en-US-AriaNeural", retry_delay_s=0.0),
        engine=engine_a,
    )
    run_dub(
        DubOptions(srt=srt, out=out, voice="en-US-GuyNeural", retry_delay_s=0.0),
        engine=engine_b,
    )

    assert engine_a.attempts["Same line"] == 1
    assert engine_b.attempts["Same line"] == 1


def test_webm_vp9_video_is_transcoded_when_copy_unsupported(tmp_path) -> None:
    """真实素材常见 WebM（VP9+Opus）：VP9 不能直接 copy 进 MP4，必须回退转码。"""

    webm = tmp_path / "in.webm"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=#202020:s=320x240:d=6:r=25",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=6",
            "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
            "-c:a", "libopus", "-shortest", str(webm),
        ],
        capture_output=True,
        check=True,
    )
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    result = dub(srt, tmp_path / "out", FakeEngine(), video=webm)

    report = report_of(result)
    assert report["outputs"]["video_transcoded"] is True
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(result.video_path)],
        capture_output=True,
        check=True,
    )
    codecs = {s["codec_type"]: s["codec_name"] for s in json.loads(probe.stdout)["streams"]}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "aac"


# ---------------------------------------------------------------- 引擎


def test_build_engine_defaults_to_edge(tmp_path) -> None:
    engine = build_engine(DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o"))

    assert isinstance(engine, EdgeTtsEngine)
    assert engine.voice == "en-US-AriaNeural"
    # 默认会自动探测装有 edge-tts 的解释器（GUI venv 里通常没有）
    assert engine.python


def test_build_engine_auto_detects_edge_tts_python(tmp_path, monkeypatch) -> None:
    import submatrix.dub as dub_module

    monkeypatch.setattr(dub_module, "_default_edge_tts_python", lambda: r"C:\tts\python.exe")
    engine = build_engine(DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o"))

    assert isinstance(engine, EdgeTtsEngine)
    assert engine.python == r"C:\tts\python.exe"


def test_build_engine_explicit_tts_python_wins(tmp_path, monkeypatch) -> None:
    import submatrix.dub as dub_module

    monkeypatch.setattr(dub_module, "_default_edge_tts_python", lambda: "auto-python")
    engine = build_engine(
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", tts_python="explicit-python")
    )

    assert isinstance(engine, EdgeTtsEngine)
    assert engine.python == "explicit-python"


def test_build_engine_passes_rate(tmp_path) -> None:
    engine = build_engine(DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", tts_rate="+30%"))

    assert isinstance(engine, EdgeTtsEngine)
    assert engine.rate == "+30%"


def test_build_engine_command_mode(tmp_path) -> None:
    engine = build_engine(
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", tts_command="ffmpeg -y {out}")
    )

    assert isinstance(engine, CommandTtsEngine)


def test_build_engine_renders_voice_id_placeholder(tmp_path) -> None:
    """多人声 + 云端复刻：每个说话人的 voice_id 要在建引擎时就替换进命令模板。"""

    options = DubOptions(
        srt=tmp_path / "a.srt",
        out=tmp_path / "o",
        tts_command="python tools/tts_dashscope.py --text-file {text_file} --out {out} --voice {voice_id}",
    )

    engine = build_engine(options, "cosyvoice-v3-flash-spkA-abc123")

    assert isinstance(engine, CommandTtsEngine)
    assert "{voice_id}" not in engine.command
    assert "cosyvoice-v3-flash-spkA-abc123" in engine.command
    # 不同说话人 → 不同命令（同一个模板，不同音色）
    other = build_engine(options, "cosyvoice-v3-flash-spkB-def456")
    assert other.command != engine.command


def test_build_engine_renders_voice_placeholder(tmp_path) -> None:
    engine = build_engine(
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", tts_command="tts --speaker {voice}"),
        "voice-x",
    )

    assert isinstance(engine, CommandTtsEngine)
    assert engine.command == "tts --speaker voice-x"


def test_command_engine_runs_external_command(tmp_path) -> None:
    """外部命令模板：用 ffmpeg 造 1 秒正弦当"TTS 输出"，验证模板替换与执行。"""

    engine = CommandTtsEngine(
        command=f'"{FFMPEG}" -v error -y -f lavfi -i sine=frequency=440:duration=1 {{out}}'
    )
    out = tmp_path / "cue.wav"

    engine.synthesize("External", out)

    assert out.is_file()
    assert out.stat().st_size > 10_000


def test_edge_engine_reports_missing_package(tmp_path) -> None:
    engine = EdgeTtsEngine(python=sys.executable, voice="en-US-AriaNeural")
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        with pytest.raises(TtsError, match="edge-tts"):
            engine.synthesize("hello", tmp_path / "cue.mp3")


def test_options_validate_rejects_bad_values(tmp_path) -> None:
    with pytest.raises(DubError):
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", concurrency=0).validate()
    with pytest.raises(DubError):
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", tempo_limit=0.5).validate()
    with pytest.raises(DubError):
        DubOptions(srt=tmp_path / "a.srt", out=tmp_path / "o", retries=-1).validate()


def test_missing_input_raises(tmp_path) -> None:
    with pytest.raises(DubError):
        run_dub(DubOptions(srt=tmp_path / "nope.srt", out=tmp_path / "o"), engine=FakeEngine())


# ---------------------------------------------------------------- 多人声（M11）


def write_speakers(path: Path, assignments: list[tuple[int, str]]) -> Path:
    speakers: list[str] = []
    for _, speaker in assignments:
        if speaker not in speakers:
            speakers.append(speaker)
    payload = {
        "schema_version": 1,
        "speakers": speakers,
        "cues": [
            {"cue": cue, "start_ms": 0, "end_ms": 1000, "speaker": speaker}
            for cue, speaker in assignments
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_multi_voice_uses_speaker_voice_map(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    speakers = write_speakers(
        tmp_path / "speakers.json",
        [(1, "SPEAKER_00"), (2, "SPEAKER_01"), (3, "SPEAKER_00")],
    )
    guy, aria = FakeEngine(), FakeEngine()
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(
            ("SPEAKER_00", "en-US-GuyNeural"),
            ("SPEAKER_01", "en-US-AriaNeural"),
        ),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )

    result = run_dub(
        options, engines={"en-US-GuyNeural": guy, "en-US-AriaNeural": aria}
    )

    report = report_of(result)
    assert [entry["voice"] for entry in report["cues"]] == [
        "en-US-GuyNeural",
        "en-US-AriaNeural",
        "en-US-GuyNeural",
    ]
    assert [entry["speaker"] for entry in report["cues"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    assert report["summary"]["speakers"] == 2
    assert {item["voice"]: item["cues"] for item in report["summary"]["voices"]} == {
        "en-US-GuyNeural": 2,
        "en-US-AriaNeural": 1,
    }
    assert len(guy.attempts) == 2
    assert len(aria.attempts) == 1
    assert report["input"]["speakers"]["sha256"]


def test_multi_voice_cache_is_per_voice(tmp_path) -> None:
    """同一段文字换个说话人音色必须重新合成，不能复用旧音色的缓存文件。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nSame line\n")
    speakers = write_speakers(tmp_path / "speakers.json", [(1, "SPEAKER_00")])
    guy, aria = FakeEngine(), FakeEngine()
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(("SPEAKER_00", "en-US-GuyNeural"),),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )
    run_dub(options, engines={"en-US-GuyNeural": guy, "en-US-AriaNeural": aria})

    # 换成另一个说话人的音色重跑（文本、字幕都没变）
    options_other = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(("SPEAKER_00", "en-US-AriaNeural"),),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )
    run_dub(options_other, engines={"en-US-GuyNeural": guy, "en-US-AriaNeural": aria})

    assert guy.attempts["Same line"] == 1
    assert aria.attempts["Same line"] == 1  # 没有被旧缓存挡住


def test_missing_speakers_file_raises(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", SRT_3)
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        speakers_path=tmp_path / "nope.json",
        retry_delay_s=0.0,
    )
    with pytest.raises(DubError, match="说话人归属文件不存在"):
        run_dub(options, engine=FakeEngine())


def test_unmapped_speaker_falls_back_to_default_voice_with_warning(tmp_path) -> None:
    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nOnly line\n")
    speakers = write_speakers(tmp_path / "speakers.json", [(1, "SPEAKER_07")])
    engine = FakeEngine()
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(("SPEAKER_00", "en-US-GuyNeural"),),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )

    result = run_dub(options, engine=engine)

    assert any("SPEAKER_07" in warning for warning in report_of(result)["warnings"])
    assert report_of(result)["cues"][0]["voice"] == "en-US-AriaNeural"


# ---------------------------------------------------------------- 音色下线降级（OPT-4）


class DeadVoiceEngine:
    """模拟被微软下线的音色：每次合成都抛 NoAudioReceived。"""

    def __init__(self, message: str = "NoAudioReceived: No audio was received") -> None:
        self.message = message
        self.calls = 0

    def synthesize(self, text: str, out_path: Path) -> None:  # noqa: ARG002
        self.calls += 1
        raise TtsError(self.message)


def test_dead_voice_is_downgraded_to_default(tmp_path) -> None:
    """OPT-4：多音色里某音色被下线 → 该条改用默认音色重合成，整片仍 ok。"""

    srt = write_srt(tmp_path / "en.srt", SRT_3)
    speakers = write_speakers(
        tmp_path / "speakers.json",
        [(1, "SPEAKER_00"), (2, "SPEAKER_01"), (3, "SPEAKER_01")],
    )
    dead = DeadVoiceEngine()
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(
            ("SPEAKER_00", "en-US-DavisNeural"),
            ("SPEAKER_01", "en-US-AriaNeural"),
        ),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )

    result = run_dub(
        options,
        engines={"en-US-AriaNeural": FakeEngine(), "en-US-DavisNeural": dead},
    )

    report = report_of(result)
    assert result.status == "ok"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["cues"] == 3
    downgrade = report["summary"]["voice_downgrades"][0]
    assert downgrade["from"] == "en-US-DavisNeural"
    assert downgrade["to"] == "en-US-AriaNeural"
    assert downgrade["cue"] == 1
    assert cue_reports(report)[1]["voice"] == "en-US-AriaNeural"
    assert any("不可用" in warning for warning in report["warnings"])
    # 首轮 3 次 + 二次补跑 3 次都没成 → 降级改用默认音色（降级那次走另一个引擎）
    assert dead.calls == 6


def test_transient_error_is_not_treated_as_dead_voice(tmp_path) -> None:
    """网络超时不是音色问题：不该降级（否则会悄悄换音色、掩盖真实故障）。"""

    srt = write_srt(tmp_path / "en.srt", SRT_3)
    speakers = write_speakers(
        tmp_path / "speakers.json",
        [(1, "SPEAKER_00"), (2, "SPEAKER_01"), (3, "SPEAKER_01")],
    )
    flaky = DeadVoiceEngine("edge-tts 合成失败：timed out")
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(
            ("SPEAKER_00", "en-US-GuyNeural"),
            ("SPEAKER_01", "en-US-AriaNeural"),
        ),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )

    result = run_dub(
        options,
        engines={"en-US-AriaNeural": FakeEngine(), "en-US-GuyNeural": flaky},
    )

    report = report_of(result)
    assert report["summary"]["voice_downgrades"] == []
    assert report["summary"]["failed"] == 1
    assert result.status == "partial"
    assert cue_reports(report)[1]["voice"] == "en-US-GuyNeural"


def test_downgrade_target_failure_keeps_failed_entry(tmp_path) -> None:
    """默认音色也失败时如实标 failed，不能无限降级。"""

    srt = write_srt(tmp_path / "en.srt", SRT_3)
    speakers = write_speakers(
        tmp_path / "speakers.json",
        [(1, "SPEAKER_00"), (2, "SPEAKER_01"), (3, "SPEAKER_01")],
    )
    dead = DeadVoiceEngine()
    also_dead = DeadVoiceEngine()
    options = DubOptions(
        srt=srt,
        out=tmp_path / "out",
        voice="en-US-AriaNeural",
        voice_by_speaker=(
            ("SPEAKER_00", "en-US-DavisNeural"),
            ("SPEAKER_01", "en-US-AriaNeural"),
        ),
        speakers_path=speakers,
        retry_delay_s=0.0,
    )

    result = run_dub(
        options,
        engines={"en-US-AriaNeural": also_dead, "en-US-DavisNeural": dead},
    )

    report = report_of(result)
    assert result.status == "partial"
    assert report["summary"]["failed"] == 3
    assert report["summary"]["voice_downgrades"][0]["cue"] == 1


def test_transient_failure_is_rescued_by_second_chance_pass(tmp_path) -> None:
    """OPT-5：首轮 3 次都因网络抖动失败 → 二次补跑救回，不算 failed。"""

    srt = write_srt(tmp_path / "en.srt", "1\n00:00:00,000 --> 00:00:02,000\nFlaky line\n")
    engine = FakeEngine(fail_texts=("Flaky line",), fail_first=3)
    result = dub(srt, tmp_path / "out", engine, retries=2)

    report = report_of(result)
    assert report["summary"]["failed"] == 0
    assert engine.attempts["Flaky line"] == 4  # 3 次首轮 + 第 4 次补跑成功
    assert cue_reports(report)[1]["action"] != "failed"
