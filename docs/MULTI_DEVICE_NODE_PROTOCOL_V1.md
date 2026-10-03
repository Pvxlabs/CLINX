# CLINX 多设备节点协议 v1

状态：源码实现已加入 P620，Mac 原生安装、跨机注册和真实 Codex Provider 读回仍需可达设备验收。

## 边界

`node_protocol.py` 是中心索引／路由与节点服务的 provider-neutral 层。它复用现有 `local_discovery` 的稳定 `node_id`、公钥信任和 mTLS；配对本身只证明设备身份，不授予会话读取或任务执行。当前已接入的 Provider 是 `codex_app_server`，其他 Agent 仍为 `NOT_IMPLEMENTED`。

中心只保存节点身份、用户共享范围、能力／版本／新鲜度和精确 thread 归属索引。原生正文留在持有历史的设备上，按需分页读取；不会复制 `.codex`，不会同步运行中的 SQLite，也不会在读请求中创建 Task、启动 Provider、发送 turn、取消执行或取得写租约。

## 身份与授权

`NodeRecord.node_id` 是长期身份，`route_ids`、`display_name` 和 `endpoint` 只是可变路由提示。已有公钥换绑另一 `node_id` 返回 `DUPLICATE_NODE_IDENTITY`；同一 `node_id` 出现新公钥返回 `NODE_IDENTITY_CONFLICT`。名称变化、地址变化和睡眠恢复只更新登记状态，不重置密钥。

首次共享写入 `SharingScope`，范围至少包含 `user_scope`、`read_sessions`、`execute_tasks` 和 Provider 白名单。撤销会将节点置为 `REVOKED`，后续读／执行均返回 `SHARING_SCOPE_DENIED`。节点、用户、Provider、协议版本和目标节点均在节点侧再次核验。

## 会话读取与覆盖范围

`NodeRouter.read` 支持：

* 指定 `node_id` 或 host route 时只访问该可信节点，目标不匹配返回 `UNKNOWN_NODE`、`UNKNOWN_THREAD_HOST` 或 `NODE_TARGET_MISMATCH`。
* 没有来源时先使用中心精确索引；未命中再对已授权节点做有界并发的精确 ID 请求。唯一命中才绑定来源，重复命中返回 `THREAD_IDENTITY_CONFLICT`。
* 节点离线、超时、未授权、版本不兼容或索引未就绪时保留 `coverage.nodes_unavailable`，返回 `THREAD_SEARCH_INCOMPLETE`，不伪装成全局 `THREAD_NOT_FOUND`。完整 miss 才返回 `absence_scope=AUTHORIZED_NODE_SET`。

每个结果带 `source_node_id`、`source_observed_at`、`coverage`、`read_only=true`。`node_reachable`、`provider_reachable`、`history_readable` 和 `execution_active` 是独立事实。`NodeRecord.stale` 由 `last_seen` 和 `stale_after_seconds` 计算，缓存不得冒充实时。

上下文 cursor 由 `node_id/user_scope/provider/native_thread_id/source/source_version/offset` 组成，可选中心密钥 HMAC；换设备、用户、Provider、thread、source 或版本都会得到 `INVALID_CONTEXT_CURSOR`。因此不会跨节点串页。

## 执行与交接

`execution.prepare/start/status/cancel` 只能通过明确的 `node_id` 调用。`prepare` 和 `start` 由现有 canonical Task／Execution／policy 适配器提供回调，节点层不另造 registry 或权限位。`request_id` 写入节点 request ledger：相同请求返回同一结果，换 payload 返回 `IDEMPOTENCY_KEY_REUSE`。每个 native thread 只有一个 `route_fence` owner；旧 owner 或并发 writer 返回 `SINGLE_WRITER_BUSY`。

执行请求没有确认时返回 `operation_state=UNKNOWN`、`side_effect=UNKNOWN`、`retry=RECONCILIATION_REQUIRED`，中心不自动换节点重放。跨节点交接只传 owner、上下文引用和成果引用，不复制活跃 Provider 进程。

## 传输与服务生命周期

`NodeRPCServer`/`NodeRPCClient` 使用单帧 JSON-RPC；生产构造必须传现有 mTLS `SSLContext`，明文只允许显式 `127.0.0.1` 测试模式。服务不监听公网、不接受地址自报、不绕过 TLS。Mac 应由 App helper 的 LaunchAgent 管理当前用户服务：关闭窗口不停止服务，退出 App 与停用服务分开；崩溃、登录、网络变化和睡眠恢复由 helper 以退避方式重连，并以 pid／socket 锁避免重复进程。本仓库本轮只交付协议与 P620 隔离验证，未宣称 Mac 制品已安装或启用。

实现依据 macOS `launchd.plist(5)` 的用户域 `gui/<uid>` `RunAtLoad`、`KeepAlive.NetworkState`、`ThrottleInterval` 和 `bootstrap/bootout` 语义；没有使用 root daemon 或未记录的 launchd API。Codex Provider 继续沿用仓库 `bridge.toml` 的 `codex app-server proxy` 与 `client_version=0.152.1`，适配器能力以现有 `provider_adapters.CodexProviderAdapter` 实测结果为准；本协议没有声明 Codex 支持 takeover、force resume 或新的动态工具契约。

## MCP 入口

默认工具目录保持兼容。受管服务设置 `CLINX_ENABLE_NODE_ROUTER=1` 后注入 `BridgeConfig.node_router`，MCP 额外暴露只读 `clinx_list_nodes` 和 `clinx_get_node_status`；`clinx_get_context`／`clinx_get_status` 接受 `node_id` 与现有 `hostId`，直接进入同一精确路由器。客户端需要重新执行 `tools/list` 才能看到新增工具；未刷新客户端不能宣称可调用。

## 验证边界

`test_node_protocol.py` 覆盖身份冲突、撤销、cursor 隔离、有界未知来源查找、重复归属、离线覆盖、读路径零执行副作用、单 writer、请求幂等及真实独立进程 RPC。P620 的通过只表示协议和隔离多进程闭环；Air/iMac 的安装、mTLS 注册、重连、原生 SwiftUI 窄窗口截图及真实 Codex thread 读回在设备可达前保持 `BLOCKED`／`NOT_RUN`。
