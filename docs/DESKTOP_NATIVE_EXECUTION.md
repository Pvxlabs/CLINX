# Desktop 原生执行

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
