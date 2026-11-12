# M1 · FR-2 字幕文件矩阵 + CPS 校验 — 实现说明

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-20 |
| 状态 | ✅ 完成（M1 交付：5 格式导出 + 校验报告） |
| 对应需求 | `视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md` §3 FR-2、AC-4 / AC-5 / AC-6 |
| 代码 | `submatrix/`（915 行，纯标准库） |
| 测试 | `tests/`（878 行，**112 passed**，含 9 个 ffmpeg 集成用例） |
| 实测产物 | `work/m1-demo/app/`（真实应用产物 7 条）、`work/m1-demo/proto/`（原型产物 6 条） |

一句话：**一对 SRT 进去，5 个可交付字幕文件 + 1 份带 cue 号/指标/实测值/阈值的 report.json 出来；只报不改。**

---

## 1. 用法

```powershell
# 在仓库根目录（= 本项目自建层根目录）执行
python -m submatrix build `
  --src spike\pvt-out-vtv\zh-cn.srt `
  --tgt spike\pvt-out-vtv\en.srt `
  --out work\m1-demo\app
```

输出：

```
原文：...\zh-cn.srt（7 条，zh）
译文：...\en.srt（7 条，en）
校验：warn（error 0 / warning 14）
产物：
  en.srt（886 字节）
  bilingual.srt（1407 字节）
  bilingual.ass（2113 字节）
  bilingual.vtt（1415 字节）
  en.txt（656 字节）
报告：...\work\m1-demo\app\report.json
```

阈值可配（默认值即 PRD 数值）：

```powershell
python -m submatrix build --src a.srt --tgt b.srt --out out `
  --cps-src 9 --cps-tgt 20 --width-src 16 --width-tgt 42 --max-lines 2
```

| 退出码 | 含义 | 脚本侧动作 |
|---|---|---|
| `0` | 通过（允许只有 warning） | 可交付，warning 人工过一眼 |
| `1` | 用法错误 / 文件读写失败 / SRT 解析失败 | 修输入 |
| `2` | 结构校验不通过（条数、时间轴、空译、漏译、零时长） | **字幕文件未导出**，修字幕后重跑 |

跑测试：`python -m pytest`（根目录，需 pytest；产品本身零第三方依赖）。

---

## 2. 交付物矩阵

| 文件 | 内容 | 用途 |
|---|---|---|
| `{tgt}.srt`（`en.srt`） | 单语译文 SRT | 给播放器/平台直接挂英文轨 |
| `bilingual.srt` | 一个 cue 内「原文一行 + 译文一行」 | 双语播放；也是后续烧录的输入 |
| `bilingual.ass` | 同一内容的 ASS，带一套基础样式 | 给 ffmpeg `subtitles=` 烧进视频（M3/M4 用） |
| `bilingual.vtt` | WebVTT 双语 | 网页播放器 / YouTube |
| `{tgt}.txt`（`en.txt`） | 纯译文稿，一条一行 | 直接当文案用 |
| `report.json` | 输入哈希、阈值配置、违规清单、产物清单 | 可复跑凭证 + 人工修字幕的工作清单 |

`report.json` 结构（截取）：

```json
{
  "schema_version": 1,
  "status": "warn",
  "inputs":  {"source": {"path": "...", "lang": "zh", "encoding": "utf-8", "cues": 7,
                         "bytes": 269, "sha256": "…"}},
  "config":  {"src_lang": "zh", "tgt_lang": "en", "cps_src": 9.0, "cps_tgt": 20.0,
              "width_src": 16, "width_tgt": 42, "max_lines": 2},
  "summary": {"cues": 7, "errors": 0, "warnings": 14, "outputs": 5,
              "by_code": {"cps": 1, "line_width": 13}},
  "findings": [
    {"severity": "warning", "code": "cps", "cue": 2, "lang": "en", "metric": "cps",
     "value": 20.93, "threshold": 20.0,
     "message": "英文 90 字符 / 4.30s = 20.93 CPS，超过上限 20.0"}
  ]
}
```

---

## 3. 关键设计决定（都带理由，供评审时直接引用）

1. **时间轴一律整数毫秒，容差真 0。** SRT 输出 `HH:MM:SS,mmm`，长视频小时位自动扩到 3 位。ASS 用厘秒且**向下取整**——四舍五入会让相邻 cue 出现 10ms 叠压。
2. **计数口径写死并写进报告。** 英文 CPS/行宽**计空格**、不计换行与首尾空白；中文**不计空白**（标点算 1 字）。两种语言口径不同，因为 CJK 没有词间空格。报告 message 里直接给出 `字符数 / 时长`，人工可审计，避免"到底算没算空格"的争论。
3. **条数不一致时只报 1 条错、不逐条比对、不导出字幕文件。** 索引已错位，逐条比会刷出上百条噪声把真正的问题埋掉；而错位的双语字幕比"没有字幕"更危险。
4. **漏译判定故意保守。** 只在「原文含中日韩字符」且「译文与原文完全相同，或译文完全不含拉丁字母」时判 error。所以 `OK → OK`、`2024年 → 2024年`（后者会报）这类语义边界不会误伤正常条目。
5. **校验层没有任何改写路径。** 不自动换行、不自动删条、不自动调时间轴——AC-5 用测试 `test_ac5_nothing_is_auto_corrected` 证明产物条数/时间轴/文案与输入逐条一致（仅行首尾空白归一）。
6. **双语顺序 = 原文在上、译文在下**，时间轴取原文（校验已保证两侧一致，取原文是为了让源字幕的边角情况不丢信息）。
7. **ASS 字体选 `Microsoft YaHei`**：Arial 没有中文字形，双语 ASS 烧出来中文会变方框。ASS 里只保留可见文本（去掉 `<i>`、`{\an8}` 这类不属于 ASS 的标记），换行转 `\N`，未闭合 `{` 转义为 `\{`（否则吞掉整行）。
8. **VTT 只转义真正的特殊字符**：保留 WebVTT 支持的 `<i>/<b>/<u>/<c>/<v>` 标记，其余 `<` `>` `&` 转义，避免斜体被写成字面量 `<i>`。
9. **SRT 读取 UTF-8(BOM) 失败后回退 GB18030。** Windows 上老工具产出的中文 SRT 常见 GBK；已用真实 `gb2312.srt` 样本验证（3 条，识别为 gb18030）。
10. **产物统一 UTF-8 无 BOM + LF**，跨平台 diff 无噪声。

---

## 4. 实测数据

### 4.1 真实素材跑通（两套 fixture）

| 输入 | 条数 | 结论 | 违规分布 |
|---|---|---|---|
| `spike/pvt-out-vtv/{zh-cn,en}.srt`（**pyVideoTrans 真机产物**） | 7 | `warn`，error **0** | `line_width` 13 + `cps` 1 |
| `spike/samples/{subs,out_translated}.srt`（原型产物） | 6 | `warn`，error **0** | `line_width` 10 |

**这两组数据本身就是结论**：底座产出的 SRT 是"一条一句、整句一行"，从不做换行和阅读性处理——

- 英文行宽最长 **135 字符/行**（上限 42），中文最长 **43 字/行**（上限 16）；
- 唯一一条 CPS 超标是 cue 2：`90 字符 / 4.30s = 20.93 CPS`，刚好越过 20 的上限。

也就是说：**FR-2 不是纸上需求，底座直接产出的字幕按平台规范是必须重排的**。人工重排 7 条就有 14 处要动，1 小时视频（约 600 条）不可手工做——这正是 FR-2 存在的理由。

### 4.2 AC-4：5 格式可解析（ffmpeg 实测）

| 文件 | ffprobe 识别 codec | ffmpeg 解码后 cue 数 | 文本/时间轴 |
|---|---|---|---|
| `en.srt` | `subrip` | 7 / 7 | 逐条一致 |
| `bilingual.srt` | `subrip` | 7 / 7 | 逐条一致 |
| `bilingual.ass` | `ass` | 7 / 7 | 逐条一致（ffmpeg 转 SRT 时会补 `<font face=…>`，属正常，按可见文本比对） |
| `bilingual.vtt` | `webvtt` | 7 / 7 | 逐条一致 |
| `en.txt` | —（纯文本） | — | UTF-8 无 BOM |

> VLC 与 Subtitle Edit **本机未安装**，无法无人值守验证，留一次人工核对（§6）。
> **但 ASS 的字体渲染风险已排除**：用 `ffmpeg -f lavfi -i color=... -vf "subtitles=bilingual.ass"` 真烧了一帧
> （`work/m1-demo/app/frame_check.png`）——中文正常显示、无豆腐块、双行白字黑边底部居中。
> 即 libass + `Microsoft YaHei` 这条渲染路径是通的。

### 4.3 性能

| 项 | 实测 |
|---|---|
| `run_matrix` 进程内耗时（7 条） | **14.1 ms** |
| 模块导入 | 133.8 ms（首次，含 .pyc 编译） |
| CLI 冷启动 | 1.1–2.1 s（本机 Windows Defender 扫描解释器，非代码开销） |

纯文本处理，1 小时视频（约 600 条）预计仍是毫秒级——**FR-2 不构成性能风险**。

### 4.4 测试覆盖

```
python -m pytest
112 passed in 3.99s
```

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_srt.py` | 解析宽容度（BOM/CRLF/缺序号/点毫秒/坐标后缀/少数位小数）、坏块必须报错、序列化回环、GB18030 回退 |
| `tests/test_textutil.py` | 标记剥离、拆行、两种字数口径 |
| `tests/test_export.py` | 5 格式逐项、AC-6 双语配对、空译不塞占位符、条数不符拒绝导出 |
| `tests/test_validate.py` | 7 类违规逐项、**阈值边界（正好等于上限不算违规）**、可配置阈值、报告字段 |
| `tests/test_matrix.py` | 端到端产物、report schema、退出码契约、复跑确定性、CLI 参数 |
| `tests/test_export_ffmpeg.py` | AC-4：ffprobe codec + ffmpeg 解码无损 |
| `tests/test_ac.py` | AC-5 零漏报（一次构造 7 类违例，断言无漏报）、AC-5 不改写、AC-6 逐条对齐 |

---

## 5. 已知边界（明确不做 / 留到后续）

| 边界 | 说明 |
|---|---|
| 不做自动换行/自动改时间/删条 | PRD 明确"报出不改"，由人工决定 |
| 输入只吃 SRT | 内封 ASS/VTT 由 FR-1 抽流时统一转 SRT；直接喂 `.ass` 暂不支持 |
| 未做句级合并 | `merge_cues` 属 FR-3 前置（M4），FR-2 只处理已定稿的 cue |
| 双语 ASS 两种语言同字号 | "基础样式"范围；若发布观感要求区分主次语言，M3 起加第二样式 |
| CPS 按 cue 平均 | 不做逐帧/逐字时序分析（成本与收益不匹配） |
| 1 小时长视频未实测 | 时间轴 3 位小时数已单测；性能量级为毫秒，风险低 |
| VLC / Subtitle Edit 未验证 | 见下节人工清单 |

---

## 6. 交付前一次性人工核对（可选，约 10 分钟）

> 前置说明：**装不装这两个工具都不影响交付**。ffmpeg + libass 已经覆盖"能否解析"和"烧出来长什么样"
> （`frame_check.png` 已证明中文不豆腐块）。下面这些是"播放器/编辑器特有行为"的补充核对。

1. **VLC** 打开 `work/m1-demo/app/bilingual.srt`：中文不花屏、每条两行、时间轴与视频对得上。
2. **VLC** 打开 `bilingual.vtt`：同上（网页播放器同源验证）。
3. **Subtitle Edit** 打开 `bilingual.ass`：样式名 `Default` 可见、中英文都能显示（不是方框）、无"重叠"红色提示。
4. **Subtitle Edit** 打开 `en.srt` 跑一次 `Tools → Fix common errors`：应几乎无改动（说明格式规范）。
5. ~~ffmpeg 烧一条样片确认字体~~ → **已完成**，见 `work/m1-demo/app/frame_check.png`。

---

## 7. 下一步

**M2 = FR-1 探测与内封字幕直出**（`ffprobe` + `ffmpeg -map 0:s:N` → SRT + `manifest.json` 路由结论），1–2 天。FR-2 已把下游接口准备好：只要 FR-1 产出 `source.srt`，`submatrix build` 直接可跑。

依赖关系：M1 的 `report.json` 的 `notes`/`findings` 结构沿用到 FR-1/FR-3/FR-4，不另建一套报告格式。
