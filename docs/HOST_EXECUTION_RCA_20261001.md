# CLINX Host Execution / QEX Admission RCA

日期：2026-10-01。范围：CLINX 基础设施诊断、capability 契约最小修复及真实 P620 Host 验收。

## 基线与边界

- Canonical repository：`Pvxlabs/CLINX`；开始时 `main = origin/main = b99dce56243b73d8ba0764360c7eb9e56bf48f47`。
- 注册工作区：`/home/pvxlabs/dev/clinx-pvx1807-remediation`；Git topology 为一个 main worktree。
- 既有 Monitor Phase 2 的 13 个文件逐个 SHA-256 保全，不改动、不 stage、不提交。
- 不部署、不重启服务、不修改 approval、不干预 ORION 或其他运行任务。
- 以下 Host evidence 全部属于本次自有 task `task_0e00f13f80bc48da92fa3ae3a9d410db`。

## 实际 authority 与执行路径

1. CLINX MCP `clinx_prepare_execution(approved=true)` 解析规范 policy，验证项目和 capability 探测状态，持久化有完整性校验的 prepared execution。
2. `clinx_start_execution` 仅接收 opaque prepared reference 与 approval；dispatcher 取得本任务 execution 和 workspace lease。
3. `bridge._configure_host_turn` 根据 sealed policy 限制动态工具的 capability/operation class；thread、task、execution、route、cwd 由 CLINX 绑定。
4. `app_server.py` 的动态工具 callback 调用 `HostExecutor.execute`；执行前重新检查 policy、route、task、project、operation、target 和 lease。
5. `HostExecutor` 用 `subprocess.Popen(shell=False)`、注册 cwd、受控 environment 在 P620 Host 启动 argv；没有自动 DRS/QEX admission 层。
6. stdout/stderr、hash、exit code、duration、timeout 和 host execution reference 进入持久化 evidence。
7. provider 的 `CLINX_EXECUTION_RESULT` 由 CLINX completion/finalizer 归集，结果回写后释放 lease；启动成功或 Agent 自报不替代 evidence。

另一条路径是 Codex native shell → DRS native hook → `drs route` → `drs exec` → pressure/QEX → process。它与 CLINX 动态 Host callback 的权限环境不同。

## 根因

### Approval

此前 `MCP tool call requires approval, but approval policy is never` 来自 Remote Desktop Commander 的 `start_process` 调用；不能归因于 CLINX 动态 Host 工具。

`bridge.toml [codex] approval="never"` 经 `bridge.py` 传入 provider turn；桌面 Codex 配置也为 `never`。CLINX prepare/start approval 批准的是 sealed task execution，不会授权外部 MCP transport，也不会覆盖 provider 的权限策略。本次保持全部 approval 配置不变，通过正式 CLINX 动态 Host 工具执行 `pwd` 等命令成功。

### QEX / DRS

- sandbox 的早期执行遇到 state directory 写权限限制；当前 DRS state fallback 可写后仍无法连接 coordinator。
- 本次 sandbox 对实际 socket `/home/pvxlabs/.local/state/qex/run/s` 的 AF_UNIX connect 返回 `EPERM`。创建 AF_UNIX socket 成功不代表 connect 被允许。
- 同一会话的 DRS evidence 在 2026-09-30 23:44 UTC 记录 `COORDINATOR_UNAVAILABLE`、exit 124、`qex_job_id=null`、`qex_terminal=false`：测试未提交成功，不能声称 pytest 失败。
- 真实 Host route 的 QEX job `e059c2af-f262-41f1-8885-a0b2fa0b91f8` 已完成，`attempts=1`、exit 0、无 blocker/lock/retry。没有本次 Host admission deadlock、stale execution、queue starvation 或 identity mismatch 的证据。
- DRS 现有 coordinator ceiling 为 15 秒；无 job id 时失败关闭，已接受 job 的等待超时不会重新提交。本次不提高 timeout，不重启 coordinator，不改 DRS 配置或实现。
- 普通 CLINX Host repository tests 直接执行正式 Python 入口；无须增加 DRS hard gate。显式选择 DRS 时仍走其既有 admission，不降级跳过。
- `drs route -- python3 -m unittest ...` 在本机分类为 DIRECT；本次随后使用识别为 TEST/DRS_EXEC 的 `python3 -m pytest` 单独验证真实 QEX。

### Capability contract

旧 discovery 的 `capabilities` 是小写探测标签，prepare 使用 `HOST_CAPABILITIES`。`filesystem`、`host_process`、`development_command` 不是规范 capability；`git` 本来就能规范化为 `GIT`，不是失败根因。

最小修复：

- `execution_policy.HOST_CAPABILITY_PROBES` 是 discovery/prepare 共用映射。
- discovery 保留旧字段，新增 `request_contract`，公开规范 capability、operation class 和 probe mapping。
- 新增 `capabilities_kind=INFORMATIONAL_LOCAL_PROBES`、`available_meaning=CONFIGURATION_ENABLED`、`runtime_health=NOT_PROBED`，避免把 binary/config 探测当作真实运行健康。
- MCP prepare schema 的说明与 examples 从同一常量生成；保留原 validator 的大小写和空白规范化兼容性，不增加 alias 或权限 fallback。
- capability 词汇被接受不保证任意 operation 存在，也不授予 target/production 权限。

规范 capability：`LOCAL_HOST_PROCESS, SYSTEMD_USER, OUTBOUND_NETWORK, SSH, HOST_FILESYSTEM, AWS_CLI, CLOUDFLARE_CLI, CLOUD_API, GIT, DOCKER, POSTGRES, TAILSCALE`。

正常 repository inspection/test/build/write/commit 使用现有 `DEVELOPMENT_MUTATION` + `development_command`。只读结构化命令可用 `READ_ONLY_HOST`。non-force push 使用既有 `GIT / push_current_branch`，固定 origin 与当前分支。`PRODUCTION_MUTATION` 要求显式 intent；`BUSINESS_ACTION` 被拒绝；privileged/external 命令与 shell evaluation 边界保持不变。

## 真实 Host 验收 evidence

| 检查 | Host execution reference | 结果 |
| --- | --- | --- |
| pwd / registered cwd | `hostexec_7425c65610d64ff6b34538393c58f97d` | exit 0 |
| git status | `hostexec_39377b22d5104d98bf6e15e78996ae8f` | exit 0，仅既有 Monitor dirty |
| git rev-parse HEAD | `hostexec_5da9a42f89364d73a4e01874b80937a6` | 基线 b99dce56 |
| python3 --version | `hostexec_89583cc4ad86403698925ab5bed07382` | Python 3.12.3 |
| 此前 2 HTTP + 3 MCP child tests | `hostexec_834cb2153035468c9315302a874a5363` | 5 tests，OK |
| 未改动源码时完整 pytest | `hostexec_81abb91f645647e292fc2c98bb64b795` | 593 passed，75 subtests passed |
| DRS route pytest schema | `hostexec_5e2ff723f8324bd2a6c5cc69fbba4e88` | 3 passed，9 subtests passed |
| 真实 QEX job status 回读 | `hostexec_b85e5cd07f1c46ada09c429077191652` | completed，attempts 1，exit 0 |
| DRS 官方 fake-QEX liveness regression | `hostexec_67e299b6669747e3903ec04b580bcd4d` | 33 cases PASS |
| 修复后定向 pytest | `hostexec_52674f6bf6fb4f6fbcdd8e88abf1615a` | 135 passed，42 subtests passed |
| 修复后完整 pytest | `hostexec_09139343ba8b46eab77ba56f15f9426a` | 600 passed，102 subtests passed |

真实 QEX 对应 DRS session `f73d5948-3adf-4446-b750-515f424449cf`、task `4ec57ad0-3629-463b-85ba-60855dfc937b`；00:01:20–00:01:21 UTC 的 command_routed → qex_submitted → qex_finished → task_finished 链路一致。QEX status 回读证实 `completed`，不是只依赖 submitted event。

`exec_ba9b93df284e4f3bbb503e3c63267177` 与 `exec_b8f878a066e049a4aaf69ad7abe8ce3a` 的结果均为 PASS / COMPLETED / WRITTEN。中间一次 `exec_01434c994afa4030bf45154fc4f52559` 因 stdout 缺少 job id 自报 BLOCKED；保留旧结果，随后通过持久化 telemetry 与 QEX status 补齐证据，没有改写历史 execution。

## A–I regression 对应

| 要求 | Coverage |
| --- | --- |
| A authorized read-only | 新增 real pwd fixture；上述真实 Host pwd |
| B normal development test | 新增临时仓库 unittest Host fixture；真实完整 pytest |
| C unauthorized denied | 既有 unsealed capability、route mismatch、lease/identity tests |
| D production intent=false | 既有 policy intent rejection；新增 development policy 不能执行 production class |
| E unsupported capability | 新增 prepare 对 probe labels、operation name、unknown 的确定性拒绝 |
| F discovery/prepare 一致 | 全部 12 capability 经 discovery vocabulary → prepare → sealed registry round trip；MCP schema 共用词汇 |
| G QEX unavailable bounded | DRS `scripts/test-qex-liveness.sh` 的 coordinator failure/accepted timeout/unknown terminal/no duplicate submit fixtures，33 checks |
| H DRS 非必要时不阻止 tests | 新增 drs/qex 均不可用的 fixture，真实 unittest 仍完成，未调用 admission |
| I evidence/writeback | 既有 host evidence、completion/finalizer tests；本次真实 get_status 回读 WRITTEN |

## 限制与交接

- 33 项 QEX liveness 是隔离 fake-QEX 故障注入；真实 QEX PASS 依据独立 job/status/telemetry。
- 新 discovery 契约经过源码和新进程测试；没有重启常驻 CLINX MCP 服务，不宣称远端常驻 catalog 已热更新。
- 本次不会解除 Codex sandbox 限制或修复外部 Remote Desktop MCP approval；使用明确授权且原本可用的 CLINX Host surface。
- 只能声明 infrastructure 文件提交后无残留；总 worktree 必须保留原 Monitor dirty，不能同时声称总 worktree clean。
- 下一任务再进行 Monitor canonical integration；Swift/macOS/runtime acceptance 不属于本次验收。
