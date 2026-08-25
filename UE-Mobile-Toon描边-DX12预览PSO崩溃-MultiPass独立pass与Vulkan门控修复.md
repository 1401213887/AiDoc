# UE-Mobile-Toon描边-DX12预览PSO崩溃-MultiPass独立pass与Vulkan门控修复

> DX12 编辑器 + AndroidVulkan 预览下，移动端外扩描边与引擎 tonemap subpass 连续触发 PSO 创建失败（`E_INVALIDARG`）导致 `PipelineStateCache.cpp:684` Fatal 崩溃；根因是"移动专用代码默认移动= Vulkan"，DX12 模拟移动（PCD3D_ES31）时假设不成立。修复 = 独立 SceneColor-only pass（MultiPass 路径）+ Vulkan 平台门控。

---

## 一、问题定位流程

### 现象

| 场景 | 表现 |
|---|---|
| DX12 直接打开编辑器 | 启动 ~80s 后崩溃（`PipelineStateCache.cpp:684`） |
| DX12 + 切 AndroidVulkan High Preview | 崩溃（`PipelineStateCache.cpp:684`） |
| -vulkan + AndroidVulkan Preview | 正常，不崩 |

### 确认的关键事实（崩溃日志 + 源码交叉验证）

1. **崩溃 shader 1**：`FMobileCustomResolvePS`（`PostProcessTonemap.usf | MobileCustomResolve_MainPS`），`Render Targets: (8)`、`error 80070057`（E_INVALIDARG）
2. **崩溃 shader 2**：`FToonOutlineExpandVS/PS`（`MobileToonOutlineExpand.usf | MainVS/MainPS`），同样 `Render Targets: (8)`、`error 80070057`
3. **崩溃时平台**：`PCD3D_ES31`（DX12 模拟 ES3.1 移动平台），日志确认 `LogD3D12RHI` / `ShaderAutogen/PCD3D_SM6`
4. **CVar 状态**：`LogConfig: Set CVar [[r.Mobile.TonemapSubpass:1]]`（运行时被设为 1）

---

## 二、根因分析

### 本质：DX12 进程 + AndroidVulkan 预览 = 用 D3D12 模拟移动路径

编辑器进程是 DX12 RHI（无 `-vulkan`），预览平台切到 AndroidVulkan_Preview → UE 用 D3D12 后端模拟移动 shader（编译平台 `PCD3D_ES31`）。**移动渲染路径的代码写死了"假设 Vulkan 能力"，DX12 模拟时这些假设不成立**，于是 PSO 创建失败。

### 崩溃 1：FMobileCustomResolvePS（tonemap subpass 路径错配）

```
r.Mobile.TonemapSubpass 运行时=1
  → bTonemapSubpass = IsMobileTonemapSubpassEnabled(...) = true
     （SceneUtils.cpp:94-97：只查 CVar + IsMobileHDR + !IsMobileDeferredShadingEnabled，【不查 IsVulkanPlatform】）
  → MobileShadingRenderer.cpp:1885  if(bTonemapSubpass)  → 调 AddMobileCustomResolvePass
  → RenderMobileCustomResolve 用 ApplyCachedRenderTargets 拿当前 render pass 全部 RT
  → DX12 下 MultiPass = 8 个 GBuffer RT，而 custom resolve PSO 只声明 1 个 RT
  → D3D12 E_INVALIDARG (80070057) → PipelineStateCache.cpp:684 Fatal
```

**关键**：tonemap **subpass inline 是 Vulkan-only**（`SceneUtils.cpp:102` 注释原话："As of UE 5.4 **only vulkan** supports inline (single pass) tonemap"）。正确写法应使用带 `IsVulkanPlatform` 检查的 `bTonemapSubpassInline`，而 `:1885` 用了无检查的 `bTonemapSubpass`。

### 崩溃 2：FToonOutlineExpand（描边壳 shader 平台不兼容）

```
描边壳 pass 设计目标：移动 SinglePass Forward（1 个 SceneColor RT）
  → IsMobileToonOutlineExpandEnabled 只查 IsMobilePlatform + !Deferred（最初无 Vulkan 门控）
  → DX12 模拟移动：PCD3D_ES31 是 IsMobilePlatform=true，且 ShadingPath=0（Forward）→ 放行
  → 但 DX12 下实际走 MultiPass，render pass = 8 个 GBuffer RT
  → 描边壳 PSO（给 1 个 SceneColor RT 设计）→ E_INVALIDARG 崩
```

### PipelineStateCache.cpp:684 的触发条件（关键认知）

```cpp
else if(!Init.bPSOPrecache)
{
    // Precache requests are allowed to fail, but if the PSO is needed by a draw/dispatch, cannot continue.
    FPlatformMisc::MessageBoxExt(...);
    UE_LOG(LogRHI, Fatal, TEXT("Shader compilation failures are Fatal."));
}
```

**不是累计触发**：任何 live draw（非 precache）需要的 PSO 创建失败都**直接 Fatal**。所以描边壳在 DX12 下只要被 dispatch 就崩。

---

## 三、修复方案

### 修复 1：`MobileShadingRenderer.cpp:1885` —— tonemap subpass 判断收紧

```cpp
// 修改前
if (bTonemapSubpass)                 // 无 Vulkan 检查，DX12 也走
{
    AddMobileCustomResolvePass(GraphBuilder, Views[ViewIndex], SceneTextures, ViewFamilyTexture);
}
// 修改后
if (bTonemapSubpassInline)           // 要求 IsVulkanPlatform，与 :2188 SinglePass 对齐
{
    AddMobileCustomResolvePass(GraphBuilder, Views[ViewIndex], SceneTextures, ViewFamilyTexture);
}
```

`bTonemapSubpass`（非 inline）不查 Vulkan，`bTonemapSubpassInline` 才要求 Vulkan——两者差异正是同类 bug 的两面。

### 修复 2：MultiPass 分支独立 pass（方案 A，照抄 PC 端 RenderPCOutlinePass）

SinglePass 分支保持内联（1 个 SceneColor RT 匹配），MultiPass 分支改为独立 RDG pass：

**`MobileOutlinePrepearPass.cpp` 新增 `RenderMobileToonOutlineExpandSeparatePass`**：
```cpp
void FMobileSceneRenderer::RenderMobileToonOutlineExpandSeparatePass(FRDGBuilder& GraphBuilder, FViewInfo& View, FSceneTextures& SceneTextures)
{
    FParallelMeshDrawCommandPass& MeshPass = View.ParallelMeshDrawCommandPasses[EMeshPass::MobilePreOutline];
    if (!IsMobileToonOutlineExpandEnabled(ShaderPlatform) || !MeshPass.HasAnyDraw())
    {
        return;
    }

    View.BeginRenderView();

    FMobilePreOutlinePassParameters* PassParameters = GraphBuilder.AllocParameters<FMobilePreOutlinePassParameters>();
    PassParameters->MobileSceneTextures = SceneTextures.MobileUniformBuffer;
    PassParameters->View = View.GetShaderParameters();
    PassParameters->RenderTargets.DepthStencil = FDepthStencilBinding(SceneTextures.Depth.Target,
        ERenderTargetLoadAction::ELoad, ERenderTargetLoadAction::ELoad, FExclusiveDepthStencil::DepthRead_StencilNop);
    PassParameters->RenderTargets[0] = FRenderTargetBinding(SceneTextures.Color.Target, ERenderTargetLoadAction::ELoad);

    GraphBuilder.AddPass(
        RDG_EVENT_NAME("MobileToonOutlineExpandPass"),
        PassParameters,
        ERDGPassFlags::Raster,
        [this, &View, &MeshPass, PassParameters](FRHICommandList& RHICmdList)
        {
            SetStereoViewport(RHICmdList, View, 1);
            // [ZXB] Culling params are already built by BuildInstanceCullingDrawParams (MobileShadingRenderer).
            MeshPass.DispatchDraw(nullptr, RHICmdList, &ToonOutlineExpandInstanceCullingDrawParams);
        });
}
```

**`MobileShadingRenderer.cpp` MultiPass 分支**：从内联 `RenderMobileToonOutlineExpand(RHICmdList, View)` 改为独立 pass 调用（SceneColorRendering pass 之后）：
```cpp
// SceneColorRendering pass 之后、resolve 之前
RenderMobileToonOutlineExpandSeparatePass(GraphBuilder, View, SceneTextures);
```

**⚠️ 踩坑（复用 culling params，勿重复 build）**：独立 pass **不能**再调 `MeshPass.BuildRenderingCommands`——`EMeshPass::MobilePreOutline` 已在 `BuildInstanceCullingDrawParams`（MobileShadingRenderer :1960，两分支共用）build 过一次，重复 build 触发 `MeshDrawCommands.cpp:1792 check(!bHasInstanceCullingDrawParameters)` 断言。必须复用 `ToonOutlineExpandInstanceCullingDrawParams`。

### 修复 3：`IsMobileToonOutlineExpandEnabled` 加 Vulkan 门控（波哥拍板，务实方案）

```cpp
bool IsMobileToonOutlineExpandEnabled(EShaderPlatform ShaderPlatform)
{
    // Vulkan gate: 描边壳 shader PSO 只在 Vulkan 能创建，DX12 (PCD3D_ES31) 下 E_INVALIDARG
    return GMobileToonOutlineExpand != 0
        && IsMobilePlatform(ShaderPlatform)
        && !IsMobileDeferredShadingEnabled(ShaderPlatform)
        && IsVulkanPlatform(ShaderPlatform);
}
```

**权衡结论**：方案 A（独立 pass）是正确架构（MultiPass 下不内联进 8 GBuffer），但实测发现**描边壳 shader 的 PSO 在 DX12 下本质创建不了**（shader 平台兼容性硬限制）。所以门控仍必需——`IsVulkanPlatform` 不是"多余平台判断"，是 shader 在 DX12 下创建不了 PSO 的真实反映。

### 修复 4：CVar 重命名 + 默认值（`MobileToonOutlineExpandPass.cpp`）

| 项 | 改前 | 改后 |
|---|---|---|
| CVar 名 | `r.MobileOutline.ExpandOutline` | `r.MobileOutline.ExpandOutlineInline` |
| 默认值 | `GMobileToonOutlineExpand = 0` | `GMobileToonOutlineExpand = 1`（默认开启 inline 外扩描边） |

---

## 四、验证结果

| 场景 | 修复前 | 修复后 |
|---|---|---|
| DX12 启动（FMobileCustomResolvePS 崩） | ❌ | ✅ 完整启动 86.4s |
| DX12 + AndroidVulkan（FToonOutlineExpand 崩） | ❌ | ✅ 稳定（描边被 Vulkan 门控，不建 PSO） |
| -vulkan + AndroidVulkan（目标环境） | ✅ | ✅ 描边正常 |
| `r.MobileOutline.ExpandOutlineInline` | — | ✅ `"1" LastSetBy: Constructor` |

---

## 五、快速排查 Checklist（DX12 下移动路径 PSO 崩溃）

| # | 检查项 | 怎么查 | 命中特征 |
|---|---|---|---|
| 1 | 读崩溃 shader | 崩溃日志 `PSO shadername:` 行 | 定位失败的具体 VS/PS |
| 2 | RT 数量 vs PSO 声明 | `Render Targets: (N)` vs `NumRenderTargets` | N≠声明数 = RT 不匹配（8 GBuffer vs 1） |
| 3 | 是否有被强制开的移动 CVar | `LogConfig: Set CVar` 附近 | 如 `r.Mobile.TonemapSubpass=1` |
| 4 | 调用点是 `bTonemapSubpass` 还是 `bTonemapSubpassInline` | grep 调用处 | 前者无 Vulkan 检查 |
| 5 | shader PSO 是否只兼容 Vulkan | 加 `IsVulkanPlatform` 门控测试 | 加了就不崩 = 平台兼容性硬限制 |
| 6 | 是否重复 BuildRenderingCommands | 独立 pass 里搜 | `check(!bHasInstanceCullingDrawParameters)` 断言 |

---

## 六、关键认知

1. **移动专用代码必须显式加 `IsVulkanPlatform` 门控**，别默认"移动 = Vulkan"
2. **`bTonemapSubpass` vs `bTonemapSubpassInline`** 是同类教训的两面：一个漏了 Vulkan 检查，一个带了
3. **`PipelineStateCache.cpp:684` 非累计**：live draw 需要的 PSO 失败直接 Fatal
4. **独立 pass 复用 culling params**，不重复 `BuildRenderingCommands`
5. **MultiPass 独立 pass 是正确架构**（不内联进 8 GBuffer），但描边 shader 的平台兼容性是硬限制，门控仍必需

---

## 七、相关参考

### 关键源码位置
| 文件 | 位置 | 内容 |
|---|---|---|
| `UE5EA/Engine/Source/Runtime/Engine/Private/SceneUtils.cpp` | :94-104 | `IsMobileTonemapSubpassEnabled` / `IsMobileTonemapSubpassEnabledInline`（Vulkan-only 判定） |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp` | :1885 / :2103 / :2237 | tonemap 判断 / Single-MultiPass 分流 / 描边独立 pass 调用 |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileOutlinePrepearPass.cpp` | :938-965 | `RenderMobileToonOutlineExpandSeparatePass`（独立 pass 实现） |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileToonOutlineExpandPass.cpp` | :46-57 | `IsMobileToonOutlineExpandEnabled`（Vulkan 门控） |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MeshDrawCommands.cpp` | :1792 | `check(!bHasInstanceCullingDrawParameters)` 断言 |
| `UE5EA/Engine/Source/Runtime/RHI/Private/PipelineStateCache.cpp` | :672-687 | live draw PSO 失败直接 Fatal |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/PostProcess/PostProcessTonemap.cpp` | :1200-1268 | `RenderMobileCustomResolve`（ApplyCachedRenderTargets） |

### 同仓相关记录
- `E:\AiDoc\UE-Mobile-Toon描边-PreOutline深度偏移污染-MSAA角色涂黑与BasePass剔除修复.md` —— 移动描边历史坑（MSAA 深度污染）
- `E:\AiDoc\MobileShadingRenderer_RenderForward_SingleMultiPass.md` —— Single/MultiPass 渲染结构
- 描边完整调试史：`.planning/2026-08-23-mobileforward-simpleoutline-outlinepassw/`（progress.md / task_plan.md / findings.md）
