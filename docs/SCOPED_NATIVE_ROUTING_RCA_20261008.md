# ChatGPT → CLINX 原线程关联失败排查

日期：2026-10-08（Asia/Shanghai）。目标线程：`01a115d9-08fd-70f0-bade-dc5301bb2b15`，标题「编写轻量实时 fan-out SPEC」，Desktop host ID 为 `remote-ssh-discovered:p620`。

## 结论与证据边界

已在当前公开 CLINX MCP 入口复现一个确定的路由缺陷：同一线程不带 `project` 能读取；带 `project=ORION` 时，节点记录新鲜返回 `NODE_EXECUTION_UNAVAILABLE`，超过 120 秒则返回 `NODE_OFFLINE`。关联的前置读取使用这条路径，所以能产生用户报告的 `adoption_status=BLOCKED`、`control_transferred=false`。

原始 ChatGPT 关联调用的完整参数和 request ID 没有取得；不将本轮重现参数冒充原始调用记录。已证明的是当前服务上的同线程失败路径，以及真实 MCP 构造器下关联操作的同错误复现。

P620 服务仍存活，普通只读接口能取得原线程。原线程没有 CLINX binding，不能把 `THREAD_UNBOUND` 或 CLINX `execution_state=UNKNOWN` 解释为原生 Codex 执行失败。真实 owner 的只读观察为 `active`。

## 根因

1. `ClinxIntegration.adopt_conversation` 使用 `ThreadIdentityReader.read` 验证精确线程和可选项目。
2. `ThreadIdentityReader.read` 在有 `project` 或 `execution_ref` 时转入 `NodeRouter.execute(operation='execution.context')`。这个 operation 实际是只读上下文查询。
3. 线上 `build_server` 只给本地节点挂接普通 session reader，没有上下文 handler 和 executor callback。因此 fresh 节点也会返回 `NODE_EXECUTION_UNAVAILABLE`。
4. 此路由在查找 handler 之前无条件要求 registry freshness。本地进程内 reader 空闲期间没有网络心跳，120 秒后先返回 `NODE_OFFLINE`，掩盖后面的 handler 缺失。
5. 先前的普通本地读取修复只覆盖 `NodeRouter.read`，未覆盖这个带范围限定的上下文路径。

实际服务 MCP PID 为 `3152611`，所属 tunnel PID 为 `3152587`，代码路径为：

`/home/pvxlabs/.local/lib/clinx-control/releases/authority-dispatch-20261007-cf07b52eb6c8/`

该运行目录的 `mcp_server.py`、`node_protocol.py`、`thread_identity.py`、`m9_integration.py` SHA-256 与本轮基线 main 中相同文件逐项一致，确认缺陷存在于正在提供请求的版本。

## 最小补丁

- `mcp_server.py`：为本地 NodeService 注册只读 `execution.context` handler，转发精确线程、项目、执行引用和分页限制给既有原生 reader；发布对应 capability，挂接受既有 SharingScope 约束的 callback。
- `node_protocol.py`：仅允许有本地 reader/executor 且没有远程 RPC transport 的只读 context/status 操作在 idle 后实际尝试读取；成功后 touch，异常或结构化错误不更新时间。
- 保留 `execute_tasks=False`、远程 freshness、撤销授权、项目和 execution_ref 校验。写操作没有 freshness 豁免。

工作区：`/home/pvxlabs/dev/clinx-scoped-native-routing-20261008`

分支：`codex/scoped-native-routing-20261008`

基线：`9259029663da0512b113fbc6380d355fdec9ec7b`。首次排查交付时补丁未提交、未发布，原 main 工作区保持干净。后续用户已授权提交、合并到 main 并在合并后的代码上重新验证；线上发布不包含在该指令中。

## 验证

新增测试在改代码前产生 4 个预期失败，分别覆盖 fresh 缺 handler、idle 判离线、带项目关联失败和 idle 后真实读取未执行。修复后相关 7 个测试文件共 **151 passed, 13 subtests passed**。

回归覆盖真实 `build_server` + MCP `tools/call`、活动线程重复关联、零 execution/history/lease/checkpoint 创建、错误项目/执行引用拒绝、撤销授权拒绝、远程离线拒绝、写操作不豁免、失败读取不制造存活证据。首次扩展回归有一个测试断言错误：撤销后 host alias 先被 resolver 排除，返回 `UNKNOWN_THREAD_HOST`；改用精确 node_id 检验 `SHARING_SCOPE_DENIED` 后通过。初次日志保留。

还用候选代码、临时隔离的 canonical DB 副本和真实原线程 native history/provider 完成只读验证。每次都先将隔离节点记录置为过期，再带 `project=ORION` 调用 `clinx_get_status` 和 `clinx_get_context`，均返回：

```text
lookup_status=THREAD_UNBOUND
context_status=AVAILABLE
node_id=p620
read_only=true
provider_state=active
node_fresh_after_read=true
```

该验证是候选代码读取真实线程的证据，不是线上修复或纠偏投递成功。生产 canonical DB 没有用于候选写入；临时副本已由 TemporaryDirectory 清理。

只读验证前后，线上 `tasks`、`conversation_bindings`、`executions`、`execution_history`、`execution_results`、`worktree_leases`、`host_executions` 的行数和逻辑 SHA-256 一致。Provider、execution owner、tunnel、bridge、node centre PID 均保持不变。`git diff --check` 通过。

证据目录：`/home/pvxlabs/.local/state/clinx/maintenance/scoped-native-routing-20261008/`

- `live-evidence.json`：公开 MCP 两种失败、普通读取成功、真实 owner 和先前纠偏回复。
- `candidate-real-native-read.json`：候选模块绝对路径、源码 SHA-256、真实线程只读结果。
- `targeted-tests-initial.txt` / `targeted-tests-final.txt`：保留初次断言失败及最终结果。
- `canonical-before.json` / `canonical-after.json`：只读验证前后的 canonical 表哈希。

## 纠偏与执行能力的边界

原线程历史显示，2026-10-08 01:57:23（北京时间）已有 owner 回复“收到，本条替代此前的持续修复要求”，并说明停止追加修改、让已启动负载收尾、保存 `CORRECTION_REVIEW.md` 后停止。这证明那条先前纠偏已经到达，不能归功于本轮 CLINX 排查。

`control_transferred=false` 是观察关联的正常合同，即使 `ADOPTED` 也如此。修复关联不等于新增在运行中 steer 的能力。既有 `select_writer_client` 会对活动 owner 拒绝 continuation，错误为 `NATIVE_ACTIVE_OWNER`；公开 CLINX catalog 和本轮可用 Codex app 工具都没有对应的原线程消息投递/steer 操作。

本轮没有向原线程重复发指令，也未 interrupt/resume/start、创建新线程或另起执行。线上带项目路由仍使用旧实现，需后续受控发布该补丁才能宣称线上修复完成。

```text
REPRODUCED_ROUTING_RCA=PASS
ORIGINAL_CALL_ARGUMENTS=NOT_CAPTURED
LOCAL_FIX_AND_REGRESSION=PASS
CANDIDATE_REAL_NATIVE_READ=PASS
LIVE_FIX=NOT_DEPLOYED
NEW_INSTRUCTION_DELIVERY=NOT_EXECUTED
ORIGINAL_THREAD_CONTROL_TRANSFERRED=false
CANONICAL_STATE_MUTATION=NONE
SERVICE_RESTART=NONE
```
