"""阿里云百炼（DashScope）同步语音合成 worker —— 给 ``cuesmith dub --tts-command`` 调用。

一次调用合成一条字幕：

    python tools/tts_dashscope.py --text-file {text_file} --out {out} --voice {voice_id}

``{voice_id}`` 由 ``dub`` 按说话人替换成系统音色名（如 ``longanyang``）或声音复刻得到的
音色 ID（``tools/clone_voice.py`` 创建）。``{text_file}`` / ``{out}`` 是 dub 的既有占位符。

模型（2026-10 实测文档）：
- ``cosyvoice-v3-flash`` / ``cosyvoice-v3.5-flash/plus`` / ``cosyvoice-v3-plus`` / ``cosyvoice-v2``
- ``qwen-audio-3.1-tts-flash/plus``、``qwen3-tts-flash``

接口：``POST {base_url}/services/audio/tts/SpeechSynthesizer``（非流式），
返回 ``output.audio.url``（24 小时有效），下载后写盘。

环境变量（密钥只放环境变量，不落盘）：
- ``DASHSCOPE_API_KEY``：必填
- ``DASHSCOPE_WORKSPACE_ID``：业务空间 ID（百炼控制台 → 业务空间）；给了走
  ``https://{id}.cn-beijing.maas.aliyuncs.com/api/v1``（官方推荐域名）
- ``DASHSCOPE_BASE_URL``：直接覆盖接口地址（优先级最高）

退出码：0 成功；1 失败（stderr 末行是给 dub 看的原因）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

__all__ = [
    "DashScopeError",
    "create_voice",
    "delete_voice",
    "encode_audio_data_uri",
    "list_voices",
    "query_voice",
    "resolve_base_url",
    "synthesize",
    "wait_voice_ready",
]

DEFAULT_MODEL = "cosyvoice-v3-flash"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
WORKSPACE_BASE_URL = "https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1"
TTS_PATH = "/services/audio/tts/SpeechSynthesizer"
CUSTOMIZATION_PATH = "/services/audio/tts/customization"
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MIN_REFERENCE_SECONDS = 5.0
MAX_REFERENCE_SECONDS = 20.0


class DashScopeError(RuntimeError):
    """接口/参数/鉴权失败。"""


def resolve_base_url(
    base_url: str | None = None, workspace_id: str | None = None
) -> tuple[str, str]:
    """返回 ``(接口地址, 来源说明)``：显式参数 > DASHSCOPE_BASE_URL > 业务空间 > 兼容域名。"""

    if base_url:
        return base_url.rstrip("/"), "显式 --base-url"
    env_url = os.environ.get("DASHSCOPE_BASE_URL")
    if env_url:
        return env_url.rstrip("/"), "环境变量 DASHSCOPE_BASE_URL"
    workspace = workspace_id or os.environ.get("DASHSCOPE_WORKSPACE_ID")
    if workspace:
        return WORKSPACE_BASE_URL.format(workspace_id=workspace.strip()), "业务空间专属域名"
    return DEFAULT_BASE_URL, "兼容域名（建议配置 DASHSCOPE_WORKSPACE_ID 走专属域名）"


def _api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise DashScopeError(
            "缺少 DASHSCOPE_API_KEY：先设置环境变量（密钥只在环境变量里，不写进任何文件）"
        )
    return key


def _post(
    url: str,
    payload: dict,
    *,
    api_key: str,
    timeout: float,
    what: str,
) -> dict:
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise DashScopeError(f"{what}网络失败：{exc}") from exc
    if response.status_code != 200:
        raise DashScopeError(f"{what}失败（HTTP {response.status_code}）：{_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise DashScopeError(f"{what}返回的不是 JSON：{response.text[:200]}") from exc
    if not isinstance(data, dict):
        raise DashScopeError(f"{what}返回格式异常：{str(data)[:200]}")
    return data


def _error_detail(response: "requests.Response") -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:300] or "没有错误详情"
    if isinstance(payload, dict):
        code = payload.get("code") or (payload.get("error") or {}).get("code")
        message = payload.get("message") or (payload.get("error") or {}).get("message")
        if code or message:
            return f"{code or ''} {message or ''}".strip()
    return str(payload)[:300]


def _audio_bytes(output: dict, *, api_key: str, timeout: float, what: str) -> bytes:
    """从 ``output.audio`` 取音频：URL 下载或 Base64 内联。"""

    audio = output.get("audio") if isinstance(output, dict) else None
    if not isinstance(audio, dict):
        raise DashScopeError(f"{what}返回里没有 output.audio：{str(output)[:200]}")
    url = audio.get("url")
    if url:
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise DashScopeError(f"{what}音频下载失败：{exc}") from exc
        if response.status_code != 200:
            raise DashScopeError(f"{what}音频下载失败（HTTP {response.status_code}）")
        return response.content
    data = audio.get("data")
    if data:
        try:
            return base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            raise DashScopeError(f"{what}音频 Base64 解码失败：{exc}") from exc
    raise DashScopeError(f"{what}返回里既没有 audio.url 也没有 audio.data")


def synthesize(
    text: str,
    out_path: Path,
    *,
    voice: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    audio_format: str = "wav",
    sample_rate: int = 24000,
    instruction: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """合成一条文本并写盘；返回 ``{request_id, bytes, model, voice}``。"""

    text = text.strip()
    if not text:
        raise DashScopeError("待合成文本为空")
    url, _source = resolve_base_url(base_url, workspace_id)
    key = _api_key(api_key)
    payload: dict[str, object] = {
        "model": model,
        "input": {
            "text": text,
            "voice": voice,
            "format": audio_format,
            "sample_rate": sample_rate,
        },
    }
    if instruction:
        payload["input"]["instruction"] = instruction  # type: ignore[index]

    what = "语音合成"
    data = _post(f"{url}{TTS_PATH}", payload, api_key=key, timeout=timeout, what=what)
    audio = _audio_bytes(data.get("output") or {}, api_key=key, timeout=timeout, what=what)
    if not audio:
        raise DashScopeError("语音合成返回了空音频")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)
    return {
        "request_id": data.get("request_id"),
        "bytes": len(audio),
        "model": model,
        "voice": voice,
    }


# ---------------------------------------------------------------------- 声音复刻


def _run_ffmpeg(command: list[str], *, what: str) -> None:
    completed = subprocess.run(command, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-400:]
        raise DashScopeError(f"{what}失败：{detail or 'ffmpeg 返回非零状态'}")


def _wav_duration_seconds(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def normalize_reference(audio_path: Path, out_wav: Path, *, max_seconds: float = MAX_REFERENCE_SECONDS) -> Path:
    """把参考音频规整成 24kHz/16bit/单声道 WAV，并截到 ≤20s（复刻要求 10–20s 最佳）。"""

    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise DashScopeError(f"参考音频不存在：{audio_path}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(audio_path),
            "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
            "-t", f"{max_seconds:g}", str(out_wav),
        ],
        what="参考音频转码",
    )
    duration = _wav_duration_seconds(out_wav)
    if duration < MIN_REFERENCE_SECONDS:
        raise DashScopeError(
            f"参考音频太短（{duration:.1f}s），声音复刻至少需要 {MIN_REFERENCE_SECONDS:g}s（推荐 10–20s）"
        )
    if out_wav.stat().st_size > MAX_REFERENCE_BYTES:
        raise DashScopeError(f"参考音频超过 10MB（{out_wav.stat().st_size} 字节）")
    return out_wav


def encode_audio_data_uri(path: Path) -> str:
    """本地音频 → Base64 Data URI（官方支持，省去 OSS 公网 URL）。"""

    data = Path(path).read_bytes()
    if not data:
        raise DashScopeError(f"参考音频为空：{path}")
    return "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")


def create_voice(
    audio_path: Path,
    *,
    prefix: str,
    target_model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    language_hints: str | None = "zh",
    max_prompt_audio_length: float = 10.0,
    enable_preprocess: bool = False,
    timeout: float = 120.0,
) -> str:
    """上传参考音频创建音色，返回 voice_id（音色创建免费，合成按字符计费）。"""

    if not prefix or not all(char.isascii() and char.isalnum() for char in prefix) or len(prefix) > 10:
        raise DashScopeError("prefix 只能包含数字/英文字母，且不超过 10 个字符")
    url, _source = resolve_base_url(base_url, workspace_id)
    key = _api_key(api_key)
    work_dir = Path(tempfile.mkdtemp(prefix="cuesmith-voice-"))
    normalized = normalize_reference(Path(audio_path), work_dir / "reference.wav")
    try:
        payload = {
            "model": "voice-enrollment",
            "input": {
                "action": "create_voice",
                "target_model": target_model,
                "prefix": prefix,
                "audio": encode_audio_data_uri(normalized),
                "max_prompt_audio_length": max_prompt_audio_length,
                "enable_preprocess": enable_preprocess,
            },
        }
        if language_hints:
            payload["input"]["language_hints"] = [language_hints]
        data = _post(
            f"{url}{CUSTOMIZATION_PATH}", payload, api_key=key, timeout=timeout, what="创建音色"
        )
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    output = data.get("output") or {}
    voice_id = output.get("voice_id")
    if not voice_id:
        raise DashScopeError(f"创建音色没有返回 voice_id：{str(data)[:300]}")
    return str(voice_id)


def query_voice(
    voice_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    timeout: float = 120.0,
) -> dict:
    url, _source = resolve_base_url(base_url, workspace_id)
    key = _api_key(api_key)
    data = _post(
        f"{url}{CUSTOMIZATION_PATH}",
        {"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": voice_id}},
        api_key=key,
        timeout=timeout,
        what="查询音色",
    )
    output = data.get("output")
    return output if isinstance(output, dict) else {}


def wait_voice_ready(
    voice_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    timeout_s: float = 120.0,
    interval_s: float = 1.0,
) -> dict:
    """轮询音色状态：DEPLOYING → OK；其他状态直接报错。"""

    deadline = time.monotonic() + timeout_s
    while True:
        output = query_voice(
            voice_id, api_key=api_key, base_url=base_url, workspace_id=workspace_id
        )
        status = str(output.get("status") or "")
        if status.upper() == "OK":
            return output
        if status.upper() not in {"DEPLOYING", "PENDING", "PROCESSING"}:
            raise DashScopeError(f"音色 {voice_id} 状态异常：{status or '未知'}")
        if time.monotonic() >= deadline:
            raise DashScopeError(f"音色 {voice_id} 在 {timeout_s:g}s 内没有就绪（最后状态 {status}）")
        time.sleep(interval_s)


def list_voices(
    *,
    prefix: str | None = None,
    page_index: int = 0,
    page_size: int = 10,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    timeout: float = 120.0,
) -> list:
    url, _source = resolve_base_url(base_url, workspace_id)
    key = _api_key(api_key)
    payload: dict[str, object] = {
        "model": "voice-enrollment",
        "input": {
            "action": "list_voice",  # 实测 HTTP 动作名是 list_voice（不是 list_voices）
            "prefix": prefix,
            "page_index": page_index,
            "page_size": page_size,
        },
    }
    data = _post(
        f"{url}{CUSTOMIZATION_PATH}", payload, api_key=key, timeout=timeout, what="查询音色列表"
    )
    output = data.get("output") or {}
    voices = output.get("voice_list") or output.get("voices") or []
    return list(voices) if isinstance(voices, list) else []


def delete_voice(
    voice_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    workspace_id: str | None = None,
    timeout: float = 120.0,
) -> None:
    url, _source = resolve_base_url(base_url, workspace_id)
    key = _api_key(api_key)
    _post(
        f"{url}{CUSTOMIZATION_PATH}",
        {"model": "voice-enrollment", "input": {"action": "delete_voice", "voice_id": voice_id}},
        api_key=key,
        timeout=timeout,
        what="删除音色",
    )


# ---------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DashScope 同步语音合成（供 dub --tts-command 调用）")
    parser.add_argument("--text-file", type=Path, required=True, help="待合成文本文件")
    parser.add_argument("--out", type=Path, required=True, help="输出音频路径")
    parser.add_argument("--voice", required=True, help="系统音色名或声音复刻返回的 voice_id")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--format", default="wav", choices=["wav", "mp3", "pcm", "opus"])
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--instruction", default=None, help="自然语言指令（语速/情感）")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    try:
        text = args.text_file.read_text(encoding="utf-8").strip()
        result = synthesize(
            text,
            args.out,
            voice=args.voice,
            model=args.model,
            audio_format=args.format,
            sample_rate=args.sample_rate,
            instruction=args.instruction,
            base_url=args.base_url,
            workspace_id=args.workspace_id,
            timeout=args.timeout,
        )
    except (DashScopeError, OSError) as exc:
        print(f"TTS 失败：{exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"request_id": result["request_id"], "bytes": result["bytes"], "voice": result["voice"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
