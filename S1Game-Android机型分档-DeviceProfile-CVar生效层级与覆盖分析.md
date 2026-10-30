# S1Game Android 机型分档：DeviceProfile CVar 生效层级与覆盖分析

> 对象：S1Game Android 画质分档（845/855 低配 · 865/870 中配 · 888+ 高配）。
> 结论：按 GPU 型号分流已落地，驱动版本不再参与分档；12 项 DeviceProfile CVar 不会被 ini / scalability 覆盖，但 `r.ScreenPercentage` 硬设会压制 ResolutionQuality 分级；一套 pak 无法同时支持 Forward 与 Deferred。
> 关键数字：`SetByDeviceProfile=0x06 > SetBySystemSettingsIni=0x04 > SetByScalability=0x01`；`PerfIndexValues_ResolutionQuality="50 70 70 85 100 100"`。

## 一、结论：三档映射与管线绑定

分档已从「按 Vulkan 驱动版本」改为「按 GPU 型号」。

| 机型 | GPU | 命中 Profile | 画质档 | 管线 |
|---|---|---|---|---|
| 845 / 855 及以下 | Adreno 6[0-4]x | `Android_Adreno6xx_Vulkan_LowDriver` | Low (0) | Forward (0) |
| 865 / 870 | Adreno 650 | `Android_Adreno65x_Vulkan` | Mid (1) | Forward (0) |
| 888 | Adreno 660 | `Android_Adreno66x_Vulkan` | High (2) | Deferred (1) |
| 8 Gen 1 | Adreno 68x | `Android_Adreno68x_Vulkan` | High (2) | Deferred (1) |
| 8 Gen 2/3/4 | Adreno 7xx/8xx | `Android_Adreno_Vulkan_SM5` | High (2) | Deferred (1) |

画质档走 `sg.FirstLaunchQuality` → `Scalability::SetFromSingleQualityLevel`，一次性决定分辨率、视距、AA、阴影、GI、反射、后处理、纹理、特效、植被等十几个组的档位（`UE5EA/Engine/Source/Runtime/Engine/Private/Scalability.cpp:1769`）。

## 二、匹配规则改造：从驱动版本到 GPU 型号

旧规则（`UE5EA/Engine/Config/BaseDeviceProfiles.ini:883`）要求 `driver < 440`，845 的 V@415 能命中，换驱动版本就掉档。改造后按型号匹配。

匹配顺序（first-match-wins）：

| 行号 | 条件 | 目标 Profile | 档位 |
|---|---|---|---|
| L869 | Adreno 7xx/8xx + SM5 | `Android_Adreno_Vulkan_SM5` | High |
| L872 | Adreno 660 + driver<=525 | `Android_Adreno66x` | High |
| L874 | Adreno 65x + Vulkan | `Android_Adreno65x_Vulkan` | Mid |
| L876 | Adreno 66x + Vulkan | `Android_Adreno66x_Vulkan` | High |
| L878 | Adreno 68x + Vulkan | `Android_Adreno68x_Vulkan` | High |
| L883 | Adreno 6[0-4]x + Vulkan | `Android_Adreno6xx_Vulkan_LowDriver` | Low |
| L885 | Adreno 6xx + driver>=400 | `Android_Adreno6xx_Vulkan` | Low |
| L887 | Adreno 6[0-4]x | `Android_Adreno6xx` | Low |

改动三点：

1. `UE5EA/Engine/Config/BaseDeviceProfiles.ini:872` — 660 老驱动规则目标从 `Android_Adreno6xx` 改为 `Android_Adreno66x`。
2. `UE5EA/Engine/Config/BaseDeviceProfiles.ini:883` — regex `6[0-9][0-9]` → `6[0-4][0-9]`，删除 GLVersion `<440` 条件。
3. `UE5EA/Engine/Config/BaseDeviceProfiles.ini:887` — 兜底 regex 同步收窄为 `6[0-4][0-9]`。

必须改引擎文件的原因：项目层 `+MatchProfile` 排在引擎规则之后，first-match-wins 抢不到。

## 三、DeviceProfile CVar 生效层级与优先级

12 项非 `sg.` CVar 以 `ECVF_SetByDeviceProfile (0x06)` 写入，高于全局 ini 与 Scalability。

| SetBy | 值 | 来源 |
|---|---|---|
| SetByGameOverride | 0x08 | 用户游戏内覆盖 |
| SetByDeviceProfile | 0x06 | DeviceProfile `+CVars` / `-dpcvars` |
| SetBySystemSettingsIni | 0x04 | `[ConsoleVariables]` 段 |
| SetByProjectSetting | 0x03 | `[/Script/Engine.RendererSettings]` |
| SetByGameSetting | 0x02 | GameUserSettings |
| SetByScalability | 0x01 | `sg.*` 走这里 |
| SetByConstructor | 0x00 | 引擎注册默认值 |

枚举见 `UE5EA/Engine/Source/Runtime/Core/Public/HAL/IConsoleManager.h:142` 与 `UE5EA/Engine/Source/Runtime/Core/Public/HAL/IConsoleManager.h:152`；分派逻辑见 `UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:621`：

```
uint32 CVarPriority = bIsScalabilityBucket ? ECVF_SetByScalability : ECVF_SetByDeviceProfile;
```

特例：`sg.FirstLaunchQuality` 因 `sg.` 前缀走 scalability 分支，优先级最低。当前 `DefaultGameUserSettings.ini` 无 `[ScalabilityGroups]`，`UE5EA/Engine/Source/Runtime/Engine/Private/Scalability.cpp:1769` 的初始化条件成立，所以暂时不会被覆盖。

## 四、继承链与覆盖规则

继承链：`Android_Adreno6xx_Vulkan_LowDriver` → `Android_Low` → `Android`。

收集顺序为子档到父档，同键只保留第一次出现的值（`UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:335`）：

```
if (!DeviceProfileCVars.Find(CVarKeyName))
    DeviceProfileCVars.Add(CVarKeyName, CVarValue);
```

→ 子档优先，父档只补子档没有的键。

`-dpcvars=` 在 DeviceProfile 之后单独应用（`UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:646`），能覆盖 DP 的值。

## 五、覆盖风险与冲突点

### 5.1 r.ScreenPercentage 硬设压制 ResolutionQuality 分级

`S1Game/Config/Android/AndroidScalability.ini:6` 定义 `PerfIndexValues_ResolutionQuality="50 70 70 85 100 100"`，index = level 0..5。Low 档（level 0）本该 50。

但 `[Android_Low]` 里 `r.ScreenPercentage=100.0` 以 `ECVF_SetByDeviceProfile (0x06)` 写入，优先级高于分级的 `ECVF_SetByScalability (0x01)`，最终值为 100。

### 5.2 本地 OnePass 全局块污染 Mid/High

`DefaultEngine.ini:523-537`（本地未提交）在全局层设了 `r.LuxGI=0`、`r.Shadowquality=0`、`r.ScreenPercentage=100` 等。Mid/High 的 DeviceProfile 没有这些键，会继承全局值。该块在 HEAD 版本中不存在。

### 5.3 r.MobileContentScaleFactor 与 ScreenPercentage 是两层缩放

`r.MobileContentScaleFactor` 在 `UE5EA/Engine/Source/Runtime/ApplicationCore/Private/Android/AndroidWindow.cpp:575` 决定 Android surface 像素（窗口层），`r.ScreenPercentage` 决定渲染分辨率比例（管线层）。

### 5.4 AA 已交回分级

`r.Mobile.AntiAliasing` 由 `S1Game/Config/Android/AndroidScalability.ini:276-278` 的 `[AntiAliasingQuality@0]` 定义：`r.Mobile.AntiAliasing=0`，Low 档仍然关闭抗锯齿。

## 六、cook 期 shader perm 一致性约束

`r.Mobile.ShadingPath` 是 `ECVF_ReadOnly`。cook 读全局 ini 的单一值编译一套 mobile permutation；运行时 DeviceProfile 推另一个值就会缺 perm。

| cook 全局值 | 能跑的档 | 崩的档 |
|---|---|---|
| 0 (Forward) | Low / Mid | High（缺 Deferred perm） |
| 1 (Deferred) | High | Low / Mid（缺 Forward perm） |

要同时支持，必须出两套包（Forward 包 + Deferred 包），按设备 GPU 型号下发。

## 七、配置清单与文件位置

[Android_Low]（`S1Game/Config/DefaultDeviceProfiles.ini:1777-1798`）：

```
+CVars=sg.FirstLaunchQuality=0
+CVars=r.Mobile.ShadingPath=0
+CVars=r.YHRP.EnableMobileOutlinePass=0
+CVars=r.LuxGI=0
+CVars=r.Shadowquality=0
+CVars=r.Mobile.TonemapSubpass=1
+CVars=r.Mobile.AmbientOcclusion=0
+CVars=r.InstanceCulling.OcclusionCull=0
+CVars=r.Mobile.EarlyZPass=2
+CVars=r.ScreenPercentage=100.0
+CVars=r.MobileContentScaleFactor=0.75
+CVars=r.AllowOcclusionQueries=0
+CVars=r.HZBOcclusion=0
+CVars=r.Mobile.AllowSoftwareOcclusion=3
```

[Android_Mid]（`S1Game/Config/DefaultDeviceProfiles.ini:1813`）：`sg.FirstLaunchQuality=1`、`r.Mobile.ShadingPath=0`。

[Android_High]（`S1Game/Config/DefaultDeviceProfiles.ini:1831`）：`sg.FirstLaunchQuality=2`、`r.Mobile.ShadingPath=1`。

高端档 Base 覆盖（`S1Game/Config/DefaultDeviceProfiles.ini:1926-1941`）：`[Android_Adreno66x]`、`[Android_Adreno68x]`、`[Android_Adreno73x]`、`[Android_Vulkan_SM5]` 全部 `BaseProfileName=Android_High`。

## 八、验证方法

重打包装机后拉设备日志：

```
LogDeviceProfileManager: Pushing Device Profile CVar: [[sg.FirstLaunchQuality:... -> 0/1/2]]
LogDeviceProfileManager: Pushing Device Profile CVar: [[r.Mobile.ShadingPath:... -> 0/1]]
```

| 机型 | 预期 Profile | FirstLaunchQuality | ShadingPath |
|---|---|---|---|
| 845 / 855 | `Android_Adreno6xx_Vulkan_LowDriver` | 0 | 0 |
| 865 / 870 | `Android_Adreno65x_Vulkan` | 1 | 0 |
| 888 | `Android_Adreno66x_Vulkan` | 2 | 1 |
| 8 Gen 2/3 | `Android_Adreno_Vulkan_SM5` | 2 | 1 |

## 九、参考与证据索引

| 主题 | 位置 |
|---|---|
| 低配匹配规则 | `UE5EA/Engine/Config/BaseDeviceProfiles.ini:883` |
| 兜底匹配规则 | `UE5EA/Engine/Config/BaseDeviceProfiles.ini:887` |
| 660 老驱动规则 | `UE5EA/Engine/Config/BaseDeviceProfiles.ini:872` |
| LowDriver 注册 | `UE5EA/Engine/Config/BaseDeviceProfiles.ini:124` |
| 引擎默认管线 | `UE5EA/Engine/Config/BaseEngine.ini:3805` |
| SetBy 枚举 | `UE5EA/Engine/Source/Runtime/Core/Public/HAL/IConsoleManager.h:142` |
| DP 优先级分派 | `UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:621` |
| 子档优先 | `UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:335` |
| dpcvars 后应用 | `UE5EA/Engine/Source/Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp:646` |
| ini 设置入口 | `UE5EA/Engine/Source/Runtime/Core/Private/Misc/ConfigUtilities.cpp:277` |
| FirstLaunchQuality 定义 | `UE5EA/Engine/Source/Runtime/Engine/Private/Scalability.cpp:28` |
| FirstLaunchQuality 初始化 | `UE5EA/Engine/Source/Runtime/Engine/Private/Scalability.cpp:1769` |
| 低配判定阴影 | `UE5EA/Engine/Source/Runtime/Engine/Private/MMH/MMHShadowMap.cpp:173` |
| 分辨率分级 | `S1Game/Config/Android/AndroidScalability.ini:6` |
| AA 分级 level0 | `S1Game/Config/Android/AndroidScalability.ini:276` |
| CSR 窗口缩放 | `UE5EA/Engine/Source/Runtime/ApplicationCore/Private/Android/AndroidWindow.cpp:575` |
| 项目侧消费 | `S1Game/Source/S1Misc/Private/S1UserSettings.cpp:1905` |
