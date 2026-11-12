"""本地 TTS 常驻 worker：由主包用"装了 sherpa-onnx 的 Python"拉起，**只有这里 import sherpa_onnx**。

主包（``submatrix``）纪律是"只用标准库 + ffmpeg、模型只在子进程里跑"（PRD 锁定），
所以本地 TTS 的模型加载/推理全部隔离在本进程里。进程常驻，模型只加载一次，
之后通过 stdin/stdout 逐行 JSON 通信（stdout 只走协议，库的日志被改道到 stderr）：

启动完成后输出一行（并 flush）::

    {"ok": true, "ready": true, "kind": "kokoro", "num_speakers": 54,
     "sample_rate": 24000, "model": "...", "load_s": 3.2}

之后每个请求一行::

    {"text": "...", "out": "C:\\...\\cue.wav", "voice": "47", "speed": 1.0}

``voice`` 支持：空/缺省 → 默认音色（sid 0）；``"47"`` 纯数字；``"kokoro:47"`` /
``"vits:0"`` 带类型前缀；kokoro 也可以是音色名（``xiaoxiao`` / ``zf_xiaoxiao``，
大小写不敏感）。单个请求成功::

    {"ok": true, "duration": 3.21, "sample_rate": 24000, "voice": 47}

失败（不退出进程，继续服务下一个请求）::

    {"ok": false, "error": "..."}

模型加载失败时输出 ``{"ok": false, "error": ...}`` 后退出码 1；stdin EOF 正常退出 0。

用法::

    <sherpa-python> submatrix/tts_worker.py --model-dir DIR [--kind auto|kokoro|vits]
                                            [--threads 8]

注意：sherpa-onnx 2.0 对 Piper/VITS-espeak 模型不支持 ``data_dir`` 前端，本 worker
只支持 kokoro（多语 54 音色）与 vits（MeloTTS 中英单女声）两类模型目录。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

if __package__:
    from . import worker_protocol as protocol
else:  # 脚本直跑：sys.path[0] 是 submatrix/，只能按顶层模块导入
    import worker_protocol as protocol

__all__ = [
    "KOKORO_SPEAKERS",
    "build_parser",
    "detect_kind",
    "load_tts",
    "main",
    "resolve_voice",
    "write_wav",
]

# Kokoro-82M 多语 v1.0 的 54 个音色，顺序与 model.onnx 元数据里的
# ``speaker_names`` / ``id2speaker`` 完全一致（0 af_alloy … 53 em_santa）。
# 中文（普通话）音色 45–52：45 xiaobei / 46 xiaoni / 47 xiaoxiao / 48 xiaoyi /
# 49 yunjian / 50 yunxi / 51 yunxia / 52 yunyang。
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

# 音色名 → sid：全名总是可解析；去掉语言前缀后的短名（xiaoxiao 等）只在
# 全表唯一时可用（dora / alex / santa 等重名短名保持歧义，必须写全名）。
_VOICE_ALIASES: dict[str, int] = {}
_SHORT_NAME_COUNTS: dict[str, int] = {}
for _sid, _name in enumerate(KOKORO_SPEAKERS):
    _VOICE_ALIASES.setdefault(_name.lower(), _sid)
    _short = _name.split("_", 1)[1].lower()
    _SHORT_NAME_COUNTS[_short] = _SHORT_NAME_COUNTS.get(_short, 0) + 1
for _sid, _name in enumerate(KOKORO_SPEAKERS):
    _short = _name.split("_", 1)[1].lower()
    if _SHORT_NAME_COUNTS[_short] == 1:
        _VOICE_ALIASES.setdefault(_short, _sid)
del _sid, _name, _short


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 TTS worker（sherpa-onnx）")
    parser.add_argument("--model-dir", required=True, type=Path, help="模型目录（含 model.onnx）")
    parser.add_argument(
        "--kind",
        default="auto",
        choices=("auto", "kokoro", "vits"),
        help="模型类型；auto = 按目录内容判断",
    )
    parser.add_argument("--threads", type=int, default=8, help="onnxruntime 线程数")
    return parser


def detect_kind(model_dir: Path) -> str:
    """按模型目录内容判断类型：有 ``voices.bin`` → kokoro；有 ``lexicon.txt`` + ``dict/`` → vits。"""

    model_dir = Path(model_dir)
    if not (model_dir / "model.onnx").is_file():
        raise FileNotFoundError(f"模型目录缺少 model.onnx：{model_dir}")
    if (model_dir / "voices.bin").is_file():
        return "kokoro"
    if (model_dir / "lexicon.txt").is_file() and (model_dir / "dict").is_dir():
        return "vits"
    raise ValueError(
        f"无法判断本地 TTS 模型类型：{model_dir}"
        "（kokoro 需要 voices.bin；vits/MeloTTS 需要 lexicon.txt + dict/）"
    )


def load_tts(model_dir: Path, kind: str, threads: int):
    """加载 sherpa-onnx 的 ``OfflineTts``（本函数内 import，主包解释器不需要装 sherpa）。"""

    import sherpa_onnx as sherpa

    model_dir = Path(model_dir)
    model_onnx = model_dir / "model.onnx"
    tokens = model_dir / "tokens.txt"
    dict_dir = model_dir / "dict"
    num_threads = max(1, int(threads))

    if kind == "kokoro":
        lexicons = [
            str(path)
            for path in (model_dir / "lexicon-us-en.txt", model_dir / "lexicon-zh.txt")
            if path.is_file()
        ]
        if not lexicons:
            lexicons = [str(path) for path in sorted(model_dir.glob("lexicon-*.txt"))]
        espeak_dir = model_dir / "espeak-ng-data"
        model_config = sherpa.OfflineTtsModelConfig(
            kokoro=sherpa.OfflineTtsKokoroModelConfig(
                model=str(model_onnx),
                voices=str(model_dir / "voices.bin"),
                tokens=str(tokens),
                data_dir=str(espeak_dir) if espeak_dir.is_dir() else "",
                lexicon=",".join(lexicons),
                dict_dir=str(dict_dir) if dict_dir.is_dir() else "",
            ),
            num_threads=num_threads,
        )
    elif kind == "vits":
        model_config = sherpa.OfflineTtsModelConfig(
            vits=sherpa.OfflineTtsVitsModelConfig(
                model=str(model_onnx),
                tokens=str(tokens),
                lexicon=str(model_dir / "lexicon.txt") if (model_dir / "lexicon.txt").is_file() else "",
                dict_dir=str(dict_dir) if dict_dir.is_dir() else "",
            ),
            num_threads=num_threads,
        )
    else:  # pragma: no cover - argparse 已限制取值
        raise ValueError(f"不支持的本地 TTS 类型：{kind!r}")

    config = sherpa.OfflineTtsConfig(model=model_config)
    return sherpa.OfflineTts(config)


def resolve_voice(
    raw: object, *, kind: str, num_speakers: int, default_sid: int = 0
) -> tuple[int, str | None]:
    """把请求里的 ``voice`` 解析成 sid；解析不出来退回默认音色并给出警告。"""

    spec = "" if raw is None else str(raw).strip()
    warning: str | None = None
    if spec:
        lowered = spec.lower()
        if lowered.startswith("kokoro:") or lowered.startswith("vits:"):
            prefix, _, rest = lowered.partition(":")
            spec = spec[len(prefix) + 1 :].strip()
            lowered = spec.lower()
        if spec:
            try:
                sid = int(spec, 10)
            except ValueError:
                sid = _VOICE_ALIASES.get(lowered, -1) if kind == "kokoro" else -1
                if sid < 0:
                    warning = f"无法识别的音色 {raw!r}，已退回默认音色 {default_sid}"
                    sid = default_sid
            else:
                if sid < 0 or sid >= max(1, num_speakers):
                    warning = (
                        f"音色编号 {sid} 超出范围（0–{max(0, num_speakers - 1)}），"
                        f"已退回默认音色 {default_sid}"
                    )
                    sid = default_sid
            return sid, warning

    if default_sid < 0 or default_sid >= max(1, num_speakers):
        default_sid = 0
    return default_sid, warning


def write_wav(
    path: Path, samples, sample_rate: int, *, peak_scale: float = 0.95
) -> float:
    """float32 采样 → 16bit 单声道 WAV（峰值缩放到 ``peak_scale`` 避免削波），返回时长（秒）。"""

    import numpy as np

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        raise ValueError("合成结果为空（0 个采样点）")
    peak = float(np.max(np.abs(audio)))
    scale = (peak_scale / peak) if peak > 0 else 1.0
    pcm = np.rint(np.clip(audio * scale, -1.0, 1.0) * 32767.0).astype(np.int16)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return float(audio.size) / float(sample_rate)


def _clamp_speed(raw: object) -> float:
    if not isinstance(raw, (int, float, str)):
        return 1.0
    try:
        speed = float(raw)
    except ValueError:
        return 1.0
    if speed != speed:  # NaN
        return 1.0
    return min(2.0, max(0.5, speed))


def _synthesize(tts, request: dict, *, kind: str) -> dict:
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请求缺少非空字符串字段 text")
    out_raw = request.get("out")
    if not isinstance(out_raw, str) or not out_raw.strip():
        raise ValueError("请求缺少字符串字段 out（输出 wav 路径）")
    out_path = Path(out_raw)

    num_speakers = int(getattr(tts, "num_speakers", 1) or 1)
    sid, warning = resolve_voice(
        request.get("voice"), kind=kind, num_speakers=num_speakers, default_sid=0
    )
    speed = _clamp_speed(request.get("speed", 1.0))

    audio = tts.generate(text, sid=sid, speed=speed)
    sample_rate = int(getattr(audio, "sample_rate", 0) or getattr(tts, "sample_rate", 0))
    if sample_rate <= 0:
        raise RuntimeError("sherpa-onnx 没有返回有效采样率")
    duration = write_wav(out_path, audio.samples, sample_rate)

    payload: dict[str, object] = {
        "ok": True,
        "duration": round(duration, 3),
        "sample_rate": sample_rate,
        "voice": sid,
        "speed": round(speed, 3),
        "out": str(out_path),
    }
    if warning:
        payload["warning"] = warning
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # stdout 是协议通道：把 sys.stdout 改道到 stderr，库里的 print/进度只走 stderr，
    # 协议行统一从启动时抓到的真实 stdout 输出。
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    for stream in (sys.stdin, protocol_out):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):  # pragma: no cover - 管道被换掉时
                pass

    def emit(payload: dict[str, object]) -> None:
        protocol.write_line(protocol_out, payload)

    started = time.monotonic()
    try:
        model_dir = Path(args.model_dir)
        kind = detect_kind(model_dir) if args.kind == "auto" else args.kind
        tts = load_tts(model_dir, kind, args.threads)
    except Exception as exc:  # noqa: BLE001 - 加载失败必须落一行 JSON 再退出
        emit(protocol.error_payload(exc))
        return 1

    emit(
        {
            "ok": True,
            "ready": True,
            "kind": kind,
            "num_speakers": int(getattr(tts, "num_speakers", 1) or 1),
            "sample_rate": int(getattr(tts, "sample_rate", 0) or 0),
            "model": str(model_dir),
            "load_s": round(time.monotonic() - started, 2),
        }
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("请求必须是 JSON 对象")
            payload = _synthesize(tts, request, kind=kind)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能弄崩 worker
            payload = protocol.error_payload(exc)
        emit(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
