# PC-Deferred-BasePass-ShaderPrint-诊断buffer探针与十字准星-完整实现.md

> PC Desktop Deferred 渲染管线上，用 ShaderPrint 诊断 buffer 模式在 base pass mesh material shader 内直接打印中间值（含最终 OutColor），并支持 CPU readback（`[ZXB-RDBK]` 日志）给无多模态能力的大模型取数。配套屏幕中心十字准星辅助瞄准中心像素。

---

## 一、问题定位流程

### 1.1 需求背景
- 在 **PC Desktop Deferred 管线**（S1/GR UE5.5，D:\GR_DevTest，Perforce）的 base pass mesh material shader（`BasePassPixelShader.usf` 的 PS）内**直接打印计算中间值**，用于调试效果
- 支持 CPU readback 给无多模态能力的模型取数
- PC 保持 Deferred，不切管线；不允许 shader 编译问题

### 1.2 阻塞点（常规模式在 mesh material shader 用不了 ShaderPrint）
1. mesh draw 是**异步提交**的（`TBasePassPS::GetShaderBindings` 无 GraphBuilder/View），拿不到当帧 RDG buffer
2. 引擎**从不给 mesh material shader 自动绑 ShaderPrint 全局 buffer** → `VerifyBindingsAreComplete` 必弹 unbound
3. 引擎所有 ShaderPrint 用法都是 **CS 或全屏 pass**，无 mesh material PS 先例

### 1.3 确认出路
`SHADER_PRINT_USE_DIAGNOSTIC_BUFFER` 模式——引擎预留的"任意 shader 无需显式绑定"方案：
- `UEDiagnosticBuffer` 通过 **root signature 自动绑定**（`BindDiagnosticBuffer`，D3D12CommandContext.cpp:147），每帧对所有 graphics/compute draw（含 mesh material）自动绑
- `UploadCS`（ShaderPrintDraw.usf）帧首把配置复制进诊断 buffer
- PC SM6 支持：`PLATFORM_SUPPORTS_DIAGNOSTIC_BUFFER = bSM6Features`

## 二、根因分析

### 2.1 转视角后 ShaderPrint 消失
**根因**：深度擂台是**单槽永久 `InterlockedMax` 累积**，只增不减。转视角后中心像素对准的物体变远（反向 Z 值变小），新深度永远打不过历史累积的"最近深度" → 准入 `depth >= arena*0.999` 恒失败 → 探针被跳过 → ShaderPrint 消失。

**修复**：**双槽轮转**（槽A/槽B 交替）。

### 2.2 F11 全屏后准星偏移
**根因**：准星 `AddLineSS` 内部用 `Config.Resolution`（= `Setup.ViewRect`，`BeginViews` 帧首冻结）归一化像素坐标，但准星中心之前用 `ResolvedView.ViewSizeAndInvSize`（当帧 ViewRect 尺寸）。**F11 切换帧**：`Setup.ViewRect` 冻结的旧值 ≠ 当帧 `ViewSizeAndInvSize` → 归一化基准与中心基准不同步 → 偏移。

诊断确认：稳定帧下 `ViewRect == UnscaledViewRect == UnconstrainedViewRect == Setup.ViewRect == Output.Extent`（完全一致），切换帧短暂不同步。

**修复**：准星中心改用 `Config.Resolution`（与 AddLineSS 归一化**同一基准**）。

### 2.3 code-review 4 个发现
| # | 问题 | 根因 |
|---|---|---|
| CR#1 | 擂台槽位与 lane 63 断言区冲突 | 槽A=383/槽B=382/相位=381 落在 lane 63 payload（378-383），引擎 shader `Assert`/`verify` 的 wave lane 63 写 `UEDiagnosticBuffer[383]`，与探针 InterlockedMax 竞争 |
| CR#2 | 探针与准星中心基准分叉 | 探针判定 `ViewSizeAndInvSize/2`，准星中心 `Config.Resolution/2`，两个不同字段 |
| CR#3 | 首帧 phase 未初始化 | 诊断 buffer 持久映射，首帧 UploadCS 前 phase 未定义，垃圾值下溢 |
| CR#4 | LINE 槽耗尽静默缺失 | AddLineSS 依赖 MaxLine=32，耗尽时静默不画 |

## 三、详细技术原理

### 3.1 诊断 buffer 布局（D3D12）
```
GPU resource = SizeInBytes(1544) + Extra(4096) = 5640 bytes = 1410 uint
```
- **lane 区**：0-383（64 lane × 6 uint），lane 63 payload = 378-383（引擎 shader Assert/verify 写面包屑）
- **MarkerIn/Out**：384-385（WITH_RHI_BREADCRUMBS）
- **配置区**：386-397（`SHADER_PRINT_DIAGNOSTIC_BUFFER_PARAMETERS_OFFSET = 6*64+2`，`PackShaderPrintConfig` 写）
- **条目区**：398 起（`SHADER_PRINT_DIAGNOSTIC_BUFFER_ENTRY_OFFSET = 386+12`）
- **readback 布局**：`Data[386+7]` = MaxCharacterCount；`Data[398+2]` = SYMBOL counter；`Data[402+4i]` = 每条目 4 uint

### 3.2 双槽深度擂台（转视角恢复的核心）
```hlsl
// ShaderPrint.ush —— 槽位定义（GPU resource 高位 extra 区，避开 lane 63 断言区）
#define ZXB_DEPTH_ARENA_OFFSET_A 1408u   // 槽A
#define ZXB_DEPTH_ARENA_OFFSET_B 1407u   // 槽B
#define ZXB_ARENA_PHASE_OFFSET   1406u   // 帧相位 (0/1 交替)

// ShaderPrintDraw.usf UploadCS —— 帧首：clamp phase + 翻转相位 + 清本帧写槽
uint ZXB_ArenaPhase = min(UEDiagnosticBuffer[ZXB_ARENA_PHASE_OFFSET], 1u); // CR#3 首帧 clamp
ZXB_ArenaPhase = 1u - ZXB_ArenaPhase;
UEDiagnosticBuffer[ZXB_ARENA_PHASE_OFFSET] = ZXB_ArenaPhase;
UEDiagnosticBuffer[ZXB_ArenaPhase != 0u ? ZXB_DEPTH_ARENA_OFFSET_A : ZXB_DEPTH_ARENA_OFFSET_B] = 0u;

// BasePassPixelShader.usf —— base pass：写 1-相位槽（CLAIM），准入读相位槽（VISIBLE）
uint ZXB_WriteSlot = (ZXB_ArenaPhase != 0u) ? ZXB_DEPTH_ARENA_OFFSET_A : ZXB_DEPTH_ARENA_OFFSET_B;
uint ZXB_ReadSlot  = (ZXB_ArenaPhase != 0u) ? ZXB_DEPTH_ARENA_OFFSET_B : ZXB_DEPTH_ARENA_OFFSET_A;
if (bIsCenterPixel) { InterlockedMax(UEDiagnosticBuffer[ZXB_WriteSlot], asuint(In.SvPosition.z), Prev); }
if (bIsCenterPixel && In.SvPosition.z >= asfloat(UEDiagnosticBuffer[ZXB_ReadSlot]) * 0.999f) { /* 打印 */ }
```
**机制**：每帧 UploadCS 清本帧写槽（让 CLAIM 从零重建）+ 翻转相位。base pass 写 1-相位槽、准入读相位槽（= 上帧该槽最近深度）。
- **转视角 2 帧恢复**：第 1 帧准入失败但 CLAIM 重建新槽，第 2 帧相位翻转读新槽成功
- **静止机位**：每帧读上帧最近深度 → 收敛单组（Symbols=40，OverDraw 不重叠）

### 3.3 十字准星（AddLineSS）
```hlsl
// BasePassPixelShader.usf —— 中心基准统一（CR#2）
FShaderPrintContext CenterCtx = InitShaderPrintContext(true, uint2(20, 30)); // 取 Config.Resolution
const float2 ZXB_CenterPx = float2(CenterCtx.Config.Resolution) / 2.0f;
uint2 PixelCoord = uint2(In.SvPosition.xy - ResolvedView.ViewRectMin.xy);
const bool bIsCenterPixel = all(PixelCoord == uint2(ZXB_CenterPx));

// 准星 4 条线段（绿色）
const float ZXB_CrossGap = 8.0f;   // 中心空隙
const float ZXB_CrossArm = 24.0f;  // 臂长
const float4 ZXB_CrossColor = float4(0,1,0,1); // 纯绿
AddLineSS(CenterCtx, float2(ZXB_CenterPx.x - ZXB_CrossArm, ZXB_CenterPx.y), float2(ZXB_CenterPx.x - ZXB_CrossGap, ZXB_CenterPx.y), ZXB_CrossColor);
AddLineSS(CenterCtx, float2(ZXB_CenterPx.x + ZXB_CrossGap, ZXB_CenterPx.y), float2(ZXB_CenterPx.x + ZXB_CrossArm, ZXB_CenterPx.y), ZXB_CrossColor);
AddLineSS(CenterCtx, float2(ZXB_CenterPx.x, ZXB_CenterPx.y - ZXB_CrossArm), float2(ZXB_CenterPx.x, ZXB_CenterPx.y - ZXB_CrossGap), ZXB_CrossColor);
AddLineSS(CenterCtx, float2(ZXB_CenterPx.x, ZXB_CenterPx.y + ZXB_CrossGap), float2(ZXB_CenterPx.x, ZXB_CenterPx.y + ZXB_CrossArm), ZXB_CrossColor);
```
**关键**：`AddLineSS` 走诊断 buffer **LINE counter**（`SHADER_PRINT_COUNTER_OFFSET_LINE`），默认 `r.ShaderPrint.MaxLine=32` 非零 → 与 `Print` 同源，**零 C++ 改动**（复用 CopyCS→DrawSymbols 链路）。准星是指示器，**不依赖深度准入**（每帧都画），只由 `bIsCenterPixel` 门控。

### 3.4 CPU readback（[ZXB-RDBK]）
D3D12RHI `ProcessInterruptQueue`（interrupt 线程，GPU 完成确认 `CompletedFenceValue >= CompletionFenceValue` 后）读诊断 buffer：
```cpp
const uint32* DiagData = reinterpret_cast<const uint32*>(CurrentQueue.DiagnosticBuffer->Data);
const uint32 MaxCharacters = DiagData[386 + 7];
const uint32 SymbolCounter = DiagData[398 + 2];
const uint32 SymbolCount = FMath::Min(FMath::Min(SymbolCounter, MaxCharacters), 128u); // 钳制防越界
// 内容指纹去重：counter + 全部 value 原始位 hash，变化才打印
UE_LOG(LogD3D12RHI, Log, TEXT("[ZXB-RDBK] Symbols=%u MaxChars=%u: %s"), ...);
```
**关键**：readback 只读 SYMBOL 区，十字准星（LINE）是纯屏幕显示，不进 readback。

## 四、修复方案

### 4.1 文件清单（6 个文件改动）
| 文件 | 类型 | 改动 |
|---|---|---|
| `UE5EA/Engine/Shaders/Private/ShaderPrint.ush` | 修改 | `SHADER_PRINT_EXPERIMENTAL_DIAGNOSTIC_BUFFER` 0→1 + 双槽擂台定义（1406/1407/1408） |
| `UE5EA/Engine/Shaders/Private/BasePassPixelShader.usf` | 修改 | `#include "ShaderPrint.ush"` + 中心像素探针块（双槽擂台）+ 十字准星 |
| `UE5EA/Engine/Shaders/Private/ShaderPrintDraw.usf` | 修改 | UploadCS clamp phase + 翻转相位 + 清本帧写槽 |
| `UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12Device.cpp` | 修改 | `CVarD3D12ExtraDiagnosticBufferMemory` 默认 0→4096（修越界） |
| `UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12Submission.cpp` | 修改 | readback 块（`[ZXB-RDBK]` 日志 + SymbolCount 钳制） |
| `UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12Util.cpp` | 修改 | `SetBoundShaderStateFlags` 无条件诊断 slot |

### 4.2 4 个 code-review 修复
| # | 修复 |
|---|---|
| CR#1 | 槽位移到 GPU resource 高位 extra 区 `1406/1407/1408`（远离 lane 63 断言区） |
| CR#2 | 探针中心判定改用 `Config.Resolution`（与 AddLineSS 归一化同基准），块顶部算一次共享 `ZXB_CenterPx` |
| CR#3 | UploadCS 里 `min(phase, 1u)` clamp |
| CR#4 | 接受不处理（准星 4 LINE 槽，MaxLine=32 充足；静默失败是 ShaderPrint 既有行为） |

## 五、快速排查 Checklist

### 启用前置（每次重启编辑器后必设）
```
r.ShaderPrint 1
ShowFlag.ShaderPrint 1        ← EngineShowFlags 门控（bEnabled 第三条件）
r.ShaderPrint.DebugReadback 1 ← readback [ZXB-RDBK] 日志开关
r.ShaderPrint.FontSize 32     ← 屏幕可读
```

### 测试基准
- **地图**：`/Game/Delete/Yoohaozhang/DT_ShowCase/DT_Showcase`
- **相机**：location `(195.63, 13.71, 82.56)`，rotation `(-1, 242.8, 0)`
- **基准值**：`R≈0.3719 G≈0.2188 B≈0.1586`

### 验证要点
- **readback**：`[ZXB-RDBK] Symbols=40: === BasePass OutColor ===R = ... G = ... B = ...`
- **屏幕**：左上角探针文本 + 屏幕中心绿色十字准星
- **OverDraw**：中心像素只出一组数据（Symbols=40）
- **转视角**：探针 2 帧内恢复，不消失

### 自测结果（2026-08-09）
- 基准 R=0.371875 ✓ | 转 300° 出数未消失 ✓ | 转回几帧内恢复 ✓
- 准星完整十字居中（水平臂 y=224 x[499-524]，垂直臂 x=512 y[210-236]）✓
- 零 shader 编译错误，无崩溃 ✓

### 分析陷阱
- **准星绿色阈值用宽松版**（`G>R && G>B && G>60`），严格版（`G>R+80`）会漏判横线（抗锯齿/blend 导致绿色不绝对主导）
- **诊断 buffer GPU resource 大小**：`SizeInBytes(1544) + Extra(4096)` = 1410 uint，槽位需在此范围内

## 六、相关参考

- 引擎代码：`UE5EA/Engine/Source/Runtime/RHICore/Public/RHIDiagnosticBuffer.h`（FQueue 布局）
- 引擎代码：`UE5EA/Engine/Shaders/Private/D3DCommon.ush`（UEDiagnosticBuffer 声明，UEDiagnosticMaxLanes=64）
- 引擎代码：`UE5EA/Engine/Shaders/Private/ShaderPrintDraw.usf`（UploadCS/CopyCS）
- 引擎代码：`UE5EA/Engine/Source/Runtime/Renderer/Private/ShaderPrint.cpp`（BeginViews/DrawView）
- 引擎代码：`UE5EA/Engine/Source/Runtime/D3D12RHI/Private/D3D12Submission.cpp`（ProcessInterruptQueue readback）
- Mobile 参照：`D:/GR_DevTest/debug-patches/MobileBasePassDebugSlots.ush`（深度擂台单槽方案）
- 测试基准记忆：`C:\Users\djangozhang\.tclaude\projects\D--GR-DevTest\memory\pc-basepass-shaderprint-test-baseline.md`
