# UE Mobile Forward vs Deferred —— 深度补充 01：可见性与遮挡剔除

> 本系列补充文档系睡眠期间持续迭代生成。配套主索引：`UE_Mobile_Forward_vs_Deferred_Tech_Doc_Index.md`
> 本篇聚焦：**SceneOcclusion / HZB / SoftwareOcclusion / OcclusionQuery / InstanceCulling** 在 Mobile 双管线下的实现差异、调度时机与项目 patch。

---

## 0. 移动端可见性系统全景

| 子系统 | 调度位置 | Forward 用 | Deferred 用 | 共享? |
|--------|---------|-----------|------------|------|
| 视锥剔除 (Frustum) | `ComputeViewVisibility` | ✅ | ✅ | 共享 |
| 距离剔除 (CullDistance) | `ComputeViewVisibility` | ✅ | ✅ | 共享 |
| 软件遮挡 (SoftwareOcclusion) | `InitViews` 内 | ✅（Mobile 主用） | ✅（Mobile 主用） | 共享 |
| 硬件 OcclusionQuery | BasePass 末尾 | ✅ | ✅ | 共享但调度位置略异 |
| HZB Occlusion | InitViews + 帧末 RenderHZB | ✅ | ✅ | 共享 |
| InstanceCullingOcclusionQuery | per-instance | ✅ | ✅ | 共享 |
| Per-Object Cull | Mesh Card | – | – | – |

**关键判定**：`bHZBOcclusion = r.HZBOcclusion != 0 && r.Mobile.AllowSoftwareOcclusion == 0`。
即 **HZB 与 SoftwareOcclusion 互斥**，移动端二选一。

---

## 1. `FMobileSceneRenderer::RenderOcclusion` 两个重载

源码：`SceneOcclusion.cpp:2315 / 2335`

### 1.1 RHI 版本（BasePass 末尾发硬件查询）

```cpp
void FMobileSceneRenderer::RenderOcclusion(FRHICommandList& RHICmdList)
{
    if (!DoOcclusionQueries()) return;

    FViewOcclusionQueriesPerView QueriesPerView;
    AllocateOcclusionTests(QueriesPerView, Scene, VisibleLightInfos, Views);

    if (QueriesPerView.Num())
        BeginOcclusionTests(RHICmdList, Views, FeatureLevel, QueriesPerView, 1.0f);
}
```

- 由 `RenderForwardSinglePass` / `RenderDeferredSinglePass` 在 BasePass Subpass 末尾内联调用
- 利用 BasePass 的 Depth 仍在 Tile 内的特性，直接发 Bounds Box Draw + OcclusionQuery，**不出 Tile**

### 1.2 RDG 版本（项目添加，发 HZB 测试）

```cpp
// GR ADD Begin  SceneOcclusion.cpp:2335
void FMobileSceneRenderer::RenderOcclusion(FRDGBuilder& GraphBuilder, FRDGTextureRef SceneDepthTexture)
{
    bool bHZBOcclusion = r.HZBOcclusion != 0
                      && r.Mobile.AllowSoftwareOcclusion == 0
                      && !ViewFamily.EngineShowFlags.SimpleSceneRendering;
    bool bHZBIndirectDraw = r.HZB.IndirectDraw != 0;

    if (!bHZBOcclusion) {
        // 清掉 HZBOcclusionTests 数据
        return;
    }
    RenderHZB(GraphBuilder, SceneDepthTexture);
    if (!bHZBIndirectDraw) {
        for view: ViewState->HZBOcclusionTests.Submit(GraphBuilder, View);
    }
}
```

- 由 `MobileShadingRenderer.cpp:1645` 在 `MobileHZBOcclusion` 阶段调用，在 BasePass 出 Tile 之后、半透明之前
- 用 SceneDepth Resolve 后的纹理生成 HZB pyramid，再发 HZB 测试（一帧后才得到结果）

> 两条路径都共享这套，但 Forward 因为 Tile-in-Tile 单 RenderPass 的特性，`RenderOcclusion(RHICmdList)` 更高效；Deferred 多 Pass 时被迫走 RDG 版本。

---

## 2. 三种遮挡策略对比

### 2.1 硬件 OcclusionQuery（默认）

```cpp
// MobileShadingRenderer.cpp:1946 / 2075 / 2314 / 2419
const bool bDoOcclusionQueries =
    (!bIsFullDepthPrepassEnabled && ViewContext.bIsLastView && DoOcclusionQueries());
PassParameters->RenderTargets.NumOcclusionQueries =
    bDoOcclusionQueries ? ComputeNumOcclusionQueriesToBatch() : 0u;
```

- **关闭条件**：
  - `bIsFullDepthPrepassEnabled = true`（PrePass 已经做了完整 Z，不需要 OcclusionQuery）
  - 非最后一个 View（多视口只在末视口发）
- **Adreno 特化**：`r.Mobile.AdrenoOcclusionMode != 0 && IsOpenGLPlatform(...)` 时发查询前调用 `SubmitCommandsHint()` 强制 flush，避免 Adreno 的延迟提交问题
- **NumOcclusionQueries Batch 大小**：`ComputeNumOcclusionQueriesToBatch()` 提前算好，对 `RenderTargets` 预声明，省去运行时动态分配

### 2.2 HZB 遮挡（Mobile 5.x+ 新增）

```cpp
// MobileShadingRenderer.cpp:702 / 1645-1647
bShouldRenderHZB = ShouldRenderHZB(Views) && bRendererOutputFinalSceneColor;
...
RDG_EVENT_SCOPE(GraphBuilder, "MobileHZBOcclusion");
RenderOcclusion(GraphBuilder, SceneTextures.Depth.Resolve);
```

- 利用上一帧的 HZB 测试该帧候选 Bounding Box
- **必须有 SceneDepth.Resolve**（出 Tile），所以会触发 `bKeepDepthContent`
- 优势：**视锥外的物体也能在 GPU 上批量测试**，CPU 端 Setup 开销低

### 2.3 软件遮挡（移动端独占）

源码：`SceneSoftwareOcclusion.cpp`（38 KB）

```cpp
// SceneVisibility.cpp:7170 附近调用
if (r.Mobile.AllowSoftwareOcclusion != 0) {
    Scene->SceneSoftwareOcclusion.Process(View, ...);
}
```

- **CPU 侧三角形光栅化遮挡**：手动选定 Occluder Mesh，软光栅化产生低分辨率 Depth Buffer，CPU 端测试 Bounding Box
- **优势**：完全避开 GPU OcclusionQuery 的一帧延迟、避开 Adreno 的 driver bug、避开 HZB 的 Resolve 成本
- **劣势**：CPU 占用高（需要主线程 + Worker Thread 配合）
- **场景**：开放世界 / 大量遮挡物（建筑、地形）的地图项目

> 项目实战：**腾讯北极星 / 王者荣耀 / 和平精英 类项目通常用 Software Occlusion**；多人 FPS 战场地图配合 Procedural Occluder 效果显著。

---

## 3. InstanceCulling Occlusion Query（5.4+ 新增）

源码：`InstanceCulling/InstanceCullingOcclusionQuery.h`

### 3.1 数据流

```cpp
// MobileShadingRenderer.cpp:566-572
if (InstanceCullingManager.IsEnabled()
    && Scene->InstanceCullingOcclusionQueryRenderer
    && Scene->InstanceCullingOcclusionQueryRenderer->InstanceOcclusionQueryBuffer)
{
    InstanceCullingManager.InstanceOcclusionQueryBuffer =
        GraphBuilder.RegisterExternalBuffer(...);
    InstanceCullingManager.InstanceOcclusionQueryBufferFormat = ...;
}
```

`FInstanceCullingOcclusionQueryRenderer::InstanceOcclusionQueryBuffer` 是一张 GPU Buffer，每个 InstanceID 对应一 bit/mask。

### 3.2 Per-Instance Mask

```cpp
// SceneRendering.h:1198-1201
uint32 InstanceOcclusionQueryMask = 0;
// 用于解读 InstanceOcclusionQueryBuffer 的 per-instance bit
```

- 单 Mesh 多 Instance 时（如 ISM/HISM/Nanite-like 物体），每个 instance 独立做 OQ
- 移动端 Foliage、Grass Cluster 极适用

### 3.3 调度

```cpp
// MobileShadingRenderer.cpp:1684-1690
if (ViewFamily.EngineShowFlags.VisualizeInstanceOcclusionQueries
    && Scene->InstanceCullingOcclusionQueryRenderer)
{
    for (FViewInfo& View : Views)
        Scene->InstanceCullingOcclusionQueryRenderer->RenderDebug(GraphBuilder, Scene->GPUScene, View, SceneTextures);
}
```

- `EndFrame`：`Scene->InstanceCullingOcclusionQueryRenderer->EndFrame(GraphBuilder);` (1769-1771)

> Forward / Deferred 共享该机制，**不分路径**。

---

## 4. 双路径 Occlusion 调度时序对比

### 4.1 Forward SinglePass

```
Subpass 0 (BasePass)
  └─ Draw scene
  └─ [if bDoOcclusionQueries]
     ├─ (Adreno) SubmitCommandsHint
     └─ RenderOcclusion(RHICmdList)  ← Tile-in 硬件查询
Subpass 1 (Decal+Translucency)
  ...
[出 RenderPass]
RenderOcclusion(GraphBuilder, Depth.Resolve)  ← HZB 测试
FenceOcclusionTests(GraphBuilder)
```

### 4.2 Deferred SinglePass

```
Subpass 0 (GBuffer Write)
  └─ Draw scene
Subpass 1 (Decal)
Subpass 2 (Lighting + Translucency)
  └─ [if bDoOcclusionQueries] RenderOcclusion(RHICmdList) ← 在最后子 pass 末发
[出 RenderPass]
RenderOcclusion(GraphBuilder, Depth.Resolve)  ← HZB 测试
FenceOcclusionTests(GraphBuilder)
```

### 4.3 Fence 行为差异

```cpp
// MobileShadingRenderer.cpp:1628-1631
if (!bIsFullDepthPrepassEnabled)
{
    FenceOcclusionTests(GraphBuilder);
}
```

- **Forward**：FenceOcclusionTests 在所有视图遍历完成、`EndOcclusionScope` 之后
- **Deferred**：同样调度，但前面 `SceneTextures.MobileSetupMode = All` 才刚 setup 完 GBuffer 给后处理用

---

## 5. `NeverOcclusionTestDistance` —— 移动端"近物免测试"

源码：`SceneVisibility.cpp:293`

```cpp
static FAutoConsoleVariableRef CVarNeverOcclusionTestDistance(
    TEXT("r.NeverOcclusionTestDistance"),
    GNeverOcclusionTestDistance, // 默认 0，Android 平台一般在 .ini 配 2000
    ...
);
```

```cpp
// SceneVisibility.cpp:3618
if (FVector::DistSquared(ViewOrigin, OcclusionBounds.Origin) < NeverOcclusionTestDistanceSquared)
    bAllowBoundsTest = false;
```

> Android `.ini`：
> ```
> [/Script/Engine.RendererSettings]
> r.NeverOcclusionTestDistance=2000
> ```
> 含义：玩家半径 20m 内的物体跳过 OcclusionQuery，**直接当成可见**。避免移动端 OcclusionQuery 一帧延迟引起的"近处物体闪烁"。

适用：**FPS / TPS** 项目（玩家持枪、面部、UI 近物）。RTS 项目可不开。

---

## 6. `bHZBOcclusion` 与 `bKeepDepthContent` 联动

```cpp
// MobileShadingRenderer.cpp:715-728
bKeepDepthContent =
    bRequiresMultiPass ||
    bForceDepthResolve ||
    ...
    bShouldRenderHZB ||           // ← 开 HZB 必须保留深度
    bHZBOcclusion ||              // ← 开 HZB Occlusion 同样要求
    GraphBuilder.IsDumpingFrame();
```

> **隐含成本**：开启 HZB 会强制 SceneDepth 出 Tile 到主存，对中低端机型带宽损耗显著。
>
> **推荐策略**：
> - 大世界 / 室外 → HZB ON（剔除收益 > 带宽损失）
> - 紧凑室内 → 关 HZB，用 SoftwareOcclusion 或纯 OcclusionQuery
> - VR / Mobile MultiView → 关 HZB（多视口 HZB 复杂）

---

## 7. SceneCapture / SimpleSceneRendering 屏蔽路径

```cpp
// MobileSceneRenderer 构造
for (FViewInfo& View : Views) {
    if (View.bIsSceneCapture) {
        View.bDisableQuerySubmissions = true;
        View.bIgnoreExistingQueries = true;
    }
}
```

并在 RenderOcclusion 阶段：

```cpp
// MobileShadingRenderer.cpp:1013
bool bDoOcclusionQueries = (ViewContext.bIsLastView && DoOcclusionQueries()
                            && !bIsSceneCaptureRenderPass);
```

> SceneCapture（如 CubeCapture / RenderTarget Capture）跳过所有 OQ —— 因为这些不需要遮挡剔除收益，每帧只渲一次。这也意味着 SceneCapture 内的 BasePass 不能复用 OQ batch slot。

---

## 8. Adreno 特化模式详解

```cpp
// MobileShadingRenderer.cpp:1992-1997
const bool bAdrenoOcclusionMode = (CVarMobileAdrenoOcclusionMode.GetValueOnRenderThread() != 0
                                  && IsOpenGLPlatform(ShaderPlatform));
if (bAdrenoOcclusionMode) {
    RHICmdList.SubmitCommandsHint();  // 强制 driver flush
}
RenderOcclusion(RHICmdList);
```

**原因**：Adreno GLES driver 把 OQ 提交延迟到下一帧 Bind 时才执行，导致：
1. 查询结果**滞后两帧**（标准已经是 1 帧延迟）
2. 物体高速移动时 popping 严重

**解决**：发 OQ 前 Flush command stream，强制 driver 立即 schedule OQ batch。

**副作用**：CPU↔GPU sync 增加，帧率轻微下降，但视觉稳定性显著提升。

---

## 9. Multi-Buffered OcclusionFence

源码：`SceneOcclusion.cpp:2389 FenceOcclusionTests`

```cpp
void FSceneRenderer::FenceOcclusionTests(FRDGBuilder& GraphBuilder)
{
    if (DoOcclusionQueries() && IsRunningRHIInSeparateThread()) {
        AddPass(...[]() {
            if (ViewFamily.bIsMultipleViewFamily) {
                // 多视口家族：buffered fence 队列
                check(OcclusionSubmittedFence[MaxBufferedOcclusionFrames - 1].Fence == nullptr);
                // 循环向后推
            } else {
                int32 NumFrames = FOcclusionQueryHelpers::GetNumBufferedFrames(FeatureLevel);
                // 固定 N 帧缓冲
            }
            OcclusionSubmittedFence[0].Fence = RHICmdList.RHIThreadFence();
        });
        GraphBuilder.AddDispatchHint();
    }
}
```

- 移动端 ES3_1 Feature Level 一般 `NumBufferedFrames = 1`（PC 是 4）
- 因此 OQ 结果在**下一帧**就能拿到，但物体闪现风险高
- 大世界项目可考虑提升到 2（增加内存，减少闪现）

---

## 10. 实战调优 Cheatsheet（针对本工程 GR 类项目）

```ini
; 大世界开放场景推荐配置
[/Script/Engine.RendererSettings]
r.HZBOcclusion=1
r.HZB.IndirectDraw=1
r.Mobile.AllowSoftwareOcclusion=0
r.NeverOcclusionTestDistance=2000
r.Mobile.AdrenoOcclusionMode=1
r.Mobile.AllowHZB=1

; 室内紧凑场景推荐配置
r.HZBOcclusion=0
r.Mobile.AllowSoftwareOcclusion=1
r.NeverOcclusionTestDistance=1000

; 单房间 / Cutscene 关闭剔除省 CPU
r.AllowOcclusionQueries=0
```

---

## 11. 易错点

| 现象 | 可能原因 | 排查 |
|------|---------|------|
| Deferred 路径 HZB 不更新 | bKeepDepthContent 未设 | `bShouldRenderHZB` 与 `bHZBOcclusion` 必须有一个为 true |
| Forward + MSAA 下 HZB 错位 | MSAA Depth 没 Resolve | `GRHISupportsDepthStencilResolve` 检查 |
| OQ 一直返回 0 | View.bDisableQuerySubmissions=true（SceneCapture） | 检查 View Family |
| 物体延迟两帧才出现 | Adreno 模式未开 | `r.Mobile.AdrenoOcclusionMode=1` |
| HZB 占用 8MB+ | HZB 默认 PF_R16F + Mip Chain | 关闭无用 mip level |
| InstanceCullingOQ 全部失效 | `Scene->InstanceCullingOcclusionQueryRenderer` 为空 | GPU Scene 必须启用 |
| FullDepthPrepass 下 OQ 跳过 | `!bIsFullDepthPrepassEnabled` 排他 | 该模式下 Z 已知，OQ 不必要 |

---

> 第 01 篇完。下一篇：**ShadowDepthRendering / CSM / 移动端阴影完整流程**。
