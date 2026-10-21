# iMac OP 原生执行接入修复

2026-10-21，证据目录：
`/data/artifacts/clinx-imac-op-execution-20261021T034123Z`。

## 已修复的接入配置

读取原生会话不要求执行共享。iMac 与 P620 的双边授权仍是
`read_sessions=true, execute_tasks=false`；iMac 的 `execution_config=null`，
中心自动原生登记映射也没有 OP。因此历史读取成功、执行准备返回
`SHARING_SCOPE_DENIED`、规范任务查询为空可以同时成立。

按用户已有执行接入授权，完成以下运行配置修复：

- 双边开启 iMac 执行共享，保持原有用户、provider、项目读取范围。
- 为 iMac 的 OP 添加精确 cwd 项目映射，保留其他节点及项目映射。
- iMac 使用干净提交 `714ed2f462666aa9218d2cc4624f40790ff8ba1c`
  的独立运行制品，替换旧观察 helper 的启动目标。
- 配置本地任务库与已存在的 Desktop control socket。运行配置位于
  `/Users/tinzleung/Library/Application Support/CLINX Monitor/native-execution.toml`。
  未设置专用 Provider socket、沙盒或网络禁用覆盖。
- 更新节点 LaunchAgent，重载节点观察服务和 P620 node centre。
  P620 Execution Owner、Provider、dispatcher、tunnel PID 均保持不变。
  iMac Desktop daemon PID `22234` 保持不变，已安装 CLINX.app 包未改写。

当前原会话：`01a15967-42ae-74c3-bc71-ba0eedbb33b9`；
唯一规范任务：`task_fa0efdc019d457ea8fe611107ac79919`。

节点已登记 `execution.adopt/prepare/start/status/context/cancel` 与
`execution.native_canonical_v1`；双边有效 scope 的 `execute_tasks=true`。
公开 prepare 已通过，返回 `NATIVE_FULL_ACCESS`、`NETWORK_ACCESS=ENABLED`，
原 OP cwd/master/HEAD 保持精确绑定。

## 尚未通过的原会话启动

在原提交轮次完成后，使用一条禁止工具和文件修改、仅回复验收标识的输入测试。
准备记录：`prepared_e4cf8eb56b4946bca899f9334ac8a423`；
执行记录：`exec_e4cf8eb56b4946bca899f9334ac8a423`。

start 返回 `NATIVE_ACTIVE_WRITER`，`execution_started=false`、
`side_effect=NONE`。失败发生于 `thread/resume`，尚未发送 `turn/start`。
执行记录为 BLOCKED，turn_id=null，iMac active execution 和 workspace lease
均为零。没有盲目重试、复制原会话或重新派发 OP 开发任务。

精确线程锁的 `lsof` 证明真实持有者是 PID `43571`：
ChatGPT.app 内嵌的 Codex app-server，父进程为 ChatGPT PID `43096`。
该进程使用应用私有 stdio；没有可供 CLINX 连接的命名监听 socket。
配置的共享 Desktop daemon PID `22234` 对此会话返回 `notLoaded`。
因此当前阻塞属于两个原生服务之间的会话所有权，不再是共享权限拒绝。

已请求用户在原应用关闭这条已完成会话标签，保留应用及其他任务。
释放须以真实写锁和服务状态重新核实，不能以关闭标签推定已完成交接。
未终止 ChatGPT/Codex 进程、删除锁文件、修改原生数据库或归档用户会话。

[官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)
规定 `thread/unsubscribe` 只移除当前连接的订阅，并可能经历无订阅卸载宽限。
连接另一个 daemon 不能取消原应用连接的订阅。

## 验证与恢复材料

- 路由、双边 scope、真实节点进程、canonical handoff、Desktop 创建、原生输入
  转交和完整访问策略回归：112 passed。使用项目声明依赖及锁定 OPAQUE 后端。
- iMac 正式回读：`imac-final-readback.json`；制品文件来源和哈希：
  `release-manifest.json`；安装过程及 bootstrap 重读：`install.log`。
- OP 源文件与原生提交阶段保存的逐文件哈希完全一致；七个既有脏文件完整保留。
  本轮没有构建、替换或重启 OP。
- P620 未完成咨询功能的四个文件与开始前 SHA-256 一致，未加入本次提交或制品。
- 原节点配置、sharing scope、LaunchAgent 和中心映射保留 before-image。
  恢复 helper 前须检查 iMac 当前执行、completion 和租约，不能中断后续任务；
  不回滚任务库或新产生的执行事实。

补充限制：中心 `clinx_get_prepared_request` 当前不会转发到远端准备记录，
该调用对本记录返回 Unknown。实际结果以远端精确 execution/status 和本地
只读任务库回读为准；此错误不表示原生输入已发送。

**结论：共享拒绝与任务接入修复已生效；原 OP 会话的 Desktop 交接和完成回读仍未通过。**
