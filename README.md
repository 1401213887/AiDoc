# 📚 E:\AiDoc 技术知识库 · 总导航

> UE 移动端渲染技术知识库。汇集 TBDR 片上优化方法论、头部手游渲染拆解、引擎源码级分析、崩溃定位、Profiling 工具链与项目专项报告。
>
> - **互链约定**：全部相对路径，文件不移动，与 git 自动备份兼容。
> - **本页由 `generate_index.py` 自动生成**，新增文档后重跑脚本即可刷新。
> - 文档总数：**174** · 更新：2026-09-29

---

## 🗂 分类目录

| # | 类目 | 文档数 | 说明 |
|---|------|:---:|------|
| 01 | [01 · TBDR 与片上优化方法论](#01-TBDR-与片上优化方法论) | 31 | TBDR 原理、片上缓存、Subpass/Imageblock、HZB、Forward/Deferred 选型——方法论纵贯线 |
| 02 | [02 · 头部手游案例库](#02-头部手游案例库) | 17 | 单款手游移动端渲染拆解（html）。方法论的具体落地参照 |
| 03 | [03 · 专题横向汇总](#03-专题横向汇总) | 11 | 跨游戏横向对比：半透明 / 遮挡剔除 / DrawCall / FPS 全景 |
| 04 | [04 · 引擎源码级分析](#04-引擎源码级分析) | 18 | PVS、视锥剔除、WorldPartition、TaskGraph、线程池、RDG 等源码深挖 |
| 05 | [05 · 崩溃与稳定性](#05-崩溃与稳定性) | 4 | 崩溃定位与修复：VT / SkeletalMesh / UseAfterFree、帧率掉档排查 |
| 06 | [06 · Profiling 工具与教程](#06-Profiling-工具与教程) | 11 | 高通 SDP / Adreno / Snapdragon Profiler、UE Insights、CPU Trace 工具链 |
| 07 | [07 · 项目专项分析](#07-项目专项分析) | 2 | 具体项目（FateTrigger 等）的单帧 / 纹理 / 三角面分析、AO 实践报告 |
| 99 | [99 · 其它与原始资料](#99-其它与原始资料) | 80 | 未归类资料、大体积归档报告、原始数据（docx/csv/pdf） |

---

## 01 · TBDR 与片上优化方法论

> TBDR 原理、片上缓存、Subpass/Imageblock、HZB、Forward/Deferred 选型——方法论纵贯线

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [UE5-Mobile-GPU-Time统计口径-stat-unit与stat-gpu差异原因-TBDR精度限制](./UE5-Mobile-GPU-Time统计口径-stat-unit与stat-gpu差异原因-TBDR精度限制.html) | HTML | 09-29 |
| [UE5 Mobile TonemapSubpass — SceneColor Memoryless 带宽优化复盘](./UE5-Mobile-TonemapSubpass-SceneColor-memoryless-带宽优化复盘.md) | MD | 08-28 |
| [UE-Mobile-TonemapSubpass-真机viewport变小-ScreenPercentage不匹配根因与规避方案](./UE-Mobile-TonemapSubpass-真机viewport变小-ScreenPercentage不匹配根因与规避方案.md) | MD | 08-27 |
| [UE Mobile：HZB / Occlusion 控制 CVar 全开关参考](./UE-Mobile-HZB-Occlusion-CVar全开关参考.md) | MD | 08-25 |
| [《燕云十六声》手游"片上 GBuffer"渲染技术总结报告](./燕云十六声_片上GBuffer_技术总结.md) | MD | 07-06 |
| [Vulkan Subpass × TBDR 带宽优化 · 系统学习指南](./Vulkan_Subpass_TBDR_带宽优化_学习指南.md) | MD | 06-29 |
| [UE5 HZB（Hierarchical Z-Buffer）实现原理与移动端分析](./UE5_HZB_实现原理与移动端分析_技术文档.md) | MD | 06-29 |
| [iOS / Metal 下实现 TBDR 优化方案：解决方案文档](./iOS_Metal_TBDR_实现方案.md) | MD | 06-23 |
| [UE 移动端 iOS vs Android：平台相关工作量全景对比](./UE_Mobile_iOS_vs_Android_平台工作量对比.md) | MD | 06-23 |
| [Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案（最终版）](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案（最终版）.md) | MD | 06-23 |
| [Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md) | MD | 06-23 |
| [UE Mobile TBDR 优化 — 查漏补缺增补卷（100 轮审阅）](./UE_Mobile_TBDR_查漏补缺增补卷.md) | MD | 06-23 |
| [UE 移动端 Imageblock 与 Tile Shading 落地技术文档](./UE_Mobile_Imageblock_TileShading_落地技术文档.md) | MD | 06-23 |
| [UE Mobile TBDR 优化技术：是否需要改造引擎？落地决策文档](./UE_Mobile_TBDR_改造决策文档.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 完整文档体系入口](./MobileRenderPath/UE_Mobile_Tech_README.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 11：Velocity / LightingCommon / 平台特化](./MobileRenderPath/UE_Mobile_Tech_DeepDive_11_Velocity_Platform.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 10：实战 FAQ + 改造模板](./MobileRenderPath/UE_Mobile_Tech_DeepDive_10_FAQ.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 07：反射系统全谱](./MobileRenderPath/UE_Mobile_Tech_DeepDive_07_Reflection.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 08：Decal / Fog / Sky / Atmosphere](./MobileRenderPath/UE_Mobile_Tech_DeepDive_08_Decal_Fog_Sky.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 09：VertexShader / Material Permutation / Substrate](./MobileRenderPath/UE_Mobile_Tech_DeepDive_09_VertexShader_Material.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 05：虚拟纹理 / 虚拟阴影 / MMH](./MobileRenderPath/UE_Mobile_Tech_DeepDive_05_VirtualTexture.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 06：MeshDrawCommand / GPUScene / InstanceCulling](./MobileRenderPath/UE_Mobile_Tech_DeepDive_06_MeshDrawCommand.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 04：半透明 / SingleLayerWater / Substrate](./MobileRenderPath/UE_Mobile_Tech_DeepDive_04_Translucency.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 02：阴影系统全谱](./MobileRenderPath/UE_Mobile_Tech_DeepDive_02_Shadow.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 03：后处理链](./MobileRenderPath/UE_Mobile_Tech_DeepDive_03_PostProcess.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 深度补充 01：可见性与遮挡剔除](./MobileRenderPath/UE_Mobile_Tech_DeepDive_01_Occlusion.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 实战篇](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Practical.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 源码索引脚手架](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md) | MD | 06-23 |
| [UE Mobile Forward vs Deferred —— 补充篇](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Appendix.md) | MD | 06-23 |
| [UE 移动端 Forward 与 Deferred 管线差异技术文档](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc.md) | MD | 06-23 |
| [UE Mobile Forward 渲染管线代码学习指南](./MobileRenderPath/UE_Mobile_Forward_Pipeline_Study_Guide.md) | MD | 06-23 |

## 02 · 头部手游案例库

> 单款手游移动端渲染拆解（html）。方法论的具体落地参照

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [手游「引擎生成面数」占比实测分析](./引擎生成面数占比-地形与300米外远景-三角洲截帧实测分析.md) | MD | 09-29 |
| [三角洲行动-地形实现方案-CDLOD+RuntimeVirtualTexture-移动端截帧反汇编分析](./三角洲行动-地形实现方案-CDLOD+RuntimeVirtualTexture-移动端截帧反汇编分析.md) | MD | 09-12 |
| [鸣潮 移动端渲染技术要点总结](./鸣潮移动端技术要点总结.md) | MD | 08-19 |
| [绝区零 移动端渲染技术要点总结](./绝区零移动端技术要点总结.md) | MD | 07-18 |
| [蛋仔派对 移动端渲染技术要点总结](./蛋仔派对移动端技术要点总结.md) | MD | 07-18 |
| [第五人格 移动端渲染技术要点总结](./第五人格移动端技术要点总结.md) | MD | 07-18 |
| [王者荣耀 移动端渲染技术要点总结](./王者荣耀移动端技术要点总结.md) | MD | 07-18 |
| [燕云十六声 移动端渲染技术要点总结](./燕云十六声移动端技术要点总结.md) | MD | 07-18 |
| [永劫无间 手游 移动端渲染技术要点总结](./永劫无间手游移动端技术要点总结.md) | MD | 07-18 |
| [《洛克王国：世界》移动端管线设计与优化 — 渲染 Pass / One Pass 技术报告](./洛克王国_pipeline_report.md) | MD | 07-18 |
| [暗区突围 移动端渲染技术要点总结](./暗区突围移动端技术要点总结.md) | MD | 07-18 |
| [崩坏 星穹铁道 移动端渲染技术要点总结](./崩坏星穹铁道移动端技术要点总结.md) | MD | 07-18 |
| [和平精英 移动端渲染技术要点总结](./和平精英移动端技术要点总结.md) | MD | 07-18 |
| [原神 移动端渲染技术要点总结](./原神移动端技术要点总结.md) | MD | 07-18 |
| [光遇 移动端渲染技术要点总结](./光遇移动端技术要点总结.md) | MD | 07-18 |
| [使命召唤手游 移动端渲染技术要点总结](./使命召唤手游移动端技术要点总结.md) | MD | 07-18 |
| [三角洲手游 移动端技术要点总结](./三角洲移动端技术要点总结.md) | MD | 07-18 |

## 03 · 专题横向汇总

> 跨游戏横向对比：半透明 / 遮挡剔除 / DrawCall / FPS 全景

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [UE手游物理方案-PhysX到Chaos内核替换-移动端五层分解与UE4UE5差异](./UE手游物理方案-PhysX到Chaos内核替换-移动端五层分解与UE4UE5差异.html) | HTML | 09-28 |
| [头部 UE 手游特效方案汇总 · 移动端 VFX 技术路线速查](./头部手游特效方案汇总-UE篇.md) | MD | 08-20 |
| [头部手游特效方案汇总-UE篇](./头部手游特效方案汇总-UE篇.html) | HTML | 08-20 |
| [二次元手游 · AA 抗锯齿方案横向专题](./头部手游AA抗锯齿方案汇总.md) | MD | 08-20 |
| [头部手游降低 Draw Call 方案汇总](./头部手游降低DrawCall方案汇总.md) | MD | 07-18 |
| [头部手游移动端遮挡剔除方案汇总](./头部手游移动端遮挡剔除方案汇总.md) | MD | 07-18 |
| [头部手游 · 画质分级方案汇总](./头部手游画质分级方案汇总.md) | MD | 07-18 |
| [头部手游半透明渲染方案汇总 · 手机版](./头部手游半透明渲染方案汇总_手机版.md) | MD | 07-18 |
| [头部手游半透明渲染方案汇总](./头部手游半透明渲染方案汇总.md) | MD | 07-18 |
| [头部手游 PSO 方案汇总](./头部手游PSO方案汇总.md) | MD | 07-18 |
| [FPS 手游移动端渲染技术全景对比](./FPS手游技术全景对比.md) | MD | 07-18 |

## 04 · 引擎源码级分析

> PVS、视锥剔除、WorldPartition、TaskGraph、线程池、RDG 等源码深挖

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [UE5-Editor-PreviewPlatform脚本化切换-UnrealEd绑定与WorldPartition崩溃修复](./UnrealMCP扩展/UE5-Editor-PreviewPlatform脚本化切换-UnrealEd绑定与WorldPartition崩溃修复.md) | MD | 07-27 |
| [WorldPartitionPVS实现](./WorldPartitionPVS实现.md) | MD | 06-12 |
| [移动端 PVS 不生效原因分析](./PVS-Mobile-NotWorking-Analysis.md) | MD | 06-12 |
| [ComputeRelevance CPU 优化报告](./ComputeRelevance优化报告.md) | MD | 05-25 |
| [obj list primitives 调试命令改动总结（ZXB）](./Obj_List_Primitives_ZXB_Command.md) | MD | 05-25 |
| [SceneVisibility_FrustumCull 优化改动总结（ZXB）](./SceneVisibility_FrustumCull_ZXB_Optimization.md) | MD | 05-25 |
| [自适应线程池调度 — IO 与 PSO 任务跨池借用技术方案](./自适应线程池调度_IO与PSO任务跨池借用技术方案.md) | MD | 04-22 |
| [UE5 Runtime Virtual Texture — 32位 WorldHeight 烘焙精度损失分析报告](./VT_R32F_PrecisionLoss_Analysis.md) | MD | 04-22 |
| [UWorldPartitionBuilder 加载数量控制 — 技术总结](./WorldPartitionBuilder_LoadControl.md) | MD | 04-22 |
| [UE5 TaskGraph Worker 线程动态数量控制 — 技术分析报告](./UE5_TaskGraph_MaxActiveWorkerCount_Report.md) | MD | 04-22 |
| [UE5 线程池架构技术文档](./UE5线程池架构技术文档.md) | MD | 04-22 |
| [RVT Normal 精度优化：BC5 双通道独立端点方案](./RVT_Normal精度优化_BC5双通道独立端点方案.md) | MD | 04-22 |
| [SceneDepthZ Transient Heap Cache Miss 问题分析与修复](./SceneDepthZ_Transient_Heap_CacheMiss_Fix.md) | MD | 04-22 |
| [FPreviousViewInfo 保存流程 — 技术总结](./FPreviousViewInfo_保存流程_技术总结.md) | MD | 04-22 |
| [RDG TransientAllocator ParallelResourceCreation 切核问题分析与优化](./RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md) | MD | 04-22 |
| [CVarLightingChannelExtractStatic 技术总结](./CVarLightingChannelExtractStatic_技术总结.md) | MD | 04-22 |
| [UWorld::Tick 中 TG_LastDemotable 阶段分析与 DeallocateTransformData 调用链](./AiDoc_UWorldTickTGLastDemotableAndDeallocateTransformDataCallChain_20260210.md) | MD | 04-22 |
| [AddToWorld 异步任务超时中断 — 技术实现文档](./AddToWorld异步任务超时中断-技术实现文档.md) | MD | 04-22 |

## 05 · 崩溃与稳定性

> 崩溃定位与修复：VT / SkeletalMesh / UseAfterFree、帧率掉档排查

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [UE Android 帧率自动降至30fps 排查与修复指南](./UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md) | MD | 06-18 |
| [UE5 虚拟纹理系统崩溃分析与修复总结](./VT_FCreateCodecTask_OrphanTask_UseAfterFree_Fix.md) | MD | 04-22 |
| [VirtualTexture 崩溃分析与修复总结](./VT_GC_DanglingPtr_Crash_In_AsyncTranscode.md) | MD | 04-22 |
| [SkeletalMesh OnUnregister DeallocateTransformData 并发崩溃修复](./AiDoc_SkeletalMeshOnUnregisterDeallocateTransformDataConcurrentCrash_20260209.md) | MD | 04-22 |

## 06 · Profiling 工具与教程

> 高通 SDP / Adreno / Snapdragon Profiler、UE Insights、CPU Trace 工具链

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [Snapdragon-Profiler-功耗分析指导手册](./Snapdragon-Profiler-功耗分析指导手册.md) | MD | 08-12 |
| [UE-Insights-Queue-Present耗时定位-GPU瓶颈判断](./UE-Insights-Queue-Present耗时定位-GPU瓶颈判断.md) | MD | 08-12 |
| [Snapdragon-Profiler-启动崩溃-msvcp140-Runtime版本不兼容排查修复指南](./Snapdragon-Profiler-启动崩溃-msvcp140-Runtime版本不兼容排查修复指南.md) | MD | 08-11 |
| [高通 SDP 性能热点定位完整资料库](./高通SDP性能热点定位-完整资料库.md) | MD | 07-18 |
| [高通 Adreno GPU 最佳实践系列 · 完整阅读报告](./高通AdrenoGPU最佳实践系列-阅读报告.md) | MD | 07-18 |
| [高通 SDP 工具使用教程 - 移动端 GPU 瓶颈定位指南](./高通SDP工具使用教程-GPU瓶颈定位.md) | MD | 07-18 |
| [Snapdragon Profiler 命令行模式操作文档 (qprof CLI)](./Snapdragon-Profiler-命令行模式操作文档.md) | MD | 07-18 |
| [Snapdragon Profiler 性能指标详解](./SDP-Counters-性能指标详解.md) | MD | 07-18 |
| [第三方插件非注册线程 CPU 耗时 Trace 链路改造技术文档](./ThirdPartyPluginThread_CPU_Trace_TechDoc.md) | MD | 04-27 |
| [Insights CPU Usage Track 技术文档](./CpuUsageTrack_TechDoc.md) | MD | 04-22 |
| [Insights_CpuUsage_UserManual_CN](./Insights_CpuUsage_UserManual_CN.docx) | DOCX | 04-17 |

## 07 · 项目专项分析

> 具体项目（FateTrigger 等）的单帧 / 纹理 / 三角面分析、AO 实践报告

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [移动端 AO（环境光遮蔽）实践方案技术报告](./移动端AO实践方案技术报告.md) | MD | 07-18 |
| [VirtualHeightfieldMesh 插件分析报告](./VHM_Analysis_Report.md) | MD | 07-18 |

## 99 · 其它与原始资料

> 未归类资料、大体积归档报告、原始数据（docx/csv/pdf）

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [2026-09-29](./.workbuddy/memory/2026-09-29.md) | MD | 09-29 |
| [2026-09-28](./.workbuddy/memory/2026-09-28.md) | MD | 09-28 |
| [UE5-VHM虚拟高度场-实现剖析-易读版](./UE5-VHM虚拟高度场-实现剖析-易读版.html) | HTML | 09-26 |
| [VHM 虚拟高度场（Virtual Heightfield Mesh）实现剖析](./UE5-VHM虚拟高度场-实现剖析-易读版.md) | MD | 09-26 |
| [UE5-VHM虚拟高度场-四叉树GPU-Driven地形-实现原理与优化学习文档](./UE5-VHM虚拟高度场-四叉树GPU-Driven地形-实现原理与优化学习文档.html) | HTML | 09-25 |
| [UE5 VHM 虚拟高度场（Virtual Heightfield Mesh）实现原理与优化学习文档](./UE5-VHM虚拟高度场-四叉树GPU-Driven地形-实现原理与优化学习文档.md) | MD | 09-25 |
| [UnrealMobileDeviceViewer CVar 提示漏抓 — FAutoConsoleCommand 限定名正则缺陷](./UnrealMobileDeviceViewer-CVar提示漏抓-FAutoConsoleCommand限定名正则缺陷.md) | MD | 09-24 |
| [GPUScene-实现原理-PC与Mobile差异-合批与InstanceCulling](./GPUScene-实现原理-PC与Mobile差异-合批与InstanceCulling.html) | HTML | 09-23 |
| [UE-Mobile-MeshDrawCommandStats-面数统计口径-与MaxDC截断同口径修复](./UE-Mobile-MeshDrawCommandStats-面数统计口径-与MaxDC截断同口径修复.md) | MD | 09-22 |
| [UE-Android-非cook迭代-快速装机与批量CVar下发工作流](./UE-Android-非cook迭代-快速装机与批量CVar下发工作流.md) | MD | 09-22 |
| [UE Mobile SlateUIMaxDC：Slate UI 的 Draw Call 限流实现与渲染链路](./UE-Mobile-SlateUIMaxDC-SlateUI-DC限流-渲染链路与编辑器副作用.md) | MD | 09-21 |
| [UE-S1Game-Android-cook-卡死与62%崩溃-根因定位与修复](./UE-S1Game-Android-cook-卡死与62%崩溃-根因定位与修复.md) | MD | 09-20 |
| [UE-Android-Vulkan-VAT材质-asuint编译失败-修复与验证](./UE-Android-Vulkan-VAT材质-asuint编译失败-修复与验证.md) | MD | 09-19 |
| [UE5 Android Vulkan — VAT 材质 asuint 编译失败：min16float 无重载根因与修复](./UE5-Android-Vulkan-VAT材质asuint编译失败-min16float无重载根因与修复.md) | MD | 09-17 |
| [UE-Android-RHIThread慢49%-VulkanValidationLayer自动加载去除指南](./UE-Android-RHIThread慢49%-VulkanValidationLayer自动加载去除指南.md) | MD | 09-15 |
| [RUSH 性能指标与资产规范测算工具 — 能力分析](./RUSH性能测算平台_能力分析.md) | MD | 09-15 |
| [Perfetto 使用手册 —— 面向 UE 手游 / 移动端 GPU 性能分析](./Perfetto使用手册-面向UE手游性能分析.md) | MD | 09-14 |
| [UnrealMCP 连不上（mcp_connected: false）排查：插件未启用 + 端口三方不一致 + UCLASS(config=Editor) 决定配置文件位置](./UnrealMCP-连不上-mcp_connected-false-插件启用与端口配置排查.md) | MD | 09-13 |
| [UE Mobile BasePass Draw Call 上限（r.Mobile.BasePassMaxDC）实现与 stat 统计口径](./UE-Mobile-BasePass-DC上限-CVar截断实现与stat-drawcount口径差异.md) | MD | 09-13 |
| [UE Mobile PreZ Pass 作用与「只画 Mask 材质」原理](./UE-Mobile-PreZ-Pass-作用与只画Mask材质原理.md) | MD | 09-12 |
| [dfm_pos4_basepass_ps_aoc_report](./dfm_pos4_basepass_ps_aoc_report.html) | HTML | 09-11 |
| [《S1 Mobile性能标准》校准对照报告](./S1Mobile性能标准_校准对照报告.md) | MD | 09-10 |
| [UE 命令行 CVar 通道分流：-dpcvars vs -ExecCmds（ReadOnly/Cheat × 优先级）](./UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md) | MD | 09-08 |
| [UE-Android-Vulkan启动后确定性闪黑-DynamicUBO与InputAttachment同DescriptorSet修复](./UE-Android-Vulkan启动后确定性闪黑-DynamicUBO与InputAttachment同DescriptorSet修复.md) | MD | 09-07 |
| [UE-Android-845黑屏-GPUScene-SSBO-stride修复](./UE-Android-845黑屏-GPUScene-SSBO-stride修复.md) | MD | 09-01 |
| [UE Android 打包无日志：相对 -project + 进程 CWD=/ 导致日志路径解析失败](./UE-Android-无日志-ABSLOG-CWD相对路径.md) | MD | 09-01 |
| [UE Android 启动崩溃：Vulkan Chunked PSO Cache × validation layer 组合必崩](./UE-Android-Vulkan启动崩溃-ChunkedPSOCache-validationlayer.md) | MD | 09-01 |
| [移动端资产规范 v0.8（草案）](./移动端资产规范_v0.8_草案.md) | MD | 08-30 |
| [移动端资产规范 v0.8 —— 全数值核对报告（对齐三角洲）](./移动端资产规范_全数值核对报告.md) | MD | 08-30 |
| [AiDoc 知识库框架与维护方案](./知识库框架与维护方案.md) | MD | 08-28 |
| [移动端资产规范 v0.8 —— 面数数值评估与修改建议](./移动端资产规范_面数评估与修改建议.md) | MD | 08-27 |
| [UE-Mobile-Toon描边-DX12预览PSO崩溃-MultiPass独立pass与Vulkan门控修复](./UE-Mobile-Toon描边-DX12预览PSO崩溃-MultiPass独立pass与Vulkan门控修复.md) | MD | 08-26 |
| [CL 1088788：Forward BasePass 整理 + PreExposure 模型重构 + r.LuxGI=0 语义变更](./CL1088788-Forward-BasePass整理-PreExposure重构-rLuxGI语义变更.md) | MD | 08-26 |
| [UE Mobile SinglePass storeOp 分析：SceneColor/Depth 与洛克王国 One Pass 对齐（恢复 Depth Memoryless）](./UE-Mobile-SinglePass-storeOp-DepthMemoryless-洛克OnePass对齐.md) | MD | 08-26 |
| [2X2资产达标率分析报告](./2X2资产达标率分析报告.html) | HTML | 08-26 |
| [S1 描边实现分析](./S1描边实现分析.md) | MD | 08-23 |
| [MobileShadingRenderer：RenderForwardMultiPass 与 RenderForwardSinglePass 原理分析](./MobileShadingRenderer_RenderForward_SingleMultiPass.md) | MD | 08-21 |
| [MobileRenderer-Forward-SingleMultiPass-实现原理与ZXB对齐修复总结](./MobileRenderer-Forward-SingleMultiPass-实现原理与ZXB对齐修复总结.md) | MD | 08-21 |
| [UE-Niagara-Mobile适配-CVar降级清单与不显示排查](./UE-Niagara-Mobile适配-CVar降级清单与不显示排查.md) | MD | 08-20 |
| [MSAA 下角色描边锯齿根因分析](./Mobile-MSAA-角色描边锯齿根因分析.md) | MD | 08-19 |
| [UE-Mobile-Toon描边-PreOutline深度偏移污染-MSAA角色涂黑与BasePass剔除修复](./UE-Mobile-Toon描边-PreOutline深度偏移污染-MSAA角色涂黑与BasePass剔除修复.md) | MD | 08-18 |
| [ZXBUnrealDebug 使用文档](./ZXBUnrealDebug_使用文档.md) | MD | 08-17 |
| [UE-Mobile-MSAA-实现原理-Resolve机制与深度采样链路](./UE-Mobile-MSAA-实现原理-Resolve机制与深度采样链路.md) | MD | 08-15 |
| [S1Game-AssignSceneProxy并发崩溃-异步关卡流送排查记录.md](./S1Game-AssignSceneProxy并发崩溃-异步关卡流送排查记录.md) | MD | 08-11 |
| [TClaude会话监控-飞书通知交互-本地工具构建指南](./TClaude会话监控-飞书通知交互-本地工具构建指南.md) | MD | 08-11 |
| [PC-Deferred-BasePass-ShaderPrint-诊断buffer探针与十字准星-完整实现.md](./PC-Deferred-BasePass-ShaderPrint-诊断buffer探针与十字准星-完整实现.md) | MD | 08-09 |
| [UE5EA-编辑器弹窗无日志-MessageBoxExt统一拦截AI追踪](./UE5EA-编辑器弹窗无日志-MessageBoxExt统一拦截AI追踪.md) | MD | 08-08 |
| [Forward 对齐 Deferred 角色渲染 — 全部修改总览](./Forward-对齐Deferred-角色渲染-全部修改总览.md) | MD | 08-07 |
| [Forward LuxGI 对齐 Deferred — 两因素修复（F/D 比值 2.53× → 1.00×）](./Forward-LuxGI-对齐Deferred-两因素修复.md) | MD | 08-07 |
| [UE-Mobile-Forward-Toon角色对齐Deferred-GBuffer量化-Emissive口径-LuxGI压暗顺序-五项修复](./UE-Mobile-Forward-Toon角色对齐Deferred-GBuffer量化-Emissive口径-LuxGI压暗顺序-五项修复.md) | MD | 08-02 |
| [UE5EA 流送收集触发 FlushAsyncLoading 卡顿 —— 材质 soft 贴图同步加载根因与修复](./UE5EA-流送收集触发FlushAsyncLoading卡顿-材质soft贴图TryLoadSynchronous根因与修复.md) | MD | 07-30 |
| [UE5 LightAccumulator_AddSplit 函数作用](./UE5-LightAccumulator-AddSplit-函数作用.md) | MD | 07-29 |
| [UE5 Mobile Deferred Shading 中 LightFunctionQuality 与 ComputeLightFunctionMultiplier 的关系](./UE5-MobileDeferredShading-LightFunctionQuality与ComputeLightFunctionMultiplier关系.md) | MD | 07-29 |
| [UE-Mobile-Forward-描边未对齐Deferred-通道语义修复](./UE-Mobile-Forward-Outline-Align-Deferred.md) | MD | 07-28 |
| [编辑器自动化-打开到截图流程卡顿-MCP两层就绪等待修复](./编辑器自动化-打开到截图流程卡顿-MCP两层就绪等待修复.md) | MD | 07-28 |
| [UE Mobile Forward LuxGI 粗糙反射对齐 Deferred — RoughReflection 注入、NaN 爆白与视角排查全记录](./UE-Mobile-Forward-LuxGI粗糙反射对齐Deferred-RoughReflection注入与NaN爆白排查.md) | MD | 07-27 |
| [UE-BecomeViewTarget-渲染帧准备阶段Detach组件-MarkActorComponentForNeededEndOfFrameUpdate-race-condition修复](./UE-BecomeViewTarget-渲染帧准备阶段Detach组件-MarkActorComponentForNeededEndOfFrameUpdate-race-condition修复.md) | MD | 07-27 |
| [UE5 BasePass PS 过程量调试 — 持久 DebugValueBuffer per-draw 绑定方案](./UE5-BasePass-PS-过程量调试-持久DebugValueBuffer-per-draw绑定方案.md) | MD | 07-27 |
| [claude_code_zelda_guide](./ClaudeCode/claude_code_zelda_guide.html) | HTML | 07-27 |
| [claude_code_guide_v2](./ClaudeCode/claude_code_guide_v2.html) | HTML | 07-27 |
| [Claude Code 使用技巧、最佳实践与效率秘籍](./ClaudeCode/03_tips.md) | MD | 07-27 |
| [B站 Claude Code 热门教程视频汇总](./ClaudeCode/04_bilibili_videos.md) | MD | 07-27 |
| [Claude Code 系统性使用教程](./ClaudeCode/02_tutorial.md) | MD | 07-27 |
| [Claude Code 深度研究报告：宏观概览](./ClaudeCode/01_overview.md) | MD | 07-27 |
| [RenderDoc-D3D12-DebugPixel-min16float-varying-与-cbuffer数组-E_INVALIDARG根因修复](./RenderDoc-D3D12-DebugPixel-min16float-varying-与-cbuffer数组-E_INVALIDARG根因修复.md) | MD | 07-25 |
| [UE5 异步关卡流送 CreateSceneProxy 跨线程读物理崩溃（SendRenderDebugPhysics 竞态）修复指南](./UE5-异步关卡流送-CreateSceneProxy跨线程读物理崩溃-SendRenderDebugPhysics竞态修复.md) | MD | 07-22 |
| [UE Mobile Forward LuxGI Permutation 化落地 —— P4 改动汇总](./UE-Mobile-Forward-LuxGI-Permutation化落地-P4改动汇总.md) | MD | 07-21 |
| [UE5 Mobile Forward vs Deferred 渲染管线全流程分析](./UE-Mobile-Forward-vs-Deferred-管线全流程分析-含Shader反汇编解读.md) | MD | 07-20 |
| [UE Mobile Forward 比 Deferred 明显偏亮 —— LuxGI 双重 PreExposure + HYBRID 天光重复 根因与修复](./UE-Mobile-Forward比Deferred偏亮-LuxGI双重PreExposure与HYBRID天光重复-根因与修复.md) | MD | 07-18 |
| [UE Mobile Deferred Preview 下 CartoonShadow 参数 unbound 根因与 IS_MOBILE_BASE_PASS 分流修复](./UE-Mobile-Deferred-Preview-CartoonShadow参数unbound根因与IS_MOBILE_BASE_PASS分流修复.md) | MD | 07-18 |
| [kb_search 知识库 — 框架架构与维护原理](./kb-search-框架与维护原理.md) | MD | 07-18 |
| [UE5 Mobile Forward Path — FoliageShadowIntensity Parameter Not Bound 修复](./UE5-Mobile-Forward-FoliageShadowIntensity-Parameter-Not-Bound修复.md) | MD | 07-18 |
| [UE5 Mobile Forward Path — Foliage 竖直彩色条带（ToonRamp Profile 索引）排查修复](./UE5-Mobile-Forward-Path-Foliage-竖直彩色条带-ToonRamp-Profile索引-排查修复.md) | MD | 07-18 |
| [UE-MobileBasePassCSM-CommandCount-Mismatch-修复](./UE-MobileBasePassCSM-CommandCount-Mismatch-修复.md) | MD | 07-18 |
| [UE Mobile LuxGI Forward 与 Deferred 效果不一致 — ApplyCartoonShadow 参数绑定修复](./UE-Mobile-LuxGI-Forward与Deferred效果不一致-ApplyCartoonShadow参数绑定修复.md) | MD | 07-18 |
| [特效材质移动端兼容修复说明（给美术）](./EFX_Mobile_Material_Fix.md) | MD | 07-18 |
| [AOC 性能分析教程](./AOC性能分析教程.md) | MD | 07-18 |
| [8Gen2 vs 8Gen3 寄存器配置对照文档](./8Gen2-8Gen3-寄存器配置对照文档.md) | MD | 07-18 |
| [2026-07-01](./.workbuddy/memory/2026-07-01.md) | MD | 07-01 |
| [80-78185-2_REV_AL_Game_Developer_Guide](./80-78185-2_REV_AL_Game_Developer_Guide.pdf) | PDF | 06-26 |

---

> 维护：新增或重命名文档后，在本目录运行 `python generate_index.py` 即可重新生成本导航。归类规则见脚本顶部 `RULES`，如分类不准可调整关键词。
