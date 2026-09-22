# ADR-0006：显式可信的持久 RLM 会话

- 状态：Accepted
- 日期：2026-08-20

## 背景

ForgeAgent 的默认安全模式通过受限工具、审批与 Docker 自动路由降低不可信仓库风险。RLM 需要持久执行模型生成的 Python、保留跨 cell 对象，并允许通过 Python 编排子 Agent；这种能力无法在不改变语义的情况下伪装成现有安全工具。

## 决策

保留 `forge run` 的全部既有边界，并新增独立的 `forge rlm` 命令。RLM 必须显式提供 `--trust-workspace`，否则在启动 Provider 或 Kernel 前拒绝。每个 session 使用一个当前用户权限的持久 IPython Kernel，宿主通过严格 Host Bridge 提供结构化 shell、child 与 message 请求。Kernel 不接收 API Key、Provider、SessionStore 或管理对象。

会话使用版本 1、UTF-8、只追加 JSONL。输入 fsync 后才确认；恢复时未开始输入可以继续，已开始但没有可靠终态的操作标记为 uncertain 并禁止自动重放。根与 child 拥有稳定 ID、`parent_id`、`root_session_id` 和 `generation=0/1`，为后续协议演进保留兼容字段。

## 备选方案

- 把 Kernel 放入现有 Docker：持久交互、Host Bridge、依赖镜像和容器逃逸面需要独立威胁模型；在完成该设计和宿主验证前不声称 Kernel 受到沙箱隔离。
- 让 Kernel 直接持有宿主管理对象：接口简单但会暴露凭据、身份和不可审计能力，拒绝采用。
- 使用数据库保存会话：第一阶段单进程单写，追加式 JSONL 更透明、易检查和易修复。
- 直接引入 Agent 框架：会隐藏循环、恢复和安全语义，不利于审计、测试和协议演进。

## 影响

- 用户必须把 RLM 视为执行可信仓库代码，而不是安全沙箱。
- Kernel 崩溃后只恢复持久会话，不承诺恢复任意 Python 对象。
- 子 Agent 在第一阶段仍是同一宿主进程中的 asyncio Task；父子 Context 与 Kernel 隔离，但 Provider 和根预算账本共享。
- 第二阶段 Supervisor/Worker、detach/attach、lease、Goal、Heartbeat 和 Cron 不属于本 ADR 的实现范围。

## 重新评估条件

真实长任务评测证明单进程或可信 Kernel 是主要可靠性瓶颈，且已有 Docker/远程 Worker 的独立身份、凭据、隔离和崩溃恢复设计。
