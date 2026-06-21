# UE Mobile Forward vs Deferred —— 深度补充 05：虚拟纹理 / 虚拟阴影 / MMH

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**RuntimeVirtualTexture / LightmapVT / VirtualShadowMap / MMH ShadowMap** 在移动端的实现差异与项目特化。

---

## 1. UE 移动端虚拟化技术全景

| 技术 | 移动端支持 | Forward 用 | Deferred 用 | 关键文件 |
|------|----------|-----------|-------------|----------|
| Streaming Virtual Texture (SVT) | ✅ | ✅ | ✅ | `VT/` 目录 |
| Runtime Virtual Texture (RVT) | ✅ | ✅ | ✅ | `VT/RuntimeVirtualTextureRender.cpp` |
| Lightmap Virtual Texture (LMVT) | ✅ | ✅ | ✅ | `LightMapRendering.cpp` |
| Virtual Shadow Map (VSM) | ⚠ 实验性 | – | – | `VirtualShadowMaps/` |
| MMH Shadow Map（项目） | ✅ | ✅ | ✅ | `MMHShadowMap*` |

---

## 2. VirtualTexturing 在 FMobileSceneRenderer 构造的判定

```cpp
// MobileShadingRenderer.cpp:334
bUseVirtualTexturing = UseVirtualTexturing(ShaderPlatform)
                    && GetRendererOutput() != FSceneRenderer::ERendererOutput::DepthPrepassOnly;
```

> 单纯做 DepthPrepass（如 SceneCapture）的 view 不需要 VT 流送，节省开销。

---

## 3. VT 流送的完整生命周期

### 3.1 BeginUpdate（Render() 早期）

```cpp
// MobileShadingRenderer.cpp:1184-1193
TUniquePtr<FVirtualTextureUpdater> VirtualTextureUpdater;

if (bUseVirtualTexturing) {
    FVirtualTextureUpdateSettings Settings;
    Settings.EnableThrottling(!ViewFamily.bOverrideVirtualTextureThrottle);

    VirtualTextureUpdater = FVirtualTextureSystem::Get().BeginUpdate(GraphBuilder, FeatureLevel, Scene, Settings);
    VirtualTextureFeedbackBegin(GraphBuilder, Views, SceneTexturesConfig.Extent);
}
```

### 3.2 InitViews 阶段产生反馈请求

```cpp
// MobileShadingRenderer.cpp:605
TaskDatas.VisibilityTaskData->FinishGatherDynamicMeshElements(
    BasePassDepthStencilAccess, InstanceCullingManager, VirtualTextureUpdater);
```

> InitViews 期间，每个 mesh 的可见性会记录到 `VirtualTextureUpdater`，告诉系统下一帧需要哪些 page。

### 3.3 EndUpdate（执行加载）

```cpp
// MobileShadingRenderer.cpp:1300-1303
if (bUseVirtualTexturing) {
    FVirtualTextureSystem::Get().EndUpdate(GraphBuilder, MoveTemp(VirtualTextureUpdater), FeatureLevel);
}
```

### 3.4 FeedbackEnd（帧末读 GPU 反馈）

```cpp
// MobileShadingRenderer.cpp:1693-1698
if (bUseVirtualTexturing) {
    RDG_EVENT_SCOPE_STAT(GraphBuilder, VirtualTextureUpdate, "VirtualTextureUpdate");
    RDG_GPU_STAT_SCOPE(GraphBuilder, VirtualTextureUpdate);
    VirtualTextureFeedbackEnd(GraphBuilder);
}
```

> Feedback Buffer 读回 CPU 后，下一帧 BeginUpdate 时根据 UV 计算需要加载哪些 page。

---

## 4. Forward / Deferred 路径下 VT 调度差异

| 阶段 | Forward 单 Pass | Deferred 单 Pass |
|------|----------------|------------------|
| BeginUpdate | Render() 早期 | 同 |
| InitViews 收集反馈 | InitViews | 同 |
| EndUpdate（开始加载） | Shadow Depth 之前 | 同 |
| BasePass 写 Feedback UAV | Subpass 0 | Subpass 0 |
| FeedbackEnd（读回） | 后处理之前 | 同 |

> 两条路径的 VT 调度**几乎完全一致**，区别仅在 BasePass PS 是否同时写 GBuffer。

---

## 5. Lightmap VT（LMVT）

```cpp
// LightMapRendering.cpp:43-46
static const auto CVar = IConsoleManager::Get().FindTConsoleVariableDataInt(TEXT("r.VirtualTexturedLightmaps"));
const bool VirtualTextureLightmaps = (CVar->GetValueOnAnyThread() != 0)
                                  && UseVirtualTexturing(Parameters.Platform);
OutEnvironment.SetDefine(TEXT("LIGHTMAP_VT_ENABLED"), VirtualTextureLightmaps);
```

> `LIGHTMAP_VT_ENABLED` 全局编译宏，影响所有 Lightmap 采样代码。

### 5.1 双路径 Permutation 影响

| Path | LMP_LQ_LIGHTMAP w/ VT | LMP_HQ_LIGHTMAP w/ VT |
|------|-----------------------|------------------------|
| Forward | 编译 9 种 × 2 (VT/非VT) = 18 种 | 同 |
| Deferred | 编译 4 种 × 2 = 8 种 | 同 |

> Forward 路径下 LMVT 引入 Permutation 数量更多。

### 5.2 LightmapVT 与 LuxGI 互斥

```hlsl
// MobileBasePassPixelShader.usf:38-46
#ifndef STATIC_LIGHTING_LIGHTMAP_ONLY
#define STATIC_LIGHTING_LIGHTMAP_ONLY 0
#endif
#ifndef STATIC_LIGHTING_LUXGI_ONLY
#define STATIC_LIGHTING_LUXGI_ONLY 1
#endif
#ifndef STATIC_LIGHTING_HYBRID
#define STATIC_LIGHTING_HYBRID 2
#endif
```

```hlsl
// MobileBasePassPixelShader.usf:984-992
if (View.StaticLightingMethod == STATIC_LIGHTING_LUXGI_ONLY) {
    // LuxGI do nothing in BasePass when calculate static lighting
} else {
    bStaticLightingUseLightmap = GetPrecomputedIndirectLightingAndSkyLight(LightmapVTPageTableResult, ...);
}
```

> 同一项目可在三种静态光照模式间切换：Lightmap Only / LuxGI Only / Hybrid。

---

## 6. Runtime Virtual Texture (RVT)

源码：`VT/RuntimeVirtualTextureRender.cpp:164-167`

```cpp
static bool ShouldCompilePermutation(const FMeshMaterialShaderPermutationParameters& Parameters) {
    return UseVirtualTexturing(Parameters.Platform) &&
        (Parameters.MaterialParameters.bHasRuntimeVirtualTextureOutput
         || Parameters.MaterialParameters.bIsDefaultMaterial);
}
```

### 6.1 Mobile RVT 应用

- **地形混合**：每个 tile 烘焙 BaseColor/Normal/Height/Roughness 到 RVT，BasePass 直接采样
- **Decal 烘焙**：贴花预烘焙到 RVT，减少运行时 Mesh Decal 开销
- **Water Caustics**：水底光斑预渲染

### 6.2 双路径影响

| 路径 | RVT 采样位置 | 性能 |
|------|------------|------|
| Forward | BasePass PS 直接采 | 与材质合并 |
| Deferred | BasePass PS 写入 GBuffer.BaseColor 等，然后 LightingPass 用 | 同 |

> RVT 对 Forward / Deferred 透明，**不影响路径选择**。但 RVT page miss 时会出现马赛克，需要项目层做 page prefetch。

### 6.3 RVT 类型支持检查

```cpp
// VT/RuntimeVirtualTextureRender.cpp:2110-2112
static bool ShouldCompilePermutation(const FGlobalShaderPermutationParameters& Parameters) {
    return UseVirtualTexturing(Parameters.Platform)
        && RuntimeVirtualTexture::IsMaterialTypeSupported(MaterialType, Parameters.Platform);
}
```

| MaterialType | Mobile 支持 |
|-------------|-----------|
| BaseColor | ✅ |
| BaseColor_Normal_Specular | ✅ |
| BaseColor_Normal_Roughness | ✅ |
| WorldHeight | ✅ |
| BaseColor_Normal_DepthStencil | ⚠（依赖 DepthStencil 出 Tile） |
| Mask | ✅ |
| Displacement | ❌ |
| 7_5_lightmap | ✅ |

---

## 7. Virtual Shadow Map（VSM）

源码：`VirtualShadowMaps/VirtualShadowMapArray.cpp:544-548`

```cpp
bool DoesVSMWantFroxels(EShaderPlatform ShaderPlatform)
{
    return UseVirtualShadowMaps(ShaderPlatform)
        && CVarMarkPagesUseFroxels.GetValueOnRenderThread() != 0
        // fall back to per-pixel marking if the front layer translucency path is enabled
}
```

### 7.1 移动端 VSM 现状

- UE5.5 移动端 VSM 仅作为实验性特性提供
- 依赖 Compute Shader + Atomic Operations
- 需要 R32 RWTexture 支持
- **本工程优先用 MMH ShadowMap 替代**

### 7.2 VSM 在 `FMobileSceneRenderer` 中的初始化

```cpp
// MobileShadingRenderer.cpp:1221
VirtualShadowMapArray.Initialize(GraphBuilder, Scene->GetVirtualShadowMapCache(),
                                  UseVirtualShadowMaps(ShaderPlatform, FeatureLevel),
                                  ViewFamily.EngineShowFlags);
```

> `UseVirtualShadowMaps(Platform, FeatureLevel)` 决定是否启用；移动端默认为 false。

---

## 8. MMH ShadowMap（项目专属）

源码：`MMHShadowMap*` 系列文件

### 8.1 调度时机

```cpp
// MobileShadingRenderer.cpp:1246-1248
Scene->MMHShadowMapSceneData.PreInitViews(GraphBuilder, *Scene, Views, ViewFamily.EngineShowFlags);

// 1383-1389
Scene->MMHShadowMapSceneData.PreUpdate(GraphBuilder, *Scene, Views);
Scene->MMHShadowMapSceneData.Update(GraphBuilder, *Scene, Views, ExternalAccessQueue);
Scene->MMHShadowMapSceneData.VirtualShadowMapArray.BeginDecodeRawFeedbackValueTask();

// 1529-1532
if (Scene->IsMMHEnabled()) {
    Scene->MMHShadowMapSceneData.AddVirtualShadowMapFeedbackSubmit(GraphBuilder, *Scene, Views, SceneTextures);
}
```

### 8.2 MMH 工作原理（推测）

- **M**ulti-**M**aterial **H**ierarchy ShadowMap：将 ShadowMap 按材质分层
- 利用 VSM 风格的稀疏存储，但用 ATILE 级别的反馈
- 移动 VR / 大世界场景用：每帧只更新可见 tile

### 8.3 与 PostProcessing 集成

```cpp
// MobileShadingRenderer.cpp:1742-1743
AddMobilePostProcessingPasses(GraphBuilder, Scene, Views[ViewIndex], ViewIndex,
    GetSceneUniforms(), PostProcessingInputs, InstanceCullingManager,
    &Scene->MMHShadowMapSceneData.VirtualShadowMapArray);
```

- PostProcessing 接收 MMH VirtualShadowMapArray 引用
- `EPass::VisualizeMMHShadowMaps` 用于调试

### 8.4 双路径下 MMH 表现

| 路径 | MMH ShadowMap 使用 |
|------|------------------|
| Forward | BasePass PS 通过 `MMHShadowMapProjection.usf` 采样 |
| Deferred | LightingPS 通过同样的 USF 采样 |

> MMH 跨路径设计，**不依赖管线**。这是项目 QiaCongShe 的改造，给 VR / 大世界做的稀疏阴影。

---

## 9. VT 在 BasePass 与 Lightmap 系统的耦合

```cpp
// MobileBasePass.cpp:432
if (ShadowMapInteraction.GetType() == SMIT_Texture
    && FReadOnlyCVARCache::MobileAllowDistanceFieldShadows())
{
    bHasCSMApplicableLightInteraction = ...;
}
```

```cpp
// MobileBasePass.cpp:488-495
if (FReadOnlyCVARCache::MobileEnableStaticAndCSMShadowReceivers()
    && !bUsesDeferredShading && bCanReceiveCSM)
{
    if (FReadOnlyCVARCache::MobileAllowDistanceFieldShadows() && !bTranslucent)
        Result.Add(LMP_MOBILE_DISTANCE_FIELD_SHADOWS_LIGHTMAP_AND_CSM);
    Result.Add(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM_AND_LIGHTMAP);
}
```

> Lightmap 类型（SMIT_Texture vs SMIT_VT）影响 LMP 选择，不影响 VT 启用决策。

---

## 10. RVT 烘焙时的 VT Stack 一致性

```cpp
// ReflectionEnvironmentCapture.cpp:1572-1574
if (UseVirtualTexturing(GetShaderPlatform())) {
    // Prefetch all virtual textures so that we have content available
    const ERHIFeatureLevel::Type InFeatureLevel = FeatureLevel;
    ...
}
```

> Reflection Capture 烘焙时也需要 VT 全量预加载，否则反射球烘焙出来会有 page miss。

---

## 11. SceneCapture 的 VT 处理

```cpp
// SceneHitProxyRendering.cpp:211-213
// Ensure VirtualTexture resources are allocated
if (UseVirtualTexturing(ViewFamily.Scene->GetShaderPlatform())) {
    FVirtualTextureUpdateSettings Settings;
    ...
}
```

> HitProxy 渲染（Editor）也需要 VT 资源，防止编辑器中的物体选择失败。

---

## 12. VT 调试 / Profile

### 12.1 关键 CVar

| CVar | 默认 | 说明 |
|------|------|------|
| `r.VT.Enable` | 1 | VT 总开关 |
| `r.VirtualTexturedLightmaps` | 1 | LMVT |
| `r.VT.RVT.Enable` | 1 | RVT |
| `r.VT.Borders` | 1 | Tile 边界过滤 |
| `r.VT.PageUpdateFlushCount` | 8 | 每帧最大 page 更新数 |
| `r.VT.ResidencyShow` | 0 | 显示驻留 |
| `r.VT.PerformanceBudget` | 5 | 预算（毫秒） |
| `r.VT.MaxAnisotropy` | 8 | 各向异性 |
| `r.Mobile.VT.MaxResidentMipsBias` | 0 | 移动端 mip 偏移 |
| `r.Shadow.Virtual.Enable` | 0 | VSM |
| `r.MMH.Enable` | 1 | 项目 MMH |
| `r.MMH.Visualize` | 0 | 调试 |

### 12.2 ProfileGPU 命名

- `VirtualTextureUpdate`：page 加载
- `VirtualTextureFeedbackBegin/End`：反馈处理
- `VisualizeMMHShadowMaps`：MMH 调试
- `ShadowProjection`：包含 MMH submit

---

## 13. VT 与 Tile Memory 关系

VT 系统的核心数据流：
1. **Physical Texture Atlas**（驻留池）：单张 16K×16K 物理纹理
2. **Page Table**：UV 映射表（每个 mip 一张）
3. **Feedback Buffer**：BasePass 写入 page 请求 (PageID + MipLevel)

### Tile 内交互

| 数据 | 是否驻留 Tile |
|------|--------------|
| Physical Texture | ❌（GPU 主存） |
| Page Table | ❌（GPU 主存） |
| Feedback UAV | ❌（GPU 主存，BasePass 写） |

> VT 不会"上 Tile"，所有数据都在主存。这就是为什么 VT 性能瓶颈在带宽。

---

## 14. 实战配置示例

### 14.1 大世界 + RVT 地形

```ini
[/Script/Engine.RendererSettings]
r.VT.Enable=1
r.VT.RVT.Enable=1
r.VT.RVT.MaterialType=2  ; BaseColor_Normal_Roughness
r.VT.MaxAnisotropy=8
r.VT.PageUpdateFlushCount=16
r.Mobile.VT.MaxResidentMipsBias=0
r.VirtualTexturedLightmaps=0  ; 大世界通常不烘焙 Lightmap
```

### 14.2 中型场景 + LMVT

```ini
r.VT.Enable=1
r.VirtualTexturedLightmaps=1
r.VT.PageUpdateFlushCount=4
r.VT.RVT.Enable=0  ; 关 RVT 省带宽
```

### 14.3 紧凑室内 + 烘焙

```ini
r.VT.Enable=0  ; 小场景 VT 收益不大
r.VirtualTexturedLightmaps=0
```

---

## 15. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| VT page miss 黑块 | PageUpdateFlushCount 太低 | 增加值 |
| LMVT 编译失败 | LIGHTMAP_VT_ENABLED 没注入 | 检查 SetDefine 路径 |
| RVT 不烘焙 | bHasRuntimeVirtualTextureOutput 不为真 | 检查材质节点 |
| MMH 不渲染 | Scene->IsMMHEnabled() 为 false | `r.MMH.Enable=1` |
| VSM 移动端崩溃 | UseVirtualShadowMaps 平台限制 | 关闭 |
| VT 反馈延迟 | VirtualTextureFeedbackEnd 在错误位置 | 必须帧末 |
| Reflection Capture 黑色 | VT 没预加载 | 加 Prefetch |
| 切场景后 VT 残留 | EndUpdate 漏调用 | 检查作用域 |
| MMH 后处理可视化空白 | MMHShadowMapArray 引用没传 | 检查 1743 行 |
| RVT Tile 边界缝隙 | r.VT.Borders 没开 | `r.VT.Borders=1` |

---

> 第 05 篇完。下一篇：**MeshDrawCommand / GPUScene / InstanceCulling 移动端深挖**。
