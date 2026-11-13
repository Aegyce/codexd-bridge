# Codex Daemon Bridge

[![English](https://img.shields.io/badge/English-555555?style=flat-square)](README.md) [![简体中文（当前）](https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-8C2F39?style=flat-square)](README.zh-CN.md)

创建供应商启动命令，通过 MCP 发现模型，并在不同供应商的独立 Codex daemon 之间派发任务。

## 使用前提

- Linux（含 WSL）、Python 3.11+ 及其 `venv` 模块，以及 `codex` CLI。
- 通过环境变量提供供应商 API Key，或使用已配置好的 Codex 供应商目录。

## 安装

从 [GitHub Releases](https://github.com/Aegyce/codexd-bridge/releases) 下载并解压源码包，在解压目录运行 `./install.sh`。也可以通过 Git 获取项目：

```bash
git clone https://github.com/Aegyce/codexd-bridge.git
cd codexd-bridge
./install.sh
```

安装脚本会在用户目录中创建独立 Python 环境、安装锁定版本的依赖、设置短命令的搜索路径，并在终端中安装完成后直接打开 TUI。无需 `sudo`，也无需激活虚拟环境；安装后可以删除下载的项目目录。如果当前终端暂时找不到短命令，重新打开终端即可；脚本会自动配置 Bash、Zsh 或 Fish 的启动文件。

升级时，在新版下载目录运行 `./install.sh`；安装器自动刷新属于本安装且已启用的 MCP 注册和未经修改的配套指南，无需重启 daemon。构建失败会保留原来的可用版本。自动化安装可使用 `./install.sh --no-launch`。

## 打开管理界面

```bash
codexd-bridge
```

在首页选择 **Set up this Codex** 接入现有 Codex 账号，保留预填的目录和名称。选择目录中已配置的供应商；标准 Codex 如果尚未显式设置供应商，则选择 **OpenAI**，再选择 **Set up**。界面会按需启动后台、验证模型访问、安装使用指南并注册工具。配置完成后，使用该目录新启动的 Codex 会话会自动发现桥接功能；已有会话可能需要重新连接以刷新工具列表。

接入其他服务时，选择 **New provider** 并按表单填写。服务必须支持 Responses API；当前支持的 Codex 版本已不再接受 Chat Completions。如果安装器提示指南有本地修改或刷新未完成，检查修改后打开 **Set up this Codex**，在指南选项中选择 **Update to this version**。配置失败会保留输入，并提供界面内重试入口。这些流程无需另外执行注册命令或手动编辑配置文件。

管理界面还支持浏览模型、管理会话、启停后台、安装 `codex-NAME` 启动命令，以及打开对应供应商的原生 Codex 界面。

首页提供常用管理操作；**More tools** 提供独立 daemon 配置创建、注册表切换、自定义模型验证、原生 Codex 参数、MCP 连接信息和桥接项目卸载入口。创建、继续、派生和引导活动任务均可在会话页面操作。

使用 **Tab / Shift+Tab** 移动焦点、**Enter** 选择、**Ctrl+S** 提交、**Esc** 返回、**Ctrl+Q** 退出。所有操作流程均可通过键盘完成，终端最小尺寸为 60 列、18 行。

创建 provider 时，在表单中填写已导出到当前终端的密钥环境变量**名称**，例如 `DEMO_API_KEY`；桥接层不保存密钥值。填写接口地址、模型 ID 和文档规定的上下文上限后，选择 **Create provider** 即开始创建、验证真实响应、启用后端并安装启动命令，无需再次提交。创建未完成时，选择 **Retry connection** 继续验证和接入。

**Install launcher** 只安装 `codex-NAME` 命令，不启动后台或发送模型请求；命令行对应 `codexd-bridge provider launcher NAME`。**Connect bridge** 用于验证和接入后台，失败后重试会保留该后端已选的指南更新和委派审批选项。

界面按实际执行结果显示配置阶段，等待 daemon 响应时仍保持可用。通过 **Open Codex** 打开的原生界面关闭后，会回到管理页。停止 daemon 需要确认；只有在 **Connect bridge** 中明确选择后，才会启用无需逐次审批的任务派发。

使用其他注册表时，运行 `codexd-bridge --config /absolute/bridge.toml ui`。包括 `install` 在内的自动化命令可通过 `--help` 查看。MCP 客户端使用 `codexd-bridge serve`；通过管道启动无参数入口时，仍保留 MCP stdio 行为。

每次创建或派生任务都会生成新的任务记录，包含唯一标识、会话类型、父会话与历史来源、根会话、派生深度、任务要求和验收标准。使用 `session_type` 指定类型（例如 `worker` 或 `reviewer`），使用 `acceptance_criteria` 指定验收标准，`prompt` 提供任务要求；省略验收标准时明确标为未指定。记录会写入首条消息，也可通过会话读取工具和管理界面查看。子会话可以继续从自身派生，新记录指向直接父会话，继承的旧任务记录作为历史上下文。

桥接层禁止向调用会话自身或已记录的上级会话反向派发；从当前会话派生会创建新的子会话。默认最多允许八层委派。通过其他包管理方式安装时，更新程序包后，在各后端的 **Connect bridge** 中勾选 **Update the installed bridge skill** 完成更新；没有调用关系记录的旧桥接任务需重新创建会话。已有调用关系会在状态文件升级时保留，升级后的状态文件不支持旧版桥接程序读取。

连续派生与自动发现由自动化测试覆盖。自动发现还通过隔离 Codex 目录中的新建原生会话检查；该检查不运行模型推理，也不代表所有供应商的模型均可用。

## 移除单个 daemon

在首页选择目标后端，再选择 **Remove daemon**。默认停止该后台并移除它的注册、桥接集成和未修改的启动命令，保留配置、凭据和会话。之后可通过 **Attach existing** 重新接入。停止会中断该后台的活动任务。

```bash
codexd-bridge daemon remove NAME
```

若还要删除该 daemon 的全部数据，在 TUI 中选择删除并输入后端名称，或执行：

```bash
codexd-bridge daemon remove NAME --purge
```

程序会拒绝删除默认或当前 Codex 目录、共享目录，以及包含桥接程序、注册表或其他后端的目录。失败时保留禁用的注册记录以便重试。其他 daemon 和桥接项目本身继续保留。

## 卸载桥接项目

```bash
codexd-bridge uninstall
```

也可以在下载的项目目录运行 `./uninstall.sh`。两种方式都卸载 **Codex Daemon Bridge 项目本身**：移除本次安装的程序、短命令和终端路径设置，禁用它注册的 MCP 工具，并删除未改动的内置指南。独立 Codex daemon 不会被卸载或停止，供应商配置、凭据和会话数据会保留。本地修改过的指南及其他程序的命令也会保留。

运行 `codexd-bridge doctor` 可检查安装和供应商配置，不会发送模型请求。诊断结果以 JSON 输出；发现检查错误时，退出码为 1。也可以在 **More tools → Diagnostics** 中查看。使用 `codexd-bridge --version` 查看安装版本。

开发与发行检查：[发行指南](docs/releasing.md)、[架构说明](docs/architecture.md)和[版本变更](CHANGELOG.md)。

## 许可

原创内容采用 [CC BY-NC 4.0](LICENSE) 许可；商业使用须另行取得[书面授权](COMMERCIAL-LICENSE.md)。第三方依赖保留各自的许可。
