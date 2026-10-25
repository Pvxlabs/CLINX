# 配置、使用、发布与运维

用途：当前入口和运维边界。适用范围：源码开发、候选构建与另行授权的安装维护。
状态：CURRENT。验证：参数解析器、安装模板及[测试指南](TESTING.md)；本文不确认当前部署。

## 前提与配置来源

Rust 1.98+、Git、C linker 为 Rust 构建前提；OpenSSL CLI 用于独立 wire tests。
macOS App 需 macOS 13+、Xcode/Swift 与原生 Mac Rust 工具链；普通 Rust 构建无 Python 或 Go 前提。
固定外部 Transport 的重建另外需要 builder 指定 SHA 的 Go/source 归档，见[ADR-007](architecture/ADR-007-rust-source-policy.md)。

[bridge.toml](../bridge.toml) 是带有既有主机、项目及历史配置字段的受控输入，**不是通用安装默认值**。
使用私有副本并核对 host、project、repository origin/branch、Provider endpoint 和路径。
`linear` 与旧 `codex.sandbox` 等字段不能覆盖当前 native forwarding 的完整访问语义。
配置读取/编译的唯一实现是 [runtime_config](../kernel/src/runtime_config.rs) 和
[native config IO](../kernel/src/runtime_config/native.rs)，Host/MCP 使用同 release 的编译器：

```sh
# 仅编译自己准备的 TOML；输出新文件，不修改已安装配置。
"$CARGO_TARGET_DIR/debug/clinx-node" --compile-config mcp /absolute/private/bridge.toml > /absolute/private/mcp.json
"$CARGO_TARGET_DIR/debug/clinx-node" --compile-config host /absolute/private/bridge.toml > /absolute/private/host.json
```

`/absolute/private` 为占位路径。相对路径、`~`、host aliases 和 scope 由编译器按现有规则解释，
不通过 Python 或 shell eval 解析。编译成功不代表 Provider 可达、Authority 初始化或部署成功。

## CLI 与正常使用

| 入口 | 必要输入和效果 | 源码 |
| --- | --- | --- |
| `bin/clinx devices` | 被动设备发现；默认子命令为 devices | [launcher](../bin/clinx)、[CLI](../kernel/src/discovery_cli.rs) |
| `bin/clinx pair accept` / `pair DEVICE` / `serve` | 建立一次性配对窗口、连接或监听；会写本机 trust/state，须选择正确设备环境 | [CLI](../kernel/src/discovery_cli.rs) |
| `clinx-mcp --config PATH --stdio` | compiled JSON、既有 Authority socket/UID；stdio MCP，不创建业务库 | [MCP 入口](../kernel/src/bin/clinx-mcp.rs) |
| `clinx-mcp --observations-only --stdio` | 独立只读观察目录；无业务写权 | [目录](../kernel/src/observation_directory.rs) |
| `clinx-node-daemon helper` | Mac 配对 Helper、Provider 与 Collector 装配 | [daemon](../kernel/src/node_daemon.rs) |
| `clinx-node-daemon --state PATH --node-id ID serve` | Centre 配对服务；state、identity、sharing 使用已有受控目录 | [daemon](../kernel/src/node_daemon.rs) |
| `clinx-runtime --state-dir PATH --socket PATH --caller-uid UID --transport-uid UID --policy PATH` | 打开既有 Authority；`--init` 是显式初始化而非普通启动步骤 | [runtime](../kernel/src/bin/clinx-runtime.rs) |
| `clinx-observer` | 按专用环境读取授权投影，提供只读 HTTP | [Observer](../kernel/src/observer_runtime.rs) |

MCP 正常流程：读取精确任务/原生 URI上下文 → prepare 用户指令 → start 原 prepared reference →
按原 Execution 读状态/结果；取消使用当前公开 cancel schema。以 [MCP registry](../clinx/mcp/registry.json)
和实际 `tools/list` 为准，不通过重复 start 恢复未知 ACK。完整语义见[契约](CONTRACTS.md)。
设备配对不自动授予业务执行权限；macOS 的 Keychain/Observer 配对说明见[App 指南](../MonitorApp/README.md)。

## 环境变量

以下列出主要操作入口，完整变量随所属源码维护；不要把 shell 当前环境直接传给服务。

| 配置 | 作用域 / 默认与要求 | 定义 |
| --- | --- | --- |
| `CLINX_RUST_SOCKET`、`CLINX_RUST_UID` | 标准 MCP 必填；指定已有 Authority，缺失拒绝启动 | [MCP](../kernel/src/bin/clinx-mcp.rs) |
| `CLINX_RUST_OBSERVATION_READS=1` | 标准 MCP 可选观察目录开关；默认不打开该目录 | [MCP](../kernel/src/bin/clinx-mcp.rs) |
| `CLINX_DEVICE_BINARY`、`CLINX_NODE_BINARY` | CLI binary 显式选择；否则按 launcher 的 release/PATH 查找 | [CLI](../bin/clinx) |
| `CLINX_MCP_RELAY` | relay 选择；默认源码 `kernel/target/release/clinx-mcp-relay`，自定义 target 时必须显式设置 | [relay launcher](../bin/clinx-context-mcp) |
| `CLINX_PROVIDER_BINARY` | Helper Provider 程序选择；不存在时按 daemon 既有默认路径解析 | [daemon](../kernel/src/node_daemon.rs) |
| `CLINX_AUTHORITY=rust` | Node 选择 Rust Authority 配置；仍需正确 socket/UID | [daemon](../kernel/src/node_daemon.rs) |
| `CLINX_OBSERVER_TOKEN` | 必填 32..256 字符 URL-safe credential；不写入文档或日志 | [Observer](../kernel/src/observer_runtime.rs) |
| `CLINX_OBSERVER_RUST_MCP`、`CLINX_OBSERVER_DB` | 至少有一个明确来源；当前 Rust 路径使用受控 MCP endpoint，DB 兼容读取不得变成 writer | [Observer](../kernel/src/observer_runtime.rs) |
| `CLINX_OBSERVER_CODEX_HOST_IDS` | 可选 JSON host 映射，默认 `{}`；错误类型拒绝启动 | [Observer](../kernel/src/observer_runtime.rs) |
| `CLINX_OBSERVER_PORT` | Observer 默认 `8766`；地址、token 与数据源须按服务配置设置 | [Observer](../kernel/src/observer_runtime.rs) |
| `CLINX_SIGNING_IDENTITY` | Mac 本地证书 common name；默认 `CLINX Monitor Local Development` | [App build](../MonitorApp/Scripts/build-app.sh) |
| `CARGO_TARGET_DIR`、`TMPDIR`、`XDG_*` | 开发/测试隔离，见测试指南；不能指向运行状态目录 | [测试指南](TESTING.md) |

安装模板在 [install_templates](../kernel/src/install_templates/)，历史 Observer/Tunnel 模板在
[systemd](../systemd/)。模板存在不等于安装；端口号、unit 名称与 drop-in 以目标现场读回为准。

## 构建、版本与发布包

[clinx-build](../kernel/src/bin/clinx-build.rs) 是唯一构建工具入口，参数详见
[脚本索引](../scripts/README.md)与各子命令 `--help`。普通程序并非全部支持 `--help`，
例如 runtime/MCP 使用参数解析错误和 contract probe，不要启动服务来猜参数。

Rust release 的明确成员来自 [BINARIES](../kernel/src/build_release.rs) 加 runtime-data 清单
与 base release 的 `host-worker`；仅发布运行成员，不继承 Python closure 或开发工具。
构建器验证 base manifest/hash、二进制权限及 contract marker，生成 `STAGED_NOT_SELECTED`。
`rust-<20 hex>` 来自文件 hash map；Cargo `0.1.0` 是包版本，core `1.0.0` 是协议版本，
它们都不是当前安装版本。每次发布保留 source HEAD、构建输入、manifest、文件 hash、验证日志。

```sh
"$CARGO_TARGET_DIR/debug/clinx-build" stage-rust-release --help
"$CARGO_TARGET_DIR/debug/clinx-build" build-tunnel-release --help
"$CARGO_TARGET_DIR/debug/clinx-source-audit" --check-artifact /absolute/private/staged-release
```

最后一条只读检查 **Rust stage release** 的完整成员、hash、native magic 和 Python 污染；
不是 Tunnel archive 或 macOS `.app` 的通用解析器。后两者仍走现有各自 packaging tests。
候选构建不更新 current symlink。生产 stage/installer 要求专用身份与现有 base，不应在普通开发
任务中调用。`clinx-install services` 会写 unit 和 daemon-reload，虽不 start/restart，仍需授权。

macOS 按 [App build](../MonitorApp/README.md) 本机编译 Swift 和三个 Rust Helpers、逐个签名再签 App。
本地自签身份不是 Developer ID 分发签名或公证；当前无已验证的自动公证发布流程。
分发必须另行验证证书、entitlements、notary/stapler 和 Gatekeeper，不能把 Linux 组装 PASS 当作证明。

## 升级、回滚与排障

升级须先保留当前 source/release/process identity、配置权限与回滚依赖，在隔离环境验证候选。
授权切换后重新读回 unit、PID/cgroup、可执行文件 hash、监听、Authority/Provider 精确回执；
如涉及 writer 交接，单独证明旧可写 FD 实际撤销。不能只确认服务 active。
回滚保留新提交事实，优先兼容前向修复；不得用旧数据库覆盖新状态。
Tunnel 的三文件 CAS/capsule 只适用于其原契约，不可扩展为业务库回退。

排障首先只读：查准确 Execution/Task 和时间窗口，区分 Provider、Transport、writer ownership、
lease 与业务 outcome。读取精确状态和持久 receipt，核对实际进程与已选 release，再解释日志。
Observer/health 成功不证明真实任务成功；缺少权限或数据为 UNKNOWN/NOT_PROVEN。
监控源码见 [diagnostics](../kernel/src/diagnostics.rs)、[Tunnel](../kernel/src/tunnel_runtime.rs)、
[Observer](../kernel/src/observer_runtime.rs)。quiet 不允许自行 recycle，断连不允许重放。
历史运维证据统一见[文档地图](README.md)，其中绝对现场路径本次未作生产可达性验收。
