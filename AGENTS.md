# P620 CLINX project workspace policy

The user requested convergence to one CLINX project repository on
2026-10-13 (Asia/Shanghai). On P620, the canonical repository is
`/home/pvxlabs/dev/clinx`.

- Do not create another long-lived CLINX clone, sibling worktree, pilot, or
  reference project under `/home/pvxlabs/dev`.
- Reuse the existing task coordination and workspace lease. Preserve unrelated
  dirty work; do not reset, clean, or overwrite it to start a task.
- If qualification needs a disposable repository, place it in the task-owned
  `/data/artifacts` directory. Record ownership and dependencies, save unique
  logs and receipts, then remove the disposable repository and regenerable
  build outputs when the task ends. Keep source recovery as refs, bundles,
  patches, or compressed snapshots rather than permanent extra checkouts.
- Keep deployment artifacts, canonical databases, credentials, and rollback
  dependencies under their existing runtime contracts. Repository convergence
  is not permission to remove those resources.
- Do not redirect retired `pilot` or `clinx-local-discovery` aliases to the
  canonical repository; their origin, branch, and path identities differ.

See [the convergence record](docs/PROJECT_CONVERGENCE.md) for recovery evidence.

## 全仓强制工程规则

用途：约束本仓库全部 Coding Agent 和人工代码变更。适用范围：所有子目录、后续任务及发布入口。
状态：CURRENT；验证方法见[贡献指南](CONTRIBUTING.md)和[测试指南](docs/TESTING.md)。
以下规则与上面的工作区、任务协调、数据保护及恢复规则同时生效。

- Rust 是 CLINX 所有后端运行时、业务逻辑、状态权威、持久化、任务生命周期、MCP、Provider、Host、Transport、Node、Discovery、监控与恢复功能的唯一实现语言。
- Swift 仅负责 macOS UI 和必要的原生桌面集成。现有 Shell 仅作启动、构建、签名薄胶水，不得承接业务状态或恢复逻辑。
- 禁止新增 Python 运行时、Python 业务逻辑、Python fallback、第二套状态权威或重复执行状态机。Python 不得作为正常运行时、构建依赖或发布包依赖重新引入。
- 确需其他实现语言时，必须在实施前取得用户明确批准并记录范围；现有外部二进制依赖不构成扩大例外的授权。详见[语言决策](docs/architecture/ADR-007-rust-source-policy.md)。
- 新功能必须复用现有 Rust 契约，保持身份、并发、回执、幂等、取消和恢复语义。先读[当前契约入口](docs/CONTRACTS.md)，不得以重命名、移动文件或包装模块声称完成迁移。
- 不得删除既有测试、弱化断言、增加无依据的 skip 来追求全绿。保留失败证据；历史测试使用原源码和隔离状态恢复，不向当前运行时补回 Python。
- 新增模块必须更新[源码分类](architecture/source-policy.json)，检查依赖方向；后端不得依赖 UI 或开发工具，传输和投影不得自行成为 Task writer。静态检查不能替代对副作用和事务边界的审查。
- 变更必须完成相关构建、回归、依赖及发布包检查；状态明确区分 PASS、FAIL、BLOCKED、未验证（NOT_RUN / NOT_PROVEN）。源码、候选包、已部署版本、设备验收和业务结果分别记录。
- 不得擅自部署、重启生产服务、修改生产数据库或删除运行数据。构建、文档、提交和推送不授予这些权限；回退不得覆盖已经提交的新权威事实。
- P620 经管理员安装后，授权运维统一使用 `sudo -n /usr/local/sbin/clinx-admin`，规则和边界见[运维指南](docs/OPERATIONS.md#p620-固定免密运维入口)。不得恢复退役 Python staging、增加任意 Shell/SQL 入口或用免密 sudoedit 绕过 Rust 校验。免密能力不代替用户对本次部署或服务操作的授权。
- 技术事实按[文档标准](docs/DOCUMENTATION.md)维护；重要架构变更同步更新架构、接口契约、测试说明和 CHANGELOG。历史报告只追加范围说明，不改写原日期、结果或失败记录。
- 提交前执行 `clinx-source-audit --check-project` 及相关回归；CI 失败不得绕过。不得修改审计清单掩盖不符合上述规则的实现。
