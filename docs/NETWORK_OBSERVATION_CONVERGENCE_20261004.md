# CLINX Air 修复与 Network Observation v1 源码收敛（2026-10-04）

## 冻结边界

唯一集成分支：`codex/network-observation-convergence-20261004`。本地精确提交见本工作区 `.validation/network-observation-convergence/FROZEN_CANDIDATE.json` 的 `integration_head`；远端推送与读回尚未完成，因此该提交是唯一**待冻结**候选，不能进入 Air 构建。后续 Air 必须 fetch `origin` 的该分支并核对精确提交；禁止从 `codex/air-result-contract-fix-20261004`、`codex/air-resume-fix-20261004` 或 `codex/p620-network-observation-v1-20261004` 构建最终 App。本轮仅冻结源码候选；部署、运行服务切换、App 安装、真实 Air/P620 观察和 iMac 均为 `NOT_RUN`。

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
- 15 个相关 Python 源文件 AST 解析、`MonitorApp/Scripts/build-app.sh` 与两个 launcher 的 `bash -n`、`git diff --check 6a6ddd1..HEAD` 均通过。最终提交后的 diff、工作区状态与 origin 分支存在性见本地 manifest；远端精确读回 `NOT_RUN`。
- `swift`、`xcodebuild` 在 P620 均不可用；Swift build/test、Air App 安装、部署与真实会话 `01a105a5-8667-79e3-afdb-6e6db6a97f65` 自动发现均 `NOT_RUN`。

## 推送权限阻塞

受管 Host 对新 worktree 的 `git push -u origin codex/network-observation-convergence-20261004` 返回 `AUTHORITY_DENIED` / `COMMAND_NOT_DISPATCHED`，原因是 `development_command git network operations require explicit external authority`。`host_dispatched=false`、`side_effect_certainty=NOT_EXECUTED`，因此没有远端写入或不确定的推送结果。当前 `push_current_branch` 只针对注册的原产品 worktree/分支，不能代表集成分支执行推送。`PUSH=BLOCKED`、`FROZEN_CANDIDATE=BLOCKED`、本轮 convergence `FINAL_STATUS=BLOCKED`；这不改变上述源码和隔离回归的 PASS。需要外层以该**同一个本地提交**授予正确的远端推送操作，再做精确远端读回；不得重跑本次被拒绝的原操作，也不得从旧分支构建 App。

## 下一阶段顺序

冻结候选 → Air fetch 并校验精确 ref/HEAD → Swift build/test → 安装候选 App → P620/Air 受控激活 → 用真实 `01a105a5-8667-79e3-afdb-6e6db6a97f65` 验证自动发现 → Air/P620 双机同屏 → UI 验收 → 最后处理 iMac。本轮在源码收敛及权限阻塞核实后停止；远端推送和读回完成前不进入下一阶段。
