# Rust native 执行核心退役验证

本轮从 `69b2471e22d9f693a2bb4ece306c90a7f9b6a26f` 的 main 开始。
完成的是 **Mac 旧业务核心退役，以及 Rust 原生执行纵向接线**。
Python 全运行时迁移尚未完成。本轮没有部署、选择 production release、重启生产服务，
没有访问真实业务任务或写入生产数据库。

## 实际替换与删除

`kernel/src/provider.rs` 实现现有 Codex WebSocket JSON-RPC 客户端；
`kernel/src/native_transport.rs` 驱动 canonical adoption、线程创建、精确 input receipt、
完成观察、取消、ACK 不确定恢复及 metadata outbox。
`clinx-native-transport` 与 Authority 使用现有 Transport principal 和本地 IPC，
不打开 Authority 数据库，不自行裁决任务终态。

Mac Helper 的正常入口直接构造 `RustNodeExecutionAdapter`，通过已有 OPAQUE 配对/mTLS
调用 Centre→Rust Authority。Provider RPC 则从 Rust worker→Centre gateway→配对 Mac
转发到现有 Codex Provider。Mac hop 不使用 Linux peer credentials。
NodeRegistry 仅承担设备、范围、分页和协议请求去重，Rust callback 不再领取第二份本地 task fence。

删除 `clinx/compat` 全部 25 个源文件，以及绑定旧 SQLite 状态的
`reconcile_historical_handoff.py`、`reconcile_task_projection.py`，共 **27 文件、16,203 行**。
包括 Bridge、TaskRegistry、M9、completion、result ingestion、prepared requests、
native input/unstarted recovery、取消链、thread identity、domain 和 shadow ledger。
没有把这些实现转移到 tools 或更名为 adapter。历史离线库修复需恢复精确 Git 版本。
Discovery CLI 移除对 Bridge 的 fallback，任务操作使用 Rust MCP/Authority 契约。

完整删除清单、源码规模、测试身份及实机精确标识见
[机器可读收据](cleanup/RUST_NATIVE_CORE_RETIREMENT_RECEIPT.json)。

## 保持的权威和恢复语义

- task/execution/thread/turn 仍精确绑定；canonical task UUIDv5 规则保持不变。
- adoption 必须由 Transport 独立读取 Provider 的 idle thread 和精确 predecessor terminal turn，
  再由 Rust 原子提交身份、checkpoint 和 binding。Node 的成功布尔不构成证据。
- thread/start 之前消费 Rust 创建许可；创建 ACK 不明时不得再创建或猜测最近线程。
- authorize_send、create 和 cancel 的物理许可使用新的请求 ID，避免把 cached true 收据当作再次执行许可。
- turn/start ACK 不证明投递，必须读取相同 client UUID 和原文 userMessage，再提交 input_commit。
  SENDING/UNKNOWN 重启只扫描证据，不重投输入；未知结果继续保留受影响 execution 的资源。
- 完成与取消仅以精确 Provider terminal evidence 经 Authority 提交后释放资源。
- 新线程绑定与 metadata outbox 同事务；标题发布不覆盖用户标题。
  标题 ACK 丢失先读回，终态执行仍可处理 outbox，重启不丢其 durable 状态。
- workspace observer 按 host 选 Provider，由该 Provider 执行固定只读探针，验证远端 cwd、
  repository common-dir identity 和 observer UID。没有将 P620 本地目录证据代替 Mac 证据。

## 发布边界

Rust 与 Monitor 两个 profile 都禁止 COMPAT，构建和结构检查验证完整 import closure。
Rust stage 仅从旧 release 继承 `deps/` 和 native `host-worker`；所有 Python 源码来自当前清单，
旧 release 根目录、旧 package 或 control 下的退役模块均不继承。
必须显式提供已验证的 Authority、MCP relay 和 native Transport binary。
本轮 stage 在任务目录 fixture 中运行，没有写 `/var/lib/clinx-rust/releases`。

Monitor 使用原 `build-app.sh` 在隔离 Mac 源码包中构建：Swift release、打包解释器和 OPAQUE wheel、
源码闭包复制、签名及 `codesign --verify --strict` 通过。最终包中 55 个声明的 Python 模块逐文件
与当前源码及 SHA256 对照，没有 compat 或平铺旧核心；原生 Swift executable hash 记录于收据。
没有安装该 App。资格结束移除可再生 App/源码/临时仓库及资格凭据，保留日志与收据。

Centre gateway 支持多个独立 endpoint；Authority 的 endpoint→host 唯一性约束保持不变。
映射配置见[仓库架构](REPOSITORY_ARCHITECTURE.md#远端路由配置契约)。
production 接线尚未部署，不能从源码通过推断已安装 Helper 已切换。

## 实机闭环

资格使用独立 `centre8` / `air8` / `qualification8`，独立 Authority state、Mac Provider HOME、
配对身份和空资格仓库。复用 Mac 现有 SSH 传输到 P620 的能力，配对与 RPC 仍使用现有 mTLS 身份边界。
Mac 仓库为 `/private/tmp/clinx8-mac-20261025/repository`，observer UID `501`，
repository identity `git-common:16777232:395273805`。这些均为本轮隔离环境，非生产任务。

| 用例 | 实际结果 |
| --- | --- |
| Mac prepare→Rust→Mac Provider→完成 | `SUCCEEDED/RELEASED`，原文 `CLINX_RUST_MAC_QUALIFICATION_OK` |
| 输入已接收、故意丢 Provider ACK、SIGKILL 资格 Authority 后重开 | 保持 SENDING/UNKNOWN，精确读取恢复为 SUCCEEDED；该 client UUID 仅一份输入 |
| 运行中取消、故意丢 interrupt ACK | 第一次结果未知；只读恢复为 `CANCELLED/RELEASED`，相同 client UUID 仅一份输入 |
| 真实 native predecessor→canonical adoption→继续执行 | 保留 canonical task ID 与 predecessor checkpoint，返回 `CLINX_CANONICAL_HANDOFF_RUST_OK` |
| 从已构建 App Resources 启动真实 `node_service_entrypoint.main` | 正常完成 `CLINX_BUNDLED_HELPER_RUST_OK`；未创建旧 task DB；重复 start 无新输入 |
| 带显式 thread identity 的重复 Helper start | Rust 返回相同 execution 收据，无第二份本地 writer fence |
| 最终 Provider 原始历史与 Authority work 读回 | 丢 ACK/取消用例精确输入各一份，在途 execution 为 0 |

canonical 实机第一次读取未满足 idle，Authority 拒绝提交。资格原生客户端保持连接后读取 idle，
同一契约通过；没有放宽该拒绝条件。首次历史读取遇到尚未写出的 rollout 时，仅重读已知 thread/turn，
没有重新发送输入。单节点 Mac 闭环已验证；多台真实 Mac 同时执行尚未验证。

## 构建和回归

| 验证 | 状态与证据 |
| --- | --- |
| Rust Release `cargo build --release --locked` | PASS；包含三个本轮要求的 binary |
| Rust 全测试 `cargo test --locked -- --test-threads=1` | PASS，64 项；保留原测试，新增 native Transport/Provider 故障与竞争许可用例 |
| 默认并行 Rust 测试 | 一次 `node_owner_and_receipt_commit_together_at_fault_boundaries` 在 Store reopen 报 WRITER_ALREADY_ACTIVE；后续完整串行通过。未证实历史归属，未声称并行全绿 |
| 跟踪的受影响 Python fixtures | PASS，40 项；真实 Authority/worker 子进程、local/paired/routed Provider fixture、身份冲突、并发重复、丢 ACK、重启、取消与发布退役 |
| Rust release 实际 stage 函数 | PASS，隔离 fixture；真实已测试 binary、继承白名单、完整 hashes、旧 base drift 拒绝 |
| Mac Swift release、Helper 包、签名、最终闭包导入 | PASS；实机资格与静态包验证分开保存 |
| 原本地受影响 Python suite | 51 PASS、1 既有失败、1 新收集错误；详见下文 |
| 完整历史 Python suite | BLOCKED；删除前 24 个收集错误，删除后 94 个，新增 70 个，无消失的原错误 |
| 生产部署与真实业务任务验收 | 未执行，本轮未授权范围 |

原本地测试 129 个文件逐字节 SHA256 保持不变，没有删除测试、加入 skip 或更改断言。
受影响 suite 的既有失败为
`test_node_runtime.py::test_product_centre_and_air_helper_register_as_separate_processes`，
子进程仍引用已经退役的根目录入口。新增收集错误为 `test_rust_desktop_daemon.py` 导入已删除 Bridge。
删除前该组合为 62 PASS / 1 FAIL，删除后其中依赖 Bridge 的 11 项无法收集。
继续收集后仍可运行的 51 项通过，同一既有失败保持。
完整历史 suite 新增的 70 个模块收集错误来自退役内部 API 的引用，列表随收据保留；
这是实际兼容性变化，不能宣称完整 Python 回归 PASS，也没有以旧源码快照替换被测对象。

复验需使用匹配环境、隔离状态及短 Unix socket 路径，例如：

```sh
CARGO_TARGET_DIR=<task-build-dir> cargo test --locked --manifest-path kernel/Cargo.toml -- --test-threads=1
CARGO_TARGET_DIR=<task-build-dir> cargo build --release --locked --manifest-path kernel/Cargo.toml
CLINX_TEST_RUST_BIN_DIR=<task-build-dir>/release python -B -m pytest -q -p no:cacheprovider \
  --basetemp=/data/artifacts/<unique-task>/pytest \
  fixtures/test_provider_relay.py fixtures/test_native_transport_binary.py \
  fixtures/test_rust_node_contract.py fixtures/test_runtime_packages.py
python -B -m tools.qualification.check_structure
```

fixtures 中 Provider 是协议 fixture，不能当作实机证据。实机资格脚本、原始 Provider/Authority 收据、
XML 与构建日志保存在 `/data/artifacts/clinx-core-retirement8-20261025/evidence`，带独立 SHA256 索引。
临时配对凭据、auth.json、资格数据库及可再生构建输出不进入该证据归档。

## 规模与未完成项

计数为 Git 源码文件的物理行，包含跟踪的资格工具/fixtures，不包含忽略的本地 tests 或第三方构建输出。

| 语言 | 清理前文件/行 | 清理后文件/行 |
| --- | ---: | ---: |
| Python | 126 / 39,410 | 103 / 24,129 |
| Rust | 24 / 10,711 | 29 / 12,297 |
| Swift | 62 / 11,355 | 62 / 11,355 |

仍有 60 个 ACTIVE_RUNTIME Python 模块，实际用途为：共享配置/契约、Rust IPC/请求映射、MCP、
Host 动态工具与子进程、Node/Discovery 配对和网络、Provider 协议桥与历史读取、监控投影及 Tunnel。
逐文件分类见 `architecture/python-sources.json`。不能将这些代码统称纯开发工具。

具体剩余迁移边界：

1. Python MCP schema/framing、Node/Discovery/OPAQUE 网络服务、监控/历史读取尚无完整 Rust 替代。
2. Provider 网络桥仍由 Python承载已有 paired mTLS；Rust 已拥有原生生命周期及证据裁决，
   但该网络协议服务自身尚未迁移。workspace 固定探针和配置解析也仍是 Python。
3. Host 动态工具、子进程执行、Host successor 与恢复仍在 Python。
   `rust_transport.py` 的服务路径仅给 Python tick 分配 HOST_EXECUTOR，
   但直接 Python `RustDispatcher` native 方法仍存在，供原有直接调用者/资格测试使用；这部分重复边界尚未删除。
4. 两个历史 forwarding build/deploy 脚本保留为 COMPAT 工具，仅接受精确历史源码，两个运行包都不包含它们。
5. 原历史测试对退役 API 的引用，以及一次并行 Store reopen 的瞬态锁失败，尚待独立处理。
6. 新代码未部署；已安装的生产 Helper/服务仍运行其不可变 release。多真实设备并发与生产升级验证未执行。

因此本轮结论是：**旧 Mac 本地业务核心退役与原生 Rust 闭环 PASS；Python 全运行时迁移未完成；
完整历史 Python 回归 BLOCKED；生产部署未执行。**
