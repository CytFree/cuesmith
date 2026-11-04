"""本地 TTS 候选模型横评：sherpa-onnx（VITS/Piper、Kokoro、MeloTTS）CPU 实测。

测三件事（同一个 CPU、同一段文本）：
1. 模型加载耗时 / 磁盘体积；
2. 合成速度（RTF = 音频秒数 / 墙钟秒数，越大越快；RTF>1 即快于实时）；
3. 生成样例 wav 供人工听感对比（"好不好"只能靠耳朵，这里不装客观分）。

用法（sherpa-onnx 环境）：
    spike\\diarization\\.venv\\Scripts\\python.exe tools\\bench_local_tts.py
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import sherpa_onnx as s  # noqa: E402

BASE = ROOT / "spike" / "local-tts"
OUT = ROOT / "work" / "local-tts-eval"
ZH_TEXT = "今天我们来测试一下本地语音合成，看看速度和自然度怎么样。"
EN_TEXT = "Local text to speech is now fast enough to run on a laptop without a GPU."


def vits_config(model_dir: Path, *, model_name: str, tokens: str, data_dir: str | None = None,
                lexicon: str | None = None, dict_dir: str | None = None, threads: int = 8):
    model = model_dir / model_name
    return s.OfflineTtsConfig(
        model=s.OfflineTtsModelConfig(
            vits=s.OfflineTtsVitsModelConfig(
                model=str(model),
                tokens=str(model_dir / tokens),
                data_dir=str(model_dir / data_dir) if data_dir else "",
                lexicon=str(model_dir / lexicon) if lexicon else "",
                dict_dir=str(model_dir / dict_dir) if dict_dir else "",
                length_scale=1.0,
            ),
            num_threads=threads,
        )
    )


def kokoro_config(threads: int = 8):
    model_dir = BASE / "kokoro-multi-lang-v1_0"
    lexicon = ",".join(
        str(model_dir / name)
        for name in ("lexicon-us-en.txt", "lexicon-zh.txt")
    )
    return s.OfflineTtsConfig(
        model=s.OfflineTtsModelConfig(
            kokoro=s.OfflineTtsKokoroModelConfig(
                model=str(model_dir / "model.onnx"),
                voices=str(model_dir / "voices.bin"),
                tokens=str(model_dir / "tokens.txt"),
                data_dir=str(model_dir / "espeak-ng-data"),
                lexicon=lexicon,
                dict_dir=str(model_dir / "dict"),
            ),
            num_threads=threads,
        )
    )


def piper_zh(threads: int = 8):
    return vits_config(
        BASE / "vits-piper-zh_CN-huayan-medium",
        model_name="zh_CN-huayan-medium.onnx",
        tokens="tokens.txt",
        data_dir="espeak-ng-data",
        threads=threads,
    )


def piper_en(threads: int = 8):
    return vits_config(
        BASE / "vits-piper-en_US-amy-medium",
        model_name="en_US-amy-medium.onnx",
        tokens="tokens.txt",
        data_dir="espeak-ng-data",
        threads=threads,
    )


def melo(threads: int = 8):
    return vits_config(
        BASE / "vits-melo-tts-zh_en",
        model_name="model.onnx",
        tokens="tokens.txt",
        lexicon="lexicon.txt",
        dict_dir="dict",
        threads=threads,
    )


def dir_size(path: Path) -> float:
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return total / 1024 / 1024


def save_wav(path: Path, samples, sample_rate: int) -> None:
    audio = np.asarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio / peak * 0.95
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())


def run_case(label: str, config_factory, model_dir: Path, *, voice: int, text: str,
             lang: str, repeats: int = 3) -> dict:
    started = time.monotonic()
    try:
        tts = s.OfflineTts(config_factory())
    except Exception as exc:  # noqa: BLE001 - 横评脚本要给出失败原因
        print(f"  [{label}] 加载失败：{type(exc).__name__}: {exc}")
        return {"label": label, "error": str(exc)}
    load_s = time.monotonic() - started
    try:
        # 预热一次（分配内存/首次编译），不计时
        tts.generate(text, sid=voice, speed=1.0)
        walls, durations = [], []
        for _ in range(repeats):
            started = time.monotonic()
            audio = tts.generate(text, sid=voice, speed=1.0)
            walls.append(time.monotonic() - started)
            durations.append(len(audio.samples) / audio.sample_rate)
        sample_path = OUT / f"{label}-{lang}.wav"
        save_wav(sample_path, audio.samples, audio.sample_rate)
        wall = sum(walls) / len(walls)
        duration = sum(durations) / len(durations)
        rtf = duration / wall if wall else 0.0
        print(
            f"  [{label}] {lang}: 音频 {duration:5.2f}s / 合成 {wall * 1000:6.0f}ms "
            f"= RTF {rtf:5.2f}  加载 {load_s:4.2f}s  样例 {sample_path.name}"
        )
        return {
            "label": label,
            "lang": lang,
            "voice": voice,
            "model_mb": round(dir_size(model_dir), 1) if model_dir.is_dir() else None,
            "sample_rate": int(audio.sample_rate),
            "audio_s": round(duration, 2),
            "synth_ms": round(wall * 1000, 0),
            "rtf": round(rtf, 2),
            "load_s": round(load_s, 2),
            "sample": str(sample_path.relative_to(ROOT)),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  [{label}] 合成失败：{type(exc).__name__}: {exc}")
        return {"label": label, "error": str(exc)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    print("== Piper（VITS，单说话人）")
    results.append(
        run_case("piper-zh-huayan", piper_zh, BASE / "vits-piper-zh_CN-huayan-medium",
                 voice=0, text=ZH_TEXT, lang="zh")
    )
    results.append(
        run_case("piper-en-amy", piper_en, BASE / "vits-piper-en_US-amy-medium",
                 voice=0, text=EN_TEXT, lang="en")
    )

    print("== Kokoro-82M multi-lang（54 voices）")
    for label, sid, text, lang in (
        ("kokoro-zh-xiaoxiao", 47, ZH_TEXT, "zh"),
        ("kokoro-zh-yunxi", 50, ZH_TEXT, "zh"),
        ("kokoro-en-heart", 3, EN_TEXT, "en"),
        ("kokoro-en-michael", 16, EN_TEXT, "en"),
    ):
        results.append(
            run_case(label, kokoro_config, BASE / "kokoro-multi-lang-v1_0",
                     voice=sid, text=text, lang=lang)
        )

    print("== MeloTTS zh+en（单女声）")
    results.append(
        run_case("melo-zh_en", melo, BASE / "vits-melo-tts-zh_en",
                 voice=0, text=ZH_TEXT, lang="zh")
    )
    results.append(
        run_case("melo-zh_en", melo, BASE / "vits-melo-tts-zh_en",
                 voice=0, text=EN_TEXT, lang="en")
    )

    import json

    (OUT / "result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\n样例与结果：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
