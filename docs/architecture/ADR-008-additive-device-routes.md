# ADR-008：设备执行路由的离线增配

用途：记录生产设备执行路由扩展的权威边界。适用范围：已有 Rust Authority 的新增设备/项目。
状态：REFERENCE；2026-10-26 接受。验证：`policy_extension` 回归及每次独立维护回执。
当前命令入口见 [运维指南](../OPERATIONS.md)，不是已部署声明。

## 背景与决定

Authority 把启动策略封存在 `business_config`，仅改 JSON 会触发
`AUTHORITY_POLICY_MISMATCH`。Mac 配对与观察在线也不自动建立任务执行路由。
必须继续由 Rust Store 的排他 writer 负责持久化，禁止使用 SQL 脚本、Python fallback
或重建数据库来新增设备。

复用 `clinx-maintenance extend-business-policy` 作为显式离线工具。由既有 Authority
用户运行，先取得原 writer lock、校验 Authority identity、旧策略哈希及完整业务链。
要求没有活动执行或 ACTIVE lease；只追加项目、endpoint 和显式 Provider route。
已有项目、路由、workspace、Host 操作权限、lease 参数和派生 Git 范围不能变更。

策略更新与 `OFFLINE_MAINTENANCE` 回执在同一 SQLite 事务进入既有
`business_commands` 哈希链，Task/Execution 投影不变。相同 operation ID 和输入读回
原回执；同 ID 不同输入拒绝。未知提交结果只能读取或以原 ID 取得回执，不能生成新操作。

## 操作边界与后果

工具不启动/停止服务、不替换 root 控制的策略文件、不修改配对信任或共享权限，
也不自动回滚数据库。操作者必须保存备份、停止并确认旧 writer 退出、执行增配，
再选择回执对应的新策略和 release。数据库提交后若文件选择失败，应修复文件选择；
不得拿旧数据库覆盖已提交事实。

此入口不支持撤销或重写旧路由；这些变更需要另行设计明确的授权及兼容性流程。
原策略不具备显式路由时，新增映射必须逐一保留旧主机原 endpoint，禁止把旧任务重定向到 Mac。
公开 MCP/IPC 没有全局策略修改接口；配对仍以 OPAQUE/mTLS 与双边 scope 独立校验。
