# Provider compatibility: offline evidence

原始记录日期：2026-08-20

本次审阅：2026-10-15

代码版本标记：`0.3.0.dev0`

下表只总结当前 Provider 适配代码和离线自动化测试（主要是模拟 HTTP），不代表对真实服务的兼容承诺。具体兼容性还取决于服务端对相应协议的实现。

| 能力 | OpenAI-compatible | Anthropic | Ollama OpenAI-compatible |
| --- | --- | --- | --- |
| 文本与工具响应规范化 | 模拟 HTTP 回归覆盖文本及工具调用规范化 | 模拟 HTTP 回归覆盖文本及工具块规范化 | 继承 OpenAI-compatible 实现；有模拟响应规范化测试 |
| 多工具调用与参数校验 | 模拟回归覆盖多调用及非法参数拒绝 | 消息/工具块结构有模拟回归覆盖 | 复用 OpenAI-compatible 协议实现；未独立验证真实 Ollama 服务 |
| Usage 与费用语义 | 返回 Usage 时规范化并可按显式价格估算；缺失 Usage 时费用未知 | 返回 Usage 时规范化并可按显式价格估算；缺失 Usage 时费用未知 | 沿用 OpenAI-compatible 解析；真实服务 Usage 行为未验证 |
| 重试 | 共享 HTTP 基类策略；模拟测试覆盖 429，5xx 按策略重试 | 共享 HTTP 基类策略；模拟测试覆盖 529，5xx 按策略重试 | 沿用 OpenAI-compatible 实现；没有真实服务测试 |
| 超时与取消 | 模拟回归覆盖超时重试和取消不重试 | 使用共享 HTTP 基类；未单独完成真实服务验证 | 使用继承的 HTTP 实现；未单独完成真实服务验证 |
| 流式接口 | SSE 文本与分片工具参数聚合；要求收到 `[DONE]` 才算完整 | `stream()` 目前先等待完整响应，只发出完成事件；原生 Anthropic SSE 尚未实现 | 继承 OpenAI-compatible SSE 实现；未验证真实 Ollama 流式行为 |
| 真实服务烟雾测试 | 未运行 | 未运行 | 未运行 |
| 真实模型质量/费用评测 | 未运行 | 未运行 | 未运行 |

Usage 缺失时，领域模型会以零值表示未知/未提供的 Token 计数，响应 `cost` 为 `None`；这不表示请求实际花费为零。若用量或价格不完整，运行指标应保留 `cost_complete=false`。本地费用预算不能替代 Provider 账户或项目的硬限额。

相关离线证据见 `tests/test_model_providers.py`、流式聚合测试 `tests/test_rlm_stage1.py` 和 `src/forge_agent/models/`。RLM 离线集成测试使用 Faux Provider 和本地 Kernel，验证的是 RLM Runtime/Kernel/Host Bridge 流程，不应解释为表中任一真实 Provider 的端到端通过。目前公开证据尚不支持真实服务的兼容性、成功率、延迟或成本结论。Reference harness 报告的边界见 [`reports/README.md`](README.md)。
