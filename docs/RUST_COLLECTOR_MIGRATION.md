# Rust Collector 队列与恢复迁移

2026-10-25；基线 `3cd582bbc9de555dde08a6f4465999b682c3de10`。
本轮状态：源码迁移与隔离验证 PASS；NOT_DEPLOYED。仓库总体 Rust 迁移继续进行。
目标按最新要求收敛为 Rust 核心、适合现有客户端的前端技术，以及尽可能薄的 Python 兼容层。

## 已交付

`kernel/src/observation_collector.rs` 接管节点 spool 的文件锁、SQLite schema、持久 stream/seq、
双边授权下的离线采集、上传批次/字节预算、scope redaction、ACK 删除、generation 重建、
轮转 inventory/audit、恢复队列公平性、保留上限、去重摘要、覆盖恢复与下一次 tick 间隔决策。
checkpoint、采样去重状态和 outbox 同事务提交。SQLite COMMIT 失败显式回滚，避免下次 tick
复用悬而未决的事务。正常 Helper/Centre 不构造 Python spool 连接。

丢 ACK 或进程在 Centre 提交后退出时，仅重新上传相同 stream/seq/内容的观察批次；现有
Rust ObservationWriter 重读摘要并去重。接受 ACK 前校验整数序号和可选 stream 身份，拒绝
超过实际发送批次的 ACK。未知 Provider 输入或业务任务从未由 Collector 重放。

`kernel/src/observation_source.rs` 接管 native/canonical turn 合并、精确证据附着、覆盖与 active
投影、generation inode 身份及历史兼容 SQL。重复 native turn 保留原来最后一条优先的语义；
重复 canonical turn 继续拒绝，错误从基线 ValueError 明确为 OBSERVATION_IDENTITY_CONFLICT。
Authority callback 错误保持原 RustAuthorityError 类型与错误码，避免掩盖身份冲突。

Python `ObservationCollector`/`CodexObservationSource` 保留调用接口与现有身份/协议回调。
仍保留 live-writer probe/cache、LocalObservationRegistration 身份装配、外层进程 tick 唤醒。
无时区时间戳的少量格式转换继续使用原 Python codec，保留 macOS 本地时区语义；不把它
错误地改成 UTC。`.conn` 仅作为既有测试显式调用的检查/注入接口，正常执行不访问它。
`native_history.py` 中失去调用者的历史 SQL budget helper 与 readonly reexport 已删除。

release/caller/installer/Monitor manifest 增加 `collector_runtime=RUST_COLLECTOR_V1`；staging
和 Mac build 实际执行 `clinx-node --collector-contract`，拒绝旧二进制与新调用端混装。
两种 profile 继续拒绝旧 COMPAT 核心，保留必要 Python 协议客户端。

## 验证

- 10 个 Linux Rust Release binaries 构建通过；Rust 84 项测试通过。
- 仓库资格测试 152 PASS，包括新增 18 项 Collector cases。
- 本地完整旧套件前后均为 295 PASS / 31 FAIL / 104 ERROR / 2 既有 SKIP；432 个身份逐项相同。
- 原 124 个本地测试/support 文件保持 SHA256；现有跟踪测试断言未删减或弱化。
- 独立基线对比 29 向量：28 项公开值/错误码一致；重复 canonical turn 保持拒绝行为，改为明确错误码。
- 新增测试覆盖 singleton、断进程/重启、丢 ACK、异常 ACK、scope 收窄、离线边界、事务回滚、
  backpressure、coverage 恢复和恢复队列公平性。全套仍覆盖既有执行/取消/恢复协议。
- Python E9/F、Rust 新文件格式、源码 import/profile 闭包、Helper shell syntax 与打包测试通过。
- strict Clippy 仍受相同 10 项历史诊断阻塞；无新增诊断。
- macOS 原生 build/signing 与本轮产物物理跨设备闭环未验证；Linux fixture 不代替实机验收。

最初定向测试遗漏 CLINX_TEST_RUST_BIN_DIR，导致 2 项启动环境错误；修正后通过。该失败日志
意外包含进程环境，已移除敏感环境内容；后续测试使用白名单环境。日志没有进入 Git。

## 规模及剩余边界

沿用前轮六语言源码字节口径，包含资格测试，未修改 Linguist。

| 项目 | 前 | 后 |
| --- | ---: | ---: |
| Python 文件 / 行 | 106 / 21,075 | 107 / 21,442 |
| ACTIVE_RUNTIME Python 行 / bytes | 11,714 / 541,459 | 11,699 / 532,708 |
| Rust 文件 / 行 | 53 / 23,207 | 55 / 23,911 |
| Swift 文件 / 行 | 62 / 11,355 | 62 / 11,355 |
| Python 字节占比 | 39.6273% | 39.2538% |

整文件删除 0。运行时 Python diff 删除 291 行、增加 276 行，净减 15 行和 8,751 bytes；
协议回调展开后的格式和新增 368 行资格文件使全仓 Python 行数增加。Rust 新增 704 行。
这些物理计数不代表删除了相同数量的纯业务逻辑，也不代表达到了 25% 目标。

尚需继续处理 Provider 子进程/协议热路径、Node 观察 router、Observer HTTP/native bridge、
Discovery/enrollment 和入口 supervision。开发、构建、资格与维护工具继续保留；仍承担核心
职责的 Python 不改标成工具。完整源码分类以 architecture/python-sources.json 为准。

证据：`/data/artifacts/clinx-collector-rust-20261025/`；机器回执见
[验证回执](cleanup/RUST_COLLECTOR_MIGRATION_RECEIPT.json)。本轮不部署、不重启服务、
不访问生产数据库或真实任务；临时构建与数据库按任务所有权回收，保留日志/测试/源码恢复包。
