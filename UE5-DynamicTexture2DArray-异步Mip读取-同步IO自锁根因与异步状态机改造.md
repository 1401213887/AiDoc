# UE5 Dynamic Texture2DArray 异步 Mip 读取：同步 IO 自锁根因与异步状态机改造

**对象**：`FTexture2DArrayDynamicAllocator`（`UTexture2DArray` 动态切片流送分配器），引擎 UE5.5.4 / `//GR/main` CL 1195928（bug 1266750）。**现象**：PC-23、白厄前言地图、固态硬盘，进图未跳伞卡死；DMP 中流送更新停在 `UWorldPartitionStreamingPolicy::UpdateStreamingState()`，Async 线程栈落在 `FTexturePlatformData::TryLoadMipsWithSizes`。**结论**：根因不是 IO 慢，而是把同步 `GetMipData → GetCopy → OpenReadBulkData → Event->Wait()` 塞进了异步状态机；在 `TaskGraph.MaxActive*Workers=1` 下，唯一活跃 worker 被钉在等待事件上，其它任务无法推进。**修复**：用 `IBulkDataIORequest` + `TaskSynchronization` 计数器把非流送 mip 改成真正的异步；不满足异步条件时整批回退同步。**验证**：显式重编 exit 0；NeoRing 1/1 跑图 9 次异步命中、0 回退/0 IO 失败/0 Fatal，自然退出 rc=0；FPS 273 采样均值 81.0 / 中位 77.3（同步基线 64.7 / 56.3）。

**路径约定**：正文中的源码文件名均相对 `C:\GR_main\UE5EA\Engine\Source\Runtime\`；跑测日志相对 `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\`。

## 1. 结论：问题不在“读得慢”，而在“异步状态机里同步读”

`FTexture2DArrayDynamicAllocator::AllocateMips` 是 `FTextureStreamIn` 状态机的一步，约定在 Async 线程执行：只做内存分配、发起异步请求，然后 `AdvanceTo` 到下一状态。改前的实现直接调用 `GetNoStreamingMips(Context)`（`depot_Texture2DArrayDynamicStreaming.cpp:75`），而 `GetNoStreamingMips` 最终落到同步的 `UTexture2DArray::GetMipData`（`TextureDerivedData.cpp:3448`）。

同步链的每一跳都能回源：

1. `TextureDerivedData.cpp:3459`：`GetMipData` 调用 `TryLoadMipsWithSizes`。
2. `TextureDerivedData.cpp:2584`：对每个未驻留的 mip 调用 `Mip.BulkData.GetCopy(&OutMipData[...], true)`。
3. `BulkData.cpp:908`、`BulkData.cpp:909`：`GetCopy` 把调用方内存包成 `FIoBuffer`，进入 `TryLoadDataIntoMemory`。
4. `BulkData.cpp:1691`：`TryLoadDataIntoMemory` 调用 `UE::BulkData::Private::OpenReadBulkData`。
5. `BulkDataStreaming.cpp:388`、`BulkDataStreaming.cpp:392`：`OpenReadBulkData` 创建 `FEventRef Event`，`Batch.IssueAndTriggerEvent` 后执行 `Event->Wait()`。

`Event->Wait()` 是硬阻塞。`TaskSynchronization` 根本看不到这次等待，所以 `TRenderAssetUpdate::TickInternal` 不会返回 `TS_Suspended`（`RenderAssetUpdate.inl:58`），状态机也无法把“等 IO”表达成“稍后继续”。worker 就这样停在调用栈里。

**为什么在 1/1 限流下会变成卡死**：`TaskGraph.MaxActive*Workers=1` 时，这个 worker 是唯一活跃名额。它停在 `Event->Wait()` 期间，流送更新、GC 增量可达性等任务都排在队列里，没有线程执行。DMP 里的 `UpdateStreamingState` 等待流送更新，正是这个窗口的表象；白厄前言的大块非流送 mip（复现用例约 45 MiB）把窗口拉长到足以被用户感知为“未跳伞卡死”。

**为什么有时不死锁**：`GetCopy` 只有在 `IsBulkDataLoaded()` 为假时才走磁盘 IO（`BulkData.cpp:891`）。如果 mip 已经驻留、页缓存命中，或者限流没有开到 1/1，`Event->Wait()` 的窗口就消失或变短。复现概率取决于资产驻留状态、磁盘与页缓存，而不是每次必现。

> **图 1**：修复前的同步读取链：Async worker 在 Event->Wait() 上停住（完整 SVG 见同名 HTML）

## 2. 修复后的异步状态机：一个 mip 请求的一生

修复把非流送 mip 的读取改成与引擎流送 provider 相同的异步协议：发起请求、计数器挂起、回调续跑。核心状态机在 `Texture2DArrayDynamicStreaming.cpp:51` 的 `AllocateMips`：

1. **首次进入（Async）**：校验 mips view；分配 `OutMipInfos`、`StreamedMipData`、`StreamedSliceSize`。分配块由 `bMipBuffersAllocated` 守住（`Texture2DArrayDynamicStreaming.cpp:61`），保证状态机重入时不会重复 `AddDefaulted` 或重复 `Malloc`。
2. **发起请求**：`IssueNoStreamingMipRequests`（`Texture2DArrayDynamicStreaming.cpp:312`）先对整批 mip 做异步兼容性预检；通过后逐 mip 调用 `IssueNoStreamingMipRequest`（`Texture2DArrayDynamicStreaming.cpp:372`）。
3. **占用计数器**：每发一个请求前执行 `Counter.Increment()` 与 `PendingNoStreamingRequests.Increment()`（`Texture2DArrayDynamicStreaming.cpp:380`、`Texture2DArrayDynamicStreaming.cpp:381`）。`Counter` 就是 `SyncOptions.Counter`，也就是 `TaskSynchronization`（`TextureMipDataProvider.h:54`）。
4. **挂起而不是轮询**：`AllocateMips` 发现 `PendingNoStreamingRequests > 0` 时执行 `AdvanceTo(ETickState::AllocateMips, ETickThread::Async)` 并返回 `true`（`Texture2DArrayDynamicStreaming.cpp:110`）。此时 `TaskSynchronization > 0`，下一次 `TickInternal` 直接返回 `TS_Suspended`，不消耗 CPU 自旋。
5. **回调续跑**：IO 完成回调（`Texture2DArrayDynamicStreaming.cpp:383`）校验结果与字节数，递减 `Counter` 与 `Pending`；最后一个回调设置 `bNoStreamingComplete` 并调用 `Reschedule`（`Texture2DArrayDynamicStreaming.cpp:396`、`Texture2DArrayDynamicStreaming.cpp:399`、`Texture2DArrayDynamicStreaming.cpp:402`）。
6. **重新进入**：`Reschedule` 就是 `SyncOptions.RescheduleCallback`（`TextureStreamIn.cpp:64`），它在对象未被锁定时调用 `Tick(TT_None)`。状态机重新进入 `AllocateMips`；`bMipBuffersAllocated` 保证不会重复分配。
7. **完成**：Pending 归零且完成标志已置位后，`AdvanceTo(ETickState::FinalizeMips, ETickThread::Render)`（`Texture2DArrayDynamicStreaming.cpp:131`）。`FinalizeMips`（`Texture2DArrayDynamicStreaming.cpp:135`）再次校验完成状态与数据量，把 slice 拷进中间 RHI 纹理，释放请求，最后 `AdvanceTo(Done)`。

**关键数据**：每个 mip 的目标缓冲区在 `IssueNoStreamingMipRequest` 中一次性分配为 `FMemory::Malloc(BulkDataSize, 16)`，并放进 `NoStreamingMipsData`（`Texture2DArrayDynamicStreaming.cpp:377`、`Texture2DArrayDynamicStreaming.cpp:378`）。`NoStreamingMipsData` 的索引与 `FinalizeMips` 的 `DataMipIndex = MipIndex - NoStreamingFirstLODIdx` 对齐；请求对象放进 `NoStreamingRequests`（`Texture2DArrayDynamicStreaming.h:85`）。

> **图 2**：修复后的异步状态机：从发请求到 Finalize，worker 只做两小段工作（完整 SVG 见同名 HTML）

**为什么用 `AdvanceTo(AllocateMips, Async)` 而不是新加一个 Poll 状态**：非流送 mip 的请求归 allocator 自己持有，不归 `FTextureMipDataProvider`。复用同一状态可以在不改变 `FTextureStreamIn` 状态机枚举的前提下表达“还没完，稍后再进”；`GetMipAllocatorThread(AllocateMips)` 仍返回 Async，语义一致（`TextureStreamIn.cpp:254`）。

**完成标志的竞态**：回调先递减 Pending，再写 `bNoStreamingComplete`。如果 `AllocateMips` 观察到 Pending 已归零、但完成标志尚未写入，会走 `!bNoStreamingComplete` 分支再次 `AdvanceTo(AllocateMips)`（`Texture2DArrayDynamicStreaming.cpp:124`）；随后回调的 `Reschedule` 会驱动下一次 Tick。这个窗口不会丢唤醒，也不会提前进入 Finalize。

## 3. 请求生命周期、失败回退与取消

所有失败路径都收敛到同一个原则：先把 `Counter` / `Pending` 归零，再决定回退；所有释放路径都先确保回调结束，再销毁对象。

| 场景 | 检测点 | 处理 | 证据 |
|---|---|---|---|
| mip 不可异步（已驻留、内联、压缩、磁盘尺寸与逻辑尺寸不一致） | 发请求前的 `CanLoadMipAsync` | 整批走同步回退，不混合 | `Texture2DArrayDynamicStreaming.cpp:19`、`Texture2DArrayDynamicStreaming.cpp:339` |
| AssetLODBias 导致首 mip 不一致 / 没有可加载 mip | `IssueNoStreamingMipRequests` 预检 | `UseSyncFallback` + `LoadNoStreamingMipsSync` | `Texture2DArrayDynamicStreaming.cpp:331`、`Texture2DArrayDynamicStreaming.cpp:333` |
| `CreateStreamingRequest` 返回空 | `IssueNoStreamingMipRequest` | 归还计数、置 `bNoStreamingFailed`、中断发请求；Pending 归零后回退 | `Texture2DArrayDynamicStreaming.cpp:407`、`Texture2DArrayDynamicStreaming.cpp:410` |
| 回调发现取消、无结果或短读 | IO 回调 | 置 `bNoStreamingFailed`，`MarkMountedStateDirty`，Pending 归零后回退 | `Texture2DArrayDynamicStreaming.cpp:385`、`Texture2DArrayDynamicStreaming.cpp:388`、`Texture2DArrayDynamicStreaming.cpp:391` |
| 运行期 IO 失败 | `AllocateMips` 看到 Pending 归零且 `bNoStreamingFailed` | `UseSyncFallback("runtime IO failure")` + 同步重读 | `Texture2DArrayDynamicStreaming.cpp:116`、`Texture2DArrayDynamicStreaming.cpp:118` |
| 状态机被取消 | `Cancel` / `Abort` | 取消并等待所有请求，再释放缓冲区 | `Texture2DArrayDynamicStreaming.cpp:474`、`Texture2DArrayDynamicStreaming.cpp:435` |

**同步回退的形状**：`LoadNoStreamingMipsSync`（`Texture2DArrayDynamicStreaming.cpp:421`）保留原语义，调用 `UTexture2DArray::GetMipData`。它仍然会阻塞，但只在两类情况下发生：整批 mip 不满足异步条件（在建请求前就能判定），或运行期 IO 失败（已经付出一次失败 IO 的代价）。这是把“正确性优先”放在“彻底异步”之前的取舍；正常资产不会走这条路径。

**取消与 Abort 的钩子**：`FTextureMipAllocator` 新增了两个默认虚函数 `AbortPollMips()` 与 `HasPendingIO()`（`TextureMipAllocator.h:80`、`TextureMipAllocator.h:81`）。`FTextureStreamIn::Abort` 在 `bIsPollingMipData` 或 `MipAllocator->HasPendingIO()` 为真时启动 abort task（`TextureStreamIn.cpp:521`、`TextureStreamIn.cpp:526`）；`FAbortPollMipsTask::DoWork` 在持有更新锁的情况下调用 allocator 的 `AbortPollMips()`（`TextureStreamIn.cpp:545`）。

**为什么释放必须先等回调**：回调按引用捕获 `Counter`，并捕获 `this` 用于日志与 `MarkMountedStateDirty`（`Texture2DArrayDynamicStreaming.cpp:383`）。如果 allocator 在请求仍在飞时析构，回调就是 UAF。`ReleaseNoStreamingRequests` 先 `PollCompletion`，未完成则 `Cancel` + `WaitCompletion`，然后才 `Reset`（`Texture2DArrayDynamicStreaming.cpp:439`、`Texture2DArrayDynamicStreaming.cpp:441`、`Texture2DArrayDynamicStreaming.cpp:445`）。

**取消线程的选择**：`GetCancelThread` 在仍有请求或 Pending 时返回 `Async`；否则若存在中间纹理返回 `Render`；否则若有临时 mip 数据返回 `Async`；都没有则 `None`（`Texture2DArrayDynamicStreaming.cpp:486`）。这让 `Cancel` 总能在正确的线程上等待回调。

> **图 3**：单个请求的生命周期：成功、失败、取消都收敛到“先等回调结束，再释放”（完整 SVG 见同名 HTML）

## 4. 改动清单（CL 1195928）

CL 1195928（2026-10-20，bug 1266750）只改了 4 个文件；异步修复的主体在前两个文件，后两个是状态机/allocator 的协作钩子。

| 文件 | 版本 | 改动 | 关键位置 |
|---|---|---|---|
| `Engine/Private/Streaming/Texture2DArrayDynamicStreaming.cpp` | #5 | 异步状态机、请求发起/释放/取消、同步回退、完成校验 | `Texture2DArrayDynamicStreaming.cpp:51`、`Texture2DArrayDynamicStreaming.cpp:312`、`Texture2DArrayDynamicStreaming.cpp:372`、`Texture2DArrayDynamicStreaming.cpp:435` |
| `Engine/Private/Streaming/Texture2DArrayDynamicStreaming.h` | #3 | 新增请求数组、Pending 计数、完成/失败/回退标志、声明 | `Texture2DArrayDynamicStreaming.h:37`、`Texture2DArrayDynamicStreaming.h:85`、`Texture2DArrayDynamicStreaming.h:87` |
| `Engine/Private/Streaming/TextureMipAllocator.h` | #2 | 新增 `AbortPollMips()` / `HasPendingIO()` 默认实现 | `TextureMipAllocator.h:80`、`TextureMipAllocator.h:81` |
| `Engine/Private/Streaming/TextureStreamIn.cpp` | #2 | `Abort` 检查 allocator 的 pending IO；abort task 调用 allocator 钩子 | `TextureStreamIn.cpp:521`、`TextureStreamIn.cpp:545` |

**头文件成员**：`NoStreamingMipsData`（`Texture2DArrayDynamicStreaming.h:83`）、`NoStreamingRequests`（`Texture2DArrayDynamicStreaming.h:85`）、`PendingNoStreamingRequests`（`Texture2DArrayDynamicStreaming.h:86`）、`bNoStreamingComplete` / `bNoStreamingFailed` / `bUseSyncFallback` 三个原子标志（`Texture2DArrayDynamicStreaming.h:87`、`Texture2DArrayDynamicStreaming.h:88`、`Texture2DArrayDynamicStreaming.h:90`）、`bNoStreamingLoadStarted` 与 `bMipBuffersAllocated`（`Texture2DArrayDynamicStreaming.h:89`、`Texture2DArrayDynamicStreaming.h:91`）、缓存的 `TextureName`（`Texture2DArrayDynamicStreaming.h:92`）。

**不在 CL 1195928 内的配套改动**：`WaitingQueue.cpp` 的阻塞补偿/让出唤醒逻辑是另一条线（本地 #4 之上仍有未提交 edit），用于 1/1 限流下“worker 阻塞在任务里”时补线程、以及有界唤醒让出者（`WaitingQueue.cpp:356`、`WaitingQueue.cpp:387`、`WaitingQueue.cpp:516`）。它不替代本次异步修复，但两者叠加才能覆盖“其它同步等待”仍可能占死名额的场景。

## 5. 实机验证：编译、跑图与帧率

**编译**：显式重编 S1Game Development，日志 `build_comment_restore_explicit.log` 显示 `[1/4] Compile Module.Engine.28.cpp`（`build_comment_restore_explicit.log:29`）、`[2/4] Compile Module.Engine.76.cpp`（`build_comment_restore_explicit.log:31`）、`[3/4] Link S1Game.exe`（`build_comment_restore_explicit.log:32`）、`BUILD_EXITCODE=0`（`build_comment_restore_explicit.log:40`）。exe/pdb 已替换到包体，产物时间 11:38。

**跑图配置**：NeoRing，`-LoginPort=13789`，`TaskGraph.MaxActiveForegroundWorkers=1`、`TaskGraph.MaxActiveBackgroundWorkers=1`、`TaskGraph.YieldWakeIntervalMs=1`、`TaskGraph.CompensateBlockedWorkers=1`，`LogTexture Log`。脚本为 `loop/run-async-13789.bat`。

**结果**：

| 指标 | 结果 | 证据 |
|---|---|---|
| 日志窗口 | 11:39:03 打开 → 11:49:23 退出 | `Logs/async_13789.log` |
| 异步非流送 mip 命中 | 9 次（均为 `TA_IMP`，1 mip，first LOD 0） | `Logs/async_13789.log:207802` |
| 同步回退 / IO 失败 / Finalize abort | 0 / 0 / 0 | 全日志 grep 无命中 |
| Fatal / Assertion / RenderThread 超时 / Access violation | 0 | 全日志 grep 无命中 |
| 自然退出 | `RequestExit(0, ...)`，wrapper `rc=0` | `Logs/async_13789.log:791538` |

**帧率对比**（Gauntlet `FPS=[...]` 采样；同脚本、同 1/1 限流、同一地图）：

| 版本 | 采样数 | 均值 | 中位 | P5 |
|---|---:|---:|---:|---:|
| 同步基线 | 274 | 64.7 | 56.3 | 35.2 |
| 上一版异步 | 273 | 81.8 | 78.8 | 53.6 |
| 本次最终版 | 273 | 81.0 | 77.3 | 52.9 |

本次与上一版异步结果在跑测噪声内重合；相对同步基线均值 +25%、中位 +37%。本次 `FPS < 30` 的采样 7 个，最低 0.7（加载/切换瞬间的单帧），与上一版异步的 11 个相比没有恶化。

**口径说明**：这是单次实机跑测，不是正式 benchmark；FPS 来自 Gauntlet 控制器日志，未做固定场景截取。异步命中 9 次表示 9 次 stream-in 事件，不是 9 个 mip。

## 6. 与原生 TextureStreaming 的对照

引擎原生的流送 mip 由 `FTexture2DMipDataProvider_IO` 处理，协议已经是异步的；本次修复是把动态 allocator 负责的“非流送 mip”补齐到同一协议。

| 维度 | 原生 `FTexture2DMipDataProvider_IO` | 改前 Dynamic allocator | 改后 Dynamic allocator |
|---|---|---|---|
| 读取方式 | `CreateStreamingRequest` 异步 | `GetMipData` 同步 `GetCopy` | `CreateStreamingRequest` 异步 |
| 挂起机制 | `SyncOptions.Counter` + `Reschedule` | 无；直接阻塞 | `SyncOptions.Counter` + `Reschedule` |
| 取消 | `AbortPollMips` / `ClearIORequests` | 无请求可取消 | `AbortPollMips` / `Release` 等待 |
| 失败处理 | `bIORequestCancelled` + `MarkMountedStateDirty` | `GetMipData` 失败即取消 | `bNoStreamingFailed` + 回退 + `MarkMountedStateDirty` |
| 线程 | Async，不阻塞 | Async，但阻塞在 `Event->Wait()` | Async，不阻塞 |

原生 provider 的关键代码可以逐条对照：`GetMips` 在发请求前 `SyncOptions.Counter->Increment()`（`Texture2DMipDataProvider_IO.cpp:102`），随后 `OwnerMip.BulkData.CreateStreamingRequest(...)`（`Texture2DMipDataProvider_IO.cpp:123`）；回调 `AsyncFileCallBack` 递减计数，归零时调用 `RescheduleCallback()`（`Texture2DMipDataProvider_IO.cpp:194`、`Texture2DMipDataProvider_IO.cpp:204`、`Texture2DMipDataProvider_IO.cpp:213`）；`PollMips` 负责收尾（`Texture2DMipDataProvider_IO.cpp:139`），`AbortPollMips` 负责取消（`Texture2DMipDataProvider_IO.cpp:159`），`ClearIORequests` 在未完成时 `Cancel` + `WaitCompletion`（`Texture2DMipDataProvider_IO.cpp:218`、`Texture2DMipDataProvider_IO.cpp:226`）。

**为什么不能直接把非流送 mip 交给 provider**：provider 的 `GetMips` 只处理 `[PendingFirstLODIdx, CurrentFirstLODIdx)` 范围内的流送 mip（`Texture2DMipDataProvider_IO.cpp:77`、`Texture2DMipDataProvider_IO.cpp:139`）；非流送 mip 位于 `NumNonStreamingLODs` 之后、由 allocator 在 Finalize 前填充，不在 provider 的窗口里。职责边界决定了 allocator 必须自己完成这段异步读取。

> **图 4**：同一段时间轴上的 worker 占用：同步把整段 IO 变成阻塞，异步只在首尾工作（完整 SVG 见同名 HTML）

## 7. 已知边界与未覆盖风险

- **Packed mip tail（`NumMipsInTail > 1`）**：异步与同步使用同一个 `LoadableMips` 计算（`Texture2DArrayDynamicStreaming.cpp:324`、`Texture2DArrayDynamicStreaming.cpp:325`），加载集合一致；但 `GetNoStreamingData` 的 `MipView.GetSize() / ArrayCount` 对 packed mip 并不成立，源码里保留了“This check works for normal mips but not packed mips”的注释（`Texture2DArrayDynamicStreaming.cpp:531`）。这是既有边界，不是本次引入，也未在本次跑测覆盖。
- **Off-thread `GetPlatformData()`**：`IssueNoStreamingMipRequests` 在 Async 线程调用 `Texture2DArray->GetPlatformData()`（`Texture2DArrayDynamicStreaming.cpp:315`）。改前的 `GetMipData` 也在 Async 线程做同样的事，线程模型没有变差；但 editor/streaming 的极端时序未做压力测试。
- **回退路径会重复 IO**：如果部分请求成功、后续请求失败，成功缓冲会被 `Reset`，整批非流送 mip 同步重读（`Texture2DArrayDynamicStreaming.cpp:430`、`Texture2DArrayDynamicStreaming.cpp:432`）。正确性没问题，失败路径的 IO 量翻倍。
- **内存驻留窗口变长**：非流送 mip 的缓冲区从发请求一直驻留到 Finalize；总量与同步路径相同，但驻留时间与其它流送工作重叠。45 MiB 级别的资产需要关注峰值。
- **未覆盖的复现矩阵**：本次只跑了 NeoRing + 1/1 限流；45 MiB / 白厄前言 / 4 核原始复现未重跑，取消与低内存压力路径未做专项压测。
- **调度器补偿是独立改动**：`WaitingQueue.cpp` 的补偿/唤醒逻辑不在 CL 1195928 内；如果其它路径仍有阻塞等待，1/1 限流下仍可能被占死。不要把本次异步修复当成所有 TaskGraph 死锁的通用解。

## 8. 复现与验证 Checklist

1. **编译**：`S1Game` Development，确认日志出现 `Compile Module.Engine.*` 与 `Link S1Game.exe`，且 `BUILD_EXITCODE=0`；把 `Binaries/Win64/S1Game.exe` 与 `.pdb` 替换到包体。
2. **跑图**：使用 `loop/run-async-13789.bat`（NeoRing、1/1 限流、`LogTexture Log`），等待自然退出，不要在中途手动结束。
3. **命中确认**：`grep -c "async non-streaming mip IO" Logs/async_13789.log`，期望 > 0；本次为 9。
4. **失败确认**：`grep -i "sync fallback\|non-streaming mip IO failed\|failed to create async request\|FinalizeMips aborted\|Fatal\|timed out waiting for RenderThread\|Access violation" Logs/async_13789.log`，期望 0 行。
5. **退出确认**：`grep "RequestExit" Logs/async_13789.log`，期望包含 `RequestExit(0`；wrapper 期望 `rc=0`。
6. **性能确认**：从 `LogGameTestGauntletController: ... FPS=[...]` 提取采样，和同步基线同脚本对比；关注均值/中位与 `<30` 采样数，不要只看单帧峰值。
7. **回归排查**：如果出现大量 `sync fallback`，先看 `CanLoadMipAsync` 的四个条件（大小、磁盘大小、是否已加载、是否压缩）；如果出现 `FinalizeMips aborted: non-streaming mip IO not complete`，检查完成标志与 `Reschedule`；如果卡死，检查 `HasPendingIO` / abort 钩子是否覆盖到 allocator。

## 9. 参考

**本次改动（CL 1195928）**

- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.cpp:51` — `AllocateMips` 异步状态机。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.cpp:312` — `IssueNoStreamingMipRequests` 预检与发请求。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.cpp:372` — `IssueNoStreamingMipRequest` 单请求与回调。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.cpp:435` — `ReleaseNoStreamingRequests` 等待后释放。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.cpp:474` — `Cancel`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DArrayDynamicStreaming.h:85` — 请求数组与计数成员。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\TextureMipAllocator.h:80` — allocator abort/pending 钩子。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\TextureStreamIn.cpp:521` — Abort 启动条件。

**引擎既有异步协议**

- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Public\Streaming\TextureMipDataProvider.h:47` — `FTextureUpdateSyncOptions`；`TextureMipDataProvider.h:54` Counter；`TextureMipDataProvider.h:57` RescheduleCallback。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Internal\Streaming\RenderAssetUpdate.inl:58` — `TaskSynchronization` 挂起。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\TextureStreamIn.cpp:64` — Reschedule 回调。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DMipDataProvider_IO.cpp:102` — 原生 provider 的 Counter++。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DMipDataProvider_IO.cpp:123` — 原生 provider 的 CreateStreamingRequest。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DMipDataProvider_IO.cpp:194` — AsyncFileCallBack。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\Streaming\Texture2DMipDataProvider_IO.cpp:218` — ClearIORequests。

**同步阻塞链**

- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\TextureDerivedData.cpp:3448` — `UTexture2DArray::GetMipData`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\TextureDerivedData.cpp:2518` — `TryLoadMipsWithSizes`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Engine\Private\TextureDerivedData.cpp:2584` — `GetCopy`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\CoreUObject\Private\Serialization\BulkData.cpp:878` — `FBulkData::GetCopy`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\CoreUObject\Private\Serialization\BulkData.cpp:1649` — `TryLoadDataIntoMemory`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\CoreUObject\Private\Serialization\BulkDataStreaming.cpp:373` — `OpenReadBulkData`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\CoreUObject\Private\Serialization\BulkDataStreaming.cpp:392` — `Event->Wait()`。

**调度器配套（独立改动）**

- `C:\GR_main\UE5EA\Engine\Source\Runtime\Core\Private\Async\Fundamental\WaitingQueue.cpp:356` — `GetActiveWorkerLimit`。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Core\Private\Async\Fundamental\WaitingQueue.cpp:387` — `ConditionalStandby` 每轮刷新。
- `C:\GR_main\UE5EA\Engine\Source\Runtime\Core\Private\Async\Fundamental\WaitingQueue.cpp:516` — `ShouldYieldWorker`。

**跑测与编译证据**

- `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\build_comment_restore_explicit.log:29` — 编译。
- `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\build_comment_restore_explicit.log:32` — 链接。
- `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\build_comment_restore_explicit.log:40` — exit 0。
- `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\loop\Logs\async_13789.log:207802` — 首次异步命中。
- `C:\GR_main\S1Game\Saved\S1GameAutomationDebug\loop\Logs\async_13789.log:791538` — RequestExit(0)。
