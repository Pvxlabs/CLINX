> 文档分类：SUPERSEDED。本页保留原日期、实现阶段、验收结果与失败记录；旧命令和路径仅适用于原基准。
> 当前源码定义见[当前契约](CONTRACTS.md)与[架构](REPOSITORY_ARCHITECTURE.md)，不得据本页推断当前部署或重新引入旧实现。

# Python 退役边界

当前受版本控制的 Python 源码为 **零**。没有必须保留的 Python 兼容层；
原 Provider/Authority/Node/Discovery/配置 DTO 的消费者已迁为 Rust 或明确退役。
构建、安装、诊断、会话维护和现役资格也由 Rust 承担。

- `architecture/python-sources.json` 的全部旧模块标为 DEAD，源码不存在。
- Rust release 和 Monitor 的 Python 源码/依赖清单为空；没有解释器或 wheel。
- `native/opaque_pairing` 直接链接 Rust OPAQUE，PyO3 绑定已删，三项原协议断言直接验证 Rust。
- 资格保留有用的语义义务、原参数和独立 oracle，删除重复 Python 测试主体与对象专有兼容接口。
- Rust 测试中的 Python 子进程脚本已替换为原生测试进程；没有通过嵌入字符串保留 Python 执行。

当前结构、工具和发布成员见 [仓库架构](REPOSITORY_ARCHITECTURE.md)；
逐项删除、替代及验证边界见 [最终源码退役](RUST_PYTHON_RETIREMENT.md)。

本地 ignored `tests/`、历史恢复 bundle、旧发布包中的 Python 未被删除。
它们属于历史测试/回滚/已安装运行资源，不是当前分发源码。原始文件哈希与全量失败对照仍保留。
本轮没有修改 `.gitignore` 或 Linguist 规则，源码统计口径和低于 5% 目标保持不变。

本轮不部署。已安装的旧 Python Dispatcher/Centre 仍按其不可变 release 运行，
不能把源码退役表述成生产环境已切换。
