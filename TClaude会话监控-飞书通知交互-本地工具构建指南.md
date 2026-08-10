# TClaude会话监控-飞书通知交互-本地工具构建指南

> 为绕过 HAPI（远程监控工具）被公司安全部门禁止的限制，构建了一个纯本地的 TClaude 会话监控 + 飞书通知/交互工具。核心能力：监控会话进度（开始/完成/结论）、通过飞书实时遥控绑定会话、ai-title 可读会话名、绑定模式一对一交互。

---

## 一、问题定位流程

**背景**：想在手机上监控 TClaude（腾讯 fork 的 Claude Code）的执行进度，并在执行完毕后收到通知，还能通过飞书交互。

**约束**：公司安全部门禁止远程监控/隧道/公网暴露类工具（HAPI 方案被否）。

**确认的关键事实**：
1. TClaude 会话数据在 `~/.tclaude/projects/`（不是 `~/.claude/projects/`，腾讯 fork 改了路径）
2. 会话是 `.jsonl` 文件，每行一条记录，含 `timestamp / type / message` 字段
3. `CLAUDE_CODE_SESSION_ID` 环境变量标识当前会话 ID
4. TClaude 支持 `--resume <session-id> --print` 接已有会话执行并返回结果
5. `ai-title` 记录提供 tclaude 自动生成的任务标题

## 二、根因分析

| 问题 | 根因 | 方案 |
|---|---|---|
| 无法远程监控 | HAPI 官方 relay 底层 tunwg 服务器 `l.tunwg.com` 在本网络不可达（TLS 坏包 + HTTP 502） | 改走纯本地 + 飞书官方 API |
| 飞书长连接收不到事件 | 裸 WebSocket 无法接入，必须用官方 SDK | 用 `lark-oapi` 官方 SDK 的 `lark.ws.Client` |
| 飞书发消息报 `99992402 field validation failed` | `receive_id_type` 必须在 **query 参数**而非 body | 改 query 传参 |
| 会话名是 UUID 不可读 | 会话 jsonl 里有 `ai-title`（tclaude 自动生成标题） | 优先用 ai-title，回退首条用户指令 |
| 事件字段解析失败 | SDK 对象字段是 `message_type` / `content`（非 `msg_type` / `body.content`） | 按 SDK 实际字段解析 |
| 动作通知太吵 | 每个命令都推送 | 加 `notify_actions` 开关（默认 false），只留开始/完成 |

## 三、详细技术原理

### 1. 架构
```
[飞书 app] ⇄(长连接 lark-oapi SDK)⇄ [feishu_client.py]
                                          │
                             [main.py] ──┤
                                          ├ [monitor.py] ── 轮询会话 jsonl（进度/动作/结论）
                                          └ [tclaude_driver.py] ──(--resume --print)⇄ tclaude
```

### 2. 会话监控状态机
`monitor.py` 轮询会话 jsonl，检测：
- **start**：会话文件开始写入（mtime 在 idle_threshold 内）
- **action**：新增 tool_use 动作（Bash/Edit/Read 等）
- **heartbeat**：运行超时的心跳兜底
- **done**：文件停止更新超 idle_threshold

提取方法：
- `_extract_current_task`：最近一条真实用户指令（跳过 `<command>` 包装）
- `_extract_conclusion`：最后一条 assistant 文本（结论）

### 3. 绑定模式
- `bind.py` 读 `$CLAUDE_CODE_SESSION_ID` 写入 `config.json` 的 `bound_session_id`
- 绑定模式下只监控/交互这一个会话（飞书消息只驱动绑定会话）
- 单值绑定，后绑定的覆盖先绑定的

### 4. 飞书接入关键点（踩坑记录）
- **token**：`POST /auth/v3/tenant_access_token/internal` 带 app_id/app_secret
- **发消息**：`POST /im/v1/messages?receive_id_type=chat_id`，`content` 是 JSON 字符串，`receive_id_type` 放 query
- **长连接**：必须用 `lark-oapi` SDK：
  ```python
  event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handler).build()
  cli = lark.ws.Client(app_id, app_secret, event_handler=event_handler)
  cli.start()
  ```
- **事件对象字段**：`event.event.message` → `.chat_id` / `.message_type` / `.content`
- **应用发布**：事件订阅改动必须重新发布版本才生效

### 5. 路径（重要）
- 会话目录：`C:/Users/djangozhang/.tclaude/projects/<项目>/*.jsonl`
- 工具目录：`C:/Users/djangozhang/Desktop/MobileWorkSpace/feishu_monitor/`

## 四、修复方案（完整实现）

### 文件清单
```
feishu_monitor/
├── main.py              # 主程序（单进程整合）
├── monitor.py           # 会话进度状态机 + 动作/任务/结论提取
├── feishu_client.py     # 飞书 token/发消息/长连接（lark-oapi）
├── tclaude_driver.py    # 驱动 tclaude 会话（--resume --print）
├── bind.py              # 绑定当前会话到飞书
├── config.json          # 配置（飞书凭证 + 监控参数 + bound_session_id）
├── config.example.json  # 配置模板
├── README.md            # 使用说明
└── 飞书配置指引.md       # 建飞书应用步骤
```

### Skill「连接飞书」
```
~/.tclaude/skills/连接飞书/
├── SKILL.md                        # 触发词 + 步骤
└── scripts/connect_feishu.py       # 一键连接（绑定+配置检查+启动+验证）
```

### 关键代码片段

**monitor.py 状态机 + 提取**：
```python
class SessionMonitor:
    def __init__(self, project_dirs, poll_interval=3, idle_threshold=90,
                 heartbeat_interval=60, bound_session_id=None):
        self.project_dirs = ...      # 支持多工作区
        self.bound_session_id = ...  # 绑定模式只监控这一个

    def _extract_current_task(self, path):  # 最近用户指令
    def _extract_conclusion(self, path):    # 最后 assistant 文本
```

**feishu_client.py 长连接（官方 SDK）**：
```python
def run_event_loop(self, on_message):
    import lark_oapi as lark
    def _handler(data):
        try: on_message(data)
        except Exception as e: log.error(...)
    eh = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_handler).build()
    cli = lark.ws.Client(self.app_id, self.app_secret,
                         event_handler=eh, log_level=lark.LogLevel.INFO)
    cli.start()  # 阻塞，SDK 内部自动重连
```

**tclaude_driver.py 驱动会话**：
```python
def resume_and_run(self, session_id, prompt):
    cmd = [self.tclaude, "--resume", session_id, "--print", prompt]
    # subprocess.run 捕获输出
```

**bind.py 绑定当前会话**：
```python
sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
cfg["bound_session_id"] = sid
```

**Windows 控制台 UTF-8 修复**（普通终端跑脚本不报编码错误）：
```python
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

## 五、快速排查 Checklist

- [ ] 飞书应用已建（open.feishu.cn 企业自建应用），AppID/AppSecret 填入 config.json
- [ ] 应用开启「机器人」能力 + 权限 `im:message` / `im:message:send_as_bot` / `im:message.p2p_msg:readonly`
- [ ] 事件订阅选「长连接」（不暴露 URL）+ 订阅 `im.message.receive_v1`
- [ ] 事件订阅改动后**重新发布版本**（否则不生效）
- [ ] 用户先在飞书给机器人发消息建立会话 → 拿到 chat_id
- [ ] `receive_id_type` 放 query 参数（否则 99992402）
- [ ] 长连接用 lark-oapi SDK（裸 WebSocket 404）
- [ ] 在 tclaude 会话里跑 `python bind.py`（读 CLAUDE_CODE_SESSION_ID）
- [ ] 绑定后重启工具 `python main.py`
- [ ] **重启后核对日志绑定值**：`grep "绑定模式" ~/.hapi/feishu-monitor.log | tail -1` 的 sid 必须 = `$CLAUDE_CODE_SESSION_ID`
- [ ] 跑 `python main.py --check` 自检发测试消息
- [ ] 普通终端跑脚本报 `UnicodeEncodeError` → 已内置 UTF-8 reconfigure

## 五·五、绑定模式故障排查（重要）

**症状**：`/sessions` 显示会话 A，但飞书通知的是会话 B。

**根因**：工具启动时读取一次 `bound_session_id`，之后 config 被其他会话改（另一个会话跑了 bind.py / connect_feishu.py）**不会热更新**。config 和运行实例脱节。

**排查三步**：
```bash
# ① config 里绑定的会话
grep bound_session_id feishu_monitor/config.json
# ② 工具实际运行的会话（日志绑定行）
grep "绑定模式" ~/.hapi/feishu-monitor.log | tail -1
# ③ 若 ①≠②，config 被覆盖了 → 跑「连接飞书」skill 或改 config 后重启工具
```

**规则**：每次改绑定**必须重启工具**，且以日志 `绑定模式` 行为准（不是 config，不是 /sessions）。

## 五·六、通知策略演进（2026-08-11 更新）

**需求变化**：从「实时动作流 + 阶段通知」精简为「只留开始/完成」，并支持「每轮完成通知」。

### 演进历程
| 版本 | 通知内容 | 问题 |
|---|---|---|
| v1 动作流 | 每个 tool_use 都推（🖥️/✏️/📖） | 太吵，刷屏 |
| v2 精简 | `notify_actions: false`，只留开始/完成 | `🎯 当前任务` 取最近 user 消息 → 混入飞书遥控消息/通知转发，噪音 |
| **v3 当前** | 去掉 `🎯`；`done` 带 `💡 结论`；绑定模式加**每轮完成** | ✅ 干净 |

### 关键改动
1. **`🎯 当前任务` 移除**：`📌` 会话名（ai-title）已是任务标题，再提取 user 消息会混入飞书遥控指令（`--resume` 注入的 user 消息也是 user 类型），反而引入噪音。
2. **`done` 带结论**：`_extract_conclusion` 取最后一条 assistant 文本。
3. **每轮完成通知（`round_done`）**：绑定模式下，检测到**新增 assistant 文本**即判定一轮完成，立即发「✅ 本轮完成 + 💡 结论」，无需等 `idle_threshold` 静默。

**monitor.py 回合检测**：
```python
self._last_round_line = {}  # session_id -> 已读回合行号

def _read_new_round_conclusion(self, sid, path):
    # 从 _last_round_line 读到文件尾，找新增 assistant 文本
    # 有则更新 _last_round_line 并返回结论；无则 None
```
`_scan_one` 中（绑定模式 + 活跃会话）：
```python
if self.bound_session_id and sid == self.bound_session_id:
    conc = self._read_new_round_conclusion(sid, path)
    if conc:
        events.append({"type": "round_done", ..., "conclusion": conc})
```

### 当前通知形态
```
🚀 任务开始
📌 Deploy Hapi monitoring tool for mobile access [MobileWorkSpace]

✅ 本轮完成            # 每轮（assistant 文本出现）
📌 Deploy Hapi ...
💡 <tclaude 这轮结论>

✅ 任务完成            # 会话静默 90s（长任务兜底）
📌 Deploy Hapi ...
💡 <最后结论>
```

## 五·七、进程管理教训（2026-08-11）

**症状**：connect_feishu.py 启动的新工具「启动后又消失」，飞书始终收不到新版本的通知。

**根因**：`start_monitor()` 用 `taskkill //F //IM python.exe` 停旧工具——但 **connect_feishu.py 本身也是 python.exe，Popen 启动的 main.py 也是 python.exe**。`taskkill` 执行瞬间把「自己 + 刚启动的新工具」一起杀了（连坐杀）。

**修复**：改用 PowerShell 按命令行精确过滤，只杀 `main.py`：
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**规则**：
- 重启工具**永远不要用 `taskkill //F //IM python.exe`**（连坐杀）
- 用 connect_feishu.py（内置精确杀）或手动 `python main.py`
- 教训已固化到「连接飞书」skill 的 Pitfalls

## 六、相关参考

- 飞书开放平台：https://open.feishu.cn/
- 飞书 Python SDK（lark-oapi）：https://github.com/larksuite/oapi-sdk-python
- 飞书长连接事件处理：https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events.md
- 飞书发消息 API：https://open.feishu.cn/document/server-docs/im-v1/message/create
- HAPI（被否方案参考）：https://github.com/tiann/hapi
