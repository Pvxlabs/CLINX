# CLINX 原生互通与 Writer 交接：P620 交接报告

STATUS=BLOCKED（候选实现与本地回归 PASS；真实 writer 验收、commit/push、正式激活和外层能力尚未完成）。

ROOT_CAUSE：已确认旧 MCP schema/parser 只接受无 query URI；未绑定读取错误依赖唯一注册项目；rollout 尾部 N 字节被 tool/reasoning 淹没；task_ref 状态按旧 turn_id 过滤，漏掉 pre-turn 失败 continuation。旧 continuation 的 app-server 错误只进入 task 当前投影，未独立保存在 execution。Writer 方面，用户既有实测证明跨 daemon loaded state 不同，源码也缺少连接自有 subscription 的显式完成释放；这不足以确定全部 Codex writer 锁释放根因。本轮未获得真实 writer 交接成功证据。

URI_HOST_ROUTING：源码 schema、parser、capability、文档同步。完整 Desktop URI 的 hostId 只解码一次，并通过 workspaces.p620.codex_host_ids 映射已授权 route；未知/重复/冲突/恶意编码不会改查本机。裸 ID+host 保持兼容。正式 connector 本轮再次拒绝原 URI，错误仍为 INVALID_ARGUMENT / pattern `^codex://threads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`。没有用去掉 query 的正式调用冒充成功。

UNREGISTERED_NATIVE_READ：真实 P620 原 URI 在源码 MCP handler 和输出 schema 验证下返回 THREAD_UNBOUND + context AVAILABLE + task_ref=null。将 reader 的 projects 配置设为空，仍从真实 native history 得到 AVAILABLE；没有初始化/迁移正式数据库。隔离测试也覆盖没有 task/project、缺 CLINX 索引、只读不改变业务数据。

CONTEXT_SOURCE_AND_BOUNDS：CODEX_NATIVE_HISTORY 优先使用 state_5.sqlite 和 thread_history_1.sqlite 的 exact identity/index；仅显示 user/agent 正文，排除 analysis/reasoning 和工具输出。20 turn 上限、250000 SQL VM steps、32 user text parts、正文 1024..128000 bytes。Legacy 单页 512 KiB 加最多 128 KiB header，有 scoped cursor；不读取完整大 rollout、不扫描全部会话。返回原生 name/cwd、持久化 turn/status/time、消息归属、projection 落后与截断标志。现存 socket 观察独立返回；权限失败不覆盖可用历史。详细语义见 THREAD_IDENTITY.md。

ADOPTION_CONTRACT：新增正式 clinx_adopt_conversation，复用原有 dispatcher adoption 与原子 task/binding storage。读取不注册；明确纳管才写 CLINX 关联。保留 native thread/session/name/cwd，动态项目解析仍使用现有安全工作区规则（未注册项目为已允许 workspace 的直接子项目）。重复、并发纳管复用同一 task；真正创建者才 task_created=true。Active 只观察，control_transferred=false / execution_started=false；无 resume/interrupt、新 turn、lease、checkpoint、Linear 或虚构 PASS。当前工作区规则不新增任意路径执行授权。

WRITER_HANDOFF：候选实现精确观察已配置 managed/native endpoints，选唯一 live idle owner，保持原 thread；活动 owner、不明/冲突归属不发 resume/turn。notLoaded 与持久化 inProgress 均不误判成真实活动执行。当前 0.156.1 普通/experimental schema 和既有 artifact 已核对：thread/unsubscribe 可用，thread/resume 没有 takeover/forceResume/dynamicTools；resume 使用 excludeTurns 与 developerInstructions，保留原静态工具定义。完成时仅释放本连接成功 claim 且 exact turn 已完成的 subscription，然后交原 completion finalizer。只读客户端不回答任何 server request；同 thread 的其他 turn 广播也不抢答。共享 Desktop namespace 行为、跨 daemon 同 thread 续接与真实并发 writer 尚未证明，不能提前激活为已恢复。

HISTORICAL_EXECUTION_ISOLATION：真实只读交叉结果：task_9295558bb6d04de1bdf843a59a28f550 选中 exec_f432813670d240cd861b95e002206fe3，RECOVERY_REQUIRED、turn=null、Host count=0、result=null；不返回旧 PASS。显式 exec_f1b3bf3e9a694b7fb017470b4dee5222 仍 COMPLETED/PASS，原 turn=01a0fa4f-7cdd-78c3-b1c2-282c2cb213a1、Host count=2。旧失败保持不变，缺 execution 级错误证据标 UNAVAILABLE_LEGACY。新初始化迁移仅新增 execution/history 的 nullable failure 字段，不从 task 回填；今后按 exact execution 保存 failure_stage/code/evidence。

ACK_REPLAY_SAFETY：原 ledger 不重写。顺序/批量 ACK、错误 owner/turn ACK、完成归属、防重放和 delivery failure counter 本地回归通过；foreign turn 请求现在不回错误包，避免与实际 owner 竞争。真实 provider 故障注入 counter=1 仍为 NOT_RUN，不借用历史测试 PASS。

TESTS：最终完整 Python 回归 `HOME=/tmp/clinx-native-interop-20261002/test-home python3 -m pytest -q`：788 passed, 6 skipped，23.52s。临时 HOME 避免 tunnel child 的正常启动迁移写正式用户数据库；未重装依赖。六项跳过为两项真实 Host contract、两项 continuation/provider、一次真实 delivery fault、一次新增 native adoption/continuation 验收。compileall 和 git diff --check PASS。新增/更新测试覆盖 URI 外层 schema、无注册 native index、legacy 大工具尾部翻页、archive/active/checkpoint、正文预算、reasoning/凭据排除、只读无副作用、纳管幂等/并发、失败续接、owner 路由/释放及旧 turn 广播。

REAL_NATIVE_EVIDENCE：原 thread 01a0fa4e-11e7-7540-91c8-1eee45d1a4d0，native name=完成 ORION 生产发布收口，cwd=/home/pvxlabs/dev/ORION，history_mode=paginated；确切 turn=01a0fa4e-13d4-7333-aeb1-eca7f52bdaad。最终保存的只读证据 observed_at=2026-10-02T03:05:47.748137+00:00，持久化状态为 completed；用户先前的 active/inProgress 观察属于更早快照。正文取自原生 display items，报告/证据不保存正文或内部 reasoning。Live state=UNKNOWN，因为两个共享 socket 连接都 PermissionError [Errno 13] Permission denied；持久化 completed 不证明实时 writer 已释放。源码 MCP handler 是真实数据库读取验收，不是正式外层运行态 PASS。

CHANGED_FILES：本报告和下方提交清单中的 25 个 Core、协议文档及 Python 测试文件。Monitor 的 13 个既有 dirty 文件不属于修复。

COMMIT/PUSH：NOT_DONE。开工与交付 HEAD=788330293350a80b036dd600ee0ec6bc9679a239，branch=main，origin=git@github.com:Pvxlabs/CLINX.git；git ls-remote 已确认远端 main 同 SHA。尝试创建 fix/native-interop-writer-handoff-20261002 时返回 `fatal: cannot lock ref 'refs/heads/fix/native-interop-writer-handoff-20261002': unable to create directory for .git/refs/heads/fix/native-interop-writer-handoff-20261002`。当前沙箱 .git 只读，未改用其他 Git 后端、未 stage/commit/push。

RUNTIME_ACTIVATION：NOT_PERFORMED。未 merge，未重启本 worker 的 MCP/provider，未重启公共 daemon，未改 iMac/Air。正式 connector metadata 仍是旧 pattern。正式共享 runtime 的有效代码/行为需外层在独立激活入口读取进程来源并验收。

OTHER_DIRTY_PRESERVED：PASS。13 个初始 Monitor dirty 文件全部 SHA-256 未变；未 reset、clean、stash、force push 或提交它们。

ORION_MUTATION：NONE。只查询该精确 native identity/history；不 adopt 本轮 ORION thread，不向它发送 turn/start、steer、interrupt 或 resume；没有重跑 ORION 发布或更改业务状态。

UNVERIFIED：真实 native adopt+两轮执行、跨真实 daemon/桌面同 thread continuation、active owner 的真实并发保护、真实 Host/ACK/fault counter、正常开发分支 commit/push、正式合并/激活及外层带 hostId URI/adoption。源码支持其他已配置 host 的结构化不可用，不宣称 iMac/Air 远程 reader 已实现。

BLOCKERS：共享 provider socket 访问被沙箱拒绝；隔离 raw provider 启动报 `app-server socket directory must be a user-owned directory with mode 0700`（已检查临时目录为 uid=1001/mode=0700，仍失败；未猜测更深层原因或继续重复尝试）；.git 只读；外层 schema 仍拒绝 query，且受管 worker 不得自行激活/嵌套执行。未把任何 NOT_RUN/UNKNOWN 记 PASS。

NEXT_STATE=BLOCKED，交外层独立入口完成以下步骤。

## 证据位置

本轮可写范围仅正式 repo 与 /tmp，因此证据放在 `/tmp/clinx-native-interop-20261002/`：

- before.json：保存 URI/parser 与 tail reader 反例。
- source-mcp-real-evidence.json：完整原 URI 的源码 schema/handler 真实读、未注册项目配置读、exact 历史执行隔离、protocol hashes、外层拒绝摘要。
- provider-before.json：两个现存 socket 的明确 PermissionError。
- raw-protocol.json、isolated-writer-protocol.json：隔离 provider 启动失败及对应原始日志路径。
- full-pass.txt：788 passed / 6 skipped；dirty-before.json、dirty-preserved.json：13 个既有文件的字节级保护证据。
- qualification-manifest.json、core-repair.patch：最终文件 SHA-256 与只包含本修复的 tracked patch。新文件按下方显式清单拷入 review worktree。

这些文件没有保存 ORION 正文、reasoning、凭据或无关任务内容。外层可按自己的 artifact 流程保留，不把 /tmp 证据当作已发布运行态。

## 外层精确交接

先完成真实隔离验收，再提交修复；不在这个 managed worker 内运行以下流程。不修改 canonical 当前 main/Monitor dirty。建议从同一 canonical Git 创建独立 review worktree，避免切换共享 runtime 工作目录的 branch。

```sh
cd /home/pvxlabs/dev/clinx
git rev-parse HEAD
git ls-remote --heads origin main
# 两者必须仍为 788330293350a80b036dd600ee0ec6bc9679a239；若变化，先在隔离分支处理再验。
handoff_dir=$(mktemp -d /tmp/clinx-native-outer.XXXXXX)
review_root="$handoff_dir/review"
repair_branch=fix/native-interop-writer-handoff-20261002
cp /tmp/clinx-native-interop-20261002/core-repair.patch "$handoff_dir/core-repair.patch"
git worktree add -b "$repair_branch" "$review_root" 788330293350a80b036dd600ee0ec6bc9679a239
git -C "$review_root" apply "$handoff_dir/core-repair.patch"
cp --parents native_history.py native_provider.py test_native_interop.py test_native_interop_live.py docs/NATIVE_INTEROP_HANDOFF_20261002.md "$review_root/"
cd "$review_root"
mkdir "$handoff_dir/test-home"
HOME="$handoff_dir/test-home" python3 -m pytest -q
# 使用原有机器认证，在原生外层运行；含新隔离执行，受管 worker 内禁止启用。
CLINX_LIVE_NATIVE_INTEROP=1 CLINX_LIVE_CONTINUATION=1 CLINX_LIVE_DELIVERY_ACCEPTANCE=1 CLINX_LIVE_HOST_CONTRACT_V2=1 python3 -m pytest -q -s test_native_interop_live.py test_worker_continuation_live.py test_tool_delivery_live.py test_host_contract_v2_live.py --basetemp="$handoff_dir/native-acceptance"
```

若隔离 daemon 的 socket 目录校验仍失败，保留实际 binary、uid、目录 mode、配置与日志证据，先解决这个资格测试环境问题；不能把测试跳过、mock 或重启公共 daemon 当作替代。新增 native live 测试从未注册 native thread 开始，先真实读取/纳管再在原 thread 续接两轮。continuation live 测试已扩成创建加两轮续接，其中 peer daemon 显式加载同 thread idle，验证原 thread 路由。仍需外层确认真实 Desktop 的 owner 行为和真实 active owner 不被打断。

所有所需验收通过后，在 review worktree 显式提交以下文件（严禁 git add -A）：

```sh
git add -- app_server.py bridge.py bridge.toml m9_integration.py mcp_server.py native_history.py native_provider.py provider_qualification.py task_registry.py thread_identity.py docs/THREAD_IDENTITY.md docs/NATIVE_INTEROP_HANDOFF_20261002.md test_bridge.py test_m11.py test_m12.py test_m13.py test_m13b.py test_m7.py test_m9.py test_native_interop.py test_native_interop_live.py test_pvx1807_remediation.py test_thread_identity.py test_tunnel_child.py test_worker_continuation_live.py
git diff --cached --name-only
git diff --cached --check
git commit -m "fix: interoperate with native Codex threads and hand off writers"
git push -u origin "$repair_branch"
```

Canonical 集成：为该分支创建面向 origin/main 的独立 PR，明确附上真实验收结果和原失败不可变证据；只合入上述 25 文件。最终 merge 与共享服务激活由外层正式发布入口完成。本 worker 不替它执行，也不重启公共 Codex daemon。普通 registry 启动迁移可加入新 failure 字段，但查询本身不可迁移。

激活后从外层刷新 CLINX connector metadata，并读取 effective service/process source、tools/list、capabilities。必须直接重试上方完整原 URI，确认 query 未被剥离且 context AVAILABLE；再检验裸 ID+host 等价。用隔离未注册 thread 验证正式 adoption 工具、重复幂等、active 仅观察。用户 ORION thread 仍不得收到执行 RPC。

最后按正式 status 当前值，对原 smoke task `task_9295558bb6d04de1bdf843a59a28f550` 走现有 prepare/start（当前只读快照为 ACTIVE，通常 task_action=continue）。新请求只授权一次 working_directory 和一次 host_identity，保持原 thread `01a0fa4f-7a17-7ad0-8135-d6e5674cab72`，禁止 fork/迁移代替。验证新独立 execution、两条 ACK/DELIVERED、exact turn/Host 归属，并再次读取旧 exec_f432… 保持历史失败。这个外层真实同 thread continuation 才能关闭 writer 交接项。
