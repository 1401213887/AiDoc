# UE-S1Game-Android-cook-卡死与62%崩溃-根因定位与修复

> S1Game 的 Android（Vulkan/ASTC）cook 先是**启动约 30 秒卡死**，修掉后又在 **62% 处 `0xC0000005`** 崩溃。
> 两个问题根因**完全不同**、互不相关，目前已分别定位并解决：cook 完整跑通
> （`Cooked packages 179593 / Remain 0`，零崩溃，78 分钟）。
> 剩余失败点是 **OBB 超过 4 GiB 硬上限**，属项目级出包配置问题，**devops 流水线同样红在此处**。

---

## 一、问题定位流程

### 1.1 问题 A：cook 启动约 30 秒卡死

| 步骤 | 确认内容 | 证据 |
|---|---|---|
| 1 | 是**卡死**不是崩溃 | 进程存活、CPU 在 50 秒内只涨 0.16 秒（约 0.3%）、日志冻结、`Saved/Cooked` 6 分钟无任何文件产出 |
| 2 | 卡在哪个调用 | 日志末段：`LogSourceControl: Attempting 'p4 print -q //GR/VirtualAsset_Store/VAPayloads/payload_metainfo.txt'` |
| 3 | 该调用能否复现 | 手工执行同一条命令：**45s / 60s 超时、零输出、不返回** |
| 4 | 为什么是死等而非报错 | UE 的 P4 集成走 P4API 直连、**没有超时机制**（见 §三.2） |
| 5 | 调用归属 | 虚拟资产（VA）后端图的持久层 = `P4SourceControlBackend` |

### 1.2 问题 B：62% 处 `0xC0000005`

| 步骤 | 确认内容 | 证据 |
|---|---|---|
| 1 | 崩溃位置固定 | `[Cook][PopulateGeneratorPackage]` 处理 `RUSH_01_Main`，对象 `BP_Container_Tree_C_UAID_580205D70A8FB6EC02_1851719811` |
| 2 | 崩溃前末几行恒定 | `LogNiagara: Construct` → `SetReplicates called on non-initialized actor Default__BP_Vehicle01_Broken_C` → `UAbilitySystemComponent::OnObjectCreate:GeSkillComponent` ×3 → 日志戛然而止 |
| 3 | 排除参数/内容/工作区差异 | devops 流水线用**逐字等价**的命令行能把 cook 跑完 |
| 4 | 排除对象池、内存、句柄耗尽 | 见 §四.3 反证表 |
| 5 | 现状 | 补齐完整 cook 缓存后**不再复现** |

---

## 二、根因分析

### 2.1 卡死的根因：VA 的 P4 调用永不返回

S1Game 使用**虚拟资产**（`Content/Arts` 下是虚拟化指针文件，实体在 `//GR/VirtualAsset_Store/VAPayloads`）。
cook 启动时 VA 系统会初始化后端图，其中 `P4SourceControlBackend` 在 `OnConnect()`
阶段要执行一次

```
p4 print -q //GR/VirtualAsset_Store/VAPayloads/payload_metainfo.txt
```

来读取 payload 清单元信息。**这条命令在本机不返回**，而 UE 的 P4 调用没有超时兜底，
于是 cook 的主线程无限等待 → 整个 cook 永久卡死，连内容 cook 阶段都进不去。

> 注意：这不是"P4 挂了"那么简单——`p4 info` / `p4 login -s` 都正常，票证有效期还有 359 小时；
> 只有涉及 `//GR/VirtualAsset_Store` 这个 depot 的 `print` 会挂住。

### 2.2 62% 崩溃的根因：尚未坐实，但已不再复现

崩溃对象、崩溃前的日志序列在多次运行中完全一致，但根因未定位到代码级。
**当前状态是"不复现"而非"已修复"**，因此本文件不对其做根因断言，只记录已证伪的方向（§四.3）。

已确认的一点：它与 §2.1 的 VA 卡死是**两个独立问题**——修好 VA 之后 62% 崩溃仍然复现过一次，
直到缓存被一次完整 cook 补齐后才消失。

---

## 三、详细技术原理

### 3.1 VA 后端图与 `%DISKDIR%` 的解析

`S1Game/Config/DefaultEngine.ini` 里定义了 3 套 VA 后端图：

| 图名 | PersistentStorage | CacheStorage | 需要 P4 |
|---|---|---|---|
| `VABackendGraph_S1_Editor_Default` | `P4SourceControlBackend` | `LocalFileSystemCommonCache` + `LocalFileSystemDDCCache` | ✅ |
| `VABackendGraph_S1_Editor_NoDDC` | `P4SourceControlBackend` | `LocalFileSystemCommonCache` | ✅ |
| `VABackendGraph_S1_Editor_Local` | `LocalFileSystemCommonCache` | `LocalFileSystemCommonCache` | ❌ |

当前生效的是 `NoDDC`：

```ini
[Core.VirtualizationModule]
BackendGraph=VABackendGraph_S1_Editor_NoDDC
```

`%DISKDIR%` 的解析在 `Engine/Source/Developer/Virtualization/Private/VirtualizationFileBackend.cpp:38-48`：

```cpp
// 替换%DiskPath%,为当前盘符,为了不同项目可以使用同样的缓存
if(RootDirectory.Contains(TEXT("%DISKDIR%")))
{
    const FString WindowsFullPath = FPaths::ConvertRelativePathToFull(FPaths::ProjectDir());
    const FString FirstChar = WindowsFullPath.Left(1);
    RootDirectory = RootDirectory.Replace(TEXT("%DISKDIR%"), *FString::Printf(TEXT("%s:"), *FirstChar));
}
```

即 **`%DISKDIR%` = 工程目录所在盘符**。本工程在 `D:`，
所以 `%DISKDIR%/VirtualizedPayloads/VAPayloads/` → `D:/VirtualizedPayloads/VAPayloads/`，
本机该目录**已填充 46647 个文件 / 71 GB**。

> ⚠️ `%GAMEDIR%` 实测**会**被解析为工程目录（`D:/GR_Devtest_V1/S1Game/`），
> 所以内置的 `VABackendGraph_S1_Editor_Local` 指向 `S1Game/VirtualizedPayloads/VAPayloads/`。
> 但该目录**是 P4 纳管的 payload 缓存**，本工作区**未同步**（本地不存在）→ 该图实际不可用。

### 3.2 UE 的 P4 集成没有超时

`Engine/Plugins/Developer/PerforceSourceControl/Source/PerforceSourceControl/Private/PerforceConnection.cpp:1210`：

```cpp
UE_LOG( LogSourceControl, Log, TEXT("Attempting 'p4 %s'"), *FullCommand );
...
FP4KeepAlive KeepAlive(InIsCancelled);
P4Client.SetBreak(&KeepAlive);
...
P4Client.Run(FROM_TCHAR(*InCommand, bIsUnicode), &User);
```

走的是 Perforce 的 C++ API（`P4Client.Run`），只有一个 `FP4KeepAlive` 取消回调，
**没有超时参数**。服务端一旦不返回，调用方就无限阻塞。
日志里只有 `Attempting 'p4 ...'` 而**永远等不到**
`P4 execution time: ... seconds. Command: ...` 那一行——这就是卡在 P4 调用的判据。

### 3.3 为什么 UAT 下崩溃拿不到调用栈

UAT 生成的 cooker 命令行里带 `-CrashForUAT`。
`Engine/Source/Runtime/Core/Private/GenericPlatform/GenericPlatformMisc.cpp:632`：

```cpp
if ((!FPlatformMisc::IsDebuggerPresent() || GAlwaysReportCrash) && !FParse::Param(FCommandLine::Get(), TEXT("CrashForUAT")))
```

该开关会把标准崩溃处理短路掉 → **不落 dump、日志直接断**，现场信息全部丢失。
要拿真实调用栈，必须**手工直起 cooker 并去掉这个开关**（见 §四.2）。

---

## 四、修复方案

### 4.1 解决卡死：把 VA 持久层换成本地缓存（推荐）

**落点**：`S1Game/Saved/Config/WindowsEditor/Engine.ini`

> ⚠️ **重要更正**：`S1Game/Saved/Config/WindowsEditor/Engine.ini` **是 P4 纳管文件**
> （`//GR/DevTest/S1Game/Saved/Config/WindowsEditor/Engine.ini`）。
> 本方案是**临时绕过手段，禁止提交**——一旦提交，全组的 VA 都会失去 P4 后端，
> 而 `AllowSubmitIfVirtualizationFailed=false`，**美术提交虚拟资产的链路会断**。
> 用完请 `p4 revert` 还原；还原命令与原始内容见 §4.4。
>
> 另外实测：**每次 cook 运行结束时该文件都会被删掉**（完整 `BuildCookRun` 与手工直起 cooker 均如此），
> 所以它是"一次一贴"的临时落点——**每次 cook 前都要重新追加**，不能当长期方案。
> 恢复 depot 原版用 `p4 sync -f //GR/DevTest/S1Game/Saved/Config/WindowsEditor/Engine.ini`。

```ini
[VABackendGraph_S1_Editor_NoDDC]
PersistentStorageHierarchy=(Entry=LocalFileSystemCommonCache)
```

原理：只覆盖生效图的「持久层」这一个键，把它从 `P4SourceControlBackend` 换成
`LocalFileSystemCommonCache`（即 §3.1 里那个 71GB 的本地 payload 缓存）。
VA 系统因此**全程不触碰 P4**，卡死消失。

**生效判据**（三条都要对）：

```
LogVirtualization: Display: BackendGraphName : VABackendGraph_S1_Editor_NoDDC
LogVirtualization: Display: The 'PersistentStorageHierarchy' has 1 backend(s)     ← 只剩本地，P4 已摘除
LogVirtualization: [FileSystem - LocalFileSystemCommonCache] Windows RootDirectory After DiskDir 'D:/VirtualizedPayloads/VAPayloads/'
```

外加：日志里 `Attempting 'p4` 的计数为 **0**。

**备选（已实测，多数场景不可用）**：命令行可覆盖图名
（`Engine/Source/Developer/Virtualization/Private/VirtualizationManager.cpp:1329`）：

```
-VABackendGraph=<图名>
```

实测结论：
- 该开关**生效**（日志 `Backend graph overriden from the cmdline: '...'`）。
  注意日志里先打印的 `BackendGraphName:` 是 ini 里的值，**早于**命令行覆盖，不要误判。
- 但**图的"内容"改不了**：`MountBackends(InitParams.ConfigFile)` 直接读原始 ini 文件，
  **不走配置覆盖层**，所以 `-ini:Engine:[...]:Key=Value` 之类的注入**对 VA 后端图无效**（已实测）。
- 内置的 `VABackendGraph_S1_Editor_Local` 虽然是纯本地图，但它指向
  `%GAMEDIR%/VirtualizedPayloads/VAPayloads/`（`%GAMEDIR%` 实测会解析为工程目录），
  而该目录**是 P4 纳管的 payload 缓存**、本工作区**未同步**（本地不存在）→ 用它等于用空缓存，不可行。

### 4.4 应用 / 撤销这条临时绕过

**应用**（在已同步的 `Engine.ini` 末尾追加两行）：

```bat
echo. >> S1Game\Saved\Config\WindowsEditor\Engine.ini
echo [VABackendGraph_S1_Editor_NoDDC] >> S1Game\Saved\Config\WindowsEditor\Engine.ini
echo PersistentStorageHierarchy=(Entry=LocalFileSystemCommonCache) >> S1Game\Saved\Config\WindowsEditor\Engine.ini
```

**撤销**（还原成 depot 版本）：

```bat
p4 revert S1Game\Saved\Config\WindowsEditor\Engine.ini
```

若文件已被删或改乱，用强制同步拿回 depot 原版：

```bat
p4 sync -f //GR/DevTest/S1Game/Saved/Config/WindowsEditor/Engine.ini
```

### 4.2 抓 62% 崩溃真实调用栈的方法

手工直起 cooker，参数与 UAT 生成的**逐字一致**，**只去掉 `-CrashForUAT`**：

```bat
"%ENGINE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" "%PROJECT_DIR%\S1Game.uproject" ^
  -run=Cook ^
  -TargetPlatform=Android_ASTC ^
  -ddc=LocalAndShared ^
  -unversioned ^
  -iterate ^
  -EXBUILDINGCONFIG=NoRush+NoBR+BasicMap+Development+NeoRing+DL ^
  -NoAlwaysCookMaps -NoDefaultMaps -NoGameAlwaysCook ^
  -NEVERCOOKDIR= ^
  -CookProcessCount=1 ^
  -DisablePlugins=UnrealMCP ^
  -fileopenlog ^
  -enabletimecollect ^
  -abslog="D:\GR_Devtest_V1\cook_diag.log" ^
  -stdout ^
  -unattended ^
  -NoLogTimes ^
  -UTF8Output
```

现成脚本：`S1Game/Scripts/CookDiag.bat`。

### 4.3 已证伪的归因（**不要再走这些方向**）

| 假设 | 反证 |
|---|---|
| 对象池（ObjectPool）导致 | 日志明确：`LogUObjectAllocator: SetEnableObjectPool 0` + `LogPoolObjectAllocator: Object Pool is disable when IsRunningCommandlet` |
| 内存 / 句柄耗尽 | 崩溃那次 `OpenFileHandles=81,663 / VirtualMemory=26.7GB`；**跑通那次反而是 277,224 / 66.2GB** |
| `Actor ... needs to be resaved` 是元凶 | 跑通那次该告警**同样大量出现** |
| `-CrashForUAT` 是崩溃元凶 | 带着它跑完整 `BuildCookRun` **也没有崩** |
| 参数不对 | devops 流水线用逐字等价参数能 cook 完 |
| `pakchunk` 打包阶段 | 崩溃发生在 WorldPartition generator 处理阶段，与打包无关 |

---

## 五、验证结果

### 5.1 cook 完整跑通

```
Cooked packages 179591  Packages Remain 0  Total 179591
Loaded Packages: 692208
LogExit: Exiting.
```

| 指标 | 值 |
|---|---|
| 耗时 | 78 分钟（devops 流水线那次 77 分钟） |
| Critical error / Fatal error | 0 / 0 |
| ACCESS_VIOLATION | 0 |
| Assertion failed / Unhandled Exception | 0 / 0 |

两次独立验证：① 手工诊断 cook（`cook_diag.log`，41 MB）② 完整 `BuildCookRun`（走 UAT 出包链路）。

### 5.2 三个 VAT 材质在**真实 cook** 中编译成功

| 材质 | `Saved shaders ... to DDC` | `Failed to compile` |
|---|---|---|
| `M_Boss02_VAT` | 6（High / Default 等档） | **0** |
| `M_RushEnemyBase_VAT` | 5（Low / Medium / High / Epic / Great） | **0** |
| `M_EFX_RBDVat_WZC_001` | 2（High / Default） | **0** |

全日志 `asuint` 相关字样 = **0**（原始问题是 188 条
`no matching function for call to 'asuint'`、182 条材质编译失败）。

### 5.3 与 devops 流水线对齐

两条链路的**失败点已经完全一致**，本机构建不再有"独有"的故障：

```
本机：      AutomationTool exiting with ExitCode=155 (Error_AndroidOBBError)
devops 流水线：AutomationTool exiting with ExitCode=155 (Error_AndroidOBBError)
```

---

## 六、快速排查 Checklist

面对「UE 工程 cook 卡死 / 崩溃」：

1. **先判卡死还是崩溃**——看进程 CPU 是否在涨（`Get-Process` 的 `CPU` 字段隔 30 秒取两次）。
   CPU 不涨 + 日志冻结 = 卡死，方向是**外部依赖等待**（P4 / DDC / 网络盘 / VA）。
2. **找最后一条"发起型"日志**——形如 `Attempting 'p4 ...'` 这种只打印请求、不打印结果的日志，
   后面没有对应的完成行，就是卡点。**手工复现那一条命令**是最快的判定。
3. **cook 日志位置别找错**：
   - UAT 侧：`UE5EA/Engine/Programs/AutomationTool/Saved/Cook-<时间戳>.txt`（**UAT 跑完会清理**）
   - 崩溃时保留的那份：`.../Saved/Logs/Cook-<时间戳>.txt`
   - UAT 汇总：`.../Saved/Logs/Log.txt`
4. **崩溃拿不到调用栈**——检查命令行是否带 `-CrashForUAT`；有就手工直起 cooker 去掉它。
5. **要判定"是修好了还是没测到"**——用**反向对照**：换回修复前的版本/资产必须能复现原错误，
   否则说明根本没跑到那段代码。
6. **VA 卡死**——确认后端图：日志 `PersistentStorageHierarchy has N backend(s)`；
   本工程需为 `1`（只剩本地），且 `Attempting 'p4` 计数为 `0`。

---

## 七、遗留问题：OBB 超过 4 GiB 上限

**现象**（本机与 devops 流水线完全一致）：

```
Failed to build OBB: ...\Saved\StagedBuilds\Android_ASTC.obb
Could not build OBB ... The file may be too big to fit in an OBB
                       (4 GiB limit and no overflows are permitted)
AutomationTool exiting with ExitCode=155 (Error_AndroidOBBError)
```

**量化**：

```
S1Game 暂存 48 GB
  pakchunk0-Android_ASTC.pak        35 GB   ← 主包，超 4 GiB 上限 8 倍
  pakchunk101-Android_ASTC.pak     8.2 GB
  pakchunk102-Android_ASTC.pak     2.4 GB
  pakchunk0optional                1.3 GB
```

**配置现状**（`S1Game/Config/DefaultEngine.ini` 的 `[/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]`）：

```ini
bForceSmallOBBFiles=False
bAllowLargeOBBFiles=True          ; 4 GiB 上限
bAllowPatchOBBFile=True
bAllowOverflowOBBFiles=True       ; 声明开启溢出
+ObbFilters=-pakchunk1*           ; 只排除 chunk1
+ObbFilters=-pakchunk2*           ; 只排除 chunk2
```

**两个待查点**：

1. 异常来自 `Engine/Source/Programs/AutomationTool/Android/AndroidPlatform.Automation.cs:1284`
   的 `"no overflows are permitted"` 分支，该分支对应 `AllowOverflowOBBLimit == 0`，
   即 `bAllowOverflowOBBFiles` **被读成 false**——与 ini 里写的 `True` 不符，需查配置读取层级。
2. 即便溢出生效（`OverflowOBBFileLimit` 默认 2），上限也只有
   `4 GiB(主) + 4 GiB(patch) + 2×4 GiB(溢出) = 16 GiB`，**装不下 35 GB 的 `pakchunk0`**。
   因此真正要动的是**分包策略**（把 `pakchunk0` 拆细，或把大块内容挪进受 `ObbFilters` 排除的 chunk）。

---

## 八、相关参考

### 8.1 代码位置

| 内容 | 文件:行 |
|---|---|
| `%DISKDIR%` 解析 | `UE5EA/Engine/Source/Developer/Virtualization/Private/VirtualizationFileBackend.cpp:38-48` |
| VA 后端图读取 | `UE5EA/Engine/Source/Developer/Virtualization/Private/VirtualizationManager.cpp:1059-1076` |
| `-VABackendGraph=` 命令行覆盖 | `UE5EA/Engine/Source/Developer/Virtualization/Private/VirtualizationManager.cpp:1329` |
| P4 调用无超时 | `UE5EA/Engine/Plugins/Developer/PerforceSourceControl/Source/PerforceSourceControl/Private/PerforceConnection.cpp:1210` |
| `-CrashForUAT` 短路崩溃处理 | `UE5EA/Engine/Source/Runtime/Core/Private/GenericPlatform/GenericPlatformMisc.cpp:632` |
| OBB 上限与溢出判定 | `UE5EA/Engine/Source/Programs/AutomationTool/Android/AndroidPlatform.Automation.cs:980-1015, 1211-1305` |

### 8.2 关键配置

- `S1Game/Config/DefaultEngine.ini`
  - `[Core.VirtualizationModule]` / `[VABackendGraph_S1_Editor_*]`（约 1320-1376 行）
  - `[/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]`（约 738 行起，OBB 相关在 757-761 行）
- `S1Game/Saved/Config/WindowsEditor/Engine.ini`（**P4 纳管**；§4.1 的临时绕过落点，**禁止提交**，见 §4.4）

### 8.3 脚本

- `S1Game/Scripts/BuildS1Android.bat` —— 完整 `BuildCookRun`（参数与 devops 流水线逐字一致）
- `S1Game/Scripts/CookDiag.bat` —— 手工直起 cooker、不带 `-CrashForUAT`，用于抓调用栈

### 8.4 devops 流水线

- 流水线：`https://devops.woa.com/console/pipeline/grgame/p-bed986edaec14b0cb5f6b7b487a3d916`
- 该流水线的真实 `BuildCookRun` 命令行可直接从其构建日志里取：
  `S1Build.py` 会执行 `print("构建命令查看\n", Command)`，日志中搜 `构建命令查看` 即可

### 8.5 相关文档

- `E:\AiDoc\UE-Android-Vulkan-VAT材质-asuint编译失败-修复与验证.md`
  —— VAT `asuint` 问题的根因与修复（本文件 §5.2 的验证对象）
