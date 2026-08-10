# S1Game-AssignSceneProxy并发崩溃-异步关卡流送排查记录.md

> 客户端（Development 包体）在 `UPrimitiveComponent::AssignSceneProxy`（PrimitiveComponent.cpp:5555）断言崩溃：`SceneProxy == nullptr && SceneData.SceneProxy == nullptr` 失败——同一组件被并发 `AssignSceneProxy` 两次。触发场景：大厅任务对话流关卡反复 `SwitchScene From Cache`。当前结论：**崩溃发生在 `FAsyncAddPrimitiveQueue` 异步任务内部的 `ParallelFor` 中，由"另一 worker 抢先 Assign"导致，已加诊断日志等待复现定位"先到者"**。

---

## 一、问题定位流程

### 崩溃现场（双线程堆栈）

**崩溃线程**（Foreground Worker）：
```
FAsyncAddPrimitiveQueue::FAddPrimitivesTask::Execute(ConstBatches, Scene, Token) 行491
  → ParallelFor lambda 行500
    → Execute(Component, Scene) 行421
      → FScene::AddPrimitive → BatchAddPrimitivesInternal 行2255
        → FActorPrimitiveComponentInterface::CreateSceneProxy 行5739
          → AssignSceneProxy 行5555  ← checkf 崩溃
```

**GameThread**（同一时刻）：
```
UGameEngine::Tick → UGeSkillComponentBase::TickComponent（技能Tick）
  → DialogueAction_ActionBridge::RecieveActionStart
    → GMP消息 → Lua → UHallSceneSubsystem::FlushStreamLevel
      → UWorld::FlushLevelStreaming → AddToWorld 行3609
        → ULevel::IncrementalUpdateComponents 行1994
          → FPhysScene_Chaos::ProcessDeferredCreatePhysicsState（纯物理，不碰 proxy）
```

### 关键证据

| 证据 | 说明 |
|---|---|
| 崩溃在 **5555** 而非 RendererScene.cpp:2254 的 `checkf(!GetSceneProxy())` | 2254 通过时 proxy 还是 nullptr；到 5555 才非空。中间只有 `CreateSceneProxy()` 一个调用 |
| GameThread 此刻在**物理阶段**（ProcessDeferredCreatePhysicsState） | 纯物理，`TSet<UBodySetup*>` 迭代，完全不碰 SceneProxy |
| 崩溃线程在 **token 版 Execute 的 ParallelFor** 内 | 与 GameThread 的同步排空（非 token 版 Execute / 单组件 Execute）是**独立调用** |

### 结论：先到者是谁？

崩溃线程的 2254→5555 窗口内，proxy 被并发写为非空。GameThread 在物理阶段不 Assign proxy，**排除 GT 在崩溃瞬间直接 Assign**。最可能是**同一 ParallelFor 内另一个 worker**（同一 `AsyncTask.Batches` 内同一组件出现在两个 index）——但这一机制的确切触发路径尚未被代码证实。

---

## 二、根因分析

### 引擎机制

`LevelStreaming.AsyncRegisterLevelContext.Enabled=1`（S1Game/Config/DefaultEngine.ini:279）启用**异步关卡组件注册**：

- `ULevel::IncrementalRegisterComponents` 增量注册组件 → `FRegisterComponentContext::AddPrimitive` → `FAsyncAddPrimitiveQueue::AddPrimitive` → `NextBatch`（满 16 个 `Flush` 进 `AddPrimitivesArray`）。
- `FAsyncAddPrimitiveQueue::Tick` 异步路径每次 `Launch` 只派发 `AddPrimitivesArray[0]` 单批次 → 后台 `FAddPrimitivesTask::Execute(Batches, Scene, Token)` → `ParallelFor` 并发 `FScene::AddPrimitive` → `AssignSceneProxy`。
- **Development 下 `DO_GUARD_SLOW=0`**，`checkSlow` 编译为 `CA_ASSUME`（无运行时检查）。

### 三个并发执行体（都能 Assign proxy）

| 执行体 | 触发 | 走的 Execute |
|---|---|---|
| 异步 worker（崩溃现场） | `Launch` → token 版 `Execute(Batches, Token)` | 后台线程 |
| GT 逐个排空 | `Tick` 258 分支（`IsRunningAsync() && done`） | `Execute(Component.Get())`（单组件，GT 线程） |
| GT 批量排空 | `Tick` 316-338 分支（`!IsRunningAsync() && done`） | 非 token `Execute(Batches, Scene)`（GT 驱动，内部 ParallelFor） |

**关键结论**：
- GT 批量排空只在 worker **不在跑**时发生（与异步互斥）。
- GT 逐个排空与异步 worker 处理**不同批次**（组件只登记一次）。
- **真正并发**：仅异步任务内部 token 版 `ParallelFor` 的多个 worker。

### 为什么正常路径推不出"批次内重复"

- `ExecuteRegisterEvents`（ActorComponent.cpp:2379）有 `!bRenderStateCreated` 守卫 → 同一组件同一次 AddToWorld 只登记一次。
- `RegisterComponentWithWorld`（ActorComponent.cpp:1863）有 `IsRegistered()` 守卫 → 不会重复 Register。
- `RecreateRenderState_Concurrent`（ActorComponent.cpp:2447）传 `nullptr` → 走同步 `Scene->AddPrimitive`，**不进 NextBatch**。

**因此"批次内同一组件两次 index"的来源未被代码证实**——需要运行时日志确认。

---

## 三、详细技术原理

### `SceneProxy` 是无同步裸指针

- `UPrimitiveComponent::SceneProxy`（PrimitiveComponent.h:2155）是普通裸指针，非原子。
- `GetSceneProxy()` 返回 `SceneData.SceneProxy`（PrimitiveComponent.h:2157）。
- `AssignSceneProxy`（PrimitiveComponent.cpp:5553）的 `checkf` + 写入**无任何同步**。

### 崩溃的 TOCTOU 本质

两个执行体并发：
1. 都读 `SceneProxy == nullptr`（裸读）→ 都通过
2. 都进入 `BatchAddPrimitivesInternal` → 2254 检查通过
3. 各自 `AssignSceneProxy`，后到者触发 5555 断言

### 关卡复用机制

- 大厅流关卡每次 `ActivateLevel` 会 `Duplicated Level`（ActorInstanceGuids.cpp:57，Actor Instance GUID 机制）。
- `SwitchScene From Cache`（BP_HallSceneSubsystem.lua）反复 `SetShouldBeVisible` 切换。
- 每次 AddToWorld 新建 `FAsyncRegisterLevelContext`（`CreateAsyncRegisterLevelContext` 有 `check(!IsValid())`，旧 context 销毁时析构 `Wait()` 收尾）。

---

## 四、修复方案（当前状态：加诊断日志观察）

由于"批次内重复"的确切触发路径未被代码证实，**当前不写修复代码**，而是加诊断日志到复现环境观察，拿到数据后一锤定音再修复。

### 诊断日志改动（已 p4 迁出，Development 包体可见）

**`PrimitiveComponent.h`**（条件编译 `#if !UE_BUILD_SHIPPING && !UE_BUILD_TEST`）：
```cpp
uint32 LastAssignSceneProxyThreadId = 0;              // 上次成功 Assign 的线程 ID
uint32 LastAssignSceneProxyIsGameThread = 0;          // 上次是 GT 吗
uint32 LastAssignSceneProxyIsParallelGameThread = 0;  // 上次是并行 GT 吗
uint32 LastAssignSceneProxyCallCounter = 0;           // 累计 Assign 次数
```

**`PrimitiveComponent.cpp`**：
1. **冲突时**（`SceneProxy` 已非空）打印完整上下文：
   - 组件/owner/level/world 路径
   - 已有 `SceneProxy` 地址 vs 本次 `InSceneProxy` 地址
   - `bRegistered`/`bRenderStateCreated`/`bRenderStateDirty`/`bRenderStateRecreating`
   - **本次线程身份**（ThreadId/IsGameThread/IsParallelGameThread）
   - **上次设置者身份**（ThreadId/IsGameThread/IsParallelGameThread/累计次数）
2. **成功 Assign 后**记录线程身份到 `LastAssignSceneProxy*`。

### 判定逻辑（拿到日志后）

- `上次设置者.IsGameThread=1` → 上次是 **GT 同步路径**（`RecreateRenderState`/GT 排空）改的。
- `上次设置者.IsParallelGameThread=1` → 上次是**异步 worker** 改的。
- 两个 proxy 地址对比 + `累计次数≥2` → 确认重复 Assign。

### 候选修复方向（拿到日志后按结论选择）

| 方向 | 适用 | 说明 |
|---|---|---|
| 登记侧完整去重（NextBatch + AddPrimitivesArray） | 跨批次重复 | 组件 re-register 后旧副本已在 AddPrimitivesArray |
| 派发侧 `TSet` 去重（ParallelFor 前） | 批次内重复 | 让每组件在 ParallelFor 只出现一次 |
| `AssignSceneProxy` 幂等化（CAS/跳过） | 覆盖所有并发路径 | 但改动重，需谨慎 |

> 注意：此前曾尝试 `AssignSceneProxy` 改为 CAS 原子幂等（bool 返回 + RendererScene 2254 改 continue），因基于"GT 改 proxy"的错误假设已回退。**等诊断日志确认先到者后再定修复**。

---

## 五、快速排查 Checklist

遇到 `AssignSceneProxy` 5555 断言崩溃（`SceneProxy == nullptr && SceneData.SceneProxy == nullptr` 失败）时：

- [ ] 确认崩溃线程：是 `FAsyncAddPrimitiveQueue` 异步 worker（token 版 Execute 的 ParallelFor）还是 GT？
- [ ] 崩溃在 5555 而非 2254 → 2254 通过时 proxy 还空，到 5555 才非空 → **并发双 Assign**。
- [ ] 看崩溃时 GT 在做什么：若在物理阶段（ProcessDeferredCreatePhysicsState）则排除 GT 直接 Assign。
- [ ] 确认 CVar `LevelStreaming.AsyncRegisterLevelContext.Enabled` 是否启用。
- [ ] 确认是 Development 包体（`DO_GUARD_SLOW=0`，`checkSlow` 无运行时检查）。
- [ ] 确认触发场景是否涉及缓存关卡反复 `SwitchScene From Cache`。
- [ ] 加诊断日志（`LastAssignSceneProxy*`）确认"上次设置者"是 GT 还是 worker。

---

## 六、相关参考

- 崩溃日志：`C:\Users\djangozhang\Downloads\CCE4008EE959421E9E796E04A5E3BCE6_CustomizedLogFile\S1Game.log`
- 崩溃点：`UE5EA/Engine/Source/Runtime/Engine/Private/Components/PrimitiveComponent.cpp:5555`（AssignSceneProxy）
- 前置 check：`UE5EA/Engine/Source/Runtime/Renderer/Private/RendererScene.cpp:2254`（`checkf(!GetSceneProxy())`）
- 异步队列：`UE5EA/Engine/Source/Runtime/Engine/Internal/Streaming/AsyncRegisterLevelContext.cpp`（FAsyncAddPrimitiveQueue）
- CVar 配置：`S1Game/Config/DefaultEngine.ini:279-280`（`LevelStreaming.AsyncRegisterLevelContext.Enabled=1`）
- 关卡切换 Lua：`S1Game/Content/Script/Client/Modules/Hall/Subsystem/BP_HallSceneSubsystem.lua`（SwitchScene From Cache）
- 同项目 race condition 先例：`D:\GR_release\UE-BecomeViewTarget-渲染帧准备阶段Detach组件-MarkActorComponentForNeededEndOfFrameUpdate-race-condition修复.md`
- 双线程堆栈：`FAsyncAddPrimitiveQueue::FAddPrimitivesTask::Execute`（异步）+ `UWorld::AddToWorld → IncrementalUpdateComponents → ProcessDeferredCreatePhysicsState`（GT）
