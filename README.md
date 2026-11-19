# SRCP — Shared Resource Coordination Protocol

> 让异构 Agent 对同一资源的并发操作产生**一致、可验证、可审计**的结果——不发明新数据库，也不替代 MCP / A2A。

本协议与参考实现源自调研《多 Agent 共享文件与记忆库并发冲突防护调研》的结论：工程原语（锁、事务、MVCC、CRDT）已经成熟，缺口在于**跨 Agent 的共享资源契约**——谁先读、谁先写、版本如何比较、冲突如何消解、失败如何回滚。

## 设计原则

1. **薄**：只定义版本、租约、条件写、冲突与审计；存储、通信、身份都复用外部能力。
2. **语义集中**：所有并发判断只在一个 `Coordinator` 里完成，后端只提供原子事务。
3. **冲突是一等对象**：不静默 LWW；语义冲突保留、升级、可裁决、可审计。
4. **可证明**：同一套一致性测试在三种后端上执行，结果必须逐项相同。

## 架构

```
L4 传输绑定   MCP stdio tools / CLI / A2A artifact 元数据
L3 协议契约   SPEC.md + 8 个 JSON Schema + 错误码
L2 协调控制   ACL · 租约+fencing · CAS+版本向量 · 冲突策略 · 审计链 · 幂等
L1 后端适配   Memory / SQLite(WAL) / Git(flock + atomic rename + commit)
```

L2 是唯一理解并发语义的组件；L1 的三个后端只是 `load → 修改 → save` 的原子容器。这让"跨后端语义一致"成为可测试的断言，而不是设计声明。

## 快速开始

环境：Linux / WSL Ubuntu，Python ≥ 3.11，依赖仅 `jsonschema`。

```bash
cd ~/project/srcp
export PYTHONPATH=$PWD/src

# 全部一致性测试（72 项 × 3 后端 + 协议级测试）
python3 -m unittest discover -s tests -t . -v

# 命令行
python3 -m srcp.cli --db /tmp/srcp.sqlite3 declare --resource-id shared/plan.md
python3 -m srcp.cli --db /tmp/srcp.sqlite3 lease --resource-id shared/plan.md --principal agent:alice@a
python3 -m srcp.cli --db /tmp/srcp.sqlite3 read --resource-id shared/plan.md --principal agent:alice@a

# 挂载为 MCP server（stdio）
python3 -m srcp.mcp_server --db /tmp/srcp.sqlite3
```

MCP 客户端配置示例（Claude Code / Codex 等支持 stdio MCP 的 CLI）：

```json
{
  "mcpServers": {
    "srcp": {
      "command": "python3",
      "args": ["-m", "srcp.mcp_server"],
      "env": {
        "PYTHONPATH": "/home/ryan/project/srcp/src",
        "SRCP_DB": "/home/ryan/project/srcp/results/state.sqlite3"
      }
    }
  }
}
```

## 协议要点

| 要素 | 实现 |
|---|---|
| 资源声明 | `declare_resource`：URI、tenant、ACL、粒度、隔离级别、策略、保留策略 |
| 身份授权 | principal `scheme:id@scope`，owner / writer / reader 三级 ACL，可替换凭证钩子 |
| 读视图 | generation + version vector + SHA-256 content hash + `stale_risk` |
| 租约 fencing | token 由独立计数器递增；资源侧 high-water 只在成功提交时推进；旧 token 硬拒绝 |
| 条件写 | `expected_generation` CAS，可选 `expected_version_vector` 因果前置，idempotency key |
| 冲突 | `no-conflict` / `stale-write` / `mergeable` / `semantic-conflict` / `authority-conflict` / `schema-conflict` |
| 策略 | `abort` / `append` / `merge` / `lww` / `manual`；策略覆盖权限仅 owner 所有 |
| 审计 | append-only SHA-256 hash chain；失败写也记录错误码；`verify_audit()` 检出篡改 |
| 降级 | `capabilities()` 协商 L0/L1/L2；不支持时显式报错，不静默 LWW |

完整规范见 [`spec/SPEC.md`](spec/SPEC.md)，线上对象由 `spec/schema/*.json` 约束，协议映射见 [`spec/bindings/`](spec/bindings/)。

## 一致性测试

`tests/conformance.py` 定义了 21 个后端无关场景，`tests/test_backends.py` 让 Memory / SQLite / Git 各跑一遍；`tests/test_protocol.py` 补充 Schema 校验、跨后端 parity、跨进程 CAS、MCP/CLI 端到端与负向对照。

关键场景：丢失更新、僵尸写者（fence token 失效）、租约过期、心跳续约、幂等重试、append 双写保留、字段级 merge、语义冲突升级与 owner 裁决、LWW 强制留痕、manual 排队、越权写、路径穿越、审计篡改、拒绝写留痕、陈旧读风险提示。

实测结果与基准数据见 [`results/`](results/)。

## 示例

```bash
PYTHONPATH=src python3 examples/naive_lost_update.py    # 无协议基线：一次更新静默丢失
PYTHONPATH=src python3 examples/protected_write.py      # SRCP：冲突显式化 + 重读重试
PYTHONPATH=src python3 examples/stale_writer.py         # 暂停后苏醒的旧写者被 fence 拒绝
PYTHONPATH=src python3 examples/multi_agent_plan.py     # hermes / claude-code / codex 共享计划
PYTHONPATH=src python3 examples/benchmark.py --rounds 100
```

## 已知边界

- 跨资源事务只有 prepare/commit/abort 接口骨架，**不承诺**分布式原子性。
- principal 是可替换的字符串钩子，**不是**完整的认证/签名体系。
- `merge` 仅处理 JSON 对象的字段级合并；同字段冲突必须人工裁决，协议不替业务判定真值。
- 控制面状态以单个 JSON blob 存储，换取实现薄与语义一致；高吞吐生产场景应映射到分表后端。
- 不承诺跨 Agent read-your-writes，也不承诺全局顺序。

## 路线图

对齐调研文档的四步推广路径：① 补 Postgres / Redis / 本地文件适配器和 TypeScript 客户端；② 做 LangGraph / Letta / CrewAI 插件；③ 与 LockRe、agent-coherence、MELD、MPAC 做兼容网关而非要求重写；④ 把一致性测试集与 RFC 带入 MCP / A2A / AGNTCY 社区。

## 许可证

Apache-2.0。协议文本、Schema、参考实现与一致性测试采用同一宽松许可，避免"协议控制权"顾虑。详见 [`LICENSE`](LICENSE)。
