# CLINX 全网任务中心 v1：P620 实现与验证报告

本轮 **P620 实现交付范围通过**；全网产品最终验收 **FINAL_STATUS=BLOCKED**。完整后端、正式启动入口、Mac 源码和 Swift 测试已经交付；真实 Air/P620 Monitor 同屏、Mac 原生构建/截图、iMac 和共享运行时激活尚未执行。隔离 fixture 不能代替实机证据。

## 身份、来源与边界

- 实际主机/用户：workstation-p620 / pvxlabs；当前正式 CLINX 受管 execution 推进，无嵌套任务或替代后台 Agent。
- 唯一产品工作区：/home/pvxlabs/dev/clinx-network-observation-v1-20261004。
- 分支：codex/p620-network-observation-v1-20261004。
- 基线：6a6ddd18ed5f2e1cb35c399e214c282affabcf69。
- 冻结产品源码 SHA：**1113952c8397ee97f1501f88b291cb6f0f921960**。后续交付提交只增加文档；交付 HEAD、源码包摘要和实际推送读回记录在 .validation/network-observation/DELIVERY_MANIFEST.json。
- 继承 Air 补丁 SHA256：2418fe9a95774a65b038ed85ecd9b8cc697ab477309b862ef95c69f3d7362501；已单独提交 **9c0bbf1**，保留来源会话 01a105a5-8667-79e3-afdb-6e6db6a97f65 的归属，没有重复 apply。
- 并发修复提交：5318a00；产品提交：16eec17、87c6a2d、b7145d7、1113952。
- 本轮未修改 main、原 Air 工作区、旧 execution、共享节点身份/授权、现有运行服务或 ORION；没有等待或伪造旧文档要求的 Air RUN_RECEIPT。

原始 prerequisite-baseline.log 保留在 .validation/network-observation-handoff/，结论为 **37 passed / 1 failed**。上一环境的 262 passed 不是本轮基线。

## 并发根因与修复

ThreadIdentityReader 已使用 BEGIN 的只读快照。实际问题在 TaskRegistry.release_execution：连接使用 isolation_level=None，关闭可选 shadow ledger 时，归档 INSERT 和活动行 DELETE 各自 autocommit。读者因而可能在一致快照里看到同一 execution 同时存在于 active/history，正确触发 THREAD_IDENTITY_CONFLICT。

修复将归档/释放迁移显式纳入单一事务，不依赖 shadow ledger 开关；没有吞冲突、扩大允许错误集合或关闭 fingerprint 守卫。新增测试在 DELETE 前强制从另一连接读取，只可见迁移前 (1,0)，提交后为 (0,1)；另有 12 组并发 Finalizer/status/context 回归。

## 已实现的产品路径

CodexObservationSource → ObservationCollector → 既有 NodeRegistrationClient / OPAQUE+mTLS → CentreService → ObservationWriter → ObservationDirectory → 正式 Observer/MCP → Mac 全部设备列表。

中心本机通过既有 NodeIdentity 与**显式本地 read_sessions 批准**接入同一采集/写入逻辑，不自配对、不创建替代 P620 身份。远端范围始终取源端与中心持久批准的交集。

- **身份与授权**：observation_id 绑定 node_id、user_scope、provider、native_thread_id；task_ref/execution_ref 可空。可信 canonical route 与原生同一 turn 合并，历史轮次保留；显示名、地址不参与身份。复制同 thread 明确显示 MULTI_SOURCE_THREAD_IDENTITY 并禁用控制映射；外机 canonical owner 不归到前端。
- **自动采集**：直接读取当前用户 Codex 索引，分页 bootstrap、时间戳+ID 游标、重叠窗口、轮转校验及活跃/历史轮次采集。没有复制 .codex、运行中 SQLite 或扫描所有历史正文来发现会话。支持 Codex Desktop、CLI 的 paginated/legacy 持久化来源和配置的 canonical 只读来源。
- **持续记录**：独占采集锁，原子 outbox/checkpoint，序号 ACK、幂等摘要校验、重复/乱序拒绝、来源重建 CAS、断线缓冲与补传。中心保存来源、轮次快照和允许字段 Activity 事件，离线仍可查询已接收内容；完整正文仅按需分页回源。
- **有界策略**：索引分页 16；增量/校验/已知来源候选总数最多 56；legacy 每次每线程 64 KiB；SQL 使用既有步数预算。节点最多 20,000 个已知会话、2,048 个待发事件，满额背压而不丢失检查点。单次最多 32 个事件且限制帧字节。中心最多 20,000 个目录项、50,000 个轮次和 50,000 个事件；淘汰计数、来源检查点、gap、待 bootstrap 均可查询。
- **隐私**：明确字段 allowlist；不收集 reasoning/analysis、原始工具输出或凭据。用户可见摘要/结果再脱敏并限长。项目路径精确匹配；旧 whole-user 批准的 * 保持兼容。授权缩小后，不允许的排队内容只发送无正文序号标记，公开 SCOPE_WITHHELD。
- **状态**：原生 turn、canonical execution、业务结果、新鲜度、覆盖分开。completed 不推导 PASS；缺结果头显示“执行结束·结果待处理”；离线不改成任务失败；旧 running 不覆盖终态；无实时 owner 证据保持 NOT_PROVEN；无分母时百分比为空。
- **纯读服务**：Observer/MCP 使用同一 ObservationDirectory，不调用 canonical get_status、不构造 TaskRegistry、不启动 completion runtime。只读身份存储视图复用验证，但不创建身份、目录或可写锁。MCP 有 --observations-only 模式；Observer launcher 使用 -I -B，仅加入已安装受信任包路径。
- **Mac 源码**：沿用品牌/组件，默认全部设备；设备选项来自中心授权来源，支持项目、状态、外部/受管筛选。短标题、详情、来源、更新、新鲜度、结果、Activity 和轮次历史接入正式 v2 API。请求代次防串详情；历史分页暂停时间线替换，无跳底调用；网络解码位于 ObserverClient actor，列表最多 500，历史/Activity 最多 512 条。
- **流转边界**：提供节点、原 thread、task/execution 与正式入口映射，判断独立执行授权和 Provider 能力。复制入口供既有控制面使用；继续/取消按钮解释并保持受控。没有绕过正式授权的 UI 写接口、隐式 adoption、Provider 控制或 lease 获取。

其他 Agent 明确 NOT_IMPLEMENTED；未持久化且 Provider 不公开的临时会话明确 UNOBSERVABLE。初次接入前已丢失的原始历史不会被恢复成虚构事实。

## API 与兼容性

既有 /v1/tasks、Activity 和 v1 Swift ObservedTask 合同保留。全网使用新的可空 canonical 引用模型，**不把 observation_id 填入 task_ref**。

| 正式 Observer GET | 同源 MCP |
| --- | --- |
| /v2/observations | clinx_list_observations |
| /v2/observations/{observation_id} | clinx_get_observation |
| /v2/observations/{observation_id}/activity | clinx_get_observation_activity |
| /v2/observations/{observation_id}/context | clinx_get_observation_context |

目录支持 node/project/state/kind、1..100 条分页；HMAC 游标绑定用户、当前授权、筛选/观察项、来源代次与保留代次。离线未缓存正文返回 UNCACHED_CONTENT_UNAVAILABLE。合同版本 clinx-observation-v1；schema 位于 docs/monitor/observation-v1.schema.json。

## 实际验证

项目依赖与 OPAQUE 扩展在本工作区 .validation/network-observation/venv；构建和 uv 缓存位于同一证据目录。使用隔离 HOME/XDG/CODEX_HOME、数据库和 loopback listener；未安装到系统/共享 Python 环境。

最终完整相关命令：

~~~sh
.validation/network-observation/venv/bin/python \
  .validation/network-observation/run-tests.py \
  .validation/network-observation/release-qualification.log -q -s \
  test_network_observation.py test_network_observation_swift_contract.py \
  test_network_observation_process.py test_air_result_contract.py \
  test_thread_identity.py test_native_interop.py test_node_runtime.py \
  test_node_protocol.py test_node_control.py test_observer_server.py \
  test_observer_activity.py test_observer_schema.py test_m12.py test_m6.py \
  test_pvx1812_completion.py test_result_ingestion.py
~~~

实际通过已批准 Host 的 python3 subprocess 包装执行上述命令。

| 检查 | 实际结果 |
| --- | --- |
| 冻结候选相关资格检查 | **289 passed, 28 subtests passed，62.17 秒** |
| Python 修改模块语法 / launcher shell 语法 / git diff --check | PASS |
| 正式 Observer launcher -I -B 独立进程 | PASS |
| Air fixture mTLS + P620 本机 fixture → 中心持久化 | PASS |
| 独立 Observer、MCP、两个独立 HTTP 客户端身份/轮次/状态一致 | PASS |
| 自动出现、受管合并、离线保留、中心重启、断线恢复 | PASS |
| MCP 详情、按需源端正文、离线五次进度 Activity | PASS |
| 同时间戳、迟到终态、重复/乱序/gap、重建、游标、撤销 | PASS |
| canonical fixture DB 字节不变、Provider 调用/lease mutation | PASS，0 / 0 |
| Swift 模型/客户端/状态/UI/测试与 Python JSON 契约 | 源码已交付；JSON 契约 PASS |
| Swift build / swift test / Mac GUI 与滚动截图 | **NOT_RUN**，P620 无 swift/xcodebuild |
| 真实 Air 样本进入真实 Monitor / iMac / 共享激活 | **NOT_RUN** |

测试组相互重叠，不能相加。原始继承失败、依赖收集失败、错误测试路径和中间测试脚本换行错误均保留；修复后有独立检查，没有覆盖失败日志。

性能为**隔离 fixture 真实进程**“源端 SQLite 提交 → 独立 HTTP 客户端可见”，不是 Mac 渲染：

- 六样本：3.821、3.990、3.965、4.015、4.182、3.964 秒；样本 p95 **4.182 秒**。
- 5 个服务进程实例（含中心重启）；取样存活进程 RSS 约 45.8–52.1 MiB，累计 CPU 0.15–1.31 秒/进程。
- Air collector 在该测试记录 16 次元数据行采样。规模很小；大目录、真实网络、Mac UI p95 与能耗尚未验收。
- 默认活跃约 2 秒、空闲约 12 秒，不等于任意规模 UI ≤5 秒保证。

## 分层结论

| 指标 | 本轮结论 |
| --- | --- |
| P620_IMPLEMENTATION_DELIVERY | **PASS** |
| NETWORK_INVENTORY | **PASS（隔离来源/产品源码）** |
| READ_ONLY_AUTO_DISCOVERY | **PASS（含本机显式授权）** |
| DURABLE_OBSERVATION | **PASS（缓存、事件、恢复、gap/保留与撤销）** |
| OBSERVER_MCP_UNIFIED | **PASS（正式同源入口）** |
| MAC_GLOBAL_UI | **未验证**：源码和 Swift 测试已交付，原生构建/GUI NOT_RUN |
| AIR_P620_ACCEPTANCE | **未验证 / NOT_RUN** |
| IMAC_ACCEPTANCE | **未验证 / NOT_RUN** |
| CONTROL_ROUTING | **PASS（只读映射）**；实际控制操作 NOT_RUN |
| RUNTIME_ACTIVATION | **未验证 / NOT_RUN** |
| FINAL_STATUS | **BLOCKED（全网实机验收未闭合）** |

P620 开发范围无剩余阻塞。外层可直接使用 docs/NETWORK_OBSERVATION_V1_ACCEPTANCE_20261004.md 串行接续，不要求用户复制任务到另一对话。

剩余风险：自由文本脱敏不能保证识别所有未知秘密格式；源端离线时无法即时传播新源端撤销，中心撤销可立即阻止缓存读取；保留上限外内容明确为 gap；spool 丢失需显式旧 stream CAS，不能盲目替换活动来源；初始回填、大目录资源消耗与真实 UI 延迟待实测。没有声称真实跨机、Mac GUI 或共享激活已通过。
