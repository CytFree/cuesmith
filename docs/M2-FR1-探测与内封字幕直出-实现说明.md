# M2 · FR-1 探测与内封字幕直出 — 实现说明

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-21 |
| 状态 | ✅ 完成（M2 交付：探测 + 路由判定 + 内封字幕直出 + manifest.json） |
| 对应需求 | `视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md` §3 FR-1、AC-1 / AC-2 / AC-3 |
| 代码 | `submatrix/probe.py`（+ `cli.py` 的 `probe` 子命令），全项目 1444 行 |
| 测试 | **66 个 M2 用例**（全项目 178 passed），含真 ffmpeg 造片、真 MKV 抽取、**真实 PGS / VobSub 位图字幕** |
| 实测产物 | `work/m2-demo/s1`（真实 MKV 抽出 6 条）、`s2` `s3` `vtv`（均正确路由到 ASR） |

一句话：**一条命令看清视频里有什么流；能抽就把内封字幕原样抽成 SRT，抽不了就明确说"得走 ASR"，是图形字幕就直接报错——绝不静默降级。**

---

## 1. 用法

```powershell
python -m submatrix probe --src spike\samples\S1_softsub.mkv --out work\m2-demo\s1
```

真实输出：

```
输入：...\S1_softsub.mkv（matroska,webm，41.33s）
流：视频 1 / 音频 1 / 字幕 1
  字幕 #2 subrip 语言=未标记
决策：extract —— 没有匹配源语言 zh 的语言标签，选取第一条未标记语言的文本轨（流 #2）
字幕：...\work\m2-demo\s1\source.srt（6 条）
清单：...\work\m2-demo\s1\manifest.json
```

| 参数 | 说明 |
|---|---|
| `--src` | 视频文件（mkv/mp4/…，ffprobe 能认的格式都行） |
| `--out` | 输出目录，自动创建；产物 `source.srt` + `manifest.json` |
| `--src-lang` | 期望的字幕语言，默认 `zh`（用于语言标签匹配） |
| `--no-extract` | 只探测与判定，不抽字幕（大文件先看路由结论） |

| 退出码 | 含义 |
|---|---|
| `0` | `extracted`（已抽 SRT）/ `extract`（dry-run）/ `route_to_asr`（需 ASR） |
| `1` | 用法错误、文件不存在、ffprobe/ffmpeg 失败、抽出来是空字幕 |
| `2` | `unsupported`：图形字幕轨或无法识别的字幕编码 |

---

## 2. 路由决策表（FR-1 的全部逻辑）

| # | 情形 | 决策 | 退出码 | 告警 |
|---|---|---|---|---|
| 1 | 有非 forced 文本轨，且语言标签匹配 `--src-lang` | `extract` 该轨 | 0 | — |
| 2 | 文本轨语言都不匹配，但存在未标记语言的文本轨 | `extract` 第一条未标记轨 | 0 | — |
| 3 | 文本轨语言都不匹配，也没有未标记轨 | `extract` 第一条 | 0 | 语言标签不匹配，已退化 |
| 4 | 文本轨 + 图形轨并存 | `extract` 文本轨 | 0 | 图形轨被跳过 |
| 5 | 只有图形轨（PGS/DVD/DVB…） | `unsupported`（**终止**，不转 ASR） | 2 | — |
| 6 | 只有无法识别的字幕编码 | `unsupported` | 2 | 编码无法识别 |
| 7 | 唯一文本轨是 `forced` | `route_to_asr` | 0 | forced 已跳过 |
| 8 | 完全没有字幕轨 | `route_to_asr` | 0 | — |

语言标签匹配做了别名归一（`zh`/`zho`/`chi`/`cmn`/`zh-CN`/`zh_Hans`/`chi-Hans` 都算中文；`und`/`mul`/空 算"未标记"），因为 ffprobe 的 `language` 标签在真实片源里 ISO 639-1/2T/2B 混用。

---

## 3. manifest.json（真实输出，截取）

```json
{
  "schema_version": 1, "tool": "submatrix", "command": "probe",
  "status": "extracted",
  "input": {"path": "...\\S1_softsub.mkv", "bytes": 400941,
            "sha256": "9a5b36cd…", "format_name": "matroska,webm", "duration_s": 41.332},
  "src_lang": "zh",
  "streams": {
    "video": [{"index": 0, "codec": "h264", "width": 1280, "height": 720, "forced": false, "default": true}],
    "audio": [{"index": 1, "codec": "aac", "channels": 1, "sample_rate": 16000}],
    "subtitle": [{"index": 2, "codec": "subrip", "language": null, "forced": false, "default": false}]
  },
  "decision": {"kind": "extract", "stream_index": 2, "subtitle_ordinal": 0,
               "codec": "subrip", "language": null,
               "reason": "没有匹配源语言 zh 的语言标签，选取第一条未标记语言的文本轨（流 #2）"},
  "warnings": [],
  "outputs": [{"kind": "source_srt", "path": "...\\source.srt", "cues": 6, "bytes": 744}]
}
```

`manifest.json` 在**所有结局**下都会写（包括 `route_to_asr` / `unsupported` / `extract_failed`），因为路由结论本身就是交付物——脚本据此决定下一步走 ASR 还是报错。

---

## 4. 实测数据

### 4.1 四个真实样本

| 素材 | 流情况 | 决策 | 结果 |
|---|---|---|---|
| `spike/samples/S1_softsub.mkv` | h264 + aac + **subrip（未标记语言）** | `extract` | **6 条**，与 ground truth **逐条 100% 一致** |
| `spike/samples/S2_hardsub.mp4` | h264 + aac，**无字幕轨**（硬字幕烧在图里） | `route_to_asr` | 正确：硬字幕不抽，一期不做 OCR |
| `spike/samples/S3_no_sub.mp4` | 无字幕 | `route_to_asr` | 正确 |
| `spike/pvt-out-vtv/S3_no_sub.mp4`（**底座 `--task vtv` 烧录产物**） | 无字幕轨 | `route_to_asr` | 正确 |

> **顺带一个对链路设计有用的发现**：底座 `--task vtv` 输出的成品视频里**没有字幕流**（字幕已经烧进画面）。
> 所以"先出 SRT 再翻译配音再烧录"的顺序不能反——反了就得对成品做 OCR。这从反面印证 FR-1 必须站在链路最前面。

### 4.2 AC-1 抽取一致性

| 素材 | 字幕轨 | 抽出 | 对比 | 结论 |
|---|---|---|---|---|
| 真实 `S1_softsub.mkv` | subrip | 6 条 | ground truth `subs.srt` | **逐条完全一致（`Subtitle ==`）** |
| 合成 MKV（ffmpeg 造） | subrip | 3 条 | 造片时塞进去的 SRT | 逐条完全一致 |
| 合成 MP4（ffmpeg 造） | mov_text | 3 条 | 同上 | 逐条完全一致 |
| 合成 MKV（ffmpeg 造） | ass（双语） | 3 条 | 同上 | 文本 + 时间轴一致（ASS→SRT 会补 `<font>` 标签，按可见文本比对，属格式降级不是内容丢失） |

`source.srt` 一律 UTF-8 无 BOM + LF。

### 4.3 AC-2 判定矩阵（对样本集 §6 的覆盖情况）

| 样本 | 要求 | 本次覆盖方式 | 结论 |
|---|---|---|---|
| #1–2 内封 SRT MKV | A 路线 + AC-1 | **真实素材** `S1_softsub.mkv` | ✅ |
| #3 内封 ASS MKV | ASS→SRT 边界 | 合成 `asssub.mkv` | ✅ |
| #4–5 无字幕有语音 | 路由到 ASR | **真实素材** `S2/S3` | ✅ |
| #6 **PGS 图形字幕** | AC-3 必须报错 | **真实 PGS 流**（ffmpeg FATE 样本 `pgs_sub.sup` 塞进 MKV） | ✅ |
| #7 仅 forced 字幕轨 | 跳过并告警 | 合成 `forced.mkv`（`-disposition:s:0 forced`） | ✅ |
| #8 多音轨多字幕轨 | 选轨优先级 | 合成 `multitrack.mkv`（eng 在前、chi 在后 → 选中 chi，ordinal=1） | ✅ |
| #9–12 | 静音/竖屏/1h/真实 BGM | 未测（不属 FR-1 核心路径） | ⏳ |

### 4.4 真实位图字幕（AC-3 闭环）

样本不在本地造，而是从 **ffmpeg 官方 FATE 测试集**下真实的位图字幕流，再用 `-c copy` 塞进任意视频：

```powershell
# 1) 下样本（共 228 KB，不用下整部蓝光）
#    https://fate-suite.ffmpeg.org/sub/pgs_sub.sup          49 KB   PGS（Blu-ray 位图字幕）
#    https://fate-suite.ffmpeg.org/sub/vobsub.idx + .sub    176 KB  VobSub（DVD 位图字幕）
# 2) 塞进任意视频，得到带真实图形字幕轨的片子
ffmpeg -i any.mkv -i pgs_sub.sup -map 0:v -map 0:a -map 1:s -c copy pgs_only.mkv
```

实测结果：

| 片子 | 决策 | 退出码 | 产物 |
|---|---|---|---|
| 只有 PGS 轨（`hdmv_pgs_subtitle`） | `unsupported` | **2** | **无 `source.srt`** ✅ |
| 文本轨 + PGS 并存 | `extract` 文本轨 | 0 | 抽出 **6 条** + 告警"存在图形字幕轨（#3 hdmv_pgs_subtitle），不支持抽取，已跳过" ✅ |
| 只有 VobSub 轨（`dvd_subtitle`） | `unsupported` | **2** | 无 `source.srt` ✅ |

这三条已固化成回归测试（`tests/test_probe_media.py`，样本缺失时自动 skip），不是一次性手工验证。

### 4.5 性能

| 项 | 实测 |
|---|---|
| `run_probe`（41s MKV：ffprobe + ffmpeg 抽字幕 + 解析 + 写清单） | **604 ms**（其中大部分是两次子进程启动） |
| 与 PRD 基线 | "内封抽取 < 1 秒" ✅ |

---

## 5. AC 对照

| AC | 证据 | 结论 |
|---|---|---|
| AC-1 抽取逐条 100% 一致 | 真实 MKV 6/6 + 合成 MKV/MP4/ASS 各 3/3，`Subtitle` 对象相等断言 | ✅ |
| AC-2 路由判定 100% 正确 | 46 个路由单测（含语言别名 7 种、forced、图形、未知编码、多轨）+ 17 个真媒体端到端 | ✅（PGS 见 §6） |
| AC-3 图形字幕必须明确报错 | **真实 PGS 流**：`unsupported` + 清单 `status=unsupported` + 退出码 2 + 不产出 `source.srt`；真实 VobSub 同结论；并有"绝不静默转 ASR"专门断言 | ✅ |

---

## 6. 已知边界

| 边界 | 说明 / 处理 |
|---|---|
| 图形字幕样本从哪来 | ffmpeg 只有位图字幕**解码器**、没有编码器（实测 `-c:s dvdsub` 报 `Subtitle encoding currently only possible from text to text or bitmap to bitmap`），本地造不出来。改用 **ffmpeg 官方 FATE 测试集**：`fate-suite.ffmpeg.org/sub/pgs_sub.sup`（49 KB）+ `vobsub.idx/.sub`（176 KB），`-c copy` 塞进任意视频（见 §4.4）。已落盘 `spike/samples/`，回归测试自动使用；换/补样本只需替换这两个文件 |
| 未知字幕编码（`eia_608` 等） | 一律 `unsupported` 显式报错，不猜、不降级 |
| 只有 forced 轨 | 走 ASR（forced 只覆盖招牌/字幕卡，当正片字幕用会漏 90% 内容） |
| 硬字幕视频 | 必然 `route_to_asr`（OCR 一期明确不做） |
| 多音轨选轨 | 只探测、不选不重编码（配音阶段由底座处理音轨） |
| 1 小时长视频 / 竖屏 / 静音视频 | 未测；FR-1 只做探测+抽流，无时长相关逻辑，风险低 |

---

## 7. 下一步

**M3 = FR-4 AIGC 标识（显式 + 隐式）**：`ffmpeg drawtext` 烧起始 3 秒标识 + `-metadata` 写 `comment/artist/copyright/content_id`，并用 `ffprobe -show_format` 回读验证（AC-10/11/12）。

依赖关系：M1 产 SRT 矩阵、M2 产/路由 SRT、M3 给成品打标、M4 把配音编排接上——四段产物都落在 `work/<task>/`，报告结构统一（`findings` + `notes` + sha256 输入指纹）。
