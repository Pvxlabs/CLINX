# CLINX 完整文档地图

用途：逐份标明文档权威与历史角色。适用范围：全部受控 Markdown、架构清单和发布证据入口。
状态：CURRENT。基线：2026-10-25（Asia/Shanghai），Git main `1340861e6e9956cdb21532031a542c0b86645178`。
验证：`clinx-source-audit --check-project`，源码/命令人工复核；没有生产运行状态推断。

## 权威入口

| 领域 | 唯一入口 |
| --- | --- |
| 产品与版本范围 | [产品定位](PRODUCT_POSITIONING.md) |
| 架构、模块、语言及数据/执行流 | [仓库架构](REPOSITORY_ARCHITECTURE.md) |
| 强制工程规范与开发流程 | [AGENTS](../AGENTS.md)、[贡献指南](../CONTRIBUTING.md) |
| Task/Execution、Provider、Host、Node、MCP、Transport、IPC | [接口契约](CONTRACTS.md)；存储细节引用 [Authority](AUTHORITY_STORAGE.md) |
| 配置、CLI、使用、发布、运维与恢复 | [配置与运维](OPERATIONS.md)、[原生工具](../scripts/README.md) |
| 测试、平台限制与既有失败 | [测试指南](TESTING.md) |
| 版本、决策、文档维护 | [CHANGELOG](../CHANGELOG.md)、[ADR-007](architecture/ADR-007-rust-source-policy.md)、[文档标准](DOCUMENTATION.md) |

## 逐份分类

CURRENT 为现行权威；REFERENCE 为专项技术参考；HISTORICAL 为历史验收证据；
SUPERSEDED 为旧定义/重复入口。后两类不删除、不改写原日期/结果/失败；旧命令不用于当前启动。
REFERENCE 中的历史 implementation mapping 也不是当前路径定义，使用上方权威入口定位源码。

| 文档 | 分类 | 用途及处置 |
| --- | --- | --- |
| [AGENTS.md](../AGENTS.md) | CURRENT | P620 CLINX project workspace policy；现行入口；按用途维护 |
| [CHANGELOG.md](../CHANGELOG.md) | CURRENT | CLINX 变更记录；现行入口；按用途维护 |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | CURRENT | CLINX 贡献与编码规范；现行入口；按用途维护 |
| [MonitorApp/README.md](../MonitorApp/README.md) | CURRENT | CLINX macOS app；现行入口；按用途维护 |
| [README.md](../README.md) | CURRENT | CLINX；现行入口；按用途维护 |
| [docs/AUTHORITY_STORAGE.md](AUTHORITY_STORAGE.md) | CURRENT | Authority 与存储边界；现行入口；按用途维护 |
| [docs/CHATGPT_CONSULTATIONS.md](CHATGPT_CONSULTATIONS.md) | SUPERSEDED | OP ChatGPT consultations；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/CLINX_MONITOR.md](CLINX_MONITOR.md) | REFERENCE | CLINX Monitor — Phase 0/1 specification and observer contract；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/CONTRACTS.md](CONTRACTS.md) | CURRENT | 当前接口与执行契约；现行入口；按用途维护 |
| [docs/CODEX_MODEL_SELECTION.md](CODEX_MODEL_SELECTION.md) | CURRENT | Codex 模型能力、参数映射与错误诊断；现行入口；按用途维护 |
| [docs/DESKTOP_NATIVE_EXECUTION.md](DESKTOP_NATIVE_EXECUTION.md) | REFERENCE | Desktop 原生执行；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/DOCUMENTATION.md](DOCUMENTATION.md) | CURRENT | 文档标准与维护规则；现行入口；按用途维护 |
| [docs/EXACT_NATIVE_READ_ROUTING.md](EXACT_NATIVE_READ_ROUTING.md) | REFERENCE | Exact native conversation reads；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/EXECUTION_AUTHORITY.md](EXECUTION_AUTHORITY.md) | SUPERSEDED | Canonical Task execution authority；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/EXECUTION_LIVENESS_V2.md](EXECUTION_LIVENESS_V2.md) | SUPERSEDED | CLINX Execution Liveness Reconciliation v2；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/EXECUTION_OWNER_R1.md](EXECUTION_OWNER_R1.md) | SUPERSEDED | Execution Owner R1：精确未派发恢复（DEV）；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/GO_TRANSPORT_REBUILD.md](GO_TRANSPORT_REBUILD.md) | HISTORICAL | 固定 Go Transport 依赖修复与全新重建；保留原基准、阶段结果和失败；不推导现状 |
| [docs/HOST_EXECUTION_CONTRACT_V2.md](HOST_EXECUTION_CONTRACT_V2.md) | REFERENCE | CLINX Host Execution Contract v2；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/LOCAL_DISCOVERY_V1.md](LOCAL_DISCOVERY_V1.md) | HISTORICAL | CLINX Local Discovery v1：生产 PAKE 配对收口报告；保留原基准、阶段结果和失败；不推导现状 |
| [docs/MULTI_DEVICE_NODE_PROTOCOL_V1.md](MULTI_DEVICE_NODE_PROTOCOL_V1.md) | REFERENCE | CLINX 多设备节点协议 v1；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/NATIVE_DESKTOP_DISCOVERY_20261011.md](NATIVE_DESKTOP_DISCOVERY_20261011.md) | HISTORICAL | CLINX 原生任务发现与打开修复；保留原基准、阶段结果和失败；不推导现状 |
| [docs/NATIVE_FULL_ACCESS.md](NATIVE_FULL_ACCESS.md) | CURRENT | Native full access；现行入口；按用途维护 |
| [docs/NATIVE_TASK_FORWARDING.md](NATIVE_TASK_FORWARDING.md) | SUPERSEDED | 原生任务流通；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/NATIVE_WORKSPACE_EXECUTION.md](NATIVE_WORKSPACE_EXECUTION.md) | SUPERSEDED | 原生开发执行与受控发布边界；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/OPERATIONS.md](OPERATIONS.md) | CURRENT | 配置、使用、发布与运维；现行入口；按用途维护 |
| [docs/PRODUCT_POSITIONING.md](PRODUCT_POSITIONING.md) | CURRENT | CLINX 产品定位与职责边界；现行入口；按用途维护 |
| [docs/PROJECT_CONVERGENCE.md](PROJECT_CONVERGENCE.md) | REFERENCE | P620 CLINX 项目收敛；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/PYTHON_RUNTIME_BOUNDARIES.md](PYTHON_RUNTIME_BOUNDARIES.md) | SUPERSEDED | Python 退役边界；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/README.md](README.md) | CURRENT | CLINX 完整文档地图；现行入口；按用途维护 |
| [docs/REPOSITORY_ARCHITECTURE.md](REPOSITORY_ARCHITECTURE.md) | CURRENT | CLINX 架构与源码布局；现行入口；按用途维护 |
| [docs/RUST_AUTHORITY.md](RUST_AUTHORITY.md) | HISTORICAL | Native Rust business authority；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_CANONICAL_HANDOFF.md](RUST_CANONICAL_HANDOFF.md) | HISTORICAL | Rust canonical handoff：身份与 predecessor 事务；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_CLIENT_RUNTIME_MIGRATION.md](RUST_CLIENT_RUNTIME_MIGRATION.md) | HISTORICAL | Rust 客户端运行时收敛；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_COLLECTOR_MIGRATION.md](RUST_COLLECTOR_MIGRATION.md) | HISTORICAL | Rust Collector 队列与恢复迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_COMPAT_RETIREMENT.md](RUST_COMPAT_RETIREMENT.md) | HISTORICAL | Python 兼容实现与维护工具退役；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_DEVICE_RUNTIME_MIGRATION.md](RUST_DEVICE_RUNTIME_MIGRATION.md) | HISTORICAL | Rust 设备运行链迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_HOST_RUNTIME_MIGRATION.md](RUST_HOST_RUNTIME_MIGRATION.md) | HISTORICAL | Rust Host / Dispatcher 迁移验证；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_MAINTENANCE_CONVERGENCE.md](RUST_MAINTENANCE_CONVERGENCE.md) | HISTORICAL | Rust 维护工具与 Monitor 兼容闭包收敛；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_MCP_RUNTIME_MIGRATION.md](RUST_MCP_RUNTIME_MIGRATION.md) | HISTORICAL | Rust MCP 业务入口迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NATIVE_CORE_RETIREMENT.md](RUST_NATIVE_CORE_RETIREMENT.md) | HISTORICAL | Rust native 执行核心退役验证；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NATIVE_CUTOVER.md](RUST_NATIVE_CUTOVER.md) | HISTORICAL | Rust 原生生产入口切换；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NATIVE_QUALIFICATION_MIGRATION.md](RUST_NATIVE_QUALIFICATION_MIGRATION.md) | HISTORICAL | Native、Host、Node 与 Provider 资格迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NODE_API_MIGRATION.md](RUST_NODE_API_MIGRATION.md) | HISTORICAL | Rust Node execution API 迁移（2026-10-25）；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NODE_DAEMON.md](RUST_NODE_DAEMON.md) | HISTORICAL | Rust Node/Centre runtime；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NODE_NETWORK_MIGRATION.md](RUST_NODE_NETWORK_MIGRATION.md) | HISTORICAL | Rust Node 网络、Registry 与执行请求迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_NODE_ROUTING.md](RUST_NODE_ROUTING.md) | HISTORICAL | Rust 节点反向请求与 Provider 路由（第七轮）；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_OBSERVATION_MIGRATION.md](RUST_OBSERVATION_MIGRATION.md) | HISTORICAL | Rust 原生历史与观察目录迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_OBSERVER_RUNTIME_MIGRATION.md](RUST_OBSERVER_RUNTIME_MIGRATION.md) | HISTORICAL | Rust Observer、观察 MCP 与设备 CLI 迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_PROVIDER_RUNTIME_MIGRATION.md](RUST_PROVIDER_RUNTIME_MIGRATION.md) | HISTORICAL | Rust Node Provider / workspace 迁移验证；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_PYTHON_RETIREMENT.md](RUST_PYTHON_RETIREMENT.md) | HISTORICAL | Python 源码最终退役；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_QUALIFICATION_MIGRATION.md](RUST_QUALIFICATION_MIGRATION.md) | HISTORICAL | Rust 资格测试与安装维护迁移；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_THIN_RUNTIME_MIGRATION.md](RUST_THIN_RUNTIME_MIGRATION.md) | HISTORICAL | Rust 薄兼容层迁移与 5% 验收；保留原基准、阶段结果和失败；不推导现状 |
| [docs/RUST_TUNNEL_SUPERVISOR.md](RUST_TUNNEL_SUPERVISOR.md) | HISTORICAL | Rust Tunnel supervisor；保留原基准、阶段结果和失败；不推导现状 |
| [docs/TESTING.md](TESTING.md) | CURRENT | 测试与质量验收；现行入口；按用途维护 |
| [docs/THREAD_IDENTITY.md](THREAD_IDENTITY.md) | REFERENCE | Codex 原生对话互通；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/TRUSTED_WORKSPACE_PATHS.md](TRUSTED_WORKSPACE_PATHS.md) | REFERENCE | Host trusted workspace paths；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/TUNNEL_RELIABILITY_P1.md](TUNNEL_RELIABILITY_P1.md) | HISTORICAL | CLINX 分阶段可靠性改进：P1 Tunnel Reliability；保留原基准、阶段结果和失败；不推导现状 |
| [docs/TUNNEL_RELIABILITY_P1_FORWARDING_RELEASE.md](TUNNEL_RELIABILITY_P1_FORWARDING_RELEASE.md) | HISTORICAL | P1：仅转发职责的正式候选；保留原基准、阶段结果和失败；不推导现状 |
| [docs/TUNNEL_RELIABILITY_P1_INSTALLER.md](TUNNEL_RELIABILITY_P1_INSTALLER.md) | HISTORICAL | P1 窄发布入口；保留原基准、阶段结果和失败；不推导现状 |
| [docs/TUNNEL_RELIABILITY_P1_RELEASE.md](TUNNEL_RELIABILITY_P1_RELEASE.md) | HISTORICAL | P1 最小发布与 CAS 回退准备；保留原基准、阶段结果和失败；不推导现状 |
| [docs/TUNNEL_RELIABILITY_P1_TRANSPORT_ONLY.md](TUNNEL_RELIABILITY_P1_TRANSPORT_ONLY.md) | HISTORICAL | P1 Tunnel：复用上游纯传输 runtime；保留原基准、阶段结果和失败；不推导现状 |
| [docs/UNIFIED_TASK_LIFECYCLE_V1.md](UNIFIED_TASK_LIFECYCLE_V1.md) | SUPERSEDED | CLINX Unified Task Lifecycle v1 — DEV 实现与有界验收；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/UNIFIED_TASK_LIFECYCLE_V1_CONTROL.md](UNIFIED_TASK_LIFECYCLE_V1_CONTROL.md) | SUPERSEDED | Unified Task Lifecycle v1：原生控制 DEV 增量；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/ADR-001-v2-architecture.md](architecture/ADR-001-v2-architecture.md) | SUPERSEDED | ADR-001: CLINX V2 Architecture Baseline；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/ADR-002-shadow-event-ledger.md](architecture/ADR-002-shadow-event-ledger.md) | SUPERSEDED | ADR-002: CLINX V2 Shadow Event Ledger；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/ADR-003-runtime-worker-ownership.md](architecture/ADR-003-runtime-worker-ownership.md) | SUPERSEDED | ADR-003: Durable RuntimeWorker Ownership and Fenced Assignment；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/ADR-004-provider-adapter-contract.md](architecture/ADR-004-provider-adapter-contract.md) | SUPERSEDED | ADR-004: Provider Adapter Contract and Codex Conformance；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/ADR-006-monitor-read-only-observer.md](architecture/ADR-006-monitor-read-only-observer.md) | REFERENCE | ADR-006: CLINX Monitor reads canonical persisted facts through a separate observer；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/architecture/ADR-007-rust-source-policy.md](architecture/ADR-007-rust-source-policy.md) | CURRENT | ADR-007：Rust 后端唯一实现与文档权威；现行入口；按用途维护 |
| [docs/architecture/ADR-008-additive-device-routes.md](architecture/ADR-008-additive-device-routes.md) | REFERENCE | 设备执行路由的离线增配；当前命令见运维指南，部署另行验收 |
| [docs/architecture/EXECUTION_CORE_STATE_CONTRACT_V1.md](architecture/EXECUTION_CORE_STATE_CONTRACT_V1.md) | REFERENCE | 第二阶段：执行核心状态契约 v1；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/architecture/RUST_RUNTIME_PROTOTYPE_STAGE3.md](architecture/RUST_RUNTIME_PROTOTYPE_STAGE3.md) | SUPERSEDED | 第三阶段：隔离 Rust Runtime 原型；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/architecture/v2-review.md](architecture/v2-review.md) | SUPERSEDED | CLINX V2 Architecture Review；旧定义已被当前 Rust 入口替代；保留取证/决策背景 |
| [docs/cleanup/REPOSITORY_CONVERGENCE_PHASE5.md](cleanup/REPOSITORY_CONVERGENCE_PHASE5.md) | HISTORICAL | 第五轮：按目标架构收敛源码、入口和发布边界；保留原基准、阶段结果和失败；不推导现状 |
| [docs/cleanup/RUST_MIGRATION_CLEANUP.md](cleanup/RUST_MIGRATION_CLEANUP.md) | HISTORICAL | Rust 迁移后清理记录（2026-10-24）；保留原基准、阶段结果和失败；不推导现状 |
| [docs/cleanup/RUST_MIGRATION_CLEANUP_PHASE2.md](cleanup/RUST_MIGRATION_CLEANUP_PHASE2.md) | HISTORICAL | Rust 迁移后第二轮源码清理（2026-10-24）；保留原基准、阶段结果和失败；不推导现状 |
| [docs/cleanup/RUST_MIGRATION_CLEANUP_PHASE3.md](cleanup/RUST_MIGRATION_CLEANUP_PHASE3.md) | HISTORICAL | Rust 迁移后第三轮清理（2026-10-24）；保留原基准、阶段结果和失败；不推导现状 |
| [docs/cleanup/RUST_MIGRATION_CLEANUP_PHASE4.md](cleanup/RUST_MIGRATION_CLEANUP_PHASE4.md) | HISTORICAL | Rust 迁移清理第四轮：Python Owner 与 shadow 业务模型退役；保留原基准、阶段结果和失败；不推导现状 |
| [docs/monitor/ACTIVITY_V1_SPEC.md](monitor/ACTIVITY_V1_SPEC.md) | REFERENCE | CLINX Monitor Activity v1；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/monitor/LAN_PAIRING_SPEC.md](monitor/LAN_PAIRING_SPEC.md) | REFERENCE | Monitor LAN Pairing v1；专项语义/历史实现参考；现行实现以契约地图为准 |
| [docs/monitor/OBSERVER_DEPLOYMENT.md](monitor/OBSERVER_DEPLOYMENT.md) | REFERENCE | Observer private deployment contract；专项语义/历史实现参考；现行实现以契约地图为准 |
| [kernel/README.md](../kernel/README.md) | CURRENT | CLINX Rust 内核、运行时与工具；现行入口；按用途维护 |
| [scripts/README.md](../scripts/README.md) | CURRENT | 原生构建与维护入口；现行入口；按用途维护 |

## 机器清单与非 Markdown 证据

- [native-runtime](../architecture/native-runtime.json)：原生入口、App helpers、零 Python 发布依赖。
- [source-policy](../architecture/source-policy.json)：每份当前代码、角色、直接依赖及锁定遗留项；与构建器/Cargo 对照。
- [runtime-data](../architecture/runtime-data.json)：现役协议数据；[python-sources](../architecture/python-sources.json) 全为 DEAD 的历史删除登记。
- [module-moves](../architecture/module-moves.json)、[native qualification](../architecture/native-qualification-migrations.json)、[qualification](../architecture/qualification-migrations.json)、[compat retirement](../architecture/compat-retirement.json)、[maintenance](../architecture/maintenance-convergence.json)：历史映射，不能据此恢复旧模块。
- [cleanup](cleanup/) 中全部 JSON receipt/inventory 是对应迁移报告的历史机器证据，保留原内容；[fixtures](../fixtures/) 是当前独立契约/回归输入。
- [monitor](monitor/) 中 schema/JSON 为观察协议资料；Swift 设计快照属于历史参考，App 源码在 [MonitorApp/Sources](../MonitorApp/Sources/)。

### 非 Markdown 文档逐份登记

| 路径 | 分类 | 用途 |
| --- | --- | --- |
| [architecture/compat-retirement.json](../architecture/compat-retirement.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/maintenance-convergence.json](../architecture/maintenance-convergence.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/module-moves.json](../architecture/module-moves.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/native-qualification-migrations.json](../architecture/native-qualification-migrations.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/native-runtime.json](../architecture/native-runtime.json) | REFERENCE | 当前原生清单或观察协议数据，按源码消费者验证 |
| [architecture/python-sources.json](../architecture/python-sources.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/qualification-migrations.json](../architecture/qualification-migrations.json) | HISTORICAL | 原迁移/退役映射，不是当前可执行入口 |
| [architecture/runtime-data.json](../architecture/runtime-data.json) | REFERENCE | 当前原生清单或观察协议数据，按源码消费者验证 |
| [docs/cleanup/GO_TRANSPORT_REBUILD_RECEIPT.json](cleanup/GO_TRANSPORT_REBUILD_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_CLIENT_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_CLIENT_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_COLLECTOR_MIGRATION_RECEIPT.json](cleanup/RUST_COLLECTOR_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_COMPAT_RETIREMENT_RECEIPT.json](cleanup/RUST_COMPAT_RETIREMENT_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_DEVICE_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_DEVICE_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_HOST_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_HOST_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_MAINTENANCE_CONVERGENCE_RECEIPT.json](cleanup/RUST_MAINTENANCE_CONVERGENCE_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_MCP_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_MCP_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_NATIVE_CORE_RETIREMENT_RECEIPT.json](cleanup/RUST_NATIVE_CORE_RETIREMENT_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_NATIVE_CUTOVER_RECEIPT.json](cleanup/RUST_NATIVE_CUTOVER_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_NATIVE_QUALIFICATION_MIGRATION_RECEIPT.json](cleanup/RUST_NATIVE_QUALIFICATION_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_NODE_API_MIGRATION_RECEIPT.json](cleanup/RUST_NODE_API_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_NODE_NETWORK_MIGRATION_RECEIPT.json](cleanup/RUST_NODE_NETWORK_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_OBSERVATION_MIGRATION_RECEIPT.json](cleanup/RUST_OBSERVATION_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_OBSERVER_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_OBSERVER_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_PROVIDER_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_PROVIDER_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_PYTHON_RETIREMENT_RECEIPT.json](cleanup/RUST_PYTHON_RETIREMENT_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_QUALIFICATION_MIGRATION_RECEIPT.json](cleanup/RUST_QUALIFICATION_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/RUST_THIN_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_THIN_RUNTIME_MIGRATION_RECEIPT.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/rust-migration-inventory-phase2.json](cleanup/rust-migration-inventory-phase2.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/rust-migration-inventory-phase3.json](cleanup/rust-migration-inventory-phase3.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/rust-migration-inventory-phase4.json](cleanup/rust-migration-inventory-phase4.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/cleanup/rust-migration-inventory.json](cleanup/rust-migration-inventory.json) | HISTORICAL | 原迁移 inventory/receipt，原字节保留 |
| [docs/monitor/api-examples.json](monitor/api-examples.json) | REFERENCE | 当前原生清单或观察协议数据，按源码消费者验证 |
| [docs/monitor/observation-v1.schema.json](monitor/observation-v1.schema.json) | REFERENCE | 当前原生清单或观察协议数据，按源码消费者验证 |
| [docs/monitor/observer-v1.schema.json](monitor/observer-v1.schema.json) | REFERENCE | 当前原生清单或观察协议数据，按源码消费者验证 |
| [architecture/source-policy.json](../architecture/source-policy.json) | REFERENCE | 当前代码角色、直接依赖、发布分类与锁定遗留项 |

## 发布与回退记录

当前规则只定义在[配置与运维](OPERATIONS.md)。历史的 [Rust Authority](RUST_AUTHORITY.md)、
[原生切换](RUST_NATIVE_CUTOVER.md)、[Python 退役](RUST_PYTHON_RETIREMENT.md)和
[固定 Go Transport 重建](GO_TRANSPORT_REBUILD.md)记录不同日期、源码、候选或已执行部署。
它们之间的阶段差异不通过改写报告消除，今天的已选 release、进程和数据库状态本次未验收。

## 历史资料恢复

[项目收敛](PROJECT_CONVERGENCE.md)保留旧工作区恢复位置。2026-10-22 整理记录的本机证据目录为
`/data/artifacts/clinx-main-consolidation-20261022T131946Z/`，包括 `source-before.tar.gz`、
`dirty-before.patch`、`files-before.json`、`retired-files/docs/`、`historical-validation/`、
`historical-validation.tar.gz` 和 `disposition-applied.json`。这些不是新 clone 自带文件，
本次未重新验证其可达性；恢复前核对原基准和 hash，不覆盖当前仓库或运行数据。
本机 ignored `tests/` 是历史 Python 测试归档，不属于当前开发依赖。
