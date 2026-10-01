# 原生开发执行与受控发布边界

2026-10-01。本次为本地源码修复，不代表 CLINX 运行态已经升级或 ORION application 已部署。

## 已实现的开发默认值

新开发任务默认 `SANDBOX_WORKSPACE`；明确请求 `network_access=true` 时使用既有
`NETWORKED_SANDBOX`。项目在本机、可写、已注册或 HostExecutor enabled 都不再隐式授予
Host 权限。`build_execution_policy`、`build_development_policy`、prepare 和直接 dispatch
使用相同默认规则；start 使用 prepare 封存的策略，不重新推断权限。能力报告中的
`host_executor_default=false` 与新任务实际路由一致。

原生路径不注入 Host-only 指令、不注册 Host 动态工具、不依赖 `development_command`
的 argv 白名单或 HostExecutor 的 PATH。Codex sandbox、approval、网络边界和宿主 OS
限制仍然有效；原生工具受阻时必须报告阻塞，不能改走其他通道。native prompt 保留
`CLINX_EXECUTION_RESULT` 结果格式和禁止嵌套 execution 的说明。
受管 native 执行沿用已有的精确 task/execution/thread/turn 结果收尾机制；监听完成事件
不需要 Host handler，结果解析、证据和租约释放仍由原机制决定，不能仅凭 Provider 完成记 PASS。

显式 `HOST_EXECUTOR` 及显式 Host capability/class 请求保持兼容。它们仍须通过既有
policy、route、lease、target 和 operation 校验。开发权限不包含生产 mutation；
`PRODUCTION_MUTATION` 仍要求独立的显式 intent，且 intent 不代替外层任务授权或平台安全检查。
既有 sealed policy 的 continuation/reopen 不升级、不回退；改变新任务默认值不改变历史权限。

## 长期职责

- CLINX：一次性任务目标授权、身份与租约、启动/恢复、进度与证据。
- Codex：在批准的原生 workspace 中读取、编辑和运行开发工具。
- 部署器：校验制品 SHA/摘要、参数、plan、迁移、依赖、幂等性和恢复条件。

生产长期应采用一次配置的受控工作流入口和任务级目标授权；版本、制品 SHA 和 plan
作为经部署器验证的参数，不作为每次新增的注册项。不新增 `orion_controller_*_<sha>`
或 release 专用脚本，不建立第二条竞争发布链。**通用生产入口及其任务授权联接尚待实现和验收**；
本次没有开放生产权限、注册/激活入口或验证生产发布。

Host receipt、结果投递、任务状态是三个独立事实。receipt 已完成而 Provider 报错时，
只恢复结果投递/对账；操作未知或已完成时不得为恢复通信重放 mutation。
controller adoption 完成不能当作 application deploy 完成。

## 旧任务重新授权：当前无正式接口

源码核查：`mcp_server.py` 的正式工具只有 prepare/start/cancel 及上下文查询；
`ClinxIntegration.prepare_execution` 拒绝覆盖 sealed policy；
`TaskRegistry.update_routing_identity` 保持 surface/authority 等字段不可变。
`TaskDispatcher.migrate_provider_thread` 仅支持 `DYNAMIC_TOOL_SCHEMA_UPGRADE`，
不是 policy 重新授权；它现在也必须具有现存的显式 Host policy。
因此没有可提供的已实现重新授权调用。不得修改运行库 `tasks.sqlite3`、伪造审批，
或通过新任务/默认回退将旧授权升级。

待实现的最小接口设计（以下名称是提案，当前不可调用）：

- `prepare_policy_reauthorization(task_ref, expected_policy_hash, target_policy, goal_scope, reason)`：
  由正式外层操作入口提交，返回不可变请求、权限差异、原/目标策略摘要及有效期。
- `apply_policy_reauthorization(prepared_reauthorization_ref, approval_receipt_ref)`：
  校验真实外层批准与平台准入，原子比较旧摘要；写入不可覆盖的策略版本及审计事件，
  仅影响同一 canonical task 的未来执行，不隐式启动、恢复、重放或部署。

验收必须覆盖：越权、伪造/过期批准、摘要变化、并发 lease、运行中或结果不明 mutation 均拒绝；
历史 execution/receipt/route 保持原版本；重复 apply 幂等；原生/Host 会话能力重新绑定受验证；
封存任务的生命周期变更仍需明确授权；整个过程不产生竞争 task、发布链或自动 mutation。
接口未完成前，旧 ORION 任务 `task_1fbb90f236d84e3f918b3fbb864024ad`
的 policy 重新授权仍为 `BLOCKED`。

## 本地验证与限制

回归入口：

```sh
python3 -m pytest -q test_native_workspace_policy.py test_bridge.py test_m6.py \
  test_m9.py test_m12.py test_m13.py test_m13b.py test_m13c.py \
  test_pvx1812_completion.py test_tool_delivery.py test_pvx1807_remediation.py test_provider_adapters.py
python3 -m pytest -q test_m5.py test_m7.py test_m8.py test_m10.py test_m11.py
CLINX_LIVE_DELIVERY_ACCEPTANCE=0 python3 -m pytest -q
git diff --check
```

最终路由/权限/完成投递相关回归：`336 passed, 57 subtests passed`；其余 M5/M7/M8/M10/M11
回归：`58 passed, 18 subtests passed`；diff 检查通过。
全套试跑（最后一次 native prompt 格式调整前）：`632 passed, 6 failed, 1 skipped,
102 subtests passed`，不能记全套 PASS。3 项 Observer HTTP 测试因 sandbox 禁止创建 socket
而 `BLOCKED`；3 项 tunnel launcher 测试子进程退出 1，具体根因未验证。
该 launcher 绑定运行态配置/数据库路径，不放宽权限或改配置重试。真实 Provider 验收显式禁用并跳过。

测试使用临时 SQLite/仓库和 fake/scripted Provider，覆盖默认/显式/联网 native、显式 Host、
旧 sealed policy continuation/reopen、未授权生产拒绝、能力/实际 route 一致、无 Host-only prompt，
以及 native 自动收尾与精确身份校验。已有 Host 安全负向测试和结果投递不重放回归继续保留。
真实受管 Provider、生产工作流、ORION application 发布和生产权限均未验证。
本次不重启服务、不推送、不部署 CLINX、不修改 `bridge.toml` 权限或 Monitor 工作。
