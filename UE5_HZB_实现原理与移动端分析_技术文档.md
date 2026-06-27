# UE5 HZB（Hierarchical Z-Buffer）实现原理与移动端分析

> **文档定位**：系统梳理 Unreal Engine 5 中 HZB（层级 Z 缓冲）的本质、构建算法、消费者与桌面端管线，并重点分析其在移动端（TBDR / Vulkan / Metal）的实现差异与工程代价。
>
> **配套文档**：本项目遮挡剔除细节见 `UE_Mobile_Tech_DeepDive_01_Occlusion.md`（HZB 作为移动端可见性系统的一环，已在该篇有调度时序与 CVar 联动）；TBDR 片上缓存机制见 `UE_Mobile_TBDR_Optimization_TechDoc.md`。本篇与之互补，聚焦 HZB 本体。
>
> **诚信边界**：本文区分 **【官方】**（Epic 官方文档/论坛、Apple/Arm/Qualcomm 一手）、**【社区佐证】**（与源码一致的第三方解析）、**【工程推断】**（基于架构原理的推导）。源码文件名/行号若未在本地引擎核实，均标注 "需本地确认"，不编造常量。

---

## 0. 执行摘要（一页纸 TL;DR）

| 维度 | 结论 |
|------|------|
| **本质** | HZB 是对 SceneDepth 做层级下采样得到的一条 **深度 mip 链（金字塔）**。Mip 0 = 全分辨率，逐级 1/2 降采样。 |
| **两种类型** | **FurthestHZB**（保守最远深度，遮挡剔除/SSR 主用）+ **ClosestHZB**（最近深度，SSGI/screen-space diffuse indirect 按需构建）。 |
| **降采样规则** | 在 UE 的 **Reversed-Z** 约定下（近大远小），"取最远" = **取 min**（不是 max）。这是最易写错的点。 |
| **构建方式** | Compute 与 Pixel Shader 双路径，由 `r.HZB.BuildUseCompute` 切换；Compute 走 single-pass downsampler（一次 dispatch 写多级 mip，思路同 FFX SPD）。 |
| **核心消费者** | ① 遮挡剔除（`FHZBOcclusionTester`，上一帧 HZB 测当前帧 Bounds）② **Nanite two-pass culling**（HZB 是 Nanite 可见性剔除的命脉）③ SSR ④ SSGI/Lumen screen trace ⑤ 体积雾等屏幕空间追踪。 |
| **格式/尺寸** | 单通道 **PF_R16F**；尺寸向下取整到 2 的幂，按 log2 生成 mip 链。 |
| **移动端关键矛盾** | HZB 构建需采样 SceneDepth → 深度必须从 **片上 Tile Memory resolve 到主存**，与 TBDR "深度全程留 tile" 的省带宽哲学直接冲突。这是移动端 HZB 的核心代价。 |
| **移动端互斥** | `bHZBOcclusion = r.HZBOcclusion!=0 && r.Mobile.AllowSoftwareOcclusion==0` —— HZB 遮挡与软件遮挡 **二选一**。 |
| **Nanite on Mobile** | 官方自 **UE 5.5** 起为 **初步/实验性**，仅面向高端机；此前产线多依赖 ARM/高通定制分支或在移动设备上启用桌面渲染器（Vulkan SM5）。 |
| **有回读吗** | 传统 HZB 遮挡 **有回读**（1 帧延迟，写小 RT 下一帧 CPU 读回）；GPU-Driven/Nanite IndirectDraw **无回读**（结果直接写 indirect draw）。硬件 OQ 同样有回读。 |
| **为何降 DrawCall** | **不是剔得更多**（剔除数量与硬件查询几乎相同），而是把"做遮挡测试"从 N 个 per-object 包围盒 draw 压成 **1 次批量纹理采样** → 查询 DC≈0。R15 实测动态物体场景 DC 1450→467、18→30fps。 |
| **行业采用** | 移动端 HZB 遮挡是**少数派**。主流：硬件 OQ（默认）/ 软件遮挡（大世界）。HZB 真正的刚需是 **Nanite**。 |

**一句话决策**：大世界室外/海量动态物体 → 可上 HZB（且需按 GPU 厂商分档，高通慎用）；紧凑室内/中低端机 → 关 HZB，用软件遮挡或纯硬件 OQ。**HZB 不是"开了就赚"，而是换了开销结构（消查询 DC，换 resolve+回读）。**

---

## 1. HZB 是什么：第一性原理

### 1.1 解决的问题

朴素的遮挡测试要么用硬件 Occlusion Query（一帧延迟 + CPU 回读）、要么逐像素比深度（昂贵）。HZB 的核心思想是：

> **把深度缓冲做成 mip 金字塔，用一次低分辨率采样代替一整块区域的逐像素深度比较。**

测试一个物体的屏幕投影包围盒时，根据包围盒的屏幕尺寸选一个合适的 mip level，使得包围盒大致覆盖 1~4 个纹素，只采样这几个纹素的"保守最远深度"，与物体的最近深度比较：若物体比该区域记录的最远深度还远 → 被完全遮挡 → 剔除。

### 1.2 为什么是"保守最远"（Furthest）

下采样时每 2×2（或更大）区域要合并成一个值。对遮挡剔除而言，**必须取这块区域里"最远的那个深度"**：

- 如果取最近，会把本应被遮挡的物体误判为可见？不——关键在于"保守"的方向：遮挡测试比较的是"遮挡物的深度墙"。HZB 记录的是"这片区域里最浅的遮挡覆盖能到多远"。取 **最远深度** 意味着：只有当被测物体比"这片区域所有遮挡物中最远的那个还要远"时才剔除 → **绝不误剔（不会把可见物剔掉）**，宁可漏掉一些本可剔除的（保守）。

**Reversed-Z 陷阱**（核验修正点）：UE 默认启用 Reversed-Z（近处深度值=1，远处=0，提升深度精度）。因此"取最远深度" 在数值上对应 **取 min（更小的值=更远）**。

```
非 Reversed-Z：远 = 深度值大 → Furthest = max()
Reversed-Z（UE 默认）：远 = 深度值小 → Furthest = min()   ← UE 实际走这条
```

> ⚠️ 写文档/读 shader 时务必看清当前是否 Reversed-Z，不要无脑写 `max`。

### 1.3 FurthestHZB vs ClosestHZB 【社区佐证，命名与源码一致】

| 类型 | 降采样取值 | 用途 | 是否默认构建 |
|------|-----------|------|------------|
| **FurthestHZB** | 最远（Reversed-Z 下取 min） | 遮挡剔除、SSR ray march、Nanite cull | ✅ 默认 |
| **ClosestHZB** | 最近 | SSGI / RenderScreenSpaceDiffuseIndirect 等需要"最近遮挡"信息的效果 | ❌ 按需（开启对应效果才构建） |

---

## 2. HZB 构建算法

### 2.1 Compute / Pixel 双路径 【官方论坛证实 CVar】

```
r.HZB.BuildUseCompute = 1  → FHZBBuildCS    （Compute，single-pass 多 mip）
r.HZB.BuildUseCompute = 0  → FHZBBuildPS    （Pixel Shader，逐 mip 多次 draw）
```

- **【官方】** Epic Developer Community 帖子证实：Fortnite 在 Mobile 上默认用 ComputeShader 构建 HZB，并讨论 `r.HZB.BuildUseCompute` 的开启建议。
- **【社区佐证】** 类名 `FHZBBuildCS`/`FHZBBuildPS` 与 UE 源码命名一致（精确文件路径与函数名需本地 `grep` 确认）。

### 2.2 Single-Pass Downsampler（一次 dispatch 多级 mip）

Compute 路径采用 single-pass downsampling：**一次 dispatch 写出多个 mip level**，避免逐级 dispatch 的多次 barrier/带宽往返。

```
                  SceneDepth (Resolve)
                         │  采样 2x2 取 Furthest
                         ▼
   ┌──────────────────────────────────────────┐
   │  FHZBBuildCS  (single dispatch)            │
   │   ├─ RWTexture FurthestHZBOutput_0  (mip0) │
   │   ├─ RWTexture FurthestHZBOutput_1  (mip1) │
   │   ├─ RWTexture FurthestHZBOutput_2  (mip2) │
   │   └─ RWTexture FurthestHZBOutput_3  (mip3) │  ← 社区资料普遍为一次 4 级
   └──────────────────────────────────────────┘
        │ 用 LDS(groupshared) 在组内逐级 reduce
        ▼ 若 mip 数 > 4，再次 dispatch 接续
```

- **【社区佐证】** shader 中存在 `FurthestHZBOutput_0..3` 多个 RWTexture 输出，社区普遍描述为"一次降 4 级 mip"。**"4" 非官方钦定常量**，建议写"每次 dispatch 生成若干级（社区资料普遍为 4）"。
- **【工程推断/类比】** 与 AMD FidelityFX SPD（Single Pass Downsampler）思路同源（一次 dispatch + LDS + 原子计数器协调多级），但 Epic 未官方声明"采用 SPD"，仅作类比。

### 2.3 格式与分辨率 【R16F 官方间接证实】

- **格式**：单通道 **PF_R16F**。**【官方】** Epic 论坛明确提到"R16F UAV 在 Android/GLES 上的兼容性问题（GLES 不支持 16F 的 UAV）"，反证 HZB 输出用 R16F。
- **尺寸**：向下取整到 2 的幂，按 `log2(ViewRect)` 计算 NumMips。**【社区佐证】** 取整确切规则（floor / 其他）需本地确认。

---

## 3. HZB 的消费者：谁在用它

```
                        ┌──────────────┐
                        │   HZB (mip)  │
                        └──────┬───────┘
        ┌───────────┬──────────┼───────────┬──────────────┐
        ▼           ▼          ▼           ▼              ▼
   遮挡剔除      Nanite      SSR        SSGI/Lumen      体积雾
 FHZBOcclusion  two-pass  ray march   screen trace   VolumetricFog
   Tester        culling   加速        (ClosestHZB)
        │           │
   上一帧HZB     命脉级依赖
   测当前帧Bounds
```

### 3.1 遮挡剔除 `FHZBOcclusionTester` 【官方 + 社区】

- **【官方】** `r.HZBOcclusion=1` 启用。Epic 文档："HZB occlusion uses a Mip mapped version of the Scene Depth render target to check the bounds of an Actor"。
- **流程**【社区佐证】：把候选物体的包围盒 center/extent 写入一张纹理 → `FScreenVS` + `FHZBTestPS` 绘制 → 每个物体采样对应 mip 比深度 → 结果回读 CPU。
- **天然一帧延迟**：用 **上一帧** 的 HZB 测 **当前帧** 的 Bounds（HZB 要等当前帧深度渲完才能建，无法当帧自测）。

### 3.2 Nanite two-pass occlusion culling 【多源一致，完全属实】

HZB 是 Nanite 可见性剔除的命脉。两遍流程：

```
┌─ Main Pass ────────────────────────────────────────┐
│ 用【上一帧 HZB】做 instance cull + cluster cull      │
│ → 光栅化通过的部分，写入本帧 VBuffer/Depth          │
└────────────────────────────────────────────────────┘
                      │
                      ▼  用本帧渲染结果构建【新 HZB】
┌─ Post Pass ────────────────────────────────────────┐
│ 用【新 HZB】重新测试 Main Pass 中被剔除的 cluster    │
│ → 补渲染那些其实可见的（修复上一帧 HZB 的误剔）       │
└────────────────────────────────────────────────────┘
```

- **关键细节**【社区佐证】：是把"当前帧的包围盒变换到上一帧空间"去测上一帧 HZB，而**不是**把上一帧 HZB 重投影到当前帧（后者会因像素不连续产生裂缝→误剔）。
- Post Pass 可由 CVar 关闭（只跑 Main Pass）。

### 3.3 其余消费者 【社区佐证】

- **SSR**：HZB 用于屏幕空间反射的 ray marching 加速（在 mip 链上做层级步进，空区域大步跳过）。
- **SSGI / screen-space diffuse indirect**：需要 **ClosestHZB**。
- **Lumen screen trace / 体积雾**：屏幕空间追踪普遍用 HZB 加速（具体 pass 名未逐一核到官方一手，作为业界共识写入）。

---

## 4. 桌面端 HZB 完整管线时序

```
┌─ 帧 N ──────────────────────────────────────────────────────┐
│ InitViews                                                    │
│   └─ FHZBOcclusionTester 用【帧 N-1 的 HZB】测试候选 Bounds   │
│        → 提交测试，结果下一帧回读                             │
│ PrePass / BasePass → 产出本帧 SceneDepth                     │
│ RenderHZB                                                    │
│   ├─ 采样本帧 SceneDepth                                     │
│   ├─ FHZBBuildCS/PS → 构建【帧 N 的 FurthestHZB】            │
│   └─ (若需要) 构建 ClosestHZB                                │
│ Nanite：Main Pass(用帧N-1 HZB) → 建帧N HZB → Post Pass       │
│ SSR / SSGI / Lumen 消费 HZB                                  │
└──────────────────────────────────────────────────────────────┘
              帧 N 的 HZB 留给帧 N+1 的遮挡测试使用
```

桌面端深度本就在显存（IMR 架构，深度天然可被采样），构建 HZB 只是一次 compute 下采样，**几乎无额外架构代价**。这与移动端形成鲜明对比（见下节）。

---

## 5. 移动端 HZB 实现：核心矛盾与代价

### 5.1 第一性矛盾：HZB 要采样深度 ⊥ TBDR 要深度留 tile

移动 GPU 是 **TBDR / TBR** 架构，渲染哲学是"深度/模板尽量留在片上 Tile Memory（Memoryless），永不写回主存以省带宽"。但 HZB 构建必须用 compute/纹理采样去**读** SceneDepth，而 compute 无法访问尚在 tile 内的深度 → **深度必须先从片上 resolve 到主存（system memory）**。

> **这是移动端 HZB 的根本代价**：开 HZB ⇒ 强制一次 SceneDepth resolve，与 TBDR 省带宽的初衷直接冲突。**【工程推断，有架构原理支撑】**——"必须 resolve"是 TBDR 的必然结论，非 Epic 明文。

本项目 `UE_Mobile_Tech_DeepDive_01_Occlusion.md` 第 6 节已记录该联动：

```cpp
// MobileShadingRenderer.cpp（行号需本地确认）
bKeepDepthContent =
    bRequiresMultiPass || bForceDepthResolve || ...
    || bShouldRenderHZB    // ← 开 HZB 必须保留深度（出 Tile）
    || bHZBOcclusion;      // ← 开 HZB Occlusion 同样要求
```

### 5.2 移动端 HZB 遮挡与软件遮挡互斥

```cpp
bool bHZBOcclusion = r.HZBOcclusion != 0
                  && r.Mobile.AllowSoftwareOcclusion == 0   // ← 互斥
                  && !ViewFamily.EngineShowFlags.SimpleSceneRendering;
```

移动端遮挡剔除三选一格局：
1. **硬件 OcclusionQuery**（默认，BasePass 末尾发，利用深度仍在 tile 内）
2. **HZB Occlusion**（需 depth resolve，视锥外物体也能 GPU 批量测，CPU setup 开销低）
3. **SoftwareOcclusion**（CPU 软光栅化，避开 resolve 与 driver bug，但吃 CPU）

### 5.3 移动端 HZB 构建的额外坑

- **R16F UAV 兼容性**【官方论坛】：Android GLES 后端不支持 16F 的 UAV，因此 `r.HZB.BuildUseCompute` 在部分 GLES 机型上不能开，需回退 Pixel Shader 路径或在 Vulkan 后端才用 compute。Fortnite Mobile 默认 compute，但有机型适配前提。
- **PrePass 与 HZB 的关系**：移动端若开 `r.Mobile.EarlyZPass` 做完整 PrePass，深度已知，硬件 OQ 会被跳过（`!bIsFullDepthPrepassEnabled`），此时遮挡更可能交给 HZB / 软件遮挡。
- **`r.HZB.IndirectDraw` / `r.Mobile.AllowHZB`**【需本地确认】：符合 UE CVar 命名习惯且社区提及，但精确拼写/默认值请在引擎里 `grep -r` 确认后落笔。

### 5.4 Vulkan ↔ Metal 机制对照

| 机制 | Android / Vulkan | iOS / Metal | 对 HZB 的影响 |
|------|-----------------|-------------|--------------|
| 深度 resolve | `VK_ATTACHMENT_STORE_OP_STORE` / 显式 resolve | `MTLStoreActionStore` / `storeAndMultisampleResolve` | 开 HZB 必须 STORE 深度，放弃 Memoryless |
| Memoryless 深度 | `LAZILY_ALLOCATED` + `STORE_OP_DONT_CARE` | `MTLStorageModeMemoryless` | 不开 HZB 时深度可全程留 tile |
| Compute 读深度 | Vulkan compute 采样 resolved depth texture | Metal compute 采样 resolved depth | 都需先 resolve；Metal 上可考虑 `MTLHeap` 复用 |
| 16F UAV / 存储纹理 | Vulkan 支持 R16F storage image | Metal 支持 R16Float read_write texture | GLES 不支持 → 移动端 compute HZB 优先 Vulkan/Metal |
| 单 pass 下采样 | compute + groupshared(LDS) | compute + threadgroup memory | 两端均可实现 SPD 式 single-pass |

> **Apple TBDR 特别说明**：Apple GPU 有 HSR（Hidden Surface Removal）硬件遮挡，Opaque 物体的 PrePass 收益本就低。在 iOS 上是否值得为 HZB 付出 depth resolve，需结合具体场景的远距剔除收益评估——很多 iOS 项目宁可不开 HZB 遮挡。

### 5.5 Nanite on Mobile 的版本现实 【官方，重点修正】

| 节点 | 事实 |
|------|------|
| UE 5.0~5.4 | 官方 mobile 渲染器 **不支持** Nanite，需回退传统 LOD |
| **UE 5.5** | 引入移动端 **初步/实验性** Nanite 支持，仅面向 **高端机型** |
| 产线现实 | ARM "Mori" demo = 改过的 UE 5.5.2 + **桌面渲染器** + Vulkan SM5（Mali-G925）；高通 ProjectOne = 基于 5.4 的自定义分支 + 骁龙 Nanite 改动 |

> 即：移动端跑 Nanite（及其依赖的 HZB two-pass culling）目前多是"在移动设备上启用桌面渲染器（Vulkan SM5）"或厂商定制分支，而非走标准 mobile forward 渲染器。

---

## 6. 决策矩阵：移动端是否开 HZB

| 场景 | 建议 | 是否改引擎 | 理由 |
|------|------|-----------|------|
| 大世界 / 室外开阔 | **HZB ON** | 否（CVar 配置） | 远距遮挡剔除收益 > depth resolve 带宽 |
| 紧凑室内 / 走廊 | HZB OFF + SoftwareOcclusion | 否 | 遮挡关系简单，省 resolve；CPU 软遮挡更稳 |
| 中低端机型 | HZB OFF | 否 | 带宽敏感，resolve 代价伤帧 |
| FPS/TPS 近物 | HZB ON + `r.NeverOcclusionTestDistance` | 否 | 近物免测避免闪烁 |
| VR / MultiView | HZB OFF | 否 | 多视口 HZB 复杂，收益不稳 |
| 自定义 RenderPass 编排 HZB 留 tile | —— | **是（fork 源码）** | 标准引擎无法让 HZB 复用片上深度，需改 RDG/RenderPass 编排（长期负债，升版 re-merge） |
| Nanite on Mobile | 启用桌面渲染器/厂商分支 | **是** | 标准 mobile 渲染器 5.5 前不支持，5.5 仍实验性 |

---

## 7. 配置速查（ASCII 直引号，可直接贴 .ini）

```ini
; ---- 大世界开放场景（HZB 遮挡）----
[/Script/Engine.RendererSettings]
r.HZBOcclusion=1
r.HZB.BuildUseCompute=1          ; 仅 Vulkan/Metal；GLES 机型需回退 0
r.Mobile.AllowSoftwareOcclusion=0
r.NeverOcclusionTestDistance=2000
r.Mobile.AdrenoOcclusionMode=1   ; Adreno GLES 强制 flush 修延迟

; ---- 紧凑室内 / 中低端机（关 HZB 用软件遮挡）----
r.HZBOcclusion=0
r.Mobile.AllowSoftwareOcclusion=1
r.NeverOcclusionTestDistance=1000

; ---- 单房间 / Cutscene（关剔除省 CPU）----
r.AllowOcclusionQueries=0
```

> 注：`r.HZB.IndirectDraw`、`r.Mobile.AllowHZB` 等请在本地引擎 `grep -r "HZB"` 核实拼写与默认值后再写入正式配置。

---

## 8. 落地 Checklist

- [ ] 确认当前是否 Reversed-Z（决定 Furthest 取 min 还是 max）。
- [ ] 目标机型后端：Vulkan/Metal 可开 compute HZB；GLES 回退 Pixel 路径。
- [ ] 评估 depth resolve 带宽：用 RenderDoc/Xcode GPU 抓帧看 SceneDepth 是否因 HZB 被强制 STORE。
- [ ] HZB 遮挡 vs 软件遮挡：根据场景遮挡复杂度二选一，勿同时开。
- [ ] 近物闪烁：FPS/TPS 配 `r.NeverOcclusionTestDistance`。
- [ ] 内存：HZB = R16F + mip chain，全屏 ~数 MB，关闭无用 mip。
- [ ] Nanite on Mobile：确认引擎版本 ≥5.5 且为高端机，或评估桌面渲染器方案。
- [ ] 本地核实：`HZB.cpp`/`HZB.usf` 路径、一次 dispatch 的 mip 数、CVar 拼写。

---

## 9. 移动端性能负担与行业采用现状（实测归因）

> 本章回答三个高频问题：**① 移动端开 HZB 到底有没有性能负担？② HZB 有回读吗？③ HZB 为什么能降 Draw Call？** 数据来自 GWB 腾讯创意游戏合作计划《如何在 UE4 移动端实现 HZB》(2020) 的真机实测，**【社区一手实测】**。

### 9.1 一句话结论

> **移动端 HZB 不是"开了就赚"的免费午餐。它不会比硬件查询剔掉更多物体，而是把"做遮挡测试"这个动作的开销结构换掉了——消除查询 Draw Call，换来 depth resolve + GPU 回读两笔固定成本。因此它强依赖 GPU 硬件档位，且行业里远不如硬件查询/软件遮挡普及。真正绕不开 HZB 的是 Nanite。**

### 9.2 移动端 HZB 的性能负担：三笔账

| 开销项 | 来源 | 真机数据（OPPO R15 / R17） | 备注 |
|--------|------|---------------------------|------|
| **① Depth Resolve 带宽** | 构建 HZB 须采样 SceneDepth，深度被迫从片上 Tile resolve 到主存 | 无独立数字，TBDR 架构固有代价 | 与"深度全程留 tile 省带宽"哲学冲突；中低端机最敏感 |
| **② HZB 构建 + 测试 GPU 耗时** | 逐级下采样 + 包围盒批量采样（采样 16 像素） | R15: 构建 2.6ms + 测试 1.6ms；R17: 构建 0.48ms + 测试 0.41ms | 不同 GPU 架构差异巨大 |
| **③ GPU 回读耗时** | 上一帧结果回读到 CPU（传统 UE4 式 HZB 遮挡） | 裸 glReadPixels：R15 6~8ms / R17 16~20ms；**PBO 双缓冲异步优化后**：R15 0.9ms / R17 4~6ms | 回读是移动端最大的坑，必须 PBO 优化 |

**机型分化（关键）**：R15（非高通）回读快、构建慢；R17（高通）回读慢、构建快。UE4 对高通设备把硬件查询上限砍到 **510 次**（其他设备 4000，推荐值 250 / 2000），导致 R17 上硬件查询本身已被"阉割"，HZB 的相对优势随之消失。**→ HZB 在移动端必须按 GPU 厂商分档决定是否开启。**

### 9.3 回读机制：HZB 有回读吗？

**有，但要分两种路径：**

| 路径 | 是否回读 | 延迟 | 用在哪 |
|------|---------|------|--------|
| **传统 HZB 遮挡（UE4 移动移植 / FHZBOcclusionTester）** | ✅ 有回读 | 1 帧 | 逐物体遮挡剔除；结果写小 RT，下一帧 CPU 读回再决定可见性 |
| **GPU-Driven / Nanite IndirectDraw** | ❌ 无回读 | 0（当帧自洽） | 剔除结果直接在 GPU 上写成 indirect draw 参数喂给光栅器，CPU 全程不参与 |

- 硬件 OcclusionQuery 同样有回读、同样 1 帧延迟——这是它和传统 HZB 的共性，也是"快速转身物体 popping"的根因。
- 传统 HZB 的回读数据量远小于硬件查询（一张打包结果 RT vs N 个 query），配合 **PBO 双缓冲异步读取**（buffer1 异步 glReadPixels / buffer2 glMapBufferRange，逐帧交换）可把回读从 ~8ms 压到 ~1ms。
- **`r.HZB.IndirectDraw=1`** 走的就是无回读路径——这也是移动端 HZB 的进化方向。

### 9.4 HZB 为什么能降 Draw Call（纠正常见误解）

> **误区**：HZB 降 DC 是因为它剔掉的物体更多。
> **真相**：实测 HZB 与硬件查询的剔除数量几乎相同（被遮挡 570 vs 579），**HZB 降的不是渲染 DC，而是"为做遮挡测试而额外付出的那部分 DC"。**

**OPPO R15，1 万随机物体，动态物体场景：**

| 指标 | 仅硬件查询 | 仅 HZB |
|------|-----------|--------|
| Occlusion Queries | 987 | ≈ 0 |
| **总 Draw Call** | **1450** | **467** |
| 被遮挡 / 可见 | 579 / 411 | 570 / 420 |
| **帧率** | **18 fps** | **30 fps** |

**机理**：
- **硬件查询**：测 N 个物体 = 发 N 个"包围盒 draw call"问 GPU。**动态物体无法 Batch**（Batch 只对静态物体生效），987 个查询直接把总 DC 顶到 1450——**测试动作本身成了 DC 的大头**。
- **HZB**：把所有物体包围盒打包进纹理，**一次** pass 批量采样 HZB 比深度，per-object 查询 draw 全部消失 → 查询 DC ≈ 0。

**静态物体场景**：硬件查询的 Batch 生效（OQ 仅 58、DC 511、35fps），此时 HZB（33fps）并无优势——**所以 HZB 的价值集中在"海量动态物体"。**

> 结论：HZB 降 DC = **把"测试"从 N 个 draw 压成 1 次批量采样**；"被遮挡物不渲染省下的渲染 DC"是所有遮挡方案共有的收益，非 HZB 独占。

### 9.5 行业采用现状：其他手游在用吗？

**移动端 HZB 遮挡是少数派。** 主流分场景选型如下：

| 方案 | 谁在用 / 适用场景 | 为何这样选 |
|------|------------------|-----------|
| **硬件 OcclusionQuery（默认）** | 绝大多数中小型 / 静态为主手游 | UE 移动端开箱默认；静态 Batch 后开销可控 |
| **软件遮挡 SoftwareOcclusion** | 大世界 / 大量遮挡物开放世界（腾讯系大地图/战场项目常用） | CPU 软光栅，避开 GPU 回读延迟与 Adreno driver bug；代价是吃 CPU |
| **自研 HZB 移动移植** | 少数有引擎团队、且**海量动态物体**、主力机非高通的项目 | 需自解决 depth resolve + PBO 异步回读 + 机型分档 |
| **HZB 服务于 Nanite** | UE5.5+ 高端机 Nanite，或 ARM "Mori" / 高通 ProjectOne 等 Demo/定制分支 | Nanite two-pass culling 离不开 HZB——这是 HZB 在移动端真正的刚需 |

**为何多数项目不用 HZB 遮挡**：① 官方 stock 引擎移动端长期未把 HZB 遮挡做成开箱即用（本项目 `UE_Mobile_Tech_DeepDive_01_Occlusion.md` 中 `RenderOcclusion(FRDGBuilder&)` 仍标 `// GR ADD Begin`，是自行 patch）；② TBDR 上 depth resolve 逆架构，剔除收益常被带宽吃掉；③ GPU 回读一帧延迟 + 高通/联发科/Mali 行为碎片化，适配成本高、性价比低。

---

## 10. 参考资料

**官方**
- Epic — Visibility and Occlusion Culling in Unreal Engine：https://docs.unrealengine.com/5.1/en-US/visibility-and-occlusion-culling-in-unreal-engine
- Epic Developer Community — Mobile HZBBuild ComputeShader（`r.HZB.BuildUseCompute`、R16F UAV/GLES 问题）：https://forums.unrealengine.com/t/mobile-hzbbulid-computeshader/2678262
- Epic — Software Occlusion / HZB occlusion 文档：https://docs.unrealengine.com/latest/INT/Engine/Rendering/VisibilityCulling/

**Nanite / two-pass culling（社区佐证，多源一致）**
- Nanite 绘制管线解析：https://wingstone.github.io/posts/2026-05-16-ue中nanite绘制管线解析/
- UE5 Nanite 原理：https://neozheng.cn/2023/08/24/UE5%20Nanite%20原理/
- Two-pass occlusion culling（Reversed-Z 取 min/max 说明）：https://www.caiqinyi.cn/index.php/2026/01/04/two_pass_occlusion_culling

**HZB 源码解析（社区）**
- HZB 构建/FHZBOcclusionTester 源码解析：https://blog.csdn.net/kuangben2000/article/details/143274440
- 腾讯 IEG — 如何在 UE4 移动端实现 HZB：https://www.toutiao.com/article/6870037893318967811
- **GWB 腾讯创意游戏合作计划 — 如何在 UE4 移动端实现 HZB（含 R15/R17 真机实测：构建/测试/回读耗时、HZB vs 硬件查询 DC 对比、PBO 异步回读优化、高通查询上限 510）**：https://www.gameres.com/874887.html

**Nanite on Mobile / 厂商方案**
- UE 5.5 Nanite/Lumen advancements（移动端初步支持）：https://ayadog.com/unreal-engine-nanite-lumen-advancements
- ARM Mori → Nanite demo（UE 5.5.2 + 桌面渲染器 + Vulkan SM5）：ARM Community Blogs

**类比参考**
- AMD FidelityFX Single Pass Downsampler (SPD)：GPUOpen 文档

---

## 11. 免责声明

- 本文部分源码文件名/CVar 拼写/常量（如"一次 4 个 mip"）来自社区资料，**未逐一在本地 UE5 源码核实**，正式落地前请在引擎目录 `grep` 确认，文中已逐处标注"需本地确认"。
- 移动端"HZB 必须 resolve 深度"为基于 TBDR 架构的工程推断，非 Epic 官方明文。
- 第 9 章 R15/R17 真机数据来自 GWB 2020 年公开文章（UE4 时代），具体数值随引擎版本/机型会变，仅用于说明"开销结构"与"降 DC 归因"的定性规律，不代表 UE5 当前绝对性能。
- 引擎版本节点（Nanite on Mobile 自 5.5）以 Epic 官方 release note 为准，不同小版本特性可能调整。

> **文档完。** 配套阅读：`UE_Mobile_Tech_DeepDive_01_Occlusion.md`（移动端遮挡调度时序）、`UE_Mobile_TBDR_Optimization_TechDoc.md`（TBDR 片上缓存）。
