# Codex Daemon Bridge

[![English](https://img.shields.io/badge/English-555555?style=flat-square)](README.md) [![简体中文（当前）](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-8C2F39?style=flat-square)](README.zh-CN.md)

管理独立 Codex daemon，发现供应商模型，并通过 MCP 跨供应商派发任务。使用键盘 TUI 完成配置和日常操作。

## 使用前提

- Linux（含 WSL）、Python 3.11+ 及其 `venv` 模块，以及 `codex` CLI。
- 已配置好的 Codex 目录，或通过密钥环境变量访问供应商服务。

## 安装

1. 从 [GitHub Releases](https://github.com/Aegyce/codexd-bridge/releases) 下载 `codexd_bridge-VERSION.tar.gz`。
2. 解压后，在解压目录运行 `./install.sh`。
3. 按安装器自动打开的 TUI 提示操作。以后在任意目录运行 `codexd-bridge` 即可。

安装器会创建独立 Python 环境，并配置 Bash、Zsh 或 Fish 的命令搜索路径。如果当前终端找不到命令，重新打开终端即可。安装无需 `sudo` 或激活虚拟环境，完成后可以删除下载目录。

<details>
<summary>通过 Git 获取项目并安装</summary>

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
./install.sh
```

自动化安装可使用 `./install.sh --no-launch`。

</details>

## 接入现有 Codex

```bash
codexd-bridge
```

1. 在首页选择 **Set up this Codex**。
2. 保留预填的 Codex 目录和名称，或选择需要接入的现有目录。
3. 使用目录中已配置的供应商；标准 Codex 如果尚未显式设置供应商，选择 **OpenAI**。
4. 选择 **Set up**。管理器会按需启动后台、验证模型访问、安装指南并注册 MCP 工具。

配置完成后，使用该目录新启动的 Codex 会话会自动发现桥接功能。已有会话可能需要重新连接；配置失败会保留输入，并提供重试入口。

![TUI 运行画面：使用演示 Codex 目录的首次接入表单](docs/images/tui-setup.png)

*实际运行的接入表单，图中使用演示目录。*

## 添加其他供应商

先在终端导出供应商的密钥环境变量，再打开管理器并选择 **New provider**。服务须支持 Responses API。

| 表单字段 | 填写内容 |
| --- | --- |
| Provider name | 简短名称，例如 `demo`；对应启动命令为 `codex-demo` |
| API endpoint | 供应商的 Responses API 基础地址 |
| API key environment variable | 环境变量**名称**，例如 `DEMO_API_KEY`；桥接层不保存密钥值 |
| Model ID | 供应商提供的准确模型标识 |
| Context limit | 模型文档规定的上下文令牌上限 |
| Reasoning effort | 可选；留空使用供应商默认值 |

选择一次 **Create provider**，即可创建配置、验证真实响应、启用后端并安装启动命令。若未完成，选择 **Retry connection** 继续接入。

## 日常操作

在首页选中目标后端，再选择对应操作。

![TUI 运行画面：包含演示后端的供应商管理首页](docs/images/tui-manager.png)

*图中的后端为尚未验证的演示配置。*

| 要做什么 | TUI 入口 | 操作结果 |
| --- | --- | --- |
| 接入已有 Codex 目录 | **Attach existing** | 将现有目录加入管理器 |
| 接入或刷新桥接工具 | **Connect bridge** | 验证模型访问，安装指南并注册 MCP |
| 浏览或验证模型 | **Models** / **All models** | 查看单个后端或所有已配置后端 |
| 管理任务 | **Sessions** | 创建、继续、派生或引导活动任务 |
| 查看、启动或停止后台 | **Daemon status** / **Start daemon** / **Stop daemon** | 操作当前选中的后端 |
| 安装 `codex-NAME` 命令 | **Install launcher** | 只安装命令，不启动后台或发送模型请求 |
| 打开原生 Codex | **Open Codex** | 进入对应供应商界面，关闭后返回管理器 |
| 查找其他操作 | **More tools** | daemon 配置创建、注册表切换、自定义模型验证、原生参数、诊断、MCP 详情和卸载 |

**快捷键：** `Tab` / `Shift+Tab` 移动焦点 · `Enter` 选择 · `Ctrl+S` 提交 · `Esc` 返回 · `Ctrl+Q` 退出。
终端最小尺寸为 60 列、18 行。停止 daemon 需要确认；免逐次审批的任务派发须在 **Connect bridge** 中明确启用。

## 升级

- 在新版下载目录运行 `./install.sh`。安装器会刷新属于本安装且已启用的 MCP 注册和未经修改的指南，无需重启 daemon；构建失败会保留原来的可用版本。
- 如果指南有本地修改或刷新未完成，检查修改后选择 **Set up this Codex → Update to this version**。
- 通过其他包管理方式安装时，更新程序包后，在各后端选择 **Connect bridge → Update the installed bridge skill**。
- 状态升级会保留已有任务的调用关系。旧版桥接程序无法读取升级后的状态；没有调用关系记录的旧任务需重新创建会话。

## 派发任务

- 每次创建或派生任务都会生成新记录：唯一标识、会话类型、父会话与历史来源、根会话、派生深度、任务要求和验收标准。
- 使用 `session_type` 指定类型（如 `worker` 或 `reviewer`），`prompt` 提供任务要求，`acceptance_criteria` 提供验收标准。省略验收标准时会标为未指定；记录写入首条消息，也可通过会话读取工具查看。
- 子会话可以继续派生，新记录指向直接父会话。默认最多允许八层委派，禁止向调用会话自身或已记录的上级反向派发。引导活动任务时须使用当前 turn ID。

## 移除 daemon 或卸载项目

| 操作 | 命令 | 处理范围 |
| --- | --- | --- |
| 移除单个 daemon | `codexd-bridge daemon remove NAME` | 停止该后台，移除注册、桥接集成和未修改的启动命令；保留配置、凭据和会话 |
| 移除 daemon 及其数据 | `codexd-bridge daemon remove NAME --purge` | 额外删除该 daemon 的数据 |
| 卸载桥接项目 | `codexd-bridge uninstall` | 移除桥接程序及其集成；保留独立 daemon 和数据 |

- **TUI 入口：** **Remove daemon** 或 **More tools → Uninstall bridge**。破坏性操作需要确认，删除数据还需输入后端名称。
- 停止或移除 daemon 会中断其活动任务。保留的数据可通过 **Attach existing** 重新接入；移除失败时保留禁用的注册记录，便于重试。
- 数据删除会拒绝受保护或共享的 Codex 目录，以及包含桥接程序、注册表或其他后端的目录。
- `./uninstall.sh` 也可卸载桥接项目。卸载会保留本地修改过的指南和其他程序的命令。

## 常用命令

| 用途 | 命令 |
| --- | --- |
| 检查安装和供应商配置 | `codexd-bridge doctor` |
| 查看安装版本 | `codexd-bridge --version` |
| 查看自动化命令 | `codexd-bridge --help` |
| 使用其他注册表 | `codexd-bridge --config /absolute/bridge.toml ui` |
| 启动 MCP stdio 入口 | `codexd-bridge serve` |

诊断不会发送模型请求，结果以 JSON 输出；发现检查错误时退出码为 1。也可在 **More tools → Diagnostics** 中查看。

[发行指南](docs/releasing.md)列出验证结果及未验证的平台和模型；[架构说明](docs/architecture.md)介绍会话与失败处理；[版本变更](CHANGELOG.md)汇总功能改进。

## 许可

原创内容采用 [CC BY-NC 4.0](LICENSE) 许可；商业使用须另行取得[书面授权](COMMERCIAL-LICENSE.md)。第三方依赖保留各自的许可。
