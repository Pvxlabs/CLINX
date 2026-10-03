# CLINX Monitor 终态行与 Activity 滚动修复报告

## 结论

- `ROW_STATUS_ROOT_CAUSE=已确认（源码与精简消费回归设计；Swift/macOS 尚未执行）`：`MonitorStore` 在 `active + recent` 汇总后按 `taskRef` 保留首项，且列表 `ForEach` 也只用 `taskRef`。同一个 `taskRef` 的较旧 active/running 快照因此可以遮住 recent 中同 execution 的较新 completed 快照；不同 execution 也会被错误压成一行。
- `SCROLL_HANG_ROOT_CAUSE=未确认（静态确认反馈放大器候选；未取得 macOS 调用栈/Time Profiler）`：`ActivityScrollObserver` 对每个 `didLiveScrollNotification` 都回调，而 `ActivityView` 在消息、工具活动和结果变化时都异步 `scrollTo`。这能形成高频主线程状态/滚动反馈，但现场 CPU 100% 的最终热点仍可能是 AppKit 富文本选择或布局，当前 P620 证据不能定论。
- `FIX_STATUS=PARTIAL`：行身份/版本合并与 Activity 反馈抑制代码已完成；可执行 Python 回归通过；SwiftPM 和真实 macOS GUI 尚未验证。
- `FINAL_STATUS=PARTIAL`：没有匹配版本的 macOS App、采样和 60 秒交互证据，不能宣称用户现场 GUI 已恢复。

## 实现

1. `MonitorState.swift` 增加稳定 execution identity，按 `(taskRef, executionRef)` 合并；同一 execution 按 `timestamps.observedAt` 选择较新快照，同时间戳的已知 terminal 状态优先于 running。不同 execution 保留为不同 execution 行，不再互相覆盖。详情回写要求 task 和 execution 同时匹配，并复用同一合并规则。
2. `selectedExecutionRef` 进入选择、导航历史、键盘移动和 archive 取消路径；`ExecutionListView` 使用 execution identity 作为 SwiftUI 行 ID，避免 lazy row 复用旧 status/stage，也避免两个 execution 同时被标成选中。
3. `ActivityScrollObserver` 只发布 bottom/not-bottom 边沿变化；Activity 的 programmatic `scrollTo` 在同一 run-loop 合并，并在执行已手动上移时丢弃待执行跳底。保留文本选择、历史锚点和 Jump to latest。
4. `ActivityStore` 对 terminal execution 做最多三次 1 秒尾部同步，收到 `kind=result` 后停止轮询；非 terminal execution 仍按原有 2 秒/notice 退避节奏刷新。Activity 合并增加 1,000 条和 16 MB display budget，空 delta 不重建 transcript。

## 回归与证据

### 已执行

- `python3 -m unittest`（测试子进程设置隔离 `HOME`、`XDG_CONFIG_HOME`、`XDG_CACHE_HOME`，未修改 Agent 认证）：两组并发、最多 2 workers。
  - `test_observer_activity test_observer_history_guard test_monitor_finalized_ui test_native_completion_report test_execution_liveness test_pvx1812_completion`：`11 passed`。
  - `test_native_interop test_observer_schema test_observer_server test_result_ingestion test_tool_delivery`：`22 passed`。
- `python3 -m compileall -q .`：通过。
- `git diff --check`：通过。

### 已加入但未执行

- `MonitorTests.swift`：同 execution 的旧 running/新 completed 重叠合并；同 task 不同 execution 保持独立并分别计入 Active/Completed。
- `ActivityTests.swift`：scroll observer 重复通知去重；terminal result 到达后停止尾部轮询。
- P620 无 `swift` 和 `xcodebuild`，对应调用在预分发阶段返回 `CAPABILITY_UNAVAILABLE`，未伪造 SwiftPM 通过。

### 未覆盖或待 macOS 执行

- SwiftPM build/test、macOS App 启动、Activity 长中文报告/链接/代码段滚动、向上滚动与新消息交错、历史锚点、Jump to latest、resize 和连续 60 秒无卡死。
- Activity 主线程/富文本布局的实际 CPU、内存、交互延迟和现场采样；用户提供的 PID `83139` 未在 P620 操作。

## 安装身份与交付边界

- `INSTALLED_BUILD_IDENTITY=UNVERIFIED`。仓库源 `MonitorApp/Info.plist` 只读显示 `CFBundleShortVersionString=0.3.0`、`CFBundleVersion=3`；P620 无 macOS `/Applications` 访问入口，也没有本 worktree 的 `.app`，无法证明已安装 Monitor 与本提交一致。
- `MACOS_REPRO=未验证`。
- `MACOS_GUI_ACCEPTANCE=BLOCKED（缺少可达 Mac、匹配 App、采样和真实 GUI 回归）`。
- 本地 read plane 没有建立绑定、写 registry/lease/checkpoint、启动 Provider、发送 turn、修改原生历史或操作共享服务。

## 隔离与集成

- `WORKTREE=/home/pvxlabs/dev/clinx-terminal-state-fix-20261003`
- `BRANCH=codex/fix-native-terminal-state-20261003`
- `BASE_SHA=ee9aa8c4ac627f0e79bf9a838b027c384a6297f0`
- `PROTECTED_EXECUTION=exec_1c81107b4f5c485eb170eb219ad1674a`
- `PARALLEL_ISOLATION=本 worktree、.validation/monitor-terminal-scroll-20261003 及测试 HOME/XDG；未向受保护 execution/provider 发控制操作`
- 本次代码与受保护多节点任务没有文件交集；受保护任务涉及的 `bridge.py`、`m9_integration.py`、`mcp_server.py`、`thread_identity.py`、`node_protocol.py` 未修改。
- `MAIN_WORKTREE_MUTATION=NONE`
- `OTHER_EXECUTION_CONTROL=NONE`
- `SHARED_RUNTIME_MUTATION=NONE`
- `MERGE=NOT_RUN`
- `PUSH=NOT_RUN`
- `RUNTIME_ACTIVATION=NOT_RUN`

## 改动文件

- `MonitorApp/Sources/CLINXMonitor/MonitorState.swift`
- `MonitorApp/Sources/CLINXMonitor/TaskPresentation.swift`
- `MonitorApp/Sources/CLINXMonitor/NavigationEntry.swift`
- `MonitorApp/Sources/CLINXMonitor/ExecutionListView.swift`
- `MonitorApp/Sources/CLINXMonitor/ActivityStore.swift`
- `MonitorApp/Sources/CLINXMonitor/ActivityView.swift`
- `MonitorApp/Tests/CLINXMonitorTests/MonitorTests.swift`
- `MonitorApp/Tests/CLINXMonitorTests/ActivityTests.swift`
- `.validation/monitor-terminal-scroll-20261003/FINAL_REPORT.md`

## 剩余风险

- Swift 编译器未安装导致本轮不能排除 macOS SDK/API 级问题；外层集成前必须在匹配 macOS/Xcode 上执行 SwiftPM 测试和构建。
- Activity 的反馈放大器已消除并有纯状态回归，但没有现场采样，不能把富文本选择、AppKit layout、历史数据量或其他线程问题排除。
- Activity API 仍按精确 `executionRef` 消费；若 Observer 对历史 execution 只提供 task 级详情，选择历史行会保留未知而不会串到当前 execution，需服务端提供精确历史详情后再做 Mac 验收。

## 机器可读交付字段

- `ROOT_CAUSE=ROW_STATUS已确认；SCROLL_HANG现场最终热点未确认`
- `IMPLEMENTATION=完成（本地未激活）`
- `REGRESSION_RESULTS=Python 33 passed；compileall PASS；git diff --check PASS；SwiftPM NOT_RUN（CAPABILITY_UNAVAILABLE）`
- `READ_ONLY_SIDE_EFFECTS=仅隔离测试 HOME/XDG 与本 worktree .validation；零绑定/lease/registry/Provider/原生历史/共享服务写入`
- `CHANGED_FILES=见上方改动文件清单`
- `COMMIT_SHA=最终 HEAD 由 git rev-parse --verify HEAD 回读，见最终回执`
