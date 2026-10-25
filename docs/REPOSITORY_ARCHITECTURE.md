# CLINX 目标架构与源码布局

唯一目标：**Rust 承担 Task Authority、执行生命周期、持久化、幂等、恢复、MCP、Provider、
Host、Transport、Node 与 Discovery 的长期核心；Swift 负责 macOS UI；Python 最终仅保留
必要的开发、构建、资格验证和维护工具。**

当前实现尚未达到这一目标。Rust 已接管 P620 任务权威、原生执行及 Host 动态工具执行；
Mac 的旧 Bridge/Registry/M9 核心和 Python Dispatcher/Host runner 已删除。
Node 的精确 Provider 请求校验、协议调用和 workspace probe 也已迁 Rust；
MCP、Node/Discovery 网络协议、配对传输及监控读取仍有 Python 运行时。

## 布局

| 位置 | 当前职责 | 分类与目标 |
| --- | --- | --- |
| `kernel/` | Rust Authority、原生与 Host 执行/Provider worker、状态/持久化/幂等/恢复、stdio byte relay、Node Provider exchange/workspace probe | ACTIVE_RUNTIME；长期核心 |
| `native/opaque_pairing/` | Rust OPAQUE 原生配对依赖 | ACTIVE_RUNTIME |
| `MonitorApp/` | Swift macOS UI、App 构建入口、Helper shell launcher | ACTIVE_RUNTIME；UI 留在 Swift |
| `clinx/contracts/` | 公共配置、身份选择、策略与响应契约 | ACTIVE_RUNTIME；运行职责待迁 Rust |
| `clinx/authority/` | 当前 Python Rust IPC 客户端、请求映射、状态读取 | ACTIVE_RUNTIME；不等于 Rust Authority 实现 |
| `clinx/mcp/` | 当前 Python MCP framing/schema | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/provider/` | Codex 协议、原生历史、精确证据读取和观测判断 | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/host/` | 安装配置的 workflow schema；冻结的操作目录参考仅用于资格验证 | 配置解析仍 ACTIVE_RUNTIME；执行、投递、恢复已迁 Rust |
| `clinx/transport/` | Tunnel supervisor | ACTIVE_RUNTIME；待迁 Rust；Python Dispatcher 已删除 |
| `clinx/nodes/`、`clinx/discovery/` | Node 路由、注册、Helper、mDNS/配对 | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/monitor/` | 监控与观察投影、只读历史 association | ACTIVE_RUNTIME；运行职责待迁 Rust |
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
`--mcp-relay-binary`、`--native-transport-binary`、`--provider-binary`。不能只更换 Python 文件再继承引用旧路径的 Authority binary。
清单标记 `source_layout=qualified-clinx-v1`、`host_transport=RUST_HOST_V1`；新服务模板仅接受该布局。
服务直接启动 Rust worker，安装工具把现有 TOML Host 配置编译为 `host-runtime.json`。
新清单还须声明 `provider_exchange=RUST_PROVIDER_V1`、`workspace_observer=RUST_PROVIDER_V1`，
Authority 从 release 同级的 `clinx-provider` 执行观察。Mac 构建用本机 Rust toolchain 编译
同一入口，放入 `Contents/Helpers/clinx-provider` 并单独签名；Helper launcher 固定该路径。
继承的 `deps/` 禁止夹带另一份 `clinx` 包或已退役的平铺 Python 核心。
候选发布仍为 STAGED_NOT_SELECTED，构建不会选择 production target。

旧 forwarding builder / control overlay 只服务历史平铺产物的证据恢复：对当前源码明确拒绝，
不得绕过清单重新打包旧 Python 核心。需要历史复现时从 Git 原提交恢复精确源码，
不要用新包文件覆盖旧不可变产物。Tunnel supervisor 的独立打包继续有效。

## 项目入口

```sh
# 从源码根目录运行；配置和 socket 必须属于指定的开发/资格环境。
python -m clinx.mcp.mcp_server --config <config> --stdio
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

Helper 默认创建 `RustNodeExecutionAdapter`，经已有 OPAQUE 配对与 mTLS 请求 Centre，
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

**仍是 Python 运行时**：MCP framing/schema、Node/Discovery 注册配对与网络协议、Provider mTLS 分块通道、
历史读取与监控投影及公共请求/配置映射。这些不是纯开发工具，仍需后续迁移。
Python 的 `host_contract.py` 仅作为冻结资格参考保留，不进入 Rust release 或 Helper 包。

`clinx-provider` 已接管 Node 对精确 thread/turn/execution/prompt/cwd 的请求校验与
Provider RPC；配对 mTLS 和 chunk 去重仍在 Python `NodeProviderRelay`，不是纯构建工具。
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
