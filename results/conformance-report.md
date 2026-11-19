# SRCP v0.1 一致性测试报告

- 日期：2026-11-19
- 环境：WSL Ubuntu 24.04，Python 3.12.3，git 2.43.0，jsonschema 4.10.3
- 命令：`PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`
- 结果：**122 项通过，0 失败，0 错误**（耗时约 12 秒）
  - 协议层：72 项（原报告范围）
  - 能力层：24 项
  - 磁盘权威：26 项（新增，见下）

## 覆盖矩阵

| 类别 | 场景 | Memory | SQLite | Git |
|---|---|---:|---:|---:|
| 基础 | 提交、版本化读、幂等声明 | 通过 | 通过 | 通过 |
| 丢失更新 | 两写者同版本，后者被拒 | 通过 | 通过 | 通过 |
| fencing | 旧 token 苏醒后被资源侧拒绝 | 通过 | 通过 | 通过 |
| 租约 | TTL 过期拒绝、心跳续约 | 通过 | 通过 | 通过 |
| 幂等 | 重复请求不重复提交 | 通过 | 通过 | 通过 |
| append | 双方写入都保留 | 通过 | 通过 | 通过 |
| merge | 不相交字段合并、同字段语义冲突升级 | 通过 | 通过 | 通过 |
| lww | 接受但强制记录冲突与审计 | 通过 | 通过 | 通过 |
| manual | 排队 + owner 裁决 | 通过 | 通过 | 通过 |
| 安全 | 越权写、路径穿越、策略覆盖限 owner | 通过 | 通过 | 通过 |
| 审计 | hash chain 篡改检出、拒绝写留痕 | 通过 | 通过 | 通过 |
| 可见性 | stale read 风险提示、lease 归属校验 | 通过 | 通过 | 通过 |

## 协议级测试

| 测试 | 结果 |
|---|---|
| 线上对象符合 8 个 JSON Schema | 通过 |
| 三后端同场景输出逐项一致（parity） | 通过 |
| SQLite 跨进程 CAS：两个真实进程并发，仅一个提交 | 通过 |
| Git 后端每次状态变更产生 commit | 通过 |
| MCP stdio 端到端：initialize / tools/list / declare / lease / write | 通过 |
| CLI 端到端：冲突返回非零退出码 | 通过 |
| 负向对照：无协议时更新确实静默丢失 | 通过 |
| version vector 比较与归并 | 通过 |

## 能力层测试（L5，`tests/test_capabilities.py`）

能力层把协议原语包装成 agent 主动使用的能力，全部复用 Coordinator，不重造并发逻辑。Memory / SQLite 两后端各跑一遍：

| 能力 | 场景 | 结果 |
|---|---|---|
| 共享记忆 | 20 线程并发追加，0 丢失；tag/query 过滤 | 通过 |
| 任务认领 | 6 方并发抢同一任务，恰好 1 个 CLAIMED | 通过 |
| 任务完成 | 仅 owner 可完成；完成后不可再被他人认领 | 通过 |
| 租约失效 | claim 到 `hold_ms` 后可被他人抢占 | 通过 |
| 任务板 | 状态汇总正确（claimed/done/result） | 通过 |
| 安全编辑 | json_set 不同字段并发都保留；json_merge 递增合并 | 通过 |
| 安全编辑 | append 双写保留；非法 op 被拒 | 通过 |
| 审计 | 能力层全部操作后 hash chain 仍有效 | 通过 |

双 agent 端到端 demo（`examples/capability_collab.py`）实证：两个 agent 并发工作，认领无重复、共享文件不同字段编辑无丢失、记忆沉淀 3 条、审计链有效。

### 设计修正记录

开发能力层时发现的职责边界问题与修正，已固化为测试：
- 初版 `file_edit` 依赖 Coordinator 的 `merge` 策略做二次合并，与能力层自身的语义变换重复，导致 `json_merge` 第二次写被误判为语义冲突。修正为资源统一声明 `abort`（纯 CAS），能力层负责完整内容计算 + 冲突重试。
- 初版 `memory_commit` 在并发下旧 token 被 fence 拒绝即丢弃（20 线程丢 2 条）。修正为每次重试重新租约+重读，与 `file_edit` 一致。

## 磁盘权威测试（L6，`tests/test_file_authority.py`）

消除影子副本：磁盘真实文件是内容权威，SRCP 只存元数据 + 内容哈希。Memory / SQLite 两后端各跑一遍：

| 场景 | 结果 |
|---|---|
| 绑定时以已有文件字节作为 generation 0 | 通过 |
| 协议写在同一事务内原子落盘，元数据哈希与文件一致 | 通过 |
| 绕过协议的直接写盘在 read 时被检出为 AUTHORITY_CONFLICT | 通过 |
| 直接写盘后协议写被阻断，文件内容不被覆盖 | 通过 |
| read→write 之间发生直接写盘，哈希栅栏拒绝该写 | 通过 |
| owner 可 adopt 外部改动并恢复同步；非 owner 被拒 | 通过 |
| 陈旧 generation 的写被 CAS 拒绝，磁盘内容不变 | 通过 |
| 路径穿越（`../`、绝对路径、符号链接逃逸）被拒绝；归一化的安全路径允许 | 通过 |
| 重复绑定到不同路径被拒；未绑定资源写被拒 | 通过 |
| 能力层 file_edit/file_read 自动走磁盘权威路径 | 通过 |
| 全部操作后审计链仍有效 | 通过 |

端到端 demo（`examples/disk_authority.py`）实证完整生命周期：绑定→协议编辑落盘→直接改盘被检测且保留→owner adopt 后恢复同步，审计链有效。

### 设计要点

- 落盘用同目录临时文件 + fsync + `os.replace`，通过 `Coordinator.commit_hook` 在**状态事务锁内**执行，元数据与文件无法分歧。
- out-of-band 检测基于内容哈希比对（磁盘 hash vs SRCP 记录 hash），read 与 write 双重校验。
- adopt 限定 owner 权限，防止 writer 借"采纳"绕过资源的冲突策略；采纳作为新 generation 记录来源与操作者，审计不隐藏分叉事实。

## 基准结果

见 `results/benchmark.json`，100 轮双写者竞争：

| 指标 | 无协议基线 | SRCP |
|---|---:|---:|
| 静默丢失更新 | 100 | 0 |
| 显式检测到的冲突 | 0 | 2（CAS 层） |
| 旧写者被 fence 拒绝 | 不适用 | 98 |
| 需要重读重试的次数 | 不适用 | 100 |
| 双方更新最终都保留的轮次 | 0 | 100 |
| 审计链有效的轮次 | 不适用 | 100 |

**关键发现**：高并发下 98% 的异常在 fencing 门禁就被拦下（`FENCE_REJECTED`），只有 2% 走到 CAS 冲突（`CONFLICT`）。这验证了"独立 token 计数器 + 资源侧高水位"的必要性——如果 token 从高水位生成，两个并发租约会拿到相同 token，这道防线会直接失效。客户端应统一处理所有非 `COMMITTED` 结果：重新获取租约、重读、重试。

## 复现

```bash
cd ~/project/srcp
export PYTHONPATH=$PWD/src
python3 -m unittest discover -s tests -t . -v
python3 examples/benchmark.py --rounds 100
```
