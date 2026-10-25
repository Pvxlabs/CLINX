# 第二阶段：执行核心状态契约 v1

状态：FROZEN；契约标识 `CLINX_EXECUTION_CORE_V1`，版本 `1.0.0`，2026-10-22 UTC。
冻结含义：本文、[转换表与 golden fixtures](../../fixtures/execution_core_v1.json)、
[离线校验器](../../kernel/examples/check_execution_core_contract.rs) 共同定义规范；定向测试必须通过。
这是后续实现的约束，不声称现行 V1 已实现所有 V2 原子性，也不授予执行权限。
当前 V1 Owner/Finalizer/TaskRegistry 保持权威，shadow、runtime_control 和已有 Rust
decision-kernel 均不因此获得生产权威。本阶段不启用它们，不实现或启动 Rust Runtime。
CLINX 仅流通任务、关联证据、协调执行和交付；上游授权与下游系统权限、业务规则
保持原权威。`actor` 是内部职责标记，不是认证或授权凭证。

## 1. 独立任务与证据边界

本阶段任务 `CLINX-STAGE2-STATE-CONTRACT-20261022`，直接用户开发指令授权。
canonical repository `/home/pvxlabs/dev/clinx`，保留开始时全部 68 个 dirty/untracked
文件；不新建 checkout，不 fetch/merge，不修改生产 DB、Provider、service 或 lease。
复用现有任务协调渠道；本阶段不是新建一次托管 Provider 执行。
2026-10-22 当前只读查询确认：P1 所记 `task_cc55f83e890646fc817215fe303365bb`
已 COMPLETED，其原 lease 已释放。历史 lease 不能作为当前写入授权。
独立任务身份与保护哈希、定向验证日志见本次 task-owned artifacts 目录的 receipt。

## 2. 权威、关系与字段

所有持久对象有 `id`（下面的具名 ID）、`contract_version`、非负 `version`、
`lifecycle`、`created_at`、`updated_at`、`evidence_refs`。时间为 UTC；未知为 null，
不能用空字符串、旧 task 指针或当前时间制造证据。ID 不复用；对象引用须在同一
租户/项目作用域存在且匹配。版本是对象写入 CAS，不等同事件流 sequence。
对象表中的字段是规范字段，允许由既有表组合投影；本阶段不加 DB migration。

| 对象 | 职责和唯一持久化权威 | 不可变标识、关系与额外状态字段 |
|---|---|---|
| Task | 人的意图和长期生命周期；Task command aggregate，执行成功不能自动完成 Task | `task_id, project_id, workspace_id, intent_revision`；`intent, objective, blocker, cancellation_requested_at`。一个 Task 有多次 Execution |
| Execution | 一次已封存请求；Execution aggregate/Owner 接纳，只有 Finalizer 决定终结 | `execution_id, task_id, prepared_ref, request_hash, policy_ref, route_snapshot`；`deadline_at, cancellation_requested_at, owner_ref, active_attempt_id, finalization_phase, terminal_outcome, result_ref`。1:N Attempt，本版本最多一个未解决 Attempt |
| Attempt | 一次具体 Provider/worker 实现；scheduler 创建，当前 fenced worker 推进，Finalizer 判结果 | `attempt_id, execution_id, retry_index`（同 execution 唯一，0 起），`provider_session_id, assignment_id`；`dispatch_certainty, provider_binding, progress_cursor, outcome`。重新连接或重新观察不增加 retry_index |
| ProviderSession | Provider 连续性；adapter 记录原生事实，持久 session ledger 管理绑定 | `provider_session_id, provider_id, adapter_kind`；`session_ref, thread_ref, turn_ref, operation_id, connection_generation, provider_cursor, capability_snapshot, provider_version, liveness, observed_at`。thread 可连续使用；Attempt 的 exact turn 绑定一经派发不可替换，历史绑定保留；session 不是 execution |
| RuntimeWorker | 稳定 worker 与当前进程代际；worker registry/assignment ledger | `worker_id, host_ref`；`incarnation_id, incarnation_generation, capabilities, capacity, heartbeat_at, assignment_ids`。一个 worker 可有多个有界 assignment，每个 Attempt 仅一个当前 assignment |
| ResourceAllocation | canonical worktree/host/provider slot 等资源使用权；resource coordinator ledger | `allocation_id, resource_key, resource_type, capacity_units, assignment_id, attempt_id, worker_id, incarnation_id, resource_epoch`；`expires_at, renewed_at, release_reason, safety_handoff_ref`。exclusive=1；容量资源须事务内检验总和，不能重复计数 |
| Event | 不可变已提交事实；event store，受对象 authority/CAS/fence 约束 | `event_id, family, schema_version, aggregate_type, aggregate_id, sequence, aggregate_version, actor, causation_id, correlation_id, transaction_id, idempotency_scope, idempotency_key, payload, payload_hash, occurred_at, recorded_at`。对象 1:N 事件，correlation 不赋予写权限 |

Assignment 是 RuntimeWorker 与 Attempt、ResourceAllocation 的持久关系，不另立第八个
执行核心对象。完整 owner tuple 为 `(execution_id, attempt_id, assignment_id, worker_id,
incarnation_id, resource_key, resource_epoch)`；所有相关版本、lease 和 pending safety guard
必须在写点同一事务检查。`owner_ref` 不是 requester PID；requester/tunnel 消失不撤销 Owner。

## 3. 状态机与正交状态

完整合法转换、内部职责 actor 和 guard 在 JSON `objects.*.transitions`；未列出的边一律
REJECT。相同状态也必须是显式列出的 renew/heartbeat/observation，不能任意跳过检查。
终态不再转换；重复命令只返回旧 receipt，不是第二次状态转换。

| 对象 | lifecycle | 终态及规则 |
|---|---|---|
| Task | OPEN, ACTIVE, BLOCKED, COMPLETED, CANCELLED, ARCHIVED | COMPLETED/CANCELLED 是工程结果，仅可 archive；ARCHIVED 不可恢复。Task cancel 不等于子执行已停止；cancel 前须无未解决执行，否则仅记录取消意图 |
| Execution | REQUESTED, ADMITTED, RUNNING, FINALIZING, RECOVERY_REQUIRED, SUCCEEDED, BLOCKED, FAILED, CANCELLED | 最后四项是不可改写结果；RECOVERY_REQUIRED 是非终态。完成结果与资源释放为独立事实，终态行也可能仍持 lease |
| Attempt | PENDING, ASSIGNED, PROVIDER_STARTED, TURN_RUNNING, OBSERVING, LOST, TERMINAL | TERMINAL 不可复用。LOST 不证明未执行。LOST→OBSERVING 是原身份观察恢复；LOST→PENDING 仅明确未派发且旧 writer 已停止/fenced，不得重发可能执行过的请求 |
| ProviderSession | DISCOVERED, BOUND, ACTIVE, PAUSED, UNAVAILABLE, CLOSED | CLOSED 不再恢复，新连接必须用新 generation；关闭连接不证明 turn 已结束 |
| RuntimeWorker | REGISTERED, HEALTHY, DRAINING, OFFLINE | OFFLINE 为该 incarnation 结束；稳定 worker 经显式新 incarnation 登记可回 REGISTERED，generation 增 1 且旧 assignment 隔离；旧 incarnation 永不复活 |
| ResourceAllocation | REQUESTED, HELD, ACTIVE, QUARANTINED, RELEASE_PENDING, RELEASED | RELEASED 终态；expiry/orphan → QUARANTINED，不自动成为可用资源。释放后另建 allocation，epoch 增长 |
| Event | COMMITTED | 只允许不存在→COMMITTED；禁止更新和删除；更正是新事件 |

Execution 正交字段：`dispatch_certainty=NOT_DISPATCHED|DISPATCHED|UNKNOWN`，
`provider_liveness=LIVE|TERMINAL|UNKNOWN`，`transport_health=HEALTHY|DEGRADED|UNAVAILABLE`，
`finalization_phase=NONE|RESULT_COMMITTED|TERMINAL_COMMITTED|RELEASE_COMMITTED`。
UNKNOWN 是事实不足，不是“未执行”或可清理终态；到期仍 UNKNOWN 时进入 RECOVERY_REQUIRED。
Attempt outcome 与 Execution outcome 分开；Host exit 0、Provider completed、response sent、
ACK DELIVERED 都不是 Execution SUCCEEDED 的同义词。SUCCEEDED 也不等于业务系统验收。

## 4. 超时、取消、重试与幂等

| 对象 | 超时/取消 | 重试与幂等键（均为 scoped durable key） |
|---|---|---|
| Task | 不从执行 timeout 推导 Task 终结；显式取消先向未解决子执行记录意图 | `(project_id, client_task_key)` + intent hash；新意图须新 revision，不能改写原请求 |
| Execution | deadline/取消只记录 intent，停止新副作用；interrupt ACK 后等 exact terminal 与 delivery 决议。超时不释放 | `(prepared_scope, prepared_ref)` + sealed request hash；相同返回原 execution/receipt，语义变化冲突。未知 Owner/start ACK 保留 consumed prepared，不 re-arm |
| Attempt | 观察超时或 owner 丢失为 LOST；不因超时推断 FAILED/NOT_DISPATCHED | `(execution_id, retry_index)`；自动 retry 仅 policy 允许、预算/截止未耗尽、无 cancel、已提交明确未派发证据且旧 writer 停止并 fenced。已运行失败后任何再次业务运行是显式新请求，不自动 replay |
| ProviderSession | 观察超时 UNAVAILABLE，连接 close 与业务取消分开 | `(provider_id, operation_id, typed_native_request_id, connection_generation)`；同进程缓存响应可重交付，跨进程必须有 durable receipt。新 generation 不允许重发 turn/start |
| RuntimeWorker | heartbeat 失联影响可观测性，lease 到期才失去写权限；新 incarnation 隔离旧 assignment | `(worker_id, incarnation_generation)` 注册键；heartbeat/assignment command 各自 scoped key；回读历史 receipt 不恢复权限 |
| ResourceAllocation | `trusted_now >= expires_at` 拒绝 renew/write；worker 时间不可延长 lease。取消不直接 release | `(resource_scope, allocation_command_key)`；epoch 从 1 开始，按 resource_key 单调递增，释放/重启不得清零；epoch=0 仅无授权的旧 DTO/未分配对象 |
| Event | 无超时删除；delivery timeout 留 pending outbox/inbox，不改写事件 | `(idempotency_scope, idempotency_key)` + 完整语义 hash；同 native event ID 去重，缺 native ID 不以相同 payload hash 合并 |

Host 调用另用 `(execution_id, tool_call_id)`，完整绑定包含 thread/turn、namespace/tool、
operation、generation 和 host_execution_ref。typed request id `1` 与 `"1"` 不相同。
相同 call ID 只返回缓存或拒绝，禁止再次 handler；换 call ID 不能绕过同 execution 的未决
副作用。幂等承诺是本地 admission/receipt，不声称网络或下游业务 exactly-once。

## 5. Provider 可重观测事实与账本事实

Provider 可只读重新观察 exact session/thread/turn 的活动、terminal status、当前 owner
claim、原生 result/event 与 cursor；必须保留 endpoint、generation、observed_at、完整性。
LIVE 只认唯一 exact active owner；TERMINAL 要 exact owner terminal、所有配置 endpoint
可确认、无 active claim/冲突。历史 completed、notLoaded、最新 turn、缺失 turn、空列表、
Provider idle、interrupt 的 no-active 错误、旧 `task.codex_running` 都不是负面执行证明。
现有 liveness grace=120s；未知观察本身不刷新 progress，不延长 grace。

只有持久化账本决定：封存请求/消费、Execution/Attempt 身份、原 Owner、派发 reservation、
明确 pre-dispatch rejection、Host process/exit evidence、response-sent、exact ACK、lease/epoch、
幂等 receipt、Finalizer decision/result/history/outbox。Provider 观察必须经 exact identity
校验和账本提交才成为相关证据；它不能重建丢失的请求账本或猜测 ACK。

晚 ACK 可补充原交付行：必须匹配完整绑定、原 host receipt、原 call ID、response-sent 和
ACK 内容；不变更 execution/attempt/owner/lease，不调用 handler，不追认新代写权限。
旧连接事件先作诊断；只允许通过 exact 账本 reconciliation 补录历史 ACK，不直接作为
当前 Provider operation 的权威事件。错代/错 owner/错 receipt 不推进状态。
Provider terminal 而 delivery pending/failed、Host completion UNKNOWN、Owner 冲突或结果
矛盾时 HOLD_UNKNOWN；已明确的失败可用现有授权 reconciliation 留下 RESOLVED 审计，
原 failed receipt/result 不改成成功，且不 replay。人工 review 不是 ACK，也不授予新权限。

## 6. 原子提交与崩溃恢复

网络/Provider/Host 操作不置于 DB 写事务内。不存在跨 Provider 和本地 SQLite 的原子提交。

1. **Admission**：execution/request/consumed prepared/Owner reservation/初始 event/receipt/
   outbox 同一事务。先 admission，后外部发送；二者之间故障也必须保守 UNKNOWN，除非
   已持久明确未派发证据。没有 Host 行不证明未执行。
2. **Fenced mutation**：按 ADR-003 先持久 safety handoff，锁内重新取 trusted clock，检查
   完整 owner tuple、incarnation、epoch、所有 expected versions、lease、guard；state/event/
   receipt/outbox 与本 token guard 清除同提交。失败保留 guard 至安全决议持久完成，
   pending guard 一律拒绝旧权限。数据库 fence 不等于物理停止证明。
3. **Finalizer**：仅 exact execution/attempt 的当前有效证据可决定结果。结果记录可先
   单独持久化为 RESULT_COMMITTED；terminal decision + terminal event + receipt + outbox
   是一个事务，为 TERMINAL_COMMITTED。禁止部分 terminal bundle 获得释放许可。
4. **Release**：重读 exact owner terminal、所有 delivery 决议、无活 writer/conflict、
   精确 lease 与 committed terminal bundle；history + 精确 allocation/lease release +
   event/receipt/outbox 同一事务，成为 RELEASE_COMMITTED。后置 projection 可失败重试。

上述是冻结的目标边界。现行 Finalizer 已有 result→terminal→release→projection 顺序，
但多次 registry 调用不天然是整个原子 bundle；不得声称已上线目标事务。
RESULT_COMMITTED 崩溃后重观测原执行，再补 terminal；TERMINAL_COMMITTED 崩溃后只补
exact release，不改结果、不重跑；release ACK 丢失只读原 receipt。任何身份变化或无
fresh terminal 证据都 HOLD_UNKNOWN。结果与 Provider 不一致时不能通过覆盖旧结果收敛。
只有完整性冲突（缺事件、hash 错、部分目标 bundle）才 REJECT；未知运行事实为 HOLD_UNKNOWN。

## 7. 事件顺序、恢复重放与兼容

每 aggregate stream `sequence` 从 1 连续增长；`aggregate_version` 是命令 post-version，
从 0 起写一次增 1；同事务相关事件由 transaction_id 与 causation 引用关联。
global_position 只是存储位置，不是跨流业务全序；occurred_at/recorded_at 不用于重新排序。
晚 ACK 按 recorded append 顺序追加，保留原 occurred_at 和原 binding。

回放必须校验 family/schema、payload hash、对象 identity、版本/CAS、连续 sequence、
合法转换和每个 prefix 的安全性；先完整验证再发布 projection。重复同 event ID 且内容
完全一致可忽略；内容不同、顺序反转、缺 baseline、gap、错误身份一律拒绝。回放不发命令、
不续 lease、不调用 Provider/Host；从回放得到的 UNKNOWN 必须保持 UNKNOWN。
离线校验器是规范的纯函数，不是 scheduler、Runtime 或可执行的恢复授权。

本 family v1 的安全字段采用 closed vocabulary：未知 action/state/guard、major/minor/schema
或未知控制字段 fail closed；补充诊断放 `extensions`，报告 NOT_COVERED，不能影响判定。
规范的语义变更须升版本及新增正反 fixture；patch 只可修说明或不改语义的证据。
既有 `V1*Observed`、`RUNTIME_WORKER_V1`、`CLINX_KERNEL_V1` family 不改字节、不换 hash，
按各自既有 reader/upcast 读取；不得把本表直接套在这些历史事件上。
V1 `COMPLETED/IN_REVIEW` 映射 SUCCEEDED 仅在 exact 已提交 Finalizer decision 下成立；
`TRANSPORT_UNCERTAIN` 映射 liveness UNKNOWN；`RECOVERY_REQUIRED` 永不映射 terminal。
runtime foundation `RELEASED` Attempt、`REGISTERED` worker 等仅是原 slice 的事实，
没有 V2 terminal 证据时不补造完整执行状态。旧 epoch=0/缺版本记录只能 NOT_COVERED，
不能通过默认值恢复写权限。新 family 使用显式升级器，不在本阶段导入历史运行对象。

## 8. 冲突证据与最小一致规则

| 证据 | 冲突/容易误读处 | 冻结规则 |
|---|---|---|
| ADR-001、v2-review §State machines/Runtime worker | LOST→PENDING、到期换 epoch 的简图不足以证明安全 takeover | ADR-003 的 quarantine、旧进程停止、side-effect fencing、guard 是必要条件；ACK UNKNOWN 禁止自动 retry |
| task_registry.py `TERMINAL_EXECUTION_STAGES` 与 EXECUTION_LIVENESS_V2 `LEASE_SAFETY` | legacy 名单含 RECOVERY_REQUIRED | 生命周期分类不能单独释放；采用 exact liveness/release guard，RECOVERY_REQUIRED 非终态 |
| m9_integration.py `ExecutionFinalizer.finalize`、EXECUTION_OWNER_R1 §提交 | 多个提交窗口与目标 event/state 原子要求 | 显式 finalization_phase、terminal bundle，允许原执行补收尾，不重派发；现行多事务兼容性是实现缺口 |
| ADR-004 Failure/Retry、execution_owner.py `start` | pre-send error 与 sent-but-unanswered 容易混同 | 只有持久、精确 pre-dispatch proof 可证明 NOT_DISPATCHED；Owner 丢失拒绝第二 start |
| HOST_EXECUTION_CONTRACT_V2 §错误、tool_delivery.py `admit/observe` | response snapshot 的 PENDING/SAFE_TO_CONTINUE 并非 ACK | 实际准入由 durable ledger 决定；Provider terminal 与 delivery 分开 |
| TUNNEL_STAGE1_CLOSURE_20261022 §下一独立阶段 | 新 transport 代可能误建 Attempt | 复用其已确认事实与测试结论，不重跑 P1；transport 恢复不换业务身份/Owner/lease |
| ADR-005 / clinx-v2-engineering-closure | 仓库已存在 Rust decision kernel 实验 | 保持 NON_AUTHORITATIVE；本次“第三阶段”是新的 Runtime 原型，不重启历史实验或声称从零开始 |

## 9. 验收和后续边界

`cargo run --locked --manifest-path kernel/Cargo.toml --example check_execution_core_contract`
校验冻结表及原 35 个 golden fixtures；
`cargo test --locked --manifest-path kernel/Cargo.toml --test execution_core_qualification`
验证表结构拒绝条件与 facts 合并，`cargo test --locked --manifest-path kernel/Cargo.toml execution_core::tests`
继续验证关键不变量和负面回放。旧 Python 校验器已退役，纯语义复用现役 Rust `execution_core`。
JSON 是转换表与规范 fixture，不是生产 wire/schema 替换。校验器检查输入账本快照的一致性，
不认证输入引用的真实性；`replay` 使用最小 envelope 验证 identity、sequence、版本、hash
和转换。完整 Event 字段仍按第 2/7 节及各原 family reader 验证；不得直接导入这个离线格式。
只附加运行与本契约有关的现存 Owner/liveness/ledger/fencing 定向测试，不跑完整 suite/P1。
测试 verdict：APPLY（合法转换）、DUPLICATE（旧事实回读）、HOLD_UNKNOWN（证据不足，
保留所有权/隔离）、REJECT（非法转换/冲突/损坏）；HOLD 与 REJECT 都不发起副作用。

契约通过不等于部署、生产 physical fencing、live Provider、业务验收、兼容 migration 或
Runtime 实现通过。未决实现项：目标事务 bundle/独立 Attempt 调度、ProviderSession 持久
模型、跨进程 ACK/inbox 恢复及物理 writer fencing。第三阶段可在独立授权后实现隔离、
非权威 Rust Runtime 原型并以本契约作 conformance gate，不可自动切换现行权威。

## 10. 本次冻结回执

独立任务 `CLINX-STAGE2-STATE-CONTRACT-20261022`；本地新增四个文件，现行实现零修改。
证据目录：`/lab/runtime-worktrees/p620/artifacts/clinx-state-contract-20261022T040337Z`。
`receipt.json` 固定四文件与阅读依据的 SHA-256、保护前后哈希、任务及清理归属。

| 定向验证 | 结果 | 证据 |
|---|---|---|
| 新增 contract tests（含真实 SQLite late ACK） | 90 passed | `new-tests.log` |
| 既有 Owner/liveness/ACK/lease/fence/原子提交 | 16 passed | `affected-command.json`, `affected-tests.log` |
| 既有部分 finalizer commit 重启恢复、外来 lease 拒绝 | 2 passed | `partial-finalizer-command.json`, `partial-finalizer.log` |
| CLI 七对象转换表与 golden fixtures | 35 cases PASS | `contract-cli.log` |
| 仓库配置 Ruff、diff 空白 | PASS | `ruff.log`, `diff-check.log` |

18 项现存测试是受影响路径的选择性验证，不重跑 P1 或完整历史套件；R1 portable fixture
的测试信任根保持隔离，不宣称真实旧制品或生产恢复。所有测试数据库/Host 文件是临时
隔离 fixture，本任务未连接 live Provider，也未操作生产执行。
