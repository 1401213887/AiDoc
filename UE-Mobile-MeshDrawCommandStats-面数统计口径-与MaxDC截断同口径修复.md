# UE-Mobile-MeshDrawCommandStats-面数统计口径-与MaxDC截断同口径修复

> `r.MeshDrawCommands.Stats 1` 报的是"不截断会画多少"而不是"实际画了多少"——因为统计采集发生在 mesh pass 的 setup task 里，而 `r.Mobile.BasePassMaxDC` 的截断发生在之后的 `Draw()` 提交阶段，中间隔着一个 `WaitForMeshPassSetupTask()`。修复方式是让 `Draw()` 把实际提交数回写给统计侧，两个消费点按它截断。

---

## 一、`r.MeshDrawCommands.Stats 1` 屏显到底是什么

### 屏显形态

```
MeshDrawCommandStats (Triangles / Budget - Category):
   1234K - BasePass
    321K - ShadowDepths
     88K - PrePass
      ...
   1890K - TOTAL
```

屏幕左上角，走 `FCoreDelegates::OnGetOnScreenMessages` 通道（与 `stat` 系 on-screen 消息同一区域）。

### 每个数字的含义

| 项 | 含义 |
|---|---|
| 单位 | **千三角形（K）**，`PrimitiveCount / 1000` 整数截断 |
| 口径 | `VisibleInstanceCount × PrimitiveCount`，其中 `VisibleInstanceCount` 从 **indirect args GPU readback** 读回 ⇒ **实例剔除之后**真正会被绘制的三角形 |
| 分组 | `Stats 1` 时 `bShowPassNameStats = true`，按 **PassName** 分组（`Stats 2..N` 才按 ini 里配置的 Budget category） |
| 排序 | 按 pass 名排序，最后一行 TOTAL |

源码：`Renderer/Private/MeshDrawCommandStats.cpp:42-51`（CVar 定义）、`:147-214`（屏显 lambda）、`:331`（累加循环）。

### 三个必须知道的坑

**坑 1：第一行的总面数不显示（引擎 bug）**

```cpp
// MeshDrawCommandStats.cpp:155
OutMessages.Add(..., FString::Printf(TEXT("MeshDrawCommandStats (Triangles / Budget - Category):"), Stats.TotalPrimitives / 1000));
```

格式串里**没有 `%d`**，参数被直接丢弃。⇒ **总数只能看最后一行 `TOTAL`**。

**坑 2：默认项目配置下不会走 budget 分支**

`S1Game/Config` 与 `UE5EA/Engine/Config` 里都没有 `UMeshDrawCommandStatsSettings.Budgets/BudgetTotals` 配置 ⇒ `StatCollections.Find(1)` 为 null ⇒ 所有 pass 落进 `UntrackedPrimitives`，走 `\t%5dK - <PassName>` 格式；`PrimitiveBudget == 0` 再走 `\t%5dK - TOTAL`。

**结论：`Stats 2..N` 在当前项目下也只会打印 TOTAL 行，没有分类别。**

**坑 3：数据滞后 1~2 帧**

`FFrameData::IsCompleted()` 要等 GPU readback 完成才处理，屏显的是回读到的上一帧（或更早）的值。同一份数还会写进 `stat rhi` 的 Triangles：

```cpp
// MeshDrawCommandStats.cpp:428-431
GNumPrimitivesIndirectDrawnRHI[GPUIndex] = Stats.TotalPrimitives;
GNumPrimitivesDrawnRHI[GPUIndex] = GNumPrimitivesIndirectDrawnRHI[GPUIndex] + GNumPrimitivesDirectDrawnRHI[GPUIndex];
SET_DWORD_STAT(STAT_RHITriangles, GNumPrimitivesDrawnRHI[0]);
```

### 编译条件与开销

- `#define MESH_DRAW_COMMAND_STATS (!UE_BUILD_SHIPPING)` ⇒ **Shipping 包没有这个 CVar**
- 开启即 `bCollectStats = true`，每帧回读 indirect args，**有条件地开、测完关**
- 只覆盖走 MeshDrawCommand 的 mesh pass，**不含 Slate / 非 MDC 绘制**

---

## 二、口径缺陷：为什么 TOTAL 会偏高

### 时序

| # | 步骤 | 位置 | 作用对象 |
|---|---|---|---|
| ① | **统计采集** | `CollectMeshDrawCommandPassStats(Context.MeshDrawCommands, ...)` — `MeshDrawCommands.cpp:1304`，在 `FMeshDrawCommandPassSetupTask::AnyThreadTask()` 内 | `PassStats->DrawData.SetNum(VisibleMeshDrawCommands.Num())` @`:1041` ⇒ **全量 N 条** |
| ② | 等 setup 完成 | `WaitForMeshPassSetupTask()` @ `MeshDrawCommands.cpp:1897` | — |
| ③ | **截断**（CVar 生效点） | `FMath::Min(MaxNumDrawsToSubmit, TaskContext.MeshDrawCommands.Num())` @`:1901-1903` | 只影响提交数 |
| ④ | **累加出屏显数据** | `MeshDrawCommandStats.cpp:331`：`for (CmdIndex = 0; CmdIndex < PassStats->DrawData.Num(); ++CmdIndex)` | **全量 N 条** |

①和④之间**没有任何一处把 `MaxNumDrawsToSubmit` 传给 stats 侧**。

### 后果

`r.Mobile.BasePassMaxDC > 0` 时：

- **`BasePass` 那一行偏高**（含被跳过的 MDC）
- **`TOTAL` 行偏高**（是各 pass 之和，BasePass 项被污染）
- 其它 pass 行不受影响（CVar 只管 BasePass）

**被跳过的 MDC 在统计侧依然完整**：`DrawData` 有槽位（全量 `SetNum`），间接绘制的 `VisibleInstanceCount` 来自 setup 阶段写满的 indirect args buffer、截断不回写，非间接的 `VisibleInstanceCount = MeshDrawCommand->NumInstances * NumInstances`（`MeshDrawCommands.cpp:1107`）也在 setup 阶段定死。

⇒ **同场景同相机下，`MaxDC=N` 与 `MaxDC=0` 两态的 Stats 数值逐位相同**。自证方法：`r.Mobile.BasePassMaxDC 1` 时画面几乎全空，Stats 照样报全量面数。

---

## 三、修复方案

**允许截断信息写回统计侧**：`Draw()` 是唯一知道"实际提交了多少条"的地方，把它回写；两个消费点各自取上界。

### 关键前提（成立才能这样做）

`DrawData[i]` 与 `MeshDrawCommands[i]` **索引严格一一对应**：

- `CollectMeshDrawCommandPassStats` 按 `for (DrawCommandIndex = 0; ... < VisibleMeshDrawCommands.Num())` 顺序填充 `DrawData[DrawCommandIndex]`（`MeshDrawCommands.cpp:1046-1054`）
- `:1039` 有 `check(VisibleMeshDrawCommands.Num() == InstanceCullingContext.MeshDrawCommandInfos.Num())`
- `MeshDrawCommands.Sort()` 在 `:1291`、采集在 `:1304` ⇒ **排序在前、采集在后**，所以统计顺序 == 提交顺序，"少累加尾部 N 条"精确等于"少提交尾部 N 条"

### 四处改动

**① `MeshDrawCommandStats.h` — 加成员**（`FMeshDrawCommandPassStats` 内）

```cpp
#pragma region Engine ZXB
	// [ZXB] 本帧实际提交的 MDC 数，由 FParallelMeshDrawCommandPass::Draw() 截断后写入（r.Mobile.BasePassMaxDC）。
	// 统计侧只累加前这么多条，否则面数报的是"不截断会画多少"。< 0 表示未受限。
	int32 NumSubmittedDraws = -1;
#pragma endregion
```

**② `MeshDrawCommands.cpp` — `Draw()` 截断后回写**（写在 `NumDrawsToSubmit <= 0` 早退**之前**）

```cpp
	const int32 NumDrawsToSubmit = MaxNumDrawsToSubmit < 0
		? TaskContext.MeshDrawCommands.Num()
		: FMath::Min(MaxNumDrawsToSubmit, TaskContext.MeshDrawCommands.Num());
#if MESH_DRAW_COMMAND_STATS
	// [ZXB] 把实际提交数回写给统计侧，r.MeshDrawCommands.Stats 只累加这部分，避免面数偏高。
	if (FMeshDrawCommandPassStats* PassStats = TaskContext.InstanceCullingContext.MeshDrawCommandPassStats)
	{
		PassStats->NumSubmittedDraws = NumDrawsToSubmit;
	}
#endif
	if (NumDrawsToSubmit <= 0)
	{
		return;
	}
```

**③ `MeshDrawCommandStats.cpp` — `Update()` 累加循环**

```cpp
	const int32 NumDrawsToConsider = PassStats->NumSubmittedDraws < 0
		? PassStats->DrawData.Num()
		: FMath::Min(PassStats->NumSubmittedDraws, PassStats->DrawData.Num());
	for (int32 CmdIndex = 0; CmdIndex < NumDrawsToConsider; ++CmdIndex)
```

**④ `MeshDrawCommandStats.cpp` — `DumpStats()` 导出循环**（CSV 明细与屏显同口径）

同上，把 range-for 改为带上界的索引循环。

> **`DumpStats` 必须一起改**：否则 CSV 明细含被丢弃条目、与屏显对不上，制造新的口径混乱。

### 可达性依据

- `FInstanceCullingContext::MeshDrawCommandPassStats` 是 **public** 成员：`Renderer/Public/InstanceCulling/InstanceCullingContext.h:360-366`，整段在 `#if MESH_DRAW_COMMAND_STATS` 内
- `FMeshDrawCommandPassSetupTaskContext::InstanceCullingContext` 是 public 成员：`Renderer/Private/MeshDrawCommands.h:108`
- `Draw()` 是 const 成员函数，但 `MeshDrawCommandPassStats` 是**指针成员**，浅 const ⇒ `PassStats->NumSubmittedDraws = N` 合法
- `MeshDrawCommands.cpp:16` 已 `#include "MeshDrawCommandStats.h"`，**不需要新增 include**

### 边界与安全性

| 情形 | 行为 |
|---|---|
| `MaxDC=0`（默认） | `MobileBasePassRendering.cpp:635` 传 `-1` ⇒ `NumDrawsToSubmit = Num()` ⇒ 写回全量 ⇒ 与改前**逐字节等价** |
| `MaxDC > 0` 且小于总数 | 写回 MaxDC ⇒ 只累加前 MaxDC 条 |
| `MaxDC > 0` 但总数更少 | `FMath::Min` 取总数 ⇒ 全量，正确 |
| `NumDrawsToSubmit <= 0` | 写回 0 后再早退 ⇒ 该 pass 计 0 条（修掉"全跳过却报全量") |
| 未走 CVar 的 pass（SkyPass / PC / 其它） | 传 `-1` ⇒ 写回全量 ⇒ 行为不变 |
| `Dispatch()` 路径（PC Deferred） | 不写 ⇒ 保持 `-1` ⇒ 全量，正确（PC 本就没截断） |
| HitProxy pass | `CreatePassStats` 不创建（`InstanceCullingContext.cpp:211`），指针为 null ⇒ 判空跳过 |

**线程安全**：写（`Draw()`，渲染线程）与读（`Update()`，`FCoreDelegates::OnEndFrameRT`，同为渲染线程）**同线程**，**不需要新增原子或锁**。`Update()` 只处理 `IsCompleted()` 的旧帧，而该帧所有 `Draw()` 必已在帧末前调完。

**多 view**：`FViewInfo::ParallelMeshDrawCommandPasses` 是 per-view，各自 `CreatePassStats` 出独立 `PassStats`，互不干扰。

**不改的地方**：`Update()` 里 `InstanceCullingGPUBufferReadback->Lock(PassStats->DrawData.Num())` 的 Lock 长度对应**全量** readback buffer，**不动**；`FFrameData::Validate()`（`:89-97`）校验的是 indirect buffer 一致性，与是否提交无关，**不动**。

---

## 四、Slate 侧核对结论（`r.Mobile.SlateUIMaxDC` 无同类问题）

详见 `E:\AiDoc\UE-Mobile-SlateUIMaxDC-SlateUI-DC限流-渲染链路与编辑器副作用.md` §十二。摘要：

| 问题 | `BasePassMaxDC` | `SlateUIMaxDC` |
|---|---|---|
| 统计早于截断 ⇒ 读数偏高 | **有**（已修） | **无** |
| 能否被 `r.MeshDrawCommands.Stats` 看到 | 能 | **不能**（Slate 不在 MDC 统计内） |
| 三角形是否进 `stat rhi` | 是 | **否** |
| 读数出口 | Stats 面板 + `stat rhi` | 仅 `stat drawcount`（RHI 口径，天然跟随） |

**为什么 Slate 没有同类问题**：`NumOpsCreated` 既是截断判据（`SlateRHIRenderingPolicy.cpp:1806`）也是实际提交数（`:1826`），**同一变量、同一时刻**；且累加条件与真正发 draw 的条件逐字一致：

```cpp
// :1820-1826 计数
if (RenderBatchOp->bShow) { NumOpsCreated++; }
// :1528-1532 发 draw
if (RenderBatchOp.bShow) { RHICmdList.DrawIndexedPrimitive(...); }
```

**Slate 的三角形不进 `stat rhi` 的原因**：`GNumPrimitivesDirectDrawnRHI` 在整个 Runtime **只有定义（`RHIStats.cpp:11`）与读取（`UnrealClient.cpp:881`、`MeshDrawCommandStats.cpp:429`），无任何写入点**，恒为 0 ⇒ `stat rhi` 的 Triangles 实际等于 `Stats.TotalPrimitives`（纯 MDC）。

---

## 五、仍未消除的既有口径差（与本次改动无关）

| 口径差 | 说明 |
|---|---|
| `SubmitDrawCommands` 内部的跳过 | `InstanceCullingContext.cpp:1880-1893` 有 Imposter 跳过逻辑（`continue` 不发 draw）⇒ "提交 N 个 MDC" 仍可能 ≠ "画了 N 个"。**在没有 MaxDC 的版本上同样存在**，本次未处理 |
| `stat drawcount` 的 `Basepass` 行 | 是 RHI 口径，含 SkyPass + `RenderMobileEditorPrimitives` + MDC 批次拆分 ⇒ **会高于 CVar 上限**。这是口径差不是 bug |
| Slate 的 `stat drawcount` 行 | 含 `CustomDrawer` / `PostProcess` 旁路（不受限却计入）⇒ 可能高于上限 |

---

## 六、提交风险：CL 1129345 分支覆盖事件

**服务端 head 上 `r.Mobile.BasePassMaxDC` 的实现曾整体消失**：

| 文件 | 本地 have | 服务端 head | head 来源 |
|---|---|---|---|
| `MeshDrawCommands.cpp` | **#8**（含 `MaxNumDrawsToSubmit`） | #9（**无**该参数、无截断） | CL 1129345 |
| `MeshDrawCommands.h` | **#7** | #8 | CL 1129345 |
| `MobileBasePassRendering.cpp` | **#14**（含 CVar 定义） | #15 | CL 1129345 |
| `MeshDrawCommandStats.cpp` | #3 | #4 | CL 1129345 |
| `MeshDrawCommandStats.h` | #2 | #3 | CL 1129345 |

- `CL 1128328`（zhangjianguo，2026/09/14 10:27，`--story=1103059 ... 调试指令`）提交了 MaxDC 的实现
- `CL 1129345`（tools@，同日 17:50，7.4 小时后）`[BranchCopy] Copy from //GR/PackageVersion/...@1129228`，**一次覆盖 2350 个文件**，把 MaxDC 整体回退；同时带入了 `MeshDrawCommandStats.*` 里他人对 `TSharedPtr<FRHIGPUBufferReadback>` 的改动与 `ALLOW_USE_CONSERVATIVE_RASTERIZATION` 等上游代码

**⇒ 提交这几个文件前必须 `p4 sync` + resolve，resolve 时不要覆盖 CL 1129345 带入的他人改动。**

查证方式：

```bash
P4CLIENT=<workspace> p4 filelog -m 3 //GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/MeshDrawCommands.cpp
```

---

## 七、快速排查 Checklist

1. **数字对不上？** → 先确认看的是哪个读数：`r.MeshDrawCommands.Stats` 是 MDC 口径、`stat rhi` 的 Triangles 同源、`stat drawcount` 是 RHI 口径含旁路
2. **TOTAL 行看不到总数？** → 第一行标题的 `%d` 缺失是引擎 bug，看最后一行 `TOTAL`
3. **设了 `r.Mobile.BasePassMaxDC` 但 Stats 数值没变？** → 修复前这是**预期行为**（报告的是不截断的面数）；修复后才跟随
4. **想验证截断是否生效** → 看画面（远处物体消失）或 `stat drawcount` 的 `Basepass` 行，**不要**用 Stats 面板
5. **Slate 的面数在哪看？** → 没有；Slate 不进任何面数统计，要看只看 `stat drawcount` 的 DC 数
6. **改了这几个文件要提交？** → 先 `p4 filelog -m 3` 确认 head 来源，再 sync + resolve

---

## 八、相关参考

### 引擎源码

> 行号取自 2026-09-21 的 UE5EA fork 快照。本次改动后 `MeshDrawCommandStats.cpp` 的相关位置会下移数行，**定位请以函数名为准**。

| 位置 | 内容 |
|---|---|
| `Renderer/Private/MeshDrawCommandStats.cpp:42-51` | CVar 定义与 help |
| `Renderer/Private/MeshDrawCommandStats.cpp:147-214` | 屏显 lambda（含第一行缺 `%d` 的 bug @`:155`） |
| `Renderer/Private/MeshDrawCommandStats.cpp:331` | `Update()` 累加循环（本次改动点） |
| `Renderer/Private/MeshDrawCommandStats.cpp:564-662` | `DumpStats()` CSV 导出（本次改动点） |
| `Renderer/Private/MeshDrawCommandStats.cpp:89-97` | `FFrameData::Validate()`（**不该截断**） |
| `Renderer/Private/MeshDrawCommandStats.h:39-63` | `FMeshDrawCommandPassStats`（本次新增成员） |
| `Renderer/Private/MeshDrawCommands.cpp:1881-1929` | `FParallelMeshDrawCommandPass::Draw()` 截断点（本次改动点） |
| `Renderer/Private/MeshDrawCommands.cpp:1026-1112` | `CollectMeshDrawCommandPassStats` |
| `Renderer/Private/MeshDrawCommands.cpp:1289-1305` | 排序 → `SetupDrawCommands` → 采集 的顺序 |
| `Renderer/Private/MobileBasePassRendering.cpp:608-636` | CVar 定义与调用点 |
| `Renderer/Public/InstanceCulling/InstanceCullingContext.h:360-366` | `MeshDrawCommandPassStats` 指针（public） |
| `Renderer/Private/InstanceCulling/InstanceCullingContext.cpp:1880-1893` | Imposter 跳过（残余口径差） |
| `Engine/Public/MeshDrawCommandStatsSettings.h` | 预算配置（编辑器里显示为 "Mesh Stats"） |

### 本项目文档

- `E:\AiDoc\UE-Mobile-BasePass-DC上限-CVar截断实现与stat-drawcount口径差异.md` — **前作**：MaxDC 的实现与 `stat drawcount` 口径差异
- `E:\AiDoc\UE-Mobile-SlateUIMaxDC-SlateUI-DC限流-渲染链路与编辑器副作用.md` — Slate 侧链路 + §十二 本次核对结论
- `E:\AiDoc\UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md` — ReadOnly CVar 的启动期通道

### 规划记录

- `D:\GR_DevTest\.planning\2026-09-21-maxdc-stats-truncation\`（task_plan / findings / progress）
