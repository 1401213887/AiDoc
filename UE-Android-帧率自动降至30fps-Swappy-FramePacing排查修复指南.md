# UE Android 帧率自动降至30fps 排查与修复指南

> 问题描述：UE 项目在 Android 设备上帧率自动降至 30fps，`stat unit` 显示 Frame 耗时 ~33ms，但 Game/Draw/RHI/GPU 各线程均 <30ms，Queue Present 耗时 20ms+。

---

## 一、问题定位流程

### 1.1 关键现象

| 指标 | 数值 | 含义 |
|------|------|------|
| Frame | ~33ms | 实际帧间隔，对应 30fps |
| Game | <30ms | 游戏线程不忙 |
| Draw | <30ms | 渲染线程不忙 |
| RHI | <30ms | RHI 线程不忙 |
| GPU | <30ms | GPU 不忙 |
| **Queue Present** | **20ms+** | **RHI 在等 vsync** |

**结论：不是性能瓶颈，是帧节奏锁帧（Frame Pacing / VSync 锁定 30fps）。**

### 1.2 验证方法

游戏中控制台逐条执行，观察 `stat unit` / `stat fps` 变化：

```
r.SetFramePace 60    ; 测试帧节奏是否锁30
t.MaxFPS 0           ; 解除 MaxFPS 限制
r.VSync 0            ; 关闭垂直同步
sm.FrameRateSmoothing 0  ; 关闭帧率平滑
```

**本案例验证结果：**
- `r.SetFramePace 60` → 仍然 30fps ❌
- `r.SetFramePace 120` → 恢复 60fps ✅

说明 Swappy 帧节奏库的 SyncInterval 计算有误：`r.SetFramePace 60` 被错误映射为 SyncInterval=2（每 2 个 vsync 提交一次 = 30fps），而 `r.SetFramePace 120` 强制 SyncInterval=1（每个 vsync 提交 = 60fps）。

---

## 二、根因分析

### 2.1 SyncInterval 计算机制

```
Swappy 内部逻辑：
  SyncInterval = Round(屏幕刷新率 / 目标帧率)

60Hz 屏幕示例：
  r.SetFramePace 30  →  Round(60/30) = 2  → 30fps ✓
  r.SetFramePace 60  →  Round(60/60) = 1  → 应该60fps，但部分设备算成2 → 30fps ✗
  r.SetFramePace 120 →  Round(60/120) = 1  → 60fps ✓（绕过Bug）
```

### 2.2 为什么国产设备更容易触发

- 国产 ROM 的 GPU 驱动和 Choreographer 实现与 AOSP 有偏差
- 设备报告的刷新率可能不是精确的 60.00Hz（如 59.94Hz / 60.1Hz）
- Swappy 的浮点计算在这些偏差下产生错误的 SyncInterval

---

## 三、t.MaxFPS vs r.SetFramePace

> 参考：[t.MaxFPS vs r.SetFramePace — The Two Knobs Every UE5 Android Dev Must Understand](https://dev.to/adbhut/tmaxfps-vs-rsetframepace-the-two-knobs-every-ue5-android-dev-must-understand-4md7)

### 3.1 对比

| 维度 | t.MaxFPS | r.SetFramePace |
|------|----------|----------------|
| 控制对象 | Game Thread（游戏线程） | RHI Thread（呈现端） |
| 实现方式 | `SleepNoStats()` 补睡到目标帧间隔 | 控制 vsync 间隔，请求屏幕切换刷新率 |
| 单独使用问题 | 游戏线程快但屏幕跟不上 | 屏幕快但游戏跟不上 |
| 配合使用 | ✅ 两者应设相同值 | ✅ 两者应设相同值 |

### 3.2 t.MaxFPS 内部实现

```
每帧结束计算：
  睡眠时间 = 目标帧间隔 - 上一帧游戏线程工作时间

例：t.MaxFPS 60
  目标帧间隔 = 16.6ms
  上一帧工作时间 = 6.9ms
  睡眠时间 ≈ 9.7ms
```

Unreal Insights 中对应事件：`STAT_FEngineLoop_UpdateTimeAndHandleMaxTickRate`

### 3.3 r.SetFramePace 内部实现

通过 `FAndroidOpenGLFramePacer` 控制 vsync 间隔：
- 设 60 → 请求屏幕以 60Hz 刷新，每 16.6ms 提交一帧
- 设 90 → 请求屏幕以 90Hz 刷新，每 11.1ms 提交一帧

### 3.4 SwappyGL 工作流程

```
RHI 调用 SwappyGL_swap(display, surface)
    ↓
读取 Choreographer vsync 时间戳
    ↓
计算目标呈现时间 = 上次呈现 + 期望帧间隔
    ↓
根据 GPU 完成时间分三种情况：

① GPU 提前完成 → 设置 presentation time，等目标 vsync 再提交
② GPU 按时完成 → 直接提交，几乎立即返回
③ GPU 迟到 → 错过 vsync，自动调整，瞄准下一个有效 vsync
```

Swappy 的核心价值：**vsync 相位锁定** — 每帧在精确 vsync 时刻替换上一帧，帧时序完全均匀。

### 3.5 何时关闭 Frame Pacing

帧率与屏幕刷新率不整除时（如 45fps on 60Hz），启用反而产生微卡顿：

```
启用：33.3ms → 16.6ms → 16.6ms → 33.3ms（微卡顿循环）
关闭：每帧自由落地，不完美但比卡顿流畅
```

---

## 四、Swappy 是否需要开启

### 4.1 官方立场

| UE 版本 | Swappy 默认状态 | 官方建议 |
|---------|----------------|---------|
| UE 4.25 | 默认关闭 | "推荐开启" |
| UE 5.2+ | **默认开启** | "We recommend using Swappy over the legacy frame pacer" |

### 4.2 社区实际反馈

| 问题 | 设备 | 来源 |
|------|------|------|
| Present 随机飙升 24-25ms | Redmi Note 13 Pro (Adreno 710) | [UE Forum 2025.12](https://forums.unrealengine.com/t/performance-issues-with-swappy-enabled/2693072) |
| P95 帧时间增加 3.56ms | 同上 | 同上 |
| Queue Present 卡 1s | 一加 ACE3 | [UE Forum 2025.11](https://d1ap1mz92jnks1.cloudfront.net/t/insights-present/2693051/1) |
| r.SetFramePace 60 锁 30fps | 部分国产 ROM | 社区广泛反馈 |
| `RequiresWaitingForFrameCompletionEvent=true` 改了更差 | Android 13 设备 | 同第一个帖子 |

Epic 开发者回应（2025.12）：**团队仍在调查中，暂无修复时间线。**

### 4.3 建议矩阵

| 设备类型 | 建议 | 理由 |
|----------|------|------|
| 国产 Android（小米/OPPO/vivo/一加等） | ⚠️ **关掉 Swappy** | GPU 驱动/Choreographer 偏差导致 SyncInterval 计算错误、Present 飙升 |
| Pixel / Samsung 等国际品牌 | ✅ 开着 | 接近 AOSP 实现，Swappy 工作正常 |
| 60Hz 屏幕跑 30/60fps | 开不开差别不大 | 整除关系下 vsync 相位锁定优势有限 |
| 高刷屏跑非整除帧率（如 45fps on 90Hz） | ✅ 开着 | 相位锁定优势明显 |

---

## 五、修复方案

### 方案 A：关闭 Swappy（推荐，社区验证）

```ini
; DefaultEngine.ini
[/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]
+CVars=a.UseSwappyForFramePacing=0
+CVars=r.SetFramePace 60
+CVars=r.VSync=1
```

**优点：** 语义清晰，社区验证有效，规避 Swappy 已知问题
**缺点：** 丢失 Swappy 的自适应帧节奏和 vsync 相位锁定

### 方案 B：设 r.SetFramePace 120（Workaround）

```ini
; DefaultEngine.ini
[/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]
+CVars=r.SetFramePace 120
```

**优点：** 一行配置，绕过 SyncInterval 计算错误
**缺点：** 语义不直观；120Hz 设备上会跑到 120fps 而非锁定 60fps

### 方案 C：分设备 Profile 配置

```ini
; DeviceProfiles.ini - 60Hz 设备
[Android_Low DeviceProfile]
+CVars=r.SetFramePace 120

; DeviceProfiles.ini - 120Hz 设备（如需锁60）
[Android_High DeviceProfile]
+CVars=a.UseSwappyForFramePacing=0
+CVars=r.SetFramePace 60
```

---

## 六、快速排查 Checklist

```
□ stat unit 确认：Frame ≈ 33ms 但各线程 < 30ms → 帧节奏锁帧
□ Queue Present 20ms+ → 确认 RHI 在等 vsync
□ r.SetFramePace 60 不生效 → Swappy SyncInterval Bug
□ r.SetFramePace 120 恢复 60fps → 确认是 Swappy 问题
□ 查 Android 系统级锁帧：省电模式、厂商游戏空间、Battery Saver
□ 查 ini 配置冲突：t.MaxFPS / r.SetFramePace / FrameRateLock 跨 Section 重复
□ adb shell dumpsys display | findstr refreshRate → 确认屏幕实际刷新率
□ 搜项目 Config 目录：findstr /S /I "SetFramePace FrameRateLock MaxFPS" *.ini
```

---

## 七、相关参考

- [t.MaxFPS vs r.SetFramePace 深度分析](https://dev.to/adbhut/tmaxfps-vs-rsetframepace-the-two-knobs-every-ue5-android-dev-must-understand-4md7)
- [UE 5.4 Frame Pacing 官方文档](https://dev.epicgames.com/documentation/de-de/unreal-engine/frame-pacing-for-mobile-devices-in-unreal-engine?application_version=5.4)
- [Performance issues with Swappy enabled (UE Forum 2025.12)](https://forums.unrealengine.com/t/performance-issues-with-swappy-enabled/2693072)
- [Queue Present 卡顿问题 (UE Forum 2025.11)](https://d1ap1mz92jnks1.cloudfront.net/t/insights-present/2693051/1)
- [Android Frame Pacing 官方文档](https://developer.android.google.cn/games/optimize/vitals/slow-session?hl=zh-cn)
