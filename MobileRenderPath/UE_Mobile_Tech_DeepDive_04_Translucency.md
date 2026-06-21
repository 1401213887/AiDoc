# UE Mobile Forward vs Deferred —— 深度补充 04：半透明 / SingleLayerWater / Substrate

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**半透明 MeshPass 体系、SeparateTranslucency、SimpleOIT、SingleLayerWater、Substrate Mobile** 在双管线下的差异。

---

## 1. 移动端半透明 Pass 体系

源码：`MobileBasePass.cpp:1330-1336`

```cpp
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyAllPass,
    CreateMobileTranslucencyAllPassProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyAll, EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyStandardPass,
    CreateMobileTranslucencyStandardPassProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyStandard, EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyAfterDOFPass,
    CreateMobileTranslucencyAfterDOFProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyAfterDOF, EMeshPassFlags::MainView);

// Skipping EMeshPass::TranslucencyAfterDOFModulate because dual blending is not supported on mobile
// Skipping EMeshPass::TranslucencyHoldout, it is not supported on mobile.
```

| MeshPass | 移动端 | PC Deferred | 备注 |
|---------|-------|-------------|------|
| `TranslucencyAll` | ✅ | ✅ | 总入口 |
| `TranslucencyStandard` | ✅ | ✅ | 主流 |
| `TranslucencyStandardModulate` | ❌ | ✅ | Dual src 不支持 |
| `TranslucencyAfterDOF` | ✅ | ✅ | DOF 后 |
| `TranslucencyAfterDOFDrawDepth` | ✅（项目） | ✅ | 写深度的半透明 |
| `TranslucencyAfterDOFModulate` | ❌ | ✅ | 同上 |
| `TranslucencyHoldout` | ❌ | ✅ | – |
| `TranslucencySimpleOIT` | ✅（项目） | ✅ | OIT 简化版 |

---

## 2. `FMobileSceneRenderer::RenderTranslucency` —— 极简实现

源码：`MobileTranslucentRendering.cpp`（仅 22 行！）

```cpp
void FMobileSceneRenderer::RenderTranslucency(FRHICommandList& RHICmdList, const FViewInfo& View)
{
    const bool bShouldRenderTranslucency = ShouldRenderTranslucency(StandardTranslucencyPass)
                                        && ViewFamily.EngineShowFlags.Translucency;
    if (bShouldRenderTranslucency) {
        RHICmdList.SetViewport(View.ViewRect.Min.X, View.ViewRect.Min.Y, 0.0f,
                               View.ViewRect.Max.X, View.ViewRect.Max.Y, 1.0f);
        View.ParallelMeshDrawCommandPasses[StandardTranslucencyMeshPass]
            .Draw(RHICmdList, &TranslucencyInstanceCullingDrawParams);
    }
}
```

> 对比 PC Deferred 的 `RenderTranslucency`（~200 行，包含 LightingVolume、SeparateTranslucency RT 拆分、Distortion 集成等），**移动端把所有半透明都压成一句 MDC Draw**。复杂逻辑通过 MeshPass 的 PSO 与 MDC 预先 cache 解决。

### StandardTranslucencyPass 选择

```cpp
// MobileShadingRenderer.cpp:352-353
StandardTranslucencyPass = ViewFamily.AllowTranslucencyAfterDOF()
    ? ETranslucencyPass::TPT_TranslucencyStandard
    : ETranslucencyPass::TPT_AllTranslucency;
StandardTranslucencyMeshPass = TranslucencyPassToMeshPass(StandardTranslucencyPass);
```

> 是否拆 AfterDOF 取决于 ViewFamily，与管线无关。

---

## 3. 半透明 Shader Permutation 的根本判定

```cpp
// MobileBasePassRendering.h:415
const bool bIsLit                       = ShadingModels.IsLit();
const bool bDeferredShadingEnabled      = IsMobileDeferredShadingEnabled(Platform);
const bool bIsTranslucent               = IsTranslucentBlendMode(...) || HasShadingModel(MSM_SingleLayerWater);
const bool bIsToonCharacter             = HasAnyShadingModel({MSM_ToonStandard, ...});
const bool bMaterialUsesForwardShading  = bIsLit && (bIsTranslucent || bIsToonCharacter);
const bool bForwardShading              = !bDeferredShadingEnabled || bMaterialUsesForwardShading;
```

> **半透明 + SingleLayerWater + Toon Character 这三类材质，在 Deferred 路径下也走 Forward Shading**。这是为什么 BasePass 的 LocalLightSetting 在半透明 Pass 中也生效。

---

## 4. 半透明 Subpass 集成（双路径关键差异）

### 4.1 Forward Single Pass

```cpp
// MobileShadingRenderer.cpp:1971-1979
RHICmdList.NextSubpass();
RenderDecals(...);
RenderModulatedShadowProjections(...);
RenderFog(RHICmdList, View);
RenderTranslucency(RHICmdList, View);  // ← DepthReadSubpass
```

- 半透明 PS 通过 `IS_MOBILE_DEPTHREAD_SUBPASS = 1` 可以 framebuffer-fetch 读 SceneDepth
- SubpassHint = `DepthReadSubpass`

### 4.2 Deferred Single Pass

```cpp
// MobileShadingRenderer.cpp:2354-2367
RHICmdList.NextSubpass();              // Subpass 2: Lighting
MobileDeferredShadingPass(...);
if (bUsingPixelLocalStorage) MobileDeferredCopyBuffer<PLSPS>(...);
RenderFog(...);
if (bUseMobileCharacterForwardPass) RenderCharacterForward(...);
RenderTranslucency(RHICmdList, View);  // ← DeferredShadingSubpass
```

- 半透明 PS 通过 `IS_MOBILE_DEFERREDSHADING_SUBPASS = 1` 可以 framebuffer-fetch 读 **SceneColor + GBuffer + SceneDepth**
- SubpassHint = `DeferredShadingSubpass`
- 注意：FBF 读 GBuffer 仅在 Vulkan/Metal/PLS 平台可用

### 4.3 共享代码：`MobileBasePassPixelShader.usf` 的半透分支

```hlsl
// MobileBasePassPixelShader.usf:117
#define DEFERRED_SHADING_PATH (MOBILE_DEFERRED_SHADING && (SOLID|MASKED) && !SLW && !Toon)

// 半透明永远不走 MOBILE_USE_GBUFFER 分支（看条件 SOLID|MASKED 排除半透）
// 因此 #if MOBILE_USE_GBUFFER ... #else 半透明永远进 else 分支 → Forward 着色
```

---

## 5. SeparateTranslucency（项目 S1 优化）

源码：`PostProcess/PostProcessing.cpp:2710 / 2828 / 2842`

```cpp
FRDGTextureRef SeparateTranslucency = nullptr; // S1:zikuan::Mobile low-res translucency
...
bool bUseSeparateTranslucency = IsMobileSeparateTranslucencyActive(View);
PassSequence.SetEnabled(EPass::SeparateTranslucency, bUseSeparateTranslucency);
```

### 5.1 工作原理

1. 半透明渲染到 **1/2 或 1/4 分辨率** RT
2. 后续 Upscale + Composite 回 SceneColor
3. 大幅减少 Tile 占用 + ALU

### 5.2 启用条件

```cpp
bool IsMobileSeparateTranslucencyActive(View)
{
    // 仅 Forward + MultiPass + r.Mobile.SeparateTranslucency.Method != 0
    // 配合 ETranslucencyPass::TPT_TranslucencyStandard
}
```

| 路径 | 可用 |
|------|------|
| Forward Single | ❌（Subpass 内强制全分辨率） |
| Forward Multi  | ✅ |
| Deferred Single | ❌ |
| Deferred Multi  | ⚠ 受限 |

---

## 6. SimpleOIT（项目实验性）

源码：`MeshDrawCommands.cpp:1435 / TranslucentRendering.cpp:285`

```cpp
case ETranslucencyPass::TPT_TranslucencySimpleOIT:
    TranslucencyMeshPass = EMeshPass::TranslucencySimpleOIT;
    break;
```

```cpp
case EMeshPass::TranslucencySimpleOIT:
    TaskContext.TranslucencyPass = ETranslucencyPass::TPT_TranslucencySimpleOIT;
    break;
```

> SimpleOIT = Order Independent Transparency 的移动端简化实现。通常采用 Weighted Blended OIT，1 个 Color + 1 个 Revealage RT。
>
> 与 PC Deferred 路径共享 MeshPass，移动端 GR 项目接入。需要额外 RT 占用 Tile。

---

## 7. TranslucencyAfterDOFDrawDepth（项目 Mega 改造）

```cpp
case ETranslucencyPass::TPT_TranslucencyAfterDOFDrawDepth:
    TranslucencyMeshPass = EMeshPass::TranslucencyAfterDOFDrawDepth;
    break;
```

> 写深度的半透明。常规半透明 BlendMode=Translucent 不写 Z；该 Pass 让特定半透物体（如 Glass、Hair Card）写入 Depth Buffer，便于后续 Pass（Decal、PostProcess）读取。

| 应用场景 | 例子 |
|---------|------|
| 玻璃 | 后处理需要"玻璃前的深度" |
| 头发卡片 | 让其他半透阻挡 |
| 卡通描边 | 描边采样深度做轮廓 |

---

## 8. SingleLayerWater 移动端

源码：`SingleLayerWaterRendering.cpp`

### 8.1 关键事实

```cpp
// SingleLayerWaterRendering.cpp:555 / 1200
FSingleLayerWaterPrePassResult* FDeferredShadingSceneRenderer::RenderSingleLayerWaterDepthPrepass(...);
void FDeferredShadingSceneRenderer::RenderSingleLayerWater(...);
```

> 注意：这两个函数都是 **`FDeferredShadingSceneRenderer`** 的成员！**Mobile 上没有专门的 `FMobileSceneRenderer::RenderSingleLayerWater`**。

### 8.2 Mobile SingleLayerWater 走半透明 Pass

```cpp
// MobileBasePass.cpp:834
const bool bIsTranslucent = IsTranslucentBlendMode(BlendMode)
                          || ShadingModels.HasShadingModel(MSM_SingleLayerWater);
```

> SingleLayerWater 在移动端被当作**半透明材质**处理，进 `EMeshPass::TranslucencyStandard`，BasePass PS 中通过 `#if MATERIAL_SHADINGMODEL_SINGLELAYERWATER` 走单层水分支。

```hlsl
// MobileBasePassPixelShader.usf:79-84
#if MATERIAL_SHADINGMODEL_SINGLELAYERWATER
    #ifdef SINGLE_LAYER_WATER_SHADING_QUALITY
    #undef SINGLE_LAYER_WATER_SHADING_QUALITY
    #endif
    // Value must match SINGLE_LAYER_WATER_SHADING_QUALITY_MOBILE_WITH_DEPTH_BUFFER
    #define SINGLE_LAYER_WATER_SHADING_QUALITY 2
#endif
```

### 8.3 SceneWithoutWater 深度提供

```cpp
// TranslucentRendering.cpp:1527-1555
const bool bHasValidSceneDepthWithoutWater = GTranslucencyDepthWithoutSLW
                                          && SceneWithoutWaterTextures
                                          && SceneWithoutWaterTextures->DepthTexture;

BasePassParameters.bSceneDepthWithoutWaterTextureAvailable = bHasValidSceneDepthWithoutWater ? 1 : 0;
BasePassParameters.SceneDepthWithoutSingleLayerWaterSampler =
    bShouldUseBilinearSamplerForDepth ? TStaticSamplerState<SF_Bilinear>::GetRHI()
                                      : TStaticSamplerState<SF_Point>::GetRHI();
BasePassParameters.SceneDepthWithoutSingleLayerWaterTexture =
    FRDGSystemTextures::Get(GraphBuilder).Black;
BasePassParameters.SceneWithoutSingleLayerWaterMinMaxUV = FVector4f(0.0f, 0.0f, 1.0f, 1.0f);
```

> 单层水着色需要"水下场景的深度"做散射估计。项目 Mega 的改造让透明物体也能使用"水下深度"——SingleLayerWater + 普通半透明共享同一张 SceneDepthWithoutSLW。

### 8.4 水的着色路径

```hlsl
// 在 MobileBasePassPixelShader.usf
#if MATERIAL_SHADINGMODEL_SINGLELAYERWATER
    #include "SingleLayerWaterShading.ush"
    ...
    // BaseColor 用 BaseMaterialCoverageOverWater 衰减
    GBuffer.DiffuseColor *= BaseMaterialCoverageOverWater;
    DiffuseColor *= BaseMaterialCoverageOverWater;
    DiffuseColorForIndirect *= BaseMaterialCoverageOverWater;
#endif
```

---

## 9. Substrate Mobile（5.5 实验特性）

源码：`MobileBasePassPixelShader.usf:170-178`

```hlsl
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

### 9.1 Substrate Mobile 的两个角色

| 宏 | 路径 |
|----|------|
| `SUBSTRATE_FORWARD_SHADING` | 不透明 Substrate + Forward 路径 |
| `SUBSTRATE_TRANSLUCENT_FORWARD` | 半透 Substrate + Forward 着色（Deferred 主路径下半透也走这里） |
| `MATERIAL_SUBSTRATE_OPAQUE_PRECOMPUTED_LIGHTING` | 烘焙静态光照 |
| `SUBSTRATE_MATERIAL_EXPORT_EXECUTED` | Export 验证 |

### 9.2 SubstrateMobileForwardLighting

源码：`Engine/Shaders/Private/Substrate/SubstrateMobileForwardLighting.ush:40`

```hlsl
float3 SubstrateMobileForwardLighting(
    uint EyeIndex,
    float4 SvPosition,
    ...);
```

> 把多层 Substrate Slab 合成一个 forward direct lighting 结果。**移动端 Substrate 是 forward only**，Deferred 路径下 Substrate 不写 GBuffer（GBuffer 没有 Substrate 槽位）。

### 9.3 Substrate Mobile Pass UB

```cpp
// MobileBasePassRendering.h:54
SHADER_PARAMETER_STRUCT(FSubstrateMobileForwardPassUniformParameters, Substrate)
```

> 独立 UB 而不是 SceneTextures 一员，因为 Substrate 数据量大。

---

## 10. 半透明 BlendMode 矩阵

```hlsl
// MobileBasePassPixelShader.usf:187-225 FrameBufferBlendOp
half3 FrameBufferBlendOp(half4 Source) {
    half4 Dest = half4(0,0,0,0);
#if MATERIALBLENDING_SOLID            return Source.rgb;
#elif MATERIALBLENDING_MASKED         return Source.rgb;
#elif MATERIALBLENDING_ALPHACOMPOSITE return Source.rgb + (Dest.rgb*(1.0 - Source.a));
#elif MATERIALBLENDING_ALPHAHOLDOUT   return Source.rgb;  // mobile 兼容
#elif MATERIALBLENDING_TRANSLUCENT    return Source.rgb * Source.a + Dest.rgb*(1.0 - Source.a);
#elif MATERIALBLENDING_ADDITIVE       return Source.rgb + Dest.rgb;
#elif MATERIALBLENDING_MODULATE       return Source.rgb * Dest.rgb;
#endif
}
```

> 这是**程序化混合**的 fallback，主要服务于不支持硬件 BlendOp 的特定平台（如 GLES 无 framebuffer fetch 时半透合成）。

### 10.1 ColorTransmittance 三档实现

```cpp
// MobileBasePassRendering.h:510-512
OutEnvironment.SetDefine("MOBILE_TRANSLUCENT_COLOR_TRANSMITTANCE_DUAL_SRC_BLENDING",
    Mode == DUAL_SRC_BLENDING);
OutEnvironment.SetDefine("MOBILE_TRANSLUCENT_COLOR_TRANSMITTANCE_PROGRAMMABLE_BLENDING",
    Mode == PROGRAMMABLE_BLENDING);
OutEnvironment.SetDefine("MOBILE_TRANSLUCENT_COLOR_TRANSMITTANCE_SINGLE_SRC_BLENDING",
    Mode == SINGLE_SRC_BLENDING);
```

| Mode | 实现 | 平台 |
|------|------|------|
| DUAL_SRC_BLENDING | 两个 SV_Target + 硬件 dual src | 需要 GLES_DXC 支持 |
| PROGRAMMABLE_BLENDING | FrameBufferBlendOp | Metal / Vulkan PLS |
| SINGLE_SRC_BLENDING | 单色 + 灰度透明度 | 兜底 |

> ThinTranslucent / SubstrateColoredTransmittance 等高级半透明依赖该机制。

### 10.2 HLSLcc + Dual Src 强制 DXC

```cpp
// MobileBasePassRendering.cpp:271-274
if (bTranslucentMaterial && FDataDrivenShaderPlatformInfo::GetSupportsDxc(Platform)
    && IsHlslccShaderPlatform(Platform)
    && MaterialRequiresColorTransmittanceBlending(MaterialParameters)
    && MobileDefaultTranslucentColorTransmittanceMode(Platform) == DUAL_SRC_BLENDING) {
    OutEnvironment.CompilerFlags.Add(CFLAG_ForceDXC);
}
```

> HLSLcc 编译器不支持 Dual Source Blending，遇到必须用 DXC（DirectX Shader Compiler）。

---

## 11. AfterDOF Translucency 链路

```
SceneColor (BasePass + Lighting + Decal)
  → RenderTranslucency Standard
  → DOF
  → RenderTranslucency AfterDOF (TPT_TranslucencyAfterDOF)
  → SunMerge + Bloom 等
```

### 启用条件

```cpp
// MobileShadingRenderer.cpp:352-353
StandardTranslucencyPass = ViewFamily.AllowTranslucencyAfterDOF()
    ? ETranslucencyPass::TPT_TranslucencyStandard    // 拆 Standard + AfterDOF
    : ETranslucencyPass::TPT_AllTranslucency;         // 合并
```

`ViewFamily.AllowTranslucencyAfterDOF()` 由后处理设置决定。

| 设置 | 行为 |
|------|------|
| `bUseDof = false` | AllTranslucency（一次性渲染） |
| `bUseDof = true && AllowTranslucencyAfterDOF` | Standard + AfterDOF 双 Pass |
| `bUseDof = true && !AllowTranslucencyAfterDOF` | 半透明全部在 DOF 前 |

---

## 12. Distortion 半透明的特殊处理

`PostProcess/MobileDistortionRendering.cpp`（未列出但相关）

- Mobile Distortion 是一种特殊半透明：**写 UV 偏移到 DistortionAccumulate RT 而不是 SceneColor**
- Distortion 材质走单独的 MeshPass，不在 EMeshPass::Translucency*
- 与普通半透明的排序需要项目自定义（视情况而定）

### 双路径调度差异

```
Forward Single:
  Subpass 1: Decals → Fog → RenderTranslucency (含 Distortion?)

Deferred Single:
  Subpass 2: Lighting → Fog → RenderTranslucency → Distortion (后处理阶段)
```

> Forward 路径下 Distortion 可以挂到 Translucency 之前/中，省一个 Pass；Deferred 必须独立后处理 Pass。

---

## 13. 半透明 + LuxGI 集成（项目）

```hlsl
// MobileBasePassPixelShader.usf:1115-1146
#if MATERIALBLENDING_ANY_TRANSLUCENT
    if (GBuffer.ShadingModelID == SHADINGMODELID_TOONSTANDARD) {
        // Toon 半透特殊阴影处理
        DirectLighting.TotalLight = lerp(DirectLighting.TotalLight,
            DirectLighting.TotalLight * GBuffer.ShadowColor, ToonShadowMask);
    }
    float ViewDistance = length(View.TranslatedWorldCameraOrigin
                              - MaterialParameters.WorldPosition_CamRelative);
    GetLuxGIFullLightingWithNonCompressedData(
        LWCToFloat(GetWorldPosition(MaterialParameters)),
        GBuffer.WorldNormal, -MaterialParameters.CameraVector,
        false, false, true,
        IndirectDiffuseLighting, SubsurfaceDiffuseLighting,
        RoughReflectionLighting, ViewDistance);
    DirectLighting.TotalLight += (IndirectDiffuseLighting * DiffuseColorForIndirect
                              + SubsurfaceDiffuseLighting * SubsurfaceColor);
#else
    if (View.StaticLightingMethod != STATIC_LIGHTING_LIGHTMAP_ONLY) {
        AccumulateLuxGILighting(...);
    }
#endif
```

> 半透明物体在 Forward 着色中通过 `GetLuxGIFullLightingWithNonCompressedData` 拿 GI 数据；不透明物体在 BasePass 通过 `AccumulateLuxGILighting`（Forward）或 LightingPass（Deferred）。

---

## 14. 半透明 LightGrid 行为

```hlsl
// MobileBasePassPixelShader.usf:1149-1230
float2 LocalPosition = SvPosition.xy - ResolvedView.ViewRectMin.xy;
uint GridIndex = ComputeLightGridCellIndex(uint2(LocalPosition.x, LocalPosition.y), SvPosition.w);

#if MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID
   || TRANSLUCENCY_LIGHTING_SURFACE_FORWARDSHADING
   || TRANSLUCENCY_LIGHTING_SURFACE_LIGHTINGVOLUME
   || MATERIAL_SHADINGMODEL_SINGLELAYERWATER
    AccumulateReflection(GBuffer, ..., GridIndex, DirectLighting);
#endif

#if MERGED_LOCAL_LIGHTS_MOBILE == 1
   // 半透明用 LocalLightTextureA/B
#elif MERGED_LOCAL_LIGHTS_MOBILE == 2
   // 半透明用 LightGrid
#endif
```

> 半透明能否享受 LocalLight：
> - `TRANSLUCENCY_LIGHTING_SURFACE_FORWARDSHADING` ✅
> - `TRANSLUCENCY_LIGHTING_VOLUMETRIC_NONDIRECTIONAL` ❌（依赖体积纹理，移动端不支持）
> - `TRANSLUCENCY_LIGHTING_VOLUMETRIC_DIRECTIONAL` ❌

---

## 15. AsyncPSO 编译 Pass 范围

```cpp
// SceneRendering.cpp:3404-3411
TArray<EMeshPass::Type> PassesWithAsyncPSOSupport;
if (Scene->GetShadingPath() == EShadingPath::Mobile) {
    PassesWithAsyncPSOSupport = {
        EMeshPass::BasePass, EMeshPass::MobileBasePassCSM,
        EMeshPass::TranslucencyAll, EMeshPass::TranslucencyStandard,
        EMeshPass::TranslucencyAfterDOF
    };
} else {
    PassesWithAsyncPSOSupport = {
        EMeshPass::BasePass, EMeshPass::DepthPass, EMeshPass::Velocity,
        EMeshPass::TranslucentVelocity, EMeshPass::TranslucencyAll,
        EMeshPass::TranslucencyStandard, EMeshPass::TranslucencyStandardModulate,
        EMeshPass::TranslucencyAfterDOFModulate, EMeshPass::TranslucencyAfterDOF
    };
}
```

> 移动端 Async PSO 范围更窄（5 个 vs 9 个 Pass），因为 DepthPass / Velocity / Modulate 等在移动端要么不存在要么不支持 Dual Blending。

---

## 16. CVar 速查（半透明 / 水 / Substrate）

| CVar | 默认 | 说明 |
|------|------|------|
| `r.SeparateTranslucency` | 1 | PC Deferred 用 |
| `r.Mobile.SeparateTranslucency.Method` | 0 | S1:zikuan 项目 |
| `r.Water.SingleLayer` | 1 | SingleLayerWater 启用 |
| `r.Water.SingleLayer.DepthPrepass` | 0 | 水深度预 Pass |
| `r.Substrate` | 0 | 实验性 Substrate |
| `r.Substrate.Mobile` | 0 | Substrate Mobile |
| `r.AllowTranslucencyAfterDOF` | 1 | 拆 Standard/AfterDOF |
| `r.AllowStandardTranslucencySeparated` | 0 | 拆 Standard/Modulate（移动端不支持） |
| `r.Mobile.PropagateAlpha` | 0 | 半透 Alpha 传播 |
| `r.Translucency.DepthOnEdge` | – | 边缘深度 |
| `r.Mobile.TranslucencyVolumeShadowSampleCount` | – | 体积阴影 |

---

## 17. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| Deferred 下半透明黑色 | LightingPass 未把 SceneColor 写入 | 检查 LightingPS Blend |
| 半透明 LocalLight 不生效 | MERGED_LOCAL_LIGHTS_MOBILE 没注入 | 检查 LocalLightSetting Permutation |
| SingleLayerWater 水下黑 | bSceneDepthWithoutWaterTextureAvailable=0 | 项目 GR 改造的 SLW 数据准备 |
| ThinTranslucent 颜色错 | DUAL_SRC_BLENDING 未启用 | 检查平台 + DXC |
| Substrate Mobile 编译失败 | Permutation 限制 | 检查 SUBSTRATE_FORWARD_SHADING |
| AfterDOFDrawDepth 不写深度 | MeshPass 未注册 | 检查项目改造 |
| Translucency 排序错 | View.SortedMeshBatches 没生成 | 检查 InitViews |
| DistortionMaterial 在 Deferred 闪烁 | Distortion 必须在 LightingPass 后 | 调整 PostProcess 顺序 |
| Mobile FBF 半透读 GBuffer 失败 | IS_MOBILE_DEFERREDSHADING_SUBPASS=0 | 检查 bIsMobileSeparateTranslucencyEnabled |
| SimpleOIT 颜色失真 | Revealage RT 没正确清零 | 检查项目改造 |

---

> 第 04 篇完。下一篇：**VirtualTexture / LightmapVT / VirtualShadowMap 移动端**。
