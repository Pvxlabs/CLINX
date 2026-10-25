# Rust 客户端运行时收敛

基线 `68b2cf23abe07e45dc9ae040b5ff23eed8364b86`。本次继续将 Provider、Node、Observer、
Discovery 和监督策略的实际实现迁入 Rust；不重复前轮 handoff 或目录整理。
目标仍是 Rust 核心、Swift macOS UI、尽可能薄的 Python 兼容层。
**Python 占比仍未低于 25%，本次不宣称全仓迁移完成。**

## 实际变更

- Provider 的正常 Unix WebSocket、JSON-RPC/通知关联、单次发送、writer/read guard、
  事件排空/溢出、精确 native input 回执及多 Provider writer/liveness 证据判定迁 Rust。
  Mac 固定 `lsof`/`ps` IO 仍由 Python 调用，判定由 Rust 完成；Linux peer UID 来自同一个
  Rust socket。真实 Rust transport 取证失败不回退旧 socket，也不信任节点自报 UID。
- Node 的只读并发 fanout、覆盖与精确身份冲突、host 选择、游标、反向 Authority scope/ACK、
  Helper read/status 与注册/心跳/collector 调度迁 Rust。未知写结果保留 UNKNOWN，未新增重放。
- Observer 的 checkpoint HMAC/绑定/过期、远端冷却/身份复检、ActivityReader、managed context、
  MCP 读取，以及历史 ObserverStore 只读查询/事件/状态投影迁 Rust。HTTP auth/路由保留。
- Discovery 的公钥/证书校验、信任写入/撤销/authenticate 事务、一次性 pairing window、
  TXT/TTL/容量/冲突、地址与 Observer bootstrap 规则迁 Rust。既有 Rust OPAQUE 算法不变；
  初次 enrollment TLS、mDNS 和本机密钥 bootstrap IO 仍在 Python。
- 公共 execution semantics/policy 的规范化、build/parse、scope/permit/route 检查和旧 CAS
  哈希委托 Rust；Python 保留公开 dataclass/tuple/JSON codec。此兼容策略没有被接入
  Authority 当前任务准入，不增加审批或权限流程。
- Python Authority client 复用 Rust UnixAuthority，保留原 request_id、超时、精确 ACK 和单次发送。
  Authority/Provider/Observer/MCP relay 共用有界 Unix connect；拥塞时只允许发送前连接等待，
  请求开始发送后不重试。
- Tunnel 恢复预算、LAUNCH_INTENT 重启检查、当前健康、恢复决策和 fsync 回执迁 Rust。
  Python 保留 owned-process/信号/HTTP IO，进程组 drain 的操作循环尚未迁移。
- 删除 `clinx/host/derived_git_targets.py`，现役实现为 Rust business_targets/Host。
  删除 native_provider 的 `new_desktop_client`、`select_writer_client`、
  `select_execution_client`、`cancel_native_turn`、`archived_upstream_failure`。
  最后一个是没有当前调用的 archive 特例，不能称作已完成 Rust 等价迁移。
  `exact_turn` 仍被 `scripts/transfer_rust_native_session.py` 的源/目的端检查调用，故保留。

Authority 的 Task/Execution、任务并行、身份、投递、ACK、幂等、取消、恢复和 metadata outbox
仍由既有 Rust 事务提交；上述观察、发现、监督状态不成为第二个任务权威。

## 发布与入口

统一源码 manifest 已更新全部新增测试与实际职责；DEAD 文件无发布 profile。
Rust release、caller/installer、Monitor 与 Tunnel 候选包要求
`client_runtime=RUST_CLIENT_RUNTIME_V1`，打包实际执行
`clinx-node --client-runtime-contract`。Tunnel 独立包同时携带已校验的 Node binary。
配置编译器增加 `--node-binary`；安装器使用已校验 release 的 binary 解析身份。
两种运行包的所有导入可解析，源码根目录仍为 0 个 Python 模块。

## 验证

- **PASS**：10 个 Linux Rust Release binaries 构建，90 个 Rust tests。
- **PASS**：全部 tracked fixtures **296/296**。包含真实私有 Authority、Provider Unix socket、
  mTLS Node、API/MCP/Helper、按节点执行与证据读取、完成/取消/丢 ACK/重启及并发身份冲突。
  包测试包含独立 Resources + Helpers 的 Helper 冷启动、mTLS 注册、读取和重启；
  包内没有旧 compat 核心或 derived_git_targets，增量拷贝会移除旧成员。
- **PASS**：旧契约差分 298/298、Observer 投影 45/45、Discovery 79/79；
  原样抽取的 AppServer、Mac/Linux writer、Node 和只读 Observer 断言通过。
  这些选定资格不代替完整历史套件。
- **PASS（无新增失败）**：完整本地 432 个 JUnit 身份及结果逐项一致：
  前后均 **295 PASS / 31 FAIL / 104 ERROR / 2 原有 SKIP**。
  124 个本地 test/support 文件 SHA 不变，14 个既有 tracked fixture 的 assert AST 不变。
- **PASS**：Python E9/F、结构/import/profile、Shell syntax、diff whitespace。
- **PASS**：独立 Tunnel 候选包在无 checkout import、无 CLINX_NODE_BINARY 环境下启动，
  使用包内 Rust binary，对私有 `/usr/bin/true` fixture 精确消耗 2 次启动预算后 HOLD；只读 status 返回非健康。
- **BLOCKED_EXISTING**：严格 Clippy 仍为相同 10 项历史诊断；17 项新增诊断均已修复。
  完整历史测试仍有已退役模块导入、旧 CLI/发布布局、隔离环境无 systemd 用户总线，
  以及 Python 3.14 TLS 异常类型等失败，没有 skip 或弱化断言。
- **未验证**：原生 macOS 构建/签名及这批新产物的物理 Mac→P620→Mac 完整闭环。
  当前验证主机仅有 Linux Rust target，无 Swift/macOS toolchain，也没有已配置的 Mac SSH 验证入口。
- **NOT_DEPLOYED**：三个生产服务的 PID、启动时间和入口前后完全一致；未部署、重启服务，
  未读写生产数据库、真实任务或运行数据。

整批验证曾暴露两个已修复问题：旧显式注入 transport 的 peer UID 接口不兼容（3 个新增失败）；
PairingWindow 最终化回调在 GC 期间同步 RPC，重入 worker 锁导致死锁。
前者保留显式注入对象的本地 socket 接口而不放宽真实 Rust transport；后者改为最终化时只入队，
独立线程执行一次清理，正常配对同步消费不变。强制持锁 GC 的有界回归通过，最终 296 项全过。
首次完整 fixtures 因死锁被限于任务进程中止，原始诊断和失败回执保留。

## 实际规模

物理源码行/字节；沿用 Python/Rust/Swift/Go/C/Shell 六语言统计，包含资格测试，不是 GitHub
Linguist 的实时百分比。没有更改语言识别，也没有删测试改变占比。

| 项目 | 迁移前 | 迁移后 |
| --- | ---: | ---: |
| Python 文件 / 行 / bytes | 107 / 21,442 / 983,411 | 113 / 22,487 / 976,532 |
| ACTIVE_RUNTIME Python 文件 / 行 / bytes | 55 / 11,699 / 532,708 | 54 / 10,185 / 424,101 |
| Rust 文件 / 行 / bytes | 55 / 23,911 / 955,374 | 65 / 29,872 / 1,188,254 |
| Swift 文件 / 行 / bytes | 62 / 11,355 / 525,634 | 不变 |
| Python 字节占比 | 39.2538% | 35.7525% |

整文件删除 **1 个、89 行**；旧 Provider 的 5 个函数删除包含在运行 Python 净变化中。
运行 Python 净减 **1,514 行 / 108,607 bytes**；全仓 Python 净减 6,879 bytes，
因新增资格测试和兼容回调，物理行数增加 1,045。没有把函数删除重复计算成文件删除。

完整剩余职责见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。其中 app_server 的
ProcessStdio/LocalStdio/SSHStdio 与必要 WebSocket base 有 4 个原样 PASS 的资格接口测试，
虽无当前运行调用，本轮保留；删除会新增资格失败，需后续等价替代。正常 Helper 不使用这些启动器。
Python 初次 enrollment、HTTP/mDNS 生命周期、进程 IO 与配置/公开客户端参数 API 均明确仍在运行，
不称为已完成全量退役。

证据：`/data/artifacts/clinx-runtime-convergence-20261025/`。
[机器回执](cleanup/RUST_CLIENT_RUNTIME_MIGRATION_RECEIPT.json)记录基线、规模、测试与服务证据。
任务独占源码副本、私有测试状态和可再生构建按 workspace policy 回收；日志、原测试归档、
源码 bundle/patch、产物摘要与 Git 推送回执保留。
