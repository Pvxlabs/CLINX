# CLINX Monitor Phase 0/1 最终报告

报告日期：2026-09-30 UTC。产物批次：clinx-monitor-spec-20261001。
总交付状态：**BLOCKED**，原因仅为指定外部 artifact 目录不在当前 execution 的 registered
project 范围。仓库内 specification/backend contract 实现与 Linux 验证已通过。
未验证的 Mac/Tailscale 部署层不能记为 PASS。

## 已交付 — PASS

- docs/CLINX_MONITOR.md：TaskRegistry/execution projection/host evidence/structured
  result/MCP/tunnel 审计；canonical schema v1；security/rotate/revoke；Swift/UI
  hierarchy、状态映射、通知、低功耗、stale、Keychain、acceptance matrix。
- docs/architecture/ADR-006-monitor-read-only-observer.md：单一 authority 与只读 adapter 决策。
- observer_server.py：四个 authenticated GET route，固定 loopback listener，
  SQLite mode=ro/query_only、单请求一致 snapshot、时间/数量/响应大小 bounds。
- docs/monitor/observer-v1.schema.json、api-examples.json：JSON Schema + 明确标记的
  synthetic fixtures；无真实 ORION payload。
- docs/monitor/ObserverModels.swift、ObserverClient.swift：Codable/Sendable 模型，
  HTTPS URLSession async/await reference、Keychain、拒绝 redirect、响应大小限制。
- test_observer_server.py、test_observer_schema.py：只读/auth/serialization/unknown/
  ownership/cursor/bounds/schema 验证。
- docs/monitor/CLAUDE_CODE_HANDOFF.md：下一阶段 Mac 与真实网络验收步骤及阻塞恢复方式。

Backend 仅使用 stdlib；schema 测试需要 development-only jsonschema >=4.18,<5。
没有修改已有 TaskRegistry、bridge、completion runtime、MCP、host executor 或 ORION 代码。
没有部署 observer，也没有启动或重启任何既有 CLINX/ORION 服务。

## 验证证据

| 验证 | 结果 | Host evidence |
| --- | --- | --- |
| python3 -m unittest -q test_observer_server test_observer_schema | PASS，21 tests，1.593s | hostexec_cb04a2b9c2ea45b98792610c4d5087ea |
| python3 -m unittest discover -q | PASS，393 tests，7.054s | hostexec_37de2e97755c4b31a9bdaf637bc3f0bf |
| python3 -m pytest -q | PASS，592 tests + 75 subtests，14.23s | hostexec_6475a95f81c24120aa6c71466613bca7 |
| git diff --cached --check | PASS | hostexec_e91a9d39ab1741f8aea13fcd71cf79df |
| Source DB 不变 / SQL 写入拒绝 | PASS，fixture bytes + logical dump 相同，DELETE 被 query_only 拒绝 | 定向测试 |
| HTTP auth/method/route/bounds | PASS，含 actual loopback fixture HTTP | 定向测试 |
| Exact result / prior execution isolation | PASS，旧 PASS、错 turn 不投影为当前 PASS | 定向测试 |
| 无真实 denominator | PASS，progress_percent=null，phases=[] | 定向测试 + schema 负例 |
| Swift/macOS 编译、MenuBarExtra UI | 未验证，P620 swift executable 不存在 | 未运行 |
| 真实 Tailscale/TLS/ACL、credential rotate/revoke | 未验证，未部署 | 未运行 |
| /data/artifacts 指定目录写入 | BLOCKED，TARGET_NOT_REGISTERED | Host executor 拒绝 |

开发过程中保留了失败 evidence：新增测试插入时的缩进错误；测试试图修改 append-only fixture
事件被拒绝；未知 event kind fixture 不符合 V1 observed 命名约束被拒绝。随后修复了测试构造，
未放宽 ledger 约束。以上最终通过的结果替代这些开发失败作为最终验证结论；失败记录未删除。
unittest 输出中的预期 dispatch/identity failure 日志属于负向 fixture，最终 suite 为 OK。

## Git 集成与远端证据 — PASS

Canonical origin：git@github.com:Pvxlabs/CLINX.git，branch：main。
实现提交：6ffcb0d56bf74f7539d8dc8403b862b24d015519
基线提交：0138356。

2026-09-30 17:58:57 UTC 已核验：
- push_current_branch：成功推送 main（hostexec_414bb2b1eb774bf288073a2afd3b1246）。
- git ls-remote origin refs/heads/main：与上述实现 SHA 完全一致
  （hostexec_57658039812b4386a9ec30aeb451ee43）。
- git status --porcelain=v1：空，clean
  （hostexec_3658266b4dbe451297d0d2c2ad4a69a7）。
- git rev-list --left-right --count HEAD...origin/main：0 0
  （hostexec_5ddf62c515e248bc859ba628962d9486）。

本报告在实现提交之后以文档提交加入；报告提交不改变上述经验证的代码。
报告提交后的最终远端 SHA / clean 0/0 另由本次执行结尾的 Host readback 与
CLINX_EXECUTION_RESULT 记录，避免在文件内部自引用自己的 commit hash。

## 安全与语义边界

P620 是唯一 authority；Monitor 不构造 TaskRegistry、不调用 get_status/reconciliation、
provider、host operations、shell/log/filesystem API。没有 Start/Cancel/Retry/Approve/Deploy。
Bearer 持有者只能通过 GET 读取被允许的字段；execution mutation classes 只作展示。
Mac 没有执行权限。测试只创建隔离 fixture 数据库和随机 loopback 测试端口。

模型 effort 不是 hidden reasoning。没有 raw terminal、argv、stdout/stderr、
prompt、conversation binding 或 raw_result。changed_files 路径被省略；artifact registry
不存在时 artifacts=[]，不扫描 filesystem。PASS 是匹配 task/execution/turn 的结构化结果，
不代表部署/生产 UI/真实交易验收。

当前运行 ORION task：没有调用其 live registry/status/provider/reconciliation 或 mutation；
没有读取它的 prompt/log/output，也没有改变它的 execution semantics。当前执行工具正常
持久化的是本次 Monitor task 的 host-operation evidence，不是 ORION task 操作。

## 阻塞 — 必须保留 BLOCKED

请求写入 /data/artifacts/clinx-monitor-spec-20261001/ 时，已绑定的 Host executor 返回：

TARGET_NOT_REGISTERED: development_command path is outside the registered project

当前 execution 无法在该目录生成 FINAL_REPORT.md 和 CLAUDE_CODE_HANDOFF.md；
文件已在仓库 docs/monitor/ 中完整提供。没有绕过 path guard，也没有创建嵌套 execution。
需要外层 operator 正式绑定该目标后导出这两个文件，并回读/核对 SHA-256。
指定目录导出完成之前，不得将总体任务标为 COMPLETED/PASS。

## 风险和未验证范围

- Mac reference 尚未编译；真实 UI、Keychain、通知、深浅色、辅助功能、低功耗均未验收。
- 未部署网络服务；Tailscale Serve/ACL/TLS/OS 只读权限和真实 rotate/revoke 尚未验证。
- 可选 ledger coverage 永远是 PARTIAL/UNAVAILABLE；没有完整历史保证。
- Cursor 仅检测高水位回退，不能识别所有 DB replacement；offset 分页有并发移动风险。
- codex_running 是持久化标志，health 的 authority_liveness 明确 UNKNOWN。
- Free text 裁剪与常见 secret pattern 清理不是通用 DLP；所有 bearer 持有者可读取可见任务，
  无 per-project/user ACL。生产者不得在 title/result 写入敏感内容。
- SQL adapter 与既有 schema 耦合；未来 migration 必须跑 observer tests，不兼容时 503。
- 对 phases、progress 与 artifact metadata 的未来扩展必须先建立 canonical 持久化事实来源。
