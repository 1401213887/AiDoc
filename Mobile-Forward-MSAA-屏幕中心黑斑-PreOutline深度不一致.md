# Mobile-Forward-MSAA-屏幕中心黑斑-PreOutline深度不一致.md

> UE5 移动端 Forward 管线，切 MSAA（r.Mobile.AntiAliasing=3）后屏幕中心角色脸部出现黑斑；BasePass 输出正常肤色，最终 SceneColor 全黑，根因是 PreOutline mesh pass 用 DepthWrite 写 4x Depth.Target，而 outline pass 采样 Depth.Resolve（prepass 快照）不一致，深度 Laplacian 误判角色内部为描边边缘 → 整角色涂黑。修 DepthRead（条件化，仅 MSAA）。

---

## 一、问题定位流程

**现象**：Android Vulkan Preview + Mobile Forward + `r.Mobile.AntiAliasing=3`（MSAA）+ outline 开启 → 屏幕中心角色脸部黑斑（8bit ~(1,1,2) 全黑）。`r.Mobile.AntiAliasing=1`（FXAA）正常。

**复现前置**：
- 相机点位：location `(295.16, 162.39, 126.92)`，rotation `(9.20, 270.20, 0)`
- CVar：`r.Mobile.AntiAliasing 3`（MSAA 复现）/ `1`（FXAA 正常）；`r.Mobile.ShadingPath 0`（Forward）；`r.YHRP.EnableMobileOutlinePass 1`；`r.MobileOutline.ToonOutlineUsePreOutline 2`

**用 ShaderPrint 探针逐步确认（关键，证伪了"BasePass 输出是黑的"判断）**：

| 探测点 | 手段 | 结果 | 结论 |
|---|---|---|---|
| BasePass 中心 (976,531) | FWD OutColor 探针 | 肤色 0.32（MSAA/FXAA 两档相同）| **黑不在 BasePass** |
| 描边 mask @黑斑中心 | MobileCharacterOutline / ScreenOutlineTexture 探针 | 全 0 | 描边输入本身是 0 |
| 最终 SceneColor 中心 | Mode A 读 Color.Resolve | outline on+MSAA = 全黑 0；outline off = 肤色 0.508 | **黑在最终 SceneColor** |
| PreOutline 开关 | CVar 二分 `ToonOutlineUsePreOutline 0/2` | =0（clear）正常，=2（mesh）黑斑 | 锁定 **PreOutline pass** |

## 二、根因分析

**关键机制**（Mobile 描边链路）：
```
PreOutline mesh pass ──写──► MobileOutlineTexture（4x Target + 1x Resolve）
                                   │
outline pass（RenderMobileOutlineTexturePass）采样 PreOutlineResolve + SceneDepthTex
   计算 ToonOutlineMask（深度 Laplacian 边缘检测）──写──► MobileCharFeatureTexture（4x Target + 1x Resolve）
                                   │
BasePass 采样 MobileCharacterOutline.a 做描边：Color = lerp(Color, 0, ToonOutlineMask)
```

**根因**：`MobileOutlinePrepearPass.cpp` 的 PreOutline mesh pass 用 `DepthWrite_StencilRead` 写 **4x Depth.Target**。而 outline pass 的 `SceneDepthTex` 在 MSAA 下是 **`Depth.Resolve`**（prepass 深度快照，**不含** PreOutline mesh 写入的深度）→ 深度 Laplacian（`MobileToonOutline.usf` 的 `CalcDepthLaplacian`）基于不一致深度，把角色内部误判为描边边缘 → `ToonOutlineMask` 全 1 → 整角色被 `lerp(Color, 0, mask)` 涂黑。

**为什么 FXAA 正常**：非 MSAA 时 `Depth.Resolve = nullptr`，outline pass 的 `SceneDepthTex = Depth.Resolve ? Resolve : Target` **回退到 `Depth.Target`**——它与 PreOutline mesh 写的是同一个 1x 深度 → 一致 → 边缘检测正确。

## 三、详细技术原理

### `NumMSAASamples` 的判定链（为什么条件分支不影响 FXAA/TAA）
```
r.Mobile.AntiAliasing（Mobile 专属 CVar，非 r.AntiAliasingMethod）
  → GetDefaultAntiAliasingMethod(ES3.1) 只读它
       FXAA=1→AAM_FXAA / TAA=2→AAM_TemporalAA / MSAA=3→AAM_MSAA
  → GetDefaultMSAACount()
       仅 AAM_MSAA 分支返回 r.MSAACount（默认 4）
       FXAA/TAA 走 else → NumSamples = 1
```

| AA 设置 | `r.Mobile.AntiAliasing` | `GetDefaultAntiAliasingMethod` | `NumMSAASamples` | 条件分支 |
|---|---|---|---|---|
| FXAA (1) | 1 | `AAM_FXAA` | **1** | false → `DepthWrite`（原行为）|
| TAA (2) | 2 | `AAM_TemporalAA` | **1** | false → `DepthWrite`（原行为）|
| MSAA (3) | 3 | `AAM_MSAA` | **4** | true → `DepthRead`（修复）|

- TAA 与 MSAA 在移动端互斥（`r.Mobile.AntiAliasing` 二选一），MSAA 下无 TAA
- 编辑器设的 `r.AntiAliasingMethod=4`(TSR) 在 ES3.1 下**被 `r.Mobile.AntiAliasing` 覆盖**（`GetDefaultAntiAliasingMethod` 只读 Mobile CVar）——这佐证黑斑仅随 `r.Mobile.AntiAliasing` 1↔3 切换出现/消失

### 为什么非 MSAA 保留 DepthWrite 是必要的
非 MSAA 时 outline pass 采 `Depth.Target`（与 PreOutline 同一 1x 深度）→ 本来就一致。此时保留 `DepthWrite` 维持 PreOutline mesh 之间的深度遮挡（原引擎语义），对非 MSAA 零改动。

## 四、修复方案

**文件**：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileOutlinePrepearPass.cpp`（RenderPreOutlinePass，PreOutline mesh pass 的 DepthBinding）

**改动**：DepthWrite → DepthRead，**条件化，仅 MSAA 生效**：
```cpp
// [ZXB Fix] MSAA 下 PreOutline 深度改为只读(DepthRead_StencilRead):
// 原 DepthWrite 会让 PreOutline mesh 之间用"更新后的深度"互相测试(后画挡先画),
// 而 outline pass(MobileToonOutline.usf 深度 Laplacian)采样的是 prepass 深度快照
// (Depth.Resolve, 见 RenderMobileOutlineTexturePass::SceneDepthTex)。
// MSAA 时两者不一致 → 角色内部被误判为描边边缘 → 整角色涂黑。
// 改只读后 PreOutline 数据基于 prepass 深度生成, 与 outline pass 采样一致(对齐 FXAA 行为)。
// ⚠ 仅 MSAA 下 DepthRead: 非 MSAA 时 outline pass 采 Depth.Target(与 PreOutline 同一 1x 深度),
//   本就一致, 保留原 DepthWrite(PreOutline mesh 间深度遮挡)不改动非 MSAA 行为。
FDepthStencilBinding DepthBinding = FDepthStencilBinding(SceneTextures.Depth.Target, ERenderTargetLoadAction::ELoad, ERenderTargetLoadAction::ELoad,
    NumMSAASamples > 1 ? FExclusiveDepthStencil::DepthRead_StencilRead : FExclusiveDepthStencil::DepthWrite_StencilRead);
```

**配套**（之前 MSAA 适配已含，供上下文）：
- outline pass 输出 `FRenderTargetBinding(OutTargetTex, MobileCharFeatureTexture.Resolve, EClear)` —— 4x Target resolve 到 1x，供 BasePass 非 MS Texture2D 采样
- outline pass 采样 `SceneDepthTex = Depth.Resolve ? Resolve : Target`
- `UsePreOutline=0` 分支同时 clear 4x Target + 1x Resolve（避免脏 Resolve）

**对齐引擎原生（2026-08-14）**：
采样/绑定写法统一为引擎原生 MSAA 接入方式（功能等价，纯机械对齐）：
- **采样三目 → 直接 `.Resolve`**（5 处：MobileOutlinePrepearPass.cpp SceneDepthTex、MobileShadingRenderer.cpp×3 ScreenSpaceOutline、SingleLayerWaterRendering.cpp×1）。依据：`CreateTextureMSAA`（RenderGraphUtils.cpp:206-238）非 MSAA 时返回 `FRDGTextureMSAA(单纹理)`，**Resolve 恒非空且==Target**，引擎原生直接 `.Resolve`（PostProcessTonemap.cpp:1228/1274），从不写三目。
- **绑定 resolve → `IsSeparate() ? Resolve : nullptr`**（2 处：PreOutline 输出、outline pass 输出）。对齐引擎原生 Forward `bMobileMSAA ? Color.Resolve : nullptr`（MobileShadingRenderer.cpp:1964）；非 MSAA 时显式 nullptr，避免 self-resolve。

> 结论：本修复的 MSAA 接入（Target/Resolve 纹理对 + resolve 分离绑定 + `NumMSAASamples` 条件化 + DepthRead 原生类型）与引擎原生 MSAA 机制一致，仅采样/绑定写法原为防御式三目，现已对齐原生。

**⚠️ 对齐后回归修复（2026-08-14）：Depth.Resolve 可能为 null**
对齐时把 `SceneDepthTex` 的三目 `Resolve ? Resolve : Target` 简化成直接 `.Resolve`，**引入 MSAA 下角色概率不渲染**：
- 根因链：MSAA + `bRequiresSceneDepthAux`（`r.Mobile.SceneDepthAux=1` + `r.Mobile.TonemapSubpass=0` 时 Vulkan 下 true）→ `MobileShadingRenderer.cpp:761` 强制 `bKeepDepthContent=false` → `SceneTextures.cpp:587-590` **无 else 分支** → `Depth.Resolve` 保持 **null**。`bKeepDepthContent` 依赖 `bRequiresMultiPass`/`bHZBOcclusion`/`bSeparateTranslucencyActive`/`IsDumpingFrame()` 等帧间条件 → **概率性**。
- 修复（`MobileOutlinePrepearPass.cpp` outline pass）：`SceneDepthTex = Depth.Resolve ? Depth.Resolve : DepthAux.Resolve`。aux 深度由 full depth prepass 产生（`MobileShadingRenderer.cpp:1169 AddResolveSceneColorPass(DepthAux)`，在 outline pass 之前），1x 可采样，是 Mobile base pass 同款深度源（`SceneTextures.cpp:1688`）。两者互斥，必有其一。
- 验证：MSAA=3 + outline 全开 + AndroidVulkan_Preview，**连续 5 帧中心肤色 (169,145,120) 稳定**（修复前概率消失）；当前环境确认 `Depth.Resolve=null`（`SceneDepthAux=1`/`TonemapSubpass=0`），即验证的就是 aux 回退路径；FXAA=1 回归 (171,147,123) 正常。

> ⚠️ **教训**：对齐原生时，"功能等价"的判断必须验证**空指针/未 produce 场景**。`CreateTextureMSAA` 创建的自定义纹理（MobileCharFeatureTexture/MobileOutlineTexture）Resolve 恒非空，但 **SceneDepth 是手动创建逻辑（`SceneTextures.cpp:587-590` 无 else），Resolve 可能为 null**——三目回退在这里不是防御，是必需的。

**验证**（AndroidVulkan_Preview 真实 Mobile 平台）：
- **平台确认**：preview = `AndroidVulkan_Preview`；日志出现 `LogTemp: MobileBasePass: material ... has no shader for lightmap policy`（Mobile 渲染路径独有，PC 路径不出现）→ 确认在跑 Mobile 管线
- **MSAA 复现条件**：`r.Mobile.AntiAliasing 3` + `r.YHRP.EnableMobileOutlinePass 1` + `r.MobileOutline.ToonOutlineUsePreOutline 2` → 中心肤色 `(170,145,120)`，**无黑斑** ✓
- 修复前后对照（均 Mobile 平台）：修复前探针实测 finalSceneColor 中心 = 全黑 0；修复后中心肤色正常
- 无渲染错误日志；region 平衡（7/7、10/10）

> ⚠️ **验证教训**：编辑器默认 preview = `None`（PC 平台），此时 `r.Mobile.AntiAliasing=3` 的 MSAA 不会在 Mobile 路径生效——看到的全是 PC 渲染效果，**不能作为 Mobile 修复的有效验证**。必须切 `AndroidVulkan_Preview` 且用 Mobile 专属日志（如 `MobileBasePass`）确认路径，截图才算数。

## 五、快速排查 Checklist

1. **确认 Mobile 平台**：preview 必须是 `AndroidVulkan_Preview`（编辑器默认 `None`=PC，Mobile CVar 不生效）；用日志 `MobileBasePass` 关键行确认真在跑 Mobile 路径
2. **确认管线**：`r.Mobile.ShadingPath 0`（Forward）；`r.Mobile.AntiAliasing 3`（MSAA）
3. **探针定位黑在哪**：BasePass OutColor 探针 vs 最终 SceneColor（Mode A 读 Color.Resolve）——若 BasePass 正常而 SceneColor 黑，黑在 BasePass 之后的 pass
4. **CVar 二分**：`r.YHRP.EnableMobileOutlinePass 0/1`；`r.MobileOutline.ToonOutlineUsePreOutline 0/2` 逐步锁定 outline 链路
5. **深度一致性检查**：PreOutline mesh pass 的 DepthBinding（Write vs Read）是否与 outline pass 采样的 SceneDepthTex 来源一致
6. **MSAA 特有怀疑**：凡 MSAA 下才出现的描边/边缘异常，优先查 "4x Target 深度写入" 与 "1x Resolve 采样" 是否同步

## 六、相关参考

- 代码：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileOutlinePrepearPass.cpp`（RenderPreOutlinePass、RenderMobileOutlineTexturePass）
- 代码：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp`（NumMSAASamples 赋值、outline pass 采样绑定）
- Shader：`UE5EA/Engine/Shaders/Private/MobileToonOutline.usf`（深度 Laplacian / ToonOutlineMask 计算）
- Shader：`UE5EA/Engine/Shaders/Private/MobilePreOutline.usf`（PreOutline mesh 渲染）
- 引擎：`UE5EA/Engine/Source/Runtime/Engine/Private/SceneUtils.cpp`（GetDefaultAntiAliasingMethod / GetDefaultMSAACount）
- 排查工具：`UnrealShaderPrint调试` skill（探针取数、Mode A 读纹理、CVar 二分）
- 排查工具：`ZXB` skill（编辑器操作、截图、CVar 管理）

## ⚠️ r.MSAACount=1 兼容（2026-08-14 追加，重要）

**问题**：`r.MSAACount 1`（配合 `r.Mobile.AntiAliasing=3`，即"AAM_MSAA + 1 采样"混合态）时角色直接变黑/不画。CVar 注释写 "1: MSAA disabled"，但代码只对 `r.MSAACount <= 0` 降级为 AAM_None（SceneUtils.cpp:159），`=1` 保持 AAM_MSAA → 引擎按 MSAA 语义渲染但采样数=1。

**根因**：r.MSAACount=1 时 `Config.NumSamples=1`（GetDefaultMSAACount），但 AA method 仍是 AAM_MSAA。此时 PreOutline 原 DepthBinding 按 `NumMSAASamples > 1` 判断 → =1 走 **DepthWrite** → 外扩描边 mesh 的偏移深度（MobilePreOutline.usf VS `Output.Position.z -= 1e-3`）写入 Depth.Target，而 outline 深度 Laplacian 采同一纹理 → **角色内部深度差异被误判为边缘 → ToonOutlineMask 全 1 → 整角色涂黑**。

**修复**（MobileOutlinePrepearPass.cpp）：PreOutline DepthBinding 改为按 **AA method（AAM_MSAA）** 而非 NumMSAASamples 判断：
```cpp
// [ZXB Fix] PreOutline DepthBinding 按 AA method（AAM_MSAA）而非 NumMSAASamples 判断
const bool bPreOutlineDepthRead = (Views[ViewIndex].AntiAliasingMethod == AAM_MSAA);
FDepthStencilBinding DepthBinding = FDepthStencilBinding(SceneTextures.Depth.Target, ELoad, ELoad,
    bPreOutlineDepthRead ? FExclusiveDepthStencil::DepthRead_StencilRead : FExclusiveDepthStencil::DepthWrite_StencilRead);
```
- AAM_MSAA（含 r.MSAACount=1）→ **DepthRead**（基于 prepass 深度，与 outline 采样一致，对齐真 MSAA）
- FXAA → DepthWrite（保持原行为）
- outline 的 SceneDepthTex 统一 `Depth.Resolve ? Resolve : DepthAux.Resolve`（**不能采 Target**：非 MSAA 时 PreOutline DepthWrite 写的偏移深度会让深度 Laplacian 误判，FXAA 实测回归即此）

**验证**（AndroidVulkan_Preview 干净状态 + UsePreOutline=2 + outline 全开）：
| 组合 | 中心像素 | 结果 |
|---|---|---|
| MSAA=3 + MSAACount=1 | (233,237,244) | ✅ 正常（修复前 (2,2,3) 黑）|
| FXAA=1 + MSAACount=4 | (167,142,116) | ✅ 正常 |
| MSAA=3 + MSAACount=4 | (164,140,116) | ✅ 正常 |

**⚠️ 切换污染（独立问题，非本次修复引入）**：运行中 `r.Mobile.AntiAliasing` 3↔1 切换后渲染黑（shader/渲染状态未刷新，可能 AA method 相关 shader permutation 缓存），**切回原 AA 即恢复**；`r.MSAACount` 1↔4 切换（同 AA）不污染。旧 DLL 亦存在此现象。

**排查教训**：编辑器反复切换 CVar 会累积状态污染，**验证必须干净重启后单次设置**；"r.MSAACount=1 让 FXAA 也黑"曾被误判为真回归，实际是切换残留。

### 最终根因 + 方案 A（2026-08-14 RenderDoc 定位，重要）

**RenderDoc 关键发现**：r.MSAACount=1 时 **MobileRenderPrePass 没画角色、画了墙体** → BasePass 画角色被深度剔除 → 角色缺失（**不是描边问题**，是角色深度没写入）。

**完整根因链**：
1. `DepthPassCanOutputVelocity`（VelocityRendering.cpp:612 **原 `GetDefaultMSAACount(FeatureLevel) > 1` 按采样数判断 MSAA**）→ r.MSAACount=1 时采样数=1 → bMSAAEnabled=false → return=true
2. `EarlyZPassMode = DDM_AllOpaqueNoVelocity`（RendererScene.cpp:5150）
3. `ShouldDrawDepthPass`（DepthRendering.cpp:2417 `DDM_AllOpaqueNoVelocity` 分支）→ **角色（movable SkeletalMesh + velocity）在 depth pass 被跳过**（:2455 `bDraw=false`）
4. 角色深度本应靠 "subsequent velocity pass"（注释 :2416）→ 但 `ShouldRenderVelocities=false`（Mobile 非 TAA，VelocityRendering.cpp:275）→ **velocity pass 不跑 → 角色深度永久缺失**
5. BasePass 画角色 → 深度测试 vs Depth.Target（无角色深度）→ 剔除 → 角色不渲染

**修复（方案 A，VelocityRendering.cpp:612 一行）**：`bMSAAEnabled` 改用 **AA method** 判断（与 PreOutline DepthBinding 的 `bPreOutlineDepthRead` 判定对齐）：
```cpp
const bool bMSAAEnabled = (GetDefaultAntiAliasingMethod(FeatureLevel) == EAntiAliasingMethod::AAM_MSAA);
```
→ r.MSAACount=1 → bMSAAEnabled=true → DepthPassCanOutputVelocity=false → **DDM_AllOpaque** → 角色在 depth pass 正常写深度。

**验证（对比法，AndroidVulkan_Preview 沉浸 + 用户视角）**：
| 对比 | 差异 | 结论 |
|---|---|---|
| MSAACount=1 vs FXAA（都 1x 采样）| **1%** | 角色完整、描边正常 ✅ 修复生效 |
| FXAA vs MSAA=4 | 1% | FXAA 无回归 |
| MSAACount=1 vs MSAA=4 | 21% | 1x/4x 光栅化采样边缘差异（正常）|

**改动影响面**：仅 VelocityRendering.cpp 一行，行为变化限定 **Mobile + AAM_MSAA + MSAACount=1**（EarlyZPassMode NoVelocity→AllOpaque）；Deferred/PC/FXAA/TAA/真 MSAA/None/TSR 路径不变（已逐一核对）。

**⚠️ 自测教训**：肤色像素判定在当前视角（环境蓝白主导）不可靠，会误判"角色缺失"（实际角色正常）；**可靠验证是对比法**——同采样数状态（如 MSAACount=1 vs FXAA）画面差异应 <5%。
