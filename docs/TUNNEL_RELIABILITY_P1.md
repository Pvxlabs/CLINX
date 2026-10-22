# CLINX 分阶段可靠性改进：P1 Tunnel Reliability

状态：源码实现与隔离资格验证；**NOT_DEPLOYED**。没有安装 unit/drop-in、
修改 profile/凭据/运行配置、重启生产 Tunnel 或执行 Owner。P2、P3 未实施。

## 实际故障边界与证据

实际网络连接：P620 tunnel-client → 本机 HTTP 代理 `127.0.0.1:7893` → 控制面
`/poll`。外部 MCP 请求进入控制面队列，经 poll 响应交给本机 Tunnel，再经 stdio
MCP requester → 独立 Execution Owner → 原生 Provider。

2026-10-20 启动的同一 client instance 中观测到 317 次 timeout、21 次其他
poll 错误、27 段报错后恢复，其中 4 段从首次报错至恢复超过 300 秒，约
16.9、32.3、24.0、16.5 分钟；该实例没有 MCP 子进程退出。时长不是精确外部
停机时间。最近一次在 2026-10-22 02:25:03（Asia/Shanghai）自行恢复，PID 未变。

已有只读 health timer 能报告 `TUNNEL_CONTROL_POLL_STALE`，但没有恢复动作；
`Restart=always` 只处理进程退出。`/healthz`、`/readyz` 成功不能证明 `/poll`
仍在前进。另一个历史故障是 stdio child 退出引起 Tunnel 正常退出，早前已修复
systemd 退出监督；不能把两种故障合并成同一根因。

**UNKNOWN**：长轮询失败由客户端连接复用、P620 代理/上游链路还是控制面导致。
日志中的 `unexpected EOF`、客户端学习缩短 poll timeout，均不能单独归责。
没有取得上游实现源码或双方连接级 trace；本 P1 为已证实的恢复缺口提供 containment，
不宣称消除了网络故障，也不通过改语言解释网络/协议问题。

线程已有证据：`/data/artifacts/clinx-p9-tunnel-20261021T182554Z/`。
本轮资格证据：`/data/artifacts/clinx-tunnel-p1-20261021T183708Z/`。

## 实现与权威边界

`tunnel_reliability.py` 是显式启用的 launcher，只监督它自己 `Popen` 创建的新
session/process group。没有 systemctl 操作、数据库依赖、任务 RPC、Provider
连接、业务 payload journal 或 replay loop。没有修改原有 Owner、租约、原生输入
恢复及其 fail-closed 规则。已有健康检查仅补充非法/非有限指标的拒绝处理，仍为只读。

- 私有目录、排他 flock、原子写入/fsync 的持久恢复预算，绑定 command hash 与 policy。
- 每代启动前删除该私有目录中的旧 health locator；只有新启动后成功的 poll 能证明恢复。
- `/metrics` 仅经 loopback 读取，禁用环境代理及重定向；独立 probe 子进程由父进程
  执行总超时约束，半开 socket、慢响应体均不能无限阻塞监督器。
- 记录状态、原因、PID/start ticks、transport generation、预算、poll age、错误计数、
  probe 耗时和检测到恢复的耗时；不记录命令参数、凭据、请求正文。
- 连续失败经过 grace 后终止本代自有 transport group，TERM 超时升级 KILL，退避后
  启动下一代。MCP child 导致 Tunnel 正常退出 0 也进入同一有界流程。
- TERM/KILL 后仍不能确认子进程退出时保留 PID，进入 HOLD，不启动第二个消费者。
- 预算耗尽 → `HOLD` / exit 78；外层重启不能清空预算。启动状态损坏、身份不匹配、
  旧 PID 仍活着或 launch intent 的 spawn 结果未知时，拒绝启动第二个消费者。
- 只有持续新鲜且确实推进的 poll 达到稳定窗口才恢复事件预算；服务持续故障不会
  因重复启动监督器而获得无限重试。
- `status` 是只读出口，联合 PID/start ticks、快照年龄与 poll 年龄判断当前健康；
  旧磁盘 `HEALTHY` 状态不能证明监督器/child 仍活着。`dispatch_verified=false`
  始终保留：poll 恢复本身不等于新任务派发成功。

监督器不会根据失联猜测任务是否执行。已接受请求、响应 ACK 丢失及 Provider ACK
不确定分别保留在原 Owner ledger 与 execution 中；现有精确 request/input readback
负责归属确认。无新证据时 UNKNOWN/lease 保留；没有自动重发 turn/start、resume、
取消或创建替代 execution。测试中的显式重复请求用于检验既有去重，监督器自身
从未发出这种请求。

## 候选策略与时限

以下为源码候选默认值，**不是已经批准或测得的生产 SLO**：

| 参数 | 候选值 |
|---|---:|
| stale_after | 120s |
| 连续 failure_grace | 15s |
| startup_grace | 45s |
| sampling interval / probe 总超时 | 5s / 3s |
| TERM grace / cooldown | 5s / 10s |
| max_restarts | 3（每事件含首次启动最多 4 代） |
| stable_for | 300s 且 poll 必须推进 |

按采样、probe、TERM、cooldown、startup 的配置边界推导：若路径在下一代启动时
恢复，保守逻辑界限为最后成功 poll 后 219s；持续失败进入 HOLD 的保守逻辑界限
为 370s。确定性时钟测试分别观测 172s（含收尾）和 357s。它们验证算法边界，
不包含任意 OS 调度、磁盘故障、进程创建阻塞、上游无限不可用或真实部署延迟。
生产恢复 SLO、路径切换能力与长期稳定窗口验收仍 **OPEN/NOT_PROVEN**。

真实二进制 fixture 使用加速策略：stale 1.2s、startup 3s、interval 0.1s、probe
0.5s、TERM 0.5s、cooldown 0.1s、failure grace 0.2s、最多 2 次恢复、稳定窗口
30s。每个可恢复场景预先设置 8s 验收界限；持续代理故障设置 16s HOLD 界限。
具体实测秒数以各场景 JSON 回执为准，不能外推为生产数字。

## 隔离资格与回归

`test_tunnel_reliability_wire.py` 使用机器上已安装的真实 tunnel-client（二进制 SHA
写入回执），本地 HTTP control-plane 与实际 HTTP fault proxy、独立 Tunnel ID、
假凭据、stdio requester、真实 ExecutionOwner/SQLite ledger/socket。原生 Provider
使用现有 deterministic fake，不调用模型、真实 Host 或生产业务。

| 故障 | 注入和验收 |
|---|---|
| 代理断连 | 接受后关闭 poll 连接；观察新 transport generation，再恢复代理 |
| 半开/停滞 | 持有 poll 连接不返回；新一代启动后释放，验证 poll 和新任务 |
| MCP child 退出 | 只终止 fixture MCP PID，真实 Tunnel 收到 child exit 后恢复 |
| 响应 ACK 丢失 | control-plane 收到结果后断开响应，再使 poll 停滞；原执行不重放 |
| Provider ACK 不确定 | fake Provider 在发送/确认边界抛出 transport uncertainty；保持原 Owner/execution/thread/binding/lease/UNKNOWN，不自动修复成已完成 |
| 持续代理故障 | 预算耗尽 HOLD；再次启动仍 HOLD、没有第四代或重复 turn |

恢复场景验证原完整 execution/owner/binding/lease 行不变，Provider turn/start
计数不增加；随后通过同一代理→真实 Tunnel→MCP→原 Owner 在另一个 disposable
工作区派发新任务。新任务仍使用同一 Owner instance，原 execution 不受影响。
Fixture teardown 直接关闭测试端点，保留未知数据库事实，不伪造终态来排空。

其他覆盖：无效/重复/未来指标、probe 总超时、锁冲突、持久预算、旧状态不确定、
PID 重用、稳定窗口、只清理自有进程组；独立 `clinx-p1-fixture-*` transient systemd
unit 验证 exit 78 与 `RestartPreventExitStatus`，不操作任何生产 unit。

回归命令（临时 fixtures 必须位于任务证据目录）：

```sh
CLINX_P1_EVIDENCE=/data/artifacts/<task> python3 -m pytest -q \
  test_tunnel_reliability.py test_tunnel_reliability_wire.py \
  test_execution_owner.py test_execution_owner_routing.py \
  test_native_input_recovery.py test_native_input_forwarding.py \
  test_desktop_native_creation.py test_lifecycle_routing_tunnel.py \
  --basetemp=/data/artifacts/<task>/fixtures
ruff check --config ruff-discovery.toml tunnel_reliability.py \
  scripts/check_tunnel_health.py test_tunnel_reliability.py \
  test_tunnel_reliability_wire.py fixtures/tunnel_reliability_mcp.py
```

既有 `test_tunnel_child.py::...::test_multiple_requests_keep_child_alive_and_stdout_is_protocol_only`
对 tools/list 的旧静态期望在干净 HEAD 和携带既有 dirty 的候选快照均失败。
已用同一运行依赖、隔离任务数据库与 node 状态目录复现；保留日志，不修改断言。
该基线失败不能被算成 PASS，具体计数以本轮 FINAL_REPORT 为准。

## 后续部署与阶段边界

仓库中的两个 `systemd/*reliability.conf.example` **未安装**。未来独立授权部署
需要 immutable release、单消费者核对、admission drain、确认现有 execution authority
全部位于独立 Owner/Provider 生命周期，再替换同一个 Tunnel unit 的 ExecStart。
不要同时开启另一套相同 Tunnel ID 的消费者，也不要把生产当前 profile 用于注入。
新的 MainPID 是监督器，旧 health 脚本的 MainPID 假设需要与只读 `status` 示例一起迁移。

监督器异常退出留下活进程或 spawn unknown 时必须人工精确核对自有 PID/group；
不要自动 kill/adopt，更不要随意删除 state.json。HOLD 的 rearm 必须先保留回执、
确认无旧消费者、处理原始执行的不确定状态，再单独授权；它不是重放任务的许可。

| 阶段 | 目标和出口 | 当前状态 |
|---|---|---|
| P1 Tunnel Reliability | 检测/有界恢复/隔离注入/不重放/生产保全证据 | 源码与隔离资格；NOT_DEPLOYED |
| P2 契约冻结 | 冻结 transport 状态、错误/ACK、恢复预算与身份/兼容契约，审查升级退出条件 | 后续阶段；未实施 |
| P3 Rust Runtime 原型 | 在 P2 契约下做隔离原型与等价性/故障验收 | 后续阶段；未实施 |

本 P1 没有 Rust 实现或权威切换，也没有改变业务、订单、执行策略或经济语义。

## 发布准备补充（仍 NOT_DEPLOYED）

后续最小release、完整Tunnel/health配对unit、显式候选policy与当前安装CAS回退
准备见 [P1 发布与回退说明](TUNNEL_RELIABILITY_P1_RELEASE.md)。本补充未修改
supervisor运行逻辑或重复已通过的隔离故障注入。离线发布/回退资格不等于生产准入；
活跃execution、内嵌Codex Bridge是否可回收、生产预算批准与独立激活授权仍是gate。
