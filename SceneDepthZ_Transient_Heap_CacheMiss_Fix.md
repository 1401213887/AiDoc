
# SceneDepthZ Transient Heap Cache Miss 问题分析与修复

## 1. 问题背景

在 UE5 RDG（Render Dependency Graph）渲染管线中，通过 RDG Resource 可视化调试界面观察到：**SceneDepthZ 资源的有无会导致 RDG Resource Heap Cache 大面积 Miss**，表现为 `AllocateTransientResources` 耗时从正常帧的 ~719 μs 飙升到异常帧的 ~12.3 ms，伴随大量密集的 `CreatePlacedResource` 和 `AllocatePlaced` 调用。

## 2. 关键概念

### 2.1 Extracted 资源

在 RDG 中，**Extracted** 表示资源在 RDG 图执行完成后不会被自动释放，而是被"提取"出来保存到外部指针中，供后续帧或 RDG 执行范围之外使用。

- 定义位置：`RenderGraphResources.h` → `FRDGViewableResource::bExtracted`
- 设置入口：`FRDGBuilder::QueueTextureExtraction()`
- Extracted 资源是 **Cull Root**，不会被 RDG 的 Pass 裁剪优化掉

### 2.2 Transient Heap

Transient Heap 是 RDG 用于临时资源的内存池，采用 first-fit 分配策略。资源在 Heap 上按偏移量放置，并通过 `Hash = ComputeHash(CreateInfo, HeapOffset)` 进行缓存匹配。

### 2.3 bForceNonTransient

`FRDGViewableResource` 的 protected 成员，用于强制标记资源不使用 Transient 分配。

### 2.4 FastVRAM

带有 `TexCreate_FastVRAM` 标志的资源，在 `IsTransientInternal` 中会绕过所有 Extracted / TransientExtractionHint 的检查，无条件被标记为 Transient。

## 3. 根因分析

### 3.1 因果链

```
SceneDepthZ 被 QueueTextureExtraction 标记为 Extracted
    ↓
QueueTextureExtraction 未传 AllowTransient → TransientExtractionHint = Disable
    ↓
但 SceneDepthZ 带有 FastVRAM 标志（FASTVRAM_CVAR(SceneDepth, 1) 默认开启）
    ↓
IsTransientInternal 中 FastVRAM 分支绕过所有检查 → 仍被标记为 Transient
    ↓
SceneDepthZ 被分配到 Transient Heap 上
    ↓
由于 bExtracted = true，Deallocate 阶段跳过内存释放
    ↓
SceneDepthZ 占据的 Heap 空间不会被归还给 first-fit allocator
    ↓
后续所有资源的分配 Offset 发生偏移
    ↓
Hash = ComputeHash(CreateInfo, Offset) 中 Offset 变了，Hash 全变了
    ↓
TRHITransientResourceCache::Acquire 找不到匹配的 Hash
    ↓
大面积 Cache Miss → 每个 Miss 都要 CreatePlacedResource → 巨大开销
```

### 3.2 关键代码路径

**IsTransientInternal 原始逻辑**（`RenderGraphBuilder.cpp`）：

```cpp
bool FRDGBuilder::IsTransientInternal(FRDGViewableResource* Resource, bool bFastVRAM) const
{
    // FastVRAM 资源无条件走 Transient，绕过所有检查
    if (!bFastVRAM || !FPlatformMemory::SupportsFastVRAMMemory())
    {
        // bForceNonTransient 检查在这里面 → 对 FastVRAM 资源无效！
        if (Resource->bForceNonTransient) { return false; }
        // Extracted / TransientExtractionHint 检查也在这里面 → 同样被绕过
        ...
    }
    return true;
}
```

**Heap Cache Hash 计算**（`RHICoreTransientResourceAllocator.h`）：

```cpp
inline uint64 ComputeHash(const FRHITextureCreateInfo& InCreateInfo, uint64 HeapOffset)
{
    return CityHash64WithSeed((const char*)&NewInfo, sizeof(FRHITextureCreateInfo), HeapOffset);
    // ↑ Hash 包含 HeapOffset，Offset 变化 → Hash 变化 → Cache Miss
}
```

## 4. 修复方案

采用**最小改动、精准传参**的策略，共修改 4 个文件：

### 4.1 修改 IsTransientInternal：bForceNonTransient 检查提前

**文件**：`Engine/Source/Runtime/RenderCore/Private/RenderGraphBuilder.cpp`

将 `bForceNonTransient` 检查从 FastVRAM 条件块**内部**移到**外部**，确保对所有资源（包括 FastVRAM 资源）都能生效。

```cpp
bool FRDGBuilder::IsTransientInternal(FRDGViewableResource* Resource, bool bFastVRAM) const
{
#pragma region Engine ZXB
    // bForceNonTransient should always be respected, even for FastVRAM resources.
    if (Resource->bForceNonTransient)
    {
        return false;
    }
#pragma endregion

    // FastVRAM resources are always transient regardless of extraction or other hints...
    if (!bFastVRAM || !FPlatformMemory::SupportsFastVRAMMemory())
    {
        // ... 原有逻辑不变（移除了原来在此处的 bForceNonTransient 检查）
    }
    return true;
}
```

### 4.2 新增 ERDGResourceExtractionFlags::ForceNonTransient 枚举值

**文件**：`Engine/Source/Runtime/RenderCore/Public/RenderGraphDefinitions.h`

```cpp
enum class ERDGResourceExtractionFlags : uint8
{
    None = 0,
    AllowTransient = 1,

#pragma region Engine ZXB
    // Forces the resource to be non-transient. Use this flag when the resource has FastVRAM flags
    // that would bypass TransientExtractionHint checks, causing it to still be allocated on the
    // Transient Heap and leading to Heap layout changes and Cache Misses.
    ForceNonTransient = 1 << 1,
#pragma endregion
};
```

### 4.3 在 QueueTextureExtraction 中处理 ForceNonTransient 标志

**文件**：`Engine/Source/Runtime/RenderCore/Public/RenderGraphBuilder.inl`

在 `QueueTextureExtraction` 中添加对 `ForceNonTransient` 标志的处理。由于 `bForceNonTransient` 是 `FRDGViewableResource` 的 **protected** 成员，外部代码无法直接访问，但 `FRDGBuilder` 是其 **friend** 类，可以在内部设置。

```cpp
inline void FRDGBuilder::QueueTextureExtraction(FRDGTextureRef Texture, ..., ERDGResourceExtractionFlags Flags)
{
    // ... 原有 AllowTransient / TransientExtractionHint 逻辑不变 ...

#pragma region Engine ZXB
    // 当调用方明确传递 ForceNonTransient 标志时，强制标记资源为 NonTransient，
    // 防止 FastVRAM 资源绕过 TransientExtractionHint 检查仍被分配到 Transient Heap。
    if (EnumHasAnyFlags(Flags, ERDGResourceExtractionFlags::ForceNonTransient))
    {
        Texture->bForceNonTransient = true;
    }
#pragma endregion

    ExtractedTextures.Emplace(Texture, OutTexturePtr);
    // ...
}
```

### 4.4 在 SceneDepthZ Extract 调用处传递 ForceNonTransient 标志

**文件**：`Engine/Source/Runtime/Renderer/Private/DeferredShadingRenderer.cpp`

```cpp
#pragma region Engine ZXB
// Add by ZXB for RDG Transient Resource Catch Miss
if (GSceneDepthZExtractStatic)
{
    FSceneViewState* ViewState = View.ViewState;
    // SceneDepthZ 带有 FastVRAM 标志，会绕过 TransientExtractionHint 检查仍被分配到 Transient Heap，
    // 导致 Heap 布局变化引发大面积 Cache Miss，需要通过 ForceNonTransient 标志明确标记为 NonTransient。
    GraphBuilder.QueueTextureExtraction(SceneTextures.Depth.Resolve,
        &ViewState->PrevFrameViewInfo.DepthBuffer,
        ERDGResourceExtractionFlags::ForceNonTransient);
}
#pragma endregion
```

## 5. 修改文件清单

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `RenderGraphBuilder.cpp` | 逻辑调整 | `IsTransientInternal` 中 `bForceNonTransient` 检查提到 FastVRAM 条件之前 |
| `RenderGraphDefinitions.h` | 新增枚举值 | `ERDGResourceExtractionFlags::ForceNonTransient = 1 << 1` |
| `RenderGraphBuilder.inl` | 新增逻辑 | `QueueTextureExtraction` 中处理 `ForceNonTransient` 标志 |
| `DeferredShadingRenderer.cpp` | 调用处传参 | SceneDepthZ Extract 时传递 `ForceNonTransient` 标志 |

## 6. 设计决策记录

### 6.1 为什么不在 QueueTextureExtraction 通用逻辑中设置 bForceNonTransient？

最初方案是在 `QueueTextureExtraction` 的 else 分支（不传 `AllowTransient` 时）统一设置 `bForceNonTransient = true`。但这会影响引擎中所有不传 `AllowTransient` 的 Extract 调用，改动范围过大。最终采用**明确传参**的方式，只在需要的调用处传递 `ForceNonTransient` 标志。

### 6.2 为什么不直接在外部设置 bForceNonTransient？

`bForceNonTransient` 是 `FRDGViewableResource` 的 **protected** 成员，外部代码（如 `DeferredShadingRenderer.cpp`）无法直接访问。`FRDGBuilder` 作为 `FRDGViewableResource` 的 **friend** 类，可以在其成员函数 `QueueTextureExtraction` 内部设置。因此通过新增 `ERDGResourceExtractionFlags::ForceNonTransient` 枚举值，让调用方通过传参间接控制。

### 6.3 为什么需要修改 IsTransientInternal？

即使设置了 `bForceNonTransient = true`，在原始代码中该检查位于 `if (!bFastVRAM || !FPlatformMemory::SupportsFastVRAMMemory())` 条件块**内部**，对 FastVRAM 资源无效。必须将其提到 FastVRAM 条件之前，才能确保 `bForceNonTransient` 对所有资源生效。

## 7. 编译问题修复记录

在开发过程中遇到过以下编译错误：

```
Error C2248: "FRDGViewableResource::bForceNonTransient": 无法访问 protected 成员
```

**原因**：最初尝试在 `DeferredShadingRenderer.cpp` 中直接通过 `SceneTextures.Depth.Resolve->bForceNonTransient = true` 访问 protected 成员。

**解决**：改为通过 `ERDGResourceExtractionFlags::ForceNonTransient` 枚举标志传参，由 `FRDGBuilder`（friend 类）在 `QueueTextureExtraction` 内部设置。

## 8. 预期效果

修复后，SceneDepthZ 在被 Extract 时会通过 `ForceNonTransient` 标志被标记为 `bForceNonTransient = true`。`IsTransientInternal` 在最前面检查到该标志后直接返回 `false`，使 SceneDepthZ 不再走 Transient Heap 分配路径。这样：

1. SceneDepthZ 不再占据 Transient Heap 空间
2. Heap 布局保持稳定，不会因 SceneDepthZ 的有无而变化
3. 后续资源的 Heap Offset 不变，Hash 不变
4. `TRHITransientResourceCache::Acquire` 能正常命中缓存
5. 消除大面积 Cache Miss 导致的 `CreatePlacedResource` 开销
