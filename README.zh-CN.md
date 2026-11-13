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

## 打开管理界面

```bash
codexd-bridge
```

TUI 是主要使用入口，支持以下操作：

- 创建 provider，或接入已有 Codex 目录。
- 查看、启动和停止 daemon，验证访问并接入桥接层。
- 浏览各 provider 的模型，按选定的模型和推理强度验证真实推理。
- 创建、阅读、继续及派生会话，向活动执行轮次追加指令。
- 安装 `codex-NAME` 启动命令，或打开该 provider 的原生 Codex 界面。

使用 **Tab / Shift+Tab** 移动焦点、**Enter** 选择、**Ctrl+S** 提交、**Esc** 返回、**Ctrl+Q** 退出。所有操作流程均可通过键盘完成，终端最小尺寸为 60 列、18 行。

创建 provider 时，在表单中填写已导出到当前终端的密钥环境变量**名称**，例如 `DEMO_API_KEY`；桥接层不保存密钥值。填写接口地址、模型 ID 和文档规定的上下文上限后，程序会验证真实响应，再启用后端并安装启动命令。创建未完成时，可选择 **Retry connection**，或回到管理页使用 **Connect bridge** 和 **Install launcher** 继续处理。

界面按实际执行结果显示配置阶段，等待 daemon 响应时仍保持可用。通过 **Open Codex** 打开的原生界面关闭后，会回到管理页。停止 daemon 需要确认；只有在 **Connect bridge** 中明确选择后，才会启用无需逐次审批的任务派发。

使用其他注册表时，运行 `codexd-bridge --config /absolute/bridge.toml ui`。自动化命令可通过 `--help` 查看。MCP 客户端使用 `codexd-bridge serve`；通过管道启动无参数入口时，仍保留 MCP stdio 行为。

桥接层禁止向调用会话自身或已记录的上级会话反向派发，默认最多允许八层委派。升级后，在各后端的 **Connect bridge** 中勾选 **Update the installed bridge skill** 完成更新；没有调用关系记录的旧桥接任务需重新创建会话。

## 许可

原创内容采用 [CC BY-NC 4.0](LICENSE) 许可；商业使用须另行取得[书面授权](COMMERCIAL-LICENSE.md)。第三方依赖保留各自的许可。
