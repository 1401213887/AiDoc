# UnrealMCP 连不上（mcp_connected: false）排查：插件未启用 + 端口三方不一致 + UCLASS(config=Editor) 决定配置文件位置

> 编辑器正常启动、进程存活，但 UnrealMCP 通道始终 `mcp_connected: false`。排查出**两个独立根因**：① `S1Game.uproject` 的 165 个插件里只启用了 `UnrealBridgeMcp`（HTTP 8080），真正要用的 `UnrealMCP`（TCP）根本没在启用列表里；② 启用后端口又对不上 —— 插件实际监听 `55557`（代码默认），而配置文件里写的 `58080`、客户端 `.claude.json` 要的 `58123`。第 ② 个根因的难点在于**改错了文件**：`UCLASS(config = Editor)` 意味着它读的是 `Editor.ini`，而项目里那个 `EditorPerProjectUserSettings.ini` 下的同名 section 是**历史遗留的野段，插件根本不读**。

---

## 一、问题定位流程（确认了什么）

| 步骤 | 命令 / 工具 | 确认结论 |
|---|---|---|
| 1 | `netstat -ano \| grep :<port>` | 目标端口无 `LISTENING` —— 没有服务在听 |
| 2 | 编辑器日志 grep `UnrealMCP` | 只有 `UnrealBridgeMcp` 在注册（`Registered 36 MCP tools` / `Starting MCP Server on 127.0.0.1:8080`），**`UnrealMCP` 完全没有日志** |
| 3 | 检查 `S1Game.uproject` 的 `Plugins` 数组 | 165 个插件里**没有 `UnrealMCP` 条目**，只有 `UnrealBridgeMcp` |
| 4 | 启用插件后重启，日志出现 `UnrealMCPBridge: Server started on 0.0.0.0:55557` | 插件起来了，但端口是 **55557** |
| 5 | 三方端口比对 | 插件 55557 / UE 配置 58080 / 客户端 58123 —— **三方全不一致** |
| 6 | 改 `EditorPerProjectUserSettings.ini` 的 `Port` 后重启 | **无效**，插件仍起 55557 → 说明该文件不被读取 |
| 7 | 读 `MCPSettings.h` 的 `UCLASS` 声明 | `UCLASS(config = Editor, defaultconfig)` → 真正该改的是 **`Editor.ini`** |

## 二、根因 1：插件未在 uproject 启用

`S1Game.uproject` 的 `Plugins` 数组里，MCP 相关只有一条：

```json
{ "Name": "UnrealBridgeMcp", "Enabled": true, "TargetAllowList": ["Editor"] }
```

`UnrealMCP` 的 DLL 编译产物**存在**（`Plugins/UnrealMCP/Binaries/Win64/UnrealEditor-UnrealMCP.dll`），插件目录也在，但**不在 uproject 的启用列表里** → 编辑器不会加载它。

> ⚠️ 同一工程里可以同时存在多个 MCP 类插件，各自监听不同端口/协议。排查时**必须先确认日志里是哪个插件在起服务**，别把 A 插件当成 B 插件。

### 修法

```json
{ "Name": "UnrealBridgeMcp", "Enabled": false, "TargetAllowList": ["Editor"] },
{ "Name": "UnrealMCP",       "Enabled": true,  "TargetAllowList": ["Editor"] }
```

`S1Game.uproject` 在 P4 上，改前需 `p4 edit`。**注意这是全组共享文件** —— `p4 edit` 时若提示已被其他同事迁出，提交时会撞冲突。

## 三、根因 2：端口三方不一致，且改错了文件

### 3.1 端口有两个消费方

| 消费方 | 文件 | 谁读 |
|---|---|---|
| UE 侧（编辑器监听端口） | `S1Game/Saved/Config/WindowsEditor/Editor.ini` | 编辑器内的 `UMCPSettings` |
| Python 侧（MCP server 连哪） | `C:/Users/<user>/.claude.json` 的 `unrealMCP.env.UNREAL_PORT` | `unreal_mcp_server.py` |

### 3.2 配置文件位置由 `UCLASS(config=...)` 决定 —— 必须反查代码

```cpp
// Plugins/UnrealMCP/Source/UnrealMCP/Public/MCPSettings.h:11
UCLASS(config = Editor, defaultconfig, meta = (DisplayName = "MCP Settings"))
class UNREALMCP_API UMCPSettings : public UDeveloperSettings
{
    UPROPERTY(config, EditAnywhere, Category = "MCP|Network", ...)
    int32 Port = 55557;              // :21 代码默认值
```

`config = Editor` → 读的是 **`Editor.ini` 层级**：

| 层 | 路径 | 优先级 | 在 P4 |
|---|---|---|---|
| 用户层（**实际生效、推荐改这里**） | `S1Game/Saved/Config/WindowsEditor/Editor.ini` | 高 | ❌ 不在（`p4 fstat` 返回 `no such file`，无需 `p4 edit`） |
| 项目默认层 | `S1Game/Config/DefaultEditor.ini` | 低 | ✅ 在（会设给全组，需谨慎） |

写法：

```ini
[/Script/UnrealMCP.MCPSettings]
Port=55557
```

> **⚠️ 陷阱：`Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini` 里有一个同名 section，但它是死配置。**
> ```
> [/Script/UnrealMCP.MCPSettings]
> Port=58080        ← 改它无任何效果（实测：改成 58123 重启后插件照样起 55557）
> ```
> 成因推测：早期版本的插件类可能声明为 `config=EditorPerProjectUserSettings`，改代码后该段成了孤儿。
> **判据：换任何 `UCLASS(config=X)` 的插件设置，先按类声明的 `config=` 反查文件，别信文档或记忆里的路径。**

## 四、关键机制：编辑器会回写 ini —— 改配置前必须先杀编辑器

`FConfigFile` 写盘的实现决定了两件事：

```cpp
// ConfigCacheIni.cpp:2219 —— WriteToStringInternal
for (TIterator SectionIterator(*this); SectionIterator; ++SectionIterator)   // 遍历【内存里的全部 section】
...
// ConfigCacheIni.cpp:2192 —— WriteInternal
if (!Dirty || NoSave || !AreWritesAllowedGlobally()) { return true; }         // 只有 Dirty 才真写盘
```

推论：

1. **写盘时 dump 的是内存里的全部 section**，不只是被改过的那个 → 编辑器启动时读进去的内容，会在下一次写盘时原样落回磁盘。
2. 因此**在编辑器运行期间删除某个 section 会被回写复活**。要让删除/修改生效，必须先 `taskkill` 掉编辑器（强杀不留写盘机会），改完再启动。

实测印证：删除野段后重启编辑器，该文件 mtime 被更新（编辑器写回过），而野段**没有回来** → 证明新进程的内存里已不含该 section，删除是持久的。

## 五、`/mcp` 什么时候需要重连

MCP server 是 harness 管理的 stdio 子进程，**启动时**把 `UNREAL_PORT` 定死在 env 里。harness 不会自动重启断开的 server。

| 改动位置 | 是否需要 `/mcp` | 原因 |
|---|---|---|
| 只改 **UE 侧**（`Editor.ini`） | ❌ **不需要** | 客户端 env 没变；编辑器起来监听对端口后，现有 server **自己重连成功**（实测：`is_unreal_editor_running` 直接返回 `mcp_connected: true`） |
| 改 **客户端 `.claude.json`** | ✅ **必须** | 运行中 server 的 env 已定死，对当前进程无效；必须用户敲 `/mcp`（或重启会话）让 harness 用新 env 拉起新 server |

## 六、`is_unreal_editor_running` 的 `running:true` 可能是僵尸进程误报

`taskkill` 后进程会以 **0 线程 / 0 句柄 / ≈0.1MB 工作集**的形态残留在进程表（内核未回收，实测挂过近一天）。此时：

```
is_unreal_editor_running → { running: true, mcp_connected: false }    ← 误导
netstat                  → 无任何相关端口 LISTENING                    ← 真相：编辑器根本没开
```

只看 `running` 字段会误判成"编辑器开着但 MCP 没起来"，从而去查端口 —— **方向全错**。

**判据要加一条：有没有端口在 `LISTENING`**（或看线程数）：

```bash
powershell -NoProfile -Command "Get-Process UnrealEditor -ErrorAction SilentlyContinue | Select-Object Id,@{n='Threads';e={\$_.Threads.Count}}"
# Threads=0 → 僵尸，无执行能力，也不能回写 ini
```

## 七、修复后的验证顺序

```
1. netstat -ano | grep :<port>            → LISTENING
2. 编辑器日志 grep "UnrealMCPBridge: Server started"  → 确认监听端口与配置一致
3. is_unreal_editor_running               → mcp_connected: true
4. execute_python("print('ping')")        → 通（TCP 通 ≠ PythonScriptPlugin 已就绪，必须 ping）
```

## 八、快速排查 Checklist

**MCP 连不上时按序走，从最便宜的开始：**

1. **编辑器日志里是哪个 MCP 插件在起服务？** → `grep -iE "MCP.*(starting|Server started)"`。没有目标插件的日志 → 查 §二（uproject 启用状态）
2. **端口对不对？** → `netstat -ano | grep :<port>`；插件实际监听的端口以日志 `Server started on 0.0.0.0:<port>` 为权威
3. **UE 侧端口配置改对文件了吗？** → 反查插件 Settings 类的 `UCLASS(config=...)`（§3.2），别信文档记的路径
4. **改配置前杀编辑器了吗？** → 没杀 → 退出时回写覆盖（§四）
5. **改的是客户端还是 UE 侧？** → 客户端改动必须 `/mcp` 重连（§五）
6. **`running:true` 是真进程吗？** → 看线程数/端口监听，排除僵尸（§六）

## 九、关键位置速查

| 位置 | 作用 |
|---|---|
| `Plugins/UnrealMCP/Source/UnrealMCP/Public/MCPSettings.h:11` | `UCLASS(config = Editor, defaultconfig)` ← 决定配置文件位置 |
| `MCPSettings.h:21` | `int32 Port = 55557` 代码默认值 |
| `MCPSettings.h:26` | `BindAddress = "0.0.0.0"` |
| `Plugins/UnrealMCP/Source/UnrealMCP/Private/UnrealMCPBridge.cpp:105/160/176` | 从 Settings 取 Port、bind、打印 `Server started on ...` |
| `S1Game/Saved/Config/WindowsEditor/Editor.ini` | **UE 侧端口实际生效位置**（用户层） |
| `S1Game/Config/DefaultEditor.ini` | 项目默认层（P4 管，本环境无 MCP 段） |
| `S1Game/Saved/Config/WindowsEditor/EditorPerProjectUserSettings.ini` | ⚠️ 含**死配置** section，插件不读 |
| `S1Game/S1Game.uproject` | 插件启用开关（P4 管，全组共享） |
| `C:/Users/<user>/.tclaude/.claude.json` | `mcpServers.unrealMCP.env.UNREAL_PORT` |
| `ConfigCacheIni.cpp:2192 / :2219` | `WriteInternal` 的 Dirty 门控 / `WriteToStringInternal` 遍历全部 section |
| `EditorViewportClient.cpp`（无关，勿混淆） | `ld.RenderEditorPrimitives` 等编辑器 CVar |

## 十、相关参考

- unreal-mcp（MCP 插件来源，117 工具 / 12 scope）：https://github.com/mscrnt/unreal-mcp
- 排查规划记录：`D:\GR_DevTest\.planning\2026-09-12-mobile-basepass-maxdc-and-statunit-row\progress.md`（"阻塞解除：MCP 通道修复"一节）
- 编辑器自动化 skill 内的同主题章节（已按本文纠正）：`C:\Users\<user>\.tclaude\skills\ZXBUnrealDebug\SKILL.md` → 「MCP 端口配置与改端口流程」
- 关联：`E:\AiDoc\UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md`（同为引擎配置层级/通道的排查方法）
