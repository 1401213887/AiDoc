# UE Mobile Forward vs Deferred —— 深度补充 02：阴影系统全谱

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**ShadowDepthRendering / CSM / OnePassPointLight / SpotLight / ModulatedShadow / ScreenSpaceShadowMask / VSM** 在双管线下的差异。

---

## 1. 阴影系统组件清单

| 子系统 | Forward | Deferred | 实现文件 |
|--------|---------|----------|---------|
| Whole Scene Directional + CSM | ✅ | ✅ | `ShadowDepthRendering.cpp` + `ShadowSetupMobile.cpp` |
| OnePassPointLight 6 面 cubemap 阴影 | ❌（受限） | ✅ | `OnePassPointLightShadowDepthPass` |
| Movable Spot Light Shadow | ⚠（需开） | ✅ | `IsMobileMovableSpotlightShadowsEnabled` |
| Static + CSM 合并 | ✅（核心场景） | ❌ | `MobileEnableStaticAndCSMShadowReceivers` |
| Distance Field Shadow | ✅（受限） | ❌ | `MobileAllowDistanceFieldShadows` |
| Per-Object Shadow | ✅（角色） | ✅ | `RenderShadowProjections` |
| Modulated Shadow | ✅ | ❌ | `RenderModulatedShadowProjections` |
| ScreenSpace Shadow Mask | ✅（用得多） | ✅（特殊场景） | `RenderMobileShadowProjections` |
| Virtual Shadow Map | ⚠ 实验 | ⚠ 实验 | `VirtualShadowMapArray` |
| Capsule Indirect Shadow | ✅ | ✅ | `CapsuleShadows` |
| Contact Shadow | ✅ | ❌ | BasePass 内部 |
| Ray Traced Shadow | ❌ | ❌ | 移动端不支持 |

---

## 2. ShadowDepth Pass Processor 注册（双路径共享）

源码：`ShadowDepthRendering.cpp:3108-3129`

```cpp
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(CSMShadowDepthPass,
    CreateCSMShadowDepthPassProcessor, EShadingPath::Deferred, EMeshPass::CSMShadowDepth, EMeshPassFlags::CachedMeshCommands);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(CSMMobileShadowDepthPass,
    CreateCSMShadowDepthPassProcessor, EShadingPath::Mobile,   EMeshPass::CSMShadowDepth, EMeshPassFlags::CachedMeshCommands);

REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(OnePassPointLightShadowDepthPass,
    CreateOnePassPointLightShadowDepthPassProcessor, EShadingPath::Deferred, ...);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(OnePassPointLightMobileShadowDepthPass,
    CreateOnePassPointLightShadowDepthPassProcessor, EShadingPath::Mobile,   ...);
```

> 双路径都注册了 `CSMShadowDepth` 与 `OnePassPointLightShadowDepth`，但**移动端的 OnePassPointLightShadowDepth 只在 Deferred 路径下真正使用**。

### UB 区别

```cpp
// ShadowDepthRendering.cpp:90-91
IMPLEMENT_STATIC_UNIFORM_BUFFER_STRUCT(FShadowDepthPassUniformParameters,        "ShadowDepthPass",       SceneTextures);
IMPLEMENT_STATIC_UNIFORM_BUFFER_STRUCT(FMobileShadowDepthPassUniformParameters,  "MobileShadowDepthPass", SceneTextures);
```

```cpp
// ShadowDepthRendering.cpp:1365
BEGIN_SHADER_PARAMETER_STRUCT(FShadowDepthPassParameters, )
    SHADER_PARAMETER_STRUCT_REF(FViewUniformShaderParameters, View)
    SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FMobileShadowDepthPassUniformParameters, MobilePassUniformBuffer)
    SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FShadowDepthPassUniformParameters,       DeferredPassUniformBuffer)
    SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVirtualShadowMapUniformParameters,      VirtualShadowMap)
END_SHADER_PARAMETER_STRUCT()
```

> 同一 PassParameters 同时持有两条路径的 UB 引用，但只激活其中一个。

---

## 3. `MobileBasePassAlwaysUsesCSM` 的连锁影响

```cpp
// RenderUtils.cpp:756-768
bool MobileBasePassAlwaysUsesCSM(const FStaticShaderPlatform Platform) {
    if (IsMobileDeferredShadingEnabled(Platform))
        return true;  // ★ Deferred 永远 true

    // Forward 路径下看 r.Mobile.Shadow.CSMShaderCullingMethod 决定
    return CVar value ...;
}
```

```cpp
// ShadowSetupMobile.cpp:308-316
const bool bSkipCSMShaderCulling = MobileBasePassAlwaysUsesCSM(Scene->GetShaderPlatform());

if (bSkipCSMShaderCulling) {
    bAlwaysUseCSM = true;
}
```

> **Deferred 路径下 bSkipCSMShaderCulling=true** → 完全跳过下面 `EnableStaticMeshCSMVisibilityState` 这套复杂的 CSM 可见性筛选。所有 mesh 默认作为 CSM Receiver。

> **Forward 路径下**才有真正的 CSM 物体级筛选：
> - `MobileCSMVisibilityInfo.MobilePrimitiveCSMReceiverVisibilityMap` 标记哪些图元在 CSM 阴影范围内
> - `MobileCSMStaticMeshVisibilityMap` 与 `MobileNonCSMStaticMeshVisibilityMap` 互斥两张可见性表
> - BasePass 时按可见性表分发到 `EMeshPass::BasePass`（NonCSM）或 `EMeshPass::MobileBasePassCSM`（CSM）

这就是为什么 **Forward 路径下 BasePass Shader Permutation 倍数高**：每个材质需要编译两份（带 CSM / 不带 CSM）。

### CSMShaderCullingMethod CVar 值含义（ShadowSetupMobile.cpp:603 case 4 等）

| 值 | 含义 |
|----|------|
| 0 | 不剔除，永远当成所有物体接收 CSM（同 Deferred） |
| 1 | 按图元 Bounds 剔除（默认） |
| 2 | 按 Light Subject Primitives 剔除（精确） |
| 3 | Composition 模式 |
| 4 | 完全关闭 CSM |

---

## 4. ScreenSpaceShadowMaskTexture 生命周期

```cpp
// MobileShadingRenderer.cpp:794-806
if (bRequiresShadowProjections) {
    FViewInfo* MainView = Views.Num() > 0 ? &Views[0] : nullptr;
    bool bIsMobileMultiView = SceneTexturesConfig.bRequireMultiView
                            || (MainView && MainView->Aspects.IsMobileMultiViewEnabled());
    InitMobileShadowProjectionOutputs(RHICmdList, SceneTexturesConfig.Extent, bIsMobileMultiView);
} else {
    ReleaseMobileShadowProjectionOutputs();
}
```

### 生成

```cpp
// MobileShadingRenderer.cpp:1521-1527
if (bRequiresShadowProjections) {
    RDG_EVENT_SCOPE_STAT(GraphBuilder, ShadowProjection, "ShadowProjection");
    RenderMobileShadowProjections(GraphBuilder);   // → ScreenSpaceShadowMaskTextureMobile
}
```

### 消费

| 路径 | 消费位置 |
|------|---------|
| Forward | BasePass PS 的 `MobileBasePass.ScreenSpaceShadowMaskTexture` |
| Deferred | LightingPass `FMobileDirectionalLightFunctionPS.ScreenSpaceShadowMaskTexture` |

### 生命周期决策

```cpp
// MobileShadingRenderer.cpp:687
bRequiresShadowProjections = MobileUsesShadowMaskTextureRuntime(ShaderPlatform)
    && ViewFamily.EngineShowFlags.Lighting
    && !Views[0].bIsReflectionCapture
    && !Views[0].bIsPlanarReflection
    && !ViewFamily.EngineShowFlags.HitProxies
    && !ViewFamily.EngineShowFlags.VisualizeLightCulling
    && !ViewFamily.UseDebugViewPS()
    && bRendererOutputFinalSceneColor;
```

> Reflection Capture / Planar Reflection 等次级 view 不生成 ShadowMaskTexture（节省带宽）。

---

## 5. CSM Cascade 数量与 PCF 质量档

源码：`MobileLightingCommon.ush:127-142`

```hlsl
half MobileShadowPCF(float2 ShadowUVs, FPCFSamplerSettings Settings)
{
#if   MOBILE_SHADOW_QUALITY == 0
    return ManualNoFiltering(ShadowUVs, Settings);   // 1 sample
#elif MOBILE_SHADOW_QUALITY == 1
    return Manual1x1PCF(ShadowUVs, Settings);        // 4 samples（gather4 一次）
#elif MOBILE_SHADOW_QUALITY == 2
    return Manual3x3PCF(ShadowUVs, Settings);        // 4x4 ≈ 16 samples
#elif MOBILE_SHADOW_QUALITY == 3
    return Manual5x5PCF(ShadowUVs, Settings);        // 6x6 ≈ 36 samples
#endif
}
```

```hlsl
// MobileLightingCommon.ush:151-153
#define MAX_MOBILE_SHADOWCASCADES 4u
```

```hlsl
// MobileLightingCommon.ush:155-179 MobileDirectionalLightCSM
float4 Count = float4(SceneDepth.xxxx >= MobileDirectionalLight.DirectionalLightShadowDistances);
uint CascadeIndex = uint(Count.x + Count.y + Count.z + Count.w);
if (CascadeIndex < MobileDirectionalLight.DirectionalLightNumCascades) {
    ShadowPosition = mul(ScreenPosition, ScreenToShadow[CascadeIndex]);
    Shadow = MobileShadowPCF(ShadowPosition.xy, Settings);
}
```

### 配对关系

| MOBILE_SHADOW_QUALITY | 用法 | 适用范围 |
|----------------------|------|---------|
| 0 | 锐边阴影 | 卡通风格 / 低端 |
| 1 | 4 sample | 中低端机 |
| 2 (默认) | 16 sample | 主流机型 |
| 3 | 36 sample | 高端机 / 室内特写 |

### Forward 与 Deferred 路径下设置位置

| 路径 | MOBILE_SHADOW_QUALITY 设置点 |
|------|----------------------------|
| Forward | `MobileBasePassRendering.cpp:291` 的 `ModifyCompilationEnvironmentForQualityLevel` 根据 QualityOverrides.MobileShadowQuality 注入到 BasePass PS |
| Deferred | `FMobileDirectionalLightFunctionPS::FShadowQuality` Permutation 1/2/3 通过 BuildPermutationVector 注入到 LightingPass PS |

---

## 6. CSM 阴影传递的 UB 链路

### Forward 链路

```
RenderShadowDepthMaps
  → FShadowDepthVS/PS 写入 ShadowDepthTexture（D24 或 R16F）
SetupMobileDirectionalLightUniformParameters
  → MobileDirectionalLight.DirectionalLightShadowTexture
    MobileDirectionalLight.DirectionalLightScreenToShadow[4]
    MobileDirectionalLight.DirectionalLightShadowDistances
    MobileDirectionalLight.DirectionalLightNumCascades
BasePass PS
  → MobileDirectionalLightCSM(...)
  → MobileShadowPCF(...)
```

### Deferred 链路

```
RenderShadowDepthMaps
  → FShadowDepthVS/PS 写入 ShadowDepthTexture
SetupMobileDirectionalLightUniformParameters
  → 同 Forward
LightingPass FMobileDirectionalLightFunctionPS
  → FEnableCSM Permutation = true
  → FShadowQuality Permutation = 1/2/3
  → MobileShadowPCF(...)
```

> 二者共用 `MobileDirectionalLightShaderParameters` UB（`MobileShadingRenderer.cpp:2821-2839 UpdateDirectionalLightUniformBuffers`）。

---

## 7. ModulatedShadow ── Forward 专属遗产

```cpp
// MobileShadingRenderer.cpp:1972-1973（RenderForwardSinglePass）
RHICmdList.NextSubpass();
RenderDecals(RHICmdList, View);
RenderModulatedShadowProjections(RHICmdList, ViewContext.ViewIndex, View);
```

```cpp
// MobileShadingRenderer.cpp:2086-2088（RenderForwardMultiPass）
RenderDecals(RHICmdList, View);
RenderModulatedShadowProjections(RHICmdList, ViewContext.ViewIndex, View);
```

Deferred 路径下不调用。

### 工作原理

- 调制阴影 = 用 Blend `BO_Add, BF_DestColor, BF_Zero` 把场景颜色"压暗"
- 优点：不需要光照计算，仅 1 次 multiplicative blend，移动端便宜
- 缺点：颜色失真（不能保留高光），与现代 PBR 不兼容
- 项目用法：低端档画质 / 早期 UE3 移植项目残留

### Stencil Bit

```cpp
// MobileShadingRenderer.cpp:2058-2062
FExclusiveDepthStencil::Type ExclusiveDepthStencil = FExclusiveDepthStencil::DepthRead_StencilRead;
if (bModulatedShadowsInUse) {
    ExclusiveDepthStencil = FExclusiveDepthStencil::DepthRead_StencilWrite;
}
```

> Modulated Shadow 写 Stencil；普通半透明 Subpass 只读 Stencil。

---

## 8. MovableSpotLight Shadow ── Deferred 专属

源码：`MobileDeferredShadingPass.cpp:267 / MobileDeferredShading.usf:446`

```cpp
class FSpotLightShadowDim : SHADER_PERMUTATION_BOOL("SUPPORT_SPOTLIGHTS_SHADOW");
```

```cpp
// MobileDeferredShadingPass.cpp:303-307
if (!IsMobileMovableSpotlightShadowsEnabled(Platform)) {
    PermutationVector.Set<FSpotLightShadowDim>(false);
}
```

```hlsl
// MobileDeferredShading.usf:446-485
#if SUPPORT_SPOTLIGHTS_SHADOW && IS_SPOT_LIGHT
    float4 HomogeneousShadowPosition = mul(float4(LocalPosition, 1), SpotLightShadowWorldToShadowMatrix);
    float2 ShadowUVs = HomogeneousShadowPosition.xy / HomogeneousShadowPosition.w;
    if (all(ShadowUVs >= SpotLightShadowmapMinMax.xy && ShadowUVs <= SpotLightShadowmapMinMax.zw)) {
        ...
        Shadow = MobileShadowPCF(ShadowUVs, Settings);
        Shadow = saturate((Shadow - 0.5) * SpotLightShadowSharpen + 0.5f);
        Shadow = lerp(1.0f, Square(Shadow), SpotLightFadeFraction);
    }
#endif
```

### Forward 路径下的 Spot Light Shadow

- 通常通过 `ScreenSpaceShadowMaskTexture` 替代
- 即每个 SpotLight 在 RenderMobileShadowProjections 阶段把阴影"刷"到全屏 Mask
- BasePass PS 通过 LightGrid 找到 SpotLight + 采样 Mask 得到阴影值

---

## 9. OnePassPointLight Shadow ── Deferred 专属

```cpp
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(OnePassPointLightMobileShadowDepthPass,
    CreateOnePassPointLightShadowDepthPassProcessor, EShadingPath::Mobile, ...);
```

- 一次 Pass 绘制 6 个 cubemap face（用 Layered Rendering 或 6× draw）
- `FReadOnlyCVARCache::EnablePointLightShadows(ShaderPlatform)` 控制
- Forward 路径下点光阴影通常不开（成本高且通过 Mask 替代）

---

## 10. Distance Field Shadow ── Forward 专属

```cpp
// MobileBasePass.cpp:432
if (ShadowMapInteraction.GetType() == SMIT_Texture
    && FReadOnlyCVARCache::MobileAllowDistanceFieldShadows()) {
    bHasCSMApplicableLightInteraction = ...;
}
```

```cpp
// MobileBasePass.cpp:488-500
if (FReadOnlyCVARCache::MobileEnableStaticAndCSMShadowReceivers() && !bUsesDeferredShading && bCanReceiveCSM) {
    if (FReadOnlyCVARCache::MobileAllowDistanceFieldShadows() && !bTranslucent)
        Result.Add(LMP_MOBILE_DISTANCE_FIELD_SHADOWS_LIGHTMAP_AND_CSM);
    Result.Add(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_LIGHTMAP);
}

if (FReadOnlyCVARCache::MobileAllowDistanceFieldShadows() && !bCanReceiveCSM && !bTranslucent)
    Result.Add(LMP_MOBILE_DISTANCE_FIELD_SHADOWS_AND_LQ_LIGHTMAP);
```

> `!bUsesDeferredShading` 是硬性约束 → DF Shadow 仅 Forward 路径使用。

### LightmapPolicy 矩阵

| Policy | Forward | Deferred |
|--------|---------|----------|
| `LMP_NO_LIGHTMAP` | ✅ | ✅ |
| `LMP_LQ_LIGHTMAP` | ✅ | ✅ |
| `LMP_HQ_LIGHTMAP` | ✅ | ✅ |
| `LMP_MOBILE_DISTANCE_FIELD_SHADOWS_AND_LQ_LIGHTMAP` | ✅ | ❌ |
| `LMP_MOBILE_DISTANCE_FIELD_SHADOWS_LIGHTMAP_AND_CSM` | ✅ | ❌ |
| `LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_LIGHTMAP` | ✅ | ❌ |
| `LMP_MOBILE_DIRECTIONAL_LIGHT_AND_SH_INDIRECT` | ✅ | ✅ |
| `LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_SH_INDIRECT` | ✅ | ❌ |
| `LMP_MOBILE_DIRECTIONAL_LIGHT_CSM` | ✅ | ❌ |

> Forward 路径 9 种 Policy；Deferred 路径仅 4 种。Shader Permutation 数量差 2~3 倍。

---

## 11. Per-Object Shadow（角色自阴影）

`ShadowFilteringCommon.ush:159`

```hlsl
// #if PER_OBJECT_SHADOW // TODO: For character self-shadow only. - Added by Mega.
```

> 项目里给 PER_OBJECT_SHADOW 留了占位但还未实现。完整 Per-Object Shadow 在 PC Deferred 走 `RenderShadowProjections`；移动端通常通过 Capsule Shadow 替代。

---

## 12. CapsuleShadow（间接阴影）

```hlsl
// CapsuleShadowShaders.usf:933-934
#if SHADING_PATH_MOBILE
    FGBufferData GBufferData = MobileFetchAndDecodeGBuffer(ScreenUV, SVPos);
```

- 需要 GBuffer 解码 → 仅 Deferred 完整支持
- Forward 路径下 CapsuleShadow 仅在限定条件下工作（无 GBuffer 时退化）

---

## 13. CSM Pass UB 设置详解

```cpp
// ShadowDepthRendering.cpp:227-230
void SetupShadowDepthPassUniformBuffer(
    const FProjectedShadowInfo* ShadowInfo,
    FRDGBuilder& GraphBuilder, const FViewInfo& View,
    FMobileShadowDepthPassUniformParameters& ShadowDepthPassParameters)
{
    SetupMobileSceneTextureUniformParameters(GraphBuilder, View.GetSceneTexturesChecked(),
                                              EMobileSceneTextureSetupMode::None,
                                              ShadowDepthPassParameters.SceneTextures);
    // 投影矩阵 / 偏移 / 接收偏移等
}
```

```cpp
// ShadowDepthRendering.cpp:608-611
if (GetFeatureLevelShadingPath(FeatureLevel) == EShadingPath::Mobile)
{
    PassUniformBuffer.Bind(Initializer.ParameterMap,
        FMobileShadowDepthPassUniformParameters::FTypeInfo::GetStructMetadata()->GetShaderVariableName());
}
```

> Shader 端通过 `MobileShadowDepthPass` UB 名访问；PC Deferred 通过 `ShadowDepthPass`。

---

## 14. 阴影绘制 PSO 与 BasePass 共享 Stencil

阴影深度 Pass 不写 Stencil，纯写 Depth。但其后的 BasePass + Shadow Mask 阶段共享 Depth/Stencil：

```cpp
// MobileBasePassRendering.cpp:93-97
DrawRenderState.SetDepthStencilState(TStaticDepthStencilState<
    true, CF_DepthNearOrEqual,
    true, CF_Always, SO_Keep, SO_Keep, SO_Replace,    // 写 stencil
    false, CF_Always, SO_Keep, SO_Keep, SO_Keep,
    0x00, 0xff >::GetRHI());
```

> BasePass 写 Stencil 是为了后续 Decal / LightingPass / Modulated Shadow / Translucency 区分。

---

## 15. 双路径阴影流程图

```
                    ShadowDepth
                         │
              ┌──────────┴──────────┐
              │                     │
          Forward                 Deferred
              │                     │
              │           ScreenSpaceShadowMaskTexture
              │                     │
       MobileBasePassCSM       MobileBasePass
       (CSM Permutation)       (无 CSM Permutation)
              │                     │
              │           MobileDeferredShadingPass
              │           (FEnableCSM + FShadowQuality + FUseShadowMaskTexture)
              │                     │
              │                     │
       BasePass PS 内 CSM    LightingPS 内 CSM
              │                     │
        ModulatedShadow Subpass     │
              │                     │
              └──────────┬──────────┘
                         │
                   场景颜色
```

---

## 16. 调优 CVar 速查

| CVar | 推荐值 | 路径 | 含义 |
|------|--------|------|------|
| `r.Shadow.MaxResolution` | 1024（移动） | 共享 | 阴影贴图分辨率 |
| `r.Shadow.MaxCSMResolution` | 1024（移动） | 共享 | CSM 分辨率 |
| `r.Shadow.CSM.MaxCascades` | 3（移动） | 共享 | 级联数 |
| `r.Mobile.Shadow.CSMShaderCullingMethod` | 1 | Forward | CSM 剔除方法 |
| `r.Mobile.EnableStaticAndCSMCombinedShadow` | 1 | Forward | 静态 + CSM 合并 |
| `r.Mobile.AllowDistanceFieldShadows` | 0/1 | Forward | DF 阴影 |
| `r.Mobile.AllowMovableDirectionalLights` | 1 | 共享 | 移动主光 |
| `r.Mobile.EnableMovableSpotlights` | 0/1 | Deferred 主 | Spot Movable |
| `r.Mobile.EnableMovableSpotlightShadows` | 0/1 | Deferred 主 | Spot Movable Shadow |
| `r.MobileShadowQuality` | 2 | 共享 | PCF 档 |
| `r.AllowStaticLighting` | 1 | 共享 | 关闭后 ShadingModel 用 CustomData |
| `r.Mobile.AllowSoftwareOcclusion` | 0 | 共享 | 与 HZB 互斥 |
| `r.Mobile.CSM.SkipBackfaceMatchingShadowQuality` | 1 | Forward | 背面剔除优化 |

---

## 17. 易错点

| 现象 | 原因 | 解决 |
|------|------|------|
| Forward CSM 跳变 | Cascade 距离配置不匹配 PCF | 调 `r.Shadow.CSMTransitionScale` |
| Deferred 下 ShadowMask 不显示 | `FUseShadowMaskTexture` Permutation 没编译 | 检查 `MobileUsesShadowMaskTextureRuntime` |
| 角色没自阴影 | PER_OBJECT_SHADOW 未实现 | 启用 PerObjectShadow / Capsule Shadow |
| Spot 阴影闪烁 | Spot SoftTransition 配置 | `SpotLightShadowSharpen` 调高 |
| Modulated 阴影颜色异常 | Blend 公式仅 multiplicative | 改用 ScreenSpaceShadowMask |
| CSM 后端 PCF 噪点 | MOBILE_SHADOW_QUALITY 太低 | 改 2 或 3 |
| Forward MobileBasePassCSM 失效 | bCanReceiveCSM=false | 检查 `MobileNonCSMStaticMeshVisibilityMap` |
| OnePassPointLight 闪现 | LightView 数据没同步 | 调 `r.Shadow.PointLightDepthBias` |
| Distance Field Shadow 黑边 | LMP_MOBILE_DISTANCE_FIELD_* Permutation 未编译 | 必须 `MobileAllowDistanceFieldShadows=1` 且 Forward |

---

> 第 02 篇完。下一篇：**MobilePostProcessing 全链 / Tonemap / Bloom / DOF / FXAA / TAA**。
