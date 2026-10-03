# CLINX 原生终态状态一致性修复报告

ROOT_CAUSE=已确认

`ThreadIdentityReader` 已经能够从 `thread_history_1.sqlite` 读到精确最新 turn，但旧实现只把 Provider 原始值放在 `native_status.status`。展示和消费链路没有稳定的 native 终态字段，只能看到 `execution_state=UNKNOWN` 与 socket `UNKNOWN/notLoaded`。这把“没有 managed execution”“实时 owner 未知”和“历史 turn 已完成”混成了一个未定义状态，外部消费方可能按缺少 active 信号错误显示为执行中。`THREAD_UNBOUND` 本身是合法未纳管结果，不是执行失败，也不能用于自动 adoption。

CLINX Monitor 的真实运行来源已核实：`observer_server.ObserverStore.project` 只投影 registry 的 `task.execution_state`、`task.codex_running` 和 exact execution result；`MonitorState.swift` / `StatusSystem.swift` 依据这些字段映射 `RUNNING`。未纳管 native thread 不会进入该 task 列表，因此本修复不能宣称修复 Codex Desktop 自身或任何未观测客户端的 UI。对于绑定 task，managed `execution_state` 仍是唯一 managed 状态，不由 native history 反写。

FIX_STATUS=PASS

最小修复内容：

- `native_history.py` 保留 Provider 原始 `status`，并按精确最新 turn 增加 `state`、`terminal`；缺失、截断、schema 不兼容和不可读 history 明确返回 `UNKNOWN`。
- `thread_identity.py` 增加只读 `native_display_state` 投影：精确证明的 live active owner 优先；`UNKNOWN`、offline、`notLoaded` 不覆盖已证明终态；owner 冲突返回 `UNKNOWN`；不改写 `execution_state`、task、lease、registry 或 provider。
- `mcp_server.py` 为 `native_turn_state`、`native_display_state`、`native_status_source` 增加兼容的可选输出字段。
- `test_thread_identity.py` 增加脱敏 SQLite fixture 和状态一致性回归；`docs/THREAD_IDENTITY.md` 补充字段契约。

FINAL_STATUS=PASS

验收范围为本 worktree 的 native history / exact thread reader / MCP schema 代码和隔离 Python 回归。未宣称共享服务、Provider、CLINX Monitor 二进制或 Codex Desktop 已激活；这些属于后续串行集成和运行时验收。

WORKTREE=/home/pvxlabs/dev/clinx-terminal-state-fix-20261003
BRANCH=codex/fix-native-terminal-state-20261003
BASE_SHA=474c52faa670929c431d341292d655fa103b5c49
COMMIT_SHA=350317b
CHANGED_FILES=docs/THREAD_IDENTITY.md,mcp_server.py,native_history.py,test_thread_identity.py,thread_identity.py,.validation/native-terminal-state-fix/FINAL_REPORT.md

REGRESSION_RESULTS=通过 `pytest -q test_thread_identity.py test_execution_liveness.py test_monitor_finalized_ui.py -k 'not mcp_schema_and_calls'`：66 passed，1 deselected，13 subtests passed；native 专项 `pytest -q test_thread_identity.py -k native_`：6 passed，23 deselected，3 subtests passed；`python3 -m compileall -q native_history.py thread_identity.py mcp_server.py test_thread_identity.py` 通过；`git diff --check` 通过。完整 `test_native_interop.py` 与 `test_thread_identity.py::test_mcp_schema_and_calls` 未运行，原因是当前环境缺少 `jsonschema` 依赖（已记录为环境限制，未放宽断言）。

READ_ONLY_SIDE_EFFECTS=reader 使用 SQLite `mode=ro`、`PRAGMA query_only=ON` 和 bounded exact lookup；新增测试只写当前 worktree 下的临时 fixture。未建立 binding、adoption、execution、lease、checkpoint，未启动或订阅 Provider，未发送 turn，未修改正式 Codex SQLite。

PROTECTED_EXECUTION=exec_1c81107b4f5c485eb170eb219ad1674a
PARALLEL_ISOLATION=本 execution 只在锁定 worktree `/home/pvxlabs/dev/clinx-terminal-state-fix-20261003` 修改；测试缓存和隔离目录位于 `.validation/native-terminal-state-fix`；未向主 worktree、受保护 execution、其他 worktree 或共享 socket/端口写入控制操作。
MAIN_WORKTREE_MUTATION=NONE
OTHER_EXECUTION_CONTROL=NONE
SHARED_RUNTIME_MUTATION=NONE
MERGE=NOT_RUN
PUSH=NOT_RUN
RUNTIME_ACTIVATION=NOT_RUN
MACOS_RUNTIME_VALIDATION=NOT_RUN；P620 无法原生运行 macOS SwiftUI，未伪造截图或 GUI 验收。SwiftUI Monitor 的现有映射已完成源码核对，但未编译运行。

剩余风险：外部客户端若忽略 `native_display_state`、继续把 `UNKNOWN` 当作执行中，仍需在该客户端单独修复；Codex Desktop 原生 UI 不受本 worktree 控制。共享 runtime 尚未激活，生产或真实用户 UI 状态保持未验证。
