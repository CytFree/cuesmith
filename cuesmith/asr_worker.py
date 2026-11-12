"""faster-whisper 工作进程：由 ``cuesmith asr`` 用"装有 faster-whisper 的 Python"拉起。

为什么不直接在 CLI 进程里 import：主包只依赖标准库 + ffmpeg（PRD 锁定），ASR 依赖
ctranslate2 / onnxruntime，版本敏感（4.8.x 在 Windows 上会访问违例，M0 已踩），必须隔离在
底座 venv 里跑。本文件可被任意 Python 3.10+ 执行，只输出一个 JSON 文件。

输出（``--output``）：

```json
{"ok": true, "language": "en", "language_probability": 0.99, "duration": 120.0,
 "words": [{"start": 0.0, "end": 0.16, "word": " We're", "probability": 0.79}, ...],
 "segments": [{"start": 0.0, "end": 2.2, "text": " We're going to see this massive renaissance."}]}
```
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__:
    from . import worker_protocol as protocol
else:  # 脚本直跑：sys.path[0] 是 cuesmith/，只能按顶层模块导入
    import worker_protocol as protocol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="faster-whisper worker（词级时间戳）")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", default=None, help="语言代码；默认自动识别")
    parser.add_argument("--hotwords", default=None, help="热词/术语偏置（逗号或空格分隔）")
    parser.add_argument("--initial-prompt", default=None)
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--cpu-threads", type=int, default=0)
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=args.cpu_threads or 0,
        )
        segments, info = model.transcribe(
            str(args.audio),
            language=args.language or None,
            beam_size=args.beam_size,
            word_timestamps=True,
            vad_filter=not args.no_vad,
            condition_on_previous_text=False,
            initial_prompt=args.initial_prompt or None,
            hotwords=args.hotwords or None,
        )
        words: list[dict[str, object]] = []
        realized: list[dict[str, object]] = []
        for segment in segments:
            segment_words = [
                {
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                    "word": word.word,
                    "probability": round(word.probability, 4),
                }
                for word in (segment.words or [])
            ]
            words.extend(segment_words)
            realized.append(
                {
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "text": segment.text,
                    "no_speech_prob": round(segment.no_speech_prob, 4),
                    "words": segment_words,
                }
            )
            if len(realized) % 25 == 0:
                protocol.emit_progress(f"… {len(realized)} 段 / {len(words)} 词")

        payload = protocol.ok_payload(
            language=info.language,
            language_probability=round(float(info.language_probability), 4),
            duration=round(float(info.duration), 3),
            duration_after_vad=round(float(info.duration_after_vad), 3),
            words=words,
            segments=realized,
            wall_s=round(time.monotonic() - started, 2),
            model=str(args.model),
        )
        code = 0
    except Exception as exc:  # noqa: BLE001 - 失败也要落 JSON 给父进程看
        payload = protocol.error_payload(exc, wall_s=round(time.monotonic() - started, 2))
        code = 1

    protocol.write_payload(args.output, payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
