"""Display-only TUI translations; provider IDs, commands and user input stay intact."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from prompt_toolkit.utils import get_cwidth

# English is also the stable identifier used by the UI's actions and fields.
ZH = {
    "Codex manager": "Codex 管理器",
    "Use backend": "使用后端", "Manage backends": "管理后端",
    "Start Codex\nSessions and models": "打开 Codex\n查看会话与模型",
    "Add/connect providers\nStart/stop services": "添加或接入供应商\n启动或停止后台服务",
    "Configured backends": "已配置的后端",
    "No Codex added yet.\nChoose Manage backends to get started.": "尚未添加 Codex。\n选择「管理后端」开始配置。",
    "No Codex added. Choose Manage backends.": "尚未添加 Codex，请选择「管理后端」。",
    "{count} backends": "{count} 个后端", "Bridge: {count} enabled": "桥接：{count} 个已启用",
    "{count} disabled": "{count} 个已禁用", "+ {count} more configured": "另有 {count} 个已配置的后端",
    "Provider": "供应商", "Bridge": "桥接", "Enabled": "已启用", "Disabled": "已禁用",
    "Settings": "设置", "Details": "详情", "Actions": "操作",
    "Setup": "配置", "Daemon": "后台服务", "Tools": "工具", "Work": "使用", "Navigate": "导航",
    "Sessions & models": "会话与模型", "Setup & services": "配置与服务",
    "Home": "首页", "Back": "返回", "Continue": "继续", "Quit": "退出", "Refresh": "刷新",
    "Retry": "重试", "Reload": "重新加载", "Request failed": "操作失败",
    "Use": "使用", "Models": "模型", "Sessions": "会话", "Session": "会话",
    "Open Codex": "打开 Codex", "Backend models": "当前后端模型", "Switch backend": "切换后端",
    "Model overview": "跨后端模型总览", "Browse backend": "浏览后端模型",
    "Model catalogs only; model access has not been verified.": "此处展示模型目录，尚未验证模型访问权限。",
    "Showing {count} models; more are available.": "已显示 {count} 个，还有更多模型。",
    "No models returned.": "未返回模型。",
    "Unavailable": "不可用",
    "Set up this Codex": "接入当前 Codex", "New provider": "新建供应商",
    "Attach existing": "接入已有目录", "Connect bridge": "连接桥接",
    "Install launcher": "安装启动命令", "Daemon status": "后台服务状态",
    "Start daemon": "启动后台服务", "Stop daemon": "停止后台服务", "Remove daemon": "移除后台服务",
    "More tools": "更多工具", "No Codex added": "尚未添加 Codex",
    "Add a Codex configuration before using a backend.": "请先添加 Codex 配置，再使用后端。",
    "Choose Codex": "选择 Codex", "Name / Provider": "名称 · 供应商",
    "Codex directory": "Codex 目录", "Name in the manager": "管理器中的名称",
    "The default is your current Codex. Keep it to enable discovery in new sessions.": "默认使用当前 Codex 目录，接入后新会话可自动发现桥接工具。",
    "Existing routes are reused.": "已有注册信息会被复用。",
    "Use the provider already configured in this directory": "使用此目录中已配置的供应商",
    "OpenAI (use this directory's login)": "OpenAI（沿用此目录的登录状态）",
    "ChatGPT: browser or device code login, using plan access.\nAPI key: usage billed to your OpenAI Platform account.\nEnterprise automation can also use a Codex access token.\nSetup reuses this directory's login; sign in with Codex first.": "ChatGPT：浏览器或设备码登录，使用套餐权益。\nAPI Key：通过 OpenAI Platform 按用量计费。\n企业自动化还可使用 Codex access token。\n请先在 Codex 中登录；接入时沿用此目录的登录状态。",
    "For standard Codex without a provider setting, select OpenAI.": "标准 Codex 若未配置供应商，请选择 OpenAI。",
    "Installed bridge guide": "已安装的桥接指南",
    "Install if missing; keep local changes": "缺失时安装，保留本地修改",
    "Update to this version (replace local guide changes)": "更新至当前版本，覆盖指南的本地修改",
    "Choose an existing absolute Codex directory.": "请选择已存在的 Codex 目录，使用绝对路径。",
    "Enter a name for this Codex in the manager.": "请填写此 Codex 在管理器中的名称。",
    "Codex setup complete": "Codex 接入完成", "Setup incomplete": "接入尚未完成",
    "Update guide": "更新指南", "Retry setup": "重试接入", "Setting up this Codex": "正在接入当前 Codex",
    "Set up": "开始接入",
    "Connect your existing Codex with one step. A stopped backend will be started.\nSetup checks the model and installs the tools and guide for future sessions.": "接入已有 Codex；若后台服务已停止，将自动启动。\n接入时会验证模型，并为新会话安装桥接工具和使用指南。",
    "The model responded, the bridge tools are available, and its guide is enabled.\n\nNew Codex sessions using this directory will discover the bridge automatically.\nExisting sessions may need to reconnect to refresh their tools.\n\n{directory}": "模型验证通过，桥接工具和使用指南已启用。\n\n使用此目录的新 Codex 会话将自动发现桥接工具。\n已有会话可能需要重新连接以刷新工具列表。\n\n{directory}",
    "A different bridge guide is already installed. Select Update guide to replace it with this version, or keep it and return to the manager.": "已安装的桥接指南与当前版本不同。选择「更新指南」替换，或保留并返回管理器。",
    "This directory has no provider selection. Return to setup and choose OpenAI for standard Codex. For another service, use New provider in the manager.": "此目录未指定供应商。标准 Codex 请返回并选择 OpenAI；其他服务请使用「新建供应商」。",
    "Create a provider": "创建供应商", "Create provider": "创建供应商", "Provider name": "供应商名称",
    "Creates a launcher named codex-NAME.": "将创建 codex-名称 启动命令。",
    "API endpoint": "API 地址", "The provider's full Responses API base URL.": "填写供应商完整的 Responses API 基础地址。",
    "API key environment variable": "API 密钥环境变量",
    "Variable name only, for example DEMO_API_KEY. Export it before opening this interface.": "只填变量名，例如 DEMO_API_KEY；请在打开界面前导出该变量。",
    "Model ID": "模型 ID", "Context limit (tokens)": "上下文上限（token）",
    "Use the model's documented limit. Automatic compaction starts at 80%.": "填写模型文档中的上限；达到 80% 时自动压缩上下文。",
    "Reasoning effort (optional)": "推理强度（可选）", "Reasoning effort": "推理强度",
    "Leave blank for the provider default.": "留空使用供应商默认值。",
    "New Codex home (optional)": "新 Codex 目录（可选）",
    "Leave blank for an independent home in ~/.local/share/codexd-bridge/homes/.": "留空会在 ~/.local/share/codexd-bridge/homes/ 下创建独立目录。",
    "Launcher directory": "启动命令目录", "Existing model catalog (optional)": "已有模型目录文件（可选）",
    "Otherwise only the selected model is declared.": "留空时仅声明所选模型。",
    "The credential variable is not exported in this terminal. Reopen the manager after making it available.": "此终端尚未导出密钥变量。请导出后重新打开管理器。",
    "Enter a model ID.": "请填写模型 ID。",
    "Context limit must be a positive number of tokens.": "上下文上限必须是正整数。",
    "Context limit must be at least two tokens.": "上下文上限至少为 2 个 token。",
    "Connection settings stay with an independent Codex home.\nCreation includes one short model request to verify access.": "连接配置保存在独立的 Codex 目录中。\n创建时会发送一次简短的模型请求，验证访问权限。",
    "Retry connection": "重试连接", "Provider setup incomplete": "供应商配置尚未完成",
    "Creating": "正在创建", "Provider ready": "供应商已就绪", "Open sessions": "查看会话",
    "Providers": "供应商", "Could not open Codex": "无法打开 Codex", "Daemon result": "后台服务结果",
    "Finishing setup": "正在完成配置", "Install provider launcher": "安装供应商启动命令",
    "Installing launcher": "正在安装启动命令", "Launcher installed": "启动命令已安装",
    "Attaching existing home": "正在接入已有目录", "Attach an existing Codex home": "接入已有 Codex 目录",
    "Provider alias": "供应商别名", "Existing Codex home": "已有 Codex 目录",
    "Its config.toml must declare the exact model_provider.": "目录中的 config.toml 必须明确指定 model_provider。",
    "Attach": "接入", "The home and its credentials remain in place. The route starts disabled.": "保留原目录及凭据；接入后桥接默认禁用。",
    "Provider attached": "供应商已接入",
    "The backend changed after selection; refresh and review the action again": "后端配置在选择后发生变化，请刷新后重新确认操作。",
    "Start daemon:": "启动后台服务：", "Stop daemon:": "停止后台服务：", "Status daemon:": "后台服务状态：",
    "Daemon start": "后台服务启动结果", "Daemon stop": "后台服务停止结果",
    "Stop {name}?": "停止 {name}？",
    "{home}\n\nStopping this daemon can interrupt its active sessions.": "{home}\n\n停止此后台服务可能中断正在运行的会话。",
    "Type the exact backend name to confirm deleting its data.": "请输入完整的后端名称，以确认删除其数据。",
    "Daemon removed": "后台服务已移除", "Removing": "正在移除",
    "Daemon removed. Its data was deleted.": "后台服务已移除，其数据已删除。",
    "Daemon removed. Configuration, credentials and sessions were retained.\nUse Attach existing to connect it again.": "后台服务已移除，配置、凭据和会话均已保留。\n可通过「接入已有目录」重新接入。",
    "Kept modified or unrelated files:": "已保留修改过或无关的文件：",
    "Daemon data": "后台服务数据", "Keep configuration and sessions": "保留配置和会话",
    "Delete configuration, credentials and sessions": "删除配置、凭据和会话",
    "Type backend name to confirm data deletion": "输入后端名称，确认删除数据",
    "Legacy custom launcher directory (optional)": "旧版自定义启动命令目录（可选）",
    "{name}\n{home}\n\nThis stops the selected daemon, including any active tasks, and removes its registration.\nOther daemons and the bridge application remain available.": "{name}\n{home}\n\n将停止所选后台服务及其活动任务，并移除注册信息。\n其他后台服务及桥接程序仍可使用。",
    "Create daemon": "创建后台服务", "Choose registry": "选择注册文件", "Diagnostics": "诊断",
    "MCP connection": "MCP 连接", "Uninstall bridge": "卸载桥接工具", "Verify model ID": "验证模型 ID",
    "Codex arguments": "Codex 启动参数", "Installation diagnostics": "安装诊断",
    "Checking installation": "正在检查安装", "Creating daemon": "正在创建后台服务",
    "Create daemon configuration": "创建后台服务配置", "New absolute Codex directory": "新 Codex 目录的绝对路径",
    "Exact provider ID": "准确的供应商 ID", "Template Codex directory (optional)": "模板 Codex 目录（可选）",
    "Create an independent configuration without starting a daemon or making a model request.\nFor an external provider, select a template with its connection settings.": "仅创建独立配置。\n使用外部供应商时，请选择含有其连接配置的模板目录。",
    "Choose an absolute registry path.": "请填写注册文件的绝对路径。",
    "Registry path": "注册文件路径", "Open registry": "打开注册文件",
    "Verify model": "验证模型", "Verifying model": "正在验证模型", "Model verified": "模型验证通过",
    "A real response was verified; the temporary session was deleted.": "已验证真实模型响应，并删除临时会话。",
    "Run verification": "开始验证", "This sends one short model request.": "将发送一次简短的模型请求。",
    "Native Codex": "原生 Codex", "Codex arguments (optional)": "Codex 启动参数（可选）",
    "Arguments go to the selected provider's Codex CLI.\nProvider and remote endpoint overrides remain restricted.": "参数将传递给所选供应商的 Codex CLI。\n仍限制覆盖供应商和远程地址的参数。",
    "Uninstall Codex Daemon Bridge?": "卸载 Codex Daemon Bridge？",
    "Remove the bridge application, its commands and owned integrations.\nIndependent daemons keep running; their configuration and sessions are retained.\n\nThis action closes the manager. For a pip installation, use its package manager.": "卸载桥接程序、命令及其集成配置。\n独立后台服务继续运行，保留其配置和会话。\n\n卸载后管理器将关闭。通过 pip 安装的版本请用包管理器卸载。",
    "Update the installed bridge skill": "更新已安装的桥接技能",
    "Allow unattended delegation (four session write tools)": "允许无人值守委派（4 个会话写入工具）",
    "Verify model access and connect MCP. A stopped daemon will be started.": "验证模型访问并连接 MCP；若后台服务已停止，将自动启动。",
    "Existing approval settings are preserved unless you select unattended delegation.": "保留已有审批设置；选择无人值守委派时才会调整。",
    "Connecting": "正在连接", "Connect": "连接", "Bridge connected": "桥接已连接",
    "Verify and connect": "验证并连接", "Loading models": "正在加载模型", "Model details": "模型详情",
    "New session": "新建会话", "Next page": "下一页",
    "Available model IDs come from this daemon's current catalog.": "模型 ID 来自此后台服务当前的模型目录。",
    "Discovering provider models": "正在发现供应商模型",
    "Provider default": "供应商默认值", "Verifying": "正在验证",
    "A real response was verified and the test session was deleted.": "已验证真实模型响应，并删除测试会话。",
    "Verify model access": "验证模型访问", "Open session": "打开会话", "No sessions yet.": "暂无会话。",
    "Loading sessions": "正在加载会话", "Session title": "会话标题", "Working directory": "工作目录",
    "Model (optional)": "模型（可选）", "File access": "文件访问权限", "Read only": "只读",
    "Workspace write": "可写工作区", "Session type": "会话类型", "Task": "任务要求",
    "Acceptance criteria (optional)": "验收标准（可选）", "Enter a title and a task.": "请填写会话标题和任务要求。",
    "Working directory must be an existing absolute path.": "工作目录必须是已存在的绝对路径。",
    "Starting session": "正在启动会话", "Start task": "启动任务", "Task accepted": "任务已受理",
    "Reading session": "正在读取会话", "Add instructions": "补充指令", "Fork session": "派生会话",
    "Read a turn": "查看轮次", "Choose a turn": "选择轮次", "Read messages": "读取消息",
    "Older turns": "更早的轮次", "More messages": "更多消息", "Turn messages": "轮次消息",
    "No visible messages.": "暂无可见消息。", "Reading messages": "正在读取消息", "Fork title": "派生会话标题",
    "Model (blank keeps the current selection)": "模型（留空保留当前选择）", "Instructions": "指令",
    "Enter instructions and a title for a fork.": "请填写指令；派生会话还需填写标题。",
    "Sending instructions": "正在发送指令", "Send instructions": "发送指令",
    "Steer session": "补充会话指令", "Continue session": "继续会话",
    "Close when done": "完成后关闭", "Completed steps": "已完成的步骤",
    "Contacting the backend": "正在连接后端", "[done]": "[完成]", "[working]": "[进行中]",
    "The interface remains available while this operation completes.": "操作执行期间，界面仍可响应。",
    "Prepare the provider configuration": "准备供应商配置", "Start or connect to the daemon": "启动或连接后台服务",
    "Verify a real model response": "验证真实模型响应", "Connect the bridge and install its skill": "连接桥接并安装技能",
    "Install the launcher and enable the provider": "安装启动命令并启用供应商", "Ready": "已就绪",
    "Disable this daemon's bridge integration": "禁用此后台服务的桥接集成",
    "Stop the selected daemon": "停止所选后台服务", "Remove its launcher and registration": "移除启动命令和注册信息",
    "Cannot read provider registry": "无法读取供应商注册文件",
    "The current operation will finish before the interface closes.": "当前操作完成后，界面将自动关闭。",
    "Finishing the current operation...": "正在等待当前操作完成……",
    "Resize the terminal to at least 60 columns by 18 rows.\nCtrl+Q exits; your form stays intact while resizing.": "请将终端调整至至少 60 列、18 行。\nCtrl+Q 退出；调整大小时会保留表单输入。",
    "Keyboard shortcuts": "快捷键说明", "Close help": "关闭帮助",
    "Language changed; could not save the preference.": "语言已切换，但无法保存偏好设置。",
    "No entries found.": "暂无条目。",
    "{name} is verified and enabled.\n\nHome       {home}\nLauncher   {launcher}\n\nThe temporary verification session has been deleted.": "{name} 验证通过并已启用。\n\n目录       {home}\n启动命令   {launcher}\n\n临时验证会话已删除。",
    "{name}\n{home}\n\nConnect the bridge to verify and enable this provider.": "{name}\n{home}\n\n请连接桥接，以验证并启用此供应商。",
    "Additional setup, native Codex and application management.\n\nCurrent registry: {registry}": "其他配置、原生 Codex 及程序管理。\n\n当前注册文件：{registry}",
    "Use Set up this Codex or Connect bridge for automatic registration.\n\nFor another MCP client, use this stdio server command:\n\n{command}": "选择「接入当前 Codex」或「连接桥接」可自动注册。\n\n其他 MCP 客户端可使用以下 stdio 服务命令：\n\n{command}",
    "{name} is verified and enabled.\nNew Codex sessions using this directory can discover the bridge automatically.": "{name} 验证通过并已启用。\n使用此目录的新 Codex 会话可自动发现桥接工具。",
    "{model}\n\n{description}\n\nReasoning: {efforts}\n\nListed by the daemon; access still needs a real inference check.": "{model}\n\n{description}\n\n推理强度：{efforts}\n\n模型由后台服务列出，访问权限仍需通过真实推理验证。",
    "The request was accepted. Open the session to follow its result.\n\n{thread}": "请求已受理，打开会话可查看执行结果。\n\n{thread}",
    "State: {state}": "状态：{state}", "Assignment: {id}": "任务标识：{id}",
    "Type: {type} / Depth: {depth}": "类型：{type} · 派生深度：{depth}",
    "Parent: {id} ({home})": "父会话：{id}（{home}）",
    "History source: {id} ({home})": "历史来源：{id}（{home}）",
    "Task: {task}": "任务要求：{task}", "Acceptance criteria: {criteria}": "验收标准：{criteria}",
    "Turn {id} / {status}": "轮次 {id} · {status}",

    "If the connection settings are wrong, correct {path} before retrying. The existing home is retained.": "若连接配置有误，请修改 {path} 后重试。已有目录会保留。",
    " (continued)": "（续）", "[Continues on the next page]": "[下一页继续]",
    "Current Codex": "当前 Codex", "Account status": "认证状态", "Inspect Codex": "检查 Codex",
    "Open native Codex": "打开原生 Codex", "Bridge setup": "接入桥接",
    "Choose an absolute Codex directory.": "请填写 Codex 目录的绝对路径。",
    "Choose a Codex directory, not a file.": "请选择 Codex 目录，不能选择文件。",
    "New to Codex? Open native Codex to finish its own setup, then return here.\nBridge setup verifies a model response and installs discovery for new sessions.": "首次使用请打开原生 Codex，完成接入后返回。\n桥接配置会验证模型响应，并为新会话安装发现功能。",
    "OpenAI API key": "OpenAI API 密钥", "ChatGPT account": "ChatGPT 账户",
    "Managed by provider configuration": "由供应商配置管理", "Sign-in required": "需要登录",
    "Not reported": "未返回", "Codex default": "Codex 默认值",
    "No running Codex backend was found. Authentication and model selection have not been checked.\nOpen native Codex to set up or sign in, then return to bridge setup.": "未发现运行中的 Codex 后台，尚未检查认证和模型选择。\n请打开原生 Codex 完成配置或登录，再返回接入桥接。",
    "Provider: {provider}": "供应商：{provider}", "Model: {model}": "模型：{model}",
    "Authentication: {auth}": "当前认证：{auth}", "Codex account: {account}": "Codex 账户：{account}",
    "Plan: {plan}": "套餐：{plan}", "Codex account and model": "Codex 认证与模型",
    "Inspecting Codex": "正在检查 Codex", "Cannot inspect Codex": "无法检查 Codex",
    "Read from the running Codex backend. This does not verify model access or remaining credits.": "信息来自运行中的 Codex 后台；模型可用性和剩余额度尚未验证。",
    "This account response does not identify browser, device-code or access-token sign-in.": "此账户响应未区分浏览器、设备码或访问令牌登录。",
    "No models on this page.": "本页暂无模型。",
    "No sessions on this page.": "本页暂无会话。",
    "Codex exited with status {code}.": "Codex 已退出，退出码为 {code}。",
    "ok": "正常", "warning": "警告", "error": "错误",
    "Bridge command": "桥接命令", "Provider registry": "后端注册文件",
    "Not installed": "尚未安装", "Not found on PATH": "在 PATH 中未找到",
    "Install Python 3.11 or newer.": "请安装 Python 3.11 或更新版本。",
    "Run install.sh again to repair dependencies.": "请重新运行 install.sh 修复依赖。",
    "This release was validated with {package} {version}.": "此版本使用 {package} {version} 完成验证。",
    "Install Codex CLI and reopen this terminal.": "请安装 Codex CLI，然后重新打开终端。",
    "Could not verify the installed CLI": "无法验证已安装的 CLI",
    "Check that codex --version works in this terminal.": "请确认在此终端运行 codex --version 能正常返回。",
    "Installed entry point is unavailable": "已安装的命令入口不可用",
    "Run install.sh again to restore the command.": "请重新运行 install.sh 恢复命令入口。",
    "No configuration yet": "尚未配置",
    "Open the manager and choose Set up this Codex or New provider.": "请打开管理器，选择“接入当前 Codex”或“新建供应商”。",
    "Configuration could not be loaded": "无法加载配置",
    "Use More tools > Choose registry to select a valid registry, or restore this file.": "请通过“更多工具 → 选择注册文件”选择有效文件，或恢复此文件。",
    "Backend directory or provider binding is invalid": "后端目录或供应商绑定无效",
    "Select the correct Codex directory and provider in the manager.": "请在管理器中选择正确的 Codex 目录和供应商。",
    "Provider binding matches": "供应商绑定一致", "route enabled": "后端已启用", "route disabled": "后端已禁用",
    "control socket present": "控制套接字存在", "no control socket": "控制套接字不存在",
    "Use Daemon status to check the process; Connect bridge verifies model access.": "可通过“后台服务状态”检查进程，通过“连接桥接”验证模型访问。",
    "{name} MCP registration": "{name} MCP 注册",
    "Registered version differs from this program": "注册版本与当前程序不同",
    "Use Connect bridge to refresh this backend, including compatibility entries.": "请通过“连接桥接”刷新此后端及其兼容入口。",
    "Could not inspect registration metadata": "无法检查注册信息",
    "Inspect this backend through Connect bridge.": "请通过“连接桥接”检查此后端。",

}

HELP = (
    ("Tab / Shift+Tab", "Switch area or field", "切换功能区或输入项"),
    ("h l / ← →", "Switch menu area", "切换菜单功能区"),
    ("j k / ↓ ↑", "Move within a menu or list", "在菜单或列表内选择"),
    ("1–9", "Switch numbered card or area", "切换对应编号的卡片或功能区"),
    ("Alt+1–9", "Focus action / bottom button", "定位区内操作或底部按钮"),
    ("Home / End", "First / last menu action", "选择区内首项或末项"),
    ("Enter / Space", "Activate selected action", "执行所选操作"),
    ("Ctrl+S", "Submit form", "提交表单"),
    ("Esc", "Back / close help", "返回或关闭帮助"),
    ("? / F1", "Help (F1 also works in inputs)", "查看帮助（输入时使用 F1）"),
    ("F2", "中文 / English · saved automatically", "中文 / English · 自动保存"),
    ("Ctrl+N", "New provider", "新建供应商"),
    ("Ctrl+Q / Ctrl+C", "Exit after any active operation", "退出（等待正在执行的操作完成）"),
)


def preference_path(registry: Path) -> Path:
    return registry.with_name(registry.name + ".ui.json")


def load_language(registry: Path) -> str:
    try:
        value = json.loads(preference_path(registry).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("language") in ("en", "zh-CN"):
            return value["language"]
    except (OSError, ValueError):
        pass
    locale = next((os.environ[key] for key in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(key)), "en")
    return "zh-CN" if locale.lower().startswith("zh") else "en"


def save_language(registry: Path, language: str) -> None:
    from .provisioning import _atomic_write, _registry_lock

    path = preference_path(registry)
    with _registry_lock(path):
        _atomic_write(path, json.dumps({"language": language}) + "\n")


def translate(language: str, source: str, **values) -> str:
    text = ZH.get(source, source) if language == "zh-CN" else source
    return text.format(**values) if values else text


def diagnostic_text(language: str, value: str) -> str:
    """Localize known diagnostic prose without changing CLI output or provider data."""
    if match := re.fullmatch(r"(\d+) backend\(s\)", value):
        return translate(language, "{count} backends", count=match[1])
    if match := re.fullmatch(r"This release was validated with (\S+) (\S+)\.", value):
        return translate(language, "This release was validated with {package} {version}.",
                         package=match[1], version=match[2])
    if value.endswith(" MCP registration"):
        return translate(language, "{name} MCP registration", name=value.removesuffix(" MCP registration"))
    if value.startswith("Provider binding matches; "):
        return ("；" if language == "zh-CN" else "; ").join(
            translate(language, part) for part in value.split("; "))
    return translate(language, value)


def wrap_text(text: str, columns: int) -> str:
    """Wrap Latin words as units and CJK at cell boundaries, retaining explicit lines."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        # Keep ASCII words/paths intact when they fit; CJK characters use two cells.
        for token in re.findall(r"[\x21-\x7e]+|[^\x21-\x7e]", paragraph):
            if line and get_cwidth(line + token) > columns:
                lines.append(line.rstrip())
                line = ""
            if not line and token.isspace():
                continue
            for char in token:
                if line and get_cwidth(line + char) > columns:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line.rstrip())
    return "\n".join(lines)
