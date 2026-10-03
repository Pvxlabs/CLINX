# CLINX Monitor Final Closure SPEC — 2026-10-03

## Objective

在同一最终源码提交上完成两项既有 Monitor 失败的最小修复，并完成受影响范围的测试、macOS 构建与界面验证、制品构建、CLINX 运行时激活、身份读回和隔离 Provider/Host 链路验收。

## Scope and boundaries

- 仓库：`Pvxlabs/CLINX`；canonical 目标为 P620 `/home/pvxlabs/dev/clinx`，本地修复先在对应 Git 工作区完成并同步。
- 允许修改：Monitor 测试、Monitor 源码（仅若根因确为实现缺陷）、必要的本轮证据文档。
- 不允许：Monitor 重设计、无关重构、强推/破坏性 reset、删除他人 worktree、ORION 代码/配置/授权/任务/数据库/生产变更。
- 运行验收只使用 CLINX 隔离 execution、测试数据和既有发布机制；不得通过手改 registry、抹除租约或重放未知副作用操作切换运行时。

## Acceptance criteria

1. `test_removed_search_row_does_not_return` 约束搜索行未恢复且 `ExecutionListView` 仍挂载，不依赖完整旧调用字符串。
2. `test_row_sizes_come_from_the_token_layer_not_from_the_view` 检查 ExecutionRow 的实际字号来自 `DS.Font.rowTrailing`；过滤器/其他合法小图标字号不被误判。
3. 两项失败复测、完整 Monitor 相关 Python/Swift 测试和受影响 control 回归通过。
4. 在可用 macOS 图形会话中从同一提交构建并启动 Monitor，检查正常/窄窗口、无搜索行、执行列表、图标字号间距及选择与 Inspector 联动，并保存截图。
5. `FINAL_SOURCE_SHA` 已合并并同步远端，工作区干净；control 与 Monitor 制品均可追溯到该 SHA。
6. 运行时切换只影响必要 CLINX 组件，取得实际加载制品身份、关键文件摘要和健康读回；失败时按既有机制回退并保留证据。
7. CLINX 隔离 Provider/Host 验收分别记录 Host 操作、结果投递、最终消息解析、Execution 状态和外层读回；ORION_MUTATION=NONE。

## Milestones

- M1：复现失败、确认是测试过度耦合还是实现缺陷，完成最小修复。
- M2：完成测试和 macOS 实际构建/界面验证，锁定最终源码提交。
- M3：从 FINAL_SOURCE_SHA 构建 control/Monitor 制品，激活并读回运行身份。
- M4：完成隔离 Provider/Host 链路验收，整理证据与状态回执。

