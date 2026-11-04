"""OPT-8 真实验证：GUI 跑一条 → 历史库落记录 → 重跑 → 重启后 CLI 仍可查。

需要一个已经启动的 GUI（`python -m submatrix gui --port 7865 --no-browser`）：

    spike\\gui\\.venv\\Scripts\\python tools\\gui_history_real.py --port 7865

用真实 gradio_client 走 HTTP，不 mock；产物写到 ``work/gui-opt8-e2e/``。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gradio_client import Client  # noqa: E402

from submatrix.history import (  # noqa: E402
    HistoryStore,
    history_rows,
    task_choice_label,
)

OUT = ROOT / "work" / "gui-opt8-e2e"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OPT-8 GUI 任务历史真实验证")
    parser.add_argument("--port", type=int, default=7865)
    args = parser.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    source = ROOT / "spike" / "samples" / "S1_softsub.mkv"
    out_dir = OUT / "run"
    report: dict[str, object] = {}

    client = Client(
        f"http://127.0.0.1:{args.port}/",
        verbose=False,
        download_files=False,
        httpx_kwargs={"timeout": 1200},
    )
    endpoints = sorted((client.view_api(return_format="dict") or {}).get("named_endpoints", {}))
    print("endpoints:", endpoints)
    report["endpoints"] = endpoints

    store = HistoryStore()
    before = store.count()
    started = time.monotonic()
    status, log, files, batch, cost, preview, speakers, speaker_value = client.predict(
        str(source),
        None,
        str(out_dir),
        "zh",
        "en",
        ["AIGC 标识"],
        "默认（按目标语言）",
        0,
        "",
        "",
        "",
        False,
        False,
        None,
        "auto",
        "",
        "",
        api_name="/stream_run",
    )
    elapsed = round(time.monotonic() - started, 1)
    print(f"run wall {elapsed}s\nstatus: {status}")
    report["run"] = {"wall_s": elapsed, "status": status, "batch": batch}

    tasks = store.list_tasks(limit=3)
    after = store.count()
    task = tasks[0] if tasks else None
    print("tasks:", after, "before:", before)
    for row in history_rows(tasks):
        print("  row:", row)
    assert after == before + 1, f"历史没有新增：{before} → {after}"
    assert task is not None and task["status"] == "ok", task
    assert task["source"] == str(source)
    assert Path(task["out_dir"]) == out_dir
    assert "stream_run" in task["argv"] or "pipeline" in task["argv"]
    report["task"] = {
        "id": task["id"],
        "status": task["status"],
        "exit_code": task["exit_code"],
        "stages": task["stages"],
        "cost": task["cost"],
        "argv": task["argv"],
        "settings": task["settings"],
    }

    choice = task_choice_label(task)
    before_rerun = store.count()
    status2, *_rest = client.predict(choice, api_name="/rerun_history")
    print(f"rerun status: {status2}")
    time.sleep(0.5)
    tasks2 = store.list_tasks(limit=5)
    replay = next((item for item in tasks2 if item.get("replay_of") == task["id"]), None)
    print("replay task:", replay and (replay["id"], replay["status"], replay["replay_of"]))
    assert store.count() == before_rerun + 1
    assert replay is not None and replay["status"] == "ok"
    report["rerun"] = {
        "status": status2,
        "task_id": replay["id"],
        "replay_of": replay["replay_of"],
        "argv": replay["argv"],
    }

    # 「刷新」与「载入参数」也走一遍真实 HTTP（确认页面重启后能看到记录、表单能回填）
    rows_payload, choices_payload, note = client.predict(api_name="/history_refresh")
    data = rows_payload.get("data") if isinstance(rows_payload, dict) else rows_payload
    if isinstance(choices_payload, dict):
        choices = choices_payload.get("choices") or []
    else:
        choices = choices_payload or []
    print("refresh rows:", len(data or []), "choices:", len(choices), "note:", note)
    assert len(data or []) >= 2 and len(choices) >= 2
    loaded = client.predict(choice, api_name="/_load_history")
    print("load note:", loaded[-1])
    assert "已载入任务" in str(loaded[-1])
    report["refresh"] = {"rows": len(data or []), "choices": len(choices), "note": note}
    report["load_note"] = loaded[-1]

    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"BASE={OUT}")
    print("GUI 历史 e2e OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
