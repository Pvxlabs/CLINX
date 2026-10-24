# Rust 迁移清理第四轮：Python Owner 与 shadow 业务模型退役

日期：2026-10-24。基准：`f61bf531f0f15d5ca82c907a1238a98a13b28567`，当前 `main`，
开始时工作区干净。本轮完成可证明安全的源码清理；**剩余 Mac 旧核心迁移仍为 BLOCKED**。
没有部署、重启现役服务、改已安装 App、操作现役任务或写生产数据库。

## 实际删除与规模

整文件删除 **14 个 / 1,636 行**；加上文件内旧实现，本轮 Python diff 删除 **3,482 行**、
增加 **316 行**，净减少 **3,166 行**。增加部分包括 10 项 Rust 节点边界回归 fixture。
统计使用受控源码物理行，包含空行和注释；不把本地忽略的测试、生成物或移动文件算作删除。

| 范围 | 清理前文件 / 行 | 清理后文件 / 行 |
| --- | ---: | ---: |
| Python | 117 / 41,916 | 104 / 38,750 |
| 根目录 Python | 60 / 31,785 | 59 / 30,227 |
| Rust | 20 / 9,416 | 20 / 9,416 |
| Swift | 62 / 11,355 | 62 / 11,355 |
| Rust 包 Python | 47 / 16,633 | 47 / 16,470 |
| Monitor 包 Python | 75 / 34,453 | 63 / 31,280 |

重点四文件从 **13,409 行降至 12,379 行**：

| 文件 | 清理前 | 清理后 | 状态 |
| --- | ---: | ---: | --- |
| `bridge.py` | 5,556 | 5,549 | COMPAT，Mac TaskDispatcher/读取/维护 CLI 仍使用 |
| `task_registry.py` | 4,486 | 4,048 | COMPAT，移除 shadow 双写，Mac 本地状态路径仍使用 |
| `m9_integration.py` | 2,804 | 2,782 | COMPAT，移除 Owner RPC，Mac prepare/start/status/cancel 仍使用 |
| `execution_owner.py` | 563 | 0 | LEGACY，整文件删除 |

其他整文件删除：`domain/` 的 attempt、compatibility、execution、project、projection、
provider_session、resource_allocation、runtime_worker、task、workspace 共 10 个模块；
`shadow_ledger/replay.py`、`shadow_ledger/__main__.py`；旧 Owner 专用的
`fixtures/tunnel_reliability_mcp.py`。

## 入口、状态与发布包

- MCP factory 仅构造 RustIntegration，删除旧 Registry/Bridge/M9/Owner fallback、
  Python completion 启动、旧 `--execution-owner`、`--consultations-only --task-db` 入口。
  非 Rust 模式明确失败，不回退到 Python writer。公共 Rust 工具契约未变。
  已安装不可变 caller unit 仍可能包含 `--no-recover-existing`，所以继续接受这个隐藏无效参数；
  新打包模板不再生成它。
- 删除 Python Owner 服务、客户端和写转发分支。只读兼容消费者尝试写时明确拒绝；
  Mac 原本的本地直连路径保留原执行顺序。Owner 历史记录的纯读投影移入
  `native_input_recovery.py`，不会新建 Owner 表或认领任务。
- 删除 NativeTaskLifecycle 自动入库类、Owner lifecycle 客户端、Centre
  `--canonical-lifecycle-config` / `reconcile-native` 入口。
  Mac 原有确定性身份计算、binding 和 `qualify_handoff` 保留。
- 删除 Registry 的实验 shadow 初始化、baseline 导入和双写；原 `atomic=True` 事务边界保持。
  `domain/` 仅留事件 JSON 模型，`shadow_ledger/` 仅留 Desktop metadata outbox 存储。
  保留旧 schema、fingerprint、事件与 outbox ID、幂等和重试次数；没有迁移或删生产数据。
- Rust node status/cancel 校验完整 host/project/thread/turn selector；历史 execution 按自身
  receipt 读取，不拿当前 Task 的 successor thread 替代。只有 Rust 确认 CANCELLED + RELEASED
  才报告取消完成。增加与 Helper 生命周期一致的无副作用 `close()`。
- 两台 Mac 只读 SQLite 检查都没有 `clinx_execution_owners` 表，也没有 `v2_` 表。
  iMac：executions 0、execution_history 1、native_task_bindings 1；
  Air：executions 1、execution_history 3、无 native_task_bindings 表。
  **记录数不证明执行活性**，本轮没有修改或恢复这些记录。
- 无 Owner 表的本地丢 ACK 恢复继续核对精确 execution、prepared seal、binding、lease、
  route、policy、Host-effect 及 Provider input 证据。有历史 Owner 表时仍要求精确 Owner 行；
  缺行不回退为无 Owner 模式。恢复通过读回提交，不 replay 输入。
- `runtime_packaging.py` 从 Monitor profile 删除 Owner；删除的 domain/replay 不再进入包。
  增量构建清理旧复制文件；Rust release 从显式清单完整刷新，不继承旧源码残留。
  `MonitorApp/Scripts/build-app.sh` 已通过该共用清单打包，本轮无需另写重复清单。

全仓 Python AST 导入、Rust module、Swift target/符号、脚本/服务文件路径引用与包归属见
[第四轮引用清单](rust-migration-inventory-phase4.json)。分类为 ACTIVE、COMPAT、TEST、
LEGACY、UNUSED；静态引用是保守依赖，不能证明每个分支正在生产执行。

## 保留的 Python 与明确阻塞

| 模块组 | 实际用途 / 保留原因 |
| --- | --- |
| `rust_*`、`mcp_server.py`、runtime config/contracts | Rust IPC、MCP 协议、请求、状态读取、Host 和投递适配；不导入旧业务核心 |
| `app_server.py`、`native_provider.py`、native history/writer/desktop socket | Codex 协议、已有 Desktop daemon、原生历史与精确 input 读取 |
| `host_executor.py`、host contract、tool delivery | Host 子进程执行和既有工具投递协议 |
| `local_discovery/`、node protocol/runtime、observation/observer、Monitor Helper | 设备发现、配对、节点路由、监控投影及 macOS 用户态 Helper |
| Bridge / Registry / M9 / node_execution_adapter | **尚未迁移的旧业务核心**。Mac `CanonicalNodeExecutionAdapter.from_config` 非 Rust 路径仍构造并调用它们 |
| `completion_runtime.py`、`result_ingestion.py` | Mac 直接执行的完成登记、精确终结和结果写回；节点尚未迁移，不能直接删除 |
| `native_input_recovery.py`、`unstarted_recovery.py`、prepared/historical/policy helpers | Mac 丢 ACK 精确读回及既有历史维护，不应伪造完成或盲目重发 |
| `domain/` 3 文件、`shadow_ledger/` 5 文件 | Desktop metadata outbox 的兼容序列化和旧存储格式；任务状态模型、双写、replay 已删除。schema 中历史表布局保留，避免暗中迁移数据库 |
| `consultations.py` | 普通共享 schema 与既有本地 SQLite 兼容调用；旧 Owner 专用 stdio 入口已删除，不属于 Rust 任务状态权威 |

整包删除前三个核心仍被以下具体差距阻塞：Rust node adapter 尚不支持 canonical handoff；
Rust Dispatcher 绑定本地 Desktop endpoint；Rust IPC 使用 Linux Unix socket 与 peer credentials。
Mac 现有身份、远端 Provider 证据、完成/取消/丢 ACK 恢复还没有同等 Rust 调用链。
将模式默认改为 Rust、把节点自报当作可信 ACK、禁用节点执行或改任务身份，都不能算完成迁移。
本轮没有新增远端 evidence transport 或 Rust handoff command，也没有声称这些能力已完成。

## 验证结果

| 检查 | 结果与边界 |
| --- | --- |
| fresh Rust release build | **PASS**；独立 Cargo target；binary SHA `56070e5b96f35e8951d34e80f5aec430fb79e423b17043177dfeacc373caa68c` |
| Rust Core / Runtime 测试 | **PASS（复用）**；21 + 25 项前轮结果，全部 Rust 源码 SHA 相同；本轮重新编译并验证下面的实际 IPC 链 |
| 复制的 Rust 包 | **PASS**；全部清单模块导入、MCP catalog、36 项原请求/原生路由测试；没有旧核心导入 |
| 实际产物调用链 | **PASS**；临时 Rust 进程 + scripted Provider，prepare/start/status、派发/完成、同请求幂等、queued cancel、丢 ACK + fixture SIGKILL/reopen + 精确读回；原生输入只发两次，无 replay |
| Rust node fixture | **PASS，10 项**；selector 冲突在取消前拒绝、历史 thread、取消终态、Helper close、MCP 禁止 fallback |
| 无 Owner 本地恢复 | **PASS，7 项**；P620 原本地 fixture + 临时 DB；成功恢复及 lease/binding/prepared/turn/Owner 缺行/input 六类冲突，无 replay |
| 旧 outbox 升级 | **PASS**；从基准源码生成 pending/retry/applied，再由新代码重复 enqueue 和续投；ID/fingerprint/历史证据不变 |
| 增量打包与 release manifest | **PASS**；旧文件清除、原生依赖哈希、基包 drift 拒绝；仅 task-owned `STAGED_NOT_SELECTED` 产物 |
| iMac / Air Python bundle | **PASS**；已安装 Python 3.13.3 加载临时精简包、临时 SQLite/outbox、退役模块缺席；临时目录清除、App 修改 0 |
| Monitor Swift | **PASS（复用）**；全部 Swift SHA 相同，复用前轮 Air Swift 6.4 build 与 15 项测试；本轮没有重新签名/安装 App |
| 18 个保留 Python 测试模块 | **FAIL，原失败保留**；386 PASS / 66 FAIL / 2 SKIP / 73 subtests PASS；439 个同名 JUnit 节点与基线结果全部相同 |
| 全本地测试 collection | **BLOCKED**；1,491 collected + 24 errors；比前轮新增 15 个退役接口 import errors，原 tests 未删改或加 skip |
| 语法 / 引用 / F821,F401 / diff | **PASS**；受控 Python AST、修改文件未定义名/未使用导入、shell 语法和 diff 空白检查 |
| 真实 Provider、现役任务、完整签名 App/GUI 与 Mac→Rust 业务验收 | **未验证**；没有用生产副作用获取 PASS |

基线选择了 66 FAIL / 490 PASS / 2 SKIP / 81 subtests PASS 的本地 suite。
删除 Owner/lifecycle 后，保留模块对比明确排除了顶部导入退役实现的
`test_execution_owner.py`、`test_native_input_recovery.py`、`test_native_lifecycle.py`；
shadow 实验测试也不是当前业务 gate。移出的基线节点共 104 个，均保存于对照 JSON。
本轮新增 collection 错误文件：archived_native_failure、bridge_first_migration、
consultation_owner、consultations、domain、execution_owner、native_input_recovery、
native_lifecycle、owner_callers、prepared_status、shadow_ledger、tunnel_reliability_wire、
tunnel_stage1_wire、tunnel_transport_runtime、unstarted_model_recovery。
它们包括仍有价值但顶部导入旧模块的测试；不能把无法 collect 当作已通过。

## 运行身份与证据

systemd 读回的 Authority/Dispatcher/Centre PID 分别为 `1023787` / `1024186` / `1024191`，
NRestarts 分别为 `0` / `1` / `1`，与前轮一致；各进程仍运行 09:56 UTC 的已安装 release。
Authority 为 `rust-73ad2dc733578a1e8947`，Dispatcher 为 `rust-c09215686ef963f9067f`，
Centre 为 `rust-6c0ba78d0ba4d7960131`，Observer 为 `rust-b2169586d3fb0d74e810`。
四份 manifest 均已只读核对并保存 SHA；源码变化不等于这些产物已更新。

证据目录：`/lab/runtime-worktrees/p620/clinx-cleanup4-20261024T1253Z`。
保留原测试 124 个文件的 SHA/压缩快照、前后 XML、所有失败日志、完整引用清单生成器、
产物/设备验证脚本、包哈希、运行读回及 Git receipt。任务结束清除专属可再生 venv、
Cargo target、复制源码/包与临时状态；Git fixture 位于任务专属
`/data/artifacts/clinx-cleanup4-20261024T1253Z`，不保留额外 checkout。
