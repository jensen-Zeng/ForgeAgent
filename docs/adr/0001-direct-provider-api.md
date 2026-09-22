# ADR-0001：直接实现 Provider 与 Agent Loop

- 状态：Accepted
- 日期：2026-07-26

## 背景

项目需要让消息规范化、工具循环、预算和错误恢复保持可观察，而不是把核心控制流隐藏在框架中。

## 决策

直接基于 HTTPX 实现 OpenAI-compatible、Anthropic 与 Ollama 适配器，Runtime 只依赖统一领域模型。

## 影响

代码更透明、离线测试更确定，但需要自行维护协议差异。出现持久化分支工作流需求时重新评估 Agent 框架。
