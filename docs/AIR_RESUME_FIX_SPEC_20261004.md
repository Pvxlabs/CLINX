# Air 原会话续接修复 SPEC

目标：通过 P620 正式 CLINX MCP 对 air.local 的原 thread `01a1020d-adca-7711-80a5-21219bf1cb12` 完成 adoption → prepare → start → status/context 新回复读回，原 thread 和历史保留。

边界：复用节点 mTLS、共享权限、Air canonical TaskRegistry 和 Provider；中心仅记录引用到节点的路由，不复制任务状态。不涉及 ORION、Monitor UI、配对重建或其他任务控制。独立 worktree / 修复分支，从 b8a4294 开始，维护入口使用 SSH/本机。

M1：共用可信 selector；控制分离权限；Air 本机项目与 writer 核验；adoption 不发送 turn且幂等；持久引用路由与 UNKNOWN 对账。受影响回归通过后才能激活。

M2：签名 bundle 重建、回退保存、最小组件切换；正式共享暂时开启并限制到目标原 thread 和专用测试项目。固定 nonce，正式工具续接并读回新 turn、实际身份、终态与结果。禁止私有调用代替验收。

M3：专用目录的新建完成、重复 start、取消、撤权拒绝测试。恢复临时共享，记录 push / merge / install / activation 与实际证据。

验收：同一 Air thread 出现新 turn且回复含固定 nonce；task/prepared/execution 引用始终指向 Air；错误 host、hostId冲突、未授权、忙碌writer、撤销明确拒绝；P620本地与Air只读回归通过。UNKNOWN仅对账不重放。

依赖：既有SSH、签名身份、Air现有Provider。Linear 项目/任务登记在实施前调用失败：UNAUTHORIZED（连接需重新认证）；用户明确规定其不作为本轮门槛，执行记录保存在本SPEC和报告，未伪称已登记。

进度：M1 PASS。受影响 Python 147 passed / 34 subtests，Swift 72 passed。正式 MCP 经配对mTLS到 canonical adapter 的 adoption/prepare/start/status/context、同thread、重复start、撤权、busy writer、UNKNOWN对账和迟到回执回归通过。M2进行中；M3未验证。

运行切换约束：P620已有真实活动execution，不重启共享MCP/Provider或接管它。保留旧MCP及completion owner进程；临时暂停旧tunnel poller，由同一profile/tunnel的候选poller提供正式入口。候选MCP使用`--no-recover-existing`，不扫描旧execution。独立SSH负责切换/恢复；旧poller可SIGCONT回退。Air旧bundle保留，helper只通过重建后签名制品加载。

临时配置：Air scope分别限制已有thread、新建项目、可取消项目；禁用任务控制之外的项目。结束后双边正式share恢复read_sessions=true / execute_tasks=false，保留canonical绑定与只读回读。
