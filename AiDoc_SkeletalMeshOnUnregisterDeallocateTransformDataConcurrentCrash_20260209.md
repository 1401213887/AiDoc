# SkeletalMesh OnUnregister DeallocateTransformData 并发崩溃修复

> 日期：2026-02-09
> 作者：DjangoZjgPku (AI 辅助)

## 背景

在项目运行过程中，出现了一个低概率但严重的崩溃，崩溃断言信息如下：

```
Title: Gravitation check failed
Expression: (Index >= 0) & (Index < ArrayNum)
Message: Array index out of bounds: 0 into an array of size 0
ModName: AssertionMacros
```

崩溃发生在 **Worker Thread** 上，调用栈如下（关键帧）：

```
UpdatePreviousRefToLocalMatrices()                    [SkeletalRender.cpp:656]
  └─ UpdateRefToLocalMatricesInner()                  [SkeletalRender.cpp:499]  ← 崩溃行：BoneVisibilityStates[ThisBoneIndex]
FDynamicSkelMeshObjectDataGPUSkin::InitDynamicSkelMeshObjectDataGPUSkin()  [SkeletalRenderGPUSkin.cpp:2532]
FSkeletalMeshObjectGPUSkin::Update()                  [SkeletalRenderGPUSkin.cpp:516]
USkinnedMeshComponent::CreateRenderState_Concurrent() [SkinnedMeshComponent.cpp:1254]
UActorComponent::RecreateRenderState_Concurrent()     [ActorComponent.cpp:2434]
UActorComponent::DoDeferredRenderUpdates_Concurrent() [ActorComponent.cpp:2561]
UWorld::FEndOfFrameUpdateContext::UpdateComponent_Parallel()  [LevelTick.cpp:1134]
ParallelForInternal<SendAllEndOfFrameUpdatesInternal> [ParallelFor.h:351]
```

崩溃的核心特征：
- 发生在 `ParallelFor` 的 Worker Thread 上，而非 Game Thread
- 访问 `BoneVisibilityStates` 数组时，数组大小为 0
- 涉及 Leader-Follower 组件间的 Previous 帧骨骼数据读取

## 分析过程

### 1. 崩溃点定位

崩溃发生在 `UpdateRefToLocalMatricesInner` 函数内部（`SkeletalRender.cpp:499`），具体是访问 `BoneVisibilityStates[ThisBoneIndex]` 时，`BoneVisibilityStates` 数组为空（size 0）。

这里的 `BoneVisibilityStates` 来自 **Leader 组件的 Previous 帧数据**：

```cpp
const TArray<uint8>& BoneVisibilityStates = (bIsLeaderCompValid) 
    ? LeaderComp->GetPreviousBoneVisibilityStates() 
    : InMeshComponent->GetPreviousBoneVisibilityStates();
```

### 2. 帧时序分析

项目配置 `GAsyncRenderThreadUpdateBeginPosition = 2`，这意味着异步渲染更新（`StartAsyncSendAllEndOfFrameUpdates`）在以下位置启动：

```
UWorld::Tick 帧时序：
  ├── TG_PrePhysics
  ├── TG_StartPhysics
  ├── TG_DuringPhysics
  ├── TG_EndPhysics
  ├── TG_PostPhysics
  ├── ★ StartAsyncSendAllEndOfFrameUpdates()  ← GAsyncRenderThreadUpdateBeginPosition=2 时在此启动
  ├── TG_PostUpdateWork
  ├── TG_LastDemotable                       ← 低优先级组件 Tick 可在此触发 OnUnregister
  ├── FinishAsyncSendAllEndOfFrameUpdates()  ← 在 GameViewport->Tick 之前等待完成
  └── GameViewport->Tick()
```

关键并发窗口：`StartAsync` 到 `FinishAsync` 之间，Worker Thread 在 ParallelFor 中处理组件的渲染更新，而 Game Thread 继续执行 `TG_LastDemotable` 等 Tick Group。

### 3. 根因定位

**`USkinnedMeshComponent::OnUnregister()`** 中直接调用 `DeallocateTransformData()` 而**没有等待异步更新完成**。

`DeallocateTransformData()` 会依次清空多个骨骼数据数组：

```cpp
void USkinnedMeshComponent::DeallocateTransformData()
{
    // 以下操作并非原子性的，顺序执行
    ComponentSpaceTransformsArray[0].Empty();
    ComponentSpaceTransformsArray[1].Empty();
    // ...
    BoneVisibilityStates.Empty();        // ← 这个最后清空
    PreviousBoneVisibilityStates.Empty();
    // ...
}
```

**竞态条件时序**：

| 时刻 | Game Thread | Worker Thread |
|------|-------------|---------------|
| T1 | `TG_LastDemotable` 触发 Leader 组件 `OnUnregister` | ParallelFor 处理 Follower 组件 |
| T2 | `DeallocateTransformData` 开始清空 `ComponentSpaceTransformsArray` | Follower 读取 Leader 的 `ComponentTransform` → 仍有数据（尚未被清空或已部分清空） |
| T3 | `DeallocateTransformData` 清空 `BoneVisibilityStates` | Follower 访问 `BoneVisibilityStates[ThisBoneIndex]` → **数组已为空，崩溃！** |

由于 `Empty()` 操作不是原子的，且多个数组的清空有先后顺序，Worker Thread 可能观察到**不一致状态**：`ComponentTransform` 仍有数据（或大小不为 0），但 `BoneVisibilityStates` 已经被清空。

### 4. 为什么其他路径安全而 OnUnregister 不安全

- `RecreateRenderState_Concurrent` 路径：有 `IsRenderStateUpdating()` 保护，会先调用 `FinishAsyncSendAllEndOfFrameUpdates` 等待完成
- `SetSkeletalMesh` 路径：通过 `FRenderStateRecreator` RAII 保证先销毁再重建
- **`OnUnregister` 路径**：直接调用 `DeallocateTransformData()`，**没有任何异步等待保护**

## 解决方案

### 采纳方案：OnUnregister 异步等待保护（层面 1 — 根因修复）

在 `USkinnedMeshComponent::OnUnregister()` 中，在调用 `DeallocateTransformData()` 之前，先等待所有异步的 End-of-Frame 更新完成。这样确保不会在 Worker Thread 还在读取骨骼数据时清空数组。

**修复原理**：`FinishAsyncSendAllEndOfFrameUpdates()` 会阻塞 Game Thread 直到所有 ParallelFor Worker Thread 完成当前批次的组件渲染更新。等待完成后再执行 `DeallocateTransformData()`，此时不存在并发读取者，操作安全。

**性能影响**：仅在组件 Unregister 时触发一次同步等待。由于 `OnUnregister` 本身就是一个相对低频的操作（组件销毁/移除），且 `FinishAsync` 仅在异步更新确实在运行时才会阻塞，正常帧下不会有额外开销。

## 关键代码变更

### 文件 1：`SkinnedMeshComponent.cpp` — OnUnregister 保护（层面 1，已采纳）

```cpp
void USkinnedMeshComponent::OnUnregister()
{
    // ++[wenlinda]
    if (bForceUpdateLODWhileTickComponentIsDisable && !IsNetMode(NM_DedicatedServer))
    {
        auto DeferUpdate = UDeferredUpdateSubsystem::Get(GetWorld());
        if (DeferUpdate != nullptr)
        {
            DeferUpdate->UnRegisterUpdateLODComponent(*this);
        }
        bForceUpdateLODWhileTickComponentIsDisable = false;
    }
    // --[wenlinda]

#pragma region Engine ZXB
    // OnUnregister 会调用 DeallocateTransformData 清空骨骼数组。
    // 如果此时有异步 EndOfFrameUpdate 正在运行（ParallelFor），
    // 其中的 Follower 组件可能正在 Worker Thread 上读取本组件（作为 Leader）的骨骼数据，
    // 非原子的 Empty() 操作会导致读线程观察到不一致状态（ComponentTransform 有数据但 BoneVisibilityStates 为空），
    // 从而崩溃："Array index out of bounds: 0 into an array of size 0"
    // 因此必须先等待所有异步更新完成，再清空数组。
    if (UWorld* World = GetWorld())
    {
        World->FinishAsyncSendAllEndOfFrameUpdates();
    }
#pragma endregion

    DeallocateTransformData();
    Super::OnUnregister();
    // ...
}
```

### 文件 2：`SkeletalRender.cpp` — 安全网检查（辅助防御层，同步实施）

在 `UpdateRefToLocalMatricesInner` 入口处增加数组空检查，以及在循环内增加索引越界保护：

```cpp
// UpdateRefToLocalMatricesInner 入口
#pragma region Engine ZXB
    // 保存数组大小，防止在并发访问时数组被置空
    if (ReferenceToLocal.Num() == 0)
    {
        return;
    }
#pragma endregion

// 循环体内
#pragma region Engine ZXB
    // 再次检查数组大小，防止在循环过程中数组被其他线程修改
    if (ThisBoneIndex >= ReferenceToLocal.Num())
    {
        continue;
    }
#pragma endregion
```

## 总结

### 问题本质

这是一个典型的 **Game Thread 生命周期操作 vs Worker Thread 并发读取** 的竞态条件问题。UE5 的异步 End-of-Frame 更新机制（ParallelFor）在大多数路径上都有正确的同步保护，但 `OnUnregister` → `DeallocateTransformData` 这条路径缺少了保护，导致 Leader 组件在被注销时可能正好有 Follower 组件在 Worker Thread 上读取其骨骼数据。

### 经验教训

1. **生命周期函数中的并发意识**：`OnRegister`/`OnUnregister` 等生命周期函数可能在异步渲染更新窗口内被调用，必须检查是否需要同步等待
2. **非原子多数组操作的风险**：`DeallocateTransformData` 顺序清空多个相关联的数组，中间状态可被并发线程观察到，这是一种常见的并发隐患模式
3. **Leader-Follower 模式的隐式依赖**：Follower 组件通过引用直接读取 Leader 的数据数组，Leader 的销毁会直接影响 Follower 的数据安全性
4. **防御性编程**：即使修复了根因（层面 1），在数据读取侧增加安全网检查（层面 2）仍然有价值，可以将潜在的崩溃降级为无害的 skip/early-return
5. **`GAsyncRenderThreadUpdateBeginPosition` 的影响**：该配置值越小，异步窗口越大，并发竞态的概率越高。值为 2 时窗口覆盖了 `TG_LastDemotable`，使得该 Tick Group 中的组件注销操作处于危险区
