# UE Mobile Forward vs Deferred —— 补充篇

> 主文档：`UE_Mobile_Forward_vs_Deferred_Tech_Doc.md`
> 本文聚焦本工程的项目层补丁、Subpass 状态机、Tile Memory 估算、Stencil 位布局、ShadowMaskTexture 生命周期、LightGrid 复用、LuxGI 集成等更细微差异。

---

## A. 本工程的关键 Patch 列表（与官方 5.5 不一致的点）

| 位置 | 原版 | 项目改造 | 影响 |
|------|------|---------|------|
| `RenderUtils.cpp:651` `MobileUsesExtenedGBuffer` | 平台相关返回 true/false | `(Platform != SP_OPENGL_ES3_1_ANDROID) && false` → **永远 false** | 项目**全平台只有 4 个 MRT（SceneColor + GBufferA/B/C）**，无 GBufferD |
| `MobileBasePass.cpp:87` `GetMobileShadingModelStencilValue` | 区分 Lit/Unlit/Custom | 注释掉旧逻辑，**所有非 Unlit 返回 1u** | Stencil 中 ShadingModel 位被项目复用为"Render on Top + LightMap"标记 |
| `MobileDeferredShadingPass.cpp:758` `ShadingModelStencilRef[0]` | `PassShadingModelStencilValue(false)` | `GET_STENCIL_MOBILE_SM_MASK(0u)` | bit_1 = Render_on_Top, bit_2 = LightMap |
| `MobileBasePassPixelShader.usf:115` `DEFERRED_SHADING_PATH` | `MOBILE_DEFERRED_SHADING && (SOLID\|MASKED) && !SLW` | 额外 `&& !MATERIAL_SHADINGMODELS_TOON_CHARACTER` | Toon 角色在 Deferred 总开关下仍走 Forward 路径 |
| `MobileBasePassPixelShader.usf:113` `MOBILE_USE_GBUFFER` | 同上 | 同上（但少了 Toon 排除）  | GBuffer 仍要写，光照逻辑跳过——**这是个特殊状态**：Toon 角色写 GBuffer 但 BasePass 内自己算光，可能存在重复 |
| `MobileBasePassPixelShader.usf:383` | 标准签名 | 增加 `out uint OutCharRenderMask : SV_Target4` | 项目专属角色掩码 |
| `MobileShadingRenderer.cpp:2363` | 不存在 | `RenderCharacterForward(...)` | Deferred 流程内再插入一个角色 Forward Pass |
| `MobileDeferredShading.usf:24-25` | 直接 include `MobileLightingCommon.ush` | 替换为 `ToonMobileLightingCommon.ush` | Toon 兼容 |
| `MobileShadingRenderer.cpp:687` `bRequiresShadowProjections` | `MobileUsesShadowMaskTexture` | `MobileUsesShadowMaskTextureRuntime` | 运行时版本，允许动态切换 |
| `MobileBasePass.cpp:1071-1075` | `GetPrimaryPrecomputedShadowMask(...)` | 硬编 `GBuffer.PrecomputedShadowFactors = 1.0;` | 项目 `GR_STATIC_LIGHTING(by JLP)` |

> **结论：** 本工程在 Deferred 主路径上做了一次"Toon 反向 Forward"重构，使得`MOBILE_USE_GBUFFER` 与 `DEFERRED_SHADING_PATH` 这两个本应等价的宏出现轻微不等，需要在调试时特别注意。

---

## B. 细化的 Subpass 状态机

### B.1 Forward Single Pass（4 个 Subpass 状态）

```
   Subpass 0 (SceneColor write, Depth write)
   ├─ DrawClearQuad (Editor)
   ├─ RenderMaskedPrePass            // 如 r.MobileEarlyZPass=1
   ├─ RenderMobileBasePass           // ★ BRDF + 光照
   ├─ RenderMobileDebugView          // 复杂度/光照等可视化
   └─ PostRenderBasePass             // ViewExtension hooks

   ─── RHICmdList.NextSubpass() ─── (变成 Depth Read)

   Subpass 1 (SceneColor write, Depth read-only via subpass)
   ├─ RenderDecals
   ├─ RenderModulatedShadowProjections
   ├─ RenderFog
   └─ RenderTranslucency

   ─── 如 bDoOcclusionQueries: RenderOcclusion ───
   ─── 如 iOS: PreTonemapMSAA ───

   Subpass 2 (Optional, CustomResolveSubpass)
   └─ RenderMobileCustomResolve      // Inline Tonemap + Resolve
```

### B.2 Deferred Single Pass（3 个 Subpass 状态）

```
   Subpass 0 (SceneColor + GBufferABC[D] + DepthAux write, Depth write)
   ├─ RenderMaskedPrePass
   ├─ RenderMobileBasePass           // 写 GBuffer + emissive
   ├─ RenderMobileDebugView
   ├─ PostRenderBasePass
   └─ VisualizeLuxLightProbesForView (项目)

   ─── NextSubpass ───

   Subpass 1 (GBuffer write, Depth read-only)
   └─ RenderDecals                   // 通过修改 GBuffer 把材质改色

   ─── NextSubpass ───

   Subpass 2 (SceneColor write, GBuffer & Depth read-only)
   ├─ MobileDeferredShadingPass      // ★ Directional + Local + Reflection + SkyLight
   ├─ (GLES) MobileDeferredCopyBuffer<PLSPS>   // 把 PLS 拷回 SceneColor
   ├─ RenderFog
   ├─ (项目) RenderCharacterForward(可选)
   └─ RenderTranslucency             // 半透明 forward 着色（在 subpass 内 FBF 读 GBuffer）

   ─── 如 bDoOcclusionQueries: RenderOcclusion ───
```

### B.3 Subpass 提示来源

```cpp
// MobileBasePass.cpp:1091
uint8 SubpassIndex = bTranslucentBasePass ? (bDeferredShading ? 2 : 1) : 0;
ESubpassHint SubpassHint = GetSubpassHint(GMaxRHIShaderPlatform, bDeferredShading,
                                          RenderTargetsInfo.MultiViewCount > 1,
                                          RenderTargetsInfo.NumSamples);
```

> 半透明 MeshDrawCommand 自带 `SubpassIndex=1`(Forward) 或 `=2`(Deferred)，PSO 创建时就被纳入正确的 RenderPass 配置。

---

## C. Tile Memory 占用估算（典型移动 GPU）

### C.1 Forward 路径（典型场景：HDR + 1920×1080）

| Target | 格式 | bit/pixel |
|--------|------|-----------|
| SceneColor | PF_FloatRGBA (R16G16B16A16_FLOAT) | 64 |
| SceneDepth | D24S8 | 32 |
| SceneDepthAux | PF_R32_FLOAT 或 PF_R16F | 32 |
| MSAA 4x（可选） | ×4 | +192 |
| **小计（无 MSAA）** | | **128 bit / pixel** |
| **小计（MSAA 4x）** | | **320 bit / pixel** |

### C.2 Deferred 路径（本工程，无 GBufferD）

| Target | 格式 | bit/pixel |
|--------|------|-----------|
| SceneColor | PF_FloatRGBA | 64 |
| GBufferA | PF_A2B10G10R10 或 FloatRGBA | 32~64 |
| GBufferB | RGBA8 | 32 |
| GBufferC | RGBA8 sRGB | 32 |
| SceneDepth | D24S8 | 32 |
| SceneDepthAux（如开） | R16F | 32 |
| (项目) OutCharRenderMask | R8_UINT | 8 |
| **小计** | | **224~264 bit / pixel** |

### C.3 解释

- 主流移动 GPU Tile 容量：
  - Mali Bifrost/Valhall: 16 KB Tile
  - Adreno 6xx: 1.5 MB GMEM（按子区域）
  - Apple GPU: 32 KB Tile
  - PowerVR: 256 KB FastSRAM
- Forward 128 bit/pixel 下，**Mali 可塞 16x16~32x32 Tile**，Deferred 264 bit/pixel 可能要切成更小 Tile，吞吐下降 ~20%。
- 因此 Deferred 在中低端机型上必须确保 `RequiresMultiPass()` 返回 false（FBF/PLS/Subpass），否则将每帧把 ~256 bit/pixel 的 GBuffer 拷出主存，**带宽爆炸**。

---

## D. Stencil Bits 布局对比

```cpp
// 共享：本工程已经把 ShadingModel bit 复用
// bit 0     : Render On Top (Mega 添加)
// bit 1     : LightMap flag (Mega 添加)
// bit 2~4   : Lighting Channels (3 个通道)
// bit 5     : Mobile Cast Contact Shadow (Forward Only)
// bit 6     : Receive Decal
// bit 7     : Stencil Sandbox (Deferred Light 剔除用)
```

### Forward 写 Stencil

```cpp
// MobileBasePassRendering.cpp:122 (else 分支)
StencilValue |= GET_STENCIL_BIT_MASK(MOBILE_CAST_CONTACT_SHADOW, CastContactShadows);
StencilValue |= GET_STENCIL_BIT_MASK(RECEIVE_DECAL, ReceiveDecals);
```

### Deferred 写 Stencil

```cpp
// MobileBasePassRendering.cpp:105
uint8 ShadingModel = 0u;
if(bUseLightMap) ShadingModel += 1u;  // bit_2 = lightmap
StencilValue |= GET_STENCIL_MOBILE_SM_MASK(ShadingModel);
StencilValue |= STENCIL_LIGHTING_CHANNELS_MASK(ProxyChannels);
```

### LightingPass 读 Stencil（Deferred）

```cpp
// MobileDeferredShadingPass.cpp:602 SetDirectionalLightDepthStencilState
PassShadingModelStencilMask(bEnableShadingModelSupport)
| STENCIL_LIGHTING_CHANNELS_MASK(1u << LightingChannelIdx)
```

> Stencil 比较是 `CF_Equal`，按 Mask 比对，使得"只有该光照通道的像素才执行光照"。LocalLight 还会用 `STENCIL_SANDBOX_MASK` 临时位标记"光体内部"。

---

## E. ShadowMaskTexture 全生命周期

### Forward 路径

```
RenderShadowDepthMaps
  → RenderMobileShadowProjections
      → 输出到 GScreenSpaceShadowMaskTextureMobileOutputs.ScreenSpaceShadowMaskTextureMobile
RenderMobileBasePass
  → BasePass PS 通过 MobileBasePass.ScreenSpaceShadowMaskTexture 采样
  → BasePass PS 通过 USE_SHADOWMASKTEXTURE 宏决定是否乘上
```

### Deferred 路径

```
RenderShadowDepthMaps
  → RenderMobileShadowProjections  (同样输出 ScreenSpaceShadowMaskTexture)
MobileDeferredShadingPass
  → FMobileDirectionalLightFunctionPS PassParameters.ScreenSpaceShadowMaskTexture
  → FUseShadowMaskTexture Permutation 决定是否乘
```

> **共享**：两条路径共用同一张 ScreenSpaceShadowMaskTexture，区别只是采样点不同。

### Runtime 切换

```cpp
// MobileShadingRenderer.cpp:687 项目改用 Runtime 版本
bRequiresShadowProjections = MobileUsesShadowMaskTextureRuntime(ShaderPlatform) && ...;
```

允许游戏运行时动态切换是否生成 ShadowMaskTexture。

---

## F. LightGrid 复用：Forward → Deferred Inline

### F.1 准备

```cpp
// MobileShadingRenderer.cpp:1305
if (bDeferredShading || bEnableClusteredLocalLights || bEnableClusteredReflections)
{
    GatherAndSortLights(SortedLightSet, bShadowedLightsInClustered);
    bool bCullLightsToGrid =
        ((bEnableClusteredReflections || bDeferredShading) && NumReflectionCaptures > 0)
        || bEnableClusteredLocalLights;
    if (bCullLightsToGrid)
        ComputeLightGrid(GraphBuilder, bEnableClusteredLocalLights, SortedLightSet);
}
```

### F.2 Forward 使用

- `MERGED_LOCAL_LIGHTS_MOBILE == 2` 时 BasePass PS 直接读 `LightGrid` 做 inline 多光源
- 反射 cluster 时 BasePass PS 读 `LightGrid` 拿候选 Cubemap

### F.3 Deferred 使用

- `UseClusteredDeferredShading(Platform)` 时 LightingPass Inline `MobileDirectionalLightFunctionPS` 在同一像素位置走 LightGrid（`ENABLE_CLUSTERED_LIGHTS` Permutation = true），减少 DrawCall
- 反射也通过 `ENABLE_CLUSTERED_REFLECTION` Permutation 复用 LightGrid

### F.4 共享数据结构

二者完全共享 `FForwardLightData`、`SortedLightSet`、`LightGrid` 三大结构；区别只是 **谁来消费**。这就是为什么 `r.Mobile.UseClusteredDeferredShading=1` 必须先开 `r.Mobile.Forward.EnableLocalLights=1`。

---

## G. LocalLightsBuffer：Forward 专属优化

```cpp
// MobileShadingRenderer.cpp:1537
if (bRendererOutputFinalSceneColor)
{
    RenderMobileLocalLightsBuffer(GraphBuilder, SceneTextures, SortedLightSet);
}
```

```cpp
// MobileBasePassRendering.cpp:21
bool MobileLocalLightsBufferEnabled(...) { return MobileForwardLocalLights(Platform) == 2; }
bool MobileMergeLocalLightsInPrepassEnabled(...) {
    return MobileLocalLightsBufferEnabled(...) && MobileUsesFullDepthPrepass(...);
}
bool MobileMergeLocalLightsInBasepassEnabled(...) {
    return MobileLocalLightsBufferEnabled(...) && !MobileUsesFullDepthPrepass(...);
}
```

> 这是 Forward 路径下"把多光源在 PrePass 阶段合成到 LocalLightTextureA/B 两张贴图"的优化（4.27+ 出现）。Deferred 路径不需要——光源天然在 LightingPass 累加。

Shader 端对应：

```hlsl
// MobileBasePassPixelShader.usf:1165
#if MERGED_LOCAL_LIGHTS_MOBILE == 1
    half3 LocalLightA = MobileSceneTextures.LocalLightTextureA.Load(int3(SvPos.xy,0)).xyz;
    half4 LocalLightB = MobileSceneTextures.LocalLightTextureB.Load(int3(SvPos.xy,0));
    // 重建方向 + 颜色 + 镜面缩放，调用 AccumulateDynamicLighting
#elif MERGED_LOCAL_LIGHTS_MOBILE == 2
    // 走 LightGrid 路径
#endif
```

---

## H. Full Depth PrePass —— 两条路径都受影响

```cpp
// MobileShadingRenderer.cpp:345
bIsFullDepthPrepassEnabled = (Scene->EarlyZPassMode == DDM_AllOpaque ||
                              Scene->EarlyZPassMode == DDM_AllOpaqueNoVelocity);

// RenderUtils.cpp:770
bool MobileUsesFullDepthPrepass(Platform) {
    return MobileUsesShadowMaskTextureRuntime(Platform)
        || IsUsingDBuffers(Platform)
        || FReadOnlyCVARCache::MobileEarlyZPass(Platform) == 1;
}
```

| 情况 | Forward | Deferred |
|------|---------|----------|
| 启用条件 | ShadowMaskTexture / DBuffer / r.Mobile.EarlyZPass=1 | ShadowMaskTexture / r.Mobile.EarlyZPass=1（无 DBuffer） |
| BasePass DepthStencil 访问 | `DepthRead_StencilWrite` | `DepthRead_StencilWrite` |
| RT0 LoadAction | ELoad | ELoad（GBuffer 仍 Clear） |
| 影响 | 减少 BasePass overdraw | 同上，但更大收益（GBuffer 写入贵） |

---

## I. LuxGI（项目集成）—— 仅在 Deferred 生效

```cpp
// RenderUtils.cpp:669
bool PlatformSupportLuxGI(const FStaticShaderPlatform Platform)
{
    return (GetMaxSupportedFeatureLevel(Platform) >= ERHIFeatureLevel::ES3_1)
        && (!IsMobilePlatform(Platform) || IsMobileDeferredShadingEnabled(Platform));
}
```

LuxGI 在 BasePass 与 LightingPass 不同位置 hook：

| Pass | 调用 | 数据流 |
|------|------|--------|
| BasePass PS | `AccumulateLuxGILighting(GBuffer, ...)` 仅 Forward 或 Toon Character | 写 IndirectDiffuse |
| MobileDirectionalLightPS | `AccumulateLuxGILighting(...)` (`MobileDeferredShading.usf:279`) | 同上 |
| LightingPass | `FLuxGIEnableAvoidLightLeaking` Permutation | VSM 防漏 |

> 由于 LuxGI 数据依赖 GBuffer.Normal/WorldPosition/Roughness，**只有 Deferred 路径才能享受完整 LuxGI**；Forward 路径下要么不开 LuxGI，要么走 BasePass 内的近似实现。

---

## J. 半透明 Shader 路径选择详解

```cpp
// MobileBasePassRendering.h:413
const bool bIsLit                  = ShadingModels.IsLit();
const bool bDeferredShadingEnabled = IsMobileDeferredShadingEnabled(Platform);
const bool bIsTranslucent          = IsTranslucentBlendMode(...) || HasShadingModel(MSM_SingleLayerWater);
const bool bIsToonCharacter        = HasAnyShadingModel({MSM_ToonStandard, MSM_ToonSkin, MSM_ToonHair, MSM_ToonFace, MSM_ToonEyeBrow});
const bool bMaterialUsesForwardShading = bIsLit && (bIsTranslucent || bIsToonCharacter);
const bool bForwardShading         = !bDeferredShadingEnabled || bMaterialUsesForwardShading;
```

> **半透明 + Toon Character → 永远 Forward Shading**，即使主路径是 Deferred。
> 这导致 Shader Permutation 数量比纯 Deferred 项目大一些（多了一组 Toon Forward Shader）。

---

## K. RenderTarget LoadAction 矩阵

| 路径 | 视图 | RT0 | RT1 (DepthAux 或 GBufferA) | RT2~3 (GBufferB/C) | Depth | Stencil |
|------|------|-----|---------------------------|-------------------|-------|---------|
| Forward First View | First | Clear | Clear | – | Clear | Clear |
| Forward Other View | Non-first | Load | Load | – | Load | Load |
| Deferred First View | First | Clear | Clear | Clear | Clear | Clear |
| Deferred Other View | Non-first | Load | Load | Load | Load | Load |
| MultiPass 第二个 RenderPass | – | Load | – (清空) | – | Load | Load |

> Forward 路径下"Other View"仅触发 RT0/RT1/Depth Load；Deferred 路径下额外需要把 GBufferA/B/C 的 LoadAction 改成 Load——一旦 GBuffer 出 Tile，**带宽代价更明显**。

---

## L. 易错点 / 排查 Cheatsheet

| 现象 | 可能原因 | 定位代码 |
|------|---------|----------|
| Deferred 下材质亮度异常 | `MOBILE_USE_GBUFFER` 与 `DEFERRED_SHADING_PATH` 由于 Toon 排除不一致 | `MobileBasePassPixelShader.usf:113-117` |
| Toon 角色不接收延迟光 | 项目主动让 Toon 走 Forward | `MOBILE_CHARACTER_FORWARD` |
| GLES 上 Deferred 卡 | 缺 FBF/PLS，走 MultiPass | `RequiresMultiPass()`, `UsingPixelLocalStorage()` |
| Deferred 切场景出现黑屏 | LightingPass Stencil mask 没正确清除 | `RenderLocalLight_StencilMask` SO_Zero |
| 半透明物体在 Deferred 下没光 | `IS_MOBILE_DEFERREDSHADING_SUBPASS` 误为 0 | 检查 `bIsMobileSeparateTranslucencyEnabled` |
| 切 Forward/Deferred 后 LocalLight 渲染数量陡变 | `r.Mobile.UseClusteredDeferredShading` 与 `r.Mobile.Forward.EnableLocalLights` 联动 | `UseClusteredDeferredShading()` |
| Forward 路径下 SSR 不生效 | SSR 仅 Deferred 项目级实现 | `AreMobileScreenSpaceReflectionsEnabled` |
| 项目报错 GBuffer 数量不对 | 项目把 `MobileUsesExtenedGBuffer` 改成永假 | `RenderUtils.cpp:651` |
| ShadowMask 在 Deferred 看不到效果 | `FUseShadowMaskTexture` Permutation 未走到 | `BuildPermutationVector` |
| Decal 写不到 GBuffer 颜色 | Forward 路径下走错 Pass | `EMeshPass::MeshDecal_SceneColorAndGBuffer` |
| Inline Tonemap 黑屏 | 只在 Forward Vulkan 单 Pass 启用 | `IsMobileTonemapSubpassEnabledInline` |
| MSAA 配 Deferred 报错 | 不兼容，强制 1x | `bMemorylessMSAA / NumMSAASamples` 检查 |

---

## M. 推荐学习路径（修订自主文档）

针对本工程，建议按以下顺序阅读：

1. `RenderUtils.cpp` 末尾的所有 `Mobile***` 函数（决定平台特性）
2. `FMobileSceneRenderer` 构造函数（一次性确定 bDeferredShading 等所有 flag）
3. `RenderForwardSinglePass` + `RenderDeferredSinglePass` 对照
4. `FMobileBasePassMeshProcessor::Process` 与 `SelectMeshLightmapPolicy`
5. `MobileBasePassPixelShader.usf` 顶部宏定义 + `#if MOBILE_USE_GBUFFER` 段
6. `MobileDeferredShading.usf` 的 `MobileDirectionalLightPS`
7. `MobileDeferredShadingPass::MobileDeferredShadingPass` 的整体调度
8. `SetMobileBasePassDepthState` + `MobileDeferredShadingPass.cpp` 的 Stencil 配置
9. （项目）`RenderCharacterForward` 与 `CharacterForwardShading` 改造

---

## N. 一句话总结

> **Forward** 把材质、光照、反射、阴影"压缩"进单个 BasePass PS 一次性算完；
> **Deferred** 把它"展开"成 BasePass(GBuffer) → Decal → Lighting → Translucency 多个子 Pass。
>
> 两条路径在 **CPU 端的 MeshDrawCommand、LightGrid、Shadow 准备阶段几乎共享**；分叉发生在 **`RenderForward()` / `RenderDeferred()` 之后**。
>
> Forward 让带宽和 RP 简单，但 Shader Permutation 多、多光源受限；
> Deferred 让光照灵活、Permutation 少，但 GBuffer 占用 Tile 内存，对硬件特性（FBF/PLS/Subpass）有要求。
>
> 本工程在 Deferred 主路径上额外做了"Toon 角色反向 Forward"改造，使得 Deferred 兼容卡通渲染的视觉表现。

---

> **补充篇结束。** 至此 Mobile 双管线差异从「调度链路 / BasePass / Lighting / GBuffer / Stencil / Subpass / Tile / 半透明 / 阴影 / 反射 / 后处理 / 项目补丁」共 12 个维度均已覆盖。
