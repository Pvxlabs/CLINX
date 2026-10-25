> 文档分类：REFERENCE。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Exact native conversation reads

When `CLINX_ENABLE_NODE_ROUTER=1` and `CLINX_OBSERVATION_REGISTRY` is configured,
MCP exact thread reads use that existing centre registry. This is the same
registry used by node heartbeats and the observation directory. A missing
configured file fails closed; it must not create a private replacement.
Standalone installations without this setting retain the legacy task-local
node registry.

Binding the local read callback or a cached remote transport does not register
a node, replace its trusted identity, regrant sharing scope, or overwrite
reference routes and writer fences. Local reads reload centre approval on each
request; remote reads retain bilateral mTLS and sharing checks.

An unqualified exact UUID is searched across the authorized node set. Missing
transport, offline nodes, scope denial, malformed identity replies, and native
storage errors leave coverage incomplete. `THREAD_NOT_FOUND` is returned only
when every authorized candidate explicitly confirms exact absence. A positive
match can include partial coverage; its source node remains explicit.

`THREAD_UNBOUND` with `provider_existence=CONFIRMED` and
`context_status=AVAILABLE` means the native conversation was read successfully
without a managed CLINX execution association. No adoption, provider start,
replay, task creation or lease acquisition is needed to read it.

Regression: `tests/test_shared_read_registry.py` exercises the public MCP boundary,
shared identity/grant preservation, live registration/revocation, unavailable
transport, and uncertain node responses. Runtime acceptance also requires an
exact URI read through the deployed external connector; a local unit pass alone
does not prove ChatGPT access.
