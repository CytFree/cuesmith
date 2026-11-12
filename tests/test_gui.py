"""GUI 薄壳的纯函数测试：批量解析、参数映射、取消、产物/成本读取。

不 import gradio；真正的服务冒烟在 ``tools/gui_smoke.py``（用 spike/gui/.venv 跑）。
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from cuesmith import gui
from cuesmith.cli import build_parser

# ---------------------------------------------------------------- 参数映射


def test_build_argv_defaults_to_dub_burn_mark() -> None:
    argv = gui.build_argv("in.mp4", "out", src_lang="en", tgt_lang="zh")

    assert argv[:3] == ["-m", "cuesmith", "pipeline"]
    assert argv[argv.index("--src") + 1] == "in.mp4"
    assert argv[argv.index("--out") + 1] == "out"
    assert argv[argv.index("--src-lang") + 1] == "en"
    assert argv[argv.index("--tgt-lang") + 1] == "zh"
    assert "--stop-after" not in argv
    assert "--no-burn-subtitles" not in argv
    assert "--no-mark" not in argv


def test_build_argv_subtitle_only_stops_after_build() -> None:
    argv = gui.build_argv("in.mp4", "out", features=["mark"])

    assert argv[argv.index("--stop-after") + 1] == "build"
    assert "--no-burn-subtitles" in argv
    assert "--no-mark" not in argv


def test_build_argv_maps_each_feature_flag() -> None:
    argv = gui.build_argv(
        "in.mp4",
        "out",
        features=["dub", "burn", "mark", "bgm", "diarize", "llm"],
    )

    assert "--keep-bgm" in argv
    assert "--diarize" in argv
    assert "--llm" in argv
    assert "--stop-after" not in argv


def test_build_argv_voice_speakers_extra_args() -> None:
    argv = gui.build_argv(
        "in.mp4",
        "out",
        voice=" zh-CN-YunxiNeural ",
        speakers=2,
        extra_args='--glossary "术语 表.json" --burn-position top',
    )

    assert argv[argv.index("--voice") + 1] == "zh-CN-YunxiNeural"
    assert argv[argv.index("--speakers") + 1] == "2"
    assert "--glossary" in argv
    assert "术语 表.json" in argv
    assert argv[-2:] == ["--burn-position", "top"]


def test_build_argv_force_regenerate_glossary() -> None:
    argv = gui.build_argv(
        "in.mp4",
        "out",
        force=True,
        regenerate=True,
        glossary=r"D:\terms.json",
    )

    assert "--force" in argv
    assert "--regenerate-tts" in argv
    assert argv[argv.index("--glossary") + 1] == r"D:\terms.json"


def test_build_argv_asr_model_and_tts_python() -> None:
    argv = gui.build_argv(
        "in.mp4",
        "out",
        asr_model="medium",
        tts_python=r"C:\tts\python.exe",
    )

    assert argv[argv.index("--asr-model") + 1] == "medium"
    assert argv[argv.index("--tts-python") + 1] == r"C:\tts\python.exe"
    assert "--asr-model" not in gui.build_argv("in.mp4", "out", asr_model="auto")
    assert "--tts-python" not in gui.build_argv("in.mp4", "out", tts_python="")


def test_build_argv_rejects_bad_extra_args() -> None:
    with pytest.raises(ValueError, match="额外参数"):
        gui.build_argv("in.mp4", "out", extra_args='"unclosed')


# ---------------------------------------------------------------- 输入解析


def test_resolve_sources_multiline_and_dedupe(monkeypatch, tmp_path: Path) -> None:
    env_var = "CUESMITH_TEST_VIDEO"
    monkeypatch.setenv(env_var, str(tmp_path / "env.mp4"))

    sources = gui.resolve_sources(f"$CUESMITH_TEST_VIDEO\n{tmp_path / 'b.mp4'}\n\n", None)

    assert sources == [str(tmp_path / "env.mp4"), str(tmp_path / "b.mp4")]


def test_resolve_sources_falls_back_to_uploaded_list(tmp_path: Path) -> None:
    first = str(tmp_path / "a.mp4")
    second = str(tmp_path / "b.mp4")

    assert gui.resolve_sources("  ", [first, second]) == [first, second]
    with pytest.raises(ValueError, match="请填视频路径"):
        gui.resolve_sources("", None)


def test_resolve_src_single_compat(tmp_path: Path) -> None:
    assert gui.resolve_src("  ", str(tmp_path / "uploaded.mp4")) == str(tmp_path / "uploaded.mp4")


def test_default_out_dir_uses_stem_and_timestamp() -> None:
    now = datetime(2026, 10, 26, 11, 41, 20)
    out = gui.default_out_dir("D:/videos/央视科普-1.mp4", now=now)

    assert out == gui.REPO_ROOT / "work" / "gui" / "央视科普-1-20261026-114120"
    assert gui.default_out_dir("", now=now) == gui.REPO_ROOT / "work" / "gui" / "video-20261026-114120"


def test_batch_out_dirs_single_and_multiple(tmp_path: Path) -> None:
    now = datetime(2026, 10, 26, 12, 0, 0)
    single = gui.batch_out_dirs(["D:/v/a.mp4"], "", now=now)
    assert single == [gui.REPO_ROOT / "work" / "gui" / "a-20261026-120000"]

    base = tmp_path / "batch"
    dirs = gui.batch_out_dirs(["D:/v/a.mp4", "D:/v/b.mkv"], str(base))
    assert dirs == [base / "01-a", base / "02-b"]

    assert gui.batch_out_dirs(["D:/v/a.mp4"], str(base)) == [base]


# ---------------------------------------------------------------- 取消


class DummyProcess:
    pid = 4242

    def poll(self):  # noqa: ANN001
        return None


def test_run_controller_cancel_without_task() -> None:
    controller = gui.RunController()

    assert "没有正在运行" in controller.cancel()
    assert controller.cancelled is True
    controller.reset()
    assert controller.cancelled is False and controller.running is False


def test_run_controller_cancel_kills_process_tree(monkeypatch) -> None:
    killed: list[object] = []
    monkeypatch.setattr(gui, "_kill_tree", lambda process: killed.append(process))
    controller = gui.RunController()
    process = DummyProcess()
    controller.process = process  # type: ignore[assignment]

    message = controller.cancel()

    assert "停止" in message
    assert killed == [process]


# ---------------------------------------------------------------- 产物/成本


def _write_pipeline(out: Path, *, cost: dict | None = None, video: bool = True) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {"pipeline_report": str(out / "pipeline.json")}
    if video:
        clip = out / "final" / "marked.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"video")
        outputs["final_video"] = str(clip)
    srt = out / "matrix" / "zh.srt"
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("1\n", encoding="utf-8")
    outputs["publish_srt"] = str(srt)
    report: dict = {"outputs": outputs}
    if cost is not None:
        report["cost"] = cost
    (out / "pipeline.json").write_text(json.dumps(report), encoding="utf-8")
    return out


def test_read_cost_output_files_and_video(tmp_path: Path) -> None:
    cost = {"basis": "artifact", "display": {"currency": "CNY", "amount": 0.01}}
    out = _write_pipeline(tmp_path / "run", cost=cost)

    assert gui.read_cost(out) == cost
    assert gui.video_path(out) == str(out / "final" / "marked.mp4")
    assert str(out / "matrix" / "zh.srt") in gui.output_files(out)
    assert str(out / "pipeline.json") not in gui.output_files(out)
    assert gui.read_cost(tmp_path / "nope") is None
    assert gui.video_path(tmp_path / "nope") is None


def test_batch_and_cost_rows(tmp_path: Path) -> None:
    cost = {
        "basis": "artifact",
        "quantities": {"llm": {"total_tokens": 10}, "tts": {"chars": 5}},
        "items": [],
        "totals": {"CNY": 0.01},
        "display": {"currency": "CNY", "amount": 0.01, "converted": True},
    }
    results = [("a.mp4", "完成", cost, str(tmp_path / "a.mp4")), ("b.mp4", "失败（退出码 2）", None, None)]

    batch = gui.batch_result_rows(results)
    assert batch[0]["文件"] == "a.mp4"
    assert batch[0]["LLM tokens"] == 10
    assert batch[0]["TTS 字符"] == 5
    assert batch[0]["成本"] == "¥0.01"
    assert batch[1]["成本"] == "—"

    rows = gui.cost_table_rows(results)
    assert rows[0]["文件"] == "a.mp4"
    assert any(row["类别"] == "折算" for row in rows)
    assert gui.cost_table_rows([])[0]["项目"].startswith("无外部计费调用")


# ---------------------------------------------------------------- 批量执行


class FakePopen:
    """假子进程：从 argv 的 --out 找输出目录，落一份最小 pipeline.json。"""

    commands: list[list[str]] = []

    def __init__(self, command, **kwargs) -> None:  # noqa: ANN001
        FakePopen.commands.append(list(command))
        out = Path(command[command.index("--out") + 1])
        cost = {
            "basis": "artifact",
            "quantities": {"llm": {"total_tokens": 10}, "tts": {"chars": 5}},
            "items": [],
            "totals": {},
            "display": {"currency": "CNY", "amount": 0.02},
        }
        _write_pipeline(out, cost=cost)
        self.stdout = io.StringIO("line one\nline two\n")
        self.pid = 1234

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


def test_stream_run_batch_two_sources(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gui.subprocess, "Popen", FakePopen)
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    steps = list(
        gui.stream_run(
            f"{first}\n{second}",
            None,
            str(tmp_path / "out"),
            "auto",
            "zh",
            ["mark"],
            "",
            0,
            "",
            "",
            "",
        )
    )
    status, log, files, batch, cost_rows, preview, speaker_rows, speaker_value = steps[-1]

    assert "完成：2/2" in status
    assert [row[0] for row in batch] == ["a.mp4", "b.mp4"]
    assert all(row[1] == "完成" for row in batch)
    assert len(files) == 4  # 每条：成片 + 字幕
    assert preview and preview.endswith("marked.mp4")
    assert "line one" in log
    assert all(row[0] in {"a.mp4", "b.mp4", "—"} for row in cost_rows)
    assert speaker_rows == [
        ["a.mp4", "—", "—", "—"],
        ["b.mp4", "—", "—", "—"],
    ]
    assert speaker_value == ""
    assert all(isinstance(cell, str) for row in batch for cell in row)


def test_table_values_converts_dicts_to_2d_strings() -> None:
    rows = [{"文件": "a.mp4", "数量": None, "金额": 1.5}]

    assert gui.table_values(rows, ["文件", "数量", "金额"]) == [["a.mp4", "", "1.5"]]


def test_stream_run_argument_error_does_not_spawn() -> None:
    generator = gui.stream_run("", None, "", "auto", "zh", [], "", 0, "", "", "")
    status, log, files, batch, cost, preview, speakers, speaker_value = next(generator)

    assert status.startswith("参数错误")
    assert (log, files, batch, cost, preview) == ("", [], [], [], None)
    assert speakers == [["—", "—", "—", "—"]]
    assert speaker_value == ""


def test_stream_run_accepts_full_gradio_input_order() -> None:
    """回归：build_app 的 17 个 inputs 必须按位置对上，不能把 speaker_voices 当 controller。"""

    generator = gui.stream_run(
        "", None, "", "auto", "zh", [], "", 0, "", "", "", False, False, None,
        "auto", "", "SPEAKER_00=zh-CN-YunxiNeural",
    )
    status, _log, _files, _batch, _cost, _preview, _speakers, speaker_value = next(generator)

    assert status.startswith("参数错误")
    assert speaker_value == "SPEAKER_00=zh-CN-YunxiNeural"


def test_cli_gui_parser_accepts_host_port_and_allowed_paths() -> None:
    args = build_parser().parse_args(
        ["gui", "--host", "0.0.0.0", "--port", "7999", "--no-browser", "--allowed-path", "D:/out"]
    )

    assert args.command == "gui"
    assert args.host == "0.0.0.0"
    assert args.port == 7999
    assert args.no_browser is True
    assert args.allowed_path == ["D:/out"]


# ---------------------------------------------------------------- 音色 / 多人声


def test_build_argv_speaker_voices_and_voice_label() -> None:
    argv = gui.build_argv(
        "in.mp4",
        "out",
        voice="zh-CN-XiaoxiaoNeural · 晓晓·女声",
        speaker_voices=" SPEAKER_00=zh-CN-YunxiNeural ",
    )

    assert argv[argv.index("--voice") + 1] == "zh-CN-XiaoxiaoNeural"
    assert argv[argv.index("--speaker-voices") + 1] == "SPEAKER_00=zh-CN-YunxiNeural"
    # 「默认」标签不能原样塞给 --voice
    assert "--voice" not in gui.build_argv("in.mp4", "out", voice=gui.DEFAULT_LABEL)


def test_speaker_rows_reads_diarize_summary(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    report = {
        "outputs": {},
        "stages": [
            {
                "stage": "diarize",
                "status": "ok",
                "summary": {
                    "speakers": 2,
                    "speaker_voices": {
                        "SPEAKER_00": "zh-CN-YunxiNeural",
                        "SPEAKER_01": "zh-CN-XiaoxiaoNeural",
                    },
                    "speaker_stats": [
                        {"speaker": "SPEAKER_00", "cues": 3},
                        {"speaker": "SPEAKER_01", "cues": 5},
                    ],
                },
            }
        ],
    }
    (out / "pipeline.json").write_text(json.dumps(report), encoding="utf-8")

    rows = gui.speaker_rows(out)

    assert rows == [
        ["SPEAKER_00", "zh-CN-YunxiNeural", "3"],
        ["SPEAKER_01", "zh-CN-XiaoxiaoNeural", "5"],
    ]
    assert gui.speaker_rows(tmp_path / "nope") == []
    assert gui.speaker_mapping(rows) == {
        "SPEAKER_00": "zh-CN-YunxiNeural",
        "SPEAKER_01": "zh-CN-XiaoxiaoNeural",
    }
    assert gui.format_speaker_map({"SPEAKER_01": "b", "SPEAKER_00": "a"}) == "SPEAKER_00=a,SPEAKER_01=b"


def test_speaker_table_rows_placeholders() -> None:
    assert gui.speaker_table_rows([]) == [["—", "—", "—", "—"]]
    assert gui.speaker_table_rows([("a.mp4", [])]) == [["a.mp4", "—", "—", "—"]]
    assert gui.speaker_table_rows(
        [("a.mp4", [["SPEAKER_00", "zh-CN-YunxiNeural", "3"]])]
    ) == [["a.mp4", "SPEAKER_00", "zh-CN-YunxiNeural", "3"]]


def test_preview_voice_ui_reports_failure(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise gui.VoicesError("没有网络")

    monkeypatch.setattr(gui, "synthesize_preview", boom)

    path, note = gui.preview_voice_ui("en-US-AriaNeural", "en")

    assert path is None and "试听失败" in note


def test_speaker_mapping_accepts_dataframe_like_input() -> None:
    """回归：Gradio Dataframe 默认 type=pandas 会传 DataFrame，不能对它的 truthiness 取 or。"""

    class _Values:
        def tolist(self):
            return [["a.mp4", "SPEAKER_00", "en-US-GuyNeural", "3"]]

    class FakeFrame:
        values = _Values()

        def __bool__(self):
            raise ValueError("The truth value of a DataFrame is ambiguous")

    assert gui.speaker_mapping(FakeFrame()) == {"SPEAKER_00": "en-US-GuyNeural"}
    assert gui.speaker_mapping({"headers": ["a"], "data": [["b"]]}) == {}
    assert gui.speaker_mapping(None) == {}


def test_stream_revoice_without_plan_yields_hint(tmp_path: Path) -> None:
    controller = gui.RunController()

    steps = list(gui.stream_revoice("en-US-GuyNeural", [], controller=controller))

    status, _log, _files, _batch, _cost, _preview, speakers, _value = steps[-1]
    assert "先点「开始」" in status
    assert speakers == [["—", "—", "—", "—"]]


def test_stream_revoice_rebuilds_argv_with_speaker_map(monkeypatch, tmp_path: Path) -> None:
    FakePopen.commands.clear()
    monkeypatch.setattr(gui.subprocess, "Popen", FakePopen)
    out = tmp_path / "out"
    settings = {
        "src_lang": "zh",
        "tgt_lang": "en",
        "features": {"dub", "burn", "mark"},
        "voice": "",
        "speakers": 0,
        "extra_args": "",
        "force": False,
        "regenerate": False,
        "glossary": None,
        "asr_model": "auto",
        "tts_python": "",
        "speaker_voices": "",
    }
    controller = gui.RunController()
    controller.last_plans = [(str(tmp_path / "a.mp4"), out, settings)]
    controller.last_env = {"PATH": "x"}

    steps = list(
        gui.stream_revoice(
            "en-US-GuyNeural",
            [
                ["a.mp4", "SPEAKER_00", "en-US-GuyNeural", "3"],
                ["a.mp4", "SPEAKER_01", "en-US-AriaNeural", "2"],
            ],
            controller=controller,
        )
    )

    status = steps[-1][0]
    command = FakePopen.commands[-1]
    assert "完成：1/1" in status
    assert command[command.index("--voice") + 1] == "en-US-GuyNeural"
    assert command[command.index("--speaker-voices") + 1] == (
        "SPEAKER_00=en-US-GuyNeural,SPEAKER_01=en-US-AriaNeural"
    )
    assert "--stop-after" not in command
    # 重配后仍然记得这批参数，方便连续改音色
    assert controller.last_plans[0][2]["speaker_voices"].startswith("SPEAKER_00=")


# ---------------------------------------------------------------- 任务历史（OPT-8）


def _stage_popen(exit_code: int = 0, *, speakers: int = 2):
    """在 FakePopen 的产物上补 stages/wall_s，用来测历史里的阶段/人数记录。"""

    class StagePopen(FakePopen):
        def __init__(self, command, **kwargs) -> None:  # noqa: ANN001
            super().__init__(command, **kwargs)
            out = Path(command[command.index("--out") + 1])
            path = out / "pipeline.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = "ok" if exit_code == 0 else "partial"
            payload["wall_s"] = 12.5
            payload["stages"] = [
                {"stage": "probe", "status": "ok"},
                {
                    "stage": "diarize",
                    "status": "ok",
                    "summary": {"speakers": speakers},
                },
                {"stage": "dub", "status": "failed" if exit_code else "ok", "error": "TTS 失败"},
            ]
            if exit_code:
                payload["warnings"] = ["配音失败 1 条"]
            path.write_text(json.dumps(payload), encoding="utf-8")

        def wait(self) -> int:
            return exit_code

    return StagePopen


def test_task_form_values_restores_form() -> None:
    task = {
        "id": 7,
        "source": r"D:\v\a.mp4",
        "out_dir": r"D:\out\a",
        "settings": {
            "src_lang": "zh",
            "tgt_lang": "en",
            "features": ["dub", "diarize", "burn"],
            "voice": "en-US-GuyNeural",
            "speakers": 3,
            "extra_args": "--burn-position top",
            "asr_model": "medium",
            "tts_python": r"C:\tts\python.exe",
            "speaker_voices": "SPEAKER_00=en-US-GuyNeural",
            "force": True,
            "regenerate": False,
            "glossary": r"D:\terms.json",
        },
    }

    values = gui.task_form_values(task)
    assert values["src_path"] == r"D:\v\a.mp4"
    assert values["features"] == ["生成配音成片", "多人声分离", "烧录硬字幕"]
    assert values["voice"] == "en-US-GuyNeural"
    assert values["speakers"] == 3
    assert values["force"] is True and values["regenerate"] is False
    assert values["glossary"] == r"D:\terms.json"

    # 老记录/空设置：给可用默认值，不能把 "None" 填进表单
    fallback = gui.task_form_values({"id": 1, "source": "a.mp4", "out_dir": "out", "settings": None})
    assert fallback["src_lang"] == "auto" and fallback["tgt_lang"] == "zh"
    assert fallback["voice"] == gui.DEFAULT_LABEL
    assert fallback["features"] == [] and fallback["glossary"] is None


def test_history_snapshot_without_store() -> None:
    rows, choices, note = gui.history_snapshot(None)

    assert rows == [] and choices == []
    assert "历史库不可用" in note


def test_stream_run_writes_task_history(monkeypatch, tmp_path: Path) -> None:
    from cuesmith.history import HistoryStore

    FakePopen.commands.clear()
    monkeypatch.setattr(gui.subprocess, "Popen", _stage_popen())
    store = HistoryStore(tmp_path / "history.db")
    source = tmp_path / "a.mp4"
    source.write_bytes(b"a")
    out = tmp_path / "out"

    steps = list(
        gui.stream_run(
            str(source), None, str(out), "zh", "en", ["dub", "mark"], "", 0, "", "", "",
            store=store,
        )
    )

    assert "完成：1/1" in steps[-1][0]
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "ok" and task["exit_code"] == 0
    assert task["source"] == str(source) and task["out_dir"] == str(out)
    assert task["settings"]["src_lang"] == "zh"
    assert task["settings"]["features"] == ["dub", "mark"]
    assert task["argv"][:3] == ["-m", "cuesmith", "pipeline"]
    assert task["stages"]["dub"] == "ok"
    assert task["speakers"] == 2
    assert task["wall_s"] == 12.5
    assert task["cost"]["display"]["amount"] == 0.02
    assert task["error"] is None

    rows, choices, note = gui.history_snapshot(store)
    assert rows[0][2] == "a.mp4" and rows[0][4] == "完成"
    assert choices[0].startswith(f"{task['id']} · a.mp4")
    assert "共 1 条记录" in note


def test_stream_run_records_failed_task(monkeypatch, tmp_path: Path) -> None:
    from cuesmith.history import HistoryStore

    monkeypatch.setattr(gui.subprocess, "Popen", _stage_popen(exit_code=2))
    store = HistoryStore(tmp_path / "history.db")
    source = tmp_path / "b.mp4"
    source.write_bytes(b"b")

    steps = list(gui.stream_run(str(source), None, "", "auto", "zh", ["mark"], "", 0, "", "", "", store=store))

    assert "失败" in steps[-1][0]
    task = store.list_tasks()[0]
    assert task["status"] == "partial" and task["exit_code"] == 2
    assert task["stages"]["dub"] == "failed"
    assert "dub: TTS 失败" in task["error"]


def test_stream_rerun_uses_stored_argv(monkeypatch, tmp_path: Path) -> None:
    from cuesmith.history import HistoryStore

    FakePopen.commands.clear()
    monkeypatch.setattr(gui.subprocess, "Popen", _stage_popen())
    store = HistoryStore(tmp_path / "history.db")
    source = tmp_path / "c.mp4"
    source.write_bytes(b"c")
    settings = {
        "src_lang": "en",
        "tgt_lang": "zh",
        "features": ["dub", "mark"],
        "voice": "",
        "speakers": 0,
        "extra_args": "",
        "force": False,
        "regenerate": False,
        "glossary": None,
        "asr_model": "auto",
        "tts_python": "",
        "speaker_voices": "",
    }
    argv = gui.build_argv(str(source), str(tmp_path / "out"), **settings)
    task_id = store.add_task(
        source=str(source), out_dir=str(tmp_path / "out"), settings=settings, argv=argv
    )
    store.finish_task(task_id, exit_code=0)
    choice = gui.task_choice_label(store.get_task(task_id))
    controller = gui.RunController()

    steps = list(gui.stream_rerun(choice, store=store, controller=controller))

    assert "完成：1/1" in steps[-1][0]
    assert FakePopen.commands[-1][2:] == argv  # sys.executable -u 之后与历史 argv 完全一致
    tasks = store.list_tasks()
    assert len(tasks) == 2
    replay = next(task for task in tasks if task["id"] != task_id)
    assert replay["replay_of"] == task_id and replay["status"] == "ok"
    # 重跑后还能接着「重配音色」
    assert controller.last_plans and controller.last_plans[0][0] == str(source)


def test_stream_rerun_requires_valid_selection(tmp_path: Path) -> None:
    from cuesmith.history import HistoryStore

    store = HistoryStore(tmp_path / "history.db")

    steps = list(gui.stream_rerun(None, store=store, controller=gui.RunController()))
    assert "先在「任务历史」刷新并选一条" in steps[-1][0]

    steps = list(gui.stream_rerun("999", store=store, controller=gui.RunController()))
    assert "先在「任务历史」刷新并选一条" in steps[-1][0]
