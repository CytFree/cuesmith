"""真实素材冒烟：默认全部跳过，靠环境变量开启。

为什么单独一个文件：``tests/`` 里其余用例都用假引擎 / 注入 runner / 6 秒彩条视频，
跑得快、可重复；但**真实素材才暴露过的两类问题**它们抓不到——

1. 阶段缓存判据漂移（换视频/改参数却复用旧产物）；
2. 报告里的时长和实际音轨对不上（``total_audio_ms`` 曾比 ``dubbed.wav`` 少 8 秒）。

三条用例按成本分档，互不依赖：

    # 便宜档（约 2 秒）：真视频探一次 + 二次命中缓存
    $env:SUBMATRIX_REAL_VIDEO = "D:\\软件\\下载\\央视科普-1.mp4"
    python -m pytest tests/test_real_sample.py -q

    # 现有产物树的结构契约（约 1 秒，不重跑任何阶段）
    $env:SUBMATRIX_REAL_OUT  = "work/cctv-demo"
    python -m pytest tests/test_real_sample.py -q

    # 重档（约 5 分钟，真 ASR；翻译走 echo、TTS 走 ffmpeg 正弦，全程离线）
    $env:SUBMATRIX_REAL_E2E = "1"
    python -m pytest tests/test_real_sample.py -q -m slow
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from submatrix.pipeline import PipelineOptions, run_pipeline
from submatrix.report import file_digest

VIDEO = os.environ.get("SUBMATRIX_REAL_VIDEO", "")
OUT = os.environ.get("SUBMATRIX_REAL_OUT", "")

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

needs_video = pytest.mark.skipif(
    not (VIDEO and Path(VIDEO).is_file()),
    reason="设置 SUBMATRIX_REAL_VIDEO=<真实视频路径> 才跑（默认跳过）",
)
needs_tree = pytest.mark.skipif(
    not (OUT and (Path(OUT) / "pipeline.json").is_file()),
    reason="设置 SUBMATRIX_REAL_OUT=<已有的产物树> 才跑（默认跳过）",
)
needs_tools = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg / ffprobe 不在 PATH"
)


def ffprobe(path: Path) -> dict:
    completed = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def duration_ms(path: Path) -> int:
    return round(float(ffprobe(path)["format"]["duration"]) * 1000)


def offline_tts_command() -> str:
    """离线 TTS：让 ffmpeg 生成 1 秒正弦当"合成结果"，不碰网络也不依赖本地模型。"""

    return f'"{FFMPEG}" -v error -y -f lavfi -i sine=frequency=440:duration=1 {{out}}'


@needs_video
@needs_tools
def test_real_video_probe_is_cached_on_rerun(tmp_path) -> None:
    """真视频探一次（真 ffprobe）→ 同参数再跑必须命中缓存。

    这条钉的是"缓存判据绑在输入 sha + 源语言上"：判据一旦退化成"文件存在就算命中"，
    换视频复用同一个 --out 就会输出上一条视频的字幕。
    """

    source = Path(VIDEO)
    options = PipelineOptions(
        src=source, out=tmp_path / "out", src_lang="zh", tgt_lang="en", stop_after="probe"
    )

    first = run_pipeline(options)
    stages = {stage["stage"]: stage["status"] for stage in first.report["stages"]}
    assert stages["probe"] == "ok"

    manifest = json.loads(
        (tmp_path / "out" / "probe" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["input"]["sha256"] == file_digest(source)[1]
    assert manifest["src_lang"] == "zh"

    second = run_pipeline(options)
    stages = {stage["stage"]: stage["status"] for stage in second.report["stages"]}
    assert stages["probe"] == "cached"


@needs_tree
@needs_tools
def test_existing_sample_contract() -> None:
    """已有产物树的结构契约：报告里的数字必须和盘上的文件对得上。

    真实素材踩过：``dub_report.json`` 写 ``total_audio_ms=146470``，而 ``dubbed.wav``
    实际 154118ms（P1-5b 把"补到视频长度"的 max() 搬进局部变量、忘了写回报告）。
    这种漂移假引擎测不出来，只能拿真产物对。
    """

    out = Path(OUT)

    report = json.loads((out / "pipeline.json").read_text(encoding="utf-8"))
    assert report["stages"], "pipeline.json 里没有阶段清单"

    dub = json.loads((out / "dub" / "dub_report.json").read_text(encoding="utf-8"))
    wav = out / "dub" / "dubbed.wav"
    assert wav.is_file(), f"缺配音音轨：{wav}"

    claimed = dub["summary"]["total_audio_ms"]
    actual = duration_ms(wav)
    # 音轨 = 内容结束点 + 100ms 尾巴，两者必须对得上（差 8 秒就是那个 bug）
    assert 0 <= actual - claimed <= 200, f"报告说 {claimed}ms，音轨实际 {actual}ms"

    assert dub["summary"]["cues"] == len(dub["cues"]) > 0
    assert all(entry["audio_ms"] > 0 for entry in dub["cues"] if entry["action"] != "failed")

    final = out / "final" / "marked.mp4"
    assert final.is_file(), f"缺最终成品：{final}"
    probe = ffprobe(final)
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in probe["streams"]}
    assert codecs.get("video") == "h264"  # AC-14
    assert codecs.get("audio") == "aac"
    if VIDEO and Path(VIDEO).is_file():
        assert abs(duration_ms(final) - duration_ms(Path(VIDEO))) <= 1000


@pytest.mark.slow
@needs_video
@needs_tools
@pytest.mark.skipif(
    os.environ.get("SUBMATRIX_REAL_E2E") != "1",
    reason="设置 SUBMATRIX_REAL_E2E=1 才跑全链路（约 5 分钟，含真 ASR）",
)
def test_real_video_full_pipeline_and_rerun_is_cached(tmp_path) -> None:
    """真视频全链路跑一遍（真 ASR / 真排版 / 真烧录 / 真标识），再跑一遍必须全缓存。

    翻译走 ``echo``、TTS 走 ffmpeg 正弦，所以这条**不需要网络**：
    它验的是编排、缓存、ffmpeg 各阶段与报告契约，不是翻译/合成质量。
    ASR 用自动探测的解释器；本机装了好几套 venv 时可以用 ``SUBMATRIX_ASR_PYTHON``
    钉死一个（实测 154s 素材约 4–5 分钟）。
    """

    source = Path(VIDEO)
    options = PipelineOptions(
        src=source,
        out=tmp_path / "out",
        src_lang="zh",
        tgt_lang="en",
        provider="echo",
        tts_command=offline_tts_command(),
        keep_bgm=False,  # 不跑 demucs：模型不在时别白等两分钟
        burn_position="top",
        asr_python=os.environ.get("SUBMATRIX_ASR_PYTHON") or None,
    )

    first = run_pipeline(options)
    assert first.status == "ok", first.report["warnings"]
    stages = {stage["stage"]: stage["status"] for stage in first.report["stages"]}
    assert stages["asr"] in {"ok", "cached"}
    assert stages["dub"] in {"ok", "partial"}
    assert stages["burn"] == "ok"
    assert stages["mark"] == "ok"

    final = Path(first.outputs["final_video"])
    probe = ffprobe(final)
    codecs = {stream["codec_type"]: stream["codec_name"] for stream in probe["streams"]}
    assert codecs.get("video") == "h264"  # AC-14
    assert codecs.get("audio") == "aac"
    assert abs(duration_ms(final) - duration_ms(source)) <= 1000

    # 同参数再跑：所有阶段要么缓存、要么跳过，不许重算
    second = run_pipeline(options)
    statuses = {stage["stage"]: stage["status"] for stage in second.report["stages"]}
    assert set(statuses.values()) <= {"cached", "skipped"}, statuses
    assert second.status == first.status
