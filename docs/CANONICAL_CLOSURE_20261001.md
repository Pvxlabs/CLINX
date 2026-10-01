# CLINX canonical closure：2026-10-01

长期 canonical checkout 统一为 `/home/pvxlabs/dev/clinx`，分支为 `main`。
本次从 GitHub `origin/main` 的 `68f7ce3bb2f63ecb278b4d888d50da65d330c7db`
建立独立干净 clone，未覆盖原共享 dirty 或运行中的 runtime worktree。

## 合入内容与权威

- Host 路径边界采用 [trusted workspace contract](TRUSTED_WORKSPACE_PATHS.md)。
  配置唯一来源是 `bridge.toml`；discovery、runtime、动态工具和回归使用同一字段。
  `git -C` 不再与 `git -c` 混淆。structured Git 的 registered origin/branch、
  mutation lease、production/network authority、Provider 交付与防重放语义保留。
- [Native workspace](NATIVE_WORKSPACE_EXECUTION.md) 默认及 completion supervision
  从 runtime 的独立 dirty patch 合入。三方比较确认原共享 dirty 与 runtime 的增删行
  一致；以各自 HEAD 为基线比较，保留主线较新的 Host v2 与 Thread Identity 修复。
  新任务默认 SANDBOX_WORKSPACE，显式网络启用 NETWORKED_SANDBOX；Host 必须显式
  请求，continuation/reopen 保留 sealed policy。
- 三份 Observer 文档统一为 trusted Tailnet devices + independent read-only Observer
  credential；`AIR_ONLY_ACL=NO`、`PUBLIC_ACCESS=DENIED`、`FUNNEL=FORBIDDEN`。
  本次 closure 不部署或重启 Observer；此前独立部署状态不由此文重新认证。
- `bridge.toml` 中已有 ORION SHA-specific compatibility commands 全部保留；缺乏
  排除 historical recovery 用途的证据，不据此删除。未执行这些 commands。
- Monitor 优先使用权威 task/execution BLOCKED 与 exact result；子命令失败不能
  覆盖 RUNNING/BLOCKED。较新、同一 execution 的详情同步更新列表行；旧或不同
  execution 的详情不能覆盖较新列表。原生回归覆盖行标签、precedence 和快照一致性。

## 验证与运行切换

完整 Python 回归要求先按 `kernel/Cargo.toml` 构建 release kernel；全新 clone 缺少
二进制导致的初次失败日志保留。验证包括完整 pytest/unittest、Host v2、Provider
真实交付故障（执行一次、结果不确定后禁止 replay）、Air 上原生 `swift test`。

共享 runtime 只由外层 operator 切换；切换前只读确认没有 active execution、Host
execution、worktree lease 或 unresolved delivery。统一 dispatcher、tunnel MCP child、
registered CLINX project 与配置来源，仅重启必要 CLINX 服务；不重启 ORION、Observer
或其他公共 daemon。随后从正式 ChatGPT connector 创建独立 closure/smoke execution。

历史 `task_5834ff2cc0fd41e8af2c3a7bc33cc177` /
`exec_e451409c3c5e4337acc5fbbcc61b5a88` 的 BLOCKED result 必须保留。
新 closure execution 通过当前 canonical project 开始，不改写旧 cwd、policy 或结果。

临时 worktree 只有在 runtime 切换及正式 smoke 完成，且其 commits/dirty 已整合、
没有 process cwd、service reference、active execution、lease 时才能移除。
原 diff、untracked source、分支 bundle、逐文件分类与删除前检查留存在 artifacts。
disposable pilot 的独立 dirty 工作不属于本次清理范围。

## 最终证据位置

`/data/artifacts/clinx-canonical-closure-20261001/` 保存逐 hunk 三方审计、历史 result
及 ORION task 校验、初始和最终 inventories、所有成功/失败测试日志、正式 connector
结果、runtime 切换与 remote readback receipt、中文最终报告。

最终状态以该目录的 `FINAL_REPORT.md` 和 `canonical-receipt.json` 为准；本提交中的
设计与流程说明不替代 runtime、remote、外层 connector 或清理验收。
