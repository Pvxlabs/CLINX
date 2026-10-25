# Rust Node execution API 迁移（2026-10-25）

基线为 `dca2d7d1ef5b4a7ccc5f3f92d28ba7b6ad2839c6`，开始时工作区干净。
本轮接管 Helper 的业务请求路径，没有重复 handoff Authority 实现或目录迁移。
**状态：源码与隔离 Release 资格验证 PASS；NOT_DEPLOYED；25% Python 目标尚未达到。**

## 已切换的执行路径

```text
NodeService（既有认证 envelope / 请求去重）
  → RustNodeExecutionAdapter.from_config（Python 协议客户端）
  → clinx-node / node_api.rs
      - project/thread/task scope 与身份冲突校验
      - canonical owner / provenance / cwd / predecessor 选择
      - prepare/start/status/context/adopt/cancel/reconcile
      - mcp_api.rs 业务映射与 NativeReads 精确 Provider 读取
  → private pipe callback（保留 Rust 生成的 operation_id）
  → Python Commands / NodeAuthorityClient（既有配对 mTLS）
  → Centre（当前认证 node/user/project 交集）
  → Rust Authority node_request（事务、幂等、ownership 与 receipt）
  → 已有 Rust Transport → 按节点路由的 Provider gateway → clinx-provider
```

正常 Helper 不构造 Python RustIntegration、Registry、Dispatcher 或本地执行 worker。
`clinx-node` 是每次调用结束即回收的私有请求进程，没有 listener、数据库或 writer lease。
Mac 端直接使用本机 Provider Unix WebSocket；Authority 请求复用既有配对通道，
没有将 Linux socket/peer credentials 用于跨设备身份认证。节点声明不能提交 Transport 证据。

canonical adoption 与 predecessor checkpoint 仍使用既有 Authority/Transport 契约。
取消仅在 Authority 返回 `CANCELLED` 且资源 `RELEASED` 时报告完成。
丢失或不匹配 callback、部分帧、EOF、超时均不重试；未知提交保留确切 operation_id，
由既有 receipt/Provider readback 恢复。没有新增审批、执行权限或业务状态转换。

## Python 删除范围与剩余接口

`rust_integration.py` 从 497 行减至 149 行：删除 Python 的请求选择、策略构造、
Registry 分页选择、上下文投影、ACK 结果投影和稳定 key 重复实现。
原 API 签名保留为 Rust 客户端，供显式维护、兼容调用和原回归使用。
其中只读 Provider IO 回调保留原注入接口；正常 Helper 使用 Rust NativeReads，不走该回调。
`RustRegistryView` 仅转换 Rust 选出的记录为原有对象视图，不持有或选择任务状态。
`RustNodeExecutionAdapter` 从 146 行减至 64 行，原目标与 handoff 业务判断全部移入 Rust。
原接口注入模式仍执行同一 Rust Node 检查；它不是 Helper 的故障回退路径。

Commands/authority selection 仅为现有传输与配置接口，集中在 `rust_authority.py`；
Centre、监控及 Helper 不再导入整个旧请求映射模块。没有删除完整文件；删除的是重复实现。
新增私有 pipe 的 Python 只负责有界 JSON IO、精确 callback 关联和回收进程。

## 发布与 App

Rust release 新增必选 `--node-binary`、`node_api=RUST_NODE_API_V1`，两个安装/调用端
脚本拒绝缺少该标记或二进制的 release。此处只修改源码，没有执行安装或 staging 到生产目录。

Mac App 构建同时为本机 Apple 架构编译 clinx-provider/clinx-node，分别复制到
Contents/Helpers 并签名、验证；launcher 固定 bundle 内路径，缺少二进制即拒绝启动。
源码包仍使用统一清单；不继承旧业务核心。已验证两种复制包内的真实 Rust API 调用，
并验证增量构建和 release 继承清理。Linux 下的目录复制验证不等于签名后的 Mac App。

## 回归结果

| 验证 | 结果 |
| --- | --- |
| Rust locked Release 全部 binary 构建 | PASS |
| Rust 完整回归（`--test-threads=1`） | 82 PASS；未 skip 或改动原断言 |
| 原 Python Node 行为与完整 callback 序列 | 47 个冻结向量 PASS，来自基线原实现 |
| 原 MCP/API 行为向量 | 原 35 个保持不变并通过 |
| tracked fixtures，基线 / Release 新版 | 76 PASS → 83 PASS |
| Helper → paired Centre → Rust Authority → routed Provider | PASS；包含 canonical predecessor、完成、丢 ACK、重启、重复 start、取消恢复及并发任务 |
| Node scope / canonical owner / thread / turn 冲突 | 原 24 Node fixture PASS；新未知提交 identity 测试 PASS |
| 原本地请求 / native-routing 回归 | 前后均 34 PASS / 2 既有 FAIL，无新增失败 |
| 完整本地套件，继续 collection error 后运行 | 前后均 295 PASS / 31 FAIL / 104 ERROR / 2 原有 skip；432 个 JUnit 身份结果变化 0 |
| 原本地套件保护 | 124 个原测试及支持文件 SHA-256 全部不变 |
| Python 分类/两种发布 import 闭包、受影响 lint、Helper/build shell 语法 | PASS |
| 物理 Mac 原生 Swift/App 构建、签名与实际 Mac→P620 执行 | 未验证；本轮没有部署或操作真实任务 |

完整套件原有失败涉及退役模块/命令、旧部署 fixture、旧 overlay 路径和相应 setup。
其中 104 ERROR 含 95 个 collection error 与 9 个运行 setup error，2 个 skip 为原测试本身行为。
完整的前后日志、JUnit 与逐身份对照保存在本任务证据目录；没有删除测试、skip 或修改断言。
并行 Rust 测试曾复现已知 `WRITER_ALREADY_ACTIVE` 偶发失败；旧证据见
[Rust MCP 迁移](RUST_MCP_RUNTIME_MIGRATION.md)。本轮未修改 Store 锁和该测试，最终串行完整回归通过。

Provider 是隔离的真实 WebSocket 协议 fixture，Authority 为真实 Rust 进程，配对使用真实 OPAQUE/mTLS。
这些验证不能替代物理 Mac 上的新 Helper 签名产物验收，也不能称为生产执行验收。

## 规模与后续阻塞

口径为 Git 跟踪源码扩展名的物理行数，包含资格测试；没有修改 Linguist 排除规则。

| 语言 | 文件数（前→后） | 行数（前→后） |
| --- | --- | --- |
| Python | 101 → 103 | 21,231 → 21,147（净减 84） |
| Rust | 37 → 40 | 17,591 → 18,254 |
| Swift | 62 → 62 | 11,355 → 11,355 |
| ACTIVE_RUNTIME Python | 53 → 54 | 13,071 → 12,819（净减 252） |

新增 Python 文件为私有协议收发客户端及资格测试，不含新增业务核心。
全仓净减少于运行时减少量，是因为新增了保留原断言的执行链与包资格测试。
提交前 GitHub languages 仍为 Python 1,035,146 bytes / 44.93%；新源码扩展名统计约
44.22%，推送后的 GitHub Linguist 可能延迟刷新。**尚未达到 25%，不能称为运行时全 Rust。**

仍需迁移的实际运行职责：

- Node/Discovery 配对注册、mTLS listener/client、Provider 分块去重：依赖现有 OPAQUE、
  证书信任与授权交集、NodeRegistry 协议去重，不能只删除 Python 网络实现。
- ObservationDirectory、监控投影、原生历史与本地 association：Rust MCP 的四个观察工具仍
  委托同身份 Python 观察进程，需要先迁移目录读写、分页、授权重检和来源投影契约。
- 配置解析与上述兼容只读 Provider 客户端：已不承担业务请求选择；在冷入口调用者迁完并
  验证前不能删除。Python 仍是 ACTIVE_RUNTIME，并未重标为 DEVOPS。
- Mac 新产物的物理设备验证：本轮主机为 Linux，未执行 Mac 编译/签名/新 Helper 实机闭环。
  既有旧产物证据不被当作本轮新产物的 PASS。

证据目录：`/data/artifacts/clinx-node-api-rust-20261025`。
保留源码 before/after bundle、patch、原本地 tests 压缩归档、原行为导出脚本、构建日志、
二进制 hash、JUnit、服务只读快照及 Git 提交/推送回执；临时 checkout 和可再生构建结束后清除。
本轮不部署、不重启服务、不修改生产数据库或运行数据。
