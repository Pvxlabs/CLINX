# CLINX Local Discovery v1 — 开发实现与验收

日期：2026-10-02。此文档描述隔离 worktree 中的开发实现，未激活公共 daemon。

## 验收结论

- STATUS=PARTIAL
- SECURITY_STATUS=BLOCKED
- WORKTREE=/home/pvxlabs/dev/CLINX-worktrees/local-discovery-v1
- BRANCH=codex/local-discovery-v1
- BASE_HEAD=788330293350a80b036dd600ee0ec6bc9679a239
- ORIGIN=git@github.com:Pvxlabs/CLINX.git
- IMPLEMENTATION=发现、持久化身份与信任、开发模式真实 SPAKE2 配对、双向 TLS 重连、CLI 和权限隔离已实现。
- REAL_DEVICE_VALIDATION=未验证（AIR/iMac）；P620 本机实测单独记载于下文。
- 不应将开发模式测试通过解释为生产安全配对完成。

唯一可用 PAKE backend 是 `python-spake2==0.9`。上游明确说明：
“This library is very much not constant-time, and does not protect against timing attacks.”
来源：https://github.com/warner/python-spake2#security
该依赖采用真正的 SPAKE2 算法，但不满足本功能的生产安全门槛。
实现没有自创密码交换，也没有 PIN 明文/哈希比较 fallback；
`PairingProtocol` 是替换和安全审查边界。当前 backend 永久标为
`DEV_ONLY`，显式 `--allow-dev-pairing` 才能使用，持久化的开发信任同样受此门禁。
依赖安装成功不会解除安全门禁。

## 现有结构审计及复用

| 现有结构 | 复用方式及边界 |
| --- | --- |
| `bridge.build_parser/main` | 新增 devices、pair、serve 分支；其余命令保留。新增 `bin/clinx` 无参数默认进入 devices。设备命令不初始化 TaskDispatcher。 |
| `bridge.detect_runtime_host`、`execution_semantics.normalize_host/HostIdentity` | 现有 canonical host 的 stable_identifier 直接作为 node_id；长期 key fingerprint 写入 HostIdentity.machine_id。首次配置/检测得到的名称被持久化，后续不同配置拒绝隐式重命名。没有第二套可执行 host registry。 |
| `bridge.toml` runtime、workspaces、projects | 仅读取 runtime_host/task_db_path 来选择本机已有身份和状态目录。广播不修改 host/workspace/project 注册或 task route。 |
| `TaskRegistry/RoutingIdentity` | 不新增/更新 task、workspace lease、execution 或路由记录；peer store 仅是本地设备密钥与 trust metadata。 |
| `execution_policy`、`host_contract`、`HostExecutor` | 原有 authority/capability/operation class/lease 校验保持原状。设备 listener 不提供执行入口。 |
| `app_server.JSONRPCTransport`、local/SSH-stdio | 原有 provider transport 保留。LAN identity transport 使用独立的 `DeviceTransport` 接口，不把原始 provider/Host RPC 暴露给网络。 |
| credential storage | 原仓库没有设备身份 key/trusted-peer store。新增私有 state 子目录；不复用 Linear/provider token，不读取其他凭据。 |
| runtime | `clinx serve` 或 `clinx pair accept` 启动前台设备 runtime，独立持有本机 state 的排他锁；本次没有启动/重启公共服务或安装自启动单元。 |
| dependencies/tests | 原 Python 主线 stdlib-only；发现功能使用 optional requirements。原 Python pytest、Rust kernel gates 全量执行；SwiftUI 原生测试需要 macOS，本机未运行。 |

架构顺序：

```text
existing HostIdentity + local Ed25519 key
  → untrusted DNS-SD candidate (address/TTL only)
  → DeviceTransport selection (v1 LAN IPv4 TLS)
  → PAKE enrollment / pinned mutual-TLS authentication
  → capabilities=[]; authority_granted=false; authorization_required=true
  → existing independently authorized routing/execution boundary
  → presence/session authentication readback and reconnect
```

这里的 DeviceSession 是认证结果快照，TLS 探测连接随后关闭，不是可执行命令的
持久 MCP/provider 会话。远程执行仍通过现有授权流程与 local/SSH 手动路径。
未实现 Tailscale、Direct-IP、Relay 适配器；接口为它们保留，未做公网 relay/mesh。

## CLI 使用

使用独立 Python 环境安装可选依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-discovery-dev.txt
export CLINX_PYTHON="$PWD/.venv/bin/python"
export PATH="$PWD/bin:$PATH"
clinx devices
```

如果系统缺少 ensurepip，可使用机器级 `uv venv` / `uv pip`。
本次 Host runner 的受限解释器路径使我们采用 worktree 内的
`.discovery-deps312` 依赖目录，以 Python 3.12 验证；没有安装全局 Python 包。

每台机器继续使用它自己的 runtime_host 配置，不应将 P620 的 bridge.toml
原样复制为 AIR/iMac 的身份配置。高级选项 `--config` 可选择已有本机配置；
首次无配置时使用本机检测名称并持久化。hostname/IP/port/token 均不出现在普通配对步骤中。

开发模式验证的最短交互路径：

```bash
# 接受方：只在本机 TTY 显示一次验证码；进程继续提供重连
clinx pair accept --allow-dev-pairing

# 发起方：选择发现的设备，输入对方屏幕上的四位码
clinx pair --allow-dev-pairing

# 下次启动无需验证码；开发信任仍需显式 development opt-in
clinx serve --allow-dev-pairing
clinx devices --allow-dev-pairing
```

没有 development opt-in 时：
`clinx devices` 仍可发现设备、展示持久化状态；
新配对及使用 DEV_ONLY 信任均被安全门禁拒绝。
正常生产默认路径尚未开放，不能宣传为安全完成。

`devices` 显示 This Device、Online Trusted、Online Unpaired、Offline Trusted、
Identity Mismatch；JSON 额外显示 authenticated 和 security_status。
Online Trusted 表示已知信任记录和在线候选；authenticated=true 才表示本次 TLS
验证成功。网络超时不会删除持久化 trust，密钥/证书验证冲突会拒绝连接。
`--wait`、`--state-dir`、`--port` 是高级选项。PIN 不接受 argv、管道或 JSON 输入。
现有 `doctor`、`tasks --host`、`onboard-thread`、local/SSH endpoint 配置不变。

## 实现细节与安全状态

### LAN_DISCOVERY

DNS-SD 类型为 `_clinx._tcp.local.`（尾点是规范 DNS 表示）。
TXT 严格只允许 node_id、display_name、protocol_version、fingerprint。
任何 PIN、token、authority、endpoint 扩展字段被拒绝。
按 node_id 聚合不同地址/服务实例；同 ID 的不同 fingerprint 显示冲突并拒绝使用。
缓存有容量上限、TTL 过期和 goodbye 下线；读取实际 DNS 剩余 TTL，
处理 zeroconf 对相同内容刷新不产生更新回调的行为。

v1 默认仅私网 IPv4 实体接口；排除 loopback、Docker bridge、veth、Tailscale、
tun/tap 等虚拟接口，最多 16 个地址。无私网接口时仍能显示离线信任状态。
同一私网不是跨 VLAN 自动可达保证；未实现 IPv6 或接口热插拔后的自动重建。

### PAIRING

- `secrets.randbelow(10000)` 生成四位码，仅在内存中保留。
- 60 秒有效、一窗一次成功、最多三次失败、每秒至多一次 admission、
  每分钟至多打开一个窗口；失败、超时、断线均不产生 trust。
- 单窗口锁原子预留；并发请求返回 busy；服务 state 排他锁拒绝双 daemon。
- 校验成功后先消费窗口，再原子写 trust；磁盘失败不重新开放 PIN。
- 每次请求创建全新 SPAKE2 状态。双方身份、公钥、服务/版本、窗口 nonce
  纳入 SPAKE2 identity strings；客户端绑定实际 TLS server certificate 的公钥。
- key confirmation 使用上游文档的 HKDF 方法和不同 A/B 标签，常数时间比较确认值。
- 旧消息/窗口、错误 PIN、身份替换均失败；无静态 PIN verifier 写盘。
- 有线协议帧大小、连接并发、TLS handshake 和每帧总时限均有上限。
- SECURITY_BLOCKER：非 constant-time backend、协议组合未经独立安全审查。
  Python 对象内存无法保证秘密立即物理清零，未声称具有此能力。

### TRUST_STORE

`identity.json` 保存 schema_version=1、本机 HostIdentity 绑定、Ed25519 私钥和自签名证书；
`peers.json` 保存 peer node_id、公钥、SHA-256 fingerprint、友好名称、
配对/更新/认证时间、trust_state、security_status 及公开证书。
无 PIN、派生 PAKE key、provider token 或执行 authority 字段。

state 目录要求 0700、文件 0600、当前用户所有；拒绝符号链接和宽松权限。
使用 flock + 同目录临时文件 + fsync + atomic replace；未知 schema/corrupt state
拒绝载入，不自动重置身份。重复 key 对应另一 node_id、key rotation、
revoked trust 均拒绝隐式覆盖。后台重启只恢复长期身份/trust，不恢复配对窗口。

### AUTO_RECONNECT / AUTHORIZATION_ISOLATION

重新发现可信节点后，使用 pinned self-signed Ed25519 certificate 做 TLS 1.3 双向认证；
不再需要 PIN。客户端验证服务器密钥，服务端验证客户端证书并再次核对持久化 peer。
双向均有 key mismatch fail-closed 与撤销检查。serve 前台 runtime 周期重连；
devices 也执行一次已信任节点认证。

返回的 capability 集合为空、authority_granted=false。
即使客户端已经配对并完成 mutual TLS，listener 也拒绝 host/terminal/repo/production/business
操作；没有 TaskRegistry、HostExecutor 或公共 MCP handler 的 dispatch 路径。
业务能力协商/远程执行的后续产品集成不在此开发 listener 中隐式启用。

## TEST_RESULTS

本次实际检查结果：

| 检查 | 结果 |
| --- | --- |
| discovery 专项（含 P620 实际 LAN 两项） | 45 passed |
| 全仓 pytest，CLINX_LAN_SELFTEST=1 | 783 passed、5 failed、5 skipped，112 subtests passed |
| 原始 BASE_HEAD 导出后的 Monitor UI 静态测试 | 13 passed、5 failed；与本次全仓失败集合一致 |
| 新模块 Ruff lint/format | PASS，6 个模块和专项测试文件 |
| 新模块 mypy，check_untyped_defs | PASS，6 个 source files |
| Rust release build / fmt / check / clippy -D warnings | PASS |
| Rust unit/frame tests | 7 passed |
| git diff --check | PASS |
| macOS SwiftUI native tests | NOT_RUN；P620 没有 Swift/macOS SDK |
| opt-in live Provider/Host lifecycle tests | 5 skipped；未创建其他 managed execution |
| AIR/iMac 跨机验证 | 未验证 |

五个基线失败：search field toolbar、210px width、content column first row、
40pt bottom inset、synthetic purple window edge。测试及相关 MonitorApp 源码均未修改。

P620 首次实际组播自检失败，原因是全部虚拟网卡导致 IGMP membership
限制及过多广播地址；修复默认接口选择后真实自检通过。未修改 sysctl 或网络配置。
通过的第二项 LAN 自检使用两个临时 node_id 和两套独立 zeroconf sockets：
互相发现 → 真实开发模式 SPAKE2 → 双向 TLS 重连 → 退出清理临时 listener/广播。
这是同机 LAN 栈证据，绝不是跨机 PASS。

主要验证日志保存在 worktree 的 ignored `.validation/`：
`discovery-all.log`、`final-full-pytest.log`、`baseline-ui.log`、
`cargo-0.log` 至 `cargo-4.log`。失败的早期日志同样保留。
Python 全仓旧 fixture 要求短且非 Git 的临时路径，因此最终使用标准 /tmp fixture；
所有代码、命令 cwd、构建目标和证据均显式指向此 worktree，未写入其他项目。

可复验命令：

```bash
python3 -m pytest -q test_local_discovery.py
CLINX_LAN_SELFTEST=1 python3 -m pytest -q
ruff check --config ruff-discovery.toml local_discovery test_local_discovery.py
ruff format --check --config ruff-discovery.toml local_discovery test_local_discovery.py
mypy --config-file mypy-discovery.ini
cargo build --release --locked --manifest-path kernel/Cargo.toml
cargo fmt --manifest-path kernel/Cargo.toml --all -- --check
cargo check --locked --manifest-path kernel/Cargo.toml
cargo test --locked --manifest-path kernel/Cargo.toml
cargo clippy --manifest-path kernel/Cargo.toml --all-targets --all-features --locked -- -D warnings
```

## 工作区保护

CANONICAL_WORKTREE_PRESERVED=本轮没有写入 canonical 文件；
前后 HEAD 均为 788330293350a80b036dd600ee0ec6bc9679a239，canonical index hash 一致。
canonical 的 dirty status 和部分内容 hash 在本轮期间发生并发变化。
因此不能宣称其 dirty 前后字节完全相同；已保留现场，没有覆盖、暂存或恢复这些变化。
用户已说明该目录中有另一个 Writer 修复任务；本轮唯一写入目标是开发 worktree。

PILOT_REPO_PRESERVED=PASS：HEAD、status、index、全部已跟踪/未忽略未跟踪文件
hash 前后一致。

没有 reset --hard、clean、stash、push、merge、生产 mutation、ORION 修改，
没有重启公共 daemon。Git 本地提交仅属于 codex/local-discovery-v1。

## REMAINING_RISKS / NEXT_STEP

1. 必须接入并评审可靠 constant-time PAKE backend，并独立审查 transcript/
   TLS identity binding、confirmation、重放和 rate-limit 威胁模型。
   当前 DEV_ONLY trust 不应自动升级为 production trust；需要显式重新配对策略。
2. 完成 AIR/iMac 安装升级、隔离双机交互、地址变化、网络中断、睡眠恢复与防火墙验证。
3. 当前是前台设备 runtime，未集成公共服务自动启动；认证快照不授予/转发执行能力。
4. IPv6、网卡热插拔、证书到期更新、人工 key rotation/revocation UX 尚需后续产品工作。
5. 固定基线的五项 UI 失败由独立 UI 工作处理；不能将它们记为 PASS。

NEXT_STEP=对本地分支进行代码/安全审查，先解除 PAKE SECURITY_BLOCKER，
然后做跨机验收；本轮不发布、不合并、不激活生产服务。