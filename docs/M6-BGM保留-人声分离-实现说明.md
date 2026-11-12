# M6 — 保留背景音乐（人声分离 + 侧链混音）

> 状态：**已完成**。新增 `cuesmith/bgm.py`、`dub --keep-bgm` 与 `tests/test_bgm.py`（16 项）；
> 一键回归新增"全链路/保留BGM"检查。触发原因：用户要求"BGM 这个也要做"——
> 收费配音软件的成片不会在没人说话时变成死寂，这是交付质感的分水岭。
>
> **M9 增补**：混音总线从 24kHz 单声道升到 **44.1kHz 立体声**（两条输入先 `aformat`，
> 避免 `amix` 按 24k 单声道配音轨协商格式把伴奏砍闷），报告记录格式，保留降级能力。
> 见 `M9-句子合并与BGM高清混音-实现说明.md` §3。

---

## 1. 为什么必须做

M4 的 `dub` 产出的是**替换音轨**：`-map 0:v -map 1:a`。结果是中文配音在说话，
没人说话的时间段是**纯静音**——原片的音乐、环境声、现场感全没了。用户对比收费软件
（剪映专业版、阿里云智能配音、Descript）第一耳朵就能听出来"你这个像半成品"。

收费软件的做法固定是三步：**人声分离 → 换人声 → 伴奏垫回**。M6 把这三步做成编排层能力。

---

## 2. 架构：模型不自建，只在进程边界调用

与 ASR/TTS 同一口径（PRD §1 已锁"不自建 ASR/MT/TTS/OCR"）：

```
dub --keep-bgm
   ├─ ffmpeg 抽原片音轨（44.1k 立体声）
   ├─ demucs --two-stems=vocals（CPU，子进程，模型只在 spike/demucs/.venv 里）
   │     → vocals.wav（丢弃） / no_vocals.wav（伴奏，保留）
   ├─ ffmpeg 混音：voice(loudnorm) + bgm(sidechaincompress, 配音一响就压)
   └─ 成片 mux：视频流 copy + 混音轨 aac
```

主包 `cuesmith` **不 import torch / demucs**，只在需要时：

1. `find_demucs_python()` 逐个探测候选解释器（显式参数 → `CUESMITH_DEMUCS_PYTHON`
   → 项目内 `spike/demucs/.venv` → 当前解释器），用 `import demucs` 判断可用（毫秒级）；
2. 找不到时按模式降级（`auto` → 相位抵消；`separate` → 明确报错）；
3. 分离结果按文件名契约找 `no_vocals.wav`，不解析 demucs 的日志。

---

## 3. 三种分离策略（`--bgm-mode`）

| 模式 | 实现 | 成本 | 适用 |
|---|---|---|---|
| `auto`（默认） | 有 demucs 用 demucs，否则相位抵消 | 取决于 demucs | 无脑用 |
| `separate` | demucs htdemucs（两轨：vocals / no_vocals） | CPU 约 1.2–1.4× 音频时长 | 要质量且环境齐 |
| `center` | ffmpeg `pan=stereo\|c0=c0-c1\|c1=c1-c0` | 秒级、零下载 | 立体声、人声居中；单声道素材无效 |
| `duck` | 不分离，原声整体压低当背景 | 秒级 | 纯环境音；有对白会糊（报告会写） |

`auto` 的降级**必须显式告知**：`dub_report.json` 的 `warnings` 会写
"demucs 不可用，已退回相位抵消（立体声居中的人声可去；单声道素材无效）"。

---

## 4. 混音：不是简单 amix

```
[0:a] loudnorm=I=-16:TP=-1.5:LRA=11, asplit=2 [voice][side];
[1:a] volume=-2dB [bgm0];
[bgm0][side] sidechaincompress=threshold=0.05:ratio=4.00:attack=20:release=300 [bgm];
[voice][bgm] amix=inputs=2:duration=longest:normalize=0,
             loudnorm=I=-16:TP=-1.5:LRA=11 [out]
```

三个设计点（都踩过坑）：

1. **配音先做响度归一化**（-16 LUFS）：Edge-TTS 不同条目的音量本来就不齐，
   不归一化的话 sidechain 阈值时灵时不灵。
2. **伴奏侧链压缩**：`sidechaincompress` 的第二个输入是控制信号（配音）。
   `--bgm-duck 12`（dB）换算成 `ratio = 10^(12/20) ≈ 4:1`——用 ratio 表达压缩深度，
   免得再叠一层 `compand`。`attack=20ms / release=300ms` 是"说话立刻让路、停顿马上回弹"。
3. **混完再统一响度**到 -16 LUFS：否则 `amix` 会按输入数量做隐式衰减，成片忽大忽小。

`normalize=0` 很关键：FFmpeg 的 `amix` 默认会把每路除以输入数量，配音会凭空小 6dB。

---

## 5. 报告字段（`dub_report.json`）

```json
{
  "config":  { "bgm": { "enabled": true, "mode": "auto", "gain_db": -2.0, "duck_db": -12.0 } },
  "summary": { "bgm": { "mode": "separate", "separator": "demucs", "wall_s": 47.95 } },
  "warnings": [],
  "outputs": { "audio": "...dubbed.wav", "mixed": "...bgm/mixed.wav", "video": "...dubbed.mp4" }
}
```

- `outputs.audio` 仍是**纯配音轨**（保持旧语义，回归不受影响）；
- `outputs.mixed` 是**真正并进视频的音轨**；
- `warnings` 会写降级原因、以及"原片几乎没有伴奏"——**不假装做了 BGM**。

---

## 6. 环境（本机已装好，可复现）

```powershell
uv venv spike\demucs\.venv --python 3.10
uv pip install --python spike\demucs\.venv\Scripts\python.exe `
    "torch==2.2.2" "torchaudio==2.2.2" --index-url https://download.pytorch.org/whl/cpu
uv pip install --python spike\demucs\.venv\Scripts\python.exe demucs soundfile "numpy==1.26.4"
```

踩过的三个坑（都写进环境固定版本了）：

| 坑 | 现象 | 处理 |
|---|---|---|
| torch 2.7.1+cpu 在本 venv 里 | `OSError [WinError 1114]` 加载 `c10.dll` 失败 | 降到 **torch 2.2.2 + torchaudio 2.2.2**（与 demucs 4.1.0 组合稳定） |
| numpy 2.x | 运行时报 `Numpy is not available`（torch 2.2 是按 numpy 1.x ABI 编的） | 固定 **numpy 1.26.4** |
| 模型权重落 `%USERPROFILE%\.cache\torch` | 该目录 PermissionError，且不在项目内 | 调用时设 `TORCH_HOME=spike\demucs\.torch`（可用 `CUESMITH_TORCH_HOME` 覆盖） |

模型权重 `htdemucs` 首次运行自动从 `dl.fbaipublicfiles.com` 下载（本机实测可达，
GitHub 被墙不影响），约 80MB，之后离线可用。

---

## 7. 实测（用户提供的英文访谈开头 60s，原片带 BGM）

| 指标 | 结果 |
|---|---|
| 素材 | 60.0s，原声 RMS 3560 |
| 配音 | 16/16 成功（Edge-TTS zh-CN-XiaoxiaoNeural，+30%，并发 6） |
| demucs 分离 | **47.95s**（≈1.25× 实时，CPU int8 无 GPU） |
| 分离结果 | 伴奏 RMS 1274 / 人声 RMS 3305 → 原人声被完整剥离 |
| 混音 | 配音说话段 RMS 5605；静音段仍有 501 的背景（不是死寂） |
| 伴奏让路 | 12dB 侧链压缩，说话时压下去、停 300ms 内回弹 |
| 响度 | 成片 -16 LUFS |
| `dub` 端到端 | **61.7s**（含抽轨、分离、混音、mux） |
| 成片 | h264 + aac，120s 源 60s 片段时长一致；AIGC 标识显式 ✅ 隐式 ✅ |

对照：**纯口播无音乐**的素材（2 分钟播客 `seg.mp4`）分离后伴奏 RMS 仅 29（原声 3116），
报告明确写"原片几乎没有伴奏，保留 BGM 无实际效果"——能力边界如实暴露。

---

## 8. 验收

- **AC-21** `dub --keep-bgm` 产出成片音轨同时含配音与原片伴奏；配音说话时伴奏被压低
  且可配置（默认 12dB），成片响度统一；分离不可用时按 `auto` 降级并在报告 `warnings` 说明；
  原片无伴奏时必须明说，不得假装保留。

单测覆盖（`tests/test_bgm.py`，16 项）：

- 滤镜图：`sidechaincompress` / `volume` / `amix` / `loudnorm` 是否按配置出现；
- 相位抵消：左右相同 → 近静音；左右不同 → 保住两侧内容；
- 混音：Goertzel 量"配音前 vs 配音中"的伴奏幅度，确认被压下 >40%；
- 降级：无 demucs 时 `auto` → center 并告警、`separate` → 明确报错；
- 接线：`dub` 报告写入 `config.bgm/summary.bgm/outputs.mixed/warnings`，
  视频用混音轨 mux；`BgmError` → `DubError`；
- 参数校验：模式、压低量、增益范围、缺 `--video/--bgm-source`。

---

## 9. 明确不做

| 项 | 原因 |
|---|---|
| UVR / MDX 系多模型分离 | 模型权重在 GitHub Releases（本机被墙），`audio_separator` 还要额外 onnxruntime；demucs 已够用 |
| 4 轨分离（鼓/贝斯/人声/其他） | 本工具只需"人声 vs 其他"；多轨对字幕配音没有额外收益 |
| 实时/流式分离 | CPU 1.2× 实时已经是离线场景的合理成本，直播不是本工具目标 |
| 自动判断"该不该保留 BGM" | 版权与创作意图只有人知道；默认关闭、显式 `--keep-bgm` 开启，报告只陈述事实 |

> **素材合规**：分离只应用于自有/已授权素材；本工具不提供"去人声后二次分发"的引导，
> 成片仍强制打 AIGC 标识（FR-4）。
