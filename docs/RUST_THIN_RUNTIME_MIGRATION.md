> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 薄兼容层迁移与 5% 验收

基线 `541c8e6f84c49e5b77f3223d587548731c307623`。目标已经收紧为 Rust 核心、适合
现有客户端的前端技术、尽可能薄的 Python 兼容层，**全仓 Python 源码字节严格低于 5%**。
开发、构建、维护和资格工具也纳入目标。本轮实际迁移下列运行实现，未宣称达到 5%。

## 实际替代

- **Provider**：模型/版本/参数与 thread/turn 请求契约、sandbox、精确冲突/异常映射、
  订阅释放和生命周期判断迁 Rust。默认 Process/SSH/Unix/WebSocket/JSONL 由 Rust 持有
  进程、协议和关闭行为；`setsid + exec` 保持 PID/进程组和同步启动错误，没有 unsafe。
  Python 保留原签名、DTO、异常及显式测试 IO 注入；并发 turn 只回传所有权增量，
  不覆盖彼此的 owned_turns。未知发送不重放。
- **Observer HTTP**：真实 listener、16 路并发、socket 超时、认证、路由、错误映射、脱敏、
  响应限额、审计和关闭生命周期由 Rust 接管。Python 为 Store/native/collector 提供
  对象回调与公开兼容接口。正常 API 差分一致；畸形原始 HTTP 的 414/431/505 使用
  有界 JSON 拒绝，不声称与旧 stdlib HTML 诊断逐字节相同。
- **配置与 Workflow**：TOML 只解码，Path 只执行平台 IO；默认值、注册顺序、项目/线程/Host
  绑定、校验以及 workflow schema/render 均进入 Rust。复用已有 Rust Host 渲染契约。
  私有 wire 保留 Python 宽整数、Unicode 数字、合法下划线、非有限浮点值与原异常类型；
  不改动已有任务准入规则。
- **Tunnel**：独立 `clinx-tunnel` 完整执行 supervisor、flock、持久预算、LAUNCH_INTENT、
  子进程 session/group、TERM/KILL/drain、信号、健康探针总 deadline 与恢复循环。
  Python 文件仅保留原资格 API 的可注入时钟/进程 IO，CLI 直接 exec Rust；独立发布包
  不再携带 Python supervisor 或 clinx-node。详见 [Tunnel 资格](RUST_TUNNEL_SUPERVISOR.md)。

Task Authority、执行身份、并行任务、投递、ACK、幂等、取消、恢复和 metadata outbox
仍使用已有 Rust 权威事务。没有新增审批/权限流程，没有使用节点自报成功替代 Provider 证据。

## 回归期间发现与修复

配置初版把字符串 `"5.5"`/`"5e0"` 接受为整数，且漏掉合法下划线；现按原 Python 词法
区分字符串与数值浮点，扩展 242 项差分全等。Observer 的 `///v1/health` 规范化也通过
旧实现差分修正。Tunnel 的 DEL 字符必须编码为 `\u007f`，固定旧 Python SHA 向量证明
已有持久预算身份不变。

最终 Rust 回归发现一次既有 Store 关闭后立即重开 `WRITER_ALREADY_ACTIVE`。
旧 `authority_store.rs` 仅靠关闭 File 释放锁；另一个线程 fork 时可能继承同一 open-file
描述符，延长锁的寿命。新增确定性回归先在旧实现上失败，再以私有 WriterLock Drop 显式
unlock 修复。SQLite 字段先于锁销毁；所有可失败初始化在结构体构造前完成，保证错误路径
同样先关闭数据库再释放锁。活跃 Store 仍拒绝第二 writer，没有改数据库/事务/lease 契约。
原失败日志、复现与独立析构顺序审查均保留。

## 验证与限制

- **PASS**：12 个 Linux Release binaries；99 项 Rust tests；370 项 tracked fixtures。
- **PASS**：Provider 639/639、Observer 356/356、配置 242/242 旧实现差分。
- **PASS**：Provider 默认真实子进程/WS/JSONL/启动失败、HTTP 认证/边界/并发/重启，
  独立 Helper 包 mTLS 注册/读取/重启，以及 Tunnel 独立包、实际子进程回收与预算。
- **PASS（无新增失败）**：完整历史套件前后相同的 432 个 JUnit 身份及结果：
  **295 PASS / 31 FAIL / 104 ERROR / 2 原有 SKIP**。这不是全量历史测试通过。
- **PASS**：124 个原本地 test/support 文件 SHA 不变；21 个既有 tracked fixture 的
  assert AST 不变。新协议 probe 仅扩充 synthetic binary 测试桩，没有弱化原断言。
- **PASS**：Python E9/F、源码分类/import/profile、Shell syntax、diff whitespace。
- **BLOCKED_EXISTING**：严格 Clippy 仍有原 10 项诊断，新诊断已修复。
- **未验证**：原生 macOS 构建、签名、新产物物理 Mac→P620→Mac 闭环。本机只有 Linux
  Rust toolchain，没有 Swift/macOS toolchain 或已配置的 Mac 验证入口。
- **NOT_DEPLOYED**：只提交源码；不部署、不重启生产服务、不操作生产数据库/真实任务。
  Tunnel 的临时 systemd fixture 为单独命名的私有测试 unit，已退出且 MainPID=0。

原测试提取用于更细的差分，不替代完整历史套件。原 ConfigTests 仍有一个已退役
ThreadRegistry 接口失败，未添加 shim；旧 Tunnel 发布测试仍停在既有 provider.service
模板断言，未修改或 skip。首次整批 fixtures 因测试环境漏传 CLINX_TUNNEL_BINARY 导致
收集失败，补齐实际候选 binary 环境后通过；原错误日志保留。
测试 wrapper 曾通过 relocation 的 bin symlink 写回源码目录；核对任务创建字节后已精确
移除，迁至真正独立的 fixture-tools 目录；回执保留，其他 bin 文件未变。

## 固定规模口径

新增只读 Rust `clinx-source-audit`，默认严格低于 5% 才退出 0，未达标退出 2。
以 Python/Rust/Swift/Go/C/Shell 六语言物理字节统计全部 tracked 与非 ignored 新源码，
包含资格测试和工具；没有调整 Linguist 或删除测试改变占比。GitHub 实时 Linguist
百分比不是此本地回执的口径。工具同时输出物理行数与分类规模。

| 项目 | 本轮之前 | 本轮之后 |
| --- | ---: | ---: |
| Python 文件 / 行 / bytes | 113 / 22,487 / 976,532 | 117 / 22,031 / 952,898 |
| ACTIVE_RUNTIME Python 文件 / 行 / bytes | 54 / 10,185 / 424,101 | 53 / 8,792 / 361,667 |
| Rust 文件 / 行 / bytes | 65 / 29,872 / 1,188,254 | 74 / 34,312 / 1,357,903 |
| Swift 文件 / 行 / bytes | 62 / 11,355 / 525,634 | 不变 |
| Python 字节占比 | 35.7525% | 33.1157% |

本轮整文件删除 **0 个、0 行**；替换的是文件内运行实现。全仓 Python 净减
**456 行 / 23,634 bytes**；ACTIVE_RUNTIME 分类净减 **1,393 行 / 62,434 bytes**，
其中已退役正常运行职责的 Tunnel Python 转为 QUALIFICATION，不再进入运行包。
新增 4 个 Python 资格文件，保留原回归与真实 Rust 子进程验证。

## 尚未完成的目标

5% gate 当前明确失败。剩余 Python 分类规模为 ACTIVE_RUNTIME 361,667 bytes、COMPAT
24,940 bytes、DEVOPS 96,750 bytes、QUALIFICATION 469,541 bytes；后者约占剩余 Python
一半。即使移走所有运行 Python，保留全部现有 Python 资格工具也无法达到全仓 5%。
后续必须等价迁移测试/构建工具，不能删覆盖或改分母达标。

剩余运行阻塞是具体代码能力尚未替代：首次 TLS/OPAQUE enrollment 与 mDNS/本机证书
bootstrap；Node/Centre/Helper 的 Python CLI、配置与对象回调装配；Collector live probe/cache
和外层 tick；部分观察 MCP/HTTP 调用与公开 Python DTO/API。旧 forwarding 510 行只用于
精确历史产物维护，不进入运行包。全部职责见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。
这些未完成实现不能统称为纯 codec；原生 Mac 资格环境缺失也不能由 Linux fixture 替代。

发布端要求 `thin_runtime=RUST_THIN_RUNTIME_V1` 和实测 `clinx-node --thin-runtime-contract`，
独立 Tunnel 要求 `RUST_TUNNEL_SUPERVISOR_V1`。源码清单、安装器、App 与候选包同步校验。
证据目录：`/data/artifacts/clinx-thin-runtime-20261025/`。
[机器回执](cleanup/RUST_THIN_RUNTIME_MIGRATION_RECEIPT.json)包含规模、基线和验证范围。
