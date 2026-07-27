# UE-BecomeViewTarget-渲染帧准备阶段Detach组件-MarkActorComponentForNeededEndOfFrameUpdate-race-condition修复

> `APlayerCameraManager::GetViewTarget()` 渲染帧准备阶段同步触发 `BecomeViewTarget` → GMP消息 → `UpdateViewMode` → `ForceOnBecomeViewTarget` → `SwitchToApproxWeapon(false)` → `DetachFromComponent`，此时组件正在异步渲染状态更新（`IsRenderStateUpdating()=true`），触发 `UWorld::MarkActorComponentForNeededEndOfFrameUpdate` 的 `check(!Component->IsRenderStateUpdating())` 崩溃。

---

## 一、问题定位流程

### 崩溃栈

```
APlayerCameraManager::GetViewTarget()
  → FTViewTarget::CheckViewTarget()
    → AssignViewTarget(NewTarget)
      → AS1GameCharacter::BecomeViewTarget(PC)
        → SendObjectMessage → GMPSignals::Fire
          → AGAWeaponActor::UpdateViewMode()
            → US1WeaponAvatarComponent::ForceOnBecomeViewTarget(false)
              → SwitchToApproxWeapon(false)
                → ApproxWeaponMeshComp->DetachFromComponent(...)   ← 崩溃点
                  → PropagateTransformUpdate → UpdateComponentToWorld
                    → MarkForNeededEndOfFrameUpdate
                      → UWorld::MarkActorComponentForNeededEndOfFrameUpdate  ← check 失败
```

### 失败的 check

```cpp
// Engine/Source/Runtime/Engine/Private/LevelTick.cpp:1028
checkf(!Component->IsRenderStateUpdating(),
    TEXT("Calling MarkActorComponentForNeededEndOfFrameUpdate on component %s but it is currently being updated. ... This is a race condition."));
```

### 确认的关键事实

- `ApproxWeaponMeshComp`（`UStaticMeshComponent`）在之前的帧中被标记了 early render state update，渲染线程 kick 了异步更新 task
- `BecomeViewTarget` 在 `ULocalPlayer::CalcSceneView` 渲染帧准备阶段被同步调用
- `SwitchToApproxWeapon(false)` 里的 `DetachFromComponent` 触发了 `PropagateTransformUpdate`，进而调 `MarkForNeededEndOfFrameUpdate`
- 此时 `IsRenderStateUpdating()=true`，引擎检测到 race condition 直接 crash

---

## 二、根因分析

### 直接原因

`ApproxWeaponMeshComp` 正在异步渲染状态更新时，GameThread 调了 `DetachFromComponent`，触发组件层级修改，被引擎 race condition check 拦截。

### 根本原因

**CL 847551（2026/03/17，陈卓晗）** 把 `UpdateViewMode` 的触发方式从直接调用改为 **GMP 消息 `Actor.OnBecomeViewTarget` 驱动**：

```cpp
// GAWeaponActor.cpp:680
GMP_OnOwnerBecomeViewTarget = FGMPHelper::ListenObjectMessage(
    OwnerCharacter, FName(TEXTVIEW("Actor.OnBecomeViewTarget")),
    this, &ThisClass::UpdateViewMode);
```

GMP 消息是同步 Fire 的。`BecomeViewTarget` 在渲染帧准备阶段（`CalcSceneView`）被调用时，GMP 消息同步触发 `UpdateViewMode` → `ForceOnBecomeViewTarget` → `SwitchToApproxWeapon(false)` → `DetachFromComponent`。

在 CL 847551 **之前**，`UpdateViewMode` 不会在这条渲染帧路径上被同步调用，因此即使 `SwitchToApproxWeapon` 里有 `DetachFromComponent`（CL 816529，2026/03/02 引入），也不会在错误的时机执行。

### 时间线

```
2025/04/14  CL 315345  BecomeViewTarget 发 GMP 消息（此时没人监听→UpdateViewMode）
2025/05/05  CL 348432  UpdateViewMode 里调 ForceOnBecomeViewTarget
2025/12/25  CL 718839  ForceOnBecomeViewTarget 里调 SwitchToApproxWeapon（只有SceneProxy，无Detach）
2026/03/02  CL 816529  SwitchToApproxWeapon 新增 DetachFromComponent 路径
2026/03/17  CL 847551  ★ GMP消息绑定 UpdateViewMode —— 链路打通，崩溃开始可能触发 ★
```

---

## 三、详细技术原理

### `IsRenderStateUpdating()` 的含义

`UPrimitiveComponent::IsRenderStateUpdating()` 返回 true 表示该组件的渲染状态（SceneProxy）正在被异步 task 更新。这个窗口期从 `MarkForNeededEndOfFrameUpdate(bReadyForEarlyUpdate=true)` 标记后、渲染线程 kick 了 async update task 开始，到 task 完成结束。

在此窗口期内，GameThread 上任何触发 `PropagateTransformUpdate` 的操作（Attach/Detach/SetWorldTransform 等）都会调 `MarkForNeededEndOfFrameUpdate`，被 `check(!IsRenderStateUpdating())` 拦截。

### 为什么 `SetTimerForNextTick` lambda 方案不安全

`SetTimerForNextTick(TFunction<void(void)>&& Callback)` 使用纯 `TFunction`，不绑定 UObject：
- `ClearAllTimersForObject` 找不到它（`GetBoundObject()` 返回 nullptr）
- Component 销毁后 lambda 捕获的 `this` 是 dangling pointer
- `FTimerUnifiedDelegate::Execute()` 对 `FTimerFunction` 类型不做任何有效性检查，直接调用

因此 lambda 捕获裸指针的 Timer 方案存在 GC/生命周期 crash 风险。

---

## 四、修复方案

### 方案选择

| 方案 | 思路 | 风险 |
|------|------|------|
| A | `SetTimerForNextTick` + `TWeakObjectPtr` 捕获弱引用 | 低，但引入 Timer 依赖 |
| **B（采用）** | `IsRenderStateUpdating()` 检查 + pending flag + Tick 重试 | **零生命周期风险** |
| C | `r.AllowAsyncRenderThreadUpdates 0` 禁用异步更新 | 性能影响大，仅作临时 workaround |

### 最终改动

**文件**：`S1Game/Source/S1Framework/S1Game/Components/S1WeaponAvatarComponent.h` + `.cpp`

#### .h 添加 pending flag

```cpp
// Pending flags for deferred SwitchToApproxWeapon when render state is being updated async
bool bPendingSwitchApproxWeapon = false;
bool bPendingSwitchValue = false;
```

#### .cpp 三处修改

**1. AttachToComponent 前保护**（`SwitchToApproxWeapon(true)` 分支）：

```cpp
else if (IsValid(ApproxWeaponMeshComp))
{
    if (ApproxWeaponMeshComp->IsRenderStateUpdating())
    {
        bPendingSwitchApproxWeapon = true;
        bPendingSwitchValue = bEnableApproxWeapon;
        return;
    }
    ApproxWeaponMeshComp->AttachToComponent(OwnerChar->GetMesh3P(),
        FAttachmentTransformRules::SnapToTargetIncludingScale,
        OwnerWeapon->SocketToAttach3p);
    ApproxWeaponMeshComp->SetHiddenInGame(false);
}
```

**2. DetachFromComponent 前保护**（`SwitchToApproxWeapon(false)` 分支，崩溃点）：

```cpp
else if (IsValid(ApproxWeaponMeshComp))
{
    if (ApproxWeaponMeshComp->IsRenderStateUpdating())
    {
        bPendingSwitchApproxWeapon = true;
        bPendingSwitchValue = bEnableApproxWeapon;
        return;
    }
    ApproxWeaponMeshComp->DetachFromComponent(FDetachmentTransformRules::KeepWorldTransform);
    ApproxWeaponMeshComp->SetHiddenInGame(true);
}
```

**3. TickComponent 处理 pending**：

```cpp
void US1WeaponAvatarComponent::TickComponent(float DeltaTime, ELevelTick TickType,
                                             FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);

    if (!IsNetMode(NM_DedicatedServer))
    {
        UpdateMIDCameraDistanceParameter();
        UpdateApproxWeaponState();

        if (bPendingSwitchApproxWeapon)
        {
            bPendingSwitchApproxWeapon = false;
            SwitchToApproxWeapon(bPendingSwitchValue);
        }
    }
}
```

### 设计要点

- **`bApproxWeaponEnabled` 只在成功执行 Attach/Detach 后才更新**：跳过时保持旧值，状态一致
- **无死循环**：若下一帧 `IsRenderStateUpdating()` 仍为 true，pending flag 重新置位，继续尝试
- **无 GC 风险**：pending flag 是普通 bool 成员变量，随 Component 销毁自然消失
- **已知边界情况**：pending 那一帧 ApproxMesh 和正常 mesh 同帧可见（视觉 glitch，一帧后恢复）

---

## 五、快速排查 Checklist

遇到 `MarkActorComponentForNeededEndOfFrameUpdate` 的 `IsRenderStateUpdating()` check 失败时：

- [ ] 确认崩溃栈中触发 `MarkForNeededEndOfFrameUpdate` 的组件是谁
- [ ] 确认触发路径是否在渲染帧准备阶段（`CalcSceneView` / `GetViewTarget` / `BecomeViewTarget`）
- [ ] 检查该路径上是否有 `AttachToComponent` / `DetachFromComponent` / `SetWorldTransform` 等操作
- [ ] 用 `p4 annotate` 查关键行的 CL 历史，确认是哪次提交引入了这条同步调用链
- [ ] 修复方案：在操作前加 `IsRenderStateUpdating()` 检查，或延迟到下一帧 Tick 执行

---

## 六、相关参考

- `Engine/Source/Runtime/Engine/Private/LevelTick.cpp:1024-1028` — `MarkActorComponentForNeededEndOfFrameUpdate` 的 check
- `Engine/Source/Runtime/Engine/Private/TimerManager.cpp:385-392` — `FTimerUnifiedDelegate::Execute` 对 `FTimerFunction` 不做有效性检查
- `S1Game/Source/S1Framework/S1Game/Components/S1WeaponAvatarComponent.cpp:1621-1692` — `SwitchToApproxWeapon` 实现
- `S1Game/Source/S1Framework/S1Game/GAWeapon/GAWeaponActor.cpp:680` — GMP 消息绑定 `UpdateViewMode`
