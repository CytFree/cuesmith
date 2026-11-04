"""补齐样本集里能自动生成的三类边界（PRD §6 #9/#10/#11 的近似）。

- `portrait.mkv`：竖屏 9:16（720×1280）+ 音轨 + 文本字幕轨
- `silent.mp4`：无音轨、无字幕（静音边界）
- `long1h.mkv`：1 小时视频 + 600 条字幕（长视频落盘/时间轴边界）

用法：python tools/make_extra_samples.py [--out work/samples-v2]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SENTENCE_ZH = "这是一段用于长视频边界测试的中文字幕内容。"
SENTENCE_EN = "This is a subtitle line used for the long video boundary test."
SHORT_ZH = (
    "1\n00:00:00,000 --> 00:00:02,000\n你好世界\n"
    "\n"
    "2\n00:00:02,500 --> 00:00:04,500\n测试字幕轨道\n"
)


def ts(ms: int) -> str:
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(path: Path, text: str, count: int, slot_ms: int) -> None:
    blocks = []
    for index in range(count):
        start = index * slot_ms
        blocks.append(f"{index + 1}\n{ts(start)} --> {ts(start + slot_ms)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")


def ffmpeg(*args: str) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.decode("utf-8", "replace")[:800])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成边界样本")
    parser.add_argument("--out", type=Path, default=Path("work/samples-v2"))
    args = parser.parse_args(argv)

    out: Path = (ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    short_srt = out / "short.srt"
    short_srt.write_text(SHORT_ZH, encoding="utf-8", newline="\n")

    print("1) 竖屏 9:16（720x1280，含字幕轨）")
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=#223344:s=720x1280:d=5:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-i", str(short_srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-c:s", "srt", "-metadata:s:s:0", "language=chi",
        str(out / "portrait.mkv"),
    )

    print("2) 静音视频（无音轨、无字幕）")
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=#101010:s=640x360:d=5:r=25",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
        str(out / "silent.mp4"),
    )

    print("3) 1 小时视频 + 600 条字幕（编码中，约 1 分钟）")
    long_zh = out / "long_zh.srt"
    long_en = out / "long_en.srt"
    write_srt(long_zh, SENTENCE_ZH, 600, 6000)
    write_srt(long_en, SENTENCE_EN, 600, 6000)
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=#1a1a2e:s=320x180:d=3600:r=2",
        "-i", str(long_zh),
        "-map", "0:v", "-map", "1:s",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "45", "-pix_fmt", "yuv420p",
        "-c:s", "srt", "-metadata:s:s:0", "language=chi",
        str(out / "long1h.mkv"),
    )

    for name in ("portrait.mkv", "silent.mp4", "long1h.mkv", "long_zh.srt", "long_en.srt"):
        path = out / name
        print(f"   {path.name:14} {path.stat().st_size / 1024:10.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
