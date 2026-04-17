# Insights CPU Usage Track 技术文档

## 1. 概述

本文档记录了 Unreal Insights Timing Profiler 中 **CPU Usage Track** 功能的完整技术实现。该功能在 Timing Profiler 窗口中新增了一个柱状图 Track 面板，以每帧为单位同时展示两条 Series：

- **Process CPU Usage**（游戏进程 CPU 使用率）：仅统计属于目标游戏进程的线程在各 CPU 核心上的运行时间
- **Total CPU Usage**（全局 CPU 使用率）：统计所有线程（包括非游戏进程的线程）在各 CPU 核心上的运行时间

两条 Series 的关系类似于 Frames Track 中的 **Game Frame** 和 **Rendering Frame**。

---

## 2. 架构设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                  STimingProfilerWindow                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  SFrameTrack │  │SCpuUsageTrack│  │ STimingView  │  │
│  └──────────────┘  └──────┬───────┘  └──────────────┘  │
│                           │                              │
│              ┌────────────┼────────────┐                │
│              ▼            ▼            ▼                │
│   FCpuUsageTrack   FCpuUsageTrack   FCpuUsageTrack     │
│     Viewport         Helper          Calculator         │
└─────────────────────────────────────────────────────────┘
                            │
                ┌───────────┼───────────┐
                ▼                       ▼
        IFrameProvider        IContextSwitchesProvider
        (帧时间区间)          (CPU核心事件/线程归属)
```

### 2.2 类职责

| 类名 | 职责 |
|------|------|
| `SCpuUsageTrack` | 核心 Widget，负责 Tick/UpdateState/OnPaint 循环、鼠标交互、右键菜单 |
| `FCpuUsageTrackViewport` | 视口管理，水平轴为帧索引，垂直轴固定 0%~100% |
| `FCpuUsageTrackHelper` | 数据模型（Sample/Series）和绘制辅助 |
| `FCpuUsageCalculator` | CPU 使用率核心计算逻辑 |
| `FCpuUsageTrackSeriesBuilder` | 将帧数据聚合到 Sample 中 |
| `FCpuUsageTrackDrawHelper` | 柱状图绘制、高亮框绘制、Tooltip 绘制 |

---

## 3. 文件清单

### 3.1 新建文件

| 文件路径 | 说明 |
|----------|------|
| `ViewModels/CpuUsageTrackHelper.h` | 数据模型定义：枚举、结构体、计算器、绘制辅助类声明 |
| `ViewModels/CpuUsageTrackHelper.cpp` | 数据模型实现：Series 构建、CPU 使用率计算、柱状图绘制 |
| `ViewModels/CpuUsageTrackViewport.h` | Viewport 类（header-only） |
| `Widgets/SCpuUsageTrack.h` | Widget 头文件 |
| `Widgets/SCpuUsageTrack.cpp` | Widget 实现（~1460 行） |

> 所有文件路径前缀：`Engine/Source/Developer/TraceInsights/Private/Insights/TimingProfiler/`

### 3.2 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `Public/Insights/IUnrealInsightsModule.h` | `FTimingProfilerTabs` 中新增 `CpuUsageTrackID` |
| `TimingProfilerCommands.h/.cpp` | 注册 `ToggleCpuUsageTrackVisibility` 命令 |
| `TimingProfilerManager.h/.cpp` | 新增 `bIsCpuUsageTrackVisible` 状态和 `ShowHideCpuUsageTrack()` |
| `Widgets/STimingProfilerWindow.h/.cpp` | Tab Spawner 注册、窗口布局、生命周期管理 |
| `Widgets/STimingProfilerToolbar.cpp` | 工具栏新增 CPU Usage 按钮 |

---

## 4. 核心算法

### 4.1 CPU 使用率计算

`FCpuUsageCalculator::ComputeCpuUsageForFrame` 的核心逻辑：

```
输入：FrameStartTime, FrameEndTime, CoreNumbers[]
输出：ProcessCpuUsage, TotalCpuUsage

ProcessRunTime = 0
TotalRunTime = 0

FOR EACH CoreNumber IN CoreNumbers:
    EnumerateCpuCoreEvents(CoreNumber, FrameStartTime, FrameEndTime):
        FOR EACH CpuCoreEvent:
            OverlapStart = max(Event.Start, FrameStartTime)
            OverlapEnd   = min(Event.End,   FrameEndTime)
            OverlapDuration = OverlapEnd - OverlapStart

            IF OverlapDuration > 0:
                TotalRunTime += OverlapDuration          // 所有线程都计入 Total

                IF GetThreadId(Event.SystemThreadId) 成功:
                    ProcessRunTime += OverlapDuration     // 仅目标进程线程计入 Process

TotalAvailableTime = FrameDuration × CoreCount
ProcessCpuUsage = Clamp(ProcessRunTime / TotalAvailableTime, 0, 1)
TotalCpuUsage   = Clamp(TotalRunTime   / TotalAvailableTime, 0, 1)
```

**关键设计**：一次遍历同时计算 Process 和 Total 两条 Series 的数据，避免重复遍历。

### 4.2 分层命中检测（GetSampleAtMousePosition）

为实现类似 Frames Track 中 Game Frame / Rendering Frame 的分层选中效果：

1. 收集所有可见 Series 在当前 SampleIndex 的柱状条信息
2. 按柱状条高度从矮到高排序（Process 通常矮于 Total）
3. 从矮柱到高柱依次检测：鼠标在柱状条区域内（含 3px 容差）则命中
4. 若鼠标在所有柱状条上方，选择最高的柱状条（Total）

**效果**：
- 鼠标落在 Process 柱状条区域内 → 选中 Process
- 鼠标落在 Process 上方但仍在 Total 区域内 → 选中 Total

### 4.3 缓存策略

- **帧级缓存**：`CachedFrameCpuUsage` (TMap<int32, FFrameCpuUsageData>)
  - 仅缓存非零结果，避免 ContextSwitch 数据尚未完全加载时将 0 值固化
- **缓存失效**：
  - `CpuCoresSerial` 变化时清空缓存（核心列表变化）
  - `ThreadsSerial` 变化时清空缓存（ContextSwitch 数据增量加载）

---

## 5. 数据流

### 5.1 Tick 阶段

```
Tick()
  ├── 检测 Viewport 尺寸变化 → bIsStateDirty
  ├── 定时同步分析数据（每 0.1s）
  │   ├── 检查 ContextSwitch 数据可用性
  │   ├── 更新 CPU 核心列表缓存（CpuCoresSerial）
  │   ├── 检测线程映射变化（ThreadsSerial）→ 清空缓存
  │   ├── 更新帧数范围（ViewportX.SetMinMaxInterval）
  │   └── 确保两条 Series 存在
  ├── AutoZoom（如果启用）
  └── UpdateState()（如果 bIsStateDirty）
```

### 5.2 UpdateState 阶段

```
UpdateState()
  ├── 获取 Session ReadScope
  ├── 创建两条 Series 的 Builder
  └── EnumerateFrames(Game, StartIndex, EndIndex)
      └── FOR EACH Frame:
          ├── 检查缓存 → 命中则直接使用
          ├── 未命中 → ComputeCpuUsageForFrame()
          ├── 非零结果 → 写入缓存
          └── 同时添加到 ProcessBuilder 和 TotalBuilder
```

### 5.3 OnPaint 阶段

```
OnPaint()
  ├── DrawBackground()
  ├── DrawHorizontalAxisGrid（背景层）
  ├── IF ContextSwitch 不可用 → 显示提示文字
  ├── ELSE
  │   ├── FOR EACH 可见 Series → DrawCached()
  │   └── DrawHighlightedInterval（Timing View 可视范围）
  ├── DrawHorizontalAxisGrid（前景层）
  ├── 绘制阈值线（80% 红色水平线）
  ├── DrawVerticalAxisGrid（百分比标签）
  ├── DrawHoveredSample（高亮框，根据 SeriesType 确定高度）
  ├── 绘制 Tooltip（Series 名称 + 帧号 + CPU% + 帧时长）
  └── 调试信息（如果启用）
```

---

## 6. Bug 修复记录

### 6.1 缓存导致的"永久 0 值"

**问题**：ContextSwitch 数据增量加载时，帧数据可能已可用但对应时间范围的 ContextSwitch 数据尚未加载完成，导致计算出 0.0 并被缓存，后续数据到达后不会重新计算。

**修复**：
1. 仅缓存非零结果（`if (ProcessCpuUsage > 0.0 || TotalCpuUsage > 0.0)`）
2. 监听 `ThreadsSerial` 变化，数据增量到达时清空缓存强制重新计算

### 6.2 鼠标悬停无法选中 Process Series

**问题**：`GetSampleAtMousePosition` 中，当两条 Series 重叠时（Total 柱状条总是 >= Process），鼠标在 Process 区域内时两条 Series 距离都是 0，由于遍历顺序 Total 总是覆盖 Process。

**修复**：重写为分层命中检测算法（见 4.2 节），按柱状条高度从矮到高排序后依次检测。

### 6.3 高亮框始终显示 Total 的高度

**问题**：`DrawHoveredSample` 使用 `FMath::Max(ProcessCpuUsage, TotalCpuUsage)` 计算高亮框高度，无论选中哪条 Series 都显示 Total 的高度。

**修复**：为 `DrawHoveredSample` 增加 `ECpuUsageSeriesType SeriesType` 参数，根据选中的 Series 类型选择对应的 CPU 使用率值。

---

## 7. 代码标记

所有新增和修改的代码均包裹在以下标记中：

```cpp
#pragma region Engine ZXB
// ... 新增/修改的代码 ...
#pragma endregion
```

---

## 8. 依赖关系

### 8.1 模块依赖

- **TraceServices**：`IFrameProvider`、`IContextSwitchesProvider`、`FFrame`、`FCpuCoreEvent`
- **TraceInsightsCore**：`FDrawContext`、`FDrawHelpers`、`FAxisViewportInt32`、`FAxisViewportDouble`
- **TraceInsights**：`FInsightsManager`、`FTimingProfilerManager`、`STimingProfilerWindow`

### 8.2 数据依赖

- 帧数据：`IFrameProvider::EnumerateFrames(TraceFrameType_Game, ...)`
- ContextSwitch 数据：`IContextSwitchesProvider::EnumerateCpuCoreEvents(...)`
- CPU 核心列表：`IContextSwitchesProvider::EnumerateCpuCores(...)`
- 线程归属判断：`IContextSwitchesProvider::GetThreadId(SystemThreadId, OutThreadId)`
