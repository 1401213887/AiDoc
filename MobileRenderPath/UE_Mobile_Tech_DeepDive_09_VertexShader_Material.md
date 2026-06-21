# UE Mobile Forward vs Deferred —— 深度补充 09：VertexShader / Material Permutation / Substrate

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**MobileBasePassVertexShader / VertexFactory / Material Permutation / Quality Level / Substrate Mobile** 在双管线下的差异。

---

## 1. MobileBasePassVertexShader 全谱

源码：`MobileBasePassVertexShader.usf` 145 行

### 1.1 输出结构

```hlsl
struct FMobileShadingBasePassVSToPS
{
    FVertexFactoryInterpolantsVSToPS FactoryInterpolants;
    FMobileBasePassInterpolantsVSToPS BasePassInterpolants;
    INVARIANT_OUTPUT float4 Position : SV_POSITION;
};
```

- **FactoryInterpolants**：来自 VertexFactory（StaticMesh/SkeletalMesh/Landscape/Particle 等）
- **BasePassInterpolants**：VS→PS 共享插值器
- **Position**：必带 INVARIANT 标记（多次调用结果一致，避免 Z-fighting）

### 1.2 MultiView 三档

```hlsl
#if INSTANCED_STEREO && MOBILE_MULTI_VIEW
    , out uint LayerIndex : SV_RenderTargetArrayIndex
#elif MOBILE_MULTI_VIEW
    , in uint ViewId : SV_ViewID
#endif
```

| 模式 | 含义 |
|------|------|
| ISR + MobileMultiView | 左右眼用 Layer index 区分 |
| MobileMultiView only | 用 SV_ViewID 指定视图 |
| 单视图 | EyeIndex = 0 |

### 1.3 ResolveView 多视图分发

```hlsl
#if INSTANCED_STEREO && MOBILE_MULTI_VIEW
    const uint EyeIndex = GetEyeIndexFromVF(Input);
    ResolvedView = ResolveView(EyeIndex);
    LayerIndex = EyeIndex;
    Output.BasePassInterpolants.MultiViewId = EyeIndex;
#elif MOBILE_MULTI_VIEW
    const uint EyeIndex = ViewId;
    ResolvedView = ResolveView(ViewId);
    Output.BasePassInterpolants.MultiViewId = ViewId;
#else
    const uint EyeIndex = 0;
    ResolvedView = ResolveView();
#endif
```

---

## 2. WPO（World Position Offset）

```hlsl
FVertexFactoryIntermediates VFIntermediates = GetVertexFactoryIntermediates(Input);
float4 WorldPositionExcludingWPO = VertexFactoryGetWorldPosition(Input, VFIntermediates);
float4 WorldPosition = WorldPositionExcludingWPO;

half3x3 TangentToLocal = VertexFactoryGetTangentToLocal(Input, VFIntermediates);
FMaterialVertexParameters VertexParameters = GetMaterialVertexParameters(Input, VFIntermediates, WorldPosition.xyz, TangentToLocal);

float3 WorldPositionOffset = GetMaterialWorldPositionOffset(VertexParameters);

WorldPosition.xyz += WorldPositionOffset;

float4 RasterizedWorldPosition = VertexFactoryGetRasterizedWorldPosition(Input, VFIntermediates, WorldPosition);
Output.Position = INVARIANT(mul(RasterizedWorldPosition, ResolvedView.TranslatedWorldToClip));
```

### 2.1 WPO 双路径影响

| 路径 | WPO 影响 |
|------|---------|
| Forward | BasePass VS 内执行 |
| Deferred | 同 |
| Shadow Depth Pass | 必须执行（保证阴影对齐） |
| Velocity Pass | 必须执行 |

> 切换 Forward / Deferred 不影响 WPO，但同一材质 WPO 计算会**在多个 Pass 中重复执行**。

### 2.2 USE_WORLD_POSITION_EXCLUDING_SHADER_OFFSETS

```hlsl
#if USE_WORLD_POSITION_EXCLUDING_SHADER_OFFSETS
    Output.BasePassInterpolants.PixelPositionExcludingWPO = WorldPositionExcludingWPO.xyz;
#endif
```

> 一些材质效果（位移贴图、Triplanar）需要"未应用 WPO 的原始位置"，通过该插值器传给 PS。

---

## 3. Vertex Fog 集成（移动端默认）

```hlsl
#if USE_VERTEX_FOG
    half4 VertexFog = CalculateHeightFog(WorldPosition.xyz - ResolvedView.TranslatedWorldCameraOrigin, EyeIndex, ResolvedView);

    #if PROJECT_SUPPORT_SKY_ATMOSPHERE && MATERIAL_IS_SKY==0
        if (ResolvedView.SkyAtmosphereApplyCameraAerialPerspectiveVolume > 0.0f) {
            VertexFog = GetAerialPerspectiveLuminanceTransmittanceWithFogOver(...);
        }
    #endif

    #if LOCAL_FOG_VOLUME_ON_TRANSLUCENT
        // ... 局部体积雾合成
        VertexFog = float4(LFVContribution.rgb + VertexFog.rgb * LFVContribution.a,
                          LFVContribution.a * VertexFog.a);
    #endif

    #if PACK_INTERPOLANTS
        PackedInterps[0] = VertexFog;
    #else
        Output.BasePassInterpolants.VertexFog = VertexFog;
    #endif
#endif
```

### 3.1 USE_VERTEX_FOG 定义

```hlsl
// MobileBasePassCommon.ush:26
#define USE_VERTEX_FOG (!PROJECT_MOBILE_DISABLE_VERTEX_FOG
                    || MATERIAL_IS_SKY
                    || (MATERIAL_ENABLE_TRANSLUCENCY_FOGGING
                        && (MATERIALBLENDING_ANY_TRANSLUCENT || MATERIAL_SHADINGMODEL_SINGLELAYERWATER)))
```

- 项目层 `PROJECT_MOBILE_DISABLE_VERTEX_FOG=1` 时，**仅 Sky 与半透明** 仍走 VS Fog
- 不透明物体走独立 `RenderFog` Pixel Pass

### 3.2 PACK_INTERPOLANTS

```hlsl
// MobileBasePassCommon.ush:31
#define PACK_INTERPOLANTS (USE_VERTEX_FOG && NUM_VF_PACKED_INTERPOLANTS > 0 && (ES3_1_PROFILE))
```

- ES3.1 平台 VS→PS 插值器数量受限（≤16）
- VertexFog 这种"额外插值器"通过打包到 VertexFactory Interpolants 中省空间

### 3.3 LANDSCAPE_BUG_WORKAROUND（iOS）

```hlsl
// MobileBasePassCommon.ush:32
#define LANDSCAPE_BUG_WORKAROUND (IOS && IS_MOBILE_BASEPASS_VERTEX_SHADER && PACK_INTERPOLANTS)
```

```hlsl
// MobileBasePassCommon.ush:61-63
#if LANDSCAPE_BUG_WORKAROUND
    half4 DummyInterp : DUMMY_INTERP;
#endif
```

> iOS DXC 编译器在 Landscape Material 上处理 PackedInterpolants 时索引顺序异常，加 DummyInterp 维持顺序。

---

## 4. FMobileBasePassInterpolantsVSToPS 详解

```hlsl
struct FSharedMobileBasePassInterpolants
{
#if USE_VERTEX_FOG && !PACK_INTERPOLANTS
    #if MOBILE_EMULATION
        float4 VertexFog : TEXCOORD7;   // PC preview 用 float
    #else
        half4 VertexFog : TEXCOORD7;
    #endif
#endif

    float4 PixelPosition : TEXCOORD8;   // xyz=World pos, w=clip z

#if USE_WORLD_POSITION_EXCLUDING_SHADER_OFFSETS
    float3 PixelPositionExcludingWPO : TEXCOORD9;
#endif

#if USE_GLOBAL_CLIP_PLANE
    float OutClipDistance : SV_ClipDistance;
#endif

#if MOBILE_MULTI_VIEW
    nointerpolation uint MultiViewId : VIEW_ID;
#endif

#if LANDSCAPE_BUG_WORKAROUND
    half4 DummyInterp : DUMMY_INTERP;
#endif
};
```

### 4.1 Forward vs Deferred 差异

> 双路径**完全共享同一 Interpolant 结构**。区别仅在 PS 端如何使用：Forward 直接算光照写颜色；Deferred 编码 GBuffer。

---

## 5. UESHADERMETADATA_VERSION 触发重编译

```hlsl
// MobileBasePassCommon.ush:8
#pragma message("UESHADERMETADATA_VERSION C1A9D426-8014-4723-8C69-E32EA8808D15")
```

> 修改 GUID 强制所有 BasePass Shader 重编译。项目改造 BasePass 时需要更新该 GUID。

---

## 6. Material Permutation 维度全谱

材质模板（`MaterialTemplate.ush`）中根据材质属性生成宏：

| 宏类别 | 示例 | 来源 |
|--------|------|------|
| ShadingModel | `MATERIAL_SHADINGMODEL_DEFAULT_LIT` / `_UNLIT` / `_TOON*` 等 | 材质 BP 节点 |
| BlendMode | `MATERIALBLENDING_SOLID` / `_MASKED` / `_TRANSLUCENT` 等 | 材质属性 |
| ShadingMethod | `MATERIAL_FULLY_ROUGH` / `_NONMETAL` | 材质优化标记 |
| TranslucencyLighting | `TRANSLUCENCY_LIGHTING_SURFACE_FORWARDSHADING` 等 | 材质属性 |
| MaterialDomain | `MATERIAL_DOMAIN_SURFACE` / `_LIGHTFUNCTION` / `_POSTPROCESS` | 材质类型 |
| UsedWith | `MATERIAL_USEDWITH_*` | 材质标记 |
| HQ Reflections | `MATERIAL_HQ_FORWARD_REFLECTIONS` | 材质标记 |
| WPO | `WORLD_POSITION_OFFSET` 输出 | 材质 BP |

### 6.1 移动端 ShadingModel 集合（项目扩展）

```hlsl
SHADINGMODELID_UNLIT, _DEFAULTLIT, _SUBSURFACE, _PREINTEGRATED_SKIN, _CLEAR_COAT,
_SUBSURFACE_PROFILE, _TWOSIDED_FOLIAGE, _HAIR, _CLOTH, _EYE,
_SINGLELAYERWATER, _THIN_TRANSLUCENT,
// 项目 GR Toon 扩展
_TOONSTANDARD, _TOONSKIN, _TOONHAIR, _TOONFACE, _TOONEYEBROW, _TOON_ENVIRONMENT
```

| 模型 | Forward | Deferred |
|------|---------|----------|
| Unlit | ✅ | ✅ |
| DefaultLit | ✅ | ✅ |
| Subsurface | ✅ | ✅ |
| PreIntegratedSkin | ✅ | ✅ |
| ClearCoat | ✅ | ✅ |
| SubsurfaceProfile | ⚠ | ✅ |
| TwoSidedFoliage | ✅ | ✅ |
| Hair | ✅ | ✅ |
| Cloth | ✅ | ✅ |
| Eye | ⚠ | ✅ |
| SingleLayerWater | ✅（仅半透） | ✅（仅半透） |
| ThinTranslucent | ✅ | ✅ |
| Toon*（项目） | ✅（角色 Forward） | ⚠（其他 Toon 走 Forward 路径） |

---

## 7. Quality Level Override 系统

源码：`MobileBasePassRendering.cpp:279-294`

```cpp
template<typename LightMapPolicyType>
bool TMobileBasePassPSPolicyParamType<LightMapPolicyType>::ModifyCompilationEnvironmentForQualityLevel(
    EShaderPlatform Platform, EMaterialQualityLevel::Type QualityLevel, FShaderCompilerEnvironment& OutEnvironment)
{
    const UShaderPlatformQualitySettings* MaterialShadingQuality = ...;
    const FMaterialQualityOverrides& QualityOverrides = MaterialShadingQuality->GetQualityOverrides(QualityLevel);

    checkf(QualityOverrides.CanOverride(Platform), ...);
    OutEnvironment.SetDefine(TEXT("MOBILE_QL_FORCE_FULLY_ROUGH"),
        QualityOverrides.bEnableOverride && QualityOverrides.bForceFullyRough != 0 ? 1u : 0u);
    OutEnvironment.SetDefine(TEXT("MOBILE_QL_FORCE_NONMETAL"),
        QualityOverrides.bEnableOverride && QualityOverrides.bForceNonMetal != 0 ? 1u : 0u);
    OutEnvironment.SetDefine(TEXT("QL_FORCEDISABLE_LM_DIRECTIONALITY"),
        QualityOverrides.bEnableOverride && QualityOverrides.bForceDisableLMDirectionality != 0 ? 1u : 0u);
    OutEnvironment.SetDefine(TEXT("MOBILE_QL_FORCE_DISABLE_PREINTEGRATEDGF"),
        QualityOverrides.bEnableOverride && QualityOverrides.bForceDisablePreintegratedGF != 0 ? 1u : 0u);
    OutEnvironment.SetDefine(TEXT("MOBILE_SHADOW_QUALITY"),
        (uint32)QualityOverrides.MobileShadowQuality);
    OutEnvironment.SetDefine(TEXT("MOBILE_QL_DISABLE_MATERIAL_NORMAL"),
        QualityOverrides.bEnableOverride && QualityOverrides.bDisableMaterialNormalCalculation);
    return true;
}
```

### 7.1 Quality Level 影响范围

| 宏 | Forward | Deferred |
|----|---------|----------|
| `MOBILE_QL_FORCE_FULLY_ROUGH` | BasePass PS 跳 IBL | LightingPS 跳 IBL（影响较弱） |
| `MOBILE_QL_FORCE_NONMETAL` | BasePass PS F0 固定 | LightingPS F0 固定 |
| `QL_FORCEDISABLE_LM_DIRECTIONALITY` | Lightmap 简化 | 同 |
| `MOBILE_QL_FORCE_DISABLE_PREINTEGRATEDGF` | BasePass PS 跳 LUT | LightingPS 跳 LUT |
| `MOBILE_SHADOW_QUALITY` | BasePass PS 内 CSM PCF | LightingPS CSM PCF |
| `MOBILE_QL_DISABLE_MATERIAL_NORMAL` | VS 用 VertexNormal | 同 |

### 7.2 Editor 配置位置

`Project Settings → Rendering → Material Quality Level → Shader Platform Settings`

- 4 档：Low / Medium / High / Epic
- 每档独立配置 6 个 bool + 1 个 int
- 编译时按 QualityLevel 注入对应宏

### 7.3 双路径下的相对收益

> **Forward 路径下 Quality Level 收益更高**：BasePass PS 是主要消耗，裁 IBL/F0/Normal 直接减少 60+ 指令。
> **Deferred 路径下 Quality Level 主要影响 LightingPS**：因为 BasePass 只写 GBuffer。

---

## 8. Substrate Mobile 编译条件

源码：`MobileBasePassPixelShader.usf:167-178`

```hlsl
#define MATERIAL_SUBSTRATE_OPAQUE_PRECOMPUTED_LIGHTING 0

#if SUBSTRATE_TRANSLUCENT_FORWARD || SUBSTRATE_FORWARD_SHADING
   || MATERIAL_SUBSTRATE_OPAQUE_PRECOMPUTED_LIGHTING || SUBSTRATE_MATERIAL_EXPORT_EXECUTED
#include "/Engine/Private/Substrate/SubstrateEvaluation.ush"
#endif
#if SUBSTRATE_TRANSLUCENT_FORWARD || SUBSTRATE_FORWARD_SHADING
#include "/Engine/Private/Substrate/SubstrateMobileForwardLighting.ush"
#endif
#if MATERIAL_SUBSTRATE_OPAQUE_PRECOMPUTED_LIGHTING || SUBSTRATE_MATERIAL_EXPORT_EXECUTED
#include "/Engine/Private/Substrate/SubstrateExport.ush"
#endif
```

### 8.1 Substrate Mobile 限制矩阵

| 维度 | Forward | Deferred |
|------|---------|----------|
| Opaque Substrate | ✅（FORWARD_SHADING） | ❌（GBuffer 无 Substrate 槽位） |
| Translucent Substrate | ✅ | ✅（仍走 Forward 着色） |
| Precomputed Lighting | ✅ | ✅ |
| Substrate Export | ⚠ 实验 | ⚠ 实验 |

> Substrate 在 Deferred 主路径下**只能用于半透明**，因为移动 GBuffer 没有 Substrate 信息存储空间。

### 8.2 SubstrateMobileForwardLighting

```hlsl
float3 SubstrateMobileForwardLighting(
    uint EyeIndex,
    float4 SvPosition,
    ...);
```

- 把多层 Slab 合成一次 forward direct lighting
- 比 PC Deferred Substrate Pass 简化 ~50%
- 但移动端 ALU 开销仍然显著

---

## 9. Hair / Cloth / Eye 特殊 ShadingModel

### 9.1 Hair

```hlsl
#if MATERIAL_SHADINGMODEL_HAIR
#ifndef USE_HAIR_COMPLEX_TRANSMITTANCE
#define USE_HAIR_COMPLEX_TRANSMITTANCE 0
#endif
#include "HairStrands/HairStrandsEnvironmentLightingCommon.ush"
#endif
```

```hlsl
if (GBuffer.ShadingModelID == SHADINGMODELID_HAIR) {
    const half3 N = GBuffer.WorldNormal;
    DiffuseColor = EvaluateEnvHair(GBuffer, V, N, SkyDiffuseLookUpNormal);
}
```

- 移动端 Hair 不支持完整 Marschner BSDF（PC Deferred 才完整）
- 使用近似的环境光评估 + 简化 specular

### 9.2 Cloth

```hlsl
if (GBuffer.ShadingModelID == SHADINGMODELID_CLOTH) {
    half3 ClothFuzz = ExtractSubsurfaceColor(GBuffer);
    DiffuseColor += ClothFuzz * GBuffer.CustomData.a;
}
```

- 用 CustomData.a 作为 Cloth 强度
- 简化的 Sheen BRDF（PC 走完整 Charlie Sheen）

### 9.3 TwoSidedFoliage

```hlsl
if (GBuffer.ShadingModelID == SHADINGMODELID_TWOSIDED_FOLIAGE) {
    half3 SubsurfaceLookup = GetSkySHDiffuseSimple(-GBuffer.WorldNormal) * View.SkyLightColor.rgb;
    half3 SubsurfaceColor = ExtractSubsurfaceColor(GBuffer);
    SubsurfaceColor = GBuffer.DiffuseColor;
    ...
    float3 FoliageIndirectLighting = SubsurfaceLookup * GBuffer.DiffuseColor;
    Lighting += FoliageIndirectLighting * FoliageMobileShadowIntensity * GBuffer.FAO;
}
```

项目特化：
- `FoliageMobileShadowIntensity` 全局参数
- `GBuffer.FAO` 树叶专属 AO
- 项目里用 RampColor（ToonLighting）替代

---

## 10. Custom Primitive Data 在 VS 中的使用

```hlsl
// MaterialTemplate.ush
float4 GetPrimitiveCustomDataFloat4(uint OffsetIndex)
{
    return Primitive.CustomPrimitiveData[OffsetIndex];
}
```

- 每个 Primitive 可携带 32 个 float4 自定义数据
- BasePass VS/PS 通过 `MaterialVertexParameters.PrimitiveData` / `MaterialPixelParameters.PrimitiveData` 访问
- 移动端典型用途：动画参数、Material Variant、TeamColor

---

## 11. VertexFactory 类型与 Permutation

移动端常用 VF：

| VertexFactory | 用途 |
|---------------|------|
| FLocalVertexFactory | StaticMesh / SkeletalMesh（不支持 GPUSkinning 时） |
| FGPUSkinVertexFactory | SkeletalMesh GPU 蒙皮 |
| FLandscapeVertexFactory | 地形 |
| FParticleSpriteVertexFactory | 粒子 |
| FParticleMeshVertexFactory | 粒子网格 |
| FNiagaraSpriteVertexFactory | Niagara 粒子 |
| FInstancedStaticMeshVertexFactory | ISM/HISM |
| FMobileLandscapeVertexFactory | 移动端地形优化 |

### 11.1 双路径 VF 编译过滤

```cpp
// VertexFactory::ShouldCompilePermutation
return Material.GetShadingModels().IsLit()
    && !Material.IsUsingFullPrecision()
    && (Mobile || Deferred);  // 多数 VF 双路径都支持
```

> 个别 VF（如 Nanite）仅 Deferred PC 路径编译。

---

## 12. INVARIANT 与 Z-Fighting 避免

```hlsl
Output.Position = INVARIANT(mul(RasterizedWorldPosition, ResolvedView.TranslatedWorldToClip));
```

`INVARIANT()` 宏对应 HLSL `precise`，保证：
- 同一像素位置在不同 Pass 中得到完全相同的 Position
- 避免 PrePass / BasePass / Shadow Pass 间的 Z-fighting

### 12.1 Mobile 特殊性

- 移动 GPU 浮点精度有限（fp16 时差异显著）
- INVARIANT 强制 fp32 路径
- Vertex 数量大的 Mesh 会有性能损失

---

## 13. Substrate Mobile 项目层未启用

```cpp
// MobileBasePassPixelShader.usf:88-91
#if !MATERIAL_IS_SUBSTRATE && SUBSTRATE_ENABLED
#undef SUBSTRATE_ENABLED
#define SUBSTRATE_ENABLED 0
#endif
```

> 项目里多数材质不是 Substrate，因此即使全局 `SUBSTRATE_ENABLED=1`，非 Substrate 材质仍走传统 BRDF。

---

## 14. UE_DF_FORCE_FP32_OPS 精度强制

```hlsl
// MobileBasePassPixelShader.usf:7-9
#if !MATERIAL_LWC_ENABLED
#define UE_DF_FORCE_FP32_OPS 1
#endif
```

- LWC（Large World Coordinates）启用时使用 Double Float 模拟
- 关闭 LWC 时强制 fp32（DF=DoubleFloat 改 fp32）
- 关系到大世界精度问题

---

## 15. CVar 速查（Material / Quality / Vertex）

| CVar | 默认 | 说明 |
|------|------|------|
| `r.MaterialQualityLevel` | 1 | 0=Low,1=Medium,2=High,3=Epic |
| `r.MaterialShadingQuality` | – | Shading 质量 |
| `r.Mobile.DisableVertexFog` | 0 | Vertex Fog 总开关 |
| `r.Mobile.UseHWsRGBEncoding` | 0 | 硬件 sRGB |
| `r.SkyAtmosphereApplyCameraAerialPerspectiveVolume` | 1 | VS 中应用 Aerial |
| `r.Mobile.EnableQualityLevelOverride` | 1 | Quality Level 启用 |
| `r.Mobile.PreviewQualityLevel` | – | Preview 质量档 |
| `r.MobileShadingPath` | 0 | 切换 Forward / Deferred |
| `r.Mobile.AllowDeferredShadingOpenGL` | 0 | GLES 支持 Deferred |
| `r.AllowGlobalClipPlane` | 0 | 全局 ClipPlane（Planar Reflection） |
| `r.MobileMultiView` | 0 | 移动多视图 |
| `r.MaterialEditorUseGameFeatureLevel` | 0 | Editor 用 Game FL |

---

## 16. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| WPO Z-fighting | INVARIANT 不一致 | 确保所有 Pass 用相同 WPO |
| 多视图渲染左右眼一致 | ViewID 没传 | 检查 SV_ViewID 注入 |
| iOS Landscape 闪烁 | LANDSCAPE_BUG_WORKAROUND 未启用 | 检查条件 |
| Vertex Fog 黑边 | half 精度损失 | MOBILE_EMULATION 用 float |
| Quality Level 切换无效 | ModifyCompilationEnvironmentForQualityLevel 未触发 | 重编译材质 |
| ShadingModel 列表过长 | 项目扩展导致 enum 溢出 | 检查 ShadingModelID 位宽 |
| Substrate Mobile 黑屏 | SUBSTRATE_FORWARD_SHADING 未启用 | 检查材质 SubstrateMode |
| TOON 着色错误 | MOBILE_CHARACTER_FORWARD 与 Deferred 冲突 | 检查 DEFERRED_SHADING_PATH 宏 |
| Custom Primitive Data 失效 | Slot 索引超限 | 32 个 vec4 限制 |
| GPUSkinning 错位 | bUseGPUScene 未开 | 启用 GPUScene |

---

> 第 09 篇完。下一篇：**整合总结 + 实战 FAQ + 进阶**。
