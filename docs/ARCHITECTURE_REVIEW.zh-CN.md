# HarnessLab 架构评审材料

这套材料面向 Agent runtime、MCP、评测基础设施和平台架构评审，核心问题是：如何把 Pi 风格的
runtime loop 变成 provider-neutral、可恢复、可审计的生产控制平面。

## 交付物

- [架构评审 PPTX](architecture-review.pptx)：15 页，包含演讲备注和每页来源。
- [Architecture](ARCHITECTURE.md)：运行时边界、上下文和工具暴露策略。
- [Agentic Control Plane](AGENTIC_CONTROL_PLANE.md)：契约、故障语义和离线场景。
- [Community Requirements](COMMUNITY_REQUIREMENTS.md)：来自 Pi GitHub issue 的公开开发者信号及映射。
- [Operations](OPERATIONS.md)：生产部署、关闭、恢复、观测和事故流程。

## 叙事结构

1. 生产问题：上下文、MCP、状态、安全和证据边界。
2. 设计：Service、Control Plane、Policy、Context、Tool Gateway 和 Adapter 分层。
3. 运行：路由、DAG、策略门、工具执行、恢复和 artifact replay。
4. 证据：Trace Contract、fingerprint、社区需求矩阵、质量门禁。
5. 决策：DeepSeek 的公开 API 边界，以及真实能力驱动的规模路线。

## 规模声明

截至 2026-08-14，项目代码范围约 8.3k 行（`src`、`tests`、`examples`），不含虚拟环境和缓存。
用户提出的 200k 行目标被定义为多包平台的长期容量目标，而不是通过复制或生成无意义代码达到的数字。
扩展方向包括 MCP Gateway、Pi-compatible Runtime、持久化与向量存储适配器、provider/eval 矩阵以及
多租户运维能力。每个新增 package 都必须有明确边界、typed contract、故障测试和运行指标。

## 研究边界

社区需求来自公开 issue 的代表性样本，不等同于统计调查。DeepSeek 目前只声明支持公开 API；未发布的
原生 Harness wire protocol 需要官方 capability document 和稳定 fixture 后再进入兼容性声明。
