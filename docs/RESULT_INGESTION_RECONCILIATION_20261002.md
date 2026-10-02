# 结果收取分类与同 execution 无重跑对账

本轮验收范围为 CLINX 源码实现、隔离回归和正常 Git 交付。真实 Provider E2E、共享运行时激活均为 NOT_RUN，交由本 execution 正常结束后的外层完成。ORION_MUTATION=NONE，ORION_REAUTHORIZATION=NOT_RUN，ORION_TASK_REPLAY=NOT_RUN。没有更改案例原 task/execution/result/lease/policy/Linear，也没有重做 ORION 工程验证。

## 实现与兼容边界

收取优先读取精确 thread/turn 的 final agent item，保留 message id、phase、可获得的创建时间、原始 UTF-8 SHA256、字节数、脱敏有界原文和具体解析错误。只收 final 消息，不收 commentary、reasoning 或 tool 输出。支持只有 terminal-turn API 的旧 Provider 单一 agent 消息表示；原生 task_complete 后备读取必须同时匹配 session_meta.id 和 turn_id。新对账入口只接受 Provider 精确 final 重读，不接受调用者提供结果文本。

解析继续接受旧的单块多行/折叠表示及已有严格外部 JSON 格式；拒绝重复块、重复字段、不完整字段、PASS/BLOCKERS 冲突、PASS/NEXT_STATE 冲突。PASS 的 BLOCKERS 必须精确等于 NONE。统一 managed contract 要求 STATUS 只裁定当前授权范围，下游待办、范围外 NOT_RUN 和未验证事项保留在 SUMMARY/VALIDATION，不新增报告字段或报告平台。

result_ingestions 是同一 registry 内的只增证据表。完整文本先解析，再按现有 Host 脱敏规则保留至约 15,000 字符，超长时保留头尾和摘要；持久化截断与 Provider 明示截断分别记录。Provider 明示截断为 RESULT_TRUNCATED，不能提供成功证明。格式错误对应 result_ingestion failure_stage、具体 failure_code/evidence、retry_required=false；原生 execution 没有 Host receipt 不说明工程失败。Host exit 0 不能代替最终结果。真实 Provider 失败和业务 BLOCKED 保持独立；未知 Host 副作用或投递未确认进入 RECOVERY_REQUIRED，保留租约，禁止重放及由迟到 ACK 自动升级。

## 已接线的维护入口

沿用 bridge.py 的 recover-execution 命令及 ExecutionFinalizer 边界：

```text
python3 bridge.py --config <隔离配置> recover-execution <execution_ref> --reconcile-result
  --expected-task-id <task_id>
  --expected-thread-id <thread_id>
  --expected-turn-id <turn_id>
  --expected-result-sha256 <当前落库 raw_result 的 SHA256>
  --expected-source-sha256 <精确 final 消息原文的 SHA256>
  --expected-message-id <可获得时必须核对的 message id>
```

实际命令将上述参数放在同一命令行；本轮只在 fixture 调用此入口的 dispatcher 路径。CLI 参数已验证，未以生产配置执行。调用前必须从精确来源取得摘要；不接受裸改 registry、重开 turn 或启动竞争 execution。没有可核验的完整最终消息就不能造 PASS。

入口首先核验旧结果和当前 task/thread/turn/worktree；之后走既有 Provider owner 观察与 final 消息读取，再在 BEGIN IMMEDIATE 中重复身份、旧摘要 CAS、新来源摘要、占用和副作用校验。活跃或未知 owner、更晚 execution、共享 worktree 租约、来源冲突、未确认投递均拒绝替换。仅允许 result_ingestion 错误或精确匹配的旧版本合成结果进入对账，正常 PASS、业务失败和真实 Provider 失败不能借此改写。

result_reconciliation_audit 与结果、task/execution 投影的替换在同一事务提交；保存旧 result/execution/task 和新证据，不删历史。相同请求重复返回 ALREADY_APPLIED，审计只有一条。事务中断全部回滚。仍无效的原始消息仍记 BLOCKED 并保留真实解析错误，不改原 BLOCKERS。Linear 只标记既有 writeback 为 PENDING，后续沿用原投影路径；维护入口本身不发送外部消息。

## 验证与证据

证据根：/data/artifacts/clinx-result-ingestion-20261002/source-delivery/。

- before.json：基线 caa915807585d44ecb66c023b321770b798ccdfa 上两项新增回归实际失败，记录 Host 调用和输出摘要。
- regression.txt、validation-command.json：最终隔离回归结果及精确命令。
- regression-worktree-guard-fixture-fail.txt：补充 worktree 校验后暴露旧 fixture 展示 key 不符合 canonical 身份的中间失败，保留供审计；fixture 已在创建 execution 前生成真实 key。
- incident-fixture.json：复用 readonly-rca-w4g7t2qg/native-final.txt 的临时数据库收取验证；只读原样本，不接触原 registry。文件有额外末尾换行，文件 SHA256 与原消息 SHA256 分开记录。
- git-delivery.json、source-manifest.json：提交/远端身份及文件摘要（交付后生成）。

最终关联回归为 490 passed、90 subtests passed；新增结果收取专项为 40 passed。

最小隔离入口为 python3 -m pytest -q test_result_ingestion.py test_native_completion_report.py test_pvx1812_completion.py test_tool_delivery.py。覆盖字段冲突、合法限定范围 PASS、缺标记、不完整/重复冲突、错误 thread/turn、commentary/tool/reasoning 混入、长输出/显式截断、并发 finalization、旧落库错误的幂等对账/审计、并发与事务失败、正常 PASS/BLOCKED、真实失败、未知副作用与迟到 ACK。所有 Provider 为 scripted fixture；不把这些结果计为真实 Provider E2E。

## 外层激活与回退交接

复用现有 control-release 机制（见 CONTROL_PLANE_RECOVERY_20261002.md），不创建新发布器。运行源新增 result_ingestion.py，并更新 bridge.py、m9_integration.py、task_registry.py；一并交付回归测试与本文。registry 启动仅新增两个证据/审计表，无历史数据迁移或自动结果修正。

外层在本 execution 结束后，从已推送提交构造原有 RELEASE.json/文件清单，进行真实隔离 Provider 验收，至少验证冲突 final 分类、合法范围 PASS、同 execution CAS 对账及无新 turn/Host 调用，再按既有机制安排共享运行时激活与有效进程来源读回。本轮没有重启 MCP、dispatcher、tunnel 或共享 Codex Provider。

用户给定的当前运行时参考为 /home/pvxlabs/.local/lib/clinx-control/releases/execution-authority-3c4976f3c904；本轮没有访问或更改该 release。外层激活前应重新读回并记录实际旧 release 作为回退点。源码基线为 caa915807585d44ecb66c023b321770b798ccdfa。回退只使用既有 release 切换机制，保留新增证据表及对账历史，不恢复整库覆盖后续更新。旧版本存在本次已确认的误归因与合成 PASS 行为，回退不能被视为缺陷消失。

剩余限制：真实 Provider 的来源字段与传输行为仍需隔离 E2E 确认；维护对账要求当前唯一 owner 的新鲜 terminal 证明，历史已卸载且无法证明 owner 的记录会拒绝修正；通用脱敏规则沿用 Host 实现，新增凭据格式应扩充该统一规则。无新最终消息时不提供人工强制 PASS 通道。
