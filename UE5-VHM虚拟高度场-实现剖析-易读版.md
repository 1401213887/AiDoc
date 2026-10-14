# VHM 虚拟高度场（Virtual Heightfield Mesh）实现剖析

> **这份文档怎么读**
> 1. 先把「一帧全景」看懂（§1），知道有哪几个阶段、数据怎么流。
> 2. 再按顺序看 V3 的五个阶段（§4），每个阶段都画了流程图 + 数据流向。
> 3. **V1（Epic 原版）单独放在 §8，读完 V3 再看**；两者的对比在 §9。混在一起讲会互相干扰。
> 4. 每条结论都标了 `完整文件名:行号`，可以回源码逐条核对。

**代码位置**：`D:\GR_DevTest\UE5EA\Engine\Plugins\Experimental\VirtualHeightfieldMesh\`

---

## 一、先看全景

### 1.1 三句话

1. **它不是「一块地形网格」，而是一个「页表遍历器」。** 每帧在 GPU 上遍历虚拟纹理的页表，把「已驻留显存 + 视野需要」的那些页逐个变成一个 **quad 实例**，最后一次 `DrawIndexedInstancedIndirect` 画出来。CPU 全程不知道要画多少个。
2. **LOD 不是「挑层级」，而是「边遍历边细分」。** 从 64 个粗 quad 起步，每个 quad 若离相机够近就分裂成 4 个，直到层降到 0。
3. **几何由 `SV_VertexID` 当场算出来，没有顶点缓冲。** 每个实例只有 16 字节（位置 + 层级 + 物理页地址），顶点位置靠 VS 查页表、采高度图位移得到。

### 1.2 一帧全景

```figure
overview
```

这张图是全文的目录：**左边 4 张纹理是输入，中间 5 个阶段是 VHM 的全部工作，右边是每步产出。箭头就是数据流向。**

### 1.3 先确认一件事：本仓库跑的是哪个版本

本仓库实际执行的是 **V3 流水线**（`r.VHM.Version` 默认 = 3，`VirtualHeightfieldMeshSceneProxy.cpp:117-122`），代码在 `VirtualHeightfieldMesh3.usf`。

Epic 官方原版是 **V1**（`VirtualHeightfieldMesh.usf`），本仓库保留但默认不走。

> **所以：§2 到 §7 只讲 V3，完全不提 V1。** 想了解 V1 请直接跳到 §8，两者的对比在 §9。先把一条线走通，比两条线对照着看要轻松得多。

---

## 二、输入：地形数据长什么样

### 2.1 高度存在哪 —— 虚拟纹理（RVT）

地形高度不是一张大图，而是被切成一个个 **页（page）**，按需流送到显存。要取某个位置的高度，得先查一张 **页表** 才知道该去哪一页取。

```figure
vt
```

**页表纹素怎么拆**

一个页表纹素 = 一个 `PhysicalAddress`。**它的容器宽度由页表格式决定，两种格式并不一样**：

| 页表格式 | 纹素通道宽度 | 决定它的地方 |
|---|---|---|
| `EVTPageTableFormat::UInt32` | **32 位**（`PF_R32_UINT` 等） | `VirtualTextureSpace.cpp:37-48` `GetFormatForNumLayers()` |
| `EVTPageTableFormat::UInt16` | **16 位**（`PF_R16_UINT` 等） | 同上 |

格式本身由物理空间大小挑选 —— 只有每维 tile 数 ≤ 64 才够格用 16 位
（`VirtualTexturePhysicalSpace.h:121` `DoesSupport16BitPageTable()`），
这个选择落在 `AllocatedVirtualTexture.cpp:138`。

打包装在 `PageTableUpdate.usf:67-81`（VHM 两个文件的注释都指向它）：

```hlsl
#if USE_16BIT
    // We can assume pPage fits in 6 bits and pack the final output to 16 bits
    const uint PageCoordinateBitCount = 6;
#else
    const uint PageCoordinateBitCount = 8;
#endif

uint Page = vLevel;
Page |= pPage.x << 4;
Page |= pPage.y << (4 + PageCoordinateBitCount);
```

展开成位域（`PageCoordinateBitCount` 下称 PCB；`PageX` 与 `PageY` **同宽**）：

| 页表格式 | 容器 | `Lv` | `PageX` | `PageY` | 容器剩余 |
|---|---|---|---|---|---|
| `UInt32`（PCB = 8） | 32 位 | bit[3:0] · 4 位 | bit[11:4] · **8 位** | bit[19:12] · **8 位** | bit[31:20] 共 12 位**无字段**，恒 0 |
| `UInt16`（PCB = 6） | 16 位 | bit[3:0] · 4 位 | bit[9:4] · **6 位** | bit[15:10] · **6 位** | **没有 —— 4 + 6 + 6 = 16，刚好占满** |

这里有两个容易画错的地方：

- **两种格式不是都占 32 位。** `UInt16` 的纹素本身就是 16 位，根本不存在 bit[16..31]。
- **`UInt32` 的高 12 位没有任何字段。** 写端最大只到第 20 位（`pPage.x/y` 各 8 位，
  `PageTableUpdate.usf:56-57`；`vLevel` 4 位），读端也没有任何一处取 bit ≥ 20
  （`VirtualTextureCommon.ush:891`、`VirtualTextureCommon.ush:900`、`VirtualTextureCommon.ush:904-905`）。

为什么 `PageX` 与 `PageY` 一定同宽？页表是 N×N 的正方形，X、Y 天然对称；
`PageY` 顶上不加掩码，只是因为那几位恒为 0。

VHM 侧解码（`VirtualHeightfieldMesh.ush:135-146`）：

```hlsl
Level = pa & 0xf
PageX = (pa >> 4) & ((1u << NumAddressBits) - 1)
PageY = pa >> (4 + NumAddressBits)      // 高位恒为 0，所以不加掩码也能取对
```

`NumAddressBits` 就是 `PageCoordinateBitCount`，取自 `GetPageTableFormat()`：
`UInt16 → 6`、`UInt32 → 8`（`VirtualHeightfieldMeshSceneProxy.cpp:922`）。

引擎自己读这两条时用的掩码，正好印证上表 —— `UInt32` 走 `(pa >> 4) & 0xff` / `(pa >> 12) & 0xff`，
`UInt16` 走 `(pa >> 4) & 0x3f` / `(pa >> 10) & 0x3f`（`VirtualTextureCommon.ush:904-905`）。

**这里有个关键的推论**：既然几何是「照着页表生成」的，那么 **没被加载的页，连几何都不会存在**。地形的精细程度本质上由「VT 流送了哪些页」决定，而不是由某个 LOD 参数直接决定。

页表由 RVT 系统负责流送和淘汰；VHM 只做两件事：**读页表**、**写反馈**（告诉 VT 系统「这段地形我需要第几级页」）。

**物理纹理能有多大 —— 8 位地址其实用不满**

`PageX`/`PageY` 各 8 位，编码上限是 `256 × 256` 个 tile。但**实际跑不到**，
因为尺寸在「挑页表格式」之前，先被两道钳制夹过 —— 都在 `VirtualTextureSystem.cpp`：

```cpp
// :1049-1053  ① 纹理维度上限
if (TileWidthHeight * InDesc.TileSize > GetMax2DTextureDimension())
    TileWidthHeight = FMath::DivideAndRoundDown(GetMax2DTextureDimension(), InDesc.TileSize);

// :1057-1060  ② tile 总数上限  ← 真正的瓶颈
// Page table encoding limits this to 1<<16. But FTexturePagePool::FreeHeap
// being 16bit and needing an overflow bit reduces us to 1<<15.
const int32 MaxTiles = 1 << 15;
const int32 MaxTilesSqrt = 181; //sqrt(1<<15)
TileWidthHeight = FMath::Min(TileWidthHeight, MaxTilesSqrt);
```

② 那句注释把两件事说全了：

- **页表编码**给的是 `1<<16`，开方正好 **256** —— 这就是 8 位地址的由来；
- 但**页池的堆**是 `FBinaryHeap<uint32, uint16> FreeHeap`（`TexturePagePool.h:230`），
  16 位索引还要留一位溢出位 → 只剩 `1<<15` → 开方 **181**。

所以实际上限是 **181 × 181 = 32761 个 tile**，不是 256×256。

顺序很关键：**先**算出尺寸、被两道钳制夹完，**再**拿 `GetSizeInTiles()` 去挑页表格式
（`AllocatedVirtualTexture.cpp:119-138`：`<= 64` → `UInt16`，否则 `UInt32`）。
推论就是 —— **8 位编码宽度从来不是瓶颈**：`SizeInTiles` 最大 181，8 位绰绰有余，
真正卡死的是页池那 `1<<15`。

而且很多时候连 181 都到不了。物理纹理边长 = `SizeInTiles × TileSize`
（`VirtualTexturePhysicalSpace.h:117`），而 RVT 的 tile 尺寸只能在 64 ~ 1024 之间取 ——
`RuntimeVirtualTexture.h:112` `GetClampedTileSize()` = `1 << Clamp(InTileSize + 6, 6, 10)`，
默认值 **256**（`RuntimeVirtualTexture.h:33`）。代入纹理维度上限（`RHIGlobals.h:912-915`，
设备相关，桌面常见 16384）：

| TileSize | 被 ① 纹理维度钳 | 被 ② 181 钳 | 最终 | 落到哪种页表 |
|---|---|---|---|---|
| **256**（默认） | 16384 / 256 = 64 | — | **64 × 64** | `UInt16`（≤ 64） |
| 128 | 16384 / 128 = 128 | — | **128 × 128** | `UInt32` |
| 64 | 16384 / 64 = 256 | **181** | **181 × 181** | `UInt32` |

也就是说：**默认 tile 尺寸下，物理纹理反而停在 64×64，还在 16 位页表的射程之内** ——
想真正吃到那 8 位地址，得把 TileSize 调到 128 或更小。移动端纹理上限更低，就更够不着。

### 2.2 MinMax 金字塔 —— 为什么需要它

剔除时要知道「这个 quad 的高度范围是多少」。如果每次都去采整块高度图，代价太大。

于是离线烘焙一张 **MinMax 纹理**：每个格子只存两个数 —— 这块地的**最低点**和**最高点**。

```figure
minmax
```

有了 `[min, max]`，一个 quad 就是一个立体的 AABB，做视锥剔除只需要 O(1) 的判断，**不用采一次高度图**。

**这里是全文最容易漏掉的一环：它有 mip，而且采样的 mip 级正好等于这个 quad 的层级。** 「O(1)」能成立全靠这一点。

#### 它是一条完整的 mip 链

构建时逐级归并，**每级取上一级 2×2 的 min/max** —— 是真正的极值下采样，不插值：

```cpp
// HeightfieldMinMaxRender.cpp:214-227
void GenerateMinMaxTextureMips(FRDGBuilder& GraphBuilder, FRDGTexture* Texture, FIntPoint SrcSize, int32 NumMips)
{
    FIntPoint Size = SrcSize;
    for (int32 MipLevel = 1; MipLevel < NumMips; ++MipLevel)
    {
        FRDGTextureSRVRef SRV = GraphBuilder.CreateSRV(FRDGTextureSRVDesc::CreateForMipLevel(Texture, MipLevel - 1));
        FRDGTextureUAVRef UAV = GraphBuilder.CreateUAV(FRDGTextureUAVDesc(Texture, MipLevel));
        AddMinMaxMipPass<TMinMaxTextureCS_RGBA8ToRGBA8>(GraphBuilder, SRV, Size, MipLevel, UAV);
        // ...
    }
}
```

mip 数在烘焙时定死（`HeightfieldMinMaxTextureBuild.cpp:255-257`）：

```cpp
const int32 NumTilesX = ((VTDesc.WidthInBlocks * VTDesc.BlockWidthInTiles) << IncLevels) >> DecLevels;
const int32 NumTilesY = /* 同上 */;
const int32 NumMips = (int32)FMath::CeilLogTwo(FMath::Max(NumTilesX, NumTilesY)) + 1;
```

所以：**mip0 一个纹素 = 一个虚拟 tile**，每级缩一半，最后一级 `1×1` 就是全地形的 min/max。

#### 运行时：采样哪一级 mip，由 quad 自己的层级决定

V3 的核心就这一行（`VirtualHeightfieldMesh3.usf:252`，在 `IsCullQuad` 里）：

```hlsl
float2 MinMaxHeight = UnPackMinMaxHeight(HeightMinMaxTexture.SampleLevel(PointSampler, UV0, ThisInfo.TextureLevel));
const float3 UVMin = float3(UV0, MinMaxHeight.x);
const float3 UVMax = float3(UV1, MinMaxHeight.y);
const float3 UVCenter = (UVMin + UVMax) * 0.5;
const float3 UVExtern = UVMax - UVMin;
const bool bFrustumCull = !PlaneTestAABB(VHMParam.FrustumPlanes, UVCenter, UVExtern);
```

注意 `SampleLevel` 的**第三个参数是 `ThisInfo.TextureLevel`** —— 就是这个 quad 自己的 VT 层级
（`VirtualHeightfieldMesh3.usf:90`：`TextureLevel = max(TmpLevel - RVTMinLevel, 0)`）。
采样用的 `UV0` 也是按同一级算出来的（`VirtualHeightfieldMesh3.usf:246`）。

**为什么一次采样就够**：MinMax **mip L 的一个纹素**覆盖的面积，正好等于页表 **mip L 的一个 tile** 覆盖的面积
（两边都是 `NumTiles >> L`）。所以纹素的落点刚好罩住这个 quad 的 footprint ——
粗 quad 采粗 mip、细 quad 采细 mip，既不用重采样，也不会取到比 quad 更大的范围而过度保守。

#### 为什么必须点采样

`VirtualHeightfieldMeshSceneProxy.cpp:1999`（V1 同一处 `VirtualHeightfieldMeshSceneProxy.cpp:2055`）绑的是
`TStaticSamplerState<SF_Point>::GetRHI()`，V3 直接用内置的 `PointSampler`。

**Min/Max 是极值，一旦双线性插值，min 会被抬高、max 会被压低** —— AABB 不再保守，
就会**漏剔**：本该可见的 quad 被剔掉，地形上直接破洞。这是这个设计里少数「写错就出画面问题」的地方。

#### V1 比 V3 多一层 mip 索引重定位

V1 要在层级上再加一个 `MinMaxLevelOffset`（`VirtualHeightfieldMesh.usf:199-201`）：

```hlsl
float MinMaxTextureLevel = max((float)TextureLevel + (float)MinMaxLevelOffset, 0);
float2 MinMaxHeight = UnPackMinMaxHeight(HeightMinMaxTexture.SampleLevel(MinMaxTextureSampler, UV0, MinMaxTextureLevel));
```

偏移在 CPU 侧算（`VirtualHeightfieldMeshSceneProxy.cpp:2493`）：

```cpp
ProxyDesc.MinMaxLevelOffset = ProxyDesc.HeightMinMaxTexture->GetNumMips() - 1 - AllocatedVirtualTexture->GetMaxLevel();
```

它的语义是**把「页表层级索引」重定位到「MinMax mip 索引」**：最粗的 VT 层 `TextureLevel = MaxLevel`
（整个 VT 一个 tile）落到最后一级 mip `NumMips - 1`（`1×1`，全地形 min/max）——
代进去就能验证。

V3 **不需要**这一层：V3 的参数结构里根本没有 `MinMaxLevelOffset`
（`VirtualHeightfieldMeshSceneProxy.cpp:1492`、`VirtualHeightfieldMeshSceneProxy.cpp:1601`
两处只绑了 `HeightMinMaxTexture`），直接把 `TextureLevel` 当 mip 用。
两者等价的条件是 `NumMips - 1 == MaxLevel`，此时偏移为 0 —— 默认配置正是如此
（`NumMinMaxTextureBuildLevels = 0`，`VirtualHeightfieldMeshComponent.h:47`）。

#### 顺带一张同构的 LodBias MinMax 金字塔

`LodBiasMinMaxTexture` 用的是**同一套 min/max mip 链**，但存的是 **LOD 偏置的极值**，不是高度
（构建见 `HeightfieldMinMaxTexture.cpp:162` 起，mip 链在 `HeightfieldMinMaxTexture.cpp:194` 起）。

V1 的 VS 在同一级 mip 上取它，实现「地势起伏大的地方自动加密」：

```hlsl
// VirtualHeightfieldMesh.usf:201
float2 MinMaxLodBias = UnPackMinMaxLodBias(LodBiasMinMaxTexture.SampleLevel(MinMaxTextureSampler, UV0, MinMaxTextureLevel), LodBiasScale);
```

消费点在 `VirtualHeightfieldMesh.usf:261`（细分判断）与 `VirtualHeightfieldMesh.usf:301-302`（反馈层级范围）。

> ⚠️ 这张纹理的打包方式在本仓库里有个**可复现的不一致**，详见 §12.1。

### 2.3 Mask 纹理 —— 本仓库自研的「挖洞」

Mask 只有三种值（`HeightfieldMaskRender.usf:52-81`）：

| 值 | 含义 | 后续处理 |
|---|---|---|
| `1.0` | 不透明地面 | 走**主材质**那一路 |
| `0.5` | 边界（混合区） | 走**洞材质**那一路 |
| `0.0` | 完全遮蔽 | **直接剔除，不生成几何** |

这套机制是 Epic 原版没有的，用来在地形上挖洞（洞穴、隧道、建筑开口）。它的代价见 §4.4 —— 多了一次 draw。

---

## 三、数据结构：每个 buffer 长什么样

> 提示：这一节可以先跳过，等看到 §4 某个阶段用到时再回来查。

### 3.1 四叉树节点 `QuadItem2`（16 字节）

这是**遍历过程中的中间态**，在 `SubdivideQuadBuffer` 里排队。

```figure
node_layout
```

### 3.2 最终渲染实例 `QuadRenderInstance`（16 字节）

这是**交给光栅化的最终态**，在 `InstanceBuffer` 里。

```figure
instance_layout
```

> **别把这两个搞混**：它们都是 16 字节，但位域划分完全不同。
> `QuadItem2` 要跨层级携带 Morton 位置（28 位），所以 `Level` 只有 4 位；
> `QuadRenderInstance` 的 `Level` 只用于 VS 里算 Morph，4 位够用，多出来的位给了 `Pos`。

**`QuadRenderInstance` 到底装了什么**（`VirtualHeightfieldMesh.ush:96-106`）：

```hlsl
/** Final render instance description used by the DrawInstancedIndirect(). */
struct QuadRenderInstance
{
	uint PosLevelPacked;
	uint3 PhysicalAddress;
};
```

4 × uint32 = 16 字节，正好是 `StructuredBuffer<float4>` 的一个元素 —— VS 里读进来先拆开
（`VirtualHeightfieldMeshVertexFactory.ush:24-27`：`asuint(Data.x)` / `asuint(Data.yzw)`）。

**它到底是什么地址**：就是 **RVT 页表纹素里存的那个值** —— 指向 RVT **物理纹理**里的**页格子**。
VHM 侧把物理纹理绑成 `HeightTexture`（`VirtualHeightfieldMeshSceneProxy.cpp:886`），页表绑成 `PageTableTexture`
（`VirtualHeightfieldMeshSceneProxy.cpp:885`）。

它不是字节地址、也不是行跨距地址，而是 `{层级, PageX, PageY}` 的位打包（拆法见 §2.1）。
VHM 用 `GetVirtualToPhysicalUVTransform()` 把它换算成**物理纹理内的 UV**，
再去 `HeightTexture.SampleLevel(HeightSampler, LocalPhysicalUV, 0)` 取高度
（`VirtualHeightfieldMeshVertexFactory.ush:147-155`、`VirtualHeightfieldMeshVertexFactory.ush:168`）。

**它的 3 个槽 = 本层 / 父层 / 祖父层**

存的是**同一块地形在连续三层的页表物理地址**，靠一根「平移寄存器」维持：

- **本层**：每个节点自己重新采一次页表，**只填 `[0]`**
  （`Info.PhysicalAddress.x = PhysicalAddress;`，`VirtualHeightfieldMesh3.usf:174`；
  采样点是 `PageTableTexture.Load(int3(Info.SampleTexPos, Info.SampleTextureLevel))`，`VirtualHeightfieldMesh3.usf:173`）
- **父层 / 祖父层**：细分时整体平移一位继承下来（`VirtualHeightfieldMesh3.usf:445-447`）

```hlsl
ChildPackData.y = 0;                            // 子节点自己的地址，等本层采样后再填
ChildPackData.z = ThisInfo.PhysicalAddress.x;   // 子节点[1] ← 父节点[0]（父层）
ChildPackData.w = ThisInfo.PhysicalAddress.y;   // 子节点[2] ← 父节点[1]（祖父层）
```

种子处还没有祖先，于是拿本层地址占位 —— 注释原话（`VirtualHeightfieldMesh3.usf:620-621`）：

> *"for first item, three layer has same PhysicalAddress, three layer is : this layer, parent layer, parent parent layer"*

占位代码就是那条 `.xxx` 广播（`VirtualHeightfieldMesh3.usf:622`）。

**但这三层目前并没有真正被用起来**，两条证据：

- 渲染路径**只读 `[0]`**（`VirtualHeightfieldMeshVertexFactory.ush:152`）—— `[1]` / `[2]` 全程无人读。
- V1 那边本来就只有一个地址，写实例时直接广播进三个槽，还留了句 TODO
  （`VirtualHeightfieldMesh.usf:413`：`OutInstance.PhysicalAddress.xyz = Item.PhysicalAddress; // todo:just record one address`）。

结论：**V3 在数据结构上预留了「三层」，当前渲染只消费最细的那一层。**

**每个实例画多少索引**

索引缓冲**全局只有一份，所有实例共享**（`VirtualHeightfieldMeshVertexFactory.cpp:139` 在 VF 构造时建一次）：

| 量 | 值 | 出处 |
|---|---|---|
| 一个实例的 quad 数 | `NumInstanceVertexSide²` = 16 × 16 = **256** | —— |
| 每个 quad 的索引数 | **6**（2 个三角形） | —— |
| **一个实例的索引数** | **1536** | `VirtualHeightfieldMeshSceneProxy.cpp:2041`、`VirtualHeightfieldMeshVertexFactory.cpp:75` |
| 一个实例的三角形数 | 256 × 2 = **512** | —— |

这个 1536 会写进间接参数的第一项当 `IndexCountPerInstance`（`VirtualHeightfieldInitBuffers.usf:35`），
所以一次 draw 就是「**1536 个索引 × InstanceCount 个实例**」。

两个顺带的实现细节：

- **索引按 Morton 序生成**，为的是提升顶点复用率 —— 源码注释给了实测数字：
  *"roughly 75% reuse rate vs 66% of naive scanline approach"*（`VirtualHeightfieldMeshVertexFactory.cpp:25`）。
- **默认走 16 位索引**：`NumQuadsPerSide < 256` 时用 `uint16`，否则才用 `uint32`
  （`VirtualHeightfieldMeshVertexFactory.cpp:78`、`VirtualHeightfieldMeshVertexFactory.cpp:82`）。
  默认 `NumInstanceVertexSide = 16 < 256`，所以索引缓冲只有 1536 × 2 = **3 KB**。

### 3.3 `IndirectArgsBuffer`（40 字节）—— 两批 draw 的参数

```figure
args_layout
```

### 3.4 其余缓冲

```figure
buffers
```

---

## 四、V3 主线：五个阶段

### 4.1 阶段① `InitAllBuffersCS` —— 把上一帧的痕迹清干净

一次 `[numthreads(1,1,1)]` 的 dispatch，把队列、dispatch 参数、实例缓冲全部归零。

**为什么单独开一个 pass**：GPU 缓冲是复用的，不并行清零的话，上一帧的残留会让 `InterlockedAdd` 从错误的基数开始累加。

产出的关键值（`VirtualHeightfieldInitBuffers.usf:61-91`）：

```
FinalArgsBuffer    = { 0, 1, 1, 0 }
DispatchArgsBuffer1 = DispatchArgsBuffer2 = { 0, 1, 1, 0 }   （每个 args 槽都重置）
InstanceArgsBuffer[0..4] = { NumIndices, 0, 0, 0, 0 }         （主材质批次）
InstanceArgsBuffer[5..9] = { NumIndices, 0, 0, 0, 0 }         （洞材质批次）
```

### 4.2 阶段② `FillLevel4QuadCS` —— 播种 64 个根节点

```figure
seed
```

**一句话**：不遍历最粗的 3 层，直接铺 8×8 = 64 个起点。

**为什么可以这么干**：更粗的层级（1 / 2 / 4 个 quad）在任何视角下要么占满全屏、要么被整个剔除，遍历它们纯属浪费。地形这个物体有个天然性质 —— **它一定和视野相交**，所以直接从第 4 层起步永远安全。

#### 「第 4 层」是哪一层 —— Level 编号是倒着数的

VHM 的 Level **从上往下递减**：根节点 Level = `MaxLevel`，最细的叶子是 Level 0。所以起点是 `MaxLevel - 3`（`VirtualHeightfieldMesh3.usf:614`）：

```hlsl
const uint Level = VHMParam.MaxLevel - 3;
```

| 层级 | 网格 | quad 数 |
|---|---|---|
| `MaxLevel`（根） | 1×1 | 1 |
| `MaxLevel-1` | 2×2 | 4 |
| `MaxLevel-2` | 4×4 | 16 |
| **`MaxLevel-3`** | **8×8** | **64** ← 从这里起步 |

64 就是这么来的 —— 也正好对上 `[numthreads(64,1,1)]` 与 `OutDispatchArgsBuffer[3] = 64`（`VirtualHeightfieldMesh3.usf:600`、`VirtualHeightfieldMesh3.usf:611`）。

#### 播种时 Morton 的妙用：线程号直接当 Morton 码

这里有一处很省的写法（`VirtualHeightfieldMesh3.usf:616-618`）：

```hlsl
uint4 ThisPackData;
ThisPackData.x = ThisThreadID | (Level << 28);     // 注意：没有调 MortonEncode()
SQuadInfo ThisInfo = GetQuadInfo(ThisPackData, VHMParam.RVTMinLevel);
```

对比标准打包函数（`VirtualHeightfieldMesh3.usf:68-71`）：

```hlsl
uint PackQuadPosLevel(uint2 Pos, uint Level)
{
    return MortonEncode(Pos) | (Level << 28);
}
```

结构完全一样，只是 `FillLevel4QuadCS` **把 `ThisThreadID`（0~63）直接放在了 Morton 码的位置**。因为解包端一律走（`VirtualHeightfieldMesh3.usf:62`）：

```hlsl
Item.Pos = MortonDecode(PackedVal.x & 0xfffffff);
```

所以线程 N 拿到的坐标就是 `MortonDecode(N)`。**0~63 这 64 个连续整数，经 Morton 解码后不重不漏地铺满 8×8 网格** —— 一行赋值把任务分完，零计算。

实际排布（Z 序曲线）：

```
        x=0   1   2   3   4   5   6   7
y=0  │   0   1   4   5  16  17  20  21
y=1  │   2   3   6   7  18  19  22  23
y=2  │   8   9  12  13  24  25  28  29
y=3  │  10  11  14  15  26  27  30  31
y=4  │  32  33  36  37  48  49  52  53
y=5  │  34  35  38  39  50  51  54  55
y=6  │  40  41  44  45  56  57  60  61
y=7  │  42  43  46  47  58  59  62  63
```

线程 0~3 是左上 2×2，0~15 是左上 4×4 —— **连续线程号总落在紧凑方块里**，这是 Z 序的定义性质，也是纹理缓存友好的来源。

#### Morton 编码与父节点编码的关系

这是 Morton 最有用的性质（已用脚本对 128×128 全部 65536 组父子做过验证，零反例）：

```
MortonEncode(child) == (MortonEncode(parent) << 2) | i
```

`i` 就是细分循环里的 `i`（`VirtualHeightfieldMesh3.usf:439-441`），它的两个 bit 正好是 `(dx, dy)`：`i = dx | (dy << 1)`。

反向同样成立：

| 操作 | 效果 |
|---|---|
| `child >> 2` | 得到父节点 Morton |
| `child & 3` | 得到「我是第几个象限」 |
| `m >> (2k)` | 得到往上 k 层的祖先（等价于坐标各右移 k 位） |

**为什么成立**：`MortonEncode` 是位交织，相邻两位固定是一组 `(y, x)`：

```
bit:   ...  7   6   5   4   3   2   1   0
            y3  x3  y2  x2  y1  x1  y0  x0
                └──────┘  └────┘  └────┘
                 第k层     第k-1层   最底层
```

`Pos * 2` 在二进制里是左移 1 位；x、y 同时左移 1 位，在交织后的码里就是整体左移 2 位。空出的最低 2 位刚好装 `(dy, dx)`。

**实例**（父 `Pos = (3,1)`，Morton = 7 = `0b000111`）：

| `i` | `(dx,dy)` | 子 Pos | 子 Morton | 二进制 | `(7<<2)\|i` |
|---|---|---|---|---|---|
| 0 | (0,0) | (6,2) | 28 | `0b011100` | 28 ✓ |
| 1 | (1,0) | (7,2) | 29 | `0b011101` | 29 ✓ |
| 2 | (0,1) | (6,3) | 30 | `0b011110` | 30 ✓ |
| 3 | (1,1) | (7,3) | 31 | `0b011111` | 31 ✓ |

高位 `0111` 原封不动继承自父，低 2 位存 `i`。**四个兄弟的 Morton 码连续（28/29/30/31）**。

**推论：Morton 码就是从根走下来的路径。** 每 2 bit 编码一层的象限选择，所以 `PackQuadPosLevel()` 打出的那个 `uint` 语义上是「**Level 高 4 bit + 从根到本节点的完整路径 28 bit**」。28 ÷ 2 = **最多 14 层细分深度**，与「每轴 14 bit → 16384 网格」是同一个约束的两种说法。

> **注意：当前细分代码并没有利用这个恒等式**，走的是解码 → 坐标算术 → 重编码（`VirtualHeightfieldMesh3.usf:441-444`）。理论上可写成 `(ParentMorton << 2) | i` 省掉往返，但收益有限 —— `ThisInfo.Pos` 在同一个 CS 里还要用于算 UV、AABB、纹理坐标，解码躲不掉。

#### Morton 带来什么好处

| 好处 | 说明 |
|---|---|
| **初始化零成本** | `Pos = MortonDecode(tid)` 只有 5 步位运算；行主序要写 `tid % 8` / `tid / 8`，GPU 上整数除模走慢路径 |
| **空间局部性** | 每线程要采 `HeightTexture` / `HeightMinMaxTexture` / `MaskTexture` 三张图。同一 warp 处理空间相邻的 quad，采样点集中，缓存几乎全命中 |
| **父子是纯位移** | 上面的恒等式；祖先 = 右移 2k 位 |
| **位预算紧凑** | 28 bit Morton + 4 bit Level 挤进一个 `uint`，让 `QuadItem2` 压进 `uint4`（16 B） |

局部性的差距在深层级尤其明显。假设某层网格宽 4096，一个 32 线程的 warp 覆盖：

| 排布 | 覆盖区域 | 2D 局部性 |
|---|---|---|
| 行主序 | 32 × 1 的细长条 | 差（跨 32 列只占 1 行） |
| **Morton** | **8 × 4 的方块** | **好** |

> **Morton 只用在四叉树节点寻址上。** VT 反馈那条路走的是普通行列打包（`VirtualHeightfieldMesh3.usf:215`）—— 因为要喂给 VT 系统，格式必须跟人家对齐。

### 4.3 阶段③ `CollectSubdivideQuadsCS` —— 核心：一轮一轮往下细分

这是 VHM 里最重要、也最长的一个阶段。它会被**重复 dispatch 若干轮**，每一轮做同一件事：**读上一轮吐出的节点，决定每个节点「还要不要更细」**。

```figure
subdiv
```

**一句话判据**：`MinDistanceLod < Level` 就继续细分（`VirtualHeightfieldMesh3.usf:335`）。
意思是「这个 quad 在当前距离下，屏幕尺寸已经撑不起它所在的层级了，需要更细」。

**距离怎么算**：不是取 quad 中心，而是在 quad 内部取 **3×3 = 9 个采样点**求最小距离（`VirtualHeightfieldMesh3.usf:120-129`）。这样才能避免「角点远、中心近」导致整块被错误降级。

**代入一个具体数字**（`r.VHM.LodScale=1`、`Lod0Distance=500`、`Lod0Distribution=1`、`LodDistribution=2`）：

| 相机到 quad 的距离 | LodForDistance0 | LodForDistanceN | 最终 LOD |
|---|---|---|---|
| 250 | saturate(250/500) = 0.5 | log₂(1+0) = 0 | **0.5** |
| 500 | saturate(1.0) = 1.0 | log₂(1+0) = 0 | **1.0** |
| 1000 | saturate(2.0) = 1.0 | log₂(1+1) = 1 | **2.0** |

**组内怎么分配输出位置**：每个线程先在自己组的 `groupshared` 数组里打标记，线程 0 做一次前缀和扫描，然后各线程用 `flag[ThisThreadID]` 当自己的写入偏移。这样**整组只需要对全局做常数次原子操作**（`VirtualHeightfieldMesh3.usf:383-407`）。

**双缓冲为什么必要**：本轮读 `i`、写 `(i+1)`，两块内存完全分开。否则同一个 pass 内部就会 Read-After-Write 冲突。

#### dispatch 是怎么组织的

**间接 dispatch（IndirectDispatch）** —— 每轮的组数不由 CPU 写死，而是上一轮 CS 产出子节点时顺手更新进 args buffer（`VirtualHeightfieldMeshSceneProxy.cpp:2257-2258`）：

```cpp
FComputeShaderUtils::DispatchIndirect(RHICmdList, ComputeShader, *Parameters, IndirectBuffer,
    sizeof(uint32) * (CalTime / 2) * 4);
```

**args buffer 的 4 个 uint**（`s_DispatchArgsSize = 4`，两个 slot 交替 ping-pong）：

| 下标 | 字段名常量 | 含义 |
|---|---|---|
| `[0]` | `s_SumDispatchQuadOffset` | **DispatchGroupCount X** —— 本轮 dispatch 多少组（`ceil(QuadCount/32)`） |
| `[1]` | — | Y（恒 1） |
| `[2]` | — | Z（恒 1） |
| `[3]` | `s_SumQuadOffset` | **QuadCount** —— 本轮有多少节点要处理 |

ping-pong 由 `CurPassCalTime`（每轮 +1 的 pass uniform）切换读写槽（`VirtualHeightfieldMesh3.usf:277-281`）：

```hlsl
const uint InArgsOffset  = (CurPassCalTime / 2)       * s_DispatchArgsSize;   // 读
const uint OutArgsOffset = ((CurPassCalTime + 1) / 2) * s_DispatchArgsSize;   // 写
const uint QuadCount     = InDispatchArgsBuffer[InArgsOffset + s_SumQuadOffset];
```

#### 一次发多少线程

```hlsl
[numthreads(COLL_THREAD_TOTAL, 1, 1)]   // COLL_THREAD_TOTAL = 32
```

一个 threadgroup = **32 线程**；组数 = `ceil(QuadCount / 32)`。总线程数通常比 `QuadCount` 略多（最后一组有尾巴）。

#### 没抢到任务的线程在干什么

最后一组可能有 1~31 个线程超出 `QuadCount`，用 `IsValidThread` 标记（`VirtualHeightfieldMesh3.usf:282-284`）：

```hlsl
const bool IsValidThread = DispatchThreadID.x < QuadCount;
// if invalid, get 0 index in group thread
const uint LoadIdx = IsValidThread ? DispatchThreadID.x : DispatchThreadID.x - ThisThreadID;
```

`DispatchThreadID.x - ThisThreadID` = 本组第 0 个全局线程号，也就是**复用该组第 0 个线程的数据**。

**为什么不让它们提前 return**：后面要做组内前缀和，所有线程必须参与 `GroupMemoryBarrierWithGroupSync()`。HLSL 里部分线程提前退出、其余还在 barrier 上等，是未定义行为。

空闲线程的实际行为：

| 阶段 | 空闲线程做什么 |
|---|---|
| 读 / 解包 | 读同一份 `InQuadBuffer[组内第0个]`，`GetQuadInfo`/`GetHeight`/`GetMinDistanceLod` 照常跑，结果丢弃 |
| 投票 | `SubdivideQuadFlag[tid+1] = 0` / `FinalQuadFlag[tid+1] = 0`（`VirtualHeightfieldMesh3.usf:303-304` 对所有线程无条件清零），不影响前缀和计数 |
| 写出 | `if (IsValidThread && !bCull)` 门控（`VirtualHeightfieldMesh3.usf:434`），**不写任何输出** |

**结论：空闲线程只是陪着走完 barrier 保证组同步正确，不贡献任何输出。**

#### CPU 侧那个 for 循环：为什么存在、跑几轮

GPU 上一次 dispatch 只能处理「当前队列里的节点」，细分产出的子节点要等下一轮。所以 CPU 用一个 `for` 驱动逐层推进（`VirtualHeightfieldMeshSceneProxy.cpp:2908-2918`）：

```cpp
const int32 MaxCalTime = VolatileBuffers.VHMParameter->MaxLevel - 3 + 1; // pre cal 4
bool EnableCull = !CVarVHMDisableCull->GetInt() && true /*defautl need cull*/;
for (int32 CalTime = 0; CalTime < MaxCalTime; CalTime++)
{
    RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_SerialCollect");
    bool WithFeedback = true;
    VirtualHeightfieldMesh::V2::AddPass_CollectSubdivideQuads_CS(GraphBuilder, GlobalShaderMap, WorkBuffers,
        VolatileBuffers, VTFeedbackBufUAV, CalTime, EnableCull, WithFeedback);
}
```

**`MaxLevel - 3 + 1` 的来历**：注释 `pre cal 4` 指阶段② 已经预处理掉最粗的几层，遍历从 `MaxLevel-3` 起步，往下走到 Level 0 —— 共 `MaxLevel - 3 + 1` 层，一层一轮。`MaxLevel = 10` 就是 8 轮。

**`CalTime` 的用途**：既做 ping-pong 槽切换（传成 shader 里的 `CurPassCalTime`），也做 IndirectArgs 的字节偏移（`VirtualHeightfieldMeshSceneProxy.cpp:2258`）。

**这条路径从不存在空转组** —— 组数由 IndirectArgs 精确给出，只有最后一组有 1~31 个尾巴线程。代价是 dispatch 次数等于层数，每次边界有启动开销与 GPU 空泡。

### 4.4 阶段④ `CullQuadsAndGenerateInstancesCS` —— 变成「可以直接画」的实例

```figure
cull
```

**这一阶段有两个产出通道**，对应两次 draw：

| 通道 | 条件 | 写入 | 走哪个材质 |
|---|---|---|---|
| 不透明 | `Mask == 1` | `QuadInstanceBuffer` | `Material` |
| 洞 | `Mask != 1` | `HoleQuadInstanceBuffer` | `HoleMaterial` |

**为什么要在 CS 里分流，而不是在材质里 `discard`**：`discard` 会破坏 TBDR / TSR 上的 HSR（早期深度测试），移动端代价尤其大。把分支从「每像素」挪到「每实例」，代价是多了一次 IndirectDraw。

**两个通道各自一块独立缓冲，大小还不一样**（`VirtualHeightfieldMeshSceneProxy.cpp:1789`、`VirtualHeightfieldMeshSceneProxy.cpp:1803`）：

| 通道 | CS 侧写哪个 UAV | 落到哪块 RHI 缓冲 | 大小 |
|---|---|---|---|
| 不透明 | `QuadInstanceBuffer` | `InstanceBuffer` | `MaxRenderItems × 16 B` |
| 洞 | `HoleQuadInstanceBuffer` | `HoleInstanceBuffer` | `MaxRenderItems × 16 B / 4` |

洞缓冲小 4 倍 —— 源码注释写得很直白：*"hold instance just little"*（`VirtualHeightfieldMeshSceneProxy.cpp:1802`），洞是少数，没必要按满量开。

> CS 侧的名字（`QuadInstanceBuffer` / `HoleQuadInstanceBuffer`）与 RHI 侧的名字（`InstanceBuffer` / `HoleInstanceBuffer`）不是同一个标识符，别当成两条不同的缓冲。
> 两条 draw 怎么各自读到属于自己那份实例，见 §4.5 末尾。

### 4.5 阶段⑤ 光栅化 —— 顶点是从哪来的

**VHM 没有顶点缓冲。** 整个 VF 是自建的，`InitRHI()` 里只挂了一个 `VertexBuffer = nullptr, Stride = 0` 的空顶点流，元素声明列表为空（`VirtualHeightfieldMeshVertexFactory.cpp:153-179`）。

```figure
vs
```

```figure
flow3
```

上面那张讲的是「**一个实例内部**」长什么样；这张讲「**这个实例在地形上处于什么位置、高度从哪取**」，
分三层，读图顺序就是数据流：

| 层 | 回答什么问题 | 关键数据 |
|---|---|---|
| ① Instance 数据 | 铺在哪？ | `Pos`（整数格号）+ `PhysicalAddress` |
| ② RVT（页表 + 物理纹理） | 高度去哪个物理 tile 取？ | 页表查 `NormalizedPos` → `PhysicalAddress` |
| ③ 地形（世界空间） | 结果长什么样？ | `(NormalizedPos, Height) × VirtualHeightfieldToWorld` |

一句话串起来：**`Pos` 说「铺在哪」，页表说「高度去哪取」，矩阵说「世界有多大」** —— 三者互不替代。

**网格怎么来**：`VertexCoord = (VertexId % GRID, VertexId / GRID)`，其中 `GRID = NumInstanceVertexSide + 1`（`VirtualHeightfieldMeshVertexFactory.ush:128-129`、`VirtualHeightfieldMeshVertexFactory.ush:7`）。也就是说，**一个实例 = 一个 GRID×GRID 的规则网格**，顶点坐标全由 `SV_VertexID` 算出。索引缓冲所有实例共享一份。

#### GRID 到底多大 —— 一个 tile 对应多少顶点

```figure
tilemesh
```

`NumInstanceVertexSide = 1 << (TileSizeLog2 - NumQuadsPerTileOfTwo)`（`VirtualHeightfieldMeshSceneProxy.cpp:882`），
其中 `TileSizeLog2 = FloorLog2(RVT TileSize)`（`VirtualHeightfieldMeshSceneProxy.cpp:877`）。
代入默认值就能看出 `NumQuadsPerTileOfTwo` 的几何含义：

| 量 | 默认值 | 出处 |
|---|---|---|
| RVT `TileSize` | **256 × 256 纹素**（默认索引 2 → `1 << (2+6)`） | `RuntimeVirtualTexture.h:33`、`RuntimeVirtualTexture.h:112` |
| `TileSizeLog2` | 8 | `VirtualHeightfieldMeshSceneProxy.cpp:877` |
| `NumQuadPerTileOfTwo` | **4**（可配 0~7） | `VirtualHeightfieldMeshComponent.h:117` |
| `NumInstanceVertexSide` | `1 << (8 - 4)` = **16** | `VirtualHeightfieldMeshSceneProxy.cpp:882` |

#### ⚠️ 先分清两种「纹素」—— 不然后面全乱

| 说法 | 指什么 |
|---|---|
| **地形纹素** | 地面上的高度采样点。整个地形横跨 `TileCount × TileSize` = 256 × 256 = **65536 个地形纹素** |
| **物理纹理纹素** | RVT 物理纹理里真正存的那个像素。**一张 tile = `TileSize × TileSize` 个** —— 与层级无关，但 `TileSize` 本身是可配的（见下框） |

**两者用不同的尺子，同一个 quad 量出来差 `2^Level` 倍** —— 之前文档只写「纹素」没区分，就是这么读岔的。

用两种尺子分别量一遍（默认参数）：

| 尺子 | 一个 quad | 一个 patch（16 × 16 个 quad） | 一张 tile |
|---|---|---|---|
| **物理纹理纹素** | **恒 = 1 个** | 16 × 16 = **256 个** | 256 × 256 = 65536 个 |
| **地形纹素** | `2^Level` 个 | `16 × 2^Level` 个 | `2^(Level+8)` 个 |

**「一个 quad = 一个物理纹素」不随层级变，这是构造出来的恒等式**：
`NumInstanceVertexSide = 2^(TileSizeLog2 − k)`、patch 占一张 tile 的 `1/2^k`，
两者一除正好抵消 —— 所以几何和纹理在每一级都是**逐像素对齐**的。
（这个恒等式**与 `TileSize` 取多少无关**，是这三节里唯一不受配置影响的结论。）

> ### ⚠️ 别把 `TileSize` 记成常量 256
>
> 它是 **RVT 资产上的一个属性**，每个资产可以不一样（`RuntimeVirtualTexture.h:31-33`）：
>
> ```cpp
> /** Page tile size. (Actual values increase in powers of 2) */
> UPROPERTY(EditAnywhere, BluePrintGetter = GetTileSize, Category = Size, meta = (UIMin = "0", UIMax = "4", ...))
> int32 TileSize = 2; // 256
>
> static int32 GetClampedTileSize(int32 InTileSize) { return 1 << FMath::Clamp(InTileSize + 6, 6, 10); }
> ```
>
> 资产里存的是**索引**，实际纹素数 = `1 << (索引 + 6)`，可配索引 0~4 ⇒ **64 / 128 / 256 / 512 / 1024**，
> 默认索引 2 ⇒ **256**。
>
> **而且运行时还会被 DeviceProfile 再偏置一次** —— 同一份资产在不同设备/画质档位下，
> 实际 tile 大小可以不同（`RuntimeVirtualTexture.cpp:349-350`）：
>
> ```cpp
> // Apply LODGroup TileSize bias here.
> const int32 TileSizeBias = UDeviceProfileManager::Get().GetActiveProfile()->GetTextureLODSettings()->GetTextureLODGroup(LODGroup).VirtualTextureTileSizeBias;
> OutDesc.TileSize = GetClampedTileSize(TileSize + TileSizeBias);
> ```
>
> （偏置字段默认 0，`TextureLODSettings.h:117`；每一档 DeviceProfile 的每个 LODGroup 都能单独设。
> 旁边的 `VirtualTextureTileCountBias` 对 `TileCount` 同理。）
>
> **本节所有具体数字**（一张 tile 256×256、16 × 16 quad、17 × 17 = 289 顶点、1536 索引）
> **都是 `TileSize = 256` 这组默认值的代入结果**，换资产、换档位就会变
> —— 因为 `NumInstanceVertexSide = 1 << (TileSizeLog2 − NumQuadsPerTileOfTwo)` 里本来就有 `TileSizeLog2`。

#### patch 与 tile 的关系：一张 tile 被 256 个 patch 共用

`patch` **不是**一张 tile。看 `GetVirtualToPhysicalUVTransform()`（`VirtualHeightfieldMesh.ush:158-169`）：

```hlsl
uint LodShift = (uint)max((int)GetVirtualLevelFromPhysicalAddress(InPhysicalAddress) - (int)InLevel, 0);
float PosDivider = InPosDivider / (float)(1u << LodShift);
float2 MinVirtualUV = frac((float2)InPos * PosDivider);
```

`InPosDivider` = `SampleGeoToTexLevelOffsetInv` = `1/2^RVTMinLevel` = **1/16**（`Level ≤ MaxLod − RVTMinLevel` 时）。
`frac(Pos / 16)` 说明 —— **一张 tile 在每维被切成 16 格，也就是 16 × 16 = 256 个 patch 共用同一张 tile**。

所以准确说法是：

- 一张 **tile**（页）的**覆盖范围随层级变大** —— 它始终是 256 × 256 个**物理纹理纹素**，
  但 mip 0 的 tile 只盖 256 个地形纹素，mip L 的 tile 盖 `2^L × 256` 个地形纹素
- 而一个 **patch 始终只占某个层级那一张 tile 的 1/16**（每维），不是「跨了多个 tile」

> 之前写成「一个 Pos 单位 = 2^(Level−4) 个 tile」是把 **mip0 的 tile 当固定尺子**去量第 L 级的东西，
> 数字当然会随层级翻倍 —— 但**同一片地上，tile 本身也在随层级变大**，这种说法会把关系讲反。

**「一个实例 = 一个 tile」只在一个特定层级成立** —— 一个实例是一个**规则的 16×16 网格**，
它覆盖的地面范围随几何层级变化（下面会看到，它是 `2^(Level−RVTMinLevel)` 个 tile）。

因为 **一个 quad 覆盖 `2^Level` 个纹素**（Level 是这个几何层级），于是：

| 量 | 纹素数 |
|---|---|
| 一个 quad | `2^Level` |
| 一个 patch（`NumInstanceVertexSide` 个 quad） | `16 × 2^Level` |
| 折成 tile（÷ 256） | `2^(Level − 4)` |

代入看就很清楚：

| 几何层级 | 一个实例 patch 覆盖 | 式子 |
|---|---|---|
| `Level < RVTMinLevel`（4） | **不足一个 tile**（几何比纹理细） | `2^(Level−4) < 1` |
| `Level = 4` | **正好 1 个 RVT tile** | `2^0 = 1` |
| `Level > 4` | **`2^(Level−4)` 个 tile**（一个 VT 页跨多个 tile） | Level 6 → 4 个 tile |

配套两条机制：`GeoToTexLevelOffset = max(RVTMinLevel - Level, 0)`
（`VirtualHeightfieldMeshVertexFactory.ush:131`）负责「几何比纹理细」那条路径；
`uint2 TexPos = Item.Pos >> GeoToTexLevelOffset;`（`VirtualHeightfieldMesh3.usf:91`）
负责把几何坐标折回**页坐标**。

验算一遍（默认 `RVTMinLevel = 4`，取 RVT 为 256 × 256 tile、TileSize 256）：

- 最粗层级 patch 只有 1 个、要盖满全地形 → `16 × 2^Level = 256 × 256` ⇒ `Level = 12`
  —— 而 `MaxLevel = FloorLog2(TileCount) + NumQuadsPerTileOfTwo = 8 + 4 = 12` ✓
- 最细层级 `Level = 0` → 每个 patch 只 16 个纹素，全地形 `65536 / 16 = 4096` 个 patch ✓

#### 为什么一个实例只用一个 `PhysicalAddress` 就够 —— 它不会跨页 / 跨 tile

结论：**一个 patch 的所有顶点，采到的都是同一张物理 tile。** 保证来自三层结构。

**① patch 的足迹 ≤ 它查询的那个页**

patch 覆盖的 tile 数 = `2^(SampleLevel − SampleGeoToTexLevelOffset)`。代入默认参数
（`RVTMinLevel = 4`、`ExtSubdivisionLevel = 0`、`MaxLod = 12`）：

| 几何层级 | `SampleLevel` = `min(L, MaxLod−4)` | `SampleGeoToTexLevelOffset` = `min(4, MaxLod−L)` | patch 覆盖 |
|---|---|---|---|
| 12 | 8 | 0 | 256 tile（整块地形） |
| 9 | 8 | 3 | 32 tile |
| 8 | 8 | 4 | 16 tile |
| 6 | 6 | 4 | 4 tile |
| **4** | 4 | 4 | **1 tile** |
| 0 | 0 | 4 | 1/16 tile |

而 patch 查询的页在 mip `SampleLevel`，覆盖 `2^SampleLevel` 个 tile。**恒有
`SampleLevel ≥ Level − RVTMinLevel`**（逐档验证：Level ≤ MaxLod−4 时 `min` 取 `Level`；
否则取 `MaxLod−4 ≥ Level−4`）—— 所以 **页 ≥ patch**，patch 塞得进一个页。

**② 光"塞得下"还不够 —— 还得看两种切分线会不会错开**

把地形按 tile 编号，这时候有两种切分线：

- **页的边界**：每 `2^SampleLevel` 个 tile 划一条
- **patch 的边界**：每 `2^(Level−4)` 个 tile 划一条

要出现跨界，必须有一条 **patch 的边线落在两条页边线中间**。但如果**页边界的位置本身也是 patch 边长的整数倍**，
页边线就必然与某条 patch 边线**重合** —— 两套线互相咬合，patch 根本没有跨界的余地。

而「`2^SampleLevel` 是 `2^(Level−4)` 的整数倍」在 2 的幂这件事上，就等价于 ①（`SampleLevel ≥ Level−4`）。

**代入具体数字**（`Level = 6` → patch 是 4 个 tile，页是 64 个 tile）：

```
tile 编号  0 ──────────────── 64 ──────────────── 128
patch 线   0   4   8   12 ...  64   68  ...
页线       0                   64                  128
                             ↑
                    64 同时是 patch 线，也是页线 —— 两套线咬合
           └── 前 16 个 patch ──┘
```

页线 64 正好落在一条 patch 线上，所以第 16 个 patch 从页边界**刚好**开始，谁也没被切开。

**反例**：假设 patch 是 3 个 tile、页是 64 个 tile。
patch 线落在 `0, 3, 6, …, 63, 66`，页线落在 `0, 64` —— 第 22 个 patch 覆盖 tile `63~66`，
**横跨了 64 这条页边界**。2 的幂之所以安全，就是因为除得尽，出不了这种错位。

**③ 一个页 = 物理纹理里的一张 tile**

VT 的本质就是「虚拟页 → 物理 tile」的映射，页表纹素存的就是这个映射（§2.1）。
于是「patch ⊆ 一个页」直接推得 **「patch 的全部顶点都从同一张物理 tile 采样」** ——
这正是 `Item.PhysicalAddress[0]` 可以只存**一个**地址就给整片网格建 UV 变换的原因
（`VirtualHeightfieldMeshVertexFactory.ush:147-155`）。

**④ 那 morph 之后顶点换页了呢？—— 不影响**

因为**最终高度根本不过这个地址**。看 `VirtualHeightfieldMeshVertexFactory.ush:208-213`，每个顶点是拿自己的 `NormalizedPos`
**重新查一次页表**、再用 `VTComputePhysicalUVs()` 换成物理 UV 的 —— 逐顶点独立解析，
跨页与否由顶点自己解决。

`PhysicalAddress[0]` 只用在 morph **之前**那次「估算距离」的预采样
（`VirtualHeightfieldMeshVertexFactory.ush:167-168`），而那个高度只喂给 `DistanceSq`，粗一点无所谓。

**⑤ 参数侧还有一道护栏**

`NumQuadsPerTileOfTwo` 被钳到不超过 `TileSizeLog2 + ExtSubdivisionLevel - 1`
（`VirtualHeightfieldMeshSceneProxy.cpp:879`），保证 `NumInstanceVertexSide ≥ 2`；
而 `RVTMinLevel` 与 `MaxLevel` 都由 `NumQuadsPerTileOfTwo` 推出
（`VirtualHeightfieldMeshSceneProxy.cpp:880`、`VirtualHeightfieldMeshSceneProxy.cpp:881`）——
三者绑死，用户在外面调不出破坏对齐的组合。

#### 一个 `Pos` 怎么算出 289 个顶点

实例里只有一个 `Pos`，但这不是「一个实例一个点」—— 它是这块 16×16 网格的**原点**，
每个顶点额外带着自己的 `SV_VertexID`，两者相加才是这个顶点的位置。

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:128-129
uint2 VertexCoord = uint2(Input.VertexId % GRID_SIZE, Input.VertexId / GRID_SIZE);
float2 LocalUV = (float2)VertexCoord / (float)(GRID_SIZE - 1);
```

`GRID_SIZE = 17`，所以 `VertexId` 0…288 → `VertexCoord` (0…16, 0…16) → **`LocalUV ∈ [0,1]²`**
（网格内归一化坐标）。

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:142-143
float2 XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1u << (uint)SampleLevel);
float2 NormalizedPos = (XY * VHM.PageTableSize.zw);
```

**先澄清最容易误解的一点：`Pos` 不是世界坐标，也不是 UV —— 它就是「我在方格图里的第几格」。**
`Pos` 是 `uint2`，一个整数格号。

```figure
posgrid
```

**关键在于：每一层的「格」不一样大。** 地形一共 256 × 256 个 tile，VHM 按几何层级把它切成方格：

| 几何层级 | 切成多少格 | **每一格覆盖多少 tile** | `Pos` 能取到 |
|---|---|---|---|
| 4 | 256 × 256 | **1 个 tile** | 0 … 255 |
| 5 | 128 × 128 | **2 个 tile** | 0 … 127 |
| 6 | 64 × 64 | **4 个 tile** | 0 … 63 |
| 8 | 16 × 16 | **16 个 tile** | 0 … 15 |
| 12 | 1 × 1 | **整块地形（256 × 256 个 tile）** | 0 |

**规律是：层级每 +1，格子边长翻倍、格数少 4 倍。** 所以在 Level 4 一格 = 1 个 tile，
往上就是 1 → 2 → 4 → 8 → 16。写成式子：**每一格的边长 = `2^(Level − 4)` 个 tile**。

> 那个 **4** 不是魔法数 —— 它是「**一格刚好等于 1 个 tile**」的那一层，也就是 `RVTMinLevel` 的默认值。

同一个 `Pos = (12, 5)`，在 Level 4 指的是「第 12 列第 5 行**那个 tile**」，
在 Level 6 指的是「第 12 列第 5 行**那块 4-tile 区域**」—— **坐标一样，指的地方大小不一样**。

代码里那个系数 `SampleGeoToTexLevelOffsetInv * (1u << SampleLevel)` 就是上表「每格覆盖多少 tile」的算式：

| 量 | 含义 | 出处 |
|---|---|---|
| `SampleLevel` | 该节点对应的页表 mip | `VirtualHeightfieldMeshVertexFactory.ush:139` |
| `SampleGeoToTexLevelOffset` | 几何坐标折回页坐标要右移几位 | `VirtualHeightfieldMeshVertexFactory.ush:140` |
| 系数 = `2^SampleLevel / 2^SampleGeoToTexLevelOffset` | **每格覆盖多少个 RVT tile** | `VirtualHeightfieldMeshVertexFactory.ush:142` |

**第二步**是把 tile 空间折成 UV：

```hlsl
// VirtualHeightfieldMeshSceneProxy.cpp:904-906
const float PageTableSizeX = AllocatedVirtualTexture->GetWidthInTiles();
UniformParams.PageTableSize = FVector4f(PageTableSizeX, PageTableSizeY, 1.f / PageTableSizeX, 1.f / PageTableSizeY);
```

即 **1 个 XY 单位 = 1 个 RVT tile**，除以 `WidthInTiles` 就归一化到 `[0,1]` 的整块地形 UV。

**把一个具体数字走一遍**（RVT = 256 × 256 tile，几何层级 `Level = 6`）：

```
Pos        = (12, 5)              该层级网格里第 12 列、第 5 行的节点
LocalUV    = (0.25, 0.5)          顶点在这块 patch 的 1/4、1/2 处
           ↓ (Pos + LocalUV) × 4  （Level 6：一个节点占 4 个 tile）
XY         = (12.25, 5.5) × 4  = (49, 22)     ← tile 空间
           ↓ × (1/256)            （PageTableSize.zw）
NormalizedPos = (0.1914, 0.0859)              ← 整块地形的 UV
```

同一个 patch 的 289 个顶点，就是拿各自的 `LocalUV` 代进这同一个式子，得到各自的地形 UV。

**最后一步：UV → 世界坐标，靠矩阵而不是靠 `Pos`。**

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:169
float3 WorldPos = mul(float4(NormalizedPos, Height, 1), VHM.VirtualHeightfieldToWorld).xyz;
```

`VirtualHeightfieldToWorld` 就是 `UVToWorld`，来自组件的虚拟纹理变换
（`VirtualHeightfieldMeshSceneProxy.cpp:792-795`：`const FTransform VirtualTextureTransform = InComponent->GetVirtualTextureTransform();`）。
**地形的世界尺寸、位置、缩放全在这个矩阵里** —— 这也解释了为什么 `Pos` 可以只是一个 14 位的小整数：
它只描述"第几格"，跟世界尺度无关。

最后两段合成顶点位置：

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:241（morph 前的预采样路径用 :169 的等价写法）
Intermediates.VTPos = float3(NormalizedPos, Height);
// VirtualHeightfieldMeshVertexFactory.ush:244
Intermediates.LocalPos = mul(float4(Intermediates.VTPos, 1), VHM.VirtualHeightfieldToLocal).xyz;
```

即 **`(地形UV.x, 地形UV.y, 采到的高度)`** 经 `VirtualHeightfieldToLocal` / `VirtualHeightfieldToWorld` 变换到世界。

**顺序上有一步不能漏**：高度采完之后，`LocalUV` 还会被 CDLOD morph 往粗网格方向吸附一次，
然后 **用 morph 后的 `LocalUV` 重算 `XY` 和 `NormalizedPos`**，才去采最终高度：

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:191-195
LocalUV = MorphVertex(LocalUV, GRID_SIZE - 1, (uint)LodMorphFloor, LodMorphFrac);
XY = ((float2)Pos + LocalUV) * SampleGeoToTexLevelOffsetInv * (1u << (uint)SampleLevel);
NormalizedPos = (XY * VHM.PageTableSize.zw);
```

所以顶点位置最终是「**morph 之后的 `LocalUV`**」算出来的。`MorphVertex()` 本身很简单
（`VirtualHeightfieldMeshVertexFactory.ush:86-101`）：按 `GRID_SIZE >> LodMorphFloor` 把 UV 取模，
减去余数就吸附到粗网格交点上；再按 `LodMorphFrac` 做一次部分吸附，实现两档之间的过渡。

#### 高度怎么来 —— 采样器用的是哪一个

分两层，别混（**页内是双线性，跨 LOD 是手工插值**）：

| 层次 | 采样器 | 依据 |
|---|---|---|
| **页内取高度** | **`SF_Bilinear`（双线性插值）** | `VirtualHeightfieldMeshSceneProxy.cpp:887` |
| 跨 mip 过渡 | **手工 `lerp`**，不是硬件 mip | `VirtualHeightfieldMeshVertexFactory.ush:208-214` |

页内这次采样**永远采 mip 0**：`VHM.HeightTexture.SampleLevel(VHM.HeightSampler, LocalPhysicalUV, 0)`
（`VirtualHeightfieldMeshVertexFactory.ush:168`）—— 物理页纹理本身没有 mip 链。

因为拿不到硬件 mip 过渡，切 LOD 只能在 shader 里手工做：查两次页表（`floor(SampleLevel)` / `ceil(SampleLevel)`），
各采一次高度，再按 `frac(SampleLevel)` 插值：

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:208-214
VTPageTableResult VTResult0 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, floor(SampleLevel) - GetGlobalVirtualTextureMipBias());
float2 UV0 = VTComputePhysicalUVs(VTResult0, 0, Uniform);
float Height0 = VHM.HeightTexture.SampleLevel(VHM.HeightSampler, UV0, 0);
VTPageTableResult VTResult1 = TextureLoadVirtualPageTableLevel(VHM.PageTableTexture, PageTableUniform, NormalizedPos, VTADDRESSMODE_CLAMP, VTADDRESSMODE_CLAMP, ceil(SampleLevel) - GetGlobalVirtualTextureMipBias());
float2 UV1 = VTComputePhysicalUVs(VTResult1, 0, Uniform);
float Height1 = VHM.HeightTexture.SampleLevel(VHM.HeightSampler, UV1, 0);
float Height = lerp(Height0.x, Height1.x, frac(SampleLevel));
```

注意这里**不是**复用前面那次预采样的 UV，而是**拿 morph 之后的 `NormalizedPos` 重查一遍页表**，
再用 `VTComputePhysicalUVs()` 换成物理 UV —— 因为顶点在 morph 之后已经移动过了。

> 引擎自己在紧邻的注释里承认了这个双线性的代价（`VirtualHeightfieldMeshVertexFactory.ush:186`）：
> *"A fix for this while keeping fractional LOD is to use some sort of triangle barycentric interpolation
> when sampling the height texture **instead of bilinear**."*
> —— 双线性会让顶点偏离它所插值的三角形平面，于是出现表面闪烁。

**对照：另外两张纹理是点采样，不能混用。** MinMax（`VirtualHeightfieldMeshSceneProxy.cpp:1999`）
和 LodBias（`VirtualHeightfieldMeshSceneProxy.cpp:889`）都是 `SF_Point` —— 它们存的是**极值 / 参数**，
一旦插值语义就错了（MinMax 插值会导致 AABB 不保守而漏剔）。

**顶点 Morph（消除 LOD 跳变）**：VS 里先采一次高度估算距离，算出应该处的 LOD，然后把顶点 UV 往粗网格方向「吸附」（`VirtualHeightfieldMeshVertexFactory.ush:86-101`）。

> 源码里有一条很重要的设计注释（`VirtualHeightfieldMeshVertexFactory.ush:184-187`）：
> *"Removing fractional continuous LOD here... fractional locations come away from the surface of the triangles that they interpolate and we see the resultant surface shimmer."*
> 即：**分数 LOD 会让顶点跑离三角形所在平面，产生表面闪烁**。除非改用重心坐标插值，否则「吸附到整级」反而是更正确的做法。

#### 两条 draw 的实例索引各从哪来

VHM 一帧有两次 `DrawIndexedInstancedIndirect`（不透明 + 洞），**共用同一个 VS**。VS 侧写的是：

```hlsl
// VirtualHeightfieldMeshVertexFactory.ush:48
uint InstanceId : SV_InstanceID;                     // 输入声明
// :121
const QuadRenderInstance Item = GetQuadRenderInstance(Input.InstanceId);
// :24
float4 Data = VHMInst.InstanceBuffer[InstanceId];    // 取到 16 B 的 QuadRenderInstance
```

**索引就是 `SV_InstanceID` 本身** —— 不减基址、不做判定、不查表。
「该读哪块 buffer」在 **CPU 侧就定死了**：两条 `FMeshBatch` 各自把一个**不同的 SRV** 塞进 per-batch 的 uniform buffer。

| 批次 | 间接参数偏移 | 绑定的实例缓冲 SRV | 出处 |
|---|---|---|---|
| 不透明 | `0` | `InstanceBufferSRV` | `VirtualHeightfieldMeshSceneProxy.cpp:1025`、`VirtualHeightfieldMeshSceneProxy.cpp:1037` |
| 洞 | `5 * sizeof(uint32)` | `HoleInstanceBufferSRV` | `VirtualHeightfieldMeshSceneProxy.cpp:1083`、`VirtualHeightfieldMeshSceneProxy.cpp:1096` |

`VHMInst` 对应的全局 uniform buffer 结构 `FVirtualHeightfieldMeshVertexFactoryParameters2` **只有一个成员**
（`VirtualHeightfieldMeshVertexFactory.h:40-42`）。两次 draw 各 `CreateUniformBufferImmediate` 出一份，
分别挂到自己 `FMeshBatchElement` 的 `UserData->InstantceBuf` 上
（`VirtualHeightfieldMeshSceneProxy.cpp:1038`、`VirtualHeightfieldMeshSceneProxy.cpp:1097`），
最后在顶点工厂里绑进 shader：

```cpp
// VirtualHeightfieldMeshVertexFactory.cpp:117
ShaderBindings.Add(Shader->GetUniformBufferParameter<FVirtualHeightfieldMeshVertexFactoryParameters2>(), UserData->InstantceBuf);
```

**同名参数、不同绑定** —— 所以 VS 里那句 `VHMInst.InstanceBuffer[InstanceId]`，在两次 draw 里指向两块不同的内存。

**为什么 `SV_InstanceID` 可以直接当下标**：因为两条 draw 的 `StartInstanceLocation` 恒为 0。
D3D 的 indirect 参数是五元组 `{IndexCountPerInstance, InstanceCount, StartIndexLocation, BaseVertexLocation, StartInstanceLocation}`，
初始化时直接写死：

```hlsl
// VirtualHeightfieldInitBuffers.usf:35-46
InstanceArgsBuffer[0] = VHMParam.NumIndices;   //  0 IndexCountPerInstance
InstanceArgsBuffer[1] = 0;                     //  1 InstanceCount（每帧由 CS 累加）
InstanceArgsBuffer[2] = 0;                     //  2 StartIndexLocation
InstanceArgsBuffer[3] = 0;                     //  3 BaseVertexLocation
InstanceArgsBuffer[4] = 0;                     //  4 StartInstanceLocation ← 恒 0
const int MaskQuadArgsOffset = 5;              //  洞那一份从 [5] 开始，同样 [9] = 0
```

CS 侧**只累加 InstanceCount，从不碰 `[4]`**：

```hlsl
// VirtualHeightfieldMesh3.usf:533-534（双缓冲版本同构，见 :548-549）
InterlockedAdd(InstanceArgsBuffer[s_IndirectDrawOffset],     QuadInstanceFlag[...], QuadInstanceOffset);
InterlockedAdd(InstanceArgsBuffer[5 + s_IndirectDrawOffset], HoleQuadInstanceFlag[...], HoleQuadInstanceOffset);
```

其中 `#define s_IndirectDrawOffset 1`（`VirtualHeightfieldMesh3.usf:12`）—— 所以写的是 `[1]`（不透明 InstanceCount）
和 `[6]`（洞 InstanceCount），正好落在五元组的第二项上。

于是 `SV_InstanceID` 从 0 连续递增到 `InstanceCount - 1`，**恰好就是各自 buffer 的下标**，两条 draw 互不干扰。

### 4.6 可选的 One-Pass 版本

> **先澄清命名**：OnePass **也是 V3 的一部分** —— 同一份 `VirtualHeightfieldMesh3.usf` 里由 `VHM_ONE_PASS` 宏切换的另一个分支，不是 V3 之外的版本。前面 §4.3 讲的是「串行多轮」分支。

`r.VHM.WithOnePass = 1` 时（默认 0），阶段③ 换成 `CollectQuadsOnePassCS`：把「N 次串行 dispatch」换成「**2 次 dispatch + GPU 内部 `while` 循环**」。

#### 核心区别：谁来驱动循环

| | 串行多轮（默认） | OnePass |
|---|---|---|
| 循环在哪 | **CPU** 的 `for (CalTime...)` | **GPU** shader 里的 `while (!bExit)` |
| dispatch 次数 | `MaxLevel - 3 + 1` 次（如 8 次） | **2 次**（固定） |
| 每次线程数 | IndirectDispatch，随层节点数变 | **固定 16 组 × 32 = 512 线程** |
| 队列结构 | ping-pong 双 buffer | **单个环形队列 + 原子游标** |
| 层间同步 | dispatch 边界（隐式 barrier） | `DeviceMemoryBarrier()` + 原子计数 |
| 线程生命周期 | 一层做完就退出 | **持久线程，抢任务直到队列干涸** |

#### 一次发多少线程：固定 512

```cpp
// VirtualHeightfieldMeshSceneProxy.cpp:2298
FComputeShaderUtils::Dispatch(RHICmdList, ComputeShader, *Parameters,
    FIntVector(CVarVHMCollectPassWavefronts.GetValueOnRenderThread(), 1, 1));
```

`r.VHM.CollectPassWavefronts` 默认 **16**（`VirtualHeightfieldMeshSceneProxy.cpp:110-115`），配合 `[numthreads(32,1,1)]` ⇒ **512 线程，跟树有多大完全无关**。

#### GPU 自循环的四个机制

**机制 1：`WorkerQueueInfo` —— 共享游标**（`VirtualHeightfieldMesh.ush:16-21`）

```hlsl
struct WorkerQueueInfo
{
    uint Read;      // 下一个待取任务的下标
    uint Write;     // 下一个可写入的位置
    int  NumActive; // 待处理任务数（带符号，可临时为负）
};
```

三个字段全靠原子访问，是 512 个线程之间唯一的通信渠道。

**机制 2：抢任务 —— 乐观占坑 + 失败回滚**（`VirtualHeightfieldMesh3.usf:667-685`）

```hlsl
InterlockedAdd(RWQueueInfo[0].NumActive, -1, NumActive);

if (NumActive <= 0 && !bExit)
{
    // No task pulled. Rewind.
    InterlockedAdd(RWQueueInfo[0].NumActive, 1, NumActive);
}
else if (!bExit)
{
    uint Read;
    InterlockedAdd(RWQueueInfo[0].Read, 1, Read);
    const uint4 ThisPackedData = OutSubdivideQuadBuffer[Read & VHMParam.OutBufferSizeMask];
    ...
}
```

原子返回的是**加之前的旧值**，所以先无条件减 1 占坑，旧值 ≤ 0 说明队列已空，马上加 1 还回去。`NumActive` 声明成 `int` 就是为了容纳这个瞬时负值。抢中的线程再递增 `Read` 拿槽位，`& OutBufferSizeMask` 做环形回绕（容量 `r.VHM.MaxPersistentQueueItems` 默认 64K，向上取整到 2 的幂，`VirtualHeightfieldMeshSceneProxy.cpp:2504`）。

**机制 3：产出任务 —— 先占位再写，最后才宣告**（`VirtualHeightfieldMesh3.usf:720-736`）

```hlsl
uint Write;
InterlockedAdd(RWQueueInfo[0].Write, 4, Write);   // 一次占 4 个连续槽位
[unroll]
for(int i = 0; i < 4; ++i)
{
    ...
    OutSubdivideQuadBuffer[(Write + i) & VHMParam.OutBufferSizeMask] = ChildPackData;
}
InterlockedAdd(RWQueueInfo[0].NumActive, 4, NumActive);   // 最后才宣告「可取」
```

**顺序关键**：先 `Write += 4` 拿独占区间 → 写数据 → 才 `NumActive += 4`。反过来的话，别的线程可能在数据还没落盘时就抢走槽位读到垃圾。

**机制 4：退出判定 —— 双条件**（`VirtualHeightfieldMesh3.usf:755-773`）

```hlsl
#if VHM_END_WITH_ONE_STEP
    // Exit if no work was found.
    if (NumActive > VHMParam.NumActiveForOnePassStep)
    {
        uint Dummy;
        InterlockedAdd(NumGroupExitRequest, 1, Dummy);
    }
#endif

DeviceMemoryBarrier();
if (NumGroupTasks == 0
#if VHM_END_WITH_ONE_STEP
    || NumGroupExitRequest > 0
#endif
)
{
    bExit = true;
}
```

| 退出条件 | 触发时机 |
|---|---|
| `NumGroupTasks == 0` | 本轮整组一个任务都没抢到 → 队列真空了 |
| `NumGroupExitRequest > 0` | 队列积压超过 `r.VHM.NumActiveForOnePassStep`（默认 **640**）→ **主动让位** |

> ⚠️ **`VirtualHeightfieldMesh3.usf:756` 那条注释 `// Exit if no work was found.` 与代码逻辑相反** —— 条件是 `NumActive > 640`，即「活太多了」才退出，不是「没活」。读代码时别被它带偏。

#### ⚠️ `DeviceMemoryBarrier()` 保护的不是 `NumGroupTasks`

这是最容易读错的一处。HLSL 的 barrier 分两个维度：

| | 只保证内存完成 | 内存 + 全组线程对齐 |
|---|---|---|
| **groupshared** | `GroupMemoryBarrier()` | `GroupMemoryBarrierWithGroupSync()` |
| **device（UAV/buffer）** | `DeviceMemoryBarrier()` | `DeviceMemoryBarrierWithGroupSync()` |
| 两者都管 | `AllMemoryBarrier()` | `AllMemoryBarrierWithGroupSync()` |

而 `NumGroupTasks` 是 **groupshared**（`VirtualHeightfieldMesh3.usf:640-643`）—— `DeviceMemoryBarrier()` **两个维度都不覆盖**。

同一个循环里的写法是不对称的。**顶部**用的是正确的 group sync（`VirtualHeightfieldMesh3.usf:656-662`）：

```hlsl
// Sync and init group task count.
NumGroupTasks = 0;
#if VHM_END_WITH_ONE_STEP
    NumGroupExitRequest = 0;
#endif
GroupMemoryBarrierWithGroupSync();      // ← groupshared + 全组对齐，标准做法
```

**底部**却只有 `DeviceMemoryBarrier()`。这不是笔误，是**刻意省掉的** —— 持久线程每轮都走一遍，几十轮下来省的就是几十次真同步。

那 `NumGroupTasks` 靠什么读对？靠 **wave lockstep**（见下）。`DeviceMemoryBarrier()` 真正在保护的是本轮所有 UAV 写入：

| 位置 | 操作 |
|---|---|
| `VirtualHeightfieldMesh3.usf:733` | `OutSubdivideQuadBuffer[...] = ChildPackData` 写子节点 |
| `VirtualHeightfieldMesh3.usf:736` | `InterlockedAdd(RWQueueInfo[0].NumActive, 4, ...)` 宣告可取 |
| `VirtualHeightfieldMesh3.usf:741-744` | `FinalDispatchArgsBuffer` / `FinalQuadBuffer` 写叶子 |

放在轮末的意义：下一轮开头就要抢任务（`VirtualHeightfieldMesh3.usf:667`），得确保本轮写进队列的数据真落地了。

#### `WAVESIZE(32)` 是什么

```hlsl
// VirtualHeightfieldMesh3.usf:644-647
#if COMPILER_SUPPORTS_WAVE_SIZE
    WAVESIZE(32)
#endif
[numthreads(COLL_THREAD_TOTAL, 1, 1)]
```

它不是提示，是**对硬件下的硬性要求：这个 CS 的 wave（SIMD 执行单元）宽度必须恰好是 32 个 lane**。三个平台各自落地：

| 平台 | 实现 | 出处 |
|---|---|---|
| **D3D12 / SM6** | `#define WAVESIZE(N) [WaveSize(N)]` —— HLSL 原生属性，直接进 DXIL | `D3DCommon.ush:15-18` |
| **Vulkan** | SPIR-V 无此属性，只能在 shader 留标记让编译器回读，最后转成管线创建时的 `VkPipelineShaderStageRequiredSubgroupSizeCreateInfo`；依赖 `VK_EXT_subgroup_size_control` | `VulkanCommon.ush:135-142`、`VulkanPipeline.cpp:1286`、`VulkanExtensions.cpp:1312-1330` |
| **其它** | `#define COMPILER_SUPPORTS_WAVE_SIZE 0` —— **宏整个消失** | `Platform.ush:131-134` |

**OnePass 为什么非要它**：`[numthreads(32)]` + `WAVESIZE(32)` ⇒ **group == 单个 wave**。一个 wave 内所有 lane 天然 lockstep，`InterlockedAdd(NumGroupTasks, 1, Dummy)`（`VirtualHeightfieldMesh3.usf:679`）虽在分支里，但 wave 内分支靠 lane mask 实现，**所有 lane 在同一指令槽通过那条原子指令**。等任何 lane 走到 765 行，累加早已落定 —— 这时 group sync 是多余的。

**所以这段代码的正确性不来自那个 barrier，来自 wave 宽度的硬约束。**

#### 为什么串行多轮不需要考虑 wave size

| | 串行多轮 | OnePass |
|---|---|---|
| 层间同步 | **dispatch 边界**（RDG 插的真 barrier，驱动保证） | shader 里的 `while` + 原子 |
| 组内同步 | `GroupMemoryBarrierWithGroupSync()`（`VirtualHeightfieldMesh3.usf:305`、`VirtualHeightfieldMesh3.usf:347`、`VirtualHeightfieldMesh3.usf:364`） | **靠 wave lockstep**，省掉了 |
| 对 wave 宽度的假设 | **无** | wave 必须 == 32 |

串行分支的 groupshared 用得也不少（`SubdivideQuadFlag[33]` / `FinalQuadFlag[33]` 做前缀和，`VirtualHeightfieldMesh3.usf:355-357`），但每次访问前后都有显式 barrier。**`GroupMemoryBarrierWithGroupSync()` 是标准 HLSL，任何 wave 宽度下都由硬件/驱动保证正确**，代价是一次真同步。

两条路是两种交易：
- **串行多轮**：用 8 次 dispatch + 每轮显式 group sync，买「任何硬件都对」
- **OnePass**：用 `WAVESIZE(32)` 换掉每轮的 group sync，代价是只能跑在 wave 可控且能设成 32 的硬件上

#### wave ≠ 32 时的真实竞态

先修正一个容易推测过头的结论。**wave=16 / group=32 并不会让 `NumGroupTasks == 0` 判据出错**：`NumGroupTasks` 是单调累加的，同一轮里后读的 wave 只会看到更大的值；而判据是 `== 0`，只有队列真空、全组都抢不到时才成立 —— 这种情况下每个 wave 读到的都是 0，不存在分歧。（已建模逐拍模拟验证：读值分歧确实出现，但没有一次导致错误退出或漏节点。）

**真有问题的是 `NumGroupExitRequest > 0` 这条**，它的判据不是 `== 0`：

| 时序 | waveA | waveB |
|---|---|---|
| waveA 先跑 | 此刻 `NumActive` = 300，**不投票** | — |
| waveA 读 `VirtualHeightfieldMesh3.usf:765` | `NumGroupExitRequest = 0` → **continue** | — |
| waveB 后跑 | — | `NumActive` 已涨到 700，**投票 +1** |
| waveB 读 `VirtualHeightfieldMesh3.usf:765` | — | `NumGroupExitRequest = 1` → **EXIT** |

**waveB 退出了，waveA 还在跑** —— 同一个 group 里 16 个 lane 结束、16 个继续。后果有两层：

1. **Pass 1 的「广度预热」目标落空**：设计意图是整组一起退出把队列留给 Pass 2，现在半组还在啃深度，可能把队列抽回 640 以下，Pass 2 启动时又回到低占用状态。
2. **下一轮的 group sync 前提被破坏**（硬伤）：轮顶 `NumGroupTasks = 0` 后跟着 `GroupMemoryBarrierWithGroupSync()`（`VirtualHeightfieldMesh3.usf:662`），该 barrier 要求**全组 32 线程都到达**。waveB 已退出循环，剩下的 waveA 在 group sync 上等一个永远不来的 wave。HLSL 规范对「部分线程已退出后的 group sync」是未定义行为，实际表现依驱动而定。

所以 `WAVESIZE(32)` 保护的不是队列完整性，是 **`bExit` 的组内一致性** —— 让 `bExit` 成为 wave 级的统一决策，从而保证轮顶那个 group sync 永远全组一起到达。

**Pass 2 反而不依赖它**（`bEndWithOneStep = false` 把那段编译掉，只剩 `== 0` 判据，安全）。这也解释了为什么两个 Pass 要用 permutation 区分，而不是共用一份代码。

#### 为什么要拆成两个 Pass

```cpp
// VirtualHeightfieldMeshSceneProxy.cpp:2924-2934
{
    RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_OnePassFirstPass");
    AddPass_CollectQuads_CS(..., EnableCull, /*bEndWithOneStep=*/true, WithFeedback);
}
{
    RDG_EXTRA_EVENT_SCOPE(GraphBuilder, "VHM_OnePassSecondPass");
    AddPass_CollectQuads_CS(..., EnableCull, /*bEndWithOneStep=*/false, WithFeedback);
}
```

差别只在 `bEndWithOneStep`，它编译进 `FWithEndWithOneStepDim` permutation（`VirtualHeightfieldMeshSceneProxy.cpp:2288`），也就是上面那个 `VHM_END_WITH_ONE_STEP` 宏。

**关键前提：退出是 group 级别且不可逆。** `NumGroupTasks` 是 groupshared，组内线程读到同一个值 ⇒ 要么整组继续、要么整组退出。一旦 `bExit = true`，`while` 终止、线程结束，**这个 group 在同一次 dispatch 内再也叫不回来**，哪怕队列后面涨到上万个任务。

**单 Pass 会怎样**：开局 `NumActive = 64`（阶段② 只播了 64 个种），512 个线程同时抢，恰好 64 个拿到正数算抢中。而 GPU 的原子请求按 wave 批量发，赢家会**按 wave 聚集** —— 64 个赢家大概率就是 2 个 group 吃满，其余 14 个 group 全员空手、当场退出：

| 迭代 | 队列任务数 | 存活 group | 有效线程 |
|---|---|---|---|
| 1 | 64 | 16 → **2**（14 组退出） | 512 → 64 |
| 2 | ~256 | 2 | 64 |
| 3 | ~1024 | 2 | 64 |
| … 深层 | 上万 | **仍然只有 2** | **64** |

**整个深层遍历只剩 12.5% 的并行宽度，而且回不去了。** 这才是单 Pass 的致命处 —— 不是浅层慢，是浅层把后面全拖死。

**两个 Pass 怎么修**：

- **Pass 1** 不在乎有多少 group 提前退出，任务只有一个：把队列养到 640 以上就主动让位。浅层节点本来就少，剩 2 个 group 也能干完。
- **Pass 2 是全新 dispatch，16 个 group 全部重新拉起**。此时队列有 640+ 个任务，而 **640 > 512**（16×32）—— 这个阈值是故意设在线程总数之上的：第一轮每个线程都能抢中，每组 `NumGroupTasks > 0`，**没有任何 group 提前退出**，512 线程满宽度进入深层。余量 128 是 25% 的安全垫。

> **注意**：拆 Pass **并没有消除浅层的原子争用** —— Pass 1 照样要走完那几轮低占用阶段。真正的收益在于**成本不对称**：

| 阶段 | 节点量级 | 占全部工作 | 低效的代价 |
|---|---|---|---|
| 浅层（`MaxLevel-3` ~ `-5`） | 64 + 256 ≈ 几百 | ~1% | **可以忍** |
| 深层 | 上万 | ~99% | **绝不能降宽度** |

**设计目标从来不是「消除低效阶段」，而是「别让低效阶段把并行宽度永久锁死」。** dispatch 边界是唯一能重新拉起已退出 group 的手段。

#### 阶段② 在 OnePass 下的额外工作

`FillLevel4QuadCS` 也有对应分支（`VirtualHeightfieldMesh3.usf:624-631`）：

```hlsl
#if VHM_ONE_PASS
    uint Read;
    InterlockedAdd(RWQueueInfo[0].NumActive, 1, Read);
    InterlockedAdd(RWQueueInfo[0].Write, 1, Read);
    OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;
#else
    OutSubdivideQuadBuffer[ThisThreadID] = ThisPackData;
#endif
```

OnePass 要额外初始化队列游标（64 个节点 ⇒ `NumActive = 64`、`Write = 64`）；串行模式靠 dispatch args 传节点数，不需要队列。

#### 为什么默认关掉

permutation 限 **SM6**（`VirtualHeightfieldMeshSceneProxy.cpp:1525-1527`）：

```cpp
static bool ShouldCompilePermutation(FGlobalShaderPermutationParameters const& Parameters)
{
    return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM6);
}
```

移动端根本不会编译它。CPU 侧那句被注释掉的判定也是同一件事的痕迹（`VirtualHeightfieldMeshSceneProxy.cpp:162-165`）：

```cpp
static bool GetVHMWithOnePass()
{
    return GVHMWithOnePass ;/*&& GMaxRHIFeatureLevel >= ERHIFeatureLevel::SM5;*/
}
```

移动端的硬件现实（`Engine/Config/Android/DataDrivenPlatformInfo.ini:135-137` vs `Engine/Config/Windows/DataDrivenPlatformInfo.ini:103-105`）：

```ini
; Android
bSupportsWaveOperations=RuntimeDependent     ; ← 编译期不能假设
MinimumWaveSize=4
MaximumWaveSize=128

; Windows
bSupportsWaveOperations=RuntimeGuaranteed    ; ← 编译期就能当真
```

Vulkan 侧真实值要等扩展初始化才知道（`VulkanExtensions.cpp:1349-1350`）：

```cpp
GRHIMinimumWaveSize = SubgroupSizeControlProperties.minSubgroupSize;
GRHIMaximumWaveSize = SubgroupSizeControlProperties.maxSubgroupSize;
```

`WAVESIZE(32)` 要生效需同时满足：① 设备支持 `VK_EXT_subgroup_size_control`；② 32 落在 `[minSubgroupSize, maxSubgroupSize]` 内。Adreno 原生 wave 是 64 或 128，Mali 是 4/8/16 —— 就算扩展在，能不能锁到 32 也是**逐设备的事**。

加上持久线程 + 全局原子自旋在移动 GPU 上开销大、`DeviceMemoryBarrier()` 在 tile-based 架构上代价更高，本项目是移动端，默认走串行多轮是合理的。

---

## 五、串起来看：一个 quad 的一生

前面分阶段讲完了，可能还是有点散。这张图把它们串成一条线：

```figure
lifecycle
```

**记住一条线索就够了**：从头到尾，这个 quad 携带的唯一「出处信息」就是 `PhysicalAddress` —— 它对应 VT 的哪个物理页。它在细分时由父节点继承，直到 VS 才被用来换算采样坐标。**这就是「几何跟着页表走」的字面含义。**

---

## 六、CPU 侧：谁在什么时候调度

CPU 这一侧其实很薄 —— 它**不决定画什么**，只负责把参数准备好、把 pass 挂上去。

### 6.1 两个挂载点

```cpp
// VirtualHeightfieldMeshSceneProxy.cpp:594-595
GEngine->GetPreRenderDelegateEx().AddRaw(this, &...::BeginFrame);
GEngine->GetPostRenderDelegateEx().AddRaw(this, &...::EndFrame);
```

对应 `DeferredShadingRenderer.cpp:2262` 和 `DeferredShadingRenderer.cpp:4434` —— 也就是**整个场景渲染的一头一尾**。

### 6.2 每帧的时序

| 时机 | 做什么 | 位置 |
|---|---|---|
| `GetDynamicMeshElements`（主视图收集期） | `AddWork()` 取/复用缓冲，`AddMesh` ×2 | `VirtualHeightfieldMeshSceneProxy.cpp:977` |
| `PreRenderDelegateEx` | `BeginFrame` → `SubmitWork_V3` 提交整条 CS 链 | `VirtualHeightfieldMeshSceneProxy.cpp:662` |
| `PostRenderDelegateEx` | `EndFrame` 回收缓冲池 | `VirtualHeightfieldMeshSceneProxy.cpp:714` |

> ⚠️ `GetDynamicMeshElements` 开头有个早退：
> ```cpp
> if (GVirtualHeightfieldMeshViewRendererExtension.IsInFrame()) { return; }   // :984
> ```
> `bInFrame` 从 PreRender 一直置位到 PostRender。**主视图的网格收集发生在 `BeginInitViews` 窗口内（早于 PreRender），所以不受影响**；但阴影等后续 pass 是否受影响，需要实测确认（见 §12.2）。

### 6.3 工作项与缓冲池

`FWorkDesc { ProxyIndex, MainViewIndex, CullViewIndex, BufferIndex }`（`VirtualHeightfieldMeshSceneProxy.cpp:557-563`），排序键：

```cpp
SortKey = (ProxyIndex << 24) | (MainViewIndex << 16) | (CullViewIndex << 8) | BufferIndex;
```

**为什么这样排**：`SubmitWork_V3` 的外层循环按 Proxy 聚合、内层按 View 聚合，同组的工作项能复用同一套 volatile 缓冲与 UB 填充 —— **排序把「重复计算」变成「复用」**。

缓冲池本身跨帧复用，4 帧未使用才释放（`VirtualHeightfieldMeshSceneProxy.cpp:636-645`、`VirtualHeightfieldMeshSceneProxy.cpp:730-744`）。

---

## 七、关键数学

### 7.1 LOD 距离函数

CPU 侧算好四个因子（`VirtualHeightfieldMeshSceneProxy.cpp:400-414`）：

```cpp
const float Lod0UVSize    = 1.f / (float)(1 << MaxLevel);
const float Lod0WorldSize = UVToWorldScale.XY * Lod0UVSize;
const float Lod0WorldRadius = Lod0WorldSize.Size();
const float ScreenMultiple  = max(0.5f * Proj[0][0], 0.5f * Proj[1][1]);
const float Lod0Distance    = Lod0WorldRadius * ScreenMultiple / Lod0ScreenSize;
return FVector4f(Lod0Distance, Lod0Distribution, LodDistribution, LodScale);
```

GPU 侧消费（`VirtualHeightfieldMesh.ush:120-126`）：

```hlsl
float ScaledDistance   = sqrt(InDistanceSq) * InLodFactors.w;                        // w = LodScale
float LodForDistance0  = saturate(ScaledDistance / (InLodFactors.x * InLodFactors.y));
float LodForDistanceN  = log2(1 + max((ScaledDistance / InLodFactors.x - InLodFactors.y), 0))
                       / log2(InLodFactors.z);
return LodForDistance0 + LodForDistanceN;
```

**符号取值来源**：
- `x = Lod0Distance`：LOD 0 对应的世界距离（由 UVToWorldScale 和投影矩阵推出）
- `y = Lod0Distribution`：LOD 0 段的距离扩展倍数（组件默认 1.0）
- `z = LodDistribution`：远处对数段的底数（组件默认 2.0，**必须 > 1**，否则 `log2(z)` 为 0 会除零 —— 这就是组件上 `ClampMin = 1.0` 的原因）
- `w = LodScale = ViewLodDistanceFactor × r.VHM.LodScale`，其中 `ViewLodDistanceFactor` 默认强制为 1

**近处线性、远处对数** —— 所以近处变化快、远处变化慢，符合屏幕像素分布。

### 7.2 LOD Bias（材质驱动的局部加密）

```hlsl
// VirtualHeightfieldMesh.ush:129-132
return max((InValue - 0.05f) * InLodBiasScale - 1.f, 0);
```

`LodBiasTexture` 在烘焙 MinMax 时一起生成（存的是高度差归一化值）。VS 里用 `clamp(LodForDistance - LodBias, Level, MaxLod)` 修正 —— **几何起伏大的地方自动加密**。

### 7.3 视锥 AABB 测试

`PlaneTestAABB`（`VirtualHeightfieldMesh.ush:188-205`）：对 5 个平面，取 AABB 在平面法线方向上「最远」的那个角，只要它落在任一平面外就剔除。

```hlsl
PlaneSigns = sign(InPlanes[PlaneIndex].xyz);          // 指向法线正方向的角
bInsidePlane = dot(plane, float4(center + extent * PlaneSigns, 1.0)) > 0;
```

> 注意 `extent` 按语义应该是**半长**，但 V3 传的是**全长** `UVExtern = UVMax - UVMin`（`VirtualHeightfieldMesh3.usf:256`）
> → 测试盒被放大 2 倍 → 偏保守（少剔除、不漏画）。这是个「安全方向」的取舍。

---

## 八、V1（Epic 原版）单独讲

前面七节讲的都是 V3。现在单独看 V1 —— **它和 V3 在数据结构、流程上都不同，唯一相同的是「要解决什么问题」。**

### 8.1 V1 唯一的根本差异：遍历方式

```figure
v1_walk
```

V1 的做法是**持久波（persistent wave）**：

- 全局只有一份队列状态 `WorkerQueueInfo { Read, Write, NumActive }`，三个数全用原子操作改。
- 队列本体是一个**环形缓冲**，用 `& (Size - 1)` 取模（`VirtualHeightfieldMesh.usf:137`）。
- 每个线程在一个 `while` 循环里：抢任务 → 处理 → 如果还要细分，就把 4 个子节点**压回同一个队列** → 直到队列真的空了才退出。
- 整个过程**只需要一次 dispatch**。

### 8.2 V1 的节点布局

```figure
v1_layout
```

### 8.3 V1 的其它不同点

| 项目 | V1 的做法 | 位置 |
|---|---|---|
| 起始 | 从单个根节点 `Level = MaxLevel` 出发 | `VirtualHeightfieldMesh.usf:87` |
| 实例打包 | `Pos.x \| Pos.y<<12 \| Level<<24`（12/12/8） | `VirtualHeightfieldMesh.usf:412` |
| 循环轮数 | 由队列长度自己决定，CPU 不知道 | —— |

### 8.4 V1 的反馈写入更「朴素」

V1 是每个线程各自做一次全局原子加（`VirtualHeightfieldMesh3.usf:202-221` 的非优化分支），而 V3 改成了整组合并一次。这是 V3 在 V1 基础上做的优化之一。

---

## 九、V1 与 V3 对比

现在两条线都讲完了，可以放在一起看：

```figure
cmp
```

| 维度 | **V1**（Epic 原版） | **V3**（本仓库默认） |
|---|---|---|
| Shader 文件 | `VirtualHeightfieldMesh.usf` | `VirtualHeightfieldMesh3.usf` |
| 遍历方式 | 持久波：1 次 dispatch，线程自己循环 | 串行：`MaxLevel + 1` 次 dispatch |
| 起点 | 单根节点 `Level = MaxLevel` | 64 个 `Level = MaxLevel−3` 的节点 |
| 节点打包 | `uint2`（12/12/8 分段） | `uint4`（14/14/4 分段） |
| 反馈原子操作 | 每线程一次 | 每组一次（降到 1/32） |
| 每轮工作量 | 不可预测 | 可预测 |
| 全局队列争用 | 有 | 无（改用每轮 args 传递） |
| 遮挡剔除（Occlusion） | 接了 `OcclusionTexture` | **未接**（`bOccludeCull = false` 硬编码，`VirtualHeightfieldMesh3.usf:237`） |
| Mask / 洞 | 有（本仓库后加） | 有 |

**该选哪个**：V3 是本仓库默认，因为它把「不可预测的 GPU 内循环」换成了「可预测的多次 dispatch」—— 更容易插剔除、更容易做统计、也更容易定位性能问题。代价是 dispatch 次数随地形规模增长；真嫌多可以临时开 `r.VHM.WithOnePass=1` 退回类似 V1 的形态。

---

## 十、本仓库（GR 分叉）的自研改动

标注体系：`#pragma region S1_Engine_Shiyu` / `Engine CYH` / `Engine ZXB`。

| 贡献者 | 改动 | 关键位置 |
|---|---|---|
| **Shiyu**（主力） | **Mask 纹理 + HoleMaterial 挖洞系统** | `HeightfieldMaskRender.usf` 全文件；`VirtualHeightfieldMeshComponent.h:53-76`；`VirtualHeightfieldMeshSceneProxy.cpp:813-824`、`VirtualHeightfieldMeshSceneProxy.cpp:1061-1120` |
| **Shiyu** | **VHM V3 流水线**（整份新增） | `VirtualHeightfieldMeshSceneProxy.cpp:1421-1612`、`VirtualHeightfieldMeshSceneProxy.cpp:2117-2436`、`VirtualHeightfieldMeshSceneProxy.cpp:2837-2972` |
| **Shiyu** | **LOD 全局调参 CVar** | `VirtualHeightfieldMeshSceneProxy.cpp:138-152` |
| **Shiyu** | **`NumQuadPerTileOfTwo` / `ExtSubdivisionLevel`** | `VirtualHeightfieldMeshComponent.h:115-122`；`VirtualHeightfieldMeshEnable.cpp:33-38` |
| **Shiyu** | **One-Pass 持久线程** | `VirtualHeightfieldMesh3.usf:648-776`；`VirtualHeightfieldMeshSceneProxy.cpp:154-173` |
| **Shiyu** | **GPU Stat 采集** | `VirtualHeightfieldMeshSceneProxy.cpp:182-212`、`VirtualHeightfieldMeshSceneProxy.cpp:2978-3047` |
| **CYH** | `r.VHM.Visualize`；**Nanite/VHM 自动互斥框架** | `VirtualHeightfieldMeshEnable.cpp:23-31`、`VirtualHeightfieldMeshEnable.cpp:43-70` |
| **ZXB** | **SM5 下不禁用 VHM**（一行关键 patch） | `VirtualHeightfieldMeshEnable.cpp:51-53` |
| **JLP** | `GR_SHOULD_CACHE_VF` —— VF permutation 缓存标记 | `VirtualHeightfieldMeshVertexFactory.h:93` |

### 10.1 移动端能被用起来的关键

`VirtualHeightfieldMeshEnable.cpp` 里的这段逻辑值得单独说：

```cpp
// 非 Editor 构建：Nanite 开着就自动关 VHM
bNaniteEnabled = (r.Nanite != 0) && (landscape.RenderNanite != 0);
#pragma region Engine ZXB for VHM and Nanite auto switch for SM5
bNaniteEnabled &= GMaxRHIFeatureLevel > ERHIFeatureLevel::SM5;      // :52
#pragma endregion
```

在移动端（ES3.1 / SM5）上 `GMaxRHIFeatureLevel > SM5` 为 **false**，于是 `bNaniteEnabled` 被强制置 false，sink 就会自动把 `r.VHM.Enable` 打开。

**这就是「手机上 VHM 能用」的前提** —— Nanite 在移动端跑不了，所以让 VHM 顶上。

> 另外注意：`r.VHM.Enable` 默认值是 **0**，但它会被这个 sink 在运行时改写（`VirtualHeightfieldMeshEnable.cpp:55-68`）。**别只看默认值就判断「没开」。**

---

## 十一、已做的优化总结

### 11.1 Epic 原始设计的优化点

| # | 优化 | 为什么有效 | 位置 |
|---|---|---|---|
| 1 | **几何量由 VT 驻留量驱动** | 显存里只有 N 页，GPU 就只画 N 个 quad —— 几何复杂度与地形总规模解耦 | 全程 |
| 2 | **MinMax 金字塔做保守剔除** | 一个 quad 是否可见只需 2 个 float 的 AABB，O(1) 判定 | `VirtualHeightfieldMesh.ush:173-179`、`VirtualHeightfieldMesh.ush:188-205` |
| 3 | **无顶点/索引缓冲的实例化** | 顶点由 `SV_VertexID` 现算，索引缓冲全局共享一份；每实例仅 16 字节 | `VirtualHeightfieldMeshVertexFactory.ush:7`、`VirtualHeightfieldMeshVertexFactory.ush:128-129` |
| 4 | **CDLOD 顶点 morph** | 消除 LOD 切换的几何跳变 | `VirtualHeightfieldMeshVertexFactory.ush:86-101` |
| 5 | **持久波工作队列**（V1） | 遍历工作量未知也能一次 dispatch 跑完，无需 CPU 回读 | `VirtualHeightfieldMesh.usf:137` |
| 6 | **`DrawIndexedInstancedIndirect`** | CPU 完全不知道实例数，无同步点 | `VirtualHeightfieldMeshSceneProxy.cpp:1024` |
| 7 | **VS 内实时算 LOD** | 不必逐实例从 CPU 传 LOD；顺带支持 FreezeRendering | `VirtualHeightfieldMeshVertexFactory.ush:170-191` |
| 8 | **视锥平面预变换到 UV 空间** | 每帧每视图只变换 5 个平面，而不是每 quad 变换一次 | `VirtualHeightfieldMeshSceneProxy.cpp:2527-2537` |

### 11.2 本仓库新增的优化

| # | 优化 | 收益 | 位置 |
|---|---|---|---|
| 1 | **V3 起点播种 `MaxLevel−3`** | 省掉最粗 3 轮的完整 dispatch | `VirtualHeightfieldMesh3.usf:600-632` |
| 2 | **直接算组内活跃线程数** | 替代逐线程 `InterlockedMax`，减少原子争用 | `VirtualHeightfieldMesh3.usf:294-296` |
| 3 | **反馈写入按组合并** | 全局原子操作数降到 1/32 | `VirtualHeightfieldMesh3.usf:411`、`VirtualHeightfieldMesh3.usf:419-426` |
| 4 | **VT 反馈全帧合并成一次提交** | V1 是每个 MainView 一次；V3 所有 work 共用一个 buffer | `VirtualHeightfieldMeshSceneProxy.cpp:2855-2862`、`VirtualHeightfieldMeshSceneProxy.cpp:2963-2971` |
| 5 | **Mask 分流 + 双 IndirectDraw** | 剔除在 CS 里做，避免 PS 阶段 discard（移动端 TBDR 上会破坏 HSR） | `VirtualHeightfieldMesh3.usf:509-519` |
| 6 | **双缓冲 Args / Quad 缓冲** | 读写分离，避免同一 pass 内 RAW 冲突 | `VirtualHeightfieldMeshSceneProxy.cpp:1861-1872`、`VirtualHeightfieldMeshSceneProxy.cpp:2221-2236` |
| 7 | **One-Pass 持久线程 + 提前退出** | 省 N 次 dispatch；尾部主动收敛 | `VirtualHeightfieldMesh3.usf:755-762` |
| 8 | **缓冲池跨帧复用 + 4 帧老化** | 避免每帧重建 GPU 缓冲 | `VirtualHeightfieldMeshSceneProxy.cpp:636-645`、`VirtualHeightfieldMeshSceneProxy.cpp:730-744` |
| 9 | **`ExtSubdivisionLevel`：几何可细于纹理** | 同物理页服务多个几何 quad，不增加 VT 内存 | `VirtualHeightfieldMesh3.usf:86-88` |
| 10 | **全局 LOD 调参 CVar** | 不改资产就能全局调 LOD 分布 | `VirtualHeightfieldMeshSceneProxy.cpp:138-152` |

### 11.3 性能观测

开 `r.VHM.StatEnable=1` 后（需 `VHM_ENABLE_STAT=1` 编译；该宏在 `VirtualHeightfieldMeshSceneProxy.cpp:176-181`，**本工作区已打开为 1**，见 `VirtualHeightfieldMeshSceneProxy.cpp:179-180`）可读回：

| Stat | 含义 |
|---|---|
| `VHM.BeforeCullInstances` | 剔除前实例总数 |
| `VHM.DrawInstances-ALL` | 剔除后绘制实例数 |
| `VHM.DrawInstances-Opacity` / `-Mask` | 主材质 / 洞材质各自的实例数 |
| `VHM.DrawInstances-LOD0..9` | 各 LOD 层的实例分布 |
| `VHM.DrawTriangles` | `实例数 × NumInstanceVertexSide² × 6 / 3` |

> **行号基线说明**：本文档中 `VirtualHeightfieldMeshSceneProxy.cpp` 的行号对齐的是**本工作区的当前版本（3053 行）**——该文件第 179-180 行被本地加了一行 `// [ZXB]` 注释并打开了 `VHM_ENABLE_STAT`，使第 176 行之后的代码整体下移 1 行。若你读的是 depot 版本（3052 行），第 176 行之后的行号需各减 1。

---

## 十二、已知问题与待确认

### 12.1 ⚠️ `UnPackMinMaxHeight` 与 `PackMinMax` 互不可逆（Min/Max 互换）

这是**纯本地源码即可复现**的不一致：

| 端 | 位置 | 表达式 |
|---|---|---|
| 打包 | `HeightfieldMinMaxRender.usf:12-21` | `.x=Max>>8, .y=Min&0xff, .z=Min>>8, .w=Max&0xff` |
| 逆运算（正确） | `HeightfieldMinMaxRender.usf:23-29` | `( .z<<8 \| .y, .x<<8 \| .w )` → **(Min, Max)** |
| 解包（**不一致**） | `VirtualHeightfieldMesh.ush:173-179` | `( .x<<8 \| .y, .z<<8 \| .w )` |

**数值验证**（Min = 0.25，Max = 0.75）：

```
PackMinMax:   MinU = 0x3FFF(16383)，MaxU = 0xBFFF(49151)
              → RGBA8 = (191, 255, 63, 255)

UnPackMinMaxHeight:  (191<<8|255, 63<<8|255) = (49151, 16383) = (0.750, 0.250)
                     ↑ 返回的是 (≈Max, ≈Min)，与调用方期望的 (Min, Max) 相反

UnPackMinMax（.usf）:(63<<8|255, 191<<8|255) = (16383, 49151) = (0.250, 0.750)  ✅ 正确
```

**影响面**：`UnPackMinMaxHeight` 的 3 个调用点全部把返回值当作 `(Min, Max)` 用：
- `VirtualHeightfieldMesh3.usf:252-254`
- `VirtualHeightfieldMesh.usf:200-202`、`VirtualHeightfieldMesh.usf:384-386`

于是 AABB 的 Z 区间被反转（zmin ≈ Max > zmax ≈ Min）。`UVCenter` 仍然正确（中点对称），但 `UVExtern.z` 变成负值，`PlaneTestAABB`（`VirtualHeightfieldMesh.ush:200`）测的其实是 **Z 方向镜像的那只角**。

**待确认**：(a) 这是否是引入通道打乱时漏改的遗漏；(b) 实际影响是「多剔除」（地形缺块）还是「少剔除」（浪费），需实测 —— 可对比 `VHM.DrawInstances-ALL` 与相机到视锥边界的距离。

> 若确认是缺陷，最小修复是把 `VirtualHeightfieldMesh.ush:176` 改为 `.z << 8 | .y` 与 `.x << 8 | .w`，与 `HeightfieldMinMaxRender.usf:26` 对齐。

### 12.2 阴影路径需要实测确认

`GetDynamicMeshElements` 的早退由 `bInFrame` 控制，而 `bInFrame` 覆盖 `DeferredShadingRenderer.cpp:2262` 到 `DeferredShadingRenderer.cpp:4434` 的整段。

- **确定**：主视图的网格收集在 `BeginInitViews` 窗口内（早于 2262），不受影响。
- **不确定**：阴影视图的收集时机。`FinishInitDynamicShadows` 在 `DeferredShadingRenderer.cpp:2708`（晚于 2262），但阴影视图是作为 culling view 注册的，收集可能仍在 `BeginInitViews` 内完成。

代码注释（`VirtualHeightfieldMeshSceneProxy.cpp:986-989`）明说 UE5.0 时阴影会被挡掉。**本仓 5.5.4 是否仍如此，建议实测**：RenderDoc 抓一帧，在 ShadowDepth pass 里搜有没有 `VirtualHeightfieldMesh` 的 draw。

### 12.3 其它观察

| 项 | 说明 | 位置 |
|---|---|---|
| `VHM_CollectQuad.usf` 是**死文件** | 插件 `Source/` 与 `IMPLEMENT_GLOBAL_SHADER` 列表中均无引用 | `Shaders/Private/VHM_CollectQuad.usf` |
| V3 无遮挡剔除 | `bOccludeCull = false` 硬编码 | `VirtualHeightfieldMesh3.usf:237` |
| V3 视锥测试盒偏大 2 倍 | 传全长而非半长，方向保守 | `VirtualHeightfieldMesh3.usf:256` |
| 容量掩码是「回绕覆盖」而非「丢弃」 | `& BufferSizeMask` 溢出时会覆盖已有数据，容量必须开够 | `VirtualHeightfieldMesh3.usf:448`、`VirtualHeightfieldMesh3.usf:453` |
| 洞实例缓冲容量只有主缓冲的 1/4 | 注释："hold instance just little" | `VirtualHeightfieldMeshSceneProxy.cpp:1802` |
| `VirtualHeightfieldMeshSetting` 配置段**悬空** | `S1Game/Config/DefaultVirtualHeightfieldMesh.ini` 引用的该类在引擎与工程源码中均搜不到 | 全仓搜索无结果 |

---

## 十三、CVar 速查

| CVar | 默认 | 作用 | 位置 |
|---|---|---|---|
| `r.VHM.Enable` | 0 | 总开关（运行时会被 Nanite 逻辑自动改写） | `VirtualHeightfieldMeshEnable.cpp:16-21` |
| `r.VHM.Visualize` | 1 | 调试用可视化开关 | `VirtualHeightfieldMeshEnable.cpp:25-31` |
| `r.VHM.EnableExtSubdivisionLevel` | 0 | 允许几何细于纹理 | `VirtualHeightfieldMeshEnable.cpp:33-38` |
| `r.VHM.Version` | **3** | 1 = V1 流水线；其它 = V3 | `VirtualHeightfieldMeshSceneProxy.cpp:117-122` |
| `r.VHM.UseAsyncCompute` | 1（工程改 **0**） | 计算 pass 走 AsyncCompute | `VirtualHeightfieldMeshSceneProxy.cpp:46-52` |
| `r.VHM.WithOnePass` | 0 | 启用 One-Pass 持久线程遍历 | `VirtualHeightfieldMeshSceneProxy.cpp:154-160` |
| `r.VHM.NumActiveForOnePassStep` | 640 | One-Pass 尾部收敛阈值 | `VirtualHeightfieldMeshSceneProxy.cpp:167-173` |
| `r.VHM.DisableCull` | 0 | 关闭剔除（调试） | `VirtualHeightfieldMeshSceneProxy.cpp:131-136` |
| `r.VHM.CloseMorphVertexForDebug` | 0 | 关闭顶点 morph | `VirtualHeightfieldMeshSceneProxy.cpp:124-129` |
| `r.VHM.LodScale` | 1.0 | 全局 LOD 缩放 | `VirtualHeightfieldMeshSceneProxy.cpp:62-67` |
| `r.VHM.AddLodDistribution` | 0（工程 **-0.3**） | LOD 分布全局偏移 | `VirtualHeightfieldMeshSceneProxy.cpp:138-144` |
| `r.VHM.AddLod0LevelBias` | 0（工程 **2**） | Lod0 层级偏置 | `VirtualHeightfieldMeshSceneProxy.cpp:146-152` |
| `r.VHM.EnableViewLodFactor` | 0 | 是否乘 `View.LODDistanceFactor`（默认关，防 FOV 双计） | `VirtualHeightfieldMeshSceneProxy.cpp:73-80` |
| `r.VHM.Occlusion` | 1 | 硬件遮挡查询（V1 生效） | `VirtualHeightfieldMeshSceneProxy.cpp:82-87` |
| `r.VHM.MaxRenderInstances` | 65536（工程 **262144**） | 实例 / quad 缓冲容量 | `VirtualHeightfieldMeshSceneProxy.cpp:89-94` |
| `r.VHM.MaxFeedbackItems` | 40960（工程 **81920**） | VT 反馈缓冲容量 | `VirtualHeightfieldMeshSceneProxy.cpp:96-101` |
| `r.VHM.MaxPersistentQueueItems` | 65536（工程 **262144**） | 工作队列容量（**必须是 2 的幂**） | `VirtualHeightfieldMeshSceneProxy.cpp:103-108` |
| `r.VHM.CollectPassWavefronts` | 16 | One-Pass 的 wavefront 数 | `VirtualHeightfieldMeshSceneProxy.cpp:110-115` |
| `r.VHM.StatEnable` | 0 | GPU Stat 采集 | `VirtualHeightfieldMeshSceneProxy.cpp:206-211` |
| `r.VHM.CaptureBuildTexture` | 0 | 构建时挂 RenderDoc 捕获 | `HeightfieldMinMaxTextureBuild.cpp:22-28` |

---

## 十四、网络资料勘误

| # | 网络说法 | 本地核实结果 |
|---|---|---|
| 1 | "VHM 依赖 WorldHeight 类型的 RVT" | ✅ **准确**。不满足则完全不创建 VF（`VirtualHeightfieldMeshSceneProxy.cpp:870`） |
| 2 | "VHM 在顶点着色器采样 height RVT 置换几何" | ✅ **准确**（`VirtualHeightfieldMeshVertexFactory.ush:208-214`） |
| 3 | "GPU Driven Pipeline，构建 Indirect Args 后以 Instance 绘制" | ✅ **准确**（`VirtualHeightfieldMeshSceneProxy.cpp:1024`） |
| 4 | "只做 Z 轴位移、无碰撞；碰撞由 Landscape 提供" | ✅ **准确**。`WorldNormal = float3(0,0,1)`，插件无碰撞代码 |
| 5 | "需在项目设置开启 virtual texture support" | ✅ **准确**（`VirtualHeightfieldMeshEnable.cpp:132`） |
| 6 | "官方说法将替代传统 landscape rendering" | ⚠️ **属旧版本口径**。本仓库已改为 **VHM 与 Nanite 按平台互斥**（`VirtualHeightfieldMeshEnable.cpp:44-68`） |
| 7 | 论坛帖："UE 5.8 VHM 的 LOD popping 是因为 `VirtualHeightfieldMesh.usf` 未同步 `VirtualTextureFeedbackBias`" | ❌ **对本仓库不适用**。本仓为 **UE 5.5.4**（CL 1128341），`Engine/Shaders/Shared/VirtualTextureDefinitions.h` **不存在**，`VirtualTextureFeedbackBias` **全仓无定义**。因此这里用 `LevelPlusOne = SampleTextureLevel + 1`（`VirtualHeightfieldMesh3.usf:213`）是**正确**的。**若日后同步到 5.8+ 引擎，此处必须一并处理。** |
| 8 | "四叉树用 indirection texture 烘焙、动态调整 sector 大小" | ❌ **张冠李戴**。该描述来自 *Advances in Real-Time Rendering 2023*（ATVI《Large Scale Terrain Rendering》），讲的是该商用引擎自己的方案。UE VHM **没有** indirection texture 的逐帧 delta 更新；它是每帧重新遍历 RVT 页表现场构建四叉树。 |
| 9 | 部分中文博文称 VHM 内有"LRU / free / locked 页链表、每帧算四叉树 delta" | ❌ **不成立**。这些是 **UE 通用 Virtual Texture 系统**的机制，VHM **只是消费者**，自身代码里没有任何页表管理/淘汰逻辑。 |
| 10 | 各类文章提到的 `r.VHM.*` CVar | ⚠️ **需以本仓为准**。本仓比公开资料多出自研 CVar：`r.VHM.Version`、`r.VHM.WithOnePass`、`r.VHM.NumActiveForOnePassStep`、`r.VHM.DisableCull`、`r.VHM.AddLodDistribution`、`r.VHM.AddLod0LevelBias`、`r.VHM.Visualize`、`r.VHM.EnableExtSubdivisionLevel`、`r.VHM.StatEnable`。 |

---

## 十五、一页总结

```
   输入                       V3 每帧五个阶段                         产出
  ─────────────────────────────────────────────────────────────────────────
  页表 / 物理页 ──┐
  MinMax 纹理   ──┼──► ① InitAllBuffersCS ──► 清零
  Mask 纹理     ──┤            │
                  │            ▼
                  └──► ② FillLevel4QuadCS ──► 64 个根节点（8×8）
                               │
                               ▼
                     ③ CollectSubdivideQuadsCS × (MaxLevel−2) 轮
                        ┌─ 够细了 → 收成叶子 → FinalQuadBuffer
                        └─ 还要更细 → 拆 4 子节点 → 下一轮
                               │
                               ▼
                     ④ CullQuadsAndGenerateInstancesCS
                        ├─ Mask == 1 → QuadInstanceBuffer（主材质）
                        └─ 其它      → HoleQuadInstanceBuffer（洞材质）
                               │
                               ▼
                     ⑤ DrawIndexedInstancedIndirect ×2
                        └─ VS：SV_VertexID 现算顶点 + 采高度 + Morph
```

**三句话记住 VHM**：
1. **几何来源是页表，不是 Mesh** —— 画什么由 VT 已驻留的页决定。
2. **LOD 是「边遍历边细分」** —— 从 64 个粗 quad 出发，够近就分裂。
3. **CPU 只出参数，GPU 决定一切** —— 实例数、剔除、LOD 全在 CS 里定。

**本仓库要特别留意的三点**：
- VHM 在移动端是 **Nanite 的替代**，`VirtualHeightfieldMeshEnable.cpp:51-53` 是「SM5 不禁用 VHM」的关键。
- **Mask / Hole 是自研能力**，公开资料里没有。
- §12.1 的 **MinMax 解包互换**是本地可复现的不一致，建议实测确认影响面。

---

## 十六、参考

- 本地源码：`D:\GR_DevTest\UE5EA\Engine\Plugins\Experimental\VirtualHeightfieldMesh\`（UE 5.5.4 / CL 1128341）
- [UVirtualHeightfieldMeshComponent — Epic 官方 API 文档](https://dev.epicgames.com/documentation/unreal-engine/API/Plugins/VirtualHeightfieldMesh/UVirtualHeightfieldMeshComponent)
- [UHeightfieldMinMaxTexture — Epic 官方 API 文档](https://dev.epicgames.com/documentation/unreal-engine/API/Plugins/VirtualHeightfieldMesh/UHeightfieldMinMaxTexture)
- [UE 5.8: VirtualHeightfieldMesh terrain LOD popping（Epic 开发者社区）](https://forums.unrealengine.com/t/ue-5-8-virtualheightfieldmesh-terrain-lod-popping-cause-and-fix/2737099)（**仅适用 5.8+**，见 §14 第 7 条）
- [UE5 VirtualHeightfieldMesh 简述（知乎）](https://zhuanlan.zhihu.com/p/575398476)
- [Virtual Texture for everything（知乎）](https://zhuanlan.zhihu.com/p/567526654)
- [Advances in Real-Time Rendering 2023 — Large Scale Terrain Rendering（ATVI）](https://advances.realtimerendering.com/s2023/Etienne(ATVI)-Large%20Scale%20Terrain%20Rendering%20with%20notes%20(Advances%202023).pdf)（**与 UE VHM 非同一方案**，见 §14 第 8 条）

---

*文档生成时间：2026-09-24　·　核对基线 UE 5.5.4 / CL 1128341 @ P4 workspace `DJANGOZHAN-PCFW_GR_DevTest`*
