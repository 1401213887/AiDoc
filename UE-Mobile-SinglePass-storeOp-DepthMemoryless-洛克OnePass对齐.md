# UE Mobile SinglePass storeOp 分析：SceneColor/Depth 与洛克王国 One Pass 对齐（恢复 Depth Memoryless）

> RenderDoc 截帧显示 GR（UE5.5 fork）移动端 Forward SinglePass 的 SceneColor 和 Depth 都是 `Store`，对比洛克王国 One Pass 方案（两者 `DONT_CARE`）。根因：storeOp 只由 `TexCreate_Memoryless` flag 决定（`RenderGraphPass.cpp:52`）——截帧的 Store 是**编辑器 Preview 假象**（1x MSAA + 编辑器合成基元把 `bMemorylessMSAA` 拉成 false），**真机 4x MSAA 包体下 SceneColor/Depth 天然都是 DONT_CARE**。修复：恢复被 GR `//ericado` 注释掉的 SceneDepth Memoryless。

---

## 一、问题定位流程

1. **截帧**（RenderDoc MCP 打开 `ForwardSinglePass.rdc`，Vulkan）：帧总耗时 2.36ms，BasePass 1.83ms（78%），`MobileToonOutlineExpand` 0.171ms（本项目新增描边）。
2. **确认 GR SinglePass 结构**（action 树）：**单个 Vulkan Render Pass + 3 个 Subpass**：
   - Subpass 0：`MobileRenderPrePass`(58 draws) → `MobileBasePass`(69) → `MobileToonOutlineExpand`(49，描边内联)
   - Subpass 1：`Translucency`(2) —— `vkCmdNextSubpass()=>1`
   - Subpass 2：`MobileTonemapSubpass`(1，全屏三角形) —— `=>2`
   - `vkCmdEndRenderPass(C=Store, DS=Store)`
3. **确认真 FBF**：Tonemap PS 的 SPIR-V 含 `Capability(InputAttachment)` + `subpass.image GENERATED_SubpassFetchAttachment0` + `ImageRead({0,0})` —— 走 **subpassLoad** 读 SceneColor，不是 sampler。
4. **确认资源**：SceneColor = **R11G11B10_FLOAT**（1912×837，编辑器非沉浸视口），Depth = **D32S8**。Render Pass 共 2 个 color attachment（SceneColor + BackBuffer）。
5. **反查消费者**：主 render pass（EID 1606）之后只有 `vkCmdCopyBuffer`（VirtualTexture / MessageBuffer）+ Present，**无任何 pass 采样 SceneColor 或 Depth**——store 出去无人读。
6. 知识库检索命中《洛克王国：世界》One Pass 报告（`E:\AiDoc\洛克王国_pipeline_report.md`），逐项对比。

## 二、根因分析

### 2.1 storeOp 只由 `TexCreate_Memoryless` flag 决定（与编辑器/包体无关）

`FRDGParameterStruct::GetRenderPassInfo`（`RenderGraphPass.cpp:40-106`）：

```cpp
:52  ERenderTargetStoreAction StoreAction = EnumHasAnyFlags(Texture->Desc.Flags, TexCreate_Memoryless) ? ENoAction : EStore;
:83  同上（depth）
:98  DepthStoreAction = ExclusiveDepthStencil.IsUsingDepth() ? StoreAction : ENoAction;
:99  StencilStoreAction = ExclusiveDepthStencil.IsUsingStencil() ? StoreAction : ENoAction;
```

- `FRenderTargetBinding`（`ShaderParameterMacros.h:523`）**没有 storeAction 字段**，storeOp 完全由 RDG 按 memoryless flag 推导。
- `InitRenderTargetBindings_Forward`（`MobileShadingRenderer.cpp:1998-2016`）只显式指定 `loadAction=EClear`。

### 2.2 Depth 恒 Store 的原因（GR fork 定制）

`GetSceneDepthStencilCreateFlags`（`SceneTexturesConfig.cpp:320-333`）：

```cpp
if (!bKeepDepthContent || (NumSamples > 1 && bMemorylessMSAA && (!IsMobilePlatform(ShaderPlatform) || !MobileUsesFullDepthPrepass(ShaderPlatform))))
{
    //DepthCreateFlags |= TexCreate_Memoryless;   // ← GR //ericado 注释掉（P4 filelog 888829 引入）
}
```

GR fork 用 `//ericado` 把 `TexCreate_Memoryless` 这行注释掉 → SceneDepth 永不 memoryless → 恒 Store。原因：深度被 SceneDepth 材质节点 / HZB / 描边 / SSXR 消费。

### 2.3 SceneColor 的 memoryless 是引擎原版逻辑（GR 没动）

`GetSceneColorFormatAndCreateFlags`（`SceneTexturesConfig.cpp:313-316`）：

```cpp
if (NumSamples > 1 && bMemorylessMSAA)
{
    SceneColorCreateFlags |= TexCreate_Memoryless;
}
```

SceneColor 在 `NumSamples>1 && bMemorylessMSAA` 时**本来就 memoryless**。当前截帧是 1x MSAA → 不走此分支 → Store。

### 2.4 `bMemorylessMSAA` 的赋值（关键判定）

`MobileShadingRenderer.cpp:778`：

```cpp
SceneTexturesConfig.bMemorylessMSAA = !(bRequiresMultiPass || bShouldCompositeEditorPrimitives || bRequireSeparateViewPass);
```

- **SinglePass + 非编辑器合成 → true**（SceneColor/Depth 可 memoryless）
- 编辑器（`bShouldCompositeEditorPrimitives`）或 MultiPass → false

### 2.5 完整判定表

| 环境 | MSAA | bMemorylessMSAA | SceneColor | Depth |
|---|---|---|---|---|
| 编辑器 Preview（本截帧） | 1x | false（编辑器合成） | **Store** | **Store** |
| **包体·真机 4x MSAA + SinglePass（Vulkan）** | 4x | **true** | **DONT_CARE** | **DONT_CARE**（恢复后） |
| 包体 1x MSAA | 1x | true 但 NumSamples=1 | Store | Store |

→ **截帧 Store 是编辑器 Preview 假象。真机 4x MSAA 包体下 SceneColor/Depth 天然 DONT_CARE，与洛克 One Pass 对齐**，无需额外 SceneColor 定制。GR 之前禁用 depth memoryless 属过度保守（SinglePass 无 full prepass 时深度本就该丢弃）。

## 三、详细技术原理

### 3.1 洛克王国 One Pass（对照基准）

- 引擎 UE 4.26，Mobile Forward；核心：PrePass→ToneMapping **全部塞进单 Render Pass**（3~4 个 Subpass），SceneDepth 走 **Memoryless**（`storeOp=DONT_CARE`），SceneColor 被消费完即丢，**整帧只有 BackBuffer 写一次 DRAM**。
- 三件套：**FrameBuffer Fetch / Depth Fetch / Memoryless Attachment**。
- SceneColor = **RGB10A2**（32bit，Alpha 偷渡 Stencil 做角色标记，省 Custom Depth 额外 pass）。
- 双轨分流：**旗舰跑 SubPass 优化版（5→2 Pass，保留 SSAO/SSR/Bloom）；中低端跑 One Pass（放弃邻域采样效果）**。One Pass 是另一条平行管线，不是 2 Pass 的再优化。
- bpp 预算：SceneColor(32) + Depth(32) = 64~96 bit/px，压进 Mali 128bit 预算。
- 性能收益（iPhone X 实测）：写带宽 −30%、读带宽 −15%、真实帧时 −3%。

### 3.2 GR SinglePass 与洛克 One Pass 对比

| 维度 | 洛克 One Pass | GR SinglePass（截帧实测） | 状态 |
|---|---|---|---|
| 单 Render Pass | ✅ | ✅（EID 447→1606） | 已对齐 |
| Subpass 划分 | PrePass / BasePass / 半透明 / 后处理 | PrePass+BasePass+描边 / 半透明 / Tonemap | 更聚合（描边内联） |
| FBF 接力 | subpassLoad | subpassLoad（InputAttachmentIndex=1） | 已对齐 |
| Tonemap 内联 | 后处理链写 BackBuffer | PreExposure+ColorGrading LUT+Tonemap 全内联 | 已对齐 |
| SceneColor storeOp | DONT_CARE | **包体 4x MSAA = DONT_CARE**；编辑器 Preview = Store（假象） | 已对齐 |
| Depth storeOp | DONT_CARE（Memoryless） | 包体 4x MSAA = DONT_CARE（恢复后）；编辑器 = Store | 已对齐 |
| 双轨分流 | 旗舰 2 Pass / 中低端 1 Pass | MultiPass / SinglePass（`bRequiresMultiPass`） | 结构已具备 |

### 3.3 Depth Memoryless 的恢复条件（引擎原版逻辑自带安全阀）

```cpp
if (!bKeepDepthContent || (NumSamples > 1 && bMemorylessMSAA && (!IsMobilePlatform(ShaderPlatform) || !MobileUsesFullDepthPrepass(ShaderPlatform))))
```

- 有 **full depth prepass** 时不 memoryless（BasePass 用 `CF_Equal` 读 prepass 深度）。
- 无 prepass + MSAA + bMemorylessMSAA 时 memoryless（深度只活在 render pass 内）。

### 3.4 Memoryless 深度对消费方的影响

- MSAA 下 `Depth.Resolve` 继承同一 Desc 的 memoryless flag（`SceneTextures.cpp:589`）。
- `SceneTextures.cpp:1653` 检查 `!EnumHasAnyFlags(SceneTextures->Depth.Resolve->Desc.Flags, TexCreate_Memoryless)` 才设置 `SceneDepthTexture` → **memoryless 时材质 SceneDepth 节点 fallback 到 `SystemTextures.DepthDummy`**（:1403-1404）。
- 影响面：SceneDepth 材质节点 / HZB occlusion / 描边深度读取 / SSXR。有 full prepass 时条件自动不 memoryless，安全。

## 四、修复方案

### 4.1 恢复 SceneDepth Memoryless（1 行）

`SceneTexturesConfig.cpp:326`，去掉 `//ericado` 的注释符号，恢复引擎默认：

```cpp
if (!bKeepDepthContent || (NumSamples > 1 && bMemorylessMSAA && (!IsMobilePlatform(ShaderPlatform) || !MobileUsesFullDepthPrepass(ShaderPlatform))))
{
#pragma region Engine ZXB
	// [ZXB] 恢复引擎默认：SceneDepth 允许 Memoryless（不写回 DRAM），撤销 GR //ericado 定制的注释
	DepthCreateFlags |= TexCreate_Memoryless;
#pragma endregion
}
```

- `p4 edit` 已挂（client `DJANGOZHAN-PCFW_GR_DevTest`），`p4 diff` 仅 1 处干净（无行尾污染，文件 LF）。
- ⚠️ 文件也被 `Tools_Program_Support` workspace 打开，提交时可能要 resolve。
- 按全局规范用 `#pragma region Engine ZXB` + `// [ZXB]` 注释包裹。

### 4.2 SceneColor 无需改动

包体 4x MSAA 下 SceneColor 天然 memoryless（引擎原版逻辑，见 §2.3）。**不要**给 SceneColor 加 memoryless（它带 `TexCreate_InputAttachmentRead` 供 tonemap subpassLoad，编辑器/截图需保留）。

## 五、快速排查 Checklist

1. **判断 storeOp 只看 `TexCreate_Memoryless` flag**（`RenderGraphPass.cpp:52`），不要纠结编辑器/包体差异。
2. **编辑器截图 Store 是假象**：编辑器 `bShouldCompositeEditorPrimitives=true` → `bMemorylessMSAA=false`。真机/包体验证才真实。
3. **包体判定**：`bMemorylessMSAA = !(bRequiresMultiPass || bShouldCompositeEditorPrimitives || bRequireSeparateViewPass)`（`MobileShadingRenderer.cpp:778`）+ `NumSamples>1`。
4. **SceneColor memoryless 条件**：`NumSamples>1 && bMemorylessMSAA`（`SceneTexturesConfig.cpp:313`，引擎原版）。
5. **Depth memoryless 条件**：`!bKeepDepthContent || (NumSamples>1 && bMemorylessMSAA && (!IsMobilePlatform || !MobileUsesFullDepthPrepass))`（有 full prepass 自动不 memoryless）。
6. **验证方法**：真机 RenderDoc 看主 Render Pass 的 SceneColor/Depth storeOp（4x MSAA 预期 `DONT_CARE`）+ SceneDepth 材质节点渲染正常。
7. **恢复 depth memoryless 后**：检查 SceneDepth 材质节点 / HZB / 描边深度读取是否 fallback 到 dummy 深度。

## 六、相关参考

- 洛克王国 One Pass 报告：`E:\AiDoc\洛克王国_pipeline_report.md`（本仓库 KB：`kb/aidoc-stubs/_pipeline_report.md`）
  - [UFSH2025《洛克王国:世界》移动端管线设计与优化（朱谷才，腾讯魔方）](https://pd.qq.com/g/roco135790/post/B_f00c2869382807001441152186774648610X60)
  - [UF2025(Shanghai) 移动端渲染管线优化实战 — GameRes](https://www.gameres.com/916723.html)
  - [UE4/UE5 移动端延迟渲染（可可西）— FBF/DepthFetch/Memoryless 细节](https://www.cnblogs.com/kekec/p/17050979.html)
- 关键源码位置（本 fork，2026-08-25 复核）：
  - `RenderGraphPass.cpp:40-106` — storeOp 推导（memoryless flag 决定）
  - `SceneTexturesConfig.cpp:313-316 / 320-333` — SceneColor/Depth create flags
  - `MobileShadingRenderer.cpp:778` — `bMemorylessMSAA` 赋值；`:1998-2016` — `InitRenderTargetBindings_Forward`（只设 loadAction）
  - `SceneTextures.cpp:555-612 / 1121-1135 / 1651-1655` — Depth/SceneColor/DepthAux 创建与 SceneDepthTexture 设置
  - `ShaderParameterMacros.h:523` — `FRenderTargetBinding`（无 storeAction）
- 关联记忆：`depth-memoryless-disabled-by-ericado`（GR 禁用原因）、`requires-multipass-in-mobileshadingrenderer`、`mobile-fwd-vs-def-post-tonemap-2x-unpinned`。

归档时间：2026-08-26。
