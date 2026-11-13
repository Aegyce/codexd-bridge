# Codex Daemon Bridge

通用 Codex App Server 会话桥接工具。各后端保留独立 daemon、供应商配置与会话数据，桥接层通过 MCP 提供统一操作。

- Python 3.11+，源码在 `src/codexd_bridge/`，测试在 `tests/`。
- provider 由显式配置决定；新建、恢复及派生均校验实际供应商，不能静默回退。
- 模型密钥由目标 daemon 管理；桥接层不读取、保存或输出凭据。
- 活动会话使用带当前 turn ID 的 steer；不擅自中断任务或重启 daemon。
- README 使用中英双语：中文在上，紧邻 `<br><sub>English text</sub>`。文档区分实际能力与未验证项。
- 原创内容采用 CC BY-NC 4.0；商业使用须另行取得书面授权。第三方依赖保留原许可。
- 临时测试结果放 `tmp/`，不得提交密钥、会话历史、虚拟环境或本机配置。
