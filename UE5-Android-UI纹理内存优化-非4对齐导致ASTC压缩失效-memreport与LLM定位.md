# UE5 Android UI 纹理内存优化：非 4 对齐导致的压缩失效

Unreal Engine 5.5（S1Game / Android Vulkan）大厅运行时内存从 **2698.55 MB** 收敛到约
**1.6～1.8 GB** 的完整排查与实施记录。核心结论：纹理 1955.6 MB 里有 **1122.7 MB**
不是"贴图太多"，而是贴图尺寸不是 4 的倍数，导致引擎强制关闭块压缩、以未压缩 32bpp 驻留。
处理 516 个资产（三个 P4 CL），预估节省 **约 1340 MB**。
第 8 章补充了 **LLM 实测归因**：稳态三项（纹理、UObject、代码段）占 65%；
峰值高出稳态的 1602 MB 与常驻资源无关，来自纹理解码、GPU 上传、文件读取与 GPU 后备内存四个加载期临时缓冲。
<p class="meta">工具链：memreport -full · DumpLLM -CSV · stat LLMFULL · rhi.DumpResourceMemory CSV ·
Unreal MCP（execute_python）· P4 1239353 / 1239799 / 1239904

## 结论与关键数据
大厅纹理 1955.6 MB 里有 1122.7 MB 不是"贴图太多"，而是"贴图没被压缩"——因为尺寸不是 4 的倍数。LLM 实测已确认：纹理、UObject、代码段三项占稳态内存 65%，峰值缺口则来自加载期临时缓冲。
| 批次 | 资产数 | 改前 | 改后（估算） | 节省 | P4 CL |
|---|---|---|---|---|---|
| HeadFrame 头像框（钳 1024 + 4 对齐） | 244 | 1122.7 MB | 42.1 MB | **1080.6 MB** | 1239353 |
| UI 贴图（仅 4 对齐，不改分辨率） | 268 | 240.2 MB | 28.8 MB | **211.4 MB** | 1239799 |
| Skybox 立方体贴图（512→256） | 4 | 64.1 MB | 约 16 MB | **约 48 MB** | 1239904 |
| **合计** | **516** | — | — | **约 1340 MB** | — |

**关键限制**：改后数值全部来自 bpp 推算（ASTC 6×6 = 3.56 bpp），**尚未经 cook 与真机验证**。

**LLM 实测补充（第 8 章）**：用 Development 包 + `-LLM` 补采了一轮，消除了"1560.5 MB 分配器堆是盲区"这一说法。
稳态 RSS 2828.07 MB（含 LLM 自身开销 288.7 MB）。LLM Default 合计 **4066.4 MB** 的构成：

| 项 | 大小 | 占比 |
|---|---|---|
| 纹理（`VulkanTextures` + `VulkanRenderTargets`） | **1967.7 MB** | 48% |
| `UObject` | 364.1 MB | 9% |
| `ProgramSize`（代码段） | 330.4 MB | 8% |
| 其余 12 项合计 | 约 1404 MB | 35% |

**峰值 4430.36 MB 与常驻资源无关**：纹理峰值仅涨 100 MB，缺口来自 `TextureMetaData`（1454.2）、
`VulkanStagingBuffers`（1350.0）、`FileSystem`（1331.1）、`VulkanSpareMemoryGPU`（1304.0）
四个加载期临时缓冲（各自独立峰值，不可累加）。

一句话根因：

```
尺寸 2047x2040  →  2047 % 4 = 3  →  bNoCompression = true  →  PF_B8G8R8A8 32bpp  →  15.98 MB/张
同规格但 4 对齐的纹理                                     →  PF_ASTC_6x6 3.56bpp →  约 1.8 MB/张
```
## 运行时内存怎么量：三层口径
进程 RSS 判预算，LLM 判类别，memreport 与资源 CSV 判具体资产。三者口径互相重叠，不能相加。
三层分工：进程 RSS 判预算 → LLM 判类别 → memreport / 资源 CSV 判具体资产。

> 口径提示：本章示例数字取自**未开 LLM** 的原始报告（15:33 那次）；第 8 章是开 `-LLM` 后的实测
> （RSS 2828.07 MB，含 LLM 自身开销 288.7 MB）。两批数字**不可直接比较**。

**2.1 进程口径**

Windows：`FPlatformMemory::GetStats()` 的 `UsedPhysical` 取 `WorkingSetSize`，`UsedVirtual` 取
`PagefileUsage`（commit charge，不是虚拟地址空间），见 `WindowsPlatformMemory.cpp:466`。

Android：`UsedPhysical` 来自 `/proc/self/status` 的 `VmRSS`，`PeakUsedPhysical` 来自 `VmHWM`，
见 `AndroidPlatformMemory.cpp:266`；`UsedVirtual` 是 `VmSize`，不要当实际占用。

```
Process Physical Memory: 2698.55 MB used, 4303.75 MB peak
Physical Memory: 8640.35 MB used, 2535.75 MB free, 11176.10 MB total
```

**2.2 分配器口径**

`FMallocBinned2`：Small Pool 897.9 MB + Large Pool 590.5 MB = 向 OS 申请 **1560.5 MB**，
其中 **342.9 MB 是空闲缓存页**，释放的内存在这里挂着，RSS 不会立刻降。

**2.3 资源口径**

`rhi.DumpResourceMemory all -csvfile` → 34941 条资源、合计 2233.2 MB，是唯一能按名字/尺寸/格式/流送标记筛选的清单。

**口径陷阱**：1560.5 + 1955.6 = 3516 MB > RSS 2698.55 MB，说明纹理与堆统计存在重叠，
"贴图少 1 GB"不等于"RSS 少 1 GB"。
## memreport -full 到底列了什么
它不是"一个内存数字"，而是按 ini 脚本执行的一次取证快照：先 GC，再逐条 dump 到文本文件。
**执行方式**：先转成延迟命令，帧末依次 `FlushAsyncLoading()` → `CollectGarbage()` →
`FlushRenderingCommands()`（`UnrealEngine.cpp:8033`），所以结果是 **GC 之后的存活对象，不是峰值**。
输出到 `MemReports/`（`UnrealEngine.cpp:8043`），子目录名 `MapName-Platform-MM.DD-HH.MM.SS`
（`ProfilingHelpers.cpp:166`），每次执行一个新目录。

**内容由 ini 决定**：`-full` 置 profile 为 `FULL`，读 `[MemReportFullCommands]`
（`UnrealEngine.cpp:8016-8024`）；不带参数读 `[MemReportCommands]`；两段定义在
`BaseEngine.ini:2264` 与 `BaseEngine.ini:2301`。

本次执行 **45 条命令**、14896 行、2.67 MB，耗时约 6 秒：

| 分组 | 命令 | 产出 |
|---|---|---|
| 进程与底层 | `Mem FromReport` | 平台内存、Binned2、FMemStack、FPageAllocator、NameTable、AssetRegistry、memory 类 stat |
| RHI 资源 | `rhi.DumpMemory`、`rhi.DumpResourceMemory` + 19 条 summary | 类型汇总 + Lumen/Nanite/Shadow/GPUScene/VB·IB 分项 |
| 资产清单 | `listTextures`、`listStaticMeshes`、`obj list` | 逐资产：尺寸、格式、LODGroup、流送、mips |
| 其他 | `LLMSnapshot`、`r.DumpRenderTargetPoolMemory` | LLM 快照、RT 池 |

**注意**：本次 `LLMSnapshot` 段落为空——该命令只调 `PublishDataSingleFrame()`，帮助文本要求
`-llmdisableautopublish`（`LowLevelMemTracker.cpp:974-980`），LLM 未开启时没有输出。
## LLM 是什么、能看什么、Android 上为什么默认没开
LLM = Low-Level Memory Tracker，按"分配来源 + 标签"给内存分类；它不告诉你调用栈，也不等于进程占用。
LLM = Low-Level Memory Tracker，按"分配来源 + 标签"分类内存。它不给调用栈，也不等于进程占用。

```
Default tracker  ← FMemory::Malloc 的分配（纹理 / UObject / 渲染目标），子集
Platform tracker ← 更底层的 OS 分配（含 Binned2 内部池），超集
两者有包含关系，不能相加。
```

**控制台命令**

```
stat LLM / stat LLMFULL / stat LLMPlatform / stat LLMOverhead
-LLMCSV   → 逐行写 CSV 到 Saved/Profiling/LLM/（LowLevelMemTracker.cpp:6578）
```

本分支写盘间隔默认 **1 秒**（`LowLevelMemTracker.cpp:956`），比文档说的 5 秒更密。

**Android 为什么默认没开**：引擎的默认开启定制带 `PLATFORM_DESKTOP` 条件
（`LowLevelMemTracker.cpp:2426`），Android 上为假，所以真机必须显式加 `-LLM`。
本次包命令行没有该参数，因此 `LLMSnapshot` 为空、`stat LLMFULL` 无法分类。

**逐资产标签不可用**：`LLM_ALLOW_ASSETS_TAGS` 固定为 0（`LowLevelMemTracker.h:37`），
传 `-llmtagsets=Assets` 只会得到警告（`LowLevelMemTracker.cpp:2526`），要开启必须改宏并重编引擎。

**不要**手动定义 `ENABLE_LOW_LEVEL_MEM_TRACKER`：它是派生宏（`LowLevelMemTracker.h:21`），直接定义会 `#error`。
## 根因：尺寸非 4 倍数导致块压缩被引擎跳过
块压缩按 4×4 宏块工作，引擎对"任一维不是 4 的倍数"的纹理直接关闭压缩，与纹理组、压缩设置都无关。
块压缩按 4×4 宏块工作，任一维不是 4 的倍数时引擎直接关闭压缩：

```cpp
// Texture.cpp:4124-4133
if ( (SizeX < 4) || (SizeY < 4) || (SizeX % 4 != 0) || (SizeY % 4 != 0) )
{
    bNoCompression = true;
}
```

判断用的是**构建后尺寸**（`Texture.cpp:4121` 调用 `GetBuiltTextureSize`），已叠加 MaxTextureSize、
LODBias、PowerOfTwoMode，不是源图尺寸。

**对照实验**（压缩设置都是 `TC_BC7`，唯一变量是能否被 4 整除）：

| 分类 | 张数 | 合计 | 真机格式 |
|---|---|---|---|
| 尺寸非 4 倍数 | **245** | **1117.8 MB** | `PF_B8G8R8A8` 未压缩 |
| 尺寸 4 对齐 | 34 | 7.5 MB | `PF_ASTC_6x6` 已压缩 |

对照组含 `2016×1676`、`1428×1032`、`1024×968` 共 17 张**非 2 的幂但 4 对齐**的纹理，全部压缩成功。

**门槛是 4 的倍数，不是 2 的幂。**
## 改尺寸的两种机制：为什么 MaxTextureSize 不可用
MaxTextureSize 的实现是"逐次减半"，会把 2047 变成 1023（仍不是 4 的倍数）；要精确控制必须走 ResizeToSpecificResolution。
**MaxTextureSize 是逐次减半**：

```cpp
// Texture.cpp:4010-4021
while( SizeX > MaxTextureSize || SizeY > MaxTextureSize )
{
    SizeX = FMath::Max(SizeX>>1,1);
    SizeY = FMath::Max(SizeY>>1,1);
}
```

`2047×2040` 设 `MaxTextureSize=1024` → `1023×1020`，**1023 % 4 = 3**，压缩依旧不生效。
这条路只适合本就是 2 的幂的纹理（512→256 恰好整除）。

**精确指定尺寸**（`TextureBuildUtilities.cpp:335-344`）需要同时设三个属性：
`power_of_two_mode = RESIZE_TO_SPECIFIC_RESOLUTION` + `resize_during_build_x` + `resize_during_build_y`。
只设后两个无效。

**移动端专用旋钮**：本分支在 `UTexture` 上加了 `MoblieMaxTextureSize`（`Texture.h:1347`，拼写如此），
消费点在 `TextureLODSettings.cpp:152` 与 `Texture2D.cpp:1589`；但受 `UsesMobileMaxTextureSize` 门控，
项目所有 profile 都设为 False（`DefaultDeviceProfiles.ini:1775`），所以现在填值不生效。
开启开关本身安全（全项目该值当前均为 0），随后逐资产填值即可只压移动端。

**代价**：两个机制都是全平台构建设置，桌面端会一起缩小。
## 三批资产的实际改动与收益
统一规则：等比缩放到长边上限 → 两轴就近/向下取整到 4 的倍数；任一维 <64 的资产不动。
**统一规则**

```
目标尺寸 = 等比缩放到长边上限 → 两轴取整到 4 的倍数 → 上限 1024、下限 4
豁免 = 任一维 < 64 直接跳过
只改 build setting，不修改源图
```

**7.2 HeadFrame 头像框（CL 1239353）**

| 项 | 数据 |
|---|---|
| 原始占用 | 279 张 / 1125.3 MB；274 张 `TEXTUREGROUP_UI`，5 张 `World` 占位 0 MB |
| 流送 / mip | 100% 非流送；100% `NumMips=1` |
| 单张极值 | 2046×2046 无压缩 = 15.98 MB |
| 处理 | 244 张（3 张 <64 豁免），钳到长边 1024 + 4 对齐，改后 99.30 Mpx |
| 收益 | 1122.7 → 42.1 MB，省 **1080.6 MB（96.3%）** |

**7.3 UI 贴图仅对齐（CL 1239799）**

范围：`/Game/Arts/UI/**` 与 `/Game/__Mobile__/Arts/UI/**` 中未压缩、非 4 对齐、≥64×64 的 268 张，
**排除已处理的 HeadFrame**。

| 项 | 数据 |
|---|---|
| 命中 | 268 张 / 240.2 MB，覆盖 27 个 UI 子目录 |
| 主要来源 | `Icon` 156.8 MB（164 张）、`VXSpriteSheets` 19.6 MB、`Shop` 17.1 MB、`Login` 11.4 MB、`VX2DTexture` 9.9 MB、`Common` 5.7 MB |
| 处理 | 向下取整到 4 的倍数，不改分辨率（最大像素改动 3px） |
| 收益 | 240.2 → 28.8 MB，省 **211.4 MB** |

典型尺寸 `468×854`（63 张）与 `548×818`（24 张）：宽度合规、**高度差 2 像素**，
整张 1.6～1.75 MB 的图标就变成未压缩。

**7.4 Skybox 立方体贴图（CL 1239904）**

| 项 | 数据 |
|---|---|
| 资产 | 4 张 `TextureCube`，512×512，压缩设置已是 `TC_HDR_COMPRESSED` |
| 真机格式 | `PF_FloatRGBA` 未压缩浮点（Android cook 未映射成 ASTC HDR） |
| 处理 | `max_texture_size=256`（512→256 一次减半，无重采样） |
| 收益 | 16.03 → 约 4.0 MB/张，合计 64.1 → 约 16 MB，省 **约 48 MB** |
## LLM 实测：2828 MB 到底分给了谁
按开发包 + `-LLM` 实测补齐归因。纹理 1967.7 MB、UObject 364.1 MB、代码段 330.4 MB 三项占 65%；峰值缺口 1602 MB 与常驻无关，来自四个加载期临时缓冲。
**采集条件**：Development 包 + `-LLM`（`setprop` 追加，无需重出包）；
20:10:13 `memreport -full`（内部先 GC）→ 20:11:03 `DumpLLM -CSV`。
RSS 稳态 **2828.07 MB**、峰值 **4430.36 MB**；LLM Default 合计 4066.4 MB；
`LLMOverhead` **288.7 MB**，与历史数字比较必须扣除。

> 三条口径红线：① Development 包含控制台开销，不等于线上包；② LLM Default Total 4066.4 大于 RSS 2828.07，
> 因为 LLM 也统计 GPU 侧分配，两者不能互相当成对方；③ LLM 未开启时 `DumpLLM` 静默返回，无任何输出。

**稳态构成（GC 后）**

| LLM 标签 | 当前 | 峰值 | 说明 |
|---|---|---|---|
| `VulkanTextures` | **1656.1** | 1756.1 | 纹理 |
| `UObject` | **364.1** | 407.8 | UClass / CDO / UPackage / 组件 |
| `ProgramSize` | **330.4** | 330.4 | 代码段，结构性不可削 |
| `VulkanRenderTargets` | 311.6 | 311.6 | 含 Bink 151 MB、TAA.History 29.6 |
| `VulkanBuffers` | 285.3 | 288.0 | 顶点 / 蒙皮 / 索引，无单点大户 |
| `VulkanSpareMemoryGPU` | 161.6 | **1304.0** | GPU 后备内存 |
| `CustomName` | 152.5 | 285.7 | 项目自定义 BYTAG |
| `FMallocUnused` | 131.7 | 242.7 | 已申请未分配 |
| `Shaders` | 124.4 | 146.7 | shader 代码 |
| `Audio` | 123.1 | 123.1 | SoundBanks **104.0** |
| `EngineMisc` | 116.8 | 116.9 | ConsoleCommandsBuildList **107.9** |
| `FileSystem` | 76.6 | **1331.1** | 文件读取缓存 |
| `ConfigSystem` | 75.6 | 75.6 | 配置项 |
| `Physics` | 59.8 | 59.8 | — |

分配器侧：`FMallocBinned2` 向 OS 申请 **1557.74 MB**（Small 968.5 + Large 586.0），
其中 **323.3 MB 为空闲缓存页**，可复用、不算泄漏。

**三套纹理口径对账**

| 口径 | 数值 | 性质 |
|---|---|---|
| `listTextures` | 1627.4 MB / 3628 条 | 只含 UTexture 资产 |
| `rhi.DumpResourceMemory` CSV | **1987.6 MB / 5358 张** | 全部 Resident，含非资产纹理 |
| LLM `VulkanTextures` + `VulkanRenderTargets` | 1656.1 + 311.6 = **1967.7** | GPU 实际分配 |

后两者差 **19.9 MB（1%）**，口径一致可互相印证；`listTextures` 少的 360 MB 是
Bink 视频帧（154.4 MB / 61 张）与 TAA.History、CaptureCube 这类不挂资产的对象。

纹理内部（未 cook，为优化前状态）：HeadFrame **1125.1 MB / 269 张**、
UI/Icon 164.1 MB / 460 张、其他 UI 139.1 MB、Skybox 101.8 MB、
Hall 45.2 MB、Character 14.5 MB。仍有 **743 张 / 1372.5 MB 非 4 对齐**。

**峰值缺口与常驻无关**

| 标签 | 稳态 | 峰值 | 性质 |
|---|---|---|---|
| `TextureMetaData` | 2.8 | **1454.2** | 纹理解码中间数据 |
| `VulkanStagingBuffers` | 4.1 | **1350.0** | GPU 上传中转 |
| `FileSystem` | 76.6 | **1331.1** | 文件读取缓存 |
| `VulkanSpareMemoryGPU` | 161.6 | **1304.0** | GPU 后备内存 |
| `Untracked` | 0.0 | 477.8 | 未打点 |

纹理本身峰值仅涨 100 MB。低内存机型真正 OOM 的是加载瞬间；这四项都与上传的纹理数据量成正比，
所以降纹理体积会同时压低峰值。

**四个此前看不见的开销**

1. Bink 视频纹理 154.4 MB / 61 张，3 张占 151.1 MB（`MT_SelectMode_Hero09` 63.38、`Hero02` 63.38、`BR_BG` 24.38），全部非流送。
2. `ConsoleCommandsBuildList` 107.9 MB，控制台自动补全列表；**仅 Development 包**。
3. Wwise SoundBanks 104.0 MB 常驻。
4. `CustomName` 152.5 MB（峰值 285.7），项目 BYTAG，集中在 `Animation_S1*Data` 技能数据资产。

**优化空间**

| 项 | 当前 | 预期 | 状态 |
|---|---|---|---|
| HeadFrame 244 张 | 1125.1 MB | 约 42 MB | 已改，待 cook |
| UI 对齐 268 张 | 约 164 MB | 约 29 MB | 已改，待 cook |
| Skybox 4 张 | 64.1 MB | 约 16 MB | 已改，待 cook |
| **小计** | **约 1353 MB** | **约 87 MB** | 省约 1266 MB |
| Bink 3 张视频 | 151.1 MB | 待评估 | 未动 |

若全部兑现，稳态理论上限约 **1.56 GB**，实际预期 **1.6～1.8 GB**。
## Skybox 立方体贴图为什么进包
不是"没用到"，而是被 38 个 UI 材质硬引用，外加一个 PrimaryAssetLabel 把它们标进 chunk。
| 资产 | 直接引用者 | 典型引用者 |
|---|---|---|
| `T_Vehicle_03_Cube` | **41** | `MI_Vehicle_Paint_UI`、`MIC_s12k_NPR_OP_UI` |
| `T_ShanningCube` | **19** | `MI_Weapon_Remapping_*`、`MIC_buttstock` |
| `T_ShanningCube_02` | **16** | `MI_Weapon_1p`、`MI_Weapon_Heirlooms_*` |
| `T_BossSKY_Cube_02` | **3** | `BP_Rush02_ParentEnvLight_Boss_02_New` 等环境光蓝图 |

前三个合计被 **38 个 `*_UI` 材质**引用，做的是角色/载具展示界面的环境反射。
大厅只要有 UI 预览，材质一加载，cube 就跟着进来。

**进包与"用不用"无关**：`/Game/AssetDatas/GeneratePatch/Chunk1_Content` 的类是 `PrimaryAssetLabel`，
把资产框进指定 chunk，cook 阶段强制包含。`T_BossSKY_Cube_02` 只被 2 个环境光蓝图引用却也在包里，原因在此。

**不释放的原因**：4 张在 `TEXTUREGROUP_Skybox` 下 `Streaming=NO`，整张 mip 链常驻。

可做的三件事：① UI 材质改用共享低精度 cube（128² 足够）；② 检查 `Chunk1_Content` 是否必须包含；
③ `T_BossSKY_Cube_02` 的蓝图默认值改 soft reference。

**边界**：静态引用图只证明"可能被加载"，确定具体触发链需要 LLM 加载记录或 Memory Insights 加载栈。
## 已验证、未验证与已知坑
资产侧改动已验证到属性与文件层；cooked 结果与真实内存收益尚未验证。
**已验证**

- 516 个资产全部写入成功，0 失败 0 跳过；保存前逐个读回 `resize_during_build_x/y` 与 `power_of_two_mode` 校验。
- 文件级：全部 `.uasset` mtime 落在批次窗口内，0 缺失；豁免资产未被触碰。
- 属性抽查：`shengdanjie` → 1024×1020、`T_Sleepcoat_Frame1` → 1024×1024、`LKLS/Blue/kuang` → 1024×1024、
  `kuang_2` → 1024×852、11×11 豁免项仍 `mode=NONE`。
- 全部目标尺寸 `%4==0` 且 `≤1024`（Skybox 为 256）。
- 第 8 章 LLM 快照与 memreport 交叉：纹理口径 1967.7 vs 1987.6，差 1%。

**未验证**

- cooked 后的真实格式与尺寸：所有"改后 MB"都是 bpp 推算，未经 cook。
- RSS 真实下降幅度：受口径重叠、分配器缓存、LODBias 影响，预期 1.6～1.8 GB 而非理论 1358 MB。
- 第 8 章 LLM 数据是"优化前"状态：三批资产未 cook，1125.1 MB 的 HeadFrame 仍原样在报告里。
  LLM 验证的是结构，不是收益。

**已知坑**

1. `blueprint_get_texture_source_disk_and_memory_size()` 返回 `(disk, mem)`，源是压缩格式时 `disk ≠ mem`，必须用 `mem`。
2. 尺寸反推搜索中心要取 `sqrt(px × aspect)`，不是 `isqrt(px)`（后者只适用近方形）。
3. `execute_python` 单批 30 个资产会响应超时（活已干完但拿不到回执），20 个/批稳定。
4. `rhi.DumpMemory` 的 stat 行在本包不可信：报 Vertex Buffer 26853 MB（26 GB）远超设备 11 GB；
   同次 CSV 合计仅 2233.2 MB，**以 CSV 为准**。
5. 部分资产运行尺寸被 LODBias 减半（源 1143×3260、运行时 571×1630），这类按真实源尺寸对齐，避免误降画质。
6. `DumpLLM` 必须写 `-CSV`（带横杠），引擎用 `FParse::Param` 解析；写 `DumpLLM CSV` 只输出 PlainText。
7. Android 上 LLM 不会自动开：默认开启定制带 `PLATFORM_DESKTOP` 条件（`LowLevelMemTracker.cpp:2426`），
   真机必须在命令行加 `-LLM`。
8. 加 `-LLM` 用 `setprop debug.ue.commandline`（追加语义，`LaunchAndroid.cpp:520`，仅非 Shipping）。
   **不要用 `UECommandLine.txt`**：它先 `FCommandLine::Set(TEXT(""))` 清空原命令行（`LaunchAndroid.cpp:505-508`）。
9. `LLMOverhead` 必须扣除，本次 288.7 MB；开 LLM 前后两批数字不可直接比较。
10. `LLM Default Total` ≠ RSS（4066.4 vs 2828.07），LLM 也统计 GPU 侧分配。
11. `-llmtagsets=Assets` 无效，本分支 `LLM_ALLOW_ASSETS_TAGS=0`（`LowLevelMemTracker.h:37`）。
## 附录：复现命令与脚本
从抓数据到批量改资产的最小可复现路径。
**采集**

```
# -full 这类带横线参数要用 -- 分隔
python send_cvar.py com.sarosgame.S1Game S1Game -- memreport -full
# 判据看日志，不看退出码（memreport 不是 CVar，必报 UNVERIFIED）
adb shell "grep -a 'MemReportDeferred' .../Saved/Logs/S1Game.log"
```

**采集 LLM（分类归因，无需重新出包）**

```bash
# 1) 追加 -LLM（setprop 是追加语义，仅非 Shipping 生效）
adb shell setprop debug.ue.commandline "-LLM"; adb shell getprop debug.ue.commandline
# 2) 冷启，LLM 只在进程启动时初始化
adb shell am force-stop com.sarosgame.S1Game
adb shell am start -n com.sarosgame.S1Game/com.epicgames.unreal.SplashActivity
# 3) 验证
adb shell "grep -a 'LLM enabled' .../Saved/Logs/S1Game.log"
# 4) 稳定后按顺序：memreport -full（内部先 GC）→ DumpLLM -CSV（必须带横杠）
python send_cvar.py com.sarosgame.S1Game S1Game -- memreport -full
python send_cvar.py com.sarosgame.S1Game S1Game -- DumpLLM -CSV
# 5) 取数：表头 '[time][frame],TagName,SizeMB,PeakMB,Tracker,PathName'
adb shell "grep -an 'PeakMB,Tracker' .../Saved/Logs/S1Game.log"
# 6) 采完清掉，否则后续性能测试一直背着 LLM 开销
adb shell setprop debug.ue.commandline ""
```

**批量改资产**

```python
import unreal
t = unreal.EditorAssetLibrary.load_asset(path)
t.set_editor_property('power_of_two_mode',
    unreal.TexturePowerOfTwoSetting.RESIZE_TO_SPECIFIC_RESOLUTION)
t.set_editor_property('resize_during_build_x', 1024)
t.set_editor_property('resize_during_build_y', 1020)
t.set_editor_property('max_texture_size', 0)
unreal.EditorAssetLibrary.save_loaded_asset(t, False)
```

**脚本与产物**

| 文件 | 用途 |
|---|---|
| `headframe_resize.py` / `_todo.json` / `_done.json` | HeadFrame 批处理（幂等可续跑） |
| `ui_align.py` / `_todo.json` / `_exempt.json` | UI 对齐批处理 |
| `headframe_resize_result.csv` / `ui_align_result.csv` | 逐资产前后对比清单 |

**参考**

- Memory Insights：https://dev.epicgames.com/documentation/en-us/unreal-engine/memory-insights-in-unreal-engine
- Low Level Memory Tracker：https://dev.epicgames.com/documentation/en-us/unreal-engine/using-the-low-level-memory-tracker-in-unreal-engine

