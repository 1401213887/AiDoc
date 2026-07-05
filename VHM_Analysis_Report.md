# VirtualHeightfieldMesh 插件分析报告

# VirtualHeightfieldMesh 插件分析报告

UE5EA · Engine/Plugins/Experimental · 代码 + P4 提交历史 · 生成于 2026-05-20 22:24

depot: //GR/trunk/UE5EA
RVT WorldHeight
GPU Quadtree LOD
Indirect Draw
Experimental

66

P4 提交数

2024/01 → 2026/03

12

源码文件

USF/USH + C++

3

渲染 Pipeline

v1 / v2 / v3

31

郭智均提交

主要贡献者

2

Shader 路径

MultiPass + OnePass

Sm6

OnePass 要求

Persistent threads

📘 代码作用分析

🕒 P4 提交历史

## VirtualHeightfieldMesh（VHM）插件源码技术分析报告

> 目标目录：`UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/`
> 分析对象：12 个核心源码文件（4 个 USF/USH Shader + 8 个 C++/H 文件）
> 分支：含腾讯 `S1_Engine_Shiyu` / `Engine CYH` / `shiyu` / `JLP` 等定制片段

---

### 1. 顶层概述

#### 1.1 VHM 解决什么问题

`VirtualHeightfieldMesh`（以下简称 VHM）是 UE5 实验阶段的一个**大规模可流式高度场地形渲染插件**。它把 UE 自带的 **Runtime Virtual Texture（RVT）的 `WorldHeight` 类型** 当作权威高度数据源，再在 GPU 端**即时构造一张顶点驱动的 mesh**，从而把整个 RVT 覆盖范围渲染为一张连续的高度场地形。它的核心目标是替代/补充传统 `Landscape` 在大世界场景下的几何 LOD/裁剪/绘制路径：

- **数据完全 GPU 驻留**：不需要 CPU 顶点缓冲，几何由 Compute Shader 动态生成 quad 实例，再由 `DrawIndexedInstancedIndirect` 绘出。
- **基于 RVT page table 的 LOD/可见性决策**：使用 RVT 的 page table + physical texture 直接采样高度，所以 LOD 颗粒度与 VT mip/页对齐，可以做到无缝流送。
- **支持极大世界**：通过 Morton 排序 + 基于 quadtree 的递归细分，结合自适应距离 LOD 与 morph，平滑过渡到远处。
- **与 Nanite 互斥**（项目定制逻辑）：在非编辑器构建中，如果 `r.Nanite` 与 `landscape.RenderNanite` 都开启，则强制关闭 VHM；只有 Nanite 不可用时才使用 VHM 走老路径。

#### 1.2 与 Landscape / RVT 的关系

- **Landscape**：传统地形组件，CPU 维护 `LandscapeComponent` 网格、Hierarchy LOD、Section、Brush。VHM 完全不复用 Landscape 的几何，只把 Landscape（或任何写入 RVT 的源 Primitive）作为**RVT 高度图的写入者**。
- **Runtime Virtual Texture**：VHM 强依赖 RVT 的 `WorldHeight` MaterialType。它通过 `URuntimeVirtualTexture::GetAllocatedVirtualTexture()` 拿到 `IAllocatedVirtualTexture`，从中获得：
- `PageTableTexture`：虚拟地址 → 物理地址映射表
- `PhysicalTextureSRV`：物理高度（线性 R 格式）
- `VTPackedUniform / VTPackedPageTableUniform`：VT 标准查表参数
- **Mask（项目定制）**：除官方 MinMax 外，腾讯定制版还引入了一张 `MaskTexture` 与可选 `MaskRVT`，用来标识可绘地表 vs. 镂空区域，并支持单独的 `HoleMaterial` 用于空洞处的渲染（例如挖洞、岩浆口、特殊地表）。

#### 1.3 一句话总结

> VHM = "从 RVT 高度页表→GPU quadtree 细分→裁剪剔除→indirect 实例 draw" 的一条几乎全 GPU 驻留的地形渲染管线，外加一套编辑器侧"高度 MinMax / Mask"预烘焙工具。

---

### 2. 模块结构

插件根目录下分两大块：`Shaders/` 与 `Source/`。`Source/` 又分为运行时模块 `VirtualHeightfieldMesh` 与编辑器模块 `VirtualHeightfieldMeshEditor`。

#### 2.1 Shaders（4 个文件）

| 文件 | 角色 |
| --- | --- |
| `Shaders/Private/HeightfieldMaskRender.usf` | （定制）Mask 纹理 mip 下采样 CS：把 RVT 输出的某通道 mask（A/B/R）2×2 合并到下一级 mip，规则是"全 0→0；全 1→1；混合→max(0.5, 各非 01 值)" |
| `Shaders/Private/VirtualHeightfieldInitBuffers.usf` | 一帧最开始把所有 GPU 工作缓冲（队列、indirect args、instance args、stat buffer 等）清零、初始化第一帧默认 quad 的 CS |
| `Shaders/Private/VirtualHeightfieldMesh3.usf` | VHM v3 的核心 quadtree 细分管线：`FillLevel4QuadCS` → `CollectSubdivideQuadsCS` （多 pass）→ `CollectQuadsOnePassCS`（持久线程版本）→ `CullQuadsAndGenerateInstancesCS` |
| `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` | 真正画 mesh 的 VS 端 Vertex Factory：从 `InstanceBuffer` 解出每个 quad，再用 `VertexId` 在 `GRID_SIZE × GRID_SIZE` 网格上展开、做 CDLOD morph、采样 RVT 高度纹理生成最终 world position |

> 注：代码里还引用了 `VirtualHeightfieldMesh.usf` 与 `VirtualHeightfieldMesh.ush`（v1 的 InitBuffers/CollectQuads/CullInstances/InitInstanceBuffer），但本次任务清单不包含这两个 v1 shader 文件，本报告对其作用按 C++ 端 `IMPLEMENT_GLOBAL_SHADER` 推导。

#### 2.2 Runtime（`VirtualHeightfieldMesh` 模块，6 个文件）

| 文件 | 角色 |
| --- | --- |
| `Public/VirtualHeightfieldMeshComponent.h` | `UVirtualHeightfieldMeshComponent`：玩家在 Actor 上挂载的入口组件，UProperty + 资源引用 |
| `Public/HeightfieldMinMaxRender.h` | 暴露 `DownsampleMinMaxAndCopy` / `GenerateMinMaxTextureMips` / `DownsampleMaskAndCopy` / `GenerateMaskTextureMips` 工具函数给编辑器模块用 |
| `Private/HeightfieldMinMaxRender.cpp` | 上述工具函数的实现，包含 MinMax 纹理多 mip 下采样的多套 CS 类型特化（R16→RG16、RG16→RG16、RG16→RGBA8 texel、RGBA8→RGBA8）以及 Mask 下采样 CS（RGBA8\_A/RGBA8\_B/R8） |
| `Private/VirtualHeightfieldMeshSceneProxy.cpp` | **整个插件的中枢**（3052 行）：`FVirtualHeightfieldMeshSceneProxy`（场景代理），`FVirtualHeightfieldMeshRendererExtension`（每帧 hook 渲染器），所有 GPU pass 的 RDG 组装，v1/v2/v3 多套 SubmitWork 实现，遮挡查询，shader 参数 uniform 等 |
| `Private/VirtualHeightfieldMeshEnable.cpp` | 控制台变量 `r.VHM.Enable` / `r.VHM.Visualize` / `r.VHM.EnableExtSubdivisionLevel` 的 Sink，`IsEnabled()` 查询，与 Nanite 的互斥逻辑（项目定制） |
| `Private/VirtualHeightfieldMeshVertexFactory.h` / `.cpp` | `FVirtualHeightfieldMeshVertexFactory`：声明两组 Uniform Buffer（`VHM`：每代理静态参数；`VHMInst`：每帧实例 buffer），生成 morton 序索引缓冲，注册 VF 类型给材质编译系统 |

#### 2.3 Editor（`VirtualHeightfieldMeshEditor` 模块，1 个文件）

| 文件 | 角色 |
| --- | --- |
| `Private/HeightfieldMinMaxTextureBuild.cpp` | 编辑器侧"Build MinMax Texture"和"Build Mask Texture"按钮背后的逻辑：逐 tile 调用 RVT 的 `RenderPagesStandAlone` 把高度/mask 渲染出来 → 用前述 CS 工具函数下采样 → 拷回 CPU → 存成 `UHeightfieldMinMaxTexture` / `UTexture2D` 资产 bulk data |

---

### 3. 关键类与数据流

#### 3.1 `UVirtualHeightfieldMeshComponent`（`VirtualHeightfieldMeshComponent.h:20`）

游戏侧入口 `UPrimitiveComponent`，关键属性：

- `VirtualTexture`（`TSoftObjectPtr<ARuntimeVirtualTextureVolume>`）：绑定的 RVT 体积，必须是 `WorldHeight` 类型才会真正构造 vertex factory。
- `MinMaxTexture` / `NumMinMaxTextureBuildLevels`：高度 MinMax 包围盒纹理（用于 GPU frustum cull / occlusion volume）。
- `MaskTexture` / `MaskRVT` / `MaterialTypeForMask`（定制）：mask 纹理 + 用哪种 RVT 材质类型烘焙。
- `Material`：主表面材质（要求材质 `UsedWithVirtualHeightfieldMesh` 勾选）。
- `HoleMaterial`（定制 `UMaterialInstance*`）：mask 不为 1 的 quad 用此材质画洞。
- `Lod0ScreenSize` / `Lod0Distribution` / `LodDistribution` / `Lod0LevelBias` / `LodBiasScale`：LOD 距离计算参数。
- `NumQuadPerTileOfTwo`（默认 4，即 16×16 quad/tile）：每个 instance 网格的 quad 数（边长 = `1<<NumQuadPerTileOfTwo`）。
- `NumOcclusionLods` / `NumForceLoadLods`：遮挡 / 强制加载层数。
- `ExtSubdivisionLevel`（定制，0~3）：在原 RVT mip 之外再额外细分几层，用于近处加密三角形。

它本身不持有几何，`CreateSceneProxy()` 实例化 `FVirtualHeightfieldMeshSceneProxy`，所有 RHI/RDG 工作都在渲染线程完成。

#### 3.2 `FVirtualHeightfieldMeshSceneProxy`（`VirtualHeightfieldMeshSceneProxy.cpp:755`）

继承 `FPrimitiveSceneProxy`。构造时把 component 的所有参数与 RVT 资源镜像到代理：

- `RuntimeVirtualTexture` / `AllocatedVirtualTexture`（在 `CreateRenderThreadResources()` 中拿）。
- 计算几何相关常量（`SceneProxy.cpp:875~921`）：
- `TileSize = Log2(VT.TileSize)`
- `NumQuadsPerTileOfTwo = min(component值, TileSize+ExtSubdivisionLevel-1)`
- `RVTMinLevel = NumQuadsPerTileOfTwo`
- `MaxLevel = Log2(VT.TileCount) + NumQuadsPerTileOfTwo + ExtSubdivisionLevel`
- `NumInstanceVertexSide = 1 << (TileSize - NumQuadsPerTileOfTwo)`
- 用上述参数构造 `FVirtualHeightfieldMeshVertexFactoryParameters` 并创建 vertex factory。
- 在 `GetDynamicMeshElements` 中**为每个 view**请求一份 `FDrawInstanceBuffers`（异步填充），并发出**两个 `FMeshBatch`**（普通材质 + 空洞材质，分别使用 `IndirectArgsOffset = 0` 和 `5 * sizeof(uint32)`，对应 instance args buffer 中两段不同的 indirect 参数）。

#### 3.3 `FVirtualHeightfieldMeshRendererExtension`（`VirtualHeightfieldMeshSceneProxy.cpp:487`）

这是一个 `TGlobalResource` 的渲染器扩展，挂在 `GEngine->GetPreRenderDelegateEx()` 上：

- `AddWork(...)`：在 `GetDynamicMeshElements` 期间被调用，登记 (Proxy, MainView, CullView) 并复用一组 `FDrawInstanceBuffers`（实例 buffer + indirect args buffer + 定制 hole instance buffer）。
- `BeginFrame(GraphBuilder)`：渲染开始时根据 `r.VHM.Version`（1/2/3）选择 `SubmitWork` / `SubmitWork_V3`（V2 已经被注释掉）。
- `EndFrame()`：清空登记表，回收 4 帧未用的 buffer。

V3 的 GPU 流水线是当前主路径（`SubmitWork_V3`），它使用 `VirtualHeightfieldMesh3.usf` 中的所有 CS。

#### 3.4 `FVirtualHeightfieldMeshVertexFactory`（`VirtualHeightfieldMeshVertexFactory.h:80`）

- 没有真实顶点 buffer，只注册一个 `NullVertexStream`（`ManualFetch`）。
- 唯一的 IndexBuffer 是按 **morton 序**生成的 `NumQuadsPerSide × NumQuadsPerSide × 6` 个三角形索引（`FVirtualHeightfieldMeshIndexBuffer`，`VertexFactory.cpp:73`），morton 顺序提升 vertex cache reuse 至 ~75%。
- `VHM` UB 提供 PageTable / Height / LodBias / VT pack uniform / `MaxLod` / `RVTMinLevel` 等不变量。
- `VHMInst` UB 在每个 view 重新创建（`UniformBuffer_SingleFrame`），承载本帧的 `InstanceBuffer` SRV。
- `ShouldCompilePermutation`：要求材质 `MaterialDomain==MD_Surface && bIsUsedWithVirtualHeightfieldMesh`，或 `bIsSpecialEngineMaterial`。

`VirtualHeightfieldMeshVertexFactory.ush` 里的 `GetVertexFactoryIntermediates` 是真正逐顶点跑的逻辑：

1. `Input.InstanceId` → `QuadRenderInstance`（`Pos`/`Level`/`PhysicalAddress`）。
2. `Input.VertexId` → 在 `GRID_SIZE × GRID_SIZE` 中的 (X,Y) → `LocalUV`。
3. 把 `Pos+LocalUV` 缩放到 RVT 的 page-space → 得到 `NormalizedPos`。
4. **CDLOD morph**：以 view origin 距离算出 `LodForDistance`，再减去 `LodBiasTexture` 得 `LodClamped`，按 `MorphFloor + MorphFrac` 把顶点向下一层 LOD 网格捕捉，避免 LOD 边缘的 cracking。
5. 用 `VTPageTableTexture + PhysicalTextureSize` 双层采样高度（`Height0` 和 `Height1` 加 lerp），得到平滑高度。
6. 把 `(NormalizedPos, Height)` 用 `VirtualHeightfieldToLocal` 映射到 primitive local space。

#### 3.5 HeightfieldMinMax 系列

由两层组成：

- **资源**：`UHeightfieldMinMaxTexture`（在引擎其他模块里定义，被 component 的 `MinMaxTexture` 字段引用）保存一张 RGBA8 纹理（前 16 bit min、后 16 bit max）+ CPU 端 `TextureData`（`TArray<FVector2D>`）+ `TextureDataMips`，前者用于 GPU cull，后者用于 CPU 端构造 `OcclusionVolumes`。
- **构建工具**（`HeightfieldMinMaxRender.cpp`）：
- `DownsampleMinMaxAndCopy(...)`：对一个 R16 高度 tile 走「R16→RG16 → 一连串 RG16→RG16 mip 下采样 → 最后 RG16→RGBA8 texel 写到目标位置」的 ladder。
- `GenerateMinMaxTextureMips(...)`：对最终 RGBA8 MinMax 纹理就地补 mip。
- 定制版同样为 mask 纹理实现了 `DownsampleMaskAndCopy` / `GenerateMaskTextureMips`。
- **运行时使用**：在 GPU cull pass（`CullInstancesCS` / `CullQuadsAndGenerateInstancesCS`）中以 quad 的 `(UV0, UV1)` 采样 MinMax 纹理得到 `(MinHeight, MaxHeight)`，构造 AABB 与 frustum 5 平面做 AABB-平面相交测试。
- **CPU 侧 OcclusionVolumes**（`SceneProxy.cpp:1136`）：用低 mip 的 MinMax 数据生成一组 `FBoxSphereBounds`，作为 `GetOcclusionQueries` 的返回值，由 UE 标准遮挡查询机制硬件加速测试。结果再 `AcceptOcclusionResults` 上传成 `OcclusionTexture` 喂给 `CollectQuadsCS`。

---

### 4. Shader 流程（GPU 流水线）

VHM 同时存在 v1 与 v3 两套 GPU 管线，由 CVar `r.VHM.Version` 选择。任务清单中提供的 4 个 USF/USH 都属于 v3 路径，下面以 v3 为主线说明。

#### 4.1 整体 pipeline（v3 = `SubmitWork_V3`）

```
[每帧入口 BeginFrame]
   │
   ▼
(1) AddPass_TransitionAllDrawBuffers   把 InstanceBuffer/IndirectArgsBuffer 切到 UAV Write
   │
   ▼
(2) AddPass_InitAllBuffers   ← VirtualHeightfieldInitBuffers.usf : InitAllBuffersCS
       清零 RWQueueInfo / FinalArgsBuffer / DispatchArgsBuffer1/2 / InstanceArgsBuffer / StatBuffer
       条件性清空 VTFeedbackBuffer
   │
   ▼
(3) AddPass_FillLevel4Quad_CS   ← VirtualHeightfieldMesh3.usf : FillLevel4QuadCS
       64 个线程一次性把"层级 = MaxLevel-3"的 64 个 root quad
       按 morton 序写入 SubdivideQuadBuffer，并把 OutDispatchArgsBuffer 设为 (2,1,1,64)
       （= 2 个 group × 32 线程 = 64 quad）
   │
   ▼
(4) [循环 N 次] AddPass_CollectSubdivideQuads_CS
                ← VirtualHeightfieldMesh3.usf : CollectSubdivideQuadsCS
       每个 quad 一个线程：采样高度，计算 MinDistanceLod，
       做 frustum + mask + min-max AABB 剔除；
       若 Lod < quad.Level 则细分成 4 个子 quad 写入下一帧 SubdivideQuadBuffer，
       否则写入 FinalQuadBuffer。
       同时把 PageTableFeedback 写到 RWFeedbackBuffer 通知 VT 系统加载缺页。
       buffer ping-pong：用 `CalTime % 2` 区分本 pass 输入/输出。
       Indirect dispatch：dispatch args 由上一 pass 累加得到。
   │
   ▼
(5) AddPass_CullQuadsAndGenerateInstances_CS
       ← VirtualHeightfieldMesh3.usf : CullQuadsAndGenerateInstancesCS
       对 FinalQuadBuffer 做最终 cull（cull 已经做过的话还会再判一次 mask）；
       根据 mask opacity 把 quad 分流到：
         · QuadInstanceBuffer       （mask=1 → 普通材质实例）
         · HoleQuadInstanceBuffer   （mask 不为 1 → 洞材质实例）
       同时把 InstanceArgsBuffer[0/5] 的 InstanceCount 累加为最终 indirect draw 个数。
   │
   ▼
(6) AddPass_TransitionAllDrawBuffers   切回 SRV/IndirectArgs Read
   │
   ▼
[场景渲染阶段]
   FMeshBatch (BatchElement.IndirectArgsOffset = 0)
       → 用 QuadInstanceBuffer + 主 Material 走 DrawIndexedInstancedIndirect
   FMeshBatch (BatchElement.IndirectArgsOffset = 5*sizeof(uint32))
       → 用 HoleQuadInstanceBuffer + HoleMaterial 走第二次 DrawIndexedInstancedIndirect
   每个 instance 在 VS 中由 VirtualHeightfieldMeshVertexFactory.ush 展开为 GRID_SIZE^2 顶点的 patch
```

如果 `r.VHM.WithOnePass=1` 且 SM6+，会把 (4) 整个 N 次循环替换成单个 **`CollectQuadsOnePassCS`** —— 它使用 **持久线程（persistent threads）**模型，所有线程共享 `RWQueueInfo{Read, Write, NumActive}`，靠 `InterlockedAdd` 不断从队列里 pop quad、subdivide、push children，直到 `NumActive==0`，相当于 GPU 端的 work-stealing BFS。它通过 `DeviceMemoryBarrier()` + `NumGroupTasks` 计数实现退出条件。

#### 4.2 关键 Shader 文件细节

##### `VirtualHeightfieldInitBuffers.usf`

- `FirstInitBuffersCS`：单线程，写入"`PackData0.x = MaxLevel<<24`"作为最初的 root quad，给 v2 路径用（已被禁用）。
- `InitAllBuffersCS`：每帧第一个 CS。`numthreads(1,1,1)` 单线程，把 `RWQueueInfo` 三个字段清零；按 `MaxArgsCount` 循环把 ping-pong 用的两个 `DispatchArgsBuffer` 都设为 `(0,1,1,0)`（其中 (1,1) 表示空 dispatch 的 Y/Z group 数，最后的 0 是 quad count 累加初值）；把 `InstanceArgsBuffer` 的两段（默认 quad、mask quad）分别写成 `(NumIndices,0,0,0,0)`，正好是 `DrawIndexedInstancedIndirect` 5 个参数（IndexCount, InstanceCount, FirstIndex, BaseVertex, FirstInstance）。

##### `VirtualHeightfieldMesh3.usf`

文件级宏：

- `COLL_THREAD_TOTAL = 32`（一个 group 32 线程，正好对齐 SM6 wavesize）。
- `VHM_WITH_FEEDBACK` / `VHM_WITH_CULL` / `VHM_ONE_PASS` / `VHM_END_WITH_ONE_STEP` / `VHM_STAT` 全是 SHADER\_PERMUTATION\_BOOL。
- `APPLY_LQT_OPTIM=1`：启用 Linear Quad-Tree 风格的 group 内预取/分配优化（用 group 内 quad 数代替 `InterlockedMax`）。

核心结构：

```
struct QuadItem2  { uint2 Pos; uint Level; uint3 PhysicalAddress; }
struct SQuadInfo  { Pos, Level, TexPos, TextureLevel, GeoToTexLevelOffset(Inv),
                    PhysicalAddress, SampleTextureLevel, SampleTexPos,
                    SampleGeoToTexLevelOffsetInv }
```

- `Pos` 用 28-bit Morton 编码 + 高 4 bit `Level` 打成 1 个 uint，因此 `uint4` 一次能放下 (Pos|Level, PhysicalAddress.xyz)。
- `GeoToTexLevelOffset` 解决"几何 Level（MaxLevel 起算）"与"VT mip Level（RVTMinLevel 起算）"不一致的问题（特别在 ExtSubdivisionLevel>0 时几何比 VT 还细）。
- `SampleTextureLevel` 是采高度时使用的 mip。

`CollectSubdivideQuadsCS`：

1. 用 `CurPassCalTime` 决定本 pass 读哪个 Args 块（`InArgsTime = pass/2`，`OutArgsTimes = (pass+1)/2`），实现 ping-pong 但又能让多个 pass 共享同一个 Args buffer（节省 buffer）。
2. 逐 quad 算 `MinDistanceLod`（从 quad 的 9 个边/中点世界坐标到 view 的最小距离），与 `Item.Level` 比较决定是否 subdivide。
3. `SubdivideQuadFlag` / `FinalQuadFlag` 是 group shared 的 prefix-sum 数组，最后一个线程做扫描后用 `InterlockedAdd` 申请全局 offset，然后所有线程并行写出。Buffer 写入用 `(offset & SizeMask)` 实现环形 buffer。
4. Feedback：把 `(SampleTexPos.x | SampleTexPos.y<<12 | (Level+1)<<24 | PageTableFeedbackId)` 打到 `RWFeedbackBuffer`，VT 系统据此异步加载页。

`CullQuadsAndGenerateInstancesCS`：

- 与 v1 的 `CullInstancesCS` 类似，但额外按 `IsOpacity(MaskValue)` 把 quad 分流到 `QuadInstanceBuffer`/`HoleQuadInstanceBuffer`。
- 把每个 instance 打成 `Instance.PosLevelPacked = Pos.x | Pos.y<<14 | Level<<28`（注意这里 14-bit X、14-bit Y，比 morton 包法宽，足够正常 RVT 大小）+ `PhysicalAddress[3]`。

##### `HeightfieldMaskRender.usf`

- 入口 `MaskCopyCS`，`numthreads(8,8,1)`，每线程读 2×2 源像素并写 1 像素到 `DstTexture[DstTextureCoord + DispatchThreadId]`。
- 多 permutation：`INPUT_FORMAT_MASK_RGBA8_A` / `_B` / `MAST_R8`，分别从 RGBA 的 a/b 通道或 R 通道读取 mask（用于不同 RVT MaterialType 的 mask 通道位置）。
- 合并规则刻意避免"全 0/全 1"被误归为中间值 0.5，从而保证 MaxLod mip 仍能精确反映 quad 是否完全可绘 / 完全镂空。

#### 4.3 Compute Shader / Mesh Shader / Indirect Draw 使用情况

- **Compute Shader**：插件几乎所有计算都靠 CS。包括 RVT 烘焙阶段（MinMax/Mask 下采样）、运行时的 quadtree 细分、剔除、生成实例。
- **Mesh Shader**：**未使用**。VHM 走的是「CS 生成 instance buffer → 传统 VS+PS」的间接绘制，不是 Mesh Shader 路径。`VirtualHeightfieldMeshVertexFactory.ush` 只声明 `FVertexFactoryInput { uint InstanceId : SV_InstanceID; uint VertexId : SV_VertexID; }`，VS 阶段从 instance buffer 拿 quad 信息再用 `VertexId` 在 `GRID_SIZE^2` 网格里展开。
- **Indirect Draw**：`DrawIndexedInstancedIndirect` 是绘制阶段唯一的 draw call 方式。`InstanceArgsBuffer` 是一个 RW Buffer，前 5 个 uint 是普通材质的 indirect args，后 5 个 uint 是 hole 材质的 indirect args，分别由 `BatchElement.IndirectArgsOffset = 0` 和 `5*sizeof(uint32)` 引用。
- **Indirect Dispatch**：v3 中 `CollectSubdivideQuadsCS` 用 `DispatchIndirect`，args 来自上一帧 pass 写入的 `OutDispatchArgsBuffer`，避免回读 CPU。

---

### 5. 编辑器构建流程（`HeightfieldMinMaxTextureBuild.cpp`）

入口 `VirtualHeightfieldMesh::BuildMinMaxHeightTexture(component)`，被组件的 `bBuildMinMaxTextureButton` 触发（典型流程）：

1. 校验：`HasMinMaxHeightTexture` 检查 `MinMaxTexture` 与 `VirtualTextureVolume` 都已经设置。
2. 拿 RVT 的 `FVTProducerDescription`，确定 `TileSize` / `MaxLevel`，并按用户配置的 `NumMinMaxTextureBuildLevels` 计算最终 MinMax 纹理的 `NumTilesX/Y` 与 `NumMips`。
3. 构造 `FMinMaxTileRenderResources`（一个 R16 的 `TileRenderTarget` + 一个 RGBA8 的 `FinalRenderTarget`，外加多 mip 的 `StagingTextures` 用于回读）。
4. 进入 `NumTilesY × NumTilesX` 二重循环：
   - 对每个 tile：触发 streaming（让源材质资源加载）。
   - `ENQUEUE_RENDER_COMMAND(MinMaxTextureTileCommand)`：
   - 用 `RuntimeVirtualTexture::RenderPagesStandAlone(GraphBuilder, Desc)` 直接把 RVT 重新渲染到 `TileRenderTarget`（`MaterialType = WorldHeight`，`bClearTextures=true`）。
   - 调 `DownsampleMinMaxAndCopy(GraphBuilder, SrcTexture, TileSize, DstTextureUAV, FIntPoint(TileX, TileY))`：内部 R16 → RG16 ladder → 把单个像素 (min,max) 写到 `FinalRenderTarget` 的 `(TileX,TileY)`。
5. 全部 tile 完成后，再用 `GenerateMinMaxTextureMips` 在 GPU 端构造其他 mip。
6. 把 `FinalRenderTarget` 各 mip `CopyTexture` 到 `StagingTextures` → `MapStagingSurface` 回读到 CPU `FinalPixels`。
7. `InComponent->InitializeMinMaxTexture(NumTilesX, NumTilesY, NumMips, FinalPixels)` 把数据塞回 `UHeightfieldMinMaxTexture` 资产 bulk data。

`BuildMaskTexture(component)`（定制函数，`HeightfieldMinMaxTextureBuild.cpp:429`）流程几乎一致，差别在于：

- 一次渲染 4 个 RVT target（4 张 `TileRenderTarget`），`MaterialType = MaterialTypeForMask`。
- 通过 `URuntimeVirtualTexture::GetMaskLayerIndex(MaterialType)` 决定哪一张是 mask 纹理。
- 调 `DownsampleMaskAndCopy(...)`，输出 PF\_R8 单通道 mask。
- 写回 `UTexture2D`（不是 MinMax 纹理），通过 `InComponent->InitializeMaskTexture`。

构建过程都支持 `r.VHM.CaptureBuildTexture` 来触发 RenderDoc 抓帧调试。

---

### 6. 渲染器集成与调控点

#### 6.1 启停逻辑（`VirtualHeightfieldMeshEnable.cpp`）

- 主开关 `r.VHM.Enable`（默认 0）+ 调试开关 `r.VHM.Visualize`（默认 1）共同决定 `VirtualHeightfieldMesh::IsEnabled(FeatureLevel)`。
- 项目定制：在非编辑器构建里若 Nanite 与 LandscapeNanite 都启用，会自动把 `r.VHM.Enable` 强制设为 0（避免 Nanite Landscape 与 VHM 同时画地形）。
- `r.VHM.EnableExtSubdivisionLevel` 切换时会遍历所有 `UVirtualHeightfieldMeshComponent` 调 `SetEnableExtSubdivisionLevel + MarkRenderStateDirty`，让 `ExtSubdivisionLevel` 0/N 之间动态切换。
- `IsEnabled` 变化时还会把所有相关 `URuntimeVirtualTextureComponent` 也 `MarkRenderStateDirty`，让所有读 RVT 的 primitive 重新提交。

#### 6.2 主要 CVars（出现在 `VirtualHeightfieldMeshSceneProxy.cpp` 顶部）

| CVar | 默认 | 作用 |
| --- | --- | --- |
| `r.VHM.UseAsyncCompute` | 1 | 所有 VHM CS pass 走 async compute |
| `r.VHM.LodScale` | 1.0 | 全局 LOD 距离缩放 |
| `r.VHM.EnableViewLodFactor` | 0 | 是否乘 `View.LODDistanceFactor`（防止 FOV 双计） |
| `r.VHM.Occlusion` | 1 | 是否做 occlusion query |
| `r.VHM.MaxRenderInstances` | 64K | quad 实例 buffer 大小 |
| `r.VHM.MaxFeedbackItems` | 40K | VT feedback buffer 上限 |
| `r.VHM.MaxPersistentQueueItems` | 64K | 持久线程队列大小（向上取 2 的幂） |
| `r.VHM.CollectPassWavefronts` | 16 | OnePass collect 的 group 数 |
| `r.VHM.Version` | 3 | 1=v1 多 pass、3=v3 多 pass / OnePass |
| `r.VHM.WithOnePass` | 0 | v3 中是否使用持久线程 OnePass |
| `r.VHM.NumActiveForOnePassStep` | 640 | OnePass 提早退出阈值 |
| `r.VHM.DisableCull` | 0 | 调试用：跳过 frustum/mask cull |
| `r.VHM.CloseMorphVertexForDebug` | 0 | 关闭 CDLOD morph，方便看 LOD 边界 |
| `r.VHM.CaptureBuildTexture` | 0 | 编辑器烘焙时 RenderDoc 抓帧 |

---

### 7. 核心设计要点小结

1. **几何完全 GPU 端构造**：CPU 不维护 mesh，由 quadtree 细分 CS 在每帧动态展开成 instance 列表；唯一的"静态资源"是一张 morton 序的 `IndexBuffer` 和两组 UB。
2. **数据通过 RVT page table 间接寻址**：所有高度采样在 VS 阶段都走 `TextureLoadVirtualPageTableLevel + VTComputePhysicalUVs`，配合 RVT 的 streaming 与 LRU，实现"无穷大世界仅占用固定显存"。
3. **LOD 由距离 + 高度可见区两路决定**：`MinDistanceLod` 决定要不要继续细分，`HeightMinMaxTexture` 在 cull 阶段构造 AABB 做 frustum cull；CDLOD morph 在 VS 内完成 LOD 边界平滑。
4. **多版本管线共存**：v1（`CollectQuadsCS` 持久线程模型，需要 SM5）、v3（多 pass + 可选 OnePass，需要 SM6）。代码通过 `r.VHM.Version` 切换；项目目前默认 v3。
5. **Hole 材质（项目定制）**：把 mask 通道引入剔除分流，让"高度场上某些区域走另一套材质"成为零额外 draw call 的事情（只是额外一段 indirect args）。这对做岩浆口、洞穴入口、湿地、砂坑等地表非常有用。
6. **统计与调试基础设施完整**：`StatBuffer` + `FRHIGPUBufferReadback` 多帧轮转；CVar 化的 LOD/cull 调参；RenderDoc 抓帧 hook。

---

### 8. 文件→职责速查表

| # | 文件 | 角色 | 关键符号 |
| --- | --- | --- | --- |
| 1 | `Shaders/Private/HeightfieldMaskRender.usf` | 定制 mask 下采样 CS | `MaskCopyCS` |
| 2 | `Shaders/Private/VirtualHeightfieldInitBuffers.usf` | 每帧初始化 GPU buffers | `FirstInitBuffersCS`、`InitAllBuffersCS` |
| 3 | `Shaders/Private/VirtualHeightfieldMesh3.usf` | v3 quadtree 细分 + cull + 实例生成 | `FillLevel4QuadCS`、`CollectSubdivideQuadsCS`、`CollectQuadsOnePassCS`、`CullQuadsAndGenerateInstancesCS` |
| 4 | `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` | VS 端 vertex factory + CDLOD morph | `GetVertexFactoryIntermediates`、`MorphVertex` |
| 5 | `Source/.../Private/HeightfieldMinMaxRender.cpp` | MinMax/Mask 多 mip 下采样 RDG 工具 | `DownsampleMinMaxAndCopy`、`GenerateMinMaxTextureMips`、`DownsampleMaskAndCopy`、`TMinMaxTextureCS<>` |
| 6 | `Source/.../Public/HeightfieldMinMaxRender.h` | 上述工具的公开 API | 命名空间 `VirtualHeightfieldMesh` |
| 7 | `Source/.../Private/VirtualHeightfieldMeshEnable.cpp` | CVar 与启停沉降 | `IsEnabled`、`VHMEnableCVarSinkFunction` |
| 8 | `Source/.../Private/VirtualHeightfieldMeshSceneProxy.cpp` | **核心调度**：scene proxy + RDG 全部 pass + V1/V3 SubmitWork | `FVirtualHeightfieldMeshSceneProxy`、`FVirtualHeightfieldMeshRendererExtension`、`SubmitWork_V3`、`InitializeInstanceBuffers`、`AddPass_*` |
| 9 | `Source/.../Private/VirtualHeightfieldMeshVertexFactory.cpp` | VF 注册 + indexbuffer 实现 | `IMPLEMENT_VERTEX_FACTORY_TYPE`、`FVirtualHeightfieldMeshVertexFactoryShaderParameters` |
| 10 | `Source/.../Private/VirtualHeightfieldMeshVertexFactory.h` | VF + 两组 UB 声明 | `FVirtualHeightfieldMeshVertexFactoryParameters`(VHM)、`...Parameters2`(VHMInst)、`FVirtualHeightfieldMeshUserData` |
| 11 | `Source/.../Public/VirtualHeightfieldMeshComponent.h` | UComponent 入口与所有 UProperty | `UVirtualHeightfieldMeshComponent` |
| 12 | `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` | 编辑器烘焙：MinMax 纹理 + Mask 纹理 | `BuildMinMaxHeightTexture`、`BuildMaskTexture`、`FMinMaxTileRenderResources`、`FMaskTileRenderResources` |

---

### 9. 后续 HTML 报告可直接引用的要点

- **一图主流程**：`InitAllBuffers → FillLevel4Quad → [CollectSubdivideQuads × N | CollectQuadsOnePass] → CullQuadsAndGenerateInstances → DrawIndexedInstancedIndirect ×2`。
- **数据沿用**：高度（RVT 物理纹理）+ MinMax（cull）+ Mask（cull/分流）+ LodBias（每像素 LOD 偏移）+ Occlusion（CPU 硬件查询结果转纹理回喂）。
- **关键技巧**：morton 序索引、CDLOD morph、ping-pong dispatch args、persistent threads、24+4 bit Pos|Level pack、indirect args 中两段 5×uint32 实现"双材质同 mesh batch 双 draw"。
- **项目定制点**（标 `S1_Engine_Shiyu` / `Engine CYH` / `shiyu`）：Mask + HoleMaterial 二次绘制、ExtSubdivisionLevel 加密、Stat 回读、Nanite 互斥逻辑、`Lod0LevelBias`。
- **Editor 烘焙**：通过 `RenderPagesStandAlone` 把 RVT 离线渲染→GPU 下采样→CPU 回读→存 bulk data，逻辑可重用做其他 RVT 派生纹理。

---

*报告基于直接 Read 12 个源文件 + 关键节段（SceneProxy 0~1900 行、1900~2700 行）阅读得出。代码定位用 `file:line` 形式给出，便于跳转。*

## VirtualHeightfieldMesh 插件 P4 提交历史分析

> depot 路径：`//GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/...`
> 生成自 P4 `describe -s` 输出，仅统计该目录内的文件改动。

### 总览

- 共扫描提交：**66** 个 changelist
- 时间范围：**2024/01/21**（CL 114）→ **2026/03/16**（CL 843633）
- 实际涉及 VHM 目录文件的提交：**66** 个（其余为空 / describe 失败）
- 主要贡献者 Top 5（按提交人统计，提交人取 `--user=` 字段，无则取 P4 用户名）：

1. **郭智均** — 31 次
2. **谢朋志** — 7 次
3. **陈永昊** — 6 次
4. **张建国\_20240109032154** — 5 次
5. **卫帅** — 3 次

> 说明：
> - 表中“涉及 VHM 文件数”仅统计 `Plugins/Experimental/VirtualHeightfieldMesh/` 路径下的文件。
> - 提交者以提交说明里 `--user=` 字段为准（中文实名），通过 `Tools_Program_Engine` 等机器账号代为入库的提交亦如此。
> - CL 114（初始分支导入）和 CL 239（type map）属于仓库基础提交，不是功能性改动。

### 提交一览（按时间正序）

| CL | 日期 | 提交者 | TAPD | VHM 文件数 | 一句话总结 |
| --- | --- | --- | --- | --- | --- |
| 114 | 2024/01/21 | perforce | - | 60 | UE5EA |
| 239 | 2024/01/24 | perforce | - | 53 | type map |
| 3360 | 2024/02/21 | guozhijun | - | 2 | 【ID1003890】【VHM】支持VHM的渲染面数统计指标 |
| 4244 | 2024/02/23 | guozhijun | - | 1 | 【ID1004265】加入VHM，但使用命令行开启 |
| 8641 | 2024/03/12 | zhangjianguo | - | 1 | 【RVT扩展】RVT加了一个Custom 通道Merging //GR/PCGDev to trunk (//GR/trunk) |
| 9076 | 2024/03/14 | zhangjianguo | - | 1 | 【RVT扩展】【再扩展一个RVT通道用于存动态白厄颜色】有种再来10个 |
| 31598 | 2024/05/19 | 郭智均 | story=1016921 | 32 | VHM合并到trunk，并适配白垩的高精度要求 |
| 33256 | 2024/05/21 | 郭智均 | story=1016921 | 1 | VHM合并到trunk，并适配白垩的高精度要求 |
| 33893 | 2024/05/22 | 谢朋志 | story=1016970 | 1 | 通用提交单 VHM in Editor |
| 35131 | 2024/05/24 | 谢朋志 | story=1016970 | 1 | 通用提交单 VHM |
| 35345 | 2024/05/24 | 刘双 | story=1017591 | 3 | 内存扩展：UE 中利用 IOS 新的内存特性 接单删除中间生成文件 |
| 36363 | 2024/05/27 | 谢朋志 | story=1016970 | 1 | 通用提交单 Demo（仅用于视频录制） |
| 36368 | 2024/05/27 | 谢朋志 | story=1016970 | 1 | 通用提交单 //GR/trunk/... changelist 36363 |
| 38190 | 2024/05/29 | 谢朋志 | story=1016970 | 1 | 通用提交单 |
| 40492 | 2024/06/03 | 谢朋志 | story=1016970 | 1 | 通用提交单 disable in Editor |
| 42957 | 2024/06/06 | 谢朋志 | story=1016970 | 1 | 通用提交单 : Undo //GR/trunk/... changelist 38190 |
| 49953 | 2024/06/18 | 郭智均 | story=1020482 | 1 | 提审代码合并到trunk |
| 49968 | 2024/06/18 | 郭智均 | story=1020482 | 2 | 提审代码合并到trunk |
| 52549 | 2024/06/21 | 郭智均 | story=1019849 | 5 | VHM提供更多更细粒度的调整网格精度的配置 |
| 54652 | 2024/06/24 | 郭智均 | bug=1021577 | 1 | 允许VHM在Game下渲染网格 |
| 59490 | 2024/06/27 | 郭智均 | bug=1023220 | 1 | 【构建报错】 UnityBuild构建失败 |
| 67907 | 2024/07/04 | 郭智均 | bug=1025460 | 1 | 【VHM】处理Build贴图失败问题 |
| 76582 | 2024/07/10 | 郭智均 | bug=1025938 | 3 | 【场景】航线阶段或者远处看向空岛场景，空岛地面异常 |
| 77264 | 2024/07/10 | 郭智均 | bug=1025415 | 3 | 【crash】【vhm】优化&Fix Stat Buffer引起的崩溃 |
| 77400 | 2024/07/10 | 郭智均 | bug=1025415 | 1 | 【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 |
| 77461 | 2024/07/10 | 郭智均 | bug=1025415 | 1 | 【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 |
| 81681 | 2024/07/13 | 郭智均 | story=1022169 | 1 | 【VHM】VHM屏蔽GetGlobalVirtualTextureMipBias |
| 82353 | 2024/07/15 | 郭智均 | bug=1022489 | 3 | 【日志告警】【netlog\_ensure告警】ieldMeshSceneProxycpp564ExpressionbInFrameMessageTitleG… |
| 88225 | 2024/07/18 | 郭智均 | bug=1029334 | 1 | 【野区场景】【副岛】地面碰撞与实际显示不符 |
| 93165 | 2024/07/22 | 郭智均 | bug=1029334 | 2 | 【野区场景】【副岛】地面碰撞与实际显示不符 |
| 110749 | 2024/08/08 | 郭智均 | story=1023328 | 5 | 【CBT】白垩毒圈剔除逻辑重构 |
| 120024 | 2024/08/16 | 郭智均 | story=1023328 | 4 | 【CBT】地形细分加入动态开关 |
| 155993 | 2024/10/10 | 郭智均 | story=1022943 | 7 | Merge from Release |
| 203456 | 2024/12/22 | tools | - | 13 | [BranchCopy] Merge from EngineUpgrade-202982 to trunk --Trigger=feiyulliu-Win\_… |
| 206628 | 2024/12/26 | 郭智均 | bug=1052327 | 1 | 【Crash】进入对局选角阶段结束进入场景偶现崩溃，FD3D12DynamicRHI::RHICreateShaderResourceView() [D:\… |
| 222041 | 2025/01/14 | 郭智均 | bug=1059699 | 1 | 【场景】全场景地形均不显示 |
| 223563 | 2025/01/15 | 郭智均 | bug=1059751 | 1 | 【构建】SVT流水线构建异常 |
| 291439 | 2025/03/29 | 张建国\_20240109032154 | story=1047503 | 11 | 【RVT 三岛合并】接入Trunk Merging //GR/PerfTest/... to //GR/trunk/... |
| 439014 | 2025/06/27 | 郭智均 | story=1057531 | 1 | 【代码合并】Release -> trunk --MergedFrom=//GR/release |
| 479212 | 2025/07/26 | 陈永昊 | story=1057052 | 1 | 地形Nanite —— 适配VHM |
| 479570 | 2025/07/28 | 陈永昊 | bug=1110806 | 1 | 【EA】【7月W3】【Crash】包体启动崩溃D: /projects\trunk\UE5EA\Enaine/Pluains\Experimenttal\V… |
| 481558 | 2025/07/29 | 陈永昊 | story=1057052 | 1 | 地形Nanite —— 适配VHM |
| 482700 | 2025/07/29 | 陈永昊 | story=1061285 | 1 | 地形Nanite —— 编辑器下关闭VHM |
| 497981 | 2025/08/08 | 杨彬 | story=1062793 | 1 | 【Bug转需求】【编辑器】打开nordland通过大纲强制加载这几个filter，明显卡顿 增加性能打桩标签 |
| 522266 | 2025/08/26 | 巩汝何 | story=1063541 | 1 | 【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug |
| 522727 | 2025/08/26 | 巩汝何 | story=1063541 | 1 | 【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug |
| 530838 | 2025/09/02 | 郭智均 | story=1065526 | 3 | 【GPU】VHM提供单Pass遍历树的功能 |
| 531156 | 2025/09/02 | 郭智均 | story=1065526 | 1 | 【GPU】VHM提供单Pass遍历树的功能 |
| 532031 | 2025/09/02 | 郭智均 | story=1065526 | 1 | 【GPU】VHM提供单Pass遍历树的功能 |
| 539523 | 2025/09/08 | 郭智均 | story=1066380 | 1 | 【GPU】VHM OnePass仅在Sm6下启用 |
| 539702 | 2025/09/08 | 郭智均 | story=1066380 | 2 | 【GPU】VHM OnePass仅在Sm6下启用 |
| 539878 | 2025/09/08 | 郭智均 | story=1066380 | 1 | 【GPU】VHM OnePass仅在Sm6下启用 |
| 575365 | 2025/09/26 | 陈永昊 | story=1067894 | 1 | 【前台性能】【EA】超高配置笔记本上worldpartition的一个调用有0.4ms~1ms的耗时 |
| 583471 | 2025/10/11 | 郭智均 | story=1052866 | 2 | VHM性能优化 - 【性能任务】 - 中台对VHM模块的优化 |
| 589356 | 2025/10/14 | 陈永昊 | story=1065085 | 1 | Nanite地形合并功能 —— 合并功能接入构建流水线 |
| 618802 | 2025/10/30 | 任晓宇 | story=1070804 | 1 | 【Mobile】材质FeatureLevel造成的渲染错误 |
| 642839 | 2025/11/12 | 张建国\_20240109032154 | bug=1149257 | 1 | 【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 适配LandscapeNanite和VHM |
| 642914 | 2025/11/12 | 张建国\_20240109032154 | bug=1149257 | 1 | 【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 回退一下，初始化太早了，有崩溃 |
| 678481 | 2025/12/01 | 贾李朋 | bug=1151320 | 2 | 【EA】【客户端性能】狼人技能引发卡顿MI\_Hero05\_Wolf\_Iris (16.6 ms) |
| 679794 | 2025/12/02 | 郭智均 | bug=1156145 | 2 | 【EA】【策划反馈】2\*2 野外输入指令（r.VHM.Visualize 0）后才能看到地表 |
| 695187 | 2025/12/10 | 张建国\_20240109032154 | story=1074379 | 5 | 【EA】RVT高精度模式兼容VHM Mask |
| 716640 | 2025/12/23 | 张建国\_20240109032154 | story=1074379 | 1 | 【EA】RVT高精度模式兼容VHM Mask |
| 816358 | 2026/03/02 | 卫帅 | story=1081019 | 1 | 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 将EngineUpgrade分支的GPU Profile5.6内容合并到Trunk |
| 817601 | 2026/03/02 | 卫帅 | story=1081019 | 1 | 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 由于出包机器有报错暂时Revert |
| 841231 | 2026/03/13 | 贾李朋 | story=1082655 | 2 | 【EA】【客户端性能】去除无效ShaderType |
| 843633 | 2026/03/16 | 卫帅 | story=1081019 | 1 | 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 #修复编译报错，增加PS5平台支持 |

### 各 changelist 详情

#### CL 114 — 2024/01/21 — perforce

- **提交说明**：UE5EA
- **涉及 VHM 文件**：60 个

**做了什么**：

UE5EA 引擎首次完整入库（`p4_trunk` 工作区一次性 add），VHM 插件作为 Epic 自带 Experimental 插件随引擎同步导入仓库。共导入 60 个 VHM 路径下的源码 / 资产 / Shader 文件，是后续所有改动的起点。

📄 查看 VHM 相关 diff（CL 114）

```
(no diff for VHM files)
```

#### CL 239 — 2024/01/24 — perforce

- **提交说明**：type map
- **涉及 VHM 文件**：53 个

**做了什么**：

`type map` 维护：批量重新指定文件类型属性，导致 VHM 目录下 53 个文件被以新类型再次入库（`uasset`/`umap`/`dll`/`cpp`/`h`/`usf`/`ush` 等）。无功能改动。

📄 查看 VHM 相关 diff（CL 239）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Binaries/Win64/UnrealEditor-VirtualHeightfieldMesh.dll#2 (binary+w/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Binaries/Win64/UnrealEditor-VirtualHeightfieldMeshEditor.dll#2 (binary+w/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/MaterialFuntions/BoundsClip.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/Mat_Landscape.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/MinMax_Landscape_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/RVT_Landscape_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/RVT_Landscape_Shading.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/SVT_Landscape_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/SVT_Landscape_Shading.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Landscape/VHM_Landscape_P.umap#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/Mat_Procedural.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/Mat_Procedural_Gen.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/MinMax_Procedural_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/RVT_Procedural_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/SVT_Procedural_Height.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/VHM_Procedural.umap#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Content/Samples/Procedural/VHM_Procedural_BuiltData.uasset#2 (binary+l/binary) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxRender.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTexture.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureMaterialExpression.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureMaterialExpression.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshActor.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshModule.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/HeightfieldMinMaxRender.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/HeightfieldMinMaxTexture.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshActor.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshModule.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/VirtualHeightfieldMesh.Build.cs#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureAssetTypeActions.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureAssetTypeActions.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureFactory.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureFactory.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureThumbnailRenderer.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureThumbnailRenderer.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/VirtualHeightfieldMeshDetailsCustomization.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/VirtualHeightfieldMeshDetailsCustomization.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/VirtualHeightfieldMeshEditorModule.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.cpp#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Public/VirtualHeightfieldMeshEditorModule.h#2 (unicode/text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/VirtualHeightfieldMeshEditor.Build.cs#2 (unicode/text) ====
```

#### CL 3360 — 2024/02/21 — guozhijun

- **提交说明**：【ID1003890】【VHM】支持VHM的渲染面数统计指标
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【ID1003890】【VHM】支持VHM的渲染面数统计指标

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh.usf` (edit)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 3360）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh.usf#2 (text) ====

21a22,30
> #define VHM_STAT 1

> #pragma region S1_Engine_Shiyu

> #if VHM_STAT

> static const uint sMaxLodLevel = 15;

> static const uint sAfterCullOffset = 16;

> RWBuffer<uint> RWStatBuffer;

> #endif

> #pragma endregion

>

191a201,210
> #pragma region S1_Engine_Shiyu

> #if VHM_STAT

>   // Init stat data

>   for (int Idx = 0; Idx < sAfterCullOffset * 2; ++Idx)

>   {

>       RWStatBuffer[Idx] = 0;

>   }

> #endif

> #pragma endregion

>

313a333,342
> #pragma region S1_Engine_Shiyu

> #if VHM_STAT

>               if (Level < sMaxLodLevel)

>               {

>                   InterlockedAdd(RWStatBuffer[Level], 1);

>               }

>               InterlockedAdd(RWStatBuffer[sMaxLodLevel], 1);

> #endif

> #pragma endregion

>

411a441,450
>

> #pragma region S1_Engine_Shiyu

> #if VHM_STAT

>       if (Level < sMaxLodLevel)

>       {

>           InterlockedAdd(RWStatBuffer[sAfterCullOffset + Level], 1);

>       }

>       InterlockedAdd(RWStatBuffer[sAfterCullOffset + sMaxLodLevel], 1);

> #endif

> #pragma endregion


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#3 (unicode) ====

17a18
> #include "RHIGPUReadback.h"

24a26,27
> PRAGMA_DISABLE_OPTIMIZATION

>

85a89,117
> #pragma region S1_Engine_Shiyu

> #if UE_BUILD_SHIPPING

> #define VHM_ENABLE_STAT 0

> #else

> #define VHM_ENABLE_STAT 1

> #endif

>

> #if VHM_ENABLE_STAT

> #include "Stats/Stats2.h"

> #include "Stats/StatsMisc.h"

>

> DECLARE_STATS_GROUP(TEXT("VHM"), STATGROUP_VHM, STATCAT_Advanced);

>

> DECLARE_DWORD_COUNTER_STAT(TEXT("BeforeCullInstances"), STAT_VHM_BeforeCullInstances, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-ALL"), STAT_VHM_DrawInstancesALL, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD0"), STAT_VHM_DrawInstancesLOD0, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD1"), STAT_VHM_DrawInstancesLOD1, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD2"), STAT_VHM_DrawInstancesLOD2, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD3"), STAT_VHM_DrawInstancesLOD3, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD4"), STAT_VHM_DrawInstancesLOD4, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD5"), STAT_VHM_DrawInstancesLOD5, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD6"), STAT_VHM_DrawInstancesLOD6, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD7"), STAT_VHM_DrawInstancesLOD7, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD8"), STAT_VHM_DrawInstancesLOD8, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD9"), STAT_VHM_DrawInstancesLOD9, STATGROUP_VHM)

> #endif

>

> #pragma endregion

>

98a131,138
>

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       static constexpr uint32 MaxReadBackSize = 4;

>       /** For Stat  */

>       TArray<TUniquePtr<FRHIGPUBufferReadback>> StatBufferReadBacks;

> #endif

> #pragma endregion

111a152,156
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       InBuffers.StatBufferReadBacks.Empty();

> #endif

> #pragma endregion

189a235,240
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>   void CollectStat();

> #endif

> #pragma endregion

>

325a377,382
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>   CollectStat();

> #endif

> #pragma endregion

>

693a751,755
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>   static constexpr int32 StatBufferByteSize = sizeof(uint32) * 32;

> #endif

> #pragma endregion

725a788,793
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

> #endif

> #pragma endregion

>

765a834,838
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

> #endif

> #pragma endregion

771a845,853
>

>       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

>       {

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>           Environment.SetDefine(TEXT("VHM_STAT"), 1);

> #endif

> #pragma endregion

>       }

825a908,912
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

> #endif

> #pragma endregion

826a914,923
>

>

>       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

>       {

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>           Environment.SetDefine(TEXT("VHM_STAT"), 1);

> #endif

> #pragma endregion

>       }

980a1078,1084
>

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       FRDGBufferRef StatBuffer;

>       FRDGBufferUAVRef StatBufferUAV;

> #endif

> #pragma endregion

996c1100
<           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(5 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect, ERHIAccess::IndirectArgs, CreateInfo);

---
>           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(5 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect|BUF_SourceCopy, ERHIAccess::IndirectArgs|ERHIAccess::CopySrc, CreateInfo);

998a1103,1111
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       InBuffers.StatBufferReadBacks.Reserve(FDrawInstanceBuffers::MaxReadBackSize);

>       for (int32 i = 0; i < FDrawInstanceBuffers::MaxReadBackSize; ++i)

>       {

>           InBuffers.StatBufferReadBacks.Emplace(MakeUnique<FRHIGPUBufferReadback>(TEXT("VHM.StatReadBacks")));

>       }

> #endif

> #pragma endregion

1020a1134,1153
>

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       OutResources.StatBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateStructuredDesc(sizeof(uint32), 32),

>                                                           TEXT("VirtualHeightfieldMesh.StatBuffer"));

>       OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));

> #endif

> #pragma endregion

>   }

>

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>   void AddPass_GatherAllStats(FRDGBuilder& GraphBuilder,

>                               VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers, const uint32 BufferDiscardId,

>                               FVolatileResources& InVolatileResources)

>   {

>       const uint32 Offset = BufferDiscardId % FDrawInstanceBuffers::MaxReadBackSize;

>       FRHIGPUBufferReadback* GPUBufferReadBack = Buffers.StatBufferReadBacks[Offset].Get();

>       check(GPUBufferReadBack);

>       AddEnqueueCopyPass(GraphBuilder, GPUBufferReadBack, InVolatileResources.StatBuffer, sizeof(int32) * 32);

1021a1155,1156
> #endif

> #pragma endregion

1039c1174
<           TransitionInfos.Add(FRHITransitionInfo(IndirectArgsBufferUAV, bToWrite ? ERHIAccess::IndirectArgs : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::IndirectArgs));

---
>           TransitionInfos.Add(FRHITransitionInfo(IndirectArgsBufferUAV, bToWrite ? ERHIAccess::IndirectArgs|ERHIAccess::CopySrc : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::IndirectArgs|ERHIAccess::CopySrc));

1072a1208,1212
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;

> #endif

> #pragma endregion

1117a1258,1262
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;

> #endif

> #pragma endregion

1159a1305,1309
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;

> #endif

> #pragma endregion

1353a1504,1513
> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

>               // Gather Stat

>               {

>                   const uint32 BufferIndex = WorkDescs[WorkIndex].BufferIndex;

>                   VirtualHeightfieldMesh::AddPass_GatherAllStats(GraphBuilder, Buffers[BufferIndex], DiscardIds[BufferIndex], VolatileResources);

>               }

> #endif

> #pragma endregion

>

1360a1521
>

1361a1523,1577
>

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

> void FVirtualHeightfieldMeshRendererExtension::CollectStat()

> {

>   int64 Instances = 0;

>   int64 DrawInstances = 0;

>   uint64 DrawInstancesLOD[16] = {0};

>   // int64 CullInstances = 0;

>   static constexpr uint32 sAfterCullOffset = 16;

>   static constexpr uint32 sSumInstancesAddress = 15;

>

>   for (const FWorkDesc& WorkDesc : WorkDescs)

>   {

>       const uint32 BufDiscardId = DiscardIds[WorkDesc.BufferIndex];

>       constexpr uint32 MaxReadBackSize = VirtualHeightfieldMesh::FDrawInstanceBuffers::MaxReadBackSize;

>       if (BufDiscardId >= MaxReadBackSize)

>       {

>           FRHIGPUBufferReadback* ReadBackBuf = Buffers[WorkDesc.BufferIndex].StatBufferReadBacks[BufDiscardId % MaxReadBackSize].Get();

>           if (ReadBackBuf->IsReady())

>           {

>               const uint32* StatBufferData = static_cast<uint32*>(ReadBackBuf->Lock(VirtualHeightfieldMesh::StatBufferByteSize));

>               Instances += StatBufferData[sSumInstancesAddress];

>               DrawInstances += StatBufferData[sAfterCullOffset + sSumInstancesAddress];

>

>               for (int32 Idx = 0; Idx < 16; ++Idx)

>               {

>                   DrawInstancesLOD[Idx] += StatBufferData[sAfterCullOffset + Idx];

>               }

>

>               ReadBackBuf->Unlock();

>           }

>       }

>   }

>   // CullInstances = Instances - DrawInstances;

>

>   SET_DWORD_STAT(STAT_VHM_BeforeCullInstances, Instances);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesALL, DrawInstances);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD0, DrawInstancesLOD[0]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD1, DrawInstancesLOD[1]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD2, DrawInstancesLOD[2]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD3, DrawInstancesLOD[3]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD4, DrawInstancesLOD[4]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD5, DrawInstancesLOD[5]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD6, DrawInstancesLOD[6]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD7, DrawInstancesLOD[7]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD8, DrawInstancesLOD[8]);

>   SET_DWORD_STAT(STAT_VHM_DrawInstancesLOD9, DrawInstancesLOD[9]);

> }

> #endif

> #pragma endregion

>

>

> PRAGMA_ENABLE_OPTIMIZATION

>
```

#### CL 4244 — 2024/02/23 — guozhijun

- **提交说明**：【ID1004265】加入VHM，但使用命令行开启
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【ID1004265】加入VHM，但使用命令行开启

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 4244）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#3 (unicode) ====

16c16
<       1,

---
>       0,  // shiyu: now we need to open it by console
```

#### CL 8641 — 2024/03/12 — zhangjianguo

- **提交说明**：【RVT扩展】RVT加了一个Custom 通道Merging //GR/PCGDev to trunk (//GR/trunk)
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【RVT扩展】RVT加了一个Custom 通道Merging //GR/PCGDev to trunk (//GR/trunk)

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 8641）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#3 (unicode) ====

216a217,219
> #pragma region S1 Engine ZXB

>                   Desc.PageDescs[0].DestBox[3] = TileBox;

> #pragma endregion
```

#### CL 9076 — 2024/03/14 — zhangjianguo

- **提交说明**：【RVT扩展】【再扩展一个RVT通道用于存动态白厄颜色】有种再来10个
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【RVT扩展】【再扩展一个RVT通道用于存动态白厄颜色】有种再来10个

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (edit)

📄 查看 VHM 相关 diff（CL 9076）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#4 (unicode) ====

217c217,219
< #pragma region S1 Engine ZXB

---
> #pragma region Engine ZXB

>                   Desc.Targets[3].Texture = nullptr;

>                   Desc.Targets[4].Texture = nullptr;

218a221
>                   Desc.PageDescs[0].DestBox[4] = TileBox;
```

#### CL 31598 — 2024/05/19 — 郭智均

- **提交说明**：--story=1016921 --user=郭智均 VHM合并到trunk，并适配白垩的高精度要求 https://www.tapd.cn/68880148/s/1247672
- **TAPD**：story=1016921
- **涉及 VHM 文件**：32 个

**做了什么**：

提交目的：VHM合并到trunk，并适配白垩的高精度要求 https://www.tapd.cn/68880148/s/1247672

- **Shader**：8 个文件
- `Shaders/Private/HeightfieldMaskRender.usf` (add)
- `Shaders/Private/HeightfieldMinMaxRender.usf` (edit)
- `Shaders/Private/VHM_CollectQuad.usf` (add)
- `Shaders/Private/VirtualHeightfieldInitBuffers.usf` (add)
- `Shaders/Private/VirtualHeightfieldMesh.usf` (edit)
- `Shaders/Private/VirtualHeightfieldMesh.ush` (edit)
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (add)
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (edit)
- **Runtime C++**：24 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp` (add)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxRender.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.h` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (edit)
- …（其余 16 个略）

📄 查看 VHM 相关 diff（CL 31598）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/HeightfieldMinMaxRender.usf#2 (text) ====

83a84
>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh.usf#3 (text) ====

21a22,27
> #pragma region S1_Engine_Shiyu

> Texture2D<float> MaskTexture;

> RWStructuredBuffer<QuadRenderInstance> RWHoleInstanceBuffer;

> #pragma endregion

>

>

42a49
>

43a51
> uint RVTMinLevel; // RVTMinLevel is equal to RVT Level 0, because geometry levels is more than RVT levels.

60,96d67
< /** Unpack the virtual level for a PhysicalAddress entry in the virtual texture page table. */

< uint GetVirtualLevelFromPhysicalAddress(uint InPhysicalAddress)

< {

<   // See packing in PageTableUpdate.usf

<   return InPhysicalAddress & 0xf;

< }

<

< /** Compute physical UV from virtual UV in the tile with the given PhysicalAddress. */

< float2 VirtualToPhysicalUV(float2 InVirtualUV, uint InPhysicalAddress, float4 InTransformFactors, uint InNumAddressBits)

< {

<   // See packing in PageTableUpdate.usf

<   float PageX = (float)((InPhysicalAddress >> 4) & ((1u << InNumAddressBits) - 1));

<   float PageY = (float)(InPhysicalAddress >> (4 + InNumAddressBits));

<   float UVScale = 1.f / (float)(1u << GetVirtualLevelFromPhysicalAddress(InPhysicalAddress));

<

<   float2 BaseUV = float2(PageX, PageY) * InTransformFactors.x;

<   float2 PageUV = InVirtualUV * InTransformFactors.y;

<   float2 BorderUV = InTransformFactors.z;

<   float2 HalfTexelUV = InTransformFactors.w;

<

<   return BaseUV + PageUV + BorderUV - HalfTexelUV;

< }

<

< /** Returns transform from virtual to physical UV in the tile with the given PhysicalAddress. Returns float3 where .xy is bias and .z is scale. */

< float3 GetVirtualToPhysicalUVTransform(uint2 InPos, uint InLevel, uint InPhysicalAddress, float4 InTransformFactors, uint InNumAddressBits)

< {

<   uint LodShift = (uint)max((int)GetVirtualLevelFromPhysicalAddress(InPhysicalAddress) - (int)InLevel, 0);

<   float PosDivider = 1.f / (float)(1u << LodShift);

<   float2 MinVirtualUV = frac((float2)InPos * PosDivider);

<   float2 MaxVirtualUV = MinVirtualUV + PosDivider;

<

<   float2 MinPhysicalUV = VirtualToPhysicalUV(MinVirtualUV, InPhysicalAddress, InTransformFactors, InNumAddressBits);

<   float2 MaxPhysicalUV = VirtualToPhysicalUV(MaxVirtualUV, InPhysicalAddress, InTransformFactors, InNumAddressBits);

<

<   return float3(MinPhysicalUV, MaxPhysicalUV.x - MinPhysicalUV.x); // Assume Max.y - Min.y == Max.x - Min.x

< }

<

108,115d78
< /** Unpack the values from the MinMaxHeight texture from the packed 8888 format. */

< float2 UnPackMinMaxHeight(float4 InPacked)

< {

<   uint4 PackedScaled = (uint4)floor(InPacked *= 255.f);

<   uint2 UnPackedScaled = uint2(PackedScaled.x << 8 | PackedScaled.y, PackedScaled.z << 8 | PackedScaled.w);

<   float2 UnPacked = (float2)UnPackedScaled / 65535.f;

<   return UnPacked;

< }

117,121d79
< /** Unpack the values from the MinMaxLodBias texture from the packed 8888 format. */

< float2 UnPackMinMaxLodBias(float4 InPacked, float InLodBiasScale)

< {

<   return float2(CalculateBiasLod(InPacked.x, InLodBiasScale), CalculateBiasLod(InPacked.y, InLodBiasScale));

< }

123,134d80
< /** Return false if the AABB is completely outside one of the planes. */

< bool PlaneTestAABB(float4 InPlanes[5], float3 InCenter, float3 InExtent)

< {

<   bool bPlaneTest = true;

<

<   [unroll]

<   for (uint PlaneIndex = 0; PlaneIndex < 5; ++PlaneIndex)

<   {

<       float3 PlaneSigns;

<       PlaneSigns.x = InPlanes[PlaneIndex].x >= 0.f ? 1.f : -1.f;

<       PlaneSigns.y = InPlanes[PlaneIndex].y >= 0.f ? 1.f : -1.f;

<       PlaneSigns.z = InPlanes[PlaneIndex].z >= 0.f ? 1.f : -1.f;

136,180d81
<       bool bInsidePlane = dot(InPlanes[PlaneIndex], float4(InCenter + InExtent * PlaneSigns, 1.0f)) > 0.f;

<       bPlaneTest = bPlaneTest && bInsidePlane;

<   }

<

<   return bPlaneTest;

< }

<

< /* Return squared distance of closest distance between a point and a bounding box. */

< float SquaredMinDistanceToAABB(float3 InPos, float3 InMin, float3 InMax, float3 InScale)

< {

<   float3 D1 = max(InMin - InPos, 0) * InScale;

<   float3 D2 = max(InPos - InMax, 0) * InScale;

<   return dot(D1, D1) + dot(D2, D2);

< }

<

< /* Return squared distance of furthest distance between a point and a bounding box. */

< float SquaredMaxDistanceToAABB(float3 InPos, float3 InMin, float3 InMax, float3 InScale)

< {

<   float3 D = max(abs(InPos - InMin), (InPos - InMax)) * InScale;

<   return dot(D, D);

< }

<

< /** Draw a bounding box using the ShaderDrawDebug system. */

< void DebugDrawUVBox(float3 InUVMin, float3 InUVMax, float4x4 InTransform, float4 InColor)

< {

< #if 0 // Enable only if ShaderDrawDebug is enabled

<   float3 WorldPos[8];

<   WorldPos[0] = mul(float4(InUVMin.x, InUVMin.y, InUVMin.z, 1), InTransform);

<   WorldPos[1] = mul(float4(InUVMax.x, InUVMin.y, InUVMin.z, 1), InTransform);

<   WorldPos[2] = mul(float4(InUVMin.x, InUVMax.y, InUVMin.z, 1), InTransform);

<   WorldPos[3] = mul(float4(InUVMax.x, InUVMax.y, InUVMin.z, 1), InTransform);

<   WorldPos[4] = mul(float4(InUVMin.x, InUVMin.y, InUVMax.z, 1), InTransform);

<   WorldPos[5] = mul(float4(InUVMax.x, InUVMin.y, InUVMax.z, 1), InTransform);

<   WorldPos[6] = mul(float4(InUVMin.x, InUVMax.y, InUVMax.z, 1), InTransform);

<   WorldPos[7] = mul(float4(InUVMax.x, InUVMax.y, InUVMax.z, 1), InTransform);

<

<   AddQuadWS(WorldPos[0], WorldPos[2], WorldPos[3], WorldPos[1], InColor);

<   AddQuadWS(WorldPos[4], WorldPos[6], WorldPos[7], WorldPos[5], InColor);

<   AddLineWS(WorldPos[0], WorldPos[4], InColor, InColor);

<   AddLineWS(WorldPos[1], WorldPos[5], InColor, InColor);

<   AddLineWS(WorldPos[2], WorldPos[6], InColor, InColor);

<   AddLineWS(WorldPos[3], WorldPos[7], InColor, InColor);

< #endif

< }

<

204c105,106
<   for (int Idx = 0; Idx < sAfterCullOffset * 2; ++Idx)

---
>   int StatCount = sAfterCullOffset * 2 + 1;

>   for (int Idx = 0; Idx < StatCount; ++Idx)

245a148
>

249a153,160
>

>       // int OriNumActive = RWQueueInfo[0].NumActive;

>       // int NumActive = 0;

>       // int DstNumActive = OriNumActive - 1;

>       // if (OriNumActive > 0)

>       // {

>       //  InterlockedCompareExchange(RWQueueInfo[0].NumActive, OriNumActive, DstNumActive, NumActive);

>       // }

250a162
>

274a187,192
>           const uint GeoToTexLevelOffset = max(int(RVTMinLevel) - int(Level), 0); // geometry levels is large than tex levels

>           const float GeoToTexLevelOffsetInv = 1.f / float(1u << GeoToTexLevelOffset);

>           const uint TextureLevel = max(int(Level) - int(RVTMinLevel), 0);

>

>           uint2 TexPos = Pos >> GeoToTexLevelOffset;

>

276c194
<           float2 Scale = (float)(1u << Level) * PageTableSize.zw;

---
>           float2 Scale = (float)(1u << TextureLevel) * PageTableSize.zw;

278,279c196,197
<           float2 UV0 = ((float2)Pos + float2(0, 0)) * Scale;

<           float2 UV1 = ((float2)Pos + float2(1, 1)) * Scale;

---
>           float2 UV0 = ((float2)Pos + float2(0, 0)) * GeoToTexLevelOffsetInv * Scale;

>           float2 UV1 = ((float2)Pos + float2(1, 1)) * GeoToTexLevelOffsetInv * Scale;

281c199
<           float MinMaxTextureLevel = max((float)Level + (float)MinMaxLevelOffset, 0);

---
>           float MinMaxTextureLevel = max((float)TextureLevel + (float)MinMaxLevelOffset, 0);

286a205,226
>           float3 UV[8] = {

>               float3(UV0.x, UV0.y, MinMaxHeight.x),

>               float3(UV1.x, UV0.y, MinMaxHeight.x),

>               float3(UV1.x, UV1.y, MinMaxHeight.x),

>               float3(UV0.x, UV1.y, MinMaxHeight.x),

>

>               float3(UV0.x, UV0.y, MinMaxHeight.x),

>               float3(UV1.x, UV0.y, MinMaxHeight.x),

>               float3(UV1.x, UV1.y, MinMaxHeight.x),

>               float3(UV0.x, UV1.y, MinMaxHeight.x),

>           };

>

>           float Distances[8] = {

>               SquaredDistance((UV[0].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[1].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[2].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[3].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[4].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[5].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[6].xy - ViewOrigin.xy) * UVToWorldScale),

>               SquaredDistance((UV[7].xy - ViewOrigin.xy) * UVToWorldScale),

>           };

291,292c231,245
<           float MinDistanceSq = SquaredMinDistanceToAABB(ViewOrigin, UVMin, UVMax, UVToWorldScale);

<           float MaxDistanceSq = SquaredMaxDistanceToAABB(ViewOrigin, UVMin, UVMax, UVToWorldScale);

---
>           // float MinDistanceSq = SquaredMinDistanceToAABB(ViewOrigin, UVMin, UVMax, UVToWorldScale);

>           // float MaxDistanceSq = SquaredMaxDistanceToAABB(ViewOrigin, UVMin, UVMax, UVToWorldScale);

>

>           float MinDistanceSq = Distances[0];

>           [unroll]

>           for (int i=1; i < 8; ++i) {

>               MinDistanceSq = min(MinDistanceSq, Distances[i]);

>           }

>

>           float MaxDistanceSq = Distances[0];

>           [unroll]

>           for (int i=1; i < 8; ++i) {

>               MaxDistanceSq = max(MaxDistanceSq, Distances[i]);

>           }

>

327c280
<               uint PhysicalAddress = PageTableTexture.Load(int3(Pos, Level));

---
>               uint PhysicalAddress = PageTableTexture.Load(int3(TexPos, TextureLevel));

345,348c298,303
<               // Add all possible pages that vertex shader might read to the virtual texture feedback buffer.

<               int MinFeedbackLevel = (int)floor(clamp(MinDistanceLod - MinMaxLodBias.y, Level, MaxLevel));

<               int MaxFeedbackLevel = (int)ceil(clamp(MaxDistanceLod - MinMaxLodBias.x, Level, MaxLevel));

<               uint NumFeedbackItems = (uint)max(MaxFeedbackLevel - MinFeedbackLevel + 1, 0);

---
>               if (Level >= RVTMinLevel)

>               {

>                   // Add all possible pages that vertex shader might read to the virtual texture feedback buffer.

>                   int MinFeedbackLevel = (int)floor(clamp(MinDistanceLod - MinMaxLodBias.y, TextureLevel, MaxLevel - RVTMinLevel));

>                   int MaxFeedbackLevel = (int)ceil(clamp(MaxDistanceLod - MinMaxLodBias.x, TextureLevel, MaxLevel - RVTMinLevel));

>                   uint NumFeedbackItems = (uint)max(MaxFeedbackLevel - MinFeedbackLevel + 1, 0);

350,351c305,306
<               uint FeedbackPos;

<               InterlockedAdd(RWFeedbackBuffer[0], NumFeedbackItems, FeedbackPos);

---
>                   uint FeedbackPos;

>                   InterlockedAdd(RWFeedbackBuffer[0], NumFeedbackItems, FeedbackPos);

353,358c308,314
<               for (int FeedbackLevel = MinFeedbackLevel; FeedbackLevel <= MaxFeedbackLevel; ++FeedbackLevel)

<               {

<                   // Note that our general virtual texture feedback buffer convention is to write Level+1

<                   uint LevelPlusOne = FeedbackLevel + 1;

<                   uint LodShift = FeedbackLevel - Level;

<                   RWFeedbackBuffer[FeedbackPos + FeedbackLevel] = (Pos.x >> LodShift) | ((Pos.y >> LodShift) << 12) | (LevelPlusOne << 24) | PageTableFeedbackId;

---
>                   for (int FeedbackLevel = MinFeedbackLevel; FeedbackLevel <= MaxFeedbackLevel; ++FeedbackLevel)

>                   {

>                       // Note that our general virtual texture feedback buffer convention is to write Level+1

>                       uint LevelPlusOne = FeedbackLevel + 1;

>                       uint LodShift = FeedbackLevel - TextureLevel;

>                       RWFeedbackBuffer[FeedbackPos + FeedbackLevel] = (TexPos.x >> LodShift) | ((TexPos.y >> LodShift) << 12) | (LevelPlusOne << 24) | PageTableFeedbackId;

>                   }

389a346,351
>

>   RWIndirectArgsBuffer[5 + 0] = NumIndices;

>   RWIndirectArgsBuffer[5 + 1] = 0;

>   RWIndirectArgsBuffer[5 + 2] = 0;

>   RWIndirectArgsBuffer[5 + 3] = 0;

>   RWIndirectArgsBuffer[5 + 4] = 0;

407a370,373
>   const uint GeoToTexLevelOffset = max(int(RVTMinLevel) - int(Level), 0); // geometry levels is large than tex levels

>   const float GeoToTexLevelOffsetInv = 1.f / float(1u << GeoToTexLevelOffset);

>   const uint TextureLevel = max(int(Level) - int(RVTMinLevel), 0);

>

413,415c379,381
<   float2 Scale = (float)(1u << Level) * PageTableSize.zw;

<   float2 UV0 = ((float2)Pos + float2(0, 0)) * Scale;

<   float2 UV1 = ((float2)Pos + float2(1, 1)) * Scale;

---
>   float2 Scale = (float)(1u << TextureLevel) * PageTableSize.zw;

>   float2 UV0 = ((float2)Pos + float2(0, 0)) * GeoToTexLevelOffsetInv * Scale;

>   float2 UV1 = ((float2)Pos + float2(1, 1)) * GeoToTexLevelOffsetInv * Scale;

417c383
<   float MinMaxTextureLevel = max((float)Level + (float)MinMaxLevelOffset, 0);

---
>   float MinMaxTextureLevel = max((float)TextureLevel + (float)MinMaxLevelOffset, 0);

425c391
<

---
>

428a395,403
>   float2 TexPos = float2(Pos.xy) * GeoToTexLevelOffsetInv;

>

> #pragma region S1_Engine_Shiyu

>   float QuadMask = MaskTexture.Load(int3(TexPos, TextureLevel));

>   const float ClipMask = 0.333;

>   bool bMaskCull = QuadMask <= ClipMask;

>   bCull |= bMaskCull;

> #pragma endregion

>

434a410
>

436,437c412,414
<       OutInstance.UVTransform.xyz = GetVirtualToPhysicalUVTransform(Pos, Level, Item.PhysicalAddress, PhysicalPageTransform, NumPhysicalAddressBits);

<

---
>       // OutInstance.UVTransform.xyz = GetVirtualToPhysicalUVTransform(Pos, GeoToTexLevelOffsetInv, TextureLevel, Item.PhysicalAddress, PhysicalPageTransform, NumPhysicalAddressBits);

>       OutInstance.PhysicalAddress.xyz = Item.PhysicalAddress; // todo:just record one address

>

439,440c416,425
<       InterlockedAdd(RWIndirectArgsBuffer[1], 1, Write);

<       RWInstanceBuffer[Write] = OutInstance;

---
>       if (abs(QuadMask - 1.0f) < 1e-3)

>       {

>           InterlockedAdd(RWIndirectArgsBuffer[1], 1, Write);

>           RWInstanceBuffer[Write] = OutInstance;

>       }

>       else

>       {

>           InterlockedAdd(RWIndirectArgsBuffer[5 + 1], 1, Write);

>           RWHoleInstanceBuffer[Write] = OutInstance;

>       }

448a434,437
>       if (!bMaskCull)

>       {

>           InterlockedAdd(RWStatBuffer[sAfterCullOffset * 2], 1);

>       }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh.ush#2 (text) ====

4a5,9
> #define VHM_ENABLE_DEBUG_PRINT 0

> #if VHM_ENABLE_DEBUG_PRINT

> #include "/Engine/Private/ShaderPrint.ush"

> #endif

>

93c98
<   float3 UVTransform;

---
>   // float3 UVTransform;

94a100,105
>   // float3 UVTransformPar;

>   // float Height;

>   // float3 UVTransformPar2;

>   // float Padding;

>   uint3 PhysicalAddress;

>   // uint Padding2;

106a118,126
> uint UnpackLevel2(QuadRenderInstance Item)

> {

>   return (Item.PosLevelPacked >> 24) & 0x7f;

> }

>

> uint UnpackMask2(QuadRenderInstance Item)

> {

>   return Item.PosLevelPacked >> 31;

> }

121a142,262
>

> /** Unpack the virtual level for a PhysicalAddress entry in the virtual texture page table. */

> uint GetVirtualLevelFromPhysicalAddress(uint InPhysicalAddress)

> {

>   // See packing in PageTableUpdate.usf

>   return InPhysicalAddress & 0xf;

> }

>

> /** Compute physical UV from virtual UV in the tile with the given PhysicalAddress. */

> float2 VirtualToPhysicalUV(float2 InVirtualUV, uint InPhysicalAddress, float4 InTransformFactors, uint InNumAddressBits)

> {

>   // See packing in PageTableUpdate.usf

>   float PageX = (float)((InPhysicalAddress >> 4) & ((1u << InNumAddressBits) - 1));

>   float PageY = (float)(InPhysicalAddress >> (4 + InNumAddressBits));

>   float UVScale = 1.f / (float)(1u << GetVirtualLevelFromPhysicalAddress(InPhysicalAddress));

>

>   float2 BaseUV = float2(PageX, PageY) * InTransformFactors.x;

>   float2 PageUV = InVirtualUV * InTransformFactors.y;

>   float2 BorderUV = InTransformFactors.z;

>   float2 HalfTexelUV = InTransformFactors.w;

>

>   return BaseUV + PageUV + BorderUV - HalfTexelUV;

> }

>

> /** Returns transform from virtual to physical UV in the tile with the given PhysicalAddress. Returns float3 where .xy is bias and .z is scale. */

> float3 GetVirtualToPhysicalUVTransform(float2 InPos, float InPosDivider, uint InLevel, uint InPhysicalAddress, float4 InTransformFactors, uint InNumAddressBits)

> {

>   uint LodShift = (uint)max((int)GetVirtualLevelFromPhysicalAddress(InPhysicalAddress) - (int)InLevel, 0);

>   float PosDivider = InPosDivider / (float)(1u << LodShift);

>   float2 MinVirtualUV = frac((float2)InPos * PosDivider);

>   float2 MaxVirtualUV = MinVirtualUV + PosDivider;

>

>   float2 MinPhysicalUV = VirtualToPhysicalUV(MinVirtualUV, InPhysicalAddress, InTransformFactors, InNumAddressBits);

>   float2 MaxPhysicalUV = VirtualToPhysicalUV(MaxVirtualUV, InPhysicalAddress, InTransformFactors, InNumAddressBits);

>

>   return float3(MinPhysicalUV, MaxPhysicalUV.x - MinPhysicalUV.x); // Assume Max.y - Min.y == Max.x - Min.x

> }

>

>

> /** Unpack the values from the MinMaxHeight texture from the packed 8888 format. */

> float2 UnPackMinMaxHeight(float4 InPacked)

> {

>   uint4 PackedScaled = (uint4)floor(InPacked *= 255.f);

>   uint2 UnPackedScaled = uint2(PackedScaled.x << 8 | PackedScaled.y, PackedScaled.z << 8 | PackedScaled.w);

>   float2 UnPacked = (float2)UnPackedScaled / 65535.f;

>   return UnPacked;

> }

>

> /** Unpack the values from the MinMaxLodBias texture from the packed 8888 format. */

> float2 UnPackMinMaxLodBias(float4 InPacked, float InLodBiasScale)

> {

>   return float2(CalculateBiasLod(InPacked.x, InLodBiasScale), CalculateBiasLod(InPacked.y, InLodBiasScale));

> }

>

> /** Return false if the AABB is completely outside one of the planes. */

> bool PlaneTestAABB(float4 InPlanes[5], float3 InCenter, float3 InExtent)

> {

>   bool bPlaneTest = true;

>

>   [unroll]

>   for (uint PlaneIndex = 0; PlaneIndex < 5; ++PlaneIndex)

>   {

>       float3 PlaneSigns;

>       PlaneSigns.x = InPlanes[PlaneIndex].x >= 0.f ? 1.f : -1.f;

>       PlaneSigns.y = InPlanes[PlaneIndex].y >= 0.f ? 1.f : -1.f;

>       PlaneSigns.z = InPlanes[PlaneIndex].z >= 0.f ? 1.f : -1.f;

>

>       bool bInsidePlane = dot(InPlanes[PlaneIndex], float4(InCenter + InExtent * PlaneSigns, 1.0f)) > 0.f;

>       bPlaneTest = bPlaneTest && bInsidePlane;

>   }

>

>   return bPlaneTest;

> }

>

> /* Return squared distance of closest distance between a point and a bounding box. */

> float SquaredMinDistanceToAABB(float3 InPos, float3 InMin, float3 InMax, float3 InScale)

> {

>   float3 D1 = max(InMin - InPos, 0) * InScale;

>   float3 D2 = max(InPos - InMax, 0) * InScale;

>   return dot(D1, D1) + dot(D2, D2);

> }

>

> /* Return squared distance of furthest distance between a point and a bounding box. */

> float SquaredMaxDistanceToAABB(float3 InPos, float3 InMin, float3 InMax, float3 InScale)

> {

>   float3 D = max(abs(InPos - InMin), (InPos - InMax)) * InScale;

>   return dot(D, D);

> }

>

> float SquaredDistance(float2 D)

> {

>   return dot(D, D);

> }

>

> float SquaredDistance(float3 D)

> {

>   return dot(D, D);

> }

>

> /** Draw a bounding box using the ShaderDrawDebug system. */

> void DebugDrawUVBox(float3 InUVMin, float3 InUVMax, float4x4 InTransform, float4 InColor)

> {

> #if VHM_ENABLE_DEBUG_PRINT // Enable only if ShaderDrawDebug is enabled

>   float3 WorldPos[8];

>   WorldPos[0] = mul(float4(InUVMin.x, InUVMin.y, InUVMin.z, 1), InTransform);

>   WorldPos[1] = mul(float4(InUVMax.x, InUVMin.y, InUVMin.z, 1), InTransform);

>   WorldPos[2] = mul(float4(InUVMin.x, InUVMax.y, InUVMin.z, 1), InTransform);

>   WorldPos[3] = mul(float4(InUVMax.x, InUVMax.y, InUVMin.z, 1), InTransform);

>   WorldPos[4] = mul(float4(InUVMin.x, InUVMin.y, InUVMax.z, 1), InTransform);

>   WorldPos[5] = mul(float4(InUVMax.x, InUVMin.y, InUVMax.z, 1), InTransform);

>   WorldPos[6] = mul(float4(InUVMin.x, InUVMax.y, InUVMax.z, 1), InTransform);

>   WorldPos[7] = mul(float4(InUVMax.x, InUVMax.y, InUVMax.z, 1), InTransform);

>

>   AddQuadWS(WorldPos[0], WorldPos[2], WorldPos[3], WorldPos[1], InColor);

>   AddQuadWS(WorldPos[4], WorldPos[6], WorldPos[7], WorldPos[5], InColor);

>   AddLineWS(WorldPos[0], WorldPos[4], InColor, InColor);

>   AddLineWS(WorldPos[1], WorldPos[5], InColor, InColor);

>   AddLineWS(WorldPos[2], WorldPos[6], InColor, InColor);

>   AddLineWS(WorldPos[3], WorldPos[7], InColor, InColor);

> #endif

> }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#2 (text) ====

7c7,9
< #define GRID_SIZE (VHM.NumQuadsPerTileSide+1)

---
> #define GRID_SIZE (VHM.NumInstanceVertexSide+1)

> #define RVT_MIN_LEVEL (VHM.RVTMinLevel)

>

9c11,12
< StructuredBuffer<QuadRenderInstance> InstanceBuffer;

---
> // StructuredBuffer<QuadRenderInstance> InstanceBuffer;

> // #define InstanceBuffer (VHMInst.InstanceBuffer)

12a16,40
>

>

> QuadRenderInstance GetQuadRenderInstance(int InstanceId)

> {

>   float4 Data = VHMInst.InstanceBuffer[InstanceId];

>   QuadRenderInstance Inst;

>   Inst.PosLevelPacked = asuint(Data.x);

>   Inst.PhysicalAddress = asuint(Data.yzw);

>   // Inst.UVTransform = Data.xyz;

>   //

>   // float4 Data1 = VHMInst.InstanceBuffer[InstanceId * 4 + 1];

>   // Inst.UVTransformPar = Data1.xyz;

>   // Inst.Height = Data1.w;

>   //

>   // float4 Data2 = VHMInst.InstanceBuffer[InstanceId * 4 + 2];

>   // Inst.UVTransformPar2 = Data2.xyz;

>   // Inst.Padding = Data2.w;

>   //

>   // float4 Data3 = VHMInst.InstanceBuffer[InstanceId * 4 + 3];

>   // Inst.PhysicalAddress = asuint(Data3.xyz);

>   //

>   // Inst.Padding2 = asuint(Data3.w);

>   return Inst;

> }

>

70a99,109
> // use cdlod morph vetex

> float2 MorphVertex_V2(float InLocalUV, uint InGridSize, uint InMorphFactorFloor, float InMorphFactorFrac)

> {

>   float MorphGridSize = InGridSize >> 1;

>   float2 MorphGridDimensions = float2(MorphGridSize, 1.f / MorphGridSize);

>   float2 MorphOffset1 = frac(InLocalUV * MorphGridDimensions.x) * MorphGridDimensions.y;

>

>   // return InLocalUV - InMorphFactorFrac * MorphOffset1;

>   return InLocalUV;

> }

>

77c116,117
<   const QuadRenderInstance Item = InstanceBuffer[Input.InstanceId];

---
>   // const QuadRenderInstance Item = VHMInst.InstanceBuffer[Input.InstanceId];

>   const QuadRenderInstance Item = GetQuadRenderInstance(Input.InstanceId);

81c121,122
<   const float3 LocalUVTransform = Item.UVTransform;

---
>   // const float3 LocalUVTransform = Item.UVTransform;

>

85a127,130
>   const uint GeoToTexLevelOffset = max(int(RVT_MIN_LEVEL) - int(Level), 0); // geometry levels is large than tex levels

>   const float GeoToTexLevelOffsetInv = 1.0f / float(1u << GeoToTexLevelOffset);

>   const uint TextureLevel = max(int(Level) - int(RVT_MIN_LEVEL), 0);

>

87c132
<   float2 XY = ((float2)Pos + LocalUV) * (float)(1u << Level);

---
>   float2 XY = ((float2)Pos + LocalUV) * GeoToTexLevelOffsetInv * (float)(1u << TextureLevel);

89,90c134,178
<   float SampleLevel = Level;

<

---
>   float SampleLevel = TextureLevel;

>

>   float3 UVTransform[3];

>   {

>       UVTransform[0] = GetVirtualToPhysicalUVTransform(

>           Pos,

>           GeoToTexLevelOffsetInv,

>           TextureLevel,

>           Item.PhysicalAddress[0],

>           VHM.PhysicalPageTransform,

>           VHM.NumPhysicalAddressBits

>       );

>   }

>   {

>       uint ThisLevel = min(Level + 1, VHM.MaxLod);

>       uint _GeoOffset = max(int(RVT_MIN_LEVEL) - int(ThisLevel), 0);

>       float _GeoOffsetInv = 1.0f / float(1u << _GeoOffset);

>       float _TexLevel = max(int(ThisLevel) - int(RVT_MIN_LEVEL), 0);

>       UVTransform[1] = GetVirtualToPhysicalUVTransform(

>           Pos >> 1,

>           _GeoOffsetInv,

>           _TexLevel,

>           Item.PhysicalAddress[1],

>           VHM.PhysicalPageTransform,

>           VHM.NumPhysicalAddressBits

>       );

>   }

>   {

>       uint ThisLevel = min(Level + 2, VHM.MaxLod);

>       uint _GeoOffset = max(int(RVT_MIN_LEVEL) - int(ThisLevel), 0);

>       float _GeoOffsetInv = 1.0f / float(1u << _GeoOffset);

>       float _TexLevel = max(int(ThisLevel) - int(RVT_MIN_LEVEL), 0);

>       UVTransform[2] = GetVirtualToPhysicalUVTransform(

>           Pos >> 2,

>           _GeoOffsetInv,

>           _TexLevel,

>           Item.PhysicalAddress[2],

>           VHM.PhysicalPageTransform,

>           VHM.NumPhysicalAddressBits

>       );

>   }

>   float3 LocalUVTransform = UVTransform[0];

>       // float2 LocalPhysicalUV = LocalUVTransform.xy + LocalUV * LocalUVTransform.z;

>       // float Height = VHM.HeightTexture.SampleLevel(VHM.HeightSampler, LocalPhysicalUV, 0);

>

101a190
>       // float LodClamped = clamp(LodForDistance - LodBias, (float)Level, VHM.MaxLod);

108c197
<

---
>

113,114c202,203
<       LodMorphFrac = 0;

<

---
>       // LodMorphFrac = 0;

>

116a206
>

118c208
<       XY = ((float2)Pos + LocalUV) * (float)(1u << Level);

---
>       XY = ((float2)Pos + LocalUV) * GeoToTexLevelOffsetInv * (float)(1u << TextureLevel);

120c210
<

---
>

122c212,213
<       SampleLevel = max(0, LodClamped - 0.5f);

---
>       // SampleLevel = max(max(0, LodClamped - 0.5f) - float(RVT_MIN_LEVEL), 0);

>       SampleLevel = max(max(0, LodClamped) - float(RVT_MIN_LEVEL), 0);

136a228,251
>   // float2 UV0, UV1, UV2;

>   // {

>   //  UV0 = UVTransform[0].xy + LocalUV * UVTransform[0].z;

>   // }

>   // {

>   //  float2 ParLocalUV = LocalUV / 2;

>   //  ParLocalUV.x += (Pos.x & 0x1) ? 0.5f : 0.0f;

>   //  ParLocalUV.y += (Pos.y & 0x1) ? 0.5f : 0.0f;

>   //  UV1 = UVTransform[1].xy + ParLocalUV * UVTransform[1].z;

>   //

>   //  {

>   //      float2 Par2LocalUV = ParLocalUV / 2;

>   //      Par2LocalUV.x += ((Pos.x >> 1) & 0x1) ? 0.5f : 0.0f;

>   //      Par2LocalUV.y += ((Pos.y >> 1) & 0x1) ? 0.5f : 0.0f;

>   //      UV2 = UVTransform[2].xy + Par2LocalUV * UVTransform[2].z;

>   //  }

>   // }

>   // bool SelectLod = (SampleLevel - TextureLevel) < 1.0f;

>   // float2 UV_0 = SelectLod ? UV0 : UV1;

>   // float2 UV_1 = SelectLod ? UV1 : UV2;

>   // float Height0 = VHM.HeightTexture.SampleLevel(VHM.HeightSampler, UV_0, 0);

>   // float Height1 = VHM.HeightTexture.SampleLevel(VHM.HeightSampler, UV_1, 0);

>   // float Height = lerp(Height0.x, Height1.x, frac(SampleLevel));

>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxRender.cpp#3 (unicode) ====

228a229,427
>

>

> #pragma region S1_Engine_Shiyu

>

> class FVHMMaskTextureCS : public FGlobalShader

> {

> public:

>   SHADER_USE_PARAMETER_STRUCT(FVHMMaskTextureCS, FGlobalShader);

>

>

>   BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

>       SHADER_PARAMETER_RDG_TEXTURE_SRV(Texture2D, SrcTexture)

>       SHADER_PARAMETER_RDG_TEXTURE_UAV(RWTexture2D, DstTexture)

>       SHADER_PARAMETER(FIntPoint, SrcTextureSize)

>       SHADER_PARAMETER(FIntPoint, DstTextureCoord)

>   END_SHADER_PARAMETER_STRUCT()

>

> };

>

> enum class EVHMMaskFormat

> {

>   EVHMMask_RGBA8,

>   EVHMMask_R8

> };

>

> template< EVHMMaskFormat InputFormat>

> class TVHMMaskTextureCS: public FVHMMaskTextureCS

> {

>   DECLARE_SHADER_TYPE(TVHMMaskTextureCS, Global)

>

>   TVHMMaskTextureCS()

>   {}

>

>   TVHMMaskTextureCS(const ShaderMetaType::CompiledShaderInitializerType& Initializer)

>       : FVHMMaskTextureCS(Initializer)

>   {}

>

>   static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

>   {

>       return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

>   }

>

>   static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters,

>                                            FShaderCompilerEnvironment& OutEnvironment)

>   {

>       FGlobalShader::ModifyCompilationEnvironment(Parameters, OutEnvironment);

>       switch (InputFormat)

>       {

>       case EVHMMaskFormat::EVHMMask_RGBA8:

>           OutEnvironment.SetDefine(TEXT("INPUT_FORMAT_MASK_RGBA8"), 1); break;

>       case EVHMMaskFormat::EVHMMask_R8:

>           OutEnvironment.SetDefine(TEXT("INPUT_FORMAT_MAST_R8"), 1); break;

>       }

>   }

> };

>

> /** Implementations of the used shader variations. */

> #define IMPLEMENT_VHM_MASK_SHADER_TYPE(Input, ShaderName) \

>   typedef TVHMMaskTextureCS<Input> TVHMMaskTextureCS##ShaderName; \

>   IMPLEMENT_SHADER_TYPE(template<>, TVHMMaskTextureCS##ShaderName, TEXT("/Plugin/VirtualHeightfieldMesh/Private/HeightfieldMaskRender.usf"), TEXT("MaskCopyCS"), SF_Compute);

>

> IMPLEMENT_VHM_MASK_SHADER_TYPE(EVHMMaskFormat::EVHMMask_RGBA8, _RGBA8);

> IMPLEMENT_VHM_MASK_SHADER_TYPE(EVHMMaskFormat::EVHMMask_R8, _R8);

>

> namespace VirtualHeightfieldMesh

> {

>   /** Initial pass that reads from R16 height. */

>   void AddMaskFirstPass(FRDGBuilder& GraphBuilder, FRDGTextureSRVRef Src, FIntPoint SrcSize,

>       FRDGTextureUAVRef Dst)

>   {

>       TShaderMapRef<TVHMMaskTextureCS_RGBA8> ComputeShader(GetGlobalShaderMap(GMaxRHIFeatureLevel));

>

>       FVHMMaskTextureCS::FParameters* Parameters = GraphBuilder.AllocParameters<FVHMMaskTextureCS::FParameters>();

>       Parameters->SrcTexture = Src;

>       Parameters->DstTexture = Dst;

>       Parameters->SrcTextureSize = SrcSize;

>       Parameters->DstTextureCoord = {0, 0};

>

>       const FIntVector GroupCount((SrcSize.X / 2 + 7) / 8, (SrcSize.Y / 2 + 7) / 8, 1);

>

>       FComputeShaderUtils::AddPass(

>           GraphBuilder,

>           RDG_EVENT_NAME("MaskFirstCS"),

>           ComputeShader, Parameters, GroupCount);

>   }

>

>   void AddMaskMipsPass(FRDGBuilder& GraphBuilder, FRDGTextureSRVRef Src, FIntPoint SrcSize, FRDGTextureUAVRef Dst,

>       FIntPoint DstCoord)

>   {

>       TShaderMapRef<TVHMMaskTextureCS_R8> ComputeShader(GetGlobalShaderMap(GMaxRHIFeatureLevel));

>

>       FVHMMaskTextureCS::FParameters* Parameters = GraphBuilder.AllocParameters<FVHMMaskTextureCS::FParameters>();

>       Parameters->SrcTexture = Src;

>       Parameters->DstTexture = Dst;

>       Parameters->SrcTextureSize = SrcSize;

>       Parameters->DstTextureCoord = DstCoord;

>

>       const FIntVector GroupCount((SrcSize.X / 2 + 7) / 8, (SrcSize.Y / 2 + 7) / 8, 1);

>

>       ClearUnusedGraphResources(ComputeShader, Parameters);

>

>       FComputeShaderUtils::AddPass(

>           GraphBuilder,

>           RDG_EVENT_NAME("MaskMipsCS (%d x %d)", SrcSize, SrcSize),

>           ComputeShader, Parameters, GroupCount);

>   }

>

>   void DownsampleMaskAndCopy(FRDGBuilder& GraphBuilder, FRDGTexture* SrcTexture, FIntPoint SrcSize,

>       FRDGTextureUAV* DstTextureUAV, FIntPoint DstCoord)

>   {

>       const FIntPoint DownsampleTextureSize = FIntPoint(SrcSize.X / 2, SrcSize.X / 2);

>

>       const int32 NumMips = FMath::FloorLog2(FMath::Max(DownsampleTextureSize.X, DownsampleTextureSize.Y)) + 1;

>       check(NumMips > 1);

>

>

>       const ETextureCreateFlags TextureFlags = TexCreate_ShaderResource | TexCreate_UAV | TexCreate_GenerateMipCapable | TexCreate_RenderTargetable;

>       const FRDGTextureDesc Desc = FRDGTextureDesc::Create2D(DownsampleTextureSize, PF_R8, FClearValueBinding::None, TextureFlags, NumMips);

>       FRDGTextureRef DownsampleTexture = GraphBuilder.CreateTexture(Desc, TEXT("DownsampleTexture"));

>

>

>       FIntPoint Size = SrcSize;

>       for (int32 MipLevel = 0; MipLevel < NumMips; ++MipLevel)

>       {

>           const bool bFirstPass = MipLevel == 0;

>           const bool bLastPass = MipLevel == NumMips - 1;

>

>           FRDGTextureSRVRef SRV;

>           if (bFirstPass)

>           {

>               SRV = GraphBuilder.CreateSRV(FRDGTextureSRVDesc::Create(SrcTexture));

>           }

>           else

>           {

>               SRV = GraphBuilder.CreateSRV(FRDGTextureSRVDesc::CreateForMipLevel(DownsampleTexture, MipLevel - 1));

>           }

>

>           FRDGTextureUAVRef UAV;

>           if (bLastPass)

>           {

>               UAV = DstTextureUAV;

>           }

>           else

>           {

>               UAV = GraphBuilder.CreateUAV(FRDGTextureUAVDesc(DownsampleTexture, MipLevel));

>           }

>

>           if (bFirstPass)

>           {

>               AddMaskFirstPass(GraphBuilder, SRV, Size, UAV);

>           }

>           else

>           {

>               AddMaskMipsPass(GraphBuilder, SRV, Size, UAV, bLastPass ? DstCoord : FIntPoint{0, 0});

>           }

>

>           Size.X = FMath::Max(Size.X / 2, 1);

>           Size.Y = FMath::Max(Size.Y / 2, 1);

>       }

>   }

>

>   void AddGenerateMaskNextLevelPass(FRDGBuilder& GraphBuilder, FRDGTextureSRVRef Src, FIntPoint SrcSize,

>       FRDGTextureUAVRef Dst)

>   {

>       const TShaderMapRef<TVHMMaskTextureCS_R8> ComputeShader(GetGlobalShaderMap(GMaxRHIFeatureLevel));

>

>       FVHMMaskTextureCS::FParameters* Parameters = GraphBuilder.AllocParameters<FVHMMaskTextureCS::FParameters>();

>       Parameters->SrcTexture = Src;

>       Parameters->DstTexture = Dst;

>       Parameters->SrcTextureSize = SrcSize;

>       Parameters->DstTextureCoord = {0, 0};

>

>       const FIntVector GroupCount((SrcSize.X / 2 + 7) / 8, (SrcSize.Y / 2 + 7) / 8, 1);

>

>       FComputeShaderUtils::AddPass(

>           GraphBuilder,

>           RDG_EVENT_NAME("GenerateMaskNextLevelCS"),

>           ComputeShader, Parameters, GroupCount);

>   }

>

>

>   void GenerateMaskTextureMips(FRDGBuilder& GraphBuilder, FRDGTexture* Texture, FIntPoint SrcSize, int32 NumMips)

>   {

>       FIntPoint Size = SrcSize;

>       for (int32 MipLevel = 1; MipLevel < NumMips; ++MipLevel)

>       {

>           FRDGTextureSRVRef SRV = GraphBuilder.CreateSRV(FRDGTextureSRVDesc::CreateForMipLevel(Texture, MipLevel - 1));

>           FRDGTextureUAVRef UAV = GraphBuilder.CreateUAV(FRDGTextureUAVDesc(Texture, MipLevel));

>

>           AddMaskMipsPass(GraphBuilder, SRV, Size, UAV, FIntPoint{0, 0});

>

>           Size.X = FMath::Max(Size.X / 2, 1);

>           Size.Y = FMath::Max(Size.Y / 2, 1);

>

>       }

>   }

> }

>

> #pragma endregion


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.cpp#3 (unicode) ====

7a8,11
> #pragma region S1_Engine_Shiyu

> #include "HeightfieldMaskTexture.h"

> #pragma endregion

>

21a26,40
>

> #pragma region S1_Engine_Shiyu

>

>   void NotifyComponents(UHeightfieldMaskTexture const* MaskTexture)

>   {

>       for (TObjectIterator<UVirtualHeightfieldMeshComponent> It; It; ++It)

>       {

>           if (It->GetMaskTexture() == MaskTexture)

>           {

>               It->MarkRenderStateDirty();

>           }

>       }

>   }

>

> #pragma endregion


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.h#3 (unicode) ====

6a7
> class UHeightfieldMaskTexture;

15a17,19
>

>   void NotifyComponents(UHeightfieldMaskTexture const* MaskTexture);

>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp#3 (unicode) ====

12a13,16
> #pragma region S1_Engine_Shiyu

> #include "HeightfieldMaskTexture.h"

> #pragma endregion

>

118a123,128
> #pragma region S1_Engine_Shiyu

>   if (HoleMaterial != nullptr)

>   {

>       OutMaterials.Add(HoleMaterial);

>   }

> #pragma endregion

163a174,179
> bool UVirtualHeightfieldMeshComponent::IsMaskTextureEnabled() const

> {

>   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();

>   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;

> }

>

181a198,221
> #pragma region S1_Engine_Shiyu

> void UVirtualHeightfieldMeshComponent::InitializeMaskTexture(uint32 InSizeX, uint32 InSizeY,

>   uint32 InNumMips, uint8* InData)

> {

>   // We need an existing StreamingTexture object to update.

>   if (MaskTexture != nullptr)

>   {

>       FHeightfieldMaskTextureBuildDesc BuildDesc;

>       BuildDesc.SizeX = InSizeX;

>       BuildDesc.SizeY = InSizeY;

>       BuildDesc.NumMips = InNumMips;

>       BuildDesc.Data = InData;

>

>       MaskTexture->Modify();

>       MaskTexture->BuildTexture(BuildDesc);

>

>       MarkRenderStateDirty();

>   }

> }

>

>

> #pragma endregion

>

>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#4 (unicode) ====

16c16
<       0,  // shiyu: now we need to open it by console

---
>       1,  // shiyu: now we need to open it by console

70c70,72
<       return CVarVHMEnable.GetValueOnAnyThread() != 0 && (InFeatureLevel >= ERHIFeatureLevel::SM5) && UseVirtualTexturing(InFeatureLevel);

---
>       return CVarVHMEnable.GetValueOnAnyThread() != 0

>           && (InFeatureLevel >= ERHIFeatureLevel::SM5 || InFeatureLevel == ERHIFeatureLevel::ES3_1)

>           && UseVirtualTexturing(InFeatureLevel);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#4 (unicode) ====

10a11
> #include "HeightfieldMaskTexture.h"

11a13
> #include "HLSLTypeAliases.h"

15a18
> #include "RenderCaptureInterface.h"

19a23
> #include "SystemTextures.h"

63c67
<   1024 * 4,

---
>   1024 * 64,

70c74
<   1024,

---
>   1024 * 4,

77c81
<   1024 * 4,

---
>   1024 * 64,

84c88
<   16,

---
>   1,

88a93,99
> static TAutoConsoleVariable<int32> CVarVHMVersion(

>   TEXT("r.VHM.Version"),

>   3,

>   TEXT("Version Of VHM"),

>   ECVF_RenderThreadSafe

> );

>

102a114
> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawTriangles"), STAT_VHM_DrawTriangles, STATGROUP_VHM)

103a116,117
> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Opacity"), STAT_VHM_DrawOpacityInstances, STATGROUP_VHM)

> DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Mask"), STAT_VHM_DrawMaskInstances, STATGROUP_VHM)

117a132,141
> static constexpr int32 IndirectArgsCount = 10;

> static constexpr int32 IndirectArgsByteSize = 4 * sizeof(uint32) * IndirectArgsCount;

> static constexpr int32 MergeDispatchArgsOffset = 5;

> #pragma region S1_Engine_Shiyu

> #if VHM_ENABLE_STAT

> static constexpr int32 MaxStatCount = 64;

> static constexpr int32 StatBufferByteSize = sizeof(uint32) * MaxStatCount;

> #endif

> #pragma endregion

>

132a157,161
>       /* Culled hold instance buffer */

>       FBufferRHIRef HoleInstanceBuffer;

>       FUnorderedAccessViewRHIRef HoleInstanceBufferUAV;

>       FShaderResourceViewRHIRef HoleInstanceBufferSRV;

>

152c181,184
< #pragma region S1_Engine_Shiyu

---
> #pragma region S1_Engine_Shiyu

>       InBuffers.HoleInstanceBuffer.SafeRelease();

>       InBuffers.HoleInstanceBufferUAV.SafeRelease();

>       InBuffers.HoleInstanceBufferSRV.SafeRelease();

157a190,273
>

>   namespace V2

>   {

>       struct FInnerBuffers

>       {

>           // // for ps

>           // // - use to draw quad by default material

>           // FBufferRHIRef QuadInstanceArgsBuffer;

>           // FUnorderedAccessViewRHIRef QuadInstanceArgsBufferUAV;

>           // FBufferRHIRef QuadInstanceBuffer;

>           // FUnorderedAccessViewRHIRef QuadInstanceBufferUAV;

>           // FShaderResourceViewRHIRef QuadInstanceBufferSRV;

>           // // - use to draw quad by hole material

>           // FBufferRHIRef HoleQuadInstanceArgsBuffer;

>           // FUnorderedAccessViewRHIRef HoleQuadInstanceArgsBufferUAV;

>           // FBufferRHIRef HoleQuadInstanceBuffer;

>           // FUnorderedAccessViewRHIRef HoleQuadInstanceBufferUAV;

>           // FShaderResourceViewRHIRef HoleQuadInstanceBufferSRV;

>

>           int32 CalTime = -1;

>           // use to compure shader

>           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};

>           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};

>           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};

>           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};

>           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};

>           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};

>

>           FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

>           {

>               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

>           }

>           FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

>           {

>               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

>           }

>

>           FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

>           {

>               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

>           }

>           FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

>           {

>               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

>           }

>       };

>

>       void InitializeInnerBuffers(FRHICommandListImmediate& RHICmdList, FInnerBuffers& InBuffers)

>       {

>           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();

>           const TCHAR* FinalQuadName[2] = {

>               TEXT("VHM.FinalQuadBuffer_0"),

>               TEXT("VHM.FinalQuadBuffer_1")};

>           const TCHAR* FinalQuadArgsName[2] = {

>               TEXT("VHM.FinalQuadArgsBuffer_0"),

>               TEXT("VHM.FinalQuadArgsBuffer_1")};

>

>           for (int i = 0; i < 2; ++i)

>           {

>               InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(

>                   FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),

>                   FinalQuadArgsName[i]

>               );

>               InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(

>                   FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)

>

>                   ,

>                   FinalQuadName[i]

>               );

>           }

>       }

>

>       void ReleaseInnerBuffers(FInnerBuffers& InBuffers)

>       {

>           InBuffers.CalTime = -1;

>           for(int i = 0; i < 2; ++i)

>           {

>               InBuffers.FinalQuadArgsBuffer[i].SafeRelease();

>               InBuffers.FinalQuadBuffer[i].SafeRelease();

>           }

>

>       }

>   }

>

189a306,307
>

>

209a328,391
>

>

>   namespace V2

>   {

>       BEGIN_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters,)

>           SHADER_PARAMETER(FVector3f,         ViewOrigin)

>           SHADER_PARAMETER(uint32,            OutBufferSizeMask)

>           SHADER_PARAMETER(FVector3f,         UVToWorldScale)

>           SHADER_PARAMETER(uint32,            FinalQuadBufferSizeMask)

>           SHADER_PARAMETER_ARRAY(FVector4f,   FrustumPlanes, [5])

>           SHADER_PARAMETER(FMatrix44f,        UVToWorld)

>           SHADER_PARAMETER(FVector4f,         LodDistances)

>           SHADER_PARAMETER(uint32,            MaxLevel)

>           SHADER_PARAMETER(uint32,            RVTMinLevel)

>           SHADER_PARAMETER(uint32,            PageTableFeedbackId)

>           SHADER_PARAMETER(uint32,            NumPhysicalAddressBits)

>           SHADER_PARAMETER(FVector4f,         PageTableSize)

>           SHADER_PARAMETER(FVector4f,         PhysicalPageTransform)

>           SHADER_PARAMETER(uint32,            QuadInstanceBufferSizeMask)

>           SHADER_PARAMETER(uint32,            NumIndices)

>           SHADER_PARAMETER(uint32,            MaxArgsCount)

>           SHADER_PARAMETER(uint32,            MaxStatCount)

>           SHADER_PARAMETER(uint32,            MergeDispatchArgsOffset)

>       END_UNIFORM_BUFFER_STRUCT()

>

>       IMPLEMENT_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters, "VHMParam")

>

>       struct FVolatileBuffers

>       {

>           FVHMCSSharedParameters* VHMParameter=nullptr;

>           TRDGUniformBufferRef<FVHMCSSharedParameters> VHMParameterUBuffer;

>           TArray<FRDGBufferRef, TFixedAllocator<2>> ArgsBuffer{nullptr, nullptr};

>           TArray<FRDGBufferRef, TFixedAllocator<2>> SubdivideQuadBuffer{nullptr, nullptr};

>           TArray<FRDGBufferRef, TFixedAllocator<2>> MergeQuadBuffer{nullptr, nullptr};

>

>

>           struct FSRVAndUAV

>           {

>               FRDGBufferSRVRef SRV = nullptr;

>               FRDGBufferUAVRef UAV = nullptr;

>               void Create(FRDGBuilder& GraphBuilder, FRDGBufferRef Buffer)

>               {

>                   EPixelFormat Format = uint32(Buffer->Desc.Usage & EBufferUsageFlags::DrawIndirect) != 0 ? PF_R32_UINT : PF_R32G32B32A32_UINT;

>                   SRV = GraphBuilder.CreateSRV(Buffer, Format);

>                   UAV = GraphBuilder.CreateUAV(Buffer, Format);

>               }

>           };

>           TArray<FSRVAndUAV, TFixedAllocator<2>> ArgsViews{{}, {}};

>           TArray<FSRVAndUAV, TFixedAllocator<2>> SubdivideViews{{}, {}};

>           TArray<FSRVAndUAV, TFixedAllocator<2>> MergeViews{{}, {}};

>           FRDGBufferRef FeedbackBuffer;

>           FRDGBufferUAVRef FeedbackBufferUAV;

>

>           FRHITexture* PageTableTexture = nullptr;

>           FRHITexture* MaskTexture = nullptr;

>           FRHIShaderResourceView* HeightTexture = nullptr;

>           FRHITexture* HeightMinMaxTexture = nullptr;

>

> #if VHM_ENABLE_STAT

>           FRDGBufferRef StatBuffer;

>           FRDGBufferUAVRef StatBufferUAV;

> #endif

>       };

>   }

234a417,422
>   void InitVolatileBuffers(FRDGBuilder& GraphBuilder, int WorkIndex, VirtualHeightfieldMesh::V2::FVolatileBuffers& VolatileBuffers);

>

>   // void SubmitWork_V2(FRDGBuilder& GraphBuilder);

>

>   void SubmitWork_V3(FRDGBuilder& GraphBuilder);

>

252a441
>

257a447
>   TArray<VirtualHeightfieldMesh::V2::FInnerBuffers> InnerBuffers;

360a551
>       InnerBuffers.AddDefaulted(); // index is equal to BufferIndex

362a554
>       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(GetImmediateCommandList_ForRenderCommand(), InnerBuffers[WorkDesc.BufferIndex]);

385c577,589
<       SubmitWork(GraphBuilder);

---
>       uint32 VHMVersion = CVarVHMVersion.GetValueOnRenderThread();

>       if (VHMVersion == 1)

>       {

>           SubmitWork(GraphBuilder);

>       }

>       else if(VHMVersion == 2)

>       {

>           // SubmitWork_V2(GraphBuilder);

>       }

>       else

>       {

>           SubmitWork_V3(GraphBuilder);

>       }

406a611
>           VirtualHeightfieldMesh::V2::ReleaseInnerBuffers(InnerBuffers[Index]);

407a613
>           InnerBuffers.RemoveAtSwap(Index);

432a639,641
> #pragma region S1_Engine_Shiyu

>   , MaskTexture(nullptr)

> #pragma endregion

435c644
<   , NumQuadsPerTileSide(0)

---
>   , NumQuadsPerTileOfTwo(4) // (1 << 4) * (1 << 4)

474a684,696
>

> #pragma region S1_Engine_Shiyu

>   UMaterialInterface* HoleComponentMaterial = InComponent->GetHoleMaterial();

>   const bool bValidHoleMaterial = HoleComponentMaterial != nullptr && HoleComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);

>   HoleMaterial = bValidHoleMaterial ? HoleComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();

>   HoleMaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());

>

>   UHeightfieldMaskTexture* HeightfieldMaskTexture = InComponent->GetMaskTexture();

>   if (HeightfieldMaskTexture)

>   {

>       MaskTexture = HeightfieldMaskTexture->Texture;

>   }

> #pragma endregion

511c733
<           NumQuadsPerTileSide = RuntimeVirtualTexture->GetTileSize();

---
>

514a737,739
>               uint32 TileSize = FMath::FloorLog2(RuntimeVirtualTexture->GetTileSize());

>               check(TileSize >= NumQuadsPerTileOfTwo);

>               NumInstanceVertexSide = 1 << (TileSize - NumQuadsPerTileOfTwo);

522c747,753
<               UniformParams.NumQuadsPerTileSide = NumQuadsPerTileSide;

---
>               UniformParams.NumInstanceVertexSide = NumInstanceVertexSide;

>               {

>                   uint32 TileSizeLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileSize());

>                   check(TileSizeLevel >= NumQuadsPerTileOfTwo);

>                   UniformParams.MaxLod = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;

>                   UniformParams.RVTMinLevel = NumQuadsPerTileOfTwo;

>               }

542d772
<               UniformParams.MaxLod = AllocatedVirtualTexture->GetMaxLevel();

544a775,782
>               const float PageSize = AllocatedVirtualTexture->GetVirtualTileSize();

>               const float PageBorderSize = AllocatedVirtualTexture->GetTileBorderSize();

>               const float PageAndBorderSize = PageSize + PageBorderSize * 2.f;

>               const float HalfTexelSize = 0.5f;

>               const FVector4 PhysicalPageTransform = FVector4(PageAndBorderSize, PageSize, PageBorderSize, HalfTexelSize) * (1.f / PhysicalTextureSize);

>               UniformParams.PhysicalPageTransform = (FVector4f)PhysicalPageTransform;

>               UniformParams.NumPhysicalAddressBits = AllocatedVirtualTexture->GetPageTableFormat() == EVTPageTableFormat::UInt16 ? 6 : 8; // See packing in PageTableUpdate.usf

>

613a852,855
>           if (!IsShadowCast(Views[ViewIndex]) && ViewFamily.Views[0] != Views[ViewIndex])

>           {

>               continue;

>           }

616,627c858,910
<           FMeshBatch& Mesh = Collector.AllocateMesh();

<           Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

<           Mesh.bUseWireframeSelectionColoring = IsSelected();

<           Mesh.VertexFactory = VertexFactory;

<           Mesh.MaterialRenderProxy = Material;

<           Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

<           Mesh.Type = PT_TriangleList;

<           Mesh.DepthPriorityGroup = SDPG_World;

<           Mesh.bCanApplyViewModeOverrides = true;

<           Mesh.bUseForMaterial = true;

<           Mesh.CastShadow = true;

<           Mesh.bUseForDepthPass = true;

---
>           {

>               FMeshBatch& Mesh = Collector.AllocateMesh();

>               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

>               Mesh.bUseWireframeSelectionColoring = IsSelected();

>               Mesh.VertexFactory = VertexFactory;

>               Mesh.MaterialRenderProxy = Material;

>               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

>               Mesh.Type = PT_TriangleList;

>               Mesh.DepthPriorityGroup = SDPG_World;

>               Mesh.bCanApplyViewModeOverrides = true;

>               Mesh.bUseForMaterial = true;

>               Mesh.CastShadow = true;

>               Mesh.bUseForDepthPass = true;

>

>               Mesh.Elements.SetNumZeroed(1);

>               {

>                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

>

>                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

>                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

>                   BatchElement.IndirectArgsOffset = 0;

>

>                   BatchElement.FirstIndex = 0;

>                   BatchElement.NumPrimitives = 0;

>                   BatchElement.MinVertexIndex = 0;

>                   BatchElement.MaxVertexIndex = 0;

>

>                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

>                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

>

>                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

>                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

>                   Parameters2.InstanceBuffer = Buffers.InstanceBufferSRV;

>                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

>                   BatchElement.UserData = (void*)UserData;

>

>                   UserData->InstanceBufferSRV = Buffers.InstanceBufferSRV;

>

>                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

>                   FSceneView const* MainView = ViewFamily.Views[0];

>                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

>

> #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

>                   // Support the freezerendering mode. Use any frozen view state for culling.

>                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

>                   if (FrozenViewMatrices != nullptr)

>                   {

>                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

>                   }

> #endif

>

>                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

>               }

629c912,915
<           Mesh.Elements.SetNumZeroed(1);

---
>               Collector.AddMesh(ViewIndex, Mesh);

>           }

> #pragma region S1_Engine_Shiyu

>           // for hole quad

631c917,932
<               FMeshBatchElement& BatchElement = Mesh.Elements[0];

---
>               FMeshBatch& Mesh = Collector.AllocateMesh();

>               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

>               Mesh.bUseWireframeSelectionColoring = IsSelected();

>               Mesh.VertexFactory = VertexFactory;

>               Mesh.MaterialRenderProxy = HoleMaterial;

>               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

>               Mesh.Type = PT_TriangleList;

>               Mesh.DepthPriorityGroup = SDPG_World;

>               Mesh.bCanApplyViewModeOverrides = true;

>               Mesh.bUseForMaterial = true;

>               Mesh.CastShadow = true;

>               Mesh.bUseForDepthPass = true;

>

>               Mesh.Elements.SetNumZeroed(1);

>               {

>                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

633,635c934,936
<               BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

<               BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

<               BatchElement.IndirectArgsOffset = 0;

---
>                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

>                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

>                   BatchElement.IndirectArgsOffset = 5 * sizeof(uint32);

637,640c938,941
<               BatchElement.FirstIndex = 0;

<               BatchElement.NumPrimitives = 0;

<               BatchElement.MinVertexIndex = 0;

<               BatchElement.MaxVertexIndex = 0;

---
>                   BatchElement.FirstIndex = 0;

>                   BatchElement.NumPrimitives = 0;

>                   BatchElement.MinVertexIndex = 0;

>                   BatchElement.MaxVertexIndex = 0;

642,643c943,944
<               BatchElement.PrimitiveIdMode = PrimID_ForceZero;

<               BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

---
>                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

>                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

645,646c946,952
<               FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

<               BatchElement.UserData = (void*)UserData;

---
>                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

>

>                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

>                   Parameters2.InstanceBuffer = Buffers.HoleInstanceBufferSRV;

>                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

>

>                   BatchElement.UserData = (void*)UserData;

648c954
<               UserData->InstanceBufferSRV = Buffers.InstanceBufferSRV;

---
>                   UserData->InstanceBufferSRV = Buffers.HoleInstanceBufferSRV;

650,652c956,958
<               //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

<               FSceneView const* MainView = ViewFamily.Views[0];

<               UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

---
>                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

>                   FSceneView const* MainView = ViewFamily.Views[0];

>                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

655,660c961,966
<               // Support the freezerendering mode. Use any frozen view state for culling.

<               const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

<               if (FrozenViewMatrices != nullptr)

<               {

<                   UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

<               }

---
>                   // Support the freezerendering mode. Use any frozen view state for culling.

>                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

>                   if (FrozenViewMatrices != nullptr)

>                   {

>                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

>                   }

663c969,972
<               UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

---
>                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

>               }

>

>               Collector.AddMesh(ViewIndex, Mesh);

664a974
>       }

666,667c976
<           Collector.AddMesh(ViewIndex, Mesh);

<       }

---
> #pragma endregion

750,755c1059
<   static const int32 IndirectArgsByteSize = 4 * sizeof(uint32);

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   static constexpr int32 StatBufferByteSize = sizeof(uint32) * 32;

< #endif

< #pragma endregion

---
>

769c1073,1079
<       uint32 AddressLevelPacked;

---
>       // uint32 AddressLevelPacked;

>       // float UVTransformPar[3];

>       // float Height;

>       // float UVTransformPar2[3];

>       // float Padding;

>       uint32 PhysicalAddress[3];

>       // uint32 Padding2;

785d1094
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, RWQuadBuffer)

796,799c1105,1108
<       static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       {

<           return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       }

---
>       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

>       // {

>       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

>       // }

819a1129
>           SHADER_PARAMETER(uint32, RVTMinLevel)

834c1144
< #pragma region S1_Engine_Shiyu

---
> #pragma region S1_Engine_Shiyu

841,844c1151,1154
<       static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       {

<           return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       }

---
>       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

>       // {

>       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

>       // }

869,873d1178
<

<       static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       {

<           return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       }

889,893d1193
<       static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       {

<           return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       }

<

897a1198
>           SHADER_PARAMETER(uint32, RVTMinLevel)

908c1209,1211
< #pragma region S1_Engine_Shiyu

---
> #pragma region S1_Engine_Shiyu

>           SHADER_PARAMETER_TEXTURE(Texture2D, MaskTexture)

>           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWHoleInstanceBuffer)

928a1232,1434
>   namespace V2

>   {

> //        class FFirstInitBuffers_CS : public FGlobalShader

> //        {

> //        public:

> //            DECLARE_GLOBAL_SHADER(FFirstInitBuffers_CS);

> //            SHADER_USE_PARAMETER_STRUCT(FFirstInitBuffers_CS, FGlobalShader);

> //

> //            BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

> //                SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

> //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

> //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, FinalQuadBuffer)

> //                SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

> //            END_SHADER_PARAMETER_STRUCT()

> //        };

> //        IMPLEMENT_GLOBAL_SHADER(FFirstInitBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "FirstInitBuffersCS", SF_Compute);

>

>       class FInitAllBuffers_CS : public FGlobalShader

>       {

>       public:

>           DECLARE_GLOBAL_SHADER(FInitAllBuffers_CS);

>           SHADER_USE_PARAMETER_STRUCT(FInitAllBuffers_CS, FGlobalShader);

>

>           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

>               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer1)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer2)

>               SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

> #if VHM_ENABLE_STAT

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

> #endif

>           END_SHADER_PARAMETER_STRUCT()

>

>           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

>           {

> #if VHM_ENABLE_STAT

>               Environment.SetDefine(TEXT("VHM_STAT"), 1);

> #endif

>           }

>       };

>       IMPLEMENT_GLOBAL_SHADER(FInitAllBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "InitAllBuffersCS", SF_Compute);

>

>

>       class FFillLevel4Quad_CS : public FGlobalShader

>       {

>       public:

>           DECLARE_GLOBAL_SHADER(FFillLevel4Quad_CS)

>           SHADER_USE_PARAMETER_STRUCT(FFillLevel4Quad_CS, FGlobalShader)

>

>           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

>               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

>               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>           END_SHADER_PARAMETER_STRUCT()

>       };

>       IMPLEMENT_GLOBAL_SHADER(FFillLevel4Quad_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "FillLevel4QuadCS", SF_Compute);

>

>

>       // class FCollectQuadsFromPreFrame_CS : public FGlobalShader

>       // {

>       // public:

>       //  DECLARE_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS);

>       //  SHADER_USE_PARAMETER_STRUCT(FCollectQuadsFromPreFrame_CS, FGlobalShader);

>       //

>       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

>       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>       //      RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

>       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

>       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

>       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

>       //  END_SHADER_PARAMETER_STRUCT()

>       // };

>       // IMPLEMENT_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectQuadsFromPreFrameCS", SF_Compute);

>       //

>

>       class FCollectSubdivideQuads_CS : public FGlobalShader

>       {

>       public:

>           DECLARE_GLOBAL_SHADER(FCollectSubdivideQuads_CS);

>           SHADER_USE_PARAMETER_STRUCT(FCollectSubdivideQuads_CS, FGlobalShader);

>

>           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

>           class FWithFeedback : SHADER_PERMUTATION_BOOL("VHM_WITH_FEEDBACK");

>           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim, FWithFeedback>;

>

>           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

>               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>               SHADER_PARAMETER(uint32, CurPassCalTime)

>               RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

>               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

>               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

>               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

>               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

>               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

>           END_SHADER_PARAMETER_STRUCT()

>

>       };

>       IMPLEMENT_GLOBAL_SHADER(FCollectSubdivideQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectSubdivideQuadsCS", SF_Compute);

>

>       // class FCollectMergeQuads_CS : public FGlobalShader

>       // {

>       // public:

>       //  DECLARE_GLOBAL_SHADER(FCollectMergeQuads_CS);

>       //  SHADER_USE_PARAMETER_STRUCT(FCollectMergeQuads_CS, FGlobalShader);

>       //

>       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

>       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>       //      SHADER_PARAMETER(uint32, CurPassCalTime)

>       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

>       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

>       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

>       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

>       //  END_SHADER_PARAMETER_STRUCT()

>       // };

>       // IMPLEMENT_GLOBAL_SHADER(FCollectMergeQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectMergeQuadsCS", SF_Compute);

>

>       // class FCollectRemainQuads_CS : public FGlobalShader

>       // {

>       // public:

>       //  DECLARE_GLOBAL_SHADER(FCollectRemainQuads_CS);

>       //  SHADER_USE_PARAMETER_STRUCT(FCollectRemainQuads_CS, FGlobalShader);

>       //

>       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

>       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

>       //      SHADER_PARAMETER(uint32, RemainCS_DispatchArgsOffset)

>       //      SHADER_PARAMETER(uint32, CurPassCalTime)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

>       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

>       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

>       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

>       //  END_SHADER_PARAMETER_STRUCT()

>       // };

>       // IMPLEMENT_GLOBAL_SHADER(FCollectRemainQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectRemainQuadsCS", SF_Compute);

>       //

>       class FCullQuadsAndGenerateInstances_CS : public FGlobalShader

>       {

>       public:

>           DECLARE_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS);

>           SHADER_USE_PARAMETER_STRUCT(FCullQuadsAndGenerateInstances_CS, FGlobalShader);

>

>           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

>           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim>;

>

>           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

>               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

>               RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

>               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

>               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

>               SHADER_PARAMETER_UAV(RWBuffer<uint>,                InstanceArgsBuffer)

>               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    QuadInstanceBuffer)

>               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    HoleQuadInstanceBuffer)

>               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

>               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

>               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

>               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

>               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

> #if VHM_ENABLE_STAT

>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

> #endif

>           END_SHADER_PARAMETER_STRUCT()

>

>           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

>           {

> #if VHM_ENABLE_STAT

>               Environment.SetDefine(TEXT("VHM_STAT"), 1);

> #endif

>           }

>       };

>       IMPLEMENT_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CullQuadsAndGenerateInstancesCS", SF_Compute);

>   };

>

1022a1529,1533
>

> #pragma region S1_Engine_Shiyu

>       FRHITexture* MaskTexture;

> #pragma endregion

>

1024a1536
>       uint32 RVTMinLevel;

1032c1544,1545
<       uint32 NumQuadsPerTileSide;

---
>       uint32 NumQuadsPerTileOfTwo;

>       uint32 NumInstanceVertexSide; // Instance is a Plane, size is NumInstanceVertexSide * NumInstanceVertexSide

1037a1551,1552
>

>       uint32 NumIndices;

1100c1615
<           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(5 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect|BUF_SourceCopy, ERHIAccess::IndirectArgs|ERHIAccess::CopySrc, CreateInfo);

---
>           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(10 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect|BUF_SourceCopy, ERHIAccess::IndirectArgs|ERHIAccess::CopySrc, CreateInfo);

1103c1618,1626
< #pragma region S1_Engine_Shiyu

---
> #pragma region S1_Engine_Shiyu

>       {

>           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.HoleInstanceBuffer"));

>           const int32 InstanceSize = sizeof(VirtualHeightfieldMesh::QuadRenderInstance);

>           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnRenderThread() * InstanceSize / 4; // hold instance just little

>           InBuffers.HoleInstanceBuffer = RHICmdList.CreateStructuredBuffer(InstanceSize, InstanceBufferSize, BUF_UnorderedAccess|BUF_ShaderResource, ERHIAccess::SRVMask, CreateInfo);

>           InBuffers.HoleInstanceBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.HoleInstanceBuffer, false, false);

>           InBuffers.HoleInstanceBufferSRV = RHICmdList.CreateShaderResourceView(InBuffers.HoleInstanceBuffer);

>       }

1137c1660
<       OutResources.StatBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateStructuredDesc(sizeof(uint32), 32),

---
>       OutResources.StatBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),

1143a1667,1722
>   namespace V2

>   {

>

>

>       void InitializeVolatileBuffers(FRDGBuilder& GraphBuilder, FVolatileBuffers& OutResources)

>       {

>           const int32 MaxRenderItems = CVarVHMMaxRenderItems.GetValueOnRenderThread();

>           const int32 MaxFeedbackItems = CVarVHMMaxFeedbackItems.GetValueOnRenderThread();

>           const TCHAR* MergeNames[2] = {

>               TEXT("VHM.MergeBuffer_0"),

>               TEXT("VHM.MergeBuffer_1")};

>           const TCHAR* MergeArgsNames[2] = {

>               TEXT("VHM.MergeArgsBuffer_0"),

>               TEXT("VHM.MergeArgsBuffer_1")};

>           const TCHAR* SubdivideNames[2] = {

>               TEXT("VHM.SubdivideBuffer_0"),

>               TEXT("VHM.SubdivideBuffer_1")};

>           const TCHAR* SubdivideArgsNames[2] = {

>               TEXT("VHM.SubdivideArgsBuffer_0"),

>               TEXT("VHM.SubdivideArgsBuffer_1")};

>           for(int i = 0; i < 2; ++i)

>           {

>               OutResources.MergeQuadBuffer[i] = GraphBuilder.CreateBuffer(

>                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),

>                   MergeNames[i]);

>               OutResources.MergeViews[i].Create(GraphBuilder, OutResources.MergeQuadBuffer[i]);

>

>               OutResources.SubdivideQuadBuffer[i] = GraphBuilder.CreateBuffer(

>                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),

>                   SubdivideNames[i]);

>               OutResources.SubdivideViews[i].Create(GraphBuilder, OutResources.SubdivideQuadBuffer[i]);

>

>               OutResources.ArgsBuffer[i] = GraphBuilder.CreateBuffer(

>                   FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize),

>                   SubdivideArgsNames[i]);

>               OutResources.ArgsViews[i].Create(GraphBuilder, OutResources.ArgsBuffer[i]);

>

>           }

>

>           FRDGBufferDesc FeedbackBufferDesc = FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxFeedbackItems + 1);

>           FeedbackBufferDesc.Usage = EBufferUsageFlags(FeedbackBufferDesc.Usage | BUF_SourceCopy);

>           OutResources.FeedbackBuffer = GraphBuilder.CreateBuffer(FeedbackBufferDesc, TEXT("VHM.FeedbackBuffer"));

>           OutResources.FeedbackBufferUAV = GraphBuilder.CreateUAV(OutResources.FeedbackBuffer, PF_R32_UINT);

>

>           // uniform

>           OutResources.VHMParameterUBuffer = GraphBuilder.CreateUniformBuffer<FVHMCSSharedParameters>(OutResources.VHMParameter);

>

> #if VHM_ENABLE_STAT

>           OutResources.StatBuffer = GraphBuilder.CreateBuffer(

>               FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),

>               TEXT("VirtualHeightfieldMesh.StatBuffer"));

>           OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));

> #endif

>       }

>   }

>

1148c1727
<                               FVolatileResources& InVolatileResources)

---
>                               FRDGBufferRef StatBuffer)

1153c1732
<       AddEnqueueCopyPass(GraphBuilder, GPUBufferReadBack, InVolatileResources.StatBuffer, sizeof(int32) * 32);

---
>       AddEnqueueCopyPass(GraphBuilder, GPUBufferReadBack, StatBuffer, sizeof(int32) * MaxStatCount);

1157c1736
<

---
>

1175a1755,1760
>

> #pragma region S1_Engine_Shiyu

>           FRHIUnorderedAccessView* HoleInstanceBufferUAV = Buffers[BufferIndex].HoleInstanceBufferUAV;

>

>           TransitionInfos.Add(FRHITransitionInfo(HoleInstanceBufferUAV, bToWrite ? ERHIAccess::SRVMask : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::SRVMask));

> #pragma endregion

1205d1789
<       PassParameters->RWQuadBuffer = InVolatileResources.QuadBufferUAV;

1222c1806
<

---
>

1240a1825
>       PassParameters->RVTMinLevel = InDesc.RVTMinLevel;

1258c1843
< #pragma region S1_Engine_Shiyu

---
> #pragma region S1_Engine_Shiyu

1271c1856
<   void AddPass_InitInstanceBuffer(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, int32 NumQuadsPerTileSide, FDrawInstanceBuffers& InOutputResources)

---
>   void AddPass_InitInstanceBuffer(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, int32 NumInstanceVertexSide, FDrawInstanceBuffers& InOutputResources)

1276c1861
<       PassParameters->NumIndices = NumQuadsPerTileSide * NumQuadsPerTileSide * 6;

---
>       PassParameters->NumIndices = NumInstanceVertexSide * NumInstanceVertexSide * 6;

1291a1877
>       PassParameters->RVTMinLevel = InDesc.RVTMinLevel;

1305c1891,1895
< #pragma region S1_Engine_Shiyu

---
>

> #pragma region S1_Engine_Shiyu

>       PassParameters->MaskTexture = InDesc.MaskTexture;

>       PassParameters->RWHoleInstanceBuffer = InOutputResources.HoleInstanceBufferUAV;

>

1323a1914,2192
>

>   namespace V2

>   {

>

> //        void AddPass_FirstInitBuffers(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, FDrawInstanceBuffers& DrawBuffers,

> //            FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers)

> //        {

> //            auto Parameters = GraphBuilder.AllocParameters<FFirstInitBuffers_CS::FParameters>();

> //

> //            Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;

> //            Parameters->FinalArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, 0);

> //            Parameters->FinalQuadBuffer = InBuffers.GetFinalQuadUAV(GraphBuilder, 0);

> //            Parameters->InstanceArgsBuffer = DrawBuffers.IndirectArgsBufferUAV;

> //

> //            TShaderMapRef<FFirstInitBuffers_CS> ComputeShader(InGlobalShaderMap);

> //

> //            FComputeShaderUtils::AddPass(

> //                GraphBuilder,

> //                RDG_EVENT_NAME("FirstInitBuffers"),

> //                ComputeShader, Parameters, FIntVector3(1, 1, 1)

> //            );

> //        }

>

>       void AddPass_InitAllBuffers(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap,

>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, FDrawInstanceBuffers& InDrawBuffers)

>       {

>           auto Parameters = GraphBuilder.AllocParameters<FInitAllBuffers_CS::FParameters>();

>           Parameters->VHMParam =InVolatileBuffers.VHMParameterUBuffer;

>           Parameters->RWFeedbackBuffer = GraphBuilder.CreateUAV(InVolatileBuffers.FeedbackBuffer, PF_R32_UINT);

>           Parameters->FinalArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, (InBuffers.CalTime + 1) % 2);

>           Parameters->DispatchArgsBuffer1 = InVolatileBuffers.ArgsViews[0].UAV;

>           Parameters->DispatchArgsBuffer2 = InVolatileBuffers.ArgsViews[1].UAV;

>           Parameters->InstanceArgsBuffer = InDrawBuffers.IndirectArgsBufferUAV;

> #if VHM_ENABLE_STAT

>           Parameters->RWStatBuffer = InVolatileBuffers.StatBufferUAV;

> #endif

>           AddClearUAVPass(GraphBuilder, InVolatileBuffers.FeedbackBufferUAV, 0xffffffff);

>

>           TShaderMapRef<FInitAllBuffers_CS> ComputeShader(InGlobalShaderMap);

>

>           FComputeShaderUtils::AddPass(

>               GraphBuilder,

>               RDG_EVENT_NAME("InitAllBuffers"),

>               ComputeShader, Parameters, FIntVector3(1, 1, 1)

>           );

>       }

>

>       void AddPass_FillLevel4Quad_CS(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap,

>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers)

>       {

>           auto Parameters = GraphBuilder.AllocParameters<FFillLevel4Quad_CS::FParameters>();

>           Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;

>           Parameters->OutDispatchArgsBuffer   = InVolatileBuffers.ArgsViews[0].UAV;

>           Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[0].UAV;

>           Parameters->PageTableTexture = InVolatileBuffers.PageTableTexture;

>

>           TShaderMapRef<FFillLevel4Quad_CS> ComputeShader(InGlobalShaderMap);

>

>           FComputeShaderUtils::AddPass(

>               GraphBuilder,

>               RDG_EVENT_NAME("FillLevelQuads"),

>               ComputeShader, Parameters, FIntVector3(1, 1, 1)

>           );

>       }

>

>       // void AddPass_CollectQuadsFromPreFrame_CS(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap,

>       //  FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers)

>       // {

>       //  auto Parameters = GraphBuilder.AllocParameters<FCollectQuadsFromPreFrame_CS::FParameters>();

>       //

>       //  Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;

>       //  FRDGBufferRef InDispatchArgsBuffer = GraphBuilder.RegisterExternalBuffer(InBuffers.FinalQuadArgsBuffer[InBuffers.CalTime]);

>       //  Parameters->InDispatchArgsBufferAccess = InDispatchArgsBuffer;

>       //  Parameters->InDispatchArgsBuffer = GraphBuilder.CreateSRV(InDispatchArgsBuffer);

>       //  // Parameters->InDispatchArgsBufferAccess = InVolatileBuffers.FinalArgsBuffer;

>       //  // Parameters->InDispatchArgsBuffer = InVolatileBuffers.FinalArgsViews.SRV;

>       //  // Parameters->InQuadBuffer = InBuffers.FinalQuadBufferSRV[InBuffers.CalTime];

>       //  Parameters->InQuadBuffer = InBuffers.GetFinalQuadSRV(GraphBuilder, InBuffers.CalTime);

>       //

>       //  Parameters->OutDispatchArgsBuffer   = InVolatileBuffers.ArgsViews[0].UAV;

>       //  Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[0].UAV;

>       //  Parameters->OutMergeQuadBuffer = InVolatileBuffers.MergeViews[0].UAV;

>       //  Parameters->RWFeedbackBuffer = InVolatileBuffers.FeedbackBufferUAV;

>       //

>       //  // Parameters->FinalDispatchArgsBuffer = InBuffers.FinalQuadArgsBufferUAV[(InBuffers.CalTime + 1) % 2];

>       //  // Parameters->FinalQuadBuffer = InBuffers.FinalQuadBufferUAV[(InBuffers.CalTime + 1) % 2];

>       //  uint32 NextCalTime = (InBuffers.CalTime + 1) % 2;

>       //  Parameters->FinalDispatchArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, NextCalTime);

>       //  Parameters->FinalQuadBuffer = InBuffers.GetFinalQuadUAV(GraphBuilder, NextCalTime);

>       //

>       //  Parameters->PageTableTexture = InVolatileBuffers.PageTableTexture;

>       //  Parameters->HeightTexture = InVolatileBuffers.HeightTexture;

>       //  Parameters->MaskTexture = InVolatileBuffers.MaskTexture;

>       //  Parameters->PointSampler = TStaticSamplerState<SF_Point>::GetRHI();

>       //

>       //  TShaderMapRef<FCollectQuadsFromPreFrame_CS> ComputeShader(InGlobalShaderMap);

>       //

>       //  FComputeShaderUtils::AddPass(

>       //      GraphBuilder,

>       //      RDG_EVENT_NAME("CollectQuadsFromPreFrameBuffers"),

>       //      ComputeShader, Parameters, InDispatchArgsBuffer, 0

>       //  );

>       // }

>

>       void AddPass_CollectSubdivideQuads_CS(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap,

>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, uint32 CalTime, bool WithCull, bool WithFeedback=true)

>       {

>           uint32 PreVolBufIdx = CalTime % 2;

>           uint32 VolBufIdx = (CalTime + 1) % 2;

>           auto Parameters = GraphBuilder.AllocParameters<FCollectSubdivideQuads_CS::FParameters>();

>

>           Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;

>           Parameters->CurPassCalTime = CalTime;

>           Parameters->InQuadArgsBuffer = InVolatileBuffers.ArgsBuffer[PreVolBufIdx];

>           Parameters->InDispatchArgsBuffer = InVolatileBuffers.ArgsViews[PreVolBufIdx].SRV;

>           Parameters->InQuadBuffer = InVolatileBuffers.SubdivideViews[PreVolBufIdx].SRV;

>

>           Parameters->OutDispatchArgsBuffer = InVolatileBuffers.ArgsViews[VolBufIdx].UAV;

>           Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[VolBufIdx].UAV;

>           Parameters->RWFeedbackBuffer = InVolatileBuffers.FeedbackBufferUAV;

>

>           uint32 NextCalTime = (InBuffers.CalTime + 1) % 2;

>           Parameters->FinalDispatchArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, NextCalTime);

>           Parameters->FinalQuadBuffer = InBuffers.GetFinalQuadUAV(GraphBuilder, NextCalTime);

>

>           Parameters->PageTableTexture = InVolatileBuffers.PageTableTexture;

>           Parameters->HeightTexture = InVolatileBuffers.HeightTexture;

>           Parameters->MaskTexture = InVolatileBuffers.MaskTexture;

>           Parameters->PointSampler = TStaticSamplerState<SF_Point>::GetRHI();

>           Parameters->HeightMinMaxTexture = InVolatileBuffers.HeightMinMaxTexture;

>

>

>           FCollectSubdivideQuads_CS::FPermutationDomain PermutationVector;

>           PermutationVector.Set<FCollectSubdivideQuads_CS::FWithCullDim>(WithCull);

>           PermutationVector.Set<FCollectSubdivideQuads_CS::FWithFeedback>(WithFeedback);

>

>           TShaderMapRef<FCollectSubdivideQuads_CS> ComputeShader(InGlobalShaderMap, PermutationVector);

>

>           FComputeShaderUtils::AddPass(

>               Grap

... [diff truncated to 80KB; full diff in vhm_diffs/31598.diff] ...
```

#### CL 33256 — 2024/05/21 — 郭智均

- **提交说明**：--story=1016921 --user=郭智均 VHM合并到trunk，并适配白垩的高精度要求 https://www.tapd.cn/68880148/s/1260281
- **TAPD**：story=1016921
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：VHM合并到trunk，并适配白垩的高精度要求 https://www.tapd.cn/68880148/s/1260281

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (edit)

📄 查看 VHM 相关 diff（CL 33256）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#6 (unicode) ====

125c125
<           TileRenderTarget.SetNum(3);

---
>           TileRenderTarget.SetNum(4);

142a143
>           GRenderTargetPool.FindFreeElement(RHICmdList, TileRenderTargetDesc, TileRenderTarget[3], TEXT("TileTarget4"));

169a171
>           TileRenderTarget[3].SafeRelease();

182c184
<       TRefCountPtr<IPooledRenderTarget> GetTileRenderTarget(int32 Index) const { check(Index < 3); return TileRenderTarget[Index]; }

---
>       TRefCountPtr<IPooledRenderTarget> GetTileRenderTarget(int32 Index) const { check(Index < 4); return TileRenderTarget[Index]; }

194c196
<       TArray<TRefCountPtr<IPooledRenderTarget>, TFixedAllocator<3>> TileRenderTarget;

---
>       TArray<TRefCountPtr<IPooledRenderTarget>, TFixedAllocator<4>> TileRenderTarget;

517c519
<                       Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;

---
>                       Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Custom_SecondColor_YCoCg;

525a528
>                       Desc.Targets[3].Texture = RenderTileResources.GetTileRenderTarget(3)->GetRHI();
```

#### CL 33893 — 2024/05/22 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1265464：Disable VHM in Editor
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1265464：Disable VHM in Editor

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 33893）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#5 (unicode) ====

12a13
> #if UE_EDITOR

15a17,24
>       0,  // shiyu: now we need to open it by console

>       TEXT("Enable virtual heightfield mesh"),

>       ECVF_RenderThreadSafe

>   );

> #else

>   /** CVar to toggle support for virtual heightfield mesh. */

>   static TAutoConsoleVariable<int32> CVarVHMEnable(

>       TEXT("r.VHM.Enable"),

19a29,30
> #endif

>
```

#### CL 35131 — 2024/05/24 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1272159：Disable VHM
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1272159：Disable VHM

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 35131）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#6 (unicode) ====

13,15c13,31
< #if UE_EDITOR

<   /** CVar to toggle support for virtual heightfield mesh. */

<   static TAutoConsoleVariable<int32> CVarVHMEnable(

---
> // #if UE_EDITOR

> //    /** CVar to toggle support for virtual heightfield mesh. */

> //    static TAutoConsoleVariable<int32> CVarVHMEnable(

> //        TEXT("r.VHM.Enable"),

> //        0,  // shiyu: now we need to open it by console

> //        TEXT("Enable virtual heightfield mesh"),

> //        ECVF_RenderThreadSafe

> //    );

> // #else

> //    /** CVar to toggle support for virtual heightfield mesh. */

> //    static TAutoConsoleVariable<int32> CVarVHMEnable(

> //        TEXT("r.VHM.Enable"),

> //        1,  // shiyu: now we need to open it by console

> //        TEXT("Enable virtual heightfield mesh"),

> //        ECVF_RenderThreadSafe

> //    );

> // #endif

>

> static TAutoConsoleVariable<int32> CVarVHMEnable(

21,29d36
< #else

<   /** CVar to toggle support for virtual heightfield mesh. */

<   static TAutoConsoleVariable<int32> CVarVHMEnable(

<       TEXT("r.VHM.Enable"),

<       1,  // shiyu: now we need to open it by console

<       TEXT("Enable virtual heightfield mesh"),

<       ECVF_RenderThreadSafe

<   );

< #endif
```

#### CL 35345 — 2024/05/24 — 刘双

- **提交说明**：--story=1017591 --user=刘双 内存扩展：UE 中利用 IOS 新的内存特性 https://www.tapd.cn/68880148/s/1276994
- **TAPD**：story=1017591
- **涉及 VHM 文件**：3 个

**做了什么**：

提交目的：内存扩展：UE 中利用 IOS 新的内存特性 https://www.tapd.cn/68880148/s/1276994

- **其它**：3 个文件
- `Binaries/Win64/UnrealEditor-VirtualHeightfieldMesh.dll` (delete)
- `Binaries/Win64/UnrealEditor-VirtualHeightfieldMeshEditor.dll` (delete)
- `Binaries/Win64/UnrealEditor.modules` (delete)

📄 查看 VHM 相关 diff（CL 35345）

```
(no diff for VHM files)
```

#### CL 36363 — 2024/05/27 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1283459：WE Demo（仅用于视频录制）
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1283459：WE Demo（仅用于视频录制）

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 36363）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#7 (unicode) ====

33c33
<       0,  // shiyu: now we need to open it by console

---
>       1,  // shiyu: now we need to open it by console
```

#### CL 36368 — 2024/05/27 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1283459：Undo //GR/trunk/... changelist 36363
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1283459：Undo //GR/trunk/... changelist 36363

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 36368）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#8 (unicode) ====

33c33
<       1,  // shiyu: now we need to open it by console

---
>       0,  // shiyu: now we need to open it by console
```

#### CL 38190 — 2024/05/29 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1296494：白厄视频录制：高精度VHM+临时开启Mask材质花的Nanite支持
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1296494：白厄视频录制：高精度VHM+临时开启Mask材质花的Nanite支持

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 38190）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#9 (unicode) ====

33c33
<       0,  // shiyu: now we need to open it by console

---
>       1,  // shiyu: now we need to open it by console
```

#### CL 40492 — 2024/06/03 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1309770：VHM disable in Editor
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1309770：VHM disable in Editor

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 40492）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#10 (unicode) ====

13,15c13,31
< // #if UE_EDITOR

< //    /** CVar to toggle support for virtual heightfield mesh. */

< //    static TAutoConsoleVariable<int32> CVarVHMEnable(

---
> #if UE_EDITOR

>   /** CVar to toggle support for virtual heightfield mesh. */

>   static TAutoConsoleVariable<int32> CVarVHMEnable(

>       TEXT("r.VHM.Enable"),

>       0,  // shiyu: now we need to open it by console

>       TEXT("Enable virtual heightfield mesh"),

>       ECVF_RenderThreadSafe

>   );

> #else

>   /** CVar to toggle support for virtual heightfield mesh. */

>   static TAutoConsoleVariable<int32> CVarVHMEnable(

>       TEXT("r.VHM.Enable"),

>       1,  // shiyu: now we need to open it by console

>       TEXT("Enable virtual heightfield mesh"),

>       ECVF_RenderThreadSafe

>   );

> #endif

>

> // static TAutoConsoleVariable<int32> CVarVHMEnable(

17,24d32
< //        0,  // shiyu: now we need to open it by console

< //        TEXT("Enable virtual heightfield mesh"),

< //        ECVF_RenderThreadSafe

< //    );

< // #else

< //    /** CVar to toggle support for virtual heightfield mesh. */

< //    static TAutoConsoleVariable<int32> CVarVHMEnable(

< //        TEXT("r.VHM.Enable"),

29,36d36
< // #endif

<

< static TAutoConsoleVariable<int32> CVarVHMEnable(

<       TEXT("r.VHM.Enable"),

<       1,  // shiyu: now we need to open it by console

<       TEXT("Enable virtual heightfield mesh"),

<       ECVF_RenderThreadSafe

<   );
```

#### CL 42957 — 2024/06/06 — 谢朋志

- **提交说明**：--story=1016970 --user=谢朋志 通用提交单 https://www.tapd.cn/68880148/s/1330836 : Undo //GR/trunk/... changelist 38190
- **TAPD**：story=1016970
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：通用提交单 https://www.tapd.cn/68880148/s/1330836 : Undo //GR/trunk/... changelist 38190

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 42957）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#11 (unicode) ====

1,92c1,86
< // Copyright Epic Games, Inc. All Rights Reserved.

<

< #include "VirtualHeightfieldMeshEnable.h"

<

< #include "Components/RuntimeVirtualTextureComponent.h"

< #include "UObject/UObjectIterator.h"

< #include "VirtualHeightfieldMeshComponent.h"

< #include "VT/RuntimeVirtualTextureVolume.h"

< #include "RenderUtils.h"

<

< namespace VirtualHeightfieldMesh

< {

< #if UE_EDITOR

<   /** CVar to toggle support for virtual heightfield mesh. */

<   static TAutoConsoleVariable<int32> CVarVHMEnable(

<       TEXT("r.VHM.Enable"),

<       0,  // shiyu: now we need to open it by console

<       TEXT("Enable virtual heightfield mesh"),

<       ECVF_RenderThreadSafe

<   );

< #else

<   /** CVar to toggle support for virtual heightfield mesh. */

<   static TAutoConsoleVariable<int32> CVarVHMEnable(

<       TEXT("r.VHM.Enable"),

<       1,  // shiyu: now we need to open it by console

<       TEXT("Enable virtual heightfield mesh"),

<       ECVF_RenderThreadSafe

<   );

< #endif

<

< // static TAutoConsoleVariable<int32> CVarVHMEnable(

< //        TEXT("r.VHM.Enable"),

< //        1,  // shiyu: now we need to open it by console

< //        TEXT("Enable virtual heightfield mesh"),

< //        ECVF_RenderThreadSafe

< //    );

<

<

<   /** Sink to apply updates when virtual heightfield mesh settings change. */

<   static void OnUpdate()

<   {

<       const bool bEnable = CVarVHMEnable.GetValueOnGameThread() != 0;

<

<       static bool bLastEnable = !bEnable;

<

<       if (bEnable != bLastEnable)

<       {

<           bLastEnable = bEnable;

<

<           TArray<URuntimeVirtualTexture*> RuntimeVirtualTextures;

<

<           for (TObjectIterator<UVirtualHeightfieldMeshComponent> It; It; ++It)

<           {

<               It->MarkRenderStateDirty();

<

<               ARuntimeVirtualTextureVolume* VirtualTextureVolume = It->GetVirtualTextureVolume();

<               URuntimeVirtualTextureComponent* VirtualTextureComponent = VirtualTextureVolume != nullptr ? ToRawPtr(VirtualTextureVolume->VirtualTextureComponent) : nullptr;

<               URuntimeVirtualTexture* VirtualTexture = VirtualTextureComponent != nullptr ? VirtualTextureComponent->GetVirtualTexture() : nullptr;

<

<               if (VirtualTextureComponent != nullptr)

<               {

<                   VirtualTextureComponent->MarkRenderStateDirty();

<               }

<               if (VirtualTexture != nullptr)

<               {

<                   RuntimeVirtualTextures.AddUnique(It->GetVirtualTexture());

<               }

<           }

<

<           for (TObjectIterator<UPrimitiveComponent> It; It; ++It)

<           {

<               for (URuntimeVirtualTexture* RuntimeVirtualTexture : RuntimeVirtualTextures)

<               {

<                   if (It->GetRuntimeVirtualTextures().Contains(RuntimeVirtualTexture))

<                   {

<                       It->MarkRenderStateDirty();

<                       break;

<                   }

<               }

<           }

<       }

<   }

<

<   FAutoConsoleVariableSink GConsoleVariableSink(FConsoleCommandDelegate::CreateStatic(&OnUpdate));

<

<   bool IsEnabled(FStaticFeatureLevel InFeatureLevel)

<   {

<       return CVarVHMEnable.GetValueOnAnyThread() != 0

<           && (InFeatureLevel >= ERHIFeatureLevel::SM5 || InFeatureLevel == ERHIFeatureLevel::ES3_1)

<           && UseVirtualTexturing(InFeatureLevel);

<   }

< }

---
> // Copyright Epic Games, Inc. All Rights Reserved.
>
> #include "VirtualHeightfieldMeshEnable.h"
>
> #include "Components/RuntimeVirtualTextureComponent.h"
> #include "UObject/UObjectIterator.h"
> #include "VirtualHeightfieldMeshComponent.h"
> #include "VT/RuntimeVirtualTextureVolume.h"
> #include "RenderUtils.h"
>
> namespace VirtualHeightfieldMesh
> {
> #if UE_EDITOR
>   /** CVar to toggle support for virtual heightfield mesh. */
>   static TAutoConsoleVariable<int32> CVarVHMEnable(
>       TEXT("r.VHM.Enable"),
>       0,  // shiyu: now we need to open it by console
>       TEXT("Enable virtual heightfield mesh"),
>       ECVF_RenderThreadSafe
>   );
> #else
>   /** CVar to toggle support for virtual heightfield mesh. */
>   static TAutoConsoleVariable<int32> CVarVHMEnable(
>       TEXT("r.VHM.Enable"),
>       1,  // shiyu: now we need to open it by console
>       TEXT("Enable virtual heightfield mesh"),
>       ECVF_RenderThreadSafe
>   );
> #endif
>
>
>
>   /** Sink to apply updates when virtual heightfield mesh settings change. */
>   static void OnUpdate()
>   {
>       const bool bEnable = CVarVHMEnable.GetValueOnGameThread() != 0;
>
>       static bool bLastEnable = !bEnable;
>
>       if (bEnable != bLastEnable)
>       {
>           bLastEnable = bEnable;
>
>           TArray<URuntimeVirtualTexture*> RuntimeVirtualTextures;
>
>           for (TObjectIterator<UVirtualHeightfieldMeshComponent> It; It; ++It)
>           {
>               It->MarkRenderStateDirty();
>
>               ARuntimeVirtualTextureVolume* VirtualTextureVolume = It->GetVirtualTextureVolume();
>               URuntimeVirtualTextureComponent* VirtualTextureComponent = VirtualTextureVolume != nullptr ? ToRawPtr(VirtualTextureVolume->VirtualTextureComponent) : nullptr;
>               URuntimeVirtualTexture* VirtualTexture = VirtualTextureComponent != nullptr ? VirtualTextureComponent->GetVirtualTexture() : nullptr;
>
>               if (VirtualTextureComponent != nullptr)
>               {
>                   VirtualTextureComponent->MarkRenderStateDirty();
>               }
>               if (VirtualTexture != nullptr)
>               {
>                   RuntimeVirtualTextures.AddUnique(It->GetVirtualTexture());
>               }
>           }
>
>           for (TObjectIterator<UPrimitiveComponent> It; It; ++It)
>           {
>               for (URuntimeVirtualTexture* RuntimeVirtualTexture : RuntimeVirtualTextures)
>               {
>                   if (It->GetRuntimeVirtualTextures().Contains(RuntimeVirtualTexture))
>                   {
>                       It->MarkRenderStateDirty();
>                       break;
>                   }
>               }
>           }
>       }
>   }
>
>   FAutoConsoleVariableSink GConsoleVariableSink(FConsoleCommandDelegate::CreateStatic(&OnUpdate));
>
>   bool IsEnabled(FStaticFeatureLevel InFeatureLevel)
>   {
>       return CVarVHMEnable.GetValueOnAnyThread() != 0
>           && (InFeatureLevel >= ERHIFeatureLevel::SM5 || InFeatureLevel == ERHIFeatureLevel::ES3_1)
>           && UseVirtualTexturing(InFeatureLevel);
>   }
> }
```

#### CL 49953 — 2024/06/18 — 郭智均

- **提交说明**：--story=1020482 --user=郭智均 提审代码合并到trunk https://www.tapd.cn/68880148/s/1364145
- **TAPD**：story=1020482
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：提审代码合并到trunk https://www.tapd.cn/68880148/s/1364145

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 49953）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#5 (unicode) ====
```

#### CL 49968 — 2024/06/18 — 郭智均

- **提交说明**：--story=1020482 --user=郭智均 提审代码合并到trunk https://www.tapd.cn/68880148/s/1364145
- **TAPD**：story=1020482
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：提审代码合并到trunk https://www.tapd.cn/68880148/s/1364145

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (integrate)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 49968）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#2 (text) ====

179a180
>   const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.

181c182
<   if (CurThreadID == 0) { InterlockedAdd(RWFeedbackBuffer[0], NumActiveGroupThread, FeedbackBeginOffset); }

---
>   if (CurThreadID == 0) { InterlockedAdd(RWFeedbackBuffer[0], NumActiveGroupThread * MultiWriteCount, FeedbackBeginOffset); }

186c187
<       uint FeedbackPos = FeedbackBeginOffset + CurThreadID + 1;

---
>       uint FeedbackPos = FeedbackBeginOffset + (CurThreadID + 1) * MultiWriteCount;

189c190,194
<       RWFeedbackBuffer[FeedbackPos] = ThisInfo.TexPos.x | (ThisInfo.TexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;

---
>       uint PackData = ThisInfo.TexPos.x | (ThisInfo.TexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;

>       for (int i = 0; i < MultiWriteCount; ++i)

>       {

>           RWFeedbackBuffer[FeedbackPos + i] = PackData;

>       }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#6 (unicode) ====

74c74
<   1024 * 4,

---
>   1024 * 4 * 10, // pre node write 10 time

136c136
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

139c139
< #endif

---
> //#endif

141a142
>

162c163
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

166c167
< #endif

---
> //#endif

185c186
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

187c188
< #endif

---
> //#endif

386c387
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

389c390
< #endif

---
> //#endif

1097,1098c1098
< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

---
> #pragma region S1_Engine_Shiyu

1100d1099
< #endif

1145d1143
< #if VHM_ENABLE_STAT

1147d1144
< #endif

1212d1208
< #if VHM_ENABLE_STAT

1214d1209
< #endif

1262d1256
< #if VHM_ENABLE_STAT

1264d1257
< #endif

1420d1412
< #if VHM_ENABLE_STAT

1422d1413
< #endif

1595c1586
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1598c1589
< #endif

---
> //#endif

1627c1618
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1633c1624
< #endif

---
> //#endif

1659c1650
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1663c1654
< #endif

---
> //#endif

1714c1705
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1719c1710
< #endif

---
> //#endif

1793c1784
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1795c1786
< #endif

---
> //#endif

1844c1835
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1846c1837
< #endif

---
> //#endif

1896c1887
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1898c1889
< #endif

---
> //#endif

1947c1938
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

1949c1940
< #endif

---
> //#endif

2176c2167
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

2178c2169
< #endif

---
> //#endif

2478c2469
< #if VHM_ENABLE_STAT

---
> //#if VHM_ENABLE_STAT

2480c2471
< #endif

---
> //#endif
```

#### CL 52549 — 2024/06/21 — 郭智均

- **提交说明**：--story=1019849 --user=郭智均 VHM提供更多更细粒度的调整网格精度的配置 https://www.tapd.cn/68880148/s/1369805
- **TAPD**：story=1019849
- **涉及 VHM 文件**：5 个

**做了什么**：

提交目的：VHM提供更多更细粒度的调整网格精度的配置 https://www.tapd.cn/68880148/s/1369805

- **Shader**：2 个文件
- `Shaders/Private/VirtualHeightfieldMesh.ush` (edit)
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- **Runtime C++**：3 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (edit)
- `Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h` (edit)

📄 查看 VHM 相关 diff（CL 52549）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh.ush#3 (text) ====

110c110
<   return uint2(Item.PosLevelPacked & 0x00000FFF, (Item.PosLevelPacked & 0x00FFF000) >> 12);

---
>   return uint2(Item.PosLevelPacked & 0x00003FFF, (Item.PosLevelPacked & 0x0FFFC000) >> 14);

115c115
<   return Item.PosLevelPacked >> 24;

---
>   return Item.PosLevelPacked >> 28;

118,126d117
< uint UnpackLevel2(QuadRenderInstance Item)

< {

<   return (Item.PosLevelPacked >> 24) & 0x7f;

< }

<

< uint UnpackMask2(QuadRenderInstance Item)

< {

<   return Item.PosLevelPacked >> 31;

< }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#3 (text) ====

51,52c51,52
<   Item.Pos = MortonDecode(PackedVal.x & 0xffffff);

<   Item.Level = PackedVal.x >> 24;

---
>   Item.Pos = MortonDecode(PackedVal.x & 0xfffffff);

>   Item.Level = PackedVal.x >> 28;

59c59
<   return MortonEncode(Pos) | (Level << 24);

---
>   return MortonEncode(Pos) | (Level << 28);

66,67c66
<   Result.x = MortonEncode(Info.Pos);

<   Result.x |= (Info.Level << 24);

---
>   Result.x = PackQuadPosLevel(Info.Pos, Info.Level);

762c761
<   Instance.PosLevelPacked = ThisInfo.Pos.x | (ThisInfo.Pos.y << 12) | (ThisInfo.Level << 24);

---
>   Instance.PosLevelPacked = ThisInfo.Pos.x | (ThisInfo.Pos.y << 14) | (ThisInfo.Level << 28);

862c861
<   ThisPackData.x = ThisThreadID | (Level << 24);

---
>   ThisPackData.x = ThisThreadID | (Level << 28);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#7 (unicode) ====

318c318
<       const uint32 MaxLevel = InProxy->AllocatedVirtualTexture->GetMaxLevel();

---
>       const uint32 MaxLevel = FMath::Max(InProxy->RVTMaxLevel - InProxy->Lod0LevelBias, 0);

645c645
<   , NumQuadsPerTileOfTwo(4) // (1 << 4) * (1 << 4)

---
>   , NumQuadsPerTileOfTwo(InComponent->GetNumQuadPerTileOfTwo()) // (1 << 4) * (1 << 4)

652a653
>   , ExtSubdivisionLevel(InComponent->GetExtSubdivisionLevel())

653a655,656
>   , RVTMaxLevel(0)

>   , Lod0LevelBias(InComponent->GetLod0LevelBias())

739,740c742,745
<               check(TileSize >= NumQuadsPerTileOfTwo);

<               NumInstanceVertexSide = 1 << (TileSize - NumQuadsPerTileOfTwo);

---
>               // check(TileSize + ExtSubdivisionLevel >= NumQuadsPerTileOfTwo);

>               NumQuadsPerTileOfTwo = FMath::Min(NumQuadsPerTileOfTwo, TileSize + ExtSubdivisionLevel - 1);

>               NumInstanceVertexSide = 1 << (TileSize + ExtSubdivisionLevel - NumQuadsPerTileOfTwo);

>               RVTMaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;

750,752c755
<                   uint32 TileSizeLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileSize());

<                   check(TileSizeLevel >= NumQuadsPerTileOfTwo);

<                   UniformParams.MaxLod = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;

---
>                   UniformParams.MaxLod = RVTMaxLevel;

2239c2242
<       check(TileCountSizeLevel >= Proxy->NumQuadsPerTileOfTwo);

---
>       check(TileCountSizeLevel + Proxy->ExtSubdivisionLevel >= Proxy->NumQuadsPerTileOfTwo);

2448c2451
<       check(TileCountSizeLevel >= Proxy->NumQuadsPerTileOfTwo);

---
>       check(TileCountSizeLevel + Proxy->ExtSubdivisionLevel >= Proxy->NumQuadsPerTileOfTwo);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h#4 (unicode) ====

71a72,77
>

> #pragma region S1_Engine_Shiyu

>   int32 ExtSubdivisionLevel;

>   int32 RVTMaxLevel;

>   int32 Lod0LevelBias;

> #pragma endregion


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h#4 (unicode) ====

77a78,82
> #pragma region shiyu

>   UPROPERTY(EditAnywhere, Category = Rendering, meta = (DisplayName = "LOD 0 Level Bias", ClampMin = "0", UIMin = "8"))

>   int32 Lod0LevelBias = 4;

> #pragma endregion

>

101a107,114
> #pragma region shiyu

>   UPROPERTY(EditAnywhere, Category = Rendering, meta = (DisplayName = "Number Quad Per Tile Of Two", ClampMin = "0", ClampMax = "7"))

>   int32 NumQuadPerTileOfTwo = 4;

>

>   UPROPERTY(EditAnywhere, Category = Rendering, meta = (DisplayName = "External Subdivision Level", ClampMin = "0", ClampMax = "3"))

>   int32 ExtSubdivisionLevel = 0;

> #pragma endregion

>

146c159,163
<

---
> #pragma region shiyu

>   int32 GetLod0LevelBias() const { return Lod0LevelBias; }

>   int32 GetNumQuadPerTileOfTwo() const { return NumQuadPerTileOfTwo; }

>   int32 GetExtSubdivisionLevel() const { return ExtSubdivisionLevel; }

> #pragma endregion
```

#### CL 54652 — 2024/06/24 — 郭智均

- **提交说明**：--bug=1021577 --user=郭智均 允许VHM在Game下渲染网格 https://www.tapd.cn/68880148/s/1376722
- **TAPD**：bug=1021577
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：允许VHM在Game下渲染网格 https://www.tapd.cn/68880148/s/1376722

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 54652）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#8 (unicode) ====

864c864,865
<               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

---
>               // Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

>               Mesh.bWireframe = ViewFamily.EngineShowFlags.Wireframe;
```

#### CL 59490 — 2024/06/27 — 郭智均

- **提交说明**：--bug=1023220 --user=郭智均 【构建报错】 UnityBuild构建失败 https://www.tapd.cn/68880148/s/1392491
- **TAPD**：bug=1023220
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【构建报错】 UnityBuild构建失败 https://www.tapd.cn/68880148/s/1392491

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 59490）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#9 (unicode) ====

30d29
< PRAGMA_DISABLE_OPTIMIZATION

2734,2736d2732
<

< PRAGMA_ENABLE_OPTIMIZATION

<
```

#### CL 67907 — 2024/07/04 — 郭智均

- **提交说明**：--bug=1025460 --user=郭智均 【VHM】处理Build贴图失败问题 https://www.tapd.cn/68880148/s/1420885
- **TAPD**：bug=1025460
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【VHM】处理Build贴图失败问题 https://www.tapd.cn/68880148/s/1420885

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.cpp` (edit)

📄 查看 VHM 相关 diff（CL 67907）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.cpp#4 (unicode) ====

14a15,16
>

> #include "WorldPartition/WorldPartitionRuntimeVirtualTextureBuilder.h"

27a30,40
>   UWorldPartition* WorldPartition = World->GetWorldPartition();

>   if (!WorldPartition)

>   {

>       UE_LOG(LogWorldPartitionVirtualHeightfieldMeshBuilder, Error, TEXT("Failed to retrieve WorldPartition."));

>       return false;

>   }

>

>   // Load required actors

>   FWorldPartitionHelpers::FForEachActorWithLoadingResult ForEachActorWithLoadingResult;

>   UWorldPartitionRuntimeVirtualTextureBuilder::LoadRuntimeVirtualTextureActors(WorldPartition, ForEachActorWithLoadingResult);

>
```

#### CL 76582 — 2024/07/10 — 郭智均

- **提交说明**：--bug=1025938 --user=郭智均 【场景】航线阶段或者远处看向空岛场景，空岛地面异常 https://www.tapd.cn/68880148/s/1455823
- **TAPD**：bug=1025938
- **涉及 VHM 文件**：3 个

**做了什么**：

提交目的：【场景】航线阶段或者远处看向空岛场景，空岛地面异常 https://www.tapd.cn/68880148/s/1455823

- **Shader**：2 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (integrate)
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (integrate)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 76582）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#4 (text) ====

35a36,38
>   uint SampleTextureLevel;

>   uint2 SampleTexPos;

>   float SampleGeoToTexLevelOffsetInv;

89a93,97
>   Info.SampleTextureLevel = min(Item.Level, VHMParam.MaxLevel - InRVTMinLevel);

>

>   const uint SampleGeoToTexLevelOffset = min(InRVTMinLevel, VHMParam.MaxLevel - Item.Level);

>   Info.SampleTexPos = Item.Pos >> SampleGeoToTexLevelOffset;

>   Info.SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << SampleGeoToTexLevelOffset);

148c156
<   const float PhysicalAddress = PageTableTexture.Load(int3(Info.TexPos, Info.TextureLevel));

---
>   const float PhysicalAddress = PageTableTexture.Load(int3(Info.SampleTexPos, Info.SampleTextureLevel));

155c163,165
<   const float3 UVTransform = GetVirtualToPhysicalUVTransform(Info.Pos, Info.GeoToTexLevelOffsetInv, Info.TextureLevel,

---
>   const float3 UVTransform = GetVirtualToPhysicalUVTransform(Info.Pos,

>       // Info.GeoToTexLevelOffsetInv, Info.TextureLevel,

>       Info.SampleGeoToTexLevelOffsetInv, Info.SampleTextureLevel,

179c189,190
<   const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.

---
>   // const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.

>   const uint MultiWriteCount = 1;

187c198
<       uint LevelPlusOne = ThisInfo.TextureLevel + 1;

---
>       uint LevelPlusOne = ThisInfo.SampleTextureLevel + 1;

189c200
<       uint PackData = ThisInfo.TexPos.x | (ThisInfo.TexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;

---
>       uint PackData = ThisInfo.SampleTexPos.x | (ThisInfo.SampleTexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#3 (text) ====

129a130
>   const uint MaxSampleLevel = VHM.MaxLod - RVT_MIN_LEVEL;

134c135,137
<   float SampleLevel = TextureLevel;

---
>   float SampleLevel = min(Level, MaxSampleLevel);

>   const uint SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - Level);

>   float SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << SampleGeoToTexLevelOffset);

140,141c143,145
<           GeoToTexLevelOffsetInv,

<           TextureLevel,

---
>           // GeoToTexLevelOffsetInv,

>           // TextureLevel,

>           SampleGeoToTexLevelOffsetInv, SampleLevel,

149,151c153,155
<       uint _GeoOffset = max(int(RVT_MIN_LEVEL) - int(ThisLevel), 0);

<       float _GeoOffsetInv = 1.0f / float(1u << _GeoOffset);

<       float _TexLevel = max(int(ThisLevel) - int(RVT_MIN_LEVEL), 0);

---
>       const uint _SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - ThisLevel);

>       float _SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << _SampleGeoToTexLevelOffset);

>       float _SampleLevel = min(ThisLevel, MaxSampleLevel);

154,155c158,160
<           _GeoOffsetInv,

<           _TexLevel,

---
>           // _GeoOffsetInv,

>           // _TexLevel,

>           _SampleGeoToTexLevelOffsetInv, _SampleLevel,

163,165c168,170
<       uint _GeoOffset = max(int(RVT_MIN_LEVEL) - int(ThisLevel), 0);

<       float _GeoOffsetInv = 1.0f / float(1u << _GeoOffset);

<       float _TexLevel = max(int(ThisLevel) - int(RVT_MIN_LEVEL), 0);

---
>       const uint _SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - ThisLevel);

>       float _SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << _SampleGeoToTexLevelOffset);

>       float _SampleLevel = min(ThisLevel, MaxSampleLevel);

168,169c173,175
<           _GeoOffsetInv,

<           _TexLevel,

---
>           // _GeoOffsetInv,

>           // _TexLevel,

>           _SampleGeoToTexLevelOffsetInv, _SampleLevel,

213c219,220
<       SampleLevel = max(max(0, LodClamped) - float(RVT_MIN_LEVEL), 0);

---
>       // SampleLevel = max(max(0, LodClamped) - float(RVT_MIN_LEVEL), 0);

>       SampleLevel = min(max(0, LodClamped - 0.5f), MaxSampleLevel);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#10 (unicode) ====

2636c2636,2637
<           bool WithFeedback = (MaxCalTime - CalTime) > (int32)VolatileBuffers.VHMParameter->RVTMinLevel;

---
>           // bool WithFeedback = (MaxCalTime - CalTime) > (int32)VolatileBuffers.VHMParameter->RVTMinLevel;

>           bool WithFeedback = true;
```

#### CL 77264 — 2024/07/10 — 郭智均

- **提交说明**：--bug=1025415 --user=郭智均 【crash】【vhm】优化&Fix Stat Buffer引起的崩溃 https://www.tapd.cn/68880148/s/1457796
- **TAPD**：bug=1025415
- **涉及 VHM 文件**：3 个

**做了什么**：

提交目的：【crash】【vhm】优化&Fix Stat Buffer引起的崩溃 https://www.tapd.cn/68880148/s/1457796

- **Shader**：2 个文件
- `Shaders/Private/VirtualHeightfieldInitBuffers.usf` (edit)
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 77264）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldInitBuffers.usf#2 (text) ====

51a52
> #if CLEAR_VT_COUNT

52a54
> #endif


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#5 (text) ====

7c7
< #define COLL_THREAD_TOTAL 64

---
> #define COLL_THREAD_TOTAL 32

863c863
<       OutDispatchArgsBuffer[0] = 1;

---
>       OutDispatchArgsBuffer[0] = 2;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#11 (unicode) ====

100c100
< #if UE_BUILD_SHIPPING

---
> #if UE_BUILD_SHIPPING || UE_BUILD_TEST

126a127,134
>

>

> static TAutoConsoleVariable<int32> CVarVHMEnableStat(

>   TEXT("r.VHM.StatEnable"),

>   0,

>   TEXT("Whether VHM open Stat."),

>   ECVF_RenderThreadSafe

> );

135c143
< //#if VHM_ENABLE_STAT

---
> #if VHM_ENABLE_STAT

138c146
< //#endif

---
> #endif

378,379d385
<           FRDGBufferRef FeedbackBuffer;

<           FRDGBufferUAVRef FeedbackBufferUAV;

484a491,494
>

>   // all vhm use one feedback buffer;

>   FRDGBufferRef VTFeedbackBuf;

>   FRDGBufferUAVRef VTFeedbackBufUAV;

1252a1263,1266
>           class FClearVTCountDim : SHADER_PERMUTATION_BOOL("CLEAR_VT_COUNT");

>

>           using FPermutationDomain = TShaderPermutationDomain<FClearVTCountDim>;

>

1700,1703d1713
<           FRDGBufferDesc FeedbackBufferDesc = FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxFeedbackItems + 1);

<           FeedbackBufferDesc.Usage = EBufferUsageFlags(FeedbackBufferDesc.Usage | BUF_SourceCopy);

<           OutResources.FeedbackBuffer = GraphBuilder.CreateBuffer(FeedbackBufferDesc, TEXT("VHM.FeedbackBuffer"));

<           OutResources.FeedbackBufferUAV = GraphBuilder.CreateUAV(OutResources.FeedbackBuffer, PF_R32_UINT);

1932c1942,1943
<           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, FDrawInstanceBuffers& InDrawBuffers)

---
>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, FDrawInstanceBuffers& InDrawBuffers,

>           FRDGBufferUAVRef VTFeedbackBufUAV=nullptr)

1936c1947
<           Parameters->RWFeedbackBuffer = GraphBuilder.CreateUAV(InVolatileBuffers.FeedbackBuffer, PF_R32_UINT);

---
>           Parameters->RWFeedbackBuffer = VTFeedbackBufUAV;

1944d1954
<           AddClearUAVPass(GraphBuilder, InVolatileBuffers.FeedbackBufferUAV, 0xffffffff);

1946c1956,1959
<           TShaderMapRef<FInitAllBuffers_CS> ComputeShader(InGlobalShaderMap);

---
>           FInitAllBuffers_CS::FPermutationDomain PermutationVector;

>           PermutationVector.Set<FInitAllBuffers_CS::FClearVTCountDim>(VTFeedbackBufUAV != nullptr);

>

>           TShaderMapRef<FInitAllBuffers_CS> ComputeShader(InGlobalShaderMap, PermutationVector);

2013c2026,2027
<           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, uint32 CalTime, bool WithCull, bool WithFeedback=true)

---
>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, FRDGBufferUAVRef VTFeedbackBufferUAV,

>           uint32 CalTime, bool WithCull, bool WithFeedback=true)

2027c2041
<           Parameters->RWFeedbackBuffer = InVolatileBuffers.FeedbackBufferUAV;

---
>           Parameters->RWFeedbackBuffer = VTFeedbackBufferUAV;

2379c2393
<               {

---
>               if (CVarVHMEnableStat->GetInt() != 0) {

2590a2605,2607
>   DECLARE_GPU_STAT(VHM_CS)

>   DECLARE_GPU_STAT(VHM_VTFeedback)

>

2600a2618,2626
>

>   {

>       FRDGBufferDesc FeedbackBufferDesc = FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), CVarVHMMaxFeedbackItems->GetInt() + 1);

>       FeedbackBufferDesc.Usage = EBufferUsageFlags(FeedbackBufferDesc.Usage | BUF_SourceCopy);

>       VTFeedbackBuf = GraphBuilder.CreateBuffer(FeedbackBufferDesc, TEXT("VHM.FeedbackBuffer"));

>       VTFeedbackBufUAV = GraphBuilder.CreateUAV(VTFeedbackBuf, PF_R32_UINT);

>

>       AddClearUAVPass(GraphBuilder, VTFeedbackBufUAV, 0xffffffff);

>   }

2607a2634,2636
>       RDG_EVENT_SCOPE(GraphBuilder, "VHM_CS");

>       RDG_GPU_STAT_SCOPE(GraphBuilder, VHM_CS);

>

2629c2658,2660
<       VirtualHeightfieldMesh::V2::AddPass_InitAllBuffers(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers, DrawBuffers);

---
>       VirtualHeightfieldMesh::V2::AddPass_InitAllBuffers(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers, DrawBuffers,

>           // just clear tv feedback count at first

>           WorkIndex == 0 ? VTFeedbackBufUAV : nullptr);

2639c2670
<               VolatileBuffers, CalTime, true, WithFeedback);

---
>               VolatileBuffers, VTFeedbackBufUAV, CalTime, true, WithFeedback);

2642,2646d2672
<       // Submit Feedback Buffer

<       FVirtualTextureFeedbackBufferDesc Desc;

<       Desc.Init(CVarVHMMaxFeedbackItems.GetValueOnRenderThread() + 1);

<       SubmitVirtualTextureFeedbackBuffer(GraphBuilder, VolatileBuffers.FeedbackBuffer, Desc);

<

2659a2686
>       if (CVarVHMEnableStat->GetInt() != 0)

2665a2693,2702
>

>   {

>       RDG_EVENT_SCOPE(GraphBuilder, "VHM_VTFeedback");

>       RDG_GPU_STAT_SCOPE(GraphBuilder, VHM_VTFeedback);

>

>       // Submit Feedback Buffer

>       FVirtualTextureFeedbackBufferDesc Desc;

>       Desc.Init(CVarVHMMaxFeedbackItems.GetValueOnRenderThread() + 1);

>       SubmitVirtualTextureFeedbackBuffer(GraphBuilder, VTFeedbackBuf, Desc);

>   }

2673a2711,2715
>   if (CVarVHMEnableStat->GetInt() == 0)

>   {

>       return;

>   }

>
```

#### CL 77400 — 2024/07/10 — 郭智均

- **提交说明**：--bug=1025415 --user=郭智均 【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 https://www.tapd.cn/68880148/s/1457994
- **TAPD**：bug=1025415
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 https://www.tapd.cn/68880148/s/1457994

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 77400）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#12 (unicode) ====

193c193
< //#if VHM_ENABLE_STAT

---
> #if VHM_ENABLE_STAT

195c195
< //#endif

---
> #endif

1635c1635
< //#if VHM_ENABLE_STAT

---
> #if VHM_ENABLE_STAT

1641c1641
< //#endif

---
> #endif

2730c2730,2736
<       if (BufDiscardId >= MaxReadBackSize)

---
>

>       if (Buffers[WorkDesc.BufferIndex].StatBufferReadBacks.IsEmpty())

>       {

>           continue;

>       }

>

>       if (BufDiscardId >= MaxReadBackSize && BufDiscardId == DiscardId)
```

#### CL 77461 — 2024/07/10 — 郭智均

- **提交说明**：--bug=1025415 --user=郭智均 【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 https://www.tapd.cn/68880148/s/1457994
- **TAPD**：bug=1025415
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【crash】S1/release/UE5EA/Engine/Source/Runtime/RHI/Private/RHIGPUReadback.cpp:70 https://www.tapd.cn/68880148/s/1457994

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 77461）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#13 (unicode) ====

143c143
< #if VHM_ENABLE_STAT

---
> // #if VHM_ENABLE_STAT

146c146
< #endif

---
> // #endif
```

#### CL 81681 — 2024/07/13 — 郭智均

- **提交说明**：--story=1022169 --user=郭智均 【VHM】VHM屏蔽GetGlobalVirtualTextureMipBias https://www.tapd.cn/68880148/s/1476472
- **TAPD**：story=1022169
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【VHM】VHM屏蔽GetGlobalVirtualTextureMipBias https://www.tapd.cn/68880148/s/1476472

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (integrate)

📄 查看 VHM 相关 diff（CL 81681）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#4 (text) ====

3a4,5
>

> #define VT_DISABLE_VIEW_UNIFORM_BUFFER 1

4a7
>
```

#### CL 82353 — 2024/07/15 — 郭智均

- **提交说明**：--bug=1022489 --user=郭智均 【日志告警】【netlog\_ensure告警】ieldMeshSceneProxycpp564ExpressionbInFrameMessageTitleGravitationensurefailedCallstackSGameexeFVirtualHeightfieldMeshRendererExtension https://www.tapd.cn/68880148/s/1479884
- **TAPD**：bug=1022489
- **涉及 VHM 文件**：3 个

**做了什么**：

提交目的：【日志告警】【netlog\_ensure告警】ieldMeshSceneProxycpp564ExpressionbInFrameMessageTitleGravitationensurefailedCallstackSGameexeFVirtualHeightfieldMeshRendererExtension https://www.tapd.cn/68880148/s/1479884

- **Runtime C++**：3 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (edit)

📄 查看 VHM 相关 diff（CL 82353）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp#4 (unicode) ====

1,223c1,228
< // Copyright Epic Games, Inc. All Rights Reserved.

<

< #include "VirtualHeightfieldMeshComponent.h"

<

< #include "Components/RuntimeVirtualTextureComponent.h"

< #include "HeightfieldMinMaxTexture.h"

< #include "SceneInterface.h"

< #include "VirtualHeightfieldMeshEnable.h"

< #include "VirtualHeightfieldMeshSceneProxy.h"

< #include "VT/RuntimeVirtualTexture.h"

< #include "VT/RuntimeVirtualTextureVolume.h"

<

< #pragma region S1_Engine_Shiyu

< #include "HeightfieldMaskTexture.h"

< #pragma endregion

<

< #include UE_INLINE_GENERATED_CPP_BY_NAME(VirtualHeightfieldMeshComponent)

<

< UVirtualHeightfieldMeshComponent::UVirtualHeightfieldMeshComponent(const FObjectInitializer& ObjectInitializer)

<   : Super(ObjectInitializer)

< {

<   CastShadow = true;

<   bCastContactShadow = false;

<   bUseAsOccluder = true;

<   bAffectDynamicIndirectLighting = false;

<   bAffectDistanceFieldLighting = false;

<   bNeverDistanceCull = true;

<   bEnableAutoLODGeneration = false;

<   Mobility = EComponentMobility::Static;

< }

<

< void UVirtualHeightfieldMeshComponent::OnRegister()

< {

<   VirtualTextureRef = VirtualTexture.Get();

<

<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

<   if (RuntimeVirtualTextureComponent)

<   {

<       // Bind to delegate so that we dirty render state whenever RuntimeVirtualTextureComponent is moved.

<       RuntimeVirtualTextureComponent->TransformUpdated.AddUObject(this, &UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate);

<       // Bind to delegate so that RuntimeVirtualTextureComponent will pull hide flags from this object.

<       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().AddUObject(this, &UVirtualHeightfieldMeshComponent::GatherHideFlags);

<       RuntimeVirtualTextureComponent->MarkRenderStateDirty();

<   }

<

<   Super::OnRegister();

< }

<

< void UVirtualHeightfieldMeshComponent::OnUnregister()

< {

<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

<   if (RuntimeVirtualTextureComponent)

<   {

<       RuntimeVirtualTextureComponent->TransformUpdated.RemoveAll(this);

<       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().RemoveAll(this);

<       RuntimeVirtualTextureComponent->MarkRenderStateDirty();

<   }

<

<   VirtualTextureRef = nullptr;

<

<   Super::OnUnregister();

< }

<

< void UVirtualHeightfieldMeshComponent::ApplyWorldOffset(const FVector& InOffset, bool bWorldShift)

< {

<   Super::ApplyWorldOffset(InOffset, bWorldShift);

<   MarkRenderStateDirty();

< }

<

< ARuntimeVirtualTextureVolume* UVirtualHeightfieldMeshComponent::GetVirtualTextureVolume() const

< {

<   return VirtualTextureRef;

< }

<

< URuntimeVirtualTexture* UVirtualHeightfieldMeshComponent::GetVirtualTexture() const

< {

<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

<   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetVirtualTexture() : nullptr;

< }

<

< FTransform UVirtualHeightfieldMeshComponent::GetVirtualTextureTransform() const

< {

<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

<   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetComponentTransform() * RuntimeVirtualTextureComponent->GetTexelSnapTransform() : FTransform::Identity;

< }

<

< bool UVirtualHeightfieldMeshComponent::IsVisible() const

< {

<   return

<       Super::IsVisible() &&

<       GetVirtualTexture() != nullptr &&

<       GetVirtualTexture()->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight &&

<       VirtualHeightfieldMesh::IsEnabled(GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5);

< }

<

< FBoxSphereBounds UVirtualHeightfieldMeshComponent::CalcBounds(const FTransform& LocalToWorld) const

< {

<   return FBoxSphereBounds(FBox(FVector(0.f, 0.f, 0.f), FVector(1.f, 1.f, 1.f))).TransformBy(LocalToWorld);

< }

<

< FPrimitiveSceneProxy* UVirtualHeightfieldMeshComponent::CreateSceneProxy()

< {

<   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;

<   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);

<   return bIsEnabled ? new FVirtualHeightfieldMeshSceneProxy(this) : nullptr;

< }

<

< void UVirtualHeightfieldMeshComponent::SetMaterial(int32 InElementIndex, UMaterialInterface* InMaterial)

< {

<   if (InElementIndex == 0 && Material != InMaterial)

<   {

<       Material = InMaterial;

<       MarkRenderStateDirty();

<   }

< }

<

< void UVirtualHeightfieldMeshComponent::GetUsedMaterials(TArray<UMaterialInterface*>& OutMaterials, bool bGetDebugMaterials) const

< {

<   if (Material != nullptr)

<   {

<       OutMaterials.Add(Material);

<   }

< #pragma region S1_Engine_Shiyu

<   if (HoleMaterial != nullptr)

<   {

<       OutMaterials.Add(HoleMaterial);

<   }

< #pragma endregion

< }

<

< void UVirtualHeightfieldMeshComponent::GatherHideFlags(bool& InOutHidePrimitivesInEditor, bool& InOutHidePrimitivesInGame) const

< {

<   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;

<   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);

<   InOutHidePrimitivesInEditor |= (bIsEnabled && !bHiddenInEditor);

<   InOutHidePrimitivesInGame |= bIsEnabled;

< }

<

< void UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate(USceneComponent* InRootComponent, EUpdateTransformFlags UpdateTransformFlags, ETeleportType Teleport)

< {

<   MarkRenderStateDirty();

< }

<

< #if WITH_EDITOR

<

< void UVirtualHeightfieldMeshComponent::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)

< {

<   static const FName HideInEditorName = GET_MEMBER_NAME_CHECKED(UVirtualHeightfieldMeshComponent, bHiddenInEditor);

<

<   const FName PropertyName = PropertyChangedEvent.Property->GetFName();

<   if (PropertyName == HideInEditorName)

<   {

<       // Force RuntimeVirtualTextureComponent to poll the HidePrimitives settings.

<       URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

<       if (RuntimeVirtualTextureComponent != nullptr)

<       {

<           RuntimeVirtualTextureComponent->MarkRenderStateDirty();

<       }

<   }

<

<   Super::PostEditChangeProperty(PropertyChangedEvent);

< }

<

< #endif

<

< bool UVirtualHeightfieldMeshComponent::IsMinMaxTextureEnabled() const

< {

<   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();

<   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;

< }

<

< #if WITH_EDITOR

<

< bool UVirtualHeightfieldMeshComponent::IsMaskTextureEnabled() const

< {

<   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();

<   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;

< }

<

< void UVirtualHeightfieldMeshComponent::InitializeMinMaxTexture(uint32 InSizeX, uint32 InSizeY, uint32 InNumMips, uint8* InData)

< {

<   // We need an existing StreamingTexture object to update.

<   if (MinMaxTexture != nullptr)

<   {

<       FHeightfieldMinMaxTextureBuildDesc BuildDesc;

<       BuildDesc.SizeX = InSizeX;

<       BuildDesc.SizeY = InSizeY;

<       BuildDesc.NumMips = InNumMips;

<       BuildDesc.Data = InData;

<

<       MinMaxTexture->Modify();

<       MinMaxTexture->BuildTexture(BuildDesc);

<

<       MarkRenderStateDirty();

<   }

< }

<

< #pragma region S1_Engine_Shiyu

< void UVirtualHeightfieldMeshComponent::InitializeMaskTexture(uint32 InSizeX, uint32 InSizeY,

<   uint32 InNumMips, uint8* InData)

< {

<   // We need an existing StreamingTexture object to update.

<   if (MaskTexture != nullptr)

<   {

<       FHeightfieldMaskTextureBuildDesc BuildDesc;

<       BuildDesc.SizeX = InSizeX;

<       BuildDesc.SizeY = InSizeY;

<       BuildDesc.NumMips = InNumMips;

<       BuildDesc.Data = InData;

<

<       MaskTexture->Modify();

<       MaskTexture->BuildTexture(BuildDesc);

<

<       MarkRenderStateDirty();

<   }

< }

<

<

< #pragma endregion

<

<

< #endif

<

---
> // Copyright Epic Games, Inc. All Rights Reserved.
>
> #include "VirtualHeightfieldMeshComponent.h"
>
> #include "Components/RuntimeVirtualTextureComponent.h"
> #include "HeightfieldMinMaxTexture.h"
> #include "SceneInterface.h"
> #include "VirtualHeightfieldMeshEnable.h"
> #include "VirtualHeightfieldMeshSceneProxy.h"
> #include "VT/RuntimeVirtualTexture.h"
> #include "VT/RuntimeVirtualTextureVolume.h"
>
> #pragma region S1_Engine_Shiyu
> #include "HeightfieldMaskTexture.h"
> #pragma endregion
>
> #include UE_INLINE_GENERATED_CPP_BY_NAME(VirtualHeightfieldMeshComponent)
>
> UVirtualHeightfieldMeshComponent::UVirtualHeightfieldMeshComponent(const FObjectInitializer& ObjectInitializer)
>   : Super(ObjectInitializer)
> {
>   CastShadow = true;
>   bCastContactShadow = false;
>   bUseAsOccluder = true;
>   bAffectDynamicIndirectLighting = false;
>   bAffectDistanceFieldLighting = false;
>   bNeverDistanceCull = true;
>   bEnableAutoLODGeneration = false;
>   Mobility = EComponentMobility::Static;
>
>   ENQUEUE_RENDER_COMMAND(RegisterVHMExternal)([](FRHICommandListImmediate& RHICmdList)
>   {
>       FVirtualHeightfieldMeshSceneProxy::RegisterExternal();
>   });
> }
>
> void UVirtualHeightfieldMeshComponent::OnRegister()
> {
>   VirtualTextureRef = VirtualTexture.Get();
>
>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
>   if (RuntimeVirtualTextureComponent)
>   {
>       // Bind to delegate so that we dirty render state whenever RuntimeVirtualTextureComponent is moved.
>       RuntimeVirtualTextureComponent->TransformUpdated.AddUObject(this, &UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate);
>       // Bind to delegate so that RuntimeVirtualTextureComponent will pull hide flags from this object.
>       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().AddUObject(this, &UVirtualHeightfieldMeshComponent::GatherHideFlags);
>       RuntimeVirtualTextureComponent->MarkRenderStateDirty();
>   }
>
>   Super::OnRegister();
> }
>
> void UVirtualHeightfieldMeshComponent::OnUnregister()
> {
>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
>   if (RuntimeVirtualTextureComponent)
>   {
>       RuntimeVirtualTextureComponent->TransformUpdated.RemoveAll(this);
>       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().RemoveAll(this);
>       RuntimeVirtualTextureComponent->MarkRenderStateDirty();
>   }
>
>   VirtualTextureRef = nullptr;
>
>   Super::OnUnregister();
> }
>
> void UVirtualHeightfieldMeshComponent::ApplyWorldOffset(const FVector& InOffset, bool bWorldShift)
> {
>   Super::ApplyWorldOffset(InOffset, bWorldShift);
>   MarkRenderStateDirty();
> }
>
> ARuntimeVirtualTextureVolume* UVirtualHeightfieldMeshComponent::GetVirtualTextureVolume() const
> {
>   return VirtualTextureRef;
> }
>
> URuntimeVirtualTexture* UVirtualHeightfieldMeshComponent::GetVirtualTexture() const
> {
>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
>   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetVirtualTexture() : nullptr;
> }
>
> FTransform UVirtualHeightfieldMeshComponent::GetVirtualTextureTransform() const
> {
>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
>   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetComponentTransform() * RuntimeVirtualTextureComponent->GetTexelSnapTransform() : FTransform::Identity;
> }
>
> bool UVirtualHeightfieldMeshComponent::IsVisible() const
> {
>   return
>       Super::IsVisible() &&
>       GetVirtualTexture() != nullptr &&
>       GetVirtualTexture()->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight &&
>       VirtualHeightfieldMesh::IsEnabled(GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5);
> }
>
> FBoxSphereBounds UVirtualHeightfieldMeshComponent::CalcBounds(const FTransform& LocalToWorld) const
> {
>   return FBoxSphereBounds(FBox(FVector(0.f, 0.f, 0.f), FVector(1.f, 1.f, 1.f))).TransformBy(LocalToWorld);
> }
>
> FPrimitiveSceneProxy* UVirtualHeightfieldMeshComponent::CreateSceneProxy()
> {
>   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;
>   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);
>   return bIsEnabled ? new FVirtualHeightfieldMeshSceneProxy(this) : nullptr;
> }
>
> void UVirtualHeightfieldMeshComponent::SetMaterial(int32 InElementIndex, UMaterialInterface* InMaterial)
> {
>   if (InElementIndex == 0 && Material != InMaterial)
>   {
>       Material = InMaterial;
>       MarkRenderStateDirty();
>   }
> }
>
> void UVirtualHeightfieldMeshComponent::GetUsedMaterials(TArray<UMaterialInterface*>& OutMaterials, bool bGetDebugMaterials) const
> {
>   if (Material != nullptr)
>   {
>       OutMaterials.Add(Material);
>   }
> #pragma region S1_Engine_Shiyu
>   if (HoleMaterial != nullptr)
>   {
>       OutMaterials.Add(HoleMaterial);
>   }
> #pragma endregion
> }
>
> void UVirtualHeightfieldMeshComponent::GatherHideFlags(bool& InOutHidePrimitivesInEditor, bool& InOutHidePrimitivesInGame) const
> {
>   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;
>   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);
>   InOutHidePrimitivesInEditor |= (bIsEnabled && !bHiddenInEditor);
>   InOutHidePrimitivesInGame |= bIsEnabled;
> }
>
> void UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate(USceneComponent* InRootComponent, EUpdateTransformFlags UpdateTransformFlags, ETeleportType Teleport)
> {
>   MarkRenderStateDirty();
> }
>
> #if WITH_EDITOR
>
> void UVirtualHeightfieldMeshComponent::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)
> {
>   static const FName HideInEditorName = GET_MEMBER_NAME_CHECKED(UVirtualHeightfieldMeshComponent, bHiddenInEditor);
>
>   const FName PropertyName = PropertyChangedEvent.Property->GetFName();
>   if (PropertyName == HideInEditorName)
>   {
>       // Force RuntimeVirtualTextureComponent to poll the HidePrimitives settings.
>       URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
>       if (RuntimeVirtualTextureComponent != nullptr)
>       {
>           RuntimeVirtualTextureComponent->MarkRenderStateDirty();
>       }
>   }
>
>   Super::PostEditChangeProperty(PropertyChangedEvent);
> }
>
> #endif
>
> bool UVirtualHeightfieldMeshComponent::IsMinMaxTextureEnabled() const
> {
>   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();
>   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;
> }
>
> #if WITH_EDITOR
>
> bool UVirtualHeightfieldMeshComponent::IsMaskTextureEnabled() const
> {
>   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();
>   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;
> }
>
> void UVirtualHeightfieldMeshComponent::InitializeMinMaxTexture(uint32 InSizeX, uint32 InSizeY, uint32 InNumMips, uint8* InData)
> {
>   // We need an existing StreamingTexture object to update.
>   if (MinMaxTexture != nullptr)
>   {
>       FHeightfieldMinMaxTextureBuildDesc BuildDesc;
>       BuildDesc.SizeX = InSizeX;
>       BuildDesc.SizeY = InSizeY;
>       BuildDesc.NumMips = InNumMips;
>       BuildDesc.Data = InData;
>
>       MinMaxTexture->Modify();
>       MinMaxTexture->BuildTexture(BuildDesc);
>
>       MarkRenderStateDirty();
>   }
> }
>
> #pragma region S1_Engine_Shiyu
> void UVirtualHeightfieldMeshComponent::InitializeMaskTexture(uint32 InSizeX, uint32 InSizeY,
>   uint32 InNumMips, uint8* InData)
> {
>   // We need an existing StreamingTexture object to update.
>   if (MaskTexture != nullptr)
>   {
>       FHeightfieldMaskTextureBuildDesc BuildDesc;
>       BuildDesc.SizeX = InSizeX;
>       BuildDesc.SizeY = InSizeY;
>       BuildDesc.NumMips = InNumMips;
>       BuildDesc.Data = InData;
>
>       MaskTexture->Modify();
>       MaskTexture->BuildTexture(BuildDesc);
>
>       MarkRenderStateDirty();
>   }
> }
>
>
> #pragma endregion
>
>
> #endif
>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#14 (unicode) ====

1,2781c1,2791
< // Copyright Epic Games, Inc. All Rights Reserved.

<

< #include "VirtualHeightfieldMeshSceneProxy.h"

<

< #include "DataDrivenShaderPlatformInfo.h"

< #include "EngineModule.h"

< #include "Engine/Engine.h"

< #include "Engine/Texture2D.h"

< #include "GlobalRenderResources.h"

< #include "GlobalShader.h"

< #include "HeightfieldMaskTexture.h"

< #include "HeightfieldMinMaxTexture.h"

< #include "HLSLTypeAliases.h"

< #include "MaterialDomain.h"

< #include "Materials/Material.h"

< #include "Materials/MaterialRenderProxy.h"

< #include "PrimitiveViewRelevance.h"

< #include "RenderCaptureInterface.h"

< #include "RHIStaticStates.h"

< #include "RenderGraphUtils.h"

< #include "RHIGPUReadback.h"

< #include "SceneInterface.h"

< #include "SystemTextures.h"

< #include "TextureResource.h"

< #include "VirtualHeightfieldMeshComponent.h"

< #include "VirtualHeightfieldMeshVertexFactory.h"

< #include "VT/RuntimeVirtualTexture.h"

< #include "VT/VirtualTextureFeedbackBuffer.h"

<

<

< DECLARE_STATS_GROUP(TEXT("VirtualHeightfieldMesh"), STATGROUP_VirtualHeightfieldMesh, STATCAT_Advanced);

< DECLARE_CYCLE_STAT(TEXT("VirtualHeightfieldMesh SubmitWork"), STAT_VirtualHeightfieldMesh_SubmitWork, STATGROUP_VirtualHeightfieldMesh);

<

< DECLARE_LOG_CATEGORY_EXTERN(LogVirtualHeightfieldMesh, Warning, All);

< DEFINE_LOG_CATEGORY(LogVirtualHeightfieldMesh);

<

< static TAutoConsoleVariable<float> CVarVHMLodScale(

<   TEXT("r.VHM.LodScale"),

<   1.f,

<   TEXT("Global LOD scale applied for Virtual Heightfield Mesh."),

<   ECVF_RenderThreadSafe

< );

<

< // We disable View.LODDistanceFactor by default.

< // When it is set according to GCalcLocalPlayerCachedLODDistanceFactor in ULocalPlayer we end up with double couting of the FOV scale.

< // Ideally we would remove the calculation in ULocalPlayer and View.LODDistanceFactor would be only for view specific adjustments (screen captures etc.)

< // However the removal of the code in ULocalPlayer could have a big impact on any preexisting data in any project.

< static TAutoConsoleVariable<int32> CVarVHMEnableViewLodFactor(

<   TEXT("r.VHM.EnableViewLodFactor"),

<   0,

<   TEXT("Enable the View.LODDistanceFactor.")

<   TEXT("This is disabled by default to avoid an issue where FOV is double counted when calculating Lods.")

<   TEXT("See comment in code for more information."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMOcclusion(

<   TEXT("r.VHM.Occlusion"),

<   1,

<   TEXT("Enable occlusion queries."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxRenderItems(

<   TEXT("r.VHM.MaxRenderInstances"),

<   1024 * 64,

<   TEXT("Size of buffers used to collect render instances."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxFeedbackItems(

<   TEXT("r.VHM.MaxFeedbackItems"),

<   1024 * 4 * 10, // pre node write 10 time

<   TEXT("Size of buffer used by virtual texture feedback."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxPersistentQueueItems(

<   TEXT("r.VHM.MaxPersistentQueueItems"),

<   1024 * 64,

<   TEXT("Size of queue used in the collect pass. This is rounded to the nearest power of 2."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMCollectPassWavefronts(

<   TEXT("r.VHM.CollectPassWavefronts"),

<   1,

<   TEXT("Number of wavefronts to use for collect pass."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMVersion(

<   TEXT("r.VHM.Version"),

<   3,

<   TEXT("Version Of VHM"),

<   ECVF_RenderThreadSafe

< );

<

< #pragma region S1_Engine_Shiyu

< #if UE_BUILD_SHIPPING || UE_BUILD_TEST

< #define VHM_ENABLE_STAT 0

< #else

< #define VHM_ENABLE_STAT 1

< #endif

<

< #if VHM_ENABLE_STAT

< #include "Stats/Stats2.h"

< #include "Stats/StatsMisc.h"

<

< DECLARE_STATS_GROUP(TEXT("VHM"), STATGROUP_VHM, STATCAT_Advanced);

<

< DECLARE_DWORD_COUNTER_STAT(TEXT("BeforeCullInstances"), STAT_VHM_BeforeCullInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawTriangles"), STAT_VHM_DrawTriangles, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-ALL"), STAT_VHM_DrawInstancesALL, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Opacity"), STAT_VHM_DrawOpacityInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Mask"), STAT_VHM_DrawMaskInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD0"), STAT_VHM_DrawInstancesLOD0, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD1"), STAT_VHM_DrawInstancesLOD1, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD2"), STAT_VHM_DrawInstancesLOD2, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD3"), STAT_VHM_DrawInstancesLOD3, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD4"), STAT_VHM_DrawInstancesLOD4, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD5"), STAT_VHM_DrawInstancesLOD5, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD6"), STAT_VHM_DrawInstancesLOD6, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD7"), STAT_VHM_DrawInstancesLOD7, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD8"), STAT_VHM_DrawInstancesLOD8, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD9"), STAT_VHM_DrawInstancesLOD9, STATGROUP_VHM)

<

<

< static TAutoConsoleVariable<int32> CVarVHMEnableStat(

<   TEXT("r.VHM.StatEnable"),

<   0,

<   TEXT("Whether VHM open Stat."),

<   ECVF_RenderThreadSafe

< );

< #endif

<

< #pragma endregion

<

< static constexpr int32 IndirectArgsCount = 10;

< static constexpr int32 IndirectArgsByteSize = 4 * sizeof(uint32) * IndirectArgsCount;

< static constexpr int32 MergeDispatchArgsOffset = 5;

< #pragma region S1_Engine_Shiyu

< // #if VHM_ENABLE_STAT

< static constexpr int32 MaxStatCount = 64;

< static constexpr int32 StatBufferByteSize = sizeof(uint32) * MaxStatCount;

< // #endif

< #pragma endregion

<

<

< namespace VirtualHeightfieldMesh

< {

<   /** Buffers filled by GPU culling used by the Virtual Heightfield Mesh final draw call. */

<   struct FDrawInstanceBuffers

<   {

<       /* Culled instance buffer. */

<       FBufferRHIRef InstanceBuffer;

<       FUnorderedAccessViewRHIRef InstanceBufferUAV;

<       FShaderResourceViewRHIRef InstanceBufferSRV;

<

<       /* IndirectArgs buffer for final DrawInstancedIndirect. */

<       FBufferRHIRef IndirectArgsBuffer;

<       FUnorderedAccessViewRHIRef IndirectArgsBufferUAV;

<

< #pragma region S1_Engine_Shiyu

<       /* Culled hold instance buffer */

<       FBufferRHIRef HoleInstanceBuffer;

<       FUnorderedAccessViewRHIRef HoleInstanceBufferUAV;

<       FShaderResourceViewRHIRef HoleInstanceBufferSRV;

<

< //#if VHM_ENABLE_STAT

<       static constexpr uint32 MaxReadBackSize = 4;

<       /** For Stat  */

<       TArray<TUniquePtr<FRHIGPUBufferReadback>> StatBufferReadBacks;

< //#endif

< #pragma endregion

<   };

<

<   /** Initialize the FDrawInstanceBuffers objects. */

<   void InitializeInstanceBuffers(FRHICommandListImmediate& InRHICmdList, FDrawInstanceBuffers& InBuffers);

<

<   /** Release the FDrawInstanceBuffers objects. */

<   void ReleaseInstanceBuffers(FDrawInstanceBuffers& InBuffers)

<   {

<       InBuffers.InstanceBuffer.SafeRelease();

<       InBuffers.InstanceBufferUAV.SafeRelease();

<       InBuffers.InstanceBufferSRV.SafeRelease();

<       InBuffers.IndirectArgsBuffer.SafeRelease();

<       InBuffers.IndirectArgsBufferUAV.SafeRelease();

< #pragma region S1_Engine_Shiyu

<       InBuffers.HoleInstanceBuffer.SafeRelease();

<       InBuffers.HoleInstanceBufferUAV.SafeRelease();

<       InBuffers.HoleInstanceBufferSRV.SafeRelease();

< #if VHM_ENABLE_STAT

<       InBuffers.StatBufferReadBacks.Empty();

< #endif

< #pragma endregion

<   }

<

<   namespace V2

<   {

<       struct FInnerBuffers

<       {

<           // // for ps

<           // // - use to draw quad by default material

<           // FBufferRHIRef QuadInstanceArgsBuffer;

<           // FUnorderedAccessViewRHIRef QuadInstanceArgsBufferUAV;

<           // FBufferRHIRef QuadInstanceBuffer;

<           // FUnorderedAccessViewRHIRef QuadInstanceBufferUAV;

<           // FShaderResourceViewRHIRef QuadInstanceBufferSRV;

<           // // - use to draw quad by hole material

<           // FBufferRHIRef HoleQuadInstanceArgsBuffer;

<           // FUnorderedAccessViewRHIRef HoleQuadInstanceArgsBufferUAV;

<           // FBufferRHIRef HoleQuadInstanceBuffer;

<           // FUnorderedAccessViewRHIRef HoleQuadInstanceBufferUAV;

<           // FShaderResourceViewRHIRef HoleQuadInstanceBufferSRV;

<

<           int32 CalTime = -1;

<           // use to compure shader

<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};

<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};

<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};

<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};

<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};

<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};

<

<           FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

<           }

<           FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

<           }

<

<           FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

<           }

<           FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

<           }

<       };

<

<       void InitializeInnerBuffers(FRHICommandListImmediate& RHICmdList, FInnerBuffers& InBuffers)

<       {

<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();

<           const TCHAR* FinalQuadName[2] = {

<               TEXT("VHM.FinalQuadBuffer_0"),

<               TEXT("VHM.FinalQuadBuffer_1")};

<           const TCHAR* FinalQuadArgsName[2] = {

<               TEXT("VHM.FinalQuadArgsBuffer_0"),

<               TEXT("VHM.FinalQuadArgsBuffer_1")};

<

<           for (int i = 0; i < 2; ++i)

<           {

<               InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(

<                   FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),

<                   FinalQuadArgsName[i]

<               );

<               InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(

<                   FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)

<

<                   ,

<                   FinalQuadName[i]

<               );

<           }

<       }

<

<       void ReleaseInnerBuffers(FInnerBuffers& InBuffers)

<       {

<           InBuffers.CalTime = -1;

<           for(int i = 0; i < 2; ++i)

<           {

<               InBuffers.FinalQuadArgsBuffer[i].SafeRelease();

<               InBuffers.FinalQuadBuffer[i].SafeRelease();

<           }

<

<       }

<   }

<

< }

<

< struct FOcclusionResults

< {

<   FTexture2DRHIRef OcclusionTexture;

<   FIntPoint TextureSize;

<   int32 NumTextureMips;

<   TArray<bool> UploadData;

< };

<

< struct FOcclusionResultsKey

< {

<   FVirtualHeightfieldMeshSceneProxy const* Proxy;

<   FSceneView const* View;

<

<   FOcclusionResultsKey(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InView)

<       : Proxy(InProxy)

<       , View(InView)

<   {

<   }

<

<   friend inline uint32 GetTypeHash(const FOcclusionResultsKey& InKey)

<   {

<       return HashCombine(GetTypeHash(InKey.View), GetTypeHash(InKey.Proxy));

<   }

<

<   friend bool operator==(const FOcclusionResultsKey& A, const FOcclusionResultsKey& B)

<   {

<       return A.View == B.View && A.Proxy == B.Proxy;

<   }

< };

<

<

<

< /** Global map for occlusion result. */

< TMap< FOcclusionResultsKey, FOcclusionResults > GOcclusionResults;

< bool GOcclusionResetRequired = false;

<

< namespace VirtualHeightfieldMesh

< {

<   /** Calculate distances used for LODs in a given view for a given scene proxy. */

<   FVector4f CalculateLodRanges(FSceneView const* InView, FVirtualHeightfieldMeshSceneProxy const* InProxy)

<   {

<       const uint32 MaxLevel = FMath::Max(InProxy->RVTMaxLevel - InProxy->Lod0LevelBias, 0);

<       const float Lod0UVSize = 1.f / (float)(1 << MaxLevel);

<       const FVector2D Lod0WorldSize = FVector2D(InProxy->UVToWorldScale.X, InProxy->UVToWorldScale.Y) * Lod0UVSize; // LWC_TODO: precision loss

<       const float Lod0WorldRadius = Lod0WorldSize.Size();

<       const float ScreenMultiple = FMath::Max(0.5f * InView->ViewMatrices.GetProjectionMatrix().M[0][0], 0.5f * InView->ViewMatrices.GetProjectionMatrix().M[1][1]);

<       const float Lod0Distance = Lod0WorldRadius * ScreenMultiple / InProxy->Lod0ScreenSize;

<       const float ViewLodDistanceFactor = CVarVHMEnableViewLodFactor.GetValueOnRenderThread() == 0 ? 1.f : InView->LODDistanceFactor;

<       const float LodScale = ViewLodDistanceFactor * CVarVHMLodScale.GetValueOnRenderThread();

<

<       return FVector4f(Lod0Distance, InProxy->Lod0Distribution, InProxy->LodDistribution, LodScale);

<   }

<

<

<   namespace V2

<   {

<       BEGIN_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters,)

<           SHADER_PARAMETER(FVector3f,         ViewOrigin)

<           SHADER_PARAMETER(uint32,            OutBufferSizeMask)

<           SHADER_PARAMETER(FVector3f,         UVToWorldScale)

<           SHADER_PARAMETER(uint32,            FinalQuadBufferSizeMask)

<           SHADER_PARAMETER_ARRAY(FVector4f,   FrustumPlanes, [5])

<           SHADER_PARAMETER(FMatrix44f,        UVToWorld)

<           SHADER_PARAMETER(FVector4f,         LodDistances)

<           SHADER_PARAMETER(uint32,            MaxLevel)

<           SHADER_PARAMETER(uint32,            RVTMinLevel)

<           SHADER_PARAMETER(uint32,            PageTableFeedbackId)

<           SHADER_PARAMETER(uint32,            NumPhysicalAddressBits)

<           SHADER_PARAMETER(FVector4f,         PageTableSize)

<           SHADER_PARAMETER(FVector4f,         PhysicalPageTransform)

<           SHADER_PARAMETER(uint32,            QuadInstanceBufferSizeMask)

<           SHADER_PARAMETER(uint32,            NumIndices)

<           SHADER_PARAMETER(uint32,            MaxArgsCount)

<           SHADER_PARAMETER(uint32,            MaxStatCount)

<           SHADER_PARAMETER(uint32,            MergeDispatchArgsOffset)

<       END_UNIFORM_BUFFER_STRUCT()

<

<       IMPLEMENT_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters, "VHMParam")

<

<       struct FVolatileBuffers

<       {

<           FVHMCSSharedParameters* VHMParameter=nullptr;

<           TRDGUniformBufferRef<FVHMCSSharedParameters> VHMParameterUBuffer;

<           TArray<FRDGBufferRef, TFixedAllocator<2>> ArgsBuffer{nullptr, nullptr};

<           TArray<FRDGBufferRef, TFixedAllocator<2>> SubdivideQuadBuffer{nullptr, nullptr};

<           TArray<FRDGBufferRef, TFixedAllocator<2>> MergeQuadBuffer{nullptr, nullptr};

<

<

<           struct FSRVAndUAV

<           {

<               FRDGBufferSRVRef SRV = nullptr;

<               FRDGBufferUAVRef UAV = nullptr;

<               void Create(FRDGBuilder& GraphBuilder, FRDGBufferRef Buffer)

<               {

<                   EPixelFormat Format = uint32(Buffer->Desc.Usage & EBufferUsageFlags::DrawIndirect) != 0 ? PF_R32_UINT : PF_R32G32B32A32_UINT;

<                   SRV = GraphBuilder.CreateSRV(Buffer, Format);

<                   UAV = GraphBuilder.CreateUAV(Buffer, Format);

<               }

<           };

<           TArray<FSRVAndUAV, TFixedAllocator<2>> ArgsViews{{}, {}};

<           TArray<FSRVAndUAV, TFixedAllocator<2>> SubdivideViews{{}, {}};

<           TArray<FSRVAndUAV, TFixedAllocator<2>> MergeViews{{}, {}};

<

<           FRHITexture* PageTableTexture = nullptr;

<           FRHITexture* MaskTexture = nullptr;

<           FRHIShaderResourceView* HeightTexture = nullptr;

<           FRHITexture* HeightMinMaxTexture = nullptr;

<

< //#if VHM_ENABLE_STAT

<           FRDGBufferRef StatBuffer;

<           FRDGBufferUAVRef StatBufferUAV;

< //#endif

<       };

<   }

< }

<

< /** Renderer extension to manage the buffer pool and add hooks for GPU culling passes. */

< class FVirtualHeightfieldMeshRendererExtension : public FRenderResource

< {

< public:

<   FVirtualHeightfieldMeshRendererExtension()

<       : bInFrame(false)

<       , DiscardId(0)

<   {}

<

<   virtual ~FVirtualHeightfieldMeshRendererExtension()

<   {}

<

<   /** Call once to register this extension. */

<   void RegisterExtension();

<

<   /** Are we inside a BeginFrame()/EndFrame() scope? */

<   bool IsInFrame() { return bInFrame; }

<

<   /** Call once per frame for each mesh/view that has relevance. This allocates the buffers to use for the frame and adds the work to fill the buffers to the queue. */

<   VirtualHeightfieldMesh::FDrawInstanceBuffers& AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView);

<   /** Submit all the work added by AddWork(). The work fills all of the buffers ready for use by the referencing mesh batches. */

<   void SubmitWork(FRDGBuilder& GraphBuilder);

<

<   void InitVolatileBuffers(FRDGBuilder& GraphBuilder, int WorkIndex, VirtualHeightfieldMesh::V2::FVolatileBuffers& VolatileBuffers);

<

<   // void SubmitWork_V2(FRDGBuilder& GraphBuilder);

<

<   void SubmitWork_V3(FRDGBuilder& GraphBuilder);

<

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   void CollectStat();

< #endif

< #pragma endregion

<

< protected:

<   //~ Begin FRenderResource Interface

<   virtual void ReleaseRHI() override;

<   //~ End FRenderResource Interface

<

< private:

<   /** Called by renderer at start of render frame. */

<   void BeginFrame(FRDGBuilder& GraphBuilder);

<   /** Called by renderer at end of render frame. */

<   void EndFrame(FRDGBuilder& GraphBuilder);

<   void EndFrame();

<

<

<   /** Flag for frame validation. */

<   bool bInFrame;

<

<   /** Buffers to fill. Resources can persist between frames to reduce allocation cost, but contents don't persist. */

<   TArray<VirtualHeightfieldMesh::FDrawInstanceBuffers> Buffers;

<   TArray<VirtualHeightfieldMesh::V2::FInnerBuffers> InnerBuffers;

<   /** Per buffer frame time stamp of last usage. */

<   TArray<uint32> DiscardIds;

<   /** Current frame time stamp. */

<   uint32 DiscardId;

<

<   /** Arrary of uniqe scene proxies to render this frame. */

<   TArray<FVirtualHeightfieldMeshSceneProxy const*> SceneProxies;

<   /** Arrary of unique main views to render this frame. */

<   TArray<FSceneView const*> MainViews;

<   /** Arrary of unique culling views to render this frame. */

<   TArray<FSceneView const*> CullViews;

<

<   /** Key for each buffer we need to generate. */

<   struct FWorkDesc

<   {

<       int32 ProxyIndex;

<       int32 MainViewIndex;

<       int32 CullViewIndex;

<       int32 BufferIndex;

<   };

<

<   /** Keys specifying what to render. */

<   TArray<FWorkDesc> WorkDescs;

<

<   /** Sort predicate for FWorkDesc. When rendering we want to batch work by proxy, then by main view. */

<   struct FWorkDescSort

<   {

<       uint32 SortKey(FWorkDesc const& WorkDesc) const

<       {

<           return (WorkDesc.ProxyIndex << 24) | (WorkDesc.MainViewIndex << 16) | (WorkDesc.CullViewIndex << 8) | WorkDesc.BufferIndex;

<       }

<

<       bool operator()(FWorkDesc const& A, FWorkDesc const& B) const

<       {

<           return SortKey(A) < SortKey(B);

<       }

<   };

<

<   // all vhm use one feedback buffer;

<   FRDGBufferRef VTFeedbackBuf;

<   FRDGBufferUAVRef VTFeedbackBufUAV;

< };

<

< /** Single global instance of the VirtualHeightfieldMesh renderer extension. */

< TGlobalResource< FVirtualHeightfieldMeshRendererExtension > GVirtualHeightfieldMeshViewRendererExtension;

<

< void FVirtualHeightfieldMeshRendererExtension::RegisterExtension()

< {

<   static bool bInit = false;

<   if (!bInit)

<   {

<       GEngine->GetPreRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::BeginFrame);

<       GEngine->GetPostRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::EndFrame);

<       bInit = true;

<   }

< }

<

< void FVirtualHeightfieldMeshRendererExtension::ReleaseRHI()

< {

<   Buffers.Empty();

< }

<

< VirtualHeightfieldMesh::FDrawInstanceBuffers& FVirtualHeightfieldMeshRendererExtension::AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView)

< {

<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.

<   if (!ensure(!bInFrame))

<   {

<       EndFrame();

<   }

<

<   // Create workload

<   FWorkDesc WorkDesc;

<   WorkDesc.ProxyIndex = SceneProxies.AddUnique(InProxy);

<   WorkDesc.MainViewIndex = MainViews.AddUnique(InMainView);

<   WorkDesc.CullViewIndex = CullViews.AddUnique(InCullView);

<   WorkDesc.BufferIndex = -1;

<

<   // Check for an existing duplicate

<   for (FWorkDesc& It : WorkDescs)

<   {

<       if (It.ProxyIndex == WorkDesc.ProxyIndex && It.MainViewIndex == WorkDesc.MainViewIndex && It.CullViewIndex == WorkDesc.CullViewIndex && It.BufferIndex != -1)

<       {

<           WorkDesc.BufferIndex = It.BufferIndex;

<           break;

<       }

<   }

<

<   // Try to recycle a buffer

<   if (WorkDesc.BufferIndex == -1)

<   {

<       for (int32 BufferIndex = 0; BufferIndex < Buffers.Num(); BufferIndex++)

<       {

<           if (DiscardIds[BufferIndex] < DiscardId)

<           {

<               DiscardIds[BufferIndex] = DiscardId;

<               WorkDesc.BufferIndex = BufferIndex;

<               WorkDescs.Add(WorkDesc);

<               break;

<           }

<       }

<   }

<

<   // Allocate new buffer if necessary

<   if (WorkDesc.BufferIndex == -1)

<   {

<       DiscardIds.Add(DiscardId);

<       WorkDesc.BufferIndex = Buffers.AddDefaulted();

<       InnerBuffers.AddDefaulted(); // index is equal to BufferIndex

<       WorkDescs.Add(WorkDesc);

<       VirtualHeightfieldMesh::InitializeInstanceBuffers(GetImmediateCommandList_ForRenderCommand(), Buffers[WorkDesc.BufferIndex]);

<       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(GetImmediateCommandList_ForRenderCommand(), InnerBuffers[WorkDesc.BufferIndex]);

<   }

<

<   return Buffers[WorkDesc.BufferIndex];

< }

<

< void FVirtualHeightfieldMeshRendererExtension::BeginFrame(FRDGBuilder& GraphBuilder)

< {

<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.

<   if (!ensure(!bInFrame))

<   {

<       EndFrame();

<   }

<   bInFrame = true;

<

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   CollectStat();

< #endif

< #pragma endregion

<

<   if (WorkDescs.Num() > 0)

<   {

<       uint32 VHMVersion = CVarVHMVersion.GetValueOnRenderThread();

<       if (VHMVersion == 1)

<       {

<           SubmitWork(GraphBuilder);

<       }

<       else if(VHMVersion == 2)

<       {

<           // SubmitWork_V2(GraphBuilder);

<       }

<       else

<       {

<           SubmitWork_V3(GraphBuilder);

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshRendererExtension::EndFrame()

< {

<   ensure(bInFrame);

<   bInFrame = false;

<

<   SceneProxies.Reset();

<   MainViews.Reset();

<   CullViews.Reset();

<   WorkDescs.Reset();

<

<   // Clean the buffer pool

<   DiscardId++;

<

<   for (int32 Index = 0; Index < DiscardIds.Num();)

<   {

<       if (DiscardId - DiscardIds[Index] > 4u)

<       {

<           VirtualHeightfieldMesh::ReleaseInstanceBuffers(Buffers[Index]);

<           VirtualHeightfieldMesh::V2::ReleaseInnerBuffers(InnerBuffers[Index]);

<           Buffers.RemoveAtSwap(Index);

<           InnerBuffers.RemoveAtSwap(Index);

<           DiscardIds.RemoveAtSwap(Index);

<       }

<       else

<       {

<           ++Index;

<       }

<   }

<

<   GOcclusionResetRequired = true;

< }

<

< void FVirtualHeightfieldMeshRendererExtension::EndFrame(FRDGBuilder& GraphBuilder)

< {

<   EndFrame();

< }

<

< const static FName NAME_VirtualHeightfieldMesh(TEXT("VirtualHeightfieldMesh"));

<

< FVirtualHeightfieldMeshSceneProxy::FVirtualHeightfieldMeshSceneProxy(UVirtualHeightfieldMeshComponent* InComponent)

<   : FPrimitiveSceneProxy(InComponent, NAME_VirtualHeightfieldMesh)

<   , bHiddenInEditor(InComponent->GetHiddenInEditor())

<   , RuntimeVirtualTexture(InComponent->GetVirtualTexture())

<   , HeightMinMaxTexture(nullptr)

<   , LodBiasTexture(nullptr)

<   , LodBiasMinMaxTexture(nullptr)

< #pragma region S1_Engine_Shiyu

<   , MaskTexture(nullptr)

< #pragma endregion

<   , AllocatedVirtualTexture(nullptr)

<   , bCallbackRegistered(false)

<   , NumQuadsPerTileOfTwo(InComponent->GetNumQuadPerTileOfTwo()) // (1 << 4) * (1 << 4)

<   , VertexFactory(nullptr)

<   , Lod0ScreenSize(InComponent->GetLod0ScreenSize())

<   , Lod0Distribution(InComponent->GetLod0Distribution())

<   , LodDistribution(InComponent->GetLodDistribution())

<   , LodBiasScale(InComponent->GetLodBiasScale())

<   , NumForceLoadLods(InComponent->GetNumForceLoadLods())

<   , NumOcclusionLods(0)

<   , ExtSubdivisionLevel(InComponent->GetExtSubdivisionLevel())

<   , OcclusionGridSize(0, 0)

<   , RVTMaxLevel(0)

<   , Lod0LevelBias(InComponent->GetLod0LevelBias())

< {

<   GVirtualHeightfieldMeshViewRendererExtension.RegisterExtension();

<

<   // They have some LOD, but considered static as the LODs (are intended to) represent the same static surface.

<   bHasDeformableMesh = false;

<

<   UMaterialInterface* ComponentMaterial = InComponent->GetMaterial();

<   const bool bValidMaterial = ComponentMaterial != nullptr && ComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);

<   Material = bValidMaterial ? ComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();

<   MaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());

<

<   const FTransform VirtualTextureTransform = InComponent->GetVirtualTextureTransform();

<

<   UVToWorldScale = VirtualTextureTransform.GetScale3D();

<   UVToWorld = VirtualTextureTransform.ToMatrixWithScale();

<

<   WorldToUV = UVToWorld.Inverse();

<   WorldToUVTransposeAdjoint = WorldToUV.TransposeAdjoint();

<

<   // UVToLocal will be initialized in OnTransformChanged() called immediately after construction.

<   UVToLocal = FMatrix::Identity;

<

<   UHeightfieldMinMaxTexture* HeightfieldMinMaxTexture = InComponent->GetMinMaxTexture();

<   if (HeightfieldMinMaxTexture != nullptr)

<   {

<       HeightMinMaxTexture = HeightfieldMinMaxTexture->Texture;

<       BuildOcclusionVolumes(HeightfieldMinMaxTexture->TextureData, HeightfieldMinMaxTexture->TextureDataSize, HeightfieldMinMaxTexture->TextureDataMips, InComponent->GetNumOcclusionLods());

<

<       LodBiasTexture = HeightfieldMinMaxTexture->LodBiasTexture;

<       LodBiasMinMaxTexture = HeightfieldMinMaxTexture->LodBiasMinMaxTexture;

<   }

<

< #pragma region S1_Engine_Shiyu

<   UMaterialInterface* HoleComponentMaterial = InComponent->GetHoleMaterial();

<   const bool bValidHoleMaterial = HoleComponentMaterial != nullptr && HoleComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);

<   HoleMaterial = bValidHoleMaterial ? HoleComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();

<   HoleMaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());

<

<   UHeightfieldMaskTexture* HeightfieldMaskTexture = InComponent->GetMaskTexture();

<   if (HeightfieldMaskTexture)

<   {

<       MaskTexture = HeightfieldMaskTexture->Texture;

<   }

< #pragma endregion

< }

<

< SIZE_T FVirtualHeightfieldMeshSceneProxy::GetTypeHash() const

< {

<   static size_t UniquePointer;

<   return reinterpret_cast<size_t>(&UniquePointer);

< }

<

< uint32 FVirtualHeightfieldMeshSceneProxy::GetMemoryFootprint() const

< {

<   return(sizeof(*this) + FPrimitiveSceneProxy::GetAllocatedSize());

< }

<

< void FVirtualHeightfieldMeshSceneProxy::OnTransformChanged()

< {

<   UVToLocal = UVToWorld * GetLocalToWorld().Inverse();

<

<   // Setup a default occlusion volume array containing just the primitive bounds.

<   // We use this if disabling the full set of occlusion volumes.

<   DefaultOcclusionVolumes.Reset();

<   DefaultOcclusionVolumes.Add(GetBounds());

< }

<

< void FVirtualHeightfieldMeshSceneProxy::CreateRenderThreadResources()

< {

<   if (RuntimeVirtualTexture != nullptr)

<   {

<       if (!bCallbackRegistered)

<       {

<           GetRendererModule().AddVirtualTextureProducerDestroyedCallback(RuntimeVirtualTexture->GetProducerHandle(), &OnVirtualTextureDestroyedCB, this);

<           bCallbackRegistered = true;

<       }

<

<       if (RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight)

<       {

<           AllocatedVirtualTexture = RuntimeVirtualTexture->GetAllocatedVirtualTexture();

<

<

<           if (AllocatedVirtualTexture != nullptr)

<           {

<               uint32 TileSize = FMath::FloorLog2(RuntimeVirtualTexture->GetTileSize());

<               // check(TileSize + ExtSubdivisionLevel >= NumQuadsPerTileOfTwo);

<               NumQuadsPerTileOfTwo = FMath::Min(NumQuadsPerTileOfTwo, TileSize + ExtSubdivisionLevel - 1);

<               NumInstanceVertexSide = 1 << (TileSize + ExtSubdivisionLevel - NumQuadsPerTileOfTwo);

<               RVTMaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;

<               // Gather vertex factory uniform parameters.

<               FVirtualHeightfieldMeshVertexFactoryParameters UniformParams;

<               UniformParams.PageTableTexture = AllocatedVirtualTexture->GetPageTableTexture(0);

<               UniformParams.HeightTexture = AllocatedVirtualTexture->GetPhysicalTextureSRV(0, false);

<               UniformParams.HeightSampler = TStaticSamplerState<SF_Bilinear>::GetRHI();

<               UniformParams.LodBiasTexture = LodBiasTexture ? LodBiasTexture->GetResource()->TextureRHI : GBlackTexture->TextureRHI;

<               UniformParams.LodBiasSampler = TStaticSamplerState<SF_Point>::GetRHI();

<               UniformParams.NumInstanceVertexSide = NumInstanceVertexSide;

<               {

<                   UniformParams.MaxLod = RVTMaxLevel;

<                   UniformParams.RVTMinLevel = NumQuadsPerTileOfTwo;

<               }

<

<               FUintVector4 PackedUniform;

<               AllocatedVirtualTexture->GetPackedUniform(&PackedUniform, 0);

<               UniformParams.VTPackedUniform = PackedUniform;

<               FUintVector4 PackedPageTableUniform[2];

<               AllocatedVirtualTexture->GetPackedPageTableUniform(PackedPageTableUniform);

<               UniformParams.VTPackedPageTableUniform0 = PackedPageTableUniform[0];

<               UniformParams.VTPackedPageTableUniform1 = PackedPageTableUniform[1];

<

<               const float PageTableSizeX = AllocatedVirtualTexture->GetWidthInTiles();

<               const float PageTableSizeY = AllocatedVirtualTexture->GetHeightInTiles();

<               UniformParams.PageTableSize = FVector4f(PageTableSizeX, PageTableSizeY, 1.f / PageTableSizeX, 1.f / PageTableSizeY);

<

<               const float PhysicalTextureSize = AllocatedVirtualTexture->GetPhysicalTextureSize(0);

<               UniformParams.PhysicalTextureSize = FVector2f(PhysicalTextureSize, 1.f / PhysicalTextureSize);

<

<               UniformParams.VirtualHeightfieldToLocal = FMatrix44f(UVToLocal);

<               UniformParams.VirtualHeightfieldToWorld = FMatrix44f(UVToWorld);        // LWC_TODO: Precision loss

<

<               UniformParams.LodBiasScale = LodBiasScale;

<

<               const float PageSize = AllocatedVirtualTexture->GetVirtualTileSize();

<               const float PageBorderSize = AllocatedVirtualTexture->GetTileBorderSize();

<               const float PageAndBorderSize = PageSize + PageBorderSize * 2.f;

<               const float HalfTexelSize = 0.5f;

<               const FVector4 PhysicalPageTransform = FVector4(PageAndBorderSize, PageSize, PageBorderSize, HalfTexelSize) * (1.f / PhysicalTextureSize);

<               UniformParams.PhysicalPageTransform = (FVector4f)PhysicalPageTransform;

<               UniformParams.NumPhysicalAddressBits = AllocatedVirtualTexture->GetPageTableFormat() == EVTPageTableFormat::UInt16 ? 6 : 8; // See packing in PageTableUpdate.usf

<

<               // Create vertex factory.

<               VertexFactory = new FVirtualHeightfieldMeshVertexFactory(GetScene().GetFeatureLevel(), UniformParams);

<               VertexFactory->InitResource(FRHICommandListImmediate::Get());

<           }

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::DestroyRenderThreadResources()

< {

<   if (VertexFactory != nullptr)

<   {

<       VertexFactory->ReleaseResource();

<       delete VertexFactory;

<       VertexFactory = nullptr;

<   }

<

<   if (bCallbackRegistered)

<   {

<       GetRendererModule().RemoveAllVirtualTextureProducerDestroyedCallbacks(this);

<       bCallbackRegistered = false;

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::OnVirtualTextureDestroyedCB(const FVirtualTextureProducerHandle& InHandle, void* Baton)

< {

<   FVirtualHeightfieldMeshSceneProxy* SceneProxy = (FVirtualHeightfieldMeshSceneProxy*)Baton;

<   SceneProxy->DestroyRenderThreadResources();

<   SceneProxy->CreateRenderThreadResources();

< }

<

< FPrimitiveViewRelevance FVirtualHeightfieldMeshSceneProxy::GetViewRelevance(const FSceneView* View) const

< {

<   const bool bValid = AllocatedVirtualTexture != nullptr;

<   const bool bIsHiddenInEditor = bHiddenInEditor && View->Family->EngineShowFlags.Editor;

<

<   FPrimitiveViewRelevance Result;

<   Result.bDrawRelevance = bValid && IsShown(View) && !bIsHiddenInEditor;

<   Result.bShadowRelevance = bValid && IsShadowCast(View) && ShouldRenderInMainPass() &&!bIsHiddenInEditor;

<   Result.bDynamicRelevance = true;

<   Result.bStaticRelevance = false;

<   Result.bRenderInMainPass = ShouldRenderInMainPass();

<   Result.bUsesLightingChannels = GetLightingChannelMask() != GetDefaultLightingChannelMask();

<   Result.bRenderCustomDepth = ShouldRenderCustomDepth();

<   Result.bTranslucentSelfShadow = false;

<   MaterialRelevance.SetPrimitiveViewRelevance(Result);

<   Result.bVelocityRelevance = DrawsVelocity() && Result.bOpaque && Result.bRenderInMainPass;

<   return Result;

< }

<

< void FVirtualHeightfieldMeshSceneProxy::GetDynamicMeshElements(const TArray<const FSceneView*>& Views, const FSceneViewFamily& ViewFamily, uint32 VisibilityMap, FMeshElementCollector& Collector) const

< {

<   check(IsInRenderingThread());

<   check(AllocatedVirtualTexture != nullptr);

<

<   if (GVirtualHeightfieldMeshViewRendererExtension.IsInFrame())

<   {

<       // Can't add new work while bInFrame.

<       // In UE5 we need to AddWork()/SubmitWork() in two phases: InitViews() and InitViewsAfterPrepass()

<       // The main renderer hooks for that don't exist in UE5.0 and are only added in UE5.1

<       // That means that for UE5.0 we always hit this for shadow drawing and shadows will not be rendered.

<       // Not earlying out here can lead to crashes from buffers being released too soon.

<       return;

<   }

<

<   for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ViewIndex++)

<   {

<       if (VisibilityMap & (1 << ViewIndex))

<       {

<           if (!IsShadowCast(Views[ViewIndex]) && ViewFamily.Views[0] != Views[ViewIndex])

<           {

<               continue;

<           }

<           VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers = GVirtualHeightfieldMeshViewRendererExtension.AddWork(this, ViewFamily.Views[0], Views[ViewIndex]);

<

<           {

<               FMeshBatch& Mesh = Collector.AllocateMesh();

<               // Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bWireframe = ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bUseWireframeSelectionColoring = IsSelected();

<               Mesh.VertexFactory = VertexFactory;

<               Mesh.MaterialRenderProxy = Material;

<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

<               Mesh.Type = PT_TriangleList;

<               Mesh.DepthPriorityGroup = SDPG_World;

<               Mesh.bCanApplyViewModeOverrides = true;

<               Mesh.bUseForMaterial = true;

<               Mesh.CastShadow = true;

<               Mesh.bUseForDepthPass = true;

<

<               Mesh.Elements.SetNumZeroed(1);

<               {

<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

<

<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

<                   BatchElement.IndirectArgsOffset = 0;

<

<                   BatchElement.FirstIndex = 0;

<                   BatchElement.NumPrimitives = 0;

<                   BatchElement.MinVertexIndex = 0;

<                   BatchElement.MaxVertexIndex = 0;

<

<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

<

<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

<                   Parameters2.InstanceBuffer = Buffers.InstanceBufferSRV;

<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

<                   BatchElement.UserData = (void*)UserData;

<

<                   UserData->InstanceBufferSRV = Buffers.InstanceBufferSRV;

<

<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

<                   FSceneView const* MainView = ViewFamily.Views[0];

<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

<

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<                   // Support the freezerendering mode. Use any frozen view state for culling.

<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

<                   if (FrozenViewMatrices != nullptr)

<                   {

<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

<                   }

< #endif

<

<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

<               }

<

<               Collector.AddMesh(ViewIndex, Mesh);

<           }

< #pragma region S1_Engine_Shiyu

<           // for hole quad

<           {

<               FMeshBatch& Mesh = Collector.AllocateMesh();

<               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bUseWireframeSelectionColoring = IsSelected();

<               Mesh.VertexFactory = VertexFactory;

<               Mesh.MaterialRenderProxy = HoleMaterial;

<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

<               Mesh.Type = PT_TriangleList;

<               Mesh.DepthPriorityGroup = SDPG_World;

<               Mesh.bCanApplyViewModeOverrides = true;

<               Mesh.bUseForMaterial = true;

<               Mesh.CastShadow = true;

<               Mesh.bUseForDepthPass = true;

<

<               Mesh.Elements.SetNumZeroed(1);

<               {

<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

<

<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

<                   BatchElement.IndirectArgsOffset = 5 * sizeof(uint32);

<

<                   BatchElement.FirstIndex = 0;

<                   BatchElement.NumPrimitives = 0;

<                   BatchElement.MinVertexIndex = 0;

<                   BatchElement.MaxVertexIndex = 0;

<

<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

<

<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

<

<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

<                   Parameters2.InstanceBuffer = Buffers.HoleInstanceBufferSRV;

<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

<

<                   BatchElement.UserData = (void*)UserData;

<

<                   UserData->InstanceBufferSRV = Buffers.HoleInstanceBufferSRV;

<

<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

<                   FSceneView const* MainView = ViewFamily.Views[0];

<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

<

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<                   // Support the freezerendering mode. Use any frozen view state for culling.

<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

<                   if (FrozenViewMatrices != nullptr)

<                   {

<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

<                   }

< #endif

<

<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

<               }

<

<               Collector.AddMesh(ViewIndex, Mesh);

<           }

<       }

<

< #pragma endregion

<   }

< }

<

< bool FVirtualHeightfieldMeshSceneProxy::HasSubprimitiveOcclusionQueries() const

< {

<   return CVarVHMOcclusion.GetValueOnAnyThread() != 0;

< }

<

< const TArray<FBoxSphereBounds>* FVirtualHeightfieldMeshSceneProxy::GetOcclusionQueries(const FSceneView* View) const

< {

<   return (CVarVHMOcclusion.GetValueOnAnyThread() == 0 || OcclusionVolumes.Num() == 0) ? &DefaultOcclusionVolumes : &OcclusionVolumes;

< }

<

< void FVirtualHeightfieldMeshSceneProxy::BuildOcclusionVolumes(TArrayView<FVector2D> const& InMinMaxData, FIntPoint const& InMinMaxSize, TArrayView<int32> const& InMinMaxMips, int32 InNumLods)

< {

<   NumOcclusionLods = 0;

<   OcclusionGridSize = FIntPoint::ZeroValue;

<   OcclusionVolumes.Reset();

<

<   if (InNumLods > 0 && InMinMaxMips.Num() > 0)

<   {

<       NumOcclusionLods = FMath::Min(InNumLods, InMinMaxMips.Num());

<

<       const int32 BaseLod = InMinMaxMips.Num() - NumOcclusionLods;

<       OcclusionGridSize.X = FMath::Max(InMinMaxSize.X >> BaseLod, 1);

<       OcclusionGridSize.Y = FMath::Max(InMinMaxSize.Y >> BaseLod, 1);

<

<       OcclusionVolumes.Reserve(InMinMaxData.Num() - InMinMaxMips[BaseLod]);

<

<       for (int32 LodIndex = BaseLod; LodIndex < InMinMaxMips.Num(); ++LodIndex)

<       {

<           int32 SizeX = FMath::Max(InMinMaxSize.X >> LodIndex, 1);

<           int32 SizeY = FMath::Max(InMinMaxSize.Y >> LodIndex, 1);

<           int32 MinMaxDataIndex = InMinMaxMips[LodIndex];

<

<           for (int Y = 0; Y < SizeY; ++Y)

<           {

<               for (int X = 0; X < SizeX; ++X)

<               {

<                   FVector2D MinMaxU = FVector2D((float)X / (float)SizeX, (float)(X + 1) / (float)SizeX);

<                   FVector2D MinMaxV = FVector2D((float)Y / (float)SizeY, (float)(Y + 1) / (float)SizeY);

<                   FVector2D MinMaxZ = InMinMaxData[MinMaxDataIndex++];

<

<                   FVector Pos[8];

<                   Pos[0] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.X));

<                   Pos[1] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.X));

<                   Pos[2] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.X));

<                   Pos[3] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.X));

<                   Pos[4] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.Y));

<                   Pos[5] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.Y));

<                   Pos[6] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.Y));

<                   Pos[7] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.Y));

<

<                   const float ExpandOcclusion = 3.f;

<                   OcclusionVolumes.Add(FBoxSphereBounds(FBox(Pos, 8).ExpandBy(ExpandOcclusion)));

<               }

<           }

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::AcceptOcclusionResults(FSceneView const* View, TArray<bool>* Results, int32 ResultsStart, int32 NumResults)

< {

<   if (GOcclusionResetRequired)

<   {

<       GOcclusionResults.Reset();

<       GOcclusionResetRequired = false;

<   }

<

<   if (CVarVHMOcclusion.GetValueOnAnyThread() != 0 && Results != nullptr && NumResults > 1)

<   {

<       FOcclusionResults& OcclusionResults = GOcclusionResults.Emplace(FOcclusionResultsKey(this, View));

<       OcclusionResults.TextureSize = OcclusionGridSize;

<       OcclusionResults.NumTextureMips = NumOcclusionLods;

<       OcclusionResults.UploadData.Append(Results->GetData() + ResultsStart, NumResults);

<   }

< }

<

< namespace VirtualHeightfieldMesh

< {

<   /* Keep indirect args offsets in sync with VirtualHeightfieldMesh.usf. */

<   static const int32 IndirectArgsByteOffset_FinalCull = 0;

<

<

<   /** Shader structure used for tracking work queues in persistent wave style shaders. Keep in sync with VirtualHeightfieldMesh.ush. */

<   struct WorkerQueueInfo

<   {

<       uint32 Read;

<       uint32 Write;

<       int32 NumActive;

<   };

<

<   /** Final render instance description used by the DrawInstancedIndirect(). Keep in sync with VirtualHeightfieldMesh.ush. */

<   struct QuadRenderInstance

<   {

<       float UVTransform[3];

<       // uint32 AddressLevelPacked;

<       // float UVTransformPar[3];

<       // float Height;

<       // float UVTransformPar2[3];

<       // float Padding;

<       uint32 PhysicalAddress[3];

<       // uint32 Padding2;

<   };

<

<   /** Compute shader to initialize all buffers, including adding the lowest mip page(s) to the QuadBuffer. */

<   class FInitBuffersVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FInitBuffersVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FInitBuffersVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER(uint32, MaxLevel)

<           SHADER_PARAMETER(uint32, NumForceLoadLods)

<           SHADER_PARAMETER(uint32, PageTableFeedbackId)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<

<       END_SHADER_PARAMETER_STRUCT()

<

<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       // {

<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       // }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FInitBuffersVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitBuffersCS", SF_Compute);

<

<   /** Compute shader to traverse the virtual texture page table for a view and generate an array of quads to potentially render. */

<   class FCollectQuadsVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FCollectQuadsVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FCollectQuadsVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)

<           SHADER_PARAMETER(int32, MinMaxLevelOffset)

<           SHADER_PARAMETER_TEXTURE(Texture2D, LodBiasMinMaxTexture)

<           SHADER_PARAMETER_TEXTURE(Texture2D<float>, OcclusionTexture)

<           SHADER_PARAMETER(int32, OcclusionLevelOffset)

<           SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<           SHADER_PARAMETER(uint32, MaxLevel)

<           SHADER_PARAMETER(uint32, RVTMinLevel)

<           SHADER_PARAMETER(FVector4f, PageTableSize)

<           SHADER_PARAMETER(uint32, PageTableFeedbackId)

<           SHADER_PARAMETER(FVector4f, LodDistances)

<           SHADER_PARAMETER(float, LodBiasScale)

<           SHADER_PARAMETER(FVector3f, ViewOrigin)

<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])

<           SHADER_PARAMETER(FMatrix44f, UVToWorld)

<           SHADER_PARAMETER(FVector3f, UVToWorldScale)

<           SHADER_PARAMETER(uint32, QueueBufferSizeMask)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, RWQuadBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<       END_SHADER_PARAMETER_STRUCT()

<

<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       // {

<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       // }

<

<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<       {

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<           Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

< #pragma endregion

<       }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FCollectQuadsVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CollectQuadsCS", SF_Compute);

<

<   /** InitInstanceBuffer compute shader. */

<   class FInitInstanceBufferVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FInitInstanceBufferVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FInitInstanceBufferVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER(int32, NumIndices)

<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<       END_SHADER_PARAMETER_STRUCT()

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FInitInstanceBufferVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitInstanceBufferCS", SF_Compute);

<

<   /** CullInstances compute shader. */

<   class FCullInstancesVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FCullInstancesVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FCullInstancesVHM_CS, FGlobalShader);

<

<       class FReuseCullDim : SHADER_PERMUTATION_BOOL("REUSE_CULL");

<

<       using FPermutationDomain = TShaderPermutationDomain<FReuseCullDim>;

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)

<           SHADER_PARAMETER(int32, MinMaxLevelOffset)

<           SHADER_PARAMETER(uint32, RVTMinLevel)

<           SHADER_PARAMETER_TEXTURE(Texture2D, PageTableTexture)

<           SHADER_PARAMETER(FVector4f, PageTableSize)

<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])

<           SHADER_PARAMETER(FVector4f, PhysicalPageTransform)

<           SHADER_PARAMETER(uint32, NumPhysicalAddressBits)

<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>, QuadBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>, IndirectArgsBufferSRV)

<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWInstanceBuffer)

<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           RDG_BUFFER_ACCESS(IndirectArgsBuffer, ERHIAccess::IndirectArgs)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_TEXTURE(Texture2D, MaskTexture)

<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWHoleInstanceBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<       END_SHADER_PARAMETER_STRUCT()

<

<

<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<       {

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<           Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

< #pragma endregion

<       }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FCullInstancesVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CullInstancesCS", SF_Compute);

<

<

<   namespace V2

<   {

< //        class FFirstInitBuffers_CS : public FGlobalShader

< //        {

< //        public:

< //            DECLARE_GLOBAL_SHADER(FFirstInitBuffers_CS);

< //            SHADER_USE_PARAMETER_STRUCT(FFirstInitBuffers_CS, FGlobalShader);

< //

< //            BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

< //                SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, FinalQuadBuffer)

< //                SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

< //            END_SHADER_PARAMETER_STRUCT()

< //        };

< //        IMPLEMENT_GLOBAL_SHADER(FFirstInitBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "FirstInitBuffersCS", SF_Compute);

<

<       class FInitAllBuffers_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FInitAllBuffers_CS);

<           SHADER_USE_PARAMETER_STRUCT(FInitAllBuffers_CS, FGlobalShader);

<

<           class FClearVTCountDim : SHADER_PERMUTATION_BOOL("CLEAR_VT_COUNT");

<

<           using FPermutationDomain = TShaderPermutationDomain<FClearVTCountDim>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer1)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer2)

<               SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

<           END_SHADER_PARAMETER_STRUCT()

<

<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<           {

< #if VHM_ENABLE_STAT

<               Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

<           }

<       };

<       IMPLEMENT_GLOBAL_SHADER(FInitAllBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "InitAllBuffersCS", SF_Compute);

<

<

<       class FFillLevel4Quad_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FFillLevel4Quad_CS)

<           SHADER_USE_PARAMETER_STRUCT(FFillLevel4Quad_CS, FGlobalShader)

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<           END_SHADER_PARAMETER_STRUCT()

<       };

<       IMPLEMENT_GLOBAL_SHADER(FFillLevel4Quad_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "FillLevel4QuadCS", SF_Compute);

<

<

<       // class FCollectQuadsFromPreFrame_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectQuadsFromPreFrame_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectQuadsFromPreFrameCS", SF_Compute);

<       //

<

<       class FCollectSubdivideQuads_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FCollectSubdivideQuads_CS);

<           SHADER_USE_PARAMETER_STRUCT(FCollectSubdivideQuads_CS, FGlobalShader);

<

<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

<           class FWithFeedback : SHADER_PERMUTATION_BOOL("VHM_WITH_FEEDBACK");

<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim, FWithFeedback>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER(uint32, CurPassCalTime)

<               RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           END_SHADER_PARAMETER_STRUCT()

<

<       };

<       IMPLEMENT_GLOBAL_SHADER(FCollectSubdivideQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectSubdivideQuadsCS", SF_Compute);

<

<       // class FCollectMergeQuads_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectMergeQuads_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectMergeQuads_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      SHADER_PARAMETER(uint32, CurPassCalTime)

<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectMergeQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectMergeQuadsCS", SF_Compute);

<

<       // class FCollectRemainQuads_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectRemainQuads_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectRemainQuads_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER(uint32, RemainCS_DispatchArgsOffset)

<       //      SHADER_PARAMETER(uint32, CurPassCalTime)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectRemainQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectRemainQuadsCS", SF_Compute);

<       //

<       class FCullQuadsAndGenerateInstances_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS);

<           SHADER_USE_PARAMETER_STRUCT(FCullQuadsAndGenerateInstances_CS, FGlobalShader);

<

<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<               SHADER_PARAMETER_UAV(RWBuffer<uint>,                InstanceArgsBuffer)

<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    QuadInstanceBuffer)

<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    HoleQuadInstanceBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

<           END_SHADER_PARAMETER_STRUCT()

<

<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<           {

< #if VHM_ENABLE_STAT

<               Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

<           }

<       };

<       IMPLEMENT_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CullQuadsAndGenerateInstancesCS", SF_Compute);

<   };

<

<   /** Default Min/Max texture has the fixed maximum [0,1]. */

<   class FHeightMinMaxDefaultTexture : public FTexture

<   {

<   public:

<       virtual void InitRHI(FRHICommandListBase& RHICmdList) override

<       {

<           const FRHITextureCreateDesc Desc =

<               FRHITextureCreateDesc::Create2D(TEXT("VirtualHeightfieldMesh.MinMaxDefaultTexture"), 1, 1, PF_B8G8R8A8)

<               .SetFlags(ETextureCreateFlags::ShaderResource);

<

<           TextureRHI = RHICreateTexture(Desc);

<

<           // Write the contents of the texture.

<           uint32 DestStride;

<           FColor* DestBuffer = (FColor*)RHILockTexture2D(TextureRHI, 0, RLM_WriteOnly, DestStride, false);

<           *DestBuffer = FColor(0, 0, 255, 255);

<           RHIUnlockTexture2D(TextureRHI, 0, false);

<

<           // Create the sampler state RHI resource.

<           FSamplerStateInitializerRHI SamplerStateInitializer(SF_Point, AM_Clamp, AM_Clamp, AM_Clamp);

<           SamplerStateRHI = GetOrCreateSamplerState(SamplerStateInitializer);

<       }

<

<       virtual uint32 GetSizeX() const override { return 1; }

<       virtual uint32 GetSizeY() const override { return 1; }

<   };

<

<   /** Single global instance of default Min/Max texture. */

<   FTexture* GHeightMinMaxDefaultTexture = new TGlobalResource<FHeightMinMaxDefaultTexture>;

<

<   /** View matrices that can be frozen in freezerendering mode. */

<   struct FViewData

<   {

<       FVector ViewOrigin;

<       FMatrix ProjectionMatrix;

<       FConvexVolume ViewFrustum;

<       bool bViewFrozen;

<   };

<

<   /** Fill the FViewData from an FSceneView respecting the freezerendering mode. */

<   void GetViewData(FSceneView const* InSceneView, FViewData& OutViewData)

<   {

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<       const FViewMatrices* FrozenViewMatrices = InSceneView->State != nullptr ? InSceneView->State->GetFrozenViewMatrices() : nullptr;

<       if (FrozenViewMatrices != nullptr)

<       {

<           OutViewData.ViewOrigin = FrozenViewMatrices->GetViewOrigin();

<           OutViewData.ProjectionMatrix = FrozenViewMatrices->GetProjectionMatrix();

<           GetViewFrustumBounds(OutViewData.ViewFrustum, FrozenViewMatrices->GetViewProjectionMatrix(), true);

<           OutViewData.bViewFrozen = true;

<       }

<       else

< #endif

<       {

<           OutViewData.ViewOrigin = InSceneView->ViewMatrices.GetViewOrigin();

<           OutViewData.ProjectionMatrix = InSceneView->ViewMatrices.GetProjectionMatrix();

<           OutViewData.ViewFrustum = InSceneView->ViewFrustum;

<           OutViewData.bViewFrozen = fal

... [diff truncated to 80KB; full diff in vhm_diffs/82353.diff] ...
```

#### CL 88225 — 2024/07/18 — 郭智均

- **提交说明**：--bug=1029334 --user=郭智均 【野区场景】【副岛】地面碰撞与实际显示不符 https://www.tapd.cn/68880148/s/1503167
- **TAPD**：bug=1029334
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【野区场景】【副岛】地面碰撞与实际显示不符 https://www.tapd.cn/68880148/s/1503167

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (edit)

📄 查看 VHM 相关 diff（CL 88225）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#5 (text) ====

136,137c136,137
<   float2 XY = ((float2)Pos + LocalUV) * GeoToTexLevelOffsetInv * (float)(1u << TextureLevel);

<   float2 NormalizedPos = (XY * VHM.PageTableSize.zw);

---
>   // float2 XY = ((float2)Pos + LocalUV) * GeoToTexLevelOffsetInv * (float)(1u << TextureLevel);

>   // float2 NormalizedPos = (XY * VHM.PageTableSize.zw);

140a141,142
>   float2 XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1 << (uint)SampleLevel);

>   float2 NormalizedPos = (XY * VHM.PageTableSize.zw);

217c219
<       XY = ((float2)Pos + LocalUV) * GeoToTexLevelOffsetInv * (float)(1u << TextureLevel);

---
>       XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1 << (uint)SampleLevel);

228c230
<   Uniform.vPageBorderSize -= .5f * VHM.PhysicalTextureSize.y; // Half texel offset is used in VT write and in sampling because we want texel locations to match landscape vertices.

---
>   // Uniform.vPageBorderSize -= .5f * VHM.PhysicalTextureSize.y; // Half texel offset is used in VT write and in sampling because we want texel locations to match landscape vertices.
```

#### CL 93165 — 2024/07/22 — 郭智均

- **提交说明**：--bug=1029334 --user=郭智均 【野区场景】【副岛】地面碰撞与实际显示不符 https://www.tapd.cn/68880148/s/1529773
- **TAPD**：bug=1029334
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【野区场景】【副岛】地面碰撞与实际显示不符 https://www.tapd.cn/68880148/s/1529773

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (integrate)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 93165）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#6 (text) ====

141c141
<   float2 XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1 << (uint)SampleLevel);

---
>   float2 XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1u << (uint)SampleLevel);

230c230
<   // Uniform.vPageBorderSize -= .5f * VHM.PhysicalTextureSize.y; // Half texel offset is used in VT write and in sampling because we want texel locations to match landscape vertices.

---
>   Uniform.vPageBorderSize -= .5f * VHM.PhysicalTextureSize.y; // Half texel offset is used in VT write and in sampling because we want texel locations to match landscape vertices.


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#15 (unicode) ====

1,2791c1,2791
< // Copyright Epic Games, Inc. All Rights Reserved.
<
< #include "VirtualHeightfieldMeshSceneProxy.h"
<
< #include "DataDrivenShaderPlatformInfo.h"
< #include "EngineModule.h"
< #include "Engine/Engine.h"
< #include "Engine/Texture2D.h"
< #include "GlobalRenderResources.h"
< #include "GlobalShader.h"
< #include "HeightfieldMaskTexture.h"
< #include "HeightfieldMinMaxTexture.h"
< #include "HLSLTypeAliases.h"
< #include "MaterialDomain.h"
< #include "Materials/Material.h"
< #include "Materials/MaterialRenderProxy.h"
< #include "PrimitiveViewRelevance.h"
< #include "RenderCaptureInterface.h"
< #include "RHIStaticStates.h"
< #include "RenderGraphUtils.h"
< #include "RHIGPUReadback.h"
< #include "SceneInterface.h"
< #include "SystemTextures.h"
< #include "TextureResource.h"
< #include "VirtualHeightfieldMeshComponent.h"
< #include "VirtualHeightfieldMeshVertexFactory.h"
< #include "VT/RuntimeVirtualTexture.h"
< #include "VT/VirtualTextureFeedbackBuffer.h"
<
<
< DECLARE_STATS_GROUP(TEXT("VirtualHeightfieldMesh"), STATGROUP_VirtualHeightfieldMesh, STATCAT_Advanced);
< DECLARE_CYCLE_STAT(TEXT("VirtualHeightfieldMesh SubmitWork"), STAT_VirtualHeightfieldMesh_SubmitWork, STATGROUP_VirtualHeightfieldMesh);
<
< DECLARE_LOG_CATEGORY_EXTERN(LogVirtualHeightfieldMesh, Warning, All);
< DEFINE_LOG_CATEGORY(LogVirtualHeightfieldMesh);
<
< static TAutoConsoleVariable<float> CVarVHMLodScale(
<   TEXT("r.VHM.LodScale"),
<   1.f,
<   TEXT("Global LOD scale applied for Virtual Heightfield Mesh."),
<   ECVF_RenderThreadSafe
< );
<
< // We disable View.LODDistanceFactor by default.
< // When it is set according to GCalcLocalPlayerCachedLODDistanceFactor in ULocalPlayer we end up with double couting of the FOV scale.
< // Ideally we would remove the calculation in ULocalPlayer and View.LODDistanceFactor would be only for view specific adjustments (screen captures etc.)
< // However the removal of the code in ULocalPlayer could have a big impact on any preexisting data in any project.
< static TAutoConsoleVariable<int32> CVarVHMEnableViewLodFactor(
<   TEXT("r.VHM.EnableViewLodFactor"),
<   0,
<   TEXT("Enable the View.LODDistanceFactor.")
<   TEXT("This is disabled by default to avoid an issue where FOV is double counted when calculating Lods.")
<   TEXT("See comment in code for more information."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMOcclusion(
<   TEXT("r.VHM.Occlusion"),
<   1,
<   TEXT("Enable occlusion queries."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMMaxRenderItems(
<   TEXT("r.VHM.MaxRenderInstances"),
<   1024 * 64,
<   TEXT("Size of buffers used to collect render instances."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMMaxFeedbackItems(
<   TEXT("r.VHM.MaxFeedbackItems"),
<   1024 * 4 * 10, // pre node write 10 time
<   TEXT("Size of buffer used by virtual texture feedback."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMMaxPersistentQueueItems(
<   TEXT("r.VHM.MaxPersistentQueueItems"),
<   1024 * 64,
<   TEXT("Size of queue used in the collect pass. This is rounded to the nearest power of 2."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMCollectPassWavefronts(
<   TEXT("r.VHM.CollectPassWavefronts"),
<   1,
<   TEXT("Number of wavefronts to use for collect pass."),
<   ECVF_RenderThreadSafe
< );
<
< static TAutoConsoleVariable<int32> CVarVHMVersion(
<   TEXT("r.VHM.Version"),
<   3,
<   TEXT("Version Of VHM"),
<   ECVF_RenderThreadSafe
< );
<
< #pragma region S1_Engine_Shiyu
< #if UE_BUILD_SHIPPING || UE_BUILD_TEST
< #define VHM_ENABLE_STAT 0
< #else
< #define VHM_ENABLE_STAT 1
< #endif
<
< #if VHM_ENABLE_STAT
< #include "Stats/Stats2.h"
< #include "Stats/StatsMisc.h"
<
< DECLARE_STATS_GROUP(TEXT("VHM"), STATGROUP_VHM, STATCAT_Advanced);
<
< DECLARE_DWORD_COUNTER_STAT(TEXT("BeforeCullInstances"), STAT_VHM_BeforeCullInstances, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawTriangles"), STAT_VHM_DrawTriangles, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-ALL"), STAT_VHM_DrawInstancesALL, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Opacity"), STAT_VHM_DrawOpacityInstances, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Mask"), STAT_VHM_DrawMaskInstances, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD0"), STAT_VHM_DrawInstancesLOD0, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD1"), STAT_VHM_DrawInstancesLOD1, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD2"), STAT_VHM_DrawInstancesLOD2, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD3"), STAT_VHM_DrawInstancesLOD3, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD4"), STAT_VHM_DrawInstancesLOD4, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD5"), STAT_VHM_DrawInstancesLOD5, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD6"), STAT_VHM_DrawInstancesLOD6, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD7"), STAT_VHM_DrawInstancesLOD7, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD8"), STAT_VHM_DrawInstancesLOD8, STATGROUP_VHM)
< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD9"), STAT_VHM_DrawInstancesLOD9, STATGROUP_VHM)
<
<
< static TAutoConsoleVariable<int32> CVarVHMEnableStat(
<   TEXT("r.VHM.StatEnable"),
<   0,
<   TEXT("Whether VHM open Stat."),
<   ECVF_RenderThreadSafe
< );
< #endif
<
< #pragma endregion
<
< static constexpr int32 IndirectArgsCount = 10;
< static constexpr int32 IndirectArgsByteSize = 4 * sizeof(uint32) * IndirectArgsCount;
< static constexpr int32 MergeDispatchArgsOffset = 5;
< #pragma region S1_Engine_Shiyu
< // #if VHM_ENABLE_STAT
< static constexpr int32 MaxStatCount = 64;
< static constexpr int32 StatBufferByteSize = sizeof(uint32) * MaxStatCount;
< // #endif
< #pragma endregion
<
<
< namespace VirtualHeightfieldMesh
< {
<   /** Buffers filled by GPU culling used by the Virtual Heightfield Mesh final draw call. */
<   struct FDrawInstanceBuffers
<   {
<       /* Culled instance buffer. */
<       FBufferRHIRef InstanceBuffer;
<       FUnorderedAccessViewRHIRef InstanceBufferUAV;
<       FShaderResourceViewRHIRef InstanceBufferSRV;
<
<       /* IndirectArgs buffer for final DrawInstancedIndirect. */
<       FBufferRHIRef IndirectArgsBuffer;
<       FUnorderedAccessViewRHIRef IndirectArgsBufferUAV;
<
< #pragma region S1_Engine_Shiyu
<       /* Culled hold instance buffer */
<       FBufferRHIRef HoleInstanceBuffer;
<       FUnorderedAccessViewRHIRef HoleInstanceBufferUAV;
<       FShaderResourceViewRHIRef HoleInstanceBufferSRV;
<
< //#if VHM_ENABLE_STAT
<       static constexpr uint32 MaxReadBackSize = 4;
<       /** For Stat  */
<       TArray<TUniquePtr<FRHIGPUBufferReadback>> StatBufferReadBacks;
< //#endif
< #pragma endregion
<   };
<
<   /** Initialize the FDrawInstanceBuffers objects. */
<   void InitializeInstanceBuffers(FRHICommandListImmediate& InRHICmdList, FDrawInstanceBuffers& InBuffers);
<
<   /** Release the FDrawInstanceBuffers objects. */
<   void ReleaseInstanceBuffers(FDrawInstanceBuffers& InBuffers)
<   {
<       InBuffers.InstanceBuffer.SafeRelease();
<       InBuffers.InstanceBufferUAV.SafeRelease();
<       InBuffers.InstanceBufferSRV.SafeRelease();
<       InBuffers.IndirectArgsBuffer.SafeRelease();
<       InBuffers.IndirectArgsBufferUAV.SafeRelease();
< #pragma region S1_Engine_Shiyu
<       InBuffers.HoleInstanceBuffer.SafeRelease();
<       InBuffers.HoleInstanceBufferUAV.SafeRelease();
<       InBuffers.HoleInstanceBufferSRV.SafeRelease();
< #if VHM_ENABLE_STAT
<       InBuffers.StatBufferReadBacks.Empty();
< #endif
< #pragma endregion
<   }
<
<   namespace V2
<   {
<       struct FInnerBuffers
<       {
<           // // for ps
<           // // - use to draw quad by default material
<           // FBufferRHIRef QuadInstanceArgsBuffer;
<           // FUnorderedAccessViewRHIRef QuadInstanceArgsBufferUAV;
<           // FBufferRHIRef QuadInstanceBuffer;
<           // FUnorderedAccessViewRHIRef QuadInstanceBufferUAV;
<           // FShaderResourceViewRHIRef QuadInstanceBufferSRV;
<           // // - use to draw quad by hole material
<           // FBufferRHIRef HoleQuadInstanceArgsBuffer;
<           // FUnorderedAccessViewRHIRef HoleQuadInstanceArgsBufferUAV;
<           // FBufferRHIRef HoleQuadInstanceBuffer;
<           // FUnorderedAccessViewRHIRef HoleQuadInstanceBufferUAV;
<           // FShaderResourceViewRHIRef HoleQuadInstanceBufferSRV;
<
<           int32 CalTime = -1;
<           // use to compure shader
<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};
<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};
<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};
<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};
<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};
<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};
<
<           FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
<           }
<           FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
<           }
<
<           FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
<           }
<           FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
<           }
<       };
<
<       void InitializeInnerBuffers(FRHICommandListImmediate& RHICmdList, FInnerBuffers& InBuffers)
<       {
<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();
<           const TCHAR* FinalQuadName[2] = {
<               TEXT("VHM.FinalQuadBuffer_0"),
<               TEXT("VHM.FinalQuadBuffer_1")};
<           const TCHAR* FinalQuadArgsName[2] = {
<               TEXT("VHM.FinalQuadArgsBuffer_0"),
<               TEXT("VHM.FinalQuadArgsBuffer_1")};
<
<           for (int i = 0; i < 2; ++i)
<           {
<               InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(
<                   FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),
<                   FinalQuadArgsName[i]
<               );
<               InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(
<                   FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)
<
<                   ,
<                   FinalQuadName[i]
<               );
<           }
<       }
<
<       void ReleaseInnerBuffers(FInnerBuffers& InBuffers)
<       {
<           InBuffers.CalTime = -1;
<           for(int i = 0; i < 2; ++i)
<           {
<               InBuffers.FinalQuadArgsBuffer[i].SafeRelease();
<               InBuffers.FinalQuadBuffer[i].SafeRelease();
<           }
<
<       }
<   }
<
< }
<
< struct FOcclusionResults
< {
<   FTexture2DRHIRef OcclusionTexture;
<   FIntPoint TextureSize;
<   int32 NumTextureMips;
<   TArray<bool> UploadData;
< };
<
< struct FOcclusionResultsKey
< {
<   FVirtualHeightfieldMeshSceneProxy const* Proxy;
<   FSceneView const* View;
<
<   FOcclusionResultsKey(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InView)
<       : Proxy(InProxy)
<       , View(InView)
<   {
<   }
<
<   friend inline uint32 GetTypeHash(const FOcclusionResultsKey& InKey)
<   {
<       return HashCombine(GetTypeHash(InKey.View), GetTypeHash(InKey.Proxy));
<   }
<
<   friend bool operator==(const FOcclusionResultsKey& A, const FOcclusionResultsKey& B)
<   {
<       return A.View == B.View && A.Proxy == B.Proxy;
<   }
< };
<
<
<
< /** Global map for occlusion result. */
< TMap< FOcclusionResultsKey, FOcclusionResults > GOcclusionResults;
< bool GOcclusionResetRequired = false;
<
< namespace VirtualHeightfieldMesh
< {
<   /** Calculate distances used for LODs in a given view for a given scene proxy. */
<   FVector4f CalculateLodRanges(FSceneView const* InView, FVirtualHeightfieldMeshSceneProxy const* InProxy)
<   {
<       const uint32 MaxLevel = FMath::Max(InProxy->RVTMaxLevel - InProxy->Lod0LevelBias, 0);
<       const float Lod0UVSize = 1.f / (float)(1 << MaxLevel);
<       const FVector2D Lod0WorldSize = FVector2D(InProxy->UVToWorldScale.X, InProxy->UVToWorldScale.Y) * Lod0UVSize; // LWC_TODO: precision loss
<       const float Lod0WorldRadius = Lod0WorldSize.Size();
<       const float ScreenMultiple = FMath::Max(0.5f * InView->ViewMatrices.GetProjectionMatrix().M[0][0], 0.5f * InView->ViewMatrices.GetProjectionMatrix().M[1][1]);
<       const float Lod0Distance = Lod0WorldRadius * ScreenMultiple / InProxy->Lod0ScreenSize;
<       const float ViewLodDistanceFactor = CVarVHMEnableViewLodFactor.GetValueOnRenderThread() == 0 ? 1.f : InView->LODDistanceFactor;
<       const float LodScale = ViewLodDistanceFactor * CVarVHMLodScale.GetValueOnRenderThread();
<
<       return FVector4f(Lod0Distance, InProxy->Lod0Distribution, InProxy->LodDistribution, LodScale);
<   }
<
<
<   namespace V2
<   {
<       BEGIN_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters,)
<           SHADER_PARAMETER(FVector3f,         ViewOrigin)
<           SHADER_PARAMETER(uint32,            OutBufferSizeMask)
<           SHADER_PARAMETER(FVector3f,         UVToWorldScale)
<           SHADER_PARAMETER(uint32,            FinalQuadBufferSizeMask)
<           SHADER_PARAMETER_ARRAY(FVector4f,   FrustumPlanes, [5])
<           SHADER_PARAMETER(FMatrix44f,        UVToWorld)
<           SHADER_PARAMETER(FVector4f,         LodDistances)
<           SHADER_PARAMETER(uint32,            MaxLevel)
<           SHADER_PARAMETER(uint32,            RVTMinLevel)
<           SHADER_PARAMETER(uint32,            PageTableFeedbackId)
<           SHADER_PARAMETER(uint32,            NumPhysicalAddressBits)
<           SHADER_PARAMETER(FVector4f,         PageTableSize)
<           SHADER_PARAMETER(FVector4f,         PhysicalPageTransform)
<           SHADER_PARAMETER(uint32,            QuadInstanceBufferSizeMask)
<           SHADER_PARAMETER(uint32,            NumIndices)
<           SHADER_PARAMETER(uint32,            MaxArgsCount)
<           SHADER_PARAMETER(uint32,            MaxStatCount)
<           SHADER_PARAMETER(uint32,            MergeDispatchArgsOffset)
<       END_UNIFORM_BUFFER_STRUCT()
<
<       IMPLEMENT_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters, "VHMParam")
<
<       struct FVolatileBuffers
<       {
<           FVHMCSSharedParameters* VHMParameter=nullptr;
<           TRDGUniformBufferRef<FVHMCSSharedParameters> VHMParameterUBuffer;
<           TArray<FRDGBufferRef, TFixedAllocator<2>> ArgsBuffer{nullptr, nullptr};
<           TArray<FRDGBufferRef, TFixedAllocator<2>> SubdivideQuadBuffer{nullptr, nullptr};
<           TArray<FRDGBufferRef, TFixedAllocator<2>> MergeQuadBuffer{nullptr, nullptr};
<
<
<           struct FSRVAndUAV
<           {
<               FRDGBufferSRVRef SRV = nullptr;
<               FRDGBufferUAVRef UAV = nullptr;
<               void Create(FRDGBuilder& GraphBuilder, FRDGBufferRef Buffer)
<               {
<                   EPixelFormat Format = uint32(Buffer->Desc.Usage & EBufferUsageFlags::DrawIndirect) != 0 ? PF_R32_UINT : PF_R32G32B32A32_UINT;
<                   SRV = GraphBuilder.CreateSRV(Buffer, Format);
<                   UAV = GraphBuilder.CreateUAV(Buffer, Format);
<               }
<           };
<           TArray<FSRVAndUAV, TFixedAllocator<2>> ArgsViews{{}, {}};
<           TArray<FSRVAndUAV, TFixedAllocator<2>> SubdivideViews{{}, {}};
<           TArray<FSRVAndUAV, TFixedAllocator<2>> MergeViews{{}, {}};
<
<           FRHITexture* PageTableTexture = nullptr;
<           FRHITexture* MaskTexture = nullptr;
<           FRHIShaderResourceView* HeightTexture = nullptr;
<           FRHITexture* HeightMinMaxTexture = nullptr;
<
< //#if VHM_ENABLE_STAT
<           FRDGBufferRef StatBuffer;
<           FRDGBufferUAVRef StatBufferUAV;
< //#endif
<       };
<   }
< }
<
< /** Renderer extension to manage the buffer pool and add hooks for GPU culling passes. */
< class FVirtualHeightfieldMeshRendererExtension : public FRenderResource
< {
< public:
<   FVirtualHeightfieldMeshRendererExtension()
<       : bInFrame(false)
<       , DiscardId(0)
<   {}
<
<   virtual ~FVirtualHeightfieldMeshRendererExtension()
<   {}
<
<   /** Call once to register this extension. */
<   void RegisterExtension();
<
<   /** Are we inside a BeginFrame()/EndFrame() scope? */
<   bool IsInFrame() { return bInFrame; }
<
<   /** Call once per frame for each mesh/view that has relevance. This allocates the buffers to use for the frame and adds the work to fill the buffers to the queue. */
<   VirtualHeightfieldMesh::FDrawInstanceBuffers& AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView);
<   /** Submit all the work added by AddWork(). The work fills all of the buffers ready for use by the referencing mesh batches. */
<   void SubmitWork(FRDGBuilder& GraphBuilder);
<
<   void InitVolatileBuffers(FRDGBuilder& GraphBuilder, int WorkIndex, VirtualHeightfieldMesh::V2::FVolatileBuffers& VolatileBuffers);
<
<   // void SubmitWork_V2(FRDGBuilder& GraphBuilder);
<
<   void SubmitWork_V3(FRDGBuilder& GraphBuilder);
<
< #pragma region S1_Engine_Shiyu
< #if VHM_ENABLE_STAT
<   void CollectStat();
< #endif
< #pragma endregion
<
< protected:
<   //~ Begin FRenderResource Interface
<   virtual void ReleaseRHI() override;
<   //~ End FRenderResource Interface
<
< private:
<   /** Called by renderer at start of render frame. */
<   void BeginFrame(FRDGBuilder& GraphBuilder);
<   /** Called by renderer at end of render frame. */
<   void EndFrame(FRDGBuilder& GraphBuilder);
<   void EndFrame();
<
<
<   /** Flag for frame validation. */
<   bool bInFrame;
<
<   /** Buffers to fill. Resources can persist between frames to reduce allocation cost, but contents don't persist. */
<   TArray<VirtualHeightfieldMesh::FDrawInstanceBuffers> Buffers;
<   TArray<VirtualHeightfieldMesh::V2::FInnerBuffers> InnerBuffers;
<   /** Per buffer frame time stamp of last usage. */
<   TArray<uint32> DiscardIds;
<   /** Current frame time stamp. */
<   uint32 DiscardId;
<
<   /** Arrary of uniqe scene proxies to render this frame. */
<   TArray<FVirtualHeightfieldMeshSceneProxy const*> SceneProxies;
<   /** Arrary of unique main views to render this frame. */
<   TArray<FSceneView const*> MainViews;
<   /** Arrary of unique culling views to render this frame. */
<   TArray<FSceneView const*> CullViews;
<
<   /** Key for each buffer we need to generate. */
<   struct FWorkDesc
<   {
<       int32 ProxyIndex;
<       int32 MainViewIndex;
<       int32 CullViewIndex;
<       int32 BufferIndex;
<   };
<
<   /** Keys specifying what to render. */
<   TArray<FWorkDesc> WorkDescs;
<
<   /** Sort predicate for FWorkDesc. When rendering we want to batch work by proxy, then by main view. */
<   struct FWorkDescSort
<   {
<       uint32 SortKey(FWorkDesc const& WorkDesc) const
<       {
<           return (WorkDesc.ProxyIndex << 24) | (WorkDesc.MainViewIndex << 16) | (WorkDesc.CullViewIndex << 8) | WorkDesc.BufferIndex;
<       }
<
<       bool operator()(FWorkDesc const& A, FWorkDesc const& B) const
<       {
<           return SortKey(A) < SortKey(B);
<       }
<   };
<
<   // all vhm use one feedback buffer;
<   FRDGBufferRef VTFeedbackBuf;
<   FRDGBufferUAVRef VTFeedbackBufUAV;
< };
<
< /** Single global instance of the VirtualHeightfieldMesh renderer extension. */
< TGlobalResource< FVirtualHeightfieldMeshRendererExtension > GVirtualHeightfieldMeshViewRendererExtension;
<
< void FVirtualHeightfieldMeshRendererExtension::RegisterExtension()
< {
<   static bool bInit = false;
<   if (!bInit && GEngine)
<   {
<       GEngine->GetPreRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::BeginFrame);
<       GEngine->GetPostRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::EndFrame);
<       bInit = true;
<   }
< }
<
< void FVirtualHeightfieldMeshRendererExtension::ReleaseRHI()
< {
<   Buffers.Empty();
< }
<
< VirtualHeightfieldMesh::FDrawInstanceBuffers& FVirtualHeightfieldMeshRendererExtension::AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView)
< {
<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.
<   if (!ensure(!bInFrame))
<   {
<       EndFrame();
<   }
<
<   // Create workload
<   FWorkDesc WorkDesc;
<   WorkDesc.ProxyIndex = SceneProxies.AddUnique(InProxy);
<   WorkDesc.MainViewIndex = MainViews.AddUnique(InMainView);
<   WorkDesc.CullViewIndex = CullViews.AddUnique(InCullView);
<   WorkDesc.BufferIndex = -1;
<
<   // Check for an existing duplicate
<   for (FWorkDesc& It : WorkDescs)
<   {
<       if (It.ProxyIndex == WorkDesc.ProxyIndex && It.MainViewIndex == WorkDesc.MainViewIndex && It.CullViewIndex == WorkDesc.CullViewIndex && It.BufferIndex != -1)
<       {
<           WorkDesc.BufferIndex = It.BufferIndex;
<           break;
<       }
<   }
<
<   // Try to recycle a buffer
<   if (WorkDesc.BufferIndex == -1)
<   {
<       for (int32 BufferIndex = 0; BufferIndex < Buffers.Num(); BufferIndex++)
<       {
<           if (DiscardIds[BufferIndex] < DiscardId)
<           {
<               DiscardIds[BufferIndex] = DiscardId;
<               WorkDesc.BufferIndex = BufferIndex;
<               WorkDescs.Add(WorkDesc);
<               break;
<           }
<       }
<   }
<
<   // Allocate new buffer if necessary
<   if (WorkDesc.BufferIndex == -1)
<   {
<       DiscardIds.Add(DiscardId);
<       WorkDesc.BufferIndex = Buffers.AddDefaulted();
<       InnerBuffers.AddDefaulted(); // index is equal to BufferIndex
<       WorkDescs.Add(WorkDesc);
<       VirtualHeightfieldMesh::InitializeInstanceBuffers(GetImmediateCommandList_ForRenderCommand(), Buffers[WorkDesc.BufferIndex]);
<       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(GetImmediateCommandList_ForRenderCommand(), InnerBuffers[WorkDesc.BufferIndex]);
<   }
<
<   return Buffers[WorkDesc.BufferIndex];
< }
<
< void FVirtualHeightfieldMeshRendererExtension::BeginFrame(FRDGBuilder& GraphBuilder)
< {
<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.
<   if (!ensure(!bInFrame))
<   {
<       EndFrame();
<   }
<   bInFrame = true;
<
< #pragma region S1_Engine_Shiyu
< #if VHM_ENABLE_STAT
<   CollectStat();
< #endif
< #pragma endregion
<
<   if (WorkDescs.Num() > 0)
<   {
<       uint32 VHMVersion = CVarVHMVersion.GetValueOnRenderThread();
<       if (VHMVersion == 1)
<       {
<           SubmitWork(GraphBuilder);
<       }
<       else if(VHMVersion == 2)
<       {
<           // SubmitWork_V2(GraphBuilder);
<       }
<       else
<       {
<           SubmitWork_V3(GraphBuilder);
<       }
<   }
< }
<
< void FVirtualHeightfieldMeshRendererExtension::EndFrame()
< {
<   ensure(bInFrame);
<   bInFrame = false;
<
<   SceneProxies.Reset();
<   MainViews.Reset();
<   CullViews.Reset();
<   WorkDescs.Reset();
<
<   // Clean the buffer pool
<   DiscardId++;
<
<   for (int32 Index = 0; Index < DiscardIds.Num();)
<   {
<       if (DiscardId - DiscardIds[Index] > 4u)
<       {
<           VirtualHeightfieldMesh::ReleaseInstanceBuffers(Buffers[Index]);
<           VirtualHeightfieldMesh::V2::ReleaseInnerBuffers(InnerBuffers[Index]);
<           Buffers.RemoveAtSwap(Index);
<           InnerBuffers.RemoveAtSwap(Index);
<           DiscardIds.RemoveAtSwap(Index);
<       }
<       else
<       {
<           ++Index;
<       }
<   }
<
<   GOcclusionResetRequired = true;
< }
<
< void FVirtualHeightfieldMeshRendererExtension::EndFrame(FRDGBuilder& GraphBuilder)
< {
<   EndFrame();
< }
<
< const static FName NAME_VirtualHeightfieldMesh(TEXT("VirtualHeightfieldMesh"));
<
< FVirtualHeightfieldMeshSceneProxy::FVirtualHeightfieldMeshSceneProxy(UVirtualHeightfieldMeshComponent* InComponent)
<   : FPrimitiveSceneProxy(InComponent, NAME_VirtualHeightfieldMesh)
<   , bHiddenInEditor(InComponent->GetHiddenInEditor())
<   , RuntimeVirtualTexture(InComponent->GetVirtualTexture())
<   , HeightMinMaxTexture(nullptr)
<   , LodBiasTexture(nullptr)
<   , LodBiasMinMaxTexture(nullptr)
< #pragma region S1_Engine_Shiyu
<   , MaskTexture(nullptr)
< #pragma endregion
<   , AllocatedVirtualTexture(nullptr)
<   , bCallbackRegistered(false)
<   , NumQuadsPerTileOfTwo(InComponent->GetNumQuadPerTileOfTwo()) // (1 << 4) * (1 << 4)
<   , VertexFactory(nullptr)
<   , Lod0ScreenSize(InComponent->GetLod0ScreenSize())
<   , Lod0Distribution(InComponent->GetLod0Distribution())
<   , LodDistribution(InComponent->GetLodDistribution())
<   , LodBiasScale(InComponent->GetLodBiasScale())
<   , NumForceLoadLods(InComponent->GetNumForceLoadLods())
<   , NumOcclusionLods(0)
<   , ExtSubdivisionLevel(InComponent->GetExtSubdivisionLevel())
<   , OcclusionGridSize(0, 0)
<   , RVTMaxLevel(0)
<   , Lod0LevelBias(InComponent->GetLod0LevelBias())
< {
<   // maybe not in RenderThread
<   // GVirtualHeightfieldMeshViewRendererExtension.RegisterExtension();
<
<   // They have some LOD, but considered static as the LODs (are intended to) represent the same static surface.
<   bHasDeformableMesh = false;
<
<   UMaterialInterface* ComponentMaterial = InComponent->GetMaterial();
<   const bool bValidMaterial = ComponentMaterial != nullptr && ComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);
<   Material = bValidMaterial ? ComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();
<   MaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());
<
<   const FTransform VirtualTextureTransform = InComponent->GetVirtualTextureTransform();
<
<   UVToWorldScale = VirtualTextureTransform.GetScale3D();
<   UVToWorld = VirtualTextureTransform.ToMatrixWithScale();
<
<   WorldToUV = UVToWorld.Inverse();
<   WorldToUVTransposeAdjoint = WorldToUV.TransposeAdjoint();
<
<   // UVToLocal will be initialized in OnTransformChanged() called immediately after construction.
<   UVToLocal = FMatrix::Identity;
<
<   UHeightfieldMinMaxTexture* HeightfieldMinMaxTexture = InComponent->GetMinMaxTexture();
<   if (HeightfieldMinMaxTexture != nullptr)
<   {
<       HeightMinMaxTexture = HeightfieldMinMaxTexture->Texture;
<       BuildOcclusionVolumes(HeightfieldMinMaxTexture->TextureData, HeightfieldMinMaxTexture->TextureDataSize, HeightfieldMinMaxTexture->TextureDataMips, InComponent->GetNumOcclusionLods());
<
<       LodBiasTexture = HeightfieldMinMaxTexture->LodBiasTexture;
<       LodBiasMinMaxTexture = HeightfieldMinMaxTexture->LodBiasMinMaxTexture;
<   }
<
< #pragma region S1_Engine_Shiyu
<   UMaterialInterface* HoleComponentMaterial = InComponent->GetHoleMaterial();
<   const bool bValidHoleMaterial = HoleComponentMaterial != nullptr && HoleComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);
<   HoleMaterial = bValidHoleMaterial ? HoleComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();
<   HoleMaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());
<
<   UHeightfieldMaskTexture* HeightfieldMaskTexture = InComponent->GetMaskTexture();
<   if (HeightfieldMaskTexture)
<   {
<       MaskTexture = HeightfieldMaskTexture->Texture;
<   }
< #pragma endregion
< }
<
<
< void FVirtualHeightfieldMeshSceneProxy::RegisterExternal()
< {
<   GVirtualHeightfieldMeshViewRendererExtension.RegisterExtension();
< }
<
< SIZE_T FVirtualHeightfieldMeshSceneProxy::GetTypeHash() const
< {
<   static size_t UniquePointer;
<   return reinterpret_cast<size_t>(&UniquePointer);
< }
<
< uint32 FVirtualHeightfieldMeshSceneProxy::GetMemoryFootprint() const
< {
<   return(sizeof(*this) + FPrimitiveSceneProxy::GetAllocatedSize());
< }
<
< void FVirtualHeightfieldMeshSceneProxy::OnTransformChanged()
< {
<   UVToLocal = UVToWorld * GetLocalToWorld().Inverse();
<
<   // Setup a default occlusion volume array containing just the primitive bounds.
<   // We use this if disabling the full set of occlusion volumes.
<   DefaultOcclusionVolumes.Reset();
<   DefaultOcclusionVolumes.Add(GetBounds());
< }
<
< void FVirtualHeightfieldMeshSceneProxy::CreateRenderThreadResources()
< {
<   if (RuntimeVirtualTexture != nullptr)
<   {
<       if (!bCallbackRegistered)
<       {
<           GetRendererModule().AddVirtualTextureProducerDestroyedCallback(RuntimeVirtualTexture->GetProducerHandle(), &OnVirtualTextureDestroyedCB, this);
<           bCallbackRegistered = true;
<       }
<
<       if (RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight)
<       {
<           AllocatedVirtualTexture = RuntimeVirtualTexture->GetAllocatedVirtualTexture();
<
<
<           if (AllocatedVirtualTexture != nullptr)
<           {
<               uint32 TileSize = FMath::FloorLog2(RuntimeVirtualTexture->GetTileSize());
<               // check(TileSize + ExtSubdivisionLevel >= NumQuadsPerTileOfTwo);
<               NumQuadsPerTileOfTwo = FMath::Min(NumQuadsPerTileOfTwo, TileSize + ExtSubdivisionLevel - 1);
<               NumInstanceVertexSide = 1 << (TileSize + ExtSubdivisionLevel - NumQuadsPerTileOfTwo);
<               RVTMaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;
<               // Gather vertex factory uniform parameters.
<               FVirtualHeightfieldMeshVertexFactoryParameters UniformParams;
<               UniformParams.PageTableTexture = AllocatedVirtualTexture->GetPageTableTexture(0);
<               UniformParams.HeightTexture = AllocatedVirtualTexture->GetPhysicalTextureSRV(0, false);
<               UniformParams.HeightSampler = TStaticSamplerState<SF_Bilinear>::GetRHI();
<               UniformParams.LodBiasTexture = LodBiasTexture ? LodBiasTexture->GetResource()->TextureRHI : GBlackTexture->TextureRHI;
<               UniformParams.LodBiasSampler = TStaticSamplerState<SF_Point>::GetRHI();
<               UniformParams.NumInstanceVertexSide = NumInstanceVertexSide;
<               {
<                   UniformParams.MaxLod = RVTMaxLevel;
<                   UniformParams.RVTMinLevel = NumQuadsPerTileOfTwo;
<               }
<
<               FUintVector4 PackedUniform;
<               AllocatedVirtualTexture->GetPackedUniform(&PackedUniform, 0);
<               UniformParams.VTPackedUniform = PackedUniform;
<               FUintVector4 PackedPageTableUniform[2];
<               AllocatedVirtualTexture->GetPackedPageTableUniform(PackedPageTableUniform);
<               UniformParams.VTPackedPageTableUniform0 = PackedPageTableUniform[0];
<               UniformParams.VTPackedPageTableUniform1 = PackedPageTableUniform[1];
<
<               const float PageTableSizeX = AllocatedVirtualTexture->GetWidthInTiles();
<               const float PageTableSizeY = AllocatedVirtualTexture->GetHeightInTiles();
<               UniformParams.PageTableSize = FVector4f(PageTableSizeX, PageTableSizeY, 1.f / PageTableSizeX, 1.f / PageTableSizeY);
<
<               const float PhysicalTextureSize = AllocatedVirtualTexture->GetPhysicalTextureSize(0);
<               UniformParams.PhysicalTextureSize = FVector2f(PhysicalTextureSize, 1.f / PhysicalTextureSize);
<
<               UniformParams.VirtualHeightfieldToLocal = FMatrix44f(UVToLocal);
<               UniformParams.VirtualHeightfieldToWorld = FMatrix44f(UVToWorld);        // LWC_TODO: Precision loss
<
<               UniformParams.LodBiasScale = LodBiasScale;
<
<               const float PageSize = AllocatedVirtualTexture->GetVirtualTileSize();
<               const float PageBorderSize = AllocatedVirtualTexture->GetTileBorderSize();
<               const float PageAndBorderSize = PageSize + PageBorderSize * 2.f;
<               const float HalfTexelSize = 0.5f;
<               const FVector4 PhysicalPageTransform = FVector4(PageAndBorderSize, PageSize, PageBorderSize, HalfTexelSize) * (1.f / PhysicalTextureSize);
<               UniformParams.PhysicalPageTransform = (FVector4f)PhysicalPageTransform;
<               UniformParams.NumPhysicalAddressBits = AllocatedVirtualTexture->GetPageTableFormat() == EVTPageTableFormat::UInt16 ? 6 : 8; // See packing in PageTableUpdate.usf
<
<               // Create vertex factory.
<               VertexFactory = new FVirtualHeightfieldMeshVertexFactory(GetScene().GetFeatureLevel(), UniformParams);
<               VertexFactory->InitResource(FRHICommandListImmediate::Get());
<           }
<       }
<   }
< }
<
< void FVirtualHeightfieldMeshSceneProxy::DestroyRenderThreadResources()
< {
<   if (VertexFactory != nullptr)
<   {
<       VertexFactory->ReleaseResource();
<       delete VertexFactory;
<       VertexFactory = nullptr;
<   }
<
<   if (bCallbackRegistered)
<   {
<       GetRendererModule().RemoveAllVirtualTextureProducerDestroyedCallbacks(this);
<       bCallbackRegistered = false;
<   }
< }
<
< void FVirtualHeightfieldMeshSceneProxy::OnVirtualTextureDestroyedCB(const FVirtualTextureProducerHandle& InHandle, void* Baton)
< {
<   FVirtualHeightfieldMeshSceneProxy* SceneProxy = (FVirtualHeightfieldMeshSceneProxy*)Baton;
<   SceneProxy->DestroyRenderThreadResources();
<   SceneProxy->CreateRenderThreadResources();
< }
<
< FPrimitiveViewRelevance FVirtualHeightfieldMeshSceneProxy::GetViewRelevance(const FSceneView* View) const
< {
<   const bool bValid = AllocatedVirtualTexture != nullptr;
<   const bool bIsHiddenInEditor = bHiddenInEditor && View->Family->EngineShowFlags.Editor;
<
<   FPrimitiveViewRelevance Result;
<   Result.bDrawRelevance = bValid && IsShown(View) && !bIsHiddenInEditor;
<   Result.bShadowRelevance = bValid && IsShadowCast(View) && ShouldRenderInMainPass() &&!bIsHiddenInEditor;
<   Result.bDynamicRelevance = true;
<   Result.bStaticRelevance = false;
<   Result.bRenderInMainPass = ShouldRenderInMainPass();
<   Result.bUsesLightingChannels = GetLightingChannelMask() != GetDefaultLightingChannelMask();
<   Result.bRenderCustomDepth = ShouldRenderCustomDepth();
<   Result.bTranslucentSelfShadow = false;
<   MaterialRelevance.SetPrimitiveViewRelevance(Result);
<   Result.bVelocityRelevance = DrawsVelocity() && Result.bOpaque && Result.bRenderInMainPass;
<   return Result;
< }
<
< void FVirtualHeightfieldMeshSceneProxy::GetDynamicMeshElements(const TArray<const FSceneView*>& Views, const FSceneViewFamily& ViewFamily, uint32 VisibilityMap, FMeshElementCollector& Collector) const
< {
<   check(IsInRenderingThread());
<   check(AllocatedVirtualTexture != nullptr);
<
<   if (GVirtualHeightfieldMeshViewRendererExtension.IsInFrame())
<   {
<       // Can't add new work while bInFrame.
<       // In UE5 we need to AddWork()/SubmitWork() in two phases: InitViews() and InitViewsAfterPrepass()
<       // The main renderer hooks for that don't exist in UE5.0 and are only added in UE5.1
<       // That means that for UE5.0 we always hit this for shadow drawing and shadows will not be rendered.
<       // Not earlying out here can lead to crashes from buffers being released too soon.
<       return;
<   }
<
<   for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ViewIndex++)
<   {
<       if (VisibilityMap & (1 << ViewIndex))
<       {
<           if (!IsShadowCast(Views[ViewIndex]) && ViewFamily.Views[0] != Views[ViewIndex])
<           {
<               continue;
<           }
<           VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers = GVirtualHeightfieldMeshViewRendererExtension.AddWork(this, ViewFamily.Views[0], Views[ViewIndex]);
<
<           {
<               FMeshBatch& Mesh = Collector.AllocateMesh();
<               // Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;
<               Mesh.bWireframe = ViewFamily.EngineShowFlags.Wireframe;
<               Mesh.bUseWireframeSelectionColoring = IsSelected();
<               Mesh.VertexFactory = VertexFactory;
<               Mesh.MaterialRenderProxy = Material;
<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();
<               Mesh.Type = PT_TriangleList;
<               Mesh.DepthPriorityGroup = SDPG_World;
<               Mesh.bCanApplyViewModeOverrides = true;
<               Mesh.bUseForMaterial = true;
<               Mesh.CastShadow = true;
<               Mesh.bUseForDepthPass = true;
<
<               Mesh.Elements.SetNumZeroed(1);
<               {
<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];
<
<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();
<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;
<                   BatchElement.IndirectArgsOffset = 0;
<
<                   BatchElement.FirstIndex = 0;
<                   BatchElement.NumPrimitives = 0;
<                   BatchElement.MinVertexIndex = 0;
<                   BatchElement.MaxVertexIndex = 0;
<
<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;
<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();
<
<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();
<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;
<                   Parameters2.InstanceBuffer = Buffers.InstanceBufferSRV;
<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);
<                   BatchElement.UserData = (void*)UserData;
<
<                   UserData->InstanceBufferSRV = Buffers.InstanceBufferSRV;
<
<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.
<                   FSceneView const* MainView = ViewFamily.Views[0];
<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss
<
< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)
<                   // Support the freezerendering mode. Use any frozen view state for culling.
<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;
<                   if (FrozenViewMatrices != nullptr)
<                   {
<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();
<                   }
< #endif
<
<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);
<               }
<
<               Collector.AddMesh(ViewIndex, Mesh);
<           }
< #pragma region S1_Engine_Shiyu
<           // for hole quad
<           {
<               FMeshBatch& Mesh = Collector.AllocateMesh();
<               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;
<               Mesh.bUseWireframeSelectionColoring = IsSelected();
<               Mesh.VertexFactory = VertexFactory;
<               Mesh.MaterialRenderProxy = HoleMaterial;
<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();
<               Mesh.Type = PT_TriangleList;
<               Mesh.DepthPriorityGroup = SDPG_World;
<               Mesh.bCanApplyViewModeOverrides = true;
<               Mesh.bUseForMaterial = true;
<               Mesh.CastShadow = true;
<               Mesh.bUseForDepthPass = true;
<
<               Mesh.Elements.SetNumZeroed(1);
<               {
<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];
<
<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();
<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;
<                   BatchElement.IndirectArgsOffset = 5 * sizeof(uint32);
<
<                   BatchElement.FirstIndex = 0;
<                   BatchElement.NumPrimitives = 0;
<                   BatchElement.MinVertexIndex = 0;
<                   BatchElement.MaxVertexIndex = 0;
<
<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;
<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();
<
<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();
<
<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;
<                   Parameters2.InstanceBuffer = Buffers.HoleInstanceBufferSRV;
<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);
<
<                   BatchElement.UserData = (void*)UserData;
<
<                   UserData->InstanceBufferSRV = Buffers.HoleInstanceBufferSRV;
<
<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.
<                   FSceneView const* MainView = ViewFamily.Views[0];
<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss
<
< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)
<                   // Support the freezerendering mode. Use any frozen view state for culling.
<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;
<                   if (FrozenViewMatrices != nullptr)
<                   {
<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();
<                   }
< #endif
<
<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);
<               }
<
<               Collector.AddMesh(ViewIndex, Mesh);
<           }
<       }
<
< #pragma endregion
<   }
< }
<
< bool FVirtualHeightfieldMeshSceneProxy::HasSubprimitiveOcclusionQueries() const
< {
<   return CVarVHMOcclusion.GetValueOnAnyThread() != 0;
< }
<
< const TArray<FBoxSphereBounds>* FVirtualHeightfieldMeshSceneProxy::GetOcclusionQueries(const FSceneView* View) const
< {
<   return (CVarVHMOcclusion.GetValueOnAnyThread() == 0 || OcclusionVolumes.Num() == 0) ? &DefaultOcclusionVolumes : &OcclusionVolumes;
< }
<
< void FVirtualHeightfieldMeshSceneProxy::BuildOcclusionVolumes(TArrayView<FVector2D> const& InMinMaxData, FIntPoint const& InMinMaxSize, TArrayView<int32> const& InMinMaxMips, int32 InNumLods)
< {
<   NumOcclusionLods = 0;
<   OcclusionGridSize = FIntPoint::ZeroValue;
<   OcclusionVolumes.Reset();
<
<   if (InNumLods > 0 && InMinMaxMips.Num() > 0)
<   {
<       NumOcclusionLods = FMath::Min(InNumLods, InMinMaxMips.Num());
<
<       const int32 BaseLod = InMinMaxMips.Num() - NumOcclusionLods;
<       OcclusionGridSize.X = FMath::Max(InMinMaxSize.X >> BaseLod, 1);
<       OcclusionGridSize.Y = FMath::Max(InMinMaxSize.Y >> BaseLod, 1);
<
<       OcclusionVolumes.Reserve(InMinMaxData.Num() - InMinMaxMips[BaseLod]);
<
<       for (int32 LodIndex = BaseLod; LodIndex < InMinMaxMips.Num(); ++LodIndex)
<       {
<           int32 SizeX = FMath::Max(InMinMaxSize.X >> LodIndex, 1);
<           int32 SizeY = FMath::Max(InMinMaxSize.Y >> LodIndex, 1);
<           int32 MinMaxDataIndex = InMinMaxMips[LodIndex];
<
<           for (int Y = 0; Y < SizeY; ++Y)
<           {
<               for (int X = 0; X < SizeX; ++X)
<               {
<                   FVector2D MinMaxU = FVector2D((float)X / (float)SizeX, (float)(X + 1) / (float)SizeX);
<                   FVector2D MinMaxV = FVector2D((float)Y / (float)SizeY, (float)(Y + 1) / (float)SizeY);
<                   FVector2D MinMaxZ = InMinMaxData[MinMaxDataIndex++];
<
<                   FVector Pos[8];
<                   Pos[0] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.X));
<                   Pos[1] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.X));
<                   Pos[2] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.X));
<                   Pos[3] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.X));
<                   Pos[4] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.Y));
<                   Pos[5] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.Y));
<                   Pos[6] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.Y));
<                   Pos[7] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.Y));
<
<                   const float ExpandOcclusion = 3.f;
<                   OcclusionVolumes.Add(FBoxSphereBounds(FBox(Pos, 8).ExpandBy(ExpandOcclusion)));
<               }
<           }
<       }
<   }
< }
<
< void FVirtualHeightfieldMeshSceneProxy::AcceptOcclusionResults(FSceneView const* View, TArray<bool>* Results, int32 ResultsStart, int32 NumResults)
< {
<   if (GOcclusionResetRequired)
<   {
<       GOcclusionResults.Reset();
<       GOcclusionResetRequired = false;
<   }
<
<   if (CVarVHMOcclusion.GetValueOnAnyThread() != 0 && Results != nullptr && NumResults > 1)
<   {
<       FOcclusionResults& OcclusionResults = GOcclusionResults.Emplace(FOcclusionResultsKey(this, View));
<       OcclusionResults.TextureSize = OcclusionGridSize;
<       OcclusionResults.NumTextureMips = NumOcclusionLods;
<       OcclusionResults.UploadData.Append(Results->GetData() + ResultsStart, NumResults);
<   }
< }
<
< namespace VirtualHeightfieldMesh
< {
<   /* Keep indirect args offsets in sync with VirtualHeightfieldMesh.usf. */
<   static const int32 IndirectArgsByteOffset_FinalCull = 0;
<
<
<   /** Shader structure used for tracking work queues in persistent wave style shaders. Keep in sync with VirtualHeightfieldMesh.ush. */
<   struct WorkerQueueInfo
<   {
<       uint32 Read;
<       uint32 Write;
<       int32 NumActive;
<   };
<
<   /** Final render instance description used by the DrawInstancedIndirect(). Keep in sync with VirtualHeightfieldMesh.ush. */
<   struct QuadRenderInstance
<   {
<       float UVTransform[3];
<       // uint32 AddressLevelPacked;
<       // float UVTransformPar[3];
<       // float Height;
<       // float UVTransformPar2[3];
<       // float Padding;
<       uint32 PhysicalAddress[3];
<       // uint32 Padding2;
<   };
<
<   /** Compute shader to initialize all buffers, including adding the lowest mip page(s) to the QuadBuffer. */
<   class FInitBuffersVHM_CS : public FGlobalShader
<   {
<   public:
<       DECLARE_GLOBAL_SHADER(FInitBuffersVHM_CS);
<       SHADER_USE_PARAMETER_STRUCT(FInitBuffersVHM_CS, FGlobalShader);
<
<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<           SHADER_PARAMETER(uint32, MaxLevel)
<           SHADER_PARAMETER(uint32, NumForceLoadLods)
<           SHADER_PARAMETER(uint32, PageTableFeedbackId)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)
< #pragma region S1_Engine_Shiyu
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)
< #pragma endregion
<
<       END_SHADER_PARAMETER_STRUCT()
<
<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)
<       // {
<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);
<       // }
<   };
<
<   IMPLEMENT_GLOBAL_SHADER(FInitBuffersVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitBuffersCS", SF_Compute);
<
<   /** Compute shader to traverse the virtual texture page table for a view and generate an array of quads to potentially render. */
<   class FCollectQuadsVHM_CS : public FGlobalShader
<   {
<   public:
<       DECLARE_GLOBAL_SHADER(FCollectQuadsVHM_CS);
<       SHADER_USE_PARAMETER_STRUCT(FCollectQuadsVHM_CS, FGlobalShader);
<
<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)
<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)
<           SHADER_PARAMETER(int32, MinMaxLevelOffset)
<           SHADER_PARAMETER_TEXTURE(Texture2D, LodBiasMinMaxTexture)
<           SHADER_PARAMETER_TEXTURE(Texture2D<float>, OcclusionTexture)
<           SHADER_PARAMETER(int32, OcclusionLevelOffset)
<           SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<           SHADER_PARAMETER(uint32, MaxLevel)
<           SHADER_PARAMETER(uint32, RVTMinLevel)
<           SHADER_PARAMETER(FVector4f, PageTableSize)
<           SHADER_PARAMETER(uint32, PageTableFeedbackId)
<           SHADER_PARAMETER(FVector4f, LodDistances)
<           SHADER_PARAMETER(float, LodBiasScale)
<           SHADER_PARAMETER(FVector3f, ViewOrigin)
<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])
<           SHADER_PARAMETER(FMatrix44f, UVToWorld)
<           SHADER_PARAMETER(FVector3f, UVToWorldScale)
<           SHADER_PARAMETER(uint32, QueueBufferSizeMask)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, RWQuadBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)
< #pragma region S1_Engine_Shiyu
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)
< #pragma endregion
<       END_SHADER_PARAMETER_STRUCT()
<
<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)
<       // {
<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);
<       // }
<
<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)
<       {
< #pragma region S1_Engine_Shiyu
< #if VHM_ENABLE_STAT
<           Environment.SetDefine(TEXT("VHM_STAT"), 1);
< #endif
< #pragma endregion
<       }
<   };
<
<   IMPLEMENT_GLOBAL_SHADER(FCollectQuadsVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CollectQuadsCS", SF_Compute);
<
<   /** InitInstanceBuffer compute shader. */
<   class FInitInstanceBufferVHM_CS : public FGlobalShader
<   {
<   public:
<       DECLARE_GLOBAL_SHADER(FInitInstanceBufferVHM_CS);
<       SHADER_USE_PARAMETER_STRUCT(FInitInstanceBufferVHM_CS, FGlobalShader);
<
<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<           SHADER_PARAMETER(int32, NumIndices)
<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)
<       END_SHADER_PARAMETER_STRUCT()
<   };
<
<   IMPLEMENT_GLOBAL_SHADER(FInitInstanceBufferVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitInstanceBufferCS", SF_Compute);
<
<   /** CullInstances compute shader. */
<   class FCullInstancesVHM_CS : public FGlobalShader
<   {
<   public:
<       DECLARE_GLOBAL_SHADER(FCullInstancesVHM_CS);
<       SHADER_USE_PARAMETER_STRUCT(FCullInstancesVHM_CS, FGlobalShader);
<
<       class FReuseCullDim : SHADER_PERMUTATION_BOOL("REUSE_CULL");
<
<       using FPermutationDomain = TShaderPermutationDomain<FReuseCullDim>;
<
<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)
<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)
<           SHADER_PARAMETER(int32, MinMaxLevelOffset)
<           SHADER_PARAMETER(uint32, RVTMinLevel)
<           SHADER_PARAMETER_TEXTURE(Texture2D, PageTableTexture)
<           SHADER_PARAMETER(FVector4f, PageTableSize)
<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])
<           SHADER_PARAMETER(FVector4f, PhysicalPageTransform)
<           SHADER_PARAMETER(uint32, NumPhysicalAddressBits)
<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>, QuadBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>, IndirectArgsBufferSRV)
<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWInstanceBuffer)
<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)
<           RDG_BUFFER_ACCESS(IndirectArgsBuffer, ERHIAccess::IndirectArgs)
< #pragma region S1_Engine_Shiyu
<           SHADER_PARAMETER_TEXTURE(Texture2D, MaskTexture)
<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWHoleInstanceBuffer)
<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)
< #pragma endregion
<       END_SHADER_PARAMETER_STRUCT()
<
<
<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)
<       {
< #pragma region S1_Engine_Shiyu
< #if VHM_ENABLE_STAT
<           Environment.SetDefine(TEXT("VHM_STAT"), 1);
< #endif
< #pragma endregion
<       }
<   };
<
<   IMPLEMENT_GLOBAL_SHADER(FCullInstancesVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CullInstancesCS", SF_Compute);
<
<
<   namespace V2
<   {
< //        class FFirstInitBuffers_CS : public FGlobalShader
< //        {
< //        public:
< //            DECLARE_GLOBAL_SHADER(FFirstInitBuffers_CS);
< //            SHADER_USE_PARAMETER_STRUCT(FFirstInitBuffers_CS, FGlobalShader);
< //
< //            BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
< //                SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)
< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, FinalQuadBuffer)
< //                SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)
< //            END_SHADER_PARAMETER_STRUCT()
< //        };
< //        IMPLEMENT_GLOBAL_SHADER(FFirstInitBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "FirstInitBuffersCS", SF_Compute);
<
<       class FInitAllBuffers_CS : public FGlobalShader
<       {
<       public:
<           DECLARE_GLOBAL_SHADER(FInitAllBuffers_CS);
<           SHADER_USE_PARAMETER_STRUCT(FInitAllBuffers_CS, FGlobalShader);
<
<           class FClearVTCountDim : SHADER_PERMUTATION_BOOL("CLEAR_VT_COUNT");
<
<           using FPermutationDomain = TShaderPermutationDomain<FClearVTCountDim>;
<
<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer1)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer2)
<               SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)
<           END_SHADER_PARAMETER_STRUCT()
<
<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)
<           {
< #if VHM_ENABLE_STAT
<               Environment.SetDefine(TEXT("VHM_STAT"), 1);
< #endif
<           }
<       };
<       IMPLEMENT_GLOBAL_SHADER(FInitAllBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "InitAllBuffersCS", SF_Compute);
<
<
<       class FFillLevel4Quad_CS : public FGlobalShader
<       {
<       public:
<           DECLARE_GLOBAL_SHADER(FFillLevel4Quad_CS)
<           SHADER_USE_PARAMETER_STRUCT(FFillLevel4Quad_CS, FGlobalShader)
<
<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)
<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<           END_SHADER_PARAMETER_STRUCT()
<       };
<       IMPLEMENT_GLOBAL_SHADER(FFillLevel4Quad_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "FillLevel4QuadCS", SF_Compute);
<
<
<       // class FCollectQuadsFromPreFrame_CS : public FGlobalShader
<       // {
<       // public:
<       //  DECLARE_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS);
<       //  SHADER_USE_PARAMETER_STRUCT(FCollectQuadsFromPreFrame_CS, FGlobalShader);
<       //
<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)
<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<       //      RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)
<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)
<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
<       //  END_SHADER_PARAMETER_STRUCT()
<       // };
<       // IMPLEMENT_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectQuadsFromPreFrameCS", SF_Compute);
<       //
<
<       class FCollectSubdivideQuads_CS : public FGlobalShader
<       {
<       public:
<           DECLARE_GLOBAL_SHADER(FCollectSubdivideQuads_CS);
<           SHADER_USE_PARAMETER_STRUCT(FCollectSubdivideQuads_CS, FGlobalShader);
<
<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");
<           class FWithFeedback : SHADER_PERMUTATION_BOOL("VHM_WITH_FEEDBACK");
<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim, FWithFeedback>;
<
<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)
<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<               SHADER_PARAMETER(uint32, CurPassCalTime)
<               RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)
<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)
<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)
<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)
<           END_SHADER_PARAMETER_STRUCT()
<
<       };
<       IMPLEMENT_GLOBAL_SHADER(FCollectSubdivideQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectSubdivideQuadsCS", SF_Compute);
<
<       // class FCollectMergeQuads_CS : public FGlobalShader
<       // {
<       // public:
<       //  DECLARE_GLOBAL_SHADER(FCollectMergeQuads_CS);
<       //  SHADER_USE_PARAMETER_STRUCT(FCollectMergeQuads_CS, FGlobalShader);
<       //
<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)
<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<       //      SHADER_PARAMETER(uint32, CurPassCalTime)
<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)
<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)
<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
<       //  END_SHADER_PARAMETER_STRUCT()
<       // };
<       // IMPLEMENT_GLOBAL_SHADER(FCollectMergeQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectMergeQuadsCS", SF_Compute);
<
<       // class FCollectRemainQuads_CS : public FGlobalShader
<       // {
<       // public:
<       //  DECLARE_GLOBAL_SHADER(FCollectRemainQuads_CS);
<       //  SHADER_USE_PARAMETER_STRUCT(FCollectRemainQuads_CS, FGlobalShader);
<       //
<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)
<       //      SHADER_PARAMETER(uint32, RemainCS_DispatchArgsOffset)
<       //      SHADER_PARAMETER(uint32, CurPassCalTime)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)
<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)
<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
<       //  END_SHADER_PARAMETER_STRUCT()
<       // };
<       // IMPLEMENT_GLOBAL_SHADER(FCollectRemainQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectRemainQuadsCS", SF_Compute);
<       //
<       class FCullQuadsAndGenerateInstances_CS : public FGlobalShader
<       {
<       public:
<           DECLARE_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS);
<           SHADER_USE_PARAMETER_STRUCT(FCullQuadsAndGenerateInstances_CS, FGlobalShader);
<
<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");
<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim>;
<
<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)
<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
<               RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)
<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)
<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)
<               SHADER_PARAMETER_UAV(RWBuffer<uint>,                InstanceArgsBuffer)
<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    QuadInstanceBuffer)
<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    HoleQuadInstanceBuffer)
<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)
<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)
<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)
<           END_SHADER_PARAMETER_STRUCT()
<
<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)
<           {
< #if VHM_ENABLE_STAT
<               Environment.SetDefine(TEXT("VHM_STAT"), 1);
< #endif
<           }
<       };
<       IMPLEMENT_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CullQuadsAndGenerateInstancesCS", SF_Compute);
<   };
<
<   /** Default Min/Max texture has the fixed maximum [0,1]. */
<   class FHeightMinMaxDefaultTexture : public FTexture
<   {
<   public:
<       virtual void InitRHI(FRHICommandListBase& RHICmdList) override
<       {
<           const FRHITextureCreateDesc Desc =
<               FRHITextureCreateDesc::Create2D(TEXT("VirtualHeightfieldMesh.MinMaxDefaultTexture"), 1, 1, PF_B8G8R8A8)
<               .SetFlags(ETextureCreateFlags::ShaderResource);
<
<           TextureRHI = RHICreateTexture(Desc);
<
<           // Write the contents of the texture.
<           uint32 DestStride;
<           FColor* DestBuffer = (FColor*)RHILockTexture2D(TextureRHI, 0, RLM_WriteOnly, DestStride, false);
<           *DestBuffer = FColor(0, 0, 255, 255);
<           RHIUnlockTexture2D(TextureRHI, 0, false);
<
<           // Create the sampler state RHI resource.
<           FSamplerStateInitializerRHI SamplerStateInitializer(SF_Point, AM_Clamp, AM_Clamp, AM_Clamp);
<           SamplerStateRHI = GetOrCreateSamplerState(SamplerStateInitializer);
<       }
<
<       virtual uint32 GetSizeX() const override { return 1; }
<       virtual uint32 GetSizeY() const override { return 1; }
<   };
<
<   /** Single global instance of default Min/Max texture. */
<   FTexture* GHeightMinMaxDefaultTexture = new TGlobalResource<FHeightMinMaxDefaultTexture>;
<
<   /** View matrices that can be frozen in freezerendering mode. */
<   struct FViewData
<   {
<       FVector ViewOrigin;
<       FMatrix ProjectionMatrix;
<       FConvexVolume ViewFrustum;
<       bool bViewFrozen;
<   };
<
<   /** Fill the FViewData from an FSceneView respecting the freezerendering mode. */
<   void GetViewData(FSceneView const* InSceneView, FViewData& OutViewData)
<   {
< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)
<       const FViewMatrices* FrozenViewMatrices = InSceneView->State != nullptr ? InSceneView->State->GetFrozenViewMatrices() : nullptr;
<       if (FrozenViewMatrices != nullptr)
<       {
<           OutViewData.ViewOrigin = FrozenViewMatrices->GetViewOrigin();
<           OutViewData.ProjectionMatrix = FrozenViewMatrices->GetProjectionMatrix();
<           GetViewFrustumBounds(OutViewData.ViewFrustum, FrozenViewMatrices->GetViewProjectionMatrix(), true);
<           OutViewData.bViewFrozen = true;
<       }
<       else
< #endif
<       {
<           OutViewData.ViewOrigin = InSceneView->ViewMatrices.GetViewOrigin();
<           OutViewData.ProjectionMatrix = InSceneView->ViewMatrices.GetProjectionMatrix();
<           OutViewData.ViewFrustum = InSceneView->ViewFrustum;
<           OutViewData.bViewFrozen = false;
<       }
<   }
<
<   /** Convert FPlane to Xx+Yy+Zz+W=0 form for simpler use in shader. */
<   FVector4 ConvertPlane(FPlane const& Plane)
<   {
<       return FVector4(-Plane.X, -Plane.Y, -Plane.Z, Plane.W);
<   }
<
<   /** Translate a plane. This is a simpler case than the full TransformPlane(). */
<   FPlane TranslatePlane(FPlane const& Plane, FVector const& Translation)
<   {
<       FPlane OutPlane = Plane / Plane.Size();
<       OutPlane.W -= FVector::DotProduct(FVector(OutPlane),  Translation);
<       return OutPlane;
<   }
<
<   /** Transform a plane using a transform matrix. Precalculate and pass in transpose adjoint to avoid work when transforming multiple planes.  */
<   FPlane TransformPlane(FPlane const& Plane, FMatrix const& Matrix, FMatrix const& TransposeAdjoint)
<   {
<       FVector N(Plane.X, Plane.Y, Plane.Z);
<       N = TransposeAdjoint.TransformVector(N).GetUnsafeNormal3();
<
<       FVector P(Plane.X * Plane.W, Plane.Y * Plane.W, Plane.Z * Plane.W);
<       P = Matrix.TransformPosition(P);
<
<       return FPlane(N, FVector::DotProduct(N, P));
<   }
<
<   /** Structure describing GPU culling setup for a single Proxy. */
<   struct FProxyDesc
<   {
<       FRHITexture* PageTableTexture;
<       FRHITexture* HeightMinMaxTexture;
<       FRHITexture* LodBiasMinMaxTexture;
<       int32 MinMaxLevelOffset;
<
< #pragma region S1_Engine_Shiyu
<       FRHITexture* MaskTexture;
< #pragma endregion
<
<
<       uint32 MaxLevel;
<       uint32 RVTMinLevel;
<       uint32 NumForceLoadLods;
<       uint32 PageTableFeedbackId;
<       uint32 NumPhysicalAddressBits;
<       FVector4 PageTableSize;
<       FVector4 PhysicalPageTransform;
<       FMatrix UVToWorld;
<       FVector UVToWorldScale;
<       uint32 NumQuadsPerTileOfTwo;
<       uint32 NumInstanceVertexSide; // Instance is a Plane, size is NumInstanceVertexSide * NumInstanceVertexSide
<
<       int32 MaxPersistentQueueItems;
<       int32 MaxRenderItems;
<       int32 MaxFeedbackItems;
<       int32 NumCollectPassWavefronts;
<
<       uint32 NumIndices;
<   };
<
<   /** View description used for LOD calculation in the main view. */
<   struct FMainViewDesc
<   {
<       FSceneView const* ViewDebug;
<       FVector ViewOrigin;
<       FVector4 LodDistances;
<       float LodBiasScale;
<       FVector4 Planes[5];
<       FTextureRHIRef OcclusionTexture;
<       int32 OcclusionLevelOffset;
<   };
<
<   /** View description used for culling in the child view. */
<   struct FChildViewDesc
<   {
<       FSceneView const* ViewDebug;
<       bool bIsMainView;
<       FVector4 Planes[5];
<   };
<
<   /** Structure to carry RDG resources. */
<   struct FVolatileResources
<   {
<       FRDGBufferRef QueueInfo;
<       FRDGBufferUAVRef QueueInfoUAV;
<       FRDGBufferRef QueueBuffer;
<       FRDGBufferUAVRef QueueBufferUAV;
<
<       FRDGBufferRef QuadBuffer;
<       FRDGBufferUAVRef QuadBufferUAV;
<       FRDGBufferSRVRef QuadBufferSRV;
<
<       FRDGBufferRef FeedbackBuffer;
<       FRDGBufferUAVRef FeedbackBufferUAV;
<
<       FRDGBufferRef IndirectArgsBuffer;
<       FRDGBufferUAVRef IndirectArgsBufferUAV;
<       FRDGBufferSRVRef IndirectArgsBufferSRV;
<
< #pragma region S1_Engine_Shiyu
< //#if VHM_ENABLE_STAT
<       FRDGBufferRef StatBuffer;
<       FRDGBufferUAVRef StatBufferUAV;
< //#endif
< #pragma endregion
<   };
<
<   /** Initialize the FDrawInstanceBuffers objects. */
<   void InitializeInstanceBuffers(FRHICommandListImmediate& RHICmdList, FDrawInstanceBuffers& InBuffers)
<   {
<       {
<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.InstanceBuffer"));
<           const int32 InstanceSize = sizeof(VirtualHeightfieldMesh::QuadRenderInstance);
<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnRenderThread() * InstanceSize;
<           InBuffers.InstanceBuffer = RHICmdList.CreateStructuredBuffer(InstanceSize, InstanceBufferSize, BUF_UnorderedAccess|BUF_ShaderResource, ERHIAccess::SRVMask, CreateInfo);
<           InBuffers.InstanceBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.InstanceBuffer, false, false);
<           InBuffers.InstanceBufferSRV = RHICmdList.CreateShaderResourceView(InBuffers.InstanceBuffer);
<       }
<       {
<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.InstanceIndirectArgsBuffer"));
<           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(10 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect|BUF_SourceCopy, ERHIAccess::IndirectArgs|ERHIAccess::CopySrc, CreateInfo);
<           InBuffers.IndirectArgsBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.IndirectArgsBuffer, PF_R32_UINT);
<       }
< #pragma region S1_Engine_Shiyu
<       {
<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.HoleInstanceBuffer"));
<           const int32 InstanceSize = sizeof(VirtualHeightfieldMesh::QuadRenderInstance);
<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnRenderThread() * InstanceSize / 4; // hold instance just little
<           InBuffers.HoleInstanceBuffer = RHICmdList.CreateStructuredBuffer(InstanceSize, InstanceBufferSize, BUF_UnorderedAccess|BUF_ShaderResource, ERHIAccess::SRVMask, CreateInfo);
<           InBuffers.HoleInstanceBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.HoleInstanceBuffer, false, false);
<           InBuffers.HoleInstanceBufferSRV = RHICmdList.CreateShaderResourceView(InBuffers.HoleInstanceBuffer);
<       }
< #if VHM_ENABLE_STAT
<       InBuffers.StatBufferReadBacks.Reserve(FDrawInstanceBuffers::MaxReadBackSize);
<       for (int32 i = 0; i < FDrawInstanceBuffers::MaxReadBackSize; ++i)
<       {
<           InBuffers.StatBufferReadBacks.Emplace(MakeUnique<FRHIGPUBufferReadback>(TEXT("VHM.StatReadBacks")));
<       }
< #endif
< #pragma endregion
<   }
<
<   /** Initialize the volatile resources used in the render graph. */
<   void InitializeResources(FRDGBuilder& GraphBuilder, FProxyDesc const& InDesc, FMainViewDesc const& InMainViewDesc, FVolatileResources& OutResources)
<   {
<       OutResources.QueueInfo = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateStructuredDesc(sizeof(WorkerQueueInfo), 1), TEXT("VirtualHeightfieldMesh.QueueInfo"));
<       OutResources.QueueInfoUAV = GraphBuilder.CreateUAV(OutResources.QueueInfo);
<       OutResources.QueueBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), InDesc.MaxPersistentQueueItems), TEXT("VirtualHeightfieldMesh.QuadQueue"));
<       OutResources.QueueBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.QueueBuffer, PF_R32_UINT));
<
<       OutResources.QuadBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 2, InDesc.MaxRenderItems), TEXT("VirtualHeightfieldMesh.QuadBuffer"));
<       OutResources.QuadBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.QuadBuffer, PF_R32G32_UINT));
<       OutResources.QuadBufferSRV = GraphBuilder.CreateSRV(FRDGBufferSRVDesc(OutResources.QuadBuffer, PF_R32G32_UINT));
<
<       FRDGBufferDesc FeedbackBufferDesc = FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), InDesc.MaxFeedbackItems + 1);
<       FeedbackBufferDesc.Usage = EBufferUsageFlags(FeedbackBufferDesc.Usage | BUF_SourceCopy);
<       OutResources.FeedbackBuffer = GraphBuilder.CreateBuffer(FeedbackBufferDesc, TEXT("VirtualHeightfieldMesh.FeedbackBuffer"));
<       OutResources.FeedbackBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.FeedbackBuffer, PF_R32_UINT));
<
<       OutResources.IndirectArgsBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize), TEXT("VirtualHeightfieldMesh.IndirectArgsBuffer"));
<       OutResources.IndirectArgsBufferUAV = GraphBuilder.CreateUAV(OutResources.IndirectArgsBuffer);
<       OutResources.IndirectArgsBufferSRV = GraphBuilder.CreateSRV(OutResources.IndirectArgsBuffer);
<
< #pragma region S1_Engine_Shiyu
< //#if VHM_ENABLE_STAT
<       OutResources.StatBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),
<                                                           TEXT("VirtualHeightfieldMesh.StatBuffer"));
<       OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));
< //#endif
< #pragma endregion
<   }
<
<   namespace V2
<   {
<
<
<       void InitializeVolatileBuffers(FRDGBuilder& GraphBuilder, FVolatileBuffers& OutResources)
<       {
<           const int32 MaxRenderItems = CVarVHMMaxRenderItems.GetValueOnRenderThread();
<           const int32 MaxFeedbackItems = CVarVHMMaxFeedbackItems.GetValueOnRenderThread();
<           const TCHAR* MergeNames[2] = {
<               TEXT("VHM.MergeBuffer_0"),
<               TEXT("VHM.MergeBuffer_1")};
<           const TCHAR* MergeArgsNames[2] = {
<               TEXT("VHM.MergeArgsBuffer_0"),
<               TEXT("VHM.MergeArgsBuffer_1")};
<           const TCHAR* SubdivideNames[2] = {
<               TEXT("VHM.SubdivideBuffer_0"),
<               TEXT("VHM.SubdivideBuffer_1")};
<           const TCHAR* SubdivideArgsNames[2] = {
<               TEXT("VHM.SubdivideArgsBuffer_0"),
<               TEXT("VHM.SubdivideArgsBuffer_1")};
<           for(int i = 0; i < 2; ++i)
<           {
<               OutResources.MergeQuadBuffer[i] = GraphBuilder.CreateBuffer(
<                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),
<                   MergeNames[i]);
<               OutResources.MergeViews[i].Create(GraphBuilder, OutResources.MergeQuadBuffer[i]);
<
<               OutResources.SubdivideQuadBuffer[i] = GraphBuilder.CreateBuffer(
<                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),
<                   SubdivideNames[i]);
<               OutResources.SubdivideViews[i].Create(GraphBuilder, OutResources.SubdivideQuadBuffer[i]);
<
<               OutResources.ArgsBuffer[i] = GraphBuilder.CreateBuffer(
<                   FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize),
<                   SubdivideArgsNames[i]);
<               OutResources.ArgsViews[i].Create(GraphBuilder, OutResources.ArgsBuffer[i]);
<
<           }
<
<
<           // uniform
<           OutResources.VHMParameterUBuffer = GraphBuilder.CreateUniformBuffer<FVHMCSSharedParameters>(OutResources.VHMParameter);
<
< //#if VHM_ENABLE_STAT
<           OutResources.StatBuffer = GraphBuilder.CreateBuffer(
<               FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),
<               TEXT("VirtualHeightfieldMesh.StatBuffer"));
<           OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));
< //#endif
<       }
<   }
<
< #pragma region S1_Engine_Shiyu
< #if VHM_ENABLE_STAT
<   void AddPass_GatherAllStats(FRDGBuilder& GraphBuilder,
<                               VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers, const uint32 BufferDiscardId,
<                               FRDGBufferRef StatBuffer)
<   {
<       const uint32 Offset = BufferDiscardId % FDrawInstanceBuffers::MaxReadBackSize;
<       FRHIGPUBufferReadback* GPUBufferReadBack = Buffers.StatBufferReadBacks[Offset].Get();
<       check(GPUBufferReadBack);
<       AddEnqueueCopyPass(GraphBuilder, GPUBufferReadBack, StatBuffer, sizeof(int32) * MaxStatCount);
<   }
< #endif
< #pragma endregion
<
<   /** Transition our output draw buffers for use. Read or write access is set according to the bToWrite parameter. */
<   void AddPass_TransitionAllDrawBuffers(FRDGBuilder& GraphBuilder, TArray<VirtualHeightfieldMesh::FDrawInstanceBuffers> const& Buffers, TArrayView<int32> const& BufferIndices, bool bToWrite)
<   {
<       TArray<FRHIUnorderedAccessView*> OverlapUAVs;
<       OverlapUAVs.Reserve(BufferIndices.Num());
<
<       TArray<FRHITransitionInfo> TransitionInfos;
<       TransitionInfos.Reserve(BufferIndices.Num() * 2);
<
<       for (int32 BufferIndex : BufferIndices)
<       {
<           FRHIUnorderedAccessView* IndirectArgsBufferUAV = Buffers[BufferIndex].IndirectArgsBufferUAV;
<           FRHIUnorderedAccessView* InstanceBufferUAV = Buffers[BufferIndex].InstanceBufferUAV;
<
<           OverlapUAVs.Add(IndirectArgsBufferUAV);
<
<           TransitionInfos.Add(FRHITransitionInfo(IndirectArgsBufferUAV, bToWrite ? ERHIAccess::IndirectArgs|ERHIAccess::CopySrc : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::IndirectArgs|ERHIAccess::CopySrc));
<           TransitionInfos.Add(FRHITransitionInfo(InstanceBufferUAV, bToWrite ? ERHIAccess::SRVMask : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::SRVMask));
<
< #pragma region S1_Engine_Shiyu
<           FRHIUnorderedAccessView* HoleInstanceBufferUAV = Buffers[BufferIndex].HoleInstanceBufferUAV;
<
<           TransitionInfos.Add(FRHITransitionInfo(HoleInstanceBufferUAV, bToWrite ? ERHIAccess::SRVMask : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::SRVMask));
< #pragma endregion
<       }
<
<       AddPass(GraphBuilder, RDG_EVENT_NAME("TransitionAllDrawBuffers"), [bToWrite, OverlapUAVs, TransitionInfos](FRHICommandList& InRHICmdList)
<       {
<           if (!bToWrite)
<           {
<               InRHICmdList.EndUAVOverlap(OverlapUAVs);
<           }
<
<           InRHICmdList.Transition(TransitionInfos);
<
<           if (bToWrite)
<           {
<               InRHICmdList.BeginUAVOverlap(OverlapUAVs);
<           }
<       });
<   }
<
<   /** Initialize the buffers before collecting visible quads. */
<   void AddPass_InitBuffers(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, FProxyDesc const& InDesc, FVolatileResources& InVolatileResources)
<   {
<       TShaderMapRef<FInitBuffersVHM_CS> ComputeShader(InGlobalShaderMap);
<
<       FInitBuffersVHM_CS::FParameters* PassParameters = GraphBuilder.AllocParameters<FInitBuffersVHM_CS::FParameters>();
<       PassParameters->MaxLevel = InDesc.MaxLevel;
<       PassParameters->NumForceLoadLods = InDesc.NumForceLoadLods;
<       PassParameters->PageTableFeedbackId = InDesc.PageTableFeedbackId;
<       PassParameters->RWQueueInfo = InVolatileResources.QueueInfoUAV;
<       PassParameters->RWQueueBuffer = InVolatileResources.QueueBufferUAV;
<       PassParameters->RWIndirectArgsBuffer = InVolatileResources.IndirectArgsBufferUAV;
<       PassParameters->RWFeedbackBuffer = InVolatileResources.FeedbackBufferUAV;
< #pragma region S1_Engine_Shiyu
< //#if VHM_ENABLE_STAT
<       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;
< //#endif
< #pragma endregion
<
<       GraphBuilder.AddPass(
<           RDG_EVENT_NAME("InitBuffers"),
<           PassParameters,
<           ERDGPassFlags::Compute,
<           [PassParameters, ComputeShader](FRHICommandList& RHICmdList)
<       {
<           //todo: If feedback parsing understands append counter we don't need to fully clear
<           RHICmdList.ClearUAVUint(PassParameters->RWFeedbackBuffer->GetRHI(), FUintVector4(0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff));
<
<           FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *PassParameters, FIntVector(1, 1, 1));
<       });
<   }
<
<   /** Collect potentially visible quads and determine their Lods. */
<   void AddPass_CollectQuads(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, FProxyDesc const& InDesc, FVolatileResources& InVolatileResources, FMainViewDesc const& InViewDesc)
<   {
<       TShaderMapRef<FCollectQuadsVHM_CS> ComputeShader(InGlobalShaderMap);
<
<       FCollectQuadsVHM_CS::FParameters* PassParameters = GraphBuilder.AllocParameters<FCollectQuadsVHM_CS::FParameters>();
<       PassParameters->HeightMinMaxTexture = InDesc.HeightMinMaxTexture;
<       PassParameters->LodBiasMinMaxTexture = InDesc.LodBiasMinMaxTexture;
<       PassParameters->MinMaxTextureSampler = TStaticSamplerState<SF_Point>::GetRHI();
<       PassParameters->MinMaxLevelOffset = InDesc.MinMaxLevelOffset;
<       PassParameters->OcclusionTexture = InViewDesc.OcclusionTexture;
<       PassParameters->OcclusionLevelOffset = InViewDesc.OcclusionLevelOffset;
<       PassParameters->PageTableTexture = InDesc.PageTableTexture;
<       PassParameters->MaxLevel = InDesc.MaxLevel;
<       PassParameters->RVTMinLevel = InDesc.RVTMinLevel;
<       PassParameters->PageTableSize = FVector4f(InDesc.PageTableSize); // LWC_TODO: precision loss
<       PassParameters->PageTableFeedbackId = InDesc.PageTableFeedbackId;
<       PassParameters->UVToWorld = FMatrix44f(InDesc.UVToWorld);       // LWC_TODO: Precision loss
<       PassParameters->UVToWorldScale = (FVector3f)InDesc.UVToWorldScale;
<       PassParameters->ViewOrigin = (FVector3f)InViewDesc.ViewOrigin;
<       PassParameters->LodDistances = FVector4f(InViewDesc.LodDistances); // LWC_TODO: precision loss
<       PassParameters->LodBiasScale = InViewDesc.LodBiasScale;
<       for (int32 PlaneIndex = 0; PlaneIndex < 5; ++PlaneIndex)
<       {
<           PassParameters->FrustumPlanes[PlaneIndex] = FVector4f(InViewDesc.Planes[PlaneIndex]); // LWC_TODO: precision loss
<       }
<       PassParameters->QueueBufferSizeMask = InDesc.MaxPersistentQueueItems - 1; // Assumes MaxPersistentQueueItems is a power of 2 so that we can wrap with a mask.
<       PassParameters->RWQueueInfo = InVolatileResources.QueueInfoUAV;
<       PassParameters->RWQueueBuffer = InVolatileResources.QueueBufferUAV;
<       PassParameters->RWQuadBuffer = InVolatileResources.QuadBufferUAV;
<       PassParameters->RWIndirectArgsBuffer = InVolatileResources.IndirectArgsBufferUAV;
<       PassParameters->RWFeedbackBuffer = InVolatileResources.FeedbackBufferUAV;
< #pragma region S1_Engine_Shiyu
< //#if VHM_ENABLE_STAT
<       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;
< //#endif
< #pragma endregion
<
<       FComputeShaderUtils::AddPass(
<           GraphBuilder,
<           RDG_EVENT_NAME("CollectQuads"),
<           ComputeShader, PassParameters, FIntVector(InDesc.NumCollectPassWavefronts, 1, 1));
<   }
<
<   /** Initialise the draw indirect buffer. */
<   void AddPass_InitInstanceBuffer(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, int32 NumInstanceVertexSide, FDrawInstanceBuffers& InOutputResources)
<   {
<       TShaderMapRef<FInitInstanceBufferVHM_CS> ComputeShader(InGlobalShaderMap);
<
<       FInitInstanceBufferVHM_CS::FParameters* PassParameters = GraphBuilder.AllocParameters<FInitInstanceBufferVHM_CS::FParameters>();
<       PassParameters->NumIndices = NumInstanceVertexSide * NumInstanceVertexSide * 6;
<       PassParameters->RWIndirectArgsBuffer = InOutputResources.IndirectArgsBufferUAV;
<
<       FComputeShaderUtils::AddPass(
<           GraphBuilder,
<           RDG_EVENT_NAME("InitInstanceBuffer"),
<           ComputeShader, PassParameters, FIntVector(1, 1, 1));
<   }
<
<   /** Cull quads and write to the final output buffer. */
<   void AddPass_CullInstances(FRDGBuilder& Gra

... [diff truncated to 80KB; full diff in vhm_diffs/93165.diff] ...
```

#### CL 110749 — 2024/08/08 — 郭智均

- **提交说明**：--story=1023328 --user=郭智均 【CBT】白垩毒圈剔除逻辑重构 https://www.tapd.cn/68880148/s/1600598
- **TAPD**：story=1023328
- **涉及 VHM 文件**：5 个

**做了什么**：

提交目的：【CBT】白垩毒圈剔除逻辑重构 https://www.tapd.cn/68880148/s/1600598

- **Shader**：2 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (edit)
- **Runtime C++**：3 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h` (edit)

📄 查看 VHM 相关 diff（CL 110749）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#6 (text) ====

77,78c77,79
<

<   const uint GeoToTexLevelOffset = max(int(InRVTMinLevel) - int(Item.Level), 0); // geometry levels is large than tex levels

---
>

>   const int TmpLevel = max(int(Item.Level) - VHMParam.ExtSubdivisionLevel, 0);

>   const uint GeoToTexLevelOffset = max(int(InRVTMinLevel) - TmpLevel, 0) + max(0, VHMParam.ExtSubdivisionLevel - (int)Item.Level); // geometry levels is large than tex levels

81c82
<   const uint TextureLevel = max(int(Item.Level) - int(InRVTMinLevel), 0);

---
>   const uint TextureLevel = max(TmpLevel - int(InRVTMinLevel), 0);

93c94
<   Info.SampleTextureLevel = min(Item.Level, VHMParam.MaxLevel - InRVTMinLevel);

---
>   Info.SampleTextureLevel = max(0, int(min(Item.Level, VHMParam.MaxLevel - InRVTMinLevel)) - VHMParam.ExtSubdivisionLevel);

95c96
<   const uint SampleGeoToTexLevelOffset = min(InRVTMinLevel, VHMParam.MaxLevel - Item.Level);

---
>   const uint SampleGeoToTexLevelOffset = min(InRVTMinLevel, VHMParam.MaxLevel - Item.Level) + max(0, (int)VHMParam.ExtSubdivisionLevel - (int)Item.Level);

96a98
>

228c230
<   const bool bMaskCull = MaskValue < CLIP_MASK && false; // ignore MaskCull

---
>   const bool bMaskCull = MaskValue < CLIP_MASK; // && false; // ignore MaskCull

774,812d775
<   // Instance.UVTransform = GetVirtualToPhysicalUVTransform(ThisInfo.Pos,

<   //  ThisInfo.GeoToTexLevelOffsetInv,

<   //  ThisInfo.TextureLevel,

<   //  ThisInfo.PhysicalAddress[0],

<   //  VHMParam.PhysicalPageTransform, VHMParam.NumPhysicalAddressBits);

<   // // Par

<   // {

<   //  uint4 ParPackedValue;

<   //  {

<   //      ParPackedValue.x = MortonEncode(ThisInfo.Pos >> 1);

<   //      uint ParLevel = min(ThisInfo.Level + 1, VHMParam.MaxLevel);

<   //      ParPackedValue.x = ParPackedValue.x | (ParLevel << 24);

<   //  }

<   //  SQuadInfo ParentInfo = GetQuadInfo(ParPackedValue, VHMParam.RVTMinLevel);

<   //  GetPhysicalAddress(ParentInfo);

<   //  Instance.UVTransformPar = GetVirtualToPhysicalUVTransform(

<   //      ParentInfo.Pos,

<   //      ParentInfo.GeoToTexLevelOffsetInv,

<   //      ParentInfo.TextureLevel,

<   //      ParentInfo.PhysicalAddress[0],

<   //      VHMParam.PhysicalPageTransform, VHMParam.NumPhysicalAddressBits);

<   // }

<   // // Par Par

<   // {

<   //  uint4 ParPackedValue;

<   //  {

<   //      ParPackedValue.x = MortonEncode(ThisInfo.Pos >> 2);

<   //      uint ParLevel = min(ThisInfo.Level + 2, VHMParam.MaxLevel);

<   //      ParPackedValue.x = ParPackedValue.x | (ParLevel << 24);

<   //  }

<   //  SQuadInfo ParentInfo = GetQuadInfo(ParPackedValue, VHMParam.RVTMinLevel);

<   //  GetPhysicalAddress(ParentInfo);

<   //  Instance.UVTransformPar2 = GetVirtualToPhysicalUVTransform(

<   //      ParentInfo.Pos,

<   //      ParentInfo.GeoToTexLevelOffsetInv,

<   //      ParentInfo.TextureLevel,

<   //      ParentInfo.PhysicalAddress[0],

<   //      VHMParam.PhysicalPageTransform, VHMParam.NumPhysicalAddressBits);

<   // }

873,874c836
<   SQuadInfo ThisInfo;

<   GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

---
>   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#7 (text) ====

12a13,15
> #ifndef DEBUG_WITH_MORPH_VERTEX

> #define DEBUG_WITH_MORPH_VERTEX 1

> #endif

13a17
>

138,139c142,143
<   float SampleLevel = min(Level, MaxSampleLevel);

<   const uint SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - Level);

---
>   float SampleLevel = max(0, float(min(Level, MaxSampleLevel)) - VHM.ExtSubdivisionLevel);

>   const uint SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - Level) + max(0, (int)VHM.ExtSubdivisionLevel - (int)Level);

156,185d159
<   {

<       uint ThisLevel = min(Level + 1, VHM.MaxLod);

<       const uint _SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - ThisLevel);

<       float _SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << _SampleGeoToTexLevelOffset);

<       float _SampleLevel = min(ThisLevel, MaxSampleLevel);

<       UVTransform[1] = GetVirtualToPhysicalUVTransform(

<           Pos >> 1,

<           // _GeoOffsetInv,

<           // _TexLevel,

<           _SampleGeoToTexLevelOffsetInv, _SampleLevel,

<           Item.PhysicalAddress[1],

<           VHM.PhysicalPageTransform,

<           VHM.NumPhysicalAddressBits

<       );

<   }

<   {

<       uint ThisLevel = min(Level + 2, VHM.MaxLod);

<       const uint _SampleGeoToTexLevelOffset = min(RVT_MIN_LEVEL, VHM.MaxLod - ThisLevel);

<       float _SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << _SampleGeoToTexLevelOffset);

<       float _SampleLevel = min(ThisLevel, MaxSampleLevel);

<       UVTransform[2] = GetVirtualToPhysicalUVTransform(

<           Pos >> 2,

<           // _GeoOffsetInv,

<           // _TexLevel,

<           _SampleGeoToTexLevelOffsetInv, _SampleLevel,

<           Item.PhysicalAddress[2],

<           VHM.PhysicalPageTransform,

<           VHM.NumPhysicalAddressBits

<       );

<   }

189c163,164
<

---
>

> #if DEBUG_WITH_MORPH_VERTEX

190a166,167
>   if (!VHM.CloseMorphVertexForDebug){

> #else

191a169
> #endif

225c203
<       SampleLevel = min(max(0, LodClamped - 0.5f), MaxSampleLevel);

---
>       SampleLevel = max(0, min(max(0, LodClamped - 0.5f), (float)MaxSampleLevel) - VHM.ExtSubdivisionLevel);

227a206
>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#16 (unicode) ====

98a99,105
> static TAutoConsoleVariable<int32> CVarCloseMorphVertexForDebug(

>   TEXT("r.VHM.CloseMorphVertexForDebug"),

>   0,

>   TEXT(""),

>   ECVF_RenderThreadSafe

> );

>

325c332
<       const uint32 MaxLevel = FMath::Max(InProxy->RVTMaxLevel - InProxy->Lod0LevelBias, 0);

---
>       const uint32 MaxLevel = FMath::Max(InProxy->MaxLevel - InProxy->Lod0LevelBias, 0);

358a366
>           SHADER_PARAMETER(int32,             ExtSubdivisionLevel)

664c672
<   , RVTMaxLevel(0)

---
>   , MaxLevel(0)

760,761c768,770
<               NumInstanceVertexSide = 1 << (TileSize + ExtSubdivisionLevel - NumQuadsPerTileOfTwo);

<               RVTMaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo;

---
>               RVTMinLevel = NumQuadsPerTileOfTwo;

>               MaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo + ExtSubdivisionLevel;

>               NumInstanceVertexSide = 1 << (TileSize - NumQuadsPerTileOfTwo);

771,772c780,781
<                   UniformParams.MaxLod = RVTMaxLevel;

<                   UniformParams.RVTMinLevel = NumQuadsPerTileOfTwo;

---
>                   UniformParams.MaxLod = MaxLevel;

>                   UniformParams.RVTMinLevel = RVTMinLevel;

802a812,815
>               UniformParams.CloseMorphVertexForDebug = CVarCloseMorphVertexForDebug->GetInt();

>

>               UniformParams.ExtSubdivisionLevel = ExtSubdivisionLevel;

>

2260,2264c2273,2274
<       uint32 MaxLevel = FMath::FloorLog2(AllocatedVirtualTexture->GetWidthInTiles()) + Proxy->NumQuadsPerTileOfTwo; // width == height

<       uint32 TileCountSizeLevel = FMath::FloorLog2(AllocatedVirtualTexture->GetVirtualTileSize());

<       ProxyDesc.MaxLevel = MaxLevel;

<       check(TileCountSizeLevel + Proxy->ExtSubdivisionLevel >= Proxy->NumQuadsPerTileOfTwo);

<       ProxyDesc.RVTMinLevel = Proxy->NumQuadsPerTileOfTwo;

---
>       ProxyDesc.MaxLevel = Proxy->MaxLevel;

>       ProxyDesc.RVTMinLevel = Proxy->RVTMinLevel;

2275a2286
>       ProxyDesc.RVTMinLevel = Proxy->RVTMinLevel;

2469,2473c2480,2482
<       uint32 MaxLevel = FMath::FloorLog2(AllocatedVirtualTexture->GetWidthInTiles()) + Proxy->NumQuadsPerTileOfTwo; // width == height

<       Param.MaxLevel = MaxLevel;

<       uint32 TileCountSizeLevel = FMath::FloorLog2(AllocatedVirtualTexture->GetVirtualTileSize());

<       check(TileCountSizeLevel + Proxy->ExtSubdivisionLevel >= Proxy->NumQuadsPerTileOfTwo);

<       Param.RVTMinLevel = Proxy->NumQuadsPerTileOfTwo;

---
>       // uint32 MaxLevel = FMath::FloorLog2(AllocatedVirtualTexture->GetWidthInTiles()) + Proxy->NumQuadsPerTileOfTwo; // width == height

>       Param.MaxLevel = Proxy->MaxLevel;

>       Param.RVTMinLevel = Proxy->RVTMinLevel;

2496a2506,2507
>

>       Param.ExtSubdivisionLevel = Proxy->ExtSubdivisionLevel;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h#6 (unicode) ====

58a59
>   uint32 RVTMinLevel;
77c78
<   int32 RVTMaxLevel;
---
>   int32 MaxLevel;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h#4 (unicode) ====

32a33,34
>   SHADER_PARAMETER(int32, CloseMorphVertexForDebug)

>   SHADER_PARAMETER(int32, ExtSubdivisionLevel)
```

#### CL 120024 — 2024/08/16 — 郭智均

- **提交说明**：--story=1023328 --user=郭智均 【CBT】地形细分加入动态开关 https://www.tapd.cn/68880148/s/1618155
- **TAPD**：story=1023328
- **涉及 VHM 文件**：4 个

**做了什么**：

提交目的：【CBT】地形细分加入动态开关 https://www.tapd.cn/68880148/s/1618155

- **Runtime C++**：4 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (edit)
- `Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h` (edit)

📄 查看 VHM 相关 diff（CL 120024）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#12 (unicode) ====

29a30,36
>
>   static TAutoConsoleVariable<int32> CVarEnableExtSubdivisionLevel(
>       TEXT("r.VHM.EnableExtSubdivisionLevel"),
>       1,
>       TEXT(""),
>       ECVF_RenderThreadSafe
>   );
75a83,94
>
>       const bool bEnableExtSubdivisionLevel = CVarEnableExtSubdivisionLevel.GetValueOnGameThread() != 0;
>       static bool bLastEnableExtSubdivisionLevel = !bEnableExtSubdivisionLevel;
>       if (bEnable && bEnableExtSubdivisionLevel != bLastEnableExtSubdivisionLevel)
>       {
>           bLastEnableExtSubdivisionLevel = bEnableExtSubdivisionLevel;
>           for (TObjectIterator<UVirtualHeightfieldMeshComponent> It; It; ++It)
>           {
>               It->SetEnableExtSubdivisionLevel(bEnableExtSubdivisionLevel);
>               It->MarkRenderStateDirty();
>           }
>       }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#17 (unicode) ====

1,2802c1,2808
< // Copyright Epic Games, Inc. All Rights Reserved.

<

< #include "VirtualHeightfieldMeshSceneProxy.h"

<

< #include "DataDrivenShaderPlatformInfo.h"

< #include "EngineModule.h"

< #include "Engine/Engine.h"

< #include "Engine/Texture2D.h"

< #include "GlobalRenderResources.h"

< #include "GlobalShader.h"

< #include "HeightfieldMaskTexture.h"

< #include "HeightfieldMinMaxTexture.h"

< #include "HLSLTypeAliases.h"

< #include "MaterialDomain.h"

< #include "Materials/Material.h"

< #include "Materials/MaterialRenderProxy.h"

< #include "PrimitiveViewRelevance.h"

< #include "RenderCaptureInterface.h"

< #include "RHIStaticStates.h"

< #include "RenderGraphUtils.h"

< #include "RHIGPUReadback.h"

< #include "SceneInterface.h"

< #include "SystemTextures.h"

< #include "TextureResource.h"

< #include "VirtualHeightfieldMeshComponent.h"

< #include "VirtualHeightfieldMeshVertexFactory.h"

< #include "VT/RuntimeVirtualTexture.h"

< #include "VT/VirtualTextureFeedbackBuffer.h"

<

<

< DECLARE_STATS_GROUP(TEXT("VirtualHeightfieldMesh"), STATGROUP_VirtualHeightfieldMesh, STATCAT_Advanced);

< DECLARE_CYCLE_STAT(TEXT("VirtualHeightfieldMesh SubmitWork"), STAT_VirtualHeightfieldMesh_SubmitWork, STATGROUP_VirtualHeightfieldMesh);

<

< DECLARE_LOG_CATEGORY_EXTERN(LogVirtualHeightfieldMesh, Warning, All);

< DEFINE_LOG_CATEGORY(LogVirtualHeightfieldMesh);

<

< static TAutoConsoleVariable<float> CVarVHMLodScale(

<   TEXT("r.VHM.LodScale"),

<   1.f,

<   TEXT("Global LOD scale applied for Virtual Heightfield Mesh."),

<   ECVF_RenderThreadSafe

< );

<

< // We disable View.LODDistanceFactor by default.

< // When it is set according to GCalcLocalPlayerCachedLODDistanceFactor in ULocalPlayer we end up with double couting of the FOV scale.

< // Ideally we would remove the calculation in ULocalPlayer and View.LODDistanceFactor would be only for view specific adjustments (screen captures etc.)

< // However the removal of the code in ULocalPlayer could have a big impact on any preexisting data in any project.

< static TAutoConsoleVariable<int32> CVarVHMEnableViewLodFactor(

<   TEXT("r.VHM.EnableViewLodFactor"),

<   0,

<   TEXT("Enable the View.LODDistanceFactor.")

<   TEXT("This is disabled by default to avoid an issue where FOV is double counted when calculating Lods.")

<   TEXT("See comment in code for more information."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMOcclusion(

<   TEXT("r.VHM.Occlusion"),

<   1,

<   TEXT("Enable occlusion queries."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxRenderItems(

<   TEXT("r.VHM.MaxRenderInstances"),

<   1024 * 64,

<   TEXT("Size of buffers used to collect render instances."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxFeedbackItems(

<   TEXT("r.VHM.MaxFeedbackItems"),

<   1024 * 4 * 10, // pre node write 10 time

<   TEXT("Size of buffer used by virtual texture feedback."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMMaxPersistentQueueItems(

<   TEXT("r.VHM.MaxPersistentQueueItems"),

<   1024 * 64,

<   TEXT("Size of queue used in the collect pass. This is rounded to the nearest power of 2."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMCollectPassWavefronts(

<   TEXT("r.VHM.CollectPassWavefronts"),

<   1,

<   TEXT("Number of wavefronts to use for collect pass."),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarVHMVersion(

<   TEXT("r.VHM.Version"),

<   3,

<   TEXT("Version Of VHM"),

<   ECVF_RenderThreadSafe

< );

<

< static TAutoConsoleVariable<int32> CVarCloseMorphVertexForDebug(

<   TEXT("r.VHM.CloseMorphVertexForDebug"),

<   0,

<   TEXT(""),

<   ECVF_RenderThreadSafe

< );

<

< #pragma region S1_Engine_Shiyu

< #if UE_BUILD_SHIPPING || UE_BUILD_TEST

< #define VHM_ENABLE_STAT 0

< #else

< #define VHM_ENABLE_STAT 1

< #endif

<

< #if VHM_ENABLE_STAT

< #include "Stats/Stats2.h"

< #include "Stats/StatsMisc.h"

<

< DECLARE_STATS_GROUP(TEXT("VHM"), STATGROUP_VHM, STATCAT_Advanced);

<

< DECLARE_DWORD_COUNTER_STAT(TEXT("BeforeCullInstances"), STAT_VHM_BeforeCullInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawTriangles"), STAT_VHM_DrawTriangles, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-ALL"), STAT_VHM_DrawInstancesALL, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Opacity"), STAT_VHM_DrawOpacityInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-Mask"), STAT_VHM_DrawMaskInstances, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD0"), STAT_VHM_DrawInstancesLOD0, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD1"), STAT_VHM_DrawInstancesLOD1, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD2"), STAT_VHM_DrawInstancesLOD2, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD3"), STAT_VHM_DrawInstancesLOD3, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD4"), STAT_VHM_DrawInstancesLOD4, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD5"), STAT_VHM_DrawInstancesLOD5, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD6"), STAT_VHM_DrawInstancesLOD6, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD7"), STAT_VHM_DrawInstancesLOD7, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD8"), STAT_VHM_DrawInstancesLOD8, STATGROUP_VHM)

< DECLARE_DWORD_COUNTER_STAT(TEXT("DrawInstances-LOD9"), STAT_VHM_DrawInstancesLOD9, STATGROUP_VHM)

<

<

< static TAutoConsoleVariable<int32> CVarVHMEnableStat(

<   TEXT("r.VHM.StatEnable"),

<   0,

<   TEXT("Whether VHM open Stat."),

<   ECVF_RenderThreadSafe

< );

< #endif

<

< #pragma endregion

<

< static constexpr int32 IndirectArgsCount = 10;

< static constexpr int32 IndirectArgsByteSize = 4 * sizeof(uint32) * IndirectArgsCount;

< static constexpr int32 MergeDispatchArgsOffset = 5;

< #pragma region S1_Engine_Shiyu

< // #if VHM_ENABLE_STAT

< static constexpr int32 MaxStatCount = 64;

< static constexpr int32 StatBufferByteSize = sizeof(uint32) * MaxStatCount;

< // #endif

< #pragma endregion

<

<

< namespace VirtualHeightfieldMesh

< {

<   /** Buffers filled by GPU culling used by the Virtual Heightfield Mesh final draw call. */

<   struct FDrawInstanceBuffers

<   {

<       /* Culled instance buffer. */

<       FBufferRHIRef InstanceBuffer;

<       FUnorderedAccessViewRHIRef InstanceBufferUAV;

<       FShaderResourceViewRHIRef InstanceBufferSRV;

<

<       /* IndirectArgs buffer for final DrawInstancedIndirect. */

<       FBufferRHIRef IndirectArgsBuffer;

<       FUnorderedAccessViewRHIRef IndirectArgsBufferUAV;

<

< #pragma region S1_Engine_Shiyu

<       /* Culled hold instance buffer */

<       FBufferRHIRef HoleInstanceBuffer;

<       FUnorderedAccessViewRHIRef HoleInstanceBufferUAV;

<       FShaderResourceViewRHIRef HoleInstanceBufferSRV;

<

< //#if VHM_ENABLE_STAT

<       static constexpr uint32 MaxReadBackSize = 4;

<       /** For Stat  */

<       TArray<TUniquePtr<FRHIGPUBufferReadback>> StatBufferReadBacks;

< //#endif

< #pragma endregion

<   };

<

<   /** Initialize the FDrawInstanceBuffers objects. */

<   void InitializeInstanceBuffers(FRHICommandListImmediate& InRHICmdList, FDrawInstanceBuffers& InBuffers);

<

<   /** Release the FDrawInstanceBuffers objects. */

<   void ReleaseInstanceBuffers(FDrawInstanceBuffers& InBuffers)

<   {

<       InBuffers.InstanceBuffer.SafeRelease();

<       InBuffers.InstanceBufferUAV.SafeRelease();

<       InBuffers.InstanceBufferSRV.SafeRelease();

<       InBuffers.IndirectArgsBuffer.SafeRelease();

<       InBuffers.IndirectArgsBufferUAV.SafeRelease();

< #pragma region S1_Engine_Shiyu

<       InBuffers.HoleInstanceBuffer.SafeRelease();

<       InBuffers.HoleInstanceBufferUAV.SafeRelease();

<       InBuffers.HoleInstanceBufferSRV.SafeRelease();

< #if VHM_ENABLE_STAT

<       InBuffers.StatBufferReadBacks.Empty();

< #endif

< #pragma endregion

<   }

<

<   namespace V2

<   {

<       struct FInnerBuffers

<       {

<           // // for ps

<           // // - use to draw quad by default material

<           // FBufferRHIRef QuadInstanceArgsBuffer;

<           // FUnorderedAccessViewRHIRef QuadInstanceArgsBufferUAV;

<           // FBufferRHIRef QuadInstanceBuffer;

<           // FUnorderedAccessViewRHIRef QuadInstanceBufferUAV;

<           // FShaderResourceViewRHIRef QuadInstanceBufferSRV;

<           // // - use to draw quad by hole material

<           // FBufferRHIRef HoleQuadInstanceArgsBuffer;

<           // FUnorderedAccessViewRHIRef HoleQuadInstanceArgsBufferUAV;

<           // FBufferRHIRef HoleQuadInstanceBuffer;

<           // FUnorderedAccessViewRHIRef HoleQuadInstanceBufferUAV;

<           // FShaderResourceViewRHIRef HoleQuadInstanceBufferSRV;

<

<           int32 CalTime = -1;

<           // use to compure shader

<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};

<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};

<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};

<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};

<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};

<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};

<

<           FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

<           }

<           FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));

<           }

<

<           FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

<           }

<           FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const

<           {

<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);

<           }

<       };

<

<       void InitializeInnerBuffers(FRHICommandListImmediate& RHICmdList, FInnerBuffers& InBuffers)

<       {

<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();

<           const TCHAR* FinalQuadName[2] = {

<               TEXT("VHM.FinalQuadBuffer_0"),

<               TEXT("VHM.FinalQuadBuffer_1")};

<           const TCHAR* FinalQuadArgsName[2] = {

<               TEXT("VHM.FinalQuadArgsBuffer_0"),

<               TEXT("VHM.FinalQuadArgsBuffer_1")};

<

<           for (int i = 0; i < 2; ++i)

<           {

<               InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(

<                   FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),

<                   FinalQuadArgsName[i]

<               );

<               InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(

<                   FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)

<

<                   ,

<                   FinalQuadName[i]

<               );

<           }

<       }

<

<       void ReleaseInnerBuffers(FInnerBuffers& InBuffers)

<       {

<           InBuffers.CalTime = -1;

<           for(int i = 0; i < 2; ++i)

<           {

<               InBuffers.FinalQuadArgsBuffer[i].SafeRelease();

<               InBuffers.FinalQuadBuffer[i].SafeRelease();

<           }

<

<       }

<   }

<

< }

<

< struct FOcclusionResults

< {

<   FTexture2DRHIRef OcclusionTexture;

<   FIntPoint TextureSize;

<   int32 NumTextureMips;

<   TArray<bool> UploadData;

< };

<

< struct FOcclusionResultsKey

< {

<   FVirtualHeightfieldMeshSceneProxy const* Proxy;

<   FSceneView const* View;

<

<   FOcclusionResultsKey(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InView)

<       : Proxy(InProxy)

<       , View(InView)

<   {

<   }

<

<   friend inline uint32 GetTypeHash(const FOcclusionResultsKey& InKey)

<   {

<       return HashCombine(GetTypeHash(InKey.View), GetTypeHash(InKey.Proxy));

<   }

<

<   friend bool operator==(const FOcclusionResultsKey& A, const FOcclusionResultsKey& B)

<   {

<       return A.View == B.View && A.Proxy == B.Proxy;

<   }

< };

<

<

<

< /** Global map for occlusion result. */

< TMap< FOcclusionResultsKey, FOcclusionResults > GOcclusionResults;

< bool GOcclusionResetRequired = false;

<

< namespace VirtualHeightfieldMesh

< {

<   /** Calculate distances used for LODs in a given view for a given scene proxy. */

<   FVector4f CalculateLodRanges(FSceneView const* InView, FVirtualHeightfieldMeshSceneProxy const* InProxy)

<   {

<       const uint32 MaxLevel = FMath::Max(InProxy->MaxLevel - InProxy->Lod0LevelBias, 0);

<       const float Lod0UVSize = 1.f / (float)(1 << MaxLevel);

<       const FVector2D Lod0WorldSize = FVector2D(InProxy->UVToWorldScale.X, InProxy->UVToWorldScale.Y) * Lod0UVSize; // LWC_TODO: precision loss

<       const float Lod0WorldRadius = Lod0WorldSize.Size();

<       const float ScreenMultiple = FMath::Max(0.5f * InView->ViewMatrices.GetProjectionMatrix().M[0][0], 0.5f * InView->ViewMatrices.GetProjectionMatrix().M[1][1]);

<       const float Lod0Distance = Lod0WorldRadius * ScreenMultiple / InProxy->Lod0ScreenSize;

<       const float ViewLodDistanceFactor = CVarVHMEnableViewLodFactor.GetValueOnRenderThread() == 0 ? 1.f : InView->LODDistanceFactor;

<       const float LodScale = ViewLodDistanceFactor * CVarVHMLodScale.GetValueOnRenderThread();

<

<       return FVector4f(Lod0Distance, InProxy->Lod0Distribution, InProxy->LodDistribution, LodScale);

<   }

<

<

<   namespace V2

<   {

<       BEGIN_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters,)

<           SHADER_PARAMETER(FVector3f,         ViewOrigin)

<           SHADER_PARAMETER(uint32,            OutBufferSizeMask)

<           SHADER_PARAMETER(FVector3f,         UVToWorldScale)

<           SHADER_PARAMETER(uint32,            FinalQuadBufferSizeMask)

<           SHADER_PARAMETER_ARRAY(FVector4f,   FrustumPlanes, [5])

<           SHADER_PARAMETER(FMatrix44f,        UVToWorld)

<           SHADER_PARAMETER(FVector4f,         LodDistances)

<           SHADER_PARAMETER(uint32,            MaxLevel)

<           SHADER_PARAMETER(uint32,            RVTMinLevel)

<           SHADER_PARAMETER(uint32,            PageTableFeedbackId)

<           SHADER_PARAMETER(uint32,            NumPhysicalAddressBits)

<           SHADER_PARAMETER(FVector4f,         PageTableSize)

<           SHADER_PARAMETER(FVector4f,         PhysicalPageTransform)

<           SHADER_PARAMETER(uint32,            QuadInstanceBufferSizeMask)

<           SHADER_PARAMETER(uint32,            NumIndices)

<           SHADER_PARAMETER(uint32,            MaxArgsCount)

<           SHADER_PARAMETER(uint32,            MaxStatCount)

<           SHADER_PARAMETER(uint32,            MergeDispatchArgsOffset)

<           SHADER_PARAMETER(int32,             ExtSubdivisionLevel)

<       END_UNIFORM_BUFFER_STRUCT()

<

<       IMPLEMENT_UNIFORM_BUFFER_STRUCT(FVHMCSSharedParameters, "VHMParam")

<

<       struct FVolatileBuffers

<       {

<           FVHMCSSharedParameters* VHMParameter=nullptr;

<           TRDGUniformBufferRef<FVHMCSSharedParameters> VHMParameterUBuffer;

<           TArray<FRDGBufferRef, TFixedAllocator<2>> ArgsBuffer{nullptr, nullptr};

<           TArray<FRDGBufferRef, TFixedAllocator<2>> SubdivideQuadBuffer{nullptr, nullptr};

<           TArray<FRDGBufferRef, TFixedAllocator<2>> MergeQuadBuffer{nullptr, nullptr};

<

<

<           struct FSRVAndUAV

<           {

<               FRDGBufferSRVRef SRV = nullptr;

<               FRDGBufferUAVRef UAV = nullptr;

<               void Create(FRDGBuilder& GraphBuilder, FRDGBufferRef Buffer)

<               {

<                   EPixelFormat Format = uint32(Buffer->Desc.Usage & EBufferUsageFlags::DrawIndirect) != 0 ? PF_R32_UINT : PF_R32G32B32A32_UINT;

<                   SRV = GraphBuilder.CreateSRV(Buffer, Format);

<                   UAV = GraphBuilder.CreateUAV(Buffer, Format);

<               }

<           };

<           TArray<FSRVAndUAV, TFixedAllocator<2>> ArgsViews{{}, {}};

<           TArray<FSRVAndUAV, TFixedAllocator<2>> SubdivideViews{{}, {}};

<           TArray<FSRVAndUAV, TFixedAllocator<2>> MergeViews{{}, {}};

<

<           FRHITexture* PageTableTexture = nullptr;

<           FRHITexture* MaskTexture = nullptr;

<           FRHIShaderResourceView* HeightTexture = nullptr;

<           FRHITexture* HeightMinMaxTexture = nullptr;

<

< //#if VHM_ENABLE_STAT

<           FRDGBufferRef StatBuffer;

<           FRDGBufferUAVRef StatBufferUAV;

< //#endif

<       };

<   }

< }

<

< /** Renderer extension to manage the buffer pool and add hooks for GPU culling passes. */

< class FVirtualHeightfieldMeshRendererExtension : public FRenderResource

< {

< public:

<   FVirtualHeightfieldMeshRendererExtension()

<       : bInFrame(false)

<       , DiscardId(0)

<   {}

<

<   virtual ~FVirtualHeightfieldMeshRendererExtension()

<   {}

<

<   /** Call once to register this extension. */

<   void RegisterExtension();

<

<   /** Are we inside a BeginFrame()/EndFrame() scope? */

<   bool IsInFrame() { return bInFrame; }

<

<   /** Call once per frame for each mesh/view that has relevance. This allocates the buffers to use for the frame and adds the work to fill the buffers to the queue. */

<   VirtualHeightfieldMesh::FDrawInstanceBuffers& AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView);

<   /** Submit all the work added by AddWork(). The work fills all of the buffers ready for use by the referencing mesh batches. */

<   void SubmitWork(FRDGBuilder& GraphBuilder);

<

<   void InitVolatileBuffers(FRDGBuilder& GraphBuilder, int WorkIndex, VirtualHeightfieldMesh::V2::FVolatileBuffers& VolatileBuffers);

<

<   // void SubmitWork_V2(FRDGBuilder& GraphBuilder);

<

<   void SubmitWork_V3(FRDGBuilder& GraphBuilder);

<

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   void CollectStat();

< #endif

< #pragma endregion

<

< protected:

<   //~ Begin FRenderResource Interface

<   virtual void ReleaseRHI() override;

<   //~ End FRenderResource Interface

<

< private:

<   /** Called by renderer at start of render frame. */

<   void BeginFrame(FRDGBuilder& GraphBuilder);

<   /** Called by renderer at end of render frame. */

<   void EndFrame(FRDGBuilder& GraphBuilder);

<   void EndFrame();

<

<

<   /** Flag for frame validation. */

<   bool bInFrame;

<

<   /** Buffers to fill. Resources can persist between frames to reduce allocation cost, but contents don't persist. */

<   TArray<VirtualHeightfieldMesh::FDrawInstanceBuffers> Buffers;

<   TArray<VirtualHeightfieldMesh::V2::FInnerBuffers> InnerBuffers;

<   /** Per buffer frame time stamp of last usage. */

<   TArray<uint32> DiscardIds;

<   /** Current frame time stamp. */

<   uint32 DiscardId;

<

<   /** Arrary of uniqe scene proxies to render this frame. */

<   TArray<FVirtualHeightfieldMeshSceneProxy const*> SceneProxies;

<   /** Arrary of unique main views to render this frame. */

<   TArray<FSceneView const*> MainViews;

<   /** Arrary of unique culling views to render this frame. */

<   TArray<FSceneView const*> CullViews;

<

<   /** Key for each buffer we need to generate. */

<   struct FWorkDesc

<   {

<       int32 ProxyIndex;

<       int32 MainViewIndex;

<       int32 CullViewIndex;

<       int32 BufferIndex;

<   };

<

<   /** Keys specifying what to render. */

<   TArray<FWorkDesc> WorkDescs;

<

<   /** Sort predicate for FWorkDesc. When rendering we want to batch work by proxy, then by main view. */

<   struct FWorkDescSort

<   {

<       uint32 SortKey(FWorkDesc const& WorkDesc) const

<       {

<           return (WorkDesc.ProxyIndex << 24) | (WorkDesc.MainViewIndex << 16) | (WorkDesc.CullViewIndex << 8) | WorkDesc.BufferIndex;

<       }

<

<       bool operator()(FWorkDesc const& A, FWorkDesc const& B) const

<       {

<           return SortKey(A) < SortKey(B);

<       }

<   };

<

<   // all vhm use one feedback buffer;

<   FRDGBufferRef VTFeedbackBuf;

<   FRDGBufferUAVRef VTFeedbackBufUAV;

< };

<

< /** Single global instance of the VirtualHeightfieldMesh renderer extension. */

< TGlobalResource< FVirtualHeightfieldMeshRendererExtension > GVirtualHeightfieldMeshViewRendererExtension;

<

< void FVirtualHeightfieldMeshRendererExtension::RegisterExtension()

< {

<   static bool bInit = false;

<   if (!bInit && GEngine)

<   {

<       GEngine->GetPreRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::BeginFrame);

<       GEngine->GetPostRenderDelegateEx().AddRaw(this, &FVirtualHeightfieldMeshRendererExtension::EndFrame);

<       bInit = true;

<   }

< }

<

< void FVirtualHeightfieldMeshRendererExtension::ReleaseRHI()

< {

<   Buffers.Empty();

< }

<

< VirtualHeightfieldMesh::FDrawInstanceBuffers& FVirtualHeightfieldMeshRendererExtension::AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView)

< {

<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.

<   if (!ensure(!bInFrame))

<   {

<       EndFrame();

<   }

<

<   // Create workload

<   FWorkDesc WorkDesc;

<   WorkDesc.ProxyIndex = SceneProxies.AddUnique(InProxy);

<   WorkDesc.MainViewIndex = MainViews.AddUnique(InMainView);

<   WorkDesc.CullViewIndex = CullViews.AddUnique(InCullView);

<   WorkDesc.BufferIndex = -1;

<

<   // Check for an existing duplicate

<   for (FWorkDesc& It : WorkDescs)

<   {

<       if (It.ProxyIndex == WorkDesc.ProxyIndex && It.MainViewIndex == WorkDesc.MainViewIndex && It.CullViewIndex == WorkDesc.CullViewIndex && It.BufferIndex != -1)

<       {

<           WorkDesc.BufferIndex = It.BufferIndex;

<           break;

<       }

<   }

<

<   // Try to recycle a buffer

<   if (WorkDesc.BufferIndex == -1)

<   {

<       for (int32 BufferIndex = 0; BufferIndex < Buffers.Num(); BufferIndex++)

<       {

<           if (DiscardIds[BufferIndex] < DiscardId)

<           {

<               DiscardIds[BufferIndex] = DiscardId;

<               WorkDesc.BufferIndex = BufferIndex;

<               WorkDescs.Add(WorkDesc);

<               break;

<           }

<       }

<   }

<

<   // Allocate new buffer if necessary

<   if (WorkDesc.BufferIndex == -1)

<   {

<       DiscardIds.Add(DiscardId);

<       WorkDesc.BufferIndex = Buffers.AddDefaulted();

<       InnerBuffers.AddDefaulted(); // index is equal to BufferIndex

<       WorkDescs.Add(WorkDesc);

<       VirtualHeightfieldMesh::InitializeInstanceBuffers(GetImmediateCommandList_ForRenderCommand(), Buffers[WorkDesc.BufferIndex]);

<       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(GetImmediateCommandList_ForRenderCommand(), InnerBuffers[WorkDesc.BufferIndex]);

<   }

<

<   return Buffers[WorkDesc.BufferIndex];

< }

<

< void FVirtualHeightfieldMeshRendererExtension::BeginFrame(FRDGBuilder& GraphBuilder)

< {

<   // If we hit this then BegineFrame()/EndFrame() logic needs fixing in the Scene Renderer.

<   if (!ensure(!bInFrame))

<   {

<       EndFrame();

<   }

<   bInFrame = true;

<

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   CollectStat();

< #endif

< #pragma endregion

<

<   if (WorkDescs.Num() > 0)

<   {

<       uint32 VHMVersion = CVarVHMVersion.GetValueOnRenderThread();

<       if (VHMVersion == 1)

<       {

<           SubmitWork(GraphBuilder);

<       }

<       else if(VHMVersion == 2)

<       {

<           // SubmitWork_V2(GraphBuilder);

<       }

<       else

<       {

<           SubmitWork_V3(GraphBuilder);

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshRendererExtension::EndFrame()

< {

<   ensure(bInFrame);

<   bInFrame = false;

<

<   SceneProxies.Reset();

<   MainViews.Reset();

<   CullViews.Reset();

<   WorkDescs.Reset();

<

<   // Clean the buffer pool

<   DiscardId++;

<

<   for (int32 Index = 0; Index < DiscardIds.Num();)

<   {

<       if (DiscardId - DiscardIds[Index] > 4u)

<       {

<           VirtualHeightfieldMesh::ReleaseInstanceBuffers(Buffers[Index]);

<           VirtualHeightfieldMesh::V2::ReleaseInnerBuffers(InnerBuffers[Index]);

<           Buffers.RemoveAtSwap(Index);

<           InnerBuffers.RemoveAtSwap(Index);

<           DiscardIds.RemoveAtSwap(Index);

<       }

<       else

<       {

<           ++Index;

<       }

<   }

<

<   GOcclusionResetRequired = true;

< }

<

< void FVirtualHeightfieldMeshRendererExtension::EndFrame(FRDGBuilder& GraphBuilder)

< {

<   EndFrame();

< }

<

< const static FName NAME_VirtualHeightfieldMesh(TEXT("VirtualHeightfieldMesh"));

<

< FVirtualHeightfieldMeshSceneProxy::FVirtualHeightfieldMeshSceneProxy(UVirtualHeightfieldMeshComponent* InComponent)

<   : FPrimitiveSceneProxy(InComponent, NAME_VirtualHeightfieldMesh)

<   , bHiddenInEditor(InComponent->GetHiddenInEditor())

<   , RuntimeVirtualTexture(InComponent->GetVirtualTexture())

<   , HeightMinMaxTexture(nullptr)

<   , LodBiasTexture(nullptr)

<   , LodBiasMinMaxTexture(nullptr)

< #pragma region S1_Engine_Shiyu

<   , MaskTexture(nullptr)

< #pragma endregion

<   , AllocatedVirtualTexture(nullptr)

<   , bCallbackRegistered(false)

<   , NumQuadsPerTileOfTwo(InComponent->GetNumQuadPerTileOfTwo()) // (1 << 4) * (1 << 4)

<   , VertexFactory(nullptr)

<   , Lod0ScreenSize(InComponent->GetLod0ScreenSize())

<   , Lod0Distribution(InComponent->GetLod0Distribution())

<   , LodDistribution(InComponent->GetLodDistribution())

<   , LodBiasScale(InComponent->GetLodBiasScale())

<   , NumForceLoadLods(InComponent->GetNumForceLoadLods())

<   , NumOcclusionLods(0)

<   , ExtSubdivisionLevel(InComponent->GetExtSubdivisionLevel())

<   , OcclusionGridSize(0, 0)

<   , MaxLevel(0)

<   , Lod0LevelBias(InComponent->GetLod0LevelBias())

< {

<   // maybe not in RenderThread

<   // GVirtualHeightfieldMeshViewRendererExtension.RegisterExtension();

<

<   // They have some LOD, but considered static as the LODs (are intended to) represent the same static surface.

<   bHasDeformableMesh = false;

<

<   UMaterialInterface* ComponentMaterial = InComponent->GetMaterial();

<   const bool bValidMaterial = ComponentMaterial != nullptr && ComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);

<   Material = bValidMaterial ? ComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();

<   MaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());

<

<   const FTransform VirtualTextureTransform = InComponent->GetVirtualTextureTransform();

<

<   UVToWorldScale = VirtualTextureTransform.GetScale3D();

<   UVToWorld = VirtualTextureTransform.ToMatrixWithScale();

<

<   WorldToUV = UVToWorld.Inverse();

<   WorldToUVTransposeAdjoint = WorldToUV.TransposeAdjoint();

<

<   // UVToLocal will be initialized in OnTransformChanged() called immediately after construction.

<   UVToLocal = FMatrix::Identity;

<

<   UHeightfieldMinMaxTexture* HeightfieldMinMaxTexture = InComponent->GetMinMaxTexture();

<   if (HeightfieldMinMaxTexture != nullptr)

<   {

<       HeightMinMaxTexture = HeightfieldMinMaxTexture->Texture;

<       BuildOcclusionVolumes(HeightfieldMinMaxTexture->TextureData, HeightfieldMinMaxTexture->TextureDataSize, HeightfieldMinMaxTexture->TextureDataMips, InComponent->GetNumOcclusionLods());

<

<       LodBiasTexture = HeightfieldMinMaxTexture->LodBiasTexture;

<       LodBiasMinMaxTexture = HeightfieldMinMaxTexture->LodBiasMinMaxTexture;

<   }

<

< #pragma region S1_Engine_Shiyu

<   UMaterialInterface* HoleComponentMaterial = InComponent->GetHoleMaterial();

<   const bool bValidHoleMaterial = HoleComponentMaterial != nullptr && HoleComponentMaterial->CheckMaterialUsage_Concurrent(MATUSAGE_VirtualHeightfieldMesh);

<   HoleMaterial = bValidHoleMaterial ? HoleComponentMaterial->GetRenderProxy() : UMaterial::GetDefaultMaterial(MD_Surface)->GetRenderProxy();

<   HoleMaterialRelevance = Material->GetMaterialInterface()->GetRelevance_Concurrent(GetScene().GetFeatureLevel());

<

<   UHeightfieldMaskTexture* HeightfieldMaskTexture = InComponent->GetMaskTexture();

<   if (HeightfieldMaskTexture)

<   {

<       MaskTexture = HeightfieldMaskTexture->Texture;

<   }

< #pragma endregion

< }

<

<

< void FVirtualHeightfieldMeshSceneProxy::RegisterExternal()

< {

<   GVirtualHeightfieldMeshViewRendererExtension.RegisterExtension();

< }

<

< SIZE_T FVirtualHeightfieldMeshSceneProxy::GetTypeHash() const

< {

<   static size_t UniquePointer;

<   return reinterpret_cast<size_t>(&UniquePointer);

< }

<

< uint32 FVirtualHeightfieldMeshSceneProxy::GetMemoryFootprint() const

< {

<   return(sizeof(*this) + FPrimitiveSceneProxy::GetAllocatedSize());

< }

<

< void FVirtualHeightfieldMeshSceneProxy::OnTransformChanged()

< {

<   UVToLocal = UVToWorld * GetLocalToWorld().Inverse();

<

<   // Setup a default occlusion volume array containing just the primitive bounds.

<   // We use this if disabling the full set of occlusion volumes.

<   DefaultOcclusionVolumes.Reset();

<   DefaultOcclusionVolumes.Add(GetBounds());

< }

<

< void FVirtualHeightfieldMeshSceneProxy::CreateRenderThreadResources()

< {

<   if (RuntimeVirtualTexture != nullptr)

<   {

<       if (!bCallbackRegistered)

<       {

<           GetRendererModule().AddVirtualTextureProducerDestroyedCallback(RuntimeVirtualTexture->GetProducerHandle(), &OnVirtualTextureDestroyedCB, this);

<           bCallbackRegistered = true;

<       }

<

<       if (RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight)

<       {

<           AllocatedVirtualTexture = RuntimeVirtualTexture->GetAllocatedVirtualTexture();

<

<

<           if (AllocatedVirtualTexture != nullptr)

<           {

<               uint32 TileSize = FMath::FloorLog2(RuntimeVirtualTexture->GetTileSize());

<               // check(TileSize + ExtSubdivisionLevel >= NumQuadsPerTileOfTwo);

<               NumQuadsPerTileOfTwo = FMath::Min(NumQuadsPerTileOfTwo, TileSize + ExtSubdivisionLevel - 1);

<               RVTMinLevel = NumQuadsPerTileOfTwo;

<               MaxLevel = FMath::FloorLog2((uint32)RuntimeVirtualTexture->GetTileCount()) + NumQuadsPerTileOfTwo + ExtSubdivisionLevel;

<               NumInstanceVertexSide = 1 << (TileSize - NumQuadsPerTileOfTwo);

<               // Gather vertex factory uniform parameters.

<               FVirtualHeightfieldMeshVertexFactoryParameters UniformParams;

<               UniformParams.PageTableTexture = AllocatedVirtualTexture->GetPageTableTexture(0);

<               UniformParams.HeightTexture = AllocatedVirtualTexture->GetPhysicalTextureSRV(0, false);

<               UniformParams.HeightSampler = TStaticSamplerState<SF_Bilinear>::GetRHI();

<               UniformParams.LodBiasTexture = LodBiasTexture ? LodBiasTexture->GetResource()->TextureRHI : GBlackTexture->TextureRHI;

<               UniformParams.LodBiasSampler = TStaticSamplerState<SF_Point>::GetRHI();

<               UniformParams.NumInstanceVertexSide = NumInstanceVertexSide;

<               {

<                   UniformParams.MaxLod = MaxLevel;

<                   UniformParams.RVTMinLevel = RVTMinLevel;

<               }

<

<               FUintVector4 PackedUniform;

<               AllocatedVirtualTexture->GetPackedUniform(&PackedUniform, 0);

<               UniformParams.VTPackedUniform = PackedUniform;

<               FUintVector4 PackedPageTableUniform[2];

<               AllocatedVirtualTexture->GetPackedPageTableUniform(PackedPageTableUniform);

<               UniformParams.VTPackedPageTableUniform0 = PackedPageTableUniform[0];

<               UniformParams.VTPackedPageTableUniform1 = PackedPageTableUniform[1];

<

<               const float PageTableSizeX = AllocatedVirtualTexture->GetWidthInTiles();

<               const float PageTableSizeY = AllocatedVirtualTexture->GetHeightInTiles();

<               UniformParams.PageTableSize = FVector4f(PageTableSizeX, PageTableSizeY, 1.f / PageTableSizeX, 1.f / PageTableSizeY);

<

<               const float PhysicalTextureSize = AllocatedVirtualTexture->GetPhysicalTextureSize(0);

<               UniformParams.PhysicalTextureSize = FVector2f(PhysicalTextureSize, 1.f / PhysicalTextureSize);

<

<               UniformParams.VirtualHeightfieldToLocal = FMatrix44f(UVToLocal);

<               UniformParams.VirtualHeightfieldToWorld = FMatrix44f(UVToWorld);        // LWC_TODO: Precision loss

<

<               UniformParams.LodBiasScale = LodBiasScale;

<

<               const float PageSize = AllocatedVirtualTexture->GetVirtualTileSize();

<               const float PageBorderSize = AllocatedVirtualTexture->GetTileBorderSize();

<               const float PageAndBorderSize = PageSize + PageBorderSize * 2.f;

<               const float HalfTexelSize = 0.5f;

<               const FVector4 PhysicalPageTransform = FVector4(PageAndBorderSize, PageSize, PageBorderSize, HalfTexelSize) * (1.f / PhysicalTextureSize);

<               UniformParams.PhysicalPageTransform = (FVector4f)PhysicalPageTransform;

<               UniformParams.NumPhysicalAddressBits = AllocatedVirtualTexture->GetPageTableFormat() == EVTPageTableFormat::UInt16 ? 6 : 8; // See packing in PageTableUpdate.usf

<

<               UniformParams.CloseMorphVertexForDebug = CVarCloseMorphVertexForDebug->GetInt();

<

<               UniformParams.ExtSubdivisionLevel = ExtSubdivisionLevel;

<

<               // Create vertex factory.

<               VertexFactory = new FVirtualHeightfieldMeshVertexFactory(GetScene().GetFeatureLevel(), UniformParams);

<               VertexFactory->InitResource(FRHICommandListImmediate::Get());

<           }

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::DestroyRenderThreadResources()

< {

<   if (VertexFactory != nullptr)

<   {

<       VertexFactory->ReleaseResource();

<       delete VertexFactory;

<       VertexFactory = nullptr;

<   }

<

<   if (bCallbackRegistered)

<   {

<       GetRendererModule().RemoveAllVirtualTextureProducerDestroyedCallbacks(this);

<       bCallbackRegistered = false;

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::OnVirtualTextureDestroyedCB(const FVirtualTextureProducerHandle& InHandle, void* Baton)

< {

<   FVirtualHeightfieldMeshSceneProxy* SceneProxy = (FVirtualHeightfieldMeshSceneProxy*)Baton;

<   SceneProxy->DestroyRenderThreadResources();

<   SceneProxy->CreateRenderThreadResources();

< }

<

< FPrimitiveViewRelevance FVirtualHeightfieldMeshSceneProxy::GetViewRelevance(const FSceneView* View) const

< {

<   const bool bValid = AllocatedVirtualTexture != nullptr;

<   const bool bIsHiddenInEditor = bHiddenInEditor && View->Family->EngineShowFlags.Editor;

<

<   FPrimitiveViewRelevance Result;

<   Result.bDrawRelevance = bValid && IsShown(View) && !bIsHiddenInEditor;

<   Result.bShadowRelevance = bValid && IsShadowCast(View) && ShouldRenderInMainPass() &&!bIsHiddenInEditor;

<   Result.bDynamicRelevance = true;

<   Result.bStaticRelevance = false;

<   Result.bRenderInMainPass = ShouldRenderInMainPass();

<   Result.bUsesLightingChannels = GetLightingChannelMask() != GetDefaultLightingChannelMask();

<   Result.bRenderCustomDepth = ShouldRenderCustomDepth();

<   Result.bTranslucentSelfShadow = false;

<   MaterialRelevance.SetPrimitiveViewRelevance(Result);

<   Result.bVelocityRelevance = DrawsVelocity() && Result.bOpaque && Result.bRenderInMainPass;

<   return Result;

< }

<

< void FVirtualHeightfieldMeshSceneProxy::GetDynamicMeshElements(const TArray<const FSceneView*>& Views, const FSceneViewFamily& ViewFamily, uint32 VisibilityMap, FMeshElementCollector& Collector) const

< {

<   check(IsInRenderingThread());

<   check(AllocatedVirtualTexture != nullptr);

<

<   if (GVirtualHeightfieldMeshViewRendererExtension.IsInFrame())

<   {

<       // Can't add new work while bInFrame.

<       // In UE5 we need to AddWork()/SubmitWork() in two phases: InitViews() and InitViewsAfterPrepass()

<       // The main renderer hooks for that don't exist in UE5.0 and are only added in UE5.1

<       // That means that for UE5.0 we always hit this for shadow drawing and shadows will not be rendered.

<       // Not earlying out here can lead to crashes from buffers being released too soon.

<       return;

<   }

<

<   for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ViewIndex++)

<   {

<       if (VisibilityMap & (1 << ViewIndex))

<       {

<           if (!IsShadowCast(Views[ViewIndex]) && ViewFamily.Views[0] != Views[ViewIndex])

<           {

<               continue;

<           }

<           VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers = GVirtualHeightfieldMeshViewRendererExtension.AddWork(this, ViewFamily.Views[0], Views[ViewIndex]);

<

<           {

<               FMeshBatch& Mesh = Collector.AllocateMesh();

<               // Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bWireframe = ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bUseWireframeSelectionColoring = IsSelected();

<               Mesh.VertexFactory = VertexFactory;

<               Mesh.MaterialRenderProxy = Material;

<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

<               Mesh.Type = PT_TriangleList;

<               Mesh.DepthPriorityGroup = SDPG_World;

<               Mesh.bCanApplyViewModeOverrides = true;

<               Mesh.bUseForMaterial = true;

<               Mesh.CastShadow = true;

<               Mesh.bUseForDepthPass = true;

<

<               Mesh.Elements.SetNumZeroed(1);

<               {

<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

<

<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

<                   BatchElement.IndirectArgsOffset = 0;

<

<                   BatchElement.FirstIndex = 0;

<                   BatchElement.NumPrimitives = 0;

<                   BatchElement.MinVertexIndex = 0;

<                   BatchElement.MaxVertexIndex = 0;

<

<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

<

<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

<                   Parameters2.InstanceBuffer = Buffers.InstanceBufferSRV;

<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

<                   BatchElement.UserData = (void*)UserData;

<

<                   UserData->InstanceBufferSRV = Buffers.InstanceBufferSRV;

<

<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

<                   FSceneView const* MainView = ViewFamily.Views[0];

<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

<

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<                   // Support the freezerendering mode. Use any frozen view state for culling.

<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

<                   if (FrozenViewMatrices != nullptr)

<                   {

<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

<                   }

< #endif

<

<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

<               }

<

<               Collector.AddMesh(ViewIndex, Mesh);

<           }

< #pragma region S1_Engine_Shiyu

<           // for hole quad

<           {

<               FMeshBatch& Mesh = Collector.AllocateMesh();

<               Mesh.bWireframe = AllowDebugViewmodes() && ViewFamily.EngineShowFlags.Wireframe;

<               Mesh.bUseWireframeSelectionColoring = IsSelected();

<               Mesh.VertexFactory = VertexFactory;

<               Mesh.MaterialRenderProxy = HoleMaterial;

<               Mesh.ReverseCulling = IsLocalToWorldDeterminantNegative();

<               Mesh.Type = PT_TriangleList;

<               Mesh.DepthPriorityGroup = SDPG_World;

<               Mesh.bCanApplyViewModeOverrides = true;

<               Mesh.bUseForMaterial = true;

<               Mesh.CastShadow = true;

<               Mesh.bUseForDepthPass = true;

<

<               Mesh.Elements.SetNumZeroed(1);

<               {

<                   FMeshBatchElement& BatchElement = Mesh.Elements[0];

<

<                   BatchElement.IndexBuffer = VertexFactory->GetIndexBuffer();

<                   BatchElement.IndirectArgsBuffer = Buffers.IndirectArgsBuffer;

<                   BatchElement.IndirectArgsOffset = 5 * sizeof(uint32);

<

<                   BatchElement.FirstIndex = 0;

<                   BatchElement.NumPrimitives = 0;

<                   BatchElement.MinVertexIndex = 0;

<                   BatchElement.MaxVertexIndex = 0;

<

<                   BatchElement.PrimitiveIdMode = PrimID_ForceZero;

<                   BatchElement.PrimitiveUniformBuffer = GetUniformBuffer();

<

<                   FVirtualHeightfieldMeshUserData* UserData = &Collector.AllocateOneFrameResource<FVirtualHeightfieldMeshUserData>();

<

<                   FVirtualHeightfieldMeshVertexFactoryParameters2 Parameters2;

<                   Parameters2.InstanceBuffer = Buffers.HoleInstanceBufferSRV;

<                   UserData->InstantceBuf = FVirtualHeightfieldMeshVertexFactoryBuffer2Ref::CreateUniformBufferImmediate(Parameters2, UniformBuffer_SingleFrame);

<

<                   BatchElement.UserData = (void*)UserData;

<

<                   UserData->InstanceBufferSRV = Buffers.HoleInstanceBufferSRV;

<

<                   //todo[vhm]: Move all the view dependent lod logic into shader. Would help us to move to static mesh batches in the future.

<                   FSceneView const* MainView = ViewFamily.Views[0];

<                   UserData->LodViewOrigin = (FVector3f)MainView->ViewMatrices.GetViewOrigin();    // LWC_TODO: Precision Loss

<

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<                   // Support the freezerendering mode. Use any frozen view state for culling.

<                   const FViewMatrices* FrozenViewMatrices = MainView->State != nullptr ? MainView->State->GetFrozenViewMatrices() : nullptr;

<                   if (FrozenViewMatrices != nullptr)

<                   {

<                       UserData->LodViewOrigin = (FVector3f)FrozenViewMatrices->GetViewOrigin();

<                   }

< #endif

<

<                   UserData->LodDistances = VirtualHeightfieldMesh::CalculateLodRanges(MainView, this);

<               }

<

<               Collector.AddMesh(ViewIndex, Mesh);

<           }

<       }

<

< #pragma endregion

<   }

< }

<

< bool FVirtualHeightfieldMeshSceneProxy::HasSubprimitiveOcclusionQueries() const

< {

<   return CVarVHMOcclusion.GetValueOnAnyThread() != 0;

< }

<

< const TArray<FBoxSphereBounds>* FVirtualHeightfieldMeshSceneProxy::GetOcclusionQueries(const FSceneView* View) const

< {

<   return (CVarVHMOcclusion.GetValueOnAnyThread() == 0 || OcclusionVolumes.Num() == 0) ? &DefaultOcclusionVolumes : &OcclusionVolumes;

< }

<

< void FVirtualHeightfieldMeshSceneProxy::BuildOcclusionVolumes(TArrayView<FVector2D> const& InMinMaxData, FIntPoint const& InMinMaxSize, TArrayView<int32> const& InMinMaxMips, int32 InNumLods)

< {

<   NumOcclusionLods = 0;

<   OcclusionGridSize = FIntPoint::ZeroValue;

<   OcclusionVolumes.Reset();

<

<   if (InNumLods > 0 && InMinMaxMips.Num() > 0)

<   {

<       NumOcclusionLods = FMath::Min(InNumLods, InMinMaxMips.Num());

<

<       const int32 BaseLod = InMinMaxMips.Num() - NumOcclusionLods;

<       OcclusionGridSize.X = FMath::Max(InMinMaxSize.X >> BaseLod, 1);

<       OcclusionGridSize.Y = FMath::Max(InMinMaxSize.Y >> BaseLod, 1);

<

<       OcclusionVolumes.Reserve(InMinMaxData.Num() - InMinMaxMips[BaseLod]);

<

<       for (int32 LodIndex = BaseLod; LodIndex < InMinMaxMips.Num(); ++LodIndex)

<       {

<           int32 SizeX = FMath::Max(InMinMaxSize.X >> LodIndex, 1);

<           int32 SizeY = FMath::Max(InMinMaxSize.Y >> LodIndex, 1);

<           int32 MinMaxDataIndex = InMinMaxMips[LodIndex];

<

<           for (int Y = 0; Y < SizeY; ++Y)

<           {

<               for (int X = 0; X < SizeX; ++X)

<               {

<                   FVector2D MinMaxU = FVector2D((float)X / (float)SizeX, (float)(X + 1) / (float)SizeX);

<                   FVector2D MinMaxV = FVector2D((float)Y / (float)SizeY, (float)(Y + 1) / (float)SizeY);

<                   FVector2D MinMaxZ = InMinMaxData[MinMaxDataIndex++];

<

<                   FVector Pos[8];

<                   Pos[0] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.X));

<                   Pos[1] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.X));

<                   Pos[2] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.X));

<                   Pos[3] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.X));

<                   Pos[4] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.X, MinMaxZ.Y));

<                   Pos[5] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.X, MinMaxZ.Y));

<                   Pos[6] = UVToWorld.TransformPosition(FVector(MinMaxU.X, MinMaxV.Y, MinMaxZ.Y));

<                   Pos[7] = UVToWorld.TransformPosition(FVector(MinMaxU.Y, MinMaxV.Y, MinMaxZ.Y));

<

<                   const float ExpandOcclusion = 3.f;

<                   OcclusionVolumes.Add(FBoxSphereBounds(FBox(Pos, 8).ExpandBy(ExpandOcclusion)));

<               }

<           }

<       }

<   }

< }

<

< void FVirtualHeightfieldMeshSceneProxy::AcceptOcclusionResults(FSceneView const* View, TArray<bool>* Results, int32 ResultsStart, int32 NumResults)

< {

<   if (GOcclusionResetRequired)

<   {

<       GOcclusionResults.Reset();

<       GOcclusionResetRequired = false;

<   }

<

<   if (CVarVHMOcclusion.GetValueOnAnyThread() != 0 && Results != nullptr && NumResults > 1)

<   {

<       FOcclusionResults& OcclusionResults = GOcclusionResults.Emplace(FOcclusionResultsKey(this, View));

<       OcclusionResults.TextureSize = OcclusionGridSize;

<       OcclusionResults.NumTextureMips = NumOcclusionLods;

<       OcclusionResults.UploadData.Append(Results->GetData() + ResultsStart, NumResults);

<   }

< }

<

< namespace VirtualHeightfieldMesh

< {

<   /* Keep indirect args offsets in sync with VirtualHeightfieldMesh.usf. */

<   static const int32 IndirectArgsByteOffset_FinalCull = 0;

<

<

<   /** Shader structure used for tracking work queues in persistent wave style shaders. Keep in sync with VirtualHeightfieldMesh.ush. */

<   struct WorkerQueueInfo

<   {

<       uint32 Read;

<       uint32 Write;

<       int32 NumActive;

<   };

<

<   /** Final render instance description used by the DrawInstancedIndirect(). Keep in sync with VirtualHeightfieldMesh.ush. */

<   struct QuadRenderInstance

<   {

<       // float UVTransform[3];

<       uint32 AddressLevelPacked;

<       // float UVTransformPar[3];

<       // float Height;

<       // float UVTransformPar2[3];

<       // float Padding;

<       uint32 PhysicalAddress[3];

<       // uint32 Padding2;

<   };

<

<   /** Compute shader to initialize all buffers, including adding the lowest mip page(s) to the QuadBuffer. */

<   class FInitBuffersVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FInitBuffersVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FInitBuffersVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER(uint32, MaxLevel)

<           SHADER_PARAMETER(uint32, NumForceLoadLods)

<           SHADER_PARAMETER(uint32, PageTableFeedbackId)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<

<       END_SHADER_PARAMETER_STRUCT()

<

<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       // {

<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       // }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FInitBuffersVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitBuffersCS", SF_Compute);

<

<   /** Compute shader to traverse the virtual texture page table for a view and generate an array of quads to potentially render. */

<   class FCollectQuadsVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FCollectQuadsVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FCollectQuadsVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)

<           SHADER_PARAMETER(int32, MinMaxLevelOffset)

<           SHADER_PARAMETER_TEXTURE(Texture2D, LodBiasMinMaxTexture)

<           SHADER_PARAMETER_TEXTURE(Texture2D<float>, OcclusionTexture)

<           SHADER_PARAMETER(int32, OcclusionLevelOffset)

<           SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<           SHADER_PARAMETER(uint32, MaxLevel)

<           SHADER_PARAMETER(uint32, RVTMinLevel)

<           SHADER_PARAMETER(FVector4f, PageTableSize)

<           SHADER_PARAMETER(uint32, PageTableFeedbackId)

<           SHADER_PARAMETER(FVector4f, LodDistances)

<           SHADER_PARAMETER(float, LodBiasScale)

<           SHADER_PARAMETER(FVector3f, ViewOrigin)

<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])

<           SHADER_PARAMETER(FMatrix44f, UVToWorld)

<           SHADER_PARAMETER(FVector3f, UVToWorldScale)

<           SHADER_PARAMETER(uint32, QueueBufferSizeMask)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWQueueBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, RWQuadBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<       END_SHADER_PARAMETER_STRUCT()

<

<       // static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)

<       // {

<       //  return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);

<       // }

<

<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<       {

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<           Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

< #pragma endregion

<       }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FCollectQuadsVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CollectQuadsCS", SF_Compute);

<

<   /** InitInstanceBuffer compute shader. */

<   class FInitInstanceBufferVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FInitInstanceBufferVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FInitInstanceBufferVHM_CS, FGlobalShader);

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER(int32, NumIndices)

<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<       END_SHADER_PARAMETER_STRUCT()

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FInitInstanceBufferVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "InitInstanceBufferCS", SF_Compute);

<

<   /** CullInstances compute shader. */

<   class FCullInstancesVHM_CS : public FGlobalShader

<   {

<   public:

<       DECLARE_GLOBAL_SHADER(FCullInstancesVHM_CS);

<       SHADER_USE_PARAMETER_STRUCT(FCullInstancesVHM_CS, FGlobalShader);

<

<       class FReuseCullDim : SHADER_PERMUTATION_BOOL("REUSE_CULL");

<

<       using FPermutationDomain = TShaderPermutationDomain<FReuseCullDim>;

<

<       BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<           SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           SHADER_PARAMETER_SAMPLER(SamplerState, MinMaxTextureSampler)

<           SHADER_PARAMETER(int32, MinMaxLevelOffset)

<           SHADER_PARAMETER(uint32, RVTMinLevel)

<           SHADER_PARAMETER_TEXTURE(Texture2D, PageTableTexture)

<           SHADER_PARAMETER(FVector4f, PageTableSize)

<           SHADER_PARAMETER_ARRAY(FVector4f, FrustumPlanes, [5])

<           SHADER_PARAMETER(FVector4f, PhysicalPageTransform)

<           SHADER_PARAMETER(uint32, NumPhysicalAddressBits)

<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>, QuadBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>, IndirectArgsBufferSRV)

<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWInstanceBuffer)

<           SHADER_PARAMETER_UAV(RWBuffer<uint>, RWIndirectArgsBuffer)

<           RDG_BUFFER_ACCESS(IndirectArgsBuffer, ERHIAccess::IndirectArgs)

< #pragma region S1_Engine_Shiyu

<           SHADER_PARAMETER_TEXTURE(Texture2D, MaskTexture)

<           SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>, RWHoleInstanceBuffer)

<           SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

< #pragma endregion

<       END_SHADER_PARAMETER_STRUCT()

<

<

<       static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<       {

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<           Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

< #pragma endregion

<       }

<   };

<

<   IMPLEMENT_GLOBAL_SHADER(FCullInstancesVHM_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh.usf", "CullInstancesCS", SF_Compute);

<

<

<   namespace V2

<   {

< //        class FFirstInitBuffers_CS : public FGlobalShader

< //        {

< //        public:

< //            DECLARE_GLOBAL_SHADER(FFirstInitBuffers_CS);

< //            SHADER_USE_PARAMETER_STRUCT(FFirstInitBuffers_CS, FGlobalShader);

< //

< //            BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

< //                SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

< //                SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>, FinalQuadBuffer)

< //                SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

< //            END_SHADER_PARAMETER_STRUCT()

< //        };

< //        IMPLEMENT_GLOBAL_SHADER(FFirstInitBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "FirstInitBuffersCS", SF_Compute);

<

<       class FInitAllBuffers_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FInitAllBuffers_CS);

<           SHADER_USE_PARAMETER_STRUCT(FInitAllBuffers_CS, FGlobalShader);

<

<           class FClearVTCountDim : SHADER_PERMUTATION_BOOL("CLEAR_VT_COUNT");

<

<           using FPermutationDomain = TShaderPermutationDomain<FClearVTCountDim>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWFeedbackBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, FinalArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer1)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, DispatchArgsBuffer2)

<               SHADER_PARAMETER_UAV(RWBuffer<uint>, InstanceArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

<           END_SHADER_PARAMETER_STRUCT()

<

<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<           {

< #if VHM_ENABLE_STAT

<               Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

<           }

<       };

<       IMPLEMENT_GLOBAL_SHADER(FInitAllBuffers_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldInitBuffers.usf", "InitAllBuffersCS", SF_Compute);

<

<

<       class FFillLevel4Quad_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FFillLevel4Quad_CS)

<           SHADER_USE_PARAMETER_STRUCT(FFillLevel4Quad_CS, FGlobalShader)

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<           END_SHADER_PARAMETER_STRUCT()

<       };

<       IMPLEMENT_GLOBAL_SHADER(FFillLevel4Quad_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "FillLevel4QuadCS", SF_Compute);

<

<

<       // class FCollectQuadsFromPreFrame_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectQuadsFromPreFrame_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectQuadsFromPreFrame_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectQuadsFromPreFrameCS", SF_Compute);

<       //

<

<       class FCollectSubdivideQuads_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FCollectSubdivideQuads_CS);

<           SHADER_USE_PARAMETER_STRUCT(FCollectSubdivideQuads_CS, FGlobalShader);

<

<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

<           class FWithFeedback : SHADER_PERMUTATION_BOOL("VHM_WITH_FEEDBACK");

<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim, FWithFeedback>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               SHADER_PARAMETER(uint32, CurPassCalTime)

<               RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<           END_SHADER_PARAMETER_STRUCT()

<

<       };

<       IMPLEMENT_GLOBAL_SHADER(FCollectSubdivideQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectSubdivideQuadsCS", SF_Compute);

<

<       // class FCollectMergeQuads_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectMergeQuads_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectMergeQuads_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      SHADER_PARAMETER(uint32, CurPassCalTime)

<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutMergeQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectMergeQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectMergeQuadsCS", SF_Compute);

<

<       // class FCollectRemainQuads_CS : public FGlobalShader

<       // {

<       // public:

<       //  DECLARE_GLOBAL_SHADER(FCollectRemainQuads_CS);

<       //  SHADER_USE_PARAMETER_STRUCT(FCollectRemainQuads_CS, FGlobalShader);

<       //

<       //  BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )

<       //      SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<       //      RDG_BUFFER_ACCESS(InQuadArgsBuffer, ERHIAccess::IndirectArgs)

<       //      SHADER_PARAMETER(uint32, RemainCS_DispatchArgsOffset)

<       //      SHADER_PARAMETER(uint32, CurPassCalTime)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)

<       //      SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)

<       //      SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<       //      SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<       //      SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<       //  END_SHADER_PARAMETER_STRUCT()

<       // };

<       // IMPLEMENT_GLOBAL_SHADER(FCollectRemainQuads_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectRemainQuadsCS", SF_Compute);

<       //

<       class FCullQuadsAndGenerateInstances_CS : public FGlobalShader

<       {

<       public:

<           DECLARE_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS);

<           SHADER_USE_PARAMETER_STRUCT(FCullQuadsAndGenerateInstances_CS, FGlobalShader);

<

<           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");

<           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim>;

<

<           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)

<               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)

<               RDG_BUFFER_ACCESS(InDispatchArgsBufferAccess, ERHIAccess::IndirectArgs)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint>,       InDispatchArgsBuffer)

<               SHADER_PARAMETER_RDG_BUFFER_SRV(Buffer<uint2>,      InQuadBuffer)

<               SHADER_PARAMETER_UAV(RWBuffer<uint>,                InstanceArgsBuffer)

<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    QuadInstanceBuffer)

<               SHADER_PARAMETER_UAV(RWStructuredBuffer<QuadRenderInstance>,    HoleQuadInstanceBuffer)

<               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)

<               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)

<               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)

<               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)

<               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)

<               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>, RWStatBuffer)

<           END_SHADER_PARAMETER_STRUCT()

<

<           static void ModifyCompilationEnvironment(const FGlobalShaderPermutationParameters& Parameters, FShaderCompilerEnvironment& Environment)

<           {

< #if VHM_ENABLE_STAT

<               Environment.SetDefine(TEXT("VHM_STAT"), 1);

< #endif

<           }

<       };

<       IMPLEMENT_GLOBAL_SHADER(FCullQuadsAndGenerateInstances_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CullQuadsAndGenerateInstancesCS", SF_Compute);

<   };

<

<   /** Default Min/Max texture has the fixed maximum [0,1]. */

<   class FHeightMinMaxDefaultTexture : public FTexture

<   {

<   public:

<       virtual void InitRHI(FRHICommandListBase& RHICmdList) override

<       {

<           const FRHITextureCreateDesc Desc =

<               FRHITextureCreateDesc::Create2D(TEXT("VirtualHeightfieldMesh.MinMaxDefaultTexture"), 1, 1, PF_B8G8R8A8)

<               .SetFlags(ETextureCreateFlags::ShaderResource);

<

<           TextureRHI = RHICreateTexture(Desc);

<

<           // Write the contents of the texture.

<           uint32 DestStride;

<           FColor* DestBuffer = (FColor*)RHILockTexture2D(TextureRHI, 0, RLM_WriteOnly, DestStride, false);

<           *DestBuffer = FColor(0, 0, 255, 255);

<           RHIUnlockTexture2D(TextureRHI, 0, false);

<

<           // Create the sampler state RHI resource.

<           FSamplerStateInitializerRHI SamplerStateInitializer(SF_Point, AM_Clamp, AM_Clamp, AM_Clamp);

<           SamplerStateRHI = GetOrCreateSamplerState(SamplerStateInitializer);

<       }

<

<       virtual uint32 GetSizeX() const override { return 1; }

<       virtual uint32 GetSizeY() const override { return 1; }

<   };

<

<   /** Single global instance of default Min/Max texture. */

<   FTexture* GHeightMinMaxDefaultTexture = new TGlobalResource<FHeightMinMaxDefaultTexture>;

<

<   /** View matrices that can be frozen in freezerendering mode. */

<   struct FViewData

<   {

<       FVector ViewOrigin;

<       FMatrix ProjectionMatrix;

<       FConvexVolume ViewFrustum;

<       bool bViewFrozen;

<   };

<

<   /** Fill the FViewData from an FSceneView respecting the freezerendering mode. */

<   void GetViewData(FSceneView const* InSceneView, FViewData& OutViewData)

<   {

< #if !(UE_BUILD_SHIPPING || UE_BUILD_TEST)

<       const FViewMatrices* FrozenViewMatrices = InSceneView->State != nullptr ? InSceneView->State->GetFrozenViewMatrices() : nullptr;

<       if (FrozenViewMatrices != nullptr)

<       {

<           OutViewData.ViewOrigin = FrozenViewMatrices->GetViewOrigin();

<           OutViewData.ProjectionMatrix = FrozenViewMatrices->GetProjectionMatrix();

<           GetViewFrustumBounds(OutViewData.ViewFrustum, FrozenViewMatrices->GetViewProjectionMatrix(), true);

<           OutViewData.bViewFrozen = true;

<       }

<       else

< #endif

<       {

<           OutViewData.ViewOrigin = InSceneView->ViewMatrices.GetViewOrigin();

<           OutViewData.ProjectionMatrix = InSceneView->ViewMatrices.GetProjectionMatrix();

<           OutViewData.ViewFrustum = InSceneView->ViewFrustum;

<           OutViewData.bViewFrozen = false;

<       }

<   }

<

<   /** Convert FPlane to Xx+Yy+Zz+W=0 form for simpler use in shader. */

<   FVector4 ConvertPlane(FPlane const& Plane)

<   {

<       return FVector4(-Plane.X, -Plane.Y, -Plane.Z, Plane.W);

<   }

<

<   /** Translate a plane. This is a simpler case than the full TransformPlane(). */

<   FPlane TranslatePlane(FPlane const& Plane, FVector const& Translation)

<   {

<       FPlane OutPlane = Plane / Plane.Size();

<       OutPlane.W -= FVector::DotProduct(FVector(OutPlane),  Translation);

<       return OutPlane;

<   }

<

<   /** Transform a plane using a transform matrix. Precalculate and pass in transpose adjoint to avoid work when transforming multiple planes.  */

<   FPlane TransformPlane(FPlane const& Plane, FMatrix const& Matrix, FMatrix const& TransposeAdjoint)

<   {

<       FVector N(Plane.X, Plane.Y, Plane.Z);

<       N = TransposeAdjoint.TransformVector(N).GetUnsafeNormal3();

<

<       FVector P(Plane.X * Plane.W, Plane.Y * Plane.W, Plane.Z * Plane.W);

<       P = Matrix.TransformPosition(P);

<

<       return FPlane(N, FVector::DotProduct(N, P));

<   }

<

<   /** Structure describing GPU culling setup for a single Proxy. */

<   struct FProxyDesc

<   {

<       FRHITexture* PageTableTexture;

<       FRHITexture* HeightMinMaxTexture;

<       FRHITexture* LodBiasMinMaxTexture;

<       int32 MinMaxLevelOffset;

<

< #pragma region S1_Engine_Shiyu

<       FRHITexture* MaskTexture;

< #pragma endregion

<

<

<       uint32 MaxLevel;

<       uint32 RVTMinLevel;

<       uint32 NumForceLoadLods;

<       uint32 PageTableFeedbackId;

<       uint32 NumPhysicalAddressBits;

<       FVector4 PageTableSize;

<       FVector4 PhysicalPageTransform;

<       FMatrix UVToWorld;

<       FVector UVToWorldScale;

<       uint32 NumQuadsPerTileOfTwo;

<       uint32 NumInstanceVertexSide; // Instance is a Plane, size is NumInstanceVertexSide * NumInstanceVertexSide

<

<       int32 MaxPersistentQueueItems;

<       int32 MaxRenderItems;

<       int32 MaxFeedbackItems;

<       int32 NumCollectPassWavefronts;

<

<       uint32 NumIndices;

<   };

<

<   /** View description used for LOD calculation in the main view. */

<   struct FMainViewDesc

<   {

<       FSceneView const* ViewDebug;

<       FVector ViewOrigin;

<       FVector4 LodDistances;

<       float LodBiasScale;

<       FVector4 Planes[5];

<       FTextureRHIRef OcclusionTexture;

<       int32 OcclusionLevelOffset;

<   };

<

<   /** View description used for culling in the child view. */

<   struct FChildViewDesc

<   {

<       FSceneView const* ViewDebug;

<       bool bIsMainView;

<       FVector4 Planes[5];

<   };

<

<   /** Structure to carry RDG resources. */

<   struct FVolatileResources

<   {

<       FRDGBufferRef QueueInfo;

<       FRDGBufferUAVRef QueueInfoUAV;

<       FRDGBufferRef QueueBuffer;

<       FRDGBufferUAVRef QueueBufferUAV;

<

<       FRDGBufferRef QuadBuffer;

<       FRDGBufferUAVRef QuadBufferUAV;

<       FRDGBufferSRVRef QuadBufferSRV;

<

<       FRDGBufferRef FeedbackBuffer;

<       FRDGBufferUAVRef FeedbackBufferUAV;

<

<       FRDGBufferRef IndirectArgsBuffer;

<       FRDGBufferUAVRef IndirectArgsBufferUAV;

<       FRDGBufferSRVRef IndirectArgsBufferSRV;

<

< #pragma region S1_Engine_Shiyu

< //#if VHM_ENABLE_STAT

<       FRDGBufferRef StatBuffer;

<       FRDGBufferUAVRef StatBufferUAV;

< //#endif

< #pragma endregion

<   };

<

<   /** Initialize the FDrawInstanceBuffers objects. */

<   void InitializeInstanceBuffers(FRHICommandListImmediate& RHICmdList, FDrawInstanceBuffers& InBuffers)

<   {

<       {

<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.InstanceBuffer"));

<           const int32 InstanceSize = sizeof(VirtualHeightfieldMesh::QuadRenderInstance);

<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnRenderThread() * InstanceSize;

<           InBuffers.InstanceBuffer = RHICmdList.CreateStructuredBuffer(InstanceSize, InstanceBufferSize, BUF_UnorderedAccess|BUF_ShaderResource, ERHIAccess::SRVMask, CreateInfo);

<           InBuffers.InstanceBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.InstanceBuffer, false, false);

<           InBuffers.InstanceBufferSRV = RHICmdList.CreateShaderResourceView(InBuffers.InstanceBuffer);

<       }

<       {

<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.InstanceIndirectArgsBuffer"));

<           InBuffers.IndirectArgsBuffer = RHICmdList.CreateVertexBuffer(10 * sizeof(uint32), BUF_UnorderedAccess|BUF_DrawIndirect|BUF_SourceCopy, ERHIAccess::IndirectArgs|ERHIAccess::CopySrc, CreateInfo);

<           InBuffers.IndirectArgsBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.IndirectArgsBuffer, PF_R32_UINT);

<       }

< #pragma region S1_Engine_Shiyu

<       {

<           FRHIResourceCreateInfo CreateInfo(TEXT("VirtualHeightfieldMesh.HoleInstanceBuffer"));

<           const int32 InstanceSize = sizeof(VirtualHeightfieldMesh::QuadRenderInstance);

<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnRenderThread() * InstanceSize / 4; // hold instance just little

<           InBuffers.HoleInstanceBuffer = RHICmdList.CreateStructuredBuffer(InstanceSize, InstanceBufferSize, BUF_UnorderedAccess|BUF_ShaderResource, ERHIAccess::SRVMask, CreateInfo);

<           InBuffers.HoleInstanceBufferUAV = RHICmdList.CreateUnorderedAccessView(InBuffers.HoleInstanceBuffer, false, false);

<           InBuffers.HoleInstanceBufferSRV = RHICmdList.CreateShaderResourceView(InBuffers.HoleInstanceBuffer);

<       }

< #if VHM_ENABLE_STAT

<       InBuffers.StatBufferReadBacks.Reserve(FDrawInstanceBuffers::MaxReadBackSize);

<       for (int32 i = 0; i < FDrawInstanceBuffers::MaxReadBackSize; ++i)

<       {

<           InBuffers.StatBufferReadBacks.Emplace(MakeUnique<FRHIGPUBufferReadback>(TEXT("VHM.StatReadBacks")));

<       }

< #endif

< #pragma endregion

<   }

<

<   /** Initialize the volatile resources used in the render graph. */

<   void InitializeResources(FRDGBuilder& GraphBuilder, FProxyDesc const& InDesc, FMainViewDesc const& InMainViewDesc, FVolatileResources& OutResources)

<   {

<       OutResources.QueueInfo = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateStructuredDesc(sizeof(WorkerQueueInfo), 1), TEXT("VirtualHeightfieldMesh.QueueInfo"));

<       OutResources.QueueInfoUAV = GraphBuilder.CreateUAV(OutResources.QueueInfo);

<       OutResources.QueueBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), InDesc.MaxPersistentQueueItems), TEXT("VirtualHeightfieldMesh.QuadQueue"));

<       OutResources.QueueBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.QueueBuffer, PF_R32_UINT));

<

<       OutResources.QuadBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 2, InDesc.MaxRenderItems), TEXT("VirtualHeightfieldMesh.QuadBuffer"));

<       OutResources.QuadBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.QuadBuffer, PF_R32G32_UINT));

<       OutResources.QuadBufferSRV = GraphBuilder.CreateSRV(FRDGBufferSRVDesc(OutResources.QuadBuffer, PF_R32G32_UINT));

<

<       FRDGBufferDesc FeedbackBufferDesc = FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), InDesc.MaxFeedbackItems + 1);

<       FeedbackBufferDesc.Usage = EBufferUsageFlags(FeedbackBufferDesc.Usage | BUF_SourceCopy);

<       OutResources.FeedbackBuffer = GraphBuilder.CreateBuffer(FeedbackBufferDesc, TEXT("VirtualHeightfieldMesh.FeedbackBuffer"));

<       OutResources.FeedbackBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.FeedbackBuffer, PF_R32_UINT));

<

<       OutResources.IndirectArgsBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize), TEXT("VirtualHeightfieldMesh.IndirectArgsBuffer"));

<       OutResources.IndirectArgsBufferUAV = GraphBuilder.CreateUAV(OutResources.IndirectArgsBuffer);

<       OutResources.IndirectArgsBufferSRV = GraphBuilder.CreateSRV(OutResources.IndirectArgsBuffer);

<

< #pragma region S1_Engine_Shiyu

< //#if VHM_ENABLE_STAT

<       OutResources.StatBuffer = GraphBuilder.CreateBuffer(FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),

<                                                           TEXT("VirtualHeightfieldMesh.StatBuffer"));

<       OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));

< //#endif

< #pragma endregion

<   }

<

<   namespace V2

<   {

<

<

<       void InitializeVolatileBuffers(FRDGBuilder& GraphBuilder, FVolatileBuffers& OutResources)

<       {

<           const int32 MaxRenderItems = CVarVHMMaxRenderItems.GetValueOnRenderThread();

<           const int32 MaxFeedbackItems = CVarVHMMaxFeedbackItems.GetValueOnRenderThread();

<           const TCHAR* MergeNames[2] = {

<               TEXT("VHM.MergeBuffer_0"),

<               TEXT("VHM.MergeBuffer_1")};

<           const TCHAR* MergeArgsNames[2] = {

<               TEXT("VHM.MergeArgsBuffer_0"),

<               TEXT("VHM.MergeArgsBuffer_1")};

<           const TCHAR* SubdivideNames[2] = {

<               TEXT("VHM.SubdivideBuffer_0"),

<               TEXT("VHM.SubdivideBuffer_1")};

<           const TCHAR* SubdivideArgsNames[2] = {

<               TEXT("VHM.SubdivideArgsBuffer_0"),

<               TEXT("VHM.SubdivideArgsBuffer_1")};

<           for(int i = 0; i < 2; ++i)

<           {

<               OutResources.MergeQuadBuffer[i] = GraphBuilder.CreateBuffer(

<                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),

<                   MergeNames[i]);

<               OutResources.MergeViews[i].Create(GraphBuilder, OutResources.MergeQuadBuffer[i]);

<

<               OutResources.SubdivideQuadBuffer[i] = GraphBuilder.CreateBuffer(

<                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),

<                   SubdivideNames[i]);

<               OutResources.SubdivideViews[i].Create(GraphBuilder, OutResources.SubdivideQuadBuffer[i]);

<

<               OutResources.ArgsBuffer[i] = GraphBuilder.CreateBuffer(

<                   FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize),

<                   SubdivideArgsNames[i]);

<               OutResources.ArgsViews[i].Create(GraphBuilder, OutResources.ArgsBuffer[i]);

<

<           }

<

<

<           // uniform

<           OutResources.VHMParameterUBuffer = GraphBuilder.CreateUniformBuffer<FVHMCSSharedParameters>(OutResources.VHMParameter);

<

< //#if VHM_ENABLE_STAT

<           OutResources.StatBuffer = GraphBuilder.CreateBuffer(

<               FRDGBufferDesc::CreateBufferDesc(sizeof(uint32), MaxStatCount),

<               TEXT("VirtualHeightfieldMesh.StatBuffer"));

<           OutResources.StatBufferUAV = GraphBuilder.CreateUAV(FRDGBufferUAVDesc(OutResources.StatBuffer, PF_R32_UINT));

< //#endif

<       }

<   }

<

< #pragma region S1_Engine_Shiyu

< #if VHM_ENABLE_STAT

<   void AddPass_GatherAllStats(FRDGBuilder& GraphBuilder,

<                               VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers, const uint32 BufferDiscardId,

<                               FRDGBufferRef StatBuffer)

<   {

<       const uint32 Offset = BufferDiscardId % FDrawInstanceBuffers::MaxReadBackSize;

<       FRHIGPUBufferReadback* GPUBufferReadBack = Buffers.StatBufferReadBacks[Offset].Get();

<       check(GPUBufferReadBack);

<       AddEnqueueCopyPass(GraphBuilder, GPUBufferReadBack, StatBuffer, sizeof(int32) * MaxStatCount);

<   }

< #endif

< #pragma endregion

<

<   /** Transition our output draw buffers for use. Read or write access is set according to the bToWrite parameter. */

<   void AddPass_TransitionAllDrawBuffers(FRDGBuilder& GraphBuilder, TArray<VirtualHeightfieldMesh::FDrawInstanceBuffers> const& Buffers, TArrayView<int32> const& BufferIndices, bool bToWrite)

<   {

<       TArray<FRHIUnorderedAccessView*> OverlapUAVs;

<       OverlapUAVs.Reserve(BufferIndices.Num());

<

<       TArray<FRHITransitionInfo> TransitionInfos;

<       TransitionInfos.Reserve(BufferIndices.Num() * 2);

<

<       for (int32 BufferIndex : BufferIndices)

<       {

<           FRHIUnorderedAccessView* IndirectArgsBufferUAV = Buffers[BufferIndex].IndirectArgsBufferUAV;

<           FRHIUnorderedAccessView* InstanceBufferUAV = Buffers[BufferIndex].InstanceBufferUAV;

<

<           OverlapUAVs.Add(IndirectArgsBufferUAV);

<

<           TransitionInfos.Add(FRHITransitionInfo(IndirectArgsBufferUAV, bToWrite ? ERHIAccess::IndirectArgs|ERHIAccess::CopySrc : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::IndirectArgs|ERHIAccess::CopySrc));

<           TransitionInfos.Add(FRHITransitionInfo(InstanceBufferUAV, bToWrite ? ERHIAccess::SRVMask : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::SRVMask));

<

< #pragma region S1_Engine_Shiyu

<           FRHIUnorderedAccessView* HoleInstanceBufferUAV = Buffers[BufferIndex].HoleInstanceBufferUAV;

<

<           TransitionInfos.Add(FRHITransitionInfo(HoleInstanceBufferUAV, bToWrite ? ERHIAccess::SRVMask : ERHIAccess::UAVMask, bToWrite ? ERHIAccess::UAVMask : ERHIAccess::SRVMask));

< #pragma endregion

<       }

<

<       AddPass(GraphBuilder, RDG_EVENT_NAME("TransitionAllDrawBuffers"), [bToWrite, OverlapUAVs, TransitionInfos](FRHICommandList& InRHICmdList)

<       {

<           if (!bToWrite)

<           {

<               InRHICmdList.EndUAVOverlap(OverlapUAVs);

<           }

<

<           InRHICmdList.Transition(TransitionInfos);

<

<           if (bToWrite)

<           {

<               InRHICmdList.BeginUAVOverlap(OverlapUAVs);

<           }

<       });

<   }

<

<   /** Initialize the buffers before collecting visible quads. */

<   void AddPass_InitBuffers(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, FProxyDesc const& InDesc, FVolatileResources& InVolatileResources)

<   {

<       TShaderMapRef<FInitBuffersVHM_CS> ComputeShader(InGlobalShaderMap);

<

<       FInitBuffersVHM_CS::FParameters* PassParameters = GraphBuilder.AllocParameters<FInitBuffersVHM_CS::FParameters>();

<       PassParameters->MaxLevel = InDesc.MaxLevel;

<       PassParameters->NumForceLoadLods = InDesc.NumForceLoadLods;

<       PassParameters->PageTableFeedbackId = InDesc.PageTableFeedbackId;

<       PassParameters->RWQueueInfo = InVolatileResources.QueueInfoUAV;

<       PassParameters->RWQueueBuffer = InVolatileResources.QueueBufferUAV;

<       PassParameters->RWIndirectArgsBuffer = InVolatileResources.IndirectArgsBufferUAV;

<       PassParameters->RWFeedbackBuffer = InVolatileResources.FeedbackBufferUAV;

< #pragma region S1_Engine_Shiyu

< //#if VHM_ENABLE_STAT

<       PassParameters->RWStatBuffer = InVolatileResources.StatBufferUAV;

< //#endif

< #pragma endregion

<

<       GraphBuilder.AddPass(

<           RDG_EVENT_NAME("InitBuffers"),

<           PassParameters,

<           ERDGPassFlags::Compute,

<           [PassParameters, ComputeShader](FRHICommandList& RHICmdList)

<       {

<           //todo: If feedback parsing understands append counter we don't need to fully clear

<           RHICmdList.ClearUAVUint(PassParameters->RWFeedbackBuffer->GetRHI(), FUintVector4(0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff));

<

<           FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *PassParameters, FIntVector(1, 1, 1));

<       });

<   }

<

<   /** Collect potentially visible quads and determine their Lods. */

<   void AddPass_CollectQuads(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap, FProxyDesc const& InDesc, FVolatileResources& InVolatileResources, FMainViewDesc const& InViewDesc)

<   {

<       TShaderMapRef<FCollectQuadsVHM_CS> ComputeShader(InGlobalShaderMap);

<

<       FCollectQuadsVHM_CS::FParameters* PassParameters = GraphBuilder.AllocParameters<FCollectQuadsVHM_CS::FParameters>();

<       PassParameters->HeightMinMaxTexture = InDesc.HeightMinMaxTexture;

<       PassParameters->LodBiasMinMaxTexture = InDesc.LodBiasMinMaxTexture;

<       PassParameters->MinMaxTextureSampler = TStaticSamplerState<SF_Point>::GetRHI();

<       PassParameters->MinMaxLevelOffset = InDesc.MinMaxLevelOffset;

<       PassParameters->OcclusionTexture = InViewDesc.OcclusionTexture;

<       PassParameters->OcclusionLevelOffset = InViewDesc.OcclusionLevelOffset;

<       PassParameters->PageTableTexture = InDesc.PageTableTexture;

<       PassParameters->MaxLevel = InDesc.MaxLevel;

<       PassParameters->RVTMinLevel = InDesc.RVTMinLevel;

<       PassParameters->PageTableSize = FVector4f(InDesc.PageTableSize); // LWC_TODO: precision loss

<       PassParameters-

... [diff truncated to 80KB; full diff in vhm_diffs/120024.diff] ...
```

#### CL 155993 — 2024/10/10 — 郭智均

- **提交说明**：--story=1022943 --user=郭智均 Merge from Release https://www.tapd.cn/68880148/s/1771215
- **TAPD**：story=1022943
- **涉及 VHM 文件**：7 个

**做了什么**：

提交目的：Merge from Release https://www.tapd.cn/68880148/s/1771215

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (integrate)
- **Runtime C++**：6 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTexture.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (integrate)

📄 查看 VHM 相关 diff（CL 155993）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#8 (text) ====

4,5d3
<

< #define VT_DISABLE_VIEW_UNIFORM_BUFFER 1

7d4
<

211c208
<   VTPageTableResult VTResult0 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, floor(SampleLevel));

---
>   VTPageTableResult VTResult0 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, floor(SampleLevel) - GetGlobalVirtualTextureMipBias());

214c211
<   VTPageTableResult VTResult1 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, ceil(SampleLevel));

---
>   VTPageTableResult VTResult1 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, ceil(SampleLevel) - GetGlobalVirtualTextureMipBias());


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp#2 (unicode) ====

30a31
>       Texture->LODGroup = TEXTUREGROUP_Project02; // Calculate data


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTexture.cpp#3 (unicode) ====

52a53
>   Texture->LODGroup = TEXTUREGROUP_Project02; // Calculate data

154a156
>       LodBiasTexture->LODGroup = TEXTUREGROUP_Project02; // Calculate data

241a244
>       LodBiasMinMaxTexture->LODGroup = TEXTUREGROUP_Project02; // Calculate data


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp#5 (unicode) ====

1,228c1,229
< // Copyright Epic Games, Inc. All Rights Reserved.
<
< #include "VirtualHeightfieldMeshComponent.h"
<
< #include "Components/RuntimeVirtualTextureComponent.h"
< #include "HeightfieldMinMaxTexture.h"
< #include "SceneInterface.h"
< #include "VirtualHeightfieldMeshEnable.h"
< #include "VirtualHeightfieldMeshSceneProxy.h"
< #include "VT/RuntimeVirtualTexture.h"
< #include "VT/RuntimeVirtualTextureVolume.h"
<
< #pragma region S1_Engine_Shiyu
< #include "HeightfieldMaskTexture.h"
< #pragma endregion
<
< #include UE_INLINE_GENERATED_CPP_BY_NAME(VirtualHeightfieldMeshComponent)
<
< UVirtualHeightfieldMeshComponent::UVirtualHeightfieldMeshComponent(const FObjectInitializer& ObjectInitializer)
<   : Super(ObjectInitializer)
< {
<   CastShadow = true;
<   bCastContactShadow = false;
<   bUseAsOccluder = true;
<   bAffectDynamicIndirectLighting = false;
<   bAffectDistanceFieldLighting = false;
<   bNeverDistanceCull = true;
<   bEnableAutoLODGeneration = false;
<   Mobility = EComponentMobility::Static;
<
<   ENQUEUE_RENDER_COMMAND(RegisterVHMExternal)([](FRHICommandListImmediate& RHICmdList)
<   {
<       FVirtualHeightfieldMeshSceneProxy::RegisterExternal();
<   });
< }
<
< void UVirtualHeightfieldMeshComponent::OnRegister()
< {
<   VirtualTextureRef = VirtualTexture.Get();
<
<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
<   if (RuntimeVirtualTextureComponent)
<   {
<       // Bind to delegate so that we dirty render state whenever RuntimeVirtualTextureComponent is moved.
<       RuntimeVirtualTextureComponent->TransformUpdated.AddUObject(this, &UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate);
<       // Bind to delegate so that RuntimeVirtualTextureComponent will pull hide flags from this object.
<       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().AddUObject(this, &UVirtualHeightfieldMeshComponent::GatherHideFlags);
<       RuntimeVirtualTextureComponent->MarkRenderStateDirty();
<   }
<
<   Super::OnRegister();
< }
<
< void UVirtualHeightfieldMeshComponent::OnUnregister()
< {
<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
<   if (RuntimeVirtualTextureComponent)
<   {
<       RuntimeVirtualTextureComponent->TransformUpdated.RemoveAll(this);
<       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().RemoveAll(this);
<       RuntimeVirtualTextureComponent->MarkRenderStateDirty();
<   }
<
<   VirtualTextureRef = nullptr;
<
<   Super::OnUnregister();
< }
<
< void UVirtualHeightfieldMeshComponent::ApplyWorldOffset(const FVector& InOffset, bool bWorldShift)
< {
<   Super::ApplyWorldOffset(InOffset, bWorldShift);
<   MarkRenderStateDirty();
< }
<
< ARuntimeVirtualTextureVolume* UVirtualHeightfieldMeshComponent::GetVirtualTextureVolume() const
< {
<   return VirtualTextureRef;
< }
<
< URuntimeVirtualTexture* UVirtualHeightfieldMeshComponent::GetVirtualTexture() const
< {
<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
<   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetVirtualTexture() : nullptr;
< }
<
< FTransform UVirtualHeightfieldMeshComponent::GetVirtualTextureTransform() const
< {
<   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
<   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetComponentTransform() * RuntimeVirtualTextureComponent->GetTexelSnapTransform() : FTransform::Identity;
< }
<
< bool UVirtualHeightfieldMeshComponent::IsVisible() const
< {
<   return
<       Super::IsVisible() &&
<       GetVirtualTexture() != nullptr &&
<       GetVirtualTexture()->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight &&
<       VirtualHeightfieldMesh::IsEnabled(GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5);
< }
<
< FBoxSphereBounds UVirtualHeightfieldMeshComponent::CalcBounds(const FTransform& LocalToWorld) const
< {
<   return FBoxSphereBounds(FBox(FVector(0.f, 0.f, 0.f), FVector(1.f, 1.f, 1.f))).TransformBy(LocalToWorld);
< }
<
< FPrimitiveSceneProxy* UVirtualHeightfieldMeshComponent::CreateSceneProxy()
< {
<   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;
<   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);
<   return bIsEnabled ? new FVirtualHeightfieldMeshSceneProxy(this) : nullptr;
< }
<
< void UVirtualHeightfieldMeshComponent::SetMaterial(int32 InElementIndex, UMaterialInterface* InMaterial)
< {
<   if (InElementIndex == 0 && Material != InMaterial)
<   {
<       Material = InMaterial;
<       MarkRenderStateDirty();
<   }
< }
<
< void UVirtualHeightfieldMeshComponent::GetUsedMaterials(TArray<UMaterialInterface*>& OutMaterials, bool bGetDebugMaterials) const
< {
<   if (Material != nullptr)
<   {
<       OutMaterials.Add(Material);
<   }
< #pragma region S1_Engine_Shiyu
<   if (HoleMaterial != nullptr)
<   {
<       OutMaterials.Add(HoleMaterial);
<   }
< #pragma endregion
< }
<
< void UVirtualHeightfieldMeshComponent::GatherHideFlags(bool& InOutHidePrimitivesInEditor, bool& InOutHidePrimitivesInGame) const
< {
<   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;
<   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);
<   InOutHidePrimitivesInEditor |= (bIsEnabled && !bHiddenInEditor);
<   InOutHidePrimitivesInGame |= bIsEnabled;
< }
<
< void UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate(USceneComponent* InRootComponent, EUpdateTransformFlags UpdateTransformFlags, ETeleportType Teleport)
< {
<   MarkRenderStateDirty();
< }
<
< #if WITH_EDITOR
<
< void UVirtualHeightfieldMeshComponent::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)
< {
<   static const FName HideInEditorName = GET_MEMBER_NAME_CHECKED(UVirtualHeightfieldMeshComponent, bHiddenInEditor);
<
<   const FName PropertyName = PropertyChangedEvent.Property->GetFName();
<   if (PropertyName == HideInEditorName)
<   {
<       // Force RuntimeVirtualTextureComponent to poll the HidePrimitives settings.
<       URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;
<       if (RuntimeVirtualTextureComponent != nullptr)
<       {
<           RuntimeVirtualTextureComponent->MarkRenderStateDirty();
<       }
<   }
<
<   Super::PostEditChangeProperty(PropertyChangedEvent);
< }
<
< #endif
<
< bool UVirtualHeightfieldMeshComponent::IsMinMaxTextureEnabled() const
< {
<   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();
<   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;
< }
<
< #if WITH_EDITOR
<
< bool UVirtualHeightfieldMeshComponent::IsMaskTextureEnabled() const
< {
<   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();
<   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;
< }
<
< void UVirtualHeightfieldMeshComponent::InitializeMinMaxTexture(uint32 InSizeX, uint32 InSizeY, uint32 InNumMips, uint8* InData)
< {
<   // We need an existing StreamingTexture object to update.
<   if (MinMaxTexture != nullptr)
<   {
<       FHeightfieldMinMaxTextureBuildDesc BuildDesc;
<       BuildDesc.SizeX = InSizeX;
<       BuildDesc.SizeY = InSizeY;
<       BuildDesc.NumMips = InNumMips;
<       BuildDesc.Data = InData;
<
<       MinMaxTexture->Modify();
<       MinMaxTexture->BuildTexture(BuildDesc);
<
<       MarkRenderStateDirty();
<   }
< }
<
< #pragma region S1_Engine_Shiyu
< void UVirtualHeightfieldMeshComponent::InitializeMaskTexture(uint32 InSizeX, uint32 InSizeY,
<   uint32 InNumMips, uint8* InData)
< {
<   // We need an existing StreamingTexture object to update.
<   if (MaskTexture != nullptr)
<   {
<       FHeightfieldMaskTextureBuildDesc BuildDesc;
<       BuildDesc.SizeX = InSizeX;
<       BuildDesc.SizeY = InSizeY;
<       BuildDesc.NumMips = InNumMips;
<       BuildDesc.Data = InData;
<
<       MaskTexture->Modify();
<       MaskTexture->BuildTexture(BuildDesc);
<
<       MarkRenderStateDirty();
<   }
< }
<
<
< #pragma endregion
<
<
< #endif
<
---
> // Copyright Epic Games, Inc. All Rights Reserved.

>

> #include "VirtualHeightfieldMeshComponent.h"

>

> #include "Components/RuntimeVirtualTextureComponent.h"

> #include "HeightfieldMinMaxTexture.h"

> #include "SceneInterface.h"

> #include "VirtualHeightfieldMeshEnable.h"

> #include "VirtualHeightfieldMeshSceneProxy.h"

> #include "VT/RuntimeVirtualTexture.h"

> #include "VT/RuntimeVirtualTextureVolume.h"

>

> #pragma region S1_Engine_Shiyu

> #include "HeightfieldMaskTexture.h"

> #pragma endregion

>

> #include UE_INLINE_GENERATED_CPP_BY_NAME(VirtualHeightfieldMeshComponent)

>

> UVirtualHeightfieldMeshComponent::UVirtualHeightfieldMeshComponent(const FObjectInitializer& ObjectInitializer)

>   : Super(ObjectInitializer)

> {

>   CastShadow = true;

>   bCastContactShadow = false;

>   bUseAsOccluder = true;

>   bAffectDynamicIndirectLighting = false;

>   bAffectDistanceFieldLighting = false;

>   bNeverDistanceCull = true;

>   bEnableAutoLODGeneration = false;

>   Mobility = EComponentMobility::Static;

>   bEnableExtSubdivisionLevel = IConsoleManager::Get().FindConsoleVariable(TEXT("r.VHM.EnableExtSubdivisionLevel"))->GetInt() ? true : false;

>

>   ENQUEUE_RENDER_COMMAND(RegisterVHMExternal)([](FRHICommandListImmediate& RHICmdList)

>   {

>       FVirtualHeightfieldMeshSceneProxy::RegisterExternal();

>   });

> }

>

> void UVirtualHeightfieldMeshComponent::OnRegister()

> {

>   VirtualTextureRef = VirtualTexture.Get();

>

>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

>   if (RuntimeVirtualTextureComponent)

>   {

>       // Bind to delegate so that we dirty render state whenever RuntimeVirtualTextureComponent is moved.

>       RuntimeVirtualTextureComponent->TransformUpdated.AddUObject(this, &UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate);

>       // Bind to delegate so that RuntimeVirtualTextureComponent will pull hide flags from this object.

>       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().AddUObject(this, &UVirtualHeightfieldMeshComponent::GatherHideFlags);

>       RuntimeVirtualTextureComponent->MarkRenderStateDirty();

>   }

>

>   Super::OnRegister();

> }

>

> void UVirtualHeightfieldMeshComponent::OnUnregister()

> {

>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

>   if (RuntimeVirtualTextureComponent)

>   {

>       RuntimeVirtualTextureComponent->TransformUpdated.RemoveAll(this);

>       RuntimeVirtualTextureComponent->GetHidePrimitivesDelegate().RemoveAll(this);

>       RuntimeVirtualTextureComponent->MarkRenderStateDirty();

>   }

>

>   VirtualTextureRef = nullptr;

>

>   Super::OnUnregister();

> }

>

> void UVirtualHeightfieldMeshComponent::ApplyWorldOffset(const FVector& InOffset, bool bWorldShift)

> {

>   Super::ApplyWorldOffset(InOffset, bWorldShift);

>   MarkRenderStateDirty();

> }

>

> ARuntimeVirtualTextureVolume* UVirtualHeightfieldMeshComponent::GetVirtualTextureVolume() const

> {

>   return VirtualTextureRef;

> }

>

> URuntimeVirtualTexture* UVirtualHeightfieldMeshComponent::GetVirtualTexture() const

> {

>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

>   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetVirtualTexture() : nullptr;

> }

>

> FTransform UVirtualHeightfieldMeshComponent::GetVirtualTextureTransform() const

> {

>   URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

>   return RuntimeVirtualTextureComponent ? RuntimeVirtualTextureComponent->GetComponentTransform() * RuntimeVirtualTextureComponent->GetTexelSnapTransform() : FTransform::Identity;

> }

>

> bool UVirtualHeightfieldMeshComponent::IsVisible() const

> {

>   return

>       Super::IsVisible() &&

>       GetVirtualTexture() != nullptr &&

>       GetVirtualTexture()->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight &&

>       VirtualHeightfieldMesh::IsEnabled(GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5);

> }

>

> FBoxSphereBounds UVirtualHeightfieldMeshComponent::CalcBounds(const FTransform& LocalToWorld) const

> {

>   return FBoxSphereBounds(FBox(FVector(0.f, 0.f, 0.f), FVector(1.f, 1.f, 1.f))).TransformBy(LocalToWorld);

> }

>

> FPrimitiveSceneProxy* UVirtualHeightfieldMeshComponent::CreateSceneProxy()

> {

>   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;

>   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);

>   return bIsEnabled ? new FVirtualHeightfieldMeshSceneProxy(this) : nullptr;

> }

>

> void UVirtualHeightfieldMeshComponent::SetMaterial(int32 InElementIndex, UMaterialInterface* InMaterial)

> {

>   if (InElementIndex == 0 && Material != InMaterial)

>   {

>       Material = InMaterial;

>       MarkRenderStateDirty();

>   }

> }

>

> void UVirtualHeightfieldMeshComponent::GetUsedMaterials(TArray<UMaterialInterface*>& OutMaterials, bool bGetDebugMaterials) const

> {

>   if (Material != nullptr)

>   {

>       OutMaterials.Add(Material);

>   }

> #pragma region S1_Engine_Shiyu

>   if (HoleMaterial != nullptr)

>   {

>       OutMaterials.Add(HoleMaterial);

>   }

> #pragma endregion

> }

>

> void UVirtualHeightfieldMeshComponent::GatherHideFlags(bool& InOutHidePrimitivesInEditor, bool& InOutHidePrimitivesInGame) const

> {

>   const FStaticFeatureLevel FeatureLevel = GetScene() ? GetScene()->GetFeatureLevel() : ERHIFeatureLevel::SM5;

>   const bool bIsEnabled = VirtualHeightfieldMesh::IsEnabled(FeatureLevel);

>   InOutHidePrimitivesInEditor |= (bIsEnabled && !bHiddenInEditor);

>   InOutHidePrimitivesInGame |= bIsEnabled;

> }

>

> void UVirtualHeightfieldMeshComponent::OnVirtualTextureTransformUpdate(USceneComponent* InRootComponent, EUpdateTransformFlags UpdateTransformFlags, ETeleportType Teleport)

> {

>   MarkRenderStateDirty();

> }

>

> #if WITH_EDITOR

>

> void UVirtualHeightfieldMeshComponent::PostEditChangeProperty(FPropertyChangedEvent& PropertyChangedEvent)

> {

>   static const FName HideInEditorName = GET_MEMBER_NAME_CHECKED(UVirtualHeightfieldMeshComponent, bHiddenInEditor);

>

>   const FName PropertyName = PropertyChangedEvent.Property->GetFName();

>   if (PropertyName == HideInEditorName)

>   {

>       // Force RuntimeVirtualTextureComponent to poll the HidePrimitives settings.

>       URuntimeVirtualTextureComponent* RuntimeVirtualTextureComponent = VirtualTextureRef != nullptr ? ToRawPtr(VirtualTextureRef->VirtualTextureComponent) : nullptr;

>       if (RuntimeVirtualTextureComponent != nullptr)

>       {

>           RuntimeVirtualTextureComponent->MarkRenderStateDirty();

>       }

>   }

>

>   Super::PostEditChangeProperty(PropertyChangedEvent);

> }

>

> #endif

>

> bool UVirtualHeightfieldMeshComponent::IsMinMaxTextureEnabled() const

> {

>   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();

>   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;

> }

>

> #if WITH_EDITOR

>

> bool UVirtualHeightfieldMeshComponent::IsMaskTextureEnabled() const

> {

>   URuntimeVirtualTexture* RuntimeVirtualTexture = GetVirtualTexture();

>   return RuntimeVirtualTexture != nullptr && RuntimeVirtualTexture->GetMaterialType() == ERuntimeVirtualTextureMaterialType::WorldHeight;

> }

>

> void UVirtualHeightfieldMeshComponent::InitializeMinMaxTexture(uint32 InSizeX, uint32 InSizeY, uint32 InNumMips, uint8* InData)

> {

>   // We need an existing StreamingTexture object to update.

>   if (MinMaxTexture != nullptr)

>   {

>       FHeightfieldMinMaxTextureBuildDesc BuildDesc;

>       BuildDesc.SizeX = InSizeX;

>       BuildDesc.SizeY = InSizeY;

>       BuildDesc.NumMips = InNumMips;

>       BuildDesc.Data = InData;

>

>       MinMaxTexture->Modify();

>       MinMaxTexture->BuildTexture(BuildDesc);

>

>       MarkRenderStateDirty();

>   }

> }

>

> #pragma region S1_Engine_Shiyu

> void UVirtualHeightfieldMeshComponent::InitializeMaskTexture(uint32 InSizeX, uint32 InSizeY,

>   uint32 InNumMips, uint8* InData)

> {

>   // We need an existing StreamingTexture object to update.

>   if (MaskTexture != nullptr)

>   {

>       FHeightfieldMaskTextureBuildDesc BuildDesc;

>       BuildDesc.SizeX = InSizeX;

>       BuildDesc.SizeY = InSizeY;

>       BuildDesc.NumMips = InNumMips;

>       BuildDesc.Data = InData;

>

>       MaskTexture->Modify();

>       MaskTexture->BuildTexture(BuildDesc);

>

>       MarkRenderStateDirty();

>   }

> }

>

>

> #pragma endregion

>

>

> #endif

>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#13 (unicode) ====

33c33
<       1,
---
>       0, // white e effect is not ok, now close external subdivision of vhm


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#18 (unicode) ====

105a106,112
> static TAutoConsoleVariable<int32> CVarVHMDisableCull(
>   TEXT("r.VHM.DisableCull"),
>   0,
>   TEXT(""),
>   ECVF_RenderThreadSafe
> );
>
2688a2696
>       bool EnableCull = !CVarVHMDisableCull->GetInt() && true /*defautl need cull*/;
2694c2702
<               VolatileBuffers, VTFeedbackBufUAV, CalTime, true, WithFeedback);
---
>               VolatileBuffers, VTFeedbackBufUAV, CalTime, EnableCull, WithFeedback);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h#8 (unicode) ====
```

#### CL 203456 — 2024/12/22 — tools

- **提交说明**：[BranchCopy] Merge from EngineUpgrade-202982 to trunk --Trigger=feiyulliu-Win\_Server\_00-https://devops.woa.com/console/pipeline/grgame/p-b089ea24e1524b8eb47529e9bd005864/detail/b-bd7c4c96b66741209ef7542d3a0fe773
- **涉及 VHM 文件**：13 个

**做了什么**：

- **Shader**：5 个文件
- `Shaders/Private/HeightfieldMaskRender.usf` (integrate)
- `Shaders/Private/VHM_CollectQuad.usf` (integrate)
- `Shaders/Private/VirtualHeightfieldInitBuffers.usf` (integrate)
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (integrate)
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (integrate)
- **Runtime C++**：7 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Public/HeightfieldMaskTexture.h` (integrate)
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (integrate)
- **uplugin**：1 个文件
- `VirtualHeightfieldMesh.uplugin` (integrate)

📄 查看 VHM 相关 diff（CL 203456）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/HeightfieldMaskRender.usf#2 (text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VHM_CollectQuad.usf#2 (utf8) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldInitBuffers.usf#3 (text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#7 (text) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#9 (text) ====

252a253,255
> float3 VertexFactoryGetPreviousInstanceSpacePosition(FVertexFactoryInput Input, FVertexFactoryIntermediates Intermediates);

> float3 VertexFactoryGetInstanceSpacePosition(FVertexFactoryInput Input, FVertexFactoryIntermediates Intermediates);

>

254c257,262
< FMaterialVertexParameters GetMaterialVertexParameters(FVertexFactoryInput Input, FVertexFactoryIntermediates Intermediates, float3 WorldPosition, half3x3 TangentToLocal)

---
> FMaterialVertexParameters GetMaterialVertexParameters(

>   FVertexFactoryInput Input,

>   FVertexFactoryIntermediates Intermediates,

>   float3 WorldPosition,

>   half3x3 TangentToLocal,

>   bool bIsPreviousFrame = false)

258a267
>

259a269,277
>   if (bIsPreviousFrame)

>   {

>       Result.PositionInstanceSpace = VertexFactoryGetPreviousInstanceSpacePosition(Input, Intermediates);

>   }

>   else

>   {

>       Result.PositionInstanceSpace = VertexFactoryGetInstanceSpacePosition(Input, Intermediates);

>   }

>   Result.PositionPrimitiveSpace = Result.PositionInstanceSpace; // No support for instancing, so instance == primitive

279a298,299
>   Result.LWCData = MakeMaterialLWCData(Result);

>

298,301c318,325
<   FLWCMatrix LocalToWorld = GetPrimitiveData(Intermediates).LocalToWorld;

<   FLWCVector3 WorldPosition = LWCMultiply(Intermediates.LocalPos, LocalToWorld);

<   float3 TranslatedWorldPosition = LWCToFloat(LWCAdd(WorldPosition, ResolvedView.PreViewTranslation));

<   return float4(TranslatedWorldPosition, 1.0f);

---
>   FDFMatrix LocalToWorld = GetPrimitiveData(Intermediates).LocalToWorld;

>   return TransformLocalToTranslatedWorld(Intermediates.LocalPos, LocalToWorld);

> }

>

> // local position relative to instance

> float3 VertexFactoryGetInstanceSpacePosition(FVertexFactoryInput Input, FVertexFactoryIntermediates Intermediates)

> {

>   return Intermediates.LocalPos; // No support for instancing, so instance == primitive

308c332
<   float4x4 WorldToLocal = GetPrimitiveData(Intermediates).WorldToLocal;

---
>   float4x4 TranslatedWorldToLocal = DFFastToTranslatedWorld(GetPrimitiveData(Intermediates).WorldToLocal, ResolvedView.PreViewTranslation);

311c335
<   float3 LocalPos = mul(float4(InWorldPosition.xyz - ResolvedView.PreViewTranslation.xyz, 1), WorldToLocal).xyz;

---
>   float3 LocalPos = mul(float4(InWorldPosition.xyz, 1), TranslatedWorldToLocal).xyz;

328,329c352,358
<   float4x4 PreviousLocalToWorldTranslated = LWCMultiplyTranslation(GetPrimitiveData(Intermediates).PreviousLocalToWorld, ResolvedView.PrevPreViewTranslation);

<   return mul(float4(Intermediates.LocalPos,1), PreviousLocalToWorldTranslated);

---
>   return DFTransformLocalToTranslatedWorld(Intermediates.LocalPos, GetPrimitiveData(Intermediates).PreviousLocalToWorld, ResolvedView.PrevPreViewTranslation);

> }

>

> /** Computes the instance space position of this vertex last frame. */

> float3 VertexFactoryGetPreviousInstanceSpacePosition(FVertexFactoryInput Input, FVertexFactoryIntermediates Intermediates)

> {

>   return Intermediates.LocalPos; // No support for instancing, so instance == primitive

348c377
<   return float4(LWCToFloat(LWCAdd(PrimitiveData.ObjectWorldPosition, ResolvedView.PreViewTranslation)), PrimitiveData.ObjectRadius);

---
>   return float4(DFFastToTranslatedWorld(PrimitiveData.ObjectWorldPosition, ResolvedView.PreViewTranslation), PrimitiveData.ObjectRadius);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp#3 (unicode) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#14 (unicode) ====

5a6
> #include "RHIGlobals.h"


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#19 (unicode) ====

29a30
> UE_DISABLE_OPTIMIZATION
193c194
<   void InitializeInstanceBuffers(FRHICommandListImmediate& InRHICmdList, FDrawInstanceBuffers& InBuffers);
---
>   void InitializeInstanceBuffers(FRHICommandListBase& InRHICmdList, FDrawInstanceBuffers& InBuffers);
231,238c232,239
<           int32 CalTime = -1;
<           // use to compure shader
<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};
<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};
<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};
<           TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};
<           // TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};
<           // TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};
---
>           //int32 CalTime = -1;
>           //// use to compute shader
>           //TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadArgsBuffer{nullptr, nullptr};
>           //// TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferUAV{nullptr, nullptr};
>           //// TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadArgsBufferSRV{nullptr, nullptr};
>           //TArray<TRefCountPtr<FRDGPooledBuffer>, TFixedAllocator<2>> FinalQuadBuffer{nullptr, nullptr};
>           //// TArray<FUnorderedAccessViewRHIRef, TFixedAllocator<2>> FinalQuadBufferUAV{nullptr, nullptr};
>           //// TArray<FShaderResourceViewRHIRef, TFixedAllocator<2>> FinalQuadBufferSRV{nullptr, nullptr};
240,256c241,257
<           FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
<           }
<           FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
<           }
<
<           FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
<           }
<           FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
<           {
<               return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
<           }
---
>           //FRDGBufferSRVRef GetFinalQuadArgsSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
>           //{
>           //  return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
>           //}
>           //FRDGBufferUAVRef GetFinalQuadArgsUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
>           //{
>           //  return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadArgsBuffer[Idx]));
>           //}
>           //
>           //FRDGBufferSRVRef GetFinalQuadSRV(FRDGBuilder& GraphBuilder, uint32 Idx) const
>           //{
>           //  return GraphBuilder.CreateSRV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
>           //}
>           //FRDGBufferUAVRef GetFinalQuadUAV(FRDGBuilder& GraphBuilder, uint32 Idx) const
>           //{
>           //  return GraphBuilder.CreateUAV(GraphBuilder.RegisterExternalBuffer(FinalQuadBuffer[Idx]), PF_R32G32B32A32_UINT);
>           //}
259c260
<       void InitializeInnerBuffers(FRHICommandListImmediate& RHICmdList, FInnerBuffers& InBuffers)
---
>       void InitializeInnerBuffers(FRHICommandListBase& RHICmdList, FInnerBuffers& InBuffers)
261,267c262,268
<           const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();
<           const TCHAR* FinalQuadName[2] = {
<               TEXT("VHM.FinalQuadBuffer_0"),
<               TEXT("VHM.FinalQuadBuffer_1")};
<           const TCHAR* FinalQuadArgsName[2] = {
<               TEXT("VHM.FinalQuadArgsBuffer_0"),
<               TEXT("VHM.FinalQuadArgsBuffer_1")};
---
>           //const int32 InstanceBufferSize = CVarVHMMaxRenderItems.GetValueOnAnyThread();
>           //const TCHAR* FinalQuadName[2] = {
>           //  TEXT("VHM.FinalQuadBuffer_0"),
>           //  TEXT("VHM.FinalQuadBuffer_1")};
>           //const TCHAR* FinalQuadArgsName[2] = {
>           //  TEXT("VHM.FinalQuadArgsBuffer_0"),
>           //  TEXT("VHM.FinalQuadArgsBuffer_1")};
269,281c270,282
<           for (int i = 0; i < 2; ++i)
<           {
<               InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(
<                   FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),
<                   FinalQuadArgsName[i]
<               );
<               InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(
<                   FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)
<
<                   ,
<                   FinalQuadName[i]
<               );
<           }
---
>           //for (int i = 0; i < 2; ++i)
>           //{
>           //  InBuffers.FinalQuadArgsBuffer[i] = AllocatePooledBuffer(
>           //      FRDGBufferDesc::CreateIndirectDesc(4 * sizeof(uint32)),
>           //      FinalQuadArgsName[i]
>           //  );
>           //  InBuffers.FinalQuadBuffer[i] = AllocatePooledBuffer(
>           //      FRDGBufferDesc::CreateBufferDesc(4 * sizeof(uint32), InstanceBufferSize)
>           //
>           //      ,
>           //      FinalQuadName[i]
>           //  );
>           //}
286,291c287,292
<           InBuffers.CalTime = -1;
<           for(int i = 0; i < 2; ++i)
<           {
<               InBuffers.FinalQuadArgsBuffer[i].SafeRelease();
<               InBuffers.FinalQuadBuffer[i].SafeRelease();
<           }
---
>           //InBuffers.CalTime = -1;
>           //for(int i = 0; i < 2; ++i)
>           //{
>           //  InBuffers.FinalQuadArgsBuffer[i].SafeRelease();
>           //  InBuffers.FinalQuadBuffer[i].SafeRelease();
>           //}
300c301
<   FTexture2DRHIRef OcclusionTexture;
---
>   FTextureRHIRef OcclusionTexture;
384a386,388
>
>           FRDGBufferRef FinalQuadArgsBuffer = nullptr;
>           FRDGBufferRef FinalQuadBuffer = nullptr;
401a406,409
>           FSRVAndUAV FinalQuadArgsViews;
>           FSRVAndUAV FinalQuadViews;
>
>
465a474,476
>   // AddWrok in WorkGraph
>   UE::FRecursiveMutex Mutex;
>
532a544,545
>   UE::TScopeLock Lock(Mutex);
>
586a600,602
>   UE::TScopeLock Lock(Mutex);
>
>
619a636,637
>   UE::TScopeLock Lock(Mutex);
>
751c769
< void FVirtualHeightfieldMeshSceneProxy::OnTransformChanged()
---
> void FVirtualHeightfieldMeshSceneProxy::OnTransformChanged(FRHICommandListBase& RHICmdList)
761c779
< void FVirtualHeightfieldMeshSceneProxy::CreateRenderThreadResources()
---
> void FVirtualHeightfieldMeshSceneProxy::CreateRenderThreadResources(FRHICommandListBase& RHICmdList)
857c875
<   SceneProxy->CreateRenderThreadResources();
---
>   SceneProxy->CreateRenderThreadResources(FRHICommandListImmediate::Get());
881c899
<   check(IsInRenderingThread());
---
>   //check(IsInRenderingThread());
1644c1662
<   void InitializeInstanceBuffers(FRHICommandListImmediate& RHICmdList, FDrawInstanceBuffers& InBuffers)
---
>   void InitializeInstanceBuffers(FRHICommandListBase& RHICmdList, FDrawInstanceBuffers& InBuffers)
1745a1764,1775
>
>           OutResources.FinalQuadArgsBuffer = GraphBuilder.CreateBuffer(
>               FRDGBufferDesc::CreateIndirectDesc(IndirectArgsByteSize),
>               TEXT("VHM.FinalQuadArgsBuffer")
>           );
>           OutResources.FinalQuadArgsViews.Create(GraphBuilder, OutResources.FinalQuadArgsBuffer);
>
>           OutResources.FinalQuadBuffer = GraphBuilder.CreateBuffer(
>               FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),
>               TEXT("VHM.FinalQuadBuffer")
>           );
>           OutResources.FinalQuadViews.Create(GraphBuilder, OutResources.FinalQuadBuffer);
1981c2011
<           Parameters->FinalArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, (InBuffers.CalTime + 1) % 2);
---
>           Parameters->FinalArgsBuffer = InVolatileBuffers.FinalQuadArgsViews.UAV;
2076,2078c2106,2107
<           uint32 NextCalTime = (InBuffers.CalTime + 1) % 2;
<           Parameters->FinalDispatchArgsBuffer = InBuffers.GetFinalQuadArgsUAV(GraphBuilder, NextCalTime);
<           Parameters->FinalQuadBuffer = InBuffers.GetFinalQuadUAV(GraphBuilder, NextCalTime);
---
>           Parameters->FinalDispatchArgsBuffer = InVolatileBuffers.FinalQuadArgsViews.UAV;
>           Parameters->FinalQuadBuffer = InVolatileBuffers.FinalQuadViews.UAV;
2200d2228
<           const int32 BufIdx = (InBuffers.CalTime + 1) % 2;
2204c2232
<           FRDGBufferRef InDispatchArgsBuffer = GraphBuilder.RegisterExternalBuffer(InBuffers.FinalQuadArgsBuffer[BufIdx]);
---
>           FRDGBufferRef InDispatchArgsBuffer = InVolatileBuffers.FinalQuadArgsBuffer;
2206,2207c2234,2235
<           Parameters->InDispatchArgsBuffer = GraphBuilder.CreateSRV(InDispatchArgsBuffer);
<           Parameters->InQuadBuffer = InBuffers.GetFinalQuadSRV(GraphBuilder, BufIdx);
---
>           Parameters->InDispatchArgsBuffer = InVolatileBuffers.FinalQuadArgsViews.SRV;
>           Parameters->InQuadBuffer = InVolatileBuffers.FinalQuadViews.SRV;
2713c2741
<       WorkBuffers.CalTime = (WorkBuffers.CalTime + 1) % 2;
---
>       // WorkBuffers.CalTime = (WorkBuffers.CalTime + 1) % 2;
2815c2843
< PRAGMA_ENABLE_OPTIMIZATION
---
> UE_ENABLE_OPTIMIZATION


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.h#9 (unicode) ====

25c25
<   virtual void CreateRenderThreadResources() override;
---
>   virtual void CreateRenderThreadResources(FRHICommandListBase& RHICmdList) override;
27c27
<   virtual void OnTransformChanged() override;
---
>   virtual void OnTransformChanged(FRHICommandListBase& RHICmdList) override;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp#4 (unicode) ====

8a9
> #include "RHIResourceUtils.h"

19c20
<       TResourceArray<T, INDEXBUFFER_ALIGNMENT> Indices;

---
>       TArray<T> Indices;

68,73c69
<       const uint32 Size = Indices.GetResourceDataSize();

<       const uint32 Stride = sizeof(T);

<

<       // Create index buffer. Fill buffer with initial data upon creation

<       FRHIResourceCreateInfo CreateInfo(TEXT("FVirtualHeightfieldMeshIndexBuffer"), &Indices);

<       return RHICmdList.CreateIndexBuffer(Stride, Size, BUF_Static, CreateInfo);

---
>       return UE::RHIResourceUtils::CreateIndexBufferFromArray(RHICmdList, TEXT("FVirtualHeightfieldMeshIndexBuffer"), EBufferUsageFlags::Static, MakeConstArrayView(Indices));


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/HeightfieldMaskTexture.h#2 (unicode) ====


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#7 (unicode) ====

315d314
<                   Desc.DebugType = ERuntimeVirtualTextureDebugType::None;

523d521
<                       Desc.DebugType = ERuntimeVirtualTextureDebugType::None;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/VirtualHeightfieldMesh.uplugin#2 (text) ====

1,29c1,29
< {

<   "FileVersion": 3,

<   "Version": 1,

<   "VersionName": "0.1",

<   "FriendlyName": "Virtual Heightfield Mesh",

<   "Description": "Mesh renderer for virtual texture heightfields",

<   "Category": "Rendering",

<   "CreatedBy": "Epic Games, Inc.",

<   "CreatedByURL": "https://epicgames.com",

<   "DocsURL": "",

<   "MarketplaceURL": "",

<   "SupportURL": "",

<   "EnabledByDefault": false,

<   "CanContainContent": true,

<   "IsExperimentalVersion": true,

<   "Installed": false,

<   "Modules": [

<       {

<           "Name": "VirtualHeightfieldMesh",

<           "Type": "Runtime",

<           "TargetDenyList": [ "Server" ],

<           "LoadingPhase": "PostConfigInit"

<       },

<       {

<           "Name": "VirtualHeightfieldMeshEditor",

<           "Type": "Editor"

<       }

<   ]

< }

---
> {
>   "FileVersion": 3,
>   "Version": 1,
>   "VersionName": "0.1",
>   "FriendlyName": "Virtual Heightfield Mesh",
>   "Description": "Mesh renderer for virtual texture heightfields",
>   "Category": "Rendering",
>   "CreatedBy": "Epic Games, Inc.",
>   "CreatedByURL": "https://epicgames.com",
>   "DocsURL": "",
>   "MarketplaceURL": "",
>   "SupportURL": "",
>   "EnabledByDefault": false,
>   "CanContainContent": true,
>   "IsExperimentalVersion": true,
>   "Installed": false,
>   "Modules": [
>       {
>           "Name": "VirtualHeightfieldMesh",
>           "Type": "Runtime",
>           "TargetDenyList": [ "Server" ],
>           "LoadingPhase": "PostConfigInit"
>       },
>       {
>           "Name": "VirtualHeightfieldMeshEditor",
>           "Type": "Editor"
>       }
>   ]
> }
```

#### CL 206628 — 2024/12/26 — 郭智均

- **提交说明**：--bug=1052327 --user=郭智均 【Crash】进入对局选角阶段结束进入场景偶现崩溃，FD3D12DynamicRHI::RHICreateShaderResourceView() [D:\projects\trunk\UE5EA\Engine\Source\Runtime\D3D12RHI\Private\D3D12SRV.cpp:335] https://www.tapd.cn/68880148/s/1932425
- **TAPD**：bug=1052327
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【Crash】进入对局选角阶段结束进入场景偶现崩溃，FD3D12DynamicRHI::RHICreateShaderResourceView() [D:\projects\trunk\UE5EA\Engine\Source\Runtime\D3D12RHI\Private\D3D12SRV.cpp:335] https://www.tapd.cn/68880148/s/1932425

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 206628）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#20 (unicode) ====

30c30
< UE_DISABLE_OPTIMIZATION
---
> // UE_DISABLE_OPTIMIZATION
442c442
<   VirtualHeightfieldMesh::FDrawInstanceBuffers& AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView);
---
>   VirtualHeightfieldMesh::FDrawInstanceBuffers& AddWork(FRHICommandListBase& RHICmdList, FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView);
542c542
< VirtualHeightfieldMesh::FDrawInstanceBuffers& FVirtualHeightfieldMeshRendererExtension::AddWork(FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView)
---
> VirtualHeightfieldMesh::FDrawInstanceBuffers& FVirtualHeightfieldMeshRendererExtension::AddWork(FRHICommandListBase& RHICmdList, FVirtualHeightfieldMeshSceneProxy const* InProxy, FSceneView const* InMainView, FSceneView const* InCullView)
591,592c591,592
<       VirtualHeightfieldMesh::InitializeInstanceBuffers(GetImmediateCommandList_ForRenderCommand(), Buffers[WorkDesc.BufferIndex]);
<       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(GetImmediateCommandList_ForRenderCommand(), InnerBuffers[WorkDesc.BufferIndex]);
---
>       VirtualHeightfieldMesh::InitializeInstanceBuffers(RHICmdList, Buffers[WorkDesc.BufferIndex]);
>       VirtualHeightfieldMesh::V2::InitializeInnerBuffers(RHICmdList, InnerBuffers[WorkDesc.BufferIndex]);
920c920
<           VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers = GVirtualHeightfieldMeshViewRendererExtension.AddWork(this, ViewFamily.Views[0], Views[ViewIndex]);
---
>           VirtualHeightfieldMesh::FDrawInstanceBuffers& Buffers = GVirtualHeightfieldMeshViewRendererExtension.AddWork(Collector.GetRHICommandList(), this, ViewFamily.Views[0], Views[ViewIndex]);
2843c2843
< UE_ENABLE_OPTIMIZATION
---
> // UE_ENABLE_OPTIMIZATION
```

#### CL 222041 — 2025/01/14 — 郭智均

- **提交说明**：--bug=1059699 --user=郭智均 【场景】全场景地形均不显示 https://www.tapd.cn/68880148/s/1974503
- **TAPD**：bug=1059699
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【场景】全场景地形均不显示 https://www.tapd.cn/68880148/s/1974503

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (edit)

📄 查看 VHM 相关 diff（CL 222041）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#8 (unicode) ====

518c518
<                       Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Custom_SecondColor_YCoCg;

---
>                       Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;
```

#### CL 223563 — 2025/01/15 — 郭智均

- **提交说明**：--bug=1059751 --user=郭智均 【构建】SVT流水线构建异常 https://www.tapd.cn/68880148/s/1977524
- **TAPD**：bug=1059751
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【构建】SVT流水线构建异常 https://www.tapd.cn/68880148/s/1977524

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTexture.cpp` (edit)

📄 查看 VHM 相关 diff（CL 223563）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTexture.cpp#4 (unicode) ====

157c157
<       LodBiasTexture->Source.Init(InBuildDesc.SizeX, InBuildDesc.SizeY, 1, InBuildDesc.NumMips, TSF_G8, LodBiasTextureData.GetData());

---
>       LodBiasTexture->Source.Init(InBuildDesc.SizeX, InBuildDesc.SizeY, 1, 1, TSF_G8, LodBiasTextureData.GetData());
```

#### CL 291439 — 2025/03/29 — 张建国\_20240109032154

- **提交说明**：--story=1047503 --user=张建国\_20240109032154 【RVT 三岛合并】接入Trunk https://www.tapd.cn/68880148/s/2206104
- **TAPD**：story=1047503
- **涉及 VHM 文件**：11 个

**做了什么**：

提交目的：【RVT 三岛合并】接入Trunk https://www.tapd.cn/68880148/s/2206104

- **Runtime C++**：11 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.h` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)
- `Source/VirtualHeightfieldMesh/Public/HeightfieldMaskTexture.h` (integrate)
- `Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h` (integrate)
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureThumbnailRenderer.cpp` (integrate)
- …（其余 3 个略）

📄 查看 VHM 相关 diff（CL 291439）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMaskTexture.cpp#4 (unicode) ====

17c17
< void UHeightfieldMaskTexture::BuildTexture(const FHeightfieldMaskTextureBuildDesc& InBuildDesc)

---
> void UHeightfieldMaskTexture::BuildTexture(const FHeightfieldMaskTextureBuildDesc& InBuildDesc, UTexture2D* InOutTexture)

18a19
>   check(InOutTexture);

20c21
<       Texture = NewObject<UTexture2D>(this, TEXT("HeightfieldMaskTexture"));

---
>       //InOutTexture = NewObject<UTexture2D>(this, TEXT("HeightfieldMaskTexture"));

27,33c28,34
<       Texture->Filter = TF_Nearest;

<       Texture->MipGenSettings = TMGS_LeaveExistingMips;

<       Texture->MipLoadOptions = ETextureMipLoadOptions::AllMips;

<       Texture->NeverStream = true;

<       Texture->LODGroup = TEXTUREGROUP_Project02; // Calculate data

<       Texture->SetLayerFormatSettings(0, Settings);

<       Texture->Source.Init(InBuildDesc.SizeX, InBuildDesc.SizeY, 1, InBuildDesc.NumMips, TSF_G8, InBuildDesc.Data);

---
>       InOutTexture->Filter = TF_Nearest;

>       InOutTexture->MipGenSettings = TMGS_LeaveExistingMips;

>       InOutTexture->MipLoadOptions = ETextureMipLoadOptions::AllMips;

>       InOutTexture->NeverStream = true;

>       InOutTexture->LODGroup = TEXTUREGROUP_Project02; // Calculate data

>       InOutTexture->SetLayerFormatSettings(0, Settings);

>       InOutTexture->Source.Init(InBuildDesc.SizeX, InBuildDesc.SizeY, 1, InBuildDesc.NumMips, TSF_G8, InBuildDesc.Data);

35c36
<       Texture->PostEditChange();

---
>       InOutTexture->PostEditChange();

38c39
<   VirtualHeightfieldMesh::NotifyComponents(this);

---
>   VirtualHeightfieldMesh::NotifyComponents(InOutTexture);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.cpp#4 (unicode) ====

29c29
<   void NotifyComponents(UHeightfieldMaskTexture const* MaskTexture)

---
>   void NotifyComponents(UTexture2D const* MaskTexture)


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxTextureNotify.h#4 (unicode) ====

18c18
<   void NotifyComponents(UHeightfieldMaskTexture const* MaskTexture);

---
>   void NotifyComponents(UTexture2D const* MaskTexture);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshComponent.cpp#6 (unicode) ====

218c218
<       MaskTexture->BuildTexture(BuildDesc);

---
>       UHeightfieldMaskTexture::BuildTexture(BuildDesc, MaskTexture);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#21 (unicode) ====

738c738
<   UHeightfieldMaskTexture* HeightfieldMaskTexture = InComponent->GetMaskTexture();
---
>   UTexture2D* HeightfieldMaskTexture = InComponent->GetMaskTexture();
741c741
<       MaskTexture = HeightfieldMaskTexture->Texture;
---
>       MaskTexture = HeightfieldMaskTexture;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/HeightfieldMaskTexture.h#3 (unicode) ====

23,24c23,24
<   UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = Texture, meta = (DisplayName = "Height Mask Texture"))

<   TObjectPtr<class UTexture2D> Texture = nullptr;

---
>   //UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = Texture, meta = (DisplayName = "Height Mask Texture"))

>   //TObjectPtr<class UTexture2D> Texture = nullptr;

27c27
<   void BuildTexture(const FHeightfieldMaskTextureBuildDesc& InBuildDesc);

---
>   static void BuildTexture(const FHeightfieldMaskTextureBuildDesc& InBuildDesc, UTexture2D *InOutTexture);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h#6 (unicode) ====

54c54
<   TObjectPtr<UHeightfieldMaskTexture> MaskTexture = nullptr;
---
>   TObjectPtr<UTexture2D> MaskTexture = nullptr;
146c146
<   UHeightfieldMaskTexture* GetMaskTexture() const { return MaskTexture; }
---
>   UTexture2D* GetMaskTexture() const { return MaskTexture; }
148c148
<   void SetMaskTexture(UHeightfieldMaskTexture* InTexture) { MaskTexture = InTexture; }
---
>   void SetMaskTexture(UTexture2D* InTexture) { MaskTexture = InTexture; }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureThumbnailRenderer.cpp#4 (unicode) ====

40,60c40,60
< UHeightfieldMaskTextureThumbnailRenderer::UHeightfieldMaskTextureThumbnailRenderer(const FObjectInitializer& ObjectInitializer)

<   : Super(ObjectInitializer)

< {

< }

<

< bool UHeightfieldMaskTextureThumbnailRenderer::CanVisualizeAsset(UObject* Object)

< {

<   const UHeightfieldMaskTexture* MaskTextureBuilder = Cast<UHeightfieldMaskTexture>(Object);

<   UTexture2D* Texture = MaskTextureBuilder != nullptr ? MaskTextureBuilder->Texture : nullptr;

<   return Texture != nullptr ? UTextureThumbnailRenderer::CanVisualizeAsset(Texture) : false;

< }

<

< void UHeightfieldMaskTextureThumbnailRenderer::Draw(UObject* Object, int32 X, int32 Y, uint32 Width, uint32 Height, FRenderTarget* RenderTarget, FCanvas* Canvas, bool bAdditionalViewFamily)

< {

<   const UHeightfieldMaskTexture* MaskTextureBuilder = Cast<UHeightfieldMaskTexture>(Object);

<   UTexture2D* Texture = MaskTextureBuilder != nullptr ? MaskTextureBuilder->Texture : nullptr;

<   if (Texture != nullptr)

<   {

<       UTextureThumbnailRenderer::Draw(Texture, X, Y, Width, Height, RenderTarget, Canvas, bAdditionalViewFamily);

<   }

< }

---
> //UHeightfieldMaskTextureThumbnailRenderer::UHeightfieldMaskTextureThumbnailRenderer(const FObjectInitializer& ObjectInitializer)

> //    : Super(ObjectInitializer)

> //{

> //}

> //

> //bool UHeightfieldMaskTextureThumbnailRenderer::CanVisualizeAsset(UObject* Object)

> //{

> //    const UHeightfieldMaskTexture* MaskTextureBuilder = Cast<UHeightfieldMaskTexture>(Object);

> //    UTexture2D* Texture = MaskTextureBuilder != nullptr ? MaskTextureBuilder->Texture : nullptr;

> //    return Texture != nullptr ? UTextureThumbnailRenderer::CanVisualizeAsset(Texture) : false;

> //}

> //

> //void UHeightfieldMaskTextureThumbnailRenderer::Draw(UObject* Object, int32 X, int32 Y, uint32 Width, uint32 Height, FRenderTarget* RenderTarget, FCanvas* Canvas, bool bAdditionalViewFamily)

> //{

> //    const UHeightfieldMaskTexture* MaskTextureBuilder = Cast<UHeightfieldMaskTexture>(Object);

> //    UTexture2D* Texture = MaskTextureBuilder != nullptr ? MaskTextureBuilder->Texture : nullptr;

> //    if (Texture != nullptr)

> //    {

> //        UTextureThumbnailRenderer::Draw(Texture, X, Y, Width, Height, RenderTarget, Canvas, bAdditionalViewFamily);

> //    }

> //}


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureThumbnailRenderer.h#4 (unicode) ====

19,30c19,30
< #pragma region S1_Engine_Shiyu

< UCLASS(MinimalAPI)

< class UHeightfieldMaskTextureThumbnailRenderer : public UTextureThumbnailRenderer

< {

<   GENERATED_UCLASS_BODY()

<

<   //~ Begin UThumbnailRenderer Interface.

<   virtual bool CanVisualizeAsset(UObject* Object) override;

<   virtual void Draw(UObject* Object, int32 X, int32 Y, uint32 Width, uint32 Height, FRenderTarget*, FCanvas* Canvas, bool bAdditionalViewFamily) override;

<   //~ EndUThumbnailRenderer Interface.

< };

< #pragma endregion

---
> //#pragma region S1_Engine_Shiyu

> //UCLASS(MinimalAPI)

> //class UHeightfieldMaskTextureThumbnailRenderer : public UTextureThumbnailRenderer

> //{

> //    GENERATED_UCLASS_BODY()

> //

> //    //~ Begin UThumbnailRenderer Interface.

> //    virtual bool CanVisualizeAsset(UObject* Object) override;

> //    virtual void Draw(UObject* Object, int32 X, int32 Y, uint32 Width, uint32 Height, FRenderTarget*, FCanvas* Canvas, bool bAdditionalViewFamily) override;

> //    //~ EndUThumbnailRenderer Interface.

> //};

> //#pragma endregion


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/VirtualHeightfieldMeshDetailsCustomization.cpp#4 (unicode) ====

26a27
> #include "Factories/Texture2dFactoryNew.h"

221c222
<   UHeightfieldMaskTexture* CreatedTexture = nullptr;

---
>   UTexture2D* CreatedTexture = nullptr;

229,231c230,232
<       UFactory* Factory = NewObject<UHeightfieldMaskTextureFactory>();

<       UObject* Object = AssetToolsModule.Get().CreateAssetWithDialog(DefaultName, DefaultPath, UHeightfieldMaskTexture::StaticClass(), Factory);

<       CreatedTexture = Cast<UHeightfieldMaskTexture>(Object);

---
>       UFactory* Factory = NewObject<UTexture2DFactoryNew>();

>       UObject* Object = AssetToolsModule.Get().CreateAssetWithDialog(DefaultName, DefaultPath, UTexture2D::StaticClass(), Factory);

>       CreatedTexture = Cast<UTexture2D>(Object);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/VirtualHeightfieldMeshEditorModule.cpp#4 (unicode) ====

43c43
<   UThumbnailManager::Get().RegisterCustomRenderer(UHeightfieldMaskTexture::StaticClass(), UHeightfieldMaskTextureThumbnailRenderer::StaticClass());

---
>   //UThumbnailManager::Get().RegisterCustomRenderer(UHeightfieldMaskTexture::StaticClass(), UHeightfieldMaskTextureThumbnailRenderer::StaticClass());
```

#### CL 439014 — 2025/06/27 — 郭智均

- **提交说明**：--story=1057531 --user=郭智均 【代码合并】Release -> trunk https://www.tapd.cn/68880148/s/2689961 --MergedFrom=//GR/release
- **TAPD**：story=1057531
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【代码合并】Release -> trunk https://www.tapd.cn/68880148/s/2689961 --MergedFrom=//GR/release

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 439014）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#22 (unicode) ====

113a114,129
> static float GVHMAddLodDistribution = 0;
> static FAutoConsoleVariableRef CVarVHMAddLodDistribution(
>   TEXT("r.VHM.AddLodDistribution"),
>   GVHMAddLodDistribution,
>   TEXT(""),
>   ECVF_RenderThreadSafe
> );
>
> static int32 GVHMAddLod0LevelBias = 0;
> static FAutoConsoleVariableRef CVarVHMLod0LevelBias(
>   TEXT("r.VHM.AddLod0LevelBias"),
>   GVHMAddLod0LevelBias,
>   TEXT(""),
>   ECVF_RenderThreadSafe
> );
>
340c356,357
<       const uint32 MaxLevel = FMath::Max(InProxy->MaxLevel - InProxy->Lod0LevelBias, 0);
---
>       const int32 Lod0LevelBias = InProxy->Lod0LevelBias + GVHMAddLod0LevelBias;
>       const uint32 MaxLevel = FMath::Max(InProxy->MaxLevel - Lod0LevelBias, 0);
347a365
>       const float LodDistribution = InProxy->LodDistribution + GVHMAddLodDistribution;
349c367
<       return FVector4f(Lod0Distance, InProxy->Lod0Distribution, InProxy->LodDistribution, LodScale);
---
>       return FVector4f(Lod0Distance, InProxy->Lod0Distribution, LodDistribution, LodScale);
```

#### CL 479212 — 2025/07/26 — 陈永昊

- **提交说明**：--story=1057052 --user=陈永昊 地形Nanite —— 适配VHM https://www.tapd.cn/68880148/s/2833592
- **TAPD**：story=1057052
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：地形Nanite —— 适配VHM https://www.tapd.cn/68880148/s/2833592

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 479212）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#15 (unicode) ====

10a11
> #include "WorldPartition/WorldPartition.h"
43a45,85
> #pragma region Engine CYH
>       // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
>       const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
>       const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
>       const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
>       const bool bLandscapeNaniteEnabled = (LandscapeNaniteCVar != nullptr) ? (LandscapeNaniteCVar->GetInt() != 0) : true;
>
>       if (bNaniteEnabled && bLandscapeNaniteEnabled)
>       {
>           if (CVarVHMEnable.GetValueOnGameThread() != 0)
>           {
>               CVarVHMEnable->Set(0, ECVF_SetByConsole);
>               for (const FWorldContext& Context : GEngine->GetWorldContexts())
>               {
>                   if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
>                   {
>                       AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
>                       FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
>                       WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
>                   }
>               }
>           }
>       }
>       else
>       {
>           if (CVarVHMEnable.GetValueOnGameThread() != 1)
>           {
>               CVarVHMEnable->Set(1, ECVF_SetByConsole);
>               for (const FWorldContext& Context : GEngine->GetWorldContexts())
>               {
>                   if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
>                   {
>                       AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
>                       FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
>                       WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
>                   }
>               }
>           }
>       }
> #pragma endregion
>
```

#### CL 479570 — 2025/07/28 — 陈永昊

- **提交说明**：--bug=1110806 --user=陈永昊 【EA】【7月W3】【Crash】包体启动崩溃D: /projects\trunk\UE5EA\Enaine/Pluains\Experimenttal\VirtualHeightfield Mesh \Source\Virtual HeightfieldMesh Private\Virtual Heightfield MeshEnable.cpp:5 https://www.tapd.cn/68880148/s/2834806
- **TAPD**：bug=1110806
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA】【7月W3】【Crash】包体启动崩溃D: /projects\trunk\UE5EA\Enaine/Pluains\Experimenttal\VirtualHeightfield Mesh \Source\Virtual HeightfieldMesh Private\Virtual Heightfield MeshEnable.cpp:5 https://www.tapd.cn/68880148/s/2834806

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 479570）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#16 (unicode) ====

45,84c45,84
< #pragma region Engine CYH
<       // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
<       const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
<       const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
<       const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
<       const bool bLandscapeNaniteEnabled = (LandscapeNaniteCVar != nullptr) ? (LandscapeNaniteCVar->GetInt() != 0) : true;
<
<       if (bNaniteEnabled && bLandscapeNaniteEnabled)
<       {
<           if (CVarVHMEnable.GetValueOnGameThread() != 0)
<           {
<               CVarVHMEnable->Set(0, ECVF_SetByConsole);
<               for (const FWorldContext& Context : GEngine->GetWorldContexts())
<               {
<                   if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
<                   {
<                       AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
<                       FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
<                       WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
<                   }
<               }
<           }
<       }
<       else
<       {
<           if (CVarVHMEnable.GetValueOnGameThread() != 1)
<           {
<               CVarVHMEnable->Set(1, ECVF_SetByConsole);
<               for (const FWorldContext& Context : GEngine->GetWorldContexts())
<               {
<                   if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
<                   {
<                       AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
<                       FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
<                       WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
<                   }
<               }
<           }
<       }
< #pragma endregion
---
> // #pragma region Engine CYH
> //        // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
> //        const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
> //        const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
> //        const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
> //        const bool bLandscapeNaniteEnabled = (LandscapeNaniteCVar != nullptr) ? (LandscapeNaniteCVar->GetInt() != 0) : true;
> //
> //        if (bNaniteEnabled && bLandscapeNaniteEnabled)
> //        {
> //            if (CVarVHMEnable.GetValueOnGameThread() != 0)
> //            {
> //                CVarVHMEnable->Set(0, ECVF_SetByConsole);
> //                for (const FWorldContext& Context : GEngine->GetWorldContexts())
> //                {
> //                    if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
> //                    {
> //                        AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
> //                        FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
> //                        WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
> //                    }
> //                }
> //            }
> //        }
> //        else
> //        {
> //            if (CVarVHMEnable.GetValueOnGameThread() != 1)
> //            {
> //                CVarVHMEnable->Set(1, ECVF_SetByConsole);
> //                for (const FWorldContext& Context : GEngine->GetWorldContexts())
> //                {
> //                    if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
> //                    {
> //                        AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
> //                        FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
> //                        WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
> //                    }
> //                }
> //            }
> //        }
> // #pragma endregion
```

#### CL 481558 — 2025/07/29 — 陈永昊

- **提交说明**：--story=1057052 --user=陈永昊 地形Nanite —— 适配VHM https://www.tapd.cn/68880148/s/2843091
- **TAPD**：story=1057052
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：地形Nanite —— 适配VHM https://www.tapd.cn/68880148/s/2843091

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 481558）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#17 (unicode) ====

45,84c45,90
< // #pragma region Engine CYH
< //        // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
< //        const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
< //        const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
< //        const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
< //        const bool bLandscapeNaniteEnabled = (LandscapeNaniteCVar != nullptr) ? (LandscapeNaniteCVar->GetInt() != 0) : true;
< //
< //        if (bNaniteEnabled && bLandscapeNaniteEnabled)
< //        {
< //            if (CVarVHMEnable.GetValueOnGameThread() != 0)
< //            {
< //                CVarVHMEnable->Set(0, ECVF_SetByConsole);
< //                for (const FWorldContext& Context : GEngine->GetWorldContexts())
< //                {
< //                    if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
< //                    {
< //                        AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
< //                        FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
< //                        WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
< //                    }
< //                }
< //            }
< //        }
< //        else
< //        {
< //            if (CVarVHMEnable.GetValueOnGameThread() != 1)
< //            {
< //                CVarVHMEnable->Set(1, ECVF_SetByConsole);
< //                for (const FWorldContext& Context : GEngine->GetWorldContexts())
< //                {
< //                    if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
< //                    {
< //                        AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
< //                        FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
< //                        WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
< //                    }
< //                }
< //            }
< //        }
< // #pragma endregion
---
> #pragma region Engine CYH
>       // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
>       const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
>       const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
>       const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
>       const bool bLandscapeNaniteEnabled = (LandscapeNaniteCVar != nullptr) ? (LandscapeNaniteCVar->GetInt() != 0) : true;
>
>       if (bNaniteEnabled && bLandscapeNaniteEnabled)
>       {
>           if (CVarVHMEnable.GetValueOnGameThread() != 0)
>           {
>               CVarVHMEnable->Set(0, ECVF_SetByConsole);
>               if (GEngine)
>               {
>                   for (const FWorldContext& Context : GEngine->GetWorldContexts())
>                   {
>                       if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
>                       {
>                           AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
>                           FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
>                           WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
>                       }
>                   }
>               }
>           }
>       }
>       else
>       {
>           if (CVarVHMEnable.GetValueOnGameThread() != 1)
>           {
>               CVarVHMEnable->Set(1, ECVF_SetByConsole);
>               if (GEngine)
>               {
>                   for (const FWorldContext& Context : GEngine->GetWorldContexts())
>                   {
>                       if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
>                       {
>                           AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
>                           FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
>                           WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
>                       }
>                   }
>               }
>           }
>       }
> #pragma endregion
```

#### CL 482700 — 2025/07/29 — 陈永昊

- **提交说明**：--story=1061285 --user=陈永昊 地形Nanite —— 编辑器下关闭VHM https://www.tapd.cn/68880148/s/2847351 #ShelveForSubmit #review-482661 #PrecheckSuccess
- **TAPD**：story=1061285
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：地形Nanite —— 编辑器下关闭VHM https://www.tapd.cn/68880148/s/2847351 #ShelveForSubmit #review-482661 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 482700）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#18 (unicode) ====

43c43
<   static void OnUpdate()
---
>   static void VHMEnableCVarSinkFunction()
46c46,47
<       // Force disable VHM when nanite is enabled (VHM is superseded by nanite)
---
> #if !WITH_EDITOR
>       // Force disable VHM when Nanite is enabled (VHM is superseded by Nanite)
57,68d57
<               if (GEngine)
<               {
<                   for (const FWorldContext& Context : GEngine->GetWorldContexts())
<                   {
<                       if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
<                       {
<                           AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
<                           FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
<                           WorldPartition->SetRuntimeGridEnabled(HLODExclusion, true);
<                       }
<                   }
<               }
76,87d64
<               if (GEngine)
<               {
<                   for (const FWorldContext& Context : GEngine->GetWorldContexts())
<                   {
<                       if (UWorldPartition* WorldPartition = Context.World()->GetWorldPartition(); WorldPartition && Context.World()->IsGameWorld())
<                       {
<                           AWorldSettings* WorldSettings = Context.World()->GetWorldSettings();
<                           FName HLODExclusion = WorldSettings ? WorldSettings->HLODExclusion : NAME_None;
<                           WorldPartition->SetRuntimeGridEnabled(HLODExclusion, false);
<                       }
<                   }
<               }
89a67
> #endif
146c124
<   FAutoConsoleVariableSink GConsoleVariableSink(FConsoleCommandDelegate::CreateStatic(&OnUpdate));
---
>   static FAutoConsoleVariableSink CVarVHMEnableSink(FConsoleCommandDelegate::CreateStatic(&VHMEnableCVarSinkFunction));
```

#### CL 497981 — 2025/08/08 — 杨彬

- **提交说明**：--story=1062793 --user=杨彬 【Bug转需求】【编辑器】打开nordland通过大纲强制加载这几个filter，明显卡顿 https://www.tapd.cn/68880148/s/2893161 增加性能打桩标签 #PrecheckSuccess
- **TAPD**：story=1062793
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【Bug转需求】【编辑器】打开nordland通过大纲强制加载这几个filter，明显卡顿 https://www.tapd.cn/68880148/s/2893161 增加性能打桩标签 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 497981）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#23 (unicode) ====

916a917
>   SCOPED_NAMED_EVENT(FVirtualHeightfieldMeshSceneProxy, FColor::Magenta);
```

#### CL 522266 — 2025/08/26 — 巩汝何

- **提交说明**：--story=1063541 --user=巩汝何 【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug https://www.tapd.cn/68880148/s/2968791 #review-522245 #PrecheckSuccess
- **TAPD**：story=1063541
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug https://www.tapd.cn/68880148/s/2968791 #review-522245 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 522266）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#24 (unicode) ====

619,620c619
<
<
---
>
621a621,636
> #if WITH_EDITOR
>   if (IsRunningCommandlet())
>   {
>       if (!bInFrame)
>       {
>           EndFrame();
>       }
>   }
>   else
>   {
>       if (!ensure(!bInFrame))//Gongruhe: Ensure False Cause Cmd Crash
>       {
>           EndFrame();
>       }
>   }
> #else
625a641,642
> #endif
>
```

#### CL 522727 — 2025/08/26 — 巩汝何

- **提交说明**：--story=1063541 --user=巩汝何 【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug https://www.tapd.cn/68880148/s/2968791 #review-522714 #PrecheckSuccess
- **TAPD**：story=1063541
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【PVS】PVS烘焙室内烘焙精度提升：解决室内剔除错误的Bug https://www.tapd.cn/68880148/s/2968791 #review-522714 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 522727）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#25 (unicode) ====

624c624
<       if (!bInFrame)
---
>       if (bInFrame)
```

#### CL 530838 — 2025/09/02 — 郭智均

- **提交说明**：--story=1065526 --user=郭智均 【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3017441 #review-530816 #PrecheckSuccess
- **TAPD**：story=1065526
- **涉及 VHM 文件**：3 个

**做了什么**：

提交目的：【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3017441 #review-530816 #PrecheckSuccess

- **Shader**：2 个文件
- `Shaders/Private/VirtualHeightfieldInitBuffers.usf` (edit)
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 530838）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldInitBuffers.usf#4 (text) ====

5a6
> RWStructuredBuffer<WorkerQueueInfo> RWQueueInfo;

54a56,59
>

>   RWQueueInfo[0].Read = 0;

>   RWQueueInfo[0].Write = 0;

>   RWQueueInfo[0].NumActive = 0;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#8 (text) ====

18a19,26
> #ifndef VHM_STAT

> #define VHM_STAT 1

> #endif

>

> #ifndef VHM_ONE_PASS

> #define VHM_ONE_PASS 0

> #endif

>

126a135,138
>

> #if VHM_ONE_PASS

> RWStructuredBuffer<WorkerQueueInfo> RWQueueInfo;

> #endif

130a143
> #if !VHM_ONE_PASS

132c145,147
< RWBuffer<uint4> OutMergeQuadBuffer;

---
> #else

> RWCoherentBuffer(uint4) OutSubdivideQuadBuffer;

> #endif

183,184d197
< groupshared uint MergeQuadFlag[COLL_THREAD_TOTAL+1];

< groupshared uint MergeQuadBeginOffset;

252,390d264
< // // No HeightTexture Sampler

< // [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< // void CollectQuadsFromPreFrameCS(

< //    uint3 GroupThreadID : SV_GroupThreadID,

< //    uint3 DispatchThreadID : SV_DispatchThreadID

< // ) {

< //    const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[s_SumQuadOffset];

< //    // if invalid, get 0 index in group thread

< //    const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;

< //

< //    // init

< //    if (ThisThreadID == 0)

< //    {

< //        NumActiveGroupThread = 0;

< //        FeedbackBeginOffset = 0;

< //        SubdivideQuadBeginOffset = 0;

< //        MergeQuadBeginOffset = 0;

< //        FinalQuadBeginOffset = 0;

< //        SubdivideQuadFlag[0] = 0;

< //        MergeQuadFlag[0] = 0;

< //        FinalQuadFlag[0] = 0;

< //    }

< //    FinalQuadFlag[ThisThreadID+1] = 0;

< //    MergeQuadFlag[ThisThreadID+1] = 0;

< //    SubdivideQuadFlag[ThisThreadID+1] = 0;

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    if (IsValidThread)

< //    {

< //        uint _Out;

< //        InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);

< //    }

< //    GroupMemoryBarrierWithGroupSync();

< //

< //

< //    // Get This Quad All Info

< //    const uint2 ThisPackData = InQuadBuffer[LoadIdx];

< //    SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

< //    float ThisHeight = GetHeight(ThisInfo);

< //

< //    // Record Feedback

< //    RecordFeedback(ThisInfo, ThisThreadID, IsValidThread);

< //

< //    uint2 ParPackedValue;

< //    {

< //        ParPackedValue.x = MortonEncode(ThisInfo.Pos >> 1);

< //        uint ParLevel = min(ThisInfo.Level + 1, VHMParam.MaxLevel);

< //        ParPackedValue.x = ParPackedValue.x | (ParLevel << 24);

< //        ParPackedValue.y = 0; // Invalid Parent PhysicalAddress;

< //    }

< //    SQuadInfo ParentInfo = GetQuadInfo(ParPackedValue, VHMParam.RVTMinLevel);

< //    float ParentHeight = GetHeight(ParentInfo);

< //

< //    // todo: shiyu: check, this feedback is not useful

< //    // Record Feedback

< //    // RecordFeedback(ParentInfo, ThisThreadID, IsValidThread);

< //

< //    // sample height texture

< //    // const float ThisMinDistanceLod = ThisInfo.MinDistanceLod;

< //    const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);

< //    const float ParentMinDistanceLod = GetMinDistanceLod(ParentInfo, ParentHeight);

< //

< //    bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;

< //    bool bParentSubdivide = (ParentInfo.Level > 0 && ParentMinDistanceLod < ParentInfo.Level)

< //        || ThisInfo.Level == VHMParam.MaxLevel; // root node parent is always can subdivide

< //    bool bMerge = ((ThisInfo.Pos.x & 0x1) == 0) && ((ThisInfo.Pos.y & 0x1) == 0); // only one child node can generate merge node

< //

< //    if (IsValidThread)

< //    {

< //        if (bThisSubdivide) // this node subdivide

< //        {

< //            SubdivideQuadFlag[ThisThreadID+1] = 1;

< //        }

< //        else if (bParentSubdivide) // this node keep

< //        {

< //            FinalQuadFlag[ThisThreadID+1] = 1;

< //        }

< //        else if (bMerge) // generate parent node

< //        {

< //            MergeQuadFlag[ThisThreadID+1] = 1;

< //        }

< //    }

< //

< //    // need flag is complete set value in group

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    // alloc locate to fill data

< //    if (ThisThreadID == 0)

< //    {

< //        int i;

< //        [unroll]

< //        for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { SubdivideQuadFlag[i] += SubdivideQuadFlag[i-1];}

< //        [unroll]

< //        for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { MergeQuadFlag[i] += MergeQuadFlag[i-1];}

< //        [unroll]

< //        for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { FinalQuadFlag[i] += FinalQuadFlag[i-1];}

< //

< //        InterlockedAdd(OutDispatchArgsBuffer[s_SumQuadOffset], SubdivideQuadFlag[NumActiveGroupThread], SubdivideQuadBeginOffset);

< //        InterlockedAdd(OutDispatchArgsBuffer[VHMParam.MergeDispatchArgsOffset * s_DispatchArgsSize + s_SumQuadOffset],      MergeQuadFlag[NumActiveGroupThread],        MergeQuadBeginOffset);

< //        InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset],        FinalQuadFlag[NumActiveGroupThread],        FinalQuadBeginOffset);

< //    }

< //

< //    // wait alloc locate in group

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    // update dispatch data

< //    if (ThisThreadID == 0)

< //    {

< //        uint SubdivideDispatch  = (COLL_THREAD_TOTAL - 1 + SubdivideQuadFlag[NumActiveGroupThread]  + SubdivideQuadBeginOffset  ) / COLL_THREAD_TOTAL;

< //        uint MergeDispatch      = (COLL_THREAD_TOTAL - 1 + MergeQuadFlag[NumActiveGroupThread]      + MergeQuadBeginOffset      ) / COLL_THREAD_TOTAL;

< //        uint FinalDispatch      = (COLL_THREAD_TOTAL - 1 + FinalQuadFlag[NumActiveGroupThread]      + FinalQuadBeginOffset      ) / COLL_THREAD_TOTAL;

< //

< //        InterlockedMax(OutDispatchArgsBuffer[s_SumDispatchQuadOffset],  SubdivideDispatch);

< //        InterlockedMax(OutDispatchArgsBuffer[VHMParam.MergeDispatchArgsOffset * s_DispatchArgsSize + s_SumDispatchQuadOffset],      MergeDispatch);

< //        InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset],        FinalDispatch);

< //    }

< //

< //

< //    // fill data

< //    if (IsValidThread)

< //    {

< //        if (bThisSubdivide) // this node subdivide

< //        {

< //            uint Index = (SubdivideQuadBeginOffset + SubdivideQuadFlag[ThisThreadID]) & VHMParam.OutBufferSizeMask;

< //            OutSubdivideQuadBuffer[Index] = PackQuadItem2(ThisInfo);

< //        }

< //        else if (bParentSubdivide) // this node keep

< //        {

< //            uint Index = (FinalQuadBeginOffset + FinalQuadFlag[ThisThreadID]) & VHMParam.FinalQuadBufferSizeMask;

< //            FinalQuadBuffer[Index] = PackQuadItem2(ThisInfo);

< //        }

< //        else if (bMerge) // generate parent node

< //        {

< //            uint Index = (MergeQuadBeginOffset + MergeQuadFlag[ThisThreadID]) & VHMParam.OutBufferSizeMask;

< //            OutMergeQuadBuffer[Index] = PackQuadItem2(ParentInfo);

< //        }

< //    }

< // }

< //

517,692d390
<

< // CS Buffers

< // All Buffer had declared before.

<

< // group shared

< // All groupshared had declared before.

<

<

< // [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< // void CollectMergeQuadsCS(

< //    uint3 GroupThreadID : SV_GroupThreadID,

< //    uint3 DispatchThreadID : SV_DispatchThreadID

< // ) {

< //    const uint InArgsTime = CurPassCalTime / 2;

< //    const uint OutArgsTimes = (CurPassCalTime + 1) / 2;

< //    const uint InArgsOffset  = (VHMParam.MergeDispatchArgsOffset + InArgsTime) * s_DispatchArgsSize;

< //    const uint OutArgsOffset = (VHMParam.MergeDispatchArgsOffset + OutArgsTimes) * s_DispatchArgsSize;

< //    const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[InArgsOffset + s_SumQuadOffset];

< //    // if invalid, get 0 index in group thread

< //    const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;

< //

< //    // init

< //    if (ThisThreadID == 0)

< //    {

< //        NumActiveGroupThread = 0;

< //        FeedbackBeginOffset = 0;

< //        MergeQuadBeginOffset = 0;

< //        FinalQuadBeginOffset = 0;

< //        MergeQuadFlag[0] = 0;

< //        FinalQuadFlag[0] = 0;

< //    }

< //    FinalQuadFlag[ThisThreadID+1] = 0;

< //    MergeQuadFlag[ThisThreadID+1] = 0;

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    if (IsValidThread) {

< //        uint _Out;

< //        InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);

< //    }

< //    GroupMemoryBarrierWithGroupSync();

< //

< //

< //    // Get This Quad All Info

< //    const uint2 ThisPackData = InQuadBuffer[LoadIdx];

< //    SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

< //

< //    uint2 ParPackedValue;

< //    {

< //        ParPackedValue.x = MortonEncode(ThisInfo.Pos >> 1);

< //        uint ParLevel = min(ThisInfo.Level + 1, VHMParam.MaxLevel);

< //        ParPackedValue.x = ParPackedValue.x | (ParLevel << 24);

< //    }

< //    SQuadInfo ParentInfo = GetQuadInfo(ParPackedValue, VHMParam.RVTMinLevel);

< //

< //    // Record Feedback

< //    RecordFeedback(ParentInfo, ThisThreadID, IsValidThread);

< //

< //    // sample height texture

< //    const float ParentHeight = GetHeight(ParentInfo);

< //    const float ParentMinDistanceLod = GetMinDistanceLod(ParentInfo, ParentHeight);

< //

< //

< //    bool bParentSubdivide = (ParentInfo.Level > 0 && ParentMinDistanceLod < ParentInfo.Level)

< //        || ThisInfo.Level == VHMParam.MaxLevel; // root node parent is always can subdivide

< //    bool bMerge = ((ThisInfo.Pos.x & 0x1) == 0) && ((ThisInfo.Pos.y & 0x1) == 0); // only one child node can generate merge node

< //

< //

< //    if (IsValidThread)

< //    {

< //        if (bParentSubdivide) // this node keep

< //        {

< //            FinalQuadFlag[ThisThreadID+1] = 1;

< //        }

< //        else if (bMerge) // generate parent node

< //        {

< //            MergeQuadFlag[ThisThreadID+1] = 1;

< //        }

< //        // skip this node

< //        // else

< //    }

< //

< //    // need flag is complete set value in group

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    // alloc locate to fill data

< //    if (ThisThreadID == 0)

< //    {

< //        int i;

< //        [unroll]

< //        for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { MergeQuadFlag[i] += MergeQuadFlag[i-1];}

< //        [unroll]

< //        for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { FinalQuadFlag[i] += FinalQuadFlag[i-1];}

< //

< //        InterlockedAdd(OutDispatchArgsBuffer[OutArgsOffset + s_SumQuadOffset],      MergeQuadFlag[NumActiveGroupThread],        MergeQuadBeginOffset);

< //        InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset],                        FinalQuadFlag[NumActiveGroupThread],        FinalQuadBeginOffset);

< //    }

< //

< //

< //    // wait alloc locate in group

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    // update dispatch data

< //    if (ThisThreadID == 0)

< //    {

< //        uint MergeDispatch      = (COLL_THREAD_TOTAL - 1 + MergeQuadFlag[NumActiveGroupThread]      + MergeQuadBeginOffset      ) / COLL_THREAD_TOTAL;

< //        uint FinalDispatch      = (COLL_THREAD_TOTAL - 1 + FinalQuadFlag[NumActiveGroupThread]      + FinalQuadBeginOffset      ) / COLL_THREAD_TOTAL;

< //

< //        InterlockedMax(OutDispatchArgsBuffer[OutArgsOffset + s_SumDispatchQuadOffset],      MergeDispatch);

< //        InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset],        FinalDispatch);

< //    }

< //

< //

< //    // fill data

< //    if (IsValidThread)

< //    {

< //        if (bParentSubdivide) // this node keep

< //        {

< //            FinalQuadBuffer[(FinalQuadBeginOffset + FinalQuadFlag[ThisThreadID]) & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);

< //        }

< //        else if (bMerge) // generate parent node

< //        {

< //            OutMergeQuadBuffer[(MergeQuadBeginOffset + MergeQuadFlag[ThisThreadID]) & VHMParam.OutBufferSizeMask] = PackQuadItem2(ParentInfo);

< //        }

< //    }

< // }

< //

<

< // uint RemainCS_DispatchArgsOffset;

< // [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< // void CollectRemainQuadsCS(

< //    uint3 GroupThreadID : SV_GroupThreadID,

< //    uint3 DispatchThreadID : SV_DispatchThreadID

< // )

< // {

< //    const uint InArgsTime = CurPassCalTime / 2;

< //    const uint InArgsOffset = (RemainCS_DispatchArgsOffset + InArgsTime) * s_DispatchArgsSize;

< //    const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[InArgsOffset + s_SumQuadOffset];

< //    // if invalid, get 0 index in group thread

< //    const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;

< //

< //    // init

< //    if (ThisThreadID == 0)

< //    {

< //        NumActiveGroupThread = 0;

< //    }

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    if (IsValidThread){

< //        uint _Out;

< //        InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);

< //    }

< //

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    const uint2 ThisPackData = InQuadBuffer[LoadIdx];

< //    SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

< //    GetPhysicalAddress(ThisInfo);

< //

< //    // alloc locate to fill data

< //    if (ThisThreadID == 0)

< //    {

< //        InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset], NumActiveGroupThread, FinalQuadBeginOffset);

< //        uint NumDispatch = (FinalQuadBeginOffset + NumActiveGroupThread + COLL_THREAD_TOTAL - 1) / COLL_THREAD_TOTAL;

< //        uint _Out;

< //        InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset], NumDispatch, _Out);

< //    }

< //

< //    GroupMemoryBarrierWithGroupSync();

< //

< //    // fill data

< //    if (IsValidThread)

< //    {

< //        FinalQuadBuffer[(FinalQuadBeginOffset + ThisThreadID) & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);

< //    }

< // }

<

841a540,545
> #if VHM_ONE_PASS

>   uint Read;

>   InterlockedAdd(RWQueueInfo[0].NumActive, 1, Read);

>   InterlockedAdd(RWQueueInfo[0].Write, 1, Read);

>   OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;

> #else

843c547,681
< }---
> #endif

> }

>

>

>

> /**

>  * Compute shader to traverse the virtual texture page table and generate an array of items to potentially render for a view.

>  */

>

> groupshared uint NumGroupTasks;

> #if COMPILER_SUPPORTS_WAVE_SIZE

>   WAVESIZE(32)

> #endif

> [numthreads(COLL_THREAD_TOTAL, 1, 1)]

> void CollectQuadsOnePassCS(

>   uint3 GroupThreadID : SV_GroupThreadID,

>   uint3 DispatchThreadId : SV_DispatchThreadID)

> {

> #if VHM_ONE_PASS

>   // Persistant threads stay alive until the work queue is drained.

>   bool bExit = false;

>   while (!bExit)

>   {

>       // Sync and init group task count.

>       NumGroupTasks = 0;

>       GroupMemoryBarrierWithGroupSync();

>

>       // Try and pull a task.

>       int NumActive;

> #if VHM_END_WITH_ONE_STEP

>       if (RWQueueInfo[0].NumActive > VHMParam.NumActiveForOnePassStep)

>       {

>           NumActive = 0;

>           bExit = true;

>       }

>       else

> #endif

>       {

>           InterlockedAdd(RWQueueInfo[0].NumActive, -1, NumActive);

>       }

>

>       if (NumActive <= 0 && !bExit)

>       {

>           // No task pulled. Rewind.

>           InterlockedAdd(RWQueueInfo[0].NumActive, 1, NumActive);

>       }

>       else if (!bExit)

>       {

>           // Increment group task count for this loop.

>           uint Dummy;

>           InterlockedAdd(NumGroupTasks, 1, Dummy);

>

>           // Read item to process from queue.

>           uint Read;

>           InterlockedAdd(RWQueueInfo[0].Read, 1, Read);

>

>           const uint4 ThisPackedData = OutSubdivideQuadBuffer[Read & VHMParam.OutBufferSizeMask];

>           SQuadInfo ThisInfo = GetQuadInfo(ThisPackedData, VHMParam.RVTMinLevel);

>

> #if VHM_WITH_FEEDBACK

>           RecordFeedback(ThisInfo, ThisThreadID, true);

> #endif

>

>           // sample height texture

>           const float ThisHeight = GetHeight(ThisInfo);

>           const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);

>

>           bool bCull = false;

> #if VHM_WITH_CULL

>           bool bOpacity;

>           bCull = IsCullQuad(ThisInfo, bOpacity);

>           // if (!bCull)

>           // {

>           //  // Check if occluded.

>           //  bool bOcclude = !OcclusionTest(Pos, Level);

>           //  bCull = bOcclude;

>           // }

> #endif

>

>           const bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;

>

>           if (bCull)

>           {

>               // Store, but don't subdivide.

>               // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(0, 0, 1, 1));

>           }

>           else

>           {

>               if (bThisSubdivide)

>               {

>                   // Add children to queue.

>                   uint Write;

>                   InterlockedAdd(RWQueueInfo[0].Write, 4, Write);

>

>                   [unroll]

>                   for(int i = 0; i < 4; ++i)

>                   {

>                       uint2 ChildPos = ThisInfo.Pos * 2 + uint2(i & 0x1, (i>>1) & 0x1);

>                       uint ChildLevel = ThisInfo.Level - 1;

>                       uint4 ChildPackData;

>                       ChildPackData.x = PackQuadPosLevel(ChildPos, ChildLevel);

>                       ChildPackData.y = 0;

>                       ChildPackData.z = ThisInfo.PhysicalAddress.x;

>                       ChildPackData.w = ThisInfo.PhysicalAddress.y;

>                       OutSubdivideQuadBuffer[(Write + i) & VHMParam.OutBufferSizeMask] = ChildPackData;

>                   }

>

>                   InterlockedAdd(RWQueueInfo[0].NumActive, 4, NumActive);

>               }

>               else

>               {

>                   uint Write;

>                   InterlockedAdd(FinalDispatchArgsBuffer[3], 1, Write);

>                   InterlockedMax(FinalDispatchArgsBuffer[0], ((Write + 1) + COLL_THREAD_TOTAL - 1) / COLL_THREAD_TOTAL);

>

>                   FinalQuadBuffer[Write & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);

>

>                   // Debug draw the bounds.

>                   // if (!bCull)

>                   {

>                       // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(1, 0, 0, 1));

>                   }

>               }

>           }

>       }

>

>       // Exit if no work was found.

>       DeviceMemoryBarrier();

>       if (NumGroupTasks == 0)

>       {

>           bExit = true;

>       }

>   }

> #endif

> }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#26 (unicode) ====

88c88
<   1,
---
>   16,
129a130,145
> static int32 GVHMWithOnePass = 0;
> static FAutoConsoleVariableRef CVarVHMWithOnePass(
>   TEXT("r.VHM.WithOnePass"),
>   GVHMWithOnePass,
>   TEXT(""),
>   ECVF_RenderThreadSafe
> );
>
> static int32 GVHMNumActiveForOnePassStep = 640;
> static FAutoConsoleVariableRef CVarVHMNumActiveForOnePassStep(
>   TEXT("r.VHM.NumActiveForOnePassStep"),
>   GVHMNumActiveForOnePassStep,
>   TEXT(""),
>   ECVF_RenderThreadSafe
> );
>
370d385
<
392a408
>           SHADER_PARAMETER(int32,             NumActiveForOnePassStep)
403c419
<           TArray<FRDGBufferRef, TFixedAllocator<2>> MergeQuadBuffer{nullptr, nullptr};
---
>           FRDGBufferRef RWQueueInfoBuffer {nullptr};
422c438
<           TArray<FSRVAndUAV, TFixedAllocator<2>> MergeViews{{}, {}};
---
>           FSRVAndUAV RWQueueInfoViews{};
1355a1372
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)
1378a1396,1398
>
>           class FWithOnePassDim : SHADER_PERMUTATION_BOOL("VHM_ONE_PASS");
>           using FPermutationDomain = TShaderPermutationDomain<FWithOnePassDim>;
1381a1402
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)
1446a1468,1497
>
>       class FCollectionQuadsOnePass_CS : public FGlobalShader
>       {
>           DECLARE_GLOBAL_SHADER(FCollectionQuadsOnePass_CS);
>           SHADER_USE_PARAMETER_STRUCT(FCollectionQuadsOnePass_CS, FGlobalShader);
>
>           class FWithCullDim : SHADER_PERMUTATION_BOOL("VHM_WITH_CULL");
>           class FWithFeedback : SHADER_PERMUTATION_BOOL("VHM_WITH_FEEDBACK");
>           class FWithOnePassDim : SHADER_PERMUTATION_BOOL("VHM_ONE_PASS");
>           class FWithEndWithOneStepDim : SHADER_PERMUTATION_BOOL("VHM_END_WITH_ONE_STEP");
>           using FPermutationDomain = TShaderPermutationDomain<FWithCullDim, FWithFeedback, FWithOnePassDim, FWithEndWithOneStepDim>;
>
>           BEGIN_SHADER_PARAMETER_STRUCT(FParameters,)
>               SHADER_PARAMETER_RDG_UNIFORM_BUFFER(FVHMCSSharedParameters, VHMParam)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWStructuredBuffer<WorkerQueueInfo>, RWQueueInfo)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     OutDispatchArgsBuffer)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    OutSubdivideQuadBuffer)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     RWFeedbackBuffer)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint>,     FinalDispatchArgsBuffer)
>               SHADER_PARAMETER_RDG_BUFFER_UAV(RWBuffer<uint2>,    FinalQuadBuffer)
>               SHADER_PARAMETER_TEXTURE(Texture2D<uint>, PageTableTexture)
>               SHADER_PARAMETER_SRV(Texture2D<float>, HeightTexture)
>               SHADER_PARAMETER_TEXTURE(Texture2D<float>, MaskTexture)
>               SHADER_PARAMETER_SAMPLER(SamplerState, PointSampler)
>               SHADER_PARAMETER_TEXTURE(Texture2D, HeightMinMaxTexture)
>           END_SHADER_PARAMETER_STRUCT()
>
>       };
>       IMPLEMENT_GLOBAL_SHADER(FCollectionQuadsOnePass_CS, "/Plugin/VirtualHeightfieldMesh/Private/VirtualHeightfieldMesh3.usf", "CollectQuadsOnePassCS", SF_Compute);
>
1770,1775d1820
<           const TCHAR* MergeNames[2] = {
<               TEXT("VHM.MergeBuffer_0"),
<               TEXT("VHM.MergeBuffer_1")};
<           const TCHAR* MergeArgsNames[2] = {
<               TEXT("VHM.MergeArgsBuffer_0"),
<               TEXT("VHM.MergeArgsBuffer_1")};
1784,1788d1828
<               OutResources.MergeQuadBuffer[i] = GraphBuilder.CreateBuffer(
<                   FRDGBufferDesc::CreateBufferDesc(sizeof(uint32) * 4, MaxRenderItems),
<                   MergeNames[i]);
<               OutResources.MergeViews[i].Create(GraphBuilder, OutResources.MergeQuadBuffer[i]);
<
1798d1837
<
1799a1839,1842
>           OutResources.RWQueueInfoBuffer = GraphBuilder.CreateBuffer(
>               FRDGBufferDesc::CreateStructuredDesc(sizeof(WorkerQueueInfo), 1),
>               TEXT("VirtualHeightfieldMesh.QueueInfo"));
>           OutResources.RWQueueInfoViews.Create(GraphBuilder, OutResources.RWQueueInfoBuffer);
1900c1943
<
---
>
2045c2088,2089
<           Parameters->VHMParam =InVolatileBuffers.VHMParameterUBuffer;
---
>           Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;
>           Parameters->RWQueueInfo = InVolatileBuffers.RWQueueInfoViews.UAV;
2072,2073c2116,2119
<           Parameters->OutDispatchArgsBuffer   = InVolatileBuffers.ArgsViews[0].UAV;
<           Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[0].UAV;
---
>           Parameters->RWQueueInfo = InVolatileBuffers.RWQueueInfoViews.UAV;
>           int32 OutIndex = GVHMWithOnePass ? 1 : 0;
>           Parameters->OutDispatchArgsBuffer   = InVolatileBuffers.ArgsViews[OutIndex].UAV;
>           Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[OutIndex].UAV;
2076c2122,2124
<           TShaderMapRef<FFillLevel4Quad_CS> ComputeShader(InGlobalShaderMap);
---
>           FFillLevel4Quad_CS::FPermutationDomain PermutationVector;
>           PermutationVector.Set<FFillLevel4Quad_CS::FWithOnePassDim>(GVHMWithOnePass);
>           TShaderMapRef<FFillLevel4Quad_CS> ComputeShader(InGlobalShaderMap, PermutationVector);
2164a2213,2250
>
>       void AddPass_CollectQuads_CS(FRDGBuilder& GraphBuilder, FGlobalShaderMap* InGlobalShaderMap,
>           FInnerBuffers& InBuffers, FVolatileBuffers& InVolatileBuffers, FRDGBufferUAVRef VTFeedbackBufferUAV,
>           bool WithCull, bool bEndWithOneStep, bool WithFeedback=true)
>       {
>           auto Parameters = GraphBuilder.AllocParameters<FCollectionQuadsOnePass_CS::FParameters>();
>
>           Parameters->VHMParam = InVolatileBuffers.VHMParameterUBuffer;
>           Parameters->RWQueueInfo = InVolatileBuffers.RWQueueInfoViews.UAV;
>           Parameters->OutDispatchArgsBuffer = InVolatileBuffers.ArgsViews[1].UAV;
>           Parameters->OutSubdivideQuadBuffer = InVolatileBuffers.SubdivideViews[1].UAV;
>           Parameters->RWFeedbackBuffer = VTFeedbackBufferUAV;
>
>           Parameters->FinalDispatchArgsBuffer = InVolatileBuffers.FinalQuadArgsViews.UAV;
>           Parameters->FinalQuadBuffer = InVolatileBuffers.FinalQuadViews.UAV;
>
>           Parameters->PageTableTexture = InVolatileBuffers.PageTableTexture;
>           Parameters->HeightTexture = InVolatileBuffers.HeightTexture;
>           Parameters->MaskTexture = InVolatileBuffers.MaskTexture;
>           Parameters->PointSampler = TStaticSamplerState<SF_Point>::GetRHI();
>           Parameters->HeightMinMaxTexture = InVolatileBuffers.HeightMinMaxTexture;
>
>
>           FCollectionQuadsOnePass_CS::FPermutationDomain PermutationVector;
>           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithCullDim>(WithCull);
>           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithFeedback>(WithFeedback);
>           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithOnePassDim>(GVHMWithOnePass);
>           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithEndWithOneStepDim>(bEndWithOneStep);
>
>           TShaderMapRef<FCollectionQuadsOnePass_CS> ComputeShader(InGlobalShaderMap, PermutationVector);
>
>           FComputeShaderUtils::AddPass(
>               GraphBuilder,
>               RDG_EVENT_NAME("CollectQuads_OnePass"),
>               ComputeShader, Parameters,
>               FIntVector(CVarVHMCollectPassWavefronts.GetValueOnRenderThread(), 1, 1)
>           );
>       }
2584a2671
>       Param.NumActiveForOnePassStep = GVHMNumActiveForOnePassStep;
2757,2761c2844,2858
<
<       const int32 MaxCalTime = VolatileBuffers.VHMParameter->MaxLevel - 3 + 1; // pre cal 4
<       // Subdivide
<       bool EnableCull = !CVarVHMDisableCull->GetInt() && true /*defautl need cull*/;
<       for (int32 CalTime = 0; CalTime < MaxCalTime; CalTime++)
---
>
>       if (!GVHMWithOnePass)
>       {
>           const int32 MaxCalTime = VolatileBuffers.VHMParameter->MaxLevel - 3 + 1; // pre cal 4
>           // Subdivide
>           bool EnableCull = !CVarVHMDisableCull->GetInt() && true /*defautl need cull*/;
>           for (int32 CalTime = 0; CalTime < MaxCalTime; CalTime++)
>           {
>               // bool WithFeedback = (MaxCalTime - CalTime) > (int32)VolatileBuffers.VHMParameter->RVTMinLevel;
>               bool WithFeedback = true;
>               VirtualHeightfieldMesh::V2::AddPass_CollectSubdivideQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
>                   VolatileBuffers, VTFeedbackBufUAV, CalTime, EnableCull, WithFeedback);
>           }
>       }
>       else
2763c2860
<           // bool WithFeedback = (MaxCalTime - CalTime) > (int32)VolatileBuffers.VHMParameter->RVTMinLevel;
---
>           bool EnableCull = !CVarVHMDisableCull->GetInt();
2765,2766c2862,2865
<           VirtualHeightfieldMesh::V2::AddPass_CollectSubdivideQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
<               VolatileBuffers, VTFeedbackBufUAV, CalTime, EnableCull, WithFeedback);
---
>           VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
>               VolatileBuffers, VTFeedbackBufUAV, EnableCull, true, WithFeedback);
>           VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
>               VolatileBuffers, VTFeedbackBufUAV, EnableCull, false, WithFeedback);
```

#### CL 531156 — 2025/09/02 — 郭智均

- **提交说明**：--story=1065526 --user=郭智均 【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3017647 #review-531144 #PrecheckSuccess
- **TAPD**：story=1065526
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3017647 #review-531144 #PrecheckSuccess

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)

📄 查看 VHM 相关 diff（CL 531156）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#9 (text) ====

576,583d575
< #if VHM_END_WITH_ONE_STEP

<       if (RWQueueInfo[0].NumActive > VHMParam.NumActiveForOnePassStep)

<       {

<           NumActive = 0;

<           bExit = true;

<       }

<       else

< #endif

675c667,671
<       if (NumGroupTasks == 0)

---
>       if (NumGroupTasks == 0

> #if VHM_END_WITH_ONE_STEP

>           || NumActive > VHMParam.NumActiveForOnePassStep

> #endif

>       )

676a673
>
```

#### CL 532031 — 2025/09/02 — 郭智均

- **提交说明**：--story=1065526 --user=郭智均 【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3024176 #review-532007 #PrecheckSuccess
- **TAPD**：story=1065526
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【GPU】VHM提供单Pass遍历树的功能 https://www.tapd.cn/68880148/s/3024176 #review-532007 #PrecheckSuccess

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)

📄 查看 VHM 相关 diff（CL 532031）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#10 (text) ====

556a557
> groupshared uint NumGroupExitRequest;

571a573
>       NumGroupExitRequest = 0;

666a669,673
>       if (NumActive > VHMParam.NumActiveForOnePassStep)

>       {

>           uint Dummy;

>           InterlockedAdd(NumGroupExitRequest, 1, Dummy);

>       }

669c676
<           || NumActive > VHMParam.NumActiveForOnePassStep

---
>           || NumGroupExitRequest > 0
```

#### CL 539523 — 2025/09/08 — 郭智均

- **提交说明**：--story=1066380 --user=郭智均 【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539509 #PrecheckSuccess
- **TAPD**：story=1066380
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539509 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 539523）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#27 (unicode) ====

137a138,142
> static bool GetVHMWithOnePass()
> {
>   return GVHMWithOnePass && GMaxRHIFeatureLevel >= ERHIFeatureLevel::SM5;
> }
>
1494a1500,1503
>           static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)
>           {
>               return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM6);
>           }
2117c2126
<           int32 OutIndex = GVHMWithOnePass ? 1 : 0;
---
>           int32 OutIndex = GetVHMWithOnePass() ? 1 : 0;
2123c2132
<           PermutationVector.Set<FFillLevel4Quad_CS::FWithOnePassDim>(GVHMWithOnePass);
---
>           PermutationVector.Set<FFillLevel4Quad_CS::FWithOnePassDim>(GetVHMWithOnePass());
2239c2248
<           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithOnePassDim>(GVHMWithOnePass);
---
>           PermutationVector.Set<FCollectionQuadsOnePass_CS::FWithOnePassDim>(GetVHMWithOnePass());
2845c2854
<       if (!GVHMWithOnePass)
---
>       if (!GetVHMWithOnePass())
```

#### CL 539702 — 2025/09/08 — 郭智均

- **提交说明**：--story=1066380 --user=郭智均 【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539689 #PrecheckSuccess
- **TAPD**：story=1066380
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539689 #PrecheckSuccess

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 539702）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#11 (text) ====

556a557
> #if VHM_END_WITH_ONE_STEP

557a559
> #endif

666a669
> #if VHM_END_WITH_ONE_STEP

668d670
<       DeviceMemoryBarrier();

673a676,678
> #endif

>

>       DeviceMemoryBarrier();


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#28 (unicode) ====

140c140
<   return GVHMWithOnePass && GMaxRHIFeatureLevel >= ERHIFeatureLevel::SM5;
---
>   return GVHMWithOnePass ;/*&& GMaxRHIFeatureLevel >= ERHIFeatureLevel::SM5;*/
```

#### CL 539878 — 2025/09/08 — 郭智均

- **提交说明**：--story=1066380 --user=郭智均 【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539874 #PrecheckSuccess
- **TAPD**：story=1066380
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【GPU】VHM OnePass仅在Sm6下启用 https://www.tapd.cn/68880148/s/3059179 #review-539874 #PrecheckSuccess

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)

📄 查看 VHM 相关 diff（CL 539878）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#12 (text) ====

574a575
> #if VHM_END_WITH_ONE_STEP

575a577
> #endif
```

#### CL 575365 — 2025/09/26 — 陈永昊

- **提交说明**：--story=1067894 --user=陈永昊 【前台性能】【EA】超高配置笔记本上worldpartition的一个调用有0.4ms~1ms的耗时 https://www.tapd.cn/68880148/s/3200705 #PrecheckSuccess
- **TAPD**：story=1067894
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【前台性能】【EA】超高配置笔记本上worldpartition的一个调用有0.4ms~1ms的耗时 https://www.tapd.cn/68880148/s/3200705 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 575365）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#19 (unicode) ====

15d14
< #if UE_EDITOR
19,27c18
<       0,  // shiyu: now we need to open it by console
<       TEXT("Enable virtual heightfield mesh"),
<       ECVF_RenderThreadSafe
<   );
< #else
<   /** CVar to toggle support for virtual heightfield mesh. */
<   static TAutoConsoleVariable<int32> CVarVHMEnable(
<       TEXT("r.VHM.Enable"),
<       1,  // shiyu: now we need to open it by console
---
>       0,
31d21
< #endif
40,41d29
<
<
48c36
<       const IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
---
>       static IConsoleVariable* EnableNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Nanite"));
50c38
<       const IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
---
>       static IConsoleVariable* LandscapeNaniteCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("landscape.RenderNanite"));
```

#### CL 583471 — 2025/10/11 — 郭智均

- **提交说明**：--story=1052866 --user=郭智均 VHM性能优化 - 【性能任务】 https://www.tapd.cn/68880148/s/3235174 - 中台对VHM模块的优化 #review-583457 #PrecheckSuccess
- **TAPD**：story=1052866
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：VHM性能优化 - 【性能任务】 https://www.tapd.cn/68880148/s/3235174 - 中台对VHM模块的优化 #review-583457 #PrecheckSuccess

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMesh3.usf` (edit)
- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 583471）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMesh3.usf#13 (text) ====

1,692c1,776
<

< #include "/Engine/Private/Common.ush"

< #include "/Engine/Private/MortonCode.ush"

< #include "VirtualHeightfieldMesh.ush"

<

< // Constants

< #define COLL_THREAD_TOTAL 32

< #define ThisThreadID GroupThreadID.x

< #define s_SumQuadOffset 3

< #define s_SumDispatchQuadOffset 0

< #define s_DispatchArgsSize 4

< #define s_IndirectDrawOffset 1

< #define s_ClipMask 0.333f

<

< #ifndef VHM_WITH_FEEDBACK

< #define VHM_WITH_FEEDBACK 1

< #endif

<

< #ifndef VHM_STAT

< #define VHM_STAT 1

< #endif

<

< #ifndef VHM_ONE_PASS

< #define VHM_ONE_PASS 0

< #endif

<

<

< struct QuadItem2

< {

<   uint2 Pos;

<   uint Level;

<   uint3 PhysicalAddress;

< };

<

< struct SQuadInfo

< {

<   uint2 Pos;

<   int Level;

<   uint2 TexPos;

<   uint TextureLevel;

<   uint GeoToTexLevelOffset;

<   float GeoToTexLevelOffsetInv;

<   uint3 PhysicalAddress;

<   uint SampleTextureLevel;

<   uint2 SampleTexPos;

<   float SampleGeoToTexLevelOffsetInv;

< };

<

< uint ConvertHeight01ToUInt16(float Height)

< {

<   return uint(Height * 65536.0f);

< }

<

< float ConvertUInt16ToHeight01(uint Val)

< {

<   return (Val & 0xffff) / 65536.0f;

< }

<

< QuadItem2 UnPackQuadItem2(uint4 PackedVal)

< {

<   QuadItem2 Item;

<   Item.Pos = MortonDecode(PackedVal.x & 0xfffffff);

<   Item.Level = PackedVal.x >> 28;

<   Item.PhysicalAddress = PackedVal.yzw;

<   return Item;

< }

<

< uint PackQuadPosLevel(uint2 Pos, uint Level)

< {

<   return MortonEncode(Pos) | (Level << 28);

< }

<

< uint4 PackQuadItem2(in SQuadInfo Info)

< {

<   uint4 Result;

<   Result.yzw = Info.PhysicalAddress;

<   Result.x = PackQuadPosLevel(Info.Pos, Info.Level);

<

<   return Result;

< }

<

< SQuadInfo GetQuadInfo(uint4 PackedVal, uint InRVTMinLevel)

< {

<   QuadItem2 Item = UnPackQuadItem2(PackedVal);

<

<   const int TmpLevel = max(int(Item.Level) - VHMParam.ExtSubdivisionLevel, 0);

<   const uint GeoToTexLevelOffset = max(int(InRVTMinLevel) - TmpLevel, 0) + max(0, VHMParam.ExtSubdivisionLevel - (int)Item.Level); // geometry levels is large than tex levels

<   const float GeoToTexLevelOffsetInv = 1.f / float(1u << GeoToTexLevelOffset);

<

<   const uint TextureLevel = max(TmpLevel - int(InRVTMinLevel), 0);

<   uint2 TexPos = Item.Pos >> GeoToTexLevelOffset;

<

<   SQuadInfo Info;

<   Info.Pos = Item.Pos;

<   Info.Level = Item.Level;

<   Info.PhysicalAddress = Item.PhysicalAddress;

<

<   Info.GeoToTexLevelOffset = GeoToTexLevelOffset;

<   Info.GeoToTexLevelOffsetInv = GeoToTexLevelOffsetInv;

<   Info.TextureLevel = TextureLevel;

<   Info.TexPos = TexPos;

<   Info.SampleTextureLevel = max(0, int(min(Item.Level, VHMParam.MaxLevel - InRVTMinLevel)) - VHMParam.ExtSubdivisionLevel);

<

<   const uint SampleGeoToTexLevelOffset = min(InRVTMinLevel, VHMParam.MaxLevel - Item.Level) + max(0, (int)VHMParam.ExtSubdivisionLevel - (int)Item.Level);

<   Info.SampleTexPos = Item.Pos >> SampleGeoToTexLevelOffset;

<

<   Info.SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << SampleGeoToTexLevelOffset);

<

<   return Info;

< }

<

< float GetMinDistanceLod(SQuadInfo Info, float Height)

< {

<   // Get UV bounding box

<   float2 Scale = (float)(1u << Info.TextureLevel) * VHMParam.PageTableSize.zw;

<   float2 UV0 = ((float2)Info.Pos + float2(0, 0)) * Info.GeoToTexLevelOffsetInv * Scale;

<   float2 UV1 = ((float2)Info.Pos + float2(1, 1)) * Info.GeoToTexLevelOffsetInv * Scale;

<

<   float MinDistanceSq = SquaredDistance((float3(UV0, Height) - VHMParam.ViewOrigin) * VHMParam.UVToWorldScale);

<   [unroll]

<   for (int k = 1; k < 9; ++k) {

<       const int i = k / 3;

<       const int j = k % 3;

<

<       const float2 Lerp = float2(i * 0.5f, j * 0.5f);

<       const float2 UV = Lerp * UV0 + (1 - Lerp) * UV1;

<       const float DistanceSq = SquaredDistance((float3(UV, Height) - VHMParam.ViewOrigin) * VHMParam.UVToWorldScale);

<       MinDistanceSq = min(MinDistanceSq, DistanceSq);

<   }

<   const float MinDistanceLod = CalculateDistanceLod(MinDistanceSq, VHMParam.LodDistances);

<

<   return MinDistanceLod;

< }

<

<

< #if VHM_ONE_PASS

< RWStructuredBuffer<WorkerQueueInfo> RWQueueInfo;

< #endif

< // CS Buffers

< Buffer<uint> InDispatchArgsBuffer;

< Buffer<uint4> InQuadBuffer;

< RWBuffer<uint> OutDispatchArgsBuffer;

< #if !VHM_ONE_PASS

< RWBuffer<uint4> OutSubdivideQuadBuffer;

< #else

< RWCoherentBuffer(uint4) OutSubdivideQuadBuffer;

< #endif

< RWBuffer<uint> RWFeedbackBuffer;

< RWBuffer<uint> FinalDispatchArgsBuffer;

< RWBuffer<uint4> FinalQuadBuffer;

< // - for cull pass

< RWBuffer<uint> InstanceArgsBuffer;

< RWStructuredBuffer<QuadRenderInstance> QuadInstanceBuffer;

< RWStructuredBuffer<QuadRenderInstance> HoleQuadInstanceBuffer;

<

< // #define VHM_STAT 1

< #if VHM_STAT

< static const uint sMaxLodLevel = 15;

< static const uint sAfterCullOffset = 16;

< RWBuffer<uint> RWStatBuffer;

< #endif

<

< //Texture

< Texture2D<uint> PageTableTexture;

< Texture2D<float> HeightTexture;

< Texture2D<float> MaskTexture;

< SamplerState PointSampler;

< Texture2D<float4> HeightMinMaxTexture;

<

<

< void GetPhysicalAddress(inout SQuadInfo Info)

< {

<   const float PhysicalAddress = PageTableTexture.Load(int3(Info.SampleTexPos, Info.SampleTextureLevel));

<   Info.PhysicalAddress.x = PhysicalAddress;

< }

<

< float GetHeight(inout SQuadInfo Info)

< {

<   GetPhysicalAddress(Info);

<   const float3 UVTransform = GetVirtualToPhysicalUVTransform(Info.Pos,

<       // Info.GeoToTexLevelOffsetInv, Info.TextureLevel,

<       Info.SampleGeoToTexLevelOffsetInv, Info.SampleTextureLevel,

<       Info.PhysicalAddress[0], VHMParam.PhysicalPageTransform, VHMParam.NumPhysicalAddressBits);

<

<   // Sample height once to approximate distance.

<   const float2 LocalPhysicalUV = UVTransform.xy + float2(0.5, 0.5) * UVTransform.z;

<   const float Height = HeightTexture.SampleLevel(PointSampler, LocalPhysicalUV, 0);

<   return Height;

< }

<

<

<

< // group shared

< groupshared uint NumActiveGroupThread;

< groupshared uint FeedbackBeginOffset;

< groupshared uint SubdivideQuadFlag[COLL_THREAD_TOTAL+1];

< groupshared uint SubdivideQuadBeginOffset;

< groupshared uint FinalQuadFlag[COLL_THREAD_TOTAL+1];

< groupshared uint FinalQuadBeginOffset;

<

<

< void RecordFeedback(in SQuadInfo ThisInfo, uint CurThreadID, bool IsValid)

< {

<   // const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.

<   const uint MultiWriteCount = 1;

<   // this is optimize, one atomic_add per group

<   if (CurThreadID == 0) { InterlockedAdd(RWFeedbackBuffer[0], NumActiveGroupThread * MultiWriteCount, FeedbackBeginOffset); }

<   GroupMemoryBarrierWithGroupSync();

<

<   if (IsValid)

<   {

<       uint FeedbackPos = FeedbackBeginOffset + (CurThreadID + 1) * MultiWriteCount;

<       uint LevelPlusOne = ThisInfo.SampleTextureLevel + 1;

<       // PageTableFeedbackId is 4bit data, this value had shift to [28,32). fuck...

<       uint PackData = ThisInfo.SampleTexPos.x | (ThisInfo.SampleTexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;

<       for (int i = 0; i < MultiWriteCount; ++i)

<       {

<           RWFeedbackBuffer[FeedbackPos + i] = PackData;

<       }

<   }

< }

<

< float GetMaskValue(in SQuadInfo ThisInfo)

< {

<   float MaskValue = MaskTexture.Load(int3(ThisInfo.TexPos, ThisInfo.TextureLevel));

<   return MaskValue;

< }

<

< bool IsOpacity(float MaskValue)

< {

<   return abs(MaskValue - 1.0f) < 1e-3;

< }

<

< bool IsCullQuad(in SQuadInfo ThisInfo, out bool bOpacity)

< {

<   // Check Occlude

<   const bool bOccludeCull = false; // todo

<

<   // Check Mask

<   const float CLIP_MASK = 0.333f;

<   const float MaskValue = GetMaskValue(ThisInfo);

<   bOpacity = IsOpacity(MaskValue);

<   const bool bMaskCull = MaskValue < CLIP_MASK; // && false; // ignore MaskCull

<

<   // Check Frustum

<   const float2 Scale = (float)(1u << ThisInfo.TextureLevel) * VHMParam.PageTableSize.zw;

<   const float2 UV0 = ((float2)ThisInfo.Pos + float2(0, 0)) * ThisInfo.GeoToTexLevelOffsetInv * Scale;

<   const float2 UV1 = ((float2)ThisInfo.Pos + float2(1, 1)) * ThisInfo.GeoToTexLevelOffsetInv * Scale;

<   // const float Height = GetHeight(ThisInfo);

<   // const float3 UVMin = float3(UV0, Height);

<   // const float3 UVMax = float3(UV1, Height);

<   float2 MinMaxHeight = UnPackMinMaxHeight(HeightMinMaxTexture.SampleLevel(PointSampler, UV0, ThisInfo.TextureLevel));

<   const float3 UVMin = float3(UV0, MinMaxHeight.x);

<   const float3 UVMax = float3(UV1, MinMaxHeight.y);

<   const float3 UVCenter = (UVMin + UVMax) * 0.5;

<   const float3 UVExtern = UVMax - UVMin;

<   const bool bFrustumCull = !PlaneTestAABB(VHMParam.FrustumPlanes, UVCenter, UVExtern);

<

<   bool bCull = bFrustumCull || bMaskCull || bOccludeCull;

<

<   return bCull;

< }

<

<

<

< // Pass Uniform

< uint CurPassCalTime;

<

<

< [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< void CollectSubdivideQuadsCS(

<   uint3 GroupThreadID : SV_GroupThreadID,

<   uint3 DispatchThreadID : SV_DispatchThreadID

< ) {

<   const uint InArgsTime = CurPassCalTime / 2;

<   const uint OutArgsTimes = (CurPassCalTime + 1) / 2;

<   const uint InArgsOffset = InArgsTime * s_DispatchArgsSize;

<   const uint OutArgsOffset = OutArgsTimes * s_DispatchArgsSize;

<   const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[InArgsOffset + s_SumQuadOffset];

<   // if invalid, get 0 index in group thread

<   const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;

<

<   // init

<   if (ThisThreadID == 0)

<   {

<       NumActiveGroupThread = 0;

<       FeedbackBeginOffset = 0;

<       SubdivideQuadBeginOffset = 0;

<       FinalQuadBeginOffset = 0;

<       SubdivideQuadFlag[0] = 0;

<       FinalQuadFlag[0] = 0;

<   }

<   FinalQuadFlag[ThisThreadID+1] = 0;

<   SubdivideQuadFlag[ThisThreadID+1] = 0;

<   GroupMemoryBarrierWithGroupSync();

<

<   if (IsValidThread)

<   {

<       uint _Out;

<       InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);

<   }

<   GroupMemoryBarrierWithGroupSync();

<

<

<   // Get This Quad All Info

<   const uint4 ThisPackData = InQuadBuffer[LoadIdx];

<   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

<

< #if VHM_WITH_FEEDBACK

<   // Record Feedback

<   RecordFeedback(ThisInfo, ThisThreadID, IsValidThread);

< #endif

<

<   // sample height texture

<   const float ThisHeight = GetHeight(ThisInfo);

<   const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);

<

<   bool bCull = false;

< #if VHM_WITH_CULL

<   bool bOpacity;

<   bCull = IsCullQuad(ThisInfo, bOpacity);

< #endif

<

<

<   const bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;

<   if (IsValidThread && !bCull)

<   {

<       if (bThisSubdivide)

<       {

<           SubdivideQuadFlag[ThisThreadID+1] = 4;

<       }

<       else

<       {

<           FinalQuadFlag[ThisThreadID+1] = 1;

<       }

<   }

<   GroupMemoryBarrierWithGroupSync();

<

<

<   // alloc locate to fill data

<   if (ThisThreadID == 0)

<   {

<       int i;

<       [unroll]

<       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { SubdivideQuadFlag[i] += SubdivideQuadFlag[i-1];}

<       [unroll]

<       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { FinalQuadFlag[i] += FinalQuadFlag[i-1];}

<

<       InterlockedAdd(OutDispatchArgsBuffer[OutArgsOffset + s_SumQuadOffset], SubdivideQuadFlag[NumActiveGroupThread], SubdivideQuadBeginOffset);

<       InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset],                        FinalQuadFlag[NumActiveGroupThread],        FinalQuadBeginOffset);

<   }

<

<   // wait alloc locate in group

<   GroupMemoryBarrierWithGroupSync();

<

<   // update dispatch data

<   if (ThisThreadID == 0)

<   {

<       uint SubdivideDispatch  = (COLL_THREAD_TOTAL - 1 + SubdivideQuadFlag[NumActiveGroupThread]  + SubdivideQuadBeginOffset  ) / COLL_THREAD_TOTAL;

<       uint FinalDispatch      = (COLL_THREAD_TOTAL - 1 + FinalQuadFlag[NumActiveGroupThread]      + FinalQuadBeginOffset      ) / COLL_THREAD_TOTAL;

<

<       InterlockedMax(OutDispatchArgsBuffer[OutArgsOffset + s_SumDispatchQuadOffset],  SubdivideDispatch);

<       InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset],        FinalDispatch);

<   }

<

<   // fill data

<   if (IsValidThread && !bCull)

<   {

<       if (bThisSubdivide) // this node subdivide

<       {

<           [unroll]

<           for(int i = 0; i < 4; ++i)

<           {

<               uint2 ChildPos = ThisInfo.Pos * 2 + uint2(i & 0x1, (i>>1) & 0x1);

<               uint ChildLevel = ThisInfo.Level - 1;

<               uint4 ChildPackData;

<               ChildPackData.x = PackQuadPosLevel(ChildPos, ChildLevel);

<               ChildPackData.y = 0;

<               ChildPackData.z = ThisInfo.PhysicalAddress.x;

<               ChildPackData.w = ThisInfo.PhysicalAddress.y;

<               OutSubdivideQuadBuffer[(SubdivideQuadBeginOffset + SubdivideQuadFlag[ThisThreadID] + i) & VHMParam.OutBufferSizeMask] = ChildPackData;

<           }

<       }

<       else

<       {

<           FinalQuadBuffer[(FinalQuadBeginOffset + FinalQuadFlag[ThisThreadID]) & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);

<       }

<   }

< }

<

< groupshared uint QuadInstanceFlag[COLL_THREAD_TOTAL+1];

< groupshared uint QuadInstanceOffset;

< groupshared uint HoleQuadInstanceFlag[COLL_THREAD_TOTAL+1];

< groupshared uint HoleQuadInstanceOffset;

<

< [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< void CullQuadsAndGenerateInstancesCS(

<   uint3 GroupThreadID : SV_GroupThreadID,

<   uint3 DispatchThreadID : SV_DispatchThreadID

< )

< {

<   const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[s_SumQuadOffset];

<   // if invalid, get 0 index in group thread

<   const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;

<

<   // init

<   if (ThisThreadID == 0)

<   {

<       NumActiveGroupThread = 0;

<       QuadInstanceFlag[0] = 0;

<       QuadInstanceOffset = 0;

<       HoleQuadInstanceFlag[0] = 0;

<       HoleQuadInstanceOffset = 0;

<   }

<   QuadInstanceFlag[ThisThreadID+1] = 0;

<   HoleQuadInstanceFlag[ThisThreadID+1] = 0;

<   GroupMemoryBarrierWithGroupSync();

<

<   if (IsValidThread) {

<       uint _Out;

<       InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);

<   }

<   GroupMemoryBarrierWithGroupSync();

<

<

<   // Get This Quad All Info

<   const uint4 ThisPackData = InQuadBuffer[LoadIdx];

<   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

<   // const float PhysicalAddress = PageTableTexture.Load(int3(ThisInfo.TexPos, ThisInfo.TextureLevel));

<   // ThisInfo.PhysicalAddress = PhysicalAddress;

<

<   bool bCull = false;

<   bool bOpacity = false;

< #if VHM_WITH_CULL

<   bCull = IsCullQuad(ThisInfo, bOpacity);

< #else

<   bOpacity = IsOpacity(GetMaskValue(ThisInfo));

< #endif

<

<   if (IsValidThread && !bCull)

<   {

<       if (bOpacity)

<       {

<           QuadInstanceFlag[ThisThreadID+1] = 1;

<       }

<       else

<       {

<           HoleQuadInstanceFlag[ThisThreadID+1] = 1;

<       }

<   }

<

<   // need flag is complete set value in group

<   GroupMemoryBarrierWithGroupSync();

<

<   // alloc locate to fill data

<   if (ThisThreadID == 0)

<   {

<       int i;

<       [unroll]

<       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { QuadInstanceFlag[i] += QuadInstanceFlag[i-1];}

<       [unroll]

<       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { HoleQuadInstanceFlag[i] += HoleQuadInstanceFlag[i-1];}

<

<       InterlockedAdd(InstanceArgsBuffer[s_IndirectDrawOffset],        QuadInstanceFlag[NumActiveGroupThread],     QuadInstanceOffset);

<       InterlockedAdd(InstanceArgsBuffer[5 + s_IndirectDrawOffset],    HoleQuadInstanceFlag[NumActiveGroupThread], HoleQuadInstanceOffset);

<   }

<

<   // wait alloc locate in group

<   GroupMemoryBarrierWithGroupSync();

<

<   QuadRenderInstance Instance;

<   Instance.PosLevelPacked = ThisInfo.Pos.x | (ThisInfo.Pos.y << 14) | (ThisInfo.Level << 28);

<   Instance.PhysicalAddress = ThisInfo.PhysicalAddress;

<

<   // fill data

<   if (IsValidThread && !bCull)

<   {

<       if (bOpacity)

<       {

<           const uint Offset = (QuadInstanceOffset + QuadInstanceFlag[ThisThreadID]) & VHMParam.QuadInstanceBufferSizeMask;

<           QuadInstanceBuffer[Offset] = Instance;

<       }

<       else

<       {

<           const uint Offset = (HoleQuadInstanceOffset + HoleQuadInstanceFlag[ThisThreadID]) & VHMParam.QuadInstanceBufferSizeMask;

<           HoleQuadInstanceBuffer[Offset] = Instance;

<       }

<   }

<

< #if VHM_STAT

<   if (ThisInfo.Level < sMaxLodLevel)

<   {

<       InterlockedAdd(RWStatBuffer[ThisInfo.Level], 1);

<   }

<   InterlockedAdd(RWStatBuffer[sMaxLodLevel], 1);

<   if (IsValidThread && !bCull)

<   {

<       if (ThisInfo.Level < sMaxLodLevel)

<       {

<           InterlockedAdd(RWStatBuffer[sAfterCullOffset + ThisInfo.Level], 1);

<       }

<       InterlockedAdd(RWStatBuffer[sAfterCullOffset + sMaxLodLevel], 1);

<       if (bOpacity)

<       {

<           InterlockedAdd(RWStatBuffer[sAfterCullOffset * 2], 1);

<       }

<   }

< #endif

<

< }

<

<

< // version 3

<

< // fill level 4 quad in buffer first

< [numthreads(64, 1, 1)]

< void FillLevel4QuadCS(

<   uint3 GroupThreadID : SV_GroupThreadID,

<   uint3 DispatchThreadID : SV_DispatchThreadID

< )

< {

<   if (ThisThreadID == 0)

<   {

<       OutDispatchArgsBuffer[0] = 2;

<       OutDispatchArgsBuffer[1] = 1;

<       OutDispatchArgsBuffer[2] = 1;

<       OutDispatchArgsBuffer[3] = 64;

<   }

<

<   const uint Level = VHMParam.MaxLevel - 3;

<

<   uint4 ThisPackData;

<   ThisPackData.x = ThisThreadID | (Level << 28);

<   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);

<   GetPhysicalAddress(ThisInfo);

<   // for first item, three layer has same PhysicalAddress,

<   // three layer is : this layer, parent layer, parent parent layer

<   ThisPackData.yzw = ThisInfo.PhysicalAddress.xxx;

<

< #if VHM_ONE_PASS

<   uint Read;

<   InterlockedAdd(RWQueueInfo[0].NumActive, 1, Read);

<   InterlockedAdd(RWQueueInfo[0].Write, 1, Read);

<   OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;

< #else

<   OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;

< #endif

< }

<

<

<

< /**

<  * Compute shader to traverse the virtual texture page table and generate an array of items to potentially render for a view.

<  */

<

< groupshared uint NumGroupTasks;

< #if VHM_END_WITH_ONE_STEP

< groupshared uint NumGroupExitRequest;

< #endif

< #if COMPILER_SUPPORTS_WAVE_SIZE

<   WAVESIZE(32)

< #endif

< [numthreads(COLL_THREAD_TOTAL, 1, 1)]

< void CollectQuadsOnePassCS(

<   uint3 GroupThreadID : SV_GroupThreadID,

<   uint3 DispatchThreadId : SV_DispatchThreadID)

< {

< #if VHM_ONE_PASS

<   // Persistant threads stay alive until the work queue is drained.

<   bool bExit = false;

<   while (!bExit)

<   {

<       // Sync and init group task count.

<       NumGroupTasks = 0;

< #if VHM_END_WITH_ONE_STEP

<       NumGroupExitRequest = 0;

< #endif

<       GroupMemoryBarrierWithGroupSync();

<

<       // Try and pull a task.

<       int NumActive;

<       {

<           InterlockedAdd(RWQueueInfo[0].NumActive, -1, NumActive);

<       }

<

<       if (NumActive <= 0 && !bExit)

<       {

<           // No task pulled. Rewind.

<           InterlockedAdd(RWQueueInfo[0].NumActive, 1, NumActive);

<       }

<       else if (!bExit)

<       {

<           // Increment group task count for this loop.

<           uint Dummy;

<           InterlockedAdd(NumGroupTasks, 1, Dummy);

<

<           // Read item to process from queue.

<           uint Read;

<           InterlockedAdd(RWQueueInfo[0].Read, 1, Read);

<

<           const uint4 ThisPackedData = OutSubdivideQuadBuffer[Read & VHMParam.OutBufferSizeMask];

<           SQuadInfo ThisInfo = GetQuadInfo(ThisPackedData, VHMParam.RVTMinLevel);

<

< #if VHM_WITH_FEEDBACK

<           RecordFeedback(ThisInfo, ThisThreadID, true);

< #endif

<

<           // sample height texture

<           const float ThisHeight = GetHeight(ThisInfo);

<           const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);

<

<           bool bCull = false;

< #if VHM_WITH_CULL

<           bool bOpacity;

<           bCull = IsCullQuad(ThisInfo, bOpacity);

<           // if (!bCull)

<           // {

<           //  // Check if occluded.

<           //  bool bOcclude = !OcclusionTest(Pos, Level);

<           //  bCull = bOcclude;

<           // }

< #endif

<

<           const bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;

<

<           if (bCull)

<           {

<               // Store, but don't subdivide.

<               // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(0, 0, 1, 1));

<           }

<           else

<           {

<               if (bThisSubdivide)

<               {

<                   // Add children to queue.

<                   uint Write;

<                   InterlockedAdd(RWQueueInfo[0].Write, 4, Write);

<

<                   [unroll]

<                   for(int i = 0; i < 4; ++i)

<                   {

<                       uint2 ChildPos = ThisInfo.Pos * 2 + uint2(i & 0x1, (i>>1) & 0x1);

<                       uint ChildLevel = ThisInfo.Level - 1;

<                       uint4 ChildPackData;

<                       ChildPackData.x = PackQuadPosLevel(ChildPos, ChildLevel);

<                       ChildPackData.y = 0;

<                       ChildPackData.z = ThisInfo.PhysicalAddress.x;

<                       ChildPackData.w = ThisInfo.PhysicalAddress.y;

<                       OutSubdivideQuadBuffer[(Write + i) & VHMParam.OutBufferSizeMask] = ChildPackData;

<                   }

<

<                   InterlockedAdd(RWQueueInfo[0].NumActive, 4, NumActive);

<               }

<               else

<               {

<                   uint Write;

<                   InterlockedAdd(FinalDispatchArgsBuffer[3], 1, Write);

<                   InterlockedMax(FinalDispatchArgsBuffer[0], ((Write + 1) + COLL_THREAD_TOTAL - 1) / COLL_THREAD_TOTAL);

<

<                   FinalQuadBuffer[Write & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);

<

<                   // Debug draw the bounds.

<                   // if (!bCull)

<                   {

<                       // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(1, 0, 0, 1));

<                   }

<               }

<           }

<       }

<

< #if VHM_END_WITH_ONE_STEP

<       // Exit if no work was found.

<       if (NumActive > VHMParam.NumActiveForOnePassStep)

<       {

<           uint Dummy;

<           InterlockedAdd(NumGroupExitRequest, 1, Dummy);

<       }

< #endif

<

<       DeviceMemoryBarrier();

<       if (NumGroupTasks == 0

< #if VHM_END_WITH_ONE_STEP

<           || NumGroupExitRequest > 0

< #endif

<       )

<       {

<

<           bExit = true;

<       }

<   }

< #endif

< }

---
>
> #include "/Engine/Private/Common.ush"
> #include "/Engine/Private/MortonCode.ush"
> #include "VirtualHeightfieldMesh.ush"
>
> // Constants
> #define COLL_THREAD_TOTAL 32
> #define ThisThreadID GroupThreadID.x
> #define s_SumQuadOffset 3
> #define s_SumDispatchQuadOffset 0
> #define s_DispatchArgsSize 4
> #define s_IndirectDrawOffset 1
> #define s_ClipMask 0.333f
>
> #ifndef VHM_WITH_FEEDBACK
> #define VHM_WITH_FEEDBACK 1
> #endif
>
> #ifndef VHM_STAT
> #define VHM_STAT 1
> #endif
>
> #ifndef VHM_ONE_PASS
> #define VHM_ONE_PASS 0
> #endif
>
>
> struct QuadItem2
> {
>   uint2 Pos;
>   uint Level;
>   uint3 PhysicalAddress;
> };
>
> struct SQuadInfo
> {
>   uint2 Pos;
>   int Level;
>   uint2 TexPos;
>   uint TextureLevel;
>   uint GeoToTexLevelOffset;
>   float GeoToTexLevelOffsetInv;
>   uint3 PhysicalAddress;
>   uint SampleTextureLevel;
>   uint2 SampleTexPos;
>   float SampleGeoToTexLevelOffsetInv;
> };
>
> uint ConvertHeight01ToUInt16(float Height)
> {
>   return uint(Height * 65536.0f);
> }
>
> float ConvertUInt16ToHeight01(uint Val)
> {
>   return (Val & 0xffff) / 65536.0f;
> }
>
> QuadItem2 UnPackQuadItem2(uint4 PackedVal)
> {
>   QuadItem2 Item;
>   Item.Pos = MortonDecode(PackedVal.x & 0xfffffff);
>   Item.Level = PackedVal.x >> 28;
>   Item.PhysicalAddress = PackedVal.yzw;
>   return Item;
> }
>
> uint PackQuadPosLevel(uint2 Pos, uint Level)
> {
>   return MortonEncode(Pos) | (Level << 28);
> }
>
> uint4 PackQuadItem2(in SQuadInfo Info)
> {
>   uint4 Result;
>   Result.yzw = Info.PhysicalAddress;
>   Result.x = PackQuadPosLevel(Info.Pos, Info.Level);
>
>   return Result;
> }
>
> SQuadInfo GetQuadInfo(uint4 PackedVal, uint InRVTMinLevel)
> {
>   QuadItem2 Item = UnPackQuadItem2(PackedVal);
>
>   const int TmpLevel = max(int(Item.Level) - VHMParam.ExtSubdivisionLevel, 0);
>   const uint GeoToTexLevelOffset = max(int(InRVTMinLevel) - TmpLevel, 0) + max(0, VHMParam.ExtSubdivisionLevel - (int)Item.Level); // geometry levels is large than tex levels
>   const float GeoToTexLevelOffsetInv = 1.f / float(1u << GeoToTexLevelOffset);
>
>   const uint TextureLevel = max(TmpLevel - int(InRVTMinLevel), 0);
>   uint2 TexPos = Item.Pos >> GeoToTexLevelOffset;
>
>   SQuadInfo Info;
>   Info.Pos = Item.Pos;
>   Info.Level = Item.Level;
>   Info.PhysicalAddress = Item.PhysicalAddress;
>
>   Info.GeoToTexLevelOffset = GeoToTexLevelOffset;
>   Info.GeoToTexLevelOffsetInv = GeoToTexLevelOffsetInv;
>   Info.TextureLevel = TextureLevel;
>   Info.TexPos = TexPos;
>   Info.SampleTextureLevel = max(0, int(min(Item.Level, VHMParam.MaxLevel - InRVTMinLevel)) - VHMParam.ExtSubdivisionLevel);
>
>   const uint SampleGeoToTexLevelOffset = min(InRVTMinLevel, VHMParam.MaxLevel - Item.Level) + max(0, (int)VHMParam.ExtSubdivisionLevel - (int)Item.Level);
>   Info.SampleTexPos = Item.Pos >> SampleGeoToTexLevelOffset;
>
>   Info.SampleGeoToTexLevelOffsetInv = 1.0f / float(1u << SampleGeoToTexLevelOffset);
>
>   return Info;
> }
>
> float GetMinDistanceLod(SQuadInfo Info, float Height)
> {
>   // Get UV bounding box
>   float2 Scale = (float)(1u << Info.TextureLevel) * VHMParam.PageTableSize.zw;
>   float2 UV0 = ((float2)Info.Pos + float2(0, 0)) * Info.GeoToTexLevelOffsetInv * Scale;
>   float2 UV1 = ((float2)Info.Pos + float2(1, 1)) * Info.GeoToTexLevelOffsetInv * Scale;
>
>   float MinDistanceSq = SquaredDistance((float3(UV0, Height) - VHMParam.ViewOrigin) * VHMParam.UVToWorldScale);
>   [unroll]
>   for (int k = 1; k < 9; ++k) {
>       const int i = k / 3;
>       const int j = k % 3;
>
>       const float2 Lerp = float2(i * 0.5f, j * 0.5f);
>       const float2 UV = Lerp * UV0 + (1 - Lerp) * UV1;
>       const float DistanceSq = SquaredDistance((float3(UV, Height) - VHMParam.ViewOrigin) * VHMParam.UVToWorldScale);
>       MinDistanceSq = min(MinDistanceSq, DistanceSq);
>   }
>   const float MinDistanceLod = CalculateDistanceLod(MinDistanceSq, VHMParam.LodDistances);
>
>   return MinDistanceLod;
> }
>
>
> #if VHM_ONE_PASS
> RWStructuredBuffer<WorkerQueueInfo> RWQueueInfo;
> #endif
> // CS Buffers
> Buffer<uint> InDispatchArgsBuffer;
> Buffer<uint4> InQuadBuffer;
> RWBuffer<uint> OutDispatchArgsBuffer;
> #if !VHM_ONE_PASS
> RWBuffer<uint4> OutSubdivideQuadBuffer;
> #else
> RWCoherentBuffer(uint4) OutSubdivideQuadBuffer;
> #endif
> RWBuffer<uint> RWFeedbackBuffer;
> RWBuffer<uint> FinalDispatchArgsBuffer;
> RWBuffer<uint4> FinalQuadBuffer;
> // - for cull pass
> RWBuffer<uint> InstanceArgsBuffer;
> RWStructuredBuffer<QuadRenderInstance> QuadInstanceBuffer;
> RWStructuredBuffer<QuadRenderInstance> HoleQuadInstanceBuffer;
>
> // #define VHM_STAT 1
> #if VHM_STAT
> static const uint sMaxLodLevel = 15;
> static const uint sAfterCullOffset = 16;
> RWBuffer<uint> RWStatBuffer;
> #endif
>
> //Texture
> Texture2D<uint> PageTableTexture;
> Texture2D<float> HeightTexture;
> Texture2D<float> MaskTexture;
> SamplerState PointSampler;
> Texture2D<float4> HeightMinMaxTexture;
>
>
> void GetPhysicalAddress(inout SQuadInfo Info)
> {
>   const float PhysicalAddress = PageTableTexture.Load(int3(Info.SampleTexPos, Info.SampleTextureLevel));
>   Info.PhysicalAddress.x = PhysicalAddress;
> }
>
> float GetHeight(inout SQuadInfo Info)
> {
>   GetPhysicalAddress(Info);
>   const float3 UVTransform = GetVirtualToPhysicalUVTransform(Info.Pos,
>       // Info.GeoToTexLevelOffsetInv, Info.TextureLevel,
>       Info.SampleGeoToTexLevelOffsetInv, Info.SampleTextureLevel,
>       Info.PhysicalAddress[0], VHMParam.PhysicalPageTransform, VHMParam.NumPhysicalAddressBits);
>
>   // Sample height once to approximate distance.
>   const float2 LocalPhysicalUV = UVTransform.xy + float2(0.5, 0.5) * UVTransform.z;
>   const float Height = HeightTexture.SampleLevel(PointSampler, LocalPhysicalUV, 0);
>   return Height;
> }
>
>
>
> // group shared
> groupshared uint NumActiveGroupThread;
> groupshared uint FeedbackBeginOffset;
> groupshared uint SubdivideQuadFlag[COLL_THREAD_TOTAL+1];
> groupshared uint SubdivideQuadBeginOffset;
> groupshared uint FinalQuadFlag[COLL_THREAD_TOTAL+1];
> groupshared uint FinalQuadBeginOffset;
>
>
> void RecordFeedback(in SQuadInfo ThisInfo, uint CurThreadID, bool IsValid)
> {
>   // const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.
>   const uint MultiWriteCount = 1;
>   // this is optimize, one atomic_add per group
>   if (CurThreadID == 0) { InterlockedAdd(RWFeedbackBuffer[0], NumActiveGroupThread * MultiWriteCount, FeedbackBeginOffset); }
>   GroupMemoryBarrierWithGroupSync();
>
>   if (IsValid)
>   {
>       uint FeedbackPos = FeedbackBeginOffset + (CurThreadID + 1) * MultiWriteCount;
>       uint LevelPlusOne = ThisInfo.SampleTextureLevel + 1;
>       // PageTableFeedbackId is 4bit data, this value had shift to [28,32). fuck...
>       uint PackData = ThisInfo.SampleTexPos.x | (ThisInfo.SampleTexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;
>       for (int i = 0; i < MultiWriteCount; ++i)
>       {
>           RWFeedbackBuffer[FeedbackPos + i] = PackData;
>       }
>   }
> }
>
> float GetMaskValue(in SQuadInfo ThisInfo)
> {
>   float MaskValue = MaskTexture.Load(int3(ThisInfo.TexPos, ThisInfo.TextureLevel));
>   return MaskValue;
> }
>
> bool IsOpacity(float MaskValue)
> {
>   return abs(MaskValue - 1.0f) < 1e-3;
> }
>
> bool IsCullQuad(in SQuadInfo ThisInfo, out bool bOpacity)
> {
>   // Check Occlude
>   const bool bOccludeCull = false; // todo
>
>   // Check Mask
>   const float CLIP_MASK = 0.333f;
>   const float MaskValue = GetMaskValue(ThisInfo);
>   bOpacity = IsOpacity(MaskValue);
>   const bool bMaskCull = MaskValue < CLIP_MASK; // && false; // ignore MaskCull
>
>   // Check Frustum
>   const float2 Scale = (float)(1u << ThisInfo.TextureLevel) * VHMParam.PageTableSize.zw;
>   const float2 UV0 = ((float2)ThisInfo.Pos + float2(0, 0)) * ThisInfo.GeoToTexLevelOffsetInv * Scale;
>   const float2 UV1 = ((float2)ThisInfo.Pos + float2(1, 1)) * ThisInfo.GeoToTexLevelOffsetInv * Scale;
>   // const float Height = GetHeight(ThisInfo);
>   // const float3 UVMin = float3(UV0, Height);
>   // const float3 UVMax = float3(UV1, Height);
>   float2 MinMaxHeight = UnPackMinMaxHeight(HeightMinMaxTexture.SampleLevel(PointSampler, UV0, ThisInfo.TextureLevel));
>   const float3 UVMin = float3(UV0, MinMaxHeight.x);
>   const float3 UVMax = float3(UV1, MinMaxHeight.y);
>   const float3 UVCenter = (UVMin + UVMax) * 0.5;
>   const float3 UVExtern = UVMax - UVMin;
>   const bool bFrustumCull = !PlaneTestAABB(VHMParam.FrustumPlanes, UVCenter, UVExtern);
>
>   bool bCull = bFrustumCull || bMaskCull || bOccludeCull;
>
>   return bCull;
> }
>
>
>
> // Pass Uniform
> uint CurPassCalTime;
>
> #define APPLY_LQT_OPTIM 1
>
> [numthreads(COLL_THREAD_TOTAL, 1, 1)]
> void CollectSubdivideQuadsCS(
>   uint3 GroupThreadID : SV_GroupThreadID,
>   uint3 DispatchThreadID : SV_DispatchThreadID,
>   uint3 GroupID : SV_GroupID
> ) {
>   const uint InArgsTime = CurPassCalTime / 2;
>   const uint OutArgsTimes = (CurPassCalTime + 1) / 2;
>   const uint InArgsOffset = InArgsTime * s_DispatchArgsSize;
>   const uint OutArgsOffset = OutArgsTimes * s_DispatchArgsSize;
>   const uint QuadCount = InDispatchArgsBuffer[InArgsOffset + s_SumQuadOffset];
>   const bool IsValidThread = DispatchThreadID.x < QuadCount;
>   // if invalid, get 0 index in group thread
>   const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;
>
>   // init
>   if (ThisThreadID == 0)
>   {
> #if !APPLY_LQT_OPTIM
>       NumActiveGroupThread = 0;
>       SubdivideQuadBeginOffset = 0;
>       FinalQuadBeginOffset = 0;
> #else
>       uint GroupQuadCountOffset = GroupID.x * COLL_THREAD_TOTAL;
>       uint GroupRemainingQuadCount = QuadCount - GroupQuadCountOffset;
>       NumActiveGroupThread = clamp(GroupRemainingQuadCount, 0, COLL_THREAD_TOTAL);
> #endif
>
>       FeedbackBeginOffset = 0;
>       SubdivideQuadFlag[0] = 0;
>       FinalQuadFlag[0] = 0;
>   }
>   FinalQuadFlag[ThisThreadID+1] = 0;
>   SubdivideQuadFlag[ThisThreadID+1] = 0;
>   GroupMemoryBarrierWithGroupSync();
> #if !APPLY_LQT_OPTIM
>   if (IsValidThread)
>   {
>       uint _Out;
>       InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);
>   }
>   GroupMemoryBarrierWithGroupSync();
> #endif
>
>   // Get This Quad All Info
>   const uint4 ThisPackData = InQuadBuffer[LoadIdx];
>   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);
>
> #if VHM_WITH_FEEDBACK && !APPLY_LQT_OPTIM
>   // Record Feedback
>   RecordFeedback(ThisInfo, ThisThreadID, IsValidThread);
> #endif
>
>   // sample height texture
>   const float ThisHeight = GetHeight(ThisInfo);
>   const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);
>
>   bool bCull = false;
> #if VHM_WITH_CULL
>   bool bOpacity;
>   bCull = IsCullQuad(ThisInfo, bOpacity);
> #endif
>
>
>   const bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;
>   if (IsValidThread && !bCull)
>   {
>       if (bThisSubdivide)
>       {
>           SubdivideQuadFlag[ThisThreadID+1] = 4;
>       }
>       else
>       {
>           FinalQuadFlag[ThisThreadID+1] = 1;
>       }
>   }
>   GroupMemoryBarrierWithGroupSync();
>
> #if !APPLY_LQT_OPTIM
>   // alloc locate to fill data
>   if (ThisThreadID == 0)
>   {
>       int i;
>       [unroll]
>       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { SubdivideQuadFlag[i] += SubdivideQuadFlag[i-1];}
>       [unroll]
>       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { FinalQuadFlag[i] += FinalQuadFlag[i-1];}
>
>       InterlockedAdd(OutDispatchArgsBuffer[OutArgsOffset + s_SumQuadOffset], SubdivideQuadFlag[NumActiveGroupThread], SubdivideQuadBeginOffset);
>       InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset],                        FinalQuadFlag[NumActiveGroupThread],        FinalQuadBeginOffset);
>   }
>
>   // wait alloc locate in group
>   GroupMemoryBarrierWithGroupSync();
>
>   // update dispatch data
>   if (ThisThreadID == 0)
>   {
>       uint SubdivideDispatch  = (COLL_THREAD_TOTAL - 1 + SubdivideQuadFlag[NumActiveGroupThread]  + SubdivideQuadBeginOffset  ) / COLL_THREAD_TOTAL;
>       uint FinalDispatch      = (COLL_THREAD_TOTAL - 1 + FinalQuadFlag[NumActiveGroupThread]      + FinalQuadBeginOffset      ) / COLL_THREAD_TOTAL;
>
>       InterlockedMax(OutDispatchArgsBuffer[OutArgsOffset + s_SumDispatchQuadOffset],  SubdivideDispatch);
>       InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset],        FinalDispatch);
>   }
> #else
> #if VHM_WITH_FEEDBACK
>   // const uint MultiWriteCount = (VHMParam.MaxLevel - ThisInfo.Level); // write more than one, let vt system load quickly.
>   const uint MultiWriteCount = 1;
> #endif
>   // update dispatch data
>   if (ThisThreadID == 0)
>   {
>         [unroll]
>         for (int i = 1; i <= COLL_THREAD_TOTAL; ++i)
>         {
>           SubdivideQuadFlag[i] += SubdivideQuadFlag[i-1];
>           FinalQuadFlag[i] += FinalQuadFlag[i-1];
>         }
>         //[unroll]
>         //for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { FinalQuadFlag[i] += FinalQuadFlag[i-1];}
>
>       int SubdividedCount = SubdivideQuadFlag[COLL_THREAD_TOTAL];
>       if (SubdividedCount > 0)
>       {
>           InterlockedAdd(OutDispatchArgsBuffer[OutArgsOffset + s_SumQuadOffset], SubdividedCount, SubdivideQuadBeginOffset);
>           uint SubdivideDispatch  = (COLL_THREAD_TOTAL - 1 + SubdividedCount  + SubdivideQuadBeginOffset  ) / COLL_THREAD_TOTAL;
>           InterlockedMax(OutDispatchArgsBuffer[OutArgsOffset + s_SumDispatchQuadOffset],  SubdivideDispatch);
>
>       }
>
>       int FinalCount = FinalQuadFlag[COLL_THREAD_TOTAL];
>       if (FinalCount > 0)
>       {
>           InterlockedAdd(FinalDispatchArgsBuffer[s_SumQuadOffset],    FinalCount, FinalQuadBeginOffset);
>           uint FinalDispatch      = (COLL_THREAD_TOTAL - 1 +  FinalCount  + FinalQuadBeginOffset      ) / COLL_THREAD_TOTAL;
>           InterlockedMax(FinalDispatchArgsBuffer[s_SumDispatchQuadOffset],        FinalDispatch);
>       }
>
> #if VHM_WITH_FEEDBACK
>       // this is optimize, one atomic_add per group
>       InterlockedAdd(RWFeedbackBuffer[0], NumActiveGroupThread * MultiWriteCount, FeedbackBeginOffset);
> #endif
>   }
>
> #if VHM_WITH_FEEDBACK
>   // Culled quad needs feedback?
>   if (IsValidThread /*&& !bCull*/)
>   {
>       uint FeedbackPos = FeedbackBeginOffset + (ThisThreadID + 1) * MultiWriteCount;
>       uint LevelPlusOne = ThisInfo.SampleTextureLevel + 1;
>       // PageTableFeedbackId is 4bit data, this value had shift to [28,32). fuck...
>       uint PackData = ThisInfo.SampleTexPos.x | (ThisInfo.SampleTexPos.y << 12) | (LevelPlusOne << 24) | VHMParam.PageTableFeedbackId;
>       for (int i = 0; i < MultiWriteCount; ++i)
>       {
>           RWFeedbackBuffer[FeedbackPos + i] = PackData;
>       }
>   }
> #endif
>
> #endif
>
>
>   // fill data
>   if (IsValidThread && !bCull)
>   {
>       if (bThisSubdivide) // this node subdivide
>       {
>           [unroll]
>           for(int i = 0; i < 4; ++i)
>           {
>               uint2 ChildPos = ThisInfo.Pos * 2 + uint2(i & 0x1, (i>>1) & 0x1);
>               uint ChildLevel = ThisInfo.Level - 1;
>               uint4 ChildPackData;
>               ChildPackData.x = PackQuadPosLevel(ChildPos, ChildLevel);
>               ChildPackData.y = 0;
>               ChildPackData.z = ThisInfo.PhysicalAddress.x;
>               ChildPackData.w = ThisInfo.PhysicalAddress.y;
>               OutSubdivideQuadBuffer[(SubdivideQuadBeginOffset + SubdivideQuadFlag[ThisThreadID] + i) & VHMParam.OutBufferSizeMask] = ChildPackData;
>           }
>       }
>       else
>       {
>           FinalQuadBuffer[(FinalQuadBeginOffset + FinalQuadFlag[ThisThreadID]) & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);
>       }
>   }
> }
>
> groupshared uint QuadInstanceFlag[COLL_THREAD_TOTAL+1];
> groupshared uint QuadInstanceOffset;
> groupshared uint HoleQuadInstanceFlag[COLL_THREAD_TOTAL+1];
> groupshared uint HoleQuadInstanceOffset;
>
> [numthreads(COLL_THREAD_TOTAL, 1, 1)]
> void CullQuadsAndGenerateInstancesCS(
>   uint3 GroupThreadID : SV_GroupThreadID,
>   uint3 DispatchThreadID : SV_DispatchThreadID
> )
> {
>   const bool IsValidThread = DispatchThreadID.x < InDispatchArgsBuffer[s_SumQuadOffset];
>   // if invalid, get 0 index in group thread
>   const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;
>
>   // init
>   if (ThisThreadID == 0)
>   {
> #if !APPLY_LQT_OPTIM
>       NumActiveGroupThread = 0;
> #endif
>       QuadInstanceFlag[0] = 0;
>       QuadInstanceOffset = 0;
>       HoleQuadInstanceFlag[0] = 0;
>       HoleQuadInstanceOffset = 0;
>   }
>   QuadInstanceFlag[ThisThreadID+1] = 0;
>   HoleQuadInstanceFlag[ThisThreadID+1] = 0;
>   GroupMemoryBarrierWithGroupSync();
> #if !APPLY_LQT_OPTIM
>   if (IsValidThread) {
>       uint _Out;
>       InterlockedMax(NumActiveGroupThread, ThisThreadID+1, _Out);
>   }
>   GroupMemoryBarrierWithGroupSync();
> #endif
>
>   // Get This Quad All Info
>   const uint4 ThisPackData = InQuadBuffer[LoadIdx];
>   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);
>   // const float PhysicalAddress = PageTableTexture.Load(int3(ThisInfo.TexPos, ThisInfo.TextureLevel));
>   // ThisInfo.PhysicalAddress = PhysicalAddress;
>
>   bool bCull = false;
>   bool bOpacity = false;
> #if VHM_WITH_CULL
>   bCull = IsCullQuad(ThisInfo, bOpacity);
> #else
>   bOpacity = IsOpacity(GetMaskValue(ThisInfo));
> #endif
>
>   if (IsValidThread && !bCull)
>   {
>       if (bOpacity)
>       {
>           QuadInstanceFlag[ThisThreadID+1] = 1;
>       }
>       else
>       {
>           HoleQuadInstanceFlag[ThisThreadID+1] = 1;
>       }
>   }
>
>   // need flag is complete set value in group
>   GroupMemoryBarrierWithGroupSync();
> #if !APPLY_LQT_OPTIM
>   // alloc locate to fill data
>   if (ThisThreadID == 0)
>   {
>       int i;
>       [unroll]
>       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { QuadInstanceFlag[i] += QuadInstanceFlag[i-1];}
>       [unroll]
>       for (i = 1; i <= COLL_THREAD_TOTAL; ++i) { HoleQuadInstanceFlag[i] += HoleQuadInstanceFlag[i-1];}
>
>       InterlockedAdd(InstanceArgsBuffer[s_IndirectDrawOffset],        QuadInstanceFlag[NumActiveGroupThread],     QuadInstanceOffset);
>       InterlockedAdd(InstanceArgsBuffer[5 + s_IndirectDrawOffset],    HoleQuadInstanceFlag[NumActiveGroupThread], HoleQuadInstanceOffset);
>   }
> #else
>   // alloc locate to fill data
>   if (ThisThreadID == 0)
>   {
>       int i;
>       [unroll]
>       for (i = 1; i <= COLL_THREAD_TOTAL; ++i)
>       {
>           QuadInstanceFlag[i] += QuadInstanceFlag[i-1];
>           HoleQuadInstanceFlag[i] += HoleQuadInstanceFlag[i-1];
>       }
>
>       InterlockedAdd(InstanceArgsBuffer[s_IndirectDrawOffset],        QuadInstanceFlag[COLL_THREAD_TOTAL],        QuadInstanceOffset);
>       InterlockedAdd(InstanceArgsBuffer[5 + s_IndirectDrawOffset],    HoleQuadInstanceFlag[COLL_THREAD_TOTAL],    HoleQuadInstanceOffset);
>   }
> #endif
>   // wait alloc locate in group
>   GroupMemoryBarrierWithGroupSync();
>
>   QuadRenderInstance Instance;
>   Instance.PosLevelPacked = ThisInfo.Pos.x | (ThisInfo.Pos.y << 14) | (ThisInfo.Level << 28);
>   Instance.PhysicalAddress = ThisInfo.PhysicalAddress;
>
>   // fill data
>   if (IsValidThread && !bCull)
>   {
>       if (bOpacity)
>       {
>           const uint Offset = (QuadInstanceOffset + QuadInstanceFlag[ThisThreadID]) & VHMParam.QuadInstanceBufferSizeMask;
>           QuadInstanceBuffer[Offset] = Instance;
>       }
>       else
>       {
>           const uint Offset = (HoleQuadInstanceOffset + HoleQuadInstanceFlag[ThisThreadID]) & VHMParam.QuadInstanceBufferSizeMask;
>           HoleQuadInstanceBuffer[Offset] = Instance;
>       }
>   }
>
> #if VHM_STAT
>   if (ThisInfo.Level < sMaxLodLevel)
>   {
>       InterlockedAdd(RWStatBuffer[ThisInfo.Level], 1);
>   }
>   InterlockedAdd(RWStatBuffer[sMaxLodLevel], 1);
>   if (IsValidThread && !bCull)
>   {
>       if (ThisInfo.Level < sMaxLodLevel)
>       {
>           InterlockedAdd(RWStatBuffer[sAfterCullOffset + ThisInfo.Level], 1);
>       }
>       InterlockedAdd(RWStatBuffer[sAfterCullOffset + sMaxLodLevel], 1);
>       if (bOpacity)
>       {
>           InterlockedAdd(RWStatBuffer[sAfterCullOffset * 2], 1);
>       }
>   }
> #endif
>
> }
>
>
> // version 3
>
> // fill level 4 quad in buffer first
> [numthreads(64, 1, 1)]
> void FillLevel4QuadCS(
>   uint3 GroupThreadID : SV_GroupThreadID,
>   uint3 DispatchThreadID : SV_DispatchThreadID
> )
> {
>   if (ThisThreadID == 0)
>   {
>       OutDispatchArgsBuffer[0] = 2;
>       OutDispatchArgsBuffer[1] = 1;
>       OutDispatchArgsBuffer[2] = 1;
>       OutDispatchArgsBuffer[3] = 64;
>   }
>
>   const uint Level = VHMParam.MaxLevel - 3;
>
>   uint4 ThisPackData;
>   ThisPackData.x = ThisThreadID | (Level << 28);
>   SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);
>   GetPhysicalAddress(ThisInfo);
>   // for first item, three layer has same PhysicalAddress,
>   // three layer is : this layer, parent layer, parent parent layer
>   ThisPackData.yzw = ThisInfo.PhysicalAddress.xxx;
>
> #if VHM_ONE_PASS
>   uint Read;
>   InterlockedAdd(RWQueueInfo[0].NumActive, 1, Read);
>   InterlockedAdd(RWQueueInfo[0].Write, 1, Read);
>   OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;
> #else
>   OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;
> #endif
> }
>
>
>
> /**
>  * Compute shader to traverse the virtual texture page table and generate an array of items to potentially render for a view.
>  */
>
> groupshared uint NumGroupTasks;
> #if VHM_END_WITH_ONE_STEP
> groupshared uint NumGroupExitRequest;
> #endif
> #if COMPILER_SUPPORTS_WAVE_SIZE
>   WAVESIZE(32)
> #endif
> [numthreads(COLL_THREAD_TOTAL, 1, 1)]
> void CollectQuadsOnePassCS(
>   uint3 GroupThreadID : SV_GroupThreadID,
>   uint3 DispatchThreadId : SV_DispatchThreadID)
> {
> #if VHM_ONE_PASS
>   // Persistant threads stay alive until the work queue is drained.
>   bool bExit = false;
>   while (!bExit)
>   {
>       // Sync and init group task count.
>       NumGroupTasks = 0;
> #if VHM_END_WITH_ONE_STEP
>       NumGroupExitRequest = 0;
> #endif
>       GroupMemoryBarrierWithGroupSync();
>
>       // Try and pull a task.
>       int NumActive;
>       {
>           InterlockedAdd(RWQueueInfo[0].NumActive, -1, NumActive);
>       }
>
>       if (NumActive <= 0 && !bExit)
>       {
>           // No task pulled. Rewind.
>           InterlockedAdd(RWQueueInfo[0].NumActive, 1, NumActive);
>       }
>       else if (!bExit)
>       {
>           // Increment group task count for this loop.
>           uint Dummy;
>           InterlockedAdd(NumGroupTasks, 1, Dummy);
>
>           // Read item to process from queue.
>           uint Read;
>           InterlockedAdd(RWQueueInfo[0].Read, 1, Read);
>
>           const uint4 ThisPackedData = OutSubdivideQuadBuffer[Read & VHMParam.OutBufferSizeMask];
>           SQuadInfo ThisInfo = GetQuadInfo(ThisPackedData, VHMParam.RVTMinLevel);
>
> #if VHM_WITH_FEEDBACK
>           RecordFeedback(ThisInfo, ThisThreadID, true);
> #endif
>
>           // sample height texture
>           const float ThisHeight = GetHeight(ThisInfo);
>           const float ThisMinDistanceLod = GetMinDistanceLod(ThisInfo, ThisHeight);
>
>           bool bCull = false;
> #if VHM_WITH_CULL
>           bool bOpacity;
>           bCull = IsCullQuad(ThisInfo, bOpacity);
>           // if (!bCull)
>           // {
>           //  // Check if occluded.
>           //  bool bOcclude = !OcclusionTest(Pos, Level);
>           //  bCull = bOcclude;
>           // }
> #endif
>
>           const bool bThisSubdivide = ThisInfo.Level > 0 && ThisMinDistanceLod < ThisInfo.Level;
>
>           if (bCull)
>           {
>               // Store, but don't subdivide.
>               // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(0, 0, 1, 1));
>           }
>           else
>           {
>               if (bThisSubdivide)
>               {
>                   // Add children to queue.
>                   uint Write;
>                   InterlockedAdd(RWQueueInfo[0].Write, 4, Write);
>
>                   [unroll]
>                   for(int i = 0; i < 4; ++i)
>                   {
>                       uint2 ChildPos = ThisInfo.Pos * 2 + uint2(i & 0x1, (i>>1) & 0x1);
>                       uint ChildLevel = ThisInfo.Level - 1;
>                       uint4 ChildPackData;
>                       ChildPackData.x = PackQuadPosLevel(ChildPos, ChildLevel);
>                       ChildPackData.y = 0;
>                       ChildPackData.z = ThisInfo.PhysicalAddress.x;
>                       ChildPackData.w = ThisInfo.PhysicalAddress.y;
>                       OutSubdivideQuadBuffer[(Write + i) & VHMParam.OutBufferSizeMask] = ChildPackData;
>                   }
>
>                   InterlockedAdd(RWQueueInfo[0].NumActive, 4, NumActive);
>               }
>               else
>               {
>                   uint Write;
>                   InterlockedAdd(FinalDispatchArgsBuffer[3], 1, Write);
>                   InterlockedMax(FinalDispatchArgsBuffer[0], ((Write + 1) + COLL_THREAD_TOTAL - 1) / COLL_THREAD_TOTAL);
>
>                   FinalQuadBuffer[Write & VHMParam.FinalQuadBufferSizeMask] = PackQuadItem2(ThisInfo);
>
>                   // Debug draw the bounds.
>                   // if (!bCull)
>                   {
>                       // DebugDrawUVBox(UVMin, UVMax, UVToWorld, float4(1, 0, 0, 1));
>                   }
>               }
>           }
>       }
>
> #if VHM_END_WITH_ONE_STEP
>       // Exit if no work was found.
>       if (NumActive > VHMParam.NumActiveForOnePassStep)
>       {
>           uint Dummy;
>           InterlockedAdd(NumGroupExitRequest, 1, Dummy);
>       }
> #endif
>
>       DeviceMemoryBarrier();
>       if (NumGroupTasks == 0
> #if VHM_END_WITH_ONE_STEP
>           || NumGroupExitRequest > 0
> #endif
>       )
>       {
>
>           bExit = true;
>       }
>   }
> #endif
> }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#29 (unicode) ====

37a38,61
> #define ENABLE_EXTRA_GPU_STAT 0
>
> #if ENABLE_EXTRA_GPU_STAT
> #define RDG_EXTRA_EVENT_SCOPE(GraphBuilder, StatName) RDG_EVENT_SCOPE(GraphBuilder, StatName)
> #else
> #define RDG_EXTRA_EVENT_SCOPE(GraphBuilder, StatName)
> #endif
>
> static int32 GVHMUseAsyncCompute = 1;
> static FAutoConsoleVariableRef CVarVHMUseAsyncCompute(
>   TEXT("r.VHM.UseAsyncCompute"),
>   GVHMUseAsyncCompute,
>   TEXT("If > 0, all VHM related pass will use async compute"),
>   ECVF_RenderThreadSafe
> );
>
> namespace VirtualHeightfieldMesh
> {
>   ERDGPassFlags GetComputeFlag()
>   {
>       return GVHMUseAsyncCompute > 0 ? ERDGPassFlags::AsyncCompute : ERDGPassFlags::Compute;
>   }
> }
>
155c179
< #define VHM_ENABLE_STAT 1
---
> #define VHM_ENABLE_STAT 0
1956c1980
<           ERDGPassFlags::Compute,
---
>           GetComputeFlag(),
2111a2136,2146
>           GraphBuilder.AddPass(
>                   RDG_EVENT_NAME("InitAllBuffers"),
>                   Parameters,
>                   GetComputeFlag(),
>                   [Parameters, ComputeShader](FRHICommandList& RHICmdList)
>               {
>                   if (Parameters->RWFeedbackBuffer)
>                   {
>                       //todo: If feedback parsing understands append counter we don't need to fully clear
>                       RHICmdList.ClearUAVUint(Parameters->RWFeedbackBuffer->GetRHI(), FUintVector4(0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff));
>                   }
2113,2117c2148,2149
<           FComputeShaderUtils::AddPass(
<               GraphBuilder,
<               RDG_EVENT_NAME("InitAllBuffers"),
<               ComputeShader, Parameters, FIntVector3(1, 1, 1)
<           );
---
>                   FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *Parameters, FIntVector(1, 1, 1));
>               });
2135,2139c2167,2174
<           FComputeShaderUtils::AddPass(
<               GraphBuilder,
<               RDG_EVENT_NAME("FillLevelQuads"),
<               ComputeShader, Parameters, FIntVector3(1, 1, 1)
<           );
---
>           GraphBuilder.AddPass(
>                   RDG_EVENT_NAME("FillLevelQuads"),
>                   Parameters,
>                   GetComputeFlag(),
>                   [Parameters, ComputeShader](FRHICommandList& RHICmdList)
>               {
>                   FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *Parameters, FIntVector(1, 1, 1));
>               });
2215,2220c2250,2258
<           FComputeShaderUtils::AddPass(
<               GraphBuilder,
<               RDG_EVENT_NAME("CollectSubdivideQuads"),
<               ComputeShader, Parameters, InVolatileBuffers.ArgsBuffer[PreVolBufIdx],
<               sizeof(uint32) * (CalTime / 2) * 4
<           );
---
>           GraphBuilder.AddPass(
>                   RDG_EVENT_NAME("CollectSubdivideQuads"),
>                   Parameters,
>                   GetComputeFlag(),
>                   [Parameters, ComputeShader, IndirectBuffer = InVolatileBuffers.ArgsBuffer[PreVolBufIdx], CalTime](FRHICommandList& RHICmdList)
>               {
>                       FComputeShaderUtils::DispatchIndirect(RHICmdList, ComputeShader, *Parameters, IndirectBuffer,
>                   sizeof(uint32) * (CalTime / 2) * 4);
>               });
2253,2258c2291,2298
<           FComputeShaderUtils::AddPass(
<               GraphBuilder,
<               RDG_EVENT_NAME("CollectQuads_OnePass"),
<               ComputeShader, Parameters,
<               FIntVector(CVarVHMCollectPassWavefronts.GetValueOnRenderThread(), 1, 1)
<           );
---
>           GraphBuilder.AddPass(
>                   RDG_EVENT_NAME("CollectQuads_OnePass"),
>                   Parameters,
>                   GetComputeFlag(),
>                   [Parameters, ComputeShader](FRHICommandList& RHICmdList)
>               {
>                   FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *Parameters, FIntVector(CVarVHMCollectPassWavefronts.GetValueOnRenderThread(), 1, 1));
>               });
2386,2390c2426,2434
<           FComputeShaderUtils::AddPass(
<               GraphBuilder,
<               RDG_EVENT_NAME("CullQuadsAndGenerateInstances"),
<               ComputeShader, Parameters, InDispatchArgsBuffer, 0
<           );
---
>
>           GraphBuilder.AddPass(
>                   RDG_EVENT_NAME("CullQuadsAndGenerateInstances"),
>                   Parameters,
>                   GetComputeFlag(),
>                   [Parameters, ComputeShader, IndirectBuffer = InDispatchArgsBuffer](FRHICommandList& RHICmdList)
>               {
>                       FComputeShaderUtils::DispatchIndirect(RHICmdList, ComputeShader, *Parameters, IndirectBuffer, 0);
>               });
2816c2860
<       AddClearUAVPass(GraphBuilder, VTFeedbackBufUAV, 0xffffffff);
---
>       //AddClearUAVPass(GraphBuilder, VTFeedbackBufUAV, 0xffffffff);
2849,2852c2893,2903
<       VirtualHeightfieldMesh::V2::AddPass_InitAllBuffers(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers, DrawBuffers,
<           // just clear tv feedback count at first
<           WorkIndex == 0 ? VTFeedbackBufUAV : nullptr);
<       VirtualHeightfieldMesh::V2::AddPass_FillLevel4Quad_CS(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers);
---
>       {
>           RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_InitBuffer");
>           VirtualHeightfieldMesh::V2::AddPass_InitAllBuffers(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers, DrawBuffers,
>              // just clear tv feedback count at first
>              WorkIndex == 0 ? VTFeedbackBufUAV : nullptr);
>       }
>       {
>           RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_FillLevel4Quad");
>           VirtualHeightfieldMesh::V2::AddPass_FillLevel4Quad_CS(GraphBuilder, GlobalShaderMap, WorkBuffers, VolatileBuffers);
>       }
>
2860a2912
>               RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_SerialCollect");
2871,2873c2923,2931
<           VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
<               VolatileBuffers, VTFeedbackBufUAV, EnableCull, true, WithFeedback);
<           VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
---
>           {
>               RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_OnePassFirstPass");
>               VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
>                   VolatileBuffers, VTFeedbackBufUAV, EnableCull, true, WithFeedback);
>           }
>
>           {
>               RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_OnePassSecondPass");
>               VirtualHeightfieldMesh::V2::AddPass_CollectQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
2874a2933,2941
>           }
>
>       }
>
>       {
>           RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_GenerateInstance");
>           // cull and generate instance
>           VirtualHeightfieldMesh::V2::AddPass_CullQuadsAndGenerateInstances_CS(GraphBuilder, GlobalShaderMap, DrawBuffers,
>               WorkBuffers, VolatileBuffers, false);
2877,2879d2943
<       // cull and generate instance
<       VirtualHeightfieldMesh::V2::AddPass_CullQuadsAndGenerateInstances_CS(GraphBuilder, GlobalShaderMap, DrawBuffers,
<           WorkBuffers, VolatileBuffers, false);
```

#### CL 589356 — 2025/10/14 — 陈永昊

- **提交说明**：--story=1065085 --user=陈永昊 Nanite地形合并功能 —— 合并功能接入构建流水线 https://www.tapd.cn/68880148/s/3261646
- **TAPD**：story=1065085
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：Nanite地形合并功能 —— 合并功能接入构建流水线 https://www.tapd.cn/68880148/s/3261646

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 589356）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#20 (unicode) ====

22a23,32
> #pragma region Engine CYH
>   // just for debug
>   static TAutoConsoleVariable<int32> CVarVHMVisualize(
>   TEXT("r.VHM.Visualize"),
>   1,
>   TEXT("Enable virtual heightfield mesh"),
>   ECVF_RenderThreadSafe
> #pragma endregion
> );
>
43c53
<           if (CVarVHMEnable.GetValueOnGameThread() != 0)
---
>           if (CVarVHMEnable.GetValueOnAnyThread() != 0)
50c60
<           if (CVarVHMEnable.GetValueOnGameThread() != 1)
---
>           if (CVarVHMEnable.GetValueOnAnyThread() != 1)
58c68
<       const bool bEnable = CVarVHMEnable.GetValueOnGameThread() != 0;
---
>       const bool bEnable = CVarVHMEnable.GetValueOnAnyThread() != 0 && CVarVHMVisualize.GetValueOnAnyThread() != 0;
116c126
<       return CVarVHMEnable.GetValueOnAnyThread() != 0
---
>       return CVarVHMEnable.GetValueOnAnyThread() != 0 && CVarVHMVisualize.GetValueOnAnyThread() != 0
```

#### CL 618802 — 2025/10/30 — 任晓宇

- **提交说明**：--story=1070804 --user=任晓宇 【Mobile】材质FeatureLevel造成的渲染错误 https://www.tapd.cn/68880148/s/3406267
- **TAPD**：story=1070804
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【Mobile】材质FeatureLevel造成的渲染错误 https://www.tapd.cn/68880148/s/3406267

- **Shader**：1 个文件
- `Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush` (edit)

📄 查看 VHM 相关 diff（CL 618802）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/VirtualHeightfieldMeshVertexFactory.ush#10 (text) ====

194c194
<       XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1 << (uint)SampleLevel);

---
>       XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1u << (uint)SampleLevel);
```

#### CL 642839 — 2025/11/12 — 张建国\_20240109032154

- **提交说明**：--bug=1149257 --user=张建国\_20240109032154 【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 https://www.tapd.cn/68880148/s/3522915 适配LandscapeNanite和VHM #ShelveForSubmit #review-642825 #PrecheckSuccess
- **TAPD**：bug=1149257
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 https://www.tapd.cn/68880148/s/3522915 适配LandscapeNanite和VHM #ShelveForSubmit #review-642825 #PrecheckSuccess

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 642839）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#21 (unicode) ====

47c47
<       const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
---
>       bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
49a50,66
>
> #pragma region Engine ZXB for VHM and Nanite auto switch for SM5
>       UWorld* CurWorld = nullptr;
>       for (const FWorldContext& Context : GEngine->GetWorldContexts())
>       {
>           UWorld* World = Context.World();
>           if (World && World->IsGameWorld())
>           {
>               CurWorld = World;
>               break;
>           }
>       }
>
>       ERHIFeatureLevel::Type FeatureLevel = CurWorld ? CurWorld->GetFeatureLevel() : GMaxRHIFeatureLevel;
>       EShaderPlatform ShaderPlatform = GetFeatureLevelShaderPlatform(FeatureLevel);
>       bNaniteEnabled &= UseNanite(ShaderPlatform);
> #pragma endregion
```

#### CL 642914 — 2025/11/12 — 张建国\_20240109032154

- **提交说明**：--bug=1149257 --user=张建国\_20240109032154 【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 https://www.tapd.cn/68880148/s/3523349 回退一下，初始化太早了，有崩溃
- **TAPD**：bug=1149257
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA-TBT】【第一轮全量】【SM5】【BR-野区】在超低画质下，人物在高空查看地面时，远处地形全部裁剪消失 https://www.tapd.cn/68880148/s/3523349 回退一下，初始化太早了，有崩溃

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp` (edit)

📄 查看 VHM 相关 diff（CL 642914）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshEnable.cpp#22 (unicode) ====

47c47
<       bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
---
>       const bool bNaniteEnabled = (EnableNaniteCVar != nullptr) ? (EnableNaniteCVar->GetInt() != 0) : true;
50,66d49
<
< #pragma region Engine ZXB for VHM and Nanite auto switch for SM5
<       UWorld* CurWorld = nullptr;
<       for (const FWorldContext& Context : GEngine->GetWorldContexts())
<       {
<           UWorld* World = Context.World();
<           if (World && World->IsGameWorld())
<           {
<               CurWorld = World;
<               break;
<           }
<       }
<
<       ERHIFeatureLevel::Type FeatureLevel = CurWorld ? CurWorld->GetFeatureLevel() : GMaxRHIFeatureLevel;
<       EShaderPlatform ShaderPlatform = GetFeatureLevelShaderPlatform(FeatureLevel);
<       bNaniteEnabled &= UseNanite(ShaderPlatform);
< #pragma endregion
```

#### CL 678481 — 2025/12/01 — 贾李朋

- **提交说明**：--bug=1151320 --user=贾李朋 【EA】【客户端性能】狼人技能引发卡顿MI\_Hero05\_Wolf\_Iris (16.6 ms) https://www.tapd.cn/68880148/s/3698399 #ShelveForSubmit #review-678455 #PrecheckSuccess
- **TAPD**：bug=1151320
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【EA】【客户端性能】狼人技能引发卡顿MI\_Hero05\_Wolf\_Iris (16.6 ms) https://www.tapd.cn/68880148/s/3698399 #ShelveForSubmit #review-678455 #PrecheckSuccess

- **Runtime C++**：2 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h` (edit)

📄 查看 VHM 相关 diff（CL 678481）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp#5 (unicode) ====

202a203,206
> bool FVirtualHeightfieldMeshVertexFactory::PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType) // GR_SHOULD_CACHE(by JLP)

> {

>   return ShouldCompilePermutation(FVertexFactoryShaderPermutationParameters(Platform, MaterialParameters, VertexFactoryType));

> }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h#5 (unicode) ====

92a93
>   static bool PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType); // GR_SHOULD_CACHE(by JLP)
```

#### CL 679794 — 2025/12/02 — 郭智均

- **提交说明**：--bug=1156145 --user=郭智均 【EA】【策划反馈】2\*2 野外输入指令（r.VHM.Visualize 0）后才能看到地表 https://www.tapd.cn/68880148/s/3705611 #ShelveForSubmit #review-679766 #PrecheckSuccess
- **TAPD**：bug=1156145
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【EA】【策划反馈】2\*2 野外输入指令（r.VHM.Visualize 0）后才能看到地表 https://www.tapd.cn/68880148/s/3705611 #ShelveForSubmit #review-679766 #PrecheckSuccess

- **Runtime C++**：2 个文件
- `Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h` (edit)
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (edit)

📄 查看 VHM 相关 diff（CL 679794）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h#7 (unicode) ====

55a56,59
>   /** Contents of virtual texture. */
>   UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = HeightfieldBuild)
>   ERuntimeVirtualTextureMaterialType MaterialTypeForMask = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;
>
117a122,125
> #pragma region shiyu
>   ERuntimeVirtualTextureMaterialType GetMaterialTypeForMask() const { return MaterialTypeForMask; }
> #pragma endregion
>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#9 (unicode) ====

456a457,458
>

>       ERuntimeVirtualTextureMaterialType VTMatType = InComponent->GetMaterialTypeForMask();

457a460
>

502c505
<                   MaxLevel, MipLevel = 0

---
>                   MaxLevel, VTMatType, MipLevel = 0

518c521,522
<                       Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;

---
>                       // Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;

>                       Desc.MaterialType = VTMatType;
```

#### CL 695187 — 2025/12/10 — 张建国\_20240109032154

- **提交说明**：--story=1074379 --user=张建国\_20240109032154 【EA】RVT高精度模式兼容VHM Mask https://www.tapd.cn/68880148/s/3764737
- **TAPD**：story=1074379
- **涉及 VHM 文件**：5 个

**做了什么**：

提交目的：【EA】RVT高精度模式兼容VHM Mask https://www.tapd.cn/68880148/s/3764737

- **Shader**：1 个文件
- `Shaders/Private/HeightfieldMaskRender.usf` (edit)
- **Runtime C++**：4 个文件
- `Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxRender.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Public/HeightfieldMinMaxRender.h` (edit)
- `Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h` (edit)
- `Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp` (edit)

📄 查看 VHM 相关 diff（CL 695187）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/HeightfieldMaskRender.usf#3 (text) ====

34c34
< #if INPUT_FORMAT_MASK_RGBA8

---
> #if INPUT_FORMAT_MASK_RGBA8_A

38a39,43
> #elif INPUT_FORMAT_MASK_RGBA8_B

>   float T00 = SrcTexture[P00].a > 0.0001f ? 1.f : 0.f;

>   float T01 = CheckXY.x != 0 ? (SrcTexture[P01].a > 0.0001f ? 1.f : 0.f) : 0.f;

>   float T10 = CheckXY.y != 0 ? (SrcTexture[P10].a > 0.0001f ? 1.f : 0.f) : 0.f;

>   float T11 = CheckXY.x != 0 && CheckXY.y != 0 ? (SrcTexture[P11].a > 0.0001f ? 1.f : 0.f) : 0.f;


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/HeightfieldMinMaxRender.cpp#4 (unicode) ====

250c250,251
<   EVHMMask_RGBA8,

---
>   EVHMMask_RGBA8_A,

>   EVHMMask_RGBA8_B,

277,278c278,281
<       case EVHMMaskFormat::EVHMMask_RGBA8:

<           OutEnvironment.SetDefine(TEXT("INPUT_FORMAT_MASK_RGBA8"), 1); break;

---
>       case EVHMMaskFormat::EVHMMask_RGBA8_A:

>           OutEnvironment.SetDefine(TEXT("INPUT_FORMAT_MASK_RGBA8_A"), 1); break;

>       case EVHMMaskFormat::EVHMMask_RGBA8_B:

>           OutEnvironment.SetDefine(TEXT("INPUT_FORMAT_MASK_RGBA8_B"), 1); break;

290c293,294
< IMPLEMENT_VHM_MASK_SHADER_TYPE(EVHMMaskFormat::EVHMMask_RGBA8, _RGBA8);

---
> IMPLEMENT_VHM_MASK_SHADER_TYPE(EVHMMaskFormat::EVHMMask_RGBA8_A, _RGBA8_A);

> IMPLEMENT_VHM_MASK_SHADER_TYPE(EVHMMaskFormat::EVHMMask_RGBA8_B, _RGBA8_B);

295a300
>   template<typename TShaderType>

299c304
<       TShaderMapRef<TVHMMaskTextureCS_RGBA8> ComputeShader(GetGlobalShaderMap(GMaxRHIFeatureLevel));

---
>       TShaderMapRef<TShaderType> ComputeShader(GetGlobalShaderMap(GMaxRHIFeatureLevel));

337c342
<       FRDGTextureUAV* DstTextureUAV, FIntPoint DstCoord)

---
>       FRDGTextureUAV* DstTextureUAV, FIntPoint DstCoord, ERuntimeVirtualTextureMaterialType MaterialType)

378c383,391
<               AddMaskFirstPass(GraphBuilder, SRV, Size, UAV);

---
>               if(MaterialType  != ERuntimeVirtualTextureMaterialType::Super_BaseColor_Normal_Specular_Mask)

>               {

>                   AddMaskFirstPass<TVHMMaskTextureCS_RGBA8_A>(GraphBuilder, SRV, Size, UAV);

>               }

>               else

>               {

>                   AddMaskFirstPass<TVHMMaskTextureCS_RGBA8_B>(GraphBuilder, SRV, Size, UAV);

>               }

>


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/HeightfieldMinMaxRender.h#4 (unicode) ====

24c24
<       FRDGTexture* SrcTexture, FIntPoint SrcSize, FRDGTextureUAV* DstTexture, FIntPoint DstCoord);

---
>       FRDGTexture* SrcTexture, FIntPoint SrcSize, FRDGTextureUAV* DstTexture, FIntPoint DstCoord, ERuntimeVirtualTextureMaterialType MaterialType);


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Public/VirtualHeightfieldMeshComponent.h#8 (unicode) ====

6a7
> #include "VT/RuntimeVirtualTexture.h"
55a57,59
>   UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = HeightfieldBuild)
>   TObjectPtr<URuntimeVirtualTexture> MaskRVT = nullptr;
>
123c127
<   ERuntimeVirtualTextureMaterialType GetMaterialTypeForMask() const { return MaterialTypeForMask; }
---
>   ERuntimeVirtualTextureMaterialType GetMaterialTypeForMask() const { return MaskRVT ? MaskRVT->GetMaterialType() : MaterialTypeForMask; }


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/HeightfieldMinMaxTextureBuild.cpp#10 (unicode) ====

437c437
<       RenderCaptureInterface::FScopedCapture RenderCapture((bool)s_CaptureBuildTexture);

---
>       RenderCaptureInterface::FScopedCapture RenderCapture((bool)s_CaptureBuildTexture, TEXT("BuildMaskTexture"));

463c463
<

---
>

521d520
<                       // Desc.MaterialType = ERuntimeVirtualTextureMaterialType::BaseColor_Normal_Specular_Mask_YCoCg;

540c539,540
<                       FRDGTextureRef SrcTexture = GraphBuilder.RegisterExternalTexture(RenderTileResources.GetTileRenderTarget(2));

---
>                       int32 MastTextureIndex = URuntimeVirtualTexture::GetMaskLayerIndex(Desc.MaterialType);

>                       FRDGTextureRef SrcTexture = GraphBuilder.RegisterExternalTexture(RenderTileResources.GetTileRenderTarget(MastTextureIndex));

545c545
<                           DstTextureUAV, FIntPoint(TileX, TileY));

---
>                           DstTextureUAV, FIntPoint(TileX, TileY), Desc.MaterialType);
```

#### CL 716640 — 2025/12/23 — 张建国\_20240109032154

- **提交说明**：--story=1074379 --user=张建国\_20240109032154 【EA】RVT高精度模式兼容VHM Mask https://www.tapd.cn/68880148/s/3764737
- **TAPD**：story=1074379
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA】RVT高精度模式兼容VHM Mask https://www.tapd.cn/68880148/s/3764737

- **Shader**：1 个文件
- `Shaders/Private/HeightfieldMaskRender.usf` (edit)

📄 查看 VHM 相关 diff（CL 716640）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Shaders/Private/HeightfieldMaskRender.usf#4 (text) ====

40,43c40,43
<   float T00 = SrcTexture[P00].a > 0.0001f ? 1.f : 0.f;

<   float T01 = CheckXY.x != 0 ? (SrcTexture[P01].a > 0.0001f ? 1.f : 0.f) : 0.f;

<   float T10 = CheckXY.y != 0 ? (SrcTexture[P10].a > 0.0001f ? 1.f : 0.f) : 0.f;

<   float T11 = CheckXY.x != 0 && CheckXY.y != 0 ? (SrcTexture[P11].a > 0.0001f ? 1.f : 0.f) : 0.f;

---
>   float T00 = SrcTexture[P00].b;

>   float T01 = CheckXY.x != 0 ? SrcTexture[P01].b  : 0.f;

>   float T10 = CheckXY.y != 0 ? SrcTexture[P10].b : 0.f;

>   float T11 = CheckXY.x != 0 && CheckXY.y != 0 ? SrcTexture[P11].b : 0.f;
```

#### CL 816358 — 2026/03/02 — 卫帅

- **提交说明**：--story=1081019 --user=卫帅 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4250404 将EngineUpgrade分支的GPU Profile5.6内容合并到Trunk
- **TAPD**：story=1081019
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4250404 将EngineUpgrade分支的GPU Profile5.6内容合并到Trunk

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (edit)

📄 查看 VHM 相关 diff（CL 816358）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#30 (unicode) ====

2442c2442
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
2839,2841c2839,2841
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
<   DECLARE_GPU_STAT(VHM_CS)
<   DECLARE_GPU_STAT(VHM_VTFeedback)
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
>   DECLARE_GPU_STAT(VHM_CS);
>   DECLARE_GPU_STAT(VHM_VTFeedback);
```

#### CL 817601 — 2026/03/02 — 卫帅

- **提交说明**：--story=1081019 --user=卫帅 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4270147 由于出包机器有报错暂时Revert
- **TAPD**：story=1081019
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4270147 由于出包机器有报错暂时Revert

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 817601）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#31 (unicode) ====

2442c2442
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
2839,2841c2839,2841
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
<   DECLARE_GPU_STAT(VHM_CS);
<   DECLARE_GPU_STAT(VHM_VTFeedback);
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
>   DECLARE_GPU_STAT(VHM_CS)
>   DECLARE_GPU_STAT(VHM_VTFeedback)
```

#### CL 841231 — 2026/03/13 — 贾李朋

- **提交说明**：--story=1082655 --user=贾李朋 【EA】【客户端性能】去除无效ShaderType https://www.tapd.cn/68880148/s/4386583 #ShelveForSubmit #review-841135 #PrecheckSuccess
- **TAPD**：story=1082655
- **涉及 VHM 文件**：2 个

**做了什么**：

提交目的：【EA】【客户端性能】去除无效ShaderType https://www.tapd.cn/68880148/s/4386583 #ShelveForSubmit #review-841135 #PrecheckSuccess

- **Runtime C++**：2 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp` (edit)
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h` (edit)

📄 查看 VHM 相关 diff（CL 841231）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.cpp#6 (unicode) ====

203c203
< bool FVirtualHeightfieldMeshVertexFactory::PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType) // GR_SHOULD_CACHE(by JLP)

---
> bool FVirtualHeightfieldMeshVertexFactory::PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType) // GR_SHOULD_CACHE_VF(by JLP)


==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshVertexFactory.h#6 (unicode) ====

93c93
<   static bool PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType); // GR_SHOULD_CACHE(by JLP)

---
>   static bool PreShouldCompilePermutation(EShaderPlatform Platform, const FMaterialShaderParameters& MaterialParameters, const FVertexFactoryType* VertexFactoryType); // GR_SHOULD_CACHE_VF(by JLP)
```

#### CL 843633 — 2026/03/16 — 卫帅

- **提交说明**：--story=1081019 --user=卫帅 【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4395774 #修复编译报错，增加PS5平台支持
- **TAPD**：story=1081019
- **涉及 VHM 文件**：1 个

**做了什么**：

提交目的：【EA 1.1】【性能工具】UE5.6-GPU Profiler 2.0 https://www.tapd.cn/68880148/s/4395774 #修复编译报错，增加PS5平台支持

- **Runtime C++**：1 个文件
- `Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp` (integrate)

📄 查看 VHM 相关 diff（CL 843633）

```
==== //GR/trunk/UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMesh/Private/VirtualHeightfieldMeshSceneProxy.cpp#32 (unicode) ====

2442c2442
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
2839,2841c2839,2841
<   DECLARE_GPU_STAT(VirtualHeightfieldMesh)
<   DECLARE_GPU_STAT(VHM_CS)
<   DECLARE_GPU_STAT(VHM_VTFeedback)
---
>   DECLARE_GPU_STAT(VirtualHeightfieldMesh);
>   DECLARE_GPU_STAT(VHM_CS);
>   DECLARE_GPU_STAT(VHM_VTFeedback);
```

---

Generated from `code_analysis.md` + `commit_history.md`
· Workspace: F:\ZJG\_GR\_trunk · 2026-05-20 22:24
