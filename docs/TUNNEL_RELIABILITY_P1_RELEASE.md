> 文档分类：HISTORICAL。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# P1 最小发布与 CAS 回退准备

状态：PREPARED_AND_OFFLINE_QUALIFIED / NOT_DEPLOYED。本次不授予生产激活权限。
原始长轮询根因仍 UNKNOWN；P2 执行内核契约冻结与 P3 Rust Runtime 原型未实施。

## 制品与配置契约

`scripts/build_tunnel_release.py` 仅读取工作区和当前安装，在新的任务 `/data/artifacts`
目录构建确定性 gzip/tar。路径排序、文件模式、UID/GID、mtime、gzip header 均固定。
内容身份包含 HEAD、逐项 dirty 源码 SHA、Python/Tunnel 二进制 SHA、复用证据 SHA。
相同输入两次构建应产生相同 release、manifest、回退 capsule 和 plan 哈希。

最小 runtime 是未改动、已隔离验收的 `tunnel_reliability.py`，只依赖现存
`/usr/bin/python3.12` 标准库；没有复制整个 CLINX、venv、MCP、Owner 或 Provider。
7 个归档成员为 supervisor、CAS 工具、policy、3 个配置文件和 manifest。
依赖使用原位置，二进制哈希绑定并作为 CAS guard；并非自包含 OS 镜像。

配置切换白名单恰为三项：

1. `~/.config/systemd/user/clinx-tunnel.service`：完整 replacement unit，无 PartOf。
2. `~/.config/systemd/user/clinx-tunnel-health.service`：完整只读 `status` unit。
3. `~/.config/systemd/user/clinx-tunnel.service.d/99-p1-reliability.conf`：最终监督配置，
   覆盖既有 90/95 的 Restart 设置；不修改 provider.conf。

旧 `PartOf=clinx.service` 必须从完整 unit 源移除，不能靠 drop-in 空赋值消除。
canonical legacy unit 补上现有 provider Wants/After；P1 显式启用使用 `.service.in`
渲染的完整 unit。历史 `.conf.example` 单独安装不足以构成发布方案。
health 与新 supervisor MainPID 成对迁移，继续使用既有60s timer，不调用旧 MainPID
健康路径、不自动启动消费者。生产旧 unit/launcher 均未修改。

`systemd/tunnel-reliability-policy.json` 将候选参数显式渲染到 ExecStart：
stale120、startup45、interval5、probe3、stop5、cooldown10、failure_grace15、
max_restarts3（共4代）、stable300秒且poll推进。预算持久化，HOLD退出78，
`RestartPreventExitStatus=78` 防止绕过；本工件不清理 state 或重置预算。
219s恢复／370s HOLD为既有模型条件下的保守逻辑界限，不是已测生产SLO。
生产SLO与预算批准仍OPEN，云端真实派发验收NOT_PROVEN。

## 当前安装 CAS 与回退边界

capsule保存上述三项的 before/after 内容、模式和原先不存在的事实；其他运行入口、
profile/凭据仅保留哈希guard，**不复制凭据**。drop-in目录新增条目也会拒绝。
回退只接受精确 after 状态；篡改blob、内容、权限、依赖或混合切换状态均fail closed。
没有数据库/lease/terminal事实写入、Provider RPC、turn replay、service stop或reload。

`scripts/tunnel_release_cas.py check --root /` 是只读配置匹配检查，不能代替准入。
写入CLI只允许带专用marker的 `/data/artifacts` fixture，拒绝生产根目录。
因此该capsule与离线CAS引擎已经可以供审查和精确回退资格验证，但**并不提供可在
本轮执行的生产安装/回退driver**。下一轮独立授权后，driver必须先证明排空、消费者
隔离、停止窗口与文件CAS均成立，再消费同一plan。不能将离线PASS当作生产rollback演练。

每个文件用fsync/rename替换；多文件整体不是crash-atomic transaction。
部分写入会拒绝双向普通CAS，必须在不reload、不启动消费者的状态下人工审查
逐项before/after事实，不能运行宽松恢复或强制覆盖。

## 构建与新验证

以本线程证据目录为例（输出必须尚不存在）：

```sh
python3 scripts/build_tunnel_release.py \
  --output /data/artifacts/<task>/candidate \
  --guard-manifest /data/artifacts/<task>/runtime-guards-before.json \
  --evidence-manifest /data/artifacts/<task>/reused-evidence.json
python3 scripts/tunnel_release_cas.py check \
  --capsule /data/artifacts/<task>/candidate/rollback-capsule \
  --root / --direction forward
python3 -m pytest -q tests/test_tunnel_release.py \
  --basetemp=/data/artifacts/<task>/cas-unit-fixtures
ruff check --config ruff-discovery.toml \
  scripts/build_tunnel_release.py scripts/tunnel_release_cas.py tests/test_tunnel_release.py
```

本轮17项新测试验证精确往返、过期/权限/guard/新增drop-in/缺失文件拒绝、
symlink逃逸、损坏回退blob、生产根目录拒绝、禁写数据库/预算范围、部分切换fail closed、
policy与既有运行默认值一致。另用精确生产配置副本（依赖只读原位核验，不复制凭据）
验证forward→rollback全部恢复，并验证stale rollback零写入；静态systemd verify通过。
双次构建相同。没有重复已有Tunnel故障注入或132项历史回归。

## 消费者归属及未关闭准入

只读证据确认Tunnel PID1306037直接持有Codex app-server1306059/1306068的三条
stdio管道；启动日志对应内嵌 `codexappserver.NewBridge`。它们与MCP1306056共用
PGID/session1306037及Tunnel cgroup，属于同一回收边界。独立Owner1306166与
Provider的socket peer666683/cgroup，以及Desktop endpoint peer2191758/session scope
均在该边界外。这解释额外进程的来源，不证明内嵌Bridge没有有效thread/turn。

对该Bridge的活跃工作未取得权威idle证明，因此消费者安全回收仍BLOCKED。
只允许未来在admission排空、精确确认Bridge工作与Owner/Provider独立后回收同一
消费者；不逐个kill/adopt，也不把共享 `.codex` 路径当成权威归属证明。

本轮仍见ORION、HDL和本CLINX执行的3个活跃execution/lease。历史3个UNKNOWN在
execution_history已BLOCKED并released，failure_code分别NATIVE_ACTIVE_OWNER、
TURN_NOT_DISPATCHED、NATIVE_SHARED_WRITER_NOT_QUALIFIED；ledger事实保留，需独立
精确native复核，不能因stage或turn_id为空就推断安全重发。4个PENDING中3个对应
当前工作等待terminal，另1个terminal_observed=1、attempts24为历史交付待对账。
这些分类未修改任何事实，也没有将活跃执行伪装成已排空。

下一轮激活前仍需：本任务也结束后的权威排空、内嵌Bridge idle证明、历史不确定状态
的明确处置边界、fresh一致备份、候选预算/SLO批准、当前CAS重新匹配，以及独立
生产激活授权。即使准入以后全PASS，本轮也没有生产切换许可。
