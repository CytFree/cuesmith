"""全链路样本回归（可复跑，PRD §4「可复跑」）。

跑什么：
1. **路由回归**：每个样本跑 `probe`，与期望结论比对（AC-2 / AC-3），含真实 PGS / VobSub 图形字幕
2. **句级合并回归**：真实 ASR 碎片 10 条 → 句级 6 条（AC-13，对照人工句级标注）
3. **全链路联调**：merge → build → dub → mark 串起来跑一遍真实素材，
   逐段核对报告状态（AC-4/6/8/9/10/11/14）
4. **翻译层回归**（离线，`--provider echo`）：中文 ASR 碎片 → 术语表占位 → 段落上下文 → 报告，
   再串 `build --wrap` 验证排版；词级切条用合成词时间戳验证切句/停顿规则；
   默认精简（condense auto）单列一条检查
5. **BGM 保留回归**：相位抵消兜底（不依赖模型） → 侧链混音 → 并进成片

配音用**占位引擎**（ffmpeg 生成 1.5 秒正弦）：回归要的是流水线机制可复跑，
不依赖网络与音色质量——真引擎的音质/延迟单独在 M4 实现说明里报。

用法：
    python tools/run_samples.py [--out work/regression]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CLI = [sys.executable, "-m", "submatrix"]
# 占位 TTS：1.5 秒正弦，只验流水线
STUB_TTS = 'ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=1.5" {out}'

# (样本 id, 视频, 期望路由结论)
ROUTING_SAMPLES = [
    ("S1-内封SRT", "spike/samples/S1_softsub.mkv", "extracted"),
    ("S2-硬字幕", "spike/samples/S2_hardsub.mp4", "route_to_asr"),
    ("S3-无字幕", "spike/samples/S3_no_sub.mp4", "route_to_asr"),
    ("vtv-底座成品", "spike/pvt-out-vtv/S3_no_sub.mp4", "route_to_asr"),
    ("PGS-纯图形", "work/m2-demo/pgs_only.mkv", "unsupported"),
    ("VobSub-纯图形", "work/m2-demo/dvd_only.mkv", "unsupported"),
    ("PGS+文本并存", "work/m2-demo/pgs_sample.mkv", "extracted"),
    ("竖屏9:16", "work/samples-v2/portrait.mkv", "extracted"),
    ("静音无音轨", "work/samples-v2/silent.mp4", "route_to_asr"),
    ("1小时长视频", "work/samples-v2/long1h.mkv", "extracted"),
    ("真实采访素材", "work/real-samples/real_olympic.webm", "route_to_asr"),
]

CHAIN = {
    "id": "chain-真机成品",
    "video": "spike/pvt-out-vtv/S3_no_sub.mp4",
    "zh": "spike/pvt-out-vtv/zh-cn.srt",
    "en": "spike/pvt-out-vtv/en.srt",
}

MERGE_SAMPLE = ("spike/samples/out_asr.srt", 10, 6)  # 碎片 10 条 → 句级 6 条（人工标注 6 句）


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*CLI, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(text: str) -> str:
    """样本 id 转成合法目录名（Windows 不允许 `:` 等字符）。"""

    for char in ':*?"<>|/\\':
        text = text.replace(char, "-")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="submatrix 全链路样本回归")
    parser.add_argument("--out", type=Path, default=Path("work/regression"))
    parser.add_argument(
        "--real-tts-python",
        default=None,
        help="给了就跑一次真 Edge-TTS 配音（联网，约 10–30s），用于记录真引擎数据",
    )
    args = parser.parse_args(argv)

    out_root: Path = (ROOT / args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: object) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}：{detail}")

    # 1) 路由回归（AC-2 / AC-3）
    for sample_id, video, expected in ROUTING_SAMPLES:
        video_path = ROOT / video
        if not video_path.is_file():
            record(f"路由/{sample_id}", False, f"样本缺失：{video}")
            continue
        sample_out = out_root / "routing" / slug(sample_id)
        completed = run("probe", "--src", str(video_path), "--out", str(sample_out))
        manifest_path = sample_out / "manifest.json"
        if not manifest_path.is_file():
            record(f"路由/{sample_id}", False, f"未产出 manifest（退出码 {completed.returncode}）")
            continue
        manifest = read_json(manifest_path)
        actual = manifest["status"]
        expected_exit = {"extracted": 0, "extract": 0, "route_to_asr": 0, "unsupported": 2}[actual]
        ok = actual == expected and completed.returncode == expected_exit
        record(f"路由/{sample_id}", ok, f"{actual}（期望 {expected}，退出码 {completed.returncode}）")

    # 2) 句级合并（AC-13）
    merge_src, before_expect, after_expect = MERGE_SAMPLE
    merge_src_path = ROOT / merge_src
    if merge_src_path.is_file():
        merge_out = out_root / "merge"
        completed = run("merge", "--src", str(merge_src_path), "--out", str(merge_out))
        if completed.returncode == 0 and (merge_out / "merge.json").is_file():
            summary = read_json(merge_out / "merge.json")["summary"]
            ok = summary["before"] == before_expect and summary["after"] == after_expect
            record(
                "句级合并/ASR碎片",
                ok,
                f"{summary['before']} → {summary['after']} 条（期望 {before_expect} → {after_expect}）",
            )
        else:
            record("句级合并/ASR碎片", False, f"退出码 {completed.returncode}")
    else:
        record("句级合并/ASR碎片", False, f"样本缺失：{merge_src}")

    # 3) 长视频 + 竖屏边界
    long_zh = ROOT / "work/samples-v2/long_zh.srt"
    long_en = ROOT / "work/samples-v2/long_en.srt"
    long_extracted = out_root / "routing" / slug("1小时长视频") / "source.srt"
    if long_zh.is_file() and long_extracted.is_file():
        from submatrix.srt import load_srt

        expected_sub, _ = load_srt(long_zh)
        actual_sub, _ = load_srt(long_extracted)
        record(
            "长视频/AC-1 逐条一致",
            expected_sub == actual_sub,
            f"抽出 {len(actual_sub)} 条，与源字幕一致={expected_sub == actual_sub}",
        )
    if long_zh.is_file() and long_en.is_file():
        long_out = out_root / "long"
        started_long = time.monotonic()
        completed = run("build", "--src", str(long_zh), "--tgt", str(long_en), "--out", str(long_out))
        elapsed = time.monotonic() - started_long
        report = read_json(long_out / "report.json") if (long_out / "report.json").is_file() else {}
        record(
            "长视频/字幕矩阵",
            completed.returncode == 0 and report.get("summary", {}).get("cues") == 600,
            f"600 条 → status={report.get('status')} 违规={len(report.get('findings', []))} 耗时={elapsed:.2f}s",
        )

    portrait = ROOT / "work/samples-v2/portrait.mkv"
    if portrait.is_file():
        portrait_out = out_root / "portrait-mark"
        completed = run("mark", "--src", str(portrait), "--out", str(portrait_out))
        report = read_json(portrait_out / "mark.json") if (portrait_out / "mark.json").is_file() else {}
        verification = report.get("verification", {})
        record(
            "竖屏/AIGC 标识",
            completed.returncode == 0 and report.get("status") == "marked",
            f"status={report.get('status')} 字号={report.get('config', {}).get('font_size')} "
            f"显式={verification.get('explicit_label', {}).get('ok')}",
        )

    # 4) 全链路：build → dub → mark
    chain_dir = out_root / "chain"
    video = ROOT / CHAIN["video"]
    zh = ROOT / CHAIN["zh"]
    en = ROOT / CHAIN["en"]
    if not (video.is_file() and zh.is_file() and en.is_file()):
        record("全链路", False, "素材缺失")
    else:
        build_out = chain_dir / "matrix"
        completed = run("build", "--src", str(zh), "--tgt", str(en), "--out", str(build_out))
        report = read_json(build_out / "report.json") if (build_out / "report.json").is_file() else {}
        record(
            "全链路/字幕矩阵",
            completed.returncode == 0 and report.get("status") in {"ok", "warn"},
            f"status={report.get('status')} 产物={len(report.get('outputs', []))} 条违规={len(report.get('findings', []))}",
        )

        dub_out = chain_dir / "dub"
        completed = run(
            "dub",
            "--srt", str(en),
            "--video", str(video),
            "--out", str(dub_out),
            "--tts-command", STUB_TTS,
            "--concurrency", "6",
        )
        dub_report = read_json(dub_out / "dub_report.json") if (dub_out / "dub_report.json").is_file() else {}
        dub_summary = dub_report.get("summary", {})
        record(
            "全链路/配音编排",
            completed.returncode == 0 and dub_report.get("status") == "ok",
            f"status={dub_report.get('status')} 成功={dub_summary.get('synthesized')}/{dub_summary.get('cues')} "
            f"溢出>10%={dub_summary.get('overflow_over_10pct')}",
        )

        if dub_out / "dubbed.mp4" is not None and (dub_out / "dubbed.mp4").is_file():
            mark_out = chain_dir / "final"
            completed = run("mark", "--src", str(dub_out / "dubbed.mp4"), "--out", str(mark_out))
            mark_report = read_json(mark_out / "mark.json") if (mark_out / "mark.json").is_file() else {}
            verification = mark_report.get("verification", {})
            record(
                "全链路/AIGC 标识",
                completed.returncode == 0 and mark_report.get("status") == "marked",
                f"status={mark_report.get('status')} "
                f"显式={verification.get('explicit_label', {}).get('ok')} "
                f"隐式={verification.get('metadata_roundtrip', {}).get('ok')}",
            )
        else:
            record("全链路/AIGC 标识", False, "配音视频缺失")

        # 保留 BGM：用相位抵消兜底（不依赖 demucs 模型），验证拆伴奏→侧链混音→并进成片
        bgm_out = chain_dir / "dub-bgm"
        completed = run(
            "dub",
            "--srt", str(en),
            "--video", str(video),
            "--out", str(bgm_out),
            "--tts-command", STUB_TTS,
            "--keep-bgm",
            "--bgm-mode", "center",
        )
        bgm_report = read_json(bgm_out / "dub_report.json") if (bgm_out / "dub_report.json").is_file() else {}
        bgm_summary = bgm_report.get("summary", {}).get("bgm") or {}
        bgm_outputs = bgm_report.get("outputs", {})
        mixed_ok = bool(bgm_outputs.get("mixed")) and Path(bgm_outputs["mixed"]).is_file()
        video_ok = bool(bgm_outputs.get("video")) and Path(bgm_outputs["video"]).is_file()
        record(
            "全链路/保留BGM",
            completed.returncode == 0 and bool(bgm_summary) and mixed_ok and video_ok,
            f"mode={bgm_summary.get('mode')} separator={bgm_summary.get('separator')} "
            f"混音={mixed_ok} 成片={video_ok}",
        )

    # 5) 翻译层（离线 echo 渠道）：术语表占位 / 段落上下文 / 报告，再串排版
    mt_src = ROOT / "spike/pvt-out-vtv/en.srt"
    if mt_src.is_file():
        mt_out = out_root / "translate"
        glossary_path = out_root / "translate-glossary.tsv"
        glossary_path.write_text(
            "artificial intelligence\tAI\nvideo production\t视频制作\n",
            encoding="utf-8",
            newline="\n",
        )
        completed = run(
            "translate",
            "--src", str(mt_src),
            "--out", str(mt_out),
            "--provider", "echo",
            "--src-lang", "en",
            "--tgt-lang", "zh",
            "--context", "paragraph",
            "--glossary", str(glossary_path),
            # echo 只是把原文回显成"[zh] 原文"，字符数必然超标；这里验的是术语/上下文/
            # 排版链路，不想让默认精简去改动它，所以显式关掉（默认精简另有专项检查）
            "--no-condense",
        )
        mt_report = read_json(mt_out / "translate.json") if (mt_out / "translate.json").is_file() else {}
        mt_text = (mt_out / "zh.srt").read_text(encoding="utf-8") if (mt_out / "zh.srt").is_file() else ""
        mt_summary = mt_report.get("summary", {})
        record(
            "翻译层/术语表+上下文",
            completed.returncode == 0
            and mt_report.get("config", {}).get("provider") == "echo"
            and "{{" not in mt_text
            and "AI" in mt_text
            and "视频制作" in mt_text,
            f"provider={mt_report.get('config', {}).get('provider')} "
            f"段落={mt_summary.get('paragraphs')} 术语丢失={mt_summary.get('placeholder_lost')}",
        )

        mt_layout = out_root / "translate-layout"
        completed = run(
            "build",
            "--src", str(mt_src),
            "--tgt", str(mt_out / "zh.srt"),
            "--out", str(mt_layout),
            "--src-lang", "en",
            "--tgt-lang", "zh",
            "--wrap",
        )
        layout_report = read_json(mt_layout / "report.json") if (mt_layout / "report.json").is_file() else {}
        layout_info = layout_report.get("layout", {})
        record(
            "翻译层/译文排版",
            completed.returncode == 0 and bool(layout_info.get("enabled")),
            f"条数 {layout_info.get('cues_before')} → {layout_info.get('cues_after')} "
            f"剩余违规={len(layout_report.get('findings', []))}",
        )

        # 默认精简：不传任何 condense 参数，超 CPS 条目应自动精简并列出待抽检条目
        cond_src = out_root / "condense-source.srt"
        cond_src.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n"
            "Hello, this is a long sentence, and it keeps going.\n",
            encoding="utf-8",
            newline="\n",
        )
        cond_out = out_root / "translate-condense"
        completed = run(
            "translate", "--src", str(cond_src), "--out", str(cond_out), "--provider", "echo"
        )
        cond_report = read_json(cond_out / "translate.json") if (cond_out / "translate.json").is_file() else {}
        cond_config = cond_report.get("config", {}).get("condense", {})
        cond_summary = cond_report.get("summary", {}).get("condense", {})
        needs_review = cond_summary.get("needs_review") or []
        record(
            "翻译层/默认精简",
            completed.returncode == 0
            and cond_config.get("enabled") is True
            and cond_config.get("mode") == "auto"
            and len(needs_review) >= 1,
            f"enabled={cond_config.get('enabled')} mode={cond_config.get('mode')} "
            f"触发={cond_summary.get('triggered')} 待抽检={len(needs_review)}",
        )

        # zh→en 双向：反向术语表 + 英文目标排版（离线 echo，验方向性契约）
        reverse_src = out_root / "reverse-source.srt"
        reverse_src.write_text(
            "1\n00:00:00,000 --> 00:00:04,000\n刘翔是中国田径运动员，我们向他学习。\n",
            encoding="utf-8",
            newline="\n",
        )
        reverse_glossary = out_root / "reverse-glossary.tsv"
        reverse_glossary.write_text("Liu Xiang\t刘翔\n", encoding="utf-8", newline="\n")
        reverse_out = out_root / "translate-zh-en"
        completed = run(
            "translate",
            "--src", str(reverse_src),
            "--out", str(reverse_out),
            "--provider", "echo",
            "--src-lang", "zh",
            "--tgt-lang", "en",
            "--glossary", str(reverse_glossary),
            "--glossary-reverse",
        )
        reverse_report = (
            read_json(reverse_out / "translate.json")
            if (reverse_out / "translate.json").is_file()
            else {}
        )
        reverse_text = (
            (reverse_out / "en.srt").read_text(encoding="utf-8")
            if (reverse_out / "en.srt").is_file()
            else ""
        )
        record(
            "翻译层/zh→en 反向术语",
            completed.returncode == 0
            and "Liu Xiang" in reverse_text
            and "{{" not in reverse_text,
            f"术语命中={'Liu Xiang' in reverse_text} "
            f"术语数={reverse_report.get('config', {}).get('glossary', {}).get('terms')}",
        )

    # 6) 一条命令全链路（pipeline）：内封字幕路线，离线 echo + 占位 TTS
    softsub = ROOT / "spike/samples/S1_softsub.mkv"
    if softsub.is_file():
        pipeline_out = out_root / "pipeline"
        completed = run(
            "pipeline",
            "--src", str(softsub),
            "--out", str(pipeline_out),
            "--src-lang", "zh",
            "--tgt-lang", "en",
            "--provider", "echo",
            "--tts-command", STUB_TTS,
        )
        pipeline_report = (
            read_json(pipeline_out / "pipeline.json")
            if (pipeline_out / "pipeline.json").is_file()
            else {}
        )
        stages = {stage["stage"]: stage["status"] for stage in pipeline_report.get("stages", [])}
        outputs = pipeline_report.get("outputs", {})
        final_ok = bool(outputs.get("final_video")) and Path(outputs["final_video"]).is_file()
        burned_ok = bool(outputs.get("burned_video")) and Path(outputs["burned_video"]).is_file()
        record(
            "全链路/一条命令 pipeline",
            completed.returncode == 0
            and pipeline_report.get("status") == "ok"
            and stages.get("asr") == "skipped"
            and stages.get("merge") == "skipped"
            and stages.get("burn") in {"ok", "cached"}
            and final_ok
            and burned_ok,
            f"status={pipeline_report.get('status')} "
            f"阶段={stages} 成品={final_ok} 带字幕成片={burned_ok}",
        )
        # 第二次跑应全部命中缓存（不重跑重活）
        completed = run("pipeline", "--src", str(softsub), "--out", str(pipeline_out),
                        "--src-lang", "zh", "--tgt-lang", "en", "--provider", "echo",
                        "--tts-command", STUB_TTS)
        cached_report = (
            read_json(pipeline_out / "pipeline.json")
            if (pipeline_out / "pipeline.json").is_file()
            else {}
        )
        cached_stages = {
            stage["stage"]: stage["status"] for stage in cached_report.get("stages", [])
        }
        cached_all = all(
            cached_stages.get(name) in {"cached", "skipped"}
            for name in ("probe", "diarize", "merge", "translate", "build", "dub", "burn", "mark")
        )
        record(
            "全链路/pipeline 阶段缓存",
            completed.returncode == 0 and cached_all,
            f"status={cached_report.get('status')} 阶段={cached_stages}",
        )

    # 6) 多人声（离线）：speakers.json → 按说话人分派音色（用占位 TTS，不依赖 Edge）
    multi_src = out_root / "multi-voice-source.srt"
    multi_src.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nFirst speaker line\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond speaker line\n",
        encoding="utf-8",
        newline="\n",
    )
    multi_speakers = out_root / "multi-voice-speakers.json"
    multi_speakers.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "speakers": ["SPEAKER_00", "SPEAKER_01"],
                "cues": [
                    {"cue": 1, "speaker": "SPEAKER_00"},
                    {"cue": 2, "speaker": "SPEAKER_01"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    multi_out = out_root / "multi-voice"
    completed = run(
        "dub",
        "--srt", str(multi_src),
        "--out", str(multi_out),
        "--speakers-json", str(multi_speakers),
        "--speaker-voices", "en-US-GuyNeural,en-US-AriaNeural",
        "--tts-command", STUB_TTS,
    )
    multi_report = read_json(multi_out / "dub_report.json") if (multi_out / "dub_report.json").is_file() else {}
    multi_cues = multi_report.get("cues", [])
    voices = {cue.get("voice") for cue in multi_cues if isinstance(cue, dict)}
    record(
        "多人声/按说话人分派音色",
        completed.returncode == 0
        and multi_report.get("summary", {}).get("speakers") == 2
        and len(voices) == 2,
        f"说话人={multi_report.get('summary', {}).get('speakers')} 音色={sorted(v for v in voices if v)}",
    )

    # 7) 词级切条（ASR 后处理规则，合成数据离线验证）
    from submatrix.asr import segment_words

    synthetic_words = [
        {"start": 0.0, "end": 0.4, "word": " Hello"},
        {"start": 0.4, "end": 0.9, "word": " world."},
        {"start": 2.0, "end": 2.4, "word": " Second"},
        {"start": 2.4, "end": 2.9, "word": " sentence."},
    ]
    segments = segment_words(synthetic_words, pause_ms=500)
    record(
        "ASR/词级切条",
        [segment.text for segment in segments] == ["Hello world.", "Second sentence."],
        "、".join(segment.text for segment in segments),
    )

    # 7) 可选：真引擎配音（联网，非确定性 → 只在显式给了解释器时跑）
    if args.real_tts_python:
        real_out = out_root / "real-tts"
        started_real = time.monotonic()
        completed = run(
            "dub",
            "--srt", str(en),
            "--video", str(video),
            "--out", str(real_out),
            "--tts-python", str(args.real_tts_python),
            "--concurrency", "6",
        )
        elapsed = time.monotonic() - started_real
        report = read_json(real_out / "dub_report.json") if (real_out / "dub_report.json").is_file() else {}
        summary = report.get("summary", {})
        record(
            "真引擎/配音",
            completed.returncode == 0 and summary.get("failed") == 0,
            f"成功={summary.get('synthesized')}/{summary.get('cues')} "
            f"吸收={summary.get('absorbed')} atempo={summary.get('atempo')} "
            f"人工={summary.get('manual')} 耗时={elapsed:.1f}s",
        )

    passed = sum(1 for check in checks if check["ok"])
    wall_s = round(time.monotonic() - started, 1)
    regression = {
        "schema_version": 1,
        "tool": "submatrix",
        "command": "run_samples",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {"checks": len(checks), "passed": passed, "failed": len(checks) - passed, "wall_s": wall_s},
        "checks": checks,
    }
    (out_root / "regression.json").write_text(
        json.dumps(regression, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"\n合计 {passed}/{len(checks)} 通过，耗时 {wall_s}s")
    print(f"报告：{out_root / 'regression.json'}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
