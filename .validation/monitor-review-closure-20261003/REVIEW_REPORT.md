# Monitor 前置复审整改报告

## 结论

```
CODE_REVIEW_REMEDIATION=PASS
PYTHON_REGRESSION=PARTIAL（23 passed；1 个用例因环境缺少 jsonschema 未执行）
SWIFT_BUILD=NOT_RUN（P620 无 swift/Xcode）
SWIFT_TESTS=NOT_RUN（未取得匹配版本的 Mac）
MACOS_GUI_ACCEPTANCE=BLOCKED
ROW_STATUS_ROOT_CAUSE=源码证据已确认；Mac 运行时未验证
SCROLL_HANG_ROOT_CAUSE=未确认（只有源码候选，缺少 Mac sample/Time Profiler）
FINAL_STATUS=PARTIAL
```

本轮只修复前置审查指出的确定性源码缺陷，没有把上一 execution 的矛盾回执改写成 parser 误判，也没有把 Mac 用户界面恢复写成已验收。受保护 execution `exec_1c81107b4f5c485eb170eb219ad1674a`、主 worktree 和共享运行时均未被控制或修改。

## 根因证据与实现

1. **行状态与 Activity 生命周期**：`ActivityView.==` 原来没有比较 `monitorStatus`、`state`、`executionState`、`codexRunning`、完成时间和观察版本；`InspectorView` 的 `.equatable()` 因而可以复用旧的 running Activity。现已纳入这些精确生命周期输入。`ActivityContentView.isTerminal` 现在只接受 canonical terminal state、`completedAt` 或精确 result 的 terminal status；展示分类 `.blocked` 本身不再停止轮询。
2. **终态尾部读取**：`ActivityStore.run` 原来三次轮询后永久 `return`，且错误、索引迟到和最后 result 仍可能未读取。现在只在成功读取后消耗有限高频尾部次数；错误不会消耗次数，次数耗尽后改为 10 秒退避并保留不完整提示，收到 `kind=result` 才结束。`stop()` 使用 generation 令牌阻止关闭后的下一次请求。
3. **显示预算**：16 MB 预算不再阻断 live tail。超预算时保留有界 final-result tail、展示历史截断提示，后续 live refresh 仍可读取结果；该预算只针对 Activity 显示内容，不宣称 App 总内存上限。
4. **历史 execution 选择**：Observer v1 只有 `task(ref)`，没有精确历史 execution detail endpoint。选择历史行时先保留 `(taskRef, executionRef)` 列表快照；若返回当前 execution，则标记 `selectedDetailIncomplete` 并停止当前 execution 的详情/事件写入，避免串状态。Inspector 显示来源明确的提示；精确 Activity capability 仍可按 execution 使用。
5. **晚到列表响应**：refresh 与分页请求增加 `listRequestGeneration`，并继续校验 source token；旧来源、旧代次的 response 不能回写当前列表。事件合并按当前选择的 execution 过滤。
6. **测试 fixture**：`ActivityTests` 的 helper 现在独立传入 `kind`，result fixture 真正使用 `kind=result`；同步 XCTest assertion 先 await 到局部值；新增错误后恢复、第四次尾部 result、重复/预算尾部和历史详情回退用例。

## 验证

- `git diff --check`：通过。
- 隔离 HOME/XDG 下执行：
  `pytest -q test_observer_activity.py test_monitor_finalized_ui.py -k 'not display_allowlist_redaction_bounds_and_read_only'`：`23 passed, 1 deselected`。
- 曾执行完整目标命令（含 `test_observer_schema.py`），收集到环境阻塞：`ModuleNotFoundError: No module named 'jsonschema'`；未安装依赖、未删除或放宽断言。`test_observer_activity.py::test_display_allowlist_redaction_bounds_and_read_only` 也因同一缺失依赖未执行。
- SwiftPM `cd MonitorApp && swift test`：NOT_RUN；P620 无 Swift/Xcode。不得以 Python 结果替代 Swift 编译或 Swift 测试。
- `.app` 构建：NOT_RUN；没有匹配版本 macOS 运行时、CPU/主线程/内存/交互延迟或截图证据。

## 变更范围与风险

变更文件：

- `MonitorApp/Sources/CLINXMonitor/ActivityStore.swift`
- `MonitorApp/Sources/CLINXMonitor/ActivityView.swift`
- `MonitorApp/Sources/CLINXMonitor/InspectorView.swift`
- `MonitorApp/Sources/CLINXMonitor/MonitorState.swift`
- `MonitorApp/Tests/CLINXMonitorTests/ActivityTests.swift`
- `MonitorApp/Tests/CLINXMonitorTests/MonitorTests.swift`

Swift 代码仍需在 macOS 13+ SDK 编译确认 `.onChange(of:)` 的兼容重载、`@MainActor` 生命周期和 `ActivityStore` 的可注入 sleep。Activity 的富文本布局、文本选择、滚动观察器以及真实 CPU 100% 热点没有 Mac 采样，因此滚动卡死根因和修复效果保持未确认。

```
BASE_SHA=edeea54f5d2b871a88c8180950eb91f0856fd43c
COMMIT_SHA=afe3c2f8119e406f704e9edf5ee4928ce9c6eb4f
WORKTREE=/home/pvxlabs/dev/clinx-terminal-state-fix-20261003
BRANCH=codex/fix-native-terminal-state-20261003
MAIN_WORKTREE_MUTATION=NONE
OTHER_EXECUTION_CONTROL=NONE
SHARED_RUNTIME_MUTATION=NONE
PROTECTED_EXECUTION=exec_1c81107b4f5c485eb170eb219ad1674a
PARALLEL_ISOLATION=本 worktree 与主 worktree/受保护 execution 隔离；仅测试子进程使用独立 HOME/XDG
READ_ONLY_SIDE_EFFECTS=未建立绑定、未写 registry/lease、未启动 Provider、未控制共享服务
PUSH=NOT_RUN
MERGE=NOT_RUN
ACTIVATION=NOT_RUN
```
