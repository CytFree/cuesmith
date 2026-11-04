"""从 Wikimedia Commons 下载真实语音样本（含署名信息）。

选材标准：真人中文说话、CC BY / 公有领域、体积小、有环境声/背景音最好。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "submatrix-research/0.1 (offline pipeline test material)"}
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "work" / "real-samples"

# Commons 原始文件名 → 本地简洁文件名
NAME_MAP = {
    "File:【巴黎奥运会】徐卓一：会尽最大的努力向刘翔靠近.webm": "real_olympic.webm",
    "File:【两会30秒】水庆霞：中国女足非世界顶级，但会不断追赶.webm": "real_football.webm",
}

TITLES = [
    "File:【巴黎奥运会】徐卓一：会尽最大的努力向刘翔靠近.webm",
    "File:【两会30秒】水庆霞：中国女足非世界顶级，但会不断追赶.webm",
]


def api(params: dict) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json"})
    for attempt in range(3):
        try:
            request = urllib.request.Request(f"{API}?{query}", headers=UA)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    return {}


def fetch_info(title: str) -> dict:
    data = api(
        {
            "action": "query",
            "titles": title,
            "prop": "videoinfo",
            "viprop": "url|size|mime|extmetadata",
        }
    )
    page = next(iter(data["query"]["pages"].values()))
    video = page["videoinfo"][0]
    meta = video.get("extmetadata") or {}

    def value(key: str) -> str:
        raw = meta.get(key, {}).get("value", "")
        return raw if isinstance(raw, str) else ""

    return {
        "title": title,
        "url": video["url"],
        "size": video.get("size", 0),
        "mime": video.get("mime"),
        "artist": value("Artist"),
        "license": value("LicenseShortName"),
        "license_url": value("LicenseUrl"),
        "description": value("ImageDescription")[:300],
        "page": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}",
    }


def download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
            if len(data) < 100_000:
                raise RuntimeError(f"下载体积异常：{len(data)} 字节")
            path.write_bytes(data)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  第 {attempt + 1} 次失败：{type(exc).__name__} {str(exc)[:70]}")
            if attempt == 3:
                raise
            time.sleep(3)


OUT.mkdir(parents=True, exist_ok=True)
credits = []
for title in TITLES:
    info = fetch_info(title)
    local = OUT / NAME_MAP.get(title, Path(urllib.parse.unquote(info["url"])).name[:40])
    if local.is_file():
        print(f"已存在，跳过下载：{local.name}")
    else:
        print(f"下载 {local.name}（{info['size'] / 1024 / 1024:.1f} MB，{info['license']}）")
        started = time.time()
        download(info["url"], local)
        print(f"  完成：{local.stat().st_size / 1024 / 1024:.1f} MB，{time.time() - started:.1f}s")
    credits.append({**info, "local_path": str(local.relative_to(ROOT))})

(OUT / "credits.json").write_text(
    json.dumps(credits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
)
print("\n署名信息已写入 work/real-samples/credits.json")
