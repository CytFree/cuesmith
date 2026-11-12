"""产物落盘：先写临时文件再 ``os.replace``，崩溃不会留下半截 JSON。

为什么单独一个模块：报告/清单是**缓存判据的来源**，半截文件比没有文件更坏 ——
上一次跑崩在写一半，下一次会读到语法错误的 JSON，然后以"产物损坏"的形式失败，
排查方向会被带偏。原子写把失败模式收敛成"要么旧文件、要么新文件"。

本模块在 L0（零内部依赖），所以任何层都能用，不必为了这个 helper 改分层。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["write_json", "write_text"]


def write_text(path: Path, text: str) -> None:
    """原子写文本文件（UTF-8、LF）：失败时原文件保持不变，临时文件必被清理。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        # 成功时 tmp 已被 replace 掉；失败时（写入异常/替换异常）在这里清掉
        tmp.unlink(missing_ok=True)


def write_json(path: Path, data: object) -> None:
    """原子写 JSON（``ensure_ascii=False`` / 缩进 2 / 结尾换行）——报告的统一格式。"""

    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
