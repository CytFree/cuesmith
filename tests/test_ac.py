"""验收条款的显式证明（AC-4 / AC-5 / AC-6）。

单元测试按模块拆开写；这个文件按**需求条款**组织，作为交付时的证据索引：
- AC-4 → tests/test_export_ffmpeg.py（ffprobe 认 codec + ffmpeg 解码无损）
- AC-5 → 本文件 test_ac5_all_violation_kinds_are_reported（一次构造全部违例，零漏报）
- AC-6 → 本文件 test_ac6_bilingual_cues_align_one_to_one
"""

from __future__ import annotations

import json

from submatrix.matrix import MatrixOptions, run_matrix
from submatrix.srt import Cue, Subtitle, Timing, dump_srt, parse_srt

# 7 条 cue，逐条塞一种/多种违例
VIOLATION_SRC = Subtitle(
    cues=(
        Cue(timing=Timing(0, 2000), text="你好世界"),
        Cue(timing=Timing(2000, 3000), text="一二三四五六七八九十"),
        Cue(timing=Timing(3000, 6000), text="第三句"),
        Cue(timing=Timing(6000, 8000), text="第四句"),
        Cue(timing=Timing(8000, 9000), text="第五句"),
        Cue(timing=Timing(9000, 9000), text="第六句"),
        Cue(timing=Timing(10000, 13000), text="第七句"),
    )
)

VIOLATION_TGT = Subtitle(
    cues=(
        Cue(timing=Timing(0, 2000), text="Hello world"),
        # 60 字符一行：同时超行宽（42）与 CPS（60/s）
        Cue(timing=Timing(2000, 3000), text="a" * 60),
        Cue(timing=Timing(3000, 6000), text="   "),  # 空译
        Cue(timing=Timing(6000, 8000), text="第四句"),  # 漏译：与原文相同
        Cue(timing=Timing(8500, 9000), text="Fifth"),  # 时间轴不一致
        Cue(timing=Timing(9000, 9000), text="Sixth"),  # 零时长
        Cue(timing=Timing(10000, 13000), text="one\ntwo\nthree"),  # 3 行
    )
)

EXPECTED_CODES = {
    "cps",
    "empty_translation",
    "line_count",
    "line_width",
    "nonpositive_duration",
    "timeline_mismatch",
    "untranslated",
}


def test_ac5_all_violation_kinds_are_reported(tmp_path) -> None:
    src = tmp_path / "zh.srt"
    tgt = tmp_path / "en.srt"
    src.write_text(dump_srt(VIOLATION_SRC), encoding="utf-8", newline="\n")
    tgt.write_text(dump_srt(VIOLATION_TGT), encoding="utf-8", newline="\n")

    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "out"))
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    reported = {finding["code"] for finding in report["findings"]}

    missing = EXPECTED_CODES - reported
    assert missing == set(), f"漏报：{sorted(missing)}"


def test_ac5_every_finding_is_actionable(tmp_path) -> None:
    """每条违规都要能给人工定位：cue 号 / 指标 / 实测值 / 阈值 / 说明。"""

    src = tmp_path / "zh.srt"
    tgt = tmp_path / "en.srt"
    src.write_text(dump_srt(VIOLATION_SRC), encoding="utf-8", newline="\n")
    tgt.write_text(dump_srt(VIOLATION_TGT), encoding="utf-8", newline="\n")

    result = run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "out"))
    report = json.loads(result.report_path.read_text(encoding="utf-8"))

    for finding in report["findings"]:
        assert finding["cue"] is not None
        assert finding["metric"]
        assert finding["threshold"] is not None
        assert finding["message"]


def test_ac5_nothing_is_auto_corrected(tmp_path) -> None:
    """校验层报出问题，但产物必须与输入一一对应：条数不变、时间轴不变、文案不变。"""

    src = tmp_path / "zh.srt"
    tgt = tmp_path / "en.srt"
    src.write_text(dump_srt(VIOLATION_SRC), encoding="utf-8", newline="\n")
    tgt.write_text(dump_srt(VIOLATION_TGT), encoding="utf-8", newline="\n")

    run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "out"))
    mono = parse_srt((tmp_path / "out" / "en.srt").read_text(encoding="utf-8"))

    assert len(mono) == len(VIOLATION_TGT)
    assert [cue.timing for cue in mono] == [cue.timing for cue in VIOLATION_TGT]
    # 文案不做改写；只做行首尾空白归一（SRT 格式本身不携带行尾空格）
    assert [cue.text.strip() for cue in mono] == [cue.text.strip() for cue in VIOLATION_TGT]


def test_ac6_bilingual_cues_align_one_to_one(tmp_path) -> None:
    src = tmp_path / "zh.srt"
    tgt = tmp_path / "en.srt"
    src.write_text(dump_srt(VIOLATION_SRC), encoding="utf-8", newline="\n")
    tgt.write_text(dump_srt(VIOLATION_TGT), encoding="utf-8", newline="\n")

    run_matrix(MatrixOptions(src=src, tgt=tgt, out=tmp_path / "out"))
    bilingual = parse_srt((tmp_path / "out" / "bilingual.srt").read_text(encoding="utf-8"))

    assert len(bilingual) == len(VIOLATION_SRC)
    for index, (src_cue, tgt_cue, bi_cue) in enumerate(
        zip(VIOLATION_SRC.cues, VIOLATION_TGT.cues, bilingual.cues), start=1
    ):
        assert bi_cue.timing == src_cue.timing, f"第 {index} 条时间轴应取原文"
        lines = bi_cue.text.split("\n")
        if tgt_cue.text.strip():
            assert lines[0] == src_cue.text.strip()
            assert "\n".join(lines[1:]) == tgt_cue.text.strip()
