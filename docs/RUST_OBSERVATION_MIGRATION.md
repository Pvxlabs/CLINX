> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust 原生历史与观察目录迁移

日期：2026-10-25。基线：`95c6b564f89731ba87850e1e8ceb6aed0c8ac543`。
状态：**本轮源码迁移与隔离回归 PASS；总体目标尚未完成；NOT_DEPLOYED**。
本报告延续 Node 网络迁移，不重复结构整理、canonical handoff 或旧 Mac 核心退役。

## 实际迁移

- `kernel/src/native_history.rs` 接管 Codex native index/history SQL、分页、JSONL 有界读取、
  legacy 增量扫描、partial/oversized record、fork rollout 与 projection offset/turn 身份、
  workspace move evidence、精确模型证据及旧 reply-template retirement 检测。
  原生数据库只读，不初始化 schema。SQLite progress budget 为 250,000 steps，
  展示字段采用 allowlist、隐私脱敏和 UTF-8 字节预算，保留 Markdown。
- `kernel/src/observation.rs` 和 `observation.sql` 接管现有观察数据库 schema/migration、
  BEGIN IMMEDIATE 原子批次、stream CAS、重复/冲突/缺口、终态合并、保留策略、
  HMAC 游标、目录/详情/activity/context 查询、授权交集与精确只读 association。
  context 读取后重新检查当前授权，并打开新只读快照核对线程与 turn，避免返回过期关联。
  这个数据库是展示投影，不是 Task Authority，不提交任务状态或 Provider 结果。
- `kernel/src/read_projection.rs` 提供只读快照、行 codec 及旧 Python compact JSON 字节兼容。
  保留 Unicode、浮点科学计数阈值/指数补零/负零，使已提交摘要和游标在迁移后继续有效。
  HMAC 使用标准 `hmac 0.12.1`，依赖固定在 Cargo.lock。
- Python `native_history.py`、`network_observation.py`、`native_associations.py` 移除上述
  SQL、解析、校验、存储和投影实现，通过既有 `clinx-node` 私有 stdio 协议调用 Rust。
  `observation_source.py` 的 native inventory/turns/legacy 扫描也已接线；保留原调用接口。
  没有为业务状态添加另一个 Python adapter 实现。
- Observer 的 NodeRegistry 只读入口仅允许 get/list/scope，不建库、不拿 writer lock。
  身份对象、既有授权文件与远端协议调用仍经原有 Python 装配，Rust 决定授权交集和投影。

任务身份、Authority 生命周期、Provider 执行与证据提交、业务取消、metadata outbox 均沿用
现有 Rust 契约。本轮没有执行真实任务、盲目重试、生产数据库访问或服务操作。

## 发布与调用端

增加 `observation_runtime=RUST_OBSERVATION_V1`，并实际执行
`clinx-node --observation-contract` 校验。Rust staging、caller staging、安装器、
Monitor 源码 manifest 和 `build-app.sh` 同步要求此契约，拒绝旧 companion binary。
没有 Python 回退实现，也不在运行时自动编译。

发布仍使用 `architecture/python-sources.json` 的显式闭包，Rust/Monitor profile 均拒绝
COMPAT、开发和资格模块。两种 profile 的隔离包验证内部 import、Helper 历史读取与退役
模块残留；App 增量源码拷贝清除旧目录。正常 Helper 与调用端不构造旧 Bridge/Registry/M9。
本轮保留其 public Python 客户端文件，不把仍有调用者的接口整体删除。

## 验证与失败分离

| 验证 | 结果 |
| --- | --- |
| 全部 10 个 Linux Rust Release binaries 构建 | PASS，含源码与二进制 SHA 回执 |
| Rust Release 测试，串行执行 | 84 PASS，0 FAIL，0 ignored |
| 仓库跟踪的资格测试 | 134 PASS，无新增 skip |
| 原观察测试的独立 33 cases | PASS；保留原 AST 测试体/断言，仅做既定 import relocation |
| 独立旧算法对比 | 1,035 PASS，含 1,000 随机有限 f64、旧摘要批次重读/重 ACK |
| 原本地套件前后 | 同为 295 PASS / 31 FAIL / 104 ERROR / 2 既有 SKIP；432 个身份逐项相同 |
| 原本地测试及 support 文件 | 124 个 SHA256 不变 |
| 两种源码发布 profile、import closure、退役残留 | PASS |
| 重写 Python 文件完整 Ruff、受影响 Python E9/F 检查 | PASS |
| 新 Rust rustfmt、Helper shell syntax、Git diff whitespace | PASS |
| strict Clippy `-D warnings` | BLOCKED_EXISTING：前后相同 10 项旧诊断，无新增 |
| macOS 原生构建、签名和安装包 | 未验证；当前主机为 Linux，无 macOS toolchain |
| 新产物物理 Mac→P620→Mac 执行/取消/恢复 | 未验证；本轮仅隔离协议 fixture，没有部署 |

旧套件的失败/错误包括已退役 legacy 模块导入、旧发布布局与隔离设施前提；逐项结果保留，
不把它们计为 PASS。没有删除既有测试、弱化断言或增加 skip。原跟踪 package fixture
只扩充 synthetic executable 对新契约探测的响应，原断言不变。

新回归覆盖：精确线程/host/source 游标、fork/turn/offset 冲突、Markdown/Unicode/隐私、
legacy 增量 checkpoint 与 partial record、原生权限不足、并发 worker 的重复批次、
序列缺口原子回滚、内容冲突、终态不回退、重启重 ACK、旧浮点摘要与签名游标、
授权撤销、复制线程冲突、实际远端 callback、本机 read_only 和远端读取期间投影变化。
完整资格集还继续执行原生 Transport/MCP/Helper 的完成、取消、未知 ACK 和恢复用例。

## 源码规模

沿用上轮六语言源码字节口径（Python/Rust/Swift/Go/C/Shell），包含跟踪资格测试与本轮
新增源码，不修改 Linguist，不以在线 GitHub 刷新值代替源码计量。新增 SQL 单独作为
嵌入 schema 数据，不计入这六语言比例。可复算记录见 JSON 回执。

| 项目 | 迁移前 | 迁移后 |
| --- | ---: | ---: |
| Python 文件 / 物理行 | 105 / 21,188 | 106 / 21,075 |
| Python bytes | 1,015,845 | 979,380 |
| ACTIVE_RUNTIME Python 文件 / 物理行 | 55 / 12,495 | 55 / 11,714 |
| ACTIVE_RUNTIME Python bytes | 602,841 | 541,459 |
| Rust 文件 / 物理行 | 50 / 20,656 | 53 / 23,207 |
| Rust bytes | 824,735 | 925,720 |
| Swift 文件 / 物理行 / bytes | 62 / 11,355 / 525,634 | 62 / 11,355 / 525,634 |
| Python 六语言字节占比 | 42.2062% | 39.6273% |

整文件删除 **0**。现有 Python runtime diff 移除 **1,156** 行、加入 **375** 行，
净减 **781 行 / 61,382 bytes**；计数包括导入、空行与接口，不能称为纯业务逻辑行数。
全仓 Python 净减 113 行；新增 36 个参数展开后的回归 cases 的测试文件共 653 行。
Rust 净增 2,551 行，另加 19 行嵌入 SQL。Python 比例下降同时来自实际运行代码减少和
Rust 实现增加；**距离 25% 目标仍有差距，不能宣称全量迁移完成**。

## 保留 Python 与下一步边界

以下均仍标 ACTIVE_RUNTIME，没有包装成开发工具：

1. `clinx/monitor/observation_source.py`：Collector 调度、SQLite spool/outbox、sample merge、
   身份注册、live proof 缓存和历史 canonical SQL 读取。必须将 checkpoint/outbox/批次 ACK
   作为一个 Rust 事务迁移，并用 Authority 读取契约替代历史 SQL seam，才能删除这些职责。
   此队列与 Authority 的 metadata outbox 不同；后者早已迁 Rust。
2. `clinx/provider/app_server.py`、`native_provider.py` 与 `contracts/thread_identity.py`：
   Provider 子进程/协议对象、只读客户端、精确 live-writer probe 和仍在用的身份约束。
   现有 Rust Provider exchange 不等于全部 Python 观察客户端已退役，需要替换这些调用者。
3. `clinx/monitor/observer_server.py`、`clinx/mcp/` 及 Node 观察 router：HTTP/MCP 观察 codec、
   本地 managed projection、分页路由/coverage 与 listener 装配。目录核心已在 Rust；
   这些调用端尚未切成完整 Rust 服务，不能从包中猜测性移除。
4. `clinx/discovery/`：mDNS、OPAQUE enrollment、身份建立与设置控制；已配对后的 Node TLS
   和授权校验由 Rust 承担。原生 OPAQUE wheel 的存在不能替代 enrollment 控制迁移。
5. `clinx/nodes/*entrypoint.py`、`clinx/transport/`、`clinx/contracts/`、`clinx/authority/`：
   监听/注册/心跳、tunnel supervisor、配置解析、私有 IPC 与现有对象装配。
   仍有运行调用者，需逐条替换后删除；`native_history.py` 等本轮保留的客户端也依赖这些边界。

下一包优先完成第 1 项 Collector 队列/ACK/checkpoint，再收口 Provider 观察与 Observer
调用端；随后迁 Discovery/enrollment 与 supervision。macOS 新产物的原生构建/签名和
独立测试身份的跨设备资格仍需在 Mac 完成，不以旧部署证据代替。

原始证据：`/data/artifacts/clinx-observation-rust-20261025/`，含前后 JUnit、测试身份对比、
差异算法、原测试摘要、二进制摘要、服务只读快照与恢复 bundle。
机器可读结论：[迁移回执](cleanup/RUST_OBSERVATION_MIGRATION_RECEIPT.json)。
