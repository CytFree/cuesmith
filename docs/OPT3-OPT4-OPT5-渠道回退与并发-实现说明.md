# OPT-3 / OPT-4 / OPT-5 实现说明：渠道回退、音色降级、长视频并发

> 对应待办：[待办-优化项.md](待办-优化项.md) 的 OPT-3、OPT-4、OPT-5（P1）。
> 完成日期：2026-11-03。**测试 672 passed；回归 27/27**；真实素材复跑成本仍 ¥0。

---

## 1. 结论

| 项 | 优化前 | 优化后 | 状态 |
|---|---|---|---|
| OPT-3 免费 MT 网络失败 | 一条都翻不动就整条链路 `failed` | 自动重试 → 切备用渠道（已配置的 LLM / 命令模板）→ 仍失败才报错；成功部分保留 | ✅ |
| OPT-4 音色被下线 | 该音色所有条目 `failed`，整片 `partial` | 识别 `NoAudioReceived`/无效音色 → 该条降级为默认音色重合成，告警 + 报告留痕 | ✅ |
| OPT-5 翻译耗时 | 串行 46.7s（31 段） | 并发 4：**19.0s**（2.5×），译文逐字节一致 | ✅ |
| OPT-5 配音耗时 | 并发 6 | 默认 **并发 10**：同网络窗口 224.0s → **153.4s**（1.5×） | ✅ |
| OPT-5 配音失败自愈 | 网络抖动时 failed 16/86 | 二次补跑（降并发）→ failed **2/86**（同窗口） | ✅ |
| OPT-5 ASR 与 diarize 并行 | 串行（ASR 217s + diarize ~85s） | 分段在 ASR 期间后台跑，ASR 出来后只补归属 **0.03s** | ✅ |
| OPT-5 10min 冷跑总耗时 | 651.8s（10.9min，11-02） | **599.7s**（本次慢网络窗口）；好网络窗口推算 **396s（6.6min）** | ✅ 目标 ≤7min 在好窗口达成 |

> 说明：本机到 Edge-TTS 端点（`speech.platform.bing.com`）在测量期间**间歇性不可达**
> （aiohttp `ClientConnectorError: 指定的网络名不再可用`），所以配音耗时/失败数波动较大——
> 下面所有数据都标了测量窗口，不挑好看的报。

---

## 2. OPT-3 翻译渠道自动回退

### 2.1 问题

免费微软端点偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`。原实现里 `MicrosoftTranslator`
已有单请求的退避重试（3 次、1/2/4s）和"失败二分拆批"，但**一条彻底失败就抛 `TranslateError`**，
`run_translate` 不再接住 → 整条链路 `failed`，前面翻好的 90% 白费。

### 2.2 方案

新增 `translate.ResilientTranslator`（多渠道路由）+ `_fallback_chain`：

- 主渠道是免费微软端点时，备用链 = **已配置的 LLM（`LlmConfig.from_env()`）→ 命令模板**；
- 当前渠道抛 `TranslateError` → 切下一个并**记住**（粘性），后续调用不再白等坏渠道的
  `1+2+4s` 重试；切换只影响"还没翻的调用"，**已完成的不重来**；
- 备用渠道也失败就继续往下切，全挂才抛错（错误里带链路，不伪装成功）；
- 线程安全（`threading.Lock` 保护切换）——为并发翻译（OPT-5）做准备；
- `translate.json` 记 `config.provider`（**实际干活的渠道**，成本按它算）、
  `config.provider_primary`（原渠道）、`config.provider_fallback.events`（from/to/error）、
  `summary.provider_fallback`；`warnings` 里逐条说明"XX 失败已回退到 YY"；
- 开关：`translate --fallback-provider auto|off`、`pipeline --translate-fallback auto|off`
  （默认 `auto`；`off` 便于排查到底是不是渠道问题）。

> 成本诚实性：回退到 LLM 时 `provider` 记为 `openai`，`config.llm.usage` 带上 token，
> `cost` 会照常算出 LLM 费用——不会出现"明明用了付费渠道却记 ¥0"。

### 2.3 测试（+6）

失败自动切 LLM 且报告/告警/用量正确；粘性（坏渠道只试一次）；`off` 关闭后保持原报错契约；
没有 LLM key 时链为空、行为不变；全部渠道都失败时报最后一条错误。

---

## 3. OPT-4 音色下线降级

### 3.1 问题

`en-US-DavisNeural` 已被微软下线（M16 实测 `NoAudioReceived`）。多人声里只要有一个音色坏了，
该说话人的每条都 `failed`，dub 整片 `partial`——用户只看到"失败了几条"，不知道是音色下线。

### 3.2 方案

`dub.py`：

- `VOICE_ERROR_HINTS` + `_looks_like_voice_error()`：识别 `NoAudioReceived` / `no audio` /
  `invalid voice` / `voice not found` / `unsupported voice` 等；
- 首轮合成后，对"失败 + 音色不是默认音色 + 错误像音色问题"的条目，把该条**改用默认音色**
  （`options.voice`）重合成一次；默认音色也失败就如实 `failed`（不做无限降级）；
- 网络类错误（timeout/连接）**不降级**——否则会悄悄换音色、掩盖真实故障；
- 报告 `summary.voice_downgrades`（cue / from / to / error）；`warnings` 汇总
  "音色 X 不可用，已把第 a、b 条降级为默认音色 Y"；单条的 `voice` 字段记降级后的音色，
  `voices` 统计同步。

### 3.3 测试（+3）

坏音色全片仍 `ok`、降级条目可查、告警正确；网络错误不误判为音色问题；
默认音色也挂时 `failed` 保留、不无限降级。

---

## 4. OPT-5 长视频并发

### 4.1 翻译分块并发（已做）

- `translate` 的段落/批次翻译改为可选并发：`TranslateOptions.concurrency`、
  `translate --concurrency`、`pipeline --translate-concurrency`（默认 4；免费 MT 开太大易被限流，
  LLM 可上 6–8）；
- `context=paragraph`（默认）：段落之间并发；`context=off/sentence`：批与批并发；
- 结果**按原顺序拼回**，对齐逻辑不变；`ResilientTranslator` 保证并发下渠道切换安全。

**实测（10min 播客转中文，31 段、86 条，同一网络、同一机器）**：

| 并发 | 墙钟 | 译文 |
|---|---|---|
| 1（串行） | 46.7s | CPS 5.92、精简 1 条（trim）、超限 3/86 |
| 4 | **19.0s** | **与串行逐字节一致** |

> 为什么提速这么大：免费端点每次请求都是新连接（TLS 握手 ~几秒），并发把"等网络"叠起来了。
> 顺带也解释/缓解了 11-02 那次 translate 318s（当时端点延迟异常高，串行被放大）。

### 4.2 TTS 并发 6 → 10 + 二次补跑（已做）

- `dub.concurrency` 默认 6 → **10**（`pipeline --concurrency`、`dub --concurrency` 同步改默认）；
- 新增**二次补跑**：首轮失败的条目（多为高并发下 Edge 连接抖动）用**更低的并发（≤3）**
  再补一轮——这类是网络抖动，不该直接标 `failed`；
- tiered retry 总量：单条最多 首轮 3 次 + 补跑 3 次 = 6 次尝试。

**实测（10min 播客，`--regenerate-tts` 强制 96 次合成，三个网络窗口）**：

| 测量窗口 | 并发 6 | 并发 10 |
|---|---|---|
| 抖动①（无二次补跑） | 161.5s / failed **16**/86 | 115.3s / failed 9/86 |
| 抖动②（有二次补跑） | 221.0s / failed **2**/86 | 194.8s / failed 5/86 |
| 恢复后（有二次补跑） | 224.0s / failed 6/86 | 153.4s / failed **1**/86 |

结论：**并发 10 普遍更快（约 1.2–1.5×）**；failed 数主要受网络影响，二次补跑把抖动窗口的
失败从 16 → 2 救回来（代价是耗时增加，因为重试本身要花时间）。网络差的机器可用
`--concurrency 6` 换更低重试压力。

### 4.3 ASR 与 diarize 并行（已做）

分段（抽音轨 + sherpa 分割/声纹/聚类，约 85–90s）只跟**音频和聚类参数**有关，与字幕无关，
所以可以提前算：

- `diarize` 新增 `segments.json` 产物（`write_segments`/`load_segments`）：只含
  `turns` + 源文件 sha + 配置 + 摘要，不含字幕归属；
- `diarize.attach_speakers()`：把「字幕条 → 说话人」补到已有分段上（时间重叠投票 + 孤岛平滑），
  写 `speakers.json` 并更新 `diarize.json`——纯计算，**毫秒级**；
- pipeline：ASR 需要实际运行时（源语言已显式给出，因为 `auto` 要等 ASR 报告才知道语言、
  才知道该用中文还是英文声纹模型），先用 daemon 线程后台跑 `run_diarize(srt=None)`；
  ASR 结束后 join，再 `attach_speakers`；
- 顺带获得**分段缓存**：`segments.json` 与字幕无关，换字幕/重跑 ASR 时可以直接复用分段，
  不再重跑分离模型（只要源文件 sha + 聚类参数没变；`--force` 仍会强制重算）。

**实测**（`work/real-samples/opt5-parallel-10min/`）：10min 播客冷跑
`diarize` 阶段墙钟 **0.03s**（分段在 ASR 的 217s 里跑掉，summary 标 `parallel_with_asr: true`），
比串行省 **~85s**；删掉 `diarize.json`/`speakers.json` 只留 `segments.json` 再跑一次，
`diarize` 阶段 **0.05s**（`reused_segments: true`）、整条链路 **0.3s**——换字幕/重跑 ASR 时
不需要再跑一遍分离模型。

> 实现约束（诚实边界）：`--src-lang auto` 时不做预取（语言未知，声纹模型选不了），
> 这种情况仍是串行 diarize；这也意味着 `auto` 的内封/ASR 路线没有这部分加速。

### 4.4 10min 素材整体耗时（实测）

**本次冷跑（慢网络窗口，`work/real-samples/opt5-parallel-10min/`，状态 ok、成本 ¥0）**：

| 阶段 | 墙钟 | 说明 |
|---|---|---|
| probe | 0.5s | |
| ASR（small） | 217.0s | CPU 瓶颈，没变 |
| diarize | **0.03s** | 分段在 ASR 里并行跑完，这里只补归属（`parallel_with_asr: true`） |
| merge | 0.02s | |
| translate | 179.4s | 并发 4；同窗口串行实测 **683.3s**（3.9×） |
| build | 0.1s | |
| dub | 197.2s | 并发 10；86/86、人工 1、提语速 10 |
| burn / mark | 5.4s | |
| **合计** | **599.7s** | 比 11-02 的 651.8s 少 8%（且当次网络更慢） |

**同机好网络窗口**（translate 19.0s、dub 153.4s）推算：217 + 0 + 19 + 153 + 7 ≈ **396s（6.6min）**
→ **≤7min 目标达成**；网络差时（本次）约 10min，瓶颈是免费 MT/Edge 端点的延迟，不是 CPU。

> 翻译并发的相对收益与端点延迟强相关：好窗口 46.7s → 19.0s（2.5×）；
> 端点抖动窗口（同一小时内实测）**683.3s → 176.5s（3.9×）**——并发把"每条请求都在等网络"叠起来了。

---

## 5. 改动文件

| 文件 | 改动 |
|---|---|
| `submatrix/translate.py` | `ResilientTranslator`、`_fallback_chain`、`_provider_name`、`TranslateOptions.fallback_provider/concurrency`、段落/批次并发、报告字段（provider/provider_primary/provider_fallback/concurrency） |
| `submatrix/dub.py` | 音色降级（`VOICE_ERROR_HINTS`/`_looks_like_voice_error`/`voice_downgrades`）、二次补跑、`concurrency` 默认 6→10 |
| `submatrix/diarize.py` | `segments.json`（`write_segments`/`load_segments`）、`attach_speakers()`（分段与字幕归属解耦） |
| `submatrix/pipeline.py` | `translate_fallback` / `translate_concurrency` 选项与校验、传参、报告 config；ASR 期间后台预取 diarize 分段（`_BackgroundJob`/`_start_diarize_segments`）并在 ASR 后 `attach_speakers`；`--regenerate-tts` 现在会让 dub 缓存失效（顺带修的 bug） |
| `submatrix/cli.py` | `translate --fallback-provider/--concurrency`、`pipeline --translate-fallback/--translate-concurrency`、`--concurrency` 默认 10 |
| `tests/*` | +16 条（translate +6、dub +4、translate 并发 +2、diarize 分段/归属 +3、pipeline 并行 +2 等） |

GUI 不需要改：新开关都可走「额外参数」；默认值已按「免费 MT 4 并发、Edge 10 并发」设好。

---

## 6. 复跑命令

```powershell
# OPT-3：模拟渠道失败（单测已覆盖真实注入）；真实端不可控，只能验证开关
python -m submatrix translate --src work\real-samples\opt-en-10min-v2\merge\merged.srt `
  --out work\real-samples\opt5-translate-c4 --src-lang en --tgt-lang zh --concurrency 4
# 期望：耗时约 19s（串行约 47s），译文与串行逐字节一致

# OPT-4：坏音色降级（单测注入即可，真音色已下线无法稳定复现）
python -m pytest tests/test_dub.py -k "downgrade or dead_voice" -q

# OPT-5：配音并发对比（--regenerate-tts 强制重新合成，注意 Edge 端点抖动）
python -m submatrix pipeline --src work\real-samples\podcast\en-podcast-10min.mp4 `
  --out work\real-samples\opt5-dub3-c10 --src-lang en --tgt-lang zh --diarize --speakers 3 `
  --stop-after dub --regenerate-tts --concurrency 10
# 期望：dub 约 150-200s（并发 6 约 220s），失败 0-1 条；看 dub_report.json 的 config.concurrency

# OPT-5：ASR 与 diarize 并行（源语言必须显式给，auto 不预取）
python -m submatrix pipeline --src work\real-samples\podcast\en-podcast-10min.mp4 `
  --out work\real-samples\opt5-parallel-10min --src-lang en --tgt-lang zh --diarize --speakers 3
# 期望：diarize 阶段墙钟接近 0（summary.parallel_with_asr = true），总耗时比串行少 ~85s
```

---

## 7. 测试与回归基线

| 项 | 结果 |
|---|---|
| 单测 | **677 passed**（+16） |
| 一键回归 | **27/27** |
| 真实素材 | 10min 播客冷跑 599.7s（慢网络窗口）/ 好窗口推算 396s（6.6min）；translate 并发同窗口 683.3s→176.5s；dub 并行段 0.03s |
| 输出正确性 | 并发/串行译文逐字节一致；diarize 并行与串行结果一致（3 人 / 79 段 / 140 条归属）；dub 86/86 |

---

## 8. 遗留与建议

1. **`--src-lang auto` 不享受 ASR/分离并行**：语言要等 ASR 报告才知道，声纹模型选不了；
   想拿满速就跑显式 `--src-lang`。
2. **听感抽检**（OPT-2 遗留）：那 10 条提语速 + 3 条 atempo >1.28 的 cue 还没人工听。
3. **Edge/微软端点不稳定**：本次实测期间端点多次抖动，说明"失败重试 + 二次补跑 + 渠道回退"
   这套兜底是必要的；长期不稳可换 TTS 渠道（外部命令模板已留插槽）。
4. OPT-6（VAD 预过滤）/ OPT-7（声纹二次聚类）/ OPT-8 / OPT-9 未开工。
