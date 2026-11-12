"""ASR 直连（第 ①② 层）：词级时间戳 + 热词偏置 + 按语义切条。

底座的段级 SRT 有两个硬伤，是 en→zh 质量问题的上游根因：
1. 切句只能靠"段尾有没有句号"，12 秒上限会把句子拦腰砍断（M4 实测 cue 以
   "and it could" 结尾）；
2. 专有名词没有热词偏置，识别错的名词后面的翻译全错。

这里测的是"拿到词级时间戳之后怎么切"以及 worker 进程的输入输出契约，不跑真模型
（真模型跑在底座 venv 里，回归脚本里有单独的真实素材链路）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cuesmith.asr import (
    AsrError,
    AsrOptions,
    find_model,
    find_python,
    run_asr,
    segment_words,
)


def word(start: float, end: float, text: str) -> dict[str, object]:
    return {"start": start, "end": end, "word": text}


def words(*items: tuple[float, float, str]) -> list[dict[str, object]]:
    return [word(start, end, text) for start, end, text in items]


# ---------------------------------------------------------------- 切条


def test_splits_at_sentence_end() -> None:
    segments = segment_words(
        words((0, 0.3, " Hello"), (0.3, 0.8, " world."), (1.2, 1.5, " Second"), (1.5, 2.0, " one."))
    )

    assert [segment.text for segment in segments] == ["Hello world.", "Second one."]
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 800
    assert segments[1].start_ms == 1200


def test_splits_at_long_pause() -> None:
    segments = segment_words(
        words((0, 0.5, " Keep"), (0.5, 1.0, " going"), (2.0, 2.5, " after"), (2.5, 3.0, " pause.")),
        pause_ms=500,
    )

    assert [segment.text for segment in segments] == ["Keep going", "After pause."]


def test_respects_max_duration() -> None:
    items = [(float(index), float(index) + 0.9, f" w{index}") for index in range(12)]
    segments = segment_words(words(*items), max_duration_ms=4000, pause_ms=9999)

    assert segments
    assert all(segment.end_ms - segment.start_ms <= 4000 for segment in segments)
    assert "".join(segment.text for segment in segments).replace(" ", "").casefold() == "".join(
        f"w{index}" for index in range(12)
    )


def test_respects_max_chars() -> None:
    items = [(float(index), float(index) + 0.5, f" word{index}") for index in range(10)]
    segments = segment_words(words(*items), max_chars=20, pause_ms=9999)

    assert len(segments) > 1
    assert all(len(segment.text) <= 20 for segment in segments)


def test_merges_too_short_segment_into_next() -> None:
    segments = segment_words(
        words((0, 0.15, " Oh."), (0.4, 1.8, " Something"), (1.8, 2.4, " else.")),
        min_duration_ms=400,
    )

    assert len(segments) == 1
    assert segments[0].text == "Oh. Something else."


def test_drops_non_speech_segments() -> None:
    segments = segment_words(
        words((0, 0.5, " [Music]"), (2.0, 2.4, " ♪"), (3.0, 4.0, " Real"), (4.0, 4.6, " speech."))
    )

    assert [segment.text for segment in segments] == ["Real speech."]


def test_empty_words_returns_nothing() -> None:
    assert segment_words([]) == []


def test_zero_length_word_gets_minimum_duration() -> None:
    segments = segment_words(words((1.0, 1.0, " Hi.")))

    assert segments[0].end_ms > segments[0].start_ms


def test_capitalizes_sentence_start_and_pronoun_i() -> None:
    segments = segment_words(words((0, 0.4, " you"), (0.4, 0.8, " and i"), (0.8, 1.2, " agree.")))

    assert segments[0].text == "You and I agree."


# ---------------------------------------------------------------- worker 契约


MODEL_WORDS = words((0.0, 0.4, " Hello"), (0.4, 0.9, " world."), (1.5, 2.0, " Bye."))


def worker_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "language": "en",
        "language_probability": 0.99,
        "duration": 10.0,
        "duration_after_vad": 8.0,
        "words": MODEL_WORDS,
        "segments": [
            {"start": 0.0, "end": 0.9, "text": " Hello world.", "words": MODEL_WORDS[:2]},
            {"start": 1.5, "end": 2.0, "text": " Bye.", "words": MODEL_WORDS[2:]},
        ],
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    """模拟两个子进程：``python -c import faster_whisper`` 和 worker。"""

    def __init__(self, payload: dict[str, object], *, python_ok: bool = True):
        self.payload = payload
        self.python_ok = python_ok
        self.calls: list[list[str]] = []

    def __call__(self, command):
        self.calls.append(list(command))
        if len(command) >= 3 and command[1] == "-c":
            stderr = "" if self.python_ok else "ModuleNotFoundError: No module named 'faster_whisper'"
            return subprocess.CompletedProcess(command, 0 if self.python_ok else 1, "", stderr)
        output = command[command.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(self.payload, handle, ensure_ascii=False)
        return subprocess.CompletedProcess(command, 0, "", "")


def make_options(tmp_path, runner: FakeRunner, **overrides) -> AsrOptions:
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    defaults = dict(
        src=tmp_path / "audio.mp4",
        out=tmp_path / "out",
        model=str(model),
        python=sys.executable,
        language="en",
        hotwords=("AI", "OpenAI"),
        min_duration_ms=400,  # 保持两条 cue，专门验 worker→SRT 契约
        runner=runner,
    )
    defaults.update(overrides)
    options = AsrOptions(**defaults)
    tmp_path.joinpath("audio.mp4").write_bytes(b"fake audio")
    return options


def test_run_asr_writes_srt_words_and_report(tmp_path) -> None:
    runner = FakeRunner(worker_payload())

    result = run_asr(make_options(tmp_path, runner))

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.srt_path is not None and result.srt_path.is_file()
    assert result.words_path is not None and result.words_path.is_file()
    assert result.report["summary"]["cues"] == 2
    assert result.report["summary"]["words"] == 3
    assert result.report["summary"]["language"] == "en"
    rendered = result.srt_path.read_text(encoding="utf-8")
    assert "Hello world." in rendered
    assert "Bye." in rendered


def test_run_asr_passes_hotwords_and_language_to_worker(tmp_path) -> None:
    runner = FakeRunner(worker_payload())

    run_asr(make_options(tmp_path, runner))

    worker_call = runner.calls[-1]
    assert "--hotwords" in worker_call
    assert "AI, OpenAI" in worker_call
    assert worker_call[worker_call.index("--language") + 1] == "en"


def test_run_asr_empty_speech_is_invalid_but_reported(tmp_path) -> None:
    runner = FakeRunner(worker_payload(words=[], segments=[]))

    result = run_asr(make_options(tmp_path, runner))

    assert result.exit_code == 2
    assert result.status == "empty"
    assert result.srt_path is None
    assert any("没有识别出任何语音" in warning for warning in result.report["warnings"])


def test_run_asr_reports_worker_failure(tmp_path) -> None:
    runner = FakeRunner({"ok": False, "error": "model not found", "traceback": ""})

    with pytest.raises(AsrError, match="model not found"):
        run_asr(make_options(tmp_path, runner))


def test_run_asr_wraps_garbage_worker_output_as_asr_error(tmp_path) -> None:
    """worker 写出坏 JSON 时必须是 AsrError，不能让 JSONDecodeError 穿透。

    以前这里是裸 ``json.loads``：worker 半途被杀、磁盘写一半都会抛
    ``json.JSONDecodeError``，而 pipeline 只捕 ``AsrError``——整套链路直接崩栈。
    """

    class GarbageRunner(FakeRunner):
        def __call__(self, command):
            self.calls.append(list(command))
            if len(command) >= 3 and command[1] == "-c":
                return subprocess.CompletedProcess(command, 0, "", "")
            output = command[command.index("--output") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                handle.write("{truncated json")
            return subprocess.CompletedProcess(command, 0, "", "")

    runner = GarbageRunner({})

    with pytest.raises(AsrError, match="不是合法 JSON"):
        run_asr(make_options(tmp_path, runner))


def test_run_asr_missing_python_has_actionable_message(tmp_path) -> None:
    runner = FakeRunner(worker_payload(), python_ok=False)

    with pytest.raises(AsrError, match="--asr-python"):
        find_python(sys.executable, runner=runner)


def test_low_language_probability_is_warned(tmp_path) -> None:
    runner = FakeRunner(worker_payload(language_probability=0.3))

    result = run_asr(make_options(tmp_path, runner))

    assert any("置信度" in warning for warning in result.report["warnings"])


def test_language_mismatch_is_warned(tmp_path) -> None:
    runner = FakeRunner(worker_payload(language="zh"))

    result = run_asr(make_options(tmp_path, runner))

    assert any("不一致" in warning for warning in result.report["warnings"])


# ---------------------------------------------------------------- 环境解析


def test_find_model_accepts_explicit_directory(tmp_path) -> None:
    model = tmp_path / "my-model"
    model.mkdir()

    assert find_model(str(model)) == str(model)


def test_find_model_maps_known_names_to_local_dirs() -> None:
    """`--model small` 要落到项目内本地目录：交给 faster-whisper 会去 HF 下载（本机被墙）。"""

    resolved = find_model("small")

    assert Path(resolved).is_dir()
    assert "faster-whisper-small" in resolved.replace("\\", "/")


def test_find_model_passes_through_unknown_names() -> None:
    assert find_model("large-v3") == "large-v3"


def test_find_model_rejects_missing_default(tmp_path, monkeypatch) -> None:
    import cuesmith.asr as asr_module

    monkeypatch.delenv("CUESMITH_ASR_MODEL", raising=False)
    monkeypatch.setattr(asr_module, "_model_dir_ok", lambda directory: None)

    with pytest.raises(AsrError, match="--model"):
        find_model(None)


def test_short_tail_cue_merges_even_if_slightly_over_max_duration() -> None:
    """真实中文素材实测：长句末尾几个字会留成 0.5s 闪条，必须并回上一条（≤1s 容差）。"""

    segments = segment_words(
        words((0.0, 6.9, "前面的话"), (6.9, 7.5, "走近")),
        max_duration_ms=7000,
        min_duration_ms=800,
    )

    assert len(segments) == 1
    assert segments[0].end_ms - segments[0].start_ms == pytest.approx(7500, abs=1)
    assert segments[0].text == "前面的话走近"  # 中文合并不能留空格


def test_model_dir_resolves_hf_cache_snapshot(tmp_path) -> None:
    """HF 缓存布局（snapshots/<hash>/model.bin）也要能用，不能只认平铺目录。"""

    from cuesmith.asr import _model_dir_ok

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "model.bin").write_bytes(b"x")
    assert _model_dir_ok(flat) == flat

    cached = tmp_path / "cached"
    snapshot = cached / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x")
    assert _model_dir_ok(cached) == snapshot

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _model_dir_ok(empty) is None
