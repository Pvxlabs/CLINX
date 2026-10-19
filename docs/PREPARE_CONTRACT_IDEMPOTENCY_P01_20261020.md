# CLINX P0.1：prepare 契约与 fingerprint 幂等性

状态：`PREPARE_CONTRACT_AND_IDEMPOTENCY=PASS`（本地源码及 disposable 数据库定向验收）。
分支：`codex/lifecycle-routing-tunnel-20261020`；本轮基线 `6e08b911044a8fadfcd6bb0b6b022575d6e9e5ea`。
本轮不部署、不合并 main，不修改 Tunnel、Finalizer、其他状态修复或 ORION。

## 根因与修复

- 连接器原有 schema 无法传入 workspace contract。基线分支已加入输入字段，
  但校验、输出封存与 MCP scrubber 仍不一致：路径类型约束不完整，scrubber 会递归
  移除封存中的 cwd/branch，且 native adoption / policy 写入可能先于 workspace 校验。
  现在公开输入/输出共用闭合 schema，并在参数解析和 integration 入口校验 shape，
  在 adoption / policy 写入前核对真实 Git 身份。scrubber 只保留完整的三个公开封存字段，
  其他内部 routing 字段仍按原规则处理。prepare 的可空 summary 输出类型同步实际值。
- `adopt_task()` 复用 native task ID 后仍无条件 INSERT fingerprint；registry 初始化
  已为同一 task 补过 fingerprint 时，触发 `UNIQUE constraint failed: work_item_fingerprints.task_id`。
  现在在原 `BEGIN IMMEDIATE` 事务内比较既有 fingerprint；一致则复用，冲突返回
  `WORK_ITEM_FINGERPRINT_CONFLICT`，不覆盖或忽略冲突。首次 native adoption 先校验原
  task 的 fingerprint，再把已验证的 repository origin 补全与 fingerprint 更新一起提交；
  保留原 task 标题、summary、fingerprint 创建时间及所有既有历史。
- 精确相同的 adoption 重试返回原 task/binding，不重复写 adoption 审计；不同
  cwd/project/origin/branch/session/project ID 或已有其他 owner 明确拒绝。
  native adoption 的 title/summary 是准备上下文，不会替换原任务身份；后续请求内容仍
  由 prepared payload 封存。新任务的 task 与 fingerprint 插入现在处于同一事务。
- 相同完整 payload 的未消费 `PREPARED` 请求在写事务内复用原 prepared ref，并再次校验
  原 integrity hash；并发提交同样收敛。`FAILED` 记录保留，新的准备重试生成新 ref。
  已运行/已派发请求仍按原 prepared ref 的 start/readback 契约处理；本轮不改变其状态逻辑。

## workspace_contract 输入规则

```json
{
  "workspace_contract": {
    "cwd": "/absolute/path/to/registered/project-or-linked-worktree",
    "branch": "codex/example",
    "head": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

三个字段必须同时提供，均为非空字符串，不允许额外字段或 null。
cwd 为无 NUL 的绝对路径；branch 为无空白/NUL 的 attached branch；head 为完整小写
40 位十六进制 Git SHA。实际 cwd 必须为 checkout 根目录，branch/HEAD 必须与 Git 一致，
且只能选配置项目或其现有 linked worktree，继续保留 common-dir、worktree list、origin 校验。
指定既有 native conversation 时不能通过契约更换该 conversation 的 cwd。

顶层 cwd/branch/head 仍被拒绝。prompt 明确指定 workspace 却未传结构化契约时返回
`WORKSPACE_CONTRACT_REQUIRED`。普通任务未指定 workspace 时沿用既有行为：封存配置工作区；
linked workspace continuation 可继承原 seal。start 前的漂移与 integrity 检查保持有效。

## 定向验收

证据目录：`/data/artifacts/clinx-prepare-p01-20261020/`。

- 新增 `test_prepare_contract_idempotency.py`：32 passed。覆盖 MCP schema/解析/完整输出，
  新任务准备、缺失/错误/漂移契约拒绝、native 原任务/fingerprint 复用、fresh admission、
  并发 adoption/prepare、不同身份与 fingerprint 冲突、失败记录保留和重试。
- 使用 SQLite trigger 注入 task/fingerprint、adoption binding、prepared INSERT 失败，
  比较全部业务表快照，证明被测失败无半成品 task、错误 fingerprint 或新增 lease。
  另以真实 fixture execution history 和 active execution/lease 验证冲突不修改原有记录。
- 相关既有回归 169 passed、6 subtests passed（prepare/MCP/native admission/identity/
  workspace routing/基础 registry）；未重跑全部历史测试。
- `test_native_desktop_discovery.py::test_exact_uri_prepare_start_resumes_original_without_preliminary_tools`
  在未修改基线 archive 中也失败：断言预期不含 network_access，而分支既有代码传入 False。
  最终相关回归明确排除这一基线失败；没有修改测试或放宽网络语义。
- 新增测试和 workspace_contract 的配置化 Ruff 检查通过；三个既有大模块 Ruff
  基线与当前均有 18 项遗留问题，增量为零；`git diff --check` 通过。

## 实际边界与未解决项

本轮只证明源码和 disposable fixture 的准备契约/幂等性，不证明外部连接器 schema 已刷新、
生产准备已恢复、真实 Provider 执行、Tunnel、Finalizer 或整体 lifecycle 修复完成。
本轮无生产数据库写入、服务切换或重启。原 canonical main 和 `bridge.toml` dirty 状态保留；
保护文件 SHA-256 为 `5cfd3c26d68d626624ef1b496aab4d5fcadb6ff3f152fc17db5ff79ec3517fa4`。
