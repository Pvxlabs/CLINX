> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 设备运行链迁移

基线 `da7fb7b2ad6835838a396ce9234f9665128bc127`，范围为首次配对/证书、mDNS、
Node/Centre/Helper 装配、Collector 探针及外层调度。代码与候选产物验证不等于部署。
全仓 Python 字节低于 5% 仍是未完成目标。

## 实际替代与保留边界

- **身份与配对**：Rust 直接生成/加载 Ed25519 身份和证书、执行 TLS/OPAQUE enrollment、
  信任原子写入、窗口一次性消费、重连和撤销检查。复用原文件和 wire，未增加准入流程。
  `native/opaque_pairing` 提供同一份共享核心，Kernel 禁用 Python feature，不链接 libpython。
  原 Python SSL/SPAKE2 仅保留显式 DEV_ONLY 资格入口；正常配对不导入这些实现。
- **DNS-SD**：Rust 拥有 IPv4 私网接口、UDP listener、PTR/SRV/TXT/A、候选索引和实际 TTL。
  有界解析拒绝压缩指针循环、截断、畸形记录及超限报文；未知 LAN presence 不授予信任。
  缺失记录主动解析，cache-flush 保留分包的一秒宽限；不以新的 PTR 公告续命旧证据。
- **装配与调度**：`clinx-node-daemon` 直接装配 Helper/Centre、配置、Node Registry、Provider
  relay、现有反向 Authority 路由、Collector 和外层 tick，处理信号与异常收尾。
  正常路径无 Python callback。每请求重读 trust/scope，精确绑定 node/user/project/request。
  参见 [Node daemon 契约](RUST_NODE_DAEMON.md)。
- **Collector 探针**：正常 native/Provider/HTTP IO、精确 thread/turn/cwd 判定、10 秒/128 项
  cache 和固定 writer probe 由 Rust 执行。失败/未知证据保持 UNKNOWN；cache 绑定 project。
  原 native_writer_probe Python 实现也收敛为同一 Rust 探针调用，避免两套取证逻辑。
- **Mac 入口与产物**：Swift 直接调用 `clinx-node --discovery-gui`，PIN 仍为独立 stdin 行；
  Helper launcher 直接 exec daemon。App 清单只含三个 Rust helper，不含 Python 源码、
  解释器、pip 依赖或 OPAQUE wheel。增量构建清除构建目录旧运行文件，保留 Swift 资产。
  原 `monitor` Python profile 只供旧 API/包布局资格使用，当前 App builder 不调用它。
- **Rust release**：源码 staging 增加 daemon artifact 和实际 marker 检查；Centre service
  模板直接启动 Rust。原 Python compatibility profile 仍服务公开 API/Observer 维护边界，
  没有重新发布已经删除的 Bridge/TaskRegistry/M9 等业务核心。

Task Authority、并行执行、幂等、取消、未知 ACK、重启恢复和 metadata outbox 继续复用
既有 Rust 事务；没有第二个状态 writer，也不以节点上报成功替代 Provider 证据。

## 删除的历史资格链

删除 7 个文件，共 2,094 行，其中 5 个 Python 文件 2,008 行：

- `scripts/qualify_runtime_shadow.py`
- `scripts/qualify_runtime_migration.py`
- `scripts/qualify_rust_runtime.py`
- `tools/qualification/runtime_lab/{__init__.py,reference.py,shadow_probe.rs}`
- `kernel/src/bin/clinx-runtime-lab.rs`

它们属于封闭的 NON_AUTHORITATIVE Stage3/P4/P5 资格链，依赖已不存在的
`tests/test_rust_runtime_lab.py`、固定旧源码哈希及旧阶段回执，无当前入口或发布成员引用。
SHA/行数与引用证据保存在本轮回执；历史源码可从基线提交恢复。
`kernel/src/runtime_lab.rs` 仍被 `authority_state.rs` 使用，保留；
`execution_core.rs`、冻结 golden、`check_execution_core_contract.py` 继续验证现役契约。
现有 ignored 本地测试没有删除、修改或新增 skip。

## 验证与实际发现

独立 Rust 进程测试执行 OPAQUE/mTLS 配对、注册、反向 Authority 请求、精确 Provider 证据读取、
并发重复、payload 身份冲突、撤销、取消待证据及丢 ACK 重启恢复。Authority 和桌面 Provider
为明确的私有协议 fixture；这不等于真实 macOS/P620 设备链验收。

- 旧 Python TLS/原 OPAQUE wheel 与新 Rust 双向互通；新旧身份文件重开保持字节和密钥不变。
  测试发现 PKCS#8 v2 不能被旧 Python/OpenSSL 读取，已恢复为标准 v1 写入，保留 v1/v2 读取。
- 原 Python config 与原生 Rust config 的顺序、host aliases、Provider endpoint、symlink/`..`
  等价对照。修复 `~//path` 丢 HOME 前缀和 40 层 symlink 截断；有有限长链和循环回归。
  原生 TOML 遵循 signed-64-bit 数字范围；旧公开 Python DTO 仍保留宽整数 codec。
- mDNS 独立复核补齐 PTR-only 主动解析与分包 flush 宽限；真实私有 UDP fixture 验证，
  未向真实 LAN 发送验收公告。真实多播/Bonjour 跨设备互操作仍需 macOS 环境验证。
- 新后端资格直接验证 Rust binary 缺失/marker 错误时拒绝，不再用 Python wheel 的安装状态
  代表已链接 Rust 后端状态。

完整本地套件保留相同的 432 个 JUnit 身份：之前 295 PASS / 31 FAIL / 104 ERROR / 2 SKIP，
之后 294 PASS / 32 FAIL / 104 ERROR / 2 SKIP。已有 29 个失败和 104 个 error 保持，两个
原失败变为 PASS；**三个原 PASS 变为 FAIL**，不能标为“无新增失败”：

1. `test_missing_backend_fails_closed_before_network`
2. `test_unknown_backend_version_fails_closed`
3. `test_backend_status_does_not_claim_available_when_missing`

它们只修改 `sys.modules['_clinx_opaque']`，故障注入不再触及真实 Rust 后端。
原断言未改，结果保留；新的 Rust 后端故障测试验证替代后的失效边界。
已有 fixture 仅更新新 binary/CLI 的测试输入和启动方式，原 assert AST 全部保留；
另外增加当前包零 Python 源码和无 DiscoveryRuntime 断言。

最终验证结果：

| 范围 | 状态 |
| --- | --- |
| 12 个 Linux Rust Release binaries | PASS；全部匹配本轮源码 |
| Rust tests | 114 PASS |
| tracked fixtures | 400 PASS |
| 旧 Python/Rust 配对、身份双向 oracle 组合 | 17 PASS；原 OPAQUE 另 3 PASS |
| 当前零 Python Helper 包实际 launcher、注册/读取/重启 | PASS |
| 124 个原 local test/support SHA | 未变 |
| 既有被接线调整的 fixture 断言 | 原 89 个全部保留，新增 2 个产物约束 |
| 分类/import/profile、Python E9/F、Shell syntax、Git diff | PASS |
| 完整 local suite | 非全通过；上述三项新增 FAIL 明确保留 |
| Strict Clippy | BLOCKED_EXISTING；前后均为同一 10 条既有诊断 |

固定六语言全部源码字节口径包含测试、资格与工具，未调整 Linguist 或排除范围：

| 语言 | 清理前文件 / 行 / 字节 | 清理后文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 117 / 22,031 / 952,898 | 116 / 20,502 / 873,194 |
| Rust | 74 / 34,312 / 1,357,903 | 80 / 39,150 / 1,546,403 |
| Swift | 62 / 11,355 / 525,634 | 62 / 11,353 / 525,288 |

Python 字节占比 **33.1157% → 29.2674%**，净减少 **1,529 行 / 79,704 字节**。
全仓 <5% gate 仍返回 2，目标未达成。Python 资格类仍有 9,406 行，主要为现役 Rust 契约回归，
这次新增的真实运行链/旧协议验证同样计入统计。完整分类及产物哈希见
[机器回执](cleanup/RUST_DEVICE_RUNTIME_MIGRATION_RECEIPT.json)。

## 尚未完成

全仓剩余 Python 主要为公开客户端/DTO/异常与私有协议绑定、Observer/只读观察 MCP 入口装配、
设备设置 CLI、显式 DEV_ONLY 后端和构建/资格/维护工具。现役资格代码仍验证 Rust 权威与协议，
不能根据文件后缀删除；逐模块用途见 [Python 运行边界](PYTHON_RUNTIME_BOUNDARIES.md)。

**未验证**：原生 macOS Release/Swift 构建、签名、新产物真实 Mac→P620→Mac 闭环、真实 LAN
多播/Bonjour。此主机只有 Linux Rust target，没有 Swift/macOS toolchain。
**未部署**：生产 Authority/Dispatcher/Centre 的 PID、启动时间、ExecStart 前后相同；没有重启、
访问生产数据库或操作真实任务。旧部署产物未修改，仓库推进不自动改变生产 target。
