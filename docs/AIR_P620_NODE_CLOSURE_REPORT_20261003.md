# Air＋P620 节点收口报告 — 2026-10-03

本报告追加于历史交接记录之后，不改写此前的 BLOCKED、NOT_RUN 或旧执行回执。当前候选分支为 codex/air-node-delivery-20261003，候选提交为本报告提交前的 fcb3be12b34c9bc18bfa011d52d4e35389d1abb5；P620 工作区仍为 f88ffe0b3b20cf4361ba3899bd341ae0ea4a42c4。前序 Monitor 终态、Activity 与滚动修复保留。

## 当前结论

Air 上的 Monitor App 已构建、签名、安装并通过真实窗口读回；本机 UI 读取的是 P620 的 Observer 数据。Air 节点 helper 当前安全地停在 blocked / PAIRING_REQUIRED，P620 没有发现已激活的 NodeRouter 注册入口、中心节点身份或 NodeRPC listener。因此本轮没有把数据库记录、服务 active 或本地协议测试冒充成 Air↔P620 实际节点闭环。

## 逐项结论

| 项目 | 结论 | 证据与边界 |
|---|---|---|
| PYTHON_FAILURE_TRIAGE | PARTIAL | .validation/air-node-delivery/PYTHON_FAILURE_TRIAGE_20261003.md；隔离 OPAQUE wheel 后全套为 956 passed、39 failed、9 skipped。剩余失败由 Air 的 macOS 路径、缺少 rustc 1.98/kernel binary、P620 Host/runtime、/usr/bin/python3.12 和局部 discovery fixture 分组；没有删除断言或新增 skip。 |
| APPLICABLE_REGRESSION | PASS（适用回归） | test_node_protocol.py、test_thread_identity.py、Monitor 目标测试与生产 OPAQUE/Monitor pairing 定向回归通过；Swift 72 项全通过。剩余全套失败未证明本轮候选引入回归。 |
| REMOTE_NODE_WIRING | BLOCKED | node_protocol.py 的 NodeRPC 是独立进程/loopback 传输；mcp_server.py 只在 CLINX_ENABLE_NODE_ROUTER=1 注入本地 NodeService；没有中心注册/心跳/远端 reader 绑定入口。P620 launch-mcp 未设置该开关，也未发现 NodeRPC listener。 |
| SHARING_AUTHORIZATION | BLOCKED | Air Devices 目录只有本机 identity.json，没有可信 P620 peer；helper 真实健康状态为 PAIRING_REQUIRED。配对只证明身份，尚不存在持久化的 Air 读取/执行共享授权。 |
| P620_RUNTIME_ACTIVATION | BLOCKED | 独立 SSH 读回显示 clinx.service 当前加载 execution-authority-474c52faa670-monitor-closure，现有服务 active，但没有独立 Node centre identity/listener/maintenance activation entry。未重启或改写共享服务、任务 DB、registry、lease。 |
| AIR_NODE_REGISTRATION | BLOCKED | 实际安装 App bundle 中的 helper 可加载并退出 0；health.json 为 blocked / PAIRING_REQUIRED，LaunchAgent last exit code=0、当前未运行。没有伪造 ONLINE 或手写节点注册。 |
| AIR_NATIVE_CONTEXT_READ | BLOCKED（Air 来源） | 通过当前对外 CLINX MCP 对只读样本 01a10178-c993-7960-9fb5-14c00f221d05 读回的是 THREAD_NOT_FOUND / CURRENT_USER_NATIVE_INDEX、host=p620、read_only=true；这证明当前中心没有 Air 来源，不证明 Air 历史不存在。 |
| AIR_DIRECTED_EXECUTION | BLOCKED | Air helper 的 SharingScope 固定 execute_tasks=False，NodeService 未接入 canonical prepare/start/status/cancel 回调；没有创建任务、发送 turn、resume、cancel 或写 lease。 |
| MONITOR_REGRESSION | PASS | cd MonitorApp && swift test --scratch-path /private/tmp/clinx-air-final-swift-20261003：72 tests、0 failures；Activity/Monitor 定向测试 16 passed。 |
| FINAL_ACTIVITY_TAIL_ACCEPTANCE | PASS（本机 UI） | 实际安装 App 中打开历史 execution，Inspector 显示准确 task/execution、Blocked、40 条 timeline；Activity 可上滑到历史、下滑回 latest。 |
| SCROLL_RUNTIME_ACCEPTANCE | PASS（本机未复现） | 安装 bundle 上真实 Activity 操作未复现卡死；sample 采样约 2.2% CPU、约 157904 KB RSS，调用栈主要为 SwiftUI layout/accessibility。原现场根因仍未确认。 |
| AIR_INSTALLED_BUILD_IDENTITY | PASS | /Applications/CLINX Monitor.app，bundle id com.pvxlabs.clinx.monitor，版本 0.3.0 (3)，arm64，codesign --verify --strict 通过；运行二进制 SHA256 074bb31067bad264e95398823a2cb7a5b67e5b034fac814d9d06338a4f02f247。旧制品保存在 .validation/air-node-delivery/。 |
| AIR_P620_NODE_ACCEPTANCE | BLOCKED | 尚未完成可信配对、中心注册、Air 原生样本读回或授权执行；不能将 Observer UI 连接、协议单测或服务 active 视为节点闭环。 |
| IMAC_ACCEPTANCE | NOT VERIFIED | 本轮没有 iMac 可达且授权的安装/配对/维护入口；不把两机状态扩写为三机完成。 |
| MERGE_AND_PUSH | PASS（候选分支） | fcb3be1 已推送到 origin/codex/air-node-delivery-20261003；这不是合并到 main，也不是 P620 运行时激活。 |
| FINAL_STATUS | BLOCKED | Air Monitor 制品可用，Monitor 回归与本机滚动验收通过；Air↔P620 NodeRPC 注册、共享授权、Air 原生读取和定向执行仍被 P620 缺少正式中心身份/注册 listener/维护激活入口阻塞。 |

## 最小解阻动作

需要在 P620 以独立于当前 MCP/Provider 的正式维护入口启用与当前候选版本匹配的 Node centre 服务，使用现有受信身份/配对窗口完成 Air pairing，并明确授予可撤销的 read_sessions；若要交付执行，还需在 Air helper 接入 canonical Task/Execution/policy/provider 回调并由中心显式授予 execute_tasks。完成后才可重新安装同一候选、执行真实 MCP Air 来源读回和专用执行验收。
