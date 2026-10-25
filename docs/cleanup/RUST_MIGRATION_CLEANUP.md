> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](../CONTRACTS.md)与[架构](../REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 迁移后清理记录（2026-10-24）

> 本文记录第一轮清理，保留当时的基线和验证结果。第二轮已删除 Python Authority、
> runtime_control 和 Provider 实验实现，并完成两台 Mac 的临时源码验证；当前清单和
> 兼容阻塞见[第二轮记录](RUST_MIGRATION_CLEANUP_PHASE2.md)。下文的“保留”状态仅适用于第一轮。

本次在 canonical `/home/pvxlabs/dev/clinx` 的 `main` 上清理，基准为
`af9c263757684270c8e38c325c19a612bd780a9e`。开始时 Git 工作区干净；已有忽略的
本地测试保持原文件、原断言及原失败。没有创建新 CLINX 仓库，没有部署、重启现役服务、
操作现役任务或写生产数据库。2026-10-24 19:16（UTC+8）读回的现役服务 PID 与开始时一致。

安全可实施的清理与源码提交完成；**整包删除全部 Python 历史实现仍为 BLOCKED**。
原因是 macOS Helper 的可配置本地执行分支仍引用这些实现。本次没有用语言比例决定删除范围。
macOS 原生 build、签名和实际设备运行为 **未验证**；Linux 上的源文件/导入验证不代替它们。

## 权威与运行入口

| 组件 | 本次实际入口/状态 |
| --- | --- |
| Rust Authority | `rust-73ad2dc733578a1e8947/clinx-runtime`，PID `1023787`，active，`NRestarts=0` |
| Dispatcher | `rust-c09215686ef963f9067f/control/rust_transport.py`，PID `1024186`，active |
| Centre | `rust-6c0ba78d0ba4d7960131/control/node_centre_entrypoint.py --rust-projection`，PID `1024191`，active |
| Observer | `rust-b2169586d3fb0d74e810/control/observer_server.py`，PID `775202`，active |
| Tunnel | 已安装 `tunnel-reliability-p1-14ebcc77d24f3838d50d`，PID `3901220`，active |
| 旧 Python 服务 | `clinx.service`、`clinx-execution-owner.service`、`clinx-provider.service`、`clinx-node-centre.service` 均 masked |
| 独立 Rust Provider unit | inactive，入口 `/usr/bin/true`；执行仍使用现有 Desktop daemon |
| 实时 MCP | 仅执行 `initialize`、`tools/list`，读到 19 个工具；没有 prepare/start/cancel/ACK 操作 |

部署的 release 是本地源码之外的不可变制品，各组件版本不同；源码提交不会切换它们。
release manifest 的逐文件 SHA、进程 argv 与源 HEAD 均保存在外部证据目录。
历史 failed 的 MCP transient units 本来就存在，本次没有清除或据此声称运行完全无故障。

## 引用清单与实际删除

[逐文件引用清单](rust-migration-inventory.json) 覆盖 373 个 Python、Rust、Swift、脚本、
服务及构建配置文件，包含本机忽略测试的入站引用。每项有 `ACTIVE`、`LEGACY`、`UNUSED`、
`COMPAT` 或 `TEST`、理由、正向/反向引用、SHA 和发布归属。
Python 条件导入、Swift 符号和文件字面量是保守引用；没有把无静态引用等同于设备运行时无依赖。
`ACTIVE` 表示保留的源码职责，不表示该文件所有兼容分支都在生产执行。

整文件删除 **8 个、269 物理行**：

| 文件 | 删除依据 |
| --- | --- |
| `bin/clinx-execution-owner` | 已退役 Python Owner 的启动器；现役服务已 masked |
| `rust_provider_socket.py` | 旧独立 Provider 的 socket 等待器，无源码、服务或测试调用 |
| `scripts/qualify_execution_owner.py` | 仅资格验证旧 Python Owner；无代码调用，且仍会在 `~/dev` 创建旧实验仓库 |
| `systemd/clinx.service` | 已退役 Python Dispatcher 的源码 unit |
| `systemd/clinx-execution-owner.service` | 已退役 Python Owner 的源码 unit |
| `systemd/clinx-provider.service` | 已退役独立 Provider 的源码 unit |
| `systemd/clinx-provider.conf` | 已退役 Provider 的依赖 drop-in |
| `systemd/clinx-node-centre.service` | 已替换的 Python canonical-db Centre unit |

还删除了 `bridge.run_codex` / `CodexRunResult`、三个无引用私有 helper，以及独立 Provider
的旧 release bootstrap 打包分支。Tunnel 源码模板移除了对已退役 Provider unit 的依赖。
已安装 unit、mask、运行制品和回退依赖均未删除。

## 发布依赖与保留的 Python

`runtime_config.py` 承接原 `BridgeConfig`、传输 DTO 和 `WorkspaceConfig`；
`runtime_contracts.py` 承接共享异常、终态常量与原 worktree key 算法。
旧导入位置仍导出相同的 Python 对象。现役 Rust 调用方不再为配置、异常或身份算法导入
整个旧 Dispatcher/Registry。MCP 和 node adapter 的 Python 兼容导入推迟到其自身分支。
Rust 失败信息也不再尝试读取旧 Python preparation 表。

`runtime_packaging.py` 是两个源码集合的统一定义：

| 保留部分 | 实际用途/边界 |
| --- | --- |
| `rust_authority`、`rust_integration`、`rust_mcp`、`mcp_server` | Rust IPC、公共请求与工具 schema；不在 Python 决定业务状态 |
| `rust_transport`、`app_server`、`native_provider`、`rust_host`、`host_executor`、`tool_delivery` | Desktop Provider 传输、Host 调用及投递适配；业务 commit/ACK/恢复仍归 Rust |
| `rust_reads`、`rust_monitor`、`rust_observation`、`observer_server`、`observation_source`、`native_history`、`thread_identity` | 精确身份、native 历史和只读投影 |
| `node_protocol`、`node_runtime`、`network_observation`、`native_observation_bridge`、`local_discovery` | macOS Helper、节点发现、配对、设备路由和观察适配 |
| `runtime_config`、`runtime_contracts`、`execution_policy`、`execution_semantics`、`host_contract` 等 | 共享配置、DTO、协议与 Host 能力映射；仍需要 Python 使用 |
| `bridge`、`task_registry`、`m9_integration`、`execution_owner` 等 | macOS 可配置本地执行路径的 COMPAT，排除在 Rust release 外 |
| `provider_adapters`、`authority_outbox`、实验资格器等 | 既有测试/资格回归的 TEST，两个应用源码清单均不发布它们 |

Rust 发布器从已安装不可变 base 保留 native binary/deps，并刷新完整 adapter 源码集合，
移除旧 `control/` 模块与独立 `provider/`。不再把部分旧导入与裁剪后的集合混装。
manifest 保存刷新文件 SHA、退役文件及 `runtime_source_profile=rust`。
`--replace-source` 保留为清单成员校验兼容参数，已不表示部分覆盖。

Monitor 的打包使用相同集合定义，移除本次构建目录中旧的复制源码，排除 Python 包 CLI/
qualification 入口和 P620 服务端入口；保留实际执行兼容依赖。
`maturin==1.9.6` 改用 `uv tool run` 在 bundle 外构建，签名 runtime 只安装发现依赖和 wheel。
没有增加生产审批、权限隔离或任务执行流程，也没有改变 Swift UI。

## 规模

以下均为物理行数（含注释、空行）。仓库语言统计只包含受版本控制的源文件；忽略的 Python
测试不计入规模，但保留并参与验证。Swift/Rust 的受控测试计入对应语言。

| 范围 | 清理前文件/行 | 清理后文件/行 |
| --- | ---: | ---: |
| 仓库 Python | 136 / 51,150 | 137 / 51,044 |
| 仓库 Rust | 20 / 9,416 | 20 / 9,416 |
| 仓库 Swift | 62 / 11,355 | 62 / 11,355 |
| Rust release 的 Python 源码 | 106 / 45,197 | 47 / 16,670 |
| Monitor bundle 的唯一 Python 源码 | 87 / 41,863 | 87 / 41,261 |

Rust release 的前值来自本次实际 Dispatcher release manifest，后值来自新清单；未部署。
Monitor 前值按基准脚本展开，后值按新清单展开，均不含独立 `node_service_entrypoint.py`。
Monitor 文件总数相同，是因移除无用入口的同时补齐了共享配置/契约和 writer identity 的依赖。
Python 仓库净减 106 行；最大的收敛发生在发布边界，Rust release 少携带 59 个 Python 文件、
28,527 行。搬出的配置和共享契约没有算作“删除代码”。

## 验证与状态

| 验证 | 结果 |
| --- | --- |
| Rust Core `cargo test --lib --locked --offline` | PASS，21/21 |
| Rust business/native routing/authority store/workspace integration | PASS，25/25，临时状态 |
| `cargo build --release --locked --offline --bin clinx-runtime` | PASS；未安装 |
| 18 个 Python 回归模块的相同前后基线 | 282 PASS、16 既有失败、44 subtests PASS；前后结果多重集相同，本地测试文件字节未改 |
| 全仓库 Python 收集 | 前后均 1,967 个测试，相同节点集合 |
| 不含旧业务模块的独立 Rust Python 包 | PASS：全部模块导入、配置解析、Host 身份算法和 MCP catalog |
| 独立 Rust Python 包的现有请求/路由回归 | PASS，36/36 |
| 独立 Monitor Python 包 | PASS：发现、观察、Rust adapter 和本地执行兼容链导入；旧构建残留清除 |
| 发布器临时 fixture | PASS：完整源码刷新、旧文件排除、native/deps SHA 保留、manifest SHA、base 漂移拒绝 |
| shell 语法、所改模块未定义符号、新模块静态检查、diff 空白检查 | PASS |
| 旧 Tunnel 模板测试 | BLOCKED：基准 PASS，清理后 1 FAIL；旧断言要求出现已退役 Provider service 依赖，原断言未改 |
| macOS Swift/App build、wheel、签名、设备实测 | 未验证：本机没有 Swift/macOS 工具链，也没有导出的 App 制品 |
| 生产部署/真实任务业务回归 | NOT_RUN；本次只读 catalog/进程核对不代替业务验收 |

16 个原有失败包括：M9 旧 catalog/version/private schema 断言、缺少本机 native OPAQUE 的
节点/配对 fixture、4 个 Host contract subprocess 用例。精确节点和错误见外部 XML/log；
没有删除、跳过或削弱它们。旧 Tunnel 测试是清理后新增的**过时依赖断言失败**，单独列出，
不声称全套测试绿色。

## 剩余历史代码与具体阻塞

- `bridge.py`、`task_registry.py`、`m9_integration.py`：Helper 的
  `node_service_entrypoint.py → CanonicalNodeExecutionAdapter.from_config` 在本地执行配置下
  仍创建 `TaskRegistry`、`TaskDispatcher` 与 `ClinxIntegration`。没有该设备配置和实机
  验证，整包删除会改变其 task/并行/投递/恢复语义。不能根据 P620 已切 Rust 推断设备也已切换。
- `execution_owner.py`：上述兼容实现仍使用其 `owner_caller`、client 和恢复接口；完整服务实现
  还有本地 Owner 回归/资格依赖。源码启动 unit 已移除，整个文件排除在 Rust release 外。
- `canonical_authority.py`、`authority_mapping.py`、`runtime_control/`、`shadow_ledger/`：
  仍有 `execution_owner`、`task_registry`、`desktop_publication` 的明确引用和对应契约测试。
  按 COMPAT/TEST 保留，不能通过删测试把它们宣称无引用。
- `provider_adapters/`、`authority_outbox.py`：没有现役 Rust 业务调用，但现有测试直接验证这些
  契约，标为 TEST；不作为生产构建依赖、不进入两个源码发布集合。
- Rust `runtime_lab::State`：`authority_state.rs` 仍直接使用它承载存储状态；不是仅凭目录名
  就能删的旧实验。`lib.rs` 的协议/解析也被现役 Authority 使用。旧 client/eval/bench 二进制
  只作兼容/测试，生产构建显式选 `--bin clinx-runtime`。未发现有证据可删的 Cargo dependency。

完整移除剩余 Python 业务实现需要先收敛 macOS 本地执行契约，并更新对应历史测试目标；这超出
本次保持已有语义的安全删除范围。本次提交完成了可证明安全的实际清理，不把这些阻塞记为 PASS。

## 证据与恢复

外部证据：`/lab/runtime-worktrees/p620/clinx-cleanup-20261024/`。
保留基准 tracked SHA/行数清单、runtime manifest/argv、引用生成脚本、before/after XML/log、
结果比较、规模清单、打包/发布器 fixture 检查脚本与回执。临时源码副本、测试状态、venv 和
Cargo build 输出在验证后移除。删除的源码可从本提交父版本恢复；不依赖第二个 checkout。
