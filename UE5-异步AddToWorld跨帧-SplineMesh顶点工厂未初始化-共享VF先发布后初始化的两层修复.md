# UE5 异步 AddToWorld 跨帧 —— SplineMesh 共享顶点工厂「先发布后初始化」竞态与两层修复

> **作者**: 张建国（ZXB）  
> **日期**: 2026-10-16  
> **涉及模块**: Engine/Components, Engine/StaticMesh, Engine/Streaming, RenderCore/RenderingThread  
> **关键技术**: AsyncAddToWorld 并行建 proxy · 非阻塞取消跨帧 · FRenderCommandList 命令录制 · 共享资源发布/初始化竞态

**一句话**：Epic 原版 `USplineMeshComponent::InitVertexFactory` 先「发布共享指针」、再在渲染线程「初始化资源」，两者之间存在窗口；项目的非阻塞取消让后台任务跨帧，使另一个 worker 的渲染命令排到本帧 `FScene::Update` 之后 —— 渲染线程于是在窗口内读到了那个指针，触发 `FMeshBatch was not properly setup` ensure。
**修复**：两层，各自独立有效、各带 CVar、可单独回退。第一层把「发布」挪到初始化之后（治本点）；第二层让这批渲染命令抢占命令流位置（治面）。
**验证**：插桩 `Sleep(0.02f)` 后三轮实测 —— 两层都关 → 复现 6 次；只开第一层 / 只开第二层 → 均 0 次，且复现现场与 bug 单记录逐字符一致。

---

## 1. 现象与根因判定
**全景（对应图 1）**——这条链路分四段：

1. **关卡流送（GT）**：WP cell 流入 → 组件批量注册 → `AddPrimitive` 入队
2. **AsyncAddToWorld**：worker 线程并行、每批 16 个组件、`CreateSceneProxy`
3. **共享资源（本文主角）**：`FSplineMeshVertexFactory` 挂在 `UStaticMesh` 上，所有 spline 段共用
4. **渲染命令流（RT）**：`ENQUEUE_RENDER_COMMAND` → `FScene::Update` → `DrawStaticElements` 读 VF

主角诞生于第 2 段的 worker 线程，寄居在第 3 段的共享位置上，最终被第 4 段的渲染线程消费。

**报错样本**（main@1110360 / release@1162595 各一次）：

```
FMeshBatch was not properly setup. Mesh has an uninitialized vertex factory!
  VertexFactory[Name: FSplineMeshVertexFactory, Initialized: 0]
  PrimitiveSceneProxy[Level: /Game/Maps/Rush/RUSH_03_Main/_Generated_/2QKNKSTP02KN1T9GKLNTNB8GO,
                      Owner: Blockout_Tube_C_UAID_ACB4803EB66C53DA02_25c32625080ab041_1433571598,
                      Resource: SM_Blockout_Box]
Callstack: FScene::Update -> FPrimitiveSceneInfo::AddStaticMeshes (ParallelFor)
           -> FStaticMeshSceneProxy::DrawStaticElements -> FBatchingSPDI::DrawMesh -> FMeshBatch::Validate
```

断言位置 `SceneManagement.cpp:1979`（跨 1972-1979 的 ensureMsgf，MSVC 报宏末行）。

现场是一根由多段 SplineMesh 拼成的 Blockout 管道，所有段共用 `SM_Blockout_Box`，集中在**关卡首次流入**时。

**根因链路：三个条件叠加**

| # | 条件 | 位置 / 证据 |
|---|---|---|
| ① | 共享资源懒创建（先发布后初始化） | Epic 原版，项目未改 —— `SplineMeshComponent.cpp:368-393` |
| ② | worker 线程并行建 proxy | CL 371998 合入 `FAsyncRegisterLevelContext`，`Enable=1`，每批 16 个组件 |
| ③ | **非阻塞取消** → 任务跨帧 | CL 894515 → 917608（main）/ 990998（release）；`World.cpp` 时间片用完只调 `CancelRunningAsyncTasks()` 设标志，不再等 worker |

**只有 ①② 不会出问题**：原版 5.6 会**等**任务结束，命令都在本帧渲染前入队，最多出现并发 `new` 泄漏（= UE-389807）。**③ 是必要的第三环**。

## 2. 缺陷本体：共享顶点工厂的创建—发布—初始化三步
主角是 `USplineMeshComponent::InitVertexFactory`（`SplineMeshComponent.cpp:320`），在 **proxy 构造时**被调用 —— 调用点见 `SplineMeshSceneProxy.cpp:92` 与 `SplineMeshSceneProxy.cpp:95`。

它写的共享指针是 `FStaticMeshVertexFactories::SplineVertexFactory`（`StaticMeshResources.h:670`，override 色版本 `StaticMeshResources.h:672`），宿主是 `UStaticMesh` 的 RenderData —— **同一张 mesh 的所有 spline 段共用一份**。渲染线程读它在 `SplineMeshSceneProxy.cpp:110` 与 `SplineMeshSceneProxy.cpp:386`。

**「创建 — 发布 — 初始化」四步**（对应图 2）：

1. `SplineMeshComponent.cpp:368` 判空（已有则 return）
2. `SplineMeshComponent.cpp:374` `new FSplineMeshVertexFactory`
3. `SplineMeshComponent.cpp:377` / `SplineMeshComponent.cpp:381` 写共享指针 = **发布**
4. `SplineMeshComponent.cpp:386` `ENQUEUE_RENDER_COMMAND(InitSplineMeshVertexFactory)`

**窗口在第 3 步与第 4 步之间**：指针已对其他线程可见，但资源还没 `InitResource`。此时任何读到该指针的人，拿到的都是「还不能用」的对象。

## 3. 出错时序：两个 Worker 走一遍
按图 3 的编号走一遍（四条泳道：Worker A / Worker B / GameThread / RenderThread）：

| # | 泳道 | 事件 |
|---|---|---|
| 1 | Worker A | 判空 → 空 |
| 2 | Worker A | new VF |
| 3 | Worker A | **写共享指针 = 发布** |
| — | Worker A | 窗口：指针可见、资源未初始化 |
| 4 | Worker B | 判空 → **非空**（A 已发布）→ 直接 return，以为「已经有了」 |
| 5 | Worker B | `AddPrimitive(组件2)` + `ENQUEUE(AddPrimitiveCommand)` |
| 6 | Worker A | `ENQUEUE(InitResource)` —— **太晚了** |
| 7 | GameThread | 时间片用完，`CancelRunningAsyncTasks()` |
| 8 | GameThread | 只设取消标志、**不等** worker，就离开 AddToWorld |
| 9 | GameThread | 提交本帧渲染 |
| 10 | RenderThread | `FScene::Update` → `AddStaticMeshes` → `DrawStaticElements` **读共享 VF** |
| 11 | RenderThread | 指针非空，但 `Initialized=0` → **ensure 触发** |

★ 成败在第 3~8 步。

## 4. 第一层修复：把发布挪到初始化之后
开关 `r.SplineMesh.InitVertexFactoryOnRenderThread`（默认 1，声明在 `SplineMeshComponent.cpp:52`；置 0 完全走原版逻辑），改动见 `SplineMeshComponent.cpp:343-364`。

**改前**：worker 判空 → new → 写共享指针（发布）→ ENQUEUE(InitResource)。发布与初始化分离，中间是窗口。

**改后**：worker 只 ENQUEUE 一条命令；渲染线程内串行完成「判空 → new → SetData → InitResource → **最后才写共享指针**」。

**为什么有效**：发布即已初始化 —— 中间态不存在。

**副产品**：并发 `new` 造成的顶点工厂泄漏（UE-389807）一并消除，因为创建点唯一了。

## 5. 第二层修复：异步任务专属的渲染命令列表
开关 `LevelStreaming.AsyncRegisterLevelContext.RecordRenderCommands`（默认 1，`AsyncRegisterLevelContext.cpp:44`）。

**机制**：

1. GT 在 `Launch` 时 `FRenderCommandList::Create()` 并**立即 `Submit()` 占位**（`AsyncRegisterLevelContext.cpp:428`）
2. 任务体内用 `FRecordScope` 绑定该列表（`AsyncRegisterLevelContext.cpp:437`），worker 的 `ENQUEUE_RENDER_COMMAND` 全部录进列表而不直达渲染线程
3. ParallelFor 的每个子任务一个子列表（`AsyncRegisterLevelContext.cpp:601-606`）—— **worker 不继承 TLS，必须分开**
4. 任务结束 → `Close` 列表 → 触发 `DispatchTaskEvent`
5. RT 执行到占位 → `TryGetDispatchTask()->Wait()`（`RenderingThread.cpp:2100-2106`）→ 回放列表全部命令

**占位为何安全**：非 `CloseOnSubmit` 的列表在**构造时**就 `DispatchTaskEvent.Emplace()`（`RenderingThread.cpp:1822-1834`），所以 RT 拿到它一定会等 `Close`。

**⚠️ 最容易推错的一点**：第二层**不是**靠「列表内 A 的 init 与 B 的 AddPrimitive 谁先谁后」救的。`AddPrimitiveCommand` 只是把 primitive 加进待处理队列，**真正读 VF 的 `DrawStaticElements` 发生在 `FScene::Update`**。只要**整批命令都在 `FScene::Update` 之前执行完**，列表内相对顺序就不影响结果 —— 「占位」保证了这一点。

**代价**：RT 在占位处会等列表关闭。任务正常跑完时列表已 Close，**零等待**；跨帧（被取消）时 RT 等剩余部分，量级约为「当前批次剩余的处理时间」（ms 级）。对比改动前，这是把「GT 等一整批」换成「RT 等剩余」。

## 6. 为什么只有 SplineMesh 中招：同类风险判据
**出问题需要三个条件同时成立**：① 共享 ② 组件期懒创建 ③ 发布/初始化分离。再叠加 worker 并发 + 跨帧才真的炸。

| 资源形态 | ① 共享 | ② 组件期懒创建 | ③ 发布/初始化分离 | 结果 |
|---|---|---|---|---|
| 普通 `FStaticMeshVertexFactories::VertexFactory` | ✓ 共享 | ✗ 资产加载期 | ✗ 绑定同一步 | 安全 |
| 组件私有资源（各自一份） | ✗ 不共享 | ✓ 组件期 | — | 安全 |
| **`SplineVertexFactory`（本单）** | **✓ 共享** | **✓ 组件期** | **✓ 分离** | **中招** |

普通 StaticMesh 的 VF **虽然也是共享的**，但它在**资产加载期**就由 `FStaticMeshVertexFactories::InitResources` 一次性建好（`StaticMesh.cpp:1190-1197`）—— 那时组件还没建 proxy，**没有并发读者**；而且它用 `BeginInitResource` 把发布与初始化绑在同一步。

SplineVertexFactory 则是「**资产级共享 + 组件级懒创建**」的混合形态 —— 两个世界的坏处都占了。它之所以懒创建，是 Epic 为省内存：大多数 StaticMesh 根本不会被拿去拼 spline。

**已确认的第二例**：WidgetComponent BodySetup（bug 1255826 / CL 1124021，2026-09-10），改为「只允许真正的 GT 创建 + 批内去重」。

**但批内去重救不了本单**：它防的是「同一个组件被处理两次」，管不了「**不同组件共用同一份资源**」。release@1162595 已含该 CL 仍复现。

**排查判据** —— 在 `CreateSceneProxy` 可达路径上找同时满足这三条的代码：

```cpp
if (SharedThing) return;              // 1. 判空
SharedThing = new ...;                // 2. 写【跨组件共享】的指针
ENQUEUE_RENDER_COMMAND(...)(          // 3. 初始化放到别的线程、滞后于发布
    [&]{ Thing->InitResource(...); });
```

重点是**看指针的归属**：`this->成员`（组件私有 → 安全）还是 `SomeAsset->RenderData->...`（资产共享 → 危险）。

## 7. 确定性复现与验证
**插桩**：在**原版路径**（cvar=0 时走的那条）的「写指针」与「ENQUEUE」之间插入 `if (!IsInGameThread()) { FPlatformProcess::Sleep(0.02f); }`，放大窗口。

| 轮次 | `InitVertexFactoryOnRenderThread` | `RecordRenderCommands` | `uninitialized vertex factory` | 预期 | 结果 |
|---|---|---|---|---|---|
| ① | **0** | **0** | **6 次** | 稳定复现 | ✅ 复现 |
| ② | **1** | 0 | **0** | 不复现 | ✅ 一致 |
| ③ | 0 | **1** | **0** | 不复现 | ✅ 一致 |

① 的复现现场与 bug 单记录**逐字符一致**：Owner UAID `..._ACB4803EB66C53DA02_25c32625080ab041_1433571598`、cell `2QKNKSTP02KN1T9GKLNTNB8GO`、`SM_Blockout_Box` —— 排除「碰到另一个 bug」。

每轮都先验证配置真的生效（日志 `LogConfig: Set CVar [[...]]`、地图确为 RUSH_03_Main）。

**复现的三个操作前提**（都是踩出来的）：

1. **进 RUSH_03_Main 必须同时给 `-CustomRoomSceneId=113`** —— 自定义房间模式下由它决定地图，`-TestMap` 会被盖过（只给 `--map` 会跑进 RUSH_01_Main）。sceneId 映射表在 `S1GameServer/bin/Tables/Gameplay.lua`：111=RUSH_01_Main、113=RUSH_03_Main、116=RUSH_03_Main_Test。
2. **RUSH_01_Main 里根本没有 SplineMesh** —— 在那张图上无论怎么插桩都复现不了。
3. cvar 下发写 `<包体>/S1Game/Saved/Config/Windows/Engine.ini` 的 `[ConsoleVariables]` 段；游戏退出时 UE 会重写该文件并清掉这一段，**每轮跑测前需重新下发**。

## 8. 配套改动与已知取舍
### 8.1 释放路径同步挪到渲染线程

`FStaticMeshVertexFactories::ReleaseResources`（`StaticMesh.cpp:1199`）。

**为什么必须改**：指针改由 RT 写之后，GT 上 `if (SplineVertexFactory)` 可能读到空而跳过释放；随后 RT 的 init 建出 VF 却没人释放 → 析构时 `delete` 一个已初始化资源 → Fatal `A FRenderResource was deleted without being released first!`（`RenderResource.cpp:248-258`）。

**改法**：把判空挪进一条**无条件** ENQUEUE 的渲染命令（`StaticMesh.cpp:1208`）。

**已知取舍（空命令）**，增量核算：

| mesh 情况 | 原版 | 现在 | 增量 |
|---|---|---|---|
| 用过 spline | ≤2 条 | 1 条（合并处理两个指针） | **−1** |
| 没用过 spline | 0 条 | 1 条 | **+1** |

调用点在 per-LOD 循环里（`StaticMesh.cpp:2737` 与 `StaticMesh.cpp:2076` 两处），一次卸载 N 张 mesh × M 个 LOD 就是 N×M 条，其中多数空跑。量级 **< 1ms、一次性、不在热路径**。

**为什么不能简单地「只针对 SplineVertexFactory」**：判据必须 GT 可见，而指针由 RT 写。曾考虑加 `std::atomic<bool>` 门控，但 `FStaticMeshVertexFactories` 被放进 `TArray`（`StaticMeshResources.h:703` / `StaticMeshResources.h:770`），atomic 不可拷贝，加进去 `TArray` 直接编译不过。若将来确要优化，正确做法是把标志放在 `UStaticMesh` 上、且**必须在「入队侧」置位**。

### 8.2 流送通知移出录制范围（锁环）

第二层引入了新的等待关系：RT 会等 worker 关闭列表。而 worker 在录制范围内取 `StreamingManagerTexture` 的锁（`StreamingManagerTexture.cpp:1567`），GT 侧持同一把锁并调 `FlushRenderingCommands`（`StreamingManagerTexture.cpp:2491-2520`）—— 形成 **GT 持锁 → RT 等列表关闭 → worker 等该锁** 的环。

**解法**：把流送通知挪到 `FRecordScope` 生命周期**之外**（新增 `FAddPrimitivesTask::NotifyStreaming`，`AsyncRegisterLevelContext.cpp:538`；调用点 `AsyncRegisterLevelContext.cpp:441`）。**不是**挪出 worker 线程 —— 流送时机不变，只是让 RT 在 root `Close()` 那刻就解套。传参用 `RenderCommandList ? &ComponentsToNotify : nullptr`，保证 cvar=0 时行为与改动前逐字等价。

**残余风险**：`CreateRenderState_Concurrent` fallback 分支里 `SkinnedMeshComponent::InitLODInfos` 的 `FlushRenderingCommands` 无法用同一手法拆解（该分支必须在录制范围内），低概率，接受并观察。

## 9. 排错记录与方法论
### 9.1 跑测抓出的真 bug：并发 Add 导致 double free

**现象**：首次跑测 **262s 崩溃**，`EXCEPTION_ACCESS_VIOLATION writing address 0x8`，调用栈**全在本次改动的代码上**。

**根因**：锁环修复时我用 `TArray<..., FConcurrentLinearArrayAllocator>` 做 ParallelFor 的多 worker 收集器 —— 而 `FConcurrentLinearArrayAllocator` 的 `Concurrent` 指的是「用并发线性分配器**分配**」，**不是「并发安全」**。其 `ResizeAllocation` 里 `Data = Realloc(Data, ...)` 全程无锁，两个 worker 同时 Add 会 use-after-free + double free；`0x8` 正是 `TArray::ArrayNum` 的字段偏移。

**修法**：改用 `TQueue<UPrimitiveComponent*, EQueueMode::Mpsc>`。注意 `TQueue::Dequeue` 是**非 const** 方法，写成 `NotifyStreaming(const TQueue<...>&)` 会编译失败。

**它的隐蔽性**：这个 bug 逃过了 `p4 diff` hunk 复核、两树逐字比对、声明/定义/调用三方自查、编译通过 —— **全绿，然后实机 262 秒炸**。静态验证测的全是「一致性与可编译性」，**没有一环测运行时正确性**。

### 9.2 方法论点

| 点 | 说明 |
|---|---|
| 内容一致性 ≠ 可编译性 | 在不能编译的工作区改代码，`p4 diff` 再干净也只证明「写进去了」。本次因在能编译的树重编才暴露 `.h` 声明漏同步（`error C2661/C2511`），否则会随 CL 一起提交 |
| 自查要双向 | 只查「新类型出现了没」不够，**还要查「旧类型清干净了没」**。第二次编译失败正是因为 .cpp 里两处 `Execute` 定义签名漏改 |
| 「Concurrent」不是线程安全承诺 | UE 类型名里的 Concurrent / Async / LockFree 描述的是**策略**，用前读实现的分配/回收路径 |
| 原版怎么用 ≠ 你可以怎么用 | 沿用类型却换使用模式（只读 → 并发写入）是典型陷阱 |
| 审计的范围决定它的保证 | 专项审计 agent 查的是「是否自锁」，**不覆盖「数据竞争」** |

### 9.3 性能实测（已有 trace 的结论）

对一份 602s / 17145 帧的实机 trace（`nr_sfxai`）分析：

| 判据 | 数据 | 结论 |
|---|---|---|
| RT vs GT 帧时间 | p50 **18.44 / 18.49**、p95 **24.25 / 23.74**、p99 **28.67 / 27.19** | 几乎重合，无系统性差异 |
| RT 侧等待类 timer | top 35 内无（`Tasks::Wait` 仅 2 次 / 0.15ms） | 没有 RT spin 等 worker 的痕迹 |
| RT-only hitch | 17 个 / 652.6ms，逐个查过 = **PSO 编译** + **硬页错误** | **都不是占位等待** |

⚠️ **局限**：该 trace 里 AsyncAddToWorld 活动极少（`AddToWorld` 日志 2 条、`updating components` 0 条）—— 脚本的 `Trace.Start` 在进对局之后才触发，而流送集中在进图阶段，天然落在录制区间之外。**所以它不能用来断言第二层零开销**，需要一份覆盖进图过程的 trace（RUSH_03_Main）才能量准。

## 10. 参考
### 相关单据与 CL

| 项 | 说明 |
|---|---|
| bug 1254748 / bug 1263687 | 同一条 ensure（main@1110360 / release@1162595） |
| UE-389807 | SplineMeshComponent resources leak（同一竞争的另一种表现，Unresolved） |
| bug 1255826 / CL 1124021 | WidgetComponent BodySetup：只允许 GT 创建 + 批内去重 |
| CL 371998 | 5.6 AsyncAddToWorld 合入 + ini 开启 |
| CL 894515 / 917608 / 990998 | 非阻塞取消（MergeTest / main / release） |

### 关键文件索引

| 文件 | 关注点 |
|---|---|
| `SplineMeshComponent.cpp:52, 320, 343-364, 368-393` | cvar / InitVertexFactory / 第一层 RT 懒创建 / 原版路径 |
| `SplineMeshSceneProxy.cpp:92, 95, 110, 386` | proxy 构造调用 / RT 读 VF |
| `StaticMesh.cpp:1199-1219, 2076, 2737` | ReleaseResources 与两个 per-LOD 调用点 |
| `StaticMeshResources.h:650-672, 703, 770` | FStaticMeshVertexFactories 定义 / TArray 约束 |
| `AsyncRegisterLevelContext.cpp:44, 428, 437, 441, 538, 601-606` | cvar / 建列表 / 录制 / 通知 / 子列表 |
| `RenderingThread.cpp:1822-1834, 2100-2106` | DispatchTaskEvent 构造即建 / RT 侧 Wait |
| `RenderingThread.h:820, 887, 920` | FRenderCommandList / Submit / FParallelForContext |
| `RenderResource.cpp:248-258` | 未释放即析构的 Fatal 判据 |

行号基线：`C:\GR_main`（含本次改动），对照 depot 时需注意改动带来的行号偏移。
