# ForgeAgent 模型服务配置指南

本文档说明如何为 ForgeAgent 配置 OpenAI、OpenAI-compatible、Anthropic 和本地 Ollama 模型服务。配置只在真正发起模型请求时需要；离线测试不会读取真实密钥，也不会产生模型费用。

## 1. 必填配置

| 环境变量 | 作用 | 示例 |
| --- | --- | --- |
| `FORGE_MODEL_PROVIDER` | 选择协议：`openai`、`anthropic` 或 `ollama` | `openai` |
| `FORGE_MODEL_BASE_URL` | API 根地址，不包含末尾的具体接口路径 | `https://api.openai.com/v1` |
| `FORGE_MODEL_NAME` | 服务商提供的准确模型 ID | 以服务商控制台显示为准 |
| `FORGE_MODEL_API_KEY` | API 密钥，属于敏感信息 | 不要写入仓库 |
| `FORGE_AGENT_MAX_COST_USD` | 单次 Agent 运行的美元预算 | `1.00` |

Provider 会自动补充具体路径：

- `openai`：向 `{Base URL}/chat/completions` 发送请求；
- `anthropic`：向 `{Base URL}/messages` 发送请求。
- `ollama`：向本地 OpenAI-compatible 的 `{Base URL}/chat/completions` 发送请求，不要求 API Key。

因此 Base URL 应填写 API 根地址，而不是完整接口地址。第三方 OpenAI-compatible 服务也选择 `openai`，并填写该服务商给出的 Base URL、模型名和密钥。

## 2. 在当前 PowerShell 中设置

这种方式最适合开发测试。配置只影响当前终端，关闭终端后失效。

### OpenAI 或 OpenAI-compatible

```powershell
$env:FORGE_MODEL_PROVIDER = "openai"
$env:FORGE_MODEL_BASE_URL = "https://api.openai.com/v1"
$env:FORGE_MODEL_NAME = "请替换为服务商提供的模型ID"
$env:FORGE_MODEL_API_KEY = "请替换为真实API-Key"
$env:FORGE_AGENT_MAX_COST_USD = "1.00"
```

如果使用兼容服务，只替换 Base URL、模型名和 API Key，不要把 Provider 改成服务商名称。

### Anthropic

```powershell
$env:FORGE_MODEL_PROVIDER = "anthropic"
$env:FORGE_MODEL_BASE_URL = "https://api.anthropic.com/v1"
$env:FORGE_MODEL_NAME = "请替换为服务商提供的Claude模型ID"
$env:FORGE_MODEL_API_KEY = "请替换为真实API-Key"
$env:FORGE_AGENT_MAX_COST_USD = "1.00"
```

Anthropic 请求默认使用 API 版本 `2023-06-01`。只有服务商明确要求时才覆盖：

```powershell
$env:FORGE_ANTHROPIC_VERSION = "2023-06-01"
```

### 本地 Ollama

先由你自行安装、启动 Ollama 并下载支持工具调用的模型，然后设置：

```powershell
$env:FORGE_MODEL_PROVIDER = "ollama"
$env:FORGE_MODEL_BASE_URL = "http://localhost:11434/v1"
$env:FORGE_MODEL_NAME = "qwen2.5-coder:7b"
Remove-Item Env:FORGE_MODEL_API_KEY -ErrorAction SilentlyContinue
```

模型名只是示例，应替换为本机已经下载且能力合适的模型。ForgeAgent 不会自动下载大模型。

## 3. 可选的可靠性配置

```powershell
$env:FORGE_MODEL_CONNECT_TIMEOUT_SECONDS = "10"
$env:FORGE_MODEL_READ_TIMEOUT_SECONDS = "60"
$env:FORGE_MODEL_TOTAL_TIMEOUT_SECONDS = "120"
$env:FORGE_MODEL_MAX_OUTPUT_TOKENS = "4096"
$env:FORGE_MODEL_MAX_RETRIES = "2"
$env:FORGE_AGENT_MAX_TOKENS = "100000"
$env:FORGE_AGENT_MAX_REPEATED_FAILURES = "3"
```

- 连接超时：建立网络连接最多等待多久；
- 读取超时：等待服务端返回数据最多多久；
- 总超时：一次尝试从开始到结束的总时间；
- 最大输出 Token：传给模型接口的单次输出限制；
- 最大重试次数：遇到网络错误、HTTP 429、529 或 5xx 时的额外尝试次数。
- 最大 Token：一次 Agent 运行累计允许的 Token；不设置时不启用此限制。
- 最大重复失败次数：连续发生多少次相同工具失败后停止 Agent Loop。

HTTP 400、401、403 等通常无法靠重试恢复，Provider 会立即失败。取消任务时也不会在后台继续重试。

### 可选费用快照

ForgeAgent 不内置可能过期的模型价格。可把服务商当前公布的“每百万 Token 美元价格”显式写入当前终端：

```powershell
$env:FORGE_MODEL_INPUT_COST_PER_MILLION = "请填写输入价格"
$env:FORGE_MODEL_OUTPUT_COST_PER_MILLION = "请填写输出价格"
$env:FORGE_MODEL_CACHED_INPUT_COST_PER_MILLION = "请填写缓存输入价格"
```

输入与输出价格必须同时设置；缓存价格可省略，省略时按普通输入价格计算。OpenAI-compatible 的缓存 Token 被视为输入 Token 子集，Anthropic 的 cache-read 用量按额外缓存 Token 计算。价格属于用户提供的快照，报告会据此累计 `known_cost`；价格变化后必须手动更新。

## 4. 安全检查配置

下面的正式 CLI 命令只校验并显示脱敏后的配置，不会联网：

```powershell
.\.venv\Scripts\forge.exe config check
```

尚未设置模型时，可以只验证工作区和预算等基础配置：

```powershell
.\.venv\Scripts\forge.exe config check --allow-missing-model
```

也可以只确认密钥是否存在，不显示密钥内容：

```powershell
if ($env:FORGE_MODEL_API_KEY) {
    Write-Output "API key is configured"
} else {
    Write-Output "API key is missing"
}
```

不要运行会直接输出 `$env:FORGE_MODEL_API_KEY` 的命令，也不要把真实密钥写入源码、测试、`forge.toml`、截图或提交记录。

## 5. 使用 `forge.toml` 保存非敏感配置

Base URL、模型名和预算可以写入项目根目录的 `forge.toml`：

```toml
[model]
provider = "openai"
base_url = "https://api.openai.com/v1"
name = "请替换为服务商提供的模型ID"
connect_timeout_seconds = 10
read_timeout_seconds = 60
total_timeout_seconds = 120
max_output_tokens = 4096
max_retries = 2

[agent]
max_cost_usd = 1.00

[commands]
approval = "risky"

[patches]
approval = "always"

[execution]
backend = "auto"
docker_image = "forge-agent:dev"
```

API Key 仍通过当前 PowerShell 提供：

```powershell
$env:FORGE_MODEL_API_KEY = "请替换为真实API-Key"
```

配置优先级为：内置默认值 < `forge.toml` < 环境变量 < CLI 参数。仓库提供的 `.env.example` 只是字段示例；ForgeAgent 不会自动读取 `.env`，避免误读目标仓库中的秘密。

`execution.backend = "auto"` 是默认值：`SAFE` 命令本地运行，`REVIEW` 命令经人工确认后进入 Docker，`DENY` 命令始终拒绝。Docker 不可用时不会回退本地。完整说明见 [docs/DOCKER_SETUP.md](./docs/DOCKER_SETUP.md)。

## 6. 费用上限的当前边界

M05 Runtime 已能读取并执行 `FORGE_AGENT_MAX_COST_USD` 预算。费用是正数美元金额，例如 `0.50` 表示 0.50 美元。

Runtime 会累计 `Usage.cost` 中已知的美元费用，并在达到上限时返回 `max_cost`。配置上述价格后，OpenAI-compatible 与 Anthropic 适配器会从服务端 Token 用量计算美元费用；未配置价格或响应缺少用量时，`cost_complete` 会是 `false`，程序不会假装费用已被准确计算。

即使费用已知，本地预算也只能在一条响应返回后判断，无法在服务端计费前精确截断单次在途请求。真实测试仍应同时在服务商控制台设置账户或项目级硬限额，并可以使用 `FORGE_AGENT_MAX_TOKENS` 限制 Token。

## 7. 当前可验证范围

自动化测试使用本地模拟 HTTP 响应，覆盖 OpenAI 与 Anthropic 的文本、工具调用、Token 用量、错误、超时、重试和取消，不使用真实 API Key，也不产生费用。

真实 API 烟雾测试需要你先选择服务并在当前 PowerShell 设置上述变量。配置检查通过后，可以运行：

```powershell
.\.venv\Scripts\forge.exe run "解释这个示例项目的入口和数据流" --workspace .\examples\read_only_repo
```

这会产生真实 API 请求和费用。建议先把 Token、费用及服务商控制台硬限额设置得较低。

## 8. 可信 RLM 使用同一套模型配置

RLM 使用与普通 `forge run` 完全相同的 Provider、Base URL、模型名、API Key、Token 与费用配置。先在 Provider 控制台设置独立项目的费用硬限额，再执行：

```powershell
.\.venv\Scripts\forge.exe rlm run "任务" --workspace . --trust-workspace
```

`--trust-workspace` 只确认本地 IPython Kernel 的权限风险，不会放宽或替代 Provider 费用限制。父 Agent 和子 Agent 的 Usage 会写入根会话共享账本；服务不返回 Usage 或未配置显式价格时，账本保留 `cost_complete=false` 语义。
