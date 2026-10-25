> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# P1 Tunnel：复用上游纯传输 runtime

2026-10-22。状态：隔离候选已验收，生产 NOT_DEPLOYED / NO-GO。

## 定位与实现选择

README 和 EXECUTION_AUTHORITY 规定 CLINX 转发原生 Codex 消息，维护身份、去重、
Owner/lease 与回执；执行环境及其权限由原生 Codex 持有。
此前建议给内嵌 Bridge 加 idle/ACK/fence/CAS 是保留第二套内嵌执行能力时的修复。
本次选择复用同一上游 commit 已有的 `cmd/client-runtime`，无需修改上游或新增
Codex 执行状态机。该入口不引入 Bridge/admin UI，仍使用相同 poll/MCP/metrics 包。

边界是关闭 Tunnel 管理 UI 的可选 Codex 执行入口；不取消 MCP 转发，不限制经
CLINX 转发给原生 Codex 的工作，不改 native sandbox/审批/模型策略或业务授权。
没有把 systemd、Git、数据库或生产权限检查加入任务转发的公开流程。

源码及制品固定到 upstream `0f870e50a973fa820d4c409000059e181e8d242b`，
不是重写 Tunnel、切换 Rust 权威或宣称原始网络/长轮询根因已解决。

## 构建与发布

`scripts/build_transport_runtime.py` 校验固定 upstream tar 和 Go 1.27.3 linux/amd64
工具链 SHA；只在新 `/data/artifacts` 输出目录解包构建。编译环境不继承运行凭据。
真实依赖图必须含 runtime/MCP/control-plane/health/metrics，拒绝 full client、
adminui、codexappserver、codexplugin、cloudflared 及其子包。
使用 readonly modules、trimpath、关闭 VCS 探测和 CGO、固定架构；不同源码目录
双次构建字节一致。保存源码逐项哈希、真实构建器源码、依赖图、Go build info、日志
及 manifest；结束删除 checkout、工具链和缓存。首次构建的只读 Go 缓存清理错误已
修正并有回归，失败日志保留，不能把失败命令报告成成功。

`scripts/build_tunnel_release.py --transport-runtime-manifest <manifest>` 是可选新路径：
核验候选 provenance/文件哈希/依赖图，在 immutable release 内加入 runtime 二进制
和来源清单，权限为0755。渲染的 ExecStart 指向 release 内二进制，不指向临时
artifact 路径，不覆盖当前 `~/.local/bin/tunnel-client`。不传新参数时保留原有行为。
配置管理仍严格限于既定 Tunnel/health 三项；旧二进制和依赖仍是回退 guard。
新增 release 目录的未来落盘与 manifest 逐项验证由激活 driver 负责，配置 CAS
本身不安装/鉴定 runtime，也不执行任何 systemd 生命周期操作。

这些本地哈希记录绑定资格证据，不是签名、生产操作授权或不可伪造的回收凭据。

## 本次验证与证据

根目录 `/data/artifacts/clinx-p1-transport-only-20261022T005407Z`：

- `candidate-v2/manifest.json`：真实 upstream runtime，无 patch；二进制 SHA256
  `31f83c5cab00cdb17b594085706190106d174a88d49ea6e7edec8e3823183f9d`。
- upstream CLI、runtimehealth、runtimeconfig 三个有测试的 package 通过；
  runtimeapp 无测试文件，如实保留 Go 输出。
- `profile-compatibility.json`：只调用配置解析器，用内存中的当前 Tunnel 环境与
  当前 profile，full/runtime 的控制面、MCP、health、logging、process、TLS 相等。
  没有启动 app、poller、Owner、Provider 或第二个生产消费者；不复制/打印凭据。
- `wire-tests.log`、`wire-acceptance.json`：实际二进制、旧 supervisor、本地控制面，
  两代共64次并发访问现有及未知 Codex 路由均404；poll推进，MCP tools/list 和
  fixture_echo 正常；通过 PATH 与所有已知 command override 设诱饵，Codex调用0。
  仅终止本测试创建且PGID吻合的 MCP 子进程；退出至恢复并完成后续检查1.899秒，
  是加速隔离参数下的观测上界，不是生产 SLO。无业务任务或Provider调用。
- `package-tests.log`：新构建/制品拒绝条件与原 CAS 回归43项通过；新增 wire 1项
  通过。未重复旧代理/半开/ACK故障矩阵；其证据哈希列入 reused-evidence.json。
- `package-qualification.json`：真实配置副本 forward→rollback 完全恢复内容、
  模式和原先不存在状态；过期回退零写入拒绝；10个归档成员及模式/哈希通过；
  两次 release/capsule/plan 字节一致，systemd 静态 verify 通过。

旧隔离故障矩阵针对旧 full binary；本次没有将它冒充为新 runtime 的完整故障矩阵。
共同传输包来源相同，加上新入口的退出恢复验证，仍不等于生产真实派发验收。

## 尚未关闭的首次迁移门槛

| Gate | 结论 |
|---|---|
| 新候选不承载内嵌 Codex，生命周期内无需猜 idle | PASS（静态依赖与隔离运行范围） |
| 当前安装 Bridge 可安全停止 | BLOCKED：只观察到无工作，仍缺 pending/uncertain/fence 权威 |
| 新候选发布与当前配置 CAS 回退 | PASS（离线副本）；生产安装/回退未执行 |
| 当前 execution/lease 排空 | BLOCKED：本次只读快照仍有2个 active execution/lease |
| 生产恢复预算/SLO 批准 | OPEN；沿用候选219s/370s条件逻辑界限，未当作生产承诺 |
| 新制品生产激活权限 | 未覆盖：此前授权精确绑定旧 release；本轮不切换约束继续有效 |
| 生产故障恢复与真实新任务验收 | 未验证；本轮没有真实任务派发 |

不能用“新 binary 没有 Bridge”证明“旧 Bridge 此刻安全可杀”。目前没有满足
既定约束的自动首次迁移路径：禁止停止、接管旧进程，禁止启动第二个生产消费者，
旧 Bridge 又不提供权威 quiesce。因此该 live gate 没有伪造关闭。
后续需取得旧 Bridge 的真实权威排空/持续准入隔离证据，或另行审查有明确授权的
生命周期隔离迁移方案；不临时增加强杀、接管或延长静默来绕过。

未来激活还须对**新制品**封存授权，批准预算，权威排空包括触发维护本身的执行，
明确历史 UNKNOWN 的保留边界并取得 fresh 一致备份，重新核验 release 文件及
配置 CAS。激活后需独立观察 poll 和真实链路，新任务验收另按授权范围执行。
不能使用旧配置/整库恢复回滚新的执行事实，不重放业务，不改orion-data epoch。

P2执行内核契约冻结、P3 Rust Runtime原型保留为后续阶段。
