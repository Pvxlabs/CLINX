# ADR-007：Rust 后端唯一实现与文档权威

用途：记录长期语言归属与维护决策。适用范围：CLINX 当前和后续开发。
状态：CURRENT / Accepted，2026-10-25（Asia/Shanghai），依据用户明确工程要求。
验证：[架构](../REPOSITORY_ARCHITECTURE.md)、[源码审计](../../kernel/src/bin/clinx-source-audit.rs)、
[测试指南](../TESTING.md)。

## 背景与决定

当前 main 已完成受控 Python 源码退役，但旧迁移报告与少数开发入口仍使用旧 Owner/pytest
描述。继续按这些入口开发可能重建已经退役的依赖或第二执行权威。
因此后端及工具使用 Rust；Swift 负责 macOS UI/native integration。强制条款唯一定义在
[AGENTS](../../AGENTS.md)，文档角色唯一入口为[文档地图](../README.md)。
新增实现语言必须先取得用户明确批准，不能用历史存在过、薄包装或百分比达标替代。
保留源码字节统计作为历史可比数据，零 Python 准入由结构/项目检查强制执行。

## 已存在的外部边界

[build_transport](../../kernel/src/build_transport.rs) 仍重建固定上游 Go `client-runtime` 归档，
[build_tunnel](../../kernel/src/build_tunnel.rs) 消费其 transport 制品；普通 Rust 构建不需要 Go。
这是基准源码已有的外部兼容依赖，不是本次新增 Go 源码，不授予后续扩展 Go 实现的许可。
如果“所有 Transport 包含全部外部依赖也必须为 Rust”是最终目标，这个边界尚未闭合；
本次保留实际构建链和历史证据，不伪称已退役，不重复实施迁移。
`host-worker` 由现有 base release 继承并验证 hash；源码检查不能单凭名字证明它的实现语言。
后续替换这类边界需明确任务范围、独立证据和兼容性评审。

系统工具（Shell 启动/签名胶水、C linker、SQLite bundled C、OpenSSL、Codex 外部程序）
不是新 CLINX 业务实现。现有胶水只能保留原用途；不得新增 shell 业务或 Python 构建依赖。
`python_numeric.rs` 等 Rust 兼容 codec 保留原线协议语义，不运行 Python。

## 备选方案与后果

拒绝仅靠 AGENTS 提醒、保留 Python fallback 或新造文档构建系统。
采用现有 `clinx-source-audit` 加上显式源码归属、直接依赖和 Markdown 链接检查，Cargo/CI
执行同一入口；构建产物沿用现有发布白名单和 marker/hash 验证。
静态准入不能证明全部动态状态或设备行为，必须保留契约回归与人工审查。
历史 ADR-001 至 ADR-004 描述当时目标/实现，不得替代当前 Rust 架构；ADR-006 的
只读投影原则继续作为专项参考，具体路径以当前源码为准。

## 已核实的受控遗留文件

基线还有 [runtime/host_worker.c](../../runtime/host_worker.c)（Host 子进程固定身份降权，
关闭继承 FD，exec 目标程序）和 [check_transport_profile.go](../../fixtures/check_transport_profile.go)
（固定上游配置对照资格）。这使“仓库所有源文件均 Rust/Swift”的说法不成立。
本次不扩展或重写这两项，将其精确路径与 SHA-256 锁定在源码分类清单；任何内容变更或新增
非 Rust/Swift 实现都必须先取得用户明确批准。锁定是防止范围扩大，不是声明已批准长期例外。
因此全后端语言目标与这项 C 启动器之间仍有未闭合边界，应单独决策并做身份隔离回归。
