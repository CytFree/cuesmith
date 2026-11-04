# M8 — 一条命令全链路（pipeline）+ 中文默认 medium

> 状态：**已完成**。用户口径："A + B 线做完"——A = 中文源语默认用 medium 模型；
> B = 一条命令跑完全链路。新增 `submatrix/pipeline.py` 与 `pipeline` 子命令。
> 验收：**AC-23**。测试 **455 passed**，一键回归 **26/26（29.5s）**。
>
> **M9 增补**（另见 `M9-句子合并与BGM高清混音-实现说明.md`）：asr 与 translate 之间
> 多了 `merge` 阶段（ASR 碎片按句合并，默认开；内封字幕路线跳过），阶段缓存改为
> 校验上游文件 sha256（人工校对后自动失效）；下文"六阶段/五个阶段"是 M8 当时的记录。
>
> **M10 增补**（另见 `M10-字幕按句切分与硬字幕成片-实现说明.md`）：dub 与 mark 之间
> 多了 `burn` 阶段（默认把字幕烧进成片，`--burn-mode`/`--burn-position` 可调，
> 缓存按视频+字幕 sha256）；mark 改为作用在烧录后的成片上。

---

## 1. 之前的问题

M0–M7 的能力都齐了，但要用起来得手动串 5 条命令：

```powershell
python -m submatrix asr ...        # 或 probe（有内封字幕时）
python -m submatrix translate ...
python -m submatrix build --wrap ...
python -m submatrix dub --keep-bgm ...
python -m submatrix mark ...
```

每条命令的参数、输出目录、下一步该喂哪个文件，都要人去记。改一次字幕重跑，前面
ASR/翻译的重活还得自己判断要不要跳过。这不是"能日常发片"的形态。

---

## 2. A 项：中文源语默认 medium

```python
DEFAULT_MODEL_BY_LANGUAGE = {"zh": "medium", "yue": "medium", "ja": "medium", "ko": "medium"}
```

- `--model` 显式给了就用显式值；否则按 `--language` 挑：中文系 → `medium`，
  其余（含 `auto`）→ `small`；
- **本地没有 medium 就自动退回 small**（不会去 HF 下载、不会卡住）；
- 报告新增 `config.model_variant`（small/medium/large/tiny/custom），pipeline 输出里直接显示。

依据是 M7 实测：中文采访用 small 把「刘翔」识别成「流向」，medium 正确、无需人工修；
代价是耗时约 2 倍（45s 音频 21s → 46s，仍 ≈1× 实时）。

**顺带修掉一个真 bug**：`find_model` 之前把 `spike/whisper-models/models--Systran--...`
当作模型目录，但那是 **HF 缓存布局**（`snapshots/<hash>/model.bin`），CTranslate2
读不到顶层 `model.bin`，跑起来直接 `Unable to open file 'model.bin'`。
现在：
- small 用底座 venv 里的**平铺目录**（一直能用的那个）；
- 新增 `_model_dir_ok()`：平铺目录和 HF `snapshots/*` 布局都能识别；
- 这个 bug 是"一条命令真跑"时暴露的——单条 `asr` 命令此前恰好走到可用路径。

---

## 3. B 项：pipeline 怎么跑

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."    # 只在当前 shell

python -m submatrix pipeline `
  --src work/user-en/seg.mp4 --out work/pipeline-demo `
  --llm --glossary work/tmp/glossary.json `
  --src-lang en --tgt-lang zh `
  --hotwords "AI,OpenAI,Blender,Astra" `
  --asr-python "spike/pyvideotrans/.venv/Scripts/python.exe" `
  --tts-python "spike/pyvideotrans/.venv/Scripts/python.exe"
```

产物目录（每阶段独立、可单独重跑）：

```
out/
├─ probe/      manifest.json（+ 内封字幕直出的 source.srt）
├─ asr/        source.srt / words.json / asr.json        （仅在需要 ASR 时）
├─ merge/      merged.srt / merge.json                    （M9：ASR 碎片按句合并）
├─ translate/  <tgt>.srt / translate.json                ← 配音用句子级译文
├─ matrix/     <tgt>.srt + bilingual.srt/.ass/.vtt + .txt + report.json  ← 发布字幕
├─ dub/        dubbed.wav / dubbed.mp4 / dub_report.json
├─ burn/       burned.mp4 / subtitles.ass / burn.json          （M10：硬字幕成片）
├─ final/      marked.mp4 / mark.json / label_frame.png
└─ pipeline.json                                         ← 总报告
```

### 行为

| 能力 | 说明 |
|---|---|
| 自动分流 | 先 `probe`：有文本字幕流 → 直出不跑 ASR；图形字幕（PGS/VobSub）→ 明确报错；无字幕 → 转 ASR |
| 语言自动 | `--src-lang auto` 时用 ASR 识别结果决定翻译语言（只支持 zh/en；内封字幕路线必须显式指定） |
| 阶段缓存 | 已存在的产物默认复用（第二次跑 0.5s 全命中）；`--force` 强制重跑。M9 起还校验上游字幕 sha256：人工校对过 `asr/source.srt` 后重跑，merge/translate/build/dub 会自动失效重算 |
| 断点 | `--stop-after probe/asr/diarize/merge/translate/build/dub/burn/mark` 只跑到某阶段；失败后修好重跑会复用前面阶段 |
| 两条时间轴 | 配音用 `translate/<tgt>.srt`（句子级），发布字幕用 `matrix/<tgt>.srt`（排版级），不混用 |
| 默认值 | 音色/语速按目标语言（zh：Xiaoxiao +30%；en：Aria +20%）；翻译默认微软免费端点，`--llm` 切 DeepSeek 并默认开双轮审校 |
| 合规 | 默认打 AIGC 标识（`--no-mark` 仅限自用素材，且 mark 层自己会留痕） |
| 失败可诊断 | `pipeline.json` 记录每阶段 `status / wall_s / artifacts / summary / error`；退出码 0=全成、2=部分失败、1=参数/输入错误 |

---

## 4. 实测

### 4.1 真实 2 分钟英文播客（en→zh，DeepSeek）

```
[ cached] probe        0.0s
[     ok] asr         38.2s  small 30 条
[     ok] translate   29.3s  CPS 6.2（超限 1） 精简 5 审校 3 token 9694
[     ok] build        0.0s  排版 30 → 47 条
[     ok] dub         31.7s  30/30 成功 人工 2
[     ok] mark         3.4s  显式 True 隐式 True
状态：ok（耗时 102.8s）
```

- 首次跑（含 probe）约 103s；**第二次同命令 0.5s**，六个阶段全部 `cached`；
- 报告里能直接看到 token（9759）、审校改动数（3）、CPS 超限（1）、排版条数（30→47）、
  配音人工条数（3）、标识核验结果——一条命令把该看的都摆出来了。

> 这次 translate 只有 29.3s（比 M5 的 298s 快一个数量级）：DeepSeek 本次响应快，
> 且 30 条只分 8 段、双轮审校只改 3 条。耗时不承诺，按实测记录。

### 4.2 一键回归（离线，不依赖网络/模型质量）

新增 2 项检查：

| 检查 | 结果 |
|---|---|
| 全链路/一条命令 pipeline | 内封字幕路线（S1_softsub.mkv）：probe ok → asr skipped → translate/build/dub/mark ok，成品存在 |
| 全链路/pipeline 阶段缓存 | 同命令再跑一次：六阶段全部 `cached/skipped` |

**26/26 通过，29.5s**（原 24 项 + 这 2 项）。

---

## 5. 测试

`tests/test_pipeline.py`（18 项）：

- 编排顺序：内封字幕路线跳过 ASR；ASR 路线先 probe 后 asr；
- `--src-lang auto` 用 ASR 识别语言（zh）决定翻译语言；
- 术语表自动并入 ASR 热词；
- 默认音色/语速按目标语言（zh/en 各一组）；
- 阶段缓存（全命中、不调用任何阶段函数）、`--force` 强制重跑；
- `--stop-after`、`--no-mark`；
- 图形字幕 → 退出码 2 + 可读错误；`--llm` 没 key → 在调用翻译前失败；
- CLI 参数默认值与覆盖；
- A 项：`model_variant`、中文默认 medium、本地缺失退回、HF 缓存布局识别。

---

## 6. 边界（明确不做/暂不做）

| 项 | 说明 |
|---|---|
| BGM 默认开 | pipeline 里 `--keep-bgm` 仍是显式开关：分离是 1.2× 实时，默认开会让"一条命令"变慢很多；要常开可加 shell alias |
| 自动删中间产物 | 不删：ASR/翻译产物是人工校对与重跑的基础，留着更值钱 |
| Web UI / 任务队列 | 仍是 CLI；pipeline 已把"日常发片"收敛到一条命令 |
| 人工校对闭环 | `needs_review` 已列出待查条目，人改 SRT 后重跑即可（M9 起上游 sha256 变了会自动失效下游缓存，无需 `--force`） |
| ASR 同音误听 | medium 也会错（M7 §5），中文素材仍建议人工过一眼 |
