# CLINX Host Execution Contract v2

## RCA before implementation — 2026-10-01

Source baseline: `65979aa017974e39ef81f8d2464488a6fb642f36`.
Isolated implementation checkout: `/home/pvxlabs/dev/clinx-host-contract-v2-20261001`.
Canonical runtime checkout contains unrelated uncommitted native-workspace work;
that work is preserved. Existing Provider delivery qualification is the baseline.

The authoritative path is capabilities (informational probes and request contract)
→ prepare (policy construction, route/project identity) → sealed ExecutionPolicy
→ TaskDispatcher dynamic-tool registration → Provider/worker tool call
→ app_server correlation/owner checks → ToolDeliveryLedger atomic admission
→ HostExecutor validation/command construction → durable Host start → process
→ Host completion → ledger host_result → response on original transport/request
→ exact Provider item/completed → ledger observe → ExecutionFinalizer → writeback.
Provider owns transport acknowledgement, Host owns execution facts, ledger correlates
and prevents replay, and Finalizer alone owns terminal result and lease release.

- CAPABILITY_DISCOVERY_ROOT_CAUSE: local binary probes were presented without an
  executable catalog. AVAILABLE is neither implemented-operation nor authority proof.
- OPERATION_DISCOVERY_ROOT_CAUSE: command branches, dynamic schema, examples and
  request capability vocabulary evolved separately. GIT exposed only push while
  generic development commands denied fetch, leaving no structured fetch path.
- PRE_DISPATCH_POISON_ROOT_CAUSE: admit rejected any execution with a non-DELIVERED
  row, including a conclusively rejected request that never reached Host dispatch.
- RECONCILIATION_ROOT_CAUSE: replay protection and continuation were conflated;
  failed() also inferred non-execution solely from missing Host evidence. Crash
  windows must stay uncertain without positive pre-dispatch rejection evidence.
- REGISTERED_TARGET_DISCOVERY_ROOT_CAUSE: partial top-level alias lists omitted
  operation/class/target associations and registered local-command identities.
  Live service identity is clinx-observer.service; registration does not deploy it.

## Gate ownership

1. Authority: prepare seals policy; Host verifies the immutable policy against
   the invocation and registered target. Provider identity checks bind the caller.
2. Execution boundary: registered project/route/cwd/branch/origin/operation/argv;
   canonical lease protects mutations (including local fetch ref updates).
3. Side-effect certainty: persistent call identity and unresolved dispatch/result
   delivery. Transport generation and exact ACK are evidence for this same gate.

QEX/DRS are not HostExecutor admission authorities and remain decoupled. Provider
correlation is not a second operation-authority system. Redundant bridge policy
membership checks can be removed in favor of Host validation; no authority is widened.

## Validation status

最终源码回归：652 passed、102 subtests passed、3 skipped（真实测试另行 opt-in）。
包含既有 native-workspace dirty 改动的临时集成回归：671 passed、102 subtests passed。
最终源码真实 Provider 测试：3 passed；临时集成源码真实九步序列：1 passed。
远端与 commit/tree 的最终回读见外部 `canonical-receipt.json`，避免在提交内自引用 SHA。
未运行、BLOCKED、UNKNOWN 不记 PASS。

## v2 可执行 contract

`host_contract.py` 是 operation vocabulary 的单一来源。每项包含 capability、精确
operation、允许的 operation classes、mutation/side-effect 语义、target kind、argument
schema 和安全 target identities。它同时生成 capabilities discovery、Provider dynamic
schema/description、managed prompt，并驱动 HostExecutor 的 argument/class 验证。

`clinx_get_capabilities.execution_surfaces.HOST_EXECUTOR` 保留旧 probes，并新增
`executable_contract.contract=CLINX_HOST_EXECUTION_CONTRACT_V2`。`capabilities` 中每个
request capability 分别公开 `probe_key`、`probe` 和 `operations`。空 operations 代表
没有对应 structured operation，即使本机 binary 的 probe 是 AVAILABLE。

services、SSH、network 和 registered local commands 只公开别名与 class/operation
元数据，不公开 argv、URL、凭据或真实主机文件路径。target 的 classes 与 operation
的 classes、sealed ExecutionPolicy 取交集；discovery 本身不授权。

### Git

| operation | 首选 operation class | 副作用与边界 |
| --- | --- | --- |
| `head` | `READ_ONLY_HOST` | 当前 registered project 的 local HEAD |
| `status` | `READ_ONLY_HOST` | worktree/index/branch 状态 |
| `fetch_origin` | `READ_ONLY_HOST` | `mutating=true`，仅 local objects 和 registered canonical branch 的 remote-tracking ref；必须持有现有 lease；无 merge/rebase/checkout/worktree 修改 |
| `remote_main_head` | `READ_ONLY_HOST` | `ls-remote` 查询 task 注册的 canonical branch，名字不假设真实分支必为 main |
| `ahead_behind` | `READ_ONLY_HOST` | registered local canonical branch 对 origin canonical branch 的 ahead/behind |
| `push_current_branch` | `DEVELOPMENT_MUTATION` | registered origin + registered current branch，明确 non-force refspec；禁用 hooks、mirror、followTags；校验 push URL |

network Git operations 校验 task 中的 origin，拒绝缺失、更改、多 URL 和不同 push URL；
branch/refspec 不由 caller 提供。fetch 使用固定 refspec，不继承任意 remote fetch rules。
旧 `HOST_FILESYSTEM/git_head`、`git_status` 继续兼容。

正常 workflow：先 discovery，再 `status → fetch_origin → remote_main_head → head →
ahead_behind → development_command(开发/测试) → push_current_branch → remote_main_head`。
需要开发和 push 时在 prepare 一次性申请 `READ_ONLY_HOST + DEVELOPMENT_MUTATION` 与
相应 capabilities；不要在执行中扩权，也不要通过另建 execution 绕过 uncertainty。

### systemd 与既有生产 targets

`SYSTEMD_USER` 公开 `service_is_active`、`service_status`、`service_journal`、
`service_restart`，对应每项的 registered targets 和允许 classes。restart 仍需 mutation
class；production mutation 仍需显式 intent。任意 service 和任意 SSH host 均拒绝。

本轮不更改 `bridge.toml`，不部署 Observer。`clinx-observer.service` 注册留给 Observer
continuation；验收仅使用已有 `clinx.service`。`orion-core`、`orion-data` 与 deployment
controller 的已注册 commands 保持原有 class 边界，没有执行生产操作。

## 错误、continuation 与 reconciliation

同 call ID 无论 request ID、connection 或请求内容如何变化，都只能返回既有缓存结果
或确定性拒绝，不再次进入 Host handler。不同 call ID 只有不存在未解决副作用不确定性
时才能执行。未交付的 mutation 会阻止同 execution 内所有后续 Host 调用，因此换新
call ID 包装同一 mutation 不能绕过保护。跨 execution 的业务幂等不在本 contract 范围。

| 执行事实 | certainty / continuation | 是否允许自动重跑原 command |
| --- | --- | --- |
| 明确的 validation rejection | `COMMAND_NOT_DISPATCHED / NOT_EXECUTED / SAFE_TO_CONTINUE`；无 Host evidence | 否；允许新合法 call |
| 非零退出 + 精确 ACK | `COMMAND_EXECUTION_FAILED / EXECUTED / SAFE_TO_CONTINUE` | 否 |
| exit 0 + 精确 ACK | `COMMAND_EXECUTED_RESULT_DELIVERED / EXECUTED / SAFE_TO_CONTINUE` | 否 |
| 已完成但交付未确认 | `EXECUTED_OR_POSSIBLY_EXECUTED / RECONCILIATION_REQUIRED` | 否 |
| dispatch 后完成状态未知 | `COMMAND_DISPATCHED / UNKNOWN / RECONCILIATION_REQUIRED` | 否 |

ledger 的 atomic admission 保留在 handler 前，以保护请求关联与并发去重。此时仅代表
保守的 dispatch reservation，`host_dispatched=null`，不能当作真实 process evidence。
HostExecutor 的验证区间、bridge 的参数解析区间可提供明确的 non-dispatch proof；
它们的拒绝只留下 invocation/rejection ledger，不创建 `host_executions` 行。
未知 handler exception、断连或崩溃则不能仅凭“找不到 Host 行”推断未执行。

public failure response 与 ledger/status 公开 `failure_stage`、`host_dispatched`、
`side_effect_certainty`、`delivery_state`、`continuation_state`、`retry_allowed` 等字段。
正常完成的响应保留 `delivery_state=PENDING`，并声明 `delivery_state_scope=RESPONSE_SNAPSHOT`、
`ack_tracking=CLINX_TRANSPORT_LEDGER`、`execution_can_continue=true`、
`continuation_state=SAFE_TO_CONTINUE`。这只授权 Worker 继续任务，绝不提前声明 DELIVERED。
下一调用的实际 dispatch 仍以持久化 ledger 为准：同 owner 的短暂 ACK 在途先排队，
单 reader 继续接收事件，exact item/completed 更新 ledger 后依次准入，支持同一批次多个调用。
队列有容量上限；ACK 使用已有 request timeout 的有界期限，不 sleep、不阻塞事件接收、不循环等待当前响应。
超时、断连、owner 更换或真正失败仍 fail closed；未知 Host completion 即使 ACK 成功也不放行。
`retry_allowed=false` 和 `retry_scope=ORIGINAL_OPERATION` 禁止重放原调用，不禁止下一合法操作。
ledger 的正常暂态为 `COMMAND_EXECUTED_RESULT_DELIVERY_PENDING`，不提前标记 delivery failure。
命令非零退出、pre-dispatch 拒绝与可靠交付分别记录；Worker 不负责推断 ACK。

| 稳定错误码 | 含义/阶段 |
| --- | --- |
| `OPERATION_NOT_SUPPORTED` | operation 未实现；Python 异常类为 UnsupportedOperation；pre-dispatch |
| `CAPABILITY_UNAVAILABLE` | Host operation executable 不可用；不再用于未知 operation |
| `TARGET_NOT_REGISTERED` | target/origin/branch/project boundary 不匹配；pre-dispatch |
| `AUTHORITY_DENIED` | class/capability/target 超出 sealed authority；pre-dispatch |
| `INVALID_ARGUMENTS` | 缺失、多余、类型或范围错误；pre-dispatch |
| `HOST_TRANSPORT_FAILED` | Host 无确定 completion；旧 Host evidence 的 `TRANSPORT_FAILED` 仍兼容 |
| `COMMAND_FAILED` | 已知非零退出；delivery 必须另行确认 |
| `RESULT_DELIVERY_FAILED_AFTER_EXECUTION` | 已执行、可信结果交付失败；retry_required=false |
| `RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH` | 是否完成未知；retry_required=false |
| `RESULT_DELIVERY_RECONCILIATION_REQUIRED` | 当前 execution 尚有 unresolved invocation，拒绝后续 Host admission |
| `TOOL_CALL_ALREADY_RECORDED` | 原 call ID 已有记录；确定性拒绝 replay，不改变原 execution facts |

不存在新的 workflow/coordinator/governance gate。bridge 不再重复检查 sealed policy
membership；它仅解析调用参数，HostExecutor 负责 authority 与 boundary。Finalizer 和
ledger 使用同一 `requires_reconciliation()`，历史 terminal result 保持不可变。

`development_command` 仍为已有受信任开发入口，使用 argv + shell=False + registered cwd，
没有改成 shell RPC。直接 sudo/su/ssh/scp/rsync/aws/kubectl/terraform/systemctl、shell -c
及其组合参数、git 网络/config/remote 修改和常见 wrapper bypass 都有拒绝回归。
这是受信任项目开发约束，不声称可隔离恶意任意 Python 代码；没有新增通用代码 sandbox。

## 验收与证据边界

所有新真实验收均使用正式 `ClinxMCPServer` tools/call：prepare → start → 专用真实
P620 Codex Provider → dynamic tool → HostExecutor → Provider ACK → completion worker
→ ExecutionFinalizer → SQLite result → get_status 回读。使用独立 TaskRegistry、lease、
临时 Git clone，防止测试污染共享任务；没有直接调用 HostExecutor 代替真实验收入口。

`execution_results.writeback_state` 是可选 Linear audit projection 状态。未配置 Linear
的隔离验收可保留 PENDING；本地 result persistence、terminal convergence 和 lease release
必须分别断言。没有为获得 WRITTEN 而伪造审计或发送外部消息。

`test_host_contract_v2_live.py` 的正常组精确验证九次 invocation：unsupported operation
无 Host 行，随后 working_directory/status/head/fetch_origin/remote_main_head/ahead_behind/
service_is_active/service_status 共八条 exit 0 + DELIVERED。多失败组验证三次拒绝、一个
working_directory Host success。每组只有一个 prepared execution、一个 terminal result，
没有活动 execution 或遗留 lease。持久化记录分别用于核对调用数与零 reconciliation。

`test_tool_delivery_live.py` 在唯一一次 counter mutation exit 0 后注入结果交付失败，
验证 BLOCKED、retry_required=false、counter=1、HOST_EXECUTION_COUNT=1。再打开真实
Provider connection 读取同一 thread；在新 listener 上通过 app-server admission path
注入同 call、新 request ID、新 call ID 的 replay frames，证明 handler 从未再次进入。
这些 replay frames 是确定性的对抗测试，不冒充第二个自主 worker turn。

证据目录：`/data/artifacts/clinx-host-contract-v2-20261001`。其中保留所有失败与通过记录，
包括初次 worktree 缺 Rust binary 的完整测试失败、验收脚本的 Linear projection/active
execution 计数口径错误和 reconnect 初始化遗漏。此前真实 command 的 PASS/BLOCKED 事实
未被改写，也没有重试原 fault mutation。最终机器可读 receipt 关联源码 SHA/tree、全部
测试与真实 execution IDs、latency、remote readback 和 inherited dirty preservation。


## 2026-10-01 最终资格快照

| 验收 | execution_ref | 结果 |
| --- | --- | --- |
| 正常九步 | `exec_a6a36e06272748039401386a17b4363f` | PASS；9 invocations，8 Host commands，8 DELIVERED |
| 三个 pre-dispatch 失败后继续 | `exec_8b6a823f04854b188069944bd2d32579` | PASS；3 拒绝，只有 1 Host command |
| mutation 后交付故障 | `exec_608785e846034e6ea1d4b7bd29415031` | 预期 BLOCKED；count=1，retry_required=false，reconnect/replay protection PASS |
| 与 inherited dirty work 合并后的九步 | `exec_53482e74a4314314a83c42e64956ed4a` | PASS；仅临时集成 checkout，未修改原 dirty files |

正常序列 prepare → first Host：12.240 s。Host process latency（ms）依次为：
working_directory 1、status 32、head 3、fetch_origin 10550、remote_main_head 4976、
ahead_behind 4、service_is_active 8、service_status 16。Provider delivery（Host completion
至 exact ACK）为 2.231–8.924 ms。这是一次真实样本，不是吞吐量或 p99 承诺。
完整每调用 latency 记录在 `qualification.json`。

`RECONCILIATION_COUNT=0`、`UNEXPECTED_BLOCK_COUNT=0`、`NESTED_EXECUTION_COUNT=0`、
`MANUAL_INTERVENTION_COUNT=0`（正常序列）。fault 的 count=1 单独统计，不与正常组混合。

原 canonical checkout 的 12 个其他工作流 dirty files 全部按字节保持；包含三份 Observer
文档和原 bridge.toml。46 条历史 BLOCKED results 的逐记录哈希保持一致。
`PRODUCTION_MUTATION=NONE`、`ORION_TASK_MUTATION=NONE`、`OBSERVER_DEPLOYMENT=NOT_RUN`。

共享 dispatcher/tunnel 仍承载另一条 ORION execution。本任务不为加载新代码重启它们，
也不修改正在使用的 dirty checkout；共享服务激活标记为
`SHARED_RUNTIME_ACTIVATION=NOT_RUN_ACTIVE_ORION_EXECUTION`，不是部署 PASS。
远端 canonical 合入完成后，Observer continuation 应先在该 execution 安全结束后加载
已验证的 Host v2 源码，再进行本任务以外的 Observer 注册/部署。临时集成验收证明两组
代码可以共存，但不代表共享进程已加载新版本。


## 2026-10-01 trusted workspace 路径契约

后续路径扩展见 [TRUSTED_WORKSPACE_PATHS.md](TRUSTED_WORKSPACE_PATHS.md)。
registered cwd 继续作为 task identity / lease anchor；path_read 和 development_command
路径参数按配置 trusted roots 判断。上述历史资格快照保持不变；新的 canonical/runtime
收敛证据见 [CANONICAL_CLOSURE_20261001.md](CANONICAL_CLOSURE_20261001.md)。
