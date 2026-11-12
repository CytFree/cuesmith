"""音乐/噪声字幕过滤（避免 LLM 被重复音节带崩）。"""

from __future__ import annotations

import json

from submatrix.clean import cleanup_speakers, cleanup_srt, is_degenerate_text


def write_srt(path, cues):
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"{index}\n00:00:0{index},000 --> 00:00:0{index},900\n{text}\n" for index, text in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def test_is_degenerate_text_recognizes_music_noise() -> None:
    assert is_degenerate_text("咪咪咪咪咪咪咪咪")
    assert is_degenerate_text("咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪咪")
    assert is_degenerate_text("mimimimimimi")
    assert is_degenerate_text("啦啦啦啦啦啦")
    assert is_degenerate_text("哈哈哈哈哈哈哈哈")


def test_is_degenerate_text_keeps_real_lines() -> None:
    assert not is_degenerate_text("输血还需要补充生命盐水咪")
    assert not is_degenerate_text("盐也会引发战争吗?")
    assert not is_degenerate_text("咪")  # 太短不武断丢
    assert not is_degenerate_text("是的，是的，没问题")


def test_cleanup_srt_drops_and_renumbers(tmp_path) -> None:
    source = write_srt(tmp_path / "merged.srt", ["盐也会引发战争吗", "咪咪咪咪咪咪咪咪咪咪", "你知道是什么吸引它们吗"])
    result = cleanup_srt(source, tmp_path / "clean.srt")

    assert result.dropped == (2,)
    assert result.kept == 2
    assert result.srt_path == tmp_path / "clean.srt"
    text = result.srt_path.read_text(encoding="utf-8")
    assert "咪" not in text
    assert "盐也会引发战争吗" in text
    assert "2\n00:00:03,000 --> 00:00:03,900" in text  # 重编号


def test_cleanup_srt_drops_neighbor_short_interjection(tmp_path) -> None:
    source = write_srt(tmp_path / "merged.srt", ["咪咪咪咪咪咪咪咪咪咪", "咪", "你知道是什么吸引它们吗"])
    result = cleanup_srt(source, tmp_path / "clean.srt")

    assert result.dropped == (1, 2)
    assert result.kept == 1
    assert "咪" not in result.srt_path.read_text(encoding="utf-8")


def test_cleanup_srt_without_drops_returns_source(tmp_path) -> None:
    source = write_srt(tmp_path / "merged.srt", ["盐也会引发战争吗", "你知道是什么吸引它们吗"])
    out = tmp_path / "clean.srt"

    result = cleanup_srt(source, out)

    assert result.dropped == ()
    assert result.srt_path == source
    assert not out.exists()


def test_cleanup_speakers_remaps_cue_numbers(tmp_path) -> None:
    source = tmp_path / "speakers.merged.json"
    source.write_text(
        json.dumps(
            {
                "speakers": ["SPEAKER_00", "SPEAKER_01"],
                "cues": [
                    {"cue": 1, "speaker": "SPEAKER_00"},
                    {"cue": 2, "speaker": "SPEAKER_01"},
                    {"cue": 3, "speaker": "SPEAKER_00"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = cleanup_speakers(source, (2,), tmp_path / "speakers.clean.json")

    assert out is not None
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [item["cue"] for item in payload["cues"]] == [1, 2]
    assert [item["speaker"] for item in payload["cues"]] == ["SPEAKER_00", "SPEAKER_00"]
    assert payload["speakers"] == ["SPEAKER_00", "SPEAKER_01"]


def test_cleanup_speakers_without_drops_returns_source(tmp_path) -> None:
    source = tmp_path / "speakers.merged.json"
    source.write_text(json.dumps({"cues": [{"cue": 1, "speaker": "SPEAKER_00"}]}), encoding="utf-8")

    assert cleanup_speakers(source, (), tmp_path / "clean.json") == source
