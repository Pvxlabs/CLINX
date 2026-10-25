# Rust 资格测试与安装维护迁移

基线 `2db6553851f88fb6ce85a9df03bcff3eb0c69ee5`。本轮推进现役资格代码和最后一项
发布包 Python 维护依赖的迁移，没有部署。全仓 Python <5% 目标仍未完成。

## 用例迁移与保留边界

9 个 Python fixture 的原 216 个参数化用例现有明确去向：

| 范围 | 原参数化用例 | Rust 测试函数/组 | 保留 Python 绑定测试 |
| --- | ---: | ---: | ---: |
| Observation、Collector | 54 | 53 | 4 |
| Native Observer、Observer HTTP | 65 | 24 | 8 |
| Execution/Provider 契约、配置、Discovery index/state | 97 | 95 | 17 |

Rust 测试组包含原参数循环，部分 Python 绑定验证同时保留核心 Rust 验证，以上数字不能
直接相加作为原用例数。原 91 个测试函数全部映射；没有未映射的原函数，也没有新增 skip。
迁移后移除 187 个重复 Python case 实现，保留 29 个 Python 特有绑定检查。

[逐用例映射](../architecture/qualification-migrations.json) 固定原提交、源码 SHA、参数、
对应 Rust 测试和保留位置；完整 assert/raises 对照存于本轮本机证据目录并记录哈希。
旧测试源码始终可由原提交恢复；没有删除 ignored `tests/` 或修改其断言。

仍保留的 Python 检查包括异常对象身份与 `__cause__`、tuple/dataclass 返回、100 位整数
wire 解码、bytes 绑定、SQLite snapshot context manager、锁内 GC finalizer、只读 subclass、
进程关闭投影和并发 callback patch。其他 fixture 仍复用的 native/history helpers 原样保留。
资格子进程 `kernel/examples/qualification_provider.rs` 是独立 Rust JSONL/WebSocket oracle，
不执行 Python，也不进入发布清单；运行 Rust 集成测试前需先 `cargo build --examples`。

保留的关键故障条件包括：真实 Rust worker 的两种 kill 时序、SQLite 真实读锁阻塞、
丢 ACK 后精确批次重发、checkpoint/stream 重启、五种非法 ACK、scope/身份变化、背压与公平性
上限、Provider writer/证据冲突、私有 HTTP 鉴权，以及无原生数据库变更。
独立复核补回旧 `json.dumps` 的带空格 cursor 字节和 Python `TimeoutError` 转离线的原测试，
没有用模拟错误替代原来的进程死亡或数据库事务时序。

## 最后一个发布包 Python 维护实现退役

新增 Rust `desktop_socket`，由 `clinx-provider --desktop-socket` 调用，原
`clinx/provider/desktop_socket.py` 已删除。安装器直接通过既有 `runuser` 身份调用原生入口。

保留原 socket 路径与 owner、Linux peer UID、精确 witness thread、socket device/inode、
固定 alias、三类既有 ACL 主体和先 grant 后复查再 rename 的顺序。不会启动 Provider、
扫描替代 endpoint 或重试请求。临时 alias 文件发生碰撞时保留已有文件并拒绝发布；
这是对原失败清理行为的保守修复。

6 项隔离维护测试覆盖真实 Unix/WebSocket Provider 读、错误 witness、读取中 inode 替换、
owner/路径/peer 冲突、dry-run、ACL 失败、证据变化和临时路径碰撞。ACL 与 alias 发布的
变更验证仅使用私有路径及注入的 ACL runner；没有执行真实安装维护或改生产 ACL。

`rust` Python source profile 从 12 文件降为 **0**。旧 release 的 deps overlay 不再继承，
只保留已有原生 `host-worker`，其余二进制和 schema 从本轮明确清单生成。
新清单要求 `desktop_socket_maintenance=RUST_DESKTOP_SOCKET_V1`，并声明
`python_sources=[]`、`python_dependencies=[]`；stager 实际探测 Provider contract。
Monitor 当前资源包继续为零 Python。构建/安装脚本本身仍在仓库中，属于后续迁移范围。
打包资格产物使用合成二进制探针，验证成员、marker 和文件哈希；14 个真实 Linux 二进制
的 Release 构建、维护 marker 与动态链接检查另行验证，未将资格包安装到生产。

打包测试中原“必须包含 desktop_socket.py”和“保留第三方 deps”的预期被新产物契约替代，
现在明确断言维护 marker、Python 源码为零及 deps 目录不存在。测试未被删除，其他原产物
完整性和退役核心检查继续执行；这两项预期变化有上面的实现和独立维护验证作为依据。

## 验证与规模

| 验证范围 | 结果 |
| --- | --- |
| Linux Release | 14 binaries PASS，无 libpython 链接 |
| Rust 全部测试 | 298 PASS；含 172 个迁移资格测试函数/组、6 个维护测试 |
| 剩余 tracked Python fixtures | 244 PASS；基线 431 个的差额为上述 187 个已迁移 case |
| 完整旧本地套件 | 前后均 294 PASS / 32 FAIL / 104 ERROR / 2 SKIP，432 个身份结果逐项相同 |
| 原本地文件 | 129 个哈希不变，包括既有缓存；原测试/支持源码未修改 |
| 分类/import/profile、E9/F、Rust format、shell、diff | PASS |
| Strict Clippy | BLOCKED_EXISTING：相同 10 条 library 诊断；非 strict 另有原 workspace_routes 告警，无新增诊断 |
| 原生 macOS build/signing、真实 Mac/P620 新产物链 | 未验证 |
| 生产 | NOT_DEPLOYED；Authority/Dispatcher/Centre PID、启动时间及 ExecStart 前后不变 |

首次基线快照缺少 Git metadata 导致两个打包测试失败；恢复基线精确 Git ref/index 后，两项
及同文件其他用例均通过。该环境失败日志保留，没有作为既有产品失败隐藏。
本轮移植中发现并修正的测试输入/编译/lint 问题均保留日志，最终验证没有新增失败。
只读服务核对显示已安装 Dispatcher/Centre 仍运行原 Python 发布入口；本轮的新源码和
原生发布契约不会自动改变已部署版本，也不能据此宣称生产 Python 已退役。

固定六语言全部源码口径，包括测试、资格、工具和 Rust 资格 example，未改变 Linguist：

| 语言 | 之前 文件 / 行 / 字节 | 之后 文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 120 / 21,216 / 911,158 | 119 / 19,013 / 828,724 |
| Rust | 86 / 40,939 / 1,613,226 | 97 / 46,442 / 1,810,134 |
| Swift | 62 / 11,353 / 525,288 | 62 / 11,353 / 525,288 |

删除 1 个完整 Python 文件（91 行）；全部 Python diff 删除 2,301 行、增加 98 行，
**净减少 2,203 行 / 82,434 字节**，其中资格 fixture 净减少 2,105 行。
Python 字节占比 **29.4956% → 25.8685%**，`<5%` gate 仍为 NOT_MET。

剩余 Python 主要是未迁移的 Host/Node/Provider/跨进程资格、公开兼容 DTO/注入 API、
显式 DEV_ONLY SPAKE2 和构建/维护脚本。它们仍有原用例或工具消费者，不能根据后缀删除。
下一批应沿用逐用例映射迁移剩余资格，再收口工具及其兼容闭包；发布包已经不依赖 Python，
仓库内公开兼容层仍未达到足够薄的目标。

机器回执：[RUST_QUALIFICATION_MIGRATION_RECEIPT.json](cleanup/RUST_QUALIFICATION_MIGRATION_RECEIPT.json)。
本机证据目录：`/data/artifacts/clinx-qualification-runtime-20261025/`；保留日志、原测试归档和
源码 bundle，结束时回收本轮临时 checkout、venv 和可再生编译输出。
