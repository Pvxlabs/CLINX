# Codex Thread 精确只读查询

用户给出 thread_id 或 codex://threads/... 时，直接调用以下 exact selector。
不要先调用 clinx_find_task，不要将 ID 放进 query。URI 只是资源标识，不能生成网络连接目标。

```python
clinx_get_context(thread_id="01a0a673-d5d6-7761-b9bf-97db5bc7e50c")
clinx_get_context(codex_uri="codex://threads/01a0a673-d5d6-7761-b9bf-97db5bc7e50c")
clinx_get_status(thread_id="01a0a673-d5d6-7761-b9bf-97db5bc7e50c")
clinx_get_status(codex_uri="codex://threads/01a0a673-d5d6-7761-b9bf-97db5bc7e50c")
```

两个 selector 互斥，禁止同时提供 task_ref 或 query；host/project 仅收窄授权范围。
execution_ref 可联合精确 selector，必须属于该 thread。旧 status(execution_ref)、
context(task_ref)、project+query 保留原 selector 契约；status 的所有 selector 均只读。

## 身份、执行与上下文

复用 conversation_bindings、conversation_binding_lineage、conversation_adoptions、
executions/execution_history 的 route.conversation.binding、prepared_executions 的
resulting_thread_id/resulting_execution_ref、context_checkpoints 与 context_anchors。
没有第二套 thread registry，也不根据 task 的当前 binding 回填历史执行。

当前 binding 每 task 一条；lineage 每 task 保留一对 predecessor/successor；历史 execution
可以有多条，同 thread 多 execution 正常。选择顺序：显式 execution_ref → 唯一活动执行 →
带时区 acquired_at 的唯一最近执行。并列时间、多个活动 owner、关联事实矛盾均 fail closed。
缺少精确 execution 关联返回 UNKNOWN，task_current_projection 不覆盖查询执行状态。

查询使用 mode=ro、query_only、短读事务。读取完成后再次读取相同精确事实并比较 fingerprint；
变化返回 THREAD_IDENTITY_CHANGED_DURING_READ。文件读取期间不持有数据库事务或写锁。
普通 task_ref/status 与 execution_ref/status 也不再自动 reclaim/reconcile/finalize。
task_ref 精确选择当前关联 execution，Host receipts、delivery ledger 与 result 均限制到该 execution；
历史 thread 查询仍限制到被查 thread，不能借用当前 task 的执行事实。
`provider_delivery` 分开汇总持久化 ACK 与 Worker 原始响应描述；
`worker_pending_is_stale_snapshot=true` 表示 Worker 写了等待 ACK，但账本已全部 DELIVERED。
这个标志不修改 Worker 的 BLOCKED，也不替代业务验收。恢复由显式 recovery 或已有 completion runtime 负责。

首版使用已运行本机 Codex 的 state_5.sqlite 中 threads 主键定位 rollout_path；
仅读取 native root 内、文件名精确匹配 ID、session_meta.id/cwd 经核实的文件。
不扫描全部 sessions；复用 TaskContextReader 的消息提取与 byte budget helper。
仅返回 user/assistant message，不返回 tool output 或 reasoning。
读取最多 max_bytes 尾部及 128 KiB metadata header，recent_turns 上限 20，max_bytes 上限 128000。
本机文件不可用时只允许同 thread checkpoint；有 anchor 时还必须匹配 turn。
显式 execution_ref 不会将线程近期上下文伪称为 execution context：context_scope 与 provenance
明确区分 THREAD_RECENT 和 THREAD_CHECKPOINT；checkpoint 关联另行列出。

Provider RPC 次数恒为零：不调用未经证明只连接现有 Provider 的 client_factory，不启动 daemon、
resume 或抢占 writer。旧共享 endpoint 不可用时可使用其已有精确本地历史；无可证明来源即
CONTEXT_UNAVAILABLE。远程主机不会读取本机同 ID 文件。Linear 不参与查询，也不影响成功条件。

## 错误语义与授权

- INVALID_THREAD_ID / INVALID_CODEX_THREAD_URI：严格 canonical UUID 格式，支持 v7，禁止 substring extraction、空白、query、fragment、userinfo、port、额外 path 和编码歧义。
- IDENTITY_SELECTOR_CONFLICT：混用 selector。
- THREAD_SCOPE_MISMATCH / THREAD_ACCESS_DENIED：调用范围不符或不在当前注册 workspace/project。
- THREAD_IDENTITY_CONFLICT：精确事实矛盾或无法可靠选择 execution。
- THREAD_UNBOUND：native exact index 证实存在且授权通过，但无 CLINX 关联；不自动 adoption。
- THREAD_LOOKUP_UNAVAILABLE：索引缺失、来源不可用或无法证明存在性；binding_status=NOT_FOUND 不代表 thread 不存在。
- CONTEXT_UNAVAILABLE：身份仍返回，上下文另行不可用。

本地索引的 miss 不是全部 Provider 历史的不存在证明。本版本不制造 THREAD_NOT_FOUND；
只有未来已授权、完整范围且给出明确不存在证据的 reader 才可使用该语义。
查询结果中旧 running/持久化 execution_state 不证明进程仍活跃，provider_observation 明确 UNKNOWN。

## 迁移、性能与验收

沿用 TaskRegistry 正常初始化迁移，添加六个可重复执行的索引；不修改历史事实或业务行，
查询本身不会初始化 Registry 或执行 migration。缺索引返回明确 unavailable。
部署前通过项目正常初始化完成迁移，或由正式 MCP 启动执行相同迁移。
每张关联表最多取 257 条，超过 256 则 fail closed，避免无界历史读；SQL 全部参数化。
隔离测试使用 10000 条无关 history 和 EXPLAIN QUERY PLAN 检查 SEARCH ... INDEX。
不把单次 latency 样本当作 p99。

```bash
python3 -m unittest -q test_thread_identity
cargo build --release --locked --offline --manifest-path kernel/Cargo.toml
python3 -m pytest -q
```

正式验收必须另外验证共享 runtime 的实际来源、外层 tools/list 与真实 tools/call；
源码或临时 MCP 测试不代表正式激活。Host v2/native workspace 的其他工作流保持单独归属。
