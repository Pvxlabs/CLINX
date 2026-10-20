# Native full access

普通 Codex 任务采用 `NATIVE_FULL_ACCESS`，实际 `turn/start` 使用
`sandboxPolicy={"type":"dangerFullAccess"}` 和 `approvalPolicy="never"`。
CLINX 不注入可写目录白名单、网络禁用或额外审批。旧 `network_access`
参数仅为调用兼容，不再限制任务；新建和续派均默认完整访问。续派通过
既有版本化策略转换移除旧限制，历史执行记录保持原样。原生服务若拒绝
该配置，返回真实错误，不静默回退到 workspaceWrite。

任务身份、请求去重和现有 workspace lease 仍用于任务协调。
工作目录用于路由，不是 Codex 的文件访问白名单。
