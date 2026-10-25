# Codex 模型选择与错误诊断

用途：定义显式模型选择、推理级别映射和 Provider 错误投影。
适用范围：Rust Native Transport、Host Transport、Provider API 与 paired Node。
状态：CURRENT。验证：`native_transport`、`native_transport_qualification`、
`provider_api_qualification`、`provider_client_qualification`、`provider_runtime_qualification`
及独立客户端合成资格；构建/源码 PASS 不代表部署。

## 能力与参数

[App Server 官方契约](https://developers.openai.com/codex/app-server)使用 `model/list` 的
`data[].model`、`supportedReasoningEfforts[].reasoningEffort` 和 `defaultReasoningEffort`。
旧的仅 `id` fixture 保持兼容；有 `model` 时它才是可执行原生 ID，`displayName` 不发送。
[官方 Codex 模型页](https://developers.openai.com/codex/models)列出 `gpt-6-astra`，
但账号与客户端能力以目标 Provider 的实时清单为准，不根据网页推断当前账号可用性。

| 输入/边界 | 行为 |
| --- | --- |
| `model` | 精确匹配目标清单的原生 ID；拒绝显示名、后缀猜测、重复或隐藏项 |
| `reasoning_effort=extra_high` / `Extra High` | 准备时统一为 `xhigh`，发送字段为 `effort` |
| 原生 effort | 保留原值，验证它属于所选模型的能力；不硬编码通用等级清单 |
| effort 省略 | 继续省略，遵循 Provider 默认/继承设置；新线程与继续旧线程应分别理解 |
| effort 显式但 model 省略 | 返回 `PROVIDER_MODEL_REQUIRED`，不猜测继承模型 |
| 显式选择且清单不可用/畸形/分页不完整 | 本地失败，不创建线程、不发送输入、不降级 |
| model 和 effort 都省略 | 保留已有 Provider 默认/继承契约，不伪造能力清单 |

显式选择在 `native_create_take`、`thread/start` 和 `authorize_send` 之前验证。
清单最多 5 页、每页 100 项、总量 16 MiB，重复 cursor 明确失败。
旧 `resolve_model` 的显示别名兼容入口仍保留；实际发送入口再次验证原生 ID，不能靠别名绕过。
准备请求的原始命令哈希仍对应原请求；规范化后的 prepared 内容封存后不再修改。

## 诊断与恢复

`CLINX_PROVIDER_DIAGNOSTIC_V1` 只投影结构字段：稳定 `code`、`category`、
`request_stage`、数字 `rpc_code`、`provider_type`、`provider_code`、
`codex_error_info`、合法 `http_status` 和 `message_redacted`。
任意 message、additionalDetails、请求正文、headers、URL 和凭据不复制进公开诊断。
原生 terminal 证据仍按已有权威契约保存；公开 `execution_result.provider_error` 是脱敏投影。
paired Node 在既有 string-error envelope 内传递同一脱敏结构，不新增 Task writer。

分类包括 `INVALID_PARAMETERS`、`MODEL_UNAVAILABLE`、`AUTHENTICATION`、`NETWORK`、
`UPSTREAM_SERVICE`、`CAPABILITY_UNAVAILABLE` 和 `UNKNOWN`。
仅 HTTP 400 或通用错误文本不足以判定参数不合法；`upstream_error` 明确记录为上游服务类。
App Server 没有提供的 HTTP 状态或内部根因保留 null/UNKNOWN，不从消息猜测。
携带 full-access/network policy 的请求也不能把所有远端错误重命名为 sandbox 错误。

`turn/start` 失败或丢失 ACK 仍保留原 Authority 的 `SENDING` / delivery certainty `UNKNOWN`。
先按原 execution/thread/client-message identity 读历史、确认输入和终态；
不能因错误类别、断线或超时创建第二任务或重发输入。
能力失败只在 Authority 证明尚未发送的 `READY` / `TAKEN` 状态关闭并释放资源。

## 2026-10-25 资格证据与限制

本次原失败 thread 为 `01a173b1-9c69-75d1-8d87-8dbf75ab0614`，
turn 为 `01a173b1-9d50-7d00-9d2b-18be5814bbf5`。
只读 Provider 历史显示该 turn `failed`、`codexErrorInfo=other`，正文类别 `upstream_error`。
同一 turn 的本地 Codex 日志显示配置的 custom Provider `/responses` 返回 HTTP 400；
它已经越过本地 App Server 参数接收阶段。当前日志记录 effort 为 `extra_high`，
但没有网关内部的具体拒绝原因，不能仅凭此把所有 Upstream request failed 认定为参数错误。

当前 Codex CLI `0.164.0` 的实时清单中，Astra 原生 ID 为 `gpt-6-astra`，默认 `low`，
支持 `low/medium/high/xhigh/max/ultra`；这是本次客户端读回，不是永久配置或所有模型通用能力。
独立 `/data/artifacts` Authority 使用候选 Rust 派发链，依次运行无工具/无文件/无业务副作用合成任务：

| 模式 | 私有 execution | 精确 Provider turn | 读回 |
| --- | --- | --- | --- |
| Astra 客户端默认（发送时省略，实际 `high`） | `exec_53cb296500bf15995fd8a553debf3de6` | `01a173c7-95ee-70c2-a3c1-c0af95432972` | ACK、CONFIRMED、SUCCEEDED、`CLINX_MODEL_OK` |
| Astra `xhigh` | `exec_46378178bfb915eb38296553beebae0d` | `01a173c8-08be-7011-8e50-1aefba8c9d06` | ACK、CONFIRMED、SUCCEEDED、`CLINX_MODEL_OK` |

两个 execution 各发送一次。首次 ACK 后历史短暂未可见时只精确读回，没有再次发送。
客户端配置默认为 `high`，覆盖模型目录中推荐的 `low`；二者不能混称为同一个默认值。
非法 effort 的私有真实能力校验得到 `PROVIDER_REASONING_EFFORT_UNSUPPORTED`、
`NOT_DISPATCHED`、thread/turn 均为 null，只有一次 `model/list`，没有 `thread/start` 或 `turn/start`。
合成任务使用独立任务/租约/数据库；未重放原失败 turn，未修改生产任务或数据库。
原生产 release 保持不变。网关内部 400 的更深原因、生产加载后的行为、远端 macOS
真实设备派发不由本次 Linux 私有资格证明。

本次独立源码快照和命令/回执保存在
`/data/artifacts/clinx-model-20261025-01a173b7`；临时 Git 仓库和可再生 build 在任务结束回收，
保留 patch、源码归档、逐项日志、身份和发送回执。
