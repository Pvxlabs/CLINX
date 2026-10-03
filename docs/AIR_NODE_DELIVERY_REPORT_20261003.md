# Air＋P620 CLINX 收口报告（2026-10-03）

源码候选已集成 P620 `codex/fix-native-terminal-state-20261003` 的完整提交链（merge-base `474c52f`，分支最终 `3d99f2b`），并加入 Air 制品运行时与生命周期修复。最终 Air 提交为 `14e13c0c17aa92ee8dd3ffffeacd2540973a68cb`，分支 `codex/air-node-delivery-20261003`，已推送 `origin/codex/air-node-delivery-20261003`；工作区干净。证据：`.validation/air-node-delivery/`。

- `MONITOR_SOURCE_INTEGRATION=PASS`：终态 precedence、execution identity、历史详情不串、Activity 尾部错误恢复／第四次结果、generation 隔离、滚动边沿抑制、native display state 均已合入；没有整文件覆盖节点路由。
- `SWIFT_BUILD=PASS`：`cd MonitorApp && swift test --scratch-path /private/tmp/clinx-air-final-swift-20261003` 通过构建；`Scripts/build-app.sh` 通过，生成带 Swift App、DiscoveryRuntime、opaque wheel、helper、node protocol 的签名 bundle。
- `SWIFT_TESTS=PASS`：72 tests、0 failures。受影响 Activity/MonitorStore 16 tests 先行通过。
- `PYTHON_REGRESSION=PARTIAL`：隔离环境 `.venv` 安装项目声明依赖及 `jsonschema` 后，节点／native／Monitor 目标回归 59 passed；完整套件为 935 passed、60 failed、9 skipped、118 subtests。失败集中在内核／Rust 实际 binary、生产 PAKE／LAN、共享 Host/SSH/systemd、tunnel 子进程等需要额外运行时或平台前置的套件；没有用 skip、删断言或修改共享服务收口。完整日志为 `full-pytest-2.log`。
- `TERMINAL_STATUS_GUI_ACCEPTANCE=PASS`：实际安装 App 的 P620 只读 Observer 行显示 `BLOCKED`，精确 execution `exec_02a865bc72c54401a08ee90dba7aa886`，没有把旧 running 快照显示为运行中；真实 Inspector 显示来源、Host、Provider、阶段和结果。
- `FINAL_ACTIVITY_TAIL_ACCEPTANCE=PASS`：实际 Activity 显示 40 events 和 `BLOCKED` 终态结果；向上滚动显示 `Jump to latest`，向下回到底部；旧 execution 的历史正文和当前 execution 没有混入。
- `SCROLL_RUNTIME_ACCEPTANCE=PASS（本机未复现卡死）`：匹配最终安装 bundle 上完成 Activity 打开、上下滚动、尾部停留；5 秒 `sample` 和 `ps` 证据已保存。样本 PID 2201，采样时约 2.2% CPU、157904 KB RSS。采样栈主要为 SwiftUI layout/accessibility graph，未发现持续高 CPU 的 Activity/scroll 自旋。
- `SCROLL_HANG_ROOT_CAUSE=未确认`：源码已消除 scroll 通知／programmatic scroll 反馈放大器并有回归；现场最终热点未在本机重现，不能把一次正常采样等同原现场根因已证实。
- `AIR_INSTALLED_BUILD_IDENTITY=PASS`：`/Applications/CLINX Monitor.app`，`CFBundleShortVersionString=0.3.0`，`CFBundleVersion=3`，开发证书 `CLINX Monitor Local Development`，`codesign --verify --strict=PASS`；安装二进制 SHA-256 见 `AIR_READBACK_20261003.txt`。旧 App 已保留在 `.validation/air-node-delivery/previous-installed-20261003.app` 与 `installed-before-final-20261003.app`。
- `AIR_P620_NODE_ACCEPTANCE=BLOCKED`：Air helper 已从实际安装 bundle 启动，复用 `~/Library/Application Support/CLINX Monitor/Devices` 身份／配对目录；无可信中心 peer 时健康状态为 `blocked / PAIRING_REQUIRED`，不会启动未认证 listener。P620 当前 MCP/service 仍加载 `execution-authority-474c52faa670-monitor-closure`，仅有旧本地中心路由，未提供可用于 Air NodeRPC 注册的正式远端维护入口；不能声称真实 mTLS 节点注册、Air 样本跨机读取或授权任务在 Air 执行。
- `P620_SHARED_RUNTIME=BLOCKED`：P620 开发 checkout 是 `f88ffe0` 且干净，但运行 release 与节点协议候选不一致；本轮没有停止 Provider/MCP、没有改 shared runtime、没有重启服务，也没有借用其他权限。
- `IMAC_AND_THREE_MACHINE=未验证`：本轮未取得 iMac 可达且已授权的安装／配对／维护渠道；Air＋P620 也因中心注册入口缺失而未形成两机 NodeRPC 闭环，不能涂绿三机目标。
- `MERGE_AND_PUSH=PASS（分支推送）/NOT MERGED`：分支已正常推送；未创建或合并 PR，`main` 未改变。
- `SIGNING_AND_NOTARIZATION=开发签名 PASS；公证未执行`：没有伪称 Developer ID 或公证，公开分发仍需独立签名／公证流程。
- `FINAL_STATUS=PARTIAL`：Monitor 原生交付与本机 UI／性能验收通过；Air helper 安装和配对前生命周期通过；P620 新中心激活、真实 Air↔P620 节点注册／读取／执行、iMac 和三机验收仍阻塞在正式独立维护入口、中心运行制品和可信配对共享范围。

原始交接报告中的旧 `PASS/BLOCKED` 未被回填；本报告只追加本机新证据。关联 Linear：PVX-1886、PVX-1887。
