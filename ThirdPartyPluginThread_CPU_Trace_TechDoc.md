# 第三方插件非注册线程 CPU 耗时 Trace 链路改造技术文档

> 文档版本：v1.0  
> 适用工程：`D:/GR_MergeTest/UE5EA`（UE5 Early Access 基础）  
> 所有改动均通过 `#pragma region Engine ZXB` / `#pragma endregion` 包裹，便于差异回溯与维护。

---

## 目录

1. [改造目标](#1-改造目标)
2. [问题背景与关键矛盾](#2-问题背景与关键矛盾)
3. [总体架构](#3-总体架构)
4. [端到端数据流](#4-端到端数据流)
5. [文件与改动清单](#5-文件与改动清单)
6. [关键设计决策与权衡](#6-关键设计决策与权衡)
7. [场景矩阵（验证口径）](#7-场景矩阵验证口径)
8. [已知局限与容量评估](#8-已知局限与容量评估)
9. [回归测试清单](#9-回归测试清单)

---

## 1. 改造目标

在启用 ContextSwitch 录制的前提下，让 **UnrealInsights** 能够正确记录并在 **CPU Core 视图**中显示第三方插件创建的"非 UE Trace 注册线程"的 CPU 耗时，并尽可能显示线程真名（而非仅 `Process Thread <tid>`）。

两个核心验收点：

- **C1**：从游戏启动时开启 trace 录制，ETW ContextSwitch 能捕获目标进程**所有线程**（注册 + 非注册）的 CPU 耗时事件。
- **C2**：UnrealInsights 在 CPU Core 视图中能正确区分并显示：
  - UE 注册线程：显示注册线程名（如 `GameThread`）；
  - Trace 内部线程：显示 `Trace Thread <tid>`；
  - 本进程未注册线程（第三方插件）：优先显示 `GetThreadDescription` 拿到的真实名字，拿不到时显示 `Process Thread <tid>`；
  - 其他进程线程：显示进程 exe 路径，默认半透明、可开关。
- **C3**：支持 **late-joining** 场景（玩家先启动游戏，10 分钟后才连 UnrealInsights），上述分类与命名仍然全部可见。

---

## 2. 问题背景与关键矛盾

### 2.1 ETW ContextSwitch 是无差别内核采集

ETW `EVENT_TRACE_FLAG_CSWITCH` 在内核层无差别上报**所有核上、所有进程**的上下文切换，天然包含第三方插件线程。采集能力本身没问题。

### 2.2 UE Trace 的 ThreadId ≠ OS 的 SystemThreadId

- UE Trace 内部 ThreadId 由 `Writer_GetThreadId()` 基于 `ETransportTid::Bias(=2)` 累加分配，只有调用过 trace 宏的线程才会有。
- ETW 上报的 ThreadId 是 OS 级的 `SystemThreadId`（`GetCurrentThreadId()`/`gettid()`/`pthread_mach_thread_np()`）。
- 原生 UE 只对自己注册的线程建立 `(TraceThreadId, SystemId, Name)` 三元组，**第三方插件线程从不出现在 `$Trace.ThreadInfo` 里**，UnrealInsights 因此无法把 CSwitch 事件归类到目标进程。

### 2.3 Late-joining 对"名字与进程归属"的严苛要求

UE Trace 的事件分两类：
- `NoSync`：流式，进入普通 Ring Buffer，**未被消费就会被覆盖丢弃**；
- `NoSync|Important`：进入 Important Cache，**全量保留**，连接时全部重放。

游戏运行 10 分钟后才连 UnrealInsights，历史事件已经滚掉，**线程名与进程归属必须存成 Important** 才能在 late-joining 时重建状态。

---

## 3. 总体架构

```
                 ┌─────────────────────────────────────────────┐
                 │                   游戏进程                     │
                 │                                             │
                 │  ┌─────────────────────────────────────┐    │
                 │  │  ETW Kernel Callback (CSwitch/Thread)│    │
                 │  │                                      │    │
                 │  │  慢路径：OpenThread + GetProcessId    │    │
                 │  │         + GetThreadDescription       │    │
                 │  │         + ThreadProcessSet 去重      │    │
                 │  │         + ThreadNameSet 去重         │    │
                 │  └──────────┬───────────────┬──────────┘    │
                 │             │               │               │
                 │    OutputContextSwitch  OutputThreadProcess │
                 │    OutputThreadName     OutputTargetProcessId│
                 │             │               │               │
                 └─────────────┼───────────────┼───────────────┘
                               ▼               ▼
                   ┌────────────────────────────────────┐
                   │     UE Trace Stream                 │
                   │  ┌─────────────┐ ┌───────────────┐ │
                   │  │ NoSync      │ │ NoSync|Important││
                   │  │ ContextSwitch│ │ ThreadName    │ │
                   │  │ StackSample │ │ ThreadProcess │ │
                   │  │             │ │ TargetProcessId││
                   │  │             │ │ $Trace.ThreadInfo││
                   │  └─────────────┘ └───────────────┘ │
                   └───────────┬────────────────────────┘
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │                    UnrealInsights (Analyzer)                   │
   │                                                               │
   │   FPlatformEventTraceAnalyzer 路由事件                         │
   │     → FContextSwitchesProvider                                 │
   │         • Add(SystemThreadId, ...)                             │
   │         • AddThreadProcessMapping  ─► SystemThreadIdToProcessId│
   │         • AddThreadName            ─► SystemThreadIdToNameMap  │
   │         • SetTargetProcessId                                   │
   │         • MarkAsTraceInternalThread                            │
   │                                                               │
   │   查询接口:                                                    │
   │     • GetThreadId(SystemThreadId)      // 注册线程             │
   │     • IsTraceThread(SystemThreadId)                            │
   │     • IsProcessThread(SystemThreadId)                          │
   │     • GetThreadName(SystemThreadId)    // 反查拿到的真名        │
   └───────────┬───────────────────────────────────────────────────┘
               ▼
   ┌─────────────────────────────────────────────────┐
   │  CPU Core View (CpuCoreTimingTrack)              │
   │    GetThreadId → 注册线程名（ThreadProvider）      │
   │    IsTraceThread → "Trace Thread <tid>"          │
   │    GetThreadName → 第三方插件真名                  │
   │    IsProcessThread → "Process Thread <tid>"      │
   │    其他 → exe 路径（半透明）                        │
   └─────────────────────────────────────────────────┘
```

---

## 4. 端到端数据流

### 4.1 Live 场景（游戏与 UI 同时在线）

```
游戏线程每次 context switch
   │
   ├─► OutputContextSwitch(SystemTid, Core, Start, End, ...)              [NoSync]
   │
   └─► 慢路径（tid 首次出现时）:
          OpenThread → GetProcessIdOfThread → GetThreadDescription
          │
          ├─► 拿到 desc 且非空:
          │     OutputThreadName(SystemTid, Pid, desc, len)               [Important]
          │     ThreadNameSet.Add(SystemTid)
          │
          ├─► OutputThreadProcess(SystemTid, Pid)                         [Important]
          │     ThreadProcessSet.Add(SystemTid)
          │
          └─► 非本进程且没有 desc:
                OutputThreadName(SystemTid, Pid, exe 路径, len)             [Important]

游戏启动 ETW 时一次性发送:
   OutputTargetProcessId(GetCurrentProcessId())                            [Important]

游戏 Thread Start/DCStart:
   OutputThreadProcess（对本进程启动前已存在线程的 rundown）                  [Important]

游戏 Thread End/DCEnd:
   ThreadProcessSet.Remove(tid)  // 释放去重位，允许 tid 被复用时再上报
   ThreadNameSet.Remove(tid)
```

### 4.2 Late-joining 场景

```
玩家运行游戏 10 分钟 → 打开 UnrealInsights → 连上 trace 端点
   │
   ├─► Important Cache 全量重放 (Writer_CacheOnConnect)
   │     • 所有 ThreadProcess 条目 ─► SystemThreadIdToProcessIdMap
   │     • 所有 ThreadName 条目    ─► SystemThreadIdToNameMap
   │     • 所有 $Trace.ThreadInfo   ─► TraceToSystemThreadIdMap + MarkTrace
   │     • TargetProcessId         ─► Provider::TargetProcessId
   │
   └─► 之后的普通 CSwitch 事件继续流式分析
```

**关键点**：`ThreadName` 必须是 Important（本次改造修正），否则 late-joining 时插件线程名全丢，只能显示 `Process Thread <tid>`。

---

## 5. 文件与改动清单

本次改造共修改 **10 个文件**，按职责分层：

### 5.1 跨平台抽象层（TraceLog 模块）

#### (1) `Runtime/TraceLog/Private/Trace/Platform.h`
- **改动**：新增跨平台函数声明 `uint32 ThreadGetCurrentSystemId();`
- **目的**：供 UE Trace 自身 Worker 线程注册时传递真实 SystemId，帮助分析端识别 Trace 内部线程。

#### (2) `Runtime/TraceLog/Private/Trace/Detail/Windows/WindowsTrace.cpp`
```cpp
#pragma region Engine ZXB
uint32 ThreadGetCurrentSystemId()
{
    return ::GetCurrentThreadId();
}
#pragma endregion
```

#### (3) `Runtime/TraceLog/Private/Trace/Detail/Android/AndroidTrace.cpp`
```cpp
#pragma region Engine ZXB
uint32 ThreadGetCurrentSystemId()
{
    return static_cast<uint32>(gettid());
}
#pragma endregion
```

#### (4) `Runtime/TraceLog/Private/Trace/Detail/Apple/AppleTrace.cpp`
```cpp
#pragma region Engine ZXB
uint32 ThreadGetCurrentSystemId()
{
    return static_cast<uint32>(pthread_mach_thread_np(pthread_self()));
}
#pragma endregion
```

#### (5) `Runtime/TraceLog/Private/Trace/Detail/Unix/UnixTrace.cpp`
```cpp
#pragma region Engine ZXB
uint32 ThreadGetCurrentSystemId()
{
#if defined(_GNU_SOURCE)
    return static_cast<uint32>(syscall(SYS_gettid));
#else
    return static_cast<uint32>(pthread_self()); // 回退
#endif
}
#pragma endregion
```

> **为什么 Android/Apple/Unix 也要改？**  
> 该函数声明在跨平台头 `Platform.h`，并在跨平台 `Writer.cpp:725` 有调用（`ThreadRegister(TEXT("Trace"), ThreadGetCurrentSystemId(), INT_MAX);`）。缺任一实现会直接链接失败。虽然 ContextSwitch 只在 Windows 使用，但实现必须全平台对齐。

#### (6) `Runtime/TraceLog/Private/Trace/Trace.cpp`
- 新增 **已注册线程 SystemId 集合**（Game 端，无锁查询，CAS 发布）：
  ```cpp
  bool IsRegisteredThread(uint32 SystemId);         // 查询
  static void AddRegisteredThread(uint32 SystemId); // 注册入口
  ```
- 在 `ThreadRegister()` 中调用 `AddRegisteredThread(SystemId)`，使 UE 侧能在不引入锁的前提下判断某 OS tid 是否已由 UE Trace 注册（后来在 ETW 热路径中被去除调用，但该能力本身保留以供将来使用）。
- 使用 Release/Acquire 内存序配合 CAS 循环：先写 `GRegisteredThreadSystemIds[Count]`，再 `AtomicCompareExchangeRelease` 推进 `Count`；读端先 `AtomicLoadAcquire(Count)` 再线性扫描。

---

### 5.2 Trace 事件定义层（Runtime/Core）

#### (7) `Runtime/Core/Public/ProfilingDebugging/PlatformEvents.h`

新增公开静态方法：
```cpp
#pragma region Engine ZXB
CORE_API static void OutputThreadProcess(uint32 SystemThreadId, uint32 ProcessId);
CORE_API static void OutputTargetProcessId(uint32 ProcessId);
#pragma endregion
```
两个分支都要（`PLATFORM_SUPPORTS_PLATFORM_EVENTS` 开关内和空实现分支），保证非 Windows 平台编译链接正常。

#### (8) `Runtime/Core/Private/ProfilingDebugging/PlatformEvents.cpp`

**新增 trace 事件定义**：
```cpp
#pragma region Engine ZXB
UE_TRACE_EVENT_BEGIN(PlatformEvent, ThreadProcess, NoSync|Important)
UE_TRACE_EVENT_FIELD(uint32, SystemThreadId)
UE_TRACE_EVENT_FIELD(uint32, ProcessId)
UE_TRACE_EVENT_END()

UE_TRACE_EVENT_BEGIN(PlatformEvent, TargetProcessId, NoSync|Important)
UE_TRACE_EVENT_FIELD(uint32, ProcessId)
UE_TRACE_EVENT_END()
#pragma endregion
```

**修改既有 `ThreadName` 事件为 Important**（本次最后一轮关键修复）：
```cpp
#pragma region Engine ZXB
// late-joining 场景下若无 Important 则线程名全丢。
// 游戏端 ThreadNameSet + End/DCEnd 清理保证每个 tid 只上报一次，空间可控。
UE_TRACE_EVENT_BEGIN(PlatformEvent, ThreadName, NoSync|Important)
#pragma endregion
UE_TRACE_EVENT_FIELD(uint32, ThreadId)
UE_TRACE_EVENT_FIELD(uint32, ProcessId)
UE_TRACE_EVENT_FIELD(UE::Trace::WideString, Name)
UE_TRACE_EVENT_END()
```

**新增两个 `Output*` 实现**：`OutputThreadProcess` / `OutputTargetProcessId`，通过 `UE_TRACE_LOG(PlatformEvent, XXX, ContextSwitchChannel)` 发送。

---

### 5.3 Windows ETW 采集层

#### (9) `Runtime/Core/Private/ProfilingDebugging/Microsoft/EventTracingForWindows.cpp`

本次主要改动密集文件，按粒度总结如下：

##### a. Thread 事件 GUID 与结构体
- 新增 `ETW_ThreadEventGuid`（`{3d6fa8d1-fe05-11d0-9dda-00c04fd7ba7c}`），用于在回调里精确区分 Thread 事件与 Process 事件（它们 Opcode 重叠）。
- 新增 `FThreadTypeGroup1Event` 结构体声明，只解析到 `StackBase`（和内核 rundown payload 对齐）。

##### b. 去重集合与类型修正
```cpp
#pragma region Engine ZXB
TSet<uint32> ThreadProcessSet;   // 原 TSherwoodSet 不支持 Remove，改为 TSet
TSet<uint32> ThreadNameSet;      // 同上
#pragma endregion
```

##### c. `Enable()` 路径修复（两处关键时序）

- **EnumAddFlags 前置**：先 `EnumAddFlags(EnabledEvents, Event)` 再 `FRunnableThread::Create`，避免工作线程读到旧的 `EnabledEvents`（竞态）。
- **UPDATE 路径补 Thread flag**：`ControlTraceW(UPDATE)` 分支也要加 `EVENT_TRACE_FLAG_THREAD`，与首次 `StartTraceW` 路径对称。

##### d. `StartETW()` 增强
- `Properties->EnableFlags` 增加 `EVENT_TRACE_FLAG_THREAD`（取得 Thread Start/DCStart rundown）。
- 成功打开 trace handle、进入 `ProcessTrace` 循环之前，调用一次 `FPlatformEventsTrace::OutputTargetProcessId(CurrentProcessId)`（必须此处：ContextSwitchChannel 启用之后才能正确记录）。

##### e. CSwitch 回调慢路径（核心改动）
```cpp
// 双重去重：process 映射 + name 上报 各自独立判断
bool bProcessAlreadySent = ThreadProcessSet.Contains(ThreadId);
bool bNameAlreadySent    = ThreadNameSet.Contains(ThreadId);
if (!bProcessAlreadySent || !bNameAlreadySent)
{
    HANDLE ThreadHandle = ::OpenThread(THREAD_QUERY_LIMITED_INFORMATION, false, ThreadId);
    if (ThreadHandle)
    {
        ProcessId = ::GetProcessIdOfThread(ThreadHandle);

        // ★ 方案 A：动态加载 GetThreadDescription（Win10 1607+）
        // ★ 反查真实线程名，成功时通过 OutputThreadName 上报
        if (!bNameAlreadySent)
        {
            using Fn = HRESULT(WINAPI*)(HANDLE, PWSTR*);
            static Fn PtrGetThreadDescription = []{
                HMODULE K = ::GetModuleHandleW(L"kernel32.dll");
                return K ? (Fn)::GetProcAddress(K, "GetThreadDescription") : nullptr;
            }();
            if (PtrGetThreadDescription)
            {
                PWSTR DescPtr = nullptr;
                if (SUCCEEDED(PtrGetThreadDescription(ThreadHandle, &DescPtr)) && DescPtr)
                {
                    if (DescPtr[0] != L'\0')
                    {
                        uint32 ReportPid = ProcessId ? ProcessId : CurrentProcessId;
                        FPlatformEventsTrace::OutputThreadName(ThreadId, ReportPid,
                            DescPtr, (uint32)::wcslen(DescPtr));
                        bThreadDescNameSent = true;
                    }
                    ::LocalFree(DescPtr);
                }
            }
        }

        ::CloseHandle(ThreadHandle);
    }

    // ProcessId 有效且未上报过 → 发送 ThreadProcess，加入去重集合
    if (ProcessId != 0 && !bProcessAlreadySent) { ... OutputThreadProcess ... }

    // 名字上报优先级：
    //   1) 已通过 GetThreadDescription 上报 → 加入 ThreadNameSet
    //   2) 非本进程线程 → 上报 exe 路径（原生行为）
    //   3) 其他（本进程无 desc、OpenThread 失败）→ 仅加入去重集合，避免反复走慢路径
}
```

##### f. Thread Start/DCStart/End/DCEnd 回调分支（新增）
```cpp
// Start/DCStart：用内核级 ProcessId 发送 OutputThreadProcess（比 GetProcessIdOfThread 更及时）
// End/DCEnd：从两个去重集合中 Remove tid，保证 tid 复用时新映射能重新发送
```

##### g. 热路径上已**移除**的早期错误改动
- 早期版本在 CSwitch 热路径调用 `UE::Trace::IsRegisteredThread + UE_LOG` → 每秒上百万次线性扫描 + 同步 I/O，严重退化。**已彻底删除**，改由分析端完成 registered vs unregistered 判定。

##### h. `Stop()` 清理
```cpp
ThreadNameSet.Empty();
ThreadProcessSet.Empty();
```

---

### 5.4 UnrealInsights 分析端（TraceServices）

#### (10) `Developer/TraceServices/Public/TraceServices/Model/ContextSwitches.h`

公共接口扩展，新增 3 个纯虚方法：
```cpp
#pragma region Engine ZXB
virtual bool IsProcessThread(uint32 SystemThreadId) const = 0;
virtual bool IsTraceThread(uint32 SystemThreadId) const = 0;
virtual bool GetThreadName(uint32 SystemThreadId, FString& OutName) const = 0;
#pragma endregion
```
同时加 `#include "Containers/UnrealString.h"` 以引入 `FString`。

#### (11) `Developer/TraceServices/Private/Model/ContextSwitchesPrivate.h`

`FContextSwitchesProvider` 新增 override 声明与三张内部表：
```cpp
#pragma region Engine ZXB
void AddThreadProcessMapping(uint32 SystemThreadId, uint32 ProcessId);
void SetTargetProcessId(uint32 ProcessId);
void MarkAsTraceInternalThread(uint32 SystemThreadId);
bool IsProcessThread(uint32 SystemThreadId) const override;
bool IsTraceThread(uint32 SystemThreadId) const override;
bool GetThreadName(uint32 SystemThreadId, FString& OutName) const override;

// ...

TMap<uint32, uint32>   SystemThreadIdToProcessIdMap;
uint32                 TargetProcessId = 0;
TSet<uint32>           TraceInternalSystemThreadIds;
TMap<uint32, FString>  SystemThreadIdToNameMap;
#pragma endregion
```

#### (12) `Developer/TraceServices/Private/Model/ContextSwitches.cpp`

- `AddThreadName` 从 `//TODO` 空实现改为真正写入 `SystemThreadIdToNameMap`，后写覆盖前写（tid 复用场景）。
- 新增 `AddThreadProcessMapping / SetTargetProcessId / MarkAsTraceInternalThread` 写入。
- 新增 `IsProcessThread / IsTraceThread / GetThreadName` 查询实现（`ReadAccessCheck`，无锁 map 查询）。

#### (13) `Developer/TraceServices/Private/Analyzers/PlatformEventTraceAnalysis.h/.cpp`

路由新增两个 RouteId：
```cpp
#pragma region Engine ZXB
RouteId_ThreadProcess,
RouteId_TargetProcessId,
#pragma endregion
```

`OnAnalysisBegin` 中 `RouteEvent` 绑定，`OnEvent` 分发到 Provider 的对应写入方法。

`OnThreadInfo` 中通过线程名 `"Trace"` 精确识别 Trace Worker，调用 `MarkAsTraceInternalThread`：
```cpp
#pragma region Engine ZXB
if (ThreadInfo.GetSystemId() != 0 &&
    FCStringAnsi::Strcmp(ThreadInfo.GetName(), "Trace") == 0)
{
    ContextSwitchesProvider.MarkAsTraceInternalThread(ThreadInfo.GetSystemId());
}
#pragma endregion
```

---

### 5.5 UnrealInsights UI 层（TraceInsights）

#### (14) `Developer/TraceInsights/Private/Insights/ContextSwitches/ViewModels/ContextSwitchesSharedState.h/.cpp`

- 新增成员 `bAreTraceThreadEventsVisible`（默认 `false`）、`bAreUnregisteredProcessThreadEventsVisible`（默认 `true`）。
- 新增配套 `Are*Visible / Toggle* / Set*Visible` 以及 `Command_* `。
- `GetThreadInfo` 的未注册分支增加分层 fallback：
  ```
  IsTraceThread ? "Trace Thread"
    : GetThreadName 拿到真名 ? 真名
      : IsProcessThread ? "Process Thread (Unregistered)"
        : "Unknown Thread"
  ```
  用 `static thread_local FString LocalThreadNameBuffer` 做 `const TCHAR*&` 返回缓冲，沿用既有"调用方在 Session 读锁内使用"的生命周期契约。

#### (15) `Developer/TraceInsights/Private/Insights/ContextSwitches/ViewModels/CpuCoreTimingTrack.cpp`

所有的过滤 / 绘制 / 命中 / tooltip / 右键菜单 5 个路径统一引入：
```
bIsProcessThread = Provider->IsProcessThread(SystemTid);
bIsRegistered    = Provider->GetThreadId(SystemTid, ThreadId);
bIsTargetProcess = bIsProcessThread || bIsRegistered;
```
- `BuildDrawState / BuildFilteredDrawState / GetEvent`：根据 `bShowTraceThreadEvents / bShowUnregisteredProcessThreadEvents / bShowNonTargetProcessEvents` 三种 toggle 决定绘制到主 Builder 还是 `NonTargetProcessEventsBuilder`（半透明）。
- `GetThreadName`（最终版）fallback 优先级：
  ```
  GetThreadId 成功 → ThreadProvider.GetThreadName（注册线程名）
  IsTraceThread   → "Trace Thread <tid>"
  GetThreadName 成功 → 真实线程名   ← 新增关键修复
  IsProcessThread → "Process Thread <tid>"
  其他            → "Unknown <tid>"
  ```
  > 为什么 `GetThreadName` 要优先于 `IsProcessThread`？因 `GetThreadDescription` 只能对本进程线程成功；能拿到名字本身就是更强的"归属目标进程"证据，无需依赖 `ThreadProcess` 事件到达。
- Tooltip 新增未注册线程的"Process:"行：
  ```
  IsTraceThread    → "Target Process (Trace recording thread)"
  IsProcessThread  → "Target Process (unregistered thread)"
  ```
- 右键菜单：对 `IsProcessThread` 的未注册线程仍允许部分导航动作。

---

### 5.6 其他辅助改动

- `Developer/TraceInsights/Private/Insights/Common/InsightsMenuBuilder.cpp/h`：集成 TraceThread / UnregisteredProcessThread 菜单开关。
- `Developer/TraceInsights/Private/Insights/TimingProfiler/TimingProfilerCommands.h`：新增两条 UI 命令。
- `Developer/TraceInsights/Private/Insights/Common/ThreadReportExporter.h/.cpp`：新增工具类（配套导出功能，本次链路主线外不展开）。
- `Developer/TraceInsights/Private/Insights/TimingProfiler/ViewModels/CpuUsageTrackHelper.cpp`：CPU 使用率计算区分 Target Process / Trace Thread / NonTargetProcess。

---

## 6. 关键设计决策与权衡

### 6.1 为什么不在游戏端做 "IsRegistered 判定 + 日志"？

初期尝试过在 ETW 热路径里调 `IsRegisteredThread` + `UE_LOG` 来标记第三方线程。结论是**此路不通**：
- CSwitch 频率每秒上百万次，线性扫描 + 同步 I/O 直接打爆 ETW 工作线程；
- UE 自己的线程在 `FRunnableThread::PreRun` 才调 `ThreadRegister`，Thread Start 时必然 false positive，误导诊断。

**最终方案**：游戏端只做数据上报（SystemTid ↔ Pid 映射、SystemTid ↔ Name），registered vs unregistered 的判断完全挪到分析端，利用 `SystemToTraceThreadIdMap` 与 `SystemThreadIdToProcessIdMap` 组合判定。

### 6.2 为什么 `ThreadName` 必须 Important？

`ContextSwitch` 事件是 `NoSync`（流式，量大，不适合进 Cache），但 **状态性的 metadata**（ThreadInfo / ThreadProcess / TargetProcessId / **ThreadName**）必须 Important：
- Important 事件进入 Important Cache，`Writer_CacheOnConnect` 会在 UI 每次连接时全量重放；
- `NoSync` 事件一旦被消费或被覆盖就永远消失。

游戏端通过 `ThreadNameSet` 去重 + Thread End/DCEnd Remove，保证 Important Cache 条目数 ≈ 游戏生命周期中曾经存在过的线程数（进程活跃线程通常数百~数千），空间开销可控。

### 6.3 为什么 `GetThreadDescription` 动态加载？

- 只有 Win10 1607+ 才提供；老 SDK 直接引用会链接失败、老 Windows 运行时找不到符号。
- 用 `GetModuleHandleW(L"kernel32.dll")`（不增加引用计数，不 FreeLibrary）+ `GetProcAddress` + `static lambda init`（C++11 magic static）做一次性解析并缓存。
- 拿不到就整段跳过，完全退化为原生"非本进程上报 exe 路径 / 本进程不上报名字" 的行为，**不破坏原生逻辑**。

### 6.4 为什么 `OpenThread` → `GetProcessIdOfThread` → `GetThreadDescription` → `CloseHandle` 连在一起？

- `GetThreadDescription` 需要有效 `HANDLE` 和 `THREAD_QUERY_LIMITED_INFORMATION` 权限；
- 原生代码在 `GetProcessIdOfThread` 之后立刻 `CloseHandle`，本次改造把 `CloseHandle` 延后到 `GetThreadDescription` 完成之后，句柄生命周期一致。

### 6.5 为什么 `EnumAddFlags` 必须在 `Create` 之前？

原代码是 `Create` 之后再 `EnumAddFlags`。工作线程在 Thread Run 入口第一时间调 `StartETW` 读取 `EnabledEvents`，可能读到**旧值**，导致首次启动时 `EVENT_TRACE_FLAG_CSWITCH` 没被设置上。前置可消除这个竞态。

### 6.6 为什么 `End/DCEnd` 要 Remove 去重集合？

Windows 线程退出后 tid 可以被回收复用给新线程（甚至不同进程）。若去重集合永不清理：
- 旧 tid 的映射滞留 → 新线程 CSwitch 触发时被误判为"已上报"，**永远收不到新 pid/name 映射**。
- 分析端 `SystemThreadIdToProcessIdMap.Add` 本身是"后写覆盖"，只要游戏端再上报一次就能自动纠正，所以清理去重集合是"允许新映射发送"的唯一杠杆。

---

## 7. 场景矩阵（验证口径）

| 线程类型 | Live 场景 | Late-joining 场景 |
|----------|-----------|-------------------|
| UE 注册线程（GameThread/RenderThread 等） | ✅ 真名（来自 ThreadProvider） | ✅ 真名（`$Trace.ThreadInfo` 是 Important） |
| Trace 内部 Worker | ✅ `Trace Thread <tid>` | ✅ `Trace Thread <tid>`（同上） |
| 第三方插件：`SetThreadDescription` 命名（D3D12/Wwise/FMOD/PhysX 等） | ✅ 真名 | ✅ 真名（`ThreadName` 是 Important） |
| 第三方插件：老式 `MS_VC_EXCEPTION` 或未命名 | ✅ `Process Thread <tid>` | ✅ `Process Thread <tid>` |
| `OpenThread` 失败（权限不足） | ✅ `Process Thread <tid>` | ✅ `Process Thread <tid>` |
| 其他进程线程 | ✅ exe 路径（半透明） | ✅ exe 路径（半透明） |

---

## 8. 已知局限与容量评估

### 8.1 Important Cache 无上限

UE Trace 的 Important Cache (`Cache.cpp`) 按 64KB block 一直 append，进程生命周期内**不会自动淘汰**。

- `ThreadProcess` 条目：约 20 字节 / tid。
- `ThreadName` 条目：
  - 本进程插件真名：~ 40-80 字节 / tid；
  - 非本进程 exe 路径：~ 100-200 字节 / tid。

**容量估算**（悲观场景）：
- 游戏跑 2 小时，累积出现 5,000 个历史 tid；
- 平均事件大小约 100 字节；
- 总占用约 `5000 × 100 = 500KB`，与其他 Important 事件（`CpuProfiler.EventSpec` 等）同量级，可接受。

**缓解措施**：`Thread End/DCEnd` 时 Remove 去重集合，让未来新 tid 的新映射能够发送；但已进入 Cache 的旧条目不会被删除（Trace 框架限制）。

### 8.2 `GetThreadDescription` 覆盖率

- 覆盖：所有使用 `SetThreadDescription` 或 `NtSetInformationThread(ThreadNameInformation)` 的现代 API。
- **不覆盖**：
  - 老式 `RaiseException(MS_VC_EXCEPTION=0x406D1388)` 命名（只对调试器可见）；
  - `CreateThread` 后完全没命名的线程。
- 两种情况下最终显示为 `Process Thread <tid>`，符合"拿不到则保持现有逻辑"的要求。

### 8.3 非 Windows 平台

本次方案仅在 **Windows ETW** 路径启用 ContextSwitch 录制与 `GetThreadDescription` 反查。Android/Apple/Unix 平台仅补齐 `ThreadGetCurrentSystemId()` 保证链接通过，本身并不参与 ContextSwitch 数据流。

### 8.4 ETW 管理员权限

启动 ETW CSwitch Session 需要管理员权限。没有时 `StartTraceW` 返回 `ERROR_ACCESS_DENIED(5)`，日志分类 `LogPlatformEvents` 会有明确报错。这是 Windows OS 限制，不属于本改造范围。

---

## 9. 回归测试清单

| # | 用例 | 预期 |
|---|------|------|
| 1 | 启动带 `-trace=contextswitch,...` 的游戏，立刻 Live 连接 UnrealInsights | CPU Core 视图看到所有类别线程，插件线程显示真名 |
| 2 | 启动游戏等 5 分钟，再 Live 连接 UnrealInsights | 与用例 1 一致（Important Cache 重放） |
| 3 | 关闭 `Show Trace Thread Events` toggle | Trace Worker 线程 CSwitch 不再绘制 |
| 4 | 关闭 `Show Unregistered Process Thread Events` toggle | 第三方插件线程 CSwitch 不再绘制 |
| 5 | 关闭 `Show Non-Target Process Events` toggle | 其他进程 CSwitch 不再绘制 |
| 6 | Live 运行下，动态加载并卸载 Wwise / FMOD 插件 | 新线程真名被正确捕捉；tid 复用时不错挂到旧进程/旧名字 |
| 7 | Android/Apple/Linux 平台构建 | 能成功编译链接（补齐的 `ThreadGetCurrentSystemId` 生效） |
| 8 | ETW 权限不足（非管理员启动） | `LogPlatformEvents` 报错，但游戏本身不崩溃 |
| 9 | 非管理员情况下 UnrealInsights 视图 | 无 CSwitch 数据，其他 UE Trace 数据正常 |
| 10 | 长时间运行（>2h）后收集 trace | `GTraceStatistics.CacheUsed` 增长在预期范围内 |

---

## 附录 A：`#pragma region Engine ZXB` 命中清单（代码审计）

所有改动严格使用 `#pragma region Engine ZXB` / `#pragma endregion` 包裹。审计时可用命令：
```
rg -n "Engine ZXB" Engine/Source
```
预期命中文件（按职责分组）：
- TraceLog：`Platform.h`、`Trace.cpp`、`WindowsTrace.cpp`、`AndroidTrace.cpp`、`AppleTrace.cpp`、`UnixTrace.cpp`
- Core/PlatformEvents：`PlatformEvents.h`、`PlatformEvents.cpp`、`EventTracingForWindows.cpp`
- TraceServices：`ContextSwitches.h`、`ContextSwitchesPrivate.h`、`ContextSwitches.cpp`、`PlatformEventTraceAnalysis.h`、`PlatformEventTraceAnalysis.cpp`
- TraceInsights：`ContextSwitchesSharedState.h`、`ContextSwitchesSharedState.cpp`、`CpuCoreTimingTrack.cpp`、`InsightsMenuBuilder.cpp/.h`、`TimingProfilerCommands.h`、`CpuUsageTrackHelper.cpp`、`ThreadReportExporter.h/.cpp`

---

## 附录 B：术语表

| 术语 | 含义 |
|------|------|
| **SystemThreadId** | 操作系统原生线程 ID（Windows: `GetCurrentThreadId`；Linux: `gettid`；macOS: `pthread_mach_thread_np`） |
| **TraceThreadId** | UE Trace 内部分配的线程 ID，基于 `ETransportTid::Bias(=2)` 递增 |
| **ContextSwitch** | Windows ETW 内核事件，描述 CPU 核心从旧线程切换到新线程 |
| **Important Cache** | UE Trace 的永久缓存，用于存储状态性事件，支持 late-joining 重放 |
| **NoSync** | trace 事件标志；进入普通 Ring Buffer，流式消费，不保留历史 |
| **Important** | trace 事件标志；进入 Important Cache，永久保留直至进程结束 |
| **DCStart / DCEnd** | ETW Data Collection Start/End，session 启动时对已存在线程的 rundown 事件 |
| **late-joining** | 分析端（UnrealInsights）在 trace session 已进行一段时间后才连接的场景 |

---

_— 文档结束 —_
