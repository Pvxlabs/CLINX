# Python 运行边界

目标是 Rust 核心、适合现有客户端的前端（macOS UI 为 Swift）、尽可能薄的 Python 兼容层。
文件后缀或目录名称不决定是否仍有运行职责。机器清单仍以
[python-sources.json](../architecture/python-sources.json) 为准；本表覆盖全部 Python 运行包组。
没有修改 Linguist 规则，也没有将测试移出统计范围。全仓 Python 字节占比目标已收紧至严格低于 5%；
`clinx-source-audit` 固定统计口径，构建和资格工具也在目标内。

| Python 文件组（`clinx/` 下） | 当前实际用途 | Rust 接管范围 / 剩余边界 |
| --- | --- | --- |
| 各包 `__init__.py` | 包命名空间 | 无任务业务逻辑 |
| `authority/rust_authority.py` | 公开 Commands API、异常与请求 ID 传递 | socket、请求验证、精确 ACK 由 `authority_client`/`UnixAuthority` 实现；单次发送，无自动重试 |
| `authority/request_pipe.py`、`nodes/network_worker.py` | 私有有界 stdio IO、对象回调 | 不写业务数据库；保留客户端超时和丢响应不确定性 |
| `authority/rust_integration.py`、`authority/rust_reads.py`（已删除） | 最后维护调用者已迁 Rust | `clinx-maintenance` 使用现役 Provider/Authority 契约；Helper 直接使用原生 API，Python read callback 通道同时退役 |
| `authority/rust_worker_identity.py` | root 管理的已安装 worker 账号读取 | 平台配置/维护边界，不创设调用权限 |
| `contracts/runtime_config.py`、`host_config.py`、`runtime_contracts.py` | TOML、配置 dataclass、固定错误/状态词汇 | 旧公开 BridgeConfig DTO 保留 Python TOML codec；正常 Rust daemon 直接 TOML/Path/Provider config；默认值、绑定、路径策略及 Host 配置校验在 Rust，不构造 Task Authority |
| `contracts/execution_semantics.py`、`execution_policy.py` | 公开 dataclass、tuple/JSON codec 和旧 API 异常 | normalize/build/parse、scope、许可/路由验证和旧 CAS 哈希由 Rust 实现；旧兼容策略没有接入 Authority 新任务准入 |
| `contracts/thread_identity.py`、`dispatch_errors.py` | 现有选择器/显示 URI、显式兼容请求形状校验和错误投影 | 含历史公开 helper，尚未全部退出 Python；正常任务路由和 Git 权威使用 Rust 契约 |
| `host/production_workflows.py` | Workflow dataclass/tuple codec | schema、注册校验及渲染复用 Rust Host 契约；执行、取消、超时、恢复、工具 ACK 已是 Rust |
| `mcp/mcp_server.py`、`monitor/observation_mcp.py`、`contracts/workspace_contract.py`（已删除） | 最后维护消费者切至 Rust | 业务、观察与策略维护全部由 Rust MCP profile 承担；Node 选择器资格迁至 Rust，原参数/断言保留 |
| `provider/app_server.py` | 公开 Codex API/请求参数与错误兼容；仍含 Process/SSH 资格传输接口 | 默认 Process/SSH/Unix/WebSocket/JSONL、request/notification、模型与thread/turn契约、响应关联、只发一次、writer guard、精确输入确认在 Rust；Python 保留签名/DTO/异常及显式测试 IO 注入 |
| `provider/native_provider.py`、`native_writer_probe.py` | Provider/OS 只读 IO、平台异常与既有接口 | 多 Provider 证据选择、Linux writer PID/FD、Mac lsof/ps 证据判定、活性分类在 Rust |
| `provider/native_history.py` | Rust 历史客户端 | 无 Python SQL/JSONL 业务实现 |
| `provider/desktop_socket.py`（已删除） | Rust `clinx-provider --desktop-socket` 替代 | 原 socket/peer/witness/inode/alias 契约保持，安装器直接执行原生工具 |
| `nodes/node_protocol.py`、`node_runtime.py`、`rust_node_adapter.py`、`provider_relay.py` | DTO、既有身份对象、协议回调 | TLS、registry/ledger、反向 Authority、Provider exchange、fanout/ACK/cursor、read/context 投影在 Rust；公开 DTO 构造/配置写入仍有 Python |
| `nodes/node_centre_entrypoint.py` | 历史 Centre CLI exec 别名 | Helper 的 Python service entrypoint 已删除；正常 Helper/Centre 均由 Rust daemon 装配 |
| `discovery/identity.py`、`pairing.py` | 公开身份/配对 DTO 与 OPAQUE 资格绑定；旧 DEV 接口仅失败桩 | 密钥/证书生成和加载、OPAQUE 状态、私有存储/锁、公钥/证书校验、信任冲突/降级/撤销/authenticate 原子更新与 pairing TTL/失败预算/一次性消费在 Rust；不扩大原信任边界 |
| `discovery/discovery.py`、`observer_bootstrap.py` | 候选 DTO、显式时钟注入和Rust 配置/URL codec 的公开 facade | DNS-SD UDP/interface、PTR/SRV/TXT/A cache、TTL/冲突/展示与 Observer 私网连接规则在 Rust |
| `discovery/transport.py` | 公开 DeviceSession/连接 DTO、独立 OpenSSL 互操作 oracle；Python SPAKE2/TLS 实验已删除 | 生产首次配对、TLS/OPAQUE wire、重连、观察连接读取都由 Rust discovery_enrollment 执行 |
| `discovery/cli.py` | 历史设备 CLI exec 别名；无 Python DEV fallback，未消费的 GUI exec 空壳已删除 | Swift 直接调用 bundle 内 clinx-node --discovery-gui；正常 GUI 和 devices/pair/serve 无 Python |
| `transport/tunnel_reliability.py` | 仅保留原公开资格 API 的可注入时钟/进程 IO；正常 CLI exec Rust | 独立 `clinx-tunnel` 接管 supervisor、flock、进程组 drain、signal、health deadline 和持久恢复；该 Python 文件不进入运行发布包 |

`tools/build/`、`scripts/` 中的构建、候选发布、安装与维护工具不在运行源码 profile 中；
`fixtures/` 和 `tools/qualification/` 是资格代码。现役测试按原参数和断言映射迁入 Rust，
Python 对象和独立 OpenSSL 互操作检查仍保留。封闭 runtime_lab 资格链已在早前提交退役。
历史 forwarding builder/deployer 仍用于精确旧产物恢复，并拒绝拿当前包冒充旧平铺 release。
源清单的 DEAD 条目记录已删除模块，不会拷入新 Rust release 或 Helper。

正常 Mac Helper/Centre/配对与 Collector 运行链已全 Rust。Python 仍用于表中公开兼容 API、
开发维护；正常 Observer/观察 MCP/设备 CLI 也已迁 Rust；这与全仓 Python <5% 是不同验收。
当前 Monitor builder 使用 `architecture/native-runtime.json`，零 Python 源码与解释器；
原 `monitor` profile 只保留给既有旧布局兼容资格，正常 App 不发布它。
新产物未部署；原生 Mac 构建/签名和新产物物理跨设备闭环需相应环境，
Linux 的私有进程/mTLS/DNS wire fixture 不替代这些验收。

Rust release 的 `rust` Python profile 现为空；安装维护由 `clinx-provider --desktop-socket`
执行，依赖 overlay 不再继承。Python 公开客户端/DTO 和显式测试注入仍在仓库中，统一标为 COMPAT，
旧 monitor profile 仅供兼容资格，不是当前 App 资源。构建/维护工具本身仍需继续迁移。
本轮将一组现役 Python 核心资格迁入 Rust，并保留依赖 Python 对象行为的最小smoke，
逐用例映射与删除量见 [资格与维护迁移](RUST_QUALIFICATION_MIGRATION.md)。
后续 Native/Host/Node/Provider 跨进程资格与 411 行旧离线契约校验器的退役，
见 [原生资格迁移](RUST_NATIVE_QUALIFICATION_MIGRATION.md)。剩余 fixture 共享的 Python
Provider wire helper 仍有消费者，不能随测试主体一并删除；它不进入任何原生运行包。

本轮进一步删除最后 Python MCP factory、维护业务入口和旧 SPAKE2 后端；
Monitor 构建、Host/MCP 配置编译及 Observer inode guard 改用 Rust。
实际消费者、原回归映射和全量既有失败区分见 [兼容实现退役](RUST_COMPAT_RETIREMENT.md)。

`monitor/rust_monitor.py`、`monitor/native_associations.py` 与 `provider/liveness.py`
在最后 Python factory 退役后已无源码、服务、动态名称或本地测试消费者，本轮删除。

`clinx/monitor/` 全部 Python 源码及旧 Helper factory 已删除；独立 SQL/HTTP 资格改走原生
Observer/Node 接口。`transfer_rust_native_session.py` 和 `local_discovery_smoke.py` 已有
Rust 工具替代。消费者与逐断言映射见 [维护与观察兼容收敛](RUST_MAINTENANCE_CONVERGENCE.md)。
