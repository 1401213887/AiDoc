# UE Mobile TBDR 优化技术：是否需要改造引擎？落地决策文档

> **配套文档**：`UE_Mobile_TBDR_Optimization_TechDoc.md`
> **结论先行**：三款手游对应的三类技术，在**当前 Stock UE5（5.3+）** 下的改造需求差异极大——
> 和平精英路线 **0 改造（纯配置）**，燕云片上 GBuffer 路线 **0~小改造（官方已内建）**，洛克王国 One Pass 路线 **需要源码改造（部分可用官方/插件能力替代）**。
> **引擎基准**：UE5.3 / 5.4 / 5.5 Vulkan RHI | 版本：v1.0 | 2026-06-20

---

## 0. 一页纸总结（TL;DR）

| 技术路线 | 对应案例 | 是否需改引擎 | 落地方式 |
|---------|---------|:----:|---------|
| **Forward + 重剔除 + 分档** | 和平精英 | ❌ **不需要** | 纯配置 + 资产/材质规范 + DeviceProfile |
| **片上 GBuffer（Mobile Deferred）** | 燕云十六声 | ⚠️ **基本不需要** | 官方已内建 `r.Mobile.ShadingPath=1`，GBuffer 自动驻留 tile memory；仅扩展通道/自定义光照模型才需改源码 |
| **Tonemap/后处理 Subpass** | 洛克王国 One Pass（部分） | ⚠️ **看范围** | 单纯 Tonemap 子通道：用官方 `r.Mobile.TonemapSubpass=1`（需 Meta XR 插件能力）；完整 One Pass：**需源码改造** |
| **完整 One Pass（PrePass→Tonemap 全程 1 RenderPass + Memoryless）** | 洛克王国 | ✅ **需要改造** | 修改 `MobileShadingRenderer` / RHI subpass 编排，自定义 RenderGraph |

**一句话**：**想白嫖官方能力，先用配置；想榨干最后 30% 写带宽（洛克王国级别），必须 fork 引擎改渲染器。**

---

## 1. 判断方法论：怎么知道一个优化要不要改引擎？

在动手前，用这三问快速分类：

```
Q1: 这个优化是否由 CVar / 项目设置 / 材质开关 暴露出来了？
    ├─ 是 → 大概率纯配置（去第 2 节）
    └─ 否 → Q2

Q2: 它是否只涉及"资产组织 / 材质写法 / Pass 顺序"，而不动 RHI 的 RenderPass 编排？
    ├─ 是 → 内容侧改造，不动引擎源码（资产规范 + 蓝图/材质）
    └─ 否 → Q3

Q3: 它是否需要新建/合并 Vulkan Subpass、改 Load/Store action、改 GBuffer 布局、
     改 MobileShadingRenderer 的 Pass 流程？
    └─ 是 → 必须 fork 引擎改源码（去第 4 节）
```

**关键认知**：TBDR 优化的"配置 vs 改造"分水岭，就在 **RenderPass / Subpass 的编排权**。
- 凡是官方已经把 subpass 编排"内建+开关化"的（Mobile Deferred 的片上 GBuffer、Tonemap Subpass）→ 配置即可。
- 凡是你想**自定义 subpass 的拆分/合并方式**（洛克王国把 5 个 Pass 自定义压成 1 个）→ 这是渲染器的核心流程，CVar 不会暴露，只能改源码。

---

## 2. 不需要改造的部分：怎么配置

### 2.1 和平精英路线（Forward + 重剔除）—— 100% 纯配置

这条路线**完全不碰引擎源码**，全靠配置 + 资产/材质规范 + 分档。

#### (1) 管线选型配置 — `DefaultEngine.ini`

```ini
[/Script/Engine.RendererSettings]
; 移动前向着色（默认即 0，显式写出以防被覆盖）
r.Mobile.ShadingPath=0
; 开启移动 HDR（按需，关掉可省带宽）
r.MobileHDR=True
; 静态+CSM 合并阴影，减少阴影 Pass
r.Mobile.EnableStaticAndCSMShadowReceivers=1
r.Mobile.EnableMovableSpotlightsShadow=0
```

#### (2) 剔除 / Early-Z 配置

```ini
[/Script/Engine.RendererSettings]
; 植被等 Masked 物体的深度预通过（减少 overdraw）
r.EarlyZPass=1                    ; 0=关 1=不透明 2=不透明+Masked 3=全部
r.EarlyZPassOnlyMaterialMasking=1 ; 只对 Masked 材质做 PrePass（和平精英策略）
; 遮挡剔除
r.AllowOcclusionQueries=1
r.Mobile.AllowSoftwareOcclusion=0 ; 高端机用硬件 OQ；低端可开软件遮挡
```

> ⚠️ 注意：UE5 移除了 UE4 的软件遮挡（`r.Mobile.AllowSoftwareOcclusion` 在 UE5 已无效，改用硬件 OQ；Meta 的 Oculus-VR fork 才重新加回）。这是**版本差异坑点**。

#### (3) 阴影策略 — 项目设置 + 光照属性

- **CSM 级联数**：World Settings / Directional Light → `Dynamic Shadow Cascades` 设 2（和平精英策略）
- **混合烘焙**：场景静态物用 Lightmass 烘焙；仅角色+近景投实时阴影
- **逐物体禁影**：低矮植被/杂草 → Mesh 的 `Cast Shadow = false`

#### (4) 分档 — DeviceProfile（核心，无需改引擎）

`Config/DefaultDeviceProfiles.ini`：

```ini
[Android_Low DeviceProfile]
DeviceType=Android
BaseProfileName=Android
+CVars=r.MobileContentScaleFactor=0.7   ; 渲染分辨率缩放
+CVars=r.ViewDistanceScale=0.5
+CVars=foliage.DensityScale=0.4
+CVars=r.Shadow.MaxResolution=512
+CVars=sg.ShadowQuality=0
+CVars=sg.PostProcessQuality=0          ; 低档几乎关后处理

[Android_High DeviceProfile]
DeviceType=Android
BaseProfileName=Android
+CVars=r.MobileContentScaleFactor=1.0
+CVars=r.ViewDistanceScale=1.0
+CVars=foliage.DensityScale=1.0
+CVars=sg.PostProcessQuality=2
```

配合 Scalability（`sg.*`）和 `r.MobileContentScaleFactor` 做动态分辨率/画质分级。

#### (5) 资产侧规范（内容改造，不动引擎）

- HLOD：World Partition HLOD / 传统 HLOD，500m 外建筑切代理网格
- 植被远景 Billboard：Foliage LOD 最后一级用 2 面片
- 远地形单 Draw Call Mesh 替代精细 Landscape
- 材质 LOD：远物用 `Quality Switch` 节点切极简版本

> ✅ **结论**：和平精英全套，Stock UE5 **零源码改造**，是配置 + 资产规范 + DeviceProfile 的工程活。

---

### 2.2 燕云十六声路线（片上 GBuffer）—— 官方已内建，基本不需改造

**重大利好**：UE5 的 **Mobile Deferred Shading** 官方实现，**已经把 GBuffer 放进 tile memory，永不写回主显存**。官方文档原文：

> "the mobile deferred shading model puts GBuffer in tile memory inside the GPU, meaning that the GBuffer is never stored in system memory. It also does not allocate memory when a device supports the LAZILY_ALLOCATED memory type."

这正是第 4 节技术文档讲的"片上 GBuffer"——**Epic 已经替你用 Vulkan Subpass + Input Attachment + Memoryless RT 实现好了。**

#### 开启配置 — `DefaultEngine.ini`

```ini
[/Script/Engine.RendererSettings]
r.Mobile.ShadingPath=1     ; 1 = Mobile Deferred（片上 GBuffer）
r.MobileHDR=True           ; Deferred 强制要求 Mobile HDR
; 可选：移动延迟下的聚类反射
r.Mobile.Forward.EnableClusteredReflections=1
```
或：项目设置 → Engine - Rendering → Mobile → **Mobile Shading = Deferred Shading**，重启编辑器。

#### 官方实现自动带来的收益（无需你写代码）

| 收益项 | 数据（官方） |
|--------|-------------|
| 材质指令数 | 147 → **34** 条 |
| 采样器数 | 2 → **0** 个 |
| GBuffer 写回主存 | **0**（支持 memoryless 的设备） |
| CPU/RHI 线程负担 | 显著降低（不绑阴影/反射纹理） |

#### 什么情况下燕云路线才需要改源码？

只有以下**超出官方默认 GBuffer 设计**的需求才需 fork：

1. **扩展 GBuffer 通道**：要往片上 GBuffer 塞自定义数据（如各向异性、自定义 ID）→ 改 `MobileGBuffer` 布局，注意 ≤128bit/像素、≤4 Input Attachment 硬约束。
2. **自定义移动延迟光照模型**：官方移动 Deferred 仅 DefaultLit/Unlit；要支持各向异性/SSS/布料 → 改 LightingPass shader + subpass。
3. **自研引擎的 Frame Graph 能力**：燕云用网易 Messiah 的 Frame Graph 自动管理瞬态资源/自动合并 subpass。UE 的等价物是 **RDG（Render Dependency Graph）**，UE5 移动端已全面 RDG 化，瞬态资源 + subpass 合并能力具备，但若要做 Messiah 那种"全自动 subpass 编排"，需在 RDG 层做深度定制。

> ✅ **结论**：燕云片上 GBuffer 的**核心能力**，Stock UE5 `r.Mobile.ShadingPath=1` 一行配置即得。**仅当你要扩展 GBuffer/自定义光照模型时才需源码改造。**

---

### 2.3 洛克王国 One Pass 的"可配置子集"—— Tonemap Subpass

洛克王国 One Pass 有一部分（**Tonemap / Color Grading / Vignette 的 subpass 化**）官方已经做成开关：

```ini
[/Script/Engine.RendererSettings]
r.Mobile.TonemapSubpass=1   ; 用 Vulkan subpass 做 tonemap，省一次全屏 Pass 的 load/store
```

**约束（重要）**：
- 此能力来自 **Meta XR 插件 / Oculus-VR fork**，标准 Epic 版需装 Meta XR 插件才有。
- 仅支持 **Color Grading（含 LUT）、Cinematic Tonemap、Vignette** 这三类逐像素后处理。
- ⚠️ **与 Mobile Deferred 不兼容**：开了 `r.Mobile.ShadingPath=1` 后，Tonemap Subpass 会被自动禁用。
- 收益参考：Meta 实测 tonemap 仅增约 600μs（对比走独立 Pass 大幅省带宽）。

> ⚠️ **结论**：One Pass 中"逐像素后处理 subpass 化"这一小块，可用官方 `r.Mobile.TonemapSubpass` 部分替代，**但能力范围远小于洛克王国的完整 One Pass**。

---

## 3. 配置能力边界：官方给到哪、给不到哪

| 洛克王国 One Pass 的子技术 | Stock UE5 是否内建 | 落地方式 |
|---------------------------|:------:|---------|
| Tonemap/ColorGrading/Vignette subpass 化 | ✅（需 Meta XR 插件） | `r.Mobile.TonemapSubpass=1` |
| 片上 GBuffer（延迟） | ✅ | `r.Mobile.ShadingPath=1` |
| 半透明读片上深度（DepthRead subpass） | ✅ | 引擎默认行为（`ESubpassHint::DepthReadSubpass`） |
| **Distortion 合并进 Bloom/Tonemap** | ❌ | 需改 PostProcess 流程 |
| **RGB10A2 Stencil 替代 Custom Depth 区分角色** | ❌ | 需改 BasePass 输出 + 后续 subpass 读取 |
| **PrePass→Tonemap 全程 1 RenderPass** | ❌ | 需重写 MobileShadingRenderer Pass 编排 |
| **整条管线深度 RT 设 Memoryless** | ❌（部分自动） | 需改 RenderTarget action |

**分水岭清晰**：官方把"标准化、通用化"的 subpass 合并做成了开关；但洛克王国那种"**针对自己游戏特性，自定义把哪些 Pass 合进一个 RenderPass、自定义 RT 的 Load/Store/Memoryless**"——这是渲染器流程的深度定制，**CVar 永远不会暴露，只能改源码**。

---

## 4. 需要改造的部分：怎么改

### 4.1 改造范围概览（完整 One Pass）

要复刻洛克王国级别的完整 One Pass，需要 fork 引擎，改动集中在 **Renderer 模块**：

```
Engine/Source/Runtime/Renderer/Private/
├── MobileShadingRenderer.cpp        ★ 主战场：Render() / RenderForward() 的 Pass 编排
├── MobileBasePassRendering.cpp/.h   ★ BasePass 输出 RGB10A2 Stencil（角色 Alpha=1）
├── PostProcess/
│   ├── PostProcessMobile.cpp        ★ 后处理 subpass 合并、Distortion 合入 Bloom/Tonemap
│   └── PostProcessing.cpp
└── (RHI 层)
    Engine/Source/Runtime/VulkanRHI/Private/
    ├── VulkanRenderPass.cpp         ★ Subpass 描述、Input Attachment、Load/Store action
    └── VulkanPipeline.cpp           ★ subpassLoad 对应的 PSO permutation（MSAA 两套）
```

### 4.2 改造步骤（工程化路线）

#### Step 1：建立可控的 RenderPass 编排点

在 `MobileShadingRenderer::RenderForward()` 中，把原本分散的多个 `BeginRenderPass/EndRenderPass` 收敛为**一个 RenderPass + 多 Subpass**：

```cpp
// 伪代码：自定义 One Pass 编排
FRHIRenderPassInfo OnePassInfo(
    SceneColor,
    ERenderTargetActions::Clear_Store,        // 仅最终 SceneColor 写回
    SceneDepth,
    EDepthStencilTargetActions::ClearDepthStencil_DontStoreDepthStencil, // 深度 Memoryless
    FExclusiveDepthStencil::DepthWrite_StencilWrite
);
OnePassInfo.SubpassHint = ESubpassHint::CustomOnePassSubpass;  // ★ 新增自定义 hint

RHICmdList.BeginRenderPass(OnePassInfo, TEXT("MobileOnePass"));
    RenderMobileBasePass(...);          // subpass 0：BasePass + RGB10A2 角色 Alpha
    RHICmdList.NextSubpass();
    RenderTranslucencyAndDecals(...);   // subpass 1：半透明/贴花，读片上深度
    RHICmdList.NextSubpass();
    RenderMergedPostProcess(...);       // subpass 2：Distortion+Bloom+Tonemap 合并，FrameBufferFetch
RHICmdList.EndRenderPass();
```

#### Step 2：扩展 `ESubpassHint` 并在 VulkanRHI 落地

在 `RHIResources.h` 增加自定义 hint，在 `VulkanRenderPass.cpp` 的 `FVulkanRenderPass` 构造里描述对应的 subpass dependency 与 input attachment：

```cpp
// RHIResources.h
enum class ESubpassHint : uint8
{
    None,
    DepthReadSubpass,
    DeferredShadingSubpass,
    CustomOnePassSubpass,   // ★ 新增
};

// VulkanRenderPass.cpp：为 CustomOnePassSubpass 配置
// - subpass 0 输出 color/depth
// - subpass 1/2 把前序 attachment 声明为 VK_ATTACHMENT_REFERENCE 的 input attachment
// - 配置 VkSubpassDependency 的 BY_REGION_BIT（保证 tile 局部性）
```

#### Step 3：BasePass 写 RGB10A2 Stencil 替代 Custom Depth

改 `MobileBasePassPixelShader.usf`：在 BasePass 输出时，把角色标记写入 SceneColor 的 A 通道（RGB10A2 格式的 2bit alpha）：

```hlsl
// MobileBasePassPixelShader.usf
OutColor.a = bIsCharacter ? 1.0 : 0.0;   // 后续 subpass 用此区分角色/场景
```
后续 subpass 用 `subpassLoad(SceneColorInput).a` 读取，**省掉 Custom Depth 的独立 Pass（约 3.5MB 带宽 + 可达 100~200 DC）**。

#### Step 4：后处理合并 + FrameBuffer Fetch

改 `PostProcessMobile.cpp`：把 Distortion 累加合入 Bloom/Tonemap，所有逐像素后处理改用 `subpassLoad` / FrameBuffer Fetch 取当前像素，不再采样 `PostProcessInput`（避免打断 RenderPass）。

#### Step 5：降级路径（必做）

不是所有设备都支持 subpass input attachment / FrameBuffer Fetch（尤其老 GLES 设备）。必须保留 capability 检测 + 走回标准多 Pass 的 fallback：

```cpp
const bool bSupportsOnePass =
    GVulkanSupportsInputAttachment &&
    GShaderPlatformSupportsFramebufferFetch[ShaderPlatform];
if (bSupportsOnePass)
    RenderForward_OnePass(...);
else
    RenderForward_Legacy(...);   // 原版多 Pass 兜底
```

### 4.3 改造工程量与风险评估

| 维度 | 评估 |
|------|------|
| 改造范围 | Renderer + VulkanRHI 两个核心模块，中等偏大 |
| 技术风险 | ⚠️ 高：subpass dependency 配错会导致 tile 行为错误/画面错误/驱动崩溃 |
| 设备兼容 | ⚠️ 必须做 capability 检测 + fallback，覆盖 GLES/低端 Vulkan |
| MSAA | 需为开/关 MSAA 准备两套 PSO（`subpassLoad` 签名不同） |
| 引擎升级成本 | ⚠️ 高：fork 后每次 UE 大版本升级需重新 merge 渲染器改动 |
| 调试工具 | RenderDoc 真机抓帧逐 subpass 验证 Load/Store；Arm Streamline 测带宽 |

> ⚠️ **决策提醒**：fork 引擎改渲染器是**长期负债**——每次 UE 升版都要重新合并。除非你的项目真的卡在写带宽（像洛克王国那样要省最后 30%），否则**优先用官方 Mobile Deferred（片上 GBuffer）+ Tonemap Subpass 组合**拿到 80% 的收益，把源码改造留给真正的瓶颈。

---

## 5. 落地决策建议（按项目类型）

### 场景 A：大世界/竞技/广机型适配（类和平精英）
→ **不改引擎**。Forward + EarlyZ + 遮挡剔除 + HLOD + 烘焙阴影 + DeviceProfile 分档。纯配置 + 内容规范。

### 场景 B：高画质/多动态光/PBR（类燕云）
→ **基本不改引擎**。`r.Mobile.ShadingPath=1` 开 Mobile Deferred，GBuffer 自动驻留 tile memory。仅当要扩展 GBuffer 通道或自定义光照模型时才 fork。

### 场景 C：中小场景/重后处理/要榨干写带宽（类洛克王国）
→ **分两步走**：
1. **先白嫖**：`r.Mobile.TonemapSubpass=1`（Meta XR 插件）+ 半透明 DepthRead subpass（默认开）+ 资产侧减 Pass。
2. **再 fork**：若仍卡写带宽，改 `MobileShadingRenderer` + `VulkanRHI` 实现完整 One Pass（第 4 节），目标写带宽 -30%。

---

## 6. 配置速查表（可直接抄）

```ini
;==================================================================
; DefaultEngine.ini —— TBDR 优化配置
;==================================================================
[/Script/Engine.RendererSettings]

; ---- 路线二选一 ----
; A. Forward（和平精英路线，默认）
r.Mobile.ShadingPath=0
; B. Deferred 片上 GBuffer（燕云路线）—— 取消注释启用
; r.Mobile.ShadingPath=1
; r.MobileHDR=True

; ---- Early-Z / 剔除（Forward 路线用）----
r.EarlyZPass=1
r.EarlyZPassOnlyMaterialMasking=1
r.AllowOcclusionQueries=1

; ---- 阴影 ----
r.Mobile.EnableStaticAndCSMShadowReceivers=1
r.Shadow.CSM.MaxMobileCascades=2

; ---- Tonemap Subpass（洛克王国 One Pass 子集，需 Meta XR 插件，且与 Deferred 互斥）----
r.Mobile.TonemapSubpass=1

; ---- 反射 ----
r.Mobile.Forward.EnableClusteredReflections=1
```

---

## 7. 参考资料

- UE 官方：Mobile Rendering and Shading Modes：https://dev.epicgames.com/documentation/zh-cn/unreal-engine/mobile-rendering-and-shading-modes-for-unreal-engine
- UE 官方：Mobile Deferred Shading Mode（GBuffer 驻留 tile memory 原文）：https://docs.unrealengine.com/documentation/en-us/unreal-engine/using-the-mobile-deferred-shading-mode-in-unreal-engine
- UE 官方：Mobile Feature Levels and Rendering Modes：https://docs.unrealengine.com/latest/zh-CN/SharingAndReleasing/Mobile/RenderingModes
- Meta：UE 中的色调映射（`r.Mobile.TonemapSubpass`）：https://developers.meta.com/horizon/documentation/unreal/unreal-tonemapping/
- CVar Wiki：`r.Mobile.ShadingPath`：https://indxzero.github.io/ue544cvarwiki/articles/r.mobile.shadingpath/
- 配套技术文档：`UE_Mobile_TBDR_Optimization_TechDoc.md`

---

> **免责声明**：本文 CVar/项目设置基于 UE5.3~5.5 验证，不同版本可能有差异，落地前请在目标版本确认。源码改造路线为架构性方案，具体 subpass dependency 配置需结合目标 GPU（Mali/Adreno）的 tile 行为在真机用 RenderDoc 验证。
