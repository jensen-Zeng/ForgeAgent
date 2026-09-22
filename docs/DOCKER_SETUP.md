# Docker 隔离环境

ForgeAgent 的容器规格用于运行不可信仓库命令，但容器不是虚拟机，也不能替代宿主机更新、最小权限账户和代码审查。

## 宿主要求

- Linux Docker Engine，或使用 WSL 2 后端的受支持 Docker Desktop；
- Client 与 daemon 都能通过 `docker version` 访问；
- 足够的镜像与构建缓存空间；
- 不启用不必要的 Kubernetes、主目录共享或 Docker Socket 挂载；
- 宿主、Docker/WSL 和内核保持安全更新。

Docker 属于系统级依赖，ForgeAgent 不会自动安装或修改它。每个宿主仍需验证网络、挂载、身份、只读文件系统和资源限制；仅通过模拟 runner 的测试不能证明真实隔离。

## 构建与运行

在项目根目录执行：

```powershell
docker build -f docker/Dockerfile -t forge-agent:dev .
$env:FORGE_WORKSPACE = (Resolve-Path .).Path
docker compose -f docker/compose.yaml run --rm forge-agent --help
```

`docker/compose.yaml` 和 `forge_agent.sandbox` 默认：

- 非 root 用户 `10001:10001`；
- `network_mode: none`、只读根文件系统；
- 删除全部 Linux capabilities，并启用 `no-new-privileges`；
- 限制 CPU、内存、PID、临时目录和宿主超时；
- 只把明确的目标工作区挂到 `/workspace`；
- 不使用 `--privileged`，不挂载 Docker Socket 或整个用户目录。

构建成功后，推荐启用按风险自动路由：

```powershell
$env:FORGE_EXECUTION_BACKEND = "auto"
$env:FORGE_DOCKER_IMAGE = "forge-agent:dev"
.\.venv\Scripts\forge.exe config check --allow-missing-model
.\.venv\Scripts\forge.exe doctor
```

`FORGE_EXECUTION_BACKEND` 支持三种模式：

| 模式 | `SAFE` | `REVIEW` | 适用场景 |
| --- | --- | --- | --- |
| `auto`（默认） | 本地 | 人工确认后强制 Docker | 日常使用，兼顾速度和隔离 |
| `docker` | Docker | 人工确认后 Docker | 希望所有允许命令都隔离 |
| `local` | 本地 | 人工确认后本地 | 明确接受风险的调试兼容模式 |

路由只由本地确定性策略决定，模型不能在工具参数中伪造风险、审批结果或后端。策略会先拒绝 Shell、网络、提权、破坏性操作和 Git 历史修改；这些 `DENY` 命令不会因为存在沙盒而放行。`REVIEW` 命令仍需本地人工确认，沙盒不能替代审批。

`auto` 和 `docker` 都采用失败关闭：Docker 可执行文件、Daemon 或目标镜像不可用时返回结构化的 `sandbox_unavailable`，不会回退到宿主机执行。审批提示和运行轨迹会记录 `risk`、`backend`、`sandbox_required`、`sandboxed` 与 `routing_reason`，便于区分“计划进入沙盒”和“确实已经在沙盒执行”。

文件读取和精确补丁仍由宿主进程在工作区边界内执行。只有 `run_command` 使用上述路由。

基础镜像包含 ForgeAgent 的开发检查工具，但不会自动安装目标仓库的任意依赖。目标需要额外依赖时，应构建经过审查且固定依赖的派生镜像；不要为了方便在运行时开放网络或挂载宿主虚拟环境。

Python 目标仓库的锁定、离线 wheelhouse 和派生镜像流程见 [docker/DEPENDENCIES.md](../docker/DEPENDENCIES.md)。

运行真实目标前，应额外验证容器无法读取宿主 SSH/云凭证、无法联网、资源限制生效，并审查目标工作区是否本身包含秘密。
