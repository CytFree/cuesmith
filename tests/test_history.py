"""任务历史（OPT-8）：sqlite 持久化 + GUI 展示/重跑的数据层。

不 import gradio；GUI 侧的组装测试在 ``tests/test_gui.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from submatrix.history import (
    HISTORY_HEADERS,
    STATUS_LABELS,
    HistoryError,
    HistoryStore,
    history_db_path,
    history_rows,
    status_for_exit,
    task_choice_id,
    task_choice_label,
)


# ---------------------------------------------------------------- 基础


def test_history_db_path_resolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SUBMATRIX_HISTORY_DB", raising=False)
    assert history_db_path().name == "history.db"

    explicit = tmp_path / "custom.db"
    assert history_db_path(explicit) == explicit

    monkeypatch.setenv("SUBMATRIX_HISTORY_DB", str(tmp_path / "env.db"))
    assert history_db_path() == tmp_path / "env.db"


def test_status_for_exit_mapping() -> None:
    assert status_for_exit(0) == "ok"
    assert status_for_exit(2) == "partial"
    assert status_for_exit(1) == "failed"
    assert status_for_exit(None) == "running"
    assert status_for_exit(0, cancelled=True) == "cancelled"
    assert set(STATUS_LABELS) >= {"running", "ok", "partial", "failed", "cancelled"}


def test_store_add_get_finish_and_rows(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    task_id = store.add_task(
        source=r"D:\videos\a.mp4",
        out_dir=r"D:\out\a",
        settings={"src_lang": "zh", "features": {"dub", "burn"}, "glossary": Path("terms.json")},
        argv=["-m", "submatrix", "pipeline", "--src", r"D:\videos\a.mp4"],
    )

    task = store.get_task(task_id)
    assert task is not None
    assert task["status"] == "running"
    assert task["created_at"] and task["updated_at"]
    assert task["settings"]["features"] == ["burn", "dub"]  # set → 排序后的 list
    assert task["settings"]["glossary"] == "terms.json"  # Path → str
    assert task["argv"][:3] == ["-m", "submatrix", "pipeline"]
    assert store.count() == 1

    store.finish_task(
        task_id,
        exit_code=0,
        cost={"display": {"currency": "CNY", "amount": 0.0123}},
        stages={"probe": "ok", "dub": "ok"},
        speakers=3,
        wall_s=12.5,
    )
    task = store.get_task(task_id)
    assert task["status"] == "ok" and task["exit_code"] == 0
    assert task["cost"]["display"]["amount"] == 0.0123
    assert task["stages"] == {"probe": "ok", "dub": "ok"}
    assert task["speakers"] == 3 and task["wall_s"] == 12.5

    rows = history_rows(store.list_tasks())
    assert len(rows) == 1
    assert rows[0][0] == str(task_id)
    assert rows[0][1]  # 时间
    assert rows[0][2] == "a.mp4"
    assert rows[0][3] == r"D:\out\a"
    assert rows[0][4] == "完成"
    assert rows[0][5] == "3"
    assert rows[0][6] == "¥0.0123"
    assert rows[0][7] == "12.5s"


def test_store_failure_partial_and_cancel(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    failed = store.add_task(source="a.mp4", out_dir="out/a")
    store.finish_task(failed, exit_code=1, error="pipeline.status=partial；配音失败")
    partial = store.add_task(source="b.mp4", out_dir="out/b")
    store.finish_task(partial, exit_code=2, error="dub: TTS failed")
    cancelled = store.add_task(source="c.mp4", out_dir="out/c")
    store.finish_task(cancelled, exit_code=1, cancelled=True)

    tasks = {task["id"]: task for task in store.list_tasks()}
    assert tasks[failed]["status"] == "failed"
    assert tasks[failed]["error"].startswith("pipeline.status")
    assert tasks[partial]["status"] == "partial"
    assert tasks[cancelled]["status"] == "cancelled"

    labels = [row[4] for row in history_rows(store.list_tasks())]
    assert labels == ["已取消", "部分失败", "失败"]


def test_store_list_limit_order_and_delete(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    for index in range(5):
        store.add_task(source=f"{index}.mp4", out_dir=f"out/{index}")

    newest_first = store.list_tasks(limit=3)
    assert [task["source"] for task in newest_first] == ["4.mp4", "3.mp4", "2.mp4"]
    assert [task["source"] for task in store.list_tasks(limit=100)] == [
        "4.mp4", "3.mp4", "2.mp4", "1.mp4", "0.mp4"
    ]
    assert len(store.list_tasks(status="running")) == 5
    assert store.list_tasks(status="ok") == []

    assert store.delete_task(newest_first[0]["id"]) is True
    assert store.delete_task(999999) is False
    assert store.count() == 4


def test_store_rejects_unusable_path(tmp_path: Path) -> None:
    blocker = tmp_path / "file.bin"
    blocker.write_bytes(b"x")
    with pytest.raises(HistoryError):
        HistoryStore(blocker / "sub" / "history.db")


def test_task_choice_label_and_id_roundtrip(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.db")
    task_id = store.add_task(source=r"D:\v\发布会.mp4", out_dir="out")
    task = store.get_task(task_id)

    label = task_choice_label(task)
    assert label.startswith(f"{task_id} · 发布会.mp4")
    assert task_choice_id(label) == task_id
    assert task_choice_id(f"{task_id}") == task_id
    assert task_choice_id(None) is None
    assert task_choice_id("乱写的选项") is None


def test_history_rows_empty_and_headers() -> None:
    assert history_rows([]) == []
    assert HISTORY_HEADERS[:2] == ["ID", "时间"]


def test_cli_history_lists_and_deletes(tmp_path: Path, capsys) -> None:
    from submatrix.cli import main

    db = tmp_path / "cli.db"
    store = HistoryStore(db)
    task_id = store.add_task(source="a.mp4", out_dir="out/a")
    store.finish_task(task_id, exit_code=0)

    assert main(["history", "--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "任务历史" in output and "完成" in output

    assert main(["history", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == task_id and payload[0]["status"] == "ok"

    assert main(["history", "--db", str(db), "--delete", str(task_id)]) == 0
    assert "已删除" in capsys.readouterr().out
    assert store.count() == 0

    assert main(["history", "--db", str(db), "--delete", "999"]) == 1
    assert "没有 id=999" in capsys.readouterr().err
