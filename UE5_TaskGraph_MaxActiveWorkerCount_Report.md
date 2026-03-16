# UE5 TaskGraph Worker 线程动态数量控制 — 技术分析报告

## 1. 项目概述

### 1.1 背景

UE5 的 TaskGraph 调度器（`FScheduler`）采用双层 Worker 架构：
- **普通 Worker**（Normal Worker）：启动时即创建，数量由 `ThreadCount` 决定
- **Standby Worker**：按需激活的备用线程，在 Oversubscription 期间动态唤醒/休眠

原始引擎未提供运行时动态调整活跃 Worker 数量的能力。本次开发的目标是实现**运行时动态限制活跃 Worker 总数**的功能，支持通过控制台命令或 C++ API 实时调节。

### 1.2 最终目标

提供 `TaskGraph.MaxActiveForegroundWorkers` 和 `TaskGraph.MaxActiveBackgroundWorkers` 两个 CVar，实现：
- **优先减少 Standby Worker**
- Standby Worker 全部休眠后，**继续减少普通 Worker**
- 放宽限制时自动唤醒休眠的 Worker

---

## 2. 迭代演进过程

本功能经历了 **7 轮迭代**，逐步完善：

| 轮次 | 主题 | 关键决策 |
|------|------|---------|
| 第 1 轮 | 新增 `ForceStandbyWorkerCount` | 默认值 0 不起作用，> 0 时限制 Standby Worker 数量 |
| 第 2 轮 | 删除 `GetEffectiveThreadCount()` | 改为用 `ActiveThreadCount - ThreadCount` 直接表达 Standby Worker 数量，语义更清晰 |
| 第 3 轮 | 默认值改为 -1 | `int32` 类型，-1 = 不限制，>= 0 时生效（0 = 禁止所有 Standby Worker） |
| 第 4 轮 | 原子操作改造 | `ForceStandbyWorkerCount` 从 `int32` 改为 `std::atomic<int32>`，消除多线程数据竞争 |
| 第 5 轮 | 代码正确性审查 | 修复 `FMath::Clamp` 下限和唤醒逻辑，确认无残留废弃代码 |
| 第 6 轮 | **重构为 `MaxActiveWorkerCount`** | 统一控制普通 + Standby Worker 总数，新增 `ShouldYieldWorker`/`WaitForYieldResume` 让出机制 |
| 第 7 轮 | 修正 `GetActiveThreadCount` 语义 + 可读性优化 | 修正判断逻辑（总数直接比较），拆分 `ShouldYieldWorker` 与 `WaitForYieldResume` 职责 |

---

## 3. 系统架构

### 3.1 整体架构图

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                              用户接口层                                     ║
║                                                                              ║
║  +--------------------------------------+  +------------------------------+  ║
║  | TaskGraph.MaxActiveForegroundWorkers |  | TaskGraph.MaxActiveBackground|  ║
║  |             (CVar)                   |  |       Workers (CVar)         |  ║
║  +------------------+-------------------+  +--------------+---------------+  ║
║                     |                                     |                  ║
║                     +------------------+------------------+                  ║
║                                        |                                     ║
║  +-------------------------------------+-------------------------------+     ║
║  | FScheduler::SetMaxActiveWorkerCount() (C++ API)                     |     ║
║  +-------------------------------------+-------------------------------+     ║
╚════════════════════════════════════════╪══════════════════════════════════════╝
                                         |
                                         v
╔══════════════════════════════════════════════════════════════════════════════╗
║                    Scheduler 层 (Scheduler.h / Scheduler.cpp)               ║
║                                                                              ║
║  +---------------------------+   +---------------------------+               ║
║  | SetMaxActiveWorkerCount() |   | GetMaxActiveWorkerCount() |               ║
║  +-------------+-------------+   +---------------------------+               ║
║                |                                                             ║
║                |   +--------------------------------------+                  ║
║                |   | WorkerLoop() — ShouldYieldWorker 检查 |--+              ║
║                |   +--------------------------------------+  |               ║
║                |   +--------------------------------------+  |               ║
║                |   | StandbyLoop() — ConditionalStandby    |  |              ║
║                |   +------------------+-------------------+  |               ║
║                |   +--------------------------------------+  |               ║
║                |   | StopWorkers() — WakeYieldedWorkers    |--+              ║
║                |   +------------------+-------------------+  |               ║
╚════════════════╪═══════════════════════╪══════════════════════╪═══════════════╝
                 |                       |                      |
                 v                       v                      v
╔══════════════════════════════════════════════════════════════════════════════╗
║                WaitingQueue 层 (WaitingQueue.h / WaitingQueue.cpp)          ║
║                                                                              ║
║  数据:                                                                       ║
║  +---------------------------------------------+                            ║
║  | MaxActiveWorkerCount (atomic<int32>, 默认-1) |<---[写入]---+              ║
║  +---------------------------------------------+             |              ║
║  +---------------------------------------------+             |              ║
║  | YieldedWorkerCount   (atomic<int32>)         |<--[CAS]--+ |              ║
║  +---------------------------------------------+           | |              ║
║  +---------------------------------------------+           | |              ║
║  | YieldEvent (FEventRef, ManualReset)          |<-[Wait]-+| |              ║
║  +---------------------------------------------+          || |              ║
║                                                            || |              ║
║  函数:                                                     || |              ║
║  +---------------------------+                             || |              ║
║  | SetMaxActiveWorkerCount() +----[写入 MaxActiveWorkerCount]+              ║
║  |                           +----[调用 WakeYieldedWorkers()]|               ║
║  |                           +----[Notify() 唤醒 Standby]   |               ║
║  +---------------------------+                             ||                ║
║  +---------------------------+                             ||                ║
║  | ShouldYieldWorker()       +---[CAS 抢名额]-> YieldedWorkerCount          ║
║  |                           +---[返回 true]               ||                ║
║  +---------------------------+                             ||                ║
║  +---------------------------+                             ||                ║
║  | WaitForYieldResume()      +---[Wait()]-> YieldEvent ----+|                ║
║  +---------------------------+                              |                ║
║  +---------------------------+                              |                ║
║  | WakeYieldedWorkers()      +---[Trigger()]-> YieldEvent   |                ║
║  |                           +---[递减]-> YieldedWorkerCount|                ║
║  +---------------------------+                              |                ║
║  +---------------------------+                              |                ║
║  | ConditionalStandby()      +---[读取 MaxActiveWorkerCount]+                ║
║  +---------------------------+                              |                ║
║  +---------------------------+                              |                ║
║  | TryStartNewThread()       +---[读取 MaxActiveWorkerCount]+                ║
║  +---------------------------+                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝

调用关系:
  用户接口层 ----> Scheduler.SetMaxActiveWorkerCount()
                          |
                          +---> WaitingQueue.SetMaxActiveWorkerCount()
                          |         |---> 写入 MaxActiveWorkerCount
                          |         |---> WakeYieldedWorkers()
                          |         +---> Notify() -> TryStartNewThread()
                          |
  WorkerLoop -----------> ShouldYieldWorker()
                              |--[CAS]--> YieldedWorkerCount
                              +--[true]--> WaitForYieldResume() --[Wait]--> YieldEvent
                          
  StandbyLoop ----------> ConditionalStandby() --[读取]--> MaxActiveWorkerCount
  TryStartNewThread() ----[读取]--> MaxActiveWorkerCount
  StopWorkers ----------> WakeYieldedWorkers()
                              |--[Trigger]--> YieldEvent
                              +--[递减]----> YieldedWorkerCount
```

### 3.2 修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `WaitingQueue.h` | 新增 `MaxActiveWorkerCount`、`YieldedWorkerCount`、`YieldEvent` 成员变量；声明 `SetMaxActiveWorkerCount`/`GetMaxActiveWorkerCount`/`ShouldYieldWorker`/`WaitForYieldResume`/`WakeYieldedWorkers` |
| `WaitingQueue.cpp` | 实现上述所有函数；修改 `Init`/`ConditionalStandby`/`TryStartNewThread` |
| `Scheduler.h` | 声明 `SetMaxActiveWorkerCount`/`GetMaxActiveWorkerCount` |
| `Scheduler.cpp` | 注册 CVar；实现 Scheduler 层接口；`WorkerLoop` 中添加 Yield 检查；`StopWorkers` 中 Shutdown 前唤醒 |

---

## 4. 核心实现细节

### 4.1 数据结构

```cpp
// WaitingQueue.h
std::atomic<int32>  MaxActiveWorkerCount{ -1 };  // -1 不限制, >= 0 限制活跃总数
std::atomic<int32>  YieldedWorkerCount{ 0 };      // 已让出的普通 Worker 数量
FEventRef           YieldEvent{ EEventMode::ManualReset }; // 普通 Worker 休眠用
```

### 4.2 Standby Worker 限制（ConditionalStandby）

```cpp
void FWaitingQueue::ConditionalStandby(FWaitEvent* Node)
{
    const int32 MaxActive = MaxActiveWorkerCount.load(std::memory_order_relaxed);
    while (GetActiveThreadCount(LocalState) > ThreadCount + Oversubscription
        // 新增条件：活跃总数超过 MaxActive 时也要休眠
        || (MaxActive >= 0 && (int32)GetActiveThreadCount(LocalState) > MaxActive)
    )
    {
        // CAS 将 Standby Worker 放入休眠栈
        // ...
    }
}
```

**关键点**：`GetActiveThreadCount()` 返回的是**所有活跃线程总数**（普通 + Standby），而非单独的 Standby Worker 数量，因此直接与 `MaxActive` 比较。

### 4.3 Standby Worker 创建限制（TryStartNewThread）

```cpp
bool FWaitingQueue::TryStartNewThread()
{
    const int32 MaxActive = MaxActiveWorkerCount.load(std::memory_order_relaxed);
    while (GetActiveThreadCount(LocalState) < MaxThreadCount
        && GetActiveThreadCount(LocalState) < ThreadCount + Oversubscription
        // 新增条件：活跃总数达到 MaxActive 时不允许创建新 Standby
        && (MaxActive < 0 || (int32)GetActiveThreadCount(LocalState) < MaxActive)
    )
    {
        // CAS 唤醒/创建 Standby Worker
        // ...
    }
}
```

### 4.4 普通 Worker 让出机制（ShouldYieldWorker + WaitForYieldResume）

**判断阶段**（无锁 CAS）：
```cpp
bool FWaitingQueue::ShouldYieldWorker()
{
    const int32 MaxActive = MaxActiveWorkerCount.load(std::memory_order_relaxed);
    if (MaxActive < 0 || MaxActive >= (int32)ThreadCount)
        return false;  // 不需要限制普通 Worker

    const int32 NeedYield = (int32)ThreadCount - MaxActive;
    int32 CurrentYielded = YieldedWorkerCount.load(std::memory_order_relaxed);
    while (CurrentYielded < NeedYield)
    {
        if (YieldedWorkerCount.compare_exchange_weak(CurrentYielded, CurrentYielded + 1))
        {
            return true;  // 抢到让出名额
        }
    }
    return false;
}
```

**休眠阶段**（职责分离，可读性好）：
```cpp
void FWaitingQueue::WaitForYieldResume()
{
    YieldEvent->Wait();                    // 休眠
    YieldedWorkerCount.fetch_sub(1);       // 唤醒后递减计数
}
```

**调用方**（Scheduler.cpp WorkerLoop）：
```cpp
if (WaitingQueue[bPermitBackgroundWork].ShouldYieldWorker())
{
    WaitingQueue[bPermitBackgroundWork].WaitForYieldResume();
}
```

### 4.5 唤醒机制（WakeYieldedWorkers）

```cpp
void FWaitingQueue::WakeYieldedWorkers()
{
    if (YieldedWorkerCount.load(std::memory_order_relaxed) > 0)
    {
        YieldEvent->Trigger();
        while (YieldedWorkerCount.load(std::memory_order_relaxed) > 0)
        {
            FPlatformProcess::YieldThread();  // 自旋等待所有 Worker 醒来
        }
        YieldEvent->Reset();
    }
}
```

### 4.6 设置接口（SetMaxActiveWorkerCount）

```cpp
void FWaitingQueue::SetMaxActiveWorkerCount(int32 InCount)
{
    const int32 OldCount = MaxActiveWorkerCount.load(std::memory_order_relaxed);
    const int32 NewCount = (InCount < 0) ? -1 : FMath::Clamp(InCount, 0, (int32)MaxThreadCount);
    MaxActiveWorkerCount.store(NewCount, std::memory_order_relaxed);

    if (NewCount < 0 || (OldCount >= 0 && NewCount > OldCount))
    {
        WakeYieldedWorkers();  // 先唤醒普通 Worker
        
        const int32 Delta = (NewCount < 0) ? (int32)MaxThreadCount : (NewCount - FMath::Max(OldCount, 0));
        if (Delta > 0)
        {
            Notify(Delta);     // 再唤醒 Standby Worker
        }
    }
}
```

---

## 5. 两类 Worker 的完整生命周期

### 5.1 普通 Worker

```
                        +-------------------+
                        |    [*] 初始状态    |
                        +---------+---------+
                                  |
                                  | StartWorkers 创建
                                  v
                  +===============================+
                  |           Running              |<---------------------------+
                  +===============================+                            |
                   |          |           |                                     |
                   |          |           |                                     |
   ShouldYieldWorker()    无任务       StopWorkers                              |
   == true (CAS抢到名额) CommitWait                                            |
                   |          |           |                                     |
                   v          v           v                                     |
           +-----------+  +---------+  +----------+                            |
           |  Yielded  |  | Parked  |  | [*] 终止 |                            |
           +-----------+  +---------+  +----------+                            |
              |    |         |    |                                             |
              |    |         |    |                                             |
 WakeYieldedWorkers()  Notify    StartShutdown                                 |
 (YieldEvent.Trigger)  Unpark                                                  |
              |    |         |    |                                             |
              |    |         |    v                                             |
              |    |         | +----------+                                    |
              |    |         | | [*] 终止 |                                    |
              |    |         | +----------+                                    |
              |    |         |                                                  |
              |    |         +--------------------------------------------------+
              |    |                                                            |
              |    +---> +----------+                                           |
              |         | [*] 终止 |  (WakeYieldedWorkers + StopWorkers)        |
              |         +----------+                                           |
              +-------------------------------------------------------------->-+
```

### 5.2 Standby Worker

```
                        +-------------------+
                        |    [*] 初始状态    |
                        +---------+---------+
                                  |
                                  | TryStartNewThread 唤醒/创建
                                  v
                  +===============================+
                  |           Running              |<-----------------------+
                  +===============================+                        |
                   |          |           |                                 |
                   |          |           |                                 |
  ConditionalStandby     无任务        StopWorkers                          |
  (ActiveCount>MaxActive) CommitStandby                                    |
                   |          |           |                                 |
                   v          v           v                                 |
           +-----------+  +----------------+  +----------+                 |
           |  Standby  |  |CommittedStandby|  | [*] 终止 |                 |
           +-----------+  +----------------+  +----------+                 |
              |    |          |    |                                        |
              |    |          |    |                                        |
 TryStartNewThread |  TryStartNewThread                                    |
 (Event.Trigger)   |          |   StartShutdown                            |
              |    |          |    |                                        |
              |    |          |    v                                        |
              |    |          | +----------+                                |
              |    |          | | [*] 终止 |                                |
              |    |          | +----------+                                |
              |    |          |                                              |
              |    |          +---------------------------------------------+
              |    |                                                        |
              |    +---> +----------+                                       |
              |         | [*] 终止 |  (StartShutdown)                       |
              |         +----------+                                       |
              +---------------------------------------------------------->-+
```

---

## 6. 限制策略决策流程

```
                +-------------------------------+
                | SetMaxActiveWorkerCount(N)     |
                +---------------+---------------+
                                |
                                v
                        +-------+-------+
                        |   N < 0 ?     |
                        +---+-------+---+
                            |       |
                       [是] |       | [N >= 0]
                            v       v
    +---------------------------+   +-------------------+
    | 不限制                     |   |  N >= ThreadCount? |
    | 使用原有 ThreadCount +     |   +----+----------+---+
    | Oversubscription 逻辑     |        |          |
    +---------------------------+   [是] |          | [否: N < ThreadCount]
                                        v          v
              +-------------------------+-+  +-----+---------------------------+
              | 仅限制 Standby Worker      |  | Standby 全禁 + 限制普通 Worker  |
              +---------------------------+  +--------------------------------+
                  |          |         |          |          |            |
                  v          v         v          v          v            v
          +-----------+ +-----------+ +------+ +--------+ +----------+ +----------------+
          |Conditional| |TryStartNew| |普通   | |Standby:| |普通Worker| |WorkerLoop ->   |
          |Standby:   | |Thread:    | |Worker:| |上限=0  | |需让出    | |ShouldYield     |
          |ActiveCount| |ActiveCount| |不受   | |全部禁止| |ThreadCount| |Worker(): CAS  |
          |> N -> 多余| |>= N -> 不 | |影响   | |激活    | |- N 个   | |抢名额 -> Wait |
          |Standby休眠| |创建新     | |       | |        | |          | |ForYieldResume |
          |           | |Standby    | |       | |        | |          | |() 休眠        |
          +-----------+ +-----------+ +------+ +--------+ +----------+ +----------------+
```

---

## 7. 线程安全分析

### 7.1 原子变量使用

| 变量 | 类型 | Memory Order | 写入线程 | 读取线程 |
|------|------|-------------|---------|---------|
| `MaxActiveWorkerCount` | `std::atomic<int32>` | `relaxed` | 游戏线程/控制台线程 | 任意 Worker 线程 |
| `YieldedWorkerCount` | `std::atomic<int32>` | `relaxed` | 任意 Worker 线程（CAS） | 游戏线程 + Worker 线程 |
| `Oversubscription` | `std::atomic<uint32>` | `relaxed` | 任意线程 | 任意 Worker 线程 |

### 7.2 选择 `memory_order_relaxed` 的原因

- 这些变量仅作为**辅助判断条件**，不需要与其他变量之间的顺序保证
- `ConditionalStandby` 和 `TryStartNewThread` 中已有 `StandbyState` 的 CAS 操作提供了更强的内存序（`memory_order_acq_rel`）
- 即使偶尔读到稍旧的值，后续循环中会重新读取，不影响正确性（"最终一致"即可）

### 7.3 CAS 抢名额模式

`ShouldYieldWorker` 使用 `compare_exchange_weak` 实现无锁名额分配：
- 多个 Worker 线程同时竞争时，只有一个线程能成功
- 失败的线程循环重试或退出
- `weak` 版本在 while 循环中性能优于 `strong`

---

## 8. Shutdown 安全保障

在 `StopWorkers` 中：
```
1. WakeYieldedWorkers()   ← 唤醒所有被 Yield 的普通 Worker
2. StartShutdown()         ← 设置 bIsShuttingDown + 唤醒所有 Standby Worker + Notify 所有 Parked Worker
3. Join() 所有线程
4. FinishShutdown()
```

如果不先 `WakeYieldedWorkers()`，被 Yield 的普通 Worker 会一直 `Wait()` 在 `YieldEvent` 上，而 `StartShutdown()` 不会触发 `YieldEvent`，导致**死锁**。

---

## 9. 使用方式

### 9.1 控制台命令

```
# 限制前台最多 3 个活跃 Worker（优先砍 Standby，不够砍普通）
TaskGraph.MaxActiveForegroundWorkers 3

# 限制后台最多 1 个活跃 Worker
TaskGraph.MaxActiveBackgroundWorkers 1

# 恢复不限制（默认）
TaskGraph.MaxActiveForegroundWorkers -1
TaskGraph.MaxActiveBackgroundWorkers -1
```

### 9.2 C++ API

```cpp
// 限制前台最多 2 个活跃 Worker，后台不变
LowLevelTasks::FScheduler::Get().SetMaxActiveWorkerCount(2, INT32_MIN);

// 限制后台最多 1 个活跃 Worker，前台不变
LowLevelTasks::FScheduler::Get().SetMaxActiveWorkerCount(INT32_MIN, 1);

// 恢复不限制
LowLevelTasks::FScheduler::Get().SetMaxActiveWorkerCount(-1, -1);

// 查询当前设置
int32 Fg, Bg;
LowLevelTasks::FScheduler::Get().GetMaxActiveWorkerCount(Fg, Bg);
```

### 9.3 参数约定

| 参数值 | 含义 |
|--------|------|
| `-1`（默认） | 不限制（使用原有 ThreadCount + Oversubscription 逻辑） |
| `0` | 禁止所有 Worker 活跃（Standby 全禁 + 普通 Worker 全部让出） |
| `1 ~ ThreadCount - 1` | Standby 全禁 + 部分普通 Worker 让出 |
| `ThreadCount ~ MaxThreadCount` | 只限制 Standby Worker，普通 Worker 不受影响 |
| `INT32_MIN`（仅 Scheduler 层） | 不修改对应队列（前台/后台独立设置时使用） |

---

## 10. 关键技术决策记录

| # | 决策 | 原因 |
|---|------|------|
| 1 | 默认值 `-1` 而非 `0` | `0` 有实际含义（禁止所有 Standby），`-1` 明确表示"不限制" |
| 2 | 使用 `std::atomic` + `memory_order_relaxed` | 与 `Oversubscription` 保持一致的并发模式，避免数据竞争 |
| 3 | 拆分 `ShouldYieldWorker()` 与 `WaitForYieldResume()` | 函数名与行为一致，`Should` 只判断，休眠交给调用方显式执行 |
| 4 | `ManualReset` 的 `FEvent` 批量唤醒 | 所有被 Yield 的 Worker 共享一个事件，`Trigger()` 一次全部唤醒，`Reset()` 后可复用 |
| 5 | 修正 `GetActiveThreadCount` 语义理解 | 它返回的是**所有活跃线程总数**（含普通 Worker），直接与 `MaxActive` 比较，不需要减去 `ThreadCount` |
| 6 | Shutdown 前先 `WakeYieldedWorkers` | 避免被 Yield 的 Worker 在 `YieldEvent.Wait()` 上死锁 |
| 7 | CVar 使用 `INT32_MIN` 区分"不修改" | 前台/后台 CVar 独立回调，修改一个不应影响另一个 |

---

## 11. 所有代码变更标记

所有新增/修改的代码均包裹在 `#pragma region Engine ZXB` / `#pragma endregion` 之间，便于在引擎升级时快速定位和合并自定义修改。
