# CLINX 文档

使用入口是[项目 README](../README.md)，开发环境见[贡献指南](../CONTRIBUTING.md)，
测试命令与前置条件见[测试指南](TESTING.md)和[测试目录索引](../tests/README.md)。

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

根目录 Python 模块实现 MCP、任务、Owner、Provider 和节点协调；`domain/`、
`runtime_control/`、`provider_adapters/`、`shadow_ledger/` 提供领域、事务和事件原语。
`kernel/` 只保留 Rust Owner client，`local_discovery/` 与 `native/opaque_pairing/`
实现发现配对，`MonitorApp/` 是 macOS 产品源码。`fixtures/` 保留现行契约和传输测试输入。
`bin/`、`systemd/` 是启动器与部署模板，存在于源码中不表示已经安装。

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
外部开发者无需这些本机历史归档即可安装依赖和运行基础测试。
