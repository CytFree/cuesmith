# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

（暂无）

## [0.1.0] - 2026-11-12

首个 MVP 版本：一条命令从视频走到"带硬字幕 + AIGC 标识的成片"。

### 新增

- **探测与内封字幕直出**（`probe`）：流清单 + 路由判定 + `source.srt`；PGS/VobSub
  位图字幕明确报 `unsupported`，不假装成功。
- **字幕矩阵**（`build` / `matrix`）：单语/双语 SRT、ASS、VTT、TXT + CPS/行宽/行数校验
  报告（只报不改，阈值可配）。
- **ASR**（`asr`）：faster-whisper 词级时间戳、热词偏置、按语义切条；模型跑在独立
  venv 的子进程里，主包只走 JSON 协议。
- **翻译**（`translate`）：免费微软 MT / OpenAI 兼容 LLM / 外部命令 / echo 四渠道；
  术语表占位保护、段落上下文、一句对一句对齐、译后精简、多轮 LLM 审校、
  渠道回退（`ResilientTranslator`）、HTTP 连接复用。
- **说话人分离**（`diarize`）：sherpa-onnx 的 pyannote 分割 + 声纹聚类；VAD 预过滤、
  embedding 二次聚类、短簇合并，按语言给默认阈值。
- **配音**（`dub`）：Edge-TTS / 本地 TTS（Kokoro、MeloTTS）/ 外部命令；并发 + 重试 +
  时长校验 + atempo 溢出吸收 + 提语速二次合成；BGM 保留（demucs 分离 + 侧链混音）。
- **成片**：`burn` 把字幕烧进画面并用像素差回读核验；`mark` 打 AIGC 显式标识 +
  写 4 项隐式元数据并回读核验。
- **一条命令**（`pipeline`）：`probe → asr → diarize → merge → [clean] → translate →
  build → dub → burn → mark`，阶段缓存、`pipeline.json` 审计、退出码区分"全成/部分失败"。
- **成本**（`cost`）与**任务历史**（`history`，sqlite）与**本地 GUI**（`gui`，Gradio 薄壳，
  可选依赖）。

### 变更

- **项目改名**：`submatrix` → `cuesmith`（中文名「字幕工坊」）。包名、CLI 命令
  （`python -m cuesmith` / `cuesmith`）、PyPI 名、环境变量前缀（`SUBMATRIX_*` →
  `CUESMITH_*`）一并改掉；这是公开发布前的改名，不提供旧名兼容。
- 编排层统一成"状态对象 + 阶段函数"：`run_pipeline` 857→22、`run_dub` 692→32、
  `run_translate` 299→12、`run_diarize` 327→13、`run_asr` 168→10、`run_mark` 192→17、
  `run_burn` 171→10、`_stage_diarize` 159→45 行。
- 外部工具调用收敛到三个接缝：`media`（ffmpeg/ffprobe）、`interpreters`（找解释器）、
  `worker_protocol`（常驻 worker 的 JSON 行协议）。
- 报告取段统一走 `report.section()`；`# type: ignore` 从 57 处清零。
- 产物写入改成原子写（`cuesmith.atomic`：临时文件 + `os.replace`）。
- 文案/文档：除 `README.md` 与 `AGENTS.md` 外，文档统一放 `docs/`。

### 修复

- 缓存失效漏洞：换源、改译文参数、`--force`、`--regenerate-tts` 等场景会误用旧产物。
- `dub_report.json` 的 `summary.total_audio_ms` 与实际音轨长度不一致（补到视频长度那次
  `max()` 没写回会话）。
- 舞台标识（mark）在"片长与标识时长接近"时核验必然失败的问题已由采样点裁剪覆盖
  （短于 `标识时长 + 1 秒` 时给出提示）。
- 本地 TTS 兜底会把"合成失败"伪装成成功（4 个长期红色的 dub 测试由此转绿）。

### 质量基线

- 858 个用例（默认 854 通过 / 4 按需跳过）、覆盖率 83%（门槛 80%）、
  `ruff` 与 `pyflakes` 零告警、结构约定由 `tests/test_structure.py` 的 10 条检查守住。
- 真实 154 秒素材端到端回归（`tests/test_real_sample.py`，按需开启）。
