# UE-Mobile-MSAA-实现原理-Resolve机制与深度采样链路

> UE5 移动端（Mobile/Forward）MSAA 实现的核心分工：**颜色 per-pixel 只着色一次、深度/覆盖率 per-sample 判定**；MSAA 纹理只在其生成 pass 内部以 4x 存在，**跨 pass 一律 resolve 成 1x 才能被采样**（4 大图形 API 硬约束）。本文讲清 UE 的封装（CreateTextureMSAA）、两条 resolve 链、depth 为何不能全程 1x、outline pass 输出带 resolve 的性能真相，并给出 **带宽成本（理论 4x vs 实际）与 AA 选型（MSAA/FXAA/TAA + 手游分档）** 的工程结论。

---

## 一、MSAA 核心原理：per-sample 覆盖 + per-pixel 着色

```
每个像素 4 个采样点（r.MSAACount=4）
┌──────┬──────┐      ● = 采样点（只参与 coverage/depth 判定，不单独着色）
│ ● ● │ ● ● │
│ ● ● │ ● ● │
└──────┴──────┘
           │
           ▼
   光栅化：对每个像素的 4 个 ● 做「三角形覆盖 + 深度测试」
   内部像素 → 4/4 覆盖 → 全写
   边缘像素 → 1~3/4 覆盖 → 只写被覆盖的 sample   ← 抗锯齿的关键
           │
           ▼
   fragment shader：每像素只执行 1 次（颜色只算一遍）
           │
           ▼
   RESOLVE（render pass 结束时，硬件）：
   4 个 sample 的颜色 → 合并 → 1 个像素颜色（边缘像素 = 混合后颜色）
```

**关键分工**：
- **深度/覆盖率 = per-sample（4x）**：决定每个 sample 写不写 → 边缘平滑全靠它
- **颜色 = per-pixel（1x）**：fragment shader 只跑一次 → 省成本的地方

**为什么 MSAA 比 SSAA 便宜**：
```
SSAA 4x：每像素 shader 跑 4 次，4x 全分辨率渲染
MSAA 4x：每像素 shader 只跑 1 次，4 sample 只做 coverage/深度测试
```

**"shading 1x"的精确含义：fragment 的生成单位是像素，不是 sample**

一个像素被三角形切到，哪怕只覆盖 1 个 sample，光栅化器也只产生 **1 个 fragment**、shader 只跑 **1 次**、产出 **1 个颜色值**，然后这个值被**复制**到所有存活的 sample slot。4 个 sample 各自独立的只有 coverage 和 depth，**颜色是共享的**——不是"平均不到 4x"，是**恰好 1x**。

```
一个像素，4x MSAA（rotated grid），三角形斜切只覆盖 s0、s2、s3：
  coverage mask = 1011  (s1 在三角形外)

  1. coverage 测试 → mask = 1011
  2. mask 非零 → 生成 1 个 fragment
  3. shader 跑 1 次，插值出颜色 C = 红
  4. 写回：
       color[s0]=红  color[s1]=不写(保留背景蓝)  color[s2]=红  color[s3]=红
  5. resolve 平均 = (红+蓝+红+红)/4 = 75%红 + 25%蓝 → 过渡色
```

3 个被覆盖的 sample copy 的是**同一个红**，它们之间没有颜色差异，差异只体现在"被覆盖/没覆盖"的二值上。这解释了 MSAA 的两条边界：

- **对三角形边缘有效**：覆盖是部分的，resolve 出来是渐变
- **对 shader 内部锯齿无效**：边缘像素里那个"红"本来就是同一个红，shader 不会为不同 sample 算出不同颜色 → specular 闪烁、法线细节、procedural pattern 的 aliasing 一律管不了

**MSAA vs SSAA 本质区别**：

| | 着色次数 | 几何采样 | 开销瓶颈 |
|---|---|---|---|
| SSAA | N 次 | N 倍 | 着色（计算）|
| MSAA | 1 次 | N 倍 | 带宽（N 倍 buffer 读写）|

**三个关键细节**：
- **插值位置**：默认像素中心。像素中心可能在三角形外（边缘像素）→ 用 **centroid**（coverage 内 sample 的重心）保证插值在三角形内
- **Sample shading / per-sample 着色**：显式要求每 sample 跑 shader → 退化回 SSAA（GL `gl_SampleID`/`glMinSampleShading`）
- **Alpha-to-coverage**：把 fragment alpha 转成 coverage mask，配合 MSAA 对 alpha test 边缘（树叶、铁丝网）抗锯齿——否则 alpha test 的硬边光栅化 coverage 是二值的，无 AA 效果

## 二、为什么 MSAA RT 必须 resolve 才能被采样

4 大 API 均禁止普通采样器读 MSAA 纹理：

| API | MSAA 采样规则 |
|---|---|
| Vulkan | `texture()` 采样函数不能用于 multisampled image，只能 `texelFetch` 显式取每个 sample |
| GLES 3.x | 同样，`texture()` 对 multisample 无效，`texelFetch` 必须带 sample 参数 |
| Metal | `sample()` 不能用于 MSAA 纹理，只能 `read()` 逐 sample 或走 resolveAttachment |
| D3D11/12 | HLSL `Texture2D.Sample` 对 MSAA 直接编译报错，只能 `Texture2DMS.Load`，输出靠 `ResolveSubresource` |

"写 4x、跨 pass 读 1x"不是优化选项，是**唯一合法路径**。UE 把约束封装成便捷模式。

**两层"颜色"辨析（避免概念混淆）**：
- **sample 的颜色**：写回阶段就确定了（被覆盖的 = shader 输出色，未覆盖的 = 之前 clear/背景值）。resolve 之前就躺在 4x buffer 里
- **1x 最终像素色**：resolve 时才由平均产生。resolve 是 **box filter（平均）**，纯降采样，**不产生新信息**，只是把 4 个已知值折成 1 个

**resolve 只在边缘像素有实质意义**：内部像素 4 个 sample 颜色全同，平均完还是它自己（平凡操作）；只有边缘像素 sample 颜色不同，平均才产出那个过渡色——这才是 AA 效果的来源。默认语义是平均，Vulkan/D3D 支持 custom resolve 换滤波核，但实际几乎没人动它。

## 三、UE 的封装：CreateTextureMSAA + 每帧链路

**一对纹理**：
```
r.MSAACount>1 → AAM_MSAA
    ├──► Target  (NumSamples=4)   ← 只作为本 pass 的 RT 被光栅化写入
    └──► Resolve (NumSamples=1)   ← 伴随的 1x，供后续跨 pass 采样
    非 MSAA 时：Resolve == Target（IsSeparate()=false）→ resolve 环节零开销
```

**写时**用 `FRenderTargetBinding(Texture, Resolve, LoadAction)`——4x 输出，render pass 结束时驱动硬件自动 resolve 到 1x。

**每帧实际链路（Mobile）**：
```
MobileSceneRender（每帧）
│
│ ① 深度 prepass
│    RenderFullDepthPrepass ──► SceneTextures.Depth (4x)
│         └─ AddResolveSceneDepthPass ──► Depth.Resolve (1x)  ← 手动 resolve pass
│
│ ② BasePass（SceneColor）
│    FRenderTargetBinding(Color, Color.Resolve, EClear)
│    BasePass ──► Color (4x) ──pass 结束·硬件自动 resolve──► Color.Resolve (1x)
│         └─ 同步写 MobileCharFeatureTexture (4x，同 pass)
│              └─ outline pass 输出 4x + resolve 1x   ← 项目补的环节
│
│ ③ 后处理 / 全屏 pass
│    采样 Color.Resolve、Depth.Resolve、MobileCharFeatureTexture.Resolve（全 1x）
│
│ ④ FinalSceneColor ──► BackBuffer
```

**两种 resolve 并存**：
| 类型 | 触发方式 | 用途 |
|---|---|---|
| 硬件自动 resolve | `FRenderTargetBinding(RT, Resolve, ...)` | color（SceneColor、outline、MobileCharFeatureTexture） |
| 手动 resolve pass | `AddResolveSceneDepthPass`（SceneRendering.cpp:7736） | depth（保持 depth+stencil 语义 / memoryless 无 Resolve attachment） |

## 四、为什么 depth 走手动 resolve 而非硬件

**核心：`GRHISupportsDepthStencilResolve`**。color resolve 是平台强制支持的核心功能；depth/stencil resolve 是**可选特性**，移动端（Vulkan/GLES）普遍 false。一旦代码给 depth attachment 绑 Resolve 想走硬件，RHI 直接 ensure：
```cpp
// RHI.cpp:1803
ensureMsgf(GRHISupportsDepthStencilResolve, TEXT("...DepthStencil resolve not supported..."));
```
（项目实机踩过：给 PreOutline depth 绑定 Resolve 触发此 ensure → depth 1x 改由 AddResolveSceneDepthPass 手动 resolve 产生。）

| | Color resolve | Depth resolve |
|---|---|---|
| 语义 | 平均/覆盖合并 → 颜色合理 | 取哪个 sample？深度非线性，"平均"无意义 |
| Stencil | 无 | depth 常带 stencil，resolve 只允许取 sample 0（Vulkan 规定） |
| 平台要求 | 强制 | 可选（`VK_RESOLVE_MODE_SAMPLE_ZERO` 等） |
| Memoryless | — | `bKeepDepthContent=false` 时 Depth 无 Resolve attachment（SceneTextures.cpp:587），硬件 resolve 无源 |

**手动 pass 的优势**：
- 不受 `GRHISupportsDepthStencilResolve` 限制，跨平台一致
- 精确控制取哪个 sample / resolve 语义
- 可同时处理 stencil 保留/重写
- 只在 `bRequiresSceneDepthAux`（后续真要 1x 深度）时才跑，否则跳过零开销
- 时机精确：插在 prepass 之后，不依赖 pass 边界

## 五、为什么 depth 必须 per-sample（不能全程 1x）

**抗锯齿全部来自 per-sample 深度覆盖判定**。如果 depth 是 1x（每像素一个深度值）：
```
depth 1x → 边缘像素 4 sample 共享同一深度
    → 覆盖判定退化为「像素级」：要么全通过、要么全被遮挡
    → 边缘像素纯三角形色或纯背景色 → 锯齿原样保留 → MSAA 失效
```

**第二层硬约束：render pass 的 attachment 采样数必须一致**：
```
同一 render pass 里绑定的所有 attachment 必须 NumSamples 相同
    ├─ SceneColor 4x（要 resolve 出 1x 给后处理）
    └─ SceneDepth 必须也 4x   ← 否则非法
```
SceneColor 要 4x，深度跟它同 pass 就必须 4x；拆成 depth 1x + color 4x 两个 pass，color pass 的 per-sample 覆盖测试又拿不到 per-sample 深度——死结。

**但 UE 已把"4x 深度不落 DRAM"做到极致**（移动端 Tile-Based GPU）：
```
SceneDepth 4x 只在 on-chip tile 内存（memoryless，不写 DRAM）← 负责 per-sample 测试
    └─ 手动 resolve → 1x DepthAux/Resolve 写 DRAM ← 负责后续采样
```
配合 `bRequiresSceneDepthAux` 懒开关：没有任何 pass 需要 1x 深度时，连 resolve pass 都跳过。

## 六、两条 resolve 链的根源辨析（本次项目改动）

```
链 A：SceneDepth 的 1x（AddResolveSceneDepthPass）——引擎级，非本次引入
    prepass 写 4x → 手动 resolve → Depth.Resolve (1x)
    ├─ 服务：DOF / 雾 / 接触阴影 / 半透明深度 / ...
    └─ outline pass 读深度（SceneDepthTex = Depth.Resolve）← 只是消费者之一

链 B：outline 输出的 1x（本次引入）——根源是"输出被跨 pass 采样"
    outline pass 写 4x → 硬件 resolve → MobileCharFeatureTexture.Resolve (1x)
    ├─ 服务：BasePass 采样 ScreenSpaceOutline
    └─ 服务：Water SSR 采样 MobileOutlineTexture
```

**结论**：
- **outline 读深度不产生 resolve**——用的是引擎现成的 `Depth.Resolve`（prepass 配置决定，非 outline 触发）
- **本次真正引入的 resolve** = outline pass 输出的 4x 落 1x，根源是 **BasePass/Water SSR 用普通采样器读 outline 结果**（非 MS 采样 4x 非法），跟"读深度"无关

## 七、为什么 outline 输出 resolve 开销可忽略（不改 BasePass 采样器）

- **resolve 是 GPU 硬件在 tile 内完成的折叠**：移动端 Tile-Based 渲染器，outline pass 的 4x 颜色写进 on-chip tile，pass 结束时驱动在 tile 内折成 1x 写回 Resolve，**不额外读回 DRAM**，成本 = 1x 分辨率的一次 DRAM 写入
- **非 MSAA 时 `IsSeparate()=false` → resolve=nullptr → 完全不走 resolve，零开销**
- **为什么不改 BasePass 直接采 4x**：
  1. 平台硬约束：BasePass 普通 `Texture2D.Sample` 采样 4x 非法（Android 崩溃/descriptor mismatch 的根源）
  2. 就算写 Tex2DMS：BasePass 是全屏最大 pass，每像素 4 次 texelFetch + 手动平均，比 outline pass 尾端 1 次硬件 resolve 贵得多
  3. 架构约定：MSAA 是"pass 内部"属性，跨 pass 一律 1x（SceneColor/SceneDepth 同模式），BasePass 巨型 permutation 加 Tex2DMS 分支不值得

| 方案 | 成本 |
|---|---|
| outline pass 尾端硬件 resolve（现状） | 1 次 1x 写入，tile 内完成，非 MSAA 零开销 |
| BasePass 内 Tex2DMS 采 4x | 每像素 4 次 texelFetch，全屏执行，且 Vulkan 非法需专门 shader 变体 |

## 八、阴影 Upsample 的 depth 绑定判空与自采深度

移动端 + MSAA 下，阴影 Upsample pass（`DistanceFieldShadowing` / `ScreenSpaceShadows`）把 4x `SceneDepth.Target` 与 1x `ScreenShadowMaskTexture` 绑进同一 render pass → RHI.cpp:1753 samples mismatch ensure。处理：**有 1x `Depth.Resolve` 才绑 depth，无则不绑**。

**ScreenShadowMaskTexture 是引擎标准 1x 纹理**（非项目加）：
| 路径 | 创建位置 | 采样数 |
|---|---|---|
| Deferred | `LightRendering.cpp:2006` `FRDGTextureDesc::Create2D(ShadowMaskBufferSize, PF_B8G8R8A8, ...)` | 1x |
| Mobile | `ShadowRendering.cpp:2814` `FPooledRenderTargetDesc::Create2DDesc(..., false, 1, false)` | NumSamples=1 |

项目 `MMH/MMHShadowMapRendering.cpp` 只作为 RT 写入它；MobileBasePass/Deferred 光照都按 1x 采样。**不能给它开 MSAA**：开了后 BasePass 非 MS 采样仍要 resolve，问题从 depth 侧原样转移；且阴影是低频内容，per-sample 覆盖毫无收益（阴影锯齿来自 shadow map 滤波，不是几何覆盖）。

```cpp
	// [ZXB] 修复 Android Vulkan+MSAA：Upsample 混绑 4x depth 与 1x mask 触发 samples ensure；改绑 1x Resolve（无则不绑，shader 自采深度）
	if (SceneTextures.Depth.Resolve)
	{
		PassParameters->RenderTargets.DepthStencil = FDepthStencilBinding(SceneTextures.Depth.Resolve,
			ERenderTargetLoadAction::ELoad, ERenderTargetLoadAction::ELoad, FExclusiveDepthStencil::DepthRead_StencilRead);
	}
```

**为什么不能回退 `? : Target`**：`Depth.Resolve` 为 null 的场景是 `bKeepDepthContent=false`（memoryless depth，SceneTextures.cpp:587），此时 `Depth.Target` 是 **4x**，回退即复发 ensure（第一版 `? : Target` 实测 ensure 后改判空）。

**为什么"不对称"是刻意的**：
| 对象 | 非 MSAA 时 Target | 回退安全？ |
|---|---|---|
| `MobileOutlineTexture.Target`（项目自建） | 本身就是 1x | ✅ 可三元回退 / 传 nullptr |
| `SceneDepth.Target`（引擎场景深度） | MSAA 下始终 4x | ❌ 回退即 ensure |

**"自采深度" = 普通纹理采样**（非片上缓存 fetch）：shader 通过 `MobileSceneTextures.SceneDepthTexture`（ScreenSpaceShadows.usf:163）`CalcSceneDepth(ScreenUV)`（:134）显式 Sample 深度数值。depth attachment 是 GPU 隐式深度测试（shader 摸不到）；SceneDepthTexture 是 shader 显式采样读数。memoryless 深度在 TBDR 上恰好躺在 on-chip tile 内存（采样走片上命中），但"自采"语义是普通采样，与"从片上读"是独立的两件事。

**不绑 attachment 结果一致**：Upsample shader 的深度数值来自 SceneTextures UB，不依赖 render pass 绑不绑 attachment；全屏四边形无遮挡，depth test 恒通过无实际过滤。所以无 1x Resolve 时不绑，结果与绑 1x 完全相同。

**保留 if 版的决策**：去掉 if 无条件不绑功能上也等价，但保留它最贴近引擎原语义（原代码绑 `DepthRead_StencilRead`），未来若 pass 加 stencil 依赖不会踩坑。判空在此是"语义正确"而非防御性死代码。

## 九、案例：MSAA 屏幕中心黑斑（PreOutline 深度不一致）

> 具体 bug 排查：Mobile Forward + MSAA 下角色脸部黑斑，根因是 PreOutline mesh pass 用 DepthWrite 写 4x Depth.Target，而 outline pass 采样 Depth.Resolve（prepass 快照）不一致，深度 Laplacian 误判角色内部为描边 → 整角色涂黑。修 DepthRead（按 AA method 条件化）。

### 现象与探针定位

Android Vulkan Preview + Mobile Forward + `r.Mobile.AntiAliasing=3`（MSAA）+ outline → 角色脸部黑斑（8bit ~(1,1,2)）；FXAA=1 正常。复现前置：相机 `(295.16,162.39,126.92)`/`(9.20,270.20,0)`；`r.Mobile.ShadingPath 0`；`r.MobileOutline.ToonOutlineUsePreOutline 2`。

| 探测点 | 手段 | 结果 | 结论 |
|---|---|---|---|
| BasePass 中心 | FWD OutColor 探针 | 肤色 0.32（MSAA/FXAA 相同）| **黑不在 BasePass** |
| 描边 mask @黑斑中心 | ScreenOutlineTexture 探针 | 全 0 | 描边输入本身 0 |
| 最终 SceneColor 中心 | Mode A 读 Color.Resolve | MSAA 全黑 0 | **黑在最终 SceneColor** |
| PreOutline 开关 | CVar 二分 `ToonOutlineUsePreOutline 0/2` | =2（mesh）黑斑 | 锁定 **PreOutline pass** |

### 根因：PreOutline 深度与 outline 采样不一致

```
PreOutline mesh pass ──写──► MobileOutlineTexture（4x+1x）
outline pass 采 SceneDepthTex + PreOutlineResolve ──► ToonOutlineMask（深度 Laplacian）
BasePass: Color = lerp(Color, 0, ToonOutlineMask)
```

`MobileOutlinePrepearPass.cpp` PreOutline mesh pass 用 `DepthWrite_StencilRead` 写 **4x Depth.Target**；而 outline pass 的 `SceneDepthTex` 在 MSAA 下是 **`Depth.Resolve`**（prepass 快照，不含 PreOutline 写入的深度）→ 深度 Laplacian（`MobileToonOutline.usf CalcDepthLaplacian`）基于不一致深度，把角色内部误判为描边边缘 → mask 全 1 → 涂黑。**FXAA 正常**：非 MSAA 时 Resolve=null，`SceneDepthTex` 回退 `Depth.Target`（与 PreOutline 同一 1x 深度）→ 一致。

### 修复：PreOutline 改 DepthRead（按 AA method 条件化）

```cpp
// [ZXB Fix] PreOutline DepthBinding 按 AA method（AAM_MSAA）而非 NumMSAASamples 判断：
// r.MSAACount=1 是"AAM_MSAA+1采样"混合态，按采样数判断会让它走 DepthWrite → 涂黑。
const bool bPreOutlineDepthRead = (Views[ViewIndex].AntiAliasingMethod == AAM_MSAA);
FDepthStencilBinding DepthBinding = FDepthStencilBinding(SceneTextures.Depth.Target, ELoad, ELoad,
	bPreOutlineDepthRead ? FExclusiveDepthStencil::DepthRead_StencilRead : FExclusiveDepthStencil::DepthWrite_StencilRead);
```

- AAM_MSAA（含 r.MSAACount=1）→ DepthRead（基于 prepass 深度，与 outline 采样一致）；FXAA/TAA → DepthWrite（保持原行为）
- `NumMSAASamples` 判定链：`r.Mobile.AntiAliasing` → `GetDefaultAntiAliasingMethod`（FXAA=1→AAM_FXAA / TAA=2→AAM_TemporalAA / MSAA=3→AAM_MSAA）→ `GetDefaultMSAACount`（仅 AAM_MSAA 返回 r.MSAACount，默认 4；其余=1）

### 回归：Depth.Resolve 可能为 null（对齐原生时的坑）

对齐原生把 `SceneDepthTex` 三目简化成直接 `.Resolve` 后，引入 MSAA 下角色概率不渲染：
- 根因链：MSAA + `bRequiresSceneDepthAux`（`r.Mobile.SceneDepthAux=1`+`TonemapSubpass=0` 时 Vulkan 下 true）→ `MobileShadingRenderer.cpp:761` 强制 `bKeepDepthContent=false` → `SceneTextures.cpp:587-590` 无 else → **`Depth.Resolve=null`**
- 修复：`SceneDepthTex = Depth.Resolve ? Depth.Resolve : DepthAux.Resolve`（aux 深度由 full depth prepass 产生，Mobile base pass 同款深度源，SceneTextures.cpp:1688）
- **教训**：`CreateTextureMSAA` 自定义纹理 Resolve 恒非空，但 **SceneDepth 手动创建逻辑 Resolve 可能 null**——三目回退在这里不是防御，是必需的

### r.MSAACount=1 兼容（方案 A）

r.MSAACount=1 是"AAM_MSAA+1 采样"混合态（CVar 注释"1: disabled"但 SceneUtils.cpp:159 只对 <=0 降级）。RenderDoc 定位：**MobileRenderPrePass 没画角色**（非描边问题）：
1. `DepthPassCanOutputVelocity` 原按采样数判断 → =1 返回 true → `DDM_AllOpaqueNoVelocity`
2. `ShouldDrawDepthPass` NoVelocity 分支跳过角色（movable+velocity）
3. Mobile 非 TAA → `ShouldRenderVelocities=false` → velocity pass 不跑 → 角色深度永久缺失
4. BasePass 深度测试剔除角色

修复（VelocityRendering.cpp:612，一行）：`bMSAAEnabled = (GetDefaultAntiAliasingMethod == AAM_MSAA)` → r.MSAACount=1 → DDM_AllOpaque → 角色在 depth pass 写深度。

验证（对比法）：MSAACount=1 vs FXAA = **1%**（修复生效）；FXAA vs MSAA=4 = 1%（无回归）；MSAACount=1 vs 4 = 21%（1x/4x 光栅化差异正常）。

### 排查 Checklist（MSAA 描边黑斑类）

1. 确认 Mobile 平台：preview 必须 `AndroidVulkan_Preview`（编辑器默认 None=PC，Mobile CVar 不生效）；日志 `MobileBasePass` 关键行确认路径
2. 探针定位黑在哪：BasePass OutColor vs 最终 SceneColor——若 BasePass 正常而 SceneColor 黑，黑在 BasePass 之后
3. CVar 二分锁定 outline 链路：`EnableMobileOutlinePass 0/1`、`ToonOutlineUsePreOutline 0/2`
4. 深度一致性：PreOutline DepthBinding（Write/Read）与 outline 采样 SceneDepthTex 来源是否一致
5. 验证必须干净重启后单次设置 CVar（切换会累积状态污染，曾误判回归）

## 十、带宽成本：理论 4x vs 实际

**理论账面**：4x MSAA 的 color + depth 每次读写 4 倍数据量，纯带宽就是 4x（无压缩下界）。

**实际靠两个机制降下来**：

| 机制 | 适用架构 | 效果 |
|---|---|---|
| 无损 color/depth 压缩 | PC immediate mode | 内部像素（4 sample 同色）压缩到近 1x，边缘像素才真 4x → 典型 **1.3~2x** 带宽 |
| tile 内折叠 resolve | 移动端 TBDR | 4x 数据全程在 on-chip tile memory，写回主存才 resolve 成 1x → **主存带宽几乎不涨** |

**移动端 MSAA 的真实开销大头**（不是带宽）：
1. **tile memory 占用翻倍** → 砍 tile 并行度 / 增大 tile 尺寸（低端小 tile memory 上被放大）
2. resolve 的 ALU（虽折叠进写回，也要算）
3. 光栅化/深度测试的 4x 采样率

**经验量级**：移动端 4x MSAA 帧时间 +10~25%，2x +5~12%；**绝不是 4x**。

## 十一、AA 方案选型：MSAA vs FXAA vs TAA

| | 成本 | 随几何复杂度 | 覆盖锯齿类型 | 副作用 |
|---|---|---|---|---|
| FXAA | +1~3%（<1ms）| 不涨 | 几何边缘（弱）| 全屏模糊、subpixel 差、无 temporal 稳定性 |
| MSAA 4x | +10~25% | 涨 | 几何边缘（好）| 无（但管不了 shader aliasing）|
| TAA | +3~8% | 不涨 | 几何 + shader + 闪烁（最广）| ghosting、需 velocity |

**FXAA 为什么便宜**：纯 2D 后处理，一个全屏 pass 读 1x scene color → 亮度梯度检测边缘 → 沿边缘加权混合。不碰 4x 采样、不碰 tile memory、不碰 resolve。三个硬伤：**全屏模糊**（糊纹理细节/小文字/UI）、**subpixel 细节差**（细电线/栅栏检测不到）、**无 temporal 稳定性**（帧间边缘跳动）。

**关键取舍变量——场景里有没有 TAA**：TAA 覆盖锯齿类型最广（几何 + specular + alpha test + 闪烁），成本固定不随几何涨，唯一代价是 ghosting。FPS 高速转视角下几何 static aliasing 没机会看清，但 specular 闪烁和 temporal 稳定性特别扎眼——**这恰恰是 MSAA 的盲区、TAA 的强项**。有成熟 TAA 实现时优先 TAA，MSAA 是"无 velocity/无 TAA"时的几何兜底。

## 十二、手游 AA 选型的三个特殊变量

照搬 PC 结论会错，手游有三个变量改权重：

1. **tile-based 架构 → MSAA 成本结构变了**：带宽免费，贵在 tile memory 翻倍 → **高端更值、低端更痛**，两极分化比 PC 更剧烈
2. **高 PPI（400+）→ AA 边际收益低**：几何锯齿本就不明显（MSAA 收益打折），FXAA 全屏模糊在小像素上也看不出来（代价被稀释）→ FXAA 兜底比 PC 更站得住
3. **功耗 + 机型碎片化 → 分档是刚需**：PC 可一刀切，手游必须分档，这是生存需求不是优化

**手游 TAA 的特殊坑**：velocity + 历史 buffer 的成本在移动端被放大；**ghosting 更严重**（转视角快 + 30fps 历史帧间隔大）。所以移动端 TAA 质量参差不齐，很多引擎做不好反而不如 FXAA。

**实际分档策略**：
```
高端机：4x MSAA（或 MSAA 主 + FXAA 兜底残留）
中端机：2x MSAA
低端机：FXAA 或关
```
TAA 只在"引擎有成熟移动端实现 + 场景 specular 闪烁严重"时才考虑，不是移动端默认。

## 十三、快速认知 Checklist

- [ ] MSAA 抗锯齿 = per-sample 覆盖/深度测试；per-pixel 着色 → 便宜
- [ ] MSAA 纹理只能 4x 写、1x 读；4 API 禁止 sampler 采样 MSAA 纹理
- [ ] `CreateTextureMSAA` = Target(4x) + Resolve(1x) 一对；非 MSAA 时 Resolve==Target
- [ ] color 用硬件自动 resolve（`FRenderTargetBinding(tex, resolve)`）；depth 用手动 pass（`AddResolveSceneDepthPass`）
- [ ] depth 必须 per-sample（4x），否则 MSAA 失效；render pass attachment 采样数必须一致
- [ ] `GRHISupportsDepthStencilResolve=false` 时 depth 不能绑 Resolve，否则 RHI.cpp:1803 ensure
- [ ] 4x depth 在移动端 memoryless 只 tile 内，DRAM 只落 1x（`bRequiresSceneDepthAux` 懒开关）
- [ ] outline 读深度用的是引擎现成 Depth.Resolve；本次加的 resolve 是 outline 输出的 4x→1x（供 BasePass/Water SSR 采样）
- [ ] ScreenShadowMaskTexture 是引擎标准 1x（LightRendering.cpp:2006 / ShadowRendering.cpp:2814）；阴影 Upsample 有 1x Depth.Resolve 才绑，无则不绑（不能回退 4x Target）；shader 自采深度所以不绑也等价
- [ ] fragment 生成单位是像素不是 sample；shading 是**精确 1x** 不是"平均不到 4x"；sample 间颜色共享、只 coverage/depth 独立
- [ ] sample 颜色写回时就定，1x 最终像素色 resolve 时才由平均产生；resolve = box filter，不产生新信息，只在边缘像素有实质意义
- [ ] 4x MSAA 实际带宽 1.3~2x（PC 压缩）/ 主存几乎不涨（tile 折叠），**不是 4x**；移动端开销大头是 tile memory 翻倍
- [ ] MSAA 盲区 = specular/normal/alpha-test 闪烁；FPS 转视角下优先 TAA（有成熟移动端实现时）
- [ ] 分档是手游刚需：高端 4x / 中端 2x / 低端 FXAA 或关

## 十四、落地背景（相关提交）

本项目本次改动：MSAA 兼容修复（r.MSAACount=1/2/4 角色渲染正常）已提交 **CL 1089429**（8 文件），关键文件：
- `MobileOutlinePrepearPass.cpp`：outline pass 输出 `FRenderTargetBinding(OutTargetTex, MobileCharFeatureTexture.Resolve, EClear)`；PreOutline DepthBinding 按 AA method 判 DepthRead；`SceneDepthTex = Depth.Resolve ? Resolve : DepthAux.Resolve`
- `MobileShadingRenderer.cpp`：3 处 `ScreenSpaceOutline = MobileCharFeatureTexture.Resolve`
- `SingleLayerWaterRendering.cpp`：Water SSR TAA 采样 `MobileOutlineTexture.Resolve`
- `DistanceFieldShadowing.cpp` / `ScreenSpaceShadows.cpp`：Upsample depth 绑定 1x Resolve（修 Android Vulkan samples mismatch ensure）
- `VelocityRendering.cpp`：`DepthPassCanOutputVelocity` 按 AA method 判断（r.MSAACount=1 → DDM_AllOpaque）
- `MobilePreOutline.usf`：depth bias 条件对齐 `bPreOutlineDepthRead`

## 十五、相关参考

- 引擎代码：`CreateTextureMSAA`（SceneTextures.cpp）、`AddResolveSceneDepthPass`（SceneRendering.cpp:7736）、`GRHISupportsDepthStencilResolve` ensure（RHI.cpp:1803）、`bRequiresSceneDepthAux`（SceneTextures.cpp:587/589）、`MobileOutlinePrepearPass.cpp`、`MobileShadingRenderer.cpp`、`MobileToonOutline.usf`、`SceneUtils.cpp`（GetDefaultAntiAliasingMethod/MSAACount）；ScreenShadowMaskTexture 创建 `LightRendering.cpp:2006` / `ShadowRendering.cpp:2814`，Upsample 自采深度 `ScreenSpaceShadows.usf:134/163`
- 相关文档：`E:\AiDoc\Mobile-Forward-MSAA-屏幕中心黑斑-PreOutline深度不一致.md`（同主题 bug 排查篇）
- 项目 memory：`scene-depth-resolve-can-be-null`、`msaa-black-blob-preoutline-depthwrite`、`mobile-preoutline-mixed-samples-crash`
