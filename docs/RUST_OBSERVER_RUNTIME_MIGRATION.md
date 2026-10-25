> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Rust Observer、观察 MCP 与设备 CLI 迁移

基线：`4efdf20159ab4fb5a10412b0b3d50bc28a0ecee2`。本轮关闭剩余正常观察入口和设备 CLI
对 Python 装配的依赖，未部署。此前配对/证书、mDNS、Node/Centre/Helper、Collector 的
Rust 实现直接复用，没有重复实现业务状态机。

## 实际运行职责替代

- `clinx-mcp` 的四个观察工具使用同进程 `observation_directory`，不再启动
  `python -m clinx.monitor.observation_mcp --backend`。独立只读入口
  `clinx-mcp --observations-only --stdio` 不要求 Authority 配置。
- Directory 复用 Registry、scope、签名 cursor、paired mTLS 和原生历史契约。
  managed context 直接调用 Rust `mcp_api::Api`，精确关联 task/execution/thread/turn，
  不递归调用 MCP/Observer。TLS 只读分支不创建锁、不更新认证时间、不写身份文件。
- `clinx-observer` 独立装配既有 HTTP、鉴权、查询投影、NativeCollector、Provider
  和固定 loopback Desktop 读取。shell、Python 历史入口及 systemd 模板均指向该二进制。
  模板保留旧 socket 目录 bind，补齐读取已有 Unix socket 所需的 AF_UNIX。
- `bin/clinx` 的正常 devices/pair/serve 直接执行 `clinx-device`。配置、选择、TTY/PIN、
  重连节流、Observer 私有配置读取及 URL codec 由 Rust 实现；GUI enrollment 返回值
  同样通过原有端点/token policy。807 个 URL 与原 urllib codec 对照一致。
- 本轮修复了读取期间身份变化的实际缺口：重新校验本地 identity 和 Registry/peer
  fingerprint。两个新增测试在修复前稳定 FAIL，修复后 PASS；不返回旧身份下读取的内容。

原有 Task Authority、并行执行、投递、取消、ACK 不确定、恢复和 metadata outbox 契约未变。
未增加执行流程、审批或新的权限主体。非法 CLI 参数的 usage 文本不承诺与 argparse
逐字一致；非法 TOML 类型仍拒绝，部分错误名称由旧 Python 异常变为 DEVICE_ERROR。

## 发布边界

Rust release 声明的 CLINX Python 源码由 **51 文件 / 8,123 行 / 324,590 字节**
收紧为 **12 文件 / 1,671 行 / 67,535 字节**，移出 39 个模块。
实际维护闭包的外部导入全部为 Python 标准库，无第三方 Python 依赖。

保留原因明确：`install_rust_services.py` 仍调用 `desktop_socket.py`，验证既有 Desktop
socket 路径、owner/peer UID、witness thread，按原规则发布固定 alias。
其共享依赖为 `provider/{app_server,native_provider}`、`nodes/network_worker`、
`authority/{request_pipe,rust_authority}`、`contracts/execution_semantics` 及包初始化文件。
这是安装维护依赖，不是正常 Authority、Provider、Observer 或 Node 的执行核心。

只读检查现役旧 manifest 后，确认其 416 个 deps 文件均属于已不需要的 Python 依赖、
wheel 和安装元数据。新继承规则按 `native-runtime.json` 的明确名单排除这些文件；
对该旧清单执行纯函数验证只保留 `host-worker`。未删除或改变已安装 release。
未知外部 native 依赖仍保守保留，现役旧包中没有剩余的此类 deps。

stager 新要求 `clinx-observer`、`clinx-device`，实际探测三项 marker：
`RUST_OBSERVER_V1`、`RUST_DEVICE_CLI_V1`、`RUST_OBSERVATION_DIRECTORY_V1`。
当前 Mac App 继续使用 Swift 和三个 Rust Helper，不包含 Python 源码/解释器/wheel。
旧 monitor Python profile 仅用于公开 API 和旧包布局资格，正常 App builder 不调用它。

## 删除量与全仓规模

**没有整文件删除。** `clinx/` 五个入口/实现文件删除旧代码 347 行、增加兼容入口 75 行，
该组净减少 272 行。DEV_ONLY SPAKE2 CLI 仍有原资格用途，其 212 行移入资格工具；
这部分没有被算作删除。新增回归同样计入全仓统计。

固定六语言、包含测试/资格/工具的 `CLINX_SOURCE_SIZE_V1` 结果：

| 语言 | 之前 文件 / 行 / 字节 | 之后 文件 / 行 / 字节 |
| --- | ---: | ---: |
| Python | 116 / 20,502 / 873,194 | 120 / 21,216 / 911,158 |
| Rust | 80 / 39,150 / 1,546,403 | 86 / 40,939 / 1,613,226 |
| Swift | 62 / 11,353 / 525,288 | 62 / 11,353 / 525,288 |

Python 净增 **714 行 / 37,964 字节**；占比 **29.2674% → 29.4956%**。
`<5%` gate 仍返回 2，**未达标，本轮未取得 Python 总量下降**。
没有调整 Linguist、排除测试或用目录搬迁声称完成目标；本轮交付的是运行依赖退役。

## 验证

| 范围 | 结果 |
| --- | --- |
| Linux Rust Release | 14 binaries PASS；无 libpython 链接 |
| Rust tests | 120 PASS |
| 全部 tracked fixtures | 431 PASS |
| 原 URL codec oracle | 807 个端点一致 |
| 原 ignored 本地测试 | 前后均 294 PASS / 32 FAIL / 104 ERROR / 2 SKIP；432 个身份结果逐项相同 |
| 原测试保护 | 129 个本地文件哈希相同；受影响 tracked fixture 原 69 个 assert AST 全保留，新增 6 个产物断言 |
| 原型/产物验证 | 独立进程只读 MCP/Observer、mTLS、Provider、scope/身份替换、并发/checkpoint/重启、真实 launcher、增量 App 资源清理 PASS |
| 分类/import/profile、Python lint、shell、Rust format、diff | PASS |
| Strict Clippy | BLOCKED_EXISTING；同一 10 条既有 library 诊断；非 strict 检查另有既有 workspace_routes 告警 |
| 原生 macOS build/signing、物理 Mac↔P620、新产物真实 LAN 多播 | 未验证；Linux 无 macOS toolchain，私有 loopback fixture 不替代实机 |
| 部署 | NOT_DEPLOYED；Authority/Dispatcher/Centre PID、启动时间和 ExecStart 前后相同 |

首轮新增 Directory fixture 使用了错误的 activity 字段 events；实际固定协议为 items，
修正该新测试后 PASS。身份替换两项真实失败另有前后证据；没有修改既有断言制造 PASS。

## 剩余工作

1. 迁移现役资格 runner/fixture 到 Rust，保留用例和失效条件映射，再删除对应 Python。
   本轮新增的资格代码也在这项工作范围内，不能再次排除到比例统计之外。
2. 迁移构建、发布、安装及维护工具，优先实现 Desktop socket 维护契约，才能去掉上述
   12 文件发布闭包。必须保留 inode/peer/witness 校验与原 alias 行为。
3. 收口公开 DTO/API 和显式 IO 注入接口。Observer/Directory 的 Python facade 已无正常
   运行职责，但仍被原测试和公开接口引用；在等价资格接管前保留并标明 COMPAT。
4. 在具有 macOS 工具链与隔离设备条件时完成原生 App、签名及物理链路验收；不得为此
   擅自部署、重启生产或操作真实任务。

机器证据见 [回执](cleanup/RUST_OBSERVER_RUNTIME_MIGRATION_RECEIPT.json)。本机日志与源码
恢复包保存在 `/data/artifacts/clinx-observer-runtime-20261025/`；临时源码和可再生构建输出
在任务结束后回收。历史失败和未验证范围不会标为 PASS。
