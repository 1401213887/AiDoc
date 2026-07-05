# 《洛克王国：世界》移动端管线设计与优化 — 渲染 Pass / One Pass 技术报告

# 《洛克王国：世界》移动端管线设计与优化

渲染 Pass 合并策略与 One Pass 实现 — 技术汇总报告

UE 4.26
Mobile Forward Rendering
Render Pass 合并
One Pass / Deferred HDR
FrameBuffer Fetch
Tile Memory / TBDR
公开资料整理

分享来源：朱谷才（腾讯魔方工作室客户端开发）· UFSH2025 Shanghai · 2025-11


## 01项目背景与管线选型

先把约束条件摆出来，再看为什么"合并 Pass"是这个项目最值的优化方向。

游戏特性

- 开放世界经营收集养成 + 精灵收集对战；移动 / PC 双端同步上线
- 高度风格化美术 + 卡通描边，视觉差异化要求高
- 完整 TOD（Time of Day）系统：动态实时光照 + 动态实时阴影
- 天气系统、水面反射、大量动态点光源
- **多人 50+ 角色 / 精灵同屏**，对 Draw Call 与带宽的压力极端
- 性能目标：主流机 30 FPS，旗舰机 60 FPS

管线选型

| 项 | 选择 | 原因 |
| --- | --- | --- |
| 引擎 | UE 4.26 | 项目周期决定的版本基线 |
| 基础管线 | Mobile Forward Rendering（前向） | 移动端兼容性最广，MSAA 友好；延迟管线在移动端有 SSR/MSAA 冲突 |
| 新增渲染特性 | SSR、SSAO、Color Grading、动态点光源、TOD、天气 | 表现需求扩展；这些扩展是逼出后续 Pass 合并优化的根本原因 |

> 扩展前向管线的代价就是 **Pass 越来越多**：BasePass、SSAO、合入、Fog、ColorGrading、半透明、SeparateTranslucency、Distortion 累加、Distortion 合并、Bloom、ToneMapping…… 每多一个 Pass，就多一次 Tile→DRAM 的写回 + 重载。本项目的优化主线，本质就是**把这条 Pass 链尽量塞回单个 Render Pass 内**。

## 02总体优化思路：从 Pass 数量入手

移动端 TBDR 架构下，Render Pass 切换 = Tile flush + DRAM 写回 + 下一 Pass 重新 Load，是带宽 / 功耗 / 发热的最大元凶。

原始链路
→
BasePass→
SSAO 计算→
Pass A：解码+SSAO 合入+Fog→
Pass B：ColorGrading+半透明→
Pass C：后处理材质→
Pass D：Distortion 累加→
Pass E：Distortion 合并 + Bloom + ToneMapping

看似已经"很合并"了，仍是 **5 个 Render Pass、4 次显存写回**。打断点正是**后处理材质**和 **Distortion** 这两类需要"读上一 Pass 输出"的步骤。

分层目标（渐进式优化路径）

1. **RGB10A2 Stencil**：替代 Custom Depth，消除一整条额外 Pass + 海量 DC（前置基础）
2. **后处理 Fog**：解决 SSAO 与顶点 Fog 冲突，顺带省 BasePass 顶点开销
3. **SubPass 优化**：用 FrameBuffer Fetch 把后处理材质和 Distortion 拉回单 Pass — **5 Pass → 2 Pass**
4. **One Pass / Deferred HDR**：终极合并，PrePass→ToneMapping 全部塞进单 Render Pass，深度 RT 走 Memoryless

## 03RGB10A2 Stencil — 前置基础

不解决"角色 / 场景像素级区分"的成本问题，就没法谈后面的 Pass 合并。

### 为什么不能用 Custom Depth Stencil

| 问题 | 开销量化 |
| --- | --- |
| 额外 Render Pass | 即使 0 Draw Call，移动端 ≥ **0.5 ms** |
| 额外贴图带宽 | 720P 下 ≥ **3.5 MB**（两张屏幕大小贴图） |
| Draw Call 爆炸 | 单角色多部件骨骼 + 描边 = **9 DC**；50+ 同屏 = **100~200 DC** |

### 方案：BasePass 输出格式改成 RGB10A2，Alpha 做 Stencil

- BasePass RT：浮点格式 → **RGB10A2**
- 角色材质：把 `1` 写入 Alpha 通道
- 后续 Pass：直接采样 BasePass.A，`== 1` 即角色像素
- 收益：**零额外 Pass、零额外带宽、零额外 Draw Call**

⚠️ 问题 1：HDR Clamp

RGB10A2 是 `UNORM`，颜色被 Clamp 到 [0,1]，高亮像素丢失，Bloom 错误。

```
// 定点数编码（恢复 HDR）
encoded = color / range;
decoded = encoded * range;
```

⚠️ 问题 2：低值精度损失（Banding）

扩 range 后比特位被高值占用，天空球出现明显色阶。

```
// 开根号编码（保护近 0 精度）
encoded = sqrt(color / range);
decoded = (encoded * encoded) * range;
```

开根号本质是把"线性量化"换成"非线性量化"，把更多比特位让给低亮度区间 — 这是 sRGB / Gamma 的同源思想。

## 04后处理 Fog — 与 SSAO 的兼容性优化

小动作，大收益。

### 冲突

原管线 BasePass 顶点阶段算 Fog；引入 SSAO 后，AO 在 BasePass 之后基于完整深度计算并合入 → 把 Fog 颜色一并压暗。

### 解决：Fog 后置到屏幕空间

BasePass（不算 Fog）→
SSAO 计算 + 合入→
屏幕空间 Fog（基于深度）

附带收益

多角色多精灵场景下，BasePass **顶点着色器开销 -10%**。复杂模型移除顶点 Fog 计算，VS 压力直接下降。

### 混合策略：哪些物体保留顶点 Fog

| 对象 | 策略 | 原因 |
| --- | --- | --- |
| 天空球 | 顶点 Fog | 不受 SSAO 影响，VS 开销远低于 PS（数十分之一） |
| 后处理 Fog 之后渲染的物体 | 顶点 Fog | 已错过后处理 Fog 阶段，模型简单 |
| 美术自定义 Fog（如雷暴云） | 材质编辑器扩展 | 需要特殊曲线 / 颜色过渡 |

## 05SubPass 后处理优化 — 5 Pass → 2 Pass

这是真正用上 FrameBuffer Fetch 的关键一步，也是 One Pass 的预演。

### 5.1 打断 Pass 的两大元凶

| 元凶 | 为什么打断 Pass |
| --- | --- |
| **后处理材质（Post Process Material）** | 需要采样 `Post Process Input`，本质是上一 Pass 的输出贴图，必须强制 Resolve 到 DRAM 后再读 |
| **Distortion 扭曲** | "累加 Pass + 合并 Pass" 两阶段，合并阶段必须读累加结果 |

### 5.2 优化一：SubPass 后处理材质

**关键观察**：项目里大多数后处理材质只用当前像素颜色，不需要邻域采样。

实现思路

- 用 **FrameBuffer Fetch** 替代 `SceneTexture: PostProcessInput0` 采样
- 当前像素颜色直接从 **Tile Memory** 读出，不下 DRAM 不下 Cache
- 上下游计算保留在同一 Render Pass 中

−23%

极限场景 · 显存带宽

−31.6%

极限场景 · GPU 耗时

−5.5%

实际游戏 · GPU 耗时

−12.8% / −16.5%

实际游戏 · 写 / 读带宽

— iPhone X 实测

### 5.3 优化二：Distortion 合并下放

**原结构**：

Distortion 累加 Pass→
Distortion 合并 Pass（采样累加 RT 做扭曲合成）→
Bloom + ToneMapping

**优化思路**：Bloom 与 ToneMapping 反正都要采样 Stencil，**顺手把 Distortion 的合并算掉**。

Distortion 累加（保留唯一一次写回）→
Bloom + ToneMapping + Distortion 合并（合并执行）

代价（项目可接受）

原本 Distortion 合并在 Bloom 之前，特效不会被扭曲；下放后特效也会被扭曲。需要美术 / 策划 sign-off。

−13.8%

GPU 耗时

−30%

读显存带宽

### 5.4 优化前后对比

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| Render Pass 数量 | 5 | 2 |
| 显存写回次数 | 4 | 1 |
| 主 Pass 工作内容 | — | Color Grading + SSAO 合入 + 后处理 Fog + 半透明 + 后处理材质 + SubPass Distortion |

## 06One Pass / Deferred HDR 终极合并 ★

把 PrePass → ToneMapping 全部合入**单个 Render Pass**。这是整篇分享技术含量最高、性价比最极端的一段。

### 6.1 命名说明 + 一个常见误解

> 分享中将这套方案称为 **"Deferred HDR"**。注意它**不是**传统的 Deferred Shading（不写多张 GBuffer 做延迟着色），而是利用 FrameBuffer Fetch + Depth Fetch，把后处理链条"延迟"到当前像素已驻留 Tile Memory 时一次性算完。本质是 **One Pass Post Process**，下文用 "One Pass" 称呼。

⚠️ 关键误解澄清：One Pass 不是 2 Pass 的"再优化"

很多人以为优化路径是 **5 Pass → 2 Pass → 1 Pass** 一路压下来。**不是**。

- **SubPass 优化（2 Pass）**：保留 SSAO/SSR/Bloom，给**旗舰/中高端**用。这是数学下限——只要保留 Bloom，就不可能再合到 1 Pass。
- **One Pass / Deferred HDR**：**主动放弃 SSAO/SSR/Bloom**，给**中低端/低端**用。是另一条平行管线。

它俩是**双轨方案，按设备档位分流**，不是连续优化。

### 6.2 为什么 2 Pass 是"不可逾越的下限"

回顾 SubPass 优化后的 2 Pass 结构：

Pass A
PrePass→BasePass→SSAO→Fog→ColorGrading→半透明→后处理材质→SubPass Distortion
💾 写回
Pass B
Bloom + ToneMapping + Distortion 合并

A 和 B 之间为什么必死？

**因为 Bloom 必须做邻域采样**：

- Bloom 是高斯模糊，要采样当前像素**周围一片区域**（典型 13×13 ≈ 169 个邻居）
- Tile Memory 里 `subpassLoad()` **只能拿到当前像素**（API 硬限制）
- 想做邻域模糊 → 必须把整张图先写回 DRAM 才能用 Sampler 采样 → **必须断 Pass**

所以只要保留 Bloom，2 Pass 就是数学下限。**One Pass 唯一的破局点是：把 Bloom（以及 SSAO/SSR）一起扔掉**。

### 6.3 One Pass 内部排布（核心实现）

舍弃邻域采样后，剩下的操作清单**全部都是"当前像素 → 当前像素"**：

| 阶段 | 需要什么 | 能否 Tile Memory 内做 |
| --- | --- | --- |
| PrePass | 当前像素深度 | ✅ |
| BasePass（不透明光照） | 当前 fragment 自己 | ✅ |
| 半透明（水/玻璃/特效） | 当前 fragment + 当前像素 SceneColor 做 blend | ✅ |
| 后处理 Fog | 当前像素深度 + 颜色 | ✅ |
| Color Grading | LUT 查表（纹理采样合法，纹理是只读的，跟 Tile 无关） | ✅ |
| Tone Mapping | 当前像素 HDR 颜色 | ✅ |

排布到 Render Pass 里，典型布局是 **1 个 Render Pass + 3~4 个 Subpass**：

```
═══ 单个 Render Pass ═══

  Subpass 0: PrePass
    → 写 SceneDepth (Memoryless)

  Subpass 1: BasePass
    → 读 SceneDepth (subpassLoad / Early-Z)
    → 写 SceneColor (RGB10A2, Alpha = Stencil)

  Subpass 2: 半透明
    → 读 SceneColor (subpassLoad) 做 alpha blend
    → 读 SceneDepth 做深度测试
    → 写 SceneColor

  Subpass 3: 后处理链（Fog → ColorGrading → ToneMapping）
    → 读 SceneColor + SceneDepth (subpassLoad)
    → 算完直接写 Backbuffer

═══ Pass 结束 ═══
  SceneDepth: storeOp = DONT_CARE  → 直接丢弃，永不下 DRAM
  SceneColor: 中间 RT，最后已被 Subpass 3 消费完，也丢弃
  Backbuffer: STORE → 这是唯一一次写回 DRAM
```

整个过程：

- SceneColor 一直住在 Tile Memory，Subpass 之间通过 `subpassLoad()` 接力，**永不下 DRAM**
- SceneDepth 整张 Pass 都在 Tile Memory，Pass 结束 `storeOp = DONT_CARE` 直接丢弃，**永远不分配显存**
- 整帧只有 **Backbuffer 写一次**

### 6.4 三件套技术细节

#### ① FrameBuffer Fetch（颜色通路）

让 Subpass N 读 Subpass N-1 的颜色输出，不下 DRAM。Vulkan/GLSL 写法：

```
// Subpass N-1 写
layout(location=0) out vec4 outColor;

// Subpass N 读（同一个 attachment 当作 input）
layout(input_attachment_index=0, set=0, binding=0) uniform subpassInput SceneColorIn;

void main() {
    vec4 c = subpassLoad(SceneColorIn);  // ← 从 Tile Memory 读当前像素
    // ...
}
```

Metal 更直接：用 `[[color(0)]]` 标记输入参数即可。
OpenGL ES 用扩展 `EXT_shader_framebuffer_fetch`，shader 输出变量加 `inout`。

#### ② Depth Fetch（深度通路 — 比颜色 fetch 挑设备）

| API / 平台 | Depth Fetch 支持 | 实现 / 备选 |
| --- | --- | --- |
| Vulkan | ✅ 通过 InputAttachment 直接读 Depth Attachment | 主流路径 |
| OpenGL ES + ARM Mali | ⚠️ 需扩展 | `ARM_shader_framebuffer_fetch_depth_stencil` |
| OpenGL ES 其他厂商 | ❌ 不支持 | 走备选方案 |
| Metal 1.x | ❌ | 走备选方案 |
| Metal 2.0+ | ⚠️ 通过 ImageBlock 自定义结构体 | iOS 11+ 设备 |

备选方案（不支持原生 Depth Fetch）

BasePass 时把 NDC 深度**额外**写到一张 R32F 的 **Memoryless Color RT** 上，后续 Subpass 用 InputAttachment 读这张 RT。

代价：bpp 多吃 32 bit；好处：**仍然不下 DRAM**，逻辑等价于 Depth Fetch。

#### ③ Memoryless Attachment（容器：让 RT 永不分配显存）

```
// Vulkan
VkImageCreateInfo:
    usage |= VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT;
VkMemoryAllocateInfo:
    properties |= VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT;
VkAttachmentDescription:
    storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;

// Metal
texture.storageMode = MTLStorageModeMemoryless;

// UE 高层封装
FRDGTextureDesc Desc = ...;
Desc.Flags |= TexCreate_Memoryless | TexCreate_DepthStencilTargetable;
```

**注意**：Memoryless 节省的是 **DRAM 带宽和显存**，不节省 Tile Memory bpp 预算。这张 RT 仍然要在 Tile Memory 里占位。

### 6.5 Tile Memory bpp 预算（这套方案能成立的硬约束）

不同 GPU 的 Tile Memory 有**每像素 bit 数（bpp）的死预算**，所有挂在 Render Pass 上的 Color/Depth/Stencil Attachment 加起来不能超：

| GPU | 每像素 bpp 预算（典型） | 超了会怎样 |
| --- | --- | --- |
| ARM Mali（旗舰） | ~128 bit | tile 自动拆小（如 16×16→16×8），BasePass binning 开销翻倍 |
| Qualcomm Adreno | 较宽松（GMEM 256KB~1MB） | — |
| Apple GPU | ~256 bit | — |

#### 项目方案 bpp 预算清单（典型 One Pass 配置）

```
SceneColor      RGB10A2          32 bit
SceneDepth      Depth32          32 bit  (Memoryless)
[备选] DepthCopy R32F             32 bit  (Memoryless, 不支持 Depth Fetch 时启用)
─────────────────────────────────────────
合计                          64 ~ 96 bit / 像素   ✅ 远低于 128 bit
```

为什么连最低端 Mali 都吃得下？

- 选了**前向**：没有 GBuffer，RT 数量本来就少
- RGB10A2 替代 RGBA16F：SceneColor bpp 直接**减半**（64 → 32）
- Stencil 偷渡进 Alpha：省了 Custom Depth 的额外 R32F + R8 = 40 bit

这就是为什么 RGB10A2 Stencil、后处理 Fog 等"小优化"**必须先做**——它们本质都是为了把 bpp 控制在安全区，让 One Pass 这个终局成立。

什么情况会撞上 Tile Memory 上限？

- 移动延迟管线（UE Mobile Deferred）：SceneColor 32 + 3×GBuffer 96 + Depth 32 = **160 bit** → 部分 Mali 设备 tile 拆小
- 加 MSAA 4x：bpp × 4 → 必爆
- SceneColor 用 RGBA16F：64 + Depth 32 + GBuffer → 高风险

这是为什么本项目选前向不选延迟，也是为什么 SceneColor 必须从 RGBA16F 降到 RGB10A2。

### 6.6 关键工程要点（Render Pass 描述符）

Attachment 的 LoadOp / StoreOp 配置

```
VkAttachmentDescription attachments[2] = {
  // 0: SceneColor — 中间 RT，最终消费完即丢
  { format = A2B10G10R10_UNORM,
    loadOp  = CLEAR,
    storeOp = DONT_CARE  },           // ← 关键：不写 DRAM

  // 1: SceneDepth — Memoryless
  { format = D32_SFLOAT,
    loadOp  = CLEAR,
    storeOp = DONT_CARE  },           // ← 关键：不写 DRAM
};
// Backbuffer 单独是另一个 attachment，storeOp = STORE
```

- 所有中间 RT **`storeOp = DONT_CARE`**，使其有资格走 Memoryless
- Subpass 之间用 InputAttachment 引用前一 Subpass 的 ColorAttachment / DepthAttachment
- InputAttachment 数量限制：UE 4.27 起统一为 **3 个**（兼容设备占比 83.7%）
- UE 触发路径：`FRHIRenderPassInfo::SubpassHint = ESubpassHint::DeferredShadingSubpass`

### 6.7 性能收益（RenderDoc 实测）

−3%

真实帧时间

−15%

读带宽

−30%

写带宽 ★

为什么"性价比极端"

**带宽降幅远大于帧时间降幅**，这是关键洞察：

- 帧时间 −3%：GPU 计算量没变多少（少做了 SSAO/SSR/Bloom，但本身不是大头）
- 写带宽 −30%：DRAM 来回少了 4 次屏幕大小贴图

带宽下降在移动端等价于：**发热↓ → 调频概率↓ → 长时间稳帧能力↑ → 续航↑**。这是手游真正的"长尾收益"，比帧时间降几个百分点重要得多。

### 6.8 双轨适用矩阵

| 设备档位 | 采用管线 | 原因 |
| --- | --- | --- |
| 旗舰 / 中高端 | SubPass 优化版（5→2 Pass） | 带宽宽裕能扛 4 次写回；保留 SSAO/SSR/Bloom 体现画质溢价 |
| 中低端 / 低端 | One Pass / Deferred HDR | 用画质换稳帧 + 续航 + 不掉档 |
| 不支持 FBF/SubpassInput 的设备 | 降级回传统多 Pass | 兜底分支必须留 |

> 所以"贵机器吃画质，便宜机器吃帧率"是产品决策，不是工程必然。如果项目硬把所有设备都用 One Pass，旗舰用户会觉得画面没该有的水准。

### 6.9 一句话总结

**One Pass 不是"再优化版"，而是另一条管线**。它的可行性建立在三个前提上：

1. **画质让步**：放弃 SSAO/SSR/Bloom 这些需要邻域采样的效果
2. **API 支持**：FrameBuffer Fetch + Depth Fetch（或 Memoryless 中转）+ Memoryless RT
3. **bpp 预算**：前向 + RGB10A2 把 Tile Memory 占用压在 64~96 bit/像素

三者缺一不可。把 Bloom 留下，方案直接降级回 2 Pass；把 SceneColor 改回 RGBA16F，bpp 就可能爆 Mali 的 128 bit。所以前面 RGB10A2 Stencil、后处理 Fog 这些铺垫，**不是各自独立的小优化，是为 One Pass 这个终局服务的连环动作**。

## 07底层原理：Tile Memory / FBF / Depth Fetch

理解为什么"少一次 Pass 切换"这么值钱。

### 7.1 TBR / TBDR 的 Pass 切换代价

1. Render Pass 开始：把对应 RT 区域从 DRAM **Load** 到 Tile Memory（如有 LoadOp=LOAD）
2. 所有 Draw 在 Tile Memory 完成 ROP 操作
3. Render Pass 结束：把 Tile Memory **Store / Resolve** 回 DRAM（如有 StoreOp=STORE）

Tile Memory 是 **SRAM**，访问 ~1ns；DRAM Load/Store 是 ~100ns 量级。每多一次 Pass 切换 = 一次屏幕大小贴图的 DRAM 来回 = 数 MB 带宽 + 数百微秒。

### 7.2 FrameBuffer Fetch / SubpassInput 的本质

都是**"让当前像素已经在 Tile Memory 里的颜色，直接被下一阶段 shader 读到"**，避免下 DRAM。它们的区别只是 API 形式不同：

| API | 形式 | 限制 |
| --- | --- | --- |
| OpenGL ES (FBF) | `inout` 修饰输出 | 仅 Adreno / PowerVR；Mali 不支持通用版本 |
| OpenGL ES (PLS) | Pixel Local Storage 自定义结构体 | Mali：128 bits 上限，仅 1 张实际 RT |
| Vulkan SubpassInput | Subpass 间 InputAttachment | ≤ 3~4 个 InputAttachment |
| Metal ImageBlock | `color(n)` | 需要 Metal 2.0+ |

### 7.3 Memoryless Attachment

关键三件套：

- **Vulkan**：`VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT` + `VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT`
- **Metal**：`MTLStorageModeMemoryless`
- **OpenGL ES**：`EXT_discard_framebuffer` / `InvalidateFramebuffer` 在 Pass 末尾让驱动有机会丢弃

含义：**这张 RT 永远不会出现在 DRAM 里**，仅在 Pass 期间存在于 Tile Memory。一旦它被引用一次也不下 DRAM，就免去了写带宽、读带宽，并节省一份显存。

## 08性能数据汇总（iPhone X 实测）

把分享中各处给的数据汇总一张表，方便横向对比。

| 优化项 | 读带宽 | 写带宽 | GPU 耗时 / 帧时 | 测试场景 |
| --- | --- | --- | --- | --- |
| RGB10A2 Stencil 替代 Custom Depth | — | 消除 ~3.5 MB / frame | 消除 ~0.5 ms 额外 Pass | 720P 多角色场景 |
| 后处理 Fog（替代顶点 Fog） | — | — | BasePass VS −10% | 多精灵场景 |
| SubPass 后处理材质（FBF） | −16.5% | −12.8% | GPU −5.5%（极限 −31.6%） | 实际游戏 / 极限场景 |
| Distortion 合并下放到 Bloom+ToneMap | −30% | — | GPU −13.8% | 含 Distortion 场景 |
| **One Pass / Deferred HDR ★** | −15% | −30% | 真实帧时间 −3% | RenderDoc 全帧 |

> 带宽下降幅度远超 GPU 耗时下降 — 这是非常典型的**移动端发热 / 续航 / 调频驱动型**收益模型。在续航受限的手游里，带宽 −30% 等价于：长时间游玩稳帧能力↑、芯片温度↓、降频概率↓、电池续航↑。

## 09UE Mobile 落地工程要点

把抽象的"Pass 合并"翻译成 UE 工程代码该改哪里。

### 9.1 涉及的 UE 模块

- `Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp` — 主入口，控制 Render Pass 划分
- `MobilePostProcessing.cpp` — 后处理链拼装；Distortion 下放、SubPass 后处理材质都在这里改
- `SceneRenderTargets` — 配置 RT 格式、StoreOp、Memoryless Flag
- `FRDGBuilder`（如果 RDG 化）/ `FRHIRenderPassInfo` — 显式声明 LoadOp / StoreOp
- `ShaderCompilers` + `SubpassInput*` HLSL 翻译（Vulkan / Metal 后端）

### 9.2 关键 RT 配置示例（伪代码）

```
// SceneColor 改为 PF_A2B10G10R10
SceneColorFormat = PF_A2B10G10R10;

// Depth RT 走 Memoryless
FRHITextureCreateDesc DepthDesc = ...;
DepthDesc.Flags |= TexCreate_Memoryless | TexCreate_DepthStencilTargetable;

// Render Pass 内部的 StoreOp
FRHIRenderPassInfo RPInfo(
    SceneColorRT, ERenderTargetActions::Clear_Store,           // 最终输出
    DepthRT,      EDepthStencilTargetActions::ClearDepthStencil_DontStoreDepthStencil
);
RPInfo.SubpassHint = ESubpassHint::DeferredShadingSubpass;     // 触发 SubpassInput 路径
```

### 9.3 材质侧改造

- BasePass 角色材质增加 `CustomData → Output Alpha = 1` 的标记节点（封装成 MaterialFunction）
- 后处理材质：把 `SceneTexture: PostProcessInput0` 替换为 **SubpassFetch** 节点（项目自定义）
- 定义 HDR 编解码 MaterialFunction：`EncodeSceneColorRGB10A2 / DecodeSceneColorRGB10A2`，全局复用

### 9.4 Stencil 解码工具节点

```
// HLSL 伪代码 - 在后处理 / Bloom / ToneMap 中使用
half4 SceneCol = SubpassFetch(0);                // RGB = encoded color, A = isCharacter
half  IsChar   = SceneCol.a;                     // 1 = 角色, 0 = 场景
half3 HDRCol   = pow(SceneCol.rgb, 2.0) * Range; // 开根号编码反解
```

### 9.5 通用注意点

- RDG 化项目优先用 `FRDGBuilder` 显式管理 RenderPass / Subpass，便于工具检查 Tile Memory 驻留
- SubpassInput 个数受设备限制 → 项目里 **常态使用 ≤ 3 个**
- 调试用 **RenderDoc Mobile**，盯紧 Render Pass 数量与 Bandwidth 视图
- 在 GPU Profile 里关注 "Tile Memory Stalls" 与 "Late Z" 比例，验证 PreZ 是否真生效

## 10避坑与最佳实践

### 避坑清单

| 坑 | 原因 | 缓解方式 |
| --- | --- | --- |
| HDR Clamp | RGB10A2 是 UNORM | 定点数编码扩 range |
| 低值 Banding（天空球色阶） | 扩 range 后近 0 精度被挤压 | 开根号非线性编码 |
| FBF / SubpassInput 不可用 | OpenGL ES Mali 兼容性 / 设备老旧 | 必须保留传统多 Pass 降级路径 |
| SubPass 材质需要邻域采样 | 美术不知道这个限制 | 提前与美术对齐，列出可用 / 不可用 Node |
| Distortion 下放后特效被扭曲 | 合并阶段后置 | 美术效果 review，标记关键特效用独立队列 |
| InputAttachment 数量超限 | 设备只支持 3~4 个 | 合并 GBuffer / Scene 数据通道，按 UE 4.27 默认 3 个上限设计 |
| Depth Fetch 不可用 | OpenGL ES / Metal 1.x | 用 Memoryless R32F 中转 |
| 无法用 MSAA | Tile Memory 被 GBuffer / SubpassInput 占用 | 改用 FXAA / TAA / FSR |

### 最佳实践

1. **渐进式上线**：RGB10A2 Stencil → 后处理 Fog → SubPass 优化 → One Pass，每一步独立可回滚
2. **双轨管线**：旗舰跑 SubPass 版（保留 SSR/SSAO/Bloom）；中低端跑 One Pass 版
3. **性能监控四件套**：Render Pass 数 / 读带宽 / 写带宽 / BasePass+PostProcess GPU 耗时
4. **分场景测试**：以 50+ 角色同屏 + 多技能特效作为压力基准，避免空场景假阳性
5. **美术 / 程序协作约束**：FBF 限制、Distortion 影响特效、SubPass 材质禁邻域采样要写进美术 wiki


**资料来源**（公开渠道整理，所有数据与方案描述以原作者分享为准）：

[[1] [UFSH2025]《洛克王国:世界》移动端管线设计与优化（朱谷才，腾讯魔方工作室）— 腾讯频道帖](https://pd.qq.com/g/roco135790/post/B_f00c2869382807001441152186774648610X60)
[[2] UF2025(Shanghai)——移动端渲染管线优化实战：《洛克王国：世界》— GameRes 游资网](https://www.gameres.com/916723.html)
[[3] UF2025(Shanghai) 移动端渲染管线优化实战 — 领域圈](https://www.lingyuq.com/news/574163.html)
[[4] 知乎专栏：UF2025 移动端渲染管线优化实战](https://zhuanlan.zhihu.com/p/1999455131893265237)
[[5] 知乎专栏：[UFSH2025]《洛克王国世界》移动端管线设计与优化（演讲整理版）](https://zhuanlan.zhihu.com/p/1977047201223046770)
[[6] UE4/UE5 移动端延迟渲染（可可西，博客园）— FrameBuffer Fetch / Depth Fetch / Memoryless 实现细节参考](https://www.cnblogs.com/kekec/p/17050979.html)
[[7] 《洛克王国世界》手游地形渲染方案逆向笔记（知乎）](https://zhuanlan.zhihu.com/p/2020638581023146049)

报告整理时间：2026-05；引擎版本：UE 4.26；测试基准设备：iPhone X。
本报告仅做技术汇总与二次组织，不代表原作者观点。所有性能数据均由原分享给出，不做单独验证。
