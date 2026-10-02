# Codex 原生对话互通

CLINX 是互通层。当前授权主机、当前 Unix 用户的原生历史是读取 authority；没有 CLINX task/project registration 也可以读取。历史读取不授予 cwd 的执行权限。

```python
uri = "codex://threads/01a0fa4e-11e7-7540-91c8-1eee45d1a4d0?hostId=remote-ssh-discovered%3Ap620"
clinx_get_context(codex_uri=uri, recent_turns=8, max_bytes=10000)
clinx_get_status(codex_uri=uri)
clinx_get_context(thread_id="01a0fa4e-11e7-7540-91c8-1eee45d1a4d0", host="p620")
# 仅在用户明确要求关联后；不开始执行。
clinx_adopt_conversation(codex_uri=uri)
```

本轮对上面的 ORION URI 仅限读取；真实关联和执行验收使用隔离 thread。不要先调用 `clinx_find_task`，不要将 ID 放入 `query`。两个 native selector 互斥，不能与 task_ref/query 混用。execution_ref 可联合精确读取 selector，但必须属于该 thread。旧 status(execution_ref)、context(task_ref)、project+query 保留兼容。

## URI 和路由

严格解析 URL，唯一允许的 query 为单个 hostId，只做一次 UTF-8 percent decode。拒绝重复/未知参数、fragment、userinfo、port、多余 path、空白/control、畸形或双重编码。URI hostId 与显式 host 冲突返回结构化错误。

`[workspaces.p620].codex_host_ids = ["remote-ssh-discovered:p620"]` 映射到已有 P620 route。URI 不能提供 SSH 地址或命令。iMac/Air hostId 不推断；未知 host 不改查本机。当前实现仅支持 runtime host 的本地原生 history；其他配置 route 没有 reader 时返回 `NATIVE_HOST_UNAVAILABLE`。MCP schema 接受完整 Desktop URI，由同一后端 resolver 验证歧义。

## 只读来源和边界

对 `state_5.sqlite.threads.id` 精确查询。默认 root 为当前用户 `~/.codex`，隔离环境可配置 app_server.native_home；URI 不能改变 root。SQLite 使用 mode=ro、query_only 和短事务，无初始化、迁移、全会话扫描或第二套 registry。CLINX 索引不可用不遮蔽可读 native history，关联状态另报 UNAVAILABLE。

| 来源 | 边界 | 证据 |
| --- | --- | --- |
| CODEX_NATIVE_HISTORY | 现有 thread_history_1.sqlite；最多 20 turn；每 turn 最近 user/agent display item；250000 SQL VM steps；user content 最多 32 text parts | turn/status/time、projection offset、rollout size、不完整标志 |
| CODEX_LOCAL_SESSION | 仅索引指向的 exact rollout；最多 128 KiB session header 验证；每页最多 512 KiB | scan byte budget、已知消息 turn/time、下一 cursor、不完整标志 |
| CLINX_CHECKPOINT | 同 thread；anchor/execution_ref 必须匹配 turn/execution | checkpoint_ref、context_execution_ref、归属来源 |

recent_turns=1..20，max_bytes=1024..128000。max_bytes 限制返回用户/助手正文总量，为两种角色分别保留预算；不等同于文件尾部 N 字节。大工具输出不会挤掉原生分页索引里的正文。Legacy 超大 record 无法在一页解析时保留不完整标志和向前翻页 cursor，不伪称没有历史。Cursor 绑定 host/thread/source，以 `clinx_get_context(..., cursor=next_cursor)` 继续。

显示字段只接受 userMessage、agentMessage 和 legacy 明确 user/assistant message；排除 analysis/reasoning channel/phase，不递归走工具、内部 summary 或任意 metadata。常见凭据格式脱敏。归档使用同一索引；active turn 的持久化事实与 live observation 分开。明确 execution_ref 的 pre-turn 失败不能借用线程上一轮正文。

Live reader 仅连接配置中的现存 Unix socket，不运行 proxy/daemon。Observer RPC allowlist 只有 initialize、thread/read、thread/turns/list、thread/items/list，所有 server request 都不回复。不会 resume/start/interrupt、创建 task、写 lease/checkpoint 或访问 Linear。本地历史可用而 socket 被拒绝时 context 可 AVAILABLE，live state 保持 UNKNOWN。

## 显式纳管

`clinx_adopt_conversation(thread_id|codex_uri, host?, project?, title?, summary?)` 复用 dispatcher.adopt_or_reuse_existing_conversation 与 registry.adopt_task 的原子存储。Native index 与 session header 身份必须一致；可用的 live session/cwd 事实也必须一致。保留 thread/session/name/cwd，沿用项目解析与 Git identity guard。受信开发根目录内未注册项目可以动态关联，无需逐线程修改配置。

不复制历史、不 fork、不伪造 execution、不调用 Linear。并发重复请求返回同一 task，task_created 只对真正创建者为 true。Active owner 可建立观察关联，返回 control_transferred=false、execution_started=false；不 resume/interrupt。续接仍需独立的当前用户请求，尊重权限、sealing、单 writer lease，不继承历史 prompt 授权。

## Writer 交接候选

授权 continuation 对配置的 managed/native daemon 分别精确读取，选择唯一 live idle owner。notLoaded 不等于 failed，持久化 inProgress 不覆盖 live idle。Active owner、多个 loaded owner 或观察不完整时不发 resume/turn。全未加载时选择已运行配置 endpoint。没有 writer error 的盲重试、强抢、清锁或新 thread 绕行。

0.156.1 schema 支持 thread/resume 的 excludeTurns、developerInstructions，但没有 takeover、forceResume、dynamicTools。动态工具沿用 thread/start 的原生配置；不得声称 resume 给任意旧 native thread 安装了 Host namespace。新纳管 thread 默认走原生 workspace；Host 工具能力需另行真实验证。

成功 start/resume 的连接只记录自己的 subscription。收到确切 owned thread/turn 完成后，先 thread/unsubscribe，再交给现有 completion finalizer。无 turn 的本连接资源可以清理；只读连接、其他 thread/turn 和不确定 active turn 不 unsubscribe。释放响应不明记录 UNCONFIRMED 后关闭本连接，不重试 RPC。

上述是源码契约，尚不是共享 Desktop、跨 daemon 单 writer 或 namespace owner 的恢复证明。本轮共享 socket 被沙箱拒绝，隔离 provider 启动也被 socket 目录校验阻断。不能用 scripted RPC PASS 代替真实验证，不能重启公共 daemon。

## 执行历史与错误

继续复用 conversation_bindings/lineage/adoptions、execution route、prepared association 和 checkpoints/anchors。选择显式 execution_ref、唯一活动执行或可靠 acquired_at 最近执行。task 的旧 turn_id 不再过滤掉 pre-turn 失败 continuation。每次关联查询最多 257 条，超过 256 明确拒绝。

正常 registry 初始化迁移为 executions/history 添加 failure_stage/code/evidence，按 execution 保存；不从当前 task 投影回填旧历史空值。旧失败缺失证据标为 UNAVAILABLE_LEGACY，task_current_projection 中的错误仅为当前投影。历史成功 PASS 与新失败分开。ACK ledger 与 call/thread/turn/connection/listener/generation 归属、防重放逻辑保留。

- THREAD_UNBOUND：原生存在、无绑定，context 可 AVAILABLE。
- THREAD_NOT_FOUND：健康原生索引 exact miss，absence_scope=CURRENT_USER_NATIVE_INDEX；不是全网不存在。有相反 live 证据时改报冲突不可用。
- NATIVE_INDEX_UNAVAILABLE、NATIVE_SCHEMA_UNSUPPORTED、NATIVE_ACCESS_DENIED、NATIVE_HOST_UNAVAILABLE 分开报告。
- INVALID_CONTEXT_CURSOR、NATIVE_HISTORY_BUDGET_EXHAUSTED 保留有界语义。
- THREAD_IDENTITY_CONFLICT、THREAD_HOST_CONFLICT 不猜选 owner。
- CONTEXT_UNAVAILABLE 与身份、历史执行、live observation 分开。

## 验收与激活

常规全量命令为 `python3 -m pytest -q`。受管沙箱可为测试设置临时 HOME，避免 tunnel child 启动迁移写正式用户数据库。
真实 opt-in 为 test_native_interop_live.py、test_worker_continuation_live.py、test_tool_delivery_live.py。这些测试含新执行，只能由外层独立入口启用。正式合并、服务激活、connector metadata refresh 和原带 hostId URI 的真实 tools/call 均需另行核实；源码 handler 验收不代表正式 connector 已更新。
