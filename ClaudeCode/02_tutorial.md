# Claude Code 系统性使用教程

> **更新日期：2026 年 7 月**
> **信息来源：Anthropic 官方文档、社区教程、技术博客**

---

## 核心术语速查

在阅读本教程前，先了解以下关键概念：

| 术语 | 英文 | 一句话解释 |
|------|------|-----------|
| **智能体（Agent）** | Agent | 能自主理解目标、制定计划、执行操作并自我纠错的 AI 程序。Claude Code 就是一个编码智能体——你描述目标，它负责执行 |
| **Token** | Token | 模型处理文本的最小单位。约 1 个 Token ≈ 0.75 个英文单词 ≈ 0.3-0.5 个中文字。200K tokens ≈ 一部中等篇幅的小说 |
| **上下文窗口** | Context Window | 模型一次能"看到"的最大文本量（以 Token 为单位）。超出窗口的内容会被遗忘，需要 `/compact` 压缩 |
| **MCP** | Model Context Protocol | Anthropic 创建的开放协议，让 Claude Code 能连接外部工具（GitHub、数据库、文件系统等），已成为行业标准 |
| **Hooks** | Hooks | 事件钩子——在特定时机（编辑文件后、命令执行前）自动触发的脚本。提供确定性保障，不依赖 LLM 记忆 |
| **Skills** | Skills | 可复用的工作流技能包，教 Claude 如何完成特定类型的任务。"给 Claude 写操作手册" |
| **子智能体** | Sub-agent | 从主智能体分离出的独立工作单元，在隔离的上下文窗口中并行执行子任务 |
| **CLAUDE.md** | CLAUDE.md | 项目根目录下的 Markdown 文件，Claude 每次启动时自动读取。相当于项目的"持久记忆"——记录技术栈、编码规范、常用命令等 |

> **提示**：以上术语后文不再重复解释。遇到不熟悉的术语时可随时回查此表。

---

## 目录

1. [安装指南](#1-安装指南)
2. [初始配置](#2-初始配置)
3. [基本使用](#3-基本使用)
4. [高级功能](#4-高级功能)
5. [项目管理](#5-项目管理)
6. [常见问题与排错](#6-常见问题与排错)
7. [与 IDE 对比：Claude Code vs Cursor vs Copilot](#7-与-ide-对比claude-code-vs-cursor-vs-copilot)
8. [CI/CD 集成实践](#8-cicd-集成实践)
9. [Docker 使用指南](#9-docker-使用指南)

---

## 1. 安装指南

### 1.1 前置条件

| 条件 | 说明 |
|------|------|
| **操作系统** | macOS 10.15+ / Windows 10+ / Ubuntu 18.04+ / WSL |
| **Node.js** | 仅 npm 方式需要 Node.js 18.0+；**原生安装器不需要 Node.js** |
| **终端** | 终端或命令提示符 |
| **账户** | Claude 订阅（Pro/Max/Team/Enterprise）或 Anthropic Console API 密钥 |
| **内存** | 推荐 4GB+ RAM（16GB 更佳） |
| **Git** | Windows 原生安装推荐安装 Git for Windows |

### 1.2 安装方式

#### 方式一：原生安装器（推荐）

**不需要 Node.js**，自包含可执行文件，支持自动后台更新。

**macOS / Linux / WSL：**

```bash
# 稳定版
curl -fsSL https://claude.ai/install.sh | bash
```

**Windows PowerShell：**

```powershell
irm https://claude.ai/install.ps1 | iex
```

**Windows CMD：**

```cmd
curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd
```

> **提示：** 如果 CMD 中提示 `'&&' is not a valid statement separator`，说明你在 PowerShell 中，请使用上面的 PowerShell 命令。如果在 PowerShell 中提示 `'irm' is not recognized`，说明你在 CMD 中。

#### 方式二：Homebrew（macOS/Linux）

```bash
# 稳定版（通常落后约一周，跳过有重大问题的版本）
brew install --cask claude-code

# 最新版（紧跟最新发布）
brew install --cask claude-code@latest
```

> **注意：** Homebrew 安装不会自动更新。定期运行 `brew upgrade claude-code` 获取最新功能和安全修复。

#### 方式三：WinGet（Windows）

```powershell
winget install Anthropic.ClaudeCode
```

> **注意：** WinGet 安装不会自动更新。定期运行 `winget upgrade Anthropic.ClaudeCode`。

#### 方式四：npm（传统方式）

```bash
# 全局安装（需要 Node.js 18+）
npm install -g @anthropic-ai/claude-code

# 不要使用 sudo！sudo npm install 会导致后续文件权限问题
```

#### 方式五：Linux 包管理器

```bash
# Arch Linux (AUR)
yay -S claude-code
# 或
paru -S claude-code
```

```bash
# Debian/Ubuntu（通过 apt）
sudo apt update && sudo apt install -y nodejs npm
npm install -g @anthropic-ai/claude-code
```

### 1.3 从 npm 迁移到原生安装器

如果你之前通过 npm 安装了 Claude Code，可以一键迁移：

```bash
claude install
```

此命令自动完成：
- 下载原生安装器
- 迁移你的配置和设置（`~/.claude/settings.json` 和项目 `.claude/` 目录会被保留）
- 替换 npm 安装为原生二进制文件

### 1.4 验证安装

```bash
# 检查版本号
claude --version

# 运行自诊断（确认安装、设置、扩展和上下文使用状况）
claude /doctor
# 或如果 claude 启动不了
claude doctor
```

---

## 2. 初始配置

### 2.1 认证与登录

#### 方式一：OAuth 登录（最常见）

适用于 Claude 订阅账户（Pro/Max/Team/Enterprise）：

```bash
# 在项目目录中启动
cd /path/to/your/project
claude
```

首次运行时会自动打开浏览器进行 OAuth 认证。按提示完成登录即可。

如需切换账户或重新认证，在会话中运行：

```text
/login
```

#### 方式二：API Key 认证

适用于 Anthropic Console 账户：

```bash
# 设置环境变量
export ANTHROPIC_API_KEY="sk-ant-..."

# 添加到 shell 配置文件使其持久化
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc  # 或 ~/.zshrc
source ~/.bashrc

# 启动
claude
```

获取 API Key：访问 [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key

#### 方式三：云平台认证

**Amazon Bedrock：**
```bash
export CLAUDE_CODE_USE_BEDROCK=1
# 配置好 AWS 凭证后运行
claude
```

**Google Vertex AI：**
```bash
export CLAUDE_CODE_USE_VERTEX=1
# 配置好 GCP 凭证后运行
claude
```

### 2.2 CLAUDE.md 项目配置文件

CLAUDE.md 是 Claude Code 的核心配置文件。Claude 在**每次会话启动时自动读取**它，使其能理解你的项目上下文。

#### 创建 CLAUDE.md

在项目根目录运行：

```text
/init
```

Claude 会自动分析代码库并生成包含构建命令、测试指令和项目约定的 CLAUDE.md。

#### CLAUDE.md 模板

在项目根目录创建 `CLAUDE.md` 文件：

```markdown
# 项目概述
本项目是一个 [1-2 句话的项目描述]。

## 技术栈
- 语言：TypeScript
- 框架：Next.js 15 (App Router)
- 数据库：PostgreSQL
- 包管理器：pnpm

## 目录结构
- `src/app/` — 页面组件
- `src/components/` — 共享组件
- `src/lib/` — 工具函数
- `prisma/` — 数据库 schema

## 编码规范
- 使用 TypeScript 严格模式
- 组件使用函数式组件
- 使用 Vitest 进行测试
- 遵循 Conventional Commits 规范

## 常用命令
- `pnpm dev` — 启动开发服务器
- `pnpm build` — 生产构建
- `pnpm test` — 运行测试
- `pnpm lint` — 运行 lint 检查

## 注意事项
- 禁止直接推送到 `main`，必须通过 Pull Request
- 环境变量写入 `.env.local`，只在 `.env.example` 中记录变量名
```

#### CLAUDE.md 的位置层级

| 作用域 | 位置 | 用途 |
|--------|------|------|
| **组织级**（托管策略） | macOS: `/Library/Application Support/ClaudeCode/CLAUDE.md`<br>Linux/WSL: `/etc/claude-code/CLAUDE.md`<br>Windows: `C:\Program Files\ClaudeCode\CLAUDE.md` | 公司编码标准、安全策略、合规要求 |
| **用户级**（个人全局） | `~/.claude/CLAUDE.md` | 个人偏好的代码风格、工具快捷方式 |
| **项目级**（团队共享） | `./CLAUDE.md` 或 `./.claude/CLAUDE.md` | 项目架构、编码标准、通用工作流 |
| **本地级**（个人项目） | `./CLAUDE.local.md`（添加到 `.gitignore`） | 个人沙箱 URL、偏好的测试数据 |
| **子目录级**（递归加载） | `./subdir/CLAUDE.md` | 子目录特定的指令，处理该子目录时按需加载 |

> **加载机制：** Claude Code 从当前工作目录向上递归读取所有 CLAUDE.md 文件。加载顺序：组织级 → 用户级 → 项目级 → 本地级。子目录中的 CLAUDE.md 在访问对应子目录时自动加载。

在 `CLAUDE.md` 中更好的做法：
- 重复出现的相同错误
- 代码审查中发现的代码库特有约定
- 新成员需要了解的上下文
- 反模式（此代码库中禁止的做法）
- 当前开发阶段说明
- 关键参考文件路径

### 2.3 IDE 集成

#### VS Code 集成

**安装扩展：**
1. 打开 VS Code，按 `Ctrl+Shift+X` / `Cmd+Shift+X`
2. 搜索 "Claude Code"
3. 点击安装（发布者为 Anthropic）

或直接安装：[VS Code 扩展](https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code)

**使用方式：**
- 点击右上角 Spark 图标打开 Claude 面板
- 或按 `Ctrl+Shift+P` / `Cmd+Shift+P`，输入 "Claude Code: Open in New Tab"
- 选中代码后发送，Claude 自动感知选中内容

**关键快捷键：**

| 快捷键 | 操作 |
|--------|------|
| `Cmd+Esc` / `Ctrl+Esc` | 在编辑器和 Claude 面板间切换焦点 |
| `Cmd+Shift+Esc` / `Ctrl+Shift+Esc` | 在新标签页打开 Claude |
| `Option+K` / `Alt+K` | 插入当前选中内容的 @-mention |
| `Cmd+N` / `Ctrl+N` | 新对话（面板聚焦时） |
| `Shift+Enter` | 换行不发送 |
| `Shift+Tab` | 切换模式（计划 → 接受编辑 → 默认） |

**扩展设置：**
- `claudeCode.initialPermissionMode` — 初始权限模式（`default`/`acceptEdits`/`plan`）
- `claudeCode.useTerminal` — 是否使用终端
- `claudeCode.autosave` — 是否自动保存
- `claudeCode.useCtrlEnterToSend` — 是否用 Ctrl+Enter 发送

#### JetBrains 集成

**安装插件：**
1. 打开 JetBrains IDE（IntelliJ IDEA、WebStorm、PyCharm 等）
2. `Settings → Plugins → Marketplace`
3. 搜索 "Claude Code"
4. 安装并重启 IDE

**使用方式：**
- 侧边栏打开 Claude Code 工具窗口
- 右键代码 → "Ask Claude Code"
- 检查插件左侧面板

---

## 3. 基本使用

### 3.1 启动 Claude Code

```bash
# 进入项目目录
cd /path/to/your/project

# 启动交互模式
claude

# 你会看到类似以下界面：
# Claude Code v2.x.x | Model: Sonnet 5 | /path/to/your/project
# >
```

> **💡 相关命令**：`/model` 切换模型、`/resume` 恢复会话、`/status` 查看当前状态。完整启动参数见 [3.6.13 CLI 启动参数](#3613-cli-启动参数)。

### 3.2 对话交互的三种模式

#### 1. 交互模式（Interactive Mode）

最常用的方式。在终端中进入对话式交互：

```bash
claude
```

```text
> 解释这个项目的结构
> 审查 src/app/page.tsx 中的代码
> 更新 README.md
> 检查 git diff 并提交更改
```

退出交互模式：按 `Ctrl+C` 或输入 `/exit`

#### 2. 单次模式（One-Shot Mode）

执行单条指令后立即退出，适合脚本化和自动化：

```bash
# 基本用法
claude -p "为此项目生成 README"

# 列出目录
claude -p "列出 src 目录下所有 TypeScript 文件"

# 摘要
claude -p "用简洁英文总结最新提交"
```

#### 3. 管道输入（Pipe Input）

```bash
# 审查文件内容
cat src/utils/format.ts | claude -p "审查此代码并提出改进建议"

# 分析错误日志
cat error.log | claude -p "分析此错误日志并找出原因"

# 总结 git diff
git diff | claude -p "以提交信息格式总结这些更改"
```

> **💡 相关命令**：`/exit` 退出会话、`/plan` 进入只读计划模式、`/background` 后台运行。完整会话管理命令见 [3.6.1 节](#361-会话管理命令-11个)。

### 3.3 安全规则与权限

Claude Code 在**修改文件或运行命令前都会请求确认**。当它提议更改时，你会看到 diff 和三个选项：

| 选项 | 含义 |
|------|------|
| **Yes** | 应用此单个更改 |
| **Yes, and don't ask again for edits** | 剩余会话中自动批准文件编辑 |
| **No** | 拒绝更改并解释原因 |

按 `Shift+Tab` 可在模式间循环切换：**Plan（计划）→ Accept Edits（接受编辑）→ Default（默认）**

> **💡 相关命令**：`/permissions` 精细化管理权限、`/fewer-permission-prompts` 自动减少提示、`/sandbox` 开启沙箱隔离。完整权限命令见 [3.6.4 节](#364-工具与权限命令-9个)。

### 3.4 文件读写操作

Claude Code 会自动按需读取文件，无需手动上传。你可以直接要求它处理任何文件。

```text
# 查找内容
> 用户认证在哪里处理？显示文件和关键函数。

# 安全编辑（低风险练手）
> 在 <文件> 的 <函数> 中添加一个文档字符串。限制在 2 行内。

# 修复问题
> 这个测试失败了：<粘贴错误>。找出原因并修复。

# 多文件编辑（Claude 自动处理）
> 在所有组件中添加 TypeScript 返回类型注解。
```

> **💡 相关命令**：`@file_path` 引用文件、`/add-dir` 添加额外工作目录、`/memory` 编辑项目记忆。完整上下文命令见 [3.6.2 节](#362-上下文与内存命令-5个)。

### 3.5 Git 操作集成

```bash
# 提交更改
> 暂存我的更改并写一个遵循我们已有风格的提交信息。

# 创建分支
> 创建一个新分支 feature/add-auth 并提交当前更改。

# 审查
> 审查最近 3 个提交的代码质量和安全性。
```

> **💡 相关命令**：`/diff` 查看代码变更、`/code-review` 审查 diff、`/commit` 提交更改。完整审查工作流见 [3.6.5 节](#365-工作流与审查命令-14个)。

### 3.6 内置斜杠命令大全

Claude Code 内置 90+ 个斜杠命令，按功能分为以下大类。每个命令包含语法、参数、功能描述、使用场景和注意事项。

> **命令数量统计**：会话管理 11 个 | 上下文与内存 5 个 | 模型控制 5 个 | 工具与权限 9 个 | 工作流与审查 14 个 | 高级/隐藏 12 个 | 认证 4 个 | 状态监控 7 个 | IDE/集成 8 个 | 平台配置 3 个 | 趣味辅助 6 个 | 捆绑技能 15 个

#### 3.6.1 会话管理命令 (11个)

##### `/clear [name]`
- **别名**：`/reset`， `/new`
- **语法**：`/clear [可选: 名称]`
- **功能**：清除当前对话的全部上下文，开始一个新的空会话。项目记忆文件（CLAUDE.md）会被保留并在新会话中重新加载。之前的对话会被完整保存到磁盘上，可以通过 `/resume` 恢复。不同于 `/compact`（保留上下文但压缩），`/clear` 会完全丢弃对话历史。
- **场景 1 — 切换任务类型**：完成了认证模块的调试工作，现在要开始实现新的 API 端点时，`/clear auth-module-debugging` 清除旧上下文
- **场景 2 — 项目初次设置**：生成 `CLAUDE.md` 后刷新会话，让新配置生效：`/clear`
- **注意**：MCP 连接、工具权限和会话设置保持不变；仅清除对话历史，不删除任何文件

##### `/compact [instructions]`
- **语法**：`/compact [可选: 焦点指令]`
- **功能**：通过总结/压缩到目前为止的对话历史来释放上下文窗口空间。Claude 会将较早的消息压缩为摘要，保留关键信息。可以通过焦点指令告诉 Claude 优先保留哪些上下文。与 `/clear` 不同，`/compact` 保留对话的连续性。
- **场景 1 — 上下文接近满载**：当 `/context` 显示 80%+ 使用率时，`/compact focus on the auth module changes and test failures` 压缩但保留认证相关上下文
- **场景 2 — 长会话优化**：开发了大半天后压缩上下文，`/compact keep all database migration details` 保留数据库迁移相关信息
- **注意**：不带焦点指令时 Claude 自行决定保留什么；建议在 50% 上下文使用时主动运行，而非等到自动压缩（~95%）

##### `/resume [session]`
- **别名**：`/continue`
- **语法**：`/resume [可选: 会话ID或名称]`
- **功能**：恢复之前保存的对话。会话选择器默认显示当前工作目录下的会话，按 Ctrl+A 可查看所有项目的历史会话。后台会话在选择器中标记为 `bg`。运行中的后台会话不可恢复，需先停止或从 `claude agents` 附加。
- **场景 1 — 第二天继续工作**：`/resume auth-refactor` 恢复昨天命名的认证重构会话
- **场景 2 — 查看所有历史**：`/resume` 打开选择器，浏览并选择要恢复的会话
- **注意**：v2.1.144+ 后台会话标记 `bg`；运行中的后台会话不可恢复

##### `/branch [name]`
- **别名**：`/fork`（在设置 `CLAUDE_CODE_FORK_SUBAGENT` 后行为改变）
- **语法**：`/branch [可选: 分支名称]`
- **功能**：在当前时间点创建对话分支。立即切换到新分支，原始会话被保留。你可以用 `/resume` 切换回原始会话。两个分支完全独立，互不影响。
- **场景 1 — 探索替代方案**：讨论架构时想试试另一种思路，`/branch try-redis-instead-of-memcache`
- **场景 2 — 并行开发**：实现了一个功能后，想同时尝试两种不同的优化方向
- **注意**：别名 `/fork` 仍然有效（v2.1.77 后重命名）；设置 `CLAUDE_CODE_FORK_SUBAGENT` 后 `/fork` 行为不同

##### `/fork [prompt]` (v2.1.212+)
- **语法**：`/fork [可选: 指令]`
- **功能**：将当前对话复制到一个新的后台会话中。原会话继续运行，副本在后台独立执行。两个会话完全独立。
- **场景 1 — 委托后台任务**：`/fork investigate the memory leak in the worker process` 在后台分析内存泄漏
- **场景 2 — 不离线的并行工作**：修复 bug 的同时，`/fork run the full regression test suite` 在后台运行回归测试
- **注意**：需要 v2.1.212+；关闭代理视图时退回到分支子代理行为

##### `/rewind`
- **别名**：`/checkpoint`， `/undo`
- **语法**：`/rewind`（执行后弹出检查点菜单）
- **功能**：将对话和/或代码回退到之前的检查点。Claude Code 在每次你按回车执行操作前自动保存检查点。提供三种精确回退粒度：
  - **仅回退代码**：恢复文件更改，但保留对话历史。Claude 记住尝试了什么、为什么失败，但文件系统回到干净状态
  - **仅回退对话**：保留代码更改，但回滚 Claude 的记忆。适合想基于相同代码状态重新推理
  - **全部回退**：同时回退代码和对话
  - **从此处摘要**：压缩从选定点开始的消息
- **场景 1 — 代码改错了但分析对了**：回退代码但保留对话，重新执行
- **场景 2 — 需要重新推理**：代码改动可以但对话方向偏了，回退对话
- **场景 3 — 完全回滚**：Claude 改了大量代码但全跑偏了，回退到之前节点
- **注意**：也可通过双击 Esc 键访问（Esc, Esc）；就像 Word 的撤销 + Git 的 revert 结合体

##### `/rename [name]`
- **语法**：`/rename [可选: 名称]`
- **功能**：重命名当前会话并在提示栏上显示名称，方便在 `/resume` 中识别。非交互模式（`-p`）也可用。不带参数时自动从对话历史生成名称。
- **场景 1 — 标记重要会话**：`/rename auth-refactor-v2`
- **场景 2 — 自动命名**：`/rename` 让 Claude 自动生成描述性名称
- **注意**：非交互模式也可用（v2.1.205+）

##### `/recap`
- **语法**：`/recap`
- **功能**：按需生成当前会话的一句话摘要。帮助你在离开后回来时快速回忆进度。Claude Code 在长时间离开后会自动显示 recap，此命令让你随时主动触发。
- **场景 1 — 回到会话**：离开喝了杯咖啡回来，`/recap` 查看进展
- **场景 2 — 与他人同步**：把会话交给同事前，`/recap` 生成摘要
- **注意**：仅生成摘要，不修改会话状态

##### `/exit`
- **别名**：`/quit`
- **功能**：退出 CLI 会话。在附加的后台会话中，此命令仅分离会话（后台继续运行），不停止会话。
- **场景 1 — 正常退出**：工作完成，`/exit` 退出
- **场景 2 — 分离后台会话**：连接到一个后台会话后，`/exit` 分离它（后台继续运行）
- **注意**：连按两次 Ctrl+C 也可以退出；后台会话中 `/exit` 只分离不停止，用 `/stop` 停止

##### `/export [filename]`
- **语法**：`/export [可选: 文件名]`
- **功能**：将当前对话导出为纯文本文件。适合保存复杂调试会话或架构决策的记录。无文件名时打开对话框选择复制到剪贴板或保存到文件。
- **场景 1 — 保存调试记录**：`/export debug-session-2026-07-26.txt`
- **场景 2 — 分享给团队**：`/export` 打开对话框选择复制到剪贴板
- **注意**：导出为纯文本，不含颜色/格式

##### `/stop`
- **语法**：`/stop`
- **功能**：停止当前后台会话。保留对话记录和 worktree。仅在附加到后台会话时可用。
- **场景 1 — 完成后台任务**：后台任务完成，`/stop` 停止
- **场景 2 — 不再需要后台会话**：想清理后台会话列表
- **注意**：保留对话记录和 worktree；仅分离不停止用 `/exit`

> **💡 相关命令**：会话压缩后想检查效果？试试 `/context` 查看上下文使用率（见 3.6.2 节）。

---

#### 3.6.2 上下文与内存命令 (5个)

##### `/context [all]`
- **语法**：`/context [可选: all]`
- **功能**：将当前上下文使用情况可视化为彩色网格。显示每个组成部分（对话历史、工具输出、系统指令、文件内容等）占用 token 的比例。提供上下文密集型工具、内存膨胀和容量警告的优化建议。全屏模式下逐项详情默认折叠以保持网格可见。
- **场景 1 — 诊断性能下降**：当 Claude 开始遗忘早期内容时，`/context` 查看是什么占用了窗口
- **场景 2 — 压缩前评估**：运行 `/compact` 前先 `/context` 了解 token 分配情况
- **场景 3 — 详细分析**：`/context all` 展开所有项的详细信息
- **注意**：v2.1.216+ 超出上下文窗口时显示警告

##### `/add-dir <path>`
- **语法**：`/add-dir <路径>`（输入部分路径会显示匹配建议，按 Tab 接受）
- **功能**：为当前会话授予 Claude 对额外工作目录的文件访问权限。添加的目录中的大多数 `.claude/` 配置不会被自动发现。
- **场景 1 — 跨项目工作**：当前在 `frontend/` 目录，`/add-dir ../backend` 让 Claude 也能访问后端代码
- **场景 2 — 引用共享库**：`/add-dir ../shared-libs` 访问共享依赖库
- **注意**：添加目录的配置不自动发现；想移动会话工作目录用 `/cd` 而非 `/add-dir`

##### `/cd <path>` (v2.1.169+)
- **语法**：`/cd <目标路径>`
- **功能**：将当前会话移动到新工作目录。对话的提示缓存被保留：新目录的 CLAUDE.md 追加为消息而非重建系统提示。会话被重新定位到新目录的项目存储中，因此 `--resume` 和 `--continue` 可以在新目录中找到它。
- **场景 1 — 切换项目**：从 `project-a/` 切换到 `project-b/`，`/cd ../project-b`
- **场景 2 — 在 monorepo 中导航**：`/cd packages/frontend`
- **注意**：需要 v2.1.169+；可通过 `Cd` 权限规则限制或禁用目标

##### `/memory`
- **语法**：`/memory`（打开交互界面）
- **功能**：编辑 CLAUDE.md 内存文件、启用或禁用自动内存（auto-memory）、查看自动内存条目。Claude Code 的 auto-memory 功能会在工作过程中自动保存学到的内容（如构建命令、调试经验等），这些记忆在会话间持久化。
- **场景 1 — 编辑项目记忆**：`/memory` 打开编辑器修改 `./CLAUDE.md`
- **场景 2 — 查看自动记忆**：查看 Claude 自动保存了哪些学习内容
- **场景 3 — 启用/禁用 auto-memory**：控制是否自动保存记忆
- **注意**：编辑时区分全局（`~/.claude/CLAUDE.md`）和项目（`./CLAUDE.md`）记忆

##### `/init`
- **语法**：`/init`
- **功能**：扫描代码库并生成启动器 CLAUDE.md 文件。这是每个项目首次使用 Claude Code 时的第一步。生成的 CLAUDE.md 包含项目结构、主要技术栈、构建命令、测试命令等。设置 `CLAUDE_CODE_NEW_INIT=1` 环境变量可启用交互式流程。
- **场景 1 — 新项目**：首次在仓库中运行 `claude` 后，立即 `/init` 生成 CLAUDE.md
- **场景 2 — 更新配置**：项目结构发生大变化后，重新 `/init` 更新 CLAUDE.md
- **注意**：生成前会探索代码库；`CLAUDE_CODE_NEW_INIT=1` 启用更详细的交互式流程

> **💡 相关命令**：了解完基础命令后，试试 `/permissions` 减少每次操作的确认提示（见 3.6.4 节）。

---

#### 3.6.3 模型控制命令 (5个)

##### `/model [model]`
- **语法**：`/model [可选: 模型名称]`
- **功能**：切换 AI 模型并保存为新会话的默认值。支持 Opus、Sonnet、Haiku 等模型。选择器中按 `s` 仅对当前会话切换，不改变默认值。对于支持的模型，左右箭头调整努力级别。键盘快捷键：Option/Alt + P。
- **场景 1 — 简单任务用快速模型**：`/model haiku` 处理文件探索等简单任务
- **场景 2 — 复杂架构用深度模型**：`/model opus` 处理架构设计、安全审查等
- **场景 3 — 临时切换**：在选择器中按 `s` 仅当前会话使用高级模型
- **注意**：非交互模式也可用（v2.1.205+）；支持完整模型 ID 如 `claude-opus-4-7`

##### `/effort [level|auto]`
- **语法**：`/effort [级别|auto]`
- **功能**：设置模型的推理努力程度（推理深度）。低级别响应快但思考浅，适合简单重命名等任务；高级别响应慢但推理深，适合架构决策、复杂调试等。`ultracode` 结合了 `xhigh` 推理和自动工作流编排。立即生效，无需等待当前响应完成。
- **级别**：`low`， `medium`， `high`， `xhigh`， `max`， `ultracode`；`auto` 重置为模型默认值
- **场景 1 — 快速日常任务**：`/effort low` 处理重命名变量、添加注释等简单任务
- **场景 2 — 复杂重构**：`/effort high` 进行架构级别的改动
- **场景 3 — 关键任务**：`/effort max` 处理安全敏感的代码审查
- **场景 4 — 结合 /fast**：`/fast` + `/effort low` 获得最快的日常开发体验
- **注意**：可用级别取决于模型；`max` 和 `ultracode` 仅限当前会话；非交互模式也可用

##### `/fast [on|off]`
- **语法**：`/fast [on|off]`（不带参数时切换）
- **功能**：开启/关闭快速模式。快速模式使用 Opus 4.8 的高速 API 配置，速度快达 2.5 倍，但每 token 成本更高。启用时提示栏旁显示 ↯ 图标。如果不在 Opus 上，开启 `/fast` 会自动切换到 Opus 4.8。
- **场景 1 — 急需响应**：`/fast on` 加速当前任务
- **场景 2 — 日常快速开发**：`/fast` 开启后挂着，持续获得快速响应
- **注意**：自 v2.1.154 起默认使用 Opus 4.8；速率限制时自动降级（↯ 变灰）

##### `/plan [description]`
- **语法**：`/plan [可选: 任务描述]`
- **功能**：进入 Plan Mode（计划模式），Claude 会先探索、解释和提议方案，但不会编辑文件或运行命令。在执行前审查方案，确保方向正确。也可以用 Shift+Tab 在普通模式和 Plan Mode 间切换。
- **场景 1 — 大型重构前**：`/plan refactor the auth module to use JWT`
- **场景 2 — 新功能规划**：`/plan implement user registration with email verification`
- **注意**：Plan Mode 下 Claude 只能读取和分析；用 Shift+Tab 切换模式

##### `/advisor [model|off]`
- **语法**：`/advisor [模型名|off]`（不带参数时打开选择器）
- **功能**：启用或禁用顾问工具。顾问工具在任务关键时刻咨询第二个模型以获取指导。不接受 Fable 5 作为顾问。
- **场景 1 — 双模型决策**：`/advisor opus` 让 Opus 审查 Sonnet 的工作
- **场景 2 — 禁用顾问**：`/advisor off` 关闭
- **注意**：不接受 Fable 5；v2.1.215 后不再自动运行

> **💡 提示**：模型的选择直接影响成本。日常使用 Sonnet 5 即可，复杂架构决策时再切 Opus。详见 [03_tips.md 第 4.4 节](03_tips.md) 成本控制策略。

---

#### 3.6.4 工具与权限命令 (9个)

##### `/permissions`
- **别名**：`/allowed-tools`
- **语法**：`/permissions`（打开交互式对话框）
- **功能**：管理工具的允许、询问和拒绝规则。可以按范围（项目/用户/企业）查看规则、添加或删除规则、管理工作目录、查看最近的自动模式拒绝。权限模式包括：default（每次询问）、acceptEdits（自动接受文件编辑）、plan（只读模式）、auto（AI 决定权限）、bypassPermissions（跳过所有提示）。
- **场景 1 — 允许 npm 命令**：把 `Bash(npm:*)` 加入 allow 列表
- **场景 2 — 保护 .env 文件**：把 `Read(.env*)` 和 `Write(.env*)` 加入 deny 列表
- **场景 3 — 让 git 操作自动执行**：把 `Bash(git:*)` 加入 allow 列表
- **注意**：规则使用模式匹配：`Bash(git diff:*)` 只允许带任何参数的 `git diff`；可配置到 `.claude/settings.json`；Shift+Tab 循环切换权限模式

##### `/mcp [subcommand]`
- **语法**：`/mcp [子命令]`
- **功能**：管理 MCP（Model Context Protocol）服务器连接和 OAuth 认证。MCP 是开放标准，用于将外部系统（GitHub、Jira、数据库、内部 API 等）作为工具接入 Claude。
- **子命令**：
  - 无参数 — 打开交互式 MCP 服务器列表
  - `reconnect <server>` — 重新连接指定服务器
  - `enable [<server>|all]` — 启用服务器
  - `disable [<server>|all]` — 禁用服务器或全部
- **场景 1 — 连接 Slack**：`/mcp` 管理 Slack MCP 连接
- **场景 2 — 添加新服务器**：`claude mcp add my-server command`
- **场景 3 — 列出服务器**：`claude mcp list`
- **注意**：非交互模式无参数时打印文本摘要（v2.1.205+）；MCP 服务器可暴露提示作为命令（`/mcp__<server>__<prompt>`）

##### `/hooks`
- **语法**：`/hooks`
- **功能**：查看工具事件的 hook 配置。Hooks 是在 Claude 生命周期中定义点自动运行的 shell 命令，例如工具运行前、编辑后、会话开始时。常用于自动格式化、linting 或阻止不安全命令。
- **场景 1 — 查看 hooks**：`/hooks` 查看当前配置
- **场景 2 — 管理 hook**：在 `.claude/settings.json` 中配置 hooks
- **注意**：Hook 在工具运行前/后、编辑后、会话开始/结束时触发

##### `/skills`
- **语法**：`/skills`
- **功能**：列出当前会话中可用的所有 skills。可以输入过滤列表。按 `t` 按 token 数排序。按 `Space` 循环 skill 的可见性设置，`Enter` 保存。
- **场景 1 — 浏览 skills**：`/skills` 查看所有自定义和捆绑 skills
- **场景 2 — 管理可见性**：按 Space 切换 skill 是否自动加载
- **注意**：包括自定义 skills、捆绑 skills 和插件提供的 skills

##### `/agents`
- **语法**：`/agents`
- **功能**：自 v2.1.198 起，运行 `/agents` 打印提醒信息，指导用户让 Claude 创建或管理子代理，或直接编辑 `.claude/agents/` 或 `~/.claude/agents/`。子代理是专门的 Claude 实例，主会话可以生成来处理特定的子任务（如测试、代码搜索、审查），各自拥有独立的上下文窗口。
- **场景 1 — 创建代码审查子代理**：直接告诉 Claude "创建一个专用代码审查子代理"
- **场景 2 — 管理现有子代理**：直接编辑 `.claude/agents/` 目录中的文件
- **注意**：v2.1.198+ 行为改变（不再打开交互界面）

##### `/plugin [subcommand]`
- **语法**：`/plugin [子命令]`
- **功能**：管理 Claude Code 插件。插件可以捆绑 LSP、MCP、skills、agents 和自定义 hooks。可从官方 Anthropic 插件市场安装，也可搭建组织内部市场。
- **子命令**：`list`， `install`， `enable`， `disable`；无参数打开插件菜单
- **场景 1 — 安装 code-review 插件**：`claude plugin install code-review@claude-plugins-official`
- **场景 2 — 浏览插件**：`/plugin` 打开菜单
- **注意**：非交互模式也可用（v2.1.205+）

##### `/reload-plugins [--force]`
- **语法**：`/reload-plugins [--force]`
- **功能**：重新加载所有活跃插件以应用待定更改，无需重启 Claude Code。报告每个已重新加载组件的计数并标记任何加载错误。
- **场景 1 — 插件更新后**：修改插件配置后，`/reload-plugins` 应用更改
- **场景 2 — 强制重新加载**：`/reload-plugins --force` 即使有风险也强制执行
- **注意**：不需要重启会话

##### `/reload-skills`
- **语法**：`/reload-skills`
- **功能**：重新扫描 skill 和 command 目录，使磁盘上新增或更改的 skills 在会话中立即可用。报告可用 skills 数量及新增/移除数量。
- **场景 1 — 添加新 skill**：创建新 skill 后，`/reload-skills` 让它立即可用
- **场景 2 — 修改 skill 后**：编辑 SKILL.md 后重新加载
- **注意**：v2.1.152 新增；Claude Code 也会自动监视 skill 目录变化

##### `/sandbox`
- **语法**：`/sandbox`
- **功能**：切换沙盒模式。沙盒模式提供文件和网络隔离，提高安全性同时减少权限提示。三种模式：带自动允许的 Sandbox BashTool、带常规权限的 Sandbox BashTool、无沙盒。
- **场景 1 — 运行不信任的代码**：开启沙盒后让 Claude 执行
- **场景 2 — 提高安全性**：在敏感项目中开启沙盒
- **注意**：仅在支持的平台上可用；仅支持 Bash tool（BashTool）

> **💡 相关命令**：想了解如何自动格式化每次编辑后的代码？试试 `/hooks` 配置 PostToolUse hook（见 [02_tutorial.md 第 4.3 节](#43-hooks-自动化钩子)）。

---

#### 3.6.5 工作流与审查命令 (14个)

##### `/diff`
- **语法**：`/diff`
- **功能**：打开交互式差异查看器，显示未提交的更改和每轮对话的差异。使用左右箭头在当前 git 差异和单个 Claude 对话轮次间切换，使用上下箭头浏览文件。按 Enter 打开选定文件的差异详情。
- **场景 1 — 提交前审查**：`/diff` 查看 Claude 改了什么再决定是否提交
- **场景 2 — 逐轮审查**：切换到每轮差异视图，了解每一步的改动
- **注意**：支持 git diff 和 per-turn diff 两种视图；v2.1.198+ 自动刷新外部 git 变化

##### `/code-review [level] [--fix] [--comment] [target]` [Skill]
- **语法**：`/code-review [努力级别] [--fix] [--comment] [目标]`
- **功能**：审查当前 diff 的正确性错误和清理机会。低级别返回较少高置信度的发现，高级别覆盖更广。ultra 级别运行云端多代理审查。v2.1.154 后 `/simplify` 独立运行仅清理审查。
- **参数**：
  - 努力级别: `low`， `medium`， `high`， `xhigh`， `max`， `ultra`
  - `--fix` — 自动将发现的问题应用到工作树
  - `--comment` — 将发现作为 GitHub PR 内联评论发布
  - `[target]` — 可选。目标路径或 PR 引用
- **场景 1 — 日常提交前审查**：`/code-review` 或 `/code-review medium`
- **场景 2 — 深度审查**：`/code-review high --fix` 自动修复发现的问题
- **场景 3 — PR 评论**：`/code-review --comment` 将发现作为 PR 内联评论发布
- **场景 4 — 云端审查**：`/code-review ultra` 使用多代理在云端深度审查（前 3 次免费，Pro/Max）
- **注意**：仅在你调用时运行（v2.1.215 起）；标记为 Skill

##### `/review [PR]`（已弃用）
- **语法**：`/review [PR编号]`
- **功能**：对 GitHub PR 进行快速单次只读审查。自 v2.1.202 起使用简单审查引擎。
- **场景 1 — 审查团队 PR**：`/review 42`
- **场景 2 — 列出 PR**：`/review` 列出开放 PR 供选择
- **注意**：已弃用，推荐安装 code-review 插件：`claude plugin install code-review@claude-plugins-official`

##### `/security-review` [Skill]
- **语法**：`/security-review`
- **功能**：分析当前分支待处理更改的安全漏洞。审查 git diff 并识别注入、认证问题、数据暴露、不安全的依赖项和错误配置等风险。只读操作，不进行修改。
- **场景 1 — 发布前安全审查**：`/security-review` 检查是否有安全漏洞
- **场景 2 — 敏感性代码审查**：修改认证/授权代码后运行
- **注意**：基于 git diff；只读，不修改代码；可结合 `/code-review` 一起使用

##### `/simplify [target]` [Skill]
- **语法**：`/simplify [可选: 目标]`
- **功能**：审查更改代码的清理机会并应用修复。四个审查代理并行运行：复用现有辅助函数、简化、效率、抽象层级。v2.1.154 起不查找正确性错误（用 `/code-review` 查找错误）。
- **场景 1 — 代码整洁**：`/simplify src/auth/` 清理认证模块代码
- **场景 2 — 提交前优化**：改完代码后 `/simplify` 整理
- **注意**：不检查正确性错误；产生 3 个并行代理——token 消耗相对较高

##### `/batch <instruction>` [Skill]
- **语法**：`/batch <变更指令>`
- **功能**：在整个代码库中并行编排大规模更改。研究代码库后将工作分解为 5 到 30 个独立单元，在隔离的 git worktree 中为每个单元启动一个后台子代理，每个子代理实现其单元、运行测试并打开一个 PR。需要 git 仓库。
- **场景 1 — 大规模迁移**：`/batch migrate src/ from Solid to React`
- **场景 2 — 批量重命名**：`/batch rename all API endpoints from v1 to v2`
- **场景 3 — 全局样式更新**：`/batch update all components to use new design tokens`
- **注意**：需要 git 仓库；分解为 5-30 个独立单元；每个单元自动创建 PR

##### `/autofix-pr [prompt]`
- **语法**：`/autofix-pr [可选: 提示]`
- **功能**：启动一个 Claude Code on the web 会话，监控当前分支的 PR，在 CI 失败或审阅者留下评论时自动推送修复。需要 `gh` CLI 和访问 Claude Code on the web。
- **场景 1 — 自动修复 CI**：`/autofix-pr` 自动修复 CI 中的测试失败
- **场景 2 — 响应审阅**：`/autofix-pr only fix lint and type errors` 仅修复特定类型问题
- **注意**：需要 gh CLI；自动检测当前分支的 PR

##### `/background [prompt]`
- **别名**：`/bg`
- **语法**：`/background [可选: 提示]`
- **功能**：将当前会话分离为后台代理运行，释放当前终端。可以传递提示以在分离前发送一条额外指令。使用 `claude agents` 监控会话。
- **场景 1 — 长时间任务**：`/background run the full regression suite and report back`
- **场景 2 — 释放终端**：`/background` 让 Claude 在后台继续工作，你继续使用终端
- **注意**：使用 `claude agents` 监控；如需复制对话用 `/fork` 而非 `/background`

##### `/loop [interval] [prompt]` [Skill]
- **别名**：`/proactive`
- **语法**：`/loop [间隔] [提示]`
- **功能**：在会话保持打开期间重复运行提示。省略间隔 Claude 会自动调整每次迭代之间的步速。省略提示则运行自主维护检查，或运行 `.claude/loop.md` 中的提示（如果存在）。会话最长运行 3 天。
- **场景 1 — 监控部署**：`/loop 5m check if the deploy finished`
- **场景 2 — 自动审查**：`/loop 30m /review` 每 30 分钟自动审查
- **场景 3 — PR 维护**：`/loop 1h babysit all my open PRs`
- **注意**：别名 `/proactive`；最长运行 3 天

##### `/schedule [description]`
- **别名**：`/routines`
- **语法**：`/schedule [可选: 描述]`
- **功能**：创建、更新、列出或运行例行任务（routines）。与 `/loop` 不同，计划的任务在云端运行——即使笔记本电脑关闭也继续工作。Claude 会以对话方式引导你完成设置。
- **场景 1 — 每日任务**：`/schedule a daily job that looks at all PRs shipped since yesterday and updates docs`
- **场景 2 — 早晨分类**：`/schedule every morning triage new issues`
- **注意**：云端执行（Anthropic 管理的云基础设施）；笔记本关闭也能运行

##### `/goal [condition|clear]`
- **语法**：`/goal [条件|clear]`
- **功能**：设置一个目标：Claude 在多个轮次中持续工作直到满足条件。每轮结束后，一个小型快速模型检查条件是否满足。如果不满足，Claude 开始另一轮而不是将控制权返回给你。非常适合有可验证最终状态的大型工作。
- **场景 1 — 持续开发**：`/goal all unit tests pass and the build is green`
- **场景 2 — 重构到完成**：`/goal migrate all API calls from v1 to v2 and verify compilation`
- **场景 3 — 清除目标**：`/goal clear`
- **注意**：v2.1.139+；每轮后用快速模型评估；自动模式处理工具审批，`/goal` 处理轮次审批

##### `/tasks`
- **别名**：`/bashes`
- **语法**：`/tasks`
- **功能**：列出和管理后台任务，包括已完成的子代理。Claude 响应期间也可使用，不需要等待。
- **场景 1 — 监控后台工作**：`/tasks` 查看当前后台任务列表
- **场景 2 — 检查子代理状态**：查看子代理是否完成
- **注意**：别名 `/bashes`；Claude 响应期间也可用

##### `/workflows`
- **语法**：`/workflows`
- **功能**：打开工作流进度视图以监控、暂停、恢复或保存运行中和已完成的工作流。工作流是跨多个子代理并行执行的动态流程。
- **场景 1 — 监控 batch 进度**：运行 `/batch` 后，`/workflows` 查看各单元进度
- **场景 2 — 管理工作流**：暂停或恢复运行中的工作流
- **注意**：主要用于监控 batch 等并行工作流

##### `/subtask <task>` (v2.1.212+)
- **语法**：`/subtask <任务描述>`
- **功能**：启动分支子代理，继承完整对话并在后台处理任务，完成后结果返回当前对话。需要在代理视图打开时可用。
- **场景 1 — 委托子任务**：`/subtask investigate the memory leak in the worker process`
- **场景 2 — 并行探索**：`/subtask analyze all API endpoints in src/`
- **注意**：v2.1.212+；代理视图关闭时不可用

##### `/run` [Skill] 和 `/verify` [Skill]
- **语法**：`/run`， `/verify`
- **功能**：`/run` 启动并驱动项目应用以查看更改的实际效果；`/verify` 通过构建和运行确认代码更改达到预期效果。使用前需先运行 `/run-skill-generator` 教导如何构建、启动和驱动项目应用。
- **场景 1 — 验证 UI 更改**：改完前端代码后 `/run` 启动应用查看效果
- **场景 2 — 提交前验证**：`/verify` 确保改动正常工作
- **注意**：v2.1.145+；需先运行 `/run-skill-generator`

##### `/run-skill-generator` [Skill]
- **语法**：`/run-skill-generator`
- **功能**：教导 `/run` 和 `/verify` 如何从干净环境构建、启动和驱动项目应用。通过编写项目级别的 skill 实现。
- **场景 1 — 设置项目验证**：首次使用 `/run` 前，先 `/run-skill-generator`
- **场景 2 — 更新构建流程**：项目构建方式变化后重新生成
- **注意**：v2.1.145+；需要先于 `/run` 和 `/verify` 运行

> **💡 推荐工作流**：提交前依次运行 `/diff` → `/code-review medium` → `/security-review`，确保代码质量和安全。

---

#### 3.6.6 高级/隐藏命令 (12个)

##### `/btw [question]`
- **语法**：`/btw [问题]`
- **功能**："By the way" 的缩写。快速提问而不添加到主对话中，不消耗上下文也不影响后续任务。相当于开一个临时小窗，问完就关。无工具访问——只能基于已有上下文回答。
- **场景 1 — 临时查询**：正在实现功能时突然想问 `/btw what was the name of that config file again?`
- **场景 2 — 不打断主任务**：Claude 正在工作时，`/btw remind me the project structure` 不影响主任务
- **注意**：无工具访问权限；问题和回答不进入上下文；可在 Claude 正在工作时使用

##### `/teleport`
- **别名**：`/tp`
- **语法**：`/teleport`（打开选择器）
- **功能**：将 Claude Code on the web 会话拉取到当前终端。需要 claude.ai 订阅。让你在 web 上开始的工作无缝转移到终端。
- **场景 1 — Web 到终端**：在 claude.ai/code 上开始的会话，`/teleport` 拉到本地继续
- **场景 2 — 手机到终端**：在手机上开始的会话，`/teleport` 拉到电脑终端
- **注意**：需要 claude.ai 订阅；CLI v2.1.51+

##### `/remote-control`
- **别名**：`/rc`
- **语法**：`/remote-control`
- **功能**：使当前本地会话可从 claude.ai 远程控制。是 `/teleport` 的逆操作。可以从手机或 web 继续/监控终端会话。需要 claude.ai 订阅。
- **场景 1 — 远程监控**：`/remote-control` 让本地长时间任务可以从手机检查
- **场景 2 — 手机操作**：在终端开始工作，`/remote-control` 后从手机继续
- **注意**：需要 claude.ai 订阅；可在 `/config` 中启用 "Enable Remote Control for all sessions"

##### `/desktop`
- **别名**：`/app`
- **语法**：`/desktop`
- **功能**：在 Claude Code Desktop 应用中继续当前会话。将终端会话切换到独立的桌面应用，享受可视化 diff 查看、多个并排会话等功能。仅限 macOS 和 Windows。
- **场景 1 — 切换界面**：终端工作想换到图形界面时 `/desktop`
- **场景 2 — 查看可视化 diff**：桌面应用提供更好的 diff 体验
- **注意**：仅限 macOS 和 Windows；需要 Claude 订阅

##### `/insights`
- **语法**：`/insights`
- **功能**：生成 HTML 报告，分析你的 Claude Code 使用模式。包括项目区域分布、交互模式、摩擦点、技能使用频率、工具使用统计等。帮助你了解自己的编码习惯和改进空间。
- **场景 1 — 使用分析**：`/insights` 查看过去一个月的使用模式
- **场景 2 — 优化工作流**：基于报告发现可以自动化或改进的环节
- **注意**：生成交互式 HTML 报告

##### `/voice [hold|tap|off]`
- **语法**：`/voice [模式]`
- **功能**：切换推送通话语音听写模式。按住 Space 键说话，支持三种模式。大多数 Claude Code 团队成员通过语音编码（语速是打字的 3 倍）。需要 Claude.ai 账户。
- **场景 1 — 语音输入**：`/voice hold` 按住说话方式输入提示
- **场景 2 — 长提示**：语音输入比打字快 3 倍
- **注意**：需要 Claude.ai 账户；仅在 Claude Code Desktop 和 Cowork 可用

##### `/focus`
- **语法**：`/focus`
- **功能**：切换焦点视图，仅显示你最后的提示、带有编辑 diffstats 的单行工具调用摘要和最终响应。v2.1.198+ 工具调用摘要还包括启动的子代理数量。仅在全屏渲染模式下可用。
- **场景 1 — 减少干扰**：`/focus` 隐藏冗长的中间过程，只显示结果
- **场景 2 — 专注审查**：审查输出时开启焦点模式
- **注意**：仅全屏渲染模式可用

##### `/tui [default|fullscreen]`
- **语法**：`/tui [default|fullscreen]`
- **功能**：设置终端 UI 渲染器并以保留对话的方式重新启动 Claude Code。v2.1.110 新增。
- **场景 1 — 全屏模式**：`/tui fullscreen` 获得更好的视觉体验
- **场景 2 — 默认模式**：`/tui default` 回到基础渲染
- **注意**：v2.1.110 新增

##### `/scroll-speed`
- **语法**：`/scroll-speed`（交互式调整）
- **功能**：交互式调整鼠标滚轮滚动速度。打开对话框时有标尺预览效果。持久化到 `~/.claude/preferences.json`。
- **场景 1 — 调整滚动速度**：觉得滚动太快或太��时 `/scroll-speed`
- **注意**：仅在全屏渲染模式下可用；JetBrains IDE 终端中不可用

##### `/heapdump`
- **语法**：`/heapdump`
- **功能**：将 JavaScript 堆快照和内存分解写入 `~/Desktop`（Linux 无 Desktop 文件夹时写入主目录），用于诊断高内存使用情况。**`.heapsnapshot` 文件包含完整对话和凭据，不要分享。**
- **场景 1 — 内存问题诊断**：遇到内存使用过高时 `/heapdump`
- **场景 2 — 提交 bug 报告**：附带堆快照帮助 Anthropic 诊断
- **注意**：**包含完整对话和凭据，绝对不要分享！**

##### `/ultraplan <prompt>`
- **语法**：`/ultraplan <提示>`
- **功能**：在 ultraplan 会话中起草计划，在浏览器中审查，然后远程执行或发送回终端。v2.1.111+ 新增。
- **场景 1 — 大型计划**：`/ultraplan design a microservices architecture for the new payment system`
- **场景 2 — 远程执行**：审查计划后直接在云端执行
- **注意**：v2.1.111+ 新增

##### `/deep-research <question>` [Workflow]
- **语法**：`/deep-research <研究问题>`
- **功能**：对问题展开网络搜索、获取和交叉检查来源，综合生成一份引用报告。使用多代理并行搜索和交叉验证。标记为 Workflow，后台运行。
- **场景 1 — 技术调研**：`/deep-research what are the best practices for real-time data synchronization in 2026`
- **场景 2 — 方案对比**：`/deep-research compare React Server Components vs traditional SSR`
- **注意**：标记为 Workflow，后台运行

> **💡 深度技巧**：以上命令的高级用法和反直觉技巧请参见 [03_tips.md 第 6 章](03_tips.md)，包括 `/btw` 与其他命令配合、`/rewind` 三种模式深度解析、`/loop` 循环批处理等。

---

#### 3.6.7 认证与权限命令 (4个)

##### `/login` 和 `/logout`
- **语法**：`/login`， `/logout`
- **功能**：`/login` 登录到 Anthropic 账户（支持 OAuth 流程）；`/logout` 从 Anthropic 账户登出。
- **场景 1 — 首次设置**：安装 Claude Code 后 `/login`
- **场景 2 — 切换账户**：`/logout` 然后 `/login`
- **注意**：也可在终端运行 `claude auth login` / `claude auth logout`

##### `/privacy-settings`
- **语法**：`/privacy-settings`
- **功能**：查看和更新隐私设置。仅 Pro 和 Max 计划订阅者可用。
- **场景 1 — 管理隐私**：`/privacy-settings` 调整数据共享偏好
- **注意**：仅 Pro/Max 计划可用

##### `/passes`
- **语法**：`/passes`
- **功能**：与朋友分享一周免费的 Claude Code。仅在账户符合条件的用户菜单中显示。
- **场景 1 — 邀请朋友**：`/passes` 生成分享链接
- **注意**：仅在符合条件时可见

---

#### 3.6.8 状态与监控命令 (7个)

##### `/cost`
- **语法**：`/cost`
- **功能**：显示当前会话的 token 使用量和成本估算。实际是 `/usage` 的快捷别名。
- **场景 1 — 监控成本**：`/cost` 查看当前会话花了多少钱
- **场景 2 — 控制开支**：定期 `/cost` 避免超预算
- **注意**：实际是 `/usage` 的快捷别名

##### `/usage`
- **别名**：`/cost`， `/stats`
- **语法**：`/usage`
- **功能**：显示会话费用、计划使用限制和活动统计。包括按 skill、子代理、插件和 MCP 服务器分类的使用量明细（Pro/Max/Team/Enterprise 计划）。
- **场景 1 — 查看用量**：`/usage` 查看计划限制和当前速率限制状态
- **场景 2 — 按分类查看**：了解哪个 skill 使用了最多 token
- **注意**：`/cost` 和 `/stats` 是不同标签页的快捷别名；非交互模式也可用

##### `/stats`
- **语法**：`/stats`
- **功能**：可视化每日使用情况、会话历史记录、连续记录和模型偏好。在 Stats 标签页打开。
- **场景 1 — 使用趋势**：`/stats` 查看过去的使用模式
- **注意**：实际是 `/usage` 的 Stats 标签页快捷方式

##### `/status`
- **语法**：`/status`
- **功能**：在设置界面的 Status 标签页显示版本、模型、账户和连接信息。Claude 响应期间也可使用。
- **场景 1 — 快速检查**：`/status` 查看当前使用哪个模型
- **场景 2 — 诊断连接**：查看连接状态
- **注意**：Claude 响应期间也可用

##### `/statusline`
- **语法**：`/statusline`（无参数时从 shell 提示自动配置）
- **功能**：配置 Claude Code 的状态栏。描述你想要的内容，或不带参数运行以从你的 shell 提示自动配置。状态栏在本地运行，不消耗 API token。
- **场景 1 — 显示模型和上下文**：配置状态栏显示 `[Opus 4.8] 📁 project | 45% context`
- **场景 2 — 自动配置**：`/statusline` 从 shell 提示同步
- **注意**：通过 JSON 数据通过 stdin 传递给脚本；状态栏脚本在本地运行

##### `/usage-credits`
- **语法**：`/usage-credits`
- **功能**：配置使用额度（usage credits）以在达到速率限制时继续工作。Team/Enterprise 无账单权限成员可向管理员发送请求。以前为 `/extra-usage`。
- **场景 1 — 超出限制**：达到速率限制时 `/usage-credits` 配置额度
- **场景 2 — 请求额度**：Team/Enterprise 成员向管理员请求
- **注意**：SSH 等无浏览器时打印 URL（v2.1.205+）

##### `/release-notes`
- **语法**：`/release-notes`（打开交互式版本选择器）
- **功能**：在交互式版本选择器中查看更新日志。选择特定版本查看其发布说明，或选择显示所有版本。
- **场景 1 — 查看更新**：`/release-notes` 了解最新版本有什么新功能
- **场景 2 — 检查修复**：查看特定版本的 bug 修复
- **注意**：v2.1.208 前查看的内容进入对话上下文

---

#### 3.6.9 IDE 与集成命令 (8个)

##### `/ide`
- **语法**：`/ide`
- **功能**：管理 IDE 集成并显示状态。查看 Claude Code 与 VS Code、JetBrains、Cursor 等 IDE 的集成情况。
- **场景 1 — 检查 IDE 状态**：`/ide` 查看 IDE 集成是否正常
- **场景 2 — 配置集成**：管理 IDE 连接
- **注意**：支持 VS Code、JetBrains、Cursor 等多种 IDE

##### `/install-github-app`
- **语法**：`/install-github-app`
- **功能**：为仓库安装 Claude GitHub App，可选设置 GitHub Actions 工作流和密钥。引导你选择仓库并配置集成。
- **场景 1 — 设置 CI**：`/install-github-app` 将 Claude Code 集成到 GitHub Actions
- **场景 2 — 自动化工作流**：配置自动代码审查、PR 管理
- **注意**：引导式设置流程

##### `/install-slack-app`
- **语法**：`/install-slack-app`
- **功能**：安装 Claude Slack 应用，打开浏览器以完成 OAuth 流程。
- **场景 1 — Slack 集成**：`/install-slack-app` 让 Claude 可以与 Slack 交互
- **注意**：打开浏览器完成 OAuth

##### `/chrome`
- **语法**：`/chrome`
- **功能**：配置 Claude in Chrome 设置。让 Claude 可以在 Chrome 中启动浏览器进行前端验证。
- **场景 1 — 前端验证**：`/chrome` 配置后 Claude 可以打开浏览器查看页面效果
- **注意**：需要 Chrome 浏览器

##### `/keybindings`
- **语法**：`/keybindings`
- **功能**：打开或创建键盘快捷键配置文件。可以重新映射任何按键。设置实时重新加载，存储在 `~/.claude/keybindings.json`。
- **场景 1 — 自定义快捷键**：`/keybindings` 打开配置编辑
- **场景 2 — Vim 模式**：设置 Vim 风格的快捷键
- **注意**：配置实时重新加载

##### `/terminal-setup`
- **语法**：`/terminal-setup`
- **功能**：配置终端键盘绑定（Shift+Enter 换行等快捷键）。仅在需要的终端中可见（VS Code、Cursor、Windsurf、Alacritty、Zed）。
- **场景 1 — 启用 Shift+Enter**：多行输入用 `/terminal-setup` 设置
- **场景 2 — IDE 终端优化**：在 VS Code 终端中优化 Claude Code 快捷键
- **注意**：仅在需要它的终端中可见

##### `/web-setup`
- **语法**：`/web-setup`
- **功能**：使用本地 `gh` CLI 凭据将 GitHub 账户连接到 Claude Code on the web。`/schedule` 在 GitHub 未连接时自动提示此操作。
- **场景 1 — 云端权限配置**：使用本地凭据授予云端权限
- **注意**：`/schedule` 会自动提示

##### `/remote-env`
- **语法**：`/remote-env`
- **功能**：为使用 `--remote` 启动的网络会话配置默认远程环境。
- **场景 1 — 选择云代理环境**：`/remote-env` 配置默认的远程环境
- **注意**：用于云端/远程会话配置

---

#### 3.6.10 平台配置命令 (3个)

##### `/setup-bedrock`
- **语法**：`/setup-bedrock`
- **功能**：通过交互向导配置 Amazon Bedrock 认证、区域和模型固定。需要设置 `CLAUDE_CODE_USE_BEDROCK=1`。
- **场景 1 — AWS Bedrock 配置**：`/setup-bedrock` 设置 AWS 认证
- **注意**：需要设置环境变量；仅当 `CLAUDE_CODE_USE_BEDROCK=1` 时可见

##### `/setup-vertex`
- **语法**：`/setup-vertex`
- **功能**：通过交互向导配置 Google Cloud Agent Platform 认证、项目、区域和模型固定。需要设置 `CLAUDE_CODE_USE_VERTEX=1`。
- **场景 1 — Google Cloud 配置**：`/setup-vertex` 设置 GCP 认证
- **注意**：需要设置环境变量；仅当 `CLAUDE_CODE_USE_VERTEX=1` 时可见

##### `/upgrade`
- **语法**：`/upgrade`
- **功能**：在浏览器中打开升级页面以切换到更高计划层级。浏览器打开失败时显示登录提示。Enterprise 计划不显示。
- **场景 1 — 升级计划**：`/upgrade` 从 Pro 升到 Max
- **注意**：Enterprise 计划不显示

---

#### 3.6.11 趣味辅助命令 (6个)

##### `/color [color|default]`
- **语法**：`/color [颜色|default]`（不带参数随机选择）
- **功能**：为当前会话设置提示栏颜色。可用颜色：`red`， `blue`， `green`， `yellow`， `purple`， `orange`， `pink`， `cyan`。Remote Control 连接时颜色同步到 claude.ai/code。
- **场景 1 — 区分会话**：`/color red` 给当前开发任务用红色，`/color blue` 给另一个会话用蓝色
- **场景 2 — 随机颜色**：`/color` 随机选择

##### `/theme`
- **语法**：`/theme`（打开主题选择器）
- **功能**：更改颜色主题。包括自动选项（匹配终端亮/暗背景）、亮色和暗色变体、色盲友好（daltonized）主题、ANSI 主题和使用终端颜色调色板的自定义主题。
- **场景 1 — 切换明暗**：`/theme` 选择 light/dark 主题
- **场景 2 — 色盲友好**：选择 daltonized 主题
- **场景 3 — 自定义**：选择 "New custom theme…" 创建自定义主题（存储在 `~/.claude/themes/`）

##### `/config [key=value ...]`
- **别名**：`/settings`
- **语法**：`/config [key=value ...]`（`--help` 列出所有可设置项）
- **功能**：打开设置界面以调整主题、模型、输出样式、编辑器模式、权限等偏好设置。v2.1.181+ 支持直接传 key=value 设置。
- **场景 1 — 基本设置**：`/config` 打开交互界面
- **场景 2 — 快速设置**：`/config theme=dark model=sonnet`
- **场景 3 — 查看选项**：`/config --help` 查看所有可设置项

##### `/radio`
- **语法**：`/radio`
- **功能**：在浏览器中打开 Claude FM lo-fi 电台。无浏览器时打印流 URL。部��平台不可用（Amazon Bedrock、Google Cloud 等）。
- **场景 1 — 放松**：编码时 `/radio` 打开背景音乐

##### `/stickers`
- **语法**：`/stickers`
- **功能**：订购 Claude Code 贴纸。
- **场景 1 — 收藏贴纸**：`/stickers` 订购 Claude Code 品牌贴纸

##### `/powerup`
- **语法**：`/powerup`
- **功能**：通过带有动画演示的快速交互式课程探索 Claude Code 功能。
- **场景 1 — 学习功能**：`/powerup` 快速了解隐藏功能
- **场景 2 — 新手指南**：新用户通过交互式课程熟悉功能

---

#### 3.6.12 键盘快捷键

| 快捷键 | 操作 |
|--------|------|
| `Enter` | 提交当前提示 |
| `Shift+Enter` | 插入换行符 |
| `Ctrl+C` | 中断 Claude（停止当前轮次）；空提示时退出 |
| `Ctrl+D` | 空提示时退出 |
| `Ctrl+L` | 清除屏幕（保留历史） |
| `Ctrl+R` | 反向搜索提示历史 |
| `Ctrl+O` | 展开到详细完整记录视图 |
| `Up / Down` | 浏览提示历史 |
| `Tab` | 自动完成斜杠命令、文件路径、@-mentions |
| `Esc` | 中断 Claude 响应；取消当前输入/关闭模态框 |
| `Esc, Esc` | 打开 rewind/检查点菜单（回滚到较早时间点） |
| `Shift+Tab` | 循环权限模式: default → acceptEdits → plan → auto → bypassPermissions |
| `Option/Alt + P` | 快速切换模型 |
| `Option/Alt + T` | 切换 Extended Thinking（扩展推理） |
| `@ + 路径` | 在提示中引用文件或目录 |
| `/` | 打开命令菜单 |
| `! command` | 直接执行 shell 命令而不经过 Claude（Bash mode） |
| `& command` | 后台执行 shell 命令 |

---

#### 3.6.13 CLI 启动参数

```bash
# 基础
claude                          # 在当前目录启动交互式会话
claude "query"                  # 带初始提示启动
claude -c                       # 继续最近的会话
claude -r "session-name"        # 按名称恢复会话
claude -n "session-name"        # 为会话命名
claude -p "query"               # 非交互模式，处理后退出
claude --print "query"          # 同 -p

# 模型选择
claude --model sonnet           # 使用 Sonnet
claude --model opus             # 使用 Opus
claude --model haiku            # 使用 Haiku

# 输出控制
claude --verbose                # 显示工具调用细节
claude --quiet                  # 最少输出
claude --output-format json     # JSON 格式输出

# 权限模式
claude --permission-mode plan   # 只读计划模式
claude --permission-mode auto   # AI 决定权限
claude --dangerously-skip-permissions  # 跳过所有权限提示（仅沙箱/容器）
claude --enable-auto-mode       # 包括自动模式在 Shift+Tab 循环中

# 会话控制
claude --no-memory              # 此会话禁用记忆
claude --bare                   # 跳过 hooks、plugins、auto-memory
claude --safe-mode              # 禁用所有自定义配置

# 工作目录与环境
claude --add-dir ../sibling     # 添加额外目录
claude -w feature-branch        # 在隔离的 git worktree 中运行
claude --remote                 # 远程环境启动
claude --teleport               # 从云端拉取会话
claude --from-pr 42             # 恢复链接到 GitHub PR 的会话

# 管道支持
cat file | claude -p "query"    # 处理管道输入

# MCP 管理
claude mcp list                 # 列出 MCP 服务器
claude mcp add name command     # 添加 MCP 服务器
claude mcp remove name          # 移除 MCP 服务器

# 插件
claude plugin list              # 列出插件
claude plugin install name      # 安装插件

# 认证与更新
claude auth login               # 登录
claude auth logout              # 登出
claude auth status              # 查看认证状态
claude update                   # 更新到最新版本
claude doctor                   # 只读诊断（不启动会话）
claude agents                   # 列出所有配置的子代理
```

---

#### 3.6.14 最常用命令 TOP 20 速查

| # | 命令 | 用途 |
|---|------|------|
| 1 | `/help` | 列出所有可用命令 |
| 2 | `/clear` | 清除对话历史 |
| 3 | `/compact` | 压缩对话释放上下文 |
| 4 | `/resume` | 恢复之前的会话 |
| 5 | `/init` | 生成项目 CLAUDE.md |
| 6 | `/model` | 切换 AI 模型 |
| 7 | `/diff` | 查看代码更改 |
| 8 | `/code-review` | 审查代码 diff |
| 9 | `/cost` | 查看 token 使用 |
| 10 | `/context` | 可视化上下文使用 |
| 11 | `/memory` | 编辑项目记忆 |
| 12 | `/plan` | 进入计划模式 |
| 13 | `/permissions` | 管理权限 |
| 14 | `/rewind` | 回滚对话/代码 |
| 15 | `/branch` | 创建对话分支 |
| 16 | `/batch` | 大规模并行更改 |
| 17 | `/doctor` | 诊断安装问题 |
| 18 | `/export` | 导出对话 |
| 19 | `/mcp` | 管理 MCP 服务器 |
| 20 | `/feedback` | 提交反馈 |

#### 3.6.15 工作流推荐命令组合

| 场景 | 命令组合 |
|------|----------|
| 项目首次使用 | `/init` → `/memory` → `/permissions` |
| 大型变更 | `/plan` → 审查方案 → `/diff` → `/code-review` |
| 长会话优化 | `/context` → `/compact focus on ...` |
| 并行工作 | `/batch` → `/workflows` |
| 切换任务 | `/clear old-task-name` → 新工作 → `/resume old-task-name` |
| 发布前检查 | `/diff` → `/code-review high` → `/security-review` |
| 后台自动��� | `/autofix-pr` 或 `/background` → `claude agents` 监控 |
| 会话恢复 | `/resume` 选会话 → `/recap` 回顾进展 |

> **💡 完整 Skills 和自定义命令**：以上为内置命令。下一节（3.6.16）介绍如何创建自己的自定义命令和 Skills。

---

#### 3.6.16 自定义斜杠命令与 Skills

Claude Code 支持两种方式创建自定义命令：

1. **自定义斜杠命令** (Legacy)：`.claude/commands/<name>.md` → 调用 `/name`
2. **Skills** (推荐)：`.claude/skills/<name>/SKILL.md` → 调用 `/name`，支持自动调用

Skills 是推荐的现代方式，支持 YAML frontmatter、配套文件、自动调用等。两种格式的命令文件同时在 `/` 菜单中显示。如果 skill 和 command 同名，skill 优先。

##### Skills 格式（推荐）

**目录结构**：
```
my-skill/
├── SKILL.md          # 主指令文件（必需）
├── template.md       # Claude 填充的模板
├── examples/
│   └── sample.md     # 展示预期格式的示例
└── scripts/
    └── validate.sh   # Claude 可执行的脚本
```

**SKILL.md 格式**：
```markdown
---
description: 简短描述，帮助 Claude 决定何时自动加载
invoke: auto          # auto 或 manual
scope: project        # global 或 project
allowedTools: ["Read", "Glob", "Grep"]
model: sonnet         # 可选，指定模型
---

## Skill 内容

实际指令内容...
$ARGUMENTS
```

**存储位置**：

| 位置 | 作用域 | 可 Git 共享 |
|------|--------|-------------|
| `.claude/skills/<name>/SKILL.md` | 仅当前项目 | 是 |
| `~/.claude/skills/<name>/SKILL.md` | 所有项目 | 否 |
| 企业托管 | 所有组织用户 | 否 |

##### Legacy Commands 格式（CLAUDE.md 内定义）

**创建命令目录**：
```bash
mkdir -p .claude/commands
```

**文件格式**：
```markdown
---
description: 命令描述
allowed-tools: Read, Grep, Bash(git:*)
argument-hint: [required] (optional)
model: sonnet
---

命令内容。$ARGUMENTS 被替换为用户输入。
```

**可用变量**：

| 变量 | 说明 |
|------|------|
| `$ARGUMENTS` | 命令后的所有文本 |
| `$1`, `$2`, `$3` | 位置参数 |
| `` !`bash command` `` | 执行 bash 命令并将输出内联 |
| `@file_path` | 内联文件内容 |

**Frontmatter 字段**：

| 字段 | 说明 |
|------|------|
| `description` | 简短描述，在 `/help` 中显示 |
| `allowed-tools` | 命令可用的工具。格式：`Bash(npm:*)`, `Read`, `Edit` 等 |
| `argument-hint` | 自动完成��显示的提示 |
| `model` | 指定模型 ID |
| `disable-model-invocation` | 设为 `true` 阻止自动调用 |

##### 实战模板

**模板 1：`/ship` — 一键提交推送**
```markdown
---
description: Stage, commit with auto message, push to main
allowed-tools: Bash(git:*), Read
---
!`git diff --stat`
!`git diff --staged --stat`

Review the diff. Run tests. If tests pass, create a commit with a concise message and push to main.
$ARGUMENTS
```

**模板 2：`/review` — 项目级代码审查**
```markdown
---
description: 审查代码变更，检查安全、性能、代码规范
allowed-tools: Read, Grep, Glob, Bash(git:*)
---
审查当前更改，检查以下方面：
- 安全问题（注入、认证间隙、数据暴露）
- TypeScript 错误和类型安全
- 缺少的错误处理
- 公共端点的速率限制
- 性能反模式
$ARGUMENTS
```

**模板 3：`/catchup` — 快速了解当前分支变更**（Skill 格式）
```markdown
---
description: Read all changed files in current git branch and summarize
invoke: manual
scope: project
allowedTools: ["Read", "Bash(git:*)"]
---

Read the git diff of the current branch against main/master.
Summarize all changes and current state. List key files changed and their purpose.
$ARGUMENTS
```

**模板 4：`/optimize` — 项目性能优化建议**
```markdown
---
description: 分析项目性能并提出三个具体优化建议
allowed-tools: Read, Grep, Glob, Bash(npm *), Bash(pnpm *)
---
分析这个项目的性能瓶颈，重点关注：
1. 不必要的数据请求或重渲染
2. 过大的依赖包
3. 未优化的算法复杂度

提出三个具体的优化建议，每个建议包括：问题描述、影响范围、具体修改方案。
$ARGUMENTS
```

**模板 5：`/triage` — Bug 优先级分类**
```markdown
---
description: 对 Issue 或 Bug 列表按严重程度进行分类
allowed-tools: Read, Grep, Bash(gh *)
---
对当前仓库的开放 Issues 进行分类，按严重程度排序：
- P0 - 阻塞性问题（崩溃、数据丢失、安全漏洞）
- P1 - 高优先级（核心功能异常、用户体验严重受损）
- P2 - 中等优先级（功能缺陷但有替代方案）
- P3 - 低优先级（改进建议、文档更新）

输出格式：表格，包含 Issue 编号、标题、严重程度、建议处理人（如可推断）
$ARGUMENTS
```

##### 命令发现与优先级

- 技能目录变化会被实时监测，无需重启
- 同名 skill 优先级高于 legacy command
- 企业 > 个人 > 项目
- 插件 skills 使用 `plugin-name:skill-name` 命名空间
- 使用 `/reload-skills` 让新增/修改的 skills 立即可用

##### 已弃用命令参考

| 命令 | 状态 | 替代方案 |
|------|------|---------|
| `/review [PR]` | 已弃用 | 安装 code-review 插件: `claude plugin install code-review@claude-plugins-official` |
| `/pr-comments [PR]` | v2.1.91 移除 | 直接向 Claude 询问查看 PR 评论 |
| `/vim` | v2.1.92 移除 | 使用 `/config` → Editor mode 切换 Vim/Normal 模式 |
| `/output-style` | v2.1.73 弃用 | 在 `/config` 中设置输出样式 |
| `/extra-usage` | 已重命名 | 使用 `/usage-credits` |

---

## 4. 高级功能

### 4.1 五层扩展架构

Claude Code 的扩展体系由五个层次组成（从底向上）：

```
┌────────────────────────┐
│     Plugins（插件）      │  ← 打包发布层
├────────────────────────┤
│     Hooks（钩子）        │  ← 确定性自动化
├────────────────────────┤
│  Sub-agents（子智能体）    │  ← 并行执行
├────────────────────────┤
│     MCP（协议）          │  ← 外部工具连接
├────────────────────────┤
│     Skills（技能）       │  ← 行为定义
└────────────────────────┘
```

### 4.2 MCP（Model Context Protocol）

MCP 是 Claude Code 连接外部工具与服务的官方扩展协议，支持数据库、API、文件系统、浏览器控制等。

#### MCP 配置

配置存储在以下文件中：

| 作用域 | 配置文件 | 用途 |
|--------|----------|------|
| **用户全局** | `~/.claude.json` | 跨项目的个人工具（如 GitHub、Slack） |
| **项目共享** | `.mcp.json` | 团队共享的工具（提交到 Git） |
| **本地** | `.claude/settings.local.json` | 私密工具（不提交，如连接字符串） |

#### MCP 配置示例

```json
{
  "mcpServers": {
    "postgres": {
      "command": "npx",
      "args": ["@anthropic/pg-mcp", "postgres://localhost/mydb"],
      "env": {"PG_PASSWORD": "secret"}
    },
    "figma": {
      "type": "http",
      "url": "https://mcp.figma.com/mcp"
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "your-token"}
    }
  }
}
```

#### 管理 MCP 服务器

```bash
# 添加 MCP 服务器
claude mcp add my-server -- npx @scope/server-name

# 查看已安装的 MCP 服务器状态
/mcp
```

> **最佳实践：** 保持活跃的 MCP 服务器不超过 3 个（每个服务器会占用上下文窗口）。超过 5 个会明显降低响应速度。

### 4.3 Hooks 自动化钩子

Hooks 是用户定义的 shell 命令，在 Claude Code 生命周期的特定时间点自动执行。它们提供**确定性控制**——不依赖 LLM 记住去执行。

#### Hook 事件类型

| 事件 | 触发时机 | 常见用途 |
|------|----------|----------|
| `PreToolUse` | 工具调用前 | 危险命令拦截、权限检查 |
| `PostToolUse` | 工具调用后 | 代码格式化、自动测试、日志记录 |
| `Notification` | Claude 需要输入时 | 桌面通知 |
| `UserPromptSubmit` | 用户提交提示词时 | 内容过滤、提示词增强 |
| `Stop` | Claude 完成回复时 | 自动后续任务、会话记录 |
| `SubagentStop` | 子智能体完成时 | 子任务结果汇总 |
| `SessionStart` | 会话启动时 | 环境准备、上下文预加载 |
| `PreCompact` | 上下文压缩前 | 保存重要信息 |
| `SessionEnd` | 会话结束时 | 清理操作、保存状态 |

#### 配置 Hooks

Hooks 配置在 `~/.claude/settings.json`（全局）或 `.claude/settings.json`（项目级）：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path' | xargs npx prettier --write"
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "osascript -e 'display notification \"Claude Code needs your attention\" with title \"Claude Code\"'"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "compact",
        "hooks": [
          {
            "type": "command",
            "command": "echo 'Reminder: use Bun, not npm. Run bun test before committing. Current sprint: auth refactor.'"
          }
        ]
      }
    ]
  }
}
```

#### 实用的 Hooks 示例

**1. 编辑后自动格式化代码：**

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "jq -r '.tool_input.file_path' | xargs npx prettier --write 2>/dev/null || true"
      }]
    }]
  }
}
```

**2. 阻止修改受保护文件：**

创建 `.claude/hooks/protect-files.sh`：

```bash
#!/bin/bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
PROTECTED_PATTERNS=(".env" "package-lock.json" ".git/")

for pattern in "${PROTECTED_PATTERNS[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "Blocked: $FILE_PATH matches protected pattern '$pattern'" >&2
    exit 2
  fi
done
exit 0
```

配置 hook：

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/protect-files.sh"
      }]
    }]
  }
}
```

**3. 阻止危险命令：**

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "jq -r '.tool_input.command' | grep -qE 'rm -rf|sudo|chmod 777' && exit 2 || exit 0"
      }]
    }]
  }
}
```

**4. 桌面通知（macOS）：**

```json
{
  "hooks": {
    "Notification": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "osascript -e 'display notification \"Claude Code needs your attention\" with title \"Claude Code\"'"
      }]
    }]
  }
}
```

**5. 每会话加载提醒：**

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "cat ~/.claude/session-reminder.txt 2>/dev/null || echo 'No session reminder found.'"
      }]
    }]
  }
}
```

#### Hook 退出码

| 退出码 | 行为 |
|--------|------|
| `0` | 成功，继续 |
| `2` | 阻止工具/停止操作 |
| 其他 | 记录错误，继续 |

### 4.4 Skills 技能系统

Skills 教 Claude **如何使用**工具。它是包含指令、元数据和可选资源的目录，Claude 按需动态加载。

#### Skill 结构

```
my-skill/
├── SKILL.md       # 必需：包含 YAML frontmatter + Markdown 指令
├── FORMS.md       # 可选：附加参考文档
└── scripts/       # 可选：可执行脚本和资源
```

#### SKILL.md 示例

```markdown
---
name: code-audit
description: 对代码进行安全和性能审计。当用户提到"审计"或"audit"时自动触发。
allowed-tools: "Bash(npm *), Read, Grep"
---

> **YAML 格式提示**：`allowed-tools` 值中包含 `*`、`(`、`)` 等特殊字符时，建议用引号包围整个值，避免解析歧义。如不使用引号，可将 `Bash(npm *)` 改为 `Bash(npm:\\*)` 进行转义。

# 代码审计

## 审计流程
1. 读取目标文件或目录
2. 检查 OWASP Top 10 安全风险
3. 检查性能反模式
4. 生成审计报告，按严重程度排序

## 示例
用户："审计 src/auth/ 的安全性"
Claude 会读取该目录下文件，检查 SQL 注入、XSS 等漏洞。
```

#### Skills 的存储位置

| 类型 | 路径 | 共享范围 |
|------|------|----------|
| 个人 Skills | `~/.claude/skills/my-skill-name/` | 所有项目 |
| 项目 Skills | `.claude/skills/my-skill-name/` | 团队成员（通过 git） |
| 插件 Skills | 通过插件安装 | 依赖插件配置 |

#### Skills 的三级加载机制

| 级别 | 加载内容 | 加载时机 |
|------|----------|----------|
| **Level 1：元数据** | YAML frontmatter（name、description） | 始终加载（启动时） |
| **Level 2：指令** | SKILL.md 正文内容 | 当用户请求与描述匹配时触发 |
| **Level 3：资源** | 附加文件、脚本等 | 按需加载 |

### 4.5 Sub-agents 子智能体

Sub-agents 将大型复杂任务自动分解为可独立执行的子任务，分配给专门的 AI 智能体并行处理。

#### 内置子智能体类型

| 类型 | 功能 |
|------|------|
| **Explore 智能体** | 快速遍历和分析代码库结构 |
| **Plan 智能体** | 设计清晰的项目实现方案 |
| **Bash 智能体** | 专注执行命令行相关任务 |

#### 自定义子智能体

在 `~/.claude/agents/` 下创建智能体定义文件：

```markdown
---
name: security-reviewer
model: sonnet
effort: high
maxTurns: 15
tools: Read, Grep, Glob, Bash(npm audit *)
skills: code-audit
---

你是一个安全审查专家。
分析给定的代码变更，检查 OWASP Top 10 漏洞和依赖风险。
输出结构化报告。
```

#### 工作原理

- 子智能体在**隔离的上下文窗口**中运行，不会污染主会话
- Claude Code 根据任务自动调度对应子智能体
- 启用 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 可让智能体共享任务队列并协调依赖

### 4.6 多文件编辑

Claude Code 天然支持多文件编辑：

```bash
# 跨文件重构
claude -p "将所有的 'var' 声明改为 'let' 或 'const'"

# 批量修改
> 在所有 React 组件中添加 TypeScript 返回类型注解。

# 架构级变更
> 将认证逻辑从 src/auth 迁移到 src/security/auth，更新所有导入路径。
```

### 4.7 终端命令执行

Claude Code 可以代你执行终端命令（需审批）：

```bash
# 运行测试
> 运行所有测试并告诉我哪些失败了。

# 构建项目
> 用生产配置构建项目。

# 安装依赖
> 安装 @tanstack/react-query 并更新相关代码。
```

### 4.8 图片/截图理解

Claude Code 支持视觉输入（多模态），可以直接分析图片、截图和架构图。这是最被低估的功能之一——一张截图胜过千言万语。

#### 三种输入方式

| 方式 | 操作方法 | 适用场景 |
|------|---------|---------|
| **剪贴板粘贴** | macOS: `Ctrl+V` (不是 Cmd+V)<br>Windows: `Alt+V` | 快速发送截图，无需保存文件 |
| **拖放** | 拖动图片文件到终端窗口 | 已有图片文件时最方便 |
| **文件路径引用** | 直接输入图片路径：`分析这张截图: /path/to/bug.png` | 远程 SSH 环境、自动化脚本 |

> **macOS 最快捷工作流**：`Cmd+Ctrl+Shift+4`（截图到剪贴板）→ `Ctrl+V`（粘贴）。注意粘贴用的是 **Ctrl** 不是 Cmd，这是大多数用户踩过的坑。

#### 支持格式

| 格式 | 适用场景 | 说明 |
|------|---------|------|
| **PNG** | 截图、UI、文本 | 清晰渲染，支持透明度 |
| **JPEG** | 照片 | 良好压缩 |
| **GIF** | 简单图形 | 仅分析第一帧 |
| **WebP** | 现代替代方案 | 最佳压缩比 |

> 不支持格式：BMP、TIFF、SVG（会被静默丢弃）。

#### 文件限制

| 环境 | 文件大小 | 分辨率 | 说明 |
|------|---------|--------|------|
| **Claude Code (API)** | 每张 5MB | 最大 8000×8000 px | API 标准限制 |
| **claude.ai 聊天** | 每张 30MB | 最大 8000×8000 px | 网页端更宽松 |
| **多图场景** | — | 超 20 张时每张限 2000×2000 px | 避免上下文溢出 |

> 长边超过 1568px 的图片会被自动等比缩放。Anthropic 建议调整到约 1.15 兆像素以内以获得最佳响应速度。

#### Token 消耗

图片消耗 tokens，公式：`tokens ≈ (宽 × 高) / 750`

| 图片尺寸 | Token 数 | 每千张成本 (Sonnet 4.5) |
|---------|---------|----------------------|
| 200×200 px | ~54 | ~$0.16 |
| 1000×1000 px | ~1,334 | ~$4.00 |
| 1092×1092 px | ~1,590 | ~$4.80 |

> 发送前裁剪到相关区域。全屏截图比只截取问题区域的 token 成本高得多。

#### 典型使用场景

```text
# UI 调试
[粘贴错误截图的按钮] 修复这个按钮的对齐问题。

# 设计转代码
[粘贴设计稿] 用 React + Tailwind 实现这个卡片组件。匹配间距和排版。

# 架构图分析
[粘贴架构图] 为这个微服务架构生成 TypeScript 接口定义。

# 迭代优化
[粘贴设计稿 + 当前实现截图] 对比这两张图，修复差异。
```

#### 最佳实践

1. **图片放在文字前面**——Claude 处理效果更好
2. **裁剪到相关区域**——减少 token 消耗，提高精度
3. **清晰的图片效果最好**——模糊、低对比度、旋转或高度压缩的图片分析结果不可靠
4. **可从终端点开图片链接**——`[Image #N]` 链接可通过 `Cmd+Click` (macOS) / `Ctrl+Click` (Windows) 点开查看
5. **图片输入失败时**：按 `Esc` 两次回退→裁剪/缩放到 2000px 以内→重试

### 4.9 深入思考模式

对于复杂问题，可以启用深度思考模式：

```bash
# 在会话中切换
/fast    # 快速模式
# /think  # 深度思考模式（如支持）
```

### 4.10 自定义斜杠命令

> **📖 本节内容已移入 3.6.16 节并大幅扩展。** 请参见 [3.6.16 自定义斜杠命令与 Skills](#3616-自定义斜杠命令与-skills)，包含 Skills 格式（推荐）、Legacy Commands 格式、frontmatter 配置和 5 个实战模板。

---

## 5. 项目管理

### 5.1 CLAUDE.md 最佳实践

#### 全局 vs 项目级 CLAUDE.md

| 放哪儿 | 放什么 |
|--------|--------|
| **全局（~/.claude/CLAUDE.md）** | 你的沟通风格偏好、通用硬规则（"禁止手动编辑 lockfile"）、机器备注（Shell 类型、编辑器）、全局工具路径 |
| **项目（./CLAUDE.md）** | 技术栈、构建/测试命令、编码规范、项目架构、文件结构概述、项目特有规则 |

**原则：** 属于**你**的放全局，属于**项目**的放项目文件。

#### CLAUDE.md 编写要点

1. **保持 200 行以内** —— 太长会被忽略或挤占上下文
2. **写事实不要写目标** —— 写"当前阶段：数据库 schema 未定"，不写"计划支持 PostgreSQL"
3. **添加反模式** —— 对此代码库特有的禁止做法
4. **当前开发阶段** —— 让 Claude 知道什么工作是在范围内的
5. **关键参考文件路径** —— 避免每次粘贴路径
6. **逐步完善** —— 不要一次写完，遇到问题时再添加

### 5.2 多项目切换

#### 最佳实践：一个会话 = 一个仓库

**不要**在同一个会话中用 `cd` 切换项目。这会导致：
- 文件缓存冲突
- 系统提示稀释（CLAUDE.md 只在启动时加载）
- 对话污染（Repo A 的决策泄露到 Repo B）

#### 推荐方案

```
终端 Tab 1：repo-a/ → claude    （API 服务）
终端 Tab 2：repo-b/ → claude    （前端应用）
终端 Tab 3：repo-c/ → claude    （共享库）
```

实测效果（2 周 A/B 对比）：

| 工作流 | 平均 Token/任务 | 返工率 | 主观烦恼度 |
|--------|----------------|--------|-----------|
| 单会话切换目录 | 42,800 | 31% | 4.1/5 |
| 每个仓库独立会话 | 18,600 | 8% | 1.8/5 |

节省了 57% 的 Token 消耗，返工减少 4 倍。

#### 跨仓库工作

对于需要跨仓库的变更（如 API 端点重命名），使用"编排器"会话：

```bash
# 在项目目录外启动一个编排会话
cd ~/workspace
claude -p "在 repo-a 和 repo-b 中探索，了解它们的 API 调用关系"
```

然后在各自的仓库会话中分别实现。

#### 多项目目录结构推荐

```text
~/workspace/
├── projects/
│   ├── active/
│   │   ├── project-a/
│   │   │   ├── CLAUDE.md
│   │   │   ├── .claude/
│   │   │   │   ├── settings.json
│   │   │   │   └── commands/
│   │   │   └── src/
│   │   ├── project-b/
│   │   └── project-c/
│   └── archive/
├── shared/
└── scripts/
```

### 5.3 会话管理

#### 会话恢复

```bash
# 列出最近会话
claude --resume

# 恢复特定会话
claude --resume <session-id>

# 恢复最近会话
claude --continue
```

#### 会话管理最佳实践

1. **一个任务 = 一个会话** — 一个功能、一个 Bug 修复、一个调查
2. **当天开新会话** — 每天开始时用新会话，重要上下文应在 git 或文档中
3. **定期 /compact** — 当会话变长时压缩上下文
4. **使用 /clear** — 完成主要任务后清理会话
5. **使用 /checkpoint** — 在大操作前创建检查点

#### 内存系统

Claude Code 有两套互补的记忆系统：

| 系统 | 谁写入 | 内容 | 加载时机 |
|------|--------|------|----------|
| **CLAUDE.md** | 你 | 指令和规则 | 每次会话 |
| **自动记忆** | Claude | 学到的模式和偏好 | 每次会话（前 200 行或 25KB） |

Claude 会从你的纠正中学习并自动记录。记忆文件保存在 `~/.claude/projects/` 中。

### 5.4 /init 智能初始化

设置 `CLAUDE_CODE_NEW_INIT=1` 启用交互式多阶段初始化：

```bash
export CLAUDE_CODE_NEW_INIT=1
claude
```

然后运行 `/init`，Claude 会：
1. 询问要设置哪些组件（CLAUDE.md、skills、hooks）
2. 用子智能体探索代码库
3. 通过追问补充缺失信息
4. 生成可审查的方案，确认后才写入文件

---

## 6. 常见问题与排错

### 6.1 安装问题

#### "command not found" 或安装失败

```bash
# 检查 Node.js 版本（npm 方式）
node --version  # 需要 18.0+

# 将 npm 全局 bin 添加到 PATH
echo 'export PATH="$(npm config get prefix)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 清除 npm 缓存后重试
npm cache clean --force
npm install -g @anthropic-ai/claude-code
```

#### "Permission denied" / EACCES 错误

```bash
# macOS/Linux 修复 npm 权限
sudo chown -R $(whoami) ~/.npm

# Windows 以管理员身份运行命令提示符
# 然后执行
npm install -g @anthropic-ai/claude-code
```

#### 安装卡住或超时

```bash
# 使用其他 npm 源
npm install -g @anthropic-ai/claude-code --registry https://registry.npmjs.org/

# 清除缓存
npm cache clean --force
```

### 6.2 认证问题

#### "Invalid API key" 或认证失败

```bash
# 方法 1：使用 /login 命令重新认证（支持 OAuth 和 API Key）
# 在 Claude Code 交互会话中执行：
#   /login

# 方法 2：通过环境变量设置 API Key
# Linux/macOS:
export ANTHROPIC_API_KEY=sk-ant-xxx
# Windows (PowerShell):
$env:ANTHROPIC_API_KEY="sk-ant-xxx"

# 检查环境变量是否生效
echo $ANTHROPIC_API_KEY

# 确保密钥没有多余的空格或字符
# 重新生成 API Key（在 console.anthropic.com）
```

#### OAuth 登录循环

1. 确保登录了正确的 Claude 账户
2. 清除浏览器 cookie 并重试认证
3. 使用无痕/私密模式浏览器
4. 运行 `/login` 重新认证

### 6.3 运行时错误

#### API Error: 529 Overloaded（服务器过载）

```
API Error: 529 {"type":"error","error":{"type":"overloaded_error"...
```

**解决：**
- 等待 2-5 分钟，这是临时性服务器问题
- 不要重装 Claude Code
- 或切换到 Sonnet 模型

#### API Error: 400 invalid_request_error

```
API Error: 400 {"type":"error","error":{"type":"invalid_request_error"...
```

**解决：**
- 按 `Esc + Esc` 回退到上一条消息重试
- 或按 `Ctrl+C` 强制退出，新窗口重启

#### 请求超时

**原因：** 任务复杂度过高或启用了深度思考模式

**解决：**
- 将任务拆分为多个子任务
- 优化提示词
- 使用 `--max-turns` 限制执行轮数

```bash
claude --max-turns 5 -p "your query"
```

#### Context window full（上下文满）

```bash
# 压缩对话（保留关键信息）
/compact

# 或清空重新开始
/clear

# 或带指令的压缩
/compact keep only the main function names and error messages
```

### 6.4 性能问题

#### 高 CPU 或内存使用

```bash
# 定期压缩上下文
/compact

# 在任务间隙重启 Claude Code
# 将大型构建目录添加到 .gitignore
# 使用安全模式检查是否为插件/MCP/hook 问题
claude --safe-mode
```

#### 响应缓慢

```bash
# 切换到更快模型
/fast

# 减少上下文
/compact

# 将大文件拆分为小文件
# 确保 16GB+ RAM
# Linux/macOS 原生环境比 WSL 更流畅
```

### 6.5 配置问题

#### 设置未应用、Hooks 未触发、MCP 服务器未加载

```bash
# 运行诊断
/doctor

# 检查 MCP 状态
/mcp

# 检查 Hooks 配置
/hooks

# 启用调试模式
claude --debug
```

```bash
# 检查配置文件语法
cat ~/.claude.json | python -m json.tool
cat .mcp.json | python -m json.tool
```

### 6.6 平台特定问题

#### WSL 问题

```bash
# 更新 WSL2
wsl --update

# 文件应存储在 Linux 文件系统中（/home/，不要用 /mnt/c/）
# 配置 .wslconfig（在 Windows 用户目录下）：
# [wsl2]
# memory=8GB
# processors=4
```

#### Windows 原生问题

- 推荐安装 Git for Windows，Claude Code 可使用 Bash 工具
- 如未安装 Git for Windows，Claude Code 会用 PowerShell 作为 shell 工具
- 检查命令提示符前缀：`PS C:\` 表示 PowerShell，`C:\` 表示 CMD

#### macOS 权限问题

```bash
# 授予终端全盘访问权限（系统偏好设置 → 安全与隐私）
# 使用原生安装器
curl -fsSL https://claude.ai/install.sh | bash
```

### 6.7 会话恢复

```bash
# 列出最近会话
claude --resume

# 恢复特定会话
claude --resume <session-id>

# 继续最近会话（从上次停下的地方继续）
claude --continue
```

会话文件位置：
- `~/.claude/sessions/` — 会话历史
- `~/.claude/auth/` — 认证 token
- `~/.claude/cache/` — 临时缓存

> 安全重置：删除 `auth/`、`sessions/`、`cache/` 目录即可清理。

### 6.8 性能最佳实践

1. **任务拆分** — 将复杂任务拆分为小问题
2. **主动清理** — 每完成一个主要任务就 `/clear`
3. **手动 Git** — 让 Claude 编辑文件，自己管理提交
4. **监控使用** — 定期使用 `/cost` 和 `/status`
5. **大文件** — Claude Code 处理大文件的能力优于竞品

---

## 7. 与 IDE 对比：Claude Code vs Cursor vs Copilot

### 7.1 核心差异总览

| 维度 | Claude Code | GitHub Copilot | Cursor |
|------|-------------|----------------|--------|
| **界面** | 终端 CLI | IDE 插件 | IDE（VS Code 分支） |
| **内联补全** | 无 | ✅（业界最佳） | ✅ |
| **Agent 模式** | ✅（核心功能） | 有限 | ✅（Composer） |
| **项目配置** | CLAUDE.md | 无等价物 | .cursorrules |
| **编辑器支持** | 任意编辑器 | VS Code、JetBrains | 仅 Cursor IDE |
| **外部工具访问** | MCP 服务器（最强） | 仅 GitHub | 有限 |
| **价格** | 按用量计费 | ~$10-19/月 | ~$20/月 |
| **上下文窗口** | 最高 1M token（取决于模型和计划）<br>Sonnet 5/Fable 5: 原生 1M<br>Opus 4.6+: Pro 需额度方可 1M<br>旧模型: 200K | 最高 128K（因模型而异） | 代码库索引 |
| **最适合** | 复杂任务、重构、架构 | 行级完成、模板代码 | 可视化 AI 编辑 |

### 7.2 工作流差异

#### Claude Code 工作流

```
终端驱动 → 读整个代码库 → 多文件编辑 → 运行命令 → 迭代测试 → 提交
```

- 适合**大型重构、多文件变更、运行构建和测试作为编码循环的一部分**
- 支持无头模式用于 CI/CD：`claude -p "审查此 PR" --output-format json`
- 可以通过 Unix 管道与任何工具链组合

#### GitHub Copilot 工作流

```
IDE 内 → 行级提示 → 手动文件切换 → 增量构建
```

- 适合**实时内联补全**、行级别编码辅助
- 需要开发者自己驱动整体结构

#### Cursor 工作流

```
Composer 面板 → 标记文件 → 预览 diff → 逐条审查 → 接受/拒绝 → 应用
```

- 适合**可视化的 AI 编辑**、在 diff 预览中逐文件审查
- 介于 Copilot 的内联辅助和 Claude Code 的全自主之间

### 7.3 CLAUDE.md 的独特优势

没有其他工具拥有等价的 CLAUDE.md 系统：

- **Cursor 的 .cursorrules** — 功能更弱，仅限 Cursor IDE
- **Copilot 无等价物** — 只有 `.github/copilot-instructions.md`

CLAUDE.md 是**可读的 Markdown**，**可通过版本控制共享**，**跨所有 Claude 接口**（CLI、IDE、Web、桌面）工作。对团队而言，这通常是决定性的因素。

### 7.4 如何选择

| 场景 | 推荐工具 |
|------|----------|
| 大型重构、架构决策、复杂调试 | **Claude Code** |
| 日常行级编码、模板代码生成 | **Copilot** |
| 可视化 diff 审查、前端/全栈开发 | **Cursor** |
| 团队协作、需要持久化项目配置 | **Claude Code** |
| 深度嵌入 GitHub 生态 | **Copilot** |

### 7.5 组合使用策略

许多专业开发者**同时使用多个工具**：

```
Claude Code（重活）+ Copilot（日常补全）
Claude Code（架构/重构）+ Cursor（可视化编辑）
Cursor（编辑器）+ Claude Code（终端复杂任务）
```

> **建议：** 从 Claude Code + Copilot 开始。Claude Code 处理繁重工作，Copilot 处理模板代码。多数开发者反馈这种组合的产出远超单独使用任一工具。

---

## 8. CI/CD 集成实践

Claude Code 支持无头模式（Headless Mode），可通过命令行参数在 CI/CD 流水线中自动化执行代码审查、测试修复、PR 总结等任务。

### 8.1 无头模式基础

```bash
# 单次执行模式（不进入交互）
claude -p "review this PR for security issues" --output-format json

# 限制最大执行轮数（防止 CI 超时）
claude -p "fix failing tests" --max-turns 10

# 免授权模式（CI 环境必需）
claude -p "generate release notes" --dangerously-skip-permissions

# 指定模型（CI 中推荐使用 Sonnet 5 平衡速度与质量）
claude -p "review code changes" --model sonnet
```

### 8.2 认证配置

在 CI/CD 环境中，Claude Code 通过 API Key 或 OAuth Token 认证：

```bash
# 方式一：环境变量认证（推荐）
export ANTHROPIC_API_KEY="${{ secrets.ANTHROPIC_API_KEY }}"

# 方式二：API Key 文件认证
echo "${{ secrets.ANTHROPIC_API_KEY }}" > ~/.claude-api-key
export ANTHROPIC_API_KEY_FILE=~/.claude-api-key
```

> **安全提示**：永远不要在 CI 配置文件中硬编码 API Key。使用 GitHub Secrets、GitLab Variables 或 CI 平台的密钥管理功能。

### 8.3 GitHub Actions 示例

#### 示例 1：PR 自动化代码审查

```yaml
# .github/workflows/claude-code-review.yml
name: Claude Code PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0  # 获取完整历史以计算 diff

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Run Claude Code Review
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          # 获取 PR 的变更文件列表
          git diff --name-only origin/${{ github.base_ref }}...HEAD > changed_files.txt

          # 执行代码审查
          claude -p "Review the code changes in this PR for:
          1. Security vulnerabilities (OWASP Top 10)
          2. Performance issues
          3. Code style violations
          4. Potential bugs
          Files changed: $(cat changed_files.txt).
          Output the review as a structured markdown report." \
            --max-turns 15 \
            --output-format json \
            > review.json

      - name: Post Review Comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const review = JSON.parse(fs.readFileSync('review.json', 'utf8'));
            const body = `## 🤖 Claude Code 自动审查\n\n${review.result || review.content}`;
            await github.rest.issues.createComment({
              ...context.repo,
              issue_number: context.issue.number,
              body: body
            });
```

#### 示例 2：CI 失败自动修复

```yaml
# .github/workflows/claude-code-auto-fix.yml
name: Claude Code Auto-Fix

on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
    branches: [main, develop]

jobs:
  auto-fix:
    # 仅在 CI 失败时运行
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install dependencies
        run: npm ci

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Auto-fix CI failures
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          # 重新运行测试，捕获错误输出
          npm test 2>&1 | tee test-output.log

          # 让 Claude Code 分析并修复失败
          claude -p "The CI pipeline failed. Here is the test output:
          $(cat test-output.log)
          Read the failing test files, identify the root cause, and fix the issues.
          Do NOT modify any test assertions that were intentionally written.
          After fixing, verify by running 'npm test'." \
            --max-turns 20 \
            --dangerously-skip-permissions

      - name: Create fix PR
        uses: peter-evans/create-pull-request@v6
        with:
          title: "🤖 Auto-fix: CI failures"
          body: "Automated fix by Claude Code for CI pipeline failures."
          branch: auto-fix/ci-${{ github.run_id }}
          labels: automated, claude-code
```

#### 示例 3：自动生成 Release Notes

```yaml
# .github/workflows/claude-code-release-notes.yml
name: Generate Release Notes

on:
  release:
    types: [created]

jobs:
  release-notes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Generate Release Notes
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          # 获取上一个 tag 到当前的 commits
          PREV_TAG=$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo "")
          if [ -n "$PREV_TAG" ]; then
            git log $PREV_TAG..HEAD --oneline > commits.txt
          else
            git log --oneline > commits.txt
          fi

          claude -p "Generate release notes from these commits:
          $(cat commits.txt)
          Format as:
          ## 🚀 Features
          ## 🐛 Bug Fixes
          ## 🛠️ Maintenance
          ## 📝 Documentation
          Use emoji and keep descriptions concise." \
            --max-turns 5 \
            > RELEASE_NOTES.md

      - name: Update Release
        uses: ncipollo/release-action@v1
        with:
          bodyFile: RELEASE_NOTES.md
          allowUpdates: true
```

### 8.4 GitLab CI 示例

```yaml
# .gitlab-ci.yml
variables:
  CLAUDE_MAX_TURNS: "10"

# PR 代码审查
claude-code-review:
  image: node:20
  stage: review
  only:
    - merge_requests
  variables:
    GIT_DEPTH: 0
  before_script:
    - npm install -g @anthropic-ai/claude-code
  script:
    - |
      # 获取 MR 变更文件
      git diff --name-only origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME...HEAD > changed_files.txt

      # 执行审查
      claude -p "Review the code changes in this MR for security, performance, and code quality issues.
      Changed files: $(cat changed_files.txt)" \
        --max-turns $CLAUDE_MAX_TURNS \
        --dangerously-skip-permissions \
        > review.md

      # 发布审查评论到 MR
      cat review.md | glab mr note $CI_MERGE_REQUEST_IID --file -
  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY
    GITLAB_TOKEN: $GITLAB_TOKEN

# CI 失败自动修复
claude-code-auto-fix:
  image: node:20
  stage: post-test
  when: on_failure
  only:
    - main
    - develop
  before_script:
    - npm install -g @anthropic-ai/claude-code
    - npm ci
  script:
    - npm test 2>&1 | tee test-output.log
    - |
      claude -p "Fix the failing tests. Output: $(cat test-output.log).
      Only modify source code, not test expectations." \
        --max-turns 15 \
        --dangerously-skip-permissions
    - |
      # 创建自动修复分支
      git checkout -b auto-fix/ci-$CI_PIPELINE_ID
      git add -A
      git commit -m "Auto-fix: CI failures [skip ci]"
      git push origin auto-fix/ci-$CI_PIPELINE_ID \
        -o merge_request.create \
        -o merge_request.title="🤖 Auto-fix: CI failures" \
        -o merge_request.label="automated,claude-code"
  variables:
    ANTHROPIC_API_KEY: $ANTHROPIC_API_KEY
```

### 8.5 CI/CD 最佳实践

| 实践 | 说明 |
|------|------|
| **限制 `max-turns`** | CI 环境必须设置 `--max-turns` 防止无限循环超时（推荐 10-20 轮） |
| **使用 `--dangerously-skip-permissions`** | CI 中无人交互，必须使用免授权模式 |
| **专用 API Key** | 为 CI 创建专用 API Key，设置使用上限和 IP 限制 |
| **Sonnet 5 优先** | CI 中速度优先，Sonnet 5 平衡质量与成本 |
| **沙箱环境** | CI 任务在隔离容器中运行，安全风险可控 |
| **失败告警** | 配置 CI/CD 在 Claude Code 步骤失败时发送通知 |
| **成本监控** | 为 CI API Key 设置月度预算上限，防止意外超支 |
| **人工审核** | 自动修复生成的 PR 应标注为需要人工 Review |

---

## 9. Docker 使用指南

Claude Code 支持在 Docker 容器中运行，适用于开发环境标准化、CI/CD 集成和团队一致性保障。

### 9.1 Docker 镜像安装

Claude Code 不提供官方 Docker 镜像，但可以通过以下方式在容器中使用：

#### 基于 Node.js 镜像安装

```dockerfile
# Dockerfile
FROM node:20-slim

# 安装依赖
RUN apt-get update && apt-get install -y \
    git \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# 安装 Claude Code
RUN npm install -g @anthropic-ai/claude-code

# 设置工作目录
WORKDIR /workspace

# 挂载项目代码
VOLUME ["/workspace"]

# 入口点
ENTRYPOINT ["claude"]
```

构建并运行：

```bash
# 构建镜像
docker build -t claude-code .

# 运行交互模式
docker run -it --rm \
  -v $(pwd):/workspace \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  claude-code

# 运行单次命令
docker run --rm \
  -v $(pwd):/workspace \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  claude-code -p "analyze the project structure"
```

#### 使用官方安装脚本（原生二进制）

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    git curl jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 使用官方脚本安装原生版本
RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /workspace
ENTRYPOINT ["claude"]
```

### 9.2 Devcontainer 配置

适用于 VS Code Dev Containers 扩展，为团队提供一致的开发环境：

```json
// .devcontainer/devcontainer.json
{
  "name": "Claude Code Dev",
  "image": "mcr.microsoft.com/devcontainers/typescript-node:20",

  "features": {
    "ghcr.io/devcontainers/features/git:1": {},
    "ghcr.io/devcontainers/features/github-cli:1": {}
  },

  "postCreateCommand": "npm install -g @anthropic-ai/claude-code",

  "containerEnv": {
    "ANTHROPIC_API_KEY": "${localEnv:ANTHROPIC_API_KEY}"
  },

  "mounts": [
    "source=${localWorkspaceFolder},target=/workspace,type=bind",
    // 可选：挂载本地 Claude Code 配置
    "source=${localEnv:HOME}/.claude,target=/home/node/.claude,type=bind"
  ],

  "customizations": {
    "vscode": {
      "extensions": [
        "anthropic.claude-code"
      ]
    }
  }
}
```

使用 devcontainer：

```bash
# 在 VS Code 中
# 1. 打开项目文件夹
# 2. Ctrl+Shift+P → "Dev Containers: Reopen in Container"
# 3. 容器启动后，Claude Code 已自动安装，可直接在终端中使用

# CLI 方式
devcontainer open .
```

### 9.3 Docker Compose 集成

```yaml
# docker-compose.yml
version: '3.8'

services:
  claude-code:
    build:
      context: .
      dockerfile: Dockerfile
    volumes:
      - .:/workspace
      # 可选：持久化 Claude Code 配置
      - claude-config:/home/node/.claude
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    working_dir: /workspace
    stdin_open: true  # 交互模式必需
    tty: true         # 交互模式必需

  # 示例：包含数据库的开发环境
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: myapp
      POSTGRES_PASSWORD: ${DB_PASSWORD:-devpassword}
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data

volumes:
  claude-config:
  postgres-data:
```

```bash
# 启动完整开发环境
docker compose up -d postgres

# 启动 Claude Code 容器
docker compose run --rm claude-code
```

### 9.4 Docker 环境注意事项

| 注意项 | 说明 | 解决方案 |
|--------|------|----------|
| **文件路径** | 容器内路径与宿主机不同。`/workspace` 挂载点需在 CLAUDE.md 中使用容器内路径 | CLAUDE.md 中统一使用相对路径（如 `src/` 而非 `/host/path/src/`） |
| **Git 配置** | 容器内 Git 用户信息可能缺失 | 在 Dockerfile 中设置或挂载 `.gitconfig`：`git config --global user.name/email` |
| **文件权限** | Linux 容器中创建的文件可能属于 root，宿主机无法编辑 | 使用 `--user` 参数匹配宿主机 UID：`docker run --user $(id -u):$(id -g) ...` |
| **网络访问** | 容器内 `localhost` 指向容器自身，非宿主机 | 使用 `host.docker.internal`（Mac/Windows）或 `--network host`（Linux）访问宿主机服务 |
| **Shell 差异** | 基础镜像可能缺少 bash/zsh | Dockerfile 中确保 `apt-get install bash` |
| **MCP 服务器** | 容器中需额外安装 MCP 依赖 | 在 Dockerfile 中预装常用 MCP 的 npm 包 |
| **认证** | API Key 通过环境变量传递，不持久化在镜像中 | 使用 `-e` 或 `--env-file` 传递敏感信息 |
| **性能** | 文件系统挂载（尤其是 macOS）可能有 I/O 开销 | 大型项目考虑使用 Docker Volume 或 `:cached` 挂载选项 |

### 9.5 Docker + CI/CD 组合使用

```yaml
# .github/workflows/claude-code-docker.yml
name: Claude Code Docker CI

on: [pull_request]

jobs:
  review-in-docker:
    runs-on: ubuntu-latest
    container:
      image: node:20
      options: --user root
    steps:
      - uses: actions/checkout@v4
      - run: npm install -g @anthropic-ai/claude-code
      - name: Review
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          git diff --name-only origin/${{ github.base_ref }}...HEAD > files.txt
          claude -p "Review changed files: $(cat files.txt)" \
            --max-turns 10 --dangerously-skip-permissions > review.md
```

---

## 参考资源

- [Anthropic 官方文档](https://docs.anthropic.com/en/docs/claude-code/overview)
- [Claude Code 快速入门](https://docs.anthropic.com/en/docs/claude-code/quickstart)
- [故障排除指南](https://docs.anthropic.com/en/docs/claude-code/troubleshooting)
- [Hooks 官方参考](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [MCP 官方文档](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Skills 官方文档](https://docs.claude.com/en/docs/claude-code/skills)
- [Claude Code 社区资源](https://github.com/hesreallyhim/awesome-claude-code)

---

## 修复记录

- 2026-07-26：修复编码乱码（C1）
  - L714 `依赖插件��置` → `依赖插件配置`
  - L763 `天��支持多文件编辑` → `天然支持多文件编辑`
  - L777 `可���代你执行终端命令` → `可以代你执行终端命令`
- 2026-07-26（第2轮 QA 修复）：
  - **NH3**：为 4.4 节 SKILL.md 示例的 `allowed-tools` 字段添加双引号包围，并补充 YAML 格式注意事项
  - **关联修复**：更新 7.1 节上下文窗口数据 200K → 1M token（Max/Team/Enterprise）
