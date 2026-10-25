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
| `authority/rust_integration.py` | 历史公开 Python API/DTO | 业务映射在 Rust；正常 Helper 绕过此类，不构造旧 Registry/Dispatcher |
| `authority/rust_reads.py`、`monitor/rust_monitor.py`、`monitor/rust_observation.py` | Authority/Provider/HTTP IO 与对象兼容 | managed context、精确身份复检、分页/覆盖/投影由 Rust 实现 |
| `authority/rust_worker_identity.py` | root 管理的已安装 worker 账号读取 | 平台配置/维护边界，不创设调用权限 |
| `contracts/runtime_config.py`、`host_config.py`、`runtime_contracts.py` | TOML、配置 dataclass、固定错误/状态词汇 | 旧公开 BridgeConfig DTO 保留 Python TOML codec；正常 Rust daemon 直接 TOML/Path/Provider config；默认值、绑定、路径策略及 Host 配置校验在 Rust，不构造 Task Authority |
| `contracts/execution_semantics.py`、`execution_policy.py` | 公开 dataclass、tuple/JSON codec 和旧 API 异常 | normalize/build/parse、scope、许可/路由验证和旧 CAS 哈希由 Rust 实现；旧兼容策略没有接入 Authority 新任务准入 |
| `contracts/thread_identity.py`、`workspace_contract.py`、`dispatch_errors.py` | 现有选择器/显示 URI、显式兼容请求形状校验和错误投影 | 含历史公开 helper，尚未全部退出 Python；正常任务路由和 Git 权威使用 Rust 契约 |
| `host/production_workflows.py` | Workflow dataclass/tuple codec | schema、注册校验及渲染复用 Rust Host 契约；执行、取消、超时、恢复、工具 ACK 已是 Rust |
| `mcp/mcp_server.py`、`monitor/observation_mcp.py` | 观察 stdio/HTTP codec、显式注入的维护/资格接口 | 正常 15 个业务工具及 4 个观察工具都在 Rust；观察 stdio 入口也直接 exec Rust |
| `provider/app_server.py` | 公开 Codex API/请求参数与错误兼容；仍含 Process/SSH 资格传输接口 | 默认 Process/SSH/Unix/WebSocket/JSONL、request/notification、模型与thread/turn契约、响应关联、只发一次、writer guard、精确输入确认在 Rust；Python 保留签名/DTO/异常及显式测试 IO 注入 |
| `provider/native_provider.py`、`native_writer_probe.py`、`liveness.py` | Provider/OS 只读 IO、平台异常与既有接口 | 多 Provider 证据选择、Linux writer PID/FD、Mac lsof/ps 证据判定、活性分类在 Rust |
| `provider/native_history.py`、`monitor/native_associations.py` | Rust 历史客户端 | 无 Python SQL/JSONL 业务实现 |
| `provider/desktop_socket.py` | 显式维护已安装 Desktop socket 别名 | 保持已有 endpoint/receipt 身份，不作为第二执行器 |
| `monitor/network_observation.py`、`native_observation_bridge.py` | 私有协议客户端、HTTP callback、授权对象装配 | 查询/目录/去重、HMAC checkpoint、scope 复检、远端冷却和身份决策在 Rust |
| `monitor/observation_source.py` | Collector 公开 facade、显式测试注入 | spool/checkpoint/ACK、正常 Provider/native/HTTP 探针、live cache 及外层调度在 Rust |
| `monitor/observer_server.py` | 对象回调、返回 codec、显式只读 snapshot API | HTTP listener/并发限制/auth/路由/脱敏和ActivityReader、旧 ObserverStore 查询/分页/事件/状态投影已迁 Rust；正常 CLI/环境/IO 装配现为 Rust；此 facade 只保留显式注入资格 API |
| `nodes/node_protocol.py`、`node_runtime.py`、`rust_node_adapter.py`、`provider_relay.py` | DTO、既有身份对象、协议回调 | TLS、registry/ledger、反向 Authority、Provider exchange、fanout/ACK/cursor、read/context 投影在 Rust；公开 DTO 构造/配置写入仍有 Python |
| `nodes/node_service_entrypoint.py`、`node_centre_entrypoint.py` | 历史 CLI exec 别名 | 正常 Helper/Centre 配置、signal/listener、注册/心跳/Collector 与外层 tick 全部在 Rust |
| `discovery/identity.py`、`pairing.py` | 公开身份/配对 DTO 与显式 DEV_ONLY 后端 | 密钥/证书生成和加载、OPAQUE 状态、私有存储/锁、公钥/证书校验、信任冲突/降级/撤销/authenticate 原子更新与 pairing TTL/失败预算/一次性消费在 Rust；不扩大原信任边界 |
| `discovery/discovery.py`、`observer_bootstrap.py` | 候选 DTO、显式时钟注入和Rust 配置/URL codec 的公开 facade | DNS-SD UDP/interface、PTR/SRV/TXT/A cache、TTL/冲突/展示与 Observer 私网连接规则在 Rust |
| `discovery/transport.py` | 公开 DeviceSession/连接 DTO、显式 DEV_ONLY SPAKE2/SSL 测试后端 | 生产首次配对、TLS/OPAQUE wire、重连、观察连接读取都由 Rust discovery_enrollment 执行 |
| `discovery/cli.py`、`monitor_helper.py` | 设备设置 CLI/历史 GUI exec 别名；显式 DEV_ONLY 后端在资格工具 | Swift 直接调用 bundle 内 clinx-node --discovery-gui；正常 GUI 和 devices/pair/serve 无 Python |
| `transport/tunnel_reliability.py` | 仅保留原公开资格 API 的可注入时钟/进程 IO；正常 CLI exec Rust | 独立 `clinx-tunnel` 接管 supervisor、flock、进程组 drain、signal、health deadline 和持久恢复；该 Python 文件不进入运行发布包 |

`tools/build/`、`scripts/` 中的构建、候选发布、安装与维护工具不在运行源码 profile 中；
`fixtures/` 和 `tools/qualification/` 是资格代码。现役测试原断言保留；本轮只删除依赖已不存在阶段测试的封闭 runtime_lab 资格链，恢复点为前一 Git 提交。
历史 forwarding builder/deployer 仍用于精确旧产物恢复，并拒绝拿当前包冒充旧平铺 release。
源清单的 DEAD 条目记录已删除模块，不会拷入新 Rust release 或 Helper。

正常 Mac Helper/Centre/配对与 Collector 运行链已全 Rust。Python 仍用于表中公开兼容 API、
开发维护；正常 Observer/观察 MCP/设备 CLI 也已迁 Rust；这与全仓 Python <5% 是不同验收。
当前 Monitor builder 使用 `architecture/native-runtime.json`，零 Python 源码与解释器；
原 `monitor` profile 只保留给既有旧布局兼容资格，正常 App 不发布它。
新产物未部署；原生 Mac 构建/签名和新产物物理跨设备闭环需相应环境，
Linux 的私有进程/mTLS/DNS wire fixture 不替代这些验收。

Rust release 的 `rust` Python profile 现只含 `desktop_socket.py` 的 12 文件安装维护闭包，
不再发布 Monitor/Discovery/MCP Python 模块；公开兼容 API 的旧包布局测试继续使用 `monitor` profile。
`observer_server`/`observation_mcp`/`observer_bootstrap` 标为 COMPAT，其 Python 主运行实现已移除。
详见 [本轮迁移记录](RUST_OBSERVER_RUNTIME_MIGRATION.md)。
