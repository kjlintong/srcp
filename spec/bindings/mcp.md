# MCP 绑定（SRCP over Model Context Protocol）

> MCP 负责"把工具暴露给 Agent"，SRCP 负责"这些工具的并发语义"。本绑定是**映射**，不修改 MCP 规范。

## 1. 工具清单

| MCP tool | SRCP 操作 | 风险提示 |
|---|---|---|
| `srcp_capabilities` | `capabilities()` | `readOnlyHint=true`, `idempotentHint=true` |
| `srcp_declare_resource` | `declare_resource()` | `destructiveHint=false`, `idempotentHint=true` |
| `srcp_acquire_lease` | `acquire_lease()` | `destructiveHint=false`, `idempotentHint=false` |
| `srcp_heartbeat` | `heartbeat()` | `destructiveHint=false`, `idempotentHint=true` |
| `srcp_release_lease` | `release_lease()` | `destructiveHint=false`, `idempotentHint=true` |
| `srcp_read` | `read()` | `readOnlyHint=true`, `idempotentHint=true` |
| `srcp_write` | `write()` | `destructiveHint=true`, `idempotentHint=false` |
| `srcp_conflicts` | `list_conflicts()` | `readOnlyHint=true` |
| `srcp_audit` | `audit()` + `verify_audit()` | `readOnlyHint=true` |
| `srcp_adjudicate` | `adjudicate()` | `destructiveHint=true`, 仅 owner |

## 2. 请求映射

每个工具的参数就是对应 SRCP 请求对象的 JSON 字段。Schema 位于 `spec/schema/`。示例：

```json
{
  "name": "srcp_write",
  "arguments": {
    "resource_id": "shared/plan.md",
    "principal": "agent:claude-code@sess-7",
    "lease_id": "lease-01J...",
    "expected_generation": 7,
    "content": "# Plan\n...",
    "content_type": "text/markdown",
    "intent": "update roadmap section",
    "idempotency_key": "sess-7-plan-42",
    "conflict_policy": "abort"
  }
}
```

返回值为 SRCP `WriteResult` 对象，直接作为 tool result 的 `structuredContent`。

## 3. 错误映射

SRCP 错误码通过 MCP tool error 返回，**不得**被降级成空结果或静默 LWW：

| SRCP error | MCP 行为 |
|---|---|
| `CONFLICT` / `STALE_WRITE` | `isError=true`，text 中含 conflict class 与当前 generation |
| `FENCE_REJECTED` / `LEASE_EXPIRED` | `isError=true`，提示重新 `acquire_lease` 并重读 |
| `REQUIRES_HUMAN_REVIEW` | `isError=true`，附 `conflict_id`，引导 `srcp_conflicts` |
| `UNSUPPORTED_CONSISTENCY` | `isError=true`，先调用 `srcp_capabilities` |

## 4. 资源声明的承载

MCP Resources / Roots 可暴露 `srcp://resources` 列表，用于**发现**；发现范围受租户 ACL 限制。发现结果只含 `resource_id`、`content_type`、`granularity`，不含内容。

## 5. 实现说明

本仓库 `src/srcp/mcp_server.py` 提供零外部依赖的 stdio JSON-RPC 2.0 实现（`initialize` / `tools/list` / `tools/call`）。它可被任何支持 MCP stdio transport 的 Agent 直接挂载；生产环境可替换为官方 MCP SDK 包装同一 `Coordinator`。
