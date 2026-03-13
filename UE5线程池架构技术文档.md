
# UE5 线程池架构技术文档

> **项目**: GR_release / S1Game  
> **引擎**: UE5EA  
> **日期**: 2026-03-13  
> **相关文件**:
> - `Engine/Source/Runtime/Core/Public/Misc/QueuedThreadPool.h` — 线程池接口定义
> - `Engine/Source/Runtime/Core/Private/HAL/ThreadingBase.cpp` — 线程池核心实现
> - `Engine/Source/Runtime/Launch/Private/LaunchEngineLoop.cpp` — 全局线程池创建
> - `Engine/Source/Runtime/Core/Public/Async/AsyncWork.h` — 异步任务封装
> - `S1Game/Plugins/GameFundamental/Source/GameFundamental/Private/Comm/WindowsCPUUsageTrack.cpp` — CPU 使用率监控（项目自定义扩展）

---

## 目录

1. [UE5 线程池整体架构](#1-ue5-线程池整体架构)
2. [FQueuedThreadPoolBase 类详解](#2-fqueuedthreadpoolbase-类详解)
   - 2.1 类继承关系
   - 2.2 核心成员变量
   - 2.3 核心方法分析
3. [MarkBusy / MarkNormal 机制](#3-markbusy--marknormal-机制)
   - 3.1 方法定义
   - 3.2 核心影响：动态限流
   - 3.3 在 WindowsCPUUsageTrack 中的应用
4. [GBackgroundPriorityThreadPool 详解](#4-gbackgroundprioritythreadpool-详解)
   - 4.1 创建参数
   - 4.2 使用场景
   - 4.3 为什么不放在 TaskGraph 内
5. [全局线程池对比总结](#5-全局线程池对比总结)

---

## 1. UE5 线程池整体架构

UE5 中存在两套并行执行架构：**TaskGraph（任务图系统）** 和 **QueuedThreadPool（队列式线程池）**。它们服务于不同的场景：

```
+=============================================+     +=============================================+
|       TaskGraph (任务图系统)                  |     |     QueuedThreadPool (线程池系统)             |
|---------------------------------------------|     |---------------------------------------------|
|  [GameThread]    (命名线程)                   |     |  GThreadPool                                |
|  [RenderThread]  (命名线程)                   |     |    Normal优先级 | 多线程                      |
|  [RHIThread]     (命名线程)                   |     |                                             |
|                                             |     |  GIOThreadPool                              |
|  [AnyThread Worker 1]                       |     |    Normal优先级 | IO专用                      |
|  [AnyThread Worker 2]                       |     |                                             |
|  [AnyThread Worker N]                       |     |  GBackgroundPriorityThreadPool              |
|                                             |     |    Lowest优先级 | 仅2线程                     |
|  * 任务间支持 DAG 依赖调度                    |     |                                             |
+=============================================+     +=============================================+
```

**全局线程池实例**（定义在 `QueuedThreadPool.h`）:

```cpp
extern CORE_API FQueuedThreadPool* GThreadPool;                    // 通用异步任务
extern CORE_API FQueuedThreadPool* GIOThreadPool;                  // IO 专用
extern CORE_API FQueuedThreadPool* GBackgroundPriorityThreadPool;  // 低优先级后台任务
#if WITH_EDITOR
extern CORE_API FQueuedThreadPool* GLargeThreadPool;               // 编辑器专用大线程池
#endif
```

---

## 2. FQueuedThreadPoolBase 类详解

`FQueuedThreadPoolBase` 是 UE 引擎中 **线程池的核心实现类**，继承自 `FQueuedThreadPool` 抽象接口，定义在 `ThreadingBase.cpp` 第1166行。它实现了一个完整的 **带优先级的工作队列线程池**。

### 2.1 类继承关系

```
  +-----------------------------------------------+
  |      FQueuedThreadPool  <<abstract>>           |
  |-----------------------------------------------|
  | + Create() : bool                             |
  | + Destroy()                                   |
  | + AddQueuedWork(IQueuedWork*, Priority)        |
  | + RetractQueuedWork(IQueuedWork*) : bool       |
  | + GetNumThreads() : int32                     |
  | + MarkBusy()                                  |
  | + MarkNormal()                                |
  | + GetCpuState() : EThreadPoolCpuStateType     |
  | # CpuState : EThreadPoolCpuStateType          |
  +-----------------------------------------------+
                        ▲
                        | 继承
  +-----------------------------------------------+
  |         FQueuedThreadPoolBase                  |
  |-----------------------------------------------|
  | # QueuedWork : FThreadPoolPriorityQueue       |
  | # QueuedThreads : TArray<FQueuedThread*>      |
  | # AllThreads : TArray<FQueuedThread*>         |
  | # SynchQueue : FCriticalSection*              |
  | # TimeToDie : bool                            |
  | # LimitThreadToUsed : int32                   |
  | # LimitThreadToUsedWhenBusy : int32           |
  |-----------------------------------------------|
  | + CreateInternal()                            |
  | + AddQueuedWork()                             |
  | + ReturnToPoolOrGetNextJob()                  |
  | + MarkBusy()                                  |
  | + MarkNormal()                                |
  +-----------------------------------------------+
         |  持有                    |  管理
         ▼                         ▼
  +----------------------------+  +----------------------------+
  | FThreadPoolPriorityQueue   |  |      FQueuedThread         |
  |----------------------------|  |----------------------------|
  | + Enqueue(Work, Priority)  |  | + DoWork(IQueuedWork*)     |
  | + Dequeue() : IQueuedWork* |  | + KillThread()             |
  | + Retract(Work) : bool     |  +----------------------------+
  | + Num() : int32            |              | 执行
  +----------------------------+              ▼
                                  +----------------------------+
                                  |  IQueuedWork <<interface>> |
                                  |----------------------------|
                                  | + DoThreadedWork()        |
                                  | + Abandon()               |
                                  +----------------------------+
```

### 2.2 核心成员变量

| 成员 | 类型 | 作用 |
|------|------|------|
| `QueuedWork` | `FThreadPoolPriorityQueue` | **优先级工作队列**，按 `EQueuedWorkPriority` 优先级（Blocking > Highest > High > Normal > Low > Lowest）存放待执行的任务 |
| `QueuedThreads` | `TArray<FQueuedThread*>` | **空闲线程列表**，存放当前处于等待状态、可被分配工作的线程 |
| `AllThreads` | `TArray<FQueuedThread*>` | **全部线程列表**，记录线程池中所有创建的线程（无论忙闲） |
| `SynchQueue` | `FCriticalSection*` | **临界区锁**，保护所有共享数据的线程安全访问 |
| `TimeToDie` | `bool` | **销毁标志**，为 `true` 时线程池进入销毁流程，拒绝新任务 |
| `LimitThreadToUsed` | `int32` | **正常模式下的线程保留数**，保证至少保留这么多线程不被使用（限流） |
| `LimitThreadToUsedWhenBusy` | `int32` | **繁忙模式下的线程保留数**，CPU 繁忙时使用更严格的限制 |
| `CpuState` | `EThreadPoolCpuStateType` | **CPU 状态标记**，`CPU_Normal` 或 `CPU_Busy`，影响限流策略选择 |

### 2.3 核心方法分析

#### 2.3.1 `CreateInternal()` — 线程池创建

```
  +---------------------------+
  |      CreateInternal       |
  +---------------------------+
              |
              v
  +---------------------------+
  |  创建 FCriticalSection 锁  |
  +---------------------------+
              |
              v
  +---------------------------+
  |     预分配线程数组          |
  +---------------------------+
              |
              v
  +---------------------------+
  | 循环创建 InNumQueuedThreads |
  |    个 FQueuedThread        |
  +---------------------------+
              |
              v
        /创建成功?/
       /          \
      v            v
  [  是  ]      [  否  ]
      |            |
      v            v
  +-----------+  +------------------+
  | 加入       |  | 销毁所有          |
  | QueuedThreads| | 已创建线程        |
  | 和 AllThreads| +------------------+
  +-----------+          |
      |                  v
      v           +-----------+
 +-----------+   | 返回失败    |
 | 返回成功    |   +-----------+
 +-----------+
```

- 根据参数决定创建普通线程还是 Forkable 线程
- 支持 `OverrideStackSize` 全局栈大小覆盖
- 创建成功的线程同时加入 `QueuedThreads`（空闲）和 `AllThreads`（总表）

#### 2.3.2 `AddQueuedWork()` — 任务调度（核心）

这是线程池最关键的方法，决定了任务如何被分配：

```
  +--------------------------------------+
  | AddQueuedWork(InQueuedWork, Priority)|
  +--------------------------------------+
                   |
                   v
             /TimeToDie?/
            /            \
         [是]            [否]
           |               |
           v               v
  +----------------+  +---------------------+
  | Abandon(任务)   |  |  加锁 SynchQueue     |
  | 返回            |  +---------------------+
  +----------------+           |
                               v
                  +----------------------------+
                  | 获取空闲线程数               |
                  | AvailableThreadCount        |
                  |   = QueuedThreads.Num()     |
                  +----------------------------+
                               |
                               v
                    /CpuState == CPU_Normal?/
                   /                        \
                [是]                      [否 CPU_Busy]
                  |                          |
                  v                          v
     /AvailableThreadCount/     /AvailableThreadCount/
     /<= LimitThreadToUsed?/    /<= LimitThreadToUsedWhenBusy?/
        /        \                  /        \
    [是 受限]  [否 有余量]      [是 受限]  [否 有余量]
       |          |                |          |
       v          |                |          |
  +------------------+             |          |
  | QueuedWork       |<------------+          |
  | .Enqueue(任务     |                        |
  |   入队等待)       |                        |
  +------------------+                        |
                                              v
                              +-----------------------------+
                              | 取最后一个空闲线程 (LIFO)     |
                              +-----------------------------+
                                              |
                                              v
                              +-----------------------------+
                              | 从 QueuedThreads 移除该线程  |
                              +-----------------------------+
                                              |
                                              v
                              +-----------------------------+
                              | Thread->DoWork(InQueuedWork)|
                              +-----------------------------+
```

**关键设计思想**：
- **LIFO 取线程策略**：从数组末尾取线程，最近使用的线程 CPU cache 更热，性能更好（类似 Windows IOCP 的调度策略）
- **双模式限流**：根据 `CpuState` 选择不同的线程数量阈值，CPU 繁忙时使用更严格的 `LimitThreadToUsedWhenBusy`，从而主动降低并发度
- 当无可用线程（或受限流限制）时，任务被放入 `QueuedWork` 优先级队列排队等待

#### 2.3.3 `ReturnToPoolOrGetNextJob()` — 线程归还/继续工作

当 `FQueuedThread` 完成当前任务后，调用此方法：

```
  +-------------------------+
  |    线程完成当前任务       |
  +-------------------------+
              |
              v
  +-------------------------+
  | ReturnToPoolOrGetNextJob|
  +-------------------------+
              |
              v
  +-------------------------+
  |    加锁 SynchQueue      |
  +-------------------------+
              |
              v
    /QueuedWork 中有待执行任务?/
       /              \
    [是]              [否]
      |                 |
      v                 v
  +------------------+ +-------------------------+
  | Dequeue 取出     | | 将线程加回              |
  | 最高优先级任务    | | QueuedThreads 空闲列表  |
  | 返回给线程       | | 线程进入等待状态         |
  | 继续执行         | +-------------------------+
  +------------------+
```

实现了 **任务的连续消费** —— 线程完成一个任务后立即检查队列，有新任务就继续执行，没有才真正回到空闲状态。

#### 2.3.4 `Destroy()` — 线程池优雅销毁

1. 设置 `TimeToDie = true`
2. 对所有未执行的排队任务调用 `Abandon()`（通知任务被放弃）
3. **自旋等待**所有正在执行任务的线程完成并返回空闲池（`AllThreads.Num() == QueuedThreads.Num()`）
4. 逐个 `KillThread()` + `delete` 释放线程
5. 释放临界区

---

## 3. MarkBusy / MarkNormal 机制

### 3.1 方法定义

这两个方法定义在 `QueuedThreadPool.h` 的 `FQueuedThreadPool` 基类中（`#pragma region Engine ZXB` 区域），实现在 `ThreadingBase.cpp` 的 `FQueuedThreadPoolBase` 中：

```cpp
// MarkBusy() — 将线程池标记为 CPU 繁忙状态
virtual void MarkBusy() override
{
    FScopeLock sl(SynchQueue);
    CpuState = EThreadPoolCpuStateType::CPU_Busy;
}

// MarkNormal() — 将线程池恢复为 CPU 正常状态
virtual void MarkNormal() override
{
    FScopeLock sl(SynchQueue);
    CpuState = EThreadPoolCpuStateType::CPU_Normal;
}
```

### 3.2 核心影响：动态限流

在 `AddQueuedWork` 方法中，`CpuState` 直接影响线程池的并发度控制：

```cpp
if (GetCpuState() == EThreadPoolCpuStateType::CPU_Normal)
{
    if (AvailableThreadCount <= LimitThreadToUsed)
    {
        // 正常状态：使用普通线程限制
        QueuedWork.Enqueue(InQueuedWork, InQueuedWorkPriority);
        return;
    }
}
else  // CPU_Busy
{
    if (AvailableThreadCount <= LimitThreadToUsedWhenBusy)
    {
        // 繁忙状态：使用更严格的线程限制
        QueuedWork.Enqueue(InQueuedWork, InQueuedWorkPriority);
        return;
    }
}
```

| 方法 | 作用 | 效果 |
|------|------|------|
| `MarkBusy()` | 标记线程池为 CPU 繁忙状态 | 使用更严格的线程数量上限（`LimitThreadToUsedWhenBusy`），**减少**并发度，降低 CPU 压力 |
| `MarkNormal()` | 标记线程池为 CPU 正常状态 | 使用正常的线程数量上限（`LimitThreadToUsed`），**恢复**正常并发能力 |

### 3.3 在 WindowsCPUUsageTrack 中的应用

`WindowsCPUUsageTrack.cpp` 是项目自定义的 CPU 使用率监控系统，每秒查询一次 CPU 使用率，当检测到 CPU 负载超过阈值时，触发一系列降压措施：

```
  +-----------------------------------+
  | CPU使用率定时器 (每秒触发)          |
  +-----------------------------------+
                  |
                  v
  +-----------------------------------+
  |        查询 CPU 使用率             |
  +-----------------------------------+
                  |
                  v
       /CPU 使用率 > GCPULimitPercent?/
          /                  \
       [是]                  [否]
         |                     |
         v                     v
  +-----------------+   +--------------------+
  | OnCpuStateBusy()|   | OnCpuStateRelaxed()|
  +-----------------+   +--------------------+
         |                     |
         |-- GIOThreadPool     |-- GIOThreadPool
         |   ->MarkBusy()      |   ->MarkNormal()
         |                     |
         |-- 限制 TaskGraph    |-- 恢复 TaskGraph
         |   前台 Worker 数    |   前台 Worker 数
         |                     |
         |-- 限制 TaskGraph    |-- 恢复 TaskGraph
         |   后台 Worker 数    |   后台 Worker 数
         |                     |
         |-- 降低纹理流送速度   |-- 恢复纹理流送速度
         |                     |
         |-- 缩减 LuxGI        |-- 恢复 LuxGI
         |   加载范围           |   加载范围
         |                     |
         |-- 降低 LuxGI        |-- 恢复 LuxGI
         |   摊还预算           |   摊还预算
         |                     |
         +-- FPS 平滑降低      +-- FPS 平滑恢复
```

**CPU 繁忙时的完整降压措施列表**：

| 措施 | 控制变量/方法 | 说明 |
|------|-------------|------|
| IO 线程池限流 | `GIOThreadPool->MarkBusy()` | 减少 IO 并发度 |
| TaskGraph 前台 Worker 限制 | `TaskGraph.MaxActiveForegroundWorkers` | 减少到 `GMaxForegroundWorkerCount`（默认1） |
| TaskGraph 后台 Worker 限制 | `TaskGraph.MaxActiveBackgroundWorkers` | 减少到 `GMaxBackgroundWorkerCount`（默认2） |
| 纹理流送限制 | `r.Streaming.MaxNumTexturesToStreamPerFrame` | 降低到每帧5张 |
| LuxGI 加载范围限制 | `wp.Runtime.LoadingRangeScale.Group1/Group3` | 缩小加载范围 |
| LuxGI 摊还预算缩减 | `r.LuxGI.Amortization.Budget` | 按 `s_BusyCPULuxGIAmortizeBudgetScale`（0.5）缩减 |
| FPS 上限平滑调整 | `GMaxFPSToLimitCPUUsage` | 平滑降低到 `GCPUBusyTargetFPS`（默认50） |

**启用条件**：`t.CPUUsageTrack.Enable = true` 且 CPU 核心数 ≤ `CPULowCoreNum`（默认8核）。

---

## 4. GBackgroundPriorityThreadPool 详解

### 4.1 创建参数

`GBackgroundPriorityThreadPool` 在 `LaunchEngineLoop.cpp` 中创建：

```cpp
GBackgroundPriorityThreadPool = FQueuedThreadPool::Allocate();
int32 NumThreadsInThreadPool = 2;  // 默认只有2个线程
if (FPlatformProperties::IsServerOnly())
{
    NumThreadsInThreadPool = 1;     // DS服务器只给1个
}
verify(GBackgroundPriorityThreadPool->Create(
    NumThreadsInThreadPool, 
    StackSize, 
    TPri_Lowest,                    // OS级最低优先级
    TEXT("BackgroundThreadPool")
));
```

| 参数 | 值 | 含义 |
|------|------|------|
| 线程数 | **2**（DS为1） | 极少的线程数，限制并发 |
| OS线程优先级 | **`TPri_Lowest`** | 操作系统级别**最低优先级** |
| 名称 | `"BackgroundThreadPool"` | 后台线程池 |

### 4.2 使用场景

| 使用场景 | 文件 | 说明 |
|----------|------|------|
| 音频解码 | `AudioMixerSourceDecode.cpp` | 音频 source 解码任务 |
| 音频解压 | `AudioDecompress.h` | 音频解压缩任务 |
| 纹理流送 | `StreamingManagerTexture.cpp` | 纹理 Mipmap 异步加载 |
| Shader预加载 | `ShaderCodeArchive.cpp` | Shader 代码的后台预加载和编译 |

共同特征：**不紧急、可以慢慢做、不应该抢占主要游戏逻辑的 CPU 资源**。

### 4.3 为什么不放在 TaskGraph 内

#### 原因一：调度模型根本不同

- **TaskGraph**：基于依赖关系的 **DAG 任务图调度器**，任务之间可声明前置依赖，调度器自动拓扑排序后分派到命名线程或 AnyThread Worker。适合帧内需要快速完成的并行计算（物理、动画、渲染命令生成等）。
- **GBackgroundPriorityThreadPool**：简单的 **优先级队列 + 工作线程** 模型。任务之间没有依赖关系，纯粹"丢进去就跑"。

#### 原因二：OS 线程优先级隔离（最关键）

| 系统 | 线程优先级 | OS 调度行为 |
|------|-----------|------------|
| TaskGraph Worker | `TPri_Normal` 或 `TPri_SlightlyBelowNormal` | OS 积极调度，保证帧内任务及时完成 |
| `GBackgroundPriorityThreadPool` | **`TPri_Lowest`** | OS 只在 CPU **空闲时**才调度 |

如果把后台任务放进 TaskGraph，它们会和物理模拟、动画计算等帧关键任务 **共享同一批 Worker 线程**，在 CPU 繁忙时互相竞争：
- 帧内关键任务被延迟 → **卡帧**
- 后台任务抢到 Worker 却不紧急 → **浪费**

用 `TPri_Lowest` 的独立线程池，OS 调度器会 **自动让出 CPU 给更高优先级的线程**，实现天然的优先级隔离。

#### 原因三：线程数量控制

TaskGraph Worker 数量通常接近 CPU 核心数，而 `GBackgroundPriorityThreadPool` **固定只有 2 个线程**。后台任务不需要也不应该占用太多 CPU 核心，2 个线程足够让后台任务缓慢推进。

#### 原因四：TaskGraph 不支持 OS 级混合优先级

TaskGraph 的所有 AnyThread Worker 共享 **同一个 OS 线程优先级**。它没有机制让某些 Task 运行在 `TPri_Lowest` 而另一些运行在 `TPri_Normal`。虽然 TaskGraph 支持 `ENamedThreads::AnyBackgroundThreadNormalTask` 等 hint，但这只是软优先级（调度顺序），不影响 OS 调度。

```
  +---------------------------+    TaskGraph                +----------------+
  | 帧关键任务                 |    Normal优先级             |                |
  | (物理/动画/渲染)           | -------- N个Worker ------> | 低延迟完成      |
  +---------------------------+                             +----------------+

  +---------------------------+    GBackgroundPriority-     +----------------+
  | 后台低优任务               |    ThreadPool              |                |
  | (音频解码/纹理流送/        |    Lowest优先级            | CPU空闲时       |
  |  Shader预编译)             | -------- 仅2线程 --------> | 缓慢推进        |
  +---------------------------+                             +----------------+
```

---

## 5. 全局线程池对比总结

| 线程池 | OS线程优先级 | 线程数 | 典型用途 | 特点 |
|--------|------------|--------|---------|------|
| **GThreadPool** | `TPri_Normal` | 较多（接近核心数-2） | 通用异步任务（AsyncTask等） | 标准并发，及时响应 |
| **GIOThreadPool** | `TPri_Normal` | 较多 | IO 密集型任务 | 支持 MarkBusy/MarkNormal 动态限流 |
| **GBackgroundPriorityThreadPool** | `TPri_Lowest` | **仅2个** | 音频解码、纹理流送、Shader预编译 | OS级最低优先级，不抢占前台资源 |
| **GLargeThreadPool** (Editor) | `TPri_Normal` | 较多 | 编辑器专用大任务 | 仅编辑器模式存在 |
| **TaskGraph Workers** | `TPri_Normal` / `TPri_SlightlyBelowNormal` | 接近CPU核心数 | 帧内并行计算（物理/动画/渲染等） | DAG依赖调度，支持命名线程 |

### 线程池工作模型

```
  +------------------+
  |     调用方        |       +================================================+
  |------------------|       |            FQueuedThreadPoolBase                |
  | Work 1 (High)    |       |================================================|
  | Work 2 (Normal)  | ----> |  +------------------------------------------+  |
  | Work 3 (Low)     |       |  |              限流判断                     |  |
  +------------------+       |  | Normal模式: LimitThreadToUsed             |  |
                             |  | Busy模式:   LimitThreadToUsedWhenBusy    |  |
                             |  +------------------------------------------+  |
                             |        |                       |                |
                             |   有空闲线程              无空闲线程            |
                             |   且未限流                或被限流              |
                             |        |                       |                |
                             |        v                       v                |
                             |  +--------------+  +------------------------+  |
                             |  | 线程管理      |  |     QueuedWork         |  |
                             |  |--------------|  |     (优先级队列)        |  |
                             |  | Thread 1 (忙)|  | Blocking > Highest >   |  |
                             |  | Thread 2 (闲)| <| High > Normal >       |  |
                             |  | Thread 3 (闲)|  | Low > Lowest           |  |
                             |  +--------------+  +------------------------+  |
                             |        ^                       |                |
                             |        |   线程完成任务后       |                |
                             |        +-- ReturnToPool -------+                |
                             |            OrGetNextJob()                       |
                             +================================================+
```

### CPU 使用率监控完整架构

```
  +------------------------------------------+
  | FWindowsCPUUsageTrack (每秒定时器)         |
  +------------------------------------------+
                      |
                      v
  +------------------------------------------+
  | 查询 CPU 使用率                            |
  | FPlatformMisc::GetCpuUsage()              |
  +------------------------------------------+
                      |
                      v
           /CPU 使用率 > 阈值? (默认70%)/
              /                  \
     [是 → CPU_Busy]      [否 → CPU_Relaxed]
             |                     |
             v                     v
  +--------------------+  +----------------------+
  | OnCpuStateBusy()   |  | OnCpuStateRelaxed()  |
  |--------------------|  |----------------------|
  | - GIOThreadPool    |  | - GIOThreadPool      |
  |   ->MarkBusy()     |  |   ->MarkNormal()     |
  | - 限制 TaskGraph   |  | - 恢复 TaskGraph     |
  |   Workers          |  |   Workers            |
  | - 降低纹理流送     |  | - 恢复纹理流送       |
  |   /LuxGI           |  |   /LuxGI             |
  | - 平滑降低         |  | - 平滑恢复           |
  |   FPS 上限         |  |   FPS 上限           |
  +--------------------+  +----------------------+
                                   |
                                   v
                        /Relaxed 持续时间/
                        /> 阈值? (默认10秒)/
                          /            \
                       [是]            [否]
                         |               |
                         v               v
              +------------------+ +------------------+
              | 完全解除所有限制  | | 保持当前限制      |
              | bIsCPUBalancing  | | 继续监控          |
              |   = false        | +------------------+
              +------------------+
```

---

> **核心设计思想总结**：UE5 通过将线程池按 OS 优先级和用途分层（`GThreadPool` / `GIOThreadPool` / `GBackgroundPriorityThreadPool`），配合 `FQueuedThreadPoolBase` 的双模式限流机制（`MarkBusy` / `MarkNormal`）和项目级 CPU 使用率监控系统（`WindowsCPUUsageTrack`），实现了从 OS 调度层到应用层的多级 CPU 资源管控，确保在 CPU 高负载场景下游戏帧率的稳定性。
