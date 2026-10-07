# 授权与派发链路最小修复及真实闭环验收

截至 2026-10-07，本次代码、运行制品激活、真实 MCP→Provider 闭环及复用既有授权的 ChatGPT 实际入口验收均已通过。用户提交了 ChatGPT 本轮入口验收记录，本轮又按准确 execution_ref 核对 CLINX 持久化执行与结果。因此整体 `FINAL_STATUS=PASS`，本次无剩余验收动作。历史拒绝原因独立保留 UNKNOWN；ChatGPT 新增权限的 apply 没有在这轮重测。

```ini
FINAL_STATUS=PASS
ORIGINAL_REJECTION_ROOT_CAUSE=UNKNOWN
IMPLEMENTATION_STATUS=PASS
RUNTIME_ACTIVATION_STATUS=PASS
MCP_PROVIDER_ROUNDTRIP=PASS
CHATGPT_ENTRY_ROUNDTRIP=PASS
ORION_MUTATION=NONE
CLINX_UI_UX_CHANGED=NO
```

## 原始失败的证据边界

原目标为 `task_03c5a033b64e462e87127ba122e8e97b`，Provider thread 为 `01a1147e-8a0e-7760-8b27-529b5a1ac88f`，准备授权请求为 `reauth_8dca5669ba0e4d7082f876a801dae997`。当前精确读回显示请求 EXPIRED，policy version 0，applied policy version null，已应用 reauthorization 数 0，execution 数 0，task QUEUED。请求准备于 04:13:11 UTC，15 分钟后过期；其拟授权范围只有主机只读和开发变更，没有生产发布 scope。这些事实证明未应用、未启动和发布范围缺口，不证明外层审批拒绝的原因。

前序诊断原生线程的 THREAD_UNBOUND 只表示未找到其 canonical task 绑定，不能用于否定上述原目标任务。完整 URI 应传 `codex_uri`；`thread_id` 只接受 UUID；`clinx_get_status` 不接受 `max_bytes`。这些参数错误与原始授权失败分开记录。没有恢复到原始拒绝回执，不把所有失败概括为安全拦截，也不声称 ChatGPT 审批已经修复。

当前运行检查还确认原 MCP 和持久 owner 使用不同版本，MCP 制品缺少 owner 转发。此问题是本次独立修复的运行缺口，与历史外层拒绝原因没有已证实的因果关系。

## 实际修复和源码

主修复提交 `2435377444b0189cad56f5d27a0071f14586d187`；真实运行验收发现新任务 prepared 读回引用不存在的 ProjectMapping.cwd，补充修复提交 `cf07b52eb6c88b1bb407dfefabae7676ffa433d4`。代码与验收文档已提交至 main；远端同步结果见 `/data/artifacts/clinx-authority-dispatch-20261007/git-delivery.json`。

- `prepared_requests.py` 和 `clinx_get_prepared_request` 只读既有准备记录，返回准确目标、policy 差异、有效期/失效状态、内容摘要、已应用版本及准确 execution/thread/turn。原 prompt 不返回，只提供 prompt hash。
- `m9_integration.py` 使用结构化 requested_operations 判断现有 policy 是否覆盖，覆盖时复用；缺少范围时返回明确 missing_operations，引导同 task reauthorization。新任务目标读回使用真实 ProjectMapping.repo/project_alias。
- apply/start 支持可选 expected_request_hash/expected_task_ref；apply 在已有 CAS 事务内再次检查，不能覆盖身份、prompt 或 policy。既有幂等和 immutable audit 保留。
- 未知 dispatch 或 Provider 响应丢失不把消费中的请求恢复为 PREPARED。读回稳定 execution_ref 后核对，避免第二次 execution/turn。旧 COMPLETED turn 不作为新 execution 证据。
- `dispatch_errors.py` 补 failure stage/source/code、关联、副作用确定性及恢复条件并过滤敏感文本；`execution_owner.py` 通过真实 owner socket 保留结构化错误，明确 Provider 拒绝与传输不确定。
- 工具目录、严格 schema/catalog 断言及调用说明同步。apply/start 保留真实写操作与 destructiveHint=true，没有放宽错误参数、改写审批标志或增加第二套授权状态机。

## A 层代码与候选制品验证

最终相关回归：180 passed，37 subtests passed；针对已构建候选控制模块：98 passed，6 subtests passed。ruff E4/E7/E9/F 和 git diff --check 通过。覆盖只读无副作用、权限复用和明确缺口、review 内容/目标绑定、expiry/CAS/idempotency、apply 响应丢失读回、start 响应丢失不重派、owner socket 错误转发和脱敏、旧 turn 不误判、已知参数/schema 错误继续拒绝。

早期失败已保留，不记 PASS：catalog 顺序不一致；仓库未配置 discovery 环境；候选 harness 误载旧测试及缺少原 config；新任务真实 prepared 读回缺陷。最终测试使用仓库声明的 discovery 依赖，复用原 MCP 的既有 .venv-node，未修改依赖版本或全局 Python。

## 制品激活与实际身份

当前制品为 `/home/pvxlabs/.local/lib/clinx-control/releases/authority-dispatch-20261007-cf07b52eb6c8`。控制源码来自 cf07b52；源树 `3bab6f3103dc75c1775603174691d1d6ca1e2791`。制品继承当前线程路由 MCP baseline `27bec157735d261a789c0c248f4125d807f04b02`，覆盖本次控制代码及持久 owner 必要依赖。完整文件来源/hash manifest 见 runtime-build.json；这是明确组成的最小后继制品，不宣称等于完整 Git tree，也没有切换 UI/UX 或网络观察服务。

实际 owner PID 3152482，正式 tunnel MCP child PID 3152611，均载入上述制品；正式连接器返回的 loaded source_root/PID 与进程 argv、cgroup 一致。真实 tools/list 导出新 getter、review 字段和原 annotations。当前隧道 healthz=200/live、readyz=200/ready。

激活前及终态预检：无 active execution、lease、CLAIMED/UNKNOWN owner、运行中 Host 调用和待处理 completion。仅切换执行 owner 与 MCP tunnel。clinx.service PID 666987、node centre PID 2323847、原有 Provider peer PID 666683/673513 均保持不变。

激活过程中存在脚本读回失败和回退，不能抹去：初次只检查进程主线程 children，未覆盖其他线程的子进程；旧固定 health port 在重启后失效；健康文本被错误按 JSON 解析。一次原配置回退还暴露原 MCP 指针不含 --execution-owner，无法单靠恢复 env 重启原 owner。最终脚本枚举所有线程 children、按当前 tunnel PID 找动态健康端口并检查实际文本响应；回退按角色固定原 loaded 制品。以上属于本次激活/验收问题，不作为原始拒绝根因。

旧制品与权限受限的配置/launcher 备份保留在 `/home/pvxlabs/.local/state/clinx/qualification/authority-dispatch-20261007/activation-rollback/`。回退脚本 `runtime_ops.py rollback` 会先重新检查零 active ownership，再停止 tunnel、drain owner，恢复旧 MCP 配置并将 owner 固定回原 `host-delivery-809fb6b1`；不覆盖数据库、历史回执或清理旧制品。当前新制品保持激活，未执行收口后的回退。

## B 层真实 MCP→Provider→Host 闭环

使用正式 launch-mcp 的 JSON-RPC stdio MCP 客户端，连接已激活运行制品和 canonical 数据库，经持久 owner 到已有 Provider。首次仅读取 README 第一行，建立专用 task；随后在同一 task 应用仅 HOST_FILESYSTEM/path_read/READ_ONLY_HOST、network disabled 的 policy version 1。之后直接复用已有授权，无重复任务或 ORION 授权。

| 身份/证据 | 实际值 |
| --- | --- |
| 专用 task | task_4ce2a4182bf34b2ab7c0f6fae991d51b |
| Host execution | exec_e9dbc314552c4ee390c6f0c4ccd52031 |
| Provider thread | 01a114bb-fd95-7680-b223-fb637cb159f8 |
| Provider turn | 01a114bc-015b-7172-9ac7-c405daa05060 |
| Host receipt | hostexec_a191ec8bb83a4c47b63dbfc9497703d9 |
| 操作 | HOST_FILESYSTEM/path_read，README.md，lines=1 |
| owner/client_pid | 3152482 |
| Host 次数/结果 | 1；SUCCEEDED / DELIVERED；exit_code=0；# CLINX |
| Provider 终态 | COMPLETED / PASS；CHANGED_FILES=NONE |

真实重复提交已确认终态的同一 prepared 请求，返回原 dispatch 回执，Host ledger 仍只有一次，未产生新 turn。task 仍 ACTIVE 与 execution 已 COMPLETED 分开处理；错误使用 reopen 的真实拒绝已保留，后续依据读回使用 continue，没有改数据库状态。

## C 层真实入口验收与收口

ChatGPT 入口的调用与真实审批由用户在当前 ChatGPT 对话完成，并将验收记录转交本线程；本轮 Codex 随后通过正式 CLINX 工具按准确 execution_ref 独立读回。入口来源证据是用户提交的 ChatGPT 验收记录，执行、Host 交付和终态证据来自 CLINX 持久化记录。

本轮实际消费 `prepared_ffce98d79e1841299b4df7e4729a79d7`，摘要 `ca04d19e5ce0d74a5e46923bd049a96a7ebf7b8052daac28de25c33459f7da02`，task `task_4ce2a4182bf34b2ab7c0f6fae991d51b`，execution `exec_ffce98d79e1841299b4df7e4729a79d7`，turn `01a114da-0cd3-7580-a87f-770023352ad2`。该 turn 与先前 B 的 turn 不同，不能混用旧回合证明本轮。

CLINX 独立读回确认仅一次 HOST_FILESYSTEM/path_read，Host 回执 `hostexec_31597b9ff5ad403db5f90fd6750d63fe`，05:33:22 UTC 读取 README 第一行 `# CLINX`，exit_code=0；call_state=SUCCEEDED，delivery_state=DELIVERED，reconciliation_required=false。05:33:25 UTC 收到本轮结果，execution_state=COMPLETED，STATUS=PASS，CHANGED_FILES=NONE，BLOCKERS=NONE，writeback_state=WRITTEN。Host delivery 身份中的 task/thread/turn 和 owner PID 3152482 与本轮关联一致，运行制品为 authority-dispatch-20261007-cf07b52eb6c8。

本轮复用现有 policy_version=1，未重新授权或扩大范围。C=PASS 仅覆盖“复用既有授权，从 ChatGPT 实际入口启动并完成这个受限任务”；未从 ChatGPT 重新验证新增权限 apply，不保证所有未来权限申请都会获准。原始拒绝的具体原因仍为 UNKNOWN。A/B/C 当前均已满足本次收口要求，无需再启动请求或开展新一轮授权改造。

原 `/data/artifacts/clinx-authority-dispatch-20261007/CHATGPT_ENTRY_VALIDATION_ZH.md` 现作为历史验收步骤保留，请勿重跑已消费请求。新验收与独立读回保存为 C-chatgpt-entry-acceptance.json、C-canonical-terminal-readback.json 和 C-canonical-identity-readback.json；早期 BLOCKED、未启动快照保留其采样时的含义。

完整 A/B/C 回执、RPC 记录、manifest、健康读回、激活问题和回退脚本保存在 `/data/artifacts/clinx-authority-dispatch-20261007/`。ORION 原目标没有应用权限或执行；本次没有 ORION 代码修改、发布、fan-out 修复或交易动作，CLINX UI/UX 没有修改。
