# MobileRenderer-Forward-SingleMultiPass-实现原理与ZXB对齐修复总结

> UE 5.5.4 source fork 下 Mobile Renderer Forward 路径深度分析：`RenderForwardSinglePass`（1 Render Pass + subpass）vs `RenderForwardMultiPass`（2 独立 Render Pass）的实现与 `RequiresMultiPass` 分流决策树；以及 Forward/Deferred toon 对齐的 `[ZXB]` 修复体系（9 项落地）。本文件为执行摘要，完整分析见 `MobileShadingRenderer_RenderForward_SingleMultiPass.md`（715 行，同存于 `E:\AiDoc\` 根目录）。

---

## 一、任务定位与产出

- **目标**：深入阅读 `MobileShadingRenderer.cpp` 的 `RenderForwardMultiPass` / `RenderForwardSinglePass` 实现原理，分别分析 + 对比分析，产出含流程图的 .md 文档。
- **核心交付**：`E:\AiDoc\MobileShadingRenderer_RenderForward_SingleMultiPass.md`（715 行 / 70389 bytes，源在 `D:\GR_DevTest\docs\`，该目录不在 Perforce，本地文件直接编辑）。
- **校验强度**：94 轮持续校验（事实 / 行号 / 流程图 / 引用 / 格式 / 结构 / 元信息 / 记忆同步 / 待办），修正 3 处行号、1 处行为误述、版本元信息（Build.version Changelist **1077366**，CLAUDE.md 引用的 1011149 已过时）。
- **最大内容补全**：shader 层 Forward-Deferred 对齐修复体系（`MobileBasePassPixelShader.usf` 中 >15 处 `[ZXB Fix]`，整理为 9 行修复表），弥补文档 §9.2 最大缺口。

## 二、核心原理：SinglePass vs MultiPass

### 2.1 分流：`RequiresMultiPass` 决策树（`MobileShadingRenderer.cpp:3191-3241`）

`RenderForward` 据此在两种实现间二选一：

| 条件 | 结果 |
|---|---|
| Vulkan | **false**（SinglePass） |
| Metal + FrameBufferFetch | **false** |
| Android GL + FBF | **false** |
| MobileDeferred | **true**（MultiPass） |
| LDR（无 HDR） | **true** |
| MSAA > 1（AAM_MSAA） | **true** |

### 2.2 两种实现形态

- **RenderForwardSinglePass**：单 Render Pass，靠 **subpass** 完成 GBuffer → Lighting → Tonemap 的帧内流转。`bTonemapSubpassInline` 仅在 Vulkan 为 true。
- **RenderForwardMultiPass**：2 个独立 Render Pass（BasePass + 后处理），无 subpass 依赖，兼容无 FBF 的平台。

### 2.3 关键标志

- `bTonemapSubpassInline` — Vulkan only。
- `bRequiresSceneDepthAux`（`RenderCore/Private/RenderUtils.cpp:493-511` 判定树）：`!MobileUsesFullDepthPrepass` 才考虑 → Metal 恒 true → HDR 非 Deferred 的 Android GL/Vulkan。
- `bKeepDepthContent`（761 行反向例外）。
- `bModulatedShadowsInUse`。
- **深度目标永不 Memoryless**（`SceneTexturesConfig.cpp:320-333`，`//ericado` 注释）。

### 2.4 Tonemap 内联与后处理链（详完整文档 §2.2）

- 开启：`r.Mobile.TonemapSubpass 1`（`ECVF_Scalability` 非 cheat）；前提 HDR 且非 Deferred。
- `bTonemapSubpass=true` → `AddMobileCustomResolvePass` 替代 `AddMobilePostProcessingPasses`——**tonemap 前后整条后处理链（Bloom/自动曝光/FXAA/tonemap 后材质）全跳过**，只留 tonemap + ColorGrading LUT + SceneColorTint。
- **命名陷阱**：非内联（Metal/GL）= 独立 pass 读内存 `SceneColor.Resolve`，不是 subpass；仅 Vulkan `bTonemapSubpassInline` 是真 subpass（input attachment，`PostProcessTonemap.usf:745-767` shader 双路径）。
- **Metal/GL 有 FBF 能力**（`MobileBasePassPixelShader.usf:217-227` 三平台宏 `FramebufferFetchColor0`：`SubpassFetchRGBA_0`/`FramebufferFetchES2`），UE 只是没给 `MobileCustomResolve_MainPS` 写 Metal/GL 内联分支（`:748` 仅 `VULKAN_PROFILE`）——**实现范围问题，非硬件限制**（UE 注释 "only vulkan supports inline" 是现状而非不能）。

### 2.5 LDR vs HDR（`r.MobileHDR`，详完整文档 §2.5）

- LDR 把 SinglePass 降级为直通管线：SceneColor=`B8G8R8A8`、`bGammaSpace`、tonemap subpass 恒关、`bRenderToSceneColor=false`→后处理链整段跳过、SceneDepthAux 取消、硬件 sRGB、Deferred 不可用。
- LDR 禁 tonemap subpass 原因：无 HDR 数据可 tonemap（SceneColor 已是显示值），再 tonemap=二次变换；与 Deferred 同理，是**互斥的两条省带宽路线**。

## 三、Forward-Deferred toon 对齐修复体系（`[ZXB]`）

`MobileBasePassPixelShader.usf` 中 >15 处 `[ZXB Fix]`，整理为 9 行修复表（§9.2），要点：

| 修复项 | 位置 | 作用 |
|---|---|---|
| TOON_CUSTOMDATA_OVERRIDE_LOCAL | usf 653-658 | Forward 仅**半透明**覆盖 CustomData.a（`MATERIALBLENDING_TRANSLUCENT \|\| (FORWARD_SHADING \|\| !SOLID \|\| !MASKED) \|\| TRANSLUCENCY_* \|\| SLW`），对齐 Deferred 的 Opacity 源 |
| 间接光对齐 | usf 986-1020 | Forward 间接光与 Deferred 同源 |
| EnvBRDF | usf 1029-1038 | IBL 环境 BRDF 对齐 |
| CameraVector 归一化 | usf 1206-1211 | 视角向量归一化一致性 |
| ToonEnergyWeight 提前 | usf 1229-1236 | 方向光后 / LuxGI 前乘，LuxGI 不吃 Weight |
| LuxGI double-PreExposure | usf 1238-1319 + 1600-1605 | `LuxGIExtracted` 剥离后绕过 PreExposure 单独加回 |
| LuxGI 剪枝 + DeviceZ | usf 1275-1295 | 采样剪枝与深度对齐 |
| ENABLE_LUX_GI permutation | `MobileBasePassRendering.h:424-434 + 562` | 编译期 permutation + 运行时 `r.LuxGI` dispatch |
| toon 末尾跳过 PreExposure | usf 1593-1596 | `MATERIAL_SHADINGMODELS_TOON_CHARACTER` 不再乘 `ResolvedView.PreExposure` |

### toon outline 完整数据流

RT 创建（`SceneTextures.cpp:614-619`）→ `RenderPreOutlinePass`（`r.YHRP.EnableMobileOutlinePass` 门控）→ PreOutline mesh pass（`MobileOutlinePrepearPass.cpp:878-884` AAM_MSAA→DepthRead、886-893 MSAA>1 跳过 Velocity 混绑）→ `MobileToonOutlinePass`（1x 深度 + 4x/1x resolve）→ usf 两链路消费。

### 修复状态

- **已落地 9 项**（上表，均带 `[ZXB Fix]` 注释）。
- **未落地 3 项**（设计决策待定）：GBufferAO 无 toon 后置 1、CustomData.a 6bit 量化（`round(saturate(v)*63)/63`，Forward 无 GBuffer 未复现 Deferred encode→decode roundtrip）、ScreenOutlineTexture fallback White→Black（`MobileBasePassRendering.cpp:375` 仍是 White）。

## 四、关键校验发现与修正

- **3 处行号修正**、**1 处行为误述修正**（文档版本基线同步）。
- **版本元信息**：CLAUDE.md 引用的 Changelist 1011149 与 `Build.version` 不一致，以 Build.version **1077366** 为准。
- **3 处记忆/文档矛盾表述更新**：`mobile-forward-toon-char-black-outline`、`fwd-vs-def-toonstandard-customdata-a-gate`、`mobile-fwd-vs-def-exposure-mismatch` 均 append 修复后状态（记忆正文修复项有 2-3 处当前代码已不适用/未落地，均标注）。

## 五、遗留待办（文档 §10）

- **#3**：LDR 真机格式验证。
- **#6**：post-tonemap 探针——插入点已静态确认（`MobileShadingRenderer.cpp:1872/1867`，`AddMobilePostProcessingPasses`），排除清单已补全（局部光）；2× 亮度差主因仍定位在 post-tonemap 阶段（[[fwd-vs-def-post-tonemap-2x-unpinned]]）。
- **代码层**：3 处未落地差异（GBufferAO / CustomData.a 量化 / ScreenOutlineTexture fallback）是否修复，属设计决策。

## 六、快速参考

- **判断顺序速查**：`RenderForward` → `bRequiresMultiPass` 决策树（见 §2.1 表）→ Single/Multi 实现。
- **关键文件**：
  - `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp`
  - `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileOutlinePrepearPass.cpp`
  - `UE5EA/Engine/Source/Runtime/Renderer/Private/SceneTextures.cpp`（注：`SceneTexturesConfig.cpp`/`SceneUtils.cpp` 在 `Engine/Private`）
  - `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.cpp` / `.h`
  - `UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`
  - `UE5EA/Engine/Shaders/Private/MobileToonOutline.usf`
  - `UE5EA/Engine/Source/Runtime/RenderCore/Private/RenderUtils.cpp`
- **渲染路径判定**：必须用 in-shader 管线戳自报，CVar 回读 / 文件名都会骗人（[[verify-pipeline-by-in-shader-stamp]]）。

## 七、相关参考

- **完整分析文档**：`E:\AiDoc\MobileShadingRenderer_RenderForward_SingleMultiPass.md`（同源 `D:\GR_DevTest\docs\`）
- **根因分析文档**：`docs/Forward_Deferred_CartoonShadow_Alignment_Plan.md`、`docs/PVS-Mobile-NotWorking-Analysis.md`、`docs/WorldPartitionPVS实现.md`
- **相关记忆**：`mobile-fwd-vs-def-exposure-mismatch`、`fwd-vs-def-toonstandard-customdata-a-gate`、`mobile-forward-toon-char-black-outline`、`fwd-vs-def-post-tonemap-2x-unpinned`、`scene-textures-config-in-engine-private`、`docs-dir-not-in-perforce`、`p4-workspace-mapping`
