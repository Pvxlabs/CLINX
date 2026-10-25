# 原生构建与维护入口

用途：原生开发与运维命令索引。适用范围：当前源码；历史验收段落按原日期理解。
状态：CURRENT。验证：[测试指南](../docs/TESTING.md)与下述实际入口；不声明当前已部署版本。

当前没有 Python 脚本。下列命令来自同源码构建的 Rust 二进制；下表构建/维护工具参数可用对应 `--help`
读取；运行程序以各自解析器和 contract probe 为准。运行时、工具和发布边界见 [仓库架构](../docs/REPOSITORY_ARCHITECTURE.md)。
固定 Transport 依赖的 EOF 处理和实际双构建证据见
[Go Transport 重建](../docs/GO_TRANSPORT_REBUILD.md)：仅模块获取有限重试，完整性错误不重试，
获取后验证缓存，测试/编译使用 `GOPROXY=off`，各次失败日志保留。

| 命令 | 用途与边界 |
| --- | --- |
| `clinx-build stage-rust-release` | 接受明确 Authority/relay/Transport/Provider/MCP/Node/daemon/Observer/device binary，校验 marker/hash，生成 STAGED_NOT_SELECTED 候选；仅继承允许的 host-worker |
| `clinx-build native-monitor-resources <source> <resources>` | 清理任务构建目录的旧 Python closure，生成零 Python 的 App 资源，不触及已安装 App |
| `clinx-build build-transport-runtime --source-archive PATH --go-archive PATH --output NEW_DIR` | 固定 SHA 源/Go 归档，拒绝 embedded executor 依赖，两个路径构建可复现；不安装 |
| `clinx-build build-tunnel-release --output DIR --guard-manifest JSON --evidence-manifest JSON` | 原生 supervisor、确定性产物和 rollback capsule；其他二进制/输入按 `--help` 显式传入 |
| `clinx-build tunnel-cas check\|fixture-apply --capsule PATH --root PATH --direction forward\|rollback` | 原有精确三文件 CAS；写入仅接受任务隔离 fixture marker，所有目标/模式先校验再写 |
| `clinx-install provision` | 建立当前专用 Authority/API/Transport 身份与私有目录；不导入旧数据，不撤销历史进程 |
| `clinx-install services --release PATH --operator-home PATH --bridge-config PATH --desktop-socket PATH --desktop-thread-id ID` | 原生安装器；验证不可变 release，使用原生 desktop socket 维护与配置编译，写服务定义并 daemon-reload；不 start/restart |
| `clinx-install callers --release PATH --legacy-node-state PATH --output PATH [--operator USER]` | 复制原 identity/peers/sharing，保持 pre-cutover loopback Centre 18774；不导入旧数据库、不切换 Tunnel |
| `clinx-maintenance transfer-native-session ...` | 显式会话维护，需指定 destination-home；精确 Provider 证据与 Authority adoption，发送前 durable receipt，未知 ACK 不重放 |
| `clinx-diagnostics tunnel-health --url-file PATH [--max-age 120]` | 精确 systemd PID 与已确认 loopback listener 的新鲜 poll，只读 |
| `clinx-diagnostics embedded-bridge --base-url URL --tunnel-pid PID --bridge-pid PID` | 固定旧 binary/进程身份的被动观察；绝不 Warmup/RPC/recycle，quiet 仍返回 BLOCKED |
| `clinx-source-audit [--revision COMMIT]` | 固定全源码统计口径；目标低于 5%，当前 Python 源码为零 |
| `clinx-source-audit --check-project` | 组合结构、文档链接/锚点/索引、源码角色、直接依赖和 Cargo/发布分类门禁 |
| `clinx-source-audit --check-artifact PATH` | 只读验证实际 Rust stage release 的完整成员、hash、native 格式及 Python 污染 |
| `clinx-source-audit --check-structure` | 验证 DEAD 源码不存在、无新增 Python、无运行依赖及 schema 数据完整 |
| `cargo run --locked --manifest-path kernel/Cargo.toml --example local_discovery_smoke -- --loopback --node-binary PATH` | 两 Node 私有配对/重启/mTLS；`--lan` 为另行显式物理 LAN 资格 |

启动器为 [CLI](../bin/clinx)、[MCP relay](../bin/clinx-context-mcp)、
[Centre](../bin/clinx-node-centre)、[Observer](../bin/clinx-observer)，均启动原生程序。
Host/MCP 配置编译使用 `clinx-node --compile-config host|mcp <toml>`；服务执行已编译配置。

旧 forwarding builder/deployer 只接受历史平铺产物，在基准源码中已明确拒绝当前布局，现已删除。
旧 Python→Rust 一次性撤权/cgroup/独立 Provider-unit 资格与 embedded Bridge control 实验
也已退出当前源码；历史恢复使用对应 Git 提交和原回执，不将旧路径/身份/数据库假设应用到新服务。
原本地 ignored 测试保持字节不变，历史失败不靠修改断言或 skip 消除。

管理员工具的存在不表示本次执行过安装、维护或部署。所有新工具资格使用私有路径、进程和
命令替身。本轮生产服务、数据库、真实任务和运行数据保持不变。
