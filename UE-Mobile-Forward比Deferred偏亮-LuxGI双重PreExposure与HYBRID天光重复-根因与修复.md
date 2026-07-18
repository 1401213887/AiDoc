# UE Mobile Forward 比 Deferred 明显偏亮 —— LuxGI 双重 PreExposure + HYBRID 天光重复 根因与修复

> 现象：同一场景、同一 CVar 配置下，Mobile **Forward** 管线比 **Deferred** 管线明显亮一个档次（草地/植被等间接光主导的表面尤为明显）。截帧逐行对比后定位为**两个相互独立的 bug 叠加**：① LuxGI 间接光被乘了两次 PreExposure；② HYBRID 静态光照模式下 Forward base pass 多累加了一份 SkyLight 间接漫反射（与 LuxGI 内部的 fake-global 天光 SH 重复）。二者修复后 Forward 与 Deferred 亮度对齐。

> 涉及文件：`UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`（只改这一个 .usf，Deferred 的 `MobileDeferredShading.usf` 未改）。所有改动用 `#pragma region Engine ZXB` 包裹。

---

## 一、问题定位流程

### 1.1 输入材料
- Mobile 截帧的三份编译后 shader（宏已展开，最可靠）：
  - `ForwardBasePassV1.txt`（Forward 单 pass，函数 `Main`）
  - `DeferredBasePassV1.txt`（Deferred base pass，函数 `Main`）
  - `DeferredLightingPassV1.txt`（Deferred 光照 pass，函数 `MobileDirectionalLightPS`）
- 现象截图：Forward 草地整体偏亮，Deferred 较暗且更"实"。

### 1.2 定位方法
逐项对比"草地像素"最终颜色的每一项光照贡献及其 PreExposure 次数。关键运行时参数：`View.PreExposure = 2.3576`。

### 1.3 关键发现（按定位顺序）
1. **PreExposure 作用范围**：Forward 末尾对整个 `OutColor` 统一 `*= PreExposure`；Deferred 分散在各阶段，反射/LuxGI 处理位置不同。
2. **截帧铁证 1（LuxGI double-Pre）**：`AccumulateLuxGILighting` **内部**已 `IndirectDiffuseLighting *= View_PreExposure`（Forward 截帧 6103 行、Deferred 截帧 6593 行都有）；而 Forward 末尾又 `OutColor.rgb *= PreExposure`（6697）→ LuxGI 被乘 **两次** Pre（≈ 2.3576² ≈ 5.56），Deferred 只乘一次。
3. **IBL 不能一起剥离**：`AccumulateReflection` 内部**不**乘 Pre（Forward 截帧 5999-6058 只有 `SpecularIBL * GetEnvBRDF`），它靠末尾那次 `×Pre`，必须保留。
4. **截帧铁证 2（天光重复）**：Deferred base pass 的 `GetSkyLighting` / `GetPrecomputedIndirectLightingAndSkyLight` **恒返回 0**（DeferredBasePass 截帧 3328-3348）；Forward base pass 真实计算天光（ForwardBasePass 截帧 6545-6554）。
5. **配置证据**：`r.Mobile.StaticLightingMethod` 默认值 = **2 (HYBRID)**（`LuxGIRendering.cpp:85`），项目 Config 未覆盖。HYBRID 下 Forward 会同时累加 base 间接光 + LuxGI = 两份间接光。

---

## 二、根因分析

### 根因 A：LuxGI 被乘了两次 PreExposure（double exposure）

- Forward `Main` 内 `AccumulateLuxGILighting` 内部：`IndirectDiffuseLighting/RoughReflection *= View_PreExposure`（一次）。
- Forward `Main` 末尾：`OutColor.rgb *= ResolvedView.PreExposure`（又一次）。
- → LuxGI 项 = `×PreExposure²`。Deferred 的 LightingPass 里 `*= PreExposure`（第 328 行等价）发生在 LuxGI 累加**之前**，LuxGI 是之后加入的，故只乘一次。

### 根因 B：HYBRID 模式下 Forward 多累加一份 base pass SkyLight 间接漫反射

- 静态光照枚举（`LuxGIRendering.h:470-472`）：`SLM_LIGHTMAP_ONLY=0`、`SLM_LUXGI_ONLY=1`、`SLM_HYBRID=2`。
- Forward base pass 原条件：`if (StaticLightingMethod == LUXGI_ONLY) skip; else GetPrecomputed`。HYBRID(2) 下 `2==1` 为 **false** → 走 `else` → **进入 `GetPrecomputed`**。
- Forward base pass `ENABLE_SKY_LIGHT=1`（见根因 C），`GetPrecomputed → GetSkyLighting` 真实算出一份天光 `GetSkySHDiffuse(N) * SkyLightColor`，累加进 `TotalLight`。
- 而 `AccumulateLuxGILighting` 内部的 fake-global 又用 **同一个** `GetSkySHDiffuse(N) * SkyColorM` 算了一份天光。
- → **同一份天光被 base + LuxGI 各算一遍**，叠加 ≈ 2×。Deferred base pass `ENABLE_SKY_LIGHT=0` 天光=0，只有 LuxGI 一份。

### 根因 C：为何 Deferred base pass 天光为 0 —— 真正的开关是 `ENABLE_SKY_LIGHT`，不是 `STATIC_LIGHTING_LUXGI_ONLY`

- `STATIC_LIGHTING_LUXGI_ONLY` 只是**编译期常量 `#define ... 1`**（`MobileBasePassPixelShader.usf:41-43`），Forward/Deferred 都有它，仅用于与运行时 uniform `View.StaticLightingMethod` 做**运行时比较**（`BRANCH`），不是 permutation 开关。
- 真正区分是 `ENABLE_SKY_LIGHT`（`MobileBasePassRendering.h:497`）：
  ```cpp
  OutEnvironment.SetDefine(TEXT("ENABLE_SKY_LIGHT"), bIsLit && bForwardShading && bProjectSupportsNonStaticSkyLights);
  ```
  - Forward base pass：`bForwardShading=true` → `ENABLE_SKY_LIGHT=1` → `GetSkyLighting` 真实算。
  - Deferred base pass：`bForwardShading=false` → `ENABLE_SKY_LIGHT=0` → `GetSkyLighting` 的 `#if ENABLE_SKY_LIGHT` 块被剔除 → 返回 0。
- Deferred 的天光在 LightingPass 独立 permutation（`MobileDeferredShadingPass.cpp:210 FEnableSkyLight`）里算，且实际由 LuxGI 承载。

---

## 三、详细技术原理

### 3.1 两条管线的 PreExposure / 间接光对照（草地 default-lit，HYBRID）

| 光照项 | Forward 内部×Pre | Forward 末尾×Pre | 修复前合计 | Deferred 合计 |
|---|---|---|---|---|
| base 间接漫反射(SkyLight) | — | ✓ | ×Pre（**且多这一份**）| **无此份** |
| 方向光 / Local | — | ✓ | ×Pre | ×Pre |
| LuxGI | ✓ | ✓ | **×Pre²**（double）| ×Pre |
| IBL 反射 | — | ✓ | ×Pre | ×Pre |

### 3.2 天光"两份"引入链

**第 ① 份（Forward 独有，多出的亮度）：**
```
bForwardShading=true → ENABLE_SKY_LIGHT=1
  → GetPrecomputedIndirectLightingAndSkyLight()  [HYBRID 下走 else 分支]
    → GetSkyLighting(): SkyDiffuse = GetSkySHDiffuseSimple(N) × SkyLightColor
      → DiffuseIndirectLighting += SkyDiffuse
        → LightAccumulator_AddSplit(DirectLighting, DiffuseColor)  // 累加进 TotalLight
```

**第 ② 份（Forward + Deferred 都有，正确的那份）：**
```
StaticLightingMethod != 0 (HYBRID/LUXGI_ONLY)
  → AccumulateLuxGILighting()
    → GetLuxGIFullLighting…(): GlobalLux = GetSkySHDiffuse(N) × SkyColorM   (SkyColorM = SkyLightColor × SkyLightIntensityScale)
      → 累加进 TotalLight（内部已 ×PreExposure）
```
①②同源（同一天光 SH、同一法线），故为重复。

### 3.3 HYBRID=2 代入各条件的真值表（解释"为何修复后对齐"）

| 情形 | 实际编译的条件 | 代入 SLM=2 | 真值 | 分支 | 进 GetPrecomputed? | base 天光 |
|---|---|---|---|---|---|---|
| Forward 修复前（`==LUXGI_ONLY`）| `2 == 1` | false | else | ✅ 进 | 有（ENABLE_SKY_LIGHT=1）→ 偏亮 |
| Forward 修复后（`!=LIGHTMAP_ONLY`）| `2 != 0` | true | if(空) | ❌ 跳过 | 无 → 对齐 |
| Deferred（`==LUXGI_ONLY`）| `2 == 1` | false | else | ✅ 进 | ENABLE_SKY_LIGHT=0 → 天光=0 → 无 |

要点：Deferred "进了 GetPrecomputed 但算出 0"，Forward 修复后"直接不进"，两者结果一致（base 无天光）。

---

## 四、修复方案

全部改动在 `MobileBasePassPixelShader.usf`，用 `#pragma region Engine ZXB` 包裹；只影响 Forward（`!MOBILE_USE_GBUFFER`），Deferred 分支保持原逻辑。

### 修复 A：剥离 LuxGI，使其不受末尾 PreExposure（消除 double-Pre）

三段，编译条件完全一致：
`#if (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER`

1. **方向光后**（快照，此刻 TotalLight = 间接 + 方向光）：
   ```hlsl
   half3 ExposureAffectedLight_ZXB = DirectLighting.TotalLight;
   ```
2. **LuxGI 之后、IBL 之前**（剥离 LuxGI 净增量，TotalLight 回退）：
   ```hlsl
   half3 LuxGIExtracted_ZXB = DirectLighting.TotalLight - ExposureAffectedLight_ZXB;
   DirectLighting.TotalLight = ExposureAffectedLight_ZXB;
   ```
   > 剥离点**必须在 IBL 之前**：IBL(`AccumulateReflection`)内部不乘 Pre，需保留在受末尾 `×Pre` 的部分。
3. **末尾 `OutColor.rgb *= PreExposure` 之后**（加回，不乘 Pre，仅雾透射）：
   ```hlsl
   OutColor.rgb += LuxGIExtracted_ZXB * VertexFog.a;
   ```

### 修复 B：HYBRID 下 Forward 不累加 base 间接光（消除重复天光）

```hlsl
bool bStaticLightingUseLightmap = false;
BRANCH
#if !MOBILE_USE_GBUFFER
	if (View.StaticLightingMethod != STATIC_LIGHTING_LIGHTMAP_ONLY)   // Forward: 启用 LuxGI(1/2) 即跳过
#else
	if (View.StaticLightingMethod == STATIC_LIGHTING_LUXGI_ONLY)      // Deferred: 保持原逻辑不变
#endif
	{
		// LuxGI 提供间接光，base pass 不累加
	}
	else
	{
		bStaticLightingUseLightmap = GetPrecomputedIndirectLightingAndSkyLight(...);
	}
```

### 方案对比 / 关键取舍

| 决策点 | 采用做法 | 原因 / 优点 | 风险与规避 |
|---|---|---|---|
| 只剥离 LuxGI 还是含 IBL | 只剥离 LuxGI | IBL 内部不乘 Pe，剥离会让它少乘一次变暗 | 剥离点放 IBL 之前 |
| 修复 B 是否加 `MOBILE_USE_GBUFFER` 分流 | 加，Deferred 走原 `==LUXGI_ONLY` | 保证 `bStaticLightingUseLightmap→CustomData.b→bHasLightmapData` 编码链对 Deferred 零改动 | 不加会改到有 lightmap 物体的 Deferred 行为 |
| 是否改 `ENABLE_SKY_LIGHT`（C++） | 不改，改 shader 条件绕过 | C++ 改动面大、牵动 permutation | shader 内跳过 GetPrecomputed，效果等价 |

### 对 Deferred 的影响 & unbound 评估
- 修复 A 三段都含 `!MOBILE_USE_GBUFFER` → Deferred base pass **整段不编译**。
- 修复 B 的 `#else` 分支 = 原始代码，Deferred base pass 运行时行为一字不变。
- 两处只用既有变量/函数（`TotalLight`/`VertexFog.a`/`PreExposure`/`GetPrecomputed`），**未引入任何新 uniform/texture 绑定** → 不会产生 unbound。`read_lints` 0 错误。

---

## 五、快速排查 Checklist

- [ ] 现象：某管线（Forward）整体偏亮 → 先确认 `View.PreExposure` 值（本例 2.3576）。
- [ ] 用**截帧编译后 shader**（宏已展开）逐项对比，不要只看源码宏分支。
- [ ] 检查 LuxGI/间接光函数**内部**是否已 `*= PreExposure`，末尾是否又乘一次 → double-Pre。
- [ ] 检查反射函数内部是否乘 Pre（本例 `AccumulateReflection` 不乘）→ 决定剥离边界。
- [ ] 确认 `r.Mobile.StaticLightingMethod` 实际值（默认 **2=HYBRID**，Config 常不覆盖）。
- [ ] 确认 base pass `ENABLE_SKY_LIGHT`：Forward=1 / Deferred=0（`bForwardShading` 驱动）。
- [ ] 区分"编译期常量宏"（`STATIC_LIGHTING_LUXGI_ONLY=1`）与"运行时 uniform"（`View.StaticLightingMethod`）与"permutation 宏"（`ENABLE_SKY_LIGHT`）。
- [ ] `.usf` 改动必须**重编 shader**（`recompileshaders changed` 或重启编辑器）才生效；`recompile_material`/Python 不生效。
- [ ] Deferred permutation（`MOBILE_USE_GBUFFER=1`）编译验证：跑 Android cook，查 `Found unbound parameters` / `not bound!`。

---

## 六、相关代码位置与参考

### 关键代码位置（UE5EA）
- `Engine/Shaders/Private/MobileBasePassPixelShader.usf`
  - `41-43` `STATIC_LIGHTING_LUXGI_ONLY` 常量定义
  - `239-262` `GetSkyLighting`（`#if ENABLE_SKY_LIGHT`）
  - `266-355` `GetPrecomputedIndirectLightingAndSkyLight`
  - `984-999` 修复 B（base 间接光条件分流）
  - LuxGI 剥离三段（修复 A，`ExposureAffectedLight_ZXB` / `LuxGIExtracted_ZXB`）
- `Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.h:494-498` —— `ENABLE_SKY_LIGHT` 的 `SetDefine`（含 `bForwardShading`）
- `Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp:207-213` —— LightingPass 的 `ENABLE_SKY_LIGHT` 等 permutation
- `Engine/Source/Runtime/Renderer/Private/LuxMobileGI/LuxGIRendering.cpp:84-89` —— `r.Mobile.StaticLightingMethod` CVar（默认 2）
- `Engine/Source/Runtime/Renderer/Private/LuxMobileGI/LuxGIRendering.h:470-472` —— `SLM_LIGHTMAP_ONLY/LUXGI_ONLY/HYBRID = 0/1/2`
- `Engine/Source/Runtime/Renderer/Private/SceneRendering.cpp:1485-1502` —— `StaticLightingMethod` 传入 View uniform
- `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp:1113-1117` —— HYBRID 下 `bUseLightmap` 判定

### 概念澄清（易错点）
| 名称 | 类别 | 说明 |
|---|---|---|
| `STATIC_LIGHTING_LUXGI_ONLY` | 编译期常量 = 1 | 两条路径都有，仅用于与运行时值比较 |
| `View.StaticLightingMethod` | 运行时 uniform | = CVar 值（默认 2 HYBRID）|
| `ENABLE_SKY_LIGHT` | permutation 宏 | 真正区分 Forward(1)/Deferred base(0) 是否算天光 |
| `MOBILE_USE_GBUFFER` | permutation 宏 | 区分 Forward(0) / Deferred base pass(1) |

### 验证方式
- 手动触发材质刷新（如 `M_FoliageBillboard`）或 `recompileshaders changed` 重编。
- Android cook：`UnrealEditor-Cmd.exe "S1Game.uproject" -run=Cook -TargetPlatform=Android_ASTC -Map=... -unattended -nullrhi`（Idle 优先级 + `-ini:Engine:[DevOptions.Shaders]:NumUnusedShaderCompilingThreads=3`），日志出现 `All required shaders are compiled.` 且无 `Found unbound parameters` 即通过。
