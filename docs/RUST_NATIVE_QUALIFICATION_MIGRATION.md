> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Native、Host、Node 与 Provider 资格迁移

基线 `5c0107dc79447d49a25a5dd16e01083f37c65840`。本轮将剩余跨进程资格迁入 Rust，
删除重复离线校验器；**运行核心、冻结契约和生产服务没有改动，没有部署**。
全仓 Python 从 25.8685% 降至 22.2285%，严格低于 5% 的目标仍为 NOT_MET。

## 用例与实际边界

原 6 个 Python fixture 的 65 个测试函数、92 个参数化 case 全部有映射：

| 范围 | 原参数 case | Rust 测试组 | 剩余 Python case |
| --- | ---: | ---: | ---: |
| Native Transport、Host | 20 | 20 | 1 个新增接口 smoke |
| Node router/network | 30 | 23 | 7 个原 case + 1 个 cursor 异常 smoke |
| Provider client/runtime | 42 | 26 | 17 个原 case + 1 个 DTO smoke |

Rust 覆盖 82 个原参数 case，部分同时保留 Python 绑定检查。原 Python case 保留 24 个，
移除 68 个已有 Rust 等价的实现，另补 3 个接口 smoke；全部 fixtures 从 244 个变为 179 个。
这些计数存在有意重叠，不能把 Rust 组数与 Python case 数直接相加当作原覆盖量。

[逐 case 映射](../architecture/native-qualification-migrations.json) 固定基线、原/新源码 SHA、
参数和目标；详细 assert/raises 对照记录在本机证据目录，索引内包含其 SHA。
没有修改 ignored `tests/`，没有新增 skip，也没有改变运行实现来让资格测试通过。

迁移保留的真实边界包括：

- Direct/MCP/Node × local/paired/routed 全部 9 组合，真实 Authority/Transport/Centre/Helper
  子进程，OPAQUE 配对、mTLS、Provider gateway、反向 Authority 路由。
- Host 的真实命令、重复请求、两个 worker 竞争、并行项目、丢输入/结果 ACK、取消进程组、
  SIGKILL 后重启、未知进程结果不重放、超时/输出截断、原始 SHA 和脱敏、canonical successor。
- Node 的真实 worker IPC、独立 SQLite 连接与进程并发 claim、原进程退出后重开同一 ledger、
  loopback listener 同端口重绑、身份冲突、scope 拒绝、receipt/fence/UNKNOWN 恢复。
- Provider 的独立 Unix WebSocket oracle、真实 Git/worktree、`--inspect` 子进程、Authority
  prepare/start 幂等，以及 OPAQUE → mTLS → Centre Gateway → Node → Provider 的隔离回环。

保留 Python 的具体原因：OpenSSL 是 Rust TLS 的独立互操作 oracle；异常对象身份、异常文本、
失败后不得写入 `native_forwarding_receipt`、DTO 属性与源码包导入属于 Python 接口行为。
Native 文件的 Provider/frame helpers 仍被其他 fixture 引用，不能随测试主体删除。
这些保留项不进入 Rust release 或当前 Monitor 资源包。

独立审查补齐了原 CPython provenance 的空格/键序 literal、Host tool result 对象与保留字段
冲突检查，以及四参数负向 receipt 属性原测试。原并发 claim 的 Rust harness 收集锁已移到
请求完成之后，避免把并发资格意外串行化。原 Python `NodeProtocolError` 文本保留在薄测试，
Rust 层检查精确 `CANONICAL_ADOPTION_PENDING` code，没有用不同错误来放宽业务预期。

## 重复离线校验器退役

删除 411 行 `scripts/check_execution_core_contract.py`。其旧 runtime_lab 消费者已退役，
包括本地 ignored tests 在内没有剩余代码消费者。新增离线命令：

```sh
cargo run --locked --manifest-path kernel/Cargo.toml --example check_execution_core_contract
```

此工具复用现役 `execution_core` 纯函数，保留 7 对象的转换表结构校验、35 个冻结 golden、
facts refs 合并次序。12 种非法表结构与原 Python 独立核对，旧新命令输出逐字相同。
`fixtures/execution_core_v1.json` 和 `kernel/src/execution_core.rs` 的字节未变。
工具不打开业务数据库、网络或执行器，不是新的 Task Authority。

## 验证

| 范围 | 结果 |
| --- | --- |
| Linux Release | 14 binaries 构建 PASS，无 libpython 链接 |
| Rust 全套 | 370 PASS；审查修正后 Host/Native 20 case 再次 PASS |
| 剩余 tracked Python fixtures | 179 PASS；基线 244 PASS |
| 完整旧本地套件 | 前后均 294 PASS / 32 FAIL / 104 ERROR / 2 SKIP；432 个身份结果逐项相同 |
| 本地文件保护 | 294 个文件 SHA 未变，含测试源码及既有缓存 |
| 分类/import/profile、E9/F、新增 Rust 格式、diff | PASS |
| 全仓 Rust format | BLOCKED_EXISTING：7 个既有文件，完整格式差异与基线逐字一致 |
| Strict Clippy | BLOCKED_EXISTING：相同 10 条核心诊断；普通 Clippy 另有既有 workspace_routes 告警，无新增告警 |
| 原生 macOS 构建/签名、新产物物理跨设备链 | 未验证；本轮 Linux 隔离回环不替代此项 |
| 生产 | NOT_DEPLOYED；Authority/Dispatcher/Centre PID、启动时间、ExecStart 均未变 |

Rust examples 必须在全部 integration tests 前构建：`cargo build --locked --manifest-path
kernel/Cargo.toml --examples`，随后运行 `cargo test --locked --manifest-path kernel/Cargo.toml`。
发布包继续使用零 Python 清单；打包 fixture 验证不含 Python/deps，使用合成二进制探针。
14 个真实 Linux 二进制的构建/链接证据另列，没有把该 fixture 包安装到生产。

## 规模与后续

固定六语言全部源码口径，包含测试、构建工具和资格 examples，没有修改 Linguist：

| 语言 | 之前 文件 / 行 / 字节 | 之后 文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 119 / 19,013 / 828,724 | 117 / 16,892 / 725,340 |
| Rust | 97 / 46,442 / 1,810,134 | 109 / 50,821 / 1,973,020 |
| Swift | 62 / 11,353 / 525,288 | 62 / 11,353 / 525,288 |

完整删除 2 个 Python 文件、918 行；全部 Python 净减少 **2,121 行 / 103,384 字节**。
剩余 COMPAT 为 8,525 行、DEVOPS 为 2,230 行、QUALIFICATION 为 6,137 行。
本轮减少的是资格代码，未把尚存兼容实现重新命名或移出统计范围。

下一批仍需处理 Observer/Collector/Node daemon/设备装配等现役资格，以及构建、维护工具。
其消费者迁移完成后才能缩减公开 Python DTO、回调注入、协议 mock 和旧布局兼容闭包；
不能依据 Rust 已能运行就删除尚在验证接口差异的测试。

回执：[RUST_NATIVE_QUALIFICATION_MIGRATION_RECEIPT.json](cleanup/RUST_NATIVE_QUALIFICATION_MIGRATION_RECEIPT.json)。
本机证据：`/data/artifacts/clinx-native-qualification-20261025/`；保留原测试归档、源码 bundle、
日志和摘要，提交后回收本轮临时 checkout、venv 与可再生编译输出。
