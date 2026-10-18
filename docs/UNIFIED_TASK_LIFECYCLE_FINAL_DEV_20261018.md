# Unified Task Lifecycle v1：最终收口增量（DEV / PARTIAL）

基线为 `3613e057678990a75123713eb53093c64b814860`，功能分支
`codex/unified-task-lifecycle-v1-20261018`。本次未合并 main、未部署、未重启正式服务。
本报告补充此前 CONTROL / AIR_VISIBILITY 验收记录，不把此前证据当成本次 GUI 复验。
证据根目录：`/data/artifacts/clinx-lifecycle-final-20261018T215559Z`。

## 单一 Owner：真实根因与最小阻断

两个真实客户端先加载同一个私有官方 daemon 的 Thread，再同时 `turn/start`，
两者返回同一个 Turn ID `01a15119-0a13-79f2-9fbe-ef987ca1040a`。
第二次 start 能进入已有 turn，因此一次 idle 观察加 CLINX lease 不能防止
错误认领 Desktop 的执行。官方接口没有可用的 turn/start compare-and-start；
不能把客户端锁解释为 Desktop 参与的原子锁。

- `native_provider.select_writer_client` 拒绝已知共享 Desktop daemon 的 managed start，
  返回 `NATIVE_SHARED_WRITER_NOT_QUALIFIED` / `side_effect=NONE`；专用 endpoint
  若是 native endpoint 的 symlink 别名也拒绝，包含 Host 执行路径。
- 默认 AppServer client 在 wire / turn ownership 登记之前再次拒绝共享 start。
- 保留已有授权、task lease、Writer fencing、prepared-request 幂等及审计。
  不强制卸载 Desktop writer，不自动创建 successor 或重派 turn。
- 真实 `turn/interrupt` 对过期 turnId 返回 expected-active-turn 错误，当前 turn 仍 active。
- 真实启动 ACK 丢失后，三个并发重试只发送一次 turn/start；prepared 保留 RUNNING
  （请求已消耗，不能重派），task 为 RECOVERY_REQUIRED，CLINX 不填造未知 Turn ID。
  这不是宣称原生 turn 仍 RUNNING；其真实 terminal 状态另有 provider 记录。

证据：`race-final/receipt.json`、`ack-loss/native-device.json`、
`mtls-ack/native-device.json`。后者是实际配对证书和 loopback mTLS → NodeService
→ 真实官方 Unix AppServer；物理机器是 P620，逻辑测试 peer 名称 air/p620，
不能当成跨物理设备 mTLS 验收。

**PASS：已知共享路径的 fail-closed、exact cancel 校验、ACK 丢失不重派。**
**BLOCKED：任意外部客户端共享专用 daemon 时的完整原子 Owner 保证，以及共享
Desktop writer 无缝继续。** 当前拒绝策略牺牲该路径可用性以避免错误认领。

## daemon 元数据通知 outbox

复用 TaskRegistry 同一 SQLite 内既有 `shadow_ledger.EventStore` / `v2_event_outbox`。
新增严格 metadata aggregate；payload 不允许 execution_ref 等执行身份，不进入
execution reducer。任务身份、旧 binding、原生历史 provenance、threadSource=user
及真实 originator 保持原约束。没有新任务表、第二个 daemon 或自动执行逻辑。

- 只由既有授权创建路径入队；观察和 get_status 不入队、不重试。
- 幂等键为 host + Thread ID 的哈希；一次 metadata event 对应一次 outbox intent。
- 复用 CompletionRuntime，execution owner / node adapter 重启恢复已有入队项。
- 每批默认 8 条、上限 16；最多 8 次，5–300 秒退避。RPC 前持久递增 attempts，
  进程崩溃不重置预算；失败为 RETRY，耗尽/身份变化为 FAILED，成功 APPLIED。
- 重试仅 thread/read + thread/name/set，不 resume/start，不启动离线 daemon。
- 校验 host、配置 endpoint、originator、cwd、threadSource、当前 binding/title、归档状态。
  已改名/已换绑时停止，不覆盖观察到的更新。
- 保留官方 socket 的稳定配置路径，不能将 symlink 的进程实例地址固化。
  本轮首次真实实验发现 daemon 重启后误报 CONFIGURATION_CHANGED，修复后重验通过。
- get_status 增加只读 desktop_publication：state/attempts/last_error/retry_at。
  重试异常记录日志，不改变执行结果或重派 turn。未增加独立 UI。

三台物理设备使用私有 CODEX_HOME、真实官方 daemon，均验证了离线入队、
新 Python 进程恢复投递、thread/name/updated 通知、标题读回、重复不重投及 turn 不重放。
P620 官方 CLI 0.160.0，Air 0.162.1，iMac 0.162.0。
证据：`p620-outbox-v2/receipt.json`、`air-outbox-receipt.json`、`imac-outbox-receipt.json`。
这三组 task binding 是隔离 fixture，不是正式 Registry 注册验收，也不是 GUI 验收。

**PASS：已经持久入队 intent 的有界重试、崩溃预算、幂等和状态可观察。**
边界：metadata 为 at-least-once，ACK 丢失可重复发送相同标题；官方 name/set 无 CAS，
read 与 set 间的同时人工改名仍有窗口。Provider 创建 / binding 与 outbox 入队尚非单一
跨系统事务，入队前的崩溃窗口不在此持久补偿保证内；存储失败明确返回诊断并记 ERROR。
入队先于 retry worker 启动，worker 启动失败不丢失已持久意图，并有专项回归。
Mac 详情 UI 尚未展示 publication 状态，当前通过 API / 日志查看。

## 本次真实 GUI 与构建

本轮仅在默认原生存储创建一条专属 P620 DEV Thread：
`01a15114-1d0d-7683-af17-ea95313a2c5c`，隔离 Task ID
`task_65da4f2590844ac48061d63e834c68ee`，标题
`CLINX_FINAL_DEV_20261018T215559Z_P620`。通过真实 bridge 创建，readOnly
sandbox、纯文字内容、真实 originator，Registry task_count=1，publication=APPLIED。
正式 Registry 没有该隔离 Task 绑定，不得声称已正式注册。

| 物理设备 / 检查 | 本轮结果 | 证据和限制 |
| --- | --- | --- |
| Air 安装版 Desktop 的 P620 项目任务列表 | PASS | AXButton 精确标题见 `air-ax-probe.json` / `air-after-focus-v2.json`；不是 thread/list 推断 |
| Air 打开正文、继续、GUI 取消/恢复 | BLOCKED / 未验证 | deep link / AXPress 未确认正文；焦点实测仍在 composer，安全 guard 拒绝 Enter，没有发送用户 turn |
| iMac 安装版 Desktop GUI | 未验证 | bundle com.openai.codex、进程存在；System Events 返回无窗口，直接 AX 发现 1 窗口，但有界遍历未取得测试条目；不能说列表不存在或 PASS |
| P620 本机 Desktop GUI | 未验证 | 仅发现 CLI/AppServer，未取得本机 Desktop UI；Air 的 SSH P620 列表证据单列 |
| 三设备 GUI 离线重连 / 通知补偿 / 统一 Task ID | 未验证 | 私有 daemon API 验收不等同于安装版 GUI；未向正式 Registry 或安装版 CLINX 注入 fixture |
| Air macOS build + Swift 测试 | PASS | `swift test --filter 'NetworkObservation|TaskPresentation'`，8 tests / 0 failures |
| iMac macOS build + Swift 测试 | PASS | 同上，8 tests / 0 failures |

GUI 探针通过公开 macOS Accessibility API，只允许精确匹配本轮测试条目的导航。
有界探针中出现远程工具传输错误与焦点拒绝；未放宽焦点断言、未在 ORION/已有用户
线程发送按键或执行请求。没有修改闭源 Desktop 二进制。

## 有限回归与兼容性

`regression-commit.log`：190 passed，8 subtests passed，12.67 秒。
覆盖 publication、thread source、native control、真实配对 mTLS + fixture Provider、
interop、execution owner/socket replay、native completion、shadow ledger。
另有上述真实官方 AppServer + mTLS 故障注入，不混淆两者。
Ruff 与功能分支基线相比新增诊断 0（两者均 139 条既有诊断）；新增文件独立 lint 通过，
`git diff --check` 通过。未宣称整个既有仓库 lint 全绿。
4 个警告为既有多线程进程 fork DeprecationWarning，无跳过竞态断言。

outbox 专项覆盖 offline/restart、并发 enqueue/delivery、重复投递、ACK 丢失、
RPC 前崩溃、最后一次崩溃/耗尽读回、改名/归档/身份变化、批次限额、
read-only status 不建 schema、socket symlink 换代。
不回填旧线程、不改写旧 Task/Execution/History，不重做已验收的 canonical 身份迁移。
shadow ledger 既有 schema 兼容，新增 destination 索引和独立 aggregate 校验。

本轮 GUI 测试结束后，仅对上述本轮创建的 Thread 通过官方 API 归档；
归档前精确状态 idle，归档后 archived thread/list 读回命中，见 `gui-thread-cleanup.json`。
未清理任何既有用户原生会话。

本轮修改：`app_server.py`、`native_provider.py`、`desktop_publication.py`、
`bridge.py`、`completion_runtime.py`、`execution_owner.py`、`node_execution_adapter.py`、
`m9_integration.py`、`mcp_server.py`、`shadow_ledger/store.py`，以及相关四个测试文件。

**Unified Task Lifecycle v1 整体仍为 PARTIAL，未达到完整验收标准。**
后续入口是官方原子 writer admission / 独占 daemon 边界，以及三设备真实安装版 GUI
打开、继续、取消/恢复、离线重连与统一 Task 详情验证；不得用上述 API PASS 替代。
