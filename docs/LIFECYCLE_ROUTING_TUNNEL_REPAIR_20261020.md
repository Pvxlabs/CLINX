# P620 CLINX 生命周期、执行路由与 Tunnel 修复

## 已证实根因

- 目标 execution 已由精确 Finalizer 写入 execution_history，completion handoff DONE，lease 已释放，但 coarse tasks.status 没有随 execution_state=COMPLETED 更新。完成的是 Codex turn；原生正文的 business_outcome 仍是 NOT_EVALUATED。
- prepare 只按 project 解析 cwd/branch；prompt 中的 worktree/HEAD 不参与路由封存。ORION 项目配置因此选择 canonical cwd/master。修复通用 workspace contract，不为 ORION 添加分支。
- 新建 prepare 强制 network_access=False，continuation 忽略显式输入，MCP 隐藏网络字段，dispatcher 的 turn/start 又省略 per-turn network policy。持久布尔值不能证明旧 Provider 的实际网络能力。
- tunnel 日志可见 stdio child 退出，随后请求 tunnel-client 正常 shutdown；旧 Restart=on-failure 不恢复正常退出。运行态已有人加过 Restart=always，但源码 unit 尚未收敛。日志没有证明 child 首次退出的触发原因；保留 UNKNOWN，增加有界请求错误处理和可选 stderr 退出诊断。

## 修复边界

1. 原 terminal reconciliation 在同一 task/execution 写事务中同步 COMPLETED status；失败/拒绝不伪装 COMPLETED。失败的 task 可保持 ACTIVE（任务尚可继续），但 execution 为 BLOCKED/FAILED、codex_running=false；RECOVERY_REQUIRED/UNKNOWN 仍保留所有权。
2. prepare 接受 workspace_contract={cwd,branch,head}，新字段在 prepared_executions 中附加迁移，参与原 integrity hash。无显式 worktree 时也封存现有 cwd/branch/HEAD。指定路径只允许配置项目的现有 Git linked worktree，核对 common-dir、worktree list、origin；不授予任意 sibling clone 权限。
3. start 和 dispatch/turn-start 前重新核对 cwd/branch/HEAD；workspace 漂移明确拒绝，无 fallback。旧未派发且没有 contract 的 preparation 必须重新准备；DISPATCHED 的相同请求仍只返回原 execution。linked workspace continuation 沿用原封存 contract，HEAD 改变需明确提供更新后的 baseline。
4. 新建任务缺省无网络；明确批准的 network_access 布尔值参与原 seal，进入 workspaceWrite sandboxPolicy，输出和状态可见。continuation 改变 sealed network policy 明确拒绝，需既有正式 policy reauthorization；不从自然语言推断授权。该布尔接口不提供目的地址 allowlist：只有受限 endpoint 授权而无可实施限制时应拒绝/采用既有受控只读通道，不能把它声称为 endpoint 隔离。
5. 网络 ACK 丢失保持 transport uncertainty，不包成确定的 sandbox 启动拒绝。SQLite 请求异常返回 MCP 错误，后续请求仍可用；stream EOF/错误可在 CLINX_STDIO_DIAGNOSTICS=1 时写入无 payload 的 stderr 诊断。
6. tunnel unit 使用 Restart=always、10s 固定恢复间隔，不再因 dispatcher PartOf 关系被连带停止；健康 timer 只读取 systemd PID、health/readiness 和最近 120 秒成功 poll。健康失败记录 systemd failure，不因网络故障盲目重启。
7. scripts/reconcile_task_projection.py 通过原 ExecutionFinalizer/finalization_lock 维护精确已完成历史投影：核对 result/turn/binding/latest execution、无 execution/lease/foreign workspace owner，原 recovery audit 记录 before-image，只改 coarse status，不调用 Provider，不增删 lease，不改变原结果或策略。

## 验证与交付

证据目录：/data/artifacts/clinx-lifecycle-routing-tunnel-20261020。定向测试使用 disposable SQLite/Git fixtures、真实 AppServer JSON 协议客户端及既有 Owner socket 故障测试，不创建真实任务或执行 P1.6。单独 transient systemd unit 验证正常退出 0 后恢复和重复 start 同 PID，不中断生产 Tunnel 以制造故障。

最终测试、已安装 release、服务/PID/源码 hash、状态维护及备份/回滚读回以该目录的 release-regression.log、supervision-regression.json、deployment-receipt.json、final-readback.json 和 FINAL_REPORT.md 为准。源码提交或构建不等于运行验收。历史 P1.6 cwd/network 事实保留，不重写成正确执行。

已独立证明的基线失败：test_execution_continuity.py::test_host_writer_stays_dedicated[states0-True] 在未修改 c853201 源码也失败；当前共享 Desktop writer 隔离规则拒绝该测试预期的 handoff。没有放宽规则或修改该测试。

## 回滚原则

保留原 release、配置和一致 SQLite backup；禁止整库覆盖恢复。新增 prepared 列要求旧 rollback 程序忽略未知 prepared 列，且拒绝不认识的新 seal。准备兼容 rollback artifact 并在迁移后的 disposable DB 上验证；任何切换前重新检查 execution/lease、Owner 和 systemd jobs，先停止 admission 并排空，不杀活跃任务。不回滚已提交终态事实、audit 或后续任务数据。
