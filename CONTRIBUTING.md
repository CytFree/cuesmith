# 贡献指南

先把两件事读完，再动代码：

1. [`AGENTS.md`](AGENTS.md) —— 十条硬规则（一屏，AI 与人都适用）；
2. [`docs/代码规范.md`](docs/代码规范.md) —— 分层、编排骨架、错误/缓存/CLI/测试/提交的完整约定。

规范和代码不一致时，**要么改代码、要么改规范并单独提交**，不许两套并行。

## 开发环境

```powershell
git clone <repo> ; cd <repo>
python -m pip install -e ".[dev]"    # pytest / pytest-cov / ruff
python -m pytest -q                  # 858 个用例（854 通过 / 4 按需跳过）
```

主包零第三方运行时依赖（只用标准库 + 外部 ffmpeg）。ASR / TTS / 说话人分离 / BGM 分离 /
GUI 的依赖各自装在 `spike/*/.venv` 里，通过子进程调用——**不要**把它们 import 进 `submatrix/`。

想跑真实素材回归（默认跳过）：

```powershell
$env:SUBMATRIX_REAL_VIDEO = "D:\...\你的视频.mp4"
$env:SUBMATRIX_REAL_OUT   = "work/cctv-demo"     # 已有产物树
$env:SUBMATRIX_REAL_E2E   = "1"                  # 全链路（约 3–5 分钟，含真 ASR）
python -m pytest tests/test_real_sample.py -q -m slow
```

> 若把 `%TEMP%` 指到别处导致子进程写临时文件被拒（受限沙箱环境常见），
> 把 `TEMP`/`TMP` 设到仓库内的 `work/_tmp` 再跑。参见 README §3.5。

## 提交前必跑

```powershell
python -m pytest -q                     # 全量（含 tests/test_structure.py 的 10 条结构检查）
python -m ruff check submatrix tests    # 必须 0（E4/E7/E9/F/I/UP；F 即原 pyflakes）
python -m pytest -q --cov=submatrix --cov-fail-under=80   # 改了较大一块逻辑时
```

## 改代码时的三条硬要求

1. **新增依赖边要显式**：改 `docs/代码规范.md §1.1` 的分层表与
   `tests/test_structure.py` 的 `EXPECTED_EDGES`，让它在 diff 里被看见。
2. **修 bug 带一个先红后绿的用例**；只改代码不补测试视为未完成。
3. **行为中立的拆分要给等价性证据**（规范 §7.6）：旧实现副本 + 覆盖矩阵 +
   逐字节比报告/产物 + 变异验证。模板见 `work/_scratch/eq_translate.py`（不入库）。

## 提交信息

`type(scope): 中文摘要`，正文写清：**为什么**（原来的问题）/ 改了什么 / **怎么验的**
（命令 + 真实数字）/ 有意不做（排除项与理由）。一次提交一件事，重构与行为变更分开。

变更记录请同步更新 [`CHANGELOG.md`](CHANGELOG.md) 的 `Unreleased` 段。

## 不要做

- 不要提交 `work/`、`spike/` 下的产物、模型、临时脚本（`.gitignore` 已忽略）；
- 不要在测试里发网络请求，也不要让测试依赖"本机恰好装了什么"；
- 不要为了通过检查而加 `# type: ignore`（基线是 0，加一处 `test_structure.py` 就红）；
- 不要把 `docs/` 里的文档搬回仓库根目录（`README.md` / `AGENTS.md` / `CHANGELOG.md` /
  `CONTRIBUTING.md` / `LICENSE` 是例外）。
