# UE5 TaskGraph 限流死锁 — 让出 Worker 无唤醒路径 · 有界让出修复 · GC 增量可达性接管

**摘要：** Windows 客户端的 CPU 限流补丁（WindowsCPUUsageTrack → TaskGraph.MaxActive*Workers）让超出名额的 worker 停在 `FWaitingQueue::WaitForYieldResume`，而它们只有「限流放宽」「关服」两条唤醒路径。限流稳定在 1/1 时，后台队列 10 个 worker 有 9 个永久睡着，MMH 虚拟阴影的后台任务链没人执行，渲染线程卡在 `FMMHVirtualShadowMapArray::Update`，游戏线程等渲染 fence **120 s** 超时后自行退出——修复前 **4/4 局必卡**。修复：让出改为有界等待，新 CVar **`TaskGraph.YieldWakeIntervalMs`（默认 1 ms，0 = 原来的无限等待）**，修复后 **8/8 局不卡**；限流局帧率中位 31.9 → 39.4（兜底 50 ms → 1 ms），代价是限流从硬上限变成软上限、进程 CPU **10.1% → 15.5%**；不限流的机器上零开销。另在 `GarbageCollection.cpp` 加 **`gc.StealWorkStuckTimeout`（默认 0.05 s）**：增量可达性分析时由调用线程接管没开工的 CollectReferences 槽位，堵住「时间片为 0 + worker 开不了工」的永久自旋，属第二道防线，实机未触发。

> 仓库 C:\GR_main · 引擎 UE5EA（UE 5.5.4 分叉）· 包体 main_0.1175013 Development Win64 · 用例 NeoRing 特效脚本 NR_SpecialEffectsAI · 限流 t.CPUUsageTrack.Max{Foreground,Background}WorkerCount = 1 · 每条结论附 文件:行号

## 1. 一句话结论与全景

> 死锁不是「线程数太少」，而是**被让出的 worker 没有任何自己醒来的机会**：限流稳定时，后台队列里的任务只能等名额内那 1 个 worker；它 120 s 没回来，整条渲染链就停了。

**图 1 · 全景**

```
控制面（游戏线程驱动）
  ① CPUUsageTrack：World 定时器每 1 s 采样；CPUUsage > 1.0 ⇒ Busy
      --Busy 跳变--> ② TaskGraph.MaxActive*Workers = 1 → SetMaxActiveWorkerCount(1)
      --Max = 1----> ③ WorkerLoop：执行完一个任务 → ShouldYieldWorker；超出名额 → WaitForYieldResume
受害链（数据面）
  GameThread：FRenderCommandFence::Wait 等渲染线程；120 s 超时 → 打 RT 栈 → RequestExit
      | 等 fence
  RenderThread：MMHVirtualShadowMapArray::Update，Wait() 等 CollectPageRequest 完成
      | 等任务事件
  后台队列：MMH 任务链 4 个（DecodeRaw + UpdatePending → DecodeContext → CollectPageRequest），排队无人取
      | 需要 worker
  后台 worker × 10：1 个在名额内（120 s 没回来取队列），9 个停在 WaitForYieldResume   <- 修复作用在这 9 个上
让出态只有两条唤醒路径：限流放宽（SetMaxActiveWorkerCount → WakeYieldedWorkers）、关服（StopWorkers → WakeYieldedWorkers）
  ✗ 入队的 Notify() 只叫醒原生空闲队列   ✗ 限流稳定在 1/1 ⇒ 不放宽   ✗ 游戏线程卡住 ⇒ 1 s 定时器不跳
修复：WaitForYieldResume 改 Wait(1 ms)；CVar TaskGraph.YieldWakeIntervalMs（默认 1，0 = 原来的无限等待）
```

全景分三层。读后文时可以随时回到这张图定位：

- **控制面（谁决定限流）**：`WindowsCPUUsageTrack.cpp:145` 在 World 定时器上每 1 s 采样一次进程 CPU，`WindowsCPUUsageTrack.cpp:217` 以 `CPUUsage > GCPULimitPercent`（默认 1.0）判 Busy。Busy 时把 `TaskGraph.MaxActiveForegroundWorkers` / `TaskGraph.MaxActiveBackgroundWorkers` 设成 `t.CPUUsageTrack.Max*WorkerCount`（`WindowsCPUUsageTrack.cpp:354`、`WindowsCPUUsageTrack.cpp:364`），Relaxed 时设回 -1（`WindowsCPUUsageTrack.cpp:440`、`WindowsCPUUsageTrack.cpp:450`）。
- **调度面（worker 怎么让出）**：两个 CVar 的回调调用 `FScheduler::SetMaxActiveWorkerCount`（`Scheduler.cpp:35`、`Scheduler.cpp:46`），写进前台/后台两个 `FWaitingQueue`。worker 每执行完一个任务调用 `ShouldYieldWorker`（`Scheduler.cpp:1042`），超出名额就进 `WaitForYieldResume`（`Scheduler.cpp:1045`）。
- **受害链（谁在等谁）**：渲染线程在 `MMHVirtualShadowMapArray.cpp:2290` 等后台队列上的 `CollectPageRequest` 任务；游戏线程在 `RenderingThread.cpp:1317` 等渲染 fence，超过 `g.TimeoutForBlockOnRenderFence`（120000 ms，`RenderingThread.cpp:1143`）后 `RequestExit`（`RenderingThread.cpp:1202`）。

两处改动分别在两层，互相独立：

| 文件 | 层 | 修什么 | 验证状态 |
|---|---|---|---|
| `WaitingQueue.cpp` | TaskGraph 调度 | 让出改有界等待（本次死锁的根因修复） | 实机：修复前 4/4 卡，修复后 8/8 不卡 |
| `GarbageCollection.cpp` | GC 增量可达性 | 调用线程接管没开工的 GC 槽位 | 代码核对；实机未触发 |

## 2. 限流补丁如何让出 worker：只有两条唤醒路径

> 让出发生在「执行完一个任务之后」；进入让出态的 worker 等的是专用事件 `YieldEvent`，新任务入队的 `Notify()` 叫不醒它，只有限流放宽或关服能叫醒。

**图 2 · 让出状态机**

```
取任务执行（TryExecuteTaskFrom，成功一次 = 执行完 1 个任务）
  --执行完 1 个--> ShouldYieldWorker()：已让出数 < NeedYield ?
        否 → 名额已满，回去继续取任务
        是 → 让出态 WaitForYieldResume：YieldEvent->Wait(无限)；醒来后 YieldedWorkerCount − 1
                 唯二的唤醒：限流放宽（SetMaxActiveWorkerCount → WakeYieldedWorkers）/ 关服（StopWorkers → WakeYieldedWorkers）
  --队列为空--> 原生空闲（UE 原有路径）：PrepareWait / CommitWait，新任务入队 Notify() 能唤醒
NeedYield = ThreadCount − MaxActive
  后台队列：10 − 1 = 9 个让出，1 个在跑；前台队列：2 − 1 = 1 个让出，1 个在跑（-LogYieldState 实测）
  Notify() 只操作原生空闲那条等待栈；让出态等的是 YieldEvent，入队再多任务也叫不醒
```

### 数据结构：每个 FWaitingQueue 多了三个字段

| 字段 | 定义 | 含义 |
|---|---|---|
| `MaxActiveWorkerCount` | `WaitingQueue.h:56` | -1 = 不限；≥0 = 本队列最多几个 worker 在跑 |
| `YieldedWorkerCount` | `WaitingQueue.h:57` | 当前处于让出态的 worker 数 |
| `YieldEvent` | `WaitingQueue.h:58` | **ManualReset** 事件，让出者都等它 |

### 流程：执行完一个任务 → 抢让出名额 → 睡

- `ShouldYieldWorker`：不限流或限流值 ≥ ThreadCount 直接返回 false（`WaitingQueue.cpp:481`）；否则计算 `NeedYield`（`WaitingQueue.cpp:488`），用 CAS 把 `YieldedWorkerCount` 加到 `NeedYield` 为止，抢到名额的返回 true。
- 抢到名额的 worker 调 `WaitForYieldResume`：等 `YieldEvent`，醒来后 `YieldedWorkerCount` 减 1（`WaitingQueue.cpp:517`）。
- 队列取空时 worker 走 UE 原有的 `PrepareWait` / `CommitWait`（`Scheduler.cpp:1066`、`Scheduler.cpp:1069`），这条路新任务入队时会被 `Notify()` 唤醒——**但它和让出态是两条互不相干的等待**。

### 代入数字：本机的两个队列

`-LogYieldState` 诊断日志（已在整理时删除）实测：后台队列 `ThreadCount=10 ActiveThreads=10 MaxActive=1 Yielded=9`，前台队列 `ThreadCount=2 ActiveThreads=2 MaxActive=1 Yielded=1`。即后台 10 个 worker 里 **9 个在让出态、1 个在跑**。线程在启动时一次建好（`Scheduler.cpp:440`，未开动态建线程），所以 `ActiveThreads` 等于 `ThreadCount`。

### 唤醒路径只有两条

| 路径 | 调用点 | 什么时候发生 |
|---|---|---|
| 限流放宽 | `WaitingQueue.cpp:458`（`SetMaxActiveWorkerCount` 内，新值更大或 <0 时） | CPUUsageTrack 判 Relaxed、把 CVar 设回 -1 |
| 关服 | `Scheduler.cpp:583`、`Scheduler.cpp:584`（`StopWorkers`） | 引擎退出 |

> **为什么这是死结**：限流稳定在 1/1 时放宽不会发生；而唯一能触发放宽的 CPUUsageTrack 跑在游戏线程的 World 定时器上（`WindowsCPUUsageTrack.cpp:145`），游戏线程一旦卡在等渲染 fence，定时器也不再跳。**让出者能否醒来，取决于一个正在等它们干活的线程**。

## 3. 跟着一帧走到卡死：渲染线程等 MMH 后台任务链

> 渲染线程把 4 个 MMH 任务派到**后台队列**后原地等最后一个；后台 10 个 worker 里能干活的只有 1 个，它 120 s 内没回来取队列，于是渲染线程、游戏线程依次停住，看门狗超时退出。

**图 3 · 一帧的一生**

```
时间 →       t0 RT 进入 MMH 更新      帧号冻结（限流生效后约 96 s）              +120 s
GameThread    正常 Tick，每 1 s 跑 CPUUsageTrack | FRenderCommandFence::Wait（定时器停摆 ⇒ 不放宽） | RequestExit
RenderThread  派发 4 个 MMH 任务到后台队列 | Wait() → WaitImpl → StateChangeEvent.WaitFor：任务不完成就一直等
后台队列      [DecodeRaw] [UpdatePending] [DecodeContext] [CollectPageRequest]  依次依赖，BackgroundThreadPriority，没人取
BG worker #0      占着唯一名额：在执行别的任务，120 s 内没回来取队列（当时在做什么未取证）
BG worker #1–#9   YieldEvent->Wait(无限)：没有放宽、没有关服，就永远不醒
等待方向：GameThread → RenderThread → CollectPageRequest → 后台 worker；能干活的只剩 #0
```

### ① 渲染线程派发任务链

`FMMHVirtualShadowMapArray::Update` 异步模式下依次派发四个 `TGraphTask`，后一个以前一个为前置：

| 任务 | 派发点 | 前置 |
|---|---|---|
| `FMMHDecodeRawFeedbackValueTask` | `MMHVirtualShadowMapArray.cpp:2049` | — |
| `FMMHUpdatePendingPrimitiveTask` | `MMHVirtualShadowMapArray.cpp:2177` | — |
| `FMMHDecodeFeedbackContextTask` | `MMHVirtualShadowMapArray.cpp:2195` | 前两个 |
| `FMMHCollectPageRequestTask` | `MMHVirtualShadowMapArray.cpp:2216` | DecodeFeedbackContext |

四个任务的 `GetDesiredThread()` 都返回 `CPrio_MMHVirtualShadowMapArrayUpdate`（例如 `MMHVirtualShadowMapArray.cpp:1332`），它的线程优先级是 **`BackgroundThreadPriority`**（`MMHVirtualShadowMapArray.cpp:477`）⇒ 全部进**后台队列**，也就是那个 10 个 worker 里 9 个在让出态的队列。

### ② 渲染线程等最后一个任务

`MMHVirtualShadowMapArray.cpp:2290` `CollectPageRequestTaskEvent->Wait()`。等待实现 `FTaskBase::WaitImpl`（`TaskPrivate.cpp:244`）每轮先 `TryRetractAndExecute` 尝试自己把任务做掉（`TaskPrivate.cpp:249`），做不掉就挂在 `StateChangeEvent.WaitFor` 上（`TaskPrivate.cpp:277`），只在任务状态变化时再试一次。run4 的符号化栈确认渲染线程停在这里（栈帧显示行号 2291，是返回地址所在行）。

### ③ 后台队列：没人取

- 9 个让出的 worker 在等 `YieldEvent`，见第 2 章——入队的 `Notify()` 叫不醒它们。
- 名额内那 1 个 worker 在 120 s 内没回来取这条任务链。**它当时在执行什么没有取证**（看门狗只打渲染线程的栈）。

### ④ 游戏线程超时退出

游戏线程在 `FRenderCommandFence::Wait` → `GameThreadWaitForTask`（`RenderingThread.cpp:1317`）等渲染线程。超过 120 s 后 `HandleRenderTaskHang` 打栈——它固定取 `GRenderThreadId`（`RenderingThread.cpp:1293`），所以日志里的栈是**渲染线程**的，不是游戏线程的——然后 `RequestExit`（`RenderingThread.cpp:1202`）。此时 CPUUsageTrack 的 1 s 定时器已经停跳，限流不可能被放宽。

### 实测时间线（run2，限流 1/1）

| 时刻 | 事件 |
|---|---|
| 19:42:11 | 进 NeoRing（`LoadMap` 2.94 s） |
| 19:42:19 | `SetMaxActiveWorkerCount: Foreground -1 -> 1` / `Background -1 -> 1` |
| 19:43:47 | 帧号冻结在 529（之后只有网络线程在打 ping） |
| +120 s | `GameThread timed out waiting for RenderThread after 120.00 seconds` → `RequestExit` |

修复前 4 局（run2/3/4/6）全部复现，都在限流生效后约 96 s 冻结，栈一致。

## 4. 修复：有界让出 TaskGraph.YieldWakeIntervalMs

> 让出者最多睡 `TaskGraph.YieldWakeIntervalMs`（默认 1 ms）就自己醒来，回到取任务循环；取到任务就执行一个，再按名额决定是否继续睡。排队的任务不再依赖「限流放宽」才能被执行。

### 改动（WaitingQueue.cpp）

```cpp
// [ZXB] 让出者的醒来间隔（毫秒），0 = 无限等待（原生行为）；调小帧率更高，但限流省下的 CPU 更少
static int32 GYieldWakeIntervalMs = 1;
static FAutoConsoleVariableRef CVarYieldWakeIntervalMs(
	TEXT("TaskGraph.YieldWakeIntervalMs"),
	GYieldWakeIntervalMs,
	TEXT("Milliseconds a yielded LowLevelTasks worker sleeps before re-checking for work. 0 = wait forever (stock behaviour)."),
	ECVF_Default
);

void FWaitingQueue::WaitForYieldResume()
{
	// [ZXB] 有界等待：限流稳定时只有放宽/关服会唤醒让出者，无限等待会让排队的任务饿死
	const uint32 WaitMs = GYieldWakeIntervalMs > 0 ? (uint32)GYieldWakeIntervalMs : MAX_uint32;
	YieldEvent->Wait(WaitMs);

	// 被唤醒后递减让出计数
	YieldedWorkerCount.fetch_sub(1, std::memory_order_relaxed);
}
```

CVar 定义在 `WaitingQueue.cpp:503`，有界等待在 `WaitingQueue.cpp:514`；为 CVar 新增的头文件在 `WaitingQueue.cpp:21`。取 0 或负数时映射为 `MAX_uint32`，即无限等待（`WaitingQueue.cpp:513`）。

**图 4 · 有界让出时间线**

```
0–6 ms 示意（任务时长为示例值）
worker A（名额内）    : [任务][任务][任务][任务][任务][任务][任务][任务]   连续执行
worker B（让出）      : 睡 1 ms [任务 0.3] 睡 1 ms [任务 0.3] 睡 1 ms [任务 0.3] ...
worker C（让出）      : [任务] 睡 1 ms [任务 0.6] 睡 1 ms [任务 0.6] ...
worker D（另一种情况）: 睡 1 ms → 醒来队列已空 → 退出让出态，转原生空闲（新任务 Notify 能叫醒）
醒来后先取任务：取到就执行 1 个，再判名额——仍缺人就再睡 ≤1 ms；被别人补满就连续干下去
Windows 上 Wait(1) 受 1 ms 定时器精度影响，实际约 1–2 ms
```

### 为什么它能解开死锁

- 醒来后回到 `WorkerLoop` 的内层循环条件，**先取任务再判让出**（`Scheduler.cpp:1029` 起）⇒ 只要队列里有任务，醒来的 worker 至少执行一个。MMH 任务链因此最多延迟 ≈ 兜底间隔就会被执行。
- 醒来时 `YieldedWorkerCount` 已减 1，名额空出一个；接下来谁先执行完任务谁去补这个名额——让出的角色在 worker 之间轮换。
- 醒来时队列空：内层循环直接退出，下一轮走 UE 原有的 `PrepareWait` / `CommitWait`，不再处于让出态 ⇒ **闲时不会每毫秒空转**。

### 与原有唤醒逻辑的兼容

- `YieldEvent` 是 ManualReset（`WaitingQueue.h:58`）。`WakeYieldedWorkers` 的「Trigger → 等计数归零 → Reset」（`WaitingQueue.cpp:520`）与超时醒来互不冲突：计数加减仍一一对应。
- 顺带消掉两个原有竞态：① worker 抢到名额后、开始等待之前事件已被 Reset，原来会永久睡下去；② `StopWorkers` 先唤醒再 `StartShutdown`（`Scheduler.cpp:583`、`Scheduler.cpp:586`），之间 worker 可能再次让出，原来会让 `Join` 卡死。现在两种情况都最多 1 个兜底间隔后自行恢复。
- Windows 进程启动时 `timeBeginPeriod(1)`（`WindowsPlatformMisc.cpp:1065`），`Wait(1)` 实际约 1–2 ms。

### 附带的防御性改动：NeedYield 按实际线程数算

```cpp
// [ZXB] 让出名额按实际活跃线程数算（动态建线程时少于 ThreadCount），并保证至少留一个线程不让出
const int32 ActiveThreads = (int32)WaitingQueueImpl::GetActiveThreadCount(StandbyState.load(std::memory_order_relaxed));
const int32 NeedYield = FMath::Max(0, FMath::Min((int32)ThreadCount, ActiveThreads) - FMath::Max(MaxActive, 1));
```

位置 `WaitingQueue.cpp:486`–`WaitingQueue.cpp:488`。本机线程预创建，`ActiveThreads == ThreadCount`，MaxActive ≥ 1 时与原公式 `ThreadCount − MaxActive` 结果完全相同——**它不是这次死锁的修复**。只在两种配置下有区别：限流值设成 0（原公式会让全部 worker 让出），或启动参数 `-TaskGraphUseDynamicThreadCreation=1`（`Scheduler.cpp:278`）。两种都没实测，见第 10 章。

## 5. 实测：死锁消除、帧率与 CPU 代价

> 修复前 4/4 局卡死，修复后 8/8 局不卡；兜底间隔取 1 ms 时限流局帧率中位 39.4（50 ms 时 31.9），进程 CPU 15.5%（50 ms 时 10.1%）；1 ms 与 5 ms 无差别。

**图 5 · 实测**

```
配置          FPS 中位  FPS 均值  进程 CPU
兜底 50 ms      31.9      45.6     10.10 %
兜底 5 ms       39.7      54.6     15.51 %
兜底 1 ms       39.4      55.2     15.53 %
无限流          55.5      66.1     无日志（CPUUsageTrack 不启用）
卡死（限流 1/1）：修复前 4 局全卡；修复后 8 局全部未卡（50 ms×4，1 ms×3，5 ms×1），均 RequestExit(0) 正常退出
每种配置单局、同机同脚本、不开 trace；1 ms 与 5 ms 持平 ⇒ 收益在 5 ms 以内已饱和
```

### 测试条件

- 包体 `S1Game_main_0.1175013…_Development_Win64`，只替换 `S1Game.exe`（及同版 `S1Game.pdb`）。
- 脚本 `NR_SpecialEffectsAI`（NeoRing 特效 + AI），`# GameStartDelayed` 下设 `t.CPUUsageTrack.MaxForegroundWorkerCount=1`、`t.CPUUsageTrack.MaxBackgroundWorkerCount=1`。本机默认分档是 {-1,-1}（不限流），必须手动设。
- FPS 为局内 1 s 采样的统计；CPU 为 `LogCPUUsageTrack: On CPU Usage` 日志均值，口径 = 进程 CPU 时间 / 系统非空闲 CPU 时间 × 100（`WindowsPlatformMisc.cpp:200`）。不限流时 CPUUsageTrack 不启用，没有这条日志。
- 每种配置单局，不开 trace。

| 配置 | FPS 均值 | FPS 中位 | P10 | P90 | <5 FPS 采样数 | CPU 均值 | 卡死 |
|---|---|---|---|---|---|---|---|
| 官方 exe，不限流 | 61.0 | 52.8 | 35.0 | 104.8 | 2 | 无日志 | 0 |
| 本次 exe，不限流 | 66.1 | 55.5 | 47.0 | 109.5 | 2 | 无日志 | 0 |
| 本次 exe，限流，兜底 50 ms | 45.6 | 31.9 | 28.8 | 82.2 | 9 | 10.10% | 0 |
| 本次 exe，限流，兜底 5 ms | 54.6 | 39.7 | 34.3 | 88.1 | 2 | 15.51% | 0 |
| **本次 exe，限流，兜底 1 ms（默认）** | **55.2** | **39.4** | 34.5 | 90.1 | 2 | 15.53% | 0 |

### 怎么读这张表

- **本次改动本身不降帧**：同样不限流，本次 exe 66.1 / 官方 61.0（单局，差值在波动内，只能说没变差）。
- **限流局的帧率差距来自限流本身**：兜底 1 ms 中位 39.4，仍低于不限流 55.5 ⇒ 并行度仍被压着，限流没有失效。
- **兜底间隔要小于一帧**：50 ms 比一帧（16–40 ms）还长，让出者整帧帮不上忙。trace 里典型帧渲染线程 `WaitForTasks` 33.1 ms、其中在 CPU 上只有 0.099 ms；从计时器树推断，它在等前台队列里唯一在跑的那个 worker。降到 1 ms 后均值 +21%、中位 +23%、<5 FPS 采样 9 → 2。
- **收益在 5 ms 以内饱和**：1 ms 与 5 ms 帧率、CPU 都持平，所以默认取 1 ms，低端机可按需调到 5 ms。

### 卡死统计

| 阶段 | 局数 | 结果 |
|---|---|---|
| 修复前（让出无限等待） | 4（run2/3/4/6） | 全部在限流生效后约 96 s 冻结，120 s 后看门狗退出 |
| 修复后，兜底 50 ms | 4（run7/8/9 + 一局 trace） | 全部跑满脚本，`RequestExit(0, UGameEngine::HandleExitCommand)` 正常退出 |
| 修复后，兜底 1 ms | 3（含一局 trace、一局换测服） | 同上 |
| 修复后，兜底 5 ms | 1 | 同上 |

### 限流局里的周期性长帧

兜底 1 ms + trace 的那局里有 5 个约 145 ms 的长帧，引擎帧号 16217 / 19102 / 21933 / 24652 / 27426，与日志里 GC `SetupWorkers` 的帧号逐个吻合，约每 61 s 一次——是增量 GC 并行可达性分析的启动帧。不限流时这一帧多长没有量，所以**不能说它与限流有关或无关**。

## 6. GC 增量可达性：调用线程接管未开工的槽位

> 增量 GC 不允许偷别人没开工的上下文；worker 开不了工、时间片又变成 0 时，游戏线程会永久自旋。`gc.StealWorkStuckTimeout`（默认 0.05 s）让调用线程离开自旋、把没人认领的槽位接过来自己做。它与第 3 章的死锁无关，是同类风险的第二道防线，实机未触发。

**图 6 · GC接管**

```
ProcessAsync（调用线程 = 游戏线程）
  ① Launch N−1 个 CollectReferences 任务（任务体 TryStartWorking(Idx) 认领槽位）
  ② TryStartWorking(0) → ProcessSync；自己那份做完 → 进 StealWork 自旋
  ③ [ZXB] 接管循环 Idx = 1…N−1：TryStartWorking(Idx) 成功 ⇒ 自己 ProcessSync
  ④ SpinUntilAllStopped：等 NumStopped == N
  ⑤ Tasks::Wait(AllWorkerTasks)：被接管任务开工时槽位已空 → 空操作返回
StealWork 自旋 while (KeepSpinning())：NumWorkless < N 就继续
  出口 1：偷到整块 / ARO → 回去处理对象
  出口 2：IsTimeLimitExceeded() → 挂起；迭代数 > gc.ReachabilityIterationsLimit(100) 后时间片 = 0 ⇒ 恒 false，失效
  出口 3 [ZXB]：只对调用线程；协调器创建后 > gc.StealWorkStuckTimeout(0.05 s) 且有槽位没人认领 ⇒ 挂起离开，去 ③
原生死角：增量模式禁止 StealContext；worker 被限流或全忙 + 时间片为 0 ⇒ 游戏线程永久自旋
```

### 原生的死角

- `ProcessAsync`（`GarbageCollection.cpp:9023`）为 N−1 个槽位各 Launch 一个 `CollectReferences` 任务（`GarbageCollection.cpp:9057`），任务体里 `TryStartWorking(Idx)` 认领槽位；调用线程自己做槽位 0（`GarbageCollection.cpp:9067`）。
- 做完自己那份进 `StealWork` 自旋。增量模式下跳过 `StealContext`（`GarbageCollection.cpp:8819`），所以没开工的槽位只能等它的任务被某个 worker 取走。
- 自旋条件 `KeepSpinning`：`NumWorkless < N` 就继续（`GarbageCollection.cpp:8730`）。没开工的槽位永远不会报告「没活干」⇒ 条件恒真。
- 唯一的时间出口是 `IsTimeLimitExceeded()`（`GarbageCollection.cpp:8867`）。迭代数超过 `gc.ReachabilityIterationsLimit`（项目配置 100，`DefaultEngine.ini:391`；代码默认 25，`GarbageCollection.cpp:465`）后，`GetReachabilityAnalysisTimeLimit` 返回 0（`GarbageCollection.cpp:7658`），`IsTimeLimitExceeded` 恒 false（`ReachabilityAnalysisState.h:88`）⇒ 出口失效。

> 两个项目补丁叠加才致命：迭代上限补丁把时间片置 0（本意是「强制收尾」，但对自旋者等于「永不中断」），限流补丁破坏了「Launch 出去的任务终会被执行」这个前提。

### 改动：逃生门 + 接管循环

- CVar `gc.StealWorkStuckTimeout`（`GarbageCollection.cpp:583`），0 = 关闭（回到原生）。
- `FWorkCoordinator` 构造时快照开关、调用线程 ID、创建时刻（`GarbageCollection.cpp:8786`–`GarbageCollection.cpp:8789`）。
- `StealWork` 自旋里新增出口：**只有调用线程**、协调器创建超过阈值、且还有槽位没人认领时，`SuspendWork` 后离开自旋（`GarbageCollection.cpp:8875`）。worker 照原样留下偷块。
- `ProcessAsync` 在 `SpinUntilAllStopped` 前逐个 `TryStartWorking(Idx)`，认领成功就在调用线程上 `ProcessSync`（`GarbageCollection.cpp:9077`–`GarbageCollection.cpp:9084`）。被接管槽位对应的任务之后开工时认领失败、直接返回，由末尾 `Tasks::Wait`（`GarbageCollection.cpp:9131`）回收。

### 为什么接管是安全的

- 接管是认领**整个槽位**再在调用线程上处理，不搬上下文内容，不违反增量模式不偷上下文的约束；原生收尾时的 Rare case 路径（`GarbageCollection.cpp:9110`）也是游戏线程直接处理别人的上下文。
- 逃生点与原生时间片挂起点在同一个自旋循环里，此时自己的队列已空、没有要保存的中间状态；`ProcessObjectArray` 入口会重置挂起标志（`FastReferenceCollector.h:957`）。
- 计数自洽：每个槽位各贡献一次 `NumStopped`，`SpinUntilAllStopped`（`GarbageCollection.cpp:8746`）能结束；析构里的 `check(NumUsedContexts == Contexts.Num())`（`GarbageCollection.cpp:8656`）成立。

### 实际触发条件

- 限流 1/1 时 `InitializeAsyncProcessingContexts` 按限流值把 worker 数压到 1（`GarbageCollection.cpp:8915`），只有一个槽位，接管不可能触发；多槽位 + 开不了工只在「增量分析中途限流收紧」时出现。
- 有界让出修复后，没开工的 `CollectReferences` 任务约 1 ms 内会被醒来的 worker 取走（推断）⇒ 接管只在所有 worker 都被 >50 ms 的长任务占住时才出手。
- 所有跑测里 50 ms 逃生日志（Warning，`GarbageCollection.cpp:8779`）0 次；接管循环的日志是 Verbose（`GarbageCollection.cpp:9081`），默认看不到。

## 7. 对比：修复前后的限流语义（硬上限 vs 软上限）

> 修复前限流是硬上限，但代价是任务可能永远没人执行；修复后变成按时间轮流的软上限——名额内的 worker 照常连续工作，让出者每 ≤1 ms 回来帮一次，所以瞬时并行度可以超过限流值，平均并行度随任务时长变化。

|  | 修复前（无限等待） | 修复后（有界等待，1 ms） |
|---|---|---|
| 名额内 worker | 连续工作 | 连续工作（不变） |
| 让出的 worker | 完全不干活，直到限流放宽或关服 | 睡 ≤1 ms → 醒来执行 1 个任务 → 按名额决定是否再睡 |
| 醒来时队列空 | 不会醒 | 转 UE 原有空闲等待，不空转 |
| 上限性质 | 硬上限（= MaxActive） | 软上限（随任务时长在 MaxActive 与 ThreadCount 之间） |
| 排队任务最长等多久 | 无界（本次 >120 s） | ≈ 1 个兜底间隔 |
| 进程 CPU（限流 1/1 实测） | —（会卡死，无完整数据） | 15.53%（兜底 50 ms 时 10.10%） |
| 新建线程 | 不新建 | 不新建（线程启动时已建好，`Scheduler.cpp:440`） |

**图 7 · 并行度模型**

```
后台队列有效并行度（MaxActive = 1，让出 9 个，兜底 1 ms，队列持续有任务）；P = 1 + 9 × t / (t + 1)
任务 0.1 ms → 1.82
任务 0.5 ms → 4.00
任务 1 ms   → 5.50
任务 5 ms   → 8.50
任务 10 ms  → 9.18
参考线：修复前硬上限 1；无限流 10。模型估算，未实测；队列空时让出者转原生空闲，不计入
```

### 代入数字：后台队列（MaxActive = 1，让出 9 个）

粗略模型：每个让出者的一个周期是「睡 1 ms + 执行一个时长 t 的任务」，它在干活的时间占比是 t / (t + 1)，有效并行度 P ≈ 1 + 9 × t / (t + 1)。t = 0.1 ms → 1.82；t = 1 ms → 5.5；t = 10 ms → 9.18（全部 10 个线程约为 10）。**任务越碎，越接近限流值；任务越长，限流越接近失效。**这是模型估算，没有单独实测。

### 两个常见误解

- **「是不是阉割了线程持续工作的能力」**——压住线程的是原来的限流（让出者完全不干活）；本次改动是在放松它。名额内线程不受影响。
- **「是不是每个任务都要等 1 ms」**——不是。有空闲线程时任务立即执行；1 ms 只限制让出者多久回来帮一次。醒来的 worker 执行完一个任务后，如果名额已被别人补满，它会继续连续执行。

### 真正「按需」放行的方向（未实现、未验证）

只在名额内的 worker 进入阻塞等待时放一个让出者出来，就能保留硬上限又不饿死任务。UE 原生用 Oversubscription 计数阻塞中的 worker（`WaitingQueue.cpp:422`），现在的限流补丁没有接入它。改动面更大，需要重新验证死锁与帧率。

## 8. 不限流的机器上的开销与生效范围

> 不限流时 `WaitingQueue.cpp` 的改动一行都不执行；`GarbageCollection.cpp` 每个可达性迭代多约 1 µs 的固定开销。限流只在 Windows 客户端、且只在低核数机器上默认开启。

### 哪些机器默认开限流

| CPU 拓扑 | 默认 {前台, 后台} | 依据 |
|---|---|---|
| 物理核 ≤ 4 | {1, 1} | `WindowsCPUUsageTrack.cpp:66` |
| 6C6T | {1, 1} | `WindowsCPUUsageTrack.cpp:67` |
| 6C12T | {2, 3} | `WindowsCPUUsageTrack.cpp:68` |
| 8C8T | {1, 2} | `WindowsCPUUsageTrack.cpp:69` |
| 其余（8C16T、10C 以上） | {-1, -1}，不限流 | `WindowsCPUUsageTrack.cpp:71` |

CPUUsageTrack 整个文件在 `#if PLATFORM_WINDOWS` 里（`WindowsCPUUsageTrack.cpp:9`），Linux DS 与移动端走不到让出路径。

### WaitingQueue.cpp：零开销

`ShouldYieldWorker` 第一行判断不限流就返回 false（`WaitingQueue.cpp:481`），`NeedYield` 的改动在它之后；`WaitForYieldResume` 只在前者返回 true 时才调用。新 CVar 只在启动时注册一次。

### GarbageCollection.cpp：固定开销可忽略，有一处行为差异

- 每次 `ProcessAsync`（每个可达性迭代一次）：构造协调器时取一次线程 ID、读一次计时器；接管循环 N−1 次原子 exchange（每个槽位独占缓存行）。合计约 1 µs，GC 迭代本身是毫秒级。
- `StealWork` 自旋每轮多一个 bool 判断；所有槽位都已认领时短路，不读计时器。
- **行为差异**：接管循环只看开关、不看 50 ms 阈值。不限流时，增量 GC 时间片到期而部分 `CollectReferences` 任务还没开工（worker 正忙），原生做法是等它们开工并跑完首段，现在是调用线程依次自己跑首段（期间空出来的 worker 仍会抢走后面的槽位）。推断调用线程的等待不会比原生长，未实测。

## 9. 复现与取证要点

> 要拿到卡死栈，必须开着 hang 看门狗、并让 pdb 与 exe 同一次构建；选测服要按包体 CL 选端口。

| 要点 | 说明 |
|---|---|
| 别带 `-nothreadtimeout` | 它关掉 UE 的 hang 看门狗（`ThreadHeartBeat.cpp:506`），卡死时没有任何栈。去掉后 120 s 超时会打出被等待线程的栈。 |
| pdb 必须同版 | 替换了包体 exe 就把同次构建的 `S1Game.pdb` 一起拷进去，否则栈全是 `S1Game.exe!UnknownFunction`。 |
| 栈是谁的 | `HandleRenderTaskHang` 固定取 `GRenderThreadId`（`RenderingThread.cpp:1293`），打出来的是渲染线程栈。 |
| 限流要手动开 | 本机（8C16T 以上）默认 {-1,-1}，需在脚本里设 `t.CPUUsageTrack.Max*WorkerCount=1`；日志出现 `SetMaxActiveWorkerCount: Foreground -1 -> 1` 才算生效。 |
| 选对测服 | 测服按端口区分，且 CL 会隔夜变化。选错的表现是能登录、进房报 `ClientVersionNotSupport`(91054)，与代码无关。 |
| trace 要带命令行通道 | `-trace=gpu,cpu,loadtime,frame,log,bookmark,task,contextswitch`；只靠脚本里的 `Trace.Start` 得到的是空 trace。 |

## 10. 已知问题与待验证

| 项 | 现状 | 怎么补 |
|---|---|---|
| 名额内那个后台 worker 卡死时在做什么 | 未取证（看门狗只打渲染线程栈） | 卡死现场抓全线程 dump |
| `NeedYield` 防御性改动 | 默认配置下与原公式等价；限流值 0、动态建线程两种配置未实测 | 保留（成本低）；如要删需同步本文第 4 章 |
| GC 接管 | 实机 0 次触发；接管循环日志为 Verbose | `LogGarbage` 调到 Verbose 跑一局，统计接管次数 |
| 不限流时省了多少 CPU | 不限流时没有 CPUUsageTrack 日志 | 用 typeperf / PerfMon 外部记录进程 CPU 对照 |
| GC 启动帧（约 145 ms）与限流的关系 | 只有限流局数据 | 不限流 + trace 对照一局 |
| 限流局偶发 1.2 FPS 采样 | 4 局 1 次，带 trace 重跑未复现，原因未定 | 多跑几局抓到再分析 |
| 硬上限版限流 | 未实现 | 接入 Oversubscription（第 7 章） |

## 11. 改动清单与参考

### 文件与 CVar

| 文件 | 改动 | CVar（默认值） |
|---|---|---|
| `UE5EA/Engine/Source/Runtime/Core/Private/Async/Fundamental/WaitingQueue.cpp` | +17 / −4，原生行零改动 | `TaskGraph.YieldWakeIntervalMs`（1；0 = 原行为） |
| `UE5EA/Engine/Source/Runtime/CoreUObject/Private/UObject/GarbageCollection.cpp` | 5 个 hunk，原生行零删除 | `gc.StealWorkStuckTimeout`（0.05；0 = 关闭） |

两处改动都包在 `#pragma region Engine ZXB` 内，带 `// [ZXB]` 意图注释。

### 相关配置

| 配置 | 值 | 位置 |
|---|---|---|
| `gc.AllowIncrementalReachability` | 1 | `DefaultEngine.ini:388`、`WindowsEngine.ini:10` |
| `gc.IncrementalReachabilityTimeLimit` | 0.002 | `DefaultEngine.ini:390` |
| `gc.ReachabilityIterationsLimit` | 100 | `DefaultEngine.ini:391` |
| `t.CPUUsageTrack.CPUUsageLimitPercent` | 1.0（代码默认） | `WindowsCPUUsageTrack.cpp:44` |
| `g.TimeoutForBlockOnRenderFence` | 120000 ms | `RenderingThread.cpp:1143` |

### 关键源码位置

- 让出：`WaitingQueue.cpp:478`（ShouldYieldWorker）、`WaitingQueue.cpp:510`（WaitForYieldResume）、`WaitingQueue.cpp:520`（WakeYieldedWorkers）、`Scheduler.cpp:1021`（WorkerLoop）、`Scheduler.cpp:888`（SetMaxActiveWorkerCount）。
- 限流控制：`WindowsCPUUsageTrack.cpp:189`（每秒回调）、`WindowsCPUUsageTrack.cpp:289`（Busy）、`WindowsCPUUsageTrack.cpp:371`（Relaxed）、`WindowsCPUUsageTrack.cpp:459`（只在状态跳变时切换）。
- 受害链：`MMHVirtualShadowMapArray.cpp:2216`（派发 CollectPageRequest）、`MMHVirtualShadowMapArray.cpp:2290`（等待）、`TaskPrivate.cpp:244`（WaitImpl）、`RenderingThread.cpp:1150`（HandleRenderTaskHang）。
- GC：`GarbageCollection.cpp:8625`（FWorkCoordinator）、`GarbageCollection.cpp:8802`（StealWork）、`GarbageCollection.cpp:9023`（ProcessAsync）、`GarbageCollection.cpp:7650`（GetReachabilityAnalysisTimeLimit）。

限流补丁本身（`MaxActiveWorkerCount` / `ShouldYieldWorker` / `WaitForYieldResume`）的原始设计与 7 轮迭代见同目录 `UE5_TaskGraph_MaxActiveWorkerCount_Report.md`；本文是它的后续：那份设计里的让出态没有自醒路径，本文记录由此引发的死锁与修复。

行号基线：`C:\GR_main` 工作区 2026-10-15，含本次两处未提交改动（基于 `WaitingQueue.cpp#3`、`GarbageCollection.cpp#18`）。排查过程记录在 `C:\GR_main\.planning\2026-10-14-gc-incremental-reachability-deadlock-ver\findings.md`。
