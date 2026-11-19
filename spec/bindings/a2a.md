# A2A 绑定（SRCP over Agent2Agent）

> A2A 负责"Agent 之间发现、任务生命周期与 artifact 交换"。它明确不要求 Agent 访问彼此内部状态；SRCP 则是双方**自愿共享的、可验证的外部状态契约**。

## 1. artifact 中的 SRCP 元数据

A2A artifact 可携带如下扩展元数据（放在 artifact metadata 中，不改变 Task 状态机）：

```json
{
  "srcp": {
    "protocol": "srcp/0.1",
    "resource_ref": "shared/plan.md",
    "generation": 7,
    "version_vector": {"agent:planner@a": 4},
    "content_hash": "sha256:...",
    "lease_token": 12,
    "merge_policy": "abort",
    "trace_id": "tr-01J..."
  }
}
```

### 字段语义

- `resource_ref`：SRCP resource_id，不是 artifact id。
- `generation` / `version_vector`：产出该 artifact 时的读视图版本，接收方据此决定是否需要条件写。
- `lease_token`：**只作为提示**，不能作为授权凭证；授权必须由接收方调用 SRCP 协调器重新验证。
- `merge_policy`：建议策略，最终策略由资源声明决定。
- `trace_id`：贯穿 A2A task update 与 SRCP 审计，便于跨协议追踪。

## 2. task update 中的协调结果

Task 状态仍使用 A2A 原生状态（`working` / `completed` / `failed` / `input-required`）。SRCP 结果放在 `task.metadata.srcp_result`：

```json
{
  "srcp_result": {
    "status": "CONFLICT",
    "conflict_class": "stale-write",
    "current_generation": 8,
    "conflict_id": "cf-01J...",
    "action_required": "re-read and retry"
  }
}
```

> 不要把 SRCP 的 `CONFLICT` 映射成 A2A `failed`。并发冲突是预期状态，通常应转为 `input-required` 或自动重试。

## 3. 身份衔接

A2A Agent Card 可声明 SRCP 能力：

```json
{
  "srcp": {
    "supported": true,
    "level": "L2",
    "policies": ["abort", "merge", "manual"],
    "principal": "agent:planner@team-a"
  }
}
```

接收方不得仅凭 Agent Card 中的 principal 文本授权，仍需通过 SRCP 的 ACL 与凭证钩子校验。

## 4. 与 AGNTCY OASF / ACP 的关系

- OASF 可用于描述"该 Agent 支持哪些 SRCP 能力与策略"，属于能力声明层。
- ACP v2 的 `git_patch` 可承载本地文件变更的审核视图；若变更目标受 SRCP 保护，审核通过前不得签发写租约。
- 三者都不改变 SRCP 的 CAS / fencing / 审计语义。
