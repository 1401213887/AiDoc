# UE5 RVT 烘焙 OOM 全量排查 — 本地 12 GB 显存 · 构建机 SVT 内存峰值 · VHM GPU 卡死 · 失败假设复盘

> 仓库 `F:\GR_Release_F` · 引擎 `UE5EA`（UE 5.5 分叉）· 场景 `/Game/Maps/Nord/NordLand` · 构建机 `release_BuildSVTVHM`（63.70 GiB 物理内存）· 本地 RTX 4070 SUPER 12 GB · 排查跨度 2026-10-27 12:14 → 10-29 17:58（23 轮提问 / 5 次上下文压缩）· 每条结论附 `文件:行号` 或日志原文

**摘要：** 同一份代码、同一批资产，在三种内存约束下死在三个完全不相干的位置。**构建机 SVT**：mip 链累计 4,095 MiB 后，`FinalPixelsAllLayers` 无预留地长到 4,095 MiB，UE 的 `TArray` 增长因子是 3/8 + 16 B，于是**一次性索要 5,630.6 MiB 连续块** —— 日志里的 `5904138256` 正是 `4,095 MiB × 11/8 + 16`，逐字节对上；加精确 `Reserve` 后峰值**15,006 → 8,190 MiB**，第二份构建机日志确认该笔分配消失、提交 `CL 1230855`。**构建机 VHM**：GPU 在 `EndUpdateTexture3D` 上超时 5 秒、随后 18 分钟零日志，进程 commit 涨到 **737.18 GiB**（WorkingSet 仅 6.66 GiB），OOM 是后果不是原因；日志同期三次警告驱动过期（`560.94` → `610.47`）。**本地 12 GB 单卡**：只读纹理 **12.3 GB > 整卡 12,282 MiB** —— 物理缺口 3.4 GB，11 轮实验证明改配置无用（峰值恒在 11.3~11.7 GB、与是否崩溃无关），最终靠 `r.Streaming.BakeMipBias 1`（**排除地形**）降需求侧跑通，4/7 产物像素零差异、2/7 真实偏差 ≤1.06%。附 15 条被推翻的假设与 23 轮编年史，供后续排查避开同样的弯路。

**目录**：1 结论与全景 · 2 三个失败现场 · 3 构建机 SVT 根因 · 4 构建机 SVT 修复与验证 · 5 构建机 VHM 根因 · 6 本地 12 GB 与 11 轮实验 · 7 本地方案与验收 · 8 落地清单 · 9 已知问题 · 10 附录 A 被推翻的假设 · 11 附录 B 编年史 · 12 附录 C 方法论 · 13 参考


## 1. 结论与全景

> 三条失败线的根因互不相干。任何把它们当成同一个问题来修的做法都会失败 —— 这是本次排查最贵的一条教训（11 次无效实验、3 次结论被推翻都是因为它）。

- **构建机 SVT（已修 · 已验证）**：内存申请方式错了。合并缓冲无预留，在 4,095 MiB 的目标尺寸上按 11/8 索要一块 **5,630.6 MiB 连续块**，而当时进程已用 44.52 GiB / 63.70 GiB。
- **构建机 VHM（未修完 · 根因在引擎外）**：GPU 先卡死 5 秒，之后 18 分钟零日志，commit 一路涨到顶满系统 commit limit，最后连 45 MiB 都申请不到。**OOM 是等待的后果。**
- **本地 12 GB 单卡（已解决 · 已 shelve）**：只读纹理 12.3 GB 超过整卡容量 —— 物理极限，只能降需求侧（`r.Streaming.BakeMipBias 1`，且必须排除地形纹理）。

```
                        ┌─ 失败现场 ────────────────┬─ 处置 ──────────────────┬─ 状态 ──────────┐
① SVT 进程 / 构建机     │ Ran out of memory          │ 逐 mip 精确 Reserve      │ ✓ 已验证        │
   63.70 GiB            │ allocating 5904138256      │ + const& 遍历            │ CL 1230855      │
                        │ (5,630.6 MiB) 合并缓冲无预留│ + 单层跳过合并           │ 7 产物入库      │
② VHM 进程 / 构建机     │ GPU timeout 5.000 s        │ 驱动 560.94 → 610.47     │ ⚠ 未验证        │
   63.70 GiB            │ EndUpdateTexture3D 卡死    │ + VHM 补 GC 开关         │ 编译 exit=0     │
                        │ → commit 737.18 GiB        │                          │ 构建机未重跑    │
                        │ → OOM 45 MiB               │                          │                 │
① SVT 进程 / 本地       │ DXGI_ERROR_DEVICE_RESET    │ r.Streaming.BakeMipBias 1│ ✓ 11 轮后跑通   │
   12 GB 单卡           │ 只读纹理 12.3 GB           │ （排除地形）+ 5 项配套    │ CL 1229056      │
                        │ > 整卡 12,282 MiB          │                          │ 已 shelve       │
                        └───────────────────────────┴─────────────────────────┴─────────────────┘

三条线的根因互不相干：同一份代码、同一批资产，在不同内存约束下死在三个完全不同的位置。
```

*图 1　三条失败线：同一份代码、三种内存约束、三个不同的死法*

**贯穿主线**：本文跟着一次完整的 `BuildSVTVHM3.bat` 走 —— `16:32:34` 起 SVT 进程 → `17:19:22` 提交 7 个产物 → `17:19:24` 起 VHM 进程 → `17:53:20` 崩。第 3~4 节只讲 SVT（构建机主线），第 5 节讲 VHM，第 6~7 节单独讲本地那台 12 GB 机器。第 10~12 节是附录：被推翻的假设、编年史、方法与坏习惯。


> **正文只保留最终根因与有效方案**；所有失败实验、错误归因集中在附录 A（15 条）与附录 B（23 轮编年史）。这是按用户要求「全量」保留，正常阅读可跳过附录。


## 2. 三个失败现场

> 把三种签名并排放，才能看出它们没有共同根因 —— 差别不只是「哪台机器」，而是失败发生在完全不同的阶段、不同的资源维度上。

| 维度 | 本地（12 GB 单卡） | 构建机 SVT 进程 | 构建机 VHM 进程 |
|---|---|---|---|
| 失败类型 | GPU 显存耗尽 | CPU 进程内存耗尽 | CPU 进程内存耗尽（GPU 卡死的后果） |
| 上报签名 | `DXGI_ERROR_DEVICE_RESET` 887A0007 | `Ran out of memory allocating 5904138256 (5630.6 MiB)` | `Ran out of memory allocating 47185920 (45.0 MiB)` |
| 崩溃位置 | `BuildThumbnailTexture` 第一个 tile（`RuntimeVirtualTextureBuildStreamingMips.cpp:284` 的 `StreamAllResources`） | `BuildStreamedMipsWithMipmap` 合并缓冲 | `EndUpdateTexture3D` 超时后 18 分钟 |
| 直接原因 | 只读纹理 12.3 GB > 卡容量 12,282 MiB | 一次 5.6 GiB 连续块申请失败 | commit 737 GiB 顶满 commit limit |
| 峰值内存 | VRAM 11,343~11,692 MiB（离天花板 590~939 MiB） | `PeakUsedPhysical` 49.04 GiB | `PeakUsedPhysical` 47.42 GiB |
| 修复手段 | `r.Streaming.BakeMipBias 1`（排除地形）+ 5 项配套 | 精确 `Reserve` + `const&` + 单层跳过 | 显卡驱动升级 + VHM 补 GC 开关 |
| 验证状态 | 11 轮后连续两轮跑通 | **已提交、构建机已验证** | **未验证** |


> **为什么必须分开**：本地那轮构建机**通过了**（`GPU crash` 计数为 0，mip 链两轮全部走完），说明构建机显存够用 —— 本地的显存瓶颈在构建机上不存在。反过来，构建机的 `Reserve` 修复对本地那张 12 GB 卡毫无帮助，因为本地崩在纹理流入期，根本走不到 mip 合并。而 VHM 跟前两者都无关：它的 OOM 申请量只有 45 MiB，是**等待**的结果，不是**需求**的结果。


### 2.1 两次构建的时间基线

后文所有对比都基于这两份日志，先把关键刻度钉住：

```
日志 1（CL 1227410，不含任何修复）
  20:29:09  SVT 启动
  20:35:55  Allocating 128MiB        ← Thumbnail 阶段
  20:59:13  Allocating 3072MiB       ← mip 链第 1 个
  21:04:05  Allocating 3MiB          ← 第二轮 mip 链末尾（第 14 次）
  21:04:12  Ran out of memory allocating 5904138256 (5630.6 MiB)

日志 2（CL 1230415，含 Reserve 修复）
  16:32:34  SVT 启动
  16:38:30  Allocating 128MiB
  17:05:02  Allocating 3072MiB
  17:06:43  Allocating 3MiB          ← 第一轮完整走完
  17:15:12  Allocating 0MiB          ← 17 次分配全部走完
  17:18:23  CaptureSharedTextures::Release()----------CommitAll...
  17:19:22  Submitting change 1230855，Locking 7 files
  17:19:24  VHM 启动
  17:34:35  GPU timeout [3D] 5.000 s / (0x8000008d) [Active] EndUpdateTexture3D
  17:53:20  Ran out of memory allocating 47185920 (45.0 MiB)
```


## 3. 构建机 SVT 根因：合并缓冲要 5,630.6 MiB 连续块

> 一个数字锁定了全部：`5904138256 = 4,095 MiB × 11/8 + 16`。误差为零，说明申请的目标尺寸正好是 mip 链总和，增长因子正好是 UE 的默认 slack 策略。


### 3.1 代码路径：三处 tile 循环 + 两段缓冲

`RuntimeVirtualTextureBuildStreamingMips.cpp` 里有三处逐 tile 渲染的循环，每处都调 `StreamAllResources(0)`：

| 函数 | tile 循环 | `StreamAllResources(0)` |
|---|---|---|
| `BuildThumbnailTexture`（`RuntimeVirtualTextureBuildStreamingMips.cpp:198`） | `RuntimeVirtualTextureBuildStreamingMips.cpp:268` | `RuntimeVirtualTextureBuildStreamingMips.cpp:284` |
| `BuildStreamedMipsWithMipmap`（`RuntimeVirtualTextureBuildStreamingMips.cpp:387`） | `RuntimeVirtualTextureBuildStreamingMips.cpp:480` | `RuntimeVirtualTextureBuildStreamingMips.cpp:496` |
| `BuildStreamedMips`（`RuntimeVirtualTextureBuildStreamingMips.cpp:631`） | `RuntimeVirtualTextureBuildStreamingMips.cpp:702` | `RuntimeVirtualTextureBuildStreamingMips.cpp:718` |

构建机崩在第二个函数里。它分两段：

1. **逐 mip 累积**（`RuntimeVirtualTextureBuildStreamingMips.cpp:447-591`）：外层按 mip 循环，每个 mip 渲染全部 tile，再把这一层像素 `Append` 进 `PixelsPerLayer[Layer]`（`RuntimeVirtualTextureBuildStreamingMips.cpp:589`）。
2. **合并各层**（`RuntimeVirtualTextureBuildStreamingMips.cpp:594-614`）：把每层内容拼成一个 `FinalPixelsAllLayers`，再交给 `InitializeStreamingTexture`（`RuntimeVirtualTextureBuildStreamingMips.cpp:619`）。

两处 `Append` 都没有 `Reserve`。第二段还额外踩了一个坑：`for` 循环写成了按值遍历。


### 3.2 增长因子：3/8 加 16 字节

UE 的 `TArray` 需要更多容量时走 `DefaultCalculateSlackGrow`：

```
// ContainerAllocationPolicies.h:233-241
if (CurrentMax)
{
    Grow = SIZE_T(NewMax) + ContainerSlackGrowthFactorNumberator * SIZE_T(NewMax)
                         / ContainerSlackGrowthFactorDenominator + ConstantGrow;
}
```

分子取 **3**、分母取 **8**（`ContainerAllocationPolicies.h:123`、`ContainerAllocationPolicies.h:136`），`ConstantGrow` 取 **16**（`ContainerAllocationPolicies.h:216`）。于是有效尺寸 = `NewMax × 11/8 + 16`。


> 注意 `CONTAINER_INITIAL_ALLOC_ZERO_SLACK` 默认是 **1**（`ContainerAllocationPolicies.h:108`）：`CurrentMax == 0` 时走的是「无 slack」分支（`ContainerAllocationPolicies.h:244`），所以**只有往非空数组里追加才带 11/8**。这条决定了后文对层数的推导。

```
NewMax                TArray 增长因子              应申请                   与日志差值
4,095 MiB      ──▶    × 11/8 + 16 B       ──▶      5,904,138,256 B   ──▶        0
= 4,293,918,720 B     (3/8 分子 + 16 B 常数项)      5,630.6 MiB
mip 链全部 mip 之和

日志原文：Ran out of memory allocating 5904138256 (5630.6 MiB) bytes with alignment 1
差的 16 B 就是 ConstantGrow 常数项 —— 数组身份与增长路径由此唯一确定。
```

*图 2　申请量与日志逐字节对上*

**这一步为什么是决定性的**：`5,904,138,256` 不可能是巧合。反解 `NewMax = 5,904,138,240 × 8 / 11 = 4,293,918,720 B = 4,095 MiB`，而 4,095 MiB 正好是 `3072 + 768 + 192 + 48 + 12 + 3`（日志里依次出现的六个 mip 尺寸）。差的 16 字节就是 `ConstantGrow` 常数项 —— 数组身份与增长路径由此唯一确定。

调用栈也与之一致（日志 1 第 10734 行起）：

```
[Callstack] FMallocMimalloc::Realloc()                                    MallocMimalloc.cpp:159
[Callstack] TSizedHeapAllocator<64,FMemory>::ResizeAllocation()  ContainerAllocationPolicies.h:897
[Callstack] TArray<unsigned char,TSizedDefaultAllocator<64>>::ResizeGrow()          Array.h:3152
[Callstack] RuntimeVirtualTexture::BuildStreamedMipsWithMipmap()  ...BuildStreamingMips.cpp:573
```


> `TArray64<uint8>` 就是 `TArray<unsigned char, TSizedDefaultAllocator<64>>`；`Append` 被内联，所以 `ResizeGrow` 的调用点直接落在 `BuildStreamedMipsWithMipmap` 上；其中 `RuntimeVirtualTextureBuildStreamingMips.cpp:573` 是**修复前版本**的行号。


### 3.3 峰值账：四笔内存同时存活

崩的那一刻，进程里同时挂着四笔钱：

| 同时存活的块 | 原版 | 修复后 | 说明 |
|---|---|---|---|
| `PixelsPerLayer[0..N-1]` | 5,280 MiB | 4,095 MiB | 原版各自带 slack 增长；修复后一次精确预留 |
| 按值层拷贝 `LayerPixels` | 2,047.5 MiB | 0 | `for` 漏了 `const&`，每轮整份复制一层 |
| `FinalPixelsAllLayers` 旧块 | 2,047.5 MiB | 0 | 有 `Reserve` 就不会 realloc，旧块不存在 |
| `FinalPixelsAllLayers` 新块 | **5,630.6 MiB（失败）** | 4,095 MiB | 11/8 slack 换成了精确预留 |
| **合计** | **15,006 MiB** | **8,190 MiB** | 单层 RVT 再降到 4,095 MiB |

| 方案 | 峰值组成（MiB） | 合计 |
|---|---|---|
| 原版 | PixelsPerLayer 含 slack 5,280 · 按值层拷贝 2,047.5 · 合并缓冲旧块 2,047.5 · 合并缓冲新块（失败）5,630.6 | **15,006 MiB** |
| + Reserve / const& | PixelsPerLayer 精确 4,095 · 合并缓冲（一次 Reserve）4,095 | **8,190 MiB** |
| + 单层跳过合并 | PixelsPerLayer[0] 直接复用 4,095 | **4,095 MiB** |

按 2 层 RVT、mip 链总量 4,095 MiB 均分推导；原版 slack 来自 PixelsPerLayer 的 11/8 增长。

*图 3　峰值组成：原版 / 加 Reserve+const& / 再加单层跳过*


> 上表按 **2 层 RVT**、mip 链总量 4,095 MiB 均分推导。2 层是 11/8 slack 能被触发的前提：第一层 `Append` 进空数组时 `CurrentMax == 0`，走「无 slack」分支（`ContainerAllocationPolicies.h:244`），第二层才 `CurrentMax != 0`。**层数未在日志中直接出现，此处按推导标注为假设。**


### 3.4 内存水位：为什么 5.6 GiB 连续块会失败

崩前一刻的内存统计（日志 1 第 10718-10725 行）：

```
AvailablePhysical 1167060992 (1.09 GiB)
     UsedPhysical 47806750720 (44.52 GiB)
 PeakUsedPhysical 52660031488 (49.04 GiB)
      UsedVirtual 668323037184 (622.42 GiB)
    TotalPhysical 68393086976 (63.70 GiB)
```

物理内存只剩 1.09 GiB，而合并缓冲要的是**一块连续 5.6 GiB**。注意 `UsedPhysical` 已经 44.52 GiB —— 这个基础占用主要不是 mip 链造成的，而是流出的 WP cell 没有被回收（见 §4.4 的 GC 开关）。


## 4. 构建机 SVT 的修复与验证

> 三项一起做：一个消除大块扩容，一个消除冗余拷贝，一个在单层时省掉整个合并缓冲。全部只影响分配策略，**不碰任何像素内容**。

```
① 逐 mip 渲染 ──▶ ② 遍历各层 ──▶ ③ 合并成单一缓冲 ──▶ ④ InitializeStreamingTexture
BuildStreamedMips           for (TArray64<uint8>       FinalPixelsAllLayers      uint8* 交给
WithMipmap                      LayerPixels :              .Append()
每个 mip：渲染全部            PixelsPerLayer)            无 Reserve → ResizeGrow
tile → Append 进                                         现场索要 5,630.6 MiB
PixelsPerLayer[L]          原版漏了 const&               连续块 —— 构建机崩在这
                           → 每轮整份拷贝一层
─────────────────          ─────────────────          ──────────────────────
6 个 mip：3072 / 768 /     峰值期这笔拷贝仍存活：         Old 2,047.5 + New 5,630.6
192 / 48 / 12 / 3 MiB，     2,047.5 MiB                 Realloc 期间两块并存
合计 4,095 MiB

传递的数据：TArray64<uint8> PixelsPerLayer[L] → 一份层拷贝 → FinalPixelsAllLayers
            → uint8* 交给 VT 构建
```

*图 4　一个 RVT 的内存生命周期与失败点*


### 4.1 逐 mip 精确 Reserve

不能用「mip0 × 4/3」估算：mip 尺寸有 `TileSize` 下限，尾部几级不再按 1/4 递减，几何级数会偏差。直接按每一级的实际尺寸累加：

```
// RuntimeVirtualTextureBuildStreamingMips.cpp:433-444
for (int32 Layer = 0; Layer < NumLayers; ++Layer)
{
    int64 AllMipsLayerBytes = 0;
    for (int32 MipIndex = 0; MipIndex < MipNum; ++MipIndex)
    {
        const int32 RenderLevel = BeginRenderLevel + MipIndex;
        const int32 MipSizeX = FMath::Max(TileSize, TextureSizeX >> RenderLevel);
        const int32 MipSizeY = FMath::Max(TileSize, TextureSizeY >> RenderLevel);
        AllMipsLayerBytes += CalculateImageBytes(MipSizeX, MipSizeY, 0,
                               GetNormalizedTileLayerFormat(LayerFormats[Layer]));
    }
    PixelsPerLayer[Layer].Reserve(AllMipsLayerBytes);
}
```


### 4.2 一个会让 Reserve 失效的格式陷阱


> **注意**：**这是编译验证阶段才发现的真缺陷**：`FTileRenderResources` 在构造时把层格式归一化 —— `PF_BC4 → PF_G16`、`PF_R32_FLOAT → PF_R32_FLOAT`、**其余全部 → `PF_B8G8R8A8`**。而第一版 `Reserve` 用的是外层**未归一化**的 `LayerFormats`：若 RVT 用 DXT/BC 系列（4~8 bpp），预留量会被**低估 4~8 倍**，`Reserve` 形同虚设，构建机 OOM 实际没修掉。

修法是把归一化规则提取成一个函数，构造与 `Reserve` 共用同一套口径：

```
// RuntimeVirtualTextureBuildStreamingMips.cpp:24-35
static EPixelFormat GetNormalizedTileLayerFormat(EPixelFormat InFormat)
{
    if (InFormat == PF_G16 || InFormat == PF_BC4)
    {
        return PF_G16;
    }
    if (InFormat == PF_R32_FLOAT)
    {
        return PF_R32_FLOAT;
    }
    return PF_B8G8R8A8;
}
```

等价性：`Append` 写入量是 `CalculateImageBytes(TileSize, TileSize, N) × NumTilesX × NumTilesY`，`NumTilesX = ImageSizeX / TileSize`；`Reserve` 是 `CalculateImageBytes(MipSizeX, MipSizeY, N)`，`MipSizeX = Max(TileSize, TextureSizeX >> Level)` —— 同一个表达式。可整除时两者恒等；不可整除时 `Append < Reserve`，只多不少，安全。


### 4.3 `const&` 遍历与单层跳过

```
// RuntimeVirtualTextureBuildStreamingMips.cpp:600-616
const bool bNeedsMergedBuffer = PixelsPerLayer.Num() > 1;
if (bNeedsMergedBuffer)
{
    int64 TotalLayerBytes = 0;
    for (const TArray64<uint8>& LayerPixels : PixelsPerLayer)   // 原来是按值
    {
        TotalLayerBytes += LayerPixels.Num();
    }
    FinalPixelsAllLayers.Reserve(TotalLayerBytes);

    for (const TArray64<uint8>& LayerPixels : PixelsPerLayer)
    {
        FinalPixelsAllLayers.Append(LayerPixels);
    }
}

uint8* const FinalPixelsData = bNeedsMergedBuffer ? FinalPixelsAllLayers.GetData()
                                                 : PixelsPerLayer[0].GetData();
```

单层 RVT 时合并缓冲只是 `PixelsPerLayer[0]` 的整块冗余拷贝 —— 直接复用它的 `GetData()`，数据布局完全相同，零风险，再省掉一个与 mip 链等大的连续块。


### 4.4 顺带修好的 GC 开关

这次提交里还带了 `FLevelStreamingGCHelper::EnableForCommandlet()`（`WorldPartitionRuntimeVirtualTextureBuilder.cpp:82`）。它针对的不是 mip 链，而是 `PeakUsedPhysical 49.04 GiB` 这个基础占用 —— 不开这个开关时，editor commandlet 下 `PrepareStreamedOutLevelForGC` 直接跳过（`LevelStreamingGCHelper.cpp:237` 的 `(!GIsEditor || bEnabledForCommandlet)` 条件），流出的 WP cell 永不回收。


### 4.5 验证：第二份构建机日志

判据不是「跑完了」，而是三条同时成立：那笔 5,630.6 MiB 分配消失、mip 链完整走完、产物真的提交入库。

|  | 旧日志 CL 1227410 | 新日志 CL 1230415 |
|---|---|---|
| OOM 申请量 | **5,904,138,256 B (5630.6 MiB)** | **该笔分配消失** |
| mip 链 | 第二轮 `3 MiB` 之后崩 | `128 → 256 → 3072 → 768 → 192 → 48 → 12 → 3 → 3072 → 768 → 192 → 48 → 12 → 3 → 12 → 32 → 0`，17 次分配全部走完 |
| 进程寿命 | 35 分钟后崩 | SVT 段 46 分钟跑完 |
| 结果 | 崩 | **17:19:22 提交 CL 1230855，Locking 7 files** |


> **注意**：**SVT 的 OOM 已修好**：同一份构建里，SVT 段完整跑完并提交了 7 个产物；新的 OOM 属于 34 分钟后启动的 VHM 进程，与 SVT 无关。


## 5. 构建机 VHM 根因：GPU 卡死 → commit 泄漏 737 GB → OOM

> 这次的 OOM 申请量只有 45 MiB —— 一个健康进程不该连 45 MB 都拿不到。真正的问题在 18 分钟前：GPU 停了。

事件（2026-10-28）：

| 时间 | 事件 |
|---|---|
| 17:34:20 | `SK_Boss01` 骨骼网格 NIL matrix 警告 |
| 17:34:26 | `streamingpoolsizemb="0"` —— 全 mip 常驻 |
| 17:34:27 | `sync fallback for TA_IMP` |
| 17:34:35 | GPU timeout `[3D]` 5.000 s |
| 17:34:35 | 面包屑 `(0x8000008d) [Active] EndUpdateTexture3D` |
| 17:34:35 | GPU timeout `[Copy]` 5.042 s |
| 17:34:54 | GPU timeout `[3D]` 再 5.000 s |
| **17:53:20** | `Ran out of memory allocating 47185920 (45.0 MiB)` |
| 17:34:54 → 17:53:20 | **18 分 26 秒零日志（等 GPU fence）** |

OOM 时刻（17:53:20）的进程内存：

| 指标 | 数值 |
|---|---|
| `UsedPhysical` | 6.66 GiB |
| `PeakUsedPhysical` | 47.42 GiB |
| `UsedVirtual` | **737.18 GiB** |
| `AvailableVirtual` | 0.05 GiB |

`UsedVirtual` = `PagefileUsage`（进程级 commit，`WindowsPlatformMemory.cpp:475`）。
WorkingSet 只用 6.66 GiB，却 commit 了 737 GiB → 典型 commit 泄漏，连 45 MiB 都申请不到。

*图 5　VHM 事件时序与 OOM 时刻的内存指标*


### 5.1 面包屑指向 `EndUpdateTexture3D`

```
17:34:35:043  LogD3D12RHI: Warning: GPU timeout: A payload (...) on the
              [0x0000045ECA415300, 3D] queue has not completed after 5.000233 seconds.
17:34:35:075  Device 0, Pipeline Graphics: (In: 0x8000008d, Out: 0x8000008d)
              (ID: 0x8000004c) [   Finished]  DrawMaterialToRenderTarget: TextureRenderTarget2D_0
              ...
              (ID: 0x8000008c) [   Finished]  EndUpdateTexture3D
              (ID: 0x8000008d) [     Active]  EndUpdateTexture3D     <- 卡在这
```

`EndUpdateTexture3D` 在引擎里只有两个调用点，都在 SSS 的 3D LUT 更新里：`SubsurfaceProfile.cpp:602` 与 `SubsurfaceProfile.cpp:732`。超时前 15 秒日志里出现的正是 `SK_Boss01` 骨骼网格的警告 —— VHM 阶段加载了带 SSS 的角色资产。


### 5.2 内存指标说明 OOM 是后果

`WindowsPlatformMemory.cpp` 里 `UsedVirtual` 取的是 `PagefileUsage`（`WindowsPlatformMemory.cpp:475`），也就是**进程级 commit charge**，不是工作集：

```
AvailablePhysical 1080000512 (1.01 GiB)
 AvailableVirtual 56197120 (0.05 GiB)
     UsedPhysical 7151603712 (6.66 GiB)
 PeakUsedPhysical 50916765696 (47.42 GiB)
      UsedVirtual 791536680960 (737.18 GiB)
    PeakUsedVirtual 791591313408 (737.23 GiB)
```


> **注意**：进程 commit 了 **737 GB**，WorkingSet 只有 **6.66 GB** —— 「commit 了却几乎不用」，典型 commit 泄漏。`AvailableVirtual` 只剩 50 MB，所以 45 MB 的申请必然失败。**OOM 是 GPU 停摆 18 分钟期间 commit 累积的结果，不是内存需求本身。**


### 5.3 两条诱因，按证据强度

| 诱因 | 证据 | 成本 | 能否解释 5 秒超时 |
|---|---|---|---|
| **显卡驱动过期** | `LogRHI: Warning: Out of date driver found. Using: '560.94' Suggested: '610.47'` 与 `Running with bad GPU drivers` 在日志中出现 3 次 | 零代码 | **能** —— `EndUpdateTexture3D` 卡满 TDR 阈值是典型驱动层症状 |
| **VHM 完全没有治理** | `WorldPartitionVirtualHeightfieldMeshBuilder.cpp:50` 直接调静态 `LoadRuntimeVirtualTextureActors`，不经过 `PreBakeRVT()`；`streamingpoolsizemb="0"` 全 mip 常驻 | 已改（见 5.4） | 不能 —— 只影响内存累积 |

TDR 配置为 `Level: Recover, Delay: 2, DdiDelay: 5`，正好对应 5 秒超时阈值。


### 5.4 已改的内容与它解决不了什么

VHM builder 不走 `PreBakeRVT()`，所以 SVT 侧那套治理（GC 开关、`BakeMipBias`、`PoolDeallocateDelayFrame`）**一项都不生效**。本轮给它补上 GC 开关：

```
// WorldPartitionVirtualHeightfieldMeshBuilder.cpp:41-46
FLevelStreamingGCHelper::EnableForCommandlet();
```


> **注意**：**必须说清楚边界**：这一项只缓解该进程 `PeakUsedPhysical 47.42 GiB` 的累积，**不解决 GPU 超时**。如果 GPU 仍在 `EndUpdateTexture3D` 卡满 5 秒，18 分钟的等待照样会把 commit 耗光。已编译通过（exit=0、0 error），但**尚未在构建机验证**。

引擎自己也在代码里承认了 VHM 的结构问题：`WorldPartitionVirtualHeightfieldMeshBuilder.cpp:53` 写着 `// todo[vhm]: Convert builder to sequentially load and build sections of the world.` —— 当前实现一次性加载全世界。


## 6. 本地 12 GB：四层根因与 11 轮实验

> 这条线在本机崩、在构建机不崩，是独立的一套根因。前三层都能修，第四层修不了 —— 所以最终方案只能改需求侧。

| 层 | 根因 | 代码/证据 |
|---|---|---|
| **L1 触发** | `StreamAllResources(0)` 逐 tile 调用，`bProcessEverything=true` → 一次性全量流入 | `ContentStreaming.cpp:736` |
| **L2 不回收** | 池块释放挂在 frame fence 上；tile 循环无帧边界 → `AlivePools == PoolIndex` 恒成立 | `D3D12PoolAllocator.cpp:594/741/777` |
| **L3 放大器** | upload 池再延迟 3600 帧才回收（game 里 60 s 合理，commandlet 全程数百帧 → 永不触发）+ 冗余池预建 | `D3D12Allocation.cpp:1366`、`RHIPoolAllocator.cpp:754` |
| **L4 物理极限** | 只读纹理 12.3 GB > 可用 9,872 MiB；64 MB×192 = 16 MB×768 = 12.3 GB → 与池大小无关，是纹理本体 | 池大小不变性实验 |

结论：前三层可以修（分段补帧 / 池回收 / 门控），第四层修不了 —— 只能降低需求侧，即 mipbias。

*图 6　本地显存崩溃的四层根因*


### 6.1 前三层：为什么池子只增不减

1. **L1 触发**：`RuntimeVirtualTextureBuildStreamingMips.cpp:496` 每个 tile 都调 `IStreamingManager::Get().StreamAllResources(0)`，语义是 `bProcessEverything=true`（`ContentStreaming.cpp:736`）—— 不分帧摊销，一次性流入。NordLand 的 tile 网格是 128×128 = 16384 个 tile（`TileSize=64`），**第一个 tile 的这一次调用就完成了全量流入。**
2. **L2 不回收**：块释放挂在 `FrameFence.GetNextFenceToSignal()` 上（`D3D12PoolAllocator.cpp:594`、`D3D12PoolAllocator.cpp:741`），只有 `IsFenceComplete` 为真才真正归还 free list（`D3D12PoolAllocator.cpp:777`）；而 frame fence 只在 `RHIEndFrame` 推进，tile 循环全程没有帧边界 → 池零回收。铁证：`AlivePools == PoolIndex` 恒成立（128==128、768==768）。
3. **L3 两处放大器**：项目自定义的 upload big block 池再延迟 3600 帧才回收（`D3D12Allocation.cpp:1366`，默认值定义在 `D3D12Allocation.cpp:239`）—— 游戏里 3600 帧 = 60 秒合理，烘焙 commandlet 全程只跑数百帧，永远触发不了，池涨到 `Pool Index: 1197 × 8 MB ≈ 9.6 GB`；另有冗余池预建（`RHIPoolAllocator.cpp:754`）不校验余量就挂 64 MB 空池。


### 6.2 第四层：物理极限

只读纹理（`HeapType=1`，真显存）总量 12.3 GB。决定性实验是换池大小看总量：

| 池大小 | 池数 | 总量 |
|---|---|---|
| 64 MB | 192 | 12.3 GB |
| 16 MB | 768 | 12.3 GB |

**总量与池大小无关 → 不是碎片，是纹理数据本体。** 显卡 12,282 MiB − 桌面基线 2,410 MiB = 可用 9,872 MiB，需求约 13.3 GB，缺口 ≈ 3.4 GB。这一层代码修不掉。


### 6.3 11 轮实测：峰值几乎不变，变的是分配节奏

| 轮次 | 改动 | 峰值 VRAM (MiB) | 结果 |
|---|---|---|---|
| v9 | 64 分块加载 | 11,423 | 崩溃 |
| v15 | + `StreamAllResources` 分段补帧 | 11,343 | 崩溃（upload 池 1152→96） |
| v18 | + 冗余池 commandlet 门控 | 11,503 | 崩溃（只读池 192→128） |
| v19 | 只读池 64 MB→16 MB | 11,616 | 崩溃（池数 768，总量不变） |
| v20 | `-dpcvars` `PoolSize=7000` | 加载期 5,432 → 烘焙期 11,692 | 崩溃 |
| v23 | `PoolSize=5000` | 11,562 | 崩溃 |
| **v25** | `PoolSize=6500` | 11,442 | **跑通**（但 v26 同条件崩） |
| v27 | `PoolSize=5000` + mipbias 2 | ≈11.6 GB | 跑通 |
| v28 | mipbias 2（**未排地形**） | ≈11.6 GB | 跑通，但产物缩水 70%（反例） |
| v29 | mipbias 2（排地形） | ≈11.6 GB | 跑通，5/7 像素零差异 |
| **v30** | mipbias 1 | 11,550 | **跑通** |
| **v31** | mipbias 1（同 v30 复跑） | 11,552 | **跑通** |

| 轮次 | 峰值 VRAM (MiB) | 结果 |
|---|---|---|
| v9 | 11,423 | 崩溃 |
| v15 | 11,343 | 崩溃 |
| v18 | 11,503 | 崩溃 |
| v19 | 11,616 | 崩溃 |
| v20 | 11,692 | 崩溃 |
| v23 | 11,562 | 崩溃 |
| v25 | 11,442 | 跑通 |
| v30 | 11,550 | 跑通 |
| v31 | 11,552 | 跑通 |

整卡容量参考线 12,282 MiB。9 轮峰值全部落在 11,343 ~ 11,692 MiB，距天花板只剩 590 ~ 939 MiB。
崩溃与跑通的峰值几乎重叠 —— 改的不是总量，是分配节奏。（图纵轴截断在 10,800 ~ 12,400 MiB）

*图 7　9 轮峰值分布：崩溃与跑通的峰值几乎重叠*


> **这张图是全篇最重要的一个否定证据**：崩溃轮（11,343~11,692 MiB）与跑通轮（11,442~11,552 MiB）的峰值**几乎完全重叠**。所以问题从来不是「总量超标」，而是**瞬时爆发的分配节奏**撞上了只剩 590~939 MiB 的余量 —— 桌面聊天软件多占 200 MB 就足以让结果翻盘（v25 跑通、v26 同条件崩就是这么来的）。


## 7. 本地方案：确定性基线与 mipbias 取舍

> 既然总量修不了，只能降需求侧。但降采样会改变烘焙质量，所以验收必须先回答「这个产物的 MD5 到底能不能复现」——不能复现的产物，任何偏差都无法归因。


### 7.1 为什么 `r.Streaming.MipBias` 在烘焙里无效

三层门控全部拦死：

1. `TextureStreamingHelpers.cpp:387`：`GlobalMipBias = !GIsEditor ? ... : 0` —— 编辑器无条件归零。
2. `TextureStreamingHelpers.cpp:415`：`if (bUseAllMips) { GlobalMipBias = 0; }`。
3. `r.Streaming.UsePerTextureBias` 默认 1，使 `!Settings.bUsePerTextureBias` 为假（`StreamingTexture.cpp:225` 的这一支不进），`GlobalMipBias` 压根不会被加。

所以新增了一个独立 CVar `r.Streaming.BakeMipBias`（默认 0，零影响），施加在以上三层门控的**下游**、`MaxAllowedMips` 钳制之前。


### 7.2 必须排除地形：v28 的教训


> **注意**：**第一版没有排除地形，直接把产物烤废了**：landscape heightmap / weightmap 也降了 2 级 mip，height 类产物缩水约 70%（`RVT_WorldHeight_For_Materail_SVT` −69%、`RVT_BridgeLandscape_WorldHeight_SVT` −70%、`..._height_pizza_SVT` −72%）。加 `if (!bIsTerrainTexture)` 后全部恢复 —— 这也正是引擎在 `StreamingTexture.h:165` 的 `IsMaxResolutionAffectedByGlobalBias()` 里排除地形的理由：地形高度图决定 RVT 的几何精度。


### 7.3 bias 取值的取舍

| bias | 能否跑通 | `RVT_WhiteE_Nord_SVT` | `RVT_BridgeLandscape_SVT` |
|---|---|---|---|
| 0 | **必崩**（v24 实证 GPU crash） | — | — |
| **1（最终采用）** | 连续两轮跑通（v30/v31，峰值 11,550 MiB） | −1.06%（−3.56 MB） | −0.068%（−356 KB） |
| 2 | 跑通（v29），收益递减 | −3.3%（−11.1 MB） | −0.17%（−876 KB） |

其余 4 个 height / WorldHeight 产物在 bias=1 下**像素零差异**；`RVT_Nordland_Thumbnail` 为
+54 B（在 ±7 B 波动内）。bias=1 是「能跑通」与「偏差最小」的交点。

*图 8　bias 0/1/2：能跑通与偏差最小的交点*

**bias = 1 是最小可行值**：再降到 0 必崩，往上到 2 收益递减而偏差放大约 3 倍。


### 7.4 先建确定性基线，再比 golden

同设置连跑两轮（v30 / v31）先测确定性：

| 产物 | v30 vs v31 | 确定性 |
|---|---|---|
| `RVT_WorldHeight_For_Materail_SVT` | 46~48 B（全在 package header） | **确定** |
| `RVT_BridgeLandscape_WorldHeight_SVT` | 46~48 B | **确定** |
| `RVT_..._height_pizza_SVT` | 46~48 B | **确定** |
| `RVT_..._height_FloatPizza_SVT` | 46~48 B | **确定** |
| `RVT_WhiteE_Nord_SVT` | 46~48 B | **确定** |
| `RVT_Nordland_Thumbnail` | ±7 字节 | 有波动 |
| `RVT_BridgeLandscape_SVT` | ±2,481 字节 | 有波动 |

**这 2 个产物的 MD5 永远不可能完全复现，不能拿来当验收标准。** 据此归因：


### 7.5 最终验收（mipbias 1 vs golden）

| 产物 | vs golden | 归因 |
|---|---|---|
| `RVT_WorldHeight_For_Materail_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_WorldHeight_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_height_pizza_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_height_FloatPizza_SVT` | 像素零差异 | 完全等价 |
| `RVT_Nordland_Thumbnail` | +54 字节 | 在 ±7 B 波动量级内，视为等价 |
| `RVT_BridgeLandscape_SVT` | −356,491 字节（−0.068%） | 超出波动，真实但极小 |
| `RVT_WhiteE_Nord_SVT` | −3,558,310 字节（−1.06%） | 该产物确定性，真实 mipbias 影响 |

**4/7 完全等价 + 1/7 在波动内 + 2/7 真实偏差 ≤1.06%。**


### 7.6 A/B：哪些是必需项、哪些只是提速

「能跑通」不等于「有用」。用 CVar 逐项关掉做单变量对照：

| 轮次 | `PoolDeallocateDelayFrame` | tile 帧边界 | 结果 | 耗时 |
|---|---|---|---|---|
| v30 / v31 | 0（提前回收） | 8 | 跑通 | **~13 分钟** |
| A/B | 3600（关） | 0（关） | 跑通 | 31 分钟 |
| A/B-2 | 0 | 0（关） | 跑通 | ~31 分钟 |

- **唯一功能必需项是 `BakeMipBias`** —— bias=0 必崩（v24 实证）。
- **其余是实打实的性能优化**：全部关掉慢 **2.4 倍**；A/B-2 证明 **tile 帧边界是提速主因**。
- **对产物零影响**：A/B-minimal vs v30 的差异全部落在 run-to-run 波动量级内。


## 8. 落地清单与验收口径

> 按「对哪台机器有效」分组，避免再把两套修复混在一个 CL 里。

| 改动 | 位置 | 针对 | 实测依据 | 状态 |
|---|---|---|---|---|
| 逐 mip 精确 `Reserve` + 格式归一化 | `RuntimeVirtualTextureBuildStreamingMips.cpp:433-444` | 构建机 SVT | 5,630.6 MiB 分配消失、7 产物提交 | **已提交** |
| `const&` 遍历 + 单层跳过合并 | `RuntimeVirtualTextureBuildStreamingMips.cpp:600-616` | 构建机 SVT | 峰值 15,006 → 8,190 MiB（单层 4,095） | **已提交** |
| GC 开关 `EnableForCommandlet` | `WorldPartitionRuntimeVirtualTextureBuilder.cpp:82` | 构建机 SVT | `PrepareStreamedOutLevel*` 计数 0 → 降低 49.04 GiB 基础占用 | **已提交** |
| GC 开关（VHM builder） | `WorldPartitionVirtualHeightfieldMeshBuilder.cpp:45` | 构建机 VHM | 仅缓解 47.42 GiB 物理峰值 | 已编译，**待验证** |
| `r.Streaming.BakeMipBias`（排除地形） | `StreamingTexture.cpp` | 本地 12 GB | bias=0 必崩 / bias=1 连续两轮跑通 | **已 shelve（CL 1229056）** |
| `StreamAllResources` 分段补帧 | `ContentStreaming.cpp` | 本地 12 GB | upload 池 1152 → 96（9.2 GB → 768 MB） | **已 shelve** |
| 冗余池预建 `!IsRunningCommandlet()` 门控 | `RHIPoolAllocator.cpp:754` | 本地 12 GB | 只读纹理池 192 → 128 | **已 shelve** |
| tile 循环周期帧边界 | `RuntimeVirtualTextureBuildStreamingMips.cpp` | 本地 12 GB | **2.4 倍提速**（关掉后 13 → 31 分钟） | **已 shelve** |
| `PoolDeallocateDelayFrame 0` + 64 分块加载 | `WorldPartitionRuntimeVirtualTextureBuilder.cpp` | 本地 12 GB | 加载期峰值 11,181 → 5,098 MiB | **已 shelve** |


### 8.1 验收三个必须看的指标

- **`Ran out of memory allocating` 是否消失**，且消失的是**哪个尺寸** —— 申请量是身份指纹，单纯「不崩了」不构成修复证据。
- **`PeakUsedPhysical` / `UsedVirtual`** 是否回落。`UsedVirtual` 是进程 commit，看泄漏；`UsedPhysical` 看真实占用。
- **产物是否真的提交入库**（`Submitting change ... Locking N files`），而不是进程 exit 0 就算过。


### 8.2 编译验证的正确做法

增量编译可能直接跳过 —— 必须强制 touch 重编，并确认目标 `.cpp` 真在其中：

```
# 强制重编，确认 Module.VirtualTexturingEditor.cpp / Module.UnrealEd.NN.cpp 出现在编译单元里
touch UE5EA/Engine/Source/Editor/VirtualTexturingEditor/Private/RuntimeVirtualTextureBuildStreamingMips.cpp
touch UE5EA/Engine/Source/Editor/UnrealEd/Private/WorldPartition/WorldPartitionRuntimeVirtualTextureBuilder.cpp
# 然后核对 obj / dll 时间戳确实更新
```


## 9. 已知问题与后续动作

| 问题 | 影响 | 建议动作 |
|---|---|---|
| VHM 的 GPU 超时未解决 | 驱动不变的话，GC 开关也救不回来 | **先把构建机驱动从 560.94 升到 610.47 再重跑** —— 零代码成本，日志已明确警告 |
| VHM builder 一次性加载全世界 | `PeakUsedPhysical 47.42 GiB`；引擎自己标了 `todo[vhm]` | 中期改造为分块加载，与 SVT 的 `PreBakeRVT` 治理对齐 |
| 单层跳过合并**未在生产路径触发过** | 只影响单层 RVT 组件，对本次 2 层资产无影响 | 下次构建对照峰值即可 |
| `RVT_WhiteE_Nord_SVT` −1.06% | 属确定性偏差，不是噪声 | 需美术确认这 1% 是否可接受（本地 mipbias 方案） |
| `RVT_BridgeLandscape_SVT` −0.068% | 真实但极小 | 同上 |
| golden 本身未必是「标准答案」 | golden 取自 DDC 冷那轮，纹理可能未完全就位 | 建立一次全热的正式基线，再作后续对比 |
| 本地峰值离天花板只剩 590~939 MiB | 桌面显存一波动就翻盘（v25 跑通 / v26 同条件崩） | 构建机不应作为验收环境的替代；本地跑必须清空桌面显存 |


## 10. 附录 A · 被推翻的 15 条假设

> 按浪费的时间排序。每条写清「我说过什么 / 怎么被推翻 / 代价」，目的是让下一次排查不在同一个地方再花一遍时间。

| # | 当时的假设 | 推翻方式 | 代价 |
|---|---|---|---|
| A1 | `-dpcvars` 能把 CVar 传进烘焙进程 | cmd 把 `=` 当分隔符（`set EXTRA=%2`）；即便传进去，DeviceProfile 也无条件覆盖 | **v16/v20/v21/v22/v23 五轮全废** |
| A2 | 开有限 `PoolSize` 能解决且可用 MD5 证明不伤画质 | 用户否决（改变烘焙条件）；且 v25 跑通、v26 同条件崩 | v25/v26/v27 三轮 |
| A3 | `PoolSize=0` 是根因 | 完整日志显示上午下午**都是** `PoolSize=0`；上午日志没那行只因值没变过 | v9 一轮 |
| A4 | TA_IMP 的 8 GB 同步上传是根因 | 实测 TA_IMP 展开仅 **19 MB**、全程只重建 1 次 | `BakeIgnoreStableSlice` 改动（已 revert） |
| A5 | 过滤 GroundGrid / 非 RVT 的 LevelInstance actor | 用户指出那是项目定制烘焙效果需要；v10 产物 SVT −12.4 MB = 烤丢内容 | 已还原 golden，MD5 7/7 |
| A6 | `D3D12.ResidencyDebugBudgetMB` 能让 residency manager 换出 | 引擎注释自述：只在「驻留集 > DXGI 报告预算」时换出，而 DXGI 报得过高 | 一轮 |
| A7 | `bProcessEverything=true` 会无视内存预算 | `StreamingManagerTexture.cpp:1935`：只是不分帧摊销（`ShouldAmortizeMipCopies`），请求仍由预算相关的优先级产生 | 一轮（真因是上限给太高 7000 MB） |
| A8 | `d3d12.TexturePool.CleanupFrameLag` 能救 | 改的是 texture pool，漏的是 **upload heap big block** pool | 一轮 |
| A9 | 下午峰值更高所以崩 | 被自己数据打脸：上午扛过 3072 MiB ×2，下午连 128 MiB 后都活不过 13 秒 | 一轮推理 |
| A10 | 上午成功是因为 DDC 冷把工作摊薄 | 真机制是**帧数**：池回收要 `GFrameCounter` 越过 3600 帧阈值 | — |
| A11 | Reserve 按 `mip0 × 4/3` 几何级数估算就够 | mip 尺寸有 `TileSize` 下限（`RuntimeVirtualTextureBuildStreamingMips.cpp:439` 的 `FMath::Max`），尾部不再 1/4 递减 | — |
| A12 | Reserve 用外层 `LayerFormats` 就行 | 构造时格式归一化（DXT/BC 4~8 bpp → B8G8R8A8 32 bpp），预留被低估 **4~8 倍** | 编译验证阶段才发现 |
| A13 | mipbias 不用排除地形 | v28 实测 height 类产物缩水 **70%** | v28 一轮 + 重新验证 |
| A14 | 崩溃点是 `FinalPixels.SetNumUninitialized` | 反解 `NewMax = 4,095 MiB` = 六 mip 之和，而 `SetNumUninitialized` 的尺寸是单个 mip（3072 MiB） | 一次错误归因（后续修正） |
| A15 | 面包屑「都卡在 EndUpdateTexture3D」 | 实际只有 `(0x8000008d) [Active]` 卡住，其余 `0x8000004c`~`0x8000008c` 均 `[Finished]` | 表述修正 |


### 10.1 几条最贵的展开

**A1 —— 五轮实验白跑**。两个独立原因叠加，任何一个单独都足以让实验无效：

```
# 原因一：cmd 把 = 当参数分隔符
set EXTRA=%2              →  -dpcvars    （= 后全丢，日志铁证）
# 原因二：即便传进去也没用
LogDeviceProfileManager: Pushing Device Profile CVar: [[r.Streaming.PoolSize:1200 -> 0]]
```

正确做法：改 CVar 就在 `PreBakeRVT()` 里 `GEngine->Exec` —— 运行时 Set 优先级最高。但注意 `RHIPoolAllocator.AllowAsyncAllocateRedundancyPool` 是 `FConsoleVariableRef`，运行时 `Exec` 会直接 access violation，只能代码里加 `!IsRunningCommandlet()` 门控。

**A5 —— 方向性的错**。用户的原话是「GroundGrid 的 Actor 是故意加载的，目的就是项目定制的烘焙效果的需要」。v10 实验恰好量化了代价：

| 产物 | golden | v10（过滤后） | 差 |
|---|---|---|---|
| `RVT_BridgeLandscape_SVT` | 521,935,835 | 509,484,114 | **−12.4 MB** |
| `RVT_Nordland_Thumbnail` | 856,529 | 834,113 | **−22 KB** |

**A10 —— 同一个「上午为什么成功」问了两遍才答对**。第一遍答「DDC 冷」，第二遍（在找到 upload 池 3600 帧这个事实之后）才答对：关键是**帧数**，不是冷热。


## 11. 附录 B · 23 轮排查编年史

> 只列真人提问（其余 user 条目是 Stop hook / skill 注入 / `/model` `/compact` 回显）。时间已换算为 CST，便于与 `S1Game.log` 对齐。

判定图例：✅ 成立　❌ 后被推翻　⚠️ 部分对　🔄 自我修正

| 时间 | 提问 | 当时的结论 | 判定 |
|---|---|---|---|
| 10-27 12:15 | 照 `BuildSVTVHM3.bat` 帮我构建 | 识别出 workspace 是 `DJANGOZHAN-PCFW_GR_release_F`；脚本的 `p4 revert //...` 会毁掉 4 个未提交 integrate | ✅ |
| 15:59 | 这个 GC 代码引入有没有问题 | 诊断对、方向对，但 4 个问题；行号 237 准确 | ⚠️ 与当天崩溃无关 |
| 16:10 | 加上 GC 重跑 SVT | 编译 exit 0 → 16:18:16 `DEVICE_RESET` | ❌ |
| 16:26 | 测一下非 GC 版 | 非 GC 版**同样崩**，`PrepareStreamedOutLevel*` 计数 0 | ✅ 从此排除 GC 假设 |
| 16:47 | 上午为什么成功过一次 | `PoolSize=0` 不是差异来源 | ✅ |
| 16:47 | （同上） | 上午是全冷，工作摊在 34 分钟里 | ❌ 真因是帧数 |
| 16:50 | 开流送效果不能保障 | 停实验；产物未被覆盖；方案空间砍到「只动机器不动配置」 | ✅ 贯穿全程 |
| 16:56 | 结合上午/下午日志分析区别 | 下午是在**更低水位**上崩的 | ✅ 关键反转 |
| 17:34 | TA_IMP 那个修复做了么 | 承认没做，并自我否定原建议 | 🔄 |
| 17:34 | （同上） | `bStableSliceIndex` 是 8 GB 分水岭 | ❌ 实测仅 19 MB |
| 17:40 | 动手实施并自测 | 三次修复全部失败 | ❌ |
| 19:24 | GroundGrid 是故意加载的；沿最后一笔大申请查 | 方向确认错误并回退；查到 upload 池 3600 帧 | 🔄 → ✅ |
| 21:45 | 构建机日志(1) 是同一个问题么 | 不是，构建机是 CPU OOM | ✅ |
| 21:45 | （同上） | 崩溃点是 `FinalPixels.SetNumUninitialized` | ❌ 后修正 |
| 10-28 00:58 | `/clean-code` 去掉无效保留有效 | A/B：功能非必需，但关掉慢 2.4 倍 | ✅ |
| 09:18 | 不要影响运行时性能 | 查出 2 处门控缺失 | ✅ |
| 10:46 | 最终做了什么优化 | 真正解决崩溃的只有 1 项（mipbias） | ✅ |
| 10:52 | Reserve 是针对日志的优化吗 | 主动降级为「未经实测验证」并自曝几何级数缺陷 | 🔄 |
| 11:51 | 优先修构建机，shelve 其余 | 5 文件 shelve 到 CL 1229056 | ✅ |
| 12:01 | 验证编译 | touch 强制重编 → exit 0 | ✅ |
| 13:17 | 回溯日志，有没有修好 | `5,904,138,256 = 4,095 MiB × 11/8 + 16`，精确到 16 字节 | ✅ |
| 19:43 | 新日志(2)，还是有报错 | 5630.6 MiB 消失、SVT 提交 CL 1230855；新 OOM 是 VHM 的 GPU 超时 | ✅ |
| 10-29 17:08 | `/SumToAiDoc` 归档 | 出 HTML + MD 双份 | ✅ |


## 12. 附录 C · 取证方法与坏习惯复盘

> 这一节是本次排查里真正可复用的部分 —— 什么手段解决了问题，什么习惯制造了弯路。


### 12.1 有效的五条手段

1. **把 OOM 申请量当身份指纹反解**。`Ran out of memory allocating N` 是约束方程：已知增长因子就能反解 `NewMax`，再与代码里的候选尺寸对表，只可能有一个答案。本次误差 **0 字节**。
2. **区分 `UsedVirtual`(commit) 与 `UsedPhysical`(WorkingSet)**。commit 高而工作集低 = 泄漏；两者都高才是真的用掉。VHM 那次是 737 GB vs 6.66 GB，一眼定性。
3. **做池大小不变性实验**。64 MB×192 = 16 MB×768 = 12.3 GB → 总量由数据本体决定，换池尺寸无解。用一次实验排除一整类假设。
4. **先建 run-to-run 确定性基线**。同设置连跑两轮测波动幅度，低于波动的不算改动引起。挡掉了「Thumbnail +54 字节也算偏差」这类误判。
5. **拿完整日志，不用 console 重定向**。上午 console log 只有 57 MB，`S1Game.log` 却涨到 908 MB —— stdout 丢了大量内容，「上午没有 X」不能用 console log 证明。


### 12.2 制造弯路的六个习惯

| 坏习惯 | 具体表现 | 教训 |
|---|---|---|
| **先归因再取证** | 「驱动被前次 device removed 搞坏」「`PoolSize=0` 是根因」都是先下结论后补证据 | 没跑过的因果只能标成假设 |
| **把「刚好跑通」当成「修好了」** | v25 跑通就当成功，v26 同条件崩 | 同条件连跑两轮才算稳定 |
| **只测机制不测收益** | tile 帧边界机制正确但收益没单变量对照，差点被当无效代码删掉 | 功能非必需 ≠ 没有价值 |
| **改错对象** | `TexturePool.CleanupFrameLag` 改 texture pool，漏的是 upload big block pool | 先确认泄漏的是哪个池 |
| **信了 CLI 传参** | 五轮 `-dpcvars` 全是空转，日志里明写着参数没进去 | 每轮实验前先核对参数真的生效 |
| **对手上工具的参数想当然** | `RHIPoolAllocator.AllowAsyncAllocateRedundancyPool` 运行时 `Exec` 直接 access violation | 改 CVar 前先看它是 `FAutoConsoleVariableRef` 还是可运行时 Set |


### 12.3 一条可复用的判据


> 面对任何 OOM，**先问「这是申请方式错了，还是需求真的超了」**。两者的处置完全相反：前者改分配策略（`Reserve`、避免按值拷贝、复用缓冲），后者只能降需求或换硬件。本次三条线恰好各占一个：构建机 SVT 是前者（修好了），本地 12 GB 是后者（只能降 mipbias），VHM 则两者都不是 —— 它是**别的故障把内存拖垮了**。


## 13. 参考

- 引擎源码：`UE5EA/Engine/Source/Editor/VirtualTexturingEditor/Private/RuntimeVirtualTextureBuildStreamingMips.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Core/Public/Containers/ContainerAllocationPolicies.h`
- 引擎源码：`UE5EA/Engine/Source/Editor/UnrealEd/Private/WorldPartition/WorldPartitionRuntimeVirtualTextureBuilder.cpp`
- 引擎源码：`UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Engine/Private/Streaming/TextureStreamingHelpers.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Engine/Private/ContentStreaming.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12PoolAllocator.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12Allocation.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Core/Private/Windows/WindowsPlatformMemory.cpp`
- 构建机日志（旧，CL 1227410）：`D:\DownLoad\Misc\1-1-9-E689A7E8A18CE887AAE5AE9AE4B989E6938DE4BD9C(1).log`
- 构建机日志（新，CL 1230415）：`D:\DownLoad\Misc\1-1-9-E689A7E8A18CE887AAE5AE9AE4B989E6938DE4BD9C(2).log`
- 规划目录：`F:\GR_Release_F\.planning\2026-10-27-svt-gc-fix-rebuild\`（`findings.md` / `chronicle.md` / `progress.md`）
