> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# P1：仅转发职责的正式候选

2026-10-22，本轮按用户明确指令收敛。CLINX 保留任务接收、精确原生对话转发、
身份/幂等投递、状态持久化、回执关联和结果返回；不承担上游审批、下游执行权限或
业务授权。此前维护审批实验不再作为 P1 发布前提，不部署、不扩建。

`scripts/build_forwarding_release.py` 复用现有 Tunnel release builder、归档和状态
哈希工具，将已安装的干净 MCP Python 源仅替换 `workspace_contract.py`，与原 P1
纯传输 runtime、supervisor、Tunnel/health 配对 unit 一同封装。loader 指向新的
immutable control 路径，Python 依赖仍精确复用原安装环境。其余 canonical dirty
代码不混入；不改变 Owner、Provider、任务策略或业务执行入口。

候选不包含 `maintenance_authorization`、`maintenance_operator`、
`maintenance_witness`、`policy_maintenance`，也不引入 cryptography 等实验运行
依赖、审批配置、凭证和 listener。已核对当前运行入口没有引用这些模块；实验
builder 和测试仍引用它们，因此保留全部既有源码，不做清理或删除。

发布范围为 Tunnel unit、health unit、99-p1 drop-in、MCP loader 共四项。
回退包保存精确 before/after、内容哈希、模式及原先不存在状态；不恢复数据库、
历史结果、lease、fencing epoch 或 supervisor 预算。新 builder 只构建制品，
不增加审批服务、生产写入工具或额外任务准入层。

本轮证据根目录：
`/data/artifacts/clinx-p1-forwarding-release-20261022T031713Z`。
新增 5 项定向测试通过；真实候选双构建一致、102 项文件核验、实际已安装 Python
加载候选 parser、四路径隔离 forward/rollback、过期回退零写入拒绝、systemd
静态 verify 均通过。不重复此前授权实验或 Tunnel 故障矩阵。

生产激活停于用户指定的实际工具拒绝边界：对历史安装脚本 `deploy.py` 的
**未改动 snapshot() 预检函数**作只读调用，返回
`AssertionError({'executions': 4, 'worktree_leases': 4})`。完整安装脚本未执行；
其要求整张执行表及 lease 表为空，后续还会停止 Owner，并非当前四路径的窄发布。
现有 P1 CAS 只有隔离副本写入口，没有四路径生产配对回退 driver。

因此生产未变更，停止在既有安装流程不适配该候选的实际阻塞；不把审批主体、
witness 或多方托管列为阻塞。下一步是修复已有安装流程的窄发布及生产配对回退
能力，保持 Owner/Provider 原样，不能清空执行事实来满足旧脚本断言。
生产切换后的 poll、预算、任务派发/回执验收均未执行；本轮当前链路只读观察
不等于候选生产验收，原始长轮询根因仍 UNKNOWN。
