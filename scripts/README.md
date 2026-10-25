# 脚本与启动器

现役任务状态由 Rust Authority 提供。Rust release 的 Python 源文件清单位于
[runtime_sources.py](../tools/build/runtime_sources.py)，不再复制整个仓库。构建器
`stage_rust_release.py --desktop-base-release <installed-release> --source <checkout> --runtime-binary <tested-authority> --mcp-relay-binary <tested-relay> --native-transport-binary <tested-native-worker> --provider-binary <tested-provider-exchange> --mcp-binary <tested-mcp-server> --node-binary <tested-node-api> --output <receipt>`
使用现有不可变 release 的 native 依赖，刷新完整的 Rust adapter 源码集合并移除旧 Python
Owner、Registry、实验包和独立 Provider。必须同时提供已验证的 Authority、Rust relay、native Transport、Provider exchange 和 MCP server binary；
`--replace-source` 仅保留为清单成员校验兼容参数，不再代表部分源码覆盖。
该命令只生成候选，不选择或启动服务。独立 Provider 的旧 bootstrap 打包分支已退役。
候选标记 `host_transport=RUST_HOST_V1`、`provider_exchange=RUST_PROVIDER_V1`、
`workspace_observer=RUST_PROVIDER_V1`、`mcp_runtime=RUST_MCP_V1`、`node_api=RUST_NODE_API_V1`、`node_network=RUST_NODE_NETWORK_V1`。服务模板直接执行 Rust native/Host worker；
新增 `observation_runtime=RUST_OBSERVATION_V1`，构建、安装与 Mac 打包实际探测
`clinx-node --observation-contract`；旧 Node binary 不能运行新历史/观察客户端。
`clinx-node --compile-config host <toml>`（JSON 输出至 stdout）
仅在构建/安装时编译 Host 配置。
`clinx-node --compile-config mcp <toml>` 编译 MCP 配置；
MCP 服务模板直接执行同 release 的 `clinx-mcp`，不经 Python 业务 server。Python Dispatcher、Host runner 及投递账本实现已删除；
`deps/` 继承同样排除重复 CLINX 包和已退役的平铺模块。

macOS Helper 默认经既有配对 mTLS 调用 Rust Authority 和远端 Provider，不再构造本地
Registry/Dispatcher/M9。Rust 与 Monitor profile 均禁止 COMPAT 源码。
Mac 打包另编译并签名 `Contents/Helpers/clinx-provider`，须有本机 Rust 1.98+ toolchain；
固定 workspace 探针和精确 Provider RPC 不再加载 Python 实现。
本批隔离验证见 [Provider/workspace 迁移报告](../docs/RUST_PROVIDER_RUNTIME_MIGRATION.md)。
实机闭环及剩余 Python 运行时见[核心退役验证](../docs/RUST_NATIVE_CORE_RETIREMENT.md)。
旧 SQLite projection/handoff 修复脚本随该业务库契约退役；历史离线维护使用对应 Git 源码。

pytest 命令依赖本机被忽略的 `tests/` 目录，Git clone 不包含它；运行器也位于该目录。
从仓库根目录运行。开发安装见[贡献指南](../CONTRIBUTING.md)，常用验证命令见
[测试指南](../docs/TESTING.md)。

| 入口 | 用途与副作用 |
| --- | --- |
| `cargo test --locked --manifest-path kernel/Cargo.toml --test mcp_retirement_qualification` | Rust MCP 请求及路由回归；无需现役任务 |
| `python -B -m tools.qualification.run_local_tests --work-dir /data/artifacts/<unique-task>/collection --collect-only` | 全仓库 pytest 收集，不执行测试 |
| `python tests/run_tests.py basic` / `owner` | 历史 profile 含已退役实验的测试，当前会收集失败；不能作为当前完整门禁 |
| `python tests/run_tests.py transport --temp-parent <task-artifact-dir>` | 隔离发布/传输回归；要求 `/data/artifacts` 下临时目录及精确外部制品 |
| `python tests/run_tests.py recovery` | 历史 profile 含退役 Authority transport 测试；其余 transient systemd 用例还需要隔离 user manager |
| [check_tunnel_health.py](check_tunnel_health.py) | `--url-file`、可选 `--max-age`；只读核对 PID 和 poll 新鲜度 |
| [check_embedded_bridge_idle.py](check_embedded_bridge_idle.py) | `--base-url --tunnel-pid --bridge-pid`；只读，不 Warmup、回收或接管 |
| [build_transport_runtime.py](build_transport_runtime.py) | 指定源码及 Go 归档，构建 transport runtime 到 `--output`，不安装 |
| [build_tunnel_release.py](build_tunnel_release.py) | 构建候选、manifest 与回退 capsule，不操作服务 |
| [build_forwarding_release.py](build_forwarding_release.py) | 构建 parser 和配对 Tunnel/health 候选 |
| [tunnel_release_cas.py](tunnel_release_cas.py) | `check` 只读；`fixture-apply` 只接受带 marker 的隔离根目录 |
| [deploy_forwarding_release.py](deploy_forwarding_release.py) | `activate` / `rollback` 写配置并操作服务，需要对应部署授权 |
| [qualify_bridge_transport.py](qualify_bridge_transport.py) | 固定上游 Bridge 源码的隔离 generation-fence 资格 |
| [local_discovery_smoke.py](local_discovery_smoke.py) | 显式启用的真实 mDNS / OPAQUE / mTLS 网络资格 |

源码启动器：[CLI](../bin/clinx)、[MCP](../bin/clinx-context-mcp)、
[节点中心](../bin/clinx-node-centre)、
[Observer](../bin/clinx-observer)。设备发现 CLI 已与旧业务核心解耦；旧 Linear `run/once/doctor` 已移除。
MCP 源码启动器只执行 Rust `clinx-mcp-relay`，连接现有 Rust MCP Unix socket，不创建 Python 后端。
这些入口可能写状态或启动服务；已安装服务通常
使用独立的 release，不因编辑源码而升级。

一次性实验资格器随对应实验退役。本地旧测试文件保持原字节，因此全量收集会显示
已退役 Python API 的导入错误；不会通过改断言或跳过测试把它们变成 PASS。历史回执位置见
[文档索引](../docs/README.md#历史资料恢复)。构建与测试只回收本任务拥有的输出；
部署包、manifest、数据库、回退和证据按各自运行契约保留。

Collector 调用端还要求 `collector_runtime=RUST_COLLECTOR_V1`；构建时实际探测 `clinx-node --collector-contract`，拒绝旧 Node binary。

客户端/Observer/Discovery/监督策略调用端要求 `client_runtime=RUST_CLIENT_RUNTIME_V1`。构建必须实测 `clinx-node --client-runtime-contract`；独立 Tunnel builder 使用 `--supervisor-binary`（或 CLINX_TUNNEL_BINARY）打包已验证的 `clinx-tunnel`，不携带 Python supervisor 或 clinx-node。Host/MCP 配置编译均由已校验 release 的 `clinx-node --compile-config` 执行，无 Python 编译器。

最新客户端还要求 `thin_runtime=RUST_THIN_RUNTIME_V1` 和实际 `clinx-node --thin-runtime-contract` 探测。全仓 Python <5% 的固定只读统计使用 Rust `clinx-source-audit`，未达标退出 2，详见架构文档。

设备运行产物要求 `device_runtime=RUST_DEVICE_RUNTIME_V1` 与 `node_daemon=RUST_NODE_DAEMON_V1`。
`stage_rust_release.py` 必须提供 `--node-daemon-binary`，并实测两个 marker；Centre service
直接启动 release 内 `clinx-node-daemon`。当前 App 使用 Rust `clinx-build native-monitor-resources <source-root> <resources>`，
不发布 Python closure/DiscoveryRuntime/wheel。旧 `--monitor-resources` 仅供原 API 布局资格。
这些是候选构建/安装工具契约，源码迁移不自动部署、切换产物或重启服务。

策略维护入口为 `clinx-mcp --policy-maintenance --config <mcp-runtime.json> --stdio`，
仅暴露原有四个 policy 工具，不含执行/恢复服务。旧 Python/TOML 维护入口已删除。
Observer inode guard 为 `bin/clinx-observer --history-mount-guard`；该维护命令可能
对唯一的 Observer 服务执行 try-restart，测试只使用临时文件和替身命令，本轮没有执行实际 guard。
