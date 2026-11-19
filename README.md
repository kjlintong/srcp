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
L6 磁盘权威   FileAuthority —— 绑定资源到真实文件，磁盘为内容权威，检测并采纳 out-of-band 写
L5 能力层     memory/claim/file_edit —— agent 主动想用的能力（自愿采用的驱动力）
L4 传输绑定   协议 MCP (mcp_server) / 能力 MCP (service_server) / CLI / A2A
L3 协议契约   SPEC.md + 8 个 JSON Schema + 错误码
L2 协调控制   ACL · 租约+fencing · CAS+版本向量 · 冲突策略 · 审计链 · 幂等
L1 后端适配   Memory / SQLite(WAL) / Git(flock + atomic rename + commit)
```

L2 是唯一理解并发语义的组件；L1 的三个后端只是 `load → 修改 → save` 的原子容器。这让"跨后端语义一致"成为可测试的断言，而不是设计声明。

**为什么要 L5**：协议原语（lease/read/write）是"合规负担"——agent 每次写都要正确编排三个调用。L5 把它们包装成**单调用能力**，让"做正确的事"成为"最省事的事"：

| 能力 | 工具 | 解决 agent 的真实痛点 |
|---|---|---|
| 共享记忆 | `memory_commit` / `memory_recall` | 开工前查到别人踩过的坑，不重复调查 |
| 任务认领 | `plan_claim` / `plan_complete` / `plan_status` | 原子认领，杜绝两人做同一个任务 |
| 安全编辑 | `file_edit` / `file_read` | 一次调用完成 lease+读+条件写+重试；不同字段并发编辑都保留 |

L5 全部复用 L2 的 Coordinator，**不重造任何锁或版本逻辑**：memory 用 append 策略天然无冲突，claim 用 CAS+fencing 保证唯一赢家，file_edit 内部执行"重读→变换→CAS→冲突重试"。职责划分：Coordinator 只做 CAS + 冲突检测，能力层做语义变换 + 重试。

`AGENTS.md` 是跨 runtime 的工作流指令（Claude Code / Hermes / Codex 都能读），配套的 WorkBuddy skill 在 `~/.workbuddy/skills/srcp-shared-workspace/`。这是"自愿遵守协议"的 adoption 层——用这些能力对 agent 自己有好处，而非被强制。

**L6 消除影子副本**：默认情况下 SRCP 的 content 存在自己的 store 里，与 agent 用原生工具编辑的真实文件是两份副本，会永久漂移。启用 `--root` 后可把资源 `file_bind` 到工作区真实文件：

- 绑定时文件的当前字节成为 generation 0 的初始内容（不丢已有内容）。
- 之后每次 `file_edit` 在同一事务内**原子落盘**（tmp + fsync + os.replace），元数据与文件不可能分歧；`file_read` 内容直接来自磁盘并校验哈希。
- 若有人绕过协议直接用文件工具改盘，读写都会返回 `AUTHORITY_CONFLICT`，**绝不静默覆盖**；owner 用 `file_adopt` 把该改动作为新 generation 纳入审计后恢复同步。
- 路径经 realpath + commonpath 校验，`../` 穿越和符号链接逃逸被拒绝。

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

# 挂载协议 MCP server（stdio，暴露 10 个协议原语）
python3 -m srcp.mcp_server --db /tmp/srcp.sqlite3

# 挂载能力 MCP server（stdio，暴露 agent 友好能力）——推荐给真实 agent 用
python3 -m srcp.service_server --db /tmp/srcp.sqlite3

# 带磁盘权威：把资源绑定到真实文件，消除影子副本
python3 -m srcp.service_server --db /tmp/srcp.sqlite3 --root /path/to/workspace
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
        "SRCP_DB": "/home/ryan/project/srcp/results/state.sqlite3",
        "SRCP_ROOT": "/home/ryan/project/srcp"
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

`tests/conformance.py` 定义了 21 个后端无关场景，`tests/test_backends.py` 让 Memory / SQLite / Git 各跑一遍；`tests/test_protocol.py` 补充 Schema 校验、跨后端 parity、跨进程 CAS、MCP/CLI 端到端与负向对照；`tests/test_capabilities.py` 覆盖能力层（记忆并发追加无丢失、claim 单赢家、owner 校验、claim 过期抢占、字段级并发编辑、审计链）；`tests/test_file_authority.py` 覆盖磁盘权威（绑定种子、原子落盘、out-of-band 检测、adopt、哈希栅栏、路径穿越、能力层集成）。

关键场景：丢失更新、僵尸写者（fence token 失效）、租约过期、心跳续约、幂等重试、append 双写保留、字段级 merge、语义冲突升级与 owner 裁决、LWW 强制留痕、manual 排队、越权写、路径穿越、审计篡改、拒绝写留痕、陈旧读风险提示。

实测结果与基准数据见 [`results/`](results/)。

## 示例

```bash
PYTHONPATH=src python3 examples/naive_lost_update.py    # 无协议基线：一次更新静默丢失
PYTHONPATH=src python3 examples/protected_write.py      # SRCP：冲突显式化 + 重读重试
PYTHONPATH=src python3 examples/stale_writer.py         # 暂停后苏醒的旧写者被 fence 拒绝
PYTHONPATH=src python3 examples/multi_agent_plan.py     # 三 runtime 直接用协议原语共享计划
PYTHONPATH=src python3 examples/capability_collab.py    # 双 agent 用能力层协作（推荐）
PYTHONPATH=src python3 examples/disk_authority.py       # 消除影子副本：绑定→落盘→检测→adopt
PYTHONPATH=src python3 examples/benchmark.py --rounds 100
```

## 已知边界

- 跨资源事务只有 prepare/commit/abort 接口骨架，**不承诺**分布式原子性。
- principal 是可替换的字符串钩子，**不是**完整的认证/签名体系。
- `merge` 仅处理 JSON 对象的字段级合并；同字段冲突必须人工裁决，协议不替业务判定真值。
- 控制面状态以单个 JSON blob 存储，换取实现薄与语义一致；高吞吐生产场景应映射到分表后端。
- 不承诺跨 Agent read-your-writes，也不承诺全局顺序。
- 能力层的 memory 是 append-only 的事实日志，不做去重/摘要；大规模后应外接检索索引。
- `file_edit` 的 `replace` 遇到并发写入会**表面冲突而不静默覆盖**；`json_set`/`json_merge` 在不同字段上可无冲突并发。
- 磁盘权威需要显式 `file_bind`；未绑定的资源仍用 SRCP 内部存储。adopt 是 owner 权限，避免 writer 借采纳绕过冲突规则。
- 原子落盘依赖同目录 `os.replace`；跨文件系统的路径不受支持（绑定路径始终在 `--root` 内）。

## 路线图

对齐调研文档的四步推广路径：① 补 Postgres / Redis / 本地文件适配器和 TypeScript 客户端；② 做 LangGraph / Letta / CrewAI 插件；③ 与 LockRe、agent-coherence、MELD、MPAC 做兼容网关而非要求重写；④ 把一致性测试集与 RFC 带入 MCP / A2A / AGNTCY 社区。

## 许可证

Apache-2.0。协议文本、Schema、参考实现与一致性测试采用同一宽松许可，避免"协议控制权"顾虑。详见 [`LICENSE`](LICENSE)。
