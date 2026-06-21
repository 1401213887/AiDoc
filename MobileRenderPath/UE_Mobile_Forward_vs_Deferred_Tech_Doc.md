# UE 移动端 Forward 与 Deferred 管线差异技术文档

> 基于源码: UE5.5 + 项目 patch (Engine 路径 `f:/ZJG_GR_DevTest/UE5EA/Engine`)
> 生成时间: 2026-06-20
> 适用对象: 引擎渲染开发、TA、移动端性能优化

---

## 0. 阅读说明

本文系统对比 UE 在移动端（`IsMobilePlatform == true`）下两条着色路径的**实现差异**，不再赘述通用前向 / 延迟概念。结构按"配置开关 → 调度链路 → BasePass → Lighting Pass → GBuffer → 阴影/反射/光源/半透明/装饰物 → Shader 宏 → CVar 速查 → 总结"层层展开。

引用文件均位于：
- C++ : `Engine/Source/Runtime/Renderer/Private/` 与 `Engine/Source/Runtime/RenderCore/`
- USF/USH : `Engine/Shaders/Private/`

---

## 1. 路径分叉的根开关

### 1.1 `IsMobileDeferredShadingEnabled(Platform)` —— 唯一总闸门

定义位于 `RenderUtils`，由项目设置或 DDPI（DataDrivenShaderPlatformInfo）决定。整个渲染器在 `FMobileSceneRenderer` 构造时根据它一次性确定 `bDeferredShading`：

```cpp
// MobileShadingRenderer.cpp:329-348
FMobileSceneRenderer::FMobileSceneRenderer(const FSceneViewFamily*, FHitProxyConsumer*)
    : ...
    , bDeferredShading(IsMobileDeferredShadingEnabled(ShaderPlatform))
    , bRequiresDBufferDecals(bDeferredShading ? false : IsUsingDBuffers(ShaderPlatform))
    , bSupportsSimpleLights(bDeferredShading || MobileForwardEnableParticleLights(ShaderPlatform))
{
    ...
    bEnableClusteredLocalLights    = MobileForwardEnableLocalLights(ShaderPlatform);
    bEnableClusteredReflections    = MobileForwardEnableClusteredReflections(ShaderPlatform);
    bRequiresScreenSpaceReflections= AreMobileScreenSpaceReflectionsEnabled(ShaderPlatform);
}
```

由此衍生出后续所有差异：渲染目标布局、Pass 调度、Permutation 选择、Shader 宏。

### 1.2 关键 Platform 工具函数（`RenderUtils.cpp`）

| 函数 | 仅在 Deferred 路径生效 | 描述 |
|------|---------------------|------|
| `IsMobileDeferredShadingEnabled` | 总开关 | DataDriven + 项目配置 |
| `MobileUsesExtenedGBuffer` | Deferred Only | 是否启用 5 个 MRT（追加 GBufferD） |
| `MobileUsesGBufferCustomData` | Deferred Only | 同上等价（项目已硬开） |
| `MobileForwardEnableLocalLights` | Forward Only | LightGrid 多光源支持 |
| `MobileForwardEnableClusteredReflections` | Forward Only | LightGrid 上挂反射球 |
| `MobileForwardEnableParticleLights` | Forward Only | 粒子 SimpleLight 支持 |
| `MobileBasePassAlwaysUsesCSM` | 永远 true（Deferred） | Deferred 不做 CSM 剔除 |
| `MobileRequiresSceneDepthAux` | Forward+HDR 时 true | 移动端独立深度辅助贴图 |
| `MobileUsesShadowMaskTexture` | Forward 用更多 | 屏幕空间 ShadowMask 路径 |

### 1.3 DBuffer Decal 的强制关系

```cpp
GDBufferPlatformMask[ShaderPlatformIndex] =
    IsMobilePlatform(...) ? (TargetPlatformSettings->UsesMobileDBuffer() && !IsMobileDeferredShadingEnabled(...))
                          : TargetPlatformSettings->UsesDBuffer();
```

> **结论：移动端 Deferred 强制禁用 DBuffer 贴花**，只能使用屏幕空间 Decal（后置在 `RenderDecals` 子 Pass）。

---

## 2. 渲染调度链路对比

### 2.1 `FMobileSceneRenderer::Render()` 顶层分支（约 1581 行）

```cpp
if (bDeferredShading)
{
    if (bRequiresMultiPass) RenderDeferredMultiPass(GraphBuilder, SceneTextures, SortedLightSet);
    else                    RenderDeferredSinglePass(GraphBuilder, SceneTextures, SortedLightSet);

    if (ScreenSpaceRayTracing::RequireMobileScreenSpaceXReflections(ShaderPlatform))
    {
        // Deferred 特有: 保存上一帧 SceneColor/Depth 给 SSXR
        ViewState->PrevFrameViewInfo.ScreenSpaceRayTracingInput = ...;
    }
}
else
{
    RenderForward(GraphBuilder, ViewFamilyTexture, SceneTextures, DBufferTextures);
}
```

### 2.2 `RequiresMultiPass()` ——是否需要拆 RenderPass

```cpp
// MobileShadingRenderer.cpp:2780
bool FMobileSceneRenderer::RequiresMultiPass(int32 NumMSAA, EShaderPlatform Platform)
{
    if (IsVulkanPlatform(Platform)) return false;                          // Vulkan 用 Subpass
    if (IsMetalMobilePlatform(Platform) && GSupportsShaderFramebufferFetch) return false;
    if (IsAndroidOpenGLESPlatform(Platform) &&
        (GSupportsShaderFramebufferFetch || GSupportsShaderDepthStencilFetch)) return false;
    if (IsMobileDeferredShadingEnabled(Platform)) return true;             // ← Deferred 默认走多 Pass
    if (!IsMobileHDR()) return false;
    if (NumMSAA > 1)   return false;
    return true;
}
```

> 重点：
> - **Forward 路径** 可在 Vulkan / Metal / GLES_FBF 上跑 **单 RenderPass + Subpass**，让 SceneColor 在 Tile Memory 里走完 Depth-PrePass → BasePass → Decal → Translucency → CustomResolve。
> - **Deferred 路径** 在以上具备 Subpass / FBF / PLS 能力的平台上同样走 **单 RenderPass**（DeferredShadingSubpass），否则降级为多 RenderPass，强制把 GBuffer 拷到主存。

### 2.3 Forward 调度（单 Pass，Single RenderPass + Subpass）

```cpp
// MobileShadingRenderer.cpp:1937 RenderForwardSinglePass()
GraphBuilder.AddPass("SceneColorRendering", Raster|NeverMerge, [...] {
    RenderMaskedPrePass(...);              // 可选 PrePass
    RenderMobileBasePass(...);             // ★ 一次性算完 材质+光照+阴影
    RenderMobileDebugView(...);
    PostRenderBasePass(...);
    RHICmdList.NextSubpass();              // 进入 DepthReadSubpass
    RenderDecals(...);                     // 网格贴花
    RenderModulatedShadowProjections(...); // 调制阴影
    RenderFog(...);                        // 像素雾
    RenderTranslucency(...);               // 半透明
    if (bDoOcclusionQueries) RenderOcclusion(...);
    PreTonemapMSAA(...);                   // iOS 专用
    if (bTonemapSubpassInline) {
        RHICmdList.NextSubpass();
        RenderMobileCustomResolve(...);    // Tonemap + Resolve 在 Tile 内完成
    }
});
```

**RenderTarget 绑定 `InitRenderTargetBindings_Forward`**：
- `RT0` = SceneColor (HDR FP16 或 sRGB8) `Clear`
- `RT1` = SceneDepthAux（仅 GLES/Vulkan HDR 需要）`Clear`
- `Depth/Stencil` = SceneDepth `Clear/Clear DepthWrite_StencilWrite`
- `SubpassHint` = `DepthReadSubpass` 或 `CustomResolveSubpass`（带内联 tonemap）

### 2.4 Deferred 调度（单 Pass，`DeferredShadingSubpass`）

```cpp
// MobileShadingRenderer.cpp:2241 RenderDeferredSinglePass()
PassParameters->RenderTargets.SubpassHint = ESubpassHint::DeferredShadingSubpass;

GraphBuilder.AddPass("SceneColorRendering", Raster|NeverMerge, [...] {
    RenderMaskedPrePass(...);
    RenderMobileBasePass(...);              // ★ 只写 GBuffer + emissive
    PostRenderBasePass(...);
    RHICmdList.NextSubpass();               // Subpass 1: GBuffer write, Depth read-only
    RenderDecals(...);
    RHICmdList.NextSubpass();               // Subpass 2: SceneColor write, GBuffer/Depth read-only
    MobileDeferredShadingPass(...);         // ★★ 真正打光（Directional / Local / IBL / SkyLight）
    if (bUsingPixelLocalStorage)
        MobileDeferredCopyBuffer<FMobileDeferredCopyPLSPS>(...);   // GLES PLS 把 PLS 拷回 SceneColor
    RenderFog(...);
    RenderTranslucency(...);                // 半透明走 forward shading（subpass 中读 GBuffer）
});
```

**RenderTarget 绑定 `InitRenderTargetBindings_Deferred` + `GetColorTargets_Deferred`**：

```cpp
ColorTargets.Add(SceneTextures.Color.Target);   // RT0  最终颜色（subpass 后写）
ColorTargets.Add(SceneTextures.GBufferA);       // RT1  Normal (PF_A2B10G10R10/FloatRGBA)
ColorTargets.Add(SceneTextures.GBufferB);       // RT2  Metallic/Specular/Roughness/ShadingModelID
ColorTargets.Add(SceneTextures.GBufferC);       // RT3  BaseColor + IndirectIrradiance
if (MobileUsesExtenedGBuffer(ShaderPlatform))
    ColorTargets.Add(SceneTextures.GBufferD);   // RT4/5 CustomData (额外 ShadingModel)
if (bRequiresSceneDepthAux)
    ColorTargets.Add(SceneTextures.DepthAux.Target);
```

PLS 兼容路径（Android GLES）只绑定 `SceneColor`，GBuffer 在像素本地存储（Pixel Local Storage）里。

### 2.5 多视口（MultiPass）退化路径

| 路径 | 触发条件 | Pass 序列 |
|------|---------|----------|
| `RenderForwardMultiPass` | 无 FBF/Subpass 且 HDR/MSAA | BasePass → ResolveSceneColor → Decals+Fog+Translucency 第二 RenderPass |
| `RenderDeferredMultiPass` | 不支持 Subpass 的 GLES/Metal 旧硬件 | BasePass(GBuffer) → HZB → Decals → SSXR → Lighting → Translucency 多 RenderPass |

> Deferred MultiPass 由于必须把 GBuffer 拷出主存读回，**移动端带宽代价巨大**，因此 Deferred 必须绑定到具备 `Vulkan/Metal/GLES_FBF/PLS` 的硬件上才有意义。

---

## 3. BasePass 的根本区别

### 3.1 CPU 端：`FMobileBasePassMeshProcessor`

```cpp
// MobileBasePass.cpp:826
FMobileBasePassMeshProcessor::FMobileBasePassMeshProcessor(...)
    , bDeferredShading       (IsMobileDeferredShadingEnabled(...))
    , bPassUsesDeferredShading(bDeferredShading && !bTranslucentBasePass)
{ }
```

> **`bPassUsesDeferredShading` = bDeferredShading 且 不是半透明 Pass**。
> 这说明：**Deferred 路径下的半透明仍走 Forward Shading**（在 Lighting 子 Pass 之后 subpass 读 GBuffer/Depth 做局部光照）。

#### 3.1.1 Lightmap Policy 选择差异

```cpp
// MobileBasePass.cpp:466
static FMobileLightMapPolicyTypeList GetUniformLightMapPolicyTypeForPSOCollection(
    bool bLitMaterial, bool bTranslucent, bool bUsesDeferredShading, bool bCanReceiveCSM, bool bMovable)
{
    ...
    if (!bUsesDeferredShading && !MobileUseCSMShaderBranch())
        Result.Add(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM);   // Forward 专属：BasePass 内部接收 CSM
    ...
    if (FReadOnlyCVARCache::MobileEnableStaticAndCSMShadowReceivers() && !bUsesDeferredShading)
        Result.Add(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_LIGHTMAP);
    if (!bUsesDeferredShading && bCanReceiveCSM)
        Result.Add(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_SH_INDIRECT);
    ...
}
```

> **Forward** 路径需要为"是否在 BasePass 内部应用 CSM"准备多种 Lightmap Permutation；
> **Deferred** 路径所有 CSM 都在独立 LightingPass 完成，BasePass 里只有 Lightmap / SH，**Shader Permutation 数量显著减少**。

#### 3.1.2 LocalLight Setting 仅 Forward 使用

```cpp
// MobileBasePass.cpp:1190
EMobileLocalLightSetting LocalLightSetting = EMobileLocalLightSetting::LOCAL_LIGHTS_DISABLED;
if (bLitMaterial && !bPassUsesDeferredShading)        // ← 关键判断
    LocalLightSetting = GetMobileForwardLocalLightSetting(ShaderPlatform);
```

含义：在 Forward 路径下，`r.Mobile.Forward.LocalLights` 的 0/1/2 值会切换 BasePass Shader 的 `MERGED_LOCAL_LIGHTS_MOBILE` 宏（0=禁用 / 1=合并到单点光纹理 / 2=LightGrid 集群多光源）；Deferred 路径下硬置为 DISABLED，因为这些光在外部 LightingPass 处理。

#### 3.1.3 BasePass Pixel Shader 的关键编译宏

```cpp
// MobileBasePassRendering.h:466 TMobileBasePassPSPolicyParamType::ModifyCompilationEnvironment
const bool bDeferredShadingEnabled = IsMobileDeferredShadingEnabled(Parameters.Platform);
const bool bIsTranslucent           = IsTranslucentBlendMode(...) || ShadingModels.HasShadingModel(MSM_SingleLayerWater);
const bool bMaterialUsesForwardShading = bIsLit && bIsTranslucent;
const bool bForwardShading          = !bDeferredShadingEnabled || bMaterialUsesForwardShading;

OutEnvironment.SetDefine(TEXT("ENABLE_SKY_LIGHT"),
    bIsLit && bForwardShading && bProjectSupportsNonStaticSkyLights);
OutEnvironment.SetDefine(TEXT("ENABLE_AMBIENT_OCCLUSION"),
    bForwardShading && IsMobileAmbientOcclusionEnabled(...));
OutEnvironment.SetDefine(TEXT("ENABLE_CLUSTERED_LIGHTS"),
    LocalLightSetting == EMobileLocalLightSetting::LOCAL_LIGHTS_ENABLED);
OutEnvironment.SetDefine(TEXT("MERGED_LOCAL_LIGHTS_MOBILE"), MergedLocalLights);
OutEnvironment.SetDefine(TEXT("ENABLE_CLUSTERED_REFLECTION"), bEnableClusteredReflections);
OutEnvironment.SetDefine(TEXT("USE_SHADOWMASKTEXTURE"), bMobileUsesShadowMaskTexture && !bTranslucentMaterial);
OutEnvironment.SetDefine(TEXT("ENABLE_DBUFFER_TEXTURES"), MaterialDomain == MD_Surface);
OutEnvironment.SetDefine(TEXT("MOBILE_SSR_ENABLED"), AreMobileScreenSpaceReflectionsEnabled(...));
```

```cpp
// MobileBasePassRendering.cpp:268 共享 ModifyCompilationEnvironment
const bool bDeferredShadingSubpass =
    bDeferredShadingEnabled && bTranslucentMaterial && !MaterialParameters.bIsMobileSeparateTranslucencyEnabled;
OutEnvironment.SetDefine(TEXT("IS_MOBILE_DEFERREDSHADING_SUBPASS"), bDeferredShadingSubpass);
```

> `IS_MOBILE_DEFERREDSHADING_SUBPASS=1` 在 Deferred + 同 Subpass 半透明时启用，允许半透明 Shader 通过 Framebuffer Fetch 读取已写入的 GBuffer/SceneColor，但这是 **半透明 PS** 而非延迟 LightingPass。

#### 3.1.4 Stencil 编码差异

```cpp
// MobileBasePassRendering.cpp:90 SetMobileBasePassDepthState
if (bUsesDeferredShading)
{
    // bit 1 = 'Render on Top'，bit 2 = LightMap
    StencilValue |= GET_STENCIL_MOBILE_SM_MASK(ShadingModel);
    StencilValue |= STENCIL_LIGHTING_CHANNELS_MASK(ProxyChannels);
}
else
{
    // Forward 路径多支持 ContactShadow
    StencilValue |= GET_STENCIL_BIT_MASK(MOBILE_CAST_CONTACT_SHADOW, CastsContactShadow);
}
```

> Deferred 必须把"光照通道 / ShadingModel"写进 Stencil，供后续 `MobileDeferredShadingPass` 用 EqualTest 做按光通道剔除；Forward 则有空间放 ContactShadow 标记（"TODO: ContactShadows do not work with deferred shading"）。

### 3.2 Shader 端：`MobileBasePassPixelShader.usf`

最关键的两条宏（第 113、115 行）：

```hlsl
// MOBILE_DEFERRED_SHADING 由项目层注入，全 Deferred 编译时为 1
#define MOBILE_USE_GBUFFER     (MOBILE_DEFERRED_SHADING && \
                                ((MATERIALBLENDING_SOLID || MATERIALBLENDING_MASKED) && \
                                 !MATERIAL_SHADINGMODEL_SINGLELAYERWATER))

#define DEFERRED_SHADING_PATH  (MOBILE_DEFERRED_SHADING && \
                                ((MATERIALBLENDING_SOLID || MATERIALBLENDING_MASKED) && \
                                 !MATERIAL_SHADINGMODEL_SINGLELAYERWATER))
```

派生宏：
- `MOBILE_USE_GBUFFER` = 1 → 输出 GBuffer，跳过 BasePass 直接打光逻辑
- `DEFERRED_SHADING_PATH` = 1 → 用于切换"Deferred 专属代码段"

#### 3.2.1 像素 Shader 输出签名差异

```hlsl
// MobileBasePassPixelShader.usf:371
#if MOBILE_USE_GBUFFER
    #if USE_GLES_FBF_DEFERRED
        out HALF4_TYPE OutProxy : SV_Target0     // 通过 framebuffer-fetch 当 emissive
    #else
        out HALF4_TYPE OutColor : SV_Target0     // emissive
    #endif
    out HALF4_TYPE OutGBufferA : SV_Target1      // Normal + ShadingModel
    out HALF4_TYPE OutGBufferB : SV_Target2      // Metallic/Specular/Roughness/SMID
    out HALF4_TYPE OutGBufferC : SV_Target3      // BaseColor + IndirectIrradiance
    #if MOBILE_DEFERRED_EXPORT_MRT
        out uint OutCharRenderMask : SV_Target4  // GR Toon 项目特有：角色 mask
    #endif
    #if MOBILE_EXTENDED_GBUFFER
        out HALF4_TYPE OutGBufferD : SV_Target5  // CustomData (非默认 ShadingModel)
    #endif
#else  // Forward 路径
    #if MOBILE_TRANSLUCENT_COLOR_TRANSMITTANCE_DUAL_SRC_BLENDING
        out HALF4_TYPE OutColor  DUAL_SOURCE_BLENDING_SLOT(0) : SV_Target0
        out HALF4_TYPE OutColor1 DUAL_SOURCE_BLENDING_SLOT(1) : SV_Target1
    #else
        out HALF4_TYPE OutColor : SV_Target0       // 直接的 HDR/sRGB 最终颜色
    #endif
#endif
```

`SV_TargetDepthAux` 位置随之偏移：

```hlsl
#if MOBILE_USE_GBUFFER && MOBILE_EXTENDED_GBUFFER
    #define SV_TargetDepthAux SV_Target5
#elif MOBILE_USE_GBUFFER
    #define SV_TargetDepthAux SV_Target4
#else
    #define SV_TargetDepthAux SV_Target1
#endif
```

> Forward 路径下 BasePass 的 `RT1` 用作 SceneDepthAux；Deferred 路径下 `RT1~RT3` 是 GBuffer，DepthAux 移到 `RT4/RT5`。

#### 3.2.2 Shader 主体的两条分支

```hlsl
// MobileBasePassPixelShader.usf:1086
#if MOBILE_USE_GBUFFER && !MATERIAL_SHADINGMODEL_UNLIT
    // Deferred BasePass: 只编码 GBuffer，不做光照
    GBuffer.IndirectIrradiance = IndirectIrradiance;
    MobileEncodeGBuffer(GBuffer, OutGBufferA, OutGBufferB, OutGBufferC, OutGBufferD);
#else
    // Forward BasePass: BRDF + 主光 + LocalLights + IBL + Fog + Lightmap + SkyLight
    AccumulateDirectionalLightingMobileToon(GBuffer, ...);
    #if MERGED_LOCAL_LIGHTS_MOBILE == 1
        // 取 LocalLightTextureA/B（合并光源贴图）
    #elif MERGED_LOCAL_LIGHTS_MOBILE == 2
        // LightGrid culling + 多光源逐像素
        MergeLocalLights(CulledLightGridHeader, ...);
    #endif
    AccumulateReflection(...);   // IBL/反射球
    // 雾、SkyLight、AO 均在 BasePass 内合成
    OutColor = SafeGetOutColor(Color);
#endif
```

> 由此可见 Forward BasePass PS **指令数远超** Deferred BasePass PS。但 Deferred 多出了独立 LightingPass 的全屏开销，对于光源数少/材质复杂度低的场景 Forward 仍然更划算。

#### 3.2.3 项目层的 Toon 兼容补丁

`DEFERRED_SHADING_PATH` 内部还有 `MATERIAL_SHADINGMODELS_TOON_CHARACTER` 例外（见 `MobileBasePassPixelShader.usf:1011`）：

```hlsl
#if DEFERRED_SHADING_PATH
    #if MATERIAL_SHADINGMODELS_TOON_CHARACTER
        // 即使在 Deferred 总路径下，Toon 角色仍走 Forward 的 BRDF
        DiffuseColor = lerp(DiffuseIndirectLighting*BaseColor, BaseColor, 0.3f);
        DiffuseColor = lerp(DiffuseColor*ShadowColor*ScaleEnergy, DiffuseColor, ShadowMaskCombine);
    #endif
#endif
```

并由 `MOBILE_CHARACTER_FORWARD` 在 `MobileBasePassPixelShader.usf:114` 控制是否把角色完全踢出 Deferred 路径——这是 GR Toon 项目的 Forward Char Pass 改造。

---

## 4. Lighting Pass：Deferred 独有

文件：`MobileDeferredShadingPass.cpp` + `MobileDeferredShading.usf`。Forward 路径中**没有**这个 Pass。

### 4.1 Pass 序列

`MobileDeferredShadingPass(RHICmdList, ViewIdx, NumViews, View, Scene, SortedLightSet, VisibleLightInfos, MobileSSRQuality)`：

```cpp
// MobileDeferredShadingPass.cpp:1212
RenderDirectionalLights(...);           // 平行光 + 可选 inline 反射/天光
if (!bMobileUseClusteredDeferredShading)
    RenderSimpleLights(...);            // 粒子 SimpleLight
for (StandardDeferredStart..UnbatchedLightStart) RenderLocalLight(...);  // 非阴影
for (UnbatchedLightStart..NumLights)   RenderLocalLight(...);  // 带阴影/LightFunction
```

### 4.2 关键 Shader Permutation

```cpp
// MobileDeferredShadingPass.cpp:80 FMobileDirectionalLightFunctionPS
class FEnableClustredLights    : SHADER_PERMUTATION_BOOL("ENABLE_CLUSTERED_LIGHTS");
class FEnableClustredReflection: SHADER_PERMUTATION_BOOL("ENABLE_CLUSTERED_REFLECTION");
class FEnablePlanarReflection  : SHADER_PERMUTATION_BOOL("ENABLE_PLANAR_REFLECTION");
class FEnableSkyLight          : SHADER_PERMUTATION_BOOL("ENABLE_SKY_LIGHT");
class FEnableCSM               : SHADER_PERMUTATION_BOOL("ENABLE_MOBILE_CSM");
class FShadowQuality           : SHADER_PERMUTATION_RANGE_INT("MOBILE_SHADOW_QUALITY", 1, 3);
class FMobileSSRQuality        : SHADER_PERMUTATION_ENUM_CLASS("MOBILE_SSR_QUALITY", EMobileSSRQuality);
class FLuxGIEnableAvoidLightLeaking: SHADER_PERMUTATION_BOOL("AVOID_LEAK_ENABLE");
class FUseShadowMaskTexture    : SHADER_PERMUTATION_BOOL("USE_SHADOWMASKTEXTURE");
class FUseLocalExposure        : SHADER_PERMUTATION_BOOL("USE_LOCAL_EXPOSURE");
```

`ShouldCompilePermutation` 强制约束：

```cpp
if (MaterialDomain != MD_LightFunction ||
    !IsMobilePlatform(Platform) ||
    !IsMobileDeferredShadingEnabled(Platform))
    return false;     // ← Forward 路径不会编译该 Shader
```

### 4.3 屏幕空间几何 + Stencil 剔除

`SetLocalLightRasterizerAndDepthState` 模板根据 `GMobileUseLightStencilCulling` 是否开启选择：

- **bWithStencilCulling = true**：先用 `RenderLocalLight_StencilMask` 把光源体在 `STENCIL_SANDBOX_MASK` 位置置 1（深度测试失败处），然后正式 LightingPass 对 `STENCIL_SANDBOX_MASK == 1` 同时光通道掩码相等的像素绘制，并把 STENCIL 清零便于下个光源复用——节省片元数量。
- **bWithStencilCulling = false**：根据相机是否在光体内部选 frontfaces / backfaces / 深度近端 / 深度远端等渲染策略，依赖 EarlyZ 剔除。

### 4.4 ClusteredDeferred 双路径

```cpp
static bool UseClusteredDeferredShading(const FStaticShaderPlatform Platform)
{
    // 需要 LightGrid（依赖 r.Mobile.Forward.EnableLocalLights=1，复用前向集群）
    return GMobileUseClusteredDeferredShading != 0 && MobileForwardEnableLocalLights(Platform);
}
```

> 即使是 Deferred，也可以把"批量"Local Light 走 Clustered Inline 着色，UnbatchedLight 才走 Stenciled Geometry。这是 5.5 之后的优化路径。

### 4.5 GBuffer 解码

无论 `MobileDirectionalLightPS` / `MobileRadialLightPS` / `MobileReflectionEnvironmentSkyLightingPS`，第一行都是：

```hlsl
// MobileDeferredShading.usf:205
FGBufferData GBuffer = MobileFetchAndDecodeGBuffer(UVAndScreenPos.xy, UVAndScreenPos.zw);
```

通过 framebuffer-fetch 或 SRV 读出 GBuffer 后重建 `FGBufferData`，再走完整 PC-like 的 `AccumulateDirectionalLighting` / `AccumulateDynamicLighting`。

### 4.6 USE_GLES_FBF_DEFERRED 特殊输出

```hlsl
// MobileDirectionalLightPS / MobileRadialLightPS / MobileReflectionEnvironmentSkyLightingPS
#if USE_GLES_FBF_DEFERRED
    out HALF4_TYPE OutProxyAdditive : SV_Target0
    out HALF4_TYPE OutGBufferA : SV_Target1   // 写回相同 binding（保留 attachment）
    out HALF4_TYPE OutGBufferB : SV_Target2
    out HALF4_TYPE OutGBufferC : SV_Target3
#else
    out HALF4_TYPE OutColor : SV_Target0
#endif
```

GLES 下因为 PLS / FBF 模型必须保持 attachment 列表一致，LightingPS 也必须声明 GBuffer 输出（值不变）才能继续 fetch。

---

## 5. GBuffer 布局（Deferred 专属）

源码：`GBufferInfo.cpp:585`

```cpp
if (bUsingPixelLocalStorage)
{
    Info.NumTargets = 1;    // GLES PLS 模式只暴露 1 个 RT，GBuffer 在 on-chip PLS
}
else
{
    Info.NumTargets = 4;    // SceneColor + GBufferA/B/C
    if (MobileUsesExtenedGBuffer(Params.ShaderPlatform))
        Info.NumTargets++;  // + GBufferD
}
```

| Target | 内容 | 备注 |
|--------|------|------|
| RT0 SceneColor | Emissive (+ 主光在合并 RP 后) | Subpass 后才被 LightingPass 写 |
| RT1 GBufferA | WorldNormal(xyz) + PerObjectGBufferData(w) | PF_A2B10G10R10 / FloatRGBA |
| RT2 GBufferB | Metallic, Specular, Roughness, ShadingModelID+SelectiveOutputMask | RGBA8 |
| RT3 GBufferC | BaseColor(rgb) + GBufferAO(a) / IndirectIrradiance | RGBA8 sRGB |
| RT4 GBufferD | CustomData（仅 ExtenedGBuffer） | 非默认 ShadingModel 用 |
| RTn SceneDepthAux | Auxiliary depth | 仅特定平台 |

> ⚠️ **移动端 GBuffer 不像 PC 端那么"重"**：默认没有 GBufferE / VelocityBuffer / GBufferF；项目里还增加了 `OutCharRenderMask`（GR Toon 项目）。

GBuffer 编/解码集中在：`DeferredShadingCommon.ush` 的 `MobileEncodeGBuffer` / `MobileFetchAndDecodeGBuffer`。

---

## 6. 阴影系统差异

### 6.1 CSM（级联阴影）

| 维度 | Forward | Deferred |
|------|---------|----------|
| 计算位置 | **BasePass PS** 内 | **MobileDeferredShadingPass** 的 DirectionalLightPS |
| 物体级剔除 | `MobileBasePassCSM` 专用 MeshPass，按 CSM 接收性区分 Permutation | 不剔除（`MobileBasePassAlwaysUsesCSM == true`） |
| Shader Permutation | 多组 LightmapPolicy × CSM 状态 | 单一组 BasePass PS，CSM 全压到 LightingPS |
| 优化点 | 静态/CSM 合并贴图 `r.Mobile.EnableStaticAndCSMCombinedShadow` | 单 DirectionalLightPS 内做 PCF |

### 6.2 ShadowMask Texture

```cpp
// MobileShadingRenderer.cpp:687
bRequiresShadowProjections = MobileUsesShadowMaskTextureRuntime(ShaderPlatform) && ...;
// 调度
if (bRequiresShadowProjections) RenderMobileShadowProjections(GraphBuilder);
```

> Forward 路径下 ShadowMask 通常用于 LocalLight 阴影、距离场阴影、PointLight 阴影、CapsuleShadow，**作为离线纹理供 BasePass 采样**。
> Deferred 路径下 `MobileDirectionalLightFunctionPS::FUseShadowMaskTexture` Permutation 也支持读 ShadowMask，但在 LightingPS 中处理。

### 6.3 调制阴影

```cpp
// RenderForwardSinglePass()
RenderModulatedShadowProjections(RHICmdList, ViewIdx, View);
```

Deferred 路径**未调用** `RenderModulatedShadowProjections`：它本质上是一种古老的 Static + CSM 混合方案，仅 Forward 路径仍保留。

### 6.4 SpotLight Movable Shadow

`FMobileRadialLightFunctionPS::FSpotLightShadowDim` Permutation 仅在 `IsMobileMovableSpotlightShadowsEnabled(Platform)` 为 true 时编译，**Deferred 专属**。

---

## 7. 反射系统差异

### 7.1 BasePass 内置反射（Forward）

```hlsl
// MobileBasePassPixelShader.usf:1152
#if MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID || ... 
    AccumulateReflection(GBuffer, SvPosition, CameraVector, ...,
                         ReflectionVector, RoughReflectionLighting, GridIndex, DirectLighting);
#endif
```

- LQ 路径：单 ReflectionCubemap
- HQ 路径：`MATERIAL_HQ_FORWARD_REFLECTIONS && !MOBILE_QL_FORCE_LQ_REFLECTIONS`，最多 3 个 Cubemap 距离加权混合
- LightGrid 反射：`ENABLE_CLUSTERED_REFLECTION` 开启时通过 LightGrid 读取候选 capture

### 7.2 LightingPass 反射（Deferred）

```cpp
// MobileDeferredShadingPass.cpp:488 RenderReflectionEnvironmentSkyLighting
// 与单 DirectionalLight inline 合并时通过 bInlineReflectionAndSky 跳过独立 Pass
TShaderMapRef<FMobileReflectionEnvironmentSkyLightingPS> PixelShader(View.ShaderMap, PermutationVector);
```

- 默认尝试与 DirectionalLight 合 Pass（`bInlineReflectionAndSky`），减少 全屏绘制次数
- `MobileSSRQuality != Disabled` 时强制拆 Pass（用于按 Stencil mask 区分高/低 SSR 质量）
- 支持 ScreenSpaceXReflection (`bRequiresSSXR`)，本帧 SceneColor + 上帧 SceneColor 做反射

### 7.3 PixelProjectedPlanarReflection

```cpp
bRequiresPixelProjectedPlanarRelfectionPass = IsUsingMobilePixelProjectedReflection(ShaderPlatform) && ...;
// 调度
if (bRequiresPixelProjectedPlanarRelfectionPass)
    RenderPixelProjectedReflection(GraphBuilder, ..., SceneTextures.PixelProjectedReflection, ...);
```

> 两条路径都支持，但在 Forward 单 RenderPass 中是 BasePass 后立即执行；Deferred 把它放在 SSXR 后、LightingPass 前。

---

## 8. 局部光源（Point/Spot/Rect）差异

| 项目 | Forward Forward+Clustered | Deferred |
|------|--------------------------|----------|
| 数据准备 | `GatherAndSortLights` → `ComputeLightGrid` | 同 + `SortedLightSet` 拆 batched/unbatched |
| Shader Permutation | `ENABLE_CLUSTERED_LIGHTS` + `MERGED_LOCAL_LIGHTS_MOBILE` | `FMobileRadialLightFunctionPS` 多个枚举 |
| 编译条件 | `bForwardShading && bIsLit` 才编译 | `IsMobileDeferredShadingEnabled` 才编译 |
| 阴影 | 通常需要 ShadowMaskTexture | 独立 LightPS + StencilCulling，**可直接做 PCF** |
| 上限 | LightGrid 容量限制；旧路径硬编 `MAX_DYNAMIC_POINT_LIGHTS=4` | 仅显存/带宽限制 |
| LightFunction | Forward 一般不支持（需要 LightGrid 扩展） | 原生支持（`USE_LIGHT_FUNCTION` Permutation） |
| IES Profile | 旧路径不支持 | `FIESProfileDim` Permutation 支持 |

> Mobile Forward 多光源支持是后期通过 LightGrid + Cluster 改造叠加的（参考 4.27 的 Mobile Forward Cluster），原生最多 4 个点光。Mobile Deferred 自带光源数量无硬上限。

---

## 9. 半透明渲染差异

```cpp
// MobileBasePassRendering.cpp:267
const bool bDeferredShadingSubpass =
    bDeferredShadingEnabled && bTranslucentMaterial && !bIsMobileSeparateTranslucencyEnabled;
OutEnvironment.SetDefine(TEXT("IS_MOBILE_DEFERREDSHADING_SUBPASS"), bDeferredShadingSubpass);
```

| 维度 | Forward | Deferred |
|------|---------|----------|
| Pass 位置 | DepthReadSubpass 中 `RenderTranslucency` | DeferredShadingSubpass 之后同 RenderPass 中 `RenderTranslucency` |
| Shading | Forward Shading（BasePass PS 完整光照） | **仍然 Forward Shading**（半透明无法 GBuffer） |
| 读 SceneColor/Depth | DepthRead via subpass | DepthRead + 可读 GBuffer via FBF |
| MSAA 行为 | MSAA 颜色通过解析；半透明在解析前 | 通常无 MSAA（Deferred 与 MSAA 不兼容） |
| Separate Translucency | `IsMobileSeparateTranslucencyActive` 时拆 RenderPass | 通常关闭 |
| ColorTransmittance | 支持 Dual / Programmable / Single src 三种 | 同样支持 |

---

## 10. 贴花（Decal）差异

| 类型 | Forward | Deferred |
|------|---------|----------|
| DBuffer Decal | 支持（`bRequiresDBufferDecals = IsUsingDBuffers(ShaderPlatform)`） | **强制禁用**（`bRequiresDBufferDecals = false`） |
| 网格贴花 | `RenderDecals` 在 DepthRead subpass | `RenderDecals` 在 GBuffer write subpass（可修改 GBuffer） |
| 屏幕空间 SceneColor Decal | 支持 | 支持 |
| Decal 写 GBuffer 通道 | 不存在 GBuffer | 写 BaseColor/Normal/Roughness 等 |

`EMeshPass::MeshDecal_SceneColor` + `EMeshPass::MeshDecal_SceneColorAndGBuffer` 这两个 Pass 在 Deferred 路径才会有第二种生效。

---

## 11. 后处理 / Tonemap 差异

```cpp
// FMobileSceneRenderer::FMobileSceneRenderer 构造
bTonemapSubpass        = IsMobileTonemapSubpassEnabled(ShaderPlatform, ...) && ViewFamily.bResolveScene && ...;
bTonemapSubpassInline  = IsMobileTonemapSubpassEnabledInline(ShaderPlatform, ...) && bTonemapSubpass;
bRequiresSceneDepthAux = MobileRequiresSceneDepthAux(ShaderPlatform) && !bTonemapSubpass;
```

| 维度 | Forward | Deferred |
|------|---------|----------|
| Inline Tonemap | 支持（CustomResolveSubpass，仅 Vulkan） | 不支持 |
| MSAA | 支持（GLES SubpassMSAA / MetalMSAA） | 不支持（GBuffer 与 MSAA 几乎不兼容） |
| SceneDepthAux | HDR 时需要（GLES/Vulkan） | 仅极端情况下需要 |
| KeepDepthContent | 复杂条件 | `bKeepDepthContent` 多了 `bPostProcessUsesSceneDepth || bSceneDepthCapture` 条件 |
| Local Exposure | 后处理阶段做 | 可选在 LightingPass inline（`r.Mobile.LocalExposure=1`） |

```cpp
// MobileShadingRenderer.cpp:721
(bDeferredShading && bPostProcessUsesSceneDepth) ||
(bDeferredShading && bSceneDepthCapture) ||
// → 触发保留深度内容
```

---

## 12. Forward 路径独有功能

- **Static + CSM 合并阴影贴图**（`r.Mobile.EnableStaticAndCSMCombinedShadow`）
- **MSAA**（2x/4x）
- **DBuffer Decals**
- **MobileInlineTonemap + CustomResolve**（仅 Vulkan，省一次离屏）
- **ContactShadow Stencil bit**
- **MOBILE_QL_FORCE_FULLY_ROUGH / NONMETAL / LQ_REFLECTIONS** 等质量档位裁剪（裁的全是 BasePass PS）
- **`MobileBasePassCSM` 专属 MeshPass**
- **Particle SimpleLight**（除非 `MobileForwardEnableParticleLights` 显式打开）

## 13. Deferred 路径独有功能

- **`MobileDeferredShadingPass` Lighting 子 Pass**
- **GBuffer 写入 + 解码 (`MobileEncodeGBuffer` / `MobileFetchAndDecodeGBuffer`)**
- **LightFunction 真正生效**（`FMobileDirectionalLightFunctionPS::FParameters.LightFunctionParameters`）
- **IES Profile**（`FMobileRadialLightFunctionPS::FIESProfileDim`）
- **SSR / SSXR**（`AreMobileScreenSpaceReflectionsEnabled`，Forward 路径下需特殊改造）
- **LuxGI**（项目特性：`PlatformSupportLuxGI` 要求 Deferred 或 SM5+）
- **Local Exposure inline 到 LightingPass**（`r.Mobile.LocalExposure=1`）
- **ClusteredDeferredShading**（`r.Mobile.UseClusteredDeferredShading=1`，复用前向 LightGrid）
- **Stencil 光体剔除**（`r.Mobile.UseLightStencilCulling=1`）
- **Pixel Local Storage 模式**（Android GLES，避免 GBuffer 出 Tile）

---

## 14. 关键 Shader 宏与 Permutation 总汇

### 14.1 决定 Path 的宏

| 宏 | 含义 | 来源 |
|----|------|------|
| `MOBILE_DEFERRED_SHADING` | 全 Deferred 编译开关 | 项目层 Inject + `LuxGIVisualize.cpp:97` |
| `MOBILE_USE_GBUFFER` | 当前材质走 GBuffer 输出 | `MOBILE_DEFERRED_SHADING && BlendMode==SOLID/MASKED && !SingleLayerWater` |
| `DEFERRED_SHADING_PATH` | Shader 内部 Deferred 分支 | 同上（项目里再排除 Toon Character） |
| `MOBILE_EXTENDED_GBUFFER` | 是否输出 GBufferD | `MobileUsesExtenedGBuffer(Platform)` |
| `MOBILE_DEFERRED_LIGHTING` | LightingPass Shader | `MobileDeferredShading.usf:3` |
| `IS_MOBILE_DEFERREDSHADING_SUBPASS` | 半透明在 Deferred subpass 内可读 GBuffer | `MobileBasePassRendering.cpp:268` |
| `IS_MOBILE_DEPTHREAD_SUBPASS` | Forward 半透明能读 Depth | `MobileBasePassRendering.cpp:265` |
| `USE_GLES_FBF_DEFERRED` | GLES 走 FBF 模式 Deferred | 与平台相关 |
| `MOBILE_DEFERRED_EXPORT_MRT` | 项目特有：导出 RenderMask | GR 项目 |

### 14.2 仅 Forward BasePass 使用

| 宏 | 含义 |
|----|------|
| `ENABLE_SKY_LIGHT` | 仅 `bForwardShading` 时 true |
| `ENABLE_AMBIENT_OCCLUSION` | 仅 `bForwardShading` 时 true（Deferred AO 在 LightingPS） |
| `ENABLE_CLUSTERED_LIGHTS` | LightGrid 多光源 |
| `ENABLE_CLUSTERED_REFLECTION` | LightGrid 反射 |
| `MERGED_LOCAL_LIGHTS_MOBILE` | 0/1/2 模式 |
| `USE_SHADOWMASKTEXTURE` | 屏幕空间 ShadowMask |
| `ENABLE_DBUFFER_TEXTURES` | DBuffer Decal 输入 |
| `MAX_DYNAMIC_POINT_LIGHTS` / `VARIABLE_NUM_DYNAMIC_POINT_LIGHTS` | 旧 4 点光路径 |
| `MOBILE_CSM_QUALITY` | CSM 滤波质量档 |
| `HQ_REFLECTIONS` | 3 反射球混合 |

### 14.3 仅 Deferred LightingPS 使用

| 宏 | 含义 |
|----|------|
| `ENABLE_MOBILE_CSM` | DirectionalLightPS CSM |
| `MOBILE_SHADOW_QUALITY` | 1/2/3 PCF 档 |
| `MOBILE_SSR_QUALITY` | EMobileSSRQuality |
| `SUPPORT_SPOTLIGHTS_SHADOW` | Spot 阴影 |
| `RADIAL_LIGHT_TYPE` | LightType_Point/Spot/Rect |
| `USE_IES_PROFILE` | IES |
| `LIGHT_SOURCE_SHAPE` | Capsule/Rect/Directional |
| `USE_LIGHT_FUNCTION` | LightFunction Material |
| `USE_LOCAL_EXPOSURE` | Inline 局部曝光 |
| `AVOID_LEAK_ENABLE` | LuxGI 防漏光 |

### 14.4 Quality Level 通用宏（影响 BasePass PS）

| 宏 | 影响 |
|----|------|
| `MOBILE_QL_FORCE_FULLY_ROUGH` | 跳过 IBL / Specular，省 ALU |
| `MOBILE_QL_FORCE_NONMETAL` | F0 固定为非金属 |
| `MOBILE_QL_FORCE_LQ_REFLECTIONS` | 单 Cubemap |
| `MOBILE_QL_FORCE_DISABLE_PREINTEGRATEDGF` | 跳过 PreIntegratedGF |
| `MOBILE_QL_DISABLE_MATERIAL_NORMAL` | 强制 VertexNormal |

> Quality Level 主要在 **Forward 路径裁 BasePass PS 指令**；Deferred 路径下 BasePass 只写 GBuffer，几乎不受这些宏影响（影响转移到 LightingPS）。

---

## 15. CVar 速查

| CVar | 默认 | 影响路径 | 含义 |
|------|------|---------|------|
| `r.Mobile.ShadingPath` | 0 | 总开关 | 0=Forward / 1=Deferred |
| `r.Mobile.Forward.EnableLocalLights` | 0 | Forward | 0/1/2: 禁/集群/合并贴图 |
| `r.Mobile.Forward.LocalLightsSinglePermutation` | 0 | Forward | 减少 LocalLight Permutation |
| `r.Mobile.Forward.EnableClusteredReflections` | 0 | Forward | LightGrid 反射球 |
| `r.Mobile.UseClusteredDeferredShading` | 0 | Deferred | LightGrid 集群 Inline 着色（需 LocalLights=1） |
| `r.Mobile.UseLightStencilCulling` | 1 | Deferred | Stencil 剔除局部光像素 |
| `r.Mobile.IgnoreDeferredShadingSkyLightChannels` | 0 | Deferred | 忽略 LightChannel 提升性能 |
| `r.Mobile.LocalExposure` | 2 | Deferred | 1=LightPass 内 / 2=PP |
| `r.Mobile.ScreenSpaceReflections` | 0 | Deferred | SSR |
| `r.Mobile.DBuffer` | 0 | Forward | Deferred 时强制 0 |
| `r.Mobile.AmbientOcclusion` | 0 | Forward 主用 | Deferred AO 在 LightingPS 处理 |
| `r.Mobile.AllowSoftwareOcclusion` | 0 | 两者 | CPU 软件遮挡 |
| `r.Mobile.EnableNoPrecomputedLighting` | - | 两者 | 跳过 lightmap |
| `r.Mobile.EarlyZPass` | - | 两者 | 1 时 `MobileUsesFullDepthPrepass` |
| `r.Mobile.Shadow.CSMShaderCullingMethod` | - | Forward | Deferred 永远 always-CSM |
| `r.MobileMSAA` | 1 | Forward | Deferred 不支持 |
| `r.Mobile.ForceDepthResolve` | 0 | 两者 | PowerVR 兼容 |

---

## 16. 关键调用链汇总

### Forward 单 Pass（理想情况：Vulkan / Metal）

```
FMobileSceneRenderer::Render
└─ InitViews / Shadow / GatherAndSortLights / ComputeLightGrid(opt)
└─ RenderShadowDepthMaps
└─ RenderMobileShadowProjections (opt, bRequiresShadowProjections)
└─ RenderForward
   └─ RenderForwardSinglePass    (单 RenderPass + 多 Subpass)
      ├─ Subpass0: MaskedPrePass → MobileBasePass(★ 内含 BRDF/CSM/IBL/LocalLights/Fog)
      ├─ Subpass1: Decals → ModulatedShadows → Fog → Translucency
      └─ Subpass2 (opt): CustomResolve (Tonemap inline)
└─ PostProcess
```

### Deferred 单 Pass（理想情况：Vulkan + 较新硬件）

```
FMobileSceneRenderer::Render
└─ InitViews / Shadow / GatherAndSortLights / ComputeLightGrid
└─ RenderShadowDepthMaps
└─ RenderMobileShadowProjections (ShadowMaskTexture 用)
└─ RenderMobileLocalLightsBuffer (合并光源贴图，opt)
└─ RenderDeferredSinglePass     (单 RenderPass + 3 个 Subpass)
   ├─ Subpass0: MaskedPrePass → MobileBasePass(★ 只写 GBuffer)
   ├─ Subpass1: Decals (写 GBuffer)
   └─ Subpass2: MobileDeferredShadingPass(★★ Directional + Local + Reflection + SkyLight)
              + RenderFog + RenderTranslucency
└─ PostProcess (Tonemap)
```

---

## 17. 性能/带宽对比要点

| 指标 | Forward | Deferred |
|------|---------|----------|
| Tile Memory 占用 | SceneColor + Depth(+DepthAux) ≈ 64~128 bit/pixel | SceneColor + Depth + 3~4×32bit GBuffer ≈ 192~256 bit/pixel |
| 单 RenderPass 可行性 | 几乎所有平台 | 仅 FBF/PLS/Subpass 平台 |
| BasePass PS 指令数 | 高（材质+光照编译一起） | 低（仅 GBuffer 编码） |
| LightingPass 全屏开销 | 0 | 1 个 DirectionalLightPS + N 个 LocalLight + 反射/天光 |
| 多光源扩展性 | 4(原生) / LightGrid 16-128(集群) | 不限（受带宽） |
| MSAA | ✅ | ❌ |
| Tonemap inline | ✅ | ❌ |
| Lightmap 支持 | ✅（多 Lightmap Permutation） | ✅（但 Permutation 大幅缩减） |
| LightFunction | ❌ | ✅ |
| IES Profile | ❌ | ✅ |
| SSR | ❌（默认） | ✅（按 Stencil 分质量） |
| GI（LuxGI 项目） | ❌（PlatformSupportLuxGI 限制） | ✅ |

---

## 18. 结论 & 选择建议

1. **多反射球 / 重材质 / 静态光照** → **Forward**。BasePass 一次性算完，没有 LightingPass 全屏开销，且能享受 MSAA、Inline Tonemap、DBuffer Decal。
2. **多动态光源 / 写实 PBR / 需要 GI/SSR** → **Deferred**。LightingPass + Stencil 剔除可摊薄 N×全屏代价；GBuffer 让 LightFunction / SSR / GI 成本可控。
3. **角色玩法品类** → 项目实践中常见做法是 "Deferred 主路径 + Forward Character Pass"（即本工程的 `MOBILE_CHARACTER_FORWARD` 改造），既享受 Deferred 多光照，又避免 Toon 角色被 GBuffer 量化精度损失。
4. **必须监控**：Deferred 在不支持 FBF/PLS 的 GLES 旧机型上会退化为 MultiPass，带宽剧增；上线前必须排查 `RequiresMultiPass()` 结果。

---

## 19. 索引：本工程核心文件列表

### C++

| 文件 | 关键符号 |
|------|---------|
| `MobileShadingRenderer.cpp/.h` | `FMobileSceneRenderer::Render / RenderForward(Single|Multi)Pass / RenderDeferred(Single|Multi)Pass` |
| `MobileBasePass.cpp` | `FMobileBasePassMeshProcessor`, `SelectMeshLightmapPolicy`, `SetOpaqueRenderState` |
| `MobileBasePassRendering.cpp/.h` | `TMobileBasePassPS`, `SetupMobileBasePassUniformParameters`, `MobileBasePassModifyCompilationEnvironment` |
| `MobileDeferredShadingPass.cpp/.h` | `MobileDeferredShadingPass`, `FMobileDirectionalLightFunctionPS`, `FMobileRadialLightFunctionPS`, `FMobileReflectionEnvironmentSkyLightingPS` |
| `RenderUtils.cpp` | `IsMobileDeferredShadingEnabled`, `MobileUsesExtenedGBuffer`, `MobileForwardEnableLocalLights`, `MobileBasePassAlwaysUsesCSM`, `MobileRequiresSceneDepthAux` |
| `GBufferInfo.cpp` | `FetchMobileGBufferInfo` |
| `SceneTextures.cpp` | GBufferA/B/C/D 创建 |
| `ShadowProjectionPixelShader.usf` & `MobileShadowProjection.cpp` | `RenderMobileShadowProjections` |

### USF/USH

| 文件 | 关键内容 |
|------|---------|
| `MobileBasePassPixelShader.usf` | `MOBILE_USE_GBUFFER / DEFERRED_SHADING_PATH` 双分支，`MobileEncodeGBuffer` 或 完整 BRDF |
| `MobileBasePassVertexShader.usf` | WPO / VertexFog |
| `MobileBasePassCommon.ush` | 共享插值器 |
| `MobileDeferredShading.usf` | `MobileDirectionalLightPS / MobileRadialLightPS / MobileReflectionEnvironmentSkyLightingPS` |
| `MobileDeferredUtils.usf` | `MobileDeferredCopyPLSPS / MobileDeferredCopyDepthPS` |
| `DeferredShadingCommon.ush` | `MobileEncodeGBuffer / MobileFetchAndDecodeGBuffer` |
| `ToonMobileLightingCommon.ush` (项目) | `AccumulateDirectionalLightingMobileToon` |
| `ReflectionEnvironmentShared.ush` | `AccumulateReflection` |
| `ShadowFilteringCommon.ush` | `MobileShadowPCF` |
| `LightmapCommon.ush` | LQ/HQ Lightmap |
| `BRDF.ush` | `PhongApprox` / `EnvBRDF` |
| `DynamicLightingCommon.ush` | `AccumulateDynamicLighting` |

---

## 附录 A：典型一帧对照表

| 阶段 | Forward 调度 | Deferred 调度 | 共享 |
|------|-------------|---------------|------|
| 0 | `InitViews` / GPU Scene | 同 | ✅ |
| 1 | Shadow Depth | 同 | ✅ |
| 2 | `RenderMobileShadowProjections`(opt) | `RenderMobileShadowProjections`(用于 ShadowMaskTexture) | 大体一致 |
| 3 | `GatherAndSortLights`(若 Cluster) | `GatherAndSortLights` + `ComputeLightGrid` | 局部 |
| 4 | `RenderForward(Single/Multi)Pass` | `RenderDeferred(Single/Multi)Pass` | × |
| 4.1 | MaskedPrePass | MaskedPrePass | ✅ |
| 4.2 | **MobileBasePass(★ 完整光照)** | **MobileBasePass(★ 只写 GBuffer)** | × |
| 4.3 | `NextSubpass` → Decals | `NextSubpass` → Decals(写 GBuffer) | × |
| 4.4 | ModulatedShadows + Fog | – | × |
| 4.5 | – | `NextSubpass` → **MobileDeferredShadingPass(★★)** | × |
| 4.6 | Translucency | Fog + Translucency | 微差 |
| 4.7 | CustomResolveSubpass(opt) | – | × |
| 5 | Post Process | Post Process | ✅ |

> ★ = BasePass 核心；★★ = Deferred LightingPass 核心。

---

## 附录 B：开发者快速排查清单

1. **某材质在 Deferred 下颜色丢失** → 检查是否走了 `MOBILE_USE_GBUFFER` 路径，是否在 `MobileEncodeGBuffer` 后 emissive 被覆盖。
2. **半透明读不到 GBuffer** → 检查 `IS_MOBILE_DEFERREDSHADING_SUBPASS` 是否为 1，材质是否 `bIsMobileSeparateTranslucencyEnabled`。
3. **Deferred 下 LocalLight 不打** → 检查 Stencil（ShadingModel/LightChannel bits）、`r.Mobile.UseLightStencilCulling`、`SortedLightSet`。
4. **Forward 下多光源不够亮** → 检查 `r.Mobile.Forward.EnableLocalLights`、`MERGED_LOCAL_LIGHTS_MOBILE`、`LightGrid` cell 容量。
5. **MSAA 黑屏** → MSAA 仅 Forward 路径生效；Deferred 时硬性返回单采样。
6. **GLES 老机型 Deferred 卡顿** → `RequiresMultiPass()` 返回 true，GBuffer 走主存。考虑回退 Forward 或限制目标平台。
7. **DBuffer Decal 不显示** → Deferred 强制关闭 DBuffer，使用屏幕空间 Decal。
8. **CSM 在 Deferred 边缘有错** → 全部 mesh 默认接收 CSM (`MobileBasePassAlwaysUsesCSM`)，需要在 LightingPS 内调试。
9. **角色拉花/丢细节** → 项目 `MOBILE_CHARACTER_FORWARD` 把角色排除 Deferred；调试时优先关该宏。

---

> **文档结束**。后续如需细化某一阶段（例如 LightGrid 构建、ShadowMaskTexture 生成、SSXR 帧间数据流），请按"索引"中文件继续展开。
