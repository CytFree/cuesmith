# M9 — pipeline 接入句级合并（消半句）+ BGM 44.1kHz 立体声混音

> 状态：**已完成**。用户口径："A + B"——A = pipeline 接 `merge`，自动把 ASR 碎片并成句，
> 消掉"英文半句"；B = BGM 混音从 24kHz 单声道升到 44.1kHz 立体声。
> 验收：**AC-24 / AC-25**。测试 **464 passed**，一键回归 **26/26（18.7s）**。
> 真实素材：`D:\软件\下载\央视科普-1.mp4`（154.0s，中文科普《盐》，zh→en）。
>
> **M10 增补**（另见 `M10-字幕按句切分与硬字幕成片-实现说明.md`）：pipeline 在 dub 与 mark
> 之间新增 `burn` 阶段（默认烧硬字幕，`--burn-position top` 可避让源片自带硬字幕）；
> 排版层改为"译文句子优先"切分，M9 记录里的 36 条/18 条半句已被 M10 修到 34 条/8 条
> 逗号分句（0 条断虚词）。

---

## 1. 问题：两条都不是"效果调优"，是明确的缺陷

### 1.1 A：英文出现半句（句子被 ASR 切条切断）

M8 的 pipeline 是 `asr → translate → build → dub`，翻译层拿到的输入是 ASR 的 cue。
但 ASR **为了字幕显示按 7 秒切条**（`segment_words` 的 `max_duration_ms=7000`），
所以一句 13 秒的话会被切成两条，翻译只能按半句翻：

```
[校对版 ASR，21 条]
7  00:00:37,060 → 00:00:44,000  盐。没错…为了生存，它们展开了
8  00:00:44,000 → 00:00:51,000  食盐争夺战。这个洞是怎么形成的？…

[翻译结果，逐 cue 翻]
… To survive, they wage.        ← 到这里就断了
a battle for salt. How did …    ← 下一条才接上，英文读不通
```

`merge` 模块（M4 就有）本来就能把碎片并成句子，但 **pipeline 没接**；而且 CLI 默认
`--max-duration 12` 对"7s + 7s = 14s"的相邻碎片永远合不回去——两个上限打架，A 项
不是"加个调用"那么简单。

### 1.2 B：保留 BGM 却把伴奏砍成 24k 单声道

`dub` 的配音轨是 TTS 原生的 **24kHz 单声道**（`dubbed.wav`），而 `bgm` 混音只做了
`-ar 24000 -ac 1`。更隐蔽的是：`amix` 的格式按**第一条输入**协商，于是 44.1k 立体声的
伴奏在进 `amix` 之前就被重采样成 24k、下混成单声道——**保留 BGM 的卖点（音乐/环境声）
被这条链路自己砍掉了一半频宽和整个声场**。M6 验收只看"伴奏有没有保留、有没有让路"，
没看格式，所以一直没暴露。

---

## 2. A 项实现：pipeline 增加 `merge` 阶段

### 2.1 新增可复用的 `run_merge`

`cuesmith/merge.py` 原来只有纯函数 `merge_cues`，CLI 自己在 `_run_merge` 里拼落盘与
报告。现在抽出 `run_merge(src, out, options)` → `MergeResult`，CLI 与 pipeline 共用：

- 读 SRT → `merge_cues` → 写 `merged.srt` + `merge.json`；
- `merge.json` 除 before/after/merged_ratio 外，**记录输入 sha256**（缓存判据）。

合并规则一字未改（句末标点切句、间隔 ≤1s、只合并不拆分）——M4 已验证过。

### 2.2 阶段位置与开关

```
probe → asr → merge → translate → build → dub → mark
                 ↑ 新增
```

- **只在 ASR 路线跑**：内封字幕是人工断句，默认 `skipped`（合并会把两个人的台词粘一起）；
- `--no-merge` 可关；`--merge-max-duration`（默认 **15s**）/ `--merge-max-gap`（默认 1s）可调；
- **15s 的理由**：ASR 按 7s 切条，两个相邻碎片就是 14s，上限设 12s 会导致"半句永远合不
  回去"（就是 §1.1 那条）。合并只影响**翻译/配音的句子单位**，发布字幕仍由排版层
  按宽度/CPS 再切——本片合并后 15 条，`build` 排版成 36 条，显示不受影响；
- 产物：`merge/merged.srt`、`merge/merge.json`，并在 `pipeline.json` 的
  `outputs.merged_srt` / `config.merge*` / 阶段 `summary{before,after,merged_away}` 留痕。

### 2.3 顺手修掉缓存的两个真问题（人工校对闭环的关键）

改字幕重跑是真实工作流（M7/CCTV 都是"ASR → 人改 → 重跑"），但 M8 的缓存只看
"产物文件在不在"：

| 问题 | 修法 |
|---|---|
| 人改了 `asr/source.srt`，`translate.json` 还在 → 重跑仍用旧译文 | 阶段缓存增加 **上游文件 sha256 比对**：`merge/<input>`、`translate/<input>`、`build/<source,target>`、`dub/<srt>` 任何一环变了就自动重算该阶段 |
| `asr` 命中缓存时，报告里的 cue 数取的是旧 `asr.json`（校对合并过 cue 后会对不上） | 命中缓存时直接用 `load_srt` 数当前文件的 cue 数 |

效果：校对完 SRT 直接重跑，ASR 不重跑，merge/translate/build/dub 自动失效重算，
**不用再手动删目录或 `--force`**。

---

## 3. B 项实现：混音总线 44.1kHz 立体声

`cuesmith/bgm.py`：

- `BgmOptions.sample_rate` → **`mix_sample_rate=44100` + `mix_channels=2`**（仍可显式降级，
  测试覆盖 24k 单声道路径）；
- 新增 `_mix_bus_format()`：两条输入先 `aformat=sample_rates=44100:channel_layouts=stereo`，
  **再做 loudnorm、volume、sidechaincompress、amix**——这样格式协商发生在混音之前，
  不会被第一条输入（24k 单声道配音）带塌；
- 输出 `-ar 44100 -ac 2`；`dub` 传 `mix_sample_rate=max(44100, sample_rate)`；
- 报告写明格式：`dub_report.json` 的 `config.bgm.mix_sample_rate/mix_channels`、
  `summary.bgm.mix_sample_rate/mix_channels`；CLI 打印"混音格式 44100Hz/立体声"；
- 成本：`mixed.wav` 从 ~1.7MB 变 ~13.6MB（154s × 176.4KB/s），成片 AAC 仍是 192k 不变。

---

## 4. 真实素材实测（同一条片、同一份校对版 ASR，A/B 前后对照）

命令：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."   # 只在当前 shell

python -m cuesmith pipeline `
  --src "D:\软件\下载\央视科普-1.mp4" --out work/cctv-demo `
  --src-lang zh --tgt-lang en --llm `
  --asr-python "spike/pyvideotrans/.venv/Scripts/python.exe" `
  --tts-python "spike/pyvideotrans/.venv/Scripts/python.exe" `
  --keep-bgm
```

`probe`/`asr`（medium，227.9s）命中缓存，只重跑 merge 之后的阶段：**150.4s**。

| 指标 | M8（无合并、24k 单声道） | **M9（合并 15s + 44.1k 立体声）** |
|---|---|---|
| ASR 输入 | 21 条（人工校对版） | 21 条（同一份） |
| merge | —（未接） | **21 → 15 条（0.0s，合并掉 6 条）** |
| 翻译 | 21 条 / 26.4s / 8481 token | **15 条 / 22.9s / 7027 token（-17%）** |
| CPS | 14.14（超限 0/21，峰值 19.67） | **13.34（超限 0/15，峰值 17.25）** |
| 精简/审校 | 触发 1 / 改 5 条 | **触发 0 / 改 4 条** |
| 排版 | 21 → 38 条，0 违规 | **15 → 36 条，0 error / 0 warning** |
| 配音 | 21/21，人工 0，atempo **7**，吸收 1 | **15/15，人工 0，atempo 0，吸收 0**（最大溢出 -0.8%，全部装得下） |
| dub 耗时 | 136.6s（BGM 106.0s） | **124.2s（BGM 111.5s）** |
| 混音格式 | 24000Hz/**单声道** | **44100Hz/立体声**，-16.16 LUFS |
| 成品 | 154.00s h264 + aac 24k | **154.00s h264 + aac 44.1k/立体声** |
| 全链路 | 166.3s | **150.4s**；再跑一次 **0.33s 全缓存** |

### 4.1 A 项效果：句子通了

| 位置 | M8 | M9 |
|---|---|---|
| cue 7 | `To survive, they wage.` / `a battle for salt.`（两条） | `To survive, they wage battles over salt.`（一条） |
| cue 8 | `…, leaving plants.` / `with very little salt. …` | `…, leaving plants with very little salt.` |
| 配音 | 21 条碎片句子，7 条要 atempo 压 | 15 条完整句子，**atempo 0**（槽位天然够） |

翻译层拿到完整句子后，CPS 反而更低（13.34 vs 14.14）、token 更省（-17%）、
审校改动更少（4 vs 5）——合并顺带把"半句翻译+压语速"的两个成本一起消掉了。

### 4.2 B 项效果：伴奏不再被砍

| 项 | 数值 |
|---|---|
| 原片音轨 | 44.1k 立体声，-12.1dB mean / **-8.55 LUFS** |
| demucs 伴奏轨 | -26.2dB mean（音乐/环境声，111.5s 分离） |
| 混音总线 | `aformat=44100/stereo` → 侧链 12dB → `amix` → loudnorm |
| 成片音轨 | `mixed.wav` pcm_s16le **44100Hz/2ch**；`marked.mp4` aac **44100Hz/2ch**，-16.16 LUFS |
| 成品时长 | 154.00s（与源一致） |

---

## 5. 测试与回归

`464 passed`（原 455，+9）：

| 文件 | 新增 |
|---|---|
| `tests/test_pipeline.py` | ASR 路线先合并再翻译（两条碎片 → 1 条，translate/build 都吃 `merge/merged.srt`）；内封字幕路线跳过；`--no-merge` 保留碎片；**上游 SRT 变了 → 下游缓存失效**；缓存测试补齐 sha256；CLI `--no-merge` 与默认值 |
| `tests/test_merge.py` | `run_merge` 落盘/报告（含 input sha256、before/after）；缺文件/空字幕报错 |
| `tests/test_bgm.py` | 混音输出 44.1k 立体声；可显式降级 24k 单声道；混音图包含两条 `aformat`；报告记录格式；`mix_channels` 校验 |

一键回归 `python tools/run_samples.py`：**26/26 通过，18.7s**（pipeline 两项检查同步
断言 `merge` 阶段存在且内封字幕路线为 `skipped`）。

真实素材闭环验收：

1. 校对版 ASR → 一条命令：merge 21→15、翻译 0 超限、排版 0 违规、配音 15/15 人工 0、
   44.1k 立体声混音、标识 ✅；
2. 同命令再跑：**0.33s，7 个阶段全部 cached**；
3. 人工改 `asr/source.srt` 后重跑：asr cached，merge/translate 自动重跑（测试覆盖）。

---

## 6. 边界与遗留（如实记录）

| 项 | 说明 |
|---|---|
| 残留跨 cue 半句 | 合并解决的是"相邻碎片被迫分开"，**解决不了"ASR 没给句末标点"**。本片仍有 1 处真实半句（`…补充自己体内的` / `盐分，只好另辟蹊径。`）——whisper 中文段尾缺标点，纯规则无法判断句界。彻底解法是标点恢复/LLM 断句（会把文本外送、增加 token），另立任务 |
| 合并上限 15s | 是"两个 7s 碎片"的工程上限，不是语言上限。超长句仍会分两条；本片配音 atempo 0，未出现压语速 |
| 内封字幕不合并 | 人工断句的字幕强行合并会把不同说话人粘一起；需要时用 `merge` 子命令单独处理 |
| 缓存失效范围 | 只覆盖字幕类上游文件的 sha256；改音色/语速/BGM 参数不会自动失效，仍用 `--force` |
| BGM 默认仍关 | 分离 1.2× 实时，`--keep-bgm` 显式开（成本考虑，未变） |
| 44.1k 混音体积 | `mixed.wav` 约 13.6MB/154s；交付成片是 AAC 192k，不影响分发体积 |

---

## 7. 关键文件

| 文件 | 变更 |
|---|---|
| `cuesmith/merge.py` | `MergeError` / `MergeResult` / `run_merge`（报告 + input sha256） |
| `cuesmith/pipeline.py` | `merge` 阶段；`merge/--no-merge`、`--merge-max-duration/gap`；缓存 sha256 校验（merge/translate/build/dub）；asr cached cue 数以文件为准 |
| `cuesmith/cli.py` | pipeline 新参数与打印；`merge` 子命令改用 `run_merge`；dub 打印混音格式 |
| `cuesmith/bgm.py` | `mix_sample_rate/mix_channels`、`_mix_bus_format()`、44.1k 立体声输出；`BgmResult` 带格式 |
| `cuesmith/dub.py` | 混音格式透传与报告 |
| `tests/` | 见 §5 |
| `work/cctv-demo/` | M9 复跑产物 + `before-m9/`（M8 对照） |
