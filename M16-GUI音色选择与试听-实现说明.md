# M16 · GUI 音色选择 + 试听 + 多人声色重配 — 实现说明

> 起因：GUI 的「音色」只是一个 Textbox，要用户自己敲 Edge-TTS 的 voice id
> （`zh-CN-XiaoxiaoNeural` 这种），记不住、拼错要等配音阶段才报错；多人声色
> `--speaker-voices` 干脆没进 GUI，只能塞「额外参数」。
>
> 收费软件（剪映「多人配音」、Descript）都是「音色下拉 + 分类 + 试听 + 按人分配」。
> M16 把这块补成薄壳：**目录/试听/回填/重配**，不碰编排逻辑。
>
> 状态：**代码、单测与本地真实测试全部完成**。本地启动 GUI（`127.0.0.1:7860`）后，
> 经 `gradio_client` 打真实 HTTP 接口跑通三件事：试听真实出声、双人素材多人声端到端、
> 改音色表后「重配音色」。期间发现并修掉一个只有真实 GUI 才暴露的 bug（DataFrame
> 输入被当成可 bool 的序列）。**634 个测试全绿（M16 +21，含 2 条 pipeline 回归）**。

---

## 1. 范围（做了什么 / 没做什么）

| 项 | 状态 | 说明 |
|---|---|---|
| 音色目录 | ✅ | `submatrix/voices.py`：本机 `edge_tts.list_voices()` 全量拉取，只留 zh/en/多语言 |
| 离线兜底 | ✅ | 精选静态表（中英常用 50 条），没装 edge-tts / 没网时下拉仍可用 |
| 下拉选择 | ✅ | 标签 `zh-CN-XiaoxiaoNeural · 晓晓·女声`；第一项「默认（按目标语言）」；`allow_custom_value` 仍可手输 |
| 试听 | ✅ | 真实合成一句目标语言样音，页面内 `gr.Audio` 播放；同名音色+文本复用缓存 |
| 目标语言联动 | ✅ | 切换目标语言自动换音色列表并重置为「默认」 |
| 多人声色输入 | ✅ | 「多人声音色」Textbox：`SPEAKER_00=音色,...` 映射或音色池；留空用内置默认池；填了自动开启多人声 |
| 多人声人数 | ✅ | `MAX_SPEAKERS = 8`；3–4 人建议在「说话人数」填实际人数（比自动更稳，见 §5.6） |
| 说话人表回填 | ✅ | 跑完从 `pipeline.json` 的 `diarize.summary` 读 `SPEAKER_xx → 音色 / 字幕条数`，表格**可编辑** |
| 重配音色 | ✅ | 改「音色」列后点按钮：复用上次 ASR/翻译缓存，只重跑 dub/burn |
| 音色克隆/音色市场 | ❌ | 不做；云端复刻仍走 M12 的 `--tts-command`（在「额外参数」里透传） |
| 音色风格/情感参数 | ❌ | 不做；`--tts-rate` 等仍走额外参数 |

## 2. `submatrix/voices.py`

### 2.1 数据来源与缓存

```
python -c "import asyncio,json,edge_tts; print(json.dumps(asyncio.run(edge_tts.list_voices()), ensure_ascii=False))"
        ↓ 只保留 Locale 以 zh/en 开头（含 *MultilingualNeural）
work/voices/edge-voices.json   # {schema_version, fetched_at, source, voices[]}
```

- 默认 TTL 30 天；新鲜缓存直接用，过期先尝试刷新；
- 刷新失败 → **先用旧缓存**，没有旧缓存才退回静态表（保证下拉永远有内容）；
- 缓存路径可用环境变量 `SUBMATRIX_VOICE_CACHE` 覆盖（测试/多环境用）；
- 静态表含中文昵称（晓晓/云希/晓伊…）、性别与多语言标记，作为标签排序依据。

### 2.2 对外接口

| 函数 | 作用 |
|---|---|
| `voice_choices(language)` | 目标语言的下拉标签列表，首项 `默认（按目标语言）`；多语言音色两种语言都出现 |
| `voice_id(value)` | 下拉标签 / 手输值 → voice id；「默认」返回 `""` |
| `default_voice(language)` | 沿用 `pipeline.DEFAULT_VOICES`（zh=晓晓、en=Aria），不在 GUI 里另写一份 |
| `preview_voice(voice, language, ...)` | 合成句样音到 `work/gui/preview/<voice>-<sha10>.mp3`；同指纹复用，返回路径 |
| `discover_voices(...)` | 目录发现 + 缓存 + 兜底（`runner` 参数供测试注入，不联网） |

试听文本按目标语言：中文「你好，这是配音试听……」，英文「Hello, this is a voice preview……」。

## 3. `submatrix/gui.py` 改造

### 3.1 界面

- 音色：`Textbox` → `Dropdown(choices=voice_choices("zh"), allow_custom_value=True)`；
  下方「试听」按钮 + `gr.Audio` + 一行提示；
- 高级区新增「多人声音色」Textbox；
- 主区新增「说话人 → 音色」`Dataframe`（`interactive=True`）+「重配音色」按钮；
- `tgt_lang.change` → 重新生成音色列表（`gr.update(choices=..., value=默认)`）。

### 3.2 「开始」与「重配音色」共用一个执行器

```
build_argv(settings) → plans[(src, out, argv)]
        ↓
_run_plans()   # start / line / done 事件，Popen + 杀进程树 + 日志
        ↓
_run_and_view()  # 批量结果 / 成本 / 产物 / 说话人表 / 音色输入框回填
```

- `RunController` 新增 `last_plans`（源文件 + 输出目录 + 完整表单参数）与 `last_env`
  （上次注入的 Key 环境）；`reset()` **不清**它们，只有新「开始」成功解析参数后才覆盖；
- `stream_revoice(voice, speaker_table)`：从 `last_plans` 重建 argv，把表格里的
  `SPEAKER_xx → voice` 转成 `--speaker-voices`，`features` 强制含 `dub`，关掉
  `--force`/`--regenerate-tts`；pipeline 的 dub 缓存判据含 `voice_by_speaker`
  （`_dub_config_changed`），所以换音色**自动重跑 dub/burn**，ASR/翻译命中缓存；
- 输入个数 16 → 17（`speaker_voices` 在 `tts_python` 之后、`controller` 之前），
  位置回归测试同步更新，避免再出现 M14 那种「控件顺序错位」。

### 3.3 边界与诚实降级

| 场景 | 行为 |
|---|---|
| 没装 edge-tts / 拉列表失败 | 用静态表；试听时明确报「试听失败：…」 |
| 内封字幕素材 + 源语言 auto | pipeline 明确报「内封字幕路线需要显式 --src-lang」；GUI 源语言标签已提示选 zh/en |
| 没开多人声 | 说话人表显示占位 `—`；「重配音色」只切全片默认音色 |
| 只识别到 1 人 | pipeline 回退单音色，表格同样显示占位 |
| 表格「音色」列留空/写错 | `speaker_mapping()` 跳过该行；写错 id 由 Edge-TTS 在合成时报错 |
| 批量多条视频 + 多人声 | 表按文件汇总；重配时使用当前表里的映射（同名 speaker 跨文件复用，简单场景够用） |
| 没跑过就点「重配音色」 | 提示先点「开始」，不启动子进程 |

## 4. 测试（M16 新增 18 条）

- `tests/test_voices.py`（12）：`voice_id` 标签/手输/默认解析；按语言过滤 + 默认项在前 +
  多语言两侧可见；`discover_voices` 解析/写缓存/新鲜缓存不重拉/`refresh` 强制重拉/
  失败退回静态表/坏 JSON 兜底/只留 zh-en；`default_voice` 与 pipeline 一致；
  `preview_voice` 合成与缓存、留空用语言默认、引擎失败包成 `VoicesError`；
- `tests/test_gui.py`（+7）：`--speaker-voices` 与「默认」标签不进 `--voice`；从
  `pipeline.json` 读说话人表 / 占位 / 映射字符串；试听失败提示；`stream_revoice`
  无运行记录提示；重配后 argv 带 `--speaker-voices`、无 `--stop-after`、仍记得参数；
  **DataFrame 型输入的 truthiness 回归**（真实 GUI 测试暴露）；
- 全量：**634 passed**（原 613）；
- `build_app()` 在 `spike/gui/.venv`（gradio 6.30.0）可构建：**51 blocks / 5 handlers**。
## 5. 本地真实测试（GUI 7860，经 gradio_client 打真实接口）

启动：`spike\gui\.venv\Scripts\python.exe -m submatrix gui --no-browser --port 7860`，
HTTP 200。测试脚本 `work/m16-real/gui_real_test.py`（不进产品代码）。

### 5.1 试听（`/preview_voice_ui`）

| 目标语言 | 音色 | 结果 |
|---|---|---|
| zh | `zh-CN-XiaoxiaoNeural`（晓晓） | 8.088s / 48,528 bytes |
| en | `en-US-AriaNeural`（Aria） | 6.144s / 36,864 bytes |

### 5.2 多人声真实链路（`/stream_run`，demo2.mp4 双人中文 19.7s → en，免费 MT）

```
status: 完成 1/1，wall 116.8s（pipeline 110.4s）
stages: probe/asr/diarize/merge/translate/build/dub/burn 全 ok，mark skipped
diarize 10.2s → 2 人 / 4 段 / 2 音色；translate 10.3s（CPS 14.67 超限 0）
dub 31.1s 3/3 成功、人工 0；burn 2.1s 核验 True（像素差 2.509）
speaker table: SPEAKER_00 → en-US-GuyNeural（2 条）；SPEAKER_01 → en-US-AriaNeural（1 条）
speaker 输入框自动回填："SPEAKER_00=en-US-GuyNeural,SPEAKER_01=en-US-AriaNeural"
产物 9 个，成本 ¥0（Edge-TTS 免费 + 免费 MT）
```

### 5.3 改音色后「重配音色」（`/_revoice`）

把表格改成 `SPEAKER_00=Jenny / SPEAKER_01=Brian` 后点按钮：

```
wall 35.4s（pipeline 31.4s）
probe/asr/diarize/merge/translate/build 全 cached（0.0s）
dub ok 29.2s（3/3）→ burn ok 2.0s
dubbed.wav sha 9241d099 → db9e1150（真的换了）
burned.mp4 2,834,038 → 2,841,780 bytes（sha 也变）
dub_report voices: Jenny 2 条 / Brian 1 条；pipeline config speaker_voices 同步
成本仍 ¥0
```

结论：换音色只重跑 dub+burn（31.4s vs 首次 110.4s，约 1/3.5），ASR/翻译/分离全部命中缓存。

### 5.4 真实测试暴露并修复的 bug

`/_revoice` 首次调用 500：`ValueError: The truth value of a DataFrame is ambiguous`。

- 原因：Gradio `Dataframe` 默认 `type="pandas"`，输入到函数是 pandas DataFrame；
  `speaker_mapping()` 里写了 `for row in rows or []`，对 DataFrame 取 bool 直接抛错。
  单测里传的是 `list[list]`，所以只测纯函数发现不了。
- 修复：新增 `_table_rows()` 统一把 DataFrame / dict / 2D list 转成 `list[list[str]]`
  （DataFrame 走 `.values.tolist()`，绝不对它取 bool）；同时把说话人表组件设为
  `type="array"`；补回归测试 `test_speaker_mapping_accepts_dataframe_like_input`。
- 修复后同一脚本重跑：三部分全绿（见上表）。

### 5.5 真实批量（`/stream_run` 两条不同路线）

`S1_softsub.mkv`（内封中文字幕 → 直出路线）+ `S2_hardsub.mp4`（硬字幕 → ASR 路线），
zh→en，`src_lang=zh`：

```
status: 完成 2/2，wall 259.0s，成本 ¥0，15 个产物
01-S1_softsub: ok 79.1s（probe 1.3s / asr skipped / translate 43.7s / dub 29.1s / burn 4.9s，核验 True）
02-S2_hardsub: ok 155.8s（probe 0.3s / asr 95.9s / translate 10.5s / dub 44.8s / burn 4.2s，核验 True）
批量结果表两行状态正确、per-file 输出目录 01-/02- 正确、说话人表两条占位 "—"
```

**顺带发现一个真实输入约束**：第一次批量把 `S1_softsub.mkv` 用 `src_lang=auto` 跑，
失败信息是「内封字幕路线需要显式 --src-lang（字幕流无法可靠判断语言）」——这是既有
产品规则（字幕流本身没有可靠语言标记），不是 M16 的 bug；GUI 侧已把源语言标签改成
「内封字幕素材请选 zh/en；auto 无法判断字幕语言」，避免用户以为程序坏了。

### 5.6 3–4 人验证（sherpa 官方 4 人中文样本，56.86s）

| 配置 | 结果 |
|---|---|
| `--speakers 4`（已知人数） | **4 人 / 10 段**：3/2/2/3 段，11.33/5.91/5.72/7.64s |
| 自动（默认 threshold 0.8） | **5 人 / 10 段**（过分割，默认阈值偏保守） |
| 自动（threshold 0.9） | **4 人 / 10 段**，分段与已知 4 人完全一致 |
| 自动（threshold 0.95） | 同上，4 人 / 10 段 |

结论：**不是只能两人**，`MAX_SPEAKERS = 8`；3–4 人建议在 GUI「说话人数」直接填实际
人数（最稳），或把自动阈值调到 0.9+。默认音色池 4 个音色刚好一人一个，超过 4 人会
复用并告警；人更多时在「多人声音色」里写映射/音色池。

**又发现并修掉一个真实 bug**：英文默认音色池第 4 个是 `en-US-DavisNeural`，微软已把
该音色下线（真实调用 `NoAudioReceived`）——4 个英文说话人时第 4 人会配音失败。
已换成在线的 `en-US-AndrewNeural`，并实测池内 4 个音色全部可合成
（Guy 13.1KB / Aria 13.4KB / Jenny 13.0KB / Andrew 8.4KB），补了默认池回归测试。

**4 人端到端配音实测**（同一 4 人音频 + 纯色画面合成 71s 测试片，zh→en）：

```
asr 65.1s（medium 8 条）→ diarize 16.1s（4 人 / 10 段 / 4 音色）
translate 140.6s（DeepSeek，3099 tokens；免费 MT 当时 SSL 失败）→ build 8 → 11 条
dub 50.2s 8/8 成功、人工 0 → burn 核验 True
音色分派：Guy 3 条 / Aria 1 条 / Jenny 2 条 / Andrew 2 条（一人一个，无复用）
成本 ≈¥0.006482；产物 work/m16-real/four-speaker-run/（burned.mp4 71s / en.srt / 报告）
```

自动 vs 显式：`--speakers 4` 的分段与 threshold 0.9/0.95 自动判断一致；默认
threshold 0.8 会过分割成 5 人。GUI 想自动判断时，可在「额外参数」加
`--diarization-threshold 0.9`（阈值越大说话人越少）。

## 6. 涉及文件

- 新增 `submatrix/voices.py`、`tests/test_voices.py`、本文件；
- 修改 `submatrix/gui.py`（音色下拉/试听/多人声色/说话人表/重配，`_run_plans` 抽取，
  `_table_rows()` 兼容 DataFrame）、`tests/test_gui.py`（输入顺序 17、输出 8 元组、
  重配测试、DataFrame 回归）；
- 真实测试产物：`work/gui/m16-real-demo2-20261030-164939/`（成片/字幕/报告）、
  `work/gui/preview/`（试听 mp3）、测试脚本 `work/m16-real/gui_real_test.py`；
- 需求文档 `视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md`：状态、AC-35、M16 行、测试数 634。




