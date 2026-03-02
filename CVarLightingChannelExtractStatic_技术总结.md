# CVarLightingChannelExtractStatic 技术总结

## 一、背景与问题

在 UE5 渲染管线中，`LightingChannelsTexture`（光照通道纹理）由 `CopyStencilToLightingChannelTexture` 函数每帧通过 `GraphBuilder.CreateTexture` 创建为 **RDG 瞬态纹理（Transient Texture）**。瞬态纹理在帧结束后会被 RDG 自动回收，下一帧需要重新分配。

在某些场景下，这种每帧重新分配的行为会导致 **RDG Transient Resource Cache Miss**（瞬态资源缓存未命中），引发不必要的显存分配/释放开销，影响性能。

## 二、解决方案

仿照已有的 `CVarSceneDepthZExtractStatic`（控制 `DepthBuffer` 的持久化策略），新增 `CVarLightingChannelExtractStatic` 控制变量，通过 `GraphBuilder.QueueTextureExtraction` 将 `LightingChannelsTexture` 提取为持久化资源，跨帧保留纹理，避免缓存未命中。

### 控制逻辑

| 控制台变量 `r.LightingChannelExtractStatic` | 行为 |
|---|---|
| **`0`（默认，false）** | `LightingChannelsTexture` 由 `GraphBuilder.CreateTexture` 创建为瞬态纹理，帧结束后自动回收 |
| **`1`（true）** | 额外调用 `GraphBuilder.QueueTextureExtraction` 将纹理持久化到 `PrevFrameViewInfo.LightingChannelsTexture`，跨帧保留，避免 Transient Resource Cache Miss |

## 三、修改文件清单

| 文件 | 修改内容 |
|---|---|
| `Engine/Source/Runtime/Renderer/Private/DeferredShadingRenderer.cpp` | CVar 变量定义（已有）+ 添加 `QueueTextureExtraction` 逻辑 |
| `Engine/Source/Runtime/Renderer/Private/SceneRendering.h` | 在 `FPreviousViewInfo` 中添加 `LightingChannelsTexture` 存储字段 |

## 四、修改详情

### 4.1 SceneRendering.h — 添加持久化存储字段

在 `FPreviousViewInfo` 结构体中，于 `DepthBuffer` 字段后新增 `LightingChannelsTexture` 字段：

```cpp
// Depth buffer and Normals of the previous frame generating this history entry for bilateral kernel rejection.
TRefCountPtr<IPooledRenderTarget> DepthBuffer;
#pragma region Engine ZXB
// LightingChannels纹理，用于跨帧缓存避免RDG瞬态资源缓存未命中
TRefCountPtr<IPooledRenderTarget> LightingChannelsTexture;
#pragma endregion
TRefCountPtr<IPooledRenderTarget> GBufferA;
```

**说明**：`TRefCountPtr<IPooledRenderTarget>` 是 UE 渲染器中标准的持久化纹理引用类型，`QueueTextureExtraction` 会将 RDG 纹理提取到该类型的智能指针中，跨帧保持引用计数，防止被回收。

### 4.2 DeferredShadingRenderer.cpp — CVar 定义（已有）

CVar 变量定义位于文件约第 174 行，与 `CVarSceneDepthZExtractStatic` 相邻：

```cpp
static int32 GLightChannelExtractStatic = 0;
static FAutoConsoleVariableRef CVarLightingChannelExtractStatic(
    TEXT("r.LightingChannelExtractStatic"),
    GLightChannelExtractStatic,
    TEXT("Add by ZXB for RDG Transient Resource Cache Miss"),
    ECVF_RenderThreadSafe);
```

### 4.3 DeferredShadingRenderer.cpp — 添加 QueueTextureExtraction 逻辑

在 `CopyStencilToLightingChannelTexture` 调用之后（约第 3121 行），插入持久化逻辑：

```cpp
FRDGTextureRef LightingChannelsTexture = CopyStencilToLightingChannelTexture(
    GraphBuilder, SceneTextures.Stencil, NaniteShadingMask);

#pragma region Engine ZXB
// Add by ZXB for RDG Transient Resource Cache Miss
if (GLightChannelExtractStatic && LightingChannelsTexture)
{
    for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ++ViewIndex)
    {
        const FViewInfo& View = Views[ViewIndex];
        if (View.ViewState && !View.bStatePrevViewInfoIsReadOnly)
        {
            GraphBuilder.QueueTextureExtraction(
                LightingChannelsTexture,
                &View.ViewState->PrevFrameViewInfo.LightingChannelsTexture);
        }
    }
}
#pragma endregion
```

**关键保护条件**：
- `GLightChannelExtractStatic`：CVar 开关控制，默认关闭
- `LightingChannelsTexture`：空指针检查，确保纹理已创建
- `View.ViewState`：确保视图状态有效
- `!View.bStatePrevViewInfoIsReadOnly`：确保前一帧视图信息可写

## 五、工作原理流程图

```
帧 N:
┌──────────────────────────────────────────────────────────────┐
│  CopyStencilToLightingChannelTexture()                       │
│  → GraphBuilder.CreateTexture() 创建 LightingChannelsTexture │
│                                                              │
│  if (GLightChannelExtractStatic == true)                     │
│  → GraphBuilder.QueueTextureExtraction()                     │
│    将纹理提取到 PrevFrameViewInfo.LightingChannelsTexture    │
│    (TRefCountPtr 引用计数 +1，纹理不被回收)                    │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
帧 N+1:
┌──────────────────────────────────────────────────────────────┐
│  PrevFrameViewInfo.LightingChannelsTexture 仍然存活           │
│  RDG 瞬态资源分配器可以复用底层显存 → 无 Cache Miss           │
│                                                              │
│  CopyStencilToLightingChannelTexture()                       │
│  → GraphBuilder.CreateTexture() 创建新纹理                    │
│    (RDG 内部复用上一帧持久化的底层资源)                        │
│                                                              │
│  if (GLightChannelExtractStatic == true)                     │
│  → QueueTextureExtraction() 更新引用                          │
│    (旧引用释放，新引用建立)                                    │
└──────────────────────────────────────────────────────────────┘
```

## 六、对比参考：CVarSceneDepthZExtractStatic

本次实现完全仿照已有的 `CVarSceneDepthZExtractStatic` 模式：

| 对比项 | CVarSceneDepthZExtractStatic | CVarLightingChannelExtractStatic |
|---|---|---|
| 控制台变量 | `r.SceneDepthZExtractStatic` | `r.LightingChannelExtractStatic` |
| 全局变量 | `GSceneDepthZExtractStatic` | `GLightChannelExtractStatic` |
| 目标纹理 | `SceneTextures.Depth.Resolve` | `LightingChannelsTexture` |
| 存储字段 | `PrevFrameViewInfo.DepthBuffer` | `PrevFrameViewInfo.LightingChannelsTexture` |
| 持久化方式 | `GraphBuilder.QueueTextureExtraction` | `GraphBuilder.QueueTextureExtraction` |
| 目的 | 避免深度缓冲 Transient Resource Cache Miss | 避免光照通道纹理 Transient Resource Cache Miss |

## 七、使用说明

### 开启持久化（推荐在出现 Cache Miss 时使用）

```
r.LightingChannelExtractStatic 1
```

### 关闭持久化（默认行为）

```
r.LightingChannelExtractStatic 0
```

### 注意事项

1. 开启后会额外占用一份 `LightingChannelsTexture` 大小的持久显存（每个视图一份）
2. 该变量为 `ECVF_RenderThreadSafe`，可在运行时动态切换
3. 仅在确认存在 LightingChannelsTexture 相关的 RDG Transient Resource Cache Miss 时建议开启
4. 与 `ScreenSpaceDenoise.cpp` 中的 `SetupSceneViewInfoPooledRenderTargets` 无关，该函数仅服务于降噪系统内部的资源管理
