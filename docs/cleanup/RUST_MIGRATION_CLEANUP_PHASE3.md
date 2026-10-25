> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](../CONTRACTS.md)与[架构](../REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 迁移后第三轮清理（2026-10-24）

后续 Python Owner / shadow 模型退役、当前规模和剩余迁移阻塞见
[第四轮清理](RUST_MIGRATION_CLEANUP_PHASE4.md)。以下为本轮当时的独立基线和结果。

基准 `efd14cd2e0c2811f5276358cbcc73532ff2f23c0`，canonical 仓库
`/home/pvxlabs/dev/clinx`，分支 `main`。开始时受控工作区干净；本地忽略的 `tests/`
129 个文件保持原字节、原断言。没有部署、重启现役服务、修改已安装 Mac App/配置、
操作现役任务或写生产数据库。

**Rust 接管 P620 业务状态权威已经成立，旧 Python 业务核心完全退役尚未完成。**
本轮实际删除旧 Linear 轮询/Dispatcher，实现独立的设备发现入口，并验证干净构建产物。
根目录仍有 60 个 Python 文件，四个旧核心文件仍合计 13,409 行，不能把它们全部称作轻量适配层。

## 实际删除和入口收口

本轮整文件删除 **0 个**；Python 差异删除 **1,525 行**、新增 28 行，净减 **1,497 行**。
其中 `bridge.py` 从 7,060 行减至 5,556 行。计数为物理行，含注释和空行。
没有靠移动文件或改名计作删除。

| 删除/修改项 | 依据与结果 |
| --- | --- |
| `bridge.Dispatcher`、`bridge.Bridge` | 独立的 V1/M3 Dispatcher 与 Linear polling owner 已退役；没有受控外部运行调用方，旧服务 masked；Mac 适配器使用保留的 `TaskDispatcher` |
| `SingleInstanceLock`、`DispatchContract`、`parse_dispatch_contract`、`TargetRegistry` | 只服务于上述退役入口；对应本地历史测试保留，失败明确报告 |
| `codex_prompt`、`write_dispatch_record`、`wait_for_codex_result`、`is_git_repo`、Linear `doctor` | 退役调用链的专用实现与工具，无现役调用方 |
| CLI `run` / `once` / `doctor` | 删除命令注册和启动尾部，README 不再指导启动旧轮询服务 |
| `native_input_recovery`、Bridge reconciliation | 移除第二轮已删除的 Authority token/submit 和 borrowed transaction 分支；保留普通兼容事务、原生输入精确读回与原有 completion 路径 |
| `bin/clinx`、`local_discovery.cli` | `devices/pair/serve` 直接走发现入口，配置读取来自 `runtime_config`；其他明确的本地兼容命令才延迟导入 Bridge |
| `bin/clinx-context-mcp` | 保留原有环境清理边界，改为执行现有 `rust_stdio.py`，连接 `/run/clinx-rust-api/mcp.sock`；不再默认构造 Python 后端 |

本轮的源码 MCP 启动器没有被所检查的现役服务或 Codex MCP 配置引用。验证使用临时 launcher
和 relay stub，没有通过它连接生产 socket。既有 `TaskDispatcher`、Registry、M9 和设备恢复
契约仍保留；没有更改并行执行、任务身份、投递、ACK、幂等及状态规则。

前三轮累计整文件删除 **28 个、7,524 行**；累计受控 Python 净减 **9,234 行**。
累计统计的初始基准为 `af9c263757684270c8e38c325c19a612bd780a9e`。

## 规模和发布包

| 范围 | 本轮前文件 / 物理行 | 本轮后文件 / 物理行 |
| --- | ---: | ---: |
| 受控 Python | 117 / 43,413 | 117 / 41,916 |
| 其中根目录 Python | 60 / 33,297 | 60 / 31,785 |
| Rust | 20 / 9,416 | 20 / 9,416 |
| Swift | 62 / 11,355 | 62 / 11,355 |
| Rust release Python 源码清单 | 47 / 16,633 | 47 / 16,633 |
| Monitor Python 源码清单 | 75 / 35,965 | 75 / 34,453 |

仓库统计只包含受控源码，不含忽略的 Python 测试、缓存及生成产物；Rust/Swift 的受控测试
包含在各自语言内。两个包的计数不含另行复制的 `node_service_entrypoint.py`，与前轮口径一致。
三种语言合计口径下，Python 从初始约 71.1% 降到约 66.9%；这仍不是完成核心退役后的规模。

本轮继续使用 `runtime_packaging.py` 的两个显式源码清单，
`MonitorApp/Scripts/build-app.sh` 无需再维护第二份依赖列表。Rust 包不带 Bridge、Registry、
M9、Owner、shadow ledger；Monitor 包保留设备兼容链，但不再包含本轮删除的符号。
验证了增量构建清除旧模块、release staging 完整刷新源码以及 manifest SHA。
没有发现新的可证无用 Cargo 依赖；Rust 和 Swift 源码与依赖声明未变。

## 当前引用分类与保留的 Python

[逐文件引用清单](rust-migration-inventory-phase3.json) 覆盖 345 个代码、脚本、服务、
构建配置及本地测试文件：ACTIVE 141、COMPAT 41、TEST 163。本轮删除的是保留文件内的
LEGACY 实现，因此当前文件清单没有新增整文件 LEGACY/UNUSED 删除项；此前删除项见
[第一轮](RUST_MIGRATION_CLEANUP.md)和[第二轮](RUST_MIGRATION_CLEANUP_PHASE2.md)。
清单记录双向引用、SHA、行数和发布归属；条件导入、Swift 符号和字面量关系是保守分析，
ACTIVE 不表示文件每个分支均在生产执行，也不把无静态引用等同于无外部依赖。

根目录 60 个文件分为 43 个 ACTIVE、15 个 COMPAT、2 个 TEST，完整文件名在上述清单中。

| 保留模块 | 实际用途 |
| --- | --- |
| `mcp_server`、`rust_mcp`、`rust_authority`、`rust_integration` | 公共 MCP 契约和 Rust IPC 适配；MCP 仍有显式兼容 factory，但 Rust 路径不导入旧核心 |
| `rust_transport`、`app_server`、`native_provider`、`desktop_socket`、`rust_host`、`host_executor`、`tool_delivery` | Codex/Desktop Provider、Host 子进程和投递；不是已删除的 prototype Provider |
| `rust_reads`、`rust_monitor`、`rust_observation`、`rust_worker_identity`、`rust_workspace_probe`、`native_history`、`native_writer_probe`、`thread_identity` | Rust 读取、监控投影、原生历史及准确身份/工作区观察 |
| `observer_server`、`observation_source`、`observation_mcp`、`native_observation_bridge`、`network_observation` | 只读观察、监控和设备信息适配 |
| `node_protocol`、`node_runtime`、`node_centre_entrypoint`、`rust_node_adapter`、`local_discovery/` | macOS Helper、节点路由、发现、配对与 Rust 节点接口 |
| `runtime_config`、`runtime_contracts`、`execution_policy`、`execution_semantics`、`workspace_contract`、`host_contract`、`dispatch_errors`、`derived_git_targets` | 共享配置、错误/状态常量、DTO、worktree 身份、执行契约与 Git 目标推导 |
| `consultations`、`production_workflows`、`execution_liveness` | 公共工具工作流及现役观察/兼容 liveness；不替代 Rust 状态权威 |
| `bridge`、`task_registry`、`m9_integration`、`execution_owner` | 尚未迁完的设备本地执行核心，COMPAT；合计 13,409 行，不进入 Rust release |
| `completion_runtime`、`native_lifecycle`、`node_execution_adapter`、`desktop_publication`、`historical_handoff`、`native_input_recovery`、`prepared_requests`、`result_ingestion`、`task_projection`、`unstarted_recovery`、`domain/`、`shadow_ledger/` | 上述兼容链的 completion、prepared seal、投影、发布、结果摄取和恢复依赖 |
| `runtime_packaging`、`rust_stdio`、`tunnel_reliability`、`observer_history_guard`、`policy_maintenance` | 构建清单、stdio relay、现役 Tunnel watchdog、历史挂载/策略维护 |
| `embedded_bridge_readiness`、`provider_qualification` | 只读 idle/Provider 资格验证，TEST，不进入应用源码清单 |

## 运行证据与具体阻塞

2026-10-24 12:31 UTC 的只读读回：Authority PID `1023787`，release
`rust-73ad2dc733578a1e8947`，NRestarts=0；Dispatcher PID `1024186`，release
`rust-c09215686ef963f9067f`；Centre PID `1024191`，release `rust-6c0ba78d0ba4d7960131`。
Observer PID `775202`，Tunnel PID `3901220`。PID 与前轮一致；Dispatcher/Centre 已有的
NRestarts=1 没有增加。旧 Python 服务仍 masked/inactive。源码 HEAD 和各运行 release
是独立身份，提交源码不会切换运行服务。

iMac Helper PID `26275`、Air Helper PID `76622`，实际入口均为已安装 App 的
`node_service_entrypoint.py --node-service`。前轮核对 iMac `execute_tasks=true`、Air false，
两台都有本地执行配置且 DB 无实验 Authority/runtime/fence schema。本轮额外确认 iMac
进程没有 `CLINX_AUTHORITY`、`CLINX_RUST_SOCKET`、`CLINX_RUST_UID`；已安装的
`rust_integration.enabled()` 默认 Python，adapter 的该分支创建 Registry/TaskDispatcher/M9。
iMac 当前观察到 degraded / OFFLINE_BUFFERING，这不是正在成功处理业务的证明。

剩余退役阻塞：

1. **Mac 本地执行链仍启用。** 剩余 `bridge.py` 5,556 行、`task_registry.py` 4,486 行、
   `m9_integration.py` 2,804 行、`execution_owner.py` 563 行相互依赖。要整体删除，必须先
   实现并验证等价的节点执行/恢复路径。当前 Rust IPC 依赖本机 Unix socket/peer credentials，
   `rust_node_adapter` 不能直接覆盖旧 canonical handoff 的全部能力。不能仅设置默认 Rust
   就宣称完成迁移，也不能通过停用 Mac 任务能力获得“清理成功”。
2. **恢复和兼容 factory 仍可达。** `mcp_server.build_server` 的显式兼容路径、普通 Owner
   client 和历史恢复工具尚未证明可以删除；`shadow_ledger`/`domain` 仍被本地注册、投影和
   发布引用。没有把“P620 不再运行”推导成所有设备无依赖。
3. **带 lab 名称的 Rust 状态实现仍现役。** `authority_state.rs` 使用
   `runtime_lab::State`；同文件的 Journal 和 Python reference 有资格用途。按名字整包删除
   会删掉现役状态实现，职责拆分也不是此次清理可以冒充完成的事项。

## 验证结果

| 验证 | 状态 / 结果 |
| --- | --- |
| 新输出目录 Rust Core / 集成测试 | PASS：21 library + 25 integration；locked/offline |
| 新输出目录 Rust release build | PASS：`clinx-runtime` SHA256 `56070e5b96f35e8951d34e80f5aec430fb79e423b17043177dfeacc373caa68c` |
| 独立 Rust 源码包 MCP catalog / 配置 / Host 身份 / 导入 | PASS；不导入旧核心；原请求/路由测试 36/36 PASS |
| 实际新 Rust binary + 独立包调用链 | PASS：MCP→IPC prepare/start/status、scripted Provider 完成、重复 start 同 execution、排队取消释放分配、丢 ACK 后 fixture 进程终止/重开并精确读回不重放；两个执行意图仅两次 native input |
| release staging 和 Monitor 增量复制 | PASS：完整源码刷新、retired 文件排除、native/deps hash、manifest integrity、base drift 拒绝和 Swift/DiscoveryRuntime 资源保留 |
| 发现 CLI / MCP 源码 launcher | PASS：发现三命令在禁止导入旧核心时可用；任意 cwd 和 config override；MCP 仅 relay，原环境清理保留 |
| Air 原生 SwiftPM 干净构建 | PASS：Swift 6.4，`swift build`；选定 NativeURI/FinalizedUIRule/NodeServiceController 测试 15 PASS；既有 actor/Sendable/Keychain 警告仍在 |
| Air 已安装 App 的 Python 3.13.3 + 临时源码包 | PASS：Helper/Provider/本地兼容导入、临时 SQLite 事务、退役 Authority 请求拒绝、无实验表/导入、旧 polling 符号不存在 |
| 20 个本地 Python 模块完整前后对照 | **FAIL（有明示退役影响）**：之前 492 PASS / 21 FAIL / 2 SKIP / 88 subtests PASS；之后 454 PASS / 66 FAIL / 2 SKIP / 81 subtests PASS |
| 全仓本地 Python collection | **BLOCKED**：仍为 1,714 collected + 第二轮留下的相同 9 个退役模块 import errors；本轮无新增 collection error |
| 未定义/未使用导入、shell 语法、diff 空白 | PASS |
| 已安装 App 签名/安装/启动、GUI、真实 Provider 和当前设备业务端到端 | **未验证**；SwiftPM build 和临时 bundle smoke 不等于完整 App 发布或业务验收 |
| 部署及生产任务写操作 | NOT_RUN |

Python 对照的 JUnit 测试节点集合不变；39 个节点的结果因删去的 Bridge/Dispatcher/contract/
prompt/旧 CLI 预期而变化，其他节点结果保持不变。pytest/unittest subtest 分组使汇总新增
45 个 failure 项；这是退役接口测试的显式失败，不能报告成整套回归 PASS。
原 21 个失败涉及既有 MCP catalog/schema 期望、缺少原生 PAKE、发现目录限制等，未通过
修改测试或当前业务逻辑隐藏。精确节点与原因保存在 `regression-delta-final.json`，原始 XML
和失败日志一并保留。测试文件没有删改或加 skip。

证据目录：`/lab/runtime-worktrees/p620/clinx-cleanup3-20261024T1210Z`。
保留前后 XML、完整失败日志、测试 SHA、引用生成器、产物验证脚本、包 SHA、运行读回和最终
Git receipt；删除任务专属可再生源码副本、venv、Cargo target、临时包和 fixture 状态。
隔离 Git fixture 位于 `/data/artifacts/clinx-cleanup3-20261024T1210Z`，执行后已清理。
Air 临时源码和构建目录已清理，其独立日志/receipt 保留，未改动已安装 App。
