> 文档分类：REFERENCE。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Desktop 原生执行

## 当前 Rust 路径（2026-10-24）

`CODEX_NATIVE` 与 `HOST_EXECUTOR` 全部使用现有 Desktop 原生 daemon。
不启动专用 Provider，不创建独立 `CODEX_HOME`，不复制会话或凭据。
Rust 继续保存唯一任务权威、发送许可、输入去重、Host 调用账本及最终回执。
动态工具只处理已绑定 Thread/Turn 的请求；Desktop 的审批、用户输入及其他任务
的工具请求交给 Desktop。

显式 `CODEX_NATIVE` 准备请求中的旧 Host capability、operation class 和
`requested_operations` 字段不选择 Host 策略，也不授予 Host 权限；Rust 沿用既有
任务策略，新任务使用原生默认策略。显式 `HOST_EXECUTOR` 及省略 surface 时由
`requested_operations` 选择的 Host 路径，继续校验准确的 capability、class 和 scope。

`clinx-provider --desktop-socket --socket <exact-path> --witness-thread-id <exact-id>`
通过现有聊天的精确 thread ID、socket inode 和内核 peer UID
验证 Desktop 端点。`/run/clinx-rust-provider/provider.sock` 仅保留为历史账本使用的
逻辑 socket 别名，直接指向 Desktop socket，不运行中间 daemon 或执行代理。
Socket 消失时明确失败；重新发布前必须再次验证准确身份，不扫描并选择其他 daemon。
服务的 private `/tmp` 映射到同一 Desktop socket 目录，不再映射旧 Provider 目录。

原生观察由用户 Observer 读取已有会话记录，Centre 通过现有鉴权只读接口续采。
观察项的 `THREAD_UNBOUND` 不表示执行失败，也不自动授予接管权。明确授权的续派
按原生 thread ID 建立 Rust task；原生 writer 未释放时不重放或复制会话。

Host 调用因 `ROOT_CONTROLLED_WORKER_LAUNCHER_REQUIRED` 在创建子进程之前
被拒绝时，Transport 可提交原生终态 turn 中的原始失败工具回执进行协调。
Rust 核对精确 endpoint、turn、call、只读和无变更证据，保留原状态及回执摘要，
按 `NOT_EXECUTED` 收敛 Host 调用，并将所属执行终结为 `FAILED / RELEASED`。
普通超时、断线、身份不符或可能发生变更的调用不能沿用此证明释放资源。

采集源故障恢复后，即使原生 turn 内容没有变化，也会在 coverage 变化时重新上报。
恢复记录使用最多 8 条的独立调度队列，避免被持续活跃会话挤出采集。
`freshness` 表示最近一次收到列表项变化的时间：30 秒内为 `RECENT`，随后为
`STALE`；不变的终态样本继续去重，不通过重复上报制造实时活跃证据。

## 历史 Python Owner 路径

普通原生任务的新建和续接统一连接 `app_server.native_socket`。
CLINX 转交用户输入并跟踪原生 thread/turn，不另建一个专用 Provider writer。
`NATIVE_FULL_ACCESS` 保持 `dangerFullAccess`、`approvalPolicy=never`；
不添加 writableRoots 或网络禁用配置。

新建连接只允许在已解析的绝对 cwd 创建一个持久 user thread，不安装 Host
动态工具或 developer instructions。创建授权在发送 RPC 前消耗；响应丢失不重试
thread/start。成功后只向返回的精确 thread 转交输入，确认原生 userMessage 的
clientId、正文、item ID 和实际 turn ID，并记录 NATIVE_INPUT_FORWARDING 回执。
Desktop 继续处理自己的 approvals、用户交互和工具请求。
标题通过同一 Desktop 原生连接设置，不重复投递给要求 CLINX originator 的旧跨服务
元数据 outbox；该 outbox 的历史身份校验保持不变。

Desktop 未配置或不可用时明确报错，不回退至专用 Provider。
历史专用 Provider 仍持有的活动会话等待原执行完成；空闲但未释放的旧会话明确
报告 NATIVE_LEGACY_OWNER_NOT_RELEASED，不抢写锁、不复制会话、不新建分叉。
历史 HOST_EXECUTOR 的专用动态工具执行契约保留；公开开发任务使用原生完整访问路径。

部署切换 CLINX Execution Owner 及保留的 Linear 调度入口的已验证源码，支持通过
`CLINX_EXECUTION_OWNER_RELEASE` 固定其独立版本，不改变共享 node-centre/MCP/Observer
版本。切换前让旧 Execution Owner 停止接收新请求并自然排空既有执行；不重启
Desktop daemon 或专用 Provider，不中断它们的 turn。既有 Owner 的排空与
thread/unsubscribe 后的原生卸载宽限期分别观察。

验收需要从 CLINX 新建一个任务，证明 Desktop daemon 为实际执行服务，并在
实际 Desktop 中确认侧栏、正文和输入区域。API 查询到历史不能代替 UI 验收。

原生输入的启动确认丢失时，不以缺少 CLINX turn ID 推断没有执行。Execution Owner
仅对自己持有的 UNKNOWN 尝试进行有限只读对账，按原 sealed execution 生成的
clientUserMessageId、完整输入正文、thread 和原生 item 精确恢复 turn 绑定，再交给
原有 completion observer 收敛结果。没有精确输入证据则继续保留租约；不重发输入。
维护入口为 `recover-execution <exec_ref> --reconcile-native-input`。恢复事务保存原错误，
同时绑定原准备记录、execution 和持久 completion handoff，不创建替代任务或会话。
保留的 Linear 调度入口也必须加载相同原生路由版本；共享 MCP/Observer 不随之切换。
