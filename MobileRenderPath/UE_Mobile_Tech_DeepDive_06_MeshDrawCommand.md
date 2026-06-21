# UE Mobile Forward vs Deferred —— 深度补充 06：MeshDrawCommand / GPUScene / InstanceCulling

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**MeshDrawCommand / GPUScene / InstanceCullingManager / Parallel MDC Pass** 在 Mobile 双路径下的实现。

---

## 1. MeshDrawCommand (MDC) 体系全景

```
材质 + VertexFactory + PassData (Lightmap, LocalLight, CSM 状态)
        ↓
FMeshPassProcessor::Process
        ↓
FMeshDrawCommand (含 PSO + Shader Bindings + 顶点/索引引用)
        ↓
缓存在 Scene->CachedMeshDrawCommandStateBuckets
        ↓
每帧 InitViews 时通过 PrimitiveVisibilityMap 筛选
        ↓
FParallelMeshDrawCommandPass::Draw → RHICmdList.DrawIndexedPrimitive
```

### 1.1 移动端注册的 MeshPass

```cpp
// MobileBasePass.cpp:1330-1336
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileBasePass,
    CreateMobileBasePassProcessor,           EShadingPath::Mobile, EMeshPass::BasePass,
    EMeshPassFlags::CachedMeshCommands | EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileBasePassCSM,
    CreateMobileBasePassCSMProcessor,        EShadingPath::Mobile, EMeshPass::MobileBasePassCSM,
    EMeshPassFlags::CachedMeshCommands | EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyAllPass,
    CreateMobileTranslucencyAllPassProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyAll,
    EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyStandardPass,
    CreateMobileTranslucencyStandardPassProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyStandard,
    EMeshPassFlags::MainView);
REGISTER_MESHPASSPROCESSOR_AND_PSOCOLLECTOR(MobileTranslucencyAfterDOFPass,
    CreateMobileTranslucencyAfterDOFProcessor, EShadingPath::Mobile, EMeshPass::TranslucencyAfterDOF,
    EMeshPassFlags::MainView);
```

### 1.2 EMeshPass 数量对比

| 路径 | 注册 MeshPass 数 |
|------|----------------|
| PC Deferred | ~25 |
| PC Forward+ | ~22 |
| Mobile（任何） | ~12 |

> 移动端少了：DepthPass（项目 EarlyZ 时按需），Velocity（独立调度），LumenCardCapture，HitProxy（仅 Editor），DistortionAccumulate（项目）等。

---

## 2. `FMobileRenderPassParameters` —— 移动端 Pass 参数核心

源码：`MobileShadingRenderer.cpp:252-262`

```cpp
BEGIN_SHADER_PARAMETER_STRUCT(FMobileRenderPassParameters,)
    SHADER_PARAMETER_STRUCT_INCLUDE(FViewShaderParameters, View)
    SHADER_PARAMETER_STRUCT_INCLUDE(FInstanceCullingDrawParams, InstanceCullingDrawParams)
    SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FMobileBasePassUniformParameters, MobileBasePass)
    SHADER_PARAMETER_STRUCT_REF(FMobileReflectionCaptureShaderData, ReflectionCapture)
    SHADER_PARAMETER_RDG_BUFFER_SRV(StructuredBuffer<FLocalFogVolumeGPUInstanceData>, LocalFogVolumeInstances)
    SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>, LocalFogVolumeTileDrawIndirectBuffer)
    SHADER_PARAMETER_RDG_TEXTURE_SRV(Texture2DArray<uint>, LocalFogVolumeTileDataTexture)
    SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>, LocalFogVolumeTileDataBuffer)
    SHADER_PARAMETER_RDG_TEXTURE(Texture2D, HalfResLocalFogVolumeViewTexture)
    SHADER_PARAMETER_RDG_TEXTURE(Texture2D, HalfResLocalFogVolumeDepthSRV)
    RENDER_TARGET_BINDING_SLOTS()
END_SHADER_PARAMETER_STRUCT()
```

> 双路径共用同一 PassParameters 结构。区别仅在 RT 绑定 slots（GBuffer 数量）。

---

## 3. GPUScene 启用与 ViewID 注册

```cpp
// MobileShadingRenderer.cpp:574-584
InstanceCullingManager.AllocateViews(Views.Num());
for (FViewInfo& ViewInfo : Views) {
    ViewInfo.GPUSceneViewId = InstanceCullingManager.RegisterView(ViewInfo);

    uint32 InstanceFactor = ViewInfo.bIsInstancedStereoEnabled
                         && IStereoRendering::IsStereoEyeView(ViewInfo)
                         && GEngine->StereoRenderingDevice.IsValid()
                        ? GEngine->StereoRenderingDevice->GetDesiredNumberOfViews(true)
                        : 1;

    ViewInfo.InstanceFactor = InstanceFactor > 0 ? InstanceFactor : 1;
}
```

### 3.1 GPUSceneViewId 含义

- 每个 View（含 InstancedStereo 副视图）在 GPUScene 中注册一个 ID
- 用于 InstanceCulling 时区分"哪个视图的剔除"
- 共享 ID 池 → 立体声渲染时一个主 view + 一个 instanced view

### 3.2 InstanceFactor

- 普通渲染：1
- ISR Stereo：2（左右眼共享 InstanceCullingResult）
- 移动多视口：1 或 2

---

## 4. GPUScene 数据上传

```cpp
// MobileShadingRenderer.cpp:829-839
{
    RDG_CSV_STAT_EXCLUSIVE_SCOPE(GraphBuilder, UpdateGPUScene);

    for (int32 ViewIndex = 0; ViewIndex < AllViews.Num(); ViewIndex++) {
        FViewInfo& View = *AllViews[ViewIndex];
        Scene->GPUScene.UploadDynamicPrimitiveShaderDataForView(GraphBuilder, View);
        Scene->GPUScene.DebugRender(GraphBuilder, GetSceneUniforms(), View);
    }
}
```

### 4.1 GPUScene 中包含什么

- Primitive Transform & Bounds
- Material Index
- Custom Primitive Data（材质参数 vector4 数组）
- Instance Data（HISM/ISM/Foliage）
- VirtualShadowMapId / LumenCardId（PC Deferred 用）
- 移动端：BoneTransform / Animation 数据（角色）

### 4.2 双路径差异

| 数据 | Forward | Deferred |
|------|---------|----------|
| Primitive Transform | ✅ | ✅ |
| Custom Primitive Data | ✅ | ✅ |
| Instance Data | ✅ | ✅ |
| LumenCardId | ❌ | ❌（移动端不支持） |
| MobileCSMVisibility | ✅ | ❌（永远 CSM） |
| 项目 MMHShadowMap Index | ✅ | ✅ |

---

## 5. `FInstanceCullingManager::BeginDeferredCulling`

```cpp
// MobileShadingRenderer.cpp:884
InstanceCullingManager.BeginDeferredCulling(GraphBuilder, Scene->GPUScene);
```

> 在 ShadowDepth / BasePass 之前，预先把 Instance Culling Compute Pass dispatch 出去（异步），结果通过 `InstanceCullingDrawParams` 传给后续 Draw。

### 5.1 Compute Pass 内容

- 视锥剔除（per-instance）
- HZB 剔除（per-instance）
- DistanceCulling（per-instance）
- LOD 选择

### 5.2 输出 buffer

- DrawArgs Buffer（IndirectDraw 参数）
- InstanceIDs Buffer（剔除后保留的实例索引）
- MDC 通过 `BuildRenderingCommands` 把这两个绑到 PassParameters

---

## 6. `BuildRenderingCommands` 链路

```cpp
// MobileShadingRenderer.cpp:1076-1079
if (Scene->GPUScene.IsEnabled()) {
    View.ParallelMeshDrawCommandPasses[EMeshPass::BasePass]
        .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, PassParameters->InstanceCullingDrawParams);
}
```

### 6.1 调度多个 Pass 的 BuildRenderingCommands

```cpp
// MobileShadingRenderer.cpp:1783-1799 BuildInstanceCullingDrawParams
void FMobileSceneRenderer::BuildInstanceCullingDrawParams(FRDGBuilder& GraphBuilder, FViewInfo& View, FMobileRenderPassParameters* PassParameters)
{
    if (Scene->GPUScene.IsEnabled()) {
        if (!bIsFullDepthPrepassEnabled)
            View.ParallelMeshDrawCommandPasses[EMeshPass::DepthPass]
                .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, DepthPassInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::BasePass]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, PassParameters->InstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::SkyPass]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, SkyPassInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[StandardTranslucencyMeshPass]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, TranslucencyInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::DebugViewMode]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, DebugViewModeInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::MeshDecal_SceneColor]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, MeshDecalSceneColorInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::MeshDecal_SceneColorAndGBuffer]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, MeshDecalSceneColorAndGBufferInstanceCullingDrawParams);

        View.ParallelMeshDrawCommandPasses[EMeshPass::MobileCharacterForwardPass]
            .BuildRenderingCommands(GraphBuilder, Scene->GPUScene, CharacterForwardInstanceCullingDrawParams);
    }
}
```

> 一次性把所有可能需要的 Pass 的剔除结果都准备好，避免运行时再 dispatch。

### 6.2 双路径下 MeshPass 集合差异

| 路径 | 涉及 Pass |
|------|----------|
| Forward | BasePass + MobileBasePassCSM + SkyPass + TranslucencyStandard/AfterDOF + MeshDecal_SceneColor |
| Deferred | BasePass + SkyPass + TranslucencyStandard/AfterDOF + **MeshDecal_SceneColorAndGBuffer** + **MobileCharacterForwardPass** |

> Forward 多了 `MobileBasePassCSM`，Deferred 多了 `MeshDecal_SceneColorAndGBuffer` 和（项目）`MobileCharacterForwardPass`。

---

## 7. `DispatchPassSetup` —— BasePass 排序+建表

```cpp
// MobileShadingRenderer.cpp:464-479
Pass.DispatchPassSetup(
    Scene,
    View,
    FInstanceCullingContext(PassName, ShaderPlatform, &InstanceCullingManager, ViewIds,
                            View.PrevViewInfo.HZB, InstanceCullingMode),
    EMeshPass::BasePass,
    BasePassDepthStencilAccess,
    MeshPassProcessor,
    View.DynamicMeshElements,
    &View.DynamicMeshElementsPassRelevance,
    View.NumVisibleDynamicMeshElements[EMeshPass::BasePass],
    ViewCommands.DynamicMeshCommandBuildRequests[EMeshPass::BasePass],
    ViewCommands.DynamicMeshCommandBuildFlags[EMeshPass::BasePass],
    ViewCommands.NumDynamicMeshCommandBuildRequestElements[EMeshPass::BasePass],
    ViewCommands.MeshCommands[EMeshPass::BasePass],
    BasePassCSMMeshPassProcessor,
    &ViewCommands.MeshCommands[EMeshPass::MobileBasePassCSM]);
```

### 7.1 DispatchPassSetup 主要工作

1. **排序**：根据 PrimitiveSortKey（深度 + 材质 + VFactory）排序
2. **合并**：把 NonCSM 与 CSM 两条 MDC 合并到 BasePass 的统一执行序列
3. **InstanceCullingContext**：绑定剔除上下文，让每个 MDC 知道自己的剔除参数

### 7.2 ViewIds 多视图支持

```cpp
TArray<int32, TInlineAllocator<2>> ViewIds;
ViewIds.Add(View.GPUSceneViewId);
if (InstanceCullingMode == EInstanceCullingMode::Stereo) {
    check(View.GetInstancedView() != nullptr);
    ViewIds.Add(View.GetInstancedView()->GPUSceneViewId);
}
```

> Stereo ISR：左右眼共享 MDC，剔除按主视图做，绘制时通过 VertexShader 切视图。

---

## 8. EInstanceCullingMode 三档

```cpp
// MobileShadingRenderer.cpp:449
EInstanceCullingMode InstanceCullingMode = View.IsInstancedStereoPass()
    ? EInstanceCullingMode::Stereo
    : EInstanceCullingMode::Normal;
```

| Mode | 含义 |
|------|------|
| Normal | 单视图剔除 |
| Stereo | 左右眼合并剔除（ISR） |
| Async | 异步剔除（PC Deferred 用） |

> 移动端不用 Async，因为移动 GPU 通常不支持并行 Compute + Graphics。

---

## 9. MeshDrawCommand Cache 命中率优化

`MobileBasePass.cpp:1330` Flags `EMeshPassFlags::CachedMeshCommands` 表示 MDC 缓存在 Scene 上：

```
Scene->CachedMeshDrawCommandStateBuckets[EMeshPass::BasePass]
```

### 9.1 触发重建的事件

- 材质重编译 / 修改
- 材质 Quality Level 切换
- LightmapPolicy 变化
- LocalLightSetting CVar 变化（仅 Forward）
- bPassUsesDeferredShading 变化（极少，需要 ShadingPath CVar 切换）
- MobileCSMVisibility 变化（仅 Forward）

### 9.2 双路径下的 Cache Miss 风险

| 操作 | Forward 影响 | Deferred 影响 |
|------|-------------|---------------|
| 切换 Lightmap | 9 种 Policy × 重建 | 4 种 Policy × 重建 |
| 切换 LocalLight | 3 种 Setting × 重建 | 不变 |
| 切换 ShadingPath | 全部重建 | 全部重建 |
| 加 / 减阴影投射光 | 重新走 CSM Culling，部分重建 | 不变 |

> Forward 路径的 MDC Cache 重建概率显著高于 Deferred。这对**关卡切换 / 时间动态变化**敏感的项目影响显著。

---

## 10. PSO Precache（移动端关键）

```cpp
// MobileBasePass.cpp:1060 CollectPSOInitializersForLMPolicy
void FMobileBasePassMeshProcessor::CollectPSOInitializersForLMPolicy(...)
{
    ...
    RenderTargetsInfo.bHasFragmentDensityAttachment = GVRSImageManager.IsAttachmentVRSEnabled();
}

// MobileBasePass.cpp:1120 CollectPSOInitializers
void FMobileBasePassMeshProcessor::CollectPSOInitializers(
    const FSceneTexturesConfig& SceneTexturesConfig, const FMaterial& Material,
    const FPSOPrecacheVertexFactoryData& VertexFactoryData,
    const FPSOPrecacheParams& PreCacheParams,
    TArray<FPSOPrecacheData>& PSOInitializers)
{
    ...
    FMobileLightMapPolicyTypeList UniformLightMapPolicyTypes =
        GetUniformLightMapPolicyTypeForPSOCollection(bLitMaterial, bTranslucentBasePass,
                                                     bPassUsesDeferredShading, bCanReceiveCSM, bMovable);
    ...
}
```

### 10.1 PSO Precache 移动端关键性

- iOS Metal：必须 Precache，否则首次绘制时编译 PSO 会卡顿
- Android Vulkan：Precache 帮助避免帧间 spike
- Android GLES：Precache 帮助 driver 缓存 Shader

### 10.2 移动端 Precache 数量爆炸

| Path | Permutation 数（典型项目） |
|------|--------------------------|
| Forward | 9 (LMP) × 3 (LocalLight) × 2 (LuxGI) × 2 (Color Trans) × 2 (CSM/no) × 2 (Translucent/no) = **432 PSO per material** |
| Deferred | 4 (LMP) × 1 (LocalLight) × 2 × 2 = **64 PSO per material** |

> Forward 路径每个材质需要 PSO 数量是 Deferred 的 ~7 倍！这是为什么移动 Forward 项目首次启动慢、APK 体积大。

---

## 11. SubpassIndex 与 PSO

```cpp
// MobileBasePass.cpp:1090-1093
// subpass info set during the submission of the draws in mobile deferred renderer.
uint8 SubpassIndex = bTranslucentBasePass ? (bDeferredShading ? 2 : 1) : 0;
ESubpassHint SubpassHint = GetSubpassHint(GMaxRHIShaderPlatform, bDeferredShading,
                                          RenderTargetsInfo.MultiViewCount > 1,
                                          RenderTargetsInfo.NumSamples);
```

> **PSO 在创建时就指定 SubpassIndex**。Forward / Deferred 路径下的 PSO 不兼容！切换 ShadingPath 必须**全量重建 PSO**。

### SubpassIndex 矩阵

| 路径 + Pass | SubpassIndex |
|------------|-------------|
| Forward Opaque BasePass | 0 |
| Forward Translucent | 1 |
| Deferred Opaque BasePass | 0 |
| Deferred Translucent | 2 |

---

## 12. ShouldDumpMeshDrawCommandInstancingStats 调试

```cpp
// MobileShadingRenderer.cpp:458-461
if (ShouldDumpMeshDrawCommandInstancingStats()) {
    Pass.SetDumpInstancingStats(GetMeshPassName(EMeshPass::BasePass));
}
```

> 命令：`DumpMeshDrawCommandInstancingStats`
> 输出每个 Pass 的 MDC 数、Instance 数、Cache 命中率，定位性能问题。

---

## 13. ParallelMeshDrawCommandPass::Draw

```cpp
// MobileTranslucentRendering.cpp:19
View.ParallelMeshDrawCommandPasses[StandardTranslucencyMeshPass]
    .Draw(RHICmdList, &TranslucencyInstanceCullingDrawParams);
```

### 13.1 内部流程

1. 取出 MDC 列表
2. 应用 InstanceCullingDrawParams（提供剔除后实例数）
3. RHICmdList.DrawIndexedPrimitive 或 DrawIndexedPrimitiveIndirect

### 13.2 移动端不支持 Parallel 命令录制

- PC Deferred：多线程并行录制 RHI 命令到 ParallelCmdList，最后串行 submit
- 移动端：通常单线程录制（线程数有限，避免 driver 锁）

`FParallelMeshDrawCommandPass` 在移动端实际是**串行**执行，但保留了 PC 命名。

---

## 14. MobileBasePassAfterShadowInit 的两阶段处理

```cpp
// MobileShadingRenderer.cpp:431 SetupMobileBasePassAfterShadowInit
void FMobileSceneRenderer::SetupMobileBasePassAfterShadowInit(...)
{
    for (int32 ViewIndex = 0; ViewIndex < AllViews.Num(); ++ViewIndex) {
        FViewInfo& View = *AllViews[ViewIndex];
        FViewCommands& ViewCommands = ViewCommandsPerView[ViewIndex];

        FMeshPassProcessor* MeshPassProcessor = FPassProcessorManager::CreateMeshPassProcessor(
            EShadingPath::Mobile, EMeshPass::BasePass, ...);

        FMeshPassProcessor* BasePassCSMMeshPassProcessor = FPassProcessorManager::CreateMeshPassProcessor(
            EShadingPath::Mobile, EMeshPass::MobileBasePassCSM, ...);
        ...
        Pass.DispatchPassSetup(..., MeshPassProcessor, ..., BasePassCSMMeshPassProcessor,
                               &ViewCommands.MeshCommands[EMeshPass::MobileBasePassCSM]);
    }
}
```

### 14.1 为什么移动端要 "AfterShadowInit"

- BasePass 排序需要 CSM 接收性信息（哪些 mesh 在 CSM 范围内）
- CSM 接收性信息在 ShadowSetupMobile 阶段才生成（`EnableStaticMeshCSMVisibilityState`）
- 因此 BasePass 排序必须延后到 Shadow Init 之后

> Deferred 路径下 `MobileBasePassAlwaysUsesCSM=true`，因此可以无视 CSM 接收性，跳过这套延后逻辑（理论上）。

---

## 15. 项目 TriCluster 改造（已注释，但保留代码）

```cpp
// MobileShadingRenderer.cpp:484-536 [TriCluster] ADD by @Beiyu
#if 0
void FMobileSceneRenderer::SetupMobileTriClusterBasePassAfterShadowInit(...)
{
    ...
    FMeshPassProcessor* MeshPassProcessor = FPassProcessorManager::CreateMeshPassProcessor(
        EShadingPath::Mobile, EMeshPass::TriClusterBasePass, ...);

    Pass.DispatchPassSetup(...);
}
#endif
```

> TriCluster = Triangle Cluster 渲染。移动端 Nanite-like 实验性方案。目前注释，但代码保留待启用。

---

## 16. CVar 与 stat 速查

| 命令 / CVar | 用途 |
|------------|------|
| `stat scenerendering` | DrawCall / MeshDraw 总览 |
| `DumpMeshDrawCommandInstancingStats` | 每 Pass MDC stats |
| `r.MeshDrawCommands.UseParallelSetup` | 并行 Setup |
| `r.GPUScene.MaxPersistentPrimitiveInstances` | GPUScene 容量 |
| `r.GPUScene.Validate` | GPU Scene 校验 |
| `r.InstanceCulling.GPUCulling` | 启用 GPU Culling |
| `r.InstanceCulling.CompactionStage` | 剔除后压缩 |
| `r.PSOPrecache` | PSO 预缓存 |
| `r.PSOPrecache.Validation` | PSO 校验 |
| `r.PSOPrecache.GlobalComputeShaders` | Compute PSO 预缓存 |
| `r.PSOPrecaching` | UE5 新 PSO Precaching |
| `r.Mobile.PSOPrecacheGraphicsOnly` | 仅图形 PSO |

---

## 17. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| 切场景后 DrawCall 翻倍 | MDC 没复用 | 检查 Material 是否被销毁 |
| Forward CSM 不接收 | MobileCSMVisibilityInfo 未更新 | `EnableStaticMeshCSMVisibilityState` |
| ISR 视图丢失实例 | InstanceCullingMode 没设 Stereo | 检查 ViewIds 数量 |
| GPUScene 数据错乱 | UploadDynamicPrimitiveShaderDataForView 顺序错 | 必须在所有 View 之前调用 |
| PSO Cache Miss 卡顿 | Precache 没覆盖 Permutation | `r.PSOPrecache.Validation=1` |
| Deferred 切 Forward 黑屏 | PSO 冲突 | 全量重建 |
| MMH MeshPass 不渲染 | CollectPSOInitializers 没覆盖 | 加 PSO 收集 |
| TriCluster 编译失败 | `#if 0` 关闭 | 项目内启用 |
| InstanceCullingDrawParams 为空 | GPUScene.IsEnabled()=false | 启用 GPUScene |
| Pass 顺序错乱 | DispatchPassSetup 在 ShadowInit 之前 | 必须 AfterShadowInit |

---

> 第 06 篇完。下一篇：**反射系统全谱（Cubemap / Planar / SSR / SSXR / PixelProjected）**。
