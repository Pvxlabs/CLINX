# Mac 原生构建与 GUI 交接

## 当前交接状态

```
MACOS_GUI_ACCEPTANCE=BLOCKED
SWIFT_BUILD=NOT_RUN
SWIFT_TESTS=NOT_RUN
INSTALLED_BUILD_IDENTITY=未验证
FINAL_STATUS=PARTIAL
```

P620 当前没有 Swift/Xcode，也没有可达的匹配版本 macOS App。没有取得安装 bundle、版本、签名、二进制 hash、截图、CPU、主线程、内存或交互延迟数据；不能据此宣称用户现场的 100% CPU/未响应已经解决。

## 精确构建与测试入口

在隔离的 Mac checkout 中执行：

```sh
cd MonitorApp
swift test
./Scripts/build-app.sh
open '.build/CLINX Monitor.app'
```

本次代码修正提交为 `4ac2e24`（包含前序 `afe3c2f` 的实现与本次 canonical terminal guard），本报告随后作为本地交付提交保存在同一分支；基线为 `edeea54f5d2b871a88c8180950eb91f0856fd43c`。测试需至少包含 `CLINXMonitorTests.ActivityTests` 与 `MonitorStoreTests` 新增用例；整包 `swift test` 的通过数量和失败必须原样记录。不得把 Linux/P620 Python 结果写成 Swift 通过。

## 隔离与安装核对

1. 使用本 worktree 的提交和独立 Mac 构建目录；SwiftPM `.build`、DerivedData、临时 HOME/XDG、日志和样本目录只属于本次验收，不触碰其他 checkout。
2. 按 `MonitorApp/Scripts/build-app.sh` 的既有本地签名流程构建，不替换共享 CLINX/Observer/Provider/Monitor 服务，不改真实 registry/lease/原生历史。
3. 安装前核对：
   - `CFBundleIdentifier=com.pvxlabs.clinx.monitor`
   - `CFBundleShortVersionString=0.3.0`
   - `CFBundleVersion=3`
   - `LSMinimumSystemVersion=13.0`
   - `codesign --verify --strict` 成功
   - 记录 `.app` 路径、签名身份、`shasum -a 256`（或 `codesign` 可复核的二进制 hash）以及实际启动进程版本。
4. 安装前后确认运行的 PID、bundle 路径和 commit/tree 与本交接一致；现场旧 App 必须退出或明确记录，不要把旧版本行为当作本提交结果。

## GUI 验收序列

使用本次中文长报告作为 Activity 样本，包含链接、代码段、长消息、代表性 tool activity 和接近上限的消息量。连接只读 Observer，选取同一 task 的旧 completed execution 与新 active execution，并按以下顺序记录屏幕录制/截图和时间：

1. `running → completed`：列表、Completed 计数、徽章、详情、菜单栏同时收敛；确认 spinner 停止、阶段和耗时固定，旧晚到列表/详情/分页响应不能回滚。
2. 选中旧 execution，再快速切换新 execution 和 `Completed/Active`；确认标题、Activity、事件和 result 不串 execution，也不串 source。
3. Activity 停在底部接收新消息；向上连续滚动并选择文本；确认位置不被抢回，出现 `New activity`/`Jump to latest`，点击后只跳到最新尾部。
4. 使用 `Load earlier feedback` 加载历史页；确认锚点保持、旧页不会推进 live cursor，历史页仍可访问。
5. 在收到 terminal 状态后观察前三次高频尾部读取；模拟暂时错误、索引迟到、分页仍有内容，再确认后台以退避恢复，最终 result 到达后停止高频刷新；不要以“没有 spinner”替代最终正文核对。
6. 关闭 Activity/Inspector 后重新打开，切换 task，再切回；确认旧订阅、旧 continuation 和旧 scroll 定位不会继续更新新 execution。
7. 在上述操作交错期间 resize 窗口，持续至少 60 秒；记录无未响应、持续忙循环、主线程长阻塞和滚动交互延迟。

## 性能与现场证据

在修复前后使用匹配版本 App 采样同一序列：Activity Monitor 取样或 Time Profiler（含主线程栈）、CPU%、线程数、内存、窗口交互延迟、滚动/布局耗时；保存时间戳、PID、bundle 路径和截图。若仍卡死，保留 sample/trace 原件与发生步骤；若未复现，写 `NOT_REPRODUCED`，不要推断根因。16 MB 只代表 Activity 显示原文预算，现场报告不要把它解释为 App 内存上限。

交接完成后，回填本文件的 `SWIFT_BUILD`、`SWIFT_TESTS`、`INSTALLED_BUILD_IDENTITY` 和 `MACOS_GUI_ACCEPTANCE`，并把证据路径与 hash 写入同一验收记录。当前这些字段保持 `NOT_RUN`/`未验证`。

```
WORKTREE=/home/pvxlabs/dev/clinx-terminal-state-fix-20261003
BRANCH=codex/fix-native-terminal-state-20261003
BASE_SHA=edeea54f5d2b871a88c8180950eb91f0856fd43c
COMMIT_SHA=4ac2e24
MAIN_WORKTREE_MUTATION=NONE
OTHER_EXECUTION_CONTROL=NONE
SHARED_RUNTIME_MUTATION=NONE
PUSH=NOT_RUN
MERGE=NOT_RUN
ACTIVATION=NOT_RUN
```
