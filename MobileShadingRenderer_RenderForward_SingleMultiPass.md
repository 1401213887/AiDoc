# MobileShadingRenderer：RenderForwardMultiPass 与 RenderForwardSinglePass 原理分析

> 版本基线：UE 5.5.4 source fork（分支 `++GR+DevTest`，**Build.version Changelist 1077366**；注：CLAUDE.md 引用的 1011149 与 Build.version 不一致，以 Build.version 为准）
> 源文件：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp`（下文简称 `MobileShadingRenderer.cpp`）
> 分析日期：2026-08-15（含 `/loop 10min` 源码深读 + **多轮交叉校验**的整合；第 10 节为阅读范围与遗留清单）

---

## 目录

- §0 一页纸结论
- §1 入口与选路
- §2 关键标志的判定
- §3 RenderForward 主流程
- §4 RenderForwardSinglePass
- §5 RenderForwardMultiPass
- §6 子回调逐一详解
- §7 对比分析
- §8 设计意图与性能考量
- §9 本仓库定制（ZXB / GR / S1）
- §10 阅读范围与遗留待办

---

## 0. 一页纸结论

- **选路**：`FMobileSceneRenderer::Render()` 中，非 Deferred 时进入 `RenderForward()`；`RenderForward()` 内依据 `bRequiresMultiPass` 决定走 `RenderForwardMultiPass()` 还是 `RenderForwardSinglePass()`。
- **本质差异**：SinglePass 用**一个 Render Pass + 多个 Subpass**（`RHICmdList.NextSubpass()`）把 BasePass / Decals / Fog / Translucency / 内联 Tonemap 串联，依赖 **framebuffer fetch / subpass 依赖**在片内取回 SceneColor/Depth；MultiPass 把 Decals/Fog/Translucency 拆到**第二个独立 Render Pass**，依赖 **resolve 后的纹理**重新绑定采样。
- **触发条件**：`bRequiresMultiPass` 基本等价于「**无 Vulkan、无 framebuffer-fetch（Metal/GL）、HDR、非 MSAA、非模拟平台** 的移动端」。Vulkan、支持 FBF 的 Metal/GL、LDR、MSAA>1 一律走 SinglePass。
- **性能语义**：SinglePass 是 TBDR 片内最优路径（MSAA 可 memoryless、深度不落盘）；MultiPass 是为老 GL 平台保留的降级路径（两次 RT 绑定 + 显式 resolve + 深度保留）。
- **仓库定制**：本 fork 在两条路径上有多处 GR/ZXB 定制（toon 描边、LuxGI 对齐、角色 forward、深度永不 Memoryless 等），详见 §9。

---

## 1. 入口与选路

### 1.1 `Render()` 内的 Forward/Deferred 分流（MobileShadingRenderer.cpp:1653-1685）

`bDeferredShading = IsMobileDeferredShadingEnabled(ShaderPlatform)`（构造 363）；`bRequiresDBufferDecals = bDeferredShading ? false : IsUsingDBuffers(...)`（364，Deferred 恒无 DBuffer）。

```
if (bRendererOutputFinalSceneColor)
{
    if (bDeferredShading)
    {
        bRequiresMultiPass ? RenderDeferredMultiPass(...)
                           : RenderDeferredSinglePass(...);
    }
    else
    {
        RenderForward(GraphBuilder, ViewFamilyTexture, SceneTextures, DBufferTextures);   // ← 本主题
    }
}
```

> 补充（第 1 轮校验）：Deferred 分支分流后还有 **SSXR 的 SceneColor/Depth 提取**（1666-1680，`QueueTextureExtraction` 到 `PrevFrameViewInfo`，供下一帧 SSXR 射线追踪）；Forward 无对应段。

### 1.2 `RenderForward()` 内的 Single/Multi 分流（MobileShadingRenderer.cpp:2068-2075）

```
for (FRenderViewContext& ViewContext : RenderViews)
{
    ... 准备 PassParameters、实例剔除 ...
    if (bRequiresMultiPass)
        RenderForwardMultiPass(GraphBuilder, PassParameters, ViewContext, SceneTextures);
    else
        RenderForwardSinglePass(GraphBuilder, PassParameters, ViewContext, SceneTextures);
}
```

### 1.3 顶层选路流程图

```
                ┌──────────────────┐
                │   FMobileSceneRenderer::Render()   │
                └────────┬─────────┘
                         │ bRendererOutputFinalSceneColor
                         ▼
              ┌──────────┴──────────┐
              │ bDeferredShading ?  │
              └────┬──────────┬─────┘
               是(Y)│          │否(N)
                   ▼          ▼
       RenderDeferredMulti/   RenderForward()
       SinglePass             │
                              │ bRequiresMultiPass ?
                         ┌────┴────┐
                         │        │
                      是(Y)     否(N)
                         ▼        ▼
              RenderForwardMulti  RenderForwardSingle
              Pass()              Pass()
```

---

## 2. 关键标志的判定

这些标志在 `FMobileSceneRenderer` 构造函数与 `InitViews*`/`SetupSceneTexturesConfig` 阶段确定，直接决定 Single/Multi 行为。

### 2.1 `bRequiresMultiPass`（核心开关）

定义：`RequiresMultiPass(NumMSAASamples, ShaderPlatform)`（MobileShadingRenderer.cpp:2923-2962；2026-08-21 复核，此前记 3191 系源码版本漂移——该函数随迭代移动过位置）。函数头注释点明语义：**是否需要把 translucency/decals 等拆到单独 render pass**。

```
bool FMobileSceneRenderer::RequiresMultiPass(int32 NumMSAASamples, EShaderPlatform ShaderPlatform)
│
├─ IsVulkanPlatform ?                                    → return false
│     // Vulkan 用 subpass（含深度/颜色 fetch），无需拆 pass
├─ IsMetalMobilePlatform && GSupportsShaderFramebufferFetch ? → return false
│     // 全部 iOS 支持 framebuffer_fetch
├─ IsAndroidOpenGLESPlatform && (FBF || DepthStencilFetch) ? → return false
│     // 部分 Android GL 支持 fetch
├─ IsMobileDeferredShadingEnabled ?                      → return true
│     // 无 FBF 的 deferred 平台必须 multipass
├─ !IsMobileHDR() && !IsSimulatedPlatform ?              → return false
│     // LDR 永远单 pass
├─ NumMSAASamples > 1 && !IsSimulatedPlatform ?          → return false
│     // MSAA depth 无法采样/解析 → 走 single pass 用 fetch
└─ 默认                                                  → return true
     // 无 FBF 的老 GL、HDR、非 MSAA → 拆双 pass
```

要点：**MSAA>1 反而返回 false（SinglePass）**，这反直觉但正确——MSAA 深度不能跨 pass 采样，只能依赖 subpass/fetch 在同一 pass 内使用。MultiPass 是「非 MSAA + 无 FBF」平台专用。

#### Android Vulkan 预览的 SinglePass/MultiPass 取决于实际 ShaderPlatform（2026-08-21 修正）

- **真·Android Vulkan（真机 / PreviewShaderPlatformName 真正设为 Android Vulkan / `-OverrideSP=VULKAN_ES3_1_ANDROID`）**：ShaderPlatform=`SP_VULKAN_ES3_1_ANDROID` → 决策树第一分支 `IsVulkanPlatform` 短路 → **SinglePass**（与 Preview 档位、HDR/LDR、MSAA 无关）。
- **编辑器 PIE 设备预览（用户实测场景）**：`Previewing Platform 'Android', DeviceProfile 'Android_High'`（日志）只改 DeviceProfile/Scalability（`LaunchEngineLoop.cpp:7219` `ChangeScalabilityPreviewPlatform`），**不碰 `GShaderPlatformForFeatureLevel`**；`EditorPerProjectUserSettings.ini` 的 `PreviewShaderPlatformName=None` + `bPreviewFeatureLevelActive=False` → 保持桌面 D3D12 初始值 **`SP_PCD3D_ES3_1`**（`WindowsD3D12Device.cpp:1149`）。材质编译日志实锤 "platform **PCD3D_ES31**"。决策树在 `SP_PCD3D_ES3_1` 上：非 Vulkan/Metal/GL、非 Deferred、HDR、MSAA=1 全不命中 → **return true = MultiPass** → `RenderForwardMultiPass`（两个 render pass：SceneColorRendering + DecalsAndTranslucency，中间 resolve 深度）。**"Android 预览" ≠ "Android Vulkan shader 平台"——前者只切 DeviceProfile，后者才切 ShaderPlatform。**
- **验证结论**：是否 SinglePass 的唯一判据是**运行时 ShaderPlatform 族**，不能从预览 UI 名称推断。判据 = 材质编译日志的 platform 名（PCD3D_ES31 = 桌面 → MultiPass；VULKAN_ES31_ANDROID = Vulkan → SinglePass）。
- **调用链**：`bRequiresMultiPass = RequiresMultiPass(...)`（:744）→ `bRequiresMultiPass ? RenderForwardMultiPass : RenderForwardSinglePass`（:2058-2061）。
- **呼应**：Vulkan 的 SinglePass 是真 subpass 结构（`RenderForwardSinglePass` 内 `RHICmdList.NextSubpass()`，仅 Vulkan/Metal/OpenGL override，`RHIContext.h:819` 基类空实现、D3D12 不 override → 桌面模拟时 no-op）。
- **为什么 Vulkan 被"强制" SinglePass（2026-08-21 深挖）**：第一分支 `IsVulkanPlatform → return false`（:2925 注释 "Vulkan uses subpasses"）是**架构取舍，不是能力限制**——Vulkan 技术上完全能开两个 render pass 走 MultiPass，是 UE 设计上短路掉这条路径。三个理由：
  1. **subpass 原生能力**：Vulkan render pass 支持多 subpass + subpass dependency（`vkCmdNextSubpass`，VulkanRenderTarget.cpp:707），base pass→decals→fog→translucency→tonemap 内联全串进一个 render pass。
  2. **省带宽（最核心）**：SinglePass 的 SceneColor 留在 on-chip/tile，后续 subpass 直接读，**不 resolve 回内存再读回**；MultiPass 则第一个 pass 必须 resolve 到内存、第二 pass 读回 = **双倍带宽**（移动端最贵资源）。
  3. **MSAA 兼容**：MSAA 深度无法跨 pass 采样（与 :2956 `NumMSAASamples>1 → return false` 同一原因），SinglePass 的 subpass 能拿到 on-chip MSAA 值。
  **决策树整体规律**：所有 `return false` 分支（Vulkan/Metal+FBF/GL+FBF/LDR/MSAA）都是"SinglePass 可行且更优"；`return true`（MultiPass）是**兜底**，只留给"无 subpass 且无 FBF + HDR + 非 MSAA"的老平台（被迫拆 pass）。所以：桌面 PCD3D_ES31 = **被迫 MultiPass**（没得选）；真 Android Vulkan = **不必 MultiPass**（有更优解）。
  **硬让 Vulkan 走 MultiPass 的后果**：① 丢 subpass 带宽优势（SceneColor 双次读写）；② MSAA 场景 decals/translucency 深度采样失效；③ 与 tonemap 内联冲突——`bTonemapSubpassInline` 仅 Vulkan 且**依赖** SinglePass 的 subpass 结构（:2141-2145 `NextSubpass + RenderMobileCustomResolve`）。

### 2.2 Tonemap 内联标志（397-399）

```
bTonemapSubpass     = IsMobileTonemapSubpassEnabled(...) && bResolveScene && 输出为最终色
bTonemapSubpassInline = IsMobileTonemapSubpassEnabledInline(...) && bTonemapSubpass
```

- `IsMobileTonemapSubpassEnabled`（Engine/Private/SceneUtils.cpp:94）：`CVar r.Mobile.TonemapSubpass==1` **或** `bMultiViewRendering`，且 MobileHDR 且非 Deferred。`bResolveScene` 默认 true（SceneView.cpp:3454，scene capture 强制 true）。
- `IsMobileTonemapSubpassEnabledInline`（Engine/Private/SceneUtils.cpp:100）：在基础上 **必须是 Vulkan**，且 `GRHISupportsMSAAShaderResolve || NumMSAASamples<=1`。
- 结论：**内联 tonemap 目前只有 Vulkan 实现**（input attachment 语义）。此时 base pass 把 `ViewFamilyTexture`（backbuffer）当 RT1，在最后一个 subpass 内直接做 tonemap+resolve 写屏。

#### 三种 tonemap 执行形态（默认 / 非内联 / 内联）

```
① 默认（bTonemapSubpass=false）
   [RenderPass A: BasePass → HDR SceneColor 写内存] → [Pass: Bloom] → [Pass: 曝光] → [Pass: Tonemap] → LDR 写屏
   └── 完整后处理链（AddMobilePostProcessingPasses）────────────────────┘

② tonemap-subpass 模式·非内联（bTonemapSubpass=true，bTonemapSubpassInline=false，Metal/GL）
   [RenderPass A: BasePass → SceneColor 写内存] → [独立 Pass: tonemap → LDR 写屏]
   └── 后处理链整个被跳过，tonemap 是独立 full-screen pass ─────────────┘

③ tonemap-subpass 模式·内联（bTonemapSubpassInline=true，Vulkan）
   [RenderPass B: subpass0=BasePass 画几何 → SceneColor(input attachment, on-chip)]
                 ──NextSubpass()──
                 [subpass1=MobileCustomResolve_MainPS 采样 SceneColor + tonemap → backbuffer]
   └── base pass 和 tonemap 同一 Render Pass，一次提交 ──┘
```

- **③ 才是真正的 "subpass"**：tonemap 内嵌进 base pass 所在 Render Pass，`NextSubpass()` 切换（MobileShadingRenderer.cpp:2409-2413）；SceneColor 通过 input attachment 留在 tile 内（:820 `TexCreate_InputAttachmentRead`、:2351 `ESubpassHint::CustomResolveSubpass`）。
- **② 名不副实**：`bTonemapSubpass=true` 但 inline=false 时（Metal/GL），tonemap 是**独立 full-screen pass**（`AddMobileCustomResolvePass`，PostProcessTonemap.cpp:1268），读的是**内存** `SceneColor.Resolve`（:1226-1230 `if (SubpassMSAASamples == 0u) ColorTexture = SceneTextures.Color.Resolve`），**不是 subpass**。shader 双路径证据（PostProcessTonemap.usf:745-767 `FetchAndResolveSceneColor`）：`VULKAN_PROFILE && SUBPASS_MSAA_SAMPLES>0` 走 `VulkanSubpassFetch0()`（input attachment，tile 内）；否则 `Texture2DSample(ColorTexture, ...)`（内存 Resolve）。
- **"内联"= 方式（把 tonemap 塞进场景 pass），"subpass" = 载体（input attachment 传 SceneColor on-chip）**。`bTonemapSubpass` 的名字来自设计意图（最早为 Vulkan subpass 设计），Metal/GL 复用模式名但没实现 subpass，退化为独立 pass——**历史命名，别被名字误导**。

#### 内联/非内联 → 后处理链全跳过（MobileShadingRenderer.cpp:1970-1977）

```cpp
if (bTonemapSubpass)     AddMobileCustomResolvePass(...);    // 只 tonemap
else                     AddMobilePostProcessingPasses(...); // 完整后处理链
```

`AddMobilePostProcessingPasses`（PostProcessing.cpp:2628）的 pass 序列：`BloomSetup → Bloom → EyeAdaptation → Tonemap → PostProcessMaterialAfterTonemapping → FXAA`。`bTonemapSubpass=true`（无论内联与否）时整条链不跑，只保留 tonemap 本身：

| 保留 | 丢失 |
|---|---|
| Tonemap（`MobileCustomResolve_MainPS`） | BloomSetup / Bloom |
| ColorGrading LUT（`AddCombineLUTPass`，PostProcessTonemap.cpp:1270） | EyeAdaptation 自动曝光 |
| SceneColorTint（PostProcessTonemap.cpp:1219） | PostProcessMaterialAfterTonemapping（tonemap 后材质） |
| | FXAA（`r.Mobile.AntiAliasing=1` 时） |

架构根因：内联时 backbuffer 作 base pass 的 RT1（:2228-2234），没有中间 HDR 目标给 bloom/曝光读，也没有 tonemap 后的 LDR 目标给 FXAA/后材质读——物理上没有后处理空间。

#### 为什么只有 Vulkan 实现内联（Metal/GL 有 FBF 能力，是 UE 未实现而非硬件限制）

- **Metal/GL 技术可行**：`MobileBasePassPixelShader.usf:217-227` 三平台 FBF 宏全在——`FramebufferFetchColor0` = `VulkanSubpassFetch0`（Vulkan）/ `SubpassFetchRGBA_0`（Metal）/ `FramebufferFetchES2`（GLES），现仅用于半透明可编程混合（:1669 `OutProgrammableBlending = OutColor1 * FramebufferFetchColor0() + OutColor`）。
- **但 `MobileCustomResolve_MainPS` 只写了 Vulkan 分支**（PostProcessTonemap.usf:748 `#if VULKAN_PROFILE && SUBPASS_MSAA_SAMPLES > 0`），无 Metal/GL 的 FBF 分支——不是不能做，是没写。
- UE 注释 `// As of UE 5.4 only vulkan supports inline (single pass) tonemap`（MobileShadingRenderer.cpp:438）是**现状措辞非硬件限制**。工程原因：Vulkan input attachment 语义最干净；Metal/GL 的 FBF 在 MSAA 取值语义/多视图/GL 驱动兼容上有额外复杂度；UE 优先实现 Vulkan，Metal/GL 用 `bTonemapSubpass` 独立 pass 兜底（功能可用，只是不省带宽）。

#### 开启方法（调试用）

- CVar：`r.Mobile.TonemapSubpass 1`（`ECVF_Scalability` 非 cheat，MobileBasePassRendering.cpp:184-190；控制台 / `-dpcvars` 均可）。
- 条件链：① CVar=1 **或** multi-view（后者强制）；② `IsMobileHDR()`（`r.MobileHDR` 默认 1——设 0 反而禁掉，LDR 无 HDR 可 tonemap）；③ 非 Deferred（`IsMobileDeferredShadingEnabled = MobileDeferredShading && IsMobileHDR`，RenderCore/Public/RenderUtils.h:322-325）；④ 内联还须 **Vulkan** + `GRHISupportsMSAAShaderResolve || NumMSAASamples<=1`（SceneUtils.cpp:100-104）。
- 验证：画面 bloom/曝光/FXAA 立即消失；`r.RDG.Debug=1` 事件树出现 `MobileCustomResolvePass` 且无 `PostProcessing`。
- ⚠️ 调试注意：若 Forward/Deferred 对比基准是在 `r.Mobile.TonemapSubpass=0`（默认）下采的，开 1 后 post-tonemap 探针对比会变（后处理链没了），可作「2× 差异是否来自后处理链」的对照实验，测完关回 0。

### 2.3 深度与后处理相关标志

| 标志 | 取值（行号） | 含义 |
|---|---|---|
| `bIsFullDepthPrepassEnabled` | 376 | `EarlyZPassMode==DDM_AllOpaque(NoVelocity)`：不透明体先整体写深度（**运行时配置**） |
| `MobileUsesFullDepthPrepass`（platform 级） | RenderCore/Private/RenderUtils.cpp:770-774 | `MobileUsesShadowMaskTextureRuntime \|\| IsUsingDBuffers \|\| FReadOnlyCVARCache::MobileEarlyZPass==1`（easonjiang 把 `IsMobileAmbientOcclusionEnabled` 换为 shadow-mask 判定，"Use Last Frame's Mobile AO Texture"）；影响 shader 编译 & `MobileRequiresSceneDepthAux` 前置（full prepass 不建 DepthAux）。与上行 `bIsFullDepthPrepassEnabled` 互补：前者 platform 级、后者帧配置 |
| `bIsMaskedOnlyDepthPrepassEnabled` | 377 | `EarlyZPassMode==DDM_MaskedOnly`：仅 Masked 走预深度 |
| `bRequiresSceneDepthAux` | 400 | `MobileRequiresSceneDepthAux(Platform) && !bTonemapSubpass`。`MobileRequiresSceneDepthAux`（RenderCore/Private/RenderUtils.cpp:493-511）判定树：**`!MobileUsesFullDepthPrepass` 才考虑**（full prepass 不建 DepthAux）→ **Metal 恒 true**（iOS 采深度用，非"无 FBF"语义）→ **HDR && 非 Deferred 的 Android GL/Vulkan** → true；否则 false |
| `bKeepDepthContent` | 746-773 | **`bRequiresMultiPass` 时强制 true** → 深度不能 discard，供第二 pass 读取；**761 行有反向例外**：`(NumMSAASamples>1) && bRequiresSceneDepthAux` 时强制 false（"never keep MSAA depth if SceneDepthAux is enabled"）；764-767 editor prims、770-773 SIM 平台也强制 true |
| `bModulatedShadowsInUse` | ShadowSetupMobile.cpp:352-359 | 遍历灯光，**任一灯 `ShadowsToProject.Num()>0` 即 true**（"Mobile renderer only projects modulated shadows"）；仅 MultiPass 消费（见 §5.1） |
| `bPostProcessUsesSceneDepth` | 739（判定 298-326） | `PostProcessUsesSceneDepth`：DOF、MobileLightShaft、或任一后处理材质 `UsesSceneTexture(PPI_SceneDepth)` → 间接影响 `bKeepDepthContent` |

`bKeepDepthContent` 连锁影响（776-777）：
```
SceneTexturesConfig.bKeepDepthContent = bKeepDepthContent;
SceneTexturesConfig.bMemorylessMSAA = !(bRequiresMultiPass || bShouldCompositeEditorPrimitives || bRequireSeparateViewPass);
```
即 **MultiPass 时 MSAA 目标不允许 memoryless**（第二 pass 要回读 resolve 结果）；SinglePass 时 MSAA 目标可 memoryless（on-chip）。

### 2.4 RT 创建标志与 SceneColor 格式（SceneTexturesConfig.cpp / SceneUtils.cpp，均位于 `Engine/Source/Runtime/Engine/Private/`）

**SceneColor 格式选择**（`GetMobileSceneColorFormat`，199-240）：

| 条件 | 格式 |
|---|---|
| 非 HDR 或平台不支持 FloatRGBA RT | `GetDefaultMobileSceneColorLowPrecisionFormat()`（177-197）：XR 时跟随交换链 `GetActualColorSwapchainFormat`；standalone stereo-only → `R8G8B8A8`；默认 **`B8G8R8A8`** |
| HDR + 需要 alpha | `PF_FloatRGBA` |
| HDR + 不需要 alpha | **默认 `PF_FloatR11G11B10`**（209） |

`r.Mobile.SceneColorFormat` CVar 覆盖：`1=FloatRGBA`、`2=R11G11B10`、`3=低精度`。对比 Deferred/PC `GetSceneColorFormat`（242-282，默认 FloatRGBA、CVar 0-5、alpha 强制 FloatRGBA）。→ **Forward 移动端默认 SceneColor 是 R11G11B10**（省约一半颜色带宽）；LDR 为 B8G8R8A8。这也是 §3.1 `bRenderToSceneColor` 判定里 `RenderTargetPixelFormat != ColorFormat` 的语义来源。

**SceneColor 创建标志**（`BuildSceneColorAndDepthFlags` → `GetSceneColorFormatAndCreateFlags`，284-318）：
```
SceneColorCreateFlags = RenderTargetable | ShaderResource | ExtraSceneColorCreateFlags(779行)
  其中 ExtraSceneColorCreateFlags = bTonemapSubpassInline ? TexCreate_InputAttachmentRead : None
MSAA && bMemorylessMSAA → TexCreate_Memoryless
```
即 **内联 tonemap 时 SceneColor 是 InputAttachment（subpass 读）**；MSAA+SinglePass 时可 memoryless。

**Depth 创建标志**（`GetSceneDepthStencilCreateFlags`，320-333，`//ericado` 标注）：
```
DepthCreateFlags = DepthStencilTargetable | ShaderResource | InputAttachmentRead | ExtraSceneDepthCreateFlags
// Memoryless 分支被整体注释：//DepthCreateFlags |= TexCreate_Memoryless;
```
→ 深度目标**始终**是 InputAttachment（支持 subpass depth fetch）；**该仓库移动端深度永不 Memoryless**，深度内容始终保留可读——与 §9 的 SceneDepth 采样防御（§6.15）相互呼应。

**来源判定（2026-08-16，P4 验证 + 官方比对）**：`//ericado` **不是 UE 官方代码**——官方 `GetSceneDepthStencilCreateFlags` 原版在有条件下（`!bKeepDepthContent \|\| (MSAA && 非 full prepass)`）会执行 `DepthCreateFlags |= TexCreate_Memoryless;`（Epic 官方文档 `bMemorylessMSAA`="MSAA targets can be memoryless"、`bKeepDepthContent`="write depth content back to memory"，确认官方机制**允许**深度 memoryless）。本 fork 的 if 结构原样保留、仅赋值被注释，且签名行署名 `//ericado`（网易工程师名，UE 官方从不个人署名）——是 **GR fork 定制**。P4 佐证：该文件无本机未提交改动（`p4 opened` not opened）；filelog #1-4 全部 `[BranchCopy]`（#4 change 888829，2026/04/07，`Copy from //GR/Mer`），确认改动随 GR 分支链（`//GR/Mer` → `//GR/trunk` → DevTest）带入，非 DevTest 分支逐行提交。

> ⚠️ **后续开发点**：若未来要恢复移动端深度 memoryless 省带宽（深度目标画完即弃、省带宽预算），需在这里恢复 `DepthCreateFlags |= TexCreate_Memoryless;` 并**同步处理**：① 所有深度消费方（toon outline 链路 PreOutline/MobileToonOutline 的深度 Laplacian、HZB、材质 SceneDepth 节点、SSXR/shadow）改为采 `Depth.Resolve` 或确认无需目标内容；② §6.15 的 DepthDummy 兜底会接管"深度不可读"的路径。当前禁用的直接原因是保证上述 SceneDepth 采样路径正确（§6.15）。

**PSO binding cache 注意**（476-480）：`SetupMobileGBufferFlags(bRequiresMultiPass=false)` 仅是 PSO 无关占位（478 注释明示 "Does not affect PSO state"）；运行时真正的 `SetupMobileGBufferFlags(bRequiresMultiPass||IsDumpingFrame||bRequireSeparateViewPass)` 在 MobileShadingRenderer.cpp:783 调用。

### 2.5 LDR vs HDR 对 SinglePass 的影响（`r.MobileHDR`，默认 1）

决策树里 LDR 走 SinglePass（§2.1 决策树 L2950-2953：`!IsMobileHDR() && !IsSimulatedPlatform → return false`）。但 LDR 不只是"绕开 MultiPass 的开关"——它把 SinglePass 整体降级成**直通管线**，7 个连锁效应：

| # | 维度 | HDR（默认） | LDR（`r.MobileHDR=0`） | 证据 |
|---|---|---|---|---|
| 1 | SceneColor 格式 | R11G11B10 / FloatRGBA | **B8G8R8A8** | SceneTexturesConfig.cpp:202 `bUseLowPrecisionFormat = !IsMobileHDR() \|\| ...` |
| 2 | 颜色空间 | 线性 | **gamma**（`bGammaSpace`） | MobileShadingRenderer.cpp:403 `bGammaSpace(!IsMobileHDR())` |
| 3 | tonemap subpass | 可能开启 | **恒关** | SceneUtils.cpp:97 `IsMobileHDR()` 是硬性前提 |
| 4 | 后处理链 | 有 | **整段跳过** | MobileShadingRenderer.cpp:703 `bRenderToSceneColor = !bGammaSpace \|\| ...` → LDR 为 false → :1935 不进 `AddMobilePostProcessingPasses` |
| 5 | SceneDepthAux | Android GL/Vulkan 有 | **无** | RenderCore/Private/RenderUtils.cpp:504 需 `bMobileHDR` |
| 6 | 颜色编码 | shader 内 | 可选**硬件 sRGB** | SceneUtils.cpp:111 `IsMobileColorsRGB = !IsMobileHDR() && r.Mobile.UseHWsRGBEncoding` |
| 7 | Deferred | 可用（→MultiPass） | **不可用** | RenderCore/Public/RenderUtils.h:324 `IsMobileDeferredShadingEnabled = MobileDeferredShading && IsMobileHDR()` |

关键语义：

- **LDR 下 `bRenderToSceneColor=false`**（无 scene capture/stereo/MSAA/full prepass 特殊场景时）→ `AddMobilePostProcessingPasses` 整个不跑（:1935）→ 无 tonemap/自动曝光/post-tonemap。L2388 注释点破：*"Draw debug primitives after translucency for LDR as we do not have a post processing pass"*。
- **为什么 LDR 反而禁掉 tonemap subpass**：tonemap 的前提是"有 HDR 数据要映射到显示空间"；LDR 的 SceneColor 本身就是 B8G8R8A8 gamma 显示值，再 tonemap 等于**二次变换**（无意义且引入错误）。与 Deferred 被 `!IsMobileDeferredShadingEnabled` 挡住同理——都是架构上装不下"base pass 最后 subpass 直接 tonemap"。LDR 与 tonemap subpass 是**互斥的两条省带宽路线**：LDR = 降精度省带宽（无 tonemap 操作）；tonemap subpass = 保精度省带宽（tonemap 内联）。
- **模拟平台例外**：决策树 `!IsMobileHDR() && !IsSimulatedPlatform` 才走 SinglePass——**PC Preview 模拟平台下 LDR 反而落到 return true（MultiPass）**。
- **调试旁证**：开 `r.MobileHDR=0` 后 post-tonemap 的 2× 亮度差异（§10 待办 #6，见记忆 `fwd-vs-def-post-tonemap-2x-unpinned`）会**直接消失**（整条后处理链没了）——与 `r.Mobile.TonemapSubpass=1` 同理（§2.2 内联小节），两者都可作"差异确实在后处理链"的对照实验。

---

## 3. RenderForward 主流程（1987-2083）

```
RenderForward()
│
├─ GVRSImageManager.PrepareImageBasedVRS()            // VRS 图准备
├─ NewShadingRateTarget = GetVariableRateShadingImage(BasePass)
├─ BasePassRenderTargets = InitRenderTargetBindings_Forward(ViewFamilyTexture, SceneTextures)
│    └─ ShadingRateTexture / MultiViewCount 附加设置
├─ GetRenderViews(Views, RenderViews)
│
└─ for each ViewContext :
   ├─ [非首 view] RT0/RT1/Depth/Stencil 全部改 ELoad   // 多 view 共享 RT
   ├─ View.BeginRenderView()
   ├─ UpdateDirectionalLightUniformBuffers()           // 方向光 UB（3 通道，CachedView 去重）
   ├─ UpdateToonShadingUniformBuffers()                // [GR] toon UB（见 §3.4）
   ├─ [ZXB] UpdateLuxGIUniformBuffers()                // ← 对齐 Deferred 的修复（见 §9）
   ├─ MobileBasePassTextures = { DBufferTextures,
   │       [ZXB] ScreenSpaceOutline = SceneTextures.MobileCharFeatureTexture.Resolve }
   ├─ SetupMode = (full prepass ? SceneDepth : None) | CustomDepth
   ├─ PassParameters = 分配 FMobileRenderPassParameters（见 §3.2）
   │    ├─ View / MobileBasePassUB(EMobileBasePass::Opaque) / ReflectionCaptureUB
   │    ├─ RenderTargets = BasePassRenderTargets
   │    └─ LocalFogVolume ×5 个 SRV/Buffer + HalfResLocalFogVolume ×2
   ├─ BuildInstanceCullingDrawParams()                 // GPU Scene 实例剔除（见 §3.3）
   │
   └─ bRequiresMultiPass ? RenderForwardMultiPass() : RenderForwardSinglePass()
```

### 3.1 InitRenderTargetBindings_Forward（1931-1985；RT 绑定构建 1963-1984 + bRenderToSceneColor 判定 662-671）

```
RT[0] = SceneColor(SceneColorResolve)          , LoadAction=EClear
RT[1] = SceneTextures.DepthAux.Target(+Resolve), LoadAction=EClear   // 仅 bRequiresSceneDepthAux
Depth = SceneDepth(SceneDepthResolve)   // resolve 仅当 支持深度resolve && MSAA>1 && Depth.IsSeparate()（1961）
Depth = bIsFullDepthPrepassEnabled
        ? (ELoad, ELoad,  DepthRead_StencilWrite)   // 深度已由 prepass 写好
        : (EClear, EClear, DepthWrite_StencilWrite)

若 bTonemapSubpassInline：                            // Vulkan 内联 tonemap
   RT[0].SetResolveTexture(nullptr)                  // 不再 resolve 到独立纹理
   RT[1] = ViewFamilyTexture（backbuffer）           // 内联 tonemap 直接写屏
```

**`bRenderToSceneColor` 分支（关键）**：
```
bRenderToSceneColor = !bGammaSpace                      // HDR
    || bStereoRenderingAndHMD                            // 立体渲染+HMD 扭曲
    || bRequiresUpscale                                  // 需要上采样
    || bShouldCompositeEditorPrimitives                  // 编辑器图元合成
    || Views[0].bIsSceneCapture || Views[0].bIsReflectionCapture
    || (MSAA>1 && !RHISupportsSeparateMSAAAndResolveTextures)
    || (MSAA>1 && RenderTargetPixelFormat 不匹配 ColorFormat)
    || bIsFullDepthPrepassEnabled;
```
- **`bRenderToSceneColor=false`（典型 LDR gamma space）时**：非 MSAA → RT0 直接 = `ViewFamilyTexture`（backbuffer，1952）；MSAA → SceneColor=`Color.Target`、resolve 指向 backbuffer（1947-1948）→ 解释了 SinglePass 内 "LDR 无后处理 pass，translucency 后直接画 debug primitives"（2130-2133）。
- **GLES 不允许 MSAA color target**：MSAA 靠 framebuffer 魔法实现——renderpass 以 MSAA 执行、结束自动 resolve 进非 MSAA 纹理（1938-1940 注释）。

### 3.2 FMobileRenderPassParameters 结构体（283-296）

`SHADER_PARAMETER_STRUCT` 声明成员：

| 成员 | 类型/访问 | 用途 |
|---|---|---|
| `View` | FViewShaderParameters | 视图参数 |
| `InstanceCullingDrawParams` | FInstanceCullingDrawParams | GPU 实例剔除参数，随结构下发 |
| `MobileBasePass` | FMobileBasePassUniformParameters UB | BasePass 统一缓冲（含 SceneTextures） |
| `ReflectionCapture` | FMobileReflectionCaptureShaderData | 反射捕获 |
| `LocalFogVolumeInstances` | Buffer\<float4\> SRV | 局部雾体积实例数组 |
| `LocalFogVolumeTileDrawIndirectBuffer` | IndirectArgs 访问 | tile 绘制间接参数 |
| `LocalFogVolumeTileDataTexture` | Texture2DArray\<uint\> | 每 tile 的雾体索引 |
| `LocalFogVolumeTileDataBuffer` | Buffer\<uint\> SRV | tile 数据缓冲 |
| `HalfResLocalFogVolumeViewSRV` / `DepthSRV` | Texture SRV | 半分辨率局部雾体积视图/深度 |
| `ColorGradingLUT` | RDG_TEXTURE_ACCESS SRVGraphics | 内联 tonemap 用（见 §6.11） |
| `RENDER_TARGET_BINDING_SLOTS()` | - | RT 绑定槽 |

→ 印证 RenderForward 里那批参数是**移动端局部雾体积（Local Fog Volume）tile 化数据**：按 tile 记录雾体索引 + 半分辨率视图，供 base pass shader 采样。

### 3.3 BuildInstanceCullingDrawParams（1912-1929）

RenderForward 每个 view 前**一次性**构建 8 个 mesh pass 的间接绘制参数：

```
DepthPass（仅 !full prepass）→ DepthPassInstanceCullingDrawParams
BasePass                    → PassParameters->InstanceCullingDrawParams  ← 直接写进 PassParameters
SkyPass                     → SkyPassInstanceCullingDrawParams
StandardTranslucencyMeshPass→ TranslucencyInstanceCullingDrawParams
DebugViewMode               → DebugViewModeInstanceCullingDrawParams
MeshDecal_SceneColor / _SceneColorAndGBuffer → 各自的 DrawParams
MobileCharacterForwardPass  → CharacterForwardInstanceCullingDrawParams  ← [GR] 定制角色 pass（仅 build；Forward 不实际 draw，见 §5.2）
```
即 translucency/decal/debug/sky 的 draw params 在 Single/Multi 分流**之前**全部备好，两条路径共用同一份。

### 3.4 Per-view UB 更新（方向光 / Toon / LuxGI）

- `UpdateDirectionalLightUniformBuffers`（2974）：`CachedView == &View` 时**直接 return（去重）**，只对每 view 首次更新 3 通道方向光 UB。
- `UpdateToonShadingUniformBuffers`（3162-3179，[GR Mobile Toon] lemonxqyang）：**每次 view 都重写**（无去重）场景级 `MobileToonShadingUniformBuffer`；`FToonShaderParameters` 收集 `ToonShadowColor`、`ToonMin/MaxLuminance`、`ToonLumenDiffuseDesaturateWeight`、`ToonLumenWeight=clamp(GlobalToonLumenWeight,0,1)`、`Toon_F0_ColorBlend`、**`LogInvPreExposure = log2(1/View.PreExposure)`**、`ToonPreExposureWeight`、`ToonConstExposure`、`ViewDirection`。Forward（2028）与 Deferred 单/多 pass（2439/2732/2788）都调用——toon 是两条路径共有的 GR 定制，`LogInvPreExposure` 揭示 **PreExposure 直接参与 toon 计算**（关联记忆 `mobile-fwd-vs-def-exposure-mismatch`）。注意：PreExposure **已被实测排除为 Forward/Deferred 画面亮度差主因**（探针两边均 2.0，见 §10 待办 #6 / 记忆 `fwd-vs-def-post-tonemap-2x-unpinned`）——"参与 toon 计算"≠"两侧取值不同"。
- `[ZXB] UpdateLuxGIUniformBuffers`（2030-2039）：Forward 独有补加，对齐 Deferred（见 §9）。

---

## 4. RenderForwardSinglePass（2085-2163）

**核心思想**：一个 Render Pass 内把「几何 → 光照/半透明 → 内联 tonemap」串起来。SceneColor 与 Depth 全程在片上（on-chip），Decals/Fog/Translucency 通过 **framebuffer fetch** 读取深度与颜色。**注意：真 subpass 只有 Vulkan**（`vkCmdNextSubpass` + input attachment）；Metal/GL 的 `NextSubpass()` 是 **no-op**，下面的 subpass 0/1/2 图只是 RDG 层抽象，实际是同一 render pass 内顺序绘制 + 按需 FBF（见 §4.2）。

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  Render Pass 1 : "SceneColorRendering"   ERDGPassFlags::Raster | NeverMerge  │
│  RT0 = SceneColor(+Resolve)   RT1 = ViewFamilyTexture(仅内联 tonemap)         │
│  Depth = SceneDepth(+Resolve)                                                 │
│  SubpassHint = CustomResolveSubpass(内联) | DepthReadSubpass                  │
│                                                                               │
│  Subpass 0（写颜色+深度）                                                       │
│   ├─ [Editor] DrawClearQuad(View.BackgroundColor)        // 仅首 view & 非捕获 │
│   ├─ RenderMaskedPrePass()            // 仅 MaskedOnly 深度模式               │
│   ├─ RenderMobileBasePass()           // Opaque+Masked + Sky + EditorPrims   │
│   ├─ RenderMobileDebugView()          // DebugViewMode（可编译关闭）          │
│   └─ PostRenderBasePass()             // ViewExtension 回调                   │
│           │  RHICmdList.NextSubpass()   // → 深度变只读、可 fetch             │
│  Subpass 1（读 SceneColor+Depth，深度只读）                                    │
│   ├─ RenderDecals()                    // 立方体投影，写 SceneColor           │
│   ├─ RenderModulatedShadowProjections()// Modulated 阴影（后处理式投影）      │
│   ├─ RenderFog()                       // SIM 平台跳过（见 4.1）             │
│   ├─ RenderTranslucency()              // StandardTranslucency               │
│   ├─ [debug] RenderMobileDebugPrimitives() // LDR / tonemap-subpass 时       │
│   ├─ RenderOcclusion()                 // 硬遮挡查询（最后 view）             │
│   └─ PreTonemapMSAA()                  // iOS on-chip tonemap（MSAA resolve 前）│
│           │  NextSubpass()              // 仅 bTonemapSubpassInline          │
│  Subpass 2（内联 tonemap，Vulkan）                                            │
│   └─ RenderMobileCustomResolve()       // SceneColor+tonemap → ViewFamilyTexture│
└───────────────────────────────────────────────────────────────────────────────┘
        │
        └─ [pass 后] AddResolveSceneDepthPass()   // 仅 无硬件 depth-resolve 且 !full prepass
```

### 4.1 关键代码细节

- **`ERDGPassFlags::NeverMerge`**（2101）：多 view 时该 pass 不允许与相邻 pass 合并，因为 subpass 连续性要求整个 render pass 一次提交。
- **SubpassHint 设置**（2093）：
  ```
  PassParameters->RenderTargets.SubpassHint = bTonemapSubpassInline
      ? ESubpassHint::CustomResolveSubpass   // Vulkan 内联 tonemap
      : ESubpassHint::DepthReadSubpass;      // 一般 fetch
  ```
  PSO 层与此**同源判定**（见 §9 / MeshPassProcessor.cpp:2140），保证 PSO 布局与 render pass 布局一致。
- **遮挡查询**（2094）：`!bIsFullDepthPrepassEnabled && ViewContext.bIsLastView && DoOcclusionQueries()` 才开启，查询数由 `ComputeNumOcclusionQueriesToBatch()`（2913-2930）汇总——遍历所有 view 累加 `Individual + Grouped` batch 查询数，冻结 view（`ViewState.bIsFrozen`，仅非 shipping 判定）除外。
- **Adreno 特例**（2140-2145）：`CVarMobileAdrenoOcclusionMode` 且 GL 平台时，查询前 `SubmitCommandsHint()` 强制 flush，规避 Adreno 的查询命令堆积问题。
- **Fog 平台门控**（2122）：`GMaxRHIShaderPlatform != SP_METAL_SIM` —— 用**全局最大平台**而非当前平台判断：进程 target 含 Metal 模拟器（PC 上跑 iOS 模拟器调试）时**跳过 RenderFog**。MultiPass 无此判断（Metal 平台必走 FBF 单 pass，MultiPass 平台集合里不存在 Metal SIM）。
- **LUT 生成时序**（2090）：`bTonemapSubpassInline` 时在 SceneColorRendering pass **开始之前**调 `AddCombineLUTPass` 生成 LUT（含跨帧缓存，见 §6.11），存入 `PassParameters->ColorGradingLUT`，由 Subpass2 的 `RenderMobileCustomResolve` 采样做 tonemap。
- **`AddAlphaInvertPass`**（2078-2082）：`r.AlphaInvertPass` CVar 门控，在 SceneColorRendering 之外的**独立全屏 pass** 做 alpha 翻转（见 §6.13）。

### 4.2 Metal/GLES 的实现机制：单 render pass 顺序绘制 + 按需 framebuffer fetch

**核心结论：Metal/GLES 的 SinglePass 没有真 subpass**。`RHINextSubpass` 在 Metal（MetalRHI **无实现**）和 GLES（OpenGLRenderTarget.cpp:1136 调基类空实现）都是 **no-op**，只有 Vulkan 是 `vkCmdNextSubpass`（VulkanRenderTarget.cpp:707）。§4 开头的 subpass 0/1/2 图是 RDG/渲染层的**抽象**，Metal/GL 实际是：

```
[同一个 render pass，连续顺序执行]
RenderBasePass → RenderDecals → RenderModulatedShadow → RenderFog → RenderTranslucency
（各阶段之间无数据依赖，不需要 subpass 依赖）
```

为什么能这样：

- **forward 光照不需要读回**：base pass fragment shader 一次算完光照直接写 SceneColor；decals/fog/translucency 是**普通 alpha 混合**画上去，不依赖前一阶段结果 → 顺序执行即可。
- **FBF 按需**：需要读当前帧缓冲时才用 framebuffer fetch——Metal `gl_LastFragDataRGBA_0.Load(...)`（MetalCommon.ush:40-46，MetalShaderCompiler patch 成 Metal framebuffer fetch，`#if IOS && PIXELSHADER`）；GLES `FramebufferFetchES2()`（OpenGLShaderCompiler.cpp:2833 编译器生成，基于 `EXT_shader_framebuffer_fetch`）。用途：半透明颜色透射可编程混合（MobileBasePassPixelShader.usf:1669，**默认关** :132）。
- **FBF 与真 subpass 殊途同归**：都让数据留在 tile（on-chip）省带宽——Metal/GL 靠 FBF，Vulkan 靠 input attachment。

**RequiresMultiPass 为什么要求 FBF 前提**（§2.1 决策树）：`Metal && GSupportsShaderFramebufferFetch`、`Android GL && (FBF || DepthStencilFetch)` 才 SinglePass。有 FBF 保证"需要读当前 RT 的效果"（可编程混合、depth/stencil fetch）能单 pass 内做；**无 FBF 的老 GL → MultiPass**（第二 pass 用 resolve 后的纹理采样）。

| | Vulkan | Metal/GLES |
|---|---|---|
| subpass | **真 subpass**（`vkCmdNextSubpass` + input attachment） | **无**（`NextSubpass` no-op，纯标记） |
| 数据留 tile | input attachment（`VulkanSubpassFetch0`） | framebuffer fetch（`gl_LastFragData` / `FramebufferFetchES2`） |
| 阶段依赖 | 显式 subpass 依赖 | 无（普通混合顺序执行） |
| tonemap 内联 | 能（最后 subpass 读 input attachment） | **不能**（无 subpass → 独立 pass 读 Resolve，见 §2.2） |

**推论**：tonemap 内联只有 Vulkan，不是 Metal/GL 硬件做不到，而是它们的 SinglePass **没有 subpass 结构**（`NextSubpass` no-op），内联无从谈起；要在 Metal/GL 内联需先实现"场景 pass 最后一步 FBF 读 SceneColor 写 backbuffer"的路径（技术可行，UE 未实现）。

### 4.3 RenderDoc 实测：SinglePass 帧的实际 Pass 组成（2026-08-21）

**实测环境**：编辑器 `-vulkan`（AndroidVulkan_Preview / Android_High）+ Forward（`r.Mobile.ShadingPath=0`）+ `r.Mobile.TonemapSubpass=1` + `r.Mobile.EarlyZPass=2`（masked prepass）+ `r.ShadowQuality=0`。RenderDoc 截帧 `2026.08.21-20.25.51_capture.rdc`。

**移动渲染侧（`MobileSceneRender`）按执行顺序**：

| 阶段 | Pass | draw | 说明 |
|---|---|---|---|
| ① GPU Scene | `ClearGPUMessageBuffer` / `ShaderPrint::UploadParameters` | 1+1 | 诊断缓冲 |
| | `GPUScene.UploadDynamicPrimitiveShaderDataForView` | 3 | `SetInstancePrimitiveIdCS` + `ScatterUpload`（primitive/instance 动态数据） |
| ② 光照网格 | `ComputeLightGrid`：`CullLights` → `LightDataBufferCopy` → `LightGridInject` → `LightGridFeedbackStatus` | 4 | 3D tile 光照聚类（此帧 `NumLights=0`） |
| ③ 天空大气 | `SkyAtmosphereLUTs`：`TransmittanceLut` → `MultiScatteringLut` → `DistantSkyLightLut` → `SkyViewLut` → `CameraVolumeLut` | 5 | 大气 LUT 预计算 |
| ④ 阴影 | `ShadowDepths`：`ClearIndirectArgInstanceCount` → `CullInstances` | 2 | `r.ShadowQuality=0`，仅 GPU culling、无实际阴影渲染 |
| ⑤ 主场景 | **`SceneColorRendering`**（单个 render pass，`SubpassHint=CustomResolveSubpass`） | | |
| | `MobileRenderPrePass` | 58 | **masked-only prepass**（`r.Mobile.EarlyZPass=2` → `DDM_MaskedOnly` → `bIsFullDepthPrepassEnabled=false`） |
| | **`MobileBasePass`** | **62** | forward base pass 写 SceneColor + depth（GPU 主开销 ~2.3ms） |
| | `Translucency` | 2 | 半透明 |
| | `BeginOcclusionTests` | 1 | 遮挡查询 |
| | **`MobileTonemapSubpass`** | **1** | **subpass 2 内联 tonemap：SceneColor → backbuffer（SinglePass 判别特征）** |
| ⑥ UI | `CanvasBatchedElements` / `SlateUI` / `CopyImageToBackBuffer` | — | 编辑器 UI，非移动渲染核心（真机帧无） |

**与 §4 subpass 图的对应与配置差异**：

- **`MobileTonemapSubpass`（1 draw）即 subpass 2 的 `RenderMobileCustomResolve`**——SinglePass 的铁证；MultiPass 帧里它是独立 `PostProcessing/Tonemap` pass。
- 本帧**无独立 `PostProcessing`**（bloom/tonemap）marker：tonemap 已内联进 render pass，且编辑器 LDR/简化设置下后处理被收窄。
- `MobileRenderPrePass` 是 masked prepass（非 full prepass），由 `r.Mobile.EarlyZPass=2` 决定。
- 无 LuxGI / SSXR pass（Forward 下该场景未触发）。
- **尺寸约束（关联 2026-08-21 修复）**：内联 tonemap 把 SceneColor 与 backbuffer 绑进**同一个 render pass**，Vulkan 要求两者**同尺寸**。SceneColor 经 `QuantizeSceneBufferSize` 对齐到 4（视口 837→840），backbuffer 未对齐（837）→ 此前 `VulkanRenderTarget.cpp:1018` Ensure、RenderDoc 截帧崩溃。已修复：`MobileShadingRenderer.cpp` `InitViews` 内 `bTonemapSubpassInline` 时把 `SceneTexturesConfig.Extent` 覆盖为 backbuffer 尺寸（见 §9.1）。

---

## 5. RenderForwardMultiPass（2165-2255）

**核心思想**：BasePass 结束后**结束 render pass**，resolve 深度/颜色、建 HZB；再**开一个新 render pass** 重新绑定 SceneColor（`ELoad`）与只读深度，绘制 Decals/Fog/Translucency。两个 pass 之间靠 resolve 后的纹理传递数据。

```
│ Pass 1 结束
├─ AddResolveSceneDepthPass()                 // !full prepass：深度 resolve
├─ AddResolveSceneColorPass(DepthAux)         // bRequiresSceneDepthAux：DepthAux resolve
└─ RenderHZB()                                // bShouldRenderHZB && !full prepass
        ↓  重新分配 SecondPassParameters（复制 + 修改）
┌───────────────────────────────────────────────────────────────────────────────┐
│  Render Pass 2 : "DecalsAndTranslucency"    ERDGPassFlags::Raster            │
│  RT0 = SceneColor(ELoad)   无 RT1（DepthAux/ViewFamilyTexture 输出被移除）    │
│  Depth = DepthRead_StencilRead / StencilWrite（ModulatedShadows 时写 stencil）│
│  MobileBasePassUB = EMobileBasePass::Translucent 版                           │
│  SetupMode = SceneDepth | SceneDepthAux | CustomDepth                        │
│                                                                               │
│   ├─ RenderDecals()                    // 从 resolve 后的深度/SceneColor 采样 │
│   ├─ RenderModulatedShadowProjections()                                       │
│   ├─ RenderFog()                                                              │
│   ├─ RenderTranslucency()                                                     │
│   ├─ RenderOcclusion()                 // 硬遮挡查询（最后 view）             │
│   └─ PreTonemapMSAA()                  // iOS on-chip tonemap（实际平台无效果）│
└───────────────────────────────────────────────────────────────────────────────┘
        │
        └─ AddResolveSceneColorPass(SceneTextures.Color)   // MSAA 时显式 resolve 到 Resolve target
```

### 5.1 关键代码细节

- **Pass1 lambda**（2170-2190）：仅做 清屏 → MaskedPrePass → MobileBasePass → DebugView → PostRenderBasePass，**没有** Decals/Fog/Translucency。`RDG_GPU_STAT_SCOPE(MobileBasePass)`（2168，GR 定制打点）。
- **中间 resolve/HZB**（2194-2206）：
  ```
  if (!bIsFullDepthPrepassEnabled)          AddResolveSceneDepthPass(Depth);
  if (bRequiresSceneDepthAux)               AddResolveSceneColorPass(DepthAux);
  if (bShouldRenderHZB && !bIsFullDepthPrepassEnabled) RenderHZB();
  ```
- **SecondPassParameters 改造**（2216-2227）——这是 MultiPass 与 SinglePass 最关键的差异点：
  ```
  *SecondPassParameters = *PassParameters;                       // 深拷贝
  SecondPassParameters->MobileBasePass = CreateMobileBasePassUniformBuffer(
          GraphBuilder, View, EMobileBasePass::Translucent,     // ← 换成 Translucent UB
          SetupMode /* SceneDepth|SceneDepthAux|CustomDepth */);
  SecondPassParameters->RenderTargets[0].SetLoadAction(ELoad);   // SceneColor 载入已有内容
  SecondPassParameters->RenderTargets[1] = FRenderTargetBinding();// 移除 DepthAux / backbuffer 输出
  SecondPassParameters->RenderTargets.DepthStencil 深度/模板 = ELoad;
  SecondPassParameters->RenderTargets.DepthStencil.SetDepthStencilAccess(
          bModulatedShadowsInUse ? DepthRead_StencilWrite : DepthRead_StencilRead);
  ```
  `bModulatedShadowsInUse` 来源见 §2.3（ShadowSetupMobile.cpp:352-359）：true 时第二 pass 深度模板 = `DepthRead_StencilWrite`（modulated shadow 投影要写 stencil，FIXME 注释注明）。SinglePass 无此判定（`DepthReadSubpass` 已隐含）。
- **遮挡查询**（2226）：同 SinglePass 条件（`!full prepass && last view`）。
- **最终 resolve**（2254）：`AddResolveSceneColorPass(SceneTextures.Color)` 把 MSAA SceneColor 显式 resolve 到 `Color.Resolve`。
- **两 pass 的 SubpassHint 均为 None**：MultiPass 不用 subpass，纯独立 render pass。

### 5.2 与 RenderDeferredMultiPass 的结构差异（2530-2730）

| 维度 | Deferred MultiPass | Forward MultiPass |
|---|---|---|
| **Decals 位置** | full prepass 时并入 Pass1（2600-2603）；否则独立 `Decals` pass（2637-2644，SetupMode 仅 SceneDepth、仅 mesh decal） | 恒在 Pass2 `DecalsAndTranslucency` |
| **Occlusion 位置** | Pass1 内、decals 前（2594-2598） | Pass2 内、translucency 后 |
| **HZB 源** | `SceneTextures.Depth.Target`（2609） | `SceneTextures.Depth.Resolve`（2205，先 resolve 再建 HZB） |
| **RT 布局** | SceneColor + GBufferA/B/C + DepthAux（`GetColorTargets_Deferred` 2343-2373）；PLS 时仅 SceneColor（2350-2353）。**GBufferD 恒不启用**：`MobileUsesExtenedGBuffer` 被 Mega 硬编码 `&& false`（RenderCore/Private/RenderUtils.cpp:651，"Remove GBuffer limits for QA review"） | SceneColor(+DepthAux) |
| **ScreenSpaceOutline** | 2540-2541 与 Forward 2049 同源（[GR Toon]/[ZXB]，`MobileCharFeatureTexture.Resolve`） | 同左 |
| **Deferred 独有段** | SSXR（2682-2713）、LuxLightProbes 可视化（2648-2679）、**`Test.CharacterForward` 角色 forward 分流**（`RenderCharacterForward`：SinglePass 内联 2512-2515 / MultiPass 独立 "CharacterForwardRendering" pass 2836-2857；门控 2717-2720）、**`RenderToonOutlineToSceneColor`**（DeferredShadingRenderer.cpp:3600-3609：lighting 后、`IsToonShadingNewEnabled` 门控，toon outline 合成进 SceneColor，关联 `SceneColorCopy`） | Forward 侧仅 build `CharacterForwardInstanceCullingDrawParams`（1927）**不实际 draw**；outline 在 Forward 走 BasePass shader 内 lerp（§9.2），无独立合成 pass |

> 反推结论：**Forward MultiPass 的 Pass2 是"合成所有非 base-pass 内容"的简化收尾**；Deferred 因有 GBuffer+lighting 编排，把 decals/lighting/translucency 拆成更细的 pass + `DeferredShadingSubpass`。

---

## 6. 子回调逐一详解（两条路径共用）

### 6.1 RenderMaskedPrePass（1105-1111）与 RenderFullDepthPrepass（988-1049）
- `RenderMaskedPrePass` 仅 `bIsMaskedOnlyDepthPrepassEnabled`（`EarlyZPassMode==DDM_MaskedOnly`）时调用 `RenderPrePass(RHICmdList, View, &DepthPassInstanceCullingDrawParams)`。
- **full depth prepass** 在 `RenderFullDepthPrepass()` 单独执行：**独立 render pass**（RT 仅 Depth，`EClear`+`DepthWrite_StencilWrite`，`EMobileBasePass::DepthPrePass` UB、SetupMode=None），多 view 非首 view 改 `ELoad`；随后 `DDM_AllOpaqueNoVelocity` 渲染 velocity；最后 **GR Seethrough** 的 `SupplementaryDepthPass`（`GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1` 时，让可穿透/可擦除轮廓物体在 velocity 之后补写深度/stencil，对齐 PC，1041-1074）。
- **full prepass 的遮挡查询在 `RenderFullDepthPrepass` 末尾的独立 pass 发出**（1076-1100）：`bIsLastView && DoOcclusionQueries() && !bIsSceneCaptureRenderPass` 时以 `DepthRead_StencilRead` 状态开 "RenderOcclusion" pass，随后 `FenceOcclusionTests`（1102）。这是除 §6.9 的 SinglePass Subpass1 / MultiPass Pass2 内发出之外的**第三种位置**。
- Single/Multi 内调用的只是 **MaskedOnly 情况**（不透明体在 BasePass 内直接画）。

### 6.2 RenderMobileBasePass（MobileBasePassRendering.cpp:607-632）
```
SetViewport(View.ViewRect)
View.ParallelMeshDrawCommandPasses[EMeshPass::BasePass].Draw(RHICmdList, InstanceCullingDrawParams);
if (ShowFlags.Atmosphere)  SkyPass.Draw(...);
RenderMobileEditorPrimitives(...);    // 编辑器简单元素/半透明调试线
```
- `InstanceCullingDrawParams` 来自 §3.3 `BuildInstanceCullingDrawParams()`。
- [GR] 注释处 TriClusterBasePass 未启用。

### 6.3 PostRenderBasePass（2881-2892）
遍历 `ViewFamily.ViewExtensions` 调 `PostRenderBasePassMobile_RenderThread`，供插件在 BasePass 后、decals 前插入。

### 6.4 RenderMobileDebugView（2894-2911）
仅 `WITH_DEBUG_VIEW_MODES` 且 `UseDebugViewPS()` 时：清黑 → 绘制 `EMeshPass::DebugViewMode`（shader complexity 等）。

### 6.5 RenderDecals（MobileDecalRendering.cpp:46-86）
```
平台支持？（Vulkan / SIM / GL+Fetch 支持 decals；LDR 下 Metal 无 DepthAux → 不支持）
→ RenderDeferredDecalsMobile（立方体投影 decals，写 SceneColor）
→ 若有 MeshDecal mesh pass（MeshDecal_SceneColor / _SceneColorAndGBuffer），绘制
```
- Deferred 平台走 `MobileBeforeLighting` 阶段；DBuffer 平台走 `Emissive` 阶段；Forward 走 `Mobile` 阶段。
- 采样深度依赖：SinglePass 用 subpass fetch；MultiPass 用 resolve 后的 SceneDepth（需 DepthAux 支持）。

### 6.6 RenderModulatedShadowProjections（ShadowRendering.cpp:2751-2801）
```
if (!DynamicShadows || bIsPlanarReflection || bRequiresShadowProjections || ShadowQuality==0) return;
for each CastsModulatedShadows 灯光:
    for each ShadowsToProject（FadeAlphas[ViewIndex] > 1/256 且非全屏方向光）:
        ProjectedShadowInfo->RenderMobileModulatedShadowProjection(...)
```
- `bRequiresShadowProjections` 为 true 时（clustered forward 已在 BasePass 内采样 shadow projection 纹理）直接跳过此函数——避免重复投影。

### 6.7 RenderFog（MobileFogRendering.cpp:129-）
- `r.Mobile.DisableVertexFog==0`（GR 项目只开顶点雾）→ 直接 return，仅渲染局部雾体。
- 否则：`CVarPixelFogQuality>0 && 有大气` → 大气透视；`ExponentialFogs>0` → 指数高度雾。
- 雾 pass 用 `STENCIL_MOBILE_SKY_MASK` 做深度/模板裁剪，混合 `BF_One, BF_SourceAlpha`。

### 6.8 RenderTranslucency（MobileTranslucentRendering.cpp:7-21）
- `ShouldRenderTranslucency(StandardTranslucencyPass) && ShowFlags.Translucency` 时绘制 `EMeshPass::TranslucencyStandard`（`TranslucencyInstanceCullingDrawParams`）。
- 注意 Mobile 只有一个半透明 pass（无 SeparateTranslucency mesh pass，除非独立半透明激活）。

### 6.9 RenderOcclusion 与遮挡查询体系（SceneOcclusion.cpp）

**`RenderOcclusion`**（2318-2335）：`DoOcclusionQueries()` 时 `AllocateOcclusionTests` + `BeginOcclusionTests`，在 SinglePass Subpass1 / MultiPass Pass2 内、translucency 之后发出。

**`AllocateOcclusionTests`**（1765-1912）：
- 按 view 分配遮挡查询；`FOcclusionQueryHelpers::GetNumBufferedFrames(FeatureLevel)` 决定查询双/三缓冲；用 `PrimitiveProbablyVisibleTime` 裁剪遮挡历史。
- **`FeatureLevel > ES3_1` 时才为阴影光分配 shadow 遮挡查询**（点光 LightInfluenceSphere / CSM / spot / OccludedPerObjectShadows）；**移动端 ES3_1 跳过全部 shadow/reflection 遮挡，只做 primitive 遮挡**。
- **`AllocateProjectedShadowOcclusionQuery`"是否发查询"决策**（842-900）：
  - `SOQ_LightInfluenceSphere`（点光）：相机在光包围球内（含 `2×NearClippingDistance` 松弛）或正交投影 → **不发查询**（851-867）。
  - `SOQ_NearPlaneVsShadowFrustum`：shadow frustum 与近平面相交 → **不发查询**（868-882）。
  - 分配走池化：`OcclusionQueryPool->AllocateQuery()` + `FProjectedShadowKey` 键控 + `ShadowOcclusionQueryMaps[QueryIndex]` 多缓冲。
- **SOQ 各调用点差异**（1834-1878）：
  - **CSM**：仅 `GOcclusionCullCascadedShadowMaps && ShadowSplitIndex > 0` 才查——**第一级联总是可见不查**；用 `SOQ_None`。
  - **普通 projected shadow**：`!bPreShadow && !SubjectsVisible` 才查（preshadow 已隐式剔除；subject 可见则 frustum 必不遮挡）；`EnablePointLightCustomOcclusion() ? SOQ_None : SOQ_NearPlaneVsShadowFrustum`。
  - **OccludedPerObjectShadows**（上帧被遮挡的 per-object shadow）：恒 `SOQ_NearPlaneVsShadowFrustum`。
  - **planar reflection 遮挡查询被整体注释**（1890-1901）；`NumReflectionBufferedFrames = NumBufferedFrames + 1`（1887-1888）注释揭示时序：**查询主帧晚期提交、帧开头读取 → 多缓冲一帧**。
  - primitive 遮挡来自 `View.IndividualOcclusionQueries` / `GroupedOcclusionQueries` 的 batch（1904）。

**`BeginOcclusionTests`**（1915-2050）：
- `check(RHICmdList.IsInsideRenderPass())`（1922）——**必须在 render pass 内**，这是它只能出现在 SinglePass Subpass1 / MultiPass Pass2 的硬约束。
- PSO 全禁用：BlendState `CW_NONE`（不写颜色）、DepthStencil `false, CF_DepthNearOrEqual`（只深度测试不写深度），纯遮挡测试。
- 只画 culling geometry 的**前向面**（1946-1947，省一半像素填充），RasterizerState 按 `View.bReverseCulling` 选 `CM_CW`/`CM_CCW`。
- 视口 downsample：`GetDownscaledRect(ViewRect, DownsampleFactor)`，Mobile 路径传 `1.0f`。
- `FeatureLevel > ES3_1` 才执行 shadow/reflection 遮挡：点光 `ExecutePointLightShadowOcclusionQuery`（用**包围球**绘制遮挡体，`CVarPointLightCustomOcclusionScale` 缩放半径）、CSM 用 6 顶点平面、spot shadow 用 8 顶点 cube、planar reflection 用 8 顶点 cube（1994-2031 CPU 构建顶点缓冲）。
- `ShowFlags.OcclusionMeshes` 时可画遮挡网格做 debug 可视化（1956-1961）。
- **[GR 定制] `RenderOcclusion(FRDGBuilder&, FRDGTextureRef SceneDepth)` 重载**（2338-2381）：**HZB 遮挡提交**（区别于上面的硬遮挡查询）——`bHZBOcclusion`（`r.HZBOcclusion && !software occlusion && !SimpleSceneRendering`，后者为 GR_SCENECAPTURE JLP 的修改）时 `RenderHZB` + `HZBOcclusionTests.Submit`；非 HZB 时重置查询历史。此重载为 GR ADD，不在上游。

### 6.10 PreTonemapMSAA（3202-3243）
```
bool bOnChipPP  = PF_FloatRGBA 支持 && FBF && ShowFlags.PostProcessing;
bool bOnChipPreTonemapMSAA = bOnChipPP && Metal && NumMSAASamples>1;
if (!bOnChipPreTonemapMSAA || bGammaSpace) return;   // 绝大多数平台直接返回
绘制 FPreTonemapMSAA_Mobile 全屏三角（PF_FloatRGBA 目标，在 MSAA resolve 前做 tonemap）
```
- 仅 **iOS Metal + MSAA** 生效。两条路径都调用，但 MultiPass 平台（老 GL 无 FBF）在此恒为 no-op（防御性调用）。

### 6.11 RenderMobileCustomResolve 与 LUT 生成（PostProcessTonemap.cpp / CombineLUTs.cpp）
- 内联 tonemap subpass 核心：把 SceneColor（MSAA）fetch 回，经 ColorGradingLUT + SceneColorTint tonemap，写入 `ViewFamilyTexture`。`SubpassMSAASamples` 从 shader 排列维度（`FMobileCustomResolvePS::FTonemapperSubpassMsaaDim`）控制 MSAA 采样数。
- 在 SinglePass Subpass2 内调用（仅 `bTonemapSubpassInline`）。独立 `AddMobileCustomResolvePass`（1268）用于非内联路径。
- **`AddCombineLUTPass` 的 LUT 持久化缓存**（495-543）：
  - `GenerateFinalTable`（396-460）从 `ContributingLUTs` **按权重降序贪心**选最强若干（去重、`1/512` 阈值丢弃、上限 `GMaxLUTBlendCount`），`OutTextures[0]` 恒为中性项。
  - **LUT 跨帧持久化**：`View.GetTonemappingLUT` 复用 view state 缓存；`CachedLUTSettings.UpdateCachedValues` 检测变化，`!bHasChanged && CVarLUTUpdateEveryFrame==0` 时**直接返回缓存不重算**（537-542）。
  - [GR] lemonxqyang：OutputTexture 无效时新建并 `AddClearRenderTargetPass` 强制 Clear（524-532）。
  - `bEnableSceneColorCorrect`（584，Yoohaozhang）；bUseComputePass 走 CS 否则 PS；3D 纹理 32×32/slice，2D 展开 1024×32。
  - `FLUTBlenderShader` 排列：`BLENDCOUNT` 1-5、`SKIP_TEMPERATURE`、`OUTPUT_DEVICE_SRGB`、`USE_VOLUME_LUT`（309-331）；PS `MainPS` / CS `MainCS`（仅 SM5）；**Yoohaozhang `FSceneColorCorrectCS`**（`ColorCorrectCS`，读 SceneColorTex + 写 ColorCorrectOutput UAV）。
  - PS/CS 本体统一走 `CombineLUTsCommon`（PostProcessCombineLUTs.usf:152）：中性色构造 → ST-2084/Log 解码 → `WhiteBalance`（`SKIP_TEMPERATURE` 跳过）→ `WorkingColorSpace` → 宽色域扩展（`ExpandGamut`）→ …；`ColorCorrectCS`（489+）标注 "逻辑参考 MainPS"。全流程为通用 UE tonemap LUT 生成，非 GR 特化。

### 6.12 Resolve / HZB pass
- `AddResolveSceneColorPass`（SceneRendering.cpp:7527）：`NumSamples==1 || 非独立 || Memoryless` 直接返回；否则 FMask+视口裁剪做 resolve（S1:zikuan 保留的 MSAA scissor 注释）。
- `AddResolveSceneDepthPass`（7716-7800）：深度 resolve 的 shader **按 NumSamples 分派专用变体** `FResolveDepth2X/4X/8XPS`（含 Array 版），不支持动态循环；深度模板状态 `CF_Always`+`SO_Zero`（resolve 时顺带清 stencil）；`NumSamples==1 || 非独立 || Memoryless` 直接 return。仓库定制：[GR] Linsan `RDG_EVENT_SCOPE("ResolveSceneDepth")`；S1:zikuan "Translucent with msaa, need EClear?" 注释（未启用）。
- `RenderHZB`（3290）：`BuildHZBFurthest` 生成最远 Mip，供遮挡剔除 / SSXR 使用；`FInstanceCullingContext::IsOcclusionCullingEnabled()` 时也导出到上一帧信息。

### 6.13 AddAlphaInvertPass（PostProcessTonemap.cpp:1292-1371）
- `r.AlphaInvertPass` CVar 门控，RenderForward 末尾（2078-2082）调用；**独立全屏 RDG pass**（非 subpass），读 `Color.Resolve` 写回 `Color.Resolve`（`ELoad`）。
- shader `AlphaInvert_MainPS`（PostProcessTonemap.usf），参数 `ColorTexture`(Point/Clamp) + `ColorScale0` + View；`RenderAlphaInvertPass` 同样 `check(IsInsideRenderPass())`。
- 用途：翻转引擎反相输出的 alpha。

### 6.14 RenderVelocities（VelocityRendering.cpp:290-）
- 在 `RenderFullDepthPrepass` 内 `DDM_AllOpaqueNoVelocity` 分支调用（EVelocityPass::Opaque, false）；Deferred 的 SSXR 段也调用（2686-2695）。
- velocity pass 深度模板 = `DepthRead_StencilWrite`（earlyZ=AllOpaqueNoVelocity 时深度已写，只读）；velocity RT 显式清空条件 = 并行 velocity 或 force 且无 draw。

### 6.15 SetupMobileSceneTextureUniformParameters（SceneTextures.cpp:1577-1741）
SetupMode 枚举定义在 `SceneRenderTargetParameters.h:89-103`（None/SceneColor/SceneDepth/CustomDepth/GBufferA-D/SceneDepthAux/SceneVelocity，组合 `GBuffers`/`All`）。传递链：`CreateMobileBasePassUniformBuffer(SetupMode)` → `SetupMobileBasePassUniformParameters`（MobileBasePassRendering.cpp:360）→ `SetupMobileSceneTextureUniformParameters(SetupMode)`。Forward 两条路径里 `MobileBasePassUniformParameters.SceneTextures` 的填充规则：
- **默认全部绑 SystemTextures 占位**（`Black`/`DepthDummy`/`StencilDummySRV`，1585-1625）；未生产的纹理一律占位——`HasBeenProduced` 门控保证 RDG 依赖正确性。
- SetupMode 标志 → 实际绑定：

  | 标志 | 绑定目标 | 备注 |
  |---|---|---|
  | `SceneColor` | `Color.Resolve`（1629-1632） | 非 MSAA 时 Resolve==Target |
  | `SceneDepth` | `Depth.Resolve` + `Stencil`（1645-1652） | **要求非 `TexCreate_Memoryless`**，否则回退 DepthDummy |
  | `PartialDepth` | `PartialDepth.Resolve`（1654-1659） | **SceneDepth 位门控**（无独立位，1654 判 SceneDepth）+ 非 Memoryless |
  | `SceneDepthAux` | `DepthAux.Resolve`（1684-1691） | |
  | `CustomDepth` | CustomDepth.Depth/Stencil（1703-1712） | |
  | `SceneVelocity` | `Velocity`（1714-1720） | |
- **`bPreciseDepthAux`**（SceneTexturesConfig.cpp:492）：`bPreciseDepthAux \|\| MobileRequiresPreciseSceneDepthAux(ShaderPlatform)`；后者（411-419）= **Deferred 恒 true** 或 `r.Mobile.SceneDepthAux==2`——决定 DepthAux 精度格式。
- **SceneDepth 的 Memoryless 防御**与 §2.4"深度目标永不 Memoryless"（SceneTexturesConfig.cpp:324-327 注释）**直接呼应**：base pass shader 要采样 SceneDepth，若目标 memoryless（不可读）则只能拿到 DepthDummy——仓库把深度 Memoryless 关掉正是为保证这条采样路径正确。
- **GR 定制纹理（生产即绑，不受 SetupMode 门控）**：`MobileCharFeatureTexture.Resolve`→`MobileCharacterOutline`（1635-1638）、`MobileCharRenderMaskTexture.Resolve`→`MobileCharRenderMask`（1640-1643）、`SeeThroughTexture`/`TempDilaTexture`（Mega）、**`HZBDepthTexture`（[GR] 忽略烟状物体，`GHZBDepthTextureNode != 0` 门控，1732-1738）**。

---

## 7. 对比分析

| 维度 | RenderForwardSinglePass | RenderForwardMultiPass |
|---|---|---|
| **Render Pass 数** | 1（`SceneColorRendering`） | 2（`SceneColorRendering` + `DecalsAndTranslucency`） |
| **Subpass** | 用 `NextSubpass()` 串联（0→1→2） | 无，纯独立 pass |
| **Decals/Fog/Translucency 时机** | BasePass 后同 pass 的 Subpass1 | 第二个独立 pass |
| **SceneColor 访问方式** | subpass/framebuffer fetch（on-chip） | resolve 后重新绑定（RT `ELoad`） |
| **深度访问方式** | subpass fetch（深度只读） | resolve 后采样（需 `SceneDepthAux`） |
| **MobileBasePassUB** | 全程 `EMobileBasePass::Opaque` | Pass2 换 `EMobileBasePass::Translucent` |
| **SetupMode** | `(SceneDepth?) \| CustomDepth` | Pass2：`SceneDepth \| SceneDepthAux \| CustomDepth` |
| **DepthAux 输出** | 无（或作为 RT1 输出） | Pass1 写+resolve；Pass2 仅采样 |
| **Tonemap** | 可内联（Vulkan `CustomResolveSubpass`） | 走标准后处理链（无内联） |
| **MSAA resolve** | subpass resolve / on-chip；必要时末尾深度 resolve | pass 结束后显式 `AddResolveSceneColorPass` |
| **Pass Flag** | `Raster \| NeverMerge` | `Raster` |
| **SubpassHint** | `CustomResolveSubpass` / `DepthReadSubpass` | `None` |
| **MSAA 目标** | 可 memoryless（`bMemorylessMSAA=true`） | 不可 memoryless（需回读 resolve） |
| **深度保留** | 视 `bKeepDepthContent`；仓库深度恒不 Memoryless | `bKeepDepthContent` 恒 true |
| **Occlusion** | Subpass1 内（translucency 后） | Pass2 内 |
| **触发平台** | Vulkan / Metal+FBF / GL+FBF / LDR / MSAA>1 / SIM | 无 FBF 的老 GL、HDR、非 MSAA、Mobile Deferred |

### 7.1 本质差异一句话

> **SinglePass 用「渲染顺序即数据依赖」在同一个 render pass 里靠 subpass 完成一切，代价是需要 FBF/subpass 硬件能力；MultiPass 用「结果纹理跨 pass」拆成两个 render pass，代价是两次 RT 绑定、显式 resolve、深度保留和带宽回读，换来的是老 GL 平台（无任何 fetch 能力）的兼容性。**

### 7.2 边界条件与陷阱

- **`RenderForwardMultiPass` 的 `PreTonemapMSAA` 调用是防御性的**：触发平台（老 GL 无 FBF）上 `bOnChipPreTonemapMSAA` 恒为 false，实际 no-op。
- **`RequiresMultiPass` 对 MSAA>1 返回 false**：MSAA 场景永远 SinglePass。Vulkan+MSAA 靠 `GRHISupportsMSAAShaderResolve` 走内联 resolve；非 Vulkan+MSAA 走 fetch。无 fetch 平台 + MSAA 被 `GSupportsShaderFramebufferFetch` 分支挡在 SinglePass 之外——但若该平台既无 fetch 又 MSAA，BasePass 后 depth 无法采样，decals 会失效（这类平台实际不存在或已被 `MobileRequiresSceneDepthAux` 兜底）。
- **`bTonemapSubpassInline` 时 RT1 是 backbuffer**，`bRequiresSceneDepthAux` 为 false（400 行 `&& !bTonemapSubpass`），即内联 tonemap 路径不产生 DepthAux，decals 靠 subpass fetch 深度。
- **MultiView 场景**：非首 view 全部 RT 改 `ELoad`；`NeverMerge` 保证 subpass 连续性不被破坏。
- **仓库深度永不 Memoryless（§2.4）**：这是相对上游的行为差异（`//ericado` 注释），影响移动端带宽预算——**已验证**（2026-08-15 第 6 轮）：无未提交改动、注释随 BranchCopy 带入（详见 §10 待办 #5）。
- **`bModulatedShadowsInUse` 只在 MultiPass 生效**：SinglePass 无 stencil 权限切换（`DepthReadSubpass` 已隐含）。

---

## 8. 设计意图与性能考量

1. **TBDR（PowerVR/Mali/Adreno）片内友好**：SinglePass 把 base pass 与 decals/fog/translucency 放在同一 render pass，片上缓冲（On-Chip Memory）可复读 SceneColor/Depth，避免回读到 DRAM 再取。这是移动端性能最优路径。
2. **MSAA memoryless**：SinglePass 下 `bMemorylessMSAA=true`，MSAA 颜色/深度目标可标记 memoryless，只存在于片上，极大省带宽。（注：本仓库深度目标的 memoryless 被注释禁用，见 §2.4。）
3. **MultiPass 的代价换兼容**：无 FBF 的老 GL（如部分 Mali/Adreno ES3.0）没有 subpass/on-chip fetch，只能把 decals/fog 放第二个 pass 用 resolve 纹理。代价：BasePass 后必须 `bKeepDepthContent=true`（深度不能 discard），加一次深度 resolve + 一次 SceneColor resolve + 一次 RT 重绑定。
4. **Vulkan 内联 tonemap 是进阶优化**：连「后处理 tonemap」都塞进 render pass 的最后一个 subpass，写屏的同时完成 resolve，省一整趟后处理带宽。这也是 `r.Mobile.TonemapSubpass` CVar 存在的原因。
5. **PSO 与 render pass 布局的强绑定**：半透明 mesh 的 `SubpassIndex` 在提交时固化进 PSO（见 §9），运行时 render pass 的 SubpassHint 用同一判定函数——两条路径的 subpass 结构是编译期就锁死的，不是运行时随意编排的。

---

## 9. 本仓库定制（ZXB / GR / S1 相关改动）

### 9.1 C++ 层

| 位置 | 改动 | 意图 |
|---|---|---|
| 2030-2039 `[ZXB]` | Forward 增加 `UpdateLuxGIUniformBuffers(GraphBuilder, View)` | 对齐 Deferred（L2353/L2641），否则 LuxGIVolume_* 绑到默认空贴图，LuxGI 间接光采样为 0，foliage 偏暗 |
| MobileBasePassRendering.h:424-434+562 `[ZXB]` | `FEnableLuxGI`：`r.LuxGI` 作为 `ENABLE_LUX_GI` **permutation 维度**（非 CVar+SetDefine 锁死，避免 DDC 不感知失效），运行时按 CVar dispatch permutation id | 支撑 §9.2 修复表的三处 `#if ENABLE_LUX_GI` 编译期剪枝；命名/语义对齐 Deferred 的 FEnableLuxGI（MobileDeferredShadingPass.cpp:221） |
| 2043-2050 `[ZXB]` | Forward 把 `SceneTextures.MobileCharFeatureTexture.Resolve` 传为 `ScreenSpaceOutline` | 对齐 Deferred（L2386-2387），否则 toon 描边走 SystemTextures fallback，描边全白/全黑 |
| 3162-3179 [GR Toon] | `UpdateToonShadingUniformBuffers`（含 `LogInvPreExposure`） | toon 光照 UB，Forward/Deferred 共用（见 §3.4） |
| 2167-2168 GR | `RDG_GPU_STAT_SCOPE(MobileBasePass)` | BasePass 单独 GPU 打点（lemonxqyang） |
| 737 GR | `CVarMobileForceDepthResolve` | PowerVR 上阴影闪烁/深度不更新时强制保留深度 |
| 2753 easonjiang | `RenderModulatedShadowProjections` 增加 `bRequiresShadowProjections` 早退 | clustered forward 已内联阴影投影时避免重复 |
| RenderCore/Private/RenderUtils.cpp:770-774 easonjiang | `MobileUsesFullDepthPrepass` 的 AO 条件 `IsMobileAmbientOcclusionEnabled` → `MobileUsesShadowMaskTextureRuntime`（注释 "Use Last Frame's Mobile AO Texture"） | platform 级 full prepass 判定（§2.3）；与 `MobileRequiresSceneDepthAux` 联动（full prepass 不建 DepthAux） |
| 7581-7595 S1 | ResolveSceneColor scissor 相关注释（zikuan，未启用） | MSAA 半透明 scissor 越界规避，5.3 已不必要 |
| SceneOcclusion.cpp:2338-2381 GR | `RenderOcclusion(RDG, SceneDepth)` 重载（HZB 遮挡提交：`RenderHZB` + `HZBOcclusionTests.Submit`；含 GR_SCENECAPTURE JLP 的 `&& !SimpleSceneRendering`） | 硬遮挡查询之外的 HZB 遮挡路径（见 §6.9） |
| SceneTexturesConfig.cpp:320-333 `//ericado` | 深度目标 `TexCreate_Memoryless` 被注释禁用 | 仓库移动端深度永不 Memoryless，保证 base pass 可采样 SceneDepth（见 §2.4） |
| 2717-2720 Mega | `Test.CharacterForward` 角色 forward 分流（`RenderCharacterForward`：SinglePass 2514 / MultiPass "CharacterForwardRendering" 2848）。实现 = 一次 `MobileCharacterForwardPass.Draw`（MobileCharacterForwardRendering.cpp:37-53，含 TODO reflection/occlusion）；mesh 过滤 = 不透明 + `bUseForMaterial`（UseForMobileToon）+ `[ZXB]` shader binding 防护（MobileCharacterForwardPass.cpp:38-98）；光照策略 `bUsesDeferredShading=false`（forward lit + CSM） | 与记忆 `mobile-fwd-vs-def-exposure-mismatch` 中的 `MOBILE_CHARACTER_FORWARD` 宏同源但为不同控制点（前者编译期宏、后者运行时 CVar）；Forward 侧仅 build `CharacterForwardInstanceCullingDrawParams`（1927）**不实际 draw**；`Test.CharacterForward` 无定义处，默认 false |
| RenderCore/Private/RenderUtils.cpp:651 Mega | `MobileUsesExtenedGBuffer` 硬编码 `&& false`（"Remove GBuffer limits for QA review"） | Mobile 永不使用 GBufferD（§5.2）；原本的平台附件数能力判定被禁用 |
| PostProcessCombineLUTs.cpp:378-393 | `FSceneColorCorrectCS`（ColorCorrectCS） | GR SceneColorCorrect pass 独立 CS（Yoohaozhang）；与 584 行 `bEnableSceneColorCorrect` 同源 |
| PostProcessCombineLUTs.cpp:524-532 | OutputTexture 无效时重建+强制 Clear | [GR] 避免 LUT 未初始化（lemonxqyang） |
| MobileOutlinePrepearPass.cpp（GR 定制文件，拼写 "Prepear"） | MobileToonOutline 前置 pass 宿主；CVar `r.MobileOutline.ToonOutlineUsePreOutline`（默认 2 = "1 + Outline Depth"）、`ToonOutlinePreOutlineMode`（EPreOutlineMode，默认 EyeBrowAndHair）、宽度 CVar（ToonOutline=60 / ToonRim=8 / SceneRim=4）；`[ZXB] GMobilePreOutlineDepthRead`（AA method==AAM_MSAA 时 DepthRead，见记忆 msaa-black-blob-preoutline-depthwrite） | 与 §9.2 的 `MobileToonOutline.usf` 四通道生成配套（C++ 侧 perm `USE_LAPALACIAN_*` / `CALC_SCENE_RIM_LIGHT_MASK`） |
| MobileBasePassRendering.cpp:509-510 [GR] | `SetupMobileDirectionalLightUniformParameters` 的 `SpecularScale=clamp(·,0,10)`、`DiffuseScale=clamp(·,0,1)`（lemonxqyang 对齐 PC） | 方向光 UB 内部填充（§3.4 `UpdateDirectionalLightUniformBuffers`） |
| SceneUtils.cpp:33-40 + 326-329 [GR] | **`r.ToonShadingNew`（`GEnableToonShadingNew` 默认 true）**，`IsToonShadingNewEnabled()`；切换触发 `OnToonShadingPathChanged`（render state recreate） | GR toon 新路径总开关：门控 `TOON_SHADING_NEW` 宏（BasePassRendering.cpp:1842）、`bUseToonData`（PrimitiveSceneInfo.cpp:179）、`RenderToonOutlineToSceneColor`（DeferredShadingRenderer.cpp:3601）、`SceneColorCopy` 创建（SceneTextures.cpp:607） |
| 131 [GR Mobile SceneCapture] | `CVarMobileSceneCaptureMainViewPP`（`r.Mobile.SceneCapture.MainViewPP`，默认 1） | Mobile SceneCapture MainView 合成/后处理流程（lemonxqyang）；关联 scene capture 强制 `bResolveScene=true`（SceneCaptureRendering.cpp:879） |
| 140 [GR LuxGI Debug] | `CVarLuxGIDepthVisibilityTestCount`（`r.LuxGI.DepthVisibilityTestCount`，默认 2） | LuxGI 间接光深度可见性测试迭代数（lemonxqyang）；与 §3.4 LuxGI 对齐呼应 |
| 2444-2454 [GR] zhangyuhao | Deferred**Single**Pass 的 `CreateMobileBasePassUniformBuffer` 加 `MobileBasePassTextures`（ScreenSpaceOutline = `MobileCharFeatureTexture.Resolve`，`[ZXB]` 注释同 Forward 2049） | §5.2 已列 DeferredMultiPass 2540-2541；此处为 DeferredSinglePass 的对应注入 |
| `MobileShadingRenderer.cpp` `InitViews`（`SceneTexturesConfig::Set` 前） | [ZXB Fix 2026-08-21] `bTonemapSubpassInline` 时把 `SceneTexturesConfig.Extent` 覆盖为 backbuffer 尺寸（`ViewFamily.RenderTarget`），修复 SinglePass 内联 tonemap 绑 SceneColor+backbuffer 时 `QuantizeSceneBufferSize` 4 对齐（视口 837→840）与未对齐 backbuffer（837）尺寸不一致导致的 `VulkanRenderTarget.cpp:1018` Ensure / RenderDoc 截帧崩溃（详见 §4.3） | 内联 tonemap 的 render pass 颜色附件必须同尺寸 |

> 范围声明：§9.1 聚焦 Forward 路径及直接相关定制；Deferred/前置阶段另有定制未逐一展开（如 qiacongshe MMH shadow map 1314、Linsan LuxGI `InitViews` 1310、**Mobile Lighting Split `r.Mobile.DeferredLightingSplitPass` 默认 1**（`MOBILE_SPLIT_IS_ENABLED`，Deferred lighting 拆 Direct/Indirect/LocalLight 三路，MobileDeferredShadingPass.cpp:66；§9.2 引用的 MobileDeferredShading.usf 中大量 `MOBILE_SPLIT` 分支即此）**、Beiyu 514 等）。

### 9.2 PSO 层与 shader 层

> 本节内部块（按顺序）：**PSO 固化** → **outline 两链路** → **Forward 对齐修复体系（表）** → **toon outline 数据流** → **四通道生成算法** → 旧实现。修复状态交叉见 §9.3 / §10 代码修复待办。

- **PSO 层 SubpassIndex/SubpassHint 固化**（MobileBasePass.cpp:1232-1248 + MeshPassProcessor.cpp:2128-2145）：
  - `uint8 SubpassIndex = bTranslucentBasePass ? (bDeferredShading ? 2 : 1) : 0`（1233）：**半透明 mesh 的 PSO 固定在 subpass 1（Forward）/ subpass 2（Deferred）执行**。
  - `GetSubpassHint`（MeshPassProcessor.cpp:2128-2145）：Mobile+Forward = `IsMobileTonemapSubpassEnabledInline(...) ? CustomResolveSubpass : DepthReadSubpass`（2140），**与运行时 §4.1 的 2093 行同一判定函数** → PSO 布局与 render pass 布局必然一致。`bIsUsingGBuffers`（Deferred）= `DeferredShadingSubpass`。
  - PSO 层用 `GMaxRHIShaderPlatform`，运行时用 `ShaderPlatform`，同源判定。
- **shader 层 outline 两条链路**（MobileBasePassPixelShader.usf）：
  - **line 76+498 主链路**：`#define ScreenOutlineTexture MobileBasePass.ScreenOutlineTexture`（76），`ChracterOutlineData = ScreenOutlineTexture.SampleLevel(...)`（498，"rgb: BaseColor A: EyebrowOpacity"）——经 **BasePassUB 的 `ScreenSpaceOutline`** 取描边 BaseColor 与眉毛透明度。
  - **line 1532-1542 `[ZXB]` 链路**：`MobileSceneTextures.MobileCharacterOutline`（SceneTextures UB）在 `MATERIAL_SHADINGMODELS_TOON_CHARACTER && !SUBSTRATE_ENABLED && !MATERIALBLENDING_ANY_TRANSLUCENT` 下，`.a`（ToonOutlineMask）做黑色描边：`Color = lerp(Color, black, ToonOutlineMask)`。
  - **`MobileCharFeatureTexture` 通道布局**（[ZXB] 注释，1533-1535）：`float4(SceneRimLight.r, SceneOutline.g, ToonRimLight.b, ToonOutline.a)`（`MobileToonOutline.usf` 生成）；**toon 角色用 `.a` 做黑色描边**。
  - **[ZXB] 修复背景**："Forward toon 描边完全重写，对齐 Deferred（MobileDeferredShading.usf:248-253+409-416）"，旧实现 `dot(rgb,1)` 把 `MobileCharFeatureTexture` 三通道（SceneRimLight/SceneOutline/ToonRimLight）求和当边缘，本质错误。**更早版本的同类问题**见记忆 `mobile-forward-toon-char-black-outline`（ScreenOutlineTexture 全角色 BaseColor 被当边缘遮罩 → 整角色涂黑；fallback White 加剧，已修 White→Black）——两阶段旧实现为同类根因家族。
  - **[GR] `ApplyMobileToonCombineShadowColor` 仅 1 处调用**（1530，紧邻 [ZXB] outline 段）：曾存在 Forward 侧在 TotalLight 上多调一次的双重合并，已删除与 Deferred 对齐（记忆 `forward-combine-shadow-duplicate-removed`）。
  - 结论：两条链路采样**同一 RT（`MobileCharFeatureTexture.Resolve`）**、用途不同；Forward/Deferred 均依赖此纹理（§9 两处注入即为此服务）。

- **Forward 对齐 Deferred 的间接光/曝光修复体系（[ZXB] shader 层，>15 处 `[ZXB Fix]`）**——这是"Forward 对齐 Deferred"的主体 shader 修复，与 §9.1 的 C++ 层对齐（LuxGI UB / ScreenSpaceOutline）配套：

  | usf 位置 | [ZXB] 修复 | 对齐目标 |
  |---|---|---|
  | 653-658 | `TOON_CUSTOMDATA_OVERRIDE_LOCAL` 门控收窄：去裸 `FORWARD_SHADING`（mobile 下=项目级全局值，不区分 opaque/translucent），改为 `FORWARD_SHADING && !SOLID && !MASKED`（Forward 仅半透明覆盖）+ 保留 TRANSLUCENT/SLW 条件 | Deferred opaque TOONSTANDARD 用 Opacity（记忆 `fwd-vs-def-toonstandard-customdata-a-gate`） |
  | 986-1020 | Forward+LuxGI 跳过 base 间接光（避免与 LuxGI fake-global SkyLight SH 重复偏亮）；`r.LuxGI=0` 时 `MOBILE_USE_GBUFFER\|\|ENABLE_LUX_GI` 剪掉天光 SH fallback → 背光全黑 | Deferred base pass 间接光恒 0（由 LightingPass LuxGI 计算） |
  | 1029-1038 | 间接漫反射乘 EnvBRDF（能量守恒），glossy 面不过曝 | Deferred `SkyLightDiffuseMobile`（MobileDeferredShading.usf:129） |
  | 1206-1211 | `CameraVector` 归一化（原未归一化，NoV/EnvBRDF 不一致） | Deferred（MobileDeferredShading.usf:276） |
  | 1229-1236 | `ToonEnergyWeight` 提前到方向光后/LuxGI 前（原位末尾乘，LuxGI 多吃 1.20×） | Deferred 时序（LuxGI 不吃 Weight，MobileDeferredShading.usf:326-330） |
  | 1238-1247 + 1303-1319 + 1600-1605 | **LuxGI double-PreExposure**：快照→剥离 LuxGI 净增量（截帧铁证：内部已乘一次 Pre，末尾再乘→偏亮一档）→ IBL 前剥离 → toon 末尾跳过 Pre → 加回（只乘雾透射） | Deferred 只乘一次 Pre；toon 角色末尾不乘 Pre |
  | 1275-1295 | LuxGI 主调用 `ENABLE_LUX_GI` 编译期剪枝；DeviceZ 传 `SvPosition.z` 使 `ApplyCartoonShadow` 生效（原传 -1 跳过→偏亮偏平） | Deferred 编译期 strip + 真实 DeviceZ |
  | ToonDeferredLightingCommon.ush:17-34 | `bUseCartoonShadow` 按 `IS_MOBILE_BASE_PASS` 映射到 `MobileBasePass.MobileForwardUseCartoonShadow`（base pass 走 UB / 非 base pass 走裸全局）；`ShadowColor` 因与 FGBufferData 成员同名不可 #define，改用局部变量 | Forward base pass 调用 ApplyCartoonShadow 的参数供给（对齐 Deferred） |
  | MobileLightingCommon.ush:38-49 | 同批 8 个 cartoon shadow 参数的 `FoliageShadowIntensity` 同样按 `IS_MOBILE_BASE_PASS` 映射到 `MobileBasePass.FoliageShadowIntensity`（注释明示与 ToonDeferredLightingCommon.ush 分流"完全对称"） | ApplyCartoonFoliage 参数供给（Forward base pass） |

**toon outline 完整数据流**（生成 → 消费）：
1. **RT 创建**（SceneTextures.cpp:614-619，Yoohaozhang）：`MobileOutlineTexture` / `MobileCharFeatureTexture`（PF_R8G8B8A8）+ `MobileCharRenderMaskTexture`（PF_R8_UINT），全 `CreateTextureMSAA`（→ 记忆 `mobile-preoutline-mixed-samples-crash` 的 4x 来源）。
2. **调用**（MobileShadingRenderer.cpp:1630-1639）：`CVarMobileOutlinePassEnable`（`r.YHRP.EnableMobileOutlinePass`，默认 true，定义 94-98）门控 `RenderPreOutlinePass`，位于 DBuffer 之后、`PreRenderBasePass` ViewExtension 之前；关闭时回退 `SystemTextures.Black`。
3. **`RenderPreOutlinePass`**（MobileOutlinePrepearPass.cpp:844+）含两个子 pass：
   - **PreOutline mesh pass**（`EMeshPass::MobilePreOutline`，只画 toon shading model 且 `IsRenderToonOutline()`，输出 `MobileOutlineTexture` = 角色 BaseColor.rgb + 眉毛透明度.a）：`[ZXB] AAM_MSAA → DepthRead_StencilRead`（878-884，记忆 `msaa-black-blob-preoutline-depthwrite` 根因修复）；`[ZXB] MSAA>1 跳过 Velocity 混绑`（886-893，`NumMSAASamples<=1` 才绑 RT1=Velocity，避免 1x Velocity 与 4x outline/depth 同 pass samples 不一致的 Android Vulkan 崩，记忆 `mobile-preoutline-mixed-samples-crash`）；eyeBrow 特殊 RenderState（`CF_Always`+`DepthNop_StencilNop`，眉毛透出头发）；`EPreOutlineMode`/`UsePreOutline` 门控哪些 mesh 走 PreOutline（默认 EyeBrowAndHair，非眉发 toon 走深度 Laplacian）。
   - **MobileToonOutlinePass 全屏 pass**（`FMobileToonOutlinePS`，655-708）：读 PreOutlineTex + **`[ZXB]` 1x SceneDepth（Depth.Resolve 优先，回退 DepthAux.Resolve）**（688-692，记忆 `mobile-forward-toon-char-black-outline` 相关）→ 输出 `MobileCharFeatureTexture` 四通道；`[ZXB]` 输出 4x+resolve 1x（676-678）。
4. **消费**：MobileBasePassPixelShader.usf 两条链路（本小节上文）。

**`MobileCharFeatureTexture` 上游生成算法**（`MobileToonOutline.usf` `MainMobileOutlinePS`，lemonxqyang 新方案）：
- 输入：`PreOutlineTex`（角色预描边纹理，`rgb`=角色 BaseColor、`a`=眉毛透明度）+ `SceneDepthTex` + 宽度参数（`SceneOutlineWidth`/`SceneRimLightWidth`/`ToonRimLightWidth`/`ToonOutlineWidthScale` + DPI/FOV）。
- 掩码基元 `ComputeOutlineAndRimMask`（58-65）：深度 Laplacian（`CalcDepthLaplacian` 48-56，`-(4*C-(L+R+U+D))`）经 `NormFactor=100/saturate(100/max(CenterDepth,1))` 归一化，`smoothstep(MinThreshold,0.5,·)` 出掩码。
- 四通道各自算法：

  | 通道 | 条件 | 算法 |
  |---|---|---|
  | `.a` ToonOutlineMask | 79-83 | `USE_LAPALACIAN_OUTLINE` → Laplacian×`ToonOutlineWidthScale`；否则 `dot(PreOutline.rgb,1)>0 && 非眉毛 ? 1:0` |
  | `.b` ToonRimLightMask | 85-108 | `USE_LAPALACIAN_RIM_LIGHT` → Laplacian（采样距=`ToonRimLightWidth×DPI×FOV`）；否则 4 向 dilate ToonOutlineMask（`DilateScale=40`×texel×DPI×FOV） |
  | `.g` SceneOutlineMask | 110-121 | `EnableSceneOutline>0\|\|EnableRimLight>0` 且 `SceneRimLightWidth>0` → Laplacian×`SceneOutlineWidth`，`MinThreshold=View.EnableSceneOutline` |
  | `.r` SceneRimLightMask | 117-121 | `CALC_SCENE_RIM_LIGHT_MASK` 门控 → Laplacian，采样距=`SceneRimLightWidth×DPI×FOV` |

- **眉毛像素特殊处理**（124-132）：非眉毛输出 `float4(SceneRimLight, SceneOutline, ToonRimLight, ToonOutline)`；眉毛透传 `float4(PreOutline.rgb, max(EyebrowOpacity, ToonOutlineMask))`——眉毛不吃 `.a` 黑色描边，与主链路 498 行 "A: EyebrowOpacity" 呼应。
- 被替换的旧实现（#else 133-198）：Sobel 深度梯度（3×3 Gx/Gy）+ 仅凹侧 RimLight，输出 `float4(RimLight,0,0,Mask)`。

### 9.3 关联记忆索引（Forward/Deferred toon 对齐排查）

> 与本文档主题直接相关的既有排查记忆，按需深挖（记忆文件含完整证据链）：

| 记忆 | 主题 | 与本文档关系 |
|---|---|---|
| `mobile-forward-toon-char-black-outline` | Forward toon 描边整黑（ScreenOutlineTexture 误当边缘遮罩）。**fallback White→Black 修复未落地**（2026-08-16 核对 MobileBasePassRendering.cpp:375 仍为 `SystemTextures.White`）；但 [ZXB] 重写（1532-1542）已使 toon 黑色描边改用 `MobileCharacterOutline`，不受 White fallback 影响（仅 498 眉毛/头发混合链路受影响） | §9.2 outline 两链路 |
| `forward-combine-shadow-duplicate-removed` | `ApplyMobileToonCombineShadowColor` 双重合并删除 | §9.2（仅 1 处调用） |
| `toon-gbuffer-has-no-ao-slot` | Toon 角色 GBuffer **无 AO 位**：Deferred 解码硬编码 AO=1、Forward 保留材质 AO → 光照必不等（**修复未落地**：2026-08-16 核对 usf 无 toon 后置 `GBufferAO=1`，Forward 仍保留材质 AO 于 629/735） | §5.2 差异背景（`MobileUsesExtenedGBuffer` 恒 false → 无 GBufferD/AO 位） |
| `fwd-vs-def-toonstandard-customdata-a-gate` | TOONSTANDARD `CustomData.a`（ShadowFalloff）在 `FORWARD_SHADING` 门控下分叉（Forward 读材质 CustomData0 / Deferred 保留 Opacity）。**门控收窄已落地（653-658）；记忆所述 6bit 量化未落地**（2026-08-16 核对 usf 无 `63`/`round(` 量化） | §9.2 shader 层差异 |
| `mobile-fwd-vs-def-exposure-mismatch` | PreExposure vs ToonEnergyWeight（**修复前快照**；修复后见 §9.2 修复表 ToonEnergyWeight 提前 / LuxGI 双 PreExposure 行——Forward toon 现已跳过末尾 Pre） | §3.4 `LogInvPreExposure` |
| `fwd-vs-def-post-tonemap-2x-unpinned` | 画面亮度差主因（post-tonemap 2×，未定位） | §10 待办 #6 关联 |
| `deferred-final-color-is-two-additive-writes` | 比较基准必须含 emissive（两次加性写入） | 比对方法 |
| `msaa-black-blob-preoutline-depthwrite` / `mobile-preoutline-mixed-samples-crash` | PreOutline DepthWrite / MSAA 采样问题 | §9.2 数据流 |

---

## 10. 阅读范围与遗留待办

> 本小节由 `/loop 10min` 源码深读后整理，细节已并入正文各章。此处仅保留范围声明与未决项。

### 已覆盖（正文已展开）

- 两条路径主流程 + 选路判定 + 全部子回调（§4/§5/§6）
- RT 创建标志与 SceneColor/Depth 格式（§2.4）
- PSO subpass 固化 + shader 层 outline 链路（§9）
- 遮挡查询体系（SOQ 决策/各调用点/执行细节，§6.9）
- LUT 生成与持久化缓存 + SinglePass 时序（§6.11）
- 与 Deferred 的结构差异（§5.2）
- 仓库定制清单（§9）

### 遗留待办（如需可单列或重开循环）

1. `TexCreate_InputAttachmentRead` 的 RHI 层读取路径（Vulkan input attachment / Metal FBF）——判定为**标准平台机制、非仓库特化**，不再深挖。
2. `FLUTBlenderShader` 混合算法**已概述**（§6.11 `CombineLUTsCommon` 流水线，2026-08-15 第 2 轮）；如需逐行展开仍可单列。
3. `GetDefaultMobileSceneColorLowPrecisionFormat` 在真机 LDR 平台的实测格式确认。
4. ~~`MobileToonOutline.usf` 四通道算法~~ **已解决**：补充进 §9.2（`MainMobileOutlinePS` 的 Laplacian/宏分支/眉毛透传，2026-08-15 第 2 轮）。
5. **已验证**（2026-08-15 第 6 轮 + 2026-08-16 官方比对）：`SceneTexturesConfig.cpp` 无未提交改动（`p4 opened` 空 / `p4 diff` 0 行）；`//ericado` 注释随 BranchCopy 带入（filelog #1-4 均为 integrate/branch，#4 change 888829 copy from //GR/Mer），确认为 GR 分支链携带的定制。**官方对比已补齐**（2026-08-16）：Epic 官方文档确认 `bMemorylessMSAA`="True if MSAA targets can be memoryless"、`bKeepDepthContent`="True if the platform should write depth content back to memory"——官方机制**允许**深度 memoryless；本 fork 仅注释掉赋值、if 结构原样保留 + `//ericado` 署名 → 判定 **非官方行为，是 GR 定制**（详见 §2.4"来源判定"）。⚠️ **后续开发点**：恢复深度 memoryless 省带宽的开关就在这里，§2.4 已标注需同步处理的深度消费方清单（toon outline / HZB / SceneDepth 节点 / SSXR）。注：`docs/` 目录整体不在 Perforce（本地分析文档）。
6. **运行时验证**（关联记忆 `mobile-fwd-vs-def-exposure-mismatch`、`fwd-vs-def-post-tonemap-2x-unpinned`、`forward-not-darker-local-lights-ruled-out`）：画面亮度差主因是 **post-tonemap 阶段 Forward ~2× 额外提升**（预后处理 F 暗 0.72× / 后处理后 F 亮 1.46×，tonemap 反转关系）。**已排除**：PreExposure（探针两边均 2.0）、LocalExposure（MobileDeferredShading.usf:41 关）、EyeAdaptation（实测无效）、tonemap 路径（同平台同 PS）、**局部光**（Deferred 注释 `AccumulateLightGridLocalLightingToon` 后画面无变化，记忆 `forward-not-darker-local-lights-ruled-out`）。**下一步 = post-tonemap 探针**（探针插入点已静态确认：`MobileShadingRenderer.cpp:1872` `AddMobilePostProcessingPasses`（标准路径，非内联 tonemap 时；内联 tonemap 走 1867 `AddMobileCustomResolvePass`）之后二次 dispatch 读 ViewFamilyTexture 中心像素；坐标用 `Config.Resolution/2` 非 `ViewSize/2`；需 C++ 重编 + MCP 截图）。

### 代码修复待办（记忆描述的修复未落地，2026-08-16 核对）

> 以下差异是 Forward/Deferred 对齐的**已知遗留**（记忆记录过修复方案但当前代码未落地）；是否修理由设计决策决定（可能有意保留），记录以供对齐工作参考。

- **GBufferAO 对齐**：记忆 `toon-gbuffer-has-no-ao-slot` 的"Forward toon 置 `GBufferAO=1`"未落地——usf 无该修复，Forward 仍保留材质 AO（629/735），与 Deferred 硬编码 1 的间接光差异仍在。
- **CustomData.a 6bit 量化**：记忆 `fwd-vs-def-toonstandard-customdata-a-gate` 的量化（`round(v*63)/63`）未落地——usf 无 `63`/`round(`，Forward 连续值 vs Deferred encode→decode 0.50794（图像影响小，记忆实测 -2.3）。
- **ScreenOutlineTexture fallback**：记忆 `mobile-forward-toon-char-black-outline` 的 White→Black 未落地——MobileBasePassRendering.cpp:375 仍 `SystemTextures.White`；影响已减弱（toon 描边改用 MobileCharacterOutline），仅 498 眉毛/头发混合链路受影响。
