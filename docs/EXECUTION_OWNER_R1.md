> 文档分类：SUPERSEDED。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Execution Owner R1：精确未派发恢复（DEV）

历史记录：2026-10-24 [第四轮清理](cleanup/RUST_MIGRATION_CLEANUP_PHASE4.md)已删除
`execution_owner.py` 服务和客户端。下文记录当时的恢复契约；现存
`unstarted_recovery.py` 仍用于精确历史证据维护，不表示旧 Owner 可以重新启动。

RCA：CONFIRMED。旧 Owner 在模型解析阶段拒绝 `gpt-6`，尚未调用
`turn/start`，但因 execution 已建立被降级为 UNKNOWN。旧取消路径拿了
Task 保留的上一轮 turn，收到 `no active turn to interrupt` 后持续保留
CANCELLATION_PENDING。排空判断因该非终态记录持续为真。

本增量仅为 DEV 实现与隔离验收，不表示生产故障已恢复。生产执行、lease、
配置、授权、运行制品和 systemd 队列均不在 R1 修改范围。

## 实现边界

- `_resolve_dispatch_model` 只在已知派发前调用点给 `ModelCapabilityError`
  附加 TURN_NOT_DISPATCHED。保留原始错误与 MODEL_CAPABILITY_UNAVAILABLE。
  transport/ACK 丢失不获得该标记；execution 存在本身不会覆盖这份现场证据。
- 新的确定失败经过既有 Finalizer，提交 BLOCKED / TURN_NOT_DISPATCHED 和
  原始快照审计，再释放精确 lease。Prepared 为 FAILED，并由恢复审计阻止
  同一 prepared ref 被重新消费；Owner 保留错误响应，重复请求返回缓存错误。
- 取消只取 execution_owned_turn。NULL 或与 Task 当前指针冲突均在 Provider
  和 Host cancel 前拒绝，不回退到历史 Task turn。旧记录仍可读取。
- `unstarted_recovery.py` 是现有维护入口的窄适配器；使用既有 Task Registry、
  ExecutionFinalizer、execution_recovery_audit、finalization_lock 和 lease。
  不新增任务、执行、Provider 或恢复队列。
- 旧记录的负面证明来自持久 Owner 模型错误与已审查制品的 SHA-256，而不是
  “没有 turn id”、Provider idle 或 interrupt 错误。仅支持已审查旧 build 的
  continue、SANDBOX_WORKSPACE、无生产/业务/能力扩权策略。其他 build/错误拒绝。
- Task 保留历史 turn 时，必须能对应到另一条已完成、早于本次 acquisition 的
  execution_history。后续原生 turn 仅作为原生事实读取，不归属给本 execution。
- 精确身份包括 task/execution/prepared/thread/request hash/owner instance/PID/
  worktree key/acquisition；另校验 seal、route、policy、binding、lease、liveness
  及现有 completion/result/Host 记录。配置端点必须可读、无活跃/冲突 writer，
  且一致返回 completed turn。首次证明有效期为 120 秒，提交时做完整快照 CAS。
- liveness UNKNOWN 不被改写为 Provider TERMINAL，也不生成 execution result、
  execution_ref、Turn 或成功事实。原始 Owner UNKNOWN/取消错误留在审计中。

## 提交、中断和幂等

1. 在现有跨进程 finalization lock 内读取证据。
2. BEGIN IMMEDIATE 重新读取完整快照并核对；同一事务写原始审计、BLOCKED
   execution/task 投影和 FAILED prepared。原来的 Task turn 保持不变。
3. 事务提交后，既有 release_execution 检查已提交审计、终态、NULL turn 和
   精确 lease 归属，原子保留 execution_history 并释放对应 lease。

第 2 步提交前进程死亡，SQLite 回滚；第 2、3 步之间死亡，终态和审计已持久化，
lease 保留；第 3 步后 ACK 丢失，重试只读回同一结果。并发恢复使用真实 flock
和 SQLite CAS，不以测试锁替代。重试出现外来 lease、绑定/请求/历史身份变化
或任何 execution 证据冲突时拒绝，不改写其他执行。

## 隔离验收证据

基准 main：`4d4f966d7f2a72f73dccd159b1c93613fac7d28b`。

`fixtures/execution_owner_r1.json` 是正式 DB 的只读选择性脱敏导出，包含原始
失败、状态、时间和 liveness；不含业务 prompt、标题、凭据或完整 DB。测试
重建隔离身份、路径和 prepared seal，再关联同结构的历史 turn。

本次事件 profile 测试通过 CLINX_R1_LEGACY_SOURCE 使用任务目录内的真实旧
源码副本，校验硬编码的四个 SHA-256，没有替换生产信任根。常规可移植测试
会显式注入测试信任根；其结果不冒充真实制品验证。

最终相关回归：224 passed，11 subtests passed，0 failed（约 17 秒）。
其中 R1 专项 54 项，涵盖模型错误、新建/继续、历史和后续 turn、UNKNOWN/
CANCELLATION_PENDING、错误 interrupt、ACK 丢失、身份冲突、证明过期、并发、
重复、提交前/后实际进程退出、终态已提交而 lease 未释放、外来 lease 和
Owner 排空条件；CLI 预检也验证不初始化/迁移/全局 reclaim，不创建 Dispatcher。

协议测试使用真实 CLINX AppServer 客户端与隔离 Unix WebSocket fixture，包含
已接收 turn/start 后关闭连接丢 ACK。它是协议故障注入，不是生产官方
AppServer 或 GUI 验收。既有 loopback mTLS 回归使用任务目录编译的 OPAQUE
后端，未接触实际节点授权。Ruff 新文件无诊断；既有文件 29 项基线诊断无新增；
git diff --check 通过。完整日志位于任务 artifact 目录，不部署。

## R2 生产恢复前置条件（本轮不执行）

目标：task_e7f16a8e7d604ba78200b07605ce3547 /
exec_4eeeec1558de42a0b067e198a077998f /
prepared_4eeeec1558de42a0b067e198a077998f /
thread 01a15005-33d0-7490-a095-6ec136f21482。

1. 另行取得针对该精确 canonical reconciliation 的生产授权；R1 DEV 授权
   不包含该权限。选定并校验已审查 R1 commit/artifact。运行其维护 CLI，
   不必先更换或重启旧 Owner；不能使用未安装此模块的旧 release CLI。
2. 只读重新核对 Owner MainPID、启动时间、锁、运行路径/制品、systemd job、
   该 execution/lease、prepared seal 和持久响应。原 PID/owner 改变、记录已被
   他人处理或未知源制品均停止；不能按历史快照强行恢复。
3. 针对当前 DB 生成并人工审查 identity JSON，字段必须且只能为：
   task_id、execution_ref、prepared_execution_ref、thread_id、request_hash、
   owner_instance、owner_pid、worktree_key、acquired_at。值来自最新只读核对，
   不接受 caller 提供的“未派发=true”。保留一致 SQLite 备份和相关配置/制品
   的回退依赖；备份不是后续覆盖整库的许可。
4. 协调短时不向此 Thread 发送新的 Desktop/CLI 输入；重新核对两条配置内
   daemon 的原生事实及故障窗口。任何 active/UNKNOWN/OFFLINE/conflict 停止。
   官方协议没有跨所有外部 writer 的原子 compare-and-start；此适配器不
   获得该能力，也不会认领或中断外部 writer。
5. 先以 `--check-only` 运行下面的精确入口，审查 qualified/identity/source
   hashes/observations；随后在授权窗口内去掉该参数执行一次。执行入口重新
   读取证据和快照，不直接信任预检输出。所有其他 recovery mode 互斥。

```text
python -B <reviewed-R1-artifact>/bridge.py --config /home/pvxlabs/dev/clinx/bridge.toml \
  recover-execution exec_4eeeec1558de42a0b067e198a077998f \
  --reconcile-model-failure --model-failure-identity-file <reviewed-identity.json> --check-only
```

该入口使用 TaskRegistry(initialize=False)，避免迁移和全局 lease reclaim；
不启动 Provider、HostExecutor、Linear 或 dispatcher，也不发送 start/resume/
interrupt。预检不创建恢复锁；写恢复只创建已有协议所需的精确恢复锁和审计。

6. 读回 BLOCKED / TURN_NOT_DISPATCHED、NULL execution turn、一条恢复审计、
   FAILED/不可重放 prepared 和对应 lease 消失；历史 Task turn、原生后续 turn、
   原始 Owner 响应及其他 execution/lease 不变，没有新增 Provider turn/Host
   delivery。ACK 丢失或中断时仅使用同一身份文件重试此维护入口，不重放任务。
7. 旧 Owner 在终态提交后即可自然发现其排空谓词消除，可能早于 lease 的第二
   笔提交退出。只读观察已排队 restart 的结果，不补发信号。核对新 Owner
   唯一锁、PID、制品和 execution 一致性；新服务 running 不是业务完成。

任一事务前校验失败不应产生执行变化。终态已提交后的中断按同一审计继续；
外来 lease 则停止调查，不能人工释放。若新版启动失败，保持已确认的执行事实，
另行审核制品回退；不恢复整库、不清除审计、不重新派发或取消 ORION。

R2_READY 表示关键隔离验收满足进入生产授权评审的条件，不表示生产前置检查
已经通过，更不表示 R2 已执行。Owner 的 FD 累积现象和任意外部 writer 的
原子所有权边界均不在此窄修复中宣称解决。
