"""BGM 保留：人声分离 + 伴奏混音（配音成片不再"只有人声、背景全没了"）。

收费配音软件（剪映专业版、阿里云智能配音、Descript）默认都会"保留背景音乐"：
先把原片音轨拆成人声 / 伴奏，再把人声换成 TTS，最后把伴奏垫回去——否则成片会
出现"朗读者独白 + 全程静音"的廉价感。

**不自建分离模型**（与"不自建 ASR/MT/TTS"同一口径），只管编排：

1. ``demucs``（htdemucs，CPU 可跑，Meta 开源）：音频领域最好用的两轨分离，模型权重
   从 ``dl.fbaipublicfiles.com`` 下载（本机实测可达）。慢但干净，是主路径；
2. ``center``（ffmpeg 相位抵消）：立体声且人声在正中的素材，``c0-c1`` 一减就掉人声，
   不需要任何模型/下载，作为 demucs 不可用时的兜底（会如实标注"兜底方案"）；
3. ``duck``：不分离，直接把原声压低当背景（原声里有说话声会很糊，只适合纯环境音）。

混音不是简单 ``amix``：TTS 一响，伴奏必须自动让路（``sidechaincompress`` 压缩），
否则两条人声打架。最后统一响度到 -16 LUFS（视频平台通行标准），避免忽大忽小。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import media
from .interpreters import (
    MISSING_NONE,
    Requirement,
    bool_probe,
    ordered_candidates,
    python_in_venv,
    resolve,
)

__all__ = [
    "BgmError",
    "BgmOptions",
    "BgmResult",
    "find_demucs_python",
    "mix_voice_and_bgm",
    "run_bgm",
]

FFMPEG = media.FFMPEG
MODES = ("auto", "separate", "center", "duck", "off")
SEPARATED_AUDIO = "no_vocals.wav"  # demucs --two-stems=vocals 的伴奏轨文件名
MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


class BgmError(RuntimeError):
    """BGM 处理不可继续（缺输入 / 分离失败 / 混音失败）。"""


@dataclass(frozen=True, slots=True)
class BgmOptions:
    source: Path  # 原始音视频（取它的音轨做人声分离）
    out_dir: Path
    mode: str = "auto"  # auto | separate | center | duck | off
    separator_python: str | None = None  # 装有 demucs 的解释器
    model: str = "htdemucs"
    bgm_gain_db: float = -2.0  # 伴奏相对原音量（负数=更轻）
    duck_db: float = -12.0  # TTS 说话时伴奏额外压低多少 dB
    duck_threshold: float = 0.05
    target_lufs: float = -16.0  # 成片响度目标
    # 混音输出格式：44.1kHz 立体声（与音源同规格）。配音轨本身是 24k 单声道，
    # 若混音也跟着用 24k 单声道，伴奏等于被砍掉一半频宽、还塌成单声道 —— 保留 BGM
    # 的意义就丢了大半，所以混音总线显式升到 44.1k/立体声。
    mix_sample_rate: int = 44100
    mix_channels: int = 2

    def validate(self) -> None:
        if self.mode not in MODES:
            raise BgmError(f"不认识的 BGM 模式：{self.mode!r}（{' / '.join(MODES)}）")
        if self.mode == "off":
            return
        if not -40 <= self.duck_db <= 0:
            raise BgmError("伴奏压低量必须在 0 到 -40 dB 之间")
        if not -30 <= self.bgm_gain_db <= 6:
            raise BgmError("伴奏增益必须在 -30–6 dB 之间")
        if self.mix_sample_rate < 8000:
            raise BgmError("采样率过低（至少 8000）")
        if self.mix_channels not in (1, 2):
            raise BgmError("混音声道数只能是 1（单声道）或 2（立体声）")


@dataclass(frozen=True, slots=True)
class BgmResult:
    mode: str  # separate | center | duck
    source: Path
    instrumental: Path
    mixed: Path
    separator: str  # demucs | center | original
    notes: tuple[str, ...] = ()
    wall_s: float = 0.0
    mix_sample_rate: int = 44100
    mix_channels: int = 2


# ---------------------------------------------------------------------- 环境


def _venv_scripts_dir(python: str | Path) -> Path:
    return Path(python).parent


def _demucs_binary(python: str | Path) -> Path | None:
    for name in ("demucs.exe", "demucs"):
        candidate = _venv_scripts_dir(python) / name
        if candidate.is_file():
            return candidate
    return None


_DEMUCS_PYTHON = Requirement(
    label="demucs",
    module="demucs",
    parameter="--separator-python",
    # 候选链（含环境变量）由 demucs_candidates 统一给，这里只管报错措辞
    hint="只在保留 BGM 时需要：把 demucs 装进 spike/demucs/.venv，"
    "或设置环境变量 SUBMATRIX_DEMUCS_PYTHON。",
)


def demucs_candidates(*explicit: str | None) -> list[str]:
    """候选解释器顺序：显式参数 → 环境变量 → 项目环境 → 当前解释器（已去重）。"""

    project = Path(__file__).resolve().parent.parent
    return ordered_candidates(
        explicit,
        (os.environ.get("SUBMATRIX_DEMUCS_PYTHON"),),
        python_in_venv(project / "spike" / "demucs" / ".venv"),
        (sys.executable,),
    )


def _can_import_demucs(python: str) -> bool:
    if not Path(python).is_file():
        return False
    try:
        completed = subprocess.run(
            [python, "-c", "import demucs"], capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def find_demucs_python(*candidates: str | None) -> str | None:
    """按顺序找第一个能 ``import demucs`` 的解释器；找不到返回 None。

    demucs 是**可选**组件（只有 ``--keep-bgm`` 才用），所以策略是
    ``MISSING_NONE`` 而不是抛错——回退到相位抵消式人声消除，由上层写告警。
    """

    return resolve(
        _DEMUCS_PYTHON,
        None,
        candidates=demucs_candidates(*candidates),
        probe=bool_probe(_can_import_demucs),
        on_missing=MISSING_NONE,
        error_cls=BgmError,
    )


# ---------------------------------------------------------------------- 分离


def _run(
    command: list[str], *, what: str, timeout: float | None = None, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[bytes]":
    return media.exec_tool(
        command,
        error_cls=BgmError,
        what=what,
        timeout_s=timeout,
        env=env,
        # bgm 这条链路既跑 ffmpeg 也跑 demucs，提示不能只说 ffmpeg
        missing_hint="找不到 {tool}，请确认已安装 ffmpeg / demucs",
    )


def _extract_audio(source: Path, out_wav: Path) -> Path:
    """取原片音轨为 44.1k 立体声（分离模型按立体声训练，先降到 24k 会伤质量）。"""

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            FFMPEG, "-v", "error", "-y", "-i", str(source),
            "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out_wav),
        ],
        what="提取原片音轨",
    )
    return out_wav


def torch_home() -> Path:
    """模型权重的落地目录：默认放项目内，避免写到用户目录（也便于离线复跑）。"""

    env = os.environ.get("SUBMATRIX_TORCH_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "spike" / "demucs" / ".torch"


def _separate_with_demucs(
    audio: Path, out_dir: Path, python: str, model: str, timeout: float
) -> Path:
    binary = _demucs_binary(python)
    base = [str(binary)] if binary else [python, "-m", "demucs.separate"]
    stem_dir = out_dir / "demucs"
    stem_dir.mkdir(parents=True, exist_ok=True)
    home = torch_home()
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TORCH_HOME": str(home)}
    _run(
        [
            *base,
            "--two-stems=vocals",
            "-n", model,
            "-d", "cpu",
            "-o", str(stem_dir),
            str(audio),
        ],
        what="demucs 人声分离",
        timeout=timeout,
        env=env,
    )
    found = sorted(stem_dir.rglob(SEPARATED_AUDIO))
    if not found:
        raise BgmError(f"demucs 跑完了但没找到伴奏轨（{SEPARATED_AUDIO}）：{stem_dir}")
    return found[0]


def _separate_center(audio: Path, out_wav: Path) -> Path:
    """相位抵消：立体声里居中的（人声）在 c0-c1 中抵消，两侧伴奏留下。"""

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            FFMPEG, "-v", "error", "-y", "-i", str(audio),
            "-af", "pan=stereo|c0=c0-c1|c1=c1-c0",
            "-c:a", "pcm_s16le", str(out_wav),
        ],
        what="相位抵消去人声",
    )
    return out_wav


# ---------------------------------------------------------------------- 混音


def _mix_bus_format(options: BgmOptions) -> str:
    """混音总线的统一格式：两条输入先转成同一采样率/声道，再进侧链与 amix。

    不做这一步的话，ffmpeg 会按第一条输入（TTS 配音，24k 单声道）协商格式，
    伴奏会被降成 24k 单声道再混——高频和声场都丢了。
    """

    layout = "stereo" if options.mix_channels == 2 else "mono"
    return f"aformat=sample_rates={options.mix_sample_rate}:channel_layouts={layout}"


def mix_filter(options: BgmOptions) -> str:
    """FFmpeg 混音图：TTS 响度归一化 + 伴奏侧链压缩（说话时自动让路）+ 总响度归一化。

    输入 0 = 配音，输入 1 = 伴奏。``sidechaincompress`` 的 main 是伴奏、sidechain 是配音：
    配音一有信号，伴奏就被压 ``duck_db``，停下后 release 回弹，听感像电台垫乐。
    两条输入都先走 ``aformat`` 升到 44.1k 立体声混音总线（见 ``_mix_bus_format``）。
    """

    fmt = _mix_bus_format(options)
    voice = f"{fmt},loudnorm=I=-16:TP=-1.5:LRA=11"
    bgm = f"{fmt},volume={options.bgm_gain_db:g}dB"
    if options.duck_db < 0:
        # sidechaincompress 的第二个输入是"控制信号"：配音一响，伴奏就被压
        # ratio 表达压缩深度，12dB ≈ 4:1（超出阈值的部分被压到 1/4）
        ratio = 10 ** (-options.duck_db / 20)
        chain = (
            f"[0:a]{voice},asplit=2[voice][side];"
            f"[1:a]{bgm}[bgm0];"
            f"[bgm0][side]sidechaincompress="
            f"threshold={options.duck_threshold:g}:ratio={ratio:.2f}:attack=20:release=300[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=longest:normalize=0,"
            f"loudnorm=I={options.target_lufs:g}:TP=-1.5:LRA=11[out]"
        )
    else:
        chain = (
            f"[0:a]{voice}[voice];"
            f"[1:a]{bgm}[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=longest:normalize=0,"
            f"loudnorm=I={options.target_lufs:g}:TP=-1.5:LRA=11[out]"
        )
    return chain


def _mean_volume_db(path: Path) -> float | None:
    """整轨平均电平（dB）：用来判断"分离出来的伴奏到底有没有东西"。"""

    completed = media.exec_tool(
        [media.FFMPEG, "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        error_cls=BgmError,
        what="读取平均电平",
        check=False,  # volumedetect 把结果写 stderr，退出码不重要
    )
    match = MEAN_VOLUME.search(media.stderr_text(completed))
    return float(match.group(1)) if match else None


def mix_voice_and_bgm(voice: Path, bgm: Path, out_wav: Path, options: BgmOptions) -> Path:
    """把配音和伴奏混成一条音轨（伴奏随配音自动让路，最后统一响度）。"""

    if not voice.is_file():
        raise BgmError(f"配音音轨不存在：{voice}")
    if not bgm.is_file():
        raise BgmError(f"伴奏音轨不存在：{bgm}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            FFMPEG, "-v", "error", "-y",
            "-i", str(voice),
            "-i", str(bgm),
            "-filter_complex", mix_filter(options),
            "-map", "[out]",
            "-ar", str(options.mix_sample_rate),
            "-ac", str(options.mix_channels),
            "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        what="配音与 BGM 混音",
    )
    return out_wav


# ---------------------------------------------------------------------- 主流程


def _source_digest(path: Path) -> str:
    """源片内容指纹，用来判断 demucs 旧结果还能不能复用（避免重跑白花几分钟）。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reusable_stems(out_dir: Path, source: Path) -> Path | None:
    """找上次 demucs 的伴奏轨（demucs 会写到 ``<out>/demucs/<模型>/<音轨>/``）。"""

    demucs_dir = out_dir / "demucs"
    if not demucs_dir.is_dir():
        return None
    found = sorted(demucs_dir.rglob(SEPARATED_AUDIO))
    if not found:
        return None
    instrumental = found[0]
    vocals = instrumental.with_name("vocals.wav")
    signature = out_dir / "demucs-source.sha256"
    if not vocals.is_file() or vocals.stat().st_size <= 0:
        return None
    if instrumental.stat().st_size <= 0 or not signature.is_file():
        return None
    try:
        if signature.read_text(encoding="utf-8").strip() != _source_digest(source):
            return None
    except OSError:
        return None
    return instrumental


def run_bgm(options: BgmOptions, *, voice: Path) -> BgmResult:
    """拆出伴奏 → 与配音混音。``mode=off`` 直接抛（调用方应先判断）。"""

    options.validate()
    source = Path(options.source)
    if not source.is_file():
        raise BgmError(f"原始音视频不存在：{source}")
    if options.mode == "off":
        raise BgmError("BGM 模式为 off，无需处理")

    started = time.monotonic()
    out_dir = Path(options.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    if options.mode == "duck":
        # 不分离：原声本身当背景（纯环境音/无对白素材适用，有对白会很糊）
        instrumental = _extract_audio(source, out_dir / "original.wav")
        mode, separator = "duck", "original"
        notes.append("duck 模式不分离人声：原片对白会一起留在背景里")
    else:
        audio = _extract_audio(source, out_dir / "source.wav")
        separator = "center"
        separate_mode = options.mode
        python = (
            find_demucs_python(options.separator_python)
            if separate_mode in {"auto", "separate"}
            else None
        )
        cached_stems = _reusable_stems(out_dir, source) if python else None
        if cached_stems is not None:
            instrumental = cached_stems
            separator = "demucs"
            mode = "separate"
            notes.append("复用同目录已分离的伴奏轨（demucs 结果缓存，源片未变）")
        elif python:
            instrumental = _separate_with_demucs(
                audio, out_dir, python, options.model, timeout=3600.0
            )
            separator = "demucs"
            mode = "separate"
            (out_dir / "demucs-source.sha256").write_text(
                _source_digest(source), encoding="utf-8"
            )
        elif separate_mode == "separate":
            raise BgmError(
                "separate 模式需要 demucs：先装 spike/demucs 环境，或用 "
                "--bgm-mode center（相位抵消兜底）/ --bgm-mode duck"
            )
        else:
            instrumental = _separate_center(audio, out_dir / "center.wav")
            mode = "center"
            notes.append("demucs 不可用，已退回相位抵消（立体声居中的人声可去；单声道素材无效）")

    mixed = mix_voice_and_bgm(voice, instrumental, out_dir / "mixed.wav", options)
    if mode != "duck":
        # 诚实报告：有些素材根本没有背景音乐，分离出来的伴奏是静音，保留 BGM 等于没做
        audio_db = _mean_volume_db(out_dir / "source.wav")
        inst_db = _mean_volume_db(instrumental)
        if audio_db is not None and inst_db is not None and inst_db <= audio_db - 20:
            notes.append(
                f"原片几乎没有伴奏（伴奏比原声低 {audio_db - inst_db:.0f}dB），保留 BGM 无实际效果"
            )
    return BgmResult(
        mode=mode,
        source=source,
        instrumental=instrumental,
        mixed=mixed,
        separator=separator,
        notes=tuple(notes),
        wall_s=round(time.monotonic() - started, 2),
        mix_sample_rate=options.mix_sample_rate,
        mix_channels=options.mix_channels,
    )
