# UE 命令行 CVar 通道分流：-dpcvars vs -ExecCmds（ReadOnly/Cheat × 优先级）

> 适用：UE5 fork（GR_DevTest / UE5EA），Android 命令行配置。
> 场景：给 UECommandLine.txt / 打包 `-cmdline` 配 CVar，要"确保生效"。
> 结论先行：**按 CVar 的 ECVF flag 分流——ReadOnly 只能走 `-dpcvars`/DP/ini；Cheat 只能走 `-ExecCmds`/consolevariables.ini；普通项走 `-ExecCmds`（优先级最高）。**

---

## 1. 两个通道的机制差异

| | `-ExecCmds="a 1,b 2"` | `-dpcvars="a=1,b=2"` / `-dpcvar=` |
|---|---|---|
| 本质 | 启动期 console exec 命令 | DeviceProfileManager 的命令行 DP-CVar 注入 |
| SetBy | **SetByConsole**（最高级） | **SetByDeviceProfile**（中档）；`-ForceDPCVars=` 才 SetByCommandline |
| ReadOnly CVar | ❌ **硬拒**：`Error: %s is read only!` | ✅ 能设（Set 只查优先级，不查 ReadOnly） |
| Cheat CVar | ✅ 能设（非 shipping console） | ❌ 拒设（`OnSetCVarFromIniEntry` 默认 `bAllowCheating=false`） |
| 应用时序 | 晚（引擎起来后） | 早（DeviceProfile 应用期），同优先级下覆盖 DP 段值 |
| 失败形态 | log `Error: xxx is read only!` | log warning `requested priority is too low ... value remains`，或 Cheat 被 ensure |

**注意：低优先级来源若已设，`-dpcvars`(SetByDeviceProfile) 也会静默失效**（DeviceProfileManager 显式告警 `requested priority is too low`）。

## 2. 分流规则表

| CVar 带 flag | 该走哪个通道 |
|---|---|
| `ECVF_ReadOnly` | **`-dpcvars` / DP 段 / ini / 代码**。console(-ExecCmds) 永远不行（包括运行时控制台手输）。 |
| `ECVF_Cheat`（非 RO） | **`-ExecCmds`** 或 `consolevariables.ini`。dpcvars/DP/ini 拒。 |
| 无特殊 flag / Scalability / RT / Preview | **`-ExecCmds`**（console 优先级最高、最稳）；`-dpcvars` 亦可但优先级低。 |
| Cheat + ReadOnly 并存 | 只能 `consolevariables.ini` / 代码。两个命令行通道都不行。 |

## 3. 源码依据（fork 路径可复核）

- **console Exec 拒 ReadOnly**：`Runtime/Core/Private/HAL/ConsoleManager.cpp` Exec 分支
  `const bool bReadOnly = CVar->TestFlags(ECVF_ReadOnly); ... else if(bReadOnly){ Ar.Logf("Error: %s is read only!") }`。
- **优先级判定 CanChange**：同文件 `NewPri >= OldPri`（`Setting ... was ignored as it is lower priority ... Value remains`）。
- **dpcvars 拒 Cheat / 设 RO**：`Runtime/Core/Private/Misc/ConfigUtilities.cpp` `OnSetCVarFromIniEntry`：
  `bCheatFlag = CVar->TestFlags(ECVF_Cheat); bAllowChange = !bCheatFlag || bAllowCheating;`（dpcvars 调用默认 bAllowCheating=false）。
- **dpcvars 的 SetBy 与"覆盖 DP"**：`Runtime/Engine/Private/DeviceProfiles/DeviceProfileManager.cpp`：
  `-dpcvars/-dpcvar` → `ECVF_SetByDeviceProfile`；`-ForceDPCVars=` → `ECVF_SetByCommandline`；注释 *"pre-apply ... override anything in the DPs"*；并带 `requested priority is too low` 告警。

## 4. 实测样例（S1Game 845 配置，13 项分流）

各 CVar 精确定义点与 flags：

**ReadOnly 3 项 → 走 `-dpcvars`**
- `r.Mobile.ShadingPath` — `ConsoleManager.cpp:3676` `ECVF_ReadOnly|RT`
- `r.Mobile.EarlyZPass` — `Renderer/Private/RendererScene.cpp:138` `ECVF_ReadOnly`
- `r.Mobile.AmbientOcclusion` — `Renderer/Private/PostProcess/PostProcessAmbientOcclusionMobile.cpp:24` `ECVF_ReadOnly|RT`

**非 RO 10 项 → 走 `-ExecCmds`**（其中无 Cheat，console 均可用）
- `r.Mobile.AntiAliasing`(ConsoleManager:4452 RT|Preview|Scalability)、`r.Mobile.TonemapSubpass`(MobileBasePassRendering:77 Scalability|RT)、`r.MobileContentScaleFactor`(ConsoleManager:4106 Default)、`r.YHRP.EnableMobileOutlinePass`(MobileShadingRenderer:95 无flag)、`r.LuxGI`(LuxGIRendering:31 RT|Scalability)、`r.InstanceCulling.OcclusionCull`(InstanceCullingContext:47)、`r.AllowOcclusionQueries`/`r.HZBOcclusion`(SceneVisibility:472/145)、`r.ScreenPercentage`(LegacyScreenPercentageDriver:36 Scalability)。

成品：
```
-dpcvars="r.Vulkan.UseChunkedPSOCache=0,r.Mobile.ShadingPath=0,r.Mobile.EarlyZPass=2,r.Mobile.AmbientOcclusion=0"
-ExecCmds="...其余10项..."
```

## 5. 陷阱速记

- **ReadOnly ≠ "运行时锁死"**：它只是 console 通道拒设；ini/DP/dpcvars/代码启动期都能设。已设后再改仍需非 console 且 ≥ 原优先级。
- **同 CVar 别双设**：两处值不一致时低优先那份静默兜底，旧值残留会悄悄生效，排查困难。单一事实源放最高优先通道。
- **`r.Shadowquality`（小写 q）是无效 CVar**：引擎只定义 `r.ShadowQuality`（scalability 那套）。小写进 ini/命令行 → config 建 dummy，不控制阴影。
- **生效前提（Android）**：运行时读 **APK 内** UECommandLine.txt（logcat `Using APK commandline`）。sdcard/本地放一份不生效；必须作为打包 `-cmdline` 源进包。装包后验证：log 抓 `Final commandline` 或控制台回读。
- 验证形态：ExecCmds 成功=无 read-only 报错且 console 回显 `name = "value"`；dpcvars 成功=`Setting CommandLine Device Profile CVar` + `Set CVar`。

---
来源：GR_DevTest 845 性能适配 session（2026-09-08）。相关 P4：changelist 1120346（DefaultDeviceProfiles.ini LowDriver 段）。
