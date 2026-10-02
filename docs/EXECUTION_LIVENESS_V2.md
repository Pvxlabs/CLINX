# CLINX Execution Liveness Reconciliation v2

STATUS: PASS（本地修复及定向验收）；全仓回归存在已确认的 5 项 Monitor UI 基线失败。公共控制面未激活。

ROOT_CAUSE: 旧 reconcile 在 transport exception 后复用 task.codex_running；部分 byte-channel 断连直接触发 terminal finalizer；endpoint 选择把 non-owner 的状态和读取失败混入 owner 判断；RECOVERY_REQUIRED 被多个 lease 清理入口视为 terminal。last_progress_at 同时承担不同活动含义，使停止推进的 execution 仍表现为运行中。

WORKTREE: /home/pvxlabs/dev/CLINX-worktrees/execution-liveness-v2

BRANCH: codex/execution-liveness-v2

HEAD: 本报告所在本地提交，以最终 CLINX_EXECUTION_RESULT 的 HEAD SHA 为准。基线为 canonical 已提交 HEAD 38631af027daf27f30af2a58204781d989a99aad；未纳入 canonical 的既有 dirty。

IMPLEMENTATION: 新增 execution_liveness.py，集中状态分类、120 秒宽限期、exact execution 证据、独立时钟及只读投影。native_provider.observe_execution 对所有 configured endpoints 做只读观察，每 endpoint 最多五页、每页 20 turns；不 resume、不订阅、不启动 provider。bridge 的 reconcile 和 native cancellation 使用 exact turn 路由，并在 terminal finalizer 前再次检查所有 endpoints。TaskRegistry 不再允许状态名或调用方旧 boolean 单独设置 running。CompletionRuntime、get_status 及 MCP schema 同步支持这些语义。Local Discovery、PAKE、ORION、Monitor UI 产品代码均未修改。

STATE_MODEL: 保留既有字段和值兼容：CODEX_RUNNING 表示 RUNNING；TRANSPORT_UNCERTAIN 表示 UNKNOWN_LIVENESS。生命周期与 liveness/transport 分开存储。唯一 exact active owner -> CODEX_RUNNING；owner exact terminal 且释放检查通过 -> 现有 finalizer；未知且在 grace 内 -> TRANSPORT_UNCERTAIN、codex_running=false；未知、无 provider/Host/live-owner 新活动、transport 仍退化且超过 grace -> RECOVERY_REQUIRED、codex_running=false。ownership conflict 保持未知并保留 lease。既有 cancellation intent 保持 CANCELLATION_PENDING，仍不构成终结或释放许可。

PROVIDER_LIVENESS: LIVE / TERMINAL / UNKNOWN。LIVE 必须来自 exact thread + exact turn 的唯一 active owner；其他 endpoint 的 interrupted/completed/notLoaded 不能推翻它。两个 active thread claims 构成冲突，哪怕其中一个 turn read 失败。未识别状态、exact turn 缺失、仅历史 completed 都不构成 live 或 terminal authority。status 对超过 120 秒未刷新的 LIVE 证据显示 UNKNOWN/false，不沿用 task boolean。

TRANSPORT_HEALTH: HEALTHY / DEGRADED / UNAVAILABLE。全部 configured observations 可读取为 HEALTHY；部分不可读为 DEGRADED；全部不可读为 UNAVAILABLE。malformed JSON、socket/connection error 只影响 transport/可观测性，不直接 terminalize。另一个 exact live owner 仍可使 execution 保持运行中。

TIMESTAMP_MODEL: last_provider_activity_at 来自 owner exact turn status 观察，或已注册 exact completion notification；notification 仅表示活动并触发 reconcile，不赋予终结权。last_live_owner_at 只由唯一 exact active owner 更新。last_host_delivery_at 实时读取既有 host_tool_deliveries 中 delivery_state=DELIVERED 且存在 host_execution_ref 的 MAX(acknowledged_at)，不把 Host execution 完成、response sent、失败 ACK 或预派发拒绝当成功 Host delivery。status 以 UTC ISO-8601 返回，未有证据时为 null。unknown_since 为首次未知观察时间，grace 从它及三个活动时钟中的最新值计算；未知轮询本身不会延长 grace。last_progress_at 保留原有生命周期进度语义。阈值只定义于 LIVENESS_GRACE_SECONDS=120.0。

LEASE_SAFETY: RECOVERY_REQUIRED 不释放 lease，包含没有新 liveness 行的旧 execution。进入 recovery 会持久化重新观察要求，之后直接 finalizer、reconcile_terminal 或 cancellation 都不能绕过。已进入 liveness 管理的 execution 不由 startup/global sweep 释放；exact reconciler 必须再次读取所有 configured endpoints、确认无 active owner、无 ownership conflict、无不可确认 endpoint，并取得 owner exact terminal evidence。低层 release、terminal/cancellation 和 finalizer 写入前均有保护。保留原有 execution/task/turn/lease ownership 校验；新的 live owner 可将持有 lease 的 recovery/部分终结状态恢复为运行中。原有尚未进入 liveness reconciliation 的 exact finalizer 入口继续遵循已有所有权合同。

STATUS_API: task/execution selector 与 exact thread selector 均新增 provider_liveness、transport_health、codex_running、last_provider_activity_at、last_host_delivery_at、last_live_owner_at、liveness_reason；另有 ownership_conflict、liveness_observed_at、liveness_grace_seconds。保留 execution_state、CODEX_RUNNING 等旧字段并使上下两种 running 字段一致。读 status 不探测 provider、不迁移数据库、不 reconcile、不清 lease。MCP strict output schema 已补齐，并经实际 schema tests 验证。

COMPLETION_RUNTIME: process 先检查 authoritative，非 authoritative 保持 PENDING 并退避重试，不能因旧 terminal/result 行而标 DONE。recover 只重试 exact enrolled identity；部分 finalizer commit 且 lease 仍在时，重新观察 provider 后才完成释放。notify 校验 exact thread/turn，并独立记录 completion activity；不会直接 terminalize。transport exceptions 保留 handoff 和 lease。身份不匹配继续 HOLD，沿用原有隔离规则。

TEST_RESULTS: 最终定向命令如下，243 passed、24 subtests passed。新增 test_execution_liveness.py 共 20 项，直接调用真实 reconciler、TaskRegistry SQLite、CompletionRuntime、ExecutionFinalizer；provider 协议由确定性 fixtures 提供，不把 fixture 当实时部署证据。

```text
python3 -m pytest -q --tb=short test_execution_liveness.py test_execution_owner_routing.py test_native_interop.py test_m13.py test_pvx1812_completion.py test_m5.py test_m9.py test_m12.py test_thread_identity.py test_tool_delivery.py test_worker_continuation.py
```

| 必须场景 | 自动化结果 |
| --- | --- |
| 1 owner active / non-owner interrupted | PASS，CODEX_RUNNING、LIVE、true |
| 2 owner completed / non-owner notLoaded | PASS，COMPLETED，exact result，lease released |
| 3 malformed JSON / 另一个 exact active owner | PASS，运行中且 DEGRADED |
| 4 短时全部不可确认 | PASS，TRANSPORT_UNCERTAIN、UNKNOWN、false，lease held |
| 5 长时间无 owner/provider/Host 活动 | PASS，RECOVERY_REQUIRED、false，重启后 lease 仍在 |
| 6 recovery 后重现 active owner | PASS，恢复 CODEX_RUNNING，禁止 release |
| 7 两个 active owner claims | PASS，OWNERSHIP_CONFLICT、UNKNOWN、false |
| 8 历史 task.codex_running=true | PASS，status 不继承旧 true |
| 9 Host 成功 delivery / provider unknown | PASS，只更新 Host 时钟，仍不判 live |
| 10 exact owner terminal + 有效结果 | PASS，正常 finalizer、handoff DONE |

其他新增覆盖包括 terminal 前 owner 重现、不可读 active claim、later-page exact turn、旧 LIVE 到期、直接 finalizer/cancellation 绕过、legacy recovery barrier、notification 独立时钟和非 authoritative handoff。

仓库声明的 broader regression 为 python3 -m pytest -q；最终 804 passed、112 subtests passed、6 skipped、5 failed。六项跳过均为 opt-in live acceptance，未启用会创建/修改 execution 的测试。测试前在本 worktree 的 kernel 中用现有 /home/pvxlabs/.cargo/bin/cargo build --release --locked 构建所需二进制，PASS；第一次未找到 PATH 中 cargo，随后确认并使用已有绝对路径，未安装依赖。完整测试期间，仅临时将本 worktree bridge.toml 的 runtime DB/log 路径指向本 worktree 下 disposable 目录，finally 恢复原字节；没有修改 HOME、没有启动读取生产 DB 的测试 child。Python syntax、import-safe、git diff --check 均 PASS。

REAL_OBSERVATION: 2026-10-02 04:28:25 UTC，对 Local Discovery thread 01a0fab5-1096-7fb0-bce7-33fcfe617204 / turn 01a0fab5-134c-7162-8285-e1e2734031cb，只读查询 canonical DB 和两个 configured sockets。注册任务已 COMPLETED，两个 endpoint 都为 notLoaded/completed：该 thread 的历史 live/non-owner 分歧本轮 NOT_REPRODUCED，未修改或重启它以制造复现。

2026-10-02 04:30:24 UTC，补充只读观察本次 execution exec_36780362f2e34f949fc52d6f3cd742c0 / thread 01a0fad0-8f53-76e0-ade9-71eca96bb4f7 / turn 01a0fad0-91df-77b3-bcd5-efaf73dd4fa7：/run/user/1001/clinx-provider.sock 返回 active/inProgress；/home/pvxlabs/.codex/app-server-control/app-server-control.sock 返回 notLoaded/interrupted。新 classify 实际判定 LIVE、HEALTHY、owner=managed socket、release_safe=false。此项证明当前真实配置下的新 owner precedence；未把新代码安装到公共 daemon。

CANONICAL_WORKTREE_PRESERVED: PASS。/home/pvxlabs/dev/clinx 的 HEAD、git status porcelain 及 13 个既有 dirty 文件 SHA-256 全部与起始快照一致。未 reset/clean/stash/覆盖 dirty；代码、测试、commit 都在新 worktree。没有 push、merge、fast-forward 或 daemon restart。

LOCAL_DISCOVERY_PRESERVED: PASS。仅 mode=ro/query_only 的数据库查询和 read_only_observer 的 thread/read、thread/turns/list；没有 Local Discovery execution mutation，没有修改其 worktree、PAKE 或产品逻辑。

KNOWN_BASELINE_FAILURES: 下列 5 个 test_monitor_finalized_ui.py 测试在本分支与从基线 38631af 原始 git archive 提取的 MonitorApp/test 文件中同样失败。基线独立运行结果为 5 failed、13 passed；没有用 UI 改动掩盖失败。

- test_search_field_is_out_of_the_toolbar
- test_search_field_width_matches_the_compact_pages
- test_search_field_is_the_first_row_of_the_content_column
- test_main_content_bottom_inset_is_forty_in_every_pane
- test_synthetic_semantics_are_still_distinct

REMAINING_RISKS: 公共 daemon 尚未激活此提交，生产行为仍以当前运行版本为准。Local Discovery 指定历史 live 场景已结束，无法声称该 execution 的原位实时验收；真实 precedence 补充证据来自本次 execution。所有 endpoint 仅返回 unloaded terminal history 时，新层刻意维持 UNKNOWN，不凭历史结果释放 lease，需要后续 exact owner terminal evidence/既有授权恢复流程。观察为有界顺序采样，超出五页的 exact turn 或不可读 endpoint 会保持 fail-closed；宽限时钟采用持久化 UTC wall clock。全仓 5 项 UI 基线失败与这次修复分离。

NEXT_STEP: 审阅本地提交及证据。如后续要激活公共控制面，单独评估主 worktree dirty 与目标文件冲突，再执行受控激活及只读 acceptance；本轮不激活。

证据目录：docs/evidence/execution-liveness-v2/，包含 targeted.log、full-regression.log、baseline-ui.log、build.log、两份真实只读 observation JSON 和 preservation/syntax/import/diff-check 验证记录。
