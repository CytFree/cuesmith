# OPT-10 实现说明：翻译提速 —— 复用 HTTP 连接（省掉每请求一次 TLS 握手）

> 新增优化项（2026-11-03）。测试 **714 passed**、回归 **27/27**；10min 素材 translate
> **15.30s → 5.70s（2.7×）**，并发 8 时 **3.42s（4.5×）**，译文逐字节一致、成本仍 ¥0。

---

## 1. 问题：翻译的耗时几乎全是"网络"，不是翻译本身

10min 英文字幕（140 cue → 39 个段落请求，并发 4）实测：**15.30s**。拆开看：

- 每段一次 POST；原来每个请求都用 `urllib.request` **新建 TLS 连接**；
- 实测单请求：**新建连接 1.74s / 复用连接 0.53s**（TLS 握手本身 **0.77s**，DNS + 三次握手都重复付）；
- 也就是说 39 个请求里有一大半时间花在"重新握手"，而不是翻译计算（端点处理约 0.5s/请求）。

> 这不是换渠道/换模型能解决的：免费 MT、LLM 都一样要付连接成本。**问题在 HTTP 层**。

---

## 2. 改法：线程内复用一条 HTTPS keep-alive 连接

`cuesmith/translate.py` 的默认 transport（`_default_transport`）从"每次 urllib 新建请求"
改为 **thread-local 的 `http.client.HTTPSConnection`**：

- 每个工作线程持有一条连接，第一个请求付一次 TLS，之后同一端点直接复用（`from/to` 在 URL query 里，路径不变）；
- 线程隔离（`threading.local`）——并发线程不会同时读写同一个 socket；
- 连接被服务端关掉（空闲超时/网络抖动）时：捕获 `RemoteDisconnected`/`OSError`/`SSL` 错误，
  **丢掉连接、重建一次再试**；仍失败才包成 `URLError` 交给上层的重试/渠道回退（OPT-3）逻辑；
- 只用标准库（`http.client`/`ssl`/`urllib.parse`），主包零依赖不变；
- 测试注入 `transport=` 的路径完全保留；报告新增 `config.http_keepalive`（默认 True，注入 transport 时 False）。

为什么不做连接池/HTTP2：单端点、并发 ≤ 8、每个线程一条连接已经够，复杂度不值得。

---

## 3. 实测（同一网络窗口，同一份字幕，冷跑）

### 3.1 10min 英文字幕（140 cue，39 个段落请求）

| 组合 | 墙钟 | 相对 | 说明 |
|---|---|---|---|
| 旧实现 + 并发 4 | **15.30s** | 1.00× | 每请求新建 TLS |
| 新实现 + 并发 4 | **5.70s** | **2.68×** | 默认配置 |
| 旧实现 + 并发 8 | 8.09s | 1.89× | 只加并发 |
| **新实现 + 并发 8** | **3.42s** | **4.47×** | `--translate-concurrency 8` |

4 组译文与旧实现**逐字节一致**（连接方式/并发不影响对齐与内容）。

### 3.2 稳定性（keep-alive + 并发 8，连跑 3 轮）

| 样本 | 3 轮墙钟 | 回退事件 | 多轮译文一致 |
|---|---|---|---|
| en 10min（140 cue） | 3.41 / 3.45 / 3.56s | 0 | ✅ |
| zh 3人 4min（19 cue → en） | 1.64 / 2.36 / 2.28s | 0 | ✅ |

### 3.3 全链路里（pipeline 实际调用）

| 场景 | 改前 | 改后 |
|---|---|---|
| zh 60s 素材 `pipeline --stop-after translate`（6 cue / 2 段） | 23.1s（慢窗口） | **3.4s** |
| en 10min `pipeline --stop-after translate`（31 段，并发 4） | 19.0s（OPT-5 好窗口） | **5.1s** |

> 60s 那条 23.1s 是端点抖动窗口，不能直接当倍数；以同窗口的 15.30→5.70 为准。

---

## 4. 建议用法

- 默认不用改任何参数：keep-alive 自动生效（并发 4）；
- 短字幕/网络好时可 `--translate-concurrency 8`（pipeline 同名参数），实测再快 ~1.4×；
  免费端点有被限流风险，**默认仍保守用 4**，大批量（>200 段）建议先小样本试 8；
- LLM（DeepSeek）渠道的连接复用同样生效（首 token 前的 TLS 不再重复），但 LLM 时间主要花在生成上，收益比例小；
- 若网络本身抖动（OPT-3 记录的 SSL 失败窗口），收益会变小——请求处理时间远大于握手时，省 0.7s/请求就不显眼了。

---

## 5. 测试

`tests/test_translate.py` 新增 **5 条**（共 59）：

1. 连续两次请求只建一条连接，且 method/path/query/body/header 正确；
2. 服务端关连接（`RemoteDisconnected`）时自动重连一次并成功，旧连接被 close；
3. 两次都失败 → 抛 `urllib.error.URLError`（交给上层重试/回退）；
4. 两个线程各用各的连接（thread-local）；
5. 报告 `config.http_keepalive`：默认 True、注入 transport 时 False。

全量 `python -m pytest` → **714 passed**；`tools\run_samples.py` → **27/27（25.4s）**。

---

## 6. 证据与复跑

| 路径 | 内容 |
|---|---|
| `work/opt10-net/probe_mt.py` | 单请求延迟：新建 vs 复用（TLS 0.77s / 1.74s vs 0.53s） |
| `work/opt10-net/bench_translate.py` | 四组合对比（legacy/keepalive × 并发 4/8）+ 译文一致性 |
| `work/opt10-net/stability.py` | keep-alive + 并发 8 连跑 3 轮 |
| `work/opt10-net/runs/result.json` | 四组合墙钟/请求数（39） |
| `work/opt10-net/cli-keepalive*/` | 命令行实际产物（translate.json 含 `http_keepalive: true`） |
| `work/real-samples/opt5-parallel-10min/` | 10min pipeline 里 translate 5.1s 的证据 |
| `work/real-samples/opt7b-pipe-zh-60s/` | 60s pipeline 里 translate 3.4s 的证据 |

```powershell
# 复跑实验
python work\opt10-net\bench_translate.py
python work\opt10-net\stability.py
# 单测
python -m pytest tests\test_translate.py -q
```
