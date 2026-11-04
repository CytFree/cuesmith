"""开发工具：用微软免费翻译端点把 SRT 译成英文（联调真实素材用）。

⚠️ 这不是产品功能：PRD 明确"翻译引擎不自建，用底座渠道"。这个脚本只是为了让
**真实素材**能一路跑到 build/dub/mark，不然缺 en.srt 链路就断了。

端点来自 pyVideoTrans 的 `_microsoft.py`（免费、无需 key），已在 spike 阶段验证可用。

用法：
    python tools/translate_srt.py --src zh.srt --out en.srt [--from zh-Hans --to en]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from submatrix.srt import Cue, Subtitle, dump_srt, load_srt  # noqa: E402

URL = "https://edge.microsoft.com/translate/translatetext?from={src}&to={tgt}&isEnterpriseClient=false"
BATCH = 50  # 一次 POST 多条，减少往返


def translate(texts: list[str], *, src: str, tgt: str, timeout: float = 120.0) -> list[str]:
    url = URL.format(src=src, tgt=tgt)
    payload = json.dumps(texts, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [item["translations"][0]["text"].strip() for item in data]


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…。！？])\s+")


def clean_text(text: str) -> str:
    """清掉 ASR 的排版噪声：多余空格、标点后缺空格、句首小写。

    这些噪声会直接拉低机器翻译质量（"Actually,I could do" 这种会被当成怪词）。
    """

    cleaned = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    cleaned = re.sub(r"([,;:])(?=[A-Za-z0-9])", r"\1 ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def split_sentences(text: str) -> list[str]:
    """按句末标点切成整句：整句喂给翻译，质量明显好于半句碎片。"""

    cleaned = clean_text(text)
    if not cleaned:
        return []
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(cleaned) if part.strip()]
    return parts or [cleaned]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="免费微软翻译（联调用）")
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--from", dest="source_lang", default="zh-Hans")
    parser.add_argument("--to", dest="target_lang", default="en")
    args = parser.parse_args(argv)

    subtitle, encoding = load_srt(args.src)
    if len(subtitle) == 0:
        print("输入字幕为空", file=sys.stderr)
        return 1

    # 每条 cue 先拆成整句再翻，翻完按原 cue 拼回去（不改变时间轴）
    units = [split_sentences(cue.text) for cue in subtitle.cues]
    flat = [sentence for group in units for sentence in group]
    if not flat:
        print("没有可翻译的文本", file=sys.stderr)
        return 1

    translated: list[str] = []
    started = time.monotonic()
    for start in range(0, len(flat), BATCH):
        chunk = flat[start : start + BATCH]
        translated.extend(translate(chunk, src=args.source_lang, tgt=args.target_lang))
    elapsed = time.monotonic() - started

    if len(translated) != len(flat):
        print(f"条数不一致：{len(flat)} → {len(translated)}", file=sys.stderr)
        return 2

    joiner = "" if args.target_lang.lower().startswith("zh") else " "
    cues: list[Cue] = []
    cursor = 0
    for cue, group in zip(subtitle.cues, units):
        pieces = translated[cursor : cursor + len(group)]
        cursor += len(group)
        cues.append(Cue(timing=cue.timing, text=joiner.join(pieces)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dump_srt(Subtitle(cues=tuple(cues))), encoding="utf-8", newline="\n")

    print(f"翻译：{len(subtitle)} 条 cue / {len(flat)} 个整句，{elapsed:.1f}s，编码 {encoding}")
    print(f"产物：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
