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
