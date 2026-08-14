# HarnessLab 中文说明

**把 Agent 的路由、执行、恢复、安全和回归验证做成可执行的工程契约。**

[在线演示](https://linxiushen.github.io/harnesslab/) ·
[Agentic 控制面](AGENTIC_CONTROL_PLANE.md) ·
[架构说明](ARCHITECTURE.md) ·
[社区需求证据](COMMUNITY_REQUIREMENTS.md) ·
[运维手册](OPERATIONS.md) ·
[架构评审材料与 PPT](ARCHITECTURE_REVIEW.zh-CN.md) ·
[平台扩展与 100k 规模路线](PLATFORM_SCALE.md) ·
[DeepSeek 面试题解](DEEPSEEK_INTERVIEW_PLAYBOOK.zh-CN.md) ·
[Trace Contract 规范](TRACE_CONTRACT.md) ·
[GitHub](https://github.com/Linxiushen/harnesslab)

HarnessLab 不是另一个只看最终答案的评测工具。v0.4 提供完整 Agentic 控制面：可解释任务路由、
DAG 调度、能力令牌、上下文/长期记忆、Token/成本预算、重试/重规划/补偿、Prompt Injection 隔离，
并把每个决策写成可持久化、可回放事件。

![HarnessLab Trace Contract 回归差分](regression.png)

> DeepSeek Harness 当前尚未公开协议。HarnessLab 不声称已经接入内部或预览接口；项目把
> capability probe 和 provider adapter 隔离在明确边界内，等真实规范出现后基于 fixture
> 实现，不猜测私有 wire format。

## 30 秒运行

默认 adapter 完全离线、结果确定，不需要模型 API Key。
工作台还会生成一个明确标注的 regression fixture，故意跳过工具步骤，以便差分页立即展示
真实的 breaking tool-path 变化。

```powershell
git clone https://github.com/Linxiushen/harnesslab.git
cd harnesslab
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\harnesslab snapshot "Review the checkout authorization change" -o baseline.trace.json
.venv\Scripts\harnesslab verify baseline.trace.json
.venv\Scripts\harnesslab agent-eval
.venv\Scripts\harnesslab serve
```

打开 `http://127.0.0.1:4318`。`verify` 在生命周期、工具路径、审批路径、终态或协议约束发生
回归时返回非零退出码，可直接作为 CI gate。

## Agentic 控制面

- 简单任务单工具直达，复杂任务进入可验证的 DAG。
- Ready Set 有界并发，支持超时、取消和输出 Schema 校验。
- 瞬时且幂等的错误重试；主路径永久失败可切换安全备用计划。
- 下游失败按完成顺序反向补偿已经发生的副作用。
- HMAC capability token 绑定用户、Run、权限和过期时间。
- 外部工具结果检测 Prompt Injection，污染结果不能驱动写操作。
- Message/Token 双窗口、输出预留、TTL、去重、乐观锁和容量上限。
- 模型先过上下文、质量、工具成功率、安全、延迟和成本硬门槛，再评分选型。
- Run 与有序事件写入 SQLite；进程重启后保留报告，并显式终止中断任务。
- `Idempotency-Key` 原子去重；同键异参冲突，避免副作用任务重复提交。
- Agent Run 支持后台执行、SSE、执行中取消，以及带双指纹的防篡改 artifact。
- 9 项离线发布门禁覆盖恢复、安全、预算、并发、记忆、流控和模型选择。

```powershell
.venv\Scripts\harnesslab agent-run --scenario enterprise-recovery
.venv\Scripts\harnesslab agent-run --scenario prompt-injection
.venv\Scripts\harnesslab agent-run --scenario enterprise-recovery --artifact run.agent.json
.venv\Scripts\harnesslab agent-replay run.agent.json
.venv\Scripts\harnesslab agent-eval
```

## Trace Contract v1

每个 `harnesslab.trace/v1` artifact 包含：

- 严格递增的生命周期事件流。
- model、tool、approval、denial 和 terminal 路径。
- 忽略时间戳、耗时、token 数和自由文本的协议指纹。
- 基于稳定且已脱敏 payload 的内容指纹。
- 缺少审批、重复终态等协议违规项。
- 可供离线对比的脱敏事件证据。

常见 credential key 和内联 bearer token 会在事件进入 SSE、UI 和 artifact 之前被脱敏；
原始模型上下文只在运行时内部使用，不通过 API 序列化。

完整规则见 [TRACE_CONTRACT.md](TRACE_CONTRACT.md)。

## 主要能力

- 多轮模型/工具循环和强制最大回合数。
- SSE 实时事件流与工具 call ID 关联。
- 只读工具自动审批；有副作用的工具默认拒绝并产生 `tool.denied` 事件。
- `snapshot` 记录可移植基线，`verify` 在 CI 中重放校验。
- 两次运行的协议指纹、结构差分和 payload 变化提示。
- 十项 provider-independent conformance 检查。
- OpenAI-compatible、公开 DeepSeek API adapter 和可选 MCP stdio bridge。
- 面向未来 DeepSeek Harness 的独立 capability probe 与 adapter 边界。

## CLI

| 命令 | 用途 |
| --- | --- |
| `harnesslab snapshot "..." -o trace.json` | 固化一次审核通过的运行基线 |
| `harnesslab verify trace.json` | 重新执行并检测结构回归 |
| `harnesslab compare before.json after.json` | 离线对比两个 artifact |
| `harnesslab check` | 运行 conformance matrix |
| `harnesslab serve` | 启动 API 与本地工作台 |
| `harnesslab agent-run --scenario NAME` | 执行确定性的生产故障场景 |
| `harnesslab agent-run --artifact run.agent.json` | 执行并导出 Agent Run artifact |
| `harnesslab agent-replay run.agent.json` | 离线校验指纹、事件与执行不变量 |
| `harnesslab agent-eval` | 运行 9 项 Agentic 发布门禁 |

确定性 fixture 还可以加 `--strict-content`，让稳定 payload 的任何变化也导致 CI 失败。

## 接入 DeepSeek Harness 的第一天

1. 保存官方协议、能力文档、生命周期和错误样例作为不可变 fixture。
2. 在 `src/harnesslab/adapters/` 实现新的 `HarnessAdapter`。
3. 把原生生命周期事件映射到稳定的 Trace Contract 词汇。
4. 运行通用 conformance 与已记录的回归基线。
5. 增加 DSH 专属检查，但不削弱通用约束。
6. 只发布 fixture 能证明的兼容范围。

这样“支持 DSH”会成为可复现的工程结论，而不是抢发布时间的口号。
