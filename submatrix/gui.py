"""本地 Web GUI（M13/M14/M16）：Gradio 薄壳，只做「表单 → CLI 参数 → 子进程 → 日志/产物/成本」。

刻意保持薄：

- GUI 不复制任何编排逻辑——所有执行都交给 ``python -m submatrix pipeline``，
  参数一键透传（``额外参数`` 输入框），CLI 以后加 flag 不用改 GUI；
- **批量**：路径一行一个 / 多文件上传，逐个跑（各自独立输出目录与报告）；
- **取消**：停止按钮终止子进程树（Windows ``taskkill /T``），已落盘产物保留；
- **预览**：成片直接内嵌播放；批量结果表汇总每条的状态/用量/成本；成片；
- **音色**（M16）：按目标语言的下拉 + 真实试听；目录来自 ``submatrix.voices``（离线有兜底）；
- **多人声**（M16）：可填音色池/显式映射，跑完回填「说话人 → 音色」表，改表后可
  「重配音色」——复用上次 ASR/翻译缓存，只重跑配音与烧字；
- 生成器边跑边回显日志；跑完读 ``pipeline.json`` 展示产物、成本块与说话人表；
- API Key 只注入本次子进程环境变量，不落盘、不回显；
- 默认只监听 127.0.0.1，不做公网共享。

``gradio`` 是可选依赖：模块顶层不 import，``python -m submatrix gui`` 会检查并在
仓库自带 venv 存在时自动重入（见 M13/M14 文档）。
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path

from .cost import cost_rows, format_amount, format_cost_line
from .history import (
    HISTORY_HEADERS,
    HistoryError,
    HistoryStore,
    history_rows,
    task_choice_id,
    task_choice_label,
)
from .validate import LANG_PROFILES
from .voices import DEFAULT_LABEL, VoicesError, default_voice as default_voice_id
from .voices import preview_voice as synthesize_preview
from .voices import voice_choices, voice_id

__all__ = [
    "FEATURE_CHOICES",
    "HISTORY_HEADERS",
    "HISTORY_SOURCE_LIMIT",
    "REPO_ROOT",
    "SPEAKER_HEADERS",
    "RunController",
    "batch_out_dirs",
    "batch_result_rows",
    "build_app",
    "build_argv",
    "cost_table_rows",
    "default_out_dir",
    "format_speaker_map",
    "history_snapshot",
    "history_task_note",
    "output_files",
    "preview_voice_ui",
    "read_cost",
    "resolve_src",
    "resolve_sources",
    "serve",
    "speaker_mapping",
    "speaker_rows",
    "speaker_table_rows",
    "stop_run",
    "stream_rerun",
    "stream_revoice",
    "stream_run",
    "task_form_values",
    "video_path",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
LANG_CHOICES = sorted(LANG_PROFILES)
DEFAULT_FEATURES = ("dub", "burn", "mark")

# 中文标签 → 内部特性 id（CheckboxGroup 的值是标签，映射后再拼 argv）
FEATURE_CHOICES: dict[str, str] = {
    "生成配音成片": "dub",
    "保留原片 BGM": "bgm",
    "多人声分离": "diarize",
    "烧录硬字幕": "burn",
    "AIGC 标识": "mark",
    "LLM 翻译（DeepSeek）": "llm",
}
_LABEL_TO_ID = FEATURE_CHOICES
_ID_TO_LABEL = {value: key for key, value in FEATURE_CHOICES.items()}

_LOG_TAIL = 800
_FILES_LIMIT = 60
HISTORY_SOURCE_LIMIT = 100

# Gradio 6 的 Dataframe 只接受二维数组/DataFrame；list[dict] 会渲染成 [object Object]
BATCH_HEADERS = ["文件", "状态", "LLM tokens", "TTS 字符", "成本", "成片"]
COST_HEADERS = ["文件", "类别", "项目", "数量", "单位", "单价", "币种", "金额"]
SPEAKER_HEADERS = ["文件", "说话人", "音色", "字幕条数"]
_EMPTY_SPEAKER_ROWS: list[list[str]] = [["—", "—", "—", "—"]]
# (状态, 日志, 产物, 批量结果, 成本明细, 成片预览, 说话人表, 多人声音色输入框)
_View = tuple[
    str,
    str,
    list[str],
    list[list[str]],
    list[list[str]],
    str | None,
    list[list[str]],
    str,
]


def _view(
    status: str,
    log: Sequence[str],
    files_seen: Sequence[str],
    results: Sequence[tuple[str, str, dict | None, str | None]],
    preview: str | None,
    speaker_items: Sequence[tuple[str, Sequence[Sequence[str]]]],
    speaker_value: str,
) -> _View:
    return (
        status,
        "\n".join(log[-_LOG_TAIL:]),
        list(files_seen[:_FILES_LIMIT]),
        batch_table(results),
        cost_table(results),
        preview,
        speaker_table_rows(speaker_items),
        speaker_value,
    )



class RunController:
    """一次 GUI 运行的控制器：保存子进程句柄 + 取消标记 + 上次运行参数（本地单用户，全局一份）。"""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.cancelled = False
        # 「重配音色」复用：上次成功解析出的 (源文件, 输出目录, pipeline 参数) 与环境变量。
        # reset() 不清它——start 之后才覆盖，避免用户改了音色却把 ASR/翻译重跑一遍。
        self.last_plans: list[tuple[str, Path, dict]] = []
        self.last_env: dict[str, str] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def reset(self) -> None:
        self.process = None
        self.cancelled = False

    def cancel(self) -> str:
        self.cancelled = True
        process = self.process
        if process is None or process.poll() is not None:
            return "当前没有正在运行的任务"
        _kill_tree(process)
        return "已请求停止，清理中…"


_ACTIVE = RunController()


def _kill_tree(process: subprocess.Popen) -> None:
    """Windows 上 pipeline 会再起 ASR/TTS 子进程，必须整棵树杀。"""

    if os.name == "nt":
        try:
            subprocess.run(  # noqa: S603 - 固定 taskkill，参数是 pid
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            process.terminate()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value.strip()))


def resolve_sources(src_path: str | None, uploaded: str | list[str] | None) -> list[str]:
    """路径框（一行一个）优先；否则用上传文件列表。去重、展开环境变量。"""

    sources: list[str] = []
    if src_path and src_path.strip():
        sources = [line.strip() for line in src_path.splitlines() if line.strip()]
    if not sources and uploaded:
        if isinstance(uploaded, (str, Path)):
            sources = [str(uploaded)]
        else:
            sources = [str(item) for item in uploaded if item]
    expanded: list[str] = []
    for source in sources:
        path = _expand(source)
        if path and path not in expanded:
            expanded.append(path)
    if not expanded:
        raise ValueError("请填视频路径（一行一个），或上传视频文件")
    return expanded


def resolve_src(src_path: str | None, uploaded: str | list[str] | None) -> str:
    """单条兼容入口（测试/旧调用）。"""

    return resolve_sources(src_path, uploaded)[0]


def default_out_dir(src: str | Path, *, now: datetime | None = None) -> Path:
    stem = Path(src).stem.strip() or "video"
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return REPO_ROOT / "work" / "gui" / f"{stem}-{timestamp}"


def batch_out_dirs(
    sources: Sequence[str], out_dir: str | None, *, now: datetime | None = None
) -> list[Path]:
    """一个输入用给定目录；多个输入在给定目录下按 ``01-文件名`` 分子目录。"""

    base = Path(_expand(out_dir)) if out_dir and out_dir.strip() else None
    if base is None:
        return [default_out_dir(source, now=now) for source in sources]
    if len(sources) == 1:
        return [base]
    return [base / f"{index:02d}-{Path(source).stem or 'video'}" for index, source in enumerate(sources, 1)]


def build_argv(
    src: str,
    out: str,
    *,
    src_lang: str = "auto",
    tgt_lang: str = "zh",
    features: Iterable[str] = DEFAULT_FEATURES,
    voice: str | None = None,
    speakers: int | None = None,
    extra_args: str = "",
    force: bool = False,
    regenerate: bool = False,
    glossary: str | None = None,
    asr_model: str | None = None,
    tts_python: str | None = None,
    speaker_voices: str | None = None,
) -> list[str]:
    """把表单值拼成 ``pipeline`` 参数；features 用内部 id，方便测试。"""

    enabled = {str(feature).lower() for feature in features}
    argv: list[str] = [
        "-m",
        "submatrix",
        "pipeline",
        "--src",
        src,
        "--out",
        out,
        "--src-lang",
        src_lang,
        "--tgt-lang",
        tgt_lang,
    ]
    if "dub" not in enabled:
        argv += ["--stop-after", "build"]
    if "bgm" in enabled:
        argv.append("--keep-bgm")
    if "diarize" in enabled:
        argv.append("--diarize")
    if "burn" not in enabled:
        argv.append("--no-burn-subtitles")
    if "mark" not in enabled:
        argv.append("--no-mark")
    if "llm" in enabled:
        argv.append("--llm")
    chosen_voice = voice_id(voice)
    if chosen_voice:
        argv += ["--voice", chosen_voice]
    if speakers:
        argv += ["--speakers", str(int(speakers))]
    if speaker_voices and str(speaker_voices).strip():
        argv += ["--speaker-voices", str(speaker_voices).strip()]
    if glossary and str(glossary).strip():
        argv += ["--glossary", str(glossary).strip()]
    if asr_model and asr_model.strip() and asr_model.strip().lower() != "auto":
        argv += ["--asr-model", asr_model.strip()]
    if tts_python and tts_python.strip():
        argv += ["--tts-python", tts_python.strip()]
    if force:
        argv.append("--force")
    if regenerate:
        argv.append("--regenerate-tts")
    if extra_args and extra_args.strip():
        try:
            argv += shlex.split(extra_args.strip())
        except ValueError as exc:
            raise ValueError(f"额外参数解析失败：{exc}") from exc
    return argv


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_cost(out_dir: str | Path) -> dict | None:
    cost = _read_json(Path(out_dir) / "pipeline.json").get("cost")
    return cost if isinstance(cost, dict) else None


def _report_outputs(out_dir: str | Path) -> dict:
    outputs = _read_json(Path(out_dir) / "pipeline.json").get("outputs")
    return outputs if isinstance(outputs, dict) else {}


def output_files(out_dir: str | Path, *, limit: int = _FILES_LIMIT) -> list[str]:
    paths: list[str] = []
    for key, value in _report_outputs(out_dir).items():
        if key == "pipeline_report" or not isinstance(value, str):
            continue
        if Path(value).is_file():
            paths.append(value)
    return list(dict.fromkeys(paths))[:limit]


def video_path(out_dir: str | Path) -> str | None:
    outputs = _report_outputs(out_dir)
    for key in ("final_video", "burned_video", "dub_video"):
        value = outputs.get(key)
        if isinstance(value, str) and Path(value).is_file():
            return value
    return None


def table_values(rows: Sequence[dict], headers: Sequence[str]) -> list[list[str]]:
    """把行字典转成 Gradio Dataframe 能渲染的二维字符串数组。"""

    return [
        ["" if row.get(header) is None else str(row.get(header)) for header in headers]
        for row in rows
    ]


def batch_table(results: Sequence[tuple[str, str, dict | None, str | None]]) -> list[list[str]]:
    return table_values(batch_result_rows(results), BATCH_HEADERS)


def cost_table(results: Sequence[tuple[str, str, dict | None, str | None]]) -> list[list[str]]:
    return table_values(cost_table_rows(results), COST_HEADERS)


def batch_result_rows(
    results: Sequence[tuple[str, str, dict | None, str | None]],
) -> list[dict]:
    """批量结果表：文件 / 状态 / 用量 / 成本 / 成片。"""

    rows: list[dict] = []
    for label, status, cost, video in results:
        quantities = (cost or {}).get("quantities") or {}
        llm = quantities.get("llm") or {}
        tts = quantities.get("tts") or {}
        rows.append(
            {
                "文件": label,
                "状态": status,
                "LLM tokens": int(llm.get("total_tokens") or 0),
                "TTS 字符": int(tts.get("chars") or 0),
                "成本": format_amount(cost) if cost else "—",
                "成片": video or "—",
            }
        )
    return rows


def cost_table_rows(
    results: Sequence[tuple[str, str, dict | None, str | None]],
) -> list[dict]:
    """成本明细表（每条产物一组行，前面加文件名）。"""

    rows: list[dict] = []
    for label, _status, cost, _video in results:
        if not cost:
            continue
        for row in cost_rows(cost):
            rows.append({"文件": label, **row})
    if not rows:
        rows.append(
            {
                "文件": "—",
                "类别": "—",
                "项目": "无外部计费调用（本地 ASR/分离/BGM + 免费渠道）",
                "数量": None,
                "单位": None,
                "单价": None,
                "币种": "",
                "金额": "0",
            }
        )
    return rows


def _stage_summary(out_dir: str | Path, stage: str) -> dict:
    report = _read_json(Path(out_dir) / "pipeline.json")
    for item in report.get("stages") or []:
        if isinstance(item, dict) and item.get("stage") == stage:
            summary = item.get("summary")
            if isinstance(summary, dict):
                return summary
    return {}


def speaker_rows(out_dir: str | Path) -> list[list[str]]:
    """从一次运行的 ``pipeline.json`` 读「说话人 → 音色 → 字幕条数」。

    多人声没开 / 只识别到 1 人时返回空列表（表格显示占位）。
    """

    summary = _stage_summary(out_dir, "diarize")
    voices = summary.get("speaker_voices")
    if not isinstance(voices, dict) or not voices:
        return []
    stats = summary.get("speaker_stats")
    counts: dict[str, object] = {}
    if isinstance(stats, list):
        for item in stats:
            if isinstance(item, dict) and item.get("speaker"):
                counts[str(item["speaker"])] = item.get("cues")
    rows: list[list[str]] = []
    for speaker, voice in sorted(voices.items()):
        cues = counts.get(str(speaker))
        # cues 来自 JSON：可能是 None 或数字；其它形状按空串处理（表格里显示为空）
        cue_text = str(int(cues)) if isinstance(cues, (int, float, str)) else ""
        rows.append([str(speaker), str(voice), cue_text])
    return rows


def speaker_table_rows(items: Sequence[tuple[str, Sequence[Sequence[str]]]]) -> list[list[str]]:
    """把每个产物的说话人行汇总成 GUI 表格（二维字符串数组）。"""

    rows: list[list[str]] = []
    for label, speaker_list in items:
        if speaker_list:
            rows.extend([label, *[str(cell) for cell in row]] for row in speaker_list)
        else:
            rows.append([label, "—", "—", "—"])
    return rows or [["—", "—", "—", "—"]]


def _stage_statuses(out_dir: str | Path) -> dict[str, str] | None:
    """pipeline.json 的逐阶段状态（写进任务历史）。"""

    report = _read_json(Path(out_dir) / "pipeline.json")
    stages = report.get("stages")
    if not isinstance(stages, list):
        return None
    statuses: dict[str, str] = {}
    for item in stages:
        if isinstance(item, dict) and item.get("stage"):
            statuses[str(item["stage"])] = str(item.get("status") or "")
    return statuses or None


def _diarize_speakers(out_dir: str | Path) -> int | None:
    summary = _stage_summary(out_dir, "diarize")
    value = summary.get("speakers")
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _report_wall(out_dir: str | Path) -> float | None:
    value = _read_json(Path(out_dir) / "pipeline.json").get("wall_s")
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _report_error(out_dir: str | Path) -> str | None:
    """失败任务的错误摘要：优先取失败阶段，其次 pipeline 状态。"""

    report = _read_json(Path(out_dir) / "pipeline.json")
    stages = report.get("stages")
    if isinstance(stages, list):
        for item in stages:
            if not isinstance(item, dict):
                continue
            if item.get("status") in {"failed", "partial"} and item.get("error"):
                return f"{item.get('stage')}: {item['error']}"
    status = report.get("status")
    if status is None or status == "ok":
        return None
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        return f"pipeline.status={status}；{warnings[0]}"
    return f"pipeline.status={status}"


def task_form_values(task: dict) -> dict[str, object]:
    """把一条历史任务还原成 GUI 表单值（「载入参数」用）。"""

    settings = task.get("settings") if isinstance(task.get("settings"), dict) else {}
    features = settings.get("features") or []
    if isinstance(features, str):
        features = [features]
    labels = [_ID_TO_LABEL.get(str(feature), str(feature)) for feature in features]
    voice = str(settings.get("voice") or "").strip() or DEFAULT_LABEL
    return {
        "src_path": str(task.get("source") or ""),
        "out_dir": str(task.get("out_dir") or ""),
        "src_lang": str(settings.get("src_lang") or "auto"),
        "tgt_lang": str(settings.get("tgt_lang") or "zh"),
        "features": [label for label in labels if label in FEATURE_CHOICES],
        "voice": voice,
        "speakers": int(settings.get("speakers") or 0),
        "extra_args": str(settings.get("extra_args") or ""),
        "asr_model": str(settings.get("asr_model") or "auto"),
        "tts_python": str(settings.get("tts_python") or ""),
        "speaker_voices": str(settings.get("speaker_voices") or ""),
        "force": bool(settings.get("force")),
        "regenerate": bool(settings.get("regenerate")),
        "glossary": settings.get("glossary") or None,
    }


# 「载入参数」按钮的 outputs 顺序（与 build_app 里保持一致）
FORM_FIELDS = (
    "src_path",
    "out_dir",
    "src_lang",
    "tgt_lang",
    "features",
    "voice",
    "speakers",
    "extra_args",
    "asr_model",
    "tts_python",
    "speaker_voices",
    "force",
    "regenerate",
    "glossary",
)


def history_snapshot(
    store: HistoryStore | None, limit: int = HISTORY_SOURCE_LIMIT
) -> tuple[list[list[str]], list[str], str]:
    """任务历史页的数据：表格行 / 下拉选项 / 说明文字。"""

    if store is None:
        return [], [], "历史库不可用（GUI 仍可运行，只是不记历史）"
    try:
        tasks = store.list_tasks(limit=limit)
    except HistoryError as exc:
        return [], [], f"读取历史失败：{exc}"
    choices = [task_choice_label(task) for task in tasks]
    return history_rows(tasks), choices, f"共 {len(tasks)} 条记录 · 数据库 {store.path}"


def history_task_note(store: HistoryStore | None) -> str:
    _rows, _choices, note = history_snapshot(store)
    return note


def _table_rows(rows: object) -> list[list[str]]:
    """把 GUI 表格输入统一成二维字符串数组。

    Gradio 的 Dataframe 可能是 ``list[list]``（``type="array"``）、pandas DataFrame
    （默认 ``type="pandas"``）或 ``{"headers":..., "data":...}``。**不能对 DataFrame
    用 ``rows or []``**——pandas 会抛 ``ValueError: truth value is ambiguous``
    （M16 真实 GUI 测试踩到）。
    """

    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    values = getattr(rows, "values", None)  # DataFrame / numpy
    if values is not None and hasattr(values, "tolist"):
        try:
            rows = values.tolist()
        except (AttributeError, TypeError, ValueError):
            return []
    if not isinstance(rows, Iterable):
        return []
    candidates = list(rows)
    table: list[list[str]] = []
    for row in candidates:
        if not isinstance(row, (list, tuple)):
            continue
        table.append(["" if cell is None else str(cell) for cell in row])
    return table


def speaker_mapping(rows: object) -> dict[str, str]:
    """从 GUI 表格行里取 ``SPEAKER_xx → voice id``（忽略空行和手输说明）。"""

    mapping: dict[str, str] = {}
    for row in _table_rows(rows):
        cells = [cell.strip() for cell in row]
        speaker = next((cell for cell in cells if cell.upper().startswith("SPEAKER_")), "")
        voice = next((voice_id(cell) for cell in cells if "neural" in cell.lower()), "")
        if speaker and voice:
            mapping[speaker] = voice
    return mapping


def format_speaker_map(mapping: dict[str, str]) -> str:
    return ",".join(f"{speaker}={voice}" for speaker, voice in sorted(mapping.items()))


def preview_voice_ui(voice: str, tgt_lang: str = "zh") -> tuple[str | None, str]:
    """「试听」按钮：合成一句样音，返回 (音频路径, 提示)。"""

    language = (tgt_lang or "zh").strip().lower()
    try:
        path = synthesize_preview(voice, language)
    except VoicesError as exc:
        return None, f"试听失败：{exc}"
    shown = voice_id(voice) or f"默认（{default_voice_id(language)}）"
    return str(path), f"试听：{shown}"


def _run_plans(
    plans: Sequence[tuple[str, Path, list[str]]],
    env: dict[str, str],
    active: RunController,
) -> Iterator[tuple[str, tuple]]:
    """逐个执行 pipeline 子进程，产出 start/line/done 事件（两个生成器复用）。"""

    for index, (source, out, argv) in enumerate(plans, 1):
        if active.cancelled:
            return
        label = Path(source).name
        command = [sys.executable, "-u", *argv]
        yield ("start", (index, len(plans), label, out, command))

        process = subprocess.Popen(  # noqa: S603 - 参数列表由本模块拼装，非用户 shell
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
            start_new_session=(os.name != "nt"),
        )
        active.process = process
        assert process.stdout is not None
        try:
            for line in process.stdout:
                yield ("line", (index, len(plans), label, line.rstrip()))
                if active.cancelled:
                    _kill_tree(process)
                    break
        finally:
            process.stdout.close()
        code = process.wait()
        active.process = None
        yield ("done", (index, len(plans), label, out, code))


def _run_and_view(
    plans: Sequence[tuple[str, Path, list[str]]],
    env: dict[str, str],
    active: RunController,
    speaker_value: str = "",
    *,
    store: HistoryStore | None = None,
    settings_list: Sequence[dict] | None = None,
    replay_of: Sequence[int | None] | None = None,
) -> Iterator[_View]:
    """执行并逐步产出界面状态；结束时给出汇总（完成 x/y 条 + 成本）。

    ``store`` 给了就把每条任务写进历史库（开始一行 running、结束更新状态/成本/阶段/人数）。
    历史库出错只写日志，绝不影响主流程。
    """

    total = len(plans)
    results: list[tuple[str, str, dict | None, str | None]] = []
    files_seen: list[str] = []
    log: list[str] = []
    preview: str | None = None
    speaker_items: list[tuple[str, list[list[str]]]] = []
    task_ids: dict[int, int] = {}

    def _record_start(index: int, out: Path) -> None:
        if store is None:
            return
        source, _out, argv = plans[index - 1]
        settings = settings_list[index - 1] if settings_list else None
        replay = replay_of[index - 1] if replay_of else None
        try:
            task_ids[index] = store.add_task(
                source=str(source),
                out_dir=str(out),
                settings=dict(settings) if settings else None,
                argv=list(argv),
                replay_of=replay,
            )
        except HistoryError as exc:
            log.append(f"[history] 写入任务失败：{exc}")

    def _record_finish(index: int, out: Path, code: int, cost: dict | None) -> None:
        task_id = task_ids.get(index)
        if store is None or task_id is None:
            return
        try:
            store.finish_task(
                task_id,
                exit_code=code,
                cancelled=active.cancelled,
                cost=cost,
                stages=_stage_statuses(out),
                speakers=_diarize_speakers(out),
                wall_s=_report_wall(out),
                error=None if code == 0 else _report_error(out),
            )
        except HistoryError as exc:
            log.append(f"[history] 更新任务失败：{exc}")

    for kind, payload in _run_plans(plans, env, active):
        if kind == "start":
            index, total, label, out, command = payload
            log.append(f"=== [{index}/{total}] {label} → {out} ===")
            log.append("命令：" + " ".join(command))
            _record_start(index, out)
            yield _view(
                f"运行中… [{index}/{total}] {label}",
                log, files_seen, results, preview, speaker_items, speaker_value,
            )
            continue
        if kind == "line":
            index, total, label, text = payload
            log.append(text)
            yield _view(
                f"运行中… [{index}/{total}] {label}",
                log, files_seen, results, preview, speaker_items, speaker_value,
            )
            continue

        index, total, label, out, code = payload
        cost = read_cost(out)
        _record_finish(index, out, code, cost)
        for path in output_files(out):
            if path not in files_seen:
                files_seen.append(path)
        candidate = video_path(out)
        if candidate:
            preview = candidate
        current = speaker_rows(out)
        speaker_items.append((label, current))
        mapping = speaker_mapping(current)
        if mapping:
            speaker_value = format_speaker_map(mapping)

        if active.cancelled:
            status_text = "已取消"
            log.append(f"--- {label}：已取消（部分产物可能已落盘）")
        elif code == 0:
            status_text = "完成"
            log.append(f"--- {label}：完成")
        else:
            status_text = f"失败（退出码 {code}）"
            log.append(f"--- {label}：失败（退出码 {code}，看日志定位阶段）")
        results.append((label, status_text, cost, candidate))
        yield _view(
            f"已处理 [{len(results)}/{total}] {label}：{status_text}",
            log, files_seen, results, preview, speaker_items, speaker_value,
        )

    ok = sum(1 for _label, status, _cost, _video in results if status == "完成")
    if active.cancelled:
        status = f"已取消：完成 {ok}/{total}（已产出文件可下载）"
    elif ok == total:
        status = f"完成：{ok}/{total} 条"
    else:
        status = f"结束：成功 {ok}/{total} 条（失败项看日志）"
    amounts = [
        float((cost.get("display") or {}).get("amount") or 0)
        for _label, _status, cost, _video in results
        if cost
    ]
    if len(results) == 1 and results[0][2]:
        status += "；" + format_cost_line(results[0][2])
    elif amounts:
        status += f"；批量累计估算 ≈¥{sum(amounts):.4g}"
    yield _view(status, log, files_seen, results, preview, speaker_items, speaker_value)


def stream_run(
    src_path: str,
    uploaded: str | list[str] | None,
    out_dir: str,
    src_lang: str,
    tgt_lang: str,
    feature_labels: Iterable[str],
    voice: str,
    speakers: float | int | None,
    extra_args: str,
    deepseek_key: str,
    dashscope_key: str,
    force: bool = False,
    regenerate: bool = False,
    glossary: str | list[str] | None = None,
    # 注意：顺序必须与 build_app 里的 inputs 列表一致（speaker_voices 在 tts_python 之后）
    asr_model: str = "auto",
    tts_python: str = "",
    speaker_voices: str = "",
    controller: RunController | None = None,
    store: HistoryStore | None = None,
) -> Iterator[_View]:
    """Gradio 生成器：开始 → 实时日志 → 批量结果 / 成本明细 / 成片预览 / 说话人表。"""

    active = controller or _ACTIVE
    if active.running:
        yield (
            "已有任务在运行，请先点「停止」或等它结束", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, speaker_voices or "",
        )
        return
    active.reset()

    try:
        sources = resolve_sources(src_path, uploaded)
        missing = [source for source in sources if not Path(source).is_file()]
        if missing:
            raise ValueError(f"视频不存在：{missing[0]}")
        outs = batch_out_dirs(sources, out_dir)
        features = {
            _LABEL_TO_ID.get(label, label) for label in (feature_labels or DEFAULT_FEATURES)
        }
        glossary_path = None
        if glossary:
            glossary_path = glossary[0] if isinstance(glossary, (list, tuple)) else glossary
        settings: dict[str, object] = {
            "src_lang": src_lang,
            "tgt_lang": tgt_lang,
            "features": features,
            "voice": voice,
            "speakers": int(speakers) if speakers else 0,
            "extra_args": extra_args,
            "force": force,
            "regenerate": regenerate,
            "glossary": glossary_path,
            "asr_model": asr_model,
            "tts_python": tts_python,
            "speaker_voices": speaker_voices or "",
        }
        plans = [
            (source, out, build_argv(source, str(out), **settings))
            for source, out in zip(sources, outs)
        ]
    except (ValueError, OSError) as exc:
        yield (
            f"参数错误：{exc}", "", [], [], [], None, _EMPTY_SPEAKER_ROWS, speaker_voices or "",
        )
        return

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if deepseek_key and deepseek_key.strip():
        env["DEEPSEEK_API_KEY"] = deepseek_key.strip()
    if dashscope_key and dashscope_key.strip():
        env["DASHSCOPE_API_KEY"] = dashscope_key.strip()

    # 记下来给「重配音色」复用：同一目录 + 同一批参数，只换音色重跑，ASR/翻译走缓存。
    active.last_plans = [(source, out, dict(settings)) for source, out, _argv in plans]
    active.last_env = env

    yield from _run_and_view(
        plans,
        env,
        active,
        speaker_voices or "",
        store=store,
        settings_list=[dict(settings) for _ in plans],
    )


def stream_revoice(
    voice: str,
    speaker_table: object = None,
    *,
    controller: RunController | None = None,
    store: HistoryStore | None = None,
) -> Iterator[_View]:
    """「重配音色」：复用上次的 ASR/翻译缓存，只按新音色重跑 dub/burn。

    ``speaker_table`` 是界面上可编辑的「说话人 → 音色」表（二维数组）。表格里没有
    可用映射时只切换全片默认音色；映射存在时按说话人分派。
    """

    active = controller or _ACTIVE
    if active.running:
        yield (
            "已有任务在运行，请先点「停止」或等它结束", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, "",
        )
        return
    plans = list(active.last_plans)
    if not plans:
        yield (
            "还没有可重配的运行：先点「开始」跑一次，再改音色重配", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, "",
        )
        return

    mapping = speaker_mapping(speaker_table)
    active.reset()
    settings = dict(plans[0][2])
    settings["voice"] = voice or ""
    if mapping:
        settings["speaker_voices"] = format_speaker_map(mapping)
    settings["force"] = False  # 重配不强制重算，尽量吃缓存
    settings["regenerate"] = False
    settings["features"] = set(settings.get("features") or DEFAULT_FEATURES) | {"dub"}
    new_plans: list[tuple[str, Path, list[str]]] = [
        (source, out, build_argv(source, str(out), **settings))
        for source, out, _settings in plans
    ]
    env = dict(active.last_env or os.environ.copy())
    env["PYTHONIOENCODING"] = "utf-8"
    active.last_plans = [(source, out, dict(settings)) for source, out, _ in plans]
    active.last_env = env

    yield from _run_and_view(
        new_plans,
        env,
        active,
        format_speaker_map(mapping) if mapping else (voice or ""),
        store=store,
        settings_list=[dict(settings) for _ in new_plans],
    )


def stream_rerun(
    choice: str | None,
    *,
    store: HistoryStore | None,
    controller: RunController | None = None,
) -> Iterator[_View]:
    """任务历史「重跑」：按历史里记录的实际 argv 原样再跑一次（新任务写进历史）。

    注意：历史**不存 API Key**——重跑 LLM 任务前要先在表单里重新填 Key（或用环境变量）。
    """

    active = controller or _ACTIVE
    if active.running:
        yield (
            "已有任务在运行，请先点「停止」或等它结束", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, "",
        )
        return
    task_id = task_choice_id(choice)
    task = store.get_task(task_id) if (store is not None and task_id) else None
    if task is None:
        yield (
            "没有选中可重跑的任务：先在「任务历史」刷新并选一条", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, "",
        )
        return
    source = str(task.get("source") or "")
    out_dir = str(task.get("out_dir") or "")
    argv = task.get("argv")
    if not isinstance(argv, list) or not argv:
        yield (
            f"任务 #{task_id} 没有记录命令行（旧记录），无法重跑；可用「载入参数」后点开始",
            "", [], [], [], None, _EMPTY_SPEAKER_ROWS, "",
        )
        return
    if not Path(source).is_file():
        yield (
            f"源文件已不存在，无法重跑：{source}", "", [], [], [], None,
            _EMPTY_SPEAKER_ROWS, "",
        )
        return

    active.reset()
    plans = [(source, Path(out_dir), [str(item) for item in argv])]
    settings = task.get("settings") if isinstance(task.get("settings"), dict) else {}
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 让「重配音色」还能接着用：同一源/输出 + 同一批参数，只换音色
    active.last_plans = [(source, Path(out_dir), dict(settings))]
    active.last_env = env

    yield from _run_and_view(
        plans,
        env,
        active,
        "",
        store=store,
        settings_list=[settings],
        replay_of=[int(task_id)],
    )


def stop_run() -> str:
    """停止按钮：终止当前子进程树。"""

    return _ACTIVE.cancel()


def _prepare_gradio_env() -> None:
    """把 Gradio 的临时目录/遥测固定下来，避免在仓库根目录生成 ``gradio/``。

    只在真正启动 GUI 时调用；用户显式设置 GRADIO_TEMP_DIR 时尊重用户值。
    """

    os.environ.setdefault("GRADIO_TEMP_DIR", str(REPO_ROOT / "work" / ".gui-tmp"))
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
    allowed_paths: Sequence[str] | None = None,
) -> int:
    """启动本地服务（阻塞直到 Ctrl+C）。"""

    _prepare_gradio_env()
    app = build_app()
    app.queue()
    app.launch(
        server_name=host,
        server_port=port,
        inbrowser=open_browser,
        quiet=False,
        allowed_paths=[str(REPO_ROOT), str(Path.home()), *(allowed_paths or [])],
    )
    return 0


def build_app(*, repo_root: Path = REPO_ROOT, store: HistoryStore | None = None):
    """构建 Gradio Blocks；仅在真正启动 GUI 时 import gradio。

    ``store`` 是任务历史库；不传时用默认库（``work/history.db``），初始化失败也不阻断 GUI
    （只是不记历史，页面会显示原因）。
    """

    _prepare_gradio_env()
    import gradio as gr

    if store is None:
        try:
            store = HistoryStore()
        except HistoryError:
            store = None

    with gr.Blocks(title="submatrix · 视频字幕与配音") as app:
        gr.Markdown(
            "# submatrix：视频字幕提取 / 翻译 / 配音 / 成片\n"
            "本地 CLI 的薄壳：勾选项会拼成 `pipeline` 参数，实时日志可查；"
            "支持**批量**（路径一行一个 / 多文件上传）与**停止**；"
            "跑完自动展示产物、成片预览和 **成本汇总**（本地 ASR/分离/BGM 不产生费用）。"
        )
        with gr.Row():
            with gr.Column(scale=3):
                src_path = gr.Textbox(
                    label="视频路径（一行一个，支持批量；大文件建议用路径）",
                    lines=3,
                    placeholder="D:\\videos\\a.mp4\nD:\\videos\\b.mp4",
                )
                uploaded = gr.File(
                    label="或上传视频（可多选）",
                    file_count="multiple",
                    type="filepath",
                    file_types=["video"],
                )
                out_dir = gr.Textbox(
                    label="输出目录（留空自动；批量时在其下按 01-文件名 分目录）",
                    placeholder="留空 = work/gui/<文件名>-<时间戳>",
                )
            with gr.Column(scale=2):
                src_lang = gr.Dropdown(
                    label="源语言（内封字幕素材请选 zh/en；auto 无法判断字幕语言）",
                    choices=["auto", *LANG_CHOICES],
                    value="auto",
                )
                tgt_lang = gr.Dropdown(label="目标语言", choices=LANG_CHOICES, value="zh")
                features = gr.CheckboxGroup(
                    label="功能（对应 CLI 开关）",
                    choices=list(FEATURE_CHOICES),
                    value=[_ID_TO_LABEL[feature] for feature in DEFAULT_FEATURES],
                )
                voice = gr.Dropdown(
                    label="音色（按目标语言筛选；可手输 voice id）",
                    choices=voice_choices("zh"),
                    value=DEFAULT_LABEL,
                    allow_custom_value=True,
                    interactive=True,
                )
                with gr.Row():
                    preview_button = gr.Button("试听", size="sm")
                preview_audio = gr.Audio(label="试听", type="filepath", interactive=False)
                preview_note = gr.Markdown("")
        with gr.Accordion("高级：透传 CLI 参数 / 缓存 / API Key", open=False):
            with gr.Row():
                speakers = gr.Number(
                    label="说话人数（0 = 自动判断；仅多人声时用）", value=0, precision=0
                )
                extra_args = gr.Textbox(
                    label="额外参数（原样追加给 pipeline）",
                    placeholder="--burn-position top --hotwords 刘翔,跨栏",
                )
            speaker_voices = gr.Textbox(
                label="多人声音色（可选）：SPEAKER_00=音色,SPEAKER_01=音色 或 音色池（逗号分隔，按出场顺序）",
                lines=2,
                placeholder="留空 = 自动用默认音色池；填了会自动开启多人声分离",
            )
            with gr.Row():
                glossary = gr.File(
                    label="术语表（可选；.json/.tsv/.txt）",
                    file_count="single",
                    type="filepath",
                )
                force = gr.Checkbox(label="强制重跑（--force，忽略缓存）", value=False)
                regenerate = gr.Checkbox(
                    label="重新合成配音（--regenerate-tts，云端会重新计费）", value=False
                )
            with gr.Row():
                asr_model = gr.Dropdown(
                    label="ASR 模型（auto = 按源语言；中文建议 medium）",
                    choices=["auto", "small", "medium"],
                    value="auto",
                )
                tts_python = gr.Textbox(
                    label="TTS Python（可选；留空自动探测 edge-tts 环境）",
                    placeholder="留空 = 自动找 spike/.venv311 等",
                )
            with gr.Row():
                deepseek_key = gr.Textbox(
                    label="DEEPSEEK_API_KEY（仅注入本次运行，不落盘）", type="password"
                )
                dashscope_key = gr.Textbox(
                    label="DASHSCOPE_API_KEY（仅注入本次运行，不落盘）", type="password"
                )
        with gr.Row():
            run_button = gr.Button("开始", variant="primary")
            stop_button = gr.Button("停止", variant="stop")
        status = gr.Markdown("待运行")
        log = gr.Textbox(label="日志", lines=16, interactive=False)
        with gr.Row():
            preview = gr.Video(label="成片预览（最近一条）", interactive=False)
            files = gr.File(label="产物（跑完可下载）", file_count="multiple", interactive=False)
        batch_table = gr.Dataframe(
            label="批量结果（每条视频一行）",
            headers=BATCH_HEADERS,
            datatype=["str"] * len(BATCH_HEADERS),
            interactive=False,
            wrap=True,
        )
        cost_table = gr.Dataframe(
            label="成本明细（产物累计口径，按价表估算）",
            headers=COST_HEADERS,
            datatype=["str"] * len(COST_HEADERS),
            interactive=False,
            wrap=True,
        )
        speaker_table = gr.Dataframe(
            label="说话人 → 音色（多人声跑完自动回填；改「音色」列后点「重配音色」）",
            headers=SPEAKER_HEADERS,
            datatype=["str"] * len(SPEAKER_HEADERS),
            type="array",
            interactive=True,
            wrap=True,
        )
        revoice_button = gr.Button("重配音色（复用 ASR/翻译缓存，只重跑配音与烧字）")
        with gr.Tab("任务历史（关页面也在，可载入/重跑）"):
            with gr.Row():
                history_refresh = gr.Button("刷新", size="sm")
                history_pick = gr.Dropdown(
                    label="选择任务（ID · 文件 · 时间；也可直接输 ID）",
                    choices=[],
                    interactive=True,
                    allow_custom_value=True,
                    scale=4,
                )
                history_load = gr.Button("载入参数到表单", size="sm")
                history_rerun = gr.Button("重跑选中", variant="primary", size="sm")
                history_delete = gr.Button("删除记录", variant="stop", size="sm")
            history_table = gr.Dataframe(
                label="历史任务（最新在上；数据在 work/history.db）",
                headers=HISTORY_HEADERS,
                datatype=["str"] * len(HISTORY_HEADERS),
                interactive=False,
                wrap=True,
            )
            history_note = gr.Markdown("")
        outputs = [
            status,
            log,
            files,
            batch_table,
            cost_table,
            preview,
            speaker_table,
            speaker_voices,
        ]
        # ------------------------------------------------------------ 任务历史处理器
        history_outputs = [history_table, history_pick, history_note]

        def _refresh_history():
            rows, choices, note = history_snapshot(store)
            return rows, gr.update(choices=choices, value=None), note

        def _load_history(choice: str | None):
            task_id = task_choice_id(choice)
            task = store.get_task(task_id) if (store is not None and task_id) else None
            if task is None:
                return (*[gr.update() for _ in FORM_FIELDS], "先在历史里选一条任务")
            values = task_form_values(task)
            updates = [gr.update(value=values[field]) for field in FORM_FIELDS]
            name = Path(str(task.get("source") or "")).name
            return (*updates, f"已载入任务 #{task_id}（{name}）；点「开始」按原参数重跑")

        def _delete_history(choice: str | None):
            task_id = task_choice_id(choice)
            note_suffix = ""
            if store is not None and task_id is not None:
                try:
                    if store.delete_task(task_id):
                        note_suffix = f"（已删除记录 #{task_id}；不影响已产出的文件）"
                except HistoryError as exc:
                    note_suffix = f"（删除失败：{exc}）"
            rows, choices, note = history_snapshot(store)
            return rows, gr.update(choices=choices, value=None), note + note_suffix

        def _rerun_history(choice: str | None):
            last: tuple | None = None
            for view in stream_rerun(choice, store=store):
                last = view
                yield (*view, "重跑中…（完成后点「刷新」看新记录）")
            if last is not None:
                yield (*last, history_task_note(store))

        def _stream_run_form(*args):
            yield from stream_run(*args, store=store)

        run_button.click(
            _stream_run_form,
            inputs=[
                src_path,
                uploaded,
                out_dir,
                src_lang,
                tgt_lang,
                features,
                voice,
                speakers,
                extra_args,
                deepseek_key,
                dashscope_key,
                force,
                regenerate,
                glossary,
                asr_model,
                tts_python,
                speaker_voices,
            ],
            outputs=outputs,
            api_name="stream_run",
        ).then(_refresh_history, inputs=None, outputs=history_outputs)
        tgt_lang.change(
            lambda language: gr.update(
                choices=voice_choices(language or "zh"), value=DEFAULT_LABEL
            ),
            inputs=[tgt_lang],
            outputs=[voice],
        )
        preview_button.click(
            preview_voice_ui,
            inputs=[voice, tgt_lang],
            outputs=[preview_audio, preview_note],
        )

        def _revoice(voice_value: str, rows: object):
            yield from stream_revoice(voice_value, rows, store=store)

        revoice_button.click(
            _revoice,
            inputs=[voice, speaker_table],
            outputs=outputs,
            api_name="stream_revoice",
        ).then(_refresh_history, inputs=None, outputs=history_outputs)
        stop_button.click(stop_run, inputs=None, outputs=[status])

        history_refresh.click(
            _refresh_history, inputs=None, outputs=history_outputs, api_name="history_refresh"
        )
        history_load.click(
            _load_history,
            inputs=[history_pick],
            outputs=[
                src_path,
                out_dir,
                src_lang,
                tgt_lang,
                features,
                voice,
                speakers,
                extra_args,
                asr_model,
                tts_python,
                speaker_voices,
                force,
                regenerate,
                glossary,
                history_note,
            ],
        )
        history_rerun.click(
            _rerun_history,
            inputs=[history_pick],
            outputs=[*outputs, history_note],
            api_name="rerun_history",
        )
        history_delete.click(_delete_history, inputs=[history_pick], outputs=history_outputs)
        app.load(_refresh_history, inputs=None, outputs=history_outputs)
    return app
