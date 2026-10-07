# CLINX macOS Dock 恢复与应用改名交付（2026-10-07）

本次修复已安装到 MacBook Air 和 iMac 的 `/Applications/CLINX.app`。应用显示名称、主窗口、菜单栏标题、打开／退出菜单及无障碍标签统一为 `CLINX`。

## 源码原因与修复

App 在关闭最后一个窗口后继续驻留，但原 `MonitorAppDelegate` 没有实现 `applicationShouldHandleReopen`。菜单栏独自处理窗口恢复，Dock 再次点击缺少同等恢复路径。原菜单栏通过窗口标题和 `canBecomeMain` 查找窗口；该属性与窗口显示状态有关，AppKit 也可能保留已关闭的窗口对象。

`CLINXMonitorApp.swift` 通过一个不参与点击命中的 `NSViewRepresentable` 登记实际主窗口；关闭通知清除弱引用。Dock reopen 和菜单栏共用 `showMonitor`：有现存主窗口时解除最小化并置前，已关闭时通过 SwiftUI `openWindow(id: "monitor")` 重开。其他可见窗口（例如 Settings）不会阻止主窗口恢复。保留原 WindowGroup 场景；重复恢复复用现存窗口。

`Info.plist` 的 `CFBundleName`／`CFBundleDisplayName` 改为 `CLINX`，构建入口生成 `.build/CLINX.app`。同步更新当前 README 和窗口捕获入口。已有 bundle identifier、签名身份、Keychain service、应用支持目录保持既有身份。

## 验证

| 检查 | MacBook Air | iMac |
| --- | --- | --- |
| Swift 全部测试（含 4 项新窗口恢复测试） | PASS：98 tests，0 failures | PASS：98 tests，0 failures |
| release bundle 构建 | PASS | PASS |
| 安装版 `codesign --verify --deep --strict` | PASS | PASS |
| `/Applications/CLINX.app` 实际 GUI 进程路径 | PASS | PASS |
| Dock 标题 CLINX | PASS | PASS |
| 关闭／Dock 重开 5 次、隐藏、最小化、仅 Settings 可见、菜单栏恢复 | PASS：实际 AX 操作与 Dock 点击 | 未验证：当前锁屏，frontmost 为 `com.apple.loginwindow` |
| 无窗口驻留 70 秒后点击 Dock | PASS：`IDLE_70_SECONDS_DOCK=PASS` | 未验证：锁屏 |

P620 的 18 项 `test_monitor_finalized_ui.py` 检查、两个 shell 入口的语法检查、plist 解析及 `git diff --check` 均通过。70 秒观察不等于数小时／睡眠唤醒的持续可靠性验收。

新增 `WindowReopenTests.swift` 覆盖重复关闭后重开、Settings 可见时恢复、重复恢复不新建窗口及解除最小化。

## 安装与服务边界

两台机器各自使用已有 `CLINX Monitor Local Development` 证书签名；旧安装版移入本次独立候选目录的 `.validation/dock-reopen-20261007/installed-before.app`。两台机器现有 `com.pvxlabs.clinx.node` 的加载配置仍引用 `/Applications/CLINX Monitor.app/Contents/Resources/CLINXNodeService`，因此旧安装路径保留为隐藏符号链接，指向各自原始备份。它供现存 Node 服务使用，不能作为普通临时文件删除。本次没有重新加载或重启 Node 服务，也没有修改其加载配置。

| 项目 | MacBook Air | iMac |
| --- | --- | --- |
| 独立候选目录 | `/Users/tinzleung/github/CLINX-dock-fix-20261007` | `/Users/tinzleung/Developer/CLINX-dock-fix-20261007` |
| 新 GUI 安装路径 | `/Applications/CLINX.app` | `/Applications/CLINX.app` |
| 原 Node helper PID（安装前后相同） | `38754` | `71674` |
| 安装二进制 SHA256 | `043d639158c33100f0eb2c69f0c0f3cea2e78520186c378e8162dd9bb90bf028` | `6b0b4342d9d36c36ccd50f5fb724b8ba73edbd1e38b0cef99ab9db8ee20d9a5d` |

这两个二进制分别在各自 Mac 编译、签名，哈希不同。两台候选基于同一源码基线 `2c2454aa9cc2bd543ba1a087fb40336428950ebd` 加本次改动；7 个修改／新增文件的 SHA256 已与 P620 逐项匹配。主窗口入口源码 SHA256 为 `5f6d30000c24aed3514f3c8a6f7db092a2a5b54fdda79ba6f35e6cbc18d5ae11`。

两台原主工作区均未被覆盖。iMac 原有 `observer_history_guard.py`、`test_observer_history_guard.py` 和未跟踪报告保留。本次更新 GUI；现存 Node 服务仍使用各自备份中的原始资源。

## 证据位置（分别位于对应 Mac）

- Air 测试：`/private/tmp/clinx-dock-air-swift-test-final-20261007.log`；构建：`/private/tmp/clinx-dock-air-build-app-final-20261007.log`。
- Air GUI：`/private/tmp/clinx-dock-air-gui-20261007.log`；操作脚本：`/private/tmp/clinx-dock-gui-20261007.applescript`。
- iMac 测试：`/private/tmp/clinx-dock-swift-test-shipping-20261007.log`；构建：`/private/tmp/clinx-dock-build-app-shipping-20261007.log`。
- 各自候选目录：`.validation/dock-reopen-20261007/install-receipt.json`。

Air 首次在 Documents 下构建遇到 `resource fork, Finder information, or similar detritus not allowed`，移至独立 github 目录并使用 `/private/tmp` scratch 后全部测试／构建通过。第一次安装在注销未注册候选时得到 Launch Services `-10814`，随后复用已验证的 staging bundle 完成安装，未重新复制或盲重试。失败日志保留在对应 Mac 的 `/private/tmp`。
