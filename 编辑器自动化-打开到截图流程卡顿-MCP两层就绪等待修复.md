# 编辑器自动化-打开到截图流程卡顿-MCP两层就绪等待修复

> 编辑器启动后 AI 在截图前频繁停顿等用户追问，根因是仅依赖 `mcp_connected=true` 判断就绪不充分：TCP 通了但 PythonScriptPlugin 未就绪时 `execute_python` 仍超时。

---

## 一、问题定位流程

**现象**：执行「关闭→打开→截图」全流程时，AI 多次在中间步骤停下来，不继续执行下一步，用户反复追问"你又失败了？""为什么停了？"

**定位过程**：

1. **确认触发点**：每次停顿都发生在 `is_unreal_editor_running` 返回 `mcp_connected: false` 之后——AI 报了一个状态就停了，没有自动轮询
2. **确认第二层问题**：第2轮测试中 MCP TCP 通了（`mcp_connected: true`），但 `execute_python` 仍然超时——说明 TCP 通 ≠ Python 就绪
3. **实测时间线**（第3轮验证通过）：
   - `open_project.py` 返回 → ~45s 后 MCP TCP 通
   - TCP 通 → 立即 ping 通 → 截图成功

**关键数据**：从编辑器进程启动到完整可用，总耗时约 45-75s（含 `open_project.py` 内置的 30s `--wait-verify`）。

---

## 二、根因分析

### 根因 1：Skill 未约束 AI 的自动轮询行为（非技术问题，是规范缺失）

原始 skill 第 ④ 节写的是：

> 若连通失败，返回提示信息并终止：
> "Unreal MCP 未连通。请确认 Unreal Editor 已运行且 MCP 服务已启动。"

AI 读到 `mcp_connected: false` 就报告状态停下来了——skill 没说这是"预期内、需自动重试"的情况。

### 根因 2：MCP 有两层独立的就绪状态

```
UnrealEditor.exe 进程存活
  ↓ 10-60s
MCP TCP server 启动 → mcp_connected: true    ← 第一层
  ↓ 再等几秒
PythonScriptPlugin 初始化完成 → execute_python 可用  ← 第二层
```

`mcp_connected: true` 只表示 C++ 侧 MCP 插件 TCP 端口已监听，但 UE 的 PythonScriptPlugin 是独立模块，初始化略晚。**TCP 通了但 Python 没就绪时，`execute_python` 会超时**。第2轮测试正是卡在这个空窗期。

---

## 三、修复方案

### 修改文件：`C:\Users\djangozhang\.tclaude\skills\编辑器自动化\SKILL.md`

两处改动：

#### 位置 1：第 ③ 节末尾 —— 打开工程后自动等待规范

新增 `### 打开工程后自动等待 MCP 就绪 ⚠️ AI 行为规范`，定义完整等待链：

```
open_project.py 返回成功
  ↓ is_unreal_editor_running → mcp_connected=false（预期内）
  ↓ 自动 sleep 15s → 再查，最长 120s
  ↓ 每轮反馈进度，不要让用户来问
  ↓ mcp_connected=true → 下一步：Python ping
  ↓ 超 120s 不通 → 读 get_editor_log(source=filesystem) 排查
```

#### 位置 2：第 ④ 节开头 —— 截图前置检测

原始规则 "连通失败即终止" 改为两层轮询：

**第一层 — TCP 连通等待**（最长 120s）：
```
is_unreal_editor_running → mcp_connected: false
  ↓ sleep 10-15s 重试
  ↓ mcp_connected: true → 进入第二层
  ↓ 超 120s → 报错并提示查日志
```

**第二层 — Python ping 验证**（最长 60s）：
```
mcp_connected=true
  ↓ execute_python("print('ping')") → 超时→ sleep 10s 重试
  ↓ ping 成功 → 继续截图
  ↓ 超 60s → 读日志排查编辑器是否卡住
```

### 核心设计原则

**等待链的关键原则：做完一步立刻下一步，不要停下来等用户追问。**
- `mcp_connected: false` 不是错误，是预期内的过渡状态
- TCP 通只是必要不充分条件，必须再验证 Python 就绪
- 两层加起来理论最长 180s，实测通常 45-75s 到位

---

## 四、快速诊断 Checklist

| 状态 | 含义 | 动作 |
|---|---|---|
| `running=false` | 编辑器没启动 | 先执行 open_project.py |
| `running=true, mcp_connected=false` | MCP 插件加载中 | 预期内，轮询等（≤120s） |
| `mcp_connected=true`, ping 超时 | PythonScriptPlugin 未就绪 | 轮询等（≤60s） |
| `mcp_connected=true`, ping 通 | 完全就绪 | 可以截图/查 API 了 |
| TCP 等满 120s 不通 | 编辑器可能卡在 PSO | `get_editor_log(source=filesystem)` 查日志 |
| Python 等满 60s 不通 | PythonScriptPlugin 异常 | `get_editor_log(contains="LogPython")` 排查 |

---

## 五、相关参考

- Skill 文件：`C:\Users\djangozhang\.tclaude\skills\编辑器自动化\SKILL.md`
- 编辑器启动脚本：`C:\Users\djangozhang\.workbuddy\skills\编辑器自动化\scripts\open_project.py`
- Unreal MCP 插件：编辑器内 C++ 插件，TCP 端口在模块加载后期打开
- PythonScriptPlugin：UE 内置插件，通过 `execute_python` 调用 `unreal.*` 反射 API
