# ADR-006: CLINX Monitor reads canonical persisted facts through a separate observer

Status: Accepted for Phase 0/1 implementation; deployment and Mac acceptance pending.
Date: 2026-09-30. Related: ADR-001/002/003, docs/CLINX_MONITOR.md.

## Context

Phase 0/1 originally treated P620 as the only CLINX authority. The 2026-10-04 addendum below supersedes that single-host assumption. A native Mac MenuBarExtra needs small, stable,
read-only snapshots over Tailscale. Existing TaskRegistry construction can migrate
the DB; ClinxIntegration.get_status can reclaim leases/reconcile an exact provider
turn. Host evidence and structured results contain fields unsafe for a UI transport.
The optional shadow ledger is not complete historical coverage.

## Decision

Implement observer_server.py as a separate serialization adapter over the same SQLite
registry, using mode=ro, query_only and one bounded transaction per request. Do not
call TaskRegistry initialization, MCP, dispatcher, provider transport, reconciliation
or host operations. There is no new authority table, event writer, state store or
migration. Existing execution semantics remain untouched.

Initially expose four authenticated GET route shapes, loopback only; use private
Tailscale Serve HTTPS at deployment. All authenticated devices joined to the trusted
Tailnet may reach the private endpoint under the existing effective Tailnet policy.
Tailnet membership is the network trust boundary; reaching the endpoint does not
authorize Observer reads. No verified Tailscale peer identity adapter currently
exists in the stack, so require an independent, revocable Observer bearer credential
for every GET, including health. Never trust client/forwarded identity headers.
Mac uses Keychain. Do not use Funnel or expose the listener publicly. Do not broaden
the Tailnet policy solely for this Observer.

Use exact execution/task/turn attribution for results and execution-owned metadata;
unknown remains null/UNKNOWN. Serialize only a positive allowlist. Progress is null
until a persisted real denominator exists. Empty phases/artifacts explicitly mean
no canonical evidence source. Existing optional V1 event observations may be read
with PARTIAL/UNAVAILABLE coverage; never synthesize missing history.

Native Mac uses URLSession async/await polling (2s active / 15s idle), state/freshness
separation, bounded responses, no redirects, and no mutation capability.

## Consequences and alternatives

Direct MCP get_status reuse is rejected because it can mutate authority despite its
status-oriented name. Adding observer handlers to the running bridge is rejected
because it couples deployment to active work. Copying the registry or maintaining a
Mac state machine as authority is rejected because results can drift. Reading logs
or terminal content is rejected because it is not canonical structured evidence.

A separate read-only SQL adapter depends on the existing table schema. Future
authority migrations must run observer contract tests; source incompatibility fails
closed with 503. Read snapshots do not certify process liveness. Event history may
be partial; global cursor reset cannot fully detect database replacement. Offset
pagination is eventually refreshed, not a multi-request snapshot. Free text cannot
provide universal secret detection and is restricted to authenticated viewers.

OS-level DB write denial, TLS/ACL checks, bearer lifecycle drills, Mac compilation,
UI/energy testing and notifications are separate acceptance gates. This ADR grants
no permission to restart CLINX, operate ORION, expose a service, or perform task
Start/Cancel/Retry/Approve/Deploy.


## 2026-10-02 addendum — Activity v1

The authorized Activity extension adds GET /v1/tasks/{task_ref}/activity with a required
execution_ref and an optional after OR before cursor. See monitor/ACTIVITY_V1_SPEC.md.
Only exact execution-owned thread/turn bindings and P620 Codex native paginated
public agentMessage records are eligible. This is a separate display read model,
not authority/liveness evidence. No provider calls, registry initialization, history
writes, internal reasoning or raw tool output are added. The standalone Observer
reads the native state/history databases through read-only OS mounts and bounded
SQLite transactions. Existing lifecycle/result serialization remains unchanged.


## 2026-10-04 增补：全网观察与持久记录 v1

执行事实归各自 canonical owner；中心是可重建观察读模型，不是第二执行权威。旧决策中“无新增 state store / writer”仅适用于原 canonical GET 适配器，不禁止本版本正常观察投影、增量事件和采集 checkpoint 的必要记录。

节点复用 NodeIdentity、TrustedPeerStore、OPAQUE/mTLS 与双方持久化 read_sessions 范围自动枚举。中心本机需显式本地观察批准，复用自身身份，不自配对、不创建替代身份。配对不等于授权；观察不要求 execute_tasks 或 adoption。

observation_id 绑定节点、用户、Provider、native thread；canonical 引用可空。可信 route 合并轮次，外机 owner 不归前端，同 thread 多来源冲突明确展示。目录、轮次与允许字段 Activity 有界持久化；断线 outbox/ACK/source generation/gap/保留状态可观察。内部推理、凭据和原始工具输出不入中心。

Observer v2 与 MCP 共享 ObservationDirectory 和当前授权检查；只读存储视图不初始化身份、数据库或锁文件，不调用 get_status/reconciliation。v1 API 保持兼容；Mac 默认全部设备，使用独立 Observation 模型；控制仍走既有授权与单写者。

本决策只交付源码与隔离资格证据，不授予运行时激活或控制权限。详见 docs/NETWORK_OBSERVATION_V1_REPORT_20261004.md 与 docs/NETWORK_OBSERVATION_V1_ACCEPTANCE_20261004.md。
