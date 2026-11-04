"""``submatrix.localtts`` 单测：**不 import sherpa、不依赖真实模型/网络/子进程**。

worker 子进程用假 ``subprocess.Popen`` 替代（stdin/stdout 是线程安全的假管道），
覆盖：ready 握手、请求 JSON 字段、时长回传、ok:false、进程 EOF/死亡后自动重启、
各探测函数的优先级与报错、Edge 风格音色回退、``close_all_workers`` 幂等。
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import pytest

from submatrix import localtts

# ---------------------------------------------------------------------------
# 假子进程
# ---------------------------------------------------------------------------


class FakeStdout:
    """阻塞式假 stdout：``for line in stream`` / 直接读队列直到 EOF。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._queue.put(line)

    def eof(self) -> None:
        self._queue.put(None)

    def __iter__(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item


class FakeStdin:
    def __init__(self, on_line) -> None:
        self._on_line = on_line
        self.lines: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.lines.append(text)
        self._on_line(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _write_stub_wav(payload: dict) -> None:
    path = Path(str(payload["out"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\x00" * 256)


class FakePopen:
    """替身进程；``handler(process, request, index) -> dict | "eof" | "hang"``。"""

    instances: list["FakePopen"] = []
    ready_payload: dict | None = {"ok": True, "ready": True, "kind": "kokoro", "num_speakers": 54, "sample_rate": 24000}
    handler = None

    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.stdin = FakeStdin(self._on_line)
        self.stdout = FakeStdout()
        self.stderr = FakeStdout()
        self.requests: list[dict] = []
        FakePopen.instances.append(self)
        if FakePopen.ready_payload is not None:
            self.stdout.push(json.dumps(FakePopen.ready_payload) + "\n")

    def _on_line(self, text: str) -> None:
        line = text.strip()
        if not line:
            return
        request = json.loads(line)
        self.requests.append(request)
        handler = FakePopen.handler
        if handler is None:
            _write_stub_wav(request)
            response = {"ok": True, "duration": 1.5, "sample_rate": 24000, "voice": 0}
        else:
            response = handler(self, request, len(self.requests))
        if response == "eof":
            self.returncode = 1
            self.stdout.eof()
            return
        if response == "hang":
            return
        if response is not None:
            self.stdout.push(json.dumps(response) + "\n")

    # subprocess.Popen 的公开接口（只实现 localtts 用到的）
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


@pytest.fixture
def fake_proc(monkeypatch):
    localtts.close_all_workers()
    FakePopen.instances.clear()
    FakePopen.ready_payload = {
        "ok": True,
        "ready": True,
        "kind": "kokoro",
        "num_speakers": 54,
        "sample_rate": 24000,
    }
    FakePopen.handler = None
    monkeypatch.setattr(localtts.subprocess, "Popen", FakePopen)
    yield FakePopen
    localtts.close_all_workers()
    FakePopen.handler = None


def make_model_dir(tmp_path: Path, kind: str = "kokoro", name: str | None = None) -> Path:
    path = tmp_path / (name or f"model-{kind}")
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.onnx").write_bytes(b"onnx")
    if kind == "kokoro":
        (path / "voices.bin").write_bytes(b"voices")
        (path / "tokens.txt").write_text("x", encoding="utf-8")
    else:
        (path / "lexicon.txt").write_text("x", encoding="utf-8")
        (path / "dict").mkdir(exist_ok=True)
        (path / "tokens.txt").write_text("x", encoding="utf-8")
    return path


def make_options(tmp_path: Path, kind: str = "kokoro", **kwargs) -> localtts.LocalTtsOptions:
    defaults = {"python": "fake-python", "timeout_s": 5.0}
    defaults.update(kwargs)
    return localtts.LocalTtsOptions(model=make_model_dir(tmp_path, kind), **defaults)


def make_engine(
    tmp_path: Path,
    kind: str = "kokoro",
    voice: str = "",
    *,
    language: str | None = None,
    speed: float = 1.0,
    options: localtts.LocalTtsOptions | None = None,
) -> localtts.LocalTtsEngine:
    return localtts.LocalTtsEngine(
        options or make_options(tmp_path, kind),
        voice,
        language=language,
        speed=speed,
    )


# ---------------------------------------------------------------------------
# 引擎 <-> worker 协议
# ---------------------------------------------------------------------------


def test_ready_handshake_and_request_fields(fake_proc, tmp_path):
    engine = make_engine(tmp_path, language="zh-CN")
    out = tmp_path / "out" / "cue.wav"

    engine.synthesize("你好，世界", out)

    assert len(FakePopen.instances) == 1
    process = FakePopen.instances[0]
    assert engine.resolved_voice == "47"
    assert process.command[0] == "fake-python"
    assert "tts_worker.py" in process.command[1]
    assert process.command[process.command.index("--model-dir") + 1] == str(engine.model)

    request = process.requests[0]
    assert request["text"] == "你好，世界"
    assert Path(request["out"]) == out.absolute()
    assert request["voice"] == "47"
    assert request["speed"] == 1.0
    assert engine.last_duration == 1.5
    assert out.is_file() and out.stat().st_size > 44


def test_worker_error_response_raises_localtts_error(fake_proc, tmp_path):
    def handler(process, request, index):
        return {"ok": False, "error": "模型推理炸了"}

    FakePopen.handler = handler
    engine = make_engine(tmp_path)

    with pytest.raises(localtts.LocalTtsError, match="模型推理炸了"):
        engine.synthesize("测试", tmp_path / "bad.wav")

    # 单条失败不应杀掉 worker，也不应触发重启
    assert len(FakePopen.instances) == 1
    assert FakePopen.instances[0].poll() is None


def test_worker_eof_then_restart_succeeds(fake_proc, tmp_path):
    def die_once(process, request, index):
        return "eof"

    FakePopen.handler = die_once
    engine = make_engine(tmp_path)

    with pytest.raises(localtts.LocalTtsError, match="退出"):
        engine.synthesize("第一条", tmp_path / "a.wav")
    assert len(FakePopen.instances) == 1
    assert FakePopen.instances[0].poll() == 1

    # 下次调用自动重启，重启后成功
    FakePopen.handler = None
    engine.synthesize("第二条", tmp_path / "b.wav")
    assert len(FakePopen.instances) == 2
    assert (tmp_path / "b.wav").is_file()


def test_worker_dies_again_after_restart_raises(fake_proc, tmp_path):
    FakePopen.handler = lambda process, request, index: "eof"
    engine = make_engine(tmp_path)

    with pytest.raises(localtts.LocalTtsError):
        engine.synthesize("一", tmp_path / "a.wav")
    with pytest.raises(localtts.LocalTtsError):
        engine.synthesize("二", tmp_path / "b.wav")
    assert len(FakePopen.instances) == 2  # 第二次是重启出来的新进程


def test_worker_ready_failure_raises(fake_proc, tmp_path):
    FakePopen.ready_payload = {"ok": False, "error": "model.onnx 损坏"}
    engine = make_engine(tmp_path)

    with pytest.raises(localtts.LocalTtsError, match="model.onnx 损坏"):
        engine.synthesize("测试", tmp_path / "a.wav")


def test_ready_timeout_raises_and_does_not_hang(fake_proc, tmp_path):
    FakePopen.ready_payload = None  # 永远不就绪
    options = make_options(tmp_path, timeout_s=0.05)
    engine = localtts.LocalTtsEngine(options)

    with pytest.raises(localtts.LocalTtsError, match="超时"):
        engine.synthesize("测试", tmp_path / "a.wav")


def test_synth_timeout_raises(fake_proc, tmp_path):
    FakePopen.handler = lambda process, request, index: "hang"
    options = make_options(tmp_path, timeout_s=0.05)
    engine = localtts.LocalTtsEngine(options)

    with pytest.raises(localtts.LocalTtsError, match="超时"):
        engine.synthesize("测试", tmp_path / "a.wav")


def test_same_options_share_one_worker(fake_proc, tmp_path):
    options = make_options(tmp_path)
    first = localtts.LocalTtsEngine(options)
    second = localtts.LocalTtsEngine(options)
    first.synthesize("一", tmp_path / "a.wav")
    second.synthesize("二", tmp_path / "b.wav")
    assert len(FakePopen.instances) == 1
    assert len(FakePopen.instances[0].requests) == 2


def test_concurrent_engines_share_worker_serially(fake_proc, tmp_path):
    options = make_options(tmp_path)
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            localtts.LocalTtsEngine(options).synthesize(
                f"并发 {index}", tmp_path / f"c{index}.wav"
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里收集
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(FakePopen.instances) == 1
    assert len(FakePopen.instances[0].requests) == 3
    assert all((tmp_path / f"c{i}.wav").is_file() for i in range(3))


def test_close_all_workers_idempotent_and_restartable(fake_proc, tmp_path):
    options = make_options(tmp_path)
    engine = localtts.LocalTtsEngine(options)
    engine.synthesize("一", tmp_path / "a.wav")
    process = FakePopen.instances[0]

    localtts.close_all_workers()
    localtts.close_all_workers()  # 幂等
    assert process.stdin.closed

    # 关闭后同 key 再合成 → 重新拉一个 worker
    localtts.LocalTtsEngine(options).synthesize("二", tmp_path / "b.wav")
    assert len(FakePopen.instances) == 2


def test_engine_rejects_missing_model_dir(tmp_path):
    options = localtts.LocalTtsOptions(model=tmp_path / "nope", python="fake-python")
    with pytest.raises(localtts.LocalTtsError, match="model.onnx"):
        localtts.LocalTtsEngine(options)


def test_engine_rejects_empty_text(fake_proc, tmp_path):
    engine = make_engine(tmp_path)
    with pytest.raises(localtts.LocalTtsError, match="文本为空"):
        engine.synthesize("   ", tmp_path / "a.wav")


def test_speed_is_clamped(fake_proc, tmp_path):
    engine = make_engine(tmp_path, speed=9.9)
    assert engine.speed == 2.0
    engine.synthesize("测试", tmp_path / "a.wav")
    assert FakePopen.instances[0].requests[0]["speed"] == 2.0


# ---------------------------------------------------------------------------
# 探测函数：find_local_tts_python
# ---------------------------------------------------------------------------


def test_find_python_explicit_wins_without_probe(monkeypatch):
    monkeypatch.setenv(localtts.ENV_PYTHON, "env-python")
    probed: list[str] = []

    result = localtts.find_local_tts_python(
        "explicit-python", runner=lambda path: probed.append(path) or True
    )

    assert result == "explicit-python"
    assert probed == []


def test_find_python_env_wins_without_probe(monkeypatch):
    monkeypatch.setenv(localtts.ENV_PYTHON, "env-python")

    def runner(path: str) -> bool:
        raise AssertionError(f"不应探测：{path}")

    assert localtts.find_local_tts_python(runner=runner) == "env-python"


def test_find_python_probes_candidates_in_order(monkeypatch):
    monkeypatch.delenv(localtts.ENV_PYTHON, raising=False)
    monkeypatch.setattr(
        localtts, "_candidate_local_tts_pythons", lambda root=None: ["first", "second"]
    )
    probed: list[str] = []

    def runner(path: str) -> bool:
        probed.append(path)
        return path == "second"

    assert localtts.find_local_tts_python(runner=runner) == "second"
    assert probed == ["first", "second"]


def test_find_python_raises_actionable_error(monkeypatch):
    monkeypatch.delenv(localtts.ENV_PYTHON, raising=False)
    monkeypatch.setattr(localtts, "_candidate_local_tts_pythons", lambda root=None: [])

    with pytest.raises(localtts.LocalTtsError) as excinfo:
        localtts.find_local_tts_python(runner=lambda path: False)

    message = str(excinfo.value)
    assert "sherpa_onnx" in message
    assert localtts.ENV_PYTHON in message


# ---------------------------------------------------------------------------
# 探测函数：find_local_tts_model / detect kind / default voice
# ---------------------------------------------------------------------------


def test_find_model_explicit_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv(localtts.ENV_MODEL, raising=False)
    explicit = make_model_dir(tmp_path, "vits", name="explicit")
    assert localtts.find_local_tts_model(explicit) == explicit

    with pytest.raises(localtts.LocalTtsError, match="model.onnx"):
        localtts.find_local_tts_model(tmp_path / "missing")

    env_model = make_model_dir(tmp_path, "kokoro", name="env-model")
    monkeypatch.setenv(localtts.ENV_MODEL, str(env_model))
    assert localtts.find_local_tts_model() == env_model
    # 显式优先于环境变量
    assert localtts.find_local_tts_model(explicit) == explicit

    monkeypatch.setenv(localtts.ENV_MODEL, str(tmp_path / "bad-env"))
    with pytest.raises(localtts.LocalTtsError, match=localtts.ENV_MODEL):
        localtts.find_local_tts_model()


def test_find_model_default_candidates(tmp_path, monkeypatch):
    monkeypatch.delenv(localtts.ENV_MODEL, raising=False)
    root = tmp_path / "repo"
    kokoro = root / localtts.DEFAULT_MODEL_CANDIDATES[0]
    kokoro.mkdir(parents=True)
    (kokoro / "model.onnx").write_bytes(b"")

    assert localtts.find_local_tts_model(root=root) == kokoro

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    assert localtts.find_local_tts_model(root=empty_root) is None


def test_find_model_default_skips_dir_without_onnx(tmp_path, monkeypatch):
    monkeypatch.delenv(localtts.ENV_MODEL, raising=False)
    root = tmp_path / "repo"
    (root / localtts.DEFAULT_MODEL_CANDIDATES[0]).mkdir(parents=True)
    assert localtts.find_local_tts_model(root=root) is None


def test_detect_kind(tmp_path):
    kokoro = make_model_dir(tmp_path, "kokoro", name="k")
    assert localtts.detect_local_tts_kind(kokoro) == "kokoro"

    vits = make_model_dir(tmp_path, "vits", name="v")
    assert localtts.detect_local_tts_kind(vits) == "vits"

    # kokoro 标记优先（kokoro 目录里也有 dict/）
    both = make_model_dir(tmp_path, "kokoro", name="both")
    (both / "lexicon.txt").write_text("x", encoding="utf-8")
    assert localtts.detect_local_tts_kind(both) == "kokoro"

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with pytest.raises(localtts.LocalTtsError, match="无法判断"):
        localtts.detect_local_tts_kind(unknown)


def test_default_local_voice(tmp_path):
    kokoro = make_model_dir(tmp_path, "kokoro")
    assert localtts.default_local_voice(kokoro, "zh") == "47"
    assert localtts.default_local_voice(kokoro, "zh-CN") == "47"
    assert localtts.default_local_voice(kokoro, "zh_CN") == "47"
    assert localtts.default_local_voice(kokoro, "en") == "3"
    assert localtts.default_local_voice(kokoro, "en-US") == "3"
    assert localtts.default_local_voice(kokoro, "fr") == "0"
    assert localtts.default_local_voice(kokoro, None) == "0"

    vits = make_model_dir(tmp_path, "vits")
    assert localtts.default_local_voice(vits, "zh") == "0"


# ---------------------------------------------------------------------------
# 音色解析（Edge id / 空 / 未知 → 本地默认）
# ---------------------------------------------------------------------------


def test_resolve_voice_edge_style_falls_back_to_default(tmp_path):
    kokoro = make_model_dir(tmp_path, "kokoro")
    assert localtts.resolve_local_voice(kokoro, "zh-CN-XiaoxiaoNeural", "zh-CN") == "47"
    assert localtts.resolve_local_voice(kokoro, "en-US-AriaNeural", "en-US") == "3"
    assert localtts.resolve_local_voice(kokoro, "", "zh") == "47"
    assert localtts.resolve_local_voice(kokoro, None, "en") == "3"
    assert localtts.resolve_local_voice(kokoro, "完全不认识的音色", "zh") == "47"


def test_resolve_voice_local_specs_pass_through(tmp_path):
    kokoro = make_model_dir(tmp_path, "kokoro")
    assert localtts.resolve_local_voice(kokoro, "47", "zh") == "47"
    assert localtts.resolve_local_voice(kokoro, "zf_xiaoxiao", "zh") == "zf_xiaoxiao"
    assert localtts.resolve_local_voice(kokoro, "xiaoxiao", "zh") == "xiaoxiao"
    assert localtts.resolve_local_voice(kokoro, "kokoro:46", "zh") == "kokoro:46"
    # 类型前缀和目标模型不一致 → 回退默认
    assert localtts.resolve_local_voice(kokoro, "vits:0", "zh") == "47"


def test_resolve_voice_vits_ignores_names(tmp_path):
    vits = make_model_dir(tmp_path, "vits")
    assert localtts.resolve_local_voice(vits, "en-US-AriaNeural", "en") == "0"
    assert localtts.resolve_local_voice(vits, "xiaoxiao", "zh") == "0"
    assert localtts.resolve_local_voice(vits, "vits:0", "zh") == "vits:0"


def test_engine_records_resolved_voice(fake_proc, tmp_path):
    kokoro = make_model_dir(tmp_path, "kokoro")
    options = localtts.LocalTtsOptions(model=kokoro, python="fake-python")
    engine = localtts.LocalTtsEngine(options, "zh-CN-XiaoxiaoNeural", language="zh-CN")
    assert engine.resolved_voice == "47"
    assert engine.kind == "kokoro"

    vits = make_model_dir(tmp_path, "vits")
    vits_engine = localtts.LocalTtsEngine(
        localtts.LocalTtsOptions(model=vits, python="fake-python"),
        "zh-CN-XiaoxiaoNeural",
        language="zh-CN",
    )
    assert vits_engine.resolved_voice == "0"
    assert vits_engine.kind == "vits"


def test_worker_speaker_tables_stay_in_sync():
    from submatrix import tts_worker

    assert tts_worker.KOKORO_SPEAKERS == localtts.KOKORO_SPEAKERS
    assert len(localtts.KOKORO_SPEAKERS) == 54
    assert localtts.KOKORO_SPEAKERS[45] == "zf_xiaobei"
    assert localtts.KOKORO_SPEAKERS[47] == "zf_xiaoxiao"
    assert localtts.KOKORO_SPEAKERS[53] == "em_santa"
