# UE5 AddPrimitive 与 RemovePrimitive 并发：SceneUpdateCommandQueue 空 SceneInfo 崩溃与 claim 作用域修复

对象：S1Game UE5 fork（release CL 1183280，崩溃版本 build 0.1178669 Development Win64）的 primitive 增删链路。
现象：渲染线程在 `SceneUpdateCommandQueue.h:531` 解引用空 `FPrimitiveSceneInfo` 崩溃，性质为访问违规而非断言失败。
根因：异步建代理期间 `SceneProxy` 先于 `SceneInfo` 发布（`PrimitiveComponent.cpp:5788` 早于 `RendererScene.cpp:2323`），
而 `BatchRemovePrimitivesInternal` 只校验 proxy 非空、不校验 SceneInfo。
修复：把 `AddPrimitiveClaim` 的持有期对齐到 `ENQUEUE_RENDER_COMMAND` 入队点，Remove 侧等待其释放，
使 Add-before-Delete 不变量由构造保证。
验证：8 小时 43 回合 soak 零异常；故障注入 76,726 次 × 15 ms 证明前置条件在本负载不成立。
工具链：Perforce `//GR/release`、UBT Test 构建（`-EnableLQGAPSystem`）、Gauntlet 自动跑测、TraceMcpS1 离线 utrace 分析。

## 1. 结论：空 SceneInfo 从哪来，怎么修

渲染线程崩在 `SceneUpdateCommandQueue.h:531` 的 `Commands.Emplace(SceneInfo, SceneInfo->GetPersistentIndex())`。
这是该函数里唯一解引用 `SceneInfo` 的地方（`GetPersistentIndex()` 见 `PrimitiveSceneInfo.h:584`，纯成员读取）。

**先定性：是访问违规，不是断言失败。** 判据是正向的 —— `check()` 失败必经 `AssertionMacros.cpp:372` 的
`GError->Logf(TEXT("Assertion failed: ..."))`，必然写日志；而崩溃日志中 `Assertion failed`、`Critical error`、
`Fatal error`、`appError`、`LogOutputDevice: Error`、`LogWindows: Error` 全部 0 次。

注意区分：TAPD 单标题里的 `UNREAL_ASSERT_EXCEPTION(0x00004000)`，其中 `0x4000` 是
`WindowsPlatformCrashContext.cpp:95` 定义的 `AssertExceptionCode`。那是另一个断言类崩溃，与本文的访问违规不是同一件事。

空指针来自 `BatchAddPrimitivesInternal` 内部的发布顺序倒置：

| 行 | 动作 | 此刻外部可见的状态 |
|---|---|---|
| `RendererScene.cpp:2306` | `CreateSceneProxy()`，内部 `PrimitiveComponent.cpp:5788` 的 `AssignSceneProxy()` 已把 proxy 写入 `SceneData` | `GetSceneProxy() != nullptr` |
| `RendererScene.cpp:2323` | `proxy->PrimitiveSceneInfo = SceneInfo` | 此前 `proxy->GetPrimitiveSceneInfo() == nullptr` |

`FPrimitiveSceneProxy::PrimitiveSceneInfo` 初值为 `nullptr`（`PrimitiveSceneProxy.cpp:747`），
而 `BatchRemovePrimitivesInternal` 原本只校验 proxy 非空，于是把 `nullptr` 投给渲染线程。

**为什么一定是 nullptr 而不是悬垂指针**：MallocBinned 的池释放后内存仍映射，读悬垂指针的 `PersistentIndex`
不会 fault（只读到垃圾，崩溃推迟到别处）；只有 nullptr 偏移后仍落在极小地址，才会在 531 行确定性 fault。

修复方向不是给队列加锁，而是把 claim 持有期对齐到 Add 命令入队点：渲染命令按入队顺序在 RT 执行，
只要 Add 已入队，后来者的 Delete 必排在它之后，`EnqueueAdd` 的「Add 必须最先」不变量自动成立。

## 2. 两个危险窗口：为什么只堵住一个会换来另一种崩溃

一句话结论：窗口 A 导致空指针崩溃，窗口 B 导致 Add/Delete 入队顺序颠倒；只处理 A 会把「罕见泄漏」换成「罕见 Dev/Test 断言」。

`BatchAddPrimitivesInternal` 的执行时序（worker 线程，异步关卡注册批次，BatchSize=16）：

1. `RendererScene.cpp:2306` — `CreateSceneProxy()`，内部 `AssignSceneProxy` 已发布 `SceneData.SceneProxy`
2. `RendererScene.cpp:2322` — `new FPrimitiveSceneInfo`，SceneInfo 已分配但尚未挂到 proxy
3. `RendererScene.cpp:2323` — `proxy->PrimitiveSceneInfo =`，双向指针打通
4. `RendererScene.cpp:2374` — `ENQUEUE_RENDER_COMMAND`，Add 命令入队，RT 后续执行 `EnqueueAdd`

对应的 GT 侧路径：组件卸载 → `UnregisterComponent()`（`ActorComponent.cpp:2401`，`AsyncAddInFlight` 在此路径无检查点）
→ `BatchRemovePrimitivesInternal` 读 `GetSceneProxy()` / `GetPrimitiveSceneInfo()`
→ `EnqueueDelete(SceneInfo)` 在 `SceneUpdateCommandQueue.h:531` 解引用。

### 窗口 A：proxy 已发布、SceneInfo 未回填（步骤 1 → 3 之间）

GT 在此刻 Remove，读到 proxy 非空而 SceneInfo 为空，投出 `nullptr`，531 行解引用崩溃。
这是线上那次崩溃命中的窗口，跨度亚微秒级。

### 窗口 B：SceneInfo 已链接、Add 命令未入队（claim 释放 → 步骤 4）

`ENQUEUE_RENDER_COMMAND(AddPrimitiveCommand)` 在 `RendererScene.cpp:2374`，位于整个循环结束之后；
而改前 claim 在每次循环迭代末尾（`RendererScene.cpp:2368` 之后）就释放。BatchSize=16 下这个窗口是毫秒级，
比窗口 A 大几个数量级，窗口 B 完全失去保护。

GT 在此刻 Remove 拿到的是有效 SceneInfo，于是正常入队 Delete —— 但它可能排在 worker 的 Add 之前。
RT 执行到 `EnqueueAdd` 时会撞上两道断言：

| 断言 | 位置 |
|---|---|
| `check(CommandSlots.Find(SceneInfo) == nullptr)`（注释原文：Add should always be the first command） | `SceneUpdateCommandQueue.h:170` |
| `check(!Command.bDeleted)` | `SceneUpdateCommandQueue.h:176` |

这意味着：只在 Remove 侧做判空，窗口 A 的结果从「崩溃」变成「代理泄漏 + 场景残留 primitive」；
而等待若只等到 claim 释放（未覆盖入队），就会必然落进窗口 B，把崩溃从 531 行搬到 `EnqueueAdd`。
Shipping 下 `check` 被编掉，同帧 Add+Delete 由引擎既有的同帧增删逻辑正确处理成「删除」，所以该断言只在 Dev/Test 触发。

## 3. 修复：claim 作用域对齐入队点

一句话结论：把逐 primitive 的 RAII claim 换成批量持有，析构点落在 `ENQUEUE_RENDER_COMMAND` 之后，
Add-before-Delete 不变量由构造保证，不依赖时序运气。

修复后 claim 持有期覆盖的四个阶段（`AddPrimitiveClaim != 0` 全程成立）：
`CreateSceneProxy` → `new SceneInfo` → 回填 `proxy->SceneInfo` → `ENQUEUE_RENDER_COMMAND`。
关键在于第四步在 claim 释放之前完成。

### 3.1 Add 侧：FAddPrimitiveClaimScope

```cpp
// RendererScene.cpp:2250  声明于循环之前，析构于函数退出（入队之后）
struct FAddPrimitiveClaimScope
{
    ~FAddPrimitiveClaimScope()
    {
        for (FThreadSafeCounter* Counter : Claimed)
        {
            Counter->Decrement();
        }
    }

    // 抢到则登记、持有到本作用域结束；没抢到立即归还
    bool TryClaim(FThreadSafeCounter& Counter)
    {
        if (Counter.Increment() == 1)
        {
            Claimed.Add(&Counter);
            return true;
        }
        Counter.Decrement();
        return false;
    }

    TArray<FThreadSafeCounter*, SceneRenderingAllocator> Claimed;
};
```

**顺带修掉的一个真 bug**：原版 RAII 构造时无条件 `Increment()`，析构却只在 `bAcquired` 时 `Decrement()` ——
每次抢占失败都会永久抬高计数，该 primitive 此后所有 `AddPrimitive` 都抢不到 claim 并 `continue`，永久不可见。
新版在 `TryClaim` 内「抢不到立即归还」，彻底消除。

### 3.2 Remove 侧：WaitForInFlightAddPrimitive

定义于 `RendererScene.cpp:2880`，调用点 `RendererScene.cpp:2960`，在读 proxy 之前：

| 阶段 | 做法 | 依据 |
|---|---|---|
| 短自旋 | 256 次 `FPlatformProcess::Yield()` | 正常窗口（`new` + 指针回填）亚微秒级 |
| 让出时间片 | 转 `FPlatformProcess::YieldThread()` | `Yield()` 实为 `_mm_pause()`（`GenericPlatformProcess.h:819`），不让时间片；持有者被调度走时纯自旋等不到 |
| 上限 | 挂钟 50 ms，超时记 Warning | 兜底不挂死；超时后由判空分支接管 |

踩过的坑：首版用 `Yield()` 做全部等待。它是 CPU 暂停指令，不释放 CPU —— 这是个「看着对、跑起来也不报错」的写法，
日志和跑测都不会暴露，只能靠读 `GenericPlatformProcess.h:819` 的实现发现。

### 3.3 判空兜底与现场记录

- `RemovePrimitiveSceneInfo_RenderThread`（`RendererScene.cpp:2842`）入口判空：为空则跳过 `EnqueueDelete`，止崩。
- `LogMissingSceneInfoOnRemove`（`RendererScene.cpp:2915`）打印
  `Component / Owner / Proxy / bRegistered / bRenderStateCreated / bRenderStateDirty / AsyncAddInFlight / World`。
  其中 `AsyncAddInFlight` 可直接区分「在途」与其它来源。
- 保留 `ReleaseSceneProxy()` 与 `DestroyCommands.Add(...)` —— 跳过会漏掉 `AttachmentCounter` 的递减，
  导致 `FinishDestroy` 的 `check(AttachmentCounter == 0)` 二次崩溃。
- 全部日志为 Warning 级，与 CL 1201185 既有的 `[SceneProxy] Concurrent AddPrimitive skipped` 一致。

## 4. FPrimitiveSceneDesc 路径：一个默认关闭的定时炸弹

一句话结论：CL 1201185 新增的二次确认 `if (Primitive->GetSceneProxy()) continue;` 对 `FPrimitiveSceneDesc`
路径是误判，会让首次合法 Add 被整个跳过。

`FPrimitiveSceneDesc` 的 `PrimitiveComponentInterface` 默认为 `nullptr`（`PrimitiveSceneDesc.h:28`），
头文件注释明确写着：不提供 ComponentInterface 时，ProxyDesc 必须已由调用方创建并传入。
项目侧 `S1WeaponAvatarComponent.cpp:1507` 正是这种用法 —— 在调用 `AddPrimitive` 之前就
`PrimitiveSceneData->SceneProxy = new FStaticMeshSceneProxy(...)`。

于是「proxy 非空」在这条路径上恒成立，二次确认必然命中 → Add 被跳过 → `RendererScene.cpp:2323` 的回填永不执行
→ 卸载时投出空 SceneInfo。

修正方式是按路径区分判据（`RendererScene.cpp:2298`）：

```cpp
FPrimitiveSceneProxy* ExistingSceneProxy = Primitive->GetSceneProxy();
const bool bAlreadyAdded = ExistingSceneProxy &&
    (Primitive->GetPrimitiveComponentInterface() != nullptr || ExistingSceneProxy->GetPrimitiveSceneInfo() != nullptr);
```

| 路径 | 判据 | 行为 |
|---|---|---|
| 组件首次 | proxy == null | 继续（不变） |
| 组件并发 / 已完成 | proxy != null | 跳过（不变） |
| **desc 首次** | proxy 非空但 SceneInfo 为空 | **继续 → 回填 SceneInfo（修复）** |
| desc 重复 Add | proxy 与 SceneInfo 都非空 | 跳过（保留原防重复语义） |

**当前未触发**：`avatar.weapon.EnableApproxWeaponMesh` 默认值为 2（`S1WeaponAvatarComponent.cpp:54`），
走 StaticMeshComponent 分支；改成 1 才走 desc 路径。属于「改个 CVar 就必崩」的定时炸弹，
无法通过跑测发现，只能靠代码推演。

## 5. 验证：故障注入与 soak，以及证据的边界

一句话结论：修复的正确性由构造保证且零成本；但「修复前崩 / 修复后不崩」的对照实验给不出来 ——
前置条件在本测试负载下不成立，且这一点已被 76,726 次留痕证明不是仪表故障。

### 5.1 故障注入：把微秒级巧合变成可复现

新增 `r.ZXB.AddPrimitive.InjectDelayMs`（默认 0，生产零开销；实验值 8 / 15 ms），
注入点在 claim 持有期内、Add 入队之前 —— 正是窗口 B 的位置。
**关键设计：注入点自身必须打日志留痕**，否则「等待 0 命中」分不清是仪表坏还是真没发生。

两个仪表坑（均已踩中并修正）：

1. 首版注入点放在抢 claim 之前 → 拉长的不是危险窗口。
2. 首版用 `!IsInGameThread()` 判线程。`ThreadingBase.cpp:227` 的 `IsCurrentTag` 是精确相等（`ActiveTaskTag == InTag`），
   而 `CoreGlobals.h:762` 定义 `EParallelGameThread = EGameThread | EParallelThread` —— 二者不等；
   `ThreadingBase.cpp:257` 又让 Development 走线程 ID 比较、Test 走 tag 判定，两配置语义不同。
   改用 `FPlatformTLS::GetCurrentThreadId() != GGameThreadId`，不依赖 tag 语义。

实验结果（用例 nr_paotu，最高流送负载）：

| 观测项 | 结果 |
|---|---|
| 注入实际执行 | **76,726 次** × 15 ms ≈ 累计 1,150 秒人为放大窗口 |
| 等待命中 / 超时 | **0 / 0** |
| `Concurrent AddPrimitive skipped` | **0** —— claim 持有变长未丢弃任何合法 Add |
| 崩溃 / 空 SceneInfo / 原断言 | 0 |

**结论**：GT-直接 `UnregisterComponent()` 去移除一个正在异步添加中的 primitive，在本负载下不发生。
合理解释是 CL 1201185 的 `AsyncAddInFlight` 已挡住帧末并行（`ActorComponent.cpp:2565`）
与批量重注册（`StaticMeshResources.cpp:152`）两条路；而 GT-直接卸载需要「关卡边加载边卸载」，
被 `World.cpp:4833` 的 `FinishAsyncSendAllEndOfFrameUpdates()` 与 AddToWorld/RemoveFromWorld 的串行化排除。

### 5.2 8 小时 soak

用例 `rush01`（循环，每回合 `KillAllDS` + 重进 → 完整关卡重载），生产配置：

| 项 | 结果 |
|---|---|
| 时长 / 回合 | 28,811 秒（8 小时）/ 43 回合，`BUDGET-EXHAUSTED` 正常结束 |
| 进程 | `Log file open` = 1（单进程全程不重启） |
| 进对局 / `ExitBattle` | 43 次 / 42 次（第 43 回合被预算截断） |
| 崩溃类信号 | `Assertion failed`、`Fatal error`、`appError called`、`Unhandled Exception`、`CrashContext.runtime-xml` 均 0 |
| 间接退化 | `AttachmentCounter`、`invalid OctreeId`、`Failed to route Create/DestroyRenderState`、`already marked for async update`、`ensureAlways` 均 0 |

间接退化那组是专门验计数器修复的：若 `Decrement` 逻辑写错，primitive 会逐渐消失或相关断言会刷 —— 43 轮下来一次未出现。

### 5.3 诚实边界

- **无法给出「修复前崩 / 修复后不崩」的对照**：前置条件在本 harness 不可复现。原 TAPD 单记录的影响设备数为 1，本身极罕见。
- **最终提交物未经跑测**：soak 与注入实验跑的是重构前的二进制；最终版（提取辅助函数 + 删除注入脚手架 + 日志降级）
  只通过了编译。重构为行为等价变更，但严格意义上提交物本身未跑测。
- **日志级别变更的连带影响**：日志从 Error 降为 Warning 后，不再被「Error 全 0」这类验收判据捕获。
  后续跑测要验证防线是否命中，判据必须改为直接 grep `[ZXB]` 字符串。

## 6. 一个 primitive 的一生（修复后）

把前面各节串成一条线，跟着一个 primitive 从关卡流入走到被移除：

1. **关卡流入 AddToWorld** — `UWorld::UpdateLevelStreaming`；`World.cpp:4833` 先 `FinishAsyncSendAllEndOfFrameUpdates`
2. **CreateRenderState_Concurrent** — 置 `bRenderStateCreated=true`，代理交异步队列；出现「已注册 / 无代理」第三态
3. **FAsyncAddPrimitiveQueue::Launch** — `AsyncAddInFlight.Set(1)`（整批），`AsyncRegisterLevelContext.cpp:468`
4. **worker: BatchAddPrimitivesInternal** — `TryClaim` → claim=1；建代理、建 SceneInfo、入队 Add；claim 覆盖全程（本次修复）
5. **RT: EnqueueAdd(SceneInfo)** — Add 成为该 SceneInfo 的第一条命令；`SceneUpdateCommandQueue.h:170/176` 两道断言成立
6. **GT: Reset → AsyncAddInFlight.Set(0)** — 脏位重新挂回帧末更新，`AsyncRegisterLevelContext.cpp:443`
7. **GT: 组件卸载 → RemovePrimitive** — claim==0 直接通过；claim!=0 则等待，`RendererScene.cpp:2960`
8. **RT: EnqueueDelete(SceneInfo)** — 顺序必在 Add 之后 → 同帧增删由引擎既有逻辑处理；`SceneUpdateCommandQueue.h:531` 不再收到 nullptr

关键不变量落在第 4~5 步之间：claim 在 Add 命令入队之后才释放，因此第 7 步的 GT Remove 要么直接通过
（claim 已为 0，意味着 Add 必已入队），要么等待到 claim 为 0 —— 两种情况下第 8 步的 Delete 都排在 Add 之后。

## 7. 排查 Checklist：再遇同类崩溃怎么定位

1. **先定性：断言还是访问违规。** 不要靠「栈里有没有 `FDebug::AssertFailed`」—— 栈可能被截断。
   正向判据是 `AssertionMacros.cpp:372` 保证 `check()` 必写日志，所以日志里 `Assertion failed` 为 0 即可排除断言。
2. **PC 落在哪一行要精确到行。** 本例 PC 在 531（解引用）而非 530（`check`），这是区分两类故障的唯一线索。
3. **排除「队列自身不同步」。** `TRobinHoodHashTable` 扩容只重建桶数组，`KeyValueData` 索引稳定追加；
   队列从不 Remove，故 `check(CommandSlot == Commands.Num())` 在单线程路径恒成立。
4. **查发布顺序，而不是只查空指针。** 双向指针（component→proxy→SceneInfo）中任一侧先发布，
   都会制造一个外部可见的不一致窗口。
5. **加仪表时，仪表自己必须留痕。** 「0 命中」在仪表未留痕时毫无意义 —— 分不清是坏仪表还是真没发生。
6. **线程判定不要用 `IsInGameThread()`。** 本 fork 中它在 Development 走线程 ID 比较
   （`ThreadingBase.cpp:257` 的 `newValue = oldValue`）、在 Test 走 `IsCurrentTag`
   （`ThreadingBase.cpp:227`，精确相等而非位测试）。而 `CoreGlobals.h:762` 定义
   `EParallelGameThread = EGameThread | EParallelThread` —— 两种配置语义不同。
   要判「是否真的在 GT」请用 `FPlatformTLS::GetCurrentThreadId() != GGameThreadId`。
7. **确认 trace 仪表确实会被记录。** `SCOPED_NAMED_EVENT` 展开含 `TRACE_CPUPROFILER_EVENT_SCOPE`
   （`PlatformMisc.h:181`），且 `ENABLE_NAMED_EVENTS` 定义为 `(!UE_BUILD_SHIPPING && 1)`（`PlatformMisc.h:33`），
   Test 下启用 —— 但 rush01 这类脚本不含 `Trace.Start`，trace 不落盘，查不到不等于没发生。

## 8. 已知问题与技术债

- **口子 2（desc 路径）只在代码层面修正，无法跑测验证** —— `avatar.weapon.EnableApproxWeaponMesh`
  默认 2 走不到该分支。真正的验收时刻是有人把它改成 1。
- **GT-直接卸载在途组件这条路，`AsyncAddInFlight` 仍无检查点。** 全仓仅两处检查
  （`ActorComponent.cpp:2565`、`StaticMeshResources.cpp:152`）。本文的 claim 等待是在 Remove 侧兜住它，而非在源头阻止。
- **50 ms 超时是权衡产物**：超时后进入判空分支，意味着放弃等待并接受「代理泄漏 + 场景残留」。
  等待更久会拉长 GT 卡顿，等待更短会更早退化。该阈值未经真实命中验证（命中率为 0）。
- **`SceneUpdateCommandQueue` 的移动构造存在潜伏不一致**：`SceneUpdateCommandQueue.h:115-121` 只搬运 `Commands`，
  目标对象的 `CommandSlots` 为默认空。目标若被 enqueue，`check(CommandSlot == Commands.Num())` 必失败（0 == N）。
  当前因目标只读而未暴露 —— 这恰好是会打在 530 行的故障，反向印证本次崩的是 531。

## 9. 行号基线与参考

**行号基线**：除特别标注外，所有 `RendererScene.cpp` 行号对齐的是 depot #62 + 本次修改后的工作区状态
（文件共 8,381 行）。崩溃栈中的原始行号（`RendererScene.cpp:2795` / `RendererScene.cpp:2857`）对齐的是 build 0.1178669 当时的版本；
该文件此后经 CL 1193034 / 1201185 两次改动，行号已整体下移。
`SceneUpdateCommandQueue.h` 自分支以来未变（`#1`），故 531 行号在两个版本中一致。

### 核心源码位置

| 文件:行 | 内容 |
|---|---|
| `SceneUpdateCommandQueue.h:531` | 崩溃点：`Commands.Emplace(SceneInfo, SceneInfo->GetPersistentIndex())` |
| `SceneUpdateCommandQueue.h:530` | `check(CommandSlot == Commands.Num())` |
| `SceneUpdateCommandQueue.h:170` / `SceneUpdateCommandQueue.h:176` | `EnqueueAdd` 的 Add-first 两道断言 |
| `RendererScene.cpp:2250` | `FAddPrimitiveClaimScope` 定义 |
| `RendererScene.cpp:2298` | 按路径区分的二次确认 `bAlreadyAdded` |
| `RendererScene.cpp:2306` / `RendererScene.cpp:2323` / `RendererScene.cpp:2374` | 建代理 / 回填 SceneInfo / 入队 Add |
| `RendererScene.cpp:2842` | `RemovePrimitiveSceneInfo_RenderThread` 入口判空 |
| `RendererScene.cpp:2880` / `RendererScene.cpp:2960` | `WaitForInFlightAddPrimitive` 定义 / 调用 |
| `RendererScene.cpp:2915` | `LogMissingSceneInfoOnRemove` |
| `PrimitiveComponent.cpp:5788` | `AssignSceneProxy()` —— proxy 的发布点 |
| `PrimitiveSceneProxy.cpp:747` | `PrimitiveSceneInfo(nullptr)` 初值 |
| `PrimitiveSceneInfo.h:584` | `GetPersistentIndex()` |
| `PrimitiveSceneDesc.h:28` | `PrimitiveComponentInterface = nullptr` 默认值 |
| `S1WeaponAvatarComponent.cpp:1507` / `S1WeaponAvatarComponent.cpp:54` | desc 路径预置 proxy / CVar 默认值 2 |
| `AsyncRegisterLevelContext.cpp:468` / `AsyncRegisterLevelContext.cpp:443` | `AsyncAddInFlight` 置位 / 清位 |
| `ActorComponent.cpp:2565` / `ActorComponent.cpp:2401` | 帧末更新的在途检查 / `DestroyRenderState_Concurrent` |
| `StaticMeshResources.cpp:152` | 批量重注册的在途检查 |
| `World.cpp:4833` | `UpdateLevelStreaming` 开头的 `FinishAsyncSendAllEndOfFrameUpdates()` |
| `LevelTick.cpp:1151` | 帧末并行 `DoDeferredRenderUpdates_Concurrent()` |
| `ThreadingBase.cpp:227` / `ThreadingBase.cpp:257` | `IsCurrentTag` 精确相等 / Development 的线程 ID 回退 |
| `CoreGlobals.h:762` | `EParallelGameThread = EGameThread \| EParallelThread` |
| `PlatformMisc.h:33` / `PlatformMisc.h:181` | `ENABLE_NAMED_EVENTS` / `SCOPED_NAMED_EVENT` 展开含 trace |
| `GenericPlatformProcess.h:819` | `Yield()` = `_mm_pause()`，不让时间片 |
| `AssertionMacros.cpp:372` | `check()` 失败必写 `Assertion failed` 日志 |
| `WindowsPlatformCrashContext.cpp:95` | `AssertExceptionCode = 0x4000` |

### 相关文档

- `UE5-异步AddToWorld非阻塞取消-SceneProxy重复创建-在途批次持有机制` —— 前作，讲 CL 1201185 的
  `AsyncAddInFlight` 机制；本文是其续篇，处理该机制未覆盖的 Remove 侧。
