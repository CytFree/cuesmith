# OPT-8 实现说明：GUI 任务历史（sqlite 持久化 + 载入/重跑）

> 对应待办：[待办-优化项.md](待办-优化项.md) 的 **P3 OPT-8**。
> 完成日期：2026-11-03。测试 **709 passed**、回归 **27/27**、GUI 无头冒烟 HTTP 200、真实 gradio_client 端到端验证通过。

---

## 1. 一句话结论

GUI 每次运行（批量里每条视频算一条）都会把**源文件 / 输出目录 / 表单参数 / 实际 argv / 状态 / 退出码 / 成本 / 逐阶段状态 / 说话人数 / 墙钟 / 错误摘要**写进 `work/history.db`（sqlite，标准库）。新增「任务历史」页：刷新、**载入参数到表单**、**重跑选中**、删除记录；关掉页面/重启 GUI 后照样能查、能重跑。

实测：真实 HTTP 跑 `S1_softsub.mkv` → 历史新增 1 条（完成、¥0、2.2s）；「重跑」生成新记录（`replay_of=原 id`）；刷新返回 **7 行 / 7 个下拉选项**；停掉服务器后 CLI `history --list` 仍能列出 7 条记录。

---

## 2. 数据层：`submatrix/history.py`（新模块，纯标准库）

### 2.1 存储位置

| 优先级 | 路径 |
|---|---|
| 显式 `--db` | 任意 |
| 环境变量 `SUBMATRIX_HISTORY_DB` | 任意 |
| 默认 | `work/history.db`（仓库内，不进分发） |

### 2.2 表结构（`tasks`）

| 列 | 说明 |
|---|---|
| `id` | 自增主键（GUI 里显示为 #ID） |
| `created_at` / `updated_at` | ISO 时间（本地时区） |
| `source` / `out_dir` | 源文件、输出目录（绝对/相对按用户输入） |
| `status` | `running` / `ok` / `partial` / `failed` / `cancelled` |
| `exit_code` | pipeline 退出码（0/2/1…） |
| `settings` | GUI 表单参数 JSON（src_lang/tgt_lang/features/voice/speakers/extra_args/…） |
| `argv` | **实际执行的 pipeline 参数**（`["-m","submatrix","pipeline",…]`），重跑按它原样执行 |
| `cost` | `pipeline.json` 的 cost 块（含 display 金额） |
| `stages` | 逐阶段状态 `{probe: ok, diarize: ok, …}` |
| `speakers` | 说话人数（多人声时） |
| `wall_s` | pipeline 墙钟 |
| `error` | 失败摘要（优先取失败阶段 `stage: error`，其次 pipeline.status） |
| `replay_of` | 由哪条记录重跑而来 |

写入时机：进程启动时 `INSERT`（`running`）→ 结束时 `UPDATE`（状态/成本/阶段/人数/用时/错误）。批量时 **每条视频一行**。所有写库异常只记进日志（`[history] 写入任务失败…`），**绝不影响主流程**。

### 2.3 安全与取舍

- **不存 API Key**：`settings` 里没有 DEEPSEEK/DASHSCOPE Key（Key 只注入子进程环境变量），重跑 LLM 任务需要重新填；
- `settings.features` 是 set → 存成排序 list（`build_argv` 接受任意可迭代）；`Path` → str；
- 每次操作新开连接（本地单用户足够），`timeout=10s`；两个 GUI 实例不会互相写坏；
- 数据库损坏/路径不可写 → `HistoryError`，GUI 显示"历史库不可用（GUI 仍可运行，只是不记历史）"。

---

## 3. GUI：`任务历史` 页 + 与 `RunController` 复用

### 3.1 页面

```
[任务历史（关页面也在，可载入/重跑）]
  刷新 | 选择任务（ID · 文件 · 时间，也可直接输 ID） | 载入参数到表单 | 重跑选中 | 删除记录
  历史任务表（最新在上）：ID / 时间 / 来源 / 输出目录 / 状态 / 人数 / 成本 / 用时
  说明行：共 N 条记录 · 数据库 work/history.db
```

- 打开页面时 `app.load` 自动刷新一次（服务端重启后能直接看到旧记录）；
- 普通运行/重配音色跑完后 `.then` 自动刷新历史页；
- 「载入参数到表单」把 14 个表单字段（路径/输出/语言/功能/音色/人数/额外参数/ASR 模型/TTS Python/多人声音色/force/regenerate/术语表）回填，然后点「开始」即可按原参数重跑（**不占用新的「重跑」语义**——载入后可以改参数再跑）；
- 「重跑选中」直接用历史里记录的 **argv** 执行，最忠实；新记录带 `replay_of`；重跑前会检查源文件是否还存在；
- 「删除记录」只删历史行，不动已产出文件。

### 3.2 与 `RunController.last_plans` 复用

`stream_rerun()` 跑完会把 `(source, out_dir, settings)` 写回 `RunController.last_plans` / `last_env`，所以**重跑之后仍然可以接着「重配音色」**（只换音色、吃 ASR/翻译缓存）。

### 3.3 API 名称稳定

原有 `stream_run` / `stream_revoice` 用显式 `api_name` 保住（M14 的真实批量脚本 `work/real-samples/gui_batch_real.py` 走 `/stream_run` 不受影响）；新增 `history_refresh` / `rerun_history`。

### 3.4 CLI 入口：`history`

不开页面也能查/清理（便于对账与自动化）：

```powershell
python -m submatrix history --limit 20                 # 表格
python -m submatrix history --json                    # JSON
python -m submatrix history --delete 7                # 删一条记录（不动产物）
python -m submatrix history --db D:\tmp\history.db    # 指定库
```

---

## 4. 真实验证（2026-11-03）

环境：Windows + CPU；GUI 用仓库自带 `spike/gui/.venv`（gradio 6.30.0），`http://127.0.0.1:7865` 真实 HTTP，脚本 [tools/gui_history_real.py](tools/gui_history_real.py)。

| 步骤 | 结果 |
|---|---|
| GUI 无头冒烟 | HTTP **200**（142,716 bytes），`tools/gui_smoke.py` 通过 |
| 真实运行（S1 内封字幕 41s，zh→en，只做字幕+标识） | 状态"完成：1/1"、¥0；历史新增 1 条：`完成 / ¥0 / 2.2s`、`argv` 记录完整 |
| 批量/重复运行 | 第 2 次起全部阶段 cached（wall 0.0s），历史仍逐条记录 |
| 「重跑选中」 | 生成新记录 `#7 status=ok replay_of=6`，argv 与历史一致 |
| 「刷新」 | 返回 **7 行表格 / 7 个下拉选项** + "共 7 条记录 · 数据库 …\work\history.db" |
| 「载入参数到表单」 | 返回 "已载入任务 #6（S1_softsub.mkv）；点「开始」按原参数重跑" |
| 服务器重启 | 重启前后记录都在（第一次 e2e 的 #1 在重启后仍被列出），证明是落盘而不是内存 |
| 停服务器后 CLI | `python -m submatrix history --limit 10` → **7 条记录**（含状态/成本/用时），`--json` 可机读 |

证据：`work/gui-opt8-e2e/report.json`、`work/history.db`、`work/gui-opt8-e2e/run/pipeline.json`。

---

## 5. 测试

| 测试 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_history.py`（新增） | 9 | 路径解析、状态映射、增删改查、JSON 往返（set/Path）、失败/部分失败/取消、排序/limit/delete、坏路径报错、下拉选项往返、CLI `history`（表格/JSON/删除/不存在） |
| `tests/test_gui.py`（+6） | 34 | `task_form_values` 回填、历史不可用降级、`stream_run` 写历史（ok / partial+error+stages+speakers+wall+cost）、`stream_rerun` 按历史 argv 重跑并记 `replay_of`、非法选择提示 |

全量：`python -m pytest` → **709 passed**；`python tools\run_samples.py` → **27/27（38.6s）**。

---

## 6. 已知限制（真话）

1. **不是后台任务队列**：这次做的是"历史持久化 + 重跑"，**关闭页面仍会终止正在跑的子进程**（与 M14 的「停止」行为一致）。要做"关页面继续跑/服务端常驻队列"需要改成常驻调度进程，属下一步；
2. 重跑按历史 argv 原样执行：源文件被移动/删除会明确提示；`--tts-python` 等环境路径变了也可能失败（会如实报错，不会悄悄换参数）；
3. **不存 Key**：重跑 LLM 任务要重新填 Key；
4. 历史库不会自动清理（每次运行一行，很小）；用「删除记录」或 `history --delete` 手动清；
5. 成本/阶段信息来自 `pipeline.json`：如果子进程被强杀在写报告之前，记录会停在 `running`（页面显示"运行中"），可手动删除。

---

## 7. 复跑命令

```powershell
# 1) 启动 GUI（仓库自带 gradio venv）
spike\gui\.venv\Scripts\python.exe -m submatrix gui --port 7865 --no-browser

# 2) 另开一个终端跑真实 HTTP 验证（跑完打印报告）
spike\gui\.venv\Scripts\python.exe tools\gui_history_real.py --port 7865

# 3) 停掉服务器后，用 CLI 看落盘记录
python -m submatrix history --limit 10
python -m submatrix history --json
```

产物：`work/gui-opt8-e2e/report.json`（run/rerun/refresh/load 全链路结果）、`work/history.db`。
