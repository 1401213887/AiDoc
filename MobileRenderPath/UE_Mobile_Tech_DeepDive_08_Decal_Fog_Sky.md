# UE Mobile Forward vs Deferred —— 深度补充 08：Decal / Fog / Sky / Atmosphere

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**Decal（DBuffer + Deferred + MeshDecal）/ HeightFog / VolumetricFog / LocalFogVolume / SkyAtmosphere / VolumetricCloud** 在双管线下的差异。

---

## 1. Decal 系统全谱

| Decal 类型 | Forward | Deferred | 说明 |
|-----------|---------|----------|------|
| DBuffer Decal | ✅ | ❌（强制关闭） | BeforeBasePass，影响材质属性 |
| Deferred Decal | ✅（Emissive only） | ✅（写 GBuffer） | 屏幕空间投影 |
| Mesh Decal SceneColor | ✅ | ✅ | 网格贴花 |
| Mesh Decal SceneColorAndGBuffer | ❌ | ✅ | 修改 GBuffer 的网格贴花 |
| Atlas Decal | ✅ | ✅ | 项目层优化 |

---

## 2. `DoesPlatformSupportDecals` 平台检测

源码：`MobileDecalRendering.cpp:25-44`

```cpp
static bool DoesPlatformSupportDecals(EShaderPlatform ShaderPlatform)
{
    if (!IsMobileHDR()) {
        // Vulkan uses sub-pass to fetch SceneDepth
        if (IsVulkanPlatform(ShaderPlatform) ||
            IsSimulatedPlatform(ShaderPlatform) ||
            // Some Androids support SceneDepth fetch
            (IsAndroidOpenGLESPlatform(ShaderPlatform) && GSupportsShaderDepthStencilFetch))
        {
            return true;
        }
        // Metal needs DepthAux to fetch depth, and its not availle in LDR mode
        return false;
    }
    // HDR always supports decals
    return true;
}
```

> **LDR + Metal 移动端 → Decal 全部失效**。其他组合大多支持。

---

## 3. `FMobileSceneRenderer::RenderDecals` 主路径

源码：`MobileDecalRendering.cpp:46-86`

```cpp
void FMobileSceneRenderer::RenderDecals(FRHICommandList& RHICmdList, FViewInfo& View)
{
    if (!DoesPlatformSupportDecals(View.GetShaderPlatform())
        || !ViewFamily.EngineShowFlags.Decals
        || View.bIsPlanarReflection)
    {
        return;
    }

    const bool bIsMobileDeferred = IsMobileDeferredShadingEnabled(View.GetShaderPlatform());
    const EDecalRenderStage DecalRenderStage = bIsMobileDeferred
        ? EDecalRenderStage::MobileBeforeLighting
        : bRequiresDBufferDecals ? EDecalRenderStage::Emissive
                                 : EDecalRenderStage::Mobile;
    const EDecalRenderTargetMode RenderTargetMode = bIsMobileDeferred
        ? EDecalRenderTargetMode::SceneColorAndGBuffer
        : EDecalRenderTargetMode::SceneColor;

    // Deferred decals
    if (Scene->Decals.Num() > 0) {
        RenderDeferredDecalsMobile(RHICmdList, *Scene, View, DecalRenderStage, RenderTargetMode);
    }

    EMeshPass::Type DecalMeshPassType = DecalRendering::GetMeshPassType(RenderTargetMode);
    if (View.ParallelMeshDrawCommandPasses[DecalMeshPassType].HasAnyDraw()) {
        ...
        View.ParallelMeshDrawCommandPasses[DecalMeshPassType].Draw(RHICmdList, InstanceCullingDrawParams);
    }
}
```

### 3.1 三档 DecalRenderStage

| 路径 | DecalRenderStage | 含义 |
|------|-----------------|------|
| Deferred | `MobileBeforeLighting` | 在 LightingPass 之前修改 GBuffer |
| Forward + DBuffer | `Emissive` | 仅写 Emissive 通道 |
| Forward (no DBuffer) | `Mobile` | 标准 Mobile Decal |

### 3.2 EDecalRenderTargetMode

| Mode | 目标 RT |
|------|--------|
| `SceneColor` | 仅 SceneColor |
| `SceneColorAndGBuffer` | SceneColor + GBufferA/B/C |
| `DBuffer_AlwaysWrite` | DBuffer RT |

---

## 4. Stencil Decal 接收性

```cpp
// MobileDecalRendering.cpp:131-144
if (bInsideDecal) {
    GraphicsPSOInit.DepthStencilState = TStaticDepthStencilState<
        false, CF_Always,
        true, CF_Equal, SO_Keep, SO_Keep, SO_Keep,
        false, CF_Always, SO_Keep, SO_Keep, SO_Keep,
        GET_STENCIL_BIT_MASK(RECEIVE_DECAL, 1), 0x00>::GetRHI();
} else {
    GraphicsPSOInit.DepthStencilState = TStaticDepthStencilState<
        false, CF_DepthNearOrEqual,
        true, CF_Equal, SO_Keep, SO_Keep, SO_Keep,
        false, CF_Always, SO_Keep, SO_Keep, SO_Keep,
        GET_STENCIL_BIT_MASK(RECEIVE_DECAL, 1), 0x00>::GetRHI();
}
```

> **Decal 通过 Stencil bit `RECEIVE_DECAL` 区分**：BasePass 写入 `RECEIVE_DECAL=1`，Decal 渲染时 `CF_Equal` 测试，仅接收 Decal 的物体被写入。Stencil ref `bInsideDecal` 决定是否有深度测试。

### 4.1 RECEIVE_DECAL Stencil bit 设置

```cpp
// MobileBasePassRendering.cpp:102-103
uint8 ReceiveDecals = (PrimitiveSceneProxy && !PrimitiveSceneProxy->ReceivesDecals() ? 0x01 : 0x00);
StencilValue |= GET_STENCIL_BIT_MASK(RECEIVE_DECAL, ReceiveDecals);
```

> 注意逻辑：`!ReceivesDecals → bit=1`，表示**不接收 Decal**。Decal 渲染时 ref=0 与 stencil bit=0 时通过测试（接收 Decal）。

---

## 5. DBuffer Decal（仅 Forward）

源码：`MobileDecalRendering.cpp:156-177`

```cpp
void FMobileSceneRenderer::RenderDBuffer(FRDGBuilder& GraphBuilder, FSceneTextures& SceneTextures, FDBufferTextures& DBufferTextures, FInstanceCullingManager& InstanceCullingManager)
{
    const EShaderPlatform Platform = GetViewFamilyInfo(Views).GetShaderPlatform();

    for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ++ViewIndex) {
        FViewInfo& View = Views[ViewIndex];

        if (!View.ShouldRenderView()) continue;

        FTransientDecalRenderDataList VisibleDecals = DecalRendering::BuildVisibleDecalList(Scene->Decals, View);
        FDeferredDecalPassTextures DecalPassTextures = GetDeferredDecalPassTextures(GraphBuilder, View, Scene->SubstrateSceneData, SceneTextures, &DBufferTextures, EDecalRenderStage::BeforeBasePass);
        AddDeferredDecalPass(GraphBuilder, View, VisibleDecals, DecalPassTextures, InstanceCullingManager, EDecalRenderStage::BeforeBasePass);
    }
}
```

### 5.1 DBuffer 工作原理

1. **BeforeBasePass** 阶段：每个 Decal 写入 3 张 DBuffer RT
   - DBufferA: BaseColor + Opacity
   - DBufferB: WorldNormal + Opacity
   - DBufferC: Roughness/Specular/Metallic + Opacity
2. **BasePass PS** 通过 `MaterialAttributesFromDBuffer` 读 DBuffer，合并到当前材质属性
3. **优势**：贴花影响完整 PBR 属性，而不是简单加在 SceneColor 上

### 5.2 为什么 Deferred 不支持 DBuffer

```cpp
// MobileShadingRenderer.cpp:333
bRequiresDBufferDecals(bDeferredShading ? false : IsUsingDBuffers(ShaderPlatform))
```

- DBuffer 需要在 BasePass 之前生成（含完整投影矩阵 + UV 计算）
- Deferred 路径下 BasePass 已经在 Tile-in 模式，再插入 DBuffer Pass 会破坏 Subpass 优化
- 而且 Deferred 路径下 Decal 可以直接修改 GBuffer，等效于 DBuffer 效果

---

## 6. MeshDecal Pass 注册

```cpp
// MeshDecalRendering（推测，因为 EMeshPass::MeshDecal_SceneColor 已有）
EMeshPass::MeshDecal_SceneColor             // Forward 与 Deferred 共用
EMeshPass::MeshDecal_SceneColorAndGBuffer   // 仅 Deferred 用
```

### 6.1 双路径下的 MeshDecal 行为

| 路径 | MeshDecal_SceneColor | MeshDecal_SceneColorAndGBuffer |
|------|--------------------|------------------------------|
| Forward | ✅（直接改 SceneColor） | ❌ |
| Deferred Subpass 1 | ✅ | ✅（同 subpass 修改 GBuffer） |

### 6.2 调度

```cpp
// MobileShadingRenderer.cpp:1795-1796
View.ParallelMeshDrawCommandPasses[EMeshPass::MeshDecal_SceneColor]
    .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, MeshDecalSceneColorInstanceCullingDrawParams);
View.ParallelMeshDrawCommandPasses[EMeshPass::MeshDecal_SceneColorAndGBuffer]
    .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, MeshDecalSceneColorAndGBufferInstanceCullingDrawParams);
```

> 即使 Forward 路径也会 BuildRenderingCommands，但 RenderDecals 内 GetMeshPassType 返回 SceneColor，对应 SceneColorAndGBuffer 的 MDC 不会被 Draw。

---

## 7. Mobile Fog 全谱

源码：`MobileFogRendering.cpp`

### 7.1 8 种 Permutation Dim

```cpp
// MobileFogRendering.cpp:62-79
class FSupportHeightFog                       : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_HEIGHT_FOG");
class FSupportFogStartDistance                : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_FOG_START_DISTANCE");
class FSupportFogInScatteringTexture          : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_FOG_INSCATTERING_TEXTURE");
class FSupportFogSecondTerm                   : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_FOG_SECOND_TERM");
class FSupportFogDirectionalLightInScattering : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_FOG_DIRECTIONAL_LIGHT_INSCATTERING");
class FSupportAerialPerspective               : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_AERIAL_PERSPECTIVE");
class FSupportVolumetricFog                   : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_VOLUMETRIC_FOG");
class FSupportLocalFogVolume                  : SHADER_PERMUTATION_INT("PERMUTATION_SUPPORT_LOCAL_FOG_VOLUME", 3);
```

> 移动端 Fog 是独立 PixelShader Pass，**不通过 BasePass 的 vertex fog**。`r.Mobile.DisableVertexFog=1` 时启用像素雾。

### 7.2 r.Mobile.PixelFogQuality

```cpp
// MobileFogRendering.cpp:25-31
static TAutoConsoleVariable<int32> CVarPixelFogQuality(
    TEXT("r.Mobile.PixelFogQuality"), 1,
    TEXT("0 - basic per-pixel fog")
    TEXT("1 - all per-pixel fog features (second fog, directional inscattering, aerial perspective)"),
    ECVF_Scalability | ECVF_RenderThreadSafe);
```

### 7.3 双路径调度

```cpp
// Forward Single Pass (MobileShadingRenderer.cpp:1972-1978)
RHICmdList.NextSubpass();
RenderDecals(...);
RenderModulatedShadowProjections(...);
if (GMaxRHIShaderPlatform != SP_METAL_SIM) {
    RenderFog(RHICmdList, View);
}
RenderTranslucency(RHICmdList, View);

// Deferred Single Pass (MobileShadingRenderer.cpp:2358)
MobileDeferredShadingPass(...);
if (bUsingPixelLocalStorage) MobileDeferredCopyBuffer<PLSPS>(...);
RenderFog(RHICmdList, View);  // ← LightingPass 后
```

> Fog 在 Forward 路径下 DepthRead subpass 渲染（可读 Depth）；Deferred 路径下 LightingSubpass 后渲染（同样可读 Depth/GBuffer）。

### 7.4 RenderFog 主体

```cpp
// MobileFogRendering.cpp:144-149
static const auto* CVarDisableVertexFog =
    IConsoleManager::Get().FindTConsoleVariableDataInt(TEXT("r.Mobile.DisableVertexFog"));
if (CVarDisableVertexFog && CVarDisableVertexFog->GetValueOnRenderThread() == 0) {
    // Project uses only vertex fogging
    return;
}
```

> 默认走 Vertex Fog（编入 BasePass VS）；`r.Mobile.DisableVertexFog=1` 切到独立像素雾 Pass。

### 7.5 项目层 SP_METAL_SIM 排除

```cpp
if (GMaxRHIShaderPlatform != SP_METAL_SIM) {
    RenderFog(RHICmdList, View);
}
```

> Metal Simulator 上 Fog Subpass 不稳定，临时跳过。

---

## 8. Local Fog Volume（局部体积雾）

```cpp
// MobileFogRendering.cpp:119-123 PassParameters
SHADER_PARAMETER_STRUCT(FLocalFogVolumeUniformParameters, LFV)
SHADER_PARAMETER_RDG_TEXTURE_SRV(Texture2D<float4>, HalfResLocalFogVolumeViewSRV)
SHADER_PARAMETER_SAMPLER(SamplerState, HalfResLocalFogVolumeViewSRVSampler)
SHADER_PARAMETER_RDG_TEXTURE_SRV(Texture2D<float>, HalfResLocalFogVolumeDepthSRV)
SHADER_PARAMETER_SAMPLER(SamplerState, HalfResLocalFogVolumeDepthSRVSampler)
```

### 8.1 三档 LFV 支持

```cpp
class FSupportLocalFogVolume : SHADER_PERMUTATION_INT("PERMUTATION_SUPPORT_LOCAL_FOG_VOLUME", 3);
```

| Mode | 含义 |
|------|------|
| 0 | Disabled |
| 1 | Sample LFV（每像素采样 Volume Buffer） |
| 2 | Compose HalfRes LFV Texture（半分辨率合成） |

### 8.2 调度（MobileShadingRenderer.cpp:1553-1559）

```cpp
// Render half res local fog volume here
for (FViewInfo& View : Views) {
    if (View.LocalFogVolumeViewData.bUseHalfResLocalFogVolume) {
        RenderLocalFogVolumeHalfResMobile(GraphBuilder, View);
    }
}
```

> HalfRes LFV 在 BasePass 之前渲染，与 RenderFog 主 Pass 合成。

---

## 9. Volumetric Fog（体积雾）

```cpp
// MobileShadingRenderer.cpp:607-610
if (ShouldRenderVolumetricFog() && bRendererOutputFinalSceneColor)
{
    SetupVolumetricFog();
}
```

### 9.1 数据流

1. `SetupVolumetricFog`：分配 3D Volume Texture（典型 160×90×128）
2. Compute Pass：体积内光照计算（光强、外散射、内散射）
3. Composite Pass：BasePass / Fog Pass 读 Volume Texture 做 Inscatter

### 9.2 移动端限制

- 需要 Compute Shader + Volume Texture 写入
- iOS Metal 早期不支持 Volume Texture UAV
- 中低端 Android 极少启用
- 替代方案：HeightFog + LocalFogVolume

### 9.3 双路径下都支持

```cpp
class FSupportVolumetricFog : SHADER_PERMUTATION_BOOL("PERMUTATION_SUPPORT_VOLUMETRIC_FOG");
```

> Permutation 注入到 MobileFogPS，Forward / Deferred 都可启用。

---

## 10. SkyAtmosphere

源码：`SkyAtmosphereRendering.cpp:385`

```cpp
bool ShouldRenderSkyAtmosphere(const FScene* Scene, const FEngineShowFlags& EngineShowFlags)
{
    if (Scene && Scene->HasSkyAtmosphere() && EngineShowFlags.Atmosphere)
        return true;
    return false;
}
```

### 10.1 SkyAtmosphere LUT 系统

```cpp
// SkyAtmosphereRendering.cpp:1338
void FSceneRenderer::RenderSkyAtmosphereLookUpTables(FRDGBuilder& GraphBuilder,
                                                      FSkyAtmospherePendingRDGResources& PendingRDGResources)
{
    check(ShouldRenderSkyAtmosphere(Scene, ViewFamily.EngineShowFlags));
    RDG_EVENT_SCOPE_STAT(GraphBuilder, SkyAtmosphereLUTs, "SkyAtmosphereLUTs");
    ...
}
```

预计算的 LUT：
- **TransmittanceLUT** 256×64：大气穿透率
- **MultiScatteredLuminanceLUT** 32×32：多次散射
- **SkyViewLUT** 192×108：天空视角 LUT
- **CameraAerialPerspectiveVolume** 32×32×16：相机视角的远景透视

### 10.2 主渲染 Pass

```cpp
// SkyAtmosphereRendering.cpp:1799
void FSceneRenderer::RenderSkyAtmosphereInternal(...);
```

`FRenderSkyAtmospherePS` 的 9 个 Permutation：

```cpp
using FPermutationDomain = TShaderPermutationDomain<
    FSampleCloudSkyAO,
    FFastSky,
    FFastAerialPespective,
    FSecondAtmosphereLight,
    FRenderSky,
    FSampleOpaqueShadow,
    FSampleCloudShadow,
    FAtmosphereOnClouds,
    FMSAASampleCount>;
```

### 10.3 移动端调度

`FMobileSceneRenderer` 内通过 `SkyAtmosphereRendering.cpp` 共享代码，按 BasePass Sky Pass 集成。

---

## 11. VolumetricCloud 移动端支持

源码：`VolumetricCloudRendering.cpp:491-494`

```cpp
return Scene->VolumetricCloud
    && Scene->VolumetricCloud->GetVolumetricCloudSceneProxy().bUsePerSampleAtmosphericLightTransmittance
    && Scene->HasSkyAtmosphere()
    && ShouldRenderSkyAtmosphere(Scene, InViewIfDynamicMeshCommand->Family->EngineShowFlags);
```

### 11.1 移动端限制

- 完整 VolumetricCloud 需要 Compute Shader Ray Marching
- 移动端通常用 Sky Atmosphere + 静态 Cloud Texture 替代
- 项目可定制 LightShaft（SunMask）模拟云层效果

---

## 12. AtmosphericLight 与移动 DirectionalLight 关系

```cpp
// SkyAtmosphereRendering.cpp:471-476
if (Scene
    && LightSceneInfo
    && LightSceneInfo->Proxy->GetLightType() == LightType_Directional
    && ShouldRenderSkyAtmosphere(LightSceneInfo->Scene, View.Family->EngineShowFlags))
{
    FLightSceneProxy* AtmosphereLight0Proxy = Scene->AtmosphereLights[0]
        ? Scene->AtmosphereLights[0]->Proxy : nullptr;
    ...
}
```

> 主光 Directional Light 标记为 `bAtmosphereSunLight=true` 时，参与 Atmosphere 计算（影响 LUT、Aerial Perspective）。

---

## 13. Mobile Fog 与 Sky Atmosphere 集成

```hlsl
// MobileFog.usf 内部
#if PERMUTATION_SUPPORT_AERIAL_PERSPECTIVE
    // 从 CameraAerialPerspectiveVolume 采样
    AerialPerspective = SampleAerialPerspective(SvPosition, SceneDepth);
    FogColor = lerp(FogColor, AerialPerspective.rgb, AerialPerspective.a);
#endif
```

> Aerial Perspective 是 SkyAtmosphere 在远处场景上的"空气透视"叠加。需要 LUT 预计算 + Fog Pass 采样。

---

## 14. EngineShowFlags 控制开关速查

| ShowFlag | 控制 |
|----------|------|
| `Atmosphere` | SkyAtmosphere |
| `Fog` | 整体 Fog 渲染 |
| `Decals` | Decal |
| `Lighting` | 整体光照（关掉后 Sky Atmosphere 也跳过） |
| `VolumetricCloud` | 体积云 |
| `VolumetricLightmap` | 体积光照图 |
| `VolumetricFog` | 体积雾 |
| `SkyLighting` | 天空光 |
| `DynamicShadows` | 动态阴影 |
| `Particles` | 粒子 |

---

## 15. CVar 速查（Decal / Fog / Sky）

| CVar | 默认 | 说明 |
|------|------|------|
| `r.Mobile.DisableVertexFog` | 0 | 0=Vertex,1=Pixel |
| `r.Mobile.PixelFogQuality` | 1 | 0=basic,1=all |
| `r.Mobile.PixelFogDepthTest` | 1 | Depth/Stencil 测试 |
| `r.Mobile.DBuffer` | 0 | DBuffer 启用 |
| `r.Decals.AllowMobileSubpass` | 1 | Decal Subpass |
| `r.SupportSkyAtmosphere` | 1 | SkyAtmosphere |
| `r.SkyAtmosphere.AerialPerspectiveLUT.SampleCount` | 16 | Aerial 采样数 |
| `r.SkyAtmosphere.SkyViewLUT.SampleCount` | 30 | SkyView 采样数 |
| `r.VolumetricFog` | 1 | 体积雾 |
| `r.VolumetricFog.GridPixelSize` | 8 | 体积雾 cell 大小 |
| `r.Mobile.VolumetricFog` | – | 移动专属体积雾 |
| `r.SupportSkyAtmosphereAffectsHeightFog` | 1 | 联动 HeightFog |
| `r.LocalFogVolume.Mobile` | – | 局部雾体积 |

---

## 16. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| Decal 不显示 | LDR + Metal 平台限制 | 切 HDR 或换平台 |
| Decal 影响所有物体 | RECEIVE_DECAL Stencil 没正确设置 | 检查 BasePass Stencil |
| DBuffer 编译失败 | Deferred 路径下 bRequiresDBufferDecals=false | 检查路径 |
| Forward Decal 修改不了 Normal | EDecalRenderTargetMode 错 | 应该用 DBuffer 才能改 Normal |
| Fog 闪烁 | bMobile + Vertex Fog 顶点稀疏 | 切 Pixel Fog |
| VolumetricFog 黑屏 | 移动端不支持 Volume UAV | 替代方案 |
| SkyAtmosphere 错误颜色 | LUT 没生成 | 检查 RenderSkyAtmosphereLookUpTables |
| AerialPerspective 不生效 | PERMUTATION_SUPPORT_AERIAL_PERSPECTIVE=0 | 检查 PixelFogQuality |
| MobileBeforeLighting Decal 没改 GBuffer | RenderTargetMode 错 | SceneColorAndGBuffer |
| Local Fog Volume 不显示 | bUseHalfResLocalFogVolume=false | 检查 LocalFogVolumeViewData |
| Atmosphere Light 不亮 | bAtmosphereSunLight=false | 主光设置 |
| Metal Simulator Fog 跳过 | SP_METAL_SIM 平台 | 项目硬编 |

---

> 第 08 篇完。下一篇：**VertexShader / MaterialPermutation / Substrate Mobile**。
