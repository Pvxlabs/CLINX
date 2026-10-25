# CLINX 贡献与编码规范

用途：开发流程和代码审查准则。适用范围：本仓库全部代码、文档及发布工具。
状态：CURRENT。验证：执行[测试指南](docs/TESTING.md)及 `clinx-source-audit --check-project`。
语言、数据保护和工作区要求以 [AGENTS](AGENTS.md) 为强制定义。

## 工作顺序

1. 读适用 AGENTS、[架构](docs/REPOSITORY_ARCHITECTURE.md)和[契约](docs/CONTRACTS.md)，
   核对 HEAD、实时远端、工作区和现有任务协调。记录不属于本任务的修改，不 reset/clean/stash。
2. 先定位实际入口、状态 owner、事务和故障证据；选择最小修复。复用 canonical 仓库，
   必要资格仓库放在任务所属 `/data/artifacts`，不建长期重复项目。
3. 保持现有公开接口和独立 oracle。新增实现使用 Rust；Swift 仅处理 UI/native integration。
   模块加入[源码分类清单](architecture/source-policy.json)，说明职责和依赖，不自动放宽边界。
4. 按影响范围运行构建、回归、审计和发布检查；保存命令、环境、退出码、失败与未验证项。
5. 对照显式文件白名单审核 diff 并提交。推送、部署或生产操作分别按用户授权执行；
   推送后重新读取本地 HEAD、tracking ref、实时远端 SHA 和 ahead/behind。

## 编码与模块边界

- Rust 使用明确的类型、作用域和 `Result` 错误；不得吞掉未知状态或把协议失败转换为成功。
  边界错误使用稳定 code，诊断附上下文但不得泄露凭据、提示词或跨任务数据。
- JSON/IPC 输入必须校验长度、类型、版本与 identity；保持 typed request ID、scope、epoch、
  capability、generation、request/task/execution/thread/turn 关联，不按“最新结果”替代精确读。
- 业务写入集中于现有 Authority 事务；结果、receipt、outbox 与状态提交保持原子性。
  Provider、Transport、Node 与只读投影不得直接打开业务库成为第二 writer。
- 超时、断连、取消 ACK 和失联不证明未执行。持久化意图后按原 identity 读取证据，
  不盲目重发；释放 lease 和业务完成是独立事实。
- 不跨模块复制状态机或同一类型。公共契约留在现有模块，I/O 放在所属适配器；
  后端不能依赖 Swift UI、`kernel/tests`、`kernel/examples` 或仅开发工具。
- 使用受控 `Command` 参数，避免插值 shell；显式超时、输出边界、进程归属与 teardown。
  测试不得连接生产 socket、真实 Provider 或共享运行数据库。
- 修改过的 Rust 文件需通过 rustfmt，相关 Clippy 不引入新诊断。全仓既有格式/lint 问题
  单独列出，不能顺手大范围改写或隐藏。Swift 修改需在 macOS 做相关构建与验证。

## 回归与交付

当前测试在 `kernel/tests/`，冻结 JSON/SQL oracle 在 `fixtures/`，子进程资格程序在
`kernel/examples/`。测试应验证独立语义、边界和失败恢复，不能只复制实现逻辑。
保留历史 ignored `tests/` 原字节；恢复历史套件时必须匹配原源码和依赖。

重要变更同步维护架构、契约、测试和 [CHANGELOG](CHANGELOG.md)；文档格式及历史处置
见[文档标准](docs/DOCUMENTATION.md)。交付报告分别给出源码、候选包、部署、实机和业务状态。
完成后仅回收本任务的临时仓库和可再生 build，保留独有日志、hash、receipt 和恢复材料。
