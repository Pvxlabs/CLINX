# CLINX Local Discovery v1：生产 PAKE 配对收口报告

日期：2026-10-02。范围：隔离开发实现、源码审查、P620 同机验证和安装路径。未部署公共服务。

## 结论与精确范围

| 项目 | 结论与证据范围 |
| --- | --- |
| IMPLEMENTATION | PASS：生产默认路径接入 OPAQUE；保留显式 DEV_ONLY；CLI、窗口、持久化及重连完成专项验证。 |
| PRODUCTION_PAKE_BACKEND | PASS：固定 `opaque-ke=4.0.1`，RFC 9807 OPAQUE-3DH；Linux 原生 wheel 已构建、安装、运行。 |
| CONSTANT_TIME_EVIDENCE | PASS（有范围限制）：核实公开审计中的 MAC/反射修复、当前协议代码的常数时间比较及所用曲线实现依据；不是当前全部机器码、Python/TLS 栈或 arm64 的无侧信道证明。 |
| PROTOCOL_REVIEW | PASS（本轮源码审查和攻击用例）；当前版本及 CLINX 组合的独立安全审计：未验证。2021 年审计不能覆盖本次集成。 |
| IDENTITY_BINDING | PASS：双方角色、版本、node_id、公钥、fingerprint、窗口及会话绑定；PAKE、trust store、TLS 使用同一公钥身份。 |
| AUTHORIZATION_ISOLATION | PASS：所有 session 返回空 capabilities、authority_granted=false、authorization_required=true；已认证设备仍不能执行命令。 |
| PRODUCTION_FAIL_CLOSED | PASS：缺少/不匹配后端、未知协议、降级、未知持久化状态及身份冲突均拒绝；没有生产到 DEV 的 fallback。 |
| P620_REAL_PAIRING | PASS：独立临时双进程，真实 mDNS → 生产 OPAQUE → 持久化可信身份。 |
| P620_REAL_RECONNECT | PASS：双方进程退出并重建 → 重新发现 → 无 PIN 双向 mTLS；实际错误服务器公钥拒绝。 |
| REAL_DEVICE_VALIDATION | 未验证：AIR/iMac 跨机、macOS arm64 编译/运行、防火墙、睡眠恢复。P620 结果不能替代这些验收。 |
| TEST_RESULTS | 专项与原生适配层通过；全仓存在 5 项既有 Monitor UI 失败，详细数量和命令见下文，不能记全仓 PASS。 |
| REMAINING_RISKS | 独立安全审计、arm64 侧信道/运行资格、跨机验收未完成；低熵 PIN 在线猜测和内存残留边界仍存在。 |

- PROJECT=`clinx-local-discovery`
- WORKTREE=`/home/pvxlabs/dev/CLINX-worktrees/local-discovery-v1`
- BRANCH=`codex/local-discovery-v1`
- 本轮起点 HEAD=`1b7e29f874b75983c2c8bb98f727984ff4439a7e`；起始工作区干净，实际 Host cwd、分支和 origin 已核对。
- ORIGIN=`git@github.com:Pvxlabs/CLINX.git`
- 控制面 `38631af027daf27f30af2a58204781d989a99aad` 未合入功能分支。最终本地提交 HEAD 由交付结果及 Git readback 给出，避免报告自引用提交哈希。
- 本轮只使用已绑定 Host execution。未借用 pilot，未修改 execution 数据库/lease，未新建 managed execution，未 push/merge，未重启公共 daemon，未触碰 ORION 或交易。

## 后端选择与官方证据

选择一个已有可行候选后直接实现，没有无限扩大候选列表，也没有自行实现曲线、OPRF、KDF 或确认 MAC。

| 候选 | 判断 |
| --- | --- |
| `python-spake2==0.9` | 官方明确不保证 constant-time；仅保留 DEV_ONLY，移出生产 requirements。不能作为生产方案。 |
| `opaque-ke==4.0.1` | 官方当前稳定版本，未撤回；基于 RFC 9807，内置 KE2/KE3 双向确认；Rust、MIT 或 Apache-2.0 双许可证，原生 Linux/macOS 构建路径明确。选择此方案。 |

2026-10-02 查询 crates.io 官方 API 得到稳定版本 4.0.1，最新预发行 4.1.0-pre.2。没有采用预发行。4.0.1 的发布记录为 2025-11-03；随包 CHANGELOG 标题为 2025-10-30。4.0.0 同步 RFC 9807，并修正 dummy record 生成时序问题；4.0.1 修正文档构建。最小 Rust 版本按包元数据为 1.85，本机实际使用 rustc 1.98.1。

依据：

1. [opaque-ke 官方仓库](https://github.com/facebook/opaque-ke)、[固定 4.0.1 文档](https://docs.rs/opaque-ke/4.0.1/opaque_ke/)、[crates.io 版本 API](https://crates.io/api/v1/crates/opaque-ke)。
2. [RFC 9807](https://www.rfc-editor.org/rfc/rfc9807.html)：OPAQUE-3DH、身份标识、应用 context、KE1/KE2/KE3 和相互认证。尤其参见 6.4.2.1 的 transcript/context。
3. [NCC Group 官方报告页面](https://www.nccgroup.com/research/public-report-whatsapp-opaque-ke-cryptographic-implementation-review/)、[完整 PDF](https://www.nccgroup.com/media/0uspzge5/_ncc_group_whatsappllc_opaque_report_2021-12-10_v13.pdf)：2021 年审查 v0.5.0，复核修复进入 v1.2.0；第 11 页记录 3DH transcript MAC 非常数时间比较及修复，第 8–9 页记录反射问题及修复。所有这些结论均有历史版本范围，**不能称 4.0.1 或 CLINX 已独立审计**。
4. 随固定 crate 核实 `src/key_exchange/tripledh.rs`：客户端 `Mac::verify`，服务端 `expected_mac.ct_eq`；`src/opaque.rs` 对 OPRF 反射元素使用 `ct_eq` 并返回 `ReflectedValueError`。测试实际构造反射元素和篡改 MAC。
5. [curve25519-dalek 4.1.3](https://docs.rs/crate/curve25519-dalek/4.1.3)：README 声明除显式 variable-time API 外采用无秘密相关分支/访存的逻辑，并对 x86_64 机器码表达信心。本适配层使用 opaque-ke 的 Ristretto 普通乘法路径，没有调用 variable-time API。这只是完整证据的一部分。
6. [python-spake2 的安全声明](https://github.com/warner/python-spake2#security)仍适用于开发后端。

原始官方响应、固定 crate 的 README/CHANGELOG、NCC PDF/文本及 RFC 保存在 ignored `.validation/pake-research/`。早期 raw.githubusercontent.com 请求连接失败后改用官方 crates.io 发布包核实，没有把网络失败报告为依赖阻塞。

固定 crate SHA-256：
`ded22991b43cd15561b62b2e1cf9ace1344a8534eebec96202d5c96a77a6616a`。

## 协议与实现审查

生产协议标识固定为 `OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1`。密码套件：OPRF Ristretto255/SHA-512，3DH Ristretto255/SHA-512，Argon2i v0x13、65536 KiB、3 次迭代、1 lane。Argon2i 采用数据无关访存；参数是协议 v1 的固定组成，不接受对端参数。它不增加四位码的熵，也不替代在线尝试预算。

`native/opaque_pairing` 是窄 PyO3 ABI3 适配层：

- 接受内存 PIN、规范化 context 和角色身份字节；只输出库规定的 KE1/KE2/KE3。
- 不自行计算确认 MAC，不把 session/export key 返回 Python，不序列化秘密，不读写文件或网络，不提供执行入口。
- 接受节点已知它本地显示的临时 PIN，因此在本地内存中完成标准 OPAQUE registration；远端不能上传 registration record。随后远端只运行标准 login。
- 每次尝试均使用新的 OS 随机数、setup、registration record 和 login 状态。它们仅服务于当前保留槽，不写磁盘。这里没有声称获得“服务器不知道 PIN”的 aPAKE 特性：显示码的节点本来就知道 PIN。
- Python/native 对象只允许一次 start/finish；错误或畸形 finish 同样消费 native 状态。Rust 持有的 PIN 用 Zeroizing 包装；成功产生的未使用 session/export key 被清除。Python 及底层库的所有临时副本不在可证明清零范围内。

有线顺序：

1. 客户端通过未预先信任的 TLS 连接发送精确生产协议标识、本机 public identity、证书、128 位 client_nonce；不发送 PIN。
2. 服务端从本地窗口原子 reserve，取得 window_id 和每次新生成的 session_id，验证客户端证书公钥与 identity 相同，然后返回自身 identity、window_id、session_id。
3. 客户端确认服务端 node_id 与所选发现候选相同，公钥与当前实际 TLS 证书一致。mDNS fingerprint 只是候选筛选，最终身份由 PAKE 认证。
4. 双方将规范 JSON 作为 RFC context：服务名、配对 transcript version=2、精确套件标识、双方完整 public identity、window_id、session_id、client_nonce。RFC Identifiers 分别使用明确的 initiator/acceptor 标签和身份。发现/持久化 public identity 的 protocol_version 仍为 1；这两个版本字段用途不同。
5. KE1 → KE2 → KE3 全部由 opaque-ke 生成和验证。KE2 验证服务端，KE3 验证客户端；各阶段精确核对生产协议标识，不协商降级。
6. 服务端验证 KE3 后消费窗口，再原子写 trust，最后通过已被 PAKE 绑定身份的 TLS 发送提交确认。客户端确认后原子写相同公钥 trust，再建立新 mTLS 连接验证持久化结果。
7. 此 TLS 确认只是提交结果，不是自制 PAKE MAC。没有发明额外密码协议或替换库的双向确认。

绑定审查沿 `pairing.py → transport.py → identity.py → mTLS` 核实：

- transcript 中的公钥/fingerprint 与写入的 peer 完全相同；证书与 peer 公钥一致。
- reconnect 客户端加载已固定的 server certificate 并再次检查公钥；服务端检查 client certificate 后将其公钥与请求 node_id 对应的 trust 比较。
- TLS 两端都拒绝错误密钥、撤销身份；同一公钥不能绑定第二个 node_id。
- 反射、角色交换、跨节点/跨会话/跨窗口/nonce 重放、换公钥、换版本、未知协议及 key mismatch 有明确测试。

这不是跨设备分布式原子提交：如果服务端落盘后网络断开，可能只一端保有 trust。此时返回失败，不自动恢复 PIN 或降低信任等级；需要本地重新开启新窗口完成相同身份的安全配对。该边界是剩余恢复 UX 工作。

## 窗口、秘密生命周期及信任迁移

- 本地 TTY 执行 `clinx pair accept` 才开启正常用户路径；listener 没有网络“开启窗口”指令。
- 四位十进制码由 `secrets.randbelow(10000)` 生成。60 秒、一次成功、最多 3 次失败、每秒最多一次 admission、每分钟最多打开一次窗口。
- 整个服务节点共享一个 PairingWindow，持有 state 排他锁。更换来源身份、重新 TLS 连接、创建 native/session 状态都不能重置失败预算。并发 reserve 返回 busy；并发成功确认只产生一次提交。
- 断开/错误/畸形已接纳请求消耗预算；超时或磁盘提交失败不会重开 PIN。16 个连接槽、TLS/帧超时和 16 KiB 帧上限继续生效。
- 空闲窗口也有定时器撤销 PIN 引用；成功、三次失败、关闭服务亦撤销。最终提交始终重新检查单调时钟 60 秒截止时间，不能依靠定时线程调度延迟越过截止。
- Python 字符串、FFI 转换、副本、allocator、swap/core dump 不能保证立即物理清零。只声明引用生命周期和 native 显式清理，不宣称整进程无秘密残留。
- PIN 不接受 argv/JSON/非 TTY 输入，不进入日志、异常文本、telemetry、mDNS、trust 文件或构建制品。真实闭环测试用私有进程内存管道协调临时代码，不写入证据。
- 长期 Ed25519 身份按既有合同保存在受限 `identity.json` 私有凭据存储（目录 0700、文件 0600），这是持久身份的必要存储；不进入报告、日志或 wheel。TLS 加载已改为有界非阻塞进程管道经 `/dev/fd` 读取，不再生成临时 PEM 磁盘文件。Linux 实测通过，macOS 该路径仍待实测。
- 本地凭据存储采用 flock、fsync、atomic replace、schema 校验及权限/符号链接拒绝。新 production peer 状态为 `OPAQUE_V1`，并保存精确 `pairing_protocol`。
- 旧 `DEV_ONLY` 记录默认不能重连；CLI 会要求一次新的生产配对，不改标签冒充升级。生产 trust 不可降级为 DEV。旧通用 `QUALIFIED` 或未知状态拒绝，避免将没有协议来源的记录当作生产信任。
- `requirements-discovery.txt` 不再安装 spake2；它仅在开发 requirements 内。生产不导入 spake2，原生库缺失/版本不同/ABI 加载失败则拒绝配对。设备发现仍可工作，JSON 明确显示 `BACKEND_UNAVAILABLE`，不伪报后端就绪。
- 已有可信身份的 mTLS 重连不需要重新运行 PAKE；PAKE 库用于新配对，TLS/信任校验仍然必需。

## 授权边界

Trusted Device 只表示设备密钥认证。它不授予 shell、SSH、Host Executor、仓库写入、生产或交易权限。

listener 只处理配对和身份 session；包括已完成 mTLS 的客户端，发送 host_execute、shell、ssh、terminal、repo_write、production_mutation、trade 也会被拒绝。没有接入 TaskRegistry、HostExecutor、公共 MCP dispatch 或 provider 执行入口。原有 CLINX routing/capability/execution policy 和高级 local/SSH 手动连接路径继续独立决定授权。

本轮未添加 GUI、mesh、relay、IPv6、网卡热插拔、公共自动启动、远程执行授权流程。

## 依赖锁定与安装

新增直接 Rust 依赖精确固定：

| 包 | 版本 |
| --- | --- |
| opaque-ke | 4.0.1 |
| pyo3 | 0.27.2，abi3-py311 |
| sha2 | 0.10.9 |
| zeroize | 1.8.2 |
| rand | 0.8.8，OS getrandom |
| 构建后端 maturin | 1.9.6 |

Cargo.lock 进一步锁定所有传递依赖及 registry checksum，包括 curve25519-dalek 4.1.3、voprf 0.5.0、argon2 0.5.3、hmac 0.12.1、subtle 2.6.1。构建使用 `--locked`，不修改 kernel 的 Cargo 文件。

现有 Python 依赖仍为 cryptography 50.0.2、zeroconf 0.151.5、ifaddr 0.2.0。开发测试另含 spake2 0.9、pytest 9.1.1、pytest-subtests 0.15.0、ruff 0.16.10、mypy 2.4.0。

本轮 artifact：

- Cargo.lock SHA-256：`b073c257279c233cc771dd2f1f298c896a01b0e35fd647e97853ff673b3c8e6e`
- Rust 适配层源码 SHA-256：`02c194146d52ea19c03957e880d3cff1b5a6f531967eaaf3792055beca023fc3`
- wheel：`.validation/wheels/clinx_opaque-0.1.0-cp311-abi3-manylinux_2_34_x86_64.whl`
- wheel SHA-256：`e75ffeaf4425393d5e4621d6efbd949e8b115799689dc62e27ae10c7f4d44316`
- wheel 安装到本 worktree 的 `.validation/opaque-wheel-env`，最终验证确认从此安装位置 import，而非手工复制的早期测试库。

常规 Linux/macOS 安装路径（在功能 checkout 中，已有独立环境则复用）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-discovery.txt maturin==1.9.6
.venv/bin/maturin build --release --locked --manifest-path native/opaque_pairing/Cargo.toml --out .validation/wheels
.venv/bin/python -m pip install .validation/wheels/clinx_opaque-0.1.0-*.whl
export CLINX_PYTHON="$PWD/.venv/bin/python"
export PATH="$PWD/bin:$PATH"
clinx pair accept
# 另一台设备：
clinx pair
# 后续无需 PIN：
clinx serve
clinx devices
```

每台机器保留自己的 runtime_host/身份配置，不能复制 P620 的私钥/state/bridge.toml 来替代本机身份。

macOS arm64 的有依据路径是**在真实 Apple Silicon Mac 上本地构建**：

```bash
.venv/bin/maturin build --release --locked --target aarch64-apple-darwin --manifest-path native/opaque_pairing/Cargo.toml --out .validation/wheels
```

需要 Python >=3.11、Rust >=1.85、Xcode Command Line Tools/Apple SDK。安装生成的 macOS arm64 ABI3 wheel 后运行专项和双机验收。[Rust 官方目标说明](https://doc.rust-lang.org/rustc/platform-support/apple-darwin.html)列出 aarch64-apple-darwin 为 Tier 1、arm64 最低 macOS 11；[maturin 官方 distribution 文档](https://www.maturin.rs/distribution.html)说明 wheel 和 --target 构建路径。ABI3 简化 Python 版本兼容，不代表 CPU 架构兼容；P620 的 Linux wheel 不可用于 Mac。本轮没有交叉编译或真实 Mac 编译/运行，不能标 PASS。

## 实际验证与命令

本轮复用 `.discovery-deps312`，没有全局安装 Python 包或重建整套环境。使用当前 Python 3.12.3 及声明的测试依赖。Rust test 因系统 Python 没有 libpython3.12 链接库，改用**已有** .venv 所指的 CPython 3.11.16 libpython 运行嵌入测试；没有为此安装系统工具链。

以下为最终工作树的验证结果；专项数量由最终全仓运行中的该功能测试集合确认。原始失败日志保留。

| 检查 | 结果 |
| --- | --- |
| 生产 OPAQUE 专项 | 31 passed（最终全仓运行）；此前独立专项 30 passed，新增后端状态用例随后由全仓覆盖 |
| 原有 discovery 专项，含 opt-in LAN | 45 passed（最终全仓运行）；早期未开启 LAN 的独立运行 43 passed、2 skipped |
| 全仓 pytest，CLINX_LAN_SELFTEST=1 | 814 passed、5 failed、5 skipped、112 subtests passed；49.35 秒；5 个失败均为既有 Monitor UI |
| Rust 适配层 release tests | 3 passed |
| Rust release wheel build、fmt、clippy -D warnings | PASS |
| Python Ruff lint/format、mypy | PASS；mypy 6 个 source files |
| git diff --check | PASS |
| P620 真实双进程闭环 | PASS；最终安装的 wheel 再次完整通过 |
| AIR/iMac、macOS arm64、SwiftUI 原生构建 | 未验证/未运行 |

本轮实际命令（在登记 worktree 内，环境路径均为本功能隔离路径）：

```bash
export PYTHONPATH="$PWD/.validation/opaque-wheel-env:$PWD/.discovery-deps312"
export CARGO_HOME="$PWD/.cache/cargo"
export PATH="/home/pvxlabs/.cargo/bin:$PATH"

# 原生构建，构建工具按固定版本安装在 worktree
.discovery-deps312/bin/maturin build --release --locked --manifest-path native/opaque_pairing/Cargo.toml --out .validation/wheels
uv pip install --python /usr/bin/python3 --target .validation/opaque-wheel-env .validation/wheels/clinx_opaque-0.1.0-cp311-abi3-manylinux_2_34_x86_64.whl

# Rust 嵌入测试使用现有 Python 3.11 的开发链接库
PYO3_PYTHON="$PWD/.venv/bin/python" LD_LIBRARY_PATH=/home/pvxlabs/.local/share/uv/python/cpython-3.11.16-linux-x86_64-gnu/lib cargo test --release --locked --manifest-path native/opaque_pairing/Cargo.toml
cargo fmt --manifest-path native/opaque_pairing/Cargo.toml -- --check
PYO3_PYTHON="$PWD/.venv/bin/python" cargo clippy --locked --manifest-path native/opaque_pairing/Cargo.toml --all-targets -- -D warnings

python3 -m pytest -q test_local_discovery.py
python3 -m pytest -q test_local_discovery_production.py
CLINX_LAN_SELFTEST=1 python3 -m pytest -q
python3 scripts/local_discovery_smoke.py
python3 -m ruff check --config ruff-discovery.toml local_discovery test_local_discovery.py test_local_discovery_production.py scripts/local_discovery_smoke.py
python3 -m ruff format --check --config ruff-discovery.toml local_discovery test_local_discovery.py test_local_discovery_production.py scripts/local_discovery_smoke.py
python3 -m mypy --config-file mypy-discovery.ini
git diff --check
```

针对本次新模块执行 Rust 检查；kernel 未修改，不重复建立无关 kernel 门槛。专项覆盖正确/错误 PIN、精确 60 秒失效、空闲秘密引用撤销、全窗口预算、新身份/新连接预算、限速、并发成功只一次、重放、反射、身份/角色/协议绑定、持久化、DEV 不自动提升、依赖缺失、免 PIN 重连及零权限提升。

全仓既有五项失败仍属于 `test_monitor_finalized_ui.py`：

- test_search_field_is_out_of_the_toolbar
- test_search_field_width_matches_the_compact_pages
- test_search_field_is_the_first_row_of_the_content_column
- test_main_content_bottom_inset_is_forty_in_every_pane
- test_synthetic_semantics_are_still_distinct

这些测试及 MonitorApp 源码本轮未修改。5 个 opt-in live Provider/Host 生命周期测试未运行，不创建其他 execution。最终日志为 .validation/opaque-final-full.log、opaque-final-smoke.log；原生日志为 rust-adapter-tests.log、opaque-rust-clippy.log、opaque-wheel-package.log。旧 full-pytest、baseline-ui、cargo 和本次初次失败日志均保留于 .validation；旧 task 的失败历史没有修改。日志只反映各次实际命令，不把最终修复后的通过回写为早期通过。

## 剩余风险与审查边界

1. 当前 opaque-ke 4.0.1、PyO3 适配层、CLINX transcript/TLS/persistence 组合没有新的独立审计。公开历史审计、官方协议和攻击测试提供依据，不等同独立审计或全产品完成。
2. arm64 的编译、常数时间机器码及 macOS /dev/fd TLS 凭据加载路径未实测；在真实 Mac 验证前不宣称跨平台交付 PASS。
3. 四位码每个窗口最多三次在线猜测，理想均匀猜测成功概率至多 3/10000；攻击者仍可耗尽窗口造成拒绝服务。明确的本地开启、短时展示和人的设备选择仍是安全模型的一部分。
4. Python、FFI、密码库临时对象及操作系统内存残留没有全部清零证明；本地凭据文件依赖既有用户/文件系统保护。信任元数据抵抗网络攻击，不抵抗能修改本机程序和私有状态的同权限攻击者。
5. 分布式落盘和确认丢失的恢复需要新配对窗口；证书到期、人工 key rotation/revocation UX、跨 VLAN、地址变化后的真实设备体验仍需后续验收。
6. 全仓 5 项 Monitor UI 失败阻塞全仓 PASS；AIR/iMac 和独立安全审计未验证。此次状态应进入代码/安全审查，不自动激活公共服务。
