# SRCP — Shared Resource Coordination Protocol（v0.1）

> 面向异构 Agent 的**共享资源控制面协议**。它不发明新的存储系统，也不替代 MCP / A2A；它只规定：当多个互不信任的 Agent 操作同一资源时，**读到的版本算不算数、写权限何时有效、冲突如何被分类和裁决、整个过程如何被审计**。

## 0. 定位与非目标

### 0.1 一句话定义

SRCP 是一个让"**声明 → 租约 → 读视图 → 条件写 → 冲突裁决 → 审计**"在异构 Agent 之间可验证、可降级、可替换后端的薄协议。

### 0.2 非目标（明确不做）

| 不做 | 原因 |
|---|---|
| 新的数据库 / 向量库 / 记忆库 | 复用 SQLite、Postgres、Redis、Git、对象存储与本地文件 |
| 新的 Agent 通信底座 | 用 MCP tool / A2A artifact 承载即可 |
| 万能合并器 | 语义事实不能靠算法静默裁决；协议只保证冲突**可见且可审计** |
| 密码学身份体系 v1 | 定义 principal 语法与凭证扩展点，参考实现中先做 ACL 校验 |
| 强一致性的全局时钟 | 用 generation + vector clock + fencing token，不假设物理时钟同步 |

### 0.3 合规等级

协议允许实现方按能力声明等级，客户端必须据此决定降级行为。

| 等级 | 名称 | 含义 |
|---|---|---|
| L0 | advisory | 调用方自愿遵守；不签发 fencing token，不保证强制拒绝 |
| L1 | enforced | 存储侧校验 generation 与 fencing 高水位；过期写者被硬拒绝 |
| L2 | audited | L1 + 防篡改 hash-chain 审计 + 冲突升级与人工裁决留痕 |

本仓库的参考实现默认 **L2**（SQLite / Git 后端跨进程持久化，Memory 后端用于测试）。

---

## 1. 术语与一致性模型

- **资源（Resource）**：可被独立寻址与版本化的共享状态单元。URI 形式，如 `shared/plan.md`、`kv://prefs/theme`、`graph://claim/c-42`。
- **代次（generation）**：资源内容提交的单调递增整数。每次成功提交 +1。
- **版本向量（version vector）**：`{principal: counter}` 映射，表达因果历史；用于判断并发与 merge 的字段来源。
- **租约（lease）**：一个带 TTL 的写授权，附带**单调递增 fencing token**。
- **fencing 高水位（high-water mark）**：资源侧记录已接受的最大 token；任何 `token <= high_water` 的写必须被拒绝，无论调用方自称是否仍持有锁。
- **读视图（read view）**：某一时刻资源的一致性快照，包含 generation、版本向量、内容哈希与可见性风险提示。
- **冲突（conflict）**：一等对象，而不是异常字符串；必须可查询、可升级、可裁决。
- **principal**：调用方身份标识，语法 `scheme:id@scope`，例如 `agent:claude-code@sess-7`。

### 1.1 承诺的一致性

| 操作 | 承诺 |
|---|---|
| 单资源条件写 | 线性化（CAS）：只有 `expected_generation == current_generation` 才提交 |
| 读 | 读取某一已提交代次的完整快照，不返回半写状态 |
| 租约 | 到期即失效；旧 token 永远无法越过 fencing 高水位 |
| 审计 | append-only；事件通过 SHA-256 hash chain 串联，篡改可被检测 |
| 跨资源事务 | v0.1 仅提供 prepare/commit/abort 的接口骨架，不承诺分布式原子性 |

> 协议**不承诺** read-your-writes 跨 Agent 可见、不承诺全局顺序、不承诺语义正确性。

---

## 2. 资源声明（Declare）

`declare_resource` 必须在任何读写之前调用。声明是幂等的（同一 `resource_id` 重复声明返回已有记录），但**不允许静默改变已有资源的安全属性**。

```json
{
  "resource_id": "shared/plan.md",
  "tenant": "default",
  "content_type": "text/markdown",
  "granularity": "file",
  "isolation": "causal",
  "owners": ["agent:lead@team"],
  "writers": ["agent:*"],
  "readers": ["agent:*"],
  "conflict_policy": "abort",
  "retention": "snapshot"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `resource_id` | URI 字符串 | 不含 `..`、不以 `/` 开头；后端据此做路径安全校验 |
| `granularity` | `file` \| `kv` \| `row-range` \| `graph-claim` | 隔离粒度，决定 merge 语义 |
| `isolation` | `serializable` \| `causal` \| `eventual` | 与后端能力协商；不支持时返回 `UNSUPPORTED_CONSISTENCY` |
| `conflict_policy` | `abort` \| `lww` \| `append` \| `merge` \| `manual` | 默认 `abort`；见 §6 |
| `retention` | `append-only` \| `snapshot` \| `compact` | 历史保留策略 |

声明时 `generation = 0`，`version_vector = {}`。

---

## 3. 身份与授权

- principal 分为 `owner`、`writer`、`reader` 三类 ACL，支持 `*` 通配后缀。
- `owner` 可以改写 `conflict_policy` 与裁决 `manual` 冲突；普通 `writer` 不可以。
- **Agent 自报名称不构成授权依据**：L1/L2 实现必须把 principal 映射到已配置的凭证或受信任的运行时身份；参考实现提供 `principal` 参数作为可替换的认证钩子。
- 每次请求必须携带 principal；缺省或格式错误 → `INVALID_PRINCIPAL`。

---

## 4. 读视图与版本

`read(resource_id, principal)` 返回：

```json
{
  "resource_id": "shared/plan.md",
  "generation": 7,
  "version_vector": {"agent:alice@a": 5, "agent:bob@b": 2},
  "content_hash": "sha256:9f2c...",
  "content": "当前内容",
  "content_type": "text/markdown",
  "read_at": "2026-11-19T08:00:00Z",
  "stale_risk": "none",
  "active_lease": null
}
```

- `content_hash` 为 `sha256:` 前缀的 UTF-8 内容摘要，供调用方验证完整性。
- `stale_risk` 取 `none` / `concurrent-writes-may-exist`；后者表示读后有其他 Agent 可能已提交，调用方应以 `expected_generation` 做条件写而非盲写。
- 读操作本身不改变 generation，也不自动获取租约（**读与写授权分离**）。

---

## 5. 租约与 Fencing（核心保证）

### 5.1 生命周期

```
acquire_lease(resource_id, principal, ttl_ms)
    → {lease_id, token, expires_at, principal, resource_id}
heartbeat(lease_id, principal) → 更新 expires_at（token 不变）
write(..., lease_id, ...)      → 服务端校验
release_lease(lease_id, principal)
```

### 5.2 服务端必须执行的四条校验

1. **存在性**：lease 存在且属于该 `resource_id` 与 `principal`。
2. **未过期**：`now < expires_at`；否则 `LEASE_EXPIRED`。
3. **fencing**：`token > high_water[resource_id]`；否则 `FENCE_REJECTED`。
4. **高水位推进**：提交成功后 `high_water[resource_id] = token`。

> 关键：**高水位只在成功提交时推进**。一个拿到新 token 但尚未提交的写者，不应让旧 token 失效——否则会放大可用性问题；真正的安全保证来自"旧 token 永远不能写入"。

### 5.3 为什么不能只靠锁

进程暂停（stop-the-world GC、虚拟机挂起、网络分区）后，旧持有者可能带着已过期的授权苏醒。若资源侧不检查单调递增 token，锁服务认为锁已释放，旧写者仍可覆盖新内容。SRCP 把**租约颁发、心跳、资源侧验证、序列号生成、过期策略**拆开，避免单点假设。

---

## 6. 写意图、幂等与冲突

### 6.1 写请求

```json
{
  "resource_id": "shared/plan.md",
  "principal": "agent:alice@a",
  "lease_id": "lease-01J...",
  "expected_generation": 7,
  "content": "新的计划内容",
  "content_type": "text/markdown",
  "intent": "update-section:roadmap",
  "idempotency_key": "client-generated-unique-key",
  "conflict_policy": "abort",
  "deadline_ms": 5000
}
```

- `expected_generation` 是 CAS 条件；`expected_version_vector` 可选，用于更强的因果前置条件。
- `idempotency_key` 必须短时间内唯一；重复请求返回**首次结果**且 generation 不重复增长。
- `deadline_ms` 超时后服务端返回 `DEADLINE_EXCEEDED`，不得半提交。
- `intent` 是人类可读的写意图，进入审计与冲突证据，不做机器裁决依据。

### 6.2 冲突分类（一等对象）

| class | 触发条件 | 是否可自动消解 |
|---|---|---|
| `no-conflict` | 条件写满足 | 否（无需消解） |
| `stale-write` | `expected_generation < current_generation` | 依策略 |
| `mergeable` | 版本向量可比较、字段不重叠 | 是（merge 策略） |
| `semantic-conflict` | 同一字段/claim 被两方独立改写 | **否**，必须升级 |
| `authority-conflict` | 越权写或策略不允许 | 否 |
| `schema-conflict` | content_type / granularity 不匹配 | 否 |

### 6.3 消解策略

| 策略 | 行为 | 信息损失 | 适用 |
|---|---|---|---|
| `abort`（默认） | 返回 `CONFLICT`，调用方重读后重试 | 无 | 代码、计划、关键状态 |
| `append` | 追加写入，不覆盖；generation +1 | 无 | 日志、事件、事实并存 |
| `merge` | 按字段合并版本向量；字段级冲突进入 `semantic-conflict` | 低 | JSON 对象、偏好集合 |
| `lww` | 按服务端逻辑时钟接受，但**必须**写入冲突记录与审计 | 高 | 明确接受丢失的低价值状态 |
| `manual` | 写入 pending 冲突队列，内容不变，等待 owner 裁决 | 无 | 高价值事实、跨组织决策 |

> 策略本身受资源 ACL 约束：`lww` / `merge` 不能绕过 `retention: append-only`；`manual` 只有 owner 可裁决。

### 6.4 写结果

```json
{
  "status": "COMMITTED | CONFLICT | QUEUED | REJECTED",
  "generation": 8,
  "version_vector": {"agent:alice@a": 6},
  "content_hash": "sha256:...",
  "conflict": {"class": "stale-write", "current_generation": 8, "detail": "..."},
  "error": {"code": "FENCE_REJECTED", "message": "..."},
  "trace_id": "..."
}
```

`status != COMMITTED` 时 `generation` 保持当前值不变。

---

## 7. 审计

每个声明、租约、读、写、冲突、裁决事件生成一条 append-only 记录：

```json
{
  "seq": 42,
  "trace_id": "tr-01J...",
  "ts": "2026-11-19T08:00:00Z",
  "actor": "agent:alice@a",
  "action": "write",
  "resource_id": "shared/plan.md",
  "outcome": "ok",
  "evidence": {"generation": 8, "conflict_class": "no-conflict"},
  "prev_hash": "sha256:...",
  "hash": "sha256:..."
}
```

- `hash = SHA256(seq || trace_id || ts || actor || action || resource_id || outcome || canonical_json(evidence) || prev_hash)`
- 首条 `prev_hash = "sha256:000...0"`。
- `verify_audit()` 从头重算，任一被修改/删除/插入都会失败。
- 参数级错误（非法 `resource_id` / `principal`）与尚未形成操作的拒绝（资源不存在、无读权限）不进入审计；**任何可能改变状态的请求（包括被拒绝的写）必须留下审计记录**，错误码写入 `evidence.error`。
- 审计保留期由资源 `retention` 与租户策略决定；参考实现不自动清理。

---

## 8. 能力协商与降级

客户端声明：支持的一致性级别、冲突策略集合、重试预算、离线模式。服务端返回：

```json
{
  "protocol": "srcp/0.1",
  "level": "L2",
  "isolation": ["serializable", "causal"],
  "policies": ["abort", "append", "merge", "lww", "manual"],
  "backends": ["sqlite", "git", "memory"],
  "max_lease_ttl_ms": 300000
}
```

不支持时**必须显式报错**，不允许静默降级为 LWW：

- `UNSUPPORTED_CONSISTENCY`
- `UNSUPPORTED_POLICY`
- `STALE_LEASE`
- `CONFLICT`
- `REQUIRES_HUMAN_REVIEW`

---

## 9. MCP / A2A 绑定

见 `spec/bindings/mcp.md` 与 `spec/bindings/a2a.md`。原则：**映射而非重新发明**。MCP 暴露 tools，A2A artifact 携带 resource-ref / version / lease token / merge-policy，不改变协议语义。

---

## 10. 安全考虑

1. `resource_id` 必须做路径穿越与非法字符校验，尤其 Git/文件后端。
2. 资源发现本身可能泄露路径：只读授权与枚举权限分离。
3. 审计与冲突内容可能含凭证或 prompt injection：存储时 redaction，展示时不可直接作为指令执行。
4. 不信任 Agent 自报优先级；owner / 组织策略优先。
5. 强制模式（L1/L2）下，未经协调层签发的 generation 由存储拒绝；遗留系统可使用 L0 advisory 接入。

---

## 11. 版本与演进

- 语义化版本；新增策略与字段必须向后兼容。
- 每个特性要求：规范文本 + JSON Schema + 正向/负向一致性测试 + 至少一个独立后端实现。
- 弃用特性至少保留两个小版本，并在 `capabilities()` 中标注 `deprecated`。
