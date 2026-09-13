# UE Mobile BasePass Draw Call 上限（r.Mobile.BasePassMaxDC）实现与 stat 统计口径

> 给移动端 base pass 加一个 DC 限流旋钮：`r.Mobile.BasePassMaxDC`（默认 0 = 不限制），超出上限的 mesh draw command 直接跳过，用于定位 DC 数量对帧率的影响。实现踩到两个非直觉点：① base pass 的 RDG pass lambda 跑在 **task worker 线程**而非渲染线程，照抄 `FPixelRenderCounters` 的 `check(IsInRenderingThread())` 会在启动时直接崩；② 与 `stat drawcount` 对比时会发现它的 `Basepass` 计数**比上限多**，根因是该 RHI category 的作用域还包含天空 pass 与编辑器图元。

---

## 一、最终形态与需求边界

| 需求 | 结果 |
|---|---|
| `r.Mobile.BasePassMaxDC`，默认 0 不限制，>0 时限制 base pass 最大 DC 数，超出跳过 | **保留并可用** |
| `stat unit` 面板新增 `Base Pass Draws:` 一行 | **已回退** —— 见 §五，`stat drawcount` 已提供该信息，新增行冗余 |

改动文件（P4 changelist default）：

| 文件 | 改动 |
|---|---|
| `Renderer/Private/MeshDrawCommands.h` / `.cpp` | `FParallelMeshDrawCommandPass::Draw()` 增加上限参数（默认 -1 = 不限制） |
| `Renderer/Private/MobileBasePassRendering.cpp` | 新增 CVar；`RenderMobileBasePass` 传上限 |
| `RenderCore/Public/RenderCounters.h`、`RenderCore/Private/RenderCore.cpp`、`Engine/Private/UnrealClient.cpp` | 已 `p4 revert`（属被回退的显示需求） |

## 二、实现链路

### 2.1 截断点选在 `Draw()`

Mobile base pass 的 MDC 提交**只有一个入口**，因此截断只需卡一处：

```
RenderForwardSinglePass        MobileShadingRenderer.cpp:2181
RenderForwardMultiPass                              :2258
RenderDeferredSinglePass / MultiPass                :2564 / :2665
  → FMobileSceneRenderer::RenderMobileBasePass      MobileBasePassRendering.cpp:620
      → ParallelMeshDrawCommandPasses[EMeshPass::BasePass].Draw(...)   ← 唯一提交点
      → SkyPass.Draw(...) / RenderMobileEditorPrimitives(...)          ← 不受限，见 §四
```

`Draw()` 内部两条提交路径的 `NumMeshDrawCommands` 都是 `TaskContext.MeshDrawCommands.Num()`，所以改一处即全覆盖：

```cpp
const int32 NumDrawsToSubmit = MaxNumDrawsToSubmit < 0
    ? TaskContext.MeshDrawCommands.Num()
    : FMath::Min(MaxNumDrawsToSubmit, TaskContext.MeshDrawCommands.Num());
if (NumDrawsToSubmit <= 0) { return; }
// GPUScene 路径
TaskContext.InstanceCullingContext.SubmitDrawCommands(..., 0, NumDrawsToSubmit, ...);
// 非 GPUScene 路径
SubmitMeshDrawCommandsRange(..., 0, NumDrawsToSubmit, ...);
```

### 2.2 三个让「卡一处就够」成立的前提

1. **Mobile 走 `Draw()`（立即提交）而非 `Dispatch()`（RDG 并行）**，PC Deferred 走 `Dispatch()` 或另一处 `Draw()`（`BasePassRendering.cpp:2548 / :2771`）→ 给 `Draw()` 加**默认参数**即可对 PC 零影响。
2. **MobileBasePassCSM 的 MDC 已合并进同一数组** —— `DispatchPassSetup` 时作为 `InOutMobileBasePassCSMMeshDrawCommands` 并入（`MobileShadingRenderer.cpp:518-533`、`MeshDrawCommands.h:144-145`），所以它们同样受上限约束，不会漏。
3. **截断必须在同步 setup task 之后算** —— `MeshDrawCommands` 在 setup task 完成前不是最终数量。截断代码放在 `WaitForMeshPassSetupTask()` 之后。

### 2.3 截尾部而非头部

pass 内部已做 front-to-back 排序（`MobileShadingRenderer.cpp:510` "Run sorting on BasePass"），所以砍掉尾部 = 优先保留近处物体，画面退化方向符合直觉。

## 三、关键坑：RDG pass lambda 跑在 task worker 线程

这是本次最贵的一个教训。

### 3.1 崩溃现象

```
Assertion failed: IsInRenderingThread()
  RenderCounters.h:<counter 的 AddBasePassDraws 内>
  FMobileSceneRenderer::RenderMobileBasePass     MobileBasePassRendering.cpp
  RenderDeferredSinglePass lambda                MobileShadingRenderer.cpp
  FRDGBuilder::SetupParallelExecute              ← 真凶
  LowLevelTasks::FScheduler::WorkerLoop
```

### 3.2 根因

`FRDGBuilder::SetupParallelExecute` 会把 pass 派到 **task worker** 上执行，那里 `IsInRenderingThread()` 为 false。

**照抄引擎现成范式时，必须核实「调用上下文」而非只看「数据结构形状」。** `FPixelRenderCounters::AddViewStatistics` 的 `check(IsInRenderingThread())` 之所以安全，是因为它由 `SceneRendering.cpp:4046` 在渲染线程直接调用；而 base pass 的采集代码在 RDG pass lambda 内，线程前提完全不同。

### 3.3 在 RDG pass lambda 内必须遵守的三条

| 禁止 | 原因 | 替代 |
|---|---|---|
| `check(IsInRenderingThread())` / `IsInRenderingThread()` 断言 | 跑在 task worker，断言必挂 | 删断言，或改为不依赖线程的校验 |
| `CVar.GetValueOnRenderThread()` | 内部 `ensure(IsInParallelRenderingThread())` | `CVar.GetValueOnAnyThread()`（`IConsoleManager.h:1732`） |
| 非原子的 `+=` 累加 | 多 view 可能并发累加 | `std::atomic<uint32>` + `fetch_add(..., relaxed)`；帧翻转用 `exchange(0, relaxed)` |

> 反过来说：`FParallelMeshDrawCommandPass::WaitForSetupTask()` 在 worker 上是**安全**的 —— `MeshDrawCommands.cpp:1668` 用 `IsInActualRenderingThread()` 三目自行分流到 `AnyThread`。判断某个 API 能否在 worker 调用，要看它自己的线程分支，不能一概而论。

## 四、stat 口径差异：为什么 `stat drawcount` 的 Basepass 会多于上限

### 4.1 现象

设 `r.Mobile.BasePassMaxDC = N`，`stat drawcount` 的 `Basepass` 计数为 N + k（实测 k = 1）。

**这不是截断的 off-by-one** —— `FMath::Min` 之后的循环 `for (i = 0; i < 0 + NumDrawsToSubmit; i++)` 精确提交 N 个 MDC。

### 4.2 根因：RHI category 的作用域大于 CVar 的管辖范围

`SCOPED_GPU_STAT(RHICmdList, Basepass)` 在 `MobileBasePassRendering.cpp:626` 就把 draw category 设好了，并一直持续到函数结束：

| 行 | 内容 | 受 CVar 限制 |
|---|---|---|
| :626 | `SCOPED_GPU_STAT(RHICmdList, Basepass)` ← category 起点 | — |
| :633 | `[BasePass].Draw(..., MaxDC)` | ✅ **唯一受限点** |
| :641 | `SkyPass.Draw(...)`（`EngineShowFlags.Atmosphere` 为真时） | ❌ |
| :649 | `RenderMobileEditorPrimitives(...)` | ❌ |

第三条放大途径：**MDC 批次拆分**。`InstanceCullingContext.cpp:1910-1918`，当 `DrawCommandInfo.NumBatches > 1` 时一个 MDC 会发多个 RHI draw（`SubmitDraw` + 循环 `SubmitDrawEnd`），使 RHI draw 数可以 > MDC 数。

### 4.3 结论：语义正确，无需修

CVar 的语义是「限制 **base pass** 的 DC」，而天空与编辑器图元本就不是 base pass 的 DC，不该被这个旋钮管。差异属口径使然，已在 CVar help 文本中记录。

### 4.4 编辑器测得的值不能外推到真机

`ld.RenderEditorPrimitives` 在普通编辑器视口默认 **1**（`EditorViewportClient.cpp:6539`，仅 Level Designer 模式设 0），所以：

| 环境 | `Basepass` 构成 |
|---|---|
| 编辑器（Android Vulkan Preview） | base pass + 天空 + **编辑器图元** |
| 真机 Android | base pass + 天空 |

要在真机上拿准数，必须**在设备上量**。

### 4.5 若要数字与上限严格等值

给被截断的 base pass 单开一个 draw-count category（`FRHIDrawStatsCategory`），或自建计数器。本次未采用：需求只是限流旋钮，差值可接受。

## 五、`stat drawcount` 在 Mobile 上是否可用

**可用，但有前提。** 三道门：

| 门 | 内容 | 结论 |
|---|---|---|
| `HAS_GPU_STATS` | `((STATS \|\| CSV_PROFILER_STATS \|\| GPUPROFILERTRACE_ENABLED) && !UE_BUILD_SHIPPING)`（`RHIDefinitions.h:53`） | Android **Development** 包可用；**Shipping 包整个 stat 系统被剥离**（`stat unit` 同样没有，故对两种方案对等） |
| RHI 是否计数 | Vulkan：`VulkanCommands.cpp:1100/1148` 调 `RHI_DRAW_CALL_STATS`、`:1122/1173/1200/1263` 调 `RHI_DRAW_CALL_INC`；GLES：`OpenGLCommands.cpp:2284/2430` | ✅ 都计数 |
| 计数写入时机 | `ProcessAsFrameStats()` 由 `RHICommandList.cpp:1512` 在每帧 `EndFrame` **无条件**调用；`Stats_AddDraw()`（`RHICommandList.h:1469`）无额外门控 | ✅ 不依赖 GPU timer 是否开启 |

`Basepass` 这个 category 是现成的：`DEFINE_GPU_DRAWCALL_STAT(Basepass)`（`BasePassRendering.cpp:229`，声明在 `BasePassRendering.h:159`）注册名为 `"Basepass"` 的类别，`stat drawcount` 逐 category 打印。

> ⚠️ `RenderStatDrawCount` 的源码注释写着 "may always report 0 ... if AreGPUStatsEnabled is not enabled"，**该注释与当前实现不符**：`AreGPUStatsEnabled()` 只门控 GPU **计时**（`FScopedGPUStatEvent`），draw 计数走 `FScopedDrawStatCategory` + `Stats_AddDraw`，无此依赖。

## 六、MDC 批次拆分的既有实现（截断安全性）

`SubmitDrawCommands` 有 `check(MeshDrawCommandInfos.Num() >= StartIndex + NumMeshDrawCommands)`（`InstanceCullingContext.cpp:1859`），截小只会更安全。每个 MDC 用自己的 `DrawCommandInfo` 索引（`:1878-1919`），跳过尾部不影响前面。instance data / indirect args 在 `BuildRenderingCommands` 阶段已全量生成 —— 截断会产生少量带宽浪费（已生成的实例数据不再被消费），但不会出错。

## 七、快速排查 Checklist

**改动了 base pass 相关代码后，按序自查：**

1. **有没有在 RDG pass lambda 里做线程假设？**
   - 有 `IsInRenderingThread()` 断言 → 删
   - 有 `GetValueOnRenderThread()` → 换 `GetValueOnAnyThread()`
   - 有非原子累加 → 换 `std::atomic` + `fetch_add` / `exchange`
2. **DLL 是否比源码新**（改过 `.cpp/.h` 后最容易漏）：
   ```bash
   ls -l --time-style=+%H:%M:%S UE5EA/Engine/Binaries/Win64/UnrealEditor-Renderer.dll
   ls -l --time-style=+%H:%M:%S UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.cpp
   ```
   引擎源码改动的产物在 **`UE5EA/Engine/Binaries/Win64/`**，不是 `S1Game/Binaries/Win64/`。**判新旧只认 mtime** —— UE 的 `TEXT()` 字面量是 UTF-16，`strings` 搜不到会反向误导。
3. **编译时编辑器必须先关掉**：编辑器在跑时构建会 `EXIT=6` 直接失败。
4. **截断验证要做 A/B 对照**：设 `MaxDC=1` 与 `MaxDC=0` 各截一张图对比。**只看 `MaxDC=1` 一张会误判** —— 若关卡是空的（0 Actor），两张都是全黑，会被误读成「截断生效，几何全消失」。
5. **`stat drawcount` / `stat unit` 的屏幕数字 MCP 截图抓不到**（FCanvas 叠加层不在截图内），需人工肉眼读；需程序化取数走 CSV Profiler（`RHI.cpp:1514` 会把各 category 的 draw 数记入 CSV）。

## 八、关键代码位置速查

| 位置 | 作用 |
|---|---|
| `MobileBasePassRendering.cpp:611` | `r.Mobile.BasePassMaxDC` CVar 定义 |
| `MobileBasePassRendering.cpp:626` | `SCOPED_GPU_STAT(RHICmdList, Basepass)` ← category 起点 |
| `MobileBasePassRendering.cpp:633` | base pass `Draw()`（**唯一受限点**） |
| `MobileBasePassRendering.cpp:641 / :649` | SkyPass / 编辑器图元（不受限，导致 §四 的口径差） |
| `MeshDrawCommands.h:165` / `.cpp:1881` | `Draw()` 及上限参数 |
| `MeshDrawCommands.cpp:1899` | `NumDrawsToSubmit` 计算（`FMath::Min`） |
| `InstanceCullingContext.cpp:1859` | `check(MeshDrawCommandInfos.Num() >= ...)` |
| `InstanceCullingContext.cpp:1910-1918` | MDC 批次拆分（一个 MDC 发多个 RHI draw） |
| `MeshDrawCommands.cpp:1662-1670` | `WaitForMeshPassSetupTask()` 的线程分流（worker 安全） |
| `BasePassRendering.cpp:229` | `DEFINE_GPU_DRAWCALL_STAT(Basepass)` |
| `RHI.cpp:1488` | `DisplayCounts` 拷贝（0.5s 防抖，:1453-1464） |
| `RHICommandList.cpp:1512` | `ProcessAsFrameStats()` 无条件调用点 |
| `RHIDefinitions.h:53` | `HAS_GPU_STATS` 定义 |
| `EditorViewportClient.cpp:6539` | `ld.RenderEditorPrimitives 1`（普通模式） |

## 九、相关参考

- 规划与排查记录：`D:\GR_DevTest\.planning\2026-09-12-mobile-basepass-maxdc-and-statunit-row\`（`task_plan.md` / `findings.md` / `progress.md`）
- UE 官方 MDC 提交路径：`FParallelMeshDrawCommandPass::Draw` / `FInstanceCullingContext::SubmitDrawCommands`
- 存量关联笔记：
  - `E:\AiDoc\UE-Mobile-PreZ-Pass-作用与只画Mask材质原理.md`（同为 mobile base pass 前置 pass）
  - `r.MeshDrawCommands.Stats` 三角面数口径（屏显 per-pass、post GPU culling）
