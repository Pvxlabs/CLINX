# Rust canonical handoff：身份与 predecessor 事务

2026-10-24，基于 main `30d5a5f390c17a94ae0d4b2a022408fa3e6871b0`。
本轮范围是剩余 Mac 业务核心的迁移，未重复整理目录。

**已交付：Rust canonical adoption/checkpoint 契约及现有 Node 入口接线。**
**尚未交付：Mac Helper → 远端 Rust Authority → Mac Provider 的生产可用闭环。**
Helper 默认路径未切换，COMPAT 实现未删除；不能把下面的 Linux 隔离资格验证当成 Mac 接管。

## 已实现契约

现有 Node `execution.adopt` 接受 `canonical_identity` 与 `exact_turn_id`。
identity 沿用既有字段：`node_id/provider/thread_id/user_scope/task_id/host/origin/first_provenance_json/title/cwd`。
NodeService 从已认证的本机 record/scope 生成 `_canonical_owner`，覆盖请求中的同名内容；
RustNodeExecutionAdapter 校验 owner、user、fingerprint、配置项目及显式选择器。
这里没有创建 Python 业务 Registry、Dispatcher、M9 或完成线程。

Rust `adopt` 的可选 `canonical_identity` 分支负责最终身份一致性：

- task ID 必须等于原 Python 契约的 `task_` + UUIDv5(URL namespace,
  compact JSON `[node_id, "codex_app_server", thread_id]`)；不重新分配或改名。
- host、thread、cwd、provenance node、provider、origin 和精确 predecessor 必须一致。
  已存在的不同 task 身份、待提交 envelope 或 predecessor 冲突均拒绝，不能覆盖既有 task。
- API 只能写入 PENDING adoption intent。envelope/provenance 不是 Provider 证据，
  API 不能调用 `adoption_commit`；没有 task、执行、lease 或 checkpoint 因声明成功而创建。
- Transport 使用现有 `adoption_begin` nonce，读取每个配置 Provider 的 `thread/read`
  以及 `thread/turns/list(itemsView=full)` 中**精确 ID**的 terminal turn。
  Rust 检查 nonce、endpoint 集合、idle thread、完整 turn 一致性和 canonical cwd，
  然后在现有同一事务中写 task、adoption receipt 与 native predecessor checkpoint。
- 初次 `canonical_handoff` 不随后续执行 checkpoint 更新。丢失 adoption ACK 后，
  重复请求仍返回原 task；API 侧读回不要求 Provider 在线。不是“重新观察最近 turn”。
- 现有 `successor_begin/take/commit` 直接消费该 checkpoint。重新创建 successor 的
  permit 仍是一次性的；未知创建 ACK 不允许重发或选择一个最近出现的 thread。

canonical `prepare` 必须看到 ADOPTED；PENDING 时返回 `CANONICAL_ADOPTION_PENDING`，
不生成执行。使用既有 `execution.adopt` 读回提交结果；失败的 Node prepare request ID
保留原结果，完成 adoption 后使用新的 preparation request ID。已接受的 start 仍使用其
原 prepared/execution ID，不能用新请求绕过未知发送结果。

不带 canonical identity 的现有 adoption 请求及已有完成、取消、input ACK/readback、
持久化和并行执行语义保持原契约。本轮未新增审批或放宽身份边界。

## 验证与边界

证据目录：`/lab/runtime-worktrees/p620/clinx-compat-retirement6-20261024`。
所有新状态、Git fixture、配对身份及 Provider fixture 都属于任务临时目录。

| 项目 | 结果 |
| --- | --- |
| Rust release | PASS：locked/offline 构建 Authority 与 relay |
| Rust lib / binary / integration | PASS：21 个 lib、4 个 runtime-lab binary、29 个 integration（包含 4 个新增 canonical 回归） |
| Node 边界 | PASS：23 项，包含原 10 项；覆盖 owner/selector 冲突、PENDING 不得执行、固定身份与无 Provider 的 adoption 读回 |
| 原本地受影响测试 | PASS：47 项，原文件未改；当前与基准逐节点结果一致 |
| 复制的 Rust package | PASS：全部 profile import/catalog 无 COMPAT，36 项 request/routing 测试 |
| 复制的 Monitor 源码 package | PASS：导入闭包及增量残留检查；仍明确包含必要 COMPAT，没有声称已退役 |
| 候选 release manifest | PASS：刷新源码、binary hashes、旧 control 排除、native 依赖保留及 base drift 拒绝；仅临时 fixture，未选择生产 release |
| 新链路资格 | PASS：原 NodeService + Rust adapter + 真实 Rust 进程 + scripted Provider；adoption 丢 ACK、fixture SIGKILL/reopen、精确 checkpoint、重复 start、完成、queued cancel、input 丢 ACK 后读取恢复；总 input 4 次，无 replay，无 COMPAT import |
| 现有 mTLS 入口 | PASS：Linux 回环上使用真实 OPAQUE 配对和现有 PairedTLS/NodeRPCServer 运行 adoption；伪造 `_canonical_owner` 被本机认证快照覆盖 |
| Mac 跨设备闭环 | BLOCKED：上述 mTLS 验证使用逻辑 air 节点，所有进程仍在 Linux；不是 Mac→P620→Mac Provider 资格 |
| Mac App / GUI / 真实业务 | 未验证；没有安装、部署或运行真实任务 |

开始时 47 项受影响测试为 45 PASS / 2 FAIL，基准与当前均因 `PRODUCTION_PAKE_UNAVAILABLE`
失败。使用仓库声明的 maturin 1.9.6 和 native/opaque_pairing 锁文件，在任务环境构建并
安装 OPAQUE wheel 后，基准与当前均为 47 PASS。未删除测试、增加 skip 或改变断言。
第五轮的其他全量历史失败本轮未重跑，不声称全量测试转绿。

新测试开发中的两个失败也保留日志：并行 Git fork 短暂继承 fixture 的 writer-lock FD，
由测试在关闭旧 Store 后限时等待解决，生产 fence 未改；新增并发测试最初预期
TASK_STILL_HELD，而现有 lease 更早返回 RESOURCE_STILL_HELD，校正为实际既有错误码后
仍断言第二执行被拒绝。已有测试的断言保持不变。

可在源码运行新增门禁：

```sh
cargo test --locked --offline --manifest-path kernel/Cargo.toml --lib --tests --target-dir <task-build>
python -B -m pytest -p no:cacheprovider -o pythonpath=. fixtures/test_rust_node_contract.py
```

本地保留 suite 使用第五轮 runner，不提供旧模块 shim。

## 为什么尚不能切换 Helper 或删除 COMPAT

这些是当前源码中的缺失契约，不是另加部署审批：

1. **反向 Authority 请求入口不存在。** `CentreService.handle` 只接受注册、心跳和观察上传。
   现有成对 mTLS 执行流是 Centre→Node，未实现 Node→Centre 的 Authority 请求转接及
   每请求绑定的 node/user/project 身份。不能把 Linux `SO_PEERCRED` 当成跨设备身份。
2. **Provider evidence 没有按节点路由。** Rust Policy 只有一个全局绝对路径 endpoint 集合，
   RustDispatcher 使用本机 Desktop socket。缺少以已认证节点、exact task/execution/turn
   绑定的远端 Provider command/observation 路径。观察投影或节点 `success=true` 不能代替它。
3. **workspace observer 也是单 endpoint。** `business_workspace::OBSERVER` 是全局
   `(program, endpoint)`，现有读取没有 host 参数。让其验证 Mac cwd 会使用错误机器的
   Provider/文件系统；必须与节点路由及持久化 repository identity 一起迁移。
4. **现有 Mac 执行状态未导入。** canonical adoption 只处理精确 terminal predecessor，
   不导入旧 Python 的 active execution、lease、completion/result ingestion、cancel、
   未确认投递或 metadata outbox。不能用接管一个 native thread 的证明释放这些旧状态。

因此 `clinx/compat/bridge.py`、`task_registry.py`、`m9_integration.py` 及其 completion、
prepared request、恢复和 outbox 依赖仍有运行职责。本轮删除文件数为 **0**；三份核心仍为
**12,394 行**。发布清单更新了新契约的分类说明，但没有假删 Monitor 所需依赖。
Rust release 继续不携带 COMPAT；Mac 包彻底排除旧核心这一验收项仍为 BLOCKED。

部署状态：NOT_DEPLOYED。只读核对 Authority、Dispatcher、Centre、Observer、Tunnel
的 PID、NRestarts、ExecStart 仍与第五轮相同；生产数据库及运行数据没有改动。
