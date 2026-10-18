# P620 → Air 原生线程可见性：DEV 验收

范围：在既有 Unified Task Lifecycle v1 分支修复新建 P620 用户线程的
Air Codex Desktop 可见性。没有部署、重启服务、改写正式 registry/授权库，
没有操作已有用户任务的执行轮次。

## 根因证据

官方 Codex 0.160.0 schema 将 `threadSource` 定义为客户端提供的 analytics
分类字符串，并未承诺 GUI visibility。它与 `thread/list.sourceKinds` 不同。
CLINX 创建路径漏传 `threadSource=user` 属实，但并非充分根因：同 canonical
cwd、真实 CLINX originator、`threadSource=user` 的测试线程
`01a150b7-147c-7ce1-8c6c-801d64aed0e7` 在独立 AppServer 创建后，普通 proxy
list 可见，Air 普通 P620 列表仍未出现。NULL 分类对照也曾短暂出现在 UI 读回中；
不能据分类字段断言 GUI PASS。

通过 Desktop 实际使用的官方 daemon 对上述测试线程执行
`thread/name/set`，保留其原测试标题后，线程仍为 `notLoaded`，未 resume、
未启动 turn，即进入 Air 普通列表。对新建线程发布官方元数据通知是本次实测
有效的刷新路径；不能将其推广为闭源客户端所有缓存行为的完整根因分析。
普通列表重新展开、App 置前台本身没有证明能刷新未加载样本。

## 最小实现

- `bridge.py` 四个持久用户线程创建路径均传 `thread_source="user"`。
- 新线程完成既有 identity guard/绑定后，仅对已配置的同节点
  `native_socket` 建立一次现有 daemon 连接。精确核对 thread ID、cwd、
  originator、threadSource 和标题；通过官方 `thread/name/set` 发布原任务标题。
- 该操作属于授权创建路径的元数据写入。没有接入 observation/reconcile，
  不扫描、不启动 daemon/proxy、不 resume/subscribe，不转移执行 Provider，
  不重试派发。不可用时返回 UNAVAILABLE 并告警，不丢失真实 binding。
- 非本节点、未配置 native socket、身份不符、已有不同标题时不写入。
  新 Host successor/migration 保持原 Canonical Task ID；原线程不做 backfill
  或批量 metadata 改写。
- `app_server.py.thread_start` 已支持 optional threadSource，本次只补 RPC
  参数/归因回归，没有改其实现。原 clientInfo/originator 保持真实。

## 真实验收

证据目录：`/data/artifacts/clinx-air-thread-visibility-20261018T2015/`。
所有 registry 写入限于该目录的 disposable DEV fixture。

| 层 | 结果与证据 |
| --- | --- |
| 真实 CLINX 创建 | `TaskDispatcher.dispatch` + 独立 stdio Provider；fixture task `task_2722d7d1bd334d4ead7dc771e94ebfa2` 绑定 thread `01a150c5-2052-7733-be0b-1218043a4a03`；`bridge-visibility.json` |
| 自动普通列表发现 | Air AX 在任何手动打开/恢复该新线程之前读到 `...BRIDGE_NOTIFY`；`air-evidence/ax-receipts.json` |
| API list/filter | Air → SSH p620 → 官方 app-server proxy 的普通 list、state-only、sourceKinds filter 均含精确 thread ID；`air-evidence/ssh-bridge-continuation.json` |
| 安全恢复与继续 | 同一新线程、readOnly sandbox、无工具文本 turn `01a150c6-f1b5-7390-9730-3f2fa788875e` 收到官方 `turn/completed`，原生 items 含正确 `...BRIDGE_AIR_CONTINUED`；不是模型自报 PASS 作为完成依据 |
| GUI 普通列表打开 | 先打开另一条本任务测试会话，再精确聚焦普通 sidebar 中 `...BRIDGE_NOTIFY` AXButton，核对 AXFocusedUIElement 后按 Enter；实际截图确认切换至同一线程，显示原始内容和 continuation 返回；`air-evidence/air-sidebar-keyboard-open.png` |
| GUI 深链打开 | 官方深链打开同一线程，截图显示正确标题、原始内容和 continuation 返回；`air-evidence/air-bridge-open-continued.png`。没有在 GUI composer 输入/发送内容 |
| Canonical 保持 | native continuation 后 DEV fixture 仍仅一个 task，binding 不变；`final-readback.json`。没有伪造额外 execution_ref 或授权 |
| Air proxy 创建对照 | Air SSH 官方 proxy 创建 `01a150cb-2dc4-7ef1-a5a4-47cd46e45822`，真实完成只读测试 turn；初始持久化前列表为空，完成后普通/state-only/source filter 均可读；`proxy-create-readback.json` |
| 测试 | 213 passed，39 subtests passed；`publish-final-tests.log` |
| lint/build | 新测试 Ruff PASS；改动既有文件与基线同为 21 条历史 lint 诊断，无新增；py_compile、diff whitespace PASS。无 macOS 源码改动，无需声称 Xcode build |

DEV harness 第一次真实 bridge stdio turn 被过早关闭而 interrupted，保存原始记录，
未记为成功；随后在同一测试线程上通过 Air 官方 proxy 恢复并取得真实 completed。
早期 AX click/全局坐标探针未可靠确认导航，不能作为打开成功证据；官方深链截图
独立证明打开。最终普通侧栏按钮的受保护键盘激活探针成功，另行保留原始回执与截图。

Air proxy 创建对照的 originator 由已经运行的 Desktop daemon 固有环境记录为
`Codex Desktop`，即使 initialize 的 clientInfo 是 CLINX。没有设置 override 或
伪装该归因；因此生产桥接执行继续使用原独立 Provider，元数据通知不能替代
执行路由。真实 bridge 样本 originator 仍为 `linear-local-codex-bridge`。

## 边界与限制

- 本次证明 P620 + 当前 Air Desktop 版本的上述有界 DEV 路径。iMac 与其他
  版本没有重新验收；远程专用 Provider 的跨主机通知不在此本地 socket 修复范围。
- native daemon 不在线时通知不会自动补偿；返回 UNAVAILABLE，不能声称 GUI
  已同步。没有增加后台执行、重试循环或服务自动启动。
- 正式服务仍使用原部署版本，功能分支通过不等于生产修复已部署。
- 旧样本 `01a14abb-124e-7413-868d-d1ced3703081` 的隔离 canonical 映射
  仍只属于先前 fixture；本次没有对它或 `01a15064-5354-7ec3-8f96-6f3fba947079`
  执行 adopt/resume/start/cancel，也不宣称正式 THREAD_UNBOUND 已解决。
- GUI 继续显示真实 Thread，CLINX Task ID 由 registry 展示；未修改闭源 UI。
  DEV task ↔ thread 双向定位由既有 binding 和官方深链验证，未增加 Task 表。
- canonical 主工作区原 35 个 dirty 文件的 SHA256 均未变化。
