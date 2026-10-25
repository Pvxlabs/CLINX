# Rust 原生生产入口切换

日期：2026-10-25，Asia/Shanghai。证据根：
`/data/artifacts/clinx-native-cutover-20261025`。

## 已执行的切换

`rust-bc61bd1362196018cde3` 的九个运行二进制与 `f645055` 留存 SHA 完全一致，
以 `412b8c5` 的 Rust 构建器安装。正式 manifest SHA 为
`f4ca2315943c5f46c09a6899770dc8043389386ab3000d8a6653f6ca9caac447`。
安装前使用 SQLite 只读在线 backup 保全 Authority 和 Centre 数据库，保留配置、服务定义
及原始状态证据；没有初始化新权威、导入旧数据或手工修改任务/lease。

Authority、Dispatcher、MCP、Centre 已选择该 release 的 Rust 入口，保留原 UID、
策略、socket、Provider、挂载和观察配置。Centre 仍监听 `0.0.0.0:18773`，没有套用
首次安装模板中的 loopback 端口。Tunnel 的 MCP launcher 已改用同 release 的 Rust relay。

Tunnel 使用生产路径专用包 `tunnel-reliability-p1-eafd924798bdf2c4d20f`，监督器为 Rust，
Go transport SHA 为 `31f83c5cab00cdb17b594085706190106d174a88d49ea6e7edec8e3823183f9d`。
首次启动因执行路径改变触发旧 supervisor identity 校验。确认旧 supervisor/child 已退出、
持有独占锁、前后 Go hash 相同、参数及策略相同后，只更新 release 路径派生的 identity；
完整原状态已归档，launches=2、launch_sequence=31 和所有其他字段原样保留。没有清空恢复预算。

## 已取得的生产证据

- 初始源码完整 Release 回归：591 PASS，0 FAIL / ignored。
- 本地和云端 MCP 对同一新任务精确返回 `SUCCEEDED / ACKED / RELEASED`。
  execution 为 `exec_10994749ac072058c30cc0c97ec77de6`，thread 为
  `01a17398-9ca3-7d82-bd3f-8409c9767af4`，turn 为 `01a17398-9d99-74c3-ae8f-9d82b4a8fef0`。
  原始响应为 `CLINX_RUST_CUTOVER_OK_20261025_NATIVE`，验收提示禁止工具、文件和网络操作。
- 重复 start 同一 prepared 返回同一个已完成 execution。
- 自建取消验收 `exec_f9b637f713cc15ea53adb81e03cc5107` 在发送前取消，返回
  `CANCELLED / NOT_DISPATCHED / RELEASED`，没有创建 Provider thread/turn。
- Tunnel 实际新鲜 control poll PASS；Centre 继续接收 Air/iMac 的上传及 ACK。

## 实际大回执故障及前向修复

历史执行 `exec_ef575e91eff9fca282ac0762842dc890` 的 Authority 状态为 RUNNING / ACKED / HELD，
认证 Provider 对原 thread `01a16dd8-a3ff-70a0-b1d1-3bfbe9979742`、turn
`01a16dd8-a54a-70b2-8051-dfee55608092` 的回读为 completed。
原 Dispatcher 日志重复报告 `REQUEST_TOO_LARGE`。完整 turn 的紧凑 UTF-8 JSON
为 **1,587,826 字节**；问题发生在 1 MiB 请求传输边界，而不是已修复的 SQLite receipt 预算。

修复保留所有原始证据：仅已认证 Transport 的 `business.observe_commit` 和
`business.adoption_commit` 允许 4 MiB 请求。Authority 先按真实 peer UID 设定读取上限，
解析后再次核对操作专属上限；公开 API、其余操作及所有响应仍为 1 MiB。
SQLite receipt 仍为 8 MiB，原 nonce、身份、事务、幂等及恢复规则不变；超过边界明确失败。
这是有界大证据支持，不是无上限历史传输。

新增真实进程回归使用 1.6 MB 完整 turn，发送后丢失回复，再重启 Authority 并精确回读：
checkpoint 字节完整、终态及资源释放持久化、同 operation 只提交一次、Provider 只执行一次。
另验证普通 API 即使由 Transport 发送也不能借用较大限额，超限请求不能创建任务。
既有断言没有删改或 skip。

修复后完整 Release 回归 **593 PASS、0 FAIL / ignored**，受影响 Authority/Transport/
路由回归 37 PASS；Release 全部二进制重建通过。Scoped Clippy 通过，保留 10 个既有
library 告警；受影响 Rust 文件格式和 `git diff --check` 通过。

本节源码修复的生产安装与历史任务恢复尚待独立回执；不得用新小任务成功代替该历史执行恢复。
完整切换结果以此证据根内的最终回执为准，不恢复 Python 业务实现。
