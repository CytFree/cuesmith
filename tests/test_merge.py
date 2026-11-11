"""FR-3 前置：ASR 句级合并（AC-13）。

M0 实测的动机：Whisper 产出 2–3 秒的碎片 cue，直接拿去翻译+配音会让译文撑爆槽位
（不合并时 10/10 条溢出、最大 +66%）。合并成句子后溢出降到 5/6、+22%，端到端 80s → 52s。

合并规则（PRD）：
- 按句末标点（。！？.!?…）切句
- 单条上限 12 秒
- 相邻碎片间隔 ≤ 1 秒才合并
"""

from __future__ import annotations

import json

import pytest

from submatrix.merge import (
    MergeError,
    MergeOptions,
    merge_cues,
    merge_cues_with_speakers,
    run_merge,
)
from submatrix.srt import Cue, Subtitle, Timing, load_srt


def sub(*pairs: tuple[int, int, str]) -> Subtitle:
    return Subtitle(
        cues=tuple(Cue(timing=Timing(start, end), text=text) for start, end, text in pairs)
    )


def texts(subtitle: Subtitle) -> list[str]:
    return [cue.text for cue in subtitle.cues]


def test_merges_fragments_until_sentence_end() -> None:
    # 中文：碎片之间不加空格
    source = sub((0, 2000, "今天我们"), (2000, 4000, "讨论三个话题"))

    merged = merge_cues(source)

    assert texts(merged) == ["今天我们讨论三个话题"]
    assert merged.cues[0].timing == Timing(0, 4000)


def test_splits_after_sentence_punctuation() -> None:
    source = sub((0, 2000, "第一句。"), (2200, 4000, "第二句。"))

    merged = merge_cues(source)

    assert texts(merged) == ["第一句。", "第二句。"]


def test_multi_sentence_cue_is_kept_whole() -> None:
    """一条 ASR cue 里本来就带多句时不拆分：拆开的边界只能靠猜，会把配音槽位切碎。

    实测（英文播客 2 分钟）：按句子拆成 48 条后，42/48 条配音需要人工重写；
    保持 27 条整段后槽位真实可用。
    """

    source = sub((0, 3000, "第一句。第二句"), (3000, 5000, "的后半段。"))

    merged = merge_cues(source)

    assert texts(merged) == ["第一句。第二句的后半段。"]
    assert merged.cues[0].timing == Timing(0, 5000)


def test_multi_sentence_single_cue_keeps_its_timing() -> None:
    source = sub((0, 6000, "First one. Second one. Third one."))

    merged = merge_cues(source)

    assert len(merged) == 1
    assert merged.cues[0].timing == Timing(0, 6000)
    assert merged.cues[0].timing.duration_ms > 0


@pytest.mark.parametrize("mark", ["。", "！", "？", ".", "!", "?", "…"])
def test_all_sentence_marks_close_a_sentence(mark: str) -> None:
    source = sub((0, 2000, f"内容{mark}"), (2000, 4000, "下一句。"))

    merged = merge_cues(source)

    assert len(merged) == 2


def test_english_fragments_are_joined_with_space() -> None:
    source = sub((0, 2000, "we will discuss"), (2000, 4000, "three topics"))

    assert texts(merge_cues(source)) == ["we will discuss three topics"]


def test_chinese_fragments_are_joined_without_space() -> None:
    source = sub((0, 2000, "人工智能"), (2000, 4000, "改变了视频制作"))

    assert texts(merge_cues(source)) == ["人工智能改变了视频制作"]


def test_respects_max_duration() -> None:
    # 两条各 7 秒，合并会超过 12 秒上限 → 不能合并
    source = sub((0, 7000, "这句话有点长"), (7000, 14000, "而且还在继续"))

    merged = merge_cues(source, MergeOptions(max_duration_ms=12000))

    assert len(merged) == 2


def test_respects_max_gap() -> None:
    # 间隔 2.5 秒 > 1 秒上限 → 不合并
    source = sub((0, 2000, "前半句"), (4500, 6000, "后半句"))

    assert len(merge_cues(source)) == 2


def test_merges_when_gap_within_limit() -> None:
    source = sub((0, 2000, "前半句"), (2800, 4000, "后半句"))

    assert len(merge_cues(source)) == 1


def test_preserves_all_text_without_loss() -> None:
    source = sub((0, 1000, "a"), (1000, 2000, "b"), (2000, 3000, "c。"))

    merged = merge_cues(source)

    assert "".join(texts(merged)).replace(" ", "") == "abc。"


def test_never_returns_empty_cue_text() -> None:
    source = sub((0, 1000, "  "), (1000, 2000, "有内容。"))

    merged = merge_cues(source)

    assert all(cue.text.strip() for cue in merged.cues)


def test_single_cue_is_returned_as_is() -> None:
    source = sub((0, 2000, "一句话。"))

    assert merge_cues(source) == source


def test_empty_subtitle() -> None:
    assert merge_cues(Subtitle()) == Subtitle()


def test_merged_timing_spans_first_to_last() -> None:
    source = sub((1000, 2000, "上"), (2000, 2500, "中"), (2500, 4000, "下。"))

    assert merge_cues(source).cues[0].timing == Timing(1000, 4000)


def test_joins_across_false_period_when_next_starts_lowercase() -> None:
    """ASR 段尾被 fix_punc 补了句号，但下一段是小写开头 → 同一句，必须合并。

    这是英文播客字幕"意思不通"的根因：句子被从中间切断，翻译只能按半句翻。
    """

    source = sub(
        (0, 3340, "you're talking to an AI and the AI knows what it's capable of, and it could."),
        (3340, 6460, "suggest to you, hey, you asked me to do this thing for you."),
    )

    merged = merge_cues(source)

    assert len(merged) == 1
    assert "could suggest to you" in merged.cues[0].text


def test_joins_when_next_starts_with_continuation_word_capitalized() -> None:
    source = sub((0, 2000, "I did some research."), (2000, 4000, "And then it worked."))

    assert len(merge_cues(source)) == 1


def test_does_not_join_real_sentence_boundary() -> None:
    source = sub((0, 2000, "First sentence."), (2000, 4000, "Second sentence."))

    assert len(merge_cues(source)) == 2


def test_does_not_join_continuation_across_large_gap() -> None:
    """间隔 >1s 说明是真正的停顿，不算同一句。"""

    source = sub((0, 2000, "It could."), (5000, 7000, "suggest things."))

    assert len(merge_cues(source)) == 2


def test_chinese_sentences_are_never_joined_by_case_rule() -> None:
    """中文没有大小写线索，不能靠这条规则合并（否则会把独立句子粘在一起）。"""

    source = sub((0, 2000, "第一句。"), (2000, 4000, "第二句。"))

    assert len(merge_cues(source)) == 2


def test_whisper_line_wraps_are_flattened() -> None:
    """whisper 按字符宽度折行，不是语义换行；合并前要拍平成一行。"""

    source = sub((0, 3000, "you're talking to an AI, and the\nAI knows what it's capable of"))

    merged = merge_cues(source)

    assert "\n" not in merged.cues[0].text
    assert merged.cues[0].text.startswith("you're talking to an AI, and the AI knows")


def test_option_defaults_match_prd() -> None:
    defaults = MergeOptions()

    assert defaults.max_duration_ms == 12000
    assert defaults.max_gap_ms == 1000


def test_unpunctuated_tail_is_kept_as_own_cue() -> None:
    source = sub((0, 2000, "第一句。"), (2000, 3000, "没有句号的尾巴"))

    merged = merge_cues(source)

    assert texts(merged) == ["第一句。", "没有句号的尾巴"]


def test_no_split_across_cue_boundary_with_trailing_tail() -> None:
    """上一条 cue 的尾巴（无标点）与下一条同句 → 合并成一条，时间轴取两端。"""

    source = sub((0, 3000, "第一句。今天"), (3000, 5000, "我们讨论话题。"))

    merged = merge_cues(source)

    assert texts(merged) == ["第一句。今天我们讨论话题。"]
    assert merged.cues[0].timing == Timing(0, 5000)


def test_merged_cues_never_have_zero_duration() -> None:
    """合并结果里不能出现 0 时长 cue（曾经因为拆分导致 12/48 条零时长，被 build 拒绝）。"""

    source = sub((0, 2000, "a"), (2000, 4000, "b。"), (4000, 6000, "c"))

    merged = merge_cues(source)

    assert all(cue.timing.duration_ms > 0 for cue in merged.cues)
    for earlier, later in zip(merged.cues, merged.cues[1:]):
        assert later.timing.start_ms >= earlier.timing.start_ms


# ---------------------------------------------------------------- 落盘（run_merge）


def test_run_merge_writes_srt_and_report(tmp_path) -> None:
    from submatrix.report import file_digest
    from submatrix.srt import dump_srt

    src = tmp_path / "source.srt"
    src.write_text(
        dump_srt(sub((0, 2000, "为了生存，它们展开"), (2000, 4000, "一场争夺战。"))),
        encoding="utf-8",
    )

    result = run_merge(src, tmp_path / "merge")

    assert result.exit_code == 0
    assert result.cues_before == 2
    assert result.cues_after == 1
    merged = load_srt(result.merged_path)[0]
    assert [cue.text for cue in merged.cues] == ["为了生存，它们展开一场争夺战。"]
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["summary"] == {
        "before": 2,
        "after": 1,
        "merged_ratio": 0.5,
        "merged_away": 1,
        "speakers": None,
    }
    # input.sha256 是 pipeline 判断缓存是否过期的依据（人工校对后必须重算）
    assert report["input"]["sha256"] == file_digest(src)[1]
    assert report["outputs"][0]["path"] == str(result.merged_path)


def test_run_merge_rejects_missing_or_empty_input(tmp_path) -> None:
    with pytest.raises(MergeError, match="不存在"):
        run_merge(tmp_path / "nope.srt", tmp_path / "out")
    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(MergeError, match="没有可用 cue"):
        run_merge(empty, tmp_path / "out")

# ---------------------------------------------------------------- 多人声（M11）


def test_speaker_change_prevents_merge() -> None:
    """两个说话人的相邻台词必须分开：并成一条就只能由一个音色来读。"""

    source = sub((0, 2000, "我们开始吧"), (2000, 4000, "好的，没问题。"))

    merged, speakers = merge_cues_with_speakers(
        source, MergeOptions(speakers=("SPEAKER_00", "SPEAKER_01"))
    )

    assert texts(merged) == ["我们开始吧", "好的，没问题。"]
    assert speakers == ("SPEAKER_00", "SPEAKER_01")


def test_same_speaker_still_merges() -> None:
    source = sub((0, 2000, "我们开始吧"), (2000, 4000, "好的没问题。"))

    merged, speakers = merge_cues_with_speakers(
        source, MergeOptions(speakers=("SPEAKER_00", "SPEAKER_00"))
    )

    assert texts(merged) == ["我们开始吧好的没问题。"]
    assert speakers == ("SPEAKER_00",)


def test_speaker_count_mismatch_raises() -> None:
    source = sub((0, 2000, "一"), (2000, 4000, "二"))

    with pytest.raises(MergeError, match="对不上"):
        merge_cues_with_speakers(source, MergeOptions(speakers=("SPEAKER_00",)))


def test_run_merge_records_speaker_map(tmp_path) -> None:
    source = tmp_path / "asr.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n我们开始吧\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n好的没问题。\n",
        encoding="utf-8",
        newline="\n",
    )
    speakers_file = tmp_path / "speakers.json"
    speakers_file.write_text('{"speakers": ["SPEAKER_00", "SPEAKER_01"]}', encoding="utf-8")

    result = run_merge(
        source,
        tmp_path / "out",
        MergeOptions(
            speakers=("SPEAKER_00", "SPEAKER_01"), speakers_path=speakers_file
        ),
    )

    report = result.report
    assert report["config"]["speaker_aware"] is True
    assert report["speaker_map"] == [
        {"cue": 1, "speaker": "SPEAKER_00"},
        {"cue": 2, "speaker": "SPEAKER_01"},
    ]
    assert len(report["input"]["speakers"]["sha256"]) == 64
    assert [cue.text for cue in load_srt(result.merged_path)[0].cues] == [
        "我们开始吧",
        "好的没问题。",
    ]
