# TClaude 安装与排障备忘（Windows）

> 来源：iWiki《TClaude》 https://iwiki.woa.com/p/4022204191
> 整理：2026-10-11 · 已在本机实际安装验证通过

## 一、这是什么

TClaude = Claude Code + 腾讯内部网关。用法与官方 Claude Code 完全一致：

- 司内开箱即用（无需自备代理）
- OA 一键登录，与内网身份系统打通
- 内部部署模型 + 外部 Claude 旗舰模型
- 与工蜂安全等级打通（敏感仓库检测、敏感信息脱敏、用量看板）

安装位置：Windows `%LOCALAPPDATA%\tcli\bin`，脚本自动配置用户级 PATH。

---

## 二、⚠️ 网络前提：iOA 是「本地代理」模式，不是虚拟网卡

**这是 90% 安装失败的根因，务必先看这节。**

### 症状

浏览器能打开内网站点，但 `npm` / `curl` / PowerShell 一律 404 或超时。

### 原因

iOA（SmartVPN）走的是 **本地 HTTP 代理 + 本地 DNS**，**不建立虚拟网卡**。所以：

- `Get-NetAdapter` 里 `NGNClient Adapter` 一直显示 `Not Present` —— **这是正常的**，不代表 iOA 没启动
- 系统 DNS（一般是路由器地址）把内网域名解析成**公网 IP**，于是命令行走公网、命中公网镜像、404
- iOA 自己的 DNS 监听在 `127.0.0.1:53`，会把内网域名解析成内网段（如 `100.12.0.x`）

验证一下就能看出差别：

```powershell
# 系统 DNS → 公网 IP（错误）
Resolve-DnsName mirrors.tencent.com | Select-Object Name, IPAddress

# iOA 的 DNS → 内网 IP（正确，100.12.0.x）
Resolve-DnsName mirrors.tencent.com -Server 127.0.0.1 | Select-Object Name, IPAddress
```

### 找到 iOA 的代理端口

```powershell
$pids = (Get-Process SmartVPN -ErrorAction SilentlyContinue).Id
Get-NetTCPConnection -State Listen |
  Where-Object { $pids -contains $_.OwningProcess -and $_.LocalAddress -eq '127.0.0.1' } |
  Select-Object LocalPort -Unique | Sort-Object
```

然后逐个试（实测本机 `12639 / 12649 / 12679 / 12759 / 12769` 是 HTTP 代理，`53` 是 DNS）：

```powershell
curl.exe -sS -x http://127.0.0.1:12639 `
  -o NUL -w "%{http_code}`n" `
  https://mirrors.tencent.com/repository/generic/tclaude/tclaude/LAST_VERSION
# 返回 200 + 版本号 = 通了；404 = 端口不对或 iOA 没登录
```

> 端口在 iOA 重启后可能变化，重跑上面的探测即可。

### 让 npm / curl 走代理

```powershell
# npm（写入 ~/.npmrc）
npm config set proxy http://127.0.0.1:12639
npm config set https-proxy http://127.0.0.1:12639

# curl / 其它命令行（当前会话）
$env:HTTPS_PROXY = 'http://127.0.0.1:12639'
$env:HTTP_PROXY  = 'http://127.0.0.1:12639'
```

---

## 三、安装

### 方式 A：本仓库脚本（推荐，自动探测代理）

```powershell
powershell -ExecutionPolicy Bypass -File .\install-tclaude.ps1
# 手动指定代理端口（自动探测失败时）
powershell -ExecutionPolicy Bypass -File .\install-tclaude.ps1 -Proxy http://127.0.0.1:12639
```

脚本流程：探测 iOA 代理 → 取 `LAST_VERSION` → 下载 `tclaude-<v>-windows-x64.exe`
→ 校验 → 写入用户级 PATH。幂等，重复执行即升级。

### 方式 B：官方一键脚本

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://mirrors.tencent.com/repository/generic/tclaude/tclaude/install.ps1 | iex"
```

### 方式 C：npm 全局安装

```bash
npm config set registry https://mirrors.tencent.com/npm/
npm install -g "@tencent/tclaude@latest" --engine-strict --registry=https://mirrors.tencent.com/npm
```

> **两个坑：**
> 1. **`@` 会被转义**。`@tencent/tclaude@latest` 在聊天/文档粘贴链里可能被当成
>    mention 触发符，变成 `@//tencent/tclaude @//latest/`。**务必加引号**。
> 2. 必须走 iOA 代理（见第二节），否则该 registry 只会返回公网镜像的内容 ——
>    里面只有公开包（`@tencent-sdk/capi`、`eslint-config-tencent`），**没有 `@tencent/tclaude`**。

### 手动安装（最透明，排障用）

```powershell
$p = 'http://127.0.0.1:12639'
$b = 'https://mirrors.tencent.com/repository/generic/tclaude/tclaude'
# 1. 取版本
curl.exe -sS -x $p "$b/LAST_VERSION"          # 例：0.1.8
# 2. 下载
$v = '0.1.8'
mkdir "$env:LOCALAPPDATA\tcli\bin" -Force
curl.exe -L -x $p -o "$env:LOCALAPPDATA\tcli\bin\tclaude.exe" `
  "$b/$v/tclaude-$v-windows-x64.exe"
# 3. 验证
& "$env:LOCALAPPDATA\tcli\bin\tclaude.exe" --version
```

---

## 四、首次登录

```powershell
tclaude login    # 走 iOA / 浏览器完成员工身份登录
tclaude          # 进入交互模式
```

> 若 `tclaude daemon status` 报 `Daemon is not running`，或启动时报
> `daemon: no authentication session` —— **就是还没登录**，先 `tclaude login`。

## 五、常用命令

| 命令 | 说明 |
| --- | --- |
| `tclaude` | 交互模式 |
| `tclaude -p "<prompt>"` | 单次指令模式（脚本 / CI 友好） |
| `tclaude login` / `logout` | 登录 / 退出并清除本地会话 |
| `tclaude update` | 升级 TClaude 本体 |
| `tclaude update [version] --claude` | 升级内置的 Claude Code 运行时 |
| `tclaude daemon <stop\|restart\|status>` | 管理后台网关守护进程 |
| `tclaude -v` / `--version` | 版本信息 |
| `tclaude -h` / `--help` | 帮助 |

参数透传：其余参数原样透传给底层 Claude Code；冲突时用 `--` 分隔
（例：`tclaude -- --help` 显示上游帮助）。

## 六、进阶

### 6.1 自定义模型

配置文件：`~/.tclaude/models.json`（用户级，对所有项目生效）

```json
{
  "models": [
    {
      "id": "my-custom-model",
      "name": "claude-my-custom",
      "url": "https://your-model-service.example.com/v1/messages",
      "apiKey": "${MY_MODEL_API_KEY}",
      "customHeaders": { "X-Team-Id": "my-team" }
    }
  ]
}
```

- `id` = 上游真实模型名；`name` = 本地显示名，**必须以 `claude` 开头**，需 1M 上下文时加 `[1m]`
- 改完 `tclaude daemon restart`，再 `tclaude --model claude-my-custom`

### 6.2 免登录模式（CI/CD）

```powershell
$env:TCLAUDE_AUTH_TOKEN = "your-api-token"   # 优先级高于本地登录态
tclaude -p "分析这段代码"
```

## 七、常见问题

| 现象 | 处理 |
| --- | --- |
| 命令抱 404 / 超时 | **先查第二节** —— 99% 是没走 iOA 代理 |
| `daemon: no authentication session` | 没登录，`tclaude login` |
| `please run /Login 401` | 登录过期，另开窗口 `tclaude login` |
| 403 无权限 | 按内部文档申请权限 |
| 429 | 额度与 CodeBuddy / WorkBuddy 合并，去「用量与额度设置」检查上限 |
| `Login failed: connect ETIMEDOUT` | 先测 `curl copilot.tencent.com`；不通可配 hosts `21.126.58.29 copilot.tencent.com` |
| 一个请求路由到多个模型 | 子 Agent 按复杂度映射档位；可用 `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL` 覆盖 |
| Linux 下 `Couldn't initialize the sign-in service` | inotify 上限不够，调 `fs.inotify.max_user_watches` 等 |

---

## 八、本次实测记录（2026-10-11）

**结果：安装成功并通过验证。**

```
@tencent/tclaude 0.1.8
  commit: b7d3493
  built:  2026-09-21T04:38:09Z
@anthropic-ai/claude-code 2.1.251
```

| 项 | 值 |
| --- | --- |
| 安装路径 | `C:\Users\Administrator\AppData\Local\tcli\bin\tclaude.exe` |
| 大小 | 1.43 MB（Windows x64） |
| PATH | 已写入用户级 PATH |
| iOA 代理 | `http://127.0.0.1:12639`（12649/12679/12759/12769 亦可） |
| iOA 本地 DNS | `127.0.0.1:53` → `mirrors.tencent.com` = `100.12.0.17` |
| 系统 DNS | `192.168.1.1` → `mirrors.tencent.com` = `14.29.51.120`（公网，故直连 404） |
| 待办 | `tclaude login`（需本人浏览器完成 OA 鉴权） |
