> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](../CONTRACTS.md)与[架构](../REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# 第五轮：按目标架构收敛源码、入口和发布边界

日期：2026-10-24（Asia/Shanghai）。基准 main：
`8e0cd453e80c54823bb7fed82f635ba5394c02ce`（第四轮清理）。开始时受控工作区干净。
本轮只集成源码，不部署、不重启服务、不修改生产数据库或运行数据。

**结构收敛 PASS；完整 Rust 运行时迁移 BLOCKED。** 根目录从 59 个 Python 模块变为 0，
入口、包导入、服务模板、Rust workspace probe、Mac Helper 和发布源码清单同步调整。
真正由 Rust 替换的是 stdio byte relay；其余 Python 仍承担的职责没有因搬家而退役。

## 交付范围

- 标准包结构及每个 Python 文件的角色、用途、发布成员：
  [架构入口](../REPOSITORY_ARCHITECTURE.md)、[源码清单](../../architecture/python-sources.json)。
  ACTIVE_RUNTIME 明示现役 Python 实现；COMPAT 明示未迁移业务核心，未改称纯适配层。
- [module-moves.json](../../architecture/module-moves.json) 记录旧路径与拆分符号；
  生产不存在平铺旧 import shim。CLI 集中在 `bin/` 或 `python -m clinx.<职责>.<模块>`。
- 将 consultation schema、纯 Provider liveness 判断、公共身份解析和只读 native association
  从 SQLite/Registry/恢复实现拆开；写入、lease 和恢复转移留在 `clinx/compat/`。
  Rust profile 的 AST 导入闭包以及复制产物的全部模块加载均不引入 `clinx.compat`。
- 删除 `rust_stdio.py`，新增 `kernel/src/bin/clinx-mcp-relay.rs`，保留透明字节传输、
  连接超时、stdin 半关闭和对端 EOF 退出语义；不解析 MCP、不重试已发送输入、不持有任务状态。
- Rust release 与 Monitor bundle 共用显式 JSON 清单。Rust release 不继承旧 control 源码，
  排除整个 COMPAT；必须同时提供已验证的新布局 Authority 和 relay binary。
  Manifest 使用 `source_layout=qualified-clinx-v1`，安装/维护入口验证布局。
- Mac Resources 使用 `clinx/...`，增量拷贝清理旧平铺源码、旧 Discovery 和退役包，
  保留解释器、原生 wheel、Swift 资源。Swift 和 Helper launcher 同步引用新路径。
- 旧 forwarding builder 和 Tunnel control overlay 拒绝打包当前布局；历史精确产物复现
  保留，Tunnel supervisor 独立打包继续使用现有安装文件名 ABI。

## 规模与真实删除量

统计口径：受控源码物理行数（含注释/空行），加本轮待提交源码；不含被忽略的本地 tests、
第三方安装依赖和构建输出。包 initializer 按实际物理行数计入。

| 范围 | 清理前文件 / 行 | 清理后文件 / 行 |
| --- | ---: | ---: |
| Python 全仓库 | 104 / 38,750 | 125 / 39,103 |
| 根目录 Python | 59 / 30,227 | 0 / 0 |
| Rust | 20 / 9,416 | 21 / 9,511 |
| Swift | 62 / 11,355 | 62 / 11,355 |
| Rust release Python profile | 47 / 16,470 | 56 / 15,693 |
| Monitor Python profile | 63 / 31,280 | 78 / 31,591 |

**实际删除实现：1 个文件、32 行（Python stdio relay）。** 根目录其余 58 个文件归包，
不计作业务代码删除。公共模块拆分、包 initializer、结构/回归工具与新 fixture 使 Python
总规模净增 353 行，不能据根目录归零宣称 Python 运行时大幅缩减。Rust profile 减少
777 行，来自排除旧 Registry identity/liveness/恢复依赖；Monitor 仍需兼容核心。

| Python 分类 | 文件数 | 行数 | 实际用途 |
| --- | ---: | ---: | --- |
| ACTIVE_RUNTIME | 59 | 16,656 | IPC 请求映射、MCP、Provider/历史证据、Host 子进程、Dispatcher transport、Node/Discovery、监控 |
| COMPAT | 27 | 16,662 | Mac 可写 Registry/Bridge/M9、完成/取消/恢复、metadata outbox，以及历史发布工具 |
| DEVOPS | 18 | 1,949 | 构建、候选发布、维护工具，不进入运行源码 profile |
| QUALIFICATION | 21 | 3,836 | fixture、参考实现、测试重定位、隔离资格检查 |
| DEAD | 1 个记录 | 0 | 已删除的 Python stdio relay，记录其 Rust 替代 |

非 Python 的边界：`clinx-runtime` 和共享 Rust Authority/state/store 属于 ACTIVE_RUNTIME；
`kernel/src/runtime_lab.rs` 被 `authority_state.rs` 使用，不能因名称删除。
Rust eval/bench/lab 可执行程序、`kernel/tests`、`fixtures` 和
`tools/qualification/runtime_lab/shadow_probe.rs` 属于 QUALIFICATION。
Monitor Swift Sources/Helper launcher 为 ACTIVE_RUNTIME，Swift Tests 为 QUALIFICATION，
App 打包脚本为 DEVOPS；systemd 文件是部署模板，不等于已部署实例。

## 验证结果

| 检查 | 结果与边界 |
| --- | --- |
| Rust release 构建 | PASS：fresh `clinx-runtime`、`clinx-mcp-relay`，locked/offline，任务独立 target |
| Rust lib / integration | PASS：21 个 lib；25 个 integration（business 3、native_routing 19、workspace_observer 1、authority_store 2） |
| Relay + Rust Node | PASS：14 项；包括二进制字节保真、半关闭、无 newline 响应立即刷新、对端关闭时退出、端点不可用不重放 |
| Rust 复制源码包 | PASS：全部 profile imports、MCP catalog、36 个 request/routing 测试；无 COMPAT 加载 |
| 真实 Rust 进程链 | PASS：临时状态的 prepare/start/status、脚本 Provider dispatch/completion、幂等、queued cancel、丢 ACK 后 fixture SIGKILL/reopen 精确读取；2 次 input、无 replay |
| 无 Owner 兼容链 | PASS：7 项本地恢复/outbox 检查，临时 DB/Provider |
| 发布清单及残留 | PASS：完整 control 刷新、旧包移除、native 依赖保留、所有 manifest hashes、base drift 拒绝；历史 builder 拒绝当前布局 |
| 入口/源码结构 | PASS：根目录 0 `.py`、125 模块声明完整、限定内部导入、Rust profile 闭包、7 个 shell 语法检查、5 个 CLI help 入口及维护程序嵌入 Python 引用 |
| Mac Helper 包 | PASS：Air 与 iMac Python 3.13.3 加载相同复制源码包，临时 SQLite、无 Owner、metadata outbox 幂等、直接文件入口；已安装 App 修改数 0 |
| Monitor Swift | PASS：Air 干净 `swift build`、15 项相关 `swift test`；存在已有 concurrency/deprecation warning |
| 格式/静态检查 | PASS：diff whitespace、relay rustfmt、F821；保留 7 个原有 F401，未宣称全仓库 lint 零告警 |
| 完整签名 App / GUI / 实际远端业务 | 未验证：未生成签名发布 App、未安装、未触发真实任务；源码包与 Swift 构建不代替这些验收 |

Authority binary SHA-256：
`5a687d5b6e0e6525a587c999a81bdcb2d34d59a970c4051469c0eaff779a42cc`。
最终 relay SHA-256：`c3d76c4881f4b5656f046adba9072776f5d8b158d43c926365dfcdfcd003ac4c`。
两台 Mac 验证的源码包 SHA-256：
`fa13b3b65f732a8e09adc8fb0e358537dbead25dc93af31c339f2b6e9e5f4df1`。

## 保留测试与既有失败

124 个原 local test/support 文件 SHA-256 不变。新 qualification runner 只在外部任务目录
重定位历史 import、patch target 和 Path 引用，原断言、skip、文件保留。复制转换记录原/新
hash；无 runtime alias。详见[测试说明](../TESTING.md)。

- 相同 18 个模块：前后均 **66 failed / 386 passed / 2 skipped / 73 subtests passed**。
  439 个唯一 JUnit 节点身份/结果逐项一致，新增失败 0；这不是完整测试 PASS。
- 全量收集：前后均 **1,491 collected / 24 errors**，24 个错误模块身份完全相同，均保留。
- 中间转换曾误改数据字符串和 reexport patch target，造成临时资格失败；修复转换映射后
  重跑得到上述最终结果，原始日志保留。没有通过删断言、skip 或改生产语义抹平失败。
- 历史 `qualify_runtime_shadow.py`、`qualify_runtime_migration.py`、`qualify_rust_runtime.py`
  仍依赖缺失的 `test_rust_runtime_lab.py`、原 source hashes 与 prior receipts，标为
  QUALIFICATION/BLOCKED。其基线先决条件没有本轮补造，也不进入生产包。

## 尚未迁移的运行时阻塞

1. `clinx/compat/bridge.py` 5,556 行、`task_registry.py` 4,052 行、`m9_integration.py`
   2,786 行，合计 **12,394 行**。它们仍服务 Mac 节点本地执行；上轮已删除的
   `execution_owner.py` 未恢复。本轮没有可据以删除这三者的等价 Rust 远端路径。
2. Rust Node 对 canonical handoff 明确返回 `RUST_NATIVE_ADOPTION_NOT_IMPLEMENTED`。
   Dispatcher 绑定本机 Desktop endpoint，当前 IPC 使用 Linux Unix socket/peer credentials。
   缺少 Mac→Rust 权威的等价 handoff/Provider 证据提交契约，不能靠更改默认模式完成迁移。
3. `completion_runtime`、`result_ingestion`、`prepared_requests`、`native_input_recovery`、
   `execution_liveness`、`unstarted_recovery` 与 Registry 事务/lease 相连。
   远端 completion/cancel、ACK 不确定及恢复读取必须保持现有身份、幂等和并发语义后才能删除。
4. `compat/domain` 和 `compat/shadow_ledger` 只保留 metadata publication/outbox 所需部分；
   仍有实际调用，不能将持久化 outbox 简化成节点自报成功。
5. `clinx/mcp`、`provider`、`host`、`transport`、`nodes`、`discovery` 的 ACTIVE_RUNTIME
   都仍是 Python 运行实现，长期必须继续迁 Rust。本轮只替换 relay，没有声称这些已 Rust 化。

## 生产与证据边界

只读核对了五个现役 systemd 服务：Authority、Dispatcher、Centre、Observer、Tunnel。
本轮前后 PID、NRestarts、启动时间、ExecStart 一致；安装 manifest 仍是旧平铺布局，
源码集成没有改变其 target。Air/iMac 已安装 Helper 也仍为旧平铺入口，读取仅记录
文件 hash 和配置键，未输出凭据；是否正在执行旧业务不能仅由安装文件或配置存在推断。

P620 原始证据目录：`/lab/runtime-worktrees/p620/clinx-convergence5-20261024T1350Z`。
保留 baseline/final XML、逐节点 delta、源码/测试 hash、Mac receipts、构建日志、产物
manifest、结构引用图和入口检查；可再生 target、venv、临时包和测试状态按 ownership 清理。
本地测试压缩保全仍不进入 Git。提交/远端回读另保存在同目录 `git-final.json`。
