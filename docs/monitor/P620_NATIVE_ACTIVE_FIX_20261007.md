# P620 原生任务 Active 状态修复与 Mac 安装更新

2026-10-07，时间按 Asia/Shanghai。源码基于 `2c2454aa9cc2bd543ba1a087fb40336428950ebd`，保留已提交的 Dock／应用名称修复 `aeb722c`。

## 实际问题与根因

用户报告时，P620 的以下两个 Codex 会话都在执行，但 CLINX Active 为 0：

| 会话 | 精确 thread_id | 错误展示 |
| --- | --- | --- |
| 修复 Dock 图标并重命名 App | `01a11539-9516-79f3-b441-7459a840186f` | Recent 中 RUNNING 阶段、Unknown 状态 |
| 追踪 SPCX 事件下游分发 | `01a1147e-8a0e-7760-8b27-529b5a1ac88f` | 数小时前关联记录的 QUEUED |

真实 owner 是配置中的既存 Codex app-server；精确只读查询证明两条当前 turn 为 `active/inProgress`。另一配置端点上的 `notLoaded`／历史 `interrupted` 不能覆盖实际 owner。

第一处根因是 `ObservationDirectory._project` 无条件输出 `liveness=NOT_PROVEN`。Swift 对缺少运行证明的持久化 RUNNING 记录保持 Unknown，这是正确的保护，但采集链路从未提供它需要的实时证据。

第二处根因是 App 按 observation 哈希顺序读取有限历史窗口。当时 P620 有 520 条会话，SPCX 在该节点排第 350；三个来源平分 500 条上限后，SPCX 被截掉，只剩 canonical 的旧关联记录。该关联任务没有 canonical execution／turn，不能把其 QUEUED 直接改写为 CLINX 启动了原生执行。

## 最终行为

- P620 采集器在 HTTP 读取路径之外，复用现有配置端点的只读 `observe_thread` 和纯 `classify`，校验 thread、当前 turn、owner 与 cwd，发布带时间戳的公共运行证据。短暂缓存最多 10 秒，证明在 reader 侧 30 秒过期；节点离线、身份冲突、turn／cwd 不匹配均不能得到 PROVEN。
- App 先读取 RUNNING 候选，再读取历史页。持久化 RUNNING 本身仍不能进入 Active。
- Observer 增加只读 `native_conversation` 关联元数据，以及精确 `native_thread_id` 查询。App 精确读取无 canonical execution 的关联会话，避免运行中或刚完成的任务被历史窗口截掉。
- 最新 native turn 优先；仅同一 task／execution 的观察行由 canonical 行去重。新鲜原生运行证明或比关联记录更新的原生终态可以替代旧关联记录的展示，真实 canonical execution 保持独立。刷新详情时保留关联元数据。
- 原生展示身份仍是 `native_observation_*`，没有制造 CLINX execution、业务 PASS、执行授权或进度百分比。

SPCX 在验证期间自然完成：其 turn `01a114f4-4970-72e0-bbf3-935dcdaa927e` 转为 COMPLETED，Codex 转为 idle。最终验收应显示其为 Recent／Completed；仍在执行的 Dock 修复会话在 Active／Running。继续本次工作后的当前 Dock turn 为 `01a1156e-ed97-7b13-a60b-36413e538237`，实际接口返回 RUNNING／PROVEN。

## 验证与安装

| 项目 | 结果 |
| --- | --- |
| Python 采集、TTL、离线／冲突／错误身份、精确查询、Observer 只读、schema、跨进程回归 | PASS：86 tests、9 subtests |
| Air 最终源码 Swift 全部回归，包含 Dock 窗口与原生任务展示 | PASS：100 tests、0 failures |
| Air／iMac 最终 release bundle 构建、签名和安装 | PASS |
| Air 最终安装版实际 Active 列表 | PASS：Running，修复 Dock 图标并重命名 App，p620 |
| Air 最终安装版实际 Recent 列表 | PASS：Completed，追踪 SPCX 事件下游分发，p620；旧 QUEUED 行没有覆盖它 |
| Air 最终安装版关闭窗口后实际 Dock 重开 | PASS：DOCK_FINAL_REOPEN |
| iMac 最终可见 GUI 验收 | SKIPPED：锁屏，用户明确要求只验证 Air |

Python 验证使用独立 `.validation/active-native-20261007/venv`，依赖遵循 `requirements-discovery-dev.txt`，复用已安装的生产 OPAQUE 原生模块。初始全局 pytest 的缺依赖收集失败不计 PASS。Swift JSON／源码契约测试同步到当前统一列表入口。

两台均安装到 `/Applications/CLINX.app`；`CFBundleName` 为 CLINX，bundle identifier 仍为 `com.pvxlabs.clinx.monitor`，使用各自已有签名证书。12 个修改文件经逐文件 SHA-256 校验同步，最后的详情元数据保留修正再次校验、构建并安装。

| 安装身份 | MacBook Air | iMac |
| --- | --- | --- |
| 独立候选目录 | `/Users/tinzleung/github/CLINX-dock-fix-20261007` | `/Users/tinzleung/Developer/CLINX-dock-fix-20261007` |
| 最终二进制 SHA-256 | `6610ee6cd2e4a424fa0a06cfe5961ac1fbdd024d2cd3f490a89fbcd25de87dd8` | `0358ade650192b97ef3f58dfd3f26c5b16affc75b7829c2ce35295960c0ccd7c` |
| 原 Node helper PID，安装前后相同 | `38754` | `71674` |

原主工作区及 iMac 不相关修改没有覆盖。原 `/Applications/CLINX Monitor.app` 隐藏链接继续指向 Dock 修复时保留的原始 helper 备份；现存 Node LaunchAgent 没有重新加载、重启或改路径。该备份和链接不能当临时文件删除。

## P620 部署边界与证据

只有 observation centre 和 Observer 切换到独立 release 并重启；配置、共享身份、spool／cursor 与既有执行数据保留：

- Centre：`/home/pvxlabs/.local/lib/clinx-network-observation/releases/active-native-centre-1f20391b36bd`，实际 PID `2170383`，配置既存 `bridge.toml` 只读 owner 观察。
- Observer：`/home/pvxlabs/.local/lib/clinx-network-observation/releases/active-native-observer-d3282295406c`，实际 PID `2170378`。既存 `50-native-activity.conf` 的有效 ExecStart 一并更新，保留其余限制。
- CLINX 服务 PID `666987`、execution owner PID `3152482` 保持不变。没有 Provider 启动、resume、replay、取消任务或 registry／binding／lease 写入。
- SPCX 的 canonical `task_03c5a033b64e462e87127ba122e8e97b` 仍保持原 QUEUED、无 execution／turn；执行字段及更新时间与部署前逐项相同。改变的是原生任务展示投影。

P620 证据目录：`.validation/active-native-20261007/`，包含发布 manifest、源码 SHA-256、部署前后服务读回、原始 canonical 事实、当前／历史运行观察、两台安装回执。Air 实际 GUI 记录位于对应候选目录的 `.validation/active-native-20261007/gui-final.log`；原生最终测试及构建日志为 `/private/tmp/clinx-active-air-swift-final-20261007.log`、`/private/tmp/clinx-active-air-build-final-20261007.log`。iMac 构建日志为 `/private/tmp/clinx-active-imac-build-final-20261007.log`。

每台最终替换前的 GUI 备份在各自候选的 `.validation/active-native-20261007/installed-before-final.app`；首轮安装回执另存为 `install-receipt.first.json`。源码文本首次同步损坏被编译拦截，随后改为压缩传输、逐文件校验才通过构建并安装。最终 GUI 验证只对 CLINX 自身窗口／菜单／Dock 元素执行 AX 操作，没有向其他 App 发送键盘输入。
