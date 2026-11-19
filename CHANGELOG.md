# Changelog

本文件记录项目的显著变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [0.1.0] - 2026-11-19

首个公开版本：协议规范、参考实现、能力层、磁盘权威层与一致性测试套件。

### Added

- **协议层（L2–L4）**：SRCP v0.1 规范与 8 个 JSON Schema；资源声明、principal/ACL、generation + 版本向量、租约 + 独立 token 计数器的 fencing、条件写 CAS、5 种冲突策略、SHA-256 hash chain 审计、幂等键、L0/L1/L2 能力降级；MCP / A2A 绑定与 CLI。
- **后端（L1）**：Memory / SQLite(WAL) / Git(flock + atomic rename + commit) 三种后端，共享同一套一致性语义。
- **能力层（L5）**：`memory_commit/recall`、`plan_claim/complete/status`、`file_edit/read`，以及能力型 MCP server（9 个 agent 友好工具）。能力全部复用 Coordinator，不重造并发逻辑。
- **磁盘权威层（L6）**：`file_bind` 把资源绑定到工作区真实文件，内容为磁盘权威；协议写在状态事务内原子落盘；read/write 双重哈希校验检测 out-of-band 修改；owner `file_adopt` 纳入外部改动；realpath + commonpath 路径防护。
- **adoption 层**：跨 runtime 的 `AGENTS.md` 工作流指令与 WorkBuddy skill。
- **一致性测试**：122 项（协议 72 + 能力层 24 + 磁盘权威 26），覆盖丢失更新、僵尸写者、租约过期、幂等、字段级并发、冲突策略、越权、路径穿越、审计篡改、跨进程 CAS、MCP/CLI 端到端。
- **示例与结果**：7 个可运行示例（含无协议基线、双 agent 协作、影子副本消除）、100 轮基准数据与一致性报告。
- **发布工程化**：Apache-2.0 许可证、GitHub Actions CI（Python 3.11/3.12）、可构建的 wheel 包。

### Notes

- 跨资源事务、完整认证体系、语义级真值裁决不在 0.1.0 范围内，详见 README「已知边界」。
