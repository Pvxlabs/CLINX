# Provider dynamic tool result delivery RCA — 2026-10-01

## 范围与结论

基线：`031269cc48a62b5a61704971c0be31548e075e11`。本次只修复 Provider
dynamic tool 的连接归属、结果关联和执行后交付失败语义。未修改 Monitor、
QEX、DRS、approval 或 capability authority；未操作 ORION 或生产系统。

`PROVIDER_DYNAMIC_TOOL_DELIVERY=PASS`：正式 MCP prepare → start → HostExecutor
→ 真正的 Codex Provider → worker → PASS / WRITTEN 已验证。
故障验收中的 BLOCKED 是预期的安全结果，不是命令未执行。

机器可读证据：[PROVIDER_TOOL_DELIVERY_EVIDENCE_20261001.json](PROVIDER_TOOL_DELIVERY_EVIDENCE_20261001.json)。
时间戳保留原始 UTC / Unix epoch。凭据及实时命令输出不进入新增交付 ledger。

## 根因和证据边界

此前“Host exit 0 后 namespace lookup 失败”的叙述不符合事件时间线：

| 案例 | Host 开始 UTC | Provider 拒绝 UTC | Host 完成 UTC |
| --- | --- | --- | --- |
| push，`hostexec_56e8cd347c8b47e19ade76e6f7f16de7` | 00:11:56.758868 | 00:11:56.802 | 00:12:02.189651 |
| ls-remote，`hostexec_17a40e669fd74af784a04bdb35bed365` | 00:15:13.882071 | 00:15:13.894 | 00:15:19.006733 |

在新的、只执行 sleep + print 的诊断中，独立被动 observer 与 Host owner 同时收到
request `7137` / call `exec-42083369-71e3-421c-abc8-f7c5b3f3cd1e`。
observer 不回复，但 Provider 约 16 ms 内结束该动态工具并报告失败，Host 约 4 秒后成功。
由此确认共享 app-server 会把 request 广播到订阅连接，拒绝响应在 Host 结果产生前已被接受。

旧 CLINX client 对没有 handler 或属于其他 thread 的广播也会回复 unsupported/error，
缺少请求响应归属约束。共享连接上的非 owner 响应可以抢先结束真正 owner 的工具调用。
具体哪一个外部客户端发出了两次历史 `Unsupported dynamic tool namespace: clinx`
仍未定位；不声称已经找到其 PID，也没有证据证明命令执行期间发生 namespace 注销。
常驻 MCP catalog 是否过期与此没有已证明的共同根因。

`clinx` 是 Provider thread 上的 dynamic tool namespace；`clinx_host_operation` 是其工具名。
它们不是 MCP server 名称、worker 路由或 Host execution authority。
response 使用原始 JSON-RPC request ID，不重新按 namespace 寻找结果接收者。

## 修复

1. `[app_server].local_socket` 明确选择 CLINX 专用 Provider Unix socket。
   未配置时兼容原连接方式；配置后 socket 不可用即失败，不回退共享 daemon。
2. `systemd/clinx-provider.service` 使用安装版 Codex 的 `app-server --listen unix://...`；
   client 使用官方 `app-server proxy --sock ...`。socket 为用户私有 0600。
   CLINX 内部非 owner / 无绑定观察连接保持静默，不竞争动态工具响应。
3. ledger 在同一 TaskRegistry SQLite 中原子准入 `(execution_ref, tool_call_id)`。
   同一 callId，或任何未确认交付后的新 callId，都不能再次进入 Host handler。
   新 connection / JSON-RPC ID 不会绕过持久化限制。ledger 不调度或执行命令。
4. 保存 task、execution、Host execution、call、request/response、thread/session/turn、
   client connection、listener、registry generation、client/proxy PID、Provider 版本信息、
   socket inode/创建时间，以及 admission / Host completion / send / ack 时间。
   Provider 进程 PID 和启动时间另在验收证据中关联。
5. socket send 成功不等于交付。必须收到匹配 thread/turn/call/namespace/tool 的
   Provider `item/completed`，且 success=true、内容含精确 Host/execution refs，才记 DELIVERED。
6. 发送前检查 transport、listener、registration generation、endpoint identity。
   生命周期变化确定性失败。连接关闭、回调异常、Host evidence 提交后封装前崩溃，
   都从 Host 表恢复执行事实；Finalizer 禁止从 Host exit 0 或 worker 自报合成交付 PASS。
7. 现存 execution result 保持不可变；不回填历史 BLOCKED 为 PASS。

## 执行语义

| 状态 | 证据与行为 |
| --- | --- |
| `COMMAND_NOT_DISPATCHED` | invocation identity/namespace/generation/authority 在 Host dispatch 前拒绝 |
| `COMMAND_DISPATCHED` | 已准入，尚无确定退出证据；不能推断未执行 |
| `COMMAND_EXECUTION_FAILED` | Host 有非零退出证据；delivery 状态独立记录 |
| `COMMAND_EXECUTED_RESULT_DELIVERY_FAILED` | Host exit 0，交付尚未确认或已被拒绝 |
| `COMMAND_EXECUTED_RESULT_DELIVERED` | Host exit 0 且精确 Provider acknowledgement 成立 |

执行后交付失败的最终结果为 `BLOCKED / RESULT_DELIVERY_FAILED_AFTER_EXECUTION`，
`retry_required=false`。未确定 Host 完成则为 `RESULT_DELIVERY_UNCONFIRMED_AFTER_DISPATCH`。
read-only 命令也保留执行事实；mutation 不自动重跑。
本保证以同一 execution 的持久化 ledger 为边界；人工新建 execution 不是自动 reconciliation，
操作员仍须先比对已提交副作用。本次没有实现跨 execution 的通用业务幂等协议。

## 正式正常交付验收

使用仓库正式 MCP stdio server（`--allow-execute`），未直接调用 HostExecutor 替代执行入口。
prepare/start 沿用现有 project、route、lease、policy 和 result writeback。

- task：`task_240ff6888bee435b93e8cd1838de2bf4`
- 第一次 execution：`exec_02fd90d22b1d4d129925a23a12ed5da2`，PASS / WRITTEN。
- 再次 continuation：`exec_abb5948ac0c3408aa1b9302a057452cd`，PASS / WRITTEN。
- 每次恰好执行 `pwd`、`git status --short`、`git rev-parse HEAD`、幂等 Python print。
- 每次 4 个 Host exit 0 + 4 个 Provider DELIVERED；worker 最终结果引用对应 Host refs。
- 正常验收没有执行 push。

迁移边界：试图在专用 Provider resume 原共享 Provider thread 时，Provider 返回
`already has an active writer`，发生在 Host dispatch 前（execution
`exec_4403a0131d314e4dad5c97c2871ddbe3`）。没有抢占、unload 或重写原 thread。
旧 thread 需通过现有显式 thread migration 流程保留上下文后迁移，不能靠共享连接 fallback。
验收的新 thread 和后续 continuation 均已通过。

## 故障注入与 regression

可复现的真实故障命令：

```bash
CLINX_LIVE_DELIVERY_ACCEPTANCE=1 python3 -m pytest -q -s test_tool_delivery_live.py
```

该测试使用隔离的临时 Git project / TaskRegistry / lease，正式 MCP prepare/start 和真实
Provider。在 Host exit 0、证据落盘、成功 envelope 已生成后，测试局部替换为 namespace
unavailable 响应。没有产品级故障开关，没有中断公共 Provider 或其他任务。

- execution：`exec_19c5d92d90814fa3bb71ef82a5806135`
- Host：`hostexec_371fe2dfa7de49dd8db0ec7bbaaaa2e3`，exit 0。
- `HOST_EXECUTION_COUNT=1`，临时 counter=`1`。
- Provider 真实收到失败响应；最终 `BLOCKED / RESULT_DELIVERY_FAILED_AFTER_EXECUTION`。
- `retry_required=0`；重连身份下的新 callId 被 ledger 拒绝。
- 真实 fault test：1 passed。

| 要求 | regression |
| --- | --- |
| A 正常执行及交付 | `test_success_requires_provider_ack_and_keeps_all_correlation` + 两次真实正常验收 |
| B 执行期间生命周期变化 | `test_registry_replaced_while_host_process_is_running` |
| C dispatch 前 namespace 不存在 | `test_namespace_or_generation_unavailable_before_dispatch` |
| D exit 0 后 registry 不存在 | `test_completed_mutation_cannot_execute_again_after_lifecycle_fault` + 真实 fault |
| E mutation 禁止重跑 | lifecycle fault / 新 connection / 新 request / 新 callId / counter=1 |
| F read-only 交付失败 | `test_rejection_after_execution_is_not_not_executed_or_retryable` |
| G 精确 identity correlation | success / wrong ack / Host commit before envelope crash |
| H stale generation | before-dispatch generation + after-dispatch registry replacement |
| I reconnect 不重复 Host | lifecycle fault 参数化测试 + 真实 fault ledger replay |
| J 历史结果不可变 | `test_historical_blocked_result_is_immutable` + 3 个历史真实结果逐字段比对 |

定向及完整测试入口是仓库已有的 `python3 -m pytest`；未增加替代依赖环境。
完整 suite 默认跳过显式 opt-in 的真实 Provider 测试，该测试已另行真实运行并通过。
最终定向回归：104 passed、27 subtests passed。完整回归：618 passed、102 subtests passed、
1 skipped（上述已经单独运行并通过的真实 Provider opt-in 测试）。
`git diff --check` 和 systemd unit verify 均通过。

保留失败事实：新增历史 fixture 最初遗漏 conversation binding，补全正常绑定后通过；
真实 fault fixture 最初为空 Git history，被 identity guard 在 dispatch 前拒绝；补齐临时
初始 commit 后，首次真实 fault 已达到预期 BLOCKED/count=1，但测试错误地用 `is False`
比较 SQLite 的整数 0。更正断言后在全新隔离 execution 验证通过，没有重试原命令。

## 运行部署边界与剩余风险

本次只安装 CLINX owned Provider unit 和两个依赖 drop-in；确认 activities=0 后重载
CLINX dispatcher/tunnel，使已修复源码生效。公共 Codex daemon、ORION、生产服务均未重启。
`systemd/clinx-provider.conf` 应安装到 `clinx.service.d/` 和 `clinx-tunnel.service.d/`。
使用现有机器凭据，无新 token、无 OAuth、无凭据复制。

历史共享 Provider thread 的跨 endpoint continuation 不自动迁移。Provider 协议属于实验接口，
版本升级需重跑真实交付验收。ACK 缺失时保持 reconciliation，宁可 BLOCKED，不能假报成功。
外部 app connector 在 CLINX tunnel 重载时曾返回 Transport closed；后续已经恢复，
并通过 `clinx_get_status` 回读 continuation 的 PASS / WRITTEN 和四条 DELIVERED。

Monitor 13 个 dirty 文件及三个历史 BLOCKED execution result 均保全；提交必须显式按文件
stage，不能 `git add .`。本次不声称 Monitor macOS runtime、Tailscale/TLS/ACL 或生产验收通过。
