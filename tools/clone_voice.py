"""阿里云百炼「声音复刻」CLI：参考音频 → 音色 ID（一次性操作，之后配音复用该 ID）。

示例：

    # 创建音色（参考音频 10–20s 最佳；支持 wav/mp3/m4a，也接受视频文件自动抽音轨）
    python tools/clone_voice.py create --audio work/multi-speaker/demo2.wav --label 说话人A --prefix spkA

    # 查看账号下已有音色
    python tools/clone_voice.py list

    # 查询/删除
    python tools/clone_voice.py query --voice-id cosyvoice-v3-flash-spkA-xxxx
    python tools/clone_voice.py delete --voice-id cosyvoice-v3-flash-spkA-xxxx

创建音色免费；合成时按目标模型字符计费。参考音频要求（官方）：
WAV(16bit)/MP3/M4A、时长 10–20s（≤60s）、≥16kHz、≤10MB、单人无 BGM/无重叠说话。

密钥只走环境变量 ``DASHSCOPE_API_KEY``；业务空间 ID 用 ``DASHSCOPE_WORKSPACE_ID``（可选）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tts_dashscope as ds  # noqa: E402

DEFAULT_VOICES_FILE = Path(__file__).resolve().parent / "voices.json"


def _load_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_map(path: Path, mapping: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _clean_prefix(raw: str) -> str:
    cleaned = "".join(char for char in raw if char.isascii() and char.isalnum())[:10]
    return cleaned or "submatrix"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阿里云百炼声音复刻")
    sub = parser.add_subparsers(dest="action", required=True)

    create = sub.add_parser("create", help="用参考音频创建音色")
    create.add_argument("--audio", type=Path, required=True, help="参考音频（10–20s 单人干净人声）")
    create.add_argument("--label", default=None, help="音色备注名（默认用文件名）")
    create.add_argument("--prefix", default=None, help="音色前缀（字母数字 ≤10，默认由 label 生成）")
    create.add_argument("--model", default=ds.DEFAULT_MODEL, help="目标合成模型（必须与后续配音一致）")
    create.add_argument("--language", default="zh", help="参考音频语种提示：zh/en/...")
    create.add_argument("--max-prompt", type=float, default=10.0, help="参考音频参与复刻的最大秒数（3–30）")
    create.add_argument("--preprocess", action="store_true", help="开启降噪/音量规整（有底噪时用）")
    create.add_argument("--save-json", type=Path, default=DEFAULT_VOICES_FILE, help="把 label→voice_id 记到 JSON")
    create.add_argument("--no-wait", action="store_true", help="只创建不等待就绪（默认等待）")
    create.add_argument("--base-url", default=None)
    create.add_argument("--workspace-id", default=None)

    list_cmd = sub.add_parser("list", help="列出账号下的音色")
    list_cmd.add_argument("--prefix", default=None)
    list_cmd.add_argument("--page-size", type=int, default=20)
    list_cmd.add_argument("--base-url", default=None)
    list_cmd.add_argument("--workspace-id", default=None)

    query = sub.add_parser("query", help="查询音色详情/状态")
    query.add_argument("--voice-id", required=True)
    query.add_argument("--base-url", default=None)
    query.add_argument("--workspace-id", default=None)

    delete = sub.add_parser("delete", help="删除音色")
    delete.add_argument("--voice-id", required=True)
    delete.add_argument("--yes", action="store_true", help="确认删除（不确认只打印）")
    delete.add_argument("--base-url", default=None)
    delete.add_argument("--workspace-id", default=None)

    args = parser.parse_args(argv)
    try:
        if args.action == "create":
            label = args.label or args.audio.stem
            prefix = _clean_prefix(args.prefix or label)
            voice_id = ds.create_voice(
                args.audio,
                prefix=prefix,
                target_model=args.model,
                language_hints=args.language or None,
                max_prompt_audio_length=args.max_prompt,
                enable_preprocess=args.preprocess,
                base_url=args.base_url,
                workspace_id=args.workspace_id,
            )
            print(f"voice_id: {voice_id}")
            if not args.no_wait:
                output = ds.wait_voice_ready(
                    voice_id, base_url=args.base_url, workspace_id=args.workspace_id
                )
                print(f"状态：{output.get('status', 'OK')}")
            mapping = _load_map(args.save_json)
            mapping[label] = voice_id
            _save_map(args.save_json, mapping)
            print(f"已记录：{args.save_json}（{label} → {voice_id}）")
            return 0

        if args.action == "list":
            voices = ds.list_voices(
                prefix=args.prefix,
                page_size=args.page_size,
                base_url=args.base_url,
                workspace_id=args.workspace_id,
            )
            if not voices:
                print("（账号下没有自定义音色）")
            for item in voices:
                print(json.dumps(item, ensure_ascii=False, default=str))
            return 0

        if args.action == "query":
            output = ds.query_voice(
                args.voice_id, base_url=args.base_url, workspace_id=args.workspace_id
            )
            print(json.dumps(output, ensure_ascii=False, default=str, indent=2))
            return 0

        if args.action == "delete":
            if not args.yes:
                print(f"将删除音色 {args.voice_id}；确认请加 --yes")
                return 0
            ds.delete_voice(args.voice_id, base_url=args.base_url, workspace_id=args.workspace_id)
            print(f"已删除：{args.voice_id}")
            return 0
    except (ds.DashScopeError, OSError) as exc:
        print(f"声音复刻失败：{exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
