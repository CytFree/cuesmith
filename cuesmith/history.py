"""任务历史（OPT-8）：用 sqlite 持久化 GUI 跑过的任务，关页面/重启后仍可查、可重跑。

设计取舍（薄壳原则）：

- **只用标准库**（``sqlite3``/``json``），主包仍保持"标准库 + ffmpeg"零运行时依赖；
- 每个处理单元（批量里的一条视频）一行：源文件、输出目录、GUI 表单参数、实际 argv、
  状态/退出码、成本块、逐阶段状态、说话人数、墙钟、错误摘要；
- **不存 API Key**（历史只记参数，重跑 LLM 任务时由用户重新填写/走环境变量）；
- 默认库文件 ``work/history.db``，可用 ``--db`` 或环境变量 ``CUESMITH_HISTORY_DB`` 覆盖。

表结构（``tasks``）：id / created_at / updated_at / source / out_dir / status /
exit_code / settings(JSON) / argv(JSON) / cost(JSON) / stages(JSON) / speakers /
wall_s / error / replay_of。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

__all__ = [
    "HISTORY_HEADERS",
    "REPO_ROOT",
    "STATUS_LABELS",
    "STATUS_ORDER",
    "HistoryError",
    "HistoryStore",
    "history_db_path",
    "history_rows",
    "task_choice_label",
    "task_choice_id",
    "status_for_exit",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_NAME = "history.db"

STATUS_LABELS: dict[str, str] = {
    "running": "运行中",
    "ok": "完成",
    "partial": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
}
STATUS_ORDER = ("running", "ok", "partial", "failed", "cancelled")

HISTORY_HEADERS = ["ID", "时间", "来源", "输出目录", "状态", "人数", "成本", "用时"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    out_dir TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    settings TEXT,
    argv TEXT,
    cost TEXT,
    stages TEXT,
    speakers INTEGER,
    wall_s REAL,
    error TEXT,
    replay_of INTEGER
)
"""

_JSON_COLUMNS = ("settings", "argv", "cost", "stages")

_STATUS_BY_EXIT = {0: "ok", 2: "partial"}


class HistoryError(RuntimeError):
    """历史库不可用（路径不可写/文件损坏等）。"""


def history_db_path(explicit: str | Path | None = None) -> Path:
    """历史库路径：显式参数 > ``CUESMITH_HISTORY_DB`` > 项目内 ``work/history.db``。"""

    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CUESMITH_HISTORY_DB")
    if env:
        return Path(env).expanduser()
    return REPO_ROOT / "work" / DEFAULT_DB_NAME


def status_for_exit(exit_code: int | None, *, cancelled: bool = False) -> str:
    """退出码 → 状态：0 完成、2 部分失败、其余失败；被取消优先。"""

    if cancelled:
        return "cancelled"
    if exit_code is None:
        return "running"
    return _STATUS_BY_EXIT.get(int(exit_code), "failed")


def _json_default(value: object) -> object:
    # GUI 的 features 是 set；统一序列化成排序后的 list，读回时保持 list（build_argv 接受任何可迭代）
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不能写入历史库的类型：{type(value).__name__}")


def _dumps(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _loads(value: object) -> object:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class HistoryStore:
    """任务历史库（每次操作新开连接；本地单用户场景足够）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = history_db_path(db_path)
        self._ensure()

    # ---------------------------------------------------------------- 连接
    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(str(self.path), timeout=10)
        except sqlite3.Error as exc:  # pragma: no cover - 罕见（权限/磁盘）
            raise HistoryError(f"打不开历史库 {self.path}：{exc}") from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"初始化历史库失败 {self.path}：{exc}") from exc

    # ---------------------------------------------------------------- 写入
    def add_task(
        self,
        *,
        source: str,
        out_dir: str,
        settings: dict | None = None,
        argv: Sequence[str] | None = None,
        status: str = "running",
        replay_of: int | None = None,
        created_at: datetime | None = None,
    ) -> int:
        stamp = (created_at or datetime.now().astimezone()).isoformat(timespec="seconds")
        values = (
            stamp,
            stamp,
            str(source),
            str(out_dir),
            str(status),
            None,
            _dumps(settings),
            _dumps(list(argv) if argv is not None else None),
            None,
            None,
            None,
            None,
            None,
            replay_of,
        )
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tasks (created_at, updated_at, source, out_dir, status, exit_code,"
                    " settings, argv, cost, stages, speakers, wall_s, error, replay_of)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                return int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise HistoryError(f"写历史失败：{exc}") from exc

    def update_task(self, task_id: int, /, **fields: object) -> None:
        """只允许更新白名单列；JSON 列自动序列化。"""

        allowed = {
            "status",
            "exit_code",
            "cost",
            "stages",
            "speakers",
            "wall_s",
            "error",
            "settings",
            "argv",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        columns = []
        values: list[object] = []
        for key, value in updates.items():
            columns.append(f"{key} = ?")
            if key in _JSON_COLUMNS:
                values.append(_dumps(value))
            else:
                values.append(value)
        columns.append("updated_at = ?")
        values.append(_now())
        values.append(int(task_id))
        try:
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE tasks SET {', '.join(columns)} WHERE id = ?",  # noqa: S608 - 列名白名单
                    values,
                )
        except sqlite3.Error as exc:
            raise HistoryError(f"更新历史失败：{exc}") from exc

    def finish_task(
        self,
        task_id: int,
        *,
        exit_code: int | None,
        cancelled: bool = False,
        cost: dict | None = None,
        stages: dict | None = None,
        speakers: int | None = None,
        wall_s: float | None = None,
        error: str | None = None,
    ) -> None:
        self.update_task(
            task_id,
            status=status_for_exit(exit_code, cancelled=cancelled),
            exit_code=exit_code,
            cost=cost,
            stages=stages,
            speakers=speakers,
            wall_s=wall_s,
            error=error,
        )

    def delete_task(self, task_id: int) -> bool:
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise HistoryError(f"删除历史失败：{exc}") from exc

    # ---------------------------------------------------------------- 读取
    def list_tasks(self, limit: int = 100, *, status: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        query = "SELECT * FROM tasks"
        params: list[object] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(f"读取历史失败：{exc}") from exc
        return [self._row_to_dict(row) for row in rows]

    def get_task(self, task_id: int) -> dict | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (int(task_id),)
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"读取历史失败：{exc}") from exc
        return self._row_to_dict(row) if row is not None else None

    def count(self) -> int:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(f"读取历史失败：{exc}") from exc
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        task = dict(row)
        for column in _JSON_COLUMNS:
            task[column] = _loads(task.get(column))
        return task


# ---------------------------------------------------------------- 展示


def _amount_text(cost: dict | None) -> str:
    if not isinstance(cost, dict):
        return "—"
    display = cost.get("display") or {}
    amount = display.get("amount")
    if amount is None:
        return "—"
    symbol = "¥" if str(display.get("currency") or "CNY").upper() == "CNY" else "$"
    try:
        return f"{symbol}{float(amount):.4g}"
    except (TypeError, ValueError):
        return "—"


def _time_text(value: object) -> str:
    text = str(value or "")
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return text[:19] or "—"


def history_rows(tasks: Iterable[dict]) -> list[list[str]]:
    """任务列表 → Gradio Dataframe 的二维字符串数组。"""

    rows: list[list[str]] = []
    for task in tasks:
        speakers = task.get("speakers")
        wall = task.get("wall_s")
        rows.append(
            [
                str(task.get("id") or ""),
                _time_text(task.get("created_at")),
                Path(str(task.get("source") or "")).name or str(task.get("source") or ""),
                str(task.get("out_dir") or ""),
                STATUS_LABELS.get(str(task.get("status") or ""), str(task.get("status") or "")),
                "" if speakers is None else str(speakers),
                _amount_text(task.get("cost") if isinstance(task.get("cost"), dict) else None),
                "" if wall is None else f"{float(wall):.1f}s",
            ]
        )
    return rows


def task_choice_label(task: dict) -> str:
    """下拉选项：``12 · a.mp4 · 11-03 13:50:02``。"""

    return (
        f"{task.get('id')} · {Path(str(task.get('source') or '')).name}"
        f" · {_time_text(task.get('created_at'))}"
    )


def task_choice_id(choice: str | None) -> int | None:
    """从下拉选项反解任务 id；非法输入返回 None。"""

    if not choice:
        return None
    head = str(choice).split("·", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None
