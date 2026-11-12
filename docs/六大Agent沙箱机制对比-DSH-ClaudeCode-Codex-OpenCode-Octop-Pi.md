# 六大 Agent 的沙箱机制对比（DSH / Claude Code / Codex / OpenCode / Octop / Pi）

> 采集时间：2026-10-29。依据各项目官方文档、开源仓库与源码说明整理；产品迭代很快，具体以官方最新文档为准。
> 姊妹篇：[三款开源Agent-Harness对比-Octop-DSH-Pi.md](三款开源Agent-Harness对比-Octop-DSH-Pi.md)（定位/架构/生态层面的对比）

---

## 0. 一句话结论

先给总览：**六家里四家是"OS 级围栏派"（Claude Code、Codex、DSH、Octop），两家是"纯审批派"（OpenCode、Pi），而 Octop 是混合体**。

| 项目 | 沙箱形态 | 默认状态 | 约束对象 | 一句话 |
|---|---|---|---|---|
| **DSH** | 可替换的 OS 进程沙箱 seam（bwrap/Landlock、Seatbelt、Windows ACL），支持远程 SSH 后端 | 由 preset 决定，默认 `workspace-write + ask` | 派生子进程（bash/pwsh）+ FS 策略 | 把"沙箱模式"做成插件 seam，**无法沙箱就 fail-closed** |
| **Claude Code** | OS 级 Bash 沙箱 + 本机网络代理 | **默认关闭**，需 `/sandbox` 或 `sandbox.enabled` | **只包 shell 命令**及其子进程 | "只沙箱 shell"，文件工具/MCP/hooks 都在外面 |
| **Codex** | OS 级沙箱 + 可选网络代理 | **默认开启**（`read-only` / `workspace-write`），网络默认关 | 模型生成的所有命令及其子进程 | 三平台原生实现最完整（含 Windows MXC） |
| **OpenCode** | **无 OS 沙箱**，纯工具级权限 | 默认宽松（多数 allow） | 工具调用 | `allow/ask/deny` 规则 + `--auto` |
| **Octop** | 组合式：工具审批/命令护栏 +（Linux bwrap 或 Docker 后端） | 取决于部署配置 | 执行/文件工具 | 产品级多租户；隔离强度取决于所选 backend |
| **Pi** | **无内置沙箱、无权限系统** | 无 | 无 | 安全 = 自己容器化 |

---

## 1. 先分清四个层次

讨论"沙箱"前必须拆开，否则会拿不同层的东西互相比较：

| 层次 | 作用 | 典型实现 |
|---|---|---|
| **L1 审批层**（Approval） | 动作发生前问不问人、问谁 | ask/never、on-request、auto-review 审核 agent |
| **L2 工具权限层**（Permission） | 这个工具/命令/路径允不允许 | allow/ask/deny 规则、命令前缀规则、HITL 审批 |
| **L3 OS 沙箱层**（Sandbox） | 操作系统强制的文件/网络边界，进程树继承 | Seatbelt、bubblewrap、Landlock、seccomp、ACL/受限令牌 |
| **L4 容器/VM 层**（Isolation） | 整个进程/机器隔离 | Docker、微 VM、远程沙箱、devcontainer |

六个项目的差异，本质是**在这四层里各选了几层、以及默认开哪几层**：

```
Claude Code : L1 强 + L2 强 + L3 有(仅shell, 默认关) + L4 可选
Codex       : L1 强 + L2 有 + L3 强(默认开, 三平台) + L4 可选(devcontainer/VM)
DSH         : L1 有 + L2 强 + L3 强(可替换/可远程, 默认开) + L4 靠部署
Octop       : L1 有 + L2 强 + L3 有条件(Linux bwrap / Docker 后端) + L4 可选(Docker/S3/远程桌面)
OpenCode    : L1 弱(可自动批准) + L2 强 + L3 无 + L4 靠外层
Pi          : L1 无 + L2 无 + L3 无 + L4 唯一依靠
```

---

## 2. 逐个拆解

### 2.1 DSH（DeepSeek Harness）—— 把沙箱做成可替换的显式 seam

**机制**
- 核心服务 `ctx.sandbox.confine(argv, policy)`：调用方把**即将 spawn 的 argv** 交给 sandbox provider，拿回"被包好的 argv + 执行事实"，然后自己 spawn。`dsh-bash-sandbox` / `dsh-pwsh-sandbox` 是消费者。
- 本地 provider（`dsh-sandbox-local`）按平台实现：
  - **Linux**：`bwrap`（bubblewrap）绑定 + **Landlock**；
  - **macOS**：**Seatbelt**（`sandbox-exec` 风格 profile）；
  - **Windows**：**ACL 受限令牌（restricted token）**后端，按会话/工作区分配随机私有 temp 目录与 SID。
- **远程沙箱**：`dsh-sandbox-ssh` 把同一套策略应用到 SSH 远端（配合 SSH 文件/子进程 provider），即"文件工具和 Bash 一起搬到远端"。
- **模式只有三种**，且**只描述文件效果**：`read-only` / `workspace-write` / `danger-full-access`。文档明确：**网络和进程可见性不在这个词汇表内**（网络不归进程沙箱管）。
- **按调用携带策略**（`SandboxExecutionPolicy`）：`mode` + `workspaceRoot`（会话不可变 cwd）+ `additionalWritableRoots` + `sessionId`。所以同一时刻，bash 可以在 `read-only` 下跑，而子 agent 的状态目录可以单独可写；被批准的提权重试是一个**新的、更宽策略的调用**。
- **强制完整性是"上报事实"**：`enforcement: 'full' | 'partial'`。旧 Landlock ABI、Windows ACL 的硬链接/未受限读/AppContainer ACL 边界都算 partial——需要绝对边界的消费者必须据此拒绝。
- **fail-closed**：没有可用后端时抛 `SandboxUnavailableError`（`SANDBOX_UNAVAILABLE`）；**"静默地不加沙箱直接跑"是明令禁止的**。
- **审批层**：`approval/policy` 只有 `ask` / `never`；结果封闭且 fail-closed（只有 `allowed-once` 算放行，`rejected/cancelled/unavailable` 全按拒绝）。审计事件只进日志、不进模型上下文。
- **预设**把两个旋钮打包：默认 `workspace-write`（workspace-write + ask）与 `danger-full-access`（danger-full-access + never），`custom` 为派生值，另有实验性 **Auto review** 审核集成（当前会话级）。
- 拒绝语句识别按后端方言（bwrap 的 EROFS、Landlock 的 EACCES、Seatbelt 的 EPERM），并区分"沙箱正常拦截"与"沙箱自身启动失败"。

**特点**：不做产品级网络代理；网络交给审批/其他插件。优点是**架构极干净、可替换、可远程、fail-closed**；缺点是理解成本高，且部分平台（旧内核/Windows）只能拿到 partial。

---

### 2.2 Claude Code —— 只沙箱 shell，网络走本机代理

**边界（最容易误解的一点）**
- 沙箱包的是 **Bash、PowerShell、Monitor 命令以及它们启动的进程**。
- **明确不包**：Claude 自己的文件工具（Read/Edit/Write）、WebFetch/WebSearch、**MCP server**、command hooks、plugin monitors、LSP server、状态栏/apiKeyHelper 等辅助命令——这些按权限规则跑，拥有完整权限。
- **平台**：macOS 用内置 **Seatbelt**；Linux/WSL2 依赖 **bubblewrap + socat**（可选 seccomp 过滤）；**原生 Windows 不支持沙箱**，要用沙箱得在 WSL2 里跑。
- 实现基于开源的 **`@anthropic-ai/sandbox-runtime`（SRT）**；SRT 自己还有 Windows 后端（专用 `srt-sandbox` 本地账户 + WFP 出站围栏 + 受限写令牌 + ACL），但 Claude Code 在原生 Windows 上并不启用沙箱。
- **默认关闭**：`/sandbox` 或 `sandbox.enabled: true` 才开；面板分 `auto-allow` 与 `regular permissions` 两种模式；`failIfUnavailable: true` 可在依赖缺失时直接拒绝启动（否则默认降级为不沙箱运行）。

**文件系统**
- 写：工作目录 + 每用户临时目录 + 显式 `--add-dir` 的目录；`filesystem.allowWrite/denyWrite` 扩展收敛。
- 读：默认几乎整机可读；`denyRead` 收口（注意 `allowRead` 优先于 `denyRead`，写方向则相反）。
- 受保护路径（如 `.git`、`.claude`）在 `bypassPermissions` 下也可能被写；文档还专门提示"名字叫 objects/refs 的目录会消失"这类 git 保护边界。
- 已知降级：被 `excludedCommands` 匹配的命令**完全在沙箱外**运行（无文件限制、无网络代理）。

**网络**
- 沙箱内**没有直接出网**，全部指向本机代理；`network.allowedDomains` 默认空。
- 允许列表按主机名判断；解析到回环/内网/云元数据地址会被拒绝（防 DNS 重绑定/元数据偷取）。
- **默认不终止 TLS、不检查内容**，所以文档自己承认：域名放得宽（如 `github.com`）时存在 domain fronting 逃逸面；要更强就自定义代理 + 在沙箱内装 CA。
- 凭证保护是特色：**凭据掩码**——沙箱里只看到 sentinel，代理在发往允许主机时替换真值；AWS SigV4 可在代理侧重签；掩码需要 `network.tlsTerminate`。
- `httpProxyPort` 自定义代理接管后，Claude 自己的域名列表/审批/本地地址检查**对该流量失效**（责任转移给代理）。

**权限层**
- 模式：`default`(Manual) / `acceptEdits` / `plan` / `auto`（后台分类器）/ `dontAsk`（自动拒）/ `bypassPermissions`。
- 规则：deny > ask > allow（顺序固定，具体度不影响优先级）；`/permissions` 管理，规则落在 `.claude/settings.local.json` 或用户/托管设置。
- 还有"沙箱失败后申请无沙箱重试"的逃生门（可关）；`sandbox.autoAllow` 类自动放行只适用于沙箱内命令。

**特点**：**最完整的"审批 + 沙箱 + 网络代理 + 凭据掩码"组合**，但沙箱覆盖面只到 shell，且默认不开。企业可用 managed settings 强制开启、禁止放宽。

---

### 2.3 Codex —— 三平台原生 OS 沙箱，默认就开

**默认**
- 本机运行（ChatGPT 桌面 App / Codex CLI / IDE 插件）**默认启用 OS 强制沙箱**，**默认无网络**，写权限通常限于当前 workspace。
- 默认组合 ≈ `--sandbox workspace-write --ask-for-approval on-request`（文档称 "Auto" 预设）；版本受控目录推荐 Auto，非 git 目录可能先 `read-only`。

**平台实现**
- **macOS**：**Seatbelt**，用 `sandbox-exec -p <profile>` 按模式下策略；受限读时会追加一份裁剪过的平台策略而不是放开 `/System`。
- **Linux**：**`bwrap` + `seccomp`**；没有 `bwrap` 时回退到需要非特权 user namespace 的内置 helper；Ubuntu 24.04 需要额外 AppArmor profile（`bwrap-userns-restrict`）。
- **WSL2**：走 Linux 沙箱实现（0.115 起不再支持 WSL1）。
- **Windows**：三套实现——
  - **`mxc`**（推荐）：微软 **Microsoft Execution Containers** 原生进程隔离，无需管理员、无需额外账户/防火墙规则；需 Win11 24H2（build 26100.9278+）或 25H2（build 26200.9278+）；
  - **`elevated`**（旧版首选回退）：需要管理员批准的初始化，专用低权限沙箱用户 + 文件系统权限边界 + 防火墙规则 + 本地策略；
  - **`unelevated`**（旧版次选）：受限 Windows 令牌 + ACL 文件边界 + 环境级离线控制，网络隔离更弱、不支持 deny-read 路径。
  - 可用 `codex -c windows.sandbox=mxc sandbox ...` 单命令验证；`requirements.toml` 可限制允许的实现。

**模式与审批**
- 沙箱模式：`read-only` / `workspace-write`（默认）/ `danger-full-access`；**网络在 workspace-write 下默认关闭**，需显式 `sandbox_workspace_write.network_access = true`。
- 审批策略：`on-request`（默认）/ `never` / `granular`（按类别）；`approvals_reviewer = user | auto_review`——**自动审批由审核 agent 评估**（数据外泄、凭据探测、持久化削弱、破坏性动作），解析/超时失败均 fail-closed。
- **rules / execpolicy**：按命令前缀 allow / prompt / forbid，可在沙箱外给窄口子，比整体提权更好。
- 受保护路径：可写根内的 `.git`（含 gitdir 指针解析）、`.agents`、`.codex`、`.aws` 强制只读。

**网络**
- 开启网络后可选 `features.network_proxy`：域名 allow/deny（`deny` 优先）、Unix socket allowlist、`allow_local_binding=false` 默认封回环/私有网段、DNS 解析分类检查（失败/超时也拒）、SOCKS5/UDP 可选；危险开关（非回环监听、全放行 socket）默认关。
- 明确边界：代理只过滤**沙箱内命令及其子进程**；web search、app/connector 工具、MCP、浏览器、Codex Cloud 各走自己的控制面。

**调试**：`codex sandbox macos|linux|windows ...`（别名 `seatbelt` / `landlock`）可直接在沙箱里跑任意命令验证行为；`/status`、`/debug-config` 查看生效策略。

**特点**：**默认安全 + 三平台最全**，连 Windows 都有原生沙箱（MXC）；代价是 Windows 依赖系统版本与 IT 配置，容器内跑可能因 namespace/seccomp 失效而需要外层容器兜底。

---

### 2.4 OpenCode —— 没有 OS 沙箱，只有工具权限系统

**机制**
- `opencode.json` 的 `permission` 按**工具名**配置：`read`、`edit`（覆盖 edit/write/patch）、`glob`、`grep`、`bash`、`task`、`todowrite`、`skill`、`lsp`、`question`、`webfetch`、`websearch`、`external_directory`、`doom_loop`。
- 取值：`allow` / `ask` / `deny`；支持对象语法做**输入级匹配**（如 `bash: {"git *": "allow", "rm *": "deny", "*": "ask"}`），**最后匹配的规则胜出**，通配 `*`/`?`，支持 `~`/`$HOME` 展开。
- `external_directory`：所有触碰工作目录之外路径的工具调用额外过这一关（默认 ask）。
- `doom_loop`：同一工具调用连续 3 次相同输入时触发（防死循环，默认 ask）。
- 默认**偏宽松**：多数权限 `allow`；但 `.env` 类文件读默认 `ask`。
- `--auto` / `opencode run --auto`：自动批准所有"本来要问"的请求，但**显式 deny 仍然生效**。
- 审批 UI 三选一：once / always（按工具建议的 pattern 在本会话内放行）/ reject；每个 agent 可覆盖权限（agent 规则优先）。
- 另有 **policies**（实验）：管控"能不能用某个 provider"（`provider.use`），属于资源治理，不是沙箱。

**边界**
- 官方文档中没有 OS 级文件/网络围栏：`bash` 一旦 allow，就是**以你的用户权限执行**；`read`/`edit` 的工具级规则拦得住工具调用，拦不住 bash 里的 `cat`/`python`。
- 需要真正隔离时只能套外层容器/VM（不是内置能力）。
- v2 版本文档中权限模型仍是这套 allow/ask/deny（`--auto` 行为一致）。

**特点**：配置灵活、默认省事，适合"信任自己机器、只想少被打断"的个人用户；**不适合不可信仓库/多租户**。

---

### 2.5 Octop —— 产品级组合：审批 + 命令护栏 + 后端隔离

Octop 不是单一沙箱，而是四层叠加：

1. **多用户隔离（L1）**：JWT 认证、按用户/专家隔离 workspace 与权限（管理端角色）。
2. **工具审批 + Shell 命令护栏（L2）**：
   - HITL 工具审批（`/settings/hitl`：`tool_guard_require_approval`、`show_approval_ui`），高风险工具/命令可在执行前等用户批准（`resume_octop_expert` 恢复）；
   - Shell 命令 allow/deny/require_approval 规则，规则文件在 `~/.octop/security/tool_guard/`；
   - PII 脱敏中间件：敏感数据在离开工作区前被处理。
3. **执行/工作区后端（L3/L4）**：agent 的 workspace 与 `execute` 可落在不同后端——
   - **`local_shell` / `filesystem`**：在宿主机执行。**唯一边界条件**：Linux + `virtual_mode=True` + `root_dir` 非主机 `/` + 宿主有 `bwrap` 时，harness 自动路由到 **`BubbledLocalShellBackend`**，把 `execute`（含 Skill 脚本）包进 **bubblewrap**（工作根绑到 `/`）。**其余情况（macOS、无 bwrap、filesystem 后端、宿主根目录）没有目录狱**，只做虚拟路径映射/路径改写；
   - **`docker` 后端**：把 `ls/read/write/execute` 全部放进容器（经 Docker Python SDK），**不自动 bind mount**（容器内同名绝对路径）；默认 `allow_network=false`，可配 `memory`/`cpus`/`pids_limit`/`command_timeout`/`volumes`；容器命名按 `sandbox_scope`：`agent`（每专家一个）/ `user`（同用户多专家共用）/ `fixed`；按需创建，删除专家不删容器，`destroy()` 才 stop+remove；
   - 其他后端：`opensandbox`、COS/S3、PostgreSQL 等（对象存储/远端工作区，不是安全沙箱，主要是存储隔离）。
4. **高危宿主能力（不在沙箱内）**：远程桌面（控制宿主桌面）、浏览器 AI+、终端 AI+ 都是**宿主级控制面**，应视为高权限能力单独治理。

**特点**：对"家庭/小团队自托管、多用户、IM 入口"场景最完整；但**隔离强度取决于 backend 选择**——默认 host 模式在非 Linux 环境可能等于"有审批、无围栏"。部署时要把"用 Docker 后端 + 收紧命令护栏 + 开审批"当成安全基线。

---

### 2.6 Pi —— 明确不做沙箱，安全靠容器化

**立场**
- README/安全文档明确：**Pi 没有内置的权限系统限制文件/进程/网络/凭据**，默认以启动用户的权限运行；**不会每次工具调用前征求批准**。
- "项目信任（project trust）"只控制**是否加载项目内的 `.pi/settings.json`、MCP、extensions、skills、prompts、SYSTEM.md**——这是启动期供应链防护，**不是运行时边界**。
- 作者观点：Agent 能写代码、执行代码、联网时，权限弹窗只是"安全感"；要边界就做真隔离。

**官方给出的隔离方式（容器化文档）**
| 方式 | Pi 在哪 | 隔离什么 | 凭据处理 | 适用 |
|---|---|---|---|---|
| **Plain Docker** | 容器内 | Pi 全部 + 内置工具 + `!` 命令 + 扩展 | 凭据传进容器 | 简单本地边界 |
| **Docker Sandboxes** | 托管沙箱 | 同上 | **真凭据留在宿主，由代理替换**（`sbx` kit） | 不想把 provider key 放进沙箱 |
| **NVIDIA OpenShell** | 本地或远端沙箱 | 同上 | 策略化凭据与推理路由 | 需要 FS/进程/网络/凭据策略 |
| **Gondolin 扩展** | 宿主（只把工具路由进 VM） | 仅内置工具与 `!` 命令 | 凭据在宿主，但命令继承宿主环境变量 | 本地微 VM + 保留宿主界面 |

- Gondolin 是本地 Linux **微 VM（QEMU）**，扩展把 `read/write/edit/bash/grep/find/ls` 覆写为在 VM 内执行，宿主工作目录挂到 `/workspace`；**其他扩展工具仍在宿主**，除非它们也显式路由。
- 注意环境变量会带入 VM，所以"Gondolin 不构成凭据边界"；同理 `~/.pi/agent` 挂进容器等于把凭据/会话交给容器。

**特点**：最"诚实"的威胁模型（承认无沙箱），把隔离责任交给容器/VM；配合 Pi 的极简工具面（4 个工具），攻击面相对可控，但**多租户/不可信代码场景必须自己上容器**。

---

## 3. 横向对比大表

| 维度 | DSH | Claude Code | Codex | OpenCode | Octop | Pi |
|---|---|---|---|---|---|---|
| **OS 级沙箱** | ✅ 可替换后端 | ✅ 仅 shell，默认关 | ✅ 默认开 | ❌ | ⚠️ Linux bwrap / Docker 后端才有 | ❌ |
| **默认状态** | preset：workspace-write+ask | sandbox off；权限默认 Manual | sandbox on；network off | 多数 allow | 部署决定 | 无 |
| **约束对象** | spawn 的子进程 + FS 策略 | Bash/PowerShell/Monitor 及子进程 | 模型生成的命令及子进程 | 工具调用 | execute + 文件工具（取决于后端） | 无 |
| **文件边界** | workspaceRoot + 额外可写根；read-only 只留 /dev/null 类 sink | 写 cwd+temp+add-dir；读默认全机；protected paths | workspace 可写；.git/.agents/.codex/.aws 只读 | 工具级路径规则 | host 模式无狱；bwrap 时工作根绑 /；Docker 时容器内 | 无 |
| **网络隔离** | ❌ 不在 SandboxMode 语义内 | ✅ 本机代理 + 域名 allowlist（默认空） | ⚠️ 默认关；可选 network_proxy + 域名策略 | ❌（仅 webfetch 权限） | Docker 默认 `allow_network=false`；host 模式无 | ❌ |
| **审批** | ask/never，fail-closed | 6 种权限模式 + deny>ask>allow 规则 | on-request/never/granular；auto_review | allow/ask/deny + `--auto` | HITL 审批 + 命令护栏 | 无 |
| **企业强制** | 部署配置/插件组合 | ✅ managed settings 强制、禁放宽 | ✅ requirements.toml / 托管配置 | 有限（enterprise 文档未覆盖沙箱） | ✅ 管理端/多用户/SSO | ❌ |
| **失败策略** | **fail-closed**（无后端即报错） | 默认降级不沙箱；可 `failIfUnavailable` | 启动告警；沙箱不可用时可外层容器 | n/a | 条件不满足则**静默无狱**（需注意） | n/a |
| **完整度上报** | ✅ full/partial | — | 各实现能力不同（unelevated 更弱） | — | — | — |
| **远程/远端** | ✅ SSH sandbox 后端 | 自定义代理 / 云端会话 | Cloud/Dev Container | 外层自建 | Docker / opensandbox | OpenShell 远端 |
| **调试工具** | 错误码 + 拒绝方言分类 | `/sandbox` 面板 + `claude doctor` | `codex sandbox macos/linux/windows` | 无 | Admin 探测/test 沙箱 | 无 |

---

## 4. 平台支持矩阵

| 平台 | DSH | Claude Code | Codex | OpenCode | Octop | Pi |
|---|---|---|---|---|---|---|
| **macOS** | Seatbelt | Seatbelt | Seatbelt（`sandbox-exec`） | 无沙箱 | 无狱（仅审批/护栏），或 Docker | 无（靠容器） |
| **Linux** | bwrap + Landlock（可能 partial） | bubblewrap + socat + seccomp | bwrap + seccomp | 无沙箱 | **bwrap（条件满足时）** 或 Docker | 无（靠容器） |
| **Windows** | ACL 受限令牌（partial） | ❌ 原生不支持，需 WSL2 | ✅ MXC / elevated / unelevated | 无沙箱 | 无狱，或 Docker | 无（靠容器） |
| **WSL2** | 走 Linux 语义 | ✅ bubblewrap | ✅ Linux 沙箱 | 无沙箱 | WSL2 下视作 Linux | 无 |
| **容器内运行** | 需注意嵌套 bwrap/Landlock | bwrap 需 `enableWeakerNestedSandbox` | 建议外层容器 + danger-full-access | 无沙箱 | Docker 后端本身即容器 | 原生支持 |

---

## 5. 网络隔离的三种做法

1. **代理 + 域名 allowlist（Claude Code、Codex network_proxy）**
   - 沙箱内无直接出网，流量走宿主代理；
   - 域名匹配 + 解析地址检查（防回环/内网/云元数据/DNS rebinding）；
   - 默认不检查 TLS 内容（Claude 明确提示 domain fronting 风险），需要更强就自建 TLS 检查代理。
2. **容器网络开关（Octop Docker 后端）**
   - 默认 `allow_network=false`，需要时显式打开；粒度是"容器有没有网"，不是域名级。
3. **不做网络隔离（DSH 的 SandboxMode、OpenCode、Pi）**
   - DSH 把网络排除在进程沙箱语义外，交给审批/插件；OpenCode 只有 `webfetch`/`websearch` 权限；Pi 完全不管。

---

## 6. 几个容易踩的坑

**Claude Code**
- 沙箱**只包 shell**：`Read/Edit/Write` 文件工具、MCP server、hooks、LSP 都在外面，`denyRead` 拦不住 Read 工具。
- 原生 Windows 无沙箱，要隔离必须进 WSL2。
- `excludedCommands`、`--dangerously-skip-permissions` 类逃生门会直接移除边界。
- 自定义代理接管后，内置域名列表/本地地址检查失效。
- macOS 上 git over SSH 在沙箱内会失败（代理隧道无法认证）。

**Codex**
- Linux 需要 `bubblewrap`，Ubuntu 24.04 还要额外 AppArmor profile。
- Windows 原生沙箱依赖系统版本（MXC 要 24H2/25H2 特定 build）与 IT 配置；`elevated` 回退需要管理员初始化。
- 网络默认关；开了 `network_access` 不等于有域名管控，还要显式开 `features.network_proxy`。
- 在受限容器里沙箱可能起不来，官方建议外层容器 + `danger-full-access`。

**DSH**
- 沙箱只管文件效果，**网络不在其中**，别指望它挡外联。
- 旧 Landlock ABI / Windows ACL 只报 partial，要求绝对边界时必须检查 `enforcement`。
- "静默不加沙箱"被设计禁止，遇到 `SANDBOX_UNAVAILABLE` 是预期行为，不是 bug。

**Octop**
- host 模式的 bwrap 狱**只在 Linux + virtual_mode + 非 `/` 的 root_dir + 有 bwrap** 时成立；macOS 或未装 bwrap 时**静默无狱**。
- Docker 后端不做 bind mount，容器内路径与宿主同名但不同盘；容器删除专家不清理，要 `destroy()`。
- 远程桌面/浏览器/终端是宿主控制面，不属于沙箱范围。

**Pi**
- 无审批、无沙箱：模型产生什么命令就按你的用户权限执行。
- Gondolin 只隔离内置工具；其他 extension 仍在宿主执行；环境变量会带进 VM（不是凭据边界）。
- 项目信任只管"加载什么"，不管"能做什么"。

**OpenCode**
- 默认宽松（多数 allow），`--auto` 进一步自动批准；只有显式 deny 才硬拦。
- 权限是按工具输入 pattern 匹配的"软边界"，拦不住 bash 内部的间接访问。

---

## 7. 选型建议

| 场景 | 推荐 | 原因 |
|---|---|---|
| 企业内网、需要托管策略强制、审计 | **Codex**（requirements.toml）或 **Claude Code**（managed settings） | 审批 + OS 沙箱 + 网络代理 + 企业强制都是现成的 |
| 多平台（尤其 Windows）统一体验 | **Codex** | 唯一有原生 Windows 沙箱（MXC/elevated/unelevated） |
| 自建 Agent 平台、要求可替换/可远程/严格 fail-closed | **DSH** | 沙箱是 seam，可用 SSH 远端，强制上报 full/partial |
| 只关心"别在我机器上乱跑"，做编码任务 | **Claude Code**（开 `/sandbox`）或 **Codex**（默认） | 两者默认/一键即可获得 shell 级围栏 |
| 信任环境、要少打扰、快速干活 | **OpenCode** / **Pi** | 权限模型轻，甚至无审批；配合容器使用 |
| 家庭/小团队自托管、IM 多入口、多用户 | **Octop** | 工具审批 + 命令护栏 + 多用户隔离；**务必配 Docker 后端** |
| 跑不可信仓库/生成代码 | **Pi + 容器**（Docker/OpenShell/Gondolin）或 **Codex/Claude Code 在 devcontainer/VM 里** | 单靠应用层权限不够，需要 OS/容器边界 |

---

## 8. 附：OpenSandbox（opensandbox-group）与这六家的关系

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) 是 **Agentic AI Foundation（AAIF）** 旗下的通用沙箱平台：Apache-2.0、Python 生态，本地用 Docker 起步、生产走 Kubernetes，统一沙箱生命周期 API；带 Fast Sandbox（Firecracker，P50 64ms / P99 125.4ms，支持 pause/resume）、ingress/egress 网络策略、Credential Vault、五语言 SDK（Py/Java/TS/C#/Go）、`osb` CLI 和 MCP server。截至 2026-10-29 约 16.2k stars。

**关键：方向不同，不要混为一谈。**

- **A. 把 OpenSandbox 当"执行后端"**（Agent → OpenSandbox）：Octop（官方）、DSH（社区插件）、Pi（社区扩展）。
- **B. 被装进 OpenSandbox 里运行**（OpenSandbox → Agent CLI）：Claude Code、Codex CLI、OpenCode（官方示例）。

| 项目 | 与 OpenSandbox 的关系 | 官方/社区 | 细节 |
|---|---|---|---|
| **Octop** | ✅ **执行后端**，与 `local_shell`、`docker` 并列的 sandbox backend | 官方 | `octop-harness` 提供 `OpenSandbox(BaseSandbox)`（`src/octop_harness/backends/opensandbox_sandbox.py`），文件工具与 `execute` 都跑在远端沙箱；Octop 主仓 `opensandbox_deps.py` 按需安装 `opensandbox>=0.1.16,<0.2`；配置项 `image/domain/api_key/protocol/timeout/command_timeout/use_server_proxy`；resolver 中 `{"local_shell","docker","opensandbox"}` 同属"可执行后端" |
| **DSH** | 🧩 **社区插件**：整个执行世界搬进 OpenSandbox | 社区 | [`codebam/dsh-opensandbox`](https://github.com/codebam/dsh-opensandbox)：替换 `ctx.subprocess` / `ctx.sandbox` / `ctx.fs` 三个服务，把 bash、PTY、grep/glob、文件工具全部路由到容器；以 dsh profile bundle 形式从 Plugins 页安装；自报 `partial` enforcement（容器本身即边界）；目前 1 star、2026-09 创建，成熟度待观察 |
| **Pi** | 🧩 **社区扩展**：把内置工具路由到 OpenSandbox | 社区 | [`pi-extension-opensandbox`](https://pi.dev/packages/pi-extension-opensandbox)（pi.dev，作者 okliuxing，v0.1.9，2026-09-22）：server-side extension factory，启动远端沙箱并让内置工具在其中执行 |
| **Claude Code** | 🏃 作为**工作负载**跑在 OpenSandbox 里 | 官方示例 | OpenSandbox `examples/claude-code`：容器内 `npm i -g @anthropic-ai/claude-code`，用 `claude -p ... --output-format json` 无头运行、`--resume` 续会话；无人值守场景建议 `--dangerously-skip-permissions`（因为外层已有沙箱边界） |
| **Codex CLI** | 🏃 作为**工作负载**跑在 OpenSandbox 里 | 官方示例 | `examples/codex-cli`：容器内装 `@openai/codex`，`codex exec --json` 取 `thread_id`，`codex exec resume` 续跑 |
| **OpenCode** | 🏃 作为**工作负载**跑在 OpenSandbox 里 | 官方示例 | `examples/opencode`：容器内 `opencode run`，默认免费模型、可换 provider |

**Octop 的 OpenSandbox 后端具体怎么调？** 不是 MCP，也不是 CLI/本地 socket，而是 **agent runtime 进程内直接调用 `opensandbox` Python SDK（HTTP/JSON 打到沙箱服务）**：

```
专家配置 config_json.backend = {"type":"opensandbox","image":...,"domain":...,"api_key":...,"protocol":"http",...}
        │
        ▼
infra/backend/resolver.py → resolve_backend(spec, workspace_dir)      # local_shell / docker / opensandbox 三种可执行后端
        │
        ▼
octop_harness.backends.opensandbox_sandbox.OpenSandbox(BaseSandbox)
        │  构造：opensandbox.SandboxSync.create(image, ConnectionConfigSync(domain, api_key, protocol, use_server_proxy))
        │  命令：sandbox.commands.run(cmd)          # 进远端沙箱
        │  文件：sandbox.files.write_files / read_bytes
        ▼
close()/destroy() → 销毁远端沙箱
```

- "远程"指**独立沙箱服务**：同机 `opensandbox-server`（Docker runtime，`localhost:8080`）或 K8s 集群；对 agent 进程来说都是 HTTP 远端。
- `use_server_proxy=false` 时直连沙箱发布的端点，`true` 时经生命周期服务代理。
- 沙箱**按 agent 启动创建、关闭销毁**，不是每条命令新建；SDK 由 Octop 自动安装（`opensandbox_deps.py`，`opensandbox>=0.1.16,<0.2`）。
- Octop 里的 **MCP 是另一条线**（Connectors/MCP 网关接工具），**ACP** 是委派编码 Agent，都与该沙箱后端无关。

> **常见误解**：OpenSandbox 后端不是"只用来跑代码/网络"的旁路组件。选了 `type: "opensandbox"` 后，agent 的**文件工具（read/write/edit/grep/glob）和 bash/execute 全部在远端沙箱内执行**——它就是该 agent 的"执行世界"。留在宿主上的只有控制面：会话/记忆/checkpoint、LLM API 调用、IM 网关、cron、Connectors/MCP、浏览器/远程桌面等 Octop 自身服务；审批与命令护栏是调用前的策略层，也不是沙箱本身。

**那文件到底在哪？** 三层，别混：

| 层 | 位置 | 说明 |
|---|---|---|
| 控制面数据 | 宿主机 `~/.octop/` | DB、secrets、会话/记忆/checkpoint、专家目录、配置 |
| 执行世界 | 取决于 backend：`local_shell`=宿主机；`docker`/`opensandbox`=容器/远端沙箱内的独立文件系统 | Octop 文档明确 Docker 是"**同名路径、两处落盘（不自动挂载）**"：宿主目录放 sessions/memory/checkpoints，容器内同路径是"专家可见工作区"；OpenSandbox 构建时甚至不传 `workspace_dir` |
| 跨层搬运 | backend 的 upload/download API（`BackendWorkspace`） | agent 启动时把 SOUL/skills/插件种子写进后端；之后文件工具的每次读写都走远端 API，不是本地 FS |

所以**"把文件传到远程沙箱"这个理解是对的**（至少对 docker/opensandbox 后端成立）：沙箱不是挂载你的宿主目录，而是自己的文件系统；想"宿主 = 沙箱"需要显式挂载（Docker `volumes`、OpenSandbox host-volume-mount + server `allowed_host_paths`），或者干脆用 `local_shell`。对照：DSH 的 OpenSandbox 社区插件就是把 session workspace **bind mount** 到沙箱同绝对路径，所以它的宿主文件在沙箱里直接可见——两种做法各有取舍（安全边界 vs 文件互通）。

补充事实：

- OpenSandbox 的官方 examples 还覆盖 Gemini CLI、Qwen Code、Kimi CLI、OpenClaw、LangGraph、Deep Agents、Google ADK、DeerFlow、VS Code、Chrome/Playwright/Desktop、Harbor 评测等；**没有 DSH / Pi / Octop 的官方 example**——这三家的集成都发生在"另一端"（它们主动接 OpenSandbox）。
- OpenSandbox 提供 `opensandbox-mcp`，Claude Code / Cursor 等 MCP 客户端可以在需要时通过 MCP 创建并使用沙箱。

**这种分工恰好印证了前面的判断：**

- DSH 的 `ctx.sandbox` 是标准 seam，社区插件只要替换 provider 就能把"执行世界"换成 OpenSandbox——这是"一切皆插件"的直接红利；
- Octop 把执行环境抽象成 `backend.type`，所以 OpenSandbox 只是第三种可选后端；
- Pi 靠 extension 在应用层路由工具；
- Claude Code / Codex / OpenCode 自身是"Agent 产品"，沙箱是内置固定的（Seatbelt/bwrap/权限），**没有"换沙箱后端"的插槽**，因此只能作为 workload 被装进 OpenSandbox——它们与 OpenSandbox 是"上层/外层"关系，不是竞品。

**选型提示**：需要"本地 Docker 起步 → 平滑上 K8s、要 egress 策略/凭据保险库/评测每 trial 一个沙箱"的团队，OpenSandbox 是现成的 L4 平台；六家里与它最"原生"的是 **Octop（官方后端）** 与 **DSH（seam 可插）**。

---

## 9. 来源

- DSH：[架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)｜[Process Sandbox](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md)｜[User Approval](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)｜[Permission Presets](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/permission-presets.md)
- Claude Code：[Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing)｜[Permissions](https://code.claude.com/docs/en/permissions)｜[Sandbox environments](https://code.claude.com/docs/en/sandbox-environments)｜[Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime)
- Codex：[Sandbox](https://developers.openai.com/codex/sandboxing)｜[Agent approvals & security](https://developers.openai.com/codex/agent-approvals-security)｜[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)｜[openai/codex](https://github.com/openai/codex)
- OpenCode：[Permissions](https://opencode.ai/docs/permissions/)｜[Policies](https://opencode.ai/docs/policies/)
- Octop：[agent-backend-file-io.md（bubblewrap / Docker backend）](https://github.com/TencentCloud/Octop/blob/main/docs/agent-backend-file-io.md)｜[user-guide.md（安全/审批/护栏）](https://github.com/TencentCloud/Octop/blob/main/docs/user-guide.md)｜[README](https://github.com/TencentCloud/Octop)
- Pi：[Containerization](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)｜[Run Pi safely](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)｜[README](https://github.com/earendil-works/pi)
- OpenSandbox：[open-sandbox.ai](https://open-sandbox.ai)｜[Examples 索引](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/examples/index.md)｜[Claude Code 示例](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/examples/claude-code.md)｜[Codex CLI 示例](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/examples/codex-cli.md)｜[OpenCode 示例](https://github.com/opensandbox-group/OpenSandbox/blob/main/docs/examples/opencode.md)
- OpenSandbox 集成方：[octop-harness opensandbox_sandbox.py](https://github.com/TencentCloud/octop-harness/blob/main/src/octop_harness/backends/opensandbox_sandbox.py)｜[Octop opensandbox_deps.py](https://github.com/TencentCloud/Octop/blob/main/src/octop/infra/backend/opensandbox_deps.py)｜[dsh-opensandbox](https://github.com/codebam/dsh-opensandbox)｜[pi-extension-opensandbox](https://pi.dev/packages/pi-extension-opensandbox)

> 说明：本文把"沙箱"严格理解为**技术强制边界**；审批提示、模型自我约束、项目信任都不算沙箱。若把"权限系统"也算进来，OpenCode 与 Pi 的对比结论会不同（前者强在规则，后者根本没有）。
