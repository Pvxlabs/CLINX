# Rust MCP 业务入口迁移

本批基于 main `81254e261df2b411c7ac4f50390266bb051a9454`。
**完成正常业务 MCP 的 Rust 接线；全部 Python 运行时退役、Python <25% 尚未完成。**
未部署、未重启生产服务、未改生产数据库或真实任务。

## 实际替代

`kernel/src/bin/clinx-mcp.rs` 处理 stdio JSON-RPC；`mcp.rs` 处理初始化、发现、
工具列表、15 个业务工具分发、错误及公开字段投影；`mcp_api.rs` 实现：

- prepare/start/adoption、策略准备/应用、派生 Git target、取消及 Authority 回执读取；
- 精确 task/execution/thread/turn 路由校验、历史 execution 与当前 task 投影分离；
- 既有生产 Provider 的只读 thread/turn 请求、有界 100 页精确 turn 搜索；
- 公开消息 allowlist、Markdown/UTF-8 预算、凭据过滤、Host 工具回执投影；
- 未绑定线程经原有认证 loopback Observer 的 `/v2/native` 读取，校验查询身份及只读标记。

Authority IPC 使用原来的 socket/UID 边界。Rust MCP 不打开任务数据库，不消费发送许可，
不启动 Provider，不回答原生审批或执行工具。Task Authority、事务、许可、outbox 和
恢复实现没有修改。未知投递保留原 operation ID，并返回 `DELIVERY_UNKNOWN`；
不重试、不降级 Python。重复 start 继续使用同一 prepared ref：新的 operation ID
返回当前 execution 回执；重复原 operation ID 仍由 Authority 返回原提交回执。

旧 `mcp_server.py` CLI exec 同 release 的 Rust binary，读取安装时生成的 JSON，
不再执行正常业务 Python server。其可注入 factory/codec 保留给显式维护、只读观察与原测试。
`RustIntegration` 仍用于正常 Node Helper 路由，未伪装成开发工具。

公开 schema 从原 HEAD 导出为唯一 `clinx/mcp/registry.json`，业务 15 项与观察 4 项
逐字段保持原数据。Rust 编译时嵌入，Python 观察/维护 codec 读同一份数据。
删除已无导入方的 `rust_mcp.py`（85 行）与 `consultation_schema.py`（26 行）。
`mcp_server.py` 从 1,394 行减至 392 行；这部分缩减包含退役 schema 构造器，
业务请求和读取迁移另由 Rust 实现及冻结行为向量证明，不以导出 JSON 充当迁移完成。

## 仍在运行的 Python 边界

| 位置 | 实际用途与不能删除的证据 |
| --- | --- |
| `clinx/authority/rust_integration.py` | 正常 Helper 的 `node_service_entrypoint.py` 仍构造 `RustIntegration + Commands(NodeAuthorityClient)`，承担业务请求映射与读取；Centre 也导入 Commands；显式维护仍使用 factory。新 Rust API 的 Authority trait 尚未绑定 Mac 上的配对反向请求通道，不能用 Linux Unix IPC 代替该认证链 |
| `clinx/nodes/`、`clinx/discovery/` | 现有身份、OPAQUE/mTLS、授权交集、Node 注册/反向路由、Provider chunk/去重；Rust Provider exchange 不等于这些网络职责已迁移 |
| `clinx/monitor/` | 当前观察目录、分享范围、缓存分页与读后权限重检；4 个观察工具仍需该运行时 |
| `clinx/provider/native_history.py`、`app_server.py` 等 | Helper/Observer 的原生历史、writer 观测及未迁移调用方仍导入；本批只替代正常业务 MCP 的精确读取 |
| `clinx/contracts/`、`clinx/host/` 配置 | Node/Discovery/维护仍读取现有配置、身份、策略；正常 MCP 配置改为安装时 JSON |
| `tools/`、`scripts/`、`fixtures/` | 开发、构建、资格验证与维护；没有改语言排除规则或删除测试来降低比例 |

4 个观察工具使用同 API 身份的只读目录子进程，没有新增 listener、systemd 服务或任务执行步骤。
本机 managed context 由它调用同 release `clinx-mcp --call clinx_get_context`，
保留目录的精确 task/execution/turn 检验及读后 scope 重检。
不代理 `/v2/observations` 回读 MCP，因此没有 Observer→MCP→Observer 循环。
远端观察上下文仍走原有认证 Node 路由；该部分是 Python 运行职责。
子进程启动失败时 MCP fail closed；不会静默移除观察工具或切回 Python 业务 API。

## 构建和发布

- `stage_rust_release.py` 新增必选 `--mcp-binary`，并标记 `mcp_runtime=RUST_MCP_V1`。
- 安装/调用端模板编译 `mcp-runtime.json`，MCP ExecStart 直接指向该 release 的 Rust server。
  构建工具 `tools.build.mcp_runtime_config` 只解析既有 TOML，不启动运行服务。
- 源码仍用 `python-sources.json`；非 Python 数据显式列于 `runtime-data.json`。
  Rust 与 Monitor 都携带必要的共享 schema，剔除两个退役模块。
- Helper 不运行业务 MCP server，Mac 包无需额外加入不用的 MCP binary。
  既有 Rust Provider companion、Python 观察与 Node 依赖仍保留。

本批只验证隔离产物，没有执行安装/stage/deploy 脚本或修改系统 service 文件。

## 验证回执

完整机器可读回执：[RUST_MCP_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_MCP_RUNTIME_MIGRATION_RECEIPT.json)。
私有证据目录：`/data/artifacts/clinx-mcp-rust-20261025`。

| 检查 | 结果 |
| --- | --- |
| Rust 全量测试，默认并行 | 最终 79 PASS / 0 FAIL / 0 ignored；基线 71 PASS |
| 原 Python API → Rust API 冻结向量 | 35 组；校验命令、完整 payload、Provider 精确读取与结果；覆盖 native/Host prepare、start、cancel、adoption、policy、历史上下文、URI/身份拒绝和 UNKNOWN |
| Rust 新增边界测试 | 15/19 工具 registry、通知、公开字段过滤、UNKNOWN operation ID、不重放、100 页历史、重复/循环页、UTF-8/Markdown/凭据过滤、Host ACK 投影 |
| 原 tracked fixtures | 基线 73 PASS；候选 Debug / Release 均 76 PASS；增加 MCP 入口参数，原 Authority 入口与断言仍执行 |
| Rust MCP→Authority→Transport→Provider | 本地、配对 mTLS、按节点 gateway 均通过；完成/精确上下文/输入 ACK 丢失/重启/取消 ACK 丢失/幂等；Provider 是资格 fixture |
| 观察后端 | 实际 Rust MCP + Python 目录子进程空列表只读调用通过；复杂分享撤销与真实 managed 目录内容的本轮端到端实机验证未执行 |
| Release | `cargo build --release --locked --bins` PASS |
| Rust/Monitor 独立包 | 明确源码/数据清单、包外全部 import、Rust companion、旧文件继承/增量清除 PASS；无 Bridge/Registry/M9/Dispatcher 等退役核心 |
| 结构、Ruff、App shell | PASS；根目录 0 个 Python 模块 |
| macOS Swift 构建、签名与实机闭环 | **未验证**：本轮在 Linux，没有启动 Mac/P620 真实业务任务 |
| 原本地 tests 完整运行 | **BLOCKED_EXISTING**：同 CPython 3.12.3、固定 dev deps，两侧均 51 PASS / 20 FAIL / 134 ERROR，逐项结果变化 0；其中 109 为 collection errors |
| 原 native/request 重定位测试 | 两侧均 34 PASS / 2 FAIL，变化 0；两项导入已退役 Python Dispatcher |
| 测试保全 | 本地测试和支持文件 hash 全部不变；没有删除/skip 测试或降低既有断言 |

一次候选 Rust 并行测试在 `node_owner_and_receipt_commit_together_at_fault_boundaries`
出现 `WRITER_ALREADY_ACTIVE`。保留该失败日志，并用迁移前原测试二进制运行两次：
退出码 `[0, 101]`，第二次复现同一位置、同一错误。最终全量 79 PASS 不抹掉该既有
偶发失败；本批未修改 Store writer 锁或该测试。尚未证明其精确竞争来源。

最初一次 fixtures 调用遗漏必要的 relay/provider binary 环境变量，配置补全后原 HEAD
73 项全通过。所有有结论的前后比较均使用同依赖与完整隔离环境，原始失败日志保留。

## 规模与下一步

口径为 Git 跟踪源文件及本批新增源码，含资格测试，不含被 Git 忽略的本地 tests：

| 语言 | 迁移前文件 / 行 / 字节 | 迁移后文件 / 行 / 字节 |
| --- | --- | --- |
| Python | 102 / 22,142 / 1,093,824 | 101 / 21,231 / 1,035,146 |
| Rust | 34 / 15,477 / 620,102 | 37 / 17,591 / 702,967 |
| Swift | 62 / 11,355 / 525,634 | 62 / 11,355 / 525,634 |

删除 2 个完整文件、111 行；另缩减 MCP 旧实现，包含新增构建与资格代码后，
Python **净减 911 行、58,678 字节**。ACTIVE_RUNTIME Python 从 55 文件 / 14,143 行
降至 53 文件 / 13,071 行。Swift 未改。

GitHub 当前基线语言 API 为 Python **47.98%**；按本批源码字节差量估算候选约 **44.93%**。
该值是发布前估算，不是 GitHub 已刷新结果。**25% 目标未达到。**

下一批应把正常 Node Helper 的请求映射接入现有 Rust API，并在 Rust 绑定已认证的
Node→Centre 反向请求/回执通道；之后才能删除 `RustIntegration` 及其读取依赖。
再迁移配对网络与观察/历史读取核心，才能实质移除余下大块 Python。
Mac 身份必须继续来自配对 mTLS，Provider 证据须精确关联，不能将 Unix socket/UID
契约跨设备套用，也不能以节点自报成功替代真实 Provider 证据。
