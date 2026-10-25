# CLINX Rust 内核、运行时与工具

用途：Rust 包导航。适用范围：`kernel/` 当前源码。状态：CURRENT。
验证：[测试指南](../docs/TESTING.md)；构建与测试不需要 Python。

包名 `clinx-decision-kernel` 为 Cargo 兼容名称；它现在包含 Authority、后端适配器、
运行程序和开发工具，不可从旧名称或 evaluator 的 `NON_AUTHORITATIVE` 常量推导
`clinx-runtime` 的状态权威。职责与依赖见[架构](../docs/REPOSITORY_ARCHITECTURE.md)。

- `src/`：运行模块；`src/bin/`：Cargo 原生入口及工具。
- `tests/`：当前 Rust 集成资格；`examples/`：独立私有子进程与显式 smoke。
- [核心 fixture](../fixtures/execution_core_v1.json)：冻结转换表和独立期望。
- [源码分类](../architecture/source-policy.json)：当前模块与直接依赖准入。

`clinx-owner-client` 仅是 bounded NDJSON Unix socket 客户端，检查 peer UID，未知发送不重试；
它不持有业务库。`clinx-kernel-eval`、`clinx-kernel-bench` 仍有 Cargo targets，均属开发工具，
不进入运行发布包。不要根据历史原型“退役”文字删除 Authority 仍复用的 `runtime_lab` 类型。
