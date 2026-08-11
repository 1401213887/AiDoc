# Snapdragon-Profiler-启动崩溃-msvcp140-Runtime版本不兼容排查修复指南

> Snapdragon Profiler（2026.8.0）在 Windows 11（26100 / 24H2）上启动数秒即崩溃，托管层报 `System.AccessViolationException`，栈尾指向 `SDPCorePINVOKE.Logger_Write__SWIG_0`。根因是 `C:\Windows\System32\msvcp140.dll`（VC++ 运行库）版本过旧（14.24），与新版 SDP 的 MSVC 标准库调用不兼容，写日志时 `std::string` 操作空指针解引用。将 14.44 新版 runtime 三件套放入 SDP 安装目录（应用本地 DLL 优先加载）即修复。

---

## 一、问题定位流程

**现象**：双击/命令行启动 SnapdragonProfiler.exe，数秒内进程消失。

**关键线索确认链**（每一步都排除了一个方向）：

| 步骤 | 确认内容 | 结论 |
|---|---|---|
| 1 | 安装目录文件齐全（exe、SDPCore.dll、GTK# 程序集等均在），版本 2026.8.0，8/5 曾有正常 startup 事件 | 不是缺文件 |
| 2 | 直接命令行运行，抓到托管未处理异常 | 拿到崩溃栈（见下） |
| 3 | 所有 exe/DLL 均为 x64，架构一致 | 排除 x86/x64 混载 |
| 4 | .NET Framework 4.8（满足 config 要求的 4.7.2）、VC++ Redist v14.44 均已装 | 排除运行库缺失 |
| 5 | `LoadLibraryEx(flags=LOAD_WITH_ALTERED_SEARCH_PATH)` 逐 DLL 加载全部成功 | 排除依赖 DLL 缺失 |
| 6 | 解析 WER 崩溃转储（`%LOCALAPPDATA%\CrashDumps\SnapdragonProfiler.exe.*.dmp`），9 个 dump 全部崩在同一地址 | 定位崩溃模块与偏移 |

**崩溃栈**（Application 日志，.NET Runtime 1026）：

```
System.AccessViolationException: Attempted to read or write protected memory.
   at SDPCorePINVOKE.Logger_Write__SWIG_0(HandleRef, Int32, String, String)
   at Sdp.Logging.Logger.LogInformation(String message)
   at Sdp.AnalyticsManager.CheckEventViewerForCrash()
   at Sdp.AnalyticsManager.CheckForPreviousCrash(String[])
   at System.Threading.Tasks.Task.Execute()          // 后台 ThreadPool 任务
```

**WER dump 关键数据**：
- 异常码 `0xC0000005`（Access Violation），访问地址 `0x0`（**空指针解引用**，param0/param1 均为 0）
- 崩溃指令地址恒落在 **`C:\Windows\System32\msvcp140.dll + 0x18C34`**（9 次崩溃偏移完全一致，确定性复现，非随机损坏）

## 二、根因分析

**直接根因**：SnapdragonProfiler 启动后台任务 `CheckEventViewerForCrash`（检查事件日志中此前的崩溃记录）→ `Logger.LogInformation()` → SWIG P/Invoke 调用原生 `SDPCore.dll` 的 `Logger_Write` → 原生实现内部对日志消息做 `std::string` 操作，该代码由新版本 MSVC 编译。进程从 System32 加载到的是**旧版** `msvcp140.dll`（**14.00.24215.1，即 VC++ 14.24**），旧版标准库在与新编译产物交互时发生空指针访问，崩溃点落在 msvcp140.dll 的 `std::string` 实现内。

**为什么 System32 是旧版**：
- 注册表 `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64` 显示 v14.44 redist 已安装，但 `System32\msvcp140.dll` 实际文件版本仍是 14.24（文件日期 7/6，疑被系统更新或旧安装覆盖），**注册表状态与磁盘文件不一致**——排查时不能只信注册表。
- SDP 2026.8.0 是 2026 年发布的最新版，按新版 MSVC 工具链编译，对运行库版本的要求超出系统当前提供。

**崩溃触发路径是"二次崩溃"放大器**：程序每次启动先查事件日志有无历史崩溃，发现后写日志时崩——所以一旦首次崩溃，后续每次启动都在同一条日志写入路径上复现，形成固定崩溃。

## 三、技术原理

- **SWIG 绑定**：SDP 用 SWIG 生成 C# → C++ 的 P/Invoke 层（`SDPCorePINVOKE` 类位于 `SDPCoreWrapper.dll`，DllImport 指向原生 `SDPCore.dll`）。托管层参数（String、HandleRef）经 CLR marshaling 转成原生 `char*`/`Logger*` 后调用。崩溃发生在原生函数体内使用 MSVC 标准库时，托管栈只能看到 SWIG 入口帧。
- **Windows DLL 搜索顺序**：进程加载 DLL 时，**应用所在目录优先于 `System32`**（无需 SafeDllSearchMode 特例）。因此把新版 `msvcp140.dll` 放进 SDP 安装目录即可让进程加载到新版运行库——这是"应用本地 DLL"（app-local deployment）机制，比覆盖 System32 系统组件更安全可逆。
- **VC++ 运行库组件**：MSVC 标准库拆为 `msvcp140.dll`（iostream/string/exception）与 `vcruntime140.dll` + `vcruntime140_1.dll`（CRT 启动与分配），三件必须一起替换为同一版本族，避免混搭。

## 四、修复方案

**已验证生效的操作**（应用本地 workaround）：

```bash
# 1. 从 VS2022 的 MSVC 工具链目录取得 14.44 三件套
SRC="C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64"

# 2. 复制到 SDP 安装目录（Windows 优先加载应用目录的 DLL）
cp "$SRC/msvcp140.dll" "$SRC/vcruntime140.dll" "$SRC/vcruntime140_1.dll" \
   "C:\Program Files\Qualcomm\Snapdragon Profiler\"

# 3. 验证版本（应为 14.44.352xx）
powershell -NoProfile -Command "(Get-Item 'C:\Program Files\Qualcomm\Snapdragon Profiler\msvcp140.dll').VersionInfo.FileVersion"
```

**验证结果**：注入后启动进程正常驻留（内存约 196MB），仅打印 SDP 自带的 GTK 警告（`Gtk-WARNING: Cannot connect attribute 'active'...`，无害）；崩溃 dump 零新增；事件日志出现正常 `startup`（Id 0）而非 `.NET Runtime` 崩溃。

**根治选项**（按推荐排序）：
1. 从微软官方下载安装最新 **Visual C++ 2015-2022 Redistributable (x64)**，刷新 System32 运行库；
2. 等 Qualcomm 发布修复版本。

## 五、快速排查 Checklist

1. **拿崩溃栈**：直接命令行运行 `SnapdragonProfiler.exe` 捕获 .NET 未处理异常；或在 Application 事件日志找 `.NET Runtime` 1026 / `Application Error` 1000。
2. **解析 WER dump 定位崩溃模块**：读 `%LOCALAPPDATA%\CrashDumps\SnapdragonProfiler.exe.*.dmp`，用 python 解析 MINIDUMP（ExceptionStream 拿异常码+地址；ModuleList 拿模块基址映射；`ModuleNameRva` 偏移为 +20，且是 UTF-16 MINIDUMP_STRING）。
3. **确认架构一致**：检查 exe 与关键 DLL 的 PE machine 字段（`0x8664`=x64）。
4. **测试依赖可加载**：`ctypes.WinDLL('kernel32').LoadLibraryExW(path, None, 0x8)`（`LOAD_WITH_ALTERED_SEARCH_PATH`）逐 DLL 加载；不用 flags=0，否则会因搜索路径不含应用目录而误报。
5. **核对运行库真实文件版本**：`(Get-Item C:\Windows\System32\msvcp140.dll).VersionInfo.FileVersion`——**不要只信注册表的 redist 版本**，注册表与磁盘文件可能不一致。
6. **应用本地 runtime 验证法**：把新版 `msvcp140.dll`/`vcruntime140.dll`/`vcruntime140_1.dll` 复制进应用目录再运行，若崩溃消失即坐实 runtime 版本问题。

## 六、相关参考

- 微软 Visual C++ 2015-2022 Redistributable 下载：https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist
- Qualcomm Snapdragon Profiler 官方页：https://www.qualcomm.com/developer/software/snapdragon-profiler
- MSVC 运行库部署文档（app-local deployment）：https://learn.microsoft.com/cpp/windows/deployment-in-visual-cpp
- Windows WER 崩溃转储（Minidump）格式：https://learn.microsoft.com/windows/win32/debug/minidump-files
