# UnrealMobileDeviceViewer CVar 提示漏抓 — FAutoConsoleCommand 限定名正则缺陷

> 现象：在 S1 Mobile Device Viewer 的控制台输入框敲 `wp.Runtime.HLOD` 没有任何补全提示，
> 但它的兄弟项 `wp.Runtime.HLOD.WarmupEnabled` 等 10 条却在库里。
> 根因：离线 CVar 扫描脚本 `CollectEngineCVars.py` 的 `_ENTRY_START` 第三个分支多要求了一个
> `\(`，导致"类外定义的静态成员 `FAutoConsoleCommand`"（`Type::Member` 形式）**整类**漏抓，
> 全仓库共 **26 条**命令因此没有提示。

---

## 一、问题定位流程

| 步骤 | 动作 | 结论 |
|------|------|------|
| 1 | 在生成物 `EngineCVarDatabase.ini` 里查 `wp.Runtime` | 91 条，含 `wp.Runtime.HLOD.Warmup*` 10 条，**无裸的 `wp.Runtime.HLOD`** |
| 2 | 全源码 grep 注册点 | 命中 `HLODRuntimeSubsystem.cpp:160`，确认是真实注册的 `FAutoConsoleCommand` |
| 3 | **导入真实脚本的正则**（非手工转写）对源码实测 | `_ENTRY_START` 在该行 **NO MATCH** |
| 4 | 用宽松正则全量统计 `FAutoConsoleCommand` 注册点 vs 库内容 | 521 个注册点 / 495 入库 / **26 漏**，26 条**全部**是限定名，plain 形式 **0 漏** |
| 5 | 原型补丁 + 全量重跑验证 | 新增 29 条、**回归 0 条** |

关键方法：第 3 步用 `importlib` 直接加载 `CollectEngineCVars.py` 并复用其 `_ENTRY_START` /
`CVAR_ENTRY_RE` 等模块级正则对象，**而不是把正则抄一遍**。抄写会引入偏差，第一次抄写时就多写了
一个 `"`，导致误判"限定名 Command 命中 0"。正则调试必须对真源。

---

## 二、根因分析

### 2.1 核心缺陷：三个分支对 `\(` 的要求不对称

`Tools/CollectEngineCVars.py` 中定义"一条注册语句起始"的 `_ENTRY_START`：

```python
_ENTRY_START = (r'(?:TAutoConsoleVariable\s*<[^>]+>\s*\w+|FAutoConsoleVariable\w*\s+\w+|'
                r'FAutoConsoleCommand\w*\s+\w+\s*\(|'          # ← 缺陷所在：多了 \s*\(
                r'IConsoleManager\s*::\s*Get\s*\(\s*\)\s*\.\s*'
                r'Register(?:ConsoleVariable|ConsoleCommand))')
```

| 分支 | 正则片段 | 名字后必须紧跟 `(`？ |
|------|----------|----------------------|
| 1 | `TAutoConsoleVariable\s*<[^>]+>\s*\w+` | 否 |
| 2 | `FAutoConsoleVariable\w*\s+\w+` | 否 |
| 3 | `FAutoConsoleCommand\w*\s+\w+\s*\(` | **是 ← 缺陷** |

分支 1/2 只吃到类名就停，后面残留的 `::Member(` 由 tempered span `_ANY{0,400}?` 一路跳过，
正好够到 `TEXT("名字")`。分支 3 多要求一个 `\(`，遇到 `::` 直接匹配失败。

**因此限定名形式下，`TAutoConsoleVariable` / `FAutoConsoleVariableRef` 反而能抓到，
只有 `FAutoConsoleCommand` 全军覆没。**

### 2.2 实证

源码 `UE5EA/Engine/Source/Runtime/Engine/Private/WorldPartition/HLOD/HLODRuntimeSubsystem.cpp:160`：

```cpp
FAutoConsoleCommand UWorldPartitionHLODRuntimeSubsystem::EnableHLODCommand(
	TEXT("wp.Runtime.HLOD"),
	TEXT("Turn on/off loading & rendering of world partition HLODs."),
	FConsoleCommandWithArgsDelegate::CreateLambda([](const TArray<FString>& Args)
	{
		UWorldPartitionHLODRuntimeSubsystem::WorldPartitionHLODEnabled = (Args.Num() != 1) || (Args[0] != TEXT("0"));
		/* ... */
	})
);
```

用真实 `_ENTRY_START` 单点匹配：

```
FAutoConsoleCommand UWorldPartitionHLODRuntimeSubsystem::EnableHLODCommand(   -> NO MATCH
FAutoConsoleCommand EnableHLODCommand(                                        -> MATCH
```

`AUTOCOMMAND_MEMBER_RE`（第 7 类入口，处理构造函数初始化列表）也救不了：
它的 `(?:^|,)\s*[A-Za-z_]\w*\s*\(` 同样吃不下名字里的 `::`。

### 2.3 影响面

- `FAutoConsoleCommand` 注册点：**521**
- 已入库：**495**
- 漏抓：**26**，其中限定名 **26**、plain 形式 **0**

对照组验证（排除其它成因）：非限定形式的 `FAutoConsoleCommand` **495/495 全部在库**，
相关性 100%。

漏掉的 26 条：

| 类别 | 条目 |
|------|------|
| WorldPartition | `wp.Runtime.HLOD`、`wp.Runtime.DumpDataLayers`、`wp.Runtime.DebugFilterByRuntimeHashGridName`、`wp.Runtime.DebugFilterByDataLayer`、`wp.Runtime.DebugFilterByStreamingStatus`、`wp.Runtime.DebugFilterByCellName` |
| GameplayDebugger | `gdt.Enable`、`gdt.Toggle`、`gdt.SelectLocalPlayer`、`gdt.SelectPreviousRow`、`gdt.SelectNextRow`、`gdt.ToggleCategory`、`gdt.EnableCategoryName`、`gdt.fontsize`、`EnableGDT` |
| Gauntlet（S1Game） | `Gauntlet.EnableClientAI`、`Gauntlet.ExecLua`、`Gauntlet.Req.PipelineSever`、`Gauntlet.CheckInMap`、`Gauntlet.SetSettingInt`、`Gauntlet.SetSettingFloat`、`Gauntlet.SetSettingIntArray` |
| 其它 | `r.CopyLockedViews`、`SaveGameStreamerImportReplays`、`SaveGameStreamerExportReplays`、`SaveGameStreamerSanitizedUnsavedNames` |

---

## 三、详细技术原理

### 3.1 扫描器整体结构

`CollectEngineCVars.py` 全量扫描 `UE5EA/Engine/Source` + `S1Game/Source` + `S1Game/Plugins`
（约 42,757 个 `.cpp/.h/.inl`），产出 `EngineCVarDatabase.ini`，供独立 Program 启动时
把 CVar 名"占位注册"进 `IConsoleManager`，从而让自带 `SConsoleInputBox` 的补全覆盖度接近编辑器。

收集入口分两大类：

**A. 主正则 `CVAR_ENTRY_RE`（覆盖 7 种注册写法）**

```python
CVAR_ENTRY_RE = re.compile(
    _ENTRY_START
    + _ANY + r'{0,400}?'                                # tempered span，至多向后 400 字符
    + r'TEXT\s*\(\s*"([^"]+)"\s*\)'                     # group 1: name
    + r'(?:' + _ANY + r'{0,300}?'                       # 可选 help
    + r'TEXT\s*\(\s*((?:"[^"]*"\s*)+)\)'                # group 2: 多段字面量 help
    + r')?'
    , re.MULTILINE)
```

其中 tempered span 的作用是**禁止跨越到下一条注册语句**：

```python
_ANY = (r'(?:(?!TAutoConsoleVariable|FAutoConsoleVariable|FAutoConsoleCommand|'
        r'RegisterConsoleVariable|RegisterConsoleCommand)[\s\S])')
```

**B. 三类专门收集**（主正则结构性抓不到）

| 收集器 | 对象 | 原因 |
|--------|------|------|
| `scan_showflags` | `ShowFlag.*` | 名字运行时拼接（`FString("ShowFlag.") + InName`），非字面量 |
| `scan_stats` | `stat <GroupId>` | `DECLARE_STATS_GROUP` 静态声明，不是 `IConsoleObject` |
| `scan_engine_stats` | `stat <Name>` | `UEngine::EngineStats` 注册的独立 stat（`STAT_FPS` 等） |

### 3.2 为什么分支 3 当初要加 `\(`

`rev#2` 里该处带有原注释：`# [Engine ZXB] #6，带 ( 排除类成员声明`

**设计意图**是排除"C 类成员声明"——即头文件里 `static FAutoConsoleCommand FooCommand;`
这类只有声明、没有注册语句的行。

**代价**是没有考虑到类外定义形式 `Type::Member(`：名字里含 `::`，`\w+` 吃不下，`\(` 也就永远匹配不上。
而 UE 里"静态成员在类外定义"是极常见的写法，于是整整一类注册点被静默丢弃。

### 3.3 为什么其它分支不受影响

分支 1/2 不含 `\(` 约束，`\w+` 只吃到类名（如 `FAndroidPlatformRHIFramePacer`）就结束，
后续 `::CVarUseSwappyForFramePacing(` 交由 `_ANY{0,400}?` 逐字符跳过，
最后落在 `TEXT("a.UseSwappyForFramePacing")` 上。

实测：限定名的 `TAutoConsoleVariable`（15 条）、`FAutoConsoleVariableRef`（43 条）
在库里都能找到，佐证了这条推断。

---

## 四、修复方案

### 4.1 改动（一行正则）

`UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Tools/CollectEngineCVars.py`

```diff
-                r'FAutoConsoleCommand\w*\s+\w+\s*\(|'
+                r'FAutoConsoleCommand\w*\s+[\w:]+|'
```

即去掉强制的 `\(`，把名字字符类放宽到 `[\w:]`，与分支 1/2 的形状对齐。
（`[\w:]+` 允许字母数字下划线加冒号，足以匹配 `UWorldPartitionHLODRuntimeSubsystem::EnableHLODCommand`。）

顺带补了两处说明性注释（缺陷编号 (3)，与该文件既有的 (1)(2) 变更史体例一致）：

```python
#   (3) [Engine ZXB] 2026-09-16 _ENTRY_START 第 3 分支多要求了一个 \(，使"类外定义的静态成员
#       FAutoConsoleCommand"（Type::Member，如 wp.Runtime.HLOD）整类漏抓 26 条；分支 1/2 无此
#       要求故只有 Command 受累。修法：去掉 \(，与分支 1/2 对齐形状。
```

### 4.2 重新生成

脚本内置"增量短路"（ini 不旧于脚本时直接退出），**必须加 `--force`**：

```bash
cd UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Tools
python CollectEngineCVars.py --force
```

输出：

```
scanned files=42757, raw_hits=9561, unique_names=9527
showflags added=230, total_names=9757
stat groups added=247, total_names=10004
engine stats added=21, total_names=10025
wrote .../Config/EngineCVarDatabase.ini
```

条目数 **9996 → 10025（+29）**。

### 4.3 验证结果

| 验证项 | 结果 |
|--------|------|
| 26 条漏抓清单 | 入库 **26 / 缺失 0** |
| `wp.Runtime.HLOD` | 已恢复，help 正确：`Turn on/off loading & rendering of world partition HLODs.` |
| 脚本 `p4 diff` | 6 增 / 1 删（仅预期改动） |
| ini `p4 diff` | **29 增 / 0 删** |
| 回归（旧正则可抓、新正则抓不到） | **0 条** |

新增 29 条 = 26 条目标 + 3 条同源（见 §5.2）。

### 4.4 生效方式

ini **不进二进制**，是运行时读取：

- `ConsoleSuggestionDatabase.cpp:37-44` — 优先取 `FPaths::EngineDir() + "Source/Programs/UnrealMobileDeviceViewer/Config/EngineCVarDatabase.ini"`，
  找不到再回退到 exe 同级目录。
- `UnrealMobileDeviceViewerMain.cpp:150` — 启动时调用 `FConsoleSuggestionDatabase::LoadAndRegister()` **一次**。

**所以：不用重新编译，重启 UnrealMobileDeviceViewer.exe 即可。**

重启后验证：输入 `wp.Runtime.HLOD`，补全项的 help 会带 `[Offline] ` 前缀
（该前缀由 `ConsoleSuggestionDatabase.cpp:30` 的 `HelpSourceTag` 加上，是"来自离线库"的可靠标记）。
日志中亦有：

```
Offline CVar database loaded: registered=N, skipped_existing=M, malformed=0 (source: ...)
```

---

## 五、快速排查 Checklist

### 5.1 某个 CVar / 命令没有补全提示时

1. **确认它是否真的被注册**——全源码 grep 名字，看落在哪一行。
   注意 `FindConsoleVariable(TEXT("..."))` 是**查询**不是注册，不算。
2. **确认它的注册写法**，对照 `CollectEngineCVars.py` 支持的 7 类入口 + 3 类专门收集。
3. **用真源正则验证**，不要手抄：
   ```python
   import importlib.util
   spec = importlib.util.spec_from_file_location('cec', r'...\CollectEngineCVars.py')
   cec = importlib.util.module_from_spec(spec); spec.loader.exec_module(cec)
   print(list(cec.CVAR_ENTRY_RE.finditer(open(src, encoding='utf-8', errors='ignore').read())))
   ```
4. **看 ini 里的近邻条目**——同名前缀的兄弟项在、它自己不在，通常说明是"名字形状"类问题而非"没扫到文件"。

### 5.2 已知的、**尚未修复**的同类缺陷

**help 文本被填成相邻命令名**（3 条已知）：

```
AssetRegistry.GetByName      → help 显示 AssetRegistry.GetByPath
CollectionManager.Create     → help 显示 CollectionManager.Destroy
PackageName.DumpMountPoints  → help 显示 PackageName.RegisterMountPoint
```

原因：这些站点的第二个参数不是 `TEXT("...")` 字面量，而是
`*LOCTEXT("CommandText_X", "...").ToString()`。help 捕获组只能向前找下一个 `TEXT("...")`，
于是吃到了**下一条命令的名字**。这与该脚本 2026-09-14 修的缺陷 (1) 同族，但触发源不同
（`LOCTEXT` 而非多段字面量拼接），属独立问题。名称本身抓取正确，对补全仍有价值，help 需要另修。

### 5.3 产物行尾陷阱

`CollectEngineCVars.py` 的 `write_ini()` 用 Python 文本模式写文件：

```python
with open(out_path, 'w', encoding='utf-8') as f:
```

Windows 下 `\n` 会被翻译成 `\r\n`，产出 **CRLF**；而本仓库 depot 存的是 **LF**
（预提交钩子 `.workflow/recopy/precheckscript/precheck.py:11 convert_to_utf8_lf()` 会强制转 LF）。

后果：`p4 diff` 整文件 10030 行变更，**完全无法评审**（真实的 29 行改动被淹没）。

处理：手工把产物转 LF 后，diff 收敛为 29 增 / 0 删。

```bash
tr -d '\r' < EngineCVarDatabase.ini > tmp && mv tmp EngineCVarDatabase.ini
```

彻底解法是在 `write_ini()` 的 `open()` 加 `newline='\n'`（未被采纳，属独立改动）。

> 注意：不要用 `grep -c $'\r'` 判断行尾——在 Git Bash 里该写法不稳定，同一文件先后报出
> 0 和 10030 两个矛盾结果。用 Python 字节统计定论：
> ```python
> d = open(path,'rb').read(); print('CRLF:', d.count(b'\r\n'), 'LF:', d.count(b'\n')-d.count(b'\r\n'))
> ```

---

## 六、参考资料

本次排查未依赖网络资料，全部结论来自本仓源码与实测。关键文件与行号：

| 文件 | 关键位置 | 说明 |
|------|----------|------|
| `UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Tools/CollectEngineCVars.py` | `_ENTRY_START` / `_ANY` / `CVAR_ENTRY_RE` | 缺陷所在 |
| `UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Config/EngineCVarDatabase.ini` | — | 生成物（10,025 条） |
| `UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Private/ConsoleSuggestionDatabase.cpp` | 33-52（路径解析）、54-138（加载注册）、30（`[Offline]` 标记） | 运行时消费端 |
| `UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/Private/UnrealMobileDeviceViewerMain.cpp` | 150 | 启动时一次性加载 |
| `UE5EA/Engine/Source/Programs/UnrealMobileDeviceViewer/UnrealMobileDeviceViewer.Target.cs` | 42-59 | PreBuildStep 自动重生成 ini（受增量短路保护） |
| `UE5EA/Engine/Source/Runtime/Engine/Private/WorldPartition/HLOD/HLODRuntimeSubsystem.cpp` | 160-189 | 本次漏抓的典型案例 |
| `.workflow/recopy/precheckscript/precheck.py` | 11 `convert_to_utf8_lf()` | 提交期强制 LF |

验证脚本（可复跑，位于工作区规划目录）：

| 脚本 | 用途 |
|------|------|
| `.planning/2026-09-16-umdv-cvar-db-refresh/verify_hlod_miss.py` | 根因实证（真源正则单点匹配） |
| `.planning/2026-09-16-umdv-cvar-db-refresh/blast_radius2.py` | 影响面统计（521/495/26） |
| `.planning/2026-09-16-umdv-cvar-db-refresh/verify_fix.py` | 修复原型验证（+29 / 回归 0） |
| `.planning/2026-09-16-umdv-cvar-db-refresh/coverage_check.py` | 覆盖率抽查（防漏抓） |

**Perforce**：改动挂 changelist **1149100**（`CollectEngineCVars.py#3`、`EngineCVarDatabase.ini#2`）。
提交描述需匹配 `--story/--bug=\d+ --user=.+.+ https://www.tapd.cn/\d+/s/\d+`，否则被提交钩子拒绝。
