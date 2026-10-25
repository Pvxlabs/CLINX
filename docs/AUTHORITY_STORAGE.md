# Authority 与存储边界

用途：定义当前唯一业务 writer 和持久化恢复责任。适用范围：Rust Authority 与调用方。
状态：CURRENT。验证：[Authority 回归](../kernel/tests/authority_store.rs)、
[业务回归](../kernel/tests/business.rs)及[完整测试指南](TESTING.md)。

当前实现是 [clinx-runtime](../kernel/src/bin/clinx-runtime.rs) 直接持有
[Store](../kernel/src/authority_store.rs) 的 SQLite 连接；`business` 通过该连接提交业务事务。
状态目录包含 `authority.sqlite3`、SQLite WAL/SHM 和 writer lock；初始化与已有库打开
明确区分。生产模式要求独立的非 root Authority、caller 和 Transport principal 及受控 policy。

协议客户端只通过现有 Authority IPC 发送操作，不因 Owner 不可达而新建数据库或 fallback。
`clinx-owner-client` 是保留的开发/兼容客户端，不是另一个 Owner；Python Owner 已不在当前源码。
Node Registry、设备 trust、Collector 与观察库只拥有各自领域数据，不能提交 Task 业务状态。

业务 mutation、command receipt、事件/projection/outbox 在已有事务边界提交；
相同 identity/hash 返回原回执，冲突、过期 epoch 与错误 owner 被拒绝。
发送意图、delivery ACK 和 commit 是不同事实；提交后丢 ACK 应读回，不重放 Provider turn。
取消、终态提交、资源释放语义以[当前契约](CONTRACTS.md)及冻结 core 为准。

同 UID 的逻辑只读或 SQLite CAS 不构成 OS 物理隔离。已持有 DB/WAL/SHM 可写 FD
的旧进程必须在单独授权的部署中实际撤权并证明；本次源码检查不声明已完成生产撤权。
回退不能用旧数据库或快照覆盖新的已提交事实。历史部署和容量修复记录保留在
[Rust Authority 报告](RUST_AUTHORITY.md)，不从其中推导当前进程状态。

## 全局路由增配

`business_config` 的受控扩展由 Rust Store 离线 writer 完成，策略与回执原子提交到原
业务哈希链，不能使用第二 writer 或手工 SQL。只追加既有主机语义不变的新路由，
要求精确身份/CAS、零活动执行和零 ACTIVE lease。见 [ADR-008](architecture/ADR-008-additive-device-routes.md)。
