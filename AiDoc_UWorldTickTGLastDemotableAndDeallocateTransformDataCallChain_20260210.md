# UWorld::Tick 中 TG_LastDemotable 阶段分析与 DeallocateTransformData 调用链

> 日期：2026-02-10
> 作者：DjangoZjgPku (AI 辅助)

## 背景

在分析 UE5 引擎的帧 Tick 流程时，需要深入理解 `UWorld::Tick()` 中 `TG_LastDemotable` tick group 的具体工作内容，以及骨骼数据清理函数 `DeallocateTransformData` 的完整调用链，以便理解其与异步 `EndOfFrameUpdate` 之间的并发竞态关系。

## 分析过程

### 1. TG_LastDemotable 的定义

在 `EngineBaseTypes.h` 中，`TG_LastDemotable` 被定义为：

```cpp
/** Catchall for anything demoted to the end. */
TG_LastDemotable UMETA(Hidden, DisplayName = "Last Demotable"),
```

它是最后一个"真实"的 tick group（`Hidden` 表示不可被用户手动设置），用于兜底所有因依赖关系被降级的 tick function。

### 2. 完整 Tick Group 执行顺序

| 顺序 | Tick Group | 说明 |
|------|-----------|------|
| 1 | `TG_PrePhysics` | 物理模拟前 |
| 2 | `TG_StartPhysics` | 启动物理模拟（不可降级目标） |
| 3 | `TG_DuringPhysics` | 物理模拟期间并行 |
| 4 | `TG_EndPhysics` | 结束物理模拟（不可降级目标） |
| 5 | `TG_PostPhysics` | 物理模拟后 |
| 6 | `TG_PostUpdateWork` | 后更新工作 |
| **7** | **`TG_LastDemotable`** | **最终兜底，所有被降级的 tick** |
| 8 | `TG_NewlySpawned` | 特殊组，非真实 tick group |

### 3. TG_LastDemotable 在 UWorld::Tick 中的位置

`TG_LastDemotable` 在以下所有工作之后执行：

- 所有标准 tick group（PrePhysics → Physics → PostPhysics → PostUpdateWork）
- Latent Actions 处理
- TimerManager Tick
- `FTickableGameObject::TickObjects`
- 摄像机更新（`UpdateCameraManager`）
- 流式加载状态更新（`InternalUpdateStreamingState`）
- `StartAsyncSendAllEndOfFrameUpdates()`（可选，取决于 `GAsyncRenderThreadUpdateBeginPosition` 的值）

并且在 `FTickTaskManagerInterface::Get().EndFrame()` 之前，是帧内最后执行的 tick group。

关键代码（`LevelTick.cpp`）：

```cpp
if (bDoingActorTicks)
{
    SCOPE_CYCLE_COUNTER(STAT_TickTime);
    {
        // TG_PostUpdateWork ...
        RunTickGroup(TG_PostUpdateWork);
    }

    // (可选) StartAsyncSendAllEndOfFrameUpdates (Position == 2)

    {
        SCOPE_CYCLE_COUNTER(STAT_TG_LastDemotable);
        SCOPE_TIME_GUARD_MS(TEXT("UWorld::Tick - TG_LastDemotable"), 5);
        CSV_SCOPED_SET_WAIT_STAT(LastDemotable);
        RunTickGroup(TG_LastDemotable);
    }

    FTickTaskManagerInterface::Get().EndFrame();
}
```

### 4. Tick Function 降级（Demotion）机制

当一个 tick function 的 prerequisite（前置依赖）在比自身更晚的 tick group 中时，该 tick function 会被"降级"到更晚的组。核心逻辑在 `TickTaskManager.cpp`：

```cpp
// tick group is the max of the prerequisites, the current tick group, and the desired tick group
ETickingGroup MyActualTickGroup = FMath::Max<ETickingGroup>(
    MaxPrerequisiteTickGroup,
    FMath::Max<ETickingGroup>(TickGroup.GetValue(), TickContext.TickGroup));

if (MyActualTickGroup != TickGroup)
{
    // if the tick was "demoted", make sure it ends up in an ordinary tick group.
    while (!CanDemoteIntoTickGroup(MyActualTickGroup))
    {
        MyActualTickGroup = ETickingGroup(MyActualTickGroup + 1);
    }
}
```

`CanDemoteIntoTickGroup` 函数规定 `TG_StartPhysics` 和 `TG_EndPhysics` 不能作为降级目标：

```cpp
FORCEINLINE bool CanDemoteIntoTickGroup(ETickingGroup TickGroup)
{
    switch (TickGroup)
    {
        case TG_StartPhysics:
        case TG_EndPhysics:
            return false;
    }
    return true;
}
```

### 5. TG_LastDemotable 中执行的工作

- **被降级的 tick function**：因 prerequisite 依赖链推迟到最后才能执行的 tick function
- **粒子系统**（`ParticleSystemManager.cpp`）：prerequisite 组件在较晚的 tick group 完成时，粒子 tick 被推迟到此。在 `TG_LastDemotable` 中所有被延迟的粒子必须全部完成，不能再继续推迟
- **聚合 Tick 系统边界**：`MAX_ALLOW_TICKING_GROUP` 设为 `TG_LastDemotable`
- **SkinnedMesh 骨骼数据回收**：Leader 的 Previous 骨骼变换数据可能在此阶段被 `DeallocateTransformData` 清空

## DeallocateTransformData 完整调用链

### 函数定义

**USkinnedMeshComponent 版本**（`SkinnedMeshComponent.cpp:2825`）：

```cpp
void USkinnedMeshComponent::DeallocateTransformData()
{
    ComponentSpaceTransformsArray[0].Empty();
    ComponentSpaceTransformsArray[1].Empty();
    PreviousComponentSpaceTransformsArray.Empty();
    BoneVisibilityStates[0].Empty();
    BoneVisibilityStates[1].Empty();
    PreviousBoneVisibilityStates.Empty();
}
```

**USkeletalMeshComponent 重写版本**（`SkeletalMeshComponent.cpp:3655`）：

```cpp
void USkeletalMeshComponent::DeallocateTransformData()
{
    Super::DeallocateTransformData();
    BoneSpaceTransforms.Empty();
}
```

### 调用链一：OnUnregister 路径（最常见，并发风险最大）

```
Actor 销毁 / 组件销毁 / 关卡卸载
  │
  ├─ AActor::Destroy() → DestroyConstructedComponents()
  ├─ UActorComponent::DestroyComponent()
  │    └─ UnregisterComponent()
  │
  └─ UActorComponent::UnregisterComponent()
       └─ ExecuteUnregisterEvents()
            ├─ DestroyPhysicsState()
            ├─ DestroyRenderState_Concurrent()
            └─ OnUnregister() (virtual)
                 └─ USkinnedMeshComponent::OnUnregister()
                      ├─ [ZXB修复] World->FinishAsyncSendAllEndOfFrameUpdates()
                      ├─ DeallocateTransformData()  ← 清空骨骼数组
                      └─ Super::OnUnregister()
```

### 调用链二：AllocateTransformData 路径（Mesh 变更时）

```
SetSkinnedAssetAndUpdate(newAsset) / SetSkeletalMesh()
  └─ AllocateTransformData()
       ├─ if (asset != null && LeaderPoseComponent == null)
       │    → 正常分配骨骼数组, return true
       └─ else
            └─ DeallocateTransformData()  ← 清空数组, return false
```

### 调用链三：SetLeaderPoseComponent 路径

```
SetLeaderPoseComponent(newLeader)
  └─ AllocateTransformData()
       └─ (当 LeaderPoseComponent != nullptr 时)
            → DeallocateTransformData()
```

### 调用链四：OnRegister 中的间接调用

```
USkinnedMeshComponent::OnRegister()
  └─ AllocateTransformData()
       └─ (内部条件不满足时) → DeallocateTransformData()
```

### 并发风险时序图

```
时间线 ───────────────────────────────────────────────────────────►

GameThread:
  ┌─ StartAsyncSendAllEndOfFrameUpdates() ─────────────────────┐
  │  (位置取决于 GAsyncRenderThreadUpdateBeginPosition = 0/1/2) │
  │                                                              │
  │  ┌── TG_LastDemotable ──┐                                   │
  │  │ 某个 tick function    │                                   │
  │  │ 销毁了一个 Actor      │                                   │
  │  │ → OnUnregister()     │                                   │
  │  │ → DeallocateTransformData() ← 清空骨骼数组!              │
  │  └──────────────────────┘                                   │
  │                                                              │
WorkerThread（并发）:                                              │
  │  EndOfFrameUpdate (ParallelFor)                              │
  │  Follower 组件正在读 Leader 的骨骼数据                        │
  │  → BoneVisibilityStates 已被 Empty()                         │
  │  → "Array index out of bounds: 0 into size 0" 崩溃           │
  └──────────────────────────────────────────────────────────────┘
```

## 解决方案

在 `USkinnedMeshComponent::OnUnregister()` 中，在调用 `DeallocateTransformData()` 之前，先等待所有异步 `EndOfFrameUpdate` 完成：

```cpp
#pragma region Engine ZXB
// OnUnregister 会调用 DeallocateTransformData 清空骨骼数组。
// 如果此时有异步 EndOfFrameUpdate 正在运行（ParallelFor），
// 其中的 Follower 组件可能正在 Worker Thread 上读取本组件（作为 Leader）的骨骼数据，
// 非原子的 Empty() 操作会导致读线程观察到不一致状态，从而崩溃。
// 因此必须先等待所有异步更新完成，再清空数组。
if (UWorld* World = GetWorld())
{
    World->FinishAsyncSendAllEndOfFrameUpdates();
}
#pragma endregion

DeallocateTransformData();
```

## 关键代码变更

| 文件 | 变更说明 |
|------|---------|
| `Engine/Source/Runtime/Engine/Private/Components/SkinnedMeshComponent.cpp` | `OnUnregister()` 中添加 `FinishAsyncSendAllEndOfFrameUpdates()` 等待异步完成 |
| `Engine/Source/Runtime/Engine/Private/Components/SkinnedMeshComponent.cpp` | `CreateRenderState_Concurrent()` 中使用 `DuplicateCurrentToPrevious` 避免读取不存在的 Previous 数组 |

## 总结

1. **`TG_LastDemotable` 是 UE Tick 系统的最终保障网**：它不是给用户手动设置的，而是引擎自动将因依赖关系无法在早期 group 执行的 tick function 降级到此处。所有被延迟的 tick function 在此阶段必须全部执行完毕。

2. **`DeallocateTransformData` 有四条调用路径**：OnUnregister（最危险）、AllocateTransformData 内部失败分支、SetLeaderPoseComponent、OnRegister。其中 OnUnregister 路径在 `TG_LastDemotable` 阶段与异步 `EndOfFrameUpdate` 存在并发竞态风险。

3. **并发竞态的根因**：`StartAsyncSendAllEndOfFrameUpdates` 在 tick group 执行前启动了异步 ParallelFor 更新 Worker Thread 上的组件渲染状态，而 `TG_LastDemotable` 中的 tick function 可能在 GameThread 上触发组件销毁，导致骨骼数组被清空时 Worker Thread 仍在读取。

4. **修复策略**：在 `DeallocateTransformData()` 调用前插入 `FinishAsyncSendAllEndOfFrameUpdates()` 同步点，确保所有异步更新完成后再安全清空数组。这是一种以牺牲少量帧内并行度换取线程安全的方案。
