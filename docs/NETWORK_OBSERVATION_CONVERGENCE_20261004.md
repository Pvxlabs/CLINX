# CLINX Air 修复与 Network Observation v1 源码收敛（2026-10-04）

## Swift 时间测试 remediation 与重新冻结（2026-10-04）

本节取代下文截至旧冻结提交的状态叙述；下文的来源、集成与历史回归证据保留为历史记录。此次仅修改 Swift 测试 fixture，未修改生产 24h 窗口、产品筛选语义、Network Observation 协议或 UI。

- OLD_FROZEN_SHA=5a3d8731a60208cd802fb7ba22789a832aaace21
- PRODUCT_FIX_SHA=1805b45b55548ffad186ac5715d97464700ea15b
- FINAL_FROZEN_SHA=以本次最终执行结果中的本地 HEAD 与 canonical origin 精确回读 SHA 为准；metadata commit 无法在自身内容中记录自身 SHA。
- SWIFT_TESTS_ON_P620=NOT_RUN（P620 无 swift/xcodebuild）
- IMAC_SWIFT_REQUALIFICATION_REQUIRED=YES（在 iMac 对 FINAL_FROZEN_SHA 重新运行精确 77 项 Swift tests）
- DEPLOYMENT=NOT_RUN
- FINAL_STATUS=IN_REVIEW（源码重新冻结完成后仍待 iMac Swift 重新资格验证）

iMac 已发现的失败发生在 `MonitorStoreTests.testNewExecutionRemainsDistinctFromHistoricalExecutionForSameTask` 的 Completed 历史快照：固定 `2026-10-03T12:20:00Z` 超出默认 24h 过滤窗口，故 `counts[.completed]` 为 0；独立的 execution identity 断言仍通过。相同固定时间模式还存在于相邻的重叠分页和历史选择测试。remediation 在 `MonitorTests.swift` 内用单个 `fixtureNow` 生成相对时间，保留 16 秒和约 8 分钟的原始排序间隔，并保留所有原有计数、identity 与历史选择断言。产品 `TimeWindow.day=1440` 分钟及 `minutes <= timeWindow.minutes` 未改变。相对 OLD_FROZEN_SHA 的产品修复 commit 仅改动该测试文件；正常 non-force push 已成功。

P620 验证：跨平台 `test_network_observation_swift_contract.py` 为 **1 passed**；Swift fixture/24h 源码静态检查及 `git diff --check` 为 **PASS**。扩展的 Network Observation Python 相关回归在隔离依赖环境中得到 **36 passed, 1 failed**：`test_network_observation_process.py::test_real_process_collection_tls_centre_observer_mcp_two_clients_and_recovery` 的子进程把 `PYTHONPATH` 重置为仓库根目录，未继承隔离安装的 `zeroconf`，启动前报 `ModuleNotFoundError`。该独立进程资格项保留为 **FAIL**，不能记为 PASS；测试依赖尝试产生的临时文件已移除。P620 Swift build/test 仍为 NOT_RUN，iMac 精确 Swift 重新资格验证仍为必需。

最终冻结提交仅更新本报告和 `.validation/network-observation-convergence/FROZEN_CANDIDATE.json`；正常 non-force push、fetch、远端 SHA 等于本地 HEAD、ahead/behind=0/0 和干净工作树以最终执行结果为准。不激活 P620 runtime，不执行 Air/iMac install。


## 冻结边界

唯一冻结分支：`codex/network-observation-convergence-20261004`。产品集成 HEAD `f1f096bb970651f5b60a067f7d8eaaecb9fa7229` 已经由首次正式 non-force push 创建到 canonical `origin` 的同名分支；本轮开始时本地与 origin 回读均为该 SHA，工作树干净。Host delivery 已恢复，受管 Host 读写操作返回已知结果。本次仅提交本报告与 `.validation/network-observation-convergence/FROZEN_CANDIDATE.json`，形成最终 metadata-only 冻结提交。

最终冻结 SHA 是本次 metadata-only 提交完成后的本地 HEAD，必须由正常 non-force push 到 canonical origin 同名分支，fetch 回读相同 SHA，ahead/behind 为 0/0，工作树干净。提交不能在自身内容中记录自己的 SHA；执行结果中的 `FINAL_FROZEN_SHA` 与 `REMOTE_SHA` 是精确值。Air 后续只能 fetch 该分支并核对这个最终 SHA；禁止从 `codex/air-result-contract-fix-20261004`、`codex/air-resume-fix-20261004` 或 `codex/p620-network-observation-v1-20261004` 构建最终 App。本轮部署与 Air build 均为 `NOT_RUN`。

## 来源与提交图

- Air 已知基线：`6a6ddd18ed5f2e1cb35c399e214c282affabcf69`。`origin/codex/air-resume-fix-20261004` 指向此提交；本轮 fetch 后 origin 未提供用户所述 `codex/air-result-contract-fix-20261004` 分支，故以给定 commit 与补丁 SHA 校验实际内容。
- Network Observation 交付：`3c5d73a5b45b0e60db533a78e07692612bc1882d`；冻结产品源码 commit：`1113952c8397ee97f1501f88b291cb6f0f921960`。Air 与交付的 `merge-base` 为 `6a6ddd18ed5f2e1cb35c399e214c282affabcf69`，它是直接祖先。
- Air 契约提交：`9c0bbf1`，同样是交付直接祖先。`git show --format= --binary 9c0bbf1` 的 SHA256 为 `2418fe9a95774a65b038ed85ecd9b8cc697ab477309b862ef95c69f3d7362501`，与给定补丁完全相同；stable patch-id 为 `ba43359f73c0fb227e0239ad55b20fba6c9802c9`。未重复 cherry-pick。
- terminal-state/Monitor 修复 `3d99f2bc4752df3d08df1c8b6fd8f1373eb80ba8` 是集成候选祖先，经 `14e13c0` 集成。未重复 cherry-pick。
- `1113952..3c5d73a` 仅新增/修改文档；原交付 manifest 的 30 个产品文件 SHA256 在新 worktree 中逐一匹配。原 `DELIVERY_MANIFEST.json` SHA256：`59a6f7be9f96d3616ad6c5ef10b01761a12369fb76fdb2040d5ba5bfaf37aa29`；其中 `source_files_sha256` 规范 JSON 的 SHA256：`3d753bf5162f8b9f3abc027df1834d022098cca123870051fed372767f726c1e`。

## 逐文件与契约核对

| 文件/路径 | 最终语义和未回退证据 |
| --- | --- |
| `bridge.py` | `9c0bbf1` 与候选的 Git blob 均为 `853247ae1bcb856e34659ca53bfbc7b1c6d441c2`。`_local_socket_command` 对 `--sock` 两种写法去重、路径归一化并拒绝冲突；`_managed_result_instructions` 同时进入 developer instructions 与 native turn prompt，已加载 thread 的 resume 仍收到结果契约。 |
| `node_protocol.py` | `9c0bbf1..候选` 的差异只有 `SharingScope.projects`、授权读操作和 observation 项目存储/读取；`READ_OPERATIONS`、`NODE_READ_UNAVAILABLE`、`NODE_READ_FAILED` 原语句逐项相同。只读 RPC 的 transport、dispatch、serialization 错误保留 `side_effect=NONE`，写操作仍是 `UNKNOWN/RECONCILIATION_REQUIRED`。 |
| `test_air_result_contract.py` | `9c0bbf1` 原 287 行均保留，后续只新增归档原子性回归。结果契约、socket 冲突、RPC 分类和精确 thread 续接回归随集成候选运行。 |
| `task_registry.py` | `release_execution` 用 `_shadow_write_connection(atomic=True)` 包住 history INSERT、active DELETE 和 lease 释放，即使 shadow ledger 未启用也保持单事务；测试在 DELETE 前从另一个只读连接只能见到 `(active=1, history=0)`，提交后见到 `(0,1)`。 |
| `network_observation.py` / `observation_source.py` / `node_runtime.py` | Observation Directory、原生源采集、持久 outbox/checkpoint、项目授权、离线 Activity 与 canonical owner 区分均保留；30 文件产品清单逐字节匹配。 |
| `observer_server.py` / `observation_mcp.py` / `mcp_server.py` | `/v2/observations` 与四个 `clinx_*observation*` MCP 入口共用目录；保留 v1 Observer 和只读边界。 |
| `MonitorApp` | `ActivityStore` 以精确 `kind=result` 停止终态 tail polling，保留 execution_ref 隔离；Network Observation 的 Models、Store、View、ObserverClient、正式 Root 导航同时存在。P620 无 Swift/Xcode 工具链，Swift build/test 为 `NOT_RUN`。 |

没有发生源码合并冲突，也没有使用整文件 `ours/theirs` 覆盖。集成分支从 Network Observation 交付 HEAD 原样分出；本轮仅增加本报告和本地验证证据。

## 新候选隔离验证

在独立 worktree `/home/pvxlabs/dev/clinx-network-observation-convergence-20261004`，以独立 HOME/XDG/CODEX_HOME、临时 DB、loopback fixture 执行；使用旧交付工作区内已建的 Python venv 与隔离测试 runner，但测试的 cwd 和导入源码均为**新集成候选**。

- 最小交集：用户指定 9 个文件，`201 passed, 13 subtests passed in 49.43s`；日志 `minimal-intersection.log` SHA256 `7963c4ee69caa7b594a1fe516166de06edba407187c5ed00c463ebfbce8d9895`。
- 完整相关资格：原 Network Observation 报告列出的 16 个测试文件，**本轮重跑** `289 passed, 28 subtests passed in 61.94s`；日志 `full-qualification.log` SHA256 `2f86fed7ae38c733c4fe8fd6d7a191d96e3009090d028dc6187594026ddc03d5`。旧交付的 289 passed 不计作本轮验证。
- 15 个相关 Python 源文件 AST 解析、`MonitorApp/Scripts/build-app.sh` 与两个 launcher 的 `bash -n`、`git diff --check 6a6ddd1..HEAD` 均通过。最终 metadata-only 提交后的 diff、工作区状态和 origin 精确读回以本轮执行结果为准。
- `swift`、`xcodebuild` 在 P620 均不可用；Swift build/test、Air App 安装、部署与真实会话 `01a105a5-8667-79e3-afdb-6e6db6a97f65` 自动发现均 `NOT_RUN`。

## 推送与最终冻结

早前受管 Host 的开发命令推送曾在 dispatch 前被拒绝，原失败证据保持为历史记录。随后首次正式注册操作以正常 non-force push 成功创建 `origin/codex/network-observation-convergence-20261004`；本轮 fetch 读回该分支与本地产品集成 HEAD 同为 `f1f096bb970651f5b60a067f7d8eaaecb9fa7229`。Host delivery 已恢复，本轮注册操作结果可继续执行。既有隔离集成回归仍为 `201 passed, 13 subtests` 与 `289 passed, 28 subtests`，本轮不重跑。

本次 metadata-only 提交正常推送后，以最终本地 HEAD 和 canonical origin 同名分支 fetch 回读完全相等、ahead/behind `0/0`、工作树干净作为 `FROZEN_CANDIDATE=PASS` 的必要条件。若任何条件不满足，最终结果为 `BLOCKED`。最终 SHA 由本轮执行结果提供；产品集成 HEAD 仅是其祖先证据。禁止 force push、main merge、自定义 refspec、产品源码修改、部署和 Air build。

## 下一阶段顺序

冻结候选 → Air fetch 并校验精确 ref/HEAD → Swift build/test → 安装候选 App → P620/Air 受控激活 → 用真实 `01a105a5-8667-79e3-afdb-6e6db6a97f65` 验证自动发现 → Air/P620 双机同屏 → UI 验收 → 最后处理 iMac。本轮在 metadata-only 冻结提交、正常推送及精确远端读回后收口；Air build 与部署留待后续独立执行。
