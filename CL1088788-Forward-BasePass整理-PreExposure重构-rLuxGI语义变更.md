# CL 1088788：Forward BasePass 整理 + PreExposure 模型重构 + r.LuxGI=0 语义变更

> CL 1088788（`--story=1096685`，杨悉琪/lemonxqyang，2026-08-14）一次提交 4 块工作：**GI 分级 / 防漏光性能优化 / Forward BasePass 整理 / 修正角色半透曝光**。核心是 `MobileBasePassPixelShader.usf` 把 PreExposure 从"末尾整体乘 + 剥离 LuxGI"重构为"各分量按序乘"，并顺带把 `r.LuxGI=0` 的语义从"无间接光"重定义为"降级到 Low 画质（天光 SH 兜底）"。

---

## 一、CL 概览

18 个文件，分属 4 组：

| 工作项 | 涉及文件 | 改动主题 |
|---|---|---|
| **① GI 分级** | `MobileDeferredShadingPass.cpp`、`MobileLightingCommon.ush`、`LuxGIRendering.cpp/.h`、`LuxGIStreamingUpdate.cpp`、`LuxGIDiffuseSample.usf`、`Android/IOSScalability.ini` | `r.LuxGI.QualityLevel` 三档（Low/Mid/High）+ streaming 分级 |
| **② 防漏光性能优化** | `LuxGIShared.ush` | Mip0 Anti-Leak 双 Chebyshev 权重 |
| **③ Forward BasePass 整理** | `MobileBasePassPixelShader.usf`、`MobileBasePassRendering.cpp/.h`、`MobileBasePass.cpp`、`MobileLightingCommon.ush`、`ToonShadingModels.ush`、`ToonDeferredLightingCommon.ush` | PreExposure 模型重构 + 光照累加整理 |
| **④ 修正角色半透曝光** | 混在 ③ 的 shader 里 | 半透 toon `PreExposure=1.0f` + 半透间接光不乘 Pre |

---

## 二、核心：Forward BasePass 的 PreExposure 模型重构

### 2.1 旧链路（post-multiply + 剥离，脆弱）

`#23`（含 ZXB 修复）的做法：
```
方向光 ─┐
LuxGI   ─┼─→ TotalLight ──(内部已乘一次 Pre)──→ 末尾 OutColor.rgb *= PreExposure
IBL     ─┘
```
- LuxGI 内部（`GetLuxGIFullLightingWithNonCompressedData`）已乘一次 Pre → 末尾再乘 = **double PreExposure**（≈Pre²），Forward 偏亮一个档次。
- ZXB 修复 = 快照（方向光后 TotalLight）→ 剥离（`LuxGIExtracted = TotalLight - 快照`）→ 末尾不乘 Pre 回加。
- **致命点**：快照/剥离/回加三块必须同源（`#if ENABLE_LUX_GI && (MASKED||SOLID) && ...`），否则变量未定义，极易编译错。

### 2.2 新链路（per-component pre-multiply，杨悉琪方案）

`#24` 把"末尾统一乘"改成**每个"应乘 Pre"的分量在各自累加点乘掉**：

| 分量 | PreExposure 应用 | 位置 |
|---|---|---|
| 方向光（非 toon） | `DirectLighting.TotalLight *= PreExposure` | usf 方向光块 |
| LuxGI 间接光/粗糙反射（opaque） | `*= View.PreExposure * GBuffer.GBufferAO` | `AccumulateLuxGILighting` 顶层共享后处理（`MobileLightingCommon.ush:1043-1045`） |
| IBL 高光 | `SpecularIBLLighting *= View.PreExposure` | `AccumulateReflection` 内部（`MobileLightingCommon.ush:635`） |
| Emissive | `Color += Emissive * PreExposure` | usf Emissive 块 |
| 末尾整体乘 | **注释掉** | 文件尾部 |
| toon 方向光 | 不乘 Pre，乘 `(1+ToonEnergyWeight)` | usf `#if MATERIAL_SHADINGMODELS_TOON_CHARACTER` |
| 半透 toon | `PreExposure = 1.0f` 硬特判；半透分支间接光不乘 Pre | usf + `MobileLightingCommon.ush:1040-1042` |

**配套动作（这次整理成立的关键）**：
1. LuxGI 内部的 Pre 乘法**上提到 `AccumulateLuxGILighting` 顶层共享后处理**，对 High/Mid/Low 三档统一生效（`MobileLightingCommon.ush:1043-1045`，仅 opaque 分支乘，半透只乘 `IndirectLightingColorScale`）。
2. `AccumulateReflection` 内部**补了 `SpecularIBLLighting *= View.PreExposure`**（`MobileLightingCommon.ush:635`）——旧方案 IBL 靠末尾乘 Pre（所以剥离点必须放在 IBL 之前）；新方案直接内部乘掉，彻底摆脱末尾依赖。

⇒ 快照/剥离/回加三件套、`LuxGIExtracted` 变量、`ENABLE_LUX_GI` 三处调用点剪枝、`ApplyCartoonFoliage` 对齐 Deferred 大段补丁、YivanLee 的 DEFERRED 段**全部可以删除**。

### 2.3 toon 角色的曝光收口

toon 方向光**故意不乘 Pre**，因为 toon 最终色由 `ApplyMobileToonCombineShadowColor`（`ToonMobileLightingCommon.ush:373`，老代码本 CL 未改）统一收口：

```hlsl
PreExposureFactor = clamp(ToonShading.LogInvPreExposure, -5, 5);  // LogInvPreExposure = -Log2(Pre)
float ToonColorExposureFactor = lerp(pow(2.0f, PreExposureFactor), ConstExposure, ToonPreExposureWeight);
//      = lerp( 1/PreExposure, ConstExposure, ToonPreExposureWeight )
return InColor * ToonShadowBlendFactor * ToonAO * ToonColorExposureFactor;
```

`pow(2, -Log2(Pre)) = 1/Pre` —— toon 的曝光在 `1/Pre` 与 `ToonConstExposure` 之间按 `ToonPreExposureWeight` 混合，**不走全局 PreExposure**。方向光若也乘 Pre 这里就抵消了，所以 toon 分支跳过。

---

## 三、⚠️ r.LuxGI=0 语义变更（最需要留意的点）

### 3.1 机制

`AccumulateLuxGILighting` 顶层 dispatch（`MobileLightingCommon.ush:1021`，Forward/Deferred **共享**）：

```hlsl
#if !ENABLE_LUX_GI || LUXGI_QUALITY_LEVEL == LUXGI_QUALITY_LEVEL_LOW
    ExposureBase = AccumulateLuxGILighting_Low(...);   // sky-SH only
#elif LUXGI_QUALITY_LEVEL == LUXGI_QUALITY_LEVEL_MID
    ExposureBase = AccumulateLuxGILighting_Mid(...);   // Mip1 only
#else
    ExposureBase = AccumulateLuxGILighting_High(...);  // full quality
#endif
```

- `r.LuxGI=0` → `ENABLE_LUX_GI=0` → 第一条件 `!ENABLE_LUX_GI` **短路** → 编译期选中 **Low 档**（与 `r.LuxGI.QualityLevel` 多少无关，即使默认 2 也一样）。
- **`r.LuxGI=0` ≡ `r.LuxGI=1 + QualityLevel=0`**（shader 层面逐位相同）。
- 这正是 `MobileDeferredShadingPass.cpp` 注释 *"No point compiling per-quality-level variants when GI is disabled entirely"* 的设计意图。

### 3.2 Low 档 = 天光 SH 兜底（非零间接光）

`AccumulateLuxGILighting_Low` 调用 `GetSimpleGIData`（`LuxGIShared.ush:1874`），**不采样任何 LuxGI 体积数据**，返回纯天光 SH：

```hlsl
half3 SkyColorM = View.SkyLightColor.rgb * View.SkyLightIntensityScale;
GlobalLuxValue.rgb = GetSkySHDiffuse(WorldNormal);      // 引擎天光 SH
GlobalLuxValue.rgb *= SkyColorM;
GlobalRoughReflectionValue.rgb = GetFakeGlobalLuxSH(ReflectionDir) * SkyColorM;
OutDiffuseLighting = FinalLuxLighting;                   // ← 非零！
// 兜底：if (GILuminance < Luminance(SkyColorM)*0.05) FinalLuxLighting = SkyColorM*0.05f;
```

**结论：`r.LuxGI=0` 走 Low 档 = 画面仍有天光间接漫反射，背光不全黑。**

### 3.3 与旧方案冲突 + 对 F/D 同时生效

- **旧 ZXB 方案**：`#if MOBILE_USE_GBUFFER || ENABLE_LUX_GI` 剪掉 `GetPrecomputedIndirectLightingAndSkyLight` → `r.LuxGI=0` 时 Forward 背光**全黑**（对齐当时 Deferred lighting pass）。
- **新方案**：`r.LuxGI` 从"开/关 LuxGI"重定义为 **"LuxGI 与质量档的融合开关"**——完全关闭 = 最低画质档（天光 SH 兜底），保证画面不黑。
- **对 Deferred 同时生效**：`AccumulateLuxGILighting` 是共享函数，Deferred 的 `MobileDeferredShading.usf` 在 `#24` 也改成无条件调用。所以 `r.LuxGI=0` 时 Deferred 也从"无间接光"变成"天光 SH 兜底"。
- **调试抓手失效**：之前"关 r.LuxGI 让背光变黑来对比 Deferred"的手法不再成立——关 LuxGI 后画面仍有天光间接光，**这是设计不是 bug**。

---

## 四、GI 分级（① 与 Forward BasePass 的交集）

- **C++ 侧**：`MobileDeferredShadingPass.cpp` 新增 `ELuxGIQualityLevel` 枚举（Low=0/Mid=1/High=2）+ CVar `r.LuxGI.QualityLevel`（默认 2）+ `FMobileDirectionalLightFunctionPS::FLuxGIQualityLevelDim` permutation；`MobileBasePass.cpp` 在 Forward 侧同样把 QualityLevel 并入 `TMobileBasePassPS` 的 permutation 计算。
- **shader 侧**：`AccumulateLuxGILighting` 拆成 `_High`（Mip0 sparse-page + Mip1 blend，原实现）/ `_Mid`（仅 Mip1）/ `_Low`（sky-SH），顶层按宏编译期 dispatch；`MobileLightingCommon.ush` 顶部暴露 `LUXGI_QUALITY_LEVEL_{LOW,MID,HIGH}`。
- **streaming**：`LuxGIRendering.cpp` 新增 `GetGIStreamingQualityLevel()`，quality ≤ 1 时 `ShouldUseOnlyMip1Irradiance=true`；`LuxGIStreamingUpdate.cpp` quality==0 直接跳过 streaming。
- **配置**：`AndroidScalability.ini` / `IOSScalability.ini` 各档位写入 `r.LuxGI.QualityLevel` + `r.LuxGI.AvoidLightLeaking`（iOS 另加 `r.gihack.IndirectLightingScale=0.65`）。
- **顺带修复**：`LuxGIStreamingManager.cpp` 把引用改指针判空（修潜在 use-after-free）。

### FLuxGIQualityLevelDim 定义不一致（实际影响零）

| | Forward `TMobileBasePassPS` | Deferred `FMobileDirectionalLightFunctionPS` |
|---|---|---|
| 定义 | `RANGE_INT("LUXGI_QUALITY_LEVEL", 0, 3)`（4 档） | `ENUM_CLASS("LUXGI_QUALITY_LEVEL", ELuxGIQualityLevel)`（0-2） |
| C++ clamp | `clamp(r.LuxGI.QualityLevel, 0, 2)` | 同左 |
| 第 3 档 | 存在但永不选中 | 不存在 |

dispatch 只看宏值 0/1/2，两边数值兼容；`r.LuxGI=0` 走 Low 靠 `!ENABLE_LUX_GI` 短路，不依赖 QualityLevel。Forward 的 `RANGE_INT(0,3)` 第 3 档是冗余槽位（`// TODO reduce permutation` 为它留的），不影响正确性。

---

## 五、防漏光（②）

`LuxGIShared.ush` 新增 `CalculateMip0AntiLeakWeights`（Simple Chebyshev + Complex Chebyshev 双权重），Mip0 级采样按 `AvoidLightLeakingMethod & 8` 分流，无 occlusion brick 时走 Simple Chebyshev 近似，减少性能开销。

---

## 六、快速排查 Checklist

1. **PreExposure 别再找"末尾整体乘"**——新模型已改为各分量 pre-multiply，`OutColor.rgb *= PreExposure` 在 Forward base pass 末尾已被注释。
2. **LuxGI 间接光 Pre 在哪**：`AccumulateLuxGILighting` 顶层共享后处理 `*= View.PreExposure * GBuffer.GBufferAO`（`MobileLightingCommon.ush:1043-1045`），Forward/Deferred 都走这里。
3. **r.LuxGI=0 走 Low 档**（`!ENABLE_LUX_GI` 短路），关 LuxGI 背光**不会全黑**（天光 SH 兜底）——调试时别再预期全黑。
4. **r.LuxGI=0 ≡ r.LuxGI=1 + QualityLevel=0**——想彻底关闭 LuxGI 体积采样用这两组之一即可。
5. **toon 方向光不乘 Pre**，最终曝光由 `ApplyMobileToonCombineShadowColor` 的 `1/Pre` 收口——排查 toon 亮度时看 `ToonPreExposureWeight` / `ToonConstExposure`，不看全局 Pre。
6. **半透 toon**：`PreExposure=1.0f` 硬特判 + 半透分支间接光不乘 Pre——半透角色不吃全局曝光，这是"修正角色半透曝光"的落地。
7. **想看旧 double-Pre 剥离逻辑**：已删干净，别在现网找 `LuxGIExtracted` / `ExposureAffectedLight`。

## 七、验证待办

1. `r.LuxGI=0` vs `r.LuxGI=1 + QualityLevel=0` 逐位一致（Forward 非 toon 墙壁背光探针）。
2. 新基准下 Forward/Deferred 间接光 Pre 对齐（`*= PreExposure*GBufferAO` 统一生效），复用 `pc-basepass-shaderprint-test-baseline` 打桩环境；顺带确认旧 2× 亮度差是否已结构性消除。
3. toon Pre 收口：`pow(2, LogInvPreExposure)=1/Pre` + `ToonPreExposureWeight` 混合是否与 Deferred 逐位一致。
4. 明天跑包体：验证 storeOp（SceneColor/Depth 恢复 memoryless 后是否 DONT_CARE）+ SceneDepth 材质节点。

---

## 八、相关参考

- **TapD 单**：story 1096685「Mobile 预研总单 - 引擎 TA 总单 - GI 分级方案」https://www.tapd.cn/68880148/s/5107190
- **CL 提交人/workspace**：Tools_Program_Support @ Tools_Program_Support_lemonxqyan-PC4_DevTest
- **关键源码位置（本 fork，2026-08-26 复核）**：
  - `MobileBasePassPixelShader.usf` — Forward BasePass 光照累加（PreExposure pre-multiply / 描边 / Emissive）
  - `MobileLightingCommon.ush:897-1144` — `AccumulateLuxGILighting` 顶层 + High/Mid/Low 三档 + 共享后处理（`:1043-1045` Pre*AO）；`:635` IBL PreExposure
  - `MobileBasePassRendering.h` — `TMobileBasePassPS::FLuxGIQualityLevelDim`（RANGE_INT 0-3）+ `SceneOutlineColor` 参数
  - `MobileDeferredShadingPass.cpp` — `ELuxGIQualityLevel` + `r.LuxGI.QualityLevel` CVar + Deferred permutation
  - `LuxGIShared.ush:1874` — `GetSimpleGIData`（Low 档天光 SH 兜底）；`:CalculateMip0AntiLeakWeights`（防漏光）
  - `LuxGIRendering.cpp` — `GetGIStreamingQualityLevel()` + Mip1-only 分流
  - `ToonMobileLightingCommon.ush:373` — `ApplyMobileToonCombineShadowColor`（toon Pre 收口）
- **关联记忆**：`fwd-vs-def-post-tonemap-2x-unpinned`（新基准验证）、`forward-combine-shadow-duplicate-removed`（CL 相关对齐判断）、`mobile-fwd-vs-def-exposure-mismatch`、`toon-gbuffer-has-no-ao-slot`。

归档时间：2026-08-26。
