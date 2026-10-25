# 测试与质量验收

用途：当前构建、回归和证据边界的唯一命令入口。适用范围：Rust 源码及私有 Linux 资格；
macOS 与生产另行验收。状态：CURRENT。验证方法：执行以下命令并保留逐项退出码和日志。

## 隔离 Linux 验证

需要 Rust 1.98+、Git、C linker、OpenSSL CLI。先创建属于本任务的目录，P620 构建放 `/lab`，
状态和临时资格仓库放 `/data/artifacts`；Unix socket 路径应短。以下路径为占位值，替换后执行。
保留原工具链/cache 的显式 `CARGO_HOME`、`RUSTUP_HOME`；不要继承生产 CLINX 环境变量。

```sh
export CARGO_TARGET_DIR=/lab/runtime-worktrees/p620/your-task/target
# 创建自己的 0700 HOME/XDG/TMPDIR 后，给测试子进程传入私有环境。
cargo build --locked --manifest-path kernel/Cargo.toml --bins --examples
"$CARGO_TARGET_DIR/debug/clinx-source-audit" --check-structure
"$CARGO_TARGET_DIR/debug/clinx-source-audit" --check-project
"$CARGO_TARGET_DIR/debug/clinx-source-audit"
env HOME=/data/artifacts/your-task/home XDG_CONFIG_HOME=/data/artifacts/your-task/config \
  XDG_STATE_HOME=/data/artifacts/your-task/state XDG_DATA_HOME=/data/artifacts/your-task/data \
  XDG_RUNTIME_DIR=/data/artifacts/your-task/run TMPDIR=/data/artifacts/your-task/tmp \
  cargo test --locked --manifest-path kernel/Cargo.toml
cargo test --locked --manifest-path native/opaque_pairing/Cargo.toml
cargo build --locked --release --manifest-path kernel/Cargo.toml --bins --examples
cargo clippy --locked --manifest-path kernel/Cargo.toml --all-targets
cargo fmt --manifest-path kernel/Cargo.toml --check
git diff --check
```

测试子进程也需要能找到上述 Cargo/Rustup 路径；不得把生产 socket/Provider/database 用作测试输入。
`qualification_workspace`、`qualification_tunnel` 是真实私有子进程，需在同 target/profile
预先构建，不进入运行发布包。网络缓存完整时可追加 `--offline`，缺失依赖应记录 BLOCKED。

## 分层与覆盖

| 层级 | 覆盖与入口 |
| --- | --- |
| core / Authority / 业务事务 | 状态 guard、scoped identity、receipt/outbox 原子性、WAL、并发、取消、未知 ACK 和 restart；`authority_store`、`business`、`execution_core_qualification` |
| Provider / Host / native | exact thread/turn/input/cwd、send once、工具 ACK、process/SSH/Unix/WebSocket、Host 证据；`provider_*`、`host_runtime_qualification`、`native_transport*` |
| Node / Discovery | principal/scope、配对 mTLS、双向独立 OpenSSL、到期证书、并发、重启、撤销、CLI/PTY；`node_*`、`discovery_*`、`device_python_retirement_qualification` |
| Collector / Observer / MCP | 独立 SQL/JSON oracle、只读字节、checkpoint/重复 ACK、cursor scope、分页、权限错误；`collector_qualification`、`observer_*`、`mcp_retirement_qualification` |
| build / install / 语言与文档 | 发布成员/hash/marker、无 Python、CAS preflight、服务模板无 activation、文档与依赖；`build_release_qualification`、`monitor_build`、`install_tools`、`source_structure`、`project_policy` |
| Tunnel / relay | 进程归属、TERM/KILL、持久预算、half-close、partial bytes、不重放；`tunnel_runtime`、`mcp_relay_retirement` |
| OPAQUE / Swift | 原生 OPAQUE 确认/上下文/单次消费；macOS 使用 `swift test` 与 App 构建，见[App 指南](../MonitorApp/README.md) |

所有 Rust 集成测试在 [kernel/tests](../kernel/tests/)，冻结期望在 [fixtures](../fixtures/)。
不得把测试名称含 `python_retirement` 误读成运行 Python。

## 本次自动化的针对性回归

```sh
cargo test --locked --manifest-path kernel/Cargo.toml --test source_structure --test project_policy
cargo test --locked --manifest-path kernel/Cargo.toml --test build_release_qualification --test monitor_build --test mcp_relay_retirement
"$CARGO_TARGET_DIR/release/clinx-source-audit" --check-project
"$CARGO_TARGET_DIR/release/clinx-source-audit" --check-artifact /absolute/private/staged-release
```

`--check-project` 组合原结构检查、全部 Markdown 链接/锚点与索引覆盖、当前文档元信息、
Rust/Swift 归属、直接模块依赖、Cargo targets 与发布名单一致性。负向测试必须覆盖损坏链接、
漏登记、Python/其他语言、违规依赖及被污染的 release，失败输出明确 code/path。
`--check-artifact` 只验证实际 Rust stage release；不执行其中二进制，不修改安装状态。
原构建回归继续验证 contract marker、App helpers、archive 和 rollback capsule。

CI 运行同一 Cargo/audit 入口，不依赖新的文档框架。workflow 与本地执行通过并不证明远端
分支保护、真实设备、生产部署或业务验收。

## macOS 编译兼容回归

在每台目标 Mac 的任务私有源码目录使用 Rust 1.98+ 和现有签名身份执行：

```sh
cargo test --locked --manifest-path kernel/Cargo.toml --lib platform::tests
sh MonitorApp/Scripts/build-app.sh
codesign --verify --deep --strict MonitorApp/.build/CLINX.app
```

`platform::tests` 检查 Unix/TCP/UDP socket 的 close-on-exec 和两种阻塞模式。
App 构建入口同时验证三个 Rust Helper 的原生契约与签名；不要复制 Linux binary。
保留源码 SHA/patch hash、工具链、构建退出码及 App/Helper hash；签名验证通过不等于
Developer ID 公证、客户端安装、配对或真实任务验收。缺少原生工具链或证书记录 BLOCKED，
不得通过恢复 Python Helper 绕过。仅构建无需启动 App、替换已安装客户端或修改权限。

## 未验证与既有失败

Linux wire/loopback 不代替物理 LAN multicast、Mac→P620 或真实 Codex/ChatGPT connector。
smoke 的 `--lan` 需要单独显式设备资格；普通测试不得接触生产。管理员 install/provision 与
writer FD 撤权只在私有 fixture 验证，不构成生产权限隔离证明。

固定 Go Transport 的全新双构建需要精确归档；普通打包测试不替代该证明。历史证据见
[重建报告](GO_TRANSPORT_REBUILD.md)，不能拿其旧 PASS 当本次执行结果。

ignored Python `tests/` 是历史归档，保留原字节、失败、skip 和独立断言。
恢复历史测试需匹配原源码与依赖，不向当前 runtime 恢复 Python 来满足旧 imports。
全仓 fmt/Clippy 若有基线问题，保留完整诊断并与基线位置比较；修改文件必须无新增问题。
当前 Rust 功能测试失败不能因迁移已完成而被豁免。
writer-lock 回归显式使用 `0700` 的 canonical 临时目录，不依赖开发机的 `umask 077`；
CI 首次在 `umask 022` 暴露的目录权限失败保留记录，修复不得放宽生产路径校验。
