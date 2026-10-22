# 脚本与启动器

从仓库根目录运行。开发安装见[贡献指南](../CONTRIBUTING.md)，常用验证命令见
[测试指南](../docs/TESTING.md)。

| 入口 | 用途与副作用 |
| --- | --- |
| `python scripts/run_tests.py basic` | 无需生产服务的基础测试，隔离 HOME、SQLite 和临时目录 |
| `python scripts/run_tests.py collect` | 全仓库 pytest 收集，不执行测试 |
| `python scripts/run_tests.py owner` | 真实隔离 Owner/socket/SQLite；需显式构建 Owner client |
| `python scripts/run_tests.py transport --temp-parent <task-artifact-dir>` | 隔离发布/传输回归；要求 `/data/artifacts` 下临时目录及精确外部制品 |
| `python scripts/run_tests.py recovery` | 恢复与故障回归；其中 transient systemd 用例需要隔离 user manager |
| [check_tunnel_health.py](check_tunnel_health.py) | `--url-file`、可选 `--max-age`；只读核对 PID 和 poll 新鲜度 |
| [check_embedded_bridge_idle.py](check_embedded_bridge_idle.py) | `--base-url --tunnel-pid --bridge-pid`；只读，不 Warmup、回收或接管 |
| [reconcile_task_projection.py](reconcile_task_projection.py) | `--database --execution-ref --check-only` 为检查；省略 `--check-only` 会应用精确维护 |
| [reconcile_historical_handoff.py](reconcile_historical_handoff.py) | 先 `--check-only`；应用需 `--expected-sha256`，记录 handoff 对账事实 |
| [build_transport_runtime.py](build_transport_runtime.py) | 指定源码及 Go 归档，构建 transport runtime 到 `--output`，不安装 |
| [build_tunnel_release.py](build_tunnel_release.py) | 构建候选、manifest 与回退 capsule，不操作服务 |
| [build_forwarding_release.py](build_forwarding_release.py) | 构建 parser 和配对 Tunnel/health 候选 |
| [tunnel_release_cas.py](tunnel_release_cas.py) | `check` 只读；`fixture-apply` 只接受带 marker 的隔离根目录 |
| [deploy_forwarding_release.py](deploy_forwarding_release.py) | `activate` / `rollback` 写配置并操作服务，需要对应部署授权 |
| [qualify_bridge_transport.py](qualify_bridge_transport.py) | 固定上游 Bridge 源码的隔离 generation-fence 资格 |
| [qualify_execution_owner.py](qualify_execution_owner.py) | 使用临时 registry，但调用真实已认证 Provider，不能当作离线测试 |
| [local_discovery_smoke.py](local_discovery_smoke.py) | 显式启用的真实 mDNS / OPAQUE / mTLS 网络资格 |

源码启动器：[CLI](../bin/clinx)、[MCP](../bin/clinx-context-mcp)、
[Execution Owner](../bin/clinx-execution-owner)、[节点中心](../bin/clinx-node-centre)、
[Observer](../bin/clinx-observer)。这些入口可能写状态或启动服务；已安装服务通常
使用独立的 release，不因编辑源码而升级。

一次性实验资格器随对应实验退役。authority 测试改用统一 `owner` 入口，
用 `--schema <schema-only.json>` 显式提供迁移输入。历史回执位置见
[文档索引](../docs/README.md#历史资料恢复)。构建与测试只回收本任务拥有的输出；
部署包、manifest、数据库、回退和证据按各自运行契约保留。
