"""测试共用的外部依赖开关与素材构造。

这里只放"多个文件里真的是同一件事"的东西：

* ``requires_media_tools`` / ``requires_ffmpeg`` —— 全仓 8 个测试文件各自
  ``shutil.which`` + ``pytest.mark.skipif`` 了一遍，条件串还是手抄的，
  改一次漏一个。收成两条常量后，跑 ``pytest -rs`` 看到的跳过理由只有一处来源。
* ``build_test_video`` —— 3 个文件各抄了一份 lavfi 造片命令，差别只在时长/颜色/分辨率。

**不**收进来的：各文件的 ``write_srt``。那几个同名函数签名互不相同
（``(path, start, end, text)`` / ``(path, text)`` / ``(path, *cues)``），
合并只能选其中一种再让其余调用点全都改写，等于拿一层间接换掉三行直白代码。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_media_tools = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg / ffprobe 不在 PATH"
)
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不在 PATH")


def build_test_video(
    path: Path, *, seconds: float, color: str, size: str
) -> Path:
    """造一段带正弦音轨的纯色测试视频。

    参数全部必填：这些用例的断言会看像素和时长，任何一项取了默认值都可能
    在改默认值时被无声改掉。
    """
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={seconds}:r=25",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        capture_output=True,
        check=True,
    )
    return path
