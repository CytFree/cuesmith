# OPT-11 实现说明：本地 TTS 接入（可配置，默认仍 Edge；联网失败自动本地兜底）

> 完成日期：2026-11-03。测试 **751 passed**、回归 **27/27**、GUI 冒烟 HTTP 200。
> 需求：**接入本地 TTS、可配置、默认保持 Edge；联网失败时可用本地 TTS 兜底。**

---

## 1. 行为（一句话）

| 配置 | 行为 |
|---|---|
| 默认（不传任何新参数） | **Edge-TTS 主渠道**；网络/端点失败（重试 + 二次补跑 + 音色降级都救不回）的条目**逐条改用本地 TTS 兜底**，报告/告警如实记录 |
| `--tts-provider local` | **完全离线**：本地 sherpa-onnx 直接合成（Kokoro 或 MeloTTS），不碰网络 |
| `--tts-fallback off` | 关闭本地兜底，失败条目按失败处理（行为等同 OPT-11 之前） |
| `--tts-command ...` | 外部命令引擎（旧行为）；与 `--tts-provider local` 互斥 |

---

## 2. 架构（遵守"主包零依赖、模型只在进程边界"）

| 文件 | 角色 |
|---|---|
| `cuesmith/tts_worker.py`（新） | **常驻 worker**，唯一 `import sherpa_onnx` 的地方；stdin/stdout 逐行 JSON；启动加载模型（Kokoro / MeloTTS-VITS），支持逐条 `{text,out,voice,speed}`；单条失败不崩、EOF 退出 |
| `cuesmith/localtts.py`（新，纯标准库） | 主包侧引擎：找 sherpa 解释器/模型、判断类型（有 `voices.bin`→kokoro，有 `lexicon.txt`+`dict/`→vits）、`LocalTtsEngine.synthesize(text,out)`；**共享 worker registry**（同一模型只加载一次）、线程锁串行化、ready/响应超时、worker 死亡自动重启一次、`close_all_workers()` |
| `cuesmith/dub.py` | 新增 `tts_provider` / `tts_fallback` / `local_tts_*`；`build_engine` 按 provider 构造引擎；Edge 失败后逐条本地兜底；缓存指纹带 engine（Edge 的 mp3 与本地 wav 分开）；报告/告警/条目级 `provider`/`fallback` |
| `cuesmith/pipeline.py` | 透传配置；dub 缓存判据加入 `tts_provider` / `tts_fallback` / 本地模型指纹（换 provider 或换模型必重配，老 Edge 缓存仍兼容） |
| `cuesmith/cli.py` | `dub` / `pipeline` 新参数；摘要打印本地 provider/兜底条数 |

---

## 3. 配置项

`dub` 与 `pipeline` 同名（pipeline 透传给 dub）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--tts-provider edge\|local` | `edge` | 主渠道 |
| `--tts-fallback local\|off` | `local` | Edge 失败是否本地兜底 |
| `--local-tts-model PATH` | 自动找 | 默认候选 `spike/local-tts/kokoro-multi-lang-v1_0`、`spike/local-tts/vits-melo-tts-zh_en`；env `CUESMITH_LOCAL_TTS_MODEL` |
| `--local-tts-kind auto\|kokoro\|vits` | `auto` | 按目录特征自动判断 |
| `--local-tts-voice` | 空 | Kokoro：`47` / `kokoro:47` / 名字（xiaoxiao…）；VITS：`0`；空=按目标语言默认（zh→47、en→3） |
| `--local-tts-python` | 自动探测 | 装有 sherpa-onnx 的解释器（默认 `spike/diarization/.venv`）；env `CUESMITH_LOCAL_TTS_PYTHON` |
| `--local-tts-threads` | `8` | 推理线程 |
| `--local-tts-speed` | `1.0` | 基础倍速（0.5–2.0），再乘 `--tts-rate`（如 `+20%` → 1.2） |
| `--tgt-lang zh\|en` | `zh` | 仅 `dub` 单独用时用于挑本地默认音色；pipeline 自动用目标语言 |

多人声：**本地音色不需要 Edge 名字**，直接写本地 spec，例如
`--speaker-voices "SPEAKER_00=50,SPEAKER_01=47,SPEAKER_02=yunyang"`；
若没给本地映射，本地 provider 下所有说话人共用默认本地音色并**告警**（不假装多人声生效）。

---

## 4. 关键实现细节

1. **兜底顺序**（Edge 主渠道时）：首轮并发 → 失败条目降并发二次补跑 → 音色类错误降级默认音色 → **仍失败的条目逐条本地合成**；本地也失败才标 failed（错误里带两段原因）。
2. **缓存**：文件名指纹含 `engine|voice|rate|text`，本地是 `.wav`、Edge 是 `.mp3`，互不串用；`--regenerate-tts` 同样对本地生效；pipeline 缓存判据加入 provider/兜底/模型指纹。
3. **速度**：本地 `speed = (--local-tts-speed 或 1.0) × (1 + --tts-rate)`；OPT-2 的提语速档位（`+45/+55/+65%`）会自动变成 speed 1.45/1.55/1.65，**不需要 atempo**（比 Edge 的提语速更少 artifact）。
4. **worker 复用**：同一模型/解释器/类型/线程数共享一个常驻进程（首个请求含加载 3.7–6.1s，之后逐条 ~1–3s）；请求用锁串行化（sherpa 单实例非线程安全），dub 多线程调用安全；结束 `close_all_workers()`。
5. 采样率不同（Kokoro 24k / MeloTTS 44.1k）不特殊处理：现有 ffmpeg 解码/铺轨/混音流程会自动重采样。

---

## 5. 真实实测

### 5.1 worker 冒烟（sherpa venv，8 线程）

| 模型 | 加载 | 中文合成 | 英文合成 |
|---|---|---|---|
| Kokoro multi-lang（310.5MB，54 音色） | **3.75s** | 墙钟 3.06s / 音频 5.24s（≈1.7× 实时） | 1.73s / 3.93s（≈2.3× 实时） |
| MeloTTS zh+en（162.5MB，单女声） | **6.05s** | 2.02s / 4.71s（≈2.3× 实时） | 1.49s / 4.85s（≈3.3× 实时） |

样例：`work/local-tts-eval/worker-smoke/{kokoro_zh,kokoro_en,melo_zh,melo_en}.wav`（ffprobe 校验时长一致）。

### 5.2 端到端（60s 中文素材，英文译文 26 条 cue）

| 场景 | 结果 |
|---|---|
| **`--tts-provider local`** | dub **37.44s**、26/26 成功、人工 0、成片 60.0s；缓存 26 个 `.wav`、0 个 `.mp3`；报告 `provider=local`、`local_tts={kind:kokoro, voice:3, speed:1.0}`；**¥0** |
| **`--tts-fallback local`（把 Edge 的 python 指到没装 edge_tts 的环境模拟断网）** | 26 条全部本地兜底、状态 **ok**、dub 71.44s（含 26 条 Edge 重试耗时）、成片 60.0s；报告 `tts_fallback.events` 26 条（from edge → to local, voice 3）、每条 cue `provider=local/fallback=true`；告警"Edge-TTS 失败 26 条，已用本地 TTS 兜底…" |
| **MeloTTS 本地**（44.1kHz 模型，10 条中文 cue） | 10/10 成功、26.66s（含 6s 模型加载）、成片 60.0s（重采样正常） |
| **pipeline（复用缓存，`--tts-provider local`）** | 只重跑 dub/burn/mark：dub **27.3s**、总 31.5s、状态 ok、¥0；报告 `tts_rate=+20%` → 本地 `speed=1.2`、提语速 0 条（1.2 倍已放得下）、`escalated` 生效逻辑正常；**同配置重跑 dub 直接 cached 0.0s** |

### 5.3 测试

| 项 | 结果 |
|---|---|
| 全量单测 | **751 passed**（+37：`tests/test_localtts.py` 27、`tests/test_dub_local.py` 6、`tests/test_pipeline.py` 4） |
| 一键回归 | **27/27（40.5s）** |
| GUI 冒烟 | HTTP 200 |

---

## 6. 已知限制（诚实）

1. **本地不是"更快"**：单 worker 串行，60s 素材 26 条 ≈ 37s（≈1.4s/条）；10min 素材 86 条约 **2 分钟** CPU 时间——比 Edge 并发（好窗口 ~150s）并不明显快，价值在**离线/兜底**。
2. **兜底会"丢掉多人声"**：本地兜底统一用一个本地音色（不同说话人不再区分），报告和告警都写明；要多人声本地配音，请主渠道就用 `--tts-provider local` 并给 `--speaker-voices` 配本地 spec。
3. **本地音质差于 Edge**（Kokoro 中文 v1.0 有多音字发音问题、MeloTTS 单女声、Piper 需额外运行时），详见 [本地 TTS 评估](本地TTS模型对比评估-2026-11-03.md)；默认仍用 Edge 就是为质量。
4. 断网瞬间：每条先走完 Edge 重试（3 次 + 二次补跑一轮）才兜底，26 条约多花 34s；大素材断网时总耗时≈Edge 失败开销 + 本地合成。
5. Kokoro 对词典 OOV（如裸缩写 "GPU"）会跳过（worker stderr 警告），字幕译文一般没有裸缩写；遇到需文本预处理。
6. 模型约 **1GB**（Kokoro 383MB + MeloTTS 182MB + 可选 Piper），在 `spike/local-tts/`，不进分发。
7. 仅 CPU（sherpa-onnx 的 CPU wheel）；GPU 版 sherpa/ORT 需另装（见 [GPU 显存与提速估算](GPU显存与提速估算-2026-11-03.md)）。

---

## 7. 复跑与证据

```powershell
# 纯本地（离线）
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\out --tts-provider local
# 或 pipeline
python -m cuesmith pipeline --src a.mp4 --out work\run --src-lang zh --tgt-lang en --tts-provider local
# 默认 Edge + 本地兜底（不传参数即是）；可显式写
python -m cuesmith dub --video a.mp4 --srt en.srt --out work\out --tts-fallback local
# worker 单测
python -m pytest tests\test_localtts.py tests\test_dub_local.py -q
```

| 证据 | 内容 |
|---|---|
| `work/local-tts-eval/worker-smoke/report.json`、`engine-check.json` | worker/引擎真实冒烟（加载、RTF、音色解析、坏请求、close） |
| `work/opt11-local-dub/` | `--tts-provider local` 端到端（dub_report.json 含 provider/local_tts/26 wav） |
| `work/opt11-fallback-dub/` | Edge 不可用 → 26 条本地兜底（events 26、成片 60s） |
| `work/opt11-melo-dub/` | MeloTTS（44.1kHz）端到端 |
| `work/real-samples/opt7b-pipe-zh-60s/` | pipeline 本地 provider（dub 27.3s、重跑 cached） |
| `cuesmith/tts_worker.py`、`cuesmith/localtts.py`、`tests/test_localtts.py` | 实现与单测 |
