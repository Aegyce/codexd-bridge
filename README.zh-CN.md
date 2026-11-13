# Codex Daemon Bridge

[English](README.md) | 简体中文

通过 MCP 协调不同模型供应商的独立 Codex App Server 会话，发现可用模型、派发任务，并继续与其他供应商的会话沟通。

## 使用前提

- Linux、Python 3.11+ 和 `codex` CLI（兼容 Codex 0.167.2）。
- 已配置好的 Codex 供应商目录：其中的 `config.toml` 声明了 `model_provider`，并且已配置访问凭据。

## 安装

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
source .venv/bin/activate
```

## 快速开始

1. 创建 `~/.config/codexd-bridge/config.toml`（参见[配置示例](examples/bridge.toml)）：

   ```toml
   [backends.demo]
   home = "~/.codex"          # 已有的 Codex 供应商目录
   model_provider = "openai"  # 必须与该目录中声明的供应商 ID 一致
   enabled = false           # connect 验证通过后启用该后端
   ```

2. 如果该后端的 daemon 尚未运行，启动并确认状态；目录中的 `config.toml` 必须声明相同的 `model_provider`：

   ```bash
   codexd-bridge daemon start demo
   codexd-bridge daemon status demo
   ```

3. 接入正在运行的 daemon，并查看模型列表。接入时会发起一次简短的模型请求，验证实际访问是否可用。

   ```bash
   codexd-bridge connect demo
   codexd-bridge models list
   ```

   如需预先批准四项会话写入工具，以便自动派发任务，可添加 `--allow-session-writes`。

4. 使用另一个 `[backends.NAME]` 条目添加其他供应商，并重复步骤 2–3。在已接入的后端中打开 Codex 会话，即可要求它派发任务：

   > 列出桥接后端和可用模型，然后让 BACKEND_NAME 审查 /absolute/path/to/project。任务完成后读取它的回复。

运行 `codexd-bridge --help` 查看更多命令，包括如何创建新的 daemon 目录。

## 许可

原创内容采用 [CC BY-NC 4.0](LICENSE) 许可；商业使用须另行取得[书面授权](COMMERCIAL-LICENSE.md)。第三方依赖保留各自的许可。
