> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# CLINX 原生任务发现与打开修复

BASE_SHA: `6eb7620`。工作区：`/home/pvxlabs/dev/clinx-native-desktop-discovery-20261011`。
仅修改 CLINX；共享服务和已安装 Monitor 未切换版本。
证据目录：`/data/artifacts/clinx-native-desktop-discovery-20261011/`。

## 实际协议和案例

案例 `exec_e358f1c9686f458e91bf478110cf15b9`：
Task `task_394bf0e47252483d91b22e68cb68820f`，
thread `01a12b9a-7b35-79c3-a45b-2f34567291af`，
turn `01a12b9a-7ddb-7640-a1b1-dc6e3d8b3288`。

精确只读观察确认 dedicated `/run/user/1001/clinx-provider.sock` 为 `active/inProgress`，
Desktop `/home/pvxlabs/.codex/app-server-control/app-server-control.sock` 为 `notLoaded/interrupted`。
后者是该 daemon 的局部加载/历史投影，不能否定活动 Owner，也不能成为抢占或新 turn 的理由。

当前 `codex-cli 0.160.0` 导出的 `thread/start.threadSource` 是任意字符串的 analytics classification。
它不控制原生 `sourceKinds`，也不保证 Desktop 发现。新验收 thread 的实际 `source=vscode`；未伪造来源。
`sessionStartSource` 的 `startup/clear` 同样不能替代可见性验收。
`thread/list` 默认 interactive sources，可能受 cwd/modelProviders/sourceKinds/archived 等过滤。
只读 CLINX observer 只允许 `useStateDbOnly=true` 列表，避免默认 rollout metadata repair。
官方协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。

Desktop App 的 `list_threads` 返回案例，但 API inventory 不等于渲染验收。
`open_in_codex` 只打开 panel，不支持普通 `codex://threads/...` 导航；不能拿它模拟打开会话。
Air 上实际注册的 `codex://` handler 为 `/Applications/ChatGPT.app`（当前 Codex mode 宿主）。
使用系统 `open` 的完整远程 URI、Accessibility 实际页面标题和正文完成验收。
未编辑 Desktop 私有 DB、rollout、缓存或 IPC。

## 修复行为

- prepare 的 `codex_uri` 精确解析并先确定授权 host route，远程请求再走既有 node command plane。
- URI 指向现有 binding 时续用同 Task/thread；未绑定时走既有原子 adoption，不启动 Provider。
- active Owner/Execution、未知或冲突身份、不同 Task、错误 project/host、fuzzy query 均保留原记录并拒绝。
- 新建无 URI 不按标题/summary 续用旧对话。重复活动任务与 worktree lease 防护保留。
- URI 优先不增加权限；原生 adoption 不安装 Host tools。必要 policy/tool successor 仍走既有 lineage，历史 URI 不静默重定向执行。
- prepare/adopt/start/status 和 Observer 返回 canonical URI。历史 Execution 与 replay 使用捕获线程，不借用当前 Task successor。
- 现有 Monitor Inspector 增加 URI 和打开链接；事件刷新保留该字段，旧 response 缺字段仍可解码。
- 修正 offline adoption 用 CLINX `client_version=1.0.0-m6` 校验 Codex CLI 版本的错误；使用已验证原生 session 版本保存 provenance，续用仍校验真实 Provider。

## 验收

`DESKTOP_CASE_VISIBLE=PASS`：Air 真实侧栏包含 `Private Input Admission v2`。
`DESKTOP_CASE_OPEN=PASS`：完整远程 URI 导航后，`AXWebArea` 标题为该任务，页面实际显示原始请求与执行正文。
`CASE_OWNER_POLICY_IDENTITY_PRESERVED=PASS`：打开前后 canonical Owner 记录相同，Task/thread/turn、route/policy JSON 相同。
原 turn 在观察期间自然完成并进入 execution_history；比较 exact 历史记录，未人为改终态或重放。

新任务使用本次源码和现有 Provider，qualification registry 独立于共享 registry：
execution `exec_3f4bd4baf7254b3384b29020a4a4f896`，
thread `01a12bb1-f6b4-7d01-9f9b-2b0e42f9308b`，
turn `01a12bb1-f95b-7512-89f0-be64d9fb769f`。
native result PASS，无工具调用；两个端点的只读 thread/list 均实际返回该 thread。
`DESKTOP_NEW_VISIBLE_AND_OPEN=PASS`：Air 页面标题为 `CLINX Desktop native URI acceptance 20261011`，
正文实际显示 `CLINX_EXECUTION_RESULT`、`STATUS=PASS` 和 `SUMMARY=CLINX native Desktop discovery acceptance`。

真实隔离 native qualification：初次原生 thread + adoption + 两次带 URI 的 create-intent preparation，
均沿用原 thread，并完成两个 PASS Execution。首次运行发现版本校验缺陷的失败日志保留，修复后的真实重跑 PASS。
fixture 定向测试 16 passed。扩展回归 375 passed / 62 subtests，1 项基线已有失败：
`tests/test_m13b.py::DynamicToolProtocolTests::test_public_catalog_includes_adoption_without_shell_tools`
固定期望 15，但当前 catalog 已有 16；在未改主工作区复现，未改断言。

Air 对本次 57 文件源码快照执行 `swift test --filter NativeURITests`，完整构建成功，1 test PASS。
传输包 SHA256 `7c3470a8a2d6e7d3f3d95b41d3e6a4bc958cc0cb1662fdf905d9e43bf1eeaa4e`。
不把源码构建记作已安装 Monitor 升级。

## 交付边界

`DEV_IMPLEMENTATION=PASS`；上述真实 Desktop 发现/打开分别为 PASS。
`SHARED_RUNTIME_ACTIVATION=NOT_RUN`；`INSTALLED_MONITOR_UPDATE=NOT_RUN`。
没有重启 CLINX shared Provider/Owner/tunnel 或其他服务，没有更改当前活跃 Execution 权限或历史。
本次未证明跨 daemon 实时事件订阅或流式展示；完整 URI 能导航不等于另一个 daemon 成为 Owner。
此前用户无法发现任务的精确 Desktop filter/cache 原因仍 UNKNOWN，本次不声称改了上游筛选机制。
