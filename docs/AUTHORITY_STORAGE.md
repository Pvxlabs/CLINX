# Owner、authority 与存储边界

`execution_owner.py` 持有业务写入口；MCP、节点和调用方通过既有 Owner transport
发送操作。Owner 缺失时拒绝本地写入，不自行建立第二个 authority。
`kernel/src/bin/clinx-owner-client.rs` 是传输客户端，不持有 DB/WAL/SHM。

`canonical_authority.py` 的 resource/epoch/operation identity、`authority_mapping.py`
的 canonical/runtime 映射与 `authority_transactions.py` 的同连接事务共同约束写入。
业务 mutation、commit receipt、outbox 在同一 SQLite 事务提交；delivery 与 commit
分开记录。重复操作返回原回执，旧 epoch 和错误 owner 被拒绝，commit 后丢 ACK
保留可读回事实，不能据此重放 Provider turn。

`runtime_control/store.py` 和 `runtime_control/transport.py` 保留 runtime allocation、
CAS、事件与恢复状态。`authority_outbox.py` 使用稳定 identity 和接收端回执处理
重复投递；跨数据库或外部系统的 ACK 不等于全局事务或业务 exactly-once。
结果、终态、租约释放须经原有 finalizer，并保留未知状态。

SQLite 约束和同 UID 的只读 worker 连接不提供 OS 级物理写隔离。持有原始可写 FD
的进程仍可能绕过 SQL，生产隔离能力必须独立验证。旧版本源码或切换前备份不能
覆盖已经提交的新 authority 事实。

对应回归是 `test_execution_owner.py`、`test_owner_callers.py`、
`test_owner_authority_transport.py`、`test_owner_business_commit.py`、
`test_canonical_authority.py`、`test_authority_closure.py`、
`test_runtime_authority_transport.py` 和 `test_prepare_contract_idempotency.py`。
用[测试指南](TESTING.md)中的 `owner` 入口运行；测试使用隔离 SQLite/socket、
真实 Rust client 和脚本化 Provider。原 schema 兼容性另需 schema-only JSON，
不得将生产数据库当作测试输入。
