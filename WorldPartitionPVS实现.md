# WorldPartitionPVS实现

> 代码位置：`UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp:39-45`
>
> 关联模块：经过本工程定制的 PVS（Potentially Visible Set）系统（`[PVS] Add by @Linsan`），与原版 UE5 的 `FPrecomputedVisibilityHandler`/Lightmass 烘焙路径并存。

---

## 1. CVar 定义

```39:45:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp
int32 GAllowPrecomputedVisibility = 1;
static FAutoConsoleVariableRef CVarAllowPrecomputedVisibility(
	TEXT("r.AllowPrecomputedVisibility"),
	GAllowPrecomputedVisibility,
	TEXT("If zero, precomputed visibility will not be used to cull primitives."),
	ECVF_RenderThreadSafe
	);
```

| 项目 | 内容 |
| --- | --- |
| 名称 | `r.AllowPrecomputedVisibility` |
| 默认值 | `1`（启用） |
| 类型 | `int32` |
| 标志 | `ECVF_RenderThreadSafe`（渲染线程读取安全） |
| 作用域 | 渲染端可见性剔除（CPU 端，作用于 PrimitiveVisibilityMap） |
| 控制目标 | 是否使用"预计算可见性数据"剔除场景图元 |

**核心语义**：当 `r.AllowPrecomputedVisibility = 0` 时，渲染管线会跳过基于预烘焙可见性数据的剔除（无论是原版 PVS 还是定制版 PVS），所有图元仅依赖运行时剔除（视锥剔除、HZB/硬件遮挡查询、距离剔除等）。

---

## 2. 功能定位：预计算可见性（PVS）是什么

PVS（Potentially Visible Set，潜在可见集）是一种**离线预烘焙的空间—可见图元映射表**：

- 离线阶段：将整个关卡空间划分为 3D 网格（Cell），对每个 Cell 中的若干视点进行光线投射 / 渲染采样，记录"从这个 Cell 出发，哪些图元可能被看到"。
- 运行时阶段：根据相机位置定位到当前 Cell，查询其位掩码，把那些"在该 Cell 中绝对看不到的图元"提前剔除掉，省去后续的渲染消耗。

它的特点：
- **CPU 端剔除**：发生在视锥剔除之后、向 GPU 提交绘制之前。
- **保守 + 静态**：只能用于静态/烘焙阶段就存在的图元；动态生成的物体不参与（依靠 `VisibilityId` 关联）。
- **高度压缩**：每个 Cell 的可见数据是位图，再用 zlib 分块压缩。

---

## 3. 该工程中的两套实现

代码中存在 **两套并存** 的 PVS 实现，`r.AllowPrecomputedVisibility` 同时是它们共同的总开关：

### 3.1 UE 原生路径（`FPrecomputedVisibilityHandler` + Lightmass 烘焙）

- 烘焙端：`UnrealLightmass`（`Programs/UnrealLightmass/Private/Lighting/PrecomputedVisibility.cpp`），由 `BuildLighting` 生成，结果存到 `ULevel`。
- 运行时数据：`FScene::PrecomputedVisibilityHandler`（`ScenePrivate.h:3566`）。
- 解析入口：

```257:262:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp
const uint8* FSceneViewState::ResolvePrecomputedVisibilityData(FViewInfo& View, const FScene* InScene)
{
	const uint8* PrecomputedVisibilityData = NULL;
	if (InScene->PrecomputedVisibilityHandler && GAllowPrecomputedVisibility && View.Family->EngineShowFlags.PrecomputedVisibility)
	{
		const FPrecomputedVisibilityHandler& Handler = *InScene->PrecomputedVisibilityHandler;
```

剔除流程（已被本工程注释掉，保留作参考）：

```4244:4268:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneVisibility.cpp
if (View.PrecomputedVisibilityData)
{
	uint8 PrecomputedVisibilityFlags = EOcclusionFlags::CanBeOccluded | EOcclusionFlags::HasPrecomputedVisibility;
	for (FSceneSetBitIterator BitIt(...); ...; ++BitIt)
	{
		FPrimitiveVisibilityId VisibilityId = Scene.PrimitiveVisibilityIds[BitIt.GetIndex()];
		if ((View.PrecomputedVisibilityData[VisibilityId.ByteIndex] & VisibilityId.BitMask) == 0)
		{
			View.PrimitiveVisibilityMap.AccessCorrespondingBit(BitIt) = false;
			INC_DWORD_STAT_BY(STAT_StaticallyOccludedPrimitives, 1);
			...
		}
	}
}
```

### 3.2 定制路径：`FPVSSceneData`（World Partition + Clipmap）

由 `[PVS] Add by @Linsan` 引入，用于支持 World Partition 大世界，按"全局粗网格 + 局部细网格（Clipmap）"组织：

| 组件 | 文件 | 作用 |
| --- | --- | --- |
| `APrecomputedVisibilityCellBucketSetActor` | `Engine/Public/WorldPartition/PrecomputedVisibilitySet/PrecomputedVisibilityCellBucketSetActor.h` | 烘焙后的 PVS 数据载体，`AActor` 形式存放在 World Partition 中流送 |
| `UPVSBucketComponent` | 同上 | Bucket 组件，包含一组 Cell |
| `FPVSSceneData` | `Renderer/Private/PVS/PVSSceneData.{h,cpp}` | 渲染端总管：注册/反注册 Bucket、解析视点数据、执行剔除 |
| `FPVSGridParams` | `PVSWorldPartitionSettings.h` | Grid 描述（`ClipmapLevels`、`CellSizeXY/Z`、Bucket 布局等） |
| `FPVSSceneViewCacheData` | `PVSSceneData.h` | 每个 ViewState 缓存当前 Bucket/Cell 的解压数据 |
| `FPVSViewInfoData` | `PVSSceneData.h` | 每帧 View 当前命中的多 Grid 数据集合 |
| `FPrecomputedVisibilityGPUBaking` | `Renderer/Private/PVS/PrecomputedVisibilityGPUBaking.{h,cpp}` | 编辑器内 **GPU 烘焙器**：用 GPU 渲染采样代替传统 CPU/Lightmass 流程 |

每帧解析与剔除：

```372:401:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp
void FSceneViewState::ResolvePVSData(FViewInfo& View, FScene* InScene)
{
	auto& PVS = InScene->PVSSceneData;
	View.PVSViewInfo.bPVSFeatureEnabled = false;
	if (PVS.HasData() && GAllowPrecomputedVisibility && View.Family->EngineShowFlags.PrecomputedVisibility)
	{
		View.PVSViewInfo.bPVSFeatureEnabled = true;
		FVector ViewOrigin = View.ViewMatrices.GetViewOrigin();
		...
		View.PVSViewInfo.PVSViewDataMap = PVS.ResolvePVSViewData(ViewOrigin, CachedPVSSceneViewData, View.PVSViewInfo.PVSCurrentValidGridMask);
		...
	}
}
```

```4296:4310:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneVisibility.cpp
uint8 PrecomputedVisibilityFlags = EOcclusionFlags::CanBeOccluded | EOcclusionFlags::AllowWorldPartitionPVSCulling;
for (FSceneSetBitIterator BitIt(...); ...; ++BitIt)
{
	if ((Scene.PrimitiveOcclusionFlags[BitIt.GetIndex()] & PrecomputedVisibilityFlags) == PrecomputedVisibilityFlags)
	{
		NumPVSCandidatePrimitives++;
		FPrimitiveVisibilityId VisibilityId = Scene.PrimitiveVisibilityIds[BitIt.GetIndex()];
		if (Scene.PVSSceneData.PVSCullPrimitive(ViewOrigin,
			View.PVSViewInfo, ViewPacket.ViewState->CachedPVSSceneViewData,
			Scene.PrimitiveOcclusionBounds[BitIt.GetIndex()],
			VisibilityId))
		{
			...
		}
	}
}
```

总开关 `r.AllowPrecomputedVisibility` 卡在 **解析阶段**：当其为 0，`PVSViewInfo.bPVSFeatureEnabled` 始终为 `false`，下游 `IsDataValid()` 检查直接短路，剔除不生效。

---

## 4. 实现原理

### 4.1 数据组织（定制路径）

```
PVSSceneData
  └── PVSGridParamsMap : TMap<FGuid, FPVSGridParams>
  │      ├── GlobalMapGridGuid     ← 全局粗网格（覆盖整张世界图）
  │      └── DetailGridGuid (多个) ← 细化局部网格
  │
  └── PVSDataMap : TMap<FGuid, TMap<FIntVector2, FPVSBucket*>>
         ├── 按 Grid 分组
         └── 每个 Grid 内按 Bucket 的 2D 整型坐标索引
                └── FPVSBucket
                     ├── Cells[]                 ← 3D Cell 数组
                     ├── CellDataChunks[Level][] ← 各 Clipmap Level 的压缩数据
                     └── CellBucketNumZ          ← Z 方向 Cell 数
```

每个 Cell 含若干个 **Clipmap Level**（精度递增）的位图数据，每位对应一个 `VisibilityId`（即 `FPrimitiveVisibilityId{ByteIndex, BitMask}`）。

### 4.2 VisibilityId 与图元关联

烘焙阶段为每个静态 Primitive 分配一个 **全局唯一 `VisibilityId`**，落到 `PrimitiveSceneInfo::VisibilityId` 与 `Scene.PrimitiveVisibilityIds[]`（`SOA` 布局，`ScenePrivate.h:3249`），运行时用这个 ID 查 Cell 的位图。

### 4.3 解析（Resolve）

每帧每个 View：
1. 取相机位置 `ViewOrigin`（如有 Frozen ViewMatrices，使用其位置）。
2. 在 `PVSGridDistributionMap`（`TQuadTree<FGuid>`）中查找当前位置覆盖了哪些 Grid。
3. 对每个命中的 Grid：
   - 计算 BucketIndex2D（`(ViewOrigin - BucketOrigin) / BucketSize`）
   - 在 Bucket 内找到包含 ViewOrigin 的 `FPVSCell`
   - 比较 `FPVSSceneViewCacheData::IsCachedDataValid()`，命中则复用缓存的 `DecompressedVisibilityChunk`，否则重新 zlib 解压。
4. 把每个 Grid 当前的 `Cell + 解压后的 BitData` 填入 `View.PVSViewInfo.PVSViewDataMap`。

### 4.4 剔除（Cull）

`PVSCullPrimitive(...)` 对每个 candidate primitive：
- 取 `FPrimitiveVisibilityId`；
- 由 Bounds 在所有命中的 Grid 中选取适合的 Clipmap Level（一般依据距离）；
- 查 `BitData[ByteIndex] & BitMask`：为 0 → 剔除（`PrimitiveVisibilityMap` 对应位置 false）。
- HLOD 走单独的 `PVSCullHLODPrimitive`。

剔除标志位：`EOcclusionFlags::CanBeOccluded | EOcclusionFlags::AllowWorldPartitionPVSCulling`，必须由 PrimitiveSceneInfo 注册时打上。

### 4.5 缓存策略

- **每 ViewState 一份** `CachedPVSSceneViewData`（`FSceneViewState::CachedPVSSceneViewData`），跨帧重用 Bucket / Chunk 解压结果，避免每帧重解压。
- 命中条件：BucketIndex2D 不变 & 各 Level 的 `ChunkIndex` 不变。

### 4.6 数据回吐到游戏线程

```403:460:UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp
void FVisibilityTaskData::SetPVSDataToPersistentLevel()
{
	...
	Scene.GetWorld()->PersistentLevel->SetPVSViewCachedData_RenderThread(
		Scene.PVSSceneData.GetMeshLoadingRange(), ViewOrigin, GlobalGridGuid, DetailGridGuid,
		..., MainView->PVSViewInfo.PVSViewDataMap, LocationParams, GFrameCounterRenderThread);
}
```

渲染端解析出的可见集会回写到 `ULevel`，供游戏线程的资源流送（Mesh Streaming）使用——这是该工程相对 UE 原生的重要扩展：**用 PVS 同时驱动 Streaming**。

### 4.7 相关 CVar 全景

| CVar | 默认值 | 作用 |
| --- | --- | --- |
| `r.AllowPrecomputedVisibility` | 1 | **总开关**（本文主角） |
| `r.ShowPrecomputedVisibilityCells` | 0 | 显示视锥内所有 Cell（蓝框） |
| `r.ShowRelevantPrecomputedVisibilityCells` | 0 | 仅显示相机所在 Cell（绿框）+ 当前 Bucket 蓝框 |
| `PVS.Visualize.ShowPVSCellsNeighborOnly` | 1 | 仅绘制相机附近的 Cell |
| `PVS.Visualize.ShowPrecomputedVisibilityCellsMaxDistance` | 10000 | 可视化最大距离（cm） |
| `PVS.Visualize.ShowGlobalMapCells` | 0 | 显示全局粗网格 Cell |
| `PVS.Visualize.ShowCompiledVisibilityData` | 0 | 仅绘制最细 Clipmap Level Cell |
| `EngineShowFlags.PrecomputedVisibility` | true | 引擎 ShowFlag，与 CVar 是**与**关系 |

---

## 5. 调用链（自顶向下）

```
FSceneRenderer::Render
  └── FVisibilityTaskData::LaunchVisibilityTasks
        └── ComputeAndMarkRelevanceForViewParallel（per-view）
              └── DecompressPrecomputedOcclusion 阶段
                    ├── FSceneViewState::ResolvePVSData(View, &Scene)        ← 检查 r.AllowPrecomputedVisibility
                    │     └── FPVSSceneData::ResolvePVSViewData(...)
                    │           └── 解压/缓存 BitData → View.PVSViewInfo.PVSViewDataMap
                    │
                    └── WorldPartitionPVSCull(ViewPacket, PrimitiveRange)
                          └── FPVSSceneData::PVSCullPrimitive(...)
                                └── 位查找 → 翻转 PrimitiveVisibilityMap

  └── 后续 Pass 仅遍历 PrimitiveVisibilityMap 中为 1 的图元
```

---

## 6. 架构图

### 6.1 模块全景

> 图例：`[原版]` = UE 原生 Lightmass 路径（已注释）；`[定制]` = 本工程 PVS 路径；`<CVar>` = 总开关 / ShowFlag 门控。

```text
======================== 编辑器 / 烘焙端 ========================

  [原版] Lightmass (PrecomputedVisibility.cpp)
     |
     +--> ULevel::PrecomputedVisibilityHandler         (L1)

  [定制] FPrecomputedVisibilityGPUBaking (GPU 采样烘焙)
     |
     +--> [定制] APrecomputedVisibilityCellBucketSetActor (A3)

                          | (L1) Level 加载        | (A3) 流送 RegisterActor
                          v                        v
==================== 运行时 - GameThread ====================

  FScene::SetPrecomputedVisibility (GT1)    FPVSSceneData::AddPrimitive (GT2)
                                                  维护 Bucket / Cell

                          |                        |
                          v                        v
==================== 运行时 - RenderThread ====================

  FScene::                              FScene::PVSSceneData (RT_PVS)
    PrecomputedVisibilityHandler (RT0)         |
        |                                      |
        |   <CVar> r.AllowPrecomputedVisibility       --+ gate 两条路径
        |   <CVar> ShowFlags.PrecomputedVisibility    --+
        v                                      v
  [原版] ResolvePrecomputedVisibility    [定制] ResolvePVSData /
         Data (已注释)                          ResolvePVSViewData
        |                                      |   (+ ViewCache 缓存)
        v                                      v
  [原版] PrecomputedOcclusionCull        [定制] WorldPartitionPVSCull
         (位图查 VisibilityId)                  -> PVSCullPrimitive
        |                                      |
        +-------------------+------------------+
                            v
              ( View.PrimitiveVisibilityMap )   <-- M
                            |
        +-------------------+-------------------+  游戏侧扩展
        |                                       |
        |   [定制] R2 --> SetPVSDataToPersistentLevel (S1)
        |                       |
        |                       v
        |   ULevel::SetPVSViewCachedData_RenderThread (S2)
        |       驱动 Mesh Streaming
        +---------------------------------------+
                            |
                            v
            后续渲染 Pass (DrawList、Shadow、Nanite 等)
```

### 6.2 单帧时序

```text
角色：GT=GameThread  RT=RenderThread(FSceneRenderer)  VS=FSceneViewState
      PVS=FPVSSceneData  Cull=WorldPartitionPVSCull

[1] GT  -->  RT  : BeginRenderViewFamily
[2] RT  -->  VS  : ResolvePVSData(View, Scene)
[3] VS  -->  VS  : 检查 GAllowPrecomputedVisibility
                   && ShowFlags.PrecomputedVisibility
                   && PVS.HasData()

        ┌─ 若全部成立 ────────────────────────────────────────┐
        │ [4] VS  -->  PVS : ResolvePVSViewData(ViewOrigin,    │
        │                    CachedData, ValidMask)            │
        │ [5] PVS -->  VS  : PVSViewDataMap (Grid->Cell/BitData)│
        │ [6] VS  -->  VS  : bPVSFeatureEnabled = true         │
        ├─ 否则（任一不成立）─────────────────────────────────┤
        │ [*] VS  -->  VS  : bPVSFeatureEnabled = false (跳过) │
        └─────────────────────────────────────────────────────┘

[7] RT  -->  Cull : 遍历 PrimitiveVisibilityMap

        ┌─ loop 每个候选 Primitive ───────────────────────────┐
        │ [8]  Cull -->  PVS  : PVSCullPrimitive(VisibilityId, │
        │                       Bounds)                        │
        │ [9]  PVS  -->  Cull : true = 不可见                  │
        │ [10] (若不可见) Cull --> RT : 关闭对应 VisibilityMap │
        │                              Bit                     │
        └─────────────────────────────────────────────────────┘

[11] RT -->  GT  : SetPVSDataToPersistentLevel (回吐 Streaming 信息)
```

---

## 7. 使用方式

### 7.1 控制台 / 配置

```
# 临时关闭（运行时调试用）
r.AllowPrecomputedVisibility 0

# 重新启用
r.AllowPrecomputedVisibility 1
```

也可以写入 `DefaultEngine.ini`：

```ini
[/Script/Engine.RendererSettings]
r.AllowPrecomputedVisibility=1
```

或在 Scalability 配置 / 设备配置文件中按平台设定（标志为 `ECVF_RenderThreadSafe`，可热切换）。

### 7.2 与 ShowFlag 联动

代码中条件是 **`GAllowPrecomputedVisibility && ShowFlags.PrecomputedVisibility`**——所以以下两种方式等价于禁用：
- `r.AllowPrecomputedVisibility 0`
- 在视图中关闭 `Show > Visualize > Precomputed Visibility`

### 7.3 调试可视化建议

```
r.AllowPrecomputedVisibility 1
r.ShowRelevantPrecomputedVisibilityCells 1
PVS.Visualize.ShowPVSCellsNeighborOnly 1
PVS.Visualize.ShowPrecomputedVisibilityCellsMaxDistance 5000
```
之后在视口中可看到当前命中的 Cell（绿）与邻近 Cell（蓝），用于核实 PVS 数据是否覆盖了相机所在区域。

可结合 `stat PVSCulling` 查看：
- `Nanite Cull Total Primitives` / `Nanite Cull Normal Meshes` / `Nanite Cull HLODs` —— 实际剔除数量
- `PVS Grid Allocated Memory` / `Compressed Visibility Data` —— 内存占用
- `Nanite Visibility Culling` cycle stat —— 每帧 PVS 剔除耗时

### 7.4 何时关闭

| 场景 | 建议 |
| --- | --- |
| 默认运行 | 保持 `1` |
| PVS 数据未烘焙 / 数据可疑导致漏渲染 | 临时设 `0` 排除 PVS 嫌疑 |
| 烘焙调试期间，希望立即看到所有真实图元 | 设 `0` |
| 性能压测 PVS 收益 | 在 `0` / `1` 之间切换对比 GPU/CPU 帧时间 |

---

## 8. 注意事项

1. **不影响动态图元**：仅作用于带 `EOcclusionFlags::AllowWorldPartitionPVSCulling`（或原版 `HasPrecomputedVisibility`）标志的图元。
2. **关闭后内存仍存在**：`r.AllowPrecomputedVisibility 0` 只跳过使用，已经加载的 `PVSSceneData` / `PrecomputedVisibilityHandler` 仍占用内存；要彻底释放需要卸载关卡 / 卸载 `APrecomputedVisibilityCellBucketSetActor`。
3. **影响 Mesh Streaming**：因为定制路径会把 PVSViewDataMap 回写给 `PersistentLevel` 驱动加载逻辑（见 `SetPVSDataToPersistentLevel`），关闭 PVS 时上层 Streaming 策略需要有兜底（例如改为距离驱动），否则可能出现 Mesh 不加载的副作用。
4. **`ECVF_RenderThreadSafe` 但要避免高频切换**：每次切换会让缓存的 `DecompressedVisibilityChunk` 失去意义，重新解压代价较高。
5. **与原版互斥**：本工程已将 `View.PrecomputedVisibilityData` 路径整体注释（`PrecomputedOcclusionCull` 函数被注释掉），实际生效的是 `PVSSceneData` 路径；CVar 仍同时控制两者解析入口，便于将来回切。

---

## 9. 关键代码索引

| 用途 | 路径 | 关键符号 |
| --- | --- | --- |
| CVar 定义 | `Renderer/Private/SceneOcclusion.cpp:39` | `GAllowPrecomputedVisibility` |
| 原版 Resolve | `Renderer/Private/SceneOcclusion.cpp:257` | `ResolvePrecomputedVisibilityData` |
| 定制 Resolve | `Renderer/Private/SceneOcclusion.cpp:372` | `ResolvePVSData` |
| 定制剔除 | `Renderer/Private/SceneVisibility.cpp:4277` | `WorldPartitionPVSCull` / `FPVSSceneData::PVSCullPrimitive` |
| Streaming 回吐 | `Renderer/Private/SceneOcclusion.cpp:403` | `SetPVSDataToPersistentLevel` |
| 渲染端数据容器 | `Renderer/Private/PVS/PVSSceneData.h` | `FPVSSceneData` / `FPVSViewInfoData` / `FPVSSceneViewCacheData` |
| World Partition Actor | `Engine/Public/WorldPartition/PrecomputedVisibilitySet/PrecomputedVisibilityCellBucketSetActor.h` | `APrecomputedVisibilityCellBucketSetActor` |
| GPU 烘焙器 | `Renderer/Private/PVS/PrecomputedVisibilityGPUBaking.{h,cpp}` | `FPrecomputedVisibilityGPUBaking` |
| Stats | `Renderer/Public/PVS/PVSStats.h` | `STATGROUP_PVSCulling` |
| 原版烘焙 (Lightmass) | `Programs/UnrealLightmass/Private/Lighting/PrecomputedVisibility.cpp` | `FStaticLightingSystem::SetupPrecomputedVisibility` 等 |
