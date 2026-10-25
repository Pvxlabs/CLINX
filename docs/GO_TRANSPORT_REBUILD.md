# 固定 Go Transport 依赖修复与全新重建

日期：2026-10-25。基准源码 `f645055f10d45fb45c36e13817c29bfe814a30fe`。
本轮修复开发构建器，未部署、未重启服务、未操作真实任务或生产数据库。
机器回执：[GO_TRANSPORT_REBUILD_RECEIPT.json](cleanup/GO_TRANSPORT_REBUILD_RECEIPT.json)。

## 结论

**PASS：两个全新、独立输出目录完成依赖获取、上游目标测试和完整 transport 编译，
二进制 SHA-256 相同；实际发布包和回滚包验证通过。**
本轮未使用旧 transport 二进制或旧模块/编译缓存。
这份新证据解除 [上轮源码退役报告](RUST_PYTHON_RETIREMENT.md) 中的 fresh Go build
`BLOCKED_NETWORK`；上轮失败日志和当时的回执保持原样。

## 下载失败的定位与证据边界

原错误发生在 `proxy.golang.org` 的 HTTP 下载/读取阶段，返回 `unexpected EOF`，
没有出现 `unknown revision`、404 或 checksum mismatch。之前的失败缓存已经按上轮
清理回执删除，无法重新检查当时的部分响应。本轮没有臆造具体断开连接的一端。

本轮实际检查：

- 模块代理仍为 `https://proxy.golang.org`，校验数据库仍为 `sum.golang.org`。
  P620 使用现有 Mihomo HTTP CONNECT 代理 `127.0.0.1:7893`；没有修改代理服务配置。
- 两项模块分别经 HTTP/1.1 和 HTTP/2 下载，均返回 200，TLS 校验成功；两个协议的
  ZIP 字节完全相同，全部成员 CRC 检查通过。四次绕过本机代理的模块下载均在 TCP 连接阶段超时。
- 用固定 Go 1.27.3 在新的诊断缓存重新下载，两个 `h1` 均与固定上游 `go.sum` 一致，
  ZIP SHA 与 curl 结果相同。默认 Go HTTP 与强制 HTTP/1 的六模块独立探针均成功；
  没有证据将问题归咎于 HTTP/2，因此未更改 Go 协议默认值。
- 候选 A 的实际完整依赖获取再次发生 EOF，包括固定 `otel@v1.41.0` 等模块；
  第二次获取成功，缓存校验通过。候选 B 在另一套空缓存中第一次获取成功。
  该行为支持“下载链路的短暂中断”，不支持“固定版本不存在”或“必须升级才能构建”。
- 可读的历史代理 journal 没有相应记录。原中断究竟发生在代理上游还是远端关闭连接，
  **无法从现存日志确定**；这不是本轮归档完整性或构建结果的未知项。

| 固定模块 | ZIP SHA-256 | 与上游 go.sum 一致的 h1 |
| --- | --- | --- |
| `github.com/modelcontextprotocol/go-sdk@v1.7.0` | `324a2a6956be6b4aa8a92057cf2ac71ad91bdbbf4dd3d9a00a192e789843a320` | `h1:yqjY2dsbKAC0LSuWZVBMrHgiG8ukXv6NRo0JiALay44=` |
| `go.opentelemetry.io/otel/sdk@v1.41.0` | `143225cecc6fd0b6b54d3d5e350896c78c886ae1646bbb7e9ea5df1e2f08b40e` | `h1:YPIEXKmiAwkGl3Gu1huk1aYWwtpRLeskpV+wPisxBp8=` |

Go 工具链归档重新获取后 SHA 仍为
`65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40`。
官方工具链下载经代理较慢，续传改走可用的官方直连路径；逐段验证 Content-Range，
最终仍以完整原 SHA 为执行前提。没有换版本、镜像、归档内容或编译器。

## 构建器修复

`clinx-build build-transport-runtime` 的参数和固定版本保持不变。

1. 仅对模块获取时明确的 EOF/connection reset，最多执行三次相同命令；
   每个获取阶段共享一个 900 秒总预算，在同一任务私有缓存内恢复。
2. checksum mismatch、SECURITY ERROR、未知版本和命令超时不进入重试。
   上游测试及编译失败也不重试，不将业务执行/Provider 的未知结果纳入这一机制。
3. 每次尝试分别保存完整 stdout/stderr，失败或超时也保留原始输出和尝试回执。
4. 先获取运行依赖与四个目标包的测试依赖，再执行 `go mod verify`；
   测试和编译阶段使用 `GOPROXY=off`。编译后再次验证模块缓存，检查上游源码未修改。

新回归使用真实私有子进程，覆盖 EOF 后保留缓存和原日志、永久错误/完整性错误不重试、
三次尝试上限，以及超时保留日志且不重新执行。受影响 Release 回归
**21 build + 4 relay + 6 Monitor = 31 PASS，0 failed / 0 ignored**。
Scoped Clippy 通过，仅保留 10 个既有 library 告警，没有新增告警。

## 两份独立构建

源提交固定为 `0f870e50a973fa820d4c409000059e181e8d242b`，源归档 SHA 为
`0281b17c0979f9db1ae2b41b0b66df697d646e726995756a003c6ffc51a64c10`。
`candidate-a` 和 `candidate-b` 起始均不存在，分别解压源码和工具链，创建自己的
HOME、GOMODCACHE、GOCACHE 与 TMPDIR；A 的 transient 已删除后才开始 B，没有缓存共享。

两者均执行：

```text
go list -mod=readonly -deps ./cmd/client-runtime
go list -mod=readonly -deps -test ./cmd/client-runtime ./pkg/runtimeapp ./pkg/runtimehealth ./pkg/runtimeconfig
go mod verify
GOPROXY=off go test -mod=readonly -count=1 -timeout=90s ./cmd/client-runtime ./pkg/runtimeapp ./pkg/runtimehealth ./pkg/runtimeconfig
GOPROXY=off go build -trimpath -buildvcs=false -mod=readonly -ldflags <固定 GitSHA/Flavor> -o <新输出> ./cmd/client-runtime
go mod verify
```

每个候选内部还复制到第二个源码路径重编译并比较 SHA。
两候选均为三个目标包的测试 PASS，`pkg/runtimeapp` 无测试文件但完成编译；
没有声称执行整个上游仓库中无关的 embedded/full-client 测试。
依赖清单验证未链接 embedded Codex、full client 或 Cloudflared executor。

两个独立候选的二进制 SHA-256 均为：

```text
31f83c5cab00cdb17b594085706190106d174a88d49ea6e7edec8e3823183f9d
```

源文件清单、依赖清单与 builder 源码 SHA 也一致。独立候选 manifest 的绝对输出路径不同，
因此 manifest 本身的 SHA 不相同，这是预期行为。

## 实际发布包与回滚

两个打包目录均使用**本轮 candidate A** 的已验证新二进制和 manifest，配合当前源码
新构建的 Rust supervisor/CAS 工具；没有读入旧 `candidate-v2`。
候选 B 用于独立编译一致性证明。使用同一候选 manifest 打包保持发布输入身份一致，
没有通过修改 manifest 来强行统一不同输出目录的路径身份。

| 产物 | 两个打包目录中相同的 SHA-256 |
| --- | --- |
| `release.tar.gz` | `e8a39a32c4f5c84e8f9a6c513c7f97e0227b32ba134f655bbd30ff7ed2fb1533` |
| `rollback-capsule.tar.gz` | `97b8c883ed63eb45e679e1161e4fb62c2807d882266c42b914be9963b3d91cac` |

逐文件校验 SHA、mode、精确文件集合；运行包没有 Python 或开发工具。
在不含 Python 的 PATH 下运行包内 supervisor status，缺失私有运行状态时按契约返回 UNKNOWN。
实际执行新回滚包内的原生 CAS，限于带任务标记的隔离目录：forward 后内容/mode 符合计划，
rollback 后所有原文件状态精确还原；没有向 systemd 安装或切换 release。

## 日志、保留与限制

完整证据根：`/data/artifacts/clinx-go-rebuild-20261025`。
包含 network 探针与 ZIP/hash、两个候选的逐尝试日志、上游测试/编译输出、build-info、
source/dependency manifests、实际包验证和最终集成/清理回执。
历史 EOF 和本轮 A 的首次失败不会被成功回执覆盖。

临时上游源码、工具链、模块/编译缓存和重复展开包在保存证据后删除。
保留本轮新产物的唯一压缩包、两个独立候选的 manifest/hash/log 与源码恢复 bundle。
没有改变生产服务、数据库、凭据或运行数据。
生产部署、物理 Mac 跨设备和 macOS/Xcode 构建不属于本轮验证，继续为 NOT_RUN。
