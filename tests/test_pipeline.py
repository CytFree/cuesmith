"""pipeline：一条命令编排全链路 + 阶段缓存。

不跑真实网络/模型：所有阶段用假函数注入，验的是编排契约——
自动分流（内封字幕 vs ASR）、阶段顺序、缓存复用、失败短路、报告与退出码。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cuesmith import cli as cli_module
from cuesmith import media as media_module
from cuesmith import pipeline as pipeline_module
from cuesmith.asr import model_variant, preferred_model
from cuesmith.cli import build_parser
from cuesmith.diarize import SpeakerTurn, write_segments
from cuesmith.glossary import Glossary, Term, glossary_fingerprint
from cuesmith.pipeline import STAGES, PipelineError, PipelineOptions, run_pipeline
from cuesmith.srt import load_srt
from cuesmith.translate import LlmConfig
from cuesmith.tts import speech_capacity_cps
from cuesmith.validate import Limits

SAMPLE_RATE = 24000


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_src(tmp_path: Path, name: str = "in.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"fake video bytes")
    return path


class Calls:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.options: dict[str, object] = {}


def install_fakes(
    monkeypatch,
    calls: Calls,
    *,
    route: str = "extracted",
    language: str = "zh",
    asr_srt: str = "1\n00:00:00,000 --> 00:00:02,000\n你好\n",
    speaker_count: int = 2,
):
    """把七个阶段函数换成假实现，并把产物落到 out 目录（模拟真实落盘）。"""

    real_out: dict[str, Path] = {}

    def fake_probe(options):
        calls.order.append("probe")
        calls.options["probe"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        src_path = Path(options.src)
        # 按真 manifest 的契约落盘：输入身份（sha256）+ 源语言。
        # 假报告如果只有 {"status": ...}，就测不出"换了视频却复用旧产物"这类缓存 bug。
        manifest_payload = {
            "tool": "cuesmith",
            "status": route,
            "input": {
                "path": str(src_path),
                "bytes": src_path.stat().st_size,
                "sha256": sha256(src_path),
            },
            "src_lang": options.src_lang,
            "decision": {"kind": route},
            "outputs": [],
            "warnings": [],
        }
        manifest = out / "manifest.json"
        manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
        subtitle = None
        if route == "extracted":
            subtitle = out / "source.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        real_out["probe"] = out
        return SimpleNamespace(
            status=route,
            manifest=manifest_payload,
            manifest_path=manifest,
            subtitle_path=subtitle,
            cues=1,
        )

    def fake_asr(options):
        calls.order.append("asr")
        calls.options["asr"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        srt = out / "source.srt"
        srt.write_text(asr_srt, encoding="utf-8")
        words = touch(out / "words.json", "[]")
        # 真 asr.py 的模型解析：显式给了就用，否则按**识别到的语言**挑默认（可能退回 small）。
        # 假报告必须照这个口径记，否则"模型一致"是碰巧成立，缓存判据的用例会假绿。
        recorded_variant = (
            model_variant(options.model)
            if options.model
            else model_variant(preferred_model(language) or "small")
        )
        report_path = out / "asr.json"
        report_path.write_text(
            json.dumps(
                {
                    "summary": {"language": language, "cues": 1, "words": 2, "rtf": 1.0},
                    "input": {
                        "path": str(Path(options.src)),
                        "sha256": sha256(Path(options.src)),
                    },
                    "config": {
                        "model": options.model,
                        "model_variant": recorded_variant,
                        "language": options.language,
                        "hotwords": list(options.hotwords),
                        "initial_prompt": options.initial_prompt,
                    },
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            srt_path=srt,
            words_path=words,
            report_path=report_path,
            report=json.loads(report_path.read_text(encoding="utf-8")),
        )

    def fake_diarize(options):
        calls.order.append("diarize")
        calls.options["diarize"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        speakers = [f"SPEAKER_{index:02d}" for index in range(speaker_count)]
        if options.srt is None:
            # OPT-5 并行预取阶段：只落说话人分段（归属等 ASR 完成后再补）
            turns = [
                SpeakerTurn(start_ms=index * 1000, end_ms=(index + 1) * 1000, speaker=speaker)
                for index, speaker in enumerate(speakers)
            ]
            config = {
                "language": options.language,
                "num_speakers_requested": options.num_speakers or None,
                "threshold": options.resolved_threshold,
                "min_speaker_speech_s": options.min_speaker_speech_s,
                "vad": {
                    "requested": bool(options.vad or options.vad_model),
                    "enabled": bool(options.vad_model),
                    "model": str(options.vad_model) if options.vad_model else None,
                    "threshold": options.vad_threshold,
                    "skipped": None if options.vad_model else "model-missing",
                },
                "clustering": options.clustering,
                "clustering_effective": options.resolved_clustering,
                "merge_alpha": options.merge_alpha if options.merge_alpha is not None else options.merge_alpha_for(False),
            }
            summary = {
                "speakers": len(speakers),
                "speakers_raw": len(speakers),
                "merged_clusters": [],
                "turns": len(turns),
                "speech_s": 1.5,
                "rtf": 0.1,
                "wall_s": 0.5,
            }
            segments_path = write_segments(
                out / "segments.json",
                turns=turns,
                source_path=Path(options.src),
                duration_s=5.0,
                config=config,
                summary=summary,
            )
            report = {
                "config": config,
                "summary": summary,
                "input": {
                    "path": str(options.src),
                    "bytes": Path(options.src).stat().st_size,
                    "sha256": sha256(Path(options.src)),
                    "duration_s": 5.0,
                },
                "warnings": [],
            }
            report_path = out / "diarize.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            assert segments_path.is_file()
            return SimpleNamespace(
                status="ok",
                exit_code=0,
                report=report,
                report_path=report_path,
                speakers_path=None,
                segments=tuple(turns),
            )
        cues: list[dict[str, object]] = []
        srt = Path(options.srt) if options.srt is not None else None
        if srt is not None and srt.is_file():
            subtitle, _ = load_srt(srt)
            cues = [
                {
                    "cue": index,
                    "start_ms": cue.timing.start_ms,
                    "end_ms": cue.timing.end_ms,
                    "speaker": speakers[(index - 1) % len(speakers)],
                }
                for index, cue in enumerate(subtitle.cues, start=1)
            ]
        speakers_path = out / "speakers.json"
        speakers_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tool": "cuesmith",
                    "version": "test",
                    "srt": str(srt) if srt else None,
                    "speakers": speakers,
                    "cues": cues,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = {
            "config": {
                "language": options.language,
                "num_speakers_requested": options.num_speakers or None,
                "threshold": options.resolved_threshold,
                "min_speaker_speech_s": options.min_speaker_speech_s,
                "vad": {
                    "requested": bool(options.vad or options.vad_model),
                    "enabled": bool(options.vad_model),
                    "model": str(options.vad_model) if options.vad_model else None,
                    "threshold": options.vad_threshold,
                    "skipped": None if options.vad_model else "model-missing",
                },
                "clustering": options.clustering,
                "clustering_effective": options.resolved_clustering,
                "merge_alpha": options.merge_alpha if options.merge_alpha is not None else options.merge_alpha_for(False),
            },
            "summary": {
                "speakers": len(speakers),
                "turns": len(speakers),
                "speech_s": 1.5,
                "cues": len(cues),
                "assigned_cues": len(cues),
                "speaker_stats": [
                    {
                        "speaker": speaker,
                        "turns": 1,
                        "speech_s": 0.5,
                        "cues": sum(1 for item in cues if item["speaker"] == speaker),
                        "chars": 1,
                    }
                    for speaker in speakers
                ],
                "rtf": 0.1,
            },
            "input": {
                "path": str(options.src),
                "sha256": sha256(Path(options.src)),
                "srt": {
                    "path": str(srt) if srt else None,
                    "sha256": sha256(srt) if srt is not None else None,
                    "cues": len(cues),
                },
            },
            "warnings": [],
        }
        report_path = out / "diarize.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            status="ok",
            exit_code=0,
            report=report,
            report_path=report_path,
            speakers_path=speakers_path,
            segments=(),
        )

    def fake_translate(options):
        calls.order.append("translate")
        calls.options["translate"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{options.tgt_lang}.srt"
        target.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        report_path = out / "translate.json"
        report_path.write_text(
            json.dumps(
                {
                    "summary": {
                        "cues": 1,
                        "paragraphs": 1,
                        "fallback_cues": 0,
                        "cps": {"mean": 8.0, "over_limit": 0},
                        "condense": {"triggered": 0},
                        "review": {"cues_changed": 0},
                    },
                    "input": {
                        "path": str(Path(options.src)),
                        "sha256": sha256(Path(options.src)),
                    },
                    "config": {
                        "provider": options.provider,
                        "provider_primary": options.provider,
                        "src_lang": options.src_lang,
                        "tgt_lang": options.tgt_lang,
                        "glossary": {
                            "path": str(options.glossary.path)
                            if options.glossary and options.glossary.path
                            else None,
                            "terms": len(options.glossary) if options.glossary else 0,
                            "fingerprint": glossary_fingerprint(options.glossary),
                        },
                        "condense": {
                            "enabled": options.condense,
                            "mode": options.condense_mode if options.condense else None,
                            "dub_cps_budget": options.dub_cps_budget,
                        },
                        "llm": {
                            **(
                                {"base_url": options.llm.base_url, "model": options.llm.model}
                                if options.llm is not None
                                else {}
                            ),
                            "review": options.llm_review,
                            "usage": {"total_tokens": 100},
                        },
                    },
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(output_path=target, report_path=report_path, report=json.loads(
            report_path.read_text(encoding="utf-8")
        ))

    def fake_matrix(options):
        calls.order.append("build")
        calls.options["matrix"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        publish = out / f"{options.tgt_lang}.srt"
        publish.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        bilingual = touch(out / "bilingual.srt", "1\n00:00:00,000 --> 00:00:02,000\nHello\nWorld\n")
        touch(out / "bilingual.ass", "ass")
        limits = options.limits or Limits.from_languages(options.src_lang, options.tgt_lang)
        report_path = out / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "summary": {"cues": 1, "warnings": 0},
                    "inputs": {
                        "source": {"sha256": sha256(Path(options.src))},
                        "target": {"sha256": sha256(Path(options.tgt))},
                    },
                    "config": {
                        "src_lang": options.src_lang,
                        "tgt_lang": options.tgt_lang,
                        "cps_src": limits.cps_src,
                        "cps_tgt": limits.cps_tgt,
                        "width_src": limits.width_src,
                        "width_tgt": limits.width_tgt,
                        "max_lines": limits.max_lines,
                        "layout": bool(options.layout),
                    },
                    "layout": {"enabled": bool(options.layout), "cues_before": 1, "cues_after": 2},
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            status="warn",
            exit_code=0,
            report_path=report_path,
            report=json.loads(report_path.read_text(encoding="utf-8")),
            outputs=(publish, bilingual),
        )

    def fake_dub(options):
        calls.order.append("dub")
        calls.options["dub"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        audio = touch(out / "dubbed.wav", "audio")
        video = touch(out / "dubbed.mp4", "video")
        report_path = out / "dub_report.json"
        inputs: dict[str, object] = {"srt": {"sha256": sha256(Path(options.srt))}}
        if options.speakers_path is not None:
            inputs["speakers"] = {"sha256": sha256(Path(options.speakers_path))}
        report_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "input": inputs,
                    "config": {
                        "voice": options.voice,
                        "tts_rate": options.tts_rate,
                        "voice_by_speaker": dict(options.voice_by_speaker),
                        "tts_command_sha256": (
                            hashlib.sha256(options.tts_command.encode("utf-8")).hexdigest()
                            if options.tts_command
                            else None
                        ),
                        "bgm": {
                            "enabled": options.keep_bgm,
                            "mode": options.bgm_mode if options.keep_bgm else None,
                            "source": str(
                                Path(options.bgm_source or options.video).resolve()
                            )
                            if options.keep_bgm
                            else None,
                            "gain_db": options.bgm_gain_db,
                            "duck_db": options.bgm_duck_db,
                        },
                    },
                    "summary": {
                        "cues": 1,
                        "synthesized": 1,
                        "failed": 0,
                        "manual": 0,
                        "atempo": 0,
                        "absorbed": 0,
                        "speakers": len(set(options.voice_by_speaker)) or None,
                        "bgm": None,
                    },
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            status="ok",
            exit_code=0,
            report_path=report_path,
            report=json.loads(report_path.read_text(encoding="utf-8")),
            audio_path=audio,
            video_path=video,
        )

    def fake_burn(options):
        calls.order.append("burn")
        calls.options["burn"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        burned = touch(out / "burned.mp4", "burned")
        report_path = out / "burn.json"
        report_path.write_text(
            json.dumps(
                {
                    "input": {
                        "video": {"sha256": sha256(Path(options.video))},
                        "subtitle": {
                            "sha256": sha256(Path(options.subtitle)),
                            "cues": 1,
                        },
                    },
                    "output": {"duration_delta_ms": 0},
                    "config": {
                        "crf": options.crf,
                        "preset": options.preset,
                        "position": options.position,
                    },
                    "verification": {"ok": True, "region_pixel_diff": 2.5},
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            status="ok",
            exit_code=0,
            report=json.loads(report_path.read_text(encoding="utf-8")),
            report_path=report_path,
            video_path=burned,
            ass_path=Path(options.subtitle),
        )

    def fake_mark(options):
        calls.order.append("mark")
        calls.options["mark"] = options
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        marked = touch(out / "marked.mp4", "marked")
        report_path = out / "mark.json"
        report_path.write_text(
            json.dumps(
                {
                    "input": {"sha256": sha256(Path(options.src))},
                    "verification": {
                        "explicit_label": {"ok": True},
                        "metadata_roundtrip": {"ok": True},
                    },
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            exit_code=0,
            report=json.loads(report_path.read_text(encoding="utf-8")),
            report_path=report_path,
            video_path=marked,
        )

    monkeypatch.setattr(pipeline_module, "run_probe", fake_probe)
    monkeypatch.setattr(pipeline_module, "run_asr", fake_asr)
    monkeypatch.setattr(pipeline_module, "run_diarize", fake_diarize)
    monkeypatch.setattr(pipeline_module, "run_translate", fake_translate)
    monkeypatch.setattr(pipeline_module, "run_matrix", fake_matrix)
    monkeypatch.setattr(pipeline_module, "run_dub", fake_dub)
    monkeypatch.setattr(pipeline_module, "run_burn", fake_burn)
    monkeypatch.setattr(pipeline_module, "run_mark", fake_mark)
    return real_out


# ---------------------------------------------------------------- 编排


def test_extracted_route_skips_asr(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", tgt_lang="zh"))

    assert result.status == "ok"
    assert result.exit_code == 0
    assert calls.order == ["probe", "translate", "build", "dub", "burn", "mark"]
    assert calls.options["translate"].src_lang == "en"  # type: ignore[union-attr]
    assert calls.options["translate"].provider == "microsoft"  # type: ignore[union-attr]
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    by_stage = {stage["stage"]: stage for stage in report["stages"]}
    assert by_stage["asr"]["status"] == "skipped"
    # 成本块总是落盘（artifact 口径），没有用量时为空账本
    assert report["cost"]["basis"] == "artifact"
    assert report["cost"]["quantities"]["llm"]["total_tokens"] == 100  # 假 translate 报告里的用量
    assert report["config"]["pricing"] is None
    assert report["outputs"]["publish_srt"].endswith("matrix\\zh.srt") or report["outputs"][
        "publish_srt"
    ].endswith("matrix/zh.srt")
    assert report["outputs"]["final_video"].endswith("marked.mp4")


def test_asr_route_uses_detected_language(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    src = make_src(tmp_path)

    result = run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", tgt_lang="en"))

    assert result.status == "ok"
    assert calls.order[0:2] == ["probe", "asr"]
    assert calls.options["asr"].language == "auto"  # type: ignore[union-attr]
    assert calls.options["translate"].src_lang == "zh"  # type: ignore[union-attr]
    assert calls.options["translate"].tgt_lang == "en"  # type: ignore[union-attr]
    # 目标英文：音色/语速默认按目标语言走
    assert calls.options["dub"].voice == "en-US-AriaNeural"  # type: ignore[union-attr]
    assert calls.options["dub"].tts_rate == "+20%"  # type: ignore[union-attr]


def test_asr_route_merges_fragments_before_translate(tmp_path, monkeypatch) -> None:
    """ASR 碎片（半句 + 半句）必须先并成句子再翻译，否则英文也是半句。"""

    calls = Calls()
    install_fakes(
        monkeypatch,
        calls,
        route="route_to_asr",
        asr_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n为了生存，它们展开\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n一场盐的争夺战。\n"
        ),
    )
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en"))

    assert result.status == "ok"
    merge_stage = next(stage for stage in result.report["stages"] if stage["stage"] == "merge")
    assert merge_stage["status"] == "ok"
    assert merge_stage["summary"] == {
        "before": 2,
        "after": 1,
        "merged_away": 1,
        "speakers": None,
    }
    merged = load_srt(out / "merge" / "merged.srt")[0]
    assert [cue.text for cue in merged.cues] == ["为了生存，它们展开一场盐的争夺战。"]
    # 翻译与排版都必须吃合并后的字幕（时间轴才是完整的句子槽位）
    assert calls.options["translate"].src == out / "merge" / "merged.srt"  # type: ignore[union-attr]
    assert calls.options["matrix"].src == out / "merge" / "merged.srt"  # type: ignore[union-attr]
    assert result.report["outputs"]["merged_srt"].endswith("merged.srt")


def test_extracted_route_skips_merge(tmp_path, monkeypatch) -> None:
    """内封字幕是人工断句，默认不合并（否则会把两个人的台词并成一条）。"""

    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    result = run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", src_lang="en"))

    merge_stage = next(stage for stage in result.report["stages"] if stage["stage"] == "merge")
    assert merge_stage["status"] == "skipped"
    assert "merged_srt" not in result.report["outputs"]
    assert calls.options["translate"].src == tmp_path / "out" / "probe" / "source.srt"  # type: ignore[union-attr]


def test_no_merge_keeps_asr_fragments(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(
        monkeypatch,
        calls,
        route="route_to_asr",
        asr_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n为了生存，它们展开\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n一场盐的争夺战。\n"
        ),
    )
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", merge=False))

    merge_stage = next(stage for stage in result.report["stages"] if stage["stage"] == "merge")
    assert merge_stage["status"] == "skipped"
    assert calls.options["translate"].src == out / "asr" / "source.srt"  # type: ignore[union-attr]
    assert not (out / "merge" / "merged.srt").is_file()


def test_zh_target_defaults_voice_and_rate(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", src_lang="en", tgt_lang="zh"))

    assert calls.options["dub"].voice == "zh-CN-XiaoxiaoNeural"  # type: ignore[union-attr]
    assert calls.options["dub"].tts_rate == "+30%"  # type: ignore[union-attr]


def test_glossary_hotwords_are_passed_to_asr(tmp_path, monkeypatch) -> None:
    from cuesmith.glossary import Glossary, Term

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    glossary = Glossary(terms=(Term(source="Blender", keep=True),))

    run_pipeline(
        PipelineOptions(
            src=src, out=tmp_path / "out", glossary=glossary, hotwords=("Astra",)
        )
    )

    assert list(calls.options["asr"].hotwords) == ["Astra", "Blender"]  # type: ignore[union-attr]


def test_stop_after_translate(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    result = run_pipeline(
        PipelineOptions(src=src, out=tmp_path / "out", src_lang="en", stop_after="translate")
    )

    assert calls.order == ["probe", "translate"]
    assert result.status == "ok"


def test_step_table_matches_stage_names() -> None:
    """``STAGES``（CLI 里 ``--stop-after`` 的选项）与 ``_STEPS``（真正执行的顺序）必须一致。

    加了阶段却忘了登记名字，用户就没法 ``--stop-after`` 停在新阶段；反过来登记了却
    没接进流程，`--stop-after` 那个值等于假的。两边都得改，所以两边都得钉住。
    """

    assert tuple(step.stage for step in pipeline_module._STEPS if step.stage) == STAGES


def test_unsupported_route_fails_with_actionable_error(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="unsupported")
    src = make_src(tmp_path)

    result = run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", src_lang="en"))

    assert result.status == "failed"
    assert result.exit_code == 2
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    failed = [stage for stage in report["stages"] if stage["status"] == "failed"]
    assert failed and "图形字幕" in failed[0]["error"]
    assert calls.order == ["probe"]


def test_bad_ffprobe_json_is_recorded_as_failed_stage(tmp_path, monkeypatch) -> None:
    """ffprobe 吐出坏 JSON 时，pipeline 要记 failed 并落盘，而不是崩在半路。

    a6b207a 之前这里是裸 ``json.loads``：``JSONDecodeError``（ValueError）穿透
    ``except (ProbeError, PipelineError, OSError)``，整条 pipeline 崩掉、连
    ``pipeline.json`` 都不写——用户拿不到任何可诊断的信息。现在 media 层把它翻成
    各自的 error_cls，probe 阶段按普通失败收尾。
    """

    src = make_src(tmp_path)
    out = tmp_path / "out"

    def garbage(argv, **_kwargs):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 0, b"{not json", b"")

    monkeypatch.setattr(media_module, "run_ffprobe", garbage)

    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))

    assert result.status == "failed"
    assert result.exit_code == 2
    assert (out / "pipeline.json").is_file()
    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["probe"]["status"] == "failed"
    assert "JSON" in stages["probe"]["error"]


def test_llm_without_key_fails_before_calling_translator(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    result = run_pipeline(
        PipelineOptions(
            src=src, out=tmp_path / "out", src_lang="en", llm=True, llm_config=None
        )
    )

    assert result.status == "failed"
    assert "DEEPSEEK_API_KEY" in json.loads(result.report_path.read_text(encoding="utf-8"))[
        "stages"
    ][-1]["error"]
    assert "translate" not in calls.order


def test_stage_artifacts_are_cached(tmp_path, monkeypatch) -> None:
    src = make_src(tmp_path)
    out = tmp_path / "out"
    source_srt = touch(
        out / "probe" / "source.srt", "1\n00:00:00,000 --> 00:00:02,000\nHi\n"
    )
    translated = touch(
        out / "translate" / "zh.srt", "1\n00:00:00,000 --> 00:00:02,000\n你好\n"
    )
    # 种子的报告必须是**真契约**形状（输入 sha + 影响产物的配置），
    # 否则"缓存判据漏字段"这类 bug 在测试里根本暴露不出来。
    limits = Limits.from_languages("en", "zh")
    touch(
        out / "probe" / "manifest.json",
        json.dumps(
            {
                "status": "extracted",
                "input": {"sha256": sha256(src)},
                "src_lang": "en",
            }
        ),
    )
    touch(
        out / "translate" / "translate.json",
        json.dumps(
            {
                "summary": {},
                "input": {"sha256": sha256(source_srt)},
                "config": {
                    "provider": "microsoft",
                    "provider_primary": "microsoft",
                    "src_lang": "en",
                    "tgt_lang": "zh",
                    "glossary": {"fingerprint": None},
                    "condense": {
                        "enabled": True,
                        "mode": "auto",
                        "dub_cps_budget": speech_capacity_cps("zh", "+30%"),
                    },
                    "llm": {"review": False},
                },
            }
        ),
    )
    touch(
        out / "matrix" / "report.json",
        json.dumps(
            {
                "summary": {},
                "layout": {},
                "inputs": {
                    "source": {"sha256": sha256(source_srt)},
                    "target": {"sha256": sha256(translated)},
                },
                "config": {
                    "src_lang": "en",
                    "tgt_lang": "zh",
                    "cps_src": limits.cps_src,
                    "cps_tgt": limits.cps_tgt,
                    "width_src": limits.width_src,
                    "width_tgt": limits.width_tgt,
                    "max_lines": limits.max_lines,
                    "layout": True,
                },
            }
        ),
    )
    touch(
        out / "matrix" / "zh.srt", "1\n00:00:00,000 --> 00:00:02,000\n你好\n"
    )
    touch(
        out / "dub" / "dub_report.json",
        json.dumps(
            {
                "summary": {},
                "input": {"srt": {"sha256": sha256(translated)}},
                "config": {
                    "voice": "zh-CN-XiaoxiaoNeural",
                    "tts_rate": "+30%",
                    "voice_by_speaker": {},
                    "tts_command_sha256": None,
                    "bgm": {
                        "enabled": False,
                        "mode": None,
                        "source": None,
                        "gain_db": -2.0,
                        "duck_db": -12.0,
                    },
                },
            }
        ),
    )
    touch(out / "dub" / "dubbed.mp4", "video")
    touch(
        out / "burn" / "burn.json",
        json.dumps(
            {
                "input": {
                    "video": {"sha256": sha256(out / "dub" / "dubbed.mp4")},
                    "subtitle": {"sha256": sha256(out / "matrix" / "zh.srt")},
                },
                "output": {},
                "config": {"crf": 18, "preset": "medium", "position": "bottom"},
                "verification": {"ok": True},
            }
        ),
    )
    touch(out / "burn" / "burned.mp4", "burned")
    touch(
        out / "final" / "mark.json",
        json.dumps({"input": {"sha256": sha256(out / "burn" / "burned.mp4")}, "verification": {}}),
    )
    touch(out / "final" / "marked.mp4", "marked")

    calls = Calls()

    def boom(*args, **kwargs):
        raise AssertionError("有缓存时不应重跑阶段")

    for name in (
        "run_probe",
        "run_asr",
        "run_diarize",
        "run_translate",
        "run_matrix",
        "run_dub",
        "run_burn",
        "run_mark",
    ):
        monkeypatch.setattr(pipeline_module, name, boom)

    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))

    assert result.status == "ok"
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["translate"] == "cached"
    assert statuses["dub"] == "cached"
    assert statuses["burn"] == "cached"
    assert statuses["mark"] == "cached"
    assert calls.order == []


def test_edited_upstream_subtitle_invalidates_downstream_cache(tmp_path, monkeypatch) -> None:
    """人工校对 asr/source.srt 后重跑：ASR 复用缓存，但 merge/translate 必须重算。

    这是真实校对闭环（M7/CCTV 实测）的关键：缓存不能只看"文件存在"，
    否则改完字幕重跑永远拿到旧译文。
    """

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en"))
    assert calls.order[:2] == ["probe", "asr"]

    raw = out / "asr" / "source.srt"
    raw.write_text("1\n00:00:00,000 --> 00:00:02,000\n你好世界\n", encoding="utf-8")
    calls.order.clear()

    result = run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en"))

    assert result.status == "ok"
    assert "asr" not in calls.order  # ASR 重活不白跑
    assert "translate" in calls.order  # 但下游必须重算
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["asr"] == "cached"
    assert statuses["merge"] == "ok"
    assert statuses["translate"] == "ok"


def test_force_reruns_cached_stages(tmp_path, monkeypatch) -> None:
    src = make_src(tmp_path)
    out = tmp_path / "out"
    touch(out / "probe" / "manifest.json", json.dumps({"status": "extracted"}))
    touch(out / "probe" / "source.srt", "1\n00:00:00,000 --> 00:00:02,000\nHi\n")
    calls = Calls()
    install_fakes(monkeypatch, calls)

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", force=True))

    assert calls.order == ["probe", "translate", "build", "dub", "burn", "mark"]


def test_no_mark_skips_label_stage(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    result = run_pipeline(
        PipelineOptions(src=src, out=tmp_path / "out", src_lang="en", mark=False)
    )

    assert calls.order == ["probe", "translate", "build", "dub", "burn"]
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["mark"] == "skipped"
    assert "final_video" not in result.report["outputs"]


def test_keep_bgm_is_passed_through(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    run_pipeline(
        PipelineOptions(src=src, out=tmp_path / "out", src_lang="en", keep_bgm=True)
    )

    assert calls.options["dub"].keep_bgm is True  # type: ignore[union-attr]


def test_invalid_options_raise(tmp_path) -> None:
    src = make_src(tmp_path)
    with pytest.raises(PipelineError):
        run_pipeline(PipelineOptions(src=src, out=tmp_path / "o", src_lang="zh", tgt_lang="zh"))
    with pytest.raises(PipelineError):
        run_pipeline(PipelineOptions(src=src, out=tmp_path / "o", stop_after="magic"))


# ---------------------------------------------------------------- 烧字幕（M10）


def test_burn_stage_runs_by_default_and_mark_uses_burned_video(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))

    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["burn"]["status"] == "ok"
    assert stages["burn"]["summary"]["verified"] is True
    # 默认烧单语译文（发布字幕），mark 吃的是烧完字幕的视频
    assert calls.options["burn"].subtitle == out / "matrix" / "zh.srt"  # type: ignore[union-attr]
    assert calls.options["burn"].position == "bottom"  # type: ignore[union-attr]
    assert calls.options["mark"].src == out / "burn" / "burned.mp4"  # type: ignore[union-attr]
    assert result.report["outputs"]["burned_video"].endswith("burned.mp4")
    assert result.report["config"]["burn_subtitles"] is True
    assert result.report["config"]["burn_mode"] == "target"


def test_no_burn_keeps_dubbed_video(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="en", burn_subtitles=False)
    )

    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["burn"]["status"] == "skipped"
    assert "burned_video" not in result.report["outputs"]
    assert calls.options["mark"].src == out / "dub" / "dubbed.mp4"  # type: ignore[union-attr]
    assert "burn" not in calls.options


def test_bilingual_burn_uses_bilingual_ass(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="en", burn_mode="bilingual")
    )

    assert result.status == "ok"
    assert calls.options["burn"].subtitle == out / "matrix" / "bilingual.ass"  # type: ignore[union-attr]
    assert result.report["config"]["burn_mode"] == "bilingual"


def test_burn_verification_failure_is_partial(tmp_path, monkeypatch) -> None:
    """核验不过不能假装成功：阶段标 partial、退出码 2、告警写进报告。"""

    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    def bad_burn(options):
        out = Path(options.out)
        out.mkdir(parents=True, exist_ok=True)
        burned = touch(out / "burned.mp4", "burned")
        report_path = out / "burn.json"
        report_path.write_text(
            json.dumps(
                {
                    "input": {
                        "video": {"sha256": sha256(Path(options.video))},
                        "subtitle": {"sha256": sha256(Path(options.subtitle))},
                    },
                    "output": {"duration_delta_ms": 0},
                    "verification": {"ok": False, "bottom_pixel_diff": 0.0},
                    "warnings": ["字幕烧录核验未通过"],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            status="warn",
            exit_code=2,
            report=json.loads(report_path.read_text(encoding="utf-8")),
            report_path=report_path,
            video_path=burned,
            ass_path=Path(options.subtitle),
        )

    monkeypatch.setattr(pipeline_module, "run_burn", bad_burn)
    result = run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", src_lang="en"))

    assert result.status == "partial"
    assert result.exit_code == 2
    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["burn"]["status"] == "partial"
    assert any("烧录核验" in warning for warning in result.report["warnings"])


# ---------------------------------------------------------------- 多人声（M11）


def test_diarize_skipped_by_default(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)

    result = run_pipeline(PipelineOptions(src=src, out=tmp_path / "out", src_lang="en"))

    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["diarize"]["status"] == "skipped"
    assert "diarize" not in calls.order
    assert result.report["config"]["diarize"] is False
    assert "speakers_json" not in result.report["outputs"]
    assert calls.options["dub"].speakers_path is None  # type: ignore[union-attr]


def test_speaker_voices_enable_multi_voice_dubbing(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(
        monkeypatch,
        calls,
        route="route_to_asr",
        asr_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n为了生存，它们展开\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n一场盐的争夺战，真的很难。\n"
        ),
    )
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(
            src=src,
            out=out,
            tgt_lang="en",
            speaker_voices="en-US-GuyNeural,en-US-AriaNeural",
        )
    )

    assert calls.order == [
        "probe", "asr", "diarize", "translate", "build", "dub", "burn", "mark",
    ]
    assert calls.options["diarize"].language == "zh"  # type: ignore[union-attr]
    assert result.report["outputs"]["speakers_json"].endswith("speakers.merged.json")
    dub_options = calls.options["dub"]  # type: ignore[union-attr]
    assert dub_options.speakers_path == out / "diarize" / "speakers.merged.json"
    assert dict(dub_options.voice_by_speaker) == {
        "SPEAKER_00": "en-US-GuyNeural",
        "SPEAKER_01": "en-US-AriaNeural",
    }
    assert result.report["config"]["diarize"] is True
    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["diarize"]["summary"]["speakers"] == 2


def test_merge_does_not_join_different_speakers(tmp_path, monkeypatch) -> None:
    """两个人的相邻台词不能被合并成一条：否则一个音色会把两个人的话都读了。"""

    calls = Calls()
    install_fakes(
        monkeypatch,
        calls,
        route="route_to_asr",
        asr_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n为了生存，它们展开\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n一场盐的争夺战。\n"
        ),
    )
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(src=src, out=out, tgt_lang="en", speaker_voices="en-US-GuyNeural,en-US-AriaNeural")
    )

    merged = load_srt(out / "merge" / "merged.srt")[0]
    assert len(merged.cues) == 2  # 单人路线会把这两条并成一条
    assignments = json.loads(
        (out / "diarize" / "speakers.merged.json").read_text(encoding="utf-8")
    )
    assert [item["speaker"] for item in assignments["cues"]] == ["SPEAKER_00", "SPEAKER_01"]
    merge_stage = next(stage for stage in result.report["stages"] if stage["stage"] == "merge")
    assert merge_stage["summary"]["speakers"] == 2


def test_extracted_route_diarize_uses_probe_subtitle(tmp_path, monkeypatch) -> None:
    """内封字幕路线：不跑 ASR/merge，说话人分离直接作用在抽出的字幕上。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(
            src=src,
            out=out,
            src_lang="en",
            tgt_lang="zh",
            speaker_voices="zh-CN-YunxiNeural,zh-CN-XiaoxiaoNeural",
        )
    )

    assert result.status == "ok"
    assert calls.order[:2] == ["probe", "diarize"]
    assert "asr" not in calls.order
    assert calls.options["diarize"].srt == out / "probe" / "source.srt"  # type: ignore[union-attr]
    # merge 跳过 → 不需要重建映射，配音直接读 diarize/speakers.json
    assert calls.options["dub"].speakers_path == out / "diarize" / "speakers.json"  # type: ignore[union-attr]
    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["merge"]["status"] == "skipped"


def test_stop_after_merge_skips_speaker_remap(tmp_path, monkeypatch) -> None:
    """``--stop-after merge`` 停在"合并完成"，不该顺手把合并后的说话人映射也重建掉。

    重建映射是合并与翻译之间的**衔接钩子**，不是阶段：它跑不跑取决于流程走到哪，
    而不是 ``--stop-after`` 里那个名字。
    """

    calls = Calls()
    install_fakes(
        monkeypatch,
        calls,
        route="route_to_asr",
        asr_srt=(
            "1\n00:00:00,000 --> 00:00:02,000\n为了生存，它们展开\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n一场盐的争夺战。\n"
        ),
    )
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(
            src=make_src(tmp_path),
            out=out,
            tgt_lang="en",
            speaker_voices="en-US-GuyNeural,en-US-AriaNeural",
            stop_after="merge",
        )
    )

    assert calls.order == ["probe", "asr", "diarize"]
    assert not (out / "diarize" / "speakers.merged.json").exists()
    assert result.report["outputs"]["speakers_json"].endswith("speakers.json")


def test_diarize_and_dub_are_cached_on_rerun(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    options = PipelineOptions(
        src=src, out=out, tgt_lang="en", speaker_voices="en-US-GuyNeural,en-US-AriaNeural"
    )
    run_pipeline(options)
    calls.order.clear()

    result = run_pipeline(options)

    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["diarize"] == "cached"
    assert statuses["dub"] == "cached"
    assert "diarize" not in calls.order  # 说话人分离不重跑（真模型耗时）
    assert "dub" not in calls.order


def test_changing_voices_invalidates_dub_cache(tmp_path, monkeypatch) -> None:
    """换了音色必须重配：否则多人声里改 --speaker-voices 会继续用旧音色的音频。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    run_pipeline(
        PipelineOptions(
            src=src, out=out, tgt_lang="en", speaker_voices="en-US-GuyNeural,en-US-AriaNeural"
        )
    )
    calls.order.clear()

    result = run_pipeline(
        PipelineOptions(
            src=src, out=out, tgt_lang="en", speaker_voices="en-US-JennyNeural,en-US-AndrewNeural"
        )
    )

    assert "dub" in calls.order
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["dub"] == "ok"


def test_changing_tts_command_invalidates_dub_cache(tmp_path, monkeypatch) -> None:
    """Edge-TTS 换成云端复刻（--tts-command 变化）也必须重配。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    run_pipeline(
        PipelineOptions(
            src=src, out=out, tgt_lang="en", tts_command="ffmpeg -y {out}"
        )
    )
    calls.order.clear()

    run_pipeline(
        PipelineOptions(
            src=src,
            out=out,
            tgt_lang="en",
            tts_command="ffmpeg -y -f lavfi -i sine=frequency=440 {out}",
        )
    )

    assert "dub" in calls.order


def test_diarize_single_speaker_falls_back_to_single_voice(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", speaker_count=1)
    src = make_src(tmp_path)

    result = run_pipeline(
        PipelineOptions(
            src=src, out=tmp_path / "out", tgt_lang="en", speaker_voices="en-US-GuyNeural"
        )
    )

    assert calls.options["dub"].speakers_path is None  # type: ignore[union-attr]
    assert calls.options["dub"].voice_by_speaker == ()  # type: ignore[union-attr]
    assert any("1 个说话人" in warning for warning in result.report["warnings"])


def test_diarize_failure_falls_back_to_single_voice(tmp_path, monkeypatch) -> None:
    from cuesmith.diarize import DiarizeError

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")

    def boom(options):
        raise DiarizeError("模型文件不存在")

    monkeypatch.setattr(pipeline_module, "run_diarize", boom)
    src = make_src(tmp_path)

    result = run_pipeline(
        PipelineOptions(
            src=src, out=tmp_path / "out", tgt_lang="en", speaker_voices="en-US-GuyNeural"
        )
    )

    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["diarize"]["status"] == "failed"
    assert result.status == "partial"
    assert calls.options["dub"].speakers_path is None  # type: ignore[union-attr]
    assert any("说话人分离失败" in warning for warning in result.report["warnings"])


def test_cli_pipeline_speaker_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])

    assert args.diarize is False
    assert args.speakers == 0
    assert args.speaker_voices is None

    args = parser.parse_args(
        [
            "pipeline", "--src", "a.mp4", "--out", "out",
            "--diarize", "--speakers", "2", "--speaker-voices", "SPEAKER_00=a,SPEAKER_01=b",
        ]
    )
    assert args.diarize is True
    assert args.speakers == 2
    assert args.speaker_voices == "SPEAKER_00=a,SPEAKER_01=b"


def test_cli_pipeline_vad_switch() -> None:
    """OPT-6：VAD 预过滤默认开，可用 --no-vad 关掉；阈值可调。"""

    parser = build_parser()
    args = parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])
    assert args.vad is True
    assert args.vad_threshold == 0.5
    assert args.vad_model is None

    args = parser.parse_args(
        ["pipeline", "--src", "a.mp4", "--out", "out", "--no-vad", "--vad-threshold", "0.6"]
    )
    assert args.vad is False
    assert args.vad_threshold == 0.6


def test_pipeline_passes_vad_options_to_diarize(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")

    run_pipeline(
        PipelineOptions(
            src=make_src(tmp_path),
            out=tmp_path / "out",
            tgt_lang="en",
            speaker_voices="en-US-GuyNeural",
            vad=False,
            vad_threshold=0.62,
        )
    )

    options = calls.options["diarize"]
    assert options.vad is False  # type: ignore[union-attr]
    assert options.vad_threshold == pytest.approx(0.62)  # type: ignore[union-attr]


def test_diarize_cache_invalidates_vad_options(tmp_path: Path) -> None:
    """OPT-6：VAD 开关/阈值变了分段会不同，必须重跑；老缓存（无 vad 字段）仍可复用。"""

    src = tmp_path / "a.mp4"
    out = tmp_path / "out"

    def opts(**kwargs):
        return PipelineOptions(src=src, out=out, **kwargs)

    new_report = {
        "config": {
            "num_speakers_requested": None,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
            "vad": {"requested": True, "enabled": True, "threshold": 0.5},
            "clustering": "auto",
            "clustering_effective": "embedding",
            "merge_alpha": 0.70,
        }
    }
    assert pipeline_module._diarize_config_matches(new_report, opts(), "en")
    assert not pipeline_module._diarize_config_matches(new_report, opts(vad=False), "en")
    assert not pipeline_module._diarize_config_matches(
        new_report, opts(vad_threshold=0.65), "en"
    )

    legacy_vad = {
        "config": {
            "num_speakers_requested": None,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
            "clustering_effective": "sherpa",
            "merge_alpha": 0.55,
        }
    }
    # OPT-6 之前的旧产物没有 vad 字段：不因为新增默认值而强制重跑
    assert pipeline_module._diarize_config_matches(legacy_vad, opts(clustering="sherpa"), "en")
    # 显式 alpha 与记录一致时也可复用
    assert pipeline_module._diarize_config_matches(
        legacy_vad, opts(clustering="sherpa", merge_alpha=0.55), "en"
    )
    assert not pipeline_module._diarize_config_matches(
        legacy_vad, opts(clustering="sherpa", merge_alpha=0.6), "en"
    )


def test_diarize_cache_invalidates_clustering_options(tmp_path: Path) -> None:
    """OPT-7：二次聚类方式/阈值变了人数会变，必须重跑；OPT-7 之前的老缓存应重跑。"""

    src = tmp_path / "a.mp4"
    out = tmp_path / "out"

    def opts(**kwargs):
        return PipelineOptions(src=src, out=out, **kwargs)

    report = {
        "config": {
            "num_speakers_requested": None,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
            "clustering": "auto",
            "clustering_effective": "embedding",
            "merge_alpha": 0.55,
        }
    }
    assert pipeline_module._diarize_config_matches(report, opts(), "en")
    # 关掉二次聚类 / 改阈值 → 重跑
    assert not pipeline_module._diarize_config_matches(report, opts(clustering="sherpa"), "en")
    assert not pipeline_module._diarize_config_matches(report, opts(merge_alpha=0.5), "en")
    # 显式人数时 auto 走 sherpa：与记录一致才复用
    explicit = {
        "config": dict(report["config"], num_speakers_requested=3, clustering_effective="sherpa")
    }
    assert pipeline_module._diarize_config_matches(explicit, opts(speakers=3), "en")
    assert not pipeline_module._diarize_config_matches(explicit, opts(), "en")

    # OPT-7 之前的老缓存没有 clustering 字段 → 自动判人数时应重跑（要拿到二次聚类的收益）
    legacy = {
        "config": {
            "num_speakers_requested": None,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
        }
    }
    assert not pipeline_module._diarize_config_matches(legacy, opts(), "en")


# ---------------------------------------------------------------- CLI


def test_cli_pipeline_defaults(tmp_path) -> None:
    parser = build_parser()
    args = parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])

    assert args.src_lang == "auto"
    assert args.tgt_lang == "zh"
    assert args.llm is False
    assert args.mark is True
    assert args.condense is True
    assert args.merge is True
    assert args.force is False
    assert args.burn_subtitles is True
    assert args.burn_mode == "target"


def test_cli_pipeline_can_disable_merge() -> None:
    parser = build_parser()

    assert parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"]).merge is True
    assert (
        parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out", "--no-merge"]).merge
        is False
    )


def test_every_pipeline_flag_lands_on_pipeline_options() -> None:
    """CLI 新加一个 pipeline 参数，必须能在 ``PipelineOptions`` 上找到落点。

    ``_pipeline_options()`` 用 ``**vars(args)`` 透传，所以"参数加了但没生效"现在只剩
    一种可能：CLI 有、Options 没有。下面三个是 CLI 专属（在 ``_pipeline_options`` 里被
    展开成别的字段），其余每个 flag 都必须同名落到 dataclass 上。
    """

    args = build_parser().parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])
    cli_only = {"command", "glossary_reverse", "hotwords_file"}
    fields = {field.name for field in dataclasses.fields(PipelineOptions)}

    orphans = sorted(set(vars(args)) - cli_only - fields)
    assert not orphans, f"这些 CLI 参数没有落到 PipelineOptions：{orphans}"


def test_pipeline_options_expands_cli_transforms(tmp_path) -> None:
    """``_pipeline_options`` 的三处展开：术语表、热词、价表路径。"""

    glossary = tmp_path / "g.txt"
    glossary.write_text("Blender\n", encoding="utf-8")
    hotwords = tmp_path / "h.txt"
    hotwords.write_text("曹操\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "pipeline", "--src", "a.mp4", "--out", "out",
            "--glossary", str(glossary), "--glossary-reverse",
            "--hotwords", " 刘备 , 曹操 ,,",
            "--hotwords-file", str(hotwords),
            "--pricing", "price.json",
        ]
    )

    options = cli_module._pipeline_options(args)

    # 术语表：路径 → 对象；--glossary-reverse 把"英文原词→中文"翻成"中文→英文"
    assert options.glossary is not None
    assert [term.source for term in options.glossary] == ["Blender"]
    # 热词：逗号串 + 文件按顺序合并（去重发生在 asr 阶段，这里保持原样）
    assert options.hotwords == ("刘备", "曹操", "曹操")
    # 价表：Path → str（报告要能 JSON 化）
    assert options.pricing == "price.json"
    assert isinstance(options.pricing, str)
    # CLI 没有 --llm-review 开关：--llm 默认双轮审校
    assert options.llm_review is True


def test_cli_burn_command(tmp_path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["burn", "--video", "in.mp4", "--subtitle", "en.srt", "--out", "out"]
    )

    assert args.crf == 18
    assert args.preset == "veryfast"
    assert args.verify is True
    assert args.position == "bottom"
    args = parser.parse_args(
        [
            "burn", "--video", "i.mp4", "--subtitle", "a.ass", "--out", "o",
            "--no-verify", "--position", "top",
        ]
    )
    assert args.verify is False
    assert args.position == "top"


def test_cli_pipeline_burn_flags(tmp_path) -> None:
    parser = build_parser()

    args = parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])
    assert args.burn_subtitles is True and args.burn_mode == "target"
    assert args.burn_position == "bottom"
    args = parser.parse_args(
        [
            "pipeline", "--src", "a.mp4", "--out", "out",
            "--no-burn-subtitles", "--burn-mode", "bilingual", "--burn-crf", "20",
            "--burn-position", "top",
        ]
    )
    assert args.burn_subtitles is False
    assert args.burn_mode == "bilingual"
    assert args.burn_crf == 20
    assert args.burn_position == "top"


def test_cli_pipeline_llm_and_keep_bgm(tmp_path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline", "--src", "a.mp4", "--out", "out", "--src-lang", "en",
            "--llm", "--keep-bgm", "--no-mark", "--force", "--stop-after", "build",
        ]
    )

    assert args.llm is True
    assert args.keep_bgm is True
    assert args.mark is False
    assert args.force is True
    assert args.stop_after == "build"


# ---------------------------------------------------------------- 模型默认（A 项）


def test_model_variant_detects_small_medium() -> None:
    assert model_variant("spike/whisper-models/faster-whisper-medium") == "medium"
    assert model_variant("C:/x/models--Systran--faster-whisper-small") == "small"
    assert model_variant("somewhere/custom-model") == "custom"


def test_preferred_model_returns_medium_for_chinese(monkeypatch, tmp_path) -> None:
    medium = tmp_path / "faster-whisper-medium"
    medium.mkdir()
    (medium / "model.bin").write_bytes(b"weights")
    monkeypatch.setattr(
        "cuesmith.asr.local_models",
        lambda: {"small": tmp_path / "faster-whisper-small", "medium": medium},
    )

    assert preferred_model("zh") == "medium"
    assert preferred_model("en") is None


def test_preferred_model_falls_back_when_medium_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "cuesmith.asr.local_models",
        lambda: {"small": tmp_path / "faster-whisper-small", "medium": tmp_path / "nope"},
    )

    assert preferred_model("zh") is None


def test_asr_uses_medium_for_chinese_by_default(tmp_path, monkeypatch) -> None:
    """中文源语默认 medium：small 会把「刘翔」识别成「流向」（M7 实测）。"""

    from cuesmith import asr as asr_module

    monkeypatch.setattr(asr_module, "preferred_model", lambda language: "medium")
    captured: dict[str, object] = {}

    def fake_find_model(explicit):
        captured["model"] = explicit
        return "resolved-medium"

    monkeypatch.setattr(asr_module, "find_model", fake_find_model)
    monkeypatch.setattr(asr_module, "find_python", lambda explicit, runner=None: "fake-python")
    source = tmp_path / "in.mp4"
    source.write_bytes(b"x")
    options = asr_module.AsrOptions(
        src=source,
        out=tmp_path / "o",
        language="zh",
        runner=lambda command, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="boom"
        ),
    )

    with pytest.raises(asr_module.AsrError):
        asr_module.run_asr(options)

    assert captured["model"] == "medium"


def test_asr_model_change_invalidates_asr_cache(tmp_path, monkeypatch) -> None:
    """ASR 模型从 medium 改成 small 后，不能复用旧的 medium 结果。

    两次都显式指定模型：不显式时默认模型取决于"本机装了哪个模型"，CI 上没有模型就会
    回落到 small，与显式 small 相同 → 缓存命中、asr 根本没跑（这条在 CI 上红过）。
    """

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", asr_model="medium"))
    calls.order.clear()

    result = run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", asr_model="small"))

    assert "asr" in calls.order
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["asr"] == "ok"


def test_partial_dub_is_not_reused_as_cache(tmp_path, monkeypatch) -> None:
    """上次配音全失败（partial）时重跑必须重新合成，不能把 0/15 当缓存。"""

    calls = Calls()
    install_fakes(monkeypatch, calls)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", tgt_lang="zh"))
    report_path = out / "dub" / "dub_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["status"] = "partial"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    calls.order.clear()

    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", tgt_lang="zh"))

    assert "dub" in calls.order
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["dub"] == "ok"


def test_music_noise_cue_filtered_before_translate(tmp_path, monkeypatch) -> None:
    """ASR 把音乐听成「咪咪咪…」时，翻译前必须过滤，不能把 LLM 带崩。"""

    calls = Calls()
    asr_srt = (
        "1\n00:00:00,000 --> 00:00:02,000\n盐也会引发战争吗\n\n"
        "2\n00:00:05,000 --> 00:00:07,000\n咪咪咪咪咪咪咪咪咪咪\n\n"
        "3\n00:00:09,000 --> 00:00:11,000\n你知道是什么吸引它们吗\n"
    )
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh", asr_srt=asr_srt)
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en"))

    assert result.status == "ok"
    translate_options = calls.options["translate"]
    assert Path(translate_options.src) == out / "merge" / "clean.srt"  # type: ignore[union-attr]
    assert any("音乐/噪声" in warning for warning in result.report["warnings"])
    assert result.report["outputs"]["clean_srt"].endswith("clean.srt")
    merge_stage = next(stage for stage in result.report["stages"] if stage["stage"] == "merge")
    assert merge_stage["summary"]["degenerate_dropped"] == 1  # type: ignore[index]

def test_default_speaker_voices_are_live_and_distinct() -> None:
    """回归：默认音色池不能含已下线的 voice（M16 真实测试：Davis 已 NoAudioReceived）。"""

    from cuesmith.pipeline import DEFAULT_SPEAKER_VOICES

    for language, pool in DEFAULT_SPEAKER_VOICES.items():
        assert len(pool) >= 4, language
        assert len(set(pool)) == len(pool), language
    assert "en-US-DavisNeural" not in DEFAULT_SPEAKER_VOICES["en"]
    assert "en-US-AndrewNeural" in DEFAULT_SPEAKER_VOICES["en"]


def test_diarize_cache_invalidates_count_threshold_and_language(tmp_path: Path) -> None:
    """回归：改 --speakers / 阈值 / 语言后不能复用旧的 diarize 结果（M16 真实测试踩到）。"""

    src = tmp_path / "a.mp4"
    out = tmp_path / "out"
    # 英文默认阈值是 0.95（OPT-1），报告里记的就是实际生效值
    report = {
        "config": {
            "num_speakers_requested": 3,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
            "clustering_effective": "sherpa",
            "merge_alpha": 0.55,
        }
    }

    def opts(**kwargs):
        return PipelineOptions(src=src, out=out, **kwargs)

    assert pipeline_module._diarize_config_matches(report, opts(speakers=3), "en")
    assert not pipeline_module._diarize_config_matches(report, opts(speakers=15), "en")
    assert not pipeline_module._diarize_config_matches(
        report, opts(speakers=3, diarization_threshold=0.9), "en"
    )
    assert not pipeline_module._diarize_config_matches(report, opts(speakers=3), "zh")
    # 关掉短簇合并也必须重跑（人数/音色分派会变）
    assert not pipeline_module._diarize_config_matches(
        report, opts(speakers=3, keep_minor_speakers=True), "en"
    )

    auto = {
        "config": {
            "num_speakers_requested": None,
            "threshold": 0.95,
            "language": "en",
            "min_speaker_speech_s": 3.0,
            "clustering_effective": "embedding",
            "merge_alpha": 0.55,
        }
    }
    assert pipeline_module._diarize_config_matches(auto, opts(), "en")
    assert pipeline_module._diarize_config_matches(auto, opts(keep_minor_speakers=False), "en")
    assert not pipeline_module._diarize_config_matches(auto, opts(speakers=3), "en")


def test_language_default_cluster_threshold(tmp_path: Path) -> None:
    """OPT-1：英文默认阈值 0.95（0.8 会把播客笑声/广告拆成 15 个簇），中文保持 0.8。"""

    from cuesmith.diarize import default_threshold

    assert default_threshold("en") == pytest.approx(0.95)
    assert default_threshold("EN") == pytest.approx(0.95)
    assert default_threshold("zh") == pytest.approx(0.8)
    assert default_threshold(None) == pytest.approx(0.8)
    assert default_threshold("ja") == pytest.approx(0.8)

    src = tmp_path / "a.mp4"
    out = tmp_path / "out"
    english = pipeline_module._diarize_threshold(PipelineOptions(src=src, out=out), "en")
    chinese = pipeline_module._diarize_threshold(PipelineOptions(src=src, out=out), "zh")
    assert english == pytest.approx(0.95)
    assert chinese == pytest.approx(0.8)
    # 显式阈值优先于语言默认
    explicit = PipelineOptions(src=src, out=out, diarization_threshold=0.7)
    assert pipeline_module._diarize_threshold(explicit, "en") == pytest.approx(0.7)


def test_asr_and_diarize_segments_run_in_parallel(tmp_path, monkeypatch) -> None:
    """OPT-5：源语言已知时，diarize 的分段与 ASR 并行跑，ASR 出来后再补字幕归属。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    result = run_pipeline(
        PipelineOptions(
            src=src,
            out=out,
            src_lang="zh",
            tgt_lang="en",
            speaker_voices="en-US-GuyNeural,en-US-AriaNeural",
        )
    )

    assert result.status == "ok"
    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["diarize"]["status"] == "ok"
    assert stages["diarize"]["summary"]["parallel_with_asr"] is True
    # 分段阶段不传字幕（并行预取），归属在 ASR 之后补
    assert calls.options["diarize"].srt is None
    assert (out / "diarize" / "segments.json").is_file()
    speakers = json.loads((out / "diarize" / "speakers.json").read_text(encoding="utf-8"))
    assert len(speakers["speakers"]) >= 2
    assert len(speakers["cues"]) >= 1
    # 归属写进 diarize.json（缓存判据依赖它）
    report = json.loads((out / "diarize" / "diarize.json").read_text(encoding="utf-8"))
    assert report["input"]["srt"]["sha256"]
    assert report["summary"]["cues"] == len(speakers["cues"])
    assert report["summary"]["assigned_cues"] == len(speakers["cues"])


def test_parallel_diarize_reuses_segments_file(tmp_path, monkeypatch) -> None:
    """分段只跟音频/聚类参数有关：重跑时能直接复用，不再跑一遍分离模型。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    options = PipelineOptions(
        src=src,
        out=out,
        src_lang="zh",
        tgt_lang="en",
        speaker_voices="en-US-GuyNeural,en-US-AriaNeural",
    )
    run_pipeline(options)
    assert (out / "diarize" / "segments.json").is_file()

    # 删掉归属产物，只留 segments.json → 重跑应复用分段、不再调用 diarize（模型）
    (out / "diarize" / "diarize.json").unlink()
    (out / "diarize" / "speakers.json").unlink()
    calls.order.clear()

    result = run_pipeline(options)

    stages = {stage["stage"]: stage for stage in result.report["stages"]}
    assert stages["diarize"]["status"] == "ok"
    assert "diarize" not in calls.order  # 没有重新跑分离
    assert (out / "diarize" / "speakers.json").is_file()


# ---------------------------------------------------------------- 本地 TTS / 兜底（OPT-11）


def test_cli_pipeline_local_tts_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "pipeline", "--src", "a.mp4", "--out", "out",
            "--tts-provider", "local", "--tts-fallback", "off",
            "--local-tts-voice", "47", "--local-tts-kind", "kokoro",
            "--local-tts-speed", "1.2", "--local-tts-threads", "4",
        ]
    )

    assert args.tts_provider == "local"
    assert args.tts_fallback == "off"
    assert args.local_tts_voice == "47"
    assert args.local_tts_kind == "kokoro"
    assert args.local_tts_speed == 1.2
    assert args.local_tts_threads == 4
    # 默认仍是 Edge + 本地兜底
    defaults = parser.parse_args(["pipeline", "--src", "a.mp4", "--out", "out"])
    assert defaults.tts_provider == "edge" and defaults.tts_fallback == "local"


def test_pipeline_passes_local_tts_options_to_dub(tmp_path, monkeypatch) -> None:
    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")

    run_pipeline(
        PipelineOptions(
            src=make_src(tmp_path),
            out=tmp_path / "out",
            tgt_lang="en",
            speaker_voices="en-US-GuyNeural",
            tts_provider="local",
            tts_fallback="off",
            local_tts_voice="47",
            local_tts_kind="kokoro",
            local_tts_threads=4,
            local_tts_speed=1.2,
        )
    )

    dub_options = calls.options["dub"]
    assert dub_options.tts_provider == "local"  # type: ignore[union-attr]
    assert dub_options.tts_fallback == "off"  # type: ignore[union-attr]
    assert dub_options.local_tts_voice == "47"  # type: ignore[union-attr]
    assert dub_options.local_tts_kind == "kokoro"  # type: ignore[union-attr]
    assert dub_options.local_tts_threads == 4  # type: ignore[union-attr]
    assert dub_options.local_tts_speed == 1.2  # type: ignore[union-attr]
    assert dub_options.tgt_lang == "en"  # type: ignore[union-attr]


def test_pipeline_rejects_conflicting_local_tts(tmp_path) -> None:
    with pytest.raises(Exception):
        PipelineOptions(
            src=tmp_path / "a.mp4",
            out=tmp_path / "out",
            tts_provider="local",
            tts_command="ffmpeg {out}",
        ).validate()
    with pytest.raises(Exception):
        PipelineOptions(
            src=tmp_path / "a.mp4", out=tmp_path / "out", tts_provider="magic"
        ).validate()


def test_dub_cache_invalidates_tts_provider_and_local_model(tmp_path: Path) -> None:
    def matches(report, **overrides):
        base = dict(
            voice="v",
            rate=None,
            voice_map={},
            tts_command=None,
            tts_provider="local",
            tts_fallback="off",
            local_fingerprint="kokoro-multi-lang-v1_0:kokoro::zh",
        )
        base.update(overrides)
        return pipeline_module._dub_tts_cache_matches(report, **base)

    report = {
        "config": {
            "voice": "v",
            "tts_rate": None,
            "voice_by_speaker": {},
            "tempo_limit": 1.4,
            "adaptive_rate": True,
            "tts_command_sha256": None,
            "tts_provider": "local",
            "tts_fallback": "off",
            "local_tts": {"fingerprint": "kokoro-multi-lang-v1_0:kokoro::zh"},
        }
    }

    assert matches(report)
    assert not matches(report, tts_provider="edge")
    assert not matches(report, local_fingerprint="melo:vits::zh")
    assert not matches(report, tts_fallback="local")
    # 老报告（OPT-11 之前，没有 tts_provider/local_tts）仍按 Edge 缓存复用
    legacy = {
        "config": {
            "voice": "v",
            "tts_rate": None,
            "voice_by_speaker": {},
            "tempo_limit": 1.4,
            "adaptive_rate": True,
            "tts_command_sha256": None,
        }
    }
    assert pipeline_module._dub_tts_cache_matches(
        legacy, voice="v", rate=None, voice_map={}, tts_command=None
    )


# ---------------------------------------------------------------- 缓存判据（P0）
# 背景：阶段缓存以前只看"产物文件是否存在"或只看输入 sha，改了参数照样复用旧产物。
# 下面这些用例钉住的是"任何会改变产物的参数都必须进判据"，每一个都对应一个真实坑。


def test_config_matches_requires_every_field_and_ignores_order() -> None:
    """判据比较规则：缺字段算不一致、序列无序、数字按容差。"""

    matches = pipeline_module._config_matches
    report = {"config": {"voice": "a", "hotwords": ["刘备", "曹操"], "budget": 9.568}}

    assert matches(report, {"config": {"voice": "a"}})
    # 热词顺序变化不该触发重跑
    assert matches(report, {"config": {"hotwords": ["曹操", "刘备"]}})
    # 报告里缺字段＝不一致：老报告没有这个字段时宁可重跑一次
    assert not matches(report, {"config": {"model": "medium"}})
    # 数字按容差
    assert matches(report, {"config": {"budget": 9.57}}, tolerance=0.01)
    assert not matches(report, {"config": {"budget": 9.70}}, tolerance=0.01)
    # 类型不对也算不一致
    assert not matches(report, {"config": {"voice": ["a"]}})
    # 18 与 "18" 不是同一个值（否则类型漂移会被静默接受）
    assert not matches({"k": 18}, {"k": "18"})
    assert not matches({"k": "18"}, {"k": 18})
    # NaN 永不相等
    assert not matches({"k": float("nan")}, {"k": 5.0})


def test_new_source_reruns_probe_despite_existing_manifest(tmp_path, monkeypatch) -> None:
    """换一个视频复用同一个 ``--out``：绝不能拿上一条视频的 manifest 当缓存。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=make_src(tmp_path, "a.mp4"), out=out, src_lang="en"))
    calls.order.clear()

    second = tmp_path / "b.mp4"
    second.write_bytes(b"another video bytes")
    result = run_pipeline(PipelineOptions(src=second, out=out, src_lang="en"))

    assert "probe" in calls.order
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["probe"] == "ok"


def test_probe_cache_invalidated_by_src_lang(tmp_path, monkeypatch) -> None:
    """源语言变了选路结论就可能变（内封字幕能否直出），probe 必须重跑。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", tgt_lang="zh"))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="zh", tgt_lang="en"))

    assert "probe" in calls.order


def test_force_reruns_probe_even_when_manifest_matches(tmp_path, monkeypatch) -> None:
    """``--force`` 必须压过缓存判据：manifest 完全对得上也要重跑。

    判据只看"报告里记的输入/配置"，很容易写成"命中就复用"而漏掉 ``--force``——
    用户明确要求重跑却被缓存挡住，是比"该复用却重跑"更难受的 bug。
    """

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))
    assert "probe" not in calls.order  # 判据全中 → 复用

    calls.order.clear()
    result = run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", force=True))

    assert calls.order[0] == "probe"
    statuses = {stage["stage"]: stage["status"] for stage in result.report["stages"]}
    assert statuses["probe"] == "ok"


def test_asr_cache_invalidated_by_new_source(tmp_path, monkeypatch) -> None:
    """换视频复用同一个 ``--out``：ASR 也不能拿上一条视频的识别结果。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    out = tmp_path / "out"

    run_pipeline(
        PipelineOptions(src=make_src(tmp_path, "a.mp4"), out=out, src_lang="zh", tgt_lang="en")
    )
    calls.order.clear()

    second = tmp_path / "b.mp4"
    second.write_bytes(b"another video bytes")
    run_pipeline(PipelineOptions(src=second, out=out, src_lang="zh", tgt_lang="en"))

    assert "asr" in calls.order


def test_dropping_asr_model_flag_invalidates_when_default_differs(
    tmp_path, monkeypatch
) -> None:
    """去掉 ``--asr-model`` 后默认模型可能是另一个：不能沿用旧的 small 结果。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr", language="zh")
    # 模拟"本机装了 medium"：这种情况下中文的自动默认就是 medium
    monkeypatch.setattr(pipeline_module, "preferred_model", lambda language: "medium")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="zh", tgt_lang="en", asr_model="small")
    )
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="zh", tgt_lang="en"))

    assert "asr" in calls.order


def test_asr_cache_invalidated_by_hotwords(tmp_path, monkeypatch) -> None:
    """热词变了识别结果就会变；同样的热词必须能命中缓存。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", hotwords=("刘备",)))

    calls.order.clear()
    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", hotwords=("刘备",)))
    assert "asr" not in calls.order  # 选项没变 → 复用

    calls.order.clear()
    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", hotwords=("曹操",)))
    assert "asr" in calls.order  # 选项变了 → 重跑


def test_translate_cache_invalidated_by_glossary_content(tmp_path, monkeypatch) -> None:
    """同一个术语表改了译法（条数不变）必须重翻，否则术语表等于没生效。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    before = Glossary.from_terms([Term(source="OpenAI", target="开放AI")])
    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", glossary=before))
    calls.order.clear()

    after = Glossary.from_terms([Term(source="OpenAI", target="Open AI")])
    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", glossary=after))

    assert "translate" in calls.order


def test_translate_cache_invalidated_by_llm_model(tmp_path, monkeypatch) -> None:
    """换 LLM 模型会换译文：``--llm-model`` 变了必须重翻。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    first = LlmConfig(api_key="k", model="deepseek-chat")
    run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="en", llm=True, llm_config=first)
    )
    calls.order.clear()

    second = LlmConfig(api_key="k", model="gpt-4o-mini")
    run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="en", llm=True, llm_config=second)
    )

    assert "translate" in calls.order


def test_translate_cache_invalidated_by_provider(tmp_path, monkeypatch) -> None:
    """换翻译渠道必须重翻：免费 MT 和 LLM 的译文不一样。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en"))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", provider="echo"))

    assert "translate" in calls.order


def test_merge_cache_invalidated_by_window(tmp_path, monkeypatch) -> None:
    """合并窗口变了，合并出来的字幕就不是同一份。"""

    def merge_status(result) -> str:
        return {stage["stage"]: stage["status"] for stage in result.report["stages"]}[
            "merge"
        ]

    calls = Calls()
    install_fakes(monkeypatch, calls, route="route_to_asr")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, tgt_lang="en", merge_max_duration=15.0))
    # 对照：参数不变时必须命中缓存（run_merge 不经过 calls，只能看阶段状态）
    same = run_pipeline(
        PipelineOptions(src=src, out=out, tgt_lang="en", merge_max_duration=15.0)
    )
    assert merge_status(same) == "cached"

    changed = run_pipeline(
        PipelineOptions(src=src, out=out, tgt_lang="en", merge_max_duration=10.0)
    )
    assert merge_status(changed) == "ok"  # 重跑


def test_build_cache_invalidated_by_wrap(tmp_path, monkeypatch) -> None:
    """关掉排版会改折行/切条，发布字幕必须重出。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", wrap=True))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", wrap=False))

    assert "build" in calls.order


def test_burn_cache_invalidated_by_crf(tmp_path, monkeypatch) -> None:
    """码率变了成片质量就变了，不能拿旧 burned.mp4 当缓存。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", burn_crf=18))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", burn_crf=24))

    assert "burn" in calls.order


def test_dub_cache_invalidated_by_keep_bgm(tmp_path, monkeypatch) -> None:
    """勾上/取消保留 BGM 会改混音产物；沿用旧配音就是"勾了没生效"。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", keep_bgm=False))
    calls.order.clear()

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", keep_bgm=True))

    assert "dub" in calls.order


def test_dub_cache_invalidated_by_bgm_source(tmp_path, monkeypatch) -> None:
    """换了 BGM 取音乐轨，混出来的就是另一份音频。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"
    other_bgm = tmp_path / "other.mp3"
    other_bgm.write_bytes(b"bgm audio")

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", keep_bgm=True))
    calls.order.clear()

    run_pipeline(
        PipelineOptions(
            src=src, out=out, src_lang="en", keep_bgm=True, bgm_source=other_bgm
        )
    )

    assert "dub" in calls.order


def test_dub_cache_invalidated_by_bgm_mode(tmp_path, monkeypatch) -> None:
    """BGM 模式变了（auto/separate/center/duck），混音结果就不同。"""

    calls = Calls()
    install_fakes(monkeypatch, calls, route="extracted")
    src = make_src(tmp_path)
    out = tmp_path / "out"

    run_pipeline(PipelineOptions(src=src, out=out, src_lang="en", keep_bgm=True))
    calls.order.clear()

    run_pipeline(
        PipelineOptions(src=src, out=out, src_lang="en", keep_bgm=True, bgm_mode="duck")
    )

    assert "dub" in calls.order
