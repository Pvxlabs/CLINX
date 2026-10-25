> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 节点反向请求与 Provider 路由（第七轮）

基准 `c440856`。本轮交付反向 Authority 请求和 Rust 按 host 选择证据端点的实现，
**尚未完成 Mac Provider 跨设备执行、Helper 切换或 COMPAT 退役**。
第六轮 canonical task UUID、exact predecessor 和 adoption 原子提交契约继续有效。

## 已接通的入口

`NodeAuthorityClient` → 既有 `PairedTLS` → `CentreService` → Centre 本机
`RustAuthorityClient` → `business.node_request`。

Mac 不使用 Linux `SO_PEERCRED`。网络身份来自既有 OPAQUE 配对证书和每请求 mTLS
认证；Linux peer credentials 只用于 Centre 到同机 Authority 的最后一段 IPC。
Centre 在 Rust 模式下复用 `Commands.from_env()`，没有新增服务、授权类别或部署流程。
Helper 的默认 factory 尚未切换。

Centre 每次重查配对身份、在线注册、注册指纹、节点 owner scope、注册 scope 和 Centre
现有授权的交集。请求自带 principal/scope 不能替换 Centre 构造的
`node_id/user_scope/fingerprint/projects`。该路径使用现有 `execute_tasks` 授权。

Rust 只接受 `adopt/prepare/start/cancel/get_status/get_prepared/list_tasks/list_projects/read_receipt`。
`observe_commit/input_commit/take_dispatch/ack_outbox` 等 Transport 接口不能通过此路径调用。
节点提交 adoption 意图不会提交 Provider 事实，也不会直接创建 task、lease 或 checkpoint。

Rust `business_node.rs` 在业务事务中校验身份与范围：

- prepared 的 `node_principal` 与请求收据原子提交；start/adoption_commit 将同一归属传递给 task。
- 已有无节点归属的任务不会被网络请求自动认领。
- 幂等键为 node/user/client request ID 的摘要，不包含 payload；同 ID 改变内容必须冲突。
  fingerprint 参与请求摘要与归属检查，换证书不会绕过旧请求冲突或认领旧任务。
- `read_receipt` 只查询同一 node/user 命名空间，再校验指纹和当前 project 范围。
  授权缩窄后不能读回超出范围的旧结果。任务列表也只返回该 principal 的可见任务。
- 丢回执返回 `DELIVERY_UNKNOWN`，客户端保存原 request ID，不自动重发；读回不生成 send permit。
  `NOT_FOUND` 只表示尚无提交收据，不证明 Provider 没有执行。

Node 帧上限仍为 256 KiB，Rust IPC 为 1 MiB。大结果/Provider 证据的跨设备分片尚未实现；
没有截断证据、放大网络上限或把未知结果改成成功。

## 按 host 选择 Provider

Rust policy 新增可选 `provider_routes`，例如：

```json
{
  "endpoints": ["/qualified/p620.sock", "/qualified/air.sock"],
  "provider_routes": {
    "p620": ["/qualified/p620.sock"],
    "air": ["/qualified/air.sock"]
  }
}
```

这是端点选择契约，**上述路径不是已实现的远端网络代理**。配置该表时所有 project/workspace
host 必须有明确路由，端点不可跨 host 重用；没有路由不能退回本机默认 socket。
不配置该表的既有单机 policy 序列化和语义保持兼容。

adoption、dispatch/resume、input recovery、completion observation、successor handoff 和取消
均使用任务 host 的端点。Dispatcher 从 Rust permit 获取选择，不再以全局配置覆盖它。
同一 thread ID 出现在不同 host 时允许独立归属；另一个 host 的同名 thread/turn 证据会被拒绝。

workspace 的 git/repository/native-resolution 查询显式传递 host；production observer 从
同一 policy 表选择端点。使用配置路由时缺失 host 会失败，不回落到 writer 的文件系统。
独立测试的本机 filesystem 模式保留。尚未增加远端动态 workspace 枚举或 Git target 检查。

## 验证与剩余阻塞

证据目录：`/lab/runtime-worktrees/p620/clinx-node-routing7-20261024`。
只使用临时身份、独立端口、资格 Authority 数据库和测试 Provider；没有部署或重启生产服务。

| 范围 | 结果 |
| --- | --- |
| Rust 全套 cargo test --locked --offline | PASS：57 项；包含新增归属/事务/host 证据隔离测试 |
| Rust release Authority 与 MCP relay | PASS |
| 新配对反向入口及原 canonical Node fixture | PASS：27 项 |
| 受影响的原本地 Python 测试 | 70 PASS、1 个既有失败；与精确 c440856 的逐测试结果相同，无新增失败 |
| 复制 Rust package 的请求/routing 测试 | PASS：36 项；所有运行模块可导入，无 COMPAT import |
| 配对 Node→Centre→真实 Rust→测试 Provider | PASS：完成、queued cancel、input 丢 ACK、SIGKILL/reopen、精确恢复、无重放；Linux 回环 |
| 实际 Air→P620 Rust | PASS：独立 OPAQUE+mTLS，经现有 SSH 临时端口转发；adoption 丢回执、重复请求、资格 Authority 重启后读回同一 hash；结果始终 PENDING |
| 完整实际 Mac→Rust→Mac Provider 执行/取消/恢复 | BLOCKED：远端 Provider RPC 与证据返回尚未实现，不能由上两项推出 PASS |
| Rust release staging 与 Helper 源码清单 | PASS：Rust 包无 COMPAT；Monitor 增量包恰好匹配清单，仍保留必要 COMPAT |
| Air Helper 源码包 | PASS：实际 Darwin Python 校验全部 78 个文件 hash 并导入所有模块；未启动 Helper 服务 |
| 完整 Swift/App 签名构建与安装产物 | 未验证；本轮无 Swift 修改、无安装 |

唯一既有失败是
`test_node_runtime.py::test_product_centre_and_air_helper_register_as_separate_processes`：
原测试子进程仍调用根目录 `node_centre_entrypoint.py`。保留原测试和断言，未删除或 skip；
外部重定位副本的结果与基准一致。其他全部本地 Python 测试本轮未全量重跑。

不能删除 `bridge.py/task_registry.py/m9_integration.py` 的具体剩余依赖：

1. `native_provider.existing_client` 仍创建本机 UnixSocketTransport；目前没有以认证 node/user/project
   关联 task/execution/thread/turn 的远端 Provider RPC，也没有远端一次性投递入口。
2. `rust_workspace_probe.observe` 仍核实 Linux worker UID 并在本机 Provider 执行固定 metadata 命令。
   host 选择已完成，Mac Provider 的 peer identity、实际 cwd/repository 证据返回尚未接通。
3. Rust Node 反向接口还未覆盖 Host policy/Git target 和完整 context 调用；`RustIntegration` 的
   native cwd/context 仍有本机 Provider 读取。不能直接将所有 Helper 操作接入已实现子集。
4. Authority 的业务 outbox 与节点 `observation-outbox`、旧 Registry metadata outbox 是不同职责。
   后两者的完整 Rust 提交/投递/ACK 迁移未完成，本轮没有宣称等价替代。
5. `node_service_entrypoint.py` 仍通过 `CanonicalNodeExecutionAdapter.from_config` 构造既有 Mac
   执行路径。上述缺口闭合前删除三个 COMPAT 核心会破坏正常 Helper 执行和恢复。

本轮删除 **0 文件、0 行退役源码**，没有以文件搬迁冒充迁移。新增的 Rust 事务/身份/路由逻辑
及测试属于可独立验证的进展；Python 仍有运行时职责，目标架构尚未达成。
按 Git 跟踪源码、包含 fixtures 和测试、不含被忽略的本地 tests/ 及构建产物统计：

| 语言 | 基准文件/行 | 本轮文件/行 |
| --- | ---: | ---: |
| Rust | 21 / 9,937 | 24 / 10,711 |
| Python | 125 / 39,249 | 126 / 39,410 |
| Swift | 62 / 11,355 | 62 / 11,355 |

`clinx/compat` 分类与 Monitor 打包依赖未虚改为 DEAD；根目录仍无 Python 模块。
