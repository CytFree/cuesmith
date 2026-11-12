# M3 · FR-4 AIGC 标识（显式 + 隐式）— 实现说明

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-21 |
| 状态 | ✅ 完成（M3 交付：显式烧帧标识 + 隐式元数据 + 自动核验 + 自用留痕） |
| 对应需求 | `视频字幕提取翻译配音工具-需求文档-v1.1-MVP.md` §3 FR-4、AC-10 / AC-11 / AC-12 |
| 法规依据 | 《人工智能生成合成内容标识办法》（国信办通字〔2025〕2 号，**2025-09-01 已施行**） |
| 代码 | `submatrix/mark.py`（+ `cli.py` 的 `mark` 子命令），全项目 2001 行 |
| 测试 | **55 个 M3 用例**（全项目 233 passed），含真 ffmpeg 烧帧 / 元数据回读 / 像素核验 |
| 实测产物 | `work/m3-demo/vtv/`（对 M0 真机成品 42.02s 视频打标） |

一句话：**一条命令给成品视频打上"看得见的标识"和"读得出来的元数据"，然后自己回读核验——核验不过就报错退出，绝不假装成功。**

---

## 1. 用法

```powershell
python -m submatrix mark --src work\vtv\S3_no_sub.mp4 --out work\m3-demo\vtv
```

真实输出（对 M0 真机成品打标）：

```
输入：...\spike\pvt-out-vtv\S3_no_sub.mp4（42.02s）
标识：显式（top-right「AI 生成」前 3 秒）+ 隐式 4 字段
产物：...\work\m3-demo\vtv\marked.mp4（850803 字节）
核验：显式 ✅；隐式 ✅
证物：...\work\m3-demo\vtv\label_frame.png
报告：...\work\m3-demo\vtv\mark.json
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--src` / `--out` | — | 成品视频 / 输出目录 |
| `--label-text` | `AI 生成` | 显式标识文案 |
| `--label-seconds` | `3` | 标识持续时长；**< 3 秒直接拒绝** |
| `--position` | `top-right` | `top-right`/`top-left`/`bottom-right`/`bottom-left` |
| `--provider` | `submatrix` | 服务提供者名称 → 写 `artist`，并作为 `copyright` 默认值 |
| `--comment` | `AI 生成内容：AI 翻译 + AI 配音` | 生成合成属性说明 |
| `--copyright` | 同 `--provider` | 版权/提供者信息 |
| `--font-file` | 自动（雅黑→雅黑粗→黑体→宋体） | 指定中文字体 |
| `--self-use` | 关 | **仅自用模式**：不打标、写留痕日志 |

| 退出码 | 含义 |
|---|---|
| `0` | 已打标（或自用模式完成） |
| `1` | 参数错误 / 输入不存在 / 字体找不到 / ffmpeg 失败 |
| `2` | **自动核验未通过**（标识没烧上或元数据没写进容器） |

---

## 2. 法规 → 实现对照

| 办法条款 | 要求 | 本实现 | 证据 |
|---|---|---|---|
| **第四条** 显式标识 | 视频**起始画面**加显著提示；导出文件自带，不能只在外壳提示 | `drawtext` 烧进画面（白字 + 半透明黑底，字号随分辨率自适应 5% 高度），默认右上角、前 3 秒；`-crf 18` 重新编码后随文件走 | `label_frame.png` 抽帧 + 像素差值核验（AC-10） |
| **第五条** 隐式标识 | 文件元数据写入生成合成属性、提供者、内容编号 | MP4 写 `comment` / `artist` / `copyright` / `content_id`（UUID v4，每次生成唯一） | `ffprobe -show_format` 回读 4/4（AC-11） |
| **第十条** 禁止删改 | 不得提供删除/篡改标识的途径 | **没有** `--no-label`/`--strip-metadata` 之类开关；唯一例外 `--self-use` 会写 `self_use_notice.json` 留痕 | 5 个"疑似移除"参数全部报错退出 1（AC-12） |

> 顺带说明：**"显著"是主观标准**，本实现的做法是字号按 `height × 5%` 自适应（720p→36px，1080p→54px）、白字 + 半透明黑底保证任何画面上都可读，并保留抽帧证物供人工确认——不假装能自动判定"显著性"。

---

## 3. 产物清单

| 文件 | 内容 |
|---|---|
| `marked.mp4` | 带显式标识 + 隐式标识的成品（H.264 + AAC，`moov` 前置可边下边播） |
| `mark.json` | 配置、元数据、输入/输出 sha256、**核验结果**（含逐点像素差值） |
| `label_frame.png` | 抽帧证物（标识应在的那一帧），人工一眼可核 |
| `label.txt` | 实际烧进画面的原始文案（避免"文案被转义吃掉"无从追溯） |
| `self_use_notice.json` | 仅自用模式下的留痕（时间、输入哈希、法条引用、禁止发布提示） |

`mark.json` 的核验段（真实输出）：

```json
"verification": {
  "metadata_roundtrip": {"ok": true, "fields": {"comment": true, "artist": true,
                          "copyright": true, "content_id": true}, "missing": []},
  "explicit_label": {
    "ok": true, "threshold": 0.0005,
    "samples": [{"at": 1.0, "expected": "label", "diff": 0.00139},
                {"at": 2.5, "expected": "label", "diff": 0.00139},
                {"at": 3.5, "expected": "none",  "diff": 0.0}]
  }
}
```

核验方式：同一时间点分别从**源片**和**产物**抽帧转灰度，统计"亮度差 > 40"的像素占比。
标识在时应明显大于 0，标识结束后应回到 0（只剩编码噪声）。这条判据是自证的——不是拿滤镜参数自我安慰。

---

## 4. 实测数据（真实素材）

| 项 | 值 |
|---|---|
| 输入 | `spike/pvt-out-vtv/S3_no_sub.mp4`（M0 真机成品，1280×720，42.02s，889,816 字节） |
| 输出 | `marked.mp4`（850,803 字节，H.264 720p + AAC 48kHz，`moov@36 < mdat@33481`） |
| 耗时 | **7.0 秒 / 42.02 秒素材 = 6× 实时**（libx264 crf18 veryfast，8C/16T CPU） |
| 显式标识 | 右上角「AI 生成」，白字 + 半透明黑底，1.0s / 2.5s 处像素差 **0.139%**，3.5s 处 **0.0%** |
| 隐式标识 | `comment`/`artist`/`copyright`/`content_id` **4/4 可被 ffprobe 读回**；`content_id` 字符串真实存在于容器字节中 |
| 视觉确认 | 标识清晰、不遮挡字幕（字幕在底部居中，标识在右上角） |

标识抽帧证物（可直接打开核对）：`work/m3-demo/vtv/label_frame.png`

---

## 5. 四个实测坑（都已固化成测试）

| 坑 | 现象 | 处理 |
|---|---|---|
| **自定义元数据键被静默丢弃** | `-metadata content_id=...` 不加 `-movflags use_metadata_tags` 时，`content_id` 根本不在容器里，ffprobe 也读不到；`comment`/`artist`/`copyright` 却正常 → 极易误以为成功 | 强制 `+faststart+use_metadata_tags`（写 `mdta` 原子），并在测试里断言 `content_id` 字节出现在文件中 |
| **标识文案不能直接塞过滤器** | 文案含 `,` 或 `[` 时过滤图解析直接失败；`:` 需要两层转义 —— 但用户文案是自由文本 | 文案写 `label.txt`，用 `textfile=` 引用 → 冒号、逗号、方括号、百分号都能整句渲染（已抽帧确认） |
| **字体路径不存在会让 ffmpeg 崩** | 不是报错，而是 `Fontconfig error` + **访问违例 0xC0000005** | 调用前 `is_file()` 校验，找不到就报"请用 --font-file 指定"；默认按 雅黑→黑体→宋体 自动探测 |
| **Windows 路径冒号要两层转义** | `fontfile=C\:/...` 报 `No option name near '/Windows/...'`；`C\\:/...` 才通过（过滤器图一层 + 选项一层） | 统一走 `escape_filter_path()`，单测钉死（含空格/中文路径） |

附带一个小副作用：`use_metadata_tags` 会让 ffprobe 把 `major_brand` 等标准项显示成 `"isom;isom"`（`udta` 与 `mdta` 两处合并展示），无实际影响。

---

## 6. AC 对照

| AC | 证据 | 结论 |
|---|---|---|
| AC-10 起始 3 秒内可见显式标识 | 像素差值三采样点（1.0s / 2.5s 有、3.5s 无）+ 抽帧证物 PNG + 人工看图确认 | ✅ |
| AC-11 `ffprobe -show_format` 可读出全部隐式字段 | 4/4 字段逐项断言 + `content_id` 字节存在于容器 + UUID v4 格式与唯一性 | ✅ |
| AC-12 除自用模式外无参数能移除标识 | 5 个"疑似移除"参数（`--no-label`/`--no-metadata`/`--strip-metadata`/`--unmarked`/`--remove-label`）全部拒绝；`--help` 里不存在 strip 类选项；默认路径 100% 打标 | ✅ |

自用模式的留痕（`self_use_notice.json`）内容示例：

```json
{"mode": "self_use",
 "notice": "本文件未写入 AIGC 标识，仅限自用，不得公开发布。《人工智能生成合成内容标识办法》第十条禁止删除、篡改、伪造标识；如需发布，请重新执行 mark 命令生成带标识的版本。",
 "input": {"path": "...", "bytes": 889816, "sha256": "..."},
 "output": {"path": "..."},
 "generated_at": "2026-10-21T11:05:00+08:00"}
```

---

## 7. 已知边界

| 边界 | 说明 |
|---|---|
| 只覆盖视频形态 | 纯音频发布（播客）需要"语音提示"，本期不做（MVP 产物是视频） |
| 不校验"显著性"主观标准 | 字号/对比度按经验值自适应 + 抽帧留证，由人最终确认 |
| 只处理第一路视频 + 全部音轨 | 多角度/画中画等复杂片子不在 MVP 范围；音轨若不是 MP4 可承载编码则转 AAC 192k |
| 重编码有一次画质损失 | `crf 18 veryfast`；42s 720p 用时 7s。若要无损只能在后期合成阶段就带上标识（M4 联调时可把 drawtext 并进合成链） |
| 平台侧标识 | 各平台发布时自己的"AI 内容"勾选与页边提示由平台负责，文件侧标识已满足"文件自带"要求 |

---

## 8. 下一步

**M4 = FR-3 配音编排 + 全链路联调 + 12 条样本回归**：
- `merge_cues`（句级合并，M0 实测：溢出 10/10 → 5/6）
- 并发 TTS（实测 6 并发 5.2×）+ 单条重试 + `dub_report.json` 时长报告
- `atempo ≤1.3x` 溢出吸收
- 把 probe → 翻译 → dub → 字幕矩阵 → mark 串成一条 `run` 命令，跑 §6 样本集出总报告

依赖关系：M4 的成品直接调用 `mark`，所以 M3 的标识不会被漏掉——**流水线最后一步就是合规步骤**，不靠人记得。
