# CLINX 目标架构与源码布局

唯一目标：**Rust 承担 Task Authority、执行生命周期、持久化、幂等、恢复、MCP、Provider、
Host、Transport、Node 与 Discovery 的长期核心；Swift 负责 macOS UI；Python 最终仅保留
必要的开发、构建、资格验证和维护工具。**

当前实现尚未达到这一目标。Rust 已接管 P620 任务权威；大量协议、Provider、Host 和节点
实现仍是 Python，Mac 还保留可写的旧业务核心。目录分组和包名不会改变这项事实。

## 布局

| 位置 | 当前职责 | 分类与目标 |
| --- | --- | --- |
| `kernel/` | Rust Authority、状态/持久化/幂等/恢复、stdio byte relay | ACTIVE_RUNTIME；长期核心 |
| `native/opaque_pairing/` | Rust OPAQUE 原生配对依赖 | ACTIVE_RUNTIME |
| `MonitorApp/` | Swift macOS UI、App 构建入口、Helper shell launcher | ACTIVE_RUNTIME；UI 留在 Swift |
| `clinx/contracts/` | 公共配置、身份选择、策略与响应契约 | ACTIVE_RUNTIME；运行职责待迁 Rust |
| `clinx/authority/` | 当前 Python Rust IPC 客户端、请求映射、状态读取 | ACTIVE_RUNTIME；不等于 Rust Authority 实现 |
| `clinx/mcp/` | 当前 Python MCP framing/schema | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/provider/` | Codex 协议、原生历史、精确证据读取和观测判断 | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/host/` | Host 子进程执行、操作契约和工具投递 | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/transport/` | 当前 Python Dispatcher transport、Tunnel supervisor | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/nodes/`、`clinx/discovery/` | Node 路由、注册、Helper、mDNS/配对 | ACTIVE_RUNTIME；待迁 Rust |
| `clinx/monitor/` | 监控与观察投影、只读历史 association | ACTIVE_RUNTIME；运行职责待迁 Rust |
| `clinx/compat/` | Mac Bridge/Registry/M9、完成、持久化活性、取消/恢复、旧 Registry projection、metadata outbox | COMPAT；仍是业务核心，完整迁移前不可直接删除 |
| `tools/build/` | 共用运行源码清单处理与增量 App 拷贝 | DEVOPS |
| `tools/maintenance/` | 显式维护工具 | DEVOPS；不进入运行包 |
| `tools/qualification/`、`fixtures/` | 独立验证、参考实现、回归 fixture | QUALIFICATION；不进入运行包 |
| `scripts/` | 构建、候选 release、安装和历史维护入口 | 各文件分类见清单；运行不因文件存在而获得授权 |
| `architecture/` | 源码分类、发布成员、旧路径/符号迁移映射 | 构建与审查事实来源 |
| 根目录 | 项目说明、配置、依赖声明；**没有 Python 模块** | CLI 入口集中于 `bin/` 或限定包的 `-m` 命令 |

Python 每个模块的 ACTIVE_RUNTIME / COMPAT / DEVOPS / QUALIFICATION / DEAD 分类、用途和
包归属只在 [python-sources.json](../architecture/python-sources.json) 声明。
DEAD 项注明替代实现，源码应不存在。`module-moves.json` 为导入/路径迁移记录，
不是运行时 import alias；生产代码没有平铺旧模块 shim。

`kernel/src/runtime_lab.rs` 仍被 `authority_state.rs` 复用，属于 ACTIVE_RUNTIME。
`tools/qualification/runtime_lab/` 是 Python/探针资格实现，两者不能因为名称相似一并删除。

## 发布规则

Rust release 和 Monitor bundle 使用同一份 `architecture/python-sources.json`，由
`tools/build/runtime_sources.py` 校验。Rust profile 禁止 COMPAT/DEVOPS/QUALIFICATION。
Monitor profile 明示其所需兼容模块；新增文件不会通过目录 glob 自动进入包。

运行包保留 `clinx/<职责>/<模块>.py` 层级及 package initializer。源码根目录或 release 的
`control/` 是 Python import root；Helper 使用 App Resources 作为 import root。
包内没有源码仓库、`tools/`、本地测试、旧平铺 `.py` 或 `Discovery/` 副本。
增量 App 构建清除旧 Python 拷贝目录，保留解释器、原生 wheel 和 Swift 资源。

`scripts/stage_rust_release.py` 需要匹配新布局、已测试的 `--runtime-binary` 和
`--mcp-relay-binary`。不能只更换 Python 文件再继承引用旧路径的 Authority binary。
清单标记 `source_layout=qualified-clinx-v1`；新服务模板仅接受该布局。
候选发布仍为 STAGED_NOT_SELECTED，构建不会选择 production target。

旧 forwarding builder / control overlay 只服务历史平铺产物的证据恢复：对当前源码明确拒绝，
不得绕过清单重新打包旧 Python 核心。需要历史复现时从 Git 原提交恢复精确源码，
不要用新包文件覆盖旧不可变产物。Tunnel supervisor 的独立打包继续有效。

## 项目入口

```sh
# 从源码根目录运行；配置和 socket 必须属于指定的开发/资格环境。
python -m clinx.mcp.mcp_server --config <config> --stdio
python -m clinx.transport.rust_transport --config <config>
python -m clinx.nodes.node_centre_entrypoint --help
bin/clinx-observer
bin/clinx --help

# stdio relay 本身已改用 Rust，透明转发到已有 MCP Unix socket。
cargo build --locked --release --manifest-path kernel/Cargo.toml --bin clinx-mcp-relay
CLINX_MCP_RELAY=<tested-binary> bin/clinx-context-mcp

# 不访问服务的结构检查。
python -B -m tools.qualification.check_structure
```

MCP 保持 `CLINX_AUTHORITY=rust` 与既有 socket/UID 配置契约。以上命令不是生产安装步骤。
`bin/clinx-context-mcp` 不再调用 Python；Rust relay 只复制字节，保留 stdin 半关闭和
对端 EOF 语义，不解析 MCP、不重试输入、不拥有任务状态。

## 未迁移边界

Mac `CanonicalNodeExecutionAdapter` 仍构造 `clinx.compat` 中的 Registry、Dispatcher、M9。
Rust node adapter 已接入 canonical 身份与精确 predecessor adoption 契约。
第七轮增加了 Node→Centre→Rust 的认证反向请求，归属、范围、幂等与收据检查由 Rust 提交；
Provider/observer 端点也由 Rust 按 host 选择。Mac 网络请求不使用 Linux peer credentials。
远端 Provider RPC、Mac workspace 证据及 metadata outbox 尚未接通，Helper 默认路径未切换，
不能删除旧核心。当前实现、实机验证范围与具体阻塞见
[Rust 节点路由](RUST_NODE_ROUTING.md)。

Python MCP、Provider、Host、Transport、Node/Discovery 也仍是长期迁移工作，不能因为位于
`clinx/` 而称作纯开发工具。本轮实质替换的是 stdio byte relay；其余结构/依赖调整不计作
Rust 业务迁移。当前验收范围见[第五轮收敛报告](cleanup/REPOSITORY_CONVERGENCE_PHASE5.md)。

后续业务契约进展及尚未切换的 Mac 路径见
[Rust canonical handoff](RUST_CANONICAL_HANDOFF.md)。第五轮报告保留其当时的验收范围。
