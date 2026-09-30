# CLAUDE_CODE_HANDOFF — CLINX Monitor Phase 0/1

交接日期：2026-09-30 UTC。用户指定产物批次名：clinx-monitor-spec-20261001。

## 当前交付与 authority 边界

P620 CLINX 是唯一 authority；Mac 是原生 Swift/SwiftUI/MenuBarExtra 只读 observer。
已实现四个 authenticated GET endpoint 和单独启动的 loopback server，未部署。
没有修改 bridge/TaskRegistry/completion/host executor/ORION，也没有调用 live status、
reconciliation、Start、Cancel、Retry、Approve 或 Deploy。任何运行验证都使用 fixture DB。

必读：
- docs/CLINX_MONITOR.md：来源审计、schema 字段、HTTP/auth、cursor、UI、轮询、验收矩阵。
- docs/architecture/ADR-006-monitor-read-only-observer.md：选择原因和剩余风险。
- observer_server.py：纯只读 DB adapter + HTTP adapter。
- docs/monitor/observer-v1.schema.json、api-examples.json：生产者 schema 与合成 examples。
- docs/monitor/ObserverModels.swift、ObserverClient.swift：Swift reference，尚未编译。
- test_observer_server.py、test_observer_schema.py：定向契约、安全和 schema 验证。
- docs/monitor/FINAL_REPORT.md：最终验证和 Git readback 证据。

## Phase 2 Mac 实现顺序

1. 在授权的 macOS workspace 创建 macOS 13+ SwiftUI MenuBarExtra app，把参考模型和
   URLSession actor 纳入 Xcode target；先跑 examples 解码测试，校验 convertFromSnakeCase、
   optional null、未知状态/字段、ISO timestamps、2 MiB bounds 和非 200 错误。
2. 使用 @MainActor store 管理 fresh/stale/connection 状态，不在 Mac 维护 execution authority。
   先实现只读 Active/Recent + task detail 的完整层级；按 spec 聚合 IDLE/RUNNING/BLOCKED/
   PASS/DISCONNECTED。COMPLETED 没有 exact PASS 时不能显示 PASS。
3. 接入 Keychain、HTTPS endpoint 设置；禁止 redirect、ATS 例外、凭据日志和 mutation actions。
   系统登录启动为可选设置，SMAppService 仅处理 Mac 自身。
4. 单一 async 轮询任务：active 2s、idle 15s，低功耗至少 15s；active stale 10s / idle 45s，
   sleep/wake、网络变化、401 停止快速重试、分页去重、cursor reset 和连接退避按 spec 验证。
5. 用 fixture backend 做原生 UI 验证；覆盖 light/dark、VoiceOver、reduced motion、
   notifications opt-in、冷启动无重放通知、BLOCKED/PASS/connection transition 去重。
6. 有独立部署授权后才在 P620 安装 observer-only service 和 Tailscale Serve 私有 HTTPS。
   不得重启现有 CLINX/ORION，不得用 Funnel，不得启用公网监听。OS 层 DB 只读、
   WAL 读取、tailnet ACL、错误 credential、rotate/revoke 和端口暴露审计是部署门槛。
7. 真实 Mac -> Tailscale -> P620 只读验收之后，才填写相应 PASS。不能把 Linux fixture
   tests 或 reference Swift 文件写成已验证的 native UI/runtime。

## 后端细节与坑

- 不得复用 ClinxIntegration.get_status；该入口能 reclaim/reconcile。
- 不得构造 TaskRegistry 作为 observer reader；构造器可能 migration。
- 不能把任务当前 route/model/result 借给历史 execution；exact task/execution/turn 必须匹配。
- 不能把 last_progress_at 当 heartbeat；codex_running 是持久化声明。
- phases 为空、progress_percent=null 是正确行为。禁止按时间、token、工具调用、推测阶段百分比。
- 不能把 stdout/stderr/argv/prompt/hidden reasoning/raw_result/路径开放给 UI。
- v2_events 是已有可选事实流，coverage PARTIAL/UNAVAILABLE；不能为 Monitor 补写事件。
- artifacts 当前为空，没有安全 artifact registry。不要解析 free text 里的文件路径。
- 当前全部可见 task 对 bearer 持有者可读；没有 per-user/project ACL。
- Offset 分页会随更新移动；cursor 对 DB replacement 的检测有限；source schema 不兼容返回 503。
- 凭据 env 变更需仅重启 observer；不可假定运行中进程自动读取已修改 env 文件。

## 验证命令

Backend runtime 仅 Python stdlib。JSON Schema 测试使用 development-only jsonschema >=4.18,<5。

```text
python3 -m unittest -q test_observer_server test_observer_schema
python3 -m unittest discover -q
python3 -m pytest -q
git diff --check
```

Mac 编译/UI/真实网络测试仍为 NOT_RUN，具体证据见 FINAL_REPORT。
原始失败记录由本次 managed host execution evidence 保存；不能删除或将未运行写成 PASS。

## 指定 artifact 目录阻塞

本 execution 的 registered project 范围之外无法写入：
/data/artifacts/clinx-monitor-spec-20261001/

Host executor 原样错误：
TARGET_NOT_REGISTERED: development_command path is outside the registered project

FINAL_REPORT.md 和本文件已保存在 docs/monitor/，内容可供审查。
外层 operator 需要为指定目录绑定合适 target/授权后导出两份文件并核对 SHA-256。
当前 worker 不得自行创建嵌套 CLINX execution、绕过 path guard 或通过脚本隐藏外部写入。
导出完成前总交付状态保留 BLOCKED；其他已通过的代码/spec gates 独立保留 PASS。
