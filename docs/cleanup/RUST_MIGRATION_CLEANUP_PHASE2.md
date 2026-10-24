# Rust 迁移后第二轮源码清理（2026-10-24）

> 本文保留第二轮基线。第三轮进一步删除旧 Linear 轮询/Dispatcher、收口源码入口，
> 并补做干净 Rust 和 macOS 构建；当前结果见[第三轮记录](RUST_MIGRATION_CLEANUP_PHASE3.md)。

基准 `94f1ef96495b3b20547a41d1000582705a52344f`，canonical 仓库
`/home/pvxlabs/dev/clinx`，分支 `main`。开始时受控工作区干净。本轮在第一轮发布边界
清理之上，实际删除 Python Authority、allocator、事务叠加层及 prototype Provider。
未部署、未重启现役服务、未修改设备配置、未操作现役任务或写入生产数据库。
原有忽略的 `tests/` 129 个文件全部保持原字节，没有强制纳入 Git。

## 删除范围与依据

整文件删除 **20 个、7,255 物理行**；另清理调用方死分支，Python 源码净减
**7,631 行**。与第一轮累计整文件删除 28 个、7,524 行；累计 Python 净减 7,737 行。
物理行包括注释和空行，移动或保留的兼容代码没有计作删除。

| 删除项 | 文件数 | 依据 |
| --- | ---: | --- |
| `canonical_authority.py`、`authority_mapping.py`、`authority_writer.py`、`authority_outbox.py`、`authority_transactions.py` | 5 | 已替代的 Python Authority、映射、writer、outbox 与借用事务实验；现役 Rust 不导入，设备 DB 无该 overlay 的 schema/fence |
| `runtime_control/` 全部受控 Python 源码 | 9 | Python allocator/epoch/lease/replay/transport 实验；对应 Authority 入口退役 |
| `provider_adapters/` 全部受控 Python 源码 | 6 | prototype Provider 契约与 scripted 实验，不是现役 `native_provider.py`/Desktop Provider 链 |

`execution_owner.py` 删除实验 Authority 对象、execution token、claim/renew/commit/finalize
桥接和实验服务参数；保留普通兼容 Owner/client、有限 domain 调用和原恢复逻辑。
客户端旧 receipt 方法保留 endpoint 兼容/负向测试接口，普通服务对其请求在写入前返回
`EXECUTION_OWNER_INVALID_REQUEST`，不会回退到本地业务写入。
`mcp_server.py` 删除 `--authority-transport` 及对应激活路径，普通兼容 `--execution-owner` 保留。
Registry、liveness、M9 finalizer 和结果摄取只删除实验借用事务分支；已有本地事务和身份、
并行、投递、ACK、幂等、恢复及状态规则保留。

## 实际运行核对

| 组件 | 只读核对结果 |
| --- | --- |
| P620 Rust Authority | active，PID `1023787`，`rust-73ad2dc733578a1e8947/clinx-runtime`，`NRestarts=0` |
| P620 Dispatcher | active，PID `1024186`，`rust-c09215686ef963f9067f/control/rust_transport.py` |
| P620 Centre | active，PID `1024191`，`rust-6c0ba78d0ba4d7960131/control/node_centre_entrypoint.py --rust-projection` |
| P620 Observer / Tunnel | active，PID `775202` / `3901220`；与第一轮结束时一致 |
| 原 Python Dispatcher/Owner/Provider/Centre services | masked / inactive；没有恢复它们 |
| iMac Helper | PID `26275`，已安装 App 的 `node_service_entrypoint.py --node-service`；sharing `execute_tasks=true` |
| Air Helper | PID `76622`，同一 Helper 入口；sharing `execute_tasks=false`，仍有已配置的本地执行路径 |

已核对实际进程 argv、当前 release manifest 身份、Python 引用和 Monitor 打包输入。
Dispatcher/Centre 本来已有 `NRestarts=1`，本轮未增加，也不声称历史上从未重启。
上述是本轮实际 systemd 读回，不能从其他文档记录的 release 名推断当前进程身份。

两台 Mac 的配置分别指向 `native-execution.toml` / `air-node-execution.toml`。
其任务数据库只以 SQLite `mode=ro`、`query_only=ON` 查询 schema 名称，均没有
`authority_%`、`runtime_%` 表或 `clinx_fence_%` schema。实际 iMac 本地配置经
`CanonicalNodeExecutionAdapter.from_config` 创建普通 Registry/Dispatcher/Integration，
因此不能整包删除 `bridge.py` / `task_registry.py` / `m9_integration.py`。
本机 helper 的普通 Python 执行契约仍须兼容；不能把 P620 Rust 切换等同于所有设备已切换。

## 引用分类与保留 Python 的用途

[第二轮逐文件清单](rust-migration-inventory-phase2.json) 覆盖 365 个代码、脚本、服务、
构建配置及本地测试文件：ACTIVE 141、COMPAT 41、TEST 163、LEGACY 20（已删除）；
未发现另外可证明安全删除的 UNUSED 项。每项记录双向引用、分类理由、SHA、行数和发布归属。
删除模块仍保留其测试入站引用，避免把“仅测试引用”伪装成“没有任何引用”。
条件导入、文件字面量和 Swift 符号关系为保守分析；ACTIVE 不表示文件中每个分支都在生产执行。

根目录剩余 **60 个 `.py`**，分为 40 个 Rust/discovery adapter 清单模块、14 个 Monitor
本地执行兼容模块和 6 个发布/维护/资格模块。根目录平铺布局尚在，文件仍有实际职责：

| 保留范围 | 实际用途 |
| --- | --- |
| `mcp_server`、`rust_mcp`、`rust_authority`、`rust_integration` | 公共 MCP schema、Rust IPC 与请求适配；P620 状态 commit 归 Rust |
| `app_server`、`native_provider`、`rust_transport`、`rust_host`、`host_executor`、`tool_delivery` | Codex Provider/Host 传输、结果和投递适配；不是被删的 prototype Provider |
| `rust_reads`、`rust_monitor`、`rust_observation`、`observer_server`、`observation_source`、`native_history` 等 | 原生历史、精确身份、只读投影与观察 |
| `node_protocol`、`node_runtime`、`network_observation`、`native_observation_bridge`、`local_discovery/` | Helper、设备发现、配对、节点路由及观察适配 |
| `runtime_config`、`runtime_contracts`、`execution_policy`、`execution_semantics`、`host_contract` 等 | 配置、共享 DTO、worktree 身份算法和协议能力映射 |
| `bridge`、`task_registry`、`m9_integration`、`execution_owner`、`completion_runtime`、`native_lifecycle`、`node_execution_adapter` | 实际设备本地执行兼容链；不进入 Rust release |
| `desktop_publication`、`historical_handoff`、`native_input_recovery`、`prepared_requests`、`result_ingestion`、`task_projection`、`unstarted_recovery` | 上述链的投影、prepared seal、发布、摄取及恢复；不进入 Rust release |
| `domain/`、`shadow_ledger/` | 兼容注册/投影/发布使用的模型与账本；不是无引用的重复 Rust Authority |
| `runtime_packaging`、`tunnel_reliability`、`observer_history_guard` | 统一构建清单、现役 Tunnel watchdog、Observer 历史挂载维护 |
| `policy_maintenance`、`embedded_bridge_readiness`、`provider_qualification` | 发布器保留的维护接口、只读 idle 检查和 native Provider 资格；不作新 Authority |

## 发布依赖和规模

Monitor 的统一清单去掉四个 Authority 根模块和整个 `runtime_control` 包。
`MonitorApp/Scripts/build-app.sh` 继续使用第一轮接入的 `runtime_packaging.py`，无需重复清单。
复制器会删除本次构建资源中残留的 retired `.py`、`runtime_control/`、`provider_adapters/`，
保留 DiscoveryRuntime、Swift 资源和必要 `domain/`、`shadow_ledger/`。
Rust release 继续只包含 Rust adapter 集合，未重新引入 Python 业务实现。
本轮没有发现另外可证无用的 Cargo 依赖，Rust/Swift 源码及依赖声明字节均未变化。

| 范围 | 本轮前文件 / 物理行 | 本轮后文件 / 物理行 |
| --- | ---: | ---: |
| 受控 Python | 137 / 51,044 | 117 / 43,413 |
| 根目录 Python | 65 / 35,253 | 60 / 33,297 |
| Rust | 20 / 9,416 | 20 / 9,416 |
| Swift | 62 / 11,355 | 62 / 11,355 |
| Rust release Python 源码清单 | 47 / 16,670 | 47 / 16,633 |
| Monitor Python 源码清单 | 87 / 41,261 | 75 / 35,965 |

仓库规模只计受控源码，不含忽略测试、缓存和构建产物。两种发布清单均不含另行复制的
`node_service_entrypoint.py`，前后口径一致。删去无效实验没有按语言占比重写有效适配层。

## 验证与未解决项

| 验证 | 状态 / 结果 |
| --- | --- |
| 21 个选定 Python 模块前后回归 | PASS（结果等价）：前后均 326 PASS、17 个既有失败、44 subtests PASS；XML 测试身份及结果多重集相同 |
| liveness / 结果摄取 / native input recovery / shadow ledger 补充回归 | PASS：前后均 124 PASS、8 subtests PASS；结果多重集相同 |
| 独立 Rust 源码包导入、配置、Host 身份和 MCP catalog | PASS；无旧 Registry/Dispatcher/Authority 导入 |
| 独立 Rust 源码包请求/路由回归 | PASS，36/36 |
| Monitor 临时源码包 | PASS；发现、观察、Rust adapter、本地兼容链导入及增量打包残留清理 |
| iMac / Air App 内置 Python 3.13.3 | PASS：同一临时源码包 SHA；Helper/Provider 导入、临时 SQLite 事务、旧 Authority 请求拒绝、无实验表/导入 |
| 真实 release staging 函数的临时 fixture | PASS；完整源码刷新、旧模块排除、native/deps SHA 保留、manifest SHA 和 base 漂移拒绝 |
| Rust Core / 集成 / release build | 复用第一轮 21 + 25 PASS、locked/offline release build PASS；本轮 Rust 20 个文件 SHA 全同，未重复构建 |
| 未定义符号、shell 语法、diff 空白检查 | PASS |
| 全仓 Python 收集 | BLOCKED：基准 1,967；本轮 1,714 + 9 个 retired 模块导入错误；未隐藏 |
| Swift/App 完整 build、签名、GUI、真实任务调用链 | 未验证；源码未改，临时 Python bundle 实测不等于完整 App build 或业务验收 |
| 部署、生产任务写操作 | NOT_RUN；源码提交和推送不激活候选 |

17 个既有失败：旧 M9 catalog/version/private schema 断言、P620 缺少 native OPAQUE 的
节点/配对 fixture、Host contract subprocess fixture、既有
`test_owner_callers.py::test_rust_pinned_owner_uid_rejects_foreign_endpoint`。
精确节点和原始错误保存在前后 XML/log，没有改断言或跳过它们。
第一轮已有的旧 Tunnel 模板断言仍要求退役 Provider service，未在本轮修正或重新宣称通过。

以下 9 个本地测试模块直接验证被删实验，仍保留原文件，因此全量收集报错：
`test_authority_closure.py`、`test_canonical_authority.py`、`test_owner_authority_transport.py`、
`test_owner_business_commit.py`、`test_provider_adapters.py`、`test_pvx1806_remediation.py`、
`test_pvx1807_remediation.py`、`test_runtime_authority_transport.py`、`test_runtime_control.py`。
减少的 253 个 collected cases 来自这些模块；后续测试目标收敛需要保留其唯一契约覆盖，
不能仅删掉它们换取绿色。本地历史 profile 的现状已在测试指南说明。

整包移除剩余 Python 业务实现仍为 **BLOCKED**：iMac 配置仍启用本地 Python 执行，
不能在本次不变更任务语义、不部署的约束下直接退役。Rust `runtime_lab::State` 仍被
`authority_state.rs` 导入；`lib.rs` 的协议/解析也被现役实现使用。Rust client/eval/bench
为兼容和资格入口，生产构建显式选择 `--bin clinx-runtime`，没有猜测性删除。

## 证据与恢复

本轮证据位于 `/lab/runtime-worktrees/p620/clinx-cleanup2-20261024T1130Z/`，包含 tracked
前后 SHA/规模、129 个测试文件 hash、引用生成脚本、前后回归 XML/log、全量收集输出、
临时发布 fixture、两台 Mac 只读配置/schema/进程核对和源码 smoke 回执、systemd/release
身份、Git 提交/推送回执。首次 Mac smoke 的 `/var` 符号链接判断属于测试脚本路径错误，
修正后两台均通过，未改仓库代码。临时 Mac 源码/DB 自动移除；本地任务拥有的源码快照、
venv、构建 fixture 和测试临时状态在验证后删除，独立日志和压缩证据保留。
删除源码可从父提交恢复，不依赖另一个长期 checkout。
