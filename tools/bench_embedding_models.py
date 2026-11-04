"""OPT-9 评估：英文声纹模型（small / large / resnet34）单窗 embedding 耗时对比。

只测 SpeakerEmbeddingExtractor 的 compute 开销（diarize 里 clustering 阶段的一部分），
用来估算换大模型对 CPU 耗时的增量，以及 GPU 上可能的收益上限。

用法（diarization venv）：
    spike\\diarization\\.venv\\Scripts\\python.exe tools\\bench_embedding_models.py
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

MODELS = {
    "titanet-small": ROOT / "spike/diarization/models/nemo_en_titanet_small.onnx",
    "titanet-large": ROOT / "spike/diarization/models/nemo_en_titanet_large.onnx",
    "wespeaker-resnet34": ROOT / "spike/diarization/models/wespeaker_en_voxceleb_resnet34.onnx",
}
WAV = ROOT / "work/real-samples/opt7b-en-10min/source.wav"
WIN = 1.5
COUNT = 128
THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 4


def load(path: Path):
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    return samples.astype(np.float32) / 32768.0, sr


def main() -> int:
    samples, sr = load(WAV)
    duration = len(samples) / sr
    starts = np.linspace(0, max(0.0, duration - WIN), COUNT)
    windows = [(int(start * sr), int((start + WIN) * sr)) for start in starts]
    print(f"audio {duration:.1f}s, windows {COUNT} x {WIN}s, threads {THREADS}")

    for name, model in MODELS.items():
        if not model.is_file():
            print(f"{name}: model missing ({model})")
            continue
        config = s.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=THREADS)
        extractor = s.SpeakerEmbeddingExtractor(config)
        dim = int(extractor.dim)
        # 预热 2 个窗（模型加载/内存分配不算进单窗耗时）
        for args in windows[:2]:
            stream = extractor.create_stream()
            stream.accept_waveform(sr, samples[args[0] : args[1]])
            stream.input_finished()
            extractor.compute(stream)
        started = time.monotonic()
        for begin, end in windows:
            stream = extractor.create_stream()
            stream.accept_waveform(sr, samples[begin:end])
            stream.input_finished()
            extractor.compute(stream)
        elapsed = time.monotonic() - started
        print(
            f"{name:20s} dim={dim:4d}  {elapsed / COUNT * 1000:7.2f} ms/window  "
            f"total {elapsed:6.2f}s for {COUNT} windows"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
