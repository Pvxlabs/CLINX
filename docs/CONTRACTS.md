# 当前接口与执行契约

用途：当前 Rust 接口、状态权威和恢复语义的统一入口。适用范围：当前源码；旧 Python
实现路径和历史验收不作为现行入口。状态：CURRENT。验证：下表链接的源码、独立 fixture
及 Rust 回归；部署后的外部工具目录和真实设备行为须另外读回。

## 契约地图

运维权限契约见[固定免密入口](OPERATIONS.md#p620-固定免密运维入口)，实现是
[admin_tools](../kernel/src/admin_tools.rs)，回归是[admin_tools tests](../kernel/tests/admin_tools.rs)。
它不接受业务 SQL，不成为 Task writer；策略变更仍使用既有 Rust CAS 与原 operation receipt。
Node/配对 TLS worker 通过 [platform](../kernel/src/platform.rs) 将 accepted socket 显式转为
blocking 后使用原超时；Darwin 的标志继承差异不改变 mTLS/OPAQUE、ACK 或恢复语义。
本地 Provider 使用已连接 socket 的内核对端 UID：Linux `SO_PEERCRED`，macOS
`getpeereid`。配置可使用 Codex 的稳定 socket alias；不从 alias 文件类型推断对端身份，
不接受远程 Node 自报 UID。预期 UID 不匹配时在 WebSocket initialize 前拒绝。
Provider IO 的绝对 deadline 保持不变；非阻塞读写配合 poll，对端 HUP 后仍读取缓冲数据，
只有实际 EOF/IO 错误或耗尽期限才报告发送结果未知，不因此重发请求。
输入恢复必须完整读取分页并唯一匹配原 `clientId`、prompt、thread 和 turn。历史扫描最多
1024 页、累计 16 MiB，重复/无效 cursor 或未读到末页均拒绝确认；不能找到第一个匹配后
提前承认 ACK。原 Authority nonce/boot/deadline 校验保持不变，未知发送不得重放。
Collector 的实时采样优先级只取最新 turn，旧未完成记录继续保留为历史证据。Monitor 的
Active 仍要求精确实时证据及未过期的新鲜度；历史 `RUNNING` 不得被直接展示为当前运行徽标。

| 边界 | 权威定义与实现 | 可重复回归 |
| --- | --- | --- |
| Task / Execution / Attempt / lease | [冻结状态契约](architecture/EXECUTION_CORE_STATE_CONTRACT_V1.md)、[golden fixture](../fixtures/execution_core_v1.json)、[execution_core](../kernel/src/execution_core.rs)、[business](../kernel/src/business.rs) | [执行核心](../kernel/tests/execution_core_qualification.rs)、[业务事务](../kernel/tests/business.rs) |
| Authority / 持久化 / IPC | [存储边界](AUTHORITY_STORAGE.md)、[authority_store](../kernel/src/authority_store.rs)、[runtime](../kernel/src/bin/clinx-runtime.rs)、[UnixAuthority](../kernel/src/native_transport.rs) | [Authority](../kernel/tests/authority_store.rs)、[Native Transport](../kernel/tests/native_transport_qualification.rs) |
| MCP | [嵌入目录](../clinx/mcp/registry.json)、[frame/profile](../kernel/src/mcp.rs)、[API 映射](../kernel/src/mcp_api.rs) | [MCP](../kernel/tests/mcp_retirement_qualification.rs)、[分页/上下文](../kernel/tests/workspace_routes.rs) |
| Provider / Codex | [模型选择与诊断](CODEX_MODEL_SELECTION.md)、[协议](../kernel/src/provider.rs)、[客户端](../kernel/src/provider_client.rs)、[精确历史](../kernel/src/native_history.rs) | [Provider](../kernel/tests/provider_runtime_qualification.rs)、[客户端](../kernel/tests/provider_client_qualification.rs) |
| Host / 工具调用 | [host_executor](../kernel/src/host_executor.rs)、[host_transport](../kernel/src/host_transport.rs)、[兼容 fixture](../fixtures/host_execution_contract_v2.json) | [Host](../kernel/tests/host_runtime_qualification.rs) |
| Node / Centre / Helper | [API](../kernel/src/node_api.rs)、[网络](../kernel/src/node_network.rs)、[scope](../kernel/src/node_scope.rs)、[gateway](../kernel/src/node_gateway.rs) | [Node 网络](../kernel/tests/node_network_qualification.rs)、[请求](../kernel/tests/node_requests.rs) |
| Discovery / 设备认证 | [enrollment](../kernel/src/discovery_enrollment.rs)、[TLS](../kernel/src/node_tls.rs)、[OPAQUE](../native/opaque_pairing/src/lib.rs) | [Discovery](../kernel/tests/discovery_qualification.rs)、[设备](../kernel/tests/device_python_retirement_qualification.rs) |
| Transport / relay / Tunnel | [native_transport](../kernel/src/native_transport.rs)、[relay](../kernel/src/bin/clinx-mcp-relay.rs)、[supervisor](../kernel/src/tunnel_supervisor.rs) | [relay](../kernel/tests/mcp_relay_retirement.rs)、[Tunnel](../kernel/tests/tunnel_runtime.rs) |
| Collector / Observer / UI | [Collector](../kernel/src/observation_collector.rs)、[HTTP](../kernel/src/observer_http.rs)、[Observer schema](monitor/observer-v1.schema.json)、[Activity](monitor/ACTIVITY_V1_SPEC.md) | [Collector](../kernel/tests/collector_qualification.rs)、[Observer](../kernel/tests/observer_http_qualification.rs) |

## 身份、执行流与 ACK

Task 为长期任务身份；Execution 为一次有界执行。ConversationBinding、Provider thread/turn、
Node/user/project/request、lease epoch 和 generation 必须按既有契约精确关联。
公开调用使用 opaque reference，内部 identity 不能被 UI 的名称、最新结果或缓存观察替代。

MCP 的 `clinx_prepare_execution` → `clinx_start_execution` 由当前目录声明输入 schema，
Authority 封存请求、去重并生成 Execution。Transport 从 Authority 领取原 dispatch、
记录发送意图，Provider 返回 exact input/turn evidence 后才提交确认。
普通 native forwarding 使用[原生访问配置](NATIVE_FULL_ACCESS.md)；Host capability
路径是独立内部契约，不可重新加到普通用户任务作为第二业务审批层。

Provider completed、Host exit 0、网络 ACK、Execution terminal 和业务验收是不同事实。
终态结果经 Authority 提交，receipt/outbox/lease 由同一权威协调；观察库、Node Registry
与 Collector checkpoint 是各自数据领域的持久化，不是第二套 Task 状态权威。

## 错误、幂等与恢复

错误码以源码返回值为唯一名称；MCP 只输出经清理的公共 code，不泄露上游正文或凭据。
下表是处理规则，不是完整错误枚举；完整边界见 `mcp::tool_error` 和各入口解析器。

| 典型 code / verdict | 语义及调用方动作 |
| --- | --- |
| `DUPLICATE` | 同一 scoped identity 与语义请求返回原 receipt；不得执行第二次副作用 |
| `HOLD_UNKNOWN` / `RECOVERY_REQUIRED` | 证据不足，保留原 Execution 和 ownership，按原身份恢复观察 |
| `DELIVERY_UNKNOWN` / `AUTHORITY_DELIVERY_UNKNOWN` | Authority 发送/ACK 不确定，读取原提交/状态，不重发新任务 |
| `NATIVE_CREATE_ACK_UNKNOWN` | 原生创建结果未知，读回原 identity，不能重新创建 |
| `THREAD_UNBOUND` | 可能已确认原生会话存在但未纳管；只读查询不得自动 adopt/start |
| `INVALID_TASK_CURSOR` / `INVALID_CURSOR` | 分页参数不合法，不以空页伪装成功 |
| `RESPONSE_CAPACITY_EXCEEDED` | 响应容量边界，不能截断终态证据后宣称完整；使用现有分页/精确读取 |
| `NATIVE_WRITER_UNPROVEN` | 原生 writer 身份无法证明（包括平台设备号不可表示），不得截断身份或绕过 fence |
| `OBSERVATION_READ_ONLY` | 观察入口不授予业务 mutation，不能回退到本地写库 |

取消先记录意图；interrupt ACK 不直接证明终态或允许释放未知副作用。
断线、超时、lease 到期都不证明未派发；只在既有契约明确证明未发送且旧 writer 已 fenced
时走对应恢复路径。不要通过 epoch、chmod 或替换 inode 声称撤销已持有的可写 FD。

## 协议与兼容边界

Authority 使用 `CLINX_RUST_AUTHORITY_V1`，core 事件使用 `CLINX_EXECUTION_CORE_V1` / `1.0.0`。
冻结转换表不等于所有状态均已在每个设备路径验收。旧 `CLINX_KERNEL_V1` evaluator 的
`NON_AUTHORITATIVE` 常量不代表当前 `clinx-runtime` 的 Authority 角色。
IPC 本地 Unix socket 检查 path/peer UID，frame 有界；远程身份走配对 mTLS，不能把 Linux UID
当成远端 Mac 的认证。MCP stdio 暴露不等于自动建立经认证的外网入口。

修改接口时保留 version/schema、错误码、幂等 scope、ACK 不确定和原子提交语义；
不兼容变更先补 golden cases、消费者验证及 ADR。旧文档的生产/迁移计数属于历史，
当前命令和安装前提以[配置与运维](OPERATIONS.md)为准。

## 离线策略维护契约

`CLINX_ADDITIVE_POLICY_V1` 只通过显式维护 CLI 使用，未加入公开 MCP 或运行 IPC。
`POLICY_EXTENDED_NOT_ACTIVATED` 表示策略及回执已持久化，不表示服务已选择它。
`POLICY_CAS_CONFLICT`、`POLICY_AUTHORITY_CONFLICT`、`POLICY_EXISTING_ROUTE_CHANGED`
和 `POLICY_ACTIVE_EXECUTIONS_OR_LEASES` 都拒绝变更；未知 ACK 按原 operation ID 读取。
限制、幂等及后续激活责任见 [ADR-008](architecture/ADR-008-additive-device-routes.md)。
