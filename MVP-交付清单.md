# MVP 交付清单与验收

> 对应需求：[视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md](视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md)（M0–M22，AC-1…AC-41）。
> 本文件是「交付时该有什么、怎么验、还有什么没做」的一页纸。
>
> 交付状态：**可交付（MVP）**。已知限制见 §5，未阻塞项见 §6。

---

## 1. 交付范围

| # | 能力 | 验收对应 | 状态 |
|---|---|---|---|
| 1 | 探测与内封字幕直出（含 PGS/VobSub 明确 unsupported、forced 轨、无字幕流） | FR-1 / AC-1、AC-2、AC-3 | ✅ |
| 2 | 字幕文件矩阵 + CPS/行宽/行数校验（5 格式 + report） | FR-2 / AC-4、AC-5、AC-6 | ✅ |
| 3 | ASR（词级时间戳、热词、语义切条、句级合并） | FR-5 / AC-15、AC-17、AC-24 | ✅ |
| 4 | 翻译（zh↔en；免费 MT / DeepSeek / 外部命令；术语表、上下文、精简、审校、回退） | FR-5 / AC-16、AC-18…AC-22、AC-34 | ✅ |
| 5 | 配音编排（并发/重试/时长/atempo、TTS 可插拔、内容指纹缓存、**本地 TTS 离线/联网失败兜底**） | FR-3 / AC-7、AC-8、AC-9、AC-13、AC-41 | ✅ |
| 6 | 保留原片 BGM（demucs 分离 + 44.1k 混音，四种模式） | FR-3 / AC-21、AC-25 | ✅ |
| 7 | 多人声：说话人分离 + 多音色（2–8 人，**自动判人数默认开启**：VAD 预过滤 + 声纹二次聚类，GUI 可重配，云端复刻插槽） | M11/M12/M16/M19 / AC-28、AC-29、AC-35、AC-38 | ✅ |
| 8 | 硬字幕成片 + AIGC 标识（显式+隐式+回读核验） | FR-4 / AC-10、AC-11、AC-12、AC-26、AC-27 | ✅ |
| 9 | pipeline 一条命令 + 阶段缓存 + 成本汇总 + GUI（批量/停止/预览/音色/试听/任务历史） | AC-23、AC-30…AC-33、AC-39 | ✅ |

---

## 2. 交付物清单

### 2.1 代码与入口

| 交付物 | 路径 | 说明 |
|---|---|---|
| 主包 | `submatrix/` | CLI + 编排；零运行时依赖 |
| CLI 入口 | `python -m submatrix` | `build/probe/asr/translate/merge/dub/burn/mark/diarize/pipeline/cost/gui/history` |
| GUI | `python -m submatrix gui` | Gradio 薄壳，默认 `127.0.0.1:7860`；含「任务历史」页 |
| 任务历史库 | `work/history.db`（sqlite）+ `python -m submatrix history` | 关页面/重启可查、可载入参数/重跑/删记录 |
| worker 脚本 | `submatrix/asr_worker.py`、`diarize_worker.py`、`tts_worker.py` | 只跑在各自 venv |
| 本地 TTS 引擎 | `submatrix/localtts.py`（主包侧纯标准库）+ `spike/local-tts/` 模型 | 离线/联网失败兜底；默认仍 Edge |
| 工具 | `tools/run_samples.py`（27 项回归）、`tools/gui_smoke.py`（GUI 冒烟）、`tools/gui_history_real.py`（任务历史真实 HTTP 验证）、`tools/bench_local_tts.py`（本地 TTS 横评）、`tools/tts_dashscope.py`、`tools/clone_voice.py`、`tools/voice_similarity.py`、`tools/validate_diarization_accuracy.py` | 验收/云端扩展 |
| 测试 | `tests/` | 751 个单测 |

### 2.2 环境与模型（本机已就绪，不进仓库）

| 交付物 | 路径 | 备注 |
|---|---|---|
| ASR 环境 | `spike/pyvideotrans/.venv` | faster-whisper 1.2.1 / ctranslate2 4.5.0 / onnxruntime 1.20.1 |
| TTS 环境 | `spike/.venv311` | edge-tts 7.3.1 |
| 说话人分离环境 | `spike/diarization/.venv` | sherpa-onnx 2.0.0 + 模型 84 MB + silero VAD 643KB（可选） |
| BGM 环境 | `spike/demucs/.venv` | demucs 4.1.0 + torch 2.2.2+cpu + 权重 80 MB |
| GUI 环境 | `spike/gui/.venv` | gradio 6.30.0 |
| Whisper 模型 | `spike/whisper-models/` | small 464 MB、medium 1.46 GB |
| 版本/许可/付费 | [依赖组件与付费项-说明.md](依赖组件与付费项-说明.md) | 必读 |

### 2.3 文档

| 文档 | 用途 |
|---|---|
| [README.md](README.md) | 快速开始、安装、命令速查、FAQ |
| [依赖组件与付费项-说明.md](依赖组件与付费项-说明.md) | 组件/版本/许可/磁盘/付费项/环境变量 |
| [视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md](视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md) | 需求与 AC、里程碑、风险、不做清单 |
| [真实素材验证-3人-长视频-批量-2026-11-02.md](真实素材验证-3人-长视频-批量-2026-11-02.md) | 三类真实素材组合的验证结果与两个 bug 修复 |
| `M0–M22-*.md` | 逐里程碑实现说明与真实实测数据（踩坑可追溯） |

---

## 3. 验收步骤（约 5 分钟）

### 3.1 环境自检

```powershell
python -m submatrix --help                                  # 无报错，列出子命令
python -m pytest -q                                         # 期望：751 passed
python tools\run_samples.py --out work\acceptance           # 期望：27/27 通过（约 90s）
```

### 3.2 单条真实素材（zh→en）

```powershell
python -m submatrix pipeline --src spike\samples\S1_softsub.mkv --out work\acceptance\one `
    --src-lang zh --tgt-lang en
```

期望：状态 `ok`；`work/acceptance/one/` 下有 `matrix/en.srt`、`dub/dubbed.mp4`、
`burn/burned.mp4`、`final/marked.mp4`、`pipeline.json`；`pipeline.json.status=ok`。
（S1 是内封中文字幕，所以 `asr` 会 `skipped`，验收更快。）

### 3.3 分阶段缓存

同一条命令再跑一次：日志应显示 `probe/asr/.../mark` 多为 `cached`，总耗时应明显小于首次。

### 3.4 多人声（可选）

```powershell
python -m submatrix pipeline --src work\multi-speaker\demo2.mp4 --out work\acceptance\multi `
    --src-lang zh --tgt-lang en --diarize --speakers 2 --asr-model medium
```

期望：`pipeline.json` 的 `diarize.summary.speakers=2`、`speaker_voices` 两个不同音色；
`dub_report.json` 的 `summary.voices` 按说话人分开。

### 3.5 GUI（可选）

```powershell
python -m submatrix gui
```

期望：浏览器打开 `http://127.0.0.1:7860/`；选音色点「试听」有声音；
勾「多人声分离」跑一条双人素材后，「说话人 → 音色」表出现两行；改音色点「重配音色」，
日志前 6 阶段为 `cached`、只重跑 dub/burn。

### 3.6 成本汇总

```powershell
python -m submatrix cost --work work\acceptance\one
```

期望：打印分项与合计；免费路径（未开 `--llm`、未用云端 TTS）金额为 ¥0。

---

## 4. 质量基线（截至 2026-11-03）

| 项 | 结果 |
|---|---|
| 单测 | **751 passed** |
| 一键回归 | **27/27，38.6s**（`work/regression/regression.json`） |
| GUI 冒烟 | gradio 6.30.0 HTTP 200（51 blocks / 5 handlers） |
| zh→en 真实素材 | 154s 央视科普：ok，成本 ¥0.0134–0.0165（LLM） |
| en→zh 真实素材 | 2 分钟英文播客：ASR 37.6s（3.19×实时）、翻译 0 回退 |
| GUI 双人多人声 | 端到端 116.8s，2 音色（Guy/Aria），dub 3/3，成本 ¥0 |
| GUI 重配音色 | 35.4s，前 6 阶段 cached，配音音轨 sha 变化 |
| 4 人端到端 | 4 人/10 段，4 音色（Guy/Aria/Jenny/Andrew）一人一个，dub 8/8，成本 ¥0.0065 |
| 批量 | S1 内封 + S2 硬字幕 2/2，259.0s，¥0 |
| **中文 3 人真实对话** | 4min 真实视频，3 人/3 音色，dub 19/19 人工 0，总 532.6s，¥0（[验证记录](真实素材验证-3人-长视频-批量-2026-11-02.md)） |
| **英文 10min+ 长视频** | 10min 3 人播客，ASR 205s + 分离 85s + 翻译 323s + 配音 239s，dub 86/86，总 651.8s，¥0 |
| **GUI 批量 + 多人声** | 两条中文片段 2/2，326.7s，¥0；一条 3 人、一条单说话人回退，目录/表格/成本各自独立 |
| **自动判人数（M19）** | VAD 预过滤 + 声纹二次聚类默认开，6 个真实样本人数全中：英文 10min auto **4→3**（140 条字幕全归属）、中文 4 人 **5→4**、中文 3 人 **4→3**（簇结构 53.68/142.58/17.24s 与人工验收一致）、新闻 3 人 **6→4**、双人 demo 2、单人口播 60s **1**；en 10min diarize 100.8s（+13%） |
| **端到端（M19 默认参数）** | zh 60s 素材 `pipeline --diarize`：ASR 128.0s（与 diarize 并行、diarize 阶段 0.0s）、3 人/3 音色、translate 23.1s、dub 6/6、burn/mark ✅、¥0，重跑 diarize cached |
| **GUI 任务历史（M20）** | 真实 HTTP（gradio 6.30.0）+ gradio_client：跑 S1 素材 → 记录 `完成/¥0/2.2s`；「重跑」`#7 replay_of=6`；「刷新」7 行/7 选项；服务器重启前后记录都在；停服务器后 CLI `history --limit 10` 仍列出 7 条（[说明](OPT8-GUI任务历史-实现说明.md)） |
| **翻译提速（M21）** | HTTP keep-alive：10min 140 cue translate **15.30s → 5.70s（2.68×）**，`--translate-concurrency 8` 时 **3.42s（4.47×）**；译文逐字节一致、连跑 3 轮稳定；pipeline 里 60s 素材 **23.1s → 3.4s**、10min **19.0s → 5.1s**（[说明](OPT10-翻译提速-HTTP连接复用-实现说明.md)） |
| **本地 TTS 兜底（M22）** | 默认仍 Edge；60s 素材 26 条 `--tts-provider local`：dub **37.44s、26/26、成片 60.0s、¥0**；模拟断网（Edge 缺依赖）**26 条全部本地兜底、状态 ok、71.44s、成片 60s**；MeloTTS 44.1kHz 端到端 10/10；pipeline 复用缓存只重跑 dub 27.3s、同配置重跑 cached（[说明](OPT11-本地TTS兜底-实现说明.md)） |
| 本次新修 bug | 英文声纹模型截断（21.9→38.4MB）；diarize 缓存忽略人数/阈值/语言（已修 + 回归）；VAD 缺模型/二次聚类失败自动降级并告警；本地 TTS 缓存按引擎隔离（Edge mp3 / 本地 wav） |

复跑证据：`work/gui/m16-real-demo2-*/`、`work/m16-real/four-speaker-run/`、
`work/gui/m16-real-batch-*/`、`work/multi-speaker/`、`work/real-samples/`（真实素材验证）、
`work/gui-opt8-e2e/report.json` + `work/history.db`（任务历史）、`work/opt10-net/`（翻译提速实验）、
`work/opt11-local-dub/`、`work/opt11-fallback-dub/`、`work/local-tts-eval/worker-smoke/`（本地 TTS）。

---

## 5. 已知限制（交付时需说明）

| 限制 | 影响 | 缓解 |
|---|---|---|
| CPU only、无 CUDA | 长视频慢（ASR ≈2×实时，demucs 单条 2–4 分钟） | 阶段缓存；短素材调参；有 GPU 时换底座 |
| 语言只有 zh↔en | 其他语言不支持 | 需求范围（§9） |
| 内封字幕必须显式源语言 | 素材已知语言，填一下即可 | GUI 标签已提示 |
| 自动说话人人数不总准 | 已默认启用 VAD 预过滤 + 声纹二次聚类，6 个真实样本人数全中；但 α 只在 6 个样本上校准，新闻类 3 人可能判 4（在 3±1 内） | 已知人数就填 `--speakers N`（最稳）；或 `--merge-alpha 0.5` 更激进合并、`--no-vad` 走原始音频对比 |
| 短片段可能判成单人 | 60s 片段只识别 1 人时回退单音色（有告警） | 用更长片段，或显式 `--speakers` |
| 英→中配音溢出偏多 | 10min 素材 86 条中 24 条溢出>10%、10 条转人工（失败 0） | 调语速/精简策略，或人工抽检 |
| 默认音色≠原人物音色 | 听感是"换了配音"，不是原声克隆 | 多人声默认只保证"不同"；要像本人需云端复刻（付费） |
| Edge-TTS 依赖微软在线服务 | 断网/音色下线会失败 | 默认已加**本地 TTS 兜底**（`--tts-fallback local`，逐条兜底+告警）；可 `--tts-provider local` 完全离线 |
| 本地 TTS 质量/音色不如 Edge | 可懂度够但自然度/音色数量有差距；兜底时统一一个本地音色（丢多人声） | 默认仍用 Edge；本地只作离线/兜底；多人声本地配音需显式 `--speaker-voices SPEAKER_00=50,...` |
| 微软免费 MT 无 SLA | 偶发限流/SSL 抖动 | 重试；重要素材开 `--llm` |
| 音乐/噪声不能 100% 过滤 | 极少数噪声仍可能进字幕 | M15 规则 + 翻译兜底；人工抽检 `clean.srt` |
| PGS/VobSub 位图字幕不支持 | 会明确报 `unsupported` | 需 OCR（明确不做） |
| GUI 任务历史是「记录 + 重跑」，不是常驻队列 | 关闭页面仍会终止正在跑的任务；历史不存 API Key（重跑 LLM 需重填）；库不自动清理 | 历史记录保留、可载入参数/重跑/删除；要「关页面继续跑」需常驻调度进程（未做） |
| 批量 + 多人声 + 长视频组合 | 已真实跑通三类组合（见 §4 与[验证记录](真实素材验证-3人-长视频-批量-2026-11-02.md)）；更大规模（10 条以上/30min+）未压测 | 按需抽样压测 |
| 仓库未声明 LICENSE | 对外分发需先定许可 | 内部使用无碍 |

---

## 6. 未阻塞的后续项

| 项 | 状态 |
|---|---|
| 火山引擎 A/B（对比阿里云） | 用户已定：打磨后再接 |
| 云端克隆供应商最终选定 | 阿里云百炼已可用；火山/MiniMax/Fish 未接 |
| GUI 任务历史/持久化队列 | 历史已做（OPT-8 ✅ 2026-11-03，[说明](OPT8-GUI任务历史-实现说明.md)）；"关闭页面继续跑"的常驻队列未做 |
| GUI 暴露分离阈值控件 | 目前走「额外参数」`--diarization-threshold` |
| 组合场景真实验证（3 人真实素材 / 10min+ 长视频 / 多人声批量） | ✅ 2026-11-02 完成（[验证记录](真实素材验证-3人-长视频-批量-2026-11-02.md)） |
| 更大规模压测（10 条以上批量 / 30min+ 长视频） | 未做，按需抽样 |
| 真实素材限制的优化（短簇合并、语速自适应、并发等） | **OPT-1…OPT-11 已收口**：P0/P1/P2 + OPT-8/10/11 完成，OPT-9 已评估待 GPU（[待办-优化项.md](待办-优化项.md)） |
| 本地 TTS 的多人声自动音色池 | 未做：本地多人声需显式 `--speaker-voices SPEAKER_00=50,...`；兜底时统一单音色（已告警） |
| 声音克隆的本地方案 | 明确不做（无 GPU） |

---

## 7. 交付前检查清单（打勾用）

- [ ] `python -m submatrix --help` 正常
- [ ] `python -m pytest -q` → 751 passed
- [ ] `python tools\run_samples.py --out work\acceptance` → 27/27
- [ ] 一条 zh→en 真实素材产出 `burned.mp4` + 字幕 + `pipeline.json(status=ok)`
- [ ] 同目录重跑命中缓存（总耗时明显下降）
- [ ] GUI 打开、试听有声音、批量结果表有行
- [ ] 多人声素材说话人表人数与预期一致、音色互不相同；自动判人数与 `--speakers N` 结果一致（默认 VAD+二次聚类已开，可用 `--no-vad`/`--clustering sherpa` 对比）
- [ ] GUI 跑一条后「任务历史」出现记录；重启 GUI/重开页面仍在；「重跑选中」生成带 `replay_of` 的新记录；`python -m submatrix history --limit 5` 能列出
- [ ] 断网/Edge 不可用时配音仍能出片：`dub --tts-provider local` 或默认兜底跑通（报告 `summary.tts_fallback` / `local_tts` 有记录）
- [ ] 成本汇总金额与是否开了付费项一致（免费路径 ¥0）
- [ ] 发布素材已按平台要求确认 AIGC 标识
- [ ] API Key 未写进任何文件/日志（只走环境变量或 GUI 临时注入）





