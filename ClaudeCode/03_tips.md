# Claude Code 使用技巧、最佳实践与效率秘籍

> 整理自 Anthropic 官方文档、Reddit、Twitter/X、Hacker News、技术博客等社区精华。
> 最后更新：2026年7月

---

## 目录

1. [CLI 效率技巧](#1-cli-效率技巧)
2. [Prompt 工程技巧](#2-prompt-工程技巧)
3. [代码编辑技巧](#3-代码编辑技巧)
4. [配置优化](#4-配置优化)
5. [实战案例](#5-实战案例)
6. [隐藏功能与冷知识](#6-隐藏功能与冷知识)
7. [社区推荐](#7-社区推荐)

---

## 1. CLI 效率技巧

### 1.1 核心快捷键速查

| 快捷键 | 作用 | 提示 |
|--------|------|------|
| `Esc` | 停止 Claude 当前操作（不退出） | 比 Ctrl+C 更安全，可随时中断并调整方向 |
| `Esc` × 2 | 打开消息历史/编辑之前提示 | 30秒内可编辑并重新发送 |
| `↑/↓` | 浏览命令历史 | 跨会话保留 |
| `Tab` | 命令自动补全 | 上下文感知 |
| `Ctrl+V` | 粘贴图片 | Mac 上不是 Cmd+V！ |
| `Shift+Enter` | 换行（需先执行 `/terminal-setup`） | 多行输入必备 |
| `Ctrl+C` | 取消当前操作/退出 | 用于退出，Esc 用于中断 |
| `Ctrl+L` | 清屏 | |
| `Shift+Tab` | 切换权限模式（Auto-Accept / Plan Mode / Normal） | 按一次切换 acceptEdits，按两次进入 Plan Mode |

### 1.2 启动参数与模式

```bash
# 交互模式 - 开始新对话
claude

# 带初始提示启动
claude "Help me set up a Kubernetes deployment"

# 单次执行模式（不进入交互）
claude -p "analyze the database schema in this project"

# 输出 JSON 格式（适合脚本集成）
claude -p "generate API documentation" --output-format json

# 恢复历史会话
claude --resume          # 显示会话列表选择
claude --continue        # 直接恢复最近的会话
claude -r session-id     # 恢复指定会话

# 指定模型
claude --model sonnet    # 日常使用（推荐）
claude --model opus      # 复杂任务

# 免授权模式
claude --dangerously-skip-permissions

# 添加额外目录
claude --add-dir ../frontend --add-dir ../backend
```

### 1.3 管道与脚本集成

```bash
# 管道输入错误日志
cat error.log | claude -p "Identify the cause of this error and explain how to fix it"

# 管道输入构建输出
pnpm build 2>&1 | claude -p "Analyze this build error and tell me which file and line to fix"

# 生成 Git 提交信息
git diff --staged | claude -p "Summarize these changes as a Conventional Commits format commit message"

# CI/CD 集成 - 分析测试覆盖率
claude -p "analyze test coverage" --output-format json | jq '.coverage_percentage'

# 生成发布说明
claude -p "generate release notes from commits" --max-turns 2 > RELEASE_NOTES.md

# Docker 日志分析
docker logs container_name | claude -p "find errors in logs"

# 数据库结构分析
mysql -e "SHOW TABLES" | claude -p "analyze database structure"
```

### 1.4 批量操作技巧

```bash
# 队列多个任务（Claude 会智能处理依赖顺序）
> Refactor the authentication module to use JWT
> Also update the documentation for the new auth flow
> And create integration tests for the JWT implementation
> Finally, update the API client examples

# 利用 Git Worktrees 并行处理
git worktree add ../project-feature-a feature-a
git worktree add ../project-feature-b feature-b
# 终端1
cd ../project-feature-a && claude
# 终端2
cd ../project-feature-b && claude
```

### 1.5 免授权模式别名

```bash
# 永久别名（添加到 ~/.bashrc 或 ~/.zshrc）
alias claude='claude --dangerously-skip-permissions'

# 更安全的方式 - 在 devcontainer 中使用
# Anthropic 官方提供的 devcontainer 有防火墙规则限制出站连接
# 在 devcontainer 内运行 --dangerously-skip-permissions 是唯一被批准的完全自主运行模式
```

---

## 2. Prompt 工程技巧

### 2.1 CLAUDE.md 高级编写技巧

> **完整模板和配置方法**请参考 [02_tutorial.md 第 2.2 节](02_tutorial.md#22-claudemd-项目配置文件)。本节聚焦最佳实践要点和常见反模式。

**核心原则：CLAUDE.md 做 80% 的提示工作，你的提示只需说当前要什么。**

#### 文件位置与加载规则

| 记忆类型 | 文件位置 | 用途 |
|----------|----------|------|
| **组织级**（托管策略） | macOS: `/Library/Application Support/ClaudeCode/CLAUDE.md`<br>Linux: `/etc/claude-code/CLAUDE.md`<br>Windows: `C:\Program Files\ClaudeCode\CLAUDE.md` | 公司编码标准、安全策略、合规要求（IT 管理员部署） |
| **用户记忆**（全局） | `~/.claude/CLAUDE.md` | 个人偏好，所有项目通用 |
| **项目记忆**（共享） | `./CLAUDE.md` 或 `./.claude/CLAUDE.md` | 团队共享的指令、架构、规范 |
| **本地记忆**（个人项目） | `./CLAUDE.local.md`（添加到 `.gitignore`） | 个人沙箱 URL、偏好的测试数据 |
| **子目录记忆**（递归） | `./subdir/CLAUDE.md` | 处理该子目录时按需加载 |

Claude Code 从当前工作目录向上递归读取所有 CLAUDE.md 文件。加载顺序（从最广泛到最具体）：组织级 → 用户级 → 项目级 → 本地级 → 子目录（递归）。

#### 编写最佳实践要点

1. **保持 200 行以内** — 太长会被忽略或挤占上下文
2. **写事实不要写目标** — "当前阶段：数据库 schema 未定"，不写"计划支持 PostgreSQL"
3. **添加反模式** — 对此代码库特有的禁止做法
4. **当前开发阶段说明** — 让 Claude 知道什么工作是在范围内的
5. **关键参考文件路径** — 避免每次粘贴路径
6. **逐步完善** — 不要一次写完，遇到问题时再添加
7. **可以导入其他文件**：
   ```markdown
   See @README.md for project overview and @package.json for available npm commands.
   Git workflow: @docs/git-instructions.md
   ```

#### CLAUDE.md 编写反模式（来自 Anthropic 内部实践）

1. **不要用 @ 直接嵌入长文档** — 每次运行都会嵌入整份文档，拖垮上下文窗口。应明确告诉智能体（Agent）"为何"与"何时"去读该文档。
   ```markdown
   # 好的写法：
   When encountering FooBarError or complex usage,
   read path/to/docs.md for advanced troubleshooting steps.
   ```

2. **不要只给"绝不"** — 避免只有负向约束。给出替代方案。
   ```markdown
   # 不好：
   Never use --foo-bar
   
   # 好：
   Prefer --baz-qux over --foo-bar for performance reasons
   ```

3. **保持简洁** — 对每一行问自己："删除这行会导致 Claude 犯错吗？"不会就删除。
   ```markdown
   # ✅ 包括：
   - Claude 无法从代码推断的 Bash 命令
   - 与默认值不同的代码风格规则
   - 测试指令和首选测试运行器
   - 存储库礼仪（分支命名、PR 约定）
   - 架构决策
   - 常见陷阱或非显而易见的行为
   
   # ❌ 排除：
   - Claude 可以通过读取代码弄清楚的任何东西
   - Claude 已经知道的标准语言约定
   - 详细的 API 文档（改为链接到文档）
   - 经常变化的信息
   - "编写干净的代码"等自明实践
   ```

4. **把 CLAUDE.md 当作自我约束机制** — 如果 CLI 命令过于复杂冗长，不要写大段文档去解释，而是写一个简单的 Bash 封装。

### 2.2 写出好 Prompt 的三原则

**原则 1：陈述结果，而不是步骤**
```
# ❌ 不好 - 过于指令化
"Open userService.ts, find the validate function, add null check at line 42."

# ✅ 好 - 描述结果
"Users without emails are crashing validation. Make it handle this gracefully and add a test."
```

**原则 2：引用已有代码**
```
"Make the settings page work like the profile page" 
# 比一段描述布局偏好的段落更有价值
```

**原则 3：设定边界**
```
"Add error handling to the checkout flow. Don't change the payment provider integration."
# 告诉 Claude 不要碰什么和告诉它做什么同样重要
```

### 2.3 实用 Prompt 模式

#### 修复 Bug
```
# 提供完整错误信息
The build fails with this error: [paste full error with stack trace].
Fix it and verify the build succeeds. Address the root cause, don't suppress the error.
```

#### 重构代码
```
# 先理解，再计划，最后执行
1. "Read src/services/UserService.ts, src/types/user.ts, and every file that imports UserService. Summarize what UserService does and what would break if we changed its interface."
2. "Based on what you've read, propose how you'd refactor this into smaller services."
3. "Implement step 1 from the plan."
```

#### 写测试
```
# 让 Claude 匹配现有模式
"Write tests for NotificationService.swift covering:
- Normal operation
- Edge cases (empty input, null values, boundary conditions)
- Error conditions
Match the existing test style in Tests/NotificationTests.swift."
```

#### 代码审查
```
"Review this code focusing on:
1. Security vulnerabilities
2. Performance issues
3. Code readability
4. Best practices adherence
Use a critical but constructive tone."
```

#### 先调研再行动
```
"I want to add Google OAuth. 
First: read /src/auth and understand how we handle sessions and login.
Then: create a plan. Don't write any code yet."
```

### 2.4 分步骤引导技巧

```
# 复杂任务分四个阶段
阶段1 - 探索（Plan Mode，只读）：
"Read /src/auth and understand how we handle sessions and login."

阶段2 - 规划（Plan Mode，只读）：
"I want to add Google OAuth. What files need to change? What's the session flow? Create a plan."

阶段3 - 实现（Normal Mode）：
"Implement step 1 from the plan. Write tests first, then implement."

阶段4 - 提交：
"Review the diff. If all tests pass, commit with a descriptive message."
```

### 2.5 上下文管理技巧

```
# 原则：一个对话一个任务，任务之间 /clear

# 识别上下文膨胀
/context    # 查看当前上下文窗口使用情况

# 任务切换时清理
/clear      # 清空对话历史，但保留 CLAUDE.md 上下文

# 复杂任务的"文档化并清空"策略
1. "Save your progress and plan to progress.md"
2. /clear
3. "Read progress.md and continue from where we left off"

# 避免上下文污染
# 使用 /btw 在 Claude 工作时插入问题，不会进入对话历史
/btw "is this caching approach correct?"
```

---

## 3. 代码编辑技巧

### 3.1 多文件重构策略

#### 四步重构工作流

```
步骤1 - 理解（Understand）：
"Read src/components/UserDashboard.tsx and its imports. Tell me:
- What does it currently do?
- What are its dependencies?
- What state does it manage?
- What would be risky to move?"

步骤2 - 规划（Plan）：
"Based on what you've read, propose how you'd split UserDashboard into smaller components.
List each new component, what it would contain, and what props it would need.
Don't write any code yet."

步骤3 - 分块执行（Execute in Chunks）：
"Implement step 1 from the plan: extract UserDashboardHeader into its own file.
Keep the logic identical — no behavior changes, just extraction."

步骤4 - 验证（Verify）：
"Run the test suite and fix any failures.
Review the diff — does UserDashboard.tsx still have logic that belongs in the extracted component?"
```

#### 大型重构的"切片策略"

```markdown
# refactor-plan.md — 重构计划
## Auth Module Refactoring
- Slice 1: Extract UserValidator class from user.py (lines 45-120)
- Slice 2: Replace manual DB session management with context manager
- Slice 3: Convert synchronous HTTP calls to async in external_api.py
- Slice 4: Standardize error types to use AuthError hierarchy

# 每片要求：
# - 触及 1 个文件或 1 个明确关注点
# - 有已有测试或可以先写测试
# - 产生 diff 不超过 200 行
```

#### Git Worktree 并行重构

```bash
# 同时进行编码和验证
git worktree add ../project-dev feature-x
git worktree add ../project-test feature-x

# 终端1：编写代码
cd ../project-dev && claude

# 终端2：验证和测试
cd ../project-test && claude
```

### 3.2 让 Claude Code 写测试的技巧

```bash
# 1. 先让 Claude 找到未覆盖的代码
"Find functions in NotificationsService.swift that are not covered by tests"

# 2. 生成测试（Claude 会自动匹配项目现有的测试风格）
"Add tests for the notification service. Use the same framework and patterns as existing tests in Tests/NotificationTests.swift."

# 3. 要求边界条件覆盖
"Add test cases for edge conditions: empty input, null values, boundary values, concurrent access"

# 4. TDD 风格 - 先写测试再实现
"Write a failing test that reproduces the login timeout bug. Don't fix the code yet."

# 5. 对 React 组件的测试
"Generate tests for SearchInput component. Test:
- Rendering with default props
- onChange callback fires correctly
- Keyboard navigation (Enter, Escape)
- Accessibility attributes"

# 6. 运行并修复
"Run the new tests and fix any failures"
```

### 3.3 Code Review 技巧

```bash
# 让 Claude 审查自己的代码
"Review the diff of what changed. Focus on:
1. Does any logic that should be in the extracted component remain in the original?
2. Are there any security issues?
3. Are all error states handled?
4. Is the code consistent with our existing patterns?"

# 审查 PR
"Review this PR as a senior engineer. Check for:
- Architecture consistency
- Test coverage adequacy
- Error handling completeness
- Potential performance regressions"

# 使用 /review 命令
/review    # 让 Claude 进行自我代码审查

# 多智能体审查（高级用法）
# 使用多个子智能体从不同角度审查同一份代码
```

### 3.4 调试与错误排查

```bash
# 1. 提供完整堆栈跟踪
"I'm getting this error: [paste full stack trace]
Here are the reproduction steps:
1. Navigate to http://localhost:3000/dashboard
2. Enter username and password
3. Click login button
4. Error: TypeError: Cannot read properties of undefined (reading 'map')
Please look at @src/components/UserList.tsx and identify the cause."

# 2. 让 Claude 自己重现问题
"Run pnpm test and show me the failing tests.
Then analyze the failures and propose fixes."

# 3. 使用深度思考模式处理复杂 Bug
"ultrathink: We have an intermittent timeout issue in the order service during peak hours. 
Analyze all possible causes: database, cache, network, connection pool, GC pauses."

# 4. 根因分析
"The build fails with this error: [paste error]. 
Fix it and verify the build succeeds. Address the root cause, don't suppress the error."

# 5. 使用 /doctor 诊断
/doctor        # 检查依赖和配置问题
/doctor --performance   # 性能诊断

# 6. 使用 Esc 纠正方向
# 如果 Claude 的方向不对：
[按 Esc]
"Stop. Undo your last changes and try using Redis instead of Memcached."
```

---

## 4. 配置优化

### 4.1 性能优化设置

#### 高级环境变量

```bash
# 最大输出 Token（默认 4096，最高 8192）
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192

# 扩展思考 Token 预算
export MAX_THINKING_TOKENS=10000

# Node.js 内存（大型构建使用）
export NODE_OPTIONS="--max-old-space-size=4096"

# MCP 超时设置
export MCP_TIMEOUT=30000        # MCP 服务器启动超时
export MCP_TOOL_TIMEOUT=60000   # MCP 工具执行超时

# 减少非必要 AI 调用
export DISABLE_NON_ESSENTIAL_MODEL_CALLS=false

# 关闭遥测（轻微性能提升）
export DISABLE_TELEMETRY=true
export DISABLE_ERROR_REPORTING=true
export CLAUDE_CODE_DISABLE_TERMINAL_TITLE=true
```

#### 自动压缩阈值调整

```bash
# 默认在 ~95% 上下文时自动压缩
# 可提前触发：
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70   # 70% 就触发
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50   # 嘈杂工作流用 50%
```

#### settings.json 高级配置

> **注意**：`maxConcurrentOperations` 为社区发现的设置项（对应环境变量 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`）。`aggressiveCaching` 和 `preferredOutputFormat` 未在官方文档中确认，已从此处移除。如需控制输出风格，请使用官方支持的 `outputStyle` 设置。

```json
{
  "statusLine": {
    "type": "command",
    "command": "jq -r '\"[\(.model.display_name)] \(.context_window.used_percentage // 0)% context\"'"
  },
  "maxConcurrentOperations": 10
}
```

### 4.2 Token 节省技巧

#### 核心原则：80% 的 Token 可能被浪费在无关上下文上

```bash
# 1. 定期查看上下文占用
/context    # 显示哪些内容在占用 Token

# 2. 关闭不需要的 MCP 服务器
/mcp        # 交互式管理 MCP
# 实测：关闭 6 个不用的 MCP 后，上下文从 67.7k → 6k（减少 93%！）

# 3. 任务之间 /clear
# 完成一个独立任务后立即清理
/clear

# 4. 手动触发压缩
/compact    # 清除对话历史但保留摘要

# 5. 用 @ 精确引用文件，而不是让 Claude 搜索
# ❌ "find the authentication logic"
# ✅ "@src/auth/login.ts — fix the null check in this file"

# 6. 使用 /compact focus on errors 在调试时
/compact "focus on errors"    # 压缩时保留错误上下文

# 7. 让 Claude 总结后清空
"Summarize our current discussion and progress into summary.md"
/clear
"@summary.md — continue from where we left off"

# 8. 限制输出大小
export MAX_MCP_OUTPUT_TOKENS=8000
export BASH_MAX_OUTPUT_LENGTH=20000
```

#### Token 节省效果估算

| 策略 | 预计节省 | 难度 |
|------|---------|------|
| 编写好的 CLAUDE.md | 50-70% | 低 |
| 任务之间 /clear | 40-60% | 极低 |
| 关闭不用的 MCP | 80-93% | 极低 |
| 用 @ 引用文件 | 30-50% | 低 |
| 使用 /compact | 50-70% | 低 |

### 4.3 权限控制最佳实践

```bash
# 方案1：Auto Mode — 分类器自动审批（推荐日常使用）
# 一个单独的分类器模型审查命令，只阻止有风险的操作

# 方案2：权限白名单
{
  "permissions": {
    "allow": [
      "Bash(npm *)",
      "Bash(pnpm *)",
      "Bash(git *)",
      "Bash(*-h*)"
    ]
  }
}

# 方案3：沙箱 — OS 级隔离
# 限制文件系统和网络访问

# 方案4：阻断危险命令（通过 Hook）
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "^Bash$",
      "hooks": [{
        "type": "command",
        "command": "echo $CLAUDE_TOOL_INPUT | grep -qE '(rm -rf|DROP TABLE|--force)' && echo 'Blocked' >&2 && exit 2 || exit 0"
      }]
    }]
  }
}

# 方案5：生产环境安全配置
claude --disallowedTools "Bash(rm:*)" "Bash(sudo:*)" "Bash(chmod:*)" \
       --permission-mode plan \
       "secure code review"
```

#### 提交阶段钩子（推荐模式）

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash(git commit)",
      "hooks": [{
        "type": "command",
        "command": "test -f /tmp/agent-pre-commit-pass || (echo 'Tests not passed' >&2 && exit 1)"
      }]
    }]
  }
}
```

> **关键原则**：在提交阶段用钩子强制状态校验，避免在写入阶段（Edit/Write）阻断。先让智能体完成计划，再检查结果。

### 4.4 成本控制策略

Claude Code 的成本主要来自 Token 消耗。掌握正确的成本控制策略可以在不降低产出的前提下大幅降低费用。

#### 月度成本估算

根据实际使用场景估算月度成本：

| 使用强度 | 使用模式 | 月均 Token 消耗 | 订阅方案（月成本） | API 方案（月成本估算） |
|---------|---------|---------------|------------------|-------------------|
| **轻度** | 每天 1-2 次提问/代码审查 | ~5M tokens | Pro（$20） | ~$15-30 |
| **中度** | 每天 3-5 次功能开发/重构 | ~20M tokens | Max 5x（$100） | ~$60-120 |
| **重度** | 全天候编码，大型重构 | ~80M tokens | Max 20x（$200） | ~$240-480 |
| **极重度** | 企业级大规模迁移/并行任务 | ~300M+ tokens | Enterprise（电话销售） | ~$900-1800+ |

> **计算假设**：基于 Sonnet 5 引入期 API 定价 $2/$10 每百万 Token，输入:输出≈3:1。实际成本因模型选择和任务复杂度波动。

#### 订阅 vs API 决策树

```
你需要 Claude Code？
├── 每天使用 < 2 小时，固定任务类型？
│   └── 是 → Pro 订阅（$20/月）
├── 每天使用 2-6 小时，重度依赖？
│   └── 是 → Max 5x（$100/月）
├── 全天候使用，大型项目？
│   └── 是 → Max 20x（$200/月）
├── 偶尔使用，按需付费？
│   └── 是 → API 按量付费（$3-30/小时）
├── 团队使用（5+ 人）？
│   └── 是 → Team 或 Enterprise 方案
└── CI/CD 自动化、批量任务？
    └── 是 → API 按量付费 + 专用 API Key
```

#### 主要成本驱动因素

| 因素 | 对成本的影响 | 优化策略 |
|------|------------|----------|
| **上下文窗口大小** | 每次 API 调用都携带完整会话历史，上下文越大成本越高 | 定期 `/compact`；一个会话一个任务 |
| **工具调用频率** | 每次文件读取、搜索、命令执行都消耗 Token | 用 `@` 精确引用文件，减少搜索次数 |
| **模型选择** | Opus 成本是 Sonnet 的 2.5 倍，Fable 5 是 5 倍 | 日常使用 Sonnet 5；复杂任务才升级模型 |
| **子智能体数量** | Dynamic Workflows 并行 N 个子智能体 ≈ N 倍 Token | 限制并行子智能体数量；评估任务是否真正需要并行 |
| **MCP 服务器数量** | 每个活跃 MCP 消耗 5K-15K Token 上下文 | 保持活跃 MCP ≤ 3 个 |
| **CLAUDE.md 大小** | 每次会话启动加载全部 CLAUDE.md 内容 | 保持 ≤ 200 行；避免 `@` 嵌入长文档 |

#### 实际成本案例

| 案例 | 任务 | 模型 | Token 消耗 | 成本 |
|------|------|------|-----------|------|
| **Bug 修复** | 修复一个中等复杂度的 NPE 问题 | Sonnet 5 | ~50K tokens | ~$0.15 |
| **功能开发** | 实现一个 REST 端点（含测试） | Sonnet 5 | ~200K tokens | ~$0.60 |
| **代码审查** | 审查 500 行 PR 变更 | Sonnet 5 | ~80K tokens | ~$0.24 |
| **大规模重构** | 回调风格迁移至 async/await（20 文件） | Opus 4.8 | ~2M tokens | ~$10-20 |
| **跨语言迁移** | Python → Go 迁移 10,000 行 | Opus 4.8 + Sonnet 5 混合 | ~10M tokens | ~$50-100 |
| **Bun 迁移案例** | Zig → Rust 100 万行 | Opus 4.6/4.7 混合 | ~66 亿 tokens | ~$165,000（极端案例） |

#### `/cost` 命令实战

```bash
# 在会话中查看当前消耗
/cost

# 典型输出示例：
# Session: 142K tokens | Cost: ~$0.43 (Sonnet 5)
# Today: 1.2M tokens | Cost: ~$3.60
# This Month: 15.8M tokens | Cost: ~$47.40

# 监控技巧：
# 1. 每次 /clear 前查看 /cost，了解单个任务的真实开销
# 2. 月底对比 /usage 中的套餐用量，评估方案是否合适
# 3. 发现成本异常时立即检查：
#    - 是否有冗余 MCP 服务器在消耗上下文
#    - 是否忘记 /clear 导致超长会话
#    - 是否有子智能体在不必要地运行
```

#### 成本优化清单

| # | 策略 | 预估节省 | 实施难度 |
|---|------|---------|---------|
| 1 | 编写精良的 CLAUDE.md（减少 50-70% 纠正 Token） | **30-50%** | 中 |
| 2 | 一个会话一个任务 + 定期 `/clear` | **20-40%** | 低 |
| 3 | 日常使用 Sonnet 5 代替 Opus | **50-60%** | 极低 |
| 4 | 关闭不用的 MCP 服务器 | **80-93%**（上下文节省） | 极低 |
| 5 | 使用 `@` 精确引用文件 | **15-30%** | 低 |
| 6 | 设置 `--max-turns` 限制无效探索 | **10-30%** | 低 |
| 7 | 启用自动压缩（`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70`） | **10-20%** | 极低 |
| 8 | CI/CD 中使用专用 API Key + 预算上限 | **风险控制** | 低 |
| 9 | 避免在 CLAUDE.md 中 `@` 嵌入长文档 | **20-50%** | 极低 |
| 10 | 使用缓存写入（API 方案） | **30-40%**（缓存命中时） | 低 |

> **黄金法则**：成本控制的本质是"减少无关上下文"。每 1K Token 的节省看似微小，但乘以每天数百次的 API 调用，月节省可达 $50-200+。

---

## 5. 实战案例

### 5.1 从零搭建项目

```bash
# 1. 初始化项目
claude
> /init    # 让 Claude 分析项目并生成 CLAUDE.md

# 2. 让 Claude 理解架构需求
> "I want to build a REST API with the following requirements:
> - FastAPI backend, PostgreSQL database
> - JWT authentication with refresh tokens
> - File upload with S3 storage
> - Rate limiting per user
> Create a detailed architecture plan. Don't write code yet."

# 3. 逐步实现
> "Implement the project structure and database models from the plan."
> "Now implement the authentication flow with tests."
> "Add the file upload endpoints."

# 4. 持续使用 /init 完善 CLAUDE.md
> /init    # 随时间推移不断更新项目记忆
```

### 5.2 遗留代码迁移（企业真实案例）

**案例：Wiz — 50,000 行 Python 迁移到 Go**

```
背景：Wiz 云安全平台需要将 PDF 解析库从 Python (pypdf) 迁移到 Go
规模：50,000+ 行代码，20 年历史的库
传统估计：2-3 个月的专家人力

Claude Code 实际：
- 1 小时：基本功能可运行
- 约 10 小时迭代：处理所有 500 个病理测试用例
- 总计约 20 小时（10 小时开发 + 10 小时测试）
- 最终产出：18,413 行 Go 代码
- 性能提升：2x+ PDF 处理速度
```

**案例：Stripe — 10,000 行 Scala 到 Java**

```
背景：Stripe 需要将 Scala 代码迁移到 Java
传统估计：10 个工程周
Claude Code 实际：4 天完成
部署规模：1,370 名工程师使用，零配置企业部署
```

**案例：Anthropic 员工 Jarred Sumner（Bun 联合创始人）使用 Claude Code 将 Bun 从 Zig 迁移到 Rust**

> 注：Bun 是 Oven 公司的开源项目（非 Anthropic 项目），但 Bun 联合创始人 Jarred Sumner 同时担任 Anthropic MTS（Member of Technical Staff）。他利用 Claude Code 完成了此次迁移，Anthropic 将其作为官方博客案例，展示 Claude Code 在大规模代码迁移中的实际能力。

```
规模：100 万行代码
耗时：不到 2 周
成本：约 $165,000（API 定价，59 亿输入 + 6.9 亿输出 Token）
结果：100% 测试通过，19 个回归 Bug 全部已修复
```

#### 迁移 CLAUDE.md 模板

```markdown
# CLAUDE.md — Legacy Migration Project

## Migration Goal
Migrate from Java 8 + Spring MVC 5.3 to Java 21 + Spring Boot 3.2 + Project Loom.

## Source State (Current)
- Java 8, Maven, Spring MVC with web.xml
- Servlet-based request handling
- ThreadLocal pattern for request context

## Target State
- Java 21, Spring Boot 3.2, Gradle
- Virtual Threads via spring.threads.virtual.enabled=true
- No ThreadLocal (incompatible with virtual threads pinning)
- Jakarta EE (javax.* → jakarta.*)

## CRITICAL MIGRATION RULES
1. Replace ALL `javax.*` imports with `jakarta.*`
2. Replace ThreadLocal with ScopedValue or request attributes
3. Replace synchronized blocks in hot paths with ReentrantLock

## What Claude MUST NOT Do
- Never touch src/main/resources/db/migration/ (Flyway scripts)
- Never modify SecurityConfig.java without explicit permission
- Never run git commit, git push, or mvn deploy
```

#### 迁移六步流程

```
阶段0：设置 CLAUDE.md（编码架构知识）
阶段1：依赖审计（研究子智能体，只读分析）
阶段2：逐层迁移（分片、可独立验证）
阶段3：测试验证（每片通过测试门禁）
阶段4：对抗性审查（多智能体独立审查）
阶段5：最终校验（对比新旧输出）
```

### 5.3 文档自动生成

```bash
# 1. 找到未文档化的代码
"Find functions without proper JSDoc comments in the auth module"

# 2. 批量生成文档
"Add JSDoc comments to all undocumented functions in auth.js.
Include parameter descriptions, return types, and usage examples."

# 3. 生成 API 文档
"Generate OpenAPI/Swagger documentation for all endpoints in src/api/"

# 4. 生成架构文档
"Analyze the project structure and generate an architecture overview document.
Include: component diagram descriptions, data flow, key design decisions."

# 5. 生成变更日志
git log --oneline v1.0..HEAD | claude -p "Generate a structured changelog grouped by feature, fix, and breaking change"

# 6. 生成 README
"Generate a comprehensive README.md for this project based on the codebase analysis."
```

---

## 6. 命令深度技巧与反直觉用法

> **📖 完整命令列表**：本章聚焦于命令的**高级技巧和反直觉用法**。所有命令的完整语法、参数和使用场景请参见 [02_tutorial.md 第 3.6 节](02_tutorial.md#36-内置斜杠命令大全)，那里有 90+ 命令的详细参考。

### 6.1 会话管理深度技巧

#### `/btw` — 零污染提问的高级用法

`/btw` 看似简单，但有几个反直觉的技巧：

**技巧 1：在 Claude 执行命令时获取系统信息**
```
# Claude 正在安装依赖，你想知道当前目录结构
/btw "what's in the src/ directory right now?"
# 回答不进入上下文，不影响主任务
```

**技巧 2：与 `/compact` 配合——压缩前检查**
```
# 在长会话压缩前，用 /btw 检查关键信息是否还在
/btw "do you remember the database migration details we discussed?"
# 如果 Claude 忘记了，在压缩时指定焦点：
/compact focus on database migration details
```

**技巧 3：实验性提问——不付代价的试探**
```
# 想试试一个方案但不确定是否靠谱
/btw "would using a thread pool instead of async I/O fix this?"
# Claude 给出分析但不写入上下文，不影响当前编码方向
```

**技巧 4：多任务场景下的记忆辅助**
```
# 同时处理多个 Bug，用 /btw 交叉确认
# 主任务：修复认证 Bug
/btw "did we fix the same null check issue in the billing module last week?"
```

**反直觉要点**：
- `/btw` 没有任何工具访问权限（不能读文件、不能搜索），只能基于已有上下文
- 问题和回答完全不会进入对话历史——这意味着 Claude 不会"记住"这个问答
- 如果 `/btw` 给出了关键信息，你需要**主动告诉主对话**

#### `/rewind` — 三种模式的适用时机

`/rewind` 提供三种精确回退粒度，每种有不同的战略用途：

**模式 1：仅回退代码（保留对话）**
```
# 适用时机：
# - Claude 改了大量文件但分析思路正确
# - 你想让 Claude "记住"刚才的探索结果但代码重新来
# - 回退后可以说"刚才你分析的对，但用 XX 方式实现"

# 实战：调试了一个 API 接口
> /rewind  # 选择"仅回退代码"
> "Your analysis was correct — the issue is the JWT expiry. 
>  But implement the fix using refresh tokens instead."
```

**模式 2：仅回退对话（保留代码）**
```
# 适用时机：
# - 代码改动正确但对话方向偏了
# - 代码是你想要的，但想用更简洁的思路解释给 Claude
# - 想基于当前代码状态重新推理

# 实战：代码重构完成后想换思路
> /rewind  # 选择"仅回退对话"
> "The code changes look good. Now let's think about testing strategy."
```

**模式 3：全部回退**
```
# 适用时机：
# - Claude 完全跑偏，代码和思路都不对
# - 实验性编码后想重来
# - 想要一个干净的起点

# 实战：试了 20 轮发现方向错误
> /rewind  # 选择"全部回退"，选择一个较早的检查点
> "Let's take a different approach. Use the strategy pattern instead."
```

**"从此处摘要"模式（高级）**：
```
# 不是回退，而是压缩——把选定检查点之前的消息压缩为摘要
# 适用：上下文接近满载但不想全部回退，只想释放早期对话空间
```

**反直觉要点**：
- 检查点是**每次你按回车前**自动保存的，所以可能很多（每轮一个）
- 回退代码会**真实修改磁盘文件**，不只是对话层面
- `/rewind` 可以搭配 `Esc, Esc` 快捷键访问

#### `/compact` — 压缩时机和焦点策略

**反直觉：不要等到自动压缩触发**
```
# Claude Code 默认在 ~95% 上下文使用时自动压缩
# 但此时已经太晚了——Claude 可能已经"忘记"了关键信息

# 推荐做法：
# 50% 上下文时：/context 查看分布
# 70% 上下文时：主动 /compact
# 80%+ 时：/compact focus on <关键主题>
```

**焦点指令的艺术**：
```
# ❌ 太宽泛：/compact keep everything important
# ❌ 太宽泛：/compact focus on the project
# ✅ 精确：/compact focus on the auth module changes and failing tests
# ✅ 精确：/compact keep all database migration details and API contract decisions
```

**压缩与 SessionStart hook 配合**：
```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "compact",
      "hooks": [{
        "type": "command",
        "command": "echo 'REMINDER: Working on auth module. Key decisions: JWT expiry=15min, refresh=7d.'"
      }]
    }]
  }
}
```
压缩后 Claude 会重新加载，hook 可以帮它找回记忆。

#### `/branch` vs `/fork` — 何时用哪个

| 命令 | 效果 | 适合场景 |
|------|------|---------|
| `/branch` | 创建对话分支，**立即切换**到新分支 | "我想试试不同的 UI 方案但不确定" |
| `/fork` (v2.1.212+) | 复制到**后台会话**，原会话继续运行 | "帮我同时在后台跑回归测试" |

**反直觉**：`/fork` 和 `/branch` 都可能"丢失"——`/fork` 生成的后台会话需要用 `claude agents` 或 `/tasks` 查看进度。

### 6.2 工作流与自动化技巧

#### `/loop` — 循环批处理的反直觉用法

**技巧 1：不指定间隔时的智能节奏**
```
# 不指定间隔时 Claude 会自动调整节奏
# 适合变化性任务："检查 CI 状态直到通过"
/loop check if the GitHub Actions CI has passed, report when done

# 指定间隔时精确控制
/loop 5m check server health at http://localhost:3000/health
```

**技巧 2：`.claude/loop.md` 自动加载**
```markdown
# .claude/loop.md
Review all open PRs for this repository:
1. Check CI status
2. Check if PRs can be merged (no conflicts)
3. Summarize changes since last review
4. Report any blocking issues
```
```
/loop 30m  # 自动读取 .claude/loop.md 的内容
```

**技巧 3：与 `/goal` 组合——有终点的循环**
```
# /loop 适合"持续监控"，/goal 适合"工作到条件满足"
# 组合使用：/loop 监控外部变化，/goal 推动内部工作

# 先设置目标
> /goal all integration tests pass

# 在另一个终端
> /loop 5m check if new commits landed on main branch, report to me
```

**反直觉要点**：
- `/loop` 最长运行 3 天，不是无限的
- 别名 `/proactive` 暗示了它的本质——主动检查而非被动等待
- 可以同时运行多个 `/loop`（在不同会话中）

#### `/batch` — 大规模变更策略

**如何让 `/batch` 工作得更好**：
```
# ❌ 太模糊——Claude 不知道怎么分解
/batch refactor the codebase

# ✅ 明确分解策略
/batch rename all API endpoints from /api/v1/ to /api/v2/, 
       update all import paths, and fix type errors

# ✅ 给出范围
/batch migrate src/services/ from JavaScript to TypeScript, 
       one file per unit
```

**监控 `/batch` 进度**：
```
# 运行 /batch 后，立即在另一个终端
/workflows  # 查看各单元进度

# 或者
/tasks  # 查看后台子代理状态
```

**反直觉**：`/batch` 会为每个单元自动创建 PR，确保设置好 GitHub 权限。5-30 个单元意味着 5-30 个 PR。

#### `/background` 与 `/fork` 的协同

```
# 场景：你有一个大型任务，想同时做三件事
# 终端 1：主工作
> 实现用户认证模块

# 在同一个会话中：
> /fork run the full regression test suite  
# → 创建后台会话A，运行回归测试

> /fork analyze all dependency security vulnerabilities
# → 创建后台会话B，分析安全漏洞

# 继续在主会话工作，用 /tasks 检查后台进度
> /tasks
```

#### `/autofix-pr` — CI 自动化策略

```
# /autofix-pr 需要 gh CLI 和 web 访问权限

# 建议：设置自定义指令限制修复范围
/autofix-pr only fix lint errors, type errors, and failing unit tests
/autofix-pr do NOT modify API contracts or database schemas

# 配合 .claude/settings.json 权限控制
# 防止 autofix 执行危险操作
```

### 6.3 模型与控制技巧

#### `/effort` — 努力级别的对照表

| 级别 | 思考深度 | Token 消耗 | 适用任务 |
|------|---------|-----------|---------|
| `low` | 最浅 | 最低 | 重命名、格式化、简单文档 |
| `medium` | 标准 | 正常 | 日常编码、Bug 修复 |
| `high` | 深度 | 较高 | 架构决策、重构 |
| `xhigh` | 更深 | 高 | 安全审查、复杂算法 |
| `max` | 极致 | 非常高 | 关键任务、仅当前会话可用 |
| `ultracode` | xhigh + 自动工作流 | 极高 | 全自动编排、仅当前会话 |

**反直觉**：
- `/effort low` + `/fast` 组合在日常任务中可以快 3-5 倍但质量下降明显
- `max` 和 `ultracode` 仅限当前会话——下次会话恢复为默认

#### `/model` 的进阶策略

```
# 一个会话中多次切换模型的策略
# 1. 先探索代码库
/model haiku  # 快速浏览文件，不费 token

# 2. 理解了问题后升级模型实现
/model sonnet  # 实现功能

# 3. 完成后审查质量
/model opus  # 深度审查

# 注意：切模型不会丢失上下文
```

> **💡 成本提示**：Haiku → Sonnet → Opus 速度递减 3-5 倍，成本递增 3-5 倍。详见 [4.4 成本控制策略](#44-成本控制策略)。

### 6.4 上下文与权限技巧

#### `/context` — 读懂上下文分布

`/context` 显示的彩色网格中，需要特别注意：

| 颜色区域 | 含义 | 如果占比过高怎么�� |
|---------|------|-----------------|
| 对话历史 | 你和 Claude 的对话 | `/compact` |
| 工具输出 | 文件读取、搜索结果 | 用 `@` 精确引用文件减少搜索 |
| 系统指令 | CLAUDE.md、Skills | 精简 CLAUDE.md 到 200 行内 |
| MCP 服务器 | 活跃 MCP 占用的 token | 关闭不用的 MCP |

**诊断工作流**：
```
1. /context → 看到哪个区域占比最高
2. 针对性优化：
   - 对话历史高 → /compact
   - MCP 高 → /mcp disable unused-server
   - 工具输出高 → 停止不必要的文件读取
3. /context → 验证优化效果
```

#### `/permissions` 的白名单策略

```
# 渐进式建立权限白名单
# 第1天：手动确认每次操作
# 第3天：识别重复性安全操作，加入 allow 列表
# 第7天：建立一个稳定的白名单

# 推荐白名单基础模板
{
  "permissions": {
    "allow": [
      "Bash(npm *)",       # npm 操作
      "Bash(pnpm *)",      # pnpm 操作  
      "Bash(yarn *)",      # yarn 操作
      "Bash(git diff:*)",  # git diff 安全
      "Bash(git status)",  # git status 安全
      "Bash(git log:*)",   # git log 安全
      "Bash(ls)",          # 列出文件
      "Bash(cat *)",       # 读取文件
      "Bash(jq *)",        # JSON 处理
      "Bash(gh *)",        # GitHub CLI
      "Bash(echo *)",      # echo 安全
      "Bash(npx *)",       # npx 执行
      "Bash(node *)"       # node 执行
    ],
    "deny": [
      "Read(.env*)",
      "Write(.env*)",
      "Read(*secret*)",
      "Write(*secret*)",
      "Bash(rm *)",
      "Bash(sudo *)",
      "Bash(chmod 777*)"
    ]
  }
}
```

> 使用 `/fewer-permission-prompts` 命令可以自动扫描历史记录并建议白名单条目。

### 6.5 Skills 与自定义命令高级配置

#### Skill 的 `invoke` 字段策略

```yaml
# invoke: auto — Claude 自动判断何时加载
# 优点：开发者无需手动触发
# 缺点：可能在不相关时误触发
---
invoke: auto
description: 当用户提到"审计"、"安全审查"、"代码检查"时使用此 skill
---

# invoke: manual — 仅通过 /command 触发
# 优点：精确控制
# 缺点：需要记得使用
---
invoke: manual
---

# 选择建议：
# auto：通用工具类（代码格式化、git 操作）
# manual：特定场景类（部署脚本、数据库迁移）
```

#### Skill 的 `allowedTools` 最佳实践

```yaml
# 最小权限原则——只给 skill 它实际上需要的工具
---
allowedTools: ["Read", "Grep", "Glob"]  
# 只读 skill，不能修改文件

allowedTools: ["Read", "Write", "Edit", "Bash(git *)"]  
# 可写 skill，但只能操作 git

allowedTools: ["Read", "Glob", "Grep", "Bash(npm *)", "Bash(npx *)"]
# 可读 + npm/npx 操作，不能改文件
```

#### 自定义命令模板变量进阶

```markdown
---
description: 高级命令示例
allowed-tools: Bash(git:*), Read
---

# 使用位置参数
# /deploy feature-branch production
# $1 = feature-branch, $2 = production

Branch: $1
Environment: $2

# 使用命令内联
Current changes:
!`git diff --stat $1`

# 使用文件内联
Deploy checklist:
@deploy-checklist.md

# 所有参数
$ARGUMENTS
```

#### 组织级 Skill 管理

```
# 企业 Skill 优先级最高，可以覆盖个人和项目 Skill

# 组织 Skill 示例：强制代码审查
# 放在企业托管位置，对所有成员生效
---
description: 强制安全审查流程。代码变更后自动运行。
invoke: auto
scope: enterprise
allowedTools: ["Read", "Grep", "Bash(git:*)"]
---

在提交代码前，必须检查：
1. 无硬编码密钥（搜索 API_KEY, SECRET, PASSWORD, TOKEN）
2. 无 eval() 或 exec() 调用
3. 所有 SQL 查询使用参数化
4. 用户输入经过验证和清理
```

### 6.6 MCP 命令高级技巧

#### MCP 提示命令

MCP 服务器可以暴露提示作为命令，格式为：
```
/mcp__<server>__<prompt>
```

例如，如果有一个名为 `github` 的 MCP 服务器公开了 `list-issues` 提示：
```
/mcp__github__list-issues
```

这些命令从连接的 MCP 服务器动态发现，不在 `/help` 中列出。

#### MCP 服务器生命周期管理

```
# 按需启用/禁用 MCP，而不是一直开着
/mcp disable unused-server  # 释放 5K-15K token 上下文

# 需要时再启用
/mcp enable my-server
```

**实测案例**：关闭 6 个不用的 MCP 后，上下文从 67.7k → 6k（减少 93%）。

### 6.7 已弃用命令与迁移路径

| 命令 | 状态 | 迁移方案 |
|------|------|---------|
| `/review [PR]` | 已弃用 | `claude plugin install code-review@claude-plugins-official` |
| `/pr-comments [PR]` | v2.1.91 移除 | 直接让 Claude 查看 PR 评论 |
| `/vim` | v2.1.92 移除 | `/config` → Editor mode 切换 Vim/Normal |
| `/output-style` | v2.1.73 弃用 | `/config` 中设置输出样式 |
| `/extra-usage` | 已重命名 | 使用 `/usage-credits` |

---

## 7. 社区推荐

### 7.1 热门配置分享

#### 社区推荐的 settings.json

```json
{
  "permissions": {
    "allow": [
      "Bash(npm *)",
      "Bash(pnpm *)",
      "Bash(git:*)",
      "Bash(ls)",
      "Bash(cat)",
      "Bash(jq *)",
      "Bash(gh *)"
    ]
  },
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "jq -r '.tool_input.file_path' | xargs npx prettier --write 2>/dev/null || true"
      }]
    }]
  },
  "statusLine": {
    "type": "command",
    "command": "echo \"[$(cat ~/.claude/.current_model 2>/dev/null || echo '?')] $(du -sh ~/.claude/projects/ 2>/dev/null | cut -f1)\""
  }
}
```

### 7.2 常用 MCP 服务器推荐

#### 必装三件套（覆盖 90% 场景）

| 名称 | 安装命令 | 用途 |
|------|----------|------|
| **sequential-thinking** | `claude mcp add sequential-thinking -- npx -y mcp-sequential-thinking` | 深度思考，复杂问题逐步推理 |
| **context7** | `claude mcp add context7 -- npx -y @upstash/context7-mcp` | 实时最新技术文档（45k+ 库） |
| **memory** | `claude mcp add memory -- npx -y @modelcontextprotocol/server-memory` | 跨会话长期记忆 |

#### 常用组合

```
日常开发：filesystem + github + memory
深度工作：sequential-thinking + context7 + memory
自动化任务：playwright + filesystem
学习新技术：context7 + deepwiki + brave-search
前端开发：filesystem + git + figma
后端开发：git + postgres + context7
全栈开发：filesystem + git + playwright
```

#### 进阶 MCP 推荐

```bash
# Playwright — 浏览器自动化（21.7k Star）
claude mcp add playwright -- npx @playwright/mcp@latest --headless

# DeepWiki — 开源项目深度文档
claude mcp add deepwiki https://mcp.deepwiki.com/mcp

# Brave Search — 实时搜索（每月 2000 免费查询）
claude mcp add brave-search -e BRAVE_API_KEY=your-key -- npx -y @modelcontextprotocol/server-brave-search

# GitHub — 仓库管理、PR、Issues
claude mcp add github -- npx -y @modelcontextprotocol/server-github

# Filesystem — 安全文件访问
claude mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem /path/to/project

# PostgreSQL — 数据库操作
claude mcp add postgres -- npx -y @modelcontextprotocol/server-postgresql

# Supabase — 全栈后端
claude mcp add supabase https://mcp.supabase.com/mcp
```

#### MCP 使用原则

```
1. 按需安装 — 先用核心的，用到再装
2. 最小权限 — 给 MCP 尽可能少的权限
3. 定期清理 — 关闭不用的，每个 MCP 消耗 5000-15000 Token 上下文
4. 优先 CLI — Anthropic 推荐用 CLI 工具而非 MCP 服务器
5. 组合使用 — 不同场景用不同组合
```

### 7.3 Skills 推荐

#### 自定义命令（项目级）

```bash
# 创建项目级命令目录
mkdir -p .claude/commands

# 优化命令
echo "分析这个项目的性能，并提出三个具体的优化建议。" > .claude/commands/optimize.md

# 提交推送命令
echo "用合理描述性信息提交所有变更文件，然后推送到远程仓库。" > .claude/commands/push.md

# 使用
/project:optimize
/project:push
```

#### 自定义命令（用户级）

```bash
# 创建用户级命令目录
mkdir -p ~/.claude/commands

# 使用
/user:push
/user:optimize
```

#### 推荐的 Skills 工作流

```yaml
# .claude/skills/catchup/SKILL.md
---
name: catchup
description: Read all changed files in current git branch
---
Read the git diff of the current branch against main/master.
Summarize all changes and current state.
```

#### 社区热门的 Hooks 配置

**自动格式化每次编辑：**

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

**压缩后重新注入上下文：**

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "compact",
      "hooks": [{
        "type": "command",
        "command": "echo 'REMINDER: Use pnpm. TypeScript strict. Run tests before committing.'"
      }]
    }]
  }
}
```

**停止前验证测试：**

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "agent",
        "prompt": "Run tests. If any fail, fix them before stopping.",
        "timeout": 120000
      }]
    }]
  }
}
```

### 7.4 必备工具

```bash
# ccusage — Token 使用分析
npm install -g ccusage
ccusage -s 20250701          # 查看从某天开始的消耗
ccusage blocks --live        # 实时监控
ccusage monthly              # 月度趋势

# Claude-Code-Usage-Monitor — 实时仪表板
git clone https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor.git
cd Claude-Code-Usage-Monitor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
./ccusage_monitor.py --plan max20

# GitIngest — 将代码库转为 AI 友好格式
# 用于一次性给 Claude 提供代码库概览
```

### 7.5 社区最佳实践总结

```
1. CLAUDE.md 是最高杠杆的优化 — 投入 45 分钟可节省 40+ 次纠正提示
2. 给 Claude 验证自己的方式 — 测试、截图、预期输出
3. 先探索、再规划、最后编码 — 别跳过规划直接写代码
4. 一个对话一个任务 — 任务之间 /clear
5. 用 @ 引用文件 — 不要让 Claude 搜索能找到的东西
6. 使用 Esc 纠正方向 — 不需要等它完成再修正
7. 定期运行 /insights — 了解自己的使用模式和改进空间
8. 把 Claude 当作能干的初级工程师 — 提供上下文、检查工作
9. 使用 CLI 工具（gh, aws）优于 MCP 服务器 — 更省上下文
10. 投资让验证变得极其可靠 — 这是你能做的最高杠杆的事情
```

---

## 参考资源

- [Anthropic 官方最佳实践](https://code.claude.com/docs/zh-CN/best-practices)
- [Claude Code CLI Cheatsheet](https://github.com/kurdin/ai-coding-best-practices-for-modern-development)
- [Claude Code Power User Tips](https://github.com/ThamJiaHe/claude-code-handbook)
- [Claude Code 隐藏命令大全](https://theplanettools.ai/guides/claude-code-hidden-commands-guide-2026)
- [MCP 最佳实践](https://mcp.harishgarg.com/learn/claude-code-mcp-integrations-hub)
- [Token 节省指南](https://github.com/gino2013/CCTCRG)
- [高级优化配置](https://github.com/drftstatic/PatchPath-AI/blob/main/ADVANCED_CLAUDE_OPTIMIZATIONS.md)

---

## 修复记录

- 2026-07-26：修正 5.2 节 "Bun 迁移" 案例表述（C2）
  - 原文 "Anthropic 内部 — Bun 从 Zig 迁移到 Rust" 易误解为 Bun 是 Anthropic 项目
  - 修正为准确表述：Bun 是 Oven 公司的开源项目，但其联合创始人 Jarred Sumner 同时担任 Anthropic MTS，他使用 Claude Code 完成此次迁移
  - 同时修正 Token 数量级错误：590 万 → 59 亿（输入）、69 万 → 6.9 亿（输出）
  - 来源：Anthropic 官方博客 "How Anthropic runs large-scale code migrations with Claude Code" (claude.com/blog/ai-code-migration)
