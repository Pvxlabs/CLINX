# Python 源码最终退役

基准：`52f6ce9244baa1dcfb4a7b514f2ca9c569e67e12`，2026-10-25。
本轮完成当前仓库源码中的 Python 退役；**不部署、不重启服务、不操作真实任务或生产数据**。
机器回执见 [RUST_PYTHON_RETIREMENT_RECEIPT.json](cleanup/RUST_PYTHON_RETIREMENT_RECEIPT.json)。

## 实际交付

删除最后 **91 个 Python 文件、12,527 行、553,115 字节**。固定口径包含开发、构建、
资格代码，统计受 Git 跟踪及非忽略的 Python/Rust/Swift/Go/Shell/C；Python 字节占比
由 **16.8872% 降到 0%**。没有修改忽略规则或 Linguist 语言分类来改变比例。
本轮删除文件总数为 **100 个、13,570 行**，其中 Python 为上述 91 个。

| 语言 | 清理前文件 / 行 / 字节 | 清理后文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 91 / 12,527 / 553,115 | 0 / 0 / 0 |
| Rust | 124 / 55,620 / 2,157,670 | 149 / 64,784 / 2,496,114 |
| Swift | 62 / 11,353 / 525,288 | 62 / 11,353 / 525,288 |
| Go | 3 / 849 / 26,718 | 1 / 40 / 1,423 |
| Shell | 9 / 224 / 10,218 | 9 / 224 / 10,218 |
| C | 1 / 50 / 2,345 | 1 / 50 / 2,345 |

当前无需保留 Python 兼容包。原生运行入口、工具及客户端边界见
[仓库架构](REPOSITORY_ARCHITECTURE.md)。`clinx/mcp/registry.json` 是 Rust 编译嵌入的协议数据，
其目录不再是 Python 包。

| 删除组 | 文件 / 行 | 替代或退役依据 |
| --- | ---: | --- |
| Authority/Provider/Host/契约及对应资格 | 32 / 4,093 | 已有原生 IPC、Provider 证据和配置契约；原断言转入 Rust；workspace 子进程也不再执行内嵌 Python |
| Node/Discovery/观察及对应资格 | 28 / 4,699 | 原生配对、证书、mTLS、Node/Centre/Helper、Collector 和反向 Authority 路由；移除 writer probe 的 Python 注入分支 |
| 发布构建/CAS/relay | 9 / 1,776 | `clinx-build` 接管当前构建与离线 CAS；仅支持旧平铺布局的 forwarding builder/deployer 明确退役 |
| 安装器/Tunnel/历史工具及空包 | 19 / 1,706 | `clinx-install`、原生 supervisor 和结构检查；删除 Python callback 后端；一次性旧迁移场景保留历史证据 |
| 被动诊断 | 3 / 253 | `clinx-diagnostics` 保留精确身份、只读探测、超时及 quiet→BLOCKED 语义 |

同时移除 Python requirements、Ruff/Mypy 配置、OPAQUE 的 PyO3/wheel 构建和绑定，
以及已闭合的 embedded Bridge control Go 实验。OPAQUE 密码学实现和三个原有语义测试保留，
直接调用 Rust 接口。当前工具不要求 Python、pytest、maturin 或 Python wheel。

## 等价验证与必要边界

资格代码是验证系统契约的程序，不是额外业务权威。仍有价值的资格迁入 Rust；
只针对已退役 Python 接口、旧 cgroup/独立 Provider 服务或历史平铺发布布局的场景，
记录具体原因后退出当前源码。没有以删除断言或 skip 来生成原生 PASS。

原用例、参数和检查映射保留在：

- [Provider/Authority/Host 映射](../fixtures/provider_retirement_assertions_v1.json)：109 项检查。
- [Observer process 映射](../fixtures/observer_process_retirement_assertions_v1.json)：52 项检查及独立 SQL oracle。
- [Node/Discovery/观察映射](../fixtures/node_discovery_retirement_assertions_v1.json)：231 项检查；其中 225 项语义保留，6 项 Python 对象机制检查明确退役。另有 812 个冻结 URL 向量。
- [构建/relay 映射](../fixtures/build_relay_retirement_assertions_v1.json)：157 项原 assert/raises，包含参数和循环。
- [Tunnel/安装器/历史场景映射](../fixtures/root_retirement_assertions_v1.json)：50 项原检查、原始时钟界限及安装器独立旧输出。
- [诊断映射](../fixtures/diagnostics_retirement_assertions_v1.json)：22 项检查及原始参数。

重点覆盖精确 task/execution/thread/turn、远端 cwd/repository identity、重复请求、并发、
丢 ACK、取消、重启、精确 Provider 回读及未知结果不重放；配对/证书测试包含独立 OpenSSL、
过期证书和撤销；Collector 验证 checkpoint/ACK 与只读历史。没有修改 Authority 业务状态语义。

本轮也修复并回归了审查发现的构建边界问题：CAS 路径穿越、较晚发现的非法 mode 导致
前面文件已写，以及结构检查漏过悬空 Python symlink。不会以更宽松校验完成删除。

## 发布和维护

Rust 发布使用明确的九个运行二进制清单，仅允许继承原生 `host-worker`；不继承旧 release
的 Python、`deps/` 或开发维护工具。实际二进制 marker、hash、精确文件集合与旧文件清除
均有私有 staging 回归。状态保持 `STAGED_NOT_SELECTED`。

Monitor 只携带 `clinx-provider`、`clinx-node`、`clinx-node-daemon` 及 Swift 资源。
原生资源装配和旧目录残留清除有 Linux 回归；这不等于在 macOS 上完成 App 构建、签名或安装。

传输构建保留既有固定源和 Go SHA，两个源码路径的构建结果必须一致。Tunnel release
及 rollback capsule 使用原生 supervisor/CAS 工具，归档可复现。旧 artifact 的
`builder-source.py` 与可选 `--python` guard 仅用于核对历史文件 hash，不启动解释器。
服务模板去掉 Python 环境变量。安装器只写定义并 daemon-reload，不启动服务或导入历史数据库。

## 测试与既有失败

本轮构建、完整原生回归、产物及静态检查的精确结果在机器回执中。
所有当前测试被 Git 跟踪；新克隆无需本机 ignored Python 测试即可运行原生回归，
隔离环境和命令见 [测试指南](TESTING.md)。

| 检查 | 本轮结果 |
| --- | --- |
| Kernel 完整 Rust 回归 | 587 PASS、0 FAIL、0 ignored |
| 最后归档格式修复后的受影响 Release 回归 | 18 build + 4 relay + 6 Monitor = 28 PASS、0 ignored；包含九个真实 Release 二进制装配 |
| 提交前模板/SQL 空白规范化后的 Release 回归 | 8 installer + 11 Observer process = 19 PASS；原服务输出字节和独立 expected oracle 未改变 |
| OPAQUE 独立原有语义测试 | 3 PASS |
| Release 全部 18 个工具/运行程序及资格 examples | `cargo build --locked --release --bins --examples` PASS；二进制未链接 libpython |
| 源码/运行依赖结构检查 | PASS；Python 0，运行/App Python 源码及依赖清单均为空 |
| 实际 Tunnel 双输出目录发布包和 rollback capsule | PASS；归档逐字节相同，清单/hash/mode 一致，原生 status 与隔离 forward→rollback 还原通过；复用已有同 pin transport |
| 本轮全新固定 Go transport 双目录重建 | **BLOCKED_NETWORK**；具体失败见下文，未执行 upstream tests/build-a/build-b |
| Shell、变更格式、差异检查 | 9 个 shell 语法检查 PASS；`git diff --check` PASS |
| Clippy all-targets | 命令 PASS；11 项既有告警，新增 0 |
| 全仓库 Rust 格式检查 | 既有 7 文件差异，诊断与基准逐字相同；OPAQUE 格式检查 PASS |

完整 587 项在最终归档格式补丁前通过；后续开发构建工具补丁和模板/SQL 源文件空白规范化，
分别使用 Release 重跑 28 项、19 项受影响测试并重建所有 Release 程序，
没有重复计算为新的完整测试次数，也没有修改独立 expected oracle 或旧断言。
新增归档检查来自真实固定 upstream 归档的 PAX global comment；严格核对原始字节、
不提取元数据，保留路径/链接/设备拒绝及畸形字段回归。

固定 Go 归档下载完成且 SHA 为
`65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40`，源归档 SHA 为
`0281b17c0979f9db1ae2b41b0b66df697d646e726995756a003c6ffc51a64c10`。
PAX 修复后，第一次依赖解析在 300 秒截止；只将网络依赖解析预算改为 900 秒，
编译仍为 300 秒、Go 测试内部仍为 90 秒。最后一次运行在 `proxy.golang.org` 下载
`github.com/modelcontextprotocol/go-sdk@v1.7.0` 和 `go.opentelemetry.io/otel/sdk@v1.41.0`
时得到 `unexpected EOF`。所有失败日志和 `BUILD_FAILED` 回执均保留，transient 已自动删除。
这是本轮新构建的外部阻塞，不能称为双目录重建 PASS。

实际 Tunnel 双包验证单独完成：复用既有同源/同工具链 pin 的已资格原生 transport
`31f83c5cab00cdb17b594085706190106d174a88d49ea6e7edec8e3823183f9d`，
搭配本轮 Release supervisor 和 Rust builder。两个输出目录的 release 与 rollback 归档
分别完全相同，runtime 无 Python 或开发工具；原生 status 和包内 CAS 的私有
forward→rollback 逐字节还原均通过。这项证据不冒充本轮 Go 源码重建成功。

历史 ignored `tests/` 原样保留。对全部 **294 个原本地文件**核对 SHA，无变化。
使用冻结原 relocation harness 在私有副本中运行，完整原始 JUnit/log 均保存：

| 历史 Python 套件 | PASS | FAIL | ERROR | SKIP |
| --- | ---: | ---: | ---: | ---: |
| 同环境清理前基线 | 213 | 44 | 118 | 2 |
| 清理后原样运行 | 23 | 15 | 114 | 0 |

这两行均为 **NOT PASS**。共同可收集项的状态完全不变：23 PASS、15 既有 FAIL、99 既有
collection ERROR。新增 **15 个模块级 collection ERROR** 来自退役的 Python API/脚本；
原 240 个单独用例因此无法逐个收集（190 PASS、29 FAIL、19 setup ERROR、2 SKIP），
不能把错误数下降或用例消失当作修复。

前轮记录的 215/42/118/2 与本轮基线的差异为两个旧 writer probe 用例仍断言
`/usr/bin/python3 -I`；同基准源码使用本轮原生 binary 时实际调用
`clinx-node --native-writer-probe`。原断言保留，原因与全部 nodeid 差异写入证据。

全仓库 `cargo fmt --check` 的七个既有文件差异与基准完全一致；正常 Clippy 与严格零告警
不是同一结果，回执单列既有告警和本轮新增告警检查。

## 未验证、运行现场与恢复

以下不宣称 PASS：物理 Mac→P620 双设备业务闭环、物理 LAN mDNS、macOS/Xcode 原生
构建签名、真实管理员安装、生产迁移/服务隔离及旧 transient-systemd 资格。
私有 Node/Centre/Helper、mTLS 和进程测试不能代替这些现场结果。

本轮前后 Authority/Dispatcher/Centre 的 PID、启动时间和 ExecStart 完全一致。
已部署 Dispatcher/Centre 仍从旧不可变 release 启动 Python。这是明确的 **NOT_DEPLOYED**，
不能将源码零 Python 描述成生产环境零 Python。

忽略的旧测试、预存虚拟环境、已安装 release、数据库、凭据、回滚依赖和运行数据均保留。
它们不再进入当前源码/发布清单。任务临时仓库与可再生构建目录在保存日志、hash、原始测试
归档和 source bundle 后清理。证据根为
`/data/artifacts/clinx-python-final-20261025`；历史源码可从基准 Git 提交或其 bundle 恢复。
