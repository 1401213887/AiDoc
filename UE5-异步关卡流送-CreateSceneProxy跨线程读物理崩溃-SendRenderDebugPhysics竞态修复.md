# UE5 异步关卡流送 CreateSceneProxy 跨线程读物理崩溃（SendRenderDebugPhysics 竞态）修复指南

> **一句话问题描述**：Development 包偶现崩溃，worker 线程栈 `FAddPrimitivesTask → CreateSceneProxy → SendRenderDebugPhysics → AppendDebugMassData → GetCoMWorldRotation`，AV 地址 `0xffffffffffffffff`。根因是 GR 的异步关卡流送把 `CreateSceneProxy` 丢到 detached 后台任务，链路里调试专用的 `SendRenderDebugPhysics` 在非游戏线程读取物理 `BodyInstance` 的游戏线程粒子，与真实 GameThread 并发的物理 Init/Term 撞车，读到半态/悬垂句柄。

---

## 一、问题定位流程

### 1.1 崩溃现场信息
| 项 | 值 |
|---|---|
| 构建配置 | Development（`STATICMESH_ENABLE_DEBUG_RENDERING` / `UE_ENABLE_DEBUG_DRAWING` 已开） |
| 异常线程 | `Foreground Worker #1`（后台 worker 线程，非 GameThread） |
| 异常类型 | `EXCEPTION_ACCESS_VIOLATION_READ` |
| 访问地址 | `0xffffffffffffffff`（即 -1） |
| 崩溃汇编 | `movss xmm7, dword ptr [rax + 0x40]`，`rax = 0xffffffffffffffff` |
| 触发场景 | 大量流式加载/卸载（Hall ↔ RUSH_02_Main 切图） |

### 1.2 崩溃调用链（worker 线程，自底向上）
```
FAsyncAddPrimitiveQueue::FAddPrimitivesTask   ← UE::Tasks::Launch 后台任务 + ParallelFor
  → FScene::AddPrimitive / BatchAddPrimitivesInternal
    → UStaticMeshComponent::CreateSceneProxy → CreateStaticMeshSceneProxy
      → SendRenderDebugPhysics(Proxy)          ← 仅 STATICMESH_ENABLE_DEBUG_RENDERING 编译
        → AppendDebugMassData
          → FBodyInstance::GetMassSpaceToWorldSpace()
            → FPhysicsCommand::ExecuteRead(ActorHandle, ...)   ← 拿场景读锁
              → FChaosEngineInterface::GetComTransform_AssumesLocked(Actor)
                → FParticleUtilitiesGT::GetCoMWorldTransform(&Actor->GetGameThreadAPI())
                  → GetCoMWorldRotation  ← 崩溃：ParticleUtilities.h:153
```

崩溃行：
```cpp
// Engine/Source/Runtime/Experimental/Chaos/Public/Chaos/Particle/ParticleUtilities.h:153
static inline FRotation3 GetCoMWorldRotation(T_PARTICLEHANDLE Particle)
{
    return TSpatialAccessor::GetRotation(Particle) * Particle->RotationOfMass();
    //     └ Particle->GetR() 读粒子 XR 属性偏移 0x40 处的 float 分量
}
```

### 1.3 GameThread 同时刻的栈
```
UWorld::UpdateStreamingState → ULevelStreaming::UpdateStreamingState → UWorld::AddToWorld
```
GameThread 正在**流式加载（AddToWorld）注册组件**——不是单纯 teardown。`UpdateLevelStreaming` 同帧既做 AddToWorld 又做 RemoveFromWorld，是完整的 register/unregister 风暴。

### 1.4 日志证据（时间窗口对齐）
```
[16:41:11] UnregisterComponent: (...BP_GameCharacter_C...S1SkeletalMeshComponent...) Not registered. Aborting.   ← ×多条，生命周期乱序
[16:41:19] UnregisterComponent: (...BP_GameCharacter_C...S1SkeletalMeshComponent...) Not registered. Aborting.   ← ×多条
[16:41:23] LogStreaming: UWorld::AddToWorld: updating components for /Game/Maps/Rush/RUSH_02_Main/_Generated_/... took 329.79 ms   ← 重度流式注册，对上 GT 栈
```
"生命周期风暴 + 流式并发窗口" 坐实。

---

## 二、根因分析

### 2.1 触发源头：GR 改造的异步关卡流送
- 文件：`Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.cpp`
- 类：`FAsyncRegisterLevelContext` / `FAsyncAddPrimitiveQueue`
- 开关：CVar `LevelStreaming.AsyncRegisterLevelContext.Enabled`
- 机制：流式 `AddToWorld` 注册组件时，把 `InScene->AddPrimitive(Component)` 丢进 **`UE::Tasks::Launch` detached 后台任务**，内部再 `ParallelFor` 执行 `CreateSceneProxy`。

### 2.2 为什么会读到 `0xffffffffffffffff`
1. 后台任务体打了 `FOptionalTaskTagScope(ETaskTag::EParallelGameThread)`。该 tag 的**契约**是"我在替游戏线程做并行工作，真实 GT 此刻被阻塞/参与、不会并发改数据"。
2. 但 GR 把它做成了 **detached 后台任务**——真实 GameThread **并未阻塞**，仍在 `UpdateLevelStreaming` 里 `AddToWorld` 注册 + 并发 `InitBody/TermBody`（日志里的 `Not registered. Aborting` 即证据）。**契约被打破。**
3. worker 读 `BodyInstance` 的 GT 粒子（`GetGameThreadAPI()`）时，真实 GT 正把该粒子 Init/Term 到中间态 → 内部数据指针为 `-1` 哨兵值 → `movss xmm7,[rax+0x40]`（`rax=-1`）→ AV。
4. `-1` 是"proxy 结构在、内部 GT particle 数据处于中间态"的特征（区别于 null=0、释放填充=0xdddd/0xcdcd），与"流式加载正在建/拆物理"高度吻合。

### 2.3 为什么加校验挡不住（TOCTOU）
崩溃瞬间以下校验**全部已通过**：

| 位置 | 已有校验 | 崩溃时 |
|---|---|---|
| `AppendDebugMassData` (PrimitiveComponent.cpp:1201) | `BI->IsValidBodyInstance()` | ✅ 通过 |
| `GetComTransform_AssumesLocked` (ChaosEngineInterface.cpp:736) | `ensure(IsValid(InActorReference))` | ✅ 通过（否则 return Identity 不崩） |
| `FPhysInterface_Chaos::ExecuteRead` (PhysInterface_Chaos.cpp:508) | `if(InActorReference)` + `FScopedSceneLock_Chaos` 读锁 | ✅ 通过 |

这是典型 **TOCTOU（检查时有效、使用时已失效）竞态**。且那把场景读锁是从"正在被拆的句柄"派生 solver，本身参与竞态，救不了。**堆更多同类浅校验只能压低概率，关不掉。**

### 2.4 关键线程语义（决定修法）
`IsInGameThread()`（`ThreadingBase.cpp:178`）：
```cpp
bool newValue = FTaskTagScope::IsCurrentTag(ETaskTag::EGameThread) || ...;
#if !UE_BUILD_SHIPPING && !UE_BUILD_TEST
    ...
    bool oldValue = (CurrentThreadId == GGameThreadId);
    // shiyu: if use ETaskTag::EParallelGameThread will trigger this ensure  ← GR 已注释掉 ensure
    newValue = oldValue;
#endif
return newValue;
```
- 在异步 worker 上：`IsInGameThread()` 返回 **false**（线程 id 不等于 GT）。
- `IsInParallelGameThread()`（`CoreGlobals.h:838`）返回 **true**（tag == EParallelGameThread）。
- → 这两个函数能精准区分"真 GT"与"并行游戏线程 worker"。

---

## 三、调试信息完整性问题（第二层缺口）

只加 worker 早退会导致异步流送的静态 mesh 质心调试"半永久"缺失，时序如下：

| 阶段 | 线程 | `SendRenderDebugPhysics` | 结果 |
|---|---|---|---|
| `InitBody`（PrimitiveComponent.cpp:989） | GT | 调了 | SceneProxy 尚未建，`if(UseSceneProxy)` false → **不发** |
| `CreateSceneProxy`（StaticMeshRender.cpp:3527） | worker | 调了 | 守卫早退 → **不采集** |
| 之后 | — | 仅 `UpdateDebugRendering`(BodyInstance.cpp:3420)/物理重建/销毁 | 静态不动的 mesh **长期不触发** |

**根本缺口**：原生 UE 同步批量路径 `StaticMeshResources.cpp:188-191` 是
```cpp
Scene->BatchAddPrimitives(StaticMeshComponents);
#if UE_ENABLE_DEBUG_DRAWING
UPrimitiveComponent::BatchSendRenderDebugPhysics(StaticMeshComponents);  // ← proxy 建好后在 GT 补发
#endif
```
GR 异步路径直接 worker 上 `AddPrimitive`，**没有对应的 GT 补发步骤**。这与崩溃同源：该在 GT 做的事跑到了 worker。

---

## 四、修复方案

采用**两段式**（止血 + 恢复调试完整性），全部 `#if UE_ENABLE_DEBUG_DRAWING` 包裹，Shipping 零开销，不碰物理核心、不改配置。

### 4.1 第一段：worker 早退（止血）
文件：`Engine/Source/Runtime/Engine/Private/Components/PrimitiveComponent.cpp` · `AppendDebugMassData` 入口
```cpp
#if UE_ENABLE_DEBUG_DRAWING
static void AppendDebugMassData(UPrimitiveComponent* Component, TArray<FPrimitiveSceneProxy::FDebugMassData>& DebugMassData)
{
#pragma region Engine ZXB
    // 物理句柄生命周期仅保证在真实游戏线程安全(IsValid/场景读锁都是 TOCTOU)。
    // 非真实游戏线程(含并行游戏线程)时直接跳过调试质量数据采集，避免读到半态/悬垂粒子句柄。
    if (IsInParallelGameThread() || !IsInGameThread())
    {
        return;
    }
#pragma endregion
    if (!Component->IsWelded() && Component->Mobility != EComponentMobility::Static)
    {
        // ... 原有逻辑不变 ...
    }
}
#endif
```

### 4.2 第二段：GT 补发（恢复调试完整性）
文件：`Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.h`
```cpp
#pragma region Engine ZXB
#if UE_ENABLE_DEBUG_DRAWING
    // 异步/并行创建 SceneProxy 时被守卫跳过物理质心采集；此处在真实 GT、SceneProxy 已建后补发一次。
    // 等价于原生 StaticMeshResources.cpp 中 BatchAddPrimitives 之后紧跟 BatchSendRenderDebugPhysics 的步骤。
    void FlushDebugPhysicsForBatchesOnGameThread(const TArray<FPrimitiveBatch>& InBatches);
#endif
#pragma endregion
```

文件：`AsyncRegisterLevelContext.cpp` · helper 实现
```cpp
#pragma region Engine ZXB
#if UE_ENABLE_DEBUG_DRAWING
void FAsyncAddPrimitiveQueue::FlushDebugPhysicsForBatchesOnGameThread(const TArray<FPrimitiveBatch>& InBatches)
{
    check(IsInGameThread() && !IsInParallelGameThread());
    for (const FPrimitiveBatch& Batch : InBatches)
    {
        for (const TWeakObjectPtr<UPrimitiveComponent>& WeakComp : Batch)
        {
            // 仅对已创建 SceneProxy 且仍注册的组件补发；被取消/失效(SceneProxy==nullptr)自动忽略。
            if (UPrimitiveComponent* Comp = WeakComp.Get())
            {
                if (Comp->IsRegistered() && Comp->SceneProxy != nullptr)
                {
                    Comp->SendRenderDebugPhysics();
                }
            }
        }
    }
}
#endif
#pragma endregion
```

**三个调用点**（均真实 GT）：
1. `WaitForAsyncTask()`：`AsyncTask.Wait()` 之后、`Reset()` 之前 → `FlushDebugPhysicsForBatchesOnGameThread(AsyncTask.Batches)`
2. `Tick()` 异步任务完成分支：`AsyncTask.Reset()` 前，`if (AsyncTask.IsValid())` → `FlushDebugPhysicsForBatchesOnGameThread(AsyncTask.Batches)`
3. `Tick()` GT 批量排空分支（line 338，`FAddPrimitivesTask::Execute(Batches, Scene)` 之后）→ `FlushDebugPhysicsForBatchesOnGameThread(Batches)`（用**局部** Batches，不在 AsyncTask.Batches 中）

### 4.3 路径覆盖矩阵（review 结论）
| 路径 | 线程 | AppendDebugMassData | 补发 | 结论 |
|---|---|---|---|---|
| 异步 Launch → worker ParallelFor | worker(EParallelGT) | 守卫早退（防崩） | Tick 完成分支 / WaitForAsyncTask，用 AsyncTask.Batches | ✅ 安全+完整 |
| Tick 单组件排空(line 280) | 真 GT，无 ParallelFor | 放行，正常采集 | 不需要 | ✅ |
| Tick 批量排空(line 338) | 真 GT，内含 ParallelFor(EParallelGT) | worker 迭代被守卫早退 | 用局部 Batches 补发 | ✅（修掉回归） |
| 取消分支(bPendingCancellation) | 真 GT | — | SceneProxy==nullptr，helper 自动跳过 | ✅ 无副作用 |

### 4.4 方案对比
| 方案 | 优点 | 缺点 | 采用 |
|---|---|---|---|
| A. 堆更多有效性校验（CodeX Phase A） | 改动小 | **挡不住 TOCTOU**，只压低概率不根治 | ❌ |
| B. worker 早退 + GT 补发（本方案） | 治本、只动调试代码、Shipping 零开销、对齐原生设计 | 调试信息晚一个"任务完成"节拍（肉眼无差） | ✅ |
| C. Generation 代际号（CodeX Phase C） | 通用护栏，覆盖其它异步物理读 | 对本 crash 属重锤打钉子，改动大 | 可选加固 |
| D. 关掉异步流送 CVar | 立即验证根因 | 牺牲流送性能 | 仅用于验证 |

---

## 五、快速排查 Checklist

排查"异步/多线程读物理"类崩溃时：
- [ ] 崩栈是否在 worker/task 线程，链路含 `CreateSceneProxy` / `SendRenderDebugPhysics` / 物理 `GetXxx_AssumesLocked`？
- [ ] AV 地址：`0x0`=空指针（对象没建好）；`0xdddd/0xcdcd`=释放后访问；`0xffff...ffff`=半态/未初始化哨兵句柄。
- [ ] GameThread 同刻栈是否在 `UpdateLevelStreaming` / `AddToWorld` / `RemoveFromWorld`（流送风暴）？
- [ ] 日志窗口是否有 `Not registered. Aborting` + `AddToWorld: updating components ... took Nms`（生命周期风暴证据）？
- [ ] 该物理读取是否本应只在 GameThread？确认线程：`IsInGameThread()`（worker 返回 false）、`IsInParallelGameThread()`（EParallelGameThread worker 返回 true）。
- [ ] 现有 `IsValid`/`ensure`/场景锁是否只是 TOCTOU？崩溃时它们是否其实都通过了？
- [ ] 是否是 detached 后台任务打了 `EParallelGameThread` tag 却没阻塞真实 GT，导致契约失效？
- [ ] 修复后是否有等价的"GT 侧补做"路径（对照原生同步路径缺了哪一步）？

---

## 六、相关文件与代码位置

| 文件 | 关键位置 |
|---|---|
| `Engine/Source/Runtime/Engine/Private/Components/PrimitiveComponent.cpp` | `AppendDebugMassData`(1195) / `SendRenderDebugPhysics`(1230) / `InitBody 后 SendRenderDebugPhysics`(989) |
| `Engine/Source/Runtime/Engine/Private/StaticMeshRender.cpp` | `CreateStaticMeshSceneProxy → SendRenderDebugPhysics(Proxy)`(3527) |
| `Engine/Source/Runtime/Engine/Private/StaticMeshResources.cpp` | 原生 `BatchAddPrimitives + BatchSendRenderDebugPhysics`(188-191) |
| `Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.h/.cpp` | `FAsyncAddPrimitiveQueue` / `FAddPrimitivesTask::Execute` / 新增 `FlushDebugPhysicsForBatchesOnGameThread` |
| `Engine/Source/Runtime/Engine/Private/PhysicsEngine/BodyInstance.cpp` | `GetMassSpaceToWorldSpace`(2850) / `UpdateDebugRendering`(3414) |
| `Engine/Source/Runtime/PhysicsCore/Private/ChaosEngineInterface.cpp` | `GetComTransform_AssumesLocked`(734) |
| `Engine/Source/Runtime/Engine/Private/PhysicsEngine/Experimental/PhysInterface_Chaos.cpp` | `ExecuteRead`(506) |
| `Engine/Source/Runtime/Experimental/Chaos/Public/Chaos/Particle/ParticleUtilities.h` | `GetCoMWorldRotation`(151-154, 崩溃行 153) |
| `Engine/Source/Runtime/Core/Private/HAL/ThreadingBase.cpp` | `IsInGameThread`(178) / `IsInParallelGameThread`(205) |
| `Engine/Source/Runtime/Core/Public/CoreGlobals.h` | `IsInParallelGameThread` 声明(838) |

## 附：关键 CVar
- `LevelStreaming.AsyncRegisterLevelContext.Enabled`：异步注册关卡组件（本崩溃触发开关，置 0 可验证根因）
- `LevelStreaming.AsyncRegisterLevelContext.PrimitiveBatchSize`：异步批大小（默认 16）
