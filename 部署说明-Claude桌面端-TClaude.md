# Claude Desktop + TClaude 本机部署说明（2026-10-29）

> 依据飞书云文档《TCodex/TClaude+Codex/Claude桌面端配置》(wiki token `TAiawIrmZiOtVVkre8Lcbbphn5e`)
> + 本机实测适配。原文档假设用 tclaude.exe 原生安装，本机是 **npm 版 @tencent/tclaude**，
> 端口发现与客户端安装方式都需要改，改动点见下。

## 一、最终链路

```
Claude Desktop 4.29.0 (3P/Gateway)
        │  inferenceGatewayBaseUrl = http://127.0.0.1:57284
        ▼
tclaude_desktop_proxy.py :57284      固定端口，改写模型名
        │  自动发现上游端口（读 metadata.json）
        ▼
TClaude daemon :57667（每次重启换端口）
        ▼
腾讯内网模型网关（GLM-5.3 / Kimi K3 / DeepSeek-V4.1-Flash / Opus 5 等 18 个）
```

关键设计不变：**客户端只认固定端口 57284，代理自动跟随 daemon 的随机端口**。
启动脚本永远不要传 `--upstream-port`。

## 二、本机与文档的三处差异（必须改）

### 1. daemon 端口文件位置

| | 文档假设 | 本机实际 |
|---|---|---|
| 端口来源 | `~/.tclaude/daemon.json` | **`~/.tclaude/tcli/daemon/metadata.json`**（npm 版不写 daemon.json） |

已给 `proxies/tclaude_desktop_proxy.py` 的 `read_daemon_port()` 增加第三优先级：
`daemon.json` → `daemon-port` → **`tcli/daemon/metadata.json`**。daemon 重启后代理重新读该文件即可跟随。

### 2. Claude Desktop 安装包

- 文档只说"下载 Claude 桌面端"。本机一开始没有安装任何版本。
- **坑**：`storage.googleapis.com/.../nest-win-x64/Claude-Setup-x64.exe` 是老 Squirrel 版
  **0.14.10**，装完没有 3P 网关能力（3P 要求 ≥1.2581.0），而且会占用
  `com.squirrel.AnthropicClaude.claude` 这个开始菜单入口。
- **正解**：用官方 MSIX 最新版
  `https://claude.ai/api/desktop/win32/x64/msix/latest/redirect`
  → 实测拿到 `releases/win32/x64/4.29.0/Claude-*.msix`（270MB）
  → `Add-AppxPackage -Path Claude.msix`（免管理员，per-user 安装成功）
  → 启动用 `shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude`

### 3. 3P 配置需要两个文件（工具包只给了一个）

工具包 `config/claude-3p-configLibrary.json` 只是 profile 本体，首次在**没配置过 3P**的机器上
必须同时给出"部署模式"和"哪个 profile 生效"：

```
%LOCALAPPDATA%\Claude-3p\
├── claude_desktop_config.json                      {"deploymentMode": "3p"}
└── configLibrary\
    ├── _meta.json                                   {"appliedId": "<profile-id>", "entries": [...]}
    └── 00000000-0000-4000-8000-000000157210.json    真正的网关配置（18 个模型）
```

`_meta.json` 里 `appliedId` 必须等于 profile 文件名（去掉 .json）。
两条都到位后启动客户端，左下角显示 `djangozhang · Gateway` 即生效。

## 三、常驻与一键启动

| 组件 | 常驻方式 |
|---|---|
| TClaude daemon | `tclaude` 自带，随 CLI 使用拉起；异常时 `tclaude daemon restart` |
| 重写代理 :57284 | **计划任务 `TClaudeDesktopProxy`**（登录时自动启动，`run_proxy.cmd` 包装） |

一键启动：桌面 `打开Claude(TClaude).cmd`（源文件 `D:\ProjMainD\open_claude_gateway.cmd`）
四步：查 daemon → 查/起代理 → 校验 3P 配置 → 启动 Claude。

### 为什么代理不能用 Start-Process 起

本机 `Start-Process -RedirectStandardOutput` 会因环境变量 `HTTPS_PROXY`/`https_proxy`
重复键直接抛异常；而工具沙箱里起的子进程在调用结束后会被回收。
所以最终用**计划任务**托管（等价于用户真实终端启动），并把 stdout/stderr 落到
`D:\ProjMainD\tclaude_proxy_run.log(.err)`。

## 四、验证方法

```bash
curl -s http://127.0.0.1:57284/v1/models        # 应返回 18 条 claude-* 别名
```

真实链路（必须带 `X-Claude-Code-Session-Id`，daemon 缺这个头会 400）：

```bash
curl -s -X POST http://127.0.0.1:57284/v1/messages \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "X-Claude-Code-Session-Id: desktop-session-1" \
  -d '{"model":"claude-opus-4-7","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'
# 实测返回 model=glm-5.3 → 说明别名 claude-opus-4-7 已正确反查为 GLM-5.3
```

代理日志判据：`[MODEL-REWRITE]`（模型列表改写）与 `[REWRITE] model: a -> b`（请求体反查）都出现 = 链路通。

## 五、模型别名（18 个，客户端看到的名字）

| 客户端名 | 显示 | 实际模型 |
|---|---|---|
| `claude-opus-4-6` | Kimi K3 | `claude-kimi-k3[1m]` |
| `claude-opus-4-7` | GLM-5.3 | `claude-glm-5.3[1m]` |
| `claude-opus-4-8` | DeepSeek-V4.1-Flash | `claude-deepseek-v4.1-flash[1m]` |
| `claude-sonnet-4-6` | HY4-Preview | `claude-hy4-preview[1m]` |
| `claude-opus-5` / `claude-sonnet-5` | Opus 5 / Sonnet 5 | 原生 |
| `claude-opus-5-5` / `claude-sonnet-5-5` | Opus 5.5 / Sonnet 5.5 | 裸 id，不能带 `[1m]`（会 11102） |

推理档位（effort）只认客户端内置能力表的 8 个槽位：`claude-opus-5`、`claude-sonnet-5`、
`claude-opus-4-8`、`claude-opus-4-7`、`claude-opus-4-6`、`claude-sonnet-4-6`、
`claude-fable-5`、`claude-fable-5-1`。常用的三个 IOA 模型已固定挂在这些槽位上，不要随意挪。

## 六、飞书文档的读取方式（本次打通）

飞书 wiki 有登录墙，WebFetch 只会拿到登录页。本机 `lark-cli`
（`@larksuite/cli`，配置在 `~/.lark-cli/config.json`，用户身份=张建国）已带
`wiki:node:read` / `docs:document.content:read` 等 scope，可直接读：

```bash
lark-cli.cmd docs +fetch --doc '<wiki-url>' --as user --doc-format markdown
lark-cli.cmd docs +media-download --token <file_token> --output ./x.zip --as user
```

注意：`drive +download` 对文档内嵌附件会 403，必须走 `docs +media-download`；
下载落盘路径必须在 cwd / temp / `~/files` 之内。
