> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 维护工具与 Monitor 兼容闭包收敛

基准：`26af9ee3f5f76979e6b1f28a1ec2ea3ace512acf`，2026-10-25。
本轮完成源码迁移、隔离回归与发布残留验证；没有部署、重启生产服务或操作真实会话。
全仓 Python 字节占比从 **19.82% 降到 16.89%**，**低于 5% 的目标仍未完成**。

## 实际退役

| 删除范围 | 原 Python 行数 | 替代及删除依据 |
| --- | ---: | --- |
| `clinx/monitor/` 全部六个源码、`authority/rust_reads.py`、`nodes/node_service_entrypoint.py` | 1,796 | 正常 Helper/Collector/Observer 已由 Rust 装配；资格改走原生 IPC、真实 Centre/Helper 和独立 SQL/HTTP oracle，不再构造 Python factory |
| `authority/rust_integration.py` | 149 | 最后维护调用者已迁 Rust；正常 Node/MCP 原生契约与先前迁入 Rust 的请求资格保留；同时删除 Rust 中无消费者的 `api_compat` read callback 后端 |
| `scripts/transfer_rust_native_session.py` | 163 | 独立 Rust `clinx-maintenance transfer-native-session`，复用现有 Provider/Authority 契约及 OS 身份边界 |
| `scripts/local_discovery_smoke.py` | 171 | Rust example 使用两个真实 Node 进程验证配对、重启及双向 mTLS；物理 LAN 模式需显式选择 |
| `fixtures/test_rust_collector.py`、`test_rust_observer_http.py` | 261 | 10 个用例已有对应 Rust 资格；逐项原断言及替代测试记录在映射中，没有把取消断言或 skip 作为 PASS |

共删除 **13 个 Python 文件、2,540 行、95,176 字节**。其余 fixture 接线精简后，
全仓 Python 净减少 **2,645 行、98,746 字节**。未修改被 Git 忽略的本地 `tests/`。

新维护命令属于 DEVOPS，不进入 Rust runtime release 或 Monitor App。正常运行包继续采用
显式原生二进制名单，继承旧 release 时只取允许的 `host-worker`；资格额外注入旧
`clinx-build`、`clinx-maintenance` 并断言新包不携带它们。Monitor 只打包
`clinx-provider`、`clinx-node`、`clinx-node-daemon` 三个 Helper，Python 源码和依赖清单均为空。

## 维护与协议语义

`clinx-maintenance transfer-native-session --help` 在 root 检查和配置 IO 前返回。
实际维护要求显式 `--destination-home`；目标 Provider endpoint 与 runtime host 使用
已编译的 `mcp-runtime.json`，Authority UID 从既有账户解析，不沿用硬编码 UID 或猜测目录。

归档、复制、resume、adopt 前均持久化阶段 receipt；adoption operation ID 与完整参数
在发送前 fsync。未知 ACK 停止，不自动重放；Authority 是唯一业务状态提交者。
验证覆盖精确 thread/turn/cwd、完整 turn 证据、archive 前后 inode、可写 FD、独占目标文件、
同 FD 校验和 hash、目标 inode，以及 Authority 提交丢 ACK 后重启的精确 receipt 读回。
root/runuser/dispatcher mount namespace 仍使用已有身份边界，没有新增权限流程。
所有测试仅使用私有临时文件、Provider、Authority 和当前测试 UID。

Discovery smoke 使用两个 Node 子进程和持久化私有身份，验证 OPAQUE、重建进程后无需 PIN
的双向 mTLS、错误 TLS 身份拒绝与无执行权限。PIN 仅经私有管道和内存传递。
本轮运行 `--loopback`，另有 7 个私有 UDP DNS wire 测试；没有运行 `--lan` 或真实 Mac 配对。

Monitor 迁移保留 **176 条原检查中的 172 条语义义务**，另 **4 条 Python 对象专有检查明确退役**，
没有把它们宣称为等价 PASS。历史 SQL/HTTP 期望来自冻结基准 oracle，当前 Rust 输出不生成自己的期望值。
真实 Centre/Helper 正反对照证明：仅配置 execution Provider 时，三条 native 事件持久上传，
观察 Provider 调用为零；显式指定 observation Provider 后，精确读取目标 thread/turn 并得到 LIVE 证据。
两组输入历史数据库字节均未变化。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| `cargo build --locked --release --bins` | PASS；16 个构建二进制，均不链接 libpython，开发工具不等于发布成员 |
| `cargo test --locked` | **452 PASS / 0 FAIL / 0 ignored** |
| 本轮维护资格 | 14 PASS；已包含在 452 项中 |
| Monitor 范围 Rust / Python 资格 | 48 / 32 PASS；分别属于对应完整测试集，不重复累计 |
| Discovery smoke / 私有 UDP mDNS | 2 / 7 PASS；已包含在 Rust 总数中；实际 example loopback CLI 也通过 |
| tracked Python fixtures | 基准 160 PASS → 当前 **150 PASS**；减少的 10 项已逐项迁入 Rust |
| Rust release / Monitor 资源打包残留 | PASS；真实构建器生成的资源无 Python，不继承退役源码或维护二进制 |
| Ruff E9/F、源码分类、diff 空白检查 | PASS |
| 普通 all-target Clippy | PASS，前后相同的 11 条既有诊断，无新增；严格零警告门禁仍不通过 |
| `cargo fmt --check` | 既有失败；路径与行号归一后与基准完全一致，涉及相同 7 个文件 |
| 原生 macOS Swift/App 构建、签名、物理跨设备 LAN | **未验证**；Linux 资源装配与私有进程测试不替代实机验收 |
| 真实 root/runuser/nsenter 会话维护 | **未执行**；未获得操作真实任务的授权，本轮源码任务也不需要此操作 |

保留的本地历史测试全量结果：

| 范围 | PASS | FAIL | ERROR | SKIP |
| --- | ---: | ---: | ---: | ---: |
| 同环境、同依赖、基准源码 | 222 | 35 | 118 | 2 |
| 当前源码、原本地测试不变 | 215 | 42 | 118 | 2 |

377 个测试身份不变；新增的 **7 个 FAIL** 全部访问已删除的 `node_service_entrypoint.py`
（2 个 ambiguous 参数、5 个 invalid 参数），对应 Rust Helper 参数资格通过。
原有 35 个 FAIL、118 个 ERROR、2 个 SKIP 仍保留，不能称本地全量测试 PASS。
另有一个必须排除的表面 PASS：旧 `test_missing_provider_config_does_not_fall_back_to_execution`
会因入口文件消失提前满足 `raises(FileNotFoundError)`，已不证明配置语义；该语义以 Rust
`missing_provider_config_never_falls_back_to_execution` 的真实 Helper 测试为证。
294 个原本地文件的 SHA256 均不变。详细状态变化和替代证据见机器回执。

## 固定口径规模

统计全部 tracked 与未被忽略的 Python/Rust/Swift/Go/C/Shell 物理源码，含构建、维护和资格代码。
不修改 Linguist、不排除测试、不把源码改后缀以降低比例。

| 语言 | 文件数：前 → 后 | 行数：前 → 后 | 字节数：前 → 后 |
| --- | ---: | ---: | ---: |
| Python | 104 → 91 | 15,172 → 12,527 | 651,861 → 553,115 |
| Rust | 117 → 124 | 53,448 → 55,620 | 2,072,382 → 2,157,670 |
| Swift | 62 → 62 | 11,353 → 11,353 | 525,288 → 525,288 |

`clinx-source-audit` 返回 2，表示 **NOT_MET**，不是通过。根目录 Python 文件为零。

## 剩余 Python 的明确边界

| 分类 | 当前行数 | 当前消费者和后续处理 |
| --- | ---: | --- |
| COMPAT | 5,446 | Provider/Node/Discovery 的公开 DTO、异常、私有 IO 与可注入对象仍被 tracked fixtures、本地 API 资格和 Python 配置工具使用；不进入原生 release。需要将独立协议 oracle 和参数用例迁到 Rust 后整块退役，不因旧 API 自身存在而永久保留 |
| DEVOPS | 1,795 | `stage_rust_release.py`、`stage_rust_callers.py`、`install_rust_services.py`、Tunnel 与历史 forwarding 构建/恢复工具，以及清单拷贝工具仍是实际入口；本轮并未把所有开发维护工具迁成 Rust |
| QUALIFICATION | 5,286 | 现役 fixture、OpenSSL 互操作、Provider wire oracle、隔离/物理撤权和历史构建资格；无语言必须为 Python 的要求。验证义务仍有价值，重复或失去目标的资格可删除，其余迁为 Rust 测试或专用工具 |

兼容层不是目标架构的必要组成。当前保留只来自可列出的消费者，不能将未迁完的资格与维护入口
说成已经完成。下一批应迁移 release/install 工具及其资格，再按 Provider/Node/Discovery
消费者闭包删除公开 Python facade；正常业务热路径已经不需要这些 Python 模块。

部署状态与源码状态分开：生产 Authority/Dispatcher/Centre 的 PID、启动时间和入口读回均与
基准一致；已安装的 Dispatcher/Centre 仍运行旧不可变 release 中的 Python 入口。
本轮不部署，所以不会改变它们，也不删除其发布目录、数据库、证书或回滚资源。

## 证据入口

- [源码/检查迁移映射](../architecture/maintenance-convergence.json)
- [机器回执](cleanup/RUST_MAINTENANCE_CONVERGENCE_RECEIPT.json)
- [当前 Python 用途](PYTHON_RUNTIME_BOUNDARIES.md)
- 本机原始日志、before/after JUnit、源码 bundle、文件 hash 与推送回执：
  `/data/artifacts/clinx-maintenance-convergence-20261025/`。

临时基准 checkout、任务 venv/target 和隔离测试运行目录在保存证据后移除；不删除源码恢复 bundle、
原测试压缩包、已部署 release 或借用的 OPAQUE wheel。提交身份以 Git 及证据目录中的 integration receipt 为准。
