# UE5 异步 AddToWorld 非阻塞取消：在途建代理与帧末更新并发导致 SceneProxy 重复创建

**对象**：S1Game UE5.5.4 fork（release CL 1183280 / main@1137756）的异步关卡注册链路。**现象**：`checkf(!Primitive->GetSceneProxy())`（Primitive has already been added to the scene!）在 World Partition Cell 流入时崩溃。**根因**：CL 917608 的非阻塞取消让在途建代理任务脱离 AddToWorld，与同帧帧末更新对同一组件并发调用 AddPrimitive。**修复**：异步批次持有在途组件（`AsyncAddInFlight`），帧末更新延后到批次收尾；GT 不等待 worker。**验证**：nr_sfxai 3 次 CASE-FINISHED、0 崩溃；nr_paotu A/B 各 3 局，平均帧 −2.6%，无退化。

## 1. 结论：两次 SceneProxy 创建的根因与修复

断言 `!Primitive->GetSceneProxy()` 不是普通的线程竞争。它是两个在引擎里各自成立的假设，在**非阻塞取消**引入后同时失效的结果：帧末更新假设“没有代理 == 从未添加过”，异步注册假设“任务一定在 AddToWorld 返回前结束”。

根因链只有五步：

1. 关卡注册时 `CreateRenderState_Concurrent` 先置 `bRenderStateCreated = true`（`ActorComponent.cpp:2123`），再把 AddPrimitive 交给异步队列（`PrimitiveComponent.cpp:732`）—— 出现“已注册、无代理”的第三态。
1. CL 917608 在 AddToWorld 时间片用完时只发取消信号就退出（`World.cpp:3617`、`World.cpp:3631`），在途迭代不被中断，任务脱离 AddToWorld 继续跑。
1. 同一帧的帧末更新处理到同一组件：`UpdatePrimitiveTransformInternal` 读到 `GetSceneProxy() == nullptr`，走“从零添加”分支 `AddPrimitive`（`RendererScene.cpp:2570-2571`）。
1. 异步 worker 和帧末 worker 都进入 `BatchAddPrimitivesInternal`，都通过了 checkf 之前的判定。
1. 后到者执行到 `checkf(!Primitive->GetSceneProxy())`（`RendererScene.cpp:2305`）时，代理已被对方创建 —— 断言。

修复不改变 CL 917608 的非阻塞取消：低配机上 worker 被调度离核时，GT 等待时间不可控，这个机制必须保留。改动方向是**让异步批次在跑完之前“持有”这批组件**：置位 `AsyncAddInFlight`，帧末更新与批量重注册看到置位就跳过并保留脏位；任务结束后在 GT 的 `Reset` 里清位，并把脏位重新挂回帧末更新列表。GT 全程不等待 worker。

> 一句话：不是“把冲突的两个线程锁起来”，而是让**后到的路径先让路**，等持有者放手后再补做。

## 2. 冲突全景：三条路径如何在同一个 primitive 上汇合

**三条路径（箭头标注传递的数据）**

1. `CreateRenderState_Concurrent`（`PrimitiveComponent.cpp:732`）：把组件交给注册上下文 `Context->AddPrimitive`，入队 `FAsyncAddPrimitiveQueue::NextBatch`（批量 16）。
2. `bRenderStateCreated = true`（`ActorComponent.cpp:2123`）：先置位，代理仍未创建 —— 第三态。
3. 异步任务 `Launch`（GT 不等待）→ worker 执行 `BatchAddPrimitivesInternal`：`checkf → CreateSceneProxy → AssignSceneProxy`。
4. 同一帧的帧末更新 worker：`DoDeferredRenderUpdates_Concurrent → SendRenderTransform → UpdatePrimitiveTransform`，读到 `GetSceneProxy() == nullptr` → `AddPrimitive`（`RendererScene.cpp:2570-2571`）。
5. 两条路径都进入 `FScene::BatchAddPrimitivesInternal`，都通过了 checkf 前的判定 → 后到者断言 / 重复创建 SceneProxy。

**关键事实**：`AssignSceneProxy` 全仓只有一个调用者（`CreateSceneProxy`，`PrimitiveComponent.cpp:5602`、`PrimitiveComponent.cpp:5606`），所以抢先创建代理的一方必然也走了这条批量添加路径。崩溃断言在 `RendererScene.cpp:2305`（main@1137756 为 2266）。

*图 1  注册、异步建代理、帧末更新三条路径在同一个 primitive 上汇合*

把三条路径拆开看，每条都“有理”：

| 路径 | 它认为自己在做什么 | 它漏掉了什么 |
|---|---|---|
| 异步建代理 worker | 组件的渲染状态已创建，我负责建代理（`AsyncRegisterLevelContext.cpp:507-508` 先查 SceneProxy 再 Add） | 没有机制告诉它“另一个线程也在做同一件事” |
| 帧末更新 worker | 组件没有代理，说明从未添加过，补一次 Add | “没有代理”也可能是“正在被别人添加” |
| GT 注册 / Reset | 创建渲染状态、启动任务、收尾 | 任务可能跨帧，收尾时机不受 AddToWorld 控制 |

## 3. 一个 primitive 的完整生命周期（修复后）

**生命周期（时间从左到右；三条泳道为 GT / 异步 worker / 帧末 worker）**

1. **① Launch（GT）**：置位 `AsyncAddInFlight = 1`（`AsyncRegisterLevelContext.cpp:468`）。
2. **② worker 执行**：`CreateSceneProxy → AssignSceneProxy`，可能跨帧（`AsyncRegisterLevelContext.cpp:507-508`）。
3. **③ 帧末看到置位**：`DoDeferredRenderUpdates_Concurrent` 保留脏位直接 `return`（`ActorComponent.cpp:2565`）。
4. **④ 任务完成**：标记仍为 1，等待 GT 收尾。
5. **⑤ Reset（GT）**：清位 0，并按脏位重新挂回帧末更新（`AsyncRegisterLevelContext.cpp:441-443`）。
6. **⑥ 下一帧帧末**：脏位仍在 → 正常执行 transform 更新。

**数据**：Step 1 置位；Step 3 只读该标记（一次原子读）；Step 5 清位并把组件重新加入 `ComponentsThatNeedEndOfFrameUpdate`；Step 6 完成真正的 transform 下发。

*图 2  在途 primitive 的完整生命周期：置位 → 帧末跳过 → 任务完成 → Reset 清位并挂回*

### 3.1 为什么“跳过”是安全的

代理创建时会读取组件当前的 `GetRenderMatrix()`（`RendererScene.cpp` 的 `FScene::BatchAddPrimitivesInternal` 内），所以即使帧末的这次 transform 更新被延后，最终代理仍然拿到最新位置。延后只是把一次冗余更新挪到下一帧，不会丢数据。

### 3.2 三个不变量

- **标记只由 GT 写**：`Launch` 和 `Reset` 都在 GT 调用，worker 只读。
- **同步帧末更新有 happens-before**：`SendAllEndOfFrameUpdatesInternal` 在 GT 上启动 ParallelFor，GT 的置位/清位先于任务启动或晚于任务结束。
- **异步帧末更新用原子类型**：`AsyncAddInFlight` 是 `FThreadSafeCounter`（`PrimitiveSceneInfoData.h:31`），覆盖 `EEndOfFrameUpdateLocation::Async` 与 GT 并发的场景。

### 3.3 脏位的去向

`DoDeferredRenderUpdates_Concurrent` 在途时直接返回，**不清任何脏位**。批次收尾的 `Reset` 会根据 `IsRenderStateDirty()` 选择 `MarkForNeededEndOfFrameRecreate()` 或 `MarkForNeededEndOfFrameUpdate()`（`AsyncRegisterLevelContext.cpp:441-443`）。没有脏位时 `DoDeferredRenderUpdates_Concurrent` 本身是 no-op，所以不需要额外记录“是否被延后过”。

## 4. 原版保证是什么，CL 917608 改了什么

**A. Epic 原版：AddToWorld 不返回，直到异步任务结束**

1. 时间片用完，`IsTimeLimitExceeded = true`。
2. 循环条件 `while (!TimeLimitExceeded || bHasRunningTasks)`（`World.cpp#2:3620`），`bHasRunningTasks` 取自 `IsRunningAsync()`（`World.cpp#2:3614`）。
3. 继续转圈等任务结束 → 退出 AddToWorld，帧末更新不会与它重叠。
4. 退出前还有断言兜底：`FRegisterComponentContext` 析构检查 `IsRunningAsync()`（`ActorComponent.cpp#1:151-152`）。
5. 原版对“已注册但代理未建”的容忍：`Execute` 里先看 `SceneProxy == nullptr` 再 Add（`AsyncRegisterLevelContext.cpp#1:300-301`）—— 前提是两边串行。

**B. CL 917608：超时后用非阻塞取消，立即退出 AddToWorld**

1. 时间片用完。
2. `CancelRunningAsyncTasks()`（`World.cpp:3631`，main@1137756）只对“尚未开始”的迭代生效。
3. 手动置 `bHasRunningTasks = false`（`World.cpp:3617`），循环立即退出。
4. 在途迭代继续跑：已进入 `BatchAddPrimitivesInternal` 的迭代不会被中断。
5. 同帧帧末更新照常执行，读 `SceneProxy == nullptr` → AddPrimitive。

**两处配套改动（都出自 CL 917608）**：① World.cpp 超时后不再等 `bHasRunningTasks`；② `ActorComponent.cpp:152` 把 Epic 的 `check(!IsRunningAsync())` 放宽为 `|| IsPendingCancellation()`。

**取舍**：低配机上 worker 被调度离核时，等待时间不可控；取消换来流畅，代价是任务必须脱离 AddToWorld 继续跑。

*图 3  Epic 原版与非阻塞取消的时序对比*

**为什么原版没有这个问题**：第三态（已注册、无代理）在 Epic 原版里也存在，但异步任务不会活过 AddToWorld。帧末更新如果在任务执行前读到无代理并抢先 Add，队列里的任务随后执行时会看到 `SceneProxy != nullptr` 而跳过 —— 两条路径**串行**，冗余 Add 被设计容忍。

**CL 917608 之后**：任务可以在 AddToWorld 返回后继续执行，原有的“先查再 Add”从串行容忍退化为 TOCTOU。这不是某一行代码写错，而是**保证被移除后，依赖该保证的检查点没有同步调整**。

## 5. 修复设计：异步批次持有在途组件

**修复点（虚线框为改动位置）**

1. **① 异步批次持有（GT）**：`Launch` 置位 `AsyncAddInFlight = 1`；`Reset` 清位 0 并重新挂回帧末更新（`AsyncRegisterLevelContext.cpp:468`、`AsyncRegisterLevelContext.cpp:441-443`）。
2. **② 帧末更新漏斗**：`DoDeferredRenderUpdates_Concurrent` 发现组件在途 → 保留脏位 `return`（`ActorComponent.cpp:2565`）。
3. **③ GT 批量重注册**：`FStaticMeshComponentBulkReregisterContext` 跳过在途组件（`StaticMeshResources.cpp:152`）。
4. **④ 安全网：AddPrimitive 抢占**：抢不到 → Warning 日志并跳过（`RendererScene.cpp:2280-2283`），正常不应命中。

**数据流**：置位 → 帧末/批量重注册均看到在途 → 跳过并保留脏位 → 任务完成 → Reset 清位并把脏位重新挂回。
**线程约定**：所有写标记的动作都在 GT；worker 只读。帧末同步路径由 ParallelFor 提供 happens-before，异步路径用原子计数器。

*图 4  修复点分布：两条“延后”通道 + 一处安全网*

| 改动点 | 代码位置 | 作用 | 正常触发的次数 |
|---|---|---|---|
| `AsyncAddInFlight` 字段 | `PrimitiveSceneInfoData.h:31` | 标记该组件所在异步批次尚未收尾 | — |
| `Launch` 置位 | `AsyncRegisterLevelContext.cpp:468` | 任务启动前告诉外界“这批组件归我” | 每批 1 次遍历（≤16 个组件） |
| `Reset` 清位 + 挂回 | `AsyncRegisterLevelContext.cpp:441-443` | 任务结束后解除持有，补发被延后的更新 | 每批 1 次遍历 |
| 帧末漏斗 | `ActorComponent.cpp:2565` | 在途组件保留脏位返回 | 仅在途期间命中 |
| 批量重注册 | `StaticMeshResources.cpp:152` | 避免对在途组件执行批量 Remove | 仅在途期间命中 |
| AddPrimitive 抢占 | `RendererScene.cpp:2280-2283` | 兜底，防止未知入口并发添加 | 0（本修复后不应命中） |

### 5.1 为什么不是“取消后等待”

取消后 `Wait()` 的逻辑更简单，但等待时长取决于 worker 是否被调度离核 —— 低配机上不可控。本次保留 CL 917608 的非阻塞取消，把“等待”换成“延后”：GT 只付出一次置位/清位遍历，不阻塞。

### 5.2 覆盖边界

已经在途组件被 GT 直接 `UnregisterComponent` / `DestroyComponent` / 强制 recreate 的路径**不在本次覆盖内**。Epic 原版在 AddToWorld 内部也存在同样的可能性（`FAddPrimitivesTask::Execute` 的注释明确提到 PostRegisterAllComponents 可能反注册组件），本次修复不改变这部分语义。

### 5.3 与既有 CR 的关系

`AddPrimitiveClaim`（`PrimitiveSceneInfoData.h:25`）保留为安全网。它最初用于兜住所有并发 Add 入口；在持有机制生效后，正常路径不应再命中，因此失败分支从静默跳过改为 `UE_LOG(LogRenderer, Warning, ...)`（`RendererScene.cpp:2283`），命中本身就是“存在未覆盖并发入口”的信号。

## 6. 验证：功能回归与性能 A/B

### 6.1 功能回归

用例 `nr_sfxai`（NeoRing 特效与 AI，单局），服务器 `lobby-test:13841`，Test 包体 CL 1183280，账号 `ZXB`。

| 轮次 | 结果 | 命令执行 | 崩溃 | 原断言 | 安全网告警 |
|---|---|---|---|---|---|
| 修复前基线构建 | CASE-FINISHED 582s | 20/20 | 0 | 0 | 0 |
| 修复后（首次） | CASE-FINISHED 562s | 20/20 | 0 | 0 | 0 |
| 修复后（clean-code 重构） | CASE-FINISHED 582s | 20/20 | 0 | 0 | 0 |

每一轮都确认：`Log file open` 只出现 1 次（单进程）、进对局 `SetGameStageType:6`、脚本末尾 `trace.stop → ExitBattle("Normal")` 正常执行、无 `CrashContext.runtime-xml`。

### 6.2 性能 A/B

用例 `nr_paotu`（NeoRing 跑图，83 个移动点，WP Cell 持续流入流出），基线/修复各 3 局，交替执行。分析窗口为每局 trace 的前 400 秒（跑图脚本时间轴固定，6 局走同一段路线）。

| 指标 | 基线 3 局 | 修复 3 局 | 中位数变化 |
|---|---|---|---|
| 平均帧时间 | 6.600 / 6.682 / 6.763 ms | 6.508 / 6.569 / 6.509 ms | −2.6% |
| p95 | 7.920 / 8.261 / 8.443 ms | 7.966 / 7.923 / 7.887 ms | −4.1% |
| p99 | 9.376 / 9.957 / 9.959 ms | 9.659 / 11.469 / 9.236 ms | −3.0% |
| 帧数（400 s） | 25358 / 25545 / 25110 | 26061 / 25906 / 25837 | +2.2% |

**结论：没有发现修复带来的性能退化。** 但帧时间的改善不能归因于修复 —— 服务器是共享测试服，每局在线人数不同，与修复无关的角色 Tick 计数在 2.1~8.0 之间波动（近 4 倍）。修复版中两帧超过 33 ms 的卡顿分别来自 Niagara 粒子池预热（93 ms）与网络包处理（24 ms），均与修复路径无关。

### 6.3 修复直接影响的路径

| 路径 | 单次开销变化 | 说明 |
|---|---|---|
| 异步建代理任务 | +7 µs/次（+7%） | 基线里帧末更新常抢先替任务做掉 Add；修复后这项工作回到任务自身，属转移非新增 |
| 流送注册 UpdateComponents | +0.037 ms/次 | 包含 Launch/Reset 的遍历，摊到每帧为微秒级 |
| DoDeferredRenderUpdates | +0.7 µs/组件 | 受组件构成影响（同组数据里未改动的 `UpdatePrimitiveTransform` 漂移 +50%），只看量级 |
| 帧末 AddPrimitive（抢占） | −7% | 命中次数减少，安全网路径不再被走到 |

这些开销摊到 GameThread 每帧都在微秒级，对 6.5 ms 的帧时间没有可测量影响。

## 7. 已知问题与技术债

- **CL 917608 放宽了 Epic 的断言**：`ActorComponent.cpp:152` 允许 `IsPendingCancellation()` 状态下析构。这是非阻塞取消的必要条件，但也意味着“任务已脱离 AddToWorld”不再被断言拦截。本次修复补上了运行时保护，断言本身未恢复。
- **同根因的另一症状**：SplineMesh 共享顶点工厂未初始化（bug 1254748 / 1263687）—— 在途 `CreateSceneProxy` 跨帧，渲染命令排到下一帧 `FScene::Update` 之后。那是渲染线程侧，本次修复只解决 GT 侧。
- **未覆盖路径**：GT 直接对在途组件做 Unregister / Destroy / 强制 recreate（见 §5.2）。
- **性能 A/B 的混杂因素**：共享服务器在线人数不可控。需要干净数据时应在无其他玩家的环境复测。
- **本地构建需要 `-EnableLQGAPSystem`**：包体 pak 是加密的（`Encrypted: 1`），本地 exe 缺少 LQGAP 会启动即报 `Corrupt pak index detected`。包体自带 exe 不需要这个参数。

### 7.1 行号基线

正文引用基于 `//GR/release` 工作区（CL 1183280，含 CL 1198715 修复）。历史版本引用单独标注：`World.cpp#2` / `ActorComponent.cpp#1` / `AsyncRegisterLevelContext.cpp#1` 为 main 分支 CL 917608 合入前的基线；`main@1137756` 为崩溃现场所在的 main 版本。同一语句在不同分支的行号可能不同（例如断言在 release 为 2305、在 main@1137756 为 2266）。

## 8. 排查 Checklist：再次出现同类崩溃怎么定位

1. **先确认断言身份**：`!Primitive->GetSceneProxy()` + “Primitive has already been added to the scene!” → 进入本链路；若是 `AssignSceneProxy` 的 `checkf(SceneProxy == nullptr && SceneData.SceneProxy == nullptr)`（`PrimitiveComponent.cpp:5604`），说明两个线程都通过了 checkf 前的判定，竞争窗口更早。
1. **确认异步注册是否开启**：`LevelStreaming.AsyncRegisterLevelContext.Enabled = 1`（`S1Game/Config/DefaultEngine.ini:286`）。关闭后走同步批量注册，`FRegisterComponentContext::Process` 的 ParallelFor 在 GT 上阻塞，帧末更新无法插入。
1. **确认崩溃时机**：日志中崩溃前是否有 World Partition Cell 活动（路径含 `_Generated_`）。只有在对局中、`bConsiderTimeLimit` 为真时才可能走到非阻塞取消。
1. **确认任务是否跨帧**：看 `FAsyncAddPrimitiveQueue` 的 pending cancellation 状态；若在途，则“任务脱离 AddToWorld”成立。
1. **用 trace 定位并发**：在崩溃帧附近对比 `UpdatePrimitiveTransform (GT)` 与 `FScene_AddPrimitive` 的线程分布，以及 `EndOfFrameUpdate` 与 `AsyncAddPrimitives` 是否在同一时间窗口。

如果要在修复版上验证“持有机制确实生效”，在 `Launch` 置位与 `Reset` 清位处各加一个计数器，并在关卡卸载时检查残留数；残留应为 0。本次验证未加计数，因此只能证明“没有崩”，不能证明“路径一定被走到”。

## 9. 参考

| 内容 | 位置 |
|---|---|
| 崩溃断言 | `UE5EA/Engine/Source/Runtime/Renderer/Private/RendererScene.cpp:2305`（main@1137756:2266） |
| 帧末更新兜底 Add | `RendererScene.cpp:2570-2571` |
| SceneProxy 赋值 | `PrimitiveComponent.cpp:5602`、`PrimitiveComponent.cpp:5606` |
| 第三态产生点 | `ActorComponent.cpp:2123`、`PrimitiveComponent.cpp:732` |
| 在途标记 | `PrimitiveSceneInfoData.h:31`；`AsyncRegisterLevelContext.cpp:468`、`AsyncRegisterLevelContext.cpp:441-443` |
| 帧末漏斗 / 批量重注册 | `ActorComponent.cpp:2565`；`StaticMeshResources.cpp:152` |
| 非阻塞取消 | `World.cpp:3617`、`World.cpp:3631`；`ActorComponent.cpp:152` |
| 原版基线 | `World.cpp#2:3614`、`World.cpp#2:3620`；`ActorComponent.cpp#1:151-152`；`AsyncRegisterLevelContext.cpp#1:300-301` |
| 相关 CL | CL 917608（非阻塞取消）、CL 1198715（本次修复） |
| 性能数据 | `D:/Trace/AsyncAddPerf/{compare.md, runs/*/metrics.json}` |
