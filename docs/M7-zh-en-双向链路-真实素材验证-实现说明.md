# M7 — zh→en 双向链路：真实素材验证与方向性修复

> 状态：**已完成**。zh→en 在真实中文采访素材上全链路跑通；换成 `medium` 模型后专名识别正确、
> 无需人工修。触发原因：用户明确"en→zh 与 zh→en 同等重要"。
> 新增验收：**AC-22**。测试 **436 passed**，一键回归 **24/24**。

---

## 1. 结论先说

| 环节 | en→zh（英文播客，M5） | **zh→en（中文采访，本轮）** |
|---|---|---|
| ASR | 475 词 / 30 条，37.6s（3.19×实时） | medium：7 条 / 35.8s 语音，**46.2s（0.98×实时）** |
| 翻译 | DeepSeek 30 次调用 / 9885 token | DeepSeek **10 次 / 3026 token / 9.1s** |
| 句子级 CPS | 6.74 → 6.42，超限 1/30 | 14.96 → **14.32（上限 20），超限 0/7**，峰值 17.01 |
| 排版 | 30 → 49 条，中文超限 1/49 | 7 → **10 条，0 违规**（`line_width` 11→0） |
| 配音 | 30 条，人工 3 | 7 条，atempo 0、吸收 1、**人工 0** |
| BGM | 原片无伴奏（如实报） | 现场环境声保留（原声 -15.5dB → 伴奏 -31.7dB） |
| 人工介入 | 0（发布前按 needs_review 抽检） | **0**（medium 把「刘翔」识别正确；small 版需要修 2 处） |

---

## 2. 素材（用户授权：自网上获取，CC BY 3.0）

Wikimedia Commons，中国新闻社视频，署名留存 `work/real-samples/credits.json`：

| 文件 | 内容 | 时长 | 分辨率 | 用途 |
|---|---|---|---|---|
| `real_olympic.webm` | 巴黎奥运会 徐卓一 采访（有现场环境声） | 45.1s | 1920×1080 | 全链路验证 |
| `real_football.webm` | 两会 水庆霞 谈女足/校园足球（带口音） | 91.8s | 640×360 | small vs medium 对照 |

选这两条的原因：真人中文说话、有背景环境声、**有官方发布稿可对照译文事实**（第一条）。

---

## 3. 方向性修复（en→zh 不会暴露的 5 个问题）

| # | 现象（真实素材实测） | 根因 | 修复 |
|---|---|---|---|
| 1 | `对我们对我们对我们来说` 原样进翻译，译文必然重复 | 中文 ASR 的口吃重复没清洗 | `clean_source(cjk)`：折叠 **≥3 字的相邻重复**（两字重复如"看看/非常非常"是正常表达，不动） |
| 2 | `不会, 因为他一般` 让机翻断句 | 中文 ASR 输出半角标点 + 标点后空格 | 同上：半角 `,?!;:` → 全角、去空格；中文字间空格也去掉（ASR 合并会留下 `向 他靠近`） |
| 3 | en 目标时"精简 rule 档"永远不触发 | 口头禅表全是中文（那么/其实/对吧） | `condense` 增加英文口头禅表（you know / I mean / basically / actually / kind of / sort of…），trim 用英文句号并首字母大写 |
| 4 | 英文译文每条都像半句话（无句号） | 中文 ASR 常不给句末标点 | `_ensure_en_terminal`：补句末标点；**源文以逗号/分号收尾 = 话没说完，保持原样** |
| 5 | 术语表 zh→en 一处都命中不了 | 原表按 en→zh 维护，`protect()` 拿英文词匹配中文字幕 | `Glossary.reversed()` + `translate --glossary-reverse`（一张表两种方向用） |

另外两个真实素材暴露的修：

- **短尾巴字幕**：长句末尾 0.5s 的「他靠近」被切成闪条 → `_merge_tiny` 合并容差放宽 ≤1s，
  并且中文合并**不加空格**（"向"+"他靠近"→"向他靠近"）；
- **术语误报丢失**：LLM 看到"占位符=应输出"的对照后可能直接写出术语而不使用占位符 →
  `unprotect` 在译文里已有正确译法时不再报丢失；
- `--model small/medium` 现在映射到项目内本地模型目录（此前会交给 faster-whisper 去 HF 下载，本机被墙）。

---

## 4. 端到端实测（olympic 45.1s，medium）

| 阶段 | 结果 |
|---|---|
| ASR（medium，词级） | 7 条 / 35.8s 语音 / 142 词；**46.2s，0.98×实时**；刘翔 ✓、孙海平 ✓、无需人工修 |
| 翻译（DeepSeek + 反向术语 + 双轮审校） | 3 段、**10 次调用 / 3026 token**、9.1s；回退 0；术语丢失 0；审校 2 轮改动 1 条 |
| 翻译质量 | CPS 14.96 → 14.32（上限 20），**超限 0/7**；LLM 精简 1 条（42→33 字符） |
| 排版（en 目标 42 字符/行） | 7 → **10 条**；输入 `line_width` 11 → 输出 **0 违规** |
| 配音（en-US-AriaNeural +20%） | 7/7 成功；atempo 0、吸收 1、**人工 0**；总时长 45.1s |
| 保留现场声（demucs） | 分离 51.42s（45s 音频，1.14×实时）；原声 -15.5dB → 伴奏 -31.7dB，混音 -18.4dB |
| AIGC 标识 | 显式 ✅ / 隐式 4 字段 ✅ |

译文抽样（左=中文 ASR 源，右=英文译文）：

| 中文 | 英文 |
|---|---|
| 孙海平教练在采访当中曾说过，有机会的话还是希望能培养出下一个刘翔 | Did Coach Sun Haiping say in an interview that if there's a chance, he still hopes to train the next Liu Xiang? |
| 他如果平常这样说的话，其实也是从侧面给我一些压力 | No, because if he said that normally, it would actually put some pressure on me indirectly. |
| 他非常渴望培养出下一个刘翔，但是我们也会尽自己最大的努力，向他靠近 | So we also know he's very eager to nurture the next Liu Xiang, but we'll also do our utmost to get closer to him. |

> 注意第一句变成了问句：medium 在句尾加了「吗?」，而原话是陈述句。
> 这是 ASR 标点的问题，翻译层照实翻——**不替 ASR 猜语义**。

---

## 5. ASR 模型：small vs medium（同一条带口音素材 91.8s）

| 指标 | small | **medium** |
|---|---|---|
| 耗时 | 43.6s（2.1×实时） | **84.5s（1.09×实时）** |
| 45s 素材耗时 | 21.0s | **46.2s** |
| 专名 | 刘翔 → **流向**（必须人工改） | 刘翔 ✓ |
| 常用词 | 踢什么球→替什么求、有些→有西施、无疑→无据、上海→山海 | 踢什么球 ✓、有些是家长 ✓，但：无疑→**政府**、上海→**山海** |

| 应识别 | small 输出 | medium 输出 |
|---|---|---|
| 小孩**踢什么球**啊 | 小孩**替什么求**啊 | 小孩**踢什么球**啊 ✅ |
| 有**些**家长抹杀 | 有**西施**家长抹杀 | 有**些是**家长抹杀 ✅ |
| 这**无疑**告诉我们 | 这**无据**告诉我们 | 这**政府**告诉我们 ❌ |
| **上海**这方面 | **山海**这方面 | **山海**这方面 ❌ |
| 通过我们**自身** | 通过我们**之身** | 通过我们**支撑** ❌ |

**结论**：medium 明显更准（专名与常用词基本正确），成本约 2 倍耗时；
但**同音词/连读仍会错**——中文当源语时，ASR 之后仍建议人工过一遍（或建术语表 + 改 1–2 处）。

> **M8 起**：中文源语（zh/ja/ko）在 `asr` 与 `pipeline` 里**默认用 medium**（本地没有则自动退回 small），
> 报告写 `config.model_variant`；不需要再手动加 `--model medium`。

### medium 怎么下载（本机已下好，放项目内）

**推荐 ModelScope**（实测 9–17 MB/s，1.46GB 约 90 秒；hf-mirror 只有 ≈0.3MB/s）：

```powershell
$base = 'https://modelscope.cn/models/pengzhendong/faster-whisper-medium/resolve/master/'
$dir  = 'spike\whisper-models\faster-whisper-medium'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
curl.exe -L -o "$dir\config.json"     ($base + 'config.json')
curl.exe -L -o "$dir\tokenizer.json"  ($base + 'tokenizer.json')
curl.exe -L -o "$dir\vocabulary.txt"  ($base + 'vocabulary.txt')
curl.exe -L -C - --retry 5 -o "$dir\model.bin" ($base + 'model.bin')   # -C - 断点续传
```

| 来源 | 地址 | 备注 |
|---|---|---|
| ModelScope（推荐） | `https://modelscope.cn/models/pengzhendong/faster-whisper-medium` | 实测快；直链 `/resolve/master/<文件>` |
| HF 官方 | `https://huggingface.co/Systran/faster-whisper-medium` | 本机被墙 |
| HF 镜像 | `https://hf-mirror.com/Systran/faster-whisper-medium/resolve/main/<文件>` | 可达但限速；hub 客户端撞 308，用 curl 手动下 |

文件清单：`model.bin` **1,527,906,378** / `config.json` 2,257 / `tokenizer.json` 2,203,239 / `vocabulary.txt` 459,861。
放好目录后 `--model medium` 直接用（`cuesmith/asr.py` 已做名称→本地目录映射，不联网、不下载）。

---

## 6. 复跑与验收

```powershell
python -m pytest                                      # 436 passed
python tools/run_samples.py --out work/regression-m7  # 24/24
```

新增/更新用例覆盖：

- `clean_source` 中文口吃折叠、两字重复不受影响、半角标点归一、中文字间空格去除；
- en 目标 `condense` 英文口头禅、英文句号、首字母大写；
- `_ensure_en_terminal` 补句号 / 源文逗号收尾不补；
- `Glossary.reversed()` 反向映射、中文子串命中、keep 术语两侧一致、**术语被直接写出不误报丢失**；
- ASR 短尾巴合并（≤1s 容差）且中文合并无空格；
- `find_model` 名称→本地目录映射（离线可用）。

**AC-22**：zh→en 全链路（ASR → 翻译 → 排版 → 配音 → BGM → 标识）可在真实中文素材上端到端产出；
排版后 0 违规、配音人工 0 条、反向术语表丢失 0；ASR 剩余误听必须如实列出，不得宣称全自动。

---

## 7. 遗留项

| 项 | 状态 |
|---|---|
| 中文 ASR 同音误听 | **仍存在**（medium 也有：无疑→政府、上海→山海）；建议人工校对或加"查找替换/发音纠正"功能 |
| ASR 标点改变语义 | medium 给陈述句加「吗?」→ 译文变问句；翻译层不猜语义，需人工修标点 |
| 说话人音色 | 仍搁置（pyannote 需 HF token + CPU 长时推理） |
| large-v3 模型 | 未评估：3GB 权重、CPU 更慢，收益需实测 |
