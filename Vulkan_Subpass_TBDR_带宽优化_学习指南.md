# Vulkan Subpass × TBDR 带宽优化 · 系统学习指南

> 面向移动端渲染工程师的 Subpass 入门到落地指南。核心命题:**为什么 Subpass 能在 TBDR(Tile-Based Deferred Rendering)架构上省下大量内存带宽,以及到底怎么写。**
>
> 本文重"原生 Vulkan API 语法 + 第一性原理 + 可运行示例代码";应用层结论与 UE 源码实战见同目录交叉引用文档(文末第 12 节)。

---

## 0. 一页纸 TL;DR

- **一句话**:把"产出中间结果的 Pass"和"消费它的 Pass"塞进**同一个 `VkRenderPass` 的不同 Subpass**,中间数据全程留在**片上 tile memory**,不写回也不读回主存(DRAM),从而省掉最贵的那笔带宽——Store + Load。
- **三件套缺一不可**:
  1. **Input Attachment**(`subpassInput` + `subpassLoad()`):后续 subpass **只能读当前像素**的前序结果。
  2. **`VK_DEPENDENCY_BY_REGION_BIT`** 的 subpass dependency:告诉驱动"依赖只在像素本地范围内",才允许 tile 内流水而不 flush。
  3. **Transient / Memoryless attachment**(`LAZILY_ALLOCATED` + `storeOp=DONT_CARE`):中间附件根本不分配主存,只活在 tile 里。
- **省多少**:移动 GPU 上典型 50%~75% 的相关带宽节省(Khronos 官方教程口径);One Pass Deferred 让整张 GBuffer 不落主存。
- **铁律**:Input Attachment **只能读 `gl_FragCoord` 当前像素**。需要邻域采样的效果(高斯模糊 / Bloom / SSAO / SSR)**无法**用 subpass 合并,只能拆 RenderPass 或用 Compute。
- **演进**:Vulkan 1.3 起 `VK_KHR_dynamic_rendering` 简化了无 RenderPass 对象的写法;Vulkan 1.4 / `VK_KHR_dynamic_rendering_local_read` 让 dynamic rendering 也能做 tile-local 读取,等价于 subpass input attachment。

---

## 1. 先建立心智模型:三层结构 + TBDR 的 tile 生命周期

### 1.1 CommandBuffer / RenderPass / Subpass 的层级

```
VkCommandBuffer
└── vkCmdBeginRenderPass(...)           ← 一个 RenderPass 实例
    ├── Subpass 0   (vkCmdDraw...)       ← 写 attachment A
    │      vkCmdNextSubpass(...)
    ├── Subpass 1   (vkCmdDraw...)       ← subpassLoad(A) 读片上, 写 B
    │      vkCmdNextSubpass(...)
    └── Subpass 2   (vkCmdDraw...)       ← subpassLoad(B) 读片上, 写 swapchain
    vkCmdEndRenderPass(...)
```

关键点:**一个 RenderPass 内的所有 Subpass 共享同一块 framebuffer 绑定,也就共享同一片 tile memory。** Subpass 之间的"接力"发生在芯片内部,不碰 DRAM。一旦你把它们拆成两个独立的 `vkCmdBeginRenderPass`,中间结果就必须 Store 到主存、下个 Pass 再 Load 回来——带宽就这么烧掉的。

### 1.2 TBR / TBDR 的 tile 工作流(带宽都花在哪)

移动 GPU(ARM Mali、Qualcomm Adreno、Apple GPU、Imagination PowerVR)是**分块渲染**:把 framebuffer 切成 16×16 或 32×32 的小块(tile),每块单独跑完整管线。每个 tile 的生命周期:

```
 ┌─────────────────────────────────────────────────────────┐
 │  对每个 Tile:                                            │
 │                                                          │
 │   [Load]  从 DRAM 读入旧内容 ──┐  ← loadOp 决定 (读带宽)  │
 │                               │                          │
 │   片上渲染 (所有 subpass)  ────┤  ← 全在 on-chip tile RAM │
 │                               │     超快, 不碰 DRAM       │
 │   [Store] 写回 DRAM       ─────┘  ← storeOp 决定 (写带宽) │
 │                                                          │
 └─────────────────────────────────────────────────────────┘
```

- **Load**:`loadOp = LOAD` 才会读回旧内容;`CLEAR` / `DONT_CARE` 不读 → 省读带宽。
- **Store**:`storeOp = STORE` 才会写回;`DONT_CARE` 不写 → 省写带宽。**Store 通常是整帧最贵的一笔写带宽**。
- **片上渲染**:tile memory 带宽是 DRAM 的几十上百倍,且不耗系统总线功耗。

> **第一性原理(本工作区一以贯之的总纲)**:消除 Store > 合并 Pass(Subpass) > 减少进管线的工作量 > 慎用全屏后处理。Subpass 的全部价值,就是把"本该 Store 出去再 Load 回来"的中间结果摁在 tile 里。

下面这张图把"拆成两个 RenderPass"和"合并成两个 Subpass"的带宽差异画出来。

---

## 2. 为什么 Subpass 省带宽:一张对比图胜过千言

(见对话中的可视化示意图)

核心对比:

| | 两个独立 RenderPass | 一个 RenderPass + 两个 Subpass |
|---|---|---|
| 中间结果 (如 GBuffer) | **Store 到 DRAM** → 下个 Pass **Load 回来** | 全程留 **tile memory**,从不落主存 |
| 中间附件主存占用 | 需要完整分配 | `LAZILY_ALLOCATED`,**0 主存** |
| 读取方式 | `texture()` 任意采样 | `subpassLoad()` **仅当前像素** |
| 带宽 | 一来一回两笔大带宽 | 省掉两笔 |
| 适用 | 需要邻域 / 跨 tile 采样 | 逐像素接力(延迟光照、Tonemap、Fog、Decal) |

---

## 3. 原生 Vulkan API:从零搭一个带 Subpass 的 RenderPass

这是已有文档没系统讲的"基础语法空白",这里补齐。一个 RenderPass 的创建分四步:**描述 Attachment → 写 Attachment Reference → 组装 Subpass → 声明 Subpass 之间的 Dependency**。

### 3.1 Step 1 — Attachment Description(每个附件的 load/store 行为)

以一个最小延迟管线为例:`GBuffer(Albedo + Normal)` + `Depth` + `Swapchain 最终色`。

```c
// 索引约定:
//   0 = swapchain 最终输出(要呈现,必须 STORE)
//   1 = GBuffer Albedo  (中间结果, 不落主存 -> DONT_CARE)
//   2 = GBuffer Normal  (中间结果, 不落主存 -> DONT_CARE)
//   3 = Depth           (中间结果, 不落主存 -> DONT_CARE)
VkAttachmentDescription attachments[4] = {};

// --- 0: Swapchain 最终色 ---
attachments[0].format         = swapchainFormat;
attachments[0].samples        = VK_SAMPLE_COUNT_1_BIT;
attachments[0].loadOp         = VK_ATTACHMENT_LOAD_OP_CLEAR;   // 不读旧内容
attachments[0].storeOp        = VK_ATTACHMENT_STORE_OP_STORE;  // 要呈现, 必须写回
attachments[0].stencilLoadOp  = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
attachments[0].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
attachments[0].initialLayout  = VK_IMAGE_LAYOUT_UNDEFINED;
attachments[0].finalLayout    = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

// --- 1: GBuffer Albedo (片上中间结果) ---
attachments[1].format         = VK_FORMAT_R8G8B8A8_UNORM;
attachments[1].samples        = VK_SAMPLE_COUNT_1_BIT;
attachments[1].loadOp         = VK_ATTACHMENT_LOAD_OP_CLEAR;
attachments[1].storeOp        = VK_ATTACHMENT_STORE_OP_DONT_CARE; // ★ 关键: 不写回 DRAM
attachments[1].stencilLoadOp  = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
attachments[1].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
attachments[1].initialLayout  = VK_IMAGE_LAYOUT_UNDEFINED;
attachments[1].finalLayout    = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

// --- 2: GBuffer Normal (同 Albedo) ---
attachments[2] = attachments[1];
attachments[2].format = VK_FORMAT_A2B10G10R10_UNORM_PACK32; // 法线用高精度

// --- 3: Depth (片上中间结果) ---
attachments[3].format         = depthFormat;          // 如 VK_FORMAT_D32_SFLOAT
attachments[3].samples        = VK_SAMPLE_COUNT_1_BIT;
attachments[3].loadOp         = VK_ATTACHMENT_LOAD_OP_CLEAR;
attachments[3].storeOp        = VK_ATTACHMENT_STORE_OP_DONT_CARE; // ★ 深度也不写回
attachments[3].stencilLoadOp  = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
attachments[3].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
attachments[3].initialLayout  = VK_IMAGE_LAYOUT_UNDEFINED;
attachments[3].finalLayout    = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
```

> **记住**:`storeOp = DONT_CARE` 是省带宽的第一开关。任何"出了这个 RenderPass 就不再需要"的附件,都要设 `DONT_CARE`。GBuffer 在 One Pass Deferred 里就是典型——光照算完它就没用了。

### 3.2 Step 2 + 3 — Attachment Reference 与 Subpass 组装

`VkAttachmentReference` 把"附件索引"绑定到"在某个 subpass 里以什么 layout 使用"。

```c
// ---------- Subpass 0: GBuffer Pass ----------
// 写 Albedo(1) / Normal(2) 作为 color, Depth(3) 作为 depth
VkAttachmentReference gbufColorRefs[2] = {
    { 1, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL },  // Albedo
    { 2, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL },  // Normal
};
VkAttachmentReference gbufDepthRef =
    { 3, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL };

// ---------- Subpass 1: Lighting Pass ----------
// 把 Albedo / Normal / Depth 当作 INPUT ATTACHMENT 读(片上),写 swapchain(0)
VkAttachmentReference lightInputRefs[3] = {
    { 1, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL },  // 读 Albedo
    { 2, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL },  // 读 Normal
    { 3, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL },  // 读 Depth
};
VkAttachmentReference lightColorRef =
    { 0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL };  // 写最终色

VkSubpassDescription subpasses[2] = {};

// Subpass 0
subpasses[0].pipelineBindPoint       = VK_PIPELINE_BIND_POINT_GRAPHICS;
subpasses[0].colorAttachmentCount    = 2;
subpasses[0].pColorAttachments       = gbufColorRefs;
subpasses[0].pDepthStencilAttachment = &gbufDepthRef;

// Subpass 1
subpasses[1].pipelineBindPoint       = VK_PIPELINE_BIND_POINT_GRAPHICS;
subpasses[1].inputAttachmentCount    = 3;          // ★ 这里声明 input attachment
subpasses[1].pInputAttachments       = lightInputRefs;
subpasses[1].colorAttachmentCount    = 1;
subpasses[1].pColorAttachments       = &lightColorRef;
```

> **layout 细节**:同一个附件(如 Albedo,索引 1)在 Subpass 0 里是 `COLOR_ATTACHMENT_OPTIMAL`(被写),在 Subpass 1 里变成 `SHADER_READ_ONLY_OPTIMAL`(被读)。这个 layout 转换由 RenderPass 自动管理,**不需要你手动插 barrier**——这正是 RenderPass/Subpass 模型相比手动同步的便利。

### 3.3 Step 4 — Subpass Dependency:`BY_REGION` 是 TBDR 的灵魂

这是初学者最容易写错、也是最该理解透的一环。Subpass 0 写了 GBuffer,Subpass 1 要读——驱动必须知道这个**生产者→消费者**的依赖,否则数据竞争。但关键在于:**这个依赖是"逐像素本地"的,不是全屏的。**

```c
VkSubpassDependency dependencies[2] = {};

// [0] 外部 → Subpass 0:让本 RenderPass 与上一个 Pass 流水起来
dependencies[0].srcSubpass    = VK_SUBPASS_EXTERNAL;
dependencies[0].dstSubpass    = 0;
dependencies[0].srcStageMask  = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
dependencies[0].dstStageMask  = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
dependencies[0].srcAccessMask = VK_ACCESS_MEMORY_READ_BIT;
dependencies[0].dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
dependencies[0].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

// [1] ★ Subpass 0 → Subpass 1:GBuffer 写完, 才能作为 input attachment 读
dependencies[1].srcSubpass    = 0;
dependencies[1].dstSubpass    = 1;
dependencies[1].srcStageMask  = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT; // 生产: 写颜色
dependencies[1].dstStageMask  = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;         // 消费: FS 里 subpassLoad
dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
dependencies[1].dstAccessMask = VK_ACCESS_INPUT_ATTACHMENT_READ_BIT;
dependencies[1].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;  // ★★★ 灵魂所在
```

**为什么必须是 `VK_DEPENDENCY_BY_REGION_BIT`?**

- 不加这个 flag:依赖被视为**全屏 / 全帧**——驱动会认为 "Subpass 1 的任意像素可能依赖 Subpass 0 的任意像素",于是它必须等整个 framebuffer 的 Subpass 0 全部完成(可能 flush 到主存)才能开始 Subpass 1。tile 流水被打断,带宽优化失效。
- 加上 `BY_REGION`:你向驱动承诺 "Subpass 1 的像素 (x,y) **只依赖** Subpass 0 的像素 (x,y)"。于是每个 tile 可以**独立地**跑完 Subpass 0 → 立刻在片上跑 Subpass 1,数据从不离开 tile。**这正是 input attachment「只能读当前像素」约束的由来**——硬件正是靠这个承诺才敢把数据留在片上。

> 一句话:`BY_REGION` = "我保证不跨像素读"，是 input attachment 能省带宽的**契约**。二者必须配套。

### 3.4 组装并创建 RenderPass

```c
VkRenderPassCreateInfo rpInfo = {};
rpInfo.sType           = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
rpInfo.attachmentCount = 4;
rpInfo.pAttachments    = attachments;
rpInfo.subpassCount    = 2;
rpInfo.pSubpasses      = subpasses;
rpInfo.dependencyCount = 2;
rpInfo.pDependencies   = dependencies;

VkRenderPass renderPass;
vkCreateRenderPass(device, &rpInfo, nullptr, &renderPass);
```

---

## 4. 让中间附件「0 主存」:Transient / Memoryless Attachment

光设 `storeOp = DONT_CARE` 还不够——那只是不写回。要让 GBuffer **连主存都不分配**(真正的片上 only),还要在创建 `VkImage` 时打两个标记:

```c
// 创建 GBuffer 的 VkImage 时:
VkImageCreateInfo imgInfo = {};
imgInfo.sType  = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
imgInfo.format = VK_FORMAT_R8G8B8A8_UNORM;
// ...
imgInfo.usage  = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
               | VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT      // ★ 要当 input attachment 读
               | VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT; // ★ 声明它是"瞬态"的

// 分配内存时, 优先选 LAZILY_ALLOCATED 的 memory type:
VkMemoryAllocateInfo allocInfo = {};
// 查找带 VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT 的 memoryTypeIndex
// 在 TBDR 上, 这类内存"按需懒分配", 若全程留在 tile 里, 物理上可能 0 字节落地
```

三件套合起来才是完整的"片上 only":

| 标记 | 作用 |
|---|---|
| `VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT` | 声明"我活不过这个 RenderPass",允许驱动不给真实后备存储 |
| `VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT` | 允许它被后续 subpass 用 `subpassLoad()` 读 |
| `VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT` | 懒分配内存,理想情况下物理占用为 0 |
| + `storeOp = DONT_CARE` | 不写回主存 |

> 这正是 UE 移动端的 `Memoryless` / `DontStore`、Metal 的 `MTLStorageModeMemoryless`、iOS Imageblock 的底层对应物。映射表见第 9 节。

---

## 5. GLSL 着色器:Input Attachment 怎么读

### 5.1 Subpass 0(GBuffer)的 Fragment Shader — 正常写多目标

```glsl
#version 450
// 输出到两个 color attachment(对应 Subpass 0 的 pColorAttachments)
layout(location = 0) out vec4 outAlbedo;  // -> attachment 1
layout(location = 1) out vec4 outNormal;  // -> attachment 2

layout(location = 0) in vec3 inWorldNormal;
layout(location = 1) in vec2 inUV;
layout(set = 1, binding = 0) uniform sampler2D albedoTex;

void main() {
    outAlbedo = texture(albedoTex, inUV);
    outNormal = vec4(normalize(inWorldNormal) * 0.5 + 0.5, 1.0);
    // 深度由固定功能写入 depth attachment, 无需手写
}
```

### 5.2 Subpass 1(Lighting)的 Fragment Shader — 用 `subpassInput` 读片上

```glsl
#version 450

// ★ input_attachment_index 对应 subpass.pInputAttachments 数组下标
//   set/binding 对应你为该 subpass 准备的 descriptor set
layout(input_attachment_index = 0, set = 0, binding = 0) uniform subpassInput inAlbedo;
layout(input_attachment_index = 1, set = 0, binding = 1) uniform subpassInput inNormal;
layout(input_attachment_index = 2, set = 0, binding = 2) uniform subpassInput inDepth;

layout(location = 0) out vec4 outColor;   // -> swapchain (attachment 0)

layout(set = 1, binding = 0) uniform Lights { vec4 dir; vec4 color; } light;

void main() {
    // ★ subpassLoad 没有 UV 参数 —— 它永远只读"当前像素", 这就是 tile-local 的本质
    vec3 albedo = subpassLoad(inAlbedo).rgb;
    vec3 normal = subpassLoad(inNormal).rgb * 2.0 - 1.0;
    float depth = subpassLoad(inDepth).r;

    float ndotl = max(dot(normalize(normal), -light.dir.xyz), 0.0);
    outColor = vec4(albedo * light.color.rgb * ndotl, 1.0);
}
```

**关键对照** `texture()` vs `subpassLoad()`:

| | `texture(sampler2D, uv)` | `subpassLoad(subpassInput)` |
|---|---|---|
| 数据来源 | 主存里的纹理(需 Store + Load) | 片上 tile memory |
| 可采样位置 | **任意 UV / 邻域** | **仅当前 `gl_FragCoord` 像素** |
| 是否带采样器 | 是(过滤、mip) | 否(就地取值) |
| 适用 | 模糊/Bloom/SSAO 等邻域算法 | 延迟光照/Tonemap/Fog/Decal 等逐像素 |

> **MSAA 坑**:多重采样下要用 `subpassInputMS` + `subpassLoad(input, sampleIndex)`,签名不同。这一点在本工作区 `UE_Mobile_TBDR_Optimization_TechDoc.md §2.2` 有记录(spirv-cross 错绑、双签名问题)。

### 5.3 录制命令:用 `vkCmdNextSubpass` 切换

```c
VkRenderPassBeginInfo begin = { /* renderPass, framebuffer, clearValues... */ };
vkCmdBeginRenderPass(cmd, &begin, VK_SUBPASS_CONTENTS_INLINE);

    // --- Subpass 0: 画场景几何, 填 GBuffer ---
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, gbufferPipeline);
    drawScene(cmd);

    // --- 切到 Subpass 1 ---
    vkCmdNextSubpass(cmd, VK_SUBPASS_CONTENTS_INLINE);

    // --- Subpass 1: 全屏三角形, subpassLoad 读 GBuffer 做光照 ---
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, lightingPipeline);
    vkCmdBindDescriptorSets(cmd, ..., inputAttachmentDescSet, ...); // 绑定 input attachment
    vkCmdDraw(cmd, 3, 1, 0, 0);  // 全屏三角形

vkCmdEndRenderPass(cmd);  // 此刻 GBuffer 随 tile 丢弃, 只 Store swapchain
```

> 注意 lighting pipeline 创建时,`VkGraphicsPipelineCreateInfo::subpass = 1`,而 gbuffer pipeline 是 `subpass = 0`。pipeline 与具体 subpass index 绑定。

---

## 6. 完整范式:One Pass Deferred(整张 GBuffer 不落主存)

把第 3~5 节拼起来,就是移动端最经典的"省带宽延迟渲染":

```
单个 VkRenderPass:
┌──────────────────────────────────────────────────────────────┐
│ Subpass 0 (GBuffer)                                            │
│   写 → Albedo / Normal / [PBR] / Depth   (全部 TRANSIENT)      │
│                                                                │
│   ── BY_REGION dependency (0→1) ──                             │
│                                                                │
│ Subpass 1 (Lighting)                                           │
│   subpassLoad ← Albedo / Normal / Depth  (片上读, 不经 DRAM)   │
│   写 → SceneColor                                              │
└──────────────────────────────────────────────────────────────┘
        EndRenderPass: 只有 SceneColor 被 Store, GBuffer 全丢弃
```

**带宽账**(以 1080p、GBuffer 4×RGBA8 ≈ 4×4 Byte/px 估):

- 传统两 Pass:GBuffer Store(写)+ Lighting 阶段 Load(读)≈ 1920×1080 × 16 Byte × 2 ≈ **~63 MB / 帧** 的额外带宽,按 60fps 即 ~3.7 GB/s,这还只是 GBuffer 一项。
- One Pass Deferred:这两笔**全省**。GBuffer 不分配主存,不 Store,不 Load。

> 这就是"和平精英 Forward 重剔除""洛克王国·世界 One Pass""燕云十六声 片上 GBuffer"等案例背后的同一个机制。具体案例与量化收益见 `UE_Mobile_TBDR_Optimization_TechDoc.md` 与 `燕云十六声_片上GBuffer_技术总结.md`。

---

## 7. Subpass 的能力边界(什么时候**不能**用)

Subpass 不是银弹。它的硬约束源自 `BY_REGION` + input attachment 的"只读当前像素":

| 场景 | 能否 subpass 合并 | 原因 / 替代方案 |
|---|---|---|
| 延迟光照 (逐像素) | ✅ | 只读当前像素 GBuffer |
| Tonemap / 色调映射 | ✅ | 逐像素 |
| 屏幕空间 Decal | ✅ | 逐像素混合 |
| 雾 Fog (基于当前深度) | ✅ | 读当前像素深度即可 |
| 高斯模糊 / Bloom | ❌ | 需邻域采样 → 拆 RenderPass 或 Compute |
| SSAO | ❌ | 需邻域深度采样 |
| SSR (屏幕空间反射) | ❌ | 需沿反射方向 ray-march, 跨像素 |
| 任意 mip / 缩放采样 | ❌ | input attachment 无采样器、无 mip |

> 记忆法:**"只摸自己脚下那一个像素"能干的事才能塞进 subpass。要看邻居,就得拆 Pass。**

另外两个工程坑(来自本工作区实战沉淀):
- **PrePass 与 subpass 深度 fetch 互斥**:UE 里开了 `r.Mobile.EarlyZPass` 走深度纹理读取变体,就不能同时吃 subpass 的片上深度。
- **Tile 内存预算有限**:每像素片上预算约 128~256 bit。GBuffer 通道太肥(太多 RT / 太高精度)会超 tile 预算,反而被迫拆 tile 或降频,得不偿失。设计 GBuffer 布局时要卡这个预算。

---

## 8. API 演进:Render Pass 2 与 Dynamic Rendering

| 写法 | 版本 | 对 Subpass 的意义 |
|---|---|---|
| `VkRenderPass` + subpasses | Vulkan 1.0 | 本文主线。显式、啰嗦,但 TBDR 友好,移动端最成熟。 |
| `VkRenderPass2`(`vkCreateRenderPass2`)| 1.2 (`KHR`) | 结构体可扩展(加 `pNext`),支持 `VkSubpassDescription2` 等,语义不变。 |
| **Dynamic Rendering** (`VK_KHR_dynamic_rendering`) | 1.3 | 直接 `vkCmdBeginRendering`,**不再需要 RenderPass / Framebuffer 对象**。代码大幅简化。**但 1.3 的 dynamic rendering 不支持 input attachment / tile-local 读取**——这恰恰是移动端最需要的能力,所以早期移动端仍多用传统 RenderPass。 |
| **Dynamic Rendering Local Read** (`VK_KHR_dynamic_rendering_local_read`) | 1.4 | 补回 tile-local 读取能力:在 dynamic rendering 下也能像 input attachment 一样**读当前像素的前序输出**,等价于 subpass 合并的省带宽效果,但写法更灵活。移动端新项目可关注。 |

> 选型建议:**移动端当前(2026)落地优先级**——成熟项目/UE 用传统 `VkRenderPass` + ESubpassHint;追求简化且目标设备支持 1.4 的新项目,可评估 `dynamic_rendering_local_read`。无论哪种,省带宽的"三件套"原理不变。

---

## 9. Vulkan ↔ Metal(iOS)对照速查

移动端要双端覆盖,Metal 没有"subpass"概念,靠"同一个 `MTLRenderCommandEncoder` 内多次 draw + programmable blending"实现等价合并:

| 概念 | Vulkan | Metal (iOS) |
|---|---|---|
| 合并多 Pass 留片上 | 同一 RenderPass 的多 Subpass | 同一 `MTLRenderCommandEncoder` 的多 draw |
| 读片上前序结果 | `subpassInput` + `subpassLoad()` | **Programmable Blending**:直接读 `[[color(n)]]` 上次写入值 |
| 中间附件不落主存 | `TRANSIENT` + `LAZILY_ALLOCATED` + `storeOp=DONT_CARE` | `MTLStorageModeMemoryless` + `storeAction = .dontCare` |
| 不读旧内容 | `loadOp = CLEAR / DONT_CARE` | `loadAction = .clear / .dontCare` |
| 逐像素本地约束 | `BY_REGION` dependency | TBDR 硬件天然保证(无需显式声明) |
| 更进一步的片上存储 | (无直接等价) | **Imageblock / Tile Shading**(`imageblock<T>`,显式片上结构) |

> Apple GPU 是**真 TBDR**(有 HSR 硬件隐面剔除,Opaque 可免 PrePass);Mali / Adreno 多为 TBR(需手动 PrePass 做 early-Z)。这条区别很重要,详见 `iOS_Metal_TBDR_实现方案.md` 与 `UE_Mobile_Imageblock_TileShading_落地技术文档.md`。

---

## 10. 落地 Checklist

写 / review 一个 subpass 合并方案时,逐条核对:

- [ ] 生产者和消费者 Pass 是否在**同一个** `vkCmdBeginRenderPass` 内?(拆开就白干)
- [ ] 中间附件 `storeOp = DONT_CARE`?
- [ ] 中间附件 `VkImage` 带 `TRANSIENT_ATTACHMENT_BIT` + `INPUT_ATTACHMENT_BIT`?
- [ ] 内存优先 `LAZILY_ALLOCATED`?
- [ ] 生产→消费的 `VkSubpassDependency` 加了 **`VK_DEPENDENCY_BY_REGION_BIT`**?
- [ ] stage/access mask 精确(生产 `COLOR_ATTACHMENT_OUTPUT`/`WRITE`,消费 `FRAGMENT_SHADER`/`INPUT_ATTACHMENT_READ`),没有用 `ALL_COMMANDS` 这种过宽 barrier?
- [ ] shader 里 `input_attachment_index` 与 `pInputAttachments` 数组下标严格对应?
- [ ] 消费阶段只用 `subpassLoad()` 读**当前像素**,没有任何邻域采样?
- [ ] GBuffer 通道总位宽没超 tile 片上预算(约 128~256 bit/px)?
- [ ] lighting pipeline 的 `VkGraphicsPipelineCreateInfo::subpass` 设成了正确的 index(=1)?
- [ ] (MSAA 时)用了 `subpassInputMS` 且按 sample 读?

---

## 11. 常见疑问 FAQ

**Q1:Subpass 一定省带宽吗?**
不一定。只有当中间结果"出了 RenderPass 就不要了"且能设 `DONT_CARE` + `LAZILY_ALLOCATED` 时才省。如果中间结果后面还要被别的 Pass 当普通纹理采样,它终究得 Store,subpass 优势就没了。

**Q2:为什么不直接用 `texture()` 采样上一个 Pass 的输出?**
那样上一个 Pass 必须 Store 到主存、这个 Pass 再 Load 回来——绕了 DRAM 一圈,正是 subpass 要消灭的带宽。而且在 tile 渲染完成前,纹理内容还不完整,采样会读到脏数据。

**Q3:桌面 GPU(立即模式 IMR)用 subpass 有用吗?**
帮助有限。IMR 没有 tile memory 这层,subpass 主要靠驱动尽量复用 cache。Subpass 的最大收益在 TBR/TBDR 移动端。但写成 subpass 不会更差,且对跨平台代码友好。

**Q4:input attachment 和 descriptor 里的 `inputAttachment` 描述符是一回事吗?**
是的。input attachment 需要在 descriptor set 里以 `VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT` 绑定对应的 image view,shader 里用 `subpassInput` 声明,`input_attachment_index` 与 `set/binding` 都要对上。

**Q5:能在一个 RenderPass 里串很多 subpass 吗?**
可以,但受 tile 预算和复杂度约束。常见是 2~3 个(GBuffer → Lighting → 可选 Tonemap/Fog)。过多 subpass 会让 GBuffer 通道在片上同时驻留,撑爆 tile 预算。

---

## 12. 与本工作区已有文档的关系(交叉引用)

本文聚焦"原生 Vulkan Subpass API 语法 + 第一性原理 + 示例代码"。更深的应用层结论、UE 源码实战、商业手游案例,见同目录:

| 文档 | 互补内容 |
|---|---|
| `UE_Mobile_TBDR_Optimization_TechDoc.md` | TBDR 带宽总纲、`FRHIRenderPassInfo` + `ESubpassHint` 实战代码、MSAA `subpassLoad` 双签名坑、洛克王国/燕云/和平精英案例与量化收益 |
| `UE_Mobile_Forward_vs_Deferred_Tech_Doc.md` | 基于 UE5.5 源码的 `RequiresMultiPass()` / `NextSubpass()` 调度、三种 SubpassHint 的宏与 GBuffer RT 布局、CVar 速查 |
| `燕云十六声_片上GBuffer_技术总结.md` | 片上 GBuffer 在真实商业项目中的落地形态(注:燕云=网易 Messiah 引擎,非 UE5) |
| `iOS_Metal_TBDR_实现方案.md` | Vulkan→Metal 完整映射、Programmable Blending、Memoryless、UE Action→Metal Load/Store Action 对照 |
| `UE_Mobile_Imageblock_TileShading_落地技术文档.md` | Metal Imageblock / Tile Shading(比 input attachment 更进一步的显式片上存储) |

---

## 13. 参考资料

1. Khronos Vulkan Tutorial — Mobile Development: Rendering Approaches(loadOp/storeOp 在 tiler 上的最佳实践、`BY_REGION`、`dynamic_rendering_local_read`):https://github.khronos.org/Vulkan-Site/tutorial/latest/Building_a_Simple_Engine/Mobile_Development/04_rendering_approaches.html
2. KDAB KDGpu — Render to Texture with Subpasses(完整 subpass + input attachment 示例,50–75% 移动端带宽节省口径):https://docs.kdab.com/kdgpu/unstable/render_to_texture_subpass.html
3. Khronos Vulkan Guide — Render Passes / Subpasses、Tile-based GPUs
4. Arm Developer — Vulkan Best Practices:Subpasses & Transient Attachments(Mali tile memory)
5. Vulkan Spec — `VkSubpassDependency`、`VK_DEPENDENCY_BY_REGION_BIT`、`VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT`、`VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT`
6. `VK_KHR_dynamic_rendering` / `VK_KHR_dynamic_rendering_local_read` 扩展规范

---

## 免责声明

- 文中量化带宽数字为基于分辨率与格式的**估算**,用于建立直觉,非特定设备实测;"50%~75% 节省"为 Khronos 教程的通用口径,实际收益随管线、GBuffer 布局、设备而异。
- 示例代码为**教学最小实现**,省略了错误处理、descriptor 完整创建、framebuffer 创建等样板,直接用于生产需补全。
- 引擎归属已交叉核验:燕云十六声 = 网易自研 Messiah 引擎(非 UE5);洛克王国·世界 = UE4.26 Mobile Forward 改造;和平精英 = UE4 Mobile Forward。
- API 版本对应关系基于 Vulkan 1.0–1.4 规范;落地前请以目标设备实际支持的扩展为准。
