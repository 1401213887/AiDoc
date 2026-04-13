# AddToWorld 异步任务超时中断 — 技术实现文档

## 1. 背景与问题

### 1.1 问题描述

在 `UWorld::AddToWorld` 的增量组件注册阶段（Incremental Update Components），存在一个 `do-while` 循环，其退出条件为：

```cpp
} while (!bTimeLimitExceeded || bHasRunningTasks);
```

当异步任务 `FAsyncAddPrimitiveQueue::AsyncTask` 长时间运行时，即使 `bTimeLimitExceeded` 已为 `true`（单帧时间预算已耗尽），循环仍会因 `bHasRunningTasks == true` 而无法退出，导致**游戏线程被阻塞，产生单帧高耗时卡顿**。

### 1.2 原始行为

原始代码在超时时仅调用 `SetCanLaunchNewTasks(false)` 阻止启动新的异步任务，但**不会中断正在执行的 `ParallelFor` 异步任务**，游戏线程必须等待其自然完成。

### 1.3 涉及的关键文件

| 文件 | 角色 |
|------|------|
| `Engine/Source/Runtime/Engine/Private/World.cpp` | `AddToWorld` 函数中的 do-while 循环 |
| `Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.h` | `FAsyncRegisterLevelContext`、`FAsyncAddPrimitiveQueue`、`FAddPrimitivesTask` 的声明 |
| `Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.cpp` | 异步任务的实现 |
| `Engine/Source/Runtime/Core/Public/Tasks/Task.h` | `UE::Tasks::FCancellationToken` 协作式取消机制 |

---

## 2. 方案设计

### 2.1 核心策略：完全非阻塞的两阶段取消

采用 **"Cancel → 立即退出 → 下一帧回收"** 的非阻塞策略，彻底避免游戏线程阻塞。

```
═══════════════════════════════════════════════════════════════════════════════
                        第一阶段：非阻塞取消（本帧）
═══════════════════════════════════════════════════════════════════════════════

  游戏线程                                          异步线程
  ────────                                          ────────
      │                                                 │
      │  ① CancellationToken.Cancel() [原子操作]        │
      │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─>│
      │                                                 │
      │  ② bPendingCancellation = true                  │  ... 正在执行 ParallelFor ...
      │                                                 │
      │  ③ bHasRunningTasks = false                     │
      │                                                 │
      │  ④ 立即退出 do-while 循环                       │
      │                                                 │
      │  ★ 游戏线程零阻塞返回 ★                         │
      ▼                                                 │
   (本帧结束)                                           │
                                                        │  ⑤ 后续迭代检测 IsCanceled()，跳过
                                                        │
                                                        │  ⑥ 任务自然完成
                                                        ▼

═══════════════════════════════════════════════════════════════════════════════
                        第二阶段：延迟回收（下一帧）
═══════════════════════════════════════════════════════════════════════════════

  游戏线程
  ────────
      │
      │  ⑦ Tick() 入口检测 bPendingCancellation == true
      │
      │  ⑧ 确认 IsCompleted() == true（异步任务已结束）
      │
      │  ⑨ 遍历 Batches，收集 SceneProxy == nullptr 的组件
      │
      │  ⑩ 将被跳过的组件重新入队到 AddPrimitivesArray 头部
      │
      │  ⑪ Reset AsyncTask + 重新构造 CancellationToken
      │
      │  ⑫ 清除 bPendingCancellation = false
      │
      │  → 继续正常的增量组件注册流程
      ▼
```

### 2.2 关键设计决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 取消方式 | `FCancellationToken` 协作式取消 | UE5 原生支持，`std::atomic<bool>` 线程安全 |
| 阻塞性 | 完全非阻塞（不调用 `Wait()`） | 避免游戏线程阻塞，真正解决单帧高耗时 |
| 组件标记 | 不在异步线程设置标记 | 避免跨线程写入 `UPrimitiveComponent` 成员变量 |
| 状态判断 | `IsCompleted()` 后在游戏线程判断 `SceneProxy == nullptr` | 完全避免线程竞争 |
| Token 管理 | `TUniquePtr<FCancellationToken>` | `FCancellationToken` 是 `UE_NONCOPYABLE` 的，需重新构造来重置 |

---

## 3. 代码实现

所有新增/修改代码均使用 `#pragma region Engine ZXB` / `#pragma endregion` 包裹。

### 3.1 FAddPrimitivesTask — CancellationToken 支持

**文件**: `AsyncRegisterLevelContext.h`

新增成员和方法：

```cpp
struct FAddPrimitivesTask
{
    // ... 原有成员 ...

#pragma region Engine ZXB
    // 带取消令牌的 Execute 重载，用于在 ParallelFor 中检查取消状态
    static void Execute(const TArray<FPrimitiveBatch>& InBatch, FSceneInterface* InScene,
                        UE::Tasks::FCancellationToken& CancellationToken);
    // 取消当前异步任务（非阻塞，仅设置原子标志）
    void CancelTask();
    // 重新构造 CancellationToken
    void ResetCancellationToken();
#pragma endregion

    TArray<FPrimitiveBatch> Batches;
    UE::Tasks::TTask<void> Task;
#pragma region Engine ZXB
    TUniquePtr<UE::Tasks::FCancellationToken> CancellationToken;
#pragma endregion
};
```

**文件**: `AsyncRegisterLevelContext.cpp`

`Launch()` 修改 — 创建 Token 并传递给 lambda：

```cpp
void FAsyncAddPrimitiveQueue::FAddPrimitivesTask::Launch(TArray<FPrimitiveBatch>& InBatches, FSceneInterface* InScene)
{
    check(IsCompleted());
    Batches = MoveTemp(InBatches);
#pragma region Engine ZXB
    if (!CancellationToken)
    {
        CancellationToken = MakeUnique<UE::Tasks::FCancellationToken>();
    }
    UE::Tasks::FCancellationToken& TokenRef = *CancellationToken;
    Task = UE::Tasks::Launch(UE_SOURCE_LOCATION, [InScene, &TokenRef, this]()
    {
        QUICK_SCOPE_CYCLE_COUNTER(STAT_AddPrimitivesTask_Execute_Async);
        FAddPrimitivesTask::Execute(Batches, InScene, TokenRef);
    });
#pragma endregion
}
```

带取消检查的 `Execute` 重载 — 在 `ParallelFor` 每次迭代前检查 `IsCanceled()`：

```cpp
#pragma region Engine ZXB
void FAsyncAddPrimitiveQueue::FAddPrimitivesTask::Execute(
    const TArray<FPrimitiveBatch>& InBatches, FSceneInterface* InScene,
    UE::Tasks::FCancellationToken& CancellationToken)
{
    check(InScene);
    const bool bAppCanEverRender = FApp::CanEverRender();
    // ... 计算 Num 和 GetComponent lambda（同原版）...

    ParallelFor(Num, [&](int32 Index)
    {
        // 协作式取消：若已取消则跳过该组件
        if (CancellationToken.IsCanceled())
        {
            return;
        }
        FOptionalTaskTagScope Scope(ETaskTag::EParallelGameThread);
        UPrimitiveComponent* Component = GetComponent(Index);
        FAddPrimitivesTask::Execute(Component, InScene, bAppCanEverRender);
    });
}
#pragma endregion
```

### 3.2 FAsyncAddPrimitiveQueue — 非阻塞取消与延迟回收

**文件**: `AsyncRegisterLevelContext.h`

新增成员和方法：

```cpp
#pragma region Engine ZXB
    void CancelAsyncTask();
    bool IsPendingCancellation() const;
#pragma endregion

    // ... 原有成员 ...
    FAddPrimitivesTask AsyncTask;
#pragma region Engine ZXB
    bool bPendingCancellation = false;
#pragma endregion
```

**文件**: `AsyncRegisterLevelContext.cpp`

`CancelAsyncTask()` — 非阻塞取消：

```cpp
#pragma region Engine ZXB
void FAsyncAddPrimitiveQueue::CancelAsyncTask()
{
    // 非阻塞取消：仅设置原子取消标志和 bPendingCancellation 标记，不调用 Wait()
    AsyncTask.CancelTask();
    bPendingCancellation = true;
}
#pragma endregion
```

`Tick()` 入口 — 延迟回收逻辑：

```cpp
bool FAsyncAddPrimitiveQueue::Tick()
{
#pragma region Engine ZXB
    if (bPendingCancellation)
    {
        if (AsyncTask.IsCompleted())
        {
            // 异步任务已完成，安全收集被跳过的组件（SceneProxy == nullptr）
            FPrimitiveBatch SkippedComponents;
            for (const FPrimitiveBatch& Batch : AsyncTask.Batches)
            {
                for (const TWeakObjectPtr<UPrimitiveComponent>& WeakComp : Batch)
                {
                    if (WeakComp.IsValid() && WeakComp->SceneProxy == nullptr)
                    {
                        SkippedComponents.Add(WeakComp);
                    }
                }
            }
            AsyncTask.Reset();
            AsyncTask.ResetCancellationToken();
            bPendingCancellation = false;
            if (!SkippedComponents.IsEmpty())
            {
                AddPrimitivesArray.Insert(MoveTemp(SkippedComponents), 0);
            }
        }
        else
        {
            // 异步任务尚未完成（极端情况），不阻塞
            return false;
        }
    }
#pragma endregion
    // ... 原有 Tick 逻辑 ...
}
```

析构函数 — 析构安全：

```cpp
FAsyncAddPrimitiveQueue::~FAsyncAddPrimitiveQueue()
{
#pragma region Engine ZXB
    if (bPendingCancellation && !AsyncTask.IsCompleted())
    {
        AsyncTask.Wait();  // 析构时允许阻塞
        AsyncTask.Reset();
        bPendingCancellation = false;
    }
#pragma endregion
    check(!HasRemainingWork());
}
```

### 3.3 FAsyncRegisterLevelContext — 上层接口

**文件**: `AsyncRegisterLevelContext.h` / `.cpp`

```cpp
#pragma region Engine ZXB
void FAsyncRegisterLevelContext::CancelRunningAsyncTasks()
{
    if (AsyncAddPrimitiveQueue.IsRunningAsync())
    {
        AsyncAddPrimitiveQueue.CancelAsyncTask();
    }
    // FSendRenderDynamicDataPrimitivesQueue 当前 IsRunningAsync() 始终返回 false，预留兼容接口
}

bool FAsyncRegisterLevelContext::IsPendingCancellation() const
{
    return AsyncAddPrimitiveQueue.IsPendingCancellation();
}
#pragma endregion
```

### 3.4 World.cpp — do-while 循环修改

**文件**: `World.cpp`（约第3612-3627行）

```cpp
bTimeLimitExceeded = IsTimeLimitExceeded(TEXT("updating components"), StartTime, Level, TimeLimit);
#pragma region Engine ZXB
// 计算 bHasRunningTasks 时排除已发出取消信号的情况，使循环可以在取消后立即退出
bHasRunningTasks = AsyncRegisterLevelContext
    ? (AsyncRegisterLevelContext->IsRunningAsync() && !AsyncRegisterLevelContext->IsPendingCancellation())
    : false;
#pragma endregion
if (AsyncRegisterLevelContext && bTimeLimitExceeded && bHasRunningTasks)
{
    // Block level context to launch new tasks since we reached the time limit
    SetLevelContextCanLaunchNewTasks(false);
#pragma region Engine ZXB
    // 非阻塞取消正在运行的异步任务，发出取消信号后循环将在下一次条件检查时退出
    AsyncRegisterLevelContext->CancelRunningAsyncTasks();
    bHasRunningTasks = false;
#pragma endregion
}
```

---

## 4. 线程安全分析

### 4.1 FCancellationToken 的线程安全性

`FCancellationToken` 内部使用 `std::atomic<bool> bCanceled`，`Cancel()` 和 `IsCanceled()` 操作本身是线程安全的。

### 4.2 竞争场景分析

当游戏线程调用 `Cancel()` 时，`ParallelFor` 中的迭代处于三种状态：

| 状态 | SceneProxy | 处理方式 |
|------|-----------|---------|
| 已完成的迭代 | `!= nullptr`（完整） | 保留，不做修改 |
| 正在执行的迭代 | 过渡状态 | 等待自然完成（单个 `AddPrimitive` 是原子的） |
| 尚未开始的迭代 | `== nullptr` | 被跳过，下一帧重新入队 |

### 4.3 线程安全保证

**关键保证**：收集被跳过组件的操作严格发生在 `IsCompleted() == true` 之后（第二阶段），此时异步任务的所有工作线程已退出，所有组件的 `SceneProxy` 状态已稳定。**不存在"统计后又赋值"的竞争条件**。

### 4.4 本帧异步任务仍在运行时的安全性

第一阶段退出 do-while 循环后，异步任务可能仍在后台运行。安全性保证：

1. **check 断言安全**：`bAreComponentsCurrentlyRegistered` 为 `false`，断言 `check(!Level->bAreComponentsCurrentlyRegistered || ...)` 不会触发
2. **后续步骤不执行**：`bAlreadyUpdatedComponents` 和 `bExecuteNextStep` 均为 `false`
3. **数据隔离**：异步任务只访问已 `MoveTemp` 到 `FAddPrimitivesTask` 中的 `Batches` 和 `FSceneInterface*`，不会访问 `AddPrimitivesArray` 或其他游戏线程独占数据

---

## 5. SceneProxy 兼容性

| 场景 | 处理方式 |
|------|---------|
| 已完成 `AddPrimitive` 的组件 | SceneProxy 已创建且已提交渲染命令，状态完整，不做修改 |
| 被跳过的组件 | `SceneProxy == nullptr`，下一帧重新入队处理 |
| 下一帧重新处理时 | 依赖现有安全检查（`IsRegistered()`、`SceneProxy == nullptr`）防止重复添加 |
| 组件在跳过后被销毁 | `TWeakObjectPtr` 机制自动处理，`IsValid()` 返回 `false` 则跳过 |

---

## 6. 边界情况处理

### 6.1 异步注册未启用

当 `LevelStreaming.AsyncRegisterLevelContext.Enabled` 为 `false` 时，`AsyncRegisterLevelContext` 为 `nullptr`，所有取消逻辑的 `nullptr` 检查已覆盖，行为与修改前完全一致。

### 6.2 增量注册已完成

当 `bIncrementalRegisterComponentsDone` 为 `true` 时，不触发取消机制，保持现有的同步收尾逻辑。

### 6.3 组件注册已完成

当 `bAreComponentsCurrentlyRegistered` 为 `true` 或 `HasPreRegisteringComponents()` 为 `true` 时，循环通过 `break` 退出，不触发取消机制。

### 6.4 析构安全

`FAsyncAddPrimitiveQueue` 析构时，如果 `bPendingCancellation` 为 `true` 且异步任务尚未完成，则调用 `Wait()` 等待完成（析构时允许阻塞，因为这是必要的资源清理）。

### 6.5 极端情况：下一帧异步任务仍未完成

如果单个 `AddPrimitive` 操作耗时极长，导致下一帧 `Tick()` 时异步任务仍未完成，系统返回 `false` 不阻塞，让调用方在后续 `Tick` 再次尝试。

---

## 7. 修改文件清单

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `AsyncRegisterLevelContext.h` | 新增声明 | `FAddPrimitivesTask` 添加 `CancellationToken`、`CancelTask()`、`ResetCancellationToken()`；`FAsyncAddPrimitiveQueue` 添加 `bPendingCancellation`、`CancelAsyncTask()`、`IsPendingCancellation()`；`FAsyncRegisterLevelContext` 添加 `CancelRunningAsyncTasks()`、`IsPendingCancellation()` |
| `AsyncRegisterLevelContext.cpp` | 新增实现 + 修改 | `Launch()` 传递 Token；新增带取消检查的 `Execute` 重载；`Tick()` 入口添加延迟回收；析构函数添加安全等待；新增 `CancelAsyncTask()`、`CancelRunningAsyncTasks()` 等方法实现 |
| `World.cpp` | 修改逻辑 | `bHasRunningTasks` 计算增加 `!IsPendingCancellation()` 条件；超时时调用 `CancelRunningAsyncTasks()` 并设置 `bHasRunningTasks = false` |

---

## 8. 验证要点

- [ ] **正常流程**：`bTimeLimitExceeded` 未触发时，行为与修改前完全一致
- [ ] **取消流程**：模拟超时场景，确认循环能立即退出（零阻塞）
- [ ] **延迟回收**：确认下一帧 `Tick()` 能正确收集被跳过的组件并重新入队
- [ ] **SceneProxy 兼容**：已完成 `AddPrimitive` 的组件 SceneProxy 不受影响
- [ ] **重复添加防护**：被跳过的组件在下一帧重新处理时不会重复添加
- [ ] **组件销毁安全**：被跳过的组件在下一帧前被销毁时能安全跳过
- [ ] **异步未启用**：`AsyncRegisterLevelContext` 为 `nullptr` 时无异常
- [ ] **析构安全**：Level 卸载时不会因未完成的异步任务导致崩溃
