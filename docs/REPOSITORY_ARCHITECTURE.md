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
Python 仍承载平台装配、mDNS/初次 enrollment、公开对象兼容及部分采样与 Centre 外层调度。
当前边界和验证见 [薄兼容层迁移](RUST_THIN_RUNTIME_MIGRATION.md)；
每组 Python 的实际用途见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。

## 布局

| 位置 | 当前职责 | 分类与目标 |
| --- | --- | --- |
| `kernel/` | Rust Authority、原生与 Host 执行/Provider worker、状态/持久化/幂等/恢复、MCP stdio/API 与精确上下文读取、stdio byte relay、Node Provider exchange/workspace probe、Node execution API、paired Node TLS/Centre/Owner、Provider gateway/chunks、Node routing registry/ledger | ACTIVE_RUNTIME；长期核心 |
| `native/opaque_pairing/` | Rust OPAQUE 原生配对依赖 | ACTIVE_RUNTIME |
| `MonitorApp/` | Swift macOS UI、App 构建入口、Helper shell launcher | ACTIVE_RUNTIME；UI 留在 Swift |
| `clinx/contracts/` | TOML 配置、Python DTO/错误类型；身份/策略规范化与兼容校验委托 Rust | ACTIVE_RUNTIME；配置与公开对象兼容 |
| `clinx/authority/` | 当前 Python IPC/配对请求转发、兼容 API 与只读 Provider 协议客户端 | ACTIVE_RUNTIME；业务请求选择与投影已迁 Rust |
| `clinx/mcp/` | 观察/显式维护用兼容 codec、Rust CLI launcher、共享公开 schema 数据 | 观察 codec 仍 ACTIVE_RUNTIME；正常业务入口已迁 Rust |
| `clinx/provider/` | Codex 公开协议 API 与平台 IO；客户端请求契约、默认 Process/SSH/Unix RPC、事件、live-writer/精确证据判断由 Rust 实现 | ACTIVE_RUNTIME；仍含资格/维护用旧传输公开 API |
| `clinx/host/` | Workflow 公开 DTO；schema/validation/render 已迁 Rust；冻结操作目录仅用于资格验证 | 配置 codec 仍 ACTIVE_RUNTIME；执行、投递、恢复已迁 Rust |
| `clinx/transport/` | Tunnel 历史公开资格 API；正常 CLI exec Rust | QUALIFICATION；运行服务直接启动 `clinx-tunnel`，不发布此 Python 实现 |
| `clinx/nodes/` | Rust Node/Registry/Provider 客户端、配置与 IO 装配 | ACTIVE_RUNTIME；路由/cursor/read 投影/Helper 注册心跳调度由 Rust 实现 |
| `clinx/discovery/` | 本机密钥与证书建立、OPAQUE enrollment/mDNS IO 与 Helper 设置 | ACTIVE_RUNTIME；信任事务/窗口/候选索引规则由 Rust 实现 |
| `clinx/monitor/` | Observer HTTP/codec 与身份/协议装配；历史/查询/精确证据/checkpoint/投影委托 Rust | ACTIVE_RUNTIME；HTTP listener/auth/router 已迁 Rust；采样探针/cache 装配仍为 Python |
| `clinx/compat/`（已删除） | 旧 Mac Bridge/Registry/M9 及 completion/recovery/outbox | DEAD；由 Rust Authority 事务与 native Transport 替代 |
| `tools/build/` | 共用运行源码清单处理与增量 App 拷贝 | DEVOPS |
| `tools/maintenance/` | 显式维护工具 | DEVOPS；不进入运行包 |
| `tools/qualification/`、`fixtures/` | 独立验证、参考实现、回归 fixture | QUALIFICATION；不进入运行包 |
| `scripts/` | 构建、候选 release、安装和历史维护入口 | 各文件分类见清单；运行不因文件存在而获得授权 |
| `architecture/` | 源码分类、发布成员、旧路径/符号迁移映射 | 构建与审查事实来源 |
| 根目录 | 项目说明、配置、依赖声明；**没有 Python 模块** | CLI 入口集中于 `bin/` 或限定包的 `-m` 命令 |

Python 每个模块的 ACTIVE_RUNTIME / COMPAT / DEVOPS / QUALIFICATION / DEAD 分类、用途和
包归属只在 [python-sources.json](../architecture/python-sources.json) 声明。
DEAD 项注明替代实现，源码应不存在。`module-moves.json` 为导入/路径迁移记录，
不是运行时 import alias；生产代码没有平铺旧模块 shim。

`kernel/src/runtime_lab.rs` 仍被 `authority_state.rs` 复用，属于 ACTIVE_RUNTIME。
`tools/qualification/runtime_lab/` 是 Python/探针资格实现，两者不能因为名称相似一并删除。

## 发布规则

Rust release 和 Monitor bundle 使用同一份 `architecture/python-sources.json`，由
`tools/build/runtime_sources.py` 校验。Rust profile 禁止 COMPAT/DEVOPS/QUALIFICATION。
Monitor profile 同样禁止 COMPAT；新增文件不会通过目录 glob 自动进入包。

运行包保留 `clinx/<职责>/<模块>.py` 层级及 package initializer。源码根目录或 release 的
`control/` 是 Python import root；Helper 使用 App Resources 作为 import root。
包内没有源码仓库、`tools/`、本地测试、旧平铺 `.py` 或 `Discovery/` 副本。
增量 App 构建清除旧 Python 拷贝目录，保留解释器、原生 wheel 和 Swift 资源。

`scripts/stage_rust_release.py` 需要匹配新布局、已测试的 `--runtime-binary` 和
`--mcp-relay-binary`、`--native-transport-binary`、`--provider-binary`、`--mcp-binary`、`--node-binary`。不能只更换 Python 文件再继承引用旧路径的 Authority binary。
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
Node router、身份兼容契约及 Tunnel policy。Monitor 源码 manifest 同样声明这些契约和所需的两个 Rust Helper。`architecture/runtime-data.json`
显式声明共享 schema 数据，Rust 在编译时嵌入，Python 观察/维护 codec 从包内读取。
新清单还须声明 `provider_exchange=RUST_PROVIDER_V1`、`workspace_observer=RUST_PROVIDER_V1`，
Authority 从 release 同级的 `clinx-provider` 执行观察。Mac 构建用本机 Rust toolchain 编译
`clinx-provider` 与 `clinx-node`，放入 `Contents/Helpers/` 并分别签名；Helper launcher 固定这两个路径。
继承的 `deps/` 禁止夹带另一份 `clinx` 包或已退役的平铺 Python 核心。
候选发布仍为 STAGED_NOT_SELECTED，构建不会选择 production target。

旧 forwarding builder / control overlay 只服务历史平铺产物的证据恢复：对当前源码明确拒绝，
不得绕过清单重新打包旧 Python 核心。需要历史复现时从 Git 原提交恢复精确源码，
不要用新包文件覆盖旧不可变产物。Tunnel supervisor 的独立打包继续有效。

## 项目入口

```sh
# 从源码根目录运行；配置和 socket 必须属于指定的开发/资格环境。
python -m tools.build.mcp_runtime_config --node-binary <tested-clinx-node> --config <toml> --output <new-mcp-json>
clinx-mcp --config <new-mcp-json> --stdio
# 安装/构建时编译配置，运行时直接启动 Rust；以下均使用独立开发环境。
python -m tools.build.host_runtime_config --config <config> --output <new-host-json>
clinx-native-transport --socket <authority-socket> --authority-uid <uid> --host-config <new-host-json>
python -m clinx.nodes.node_centre_entrypoint --help
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

Helper 默认通过 `RustNodeExecutionAdapter.from_config` 调用 `clinx-node`，不构造 Python
`RustIntegration`、Registry 或 Dispatcher。Node 目标/scope/canonical 校验与所有业务映射在 Rust；
Rust 子进程的 Authority 请求由 Python 经已有 OPAQUE 配对与 mTLS 转发 Centre，
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
4 个观察工具暂委托同 API 身份的只读目录子进程；它使用原有 trust/project 授权交集和读后重检。
本机 managed context 返回 Rust `--call clinx_get_context`，不回读 MCP/Observer 造成循环，
不构造 Python `RustIntegration`。该子进程没有新 listener、service 或任务执行权限。
详见 [Rust MCP 迁移报告](RUST_MCP_RUNTIME_MIGRATION.md)。

Node 配对后的 TLS、Centre/Owner scope、反向请求路由、Provider 分块/IPC、设备 Registry
及执行请求恢复已由 Rust 实现，Python 仍装配现有身份对象、监听器与回调；详见
[Rust Node 网络迁移](RUST_NODE_NETWORK_MIGRATION.md)。

原生历史 SQL、JSONL 解析、fork/projection/turn 身份证明、增量历史 checkpoint，以及
观察目录 SQL、批次摘要/去重、stream CAS、终态合并、保留策略、签名游标和查询投影，
已迁入 `native_history.rs`、`observation.rs` 和 `read_projection.rs`。
Python `NativeHistory`、`ObservationWriter` 和 `ObservationDirectory` 使用现有
`clinx-node` 私有 stdio 通道；本地原生索引只读，观察数据库只写展示投影，均不承担任务权威。
旧 Python 实现已移除，保留 public 调用接口；详见
[Rust 历史与观察目录迁移](RUST_OBSERVATION_MIGRATION.md)。

Collector 的 spool/outbox、调度决策、checkpoint/ACK 事务、采样合并及历史 canonical SQL
已迁 Rust，见 [Collector 迁移](RUST_COLLECTOR_MIGRATION.md)。

**仍是 Python 运行时**：公开客户端 DTO/API 与错误编解码；TOML 和已安装配置读取；
Observer HTTP 路由、mDNS IO、初次 OPAQUE enrollment 的 Python TLS/协议编排；
本机密钥/证书 bootstrap、Collector live probe/cache 装配、Centre 外层 tick 及 Tunnel
owned-process/信号 IO。Provider 子进程/SSH 资格 API 仍有 Python 兼容实现，未以新名称隐藏。
Discovery 密码算法继续用既有 Rust OPAQUE 原生库；这不等于 enrollment 网络 IO 已全迁 Rust。
具体逐文件职责、发布范围和剩余收敛项见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。
Collector 队列和 Task Authority metadata outbox 分别由 Rust 持久化；Python 没有第二套任务状态。
`rust_integration.py` 已删除重复业务判断，保留原接口委托 Rust、对象视图和只读 Provider 协议回调；
正常 Helper 不调用该兼容接口，直接使用 Rust NativeReads。它与配对转发客户端仍为 ACTIVE_RUNTIME，不能标为开发工具。
本轮协议、原 Python 行为向量、完整套件前后对照和 Mac 验证边界见
[Rust Node API 迁移](RUST_NODE_API_MIGRATION.md)。
这些明确未完成迁移，25% 目标尚未达到。
Python 的 `host_contract.py` 仅作为冻结资格参考保留，不进入 Rust release 或 Helper 包。

`clinx-provider` 已接管 Node 对精确 thread/turn/execution/prompt/cwd 的请求校验与
Provider RPC；配对 mTLS 和 chunk 去重已由 Rust 接管，Python `NodeProviderRelay` 保留既有协议对象接口。
`clinx/provider/rust_workspace_probe.py` 已删除，固定 Git 元数据命令及 UID 证据校验在
`kernel/src/workspace_probe.rs`。隔离回归、发布包验证及 Mac 未验证边界见
[Rust Provider/workspace 迁移报告](RUST_PROVIDER_RUNTIME_MIGRATION.md)。

## 远端路由配置契约

Centre `serve --provider-gateway <absolute-path>` 可以重复，为不同节点建立独立 IPC endpoint；
每个 endpoint 保留既有 Transport UID，observer UID 仅可执行固定 `workspace/inspect`。
跨 Linux UID 时由 `--provider-gateway-gid` 指定既有共享 IPC 组（如 `clinx-ipc`），不修改账号成员关系。
Authority `provider_routes` 继续要求 endpoint 归属唯一，不放宽为多个 host 共享同一地址。
Transport 与 observer 使用 `CLINX_RUST_PROVIDER_GATEWAYS`（JSON host→绝对 socket 路径映射）
及 `CLINX_RUST_PROVIDER_GATEWAY_UID`；worker 转换为重复的 `--provider-route HOST PATH`。
单节点旧配置 `CLINX_RUST_REMOTE_HOSTS` 与 `CLINX_RUST_PROVIDER_GATEWAY` 继续有效。
这些是待部署的配置接口，本轮未安装/选择 release 或重启生产服务。

## 全仓 Python 低于 5% 的验收

`clinx-source-audit` 是只读 Rust 工具，固定使用 Python/Rust/Swift/Go/C/Shell 六语言
物理源码字节口径，包含 tracked 和非 ignored 新源码中的测试、构建及维护工具。
不更改 Linguist、不排除 Python 资格代码、不把生成代码加入分母。
同时输出物理行数、分类规模和根目录 Python 列表。

```sh
<task-target>/release/clinx-source-audit --revision HEAD
<task-target>/release/clinx-source-audit
```

默认严格小于 5% 才退出 0；未达标退出 2，工具错误退出 1。当前目标仍未达成。
运行包不含旧核心、局部迁移 PASS 和全仓占比达标是三个独立验收项。
资格工具占有显著 Python 规模，必须保留测试身份、断言和覆盖地迁移到 Rust；
不能通过删除测试或调整统计口径达标。
