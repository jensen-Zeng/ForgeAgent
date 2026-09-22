# ForgeAgent 项目交接说明

本文档面向后续接手本仓库的编程 Agent 或大语言模型。开始修改前先完整阅读本文，再按任务选择源码和专题文档。不要仅凭 README 标题、历史日志或测试名称推断功能已经通过真实环境验证。

## 1. 当前事实基线

- 项目：ForgeAgent，一个面向本地代码仓库的轻量、透明、可评测 Coding Agent。
- 当前包版本：`0.3.0.dev0`。
- Git：默认分支 `main`；公开版本与变更记录见 `CHANGELOG.md`。
- 当前开发线：`0.3.0.dev0`；核心安全模式和第一阶段可信 RLM 已有离线自动化覆盖，外部验收见路线图。
- 最近完整本地回归：`224 passed`。
- 最近总体覆盖率：`86.16%`，CI 门禁为 80%。
- GitHub CI 已在 Windows/Linux、Python 3.12/3.13 四个组合通过：[CI #2](https://github.com/jensen-Zeng/ForgeAgent/actions/runs/33252930217)。
- RLM 离线测试使用真实本地 IPython Kernel 和 Faux Provider；不代表真实模型质量。
- Docker 隔离、真实 Provider 效果与费用限制必须在实际部署环境中单独验证。
- 不得把 Fake HTTP 测试、reference 评测或模拟 Docker 调用描述为真实模型效果或真实容器隔离证据。

每次接手仍需重新运行：

```powershell
git status --short
git log -5 --oneline
.\.venv\Scripts\forge.exe doctor
```

不要假设工作区干净，也不要回退、覆盖或提交与当前任务无关的用户修改。

## 2. 建议阅读顺序

1. 本文：快速掌握事实、约束和入口。
2. [README.md](./README.md)：产品定位、快速开始和整体架构。
3. [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)：实现工作流、能力矩阵和发布门禁。
4. [docs/ROADMAP_PRIORITIES.md](./docs/ROADMAP_PRIORITIES.md)：只包含尚未完成的事项。
5. 与任务相关的 ADR、Docker、模型或失败案例文档。

新手背景说明见 [BEGINNER_GUIDE.md](./BEGINNER_GUIDE.md)，模型配置见 [MODEL_CONFIGURATION.md](./MODEL_CONFIGURATION.md)，安全边界见 [SECURITY.md](./SECURITY.md)。

## 3. 核心运行链路

```text
CLI
  → 分层配置与密钥校验
  → Provider 适配层
  → AgentRuntime
  → ContextManager
  → ToolRegistry
  → Workspace / Patch / Git / Command
  → TraceEvent + JSONL Sink
  → RunResult / evaluation report
```

显式可信 RLM 链路：

```text
forge rlm --trust-workspace
  → RLMApplication / shared UsageLedger
  → SessionRuntime / durable TurnQueue / LoopAgentEngine
  → SessionStore (.forge/sessions/<root-id>/)
  → trusted persistent KernelManager + HostBridge
  → ChildManager / AgentMessageRouter / ContextBuilder
```

关键原则：Runtime、工具和上下文层只使用项目自己的领域模型，不依赖 OpenAI 或 Anthropic SDK 对象。

## 4. 源码地图

| 路径 | 职责 | 修改时重点 |
| --- | --- | --- |
| `src/forge_agent/domain/models.py` | Provider 无关的消息、工具、预算、结果模型 | 公共 Schema 和序列化兼容 |
| `src/forge_agent/config.py` | TOML、环境变量、CLI 覆盖与脱敏配置 | 默认值、优先级、SecretStr |
| `src/forge_agent/models/` | Fake、OpenAI-compatible、Anthropic、Ollama | 协议规范化、重试、取消、Usage |
| `src/forge_agent/runtime.py` | Agent Loop 与停止条件 | 预算必须在每轮和工具调用前检查 |
| `src/forge_agent/engine.py` | Turn Engine、ConversationContext、取消域 | busy/取消必须在 finally 清理 |
| `src/forge_agent/session/` | JSONL Store、输入队列、单写 Runtime 与恢复 | fsync 后确认；不重放 uncertain 副作用 |
| `src/forge_agent/kernel/` | 持久 IPython 与严格 Host Bridge | 这是可信执行，不是沙箱；不得注入凭据或宿主对象 |
| `src/forge_agent/children.py` | 子 Agent 生命周期与 family 日志 | admitted 后立即返回；状态转换只追加 |
| `src/forge_agent/messages.py` | 家族内持久 mailbox 与 exactly-once 消费 | sender 由宿主绑定；拒绝跨家族和重复消息 |
| `src/forge_agent/compaction.py` | 固定章节摘要与近期上下文保留 | 摘要失败不得丢历史或伪装完成 |
| `src/forge_agent/rlm.py` | RLM 对象装配、共享预算和可信 shell | 子会话共享根预算但隔离 Context/Kernel |
| `src/forge_agent/context.py` | 初始上下文、压缩、去重、停滞检测 | 规则和失败观察不能被错误丢弃 |
| `src/forge_agent/tools/base.py` | 工具协议、参数校验、注册表 | 错误必须结构化，不能泄漏异常细节 |
| `src/forge_agent/tools/workspace.py` | 工作区和敏感路径边界 | Windows 路径、符号链接、越界 |
| `src/forge_agent/tools/read_only.py` | 列目录、搜索、读文件 | 输出上限、二进制和敏感文件 |
| `src/forge_agent/tools/patch.py` | 精确上下文补丁和原子写入 | 并发修改、审批、回滚、已有 Diff |
| `src/forge_agent/tools/git.py` | Git status/diff | 只读、超时、错误封装 |
| `src/forge_agent/policy.py` | `SAFE / REVIEW / DENY` 分类 | 模型不得影响风险决策 |
| `src/forge_agent/tools/command.py` | 审批、自动路由、本地/容器执行 | 无 Shell、净化环境、失败关闭 |
| `src/forge_agent/sandbox.py` | Docker 规格、超时清理、统一工厂 | 禁网、非 root、最小挂载和资源限制 |
| `src/forge_agent/tracing/` | 事件、脱敏、JSONL Sink | 任何输出前先脱敏 |
| `src/forge_agent/evaluation.py` | 重置任务、隐藏检查、指标和报告 | reference 结果不是模型得分 |
| `src/forge_agent/cli.py` | 用户入口和对象装配 | CLI 与 live evaluation 必须使用同一安全配置 |

## 5. 必须保持的安全不变量

### 文件边界

- 所有目标路径必须经过 `Workspace` 解析，并保持在明确的工作区根目录内。
- 敏感文件、越界路径、符号链接逃逸、任意删除和任意文件覆盖继续拒绝。
- 补丁使用精确旧文本/新文本编辑、预览 Diff、人工批准和原子替换。
- 不要用“模型说路径安全”代替本地验证。

### 命令边界

- 模型只能提供 `executable`、`args`、`cwd` 等结构化字段，不能提供 Shell 源字符串。
- `DENY`：Shell、提权、破坏性命令、网络客户端、危险 Git 操作等，在选择后端前拒绝。
- `REVIEW`：包管理、格式化等，默认需要本地人工确认。
- `SAFE`：只表示命令形状进入窄白名单，不代表目标仓库代码可信。
- 默认 `FORGE_EXECUTION_BACKEND=auto`：
  - `SAFE` 在清理过敏感变量的本地环境执行；
  - `REVIEW` 批准后强制进入 Docker；
  - Docker 不可用时返回 `sandbox_unavailable`，禁止降级本地。
- `docker` 模式让所有允许命令进入容器；`local` 是明确关闭自动沙盒的兼容模式。
- 审批、风险和后端由宿主本地策略决定，不能接受模型伪造的 `approved`、`risk` 或 `backend` 字段。

### 模型、费用和轨迹

- API Key 不写入仓库、`forge.toml`、测试输出、轨迹或异常消息。
- 本地费用预算只能在响应返回后累计已知费用，不能替代 Provider 控制台硬限额。
- 未配置显式价格或服务未返回 Usage 时，必须保留 `cost_complete=false` 语义。
- 所有终态都应产生唯一、机器可读的停止原因；取消和错误必须释放 Provider 资源。
- Trace Sink 失败不得覆盖主任务结果；写入轨迹前必须脱敏和截断。

### 可信 RLM 边界

- `forge run` 永远不启动 Kernel；只有 `forge rlm ... --trust-workspace` 可以启动。
- Kernel 继承当前用户权限，可以直接使用 `os`、`subprocess` 和文件 API，不构成 Docker 沙箱。
- Kernel 环境移除常见凭据变量，API Key、SessionStore、Provider 和 ChildManager 对象不得注入。
- Host Request 的身份由所属 Kernel/Bridge 绑定；拒绝 payload 中的伪造 session 身份和未知字段。
- 每个 session 一个串行 Kernel；大输出完整写 artifact，模型只看到有界预览。
- 会话恢复只重排尚未开始的输入；已开始但未确认完成的操作标记 `interrupted/uncertain`，禁止自动重放。

## 6. 配置速查

真实模型请求至少需要：

```powershell
$env:FORGE_MODEL_PROVIDER = "openai"  # 或 anthropic / ollama
$env:FORGE_MODEL_BASE_URL = "https://example.com/v1"
$env:FORGE_MODEL_NAME = "provider-model-id"
$env:FORGE_MODEL_API_KEY = "仅在当前终端提供"
$env:FORGE_AGENT_MAX_COST_USD = "1.00"
```

Ollama 不要求 API Key。价格快照、重试、超时和 Token 上限见 `MODEL_CONFIGURATION.md`。

命令执行默认值：

```powershell
$env:FORGE_EXECUTION_BACKEND = "auto"
$env:FORGE_DOCKER_IMAGE = "forge-agent:dev"
$env:FORGE_COMMANDS_APPROVAL = "risky"
$env:FORGE_PATCHES_APPROVAL = "always"
```

ForgeAgent 不自动加载目标工作区中的 `.env`。

RLM 默认限制可由 `[rlm]`、`[session]`、`[context]` 或对应 `FORGE_RLM_*` / `FORGE_CONTEXT_*` 环境变量调整。旧 `FORGE_CONTEXT_MAX_TOKENS` 继续映射到 `window_tokens`。

## 7. 标准验证命令

安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements\constraints.txt -e ".[dev,rlm]"
```

提交前：

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest --cov=forge_agent --cov-report=term-missing --cov-fail-under=80 -q
.\.venv\Scripts\python.exe -m pip check
git diff --check
git status --short
```

离线功能检查：

```powershell
.\.venv\Scripts\forge.exe config check --allow-missing-model
.\.venv\Scripts\forge.exe doctor
.\.venv\Scripts\forge.exe eval run --mode reference --repetitions 1
```

只运行受影响测试不能替代最终完整回归。若测试数量或覆盖率变化，同步更新 README、实施看板和本文事实基线。

## 8. 测试与证据解释

- `tests/test_model_providers.py` 使用模拟 HTTP，不联系真实服务。
- `tests/test_rlm_stage1.py` 使用真实本地 Kernel，但模型为 Faux/Mock；它证明状态、队列和桥接，不证明在线模型成功率。
- `tests/test_sandbox.py` 和命令工具中的 fake sandbox 验证命令规格和路由，不证明 Docker 宿主隔离有效。
- `reports/evaluation-reference` 的 20 任务 × 3 次、60/60 仅验证评测器和参考实现。
- `reports/context_ablation.md` 使用合成 Token 代理；约 57% 的下降不是模型成功率提升。
- 真实结论必须记录 Provider、模型 ID、Prompt 版本、任务快照、重复次数、Token、费用和失败类型。

## 9. 当前已知阻塞

以下事项不能仅靠本地代码声称完成：

1. Docker/WSL 安装、基础镜像和派生依赖镜像真实构建。
2. 禁网、非 root、只读根文件系统、敏感宿主文件不可达和资源限制的宿主实测。
3. 至少两类真实 Provider 各三次固定评测。
4. 至少一次带费用硬上限的真实 Provider RLM 全流程验收。
5. Provider 控制台费用硬限额验证。
6. 受保护 Release Environment 和标签发布工作流现场验证。
7. 可复现的公开端到端演示。

完整优先级和验收标准只维护在 `docs/ROADMAP_PRIORITIES.md`。已经本地完成的事项不要重新加入该文档。

## 10. 修改工作流

1. 先读 `git status`，识别已有修改。
2. 找到最小模块和相邻测试，不顺手重构无关代码。
3. 公共 Schema、环境变量、CLI 输出或安全策略变化时，先写/改测试。
4. 保持错误码和轨迹字段机器可读；新增字段考虑旧报告和 Fake Provider。
5. 修改 CLI 对象装配时，同时检查 `RuntimeEvalExecutor`，避免 live 评测绕过安全配置。
6. 修改命令策略时，至少覆盖 SAFE、REVIEW、DENY、审批拒绝、Docker 不可用和禁止本地回退。
7. 修改 Provider 时，覆盖文本、工具调用、非法 JSON、Usage、重试、超时和取消。
8. 完成后运行相关测试、静态检查和完整回归，再更新文档中的当前数字。

## 11. 明确暂不做

除非真实评测证明需要，不要直接加入：

- 第二阶段 Supervisor/Worker、detach/attach、跨进程 lease、Goal、Heartbeat 和 Cron；
- 向量数据库长期记忆；
- 自动 PR、Push、部署；
- 大规模 SWE-bench；
- AST/LSP、TUI、IDE 集成；
- 自动安装 Docker、WSL 或模型；
- 在默认安全模式开放 Shell、容器网络、Docker Socket 或主目录挂载。

需要改变这些边界时，先更新路线图或新增 ADR，再实现和测试。
