# Air 原会话续接实际验收

本次尚未实现实机原会话续接成功。正式CLINX入口到Air的adoption和prepare通过，start到达Air本机Provider，但原生thread/resume因另一Provider持有writer而拒绝；没有新的turn，未收到本轮nonce回复。SAME_THREAD_CONTINUATION=BLOCKED；FULL_EXECUTION_ACCEPTANCE=未验证。

## 正式调用证据

- 原thread：`01a1020d-adca-7711-80a5-21219bf1cb12`，host `air.local`，cwd `/Users/tinzleung/github/CLINX`。
- adoption：`ADOPTED`，canonical `task_2ea1758adace4ec0affb23dd8ceb21c1`，未创建对话、未启动execution。Air本机项目核验；P620仅保存引用路由。
- 第一次prepare：`prepared_faad5101f5914972ab1326b515c18d5e`；execution `exec_faad5101f5914972ab1326b515c18d5e`。大WebSocket frame短读导致model/list JSON不完整，尚未派发turn。修正精确长度接收后，真实model/list正常。
- 第二次prepare：`prepared_25b9d2dd0df745ffb8d74ccc66022417`；execution `exec_25b9d2dd0df745ffb8d74ccc66022417`。正式start返回`thread/resume failed (code -32600): thread 01a1020d-adca-7711-80a5-21219bf1cb12 already has an active writer`。
- 固定nonce：`4a9e2822982b`；预期标记`CLINX_AIR_RESUME_OK_4a9e2822982b`，未出现。
- 新turn：无。最近原生turn仍为`01a1026d-0f85-7f13-a154-228188ed6f80`、completed；它是旧turn，不能当成本次回复。
- 两次未派发attempt经正式产品maintenance CLI负向对账：native completion早于acquisition，实时Provider仍是同一旧completed turn。canonical Finalizer归为BLOCKED、释放仅该失败attempt的lease，未resume、未cancel、未重放。
- 正式CLINX get_status再次读回第二execution为BLOCKED、`execution_turn_ref=null`、`active_execution=null`、node `air.local`。不以SSH/直接Provider成功替代CLINX入口。

## Provider阻塞证据与边界

`lsof`读取目标thread writer lock：唯一持有者PID 63094、用户tinzleung、桌面内置Codex app-server。该进程也是本修复对话的父Provider。CLINX配置的现有daemon为PID 97196、0.160.0，实时观察目标为notLoaded。原对话未显示不等于writer已释放。

内置Provider仅观察到stdio/匿名socket，未发现正式可连接控制socket。没有删除或改写lock，没有重启或结束任一Provider，没有发起替代对话，没有修改rollout。解除该跨Provider writer冲突需要桌面正式释放目标writer，或提供可连接其持有者的正式Provider入口；当前约束下不能强行执行。

新处理对准确thread/resume writer拒绝返回`NATIVE_ACTIVE_WRITER`、`side_effect=NONE`、BLOCKED，不发送turn/start；仅该失败attempt经单一Finalizer收束。其他超时保持UNKNOWN并先对账。重复拒绝请求不重试Provider。

## 变更与验证范围

可信selector和中心引用路由在thread_identity、m9_integration、node_protocol；Air canonical适配器在node_execution_adapter、node_runtime、node_service_entrypoint。node_centre_entrypoint使用既有正式share/configure机制，明确限定节点、原thread、新建项目及可取消项目。mcp_server并行维护不恢复现有执行。app_server修正精确长度frame读取；bridge/native_provider/task_registry实现严格未派发对账与明确writer拒绝。Monitor build-app.sh重建签名helper依赖，未原地修改已签名bundle。

隔离回归覆盖真实产品MCP→配对mTLS→canonical适配器，错误host/hostId、scope撤销、writer拒绝、重复请求、迟到receipt、UNKNOWN对账、same-thread及本地任务路径。实机adoption/prepare PASS；实机原会话执行BLOCKED。新任务完成/结果/取消验收未运行，未提前越过M2。

本次最终受影响Python检查：175 passed、16 subtests passed（11个相关测试文件）。Swift检查：72 passed；build-app.sh及codesign严格验证通过。macOS临时目录使用`TMPDIR=/private/tmp`避免既有测试的`/var`与`/private/var`字符串断言差异；未修改或跳过该断言。不是全仓Python审计。

## 交付边界

独立worktree `/Users/tinzleung/.codex/worktrees/air-resume-fix/CLINX`，修复分支`codex/air-resume-fix-20261004`，基线b8a4294。原工作目录及前序分支保持不变；正常推送，没有强推、主线合并或接管其他执行。Linear创建在实施前因UNAUTHORIZED失败，未伪称登记；本SPEC和记录保持实际进度。

P620原MCP与Provider持续保留，候选tunnel使用同一正式profile，旧poller暂停且可SIGCONT回退；候选MCP不扫描旧执行。并行维护尚非永久service切换，须明确记录。旧release、环境/launcher备份及Air原bundle保留。临时授权通过双边正式share恢复只读后，执行仍需再次显式授权。

最终运行代码SHA：`56bcac32125ef79e190419241a0033b1ab7b798d`。Air安装路径`/Applications/CLINX Monitor.app`，helper实际PID 75746；关键Python资源与该源码哈希逐一相同，`codesign --verify --deep --strict`通过。P620实际release `/home/pvxlabs/.local/lib/clinx-control/releases/air-resume-56bcac3`；centre PID 2248985、正式候选MCP PID 2249019。旧MCP PID 1607560和Provider PID 1829828继续运行，旧tunnel poller PID 1607539为暂停状态；canary service active。永久切换不能重启这些既有执行owner。

临时授权恢复：PASS。双边正式share均返回`read_sessions=true / execute_tasks=false`，未修改数据库共享字段。恢复后正式get_status读回第二execution为BLOCKED；get_context以canonical task_ref读回`context_status=AVAILABLE`、`context_source=CODEX_NATIVE_HISTORY`、原thread不变、旧turn仍COMPLETED、本轮标记不存在。重复start返回`SHARING_SCOPE_DENIED / execution_started=false / node_id=air.local`。以execution_ref读context时因没有新turn而CONTEXT_UNAVAILABLE；不能把原历史AVAILABLE作为新execution回复。

代码正常提交并推送；主线合并：未进行。安装：PASS；候选运行激活：PASS；原会话实际续接：BLOCKED；完整专用执行验收：未验证。本记录的后续提交仅补齐验收文档，不改变56bcac3运行代码。

`ORION_MUTATION=NONE`。不暴露凭据、配对秘密或完整原历史。

## 剩余实机验收的固定交接

不再要求关闭原对话窗口。需由用户在桌面内其他任务全部空闲后完全退出桌面App，或由产品正式释放仅该thread writer；不能由本修复Agent结束自己的Provider。外层执行者先只读确认目标writer不再被原桌面Provider持有、原turn未活动，再使用同一正式CLINX服务端验收。

已有临时任务scope仍限定原thread及专用项目，只需通过双边既有share机制再次显式授予execute_tasks；完成后恢复只读。adoption应返回同一task_ref；新prepare使用这个canonical task_ref和host air.local。不要重复启动已收束的两条prepared ref，其请求ledger应继续返回原拒绝/对账结果。新prepare并不创建替代thread。

固定任务原文：

> 本 turn 只执行一次 CLINX 续接验收，不继续此前开发计划。只读获取当前 hostname、当前用户与工作目录，并返回标记 CLINX_AIR_RESUME_OK_4a9e2822982b。不要修改文件、运行项目测试、提交、安装、重启服务，也不要调用 CLINX 的控制接口。使用中文报告实际结果。

start只能传该新prepare的正式引用和approved=true；随后get_status/get_context核验execution、原thread、新turn、实际命令输出和nonce终态。此交接仍是待验收步骤，不是PASS证据。
