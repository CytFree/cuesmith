# OPT-1 / OPT-2 优化说明：英文自动人数 + 配音溢出

> 对应待办：[待办-优化项.md](待办-优化项.md) 的 OPT-1（P0）与 OPT-2（P0）。
> 来源限制：[真实素材验证-3人-长视频-批量-2026-11-02.md](真实素材验证-3人-长视频-批量-2026-11-02.md)。
> 完成日期：2026-11-02。**测试 661 passed（+27）；回归 27/27（50.7s）**；真实素材复跑全部免费（Edge-TTS / 免费 MT）。

---

## 1. 一句话结论

| 项 | 优化前 | 优化后 | 验收 |
|---|---|---|---|
| 英文 10min 播客「自动人数」 | 原始 **10 簇**（0.95 阈值下） | **4 人 / 90 段**（并掉 6 个 <3s 簇） | ✅ 目标 4~5 人 |
| 英→中配音「转人工」条数 | **10 / 86** | **1 / 86** | ✅ 目标 ≤4 |
| 英→中配音「播放时长溢出 >10%」 | 未统计（只有合成原始时长口径 24 条） | 合成口径 24 条不变，**播放口径 9 条** | ✅ 目标 ≤10（口径见 §3.4） |
| 英→中配音「落在可用时间内」 | 未统计 | **85 / 86** | ✅ |
| 字幕信息量 | 精简 1 条（trim） | **精简 1 条（不变）** | ✅ 不为配音多丢信息 |
| 中文 3 人 zh→en 链路 | 人工 0 / atempo 4 / 溢出 2 | **完全一致**（人工 0 / atempo 4 / 溢出 2） | ✅ 无劣化 |
| 其他回归 | — | 中文 4 人 `--speakers 4` 4 人/10 段；demo2 2 人；单人口播仍 1 人 | ✅ |

---

## 2. OPT-1：英文自动人数（短簇合并 + 语言阈值）

### 2.1 问题

同一条 10min 英文播客，`diarize` 自动聚类人数随阈值剧烈变化：0.8 → **15 人**、0.95 → 10 人、
0.98 → 9 人、0.99 → 8 人。根因不是"模型不准"，而是**两个声纹模型的簇距离分布不同**：

- 中文 3D-Speaker（eres2net）在真实素材上 0.8 就稳定；
- 英文 NeMo titanet-small 偏"碎"：笑声、广告人声、音乐人声都会被单独聚成簇，
  这些簇语音只有 0.32–4.07s，对配音毫无意义，却会把音色池耗光、让报告人数失真。

### 2.2 方案

1. **语言默认阈值**：`diarize.DEFAULT_THRESHOLDS = {"zh": 0.8, "en": 0.95}`
   （`DEFAULT_THRESHOLD = 0.8` 兜底未知语言）；显式 `--threshold` / `--diarization-threshold`
   仍然优先，报告记 `config.threshold_source = explicit | language-default`。
2. **短簇合并** `merge_minor_speakers()`：
   - 主簇 = 语音 ≥ `min_speaker_speech_s`（默认 3s）**且** 占比 ≥ `min_speaker_share`（默认 1%）；
   - 短簇按**时间间隙最近**的主簇并入（逐段投票，多段取多数），不按簇编号；
   - 合并后按出场顺序重新编号（不留 `SPEAKER_03` 空洞，音色池仍按顺序分配）。
3. **宁多不并**：合并后只剩 1 人（原簇 ≥2）时保留原结果并告警——可能真是一人分饰/停顿被拆。
4. **可关闭**：`--keep-minor-speakers`（`diarize` / `pipeline` 都有）；
   diarize 缓存判据加入 `min_speaker_speech_s`，改开关会重跑。
5. **报告**：`summary.speakers_raw` / `merged_clusters`（逐簇：`speaker`、`into`、`speech_s`、
   `share_pct`、`turns`）/ `cluster_merge_skipped`；告警列出每个被并簇与人数变化。
   CLI 摘要直接显示「原始 N 簇，并入 M 个短簇」。

### 2.3 实测

| 素材 | 场景 | 结果 |
|---|---|---|
| 英文 10min 播客（3 位主持人 + 广告/笑声） | auto（默认 0.95 + 合并） | 原始 10 簇 → **4 人 / 90 段**；主簇 311.9s / 254.0s；并掉 6 个短簇（4.07s、0.56s、0.62s、1.15s、0.32s、1.57s） |
| 中文 4 人样本（sherpa 官方 30s） | `--speakers 4` | **4 人 / 10 段**（与优化前一致） |
| 中文 4 人样本 | auto（0.8） | 仍 5 人（没有 <3s 的簇可并，属 OPT-7 范围，如实保留） |
| 中文 3 人素材 4min | `--speakers 3` | **3 人 / 10 段**，逐簇 54.71 / 158.19 / 19.39s 与优化前**完全一致** |
| 双人对话 demo2 | auto | **2 人 / 4 段**（音色 2 种） |
| 中文 60s 单人口播 | auto | **1 人** + 「多音色不会生效」告警（回退单音色） |

> 注意：10min 播客 4 人里有两个是**短发言者**（22.5s / 9.7s），它们超过了 3s 门槛所以保留——
> 这正是设计意图：只清碎片、不吞掉真实的小角色。要完全还原可加 `--keep-minor-speakers`。

### 2.4 单测

`tests/test_diarize.py` +10：短簇并入按时间间隙、主簇不动、按占比并（时长够但占比 <1%）、
宁多不并（single-main）、`--keep-minor-speakers` 关闭、语言默认阈值（en 0.95 / zh 0.8 / 显式优先）、
`run_diarize` 报告字段与告警；`tests/test_pipeline.py` +1：语言默认阈值与缓存判据（阈值/合并开关，复用原有用例扩写）。

---

## 3. OPT-2：配音溢出（自适应提语速 + atempo 1.4 + 槽位预算保险丝）

### 3.1 问题与先测量

英→中 10min 播客：86 条里 **24 条合成时长溢出 >10%**、**10 条转人工**（>1.3× 压不下去）。

先确认"到底是不是译文太长"——**不是**：

- 译文 CPS 均值 5.92（下限 9，完全不超）；
- 中文 TTS 在 +30% 下实测 **5.99 字/秒**（86 条中位 6.12，p10 4.63、p90 7.23），
  而槽位密度均值 5.95 字/秒 —— 平均刚好说满、**方差**导致一部分说不完。

所以真正的杠杆在**配音侧**：让 TTS 自己说快一点（保留语调），而不是硬剪译文或硬 atempo。

### 3.2 方案（三件，按优先级）

**A. 自适应提语速（主力）** — `dub.adaptive_rate`

- 首轮全部按基准语速合成 → 量时长 → 只对「连 `--tempo-limit` 都救不回来」的条目
  用**同一音色**换更高语速重合成一次（不换音色、不动文本）；
- 档位 = 基准之上 +15/+25/+35 个百分点（zh +30% → **+45/+55/+65%**；en +20% → +35/+45/+55%），
  上限 `MAX_RATE_PCT = +80%`；选"能压进可用时间（留 ≤15% 给 atempo）的最小档"，不够就给最大档；
- 缓存键本来就是「文本 + 音色 + **语速** + 命令」内容指纹，提语速后的音频独立缓存，
  重跑不会重复合成；`reused` 字段也改成按**最终使用的那条音频**判定；
- TTS 失败/读不出时长 → 保留原速音频、如实告警，不让整片失败；
- `--tts-command`（外部命令引擎）不支持改语速 → 自动跳过并在 `config.adaptive_rate=false` 记录；
- `--no-adaptive-rate` 可关闭。

**B. atempo 上限 1.3 → 1.4**（`--tempo-limit` 可调）——提语速后的兜底。

**C. 槽位预算保险丝** — `translate.dub_cps_budget`

- `tts.speech_cps()` 用实测语速标定（zh 4.6 字/秒、en 15.3 字符/秒 为原速），
  `speech_capacity_cps(headroom=1.6)` 再乘「提语速 × atempo」余量 → zh@+30% 约 **9.57 字/秒**、
  en@+20% 约 **29.4 字符/秒**；
- 精简预算取**字幕阅读预算**与**配音容量预算**的**更严者**（`min`）：
  默认语速下容量更宽 → 由阅读 CPS 把关（**字幕可读性不被牺牲**）；
  只有 `--tts-rate` 调到 TTS 根本念不完时才由容量把关，提前精简；
- 报告 `summary.dub_budget = {capacity_cps, binds, cues_narrowed, over_capacity, over_capacity_cues}`，
  超容量的条目逐条列出并告警（预告"可能转人工"）；
- translate 缓存判据加入 `dub_cps_budget`，改语速会重算译文。

### 3.3 一个被推翻的设计（留档，避免以后再踩）

最初 C 用的是 `max()`（容量比阅读宽就放宽精简），实测 **zh→en 出现可读性回归**：

| zh→en 4min 3 人素材 | 精简条数 | 译文 CPS 均值 | 超 20 CPS 条数 |
|---|---|---|---|
| 旧行为（只用阅读 CPS） | 7 条（截断 734 字） | 15.57 | 0 |
| `max()` 放宽 | 1 条 | **18.25** | **7** |

即：为了少剪 6 条译文，把 7 条字幕推到"读不完"（最多 25.8 CPS），得不偿失。
改成 `min()` 后该素材**逐项与旧行为完全一致**（CPS 15.57、精简 7、TTS 3,689 字符）。
→ 所以 OPT-2 的信息量优化由**配音侧**承担，文本侧只做"怎么都念不完"的保险丝。

### 3.4 实测（英→中 10min 播客，同一素材、同样参数，全冷缓存）

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 合成成功 | 86/86 | 86/86 |
| **转人工（manual）** | **10** | **1**（cue 32） |
| 提语速条数 | 0 | 10（全部 +65%，剩余 atempo 1.128–1.40） |
| atempo 条数 / 均值 | 16 / 1.123 | 25 / 1.155 |
| 静音吸收（absorbed） | 11 | 11 |
| 合成原始时长溢出 >10% | 24 | 24（口径未变，仍然如实报） |
| **播放时长溢出 >10%**（atempo 之后） | 未统计 | **9** |
| **落在可用时间内**（fits_room） | 未统计 | **85/86** |
| 译文精简（信息损失面） | trim 1 | trim 1（**不变**） |
| dub 墙钟 | 238.6s | 296.0s（多 10 次重合成调用，+24%） |
| 成本 | ¥0（Edge-TTS 3,159 字符） | ¥0（同 3,159 字符） |

唯一转人工的 cue 32 是"0.82s 槽位要塞 12 字"的极端条：提语速到 +65% 后 2040ms，
相对可用 1060ms 仍需 atempo 1.93（超上限 1.4）→ 如实标 manual，并在 translate 侧已被
`over_capacity` 预警（`over_capacity_cues: [32]`）。

**关于两个"溢出"口径**（避免误读）：

- `overflow_over_10pct` = **合成原始时长** vs 槽位——优化前后都是 24，因为它不含任何补救；
- `played_overflow_over_10pct` = **最终播放时长**（含 atempo）vs 槽位 = 9；
- 这 9 条都落在"本条开始 → 下一条开始"的**句间静音**内（`fits_room` 85/86），不会压到下一条，
  和收费软件"借空隙"的做法一致；真要贴槽位就得把 20+ 条再压 10–20%，听感代价更大。

### 3.5 中文 zh→en 回归（4min 3 人素材）

| 指标 | 优化前 | 优化后 |
|---|---|---|
| dub 成功 / 转人工 | 19/19 / 0 | 19/19 / 0 |
| atempo | 4 条（1.003–1.138） | 4 条（**同一批 cue、同一数值**） |
| 合成溢出 >10% | 2 | 2 |
| 播放溢出 / fits_room | — | 0 / 19 |
| 提语速 | — | 0（没有需要提的条目） |
| translate CPS / 精简 | 15.57 / trim 7 | 15.57 / trim 7（**完全一致**） |

### 3.6 单测

- `tests/test_dub.py` +7：档位随基准语速伸缩并封顶、选档逻辑（最小够用/最大兜底/无语速基准）、
  提语速后不再是 manual 且 played ≤ room、`--no-adaptive-rate`、提语速失败保留原速音频并告警、
  `--tts-command` 自动跳过、`--tempo-limit` 显式值。
- `tests/test_translate.py` +5：槽位预算借句间空隙、`min` 语义（容量宽→不改结果、容量窄→提前精简）、
  超容量逐条报 + 告警、旧报告无该字段时 `dub_budget=None`。
- `tests/test_tts.py` +4：`parse_rate` / `format_rate` / `speech_cps`（对齐实测 6.0 与 18.4）/
  `speech_capacity_cps`（headroom、未知语言）。
- `tests/test_pipeline.py`：缓存判据补 `tempo_limit` / `adaptive_rate` / `dub_cps_budget`。

---

## 4. 改动文件

| 文件 | 改动 |
|---|---|
| `cuesmith/diarize.py` | `DEFAULT_THRESHOLDS`、`default_threshold()`、`ClusterMerge`、`merge_minor_speakers()`；`DiarizeOptions.threshold=None` + `resolved_threshold` + `min_speaker_speech_s` / `min_speaker_share`；报告与告警 |
| `cuesmith/tts.py` | `BASE_SPEECH_CPS`、`parse_rate` / `format_rate` / `speech_cps` / `speech_capacity_cps` |
| `cuesmith/dub.py` | 两轮合成（首轮 + 提语速）、`rate_ladder` / `_choose_escalated_rate`、语速进缓存键/引擎键、`tempo_limit=1.4`、`played_ms` / `played_overflow_pct` / `fits_room` / `rate_escalated` |
| `cuesmith/translate.py` | `dub_cps_budget`、`_slot_budgets`（借用句间空隙）、精简预算取 `min`、`summary.dub_budget` |
| `cuesmith/pipeline.py` | `--keep-minor-speakers`、`diarization_threshold=None`（按语言默认）、`--adaptive-rate`、`tempo_limit=1.4`、translate/dub 缓存判据 |
| `cuesmith/cli.py` | 上述开关与帮助文案（`diarize` / `pipeline` / `dub`） |
| `tests/*` | +27 条单测（`test_diarize` +10、`test_pipeline` +1、`test_dub` +7、`test_translate` +5、`test_tts` +4） |

GUI 不需要改：`--diarization-threshold` / `--keep-minor-speakers` / `--adaptive-rate` 走「额外参数」透传，
默认值已按语言与最佳实践设好。

---

## 5. 复跑命令

```powershell
# OPT-1：英文自动人数（10min 播客）
python -m cuesmith diarize --src work\real-samples\podcast\en-podcast-10min.mp4 `
  --out work\real-samples\diarize\opt-en-10min `
  --srt work\real-samples\run-en-10min\asr\source.srt --src-lang en
# 期望：说话人 4 个 / 90 段（原始 10 簇，并入 6 个短簇）

# OPT-1 回归：中文 4 人样本 / 中文 3 人素材 / 双人 demo2
python -m cuesmith diarize --src spike\diarization\models\0-four-speakers-zh.wav `
  --out work\real-samples\diarize\opt-4p-known4 --src-lang zh --speakers 4
python -m cuesmith diarize --src work\real-samples\zh-dialogue-4min.mkv `
  --out work\real-samples\diarize\opt-zh-3p-known3 `
  --srt work\real-samples\run-zh-3p\asr\source.srt --src-lang zh --speakers 3

# OPT-2：英→中全链路（10min 播客，冷缓存约 10–11min，成本 ¥0）
python -m cuesmith pipeline --src work\real-samples\podcast\en-podcast-10min.mp4 `
  --out work\real-samples\opt-en-10min-v2 --src-lang en --tgt-lang zh `
  --diarize --speakers 3
# 期望：dub 86/86、人工 1、fits_room 85/86、rate_escalated 10

# zh→en 回归（4min 3 人素材）
python -m cuesmith pipeline --src work\real-samples\zh-dialogue-4min.mkv `
  --out work\real-samples\opt-zh-3p-v2 --src-lang zh --tgt-lang en `
  --diarize --speakers 3
```

报告里看哪里：

- `diarize.json` → `summary.speakers_raw` / `summary.merged_clusters` / `config.threshold_source`；
- `translate.json` → `summary.dub_budget` / `config.condense.dub_cps_budget`；
- `dub_report.json` → `cues[].rate` / `rate_escalated` / `played_ms` / `played_overflow_pct`；
  `summary.manual` / `played_overflow_over_10pct` / `fits_room` / `rate_escalated`。

---

## 6. 测试与回归基线

| 项 | 结果 |
|---|---|
| 单测 | **661 passed**（OPT-1 +11、OPT-2 +16） |
| 一键回归 | **27/27，50.7s**（`work/regression/regression.json`） |
| 真实素材 | 英文 10min 播客 auto 4 人；英→中 dub 86/86 人工 1；中文 3 人 zh→en 与优化前逐项一致 |
| GUI | 未改 GUI 代码；`cuesmith.gui` 冒烟测试仍随单测通过 |

---

## 7. 没做的（仍在待办）

- **OPT-7 自动判人人数**：中文 4 人 auto 仍 5 人（没有 <3s 的簇，短簇合并无能为力）——
  要彻底解决得做声纹 embedding 二次聚类；已知人数用 `--speakers N` 最准。
- **OPT-5 长视频耗时**：本次 dub 因为多 10 次重合成调用慢了约 24%（238.6s → 296.0s），
  可以靠提并发/缓存优化补回来。
- **听感抽检**：本次只保证数值口径（atempo ≤1.4、语速档位有上限、文本不变），
  人工听感抽检未做——建议发布前抽听那 10 条提语速的 cue 与 3 条 atempo >1.28 的 cue。
- OPT-3（翻译渠道回退）、OPT-4（音色下线降级）、OPT-6（VAD 预过滤）、OPT-8、OPT-9 未开工。
