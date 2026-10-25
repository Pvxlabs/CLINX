> 文档分类：SUPERSEDED。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# 原生任务流通

CLINX 接收用户已授权的任务，路由到指定主机和原生 Codex 对话，关联输入与回执。
普通原生转发不要求用户把 Desktop 的独占执行权交给 CLINX。

## 本次移除的派发门槛

- Desktop 已持有对话：直接通过同一个 daemon 继续原对话，不要求独占 handoff、
  手工 adoption、新 Task 或 successor Thread。
- Desktop 原生活动状态、其他 daemon 的 idle/unknown 副本：不作为普通输入的
  独占执行权否决。原生 `turn/start` 可以把并发到达的消息送入已有 Turn，回执明确
  `exclusive_writer=false`，不宣称新建或独占该 Turn。
- 共享连接收到 Desktop 的审批、用户输入或动态工具请求：CLINX 不发送抢答的
  unsupported 错误，交给原生客户端处理。
- 普通新建与续派使用 `NATIVE_FULL_ACCESS`；旧 `network_access` 参数仅为调用兼容，
  不再限制网络。续派沿用版本化策略转换，历史执行记录保持原样，详见下文。
- 指定既有工作目录：自动读取 branch 和 HEAD，再使用既有目录解析；不要求用户
  手填 Git 元数据。路径不存在或不是选定项目的 checkout 时返回真实路由错误，
  不静默落到另一个目录。显式提供的完整 workspace contract 仍按用户要求匹配。
- linked worktree 已产生新提交：新一轮 continuation 读取该目录当前 HEAD，不再继承
  上一次请求的旧 HEAD。Task 和 Thread 身份不变。
- 历史 preparation 没有工作区快照：从已有 Task checkout 内部解析，不要求重建任务。
- 已证明未发送 RPC 的共享 writer 拒绝：旧错误缓存不能永久阻止修复后的原请求。
  Execution Owner 原子保留先前拒绝的 checkpoint，并重新评估同一封存请求。

## 内部关联与回执

原生输入使用由 Thread 和既有 execution reference 派生的 `clientUserMessageId`。
只有在返回 Turn 的原生 `userMessage` 中核对到同一 clientId、完整输入和原生 item ID，
才记录 `NATIVE_INPUT_FORWARDING` checkpoint。公开 start/status 回执可读到
`native_forwarding`，包含实际 Thread/Turn/item/client ID、`input_confirmed=true` 和
`exclusive_writer=false`。这些是请求输入的证据，不是对共享 daemon 独占权的声明。

重复调用已派发 preparation 返回既有结果，不再次发送输入。ACK 或输入观察丢失时，
保留已消耗请求和当前执行用于准确对账。未发送 RPC 的旧拒绝可自动重新评估，
条件由 canonical preparation、原拒绝记录及没有 Turn、活动 lease、结果、Host effect
或 completion handoff 共同确定；旧失败证据保存在原有 context_checkpoints，
不建立第二套任务或数据库。状态查询保留旧 BLOCKED 历史，同时给出可直接重试的
原 prepared reference 和恢复说明，不再让旧回执引导用户手工交接。

Host 动态工具需要专用响应处理器，沿用既有专用 Provider。普通原生任务不先调用
Host operation catalog、权限审查或模型探测；默认继承模型和 reasoning。
Linear 仅作审计，不决定 Codex 能否执行。

## 验证

`tests/test_native_input_forwarding.py` 覆盖共享连接、并发原生 Turn 的输入关联、错误
Thread/Turn/clientId/正文、Desktop server request、不确定回执不重放，以及旧错误缓存
在 Owner 重启后的精确重评估。工作目录、网络和请求幂等测试保留在现有测试中。

真实资格脚本在任务专属目录启动隔离官方 daemon，保持模拟 Desktop 连接，使用完整
prepare → start → input receipt → exact completion 路径连续两轮继续同一 Thread，
重复 start 不新建 Turn，最终只有一个 Task，lease 全部收口。
正式用户对话用于只读路由核对，不用业务任务来制造测试 PASS。

协议依据：[OpenAI Codex App Server](https://developers.openai.com/codex/app-server/)
及当前已安装 CLI 生成的版本对应 JSON schema。

## 普通任务的原生完整访问

普通 Codex 任务采用 `NATIVE_FULL_ACCESS`，实际 `turn/start` 使用
`sandboxPolicy={"type":"dangerFullAccess"}` 和 `approvalPolicy="never"`。
CLINX 不注入可写目录白名单、网络禁用或额外审批。旧 `network_access`
参数仅为调用兼容，不再限制任务；新建和续派均默认完整访问。续派通过
既有版本化策略转换移除旧限制，历史执行记录保持原样。原生服务若拒绝
该配置，返回真实错误，不静默回退到 workspaceWrite。

任务身份、输入确认、请求去重和现有 workspace lease 仍用于任务协调。
工作目录用于路由，不是 Codex 的文件访问白名单。
