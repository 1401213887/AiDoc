# UE 移动端 Imageblock 与 Tile Shading 落地技术文档

> **文档性质**：专题技术文档，聚焦 Apple GPU 两项 A11+ 片上能力（Imageblock / Tile Shading）在 Unreal Engine 移动管线中的落地分析。
> **定位**：是 `UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案（最终版）.md` §A13 的展开与资料补强，可独立阅读。
> **结论先行**：UE 标准渲染器在 iOS 上用 **Programmable Blending（framebuffer fetch）** 实现片上 GBuffer，**并未使用** Apple 的自定义 Imageblock / Tile Shading；后两者在 UE 里属于「需 fork 引擎」的差异化高端能力。
> **版本基准**：UE5.3+ Mobile Deferred；Metal / iOS 11+；A11（`MTLGPUFamilyApple4`）+。
> **资料来源**：Apple 官方文档、WWDC 2018/2019/2020、UE 官方 Mobile Deferred 文档、社区源码实践（见文末参考）。

---

## 0. 一页速览

| 维度 | Programmable Blending | Imageblock | Tile Shading |
|------|----------------------|------------|--------------|
| 硬件要求 | 全系 Apple GPU | A11+（GPU Family 4） | A11+（GPU Family 4） |
| 能力 | 读/写**当前像素**片上值 | **自定义**片上 per-pixel 数据结构 | render pass 内**内联 compute**，**跨像素**访问整 tile |
| Vulkan 等价物 | Subpass Input Attachment（≈ 打平） | ❌ 无 | ❌ 无 |
| UE 是否默认使用 | ✅ **是**（Mobile Deferred 的 iOS 实现） | ❌ 否（需 fork） | ❌ 否（需 fork） |
| 典型用途 | 单 pass 延迟着色、片上 GBuffer | 自定义 GBuffer 布局、复用 tile 内存做 OIT | 片上光源裁剪、OIT、自定义 MSAA resolve、曝光统计 |

**一句话**：UE 用户能「开箱即用」的是 Programmable Blending 带来的片上 GBuffer 收益；Imageblock 与 Tile Shading 是 Apple 给的更高级武器，UE 标准分支没接，要用得改引擎。

---

## 1. 特性介绍

### 1.1 背景：A11 起 Apple TBDR 的四件套

Apple 自 A11（GPU Family 4）起，在 TBDR 基础上开放了一组增强能力，官方称之为对 tile memory 的可编程访问。完整四件套是：

1. **Imageblocks** —— 自定义片上 per-pixel 数据结构
2. **Tile Shading** —— render pass 内联 compute
3. **Raster Order Groups** —— 并发写同像素的顺序保证
4. **Imageblock Sample Coverage Control** —— 自定义 MSAA 样本跟踪

它们共同的目的只有一个：**让更多中间计算留在片上 tile memory，避免数据往返 device memory（主显存）产生的带宽与功耗。** 这与移动 TBDR 优化的总纲领完全一致——省的是「最贵的写带宽」。

本文聚焦其中开发者最常用、对渲染管线影响最大的两项：**Imageblock** 与 **Tile Shading**。

### 1.2 Imageblock：自定义片上数据布局

普通 render target 的像素布局（几个通道、各占几位）由 API/驱动定死，开发者只能用引擎给的固定 color attachment。

**Imageblock 让开发者在 MSL 里像声明 struct 一样，直接定义 tile memory 中每个像素塞什么。** 它支持新的 pack 数据类型（与纹理格式对应，访问时透明打包/解包），还支持数组、嵌套 struct 等复杂结构。

> WWDC 2018 原话：*"Imageblocks give you full control of your data in tile memory. Instead of describing pixels as arrays of render pass attachments in the Metal API, imageblocks let you declare your pixel layouts directly in the shading language as structs."*

它的杀手价值在于：**可以在一个 pass 内改变 tile 内存的用途**——读完 GBuffer 算完光照后，把同一块 tile 内存复用去做别的（如 OIT 的 per-pixel 链表），这是 Programmable Blending 单独做不到的。

### 1.3 Tile Shading：render pass 内联 compute

普通的 fragment shader 只能读「当前像素」。许多算法（光源裁剪、OIT、直方图统计）需要「看一整块 tile 的所有像素做归约」，传统做法只能：**render pass 结束 → 数据写回 device memory → 另开 compute pass 读回来 → 算完再写回**。这一圈往返在 TBDR 上恰恰是最该消灭的带宽。

**Tile Shading 允许在一个正在进行的 render pass 中间，内联一段 compute（或 fragment）函数（tile shader / kernel），它能访问整个 tile 的所有像素，并读写 threadgroup 共享内存，中间结果全程留在片上。**

> Apple 官方原话：*"Tile shaders are compute or fragment functions that execute as part of a render pass, allowing for midrender compute with persistent memory between rendering phases. The tile memory that tile shaders work within remains in the on-chip memory of the GPU."*

**关键澄清：tile shader 是不是 compute shader？**
- **是**：它用 `kernel` 限定符，有 threadgroup、共享内存、`thread_position_in_threadgroup`，能力上就是 compute。
- **但不是普通 compute pass**：它嵌在 render pass 内部，用**同一个 `MTLRenderCommandEncoder`**，通过 `dispatchThreadsPerTile` 触发，线程组天然对齐到一个 tile 的像素范围，数据直接吃片上、零搬运。

---

## 2. 特性用法介绍

### 2.1 Imageblock 用法

**第 1 步：在 MSL 里定义片上结构。**

```metal
// 自定义片上 GBuffer 布局——每像素塞什么、各占几位，自己说了算
struct GBufferImageblock {
    half4 albedo    [[raster_order_group(0)]];   // 颜色
    half4 normal    [[raster_order_group(0)]];   // 法线（可配八面体编码）
    half  roughness [[raster_order_group(0)]];
};
```

**第 2 步：fragment 写入 imageblock**，全程留 tile，不落主存。

**第 3 步：后续 fragment / tile shader 直接读这个 imageblock** 算光照，输出最终颜色。只有最终 SceneColor 写回一次。

`[[raster_order_group(n)]]` 配合使用，保证并行 fragment 写同一像素时按光栅化顺序串行——这是 OIT、多层混合正确性的前提。

### 2.2 Tile Shading 用法（以片上光源裁剪为例）

**第 1 步：用 `MTLTileRenderPipelineDescriptor` 配置 tile pipeline。**

```objc
MTLTileRenderPipelineDescriptor *tileDesc = [MTLTileRenderPipelineDescriptor new];
tileDesc.label = @"Light Culling";
tileDesc.rasterSampleCount = NumSamples;
tileDesc.tileFunction = lightCullingKernel;          // 指定 tile kernel
tileDesc.threadgroupSizeMatchesTileSize = YES;       // 线程组覆盖整 tile
id<MTLRenderPipelineState> tilePSO =
    [device newRenderPipelineStateWithTileDescriptor:tileDesc options:0 reflection:nil error:&err];
```

**第 2 步：在同一个 render encoder 中间，切到 tile PSO 并 dispatch。**

```objc
// ... BasePass 的 draw 已写好片上 GBuffer/深度 ...
[renderEnc setRenderPipelineState:tilePSO];
[renderEnc dispatchThreadsPerTile:MTLSizeMake(tileW, tileH, 1)];   // 内联 compute
// ... 紧接着 Lighting 的 draw，直接用裁剪后的光源列表 ...
```

**第 3 步：tile kernel 内做跨像素归约。**

```metal
kernel void TileLightCulling(
    imageblock<GBufferImageblock> imageBlock,            // 读整块片上 GBuffer
    threadgroup uint* culledLightList [[threadgroup(0)]],// 写 tile 共享光源列表
    ushort2 tid [[thread_position_in_threadgroup]])
{
    // 1. 读本 tile 所有像素深度，算 min/max → tile 视锥包围盒
    // 2. 全场景光源做包围盒相交测试 → 剔掉照不到本 tile 的光
    // 3. 通过的光源 index 写进 threadgroup 共享内存
    // 后续 fragment 着色时只遍历本 tile 的光源子集 → 大幅减少光照计算
}
```

### 2.3 两者的协作关系

- **Programmable Blending**（全系）= 读当前像素 → 单 pass 延迟着色的基础。
- **Imageblock**（A11+）= 自定义片上数据怎么摆 → 让 tile 内存可被精细规划和复用。
- **Tile Shading**（A11+）= 在 tile 内跑 compute、跨像素访问 → 让 Forward+/OIT 等归约类算法全程留片上。

后两者通常组合使用：Imageblock 定义片上数据，Tile Shading 跨像素处理这些数据。

---

## 3. 特性在引擎中如何落地

### 3.1 UE 移动延迟的现状：用的是 Programmable Blending，不是 Imageblock

UE 官方文档明确说明了 Mobile Deferred 在各平台访问片上 GBuffer 的方式：

> UE 官方原文（5.6 中文文档）：
> - **iOS 使用类似于 `framebuffer_fetch` 的功能访问图块内存中的 GBuffer。**
> - Android Vulkan 使用 Vulkan 的子处理通道（Subpass）。
> - Android GLES 需要扩展，并且没有适用于所有 GPU 的通用方法。

这说明：**UE 在 iOS 上实现「GBuffer 全程留 tile、永不落主存」靠的是 Programmable Blending（framebuffer fetch）这条全系兼容的路径，而非 Apple 的自定义 Imageblock / Tile Shading。**

原因很清楚：
- **Programmable Blending 全系 Apple GPU 支持**，无需 A11+ 检测、无需降级路径，兼容性最广。
- **Imageblock / Tile Shading 要 A11+**，还得为非 A11 设备写 fallback——对一个要覆盖全机型的商业引擎，这是不划算的默认选择。

### 3.2 UE 的 framebuffer fetch 在 .usf 里长什么样

UE 用一套跨平台宏把片上读取统一成一份 shader，编译期按后端展开。社区源码实践（freesion 博客，UE4.26 起 Vulkan/Metal 均实现）给出了真实用法：

```hlsl
// UE 跨平台片上读取宏（编译期转译为各后端）
// fetch color : SubpassFetchRGBA_0() / SubpassFetchRGBA_1() / ...
// fetch float : SubpassFetchR_0()    / SubpassFetchR_1()    / ...
```

这些宏在 iOS 上被翻译成 Metal 的 `[[color(n)]]`（Programmable Blending），在 Vulkan 上翻译成 `subpassLoad` + input attachment。配套主文档 §A2 的 `LookupDeviceZ()` 五平台分支即此机制的深度证据。

> ⚠️ **社区已知坑（落地必读）**：iOS 上曾出现 `SubpassFetchR_1()` 不生效——XCode GPU Capture 发现 `gl_LastFragDataR_1` 被错误绑定到 `[[color(0)]]`。根因是 `spirv-cross` 在「HLSL → Metal」转译时，缺少 `[[color(0)]]` 引用会把 input_attachment_index 错绑。规避办法是确保 color(0) 被引用（社区用 `SubpassFetchRGBA_0().w * 0 + SubpassFetchR_1()` 撞大运式解决）。UE4.26 对此做过修正，落地前仍需在目标版本实测。

### 3.3 想真正用 Imageblock / Tile Shading：需要 fork 哪些地方

UE 的 Metal RHI 默认**不向上层暴露**自定义 Imageblock 布局与 tile shader dispatch。要落地这两项能力，改造点包括：

| 改造层 | 改造内容 |
|--------|---------|
| **MetalRHI** | 支持 `MTLTileRenderPipelineDescriptor` 的创建与绑定；暴露 `dispatchThreadsPerTile` 到 RHI command list；支持自定义 imageblock 布局声明 |
| **Renderer** | 在 `MobileShadingRenderer` 的 BasePass 与 Lighting 之间插入 tile shader 阶段（如光源裁剪） |
| **Shader（.usf/.metal）** | 编写 tile kernel；定义 imageblock struct；用 `[[raster_order_group(n)]]` 保证写序 |
| **设备检测** | 运行时检测 `MTLGPUFamilyApple4`（A11+）；为非 A11 设备保留 Programmable Blending 的降级路径 |
| **PSO 管理** | tile PSO 与传统 render PSO 两套并存；MSAA 开关下变体管理 |

### 3.4 落地流程（理想形态）

```
1. 设备分档：检测 MTLGPUFamilyApple4
   ├─ A11+   → 走 Imageblock + Tile Shading 高端路径
   └─ 非 A11 → 回落到 Programmable Blending（UE 默认路径）
2. 在 MetalRHI 暴露 tile pipeline / imageblock 能力
3. Renderer 编排：BasePass(写 imageblock) → TileShader(光源裁剪) → Lighting(读裁剪结果)
4. 全程 tile memory，SceneColor 最后 store 一次
5. Xcode GPU Capture 验证：中间 RT 全 Memoryless、无 device memory 往返
```

---

## 4. UE 中使用该特性的方式和收益

### 4.1 方式分层：能直接用的 vs 需 fork 的

| 你想要的效果 | 在 UE 里的方式 | 是否改引擎 |
|-------------|---------------|:---------:|
| 片上 GBuffer、单 pass 延迟、GBuffer 零写回 | `r.Mobile.ShadingPath=1`（项目设置选 Deferred Shading） | ❌ 开箱即用 |
| 半透明读片上深度 | 默认 Subpass/framebuffer fetch | ❌ |
| 逐像素后处理省带宽（Tonemap） | `r.Mobile.TonemapSubpass`（需 Meta XR 插件） | ❌ |
| **自定义 GBuffer 通道布局** | 自定义 Imageblock struct | ✅ fork MetalRHI |
| **片上 Tile-Based 光源裁剪（Forward+ 片上版）** | Tile Shading + dispatchThreadsPerTile | ✅ fork MetalRHI |
| **片上 OIT（顺序无关半透明）** | Imageblock + Raster Order Group + Tile Shading | ✅ fork MetalRHI |
| **片上曝光统计 / 自定义 MSAA resolve** | Tile Shading | ✅ fork MetalRHI |

### 4.2 收益（分层量化）

**A. 开箱即用层（Programmable Blending，UE 默认）——这是大多数项目能拿到的收益：**
- **GBuffer 永不落主存**：官方 "GBuffer is never stored in system memory"；支持 memoryless 的设备不分配 device memory（LAZILY_ALLOCATED）。
- **材质大幅简化**：官方示例材质 **指令 147→34、采样器 2→0**（延迟无需在材质里带光照/阴影代码）。
- **CPU/RHI 线程减负**：延迟无需为每个 draw 绑定阴影/反射纹理，图形状态管理更少，RHI 线程更空，大核可服务其他线程。
- **带宽收益量级**：1080p 下 4~5 张 GBuffer 若落主存约 31.6MB/帧（60fps ≈ 1.9GB/s 写带宽），片上化后这部分写带宽直接归零（详见主文档 §A5 公式）。

> 这一层就是 Imageblock 想达到的「单 pass 延迟」效果的 **80%**——**不碰 Imageblock API 也能吃到**。

**B. fork 高端层（Imageblock + Tile Shading）——增量收益，仅高端档位：**
- **光源裁剪全程片上**：传统 Forward+ 的 light culling 需独立 compute pass + 主存往返；改用 Tile Shading 后，BasePass 刚写的深度还在片上，裁剪在同 render pass 内完成，**省掉整趟「光照剔除 compute pass 读写主存」的带宽**。
- **tile 内存复用做 OIT**：算完光照后复用同块 tile 内存做多层 alpha 混合（MLAB），WWDC 原话 *"sorting the MLAB array is really fast because it lives in tile memory; doing the same off chip would be really expensive."*
- **通道预算精打细算**：自定义 imageblock 布局可逐 bit 规划 GBuffer，配合八面体法线 + YCoCg 把每像素压到极限（呼应燕云 ~20byte/px 避免 tile spilling）。

### 4.3 投入产出判断（给项目的决策建议）

| 项目情况 | 建议 |
|---------|------|
| 大多数移动项目 | **只用 Programmable Blending（开 Mobile Deferred 即可）**，拿 80% 片上收益，零维护成本 |
| 重多动态光、追求 iOS 高端画质/能效差异化 | 评估 fork 做 **Tile Shading 光源裁剪**；需团队有 Metal RHI 维护能力 |
| 需要高质量半透明（毛发/烟雾/玻璃叠层） | 评估 fork 做 **Imageblock + ROG 的片上 OIT** |
| 全机型广覆盖、维护人力有限 | **不建议** fork——Imageblock/Tile Shading 仅 A11+，还要写降级路径，长期负债 |

> **核心忠告**：fork 渲染器是长期负债，每次 UE 升版都要重新 merge MetalRHI。优先用官方 Mobile Deferred（Programmable Blending）拿主要收益，把 Imageblock/Tile Shading 留给真正卡在「光照剔除/OIT 带宽」且面向 iOS 高端机的瓶颈。

---

## 5. 关键事实核验与边界

1. **UE 官方确认**：iOS Mobile Deferred 用 `framebuffer_fetch`（即 Programmable Blending）访问片上 GBuffer——**非** Imageblock。（UE 5.6 官方文档）
2. **Imageblock / Tile Shading 要 A11+**（GPU Family 4 / `MTLGPUFamilyApple4`），iOS 11+ API。（Apple 官方）
3. **Tile Shading 是 render pass 内联的 compute/fragment 函数**，通过 `MTLTileRenderPipelineDescriptor` + `dispatchThreadsPerTile` 使用，数据留片上。（Apple 官方）
4. **UE 标准分支未暴露自定义 Imageblock / tile shader dispatch**，需 fork MetalRHI——此为基于「官方文档明确走 framebuffer fetch + 社区无公开的 UE 官方 tile shader 支持」的合理判断，落地前请在目标 UE 版本源码确认 MetalRHI 是否已有相关接口。
5. **spirv-cross 转译坑真实存在**（iOS framebuffer fetch input attachment 错绑），UE4.26 起有修正，仍需实测。（社区源码实践）

---

## 6. 参考资料

**Apple 官方**
- Tailor your apps for Apple GPUs and TBDR（GPU Family 4 四件套总览）：https://developer.apple.com/documentation/metal/gpu_features/understanding_gpu_family_4
- About Tile Shading（render pass 内联 compute）：https://developer.apple.com/documentation/metal/mtldevice/ios_and_tvos_devices/about_gpu_family_4/about_tile_shading
- MTLTileRenderPipelineDescriptor：https://developer.apple.com/documentation/metal/mtltilerenderpipelinedescriptor
- dispatchThreadsPerTile：https://developer.apple.com/documentation/metal/mtlrendercommandencoder/dispatchthreadspertile(_:)

**WWDC**
- WWDC 2018 - Metal for Game Developers（Programmable Blending vs Imageblock，单 pass 延迟 + MLAB OIT）：https://nonstrict.eu/wwdcindex/wwdc2018/607/
- WWDC 2019 - Modern Rendering with Metal（programmable blending 合并 geometry+lighting pass）：https://developer.apple.com/videos/play/wwdc2019/601/
- WWDC 2020 - Harnessing Apple GPUs with Metal

**UE 官方**
- Using the Mobile Deferred Shading Mode in UE（iOS 用 framebuffer_fetch / GBuffer never stored in system memory / 材质 147→34）：https://dev.epicgames.com/documentation/zh-cn/unreal-engine/using-the-mobile-deferred-shading-mode-in-unreal-engine

**社区源码实践**
- 【Metal 引擎剖析(五)：Forward+ with Tile Shading】（MTLTileRenderPipelineDescriptor 真实用法）：https://www.freesion.com/article/3647211164
- UE4 iOS Metal FrameBufferFetchMRT（SubpassFetch 宏 + spirv-cross 错绑坑）：https://www.freesion.com/article/55001484421

---

> **免责声明**：本文整合 Apple 官方文档、WWDC 分享、UE 官方文档与社区源码实践。涉及「UE 是否暴露某 API」的判断基于公开资料的合理推断（UE 官方明确 iOS 走 framebuffer fetch、社区无公开 UE 官方 tile shader 支持），不同 UE 版本 MetalRHI 实现可能演进，落地前请在目标版本引擎源码核实。Imageblock/Tile Shading 需 A11+，落地必须保留非 A11 设备的降级路径。
