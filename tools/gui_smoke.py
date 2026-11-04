"""GUI 冒烟：用真实 gradio 无头启动，HTTP GET / 返回 200 后关闭。

在有 gradio 的解释器里跑（仓库自带：spike/gui/.venv）：

    spike\\gui\\.venv\\Scripts\\python tools\\gui_smoke.py

不占固定端口（默认随机空闲端口），不打开浏览器，不跑 pipeline。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Gradio 默认把临时文件放到 cwd/gradio；冒烟时也固定到 work/.gui-tmp，别弄脏仓库根
os.environ.setdefault("GRADIO_TEMP_DIR", str(REPO_ROOT / "work" / ".gui-tmp"))
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gradio GUI 无头冒烟")
    parser.add_argument("--port", type=int, default=None, help="固定端口（默认随机）")
    args = parser.parse_args(argv)

    import gradio as gr

    from submatrix.gui import build_app

    port = args.port or free_port()
    app = build_app()
    app.queue()
    app.launch(
        server_name="127.0.0.1",
        server_port=port,
        prevent_thread_lock=True,
        inbrowser=False,
        quiet=True,
        allowed_paths=[str(REPO_ROOT)],
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            body = response.read()
            print(f"gradio {gr.__version__}  HTTP {response.status}  {len(body)} bytes  {url}")
            assert response.status == 200, response.status
            assert b"<html" in body.lower(), "首页不是 HTML"
    finally:
        app.close()
    print("GUI smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
