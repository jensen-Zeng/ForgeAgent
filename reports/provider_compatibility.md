# Provider compatibility matrix

日期：2026-08-20
版本：`0.3.0.dev0`

| 能力 | OpenAI-compatible | Anthropic | Ollama OpenAI-compatible |
| --- | --- | --- | --- |
| 文本响应规范化 | 自动化通过 | 自动化通过 | 继承并通过 |
| 工具 Schema 发送 | 自动化通过 | 自动化通过 | 自动化通过 |
| 多工具调用 | 自动化通过 | 自动化通过 | 协议继承覆盖 |
| 非法工具参数拒绝 | 自动化通过 | 结构模型覆盖 | 协议继承覆盖 |
| Token 用量 | 自动化通过 | 自动化通过 | 缺失时安全归零 |
| 429/5xx 重试 | 自动化通过 | 529 重试通过 | 协议继承覆盖 |
| 超时与取消 | 自动化通过 | 公共基类覆盖 | 公共基类覆盖 |
| 统一流事件 | SSE 文本与工具参数分片自动化通过 | 单次结果兼容事件 | 继承 OpenAI-compatible SSE |
| 流中断/半包 | 无 `[DONE]` 时结构化拒绝 | 待真实原生流实现 | 继承覆盖 |
| RLM 离线闭环 | Faux Provider + 真实 Kernel 通过 | Faux Provider + 真实 Kernel 通过 | Faux Provider + 真实 Kernel 通过 |
| 真实服务烟雾测试 | 未运行 | 未运行 | 未运行 |
| 20 任务×3 live 评测 | 未运行 | 未运行 | 未运行 |

结论只适用于模拟 HTTP 回归和公共接口契约。未提供 API Key、未运行 Ollama 服务，也未授权产生费用，因此没有比较真实成功率、延迟或成本。`reports/evaluation-reference` 的 60/60 只验证评测器与参考实现，不能解释为模型得分。
