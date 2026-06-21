# iOS / Metal 下实现 TBDR 优化方案：解决方案文档

> **配套文档**：`UE_Mobile_TBDR_Optimization_TechDoc.md`、`UE_Mobile_TBDR_改造决策文档.md`
> **主题**：把前文 Vulkan（Android）下的 TBDR 优化方案，映射到 iOS / Metal 上如何实现
> **引擎基准**：UE5.3+ Metal RHI（iOS / iPadOS，Apple A 系列 GPU）| 版本：v1.0 | 2026-06-20

---

## 0. 一页纸总结（TL;DR）

**好消息：iOS/Metal 是 TBDR 优化的「天选平台」。** Apple GPU 从设计之初就是 TBDR，Metal 把 Programmable Blending、Memoryless、Imageblock、Tile Shading 等片上能力作为一等公民暴露——很多在 Vulkan 上要靠 subpass 费劲拼出来的效果，Metal 上更直接、更强。

| Vulkan（Android）机制 | Metal（iOS）等价 / 更优机制 | UE Metal RHI 是否内建 |
|----------------------|---------------------------|:------:|
| Subpass + Input Attachment | **Programmable Blending**（`[[color(n)]]` 直接读片上） | ✅ 自动转译 |
| `DontStore` / Memoryless | **`MTLStorageModeMemoryless`** | ✅ 自动转译 |
| FrameBuffer Fetch（EXT/QCOM 扩展） | **Programmable Blending**（原生，无需扩展） | ✅ |
| 片上 GBuffer（subpass 0→1） | **Programmable Blending + Memoryless GBuffer** | ✅ `r.Mobile.ShadingPath=1` |
| ——（无对应） | **Imageblock + Tile Shading**（A11+，更强：可跨像素/compute 内联） | ⚠️ 引擎默认不直接用，自定义需改源码 |
| ——（无对应） | **Raster Order Group**（精确控制并行片元访问顺序） | ⚠️ 自定义 |

**核心结论**：
1. **燕云片上 GBuffer**：iOS 上 `r.Mobile.ShadingPath=1` 一行配置，GBuffer 自动用 Programmable Blending + Memoryless 驻留 tile memory——**比 Android 实现更干净（不需要 input attachment 声明）**。
2. **洛克王国 One Pass**：iOS 上**实现成本比 Android 低**——Programmable Blending 原生支持读当前像素，深度/中间 RT 用 Memoryless 一键消除写回。但"自定义把多个 Pass 合成一个 RenderCommandEncoder"仍需改 Metal RHI 源码。
3. **和平精英重剔除**：与图形 API 无关，配置/资产层面 iOS/Android 通用。
4. **进阶**：若要做洛克王国都没做到的极致（tile-based 光源裁剪、OIT），可用 A11+ 的 **Imageblock + Tile Shading**，这是 Metal 独有的杀手锏，但需要 fork 引擎深度定制。

---

## 1. Apple TBDR 与 Metal 的片上能力（基础）

### 1.1 Apple GPU 的 TBDR 本质

Apple GPU 是真正的 **TBDR**（带硬件 HSR 隐面剔除），而非 Mali/Adreno 的 TBR。关键特性：

```
渲染流程：
  顶点/分块阶段(Tiling)  →  逐 Tile 处理：
                            1. 先生成整块的 depth/stencil（HSR 硬件剔除被遮挡片元）
                            2. 只对可见片元跑 fragment shader → 写入 Tile Memory
                            3. 整个 Tile 处理完，才一次性 store 到 device memory
```

- **HSR（Hidden Surface Removal）**：硬件在着色前完成不透明物体的完美遮挡剔除——**这意味着 iOS 上 Opaque 物体往往不需要做 PrePass/Early-Z**（硬件已经帮你做了），这点和 Android Mali/Adreno 需要手动 PrePass 不同。
- **Tile Memory**：片上高速 SRAM，带宽是 device memory 的数倍、延迟低数倍、功耗低得多。
- **Tiling 与 Rendering 阶段分离**：一个 Pass 的 tiling 可与上一个 Pass 的 rendering 重叠，天然流水线。

### 1.2 Metal 暴露的四大片上武器

| 能力 | 作用 | 对应解决的问题 | 最低 GPU |
|------|------|--------------|:------:|
| **Memoryless Render Target** (`MTLStorageModeMemoryless`) | RT 只存在于 tile memory，永不分配 device memory | 消除深度/GBuffer/MSAA 中间 RT 的写回 | 全系 |
| **Programmable Blending** | fragment shader 用 `[[color(n)]]` 入参直接读当前像素的片上颜色 | 自定义混合、片上 GBuffer 读取、One Pass 后处理 | 全系 |
| **Imageblock** | 自定义 per-pixel 片上数据结构（`[[imageblock_data]]`） | 任意布局的片上 GBuffer、多层数据 | A11+ |
| **Tile Shading** | render pass 内联 compute，读写整个 imageblock | tile 光源裁剪、OIT、自定义 MSAA resolve | A11+ |
| **Raster Order Group** (`[[raster_order_group(n)]]`) | 精确控制并行片元访问同一像素的顺序 | OIT、多层混合的正确性 | A11+ |

> **关键认知**：Vulkan 的 subpass input attachment **只能读"当前像素"**，Metal 的 **Programmable Blending 同样是当前像素**——两者在"片上 GBuffer/One Pass"这类逐像素场景上等价。但 Metal 的 **Imageblock + Tile Shading 能让 compute 访问整个 tile 的所有像素**，这是 Vulkan subpass 做不到的，可实现 tile 光源裁剪这类高级算法。

---

## 2. UE Metal RHI：你写的代码如何映射到 Metal

**重要前提**：UE 的渲染代码是 **RHI 抽象的**。你在 `MobileShadingRenderer` 里写的 `FRHIRenderPassInfo` / `SubpassHint` / RenderTarget action，**同一套代码**会被：
- **VulkanRHI** 翻译成 `VkRenderPass` + subpass；
- **MetalRHI** 翻译成 `MTLRenderPassDescriptor` + Programmable Blending + Memoryless。

所以前文 Android 方案里写的 RHI 层代码，**绝大部分在 iOS 上自动工作**，差异在 RHI 的翻译层和 shader 后端。

### 2.1 RenderTarget Action → Metal Load/Store Action

UE 的 `ERenderTargetActions` 直接映射到 Metal 的 `MTLLoadAction` / `MTLStoreAction`：

| UE Action | Metal LoadAction | Metal StoreAction |
|-----------|------------------|-------------------|
| `Clear_Store` | `MTLLoadActionClear` | `MTLStoreActionStore` |
| `Load_Store` | `MTLLoadActionLoad` | `MTLStoreActionStore` |
| `Clear_DontStore` | `MTLLoadActionClear` | `MTLStoreActionDontCare` |
| `DontLoad_DontStore`（Memoryless） | `MTLLoadActionDontCare` | `MTLStoreActionDontCare` |

```objc
// MetalRHI 内部：深度设为 Memoryless 的等价
MTLTextureDescriptor *depthDesc = ...;
depthDesc.storageMode = MTLStorageModeMemoryless;  // ★ 永不落 device memory

renderPassDesc.depthAttachment.loadAction  = MTLLoadActionClear;
renderPassDesc.depthAttachment.storeAction = MTLStoreActionDontCare; // 用完即弃
```

> ✅ 这意味着：你在 UE 里把深度 RT 标 `DontStore`，MetalRHI 会自动用 Memoryless——**洛克王国"深度 Memoryless"在 iOS 上是免费午餐**。

### 2.2 Subpass / FrameBuffer Fetch → Programmable Blending

Vulkan 的 `subpassLoad(InputAttachment)`，在 Metal 上对应 fragment shader 的 **color attachment 入参**：

```metal
// Metal Shading Language：Programmable Blending 读当前像素片上值
fragment half4 LightingPass(
    RasterizerData in [[stage_in]],
    // ★ 直接把上一阶段写在 tile memory 的 GBuffer 作为入参读回，无需采样、无需扩展
    GBufferData gbuffer [[color(0)]]   // color(0..n) 即片上当前像素
)
{
    half3 albedo    = gbuffer.albedo;
    half3 normal    = gbuffer.normal;
    half  roughness = gbuffer.roughness;
    half3 lit = ComputeLighting(albedo, normal, roughness, ...);
    return half4(lit, 1.0);
}
```

UE 的跨平台 shader（`.usf`）通过 `FrameBufferFetch` 宏统一书写，HLSLcc/Metal 后端会把它编译成上面的 `[[color(n)]]` 形式。Vulkan 后端则编译成 `subpassLoad`。**同一份 .usf，双端自动适配。**

---

## 3. 三款手游方案在 iOS/Metal 上的落地

### 3.1 燕云片上 GBuffer（Mobile Deferred）—— iOS 上最干净

**配置（与 Android 相同）**：
```ini
[/Script/Engine.RendererSettings]
r.Mobile.ShadingPath=1
r.MobileHDR=True
```

**iOS 上的实现差异（更优）**：
- Android（Vulkan）：需要显式声明 subpass + input attachment + subpass dependency（BY_REGION）。
- **iOS（Metal）**：GBuffer 用 **Memoryless 纹理** + **Programmable Blending** 读回，**不需要 input attachment 这套机制**，MetalRHI 直接在同一个 `MTLRenderCommandEncoder` 里：
  1. BasePass 把 GBuffer 写进 Memoryless color attachments（tile memory）；
  2. LightingPass 用 `[[color(n)]]` 把 GBuffer 当入参读回，算光照，输出 SceneColor；
  3. 只有 SceneColor 是 `MTLStorageModeStore`，GBuffer 全程 Memoryless。

官方文档明确：移动延迟在支持的设备上 "GBuffer is never stored in system memory"，且 "does not allocate memory when a device supports the LAZILY_ALLOCATED memory type"。iOS 全系 Apple GPU 都支持 Memoryless，**收益拉满**。

> ✅ **结论**：燕云片上 GBuffer 在 iOS 上 = `r.Mobile.ShadingPath=1`，零源码改造，且实现比 Android 更直接。

### 3.2 洛克王国 One Pass —— iOS 实现成本低于 Android

洛克王国的几个子技术在 iOS 上的对应：

| One Pass 子技术 | iOS/Metal 实现 | 是否需改源码 |
|----------------|---------------|:------:|
| 后处理材质合并（FrameBuffer Fetch） | Programmable Blending（`[[color(0)]]` 读当前像素） | ⚠️ 后处理流程改造 |
| Distortion 合并进 Bloom/Tonemap | 同上，逐像素累加 | ⚠️ 后处理流程改造 |
| RGB10A2 Stencil 区分角色 | SceneColor A 通道（`MTLPixelFormatRGB10A2Unorm`），后续 Programmable Blending 读 A | ⚠️ BasePass shader 改造 |
| 深度/SceneColor 全程 Memoryless | `MTLStorageModeMemoryless`（UE 标 DontStore 即自动） | ✅ 配置/RT action |
| PrePass→Tonemap 全程 1 个 Pass | 合并到一个 `MTLRenderCommandEncoder` | ✅ 需改 MetalRHI 编排 |
| Tonemap/ColorGrading subpass 化 | `r.Mobile.TonemapSubpass=1`（Meta XR，但 iOS 同样走 Programmable Blending） | ⚠️ 需插件 |

**为什么 iOS 成本更低**：
1. Programmable Blending **原生**，不像 Android 要判断 `EXT_shader_framebuffer_fetch` / `QCOM_..._noncoherent` 扩展是否支持——**iOS 全系支持，不需要 fallback**。
2. Memoryless 是 storage mode 一个标志位，比 Vulkan 配置 subpass dependency 简单得多。
3. Apple TBDR 的 HSR 让 Opaque 不需要 PrePass，少一个 Pass 要合并。

**仍需源码改造的部分**：把原本多个 `MTLRenderCommandEncoder`（每个对应一个 RenderPass）合并成**一个 encoder 内多 draw**，这是渲染器流程编排，CVar 不暴露，需 fork `MobileShadingRenderer` + `MetalRHI`（见第 4 节）。

### 3.3 和平精英重剔除 —— 与 API 无关

Forward + EarlyZ + 遮挡剔除 + HLOD + 分档，这些都在 UE 配置 / 资产 / DeviceProfile 层面，**iOS/Android 通用**。iOS 特有的微调：

```ini
; iOS DeviceProfile
[iPhone DeviceProfile]
+CVars=r.MobileContentScaleFactor=...   ; iOS 用此控制渲染分辨率（与屏幕原生分辨率解耦）
```

> ⚠️ iOS 注意点：**Opaque PrePass 在 Apple GPU 上常常是负优化**——HSR 已经做了硬件遮挡剔除，再做软件 PrePass 是重复劳动。和平精英的"Masked 植被 Early-Z"在 iOS 上仍有价值（HSR 对 AlphaTest 物体不剔除），但 Opaque PrePass 建议在 iOS 上关掉：
> ```ini
> r.EarlyZPass=1
> r.EarlyZPassOnlyMaterialMasking=1   ; iOS 上尤其重要：只对 Masked 做，Opaque 交给 HSR
> ```

---

## 4. 需要改造时：Metal RHI 改造方案

若要做完整 One Pass（合并多个 encoder）或用 Imageblock/Tile Shading 做进阶优化，需 fork 引擎。改动点：

### 4.1 改造文件清单

```
Engine/Source/Runtime/Renderer/Private/
├── MobileShadingRenderer.cpp        ★ Pass 编排（与 Android 共用同一套 RHI 调用）
├── MobileBasePassRendering.cpp/.h   ★ RGB10A2 角色标记
└── PostProcess/PostProcessMobile.cpp ★ 后处理合并 + FrameBufferFetch 宏

Engine/Source/Runtime/Apple/MetalRHI/Private/
├── MetalRenderPass.cpp              ★ MTLRenderPassDescriptor 的 Load/Store/Memoryless 编排
├── MetalCommandEncoder.cpp          ★ RenderCommandEncoder 合并逻辑
└── MetalStateCache.cpp              ★ Programmable Blending 的 PSO 配置

Engine/Shaders/Private/
├── MobileBasePassPixelShader.usf    ★ 写 RGB10A2 A 通道
└── PostProcessMobile.usf            ★ FrameBufferFetch 宏（Metal 后端→[[color(n)]]）
```

### 4.2 关键改造点

#### (1) 深度 Memoryless（最简单，配置即可）
UE 标 `DontStore`，MetalRHI 自动用 `MTLStorageModeMemoryless`。无需改源码，只要在 RenderPass 编排里把深度 action 设对。

#### (2) Programmable Blending 读 GBuffer / SceneColor
在 `.usf` 用 UE 的 FrameBufferFetch 宏（跨平台），Metal 后端自动编译为 `[[color(n)]]` 入参。需在 `MetalStateCache.cpp` 确保对应 PSO 开启了 fetch 能力（color attachment 作为 shader 输入）。

#### (3) 合并 RenderCommandEncoder（核心改造）
Android 上是 `vkCmdNextSubpass`；**Metal 上没有 subpass 概念**，而是把多个 draw 放进**同一个 `MTLRenderCommandEncoder`**（只要 RT 配置不变，中间结果就留在 tile memory）：

```objc
// MetalRHI 伪代码：One Pass 合并
id<MTLRenderCommandEncoder> encoder =
    [commandBuffer renderCommandEncoderWithDescriptor:onePassDesc];
//   ↓ 同一个 encoder 内，GBuffer/SceneColor 始终在 tile memory
DrawBasePass(encoder);              // 写 Memoryless GBuffer + RGB10A2 角色标记
DrawTranslucencyAndDecals(encoder); // Programmable Blending 读片上深度
DrawMergedPostProcess(encoder);     // Programmable Blending 读 SceneColor，合并后处理
[encoder endEncoding];              // 此时才 store 最终 SceneColor
```

> 关键：**只要不结束 encoder、不切换 RT 配置，Metal 就保证中间结果不落 device memory**。这比 Vulkan 配置 subpass dependency 简单。难点在于 UE 默认会为不同 Pass 创建独立 encoder，需要改 `MobileShadingRenderer` 让它们复用同一个。

#### (4) 进阶：Imageblock + Tile Shading（A11+，Metal 独有）
若要做洛克王国都没做的极致优化（tile 光源裁剪、OIT），用 Imageblock 自定义片上 GBuffer 布局 + Tile Shading 内联 compute：

```metal
// 自定义片上 GBuffer 结构（Imageblock）
struct GBufferImageblock {
    half4 albedo    [[raster_order_group(0)]];
    half4 normal    [[raster_order_group(0)]];
    half  roughness [[raster_order_group(0)]];
};

// Tile Shader：render pass 内联 compute，访问整个 tile 做光源裁剪
kernel void TileLightCulling(
    imageblock<GBufferImageblock> imageBlock,
    threadgroup uint *culledLightList [[threadgroup(0)]],
    ...)
{
    // 读整块 min/max depth → 裁剪光源 → 写 threadgroup 共享给后续 fragment
}
```

这需要在 MetalRHI 里支持 tile pipeline state（`MTLTileRenderPipelineDescriptor`），是较大的引擎扩展。**Vulkan 没有等价物**，所以这是 iOS 专属能力，可作为 iOS 高端档位的差异化优势。

### 4.3 改造工程量评估

| 改造项 | 工程量 | 风险 | 说明 |
|--------|:----:|:----:|------|
| 深度 Memoryless | 极小 | 低 | RT action 配置 |
| Programmable Blending 后处理 | 中 | 中 | .usf FrameBufferFetch 宏 + PSO |
| 合并 RenderCommandEncoder | 中大 | 中 | 改 MobileShadingRenderer + MetalRHI |
| Imageblock/Tile Shading | 大 | 高 | 全新 tile pipeline，A11+ 才支持 |

> ✅ **跨平台红利**：因为 UE 是 RHI 抽象，**第 4 节 (1)(2)(3) 的 Renderer 层改造与 Android 共用同一套代码**——你为 Android One Pass 写的 `MobileShadingRenderer` 改动，iOS 上自动生效，只需分别验证 VulkanRHI / MetalRHI 的翻译正确性。只有 (4) Imageblock 是 iOS 独有，需单独写。

---

## 5. iOS vs Android 实现差异对照（精华）

| 维度 | Android（Vulkan/GLES） | iOS（Metal） |
|------|------------------------|--------------|
| 片上读当前像素 | subpass input attachment / FBF 扩展（需检测支持） | Programmable Blending（原生，全系支持） |
| 中间 RT 消除 | `DontStore` → 部分 driver 支持 memoryless | `MTLStorageModeMemoryless`（全系，确定性强） |
| 片上 GBuffer | subpass + input attachment | Programmable Blending + Memoryless（更简洁） |
| Opaque 遮挡剔除 | 需手动 PrePass/Early-Z | **HSR 硬件完成，Opaque 免 PrePass** |
| Pass 合并机制 | `vkCmdNextSubpass` | 同一 `MTLRenderCommandEncoder` 多 draw |
| 跨 tile compute | ❌ 无 | ✅ Tile Shading（A11+，独有） |
| 自定义片上结构 | ❌ 无 | ✅ Imageblock（A11+，独有） |
| 设备碎片化 | 高（需大量 capability 检测 + fallback） | 低（Apple GPU 统一，A11+ 覆盖绝大多数在用设备） |
| MSAA resolve | tile 内 resolve（需配置） | tile 内 resolve（Memoryless MSAA 纹理，零写回） |

**一句话**：**iOS/Metal 在 TBDR 优化上比 Android 更省心、更强大、碎片化更低。** 同样的优化目标，iOS 实现路径更短、收益更确定，还多了 Imageblock/Tile Shading 两张 Android 没有的王牌。

---

## 6. iOS 落地配置速查

```ini
;==================================================================
; DefaultEngine.ini —— iOS/Metal TBDR 优化
;==================================================================
[/Script/Engine.RendererSettings]

; ---- 路线选择 ----
; 燕云路线：片上 GBuffer（iOS 全系支持 Memoryless，强烈推荐）
r.Mobile.ShadingPath=1
r.MobileHDR=True

; 或 和平精英路线：Forward
; r.Mobile.ShadingPath=0

; ---- iOS Early-Z：只对 Masked 做，Opaque 交给 HSR ----
r.EarlyZPass=1
r.EarlyZPassOnlyMaterialMasking=1

; ---- Metal 桌面渲染器（高端 iOS，可选，开销大）----
; 项目设置 → Platforms → iOS → Rendering → Metal Desktop Renderer

; ---- Tonemap Subpass（需 Meta XR 插件；与 Deferred 互斥）----
; r.Mobile.TonemapSubpass=1

[/Script/IOSRuntimeSettings.IOSRuntimeSettings]
; 最低 Metal 版本（确保 Programmable Blending / Memoryless 可用）
MinimumiOSVersion=IOS_15
; A11+ 特性（Imageblock/Tile Shading）需运行时检测 MTLGPUFamilyApple4
```

```cpp
// 运行时检测 A11+ 进阶能力（Imageblock/Tile Shading/ROG）
const bool bSupportsImageblock =
    [MTLDevice supportsFamily:MTLGPUFamilyApple4];
if (bSupportsImageblock)
    UseTileShadingLightCulling();   // 高端档位
else
    UseStandardDeferred();          // 标准片上 GBuffer 兜底
```

---

## 7. iOS 落地 Checklist

### 配置层（零改造）
- [ ] `r.Mobile.ShadingPath=1` 开片上 GBuffer（iOS 全系支持 Memoryless）。
- [ ] `r.EarlyZPassOnlyMaterialMasking=1`：Opaque 交给 HSR，不做软件 PrePass。
- [ ] 深度等中间 RT 确认走 `DontStore`（MetalRHI 自动 Memoryless）。
- [ ] MSAA 纹理设 Memoryless（tile 内 resolve，零写回）。

### 源码层（需改造时）
- [ ] `.usf` 用跨平台 FrameBufferFetch 宏（Metal 后端→`[[color(n)]]`）。
- [ ] 合并 `MTLRenderCommandEncoder`：让多 Pass 复用同一 encoder。
- [ ] BasePass 写 RGB10A2 A 通道标记角色（替代 Custom Depth）。
- [ ] A11+ 进阶：Imageblock 自定义 GBuffer + Tile Shading 光源裁剪（需 `MTLTileRenderPipelineDescriptor`）。

### 验证工具
- [ ] **Xcode GPU Frame Capture**：逐 encoder 检查 Load/Store action、确认中间 RT 是 Memoryless（不占 device memory）。
- [ ] **Metal System Trace（Instruments）**：测带宽、tile 利用率、Hidden Surface Removal 效率。
- [ ] **Xcode GPU Counters**：看 fragment shader 调用数（验证 HSR 剔除效果）。
- [ ] `stat RHI` / `stat GPU`：对照各 encoder 耗时。

---

## 8. 参考资料

- Apple：Tailor your apps for Apple GPUs and TBDR（imageblock/tile shading/raster order group）：https://apple-docs.everest.mt/docs/metal/tailor-your-apps-for-apple-gpus-and-tile-based-deferred-rendering
- Apple WWDC2020：Harnessing Apple GPUs with Metal（Memoryless / Programmable Blending / Deferred）：https://developer.apple.com/cn/videos/play/wwdc2020/10602/
- Apple Tech Talk 602：Metal 2 on A11 Overview（imageblock/tile shading 发布）：https://developers.apple.com/videos/play/tech-talks/602
- Apple：Implementing order-independent transparency with image blocks：http://docs.developer.apple.com/documentation/Metal/implementing-order-independent-transparency-with-image-blocks
- UE 官方：Mobile Deferred Shading Mode（GBuffer 驻留 tile memory）：https://docs.unrealengine.com/documentation/en-us/unreal-engine/using-the-mobile-deferred-shading-mode-in-unreal-engine
- 配套文档：`UE_Mobile_TBDR_Optimization_TechDoc.md`、`UE_Mobile_TBDR_改造决策文档.md`

---

> **免责声明**：本文 Metal 特性基于 Apple A11+ GPU（MTLGPUFamilyApple4+）与 UE5.3+ Metal RHI。Programmable Blending / Memoryless 全系 Apple GPU 支持；Imageblock / Tile Shading / Raster Order Group 需 A11 及以上。落地前请用 Xcode GPU Frame Capture 在目标机型实测验证 Memoryless 与 encoder 合并的实际带宽收益。
