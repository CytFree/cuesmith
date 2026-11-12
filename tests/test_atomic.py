"""原子写的契约：要么旧文件、要么新文件，不留半截、不留临时文件。"""

from __future__ import annotations

import json
import os

import pytest

from cuesmith import atomic


def test_write_text_replaces_content_without_leaving_temp(tmp_path) -> None:
    target = tmp_path / "a" / "report.json"
    atomic.write_text(target, "first")
    assert target.read_text(encoding="utf-8") == "first"
    atomic.write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert not list(target.parent.glob(".*.tmp"))


def test_write_json_format_is_stable(tmp_path) -> None:
    target = tmp_path / "report.json"
    atomic.write_json(target, {"中文": 1, "b": [1, 2]})
    text = target.read_text(encoding="utf-8")
    assert text == json.dumps({"中文": 1, "b": [1, 2]}, ensure_ascii=False, indent=2) + "\n"
    assert not text.endswith("\r\n")


def test_creates_parent_directories(tmp_path) -> None:
    target = tmp_path / "deep" / "nested" / "out.json"
    atomic.write_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_failed_replace_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch) -> None:
    """替换那一步失败（磁盘满 / 权限）时，旧文件必须原样保留、临时文件不留。"""

    target = tmp_path / "report.json"
    atomic.write_json(target, {"v": 1})

    def boom(src, dst):  # noqa: ANN001, ARG001
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        atomic.write_json(target, {"v": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert not list(tmp_path.glob(".*.tmp"))


def test_failed_write_cleans_temp(tmp_path, monkeypatch) -> None:
    """写入那一步失败（编码/磁盘）时也不能留临时文件。"""

    target = tmp_path / "report.json"

    def boom(*args, **kwargs):  # noqa: ANN001, ARG001
        raise OSError("no space")

    monkeypatch.setattr(atomic.os, "fsync", boom)
    with pytest.raises(OSError):
        atomic.write_json(target, {"v": 1})
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_real_os_replace_is_used(tmp_path, monkeypatch) -> None:
    """确认走的是 os.replace（而不是直接覆盖写）：临时文件与目标不在同一路径。"""

    calls: list[tuple[str, str]] = []
    real = os.replace

    def spy(src, dst):  # noqa: ANN001
        calls.append((str(src), str(dst)))
        return real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", spy)
    target = tmp_path / "report.json"
    atomic.write_json(target, {"v": 1})
    assert len(calls) == 1
    assert calls[0][1] == str(target)
    assert calls[0][0] != str(target)
