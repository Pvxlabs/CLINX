# CLINX Monitor Phase 0/1 最终报告（历史记录）

## Phase 2 最终 canonical integration — 2026-10-01 UTC

本节是 Phase 2 当前状态；下方 2026-09-30 的 BLOCKED 记录保留为历史证据。

- 基线为最新 canonical `main`：`d1ad3f2d761f4f2f6a4f82759b4eb4d522ee813b`。
  `43ee9af5` 和 `22aae814` 均为其祖先。本次只集成原有 13 个 Monitor dirty
  文件，不修改此前 Provider/Host/QEX infrastructure 提交。
- **CANONICAL_INTEGRATION=PASS，ENGINEERING_COMPLETE=YES。**
  Monitor Phase 2 已作为独立 commit 进入 `main`，以非强制 push 同步到 origin。
  最终 local HEAD、origin/main、`git ls-remote` SHA 相等；ahead/behind `0/0`，
  worktree clean。精确提交 SHA 以本仓库 `git rev-parse HEAD` 为准。
- MonitorApp 是正式 macOS 13+ SwiftPM / SwiftUI MenuBarExtra 工程。
  `docs/monitor/Observer*.swift` 是 Phase 0/1 历史交接快照，不参与正式 target。
  客户端只调用 ADR-006 的四个 GET route shape；没有 task/execution mutation API
  或 UI。Observer bearer 通过本机非同步 Keychain 保存，endpoint 先验证 HTTPS、
  禁止 URL userinfo/query/fragment 和 redirect；产品界面不展示完整 credential。
- 本轮精确重跑此前 P620 环境失败的 2 个 Observer HTTP 与 3 个 MCP child 测试：
  **5/5 PASS**。完整 Observer 定向 unittest：**21 tests PASS**；schema 有效，
  **9 份 synthetic examples PASS**；Monitor Swift fixture 与 canonical examples
  逐字段相等，Info.plist 解析、build script shell 语法和 `git diff --check` PASS。
- 完整 canonical Python regression：**618 passed、102 subtests passed、
  1 skipped、0 failed**。被跳过的是需要显式启用真实 Provider 的 opt-in 测试，
  此前已经另行在 P620 运行并通过；没有降低或排除 Observer/backend tests。
- **RUNTIME_ACCEPTANCE_PENDING=YES。** P620 为 Linux，未提供合法 macOS/Swift
  toolchain，因此 `swift build`、`swift test`、app launch 与原生 UI 均未运行。
  真实 Observer connection、Keychain runtime、Tailscale/TLS/ACL 均未验证。
  这些 Mac 侧门槛不影响本次 canonical engineering integration 的 PASS。
- `PRODUCTION_MUTATION=NO`；`ORION_TASK_MUTATION=NO`；没有部署 Observer、
  改变正在运行的 CLINX task 或触碰生产服务。

**NEXT_STATE=READY_FOR_MACOS_RUNTIME_ACCEPTANCE。** 在真实 Mac 上运行
`swift test`、`./Scripts/build-app.sh`、启动 `.app`，再分别验证 synthetic
fixture UI、经授权的真实 Observer 连接、Keychain 和私有链路。

---

## Phase 2 canonical integration 历史续验 — 2026-09-30 UTC

- Canonical remote 已由 GitHub 仓库接口回读：`Pvxlabs/CLINX` 默认分支 `main`
  本轮最新读数为 `b99dce56243b73d8ba0764360c7eb9e56bf48f47`，本地同 SHA。
  `43ee9af5ea9d5e7a9370bff0583946f489a32eec` 和 `22aae814` 均为祖先；
  两个后续提交修改 app_server/bridge/host_executor/配置/既有测试，
  与 Phase 2 dirty diff 不重叠。
- P620 现有文件树没有旧 `/home/pvxlabs/dev/clinx` 路径；可见 CLINX checkout 是
  `/home/pvxlabs/dev/clinx-pvx1807-remediation`，其 origin 指向上述仓库。
- Phase 2 逐文件审计：`MonitorApp/` 10 个文件均为项目配置、Swift 源、
  synthetic fixture、测试或构建说明；另有 `.gitignore` 和两份 Monitor 交接文档。
  无生成构建产物、真实 secret/token、运行态数据库或任务 mutation 入口。
  测试中的凭据样式占位查询改为普通样例，仍检验拒绝带查询的 endpoint。
- 正式 `python3 -m unittest -q test_observer_server test_observer_schema` 和
  `python3 -m pytest -q` 都停在 DRS/QEX admission：协调器 socket 15 秒内
  未响应（exit 124），测试未启动。直接 Python runner 执行相同 suites：
  定向 19/21；在最新 `b99dce56` 上的全量 pytest 为
  588 passed、75 subtests passed、5 failed。
  2 个 HTTP 测试被 sandbox 拒绝创建 loopback socket；3 个 MCP child
  测试因只读数据库退出。它们仍需真正 P620 Host 重跑，不记 regression PASS。
- 在线 P620 Host 的终端执行工具被自动审批拒绝，原文为
  `MCP tool call requires approval, but approval policy is never`；只读
  `pwd`/`git status` 也未执行。当前 `.git` 仍只读。由于 P620 工程验收
  未通过、Git metadata 不可写，Phase 2 **没有 commit/push**。

本轮状态：`CANONICAL_INTEGRATION=BLOCKED`，`PYTHON_REGRESSION=BLOCKED`，
`RUNTIME_ACCEPTANCE_PENDING`。这不改变下方 Phase 0/1 历史证据。

---

## Phase 2 更新 — 2026-09-30 UTC

Phase 2 已增加 `MonitorApp/` 正式 macOS 13+ SwiftPM/Xcode 项目：SwiftUI
MenuBarExtra、ObserverModels/ObserverClient 正式 target、Keychain/HTTPS 设置、
只读 GET、任务与当前 execution 列表/详情、event timeline/cursor、offset 翻页、
刷新/轮询与 stale/error/degraded/unknown 显示，以及 synthetic wire/client/store tests。
`docs/monitor/Observer*.swift` 保留为 Phase 0/1 历史交接快照。

当前环境是 Linux，无 `swift`/`xcodebuild`，所以 Swift build/test、app launch、
真实 Observer connection 与 UI acceptance 均 **未验证**。没有启动生产 Observer，
没有修改 CLINX/ORION 运行任务；Tailscale/TLS/ACL 没有真实链路证据，均未验收。

本轮验证：JSON Schema 本体有效，9 份 synthetic examples 可解析；
`sh -n MonitorApp/Scripts/build-app.sh` 与 Info.plist 解析通过。
定向 unittest 运行 21 项，19 项通过、2 项因 sandbox `socket.socket` 返回
`PermissionError: [Errno 1] Operation not permitted` 失败。全量 unittest 运行
393 项，5 项失败（上述 2 项，加 3 项既有 MCP child 因只读数据库写入失败退出）。
全量 pytest：**587 passed, 75 subtests passed, 5 failed**，对应同 5 项。
直接标准测试命令还被 DRS/QEX 包装层拦截：其状态目录只读；本轮用
Python 内嵌 runner 执行了相同 suites，失败证据如上。不可把部分通过写成
完整 regression PASS。Swift 工程尚未取得编译/测试证据，所以工程资格仍待 Mac gate。

本轮 Git 交付 **BLOCKED**：`git add` 无法在只读 `.git` 创建 `index.lock`；
`git ls-remote origin refs/heads/main` 因 `github.com` DNS 解析失败，未能确认
最新远端 main 或执行 push。本轮进行中，本地 `main` 与本地 tracking ref
从起始 `43ee9af5ea9d5e7a9370bff0583946f489a32eec` 一同前进到
`22aae814d79c66c19dc2926178f28df19ef5d400`（独立的 host tools 修复）；
基线仍是该提交祖先，改动文件无 Monitor 重叠。ahead/behind `0/0` 仅指
本地 tracking ref，工作区有 Phase 2 未提交改动。外部 `/data/artifacts/clinx-monitor-spec-20261001/`
重复导出仍待合法写入范围，不再作为 Phase 2 前置 Gate。

---

报告日期：2026-09-30 UTC。产物批次：clinx-monitor-spec-20261001。
总交付状态：**BLOCKED**，原因仅为指定外部 artifact 目录不在当前 execution 的 registered
project 范围。仓库内 specification/backend contract 实现与 Linux 验证已通过。
未验证的 Mac/Tailscale 部署层不能记为 PASS。

## 已交付 — PASS

- docs/CLINX_MONITOR.md：TaskRegistry/execution projection/host evidence/structured
  result/MCP/tunnel 审计；canonical schema v1；security/rotate/revoke；Swift/UI
  hierarchy、状态映射、通知、低功耗、stale、Keychain、acceptance matrix。
- docs/architecture/ADR-006-monitor-read-only-observer.md：单一 authority 与只读 adapter 决策。
- observer_server.py：四个 authenticated GET route，固定 loopback listener，
  SQLite mode=ro/query_only、单请求一致 snapshot、时间/数量/响应大小 bounds。
- docs/monitor/observer-v1.schema.json、api-examples.json：JSON Schema + 明确标记的
  synthetic fixtures；无真实 ORION payload。
- docs/monitor/ObserverModels.swift、ObserverClient.swift：Codable/Sendable 模型，
  HTTPS URLSession async/await reference、Keychain、拒绝 redirect、响应大小限制。
- test_observer_server.py、test_observer_schema.py：只读/auth/serialization/unknown/
  ownership/cursor/bounds/schema 验证。
- docs/monitor/CLAUDE_CODE_HANDOFF.md：下一阶段 Mac 与真实网络验收步骤及阻塞恢复方式。

Backend 仅使用 stdlib；schema 测试需要 development-only jsonschema >=4.18,<5。
没有修改已有 TaskRegistry、bridge、completion runtime、MCP、host executor 或 ORION 代码。
没有部署 observer，也没有启动或重启任何既有 CLINX/ORION 服务。

## 验证证据

| 验证 | 结果 | Host evidence |
| --- | --- | --- |
| python3 -m unittest -q test_observer_server test_observer_schema | PASS，21 tests，1.593s | hostexec_cb04a2b9c2ea45b98792610c4d5087ea |
| python3 -m unittest discover -q | PASS，393 tests，7.054s | hostexec_37de2e97755c4b31a9bdaf637bc3f0bf |
| python3 -m pytest -q | PASS，592 tests + 75 subtests，14.23s | hostexec_6475a95f81c24120aa6c71466613bca7 |
| git diff --cached --check | PASS | hostexec_e91a9d39ab1741f8aea13fcd71cf79df |
| Source DB 不变 / SQL 写入拒绝 | PASS，fixture bytes + logical dump 相同，DELETE 被 query_only 拒绝 | 定向测试 |
| HTTP auth/method/route/bounds | PASS，含 actual loopback fixture HTTP | 定向测试 |
| Exact result / prior execution isolation | PASS，旧 PASS、错 turn 不投影为当前 PASS | 定向测试 |
| 无真实 denominator | PASS，progress_percent=null，phases=[] | 定向测试 + schema 负例 |
| Swift/macOS 编译、MenuBarExtra UI | 未验证，P620 swift executable 不存在 | 未运行 |
| 真实 Tailscale/TLS/ACL、credential rotate/revoke | 未验证，未部署 | 未运行 |
| /data/artifacts 指定目录写入 | BLOCKED，TARGET_NOT_REGISTERED | Host executor 拒绝 |

开发过程中保留了失败 evidence：新增测试插入时的缩进错误；测试试图修改 append-only fixture
事件被拒绝；未知 event kind fixture 不符合 V1 observed 命名约束被拒绝。随后修复了测试构造，
未放宽 ledger 约束。以上最终通过的结果替代这些开发失败作为最终验证结论；失败记录未删除。
unittest 输出中的预期 dispatch/identity failure 日志属于负向 fixture，最终 suite 为 OK。

## Git 集成与远端证据 — PASS

Canonical origin：git@github.com:Pvxlabs/CLINX.git，branch：main。
实现提交：6ffcb0d56bf74f7539d8dc8403b862b24d015519
基线提交：0138356。

2026-09-30 17:58:57 UTC 已核验：
- push_current_branch：成功推送 main（hostexec_414bb2b1eb774bf288073a2afd3b1246）。
- git ls-remote origin refs/heads/main：与上述实现 SHA 完全一致
  （hostexec_57658039812b4386a9ec30aeb451ee43）。
- git status --porcelain=v1：空，clean
  （hostexec_3658266b4dbe451297d0d2c2ad4a69a7）。
- git rev-list --left-right --count HEAD...origin/main：0 0
  （hostexec_5ddf62c515e248bc859ba628962d9486）。

本报告在实现提交之后以文档提交加入；报告提交不改变上述经验证的代码。
报告提交后的最终远端 SHA / clean 0/0 另由本次执行结尾的 Host readback 与
CLINX_EXECUTION_RESULT 记录，避免在文件内部自引用自己的 commit hash。

## 安全与语义边界

P620 是唯一 authority；Monitor 不构造 TaskRegistry、不调用 get_status/reconciliation、
provider、host operations、shell/log/filesystem API。没有 Start/Cancel/Retry/Approve/Deploy。
Bearer 持有者只能通过 GET 读取被允许的字段；execution mutation classes 只作展示。
Mac 没有执行权限。测试只创建隔离 fixture 数据库和随机 loopback 测试端口。

模型 effort 不是 hidden reasoning。没有 raw terminal、argv、stdout/stderr、
prompt、conversation binding 或 raw_result。changed_files 路径被省略；artifact registry
不存在时 artifacts=[]，不扫描 filesystem。PASS 是匹配 task/execution/turn 的结构化结果，
不代表部署/生产 UI/真实交易验收。

当前运行 ORION task：没有调用其 live registry/status/provider/reconciliation 或 mutation；
没有读取它的 prompt/log/output，也没有改变它的 execution semantics。当前执行工具正常
持久化的是本次 Monitor task 的 host-operation evidence，不是 ORION task 操作。

## 阻塞 — 必须保留 BLOCKED

请求写入 /data/artifacts/clinx-monitor-spec-20261001/ 时，已绑定的 Host executor 返回：

TARGET_NOT_REGISTERED: development_command path is outside the registered project

当前 execution 无法在该目录生成 FINAL_REPORT.md 和 CLAUDE_CODE_HANDOFF.md；
文件已在仓库 docs/monitor/ 中完整提供。没有绕过 path guard，也没有创建嵌套 execution。
需要外层 operator 正式绑定该目标后导出这两个文件，并回读/核对 SHA-256。
指定目录导出完成之前，不得将总体任务标为 COMPLETED/PASS。

## 风险和未验证范围

- Mac reference 尚未编译；真实 UI、Keychain、通知、深浅色、辅助功能、低功耗均未验收。
- 未部署网络服务；Tailscale Serve/ACL/TLS/OS 只读权限和真实 rotate/revoke 尚未验证。
- 可选 ledger coverage 永远是 PARTIAL/UNAVAILABLE；没有完整历史保证。
- Cursor 仅检测高水位回退，不能识别所有 DB replacement；offset 分页有并发移动风险。
- codex_running 是持久化标志，health 的 authority_liveness 明确 UNKNOWN。
- Free text 裁剪与常见 secret pattern 清理不是通用 DLP；所有 bearer 持有者可读取可见任务，
  无 per-project/user ACL。生产者不得在 title/result 写入敏感内容。
- SQL adapter 与既有 schema 耦合；未来 migration 必须跑 observer tests，不兼容时 503。
- 对 phases、progress 与 artifact metadata 的未来扩展必须先建立 canonical 持久化事实来源。
