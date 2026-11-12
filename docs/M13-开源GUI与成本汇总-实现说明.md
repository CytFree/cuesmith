# M13 · 开源 GUI 薄壳与成本汇总 — 实现说明

> 目标：在 M12（547 测试）之上加两层**薄、通用**的能力——
> ① **成本汇总**：本地把 LLM token / TTS 字符按价表折算成钱，回答"这条视频花了多少"；
> ② **开源 GUI**：用 Gradio 包一层本地 Web 界面，复用现有 CLI，不复制任何业务逻辑。
>
> 结果：**581 个测试全绿（+34）**、**27/27 一键回归通过（43.2s）**、GUI 无头冒烟 HTTP 200、
> 系统 Python 启动自动重入 GUI venv 实测 HTTP 200；真实 M12 产物成本 **≈¥0.0292**。

---

## 1. 范围与边界

| 项 | 决定 |
|---|---|
| GUI 技术 | **Gradio 6.30.0**（开源、单文件、自带日志/文件/表格组件），装在独立 venv `spike/gui/.venv`，不污染主环境 |
| GUI 边界 | **只做壳**：表单 → `python -m submatrix pipeline ...` 子进程 → 实时日志/产物/成本表；不 import 任何业务模块 |
| 成本口径 | **artifact（产物累计）**：按当前产物报告算总花费，缓存重跑数字不变；`--force`/`--regenerate-tts` 重跑不累计历史账单 |
| 成本数据源 | 只读 `translate/translate.json`（token 用量）与 `dub/dub_report.json`（字符/复用/失败），不联网、不调服务商账单 |
| 币种 | 分项保留原币种（CNY/USD），展示金额按 `fx` 汇率折算；缺汇率如实标注、不硬编 |
| 依赖 | 主包仍 **零运行时依赖**；GUI 是可选 extra（`pip install -e .[gui]` 或仓库自带 venv） |

---

## 2. 成本汇总

### 2.1 抽量（collect_quantities）

| 来源 | 抽什么 | 说明 |
|---|---|---|
| `translate/translate.json` → `config.provider` / `config.llm.model,base_url,usage` | `prompt_tokens` / `completion_tokens` / `total_tokens` / `calls` | 只对 `openai`（LLM）渠道计价；`microsoft`/`command` 等如实标 0 |
| `dub/dub_report.json` → `config.engine` + `cues[]` | 每条 `chars`、`reused`、`action=failed` | 字符数含复用音频（产物累计口径），失败条目不计入 |
| TTS 渠道识别 | 子串匹配 `engine + --tts-command` | `dashscope/cosyvoice/bailian/aliyun` → dashscope；`edge` → edge；其余 `unknown`（按 0 元计并提示） |

### 2.2 定价（价表）

内置示例价表在 `submatrix/cost.py` 的 `DEFAULT_PRICING`：

| 渠道 | 单价 | 币种 |
|---|---|---|
| DeepSeek（示例） | 输入 **2 元 / 百万 token**，输出 **3 元 / 百万 token** | CNY |
| 阿里云百炼 CosyVoice | **$0.14335 / 万字符** | USD |
| Edge-TTS / 微软翻译 | 0 | — |
| 汇率 | `fx: {"USD": 7.2}`（1 USD = 7.2 CNY），`fx_base = CNY` | — |

> 价格会变：默认值是**示例价**，以服务商官网为准。用 `--pricing 价表.json` **深合并**覆盖，
> 只需写要改的字段，例如：

```json
{
  "display_currency": "CNY",
  "llm": {"deepseek": {"input_per_million": 2.5, "output_per_million": 8.0}},
  "tts": {"dashscope": {"per_10k_chars": 0.14335}, "mycloud": {"per_10k_chars": 0.5}}
}
```

价表字段：`llm.<渠道>.input_per_million / output_per_million / currency`、
`tts.<渠道>.per_10k_chars / currency`、`fx`、`fx_base`、`display_currency`。

### 2.3 出账结构（`pipeline.json` 的 `cost` 块）

```json
{
  "schema_version": 1,
  "basis": "artifact",
  "pricing": {"source": "builtin", "display_currency": "CNY", "fx_base": "CNY", "fx": {"USD": 7.2}},
  "quantities": {
    "llm": {"provider": "openai", "model": "deepseek-chat", "calls": 2,
             "prompt_tokens": 582, "completion_tokens": 138, "total_tokens": 720},
    "tts": {"engine": "CommandTtsEngine", "provider": "dashscope",
             "cues": 3, "chars": 268, "reused_chars": 158, "failed_cues": 0}
  },
  "items": [
    {"category": "llm", "label": "输入 tokens", "detail": "deepseek-chat",
     "quantity": 582, "unit": "token", "unit_price": 2.0, "price_unit": "1M token",
     "currency": "CNY", "amount": 0.001164},
    {"category": "tts", "label": "TTS 字符", "detail": "dashscope",
     "quantity": 268, "unit": "字符", "unit_price": 0.14335, "price_unit": "1万字符",
     "currency": "USD", "amount": 0.003842}
  ],
  "totals": {"CNY": 0.00156, "USD": 0.003842},
  "display": {"currency": "CNY", "amount": 0.02924, "converted": true},
  "notes": ["TTS 字符含复用已有音频 158 字符（产物累计口径，表示当初已生成）", "..."]
}
```

### 2.4 入口

```powershell
# pipeline 自动写 cost 块并在摘要里打一行
python -m submatrix pipeline --src in.mp4 --out out --llm --tts-command "...{voice_id}..."
# 成本：≈¥0.02924（LLM 720 tokens，TTS 268 字符；按价表估算）

# 单独查/换价表重算
python -m submatrix cost --work out
python -m submatrix cost --work out --json
python -m submatrix cost --work out --pricing my-pricing.json --json
python -m submatrix cost --work old-out --tts-command "tools\tts_dashscope.py"   # 旧产物补识别
```

### 2.5 真实数据

| 素材 | 量 | 金额 |
|---|---|---|
| M12 双人复刻 `work/dashscope/multi` | LLM 720 tokens（582 in / 138 out）；TTS 268 字符（含 158 复用） | **≈¥0.02924**（LLM ¥0.0016 + TTS $0.0038） |
| 同一产物全缓存重跑 | 9 阶段全部 `cached`，0.1s | cost 块数字不变（artifact 口径） |
| 纯本地/免费组合（GUI 实跑） | ASR + echo 翻译（无 LLM/无 TTS） | **¥0**（"无外部计费调用"） |

> 与 M12 文档记录的 274 字符 / $0.0039 的差异：M12 是当时请求文本的字符数，
> 本功能统一以 **dub 报告每条字幕的 `chars`** 为准（268 字符 / $0.0038）。

---

## 3. 开源 GUI 薄壳

### 3.1 结构（`submatrix/gui.py`，约 310 行）

```
表单（视频路径/上传、输出目录、源/目标语言、功能勾选、音色、高级参数、API Key）
  → build_argv()：拼成 pipeline CLI 参数（额外参数 shlex 透传）
  → subprocess.Popen([sys.executable, "-u", "-m", "submatrix", "pipeline", ...])
  → 生成器逐行 yield 日志（Gradio Textbox 实时滚动）
  → 读 pipeline.json：output_files() 产物下载 + read_cost() → cost_rows() 表格
```

- **勾选 → 参数**：生成配音成片（不勾 → `--stop-after build`）、保留 BGM → `--keep-bgm`、
  多人声 → `--diarize`、烧录硬字幕（不勾 → `--no-burn-subtitles`）、AIGC 标识（不勾 → `--no-mark`）、
  LLM 翻译 → `--llm`；音色/说话人数直通。
- **通用性**：一个「额外参数」输入框原样追加给 CLI（`--glossary`、`--burn-position top` 等不用改 GUI），
  以后 CLI 加 flag 也自动可用。
- **API Key**：密码框只注入本次子进程的 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`，不落盘、不回显。
- **安全**：默认 `127.0.0.1`，不做公网 share；Gradio 临时目录固定到 `work/.gui-tmp`
  （避免在仓库根目录生成 `gradio/`，否则会遮蔽真实 gradio 模块）。

### 3.2 启动方式

```powershell
# 仓库自带 GUI venv（本次安装 gradio 6.30.0）
python -m submatrix gui                  # 系统 Python 会自动重入 spike\gui\.venv
python -m submatrix gui --port 7860 --no-browser
# 或手动
spike\gui\.venv\Scripts\python -m submatrix gui
```

`python -m submatrix gui` 会检查当前解释器的 gradio：没有（或只是同名命名空间目录）时，
自动用 `spike/gui/.venv/Scripts/python.exe` 重入并置 `SUBMATRIX_GUI_REEXEC=1` 防循环。

### 3.3 依赖

`pyproject.toml` 增加可选 extra：`gui = ["gradio>=6"]`。
仓库内 venv：`spike/gui/.venv`（gradio 6.30.0 + pandas；约 150MB，不影响主包零依赖）。

---

## 4. 验证记录

| 项 | 结果 |
|---|---|
| 主测试 | **581 passed**（547 + 成本 23 + GUI 11） |
| 一键回归 | **27/27 通过，43.2s** |
| CLI 帮助 | `--help` 已列出 `cost` / `gui` 及说明 |
| GUI 无头冒烟 | `tools/gui_smoke.py`：gradio 6.30.0，HTTP **200**，43,295 bytes，随机端口，自动关闭 |
| 系统 Python 启动 | `python -m submatrix gui --no-browser --port 7863`：自动重入 venv → HTTP **200**（约 14s，含 Gradio 6 SSR 预热） |
| GUI 端到端 | `stream_run` 实跑 `work/multi-speaker/demo2.mp4`（zh→en，stop-after build，`--provider echo`）：状态"完成"，产出 5 个文件，成本表显示"无外部计费调用"（¥0） |
| 真实成本 | `cost --work work/dashscope/multi`：582/138 tokens、268 字符、**≈¥0.02924**，与 `cost --tts-command ...` 重算一致 |
| 缓存一致性 | 真实产物全缓存重跑 0.1s，cost 数字不变 |

测试覆盖要点：

- 抽量拆分（LLM/TTS、复用/失败、provider 识别）；
- 定价（free=0、DeepSeek 按百万 token、DashScope 按万字符、未知渠道 0 + 提示）；
- 多币种与汇率（USD→CNY；改展示币种缺汇率时如实 `converted=false`）；
- 价表深合并、坏价表报错、旧产物 `--tts-command` 补识别、`--pricing` 重算；
- CLI `cost`（文本/JSON/缺文件/坏价表）；
- GUI 纯函数（argv 映射、路径解析、产物/成本读取、参数错误不启子进程、CLI parser）。

---

## 5. 文件清单

| 文件 | 变更 |
|---|---|
| `submatrix/cost.py` | 新增：抽量/价表/出账/CLI 文本/GUI 行 |
| `submatrix/gui.py` | 新增：Gradio Blocks + `build_argv` + `stream_run` + `serve` |
| `submatrix/cli.py` | `pipeline --pricing`；新增 `cost` / `gui` 子命令；摘要打印成本行 |
| `submatrix/pipeline.py` | `PipelineOptions.pricing`；报告写 `cost` 块；`_finish` 计算成本 |
| `tests/test_cost.py` | 新增 23 个 |
| `tests/test_gui.py` | 新增 11 个 |
| `tests/test_pipeline.py` | 断言 cost 块与 `config.pricing` |
| `tools/gui_smoke.py` | 新增：无头 GUI 冒烟 |
| `pyproject.toml` | 可选 extra `gui = ["gradio>=6"]` |
| 本文档 + PRD v1.1 | 状态/AC/M13 行/引用 |

产物：`work/dashscope/multi/pipeline.json` 已写入真实 `cost` 块；
`work/gui-smoke/demo/`（GUI 实跑产物）。

---

## 6. 已知限制与后续

1. **成本是估算**：不读服务商账单；重试/失败请求可能未计入；复用音频按"当初已生成"计入产物累计。
2. **旧产物**没有 `cost` 块时，`cost --work` 会按子报告重算；命令式 TTS 需 `--tts-command` 才能识别付费渠道。
3. ~~**GUI 无取消按钮**~~：M14 已补批量、停止、成片预览；仍无任务历史/持久化队列。
4. GUI 首次启动约 10–15s（Gradio 6 前端/SSR 预热）；仅本机使用，未做鉴权。
5. 价格默认值需随官网更新（或用自己的 `--pricing` 固化）。
6. 未做：批量队列、任务历史、断点续跑按钮（CLI 已支持缓存重跑，GUI 暂未暴露"继续"）。
