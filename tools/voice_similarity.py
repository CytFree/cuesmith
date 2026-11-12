"""音色相似度检查：用 3D-Speaker 声纹模型比较「参考音频」和「TTS 合成音频」的说话人相似度。

用途：声音复刻接入后做**客观**验证（不能只靠"听着像"）：

    # 参考 = 复刻用的原始录音；候选 = 克隆音色合成的音频
    spike/diarization/.venv/Scripts/python.exe tools/voice_similarity.py `
        --reference work/dashscope/refs/ref-S0.wav `
        --candidate work/dashscope/clone-S0-zh.wav `
        --candidate work/dashscope/clone-S0-en.wav `
        --candidate work/dashscope/refs/ref-S1.wav

- 相似度 = 两个音频的声纹余弦相似度（同一模型，模型路径默认取 spike/diarization/models/3dspeaker*.onnx）
- 经验参考：同一人通常 >0.6，不同人通常 <0.3（具体随素材/模型浮动，只做相对比较）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = next(
    (ROOT / "spike" / "diarization" / "models").glob("3dspeaker*.onnx"), None
)
SAMPLE_RATE = 16000


def _to_16k_wav(src: Path, dst: Path) -> None:
    completed = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst),
        ],
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"转码失败（{src.name}）：{completed.stderr.decode('utf-8', 'replace')[-300:]}"
        )


def _read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise RuntimeError(f"需要 16bit PCM：{path}")
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def _embedding(extractor, path: Path) -> "list[float]":
    samples, rate = _read_wav(path)
    if rate != SAMPLE_RATE:
        raise RuntimeError(f"采样率不是 {SAMPLE_RATE}：{path}")
    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    stream.input_finished()
    return extractor.compute(stream)


def _cosine(left, right) -> float:
    import numpy as np

    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    denominator = float((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0)
    return float(np.dot(a, b) / denominator)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="声纹相似度检查（复刻效果客观验证）")
    parser.add_argument("--reference", type=Path, action="append", required=True, help="参考音频（可多个）")
    parser.add_argument("--candidate", type=Path, action="append", required=True, help="待比对音频（可多个）")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="声纹模型 .onnx")
    parser.add_argument("--output", type=Path, default=None, help="把结果写成 JSON")
    args = parser.parse_args(argv)

    model = Path(args.model) if args.model else None
    if model is None or not model.is_file():
        print(f"找不到声纹模型：{model}", file=sys.stderr)
        return 1

    import numpy as np
    import sherpa_onnx

    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2)
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="cuesmith-voice-sim-"))
    results: list[dict[str, object]] = []
    try:
        reference_embeddings = {}
        for index, path in enumerate(args.reference):
            wav = temp_dir / f"ref-{index}.wav"
            _to_16k_wav(path, wav)
            reference_embeddings[path.name] = _embedding(extractor, wav)
        for index, path in enumerate(args.candidate):
            wav = temp_dir / f"cand-{index}.wav"
            _to_16k_wav(path, wav)
            embedding = _embedding(extractor, wav)
            for ref_name, ref_embedding in reference_embeddings.items():
                similarity = _cosine(ref_embedding, embedding)
                results.append(
                    {
                        "reference": ref_name,
                        "candidate": path.name,
                        "similarity": round(similarity, 4),
                    }
                )
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"{'reference':<18}{'candidate':<24}similarity")
    for item in results:
        print(f"{item['reference']:<18}{item['candidate']:<24}{item['similarity']:.4f}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"model": str(model), "reference": [p.name for p in args.reference], "results": results},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n结果：{args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
