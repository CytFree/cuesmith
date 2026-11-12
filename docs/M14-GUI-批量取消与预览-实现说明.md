# M14 · GUI 增强：批量、取消、成片预览 — 实现说明

> M13 的 GUI 只能"跑一条、等它跑完、不能停"。M14 把它补成能日常干活的样子：
> **批量队列、停止按钮、成片预览、缓存/术语表开关**。
>
> 边界不变：**GUI 仍是 CLI 的薄壳**，所有执行都走 `python -m submatrix pipeline`，
> 不复制任何编排/翻译/配音逻辑。
>
> 结果：**588 个测试全绿（+7）**、**27/27 一键回归通过（36.3s）**、GUI 无头冒烟 HTTP 200、
> 真实双文件批量 2/2、运行中停止实测生效且无残留子进程。

---

## 1. 本次新增

| 能力 | 交互 | 实现 |
|---|---|---|
| **批量** | 路径框一行一个，或上传多个视频 | `resolve_sources()` 解析/去重/展开环境变量；`batch_out_dirs()`：单条用给定目录，多条在目录下按 `01-文件名` 分子目录；逐个串行跑，每条各自写 `pipeline.json` |
| **取消** | 「停止」按钮 | `RunController` 保存子进程句柄；停止时 Windows `taskkill /PID <pid> /T /F`（杀整棵 ASR/TTS 进程树），POSIX `killpg`；生成器每行检查取消标记，状态标「已取消」，已落盘产物保留 |
| **成片预览** | 内嵌视频播放器 | 取 `pipeline.json.outputs.final_video`（回退 `burned_video`/`dub_video`）；批量显示最近一条 |
| **批量结果表** | 文件 / 状态 / LLM tokens / TTS 字符 / 成本 / 成片 | `batch_result_rows()`；单条完成即刷新 |
| **成本明细表** | 文件 / 类别 / 项目 / 数量 / 单价 / 金额 | `cost_table_rows()` 复用 M13 的 `cost_rows`，前面加「文件」列 |
| **缓存开关** | 「强制重跑」「重新合成配音」 | 分别透传 `--force` / `--regenerate-tts`（重新合成会重新计费，标签已提示） |
| **术语表上传** | 单个 .json/.tsv/.txt | 透传 `--glossary <上传路径>` |
| **预览目录** | CLI `--allowed-path`（可多次） | Gradio 默认只允许仓库 + 用户主目录下的文件预览/下载；输出到其他盘时加此参数 |

---

## 2. 设计要点

### 2.1 批量：串行、互不污染

```
路径框（多行）/ 多文件上传
  → resolve_sources()：展开 $ENV / ~，去重
  → batch_out_dirs()：out 为空 → 每条 work/gui/<名>-<时间戳>；给了 out → 多条走 out/01-<名>
  → for 每条：build_argv() → Popen → 实时日志 → 读该目录 pipeline.json
```

- 每条独立输出目录、独立报告，失败一条不影响后面的（继续跑，结果表标失败）；
- CPU 环境下并行跑多条只会互相抢核，**故意串行**；
- 批量时状态行显示 `[2/5] 文件名`，结果表逐条追加。

### 2.2 取消：整棵进程树

pipeline 会再起 ASR/配音/云端 TTS 子进程，只杀父进程会留下孤儿任务，所以：

```python
if os.name == "nt":
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], ...)
else:
    os.killpg(os.getpgid(pid), signal.SIGTERM)   # Popen(start_new_session=True)
```

- 「停止」是**独立的 Gradio 事件**，与运行中的生成器并发执行；
- 生成器读到取消标记后主动 `break`，不再继续下一条；
- 已完成的阶段产物保留（重跑会自动命中缓存）；
- 本地单用户，`RunController` 全局一份，一次只允许一个任务（第二次点开始会提示先停止）。

### 2.3 UI 输出契约

一次生成器 yield 六元组：`status / log / files / batch_table / cost_table / preview`。
这样停止按钮只需更新 `status`，运行中每一步都能刷新日志、结果表与预览。

---

## 3. 验证记录

| 项 | 结果 |
|---|---|
| 主测试 | **599 passed**（M13 的 581 + M14 GUI 7 + M14.1 修复 11） |
| 一键回归 | **27/27 通过，97.1s** |
| GUI 冒烟 | `tools/gui_smoke.py`：gradio 6.30.0，HTTP **200**，65,109 bytes |
| 真实批量 | 两条 2s 内封字幕 MKV（en→zh，`--provider echo`，仅到排版）：**完成 2/2**；输出 `work/gui-smoke/batch-out/01-a`、`02-b`；各 4 个产物；结果表 2 行、成本 ¥0 |
| 真实取消 | 跑 `demo2.mp4`（ASR 中）6s 后点停止：`running_before_stop=True` → 「已请求停止」→ 线程结束，状态「已取消：完成 0/1」，`taskkill` 后**无残留 python 进程** |
| GUI venv 执行 | 批量与取消均在 `spike/gui/.venv` 下跑通（子进程用 venv python 调 CLI，行为同系统 Python） |

新增测试覆盖：多行/多文件解析与去重、批量输出目录规则、`--force/--regenerate-tts/--glossary` 参数、停止控制器（无任务/有任务）、批量结果表与成本明细表、`--allowed-path` 解析、假子进程下 2 条批量全流程。

---

## 4. 文件清单

| 文件 | 变更 |
|---|---|
| `submatrix/gui.py` | 重构：`RunController` / 批量解析 / 停止 / 预览 / 结果表 / 缓存开关（约 550 行）；M14.1 增加 ASR 模型 / TTS Python 入口 |
| `submatrix/tts.py` | 新增 `find_edge_tts_python()`（M14.1） |
| `submatrix/dub.py` | Edge 引擎默认自动探测解释器；报告记录 `tts_python`（M14.1） |
| `submatrix/pipeline.py` | dub `partial` 不当缓存；显式 `--asr-model` 变化使 ASR 缓存失效（M14.1） |
| `submatrix/bgm.py` | demucs 伴奏轨按源片 sha256 复用（M14.1） |
| `tests/test_tts.py` | 新增 4 个（M14.1） |
| `tests/test_bgm.py` | +2 伴奏复用（M14.1） |
| `tests/test_dub.py` | +2 解释器探测（M14.1） |
| `tests/test_pipeline.py` | +2 缓存失效（M14.1） |
| `submatrix/cost.py` | 新增 `format_amount()`（批量表用） |
| `submatrix/cli.py` | `gui --allowed-path`，重入时透传 |
| `tests/test_gui.py` | 11 → 18 个测试 |
| `tests/test_cost.py` | 增加 `format_amount` 断言 |
| `tools/gui_smoke.py` | 不变（继续做无头冒烟） |
| 本文档 + PRD | 状态/AC-32/M14 行/引用 |

---

## 5. 已知限制

1. **单用户单任务**：`RunController` 是全局一份，多标签页同时点开始只有第一个能跑；本地工具够用。
2. **批量串行**：没有并发队列/重试编排；要并行请开多个 GUI 端口或 CLI。
3. **取消后报告可能不完整**：pipeline 未走完不会写最终 `pipeline.json`，结果表该条为「已取消」，但已落盘产物仍在。
4. **预览只看最近一条成片**；字幕-only 任务没有成片，预览为空（产物列表仍可下载）。
5. 没有任务历史/持久化队列/断点按钮；重跑同一目录会自动命中 CLI 阶段缓存。
6. 输出到仓库与用户主目录之外时，预览/下载需 `--allowed-path` 放行。

---

## 6. M14.1 修复：第一次真实 GUI 跑暴露的问题

### 6.1 现象

用 GUI 跑 `央视科普-1.mp4`（154s，auto→en，保留 BGM）：ASR/翻译/排版/烧录都成功，
但 **配音 0/15 全部失败**，链路 `partial`（退出码 2），用户拿不到配音成片。

### 6.2 根因（四处）

| # | 问题 | 后果 |
|---|---|---|
| 1 | `EdgeTtsEngine` 默认用 `sys.executable`；GUI 跑在 `spike/gui/.venv`，**这个 venv 没装 edge-tts**（只有 `spike/.venv311`、`pyvideotrans/.venv` 装了） | 15 条全部 `No module named edge_tts`，重试 3 次仍失败 |
| 2 | pipeline 的 dub 缓存只看输入 sha / 音色 / 命令，**不看上次状态** | 修好 TTS 后重跑仍会把 `partial` 当缓存，永远 0/15（只能手动删目录） |
| 3 | ASR 缓存只看输入 sha，**换了 `--asr-model` 也复用旧结果** | GUI 加模型选择后，用户改 medium 也不生效 |
| 4 | 每次重试 dub 都重新跑 demucs（154s 素材约 200s） | 修一个 TTS 问题要等 3–4 分钟，体验差 |

另外：源语言 `auto` 时 ASR 按现状退回 small（中文质量不如 medium），GUI 之前没暴露模型选择。

### 6.3 修复

1. **`tts.find_edge_tts_python()`**：显式 `--tts-python` → `SUBMATRIX_TTS_PYTHON` →
   当前解释器 → `spike/.venv311` → `spike/pyvideotrans/.venv` → 仓库 `.venv`，
   逐个 `import edge_tts` 探测；`build_engine` 默认用它（显式值不探测，尊重用户）。
   dub 报告记录实际解释器 `tts_python`，排障不再靠猜。
2. **dub 缓存加 status 判据**：上次 `partial`/`failed` 必须重跑；旧报告没有 status 字段按 ok 兼容。
3. **ASR 缓存加模型判据**：显式 `--asr-model small/medium/路径` 与报告 `model_variant` 不一致 → 重跑 ASR。
4. **demucs 伴奏复用**：`demucs-source.sha256` 校验源片内容，源片没变直接复用
   `<out>/demucs/<模型>/<音轨>/no_vocals.wav`，dub 重试不再白跑分离。
5. **GUI 高级新增**：ASR 模型（auto/small/medium）、TTS Python（留空自动）；
   `--asr-model` 变化会正确触发 ASR 重算。

### 6.4 复跑结果（同一目录、同一素材）

```
[ cached] probe/asr/merge/translate/build   （ASR 仍是之前 small 的 23 条）
[     ok] dub    67.5s  15/15 成功 人工 1  BGM separate
[     ok] burn    4.6s  烧录 34 条 核验 True
状态：ok（耗时 72.2s）
[warn] 复用同目录已分离的伴奏轨（demucs 结果缓存，源片未变）
成本：≈¥0（TTS 1,687 字符；Edge-TTS 免费）
```

- 修复前：**0/15**、partial、退出码 2；修复后：**15/15**、ok、退出码 0；
- 未重跑 ASR/翻译（缓存命中），BGM 未重跑 demucs（从 242s dub 降到 67.5s）；
- 成片：`work/gui/央视科普-1-20261026-170251/burn/burned.mp4`（另有 en/双语字幕）。

### 6.5 验证

| 项 | 结果 |
|---|---|
| 主测试 | **600 passed**（+12：tts 4 / dub 2 / bgm 2 / pipeline 2 / gui 2） |
| 一键回归 | **27/27，97.1s** |
| GUI 冒烟 | gradio 6.30.0，HTTP 200（68,059 bytes，含新控件） |
| 服务重启 | 已用修复后代码重启 `http://127.0.0.1:7860/`，HTTP 200 |
| 真实接口调用 | `gradio_client` 调 `/stream_run`（16 个 inputs）返回「参数错误」而非 `AttributeError` |

> 提醒：这次复跑沿用旧的 small ASR。中文素材建议 GUI「源语言」选 `zh`（会自动用 medium），
> 或在高级里把「ASR 模型」设为 `medium` 后重跑；因为模型判据已生效，会重跑 ASR（约 4 分钟）。

### 6.6 二次修复：控件参数位置错位（用户点开始直接报错）

- **现象**：M14.1 加完「ASR 模型 / TTS Python」后，用户刷新页面、点开始，前端直接报
  `AttributeError: 'str' object has no attribute 'running'`。
- **原因**：`build_app` 的 inputs 新增到 16 个，但 `stream_run` 把 `controller` 参数放在
  `asr_model/tts_python` 之前，Gradio 按位置传参把 `asr_model="auto"` 传给了 `controller`。
- **修复**：调整 `stream_run` 形参顺序（`asr_model → tts_python → controller`），与 inputs 严格对齐；
  新增回归测试 `test_stream_run_accepts_full_gradio_input_order`（16 个位置参数直接调用）；
  重启服务后用 `gradio_client` 真实调用 `/stream_run` 验证。
- **教训**：Gradio 薄壳的「输入组件列表 ↔ 函数签名」是隐式契约，加控件必须同时加回归测试；
  以后新增 controls 先写位置调用测试再改 UI。

### 6.7 三次修复：批量结果/成本明细显示 `[object Object]`

- **现象**：用户批量跑完，两张表只显示 `[object Object]`，看不到文件、状态、成本。
- **原因**：Gradio 6 的 `Dataframe` 值只接受二维数组 / DataFrame；我们传的是 `list[dict]`，
  前端把对象直接转成了字符串。
- **修复**：新增 `table_values()`，把行字典按固定表头转成 `list[list[str]]`
  （`None` → 空串），`stream_run` 的批量/成本两个输出都走它；表头抽成
  `BATCH_HEADERS` / `COST_HEADERS` 常量，`Dataframe(datatype=["str"] * N)`。
- **验证**：用 `gradio_client` 真实调用 `/stream_run`（缓存命中，0.6s），返回：
  批量结果 `[['央视科普-1.mp4','完成','7471','421','¥0.01598','...\\marked.mp4']]`；
  成本明细 `输入 tokens 6433/¥0.0129`、`输出 tokens 1038/¥0.0031`、`TTS 421/¥0`。
  文件产物 10 个可下载。测试 601 passed。
