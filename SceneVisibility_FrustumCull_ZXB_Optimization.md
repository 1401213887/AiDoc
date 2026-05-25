# SceneVisibility_FrustumCull 优化改动总结（ZXB）

> 文档对应 P4 stream：`//GR/MergeTest`
> 涉及 client：`DJANGOZHAN-PCFW_GR_MergeTest`（root: `D:\GR_MergeTest`）
> 所有改动均使用 `#pragma region Engine ZXB` / `#pragma endregion` 包裹，方便后续 diff / 整理。

---

## 1. 背景与目标

`SceneVisibility_FrustumCull`（即 [SceneVisibility.cpp](./SceneVisibility.cpp) 中的 `FrustumCull(...)` 主循环以及紧随其后的 `GPVSSkipNanitePrimitives` 后处理段）在 `NumTestedPrimitives` 大的场景下成为 CPU 端可见性阶段的热点，profile 反馈耗时突出。

通过代码分析定位到以下三类共性问题：

1. **每个可见 primitive 都触发 `FPrimitiveSceneProxy*` 解引用**，跳跃随机堆地址 → cache line miss。
2. **热路径上调用 `FMath::Sin` / `FVector::Normalize` / `FVector::Dist`** 等高成本数学操作（transcendental / sqrt）。
3. **CVar / 虚函数调用未提前缓存或预烘**，每个 primitive 都重复支付。

针对上述问题，本次优化采用了三种通用手段：

- **SoA 预烘**：将原本依赖 Proxy 的标量信息（`sin(angle)`、`bIsNaniteMesh`）烘到与 `PrimitiveBounds` 索引同步的并行数组里，主循环按下标顺序访问，prefetcher 友好。
- **代数变形**：用平方比较替代 sqrt，移除全部不必要的 `Normalize` / `Dist`。
- **入口缓存**：把全局 CVar 在 task 入口取一次，写到 `FFrustumCullingFlags`，循环里复用。

---

## 2. 改动一览

| # | 文件 | 位置 | 类型 | 简述 |
|---|---|---|---|---|
| C1 | [ScenePrivate.h](./ScenePrivate.h) | `FScene` 字段段（PrimitiveBounds 之后） | 数据结构 | 新增 `TArray<float> PrimitiveAxisZConeCullingSinThreshold` SoA 数组 |
| C2 | [PrimitiveSceneInfo.cpp](./PrimitiveSceneInfo.cpp) | `UpdatePrimitiveSceneInfo`，紧跟 `PrimitiveBounds` 设置 | 数据生命周期 | 烘焙 `sin(AxisZConeCullingAngle)` 写入 SoA 数组 |
| C3 | [RendererScene.cpp](./RendererScene.cpp) | `CheckPrimitiveArrays` / `Remove` / `Reserve` / `Add` 四处 | 数据生命周期 | 与 `PrimitiveBounds` 同生命周期维护 SoA 数组 |
| C4 | [SceneVisibility.cpp](./SceneVisibility.cpp) | `FFrustumCullingFlags` 结构体 | 入口缓存 | 增加 `bUseAxisZConeCulling` 字段 |
| C5 | [SceneVisibility.cpp](./SceneVisibility.cpp) | `LaunchVisibilityTaskPipe` 内 Flags 初始化处 | 入口缓存 | 入口取一次 `IsAxisZConeCullingEnabled()` |
| C6 | [SceneVisibility.cpp](./SceneVisibility.cpp) | `FrustumCull(...)` 主循环 ConeCulling 段 | 主循环重写 | 移除 Proxy 解引用 / Sin / Normalize / 双 sqrt，使用 SoA 数组 + 平方代数 |
| C7 | [SceneVisibility.cpp](./SceneVisibility.cpp) | `LaunchVisibilityTaskPipe` 中 threaded 分支 `GPVSSkipNanitePrimitives` 段 | 主循环重写 | 用 `PrimitiveFlagsCompact[Index].bIsNaniteMesh` SoA 短路替代虚函数 `IsNaniteMesh()` |
| C8 | [SceneVisibility.cpp](./SceneVisibility.cpp) | 单线程兜底分支 `GPVSSkipNanitePrimitives` 段 | 主循环重写 | 同 C7 |

---

## 3. 改动详情

### C1 · `ScenePrivate.h` — 新增 SoA 数组

声明位置紧邻 `PrimitiveBounds`，便于在 `RendererScene.cpp` 中同生命周期维护，亦使 cache line 局部性最大化。

```cpp
/** Packed array of primitive bounds. */
TScenePrimitiveArray<FPrimitiveBounds> PrimitiveBounds;
#pragma region Engine ZXB
/**
 * Packed array of pre-baked sin(AxisZConeCullingAngle) values, parallel to PrimitiveBounds/Primitives.
 * 0.0f means the primitive does NOT participate in AxisZ cone culling (fast path).
 * Maintained synchronously with PrimitiveBounds in RendererScene.cpp / PrimitiveSceneInfo.cpp.
 * Avoids per-primitive FPrimitiveSceneProxy dereference + FMath::Sin call inside FrustumCull hot loop.
 */
TArray<float> PrimitiveAxisZConeCullingSinThreshold;
#pragma endregion
/** Packed array of primitive flags. */
TArray<FPrimitiveFlagsCompact> PrimitiveFlagsCompact;
```

### C2 · `PrimitiveSceneInfo.cpp` — 烘焙 sin(angle)

在 `UpdatePrimitiveSceneInfo` 中、`PrimitiveBounds` 写完之后插入烘焙逻辑。`IsNearlyZero(angle) → 0.0f` 作为"该 primitive 不参与 cone culling"的 sentinel，使主循环可以一行 `if (SinThreshold > 0.0f)` 极廉价短路。

```cpp
PrimitiveBounds.MaxCullDistance = PrimitiveBounds.MaxDrawDistance;

#pragma region Engine ZXB
// Bake sin(AxisZConeCullingAngle) into a parallel array so FrustumCull hot loop
// can avoid dereferencing FPrimitiveSceneProxy and calling FMath::Sin per primitive.
// 0.0f means "no cone culling" (fast skip in the hot loop).
{
    const float ConeAngleDeg = Proxy->GetAxisZConeCullingAngle();
    const float SinThreshold = FMath::IsNearlyZero(ConeAngleDeg)
        ? 0.0f
        : FMath::Sin(FMath::DegreesToRadians(ConeAngleDeg));
    Scene->PrimitiveAxisZConeCullingSinThreshold[PackedIndex] = SinThreshold;
}
#pragma endregion

Scene->PrimitiveFlagsCompact[PackedIndex] = FPrimitiveFlagsCompact(Proxy);
```

### C3 · `RendererScene.cpp` — 同生命周期维护

四处与 `PrimitiveBounds` 配套维护：

- **`CheckPrimitiveArrays`**（约 1482）：增加一致性 `check`。
- **`Remove`**（约 6379）：与 `PrimitiveBounds.Remove` 同批 `RemoveAt(SourceIndex, RemoveCount, EAllowShrinking::No)`。
- **`Reserve`**（约 6578）：与 `PrimitiveBounds.Reserve` 同步。
- **`Add`**（约 6621）：每次新加一个 primitive 默认 `Add(0.0f)`（不参与 cone culling 的安全态），随后由 `UpdatePrimitiveSceneInfo` 写入真实值。

### C4 · `SceneVisibility.cpp` — `FFrustumCullingFlags` 增加字段

```cpp
struct FFrustumCullingFlags
{
    bool bShouldVisibilityCull;
    bool bUseCustomCulling;
    bool bUseSphereTestFirst;
    bool bUseFastIntersect;
    bool bUseVisibilityOctree;
    bool bHasHiddenPrimitives;
    bool bHasShowOnlyPrimitives;
#pragma region Engine ZXB
    // Pre-fetched once at FrustumCull entry to avoid repeatedly reading the CVar inside the hot loop.
    bool bUseAxisZConeCulling;
#pragma endregion
};
```

### C5 · `SceneVisibility.cpp` — 入口缓存 CVar

```cpp
Flags.bHasShowOnlyPrimitives = View.ShowOnlyPrimitives.IsSet();
#pragma region Engine ZXB
// Cache CVar once per visibility task setup; avoids repeatedly reading GEnableAxisZConeCulling
// inside the per-primitive FrustumCull hot loop.
Flags.bUseAxisZConeCulling   = IsAxisZConeCullingEnabled();
#pragma endregion
```

### C6 · `SceneVisibility.cpp` — 主循环 AxisZ ConeCulling 重写（**核心**）

#### 6.1 原实现的问题

```cpp
// 改前
if (Scene.PrimitiveSceneProxies.IsValidIndex(Index))                      // 多余的边界检查
{
    FPrimitiveSceneProxy* Proxy = Scene.PrimitiveSceneProxies[Index];     // ← cache miss
    if (Proxy != nullptr
        && IsAxisZConeCullingEnabled()                                    // ← per-primitive CVar 读
        && !FMath::IsNearlyZero(Proxy->GetAxisZConeCullingAngle())        // ← per-primitive 虚函数（间接走 Proxy）
        && bIsVisible)                                                    // ← 短路放在最后，前面已支付的开销作废
    {
        FVector PrimitiveToView = ViewOriginForDistanceCulling - Bounds.BoxSphereBounds.Origin;
        PrimitiveToView.Normalize();                                      // ← sqrt + div
        float Distance = FVector::Dist(...);                              // ← 第二次 sqrt
        if (Distance > Bounds.BoxSphereBounds.SphereRadius * 1.5)
        {
            float AngleInRadians = FMath::DegreesToRadians(Proxy->GetAxisZConeCullingAngle());
            bIsVisible = PrimitiveToView.Z > FMath::Sin(AngleInRadians);  // ← per-primitive Sin
        }
    }
}
```

#### 6.2 新实现

```cpp
// [AxisZ ConeCulling] -- ADD BEGIN @Beiyu @CYH
#pragma region Engine ZXB
// Optimized AxisZ cone culling.
//
// Original implementation dereferenced FPrimitiveSceneProxy* per primitive and called
// FMath::Sin / FVector::Normalize / FVector::Dist on every visible candidate, which:
//   - issued a random-pointer cache miss on PrimitiveSceneProxies[Index]
//   - ran transcendental Sin in the hot loop (~15-25 cycles)
//   - did sqrt twice (Normalize + Dist)
//
// New implementation:
//   - sin(angle) is pre-baked into Scene.PrimitiveAxisZConeCullingSinThreshold (parallel
//     to PrimitiveBounds, contiguous SoA layout, prefetcher-friendly).
//   - 0.0f sentinel skips the whole branch (most primitives in the scene).
//   - bIsVisible short-circuits the test cheaply at the very front.
//   - the original geometric test
//         (V - C).GetSafeNormal().Z > sin(angle)   (only when |V-C| > 1.5*Radius)
//     is rewritten as the equivalent square-form, eliminating both sqrt ops:
//         DiffZ > 0  &&  DiffZ * DiffZ > sin^2(angle) * LenSq
//     with the radius check expressed as
//         LenSq > (1.5 * Radius)^2
//
// Note: angle is in [0, 90] -> sin(angle) >= 0, so squaring it is value-preserving.
if (bIsVisible && Flags.bUseAxisZConeCulling)
{
    const float SinThreshold = Scene.PrimitiveAxisZConeCullingSinThreshold[Index];
    if (SinThreshold > 0.0f)
    {
        const FVector::FReal DX    = ViewOriginForDistanceCulling.X - Bounds.BoxSphereBounds.Origin.X;
        const FVector::FReal DY    = ViewOriginForDistanceCulling.Y - Bounds.BoxSphereBounds.Origin.Y;
        const FVector::FReal DiffZ = ViewOriginForDistanceCulling.Z - Bounds.BoxSphereBounds.Origin.Z;
        const FVector::FReal LenSq = DX * DX + DY * DY + DiffZ * DiffZ;

        const FVector::FReal RadiusScaled = Bounds.BoxSphereBounds.SphereRadius * 1.5;
        const FVector::FReal MinDistSq    = RadiusScaled * RadiusScaled;

        if (LenSq > MinDistSq)
        {
            // Equivalent to (DiffZ / sqrt(LenSq)) > SinThreshold, but without sqrt.
            // DiffZ <= 0 implies the view is at-or-below the primitive -> always cull.
            const FVector::FReal SinSq = FVector::FReal(SinThreshold) * FVector::FReal(SinThreshold);
            bIsVisible = (DiffZ > 0.0) && (DiffZ * DiffZ > SinSq * LenSq);
        }
    }
}
#pragma endregion
// [Axis ConeCulling] -- ADD END
```

#### 6.3 数学等价性证明（从原条件到平方形式）

原条件（仅在 `Distance > 1.5 * Radius` 域内启用）：

```
PrimitiveToView.Z > sin(angle)
  其中 PrimitiveToView = (V - C) / |V - C|
```

由于 `|V - C| > 0`，两边乘以 `|V - C|`：

```
DiffZ > sin(angle) * |V - C|
  其中 DiffZ = V.z - C.z
```

注意 `sin(angle) ∈ [0, 1]`（angle ∈ [0°, 90°]），右侧非负。
- 必要条件：`DiffZ > 0`（否则左侧非正、右侧非负，恒为 false）。
- 在 `DiffZ > 0` 前提下两侧均非负，可直接平方：

```
DiffZ² > sin²(angle) * |V - C|²
  即 DiffZ² > SinSq * LenSq
```

距离阈值同步平方：

```
|V - C| > 1.5 * Radius   ⇔   LenSq > (1.5 * Radius)²
```

→ 原命题与平方命题在合法定义域内严格等价，**不引入数值偏差**。

### C7 · `SceneVisibility.cpp` — Threaded 路径 Nanite 跳过

```cpp
// [PVS] Add by @Linsan
if (GPVSSkipNanitePrimitives)
{
#pragma region Engine ZXB
    // Optimized: short-circuit on FPrimitiveFlagsCompact::bIsNaniteMesh which is a tiny SoA byte
    // array (Scene.PrimitiveFlagsCompact) sequentially laid out and prefetcher-friendly. Only the
    // rare Nanite primitives need the proxy dereference for the IsSelected() check.
    const FPrimitiveFlagsCompact* RESTRICT FlagsCompactPtr = Scene.PrimitiveFlagsCompact.GetData();
    FPrimitiveSceneProxy* const* RESTRICT SceneProxiesPtr = Scene.PrimitiveSceneProxies.GetData();
    for (FSceneSetBitIterator BitIt(View.PrimitiveVisibilityMap, PrimitiveRange.StartIndex);
         BitIt.GetIndex() < PrimitiveRange.EndIndex; ++BitIt)
    {
        const int32 Index = BitIt.GetIndex();
        if (!FlagsCompactPtr[Index].bIsNaniteMesh)
        {
            continue;
        }
        if (!SceneProxiesPtr[Index]->IsSelected())
        {
            View.PrimitiveVisibilityMap.AccessCorrespondingBit(BitIt) = false;
        }
    }
#pragma endregion
}
// [PVS] End
```

关键点：
- `bIsNaniteMesh` 是 `FPrimitiveFlagsCompact` 已有位（`PrimitiveSceneInfo.h:190`），构造时 `bIsNaniteMesh(Proxy->IsNaniteMesh())` 与原虚调严格等价。
- 非 Nanite primitive（场景大头）**1 字节短路**返回，省掉 Proxy 解引用与虚调。
- 仅极少数 Nanite primitive 才走 `IsSelected()` 这一次解引用。

### C8 · `SceneVisibility.cpp` — 单线程兜底路径 Nanite 跳过

```cpp
// [PVS] Add by @Linsan
if (GPVSSkipNanitePrimitives)
{
#pragma region Engine ZXB
    // Same SoA short-circuit as the threaded path above: avoid per-primitive virtual call to
    // IsNaniteMesh() by reading the precomputed bit from FPrimitiveFlagsCompact.
    const FPrimitiveFlagsCompact* RESTRICT FlagsCompactPtr = Scene.PrimitiveFlagsCompact.GetData();
    for (FSceneSetBitIterator BitIt(View.PrimitiveVisibilityMap, 0);
         BitIt.GetIndex() < int32(TaskConfig.NumTestedPrimitives); ++BitIt)
    {
        if (FlagsCompactPtr[BitIt.GetIndex()].bIsNaniteMesh)
        {
            View.PrimitiveVisibilityMap.AccessCorrespondingBit(BitIt) = false;
        }
    }
#pragma endregion
}
// [PVS] End
```

---

## 4. 性能模型（per primitive）

| 项 | 改前 | 改后 | 单 primitive 节省（粗算） |
|---|---|---|---|
| Proxy 指针解引用 | 1+ 次（potential cache miss） | 0 次（cone path）/ ≤1 次（Nanite 选中检查） | ~50–200 cycle（视 cache 命中） |
| `IsValidIndex` | 1 次 | 0 次 | 1–2 cycle |
| CVar 读取（`GEnableAxisZConeCulling`） | 1 次 | task 入口 1 次（摊销 → 0） | 数 cycle |
| `FMath::Sin` | 1 次 | 0 次（一次性烘焙） | ~15–25 cycle |
| `FMath::DegreesToRadians` | 1 次 | 0 次 | 1–2 cycle |
| `FVector::Normalize`（sqrt+div） | 1 次 | 0 次 | ~25–35 cycle |
| `FVector::Dist`（sqrt） | 1 次 | 0 次 | ~20–30 cycle |
| `bIsVisible` 短路位置 | 第 4 个 `&&` 之后 | 第 1 个 `&&` | 大量预剔除 primitive 直接跳过 |
| Nanite 跳过段 | per primitive 至少 1 次虚调 + 1 次 Proxy 解引用 | 1 字节 SoA 顺序读，命中后才 1 次 Proxy 解引用 | 非 Nanite primitive 节省 50–200 cycle |
| 数据访问模式 | Proxy 数组（指针）→ 各自堆对象（随机） | 与 `PrimitiveBounds[Index]` 紧邻的连续 SoA 数组 | prefetcher 友好，抖动小 |

按 `NumTestedPrimitives` 万级粗估，AxisZ ConeCulling 段 + Nanite 跳过段合计预期下降幅度大致 **60%–80%**，具体看 Nanite 占比与场景中 cone-culling-enabled mesh 的比例。

---

## 5. 行为兼容性

| 场景 | 行为 |
|---|---|
| `r.EnableAxisZConeCulling` 开关 | 完全保留，仅从循环里搬到 task 入口 |
| `UStaticMesh::AxisZConeCullingAngle` 属性运行时变更 | `UpdatePrimitiveSceneInfo` 同步重新烘焙；行为不变 |
| `IsNearlyZero(angle)` 临界 | 烘焙时按原阈值（`KINDA_SMALL_NUMBER`）写 0；主循环用 `> 0.0f` 短路；临界 angle 表现一致 |
| `PVS.Culling.SkipNanitePrimitives` 开关 | 完全保留 |
| Nanite + Selected 在 Editor 选中 | 仍受 Editor 选中影响，逻辑一致 |
| 多 View / Stereo / SceneCapture | 每 View 独立调用 `FrustumCull`，行为一致 |
| LWC（double） | 全程 `FVector::FReal`，无精度损失 |

---

## 6. 数据生命周期不变量

`PrimitiveAxisZConeCullingSinThreshold` 与 `Primitives / PrimitiveBounds / PrimitiveSceneProxies / PrimitiveFlagsCompact` 一一对应，索引同步：

| 入口 | 维护 |
|---|---|
| `FScene::AddPrimitiveSceneInfos_RenderThread`（`Add`） | `Add(0.0f)` |
| 同函数 `Reserve` 段 | `Reserve(...)` |
| `FScene::RemovePrimitiveSceneInfo_RenderThread`（`Remove`） | `RemoveAt(SourceIndex, RemoveCount, EAllowShrinking::No)` |
| `FScene::UpdatePrimitiveSceneInfo` | 写入真实 `sin(angle)` |
| `FScene::CheckPrimitiveArrays` | `check(Primitives.Num() == PrimitiveAxisZConeCullingSinThreshold.Num())` |

> 其它会修改 `PrimitiveBounds` 但不动 angle 来源的入口（World offset 应用、MinDraw/MaxDraw 距离更新）已确认**不影响** `SinThreshold`，无需同步。

---

## 7. 风险点与注意事项

1. **新增 SoA 数组的内存开销**：每个 primitive 4 字节，10 万 primitive 量级约 400 KB，相对 `PrimitiveSceneProxies` / `PrimitiveBounds` 完全可忽略。
2. **`FPrimitiveFlagsCompact` 必须在 `PrimitiveSceneProxies` 之前/同时被填充**：当前引擎流程已保证（`PrimitiveSceneInfo.cpp` 中 `Scene->PrimitiveFlagsCompact[PackedIndex] = FPrimitiveFlagsCompact(Proxy);` 是 `UpdatePrimitiveSceneInfo` 的一部分），无需额外处理。
3. **`Scene.PrimitiveFlagsCompact[Index].bIsNaniteMesh` 与 `Proxy->IsNaniteMesh()` 的等价性**：构造函数 `FPrimitiveFlagsCompact(const FPrimitiveSceneProxy* Proxy)` 中显式 `bIsNaniteMesh(Proxy->IsNaniteMesh())`，运行时变化（极少）走 `MarkRenderStateDirty` → `UpdatePrimitiveSceneInfo` 重新填充，等价。
4. **`HISMComponent` / 实例化组件**：本优化只影响 per-component 维度的剔除（`Scene.Primitives` 维），对 instance 维度（GPUScene + InstanceCulling）无任何影响，行为完全保留。

---

## 8. 验证 / 回滚

### 8.1 验证

- **编译**：四个文件 lint 0 报错，编译通过。
- **行为对比**：
  - 关闭 CVar `r.EnableAxisZConeCulling 0` 与改前一致（同一 fast-skip）。
  - 启用 CVar 时，对 `AxisZConeCullingAngle != 0` 的 mesh 视点测试结果与原实现一致（数学等价）。
- **profile 对比**：建议在 Insights 中对比 `SceneVisibility_FrustumCull` scope 改前/改后耗时与抖动。

### 8.2 回滚

所有改动均被 `#pragma region Engine ZXB` / `#pragma endregion` 包裹，回滚策略：

```bash
# 在 D:\GR_MergeTest 工作区下
p4 -c DJANGOZHAN-PCFW_GR_MergeTest revert <文件路径>
```

涉及文件：
- [SceneVisibility.cpp](./SceneVisibility.cpp)
- [RendererScene.cpp](./RendererScene.cpp)
- [PrimitiveSceneInfo.cpp](./PrimitiveSceneInfo.cpp)
- [ScenePrivate.h](./ScenePrivate.h)

---

## 9. 后续可选优化（未在本次实施，仅记录）

| 方向 | 难度 | 预期收益 | 备注 |
|---|---|---|---|
| 给 `FPrimitiveFlagsCompact` 增加 `bIsDetailMesh` / `bIsUsingDistanceCullFade` 位 | 中 | 低 | 这两个 Proxy 调用已被廉价 EngineShowFlags / Fade 区间条件短路保护，命中率低；扩展字段会触及公共结构体 |
| 把 sphere-only fast pass（`bUseSphereTestFirst`）改为 SoA 4-wide SIMD | 中-高 | 中 | 需要把 `Origin/Radius` 拆分成单字段 SoA；与 GPUScene 路径有一定职责重叠 |
| 加 prefetch（`FPlatformMisc::Prefetch(&PrimitiveSceneProxies[Index+K])`） | 低 | 中 | 仅在仍保留 Proxy 解引用的少数路径下使用 |

---

## 10. 联系人 / 来源

- 改动作者：ZXB
- Stream：`//GR/MergeTest`
- 关联 P4 client：`DJANGOZHAN-PCFW_GR_MergeTest`
- 改动范围标记：`#pragma region Engine ZXB` / `#pragma endregion`
