# UE Mobile Forward LuxGI 粗糙反射对齐 Deferred — RoughReflection 注入、NaN 爆白与视角排查全记录

> Mobile Forward base pass 缺失 Deferred `MobileDirectionalLightPS` 的 LuxGI 粗糙反射（`RoughReflectionResult × GetEnvBRDF`）直接注入，导致 Forward+LuxGI 开启时地面/岩石丢失偏蓝天空反弹。补齐后某些视角/材质出现大面积纯白爆白，最终根因是 `max(x, 0)` vs `-min(-x, 0)` 在移动 GPU 上对 NaN 的处理差异，以及 `CameraVector` 插值非归一化导致的 LuxGI 探针采样方向偏移。

---

## 一、问题定位流程

### 1.1 现象（分阶段）

**阶段一**：
- Forward + LuxGI 开启：地面/岩石相比 Deferred 丢失偏蓝天空反弹（B −27 > G −17 > R −12）

**阶段二（补齐 LuxRoughSpec 后）**：
- 岩石等低粗糙表面在某些视角大面积纯白爆白
- foliage（TWOSIDED_FOLIAGE）树冠也爆白
- ToonEnvironment（ShadingModelID=7）材质区域大片白色过曝

### 1.2 定位手段

| 手段 | 用途 |
|---|---|
| **shader dump 逐行对拍** | Forward（`ForwardBasePassRockLuxGI.txt` / `ForwardBasePassLuxGIV3.txt`）vs Deferred（`DeferredLightingPassLuxGIV2.txt`）的 `MobileDirectionalLightPS` |
| **二分定位实验** | 临时把 `LuxRoughSpec *= 0.0f` 置零，确认爆白 100% 源自 LuxRoughSpec |
| **可视化调试** | 把 `Luminance(RoughReflectionLighting)` / `Luminance(LuxRoughSpec)` / `Roughness` 写到 `OutColor.rgb`，确认真实数值范围 |
| **debug 输出中间值** | `OutColor.rgb = RoughReflectionLighting` → 过曝区域变黑(0) → 说明该区域是负数或 NaN |
| **编辑器 A/B 截图 + 定量分析** | `r.LuxGI` 开/关同机位对比，Python PIL 算 Luma/P95/过曝像素百分比 |

### 1.3 确认有效的排查步骤（NaN 爆白阶段）

1. **屏蔽整个 LuxGI 四块**（`#if 0` ENABLE_LUX_GI）→ 过曝消失 → 锁定 LuxGI 链路
2. **debug 输出 LuxRoughSpec**（`OutColor.rgb = LuxRoughSpecDebug`）→ 白色区域与过曝区域重合 → 锁定 LuxRoughSpec
3. **debug 输出 GetEnvBRDF 本身** → 深灰色正常值 → 排除 GetEnvBRDF 自身异常
4. **debug 输出 RoughReflectionLighting**（去掉 GetEnvBRDF 乘法）→ 过曝区域变黑色（0）→ RoughReflectionLighting 在该区域是负数或 NaN
5. **对比 Deferred dump 7246-7247 行** → 发现 Deferred 用 `-min(-x, 0)` 而非 `max(x, 0)` → 根因

### 1.4 确认的关键事实

| 事实 | 来源 |
|---|---|
| Deferred `MobileDirectionalLightPS` 非 split 模式总走 `#else`（367-369 行），即 `RoughReflectionResult × GetEnvBRDF` 直接注入 | `MobileDeferredShading.usf:362-370` + dump 7246-7248 |
| Forward `AccumulateReflection → GetImageBasedReflectionLighting_Mobile` 只取 `Luminance(RoughReflection)` 存进 `IndirectIrradiance`，颜色本体丢弃 | `MobileLightingCommon.ush:430` + Forward dump 6857-6876 |
| `IndirectIrradiance` 本应用于 `ComputeMixingWeight`（天空盒 IBL 能量归一化），但被 `#if ALLOW_STATIC_LIGHTING` 剥掉 | `MobileLightingCommon.ush:482-487` |
| GR 引擎把 `ALLOW_STATIC_LIGHTING` **无条件硬编码为 0** | `ShaderCompiler.cpp:3912` + `ShaderGenerationUtil.cpp:348`（`#if 1 // GR_STATIC_LIGHTING(by JLP)`） |
| Deferred 在算 NoV 前先 `normalize(TranslatedWorldPosition)` 重算 CameraVector | `MobileDeferredShading.usf:7203-7205`（dump 7203） |
| Forward base pass 的 `CameraVector` 是 vertex interpolant 插值后的值，某些视角下非归一化 | `Common.ush:1631` `GetCameraVector` 内部 normalize，但插值后失真 |
| **Deferred 用 `-min(-x, 0)` 归零，Forward 用 `max(x, 0)` — 后者在移动 GPU 上 NaN 穿透** | dump 7247 vs Forward dump 8194 |

---

## 二、根因分析

### 2.1 Forward 缺失 LuxGI 粗糙反射（主根因）

**Deferred（`MobileDeferredShading.usf:367-369`，非 split 模式总走 `#else`）**：
```hlsl
RoughReflectionResult *= GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV);
RoughReflectionResult = -min(-RoughReflectionResult, 0.0);  // = max(0,.)
LightAccumulator_AddSplit(DirectLighting, 0.0f, RoughReflectionResult, RoughReflectionResult, 1.0f, false);
```

**Forward（`MobileBasePassPixelShader.usf` 原生）**：
`AccumulateReflection(GBuffer, ..., RoughReflectionLighting, ..., DirectLighting)` → `GetImageBasedReflectionLighting_Mobile`：
```hlsl
half IndirectIrradiance = Luminance(RoughReflection);  // 只取亮度
// ... SpecularIBL 来自天空盒采样，跟 RoughReflection 颜色无关 ...
if (bNormalize) {
    #if ALLOW_STATIC_LIGHTING
    SpecularIBL *= ComputeMixingWeight(IndirectIrradiance, ...);  // 被 ALLOW_STATIC_LIGHTING=0 剥掉
    #endif
}
return SpecularIBL;  // 返回值里没有一丝 RoughReflection 颜色
```

→ Forward 的 LuxGI 粗糙反射（`RoughReflectionLighting`，偏蓝天空反弹）**颜色本体被彻底丢弃**。

### 2.2 ALLOW_STATIC_LIGHTING=0 的连锁影响

GR 引擎在 `ShaderCompiler.cpp:3911-3915` 和 `ShaderGenerationUtil.cpp:347-351` 用 `#if 1 // GR_STATIC_LIGHTING(by JLP)` 把 `ALLOW_STATIC_LIGHTING` **无条件设为 0**：
```cpp
#if 1 // GR_STATIC_LIGHTING(by JLP)
    SET_SHADER_DEFINE(Input.Environment, ALLOW_STATIC_LIGHTING, 0);
#else
    SET_SHADER_DEFINE(Input.Environment, ALLOW_STATIC_LIGHTING, IsStaticLightingAllowed() ? 1 : 0);
#endif
```

这导致 `MobileLightingCommon.ush:484` 的 `ComputeMixingWeight` 永远被 `#if` 剥掉 → `bNormalize` 分支体空 → `IndirectIrradiance`（= `Luminance(RoughReflection)`）完全没被消费。

### 2.3 Deferred 天空盒 IBL 在独立 pass

Deferred 的天空盒 IBL 不在 `MobileDirectionalLightPS`（方向光 pass）里，而在**独立反射 pass** `MobileReflectionEnvironmentSkyLightingPS`（`MobileDeferredShadingPass.cpp:679`），用加法混合（`BO_Add, BF_One, BF_One`）叠加到 SceneColor。

**非 split 模式下 `MobileDirectionalLightPS` 的 362 行 `#if ENABLE_SKY_LIGHT || ...` 全为 0**（`bInlineReflectionAndSky=false`），所以方向光 pass 只走 `#else`（LuxGI 粗糙反射 ×EnvBRDF），天空盒 IBL 在独立 pass。

Forward 的 `AccumulateReflection`（base pass 内）对应 Deferred 独立反射 pass 的天空盒 IBL。**两边都保留 → 两份镜面都有（LuxGI 粗糙反射 + 天空盒 IBL），与 Deferred 逐项对齐。**

### 2.4 NaN 爆白根因：`max(x, 0)` vs `-min(-x, 0)`

**Forward（`ForwardBasePassLuxGIV3.txt:8193-8194`）**：
```hlsl
min16float3 LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV_LuxRR);
LuxRoughSpec = max(LuxRoughSpec, min16float3(0.0f, 0.0f, 0.0f));
```

**Deferred（`DeferredLightingPassLuxGIV2.txt:7246-7247`）**：
```hlsl
RoughReflectionResult *= GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV);
RoughReflectionResult = -min(-RoughReflectionResult, 0.0);
```

#### 数学等价但不等价

数学上 `-min(-x, 0)` ≡ `max(x, 0)`：

| x 值 | max(x, 0) | -min(-x, 0) | 是否一致 |
|---|---|---|---|
| 5.0 | 5.0 | -min(-5, 0) = -(-5) = 5.0 | ✓ |
| -3.0 | 0.0 | -min(3, 0) = -(0) = 0.0 | ✓ |
| 0.0 | 0.0 | -min(0, 0) = 0.0 | ✓ |

#### NaN 处理的关键差异

`RoughReflectionLighting` 在某些像素上是 NaN（来自 GI Volume 探针 SH 反射采样的边界情况）：

| 输入 | `max(NaN, 0)` | `-min(-NaN, 0)` | 差异 |
|---|---|---|---|
| NaN | 未定义（某些移动 GPU 返回 NaN） | `-min(NaN, 0)`，min 返回非 NaN 参数 0，`-0 = 0` | **Forward 穿透，Deferred 归零** |

**D3D12 规范**：`min(a, b)` 和 `max(a, b)` 对 NaN 应返回非 NaN 参数。但**移动 GPU（特别是某些 Mali/Adreno）在 FP16 下不保证此行为**：
- `max(NaN, 0)` 可能返回 NaN
- `-min(-NaN, 0)` = `-min(NaN, 0)`，`min(NaN, 0)` 返回 0（因为 0 是非 NaN 常量参数），`-0 = 0`

Deferred 用 `-min(-x, 0)` 的写法**规避了移动 GPU 的 NaN 穿透问题**。Forward 用 `max(x, 0)` 没有规避，导致 NaN 穿透到 `LuxGIExtracted`，再被加到 `OutColor.rgb`，渲染时显示为白色。

#### NaN 的来源

`RoughReflectionLighting` 来自 `GetLuxGIFullLightingWithNonCompressedData`（dump 7485-7488 调用，7267 输出 `OutRoughReflectionLighting`）。内部经过：

1. `GetFakeGlobalLuxSH(ReflectionDir) * SkyColorM`（dump 7153）— SH 反射初始值
2. `GetIrradianceFromSparseBrickPage`（dump 7241-7246）— GI Volume 探针 Trilinear 采样
3. `lerp(GlobalRoughReflectionValue, RoughReflectionLighting, FadeRatio)`（dump 7263）— 混合

**SH 反射在反射方向接近天顶/天底时 L1 系数可能产生负值，某些边界组合下产生 NaN**（特别是 FP16 精度下 0/0 或 Inf-Inf）。

### 2.5 视角相关爆白：CameraVector 插值失真

Deferred（`MobileDeferredShading.usf:7203-7204`）：
```hlsl
min16float3 CameraVector = normalize(TranslatedWorldPosition);
min16float3 V = -CameraVector;
```

Forward base pass 的 `CameraVector` 来自 `GetCameraVectorFromTranslatedWorldPosition`（`Common.ush:1631`），虽然内部 normalize 过，但**经过 vertex→pixel 插值后可能不再归一化**（大三角形/斜视角下插值失真）。

影响链：
1. `AccumulateLuxGILighting` 传 `V = -CameraVector`（非归一化）→ `GetLuxGIFullLightingWithNonCompressedData` 的探针采样方向偏移 → 某些视角采到更亮天空/太阳 → `RoughReflectionLighting` 爆值
2. LuxRoughSpec 的 `NoV = dot(N, -CameraVector)` 偏离合法余弦值 → `GetEnvBRDF` 返回值异常

---

## 三、详细技术原理

### 3.1 Forward LuxRoughSpec 的完整计算链路（基于 dump）

| 步骤 | dump 行 | 操作 | 说明 |
|---|---|---|---|
| 1 | 7485-7488 | `GetLuxGIFullLightingWithNonCompressedData(...)` → `OutRoughReflectionLighting` | GI Volume 探针采样输出 |
| 2 | 7492 | `RoughReflectionLighting *= View_PreExposure * GBuffer.GBufferAO` | 乘 PreExposure |
| 3 | 7516 | `RoughReflectionLighting *= View_IndirectLightingColorScale` | 乘 IndirectColorScale |
| 4 | 8193 | `LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(...)` | 乘 GetEnvBRDF |
| 5 | 8194 | `LuxRoughSpec = max(LuxRoughSpec, 0)` | **这里 NaN 穿透** |
| 6 | 8195 | `LuxGIExtracted += LuxRoughSpec` | 加到 LuxGIExtracted |
| 7 | 8224 | `OutColor.rgb += LuxGIExtracted * VertexFog.a` | 输出 |

### 3.2 Deferred 的对应链路（基于 dump）

| 步骤 | dump 行 | 操作 | 说明 |
|---|---|---|---|
| 1 | 7243 | `AccumulateLuxGILighting(...)` → `RoughReflectionResult` | 同 Forward，GI Volume 探针采样 |
| 2 | 6586（函数内） | `RoughReflectionLighting *= View_PreExposure * GBuffer.GBufferAO` | 同 Forward |
| 3 | 6610（函数内） | `RoughReflectionLighting *= View_IndirectLightingColorScale` | 同 Forward |
| 4 | 7246 | `RoughReflectionResult *= GetEnvBRDF(...)` | 乘 GetEnvBRDF |
| 5 | 7247 | `RoughReflectionResult = -min(-RoughReflectionResult, 0.0)` | **NaN 正确归零** |
| 6 | 7248 | `LightAccumulator_AddSplit(DirectLighting, 0, RoughReflectionResult, ...)` | 加到 DirectLighting |

---

## 四、修复方案

### 4.1 补齐 LuxGI 粗糙反射注入 + NaN 归零（对齐 Deferred 367-369 + 7247）

**文件**：`UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`

在 `LuxGIExtracted` 剥离块之后、`AccumulateReflection` 调用之前，插入：

```hlsl
#pragma region Engine ZXB
// [ZXB] 对齐 Deferred 的 LuxGI 粗糙反射注入。
// Deferred(MobileDeferredShading.usf:367-369, MobileDirectionalLightPS 非 split 模式总走 #else):
//     RoughReflectionResult *= GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV);
//     RoughReflectionResult = -min(-RoughReflectionResult, 0.0);  // = max(0,.)
//     LightAccumulator_AddSplit(DirectLighting, 0.0f, RoughReflectionResult, RoughReflectionResult, 1.0f, false);
// Forward 的 AccumulateReflection -> GetImageBasedReflectionLighting_Mobile 只对 RoughReflection 取
// Luminance() 存进 IndirectIrradiance(mixing weight 用，且被 ALLOW_STATIC_LIGHTING=0 剥掉)，
// 颜色本体丢弃 → 缺这份 LuxGI 粗糙反射。走 LuxGIExtracted 通道: 与 LuxGI 漫反射同，末尾不被 PreExposure 二次缩放，
// 保持与 Deferred 那三行"位于 TotalLight*=Pre 之后、Color 输出前不再乘 Pre"的同源语义。
// Deferred 的天空盒 IBL 在独立反射 pass(MobileReflectionEnvironmentSkyLightingPS, 加法混合到 SceneColor)，
// Forward 的 AccumulateReflection 对应那份，两边都保留 → 两份镜面都有，与 Deferred 逐项对齐。
#if ENABLE_LUX_GI && (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER
	{
		// [ZXB] 对齐 Deferred(7203-7205): 算 NoV 前先 normalize，避免插值失真。
		half3 V_LuxRR = normalize(-CameraVector);
		half NoV_LuxRR = saturate(abs(dot(GBuffer.WorldNormal, V_LuxRR)) + 1e-5);
		half3 LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV_LuxRR);
		// [ZXB Fix] 对齐 Deferred 的取负操作(Dump 7247: -min(-RoughReflectionResult, 0.0))
		// 而不是用 max(LuxRoughSpec, 0)。某些移动 GPU 上 max(NaN, 0) 返回 NaN 导致爆白，
		// 而 -min(-x, 0) 能正确把 NaN/负值归零。
		LuxRoughSpec = -min(-LuxRoughSpec, half3(0.0f, 0.0f, 0.0f));
		LuxGIExtracted += LuxRoughSpec;
	}
#endif
#pragma endregion
```

**关键点**：
- 用 `-min(-x, 0)` 而非 `max(x, 0)` — 规避移动 GPU NaN 穿透
- `normalize(-CameraVector)` — 对齐 Deferred 7203 的 CameraVector 归一化

### 4.2 AccumulateLuxGILighting 传归一化 V（对齐 Deferred 7203-7204）

```hlsl
// [ZXB] 对齐 Deferred: V = -normalize(TranslatedWorldPosition)。
//   Forward base pass 的 CameraVector 是插值后的，某些视角非归一化，
//   直接传 -CameraVector 给 AccumulateLuxGILighting → LuxGI 探针采样方向偏移 → 爆值。
AccumulateLuxGILighting(GBuffer, MaterialParameters.WorldPosition_CamRelative,
    normalize(-CameraVector),  // ← 关键：normalize
    DynamicShadowFactors, /*DeviceZ*/SvPosition.z,
    MobileSceneTextures.SceneDepthTexture, MobileSceneTextures.SceneDepthTextureSampler,
    RoughReflectionLighting, DirectLighting);
```

### 4.3 保留原生 AccumulateReflection（不跳过天空盒 IBL）

Forward 的 `AccumulateReflection`（天空盒 IBL）**必须保留**——它对应 Deferred 独立反射 pass 的天空盒 IBL。两边都保留 → 两份镜面都有，与 Deferred 逐项对齐。

### 4.4 LuxGI double-PreExposure 剥离（前置修复，保留）

Forward 末尾 `OutColor.rgb *= ResolvedView.PreExposure` 会把 LuxGI（内部已乘一次 Pre）再乘一次。剥离方案：
```hlsl
// 快照（方向光累加后、LuxGI 累加前）
half3 ExposureAffectedLight = DirectLighting.TotalLight;
// ... AccumulateLuxGILighting 累加 ...
// 剥离 LuxGI 净增量
half3 LuxGIExtracted = DirectLighting.TotalLight - ExposureAffectedLight;
DirectLighting.TotalLight = ExposureAffectedLight;
// ... AccumulateReflection 累加 ...
// 末尾：LuxGIExtracted 不乘 Pre 直接加回
OutColor.rgb += LuxGIExtracted * VertexFog.a;
```

---

## 五、关键踩坑：UE5 shader 热替换不可靠

### 5.1 现象

`recompileshaders changed` 在同一编辑器进程内**第一次**修改源码后能正确热替换，**第二次及以后**虽然编译成功进 DDC，但**材质 ShaderMap 不重新绑定**，视口仍用旧 shader 渲染。

### 5.2 可靠的验证方式

| 方法 | 可靠性 | 说明 |
|---|---|---|
| **重启编辑器** | ✅ 100% | 从 DDC 加载最新 shader，无热替换问题 |
| `recompileshaders changed`（首次） | ✅ 可靠 | 全新进程第一次调用 |
| `recompileshaders changed`（非首次） | ❌ 不可靠 | 编译进 DDC 但不绑定 |
| `recompileshaders /Engine/Private/xxx.usf` | ❌ 不可靠 | 同上 |
| `recompileshaders all` | ⚠️ 可能崩编辑器 | Forward preview 模式下曾导致崩溃 |
| 切 `r.LuxGI` 0→1 | ⚠️ 只重建 MDC | 不重编 shader，只切 permutation |

### 5.3 验证闭环

1. 改源码 → `p4 edit`
2. `recompileshaders changed`（首次，等编译完）
3. **重启编辑器**（确保加载最新 shader）
4. `r.LuxGI 1` + 设相机到目标机位
5. 截图（`HighResShot 1` 或 MCP `take_screenshot`）
6. Python PIL 定量分析（Luma/P95/过曝像素%）

---

## 六、Deferred vs Forward 间接镜面对齐速查

| 间接镜面来源 | Deferred | Forward |
|---|---|---|
| LuxGI 粗糙反射 ×EnvBRDF | `MobileDirectionalLightPS` #else（367-369），方向光 pass 内 | **需手动补**（Fix1，走 `LuxGIExtracted` 通道） |
| 天空盒 IBL | `MobileReflectionEnvironmentSkyLightingPS`（独立反射 pass，加法混合） | `AccumulateReflection`（base pass 内，**保留原生**） |
| `RoughReflection` 颜色用途 | `× EnvBRDF` 直接注入（#else）/ `Luminance()` 做 mixing weight（#if 有天空盒，但非 split 不走） | `Luminance()` 做 mixing weight（被 `ALLOW_STATIC_LIGHTING=0` 剥掉）→ 颜色丢弃 |
| NoV 计算 | `NoV = dot(N, V)`，`V = -normalize(TranslatedWorldPosition)`（7203-7205） | 需 `normalize(-CameraVector)` 后算 NoV（对齐 Deferred） |
| 负值/NaN 归零 | `-min(-x, 0)`（7247，移动 GPU 安全） | 需用 `-min(-x, 0)`（对齐 Deferred，**不能用 `max(x, 0)`**） |

---

## 七、快速排查 Checklist

### 7.1 Forward vs Deferred 过曝差异排查

1. **基于 dump 文件逐行对比**（不要飞到 .usf 源文件）
2. **逐行对比每一行**，包括看似"数学等价"的写法（`max` vs `-min(-x)`）
3. **NaN 排查**：如果某值在某个区域是负数或 NaN，`max` 可能穿透
4. **先看 Deferred 怎么处理**，而不是自己发明 NaN 检测方案
5. **debug 输出中间值**：用 `OutColor.rgb = 中间值` 可视化
6. **屏蔽验证**：把可疑块 `#if 0` 屏蔽，看是否消失

### 7.2 NaN 规避写法对照

| 写法 | NaN 行为 | 推荐度 |
|---|---|---|
| `max(x, 0)` | 移动 GPU 可能返回 NaN | ✗ |
| `clamp(x, 0, N)` | 同上 | ✗ |
| **`-min(-x, 0)`** | min 对常量 0 稳定归零 | **✓** |

### 7.3 LuxGI 对齐检查

- [ ] 确认 `r.LuxGI` 值（`r.LuxGI 1` 开启）
- [ ] 确认 `r.Mobile.ShadingPath`（0=Forward，1=Deferred）
- [ ] 确认 `ALLOW_STATIC_LIGHTING` 在 GR 项目恒为 0（`ShaderCompiler.cpp:3912`）
- [ ] 确认 Deferred `MobileDirectionalLightPS` 走 `#else`（非 split 模式，`bInlineReflectionAndSky=false`）
- [ ] 确认 Forward `AccumulateReflection` 保留（对应 Deferred 独立反射 pass）
- [ ] 确认 `AccumulateLuxGILighting` 传 `normalize(-CameraVector)`（对齐 Deferred 7203）
- [ ] 确认 LuxRoughSpec 的 NoV 用 `normalize(-CameraVector)` 算
- [ ] 确认 LuxRoughSpec 用 `-min(-x, 0)` 归零（**不能用 `max(x, 0)`**）
- [ ] 确认 `LuxGIExtracted` 走"末尾不乘 Pre"通道（double-PreExposure 修复）
- [ ] 验证时**重启编辑器**（避免热替换坑）
- [ ] 截图定量分析：过曝像素% 应为 0%，B−R 应为正值（偏蓝反射保留）

---

## 八、相关文件与行号

| 文件 | 关键行 | 内容 |
|---|---|---|
| `UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf` | 362-370 | Deferred `#if ENABLE_SKY_LIGHT \|\| ...` → `#else`（LuxGI 粗糙反射 ×EnvBRDF） |
| `UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf` | 7203-7205 | Deferred `CameraVector = normalize(TranslatedWorldPosition)` |
| `UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf` | 580-629 | `MobileReflectionEnvironmentSkyLightingPS`（独立反射 pass） |
| `UE5EA/Engine/Shaders/Private/MobileLightingCommon.ush` | 421-493 | `GetImageBasedReflectionLighting_Mobile`（RoughReflection 颜色丢弃） |
| `UE5EA/Engine/Shaders/Private/MobileLightingCommon.ush` | 482-487 | `ComputeMixingWeight` 被 `#if ALLOW_STATIC_LIGHTING` 剥掉 |
| `UE5EA/Engine/Shaders/Private/MobileLightingCommon.ush` | 538-628 | `AccumulateReflection`（天空盒 IBL） |
| `UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf` | 1208-1306 | Forward LuxGI 剥离 + 粗糙反射注入 + AccumulateReflection |
| `UE5EA/Engine/Source/Runtime/Engine/Private/ShaderCompiler/ShaderCompiler.cpp` | 3911-3915 | `ALLOW_STATIC_LIGHTING` 硬编码 0 |
| `UE5EA/Engine/Source/Runtime/Engine/Private/ShaderCompiler/ShaderGenerationUtil.cpp` | 347-351 | `ALLOW_STATIC_LIGHTING = false` |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp` | 395-396, 679-706 | `bInlineReflectionAndSky` / 独立反射 pass blend state |
| `UE5EA/Engine/Shaders/Private/ReflectionEnvironmentShared.ush` | 281-295 | `ComputeMixingWeight` 实现 |
| `UE5EA/Engine/Shaders/Private/BRDF.ush` | 689-725 | `EnvBRDFApproxLazarov` / `EnvBRDFApprox` |
| `UE5EA/Engine/Shaders/Private/ShadingModels.ush` | 68-82 | `GetEnvBRDF` |
| `UE5EA/Engine/Shaders/Private/Common.ush` | 1616-1639 | `GetCameraVector`（内部 normalize，但插值后失真） |

---

## 九、涉及的 dump 文件

| dump 文件 | 用途 |
|---|---|
| `DeferredLightingPassLuxGIV2.txt` | Deferred `MobileDirectionalLightPS` shader dump（7246-7248 关键三行） |
| `ForwardBasePassRockLuxGI.txt` | Forward 岩石材质 base pass shader dump |
| `ForwardBasePassFoliageLuxGI.txt` | Forward foliage 材质 base pass shader dump |
| `ForwardBasePassLuxGIV3.txt` | Forward base pass shader dump（8193-8194 NaN 穿透行） |

---

## 十、P4 状态

- **文件**：`UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`
- **P4 workspace**：`DJANGOZHAN-PCFW_GR_DevTest`
- **Depot**：`//GR/DevTest/UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`
- **Base revision**：#18
- **状态**：已 `p4 edit`（default changelist），未提交
- **改动区**：`#pragma region Engine ZXB` 包裹（1208-1306 行附近）
