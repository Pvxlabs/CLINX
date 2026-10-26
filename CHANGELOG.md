# CLINX 变更记录

用途：追踪源码与兼容边界变化。适用范围：Git 主线；不是已部署版本清单。
状态：CURRENT。验证：Git diff/log、[测试指南](docs/TESTING.md)与候选 manifest。

## 0.3.2 (292) — 发布候选 2026-10-26

- macOS Provider 使用 `getpeereid` 校验已连接 Unix socket 的真实对端 UID，支持 Codex
  的稳定 socket 符号链接；Linux 保留 `SO_PEERCRED`。错误 UID 在握手前拒绝，
  不以符号链接自身的文件类型代替对端身份。复用 nix 的安全 socket API，保留
  `forbid(unsafe_code)`，不放宽 mTLS/项目授权。
- Provider IO 按原绝对 deadline 使用非阻塞读写与 poll；Darwin 对端关闭后仍排空缓冲帧，
  不因重新设置 socket timeout 的 `EINVAL` 丢失已经收到的响应。未知发送仍不得重放。

## 0.3.2 (291) — 发布候选 2026-10-26

- 修复 Darwin 非阻塞 listener 的 accepted socket 继承行为：Node 和配对 TLS worker
  显式转回 blocking IO，再应用既有超时；保留认证、ACK 和任务状态语义。
- 增加 root 所有的 Rust `clinx-admin` 固定运维入口、摘要绑定的专用 sudoers 安装器；
  替换退役 Python staging 和免密 unit 编辑规则，校验发布目录及祖先权限。
  入口覆盖只读诊断、配对、已有共享范围授权、发布暂存/切换和原有策略 CAS 工具，
  不授予任意 Shell、SQL、服务或输出路径。规则安装、部署与真实设备验收分别记录。

## 0.3.2 (290) — 发布候选 2026-10-26

macOS 版本号从现装 `0.3.1 (289)` 前进到 `0.3.2 (290)`；P620 release 仍按内容哈希编号。
以下源码已进入本次候选；安装与生产验收须以当次部署回执为准。

- 增加受限 Rust 离线设备路由增配：精确身份/CAS、零活动执行、仅追加、原子审计回执；
  不改变公开 MCP、任务状态机或现有路由。见 [ADR-008](docs/architecture/ADR-008-additive-device-routes.md)。
- 修正 writer-lock 回归的临时目录前提：显式私有权限与 canonical 路径，
  在 CI 的 `umask 022` 下保持原有 writer 排他、子进程继承和关闭释放断言。
- 限制 CI 独立测试 fixture 并发，避免未优化 OPAQUE 计算争用共享 CPU；保留生产超时、
  原有并发测试和全部断言，资源实验与历史失败边界见[测试指南](docs/TESTING.md)。

- 修复 macOS Rust Helper 编译：以平台适配保持 socket FD/阻塞标志、时钟和设备号类型正确，
  新增六种 socket 模式回归；Linux 原有 socket/lease 语义保持。验证入口见
  [macOS 编译回归](docs/TESTING.md#macos-编译兼容回归)，不代表客户端安装或设备执行验收。

- 修复 Codex 显式模型选择：原生/Host/API 发送前读取目标 Provider 的有界模型清单，
  校验原生 ID 与各模型 effort，统一 `extra_high` → `xhigh`；禁止静默降级。
  失败回执保留脱敏 Provider 类别、底层码和阶段，发送未知继续精确读回原执行。
  契约及本次证据边界见 [模型选择](docs/CODEX_MODEL_SELECTION.md)。源码修复不代表部署。

- 以 `1340861e6e9956cdb21532031a542c0b86645178` 为核对基线，统一产品、架构、开发、
  契约、配置运维、测试与历史文档地图。修正 Python Owner 和 pytest 当前入口矛盾。
- AGENTS 强制 Rust 后端、Swift UI、零 Python 运行/构建/发布依赖和单一权威；新增 ADR-007。
- 在现有 Rust source audit 加入项目文档、源码归属、直接依赖及发布一致性检查，CI 调用相同入口。
- 不变更业务协议、数据库 schema、现有运行语义或生产部署；外部 Go Transport 兼容边界明确保留。

## 基线历史

本记录从上述基线开始，不补造历史 SemVer 或发布验收。Cargo package `0.1.0` 不是安装
release ID。历史 Rust 迁移、外部 Transport 重建及生产回执见[文档地图](docs/README.md)；
它们各自的日期、SHA、失败和未验证范围保留。具体版本/升级规则见[配置与运维](docs/OPERATIONS.md)。
