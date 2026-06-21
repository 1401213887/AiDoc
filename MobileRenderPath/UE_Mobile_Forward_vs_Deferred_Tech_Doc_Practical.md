# UE Mobile Forward vs Deferred —— 实战篇

> 主文档：`UE_Mobile_Forward_vs_Deferred_Tech_Doc.md`
> 补充篇：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Appendix.md`
> 本文聚焦：**真机抓帧对照**、**Hook 改造点**、**典型性能开销分布**、**调试技巧**与**Forward+ / Deferred Cluster 改造路径**。

---

## 1. RenderDoc 真机抓帧 ─ Pass 对应表

### 1.1 Forward 路径 RenderDoc Event 树（典型）

```
└─ Frame
   ├─ UpdatePrimitives                   (FScene::UpdateAllPrimitiveSceneInfos)
   ├─ InitViews
   │  ├─ OcclusionQueries (HZB)
   │  ├─ ComputeViewVisibility
   │  └─ GatherDynamicMeshElements
   ├─ ShadowDepth                        (RenderShadowDepthMaps)
   │  └─ Whole Scene → CSM
   ├─ SceneColorRendering (Render Pass) ← 单 RenderPass
   │  ├─ ── Subpass 0 ──
   │  │  ├─ MaskedPrePass                (可选)
   │  │  ├─ MobileBasePass ← ★★ GPU 热点
   │  │  ├─ Sky Pass / Fog Pass         (项目)
   │  │  └─ HZB Build                   (可选)
   │  ├─ ── Subpass 1 ──
   │  │  ├─ Decals                       MeshDecal_SceneColor
   │  │  ├─ ModulatedShadows
   │  │  ├─ Fog
   │  │  └─ Translucency
   │  └─ ── Subpass 2 (opt) ──
   │     └─ MobileCustomResolve         (Inline Tonemap)
   ├─ OcclusionTests                     (RenderOcclusion)
   └─ PostProcessing
      ├─ Bloom / DOF / Tonemap
      └─ FXAA / TAA
```

### 1.2 Deferred 路径 RenderDoc Event 树（典型）

```
└─ Frame
   ├─ UpdatePrimitives
   ├─ InitViews
   │  └─ GatherDynamicMeshElements
   ├─ ShadowDepth
   ├─ GatherAndSortLights + ComputeLightGrid
   ├─ ShadowProjection                   (MobileShadowProjections → ShadowMaskTexture)
   ├─ MobileLocalLightsBuffer            (可选，r.Mobile.Forward.LocalLights=2)
   ├─ SceneColorRendering (Render Pass) ← 单 RenderPass，三 Subpass
   │  ├─ ── Subpass 0 (GBuffer Write) ──
   │  │  ├─ MaskedPrePass
   │  │  ├─ MobileBasePass ← ★ 写 GBuffer
   │  │  └─ PostRenderBasePass
   │  ├─ ── Subpass 1 (GBuffer Modify) ──
   │  │  ├─ Decals
   │  │  └─ MeshDecal_SceneColorAndGBuffer
   │  └─ ── Subpass 2 (Lighting) ──
   │     ├─ DeferredShading ← ★★ GPU 热点
   │     │  ├─ DirectionalLight (Inline reflection if可)
   │     │  ├─ ReflectionEnvironmentSkyLighting
   │     │  ├─ SimpleLights
   │     │  └─ LocalLight × N (Stencil Culling)
   │     ├─ (GLES) PLS → SceneColor Copy
   │     ├─ Fog
   │     ├─ (项目) CharacterForwardPass
   │     └─ Translucency
   ├─ MobileHZBOcclusion / SSXR (opt)
   └─ PostProcessing
```

> **关键差别**：Deferred 路径下 RenderDoc 在 SceneColorRendering 内会看到 `MobileDeferredShadingPass`，这是 Forward 路径完全没有的全屏 LightingPass。

---

## 2. 典型耗时分布（中端 Adreno 730 / Mali G715 实测参考）

> 数据为粗略量级，仅供方向判断。具体值因项目而异。

### 2.1 1080P 中等场景（4 个动态光源 + CSM + 1 个反射球）

| Pass | Forward (ms) | Deferred (ms) | 备注 |
|------|-------------|---------------|------|
| Shadow Depth | 1.2 | 1.2 | 共用 |
| LightGrid | 0 | 0.3 | Forward 默认不算 |
| MobileShadowProjections | 0.3 | 0.3 | 共用 |
| MobileBasePass | **4.5** | **2.1** | Forward 内含光照计算 |
| Decals | 0.4 | 0.5 | Deferred 多了 GBuffer 写 |
| MobileDeferredShading | – | **2.8** | Deferred 全屏 LightingPass |
| Fog/Translucency | 1.0 | 1.0 | 共用 |
| PostProcessing | 1.5 | 1.5 | 共用 |
| **合计** | **~8.9** | **~9.7** | 此场景 Forward 略优 |

### 2.2 同场景但 16 个动态光源

| Pass | Forward (ms) | Deferred (ms) | 备注 |
|------|-------------|---------------|------|
| LightGrid | 0.3 | 0.3 | 共用 |
| MobileBasePass | **7.5** | **2.2** | Forward LocalLight 循环开销 |
| MobileDeferredShading | – | **3.6** | Local Light 多了一些 stencil 体 |
| 其他 | 5.0 | 5.0 | – |
| **合计** | **~12.8** | **~11.1** | Deferred 反超 |

### 2.3 同场景 + 5 个 LightFunction + SSR

| Pass | Forward (ms) | Deferred (ms) | 备注 |
|------|-------------|---------------|------|
| MobileBasePass | **9.0** | **2.3** | Forward LightFunction 实现复杂 |
| MobileDeferredShading | – | **5.2** | + LightFunction Permutation |
| SSR | – | **1.8** | Forward 路径下默认不支持 |
| **合计** | **~16.5** | **~12.8** | Deferred 明显更优 |

### 2.4 经验阈值

| 项目特征 | 推荐路径 |
|---------|---------|
| ≤4 个常亮动态光 + 重材质（角色多） | Forward |
| ≥8 个动态光 + 大场景 | Deferred |
| 必须有 SSR / SSXR | Deferred |
| 必须 MSAA | Forward |
| Inline Tonemap 节省后处理 RP | Forward (Vulkan) |
| 写实 PBR + GI | Deferred + LuxGI |
| 卡通 + 多 LightFunction | Deferred + Forward Character Pass（本项目方案） |

---

## 3. 项目最常用的 Hook 改造点

### 3.1 BasePass 添加自定义输出（参考 GR Toon 项目）

如本工程对 BasePass PS 增加了 `OutCharRenderMask : SV_Target4`，对应 C++ 端：

1. **`MobileShadingRenderer.cpp` GetColorTargets_Deferred** 增加新 RT：
   ```cpp
   if (CVar_OutputCharMask) ColorTargets.Add(SceneTextures.MobileCharFeatureTexture);
   ```
2. **`SceneTextures.cpp`** 创建对应 RDG 纹理。
3. **`MobileBasePassPixelShader.usf`** 增加 `#if MOBILE_DEFERRED_EXPORT_MRT` 输出。
4. **`MobileBasePassRendering.h::ModifyCompilationEnvironment`** 注入 `MOBILE_DEFERRED_EXPORT_MRT` 宏。
5. 注意调整 `SV_TargetDepthAux` 的 slot 偏移。

### 3.2 给 Forward 加 Forward+ Cluster 改造

```
Step 1: 启用 LightGrid
  r.Mobile.Forward.EnableLocalLights = 1   // ENABLE_CLUSTERED_LIGHTS
Step 2: MobileBasePassPixelShader.usf
  #if ENABLE_CLUSTERED_LIGHTS
    AccumulateLightGridLocalLightingToon(...)
  #endif
Step 3: 调高单 Cell 光源容量
  修改 FForwardLightingResources::FForwardLightGridCellLightCount
Step 4: 在 RenderForward 调度阶段加 ComputeLightGrid
```

### 3.3 给 Deferred 加 Character Forward Pass（本工程做法）

```
Step 1: 注册新 MeshPass
  EMeshPass::MobileCharacterForwardPass
Step 2: View.ParallelMeshDrawCommandPasses[...] 在 BuildInstanceCullingDrawParams 中
  CharacterForwardInstanceCullingDrawParams = ...
Step 3: 在 RenderDeferredSinglePass 的 Lighting Subpass 内
  if (bUseMobileCharacterForwardPass) RenderCharacterForward(RHICmdList, View);
Step 4: BasePass PS 加判断
  #define DEFERRED_SHADING_PATH (... && !MATERIAL_SHADINGMODELS_TOON_CHARACTER)
```

### 3.4 Tile-based Subpass Inline Tonemap（Vulkan 专属）

```cpp
// 启用：
bTonemapSubpassInline = IsMobileTonemapSubpassEnabledInline(...)
                     && bTonemapSubpass;
// 调度：
PassParameters->RenderTargets.SubpassHint = ESubpassHint::CustomResolveSubpass;
// 末尾子 Pass：
RHICmdList.NextSubpass();
RenderMobileCustomResolve(RHICmdList, View, NumMSAASamples, SceneTextures);
```

Forward 专属，节省一次 SceneColor 出 Tile。

---

## 4. Shader 调试要点

### 4.1 用 `#define MOBILE_DEFERRED_SHADING 1` 强制走 Deferred 分支

在 `MobileBasePassPixelShader.usf` 临时加：

```hlsl
#undef MOBILE_DEFERRED_SHADING
#define MOBILE_DEFERRED_SHADING 1
```

可在本地 Shader 编辑器/PIX 中比对 Forward / Deferred 输出差异。

### 4.2 GBuffer 可视化

在 `MobileBasePassPixelShader.usf` 替换：

```hlsl
MobileEncodeGBuffer(GBuffer, OutGBufferA, OutGBufferB, OutGBufferC, OutGBufferD);
// 调试：
OutGBufferA.rgb = GBuffer.WorldNormal * 0.5 + 0.5;
OutGBufferB.rgba = float4(GBuffer.Metallic, GBuffer.Specular, GBuffer.Roughness, GBuffer.ShadingModelID / 16.0);
OutGBufferC.rgb = GBuffer.BaseColor;
```

### 4.3 Stencil Mask 可视化

```cpp
// MobileDeferredShadingPass.cpp 在 RenderDirectionalLight 后插入：
GraphBuilder.AddPass("VisualizeStencil", ..., [](FRHICommandList& RHICmdList) {
    // 用 Stencil mask 输出灰度
});
```

### 4.4 Forward / Deferred 切换的脚本

```ini
; DefaultEngine.ini
[/Script/Engine.RendererSettings]
r.Mobile.ShadingPath=0   ; Forward
r.Mobile.ShadingPath=1   ; Deferred
```

> 切换后必须重启 Editor / 重启 PIE，因为 `IsMobileDeferredShadingEnabled` 走 DDPI cache。

---

## 5. PhongApprox vs GGX：BRDF 的 Mobile 化

参考 `BRDF.ush` 中的 `PhongApprox`（移动端 BasePass 的高光近似）：

```hlsl
half PhongApprox(half Roughness, half RoL)
{
    half a  = Roughness * Roughness;
    a       = max(a, 0.008);                  // FP16 安全下界
    half a2 = a * a;
    half rcp_a2 = rcp(a2);
    half c  = 0.72134752 * rcp_a2 + 0.39674113;
    half p  = rcp_a2 * exp2(c * RoL - c);     // 球谐高斯近似
    return min(p, rcp_a2);                    // Mali GPU 防溢出
}
```

| 位置 | Forward 用 | Deferred 用 |
|------|-----------|-------------|
| Forward BasePass 主光高光 | ✅ PhongApprox | – |
| Deferred LightingPass 主光高光 | – | 走 `AccumulateDirectionalLighting` 内部完整 GGX（成本可控因为是全屏后） |
| Mobile 反射球 | EnvBRDFApprox（两者通用） | EnvBRDFApprox |

> **核心思想**：Forward 路径下高光算法必须极致简化（每物体每像素都执行），因此用 PhongApprox；Deferred 路径下 LightingPass 因为是延迟一次，可以负担相对完整的 GGX/EnvBRDF。

---

## 6. 多视图 / VR / Stereo 差异

```cpp
// MobileShadingRenderer.cpp
// Forward
BasePassRenderTargets.MultiViewCount = MainView.bIsMobileMultiViewEnabled ? 2
                                      : MainView.Aspects.IsMobileMultiViewEnabled() ? 1
                                      : 0;
// Deferred
BasePassRenderTargets.MultiViewCount = Views[0].bIsMobileMultiViewEnabled ? 2
                                      : Views[0].Aspects.IsMobileMultiViewEnabled() ? 1
                                      : 0;
```

| 维度 | Forward | Deferred |
|------|---------|----------|
| Mobile MultiView | ✅ | ✅ |
| Instanced Stereo | ✅ | ✅ |
| Tonemap Subpass + MultiView | ✅ | ❌（Subpass 在 Stereo 下混乱） |
| VR 推荐 | ✅（VR 多用 Forward + MSAA） | ❌（带宽 + MSAA 不兼容） |

---

## 7. 后处理输入数据准备

```cpp
// MobileShadingRenderer.cpp:721
bKeepDepthContent =
    bRequiresMultiPass ||
    bForceDepthResolve ||
    bRequiresPixelProjectedPlanarRelfectionPass ||
    bSeparateTranslucencyActive ||
    Views[0].bIsReflectionCapture ||
    (bDeferredShading && bPostProcessUsesSceneDepth) ||   // ← Deferred 特殊条件
    (bDeferredShading && bSceneDepthCapture) ||           // ← Deferred 特殊条件
    Views[0].AntiAliasingMethod == AAM_TemporalAA ||
    bRequireSeparateViewPass ||
    bIsFullDepthPrepassEnabled ||
    bShouldRenderHZB ||
    bHZBOcclusion ||
    GraphBuilder.IsDumpingFrame();
```

> Deferred 路径下，**任何后处理读 SceneDepth** 都会触发 KeepDepthContent，**深度强制从 Tile 拷到主存**。这是 Deferred 路径多消耗带宽的一个隐藏成本。

---

## 8. 测量项目实际带宽差异的方法

### 8.1 ARM Streamline / Adreno Profiler

监控：
- L2 Read / Write Bandwidth
- Tile Memory Hit Rate
- GMEM Load / Store

### 8.2 RenderDoc 自带 Stat

```
Counters → GPU Stats
  • IA Vertices
  • PS Invocations
  • Color Write Pixels
  • Depth Test Pixels
```

### 8.3 关键计算

```
Forward 带宽 (出 Tile) ≈ ResolutionX × ResolutionY × 64bit (SceneColor) × FrameRate
                       ≈ 1920×1080×8B×60Hz ≈ 1.0 GB/s

Deferred 带宽 (无 FBF, MultiPass) ≈ 上述 × (1 + 3×4B/8B) ≈ 1.0 GB/s × 2.5 ≈ 2.5 GB/s
                                ← 因为 GBuffer 也必须从主存读回 LightingPass
```

> 因此 **Deferred 必须确保 FBF/PLS/Subpass 全程在 Tile 内**，否则带宽会翻倍。

---

## 9. 常见性能 PITFALL

### 9.1 Deferred + 透明 Decal 滥用

每个透明 MeshDecal_SceneColorAndGBuffer 都会触发一次 GBuffer 修改 + 重新 Tile load，建议：
- 合并到一张 DecalAtlas
- 优先 SceneColor-only Decal

### 9.2 Forward + 强光照 BasePass 复杂材质

Forward BasePass PS 指令数过高（>200 指令）会让 ALU 成为瓶颈，建议：
- 启用 `MOBILE_QL_FORCE_FULLY_ROUGH` 对远处或低质量档
- 用 LightGrid 替代逐物体 4 点光

### 9.3 Deferred 半透明物体反复 fetch GBuffer

Subpass2 中半透明物体的 forward 着色 fetch GBuffer 的成本随半透明像素数累加，建议：
- 限制半透明物体面积
- 在远处提前 sort & clamp

### 9.4 ShadowMaskTexture 全屏成本

ShadowMaskTexture 需要全屏渲染一次（虽然质量较低，但仍是全屏），Forward 路径下还会被 BasePass PS 二次采样，建议：
- 关闭项目无用的距离场阴影
- `r.Shadow.Virtual.Enable` 决策（5.5+ 可考虑）

---

## 10. 调试 / 性能分析命令汇总

| 命令 | 作用 |
|------|------|
| `ProfileGPU` | 单帧 GPU 各 Pass 耗时 |
| `stat unit` | 每帧 CPU/GPU 总览 |
| `stat scenerendering` | DrawCall / MeshDraw 数 |
| `stat InitViews` | 可见性耗时 |
| `r.Mobile.ShadingPath ?` | 查当前路径 |
| `r.Mobile.Forward.EnableLocalLights ?` | 查 LightGrid 状态 |
| `r.Mobile.UseClusteredDeferredShading ?` | Deferred Cluster |
| `r.MobileMSAA ?` | MSAA 档 |
| `r.Mobile.LocalExposure ?` | 局部曝光位置 |
| `r.Mobile.AmbientOcclusion ?` | AO 启用 |
| `r.Mobile.EarlyZPass ?` | Z PrePass |
| `viewmode lit/unlit/shadercomplexity/lightcomplexity/lightingonly` | 视图模式 |
| `showflag.MeshEdges 1` | 网格线框 |
| `showflag.Decals 0` | 关 Decals 调试 |

---

## 11. 项目实战检查清单（GR / Toon 类）

- [ ] 是否正确开启 `MOBILE_DEFERRED_SHADING`？（构造 Render 时）
- [ ] 是否所有 ShadingModel Stencil 都正确写入？
- [ ] Toon 角色路径是否被 `MOBILE_CHARACTER_FORWARD` 影响？
- [ ] `MobileUsesExtenedGBuffer` 项目改造是否符合目标平台？
- [ ] LightGrid 容量是否够用？
- [ ] LuxGI 是否在 Deferred 下启用 / Forward 下回退？
- [ ] 是否所有目标机型都支持 FBF/PLS/Subpass？
- [ ] Forward 路径下是否开启 MSAA 节省后处理？
- [ ] ShadowMaskTexture 是否每帧都生成？
- [ ] 是否有不必要的 KeepDepthContent 路径？
- [ ] 半透明物体数量是否合理？

---

## 12. 推荐继续阅读的源码 / 文档

### 引擎源码

1. `Engine/Shaders/Private/ToonMobileLightingCommon.ush` — 本项目 Toon 着色入口
2. `Engine/Shaders/Private/ReflectionEnvironmentShared.ush` — LQ/HQ 反射混合
3. `Engine/Shaders/Private/ShadowFilteringCommon.ush` — PCF / 滤波
4. `Engine/Shaders/Private/LuxIrradianceVolume/LuxGI*.usf` — GI 集成
5. `Engine/Source/Runtime/Renderer/Private/ScreenSpaceRayTracing.cpp` — SSR/SSXR
6. `Engine/Source/Runtime/Renderer/Private/SceneVisibility.cpp` — 剔除
7. `Engine/Source/Runtime/Renderer/Private/MeshPassProcessor.cpp` — MDC 框架

### Epic 官方文档

- [Mobile Rendering and Shading Modes](https://dev.epicgames.com/documentation/en-us/unreal-engine/mobile-rendering-and-shading-modes-for-unreal-engine)
- [Mobile Deferred Renderer Setup](https://docs.unrealengine.com/...)
- [Software Occlusion Queries for Mobile](https://dev.epicgames.com/documentation/zh-cn/unreal-engine/software-occlusion-queries-for-mobile)
- [Mobile Forward Renderer Improvements (UE5.5)](https://docs.unrealengine.com/...)

### 移动 GPU 厂商

- [ARM: Best Practices for Mobile Rendering with Unreal Engine](https://developer.arm.com/...)
- [Qualcomm: Achieve 30 FPS in UE5 on Snapdragon](https://www.qualcomm.com/developer/blog/2026/01/run-unreal-engine-5-content-30fps-snapdragon-mobile)
- [Apple Metal: Frame Buffer Fetch](https://developer.apple.com/metal/...)
- [PowerVR: Tile-Based Deferred Rendering Best Practices](https://developer.imaginationtech.com/...)

### 社区文章

- UE4 Forward+ 实现：https://blog.csdn.net/kuangben2000/article/details/135188219
- Mobile Forward Cluster 改造：https://blog.csdn.net/qq_29523119/article/details/123102447
- MobileBasePass Shader Binding：https://qiutang98.github.io/post/unreal/ue4.26-lightmap从烘焙到渲染/
- UE5.5 移动端总览：https://blog.csdn.net/boxiaozi/article/details/159355298

---

> **实战篇结束。**
>
> 三份文档配合使用：
> - **主文档** 讲"理论与机制" → 让你理解 Forward / Deferred 在 UE Mobile 里到底差在哪
> - **补充篇** 讲"项目补丁与细节" → 让你能调试本工程的特殊状况
> - **实战篇** 讲"性能、改造、调试" → 让你能落地优化和改造
>
> 通读这三份文档 + 参照"推荐学习路径"过一遍源码，应可独立胜任移动端渲染管线分析、改造与优化工作。
