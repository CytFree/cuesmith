"""说话人分离精度验证（真实多人对话素材 + 官方参考输出）。

素材：sherpa-onnx 官方测试文件 ``0-four-speakers-zh.wav``（56.9s，4 人中文对话）。
参考输出：sherpa 官方文档给出的 10 段 turn 列表（见
https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html ），
在「已知 4 人」条件下逐段比对边界与说话人归属，并顺带看自动聚类阈值的影响。

用法：
    python tools/validate_diarization_accuracy.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CLI = [sys.executable, "-m", "submatrix"]
MODEL_DIR = ROOT / "spike" / "diarization" / "models"
AUDIO = MODEL_DIR / "0-four-speakers-zh.wav"
OUT = ROOT / "work" / "multi-speaker" / "validation"
TOLERANCE_S = 0.35

# 官方文档输出（speaker 0–3 为官方标签，顺序即时间顺序）
EXPECTED = [
    (0.318, 6.865, 0),
    (7.017, 10.747, 1),
    (11.455, 13.632, 1),
    (13.750, 17.041, 2),
    (22.137, 24.837, 0),
    (27.638, 29.478, 3),
    (30.001, 31.553, 3),
    (33.680, 37.932, 3),
    (48.040, 50.470, 2),
    (52.529, 54.605, 0),
]


def run_diarize(out_dir: Path, extra: list[str]) -> dict:
    import subprocess

    completed = subprocess.run(
        [
            *CLI,
            "diarize",
            "--src", str(AUDIO),
            "--out", str(out_dir),
            "--src-lang", "zh",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report_path = out_dir / "diarize.json"
    if completed.returncode != 0 or not report_path.is_file():
        raise RuntimeError(f"diarize 失败（{completed.returncode}）：{completed.stderr[-500:]}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def main() -> int:
    if not AUDIO.is_file():
        print(f"缺少测试音频：{AUDIO}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    # 1) 已知 4 人：逐段比对
    report = run_diarize(OUT / "known-4", ["--speakers", "4"])
    turns = [(item["start_ms"] / 1000, item["end_ms"] / 1000, item["speaker"]) for item in report["segments"]]

    # 官方标签 → 本工具标签：按时间重叠投票
    overlap: dict[int, dict[str, float]] = {}
    for start, end, label in EXPECTED:
        for actual_start, actual_end, speaker in turns:
            value = max(0.0, min(end, actual_end) - max(start, actual_start))
            if value > 0:
                overlap.setdefault(label, {})
                overlap[label][speaker] = overlap[label].get(speaker, 0.0) + value
    label_map = {label: max(items, key=items.get) for label, items in overlap.items()}

    matched = 0
    max_boundary_diff = 0.0
    details: list[dict[str, object]] = []
    for start, end, label in EXPECTED:
        # 找重叠最大的实际 turn
        best = None
        best_value = 0.0
        for actual_start, actual_end, speaker in turns:
            value = max(0.0, min(end, actual_end) - max(start, actual_start))
            if value > best_value:
                best, best_value = (actual_start, actual_end, speaker), value
        if best is None:
            details.append({"expected": [start, end, label], "actual": None, "ok": False})
            continue
        actual_start, actual_end, speaker = best
        boundary_diff = max(abs(actual_start - start), abs(actual_end - end))
        max_boundary_diff = max(max_boundary_diff, boundary_diff)
        same_speaker = label_map.get(label) == speaker
        ok = boundary_diff <= TOLERANCE_S and same_speaker
        matched += int(ok)
        details.append(
            {
                "expected": [round(start, 3), round(end, 3), label],
                "actual": [round(actual_start, 3), round(actual_end, 3), speaker],
                "boundary_diff_s": round(boundary_diff, 3),
                "same_speaker": same_speaker,
                "ok": ok,
            }
        )

    summary = report["summary"]
    known = {
        "expected_turns": len(EXPECTED),
        "actual_turns": len(turns),
        "speakers": summary["speakers"],
        "turn_accuracy": f"{matched}/{len(EXPECTED)}",
        "max_boundary_diff_s": round(max_boundary_diff, 3),
        "speaker_label_map": label_map,
        "rtf": summary["rtf"],
        "wall_s": summary["wall_s"],
        "details": details,
    }
    print(f"已知 4 人：{matched}/{len(EXPECTED)} 段匹配，最大边界误差 {max_boundary_diff:.3f}s，"
          f"说话人 {summary['speakers']} 个，RTF {summary['rtf']}")

    # 2) 自动聚类阈值扫描
    threshold_sweep: list[dict[str, object]] = []
    for threshold in (0.5, 0.7, 0.8, 0.9):
        sweep_report = run_diarize(OUT / f"auto-{str(threshold).replace('.', '')}", ["--threshold", str(threshold)])
        threshold_sweep.append(
            {
                "threshold": threshold,
                "speakers": sweep_report["summary"]["speakers"],
                "turns": sweep_report["summary"]["turns"],
                "ok": sweep_report["summary"]["speakers"] == 4,
            }
        )
        print(f"自动聚类 threshold={threshold}: {sweep_report['summary']['speakers']} 个说话人")

    payload = {
        "schema_version": 1,
        "tool": "validate_diarization_accuracy",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {
            "audio": str(AUDIO.relative_to(ROOT)),
            "duration_s": report["input"]["duration_s"],
            "reference": "sherpa-onnx 官方 4 人测试音频 + 官方文档参考输出",
        },
        "known_speakers": known,
        "auto_threshold_sweep": threshold_sweep,
        "wall_s": round(time.monotonic() - started, 1),
    }
    output = OUT / "diarization-validation.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\n报告：{output}")
    return 0 if matched == len(EXPECTED) else 1


if __name__ == "__main__":
    sys.exit(main())
