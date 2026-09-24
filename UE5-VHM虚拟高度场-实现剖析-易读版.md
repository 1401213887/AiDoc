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

**这里有个关键的推论**：既然几何是「照着页表生成」的，那么 **没被加载的页，连几何都不会存在**。地形的精细程度本质上由「VT 流送了哪些页」决定，而不是由某个 LOD 参数直接决定。

页表由 RVT 系统负责流送和淘汰；VHM 只做两件事：**读页表**、**写反馈**（告诉 VT 系统「这段地形我需要第几级页」）。

### 2.2 MinMax 金字塔 —— 为什么需要它

剔除时要知道「这个 quad 的高度范围是多少」。如果每次都去采整块高度图，代价太大。

于是离线烘焙一张 **MinMax 纹理**：每个格子只存两个数 —— 这块地的**最低点**和**最高点**。

```figure
minmax
```

有了 `[min, max]`，一个 quad 就是一个立体的 AABB，做视锥剔除只需要 O(1) 的判断，**不用采一次高度图**。

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

### 4.5 阶段⑤ 光栅化 —— 顶点是从哪来的

**VHM 没有顶点缓冲。** 整个 VF 是自建的，`InitRHI()` 里只挂了一个 `VertexBuffer = nullptr, Stride = 0` 的空顶点流，元素声明列表为空（`VirtualHeightfieldMeshVertexFactory.cpp:153-179`）。

```figure
vs
```

**网格怎么来**：`VertexCoord = (VertexId % GRID, VertexId / GRID)`，其中 `GRID = NumInstanceVertexSide + 1`。也就是说，**一个实例 = 一个 GRID×GRID 的规则网格**，顶点坐标全由 `SV_VertexID` 算出。索引缓冲所有实例共享一份。

**高度怎么来**：查两次页表（`floor(SampleLevel)` 和 `ceil(SampleLevel)`），各采一次高度，再按 `frac` 插值（`VirtualHeightfieldMeshVertexFactory.ush:208-214`）。因为物理页纹理只有 level 0，**没有硬件 mip 链，过渡必须手工做**。

**顶点 Morph（消除 LOD 跳变）**：VS 里先采一次高度估算距离，算出应该处的 LOD，然后把顶点 UV 往粗网格方向「吸附」（`VirtualHeightfieldMeshVertexFactory.ush:86-101`）。

> 源码里有一条很重要的设计注释（`VirtualHeightfieldMeshVertexFactory.ush:184-187`）：
> *"Removing fractional continuous LOD here... fractional locations come away from the surface of the triangles that they interpolate and we see the resultant surface shimmer."*
> 即：**分数 LOD 会让顶点跑离三角形所在平面，产生表面闪烁**。除非改用重心坐标插值，否则「吸附到整级」反而是更正确的做法。

### 4.6 可选的 One-Pass 版本

`r.VHM.WithOnePass = 1` 时（默认 0），阶段③ 换成 `CollectQuadsOnePassCS`：把「N 次串行 dispatch」换成「一次 dispatch + GPU 内部循环」—— 这和 V1 的做法是一样的（见 §8）。

它还有个尾部优化：剩余任务数 ≤ `r.VHM.NumActiveForOnePassStep`（默认 640）时主动请求退出，避免最后几个任务时大量线程空转（`VirtualHeightfieldMesh3.usf:755-762`）。

> 注意这个 permutation 限 **SM6**（`VirtualHeightfieldMeshSceneProxy.cpp:1527`），移动端不会编译它。

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
