# Codex Daemon Bridge

[![English](https://img.shields.io/badge/English-555555?style=flat-square)](README.md) [![简体中文（当前）](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-8C2F39?style=flat-square)](README.zh-CN.md)

创建供应商启动命令，通过 MCP 发现模型，并在不同供应商的独立 Codex daemon 之间派发任务。

## 使用前提

- Linux、Python 3.11+ 和 `codex` CLI（兼容 Codex 0.167.2）。
- 通过环境变量提供供应商 API Key，或使用已配置好的 Codex 供应商目录。

## 安装

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps .
source .venv/bin/activate
```

## 创建供应商

先在 shell 中导出 API Key 环境变量，再填写供应商的接口地址、模型 ID 和文档规定的上下文上限。以下参数仅作示例：

```bash
codexd-bridge provider add demo \
  --base-url https://provider.example/v1 --env-key DEMO_API_KEY \
  --model MODEL_ID --context-window 131072
codex-demo
```

命令会创建独立目录、验证真实推理、接入 MCP，并将 `codex-demo` 安装到 `~/.local/bin`；请将该目录加入 `PATH`。配置只保存环境变量名，已有目录和启动命令不会被覆盖。
Chat Completions 接口使用 `--wire-api chat`；推理强度使用 `--effort`；已有原生模型目录可通过 `--model-catalog /absolute/catalog.json` 指定。未提供目录时，只登记指定模型，并将自动压缩阈值设为上下文上限的 80%。

如果创建目录后接入失败，修复报错原因，再运行 `codexd-bridge provider connect demo` 完成验证和启动器安装。`codex-demo server start|status|stop` 管理该供应商的 daemon。

## 接入已有供应商

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

桥接层禁止向调用会话自身或已记录的上级会话反向派发，默认最多允许八层委派。升级后，在各后端运行 `connect NAME --update-skill` 刷新 MCP 工具；没有调用关系记录的旧桥接会话需重新创建任务会话。

## 许可

原创内容采用 [CC BY-NC 4.0](LICENSE) 许可；商业使用须另行取得[书面授权](COMMERCIAL-LICENSE.md)。第三方依赖保留各自的许可。
