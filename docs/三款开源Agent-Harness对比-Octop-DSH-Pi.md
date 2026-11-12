# Octop vs DeepSeek Harness (dsh) vs Pi —— 三款开源 Agent Harness 对比

> 数据采集时间：2026-10-19（Star/Fork/Issue 取自 GitHub API；功能取自各自 README / 架构文档 / 官方站点）
> 一句话结论：**Octop 是"产品"，dsh 是"平台内核"，Pi 是"工具/库"**。三者都叫 harness，但解决的问题层级完全不同，直接比"谁更强"会失真。

---

## 1. 先分清层级：它们不是同一类东西

| 层级 | 项目 | 本质 |
|---|---|---|
| **应用/产品层** | **Octop**（腾讯 TencentCloud） | 开箱即用的**自托管多用户 AI 助手平台**：Web 控制台 + IM + CLI + 桌面端 + 定时任务，一家人/一个团队装完就能用 |
| **框架/内核层** | **dsh / DeepSeek Harness**（DeepSeek AI） | **Agent 执行底座**："一切皆插件"的 Cordis 内核，你可以用它拼出自己的 Agent 产品（它自带 Web/Desktop/SDK/ACP 多种 profile 作为参考形态） |
| **工具/库层** | **Pi**（earendil-works，作者 Mario Zechner / badlogic） | **极简 coding agent CLI + 可复用 TypeScript 工具包**：终端里干活的编程 Agent，也可当 SDK 嵌入自己的应用 |

三者都用 MIT 协议、都是"Agent harness"，但：
- Octop 类比：**自托管版 ChatGPT/扣子 + 多专家 + IM 网关**；
- dsh 类比：**Agent 界的"内核 + 插件总线"**（更接近"自己做 Claude Code"）；
- Pi 类比：**Agent 界的 "Unix 小工具"**（4 个工具、<1000 token 提示词、拒绝功能膨胀）。

---

## 2. 硬数据对照（2026-10-19）

| 指标 | Octop | dsh (DeepSeek Harness) | Pi |
|---|---|---|---|
| 仓库 | [TencentCloud/Octop](https://github.com/TencentCloud/Octop) | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | [earendil-works/pi](https://github.com/earendil-works/pi) |
| Star | **9,962** | **253,545** | **116,241** |
| Fork | 1,278 | 30,571 | 14,823 |
| Open Issues | **830** | 0（仓库关闭了 Issues，走 Discussions） | 347 |
| 主语言 | Python 3.12+ | TypeScript | TypeScript |
| 许可 | MIT | MIT | MIT |
| 首次开源 | 2026-07-08（约 3 个月） | 2026-08-13（约 2 个月） | 2025-08-09（较早） |
| 版本/状态 | **v1.0.2，已 GA** | **0.2.0-rc.x，Developer Preview，官方明示会有 breaking change** | **v1.3.0** |
| 官网/文档 | octop.cloud | deepseek-harness.github.io | pi.dev |
| 出品方 | 腾讯云 | DeepSeek | earendil-works（个人/小团队主导，Shopify CEO 等外部贡献者活跃） |

**读这张表要注意**：
- dsh 的 25 万 Star 是"DeepSeek 品牌 + 开源首日 2.8 万 Star"式爆发，**Star ≠ 生产可用度**，它自己标注 developer preview；
- Octop 是三者中**唯一已经 GA（v1.0.x）**、且面向"非开发者用户"的产品；
- Pi 的 Star 曲线是长跑型（3 个月 54k→98k，现 116k），npm 周下载量百万级，用户粘性高。

---

## 3. 架构对照

### Octop：单进程、一体化服务端

```
OctopServer
 ├─ DatabasePool        SQLite(WAL)/PostgreSQL
 ├─ SharedServices      依赖注入根
 ├─ ExpertCatalog       专家库（启动时扫描）
 ├─ UserManager → HarnessAgentManager → AgentRuntime
 │     ├─ HarnessAgent        Agent 运行时（octop-harness）
 │     ├─ HarnessProcessor    IM/UI/cron 统一入口
 │     ├─ ChannelManager      IM 网关（octop-gateway）
 │     └─ CronManager         APScheduler
 └─ FastAPI + uvicorn
```

- **Python + FastAPI**，前端 React 18 + TS + Vite + Ant Design；
- 无外部消息队列：Web/IM/cron 全部走进程内 `HarnessProcessor`，重启从控制面 DB 重建状态（有 ADR 记录）；
- 拆分为 4 个配套库：**octop-harness（运行时）、octop-gateway（IM 网关）、octop-memory（记忆）、octop-browser（CDP 浏览器）**；
- 数据全在 `~/.octop/`，可换 PostgreSQL、COS/S3、Docker 沙箱等 workspace 后端。

### dsh：Cordis 微内核 + 插件树 + Profile/Bundle

- 基于 **Cordis**（"时空可组合编程范式"，有 arXiv 论文），**连模型适配器、工具注册表、会话日志、Agent 循环本身都是插件**，"没有特权内核"；
- 一次 `dsh` 运行 = 启动时按**有序层**组装出的插件树：
  - **profile**（`web` / `headless` / `sdk` / `sdk-minimal` / `acp`）→ 决定装哪些 bundle；
  - **bundle**（`dsh-base` + `dsh-web-app` / `dsh-headless` / `dsh-sdk-app` / `dsh-acp-app`）→ 分发 Cordis 配置行；
  - 再叠加用户 `cordis.patch.yml` 和 `--patch` 覆盖层，任意一行都能被 patch 替换；
- 核心包：`core/session`（只追加事件日志）、`core/system-prompt`、`core/tools`、`core/agent`、`core/agent-loop`、`core/scope`、`llm/llm`；
- **能力 seam（缝）**：FS/subprocess/sandbox/subagent 都是可替换 provider —— 换一个 provider 即可把 Bash/PTY/LSP 整体搬到远程沙箱；
- 自带 **Electron 桌面端**、**TypeScript SDK**、**Python SDK**、**ACP**、Web UI（`npx @deepseek-ai/dsh web` → 127.0.0.1:3080）、HMR 热插拔。

### Pi：TypeScript monorepo 分层

| 层 | 包 | 职责 |
|---|---|---|
| L1 | `pi-ai` | 统一多模型 API（本质只对接 OpenAI Completions/Responses、Anthropic Messages、Google 四类协议）；**跨提供商上下文接力**（Claude 思维链转 `<thinking>` 标签）、全链路 Abort、工具结果与 UI 数据分离 |
| L2 | `pi-agent-core` | Agent 循环（消息→LLM→工具→回填→再调）；两种队列：**Steering**（插话，当前工具跑完就处理）、**Follow-up**（排队到本轮结束）；事件驱动 |
| L3 | `pi-coding-agent` | 编码 Agent CLI：`AgentSession`（JSONL 树形会话、自动 compaction、工具注册、jiti 动态加载 TS 扩展）、4 种运行模式 |
| L4 | `pi-tui` | 差分渲染终端 UI（Retained Mode + `CSI ?2026h/l` 同步输出，Ghostty/iTerm2 零闪烁） |
| 附加 | `chord` / `pi-durable` / `pi-telemetry` | 热重载插件宿主、持久化会话/任务/文档运行时、厂商中立遥测 |

---

## 4. 能力矩阵

| 能力 | Octop | dsh | Pi |
|---|---|---|---|
| **主要界面** | Web dashboard、桌面端（Win/mac/Linux/FnOS）、CLI、IM | Web UI（3080）、Electron 桌面、CLI、SDK、ACP | 终端 TUI、CLI、SDK |
| **IM 集成** | ✅ 飞书/钉钉/QQ/微信/企微/Telegram/Discord 等 | ❌（走插件/自建） | ❌ |
| **多用户/隔离** | ✅ JWT 多用户 + 每用户独立 agent/workspace | 多会话；团队协作靠 Agent Teams | ❌ 单用户 |
| **多 Agent** | AgentTeams（Beta，协调者+成员专家）、平行 Agent | 子 Agent、**实验性 Agent Teams**、workflow、goal、schedule | ❌ 内置无 sub-agent，设计上让你用 `bash` 自调 `pi -p` 或用 tmux |
| **工具面** | 非常大：浏览器自动化、远程桌面、终端 AI、知识库 RAG、cron、连接器（OAuth+MCP） | 标准工具面 + **PTC 代码模式**（`run_code` 一次执行多工具）+ 插件工具 | **仅 4 个**：read/write/edit/bash（可选 grep/find/ls） |
| **上下文/成本策略** | 记忆（Octop Memory）+ 知识库 + 压缩 | 会话事件日志、compaction、Goald；四预设从"标准"到"极简（2 工具、冻结提示词、99.93% 缓存命中）" | 极简系统提示词（<1000 token）+ 树形会话 + 自动 compaction；刻意追求 cache 命中与低成本 |
| **扩展机制** | 插件、Connectors（OAuth/MCP）、专家库/专家市场、Skills | **Cordis 插件（一切皆插件）**，HMR 热插拔，社区 `dsh-plugin` 生态 | Extensions（TS 代码）、Skills（Markdown）、Prompt Templates、Themes、Packages（npm/git 安装） |
| **模型接入** | OpenAI 兼容、DashScope(Qwen)、Ollama 等 | 多 provider 适配器（含 OpenAI/Claude/DeepSeek 等） | 15+ 提供商，`/login` OAuth（Claude Pro/ChatGPT Plus），**会话中途换模型并做上下文转换** |
| **记忆/知识** | ✅ 可迁移记忆 + RAG 知识库（可部署内共享） | 会话日志为事实源；长期记忆靠插件/技能 | ❌ 内置无长期记忆/知识库 |
| **权限/沙箱** | 工具审批、shell 命令护栏（可编辑规则）、PII 脱敏、JWT 隔离 | 沙箱后端 + 审批策略 + 凭据管理（base bundle 内置） | **默认无任何权限系统**（作者刻意为之）；需隔离时用 Gondolin 微虚拟机 / Docker / OpenShell |
| **IDE/协议集成** | ACP 双向（`octop acp`；可委派 OpenCode/Claude Code/Codex） | ACP server、Webhook、SDK（TS/Python） | RPC（stdin/stdout JSON）、TS SDK；无内置 MCP（建议 CLI+README 替代，省上下文） |
| **部署方式** | 一键脚本 / PyPI / Docker / 桌面安装包 / FnOS NAS | `npx @deepseek-ai/dsh web` / 源码 pnpm / Electron | 一键脚本 / npm / Nix / 容器 |
| **企业化** | 首次向导、系统服务、备份/恢复、TLS、SSO（v1.0.1 起）、审计 | 开发者预览，企业能力靠自己拼 | 无（个人工具定位），仅供应链加固（锁定依赖、审计签名） |

---

## 5. 扩展机制的三种哲学

这是三者最本质的差异，比功能列表更值得看。

**Octop：插件 + 连接器 = "能力即服务"**
- 目标是**让终端用户点几下就扩展**：dashboard 里装插件、连 OAuth、接 MCP、建专家；
- 扩展的粒度是"业务能力"（腾讯文档、腾讯会议、Notion、Obsidian、Dify、浏览器、远程桌面……）；
- 代价是**内核可替换性弱于 dsh**：你扩的是产品，不是重写产品。

**dsh：一切皆插件 = "可替换的内核"**
- 连 Agent 循环、模型适配器、会话日志都是插件；"没有特权核心可以打补丁"，你只能在旁边挂插件，卸载时副作用自动回滚；
- 扩展的粒度是"架构组件"，因此能做到：换 provider 就把 Bash/PTY/LSP 搬到远程沙箱；用 **创造模式（create preset）** 让 Agent 检查自己运行的 Cordis 环境、现场写插件挂上去；
- 四种预设（标准 / PTC / 极简 / 创造）本质就是**打包好的插件集合**，所以模式切换 = 换一套"活动面"，不是原地开关；
- 代价：**插件疲劳**（社区实测 101 个插件只有 11 个能干净 `npm` 装上），学习曲线陡（要先懂 Cordis）。

**Pi：Extensions + Skills + Packages = "渐进式披露"**
- 三层：**Extensions**（TS 代码：注册工具/命令/快捷键/拦截调用，`/reload` 热加载）、**Skills**（Markdown 指令，平时只占一行描述，触发时再加载全文）、**Packages**（前两者 + 模板/主题打包，npm 或 git 分发）；
- 明确"不做的功能"给了更简单的替代：Plan Mode→写 `PLAN.md`；Sub-agents→`pi -p` 自调用 + tmux；MCP→CLI 工具 + README；后台进程→tmux；
- 代价：**能力要自己攒**，且没有官方托管/企业支持。

一句话：**Octop 把复杂度做进产品，dsh 把复杂度做进架构，Pi 把复杂度留给用户。**

---

## 6. 安全与权限模型（选型常被忽略但很关键）

| | Octop | dsh | Pi |
|---|---|---|---|
| 默认权限 | 有审批、命令护栏、PII 脱敏 | 沙箱 + 审批策略（可配） | **无**（模型直接执行） |
| 隔离手段 | JWT 多用户、Docker workspace 后端 | `ctx.sandbox` 可替换后端、远程沙箱 | 容器化三选一：Gondolin 微 VM / Docker / OpenShell |
| 适合场景 | 多人共享一台机器、企业内网 | 需要策略化管控的产品 | 可信环境 + 自担风险 |

Pi 的立场值得单独说明：作者认为"Agent 能写代码、能执行代码、能联网时，权限弹窗只是安全感"，所以默认全权限；需要边界就整个容器化。**这条路适合个人开发者，不适合多租户/企业环境。**

---

## 7. 生态与社区现状

- **Octop**：腾讯云官方维护，配套 4 个子库 + 桌面端 + FnOS 打包；Discord + 企微群；**830 个 open issue**，社区文章记录其 3 个月 5 次大改（含从 LightClaw ACE 重构为 Octop）以及曾出现 CVE 与 issue 压力 —— 迭代极快，但**稳定性风险真实存在**。
- **dsh**：DeepSeek 官方，Star/Fork 量级最大（25 万+/3 万+），有官方文档站、Discord、`dsh-plugin` 主题标签、社区插件目录/审计站（第三方 dshbase）；**Issues 关闭**，反馈走 Discussions —— 官方节奏主导，社区可参与但入口收敛。
- **Pi**：作者个人色彩强但生态最"野"：npm 周下载百万级，官方示例 50+，社区包如 **pi-autoresearch**（自动性能优化循环，7.9k Star，Shopify CEO 贡献 32 个 commit，Shopify 内部单测提速 300 倍）；**新贡献者 PR 默认自动关闭**（维护者每日人工复核）—— 质量控制严，但贡献门槛体感高。

---

## 8. 怎么选（按场景）

| 你的情况 | 推荐 | 理由 |
|---|---|---|
| 想给家庭/小团队搭一个"自己的 AI 助手"，要 IM、知识库、定时任务、多用户 | **Octop** | 唯一 GA、开箱即用、Web+IM+桌面全覆盖，隐私本地化 |
| 企业内网部署、需要审批/护栏/多租户 | **Octop**（注意跟进 CVE 与版本） | 内建 JWT 隔离、工具审批、PII 脱敏、SSO |
| 要做**自己的 Agent 产品**、深度定制运行时、插件化、多模型/多形态（Web/Desktop/SDK/ACP） | **dsh** | Cordis 内核可替换性最强，profile/bundle/patch 组合灵活，SDK 齐全 |
| 研究 Agent 架构、写论文/做基准、需要"能让 Agent 改造自己" | **dsh 创造/极简模式** | 极简模式是干净的模型裸测环境；创造模式可现场造插件 |
| 个人终端编程助手、看重低 token 成本/高缓存命中、喜欢"Unix 哲学" | **Pi** | 4 工具 + <1000 token 提示词；树形会话、四模式、SDK 嵌入 |
| 要嵌入自己的 Node/TS 应用、或做 CI 里的 headless agent | **Pi**（SDK/RPC）或 **dsh**（SDK profile） | Pi 更轻；dsh 更体系化 |
| 需要 sub-agent/plan mode/MCP 这类"重功能" | Octop 或 dsh | Pi 明确不做这些（或需自己用 bash 拼） |

**组合用法也很合理**：Octop 里可通过 ACP 委派外部编码 Agent（OpenCode/Claude Code/Codex）；dsh 可通过 subagent provider 把任务委派给"另一个产品"；Pi 可作为极简执行器被上层调度。

---

## 9. 风险清单

| 项目 | 主要风险 |
|---|---|
| Octop | 太年轻（3 个月）且迭代激进（5 次大改）；830 open issues；安全事件需持续跟进；Python 单进程在高并发/多用户下的性能边界未验证 |
| dsh | 官方明示 **developer preview + 会有 breaking change**；Cordis 学习成本高；插件生态"长尾不干净"；Issues 关闭导致问题可见度低 |
| Pi | **无权限系统**（误操作风险自担）；刻意缺功能，团队协作/企业治理需自建；单人主导的治理模式；依赖 npm 供应链（已做加固） |

---

## 10. 一个观察：三者其实在互相"借用"思路

- dsh 的**极简模式**（2 工具、冻结提示词、追求 99.93% 缓存命中）与 Pi 的极简哲学是同一套成本逻辑；
- dsh 的**创造模式**（现场造插件改装自己）是"一切皆插件"架构的直接红利，Octop/Pi 都做不到；
- Octop 的 **AgentTeams** 与 dsh 的**实验性 Agent Teams** 几乎同题竞速，说明"多专家协作"是 2026 下半年共同方向；
- Pi 的 **Packages/Skills 分层 + 渐进式披露** 正在被各家 Skills 机制吸收。

---

## 11. 数据来源

- [TencentCloud/Octop README](https://github.com/TencentCloud/Octop) ｜ [GitHub API 仓库数据](https://api.github.com/repos/TencentCloud/Octop)
- [DeepSeek Harness README](https://github.com/deepseek-ai/deepseek-harness) ｜ [架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md) ｜ [GitHub API 仓库数据](https://api.github.com/repos/deepseek-ai/deepseek-harness)
- [earendil-works/pi README](https://github.com/earendil-works/pi) ｜ [GitHub API 仓库数据](https://api.github.com/repos/earendil-works/pi) ｜ [pi.dev](https://pi.dev)
- [DeepSeek Harness：当"一切皆插件"成为 Agent 的新底座（阿里云开发者社区）](https://developer.aliyun.com/article/1756806)
- [一文吃透 Pi：10w stars 的极简 Agent harness（阿里云开发者社区）](https://developer.aliyun.com/article/1764109)
- [DeepSeek Harness 四种模式完整指南（dshbase）](https://dshbase.com/zh/blog/deepseek-harness-modes/)
- [开源三个月、5次大变更、9100+ Star：腾讯 Octop 的狂奔之路（腾讯云开发者社区）](https://cloud.tencent.com.cn/developer/article/2760558)
- [腾讯低调开源 WorkBuddy（36Kr）](https://eu.36kr.com/zh/p/3986712042322947)

> 注：Star/Fork/Issue 数为 2026-10-19 采集快照，会持续变化；功能以各仓库主分支文档为准。

> 沙箱机制专项对比（含 Claude Code / Codex / OpenCode）见：[六大Agent沙箱机制对比-DSH-ClaudeCode-Codex-OpenCode-Octop-Pi.md](六大Agent沙箱机制对比-DSH-ClaudeCode-Codex-OpenCode-Octop-Pi.md)
