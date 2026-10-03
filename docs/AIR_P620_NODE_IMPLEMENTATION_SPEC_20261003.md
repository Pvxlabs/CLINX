# CLINX Air/P620 多设备节点实现 SPEC（2026-10-03）

## 目标与边界

把当前 Air 交付分支中已有的节点协议接入真实运行路径：中心进程使用既有 `NodeRegistry`/MCP 路由，Air helper 使用既有 `NodeIdentity`、`TrustedPeerStore`、OPAQUE 配对和 mTLS，先完成注册、心跳、离线/重连和只读会话读取，再以独立 scope 接入 canonical execution 回调。沿用现有 Task/Execution/policy/provider 权威，不新建任务数据库、mesh、历史同步或第二套权限系统。Monitor 已完成的修复和验收证据只作为回归基线。

本 SPEC 不包含 ORION、交易、数据库迁移、共享服务的无批准切换，也不把源码通过等同于跨机实机通过。

## 交付面

- `node_protocol.py`：认证注册/心跳消息、中心注册服务、远端 mTLS reader/executor 路由、授权 scope 变化和 stale/offline 处理。
- `mcp_server.py`：在同一节点注册库上加载中心路由；真实中心 reader 通过共享注册库进入 MCP，不直接 attach 假 reader。
- `MonitorApp/Scripts/node_service_entrypoint.py`：从可信配置读取中心 endpoint，绑定可达地址，使用已配对 mTLS 身份注册、心跳、断线重连，scope 未批准时不启动远端服务。
- `node_execution_adapter.py`（如需要）：通过现有 `ClinxIntegration` 的 prepare/start/status/cancel canonical 方法提供 node callback；缺少合法本地 provider 配置时返回明确 `OPERATION_NOT_IMPLEMENTED`，不宣称可执行。
- `local_discovery/identity.py`/helper：持久化 endpoint 与显式可撤销 read/execute scope，所有变更走产品入口。
- 隔离 integration tests：启动真实中心和 Air helper 进程，使用认证传输、注册、心跳、MCP 读回和撤销/恢复路径，验证无 task/turn/lease 副作用。

## 验收标准

1. 未配对、未授权、身份不匹配和撤销连接全部拒绝；注册不信任客户端自报 node_id、用户名或 endpoint。
2. 认证注册后中心路由精确到 `source_node_id`；单节点离线时返回不完整 coverage，不合成全局不存在；恢复后路由可读。
3. 重启中心或节点后能够重新注册；心跳过期标记 OFFLINE，取消旧授权后旧连接不能读取或执行。
4. read scope 与 execute scope 独立；读取不创建 Task/Execution、发送 turn、resume/cancel 或取得 execution lease。
5. canonical execution 回调使用现有 policy、Task/Execution、provider adapter 和 request ledger/fence；重复请求不重复启动，未知结果需要对账。
6. 同一个构建候选完成 Swift 回归、必要 Python 回归和 Air/P620 实机检查；源码、制品、激活、推送分别记录。

## 里程碑

- M0：SPEC、当前 HEAD/dirty 状态和 Linear 记录。
- M1：中心注册/心跳/动态远端路由与 Air 注册循环。
- M2：持久化授权 scope 与 canonical execution adapter。
- M3：隔离跨进程回归、Swift/Bundle 回归。
- M4：在现有权限范围内进行 P620 激活、Air 只读样本验收；取得明确 execute 批准后再做定向执行。
- M5：追加报告、Linear 事实和 push；未完成项按代码缺口、验证失败、外部批准分别归类。

## 验证命令

- `python -m pytest -q <受影响测试>`，之后再运行必要的完整 Python 回归。
- `cd MonitorApp && swift test --scratch-path /private/tmp/clinx-air-node-swift-20261003`。
- `MonitorApp/Scripts/build-app.sh`，通过实际 `.app` bundle 运行验收。
