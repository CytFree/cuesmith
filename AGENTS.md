# AGENTS.md · 给 AI 协作者的入口

本仓库是 Python 项目：视频字幕提取 / 翻译 / 配音 / 成片（`submatrix/` 是主包）。
用户文档见 `README.md`，其余文档都在 `docs/`。

**写或改任何代码前，先读 [`docs/代码规范.md`](docs/代码规范.md)。** 冲突时以它为准；
它和代码不一致时，要么改代码，要么改规范并单独提交，不许两套并行。

## 十条硬规则（违反即返工）

1. **主包零第三方依赖**：`submatrix/` 只用标准库 + 外部 `ffmpeg`/`ffprobe`；
   重依赖（numpy / torch / faster_whisper / sherpa_onnx / gradio…）只能在 `*_worker.py`
   里 import，且只能跑在子进程。
2. **三个接缝只走一个入口**：跑 ffmpeg/ffprobe → `submatrix/media.py`；找解释器 →
   `submatrix/interpreters.py`；起常驻 worker → `submatrix/worker_protocol.py`。
   别处不许 `shutil.which`、不许手拼 ffmpeg 命令。
3. **依赖单向**（分层表见规范 §1.1），不许有环；新增一条跨层依赖要同时改规范和
   `tests/test_structure.py`，让它出现在 diff 里。
4. **长函数要拆**：>150 行必须拆成"状态对象 + 阶段函数"，或登记进规范 §10.2 的豁免名单；
   跨阶段变量 ≥3 个就该有状态对象，不许用多张平行 dict 记同一条目的状态。
5. **异常不许含糊**：不许裸 `except`、不许 `except Exception: pass`；失败不许当成功
   （`status` / `exit_code` / 报告 `errors` 必须一致）；内部不变量用"内部错误："前缀。
6. **新增 CLI 参数要动全链路**：options → `_add_*_args` →（pipeline 还需
   `_CLI_ONLY_ARGS` / `_EXPANDED_ARGS`）→ 缓存键 → 报告 `config` → GUI/README。
   缓存键漏掉新参数就是 bug，除非按规范 §10.4 登记"有意不进键"。
7. **库模块不许 `print` 到 stdout**（`cli.py` 除外）。
8. **不靠压制告警过关**：禁止新增 `# type: ignore`（基线见规范 §10），报告 JSON 边界
   用 TypedDict/小类型收窄。
9. **测试不联网、不依赖本机状态**：外部引擎注入假对象；真实素材用例用环境变量开关并写
   `skip(reason=...)`。
10. **证据要求**：修 bug 必须带一个先红后绿的用例；重构必须带等价性对照（规范 §7.6：
    旧实现副本 + 覆盖矩阵 + 逐字节比报告/SRT + 变异验证）。

## 提交前必跑

```powershell
python -m pytest -q                     # 全量；含 tests/test_structure.py 的结构检查
python -m pyflakes submatrix tests      # 必须 0 告警
```

## 提交信息

`type(scope): 中文摘要`，正文写清：**为什么**（原来的问题）/ 改了什么 / **怎么验的**
（命令 + 真实数字）/ 有意不做（排除项与理由）。一次提交一件事，重构与行为变更分开。

## 不要做

- 不要提交 `work/`、`spike/` 下的产物、模型、临时脚本（`.gitignore` 已忽略，别强行 `add -f`）；
- 不要在测试里发网络请求，也不要让测试依赖"本机恰好装了什么"；
- 不要在缺少等价性证据时"顺手重构"大函数 —— 先建基线，再动手；
- 不要把 `docs/` 里的文档搬回仓库根目录（README 与 AGENTS 是唯二例外）。
