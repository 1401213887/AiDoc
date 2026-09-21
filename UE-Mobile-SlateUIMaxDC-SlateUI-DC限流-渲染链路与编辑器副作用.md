# UE Mobile SlateUIMaxDC：Slate UI 的 Draw Call 限流实现与渲染链路

> 参照 `r.Mobile.BasePassMaxDC` 的形态，为 Slate UI 增加一个 DC 限流旋钮 `r.Mobile.SlateUIMaxDC`（默认 0 不限制），用于量化 UI 的 DC 数量对帧率的影响。**Slate 与 base pass 的结构完全不同**：base pass 有现成的 MeshDrawCommand 数组可以 `FMath::Min` 一刀切，Slate 走的是 `FSlateRenderBatchOp` 链表，必须在 batch 遍历环节拦截。落点定在「创建 op 之前」，依据是 **1 个 Primitive batch → 0/1 个 op → 0/1 次 RHI draw** 这个粒度关系 —— 它使得按 op 计数严格等于按 `stat drawcount` 的 `Slate UI` 口径计数。

---

## 一、需求与最终形态

`stat drawcount` 逐 category 打印 RHI draw 数，其中一项是 `Slate UI`。需求是给它加一个可限流的 CVar。

| 项 | 值 |
|---|---|
| CVar | `r.Mobile.SlateUIMaxDC`，默认 `0`（不限制），`ECVF_RenderThreadSafe` |
| 语义 | > 0 时，Slate 每窗口提交的 DC 数不超过该值，超出的**从尾部**跳过 |
| 改动文件 | `Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderingPolicy.cpp`（单文件，3 处） |
| 命名说明 | 沿用 `r.Mobile.` 前缀仅为与 `r.Mobile.BasePassMaxDC` 成组；**Slate 渲染平台无关，PC 上同样生效** |

## 二、背景：`Slate UI` 这个 category 的来历

```cpp
// SlateRHIRenderer.cpp:68
DECLARE_GPU_DRAWCALL_STAT_NAMED(SlateUI, TEXT("Slate UI"));   // 注意显示名带空格
```

作用域在 `FSlateRHIRenderer::DrawWindow_RenderThread`：

```cpp
// SlateRHIRenderer.cpp:675
RDG_GPU_STAT_SCOPE(GraphBuilder, SlateUI);
```

`RDG_GPU_STAT_SCOPE` 展开包含 `FScopedDrawStatCategory`，把该 RDG 范围内所有 RHI draw 记入 `SlateUI` category → `stat drawcount` 出现 `Slate UI: <N>`。

**它是每窗口独立的**：`DrawWindow_RenderThread` 每个 Slate 窗口跑一次。

## 三、Slate 的绘制链路（从 batch 到 RHI draw）

```
FSlateRHIRenderer::DrawWindow_RenderThread                    SlateRHIRenderer.cpp:677 起
  └─ AddSlateDrawElementsPass(GraphBuilder, Policy, Inputs,
                              BatchData.GetRenderBatches(), FirstBatchIndex, ...)
        SlateRHIRenderer.cpp:801（HDR batch）/ :813（常规 batch）
        ↓
     AddSlateDrawElementsPass                               SlateRHIRenderingPolicy.cpp:1531
        │
        ├─ while (NextRenderBatchIndex != INDEX_NONE)        :1676  沿 NextBatchIndex 链表遍历 batch
        │     switch (GetSlateRenderBatchType(NextRenderBatch))  :1719
        │       case CustomDrawer : ICustomSlateElement::Draw_RenderThread（自绘，独立发 draw）
        │       case PostProcess  : AddSlatePostProcessBlurPass
        │       case Primitive    : CreateSlateRenderBatchOp(...)   :1785  ← 每 batch 生成 1 个 op，追加链表
        │
        └─ FlushDrawElementsPass()                           :1636
              └─ GraphBuilder.AddPass("ElementBatch", ...)    :1650
                   lambda 内：for (; RenderBatchOp; RenderBatchOp = RenderBatchOp->Next)   :1662
                                  DrawSlateRenderBatch(RHICmdList, DrawState, *Inputs, *RenderBatchOp);   :1664
```

## 四、粒度论证：为什么能按 op 计数当成 DC 计数

这是整个实现能成立的**前提**，两段链都要成立：

**batch → op**：`CreateSlateRenderBatchOp`（`:1105`）内部**只分配一个 op**：

```cpp
// :1366
FSlateRenderBatchOp* RenderBatchOp = GraphBuilder.AllocPOD<FSlateRenderBatchOp>();
```

另有 4 个提前 `return nullptr` 的分支（`!GEngine`、`MaterialRenderProxy` 为空等）。⇒ **1 个 Primitive batch ↔ 0 或 1 个 op**。

**op → draw**：`DrawSlateRenderBatch`（`:1388`）每个 op 恰好发**一次** draw：

```cpp
// :1531
RHICmdList.DrawIndexedPrimitive(ElementsIndexBuffer, 0, 0, RenderBatch.NumVertices,
                                RenderBatch.IndexOffset, PrimitiveCount, RenderBatch.InstanceCount);
```

（两个例外，见 §六：AsyncPSO 未就绪时提前 `return`；`bShow == false` 时不发。）

⇒ **1 op = 1 RHI draw**，故「按 op 计数」与 `stat drawcount` 的 `Slate UI` 口径一致。

## 五、实现

### 5.1 CVar（`:79`）

```cpp
#pragma region Engine ZXB
// [ZXB] 限制 Slate 每窗口提交的 draw call 数，用于量化 UI 的 DC 开销。Slate 渲染平台无关，PC 上同样生效。
static TAutoConsoleVariable<int32> CVarSlateUIMaxDC(
	TEXT("r.Mobile.SlateUIMaxDC"),
	0,
	TEXT("Limits the number of draw calls Slate UI submits per window.\n")
	TEXT("One render batch op equals one draw call; ops beyond the limit are skipped, so the UI is drawn partially.\n")
	TEXT("Custom drawer and post-process batches use their own draw paths and are not limited, so 'stat drawcount'\n")
	TEXT("may still report a higher 'Slate UI' count than this value.\n")
	TEXT("0: no limit (default)"),
	ECVF_RenderThreadSafe);
#pragma endregion
```

### 5.2 预算计数器（`AddSlateDrawElementsPass` 内，`:1646`）

```cpp
#pragma region Engine ZXB
	// [ZXB] NumRenderBatchOps 每次 flush 归零，不能当全局预算，故另立一个跨 pass 累计的计数。
	const int32 MaxDC = CVarSlateUIMaxDC.GetValueOnAnyThread();
	int32 NumOpsCreated = 0;
#pragma endregion
```

> ⚠️ `NumRenderBatchOps`（原文件 `:1634`）是**每个 RDG pass 内**的 op 计数、`FlushDrawElementsPass` 后归零，**不能**直接当全局预算。

### 5.3 截断（`case ESlateRenderBatchType::Primitive`，`:1801`）

```cpp
		case ESlateRenderBatchType::Primitive:
		{
#pragma region Engine ZXB
			// [ZXB] 达上限后跳过后续 batch：只截尾不挖洞，避免破坏 op 之间的 clipping 状态机。
			if (MaxDC > 0 && NumOpsCreated >= MaxDC)
			{
				break;
			}
#pragma endregion
			if (FSlateRenderBatchOp* RenderBatchOp = CreateSlateRenderBatchOp(GraphBuilder, RenderBatchCreateInputs, &NextRenderBatch, NextClippingOp, FinalBrushVTData))
			{
				// ...（原有链表追加代码）
				NumRenderBatchOps++;
#pragma region Engine ZXB
				// [ZXB] bShow=false 的 op（VT feedback 路径）在 DrawSlateRenderBatch 里不发 draw，不计入预算。
				if (RenderBatchOp->bShow)
				{
					NumOpsCreated++;
				}
#pragma endregion
			}

			break;
		}
```

### 5.4 三个设计决策的理由

| 决策 | 理由 |
|---|---|
| 粒度取 **op 级**而非 batch 级 | 1 op = 1 draw，与 `Slate UI` 计数口径严格一致（batch 级会偏，因为 batch↔op 是 0/1 关系） |
| 截断点取 **创建 op 之前**（而非 RDG lambda 的 draw 循环） | 连 op 都不分配，零额外开销；语义等价但更早 |
| **只截尾、不挖洞** | `DrawSlateRenderBatch` 会按需 `SetSlateClipping` 更新 `State.LastClippingOp`；中间挖洞会让后续 op 的裁剪状态机不连续。一旦达上限就全部跳过（等价于 break），状态机不受影响 |
| 预算只算 `bShow == true` 的 op | `:1528` 的 `if (RenderBatchOp.bShow)` 决定该 op 是否真发 draw；把不发 draw 的算进预算会导致提前触顶、限制偏严 |

## 六、已知边界（当前不修，记录备查）

| 边界 | 机制 | 影响 |
|---|---|---|
| **`Slate 3D` 被连带限制** | `Slate3DRenderer.cpp:184` 调用**同一个** `AddSlateDrawElementsPass` | 世界空间 UI（`Slate 3D` category）的 DC 也被 `r.Mobile.SlateUIMaxDC` 限制，属语义溢出。若要隔离，需给函数加参数区分调用方 |
| **HDR 分支会让上限翻倍** | `bCompositeUIWithSceneHDR` 为真时同窗口调两次（`:801` HDR batch + `:813` 常规 batch），而 `NumOpsCreated` 是函数局部变量 | 实际上限 = 2×N。触发条件 `ViewportInfo.bDisplayFormatIsHDR && CompositeUIWithSceneHDR()`，移动端基本不触发 |
| **多窗口各自限 N** | `DrawWindow_RenderThread` 每窗口调一次该函数，各自独立计数 | `stat drawcount` 的 `Slate UI` 是**所有窗口累加**，读数可能是 N × 窗口数 |
| **`CustomDrawer` / `PostProcess` 不受限** | 走 `ICustomSlateElement::Draw_RenderThread` / `AddSlatePostProcessBlurPass` 各自的自绘路径，不在 op 链表里 | 其 draw 仍计入 `Slate UI`，故 drawcount 可能 > 上限。与 `r.Mobile.BasePassMaxDC` 的既有语义一致 |

## 七、⚠️ 关键副作用：编辑器里 `Slate UI` 就是编辑器自己的界面

`SlateUI` category 来自 `FSlateRHIRenderer::DrawWindow_RenderThread` —— **每个 Slate 窗口**都走这条路径，**包括编辑器主窗口**。

⇒ 在编辑器里设 `r.Mobile.SlateUIMaxDC` 会把**编辑器界面本身**花掉（菜单栏、面板、Content Browser 全部消失），不只是游戏 UI。

- **真机上**目标是游戏 UI（HUD/菜单），不会伤到编辑器
- 在编辑器里调试时若误设小值，界面会消失；**CVar 每帧读取，设回 0 立即恢复**
- MCP 通道是 TCP，**不受 Slate 影响**，所以编辑器 UI 花掉期间仍能远程设回 0

## 八、验证方法

### 8.1 为什么必须用 OS 屏幕截图

| 通道 | 能否看到 Slate UI |
|---|---|
| MCP `take_screenshot` | ❌ 只截 3D 视口渲染目标，不含 Slate 层（`stat drawcount` 的 FCanvas 文字同样截不到） |
| **OS 屏幕截图（PowerShell `CopyFromScreen`）** | ✅ **唯一有效通道** |

```powershell
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('D:\GR_DevTest\Saved\_Screenshots\slate.png', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
```

**前置**：编辑器窗口必须在前台。用 `SetForegroundWindow` + `ShowWindow(SW_MAXIMIZE=3)` 拉前台（脚本见 `.planning/2026-09-13-mobile-slateui-maxdc/`）。

**触发 Slate 重绘**：`unreal.EditorLevelLibrary.editor_invalidate_viewports()`（视口 invalidate 会带动 Slate 合成重绘）。改 CVar 本身不会触发重绘。

### 8.2 三态 A/B（实证结果）

| 状态 | 编辑器界面 |
|---|---|
| `MaxDC=0`（基线） | 菜单栏 / 左侧面板 / Content Browser / Details **完整** |
| **`MaxDC=1`** | **几乎全部消失，只剩一个空窗口** |
| 恢复 `MaxDC=0` | **完整回来** |

⚠️ **必须做对照**：只截 `MaxDC=1` 一张图会误判 —— 若关卡为空（0 Actor）或视口未渲染，截图本来就是黑的，会被读成"截断生效"。基准/恢复两张是对照组。

### 8.3 验证清单

```bash
# 1. 编译（编辑器须先关闭，否则 EXIT=6）
build_s1_editor.py "D:/GR_DevTest/S1Game/S1Game.uproject" --config Development

# 2. DLL 必须比源码新（判新旧只认 mtime；UE 的 TEXT() 是 UTF-16，strings 搜不到）
ls -l --time-style=+%H:%M:%S UE5EA/Engine/Binaries/Win64/UnrealEditor-SlateRHIRenderer.dll
ls -l --time-style=+%H:%M:%S UE5EA/Engine/Source/Runtime/SlateRHIRenderer/Private/SlateRHIRenderingPolicy.cpp

# 3. 启动后回读 CVar 注册
#    execute_console_command("r.Mobile.SlateUIMaxDC")  →  = "0"  LastSetBy: Constructor
```

## 九、快速排查 Checklist

**改 Slate 渲染相关代码后：**

1. **改动进二进制了吗** → 比 mtime（产物在 `UE5EA/Engine/Binaries/Win64/`，模块 DLL 名 `UnrealEditor-SlateRHIRenderer.dll`）
2. **编译前关编辑器了吗** → 没关会 `EXIT=6`
3. **CVar 注册了吗** → 控制台回读，看 `LastSetBy: Constructor`（来自构造函数默认值）
4. **截断生效了吗** → 三态 A/B，**必须带对照组**
5. **数字对得上吗** → `stat drawcount` 的 `Slate UI` 需**肉眼读**（截图通道抓不到）；且它是多窗口累加，不等于单窗口上限
6. **是不是把编辑器 UI 也搞没了** → 正常，见 §七；CVar 设回 0 立即恢复

## 十、关键代码位置速查

| 位置 | 作用 |
|---|---|
| `SlateRHIRenderer.cpp:68` | `DECLARE_GPU_DRAWCALL_STAT_NAMED(SlateUI, "Slate UI")` |
| `SlateRHIRenderer.cpp:675` | `RDG_GPU_STAT_SCOPE(GraphBuilder, SlateUI)` ← category 作用域 |
| `SlateRHIRenderer.cpp:801 / :813` | `AddSlateDrawElementsPass` 的两个调用点（HDR / 常规） |
| `Slate3DRenderer.cpp:184` | **同一函数**的另一调用点（`Slate 3D`，见 §六） |
| `SlateRHIRenderingPolicy.cpp:1105` | `CreateSlateRenderBatchOp`（1 batch → 0/1 op） |
| `SlateRHIRenderingPolicy.cpp:1366` | `AllocPOD<FSlateRenderBatchOp>()` ← op 分配点 |
| `SlateRHIRenderingPolicy.cpp:1388` | `DrawSlateRenderBatch` |
| `SlateRHIRenderingPolicy.cpp:1528` | `if (RenderBatchOp.bShow)` ← 决定是否真发 draw |
| `SlateRHIRenderingPolicy.cpp:1531` | `DrawIndexedPrimitive` ← 1 op = 1 draw |
| `SlateRHIRenderingPolicy.cpp:1531` 起 | `AddSlateDrawElementsPass` 定义 |
| `SlateRHIRenderingPolicy.cpp:1634` | `NumRenderBatchOps`（per-pass，会归零，勿用作预算） |
| `SlateRHIRenderingPolicy.cpp:1676` | while 遍历 batch 链表 |
| `SlateRHIRenderingPolicy.cpp:1719` | `switch (GetSlateRenderBatchType(...))` |
| `SlateRHIRenderingPolicy.cpp:1785` | `case Primitive` → `CreateSlateRenderBatchOp` |
| `SlateRHIRenderingPolicy.cpp:1662` | RDG lambda 内逐 op 绘制循环 |

## 十一、相关参考

- 同族文档（同一手法、同一次工作）：`E:\AiDoc\UE-Mobile-BasePass-DC上限-CVar截断实现与stat-drawcount口径差异.md`
  - 那篇记录了 `r.Mobile.BasePassMaxDC` 的实现，以及 **RDG pass lambda 跑在 task worker 线程**的坑（`IsInRenderingThread()` 断言崩溃）
  - 两篇共享的口径问题：CVar 上限 ≠ `stat drawcount` 的 category 计数（category 含不受限的旁路）
- 规划与排查记录：`D:\GR_DevTest\.planning\2026-09-13-mobile-slateui-maxdc\`（`task_plan.md` / `findings.md` / `progress.md`）
- 验证通道相关：
  - `E:\AiDoc\UE-Mobile-PreZ-Pass-作用与只画Mask材质原理.md`（同为 mobile 渲染pass分析）
  - MCP 截图能力的边界（截不到 FCanvas / Slate 层）

---

## 十二、`r.MeshDrawCommands.Stats` 同口径核对（2026-09-21 追加）

**背景**：`r.Mobile.BasePassMaxDC` 被查出「统计早于截断」缺陷 —— 统计采集发生在 mesh pass 的 setup task 里，截断发生在其后的 `Draw()` 提交时，中间隔着 `WaitForMeshPassSetupTask()`，于是 `r.MeshDrawCommands.Stats` 的 BasePass 行与 TOTAL 行报的是"不截断会画多少"而非"实际画了多少"。已修：`FMeshDrawCommandPassStats::NumSubmittedDraws` 由 `Draw()` 截断后回写，两个消费点（`Update()` 累加、`DumpStats()` 导出）按它截断。

本篇核对 Slate 侧有无同类问题。

### 结论 1：Slate 侧**不存在**同类问题

| 环节 | `r.Mobile.BasePassMaxDC`（有缺陷） | `r.Mobile.SlateUIMaxDC`（无缺陷） |
|---|---|---|
| 计数采集 | setup task 里采集**全量** `DrawData`（`MeshDrawCommands.cpp:1041`） | `NumOpsCreated++`（`SlateRHIRenderingPolicy.cpp:1826`） |
| 截断判据 | `Draw()` 里另一个时刻（`MeshDrawCommands.cpp:1901`） | `NumOpsCreated >= MaxDC`（`:1806`，**创建 op 之前**） |
| 两者关系 | 两个阶段，中间有同步点 | **同一变量、同一时刻** |

且计数条件与真正发 draw 的条件**逐字一致**：

```cpp
// :1820-1826 计数
if (RenderBatchOp->bShow) { NumOpsCreated++; }
// :1528-1532 发 draw
if (RenderBatchOp.bShow) { RHICmdList.DrawIndexedPrimitive(...); }
```

⇒ `NumOpsCreated` == 实际发出的 draw 数（同步路径下逐位相等），没有"偏高"的余地。

### 结论 2：Slate 的截断无法体现在任何「面数」读数上（要新增统计源才行）

- **`r.MeshDrawCommands.Stats` 完全不含 Slate**：该统计只在 mesh pass 的 setup task 里采集；Slate 的 draw 由 `DrawSlateRenderBatch` 直接 `RHICmdList.DrawIndexedPrimitive` 发出（`:1531`），不经过 MeshDrawCommand
- **Slate 的三角形也进不了 `stat rhi` 的 Triangles**：`GNumPrimitivesDirectDrawnRHI` 在整个 Runtime 只有定义（`RHIStats.cpp:11`）与读取（`UnrealClient.cpp:881`、`MeshDrawCommandStats.cpp:429`），**无任何写入点**，恒为 0；而 `GNumPrimitivesDrawnRHI = GNumPrimitivesIndirectDrawnRHI + Direct`（`MeshDrawCommandStats.cpp:429`）⇒ `stat rhi` 的 Triangles 实际等于 `Stats.TotalPrimitives`（纯 MDC 口径，且同样已被 MaxDC 截断修复覆盖）
- ⇒ 要让 UI 的几何开销可见，必须**新增统计源**（在 op 创建处累计顶点/三角形数），不是"让 Stats 跟随截断"能解决的

### 结论 3：真实存在的两处口径差（均为既有问题，与本次改动无关）

**(a) CVar 是「每次 `AddSlateDrawElementsPass` 调用」的上限，不是 help 文本写的 "per window"**

`NumOpsCreated` 是函数局部变量（`:1652`），每次调用从 0 起算；而该函数有**三个**调用点：

| 调用点 | 场景 |
|---|---|
| `SlateRHIRenderer.cpp:801` | HDR batch 组 |
| `SlateRHIRenderer.cpp:813` | 常规 batch 组（每窗口必走） |
| `Slate3DRenderer.cpp:184` | Slate 3D（世界内 UI） |

HDR 组的触发条件：`bCompositeUIWithSceneHDR = ViewportInfo.bDisplayFormatIsHDR && CompositeUIWithSceneHDR()`（`SlateRHIRenderer.cpp:740`）**且** `!BatchDataHDR.GetRenderBatches().IsEmpty()`（`:793`）。

⇒ 实际可提交上限 = `MaxDC × 实际调用次数`。移动端在 `bDisplayFormatIsHDR == false` 时（Android 常规输出格式；**该值未在设备上实测**）只有 `:813` 一次调用 ⇒ "per window" 语义成立；PC/HDR 场景下 help 文本不准确。另：多窗口（编辑器主窗口 + 各浮动窗口）天然各自计一份。

**(b) `CustomDrawer` / `PostProcess` 不计入预算但计入 drawcount**（help 文本已写明，此处补代码依据）

- `case ESlateRenderBatchType::CustomDrawer:`（`:1740`）直接走 `ICustomSlateElement::Draw_RenderThread` 自绘，**不经过 op 链表、不 ++NumOpsCreated**；`PostProcess` 同理走 `AddSlatePostProcessBlurPass`
- 它们的 draw 落在 `RDG_GPU_STAT_SCOPE(GraphBuilder, SlateUI)`（`SlateRHIRenderer.cpp:675`）作用域内 ⇒ 计入 `stat drawcount` 的 `Slate UI` 行
- ⇒ 该行**可能高于 CVar 上限**，是口径差不是 bug

### 结论 4：Slate 的 DC 读数天然跟随截断

`stat drawcount` 的 `Slate UI` 是 **RHI 口径**（`FRHIDrawStats::AddDraw` 统计实际发出的 draw，`RHIStats.h:179`），发生在截断之后 ⇒ **天然反映截断效果**，无需修复。其偏差只来自上面 (b) 的旁路。

### 一句话总结

| 问题 | `BasePassMaxDC` | `SlateUIMaxDC` |
|---|---|---|
| 统计早于截断 → 读数偏高 | **有**（2026-09-21 已修） | **无**（计数即截断判据本身） |
| 能否被 `r.MeshDrawCommands.Stats` 看到 | 能 | **不能**（Slate 不在 MDC 统计内） |
| CVar 上限语义 | per pass / per view | per `AddSlateDrawElementsPass` 调用（移动端 == per window） |
| 读数出口 | Stats 面板 + `stat rhi` | 仅 `stat drawcount`（RHI 口径，天然跟随） |
| 三角形是否进 `stat rhi` | 是 | **否**（`GNumPrimitivesDirectDrawnRHI` 无写入点） |

