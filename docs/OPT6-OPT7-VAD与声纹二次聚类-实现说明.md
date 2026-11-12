# OPT-6 / OPT-7 实现说明：VAD 预过滤 + 声纹 embedding 二次聚类

> 对应待办：[待办-优化项.md](待办-优化项.md) 的 **P2（OPT-6、OPT-7）**。
> 完成日期：2026-11-03。测试 **694 passed**、回归 **27/27**、真实素材 6 个样本全部达标、成本仍 **¥0**。

---

## 1. 一句话结论

| 项 | 做了什么 | 效果（真实素材） |
|---|---|---|
| **OPT-6** | `diarize_worker.py` 先用 **silero VAD** 切出语音段、把语音段**拼成一条"只有语音"的音轨**送分离/聚类，结果再按时间映射回原视频 | 10min 英文播客原始簇 **10 → 9**、说话人 **4 → 3**（真值 3）；中文 4 人样本 **5 → 4**（真值 4）；音乐/掌声不再参与聚类，报告新增 VAD 覆盖度 |
| **OPT-7** | worker 对每个 turn 提声纹 embedding，做**只合并不拆分**的 turn 级层次聚类，阈值 **τ = α × 同人相似度参考 s_same**，自动判人数 | 6 个真实样本人数全中：60s 单人 **1**、双人 demo **2**、中文 4 人 **4**、中文 3 人 **3**、英文 10min **3**、中文新闻 3 人 **4**（真值 3，在 3±1 内）；`--speakers N` 显式人数仍走原 sherpa 路径 |

默认全部开启（模型缺失时 VAD 自动跳过、二次聚类失败回退 sherpa 并如实告警），可用 `--no-vad` / `--clustering sherpa` / `--merge-alpha` 关或调。

---

## 2. OPT-6：VAD 预过滤（先切语音、再聚类）

### 2.1 三种候选方案都试了，只留了一种

背景：英文 10min 播客 auto 0.8 曾聚出 15 人、auto 0.95 聚出 10 簇（短簇合并后 4 人），
中文 4 人样本 auto 0.8 出 5 人。最初假设"音乐/掌声段被当成说话人"，因此先在真实素材上
对比了三种 VAD 用法（脚本 `work/opt6-exp/compare.py`、`work/opt6-exp/postfilter.py`）：

| 方案 | 做法 | en 10min（真值 3） | zh 4p（真值 4） | zh 60s 单人（真值 1） | 结论 |
|---|---|---|---|---|---|
| A. 置零 | 非语音段置零、时间轴不变 | 4→**5** ❌ | 5→5 | 1→1 | 无效（分割模型对全零区间仍出段） |
| B. 后过滤 | 先跑完整分离，再按 turn 的 VAD 语音占比丢 turn | 4→4 | 5→5 | 1→1 | 无效（turn 本来就 99% 落在语音上） |
| C. **拼接 + 回映射** | 只把语音段拼起来送模型，结果映射回原时间轴 | 10 簇→**3 人** ✅ | 5→**4 人** ✅ | 1→1（0.15s 拼接会变 2，见下） | 唯一有效，采用 |

方案 B 的实测说明：拿基线 90 个 turn 算 VAD 语音占比，cut=0.1/0.3/0.5/0.8 下几乎全部 turn 都
≥0.9（en 只丢 1–7 个 turn）。**伪簇不是来自"纯静音"，而是来自音乐里的人声/笑声**——
这类段 VAD 也判成语音，所以"后过滤"没用；必须让它们**根本不进**分割/聚类。

### 2.2 实现（`cuesmith/diarize_worker.py`）

1. `_vad_intervals()`：silero VAD（`spike/diarization/models/vad/silero_vad.onnx`，643KB）扫全片，
   参数 `threshold=0.5 / min_silence=0.25s / min_speech=0.25s / max_speech=30s`；
2. `_compact_speech()`：按时间顺序把语音段拼成一条音轨，段间补 **0.15s 静音**（避免两句被
   当成同一句），同时产出映射表 `(compact_start, compact_end, original_start, original_end)`；
3. 分离/聚类只在拼接音轨上跑（`model_input_s` 通常比原片短 10–15%）；
4. `_map_segments_back()`：把分段映射回原时间轴，**跨多个语音段的 turn 按段拆开**——
   否则 turn 会把被丢掉的音乐区间也"覆盖"了，覆盖率虚高、字幕归属被拉偏。

主进程 `cuesmith/diarize.py` 的变化：

- `find_vad_model()`：显式 `--vad-model` > `CUESMITH_DIARIZATION_VAD` > 项目内默认路径；
  没装模型时 **不报错**（VAD 只是优化），报告 `config.vad.skipped="model-missing"`；
- `DiarizeOptions.vad / vad_model / vad_threshold`（默认开、0.5）；
- 报告新增 `config.vad{mode:"compact"}` 与 `summary.vad{segments, speech_s, speech_pct, removed_s, elapsed_s, model_input_s}`，
  没检测到语音时告警"VAD 没有检测到语音…"；
- `find_models()` 显式排除 VAD 模型，避免它被当成声纹候选。

### 2.3 已知限制（诚实记录）

- **拼接会让单人口播被拆成 2 个簇**：60s 单人样本（16 个 VAD 段）直接聚出 2 人。
  OPT-7 的合并步骤（τ=0.70）把它并回 1 人，所以最终结果正确；
- 拼接对参数敏感：`min_silence` 0.25→0.5 时 60s 能稳住 1 人，但中文 4 人样本会掉到 3；
  0.15s 的段间静音也是折中值。这类敏感性由 OPT-7 的"只合并不拆分"兜底，但**仍属于启发式**；
- VAD 保留的是"语音活动"，**音乐中的人声（唱歌/广告）仍会被当语音**，不在本项范围内。

---

## 3. OPT-7：声纹 embedding 二次聚类（只合并、不拆分）

### 3.1 为什么不是"重新聚一遍 k"

基线 sherpa 的 `FastClustering` 用固定阈值/固定人数，问题主要在**把同一个人拆成多个簇**
（中文 3 人 → 4、新闻 3 人 → 6、英文 10min → 10）。而"拆"比"并"危险：把两个人并成一个，
全片只有一种音色；把一个人拆成两个，多一种音色但至少内容对。所以二次聚类做成
**merge-only（只合并、不拆分）**，并保留"宁多不并"的底线（s_same 退化时阈值偏高）。

### 3.2 算法（`_turn_embeddings()` + `_merge_embeddings()`，纯 numpy）

1. **turn embedding**：对每个 turn 切 **1.5s 窗 / hop 1.0s**（首尾各掐 80ms，避免混入相邻人声），
   每窗提声纹 → 逐窗 L2 归一化 → 平均 → 再归一化；
2. **同人相似度参考 `s_same`**：每个 turn 内多窗两两余弦均值（`within`，只有 1 个窗则为 NaN），
   取所有 turn 的**中位数**；有效值不足 2 个时退回 0.6。这是"把绝对阈值标定到这段音频/这个
   声纹模型尺度上"的关键——中文 3dspeaker 的 s_same≈0.55、英文 titanet≈0.53，绝对值不可通用；
3. **τ = α × s_same**；主簇 = 语音 ≥ max(3s, 1% 总语音)，其余算短簇；
4. 主簇间**贪心 average-linkage**：每次合并"turn 级平均成对相似度"最高且 ≥τ 的两个簇，遇到
   第一个 <τ 就停；**不能用簇质心余弦**（大簇质心会被平均拉向公共方向：实测 en 10min 里
   45 个 turn 的簇质心相似度 0.914，真实平均成对只有 0.33，用质心会把 10 簇并成 1 人）；
5. 短簇按"相似度 + 时间邻近（`+0.25·exp(-gap/10)`）"并入主簇，最后按首次出现重编号。

**α 两档（关键）**：

| 输入模式 | α | 原因 |
|---|---|---|
| 原始音频（`--no-vad`，或没装 VAD 模型） | **0.55** | 6 样本校准值 |
| VAD 拼接音频（默认） | **0.70** | 拼接让 turn 变长/变杂，跨簇平均成对相似度整体抬高 **10–50%**（press 0.3615→0.5302、3p 0.4239→0.4727），而 s_same 基本不变；沿用 0.55 会过并（3p→2、press→2） |

`--merge-alpha` 显式给值优先；报告 `config.merge_alpha` + `merge_alpha_source`（explicit /
vad-default / default）。

### 3.3 集成位置与"能并成 1 人"

- 二次聚类在 **worker 内**完成（只做**主簇之间**的合并），短簇合并仍留给主进程原有的
  `merge_minor_speakers()`——分工与线上规则不变；worker 先并成 1 人时主进程看到 1 个簇，
  不会再触发"宁多不并"回退（已单测覆盖）；
- worker 失败（模型异常等）→ 保留 sherpa 结果，`clustering.fallback` 写错误摘要，链路不失败；
- `--speakers N` 显式人数时 `clustering=auto` 解析为 **sherpa**（人数已由用户给定，
  二次聚类无从改进，还会把用户指定的 N 并掉）；
- 缓存判据加入 `clustering_effective` 与 `merge_alpha`；OPT-7 之前的老缓存（无 `clustering`
  字段）在自动判人数时会重跑一次，保证升级后拿到新结果。

### 3.4 新增报告字段

`diarize.json`：

```json
"config": {"clustering": "auto", "clustering_effective": "embedding", "merge_alpha": 0.7, "merge_alpha_source": "vad-default"},
"summary": {"clustering": {"mode": "embedding", "turns": 62, "clusters_in": 9, "clusters_out": 3,
                            "s_same": 0.5125, "tau": 0.3587, "merge_sims": [], "elapsed_s": 14.05},
            "vad": {"segments": 157, "speech_s": 505.6, "speech_pct": 84.27, "removed_s": 94.4,
                    "model_input_s": 529.0, "elapsed_s": 6.58}}
```

CLI 摘要追加两行：`VAD 预过滤：语音 84.27%（157 段，丢弃非语音 94.4s）`、
`声纹二次聚类：9 → 3 人（τ=0.3587，同人相似度 0.5125）`；人数变少时 warnings 里也写一条。

---

## 4. 真实素材验收（默认参数：VAD 开 + 二次聚类 auto）

素材都是项目内已有的真实样本（中文 3D-Speaker / 英文 NeMo 声纹，8 线程 CPU）。

| 样本 | 时长 | 真值 | 基线（OPT-1/2） | **本次默认** | 原始簇 | 判定 |
|---|---|---|---|---|---|---|
| 英文播客 | 600s | 3 | 4 | **3** | 10 → 9 | ✅ 3±1 |
| 中文 4 人对话（sherpa 官方） | 56.9s | 4 | 5 | **4** | 5 → 4 | ✅ |
| 中文 3 人对话 | 240s | 3 | 4 | **3** | 4 → 3 | ✅ |
| 中文新闻 3 人 | 229s | 3 | 6 | **4** | 6 → 4 | ✅ 3±1 |
| 中文双人 demo | 19.7s | 2 | 2 | **2** | 2 | ✅ |
| 中文单人口播 | 60s | 1 | 1 | **1** | 1 → 2 → 1 | ✅（靠二次聚类并回） |

英文 10min 细节：VAD 判定语音 **505.6s / 84.27%**（157 段，丢弃 94.4s 音乐/掌声），
模型输入 **529.0s**；9 簇 → **3 人 / 204 段**（映射拆分后段数变多，属正常），
140 条字幕 **140 条全归属**（74 / 60 / 6 条），主簇 272.4s / 239.0s / 22.5s，
覆盖率 89.0%（只统计语音区间，比原来的 99.7% 更诚实）。

中文 3 人细节：4 → **3 人**，三簇语音 **53.68s / 142.58s / 17.24s**——与历史
`--speakers 3` 人工验收的簇结构一致（54.71 / 158.19 / 19.39，差异来自本次 VAD 只保留语音）。

耗时（en 10min，单次冷跑 diarize）：**100.8s**（VAD 6.6s + 分离/聚类主体 ~80s +
turn embedding 14.0s），比优化前 89.1s 多 **+13%**；中文样本增量 2–8s。
**成本 ¥0**（全部本地 CPU 推理）。

### 4.1 单测与回归

- 测试 **677 → 694 passed（+17）**：VAD 模型解析/降级/参数透传、compact 拼接与回映射、
  二次聚类（相似簇合并、低于阈值不并、单人塌缩、短簇/重编号/窗口辅助函数）、
  auto/sherpa 解析、α 随 VAD 模式、报告字段、pipeline 缓存判据（vad / clustering / merge_alpha）；
- 回归 `python tools\run_samples.py` → **27/27（34.8s）**；
- 端到端：zh 60s 素材 `pipeline --diarize` 真实跑通——ASR 128.0s（与 diarize 并行，diarize
  阶段 0.0s）、3 人/15 段/3 音色，translate 23.1s、dub **6/6**、burn/mark ✅、状态 ok、成本 ¥0；
  再跑一次 diarize 阶段 `cached`（缓存判据生效）。

---

## 5. 风险与遗留

1. **α 只在 6 个样本上校准**：4p 的合并间距 15%+ 很稳；3p / en / press 的合并相似度与 τ 只差
   1–4%。上线前建议再拿 1–2 条真实素材回归（尤其多主播闲聊类）；
2. **3p vs press 存在结构性阈值冲突**：3p 该停的合并相似度 **0.3004**、press 该继续的 **0.31**，
   差 1%；样本 s_same 也差 6%，单一 α 无解。默认 α 按"3p 要准"取 0.70（press 6→4，仍在 3±1）；
   若要 press 更准可 `--merge-alpha 0.47~0.52`（代价是 3p 可能掉到 2）；
3. **VAD 参数敏感**：`min_silence` / 段间静音长度会影响拼接后的聚类尺度；本版取
   0.25s / 0.15s，靠二次聚类兜底。若素材特别碎（大量 <0.25s 停顿），可 `--no-vad` 走原始音频
   （α 自动变 0.55）；
4. **turn 数膨胀**：拼接回映射会按语音段拆 turn（en 10min 90 → 204 段），报告 `summary.turns`
   会变大；这是真实的分段，不影响归属与配音；
5. **VAD 只解决"音乐/静音不参与聚类"**，音乐里的人声、笑声仍会被判为语音；这类伪说话人靠
   OPT-1 短簇合并 + OPT-7 二次聚类兜底；
6. 模型不在项目内（`spike/diarization/models/vad/silero_vad.onnx` 需下载，643KB，gh-proxy 可用）；
   缺模型时 VAD 自动跳过并写 `skipped`，不影响链路。

---

## 6. 复跑命令

```powershell
# 模型（一次性，643KB）
$gh = "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download"
curl.exe -L -o "spike/diarization/models/vad/silero_vad.onnx" "$gh/asr-models/silero_vad.onnx"

# 默认：VAD 预过滤 + 自动二次聚类
python -m cuesmith diarize --src work/real-samples/podcast/en-podcast-10min.mp4 `
  --srt work/real-samples/run-en-10min/asr/source.srt --out work/real-samples/opt7b-en-10min --src-lang en

# 关掉 VAD / 关掉二次聚类 / 显式人数 / 调阈值
python -m cuesmith diarize --src in.mp4 --out out --src-lang zh --no-vad
python -m cuesmith diarize --src in.mp4 --out out --src-lang zh --clustering sherpa
python -m cuesmith diarize --src in.mp4 --out out --src-lang zh --speakers 4
python -m cuesmith diarize --src in.mp4 --out out --src-lang zh --merge-alpha 0.5

# pipeline 同款开关：--vad/--no-vad / --clustering / --merge-alpha / --vad-threshold
```

## 7. 产物索引

- 代码：`cuesmith/diarize_worker.py`（VAD 拼接/回映射、embedding 二次聚类）、
  `cuesmith/diarize.py`（模型定位/选项/报告/缓存字段）、`cuesmith/cli.py`、`cuesmith/pipeline.py`；
- 单测：`tests/test_diarize.py`（+14）、`tests/test_pipeline.py`（+3）；
- 实测产物：`work/real-samples/opt7b-en-10min/`、`opt7b-zh-4p/`、`opt7b-zh-3p/`、
  `opt7b-zh-press/`、`opt7b-zh-60s/`、`opt7b-demo2/`、`opt7b-pipe-zh-60s/`；
- 实验记录（离线选型，不参与产品代码）：`work/opt6-exp/`、`work/opt7-exp/REPORT.md`。
