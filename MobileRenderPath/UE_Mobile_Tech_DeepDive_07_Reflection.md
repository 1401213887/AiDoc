# UE Mobile Forward vs Deferred —— 深度补充 07：反射系统全谱

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**Reflection Capture / Sky Light Cubemap / Planar Reflection / SSR / SSXR / PixelProjectedReflection** 在双管线下的差异。

---

## 1. UE 移动端反射技术栈全景

| 技术 | Forward | Deferred | 实现 | 性能 |
|------|---------|----------|------|------|
| Reflection Capture (Cubemap) | ✅ LQ/HQ | ✅ Clustered | `ReflectionEnvironmentShared.ush` | 中 |
| Sky Light Cubemap | ✅ | ✅ | 同上 | 低 |
| Planar Reflection (CaptureMesh) | ✅ | ✅ | `PlanarReflectionRendering.cpp` | 高 |
| Pixel Projected Reflection (PPR) | ✅ | ✅ | `PostProcessPixelProjectedReflectionMobile.cpp` | 中 |
| Screen Space Reflection (SSR) | ❌ | ✅ | `MobileSSR.cpp` | 高 |
| Screen Space X Reflection (SSXR) | ❌ | ✅ | `ScreenSpaceXRayTracing.cpp` | 极高 |

---

## 2. Reflection Capture（Cubemap 反射球）

### 2.1 数据准备

```cpp
// MobileShadingRenderer.cpp:900-906
if (bDeferredShading ||
    bEnableClusteredLocalLights ||
    bEnableClusteredReflections)
{
    SetupSceneReflectionCaptureBuffer(RHICmdList);
}
```

`MobileReflectionCaptureUniformBuffer` 通过 ViewUB 传给 BasePass / LightingPass。

### 2.2 LQ vs HQ 反射

```hlsl
// MobileBasePassPixelShader.usf
#define HQ_REFLECTIONS (MATERIAL_HQ_FORWARD_REFLECTIONS && !MOBILE_QL_FORCE_LQ_REFLECTIONS)

#if !FULLY_ROUGH
    TextureCube ReflectionCubemap;
    #if HQ_REFLECTIONS
        #define MAX_HQ_REFLECTIONS 3
        TextureCube ReflectionCubemap1;
        TextureCube ReflectionCubemap2;
        float4 ReflectionPositionsAndRadii[MAX_HQ_REFLECTIONS];
    #endif
#endif
```

| 路径 | LQ | HQ |
|------|----|----|
| Forward | ✅（默认 1 个反射球） | ✅（最多 3 个混合） |
| Deferred | – | – （走 ClusteredReflection） |

### 2.3 ClusteredReflection（Deferred 主用）

```cpp
// MobileDeferredShadingPass.cpp:86-93
class FEnableClustredReflection : SHADER_PERMUTATION_BOOL("ENABLE_CLUSTERED_REFLECTION");
```

```cpp
// MobileDeferredShadingPass.cpp:211-213
bool bClustredReflection = bInlineReflectionAndSky
                        && ((View.NumBoxReflectionCaptures + View.NumSphereReflectionCaptures) > 0
                            || View.NumGlobalReflectionCaptures > 0);
```

- 通过 LightGrid 查找当前 cell 内候选反射球
- 按距离权重混合
- 数量无硬上限

### 2.4 SkyLightCubemap

```hlsl
// ReflectionEnvironmentShared.ush:44-46
float AbsoluteSpecularMip = ComputeReflectionCaptureMipFromRoughness(Roughness, ReflectionStruct.SkyLightParameters.x);
float3 Reflection = TextureCubeSampleLevel(ReflectionStruct.SkyLightCubemap, ...);
OutSkyAverageBrightness = GetSkyLightCubemapBrightness() * Luminance(View.SkyLightColor.rgb);
```

- 全局 SkyLight Cubemap，所有像素共享
- 根据 Roughness 选择 mip level
- 与反射球混合：`Reflection = lerp(SkyLightReflection, CaptureReflection, CaptureWeight)`

### 2.5 SkyLight Blend Destination（场景过渡）

```hlsl
// ReflectionEnvironmentShared.ush:97-101
float AbsoluteSpecularMip = ComputeReflectionCaptureMipFromRoughness(...);
float3 BlendDestinationReflection = TextureCubeSampleLevel(
    ReflectionStruct.SkyLightBlendDestinationCubemap, ..., ReflectionVector, AbsoluteSpecularMip).rgb;
Reflection = lerp(Reflection, BlendDestinationReflection * View.SkyLightColor.rgb,
                  ReflectionStruct.SkyLightParameters.w);
```

> 项目里可同时持有两个 SkyLight Cubemap，做日夜过渡 / 区域过渡的平滑混合。

---

## 3. Planar Reflection（平面反射，捕获 Mesh）

源码：`PlanarReflectionRendering.cpp`

### 3.1 捕获流程

1. 关卡内放置 `APlanarReflectionComponent`
2. 系统从镜像视角渲染一遍场景到 PlanarReflectionTexture
3. BasePass PS 通过 UV 投影采样

### 3.2 调度

```cpp
// MobileShadingRenderer.cpp:662-672
const FPlanarReflectionSceneProxy* PlanarReflectionSceneProxy =
    Scene ? Scene->GetForwardPassGlobalPlanarReflection() : nullptr;

bRequiresPixelProjectedPlanarRelfectionPass = IsUsingMobilePixelProjectedReflection(ShaderPlatform)
    && PlanarReflectionSceneProxy != nullptr
    && PlanarReflectionSceneProxy->RenderTarget != nullptr
    && !Views[0].bIsReflectionCapture
    && !ViewFamily.EngineShowFlags.HitProxies
    && ViewFamily.EngineShowFlags.Lighting
    && !ViewFamily.EngineShowFlags.VisualizeLightCulling
    && !ViewFamily.UseDebugViewPS()
    && bRendererOutputFinalSceneColor;
```

> **关键限制**：移动端**只能有一个全局 Planar Reflection**（通过 `GetForwardPassGlobalPlanarReflection` 取）。多平面反射在 PC Deferred 才支持。

### 3.3 Permutation 控制

```hlsl
// MobileBasePassPixelShader.usf
#define ENABLE_PLANAR_REFLECTION  // 仅 Forward + 项目配置
```

```cpp
// MobileDeferredShadingPass.cpp:88
class FEnablePlanarReflection : SHADER_PERMUTATION_BOOL("ENABLE_PLANAR_REFLECTION");
```

> Deferred 路径下 `FEnablePlanarReflection` Permutation 由 LightingPS 选择。

---

## 4. Pixel Projected Reflection (PPR) ── 移动端独有

源码：`PostProcess/PostProcessPixelProjectedReflectionMobile.cpp`

### 4.1 核心思想

- 不是真正的"捕获"反射，而是基于场景深度的**屏幕空间像素投影**
- 比 SSR 简单：只支持**水平面镜面反射**（与 z=0 平行的镜面）
- 比 Planar Reflection 便宜：**不需要重新渲一遍场景**

### 4.2 调度

```cpp
// MobileShadingRenderer.cpp:1672-1677
if (bRequiresPixelProjectedPlanarRelfectionPass)
{
    const FPlanarReflectionSceneProxy* PlanarReflectionSceneProxy =
        Scene ? Scene->GetForwardPassGlobalPlanarReflection() : nullptr;

    RenderPixelProjectedReflection(GraphBuilder, SceneTextures.Color.Resolve,
        SceneTextures.Depth.Resolve, SceneTextures.PixelProjectedReflection,
        PlanarReflectionSceneProxy);
}
```

### 4.3 Quality 档

```cpp
// PostProcessPixelProjectedReflectionMobile.cpp:34
static TAutoConsoleVariable<int32> CVarMobilePixelProjectedReflectionQuality(
    TEXT("r.Mobile.PixelProjectedReflectionQuality"), 1, ...);
```

| Quality | 含义 |
|---------|------|
| 0 | Off |
| 1 | Low（1/4 分辨率） |
| 2 | Medium |
| 3 | High（全分辨率） |

### 4.4 三个 Shader

1. **ProjectPassCS**：每像素投影到镜面 UV 写 Output
2. **ReflectionPassPS**：反射 Quad 渲染时 Sample Projected RT
3. **CompositePassPS**：合成回 SceneColor

```cpp
// PostProcessPixelProjectedReflectionMobile.cpp:102-105
static bool ShouldCompilePermutation(const FGlobalShaderPermutationParameters& Parameters) {
    return IsMobilePixelProjectedReflectionEnabled(Parameters.Platform);
}
```

### 4.5 与传统 Planar 的区别

| 维度 | 传统 Planar | PPR |
|------|------------|-----|
| 场景渲染 | 重新渲一遍 | 复用主场景 |
| 性能 | 高 | 中 |
| 真实度 | 高（任意角度） | 低（只能水平面） |
| 平台 | 全部 | Mobile only |
| 多反射面 | ❌（移动端） | ❌ |

### 4.6 SceneTextures.PixelProjectedReflection 创建

```cpp
// SceneTextures.cpp:818-821
if (Config.MobilePixelProjectedReflectionExtent != FIntPoint::ZeroValue)
{
    SceneTextures.PixelProjectedReflection =
        CreateMobilePixelProjectedReflectionTexture(GraphBuilder, Config.MobilePixelProjectedReflectionExtent);
}
```

> RT 大小由 PlanarReflectionComponent 的 ScreenPercentage 决定。

### 4.7 PlanarReflectionComponent 的特殊角色

```cpp
// PlanarReflectionRendering.cpp:512-517
const bool bIsMobilePixelProjectedReflectionEnabled = IsMobilePixelProjectedReflectionEnabled(GetShaderPlatform());

const bool bIsRenderTargetValid = ...
    && (bIsMobilePixelProjectedReflectionEnabled || CaptureComponent->RenderTarget->TextureRHI.IsValid());
```

> 当 PPR 启用时，`PlanarReflectionComponent` 的 RenderTarget 仅作为**位置 / 法线信息容器**，不分配实际 RHI 纹理（节省内存）。

---

## 5. Screen Space Reflection (SSR) ── Deferred 专属

### 5.1 启用条件

```cpp
// MobileShadingRenderer.cpp:349
bRequiresScreenSpaceReflections = AreMobileScreenSpaceReflectionsEnabled(ShaderPlatform);
```

```cpp
// PostProcessPostProcess.cpp:3298
else if (IsMobileSSREnabled(View))
{
    // If we need SSR, and TAA is enabled, then AddTemporalAAPass() has already handled the scene history.
    ...
}
```

### 5.2 EMobileSSRQuality 枚举

```cpp
// MobileDeferredShadingPass.cpp:92
class FMobileSSRQuality : SHADER_PERMUTATION_ENUM_CLASS("MOBILE_SSR_QUALITY", EMobileSSRQuality);
```

| Quality | 含义 |
|---------|------|
| Disabled | 0 |
| Low | 单步进 |
| Medium | 多步进 + 二分查找 |
| High | + 多次抖动采样 |

### 5.3 ActiveMobileSSRQuality 动态决策

```cpp
// MobileShadingRenderer.cpp:2313
const EMobileSSRQuality MobileSSRQuality = ActiveMobileSSRQuality(View, bShouldRenderVelocities);
```

- 根据 View 设置 + Velocity 是否有效 选择 Quality
- 远处材质强制 Low
- 高粗糙度强制 Disabled

### 5.4 SSR Stencil 拆分

```cpp
// MobileDeferredShadingPass.cpp:541-548
if (MobileSSRQuality != EMobileSSRQuality::Disabled) {
    // Separate pass for fully rough default lit materials
    int PassIndex = NumPasses++;
    StencilState[0] = StencilState[PassIndex] = TStaticDepthStencilState<
        false, CF_Always,
        true, CF_Equal, SO_Keep, SO_Keep, SO_Keep,
        false, CF_Always, SO_Keep, SO_Keep, SO_Keep,
        GET_STENCIL_MOBILE_SM_MASK(0xff), 0x00>::GetRHI();

    StencilRef[PassIndex] = STENCIL_MOBILE_DEFAULTLIT_MASK | STENCIL_MOBILE_REFLECTIVE_MASK;
    PassEnableSSR |= (1 << PassIndex);
}
```

> 通过 Stencil bit `STENCIL_MOBILE_REFLECTIVE_MASK` 区分"接收 SSR"和"不接收 SSR"的像素，分两个 PS 渲染——**避免 fully rough 像素浪费 SSR 计算**。

---

## 6. Screen Space X Reflection (SSXR) ── Deferred 专属

源码：`ScreenSpaceXRayTracing.cpp:2925-2933`

```cpp
static void LightingMobileScreenSpaceReflections(
    FRDGBuilder& GraphBuilder,
    const FMobileCommonSSRTParameters& CommonSSRTParameters,
    ...);

// 3015
ScreenSpaceRayTracing::LightingMobileScreenSpaceReflections(
    GraphBuilder, CommonSSRTParameters, View, Scene->SkyLight, Context.TemporalInfo, SceneColor);
```

### 6.1 与 SSR 区别

- SSR：当前帧 SceneColor + Depth 做反射追踪
- SSXR：**当前帧 + 上一帧** SceneColor + Depth 做追踪，**支持透视背面**

### 6.2 调度

```cpp
// MobileShadingRenderer.cpp:1592-1606
if (ScreenSpaceRayTracing::RequireMobileScreenSpaceXReflections(ShaderPlatform)) {
    for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ++ViewIndex) {
        const FViewInfo& View = Views[ViewIndex];

        if (!View.bStatePrevViewInfoIsReadOnly) {
            // Keep scene color and depth for next frame SSXR ray tracing.
            FSceneViewState* ViewState = View.ViewState;
            GraphBuilder.QueueTextureExtraction(SceneTextures.Depth.Resolve, &ViewState->PrevFrameViewInfo.DepthBuffer);
            GraphBuilder.QueueTextureExtraction(SceneTextures.Color.Resolve, &ViewState->PrevFrameViewInfo.ScreenSpaceRayTracingInput);
        }
    }
}
```

### 6.3 SSXR 的额外 Velocity 准备

```cpp
// MobileShadingRenderer.cpp:2530-2561
if (bRequiresSSXR) {
    FRDGTextureRef ScreenSpaceXReflection = GScreenSpaceXReflectionMobileOutputs.CreateTexture(ViewFamily, GraphBuilder);
    EMobileSceneTextureSetupMode SetupMode = EMobileSceneTextureSetupMode::All;
    if (bShouldRenderVelocities) {
        EDepthDrawingMode EarlyZPassMode = Scene ? Scene->EarlyZPassMode : DDM_None;
        if (EarlyZPassMode != DDM_AllOpaqueNoVelocity) {
            RenderVelocities(GraphBuilder, Views, SceneTextures, EVelocityPass::Opaque, false);
        }
        RenderVelocities(GraphBuilder, Views, SceneTextures, EVelocityPass::Translucent, false);
        bShouldRenderVelocities = false;
    } else {
        SetupMode &= ~EMobileSceneTextureSetupMode::SceneVelocity;
    }
    SceneTextures.MobileUniformBuffer = CreateMobileSceneTextureUniformBuffer(GraphBuilder, &SceneTextures, SetupMode);
    FMobileSceneTextureParameters MobileSceneTextureParameters = GetMobileSceneTextureParameters(GraphBuilder, SceneTextures.MobileUniformBuffer);

    for (FRenderViewContext& ViewContext : RenderViews) {
        FViewInfo& View = *ViewContext.ViewInfo;
        FMobileCommonSSRTParameters CommonSSRTParameters;
        ScreenSpaceRayTracing::SetupCommonSSRTParametersMobile(GraphBuilder, MobileSceneTextureParameters, View, &CommonSSRTParameters);
        ScreenSpaceRayTracing::RenderScreenSpaceXReflectionsMobile(GraphBuilder, ScreenSpaceXReflection, View, Scene, CommonSSRTParameters);
    }
    MobileBasePassTextures.ScreenSpaceXReflection = ScreenSpaceXReflection;
}
```

> SSXR 必须有 Velocity Buffer（上一帧像素位置）才能正确追踪。

---

## 7. AccumulateReflection 统一入口

```hlsl
// MobileBasePassPixelShader.usf:1152-1162
#if MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID
    || TRANSLUCENCY_LIGHTING_SURFACE_FORWARDSHADING
    || TRANSLUCENCY_LIGHTING_SURFACE_LIGHTINGVOLUME
    || MATERIAL_SHADINGMODEL_SINGLELAYERWATER
    // Reflection IBL
    AccumulateReflection(GBuffer
        , SvPosition
        , CameraVector
        , MaterialParameters.WorldPosition_CamRelative
        , MaterialParameters.ReflectionVector
        , RoughReflectionLighting
        , GridIndex
        , DirectLighting);
#endif
```

`AccumulateReflection` 在 `ReflectionEnvironmentShared.ush` 内实现：

1. 取 SSR / SSXR 缓冲（若可用）
2. 否则尝试 PlanarReflection
3. 否则采样 ReflectionCubemap（LQ 单球 / HQ 三球 / ClusteredReflection 多球）
4. 与 SkyLight Cubemap 混合
5. 输出到 DirectLighting

> **二者共用同一 AccumulateReflection 入口**，区别在 `#define` 配置。

---

## 8. 双路径反射性能对比（典型场景）

### 8.1 LQ Cubemap（远处 / 移动端默认）

| 路径 | 单像素开销 |
|------|-----------|
| Forward | 1 个 TextureCubeSample（~5 cycles） |
| Deferred | 同 + GBuffer 解码（~10 cycles） |

### 8.2 HQ 3 球（近处 / 高端）

| 路径 | 单像素开销 |
|------|-----------|
| Forward | 3 个 TextureCubeSample + 距离权重（~20 cycles） |
| Deferred | 不支持（用 ClusteredReflection） |

### 8.3 ClusteredReflection（Deferred 优势）

| 路径 | 单像素开销 |
|------|-----------|
| Forward | – |
| Deferred | LightGrid lookup（~3 cycles） + N 个采样 |

### 8.4 SSR / SSXR

| 路径 | 单像素开销 |
|------|-----------|
| Forward | 不支持 |
| Deferred SSR Low | ~50 cycles |
| Deferred SSR Med | ~120 cycles |
| Deferred SSR High | ~300 cycles |
| Deferred SSXR | ~500 cycles（含上帧缓冲） |

---

## 9. SkyLightParameters Vector4 解析

```hlsl
// ReflectionStruct.SkyLightParameters
// x: max mip level
// y: enable specular  (UseBasePassSkylightSpecular)
// z: BlendDestinationFraction
// w: BlendDestinationFactor
```

```hlsl
// MobileBasePassPixelShader.usf:50
#define UseBasePassSkylightSpecular (MobileBasePass.ReflectionsParameters.SkyLightParameters.y)
```

> 移动端 SkyLight Specular 默认关闭（`y=0`），可通过 `r.SkyLight.EnableSpecular` 启用。开启后 Forward 路径 BasePass 会多采样 SkyLight Cubemap 用于 specular。

---

## 10. ReflectionsParameters UB 内容

```cpp
// MobileBasePassRendering.h:57
SHADER_PARAMETER_STRUCT(FReflectionUniformParameters, ReflectionsParameters)
```

包含：
- `SkyLightCubemap`
- `SkyLightBlendDestinationCubemap`
- `SkyLightParameters`
- `PreIntegratedGF`
- `ReflectionCubemap`（LQ）
- `ReflectionCubemap1/2`（HQ）
- `ReflectionPositionsAndRadii[3]`（HQ）

> 双路径共用同一 UB，但仅 LightingPS（Deferred）或 BasePass PS（Forward）实际访问。

---

## 11. PreIntegratedGF（EnvBRDF LUT）

```hlsl
// MobileBasePassPixelShader.usf:52-57
#define MOBILE_USE_PREINTEGRATED_GF (MATERIAL_USE_PREINTEGRATED_GF && !MOBILE_QL_FORCE_DISABLE_PREINTEGRATEDGF)

#if MOBILE_USE_PREINTEGRATED_GF
#define PreIntegratedGF        MobileBasePass.PreIntegratedGFTexture
#define PreIntegratedGFSampler MobileBasePass.PreIntegratedGFSampler
#endif
```

- 预积分 Geometric Function (G) × Fresnel (F) → 2D LUT
- 高粗糙度时 EnvBRDF 近似
- `MOBILE_QL_FORCE_DISABLE_PREINTEGRATEDGF=1` 时跳过，省一次采样

### 双路径使用

| 路径 | PreIntegratedGF 用处 |
|------|--------------------|
| Forward | BasePass PS 算 IBL Specular |
| Deferred | LightingPS（含 BasePass 的反射写入）算 IBL Specular |

---

## 12. 反射场景的 Capture/Update 流程

源码：`ReflectionEnvironmentCapture.cpp:1572`

```cpp
if (UseVirtualTexturing(GetShaderPlatform())) {
    // Prefetch all virtual textures so that we have content available
    const ERHIFeatureLevel::Type InFeatureLevel = FeatureLevel;
    ...
}
```

### 12.1 Capture 流程

1. Editor 中点击 Build Reflection Captures
2. 系统在 ReflectionCapture 位置创建虚拟相机
3. 渲染 6 个 cube face → CaptureCubemap RT
4. Filter / Pre-integrate → 各 mip 存 IBL Cubemap
5. 保存到 LightMap 数据

### 12.2 Runtime 加载

- 每个 ReflectionCaptureComponent 注册到 Scene
- BuildPermutationVector 时统计 `NumBoxReflectionCaptures + NumSphereReflectionCaptures + NumGlobalReflectionCaptures`
- 决定是否启用 ClusteredReflection Permutation

---

## 13. Forward 路径下反射的最大限制

```cpp
// MobileForwardEnableClusteredReflections(Platform) 默认 false
```

> Forward 路径默认**不启用 ClusteredReflection**，最多 1 个 LQ 反射球或 3 个 HQ 反射球。多反射球场景必须切 Deferred。

---

## 14. CVar 速查（反射）

| CVar | 默认 | 路径 | 含义 |
|------|------|------|------|
| `r.Mobile.Forward.EnableClusteredReflections` | 0 | Forward | LightGrid 反射 |
| `r.ReflectionEnvironment` | 1 | 共享 | 反射环境总开关 |
| `r.SkyLight.EnableSpecular` | 0 | 共享 | SkyLight 镜面 |
| `r.Mobile.PixelProjectedReflectionQuality` | 1 | 共享 | PPR 质量 |
| `r.Mobile.PixelProjectedReflectionEnabled` | – | 共享 | PPR 总开关 |
| `r.Mobile.ScreenSpaceReflections` | 0 | Deferred | SSR |
| `r.Mobile.SSR.Quality` | 1 | Deferred | SSR 质量 |
| `r.Mobile.SSXR.Enabled` | 0 | Deferred | SSXR |
| `r.Mobile.HQReflections` | 0 | Forward | HQ 3 球 |
| `r.Mobile.ForceReflectionCaptureQuality` | – | 共享 | 强制档 |
| `r.Mobile.NumLocalReflectionCaptures` | – | 共享 | LightGrid cell 容量 |
| `r.ReflectionCaptureResolution` | 128 | 共享 | Cubemap 分辨率 |
| `r.SkyLightingQuality` | 1 | 共享 | SkyLight 质量 |

---

## 15. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| 反射全黑 | Reflection Capture 没烘焙 | Editor BuildReflectionCaptures |
| 切场景反射不更新 | UB 没刷新 | 检查 SetupSceneReflectionCaptureBuffer |
| PPR 镜面错位 | PlanarReflectionComponent 法线错 | 检查 Transform |
| SSR 噪点 | TAA 没开 | `r.AntiAliasingMethod=2` |
| SSXR 黑屏 | 上一帧数据未保存 | bStatePrevViewInfoIsReadOnly=true |
| HQ 反射不混合 | ReflectionPositionsAndRadii 没传 | 检查 BuildPermutationVector |
| ClusteredReflection 不生效 | bEnableClusteredReflections=false | 检查 r.Mobile.Forward.EnableClusteredReflections |
| SkyLight 闪烁 | Blend Destination 配错 | SkyLightParameters.w |
| Forward HQ 反射性能崩 | 3 个 Cubemap 采样太重 | 改用 MobileQL_FORCE_LQ_REFLECTIONS |
| Deferred 反射 Stencil 错 | STENCIL_MOBILE_REFLECTIVE_MASK 未写入 | 检查 BasePass Stencil 写入 |

---

> 第 07 篇完。下一篇：**Decal / Fog / Sky / Atmosphere 完整对比**。
