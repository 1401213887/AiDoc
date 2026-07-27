# UE5-Editor-PreviewPlatform脚本化切换-UnrealEd绑定与WorldPartition崩溃修复

> 目标:让编辑器"Preview Rendering Level(预览平台)"切换可被 Python / Editor Utility Blueprint / Unreal MCP 脚本化调用。核心难点:`UEditorEngine::SetPreviewPlatform` 不是 UFUNCTION、`FPreviewPlatformInfo` 不是 USTRUCT,脚本层够不着;实现后又踩到 WorldPartition 嵌套 loading context 断言崩溃。本文记录在 **UE5.5 源码 fork(UE5EA / UnrealEd 模块)** 里加薄反射绑定的完整方案、关键引擎 API(带 file:line)、崩溃根因与延迟执行修复,以及构建/验证要点。

---

## 背景与动机

在做 **移动端 Forward vs Deferred 对齐**(LuxGI / CartoonShadow / PreExposure)时,需要在编辑器里反复在 **桌面 Deferred(SM5/SM6)** 与 **移动预览(Android Vulkan / GLES3.1 / 桌面 ES3.1 预览)** 之间切换验证渲染差异。这个切换在 UI 上是工具栏 `Settings → Preview Rendering Level` 下拉,底层调用:

```cpp
UEditorEngine::SetPreviewPlatform(const FPreviewPlatformInfo& NewPreviewPlatform, bool bSaveSettings);
```

它**没有暴露给脚本层**,每次只能手点 UI。目标是把它变成一条命令。

---

## 一、可行性判定(先探路,再动手)

结论:**开箱即用不行,只能加 C++ 绑定**。实测确认:

| 途径 | 是否可用 | 依据 |
|---|---|---|
| 专门的 Unreal MCP 工具 | ❌ | 12 个 tool scope 全扫过,无 `set_preview_platform` 类工具 |
| Python 反射(`unreal.*`) | ❌ | `unreal.PreviewPlatformInfo` 不存在;`UnrealEditorSubsystem`/`LevelEditorSubsystem` 无 preview/feature-level 方法;无全局 `set_preview_platform*` |
| 控制台命令 | ❌ | `SetPreviewPlatform` 是纯 C++ 调用,无对应 console command |
| **UFUNCTION 薄绑定** | ✅ | 本方案 |

根因:`SetPreviewPlatform` 非 UFUNCTION、`FPreviewPlatformInfo` 非 USTRUCT,都不进反射系统;且该切换联动 shader platform / feature level / material quality / 重建渲染状态,没有单个 CVar 可平替。

万能逃生舱是 MCP 的 `execute_python` / `execute_console_command`(能拿到完整 `unreal` 模块),但要脚本化 `SetPreviewPlatform` 仍需先有反射入口 → 加绑定。

---

## 二、方案设计

**落点:UE5EA 引擎的 UnrealEd 模块**内新增一个 `UBlueprintFunctionLibrary`。

- `SetPreviewPlatform` / `FPreviewPlatformInfo` 本就声明在 UnrealEd 的 `EditorEngine.h`,**同模块直接可调**,不碰 `UEditorEngine` 核心类。
- 内部按引擎自己的构造逻辑拼 `FPreviewPlatformInfo`,对外只暴露简单参数(`FName` 平台名),规避 struct 无反射。
- UnrealEd 已依赖 `Core / CoreUObject / Engine / RenderCore / RHI`(`UnrealEd.Build.cs:71-153`)→ **无需改任何 Build.cs**。
- 规范范例参照同模块 `Public\Subsystems\EditorSubsystemBlueprintLibrary.h`:`UCLASS(MinimalAPI)` + 每函数 `static UNREALED_API`。

> 备选落点(未采用):放在游戏侧 `S1Editor` 模块(它也已依赖 UnrealEd),只需重编游戏 editor 模块、不动引擎。本次按需求选择了引擎 fork。

### 对外 4 个函数
| 函数 | 作用 |
|---|---|
| `SetPreviewPlatformByName(FName, bSaveSettings)` | 按 `PreviewShaderPlatformName` 切换,返回是否命中有效平台 |
| `ResetPreviewPlatformToDefault(bSaveSettings)` | 退回默认桌面渲染层(= UI 的 Disable Preview) |
| `GetAvailablePreviewPlatformNames() → TArray<FName>` | 枚举可选预览平台 |
| `GetCurrentPreviewPlatformName() → FName` | 查询当前;默认层返回 None |

---

## 三、关键引擎 API(全部经源码核对,带 file:line)

路径基准:`D:\GR_DevTest\UE5EA\Engine\Source\`

| 符号 | 位置 | 说明 |
|---|---|---|
| `UEditorEngine::SetPreviewPlatform(const FPreviewPlatformInfo&, bool)` | `Editor/UnrealEd/Classes/Editor/EditorEngine.h:3137` | `UNREALED_API`,非 UFUNCTION |
| `FPreviewPlatformInfo`(8 参构造) | `EditorEngine.h:221-302`(构造 `:235`) | 非 USTRUCT。构造签名见下 |
| `UEditorEngine::PreviewPlatform`(公有成员) | `EditorEngine.h:604` | 存当前预览平台;LevelEditor 模块跨模块读它(`LevelEditorActions.cpp:781/790/845`)→ 证明 public |
| 正典构造 + disable 分支 | `Editor/LevelEditor/Private/LevelEditor.cpp:1907-1937`(disable `:1924`,normal `:1927-1928`) | 菜单构造逻辑,本绑定严格照抄 |
| `FDataDrivenPlatformInfoRegistry::GetAllPreviewPlatformMenuItems()` | `Runtime/Core/Public/Misc/DataDrivenPlatformInfoRegistry.h:357` | 返回 `TArray<FPreviewPlatformMenuItem>` |
| `FPreviewPlatformMenuItem`(字段) | 同上 `:98-113` | 含 `PlatformName / ShaderFormat / PreviewShaderPlatformName / DeviceProfileName / OptionalFriendlyNameOverride` 等 |
| `FDataDrivenShaderPlatformInfo::GetShaderPlatformFromName` | `Runtime/RHI/Public/DataDrivenShaderPlatformInfo.h:152` | 名字→`EShaderPlatform`;未编入返回 `SP_NumPlatforms` |
| `...::GetPreviewShaderPlatformParent` | 同上 `:858` | 判断是否"默认平台" |
| `GetMaxSupportedFeatureLevel(FStaticShaderPlatform)` | 同上 `:986` | 派生 feature level |
| `GMaxRHIShaderPlatform` | `Runtime/RHI/Public/RHIShaderPlatform.h:86` | |
| `GMaxRHIFeatureLevel` | `Runtime/RHI/Public/RHIFeatureLevel.h:109` | |
| 规范模板 | `UnrealEd/Public/Subsystems/EditorSubsystemBlueprintLibrary.h` | `UCLASS(MinimalAPI)` + `static UNREALED_API` |

`FPreviewPlatformInfo` 构造签名(`EditorEngine.h:235`):
```cpp
FPreviewPlatformInfo(
    ERHIFeatureLevel::Type InFeatureLevel,
    EShaderPlatform InShaderPlatform = SP_NumPlatforms,
    FName InPreviewPlatformName = NAME_None,
    FName InPreviewShaderFormatName = NAME_None,
    FName InDeviceProfileName = NAME_None,
    bool  InbPreviewFeatureLevelActive = false,
    FName InShaderPlatformName = NAME_None,
    FText InPreviewShaderPlatformFriendlyName = FText());
```

**键的选择**:用 `PreviewShaderPlatformName`(如 `AndroidVulkan_Preview`)作为切换 key —— 引擎自身查找也用它(`LevelEditor.cpp:1914`),比 `PlatformName` 更唯一稳定。

---

## 四、实现代码(最终加固版)

### 头文件 `UnrealEd\Public\PreviewPlatform\PreviewPlatformScriptLibrary.h`
```cpp
#pragma once
#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "PreviewPlatformScriptLibrary.generated.h"

UCLASS(MinimalAPI)
class UPreviewPlatformScriptLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintCallable, Category = "Editor|Preview Platform")
    static UNREALED_API bool SetPreviewPlatformByName(FName PreviewPlatformName, bool bSaveSettings = false);

    UFUNCTION(BlueprintCallable, Category = "Editor|Preview Platform")
    static UNREALED_API void ResetPreviewPlatformToDefault(bool bSaveSettings = false);

    UFUNCTION(BlueprintPure, Category = "Editor|Preview Platform")
    static UNREALED_API TArray<FName> GetAvailablePreviewPlatformNames();

    UFUNCTION(BlueprintPure, Category = "Editor|Preview Platform")
    static UNREALED_API FName GetCurrentPreviewPlatformName();
};
```

### 源文件 `UnrealEd\Private\PreviewPlatform\PreviewPlatformScriptLibrary.cpp`
```cpp
#include "PreviewPlatform/PreviewPlatformScriptLibrary.h"
#include "Editor.h"                               // GEditor
#include "Editor/EditorEngine.h"                  // UEditorEngine, FPreviewPlatformInfo, 公有成员 PreviewPlatform
#include "Misc/DataDrivenPlatformInfoRegistry.h"  // GetAllPreviewPlatformMenuItems, FPreviewPlatformMenuItem
#include "DataDrivenShaderPlatformInfo.h"         // GetShaderPlatformFromName / GetPreviewShaderPlatformParent / GetMaxSupportedFeatureLevel
#include "RHIShaderPlatform.h"                     // EShaderPlatform, SP_NumPlatforms, GMaxRHIShaderPlatform
#include "RHIFeatureLevel.h"                       // ERHIFeatureLevel, GMaxRHIFeatureLevel
#include "Containers/Ticker.h"                     // FTSTicker (deferred application)

#include UE_INLINE_GENERATED_CPP_BY_NAME(PreviewPlatformScriptLibrary)

namespace
{
    // SetPreviewPlatform 会广播 PreviewFeatureLevelChanged/PreviewPlatformChanged,驱动 WorldPartition
    // RefreshLoadedState 构造 FWorldPartitionLoadingContext::IContext,其 ctor 断言
    // check(ActiveContext == &DefaultContext)(WorldPartitionHandle.cpp:80)。若切换在流式加载途中被同步
    // 调用(嵌套进已有 loading context),断言致命触发。core ticker 由 FEngineLoop::Tick 在任何 WP 加载
    // 操作之外泵动,该点 ActiveContext 必为默认 → 延迟到那里执行即安全;附带让长时 shader 重编不再阻塞调用帧。
    void ApplyPreviewPlatformDeferred(const FPreviewPlatformInfo& Info, bool bSaveSettings)
    {
        FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateLambda(
            [Info, bSaveSettings](float) -> bool
            {
                if (GEditor) { GEditor->SetPreviewPlatform(Info, bSaveSettings); }
                return false; // one-shot
            }));
    }
}

bool UPreviewPlatformScriptLibrary::SetPreviewPlatformByName(FName PreviewPlatformName, bool bSaveSettings)
{
    if (!GEditor || PreviewPlatformName.IsNone()) { return false; }

    for (const FPreviewPlatformMenuItem& Item : FDataDrivenPlatformInfoRegistry::GetAllPreviewPlatformMenuItems())
    {
        if (Item.PreviewShaderPlatformName != PreviewPlatformName) { continue; }

        const EShaderPlatform ShaderPlatform =
            FDataDrivenShaderPlatformInfo::GetShaderPlatformFromName(Item.PreviewShaderPlatformName); // :1914
        if (ShaderPlatform >= SP_NumPlatforms) { return false; } // 未编入该 editor build(:1916)

        const bool bIsDefault =
            FDataDrivenShaderPlatformInfo::GetPreviewShaderPlatformParent(ShaderPlatform) == GMaxRHIShaderPlatform; // :1918

        const FPreviewPlatformInfo Info = bIsDefault
            ? FPreviewPlatformInfo(GMaxRHIFeatureLevel, GMaxRHIShaderPlatform, NAME_None, NAME_None, NAME_None, true, NAME_None) // :1924
            : FPreviewPlatformInfo(GetMaxSupportedFeatureLevel(ShaderPlatform), ShaderPlatform, Item.PlatformName,
                                   Item.ShaderFormat, Item.DeviceProfileName, true, Item.PreviewShaderPlatformName,
                                   Item.OptionalFriendlyNameOverride); // :1927-1928

        ApplyPreviewPlatformDeferred(Info, bSaveSettings);
        return true;
    }
    return false;
}

void UPreviewPlatformScriptLibrary::ResetPreviewPlatformToDefault(bool bSaveSettings)
{
    if (!GEditor) { return; }
    const FPreviewPlatformInfo DefaultInfo(GMaxRHIFeatureLevel, GMaxRHIShaderPlatform, NAME_None, NAME_None, NAME_None, true, NAME_None);
    ApplyPreviewPlatformDeferred(DefaultInfo, bSaveSettings);
}

TArray<FName> UPreviewPlatformScriptLibrary::GetAvailablePreviewPlatformNames()
{
    TArray<FName> Names;
    for (const FPreviewPlatformMenuItem& Item : FDataDrivenPlatformInfoRegistry::GetAllPreviewPlatformMenuItems())
    {
        if (!Item.PreviewShaderPlatformName.IsNone()) { Names.AddUnique(Item.PreviewShaderPlatformName); }
    }
    return Names;
}

FName UPreviewPlatformScriptLibrary::GetCurrentPreviewPlatformName()
{
    if (!GEditor) { return NAME_None; }
    const FPreviewPlatformInfo& Info = GEditor->PreviewPlatform; // 公有成员 EditorEngine.h:604
    return (Info.bPreviewFeatureLevelActive && !Info.PreviewShaderPlatformName.IsNone())
        ? Info.PreviewShaderPlatformName : NAME_None;
}
```

---

## 五、WorldPartition 崩溃:根因与修复(核心章节)

### 症状
在**刚启动、WorldPartition 仍在流式加载**时立即调用切换,编辑器断言崩溃:
```
Assertion failed: ActiveContext == &DefaultContext
[File: .../WorldPartition/WorldPartitionHandle.cpp] [Line: 80]
```

### 崩溃链路(crash 回栈,自顶向下有效段)
```
UPreviewPlatformScriptLibrary::SetPreviewPlatformByName()
  → UEditorEngine::SetPreviewPlatform()                    [EditorEngine.cpp ~6398/6421 广播]
    → TMulticastDelegate::Broadcast()  (PreviewFeatureLevelChanged / PreviewPlatformChanged)
      → IWorldPartitionActorLoaderInterface::ILoaderAdapter::RefreshLoadedState()  [WorldPartitionActorLoaderInterface.cpp:245]
        → FWorldPartitionLoadingContext::IContext::IContext()  [WorldPartitionHandle.cpp:80]
          → check(ActiveContext == &DefaultContext)  ← 触发
```

### 根因
`FWorldPartitionLoadingContext::ActiveContext` 是全局静态(`WorldPartitionHandle.cpp:57`,初值 `&DefaultContext`);loading context 是**栈作用域 RAII**,构造时断言"当前必须是默认 context"(即**不允许嵌套**)。断言触发 = 切换被**同步**执行在了一个**已激活的 WP 加载操作调用栈内部**(re-entrancy)。刚启动时地图正在流式加载,脚本调用恰好落在这个窗口 → 嵌套 → 崩溃。

注意:UI 工具栏走**同一** `GEditor->SetPreviewPlatform`,同样时序理论上也会崩;这是引擎在 WP 图上切预览平台的时序脆弱点,不是绑定 wrapper 的逻辑错误。

### 修复:延迟到 core-ticker 执行
把 `GEditor->SetPreviewPlatform` 从**内联同步**改为**延迟到下一 core-ticker tick**执行(见上文 `ApplyPreviewPlatformDeferred`,`FTSTicker::GetCoreTicker()`)。core ticker 由 `FEngineLoop::Tick` 在任何 WP 加载操作之外泵动,该点 `ActiveContext` 必为默认 → `RefreshLoadedState` 创建自己的 context 时断言通过。

**附带好处**:切换内部长达数分钟的 shader 重编(`ProcessAsyncResults` 同步阻塞)不再阻塞脚本调用帧,Python 调用立即返回。

**语义变化**:`SetPreviewPlatformByName` 返回 `true` = "平台有效、切换已排程"(下一 tick 生效);`GetCurrentPreviewPlatformName()` 在延迟切换执行后才反映新值;无效/未编入的平台名仍**同步**返回 `false`。

### 修复验证(在原崩溃时序下)
编辑器刚连上、流式加载中**立即** `set_preview_platform_by_name("AndroidVulkan_Preview")` → **不崩溃**,即时返回 `True`;延迟执行后编辑器存活,`get_current` = `AndroidVulkan_Preview`;`reset` 回 `None`。往返全绿。

---

## 六、构建与验证

### 构建
- 目标:`S1GameEditor Win64 Development`(带 `-project`),**增量仅重编 UnrealEd 模块** + UHT + 重链 `UnrealEditor-UnrealEd.dll`,非全引擎重编。无 Build.cs 改动。
- 命令:
  ```bash
  "UE5EA/Engine/Build/BatchFiles/Build.bat" S1GameEditor Win64 Development \
    -project="D:/GR_DevTest/S1Game/S1Game.uproject" -waitmutex
  ```
- **Live Coding 不适用**:新增反射类/UFUNCTION 无法热载,必须正式编译 + **重启编辑器**才能被 Python 看到。

### 实测结果(全绿)
- `unreal.PreviewPlatformScriptLibrary` + 4 方法全部暴露(Python / EUB 皆可达)。
- 可用预览平台(本 build 实际枚举,10 项):
  `AndroidVulkan_Preview / IOSMetal_Preview / METAL_SM5_Preview / METAL_SM6_Preview / VULKAN_SM5_Preview / PCD3D_SM5_Preview / PCD3D_SM6_Preview / PCD3D_ES3_1_Preview / SP_PCD3D_ES3_1_SDF / PS5_Preview`
- 完整往返:`None → AndroidVulkan_Preview / PCD3D_ES3_1_Preview → None`。
- 无效名 / 空名 → 同步 `False`,不崩溃、不排程。

> 关键实测点:实际 Android 键是 **`AndroidVulkan_Preview`**(非想当然的 `AndroidES31_Preview`);桌面上验证 mobile 管线最实用的是 **`PCD3D_ES3_1_Preview`**(桌面 D3D 上跑移动 feature level,无需真机)。数据驱动枚举天然兼容了这些差异。

### 两个坑(务必知道)
1. **切 mobile 预览会超时属正常**:切换触发全量材质 + global shader 重编,同步阻塞 game thread(`EditorEngine.cpp:6386-6395`),MCP 请求会 "Timeout receiving Unreal response";编译完再读即成功。reset 回桌面因 shader 已缓存是秒回。
2. **Live Coding 残留导致编译失败(exit 6)**:报 `Unable to build while Live Coding is active`。成因是残留的 `LiveCodingConsole.exe` + 未完全退出的 `UnrealEditor.exe` 仍持锁。处理:
   ```bash
   powershell -Command "Stop-Process -Name LiveCodingConsole,UnrealEditor,CrashReportClientEditor -Force"
   ```
   保留 `UnrealGameSync` / `UnrealTraceServer`,清完重编即通过。

---

## 七、日常用法
```python
import unreal
L = unreal.PreviewPlatformScriptLibrary
L.get_available_preview_platform_names()                  # 列出可选平台
L.set_preview_platform_by_name("PCD3D_ES3_1_Preview")     # 桌面移动预览(最贴合 mobile 管线验证)
L.get_current_preview_platform_name()                     # 查当前(延迟生效后)
L.reset_preview_platform_to_default()                     # 退回桌面 Deferred(= Disable Preview)
```
Editor Utility Blueprint 里可搜到 "Set Preview Platform By Name" 节点。

---

## 八、可复用套路:把"非反射的引擎 editor 函数"暴露给脚本
1. 定位目标函数 & 参数结构的声明模块(本例 UnrealEd 的 `EditorEngine.h`)。
2. 在**同模块**加 `UBlueprintFunctionLibrary`(`UCLASS(MinimalAPI)` + `static UNREALED_API`),对外用**可反射的简单类型**(FName/bool/TArray),内部自己构造非 USTRUCT 参数。
3. 参数构造**照抄引擎 UI 回调的正典写法**(本例 `LevelEditor.cpp` 菜单构造),别自己猜字段。
4. 同模块加新 UCLASS **无需改 Build.cs**(UHT 自动扫 `Public`/`Private`/`Classes`),前提是模块已依赖所需库。
5. 若目标函数会广播 → 驱动 WorldPartition / streaming 等**有 loading context 的子系统**,考虑**延迟到 core-ticker** 执行,避免嵌套 context 断言,并让长任务不阻塞调用帧。
6. 新反射类**必须重编 + 重启编辑器**(Live Coding 载不了),之后 `unreal.<ClassName>.<snake_case_method>()` 自动可用。

---

## 九、相关参考(源码 file:line)
- `UE5EA/Engine/Source/Editor/UnrealEd/Classes/Editor/EditorEngine.h` — `FPreviewPlatformInfo`(221-302)、公有 `PreviewPlatform`(604)、`SetPreviewPlatform`(3137)
- `UE5EA/Engine/Source/Editor/LevelEditor/Private/LevelEditor.cpp:1907-1937` — 预览平台构造正典(disable 分支 1924)
- `UE5EA/Engine/Source/Editor/LevelEditor/Private/LevelEditorActions.cpp:781/790/845` — 跨模块读 `GEditor->PreviewPlatform`(证明 public);`CanExecutePreviewPlatform`(813-841,可选加固:UI 侧可用性校验)
- `UE5EA/Engine/Source/Runtime/Core/Public/Misc/DataDrivenPlatformInfoRegistry.h` — `FPreviewPlatformMenuItem`(98-113)、`GetAllPreviewPlatformMenuItems`(357)
- `UE5EA/Engine/Source/Runtime/RHI/Public/DataDrivenShaderPlatformInfo.h` — `GetShaderPlatformFromName`(152)、`GetPreviewShaderPlatformParent`(858)、`GetMaxSupportedFeatureLevel`(986)
- `UE5EA/Engine/Source/Runtime/Engine/Private/WorldPartition/WorldPartitionHandle.cpp:57/78-88` — `ActiveContext` 全局 + `IContext` 断言
- `UE5EA/Engine/Source/Editor/UnrealEd/Public/Subsystems/EditorSubsystemBlueprintLibrary.h` — MinimalAPI + static UNREALED_API 规范范例

## 新增文件
- `UE5EA/Engine/Source/Editor/UnrealEd/Public/PreviewPlatform/PreviewPlatformScriptLibrary.h`
- `UE5EA/Engine/Source/Editor/UnrealEd/Private/PreviewPlatform/PreviewPlatformScriptLibrary.cpp`
- P4 client:`DJANGOZHAN-PCFW_GR_DevTest`(stream `//GR/DevTest`);两文件已 `p4 add`(default changelist,未 submit)。引擎 Binaries 在库中 head=delete,编译产物不入库。
