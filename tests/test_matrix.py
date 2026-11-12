"""端到端：加载 → 校验 → 导出 5 格式 → 写 report.json，以及 CLI 契约。

对应验收：
- AC-4 产物落盘且非空（可解析性见 test_export_ffmpeg.py）
- AC-5 违规样本 100% 进报告，字段齐全
- AC-6 双语 SRT 逐条对应（端到端复核）
- 退出码契约：0 = 通过（含仅 warning）；2 = 结构校验不通过；1 = 用法/IO/解析错误
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cuesmith.cli import main
from cuesmith.matrix import MatrixOptions, run_matrix
from cuesmith.srt import dump_srt, parse_srt
from cuesmith.validate import Limits

NOW = datetime(2026, 10, 20, 18, 0, 0, tzinfo=timezone(timedelta(hours=8)))

CLEAN_SRC = "1\n00:00:00,000 --> 00:00:02,000\n你好世界\n\n2\n00:00:02,500 --> 00:00:04,000\n再见\n"
CLEAN_TGT = "1\n00:00:00,000 --> 00:00:02,000\nHello world\n\n2\n00:00:02,500 --> 00:00:04,000\nBye\n"

LONG_LINE_TGT = (
    "1\n00:00:00,000 --> 00:00:02,000\n" + "a" * 60 + "\n"
    "\n"
    "2\n00:00:02,500 --> 00:00:04,000\nBye\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def pair(tmp_path: Path, src_text: str = CLEAN_SRC, tgt_text: str = CLEAN_TGT) -> tuple[Path, Path]:
    return (
        write(tmp_path / "in" / "zh.srt", src_text),
        write(tmp_path / "in" / "en.srt", tgt_text),
    )


def run(tmp_path: Path, *, src_text: str = CLEAN_SRC, tgt_text: str = CLEAN_TGT):
    src, tgt = pair(tmp_path, src_text, tgt_text)
    out = tmp_path / "work" / "demo"
    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=out, generated_at=NOW))
    return result, out


# ---------------------------------------------------------------- 产物


def test_writes_five_formats_plus_report(tmp_path) -> None:
    result, out = run(tmp_path)

    assert sorted(path.name for path in result.outputs) == [
        "bilingual.ass",
        "bilingual.srt",
        "bilingual.vtt",
        "en.srt",
        "en.txt",
    ]
    assert result.report_path == out / "report.json"
    for path in (*result.outputs, result.report_path):
        assert path.is_file()
        assert path.stat().st_size > 0


def test_outputs_are_written_as_utf8_lf(tmp_path) -> None:
    result, _ = run(tmp_path)

    bilingual = next(path for path in result.outputs if path.name == "bilingual.srt")
    raw = bilingual.read_bytes()

    assert b"\r\n" not in raw
    assert "你好世界".encode() in raw


def test_bilingual_srt_pairs_each_cue(tmp_path) -> None:
    result, _ = run(tmp_path)

    bilingual = next(path for path in result.outputs if path.name == "bilingual.srt")
    parsed = parse_srt(bilingual.read_text(encoding="utf-8"))

    assert [cue.text for cue in parsed.cues] == ["你好世界\nHello world", "再见\nBye"]


def test_ass_has_one_dialogue_per_cue(tmp_path) -> None:
    result, _ = run(tmp_path)

    ass = next(path for path in result.outputs if path.name == "bilingual.ass")

    assert ass.read_text(encoding="utf-8").count("Dialogue: ") == 2


def test_txt_contains_translation_only(tmp_path) -> None:
    result, _ = run(tmp_path)

    txt = next(path for path in result.outputs if path.name == "en.txt")

    assert txt.read_text(encoding="utf-8") == "Hello world\nBye\n"


# ---------------------------------------------------------------- 报告


def test_report_schema_and_status_ok(tmp_path) -> None:
    result, _ = run(tmp_path)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["schema_version"] == 1
    assert report["tool"] == "cuesmith"
    assert report["command"] == "build"
    assert report["status"] == "ok"
    assert report["generated_at"] == NOW.isoformat()
    assert report["summary"] == {
        "cues": 2,
        "errors": 0,
        "warnings": 0,
        "outputs": 5,
        "by_code": {},
    }
    assert report["config"] == {
        "src_lang": "zh",
        "tgt_lang": "en",
        "cps_src": 9.0,
        "cps_tgt": 20.0,
        "width_src": 16,
        "width_tgt": 42,
        "max_lines": 2,
        # 排版开关进报告：pipeline 拿它当 build 阶段的缓存判据
        "layout": False,
    }
    assert report["notes"] == []
    assert report["findings"] == []


def test_report_inputs_carry_hash_encoding_and_lang(tmp_path) -> None:
    result, _ = run(tmp_path)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    source = report["inputs"]["source"]

    assert Path(source["path"]) == (tmp_path / "in" / "zh.srt").resolve()
    assert source["cues"] == 2
    assert source["lang"] == "zh"
    assert source["encoding"] == "utf-8"
    assert source["bytes"] == (tmp_path / "in" / "zh.srt").stat().st_size
    assert len(source["sha256"]) == 64


def test_report_lists_violations_with_all_fields(tmp_path) -> None:
    result, _ = run(tmp_path, tgt_text=LONG_LINE_TGT)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["status"] == "warn"
    assert report["summary"]["warnings"] >= 1
    assert report["summary"]["by_code"]["line_width"] == 1

    width_finding = next(f for f in report["findings"] if f["code"] == "line_width")
    assert width_finding["severity"] == "warning"
    assert width_finding["cue"] == 1
    assert width_finding["lang"] == "en"
    assert width_finding["metric"] == "line_width"
    assert width_finding["value"] == 60
    assert width_finding["threshold"] == 42
    assert "42" in width_finding["message"]


def test_report_outputs_listed_with_bytes(tmp_path) -> None:
    result, _ = run(tmp_path)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert len(report["outputs"]) == 5
    for entry in report["outputs"]:
        assert Path(entry["path"]).is_file()
        assert entry["bytes"] == Path(entry["path"]).stat().st_size
        assert entry["cues"] == 2
    assert {entry["kind"] for entry in report["outputs"]} == {
        "mono_srt",
        "bilingual_srt",
        "ass",
        "vtt",
        "txt",
    }


# ---------------------------------------------------------------- 条数不一致


def test_count_mismatch_exports_nothing_but_still_writes_report(tmp_path) -> None:
    result, out = run(tmp_path, tgt_text="1\n00:00:00,000 --> 00:00:02,000\nHello world\n")

    assert result.status == "fail"
    assert result.exit_code == 2
    assert result.outputs == ()
    assert result.report_path.is_file()
    assert not (out / "bilingual.srt").exists()

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "fail"
    assert [finding["code"] for finding in report["findings"]] == ["cue_count_mismatch"]
    assert report["notes"] == ["译文条数与原文不一致，未导出任何字幕文件"]
    assert report["outputs"] == []


def test_count_mismatch_finding_has_no_cue_number(tmp_path) -> None:
    result, _ = run(tmp_path, tgt_text="1\n00:00:00,000 --> 00:00:02,000\nHello world\n")
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["findings"][0]["cue"] is None
    assert report["findings"][0]["value"] == 1
    assert report["findings"][0]["threshold"] == 2


LONG_SRC = (
    "1\n00:00:00,000 --> 00:00:08,000\n"
    "This is a fairly long English sentence that will definitely not fit in two lines of subtitles.\n"
)
LONG_TGT = (
    "1\n00:00:00,000 --> 00:00:08,000\n"
    "这是一句相当长的中文字幕内容，肯定需要超过两行的空间来完整显示，所以要切成多条才行。\n"
)


def test_wrap_splits_long_cues_and_clears_violations(tmp_path) -> None:
    src, tgt = pair(tmp_path, LONG_SRC, LONG_TGT)
    out = tmp_path / "wrap"
    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=out, layout=True))
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["layout"]["enabled"] is True
    assert report["layout"]["cues_after"] > report["layout"]["cues_before"]
    assert report["layout"]["violations_after"]["by_code"].get("line_width", 0) == 0
    assert report["layout"]["violations_after"]["by_code"].get("line_count", 0) == 0

    bilingual = parse_srt((out / "bilingual.srt").read_text(encoding="utf-8"))
    for cue in bilingual.cues:
        lines = cue.text.split("\n")
        assert len(lines) <= 4  # 中英各最多 2 行
    for cue in parse_srt((out / "en.srt").read_text(encoding="utf-8")).cues:
        assert len(cue.text.split("\n")) <= 2
        assert max(len(line) for line in cue.text.split("\n")) <= 42


def test_wrap_keeps_bilingual_pairs_aligned(tmp_path) -> None:
    src, tgt = pair(tmp_path, LONG_SRC, LONG_TGT)
    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "wrap", layout=True))

    en = parse_srt((tmp_path / "wrap" / "en.srt").read_text(encoding="utf-8"))
    bilingual = parse_srt((tmp_path / "wrap" / "bilingual.srt").read_text(encoding="utf-8"))

    assert len(en) == len(bilingual)
    assert result.report["layout"]["cues_after"] == len(en)


def test_without_wrap_cue_count_is_unchanged(tmp_path) -> None:
    src, tgt = pair(tmp_path, LONG_SRC, LONG_TGT)
    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "plain"))
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert "layout" not in report
    assert report["summary"]["cues"] == 1


# ---------------------------------------------------------------- 可复跑 / 自定义阈值


def test_rerun_is_deterministic(tmp_path) -> None:
    first, out = run(tmp_path)
    snapshot = {path.name: path.read_bytes() for path in first.outputs}
    first_report = json.loads(first.report_path.read_text(encoding="utf-8"))

    second = run_matrix(
        MatrixOptions(
            src=tmp_path / "in" / "zh.srt",
            tgt=tmp_path / "in" / "en.srt",
            out=out,
            generated_at=NOW,
        )
    )
    second_report = json.loads(second.report_path.read_text(encoding="utf-8"))

    assert {path.name: path.read_bytes() for path in second.outputs} == snapshot
    assert first_report == second_report
    assert first_report["generated_at"] == NOW.isoformat()


def test_custom_limits_are_recorded_and_applied(tmp_path) -> None:
    src, tgt = pair(tmp_path)
    out = tmp_path / "work" / "strict"
    result = run_matrix(
        MatrixOptions(
            src=src,
            tgt=tgt,
            out=out,
            limits=Limits(cps_tgt=1.0),
            generated_at=NOW,
        )
    )
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    assert report["config"]["cps_tgt"] == 1.0
    assert report["status"] == "warn"
    assert "cps" in report["summary"]["by_code"]


# ---------------------------------------------------------------- CLI


def test_cli_returns_0_on_clean_pair(tmp_path, capsys) -> None:
    src, tgt = pair(tmp_path)
    code = main(["build", "--src", str(src), "--tgt", str(tgt), "--out", str(tmp_path / "out")])

    assert code == 0
    captured = capsys.readouterr()
    assert "report.json" in captured.out
    assert "ok" in captured.out


def test_cli_returns_0_when_only_warnings(tmp_path) -> None:
    src, tgt = pair(tmp_path, tgt_text=LONG_LINE_TGT)

    code = main(["build", "--src", str(src), "--tgt", str(tgt), "--out", str(tmp_path / "out")])

    assert code == 0


def test_cli_returns_2_on_structural_failure(tmp_path, capsys) -> None:
    src, tgt = pair(tmp_path, tgt_text="1\n00:00:00,000 --> 00:00:02,000\nHello world\n")

    code = main(["build", "--src", str(src), "--tgt", str(tgt), "--out", str(tmp_path / "out")])

    assert code == 2
    assert "条数" in capsys.readouterr().err


def test_cli_returns_1_on_missing_input(tmp_path) -> None:
    src, tgt = pair(tmp_path)

    code = main(
        ["build", "--src", str(tmp_path / "nope.srt"), "--tgt", str(tgt), "--out", str(tmp_path / "o")]
    )

    assert code == 1


def test_cli_returns_1_on_unknown_language(tmp_path) -> None:
    src, tgt = pair(tmp_path)

    code = main(
        ["build", "--src", str(src), "--tgt", str(tgt), "--out", str(tmp_path / "o"), "--src-lang", "ja"]
    )

    assert code == 1


def test_cli_returns_1_on_broken_srt(tmp_path) -> None:
    bad = write(tmp_path / "in" / "bad.srt", "1\nnot a timing line\nHi\n")
    _, tgt = pair(tmp_path)

    code = main(["build", "--src", str(bad), "--tgt", str(tgt), "--out", str(tmp_path / "o")])

    assert code == 1


def test_cli_accepts_threshold_overrides(tmp_path) -> None:
    src, tgt = pair(tmp_path)

    code = main(
        [
            "build",
            "--src",
            str(src),
            "--tgt",
            str(tgt),
            "--out",
            str(tmp_path / "out"),
            "--cps-tgt",
            "1",
            "--width-tgt",
            "10",
        ]
    )
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))

    assert code == 0
    assert report["config"]["cps_tgt"] == 1.0
    assert report["config"]["width_tgt"] == 10
    assert report["status"] == "warn"


def test_cli_help_is_available(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    assert "build" in capsys.readouterr().out


def test_generated_report_can_be_read_back_by_own_parser(tmp_path) -> None:
    """自产 en.srt 必须能被自己的解析器读回（时间轴毫秒不漂移）。"""

    result, _ = run(tmp_path)
    mono = next(path for path in result.outputs if path.name == "en.srt")

    assert parse_srt(mono.read_text(encoding="utf-8")) == parse_srt(dump_srt(parse_srt(CLEAN_TGT)))
