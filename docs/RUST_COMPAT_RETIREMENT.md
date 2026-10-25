# Python 兼容实现与维护工具退役

基线 `29f72a6fec2022792bcc2e635b5e34ab60171d7e`。本轮删除最后的 Python MCP factory、
旧配对实验及其无消费者依赖，把实际构建和维护入口接到 Rust。**没有部署或重启服务。**
Python 由 22.2285% 降至 **19.8206%**；全仓严格低于 5% 仍为 **NOT_MET**。
固定 `CLINX_SOURCE_SIZE_V1` 六语言源码字节口径，测试、构建和维护代码均纳入，未改 Linguist。

## 保留能力与删除实现

兼容能力按真实消费者保留；曾经存在的 Python 接口不是永久保留依据。资格代码是证明
身份、配对、并发、丢 ACK、重启及产物边界正确的测试，不要求 Python。有效断言继续迁到
Rust；已经退役协议的专属实现可以删除，独立 OpenSSL 互操作和仍在使用的 Python 对象接口测试保留。
开发、构建、维护工具的职责仍需要；当前并非已全部 Rust 化。

| 实际调用链 | 本轮结果 |
| --- | --- |
| operator policy → Python MCP factory → Authority | `clinx-mcp --policy-maintenance --config <json> --stdio`；删除 Python server、policy 入口、observation facade 和 workspace helper |
| Monitor build → Python 资源清理/Cargo JSON | Rust 开发工具 `clinx-build`；真实 `build-app.sh` 无 Python 调用，工具不进入 App/运行 release |
| 两个安装器 → Python Host/MCP 配置编译 | 已校验 release 内 `clinx-node --compile-config host\|mcp <toml>`；删除两个 Python 编译器 |
| systemd history guard → Python inode 检查 | `bin/clinx-observer --history-mount-guard`；保持六个固定文件、两次源快照、两次活动 PID 检查和 Observer-only try-restart；仅更新源码模板 |
| DEV_ONLY CLI → SPAKE2/HKDF → Python TLS server/client | 删除整条实验实现及 spake2 依赖；现役 OPAQUE、降级拒绝和独立 OpenSSL oracle 保留 |
| 无消费者的 observation/Provider/GUI facade | 删除 `rust_monitor.py`、`native_associations.py`、`provider/liveness.py`、`monitor_helper.py` 共 229 行；tracked、原本地测试、动态文件/符号和服务引用均已核对 |

策略维护保留四个已有工具、精确参数、明确批准与 review hash/task 绑定；所有写入仍由原
Rust Authority CAS 执行。旧 Python 声明四工具但实际只暴露 prepare/apply，本轮修复为四个，
补回两个只读审阅工具；没有增加权限或执行/恢复服务。新维护 CLI 读取已编译 JSON，旧 TOML 入口退役。

Host/MCP 有效配置的 12 次原 Python / 新 Rust 输出比较通过。MCP 静态编译不新增 runtime
self route；既有 API 模式仍保留它。原编译器生成的 NaN/Infinity 本来无法被 Rust runtime
JSON 解析，新命令在输出前返回 `NONFINITE_RUNTIME_CONFIG`。发布器实际探测
`--config-compile-contract`，写入 `config_compiler=RUST_CONFIG_COMPILER_V1`；安装器在任何
文件/权限/进程修改前拒绝缺少契约的旧 release，避免新安装器误用旧 Node。

## 回归与历史失败

[逐 case 迁移与退役映射](../architecture/compat-retirement.json) 保存基线、原源码 SHA、
替代用例和独立回执摘要。原 24 个 Node contract case 映射为 9 组 Rust 测试，原参数不变。
Observer guard 的 5 个本地原测试语义迁入 Rust，另加 PID/命令超时验证；测试只调用私有替身命令。

| 验证范围 | 结果 |
| --- | --- |
| Linux Release | **PASS**：15 binaries，包含仅开发使用的 `clinx-build`；无 libpython 链接 |
| Rust 全套 | **429 PASS**，0 failed/ignored；先构建 examples |
| tracked Python fixtures | 基线 179 PASS → **160 PASS**；减少来自 24 case 的完整迁移，增加 5 case；后续四个无消费者文件删除后，受影响 17 case 再次 PASS |
| 完整 ignored 旧本地套件 | 基线 **294 PASS / 32 FAIL / 104 ERROR / 2 SKIP** → **222 PASS / 35 FAIL / 118 ERROR / 2 SKIP**；两次最终运行身份/结果一致，全部差异记录 |
| 发布/App payload | **PASS**：原生源码/依赖清单仍为零 Python，真实 Rust 资源清理、旧文件/软链/只读目录处理和 Helper 注册/重启资格通过；release 继承门禁 fixture 使用合成 host/protocol 探针，配置 marker 单独由真实 binary 验证 |
| 源码分类/import、shell 语法、Python E9/F、diff | **PASS**；根目录 Python 为 0；所有 DEAD 源码不存在 |
| 全仓 rustfmt | **BLOCKED_EXISTING**：同 7 个既有文件差异；消除本轮新增差异后，仅路径/行号偏移，内容与基线相同 |
| Clippy | 普通检查通过；strict warning-free 门禁 **BLOCKED_EXISTING**：10 条既有 Core + 1 条既有 workspace_routes 诊断；本轮无新增 |
| Mac SDK/Swift App 原生编译、签名、新产物物理 Mac→P620 闭环 | **未验证**：本轮 Linux 测试不能替代实机验收 |
| 工作区与生产保护 | 原本地 294 个文件 SHA 未变；Authority/Dispatcher/Centre PID、启动时间及 ExecStart 完全相同；未执行 installer 或实际 history guard |

完整旧套件的新增负向结果没有隐去：

- 旧 SPAKE2 的 14 个原 PASS 变为 5 FAIL / 9 setup ERROR，另 1 个原 FAIL 变为 setup ERROR；
  两轮均在同一已装 SPAKE2 的环境执行，没有靠卸载依赖变成 SKIP。现役 OPAQUE 组结果不变。
- 4 个退役 Python API 的 collection ERROR 使原 59 case 无法收集，其中 57 原 PASS、2 原 FAIL。
  原文件字节不变。guard 5 case 和仍有效 MCP/Node 请求均已有 Rust 替代；两个原失败的有效语义
  也完成真实 Authority/Transport/Provider 资格，原 Python 失败不因此被改记 PASS。
- 9 个旧“从自然语言 prompt 自动提取路径并 Git seal/reject”断言明确为 **RETIRED_CONTRACT**。
  基线中此函数已无 tracked 调用者，现役 Rust 直接使用显式 workspace_contract；本轮
  `mcp_api.rs` 与 `registry.json` 字节未变。另 9 个“类别文本不能取得路由权威”不变量继续验证。
- 旧 `test_tunnel_child` 硬编码已删除 Python server 路径，新增 1 FAIL；Rust CLI 保留缺配置时
  stdout 无协议数据、stderr BLOCKED 的原不变量。没有修改 ignored tests、删除断言或增加 skip。

## 规模与具体剩余依赖

| 语言 | 之前 文件 / 行 / 字节 | 之后 文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 117 / 16,892 / 725,340 | 104 / 15,172 / 651,861 |
| Rust | 109 / 50,821 / 1,973,020 | 117 / 53,448 / 2,072,382 |
| Swift | 62 / 11,353 / 525,288 | 62 / 11,353 / 525,288 |

完整删除 **13 个 Python 文件、1,452 行**；包含实现缩减和新增回归后，Python **净减
1,720 行、73,479 字节**。COMPAT 从 8,525 降至 **7,391 行**，DEVOPS 从 2,230 降至
**1,958 行**，QUALIFICATION 从 6,137 降至 **5,823 行**。没有仅靠增加 Rust 测试行数宣称退役。

剩余代码不能一概称为薄兼容或不可删除：

- `scripts/transfer_rust_native_session.py` 嵌入的 `existing_client`、`exact_turn`、
  `BridgeConfig`、`RustIntegration` 仍消费 Provider/Authority/观察回调。下一步应将该精确维护
  流程改用原生契约，再消除它牵引的对象链；不能直接删 adoption/read 能力。
- `scripts/local_discovery_smoke.py` 及 Node/Provider/Observer fixtures 仍消费 Python DTO/回调。
  Monitor+Node+Provider 共 19 文件、4,201 行是下一块可追踪闭包；它不是第二套任务状态权威，
  也不能只改名为 adapter 就算迁移完成。
- `node_service_entrypoint.py` 仍被本地测试通过 `spec_from_file_location` 和历史 App 路径调用；
  需迁移这些有效装配断言后才能删除。独立 Python/OpenSSL oracle 保留用于不同 TLS 实现互操作，
  可以日后换成其他独立工具，资格验证本身不要求 Python。
- `stage_rust_release.py`、安装/CAS/健康检查及清单工具仍是 Python。历史 forwarding/tunnel
  构建和回退工具必须用精确旧源码，不能从仓库中删除后假称已安装旧 release 失去依赖。

新 Rust release 与当前 Monitor payload 不包含上述 Python。安装中的 Dispatcher/Centre
仍是历史 Python releases；本轮没有切换它们，源码收敛不等于生产升级。

完整回执：[RUST_COMPAT_RETIREMENT_RECEIPT.json](cleanup/RUST_COMPAT_RETIREMENT_RECEIPT.json)。
本机证据 `/data/artifacts/clinx-compat-retirement-20261025/` 保留原测试归档、前后源码 bundle、
逐 case 差异、消费者图、日志和提交/远端回执；任务结束回收临时 checkout、venv 与可再生构建输出。
