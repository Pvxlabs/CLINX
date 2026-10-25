> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust Host / Dispatcher 迁移验证

基准：`ccd7e1897be92a4d7325b03fcd83860d61df619a`，2026-10-24 UTC。
本批状态：**PASS（限定 Host/Dispatcher 源码、隔离构建和回归），NOT_DEPLOYED**。
Python 降到 25% 以下及全部运行时迁 Rust仍未完成。
机器回执见 [RUST_HOST_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_HOST_RUNTIME_MIGRATION_RECEIPT.json)。

## 已替代并删除的运行职责

`kernel/src/host_executor.rs` 接管命令目录、参数校验、workspace/symlink/Git target 校验、
固定 workflow 参数展开、环境清理、子进程组、取消/超时、输出截断/脱敏和原始输出 SHA-256。
执行前须取得 Authority 的新 `host_begin` 许可，完成事实由 `host_complete` 原子提交。
生产继续使用既有 root-controlled `host-worker`；只有 Authority 的资格身份允许直接启动 fixture 子进程。
没有新增服务、权限身份或审批流程。

`kernel/src/host_transport.rs` 接管精确 execution/thread/turn 的 Provider 会话：
先提交原生输入证据，再接纳工具请求；处理 `item/started`、工具请求、重复/冲突 RPC id、
多个工具的 ACK 队列、ACK deadline、断线、取消和重启恢复。
原生观察连接不响应工具或 Desktop approval。完成必须同时满足 Rust Host 事实和精确 Provider 证据。
新旧 worker 竞争中的异常不能被当作另一个 worker 未发送的证明。

Host 线程创建与 canonical predecessor→successor 交接复用既有 Rust Authority 契约。
新 successor 的标题发布 outbox 与交接在同一事务建立，读回确认后收口；前驱标题不变。
输入、线程创建或子进程结果未知时不重放；Host 副作用不明时保留 HELD 和恢复所需证据。
恢复通过 Provider 的精确历史重建 ACK，不以节点或 Host 自报成功代替 Provider 回执。

实际删除 4 个文件，共 **2,062 行**：

| 文件 | 删除行数 |
| --- | ---: |
| `clinx/host/host_executor.py` | 957 |
| `clinx/host/rust_host.py` | 175 |
| `clinx/host/tool_delivery.py` | 445 |
| `clinx/transport/rust_transport.py` | 485 |

另删除 `clinx/provider/app_server.py` 内旧 Host handler、投递/ACK 队列和后台 supervisor，
保留 Node relay 和观测仍使用的协议功能。只读 `exact_turn` 读取移入现有 Provider 读取模块，
Host 配置 dataclass 提取至 `clinx/contracts/host_config.py`；这些类型不执行命令或持有业务状态。

## 发布与入口

服务模板直接启动 `clinx-native-transport --host-config ...`，不再启动 Python Dispatcher。
安装/构建工具将既有 TOML 配置编译为 Rust JSON，安装器要求 `host_transport=RUST_HOST_V1`。
既有 native/远端 Provider 路由环境契约保留。

Rust release 和 Monitor Helper 共用分类清单，删除文件及冻结的 Python Host catalog 参考均不进入包。
旧 release 的 `deps/` 也不得继承第二份 `clinx` 包或退役的平铺 Python 模块/字节码。
资格构建中已验证源码成员、内容哈希、增量清理和独立导入。
未调用生产 release staging/安装器，未选择 release、部署、重启服务或访问生产业务数据库。

## 验证与失败对照

证据目录：`/data/artifacts/clinx-host-rust-20261025`。所有数据库、Provider socket、
Host 子进程和 Git 仓库均为 fixture 所有，不操作真实业务任务。

| 检查 | 结果 |
| --- | --- |
| Rust `cargo build --release --locked` | PASS |
| Rust 全量默认并行测试 | 69 PASS，0 FAIL；基线串行 64 PASS |
| 删除前 Python 生成的命令 golden / 完整操作目录 | 34 个命令逐字段相等；目录相等 |
| Release binaries 驱动的全部跟踪 fixtures | 60 PASS，0 FAIL；其中 11 项 Host 纵向用例 |
| Host 交接与幂等 | canonical adoption→精确 checkpoint→Host successor→执行→Provider ACK→完成 PASS |
| 故障和并发 | 重复/冲突请求、双 worker、双项目、输入/结果 ACK 丢失、Authority/worker 重启、取消、超时、截断/脱敏、未知结果不重放 PASS |
| Rust release / Helper 源码包 | 分别 53 / 51 模块可在仓库外独立导入；无已退役 Host/Dispatcher 残留 |
| 分类、内部 import、发布 closure、Ruff、diff 空白检查 | PASS |
| 渲染的 systemd 模板 `systemd-analyze verify` | PASS；仅校验，未安装 |
| 原本地 Python 全量收集 | BLOCKED_EXISTING：前后均 109 个收集错误，错误文件集合一致；可收集的 96 个测试身份相同 |
| 这 96 个本地历史测试逐项对照 | 前后均 69 PASS / 25 FAIL / 2 ERROR；新增失败 0，结果变化 0 |
| 本地测试保全 | 121 个 Python 测试源码 SHA-256 全部不变；pytest 自身更新了一个 lastfailed 缓存文件 |
| macOS Swift 编译、签名及真机 Provider 执行 | 本批 NOT_RUN；没有把 Linux fixture 等同于真实 Mac 验收 |

并行验证暴露 journal 的已有 FD 继承问题：并发 fork 继承锁 FD 后，单纯关闭原 FD
不能立即解除 flock。增加了修复前稳定失败的继承回归，journal Drop 显式释放锁后通过；
未修改既有断言或放宽 Authority 的 writer fence。

验证过程也保留了两类测试环境/时序失败证据：过长资格目录导致 Unix socket bind 失败；
丢输入 ACK fixture 最初只等到 Authority 的 SENDING，可能在 Provider 接收前杀进程，
此时保持 UNKNOWN 是正确结果。fixture 现明确等待 Provider 已接收输入再模拟 ACK 丢失。
所有原断言保留，最终结果来自短路径下的 Release binaries 回归。

## 规模与剩余迁移

统计 Git 跟踪源码及本批新增源码，包含资格工具，不包含被忽略的本地测试和生成产物。

| 语言 | 文件数，前→后 | 行数，前→后 | 字节，前→后 |
| --- | ---: | ---: | ---: |
| Python | 103→102 | 24,129→21,969 | 1,206,381→1,084,599 |
| Rust | 29→31 | 12,297→14,788 | 494,222→594,303 |
| Swift | 62→62 | 11,355→11,355 | 525,634→525,634 |

Python 净减少 **2,160 行、121,782 字节**，其中 ACTIVE_RUNTIME 从 17,219 行降至 14,321 行。
新增 Python 主要是必要配置类型、构建工具和真实进程资格 fixture。
按 GitHub 基线语言字节数加本批差量估算占比 **53.25%→48.34%**；这是源码估算，
不把 GitHub 的异步语言统计刷新当作验收证据。尚未达到 25%。

剩余 Python 有 56 个 ACTIVE_RUNTIME 模块（14,321 行），具体职责和阻塞如下：

| 范围 | 当前实际职责 | 退役前必须完成 |
| --- | --- | --- |
| `clinx/mcp/`、`clinx/authority/`、公共 contracts | MCP framing/schema、公共参数/结果映射、IPC 客户端 | Rust MCP 完整工具/错误/请求身份等价，替换请求入口；stdio relay 本身不等于 MCP 实现 |
| `clinx/nodes/`、`clinx/discovery/` | OPAQUE/mTLS 配对、Node/Centre 网络路由与注册、Helper bootstrap | Rust Node/Discovery listener/client、Keychain/身份存储兼容、设备发现和实机重连资格；不能用 Linux peer UID 代替跨 Mac 身份 |
| `clinx/provider/` | Node Provider 网络桥、workspace 固定探针、历史读取、只读 writer 观察 | Rust 网络 relay/observer、精确分页/取消/读证据接线，真实跨设备资格后替换 |
| `clinx/monitor/` | 监控投影、观察 API、历史 association | Rust 观察服务与 Swift 消费契约等价后删除 Python 服务 |
| 公共配置、workflow/derived-target schema | 尚有 Python 配置和请求解析消费者 | 这些消费者迁 Rust 后删除对应配置解析；实际 Host 执行/目标校验已在 Rust |

另有 27 个 QUALIFICATION、17 个 DEVOPS、2 个仅离线工具使用的 COMPAT 模块，均不进入运行包。
`clinx/host/host_contract.py` 是 158 行冻结资格参考，不是备用 Host runner。
不能把上述 ACTIVE_RUNTIME 宣称为仅开发工具，或通过移动路径改变迁移完成度。

源码与部署严格区分：本批只读观察到 Authority 与 Dispatcher 仍从原不可变 release 运行，
Dispatcher 的实际入口仍为原 Python `rust_transport.py`。本批删除的是仓库源码和新包依赖，
并未更改正在运行的进程或生产 release。这也是本次 NOT_DEPLOYED 的明确边界。
