# UE Mobile Forward vs Deferred —— 深度补充 03：后处理链

> 配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**Mobile PostProcessing 完整链路 / Bloom / DOF / Tonemap / TAA / FXAA / SunShaft / Distortion / EyeAdaptation** 在 Forward / Deferred 下的差异。

---

## 1. AddMobilePostProcessingPasses —— 24 个 EPass

源码：`PostProcess/PostProcessing.cpp:2606-2900+`

```cpp
enum class EPass : uint32 {
    VisualizeLuxGIScene,        // 项目 LuxGI 可视化（仅 Deferred）
    Distortion,
    SunMask,
    BloomSetup,
    DepthOfField,
    Bloom,
    EyeAdaptation,
    SunMerge,
    SeparateTranslucency,       // S1:zikuan Mobile low-res 半透
    TAA,
    Tonemap,
    BloomVisualize,             // S1:zikuan cs bloom 调试
    PostProcessMaterialAfterTonemapping,
    FXAA,
    VisualizeMMHShadowMaps,     // [MMH Mobile] @VanderShen
    HighResolutionScreenshotMask,
    SelectionOutline,           // Editor only
    EditorPrimitive,            // Editor only
    DebugPrimitive,             // !UE_BUILD_SHIPPING
    PrimaryUpscale,
    SecondaryUpscale,
    Visualize,                  // ShaderComplexity
    HMDDistortion,              // VR
    MAX
};
```

> 每个 Pass 通过 `PassSequence.SetEnabled(EPass::X, condition)` 启用，最后 `Finalize()` 计算依赖顺序。本工程在原版基础上**额外加了 LuxGI / MMH / BloomCS / S1 SeparateTranslucency** 等 Pass。

---

## 2. Forward / Deferred 在后处理阶段的关键差异

### 2.1 调度入口位置不同

```cpp
// MobileShadingRenderer.cpp:1701-1755
if (ViewFamily.bResolveScene) {
    if (bRenderToSceneColor && !bTonemapSubpassInline) {
        // 走完整后处理链
        AddMobilePostProcessingPasses(GraphBuilder, Scene, View, ...);
    } else if (bTonemapSubpass) {
        // Forward 路径 + Vulkan + 单 RenderPass 时走这里
        AddMobileCustomResolvePass(GraphBuilder, View, SceneTextures, ViewFamilyTexture);
    }
}
```

| 路径 | 后处理触发条件 | 出 Tile 次数 |
|------|---------------|-------------|
| Forward + bTonemapSubpassInline | CustomResolveSubpass 内 inline | **0 次**（最优） |
| Forward + bTonemapSubpass | CustomResolvePass | 1 次 |
| Forward (HDR) 常规 | AddMobilePostProcessingPasses | 1 次 |
| Deferred 任何模式 | AddMobilePostProcessingPasses | 1+ 次 |

### 2.2 SunMask 决策差异

```cpp
// PostProcessing.cpp:2882
bool bUseDepthTexture = !MobileRequiresSceneDepthAux(View.GetShaderPlatform())
                      || IsMobileDeferredShadingEnabled(View.GetShaderPlatform());
```

- **Forward HDR**：通常没有完整 SceneDepth 出 Tile（节省带宽），SunMask 读 SceneDepthAux
- **Deferred**：SceneDepth 必须出 Tile（LightingPass 需要），SunMask 直接读 SceneDepth

### 2.3 Distortion 差异

```cpp
bool bUseDistortion = IsMobileDistortionActive(View);
```

- **Forward**：Distortion 半透物体在 SubpassDepthRead 中渲染，可直接读 SceneColor
- **Deferred**：Distortion 必须等 Lighting 完成后再处理（半透在 LightingSubpass 后）

### 2.4 SeparateTranslucency（S1 项目特色）

```cpp
bool bUseSeparateTranslucency = IsMobileSeparateTranslucencyActive(View);
PassSequence.SetEnabled(EPass::SeparateTranslucency, bUseSeparateTranslucency);
```

- 半透明渲染到低分辨率 RT，节省带宽
- 仅 **Forward + MultiPass** 模式有效（需要拆 RenderPass）
- Deferred 路径下半透必须在 LightingSubpass 内，无法 SeparateTranslucency

---

## 3. Bloom 计算路径详解

### 3.1 三档实现

| 实现 | CVar | 适用 |
|------|------|------|
| FMobileBloomSetupPS（Pixel） | `r.Mobile.Bloom.CS=0` | 主流，所有平台 |
| FMobileBloomSetupCS（Compute） | `r.Mobile.Bloom.CS=1` | S1:zikuan 项目，性能更优 |
| TilePack 模式 | `r.Mobile.Bloom.TilePack=1` | CS + Tile 优化，最低带宽 |

### 3.2 BloomSetup Permutation 矩阵

```cpp
// PostProcessMobile.cpp:170-175
uint32 Variation = bUseBloom    ? 1 << 0 : 0;
Variation |= bUseSun           ? 1 << 1 : 0;
Variation |= bUseDof           ? 1 << 2 : 0;
Variation |= bUseEyeAdaptation ? 1 << 3 : 0;
```

16 种组合，但通过 `IsValidBloomSetupVariation` 过滤掉低效组合：

```cpp
return !bIsRareCases || CVarMobileSupportBloomSetupRareCases.GetValueOnAnyThread() != 0;
```

> Forward / Deferred 都用同一套 Bloom Pass，但 Deferred 路径下 SceneColor 已经是完整 HDR (LightingPass 后)，Forward 路径下 SceneColor 可能是 Inline-Tonemapped 颜色 → BloomThreshold 行为不同。

### 3.3 CartoonBloom + SimplifiedBloom（项目专属）

```cpp
class FCartoonBloomDim    : SHADER_PERMUTATION_BOOL("CARTOON_BLOOM");   // S1
class FSimplifiedBloomDim : SHADER_PERMUTATION_BOOL("SIMPLIFIED_BLOOM"); // GR
```

- CartoonBloom：阈值化 + 颜色强化，卡通项目专用
- SimplifiedBloom：跳过非线性 Setup，节省指令

### 3.4 BloomDownsampleForLocalExposure

```cpp
// PostProcessing.cpp:2807
FScreenPassTextureSlice BloomDownsampleForLocalExposure;
// 给 Tonemap 时的 simplified LocalExposure 用
```

> 本工程的 LocalExposure 不再 inline 到 LightingPass，而是在 Tonemap 阶段用 Bloom Downsample 数据近似。

---

## 4. EyeAdaptation 两套机制

### 4.1 Basic vs Histogram

```cpp
bool bUseBasicEyeAdaptation     = bUseEyeAdaptation && (AutoExposureMethod == AEM_Basic);
bool bUseHistogramEyeAdaptation = bUseEyeAdaptation && (AutoExposureMethod == AEM_Histogram)
                                && (AutoExposureMinBrightness < AutoExposureMaxBrightness);
```

- **AEM_Basic**：从 BloomSetup Downsample 取平均亮度，1 个 float
- **AEM_Histogram**：Compute Shader 生成 64-bin Histogram，从中提取百分位
- **AEM_Manual**：固定曝光，无 Pass

### 4.2 双路径区别

| 路径 | Local Exposure | Eye Adaptation Buffer |
|------|---------------|----------------------|
| Forward | 仅 Tonemap 阶段 | Inputs.SceneTextures 通用 |
| Deferred + `r.Mobile.LocalExposure=1` | LightingPass inline | LightingPS Permutation 控制 |
| Deferred + `r.Mobile.LocalExposure=2` | Tonemap 阶段（默认） | 同 Forward |

---

## 5. TAA 移动端的限制

```cpp
bool bUseTAA = View.AntiAliasingMethod == AAM_TemporalAA;
ensure(View.AntiAliasingMethod != AAM_TSR);  // 移动端不支持 TSR
```

| 维度 | Forward | Deferred |
|------|---------|----------|
| TAA | ✅ | ✅ |
| TSR | ❌ | ❌ |
| FXAA | ✅ | ✅ |
| MSAA | ✅ | ❌ |
| 历史帧需求 | 上一帧 SceneColor | 上一帧 SceneColor + Depth |
| 移动 Vector 计算 | RenderVelocities Opaque + Translucent | 同 + GBuffer 辅助 |
| Forward inline TAA | ❌ | ❌ |

### TAA Velocity 来源

```cpp
// MobileShadingRenderer.cpp:1653-1664
if (bShouldRenderVelocities) {
    EDepthDrawingMode EarlyZPassMode = Scene ? Scene->EarlyZPassMode : DDM_None;
    if (EarlyZPassMode != DDM_AllOpaqueNoVelocity) {
        RenderVelocities(GraphBuilder, Views, SceneTextures, EVelocityPass::Opaque, false);
    }
    RenderVelocities(GraphBuilder, Views, SceneTextures, EVelocityPass::Translucent, false);
}
```

> Velocity Buffer 在双路径下都是独立 Pass。

---

## 6. Tonemap Pass 移动端实现

### 6.1 启用条件

```cpp
bool bIsOutputTexsRGB = EnumHasAnyFlags(Inputs.ViewFamilyTexture->Desc.Flags, TexCreate_SRGB);
bool bUseToneMapper  = !View.Family->EngineShowFlags.ShaderComplexity
                    && (IsMobileHDR() || (IsMobileColorsRGB() && !bIsOutputTexsRGB));
```

> LDR Mobile 模式：硬件 sRGB 自动转换 → 不需要 Tonemap
> HDR Mobile 模式：必须软件 Tonemap

### 6.2 Inline Tonemap（Forward 专属）

```cpp
// MobileShadingRenderer.cpp:367
bTonemapSubpass        = IsMobileTonemapSubpassEnabled(ShaderPlatform, ...)
                       && ViewFamily.bResolveScene && ...;
bTonemapSubpassInline  = IsMobileTonemapSubpassEnabledInline(ShaderPlatform, ...) && bTonemapSubpass;
```

- **Inline**：Vulkan 单 RenderPass 末尾子 Pass，直接 Tile-in 完成 Tonemap，**省一次 SceneColor 写出**
- **CustomResolve**：单独 ScreenSpace Pass，但通过 SubpassHint 仍可保留 Tile-in

### 6.3 LUT 准备

```cpp
// MobileShadingRenderer.cpp:1942
if (bTonemapSubpassInline) {
    PassParameters->ColorGradingLUT = AddCombineLUTPass(GraphBuilder, *ViewContext.ViewInfo);
}
```

> 在 BasePass 启动前生成 ColorGradingLUT，让 Tonemap Subpass 能 sample。

---

## 7. Distortion Pass 双路径差异

### 7.1 Mobile Distortion 工作原理

- Distortion Material 写 UV 偏移到 DistortionAccumulate RT
- Distortion Merge 阶段用 UV 偏移采样 SceneColor，混合回去

```cpp
FMobileDistortionAccumulateOutputs DistortionAccumulateOutputs =
    AddMobileDistortionAccumulatePass(GraphBuilder, Scene, View, DistortionAccumulateInputs);

FMobileDistortionMergeInputs DistortionMergeInputs;
DistortionMergeInputs.SceneColor = SceneColor;
DistortionMergeInputs.DistortionAccumulate = DistortionAccumulateOutputs.DistortionAccumulate;
SceneColor = AddMobileDistortionMergePass(GraphBuilder, View, DistortionMergeInputs);
```

### 7.2 调度差异

| 路径 | Distortion 调度位置 |
|------|-------------------|
| Forward Single | Subpass1 后，在 Translucency 之前 |
| Forward Multi  | 第二个 RenderPass 内 |
| Deferred Single | Lighting Subpass 之后，所有 Translucency 之后（因为 Distortion 通常是半透 Mesh） |

### 7.3 Distortion 与 KeepDepthContent

```cpp
const bool bPostProcessUsesSceneDepth = PostProcessUsesSceneDepth(Views[0]) || IsMobileDistortionActive(Views[0]);
// 触发 bKeepDepthContent
```

> Mobile Distortion 主动触发 SceneDepth 保留，**对 Forward 路径是带宽负担**。建议项目避免大面积 Distortion 材质。

---

## 8. SunShaft / LightShaft 移动端实现

### 8.1 双 Pass 结构

```cpp
// PostProcessing.cpp:2884-2906
FMobileSunMaskInputs SunMaskInputs;
SunMaskInputs.bUseDepthTexture = bUseDepthTexture;  // Forward Aux / Deferred Depth
SunMaskInputs.bUseDof = bUseDof;
SunMaskInputs.bUseMetalMSAAHDRDecode = bMetalMSAAHDRDecode;  // iOS MSAA HDR 解码
SunMaskInputs.bUseSun = bUseSun;

FMobileSunMaskOutputs SunMaskOutputs = AddMobileSunMaskPass(GraphBuilder, View, SunMaskInputs);
PostProcessSunShaftAndDof = SunMaskOutputs.SunMask;
```

后续：
1. SunBlur Pass：对 SunMask 做 RadialBlur
2. SunMerge Pass：把 Bloom + SunShaft 合并

### 8.2 iOS MSAA HDR 解码

```cpp
bool bMetalMSAAHDRDecode = GSupportsShaderFramebufferFetch
                        && IsMetalMobilePlatform(View.GetShaderPlatform())
                        && GetDefaultMSAACount(ERHIFeatureLevel::ES3_1) > 1;
```

> iOS Metal MSAA HDR 必须在 SunMask 阶段 decode；Deferred 不支持 MSAA → 不走该路径。

---

## 9. DOF（景深）移动端

### 9.1 三档

| 模式 | 条件 | 性能 |
|------|------|------|
| Disabled | `GetMobileDepthOfFieldScale(View) == 0` | – |
| Mobile DOF（默认） | `bMobileHQGaussian = false` | 低开销，CoC + 双向 Blur |
| HQ Gaussian | `bMobileHQGaussian = true` | 高质量，多次 Gaussian |

```cpp
bool bUseDof       = GetMobileDepthOfFieldScale(View) > 0.0f
                  && View.Family->EngineShowFlags.DepthOfField
                  && !View.Family->EngineShowFlags.VisualizeDOF;
bool bUseMobileDof = bUseDof && !View.FinalPostProcessSettings.bMobileHQGaussian;
```

### 9.2 与 SunMask 复用 PostProcessSunShaftAndDof

> SunShaft 与 DOF 共用同一 RT `PostProcessSunShaftAndDof`（R = SunMask, G = CoC），节省一张全屏 buffer。

---

## 10. FXAA 移动端

```cpp
PassSequence.SetEnabled(EPass::FXAA, View.AntiAliasingMethod == AAM_FXAA);
```

- 在 Tonemap 之后 / 之前都可，UE 移动端通常放在 Tonemap 之后
- 与 TAA / MSAA 互斥
- Deferred 路径几乎只能用 FXAA 或 TAA

---

## 11. PostProcessMaterial 注入点

```cpp
// PostProcessing.cpp:2771
auto AddPostProcessMaterialPass = [...](EBlendableLocation BlendableLocation, bool bLastPass) {...};
```

| BlendableLocation | 何时执行 |
|------------------|---------|
| `BL_BeforeTranslucency` | BasePass 后，半透前（Forward 在 Subpass 内难做） |
| `BL_BeforeTonemapping` | BasePass 全完成，Tonemap 前 |
| `BL_ReplacingTonemapper` | 替代 Tonemap |
| `BL_SceneColorAfterTonemapping` | Tonemap 后 |
| `BL_AfterTonemapping` (deprecated) | 同上 |

| 路径 | BL_BeforeTranslucency 可用性 |
|------|----------------------------|
| Forward Single | ❌ 在 Subpass 中无法插 PostProcessMaterial |
| Forward Multi | ✅ |
| Deferred Single | ⚠ 实验性 |
| Deferred Multi | ✅ |

---

## 12. MMH Shadow Map 可视化（项目）

```cpp
PassSequence.SetEnabled(EPass::VisualizeMMHShadowMaps,
    View.Family->EngineShowFlags.VisualizeMMHShadowMap && MMHShadowMapArray != nullptr);
```

> MMH（Multi-Material Hierarchy）是项目自定义的 Shadow Map 系统，移动端 VR 优化用。`MMHShadowMapProjection.usf` 内做单层 ShadowMap → 多投影。

---

## 13. UpScale 双档

```cpp
bShouldPrimaryUpscale = (View.PrimaryScreenPercentageMethod == EPrimaryScreenPercentageMethod::SpatialUpscale
                          && View.UnscaledViewRect != View.ViewRect)
                     || View.LensDistortionLUT.IsEnabled();
bShouldPrimaryUpscale |= View.Family->GetPrimarySpatialUpscalerInterface() != nullptr;

PassSequence.SetEnabled(EPass::PrimaryUpscale, bShouldPrimaryUpscale);
PassSequence.SetEnabled(EPass::SecondaryUpscale, View.Family->GetSecondarySpatialUpscalerInterface() != nullptr);
```

- **PrimaryUpscale**：通常配合 `r.ScreenPercentage<100` 做 Dynamic Resolution
- **SecondaryUpscale**：插入项目自定义 Upscaler（如 FSR / MetalFX）

> 移动端 Dynamic Resolution 不分管线，但 Forward 路径下因为 SceneColor 已经是最终颜色，Upscale 直接拿来用；Deferred 路径下 LightingPass 还在 100% 分辨率（GBuffer 全分辨率），Upscale 必须在 LightingPass 之后。

---

## 14. ViewFamily 多视口 / SceneCapture 后处理跳过

```cpp
// MobileShadingRenderer.cpp:1733
for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ViewIndex++) {
    if (Views[ViewIndex].ShouldRenderView()) {
        RDG_EVENT_SCOPE_CONDITIONAL(GraphBuilder, Views.Num() > 1, "View%d", ViewIndex);
        if (bTonemapSubpass) {
            AddMobileCustomResolvePass(GraphBuilder, Views[ViewIndex], SceneTextures, ViewFamilyTexture);
        } else {
            AddMobilePostProcessingPasses(GraphBuilder, Scene, Views[ViewIndex], ViewIndex, ...);
        }
    }
}
```

> SceneCapture 也会走完整后处理（除非通过 ShowFlag 跳过）。Reflection Capture 跳过。

---

## 15. 后处理对 KeepDepthContent 的反向影响

```cpp
const bool bPostProcessUsesSceneDepth =
    PostProcessUsesSceneDepth(Views[0]) || IsMobileDistortionActive(Views[0]);

// MobileShadingRenderer.cpp:721
bKeepDepthContent =
    bRequiresMultiPass ||
    ...
    (bDeferredShading && bPostProcessUsesSceneDepth) ||  // ← 双路径都受影响
    ...
```

`PostProcessUsesSceneDepth` 检查：
- DOF
- SunShaft（使用 DepthAux 时不算）
- TAA（用 Depth 重投影）
- TemporalUpscale
- Distortion
- 自定义 PostProcessMaterial 引用了 SceneDepth

> 项目里**只要后处理用到 Depth，Deferred 强制保留深度内容**。Forward 路径下因为 DepthAux 已经独立，影响较小。

---

## 16. PostProcessing CVar 速查（双路径通用）

| CVar | 默认 | 说明 |
|------|------|------|
| `r.Mobile.Bloom.CS` | 0 | 启用 Compute Shader Bloom |
| `r.Mobile.Bloom.CSThreshold` | 0.8 | CS Bloom 阈值 |
| `r.Mobile.Bloom.TilePack` | 0 | Tile 打包优化 |
| `r.Mobile.Bloom.CSVisualize` | 0 | 调试 |
| `r.MobileHDR` | 1 | HDR 渲染 |
| `r.Mobile.UseHWsRGBEncoding` | 0 | 用硬件 sRGB 而非软件 Tonemap |
| `r.MobileTonemapSubpass` | 1 | 启用 Tonemap Subpass（Forward） |
| `r.Mobile.HQGaussian.Method` | 0 | HQ DOF 方法 |
| `r.MobileBloomQuality` | – | Bloom 质量档 |
| `r.Mobile.EnablePostProcessing` | 1 | 总开关 |
| `r.AutoExposureMethod` | 1 | 0=Manual,1=Basic,2=Histogram |
| `r.Mobile.SupportBloomSetupRareCases` | 0 | 编译稀有 BloomSetup Permutation |
| `r.Mobile.SeparateTranslucency.Method` | – | S1 SeparateTranslucency |
| `r.Mobile.PropagateAlpha` | 0 | 半透 Alpha 传播 |
| `r.AntiAliasingMethod` | – | TAA/FXAA 全局 |
| `r.MobileMSAA` | 1 | MSAA 档（仅 Forward） |
| `r.Mobile.LocalExposure` | 2 | LocalExposure 位置 |

---

## 17. 易错点

| 现象 | 原因 | 排查 |
|------|------|------|
| Deferred 下 DOF 颜色漏出 | DOF 半透与 LightingPass 顺序冲突 | 检查 SubpassHint |
| Forward Inline Tonemap 黑屏 | LUT 没生成 | 检查 AddCombineLUTPass |
| Bloom CS 模式崩溃 | 不支持 Compute 平台 | `r.Mobile.Bloom.CS=0` |
| EyeAdaptation Jitter | 帧间数据不稳 | 关闭后用 Manual 测试 |
| TAA 鬼影 | Velocity Buffer 错 | 检查 RenderVelocities Opaque/Translucent |
| Distortion 没效果 | SeparateTranslucency 抢了 | 关 SeparateTranslucency |
| iOS MSAA HDR 颜色偏 | bMetalMSAAHDRDecode 未启用 | 检查平台条件 |
| CartoonBloom 不显示 | bCartoonBloom Permutation 未编译 | 检查 SupportBloomSetupRareCases |
| MMH ShadowMap 可视化失效 | ShowFlag 未开 | `showflag.VisualizeMMHShadowMap 1` |
| Deferred 后处理读不到 Depth | bKeepDepthContent 未触发 | 检查 PostProcessUsesSceneDepth |

---

> 第 03 篇完。下一篇：**TranslucentRendering / SingleLayerWater / Substrate Mobile**。
