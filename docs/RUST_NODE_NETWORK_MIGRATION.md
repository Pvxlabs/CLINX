# Rust Node 网络、Registry 与执行请求迁移

基准 `3a6372e8af57a2af83dff4d181c712ee6251e135`，2026-10-25。
本轮落实 Node/Centre 运行核心迁移，不重复 Authority、Provider exchange、Host、MCP 与
canonical handoff/adoption。**全项目运行时仍未全部退役 Python，25% 目标尚未达成。**

## 已替代的运行职责

| 原 Python 职责 | 当前 Rust 实现 |
| --- | --- |
| `PairedTLS`、`NodeRPCServer` 的网络 listener、现有配对身份/证书验证、正常 paired client | `node_tls.rs`、`node_network.rs` |
| `CentreService` 的认证、双方 scope 交集、注册/心跳、反向请求 principal 构造 | `node_centre.rs`、`node_scope.rs` |
| Node owner/Centre 身份与当前 scope 重检、Provider/Node 分流 | `node_owner.rs` |
| Provider 上传缓冲、分块身份/digest、IN_FLIGHT 封存、READY/CONSUMED 回复及配对远端回读 | `node_relay.rs`、`node_paired.rs` |
| Centre-local Provider IPC socket、Linux UID 检查与 observer 方法限制 | `node_gateway.rs`，复用 `remote_provider.rs` 客户端 |
| `NodeRegistry` 的 Node/scope/reference/thread index、请求账本与独立 callback fence | `node_registry.rs`、编译时嵌入的既有 `node_registry.sql` |
| NodeService 的执行授权、canonical owner、prepare/start/status/cancel/reconcile、未知请求恢复 | `node_service.rs`，执行 callback 仍调用既有 Rust Node API |

没有新的 Python 业务状态机。Python 保存类型/配置和私有协议客户端，剩余观察投影及入口
supervision 明确仍为 ACTIVE_RUNTIME。`network_worker.py` 只做私有 stdio 收发、并发回调和
子进程收尾；它不持有网络 socket、Provider buffer、SQL connection 或任务执行状态。
`node_protocol.py` 中的旧 SQLite 实现、`_idempotent`、`_canonical_request` 与 Python start
恢复判断已删除；`provider_relay.py` 中的 buffer 状态与 socketserver 实现已删除。
原文件仍有有效客户端/观察职责，因此本轮删除整文件 **0 个**，不把文件改名计作退役。

## 身份与不确定结果

- 复用已部署的 `identity.json` / `peers.json` / `sharing.json`、`store.lock`、0700/0600、
  原子写入与 fsync。保留 Ed25519 自签 CA 证书及 OPAQUE_V1 信任，不要求重新配对。
- TLS 仅 1.3。自定义验证只适配现有自签身份：检查有效期、自签名、原始公钥 fingerprint、
  唯一受信任 peer 和标准 TLS handshake signature；没有关闭签名验证。每连接重新读取撤销状态。
- Centre 从认证证书和当前注册/双方 scope 构造 node/user/project principal，忽略请求中自报的
  principal。Node owner 在 dispatch 前再次验证已配置 Centre 和当前双方授权。
- Linux peer credentials 仅用于 Centre 本机 Unix IPC；Mac hop 始终用现有 paired mTLS。
  workspace 与 Provider 证据继续来自实际 Provider 契约，不能由 node.status 推导业务成功。
- 上传在任何 Provider I/O 前封存。重复 final chunk 不再执行；消费完的 read 不重放；
  断连、callback 失败和 buffer 丢失保留 UNKNOWN/RECONCILIATION_REQUIRED。
  buffer 是临时协议状态，不是 Authority receipt；重启丢失 buffer 不能证明未投递。
- Registry 保留既有 schema 和 JSON payload hash 编码，包括 Python `ensure_ascii=True` 的
  Unicode/浮点历史字节。Rust 在 BEGIN IMMEDIATE 中原子检查并 claim；跨进程也只有一个 NEW。
  已 IN_FLIGHT 的 start 仅查询精确 execution，不再次 start。独立 callback fence 保留；
  Rust Authority 执行路径不取得第二个 Node writer fence。
- 完成/取消/metadata outbox 的权威提交仍由既有 Rust Authority 原子事务负责，本轮没有
  新增写入入口、重放策略、审批或权限流程。保持任务、execution、thread、turn 的关联。
- Node v1 继续接受 newline 或 EOF 上完整的 JSON object；截断 JSON 和超限 frame 失败。
  私有 worker 用更大的有界 frame 容纳既有合法 Unicode 请求的历史 ASCII hash 字节，
  未扩大网络 wire 上限或 Authority IPC 上限。

## 发布与验证

release 和 Monitor 源码共用 `architecture/python-sources.json`。新增私有 Worker 仅进入
现有 runtime profiles；资格 fixture 不进入包。release 标记及安装检查要求
`node_network=RUST_NODE_NETWORK_V1`，打包实际检查 `clinx-node --network-contract`。
Mac build 使用本机 Rust 编译 `clinx-node` / `clinx-provider`，检查契约并分别签名。
不会继承已退役 Python core，也不会把 Linux ELF 当作 Mac Helper。

| 验证 | 结果 |
| --- | --- |
| Rust Release 全 binary 构建；完整 Rust suite 串行 | PASS，82 tests |
| tracked fixtures，Release binaries，私有 HOME/XDG/TMPDIR | PASS，98 tests，原 83 项保留 |
| 原 OpenSSL client ↔ Rust server / Rust client ↔ 原 OpenSSL server | PASS，含 EOF 完整 JSON、撤销与过期证书 |
| Registry 历史 Unicode/浮点 hash、两个 worker 并发 claim、重启后 IN_FLIGHT | PASS |
| 同请求并发执行、精确 execution 恢复、重复取消、Provider 未知/消费回复 | PASS；不重放未知执行 |
| release/Monitor 源码闭包、脱离 checkout 的同级 Rust binary 调用、退役残留 | PASS |
| Python 分类/import、受影响 lint、Helper/App build shell 语法 | PASS |
| 完整本地旧套件 | 前后均 295 PASS / 31 FAIL / 104 ERROR / 2 原有 skip；432 JUnit 身份变化 0 |
| 原本地 tests 保护 | 124 文件 SHA-256 不变；没有删除/skip 测试或修改原断言 |
| strict Clippy `-D warnings` | BLOCKED：基线和本轮同样 10 项旧诊断；新增 Node 模块无告警 |
| 新产物物理 Mac 编译/签名/跨设备执行、回执和恢复 | 未验证；当前 Linux 没有 Swift/macOS target，不部署或操作真实任务 |

旧套件失败仍涉及已退役模块/命令、历史平铺入口、部署 fixture、旧 overlay 等。
旧 Node runtime 子进程测试仍调用已删除的根目录脚本；该失败前后相同。
两处 tracked fixture 只更新构建输入：Provider 包测试携带新必需的 Node companion；
源码继承单测的合成 executable 提供契约探针 stub。所有原断言保留；实际 Rust binary
契约由新增独立 fixture 验证。没有通过删除断言来放行。

过程中的新增失败均修复：client 参数名与 payload.operation 冲突、无返回值 callback
误序列化 Mock、包 fixture 未带新 Node binary、合成打包 binary 缺少契约 stub。
资格环境的长 Unix socket 路径另造成 11 setup error；缩短任务私有 TMPDIR 后使用同一套
测试重新验证。原始失败日志均保留，最终新增失败 0。

## 规模与剩余运行边界

物理行数/字节统计覆盖 Git 跟踪源码及本轮新增源码，包含资格测试，不修改 Linguist。
同口径扩展名与 shell shebang 的基线字节数与 GitHub languages API 完全相同。

| 项目 | 迁移前 | 迁移后 |
| --- | ---: | ---: |
| Python 文件 / 行数 | 103 / 21,147 | 105 / 21,188 |
| ACTIVE_RUNTIME Python 文件 / 行数 | 54 / 12,819 | 55 / 12,495 |
| ACTIVE_RUNTIME Python bytes | 632,578 | 602,841 |
| Rust 文件 / 行数 | 40 / 18,254 | 50 / 20,656 |
| Swift 文件 / 行数 | 62 / 11,355 | 62 / 11,355 |
| Python 全语言字节占比 | 44.22% | 42.21% |

Python runtime 净减 324 行 / 29,737 bytes；全仓 Python 净增 41 行，原因是新增私有
协议客户端和 15 项资格测试。三个原运行模块的 diff 删除 843 行，包括 SQL、导入与空行；
这是物理删除口径，不声称删除了 843 行纯业务逻辑。新增 Rust 2,402 行承担实际职责。
GitHub Linguist 可能延迟刷新；本表源码计量不等同于已部署语言占比。

仍须后续迁移，不能删或改标为 DEVOPS：

1. `clinx/provider/native_history.py`、Provider 观察客户端、`clinx/monitor/`：原生历史、
   ObservationDirectory、association、实时证据投影、四个 MCP 观察 callback。
   NodeRouter 的观察搜索/coverage 和 cursor 包装也依赖这条链；下一包优先收口这部分。
2. `clinx/discovery/`：身份创建、OPAQUE enrollment、mDNS 和 LAN pairing 控制仍在 Python。
   本轮迁的是既有配对身份的 Node 通信，不能宣称 discovery/enrollment 一并 Rust 化。
3. `clinx/nodes/*entrypoint.py`、`clinx/transport/`：启动、注册/心跳循环、配置及 tunnel
   supervision 仍为 Python；要在上述观察/配对协议接管后才能删除。
4. `clinx/contracts/` 配置解析、`clinx/authority/` IPC/私有 callback 绑定：调用者尚未
   全部变为 Rust supervisor，仍需维护现有配置与身份路径。显式注入的 Python SSL client
   兼容分支仍保留；正常 paired Node 路径已走 Rust。
5. 新 macOS 产物的原生 toolchain、签名与独立测试身份设备闭环：需要在 Mac 验证，不用
   Linux 协议 fixture 或旧版本上线证据代替新产物结果。

证据：`/data/artifacts/clinx-node-network-rust-20261025`；短 socket 临时目录
`/data/artifacts/cn-net-25` 同属本任务，结束回收。保留源码 bundle、原 tests 归档、patch、
构建/测试/失败日志、binary hashes、逐测试对照和 Git 提交/远端回执；临时 checkout、
私有状态、venv 与构建 target 在收尾清除。生产服务、部署 target、生产数据库与任务不变。
