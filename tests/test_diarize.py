"""多人声（M11）：说话人分离编排 + 字幕条归属 + 音色分派。

真模型不在这里跑（sherpa-onnx 只装在 spike/diarization/.venv，真实素材验证在
``work/multi-speaker/`` 的回归里）；这里测的是：

1. 分离结果 → 字幕条归属（重叠投票 / 中点兜底 / 孤岛平滑）；
2. worker 进程的输入输出契约（失败也要写 JSON，错误要可读）；
3. ``--speaker-voices`` 的两种写法与音色分派；
4. 模型/解释器缺失时的可操作报错。
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from cuesmith.diarize import (
    DiarizeError,
    DiarizeOptions,
    SpeakerTurn,
    assign_speakers,
    build_speaker_voice_map,
    default_threshold,
    find_diarization_python,
    find_models,
    find_vad_model,
    merge_minor_speakers,
    parse_speaker_voices,
    run_diarize,
    smooth_speakers,
)

SRT_TWO = (
    "1\n00:00:00,000 --> 00:00:02,000\nFirst speaker here\n"
    "\n"
    "2\n00:00:02,000 --> 00:00:05,000\nSecond speaker now\n"
)


def turn(start_ms: int, end_ms: int, speaker: str) -> SpeakerTurn:
    return SpeakerTurn(start_ms=start_ms, end_ms=end_ms, speaker=speaker)


# ---------------------------------------------------------------- 归属


def test_assigns_by_longest_overlap() -> None:
    turns = [turn(0, 1200, "SPEAKER_00"), turn(1200, 2500, "SPEAKER_01")]

    speakers = assign_speakers([(0, 1000), (1000, 2000)], turns)

    assert speakers == ["SPEAKER_00", "SPEAKER_01"]


def test_tie_prefers_previous_speaker() -> None:
    """两条分段各占一半时，沿用上一条的说话人，避免边界抖动换声。"""

    turns = [turn(0, 1000, "SPEAKER_01"), turn(1000, 2000, "SPEAKER_00")]
    timings = [(500, 1500), (500, 1500)]

    assert assign_speakers(timings, turns) == ["SPEAKER_01", "SPEAKER_01"]


def test_falls_back_to_nearest_midpoint_when_no_overlap() -> None:
    turns = [turn(0, 1000, "SPEAKER_00"), turn(5000, 6000, "SPEAKER_01")]

    # 6000–7000 与两段都无重叠，中点 6500 离 SPEAKER_01 更近
    assert assign_speakers([(6000, 7000)], turns) == ["SPEAKER_01"]


def test_empty_turns_returns_blank() -> None:
    assert assign_speakers([(0, 1000), (1000, 2000)], []) == ["", ""]


def test_smooths_short_speaker_island() -> None:
    speakers = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    timings = [(0, 2000), (2000, 3000), (3000, 5000)]

    assert smooth_speakers(speakers, timings) == ["SPEAKER_00"] * 3


def test_keeps_long_speaker_change() -> None:
    """插话超过 2.5 秒就不是抖动，保留真实换人。"""

    speakers = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    timings = [(0, 2000), (2000, 6000), (6000, 8000)]

    assert smooth_speakers(speakers, timings) == speakers


def test_smooth_requires_matching_neighbours() -> None:
    speakers = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]

    assert smooth_speakers(speakers) == speakers


# ---------------------------------------------------------------- 音色


def test_parse_explicit_speaker_voices() -> None:
    explicit, pool = parse_speaker_voices(
        "SPEAKER_00=zh-CN-YunxiNeural, SPEAKER_01=zh-CN-XiaoxiaoNeural"
    )

    assert explicit == {"SPEAKER_00": "zh-CN-YunxiNeural", "SPEAKER_01": "zh-CN-XiaoxiaoNeural"}
    assert pool == ()


def test_parse_voice_pool() -> None:
    explicit, pool = parse_speaker_voices("en-US-GuyNeural, en-US-AriaNeural")

    assert explicit == {}
    assert pool == ("en-US-GuyNeural", "en-US-AriaNeural")


def test_parse_rejects_mixed_forms() -> None:
    with pytest.raises(DiarizeError, match="不能混用"):
        parse_speaker_voices("SPEAKER_00=en-US-GuyNeural,en-US-AriaNeural")


def test_parse_empty_returns_nothing() -> None:
    assert parse_speaker_voices(None) == ({}, ())
    assert parse_speaker_voices("  ") == ({}, ())


def test_voice_map_uses_explicit_then_pool_then_default() -> None:
    mapping = build_speaker_voice_map(
        ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
        explicit={"SPEAKER_01": "zh-CN-YunjianNeural"},
        pool=("zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural"),
        default="zh-CN-XiaoxiaoNeural",
    )

    assert mapping == {
        "SPEAKER_00": "zh-CN-YunxiNeural",
        "SPEAKER_01": "zh-CN-YunjianNeural",
        "SPEAKER_02": "zh-CN-XiaoxiaoNeural",
    }


def test_voice_map_falls_back_to_default() -> None:
    mapping = build_speaker_voice_map(["SPEAKER_00"], default="en-US-AriaNeural")

    assert mapping == {"SPEAKER_00": "en-US-AriaNeural"}


# ---------------------------------------------------------------- 环境解析


def test_find_models_prefers_segmentation_and_requested_embedding(tmp_path, monkeypatch) -> None:
    import cuesmith.diarize as module

    models = tmp_path / "models"
    seg = models / "sherpa-onnx-pyannote-segmentation-3-0"
    seg.mkdir(parents=True)
    (seg / "model.onnx").write_bytes(b"x")
    (models / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx").write_bytes(b"y")
    (models / "nemo_en_titanet_small.onnx").write_bytes(b"z")
    monkeypatch.setattr(module, "_models_dir", lambda: models)
    monkeypatch.delenv("CUESMITH_DIARIZATION_SEGMENTATION", raising=False)
    monkeypatch.delenv("CUESMITH_DIARIZATION_EMBEDDING", raising=False)

    segmentation, embedding = find_models(language="zh")

    assert segmentation.name == "model.onnx"
    assert "3dspeaker" in embedding.name
    _, english = find_models(language="en")
    assert "titanet" in english.name


def test_find_models_missing_hint(tmp_path, monkeypatch) -> None:
    import cuesmith.diarize as module

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(module, "_models_dir", lambda: empty)
    monkeypatch.delenv("CUESMITH_DIARIZATION_SEGMENTATION", raising=False)

    with pytest.raises(DiarizeError, match="分割模型"):
        find_models()


def test_find_models_ignores_vad_model(tmp_path, monkeypatch) -> None:
    """VAD 模型不是声纹模型：只剩 segmentation + vad 时不能把它当 embedding。"""

    import cuesmith.diarize as module

    models = tmp_path / "models"
    seg = models / "sherpa-onnx-pyannote-segmentation-3-0"
    seg.mkdir(parents=True)
    (seg / "model.onnx").write_bytes(b"x")
    vad = models / "vad"
    vad.mkdir()
    (vad / "silero_vad.onnx").write_bytes(b"v")
    monkeypatch.setattr(module, "_models_dir", lambda: models)
    monkeypatch.delenv("CUESMITH_DIARIZATION_SEGMENTATION", raising=False)
    monkeypatch.delenv("CUESMITH_DIARIZATION_EMBEDDING", raising=False)

    with pytest.raises(DiarizeError, match="声纹模型"):
        find_models()


def test_find_vad_model_resolution(tmp_path, monkeypatch) -> None:
    """OPT-6：显式 > 环境变量 > 项目默认目录；没装返回 None（不是错误）。"""

    import cuesmith.diarize as module

    models = tmp_path / "models"
    monkeypatch.setattr(module, "_models_dir", lambda: models)
    monkeypatch.delenv("CUESMITH_DIARIZATION_VAD", raising=False)
    assert find_vad_model() is None

    default = models / "vad" / "silero_vad.onnx"
    default.parent.mkdir(parents=True)
    default.write_bytes(b"v")
    assert find_vad_model() == default
    assert find_vad_model(str(default)) == default

    with pytest.raises(DiarizeError, match="VAD 模型文件不存在"):
        find_vad_model(tmp_path / "nope.onnx")

    other = tmp_path / "other.onnx"
    other.write_bytes(b"o")
    monkeypatch.setenv("CUESMITH_DIARIZATION_VAD", str(other))
    assert find_vad_model() == other


def test_find_python_reports_actionable_hint() -> None:
    def runner(command):  # noqa: ANN001
        return subprocess.CompletedProcess(command, 1, "", "ModuleNotFoundError: sherpa_onnx")

    with pytest.raises(DiarizeError, match="sherpa-onnx"):
        find_diarization_python(sys.executable, runner=runner)


# ---------------------------------------------------------------- worker 契约


def worker_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "sample_rate": 16000,
        "duration_s": 5.0,
        "segments": [
            {"start": 0.0, "end": 2.0, "speaker": 1},
            {"start": 2.0, "end": 5.0, "speaker": 0},
        ],
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    def __init__(self, payload: dict[str, object], *, python_ok: bool = True) -> None:
        self.payload = payload
        self.python_ok = python_ok
        self.calls: list[list[str]] = []

    def __call__(self, command):
        self.calls.append(list(command))
        if len(command) >= 3 and command[1] == "-c":
            stderr = "" if self.python_ok else "ModuleNotFoundError: No module named 'sherpa_onnx'"
            return subprocess.CompletedProcess(command, 0 if self.python_ok else 1, "", stderr)
        output = command[command.index("--output") + 1]
        Path(output).write_text(json.dumps(self.payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")


def make_extractor(duration_ms: int = 5000):
    def extract(source: Path, out: Path) -> None:  # noqa: ARG001
        out.parent.mkdir(parents=True, exist_ok=True)
        frames = int(16000 * duration_ms / 1000)
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * frames)

    return extract


def make_options(tmp_path: Path, runner, **overrides) -> DiarizeOptions:
    seg = tmp_path / "seg.onnx"
    emb = tmp_path / "emb.onnx"
    seg.write_bytes(b"seg")
    emb.write_bytes(b"emb")
    defaults = dict(
        src=tmp_path / "in.mp4",
        out=tmp_path / "out",
        python=sys.executable,
        segmentation_model=seg,
        embedding_model=emb,
        vad=False,  # VAD 默认关：单测不依赖本机是否装了 silero_vad.onnx
        runner=runner,
        extractor=make_extractor(),
    )
    defaults.update(overrides)
    options = DiarizeOptions(**defaults)
    if not options.src.is_file():
        Path(options.src).write_bytes(b"fake video")
    return options


def test_run_diarize_writes_report_and_assignments(tmp_path) -> None:
    srt = tmp_path / "tgt.srt"
    srt.write_text(SRT_TWO, encoding="utf-8", newline="\n")

    result = run_diarize(make_options(tmp_path, FakeRunner(worker_payload()), srt=srt, language="en"))

    assert result.status == "ok"
    assert result.exit_code == 0
    # 按出场顺序归一：先出现的 speaker=1 → SPEAKER_00
    assert [item.speaker for item in result.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.speakers_path is not None and result.speakers_path.is_file()
    assignment = json.loads(result.speakers_path.read_text(encoding="utf-8"))
    assert assignment["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert [item["speaker"] for item in assignment["cues"]] == ["SPEAKER_00", "SPEAKER_01"]
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["summary"]["speakers"] == 2
    assert report["summary"]["turns"] == 2
    assert report["summary"]["assigned_cues"] == 2
    assert report["config"]["num_speakers_requested"] is None
    assert report["segments"][0] == {"start_ms": 0, "end_ms": 2000, "speaker": "SPEAKER_00"}


def test_run_diarize_passes_num_speakers_to_worker(tmp_path) -> None:
    runner = FakeRunner(worker_payload())

    run_diarize(make_options(tmp_path, runner, num_speakers=2))

    worker_call = runner.calls[-1]
    assert worker_call[worker_call.index("--num-speakers") + 1] == "2"


def test_run_diarize_enables_vad_when_model_present(tmp_path) -> None:
    """OPT-6：VAD 模型就绪时把参数传给 worker，并把预过滤统计写进报告。"""

    vad = tmp_path / "silero_vad.onnx"
    vad.write_bytes(b"v")
    runner = FakeRunner(
        worker_payload(
            vad={
                "enabled": True,
                "segments": 2,
                "speech_s": 4.0,
                "speech_pct": 80.0,
                "removed_s": 1.0,
                "elapsed_s": 0.2,
            }
        )
    )

    result = run_diarize(make_options(tmp_path, runner, vad=True, vad_model=vad))

    call = runner.calls[-1]
    assert call[call.index("--vad-model") + 1] == str(vad)
    assert call[call.index("--vad-threshold") + 1] == "0.5"
    assert "--vad-min-silence" in call and "--vad-max-speech" in call
    report = result.report
    assert report["config"]["vad"]["enabled"] is True
    assert report["config"]["vad"]["requested"] is True
    assert report["summary"]["vad"]["speech_pct"] == 80.0
    segments_payload = json.loads(
        (tmp_path / "out" / "segments.json").read_text(encoding="utf-8")
    )
    assert segments_payload["summary"]["vad"]["speech_s"] == 4.0


def test_run_diarize_vad_model_missing_is_skipped(tmp_path, monkeypatch) -> None:
    """没装 VAD 模型时安静降级：不传参、报告写 skipped，链路照跑。"""

    import cuesmith.diarize as module

    monkeypatch.setattr(module, "find_vad_model", lambda explicit=None: None)
    runner = FakeRunner(worker_payload())

    result = run_diarize(make_options(tmp_path, runner, vad=True))

    call = runner.calls[-1]
    assert "--vad-model" not in call
    assert result.status == "ok"
    assert result.report["config"]["vad"] == {
        "requested": True,
        "enabled": False,
        "model": None,
        "threshold": 0.5,
        "skipped": "model-missing",
        "mode": None,
    }


def test_run_diarize_warns_when_vad_finds_no_speech(tmp_path) -> None:
    vad = tmp_path / "silero_vad.onnx"
    vad.write_bytes(b"v")
    payload = worker_payload(
        segments=[],
        vad={
            "enabled": True,
            "skipped": "no-speech",
            "segments": 0,
            "speech_s": 0.0,
            "speech_pct": 0.0,
            "removed_s": 5.0,
            "elapsed_s": 0.1,
        },
    )

    result = run_diarize(make_options(tmp_path, FakeRunner(payload), vad=True, vad_model=vad))

    assert result.status == "empty"
    assert any("VAD 没有检测到语音" in item for item in result.report["warnings"])


def test_worker_compacts_speech_and_maps_back() -> None:
    """OPT-6：只拼语音段送模型，再把分段映射回原时间轴（跨语音段要拆开）。"""

    import numpy as np

    from cuesmith import diarize_worker as worker

    sr = 16000
    samples = (np.arange(sr * 3, dtype=np.float32) / sr).astype(np.float32)
    intervals = [(0, sr), (2 * sr, 3 * sr)]  # 0–1s、2–3s 是语音，中间 1 秒是音乐

    compact, layout = worker._compact_speech(samples, sr, intervals)

    assert compact.shape[0] == 2 * sr + int(round(0.15 * sr))
    assert layout == [(0.0, 1.0, 0.0, 1.0), (1.15, 2.15, 2.0, 3.0)]

    mapped = worker._map_segments_back(
        [{"start": 0.2, "end": 0.6, "speaker": 0}], layout
    )
    assert mapped == [{"start": 0.2, "end": 0.6, "speaker": 0}]

    # 跨 chunk 的分段要拆成两段，且都不覆盖被丢掉的音乐区间
    mapped = worker._map_segments_back(
        [{"start": 0.8, "end": 1.4, "speaker": 1}], layout
    )
    assert mapped == [
        {"start": 0.8, "end": 1.0, "speaker": 1},
        {"start": 2.0, "end": 2.25, "speaker": 1},
    ]

    # 没有语音段时原样返回，不拼接
    same, empty_layout = worker._compact_speech(samples, sr, [])
    assert empty_layout == []
    assert same.shape == samples.shape


# ---------------------------------------------------------------- OPT-7 二次聚类


def _unit(*values: float) -> list[float]:
    """构造单位向量（测试用短向量）。"""

    import math

    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def test_worker_merge_embeddings_merges_similar_clusters() -> None:
    """OPT-7：两个簇平均成对相似度高 → 合并；低 → 保留。"""

    import numpy as np

    from cuesmith import diarize_worker as worker

    embeddings = np.array(
        [_unit(1, 0, 0), _unit(0.98, 0.1, 0), _unit(0, 1, 0)], dtype=np.float64
    )
    spans = [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0)]
    labels = [0, 1, 2]
    within = [0.99, float("nan"), 0.99]  # s_same=0.99 → tau=0.55*0.99

    merged = worker._merge_embeddings(embeddings, spans, labels, within, alpha=0.55)

    assert merged["clusters_in"] == 3
    assert merged["clusters_out"] == 2
    assert merged["merge_sims"] == [pytest.approx(0.999, abs=1e-2)]
    assert merged["s_same"] == pytest.approx(0.99)
    assert merged["tau"] == pytest.approx(0.5445)
    # 前两段（相似）并成一簇，第三段保持独立
    assert list(merged["labels"]) == [0, 0, 1]

    # 全部低于阈值 → 不合并
    far = np.array([_unit(1, 0, 0), _unit(0, 1, 0)], dtype=np.float64)
    kept = worker._merge_embeddings(far, [(0.0, 5.0), (5.0, 10.0)], [0, 1], [0.99, 0.99])
    assert kept["clusters_out"] == 2
    assert kept["merge_sims"] == []


def test_worker_merge_embeddings_single_speaker_collapses_to_one() -> None:
    """OPT-7：同一人被切碎成很多 turn（每段独立簇）时能并回 1 人（60s 单人场景）。"""

    import numpy as np

    from cuesmith import diarize_worker as worker

    count = 16
    embeddings = np.array(
        [_unit(1.0, 0.02 * index, 0.0) for index in range(count)], dtype=np.float64
    )
    spans = [(index * 4.0, (index + 1) * 4.0) for index in range(count)]
    labels = list(range(count))
    merged = worker._merge_embeddings(embeddings, spans, labels, [0.6] * count, alpha=0.70)

    assert merged["clusters_in"] == count
    assert merged["clusters_out"] == 1
    assert set(int(value) for value in merged["labels"]) == {0}


def test_worker_windows_and_relabel_helpers() -> None:
    import numpy as np

    from cuesmith import diarize_worker as worker

    assert worker._windows_in_span(0.0, 0.5) == []  # 短于 min_win 丢弃
    assert worker._windows_in_span(0.0, 0.8) == [(0.0, 0.8)]
    windows = worker._windows_in_span(0.0, 3.2)
    assert windows[0] == (0.0, 1.5)
    assert windows[-1][1] == pytest.approx(3.2)

    labels = worker._relabel_by_first_seen(
        np.array([2, 2, 0, 0, 1]), [(5, 6), (5.5, 6), (0, 1), (0.5, 1), (2, 3)]
    )
    assert list(labels) == [2, 2, 0, 0, 1]
    assert worker._same_speaker_reference([float("nan"), float("nan")]) == pytest.approx(0.6)
    assert worker._same_speaker_reference([0.3, 0.5, 0.7]) == pytest.approx(0.5)


def test_run_diarize_passes_clustering_mode_and_alpha(tmp_path) -> None:
    """auto + 自动判人数 → embedding 二次聚类；显式人数 → sherpa。"""

    runner = FakeRunner(worker_payload())
    run_diarize(make_options(tmp_path, runner))

    call = runner.calls[-1]
    assert call[call.index("--clustering") + 1] == "embedding"
    # make_options 默认 vad=False → 原始音频默认 alpha 0.55
    assert call[call.index("--merge-alpha") + 1] == "0.55"
    assert call[call.index("--merge-min-speech") + 1] == "3"

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    runner2 = FakeRunner(worker_payload())
    run_diarize(make_options(explicit_dir, runner2, num_speakers=2))
    call = runner2.calls[-1]
    assert call[call.index("--clustering") + 1] == "sherpa"


def test_run_diarize_merge_alpha_follows_vad_mode(tmp_path) -> None:
    """VAD 拼接音频用 0.70（拼接会抬高跨簇相似度，0.55 会过并）。"""

    vad = tmp_path / "silero_vad.onnx"
    vad.write_bytes(b"v")
    runner = FakeRunner(worker_payload())

    result = run_diarize(make_options(tmp_path, runner, vad=True, vad_model=vad))

    call = runner.calls[-1]
    assert call[call.index("--merge-alpha") + 1] == "0.7"
    assert result.report["config"]["merge_alpha"] == pytest.approx(0.7)
    assert result.report["config"]["merge_alpha_source"] == "vad-default"

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    explicit = run_diarize(
        make_options(
            explicit_dir, FakeRunner(worker_payload()), vad=True, vad_model=vad, merge_alpha=0.6
        )
    )
    assert explicit.report["config"]["merge_alpha"] == pytest.approx(0.6)
    assert explicit.report["config"]["merge_alpha_source"] == "explicit"


def test_run_diarize_reports_second_clustering(tmp_path) -> None:
    """worker 的二次聚类结果进报告，并如实告警"并了几人"。"""

    vad = tmp_path / "silero_vad.onnx"
    vad.write_bytes(b"v")
    payload = worker_payload(
        clustering={
            "mode": "embedding",
            "alpha": 0.7,
            "turns": 3,
            "clusters_in": 3,
            "clusters_out": 2,
            "s_same": 0.55,
            "tau": 0.385,
            "merge_sims": [0.9],
        }
    )

    result = run_diarize(
        make_options(tmp_path, FakeRunner(payload), vad=True, vad_model=vad)
    )

    clustering = result.report["summary"]["clustering"]
    assert clustering["clusters_out"] == 2
    assert result.report["config"]["clustering_effective"] == "embedding"
    assert any("3 → 2 人" in item for item in result.report["warnings"])


def test_options_clustering_resolution(tmp_path) -> None:
    """OPT-7：auto 只在自动判人数时走 embedding；显式人数走 sherpa。"""

    options = DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o")
    assert options.resolved_clustering == "embedding"
    assert options.merge_alpha_for(vad_enabled=False) == pytest.approx(0.55)
    assert options.merge_alpha_for(vad_enabled=True) == pytest.approx(0.70)

    explicit = DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", num_speakers=2)
    assert explicit.resolved_clustering == "sherpa"

    forced = DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", clustering="sherpa")
    assert forced.resolved_clustering == "sherpa"

    with pytest.raises(DiarizeError):
        DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", clustering="magic").validate()
    with pytest.raises(DiarizeError):
        DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", merge_alpha=1.5).validate()


def test_run_diarize_single_speaker_warns(tmp_path) -> None:
    payload = worker_payload(
        segments=[{"start": 0.0, "end": 5.0, "speaker": 0}]
    )

    result = run_diarize(make_options(tmp_path, FakeRunner(payload)))

    assert result.report["summary"]["speakers"] == 1
    assert any("1 个说话人" in warning for warning in result.report["warnings"])


def test_run_diarize_empty_is_invalid_but_reported(tmp_path) -> None:
    result = run_diarize(make_options(tmp_path, FakeRunner(worker_payload(segments=[]))))

    assert result.exit_code == 2
    assert result.status == "empty"
    assert any("没有识别出任何说话人" in warning for warning in result.report["warnings"])


def test_run_diarize_reports_worker_failure(tmp_path) -> None:
    runner = FakeRunner({"ok": False, "error": "model load failed", "traceback": "..."})

    with pytest.raises(DiarizeError, match="model load failed"):
        run_diarize(make_options(tmp_path, runner))


def test_run_diarize_requires_existing_source(tmp_path) -> None:
    with pytest.raises(DiarizeError, match="不存在"):
        run_diarize(DiarizeOptions(src=tmp_path / "nope.mp4", out=tmp_path / "out"))


def test_run_diarize_requires_existing_srt(tmp_path) -> None:
    with pytest.raises(DiarizeError, match="字幕文件不存在"):
        run_diarize(
            make_options(tmp_path, FakeRunner(worker_payload()), srt=tmp_path / "nope.srt")
        )


def test_run_diarize_keeps_raw_worker_payload_when_asked(tmp_path) -> None:
    run_diarize(make_options(tmp_path, FakeRunner(worker_payload()), keep_raw=True))

    assert (tmp_path / "out" / "diarization-worker.json").is_file()


def test_options_validate_rejects_bad_values(tmp_path) -> None:
    with pytest.raises(DiarizeError):
        DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", num_speakers=-1).validate()
    with pytest.raises(DiarizeError):
        DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", threshold=1.5).validate()
    with pytest.raises(DiarizeError):
        DiarizeOptions(src=tmp_path / "a.mp4", out=tmp_path / "o", vad_threshold=1.5).validate()


# ---------------------------------------------------------------- OPT-1 短簇合并


def test_default_threshold_by_language() -> None:
    """英文声纹模型偏碎：默认 0.95；中文 3D-Speaker 0.8 就够；未知语言退回 0.8。"""

    assert default_threshold("en") == pytest.approx(0.95)
    assert default_threshold("EN") == pytest.approx(0.95)
    assert default_threshold("zh") == pytest.approx(0.8)
    assert default_threshold(None) == pytest.approx(0.8)
    assert default_threshold("ja") == pytest.approx(0.8)


def test_merge_minor_clusters_into_neighbours() -> None:
    turns = [
        turn(0, 10_000, "SPEAKER_00"),
        turn(10_000, 12_000, "SPEAKER_01"),  # 2s 短簇（<3s）
        turn(12_000, 20_000, "SPEAKER_02"),
    ]

    merge = merge_minor_speakers(turns)

    assert [item.speaker for item in merge.turns] == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"]
    assert merge.speakers_before == ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02")
    # 合并后按出场顺序重新编号，不留 SPEAKER_02 空洞
    assert merge.speakers_after == ("SPEAKER_00", "SPEAKER_01")
    assert merge.changed
    entry = merge.merged[0]
    assert entry["speaker"] == "SPEAKER_01"
    assert entry["into"] == "SPEAKER_00"
    assert entry["speech_s"] == 2.0
    assert entry["turns"] == 1


def test_merge_keeps_clusters_above_threshold() -> None:
    turns = [turn(0, 5000, "SPEAKER_00"), turn(5000, 12_000, "SPEAKER_01")]

    merge = merge_minor_speakers(turns)

    assert not merge.changed
    assert merge.skipped is None
    assert merge.speakers_after == ("SPEAKER_00", "SPEAKER_01")
    assert merge.turns == tuple(turns)


def test_merge_by_share_not_only_duration() -> None:
    """语音过 3s 但占比 <1%（长片里的广告人声）同样算碎片，并进主簇。"""

    turns = [
        turn(0, 250_000, "SPEAKER_00"),
        turn(250_000, 253_500, "SPEAKER_01"),  # 3.5s（够 3s 门槛），占比仅 0.5%
        turn(253_500, 653_500, "SPEAKER_02"),
    ]

    merge = merge_minor_speakers(turns)

    assert merge.changed
    entry = merge.merged[0]
    assert entry["speaker"] == "SPEAKER_01"
    assert entry["speech_s"] == 3.5  # 时长本身够，靠占比规则并掉
    assert entry["share_pct"] < 1.0
    assert merge.speakers_after == ("SPEAKER_00", "SPEAKER_01")


def test_merge_preserves_result_when_only_one_main_cluster() -> None:
    """宁多不并：合并后只剩 1 人时保留原结果（可能真是一人分饰/停顿被拆）。"""

    turns = [turn(0, 20_000, "SPEAKER_00"), turn(20_000, 21_000, "SPEAKER_01")]

    merge = merge_minor_speakers(turns)

    assert merge.skipped == "single-main"
    assert not merge.changed
    assert merge.speakers_after == ("SPEAKER_00", "SPEAKER_01")
    assert merge.turns == tuple(turns)


def test_merge_can_be_disabled() -> None:
    """--keep-minor-speakers：两个阈值都给 0 = 关闭合并，原始簇全保留。"""

    turns = [turn(0, 10_000, "SPEAKER_00"), turn(10_000, 10_500, "SPEAKER_01")]

    merge = merge_minor_speakers(turns, min_speech_s=0.0, min_share=0.0)

    assert merge.skipped == "disabled"
    assert not merge.changed
    assert merge.speakers_after == ("SPEAKER_00", "SPEAKER_01")


def test_merge_picks_nearest_main_cluster_by_time_gap() -> None:
    """离右边主簇 0.2s、离左边 3s：并到右边（按时间间隙，不按序号）。"""

    turns = [
        turn(0, 5000, "SPEAKER_00"),
        turn(8000, 9000, "SPEAKER_01"),
        turn(9200, 15_000, "SPEAKER_02"),
    ]

    merge = merge_minor_speakers(turns)

    assert merge.changed
    # 目标簇 SPEAKER_02 合并后重编号成 SPEAKER_01
    assert merge.merged[0]["into"] == "SPEAKER_01"
    assert [item.speaker for item in merge.turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01"]


def test_run_diarize_merges_short_clusters_and_reports(tmp_path) -> None:
    srt = tmp_path / "tgt.srt"
    srt.write_text(SRT_TWO, encoding="utf-8", newline="\n")
    payload = worker_payload(
        segments=[
            {"start": 0.0, "end": 10.0, "speaker": 0},
            {"start": 10.0, "end": 10.6, "speaker": 1},
            {"start": 10.6, "end": 20.0, "speaker": 2},
        ]
    )

    result = run_diarize(make_options(tmp_path, FakeRunner(payload), language="en", srt=srt))

    summary = result.report["summary"]
    assert summary["speakers"] == 2
    assert summary["speakers_raw"] == 3
    assert summary["merged_clusters"][0]["speaker"] == "SPEAKER_01"
    assert summary["turns"] == 3  # 段数不变，只改归属
    assert result.report["config"]["threshold"] == pytest.approx(0.95)
    assert result.report["config"]["threshold_source"] == "language-default"
    assert any("短簇" in warning for warning in result.report["warnings"])
    assignment = json.loads(result.speakers_path.read_text(encoding="utf-8"))
    assert assignment["speakers"] == ["SPEAKER_00", "SPEAKER_01"]


def test_run_diarize_threshold_defaults_by_language_and_explicit_wins(tmp_path) -> None:
    chinese = FakeRunner(worker_payload())
    zh_result = run_diarize(make_options(tmp_path, chinese, language="zh"))
    zh_call = chinese.calls[-1]
    assert zh_call[zh_call.index("--threshold") + 1] == "0.8"
    assert zh_result.report["config"]["threshold"] == pytest.approx(0.8)

    explicit = FakeRunner(worker_payload())
    en_result = run_diarize(make_options(tmp_path, explicit, language="en", threshold=0.7))
    en_call = explicit.calls[-1]
    assert en_call[en_call.index("--threshold") + 1] == "0.7"
    assert en_result.report["config"]["threshold"] == pytest.approx(0.7)
    assert en_result.report["config"]["threshold_source"] == "explicit"


def test_run_diarize_keep_minor_speakers_disables_merge(tmp_path) -> None:
    payload = worker_payload(
        segments=[
            {"start": 0.0, "end": 10.0, "speaker": 0},
            {"start": 10.0, "end": 10.6, "speaker": 1},
            {"start": 10.6, "end": 20.0, "speaker": 2},
        ]
    )
    options = make_options(
        tmp_path,
        FakeRunner(payload),
        language="en",
        min_speaker_speech_s=0.0,
        min_speaker_share=0.0,
    )

    result = run_diarize(options)

    assert result.report["summary"]["speakers"] == 3
    assert result.report["summary"]["merged_clusters"] == []
    assert result.report["summary"]["cluster_merge_skipped"] == "disabled"


# ---------------------------------------------------------------- 分段/归属分离（OPT-5）


def test_write_and_load_segments_round_trip(tmp_path) -> None:
    from cuesmith.diarize import load_segments, write_segments

    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake")
    turns = [turn(0, 1000, "SPEAKER_00"), turn(1000, 2000, "SPEAKER_01")]

    path = write_segments(
        tmp_path / "segments.json",
        turns=turns,
        source_path=src,
        duration_s=2.0,
        config={"language": "en", "threshold": 0.95},
        summary={"speakers": 2},
        warnings=["w"],
    )
    loaded, payload = load_segments(path)

    assert [item.speaker for item in loaded] == ["SPEAKER_00", "SPEAKER_01"]
    assert payload["source"]["sha256"]
    assert payload["config"]["threshold"] == 0.95


def test_attach_speakers_writes_assignments_and_report(tmp_path) -> None:
    from cuesmith.diarize import attach_speakers

    srt = tmp_path / "src.srt"
    srt.write_text(SRT_TWO, encoding="utf-8", newline="\n")
    turns = [turn(0, 2000, "SPEAKER_00"), turn(2000, 5000, "SPEAKER_01")]
    base = {
        "input": {"path": "x.mp4", "bytes": 4, "sha256": "abc", "duration_s": 5.0},
        "config": {"language": "en", "threshold": 0.95},
        "summary": {"speakers": 2, "turns": 2, "speech_s": 5.0},
        "warnings": [],
    }

    speakers_path, report = attach_speakers(turns, srt, tmp_path / "diarize", report=base)

    assignment = json.loads(speakers_path.read_text(encoding="utf-8"))
    assert [item["speaker"] for item in assignment["cues"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert report["input"]["srt"]["sha256"]
    assert report["summary"]["cues"] == 2
    assert report["summary"]["assigned_cues"] == 2
    assert report["summary"]["speaker_stats"][0]["cues"] == 1
    assert report["segments"][0]["speaker"] == "SPEAKER_00"
    assert (tmp_path / "diarize" / "diarize.json").is_file()


def test_attach_speakers_with_no_turns_is_empty(tmp_path) -> None:
    from cuesmith.diarize import attach_speakers

    srt = tmp_path / "src.srt"
    srt.write_text(SRT_TWO, encoding="utf-8", newline="\n")

    speakers_path, report = attach_speakers([], srt, tmp_path / "diarize")

    assignment = json.loads(speakers_path.read_text(encoding="utf-8"))
    assert assignment["speakers"] == []
    assert [item["speaker"] for item in assignment["cues"]] == ["", ""]
    assert report["status"] == "empty"
    assert any("没有可归属" in warning for warning in report["warnings"])
