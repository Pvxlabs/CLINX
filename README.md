# CLINX

**AI 任务路由与执行流通基础设施。** CLINX 把任务、上下文和指令送到正确的执行端，
持久记录真实状态、关联回执并在断连后恢复原执行。Codex 是当前主要 Provider；
其他 Agent 适配器属于目标方向，不能当作已实现能力。

用途：项目总入口。适用范围：当前 Git 源码与开发使用。状态：CURRENT。
验证：按[测试指南](docs/TESTING.md)核对当前源码；本文不声明任何生产部署或设备验收。

Rust 是后端唯一实现语言，Swift 负责 macOS UI 与必要的原生集成。
Task/Execution 状态只由 Rust Authority 提交；Node、Provider、Transport 和 Observer
传递请求、证据与投影，不另建执行权威。身份、幂等、ACK 和未知结果恢复是 CLINX 的职责；
业务操作、业务决策及其验收由实际执行端负责。详见[产品定位](docs/PRODUCT_POSITIONING.md)。

| 需要做什么 | 权威入口 |
| --- | --- |
| 理解产品、用户与版本范围 | [产品定位](docs/PRODUCT_POSITIONING.md) |
| 理解组件、数据流与语言边界 | [仓库架构](docs/REPOSITORY_ARCHITECTURE.md) |
| 修改代码与验收变更 | [AGENTS](AGENTS.md)、[贡献指南](CONTRIBUTING.md)、[测试指南](docs/TESTING.md) |
| 查 Task、Provider、Host、Node、MCP、IPC、错误及恢复 | [当前接口契约](docs/CONTRACTS.md) |
| 配置、启动、构建、发布与排障 | [配置与运维](docs/OPERATIONS.md)、[原生入口](scripts/README.md) |
| 构建 macOS App | [MonitorApp](MonitorApp/README.md) |
| 查文档分类、历史证据与编写标准 | [完整文档地图](docs/README.md)、[文档标准](docs/DOCUMENTATION.md) |
| 查版本与重要决策 | [CHANGELOG](CHANGELOG.md)、[Rust 架构决策](docs/architecture/ADR-007-rust-source-policy.md) |

## 开始开发

需要 Rust 1.98+、Git、C linker 和 OpenSSL CLI；macOS UI 另需 macOS/Xcode。
先按[隔离验证环境](docs/TESTING.md#隔离-linux-验证)设置任务私有构建与状态目录，再执行：

```sh
cargo build --locked --manifest-path kernel/Cargo.toml --bins --examples
"$CARGO_TARGET_DIR/debug/clinx-source-audit" --check-project
cargo test --locked --manifest-path kernel/Cargo.toml
```

全套验证及发布检查只有一个定义：[测试指南](docs/TESTING.md)。当前源码不依赖 Python、pytest
或本机 ignored `tests/` 历史归档。新 clone 不自动获得 Authority、凭据、已安装 release 或设备绑定。

## 使用边界

`bin/clinx` 默认进入 Rust 设备发现 CLI；任务执行使用 MCP 的 prepare/start 与精确状态读取。
配置与安全的使用顺序见[配置与运维](docs/OPERATIONS.md)。不要把设备发现 CLI 当作已退役的
Linear 轮询入口，不要用旧报告里的 Python 命令启动当前服务。

源码提交、候选产物、实际运行进程和业务结果是不同证据。外部固定 Go Transport 制品仍有
历史兼容构建路径，范围见[架构决策](docs/architecture/ADR-007-rust-source-policy.md)；
它不是新增非 Rust CLINX 后端的许可。P620 只使用现有 canonical 仓库，恢复规则见
[项目收敛记录](docs/PROJECT_CONVERGENCE.md)。
