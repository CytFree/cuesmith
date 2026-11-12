#!/usr/bin/env python3
"""sherpa-onnx 离线说话人分离 worker。

这个脚本只跑在 ``spike/diarization/.venv`` 里（sherpa-onnx + onnxruntime + numpy），
主包 cuesmith 保持"标准库 + ffmpeg"，模型只在进程边界调用（和 demucs/ASR 同一口径）。

输入必须是 **16kHz / 16bit / 单声道** WAV（由 ``cuesmith.diarize`` 用 ffmpeg 抽好）。
输出 JSON：``{"ok": true, "segments": [{"start": 0.31, "end": 6.86, "speaker": 0}, ...]}``。
出错时也写 JSON（``ok=false`` + traceback），让主进程能给出可读错误。

**VAD 预过滤（OPT-6）**：给了 ``--vad-model`` 时先用 silero VAD 找出语音区间，
把**语音区间按顺序拼起来**（之间补 0.15s 静音）再送分离/聚类，最后把结果按时间轴
映射回原视频。音乐/掌声/环境声完全进不了分割与聚类——它们原本会被 pyannote 分割
模型当成"说话人"，聚出毫无意义的伪簇（真实素材里 10min 播客 auto 0.8 能聚出 15 人；
中文 4 人样本 auto 会多出第 5 人）。实测 10min 英文播客原始簇 10→9、说话人 4→3，
中文 4 人样本 5→4，且映射后 100% 落在 VAD 语音区间内。
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

if __package__:
    from . import worker_protocol as protocol
else:  # 脚本直跑：sys.path[0] 是 cuesmith/，只能按顶层模块导入
    import worker_protocol as protocol

# 拼接语音段时插入的静音（秒）：完全无缝拼接会让分割模型把两段不同的话
# 当成同一句，留一点静音既保留换人线索，又不让音乐/噪声进来。
_VAD_JOIN_SILENCE_S = 0.15


def _read_wav(path: Path) -> tuple[object, int]:
    """读 16bit 单声道 wav → float32 [-1, 1] 的 numpy 数组。"""

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sampwidth = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if sampwidth != 2:
        raise RuntimeError(f"只支持 16bit PCM（当前 {sampwidth * 8}bit）：{path}")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _resample(samples, sample_rate: int, target: int):
    """线性插值重采样（只为兜底；正常路径由 ffmpeg 直接给 16k）。"""

    import numpy as np

    if sample_rate == target:
        return samples
    duration = len(samples) / sample_rate
    count = int(round(duration * target))
    old_t = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    new_t = np.linspace(0.0, duration, num=count, endpoint=False)
    return np.interp(new_t, old_t, samples).astype(np.float32)


def _vad_intervals(
    samples,
    sample_rate: int,
    model: Path,
    *,
    threshold: float,
    min_silence: float,
    min_speech: float,
    max_speech: float,
    num_threads: int,
) -> list[tuple[int, int]]:
    """用 silero VAD 找出语音区间，返回 ``[(start_sample, end_sample), ...]``。"""

    import sherpa_onnx

    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(model)
    config.silero_vad.threshold = float(threshold)
    config.silero_vad.min_silence_duration = float(min_silence)
    config.silero_vad.min_speech_duration = float(min_speech)
    config.silero_vad.max_speech_duration = float(max_speech)
    config.sample_rate = int(sample_rate)
    config.num_threads = max(1, int(num_threads))
    if not config.validate():
        raise RuntimeError(f"VAD 配置校验失败：检查模型文件与其采样率：{model}")

    window = int(getattr(config.silero_vad, "window_size", 0) or 0)
    if window <= 0:
        window = 512 if sample_rate == 16000 else max(1, int(round(sample_rate * 0.032)))
    # 缓冲区必须能装下最长一段语音，否则 max_speech_duration 会被内部截断逻辑影响
    buffer_seconds = max(60.0, float(max_speech) + 20.0)

    detector = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=buffer_seconds)
    for start in range(0, len(samples), window):
        detector.accept_waveform(samples[start : start + window])
    detector.flush()

    intervals: list[tuple[int, int]] = []
    while not detector.empty():
        segment = detector.front
        begin = int(segment.start)
        end = begin + len(segment.samples)
        if end > begin:
            intervals.append((begin, end))
        detector.pop()
    return intervals


def _compact_speech(
    samples, sample_rate: int, intervals: list[tuple[int, int]]
) -> tuple[object, list[tuple[float, float, float, float]]]:
    """把语音区间拼成一条"只有语音"的音轨，并返回时间轴映射表。

    映射表每项 ``(compact_start, compact_end, original_start, original_end)``（秒）。
    分离/聚类只看得到语音，音乐/掌声/环境声不会变成伪说话人。
    """

    import numpy as np

    pad = max(1, int(round(_VAD_JOIN_SILENCE_S * sample_rate)))
    pieces: list[object] = []
    layout: list[tuple[float, float, float, float]] = []
    position = 0  # 已拼接的采样点数
    for start, end in intervals:
        start = max(0, start)
        end = min(len(samples), end)
        if end - start < 2:
            continue
        if layout:
            pieces.append(np.zeros(pad, dtype=np.float32))
            position += pad
        length = end - start
        layout.append(
            (
                position / sample_rate,
                (position + length) / sample_rate,
                start / sample_rate,
                end / sample_rate,
            )
        )
        pieces.append(samples[start:end])
        position += length
    if not pieces:
        return samples, []
    return np.concatenate(pieces), layout


def _map_segments_back(
    segments: list[dict],
    layout: list[tuple[float, float, float, float]],
    *,
    min_piece_s: float = 0.04,
) -> list[dict]:
    """把 compact 时间轴上的分段映射回原时间轴。

    一个 turn 在 compact 轴上可能横跨多个语音段（中间隔着被丢掉的音乐），
    映射回去要**按语音段拆开**，否则 turn 会把音乐区间也算成"在说话"，
    覆盖率虚高、字幕归属也会被拉偏。
    """

    mapped: list[dict] = []
    for item in segments:
        compact_start = float(item["start"])
        compact_end = float(item["end"])
        speaker = int(item["speaker"])
        for part_start, part_end, original_start, _original_end in layout:
            overlap_start = max(compact_start, part_start)
            overlap_end = min(compact_end, part_end)
            if overlap_end - overlap_start < min_piece_s:
                continue
            start = original_start + (overlap_start - part_start)
            end = original_start + (overlap_end - part_start)
            if end > start:
                mapped.append(
                    {"start": round(start, 3), "end": round(end, 3), "speaker": speaker}
                )
    mapped.sort(key=lambda item: (item["start"], item["end"]))
    return mapped


# ---------------------------------------------------------------- 二次聚类（OPT-7）
# 纯 numpy 实现（worker 环境只有 sherpa-onnx + numpy）。思路：
#   1. 对每个 turn 切 1.5s 滑窗提声纹 embedding，窗均值 = turn embedding；
#      顺带算"同一 turn 内多窗两两相似度"的中位数 → 同人相似度参考 s_same；
#   2. 在 sherpa 原始簇上做 **只合并不拆分** 的贪心 average-linkage：
#      每次合并平均成对相似度最高、且 ≥ τ = alpha × s_same 的两个主簇；
#   3. 短簇（语音 <3s 或 <1%）按相似度 + 时间邻近并入主簇。
# 为什么不用簇质心相似度：大簇质心会被"平均"拉向公共方向，相似度虚高
# （实测 en 10min 45 个 turn 的簇质心相似度 0.914，真实平均成对只有 0.33）。


def _l2_normalize(matrix):
    import numpy as np

    matrix = np.asarray(matrix, dtype=np.float64)
    norm = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.maximum(norm, 1e-12)


def _windows_in_span(
    start: float, end: float, *, win: float = 1.5, hop: float = 1.0, min_win: float = 0.6
) -> list[tuple[float, float]]:
    """在 turn 内切滑动窗；太短的 turn 退化成整段（≥min_win 才保留）。"""

    out: list[tuple[float, float]] = []
    duration = end - start
    if duration <= 0:
        return out
    if duration <= win:
        if duration >= min_win:
            out.append((start, end))
        return out
    t = start
    while t + win <= end + 1e-6:
        out.append((t, t + win))
        t += hop
    if out and (end - out[-1][1]) >= min_win:
        out.append((max(start, end - win), end))
    return out


def _turn_embeddings(extractor, samples, sample_rate: int, spans, *, win: float = 1.5, hop: float = 1.0):
    """每个 turn → L2 归一化 embedding；同时返回同人相似度参考 within（NaN = 单窗算不出）。"""

    import numpy as np

    dim = int(extractor.dim)
    embeddings = np.zeros((len(spans), dim), dtype=np.float32)
    within = np.full(len(spans), np.nan, dtype=np.float32)
    for index, (start, end) in enumerate(spans):
        # 掐掉首尾 80ms：切分边界常混入相邻人声
        a = start + 0.08
        b = max(a + 0.1, end - 0.08)
        windows = _windows_in_span(a, min(b, end), win=win, hop=hop)
        values = []
        for w_start, w_end in windows:
            s0 = max(0, int(round(w_start * sample_rate)))
            s1 = min(len(samples), int(round(w_end * sample_rate)))
            if s1 - s0 < int(0.1 * sample_rate):
                continue
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate, samples[s0:s1])
            stream.input_finished()
            if not extractor.is_ready(stream):
                continue
            values.append(np.asarray(extractor.compute(stream), dtype=np.float32))
        if not values:
            continue
        normalized = _l2_normalize(np.stack(values))
        embeddings[index] = _l2_normalize(normalized.mean(axis=0))
        if len(normalized) >= 2:
            sims = normalized @ normalized.T
            upper = np.triu_indices(len(normalized), 1)
            within[index] = float(sims[upper].mean())
    return embeddings, within


def _same_speaker_reference(within) -> float:
    """同人相似度参考 = within 的中位数（有效值不足 2 个时退回 0.6，宁可少并）。"""

    import numpy as np

    if within is None:
        return 0.6
    values = np.asarray(within, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return 0.6
    return float(np.median(values))


def _relabel_by_first_seen(labels, spans) -> object:
    """按簇首次出现的时间顺序重编号 0..K-1（与线上 SPEAKER_00 口径一致）。"""

    import numpy as np

    labels = np.asarray(labels)
    order = np.argsort(np.asarray(spans, dtype=np.float64)[:, 0], kind="stable")
    mapping: dict[int, int] = {}
    out = np.empty(len(labels), dtype=np.int64)
    for index in order:
        label = int(labels[index])
        if label not in mapping:
            mapping[label] = len(mapping)
        out[index] = mapping[label]
    return out


def _merge_embeddings(
    embeddings,
    spans,
    labels,
    within,
    *,
    alpha: float = 0.55,
    min_speech_s: float = 3.0,
    min_share: float = 0.01,
) -> dict:
    """只合并不拆分的二次聚类；返回新标签与诊断信息。"""

    import numpy as np

    x = _l2_normalize(np.asarray(embeddings, dtype=np.float64))
    spans = np.asarray(spans, dtype=np.float64).reshape(-1, 2)
    labels = np.asarray(labels).copy()
    count = len(x)
    if count == 0:
        return {"labels": labels, "clusters_in": 0, "clusters_out": 0, "s_same": None, "tau": None, "merge_sims": []}

    durations = np.maximum(spans[:, 1] - spans[:, 0], 0.0)
    total = float(durations.sum())
    unique = np.unique(labels)
    speech = {int(u): float(durations[labels == u].sum()) for u in unique}
    s_same = _same_speaker_reference(within)
    tau = float(alpha) * s_same

    mains = [
        int(u)
        for u in unique
        if speech[int(u)] >= max(min_speech_s, min_share * total)
    ]
    if not mains:  # 全是极短 turn：整体一簇
        merged_labels = _relabel_by_first_seen(np.zeros(count, dtype=np.int64), spans)
        return {
            "labels": merged_labels,
            "clusters_in": int(len(unique)),
            "clusters_out": 1,
            "s_same": round(s_same, 4),
            "tau": round(tau, 4),
            "merge_sims": [],
        }

    groups = [[int(i) for i in np.where(labels == u)[0]] for u in mains]
    merge_sims: list[float] = []
    while len(groups) >= 2:
        k = len(groups)
        sims = np.zeros((k, k), dtype=np.float64)
        for i in range(k):
            gi = np.asarray(groups[i], dtype=np.int64)
            for j in range(i + 1, k):
                gj = np.asarray(groups[j], dtype=np.int64)
                if len(gi) and len(gj):
                    value = float((x[gi] @ x[gj].T).mean())  # 平均成对相似度（非质心！）
                    sims[i, j] = sims[j, i] = value
        np.fill_diagonal(sims, -np.inf)
        flat = int(np.argmax(sims))
        i, j = divmod(flat, k)
        if i > j:
            i, j = j, i
        best = float(sims[i, j])
        if best < tau:
            break
        merge_sims.append(round(best, 4))
        groups[i] = groups[i] + groups[j]
        groups.pop(j)

    out = np.full(count, -1, dtype=np.int64)
    for group_index, group in enumerate(groups):
        out[np.asarray(group, dtype=np.int64)] = group_index
    # 短簇就近并入主簇：相似度为主、时间邻近加分
    short_indices = np.where(out < 0)[0]
    if len(short_indices):
        live = sorted(set(int(v) for v in out if v >= 0))
        for index in short_indices:
            centroid = x[index]
            start, end = spans[index]
            best_main, best_score = live[0], -np.inf
            for main in live:
                members = np.where(out == main)[0]
                similarity = float(centroid @ _l2_normalize(x[members].mean(axis=0)))
                gap = float(
                    np.min(
                        np.minimum(
                            np.abs(spans[members, 0] - end), np.abs(spans[members, 1] - start)
                        )
                    )
                )
                score = similarity + 0.25 * float(np.exp(-gap / 10.0))
                if score > best_score:
                    best_main, best_score = main, score
            out[index] = best_main

    final = _relabel_by_first_seen(out, spans)
    return {
        "labels": final,
        "clusters_in": int(len(unique)),
        "clusters_out": int(len(np.unique(final))),
        "s_same": round(s_same, 4),
        "tau": round(tau, 4),
        "merge_sims": merge_sims,
    }


def _vad_report(
    intervals: list[tuple[int, int]], sample_rate: int, samples_len: int
) -> dict:
    speech = sum(end - start for start, end in intervals) / float(sample_rate)
    total = samples_len / float(sample_rate) if samples_len else 0.0
    return {
        "enabled": True,
        "segments": len(intervals),
        "speech_s": round(speech, 3),
        "speech_pct": round(speech / total * 100, 2) if total > 0 else 0.0,
        "removed_s": round(max(0.0, total - speech), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="sherpa-onnx offline speaker diarization worker")
    parser.add_argument("--audio", required=True, help="16kHz 16bit mono wav")
    parser.add_argument("--segmentation", required=True, help="pyannote segmentation onnx")
    parser.add_argument("--embedding", required=True, help="speaker embedding onnx")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--num-speakers", type=int, default=-1, help="-1 = 自动聚类")
    parser.add_argument("--threshold", type=float, default=0.5, help="自动聚类阈值，越大说话人越少")
    parser.add_argument("--min-duration-on", type=float, default=0.3)
    parser.add_argument("--min-duration-off", type=float, default=0.5)
    parser.add_argument("--num-threads", type=int, default=1)
    parser.add_argument("--window-shift-ratio", type=float, default=0.1)
    # OPT-6：VAD 预过滤
    parser.add_argument("--vad-model", default=None, help="silero VAD onnx（给了才启用预过滤）")
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--vad-min-silence", type=float, default=0.25)
    parser.add_argument("--vad-min-speech", type=float, default=0.25)
    parser.add_argument("--vad-max-speech", type=float, default=30.0)
    # OPT-7：声纹二次聚类（只合并不拆分）
    parser.add_argument(
        "--clustering", choices=["sherpa", "embedding"], default="sherpa", help="聚类方式"
    )
    parser.add_argument("--merge-alpha", type=float, default=0.55, help="合并阈值系数 τ=α×s_same")
    parser.add_argument("--merge-min-speech", type=float, default=3.0, help="主簇最短语音（秒）")
    parser.add_argument("--merge-min-share", type=float, default=0.01, help="主簇最小语音占比")
    args = parser.parse_args()

    output = Path(args.output)
    payload: dict[str, object] = {"ok": False}
    started = time.monotonic()
    vad: dict | None = None
    try:
        import sherpa_onnx

        samples, sample_rate = _read_wav(Path(args.audio))
        original_len = len(samples)
        original_duration_s = original_len / float(sample_rate) if sample_rate else 0.0
        layout: list[tuple[float, float, float, float]] | None = None
        if args.vad_model:
            vad_started = time.monotonic()
            intervals = _vad_intervals(
                samples,
                sample_rate,
                Path(args.vad_model),
                threshold=args.vad_threshold,
                min_silence=args.vad_min_silence,
                min_speech=args.vad_min_speech,
                max_speech=args.vad_max_speech,
                num_threads=args.num_threads,
            )
            vad = _vad_report(intervals, sample_rate, original_len)
            vad["model"] = str(args.vad_model)
            vad["threshold"] = float(args.vad_threshold)
            vad["min_silence_s"] = float(args.vad_min_silence)
            vad["min_speech_s"] = float(args.vad_min_speech)
            vad["max_speech_s"] = float(args.vad_max_speech)
            if intervals:
                samples, layout = _compact_speech(samples, sample_rate, intervals)
                vad["mode"] = "compact"
                vad["model_input_s"] = round(len(samples) / sample_rate, 3)
            else:
                # 全是音乐/静音：没有语音可拼，按原音频跑（上层如实报告 0 人）
                vad["skipped"] = "no-speech"
            vad["elapsed_s"] = round(time.monotonic() - vad_started, 3)

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(args.segmentation),
                    window_shift_ratio=args.window_shift_ratio,
                ),
                num_threads=max(1, args.num_threads),
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(args.embedding),
                num_threads=max(1, args.num_threads),
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=args.num_speakers, threshold=args.threshold
            ),
            min_duration_on=args.min_duration_on,
            min_duration_off=args.min_duration_off,
        )
        if not config.validate():
            raise RuntimeError("配置校验失败：检查模型文件路径/采样率是否匹配")

        diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        samples = _resample(samples, sample_rate, diarizer.sample_rate)
        result = diarizer.process(samples).sort_by_start_time()
        segments = [
            {
                "start": round(float(item.start), 3),
                "end": round(float(item.end), 3),
                "speaker": int(item.speaker),
            }
            for item in result
        ]
        clustering_payload: dict = {"mode": "sherpa", "alpha": args.merge_alpha}
        if args.clustering == "embedding" and len(segments) >= 2:
            # OPT-7：声纹二次聚类（只合并不拆分）。失败就保留 sherpa 原始结果并如实记录。
            try:
                clustering_started = time.monotonic()
                extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                    sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                        model=str(args.embedding),
                        num_threads=max(1, min(4, args.num_threads)),
                    )
                )
                spans = [(item["start"], item["end"]) for item in segments]
                raw_labels = [item["speaker"] for item in segments]
                embeddings, within = _turn_embeddings(
                    extractor, samples, diarizer.sample_rate, spans
                )
                merged = _merge_embeddings(
                    embeddings,
                    spans,
                    raw_labels,
                    within,
                    alpha=args.merge_alpha,
                    min_speech_s=args.merge_min_speech,
                    min_share=args.merge_min_share,
                )
                for item, label in zip(segments, merged.pop("labels")):
                    item["speaker"] = int(label)
                clustering_payload = {
                    "mode": "embedding",
                    "alpha": args.merge_alpha,
                    "turns": len(segments),
                    "elapsed_s": round(time.monotonic() - clustering_started, 3),
                    **merged,
                }
            except Exception as exc:  # noqa: BLE001 - 二次聚类失败不能拖垮整条链路
                clustering_payload = {
                    "mode": "sherpa",
                    "alpha": args.merge_alpha,
                    "fallback": f"{type(exc).__name__}: {exc}",
                }
        if layout is not None:
            # compact 时间轴 → 原视频时间轴（按语音段拆开，不含被丢掉的音乐区间）
            segments = _map_segments_back(segments, layout)
        payload = protocol.ok_payload(
            sample_rate=int(diarizer.sample_rate),
            duration_s=round(original_duration_s, 3),
            model_input_s=round(len(samples) / diarizer.sample_rate, 3),
            elapsed_s=round(time.monotonic() - started, 3),
            vad=vad,
            clustering=clustering_payload,
            segments=segments,
        )
    except Exception as exc:  # noqa: BLE001 - worker 必须把失败原因带回主进程
        payload = protocol.error_payload(
            exc, elapsed_s=round(time.monotonic() - started, 3), vad=vad
        )
    finally:
        protocol.write_payload(output, payload, indent=2)

    if not payload.get("ok"):
        protocol.emit_progress(str(payload.get("error", "diarization failed")))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
