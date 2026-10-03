# CLINX Air＋P620 多设备节点实现与验收报告（2026-10-04）

本报告追加本轮实现事实，不改写历史报告中的 PASS/BLOCKED。范围仅为 CLINX；未操作 ORION。交付分支为 `codex/air-node-delivery-20261003`，最终 HEAD 为 `1cc61f80e914a50477ac3ed63bfad40b976e1aa4`，已推送到 origin，未声称已合并。

## 本轮新增产品实现

- `node_protocol.py)：`NodeAuthorizationStore` 的持久化可撤销 scope；`NodeRegistry` 的注册、心跳、stale/offline/revoked 状态；`NodeService` 的只读会话与 canonical prepare/start/status/cancel 回调；请求幂等 ledger 和 execution fence；`NodeRouter` 的精确远端路由、部分节点离线 coverage 与 cursor 隔离；`NodeRPCServer` 的按连接 mTLS context。
- `node_runtime.py)：基于现有 `NodeIdentity`、`TrustedPeerStore`、OPAQUE 和 mTLS 的 `CentreService`、`NodeRegistrationClient`、`AuthorizedRemoteClient`，并校验认证证书与已配对 node_id、明确 user scope 和双边授权。中心使用 TCP 观测地址，不接受客户端自报回连地址。
- `MonitorApp/Scripts/node_service_entrypoint.py)：从可信 `node-config.json` 读取中心，未配对/未授权时不启动可用节点服务；以 mTLS 注册、心跳、重连，scope 变化即时收敛，原生读取只走 `NativeHistory`，不创建 Task/Execution、发送 turn、resume、cancel 或 lease。
- `node_execution_adapter.py)：将节点执行回调委托给既有 `ClinxIntegration`、TaskRegistry、policy、Provider adapter、request ledger/fence；配置不合法或未批准时明确不可用，不把注册成功等同于可执行。
- `node_centre_entrypoint.py`、`bin/clinx-node-centre`、`systemd/clinx-node-centre.service`：中心启动、bootstrap、配对、显式 share/revoke、节点配置和用户级 service 入口。
- `mcp_server.py)、`bin/clinx-context-mcp`：使用同一节点 registry 和真实远端 factory；启用后 MCP 的精确 host/node 路由进入远端 reader。
- `local_discovery/transport.py`：仅在显式 pairing/centre endpoint 流程中允许直接可达的 Tailscale 地址，默认 LAN discovery 约束不变。
- `MonitorApp/Scripts/build-app.sh` 与 `CLINXNodeService`：将 canonical adapter 依赖打入 bundle，并设置 `PYTHONDONTWRITEBYTECODE=1)，避免 helper 运行时修改已签名 App。

## CENTRE_IMPLEMENTATION

**PASS。** P620 使用同一最终源码 SHA 的 release 目录：

`/home/pvxlabs/.local/lib/clinx-control/releases/air-node-delivery-13e82d3`

中心由 `node_centre_entrypoint.py serve` 启动，使用既有 `tasks.sqlite3.nodes.sqlite3` registry；已切换为用户级 `clinx-node-centre.service`，实际状态为 active，mTLS 监听端口为 18773。中心只负责身份、授权、节点状态和路由索引，原生历史仍归 Air 所有。

## AIR_NODE_IMPLEMENTATION

**PASS。** Air 身份为 `air.local`，中心为 `p620`；Air bundle 中的 LaunchAgent helper 实际运行并持续注册，health 为：

- node_id：`air.local`
- centre_id：`p620`
- capabilities：`session.read`, `session.status`
- status：`running`

P620 registry 中 Air 记录为 ONLINE，endpoint 为中心根据连接观测到的 `tls://100.126.61.35:18772`。Air 没有使用随机端口或 loopback 默认作为生产注册地址。

## SHARING_AUTHORIZATION

**只读 PASS；执行未批准/未验证。** 配对使用现有 OPAQUE_V1/mTLS 身份。明确批准的映射为：

- Air `air.local` → P620 `p620`：user scope `tinzleung`，`read_sessions=true)，`execute_tasks=false)。
- P620 端保存 Air 的同一明确 `tinzleung` scope；没有把 OS 用户 `pvxlabs` 改名成 `tinzleung`。

旧连接每次请求重新检查 paired identity、registry scope 和 durable approval；撤销后不继续使用缓存授权。执行测试需要单独批准，因此本轮没有产生执行测试副作用。

## ISOLATED_INTEGRATION

**PASS。** `.venv/bin/python -m pytest -q test_node_protocol.py test_node_runtime.py --disable-warnings`：8 passed，6.33s。覆盖真实 OPAQUE 配对、独立中心/节点进程、mTLS 注册、心跳、远端只读、stale/offline、撤销、cursor 隔离、幂等 start 和读取无执行副作用。

本轮隔离证据保存在被忽略的 `.validation/air-node-delivery/` 下，包括：

- `p620-air-native-read-20261004.json`
- `p620-node-registry-20261004.txt`
- `air-node-health-final-20261004.json`
- `node-regression-final-20261004.log`

受影响的更宽 Python 回归历史结果为 114 passed、3 failed、2 skipped；3 个失败分别是 BrokenPipeError 类型差异、Air 本机不支持的 127.0.0.2 bind、缺少 discovery 依赖的既有环境用例，未由本轮读取路径引入。完整旧基线 39 分类失败也未被重新包装为本轮代码阻塞。

## P620_ACTIVATION

**PASS（本轮候选组件）。** 使用独立 SSH 账号 `pvxlabs@p620`，未修改 P620 主 worktree，保留原 `launch-mcp`、service 配置和状态目录备份。只重启了 `clinx-tunnel.service`，没有取消已有任务；中心独立运行在 `clinx-node-centre.service`。最终 P620 MCP child 实际命令来自 `air-node-delivery-13e82d3`，环境显式设置 `CLINX_ENABLE_NODE_ROUTER=1`、`CLINX_NODE_ID=p620`、`CLINX_USER_SCOPE=tinzleung`。

这证明的是本轮节点候选的激活；不把 Git push 写成 merge，也不把候选激活写成主线合并。

## AIR_NATIVE_READ

**PASS。** 使用指定样本 `01a10178-c993-7960-9fb5-14c00f221d05)，通过 P620 实际 MCP stdio、同一 P620 node router、mTLS 到 Air helper、Air NativeHistory 读回：

- `source_node_id=air.local)
- `host=air.local)
- `lookup_status=THREAD_UNBOUND)
- `provider_existence=CONFIRMED)
- `context_status=AVAILABLE)
- `coverage.complete=true)，`nodes_queried=[air.local])
- `native_status.state=COMPLETED)
- `read_only=true)

这不是 P620 本地旧索引的结果；没有 adoption、resume、turn、cancel 或 execution lease。Codex app connector 使用 `host=air.local` 的精确 selector 也返回同一 Air 来源和完整 coverage。未带精确 host 的多节点查询按协议返回部分 coverage，并列出 P620 unavailable，未静默合成全局不存在。

## AIR_DIRECTED_EXECUTION

**实现 PASS；真实执行未验证/当前阻断于批准。** canonical adapter、节点 execution callbacks、policy、幂等 ledger、单写者 fence 和未知结果对账路径已经实现并打包；Air/P620 双方当前 durable scope 的 `execute_tasks=false)，因此没有启动专用测试任务，也没有取消任何已有 execution。下一步必须使用新的专用测试任务和独立可取消 execution，且只在明确批准该执行范围后进行。不能把只读交付写成执行交付。

## MONITOR_REGRESSION

**PASS。** 最终 bundle 构建入口为 `MonitorApp/Scripts/build-app.sh)。最终重新运行：

`cd MonitorApp && swift test --scratch-path /private/tmp/clinx-air-node-swift-20261004-final`

结果：72 tests，0 failures。ActivityTests、MonitorTests、DeviceConnectionTests、FinalizedUIRuleTests、TimelineArchiveTests 等全部通过。最终 App 已从真实 `.app` bundle 启动，LaunchAgent helper 也从该 bundle 运行。

## TERMINAL_STATUS_GUI_ACCEPTANCE

**PASS（已有 Monitor UI 交付基线；本轮 helper-only 变更未改 Swift UI）。** 既有 Monitor 终态、Inspector 精确身份、最终消息、Activity 分页/历史 execution 交付证据保留；本轮重新启动的 App 当前显示 read-only observer surface，执行数为 0。当前 UI surface 的 P620 Observer 连接显示 Offline，这个显示属于既有 Observer 连接，不覆盖本轮已经通过的 mTLS 节点 registry/远端 MCP 读回，也不被写成新的跨机 UI PASS。

## FINAL_ACTIVITY_TAIL_ACCEPTANCE

**PASS（Swift 回归）。** Activity 终态尾部、延迟 result、错误后恢复、关闭详情后的晚到响应隔离和分页 cursor 行为由最终 72 项 Swift 回归覆盖；本轮没有修改 Swift Activity 实现。

## SCROLL_RUNTIME_ACCEPTANCE

**PASS（历史实机证据沿用；本轮未改 Swift 滚动实现）。** 既有匹配 Monitor 候选已完成长 Activity 上下滚动、底部 live tail、分页、任务切换、文本选择和终态操作；历史采样记录约 2.2% CPU、RSS 157904 KB，未见持续高 CPU。当前追加的 helper/bundle 变化不改变 Swift Activity 代码，因此没有重复猜测或关闭必要更新。

## SCROLL_HANG_ROOT_CAUSE

**未确认。** 本机未复现原现场卡死根因；已保留“本机未复现，原现场根因仍未确认”的边界。确定性修复仍为滚动观察只发布 bottom-state transition、避免旧分页覆盖 live cursor、Activity 请求代次隔离和终态尾部读取。没有把当前不再卡顿写成原现场根因已证实。

## AIR_INSTALLED_BUILD_IDENTITY

**PASS。** 当前实际安装并运行：

- 路径：`/Applications/CLINX Monitor.app`
- Bundle ID：`com.pvxlabs.clinx.monitor`
- 版本：`0.3.0)，build `3)
- 架构：arm64
- 当前二进制 SHA256：`1fd037cf96f710d9a650afba3378e07dff17ea7129f32afef863bca09ccf3ff9`
- `codesign --verify --deep --strict)：PASS
- 当前 Monitor PID：13103；node helper PID：13110
- 运行健康：`status=running)，`centre_id=p620)，`node_id=air.local)

安装前的旧 bundle 已移到 `.validation/air-node-delivery/` 回退目录，没有清空历史、身份、配对或任务状态。

## AIR_P620_NODE_ACCEPTANCE

**PASS（真实跨进程、跨机只读路径）。** Air helper、P620 centre、P620 MCP child 三个独立进程已运行；P620 MCP 经过 mTLS 路由读取 Air 原生样本成功。中心 registry、Air health、实际进程命令和 MCP 返回均已保存。Monitor UI 的旧 Observer surface Offline 单独记录，不能覆盖节点协议验收。

## PYTHON_REGRESSION

**PASS（受影响范围）。** Node protocol/runtime 8 项全通过；py_compile、git diff --check 通过。App bundle 的 canonical execution 依赖已自包含；本轮没有改系统或共享 Python 环境。

## MERGE_AND_PUSH

**PUSH=PASS；MERGE=NOT_RUN。** 最终分支已正常推送：

`13e82d3` → `0aad5aa` → `1cc61f8)

远端 branch 与 Air 当前 HEAD 一致；没有强推、reset 或覆盖其他分支。Linear PVX-1886/PVX-1887 的历史评论保留；本轮追加评论调用因 Linear connector 返回 `UNAUTHORIZED / requires reauthentication` 未写入，未伪称已更新。

## FINAL_STATUS

**源码实现与真实只读交付 PASS；Monitor 制品和 P620/Air 节点运行 PASS；执行交付未完成。**

已完成的真实产品路径是“配对身份 → 双边明确只读授权 → 中心注册/心跳 → mTLS 远端 reader → Air 原生会话读回”。未完成项只有两类：

1. **执行验收**：代码已实现，但当前 scope 明确为 `execute_tasks=false)，没有执行批准，因此没有创建或取消专用测试 execution。
2. **Linear 追加评论**：实现与证据已保存在仓库和 validation artifacts，Linear app connector 需要重新认证后才能追加事实；既有历史记录没有被改写。

历史 execution 的 PUSH/MERGE/ACTIVATION/SWIFT_BUILD/SWIFT_TESTS/MACOS_GUI_ACCEPTANCE/BLOCKED 事实保持不变；本报告只追加本轮新的实现、制品和运行证据。

