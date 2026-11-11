"""本地 TTS 引擎（主包侧）：只用标准库，把 sherpa-onnx 关在 ``submatrix/tts_worker.py`` 子进程里。

PRD 锁定主包依赖为"标准库 + ffmpeg"，而 sherpa-onnx / onnxruntime / numpy 版本敏感且体积大，
所以本地 TTS 采用与 ASR/说话人分离相同的**常驻 worker** 模式：

- :func:`find_local_tts_python` 找"能 ``import sherpa_onnx`` 的 Python"（默认探
  ``spike/diarization/.venv``）
- :func:`find_local_tts_model` 找本地模型目录（kokoro 多语 / MeloTTS 中英）
- :class:`LocalTtsEngine` 实现编排层唯一依赖的 ``synthesize(text, out_path) -> None``

同一组 ``(python, 模型目录, 类型, 线程数)`` 的引擎**共享一个 worker 子进程**（模块级 registry）：
模型只加载一次，请求用 ``threading.Lock`` 串行化（sherpa 单实例不是线程安全的），
所以 ``dub`` 的多线程并发调用是安全的。``dub`` 结束时调 :func:`close_all_workers`。

音色：``voice`` 为数字 / ``kokoro:47`` / kokoro 音色名（``xiaoxiao`` 等）时原样交给 worker；
Edge-TTS 风格 id（``zh-CN-XiaoxiaoNeural``）、空值或无法识别的值都退回
:func:`default_local_voice` 选出的本地默认音色（解析结果记在 ``engine.resolved_voice``）。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .interpreters import (
    Requirement,
    bool_probe,
    resolve,
    subprocess_probe,
)
from .worker_protocol import describe_error

__all__ = [
    "DEFAULT_MODEL_CANDIDATES",
    "KOKORO_SPEAKERS",
    "LocalTtsEngine",
    "LocalTtsError",
    "LocalTtsOptions",
    "close_all_workers",
    "default_local_voice",
    "detect_local_tts_kind",
    "find_local_tts_model",
    "find_local_tts_python",
    "resolve_local_voice",
]

ENV_PYTHON = "SUBMATRIX_LOCAL_TTS_PYTHON"
ENV_MODEL = "SUBMATRIX_LOCAL_TTS_MODEL"

_LOCAL_TTS_PYTHON = Requirement(
    label="sherpa-onnx",
    module="sherpa_onnx",
    parameter="--local-tts-python",
    env_var=ENV_PYTHON,
    hint="本地 TTS 是可选能力：没有模型/依赖时用默认 Edge-TTS（不传 --local-tts 即可）；"
    "要用的话在装了 sherpa-onnx 的环境里补装依赖，或指一个已装好的解释器。",
)

#: 仓库内默认模型目录（相对仓库根）；按优先级取第一个"存在且含 model.onnx"的。
DEFAULT_MODEL_CANDIDATES = (
    "spike/local-tts/kokoro-multi-lang-v1_0",
    "spike/local-tts/vits-melo-tts-zh_en",
)

WORKER_SCRIPT = Path(__file__).with_name("tts_worker.py")

# 与 submatrix/tts_worker.py 的 KOKORO_SPEAKERS 保持一致（顺序 = model.onnx 里的
# speaker_names / id2speaker：0 af_alloy … 53 em_santa，中文 45–52）。
KOKORO_SPEAKERS = (
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
    "em_santa",
)

# 全名 + 唯一的短名（xiaoxiao / yunjian …）都认；重名短名（dora / alex / santa）
# 只认全名，避免把 "santa" 解析成错误的性别/语言音色。
_KNOWN_VOICES: dict[str, str] = {}
_SHORT_NAME_COUNTS: dict[str, int] = {}
for _name in KOKORO_SPEAKERS:
    _KNOWN_VOICES.setdefault(_name.lower(), _name)
    _short = _name.split("_", 1)[1].lower()
    _SHORT_NAME_COUNTS[_short] = _SHORT_NAME_COUNTS.get(_short, 0) + 1
for _name in KOKORO_SPEAKERS:
    _short = _name.split("_", 1)[1].lower()
    if _SHORT_NAME_COUNTS[_short] == 1:
        _KNOWN_VOICES.setdefault(_short, _name)
del _name, _short

# Edge-TTS 音色 id，例如 zh-CN-XiaoxiaoNeural / en-US-AriaNeural / zh-CN-liaoning-XiaobeiNeural
_EDGE_VOICE_RE = re.compile(r"^[a-zA-Z]{2,3}-[a-zA-Z]{2,4}-[A-Za-z0-9-]+Neural$")
_DIGITS_RE = re.compile(r"^\d+$")


class LocalTtsError(RuntimeError):
    """本地 TTS 不可用/单条合成失败（信息面向用户，可直接展示）。"""


# ---------------------------------------------------------------------------
# 解释器 / 模型目录探测
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _candidate_local_tts_pythons(root: Path | None = None) -> list[str]:
    """仓库里可能装了 sherpa-onnx 的解释器（按优先级）。"""

    base = Path(root) if root is not None else _repo_root()
    candidates: list[str] = []
    for venv in (
        base / "spike" / "diarization" / ".venv",
        base / "spike" / "local-tts" / ".venv",
        base / ".venv",
        base / "venv",
    ):
        for relative in (Path("Scripts") / "python.exe", Path("bin") / "python"):
            path = venv / relative
            if path.is_file():
                candidates.append(str(path))
    return candidates


_SHERPA_ONNX_PROBE = subprocess_probe("sherpa_onnx", timeout_s=60.0)


def _probe_sherpa_onnx(python: str) -> bool:
    """``[python, -c, "import sherpa_onnx"]`` 探针；任何失败都当"不可用"。

    本体在 ``interpreters.subprocess_probe``（tts / localtts / bgm 共用一份）。
    """

    return _SHERPA_ONNX_PROBE(python)[0]


def find_local_tts_python(
    explicit: str | None = None,
    *,
    runner: Callable[[str], bool] | None = None,
) -> str:
    """找第一个能 ``import sherpa_onnx`` 的解释器。

    顺序：显式 ``--local-tts-python`` → 环境变量 ``SUBMATRIX_LOCAL_TTS_PYTHON`` →
    仓库已知 venv（``spike/diarization/.venv``：装了 sherpa-onnx 的那个）→ 当前解释器。
    显式值与环境变量**不做探测**（尊重用户配置，真跑不通时 worker 启动错误会带出
    ``ModuleNotFoundError`` 与可操作提示）；自动探测全部失败时抛 :class:`LocalTtsError`。

    ``runner`` 注入探针（测试用）：``runner(python) -> bool``。
    """

    check = runner or _probe_sherpa_onnx
    found = resolve(
        _LOCAL_TTS_PYTHON,
        explicit,
        candidates=[*_candidate_local_tts_pythons(), sys.executable],
        probe=bool_probe(check),
        trust_explicit=True,
        trust_env=True,
        error_cls=LocalTtsError,
    )
    if found is None:  # 契约上走不到：on_missing=raise 要么返回解释器、要么已抛错
        raise LocalTtsError("内部错误：解释器探测既没返回结果也没报错")
    return found


def _is_model_dir(path: Path) -> bool:
    return path.is_dir() and (path / "model.onnx").is_file()


def find_local_tts_model(
    explicit: str | Path | None = None,
    *,
    root: str | Path | None = None,
) -> Path | None:
    """定位本地 TTS 模型目录（含 ``model.onnx``）。

    顺序：显式路径（必须存在且含 ``model.onnx``，否则抛错）→ 环境变量
    ``SUBMATRIX_LOCAL_TTS_MODEL`` → 仓库默认候选（kokoro 多语 → MeloTTS 中英）。
    都没有时返回 ``None``（调用方回退 Edge-TTS）。
    """

    if explicit is not None:
        path = Path(explicit).expanduser()
        if not _is_model_dir(path):
            raise LocalTtsError(
                f"指定的本地 TTS 模型目录不可用：{path}\n"
                "目录必须存在且包含 model.onnx（sherpa-onnx 模型根目录，不是 .tar.bz2 压缩包）。"
            )
        return path

    configured = os.environ.get(ENV_MODEL)
    if configured:
        path = Path(configured).expanduser()
        if not _is_model_dir(path):
            raise LocalTtsError(
                f"环境变量 {ENV_MODEL} 指向的目录不可用：{path}\n"
                "目录必须存在且包含 model.onnx（sherpa-onnx 模型根目录）。"
            )
        return path

    base = Path(root) if root is not None else _repo_root()
    for relative in DEFAULT_MODEL_CANDIDATES:
        candidate = base / relative
        if _is_model_dir(candidate):
            return candidate
    return None


def detect_local_tts_kind(model_dir: str | Path) -> str:
    """判断模型类型：有 ``voices.bin`` → ``"kokoro"``；有 ``lexicon.txt`` + ``dict/`` → ``"vits"``。"""

    path = Path(model_dir)
    if (path / "voices.bin").is_file():
        return "kokoro"
    if (path / "lexicon.txt").is_file() and (path / "dict").is_dir():
        return "vits"
    raise LocalTtsError(
        f"无法判断本地 TTS 模型类型：{path}\n"
        "kokoro 模型目录需要 voices.bin；MeloTTS/VITS 模型目录需要 lexicon.txt + dict/。"
        "（Piper 模型的 espeak-ng-data 前端在 sherpa-onnx 2.0 下不可用，不在支持范围）"
    )


def default_local_voice(model_dir: str | Path, language: str | None) -> str:
    """本地模型该语言的默认音色 id。

    kokoro：中文 → ``"47"``（zf_xiaoxiao）、英文 → ``"3"``（af_heart）、其它 → ``"0"``；
    MeloTTS/VITS 只有 1 个女声 → 固定 ``"0"``。
    """

    kind = detect_local_tts_kind(model_dir)
    if kind == "kokoro":
        lang = (language or "").strip().lower().replace("_", "-")
        if lang.startswith("zh"):
            return "47"
        if lang.startswith("en"):
            return "3"
    return "0"


def resolve_local_voice(
    model_dir: str | Path, voice: str | None, language: str | None = None
) -> str:
    """把外部音色名解析成本地 worker 能懂的 spec；解析不出来退回默认音色（不抛错）。"""

    fallback = default_local_voice(model_dir, language)
    spec = (voice or "").strip()
    if not spec:
        return fallback
    kind = detect_local_tts_kind(model_dir)

    if _DIGITS_RE.match(spec):
        return spec

    lowered = spec.lower()
    prefix, _, rest = lowered.partition(":")
    if prefix in {"kokoro", "vits"} and rest:
        if prefix != kind:
            return fallback
        rest = rest.strip()
        if _DIGITS_RE.match(rest):
            return spec
        if kind == "kokoro" and rest in _KNOWN_VOICES:
            return spec
        return fallback

    if kind == "kokoro" and lowered in _KNOWN_VOICES:
        return spec
    if _EDGE_VOICE_RE.match(spec):
        return fallback
    return fallback


# ---------------------------------------------------------------------------
# worker 子进程
# ---------------------------------------------------------------------------


class _StreamReader:
    """后台线程按行读管道，``readline(timeout)`` 支持超时（Windows 管道不能 select）。"""

    def __init__(self, stream) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._eof = False
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except (OSError, ValueError):  # 管道被关闭
            pass
        finally:
            self._queue.put(None)

    def readline(self, timeout: float) -> str | None:
        if self._eof:
            return None
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"等待子进程输出超过 {timeout:g} 秒") from None
        if item is None:
            self._eof = True
        return item


class _StderrCollector:
    """收集 worker stderr 末尾若干行，失败时拼进错误信息（不阻塞子进程）。"""

    def __init__(self, stream, *, max_lines: int = 60) -> None:
        self._lines: list[str] = []
        self._max_lines = max_lines
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                with self._lock:
                    self._lines.append(line.rstrip())
                    if len(self._lines) > self._max_lines:
                        del self._lines[: len(self._lines) - self._max_lines]
        except (OSError, ValueError):  # 管道被关闭
            pass

    def tail(self, lines: int = 12) -> str:
        with self._lock:
            return "\n".join(self._lines[-lines:])


def _parse_json_line(line: str) -> dict[str, object]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LocalTtsError(f"本地 TTS worker 输出了非 JSON 行：{line[:300]!r}") from exc
    if not isinstance(payload, dict):
        raise LocalTtsError(f"本地 TTS worker 输出不是 JSON 对象：{line[:300]!r}")
    return payload


class _Worker:
    """一个常驻的 ``tts_worker.py`` 子进程；同 key 的引擎共享它。"""

    def __init__(
        self, *, python: str, model: Path, kind: str, threads: int, timeout_s: float
    ) -> None:
        self.python = python
        self.model = model
        self.kind = kind
        self.threads = int(threads)
        self.timeout_s = float(timeout_s)
        self._process: subprocess.Popen | None = None
        self._stdin = None
        self._stdout: _StreamReader | None = None
        self._stderr: _StderrCollector | None = None
        self._lock = threading.Lock()
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------
    def _command(self) -> list[str]:
        return [
            self.python,
            str(WORKER_SCRIPT),
            "--model-dir",
            str(self.model),
            "--kind",
            self.kind,
            "--threads",
            str(self.threads),
        ]

    def _popen_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":  # GUI/无控制台进程里不要弹黑框
            flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if flag:
                kwargs["creationflags"] = flag
        return kwargs

    def _start(self) -> None:
        command = self._command()
        try:
            process = subprocess.Popen(command, **self._popen_kwargs())
        except FileNotFoundError as exc:
            raise LocalTtsError(
                f"启动本地 TTS worker 失败：找不到 Python 解释器 {self.python}\n"
                f"请检查 --local-tts-python 或环境变量 {ENV_PYTHON}，"
                f"或用 {sys.executable} 所在环境安装 sherpa-onnx。"
            ) from exc
        except OSError as exc:
            raise LocalTtsError(f"启动本地 TTS worker 失败：{exc}") from exc

        self._process = process
        self._stdin = process.stdin
        self._stdout = _StreamReader(process.stdout)
        self._stderr = _StderrCollector(process.stderr)

        try:
            line = self._stdout.readline(self.timeout_s)
        except TimeoutError as exc:
            self._discard()
            raise LocalTtsError(
                f"本地 TTS 模型加载超时（{self.timeout_s:g} 秒）：{self.model}\n"
                f"命令：{' '.join(command)}"
            ) from exc

        if line is None:
            code = process.poll()
            detail = self._stderr_tail()
            self._discard()
            raise LocalTtsError(
                f"本地 TTS worker 在就绪前退出（退出码 {code}）：{self.model}\n"
                f"{detail or '（stderr 为空）'}"
            )

        try:
            payload = _parse_json_line(line)
        except LocalTtsError:
            self._discard()
            raise
        if not payload.get("ok") or not payload.get("ready"):
            detail = self._stderr_tail()
            self._discard()
            raise LocalTtsError(
                "本地 TTS worker 模型加载失败："
                f"{payload.get('error') or payload}\n"
                f"{detail or ''}"
            )

    def _stderr_tail(self) -> str:
        return self._stderr.tail() if self._stderr is not None else ""

    def _discard(self) -> None:
        """结束当前子进程（失败/关闭时用），保留 worker 对象以便下次重启。"""

        process, self._process = self._process, None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._discard()

    # -- 请求 -------------------------------------------------------------
    def request(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            if self._closed:
                raise LocalTtsError("本地 TTS worker 已关闭")
            if self._process is None or self._process.poll() is not None:
                self._discard()  # 上次异常退出：本次调用自动重启一次
                self._start()

            assert self._stdin is not None and self._stdout is not None
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            try:
                self._stdin.write(line)
                self._stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                detail = self._stderr_tail()
                self._discard()
                raise LocalTtsError(
                    f"本地 TTS worker 管道已断开：{exc}\n{detail or ''}"
                ) from exc

            try:
                response = self._stdout.readline(self.timeout_s)
            except TimeoutError as exc:
                detail = self._stderr_tail()
                self._discard()
                raise LocalTtsError(
                    f"本地 TTS 合成超时（{self.timeout_s:g} 秒）：{payload.get('out')}\n"
                    f"{detail or ''}"
                ) from exc

            if response is None:
                code = self._process.poll() if self._process is not None else None
                detail = self._stderr_tail()
                self._discard()
                raise LocalTtsError(
                    f"本地 TTS worker 在合成过程中退出（退出码 {code}）\n{detail or ''}"
                )

            result = _parse_json_line(response)
            if not result.get("ok"):
                # 带上 worker 的 traceback 尾巴：本地 TTS 失败最常见的根因（模型/音色/sherpa 版本）
                # 只在栈里看得见，以前这里把 traceback 丢了，排查等于盲猜
                raise LocalTtsError(f"本地 TTS 合成失败：{describe_error(result)}")
            return result


_WORKERS: dict[tuple[str, str, str, int], _Worker] = {}
_WORKERS_LOCK = threading.Lock()


def _get_worker(key: tuple[str, str, str, int], factory: Callable[[], _Worker]) -> _Worker:
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None:
            worker = factory()
            _WORKERS[key] = worker
        return worker


def close_all_workers() -> None:
    """关闭并移除所有共享 worker（幂等；``dub`` 结束时调用）。"""

    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        try:
            worker.close()
        except OSError:  # pragma: no cover - 关闭失败不影响退出
            pass


atexit.register(close_all_workers)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalTtsOptions:
    """本地 TTS 引擎配置（``model`` = 模型**目录**）。"""

    model: Path
    kind: str = "auto"  # auto | kokoro | vits
    python: str | None = None
    threads: int = 8
    timeout_s: float = 300.0


class LocalTtsEngine:
    """本地 TTS 引擎：实现 ``synthesize(text, out_path) -> None``，可替换 Edge-TTS。

    同一 ``(python, 模型, 类型, 线程数)`` 的多个实例共享一个 worker 子进程（模型只加载一次）；
    worker 意外退出时，本次调用抛 :class:`LocalTtsError`，下次调用自动重启一次。
    """

    def __init__(
        self,
        options: LocalTtsOptions,
        voice: str = "",
        *,
        language: str | None = None,
        speed: float = 1.0,
    ) -> None:
        self.options = options
        self.voice = voice or ""
        self.language = language
        self.speed = min(2.0, max(0.5, float(speed)))
        self.last_duration: float | None = None
        self._python: str | None = None

        model = Path(options.model)
        if not (model / "model.onnx").is_file():
            raise LocalTtsError(
                f"本地 TTS 模型目录不可用：{model}\n目录必须存在且包含 model.onnx。"
            )
        self.model = model
        if options.kind not in {"auto", "kokoro", "vits"}:
            raise LocalTtsError(f"不支持的本地 TTS 类型：{options.kind!r}（可选 auto/kokoro/vits）")
        self.kind = detect_local_tts_kind(model) if options.kind == "auto" else options.kind
        self.resolved_voice = resolve_local_voice(model, voice, language)

    # -- 内部 -------------------------------------------------------------
    def _resolved_python(self) -> str:
        if self._python is None:
            self._python = find_local_tts_python(self.options.python)
        return self._python

    def _worker(self) -> _Worker:
        python = self._resolved_python()
        try:
            model_key = str(self.model.resolve())
        except OSError:  # pragma: no cover - 路径异常时退回原字符串
            model_key = str(self.model)
        key = (python, model_key, self.kind, int(self.options.threads))
        return _get_worker(
            key,
            lambda: _Worker(
                python=python,
                model=self.model,
                kind=self.kind,
                threads=int(self.options.threads),
                timeout_s=self.options.timeout_s,
            ),
        )

    # -- 编排层接口 -------------------------------------------------------
    def synthesize(self, text: str, out_path: Path) -> None:
        if not isinstance(text, str) or not text.strip():
            raise LocalTtsError("待合成文本为空（本地 TTS 拒绝合成空文案）")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, object] = {
            "text": text,
            "out": str(out_path.absolute()),
            "voice": self.resolved_voice,
            "speed": self.speed,
        }
        worker = self._worker()
        result = worker.request(payload)
        self.last_duration = float(result.get("duration") or 0.0) or None
        if not out_path.is_file() or out_path.stat().st_size <= 44:
            raise LocalTtsError(f"本地 TTS 未生成有效 wav：{out_path}")
