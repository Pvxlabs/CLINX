# CLINX 变更记录

用途：追踪源码与兼容边界变化。适用范围：Git 主线；不是已部署版本清单。
状态：CURRENT。验证：Git diff/log、[测试指南](docs/TESTING.md)与候选 manifest。

## Unreleased — 2026-10-25

- 以 `1340861e6e9956cdb21532031a542c0b86645178` 为核对基线，统一产品、架构、开发、
  契约、配置运维、测试与历史文档地图。修正 Python Owner 和 pytest 当前入口矛盾。
- AGENTS 强制 Rust 后端、Swift UI、零 Python 运行/构建/发布依赖和单一权威；新增 ADR-007。
- 在现有 Rust source audit 加入项目文档、源码归属、直接依赖及发布一致性检查，CI 调用相同入口。
- 不变更业务协议、数据库 schema、现有运行语义或生产部署；外部 Go Transport 兼容边界明确保留。

## 基线历史

本记录从上述基线开始，不补造历史 SemVer 或发布验收。Cargo package `0.1.0` 不是安装
release ID。历史 Rust 迁移、外部 Transport 重建及生产回执见[文档地图](docs/README.md)；
它们各自的日期、SHA、失败和未验证范围保留。具体版本/升级规则见[配置与运维](docs/OPERATIONS.md)。
