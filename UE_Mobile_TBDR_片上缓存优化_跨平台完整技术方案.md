# Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案

> **文档性质**：主技术文档（Master Doc），整合并迭代优化三份前置成果
> **覆盖范围**：TBDR 原理 → Vulkan(Android) 实现 → 三款手游案例 → 是否需改引擎决策 → iOS(Metal) 实现
> **引擎基准**：UE4.22+ / UE5.3+（Vulkan RHI + Metal RHI）
> **案例**：洛克王国·世界（One Pass）、燕云十六声（片上 GBuffer）、和平精英（Forward 重剔除）
> **版本**：v2.0（10 轮迭代优化稿）| 2026-06-20

---

> **本文档配套增补卷**：`UE_Mobile_TBDR_查漏补缺增补卷.md`（100 点审阅、真实 .usf 源码、RGB10A2 因果修正、PrePass×Subpass 冲突、带宽估算公式、版本矩阵、术语表）。
> **知识库导航**：与既有手游 html 的交叉引用见 [§12 知识库导航](#12-知识库导航eaidoc-内部交叉引用)；全库总入口见同目录 `知识库导航_README.md`。

---

## 文档迭代说明（10 轮优化维度）

本文档由三份前置文档（TBDR 优化技术文档 / 改造决策文档 / iOS Metal 实现方案）合并迭代而成，优化维度：

1. **结构合并**：三文档收敛为单一主线（原理→平台→案例→决策→跨平台）
2. **去重**：消除三份文档间重复的 TBDR 原理、CVar 表
3. **事实核验**：交叉验证燕云=网易 Messiah 自研引擎（非 UE5）
4. **数据补强**：注入燕云真实 GBuffer 布局（B8G8R8A8+R10、~20 byte/px、~1MB tile SRAM、Octahedron/YCoCg）
5. **跨平台并列**：Vulkan 与 Metal 机制改为左右对照表
6. **术语统一**：Tile Memory / 片上缓存 / Memoryless 等术语全文一致
7. **决策前置**：增加执行摘要 + 一页决策矩阵
8. **代码精炼**：RHI / Shader 代码片段去冗余、标注平台
9. **配置附录**：抽出可直接复制的 ini 配置速查
10. **一致性终检**：引用来源、版本号、免责声明统一

---

## 0. 执行摘要（一页纸）

**核心命题**：移动 GPU 是 TBDR 架构，渲染在片上缓存（Tile Memory）完成。**所有优化的终极目标只有一句话——让中间结果留在片上，不要落主显存（省写带宽 = 省功耗、省发热、提帧率）。**

### 三款手游 = 三条技术路线 = 三种改造成本

| 案例 | 引擎 | 技术路线 | 核心手段 | Stock UE5 改造成本 |
|------|------|---------|---------|:------:|
| **和平精英** | UE4 | Forward + 重剔除 | EarlyZ/遮挡/HLOD/烘焙阴影/分档 | ❌ **零改造**（纯配置+资产） |
| **燕云十六声** | Messiah(自研) | 片上 GBuffer（延迟） | Subpass+Memoryless，GBuffer 不落主存 | ⚠️ **基本零改造**（`r.Mobile.ShadingPath=1`） |
| **洛克王国·世界** | UE4.26 | One Pass（前向改造） | FrameBufferFetch 合并 Pass + Memoryless 深度 | ✅ **需 fork**（改渲染器） |

### 平台差异速判

- **Android(Vulkan)**：靠 Subpass + Input Attachment + FrameBufferFetch 扩展拼片上复用，需做设备 capability 检测 + fallback。
- **iOS(Metal)**：TBDR 天选平台。Programmable Blending 原生读片上、Memoryless 一个标志位、HSR 硬件遮挡免 PrePass，还独享 Imageblock/Tile Shading。**同方案 iOS 更省心、收益更确定。**

---

## 1. TBDR 架构与带宽问题（共同基础）

### 1.1 IMR vs TBDR

```
桌面 IMR：    每个三角形 → 直接读写主显存的 FrameBuffer
移动 TBDR：   屏幕切 Tile(16x16/32x32) → 每个 Tile 全程在片上 SRAM 计算
              → Tile 完成后才一次性 store 回主显存
```

| 厂商 | 架构 | 特点 |
|------|------|------|
| Apple A 系列 | 真 TBDR | 硬件 HSR 隐面剔除（着色前剔除遮挡片元） |
| ARM Mali | TBR | 软件需手动 PrePass 辅助 Early-Z |
| Qualcomm Adreno | TBR(FlexRender) | 可切 tiled/direct 模式 |
| Imagination PowerVR | 真 TBDR | HSR 始祖 |

### 1.2 带宽是头号敌人

移动 GPU 瓶颈极少在 ALU，**绝大多数卡在带宽与其引发的发热降频**。Tile Memory ↔ 主显存的搬运代价：

| 操作 | 含义 | 代价 |
|------|------|------|
| **Load** | Pass 开始把主存 RT 读进片上 | 读带宽 |
| **Store** | Pass 结束把片上结果写回主存 | **写带宽（最贵）** |
| **Clear** | 片上直接清空 | 几乎免费 |
| **Discard / DontStore** | 渲染完不写回（Memoryless） | 省下写带宽 |

> **核心矛盾**：UE 默认把一帧拆成多个独立 RenderPass（阴影→BasePass→半透明→后处理）。每个 Pass 边界 = 一次 Store + 下个 Pass 一次 Load。中间结果在主存里"出差一圈又回来"，产生冗余带宽。

### 1.3 优化总纲领（优先级排序）

1. **消除 Store**：只在本 Pass 用的 RT 标 `DontStore`/Memoryless（深度、GBuffer、中间结果）。
2. **合并 Pass**：用 Subpass(Vulkan) / 同一 Encoder(Metal) 把"逐像素消费上阶段结果"的 Pass 并进同一 RenderPass。
3. **减少工作量**：Early-Z、遮挡剔除、LOD/HLOD 降低进入管线的几何/像素。
4. **慎用全屏后处理**：邻域采样的 Bloom/SSAO/SSR 无法 subpass 合并，是带宽大户。

---

## 2. 引擎机制：Vulkan 与 Metal 双平台对照

**关键前提**：UE 渲染代码是 **RHI 抽象**的。你在 `MobileShadingRenderer` 写的 `FRHIRenderPassInfo`/`SubpassHint`/RenderTarget action，同一套代码被 VulkanRHI 译成 `VkRenderPass`+subpass，被 MetalRHI 译成 `MTLRenderPassDescriptor`+Programmable Blending。**Renderer 层改一次，双端生效。**

### 2.1 机制映射总表

| 能力 | Vulkan(Android) | Metal(iOS) | UE RHI 内建 |
|------|-----------------|------------|:------:|
| 片上读当前像素 | Subpass Input Attachment / FBF 扩展（需检测） | **Programmable Blending**（`[[color(n)]]`，原生） | ✅ 自动转译 |
| 消除中间 RT | `DontStore`（部分驱动 memoryless） | **`MTLStorageModeMemoryless`**（全系） | ✅ 自动转译 |
| 片上 GBuffer | Subpass 0→1 + input attachment | Programmable Blending + Memoryless | ✅ `r.Mobile.ShadingPath=1` |
| Pass 合并机制 | `vkCmdNextSubpass` | 同一 `MTLRenderCommandEncoder` 多 draw | ✅/⚠️ |
| Opaque 遮挡剔除 | 需手动 PrePass | **HSR 硬件完成，免 PrePass** | — |
| 跨像素 compute | ❌ 无 | ✅ **Tile Shading**（A11+） | ⚠️ 需改源码 |
| 自定义片上结构 | ❌ 无 | ✅ **Imageblock**（A11+） | ⚠️ 需改源码 |

### 2.2 RenderTarget Action（双端通用语义）

| UE Action | Vulkan 行为 | Metal LoadAction/StoreAction | 用途 |
|-----------|------------|------------------------------|------|
| `Clear_Store` | 片上清→渲染→写回 | Clear / Store | 最终颜色 |
| `Load_Store` | 读主存→渲染→写回 | Load / Store | 叠加已有内容 |
| `Clear_DontStore` | 片上清→渲染→丢弃 | Clear / DontCare | 只在本 Pass 用的深度 |
| `DontLoad_DontStore` | Memoryless | DontCare / DontCare | 片上 GBuffer/中间 RT |

### 2.3 显式 RenderPass + Subpass（Vulkan，UE4.22 重构）

```cpp
// 【Vulkan/通用 RHI】显式声明 RenderPass + Load/Store + Subpass 提示
FRHIRenderPassInfo RPInfo(
    SceneColor,  ERenderTargetActions::Clear_Store,            // 颜色：清→渲染→写回
    SceneDepth,  EDepthStencilTargetActions::ClearDepthStencil_DontStoreDepthStencil, // 深度 Memoryless
    FExclusiveDepthStencil::DepthWrite_StencilWrite
);
RPInfo.SubpassHint = ESubpassHint::DepthReadSubpass;   // 提示后续 subpass 读深度

RHICmdList.BeginRenderPass(RPInfo, TEXT("MobileSceneColor"));
//   subpass 0：不透明 BasePass
RHICmdList.NextSubpass();   // → Vulkan: vkCmdNextSubpass / GLES: FramebufferFetchBarrierQCOM
//   subpass 1：贴花/半透明，直接读片上深度
RHICmdList.EndRenderPass();
```

### 2.4 Programmable Blending（Metal，原生读片上）

```metal
// 【Metal】fragment 用 color attachment 入参直接读当前像素片上值，无需采样、无需扩展
fragment half4 LightingPass(
    RasterizerData in [[stage_in]],
    GBufferData gbuffer [[color(0)]])   // color(0..n) = 片上当前像素
{
    return half4(ComputeLighting(gbuffer.albedo, gbuffer.normal, gbuffer.roughness), 1.0);
}
```

> **黄金约束（双端通用）**：Subpass Input Attachment 与 Programmable Blending **都只能读当前像素，不能读邻域**。
> - ✅ 可合并：Tonemap、Color Grading、Fog、Distortion 累加、贴花、延迟逐像素光照
> - ❌ 不可合并：高斯模糊、Bloom 降采样、SSAO、SSR（需邻域采样，必须独立 Pass + 落主存）

### 2.5 相关 CVar 速查

| CVar | 默认 | 作用 |
|------|:----:|------|
| `r.Mobile.ShadingPath` | 0 | 0=Forward，1=Deferred（片上 GBuffer） |
| `r.MobileHDR` | True | Deferred 强制要求 |
| `r.Mobile.TonemapSubpass` | 0 | Tonemap/ColorGrading/Vignette 子通道（需 Meta XR 插件，与 Deferred 互斥） |
| `r.EarlyZPass` | — | 0关/1不透明/2+Masked/3全部 |
| `r.EarlyZPassOnlyMaterialMasking` | — | 只对 Masked 做 PrePass（iOS 强烈建议=1） |
| `r.Mobile.Forward.EnableClusteredReflections` | — | 移动前向聚类反射 |

---

## 3. 案例一：洛克王国·世界 One Pass（前向改造）

> 来源：UFSH2025《洛克王国：世界》移动端管线设计与优化（朱谷才，腾讯魔方）。UE4.26 Mobile Forward。下列百分比为 iPhone X 实测。

### 3.1 问题
低端机（iPhone 8 Plus）50+ 角色同屏 + 新增 SSR/Color Grading/TOD/动态点光。UE 默认管线为此拆成 **5 个 RenderPass，产生 4 次中间结果写回**。打断 Pass 的元凶：Distortion 合并 + 后处理材质。

### 3.2 实现（FrameBufferFetch + Depth Fetch 收敛为单 RenderPass 多 Subpass）

| 步骤 | 做法 |
|------|------|
| ① Subpass 化后处理材质 | 后处理不采样邻域 → FrameBufferFetch 取当前像素，整段后处理在片上算完才写回 |
| ② Distortion 合并 | 去掉独立 Distortion Pass，并入本就采样 Stencil 的 Bloom/Tonemap |
| ③ RGB10A2 通道分配区分角色 | 深度走 **subpass depth fetch 全程留 tile（Memoryless，从不写回任何地方）**；正因深度不再占 SceneColor.A，RGB10A2 的 2-bit alpha 才腾出来当角色/场景 mask（RGB 10/10/10 保颜色精度），**替代 Custom Depth**（省约 3.5MB 带宽、DC 可达 100~200）。⚠️ alpha 与深度无关，详见增补卷 §3 |
| ④ Deferred HDR 终极管线 | 剔除需邻域的 SSAO/SSR/Bloom，保留 Fog/ColorGrading/Tonemap，**PrePass→Tonemap 全程 1 个 RenderPass，深度全程 Memoryless** |

### 3.3 量化收益（iPhone X 实测）

| 优化项 | 收益 |
|--------|------|
| Subpass 后处理（极限场景） | 带宽 **-23%**、GPU **-31.6%** |
| Subpass 后处理（实际场景） | 带宽 **-12.8%**、读带宽 **-16.5%** |
| Distortion 合并 | GPU **-13.8%**、读带宽 **~-30%** |
| Deferred HDR | 真实帧 **+3%**、读带宽 **-15%**、**写带宽 -30%（最大收益）** |

> 📎 **延伸阅读（知识库内）**：本案例的"前向 + 片上 channel repack"在 FPS 横向对比中的位置，见 [FPS手游技术全景对比.html](./FPS手游技术全景对比.html)（"片上做 channel repack"段）。Subpass 折射代替 GrabPass 的半透明做法，见 [头部手游半透明渲染方案汇总.html](./头部手游半透明渲染方案汇总.html)（"Subpass 折射"段）。配套源码级修正见 [增补卷 §3](./UE_Mobile_TBDR_查漏补缺增补卷.md)（RGB10A2 因果）。

---

## 4. 案例二：燕云十六声 片上 GBuffer（延迟）

> **事实核验**：燕云用网易**自研 Messiah 引擎**（非 UE5，已交叉验证）。本节以"片上 GBuffer 通用技术 + UE5 Mobile Deferred 同源范本"讲清原理，燕云真实 GBuffer 布局数据来自其公开技术要点。

### 4.1 问题：移动延迟的 GBuffer 带宽

```
错误做法（GBuffer 落主存）：
  BasePass:    写 GBuffer → [Store 主存]   ← 巨量写带宽
  LightingPass: [Load 主存] → 算光 → SceneColor  ← 巨量读带宽
```
1080p 下 4~5 张 GBuffer RT 每帧写回可达数十 MB，移动端扛不住。

### 4.2 实现：GBuffer 全程驻留 Tile Memory

```
RenderPass {
  Subpass 0 (BasePass):    GBuffer → Tile Memory（Memoryless/DontStore）
  Subpass 1 (Lighting):    subpassLoad/[[color(n)]] 读片上 GBuffer → 算光 → SceneColor
  → 只有 SceneColor 写回主存，GBuffer 永不落主存
}
```
燕云实际 Pass 序：**GBuffer → Lighting → Sky → Translucency**，全部组织在片上。

### 4.3 燕云真实 GBuffer 布局（公开数据）

| 项 | 数据 |
|----|------|
| GBuffer Format | **B8G8R8A8 + R10**（tile 直读） |
| 每像素 tile 占用 | **~20 byte/px** |
| Tile SRAM 容量 | **~1MB**，32×32 tile 下可容纳全 GBuffer+Depth+SceneColor（避免 tile spilling） |
| 法线压缩 | **Octahedron Normal**（八面体编码，2 通道） |
| 颜色压缩 | **YCoCg Albedo** |
| 关键洞察 | Tile memory 模式下 GBuffer 不进 DDR，**压缩节省的不是带宽（本就 0 写回），而是腾出 tile 空间避免 spilling** |

### 4.4 移动 GBuffer 硬约束

| 约束 | 典型值 |
|------|--------|
| 每像素 tile 预算 | ≤ 128 bit（Mali）；燕云用 ~20 byte 留余量 |
| Input Attachment 数 | ≤ 4（Vulkan） |
| 光照模型 | 移动 Deferred 通常仅 DefaultLit/Unlit |

### 4.5 UE5 同源实现 + 收益（官方）
`r.Mobile.ShadingPath=1` 即得。官方："GBuffer is never stored in system memory"。材质指令 **147→34**，采样器 **2→0**。

> Messiah 早于 UE 引入 **Frame Graph**（UE 等价物=RDG），自动管理瞬态资源 + 合并 subpass，是片上 GBuffer 的底层基础设施。

> 📎 **延伸阅读（知识库内）**：燕云完整技术要点（含 GBuffer 布局、Pass 序、片上 SRAM 容量原始数据）见 [燕云十六声移动端技术要点总结.html](./燕云十六声移动端技术要点总结.html)（"Deferred + Subpass""Mobile Deferred 管线深"段）。同走移动 Deferred + Subpass 的另一案例（含多档位退化路径）见 [三角洲移动端技术要点总结.html](./三角洲移动端技术要点总结.html)（"前向管线截帧详解（5 Pass）""Subpass 优化"段）。

---

## 5. 案例三：和平精英 Forward 重剔除

> 来源：Epic 官方光子访谈(2018)、第三方抓帧(2020)、光子 GDC 分享。UE4 Mobile Forward。

### 5.1 选型：Forward（大世界+远视距+多玩家+单主光）
- Deferred 在远视距高顶点负载下不划算，GBuffer 还占 tile 空间
- 场景以单方向光为主，Forward 逐物体按灯成本最低
- 兼容性最广 + 支持烘焙光照

### 5.2 带宽优化（有据可查）
- Shadow Depth 去掉 Color RT（减 tile store）
- 光影贴图采样 **3→1** 次（SDF 阴影塞 ETC2 alpha）
- 天空/地表最后渲染，吃满 Early-Z
- Masked 植被 Early Z-pass；Opaque 不做 PrePass

### 5.3 阴影/分档
- 2 级 CSM + Lightmass 烘焙混合；仅角色近景实时阴影
- UE Scalability + DeviceProfiler 按机型分档
- Foliage Scalability / Texture Streaming / FOV Culling / HLOD

### 5.4 量化（有据可查）
| 指标 | 数据 |
|------|------|
| Draw Call | 稳定 **<300**（139~337） |
| Overdraw | **2.49x~2.83x**（<3x） |
| 光影贴图采样 | 3→1 |
| Landscape 内存 | 比同复杂 Static Mesh 省 6~7 倍 |

> 📎 **延伸阅读（知识库内）**：和平精英完整技术要点（"Forward 是正解""带宽完全顶不住"论证）见 [和平精英移动端技术要点总结.html](./和平精英移动端技术要点总结.html)。同类 FPS 的 Forward 多档位策略见 [使命召唤手游移动端技术要点总结.html](./使命召唤手游移动端技术要点总结.html)、[暗区突围移动端技术要点总结.html](./暗区突围移动端技术要点总结.html)。其重剔除手段的专题汇总见 [头部手游移动端遮挡剔除方案汇总.html](./头部手游移动端遮挡剔除方案汇总.html) 与 [头部手游降低DrawCall方案汇总.html](./头部手游降低DrawCall方案汇总.html)。

---

## 6. 是否需要改造引擎：决策矩阵

### 6.1 判断方法论

```
Q1 该优化由 CVar/项目设置/材质开关 暴露了吗？  是→纯配置  否→Q2
Q2 只涉及资产/材质/Pass顺序，不动 RHI 编排吗？  是→内容侧  否→Q3
Q3 需新建/合并 Subpass、改 Load/Store、改 GBuffer 布局、改渲染器流程吗？  是→fork 改源码
```
**分水岭 = RenderPass/Subpass 的编排权。** 官方做成开关的（Mobile Deferred、TonemapSubpass）→ 配置；要自定义 Pass 拆分合并的（洛克王国完整 One Pass）→ 改源码。

### 6.2 决策矩阵

| 子技术 | Stock UE5 内建 | 落地方式 | 改引擎 |
|--------|:------:|---------|:------:|
| Forward + 重剔除（和平精英全套） | ✅ | 配置+DeviceProfile+资产 | ❌ |
| 片上 GBuffer（燕云核心） | ✅ | `r.Mobile.ShadingPath=1` | ❌ |
| 半透明读片上深度 | ✅ | 默认 `ESubpassHint::DepthReadSubpass` | ❌ |
| Tonemap/ColorGrading 子通道 | ✅ | `r.Mobile.TonemapSubpass=1`（需 Meta XR） | ❌ |
| 扩展 GBuffer 通道/自定义光照模型 | ❌ | 改 MobileGBuffer 布局 + LightingPass | ✅ |
| Distortion 合并进 Bloom/Tonemap | ❌ | 改 PostProcessMobile | ✅ |
| RGB10A2 替代 Custom Depth | ❌ | 改 BasePass 输出 + 后续 subpass | ✅ |
| PrePass→Tonemap 全程 1 RenderPass | ❌ | 重写 MobileShadingRenderer 编排 | ✅ |

### 6.3 源码改造路线（完整 One Pass）

**改造文件**：
```
Renderer/Private/MobileShadingRenderer.cpp   ★ Pass 编排（Android/iOS 共用）
Renderer/Private/MobileBasePassRendering.*   ★ RGB10A2 角色标记
Renderer/Private/PostProcess/PostProcessMobile.cpp ★ 后处理合并+FBF
VulkanRHI/Private/VulkanRenderPass.cpp       ★ subpass 描述/dependency
Apple/MetalRHI/Private/MetalRenderPass.cpp   ★ Memoryless/encoder 合并
Shaders/Private/MobileBasePassPixelShader.usf ★ 写 RGB10A2 A 通道
```

**5 步**：① 收敛多个 BeginRenderPass 为单 RenderPass 多 Subpass → ② 扩展 `ESubpassHint` 并在 RHI 落地 → ③ BasePass 写 RGB10A2 角色标记 → ④ 后处理改 FrameBufferFetch → ⑤ **必做 fallback**（capability 检测，不支持设备走传统多 Pass）。

**风险**：subpass dependency 配错致画面错误/崩溃；MSAA 需两套 PSO；fork 后每次 UE 升版需重新 merge（长期负债）。

---

## 7. iOS / Metal 专属实现

### 7.1 为什么 iOS 更省心
1. **Programmable Blending 原生**——无需检测 `EXT_shader_framebuffer_fetch`/`QCOM` 扩展，全系支持，不需要 fallback
2. **Memoryless 是一个 storage 标志位**——比 Vulkan 配 subpass dependency 简单
3. **HSR 硬件遮挡**——Opaque 不需要软件 PrePass（做了反而是负优化）

### 7.2 三方案 iOS 落地
- **燕云片上 GBuffer**：`r.Mobile.ShadingPath=1`，GBuffer 用 Memoryless+Programmable Blending，**比 Android 更干净（无需 input attachment 声明）**
- **洛克王国 One Pass**：实现成本**低于 Android**；仅"合并 `MTLRenderCommandEncoder`"需改 MetalRHI
- **和平精英重剔除**：API 无关，但 iOS 必须 `r.EarlyZPassOnlyMaterialMasking=1`（Opaque 交给 HSR）

### 7.3 Pass 合并：Metal 无 subpass 概念
```objc
// 【Metal】只要不结束 encoder、不切 RT 配置，中间结果就留 tile memory
id<MTLRenderCommandEncoder> enc = [cmdBuf renderCommandEncoderWithDescriptor:onePassDesc];
DrawBasePass(enc);              // 写 Memoryless GBuffer + RGB10A2 角色标记
DrawTranslucencyAndDecals(enc); // Programmable Blending 读片上深度
DrawMergedPostProcess(enc);     // Programmable Blending 读 SceneColor，合并后处理
[enc endEncoding];              // 此时才 store 最终 SceneColor
```

### 7.4 Apple 独有王牌：Imageblock + Tile Shading（A11+）
可在 render pass 内联 compute、跨像素访问整个 tile，实现洛克王国都没做的 **tile 光源裁剪 / OIT**。Vulkan subpass 无等价物，是 iOS 高端档位差异化优势。需 `MTLTileRenderPipelineDescriptor`，运行时检测 `MTLGPUFamilyApple4`。

---

## 8. 落地决策建议（按项目类型）

| 项目类型 | 推荐方案 | 改引擎 |
|---------|---------|:------:|
| 大世界/竞技/广机型（类和平精英） | Forward + EarlyZ + HLOD + 烘焙阴影 + DeviceProfile | ❌ |
| 高画质/多动态光/PBR（类燕云） | `r.Mobile.ShadingPath=1` 片上 GBuffer | ❌（扩展通道才需） |
| 中小场景/重后处理/榨写带宽（类洛克王国） | 先白嫖(TonemapSubpass+DepthRead)，再 fork 改 One Pass | ⚠️ 分两步 |

> **工程忠告**：fork 渲染器是长期负债。优先用「官方 Mobile Deferred + Tonemap Subpass」拿 80% 收益，把源码改造留给真正卡写带宽的瓶颈。

---

## 9. 配置速查附录（可直接复制）

```ini
;==================================================================
; DefaultEngine.ini —— TBDR 优化（Android/iOS 通用 + iOS 专属注释）
;==================================================================
[/Script/Engine.RendererSettings]

; ---- 路线二选一 ----
; A. Forward（和平精英路线，默认）
r.Mobile.ShadingPath=0
; B. Deferred 片上 GBuffer（燕云路线）—— 取消注释启用
; r.Mobile.ShadingPath=1
; r.MobileHDR=True

; ---- Early-Z / 剔除 ----
r.EarlyZPass=1
r.EarlyZPassOnlyMaterialMasking=1   ; iOS 尤其重要：Opaque 交给 HSR
r.AllowOcclusionQueries=1

; ---- 阴影 ----
r.Mobile.EnableStaticAndCSMShadowReceivers=1
r.Shadow.CSM.MaxMobileCascades=2

; ---- Tonemap Subpass（洛克王国子集，需 Meta XR 插件，与 Deferred 互斥）----
; r.Mobile.TonemapSubpass=1

; ---- 反射 ----
r.Mobile.Forward.EnableClusteredReflections=1

[/Script/IOSRuntimeSettings.IOSRuntimeSettings]
MinimumiOSVersion=IOS_15   ; 确保 Programmable Blending/Memoryless 可用；A11+ 检测 MTLGPUFamilyApple4
```

```ini
;==================================================================
; DefaultDeviceProfiles.ini —— 分档示例
;==================================================================
[Android_Low DeviceProfile]
+CVars=r.MobileContentScaleFactor=0.7
+CVars=foliage.DensityScale=0.4
+CVars=sg.PostProcessQuality=0
```

---

## 10. 全平台验证 Checklist

### 配置层
- [ ] 路线选定（Forward/Deferred），`r.Mobile.ShadingPath` 显式写出
- [ ] 中间 RT（深度/GBuffer/MSAA）确认走 `DontStore`/Memoryless
- [ ] iOS：`r.EarlyZPassOnlyMaterialMasking=1`，Opaque 不做软件 PrePass
- [ ] DeviceProfile 分档接入

### 源码层（改造时）
- [ ] `.usf` 用跨平台 FrameBufferFetch 宏（Vulkan→subpassLoad / Metal→`[[color(n)]]`）
- [ ] MSAA 开/关两套 PSO
- [ ] capability 检测 + fallback（Android 必做；iOS 检测 A11+）

### 验证工具
- [ ] **Android**：RenderDoc 逐 RenderPass 看 Load/Store；Arm Streamline / Snapdragon Profiler 测带宽
- [ ] **iOS**：Xcode GPU Frame Capture 逐 encoder 确认中间 RT 是 Memoryless（不占 device memory）；Metal System Trace 测带宽/HSR 效率
- [ ] 通用：`stat RHI` / `stat GPU`

---

## 11. 参考资料（外部）

**TBDR / 引擎机制**
- 堡垒之夜移动端优化（Vulkan/GLES，UE4.22 显式 RenderPass+Subpass）：https://blog.csdn.net/weixin_33501343/article/details/112190046
- 剖析虚幻渲染体系(12) 移动端专题（RHI RenderPass/SubpassHint 代码）：https://www.ufcn.cn/it/1027192.html
- UE 官方 `FRHIRenderPassInfo`：https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/RHI/FRHIRenderPassInfo
- UE 官方 Mobile Rendering and Shading Modes：https://dev.epicgames.com/documentation/zh-cn/unreal-engine/mobile-rendering-and-shading-modes-for-unreal-engine
- UE 官方 Mobile Deferred Shading（GBuffer 驻留 tile memory）：https://docs.unrealengine.com/documentation/en-us/unreal-engine/using-the-mobile-deferred-shading-mode-in-unreal-engine

**iOS / Metal**
- Apple：Tailor your apps for Apple GPUs and TBDR：https://apple-docs.everest.mt/docs/metal/tailor-your-apps-for-apple-gpus-and-tile-based-deferred-rendering
- Apple WWDC2020：Harnessing Apple GPUs with Metal：https://developer.apple.com/cn/videos/play/wwdc2020/10602/
- Apple Tech Talk 602：Metal 2 on A11：https://developers.apple.com/videos/play/tech-talks/602
- Apple：OIT with image blocks：http://docs.developer.apple.com/documentation/Metal/implementing-order-independent-transparency-with-image-blocks
- Meta：UE Tonemapping（`r.Mobile.TonemapSubpass`）：https://developers.meta.com/horizon/documentation/unreal/unreal-tonemapping/

**案例**
- UFSH2025《洛克王国：世界》移动端管线设计与优化（朱谷才，腾讯魔方）：https://www.gameres.com/916723.html
- Epic 官方《刺激战场》开发经验（光子，2018）：https://www.unrealengine.com/zh-CN/blog/chn-pubg-mobile-ue4-development-experience
- 《和平精英》渲染技术浅析（第三方抓帧，2020）：https://www.magesbox.com/article/detail/id/991.html
- 网易 Messiah 引擎 Frame Graph 技术（官方媒体证实自研引擎）

---

## 12. 知识库导航（E:\AiDoc 内部交叉引用）

本主文档是"TBDR 片上缓存优化"的**方法论纵贯**；以下既有文档是**单款游戏 / 单一主题**的横向资料，互为补充。点击跳转（相对路径）：

### 12.1 本系列文档（TBDR 优化）
| 文档 | 作用 |
|------|------|
| [UE_Mobile_TBDR_验收索引.md](./UE_Mobile_TBDR_验收索引.md) | 🚪 验收入口：产出清单 + 原始问题→答案速查 |
| 本文件 | 主文档：原理→双平台→三案例→决策→iOS |
| [UE_Mobile_TBDR_查漏补缺增补卷.md](./UE_Mobile_TBDR_查漏补缺增补卷.md) | 增补卷：100 点审阅、真实源码、因果修正、工程盲区 |

### 12.2 按"渲染管线选型"映射到既有手游案例
| 管线路线 | 主文档章节 | 既有手游案例（html） |
|---------|-----------|---------------------|
| **Forward + 重剔除** | §5 和平精英 | [和平精英](./和平精英移动端技术要点总结.html)、[使命召唤手游](./使命召唤手游移动端技术要点总结.html)、[暗区突围](./暗区突围移动端技术要点总结.html) |
| **Mobile Deferred + 片上 GBuffer** | §4 燕云十六声 | [燕云十六声](./燕云十六声移动端技术要点总结.html)、[三角洲](./三角洲移动端技术要点总结.html)（多档位退化） |
| **One Pass / Subpass 合并** | §3 洛克王国 | [三角洲](./三角洲移动端技术要点总结.html)（Subpass 优化）、[燕云十六声](./燕云十六声移动端技术要点总结.html) |
| **多档位/平台横向对比** | §6 决策矩阵、§8 落地建议 | [FPS手游技术全景对比](./FPS手游技术全景对比.html) |

### 12.3 按"专题技术"映射（主文档某点 → 既有专题汇总）
| 主文档涉及点 | 既有专题文档（html） |
|-------------|---------------------|
| §1.3 减少进管线工作量 / §5 重剔除 | [头部手游移动端遮挡剔除方案汇总](./头部手游移动端遮挡剔除方案汇总.html)、[头部手游降低DrawCall方案汇总](./头部手游降低DrawCall方案汇总.html) |
| §2.4 半透明读片上深度 / §3 Distortion 合并 | [头部手游半透明渲染方案汇总](./头部手游半透明渲染方案汇总.html)（Subpass 折射代替 GrabPass、Tile Light List） |
| §3 片上 channel repack / §6 分档决策 | [FPS手游技术全景对比](./FPS手游技术全景对比.html)（"片上做 channel repack""带宽的最大杠杆"） |

### 12.4 相关引擎源码级分析（md，深挖时参考）
| 主题 | 文档 |
|------|------|
| 移动端 PVS 不生效分析 | [PVS-Mobile-NotWorking-Analysis.md](./PVS-Mobile-NotWorking-Analysis.md) |
| 视锥剔除优化 | [SceneVisibility_FrustumCull_ZXB_Optimization.md](./SceneVisibility_FrustumCull_ZXB_Optimization.md) |
| RDG 瞬态资源并行创建（subpass 相关） | [RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md](./RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md) |
| SceneDepthZ 瞬态堆 Cache Miss | [SceneDepthZ_Transient_Heap_CacheMiss_Fix.md](./SceneDepthZ_Transient_Heap_CacheMiss_Fix.md) |
| Android 帧率降至 30fps（Swappy） | [UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md](./UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md) |

> 反向入口：建议在既有手游 html 顶部回链本主文档，形成双向导航（见同目录 `知识库导航_README.md`）。

---

> **免责声明**：本文整合官方技术分享、第三方抓帧分析与 TBDR/UE 通用原理。三款商业游戏部分，带具体数字均标注来源；无法证实者标为合理推断，不含编造数据。**燕云十六声使用网易自研 Messiah 引擎（非 UE5）**，相关 UE 实现为同源范本类比。引擎 API 以 UE4.22+/UE5.3+ 为基准；Metal 特性 Programmable Blending/Memoryless 全系 Apple GPU 支持，Imageblock/Tile Shading 需 A11+。落地前请在目标版本/机型用 RenderDoc(Android)/Xcode GPU Capture(iOS) 实测验证。
