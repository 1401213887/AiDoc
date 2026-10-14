# UE-Mobile-Toon描边-PreOutline深度偏移污染-MSAA角色涂黑与BasePass剔除修复

> Android Vulkan 4x MSAA 下 toon 角色内部出现黑斑、后续 BasePass draw 被深度剔除；根因是 `Mobile_PreOutline_Pass` 把一个朝相机 0.001（NDC）的 depth bias **写进了** `Depth.Target`，污染了描边 Laplacian 与 BasePass 的深度消费。PC（D3D12）不复现，因为 read-only DSV 会强制忽略 PSO 的深度写；Vulkan 下这是未定义行为，Adreno 真的写入。

---

## 一、问题定位流程

### 现象

| 平台 | 配置 | 表现 |
|---|---|---|
| Android Vulkan Preview | `r.Mobile.AntiAliasing 3`（MSAA） | toon 角色内部大面积涂黑；RenderDoc 中后续 BasePass draw（如 event 1597）被深度剔除 |
| Android Vulkan Preview | `r.Mobile.AntiAliasing 1`（FXAA） | 正常 |
| PC Preview（D3D12） | 同一份代码 | **正常，不复现** |

### 确认的事实（RenderDoc + 源码交叉验证）

1. **PreOutline 的 Depth State**（RenderDoc，修复前截帧）：
   ```
   Enabled: ✓        Func: Greater Equal        Write: Read-Only DSV
   ```
   → attachment 层确实是 read-only DSV（`DepthBinding = DepthRead_StencilRead` 生效）

2. **mesh PSO 层的 depth write 却是 `true`** —— `MobileOutlinePrepearPass.cpp` 三处硬编码
   `SetDepthStencilAccess(FExclusiveDepthStencil::DepthWrite_StencilWrite)`，**从未跟随 DepthBinding 变化**

3. **UE 是 reverse-Z**（`RHIDefinitions.h:234-238`）：
   ```cpp
   enum class ERHIZBuffer { FarPlane = 0, NearPlane = 1,
       IsInverted = (int32)((int32)ERHIZBuffer::FarPlane < (int32)ERHIZBuffer::NearPlane) };
   ```
   → `CF_DepthNearOrEqual` = `CF_GreaterEqual`（`RHIDefinitions.h:352`），**z 越大越近**

4. **shader 里有两处 depth 偏移**，净效果是"恒定朝相机 0.001"：
   ```hlsl
   // MobilePreOutline.usf:128（外扩路径）
   Output.Position.z -= 1e-3;                                   // clip 空间 → NDC -0.001/w（随距离衰减）
   // MobilePreOutline.usf:139
   Output.Position.z += IsAAMMSAA ? GDepthBias * Output.Position.w : 0.0;   // NDC 恒定 +0.001
   ```
   净值：`z_ndc = z_orig - 0.001/w + 0.001` —— 远处趋近 **+0.001（更近）**

5. **描边 Pass 2 采的是 1x `Depth.Resolve`**（`MobileOutlinePrepearPass.cpp:717`），而 PreOutline 写的是 4x `Depth.Target` —— MSAA 下两者分裂

---

## 二、根因分析

### 完整因果链

```
usf:128 + usf:139 → PreOutline 顶点 z 净偏移 NDC +0.001（reverse-Z：朝相机）
        ↓
mesh PSO depthWrites = true（硬编码，与 attachment 的 DepthRead 声明矛盾）
        ↓
【平台分叉】
  D3D12  : read-only DSV 下 PSO 写被驱动强制忽略 → 深度干净 → PC 不复现
  Vulkan : read-only layout + PSO write = invalid usage（未定义行为）
           → Adreno 真的执行写入 → Depth.Target 被"偏近 0.001"污染
        ↓
污染后果 A：MobileToonOutline.usf 的 CalcDepthLaplacian 采到角色内部的人为深度台阶
           → ToonOutlineMask 在角色内部为 1 → BasePass 按 mask 涂描边色 → 涂黑
污染后果 B：偏近的深度让后续 BasePass 的 CF_DepthNearOrEqual 测试失败 → draw 被剔除
```

### 两个"Write"是不同层级（这是最容易混淆的点）

| | Mesh 层（PSO） | Pass 层（DepthBinding / attachment） |
|---|---|---|
| 代码 | `TStaticDepthStencilState<true, ...>` 第 1 参 `bEnableDepthWrite` + `SetDepthStencilAccess` | `FDepthStencilBinding(..., DepthRead_StencilRead / DepthWrite_StencilRead)` |
| 粒度 | 单个 draw | 整个 render pass |
| 管什么 | 像素级写掩码（GPU 执行） | 资源级访问权限（D3D12 建 read-only DSV / Vulkan 选 read-only layout） |
| 修复前 | **true**（要写） | **false**（声明只读）← 矛盾 |

`SetDepthStencilAccess` **只存 access 值，不覆盖 static state 的 depth write 位**（`MeshPassProcessor.h:2473-2476`），所以两层必须各自显式对齐。

### 为什么 bias 只在 MSAA 下加

`bPreOutlineDepthRead`（后重命名 `IsAAMMSAA`）= `AntiAliasingMethod == AAM_MSAA`，它是 **DepthBinding 状态的标志**，不是"MSAA 采样数"：

- MSAA 下深度走 `Target`(4x) / `Resolve`(1x 快照) 分裂 → PreOutline 写深度会让 Laplacian 采到不一致数据 → **被迫声明 DepthRead**
- DepthRead 模式下 PreOutline 只读 prepass 深度做比较，同网格浮点误差落在 `GreaterEqual` 的 equal 边界 → z-fight → **需要 bias 稳定通过**
- 非 MSAA（1x）深度无分裂，PreOutline 正常 DepthWrite 写自己的深度 → 不存在"读别人深度比较" → **不需要 bias**

⚠️ 注意：`AAM_MSAA` ≠ `NumSceneColorMSAASamples > 1`。`r.MSAACount=1` 时 AA method 仍是 `AAM_MSAA`（DepthRead 成立）但采样数为 1，这个边界差异正是把 shader 门控从 `NumSceneColorMSAASamples > 1` 改为复用 C++ 侧标志的原因。

### bias 的方向本身是对的

reverse-Z 下 `+0.001` = 更近，与 usf 原注释 "Move all geometry a little bit towards the camera" 一致，也确实让 `GreaterEqual` 更容易通过。**问题不是方向反了，而是这个 bias 的设计前提是"DepthRead 不落盘"，而 Vulkan 下它真的落盘了。**

---

## 三、详细技术原理：这套描边的两阶段实现

理解修复的前提是知道深度在这套描边里被谁消费。

### Pass 1：`Mobile_PreOutline_Pass`（Mesh Pass，逐角色）

**职责**：把 toon 角色"盖章"到屏幕空间纹理，并沿屏幕法线外扩预留描边空间。

- **VS 做 NDC 空间外扩**（`MobilePreOutline.usf:102-124`）：
  ```hlsl
  float3 ClipSpaceNormal = normalize(mul(WorldNormal, (float3x3)View.TranslatedWorldToClip));
  float3 NDCNormal = float3(normalize(ClipSpaceNormal.xy), 1) * ScreenPos.w;
  NDCNormal *= float3(View.ViewSizeAndInvSize.zw, 1);
  NDCPositionOffset += NDCNormal * OutlineWidth * OutlineAplha;
  ScreenPos.xy += NDCPositionOffset.xy;      // ← 只改 xy，z 不动
  ```
  **关键**：外扩只位移 `xy`，`z` 的计算路径与 prepass 完全相同 → 去掉 bias 后 z 与 prepass 逐位一致
- **输出 RT0** → `MobileOutlineTexture`：`float4(BaseColor, 1.0)`；眉毛走 `min(0.99, MaterialOpacity)` 作标记位
- **深度**：`ELoad` 复用 prepass 深度 + `CF_DepthNearOrEqual` 测试 → 保证外扩部分只画在角色可见处

### Pass 2：`RenderMobileOutlineTexturePass`（全屏 PS，`MobileToonOutline.usf`）

**职责**：屏幕空间深度 Laplacian 算真正的边缘，输出 4 通道 mask。

```hlsl
// 十字四采样 Laplacian（MobileToonOutline.usf:48-56）
return -(4.0 * CenterDepth - (Depth_L + Depth_R + Depth_U + Depth_D));

// 4 通道 mask 输出（:126）
OutColor = float4(SceneRimLightMask, SceneOutlineMask, ToonRimLightMask, ToonOutlineMask);
```

`USE_LAPALACIAN_OUTLINE` 由 `GMobileToonOutlineUsePreOutline`（默认 **2**）决定为 true → **描边完全靠深度 Laplacian 算，不看 Pass 1 的 RGB**。这解释了为什么深度被污染就直接涂黑。

### 分工总结

| | Pass 1 (PreOutline) | Pass 2 (ToonOutline) |
|---|---|---|
| 类型 | Mesh pass（逐角色 draw） | 全屏 PS |
| 干什么 | 标记"哪里有 toon 角色" + 外扩预留 + 眉毛标记 | 深度 Laplacian 算"哪里是边缘" + 合成 mask |
| 对深度 | **测试 + 写入** | **采样**（Laplacian 数据源） |
| 产出 | `MobileOutlineTexture`（RGB=BaseColor, A=眉毛标记） | `MobileCharFeatureTexture`（RGBA=4 种 mask） |

---

## 四、修复方案

**核心决策**：PreOutline 的深度**必须写**（后续 pass 消费），所以不能靠关掉写来解决 —— 要修的是**写进去的值**。

### 改动 1：`MobilePreOutline.usf` —— 移除全部 depth bias

```hlsl
// 删除 usf:14
static const float GDepthBias = 0.001f;

// 删除 usf:16-17
// [ZXB] Whether PreOutline DepthBinding is DepthRead ...
uint IsAAMMSAA;

// 删除 usf:127-129（外扩路径的 clip 空间偏移）
#if HAVE_GetMobileToonCharacterParameters1
    Output.Position.z -= 1e-3;
#endif

// 删除 usf:139（MSAA 门控的 NDC 恒定偏移）
Output.Position.z += IsAAMMSAA ? GDepthBias * Output.Position.w : 0.0;
```

替换为 `Output.Position = ScreenPos;` 后的一条说明注释：

```hlsl
			Output.Position = ScreenPos;
#pragma region Engine ZXB
			// [ZXB] No depth bias: PreOutline writes depth consumed by the outline Laplacian and the
			// BasePass test. A camera-ward bias created fake steps inside the character, so the
			// Laplacian read interior pixels as edges. Expansion shifts xy only, so z equals the
			// prepass and NearOrEqual passes on equal.
#pragma endregion
```

### 改动 2：`MobileOutlinePrepearPass.cpp` —— attachment 与 PSO 统一 DepthWrite

```cpp
#pragma region Engine ZXB
				// [ZXB] PreOutline 必须写深度（后续 pass 消费），attachment 与 mesh PSO 统一 DepthWrite：
				// Vulkan 下 read-only layout + PSO depth write 是未定义行为，Adreno 会真写入。
				FDepthStencilBinding DepthBinding = FDepthStencilBinding(SceneTextures.Depth.Target,
					ERenderTargetLoadAction::ELoad, ERenderTargetLoadAction::ELoad,
					FExclusiveDepthStencil::DepthWrite_StencilRead);
#pragma endregion
```

同时移除已成死代码的整条链：
- `bool GMobileAAMMSAA`（全局变量）
- `ShaderBindings.Add(IsAAMMSAAParam, ...)`
- `IsAAMMSAAParam.Bind(Initializer.ParameterMap, TEXT("IsAAMMSAA"))`
- `LAYOUT_FIELD(FShaderParameter, IsAAMMSAAParam)`

mesh 层三处 `DepthWrite_StencilWrite`（`CreatePreOutlinePassProcessor` 默认 state + `AddMeshBatch` 的 SM5/移动端两个分支）**保持原样不动** —— 现在与 attachment 一致。

### 修复后行为对比

| | 修复前（MSAA） | 修复后 |
|---|---|---|
| attachment 声明 | DepthRead（与 PSO 矛盾） | **DepthWrite** |
| mesh PSO | DepthWrite | DepthWrite（一致，无未定义行为） |
| 写入的深度值 | `z + 0.001`（朝相机偏） | **`z` 原值（= prepass）** |
| Pass 2 Laplacian | 角色内部有假台阶 → 涂黑 | 内部平滑 → 正常 |
| BasePass 深度测试 | 被偏近深度挡（1597 剔除） | equal 通过 |
| 深度是否写入 | 是（意外） | **是（明确设计）** |

### 残留风险与回退方案

PreOutline VS 与 prepass VS 是两个 shader，若编译器指令重排导致 z 有 ~1e-7 级差异，理论上可能在 `GreaterEqual` 的 equal 边界产生 z-fight 闪烁。真出现时的回退是加**极小** bias（`1e-6 * w`，比原值小 3 个数量级，足够压浮点噪声又不会造出 Laplacian 台阶），而不是恢复 `0.001`。

---

## 五、快速排查 Checklist

排查"PreOutline 深度相关"的渲染异常时按此顺序：

| # | 检查项 | 怎么查 | 命中特征 |
|---|---|---|---|
| 1 | **PC 复现吗** | 同配置切 PC Preview | PC 正常 + Mobile 异常 → 高度怀疑 attachment/PSO 深度声明矛盾（D3D12 兜住、Vulkan 捅破） |
| 2 | **attachment 与 PSO 的 depth write 是否一致** | RenderDoc 看 `Write: Read-Only DSV` vs 源码 `SetDepthStencilAccess` / `TStaticDepthStencilState` 第 1 参 | 一边 read-only 一边 write = Vulkan 未定义行为 |
| 3 | **shader 里有几处改 `Output.Position.z`** | grep `Position.z` | 多处偏移会互相抵消/叠加，必须合起来算净值 |
| 4 | **净偏移的方向与量级** | reverse-Z 下 `+` = 更近；`±k*w` 是 NDC 恒定，`±k` 是随距离衰减 | NDC 恒定 0.001 足以让 Laplacian 产生假边缘 |
| 5 | **描边 Pass 采的是 Target 还是 Resolve** | 看 `PassParameters->SceneDepthTex` 赋值 | MSAA 下 Target(4x)/Resolve(1x) 分裂，写 Target 采 Resolve 必不一致 |
| 6 | **`USE_LAPALACIAN_OUTLINE` 是否开** | `GMobileToonOutlineUsePreOutline`（默认 2 → 开） | 开 = 描边靠深度算，深度污染直接涂黑 |
| 7 | **改动是否进了二进制** | `stat -c '%y'` 比 `UnrealEditor-Renderer.dll` vs 源码 mtime | DLL 早于源码 = 没重编（`strings` 判断不了，UE 日志是 UTF-16） |

### 关键判据速查

```cpp
// reverse-Z 确认（RHIDefinitions.h:234-238, 352）
FarPlane = 0, NearPlane = 1  →  IsInverted = true
CF_DepthNearOrEqual = CF_GreaterEqual   // z 大 = 近 = 通过

// SetDepthStencilAccess 不覆盖 static state 的 write 位（MeshPassProcessor.h:2473-2476）
void SetDepthStencilAccess(FExclusiveDepthStencil::Type In) { DepthStencilAccess = In; }

// static state 第 1 参才是 depth write 开关（RHIStaticStates.h:197-213）
template<bool bEnableDepthWrite = true, ECompareFunction DepthTest = CF_DepthNearOrEqual, ...>
class TStaticDepthStencilState
```

### MSAA 黑斑复现基线

```
相机：location (295.16, 162.39, 126.92)  rotation (9.2, 270.2, 0)
r.Mobile.AntiAliasing 1  → FXAA，正常
r.Mobile.AntiAliasing 3  → MSAA，复现
```
⚠️ AA method 3↔1 切换有状态污染，切换后需**干净重启**验证。

---

## 六、相关参考

### 涉及的源码位置

| 文件 | 关键行 | 内容 |
|---|---|---|
| `UE5EA/Engine/Shaders/Private/MobilePreOutline.usf` | 102-124 | NDC 空间外扩（只改 xy） |
| | 123 | `Output.Position = ScreenPos`（bias 移除处） |
| `UE5EA/Engine/Shaders/Private/MobileToonOutline.usf` | 48-56 | `CalcDepthLaplacian` 十字四采样 |
| | 126 | 4 通道 mask 输出 |
| `UE5EA/.../Renderer/Private/MobileOutlinePrepearPass.cpp` | 871-875 | `DepthBinding`（统一 DepthWrite） |
| | 717 | Pass 2 采 `Depth.Resolve`（1x） |
| | 565-570 | `CreatePreOutlinePassProcessor` 默认 depth state |
| | 363-371 / 406-408 | `AddMeshBatch` SM5 / 移动端分支 depth state |
| | 694 | `USE_LAPALACIAN_OUTLINE` permutation 决策 |
| `UE5EA/.../RHI/Public/RHIDefinitions.h` | 229-239, 352-355 | reverse-Z 定义 + `CF_Depth*` 映射 |
| `UE5EA/.../RenderCore/Public/RHIStaticStates.h` | 197-254 | `TStaticDepthStencilState` 模板签名 |
| `UE5EA/.../Renderer/Public/MeshPassProcessor.h` | 2473-2481 | `SetDepthStencilAccess` 实现 |

### 相关规范文档

- Vulkan 规范：render pass attachment 声明 read-only 时 pipeline 不得开启 depth write（VUID 约束，行为未定义）
- D3D12 规范：`D3D12_DSV_FLAG_READ_ONLY_DEPTH` 下 PSO 的 depth write 被静默忽略

### 同仓相关记录

- `msaa-black-blob-preoutline-depthwrite` —— 早期同源问题（PreOutline 写 4x Depth.Target 但 outline 采 Depth.Resolve）
- `msaacount-one-preoutline-depthread` —— `r.MSAACount=1` 混合态
- `scene-depth-resolve-can-be-null` —— `Depth.Resolve` 可能为 null，需回退 `DepthAux.Resolve`
- `verify-dll-newer-than-source-not-strings` —— 判断改动进没进二进制只认 mtime
