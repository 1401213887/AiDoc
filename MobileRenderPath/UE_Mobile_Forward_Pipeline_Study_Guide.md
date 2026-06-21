# UE Mobile Forward 渲染管线代码学习指南

> 版本基准: UE5.5 | 更新日期: 2026-06-19

---

## 1. 管线总览

Mobile Forward 是 UE 移动端的默认渲染路径 (`r.Mobile.ShadingPath=0`)，采用传统前向渲染，
每个物体在 BasePass 中一次性完成材质+光照+阴影的着色。

### 1.1 一帧的 Pass 流程

```
FMobileSceneRenderer::Render()
    |
    +-- FScene::UpdateAllPrimitiveSceneInfos()   // 更新图元场景信息
    |
    +-- InitViews()                              // 可见性剔除
    |       |-- ComputeViewVisibility()          //   视锥/遮挡剔除
    |       |-- GatherDynamicMeshElements()      //   收集动态网格
    |       +-- SetupMeshPasses()                //   设置各Pass的MeshDrawCommand
    |
    +-- RenderShadowDepthMaps()                  // 阴影深度
    |
    +-- RenderForward()                          // 前向渲染主分支
    |       |-- RenderPrePass()                  //   深度预通行（可选）
    |       |-- RenderMobileBasePass()           //   ★ 核心Pass：前向着色
    |       |-- Render decals / AO / etc.        //   贴花、环境光遮蔽
    |       +-- RenderTranslucency()             //   半透明物体
    |
    +-- RenderPostProcessing()                   // 后处理
    |
    +-- FScene::UpdateAllPrimitiveSceneInfos()   // 帧尾更新
```

### 1.2 Mobile Forward vs Mobile Deferred 对比

| 维度 | Mobile Forward | Mobile Deferred |
|------|---------------|-----------------|
| CVar | `r.Mobile.ShadingPath=0` | `r.Mobile.ShadingPath=1` |
| 着色方式 | 物体级：BasePass内一次完成材质+光照 | 分离：BasePass写GBuffer，Lighting Pass算光 |
| 动态光源 | 最多4个点光源（硬编码） | 支持多光源，按屏幕空间计算 |
| 反射 | 反射球采样，HQ最多3个 | 支持SSR |
| 材质复杂度 | 光照代码编入材质Shader，指令多 | 材质Shader轻量，光照分离 |
| 兼容性 | 最广，所有移动GPU | 需要Tile-Based GPU更优 |
| 适用场景 | 预计算光照项目、多反射球 | 动态光照多、开放世界 |
| 抗锯齿 | 支持MSAA等多种选项 | 仅FXAA/TAA |

---

## 2. 核心源码文件清单

### 2.1 C++ 层（Engine/Source/Runtime/Renderer/Private/）

| 文件 | 职责 | 关键类/函数 |
|------|------|------------|
| `MobileShadingRenderer.cpp/.h` | Mobile渲染器主入口 | `FMobileSceneRenderer::Render()`, `RenderForward()`, `RenderDeferred()` |
| `MobileBasePassRendering.cpp/.h` | BasePass CPU端数据组织 | `FMobileBasePassMeshProcessor`, `RenderMobileBasePass()`, `SetupMobileBasePassAfterShadowInit()` |
| `SceneVisibility.cpp/.h` | 可见性系统 | `ComputeViewVisibility()`, `GatherDynamicMeshElements()`, `ApplyHZB()` |
| `MeshDrawCommand.cpp/.h` | 绘制命令 | `FMeshDrawCommand`, `FParallelMeshDrawCommandPass::DispatchDraw()` |
| `MeshPassProcessor.cpp/.h` | Pass处理器框架 | `FMeshPassProcessor`, `FPassProcessorManager::JumpTable` |
| `ShadowDepthRendering.cpp/.h` | 阴影深度 | `RenderShadowDepthMaps()` |
| `TranslucentRendering.cpp/.h` | 半透明渲染 | `RenderTranslucency()` |
| `PostProcess/` 目录 | 后处理 | 各后处理效果的RDG Pass |
| `SceneRendering.cpp/.h` | 场景渲染基类 | `FSceneRenderer`, `FSceneView`, `FViewInfo` |

### 2.2 Shader 层（Engine/Shaders/）

| 文件 | 职责 | 关键内容 |
|------|------|---------|
| `MobileBasePassPixelShader.usf` | ★ BasePass像素着色器 | 动态点光源循环、反射球采样、CSM阴影、Phong高光近似 |
| `MobileBasePassVertexShader.usf` | BasePass顶点着色器 | 世界坐标变换、WPO、顶点雾 |
| `MobileBasePassCommon.ush` | BasePass共享定义 | 插值器结构体、Pass共用工具函数 |
| `DynamicLightingCommon.ush` | 动态光照 | 点光源衰减、颜色计算 |
| `ShadowFilteringCommon.ush` | 阴影滤波 | CSM采样与PCF滤波 |
| `ReflectionEnvironmentShared.ush` | 反射环境 | 反射球采样、HQ反射混合 |
| `LightmapCommon.ush` | 光照图 | LQ/HQ光照图采样 |
| `BRDF.ush` | BRDF函数 | 移动端简化的BRDF实现 |
| `SHCommon.ush` | 球谐函数 | 环境光球谐计算 |
| `HeightFogCommon.ush` | 高度雾 | 指数高度雾计算 |
| `PlanarReflectionShared.ush` | 平面反射 | 平面反射采样 |
| `Material.usf` / `Material.ush` | 材质系统 | 材质属性输出、Shader Permutation |

---

## 3. 逐阶段代码走读

### 3.1 FMobileSceneRenderer::Render() — 渲染入口

```cpp
// MobileShadingRenderer.cpp
void FMobileSceneRenderer::Render(FRDGBuilder& GraphBuilder)
{
    // 1. 更新所有图元场景信息（GPU Scene）
    FScene::UpdateAllPrimitiveSceneInfos(...);

    // 2. 初始化视图（可见性剔除的核心）
    InitViews(GraphBuilder, SceneTexturesConfig,
              InstanceCullingManager, VirtualTextureUpdater, InitViewTaskDatas);

    // 3. 根据bDeferredShading分支
    if (bDeferredShading)
        RenderDeferred(GraphBuilder, SortedLightSet, ViewFamilyTexture, SceneTextures);
    else
        RenderForward(GraphBuilder, ViewFamilyTexture, SceneTextures);

    // 4. 帧尾更新
    FScene::UpdateAllPrimitiveSceneInfos(...);
}
```

**学习要点**：
- `bDeferredShading` 由 `r.Mobile.ShadingPath` 决定
- RDG (`FRDGBuilder`) 自UE4.26起贯穿移动端渲染全流程
- `InitViews` 是CPU性能热点之一

### 3.2 InitViews() — 可见性系统

```cpp
void FMobileSceneRenderer::InitViews(...)
{
    // 并行处理可见性任务
    TaskDatas.VisibilityTaskData->ProcessRenderThreadTasks();
        // |-- ComputeViewVisibility()     // 视锥剔除 + 遮挡剔除(HZB)
        // |-- GatherDynamicMeshElements() // 收集动态物体

    // 完成动态网格收集，建立MeshPass
    TaskDatas.VisibilityTaskData->FinishGatherDynamicMeshElements(
        BasePassDepthStencilAccess, InstanceCullingManager, VirtualTextureUpdater);
        // |-- SetupMeshPasses()           // 为每个Pass创建FMeshDrawCommand
        //     |-- ComputeDynamicMeshRelevance()
}
```

**学习要点**：
- 移动端遮挡剔除：硬件Occlusion Query + 可选软件遮挡（`r.Mobile.AllowSoftwareOcclusion`）
- HZB（Hierarchical Z-Buffer）遮挡剔除：`ApplyHZB()`
- `GatherDynamicMeshElements` 是CPU瓶颈常见来源
- MeshDrawCommand机制：缓存的StaticMeshDrawCommand + 每帧重建的DynamicMeshDrawCommand

### 3.3 RenderForward() — 前向渲染主流程

```cpp
void FMobileSceneRenderer::RenderForward(
    FRDGBuilder& GraphBuilder,
    FRDGTextureRef ViewFamilyTexture,
    FSceneTextures& SceneTextures)
{
    // 1. 可选：深度预通行（减少Overdraw）
    RenderPrePass(GraphBuilder, ...);

    // 2. ★ 核心：MobileBasePass
    RenderMobileBasePass(GraphBuilder, ...);

    // 3. 可选效果
    //    - RenderDeferredDecals()    // 贴花
    //    - AmbientOcclusion          // AO
    //    - Atmospheric Fog / SkyAtm  // 大气效果

    // 4. 半透明
    RenderTranslucency(GraphBuilder, ...);

    // 5. 后处理
    RenderPostProcessing(GraphBuilder, ...);
}
```

**学习要点**：
- PrePass在移动端TBDR架构上有特殊意义：利用On-Chip Tile Memory减少主存读写
- BasePass输出直接就是最终颜色，不像Deferred写GBuffer

### 3.4 RenderMobileBasePass() — 核心BasePass

```cpp
// CPU端
void FMobileSceneRenderer::RenderMobileBasePass(FRDGBuilder& GraphBuilder, ...)
{
    // 更新View UniformBuffer
    View.ParallelMeshDrawCommandPasses[EMeshPass::BasePass].DispatchDraw(
        GraphBuilder, ...);
    // 内部: SubmitMeshDrawCommandsRange -> RHI Draw Commands
}

// Pass注册机制
// FPassProcessorManager::JumpTable 存储每个Pass的创建函数
// MobileBasePass 注册: FMobileBasePassMeshProcessor
//   |-- FMeshPassProcessorRenderState (指定View UB + Pass独有UB)
//   |-- 处理Static/Dynamic Mesh Batch
```

**学习要点**：
- `FMobileBasePassMeshProcessor` 决定了哪些材质Permutation参与BasePass
- `FMeshPassProcessorRenderState` 绑定View UniformBuffer和Pass专属UB
- `FParallelMeshDrawCommandPass` 支持并行提交Draw Command

### 3.5 MobileBasePassPixelShader.usf — Shader核心

```hlsl
// 关键宏控制Shader分支
#define FULLY_ROUGH    (MATERIAL_FULLY_ROUGH || MOBILE_QL_FORCE_FULLY_ROUGH)
#define NONMETAL       (MATERIAL_NONMETAL || MOBILE_QL_FORCE_NONMETAL)
#define HQ_REFLECTIONS (MATERIAL_HQ_FORWARD_REFLECTIONS && !MOBILE_QL_FORCE_LQ_REFLECTIONS)

// 动态点光源（硬编码最大4个）
#if MAX_DYNAMIC_POINT_LIGHTS > 0
  #if VARIABLE_NUM_DYNAMIC_POINT_LIGHTS
    int NumDynamicPointLights;
  #endif
  float4 LightPositionAndInvRadius[MAX_DYNAMIC_POINT_LIGHTS];
  float4 LightColorAndFalloffExponent[MAX_DYNAMIC_POINT_LIGHTS];
#endif

// 反射球
#if !FULLY_ROUGH
  TextureCube ReflectionCubemap;
  #if HQ_REFLECTIONS
    #define MAX_HQ_REFLECTIONS 3
    TextureCube ReflectionCubemap1/2;
    float4 ReflectionPositionsAndRadii[MAX_HQ_REFLECTIONS];
  #endif
#endif
```

**Shader主要流程**：
1. 计算材质属性（从Material.usf获取BaseColor/Metallic/Roughness/Normal）
2. 采样光照图（如果有预计算光照）
3. 采样CSM阴影（`MOBILE_CSM_QUALITY`控制质量档位）
4. 累加动态点光源（最多4个，循环遍历）
5. 采样反射球（LQ: 1个；HQ: 最多3个混合）
6. 计算天光（SH球谐）
7. 雾效混合
8. 输出最终颜色

**Phong高光近似**（移动端优化）：
```hlsl
half PhongApprox(half Roughness, half RoL)
{
    half a = Roughness * Roughness;
    a = max(a, 0.008);        // FP16安全下界
    half a2 = a * a;
    half rcp_a2 = rcp(a2);
    half c = 0.72134752 * rcp_a2 + 0.39674113;
    half p = rcp_a2 * exp2(c * RoL - c);  // 球谐高斯近似
    return min(p, rcp_a2);    // Mali GPU防溢出
}
```

---

## 4. 关键CVar速查

| CVar | 默认值 | 说明 |
|------|--------|------|
| `r.Mobile.ShadingPath` | 0 | 0=Forward, 1=Deferred |
| `r.Mobile.AllowSoftwareOcclusion` | 0 | 启用软件遮挡剔除 |
| `r.Mobile.EnableStaticAndCSMCombinedShadow` | - | 合并静态/CSM阴影 |
| `r.Mobile.AllowDistanceFieldShadows` | - | SDF阴影（移动端） |
| `r.Mobile.AmbientOcclusion` | - | 移动端AO |
| `r.Mobile.DisableVertexFog` | - | 禁用顶点雾（改用像素雾） |
| `r.Mobile.EnableNoPrecomputedLighting` | - | 无预计算光照模式 |
| `r.EarlyZPass` | - | 控制PrePass行为 |
| `r.NeverOcclusionTestDistance` | 2000(Android) | 小于此距离不查询遮挡 |

**Quality Level宏控制**（在Shader中通过permutation切换）：

| 宏 | 作用 | 性能影响 |
|----|------|---------|
| `MOBILE_QL_FORCE_FULLY_ROUGH` | 强制完全粗糙（跳过反射） | 大幅减少ALU |
| `MOBILE_QL_FORCE_NONMETAL` | 强制非金属（简化BRDF） | 减少ALU |
| `MOBILE_QL_FORCE_LQ_REFLECTIONS` | 强制低质量反射 | 减少纹理采样 |
| `MOBILE_CSM_QUALITY` | CSM阴影质量(0/1/2) | 影响PCF采样次数 |

---

## 5. 推荐学习路径

### Phase 1: 建立全局认知（1-2天）

1. **阅读Epic官方文档**
   - [Mobile Rendering and Shading Modes](https://dev.epicgames.com/documentation/ru-ru/unreal-engine/mobile-rendering-and-shading-modes-for-unreal-engine)
   - [Software Occlusion Queries for Mobile](https://dev.epicgames.com/documentation/zh-cn/unreal-engine/software-occlusion-queries-for-mobile)

2. **跑一遍源码入口**
   - 打开 `MobileShadingRenderer.cpp`，从 `FMobileSceneRenderer::Render()` 开始
   - 用GPU Visualizer（`Ctrl+Shift+,`）对照理解每个Pass

### Phase 2: 逐Pass深入（3-5天）

3. **InitViews — 可见性系统**
   - 源码: `SceneVisibility.cpp`
   - 重点: `ComputeViewVisibility()`、`GatherDynamicMeshElements()`
   - 对比PC端和Mobile端的剔除差异

4. **Shadow — 阴影系统**
   - 源码: `ShadowDepthRendering.cpp`
   - 重点: CSM级联阴影、SDF阴影移动端适配
   - Mobile Forward的阴影是编入BasePass PS的

5. **MobileBasePass — 核心Pass**
   - C++: `MobileBasePassRendering.cpp` → `FMobileBasePassMeshProcessor`
   - Shader: `MobileBasePassPixelShader.usf`（重点啃这个文件）
   - 对比阅读: `MobileBasePassVertexShader.usf`

### Phase 3: Shader深入（3-5天）

6. **光照计算**
   - `DynamicLightingCommon.ush` — 4点光源循环
   - `BRDF.ush` — 移动端简化BRDF
   - `PhongApprox()` — 高光近似，理解为什么不用GGX

7. **反射与IBL**
   - `ReflectionEnvironmentShared.ush` — LQ/HQ反射球
   - `SHCommon.ush` — 球谐环境光
   - FULLY_ROUGH / NONMETAL宏如何裁剪Shader分支

8. **阴影**
   - `ShadowFilteringCommon.ush` — CSM滤波
   - `MOBILE_CSM_QUALITY` 三档对比

### Phase 4: 实战与扩展（持续）

9. **RenderDoc截帧分析**
   - 真机上抓一帧，对照源码理解每个RenderPass
   - 关注Tile Memory利用、Load/Store操作

10. **扩展改造参考**
    - Cluster多光源剔除: [UE4 4.27 Mobile Forward Cluster改造](https://blog.csdn.net/qq_29523119/article/details/123102447)
    - Forward+ LightGrid: [UE4 Forward+流程分析](https://blog.csdn.net/kuangben2000/article/details/135188219)

---

## 6. 优秀参考文章

| 文章 | 侧重 | 链接 |
|------|------|------|
| UE源码渲染机制解析 | 全流程图+代码对照 | https://natsuneko3.github.io/2022/11/13/ue源码/渲染机制解析/ |
| UE5 Mesh Drawing Pipeline | MeshDrawCommand机制详解 | https://suikasan111.github.io/2024/06/24/UE5/渲染架构/UE5-MeshDrawingPipeline/ |
| UE4.26 Lightmap从烘焙到渲染 | MobileBasePass Shader Binding详解 | https://qiutang98.github.io/post/unreal/ue4.26-lightmap从烘焙到渲染/ |
| UE移动端灯光/反射/优化 | UE5.5移动端渲染总览 | https://blog.csdn.net/boxiaozi/article/details/159355298 |
| UE4渲染流程 | FMobileSceneRenderer流程概述 | https://blog.csdn.net/qq_33060405/article/details/143899080 |
| UE4 Forward+流程分析 | LightGrid/Cluster光源剔除 | https://blog.csdn.net/kuangben2000/article/details/135188219 |
| Mobile Forward Cluster多光源 | 改造Forward支持多光源 | https://blog.csdn.net/qq_29523119/article/details/123102447 |
| UE Shader开发技巧 | 渲染管线入口+调试方法 | https://www.163.com/dy/article/JTP7H3HU0511L9VL.html |
| Qualcomm UE5 30FPS实践 | 骁龙移动端CVar调优 | https://www.qualcomm.com/developer/blog/2026/01/run-unreal-engine-5-content-30fps-snapdragon-mobile |

---

## 7. 源码阅读技巧

1. **从Render()入口走，不要从中间插入**
2. **用GPU Visualizer对照**：Editor中按 `Ctrl+Shift+,`，逐Pass看耗时和输出
3. **RenderDoc真机截帧**：每个RenderPass对应一段C++代码，双向对照
4. **关注宏条件**：Mobile Shader大量用宏控制Permutation，`FULLY_ROUGH`/`NONMETAL`等
5. **RDG系统**：UE5移动端已全面RDG化，理解`FRDGBuilder`的Pass注册和资源依赖
6. **Shader编译**：修改.ush/usf后不需要全量编译引擎，可用RenderDoc快速迭代

---

## 8. 架构速记图

```
+------------------------------------------------------------------+
|                    FMobileSceneRenderer::Render()                 |
+------------------------------------------------------------------+
        |                    |                     |
   UpdatePrimitives     InitViews            RenderForward
   (GPU Scene)          (可见性)             (前向主流程)
        |                    |                     |
        |            +-------+--------+     +------+------+
        |            |       |        |     |      |      |
        |         ComputeV  Gather   Setup  PrePass Base  Translucency
        |         isibility Dynamic  Mesh   (可选)  Pass  PostProcess
        |                   Mesh    Passes          |
        |                   Elems                   |
        |                                           |
        v                                           v
  +------------+                     +---------------------------+
  | GPU Scene  |                     | MobileBasePassPixelShader |
  | Buffer     |                     |                           |
  | (SceneUB)  |                     | Material -> Lighting ->   |
  +------------+                     | Shadow -> Reflection ->   |
                                     | Fog -> Output Color       |
                                     +---------------------------+
```
