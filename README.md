# 📚 E:\AiDoc 技术知识库 · 总导航

> UE 移动端渲染技术知识库。汇集 TBDR 片上优化方法论、头部手游渲染拆解、引擎源码级分析、崩溃定位、Profiling 工具链与项目专项报告。
>
> - **互链约定**：全部相对路径，文件不移动，与 git 自动备份兼容。
> - **本页由 `generate_index.py` 自动生成**，新增文档后重跑脚本即可刷新。
> - 文档总数：**84** · 更新：2026-07-06

---

## 🗂 分类目录

| # | 类目 | 文档数 | 说明 |
|---|------|:---:|------|
| 01 | [01 · TBDR 与片上优化方法论](#01-TBDR-与片上优化方法论) | 27 | TBDR 原理、片上缓存、Subpass/Imageblock、HZB、Forward/Deferred 选型——方法论纵贯线 |
| 02 | [02 · 头部手游案例库](#02-头部手游案例库) | 15 | 单款手游移动端渲染拆解（html）。方法论的具体落地参照 |
| 03 | [03 · 专题横向汇总](#03-专题横向汇总) | 7 | 跨游戏横向对比：半透明 / 遮挡剔除 / DrawCall / FPS 全景 |
| 04 | [04 · 引擎源码级分析](#04-引擎源码级分析) | 17 | PVS、视锥剔除、WorldPartition、TaskGraph、线程池、RDG 等源码深挖 |
| 05 | [05 · 崩溃与稳定性](#05-崩溃与稳定性) | 4 | 崩溃定位与修复：VT / SkeletalMesh / UseAfterFree、帧率掉档排查 |
| 06 | [06 · Profiling 工具与教程](#06-Profiling-工具与教程) | 8 | 高通 SDP / Adreno / Snapdragon Profiler、UE Insights、CPU Trace 工具链 |
| 07 | [07 · 项目专项分析](#07-项目专项分析) | 2 | 具体项目（FateTrigger 等）的单帧 / 纹理 / 三角面分析、AO 实践报告 |
| 99 | [99 · 其它与原始资料](#99-其它与原始资料) | 4 | 未归类资料、大体积归档报告、原始数据（docx/csv/pdf） |

---

## 01 · TBDR 与片上优化方法论

> TBDR 原理、片上缓存、Subpass/Imageblock、HZB、Forward/Deferred 选型——方法论纵贯线

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [Vulkan Subpass × TBDR 带宽优化 · 系统学习指南](./Vulkan_Subpass_TBDR_带宽优化_学习指南.md) | MD | 06-28 |
| [UE5 HZB（Hierarchical Z-Buffer）实现原理与移动端分析](./UE5_HZB_实现原理与移动端分析_技术文档.md) | MD | 06-27 |
| [UE 移动端 iOS vs Android：平台相关工作量全景对比](./UE_Mobile_iOS_vs_Android_平台工作量对比.md) | MD | 06-22 |
| [UE 移动端 Imageblock 与 Tile Shading 落地技术文档](./UE_Mobile_Imageblock_TileShading_落地技术文档.md) | MD | 06-22 |
| [Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案（最终版）](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案（最终版）.md) | MD | 06-22 |
| [Unreal Mobile TBDR 片上缓存优化：跨平台完整技术方案](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md) | MD | 06-22 |
| [UE Mobile TBDR 优化 — 查漏补缺增补卷（100 轮审阅）](./UE_Mobile_TBDR_查漏补缺增补卷.md) | MD | 06-22 |
| [UE Mobile Forward vs Deferred —— 完整文档体系入口](./MobileRenderPath/UE_Mobile_Tech_README.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 11：Velocity / LightingCommon / 平台特化](./MobileRenderPath/UE_Mobile_Tech_DeepDive_11_Velocity_Platform.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 10：实战 FAQ + 改造模板](./MobileRenderPath/UE_Mobile_Tech_DeepDive_10_FAQ.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 09：VertexShader / Material Permutation / Substrate](./MobileRenderPath/UE_Mobile_Tech_DeepDive_09_VertexShader_Material.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 08：Decal / Fog / Sky / Atmosphere](./MobileRenderPath/UE_Mobile_Tech_DeepDive_08_Decal_Fog_Sky.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 07：反射系统全谱](./MobileRenderPath/UE_Mobile_Tech_DeepDive_07_Reflection.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 06：MeshDrawCommand / GPUScene / InstanceCulling](./MobileRenderPath/UE_Mobile_Tech_DeepDive_06_MeshDrawCommand.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 05：虚拟纹理 / 虚拟阴影 / MMH](./MobileRenderPath/UE_Mobile_Tech_DeepDive_05_VirtualTexture.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 04：半透明 / SingleLayerWater / Substrate](./MobileRenderPath/UE_Mobile_Tech_DeepDive_04_Translucency.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 03：后处理链](./MobileRenderPath/UE_Mobile_Tech_DeepDive_03_PostProcess.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 02：阴影系统全谱](./MobileRenderPath/UE_Mobile_Tech_DeepDive_02_Shadow.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 深度补充 01：可见性与遮挡剔除](./MobileRenderPath/UE_Mobile_Tech_DeepDive_01_Occlusion.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 源码索引脚手架](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 实战篇](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Practical.md) | MD | 06-20 |
| [UE Mobile Forward vs Deferred —— 补充篇](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc_Appendix.md) | MD | 06-20 |
| [UE 移动端 Forward 与 Deferred 管线差异技术文档](./MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc.md) | MD | 06-20 |
| [iOS / Metal 下实现 TBDR 优化方案：解决方案文档](./iOS_Metal_TBDR_实现方案.md) | MD | 06-20 |
| [UE Mobile TBDR 优化技术：是否需要改造引擎？落地决策文档](./UE_Mobile_TBDR_改造决策文档.md) | MD | 06-20 |
| [《燕云十六声》手游"片上 GBuffer"渲染技术总结报告](./燕云十六声_片上GBuffer_技术总结.md) | MD | 06-20 |
| [UE Mobile Forward 渲染管线代码学习指南](./MobileRenderPath/UE_Mobile_Forward_Pipeline_Study_Guide.md) | MD | 06-19 |

## 02 · 头部手游案例库

> 单款手游移动端渲染拆解（html）。方法论的具体落地参照

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [洛克王国_pipeline_report](./洛克王国_pipeline_report.html) | HTML | 06-20 |
| [暗区突围移动端技术要点总结](./暗区突围移动端技术要点总结.html) | HTML | 06-20 |
| [使命召唤手游移动端技术要点总结](./使命召唤手游移动端技术要点总结.html) | HTML | 06-20 |
| [三角洲移动端技术要点总结](./三角洲移动端技术要点总结.html) | HTML | 06-20 |
| [燕云十六声移动端技术要点总结](./燕云十六声移动端技术要点总结.html) | HTML | 06-20 |
| [和平精英移动端技术要点总结](./和平精英移动端技术要点总结.html) | HTML | 06-20 |
| [鸣潮移动端技术要点总结](./鸣潮移动端技术要点总结.html) | HTML | 06-14 |
| [蛋仔派对移动端技术要点总结](./蛋仔派对移动端技术要点总结.html) | HTML | 06-14 |
| [绝区零移动端技术要点总结](./绝区零移动端技术要点总结.html) | HTML | 06-14 |
| [王者荣耀移动端技术要点总结](./王者荣耀移动端技术要点总结.html) | HTML | 06-14 |
| [第五人格移动端技术要点总结](./第五人格移动端技术要点总结.html) | HTML | 06-14 |
| [永劫无间手游移动端技术要点总结](./永劫无间手游移动端技术要点总结.html) | HTML | 06-14 |
| [崩坏星穹铁道移动端技术要点总结](./崩坏星穹铁道移动端技术要点总结.html) | HTML | 06-14 |
| [光遇移动端技术要点总结](./光遇移动端技术要点总结.html) | HTML | 06-14 |
| [原神移动端技术要点总结](./原神移动端技术要点总结.html) | HTML | 06-14 |

## 03 · 专题横向汇总

> 跨游戏横向对比：半透明 / 遮挡剔除 / DrawCall / FPS 全景

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [头部手游画质分级方案汇总](./头部手游画质分级方案汇总.html) | HTML | 07-02 |
| [头部手游PSO方案汇总](./头部手游PSO方案汇总.html) | HTML | 07-02 |
| [头部手游降低DrawCall方案汇总](./头部手游降低DrawCall方案汇总.html) | HTML | 06-20 |
| [头部手游移动端遮挡剔除方案汇总](./头部手游移动端遮挡剔除方案汇总.html) | HTML | 06-20 |
| [头部手游半透明渲染方案汇总](./头部手游半透明渲染方案汇总.html) | HTML | 06-20 |
| [FPS手游技术全景对比](./FPS手游技术全景对比.html) | HTML | 06-20 |
| [头部手游半透明渲染方案汇总_手机版](./头部手游半透明渲染方案汇总_手机版.html) | HTML | 06-16 |

## 04 · 引擎源码级分析

> PVS、视锥剔除、WorldPartition、TaskGraph、线程池、RDG 等源码深挖

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [自适应线程池调度 — IO 与 PSO 任务跨池借用技术方案](./自适应线程池调度_IO与PSO任务跨池借用技术方案.md) | MD | 06-14 |
| [UWorldPartitionBuilder 加载数量控制 — 技术总结](./WorldPartitionBuilder_LoadControl.md) | MD | 06-14 |
| [WorldPartitionPVS实现](./WorldPartitionPVS实现.md) | MD | 06-14 |
| [UE5 Runtime Virtual Texture — 32位 WorldHeight 烘焙精度损失分析报告](./VT_R32F_PrecisionLoss_Analysis.md) | MD | 06-14 |
| [UE5 TaskGraph Worker 线程动态数量控制 — 技术分析报告](./UE5_TaskGraph_MaxActiveWorkerCount_Report.md) | MD | 06-14 |
| [UE5 线程池架构技术文档](./UE5线程池架构技术文档.md) | MD | 06-14 |
| [SceneVisibility_FrustumCull 优化改动总结（ZXB）](./SceneVisibility_FrustumCull_ZXB_Optimization.md) | MD | 06-14 |
| [RVT Normal 精度优化：BC5 双通道独立端点方案](./RVT_Normal精度优化_BC5双通道独立端点方案.md) | MD | 06-14 |
| [SceneDepthZ Transient Heap Cache Miss 问题分析与修复](./SceneDepthZ_Transient_Heap_CacheMiss_Fix.md) | MD | 06-14 |
| [移动端 PVS 不生效原因分析](./PVS-Mobile-NotWorking-Analysis.md) | MD | 06-14 |
| [RDG TransientAllocator ParallelResourceCreation 切核问题分析与优化](./RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md) | MD | 06-14 |
| [obj list primitives 调试命令改动总结（ZXB）](./Obj_List_Primitives_ZXB_Command.md) | MD | 06-14 |
| [FPreviousViewInfo 保存流程 — 技术总结](./FPreviousViewInfo_保存流程_技术总结.md) | MD | 06-14 |
| [ComputeRelevance CPU 优化报告](./ComputeRelevance优化报告.md) | MD | 06-14 |
| [CVarLightingChannelExtractStatic 技术总结](./CVarLightingChannelExtractStatic_技术总结.md) | MD | 06-14 |
| [UWorld::Tick 中 TG_LastDemotable 阶段分析与 DeallocateTransformData 调用链](./AiDoc_UWorldTickTGLastDemotableAndDeallocateTransformDataCallChain_20260210.md) | MD | 06-14 |
| [AddToWorld 异步任务超时中断 — 技术实现文档](./AddToWorld异步任务超时中断-技术实现文档.md) | MD | 06-14 |

## 05 · 崩溃与稳定性

> 崩溃定位与修复：VT / SkeletalMesh / UseAfterFree、帧率掉档排查

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [UE Android 帧率自动降至30fps 排查与修复指南](./UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md) | MD | 06-19 |
| [VirtualTexture 崩溃分析与修复总结](./VT_GC_DanglingPtr_Crash_In_AsyncTranscode.md) | MD | 06-14 |
| [UE5 虚拟纹理系统崩溃分析与修复总结](./VT_FCreateCodecTask_OrphanTask_UseAfterFree_Fix.md) | MD | 06-14 |
| [SkeletalMesh OnUnregister DeallocateTransformData 并发崩溃修复](./AiDoc_SkeletalMeshOnUnregisterDeallocateTransformDataConcurrentCrash_20260209.md) | MD | 06-14 |

## 06 · Profiling 工具与教程

> 高通 SDP / Adreno / Snapdragon Profiler、UE Insights、CPU Trace 工具链

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [SDP-Counters-性能指标详解](./SDP-Counters-性能指标详解.html) | HTML | 07-01 |
| [Snapdragon-Profiler-命令行模式操作文档](./Snapdragon-Profiler-命令行模式操作文档.html) | HTML | 06-27 |
| [高通AdrenoGPU最佳实践系列-阅读报告](./高通AdrenoGPU最佳实践系列-阅读报告.html) | HTML | 06-24 |
| [高通SDP性能热点定位-完整资料库](./高通SDP性能热点定位-完整资料库.html) | HTML | 06-23 |
| [高通SDP工具使用教程-GPU瓶颈定位](./高通SDP工具使用教程-GPU瓶颈定位.html) | HTML | 06-23 |
| [第三方插件非注册线程 CPU 耗时 Trace 链路改造技术文档](./ThirdPartyPluginThread_CPU_Trace_TechDoc.md) | MD | 06-14 |
| [Insights_CpuUsage_UserManual_CN](./Insights_CpuUsage_UserManual_CN.docx) | DOCX | 06-14 |
| [Insights CPU Usage Track 技术文档](./CpuUsageTrack_TechDoc.md) | MD | 06-14 |

## 07 · 项目专项分析

> 具体项目（FateTrigger 等）的单帧 / 纹理 / 三角面分析、AO 实践报告

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [移动端AO实践方案技术报告](./移动端AO实践方案技术报告.html) | HTML | 06-28 |
| [VHM_Analysis_Report](./VHM_Analysis_Report.html) | HTML | 05-20 |

## 99 · 其它与原始资料

> 未归类资料、大体积归档报告、原始数据（docx/csv/pdf）

| 文档 | 类型 | 更新 |
|------|:---:|:---:|
| [2026-07-01](./.workbuddy/memory/2026-07-01.md) | MD | 07-02 |
| [AOC性能分析教程](./AOC性能分析教程.html) | HTML | 07-01 |
| [8Gen2-8Gen3-寄存器配置对照文档](./8Gen2-8Gen3-寄存器配置对照文档.html) | HTML | 07-01 |
| [80-78185-2_REV_AL_Game_Developer_Guide](./80-78185-2_REV_AL_Game_Developer_Guide.pdf) | PDF | 07-01 |

---

> 维护：新增或重命名文档后，在本目录运行 `python generate_index.py` 即可重新生成本导航。归类规则见脚本顶部 `RULES`，如分类不准可调整关键词。
