# CLINX 目标架构与源码布局

唯一目标：**Rust 承担 Task Authority、执行生命周期、持久化、幂等、恢复、MCP、Provider、
Host、Transport、Node 与 Discovery 的长期核心；前端采用适合现有客户端的技术（macOS UI 保留 Swift）；
Python 仅保留尽可能薄的必要兼容层。全仓 Python 源码字节占比必须低于 5%，
开发、构建、资格验证和维护工具同样纳入统计与迁移范围。**

当前实现尚未达到这一目标。Rust 已接管 P620 任务权威、原生执行及 Host 动态工具执行；
Mac 的旧 Bridge/Registry/M9 核心和 Python Dispatcher/Host runner 已删除。
Node 的精确 Provider 请求校验、协议调用和 workspace probe 也已迁 Rust；
正常业务 MCP framing、请求映射和精确 Provider 上下文读取已由 Rust `clinx-mcp` 接管；
Node 的目标校验、canonical handoff、prepare/start/cancel/context 请求映射已由 Rust `clinx-node` 接管；
Node 的 TCP/mTLS、现有配对身份校验、Centre/Owner 授权交集、反向 Authority 路由、
Provider 分块/IPC、Node Registry 持久化及请求去重/恢复也已由 Rust 接管。
原生历史、观察目录与 Collector 队列/checkpoint/ACK 恢复已迁 Rust；Provider Unix RPC/事件/证据判定、
Node 观察路由、Helper 心跳调度、Observer 查询投影、信任记录事务与配对窗口也由 Rust 承担。
Provider 客户端请求契约、默认 Process/SSH/WebSocket、Observer HTTP listener/auth/router、
配置校验与 Workflow 规则、独立 Tunnel 完整生命周期也已迁 Rust。
首次 OPAQUE enrollment、密钥/证书初始化、DNS-SD、配置文件读取、Node/Centre/Helper 装配、
Collector 探针/cache 和进程外层调度现由 Rust 执行。正常 Swift 配对和 Helper 不启动 Python。
Observer、四个观察 MCP 工具和设备 CLI 的正常装配现也由 Rust 直接执行。
Python 仍保留公开 API/DTO、独立协议 oracle 及尚未迁完的构建、资格和维护工具。
旧 SPAKE2 DEV_ONLY 后端已退役；Monitor 构建、Host/MCP 配置编译、策略维护及 Observer 挂载检查已迁 Rust。
当前边界和验证见 [资格与维护迁移](RUST_QUALIFICATION_MIGRATION.md)；
每组 Python 的实际用途见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。
后续跨进程资格与重复离线校验器退役见 [原生资格迁移](RUST_NATIVE_QUALIFICATION_MIGRATION.md)。

## 布局

| 位置 | 当前职责 | 分类与目标 |
| --- | --- | --- |
| `kernel/` | Rust Authority、原生与 Host 执行/Provider worker、状态/持久化/幂等/恢复、MCP stdio/API 与精确上下文读取、stdio byte relay、Node Provider exchange/workspace probe、Node execution API、paired Node TLS/Centre/Owner、Provider gateway/chunks、Node routing registry/ledger | ACTIVE_RUNTIME；长期核心 |
| `native/opaque_pairing/` | Rust OPAQUE 原生配对依赖 | ACTIVE_RUNTIME |
| `MonitorApp/` | Swift macOS UI、App 构建入口、Helper shell launcher | ACTIVE_RUNTIME；UI 留在 Swift |
| `clinx/contracts/` | TOML 配置、Python DTO/错误类型；身份/策略规范化与兼容校验委托 Rust | COMPAT；配置与公开对象兼容 |
| `clinx/authority/` | Python IPC/私有协议客户端 | COMPAT；RustIntegration/Registry view 与 desktop read callback 已删除，实际维护消费者已迁 Rust |
| `clinx/mcp/` | 共享公开 schema 数据 | Python server/factory 已删除；业务、观察和策略维护 profile 均为 Rust |
| `clinx/provider/` | Codex 公开协议 API 与平台 IO；客户端请求契约、默认 Process/SSH/Unix RPC、事件、live-writer/精确证据判断由 Rust 实现 | COMPAT；仍含资格/维护用旧传输公开 API |
| `clinx/host/` | Workflow 公开 DTO；schema/validation/render 已迁 Rust；冻结操作目录仅用于资格验证 | 配置 codec 仍 COMPAT；执行、投递、恢复已迁 Rust |
| `clinx/transport/` | Tunnel 历史公开资格 API；正常 CLI exec Rust | QUALIFICATION；运行服务直接启动 `clinx-tunnel`，不发布此 Python 实现 |
| `clinx/nodes/` | Rust Node/Registry/Provider 公开 Python 客户端、历史 CLI exec 别名 | COMPAT；正常 Helper/Centre 装配、路由/cursor/read 投影/注册心跳及外层调度全部由 Rust 实现 |
| `clinx/discovery/` | 公开身份/配对 DTO、历史设备 CLI exec 和独立 OpenSSL 资格 oracle | COMPAT；正常密钥/证书、信任事务、OPAQUE/TLS enrollment、DNS-SD、GUI helper 全由 Rust 实现 |
| `clinx/monitor/`（已删除） | 原观察 Python 对象/回调/HTTP 封装 | DEAD；资格直接验证 Rust Observer/Node，保留独立 SQL/HTTP oracle |
| `clinx/compat/`（已删除） | 旧 Mac Bridge/Registry/M9 及 completion/recovery/outbox | DEAD；由 Rust Authority 事务与 native Transport 替代 |
| `tools/build/` | 发布源码清单与历史布局资格拷贝 | DEVOPS；正常 App 构建改用 Rust `clinx-build`，配置编译由 `clinx-node` 执行 |
| `tools/maintenance/` | 显式维护工具 | DEVOPS；不进入运行包 |
| `tools/qualification/`、`fixtures/` | 独立验证、参考实现、回归 fixture | QUALIFICATION；不进入运行包 |
| `scripts/` | 构建、候选 release、安装和历史维护入口 | 各文件分类见清单；运行不因文件存在而获得授权 |
| `architecture/` | 源码分类、发布成员、旧路径/符号迁移映射 | 构建与审查事实来源 |
| 根目录 | 项目说明、配置、依赖声明；**没有 Python 模块** | CLI 入口集中于 `bin/` 或限定包的 `-m` 命令 |

`clinx/` 中仍存在的 Python API 均为 COMPAT 或 QUALIFICATION，正常 release 不发布这些模块。
Python 每个模块的 ACTIVE_RUNTIME / COMPAT / DEVOPS / QUALIFICATION / DEAD 分类、用途和
包归属只在 [python-sources.json](../architecture/python-sources.json) 声明。
DEAD 项注明替代实现，源码应不存在。`module-moves.json` 为导入/路径迁移记录，
不是运行时 import alias；生产代码没有平铺旧模块 shim。

`kernel/src/runtime_lab.rs` 仍被 `authority_state.rs` 复用，属于 ACTIVE_RUNTIME。
`tools/qualification/runtime_lab/` 及三条旧阶段资格启动脚本现已退役：它们只相互引用，
依赖已不存在的 Stage3 测试与固定旧源码哈希。现役 execution_core 冻结契约和测试保留。

## 发布规则

Rust release 的 Python 清单现为空。`clinx-provider --desktop-socket` 接管既有 socket/peer UID/
witness thread/inode 验证及固定 alias 发布，原 Python 维护实现已删除。安装器仍是构建/维护工具，
不进入 release，也不在正常服务运行时执行。旧依赖 overlay 全部退出发布，只继承原生 host-worker。
当前 Monitor 使用 `architecture/native-runtime.json`，仅包含 Swift 与三个 Rust Helper，
不再拷贝 Python 模块、解释器、OPAQUE wheel 或 pip 依赖。`clinx-build native-monitor-resources <source-root> <resources>`
清除构建目录中的旧 Python closure 和 DiscoveryRuntime，保留 Swift 资源；不触及已安装应用或用户数据。
旧 `monitor` Python profile / `copy_monitor_sources` 只保留为既有 API/旧布局资格 fixture，
正常 App builder 不再调用它；新增资格验证检查真实当前包中 Python 文件为零。

`scripts/stage_rust_release.py` 需要匹配新布局、已测试的 `--runtime-binary` 和
`--mcp-relay-binary`、`--native-transport-binary`、`--provider-binary`、`--mcp-binary`、`--node-binary`、`--node-daemon-binary`、`--observer-binary`、`--device-binary`。不能只更换 Python 文件再继承引用旧路径的 Authority binary。
清单标记 `source_layout=qualified-clinx-v1`、`host_transport=RUST_HOST_V1`；新服务模板仅接受该布局。
服务直接启动 Rust worker 和 `clinx-mcp`；安装工具把现有 TOML 编译为
`host-runtime.json` 与 `mcp-runtime.json`，运行时不启动配置编译器。
新 release 必须标记 `mcp_runtime=RUST_MCP_V1`、`node_api=RUST_NODE_API_V1`、
`node_network=RUST_NODE_NETWORK_V1`，并包含 `clinx-node`。打包时实际执行
`clinx-node --network-contract` 和 `--observation-contract` 校验二进制；新清单必须有
`observation_runtime=RUST_OBSERVATION_V1`，不能将新客户端与旧 Node binary 混用。
最新客户端同时要求 `thin_runtime=RUST_THIN_RUNTIME_V1` 和实测
`clinx-node --thin-runtime-contract`；旧 binary 不支持新配置/Observer/Provider 契约，构建必须拒绝。
客户端还要求 `client_runtime=RUST_CLIENT_RUNTIME_V1` 和实测
`clinx-node --client-runtime-contract`；同一 marker 覆盖 Provider、Observer、Discovery、
Node router、身份兼容契约及 Tunnel policy。当前设备运行还要求 `device_runtime=RUST_DEVICE_RUNTIME_V1`、`node_daemon=RUST_NODE_DAEMON_V1`；
构建实际检查 Node 和 daemon marker，拒绝缺失新装配入口的旧产物。`architecture/runtime-data.json`
显式声明共享 schema 数据，Rust 在编译时嵌入，Python 观察/维护 codec 从包内读取。
新清单还须声明 `provider_exchange=RUST_PROVIDER_V1`、`workspace_observer=RUST_PROVIDER_V1`，
Authority 从 release 同级的 `clinx-provider` 执行观察。Mac 构建用本机 Rust toolchain 编译
`clinx-provider`、`clinx-node` 与 `clinx-node-daemon`，放入 `Contents/Helpers/` 并分别签名；
Swift 配对直接调用 `clinx-node --discovery-gui`，Helper launcher 直接 exec `clinx-node-daemon helper`。
继承的 `deps/` 禁止夹带另一份 `clinx` 包或已退役的平铺 Python 核心。
候选发布仍为 STAGED_NOT_SELECTED，构建不会选择 production target。

旧 forwarding builder / control overlay 只服务历史平铺产物的证据恢复：对当前源码明确拒绝，
不得绕过清单重新打包旧 Python 核心。需要历史复现时从 Git 原提交恢复精确源码，
不要用新包文件覆盖旧不可变产物。Tunnel supervisor 的独立打包继续有效。

## 项目入口

```sh
# 从源码根目录运行；配置和 socket 必须属于指定的开发/资格环境。
(set -C; <tested-clinx-node> --compile-config mcp <toml> > <new-mcp-json>)
clinx-mcp --config <new-mcp-json> --stdio
# 安装/构建时编译配置，运行时直接启动 Rust；以下均使用独立开发环境。
(set -C; <tested-clinx-node> --compile-config host <config> > <new-host-json>)
clinx-native-transport --socket <authority-socket> --authority-uid <uid> --host-config <new-host-json>
clinx-node-daemon --help
bin/clinx-observer
bin/clinx --help

# stdio relay 本身已改用 Rust，透明转发到已有 MCP Unix socket。
cargo build --locked --release --manifest-path kernel/Cargo.toml --bin clinx-mcp-relay
CLINX_MCP_RELAY=<tested-binary> bin/clinx-context-mcp

# 不访问服务的结构检查。
python -B -m tools.qualification.check_structure
```

MCP 保持 `CLINX_AUTHORITY=rust` 与既有 socket/UID 配置契约。以上命令不是生产安装步骤。
`bin/clinx-context-mcp` 不再调用 Python；Rust relay 只复制字节，保留 stdin 半关闭和
对端 EOF 语义，不解析 MCP、不重试输入、不拥有任务状态。

## 原生执行与剩余运行时

Helper 正常入口是 `clinx-node-daemon helper`，直接 Rust 配置、监听器、NativeReads、
Provider relay、Collector 与 supervisor，不构造 Python Registry/Dispatcher/M9。
Node 目标/scope/canonical 校验、业务映射、OPAQUE 配对与 mTLS 反向请求全部在 Rust，
由 Centre 本地 IPC 调用 Rust Authority。Mac 不使用 Linux Unix socket 或自报 UID 充当远端认证。
Rust `clinx-native-transport` 经 Centre 的 Provider gateway 调用该节点的真实 Codex Provider；
thread/turn/input 证据由 Rust 校验并原子提交。workspace observer 同样按 host 路由，
读取 Mac Provider 中执行的固定只读探针，验证 cwd、Git common-dir identity 与 observer UID。

新线程创建、输入发送和取消许可消费后均不因未知 ACK 重放。恢复只读取精确 Provider 历史。
metadata outbox 与绑定线程在同一 Authority 事务内建立，终态执行仍可发布标题；未知 ACK 先读回。
NodeRegistry 只保留设备/范围、分页与协议去重，不承担 Rust task 的本地 writer fence。
历史 SQLite handoff/projection 修复入口随旧业务库契约退役；离线历史维护需使用对应 Git 提交，
不能将旧修复命令指向 Rust Authority 的数据库。

本轮真实 Mac→P620 Rust→Mac Provider、丢 ACK、重启、取消与 canonical handoff 的证据和限制见
[Rust native 核心退役验证](RUST_NATIVE_CORE_RETIREMENT.md)。既有第五至七轮报告保留其当时状态。

Host 命令校验/构造、子进程、取消/超时、输出取证、工具 ACK 队列及重启恢复已在
`kernel/src/host_executor.rs`、`host_transport.rs` 实现。它们复用现有 Authority 的
take/begin/complete/ACK、successor checkpoint 和取消许可，不持有数据库。
生产子进程继续经过原有 root-controlled `host-worker`；资格直启只由 Authority health 决定。
未知输入、未知创建 ACK 或未知子进程结果均不重放。验证范围见
[Rust Host 迁移报告](RUST_HOST_RUNTIME_MIGRATION.md)。

`clinx-mcp` 的 15 个业务工具直接使用 Rust 请求映射、Authority IPC 与 Provider 精确只读契约。
4 个观察工具在同一 Rust 进程中调用 `observation_directory`，复用原有 trust/project 授权交集和读后重检。
本机 managed context 直接调用 Rust `mcp_api::Api`，不回读 MCP/Observer；独立只读入口为
`clinx-mcp --observations-only --stdio`，无 Authority 配置也能提供原四个只读工具。
详见 [Rust MCP 迁移报告](RUST_MCP_RUNTIME_MIGRATION.md)。

Node 配对后的 TLS、Centre/Owner scope、反向请求路由、Provider 分块/IPC、设备 Registry
及执行请求恢复已由 Rust 实现；正常服务装配也已迁 Rust，公开 Python 对象仅为兼容 API；详见
[Rust Node 网络迁移](RUST_NODE_NETWORK_MIGRATION.md)。

原生历史 SQL、JSONL 解析、fork/projection/turn 身份证明、增量历史 checkpoint，以及
观察目录 SQL、批次摘要/去重、stream CAS、终态合并、保留策略、签名游标和查询投影，
已迁入 `native_history.rs`、`observation.rs` 和 `read_projection.rs`。
Python `NativeHistory` 仍使用 `clinx-node` 私有 stdio 通道；`ObservationWriter` 和
`ObservationDirectory` 的 Python 接口已随 Monitor 兼容闭包退役，资格直接调用 Rust。
本地原生索引只读，观察数据库只写展示投影，均不承担任务权威。首次迁移记录见
[Rust 历史与观察目录迁移](RUST_OBSERVATION_MIGRATION.md)。

Collector 的 spool/outbox、调度决策、checkpoint/ACK 事务、采样合并及历史 canonical SQL
已迁 Rust，见 [Collector 迁移](RUST_COLLECTOR_MIGRATION.md)。

**剩余 Python 边界**：公开 DTO/API/异常与私有 stdio 协议客户端；独立 OpenSSL 互操作
及 Python 对象接口资格；尚未迁完的构建、资格和维护工具。旧 SPAKE2 实现已删除。正常 Observer、观察 MCP、devices/pair/serve 已无 Python 装配。
现役测试和原回归断言保留；不能为降低比例删除现役资格链。
新设备运行链不调用 Python enrollment、DNS-SD、Collector live probe/cache 或 Centre 外层调度。
Collector 队列和 Task Authority metadata outbox 仍分别由 Rust 持久化，无第二套任务权威。
跨 macOS 实机、原生 App 构建签名与真实 LAN 多播验证边界见 [本轮报告](RUST_DEVICE_RUNTIME_MIGRATION.md)。
全仓 Python <5% 仍是未完成目标；每组用途、发布范围见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。

最新入口与清单额外要求 `observation_directory=RUST_OBSERVATION_DIRECTORY_V1`、
`observer_runtime=RUST_OBSERVER_V1`、`device_cli=RUST_DEVICE_CLI_V1`；stager 实测三个二进制 marker。
验证、删除边界与剩余工具迁移见 [Observer 与设备入口迁移](RUST_OBSERVER_RUNTIME_MIGRATION.md)。

发布还要求 `desktop_socket_maintenance=RUST_DESKTOP_SOCKET_V1`，Python源码与依赖数组必须为空。
现役 Python 资格分批迁入 `kernel/tests/*_qualification.rs`；原参数/断言映射与必要绑定smoke
见 [资格与维护迁移](RUST_QUALIFICATION_MIGRATION.md)，不能通过取消测试降低统计量。
Native Transport、Host、Node 路由和 Provider 的后续逐用例映射见
[native-qualification-migrations.json](../architecture/native-qualification-migrations.json)。
冻结契约的离线入口为 `cargo run --locked --manifest-path kernel/Cargo.toml --example check_execution_core_contract`；
它复用现役 Rust `execution_core`，不构造第二状态机或发起运行时操作。

本轮进一步删除最后 Python MCP factory、维护业务入口和旧 SPAKE2 后端；
Monitor 构建、Host/MCP 配置编译及 Observer inode guard 改用 Rust。
实际消费者、原回归映射和全量既有失败区分见 [兼容实现退役](RUST_COMPAT_RETIREMENT.md)。

配置安装另要求 `config_compiler=RUST_CONFIG_COMPILER_V1`，stager 实际探测
`clinx-node --config-compile-contract`；安装器在任何修改前拒绝缺少此契约的旧 release。
`clinx-build` 仅是开发工具，不进入 release 或 App 的三个 Helpers。

最新维护入口为开发工具 `clinx-maintenance transfer-native-session`，复用原有身份边界与
Provider/Authority 契约，明确目标 Provider home，不进入正常 release/App。
Rust `local_discovery_smoke` example 提供显式 LAN 资格及隔离 loopback 模式。
完整删除与证据见 [维护与观察兼容收敛](RUST_MAINTENANCE_CONVERGENCE.md)。
