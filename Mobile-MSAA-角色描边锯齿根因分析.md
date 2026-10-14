# MSAA 下角色描边锯齿根因分析

> 分析对象：S1/GR 引擎 Mobile Toon 描边（PreOutline + Laplacian mask + BasePass 应用），
> 配置：Mobile Forward + `r.Mobile.AntiAliasing=3`（MSAA 4x）。
> 本文为**代码链路静态分析**（含行号证据）；运行时 AA 以当前编辑器实测为准（2026-08-19 实测 AA CVar 实际为 TAA/Console=2）。

---

## 一、结论速览

**MSAA 抗的是"几何轮廓锯齿"（三角形覆盖 + per-sample resolve 混合），而角色描边是"图像空间 mask 驱动的边缘"——从 mask 生成到应用全程没有 per-sample 差异化，MSAA 机制对描边边缘完全失效，锯齿原样保留。**

一句话：**MSAA 4x 平滑的是"角色模型的几何边缘"，描边黑线是后处理 mask 画的，mask 边缘的锯齿 MSAA 管不到。**

核心链条：
```
mask 数据源全 1x（Depth.Resolve + PreOutline Resolve）
  → 4x mask 每个像素 4 个 sample 值完全相同（空壳 4x）
  → resolve 无混合 → mask 硬边 0/1
  → BasePass 采 1x Resolve、逐对象 1x 精度应用
  → 描边边缘 1x 阶梯锯齿（MSAA 无能为力）
```

> **叠加性质（2026-08-18 用户实测）**：关光照 `showflag.lighting 0` 或关描边 `r.YHRP.EnableMobileOutlinePass 0`，**单独关一个仍有锯齿，两个都关才无锯齿** → 锯齿 = toon 明暗分界 AND 描边线叠加（缺一不可）。两者同为逐像素图像内容、同为 MSAA 机制盲区，见「根因 6」。

---

## 二、描边渲染链路（含 sample 状态）

| 环节 | 文件:行 | MSAA 下的 sample 状态 |
|---|---|---|
| SceneColor / 描边 RT 创建 | `SceneTextures.cpp:606` `Desc.NumSamples = Config.NumSamples`；`:615-619` mask 纹理复用同 Desc | **全 4x**（Target 4x + Resolve 1x） |
| Pass1 PreOutline：外扩写 `MobileOutlineTexture` | `MobileOutlinePrepearPass.cpp:857/877`；`MobilePreOutline.usf` NDC 外扩（只改 xy） | 写 **4x Target**，Resolve 1x |
| Pass2 Laplacian：读 depth + PreOutline → 产 mask | `MobileOutlinePrepearPass.cpp:686` `SceneDepthTex = Depth.Resolve`；`:914` `PreOutlineResolve = MobileOutlineTexture.Resolve`；`:672` 输出 4x Target + 1x Resolve | **输入全 1x**，输出空壳 4x |
| BasePass（Forward）应用：采 mask 拆 `.a` 涂黑 | `MobileShadingRenderer.cpp:2049/2446/2541` `ScreenSpaceOutline = MobileCharFeatureTexture.Resolve`；`MobileBasePassPixelShader.usf:498` Sample + `:1263-1269` `Color=lerp(Color,0,mask)` | 采 **1x Resolve**，逐对象 1x 精度 |

---

## 三、锯齿根因分解

### 根因 1：mask 数据源全 1x → 4x mask 是"空壳"，resolve 无平滑作用

- Pass2 Laplacian 的两个输入都是 1x Resolve：`Depth.Resolve`（`:686`，ZXB 修复，防 MSAA 深度分裂）和 `MobileOutlineTexture.Resolve`（`:914`）。
- mask 虽是 4x 纹理（4x Target 上光栅化全屏），但每个像素的 **4 个 sample 由相同的 1x 输入算出 → 4 sample 值完全相同**。
- MSAA resolve 的平滑本质是"每 sample 覆盖/颜色不同 → 混合成半色阶"。**sample 间无差异 → resolve 输出仍是 0/1 硬边 → 阶梯锯齿保留。**

### 根因 2：应用端采 1x Resolve + 逐对象光栅化 → 描边落点 1x 像素精度

- BasePass 绑定 `MobileCharFeatureTexture.Resolve`（1x，`:2049` 等），每像素单次采样。
- Forward 逐对象光照只光栅化原网格像素，描边色在 1x 分辨率下逐像素上色，边缘像素非黑即白 → 阶梯。
- MSAA 对"读纹理算出的颜色差异"无任何 per-sample 作用（coverage 判定与 mask 无关）。

### 根因 3（机制错配本质）：MSAA = 几何抗锯齿，描边 = 图像抗锯齿需求

| 维度 | MSAA 能做什么 | 描边需要的 |
|---|---|---|
| 作用对象 | 三角形几何轮廓（per-sample coverage + resolve） | 图像空间 mask 边缘（读纹理产生的色差） |
| 平滑机制 | sample 覆盖/颜色不同 → resolve 混合 | 边缘像素需半透明过渡或时序累积 |
| 对描边 | **无效**（mask 各 sample 相同，coverage 无意义） | 需要 mask 渐变（图像 AA）或 TAA（时序 AA） |

MSAA 4x 能平滑的是角色**几何轮廓**的锯齿（模型边缘），但描边黑线画在 resolve 前的场景上，其边缘锯齿属于**图像空间问题**，MSAA 的 coverage 机制天然管不到。

### 根因 4：Forward 外扩丢失加剧落点锯齿

- PreOutline 在 NDC 外扩（`MobilePreOutline.usf` 只改 xy），外扩区域不在原网格光栅化范围内 → Forward BasePass 采不到外扩那半圈 mask（内侧描边）。
- 描边只落在对象自身像素上，可用描边宽度被砍半，剩余的窄描边在 1x 精度下锯齿更明显。
- **外扩不产生额外边缘**：外扩只改 xy、z 与 prepass 逐位一致（外扩圈与角色本体同一深度平面，无深度梯度）；且 Pass2 采的 `Depth.Resolve` 在 full depth prepass 下于 PreOutline **之前**生成（`MobileShadingRenderer.cpp:1572 < 1633`），外扩写深度不进 Pass2 的 Laplacian。**内部描边来自各部件间的真实深度台阶**（手臂/躯干/衣褶/配饰等深度分离），不是外扩造成。

### 根因 5：无时序平滑（MSAA 无 TAA 能力）

- MSAA 不做帧间累积，mask 边缘的阶梯/抖动无时序平滑。
- 对比：TAA 的抖动采样 + 历史混合能平滑 mask 边缘的时序锯齿与闪烁——**这正是 TAA 下描边比 MSAA 平滑的原因**（当前编辑器实测 AA 是 TAA，描边走的就是这条）。

### 根因 6：明暗分界（光照）叠加贡献 —— 锯齿 = 光照 AND 描边缺一不可

描边之外，toon 角色的明暗分界本身也是逐像素硬切：

```hlsl
// ToonShadingCommon.ush:36-47
float GetToonDiffuseBRDF(float NoL, float ShadowFalloff)
{
    ToonBRDF = smoothstep(0.5f, 0.5f + ShadowFalloff, NoL);  // 过渡带=ShadowFalloff(小) → ~1px 硬切
}
```

- 这是 Forward 逐对象光照结果，**几何内部 coverage 全 1** → 4 sample 同值 → resolve 无混合 → 与描边**同源同错配**（MSAA 无效）。
- 黑色描边线 `Color=lerp(Color, 0, OutlineMask)`（`MobileBasePassPixelShader.usf:1263-1269`）与明暗分界在**剪影附近叠加** → "亮-暗-黑"高频交替 → 视觉锯齿被放大。

**用户逐项开关实测（2026-08-18）**：

| 操作 | 结果 |
|---|---|
| 单关光照 `showflag.lighting 0` | **仍有锯齿**（剩描边线贡献） |
| 单关描边 `r.YHRP.EnableMobileOutlinePass 0` | **仍有锯齿**（剩明暗分界贡献） |
| 两个都关 | **无锯齿** |

**对修复方向的直接影响**：纯 MSAA 下**只做描边 mask 渐变不够**——明暗分界那一半锯齿还在（实测单软化描边无效）。要纯 MSAA 收效，必须 **描边 mask 渐变 + 明暗分界 ramp 加宽同时落地**；或直接 TAA 兜底（对最终 1x 画面时序平滑，一次抹平两者）。

---

## 四、现状缓解（SoftenOutlineMask）与局限

> ⚠️ **已回退（2026-08-19）**：放弃 MSAA 软化方案，`SoftenOutlineMask` 已 `p4 revert`（单做无效——明暗分界那一半锯齿还在，见根因 6）。

`MobileToonOutline.usf:71-106`（ZXB 添加）对 mask 做了 ~2px 软化：

```hlsl
// 核心(mask>0.5)保持硬边；mask 外侧若近邻有深度跳变，用 4 邻域重算的平均做 ramp
if (InCenterMask > 0.5) return InCenterMask;      // 核心仍硬边
// ... bNearEdge 检测 → Sum/4 邻域平均 → ~2px 渐变
```

局限：
- **只在 mask 外侧做 2px 邻域平均**，核心边缘仍是 0/1 硬边；
- 2px ramp 在 1x 分辨率下不足以消除长边缘（斜 45° 长描边）的阶梯；
- 它是"宽度软化"，不是"逐像素抗锯齿"，对单帧静态锯齿的根治能力有限。

### 2026-08-19 已实施：消除 mask 空壳 4x（性能优化）

「根因 1」落地：`MobileCharFeatureTexture` 创建改 `NumSamples=1`（`SceneTextures.cpp:617`，ZXB region）。全屏 Pass2 输入全 1x（Depth.Resolve + PreOutline.Resolve），4x Target 是空壳 → 直接建 1x，省 4x 写带宽 + resolve pass。

- `CharFeatureDesc.NumSamples=1` + `CreateTextureMSAA`（`RenderGraphUtils.cpp:212-237`：NumSamples=1 时 Resolve==Target）
- 消费方全采 `.Resolve`（`MobileShadingRenderer.cpp:2049/2446/2541`、`SceneTextures.cpp:1635-1637`）→ Resolve==Target，行为不变；Pass2 `IsSeparate()`=false → 只绑 1x Target，无 resolve
- `MobileOutlineTexture` 保留 4x（PreOutline mesh 外扩有几何边缘 AA，非空壳）
- 编译 36s 进二进制（DLL mtime > 源码）；编辑器 MSAA 下画面验证描边正常无回归
- `MobileCharRenderMaskTexture` 走 Deferred（后处理消费），不在本次范围，待查同类空壳

---

## 五、修复方向建议

| 方向 | 做法 | 代价/评价 |
|---|---|---|
| **mask 边缘渐变（图像 AA）** | 对 `MobileCharFeatureTexture` 的描边 mask 做边缘过渡（基于距离的 1~2px 梯度，如 sigmoid/sobel 平滑），让 0/1 硬边变半透明过渡 | 一个后处理或 shader 内软化，直接消除阶梯；注意别糊掉描边锐度 |
| **TAA 时序平滑** | 启用 TAA（当前实际配置），时序抖动+历史混合平滑 mask 边缘；需配锐化（Unsharp）控"糊" | 当前已在跑；描边 + TAA 边缘更稳，但要保证描边 mask 参与历史混合/写 velocity |
| **扩大 SoftenOutlineMask** | ramp 从 2px 加宽到 3~4px + 增加沿边缘方向的多 tap | 便宜，但对长边缘阶梯仍有限 |
| **描边走后处理全屏 + 全屏 AA** | 描边应用从逐对象 BasePass 挪到后处理全屏（统一 F/D），配合全屏图像 AA | 根治 Forward 外扩丢失 + 1x 落点问题，但要加 1 个全屏 pass（移动端带宽账） |

> ⚠️ **关键认知**：只要描边仍是"图像空间 mask 驱动"，MSAA 4x 就对它无效——这是机制性错配，不是参数问题。要平滑描边，只能靠 **mask 渐变（图像 AA）** 或 **TAA（时序 AA）** 二选一（或组合）。

---

## 六、参考

- 代码：`SceneTextures.cpp:606-619`、`MobileOutlinePrepearPass.cpp:672/686/857/877/914`、`MobileShadingRenderer.cpp:396/1572/1633/2049/2446/2541`、`MobileBasePassPixelShader.usf:498/1263-1269`、`MobileToonOutline.usf:71-106`、`ToonShadingCommon.ush:36-47`、`MobilePreOutline.usf`
- 同源文档：`E:\AiDoc\UE-Mobile-MSAA-实现原理-Resolve机制与深度采样链路.md`（MSAA 机制/成本/选型）
- 同源文档：`E:\AiDoc\UE-Mobile-Toon描边-PreOutline深度偏移污染-MSAA角色涂黑与BasePass剔除修复.md`（PreOutline 深度一致性）
- 截帧证据：`D:\GR_DevTest\S1Game\Saved\RenderDocCaptures\2026.08.19-14.53.57_capture_3.rdc`（TAA pass 在 `PostProcessing→TAA→TAA(MainUpsampling)`）
