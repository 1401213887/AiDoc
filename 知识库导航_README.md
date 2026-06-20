# 📚 E:\AiDoc 知识库导航 README

> UE 移动端渲染技术知识库总入口。本目录汇集头部手游渲染管线案例、专题方案汇总、引擎源码级分析与 TBDR 优化方法论。
> 维护：交叉引用以**相对路径**互链；手游 html 顶部均有回链本库的导航横幅。
> 更新：2026-06-20

---

## 🚪 推荐入口

| 你想做什么 | 从这里开始 |
|-----------|-----------|
| 系统学习 TBDR 片上缓存优化（原理→双平台→案例→决策） | [TBDR 跨平台完整技术方案](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md) |
| 验收 TBDR 系列产出 / 快速答疑 | [TBDR 验收索引](./UE_Mobile_TBDR_验收索引.md) |
| 查某个手游怎么做的 | 见下方「② 手游案例」 |
| 查某个专题（半透明/剔除/DrawCall）跨游戏对比 | 见下方「③ 专题汇总」 |
| 排查具体引擎源码问题 | 见下方「④ 引擎源码级分析」 |

---

## ① TBDR 优化方法论系列（本次新增，方法论纵贯）

| 文档 | 作用 |
|------|------|
| [UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md) | **主文档**：TBDR 原理 → Vulkan/Metal 双平台机制 → 三案例 → 是否改引擎决策 → iOS 实现 |
| [UE_Mobile_TBDR_查漏补缺增补卷.md](./UE_Mobile_TBDR_查漏补缺增补卷.md) | 100 点审阅、真实 `LookupDeviceZ()` 源码、RGB10A2 因果修正、PrePass×Subpass 互斥、带宽公式、版本矩阵、术语表 |
| [UE_Mobile_TBDR_验收索引.md](./UE_Mobile_TBDR_验收索引.md) | 产出清单 + 原始问题→答案速查 + 诚信边界 |

---

## ② 手游案例（单款，横向资料）

> 与主文档的对应关系：主文档讲"方法论"，案例 html 讲"某款游戏的具体落地"。

### 渲染管线选型分组
| 管线路线 | 主文档章节 | 手游案例 |
|---------|-----------|---------|
| **Forward + 重剔除** | [§5 和平精英](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md#5-案例三和平精英-forward-重剔除) | [和平精英](./和平精英移动端技术要点总结.html) · [使命召唤手游](./使命召唤手游移动端技术要点总结.html) · [暗区突围](./暗区突围移动端技术要点总结.html) |
| **Mobile Deferred + 片上 GBuffer** | [§4 燕云十六声](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md#4-案例二燕云十六声-片上-gbuffer延迟) | [燕云十六声](./燕云十六声移动端技术要点总结.html)（Messiah 自研） · [三角洲](./三角洲移动端技术要点总结.html)（多档位退化） |
| **One Pass / Subpass 合并** | [§3 洛克王国](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md#3-案例一洛克王国世界-one-pass前向改造) | [三角洲](./三角洲移动端技术要点总结.html) · [燕云十六声](./燕云十六声移动端技术要点总结.html) |

### 其他手游案例（未直接进入 TBDR 主文档，供参考）
| 游戏 | 文档 |
|------|------|
| 原神 | [原神移动端技术要点总结.html](./原神移动端技术要点总结.html) |
| 崩坏：星穹铁道 | [崩坏星穹铁道移动端技术要点总结.html](./崩坏星穹铁道移动端技术要点总结.html) |
| 绝区零 | [绝区零移动端技术要点总结.html](./绝区零移动端技术要点总结.html) |
| 鸣潮 | [鸣潮移动端技术要点总结.html](./鸣潮移动端技术要点总结.html) |
| 王者荣耀 | [王者荣耀移动端技术要点总结.html](./王者荣耀移动端技术要点总结.html) |
| 永劫无间手游 | [永劫无间手游移动端技术要点总结.html](./永劫无间手游移动端技术要点总结.html) |
| 光遇 | [光遇移动端技术要点总结.html](./光遇移动端技术要点总结.html) |
| 第五人格 | [第五人格移动端技术要点总结.html](./第五人格移动端技术要点总结.html) |
| 蛋仔派对 | [蛋仔派对移动端技术要点总结.html](./蛋仔派对移动端技术要点总结.html) |

---

## ③ 专题汇总（跨游戏横向）

| 专题 | 文档 | 关联 TBDR 主文档 |
|------|------|-----------------|
| FPS 技术全景对比 | [FPS手游技术全景对比.html](./FPS手游技术全景对比.html) | §6 决策矩阵 / §8 落地建议 |
| 半透明渲染方案 | [头部手游半透明渲染方案汇总.html](./头部手游半透明渲染方案汇总.html)（+[手机版](./头部手游半透明渲染方案汇总_手机版.html)） | §2.4 半透明读片上深度 / §3 Distortion 合并 |
| 遮挡剔除方案 | [头部手游移动端遮挡剔除方案汇总.html](./头部手游移动端遮挡剔除方案汇总.html) | §1.3 减少进管线工作量 / §5 重剔除 |
| 降低 DrawCall 方案 | [头部手游降低DrawCall方案汇总.html](./头部手游降低DrawCall方案汇总.html) | §5 和平精英重剔除 |

---

## ④ 引擎源码级分析（深挖时参考）

| 主题 | 文档 |
|------|------|
| 移动端 PVS 不生效 | [PVS-Mobile-NotWorking-Analysis.md](./PVS-Mobile-NotWorking-Analysis.md) |
| 视锥剔除优化 | [SceneVisibility_FrustumCull_ZXB_Optimization.md](./SceneVisibility_FrustumCull_ZXB_Optimization.md) |
| WorldPartition PVS 实现 | [WorldPartitionPVS实现.md](./WorldPartitionPVS实现.md) |
| ComputeRelevance 优化 | [ComputeRelevance优化报告.md](./ComputeRelevance优化报告.md) |
| RDG 瞬态资源并行创建 | [RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md](./RDG_TransientAllocator_ParallelResourceCreation_切核问题分析与优化.md) |
| SceneDepthZ 瞬态堆 Cache Miss | [SceneDepthZ_Transient_Heap_CacheMiss_Fix.md](./SceneDepthZ_Transient_Heap_CacheMiss_Fix.md) |
| Android 帧率降至 30fps（Swappy） | [UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md](./UE-Android-帧率自动降至30fps-Swappy-FramePacing排查修复指南.md) |
| RVT Normal 精度优化（BC5） | [RVT_Normal精度优化_BC5双通道独立端点方案.md](./RVT_Normal精度优化_BC5双通道独立端点方案.md) |
| VT 系列（精度/GC/任务） | [VT_R32F_PrecisionLoss_Analysis.md](./VT_R32F_PrecisionLoss_Analysis.md) · [VT_GC_DanglingPtr_Crash_In_AsyncTranscode.md](./VT_GC_DanglingPtr_Crash_In_AsyncTranscode.md) · [VT_FCreateCodecTask_OrphanTask_UseAfterFree_Fix.md](./VT_FCreateCodecTask_OrphanTask_UseAfterFree_Fix.md) |
| TaskGraph / 线程池 | [UE5_TaskGraph_MaxActiveWorkerCount_Report.md](./UE5_TaskGraph_MaxActiveWorkerCount_Report.md) · [UE5线程池架构技术文档.md](./UE5线程池架构技术文档.md) · [自适应线程池调度_IO与PSO任务跨池借用技术方案.md](./自适应线程池调度_IO与PSO任务跨池借用技术方案.md) |
| CPU Trace / 性能 | [CpuUsageTrack_TechDoc.md](./CpuUsageTrack_TechDoc.md) · [ThirdPartyPluginThread_CPU_Trace_TechDoc.md](./ThirdPartyPluginThread_CPU_Trace_TechDoc.md) |
| 其他 | [AddToWorld异步任务超时中断-技术实现文档.md](./AddToWorld异步任务超时中断-技术实现文档.md) · [WorldPartitionBuilder_LoadControl.md](./WorldPartitionBuilder_LoadControl.md) · [FPreviousViewInfo_保存流程_技术总结.md](./FPreviousViewInfo_保存流程_技术总结.md) · [CVarLightingChannelExtractStatic_技术总结.md](./CVarLightingChannelExtractStatic_技术总结.md) · [Obj_List_Primitives_ZXB_Command.md](./Obj_List_Primitives_ZXB_Command.md) |

---

> **导航维护约定**：
> - 新增 TBDR 相关手游案例时，在「② 手游案例」补一行，并在该 html 顶部加 `tbdr-backlink-banner` 回链横幅。
> - 主文档与本 README 互为双向入口；手游 html → 主文档（横幅）→ 本 README（横幅链接）→ 全库（本表）。
