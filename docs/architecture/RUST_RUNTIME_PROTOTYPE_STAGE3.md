> 文档分类：SUPERSEDED。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](../CONTRACTS.md)与[架构](../REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

> 历史记录：本文件保留当时的验证结果。2026-10-25 已退役下述 Stage3 CLI、Python reference 与旧资格脚本；复现需恢复 `da7fb7b` 或更早对应源码。现役 `kernel/src/runtime_lab.rs` 仍被 Authority 使用，未删除。当前验证见 [设备运行链迁移](../RUST_DEVICE_RUNTIME_MIGRATION.md)。

# 第三阶段：隔离 Rust Runtime 原型

任务：`CLINX-STAGE3-RUST-RUNTIME-20261022`。
现有协调任务：`task_23207b6af2084484ab2c53d4f842b8b0`，
标题 `CLINX P3 Rust Runtime Prototype`；复用其既有 workspace lease，未创建第二个托管执行。
冻结语义唯一来源：`CLINX_EXECUTION_CORE_V1 / 1.0.0` 的文档、35 个 golden fixtures 和
Python 校验器。四个 P2 文件与 P2 回执中的 SHA-256 一致，没有修改 V1 或历史 family。

实现状态：`PROTOTYPE_QUALIFIED`，`NON_AUTHORITATIVE`，`NOT_DEPLOYED`。
Python 继续协调；没有任何现行 runtime/Owner/Finalizer/TaskRegistry 路径导入本原型。

## 实现与输入可信边界

复用 `kernel/Cargo.toml`，增加 `execution_core.rs`、`runtime_lab.rs` 和
`clinx-runtime-lab` binary。原 `CLINX_KERNEL_V1` 和 `RUNTIME_WORKER_V1` reader 保留；
没有新 Cargo 项目、新依赖、checkout 或数据库 migration。
`serde_json` 仅增加既有 `float_roundtrip` feature：定向差分先证明默认浮点解析造成
合法 payload hash 不匹配，再修解析并增加 256 个固定种子回归样本。新 canonical writer
保留 Python 的 UTF-8、排序、整数/浮点区别及指数拼写；没有重算旧 fixtures 的 hash。

Rust 独立实现冻结表的 67 条边及全部 guards，不调用 Python 完成判断。
Python `runtime_lab/reference.py` 的语义判断直接调用未修改的冻结校验器。
Rust 接收确定性输入事实，校验合法转换、版本、完整 binding、owner tuple、lease 边界、
fencing、incarnation、sequence 和 hash；它不认证这些事实的来源，不生成授权。
`now` 是显式提供的可信时间样本；原型没有 heartbeat scheduler 或 wall-clock lease 续期。

有状态路径额外关联已提交的跨对象事实：worker incarnation、原 owner tuple、同资源
已知 epoch、当前 unresolved attempt、已知 delivery 与 committed terminal。不能同时改写
`stored_owner/presented_owner` 绕过历史 fence；不能用 terminal 输入中的 DELIVERED 覆盖
已持久的 PENDING；没有本地 exact terminal 不释放 allocation。这些是冻结文字规则的
本地账本检查，不把离线 snapshot 校验器当作完整数据库 authority。
这个最小 slice 不执行 owner takeover 或新的业务 retry；身份变更需要后续独立实现。

## 版本化协议

传输为 UTF-8 NDJSON。输入恰有四个字段；重复 JSON key、未知字段/operation/version、
非有限数和非法 frame fail closed。

```json
{"protocol_version":"CLINX_RUNTIME_LAB_V1","request_id":"unique-durable-command-id","operation":"inspect","payload":{}}
```

输出保留相同 `request_id`，固定 `protocol_version=CLINX_RUNTIME_LAB_V1`、
`contract_family=CLINX_EXECUTION_CORE_V1`、`contract_version=1.0.0`、
`authority=NON_AUTHORITATIVE`，结果放在 `result`。
`APPLY` 只表示本地原型接纳，绝不是执行许可。`extensions` 的诊断语义为 `NOT_COVERED`；
不改变判断，但仍纳入原事件完整性 hash。

| operation | payload 的全部字段 | 行为 |
|---|---|---|
| `evaluate` | `record` | 独立计算冻结转换 verdict，不提交 |
| `replay` | `events` | 接收 P2 **离线** envelope；验证完整 trace 后返回 projection，不提交 |
| `receipt` | `original, request` | 按 scope/key/完整语义 hash 判断历史重复，无授权恢复 |
| `ack` | `row, ack` | 纯 exact late-ACK 判定，不提交 |
| `recovery` | `record` | 冻结 verdict 与恢复分类；成功分为 `COMMIT_ORIGINAL_TERMINAL_ONLY` 或 `EXACT_RELEASE_ONLY`，不执行恢复 |
| `append` | `events` | 接收下述完整 Event envelope，整批验证后提交一个 journal frame |
| `record_delivery` | `row` | 保存确定性原调用交付证据，不执行 Host |
| `late_ack` | `ack` | 只补原 delivery 的 DELIVERED/ack_host_ref，不动 Execution/Attempt/Owner/lease |
| `checkpoint` | `record` | 仅为当前 FINALIZING 的 exact result 保存 RESULT_COMMITTED 证据，返回 HOLD_UNKNOWN，不推进 terminal |
| `inspect` | 无字段，即 `{}` | 回读已验证 projection、delivery、checkpoint 与事件数 |

有状态命令的 `request_id` 同时是 lab journal 内的 durable command key。相同 key 和
相同 operation/payload 返回 `DUPLICATE` 与原 receipt；语义变化 REJECT。纯查询不占该 key。
不同 key 的相同 Event ID 也只接受字节语义相同的原事件，不追加第二次。

`append` 的每个事件要求以下完整字段，不把 P2 的最小离线 envelope 当生产 Event：

```text
event_id family contract_version schema_version
aggregate_type aggregate_id sequence aggregate_version actor
causation_id correlation_id transaction_id idempotency_scope idempotency_key
payload payload_hash occurred_at recorded_at
```

`payload` 是冻结 `check_transition` record；identity、actor、post-version 须与外层一致。
时间固定 UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`，校验真实日历及 recorded >= occurred。
每个 aggregate 从 initial/version 0 开始，sequence 从 1 开始；独立 stream 不按时间重排。
新协议只覆盖 JSON/serde 可表示的有限数和整数范围，不宣称任意精度 JSON 整数兼容。
单 frame 上限 1 MiB，单次 process 上限 4096 请求，trace/本地事件上限 1024，
journal 上限 64 MiB；超过预算拒绝，不隐式截断旧事件。

## 持久化、恢复与崩溃窗口

必须显式指定 `--lab-dir`，首次加 `--init`，且目录必须不存在。
只在目录包含精确 `LAB_ONLY` marker 时重开；不接收 SQLite/生产数据库路径，不创建 service。
journal 使用独占 OS 文件锁、单调 position、前帧 hash 与当前完整请求 hash。
写入完整 frame 后 `sync_all`，再发布内存状态和返回 receipt。初始化同步文件、目录及父目录。
state/event/本地 receipt 的提交单元是一条 journal frame；它不是生产数据库事务，
没有实现真实 outbox/inbox 的网络投递或下游原子性。

重开时从零完整校验每条已提交命令、Event 和每个 prefix，再发布 projection。
回放不发命令、不续 lease、不连接 Provider/Host。result checkpoint 保留原 result，不能被
矛盾 terminal result 覆盖；终态已提交后，后续操作仅能补 exact allocation release。
部分 terminal bundle、hash/顺序/身份损坏 REJECT；dispatch/ACK/观察/结果证据不足保留
`HOLD_UNKNOWN`。`RECOVERY_REQUIRED` 不变成终态或资源可用证明。

测试专用 `CLINX_RUNTIME_LAB_CRASH` 可在原型子进程的下列点 `exit(86)`：

| 窗口 | 重启证据 |
|---|---|
| `BEFORE_APPEND` | 没有提交；原命令可首次接纳，事件数不会提前增长 |
| `AFTER_SYNC` | 回复丢失但提交存在；原 key 回读 DUPLICATE，不追加第二次 |
| `TORN_APPEND` | journal 不完整；启动失败，stdout 无 projection，不擅自删尾或重放业务 |
| RESULT_COMMITTED 后 | 仍 FINALIZING，原 result 保留；只在新 exact terminal 证据下补 terminal |
| TERMINAL_COMMITTED 后 | 保留 SUCCEEDED/原结果；不重新派发 |
| release 同步后回复丢失 | 原 receipt 回读；保持 SUCCEEDED + RELEASED，事件数不变 |

这些是实际退出/重开子进程的证据。没有声称模拟了机器掉电或磁盘控制器缓存丢失。

## 可重复运行与验收

在 canonical repository 运行，输出必须是新的 task-owned 目录：

```sh
python3 scripts/qualify_rust_runtime.py \
  --output /lab/runtime-worktrees/p620/artifacts/clinx-runtime-qualification-UNIQUE
```

脚本只运行冻结 CLI、本阶段 Python 差分测试、Rust library 单元测试和 fmt/clippy/Ruff；
不跑完整 Python suite、不复跑 P1、不运行 Provider/Host/部署。使用 `--locked --offline`
构建既有 Cargo 工程；日志、可执行文件、原始性能样本和 qualification receipt 保留。
脚本清除它自己创建的 target/临时 fixture 输出，不触及原工作区缓存或其他 artifacts。

已构建 binary 可手动启动：

```sh
/path/to/qualification/bin/clinx-runtime-lab --lab-dir /path/to/NEW-lab-journal --init
```

输入上面的 inspect NDJSON 即可回读。省略 `--init` 重开同一个 lab journal。
`runtime_lab.reference.run` 是有超时的一次性 Python 子进程协调入口；失败不自动重试。

本次证据目录：
`/lab/runtime-worktrees/p620/artifacts/clinx-rust-runtime-20261022T042100Z`。

| 定向 gate | 结果 |
|---|---|
| 冻结 CLI | 35/35 PASS |
| Rust library | 15 passed，其中新增 12 项、原 kernel 3 项 |
| Python/Rust differential + crash tests | 177 passed |
| 表覆盖 | 67 条合法边均 APPLY；每条对应 4 个非法变体均 REJECT |
| prefix 覆盖 | 固定恢复链、全局事件异常、160 个固定种子变异序列的每个 prefix；并比较每个 stateful command 后完整 snapshot |
| 精度/完整性 | 256 个有限浮点样本、12 个边界拼写、重复事件、hash/gap/bundle/元数据损坏 |
| 既有 kernel 定向兼容性（原 ownership、replay prefixes、protocol） | 6 passed；使用本次重编译 binary |
| fmt / clippy / 仓库 Ruff / diff | PASS |

`qualification-final-v3/benchmark.json` 保留一轮 warmup 和五轮测量、input/binary SHA-256。
工作负载为 64 个逐条提交并 `sync_all` 的 RuntimeWorker heartbeat；release build，
本机子进程 wall time 中位数：总批次 **93.67 ms**，摊销每命令 **1.464 ms**；
重开并完整回放 64 条 journal 的中位数 **79.70 ms**。包括启动、JSON/stdio、复制、
全量验证和本机文件系统成本；不等于 Rust 纯计算时间、单次延迟分位数或生产容量。
实际崩溃窗口的逐步状态、exit code、原 receipt 与 journal hash 见
`crash-recovery.json`；原始 journal 压缩保留于 `crash-journals.tar.gz`。
result/terminal 提交后 allocation 仍为 ACTIVE，显式 release 后才 RELEASED；共 10 个事件，
只有一次原始 dispatch。

当前实现逐次验证完整 trace、复制 prototype state，规模增长不是 O(1)；没有做缓存/
compaction/调度性能优化，也没有据此提出生产容量结论。

## 未实现或未验收的生产能力

没有部署、service、生产 DB/schema 修改、权威路径切换、真实 Provider/Host、外部任务重放、
物理 writer fencing、授权认证、可信时钟采样、调度器/监督进程、跨机器协调、生产资源容量
事务、生产 outbox/inbox 投递、业务 retry、完整 ProviderSession 持久模型或历史 family migration。
没有把 OS 文件锁当作资源 lease，也没有把 journal hash chain 当作输入证据真实性证明。
断尾 journal 需后续独立离线恢复程序，当前拒绝启动；未实现自动修复、日志压缩、生产
长期运行、P99/压力/掉电验收。诊断 `extensions` 语义保持 `NOT_COVERED`。

本阶段只完成本地隔离原型资格，不授予生产执行权。V1 Owner/Finalizer/TaskRegistry 继续权威。
