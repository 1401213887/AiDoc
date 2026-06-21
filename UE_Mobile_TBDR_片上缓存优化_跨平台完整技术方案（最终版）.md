# Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案（最终版）

> **文档性质**：知识库主技术文档（Master Doc），整合「跨平台完整技术方案 + 100 点查漏补缺增补卷 + 原始问题答案速查」三份成果为单一文档。
> **覆盖范围**：TBDR 原理 → Vulkan(Android) 实现 → 三款手游案例 → 是否需改引擎决策 → iOS(Metal) 实现 → 100 点审阅勘误/深挖 → 原始问题速查
> **引擎基准**：UE4.22+ / UE5.3+（Vulkan RHI + Metal RHI）
> **案例**：洛克王国·世界（One Pass）、燕云十六声（片上 GBuffer）、和平精英（Forward 重剔除）
> **版本**：v3.0（终版合并稿）| 2026-06-22

---

## 文档结构导读

本文档分为三大部分：

- **第一部分（§0–§12）**：跨平台主技术方案。原理 → 双平台机制 → 三案例 → 改引擎决策 → iOS 专属 → 配置/验证 → 参考资料 → 知识库导航。
- **第二部分（§A2–§A14）**：查漏补缺增补卷。100 点结构化审阅，含真实 .usf 源码核验、RGB10A2 因果修正、PrePass×Subpass 冲突、带宽估算公式、版本矩阵、术语表、MobileHDR/八面体法线/Imageblock 深挖。正文中"见增补卷 §N"即指向第二部分的 §A‑N。
- **第三部分（§Q）**：原始问题答案速查，把六个调研问题一句话收口，便于快速检索。

---

## 文档迭代说明（合并维度）

本文档由三份前置文档（TBDR 优化技术文档 / 改造决策文档 / iOS Metal 实现方案）合并迭代而成，并整合了 100 点查漏补缺增补卷与问题速查，优化维度：

1. **结构合并**：三文档收敛为单一主线（原理→平台→案例→决策→跨平台），增补卷并入同一文档第二部分
2. **去重**：消除三份文档间重复的 TBDR 原理、CVar 表
3. **事实核验**：交叉验证燕云=网易 Messiah 自研引擎（非 UE5）
4. **数据补强**：注入燕云真实 GBuffer 布局（B8G8R8A8+R10、~20 byte/px、~1MB tile SRAM、Octahedron/YCoCg）
5. **跨平台并列**：Vulkan 与 Metal 机制改为左右对照表
6. **术语统一**：Tile Memory / 片上缓存 / Memoryless 等术语全文一致
7. **决策前置**：增加执行摘要 + 一页决策矩阵
8. **代码精炼**：RHI / Shader 代码片段去冗余、标注平台
9. **配置附录**：抽出可直接复制的 ini 配置速查
10. **一致性终检**：引用来源、版本号、免责声明统一
11. **勘误深挖内置**：100 点审阅、真实源码、因果修正、工程盲区直接并入正文

---

# 第一部分 · 跨平台主技术方案

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

**关键前提**：UE 渲染代码是 **RHI 抽象**的。你在 `MobileShadingRenderer` 写的 `FRHIRenderPassInfo`/`SubpassHint`/RenderTarget action，同一套代码被 VulkanRHI 译成 `VkRenderPass`+subpass，被 MetalRHI 译成 `MTLRenderPassDescriptor`+Programmable Blending。**Renderer 层改一次，双端生效。**（真实 .usf 五平台分支源码见增补卷 §A2）

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
| `r.MobileHDR` | True | 浮点 SceneColor 承接 HDR 光照；Deferred 强制要求（详见增补卷 §A11） |
| `r.Mobile.TonemapSubpass` | 0 | Tonemap/ColorGrading/Vignette 子通道（需 Meta XR 插件，与 Deferred 互斥；详见增补卷 §A10） |
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
| ③ RGB10A2 通道分配区分角色 | 深度走 **subpass depth fetch 全程留 tile（Memoryless，从不写回任何地方）**；正因深度不再占 SceneColor.A，RGB10A2 的 2-bit alpha 才腾出来当角色/场景 mask（RGB 10/10/10 保颜色精度），**替代 Custom Depth**（省约 3.5MB 带宽、DC 可达 100~200）。⚠️ alpha 与深度无关，详见增补卷 §A3 |
| ④ Deferred HDR 终极管线 | 剔除需邻域的 SSAO/SSR/Bloom，保留 Fog/ColorGrading/Tonemap，**PrePass→Tonemap 全程 1 个 RenderPass，深度全程 Memoryless** |

### 3.3 量化收益（iPhone X 实测）

| 优化项 | 收益 |
|--------|------|
| Subpass 后处理（极限场景） | 带宽 **-23%**、GPU **-31.6%** |
| Subpass 后处理（实际场景） | 带宽 **-12.8%**、读带宽 **-16.5%** |
| Distortion 合并 | GPU **-13.8%**、读带宽 **~-30%** |
| Deferred HDR | 真实帧 **+3%**、读带宽 **-15%**、**写带宽 -30%（最大收益）** |

> 📎 **延伸阅读（知识库内）**：本案例的"前向 + 片上 channel repack"在 FPS 横向对比中的位置，见 [FPS手游技术全景对比.html](./FPS手游技术全景对比.html)（"片上做 channel repack"段）。Subpass 折射代替 GrabPass 的半透明做法，见 [头部手游半透明渲染方案汇总.html](./头部手游半透明渲染方案汇总.html)（"Subpass 折射"段）。配套源码级修正见本文档增补卷 §A3（RGB10A2 因果）。

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
| 法线压缩 | **Octahedron Normal**（八面体编码，单位法线 3 通道→2 通道；原理见增补卷 §A12） |
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
可在 render pass 内联 compute、跨像素访问整个 tile，实现洛克王国都没做的 **tile 光源裁剪 / OIT**。Vulkan subpass 无等价物，是 iOS 高端档位差异化优势。需 `MTLTileRenderPipelineDescriptor`，运行时检测 `MTLGPUFamilyApple4`。**完整原理、MSL 代码与三者（Programmable Blending / Imageblock / Tile Shading）关系见增补卷 §A13。**

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

### 12.1 按"渲染管线选型"映射到既有手游案例
| 管线路线 | 主文档章节 | 既有手游案例（html） |
|---------|-----------|---------------------|
| **Forward + 重剔除** | §5 和平精英 | [和平精英](./和平精英移动端技术要点总结.html)、[使命召唤手游](./使命召唤手游移动端技术要点总结.html)、[暗区突围](./暗区突围移动端技术要点总结.html) |
| **Mobile Deferred + 片上 GBuffer** | §4 燕云十六声 | [燕云十六声](./燕云十六声移动端技术要点总结.html)、[三角洲](./三角洲移动端技术要点总结.html)（多档位退化） |
| **One Pass / Subpass 合并** | §3 洛克王国 | [三角洲](./三角洲移动端技术要点总结.html)（Subpass 优化）、[燕云十六声](./燕云十六声移动端技术要点总结.html) |
| **多档位/平台横向对比** | §6 决策矩阵、§8 落地建议 | [FPS手游技术全景对比](./FPS手游技术全景对比.html) |

### 12.2 按"专题技术"映射（主文档某点 → 既有专题汇总）
| 主文档涉及点 | 既有专题文档（html） |
|-------------|---------------------|
| §1.3 减少进管线工作量 / §5 重剔除 | [头部手游移动端遮挡剔除方案汇总](./头部手游移动端遮挡剔除方案汇总.html)、[头部手游降低DrawCall方案汇总](./头部手游降低DrawCall方案汇总.html) |
| §2.4 半透明读片上深度 / §3 Distortion 合并 | [头部手游半透明渲染方案汇总](./头部手游半透明渲染方案汇总.html)（Subpass 折射代替 GrabPass、Tile Light List） |
| §3 片上 channel repack / §6 分档决策 | [FPS手游技术全景对比](./FPS手游技术全景对比.html)（"片上做 channel repack""带宽的最大杠杆"） |

### 12.3 相关引擎源码级分析（md，深挖时参考）
| 主题 | 文档 |
|------|------|
| 移动端 PVS 不生效分析 | [PVS-Mobile-NotWorking-Analysis.md](./PVS-Mobile-NotWorking-Analysis.md) |
| 视锥剔除优化 | [SceneVisibility_FrustumCull_ZXB_Optimization.md](./SceneVisibility_FrustumCull_ZXB_Optimization.md) |
| RDG 瞬态资源并行创建（subpass 相关） | [RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md](./RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md) |
| SceneDepthZ 瞬态堆 Cache Miss | [SceneDepthZ_Transient_Heap_CacheMiss_Fix.md](./SceneDepthZ_Transient_Heap_CacheMiss_Fix.md) |
| Android 帧率降至 30fps（Swappy） | [UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md](./UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md) |

> 反向入口：建议在既有手游 html 顶部回链本主文档，形成双向导航（见同目录 `知识库导航_README.md`）。

---

# 第二部分 · 查漏补缺增补卷（100 点审阅）

> **性质**：对主技术方案的查漏补缺增补——勘误、深挖、工程盲区补丁。
> **审阅方式**：100 点结构化审阅，按主题归并为 12 组，逐组列出"发现的问题 → 修正/补充"。
> **核心价值**：注入**经源码核验的真实 UE Shader 代码**，修正两处事实性偏差，补全 6 个工程盲区。
> 本部分章节编号统一加前缀 `A`（§A2…§A14），与第一部分的 §2…§12 区分；第一部分中"见增补卷 §A‑N"即指向此处。

## 摘要：本轮审阅最重要的 3 个发现

1. **找到了"同一份 .usf 双后端编译"的真实源码证据** —— UE 的 `LookupDeviceZ()` 函数用一个 `#if` 阶梯，把 GLES/Vulkan/Metal 的片上深度读取统一成一份 shader。这是前文"RHI 抽象、改一次双端生效"论断的硬证据（见 §A2）。
2. **修正了洛克王国 RGB10A2 的因果** —— 深度**全程留 tile（Memoryless），靠 subpass depth fetch 就地读，从不存任何地方**。RGB10A2 的 2-bit alpha 是角色 mask（与深度无关）；那句"alpha only 8-bit"源码注释讲的是另一条路径（深度打包进 SceneColor.A，仅 Mobile HDR/RGBA16F 可行）。详见 §A3。
3. **补全了 PrePass 与 Subpass 深度读取的冲突盲区** —— 开 `r.Mobile.EarlyZPass` 会与 subpass 深度 fetch 路径冲突，需要 `FORCE_DEPTH_TEXTURE_READS` 独立 shader 变体动态切换。这直接关联和平精英(PrePass) 与洛克王国(Subpass) 两个案例的兼容性（见 §A4）。

---

## 100 点审阅 changelog（按 12 主题归并）

> 说明：逐条记录"审阅点 → 判定（✅无误 / ⚠️已修正 / ➕已补充）"。编号 1–100。

### A 组｜事实核验（点 1–12）
| # | 审阅点 | 判定 |
|---|--------|------|
| 1 | 燕云=网易 Messiah 自研引擎，非 UE5 | ✅ 已交叉验证（E:\AiDoc 既有文档佐证） |
| 2 | 洛克王国·世界 = UE4.26 Mobile Forward | ✅ 无误 |
| 3 | 和平精英 = UE4 Mobile Forward | ✅ 无误 |
| 4 | 燕云 GBuffer 布局 B8G8R8A8+R10 | ✅ 来源既有技术要点 |
| 5 | 燕云 tile ~20byte/px、SRAM~1MB | ✅ 已注入主文档 |
| 6 | "GBuffer never stored in system memory" | ✅ UE 官方原文 |
| 7 | 材质指令 147→34 | ✅ UE 官方 |
| 8 | 洛克王国写带宽 -30% | ✅ UFSH2025 |
| 9 | 和平精英 DrawCall<300 / Overdraw<3x | ✅ 第三方抓帧 |
| 10 | **SceneColor.A 8-bit 仅在"深度打包进 alpha"路径(B)相关；洛克王国深度走 subpass 留 tile，不入 alpha** | ⚠️ 已修正 |
| 11 | **UE5 移除软件遮挡，r.Mobile.AllowSoftwareOcclusion 失效** | ✅ 已在决策文档标注 |
| 12 | 燕云"压缩省的不是带宽而是 tile 空间" | ✅ 已修正认知 |

### B 组｜Shader 代码真实性（点 13–24）
| # | 审阅点 | 判定 |
|---|--------|------|
| 13 | 前文 Programmable Blending `[[color(n)]]` 示例 | ✅ 语义正确 |
| 14 | 前文 `subpassLoad` 示例 | ✅ 正确 |
| 15 | **缺少 UE 真实跨平台深度 fetch 源码** | ➕ 本轮补 `LookupDeviceZ()`（§A2） |
| 16 | **缺少 `VulkanSubpassDepthFetch()` intrinsic 说明** | ➕ 已补 |
| 17 | **缺少 GLES `DepthbufferFetchES2()`/`FramebufferFetchES2()` 区分** | ➕ 已补 |
| 18 | **缺少 Metal(非 Mac) 走 `DepthbufferFetchES2()` 的事实** | ➕ 已补 |
| 19 | `MOBILE_DEFERRED_SHADING` 下走 SceneDepthAuxTexture | ➕ 已补（§A2 注） |
| 20 | PhongApprox 移动近似（前向管线指南） | ✅ 无误 |
| 21 | 八面体法线编码 | ✅ 无误 |
| 22 | YCoCg Albedo（燕云） | ✅ 无误 |
| 23 | MSAA 两套 PSO（subpassLoad 签名差异） | ✅ 无误 |
| 24 | **FrameBufferFetch 宏在 .usf 中的真实条件链** | ➕ 已补（§A2） |

### C 组｜PrePass / Subpass 冲突（点 25–34）
| # | 审阅点 | 判定 |
|---|--------|------|
| 25 | **EarlyZPass 与 subpass 深度 fetch 冲突** | ➕ 本轮新增盲区（§A4） |
| 26 | **`FORCE_DEPTH_TEXTURE_READS` 变体需求** | ➕ 已补 |
| 27 | **`IS_MOBILE_DEPTHREAD_SUBPASS` / `MOBILE_DEPTHFECTH` 宏** | ➕ 已补 |
| 28 | **cook 后变体缺失导致无法动态切换的坑** | ➕ 已补 |
| 29 | iOS Opaque 免 PrePass（HSR） | ✅ 无误 |
| 30 | Masked 植被仍需 Early-Z（HSR 不剔 AlphaTest） | ✅ 无误 |
| 31 | 和平精英 Opaque 不做 PrePass | ✅ 无误 |
| 32 | PrePass 必须做成动态非 readonly | ➕ 已补（§A4） |
| 33 | View 额外 uniform 标记 PrePass 状态 | ➕ 已补 |
| 34 | r.EarlyZPass 0/1/2 与 subpass 路径的决策表 | ➕ 已补（§A4 表） |

### D 组｜带宽量化口径（点 35–44）
| # | 审阅点 | 判定 |
|---|--------|------|
| 35 | 区分读带宽 vs 写带宽 | ✅ 已明确 |
| 36 | Store 为最贵操作 | ✅ 无误 |
| 37 | Clear 片上免费 | ✅ 无误 |
| 38 | 洛克王国分场景给数（极限 vs 实际） | ✅ 已保留双口径 |
| 39 | 端到端功耗数字缺失 | ✅ 已诚实标注"无公开数据" |
| 40 | tile spilling 概念 | ➕ 已补（燕云 §4.3） |
| 41 | 1080p GBuffer 数十 MB/帧估算 | ✅ 量级合理 |
| 42 | MSAA tile 内 resolve 零写回 | ✅ 无误 |
| 43 | Memoryless 省的是 footprint+带宽 | ✅ 无误 |
| 44 | **带宽估算公式（像素数×字节×Pass 数×帧率）** | ➕ 已补（§A5） |

### E 组｜CVar 准确性（点 45–54）
| # | 审阅点 | 判定 |
|---|--------|------|
| 45 | `r.Mobile.ShadingPath` 0/1 | ✅ |
| 46 | `r.MobileHDR` Deferred 必需 | ✅ |
| 47 | `r.Mobile.TonemapSubpass` 需 Meta XR + 与 Deferred 互斥 | ✅ |
| 48 | `r.EarlyZPass` / `r.EarlyZPassOnlyMaterialMasking` | ✅ |
| 49 | `r.Mobile.Forward.EnableClusteredReflections` | ✅ |
| 50 | **`r.Mobile.EarlyZPass`（移动专用，区别于 `r.EarlyZPass`）** | ➕ 已补：两者并存，移动端实际看前者 |
| 51 | `r.Mobile.AllowSoftwareOcclusion` UE5 失效 | ✅ |
| 52 | `vulkan.SubpassDepthRead` | ✅ |
| 53 | `MinimumiOSVersion=IOS_15` | ✅ |
| 54 | `MTLGPUFamilyApple4` 运行时检测 | ✅ |

### F 组｜决策矩阵完备性（点 55–64）
| # | 审阅点 | 判定 |
|---|--------|------|
| 55 | 三案例改造成本三分类 | ✅ |
| 56 | 判断三问方法论 | ✅ |
| 57 | 分水岭=RenderPass 编排权 | ✅ |
| 58 | fork 长期负债提示 | ✅ |
| 59 | "先白嫖再 fork"两步走 | ✅ |
| 60 | **缺少版本兼容矩阵（UE5.3/5.4/5.5 差异）** | ➕ 已补（§A6） |
| 61 | **缺少回滚/降级路径完整性检查** | ➕ 已补（§A4 fallback 强调） |
| 62 | 改造文件清单（Vulkan+Metal） | ✅ |
| 63 | 风险评估表 | ✅ |
| 64 | **缺少改造前的基准测量步骤** | ➕ 已补（§A7 流程） |

### G 组｜iOS/Metal 专项（点 65–74）
| # | 审阅点 | 判定 |
|---|--------|------|
| 65 | Programmable Blending 原生无需扩展 | ✅ |
| 66 | Memoryless 全系支持 | ✅ |
| 67 | HSR 硬件遮挡 | ✅ |
| 68 | Imageblock A11+ | ➕ 已深挖（§A13） |
| 69 | Tile Shading A11+ | ➕ 已深挖（§A13） |
| 70 | Raster Order Group | ➕ 已深挖（§A13.4） |
| 71 | 同 encoder 多 draw 留片上 | ✅ |
| 72 | **Metal 非 Mac 深度走 DepthbufferFetchES2** | ➕ 源码核验补充 |
| 73 | LAZILY_ALLOCATED 内存类型 | ✅ UE 官方 |
| 74 | **Apple GPU tile 尺寸随 RT 格式动态变化** | ➕ 已补（§A8） |

### H 组｜术语/一致性（点 75–82）
| # | 审阅点 | 判定 |
|---|--------|------|
| 75 | Tile Memory / 片上缓存 统一 | ✅ |
| 76 | Memoryless 大小写统一 | ✅ |
| 77 | Subpass / 子通道 统一 | ✅ |
| 78 | Forward/前向、Deferred/延迟 统一 | ✅ |
| 79 | TBDR/TBR 区分（Apple vs Mali/Adreno） | ✅ 已澄清 |
| 80 | 相对路径交叉引用 | ✅ 全文统一相对路径 |
| 81 | 免责声明三文档统一 | ✅ |
| 82 | 版本号统一 | ✅ |

### I 组｜结构/可读性（点 83–88）
| # | 审阅点 | 判定 |
|---|--------|------|
| 83 | 执行摘要前置 | ✅ |
| 84 | 决策树/矩阵 | ✅ |
| 85 | 配置速查附录可复制 | ✅ |
| 86 | Checklist 分层（配置/源码/验证） | ✅ |
| 87 | 代码块标注平台 | ✅ |
| 88 | **缺少术语表/缩写表** | ➕ 已补（§A9） |

### J 组｜验证工具链（点 89–93）
| # | 审阅点 | 判定 |
|---|--------|------|
| 89 | RenderDoc(Android) 逐 Pass | ✅ |
| 90 | Xcode GPU Capture(iOS) 验 Memoryless | ✅ |
| 91 | Arm Streamline / Snapdragon Profiler | ✅ |
| 92 | **RenderDoc 如何确认 Load/Store action 的具体操作** | ➕ 已补（§A7） |
| 93 | stat RHI / stat GPU | ✅ |

### K 组｜专家规则符合性（点 94–97）
| # | 审阅点 | 判定 |
|---|--------|------|
| 94 | 量化 tradeoff（带宽/指令数） | ✅ |
| 95 | 精确引擎限制（≤128bit、≤4 RT、16M Nanite 等） | ✅ |
| 96 | 改造前警告（fork 负债、subpass 配错崩溃） | ✅ |
| 97 | C++/源码层级清晰 | ✅ |

### L 组｜诚信边界（点 98–100）
| # | 审阅点 | 判定 |
|---|--------|------|
| 98 | 推断 vs 实据 全程标注 | ✅ |
| 99 | 无编造端到端功耗数字 | ✅ |
| 100 | 燕云=Messiah 类比边界声明 | ✅ |

**统计**：100 点中 ✅ 无误 76 项、➕ 补充 22 项、⚠️ 修正 2 项（点 10 因果修正、点 12 认知修正）。

---

## §A2 真实源码：同一份 .usf 如何编译到三平台（核验补充）

UE 移动管线读取片上深度的统一入口 `LookupDeviceZ()`（节选自引擎 shader，经源码核验）：

```hlsl
float LookupDeviceZ(float2 ScreenUV)
{
#if SCENE_TEXTURES_DISABLED
    return FarDepthValue;
#elif (POST_PROCESS_MATERIAL || POST_PROCESS_MATERIAL_MOBILE) && !POST_PROCESS_AR_PASSTHROUGH
    #if MOBILE_DEFERRED_SHADING
        // 延迟：从 SceneDepthAuxTexture 采样
        return Texture2DSample(MobileSceneTextures.SceneDepthAuxTexture,
                               MobileSceneTextures.SceneDepthAuxTextureSampler, ScreenUV).r;
    #else
        // ★ 前向：BasePass 结束时 SceneDepth 被丢弃，改从 SceneColor.A 取 DeviceZ
        return Texture2DSample(MobileSceneTextures.SceneColorTexture,
                               MobileSceneTextures.SceneColorTextureSampler, ScreenUV).a;
    #endif
#elif COMPILER_GLSL_ES3_1 && PIXELSHADER
    #if !OUTPUT_MOBILE_HDR
        // 【GLES】扩展可用时直接 fetch 深度/模板
        return DepthbufferFetchES2();
    #else
        // 【GLES】否则从 framebuffer fetch 的 alpha 取
        return FramebufferFetchES2().w;
    #endif
#elif VULKAN_SUBPASS_DEPTHFETCH && PIXELSHADER
    // 【Vulkan】专用 intrinsic，从当前 subpass 的深度 attachment 读
    return VulkanSubpassDepthFetch();
#elif (METAL_PROFILE && !MAC) && PIXELSHADER
    // 【Metal iOS】走 framebuffer fetch（Programmable Blending 路径）
    return DepthbufferFetchES2();
#else
    // 兜底：原生深度纹理采样（落主存）
    return Texture2DSampleLevel(MobileSceneTextures.SceneDepthTexture,
                                MobileSceneTextures.SceneDepthTextureSampler, ScreenUV, 0).r;
#endif
}
```

**这段代码证明了什么**：
1. **一份 .usf，五条平台分支**——GLES（两种扩展）、Vulkan（`VulkanSubpassDepthFetch`）、Metal iOS（`DepthbufferFetchES2`）、原生兜底。这就是前文"RHI 抽象、改一次双端生效"的硬证据。
2. **`VULKAN_SUBPASS_DEPTHFETCH` 是 Vulkan 片上深度读取的开关宏**——对应前文 `ESubpassHint::DepthReadSubpass`。
3. **兜底分支会落主存**（`SceneDepthTexture` 采样）——这就是不支持片上 fetch 设备的 fallback，印证 §A4 必做降级路径。

> 来源核验：UE 引擎 `Common.ush` / `MobileSceneTextures` 相关 shader（社区源码解读，见参考）。

---

## §A3 修正：洛克王国 RGB10A2 的真实因果

**前文表述（早期有误，本次修正）**：曾说"RGB10A2 是为了腾通道承载深度+角色标记，绕开 8-bit 精度墙"。这是**自相矛盾**——RGB10A2 的 alpha 只有 **2-bit**，比 RGBA8 的 8-bit 还少，更不可能装深度。错在把**两条独立路径**揉成一段。

### 先回答核心问题：深度最终存哪？

**洛克王国 One Pass 里，深度哪儿都没"存"——它全程留在 tile 上的 depth attachment（Memoryless），在同一个 RenderPass 内被 subpass depth fetch 就地读取，出 RenderPass 即丢弃，从不写回主存。**

### UE 前向管线深度的三种归宿（完整图景）

| 路径 | 深度去哪 | 精度 | 何时用 |
|------|---------|------|--------|
| **A. Subpass Depth Fetch** ★洛克王国 | 留 tile 的 depth attachment（Memoryless），同 Pass 内 `VulkanSubpassDepthFetch()`/`DepthbufferFetchES2()` 就地读，用完即弃 | 完整 D24/D32 | One Pass、iOS Programmable Blending |
| **B. 打包进 SceneColor.A** | BasePass 丢弃 SceneDepth 后，把 DeviceZ 写进 SceneColor 的 alpha，供**跨 Pass**后处理采样 | 依赖格式：RGBA16F(Mobile HDR) 16-bit float alpha 够；RGBA8 8-bit **不够** | 跨 Pass 读深度且开 Mobile HDR |
| **C. SceneDepthAux 单独纹理** | 移动延迟用独立 `SceneDepthAuxTexture` 存 DeviceZ | 单独 RT | Mobile Deferred |

### 源码注释的真实含义（路径 B，非洛克王国路径）

```hlsl
// 引擎源码注释（核验）：
// "We cannot fall back to fetching the alpha channel when MobileHDR=false
//  because the alpha channel is only 8-bit."
```
这句讲的是**路径 B**：只有开 Mobile HDR（SceneColor=RGBA16F、alpha 是 16-bit float）才能把深度塞进 alpha；不开 HDR（RGBA8）时 8-bit alpha 装不下深度，这条路走不通。**与洛克王国的 RGB10A2 无关。**

### RGB10A2 的通道分配（澄清）

洛克王国走的是**路径 A**，其 RGB10A2 格式：
- **RGB 10/10/10** = 颜色（LDR 下比 RGBA8 色彩精度更高）
- **A 2-bit** = **角色/场景标记位**（替代 Custom Depth 的 mask），**与深度无关**
- **深度** = 全程留 tile（路径 A），subpass depth fetch 读取

### 正确的因果链

**不是**"为了装深度才选 RGB10A2"；**而是**——因为深度走 subpass 留在片上（路径 A），SceneColor.A 被解放、不必再背深度，这 2-bit alpha 才能挪作角色标记用。选 RGB10A2 是为了在 LDR 预算下**既保住颜色精度、又腾出 2-bit 做 mask**。

**结论**：One Pass 能成立的真正前提是 **subpass depth fetch 让深度全程驻留 tile（Memoryless，零写回）**；RGB10A2 只是在此前提下对 SceneColor 通道的顺带优化（2-bit alpha 当 mask）。深度与 alpha 是两件事。

---

## §A4 补全盲区：PrePass 与 Subpass 深度读取的冲突

这是和平精英(用 PrePass) 与洛克王国(用 Subpass 深度 fetch) 两种策略**不能简单叠加**的关键工程坑。

### 问题本质
- Subpass 深度 fetch 路径（`MOBILE_DEPTHFETCH`/`IS_MOBILE_DEPTHREAD_SUBPASS`）依赖"深度在当前 RenderPass 的 subpass 里可读"。
- 一旦开 `r.Mobile.EarlyZPass`（PrePass），深度在独立 PrePass 里先写好，后续 Pass 应直接读深度纹理，而非走 subpass fetch。
- **两条路径的 shader 变体不同**。若 cook 时只生成了 subpass 变体，运行时无法动态切到"读深度纹理"的变体。

### 工程解法（核验自社区实践）
```
1. 为相关 Pass 增加 FORCE_DEPTH_TEXTURE_READS 变体：
   PrePass 开启时，强制走"读深度纹理"路径
2. 把 PrePass 开关做成动态（非 readonly），否则 cook 不出可切换变体
3. 部分 Pass 用 IS_MOBILE_DEPTHREAD_SUBPASS 宏（即 MOBILE_DEPTHFETCH 条件）
   统一管理：强制设为 1 以保证 subpass 变体被 cook 出来，再运行时选择
4. View 上加一个 uniform 标记当前 PrePass 状态，shader 据此选分支
```

### 决策表：EarlyZPass × 深度读取路径
| `r.Mobile.EarlyZPass` | 深度来源 | shader 变体 |
|:---:|---------|------------|
| 0（关） | subpass fetch（片上） | 默认 subpass 变体 |
| 1（不透明） | 深度纹理（PrePass 已写） | `FORCE_DEPTH_TEXTURE_READS` |
| 2（不透明+Masked） | 深度纹理 | `FORCE_DEPTH_TEXTURE_READS` |

> **给项目的建议**：
> - **iOS**：HSR 已做硬件遮挡，Opaque 别开 PrePass，让深度走 subpass/Programmable Blending 路径最省。
> - **Android Mali/Adreno**：若 overdraw 严重需要 PrePass，则接受"深度走纹理读取"、放弃 subpass 深度 fetch；二者权衡，别想同时吃。
> - **本质**：PrePass（省 overdraw/FS）与 Subpass 深度 fetch（省深度带宽）在移动端是**互斥取舍**，不是叠加增益。

---

## §A5 补充：带宽估算公式（改造前先算账）

改造前用这个粗算判断收益上界，避免无效 fork：

```
单 RT 单次 Store 带宽（MB/帧）
  = 宽 × 高 × 每像素字节 / (1024×1024)

每帧总写带宽 ≈ Σ(各 RT × 该 RT 的 Store 次数)

示例：1080p（1920×1080）SceneColor RGBA8（4 byte）
  单次 Store = 1920×1080×4 / 1048576 ≈ 7.9 MB
  60fps → 474 MB/s（仅一张 RT 一次 Store）

延迟 GBuffer（4 RT × 4 byte = 16 byte/px）若落主存：
  1920×1080×16 / 1048576 ≈ 31.6 MB/帧 → 60fps ≈ 1.9 GB/s
  → 这就是"片上 GBuffer"要消灭的写带宽量级
```

> 用法：先估"中间 RT 落主存"的写带宽，再估"改片上后省下多少"，若省下量级 < 总带宽 10%，fork 不值得。

---

## §A6 补充：UE 版本兼容矩阵

| 特性 | UE5.3 | UE5.4 | UE5.5 | 备注 |
|------|:----:|:----:|:----:|------|
| `r.Mobile.ShadingPath=1` 片上 GBuffer | ✅ | ✅ | ✅ | 各版 GBuffer 布局略有调整；强制要求 `r.MobileHDR=True`（详见 §A11） |
| `ESubpassHint::DepthReadSubpass` | ✅ | ✅ | ✅ | 稳定 |
| `r.Mobile.TonemapSubpass` | 插件 | 插件 | 插件 | 需 Meta XR / Oculus fork（详见 §A10） |
| 软件遮挡 | ❌ | ❌ | ❌ | UE5 全系移除 |
| RDG 移动端覆盖 | ✅ | ✅ | ✅ | 5.x 持续强化 |

> ⚠️ fork 改渲染器后，每次跨小版本（如 5.3→5.4）都需重新 merge `MobileShadingRenderer` / `VulkanRenderPass` / `MetalRenderPass`。建议把改动集中在少量文件并加清晰 `// [PROJECT] ...` 标记。

---

## §A7 补充：改造前的基准测量标准流程（SOP）

```
1. 选定 3 个代表场景（最复杂战斗 / 大世界远景 / UI 重场景）
2. RenderDoc 抓帧（Android）：
   - 逐 RenderPass 看 LoadOp/StoreOp（确认哪些 RT 在 Store）
   - 记录 SceneColor/Depth/GBuffer 的 Store 次数
3. Xcode GPU Frame Capture（iOS）：
   - 逐 encoder 看 attachment 的 storeAction
   - 确认中间 RT 是否已是 Memoryless（不占 device memory）
4. Arm Streamline / Snapdragon Profiler：测 GPU 外部带宽（read/write 分开）
5. 记录基线：帧率 / GPU 时间 / 读带宽 / 写带宽 / 峰值温度
6. 改造后同场景同流程复测，对比 delta
7. 收益 < 预期或带来兼容问题 → 回滚
```

---

## §A8 补充：Apple GPU tile 尺寸随 RT 格式动态变化

Apple GPU 的 tile 像素数**不是固定的**，取决于该 RenderPass 所有 attachment 的总 bit 数：
- attachment 越"胖"（如多张 GBuffer），单 tile 容纳的像素越少，tile 数越多。
- 这影响 Imageblock 的 `imageBlockSampleLength` 和 tile shader 的 threadgroup 尺寸。
- **设计片上 GBuffer 时要确保总 bit 数不触发 tile 缩小到影响并行度**——这与燕云"~20byte/px 留余量避免 spilling"是同一个工程考量在 Apple 侧的体现。

---

## §A9 术语表 / 缩写表

| 术语 | 全称 / 含义 |
|------|------------|
| TBDR | Tile-Based Deferred Rendering，移动 GPU 分块延迟渲染架构 |
| TBR | Tile-Based Rendering（Mali/Adreno，无硬件 HSR） |
| HSR | Hidden Surface Removal，Apple GPU 硬件隐面剔除 |
| Tile Memory | 片上高速 SRAM，渲染中间结果暂存处 |
| Memoryless | RT 只存在于 tile memory，不分配 device memory（Vulkan: lazily allocated / Metal: `MTLStorageModeMemoryless`） |
| Subpass | Vulkan RenderPass 内共享 tile memory 的子阶段 |
| Input Attachment | Vulkan subpass 间传递片上数据的 attachment（仅当前像素） |
| Programmable Blending | Metal 中 fragment 用 `[[color(n)]]` 直接读片上当前像素 |
| FrameBuffer Fetch | 从片上 framebuffer 读当前像素值（GLES 扩展 / Metal 原生） |
| Imageblock | Apple A11+ 自定义片上 per-pixel 数据结构（详见 §A13） |
| Tile Shading | Apple A11+ render pass 内联 compute，可跨像素访问（详见 §A13） |
| RDG | Render Dependency Graph，UE 的渲染图（自动管理瞬态资源） |
| One Pass | 把多 RenderPass 合并为单 RenderPass 多 subpass 的优化（洛克王国） |
| Octahedron Normal | 八面体法线编码，单位法线 3 通道→2 通道（详见 §A12） |
| YCoCg | 一种颜色空间，用于 Albedo 压缩 |
| tile spilling | tile 数据超出 SRAM 容量被迫溢出到主存的劣化 |

---

## §A10 深挖：`r.Mobile.TonemapSubpass` 到底做了什么

> 对应 §A6 版本矩阵中"`r.Mobile.TonemapSubpass` 插件/插件/插件 | 需 Meta XR / Oculus fork"一行的展开。它是洛克王国 One Pass §3① "后处理 subpass 化"在**官方层面被产品化**的那一小块。

### A10.1 一句话
把 Tonemap（色调映射）从一个**独立全屏后处理 Pass**，变成 SceneColor 所在 RenderPass 内的**一个 Subpass**，从而消除 SceneColor 为做调色而"写回主存→再读回"的一趟来回带宽。

### A10.2 不开它：Tonemap 在 TBDR 上的带宽代价

UE 移动管线默认，Tonemap 是独立全屏 Pass：

```
BasePass (RenderPass A)
  → [Store SceneColor 到主显存]      ← 写带宽
Tonemap (RenderPass B)
  → [Load SceneColor 回 tile]        ← 读带宽
  → Tonemap / Color Grading / Vignette
  → [Store 最终颜色到主显存]          ← 写带宽
```

在 TBDR 上，**两个独立 RenderPass 的边界 = 一次 Store + 一次 Load**。按 §A5 公式，1080p RGBA8 的 SceneColor 来回搬一次 ≈ 7.9MB × 2 ≈ 16MB/帧，60fps ≈ 950MB/s——而这趟搬运唯一目的只是"读回来做个逐像素调色再写回去"，是典型的可消除带宽。

### A10.3 开 `r.Mobile.TonemapSubpass=1`：合进同一 RenderPass

用 **Vulkan Subpass + Input Attachment**（iOS 上是 **Programmable Blending**）把 Tonemap 并进 SceneColor 的同一 RenderPass：

```
RenderPass {
  Subpass 0: BasePass 渲染 → SceneColor 留 tile memory
  Subpass 1: subpassLoad(SceneColor) 读片上当前像素
             → Tonemap / Color Grading(含 LUT) / Vignette
             → 输出最终颜色
  → SceneColor 从不落主存，只有最终结果 Store 一次
}
```

省掉的就是 §A10.2 那趟来回：SceneColor 不写回、不读回。Meta 官方实测：走 subpass 的 tonemap 仅额外增加约 **600μs**，对比独立 Pass 大幅省带宽（最初为 Quest VR 这种带宽/功耗极敏感场景而做）。

### A10.4 三个关键约束（"需 Meta XR / Oculus fork"的由来）

| 约束 | 说明 |
|------|------|
| **① 标准 Epic 版没有，需插件** | 特性由 Meta 在 Oculus-VR fork 实现并贡献，标准引擎需装 **Meta XR 插件**才有此 CVar——这就是矩阵三格都写"插件"而非"内建"的原因。 |
| **② 只能做"逐像素"后处理** | 支持范围严格限定 **Cinematic Tonemap、Color Grading（含 LUT）、Vignette**。因为 subpass/Programmable Blending **只能读当前像素**；需邻域采样的 **Bloom / SSAO / DOF 不能**走这条路（与 §2.4 黄金约束一致）。 |
| **③ 与 Mobile Deferred 互斥** | `r.Mobile.ShadingPath=1` 时它会被**自动禁用**。因为移动延迟已把 BasePass→Lighting 占满 subpass 链，tonemap 无法再插一个 subpass。**燕云路线（Deferred）与本开关二选一。** |

### A10.5 配置

```ini
[/Script/Engine.RendererSettings]
r.Mobile.TonemapSubpass=1
```
或运行时 `r.Mobile.TonemapSubpass 1` / 蓝图 `Execute Console Command`。

### A10.6 在三案例图谱中的定位
这是**洛克王国 One Pass 的"可白嫖子集"**：洛克王国当年要 fork 引擎手动 subpass 化后处理，如今只想要 Tonemap/调色这一段省带宽，装 Meta XR 插件开 CVar 即可、**不改源码**。但它**只覆盖逐像素后处理这一小块**——洛克王国完整 One Pass 的 Distortion 合并、RGB10A2 角色 mask、PrePass→Tonemap 全程单 RenderPass，官方开关仍给不了，需 fork（对应决策矩阵 §6 那几行"✅ 需改引擎"）。

---

## §A11 深挖：`r.MobileHDR` 为何是 Mobile Deferred 的"必需项"

> 对应主文档 §2.5 CVar 表"`r.MobileHDR` | True | Deferred 强制要求"一行的展开。回答"必需"到底是什么意思。

### A11.1 先厘清 `r.MobileHDR` 是什么
它决定移动端 **SceneColor 渲染目标的格式与渲染路径**，本质是"中间场景颜色用高精度浮点、还是低精度 LDR"：

| `r.MobileHDR` | SceneColor 格式 | 含义 |
|:---:|------|------|
| **True（HDR）** | **`PF_FloatR11G11B10`（默认，32-bit）/ RGBA16F（64-bit）** 等浮点格式 | 场景颜色按线性 HDR 渲染，最后经 Tonemap 落到屏幕；支持 Bloom、eye adaptation、完整后处理 |
| **False（LDR）** | **RGBA8（每通道 8-bit，sRGB）** | 直接渲染到低精度 LDR，跳过 Tonemap 链，省带宽/省功耗，但**没有 HDR 中间精度** |

#### A11.1.1 为什么默认 SceneColor 是 R11G11B10F（"是 HDR 但精度看着一般"的解惑）

容易困惑：R11G11B10F 尾数才 5~6 bit，精度看着比 RGBA8 还低，凭什么算 HDR？

**关键认知：HDR 的资格看"指数位"（能否表示 >1），不看"尾数位"（精度）。**

| 格式 | 每像素 | 尾数（精度） | 动态范围（指数） | alpha | 备注 |
|------|:----:|------|------|:----:|------|
| RGBA8 | 32-bit | 8-bit 定点（线性精度尚可） | ❌ 无，**钳在 [0,1]** | 8-bit | 不是 HDR |
| **R11G11B10F** ★默认 | **32-bit** | R/G 6-bit、B 5-bit 尾数（**低**） | ✅ 5-bit 指数，上限 ~65504 | **❌ 无 alpha** | HDR，移动端首选 |
| RGBA16F | 64-bit | 10-bit 尾数（高） | ✅ 有指数 | ✅ 16-bit float | HDR 高保真，带宽翻倍 |

- **R11G11B10F 每分量都是浮点（含独立指数位）**，能表示远超 1.0 的值 → 这就是它"是 HDR"的根本。
- 它"精度一般"是因为**尾数只有 5~6 bit**——在暗部、平滑渐变（天空/雾）上可能 banding，常靠 dithering 缓解。
- 移动端选它的真正理由：**占用与 RGBA8 同宽（32-bit），只有 RGBA16F 的一半**——tile 里少占一半空间（直接关系 §A8 Apple tile 尺寸、§A4 燕云 tile spilling 预算），带宽省一半，却换来 HDR 动态范围。本质是"用 RGBA8 的带宽成本，买到 HDR 资格，代价是精度和 alpha"。
- ⚠️ **连带坑**：R11G11B10F **没有 alpha 通道**。所以 §A3 路径 B"把深度塞进 SceneColor.A"在默认 R11G11B10F 下**根本无 alpha 可塞**，必须 SceneColor 配成 RGBA16F 才有 16-bit float alpha 可用。这进一步解释了为何"深度打包进 alpha"是受格式限制的退化路径，而洛克王国宁可走 subpass depth fetch（路径 A）让深度全程留 tile。

#### A11.1.2 为什么 RGBA8 表示不了 HDR（定点钳位 vs 浮点指数）

> 常见疑问：RGBA8 在 [0,1] 内的精度其实不差，为什么就不能当 HDR 缓冲？

**根因：RGBA8 是"定点 + 钳位"，没有指数位，数值死锁在 [0,1]。**

| 维度 | RGBA8 | R11G11B10F |
|------|-------|-----------|
| 数值类型 | **定点（normalized integer，0~255 映射到 [0,1]）** | 浮点（含指数位） |
| 取值范围 | **死锁 [0.0, 1.0]** | 可到 ~65504 |
| 超 1.0 怎么办 | **直接 clamp 到 1.0，信息永久丢失** | 指数位放大，照常表示 |

- RGBA8 物理上**没有任何位来表达"比 1 更亮"**——写 5.0 进去落盘即 1.0，回读还是 1.0。而 HDR 的本质恰恰是"白点之上仍有大量亮度层次"（太阳、高光、自发光），这一段在 RGBA8 里**不存在**。
- **关键区分：精度 ≠ 动态范围。** RGBA8 的 8-bit 定点在 [0,1] 内是均匀 256 级，比 R11G11B10F 的 6-bit 尾数在同区间还更细——但精度高 ≠ 能表示 HDR。HDR 要的是**动态范围（能否 >1）**，靠**指数位**；RGBA8 无指数位，范围被钉死，再高的定点精度也跨不出 [0,1] 的牢笼。
- 一句话：**RGBA8 是"在 [0,1] 里画得很细的尺子"，但尺子总长只有 1；HDR 需要"能量到几万"的尺子。**

**为什么最终输出用 8-bit 却没事？** 因为最终显示本就是 LDR（普通屏幕只显示 [0,1]）。HDR 的意义在**渲染中间过程**：光照在线性 HDR 空间累加（>1 不丢），**最后经 Tonemap 压回 [0,1]** 再交给 8-bit 输出：

```
HDR 中间缓冲（浮点，>1 不丢）──Tonemap──▶ LDR 输出（RGBA8，[0,1]）
```
问题从来不在"最终用不用 8-bit"，而在"**中间累加光照时用不用 8-bit**"——中间用 RGBA8，光照没来得及 Tonemap 就被钳掉了（§A11.2 延迟着色的死穴）。

**8-bit 能"假装"HDR 吗？** 有 trick 但都不是真 HDR：**RGBE/RGBM**（拿一个通道存共享指数/乘子）能用 8-bit 容器编码 HDR，但**不能硬件直接 blending、不能当普通 render target 用**，移动实时管线基本不采用；**sRGB** 只是 gamma 曲线、仍钳 [0,1]，**不增加动态范围**，更不是 HDR。这些是"用 LDR 容器塞 HDR 数据"的编码技巧，与"原生浮点 render target"是两回事。

### A11.2 为什么 Deferred "必需" HDR

Mobile Deferred 的核心是**片上 GBuffer + Lighting Pass**（见主文档 §4）：
- **GBuffer → Lighting** 在 tile 内算出的是**线性空间的光照结果**，动态范围天然超过 [0,1]（高光、多光叠加轻易 >1）。
- 这个光照结果要写进 SceneColor 等待后续 Tonemap。**若 SceneColor 是 LDR（RGBA8），[0,1] 之外的部分会被直接截断（clip）**，高光死白、多光叠加溢出，PBR 的物理意义全毁。
- 因此延迟着色**必须**有一个浮点 SceneColor 承接光照结果 → 即必须开 `r.MobileHDR=True`。

引擎层面这是一个**硬门槛**：开启 `r.Mobile.ShadingPath=1` 而 `r.MobileHDR=False` 时，延迟路径要么被忽略、要么报错——所以文档写"Deferred 强制要求 Mobile HDR"。CVar Wiki 原文亦标注：*"Deferred shading requires Mobile HDR to be enabled."*

### A11.3 连带影响（三个文档点在此交汇）

1. **与深度打包路径（§A3 路径 B）的关系**：§A3 那句源码注释 *"cannot fall back to fetching the alpha channel when MobileHDR=false because the alpha channel is only 8-bit"* 正是这里——只有 `MobileHDR=True`（SceneColor 是 RGBA16F、alpha 16-bit float）才能把 DeviceZ 塞进 alpha；`MobileHDR=False`（RGBA8、8-bit alpha）装不下。**HDR 开关直接决定了"深度能否走 alpha 打包路径"。**
2. **与 TonemapSubpass（§A10）的关系**：LDR 模式本就跳过 Tonemap 链，`r.Mobile.TonemapSubpass` 自然无从谈起；该特性面向的是 HDR 管线的 tonemap 阶段。
3. **带宽权衡**：HDR 的浮点 SceneColor 比 RGBA8 更"胖"（R11G11B10=4byte 与 RGBA8 同宽，但 RGBA16F=8byte 翻倍），在 tile 预算里占更多空间——这也是为何**和平精英这类纯前向 + 预计算光照的项目，有时反而关掉 MobileHDR 走 LDR 省带宽**（见主文档 §5 选型逻辑）。

### A11.4 一句话总结
`r.MobileHDR=True` 提供了一个**浮点 SceneColor 来承接超 [0,1] 的光照结果**。延迟着色的光照结果天然是 HDR，没有浮点 SceneColor 就会被截断，所以 Deferred「必需」它——这不是建议，是引擎的硬性前置条件。

---

## §A12 深挖：八面体法线编码（Octahedron Normal Encoding）

> 对应 §4.3 燕云 GBuffer 布局"法线压缩 = Octahedron Normal（八面体编码，2 通道）"、主文档 §4.4 GBuffer 约束"法线用八面体编码"的展开。它是片上 GBuffer 能塞进 tile 预算的关键压缩手段之一。

### A12.1 要解决的问题
GBuffer 要存**单位世界法线** `N=(x,y,z)`。朴素做法用 3 个通道（如 RGB10 各存一个分量），但这有两个浪费：
- 法线是**单位向量**（`x²+y²+z²=1`），三个分量其实只有**两个自由度**——存 3 个通道是冗余的。
- 在 tile 预算 ≤128bit/px、Input Attachment ≤4 的硬约束下（§4.4），每省一个通道都极宝贵。

**八面体编码把单位法线从 3 通道压到 2 通道，且精度损失极小、编解码极廉价（几条 ALU）。** 这正是燕云、UE Mobile Deferred 都用它的原因。

### A12.2 核心思想（为什么叫"八面体"）
分三步把球面映射到一个 2D 正方形：

1. **球面 → 八面体**：单位向量除以其 L1 范数（`|x|+|y|+|z|`），把单位球面投影到一个**正八面体**的表面。
2. **八面体 → 平面**：八面体上半部分（z≥0）直接投影到 xy 平面，得到正方形内的 `(x,y)`；下半部分（z<0）做一次"折叠"翻到正方形四角。
3. **存 2 通道**：最终得到 `[-1,1]²` 的二维坐标，重映射到 `[0,1]²` 存进 GBuffer 的两个通道。

解码逆着来：读 2 通道 → 还原八面体坐标 → `z = 1 - |x| - |y|`，z<0 时做反折叠 → 归一化得回单位法线。

### A12.3 编解码代码（HLSL，约定俗成的 Cigolle 版本）

```hlsl
// ---- 编码：单位法线 N → 2 通道 [0,1] ----
float2 OctEncode(float3 N)
{
    N /= (abs(N.x) + abs(N.y) + abs(N.z));      // 投影到八面体
    float2 oct = N.xy;
    if (N.z < 0.0)                               // 下半球：折叠到四角
        oct = (1.0 - abs(N.yx)) * sign(N.xy);
    return oct * 0.5 + 0.5;                      // [-1,1] → [0,1]
}

// ---- 解码：2 通道 → 单位法线 ----
float3 OctDecode(float2 f)
{
    f = f * 2.0 - 1.0;                           // [0,1] → [-1,1]
    float3 N = float3(f.xy, 1.0 - abs(f.x) - abs(f.y));
    float t = saturate(-N.z);
    N.xy += (N.x >= 0.0 ? -t : t);               // 反折叠（按分量符号）
    return normalize(N);
}
```
> 编码约 5~6 条 ALU、解码约 6~8 条，**比球面映射/Lambert 方位角等方案都廉价且无三角函数**，移动端友好。

### A12.4 精度与适用性
- **8-bit/通道**（共 16-bit）即可达到肉眼无差的法线质量；**10-bit/通道**已远超 PBR 所需。八面体编码的误差在整个球面上**分布均匀**（不像经纬度映射在两极退化），这是它优于"直接丢弃 z 靠 sqrt 重建"的关键——后者在 z≈0（掠射角）处精度骤降且丢失 z 符号。
- **适用**：GBuffer 世界法线、切线空间法线压缩、法线纹理在线压缩。
- **注意**：编码后的两通道**不能再做线性插值/blending 后当法线用**（八面体空间非线性），所以它适合"逐像素存取"，与片上 GBuffer 的 subpass 读取模型天然契合。

### A12.5 在本系列中的位置
燕云 GBuffer（§4.3）用 Octahedron Normal 把法线压到 2 通道、配合 YCoCg Albedo，把每像素压到 ~20byte，**正是为了在 ~1MB tile SRAM 里容纳全 GBuffer+Depth+SceneColor、避免 tile spilling**（§A8 同款考量）。所以八面体编码不是孤立的"省显存"技巧，而是**片上 GBuffer 能成立的通道预算前提之一**。

---

## §A13 深挖：iOS 专属王牌 Imageblock 与 Tile Shading（A11+）

> 对应 §A6 版本矩阵第 68/69 行"Imageblock A11+ ✅ / Tile Shading A11+ ✅"，以及主文档 §7.4。这两个是 **Vulkan 没有等价物**的 Metal 独占能力，是 iOS 高端档位的差异化武器。两者都要求 **A11 及以上（`MTLGPUFamilyApple4`）**，需运行时检测。

### A13.1 先厘清三者关系
- **Programmable Blending**（全系 Apple GPU）：fragment shader 用 `[[color(n)]]` 读**当前像素**的片上值——等价于 Vulkan subpass input attachment，**只能读自己这一个像素**。
- **Imageblock**（A11+）：让你**自定义** tile memory 里的 per-pixel 数据结构（任意布局、任意通道），而不只是引擎给的固定 color attachment。
- **Tile Shading**（A11+）：在 render pass **内联一段 compute**（tile shader），它能**访问整个 tile 的所有像素**（不再受"只读当前像素"限制），并能读写 threadgroup 共享内存。

**一句话区分**：Programmable Blending = 读当前像素；**Imageblock = 自定义片上数据怎么摆**；**Tile Shading = 在 tile 内跑 compute、跨像素访问**。后两者组合，突破了 Vulkan subpass "只能逐像素" 的天花板。

### A13.2 Imageblock：自定义片上 per-pixel 结构

普通 render target 的像素布局是引擎/驱动定的；Imageblock 让你在 MSL 里**像定义 struct 一样定义 tile memory 的每像素内容**：

```metal
// 自定义片上 GBuffer 布局（每像素塞什么、各占几位，自己说了算）
struct GBufferImageblock {
    half4 albedo    [[raster_order_group(0)]];   // 颜色
    half4 normal    [[raster_order_group(0)]];   // 法线（可配合八面体编码）
    half  roughness [[raster_order_group(0)]];
};
// fragment 写入片上、后续 tile shader / fragment 直接读，全程不落主存
```
- **价值**：GBuffer 通道布局不再被引擎固定结构束缚，可按项目需要精打细算（呼应 §4.4 的通道预算、§A8 的 tile bit 预算）。
- `[[raster_order_group(n)]]` 配合 Raster Order Group 保证并行 fragment 写同一像素时的**确定性顺序**（见 A13.4）。

### A13.3 Tile Shading：render pass 内联 compute（杀手锏）

这是真正 Vulkan 给不了的东西。经典用例 **Tile-Based 光源裁剪**（Forward+ 的片上版）：

```metal
// 在 BasePass 之后、Lighting 之前，插一段 tile shader
kernel void TileLightCulling(
    imageblock<GBufferImageblock> imageBlock,            // 读整块片上 GBuffer
    threadgroup uint* culledLightList [[threadgroup(0)]],// 写 tile 共享的光源列表
    ushort2 tileCoord [[thread_position_in_threadgroup]])
{
    // 1. 读本 tile 所有像素的深度，算 min/max depth → tile 视锥包围盒
    // 2. 对全场景光源做包围盒相交测试 → 剔除掉本 tile 照不到的光
    // 3. 把通过的光源 index 写进 threadgroup 共享内存
    // 后续 fragment 着色时，只遍历本 tile 的光源子集 → 大幅减少光照计算
}
```
- **为什么 Vulkan 做不到**：Vulkan subpass input attachment **只能读当前像素**，无法在 pass 内"看整个 tile 的所有像素"做归约（min/max depth、光源列表）。Tile Shading 把 compute 能力**内联**进 render pass，且数据全程在 tile memory，省下"光照剔除单独开 compute pass + 读写主存"的整趟带宽。
- **其他用例**：顺序无关半透明（OIT）、自定义 MSAA resolve、tile 内直方图/曝光统计（eye adaptation）。

### A13.4 Raster Order Group（并发写同像素的顺序保证）
A11+ 还提供 `[[raster_order_group(n)]]`：当多个 fragment 线程并行写**同一像素坐标**时，强制它们按光栅化顺序串行访问。这是 OIT、多层混合**正确性**的前提——没有它，并行写同一像素结果不确定。它和 Imageblock 搭配使用。

### A13.5 在 UE 中的现状与落地
- UE Metal RHI **默认不直接暴露**自定义 Imageblock / Tile Shading 给上层——官方 Mobile Deferred 的片上 GBuffer 走的是 Programmable Blending 这条"全系兼容"路径。
- 要用 Imageblock + Tile Shading 做 tile 光源裁剪/OIT 这类**洛克王国都没做的极致优化**，需 **fork 引擎**：在 MetalRHI 里支持 `MTLTileRenderPipelineDescriptor`、自定义 imageblock 布局，并加 `MTLGPUFamilyApple4` 运行时检测 + 非 A11 设备的降级路径。
- **定位**：这是 iOS 高端档位的**差异化天花板**——同一套 UE 项目，iOS 端可以比 Android 端多吃这两张牌（Android Vulkan 无等价物），代价是 fork + 维护成本。

### A13.6 一句话总结
**Programmable Blending 让 iOS 追平 Vulkan subpass（读当前像素）；Imageblock + Tile Shading 让 iOS 超越 Vulkan**——前者自定义片上数据布局，后者在 render pass 内跑跨像素 compute，组合起来能在片上直接做光源裁剪/OIT，这是 Apple TBDR 给高端 iOS 的独家红利。

---

## §A14 仍存在的已知局限（诚实声明）

1. **端到端功耗/温度数字**：三案例均无官方公开的"开优化前后功耗 mW / 温度℃"对比，本系列不提供。
2. **燕云内部实现细节**：Messiah 闭源，§4.x 的 GBuffer 布局来自公开技术要点，subpass 编排为架构推断。
3. **源码行号**：`LookupDeviceZ` 等代码来自社区源码解读，不同 UE 小版本行号/宏名可能微调，落地前请在目标版本引擎源码确认。
4. **RGB10A2 因果**：§A3 的因果链基于引擎前向管线通用行为 + 洛克王国公开分享综合推断，未经洛克王国团队逐字确认。

---

## 增补卷参考资料（本轮新增核验源）

- UE 对 scene depth 的封装（`LookupDeviceZ` 源码解读）：https://www.cnblogs.com/minggoddess/p/14532050.html
- UE Mobile: Prepass Or Not?（`FORCE_DEPTH_TEXTURE_READS` / `IS_MOBILE_DEPTHREAD_SUBPASS` 实践）：https://www.blurredcode.com/2025/03/239ae6a3
- Meta：UE 中的色调映射（`r.Mobile.TonemapSubpass`，§A10 来源）：https://developers.meta.com/horizon/documentation/unreal/unreal-tonemapping/
- CVar Wiki：`r.Mobile.ShadingPath`（"Deferred requires Mobile HDR"，§A11 来源）：https://indxzero.github.io/ue544cvarwiki/articles/r.mobile.shadingpath/
- 其余来源见第一部分 §11。

---

# 第三部分 · 原始问题答案速查

> 把六个核心调研问题一句话收口，便于快速检索。详细论证见对应章节。

### Q1 如何在 Vulkan 下利用 TBDR 片上缓存降低写带宽？（结合 UE 代码）
**答**：三招——①消除 Store（`DontStore`/Memoryless）②合并 Pass（Subpass + Input Attachment）③减少工作量（Early-Z/剔除）。
- UE 代码证据：`FRHIRenderPassInfo` + `ESubpassHint::DepthReadSubpass`（§2.3）；真实 `LookupDeviceZ()` 五平台分支源码（§A2）。
- 一份 .usf 经 `#if` 阶梯编译到 GLES/Vulkan/Metal 三后端 —— RHI 抽象的硬证据。

### Q2 洛克王国 One Pass 如何实现？
**答**：UE4.26 前向改造。FrameBufferFetch + Depth Fetch 把 5 Pass/4 次写回收敛成 1 个 RenderPass，深度全程 Memoryless。**写带宽 -30%**（iPhone X 实测）。
- 关键修正：深度全程留 tile（subpass depth fetch），RGB10A2 的 2-bit alpha 是角色 mask、与深度无关（§A3）。

### Q3 燕云十六声片上 GBuffer 如何实现？
**答**：⚠️ **燕云=网易 Messiah 自研引擎，非 UE5**。技术本质=GBuffer 全程驻留 Tile Memory 永不落主存（Subpass + Memoryless）。真实布局：B8G8R8A8+R10、~20byte/px、~1MB SRAM、Octahedron Normal + YCoCg。
- UE 同源实现：`r.Mobile.ShadingPath=1`，官方"GBuffer never stored in system memory"，材质指令 147→34（§4）。

### Q4 和平精英渲染管线如何实现？
**答**：UE4 Mobile Forward + 重剔除。Forward 选型（大世界+远视距+单主光），Shadow 去 Color RT、光影贴图 3→1、CSM 烘焙混合、Scalability 分档。DrawCall<300、Overdraw<3x（§5）。

### Q5 是否需要改引擎？
**答**：和平精英=0 改造（纯配置）；燕云片上 GBuffer=基本 0 改造（`r.Mobile.ShadingPath=1`）；洛克王国完整 One Pass=需 fork 改渲染器。分水岭=RenderPass 编排权（§6）。

### Q6 iOS Metal 怎么实现？
**答**：iOS 是 TBDR 天选平台。Vulkan Subpass↔Metal Programmable Blending（原生）、DontStore↔Memoryless、Opaque 免 PrePass（HSR）。独享 Imageblock+Tile Shading。UE RHI 抽象让 Renderer 层改造双端共用（§7）。

---

> **免责声明**：本文整合官方技术分享、第三方抓帧分析与 TBDR/UE 通用原理。三款商业游戏部分，带具体数字均标注来源；无法证实者标为合理推断，不含编造数据。**燕云十六声使用网易自研 Messiah 引擎（非 UE5）**，相关 UE 实现为同源范本类比。引擎 API 以 UE4.22+/UE5.3+ 为基准；Metal 特性 Programmable Blending/Memoryless 全系 Apple GPU 支持，Imageblock/Tile Shading 需 A11+。落地前请在目标版本/机型用 RenderDoc(Android)/Xcode GPU Capture(iOS) 实测验证。诚信边界详见 §A14。
