# CLINX 文档

使用入口是[项目 README](../README.md)，开发环境见[贡献指南](../CONTRIBUTING.md)，
测试命令与前置条件见[测试指南](TESTING.md)；当前测试位于 `kernel/tests/`，本地 ignored `tests/` 仅为历史归档。

## 产品定位

[产品定位与职责边界](PRODUCT_POSITIONING.md)定义 CLINX 的核心使命、职责范围、
Rust Core 唯一状态权威、五条设计原则与效果指标。CLINX 是 AI 任务路由与执行流通
基础设施；代码工作、业务决策及业务安全策略由对应上下游负责。

## 运行契约

| 内容 | 文档 |
| --- | --- |
| 原生任务与权限 | [任务转发](NATIVE_TASK_FORWARDING.md)、[Desktop 执行](DESKTOP_NATIVE_EXECUTION.md)、[完整访问](NATIVE_FULL_ACCESS.md)、[执行权威](EXECUTION_AUTHORITY.md) |
| 身份、路由与工作区 | [Thread 身份](THREAD_IDENTITY.md)、[精确读取](EXACT_NATIVE_READ_ROUTING.md)、[可信路径](TRUSTED_WORKSPACE_PATHS.md)、[工作区执行](NATIVE_WORKSPACE_EXECUTION.md) |
| 生命周期与存储 | [统一生命周期](UNIFIED_TASK_LIFECYCLE_V1.md)、[控制接口](UNIFIED_TASK_LIFECYCLE_V1_CONTROL.md)、[Execution Owner](EXECUTION_OWNER_R1.md)、[存储与 authority](AUTHORITY_STORAGE.md)、[活性](EXECUTION_LIVENESS_V2.md) |
| 咨询与客户端 | [ChatGPT 咨询](CHATGPT_CONSULTATIONS.md)、[Monitor](CLINX_MONITOR.md)、[Monitor 构建](../MonitorApp/README.md) |
| 发现、配对与观察 | [Local Discovery](LOCAL_DISCOVERY_V1.md)、[多设备协议](MULTI_DEVICE_NODE_PROTOCOL_V1.md)、[LAN Pairing](monitor/LAN_PAIRING_SPEC.md)、[Observer 部署](monitor/OBSERVER_DEPLOYMENT.md) |
| Transport 构建与维护 | [脚本索引](../scripts/README.md)、[Tunnel 可靠性](TUNNEL_RELIABILITY_P1.md)、[纯传输](TUNNEL_RELIABILITY_P1_TRANSPORT_ONLY.md)、[候选发布](TUNNEL_RELIABILITY_P1_RELEASE.md)、[转发候选](TUNNEL_RELIABILITY_P1_FORWARDING_RELEASE.md)、[安装器](TUNNEL_RELIABILITY_P1_INSTALLER.md) |
| 兼容接口 | [历史 Host 执行契约](HOST_EXECUTION_CONTRACT_V2.md) |

## 源码布局

当前布局、源码分类和发布边界见 [仓库架构](REPOSITORY_ARCHITECTURE.md)。
最新 [Python 源码退役](RUST_PYTHON_RETIREMENT.md) 记录最后兼容层、构建/维护工具和资格的
Rust 替代与验证；受版本控制的 Python 源码已为零，不依赖历史本地测试运行当前回归。

`kernel/` 为 Rust runtime、开发工具和测试；`native/opaque_pairing/` 为直接链接的 OPAQUE core；
`MonitorApp/` 为 Swift UI。`clinx/mcp/registry.json` 只保留协议数据，`fixtures/` 保留
冻结独立 JSON/SQL oracle 与逐断言迁移映射。`bin/`、`systemd/` 为原生启动器/模板。

旧 Bridge/Registry/M9/Owner、completion/recovery/outbox 和 Python Dispatcher/Host runner
均已删除；当前 Helper 通过配对通道交给 Rust Authority 与 native Transport。
历史 [核心退役验证](RUST_NATIVE_CORE_RETIREMENT.md)、[Host 迁移](RUST_HOST_RUNTIME_MIGRATION.md)、
[维护与 Monitor 收敛](RUST_MAINTENANCE_CONVERGENCE.md) 保留当时的源码状态和计数。
当前运行身份须另行读回，不从源码目录或历史记录推导；本次源码退役没有部署新产物。

架构背景保留 [V2](architecture/ADR-001-v2-architecture.md)、
[事件账本](architecture/ADR-002-shadow-event-ledger.md)、
[worker ownership](architecture/ADR-003-runtime-worker-ownership.md)、
[Provider adapter](architecture/ADR-004-provider-adapter-contract.md)和
[只读 Observer](architecture/ADR-006-monitor-read-only-observer.md)。
其中 roadmap 和历史资格范围应按文档日期解读。

## 发布与回退记录

源码版本、候选 manifest、安装目录和实际进程身份分别核对。2026-10-22 的生产
Owner 候选标识为 `owner-shared-07da5fe20c4119dee1b4`；安装/切换证据位于
`/data/artifacts/clinx-installer-identity-review-20261022T114753Z/production-cutover/`。
同 UID 部署不提供 DB/WAL/SHM 原始写 FD 的 OS 级排他隔离，不能从 SQLite CAS
或测试通过推导出物理隔离。回退须保留新权威事实，不能恢复旧数据库覆盖新提交。
后续操作必须重新读回运行状态，历史回执不代表当前状态。

## 历史资料恢复

[项目收敛记录](PROJECT_CONVERGENCE.md)保留旧工作区的恢复位置。
2026-10-22 仓库整理将一次性调查报告、交接快照和验证证据迁出源码目录。
本机恢复目录为 `/data/artifacts/clinx-main-consolidation-20261022T131946Z/`：

- `source-before.tar.gz`：整理前全部 Git 可见源码、文档和未跟踪文件。
- `dirty-before.patch`、`files-before.json`：未提交差异、文件哈希与权限。
- `retired-files/docs/`：移出的原始报告与证据，保留原相对路径。
- `historical-validation/`、`historical-validation.tar.gz`：原 `.validation/` 内容。
- `disposition-applied.json`：逐文件处置清单。

实验性 Decision Kernel、Runtime Lab、维护授权原型及专属测试已退出源码树；
核心 Owner、authority、SQLite、transport、幂等与恢复回归继续保留。
当前开发与测试使用 Rust 工具链。历史 pytest 套件保留在本机被忽略的 `tests/`，
不随 Git 分发；新 clone 包含现役 Rust 测试，未包含的是这份历史 Python 归档。

- [Rust Node 网络、Registry 与执行请求迁移](RUST_NODE_NETWORK_MIGRATION.md)：现有配对 TLS、Centre/Owner scope、Provider 分块/IPC、Node 路由账本及 ACK 不确定恢复。
- [Rust 原生历史与观察目录迁移](RUST_OBSERVATION_MIGRATION.md)：精确 native 历史、增量扫描、持久观察批次、查询授权与旧摘要/游标兼容；Collector 队列仍待迁移。

- [Rust Collector 队列与恢复迁移](RUST_COLLECTOR_MIGRATION.md)：节点 spool、checkpoint/ACK 原子事务、采样合并、丢 ACK 与进程重启资格。

- [Rust 客户端运行时收敛](RUST_CLIENT_RUNTIME_MIGRATION.md)：Provider/Node/Observer/Discovery 与监督策略实际迁移、完整回归、残留和规模；[全部 Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。

- [Rust 薄兼容层迁移](RUST_THIN_RUNTIME_MIGRATION.md)：Provider 客户端/进程、Observer HTTP、配置与独立 Tunnel 生命周期；全仓 Python <5% 的验收口径和当前差距。
- [Rust Tunnel supervisor](RUST_TUNNEL_SUPERVISOR.md)：独立服务生命周期、精确恢复与打包资格。
