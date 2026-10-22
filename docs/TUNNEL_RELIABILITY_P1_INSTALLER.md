# P1 窄发布入口

2026-10-22。`scripts/deploy_forwarding_release.py` 复用已封存的
`forwarding-p1-dfd8d647c3576608c0ea` 及其回退包，不重新构建 runtime。
它取代本次发布中不适用的旧全栈 `deploy.py` 调用，不修改历史脚本或证据。

旧脚本同时改写多个控制入口、停止 Owner，并用 `snapshot()` 要求 executions 和
worktree_leases 整表为空。本次仅修改 Tunnel unit、health unit、99-p1 drop-in 和
MCP loader；全局历史任务数量与这些文件的发布无关，新入口不读取任务数据库。

流程：校验两个归档的精确 SHA256、manifest、回退数据及目标范围；按原包快照对
四项文件和依赖做 CAS；先持久化回退包、原服务状态与操作进度，再暂停 health/timer
及 Tunnel、复核 CAS、逐项原子替换并 fsync；daemon-reload 后只启动 Tunnel 和
health/timer。新 release 按 manifest 落入原 releases 根，不覆盖既有 release。
完整安装保留精确内容、模式和原先不存在状态。

健康验收要求在 90 秒内观察到至少两次推进的成功 poll，采样年龄不超过 30 秒，
health oneshot 成功、timer 恢复原活动状态，Owner/Provider 的进程及启动时间不变。
这是发布后的短期运行验收，不是长轮询根因结论或长期恢复 SLO 承诺。

失败时用同一包的精确 before 数据恢复。更新前持久化 attempted 记录，故障或
响应丢失后的每项状态必须仍为已知 before/after；若检测到第三方改动，停止并报告
冲突，绝不覆盖。四文件不是跨文件原子事务；消费者在更新期间停止，已持久化进度
支持中断后显式回退。不会恢复数据库、历史结果、lease、fencing epoch、凭据、
Owner/Provider 或 supervisor 预算；不会重放任务。

生产入口使用当前账户的 `systemctl --user`，没有 sudo、权限提升、审批平台或新服务。
备份必须位于既有 `~/.local/state/clinx/maintenance`；归档、安装脚本及所需 helper
均保存在该备份目录。显式回退命令：

```sh
python3 <backup>/deploy_forwarding_release.py rollback --backup <backup>
```

新测试仅在任务拥有的 artifact fixture 中模拟服务并操作文件，不启动真实消费者。
测试覆盖非空历史保全、成功发布/配对回退、重复回退、健康失败、写入后响应丢失、
并发修改、权限拒绝及中断后的部分回退。原 parser 和 Tunnel 故障矩阵不重复运行。

本次实际发布已完成：回执 `DEPLOYED`，105 项实际安装文件状态匹配候选；
supervisor/child/MCP PID 分别为 2950141/2950142/2950160，Owner 1306166 和
Provider 666593 的 PID/启动时间未变。poll 推进、health 成功、timer 活动；
恢复预算 launches=1，剩余 3 次，未耗尽。未触发生产故障注入或生产回退。
安装后真实 connector 已读取同一原生任务状态及既有 COMPLETED/WRITTEN 回执；
没有为验收另建任务、重放历史任务或向当前运行轮次插入新消息。
长期恢复 SLO 和新发独立任务的完整派发/完成验收未据此宣称通过。

运行备份：
`/home/pvxlabs/.local/state/clinx/maintenance/forwarding-p1-activation-20261022T033040Z`。
完整证据：`/data/artifacts/clinx-p1-activation-20261022T033040Z`。
