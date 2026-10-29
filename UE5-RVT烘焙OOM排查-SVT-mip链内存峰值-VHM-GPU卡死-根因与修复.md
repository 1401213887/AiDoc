> ⚠️ **本文已被全量版取代**：[UE5-RVT烘焙OOM全量排查-本地12GB显存-SVT内存峰值-VHM-GPU卡死-失败假设复盘](./UE5-RVT烘焙OOM全量排查-本地12GB显存-SVT内存峰值-VHM-GPU卡死-失败假设复盘.md)
> 全量版增加了本地 12 GB 的 11 轮实验、多方案验收矩阵、15 条被推翻的假设与 23 轮编年史。
> 本文保留作为精简阅读入口。

# UE5 RVT 烘焙 OOM 排查 — SVT 合并缓冲内存峰值 · VHM GPU 卡死 · 根因与修复

> 仓库 `F:\GR_Release_F` · 引擎 `UE5EA`（UE 5.5 分叉）· 场景 `/Game/Maps/Nord/NordLand` · 构建机 `release_BuildSVTVHM`（63.70 GiB 物理内存）· 本地 RTX 4070 SUPER 12 GB · 每条结论附 `文件:行号` 或日志原文

**摘要：** 一次 `BuildSVTVHM3.bat` 串行拉起两个 `UnrealEditor.exe`，两者都 OOM，但**根因完全不同**。SVT 侧：mip 链在内存里累计 4,095 MiB 后，`FinalPixelsAllLayers` 无预留地长到 4,095 MiB，而 UE 的 `TArray` 增长因子是 3/8 + 16 B，于是**一次性索要 5,630.6 MiB 连续块**——日志里的 `5904138256` 正是 `4,095 MiB × 11/8 + 16`，逐字节对上。加逐 mip 精确 `Reserve`、`const&` 遍历、单层跳过合并后峰值从 **15,006 MiB 降到 4,095 MiB**，第二份构建机日志确认该笔分配消失、SVT 跑完并提交 `CL 1230855`。VHM 侧是完全另一件事：GPU 在 `EndUpdateTexture3D` 上 5 秒超时、随后 18 分钟零日志，进程 commit 涨到 **737.18 GiB**（WorkingSet 仅 6.66 GiB），OOM 是后果不是原因；日志同期明确警告显卡驱动过期（`560.94` → `610.47`）。

**目录**：1 结论与全景 · 2 症状归因 · 3 SVT 根因 · 4 修复 · 5 验证 · 6 VHM 根因 · 7 本地 12 GB · 8 落地清单 · 9 已知问题 · 10 取证方法 · 11 参考


## 1. 结论与全景

> 两个进程的 OOM 必须分开修。SVT 是**内存申请方式错了**（无预留的几何增长），VHM 是**GPU 先卡死**、内存只是被拖垮的受害者。

先把两件事分开看，再逐一展开：

- **SVT（可修、已修、已验证）**：`BuildStreamedMipsWithMipmap` 把各 mip 逐段 `Append` 进 `PixelsPerLayer`，再合并进 `FinalPixelsAllLayers`，两个缓冲都**没有预留**。合并那一步在 4,095 MiB 的目标尺寸上触发 `ResizeGrow`，按 11/8 要一块 **5,630.6 MiB 连续内存**，而当时进程已用 44.52 GiB / 63.70 GiB —— 直接失败。
- **VHM（未修完、根因在引擎外）**：GPU 在 `EndUpdateTexture3D` 上卡满 5 秒触发 timeout，之后 18 分 26 秒日志一片空白，进程 commit 从 6.66 GiB 一路涨到 737.18 GiB 顶满系统 commit limit，最后连 45 MiB 都申请不到。**OOM 是等待的后果，不是内存需求大。**
- **本地 12 GB 单卡（第三套根因，CL 已 shelve）**：只读纹理总量 12.3 GB 超过整卡容量 —— 这是物理极限，只能降需求侧（`r.Streaming.BakeMipBias 1`，且必须排除地形纹理）。

```
BuildSVTVHM3.bat（串行：前一个进程退出再启下一个）
   │
   ├─▶ ① UnrealEditor.exe  -Builder=WorldPartitionRuntimeVirtualTextureBuilder
   │       16:32:34 启动 · 46 分钟后进入提交
   │       ✗ 合并缓冲一次要 5,630.6 MiB 连续块
   │       ✓ 加 Reserve 后跑通，提交 CL 1230855
   │           │ 输入：逐 mip 渲染的像素
   │           ▼
   │       SVT / RVT 产物 ×7（RVT_WhiteE_Nord_SVT / RVT_BridgeLandscape_SVT /
   │                          WorldHeight / height_pizza / ... / Thumbnail）
   │
   └─▶ ② UnrealEditor.exe  -Builder=WorldPartitionVirtualHeightfieldMeshBuilder
           17:19:24 启动 · 34 分钟后 OOM
           ✗ GPU 5 秒超时 → 18 分钟零日志
           ✗ commit 涨到 737 GiB → 45 MiB 都失败
               │ 输入：MinMax / Mask 高度数据
               ▼
           HeightfieldMinMaxTexture / HeightfieldMaskTexture（本轮未产出）

图外：本地 12 GB 单卡是第三套根因 —— 只读纹理 12.3 GB 超过卡容量，
      用 r.Streaming.BakeMipBias 解决（见第 7 节）。
```

*图 1　一次构建、两个进程、三种失败签名*

**贯穿主线**：本文跟着一次 NordLand 构建走完 —— `16:32:34` 起 SVT 进程 → `17:19:22` 提交 7 个产物 → `17:19:24` 起 VHM 进程 → `17:53:20` 崩。第 3~5 节只讲 SVT（主线），第 6 节讲 VHM，第 7 节讲本地那台机器。


## 2. 症状归因：一次构建、两个进程、三种 OOM

> 三种失败签名放在一起对比，才能看出它们**没有共同根因**——任何把它们当成同一个问题的修复都会失败。

| 维度 | 本地（12 GB 单卡） | 构建机 SVT 进程 | 构建机 VHM 进程 |
|---|---|---|---|
| 失败类型 | GPU 显存耗尽 | CPU 进程内存耗尽 | CPU 进程内存耗尽（GPU 卡死的后果） |
| 上报签名 | `DXGI_ERROR_DEVICE_RESET` 887A0007 | `Ran out of memory allocating 5904138256 (5630.6 MiB)` | `Ran out of memory allocating 47185920 (45.0 MiB)` |
| 崩溃位置 | `BuildThumbnailTexture` 第一个 tile | `BuildStreamedMipsWithMipmap` 合并缓冲 | `EndUpdateTexture3D` 超时后 18 分钟 |
| 直接原因 | 只读纹理 12.3 GB > 卡容量 | 一次 5.6 GiB 连续块申请失败 | commit 737 GiB 顶满 commit limit |
| 修复手段 | `r.Streaming.BakeMipBias 1`（排除地形） | 精确 `Reserve` + `const&` + 单层跳过 | 显卡驱动升级 + VHM 补 GC 开关（未验证） |


> **为什么必须分开**：本地那轮构建机**通过了**（`GPU crash` 计数为 0，mip 链两轮全部走完），说明构建机显存够用 —— 本地的显存瓶颈在构建机上不存在。反过来，构建机的 `Reserve` 修复对本地那张 12 GB 卡毫无帮助，因为本地崩在纹理流入期，根本走不到 mip 合并。


## 3. SVT 根因：合并缓冲要 5,630.6 MiB 连续块

> 一个数字锁定了全部：`5904138256 = 4,095 MiB × 11/8 + 16`。误差为零，说明申请的目标尺寸正好是 mip 链总和，增长因子正好是 UE 的默认 slack 策略。


### 3.1 代码路径

`RuntimeVirtualTextureBuildStreamingMips.cpp` 里 `BuildStreamedMipsWithMipmap` 分两段：

1. **逐 mip 累积**（`RuntimeVirtualTextureBuildStreamingMips.cpp:447-591`）：外层按 mip 循环，每个 mip 渲染全部 tile，然后把这一层的像素 `Append` 进 `PixelsPerLayer[Layer]`（`RuntimeVirtualTextureBuildStreamingMips.cpp:589`）。
2. **合并各层**（`RuntimeVirtualTextureBuildStreamingMips.cpp:594-614`）：把每层内容拼成一个`FinalPixelsAllLayers`，再交给 `InitializeStreamingTexture`（`RuntimeVirtualTextureBuildStreamingMips.cpp:619`）。

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

其中分子取 **3**、分母取 **8**（`ContainerAllocationPolicies.h:123`、`ContainerAllocationPolicies.h:136`），`ConstantGrow` 取 **16**（`ContainerAllocationPolicies.h:216`）。于是有效尺寸 = `NewMax × 11/8 + 16`。

```
NewMax                TArray 增长因子              应申请                   与日志差值
4,095 MiB      ──▶    × 11/8 + 16 B       ──▶      5,904,138,256 B   ──▶        0
= 4,293,918,720 B     (3/8 分子 + 16 B 常数项)      5,630.6 MiB
mip 链全部 mip 之和

日志原文：Ran out of memory allocating 5904138256 (5630.6 MiB) bytes with alignment 1
差的 16 B 就是 ConstantGrow 常数项 —— 数组身份与增长路径由此唯一确定。
```

*图 2　申请量与日志逐字节对上*

**这一步为什么是决定性的**：`5,904,138,256` 这个数字不可能是巧合。反解 `NewMax = 5,904,138,240 × 8 / 11 = 4,293,918,720 B = 4,095 MiB`，而 4,095 MiB 正好是 `3072 + 768 + 192 + 48 + 12 + 3`（日志里依次出现的六个 mip 尺寸）。差的 16 字节就是 `ConstantGrow` 常数项 —— 数组身份与增长路径由此唯一确定。

调用栈也与之一致（旧日志 `1-1-9-...(1).log` 第 10734 行起）：

```
[Callstack] FMallocMimalloc::Realloc()                       MallocMimalloc.cpp:159
[Callstack] TSizedHeapAllocator<64,FMemory>::ResizeAllocation() ContainerAllocationPolicies.h:897
[Callstack] TArray<unsigned char,TSizedDefaultAllocator<64>>::ResizeGrow()  Array.h:3152
[Callstack] RuntimeVirtualTexture::BuildStreamedMipsWithMipmap()  ...BuildStreamingMips.cpp:573
```


> `TArray64<uint8>` 就是 `TArray<unsigned char, TSizedDefaultAllocator<64>>`；`Append` 被内联，所以 `ResizeGrow` 的调用点直接落在 `BuildStreamedMipsWithMipmap` 上；其中 `RuntimeVirtualTextureBuildStreamingMips.cpp:573` 是修复前版本的行号。


### 3.3 峰值账：三块内存同时存活

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


> 上表按 **2 层 RVT**、mip 链总量 4,095 MiB 均分推导 —— 2 层是把 11/8 slack 触发出来的前提：第一层 `Append` 进空数组时 `CurrentMax == 0`，走的是「无 slack」分支（`ContainerAllocationPolicies.h:233-247`），第二层才 `CurrentMax != 0`。层数未在日志中直接出现，此处按推导标注为**假设**。


### 3.4 内存水位：为什么 5.6 GiB 连续块会失败

崩前一刻的内存统计（旧日志第 10718-10725 行）：

```
AvailablePhysical 1167060992 (1.09 GiB)
     UsedPhysical 47806750720 (44.52 GiB)
 PeakUsedPhysical 52660031488 (49.04 GiB)
      UsedVirtual 668323037184 (622.42 GiB)
    TotalPhysical 68393086976 (63.70 GiB)
```

物理内存只剩 1.09 GiB，而合并缓冲要的是**一块连续 5.6 GiB**。注意 `UsedPhysical` 已经 44.52 GiB —— 这个基础占用主要不是 mip 链造成的，而是流出的 WP cell 没有被回收（见 §5.1 的 GC 开关）。


## 4. 修复：精确 Reserve + `const&` + 单层跳过

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


## 5. 验证：第二份构建机日志确认 SVT 已修好

> 判据不是「跑完了」，而是三条同时成立：那笔 5,630.6 MiB 分配消失、mip 链完整走完、产物真的提交入库。

|  | 旧日志 CL 1227410 | 新日志 CL 1230415 |
|---|---|---|
| OOM 申请量 | **5,904,138,256 B (5630.6 MiB)** | **该笔分配消失** |
| mip 链 | 第二轮 `3 MiB` 之后崩 | `128 → 256 → 3072 → 768 → 192 → 48 → 12 → 3 → 3072 → 768 → 192 → 48 → 12 → 3 → 12 → 32 → 0`，17 次分配全部走完 |
| 进程寿命 | 35 分钟后崩 | SVT 段 46 分钟跑完 |
| 结果 | 崩 | **17:19:22 提交 CL 1230855，Locking 7 files** |

时间线（新日志）：

```
16:32:34  SVT 进程启动  -Builder=WorldPartitionRuntimeVirtualTextureBuilder
17:18:23  CaptureSharedTextures::Release()----------CommitAll...
17:19:22  Submitting change 1230855.
17:19:24  VHM 进程启动  -Builder=WorldPartitionVirtualHeightfieldMeshBuilder
17:53:20  Ran out of memory allocating 47185920 (45.0 MiB)
```


> **注意**：**SVT 的 OOM 已修好**：同一份构建里，SVT 段完整跑完并提交了 7 个产物；新的 OOM 属于 34 分钟后启动的 VHM 进程，与 SVT 无关。


### 5.1 顺带修好的 GC 开关

这次提交里还带了 `FLevelStreamingGCHelper::EnableForCommandlet()`（`WorldPartitionRuntimeVirtualTextureBuilder.cpp:82`）。它针对的不是 mip 链，而是 `PeakUsedPhysical 49.04 GiB` 这个基础占用 —— 不开这个开关时，editor commandlet 下 `PrepareStreamedOutLevelForGC` 直接跳过（`LevelStreamingGCHelper.cpp:237` 的 `(!GIsEditor || bEnabledForCommandlet)` 条件），流出的 WP cell 永不回收。


## 6. VHM 根因：GPU 卡死 → commit 泄漏 737 GB → OOM

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


### 6.1 面包屑指向 `EndUpdateTexture3D`

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


### 6.2 内存指标说明 OOM 是后果

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


### 6.3 两条诱因，按证据强度

| 诱因 | 证据 | 成本 | 能否解释 5 秒超时 |
|---|---|---|---|
| **显卡驱动过期** | `LogRHI: Warning: Out of date driver found. Using: '560.94' Suggested: '610.47'` 与 `Running with bad GPU drivers` 在日志中重复 3 次 | 零代码 | **能** —— `EndUpdateTexture3D` 卡满 TDR 阈值是典型驱动层症状 |
| **VHM 完全没有治理** | `WorldPartitionVirtualHeightfieldMeshBuilder.cpp:50` 直接调静态 `LoadRuntimeVirtualTextureActors`，不经过 `PreBakeRVT()`；`streamingpoolsizemb="0"` 全 mip 常驻 | 已改（见 6.4） | 不能 —— 只影响内存累积 |

TDR 配置为 `Level: Recover, Delay: 2, DdiDelay: 5`，正好对应 5 秒超时阈值。


### 6.4 已改的内容与它解决不了什么

VHM builder 不走 `PreBakeRVT()`，所以 SVT 侧那套治理（GC 开关、`BakeMipBias`、`PoolDeallocateDelayFrame`）**一项都不生效**。本轮给它补上 GC 开关：

```
// WorldPartitionVirtualHeightfieldMeshBuilder.cpp:41-46
FLevelStreamingGCHelper::EnableForCommandlet();
```


> **注意**：**必须说清楚边界**：这一项只缓解该进程 `PeakUsedPhysical 47.42 GiB` 的累积，**不解决 GPU 超时**。如果 GPU 仍在 `EndUpdateTexture3D` 卡满 5 秒，18 分钟的等待照样会把 commit 耗光。已编译通过（exit=0、0 error），但**尚未在构建机验证**。

引擎自己也在代码里承认了 VHM 的结构问题：`WorldPartitionVirtualHeightfieldMeshBuilder.cpp:53` 写着 `// todo[vhm]: Convert builder to sequentially load and build sections of the world.` —— 当前实现一次性加载全世界。


## 7. 本地 12 GB：四层根因与 `r.Streaming.BakeMipBias`

> 这条线在本机崩、在构建机不崩，是独立的一套根因。前三层都能修，第四层修不了 —— 所以方案只能改需求侧。

| 层 | 根因 | 代码/证据 |
|---|---|---|
| **L1 触发** | `StreamAllResources(0)` 逐 tile 调用，`bProcessEverything=true` → 全世界纹理按全 mip 流入 | `ContentStreaming.cpp:736` |
| **L2 不回收** | 池块释放挂在 frame fence 上；tile 循环无帧边界 → `AlivePools == PoolIndex` 恒成立 | `D3D12PoolAllocator.cpp:594/741/777` |
| **L3 放大器** | upload 池再延迟 3600 帧才回收（game 里 60 s 合理，commandlet 全程数百帧 → 永不触发）+ 冗余池预建 | `D3D12Allocation.cpp:1366`、`RHIPoolAllocator.cpp:754` |
| **L4 物理极限** | 只读纹理 12.3 GB > 可用 9,872 MiB；64 MB×192 = 16 MB×768 = 12.3 GB → 与池大小无关，是纹理本体 | 池大小不变性实验 |

结论：前三层可以修（分段补帧 / 池回收 / 门控），第四层修不了 —— 只能降低需求侧，即 mipbias。

*图 6　本地显存崩溃的四层根因*


### 7.1 前三层：为什么池子只增不减

1. **L1 触发**：`RuntimeVirtualTextureBuildStreamingMips.cpp:496` 每个 tile 都调 `IStreamingManager::Get().StreamAllResources(0)`，语义是 `bProcessEverything=true`（`ContentStreaming.cpp:736`）—— 无视限流，把全世界纹理按全 mip 拉进显存。NordLand 的 tile 网格是 128×128 = 16384 个 tile（`TileSize=64`），**第一个 tile 的这一次调用就完成了全量流入。**
2. **L2 不回收**：块释放挂在 `FrameFence.GetNextFenceToSignal()` 上（`D3D12PoolAllocator.cpp:594`、`D3D12PoolAllocator.cpp:741`），只有 `IsFenceComplete` 为真才真正归还 free list（`D3D12PoolAllocator.cpp:777`）；而 frame fence 只在 `RHIEndFrame` 推进，tile 循环全程没有帧边界 → 池零回收。铁证：`AlivePools == PoolIndex` 恒成立（128==128、768==768）。
3. **L3 两处放大器**：项目自定义的 upload big block 池再延迟 3600 帧才回收（`D3D12Allocation.cpp:1366`）—— 游戏里 3600 帧 = 60 秒合理，烘焙 commandlet 全程只跑数百帧，永远触发不了，池涨到 `Pool Index: 1197 × 8 MB ≈ 9.6 GB`；另有冗余池预建（`RHIPoolAllocator.cpp:754`）不校验余量就挂 64 MB 空池。


### 7.2 第四层：物理极限

只读纹理（`HeapType=1`，真显存）总量 12.3 GB。决定性实验是换池大小看总量：

| 池大小 | 池数 | 总量 |
|---|---|---|
| 64 MB | 192 | 12.3 GB |
| 16 MB | 768 | 12.3 GB |

**总量与池大小无关 → 不是碎片，是纹理数据本体。** 显卡 12,282 MiB − 桌面基线 2,410 MiB = 可用 9,872 MiB，需求约 13.3 GB，缺口 ≈ 3.4 GB。这一层代码修不掉。


### 7.3 方案：`r.Streaming.BakeMipBias`，但必须排除地形

`r.Streaming.MipBias` 在烘焙 commandlet 里三层门控全部拦死：

1. `TextureStreamingHelpers.cpp:387`：`GlobalMipBias = !GIsEditor ? ... : 0` —— 编辑器无条件归零。
2. `TextureStreamingHelpers.cpp:415`：`if (bUseAllMips) { GlobalMipBias = 0; }`。
3. `r.Streaming.UsePerTextureBias` 默认 1，使 `!Settings.bUsePerTextureBias` 为假，`GlobalMipBias` 压根不会被加。

所以新增了一个独立 CVar `r.Streaming.BakeMipBias`（默认 0，零影响），施加在以上三层门控的**下游**、`MaxAllowedMips` 钳制之前。


> **注意**：**第一版没有排除地形，直接把产物烤废了**：landscape heightmap / weightmap 也降了 2 级 mip，height 类产物缩水约 70%（`RVT_WorldHeight_For_Materail_SVT` −69%、`RVT_BridgeLandscape_WorldHeight_SVT` −70%、`..._height_pizza_SVT` −72%）。加 `if (!bIsTerrainTexture)` 后全部恢复 —— 这也正是引擎在 `StreamingTexture.h:165-172` 的 `IsMaxResolutionAffectedByGlobalBias()` 里排除地形的理由：地形高度图决定 RVT 的几何精度。


### 7.4 为什么不改 `r.Streaming.PoolSize`

`PoolSize` 大于 0 会开启纹理流送，纹理按低 mip 采样，烤出来的 RVT/SVT 不合格 —— 这条路已被明确否决（「验证通过也没有意义」）。`BakeMipBias` 保留 `PoolSize=0` 的原语义，`Texture pool size now 0 MB` 不变，只是把采样 mip 上限往下压一级。


### 7.5 验收：先建确定性基线，再比 golden

同设置连跑两轮（v30 / v31）先测确定性：5 个产物两轮只差 46~48 字节（全在 package header），`RVT_Nordland_Thumbnail` 波动 ±7 字节，`RVT_BridgeLandscape_SVT` 波动 ±2,481 字节。**这 2 个产物的 MD5 永远不可能复现，不能拿来当验收标准。** 据此归因：

| 产物 | vs golden | 归因 |
|---|---|---|
| `RVT_WorldHeight_For_Materail_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_WorldHeight_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_height_pizza_SVT` | 像素零差异 | 完全等价 |
| `RVT_BridgeLandscape_height_FloatPizza_SVT` | 像素零差异 | 完全等价 |
| `RVT_Nordland_Thumbnail` | +54 字节 | 在 ±7 B 波动量级内，视为等价 |
| `RVT_BridgeLandscape_SVT` | −356,491 字节（−0.068%） | 超出波动，真实但极小 |
| `RVT_WhiteE_Nord_SVT` | −3,558,310 字节（−1.06%） | 该产物确定性，真实 mipbias 影响 |

| mipbias | `WhiteE_Nord_SVT` | `BridgeLandscape_SVT` |
|---|---|---|
| 2 | −11.1 MB（−3.3%） | −876 KB（−0.17%） |
| **1** | **−3.56 MB（−1.06%）** | **−356 KB（−0.068%）** |
| 0 | — | 崩溃（必需项） |

**bias = 1 是最小可行值**：再降一级收益递减，而 0 必崩。


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
# 然后核对产物时间戳
ls -l --time-style=full-iso UE5EA/Engine/Intermediate/Build/Win64/.../Module.VirtualTexturingEditor.cpp.o
```


## 9. 已知问题与后续动作

| 问题 | 影响 | 建议动作 |
|---|---|---|
| VHM 的 GPU 超时未解决 | 驱动不变的话，GC 开关也救不回来 | **先把构建机驱动从 560.94 升到 610.47 再重跑** —— 零代码成本，日志已明确警告 |
| VHM builder 一次性加载全世界 | `PeakUsedPhysical 47.42 GiB`；引擎自己标了 `todo[vhm]` | 中期改造为分块加载，与 SVT 的 `PreBakeRVT` 治理对齐 |
| 单层跳过合并**未在构建机验证** | 只影响单层 RVT 组件，对本次 2 层资产无影响 | 下次构建对照峰值即可 |
| `RVT_WhiteE_Nord_SVT` −1.06% | 属确定性偏差，不是噪声 | 需美术确认这 1% 是否可接受（本地 mipbias 方案） |
| `RVT_BridgeLandscape_SVT` −0.068% | 真实但极小 | 同上 |
| golden 本身未必是「标准答案」 | golden 取自 DDC 冷那轮，纹理可能未完全就位 | 建立一次全热的正式基线，再作后续对比 |


## 10. 取证方法：这套结论是怎么钉死的

> OOM 类问题的坑在于「不崩了」很容易自欺。以下四条是本次真正起作用的取证手段。

1. **用申请量当指纹反解数组尺寸**。`Ran out of memory allocating N` 里的 N 是约束方程：已知增长因子 `11/8 + 16`，反解出 `NewMax`，再与代码里可能的尺寸对表 —— 只可能有一个答案。本次反解出 4,095 MiB = 六个 mip 之和，误差 0 字节。
2. **区分 `UsedVirtual` 与 `UsedPhysical`**。前者是 `PagefileUsage`（进程 commit），后者是工作集。**commit 高而工作集低 = 泄漏**；两者都高 = 真的用掉了。VHM 那次是 737 GB vs 6.66 GB，一眼定性。
3. **做池大小不变性实验区分「碎片」与「数据本体」**。64 MB×192 与 16 MB×768 都是 12.3 GB，说明总量由纹理数据决定，换池尺寸没用。
4. **先建确定性基线，再谈差异**。同设置连跑两轮，测出每个产物的 run-to-run 波动幅度；低于波动幅度的差异不能算「改动引起」，高于的才需要归因。这一条挡掉了「Thumbnail +54 字节也算偏差」这类误判。


### 10.1 踩过的坑

| 坑 | 现象 | 教训 |
|---|---|---|
| `-dpcvars` 传参被 cmd 截断 | 五轮实验全部无效，日志里 `-dpcvars` 后面是空的 | 且 DeviceProfile 会无条件覆盖 `-dpcvars`；要改 CVar 就在 `PreBakeRVT()` 里 `GEngine->Exec` |
| `Reserve` 用未归一化格式 | 预留量低估 4~8 倍，`Reserve` 形同虚设 | 计大小必须与写入方同一口径考察 |
| 过滤 GroundGrid actor | 产物缩小（SVT −12.4 MB），烤丢内容 | 那些 actor 是项目定制烘焙效果需要故意加载的 |
| mipbias 未排除地形 | height 类产物缩水 70% | 地形纹理决定 RVT 几何精度，引擎本来就在 `IsMaxResolutionAffectedByGlobalBias()` 里排除它 |
| 按值遍历漏 `const&` | 每轮整份拷贝一层，单层可达 4 GB | range-for 遇到大容器一律写 `const&` |


## 11. 参考

- 引擎源码：`UE5EA/Engine/Source/Editor/VirtualTexturingEditor/Private/RuntimeVirtualTextureBuildStreamingMips.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Core/Public/Containers/ContainerAllocationPolicies.h`
- 引擎源码：`UE5EA/Engine/Source/Editor/UnrealEd/Private/WorldPartition/WorldPartitionRuntimeVirtualTextureBuilder.cpp`
- 引擎源码：`UE5EA/Engine/Plugins/Experimental/VirtualHeightfieldMesh/Source/VirtualHeightfieldMeshEditor/Private/WorldPartitionVirtualHeightfieldMeshBuilder.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Engine/Private/Streaming/TextureStreamingHelpers.cpp`
- 引擎源码：`UE5EA/Engine/Source/Runtime/Core/Private/Windows/WindowsPlatformMemory.cpp`
- 构建机日志（旧，CL 1227410）：`D:\DownLoad\Misc\1-1-9-E689A7E8A18CE887AAE5AE9AE4B989E6938DE4BD9C(1).log`
- 构建机日志（新，CL 1230415）：`D:\DownLoad\Misc\1-1-9-E689A7E8A18CE887AAE5AE9AE4B989E6938DE4BD9C(2).log`
