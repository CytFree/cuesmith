# cuesmith · 字幕工坊

**一条命令把视频变成「发布字幕 + 双语字幕 + 配音成片 + 硬字幕成片 + AIGC 标识」。**
（原名 submatrix，公开发布前改名；见 [CHANGELOG](CHANGELOG.md)。）

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11-blue)
[![tests](https://github.com/CytFree/cuesmith/actions/workflows/tests.yml/badge.svg)](https://github.com/CytFree/cuesmith/actions/workflows/tests.yml)

> Windows + CPU 实测通过的 MVP：一条命令把「一条视频」变成「发布字幕 + 双语字幕 + 配音成片 + 硬字幕成片 + AIGC 标识」。
>
> 状态：**M0–M22 完成**，**858 个用例**（854 通过 / 4 按需跳过）、**覆盖率 83%**、
> `ruff` 与 `pyflakes` 零告警、**27/27 一键回归**、zh↔en 双向真实素材、多人声自动判人数
> （VAD 预过滤 + 声纹二次聚类，6 个真实样本全中）与多音色配音、本地 Web GUI（含任务历史/重跑）均已跑通。

---

## 1. 它能做什么

| 能力 | 产物 | 说明 |
|---|---|---|
| 探测与内封字幕直出 | `probe/source.srt` | 软字幕直接抽（SRT/ASS/WebVTT），PGS/VobSub 位图字幕明确报 `unsupported` |
| 字幕矩阵 + 校验 | 单语 / 双语 SRT、ASS、VTT、TXT | CPS、行宽、行数校验；`report.json` 留痕 |
| 识别（ASR） | 词级时间戳、语义切条 | faster-whisper；中文默认 medium、英文 small；支持热词 |
| 翻译 | zh↔en | 免费微软 MT / DeepSeek LLM / 外部命令；术语表、上下文、译后精简与审校 |
| 配音 | `dub/dubbed.mp4` + 配音音轨 | Edge-TTS 免费音色；并发 + 重试 + 时长校验 + atempo；可保留原片 BGM；**Edge 失败可自动用本地 TTS 兜底/离线**（OPT-11） |
| 多人声 | 每个说话人不同音色 | sherpa-onnx 离线分离（2–8 人），音色可下拉/试听/按人重配 |
| 成片 | `burn/burned.mp4`、`final/marked.mp4` | 硬字幕烧录 + 自动核验；AIGC 显式标识 + 隐式元数据 |
| 全部串起来 | `pipeline.json` | `probe → asr → diarize → merge → [clean] → translate → build → dub → burn → mark`，阶段缓存 |
| 成本汇总 | `pipeline.json.cost` + `cost` 子命令 | 产物累计口径、多币种、离线可复算 |
| 本地 GUI | `http://127.0.0.1:7860/` | 批量、停止、预览、音色下拉/试听、多人声色回填与重配 |
| 任务历史 | `work/history.db` + `history` 子命令 | GUI 跑过的任务落 sqlite：关页面/重启可查，可载入参数、按原 argv 重跑、删记录 |

---

## 2. 30 秒上手（本机环境已就绪）

```powershell
# 一条命令：中文视频 → 英文字幕 + 英文配音 + 硬字幕成片 + AIGC 标识
python -m cuesmith pipeline --src D:\videos\a.mp4 --out work\run1 --src-lang zh --tgt-lang en

# 反向：英文视频 → 中文成片
python -m cuesmith pipeline --src D:\videos\b.mp4 --out work\run2 --src-lang en --tgt-lang zh

# GUI
python -m cuesmith gui
```

> `pipeline` 要求**源语言 ≠ 目标语言**（同语言没有翻译必要，会直接报错）。
> 只想识别中文、输出中文字幕/中文配音：分阶段跑
> `asr → merge → build`（或 `dub`），跳过 `translate`。

输出目录（`--out`）：

```
work/run1/
├─ probe/     manifest.json、source.srt
├─ asr/       source.srt、words.json、asr.json
├─ diarize/   diarize.json、speakers.json        # 开了多人声才有
├─ merge/     merged.srt、clean.srt              # clean.srt 为过滤后的字幕
├─ translate/ en.srt、translate.json
├─ matrix/    en.srt、bilingual.srt、*.ass、*.vtt、*.txt
├─ dub/       dubbed.mp4、dubbed.wav、dub_report.json
├─ burn/      burned.mp4、burn.json
├─ final/     marked.mp4                         # 打标后才有
└─ pipeline.json                                 # 阶段状态/耗时/产物/成本/告警
```

两个最容易踩的默认值：

- **内封字幕素材必须显式给源语言**（`--src-lang zh/en`）：字幕流本身没有可靠语言标记，
  `auto` 会明确报「内封字幕路线需要显式 --src-lang」；
- **中文源默认用 medium ASR**（本地有）；没有 medium 会退回 small，识别质量明显下降。

---

## 3. 从零安装（新机器）

### 3.1 前置

| 项 | 要求 | 本机实测 |
|---|---|---|
| 系统 | Windows 10/11 x64 | Windows 11 |
| Python | 3.11（3.10 也能跑部分 worker，但不保证） | 3.11.6 |
| ffmpeg | 在 `PATH`，带 libx264/aac | ffmpeg 9.0.2-full_build |
| 磁盘 | ≥ 4 GB（ASR 环境 + small 模型）/ ≈ 6 GB（含 medium）；本机全量（含 demucs/GUI）≈ 7.5 GB | 见[依赖说明](docs/依赖组件与付费项-说明.md) |
| GPU | 不需要（全部 CPU） | AMD 780M 核显，无 CUDA |

### 3.2 主包（零运行时依赖）

```powershell
git clone https://github.com/CytFree/cuesmith.git ; cd cuesmith
python -m cuesmith --help          # 直接可用：主包只用标准库 + ffmpeg
# 可选：安装成包（装完多一个 `cuesmith` 命令，等价于 python -m cuesmith）
python -m pip install -e .
python -m pip install -e ".[gui]"    # 带本地 Web GUI
python -m pip install -e ".[dev]"    # 开发：pytest / pytest-cov / ruff
```

> 主包不 import 任何模型库；ASR / TTS / 分离 / BGM / GUI 都在各自的 venv 里，
> 通过**子进程**调用（这也是 GPL/LGPL 组件与主包隔离的方式）。
>
> 也可以直接装打好的 wheel：`python -m pip install cuesmith-0.1.0-py3-none-any.whl`
> （wheel 里含全部 37 个模块与三个 worker 脚本，但**不含** `spike/` 下的模型与 venv）。

### 3.3 Worker 环境（按需装）

| 环境 | 用途 | 必需性 | 安装到 |
|---|---|---|---|
| ASR | faster-whisper | ASR 路线必需 | `spike/pyvideotrans/.venv`（现成）或自建 `spike/asr/.venv` + `--asr-python` |
| TTS | edge-tts | 配音必需 | `spike/.venv311`（现成） |
| 多人声 | sherpa-onnx | 可选 | `spike/diarization/.venv` |
| BGM 保留 | demucs | 可选 | `spike/demucs/.venv` |
| GUI | gradio | 可选 | `spike/gui/.venv` |

本机已有环境，无需重装。新机器命令（版本必须按此 pin，踩过的坑见
[依赖组件与付费项-说明.md](docs/依赖组件与付费项-说明.md#4-版本-pin-为什么不能随便升级)）：

```powershell
# TTS：edge-tts（免费）
python -m venv spike/.venv311
spike/.venv311/Scripts/python.exe -m pip install "edge-tts==7.3.1" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 多人声：sherpa-onnx
python -m venv spike/diarization/.venv
spike/diarization/.venv/Scripts/python.exe -m pip install "sherpa-onnx==2.0.0" "numpy==1.26.4" -i https://pypi.tuna.tsinghua.edu.cn/simple

# GUI：gradio
python -m venv spike/gui/.venv
spike/gui/.venv/Scripts/python.exe -m pip install "gradio==6.30.0" -i https://pypi.tuna.tsinghua.edu.cn/simple

# ASR：只装 faster-whisper 这三个（比沿用 pyVideoTrans 的 3.5GB 环境轻很多）
python -m venv spike/asr/.venv
spike/asr/.venv/Scripts/python.exe -m pip install "faster-whisper==1.2.1" "ctranslate2==4.5.0" "onnxruntime==1.20.1" `
    -i https://pypi.tuna.tsinghua.edu.cn/simple
# 之后用 --asr-python spike/asr/.venv/Scripts/python.exe（或设 CUESMITH_ASR_PYTHON）
```

BGM 保留（demucs + torch CPU）的安装命令见
[M6 实现说明 §6](docs/M6-BGM保留-人声分离-实现说明.md)。

### 3.4 模型

| 模型 | 大小 | 是否必需 | 自己下载 |
|---|---|---|---|
| faster-whisper small | 464 MB | ASR 英文默认 | 首次自动下载（HF 被墙时走 hf-mirror） |
| faster-whisper medium | 1.46 GB | 中文/日韩默认 | 手动放 `spike/whisper-models/faster-whisper-medium`，见 [M7 文档](docs/M7-zh-en-双向链路-真实素材验证-实现说明.md) |
| 说话人分离 3 个 ONNX | ≈ 84 MB | 多人声 | `spike/diarization/models/`，命令见 [M11 文档](docs/M11-多人声-说话人分离与多音色配音-实现说明.md) |
| silero VAD | 643 KB | 否（VAD 预过滤用，缺了自动跳过） | `spike/diarization/models/vad/silero_vad.onnx`，命令见 [OPT-6/7 说明](docs/OPT6-OPT7-VAD与声纹二次聚类-实现说明.md) |
| demucs 权重 | 80 MB | 保留 BGM | 首次运行自动下到 `spike/demucs/.torch` |

### 3.5 装完自检

```powershell
python -m pytest -q                          # 854 passed, 4 skipped
python -m pytest -q --cov=cuesmith --cov-fail-under=80   # 覆盖率基线 83%（需 pytest-cov）
python -m ruff check cuesmith tests                       # 0 告警（需 ruff）
python tools\run_samples.py --out work\regression   # 27/27
spike\gui\.venv\Scripts\python.exe tools\gui_smoke.py   # GUI HTTP 200
```

测试默认**不碰真实素材**（全用假引擎 / 6 秒彩条视频，跑得快且可重复）。真实素材的回归
按需开启，见 `tests/test_real_sample.py`：

```powershell
$env:CUESMITH_REAL_VIDEO = "D:\...\你的视频.mp4"     # 真视频探一次 + 二次命中缓存（约 2 秒）
$env:CUESMITH_REAL_OUT   = "work/cctv-demo"          # 已有产物树：报告数字 vs 盘上文件（约 1 秒）
$env:CUESMITH_REAL_E2E   = "1"                        # 全链路真跑（约 5 分钟，含真 ASR，离线）
python -m pytest tests/test_real_sample.py -q -m slow
```

---

## 4. 常用命令

```powershell
python -m cuesmith --help                    # 子命令总览

# 分阶段（调试/局部重跑）
python -m cuesmith probe     --src a.mp4 --out work\p --src-lang zh
python -m cuesmith asr       --src a.mp4 --out work\p --language zh
python -m cuesmith merge     --src work\p\asr\source.srt --out work\p
python -m cuesmith translate --src work\p\merge\merged.srt --out work\p --src-lang zh --tgt-lang en
python -m cuesmith build     --src work\p\merge\merged.srt --tgt work\p\translate\en.srt --out work\p
python -m cuesmith dub       --video a.mp4 --srt work\p\translate\en.srt --out work\p
python -m cuesmith burn      --video a.mp4 --subtitle work\p\matrix\en.srt --out work\p
python -m cuesmith mark      --src work\p\burn\burned.mp4 --out work\p

# 多人声（自动判人数：VAD 预过滤 + 声纹二次聚类，默认开）
python -m cuesmith diarize   --src a.mp4 --out work\p --srt work\p\merge\merged.srt `
    --python spike\diarization\.venv\Scripts\python.exe
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\p --speakers-json work\p\diarize\speakers.json `
    --speaker-voices "SPEAKER_00=en-US-GuyNeural,SPEAKER_01=en-US-AriaNeural"

# 已知人数最稳（显式 --speakers 时二次聚类自动让位）；
# 音色/音乐干扰大或想对比基线：--no-vad / --clustering sherpa / --merge-alpha 0.5
python -m cuesmith diarize --src a.mp4 --out work\p --speakers 4
python -m cuesmith diarize --src a.mp4 --out work\p --src-lang zh --no-vad

# 配音：默认 Edge-TTS（在线）；联网失败自动用本地 TTS 兜底（需本地模型）
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\p
# 完全离线：本地 Kokoro（默认音色按目标语言：zh→47 / en→3）
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\p --tts-provider local
# 指定本地模型/音色（多人声：SPEAKER_00=本地音色 spec）
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\p --tts-provider local `
    --local-tts-model spike\local-tts\vits-melo-tts-zh_en --speaker-voices "SPEAKER_00=0"
# 只关掉兜底（行为回到旧版）
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\p --tts-fallback off

# 全链路
python -m cuesmith pipeline --src a.mp4 --out work\run --src-lang zh --tgt-lang en `
    [--llm] [--keep-bgm] [--diarize] [--speakers 4] [--speaker-voices ...] [--no-vad] `
    [--clustering auto|sherpa|embedding] [--merge-alpha 0.55] `
    [--tts-provider edge|local] [--tts-fallback local|off] [--local-tts-voice 47] `
    [--force] [--regenerate-tts] [--glossary terms.json] [--no-burn-subtitles] [--no-mark]

# 成本
python -m cuesmith cost --work work\run             # 已有产物估算
python -m cuesmith cost --work work\run --json
python -m cuesmith cost --work work\run --pricing 我的价表.json

# GUI
python -m cuesmith gui [--port 7860] [--no-browser] [--allowed-path D:\out]

# 任务历史（GUI 跑过的记录都在 sqlite；可不开页面查/删）
python -m cuesmith history --limit 20
python -m cuesmith history --json
python -m cuesmith history --delete 7
```

关键默认值：

- 已存在产物默认复用（阶段缓存）；`--force` 强制全跑，`--regenerate-tts` 只重合成配音；
- 改 `--asr-model`、音色、语速、TTS 命令、说话人映射等会让对应阶段或缓存失效，其余阶段仍复用；
- 配音默认 **Edge-TTS（在线）**；Edge 失败（网络/端点）的条目会**逐条用本地 TTS 兜底**
  （`--tts-fallback local`，默认开；报告/告警会写明哪些条用了本地、音色是什么）；
  `--tts-provider local` 可完全离线（本地模型在 `spike/local-tts/`，约 1GB）；
- 翻译默认**复用 HTTP 连接**（OPT-10，TLS 只握手一次）：10min 字幕约 **5s**（原来 15s）；
  网络好/短字幕可 `--translate-concurrency 8`（实测再快 ~1.4×，约 3.4s）；大批量仍保守用默认 4；
- 退出码：`0` 成功、`1` 环境/前置失败、`2` 参数错误或校验不过（如 AIGC 回读核验失败）。

---

## 5. GUI 使用

```powershell
python -m cuesmith gui        # 自动重入 spike/gui/.venv，打开 http://127.0.0.1:7860/
```

- **视频路径**：一行一个（批量）；或用「上传视频」多选；
- **源语言 / 目标语言**：内封字幕素材请显式选 zh/en；
- **功能勾选**：生成配音成片 / 保留原片 BGM / 多人声分离 / 烧录硬字幕 / AIGC 标识 / LLM 翻译；
- **音色**：按目标语言筛好的下拉，第一项「默认（按目标语言）」；点「试听」真实合成一句样音；
- **多人声**：
  - 勾「多人声分离」；已知人数就在「说话人数」填 3/4（比自动判断稳）；
  - 「多人声音色」留空 = 内置默认池（中/英各 4 个音色，按出场顺序一人一个）；
    也可填 `SPEAKER_00=音色,SPEAKER_01=音色`，或只写音色池 `音色1,音色2`；
  - 跑完「说话人 → 音色」表自动回填，可改「音色」列后点**重配音色**（只重跑配音/烧字）；
- **高级**：ASR 模型（中文建议 medium）、TTS Python、术语表、强制重跑、重新合成配音、API Key；
- 批量逐条跑，每条独立输出目录（`01-文件名` / `02-文件名`）；「停止」会杀掉整棵子进程树；
- **任务历史**（sqlite `work/history.db`）：关页面/重启后仍可查；每行含源/输出/参数/状态/人数/成本/用时；
  可「载入参数到表单」改完再跑，或「重跑选中」按原 argv 重跑（新记录标 `replay_of`）；也可 `history` CLI 查/删；
  历史**不存 API Key**（重跑 LLM 需重填）；关页面会终止正在跑的任务（不是常驻队列）；
- API Key 只注入本次子进程环境，不落盘。

---

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `找不到装有 edge-tts 的 Python` | 用 `--tts-python spike\.venv311\Scripts\python.exe`，或设 `CUESMITH_TTS_PYTHON` |
| `找不到装有 faster-whisper 的 Python` | `--asr-python` 指向装了 faster-whisper 的解释器 |
| `没找到说话人分割模型` | 按 [M11 §3](docs/M11-多人声-说话人分离与多音色配音-实现说明.md) 下载到 `spike/diarization/models` |
| `找不到可用的中文字体` | `mark` 默认只找 Windows 字体（雅黑/黑体/宋体）与 Linux 的 Noto CJK；macOS 或自定义字体请用 `--font-file` 指向一个 `.ttf/.ttc` |
| 内封字幕 + `src-lang auto` 失败 | 显式 `--src-lang zh`（字幕流无法判断语言），GUI 源语言同样要选 |
| 翻译报 `LLM 返回不是 JSON 数组` 或输出 `Mi mi mi…` | 音乐/噪声字幕；M15 会自动过滤并回退免费 MT，重跑即可；仍失败看 `warnings` |
| 免费 MT 报 `SSL: UNEXPECTED_EOF...` | 网络抖动；重试，或改用 `--llm`（DeepSeek） |
| 配音 `0/15` 或部分失败 | 看 `dub/dub_report.json` 的失败条目；多半是 TTS 解释器/音色名不对 |
| Edge-TTS 不可用（断网/端点抖动） | 默认会自动用本地 TTS 兜底（`--tts-fallback local`，告警里写明；需 `spike/local-tts/` 模型）；想彻底离线用 `--tts-provider local`；关掉兜底 `--tts-fallback off` |
| 改了参数但结果没变 | 阶段缓存生效；确认对应阶段是否 `cached`，或加 `--force` |
| GUI 端口占用 | `python -m cuesmith gui --port 7861` |
| 长视频很慢 | ASR/BGM 是 CPU 大头；阶段缓存可避免重复跑；先用短素材调参 |

---

## 7. 费用（一句话版）

**默认完全免费**：本地 ASR/分离/BGM + Edge-TTS + 微软免费翻译，只花 CPU 时间。
只有两项可选付费：**DeepSeek LLM 翻译**（实测 ¥0.006–0.017/条）和**阿里云百炼声音复刻**
（$0.0039/274 字符）。价表、计费口径、如何完全关掉付费项，见
[依赖组件与付费项-说明.md](docs/依赖组件与付费项-说明.md)。

---

## 8. 目录结构

```
cuesmith/            主包（标准库 + ffmpeg，零运行时依赖）
  cli.py pipeline.py asr.py translate.py dub.py burn.py mark.py ...
  gui.py voices.py cost.py clean.py history.py localtts.py tts_worker.py
spike/                worker 环境与模型（不进仓库分发）
  .venv311/           edge-tts
  pyvideotrans/.venv/ faster-whisper（现成）
  diarization/.venv/  sherpa-onnx + models/（本地 TTS 也复用这个 venv）
  diarization/models/ pyannote 分割 + 中/英声纹 + silero VAD
  local-tts/          本地 TTS 模型（Kokoro 383MB / MeloTTS 182MB / 可选 Piper）
  demucs/.venv/       demucs + .torch/
  gui/.venv/          gradio
  whisper-models/     faster-whisper-medium
work/history.db       GUI 任务历史（sqlite，本地；可用 history 子命令查/删）
tools/                回归、冒烟、云 TTS/复刻、GUI 历史 e2e、本地 TTS 基准
tests/                单测（`python -m pytest`；真实素材用例按需开启，见 3.5）
docs/                 除本 README 外的全部文档：需求、评审、里程碑实现说明与实测记录
```

---

## 9. 文档索引

除本 README 外，所有文档都在 `docs/` 下（`git mv` 搬的，历史可追）。

| 文档 | 内容 |
|---|---|
| [代码规范](docs/代码规范.md) | 包结构与分层、编排骨架、错误/缓存/CLI/测试/提交约定（人和 AI 写代码前必读；入口 [AGENTS.md](AGENTS.md)） |
| [视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md](docs/视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md) | 需求、AC-1…AC-39、里程碑、风险、不做清单 |
| [依赖组件与付费项-说明.md](docs/依赖组件与付费项-说明.md) | 组件/版本/许可/磁盘、模型下载、付费项与价表 |
| [MVP-交付清单.md](docs/MVP-交付清单.md) | 交付物、验收步骤、质量基线、已知限制 |
| [OPT-1/OPT-2 优化说明](docs/OPT1-OPT2-识别人数与配音溢出-优化说明.md) | 英文自动人数（短簇合并+语言阈值）、配音溢出（提语速+atempo） |
| [OPT-3/4/5 优化说明](docs/OPT3-OPT4-OPT5-渠道回退与并发-实现说明.md) | 翻译渠道回退、音色下线降级、翻译/TTS 并发、二次补跑、ASR 与 diarize 并行 |
| [OPT-6/OPT-7 优化说明](docs/OPT6-OPT7-VAD与声纹二次聚类-实现说明.md) | VAD 预过滤（拼接语音再聚类）、声纹 embedding 二次聚类（自动判人数） |
| [OPT-8 优化说明](docs/OPT8-GUI任务历史-实现说明.md) | GUI 任务历史（sqlite 持久化、载入参数、重跑、history CLI） |
| [OPT-9 评估](docs/OPT9-更大声纹模型评估-2026-11-03.md) | 更大英文声纹模型值不值得换（实测 + GPU 提速估算；结论：暂不默认接入） |
| [OPT-10 优化说明](docs/OPT10-翻译提速-HTTP连接复用-实现说明.md) | 翻译提速：HTTP 连接复用，10min 字幕 15.3s→5.7s（并发 8 时 3.4s） |
| [OPT-11 实现说明](docs/OPT11-本地TTS兜底-实现说明.md) | 本地 TTS 接入（Kokoro/MeloTTS）+ 联网失败自动兜底；默认仍 Edge |
| [本地 TTS 评估](docs/本地TTS模型对比评估-2026-11-03.md) | Piper / Kokoro-82M / MeloTTS 实测（速度、CER/WER、多音色、集成方式；待决策） |
| [GPU 显存与提速估算](docs/GPU显存与提速估算-2026-11-03.md) | 上 GPU 各阶段显存估算、整链峰值与显卡档位建议（6–8GB 够用 / 12GB 舒适） |
| [M13 实现说明](docs/M13-开源GUI与成本汇总-实现说明.md) | GUI 薄壳与成本汇总 |
| [M14 实现说明](docs/M14-GUI-批量取消与预览-实现说明.md) | 批量/停止/预览、配音解释器自动探测 |
| [M15 实现说明](docs/M15-音乐噪声字幕过滤-实现说明.md) | 音乐/噪声字幕过滤与翻译兜底 |
| [M16 实现说明](docs/M16-GUI音色选择与试听-实现说明.md) | 音色目录/试听、多人声色重配、真实 GUI 测试 |
| [真实素材验证记录](docs/真实素材验证-3人-长视频-批量-2026-11-02.md) | 中文 3 人 / 英文 10min+ / GUI 批量+多人声结果与两个 bug 修复 |
| [待办 · 优化项](docs/待办-优化项.md) | 真实素材限制的优化方案（P0–P3、改动范围、验收标准） |
| M1–M12 | 分阶段实现细节与实测数据（见 `docs/M*.md`） |

---

## 10. 边界（避免误解）

- **不自建模型**：ASR / MT / TTS / 说话人分离 / BGM 分离都是外部模型或服务，本项目只做编排与工程化；
- **CPU only**：无 CUDA；长视频、medium ASR、demucs 是耗时大头；
- **只处理自有/已授权素材**；不做链接下载、硬字幕 OCR、唇形同步、本地声音克隆（理由见需求文档 §9）；
- **AIGC 合规**：默认在成片上打显式标识 + 写隐式元数据并回读核验；发布前按平台要求确认文案与位置；
- **语言**：目前只支持 zh↔en 双向（`LANG_PROFILES` 仅 zh/en）。

---

## 11. 许可与合规

- 本仓库代码：[MIT](LICENSE)。
- **外部组件各自许可**：ffmpeg（主包只以子进程调用，未链接其库）、ASR/TTS/分离/BGM
  的模型与服务各有自己的条款，见 [依赖组件与付费项](docs/依赖组件与付费项-说明.md)。
  主包刻意不 import 任何模型库，就是为了把 GPL/LGPL 组件隔离在进程边界之外。
- 素材：只处理自有或已授权素材；AIGC 标识的文案与位置仍需按发布平台的要求确认。

贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，编码约定见 [docs/代码规范.md](docs/代码规范.md)。





