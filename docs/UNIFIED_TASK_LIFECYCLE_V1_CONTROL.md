> 文档分类：SUPERSEDED。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Unified Task Lifecycle v1：原生控制 DEV 增量

状态：**PARTIAL，未达到整体生命周期验收标准，未部署。**
基线：`790b568369fe156fe56cd240ff4e05145729c928`。
分支：`codex/unified-task-lifecycle-v1-20261018`。
本轮 P620 证据目录：`/data/artifacts/clinx-lifecycle-closure-20261018T210514Z`。

## 根因与变更

中心已观察登记的 native Task 原先没有把 canonical 身份传到执行 owner。
直接远端 adopt 会按旧路径分配另一个 Task，因此原先必须拒绝远端接管。
现在在既有 execution RPC 内传 identity-only envelope；接收端需要显式
`execution.native_canonical_v1` capability。旧 helper 不得忽略 envelope 后另建 Task。
中心核对成功 ack 的 task_ref；不一致时返回 UNKNOWN，不绑定错误 route。

NodeService 在双边 execute scope 授权后盖上真实 owner/user/fingerprint，
覆盖外来同名断言。Adapter 在导入前检查 project/thread/cancel allowlist，
再核对本地真实 NativeHistory header、cwd、project、确定性 Task ID 和 immutable
first observation provenance。事务只在既有 tasks/native_task_bindings 中保存同一
identity，不导入 policy、lease、execution 或权限，也不推进观察 freshness。
managed 请求保留旧幂等摘要，避免升级使历史请求重放冲突。

原生取消复用 `clinx_cancel_execution`：

```json
{"task_ref":"task_...","native_turn_id":"exact-observed-turn","approved":true}
```

它与 managed execution_ref 模式互斥，要求 qualified node owner 和取消 scope。
写边界前两次证明同一 exact LIVE owner，在该官方 AppServer 上调用
`turn/interrupt(threadId, turnId)`；不 resume、不 start、不创建 managed execution。
ack 仅表示 CANCELLATION_PENDING；只有真实 turn/completed/后续 observation 才确认终态。
同 task+turn 的请求 ID 稳定，跨重启重放不会再 interrupt。
受管活跃执行继续使用原 managed cancel contract。

继续原生任务沿用 approved prepare/start、活跃 owner 拒绝、sealed preparation、
现有 lease/fencing。新 turn 成为真实 CLINX_MANAGED execution；过去 native turn
保留 CODEX_NATIVE 和 execution_ref=null，不追补伪造执行记录。

## Thread namespace 与迁移

native 主键仍是 node_id+provider+thread_id，Task ID 算法不变。
conversation_bindings/adoptions/lineage 的全局 UUID UNIQUE 改为按既有 tasks.host
隔离的约束；本系统当前仅支持 Codex provider。原 managed 历史只有 host，
不捏造 paired node identity。native route 继续要求 host 一对一与 fingerprint pin。

迁移在现有初始化事务中复制原 row 值，重建必要索引与 immutable lineage trigger；
不改 Task/Thread ID、旧 provenance 或旧关联。同 host 重复拒绝，跨 host 同 UUID
允许；未限定 host 的多 owner 查询返回 THREAD_IDENTITY_AMBIGUOUS。已绑定 Task
不得直接变更 host。真实旧 schema fixture 的逻辑 row 值在迁移前后相同，重启稳定。
这是前向迁移；旧程序不应直接打开已包含跨 host 重复 UUID 的新库。
新增 provider、复杂多 thread lineage 或非确定性 native owner 冲突仍需另行资格，
当前 fail closed，不自动 merge。

## 验收分层

| 层次 | 结果与证据 |
| --- | --- |
| Python 相关回归 | PASS：regression-final.log，191 tests + 39 subtests；后续兼容修复另见 compatibility-final.log |
| 真实进程/mTLS/observer/helper 回归 | PASS：core-tests-qualified.log，54 tests；是隔离节点 fixture，不是三台物理设备端到端 mTLS |
| 历史 schema 迁移 | PASS：legacy-migration.json，旧 schema 原 row 保留、跨 host adoption/lineage、重启 |
| P620 官方 provider | PASS_SCOPED_REAL_PROVIDER：p620-v7/native-device.json |
| Air 官方 provider | PASS_SCOPED_REAL_PROVIDER：air-evidence/native-device.json |
| iMac 官方 provider | PASS_SCOPED_REAL_PROVIDER：imac-evidence/native-device.json |
| 三设备 provider 测试内容 | 原生自动登记、相同 Task ID handoff、active prepare 拒绝、exact cancel/真实 interrupted、同 Thread 授权继续/真实 completed、native/managed 历史来源、scope 拒绝 |
| 离线/重连 | PASS（隔离注入）：各物理设备 helper 将任务自有 NodeRegistry 标为 OFFLINE 后恢复，Task ID 不变；未实际断开网络 |
| 两个官方 AppServer writer lock | PASS（有限）：writer-fence-v4/receipt.json，第二 daemon resume 拒绝 already has an active writer；不能证明同 daemon 多客户端竞态 |
| Air/iMac Mac 构建 | PASS：两台实际 Mac 上 swift test --filter 'NetworkObservation\|TaskPresentation'，各 8 tests；未安装/替换正式 App |
| 上阶段 Air 可见性代码回归 | PASS（API/单测）：threadSource=user、真实 originator、官方 name/set publication 保留；见 tests/test_thread_source.py |
| 本轮 GUI open | 未验证：Air 旧测试 Thread 的深链只读 AX 检查在 12 秒内未读到预期正文；不能推导 Thread 消失或 UI 成功 |
| 新能力三设备 GUI 闭环 | 未验证：provider 用私有 CODEX_HOME，新线程不在安装版 Desktop 默认 store；没有新 binary 的 live CLINX UI 验收 |
| daemon 离线通知补偿 | 未实现：既有新线程 metadata publication 失败仅记录 UNAVAILABLE，无 durable retry/outbox |
| 正式环境 | NOT_DEPLOYED：未写正式 Task/授权库，未重启正式服务，旧节点继续拒绝未资格 handoff |

三台真机的 scope/身份均为任务自有 DEV fixture；官方 provider 是真实进程，
node RPC 为 in-process scoped NodeService。不要把这些证据合成“三设备 mTLS+GUI PASS”。
provider 收据中的源码 hash 对应 v3 archive；最终源码仅另有 dict 字面量 lint 修复、
managed 请求摘要兼容修复和说明文字，补测见 compatibility-final.log。具体差异保留
在 tested-final-source.patch，不能宣称所有源码 hash 完全一致。

## 剩余入口

1. 官方 AppServer 的 turn/start 没有公开 compare-and-start 前置条件；CLINX lease
   不能约束同 daemon 内 Desktop 客户端。现有 busy guard、幂等、官方跨 daemon
   writer lock 均有证据，但同 daemon Desktop/CLINX 同时启动的 TOCTOU 未完全资格。
   在证明该边界前，不宣称绝对单执行 Owner 的所有竞态已 PASS。
2. daemon 通知补偿需要在授权创建路径记录 bounded metadata-only retry，并在
   disposable daemon 离线/重连上证明；不能把重试挂到只读 observer，也不能重派发 turn。
3. 三设备 GUI 需要使用可供实际 Desktop 连接的隔离 DEV store 与 scoped observer，
   在不替换正式配置/授权或操作已有用户 turn 的前提下逐项核对列表、内容、继续、取消。
   CaptureHarness 的 synthetic 界面不能替代此项。

官方接口依据：https://developers.openai.com/codex/app-server/ 。
没有修改闭源 Desktop、伪装 originator、写原生 SQLite 或新增自动派发行为。
