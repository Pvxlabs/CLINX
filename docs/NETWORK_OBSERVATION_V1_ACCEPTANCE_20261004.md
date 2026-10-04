# 全网观察 v1 候选接续与实机验收

给已有 Air/P620 操作渠道的外层执行者使用。当前 worker 已完成 P620 实现和隔离验证，**没有激活共享服务**。源码 SHA：1113952c8397ee97f1501f88b291cb6f0f921960；分支：codex/p620-network-observation-v1-20261004。交付 HEAD 与摘要以 .validation/network-observation/DELIVERY_MANIFEST.json 为准。

## 1. 候选入口

P620 证据目录 .validation/network-observation/：

- candidate-source.tar.gz：完整源码、测试和文档。
- evidence.tar.gz：本轮日志、身份、PROGRESS 与原始继承失败，不含节点身份/凭据/venv。
- DELIVERY_MANIFEST.json：交付 HEAD、产品源码 SHA、包 SHA256 和实际 push 读回。

隔离 runner 位于 evidence.tar.gz；将源包和证据包解压到同一候选目录即可保留报告中的 .validation 路径。Python 依赖按 requirements-discovery.txt 和 native/opaque_pairing 安装到候选自己的 venv；测试额外使用 pytest、pytest-subtests、jsonschema。

正常 fetch 本分支，在新隔离 checkout 构建，不切换 main 或原 Air 工作区：

~~~sh
git fetch origin codex/p620-network-observation-v1-20261004
git worktree add --detach /tmp/clinx-network-candidate origin/codex/p620-network-observation-v1-20261004
cd /tmp/clinx-network-candidate
git diff --check
~~~

代码与上述产品 SHA 应一致，后续提交仅增加文档。fixture 的 air/p620 字符串不是真实设备证据。

## 2. Air 原生构建

~~~sh
cd MonitorApp
swift test
sh Scripts/build-app.sh
~~~

制品：MonitorApp/.build/CLINX Monitor.app。使用既有签名和 Keychain，先保留候选而不覆盖安装版。缺编译、签名或屏幕能力时保存准确错误，不能记 PASS。

新增 NetworkObservationTests.swift 覆盖空 canonical 引用、缺结果头、异步切换和 Activity 身份；既有 Activity/滚动测试同跑。P620 上这些 Swift 测试只交付源码，未执行。

## 3. 串行准备配置与激活

沿用现有发布/切换机制，先记录旧 release、实际 ExecStart、Python、Observer URL 和授权摘要供回退。候选需要完整受信任包与项目 discovery/OPAQUE 依赖，不能只拷贝 observer_server.py。bin/clinx-observer 由 CLINX_OBSERVER_PYTHON 选择发布目录内独立运行时，保持 -I -B。

Centre、Observer、MCP 使用同一组核实路径：

| 配置 | 含义 |
| --- | --- |
| CLINX_OBSERVATION_DB | 独立观察库；centre 默认 node-centre state/observations.sqlite3 |
| CLINX_OBSERVATION_REGISTRY | 既有 NodeRegistry，不是 canonical TaskRegistry |
| CLINX_OBSERVATION_STATE | 既有中心身份、peers 和 sharing.json 目录 |
| CLINX_OBSERVATION_USER_SCOPE | 实际持久批准的用户映射，不按 Unix 用户名猜测 |
| CLINX_OBSERVATION_NODE_ID | 既有中心稳定 node_id |
| CLINX_OBSERVATION_NATIVE_HOME | 中心本机批准的只读 Codex 根 |
| CLINX_OBSERVER_DB / TOKEN / PORT | 沿用 v1 canonical DB、秘密注入及私有 listener |
| CLINX_OBSERVATION_CANONICAL_DB / OWNER_HOST | 本机采集的已有 canonical DB / 真实 owner |

正式 centre launcher 已接入观察库和本机 source。直接候选入口：

~~~sh
python3 node_centre_entrypoint.py --state "$candidate_node_state" --node-id "$candidate_node_id" serve \
  --registry "$candidate_node_registry" --observations "$candidate_observations" \
  --bind 127.0.0.1 --port "$candidate_centre_port" \
  --local-native-home "$candidate_native_home" --local-canonical-db "$candidate_canonical_db" \
  --local-owner-host "$candidate_owner_host"
~~~

变量来自外层已核实的候选配置；不创建替代身份。启用 source 不自动授权。本机如尚无明确观察批准，应在用户批准范围内通过正式入口持久化一次：

~~~sh
python3 node_centre_entrypoint.py --state "$candidate_node_state" --node-id "$candidate_node_id" share \
  --local-source --peer "$candidate_node_id" --user-scope "$candidate_user_scope" \
  --read-sessions --project "$approved_project_cwd" --approved
~~~

本机观察不自配对，也不允许 --execute-tasks。远端仍需双方 read_sessions 批准，不能为验收扩大项目/Provider/用户。

Air helper 使用安装包内 node_service_entrypoint.py，由既有 NodeServiceController 管理。保留 node-config 的中心、端点和控制字段，仅按既有批准计划补入 observation_canonical_db / observation_owner_host；原生未受管会话无需 canonical DB 或 adoption。configure-node 新参数可写这些设置，但调用时须保留原控制参数，不能用省略参数清空原配置。

~~~sh
python3 mcp_server.py --stdio --observations-only
CLINX_OBSERVER_PYTHON="$candidate_python" sh bin/clinx-observer
~~~

凭据由既有秘密配置注入，不写命令、报告或截图。systemd 只读挂载须覆盖候选包、独立依赖、观察库/NodeRegistry、授权状态和批准的 native history，不放宽整个 HOME 为可写。Mac 配置一次中心 Observer URL 后即可筛选全部设备。

外层确认候选及回退点后，再通过既有机制串行切换；当前 worker 没有执行该阶段。不要同时替换 centre、Provider 与活动 execution 的承载运行时。

## 4. 真实验收

1. 只读核对实际进程的 release SHA/Python，不能只看 Git HEAD。
2. 通过 clinx_list_observations 或 /v2/observations 自动枚举；不能先按 ID 手工灌入目录。真实 Air 原生会话 01a105a5-8667-79e3-afdb-6e6db6a97f65 必须自动出现，保持真实 THREAD_UNBOUND 和可空引用；禁止重跑、接管或修改历史。
3. 同一真实 Monitor 显示真实 P620/Air，对照后端 observation_id、设备、turn/execution、结果和新鲜度。
4. 验证设备/项目/状态/外部/受管筛选、短标题、详情切换、Activity/轮次/正文分页；旧记录滚动保持位置，无串内容。保存只含批准 Monitor 窗口的截图及对应后端读回时间/摘要。
5. 使用自然事实变化测量“源端可见 → Mac UI 呈现”p95，记录样本数、网络、CPU/RSS/能耗。隔离 HTTP p95=4.182 秒不能替代 UI 测量。
6. 在获批测试来源验证离线历史和恢复；未缓存内容应明确不可用。撤销测试只能用专用批准范围，不能撤销承载现有任务的节点。
7. iMac 没有实际渠道或未执行时保持未验证；实际控制动作仍需独立授权，不操纵已有任务测试观察路径。

使用现有 capture-window-geometry.sh 或指定窗口 ID 截图，保存 mac-global-list.png、mac-filters.png、mac-detail.png、mac-activity-history.png 与对应后端摘要。没有真实截图，MAC_GLOBAL_UI 就不记 PASS。

## 5. 回退与重建

外层将候选 App/服务 release 恢复到已记录旧版本，再只读验证有效进程；不删观察库、outbox、授权或历史，不迁移 Provider，不取消 execution。旧 v1 Observer 仍可读 canonical DB，新观察库不是执行权威。

普通重启保留数据库即可继续。索引 inode 重建公开 SOURCE_REBUILT；spool 丢失时保留旧证据，确认单采集者后使用新 spool，并以 observation_previous_stream / --observation-previous-stream 提供中心记录的旧 stream 做 CAS。不能自动替换仍活动的来源。中心投影完全丢失需从来源重新 bootstrap；源端已不存在的历史保持 gap，不能伪造恢复。

只有真实 Air/P620、Mac UI、必要激活与所声明设备覆盖都有证据后，才将全网 FINAL_STATUS 改为 PASS。
