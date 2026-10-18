# P620 CLINX 项目收敛

用户在 2026-10-13（Asia/Shanghai）要求 P620 最终只保留一个 CLINX 项目仓库。
唯一的开发仓库是 `/home/pvxlabs/dev/clinx`。历史修复 worktree、独立 pilot、
临时验收仓库和 Monitor 前端参考工作区不再作为开发项目或运行路由。

## 历史内容恢复

本轮移除目录的源码快照、文件 SHA-256 清单、暂存与未暂存补丁、独立验收仓库
Git bundle 和处置记录位于：

`/data/artifacts/clinx-project-convergence-20261013-01a130fb/`

主仓库中 `refs/archive/convergence-20261013/*` 保留历史 worktree 的精确 HEAD。
原有分支与提交保留；归档不表示历史未提交修改已经合入 `main`。
归档是恢复及审计制品，不是另外一套活动项目仓库。需要恢复时先核对
`inventory.json` 中的路径、基准提交、快照哈希以及两层补丁，避免覆盖当前开发内容。

## 后续任务的目录约定

- CLINX 日常开发使用唯一的 canonical 仓库，复用现有任务协调及 workspace lease。
  不在 `/home/pvxlabs/dev` 创建另一个长期存在的 CLINX clone、worktree 或 pilot。
- 必须隔离的临时测试环境放在本任务的 `/data/artifacts` 输出目录，显式记录归属、
  用途、运行引用和删除条件。结束后保留日志、manifest、receipt 与独有证据，
  定向回收临时仓库和可再生构建输出。
- 历史源码用 Git refs、bundle、补丁和压缩快照保留，避免用活动 worktree 长期归档。
- 发布制品位于 `.local/lib/clinx-*`，数据库与凭证位于机器现有状态和配置目录。
  它们按部署及恢复契约维护；项目目录收敛不能替代运行发布、数据库或凭证维护。
- 已撤下的 `pilot` 与 `clinx-local-discovery` 项目别名不能重定向到 canonical 仓库。
  这会改变原项目的 origin、branch 和路径身份。需要新验收时注册准确的临时身份，
  验收完成后撤下注册项并收尾。

目录收敛验证以单一工作区、证据可恢复、路由无失效目标和实际服务读回为准。
它不构成新的产品、Provider 或跨设备验收。
