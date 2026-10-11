# CLINX RCA 收口与发布

本轮基线为 P620 main `363c954473c5c8956515c27fd729b02faf61bde4`；GitHub main
`9259029663da0512b113fbc6380d355fdec9ec7b` 是其直接父提交。复用既有 scoped native
routing 修复和 151 passed / 13 subtests 的合并后证据，不重新开展 RCA。

新建原生任务与 continuation 均在本轮请求携带结果协议；开发者说明明确无工具、仅回显和
验收标识任务也必须输出六字段。原始 prepared prompt/hash 不改写，解析器、审批、CAS、
租约及不确定执行的禁止重放规则保持原合同。

今后的无工具验收提示词使用：

> 验收标识为 `<唯一标识>`。不调用工具，不读写文件，不执行业务操作。最终只返回一个
> CLINX_EXECUTION_RESULT 块，包含 STATUS、SUMMARY、CHANGED_FILES、VALIDATION、
> BLOCKERS、NEXT_STATE，各占一行。将标识原样写入 SUMMARY 和 VALIDATION；任务完成
> 使用 STATUS=PASS、CHANGED_FILES=NONE、BLOCKERS=NONE、NEXT_STATE=COMPLETED。

`clinx_get_node_status` 返回三个独立部分：`cache_status` 为节点缓存状态和时间，
`connection_status` 为真实连接观测（未探测时 UNKNOWN/null），`task_authority` 为任务权限
选择（没有 task 时 NO_TASK_SELECTED/null）。顶层 reachability/execution_active 也在无证据时
返回 null。SharingScope 的 execute_tasks 不代表 canonical task 的 policy grant；精确权限
使用 get_effective_authority，执行及 Provider 观测使用精确 get_status。读节点状态不发起
Provider、读取线程或更新时间来制造存活证据。MCP 输出 schema 同步支持 null。

原 reauth 未应用原因继续 UNKNOWN；10 月 7 日复用已有授权的真实入口 PASS 保留；
10 月 8 日缺 marker 的 BLOCKED 历史回执保留。后者不被重派或重写为 PASS。

定向回归、新增无工具真实 Provider 验证、制品 manifest、发布前后 identity、canonical
历史摘要和回退记录保存在 `/data/artifacts/clinx-rca-closure-20261011/`。
发布状态以该目录的最终 receipt 为准。只发布 P620 CLINX control MCP/Owner 的必要模块，
不切换 Monitor/UI、node centre 或已有 Provider 进程。

定向回归为 208 passed、19 subtests passed；默认跳过的真实 Provider opt-in 已单独运行，
1 passed，返回完整六字段、标识保留、native/Host 调用均为 0。该验证使用隔离 DB 和专用
临时 Provider，不代表 ChatGPT 客户端新增权限 apply 验收。

扩展回归保留 276 passed、46 subtests passed、2 failed。两项失败在未修改的 P620 main
363c954 上分别重现：远程配对测试环境缺 PRODUCTION_PAKE backend；旧 dynamic catalog
测试硬编码 15，而正式 catalog 已有 16 个工具。不修改这两项无关测试，不记扩展回归 PASS。
