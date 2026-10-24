# CLINX Unified Task Lifecycle v1 — DEV 实现与有界验收

历史记录：2026-10-24 [第四轮清理](cleanup/RUST_MIGRATION_CLEANUP_PHASE4.md)已删除
Python Centre 自动入库类和 `reconcile-native` 入口。Mac 节点的身份校验与 handoff
仍保留；本文的历史验收不表示这些能力已迁移到 Rust。

本分支为 **DEV / PARTIAL / NOT_DEPLOYED**。已实现真实 Canonical Task 注册链路，
并以真实 P620 原生历史、正式 registry 的只读隔离副本和真实多进程传输验收。
不表示 Air/iMac 实机、macOS GUI、原生任务的双向取消/恢复或生产部署已经通过。

## 权威和工作区

2026-10-18 的 canonical 仓库为 `/home/pvxlabs/dev/clinx`；默认分支是 `main`，
远端无 `master`。开发基线 `8e0077eb2b2b082d936994efcab58b02bcc707dd`。
主工作区的未提交转发/原生完成修复不属于本分支，也没有复制或覆盖。
复用本任务已存在的 `task_381aad17f3e141029acf4ee07f13f122` 工作区 lease；
DEV worktree、日志和 fixture 位于 `/data/artifacts/clinx-unified-lifecycle-20261018/`，
推送后清理 disposable checkout 和可再生数据，保留 branch、bundle、patch 和验收 receipt。

只读确认的入口：`clinx.service`、`clinx-execution-owner.service`、
`clinx-node-centre.service`、`clinx-observer.service`、`clinx-provider.service` 和
`clinx-tunnel.service`。实际服务混合使用 canonical cwd 和 `.local/lib/clinx-*` 发布制品；
本分支不是运行版本，没有服务 restart、配置激活、生产 registry/授权库写入或 ORION 控制。

## 实现链路和身份

`CodexObservationSource` → 既有 collector/outbox → 已认证的 `ObservationWriter.accept`
→ `NativeTaskLifecycle` → 既有 `tasks`；`NodeRegistry.thread_index` 增补 canonical 索引。
既有 Observer HTTP/MCP、`ThreadIdentityReader`、`find_task/get_context/get_status` 和
Monitor 的主列表/详情消费同一 Task ID。未增加第二套任务表、执行系统或 Task Manager。

`native_task_bindings` 仅为关联索引：主键 `(node_id, provider, native_thread_id)`，
关联已有 `tasks.task_id`，保存 user、精确 project、origin 和首次/最新 observation provenance。
node_id 必须是配对后的持久节点身份，且配置 fingerprint 与当前注册身份一致；
首次 fingerprint 不符会拒绝重用。host alias 不能充当跨节点合并证据。
原生新任务 ID 由这三个身份字段做 UUIDv5 派生，SQLite `BEGIN IMMEDIATE` 原子登记；
重复上传、竞态、重启和离线重连复用同一 ID。不同节点同一 thread UUID 形成不同 Task。
精确 native 读取也按 host 限定关联，另一主机相同 UUID 的旧受管关联不参与 owner 选择。
旧受管 `conversation_bindings` 仍保留全局 thread 唯一约束；若这种跨主机 UUID 碰撞任务
需要显式转为受管执行，应先完成该旧契约的命名空间迁移，不能绕过冲突强行 adopt。
`find_task` 支持精确 Canonical Task ID，排序优先于标题和 task_key 的模糊匹配。

先查既有 conversation binding、精确 execution route 和明确的 thread lineage；有唯一
既有 owner 就关联原 Task，不按标题或相似 cwd 合并。多个 owner 冲突时拒绝自动合并。
远端声称的 task_ref 必须在 canonical registry 中有对应 route，未验证的引用不作为
已注册 Task 对外展示。明确的受管 lineage 可令多个线程属于同一 Task；没有证据不会猜测。

## 自动登记、历史和容量边界

自动登记需同时满足：当前 bilateral sharing/trust/user/project/provider 允许观察，
显式精确 node/fingerprint/host/workspace/project 映射，持久 UUID thread，真实 turn，
顶层 `cli/vscode/desktop/codex_cli/codex_desktop` 来源，以及 index 用户事件或已读取的真实
user message 证据。`exec` 临时调用、subagent、guardian、未知/测试 client、无用户事件或
无 turn 的占位线程不自动登记。没有映射的 fixture/临时 cwd 不会因有进程而变成用户任务。
这是一条保守的 v1 产品边界；有实际用户内容的 `exec` 批任务也不在默认自动登记范围内。

真实样本证明 `has_user_event=0` 不能直接等价于没有用户意图；collector 只在实际读取到
历史 user message 后补充布尔证据，不修改 native index，也不根据标题推测。

首次配置必须指定固定 `since`；早于该时刻的线程不会因重新出现就自动导入。
每批上传最多 32 events，collector 沿用 16 行索引页、增量 checkpoint、有限 outbox/retention。
自动新增额度默认持久化 1000，初始可配置 0..10000，耗尽返回 `AUTO_REGISTRATION_LIMIT`；
重启不重置额度。历史 backfill 默认预算 0，每次显式补给上限 1000，一页 1..100，
保存 cursor，可暂停。既有已注册任务的同步不消耗新增额度。
注册查询限制返回 owner 数量，并有 SQLite 时间预算；不可证明时记录 unavailable 而非无限扫描。

`native_lifecycle_control` 仅存预算、暂停、since 和 cursor，不是一份 task 数据表。
历史补录只读取现有观察缓存；旧 collector 未提供用户证据时需由升级后的 collector
重新观察，不能为了导入而伪造证据。现有缓存淘汰、缺页、源重建仍显示 coverage/gap。

## 生命周期、历史与权限

原生 turn 的 SOURCE 为 `CODEX_NATIVE`；只有真实 CLINX execution route/ref 才标
`CLINX_MANAGED`。task origin 和每个 turn 的执行来源分别表示，不混用。
原生观察不创建 `execution_ref`、prepared execution、execution/history/result、lease、
checkpoint、Linear 审计或执行批准。历史由既有 `turns/events` 服务，audit 返回
node/stream/seq/digest；首次登记保留 observation_id、source_generation、fingerprint 和 digest。
事件保留策略可能淘汰正文，不能把 digest 当作完整历史仍在的证明。

完成/失败/取消/中断/暂停取自真实 native turn；archive 来自 native 元数据。
同 turn 已证明终态不回退为 RUNNING；重开/恢复需要后续真实 turn。
持久 RUNNING 只代表历史事实，registry 将其存为 UNKNOWN、codex_running=false；
UI 的运行态要求精确 thread/turn owner liveness、在线且证明在 30 秒内。
离线保留已知终态，非终态显示 UNKNOWN；不推断业务 PASS。
正式副本里的结果 `COMPLETED` 表示执行事实，不能映射成 business PASS。

迁移仅增加索引/控制表及缺失列，不重写旧 Task/Execution provenance。
旧 producer 没有新字段时保持原有规范化字节和 replay digest；回归固定校验
`8e0077e` 的历史 digest，不通过更新历史 hash 消除冲突。
原生-only v1 Task 详情和列表也重新检查当前 sharing；缺少目录或撤销共享时 fail closed。

本地显式 adopt 复用已经自动登记的 Canonical Task；`task_created=false`，不会派发。
显式 approved prepare 可以沿用原 Thread，现有 sealed preparation/start、单 writer、
活跃 owner 拒绝和正式 cancel execution 接口仍是唯一受管控制入口。
观察/发现不会触发这些入口。活跃 native owner 的 prepare 在写 preparation 前拒绝。

**控制闭环增量：** [隔离 DEV 资格与未完成验收](UNIFIED_TASK_LIFECYCLE_V1_CONTROL.md)
记录 canonical identity 的授权远端传播、无 execution_ref 的原生 turn 精确取消，
以及沿用既有 prepare/start 的同 Thread 继续。仅 advertise
`execution.native_canonical_v1` 的 owner 可接收；旧节点仍拒绝
`NATIVE_REMOTE_HANDOFF_NOT_QUALIFIED`。这不代表正式节点已部署，也不代表
Desktop 并发操作或三设备 GUI 整体闭环已通过。

## 当前 App 与 Codex UI

保持原有 Monitor 主列表、筛选、Inspector 和 Activity；原生 row 使用真实 task_ref，
从 observation_id 读取 native 历史，避免重新落回 synthetic Task ID 或调用错误的 v1 执行详情。
空 canonical snapshot 与 native row 同 ID 时选择 native 历史投影；已有精确受管 execution
保持原来的 execution 视图和优先级。打包清单已包含新增 Python 模块。

详情显示 Task ID、Thread ID、SOURCE 和原生深链。原有全网视图增加取消/中断/暂停/归档/
未知/离线筛选；后端按真实最新 turn ordinal 加稳定 id 排序，分页绑定授权和筛选。
主 Monitor 的既有七态视觉系统保持不变，暂停/归档等细分事实保留在 stage/详情和全网筛选，
未冒充额外业务成功态。分页不会保证未经加载的全量任务都在当前 500 行窗口内。

Codex 原生仍显示和执行原 Thread。实测 `codex-cli 0.160.0` 独立 app-server 的
thread/start/read 返回真实 Thread ID；没有在本轮接入或验证可向 Desktop 扩展显示 Task ID
的官方 metadata API（返回字段含 extra 也不等于可安全写入 UI 扩展）。不修改闭源 App。
CLINX 的 Task→Thread 深链和按 Thread 反查 Canonical Task 可用；Codex 原生 UI 中
显示 CLINX Task ID、按钮或额外双向导航没有验收。

## DEV 入口

默认不启用自动写 canonical registry。DEV centre 的 `serve --canonical-lifecycle-config FILE`
显式启用；Observer/MCP 配置 `CLINX_CANONICAL_TASK_DB` 指向同一隔离 registry。
示例配置的所有路径必须换成任务专属 fixture，不能指向正式库：

```json
{
  "canonical_db": "/data/artifacts/TASK/fixtures/tasks.db",
  "user_scope": "fixture-user",
  "since": "2026-10-18T00:00:00Z",
  "auto_budget": 1000,
  "routes": {
    "p620": {
      "host": "p620",
      "fingerprint": "EXACT_FIXTURE_IDENTITY_FINGERPRINT",
      "projects": {
        "/fixture": {"workspace_alias": "p620", "project_alias": "fixture"}
      }
    }
  }
}
```

显式控制/补录使用同一 `node_centre_entrypoint.py --state FIXTURE_STATE --node-id p620
reconcile-native --registry FIXTURE_NODE_DB --observations FIXTURE_OBSERVATION_DB
--canonical-lifecycle-config FILE --backfill-budget 10 --limit 10`。
`--paused yes` 暂停；`--paused no` 恢复；`--auto-budget N` 显式补给自动额度。
这些是本地 operator 行为，不从 observation payload 接受指令。

## 证据和验收状态

所有原始日志、版本/依赖哈希、sample receipt 和最终远端 SHA 位于上述 artifacts 目录。
最终相关回归为 **254 passed, 54 subtests passed / 22.11s**；三节点独立进程验收
为 **1 passed / 21.59s**，源状态到 HTTP 可见延迟为 **3.807 / 4.134 / 3.972 秒**。
新增文件 Ruff、相关 Python 编译、打包脚本 shell 语法及 Git whitespace 检查通过。
原始证据见 `final-tests.log` / `process-final.log`；提交和远端核对见 `git-receipt.json`。

| 验收项 | 状态及证据范围 |
|---|---|
| Python 相关回归 | PASS；registry、观察、精确 context/status、prepare/adopt、Observer schema、迁移、竞态、预算和授权回归 |
| 三节点传输 | PASS / DEV fixture；独立 centre + P620 本地 collector + Air/iMac node helper + Observer HTTP + MCP，真实 OPAQUE/mTLS |
| 取消/暂停/完成、离线重启 | PASS / 真实进程读取隔离 native-history fixture；source SQL commit→HTTP 约 4 秒，非 Mac UI 时延 |
| 原生真实 provider | PASS / 独立 CODEX_HOME 的官方 app-server thread/start/read；未开始用户 turn，未做完整新 turn→登记正向验收 |
| 指定 P620 旧线程 | PASS / 真实历史只读→隔离 registry，见下方精确标识；正式仍 THREAD_UNBOUND |
| ORION 对照去重 | PASS / 正式 DB read-only backup replay，Task/Binding/Execution/History/Result/Lease 六表对照行未变，新增 Task=0 |
| macOS SwiftUI build、启动、交互 | BLOCKED / P620 为 Linux；SwiftUI/AppKit 和 Swift toolchain 不在当前环境，未访问远端 Mac 构建 |
| Air/iMac 真机自动登记/控制 | NOT_RUN；现有注册库三个节点当时均有在线 heartbeat，但不是本分支 runtime |
| 原生-only 双向取消/远端接管 | PARTIAL；缺少 canonical owner 传播/正式 native control 资格，明确拒绝二次派发 |
| 生产激活/正式样本绑定 | NOT_DEPLOYED / NOT_RUN；没有修改正式 registry、grant 或服务 |

指定真实原生线程：`01a14abb-124e-7413-868d-d1ced3703081`，隔离 canonical
`task_ff18a3d5d3b75c7b9f1274c8f6ed094c`；`THREAD_UNBOUND → RESOLVED / NATIVE_REGISTERED`，
`context_status=AVAILABLE`，`execution_ref=null`，SOURCE=`CODEX_NATIVE`，限额 backfill=1。
原 URI 的 `hostId=remote-ssh-discovered%3Ap620` 保留；原 native 元数据不变，正式库仍未绑定。

ORION 对照 `task_e7f16a8e7d604ba78200b07605ce3547` /
`01a15005-33d0-7490-a095-6ec136f21482` 仅在隔离备份登记索引，仍关联原 Task ID。
没有为了验收对它做 adopt/start/cancel/resume，也没有修改 ORION 文件或业务状态。

下一步入口是本 feature branch：先在授权的 macOS DEV 环境构建/运行 App 并核对选中、
Activity、过滤与深链；再用三个节点专属新建 DEV 原生 turn 做正向验收。
远端控制需先完成 canonical ID 到单一 owner 的传播和原生-only cancel/resume 授权协议，
仍沿用既有 writer fencing。生产激活应是后续独立授权任务；本分支不合入 main。
