> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust Node Provider / workspace 迁移验证

基准：`bed0dc2e5240677e74780c1b105b73f6f4cf7446`，2026-10-25 Asia/Shanghai。
状态：**PASS（源码、Linux Release、隔离 Provider/mTLS、源码包回归）；NOT_DEPLOYED**。
MCP 和全部 Python 运行时迁移、Python 比例降至 25% 以下均 **尚未完成**。
机器回执：[RUST_PROVIDER_RUNTIME_MIGRATION_RECEIPT.json](cleanup/RUST_PROVIDER_RUNTIME_MIGRATION_RECEIPT.json)。

## 实际替代

新增 Rust `clinx-provider`，由现有 Helper 的已认证配对通道调用，接管：

- Node 对 Provider 请求的 thread、turn、execution、prompt、cwd、model/effort 和调用模式精确校验；
  不匹配在 Provider initialize 前拒绝。保留普通 user thread/start 的无工具/无额外指令约束。
- Provider Unix WebSocket RPC、精确的未加载线程错误识别；观察连接不回答工具或审批请求。
- 固定只读 workspace/Git 探针、Git 环境清理、canonical cwd、Git common-dir dev/inode、
  linked worktree、observer UID、输出上限及超时。Authority 仍校验观察结果并原子提交。
- 按 host 复用现有 Centre gateway；远端身份来自原有配对 mTLS 和当前授权交集。
  Linux peer credentials 只验证本机 Unix 对端，Mac 使用配置的同用户本地 socket 边界。

`clinx/nodes/provider_relay.py` 保留 mTLS chunk 缓冲、当前范围检查和协议去重，
把已封口的请求通过 stdin 传给 Rust。prompt 不放进子进程命令行。
丢失子进程/Provider 响应只返回不确定，不重试；原有 IN_FLIGHT/READY/CONSUMED 行为保留。
它仍是 **Python ACTIVE_RUNTIME**，不是已退役核心或开发工具。
执行状态、许可、幂等、恢复及 outbox 继续由既有 Rust Authority/native Transport 管理。

删除 `clinx/provider/rust_workspace_probe.py`：**1 文件，137 行**。
运行源码、发布源码清单及旧 `deps/` 继承均不再携带它；未改名或移动成 Python adapter。
Python ACTIVE_RUNTIME 从 **56 / 14,321 行 → 55 / 14,143 行**，净减 **178 行**。

## 产物接线

Rust release 增加必选的 `--provider-binary`，与本轮 Authority binary 同批打包；
标记 `provider_exchange=RUST_PROVIDER_V1`、`workspace_observer=RUST_PROVIDER_V1`。
Authority 默认从同级 `clinx-provider` 执行观察。服务安装/调用器 staging 校验新契约，
不能将新 Authority 和缺少该 Helper 的旧 release 混用。未执行任何安装或 staging 生产命令。

Monitor 构建使用本机 macOS Rust target，产出 `Contents/Helpers/clinx-provider`，
先单独 codesign/verify 再签 App。Helper launcher 固定该配套路径；
包外资格也验证不依赖 checkout、PYTHONPATH 或开发环境的二进制覆盖变量。
解释器和 discovery wheel 仍需打包，因为 Node/Discovery、历史读取等尚为 Python 运行时。
本批没有可用的 macOS SDK、签名身份或独立 Mac 资格设备；没有把 Linux 包测试当作 Mac 构建。

## 验证

证据：`/data/artifacts/clinx-provider-rust-20261025`。数据库、仓库、socket 和节点均为任务所有的 fixture。

| 检查 | 结果 |
| --- | --- |
| `cargo build --release --locked` | PASS |
| Rust 全量默认并行测试 | 71 PASS，0 FAIL；基准 69 PASS |
| 全部跟踪 fixtures，Release binaries 驱动 | 73 PASS，0 FAIL；基准 60 PASS |
| 原生执行、丢 ACK、恢复、取消、身份/并发 | 原有本机、配对 mTLS、按节点路由和 Host 用例全部保留并通过 |
| Python→Rust workspace 等价 | 15 个真实 Git/linked-worktree/error 输入逐字段一致 |
| 新 Provider/observer 调用链 | 实际 WebSocket→Rust 探针；未响应 unsolicited approval/tool 请求；错误 peer UID、返回 UID、cwd/方法/精确身份拒绝 PASS |
| 远端 workspace | Rust observer→Centre gateway→配对 mTLS→Node→Rust Provider→Rust probe PASS；错误 host 路由、项目范围、UID 拒绝 |
| Rust / Monitor 源码包 | 仓库外分别导入 52 / 50 模块；两种正常相对路径均找到配套 Rust binary 并完成 metadata 调用；无旧探针 |
| 增量打包和继承依赖 | 原有核心清除断言保留；新增 Rust binary/hash/manifest 和旧 flat probe 排除断言 PASS |
| 源码分类、import closure、Ruff、shell 语法、diff 空白 | PASS |
| 本机被 Git 忽略的历史 Python tests | BLOCKED_EXISTING：相同 CPython 3.14.7/pytest 9.1.1 环境下前后均 52 PASS / 26 FAIL / 127 ERROR / 0 SKIP；其中 109 为 collection errors；逐项结果变化 0 |
| 本地 MCP/原生请求重定位回归 | 前后均 34 PASS / 2 FAIL；两项仍导入上一批已退役的 Python Dispatcher；新增变化 0 |
| 历史测试保全 | 121 个 Python 测试源码 SHA-256 不变；没有删测试、skip 测试或放松断言 |
| 真正的 Mac 编译、签名、跨设备业务执行 | NOT_RUN；本轮没有资格 Mac，未部署、未操作现役任务 |

历史测试计数与上一批不同，本批使用 CPython 3.14.7、完整 requirements-dev pins 和禁用插件自动加载；这里只对比同环境的
原始 HEAD 和改动源码。资格基准是 task-owned 临时仓库的精确 `bed0dc2`，
保留 bundle/patch/日志后移除副本及可再生构建输出。
首轮 fixtures 漏设 `CLINX_MCP_RELAY` 导致 4 个 setup error；补齐配置后全部通过。
基准源码最初仅为无 Git metadata 的 archive，导致 release fixture 1 失败；
补齐原 HEAD Git metadata 后该原始 package fixture 3 项重跑通过。两者均为资格环境修复。
新 Authority observer 用例最初缺少必填 title，修正 fixture 输入后通过；未修改旧断言。
本地重定位测试初次缺少 jsonschema，安装仓库锁定开发依赖后重跑；
基准副本补齐被忽略的 tests/pytest.ini 后，两个版本均为 34 PASS / 2 FAIL。

## 规模与剩余工作

| 跟踪源码口径 | 之前 | 之后 |
| --- | ---: | ---: |
| Python 文件 / 行 | 102 / 21,969 | 102 / 22,142 |
| Rust 文件 / 行 | 31 / 14,788 | 34 / 15,477 |
| Swift 文件 / 行 | 62 / 11,355 | 62 / 11,355 |

运行时 Python 减少，新增跨协议资格测试使 **Python 总源码反而净增 173 行**。
不能据此声称 Python 已明显退役。按当前 GitHub languages 基准与源码字节差量估算，
Python **48.34% → 47.98%**；GitHub 重新统计前该值是估算，25% 目标仍未达到。
没有排除资格测试、改 Linguist 配置或添加无功能 Rust 来调整比例。

仍保留的 Python 运行职责：

| 模块组 | 具体用途与尚未迁移的边界 |
| --- | --- |
| `clinx/mcp/` | MCP framing/schema、工具分发仍在 Python；本批未实施 MCP server 迁移 |
| `clinx/authority/`、`clinx/contracts/` | API 请求/策略/选择器映射、原生上下文读取、配置和 Rust IPC 客户端；迁移须保持现有公开错误和精确读取契约 |
| `clinx/nodes/`、`clinx/discovery/` | 当前配对身份、mTLS、mDNS、Node 注册、授权交集、chunk/分页/去重仍在 Python；新 Rust exchange 不代表网络栈退役 |
| `clinx/provider/` | Monitor/MCP 的 Codex 客户端、原生历史、writer 存活与上下文投影仍有调用者；不能删除整个 `app_server.py` 或历史读取 |
| `clinx/monitor/` | 状态与观察投影、用户范围检查、Swift/HTTP 消费入口；仍需迁移运行逻辑 |
| `clinx/transport/`、`clinx/host/` | Tunnel supervisor 和 TOML workflow 配置类型仍被运行入口使用；Host 执行已在上一批迁 Rust |

下一纵向边界是 MCP 服务及其 Authority 请求映射/精确上下文读取；
迁移前不能仅导出 schema JSON 或保留 Python 调度后声称 MCP 已迁 Rust。
真正 Mac 构建和 Mac→Centre→Rust→Mac Provider 的实机资格需要可用的独立 Mac 环境，
本次不通过部署生产或运行真实任务来获取 PASS。
