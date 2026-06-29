# FateTrigger 单帧三角面数统计报告

> 捕获文件 `TDM8Gen3.rdc` · API: Vulkan · Frame 7779 · 统计时间 2026-06-28

## 核心指标概览

| 指标 | 数值 |
| --- | --- |
| Draw 操作总数 | 600 |
| 三角面渲染总量 | 948,962 tris |
| 索引总数 | 2,197,057 |
| 渲染 Pass 数 | 37 |

> ⚠ **重要说明：** 此处 **948,962** 是**本帧 GPU 实际提交的三角形渲染吞吐量**，并非场景中唯一几何体的面数。同一个 Mesh 通常会被多个 Pass 重复绘制（Depth Prepass → BasePass → Velocity → ShadowDepth → Outline 等），因此该数值已包含重复计数。计算口径：`三角形 = floor(索引数 / 3) × 实例数`（按索引三角形列表）。Indirect 绘制(⚡)的索引数为 RenderDoc 从间接缓冲读取的实际值。

## 按渲染 Pass 汇总

| 渲染 Pass（最内层 Marker） | Draw 数 | 三角面数 | 占比 |
| --- | ---: | ---: | ---: |
| MobileBasePass | 222 | 403,031 | 42.5% |
| MobileRenderPrePass | 204 | 333,790 | 35.2% |
| MMHRenderShadowDepths(Non-Nanite) | 2 | 65,884 | 6.9% |
| Velocity | 14 | 58,653 | 6.2% |
| ShadowDepthPass | 8 | 42,118 | 4.4% |
| Mobile_PreOutline_Pass | 7 | 41,398 | 4.4% |
| Translucency | 5 | 2,330 | 0.2% |
| ElementBatch | 91 | 1,028 | 0.1% |
| CanvasBatchedElements | 1 | 672 | 0.1% |
| PerObject BP_GameCharacter_C_2147465690 | 1 | 12 | 0.0% |
| DownsampleHZB(mip=0) 512x256 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=1) 256x128 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=2) 128x64 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=3) 64x32 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=4) 32x16 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=5) 16x8 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=6) 8x4 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=7) 4x2 | 3 | 3 | 0.0% |
| DownsampleHZB(mip=8) 2x1 | 3 | 3 | 0.0% |
| MP_Map_1.BP_ParentCommonEnvLight1_Cloud | 1 | 2 | 0.0% |
| AmbientOcclusion_HorizonSearchIntegral 507x252 (PS) | 1 | 1 | 0.0% |
| AmbientOcclusion_SpatialFilter 507x252 (PS) | 1 | 1 | 0.0% |
| MMH::ShadowMapProjection | 1 | 1 | 0.0% |
| MobileToonOutlinePass | 1 | 1 | 0.0% |
| BloomSetup 254x126 (PS) | 1 | 1 | 0.0% |
| BloomDown 127x63 (PS) | 1 | 1 | 0.0% |
| BloomDown 64x32 (PS) | 1 | 1 | 0.0% |
| BloomDown 32x16 (PS) | 1 | 1 | 0.0% |
| BloomDown 16x8 (PS) | 1 | 1 | 0.0% |
| BloomUp 32x16 (PS) | 1 | 1 | 0.0% |
| BloomUp 64x32 (PS) | 1 | 1 | 0.0% |
| BloomUp 127x63 (PS) | 1 | 1 | 0.0% |
| SunMerge 254x126 (PS) | 1 | 1 | 0.0% |
| TAA(Main Quality=High) 1014x504 -> 1014x504 | 1 | 1 | 0.0% |
| Tonemap 1014x504 (PS GammaOnly=0) | 1 | 1 | 0.0% |
| Upscale(PrimaryToOutput Method=1) | 1 | 1 | 0.0% |
| CopyImageToBackBuffer | 1 | 1 | 0.0% |
| **合计** | **600** | **948,962** | **100%** |

## 三角面数分布（按 Draw 大小分桶）

| 单 Draw 三角面区间 | Draw 数 | 三角面合计 | 占比 |
| --- | ---: | ---: | ---: |
| ≥20k | 13 | 402,628 | 42.4% |
| 5k–20k | 27 | 275,672 | 29.0% |
| 1k–5k | 81 | 204,192 | 21.5% |
| <1k | 479 | 66,470 | 7.0% |

> 少数重型 Draw 贡献了绝大部分三角面，是几何剔除/LOD 优化的重点对象。

## Top 30 重型 Draw 操作

| EventID | 渲染 Pass | 索引数 | 实例 | 三角面数 | 间接 |
| ---: | --- | ---: | ---: | ---: | :---: |
| 729 | MMHRenderShadowDepths(Non-Nanite) | 49,338 | 4 | 65,784 | ⚡ |
| 1769 | MobileRenderPrePass | 109,098 | 1 | 36,366 | |
| 3443 | MobileBasePass | 109,098 | 1 | 36,366 | |
| 1789 | MobileRenderPrePass | 90,273 | 1 | 30,091 | |
| 3463 | MobileBasePass | 90,273 | 1 | 30,091 | |
| 797 | ShadowDepthPass | 88,110 | 1 | 29,370 | |
| 2046 | Velocity | 88,110 | 1 | 29,370 | |
| 2377 | Mobile_PreOutline_Pass | 88,110 | 1 | 29,370 | |
| 3567 | MobileBasePass | 88,110 | 1 | 29,370 | |
| 1794 | MobileRenderPrePass | 68,199 | 1 | 22,733 | |
| 3468 | MobileBasePass | 68,199 | 1 | 22,733 | |
| 1799 | MobileRenderPrePass | 61,476 | 1 | 20,492 | |
| 3473 | MobileBasePass | 61,476 | 1 | 20,492 | |
| 1779 | MobileRenderPrePass | 57,384 | 1 | 19,128 | |
| 3453 | MobileBasePass | 57,384 | 1 | 19,128 | |
| 1748 | MobileRenderPrePass | 50,391 | 1 | 16,797 | |
| 3421 | MobileBasePass | 50,391 | 1 | 16,797 | |
| 2067 | Velocity | 49,338 | 1 | 16,446 | |
| 2627 | MobileBasePass | 49,338 | 1 | 16,446 | |
| 1920 | MobileRenderPrePass | 6,708 | 7 | 15,652 | |
| 3683 | MobileBasePass | 6,708 | 7 | 15,652 | |
| 3553 | MobileBasePass | 16,572 | 2 | 11,048 | |
| 1784 | MobileRenderPrePass | 32,985 | 1 | 10,995 | |
| 3458 | MobileBasePass | 32,985 | 1 | 10,995 | |
| 1835 | MobileRenderPrePass | 6,354 | 4 | 8,472 | |
| 944 | MobileRenderPrePass | 1,800 | 14 | 8,400 | |
| 2874 | MobileBasePass | 1,800 | 14 | 8,400 | |
| 1347 | MobileRenderPrePass | 22,590 | 1 | 7,530 | |
| 3242 | MobileBasePass | 22,590 | 1 | 7,530 | |
| 908 | MobileRenderPrePass | 3,660 | 6 | 7,320 | |

> 完整每-Draw 明细见同目录 CSV：`FateTrigger_TDM8Gen3_三角面数统计.csv`（共 600 行）
