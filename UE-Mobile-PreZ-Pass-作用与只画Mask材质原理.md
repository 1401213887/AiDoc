# UE Mobile PreZ Pass 作用与「只画 Mask 材质」原理

> 移动端 PreZ Pass（Early-Z / 深度 prepass）是 base pass 之前的一趟**只写深度、不写颜色**的 pass，由 `r.Mobile.EarlyZPass=2` 开启时**只画 Masked（alpha-test）材质**。它的底层逻辑是把「alpha test 会破坏 TBDR HSR」的掩膜几何提前固化成纯 opaque 深度，让 base pass 的早期 Z 重新生效，砍掉植被/栅栏类 masked 物件最重的 overdraw。

---

## 一、PreZ Pass 是什么

UE5 移动端在 base pass 之前可选跑一趟深度预写，官方注释原文（`RendererScene.cpp:138`）：

```
r.Mobile.EarlyZPass
  Whether to use a depth only pass to initialize Z culling for the mobile base pass.
  0: off
  1: all opaque
  2: masked primitives only
```

三个档位：

| 值 | 语义 | 对应 `EDepthDrawingMode` |
|---|---|---|
| 0 | 关 | `DDM_None` |
| 1 | 全 opaque 预写 | （走 `MobileUsesFullDepthPrepass` 时映射为 `DDM_AllOpaque`） |
| 2 | **只画 Masked 材质** | `DDM_MaskedOnly` |

「PreZ 只画 Mask 材质」指的就是 `=2` 这个档位。注意它和 `MobileUsesFullDepthPrepass → DDM_AllOpaque` 是**两条独立路径**（见 §五）。

## 二、代码链路（从 CVar 到 mesh processor）

完整调用链，逐层可查：

1. **CVar 定义**：`RendererScene.cpp:138`，`r.Mobile.EarlyZPass`，`ECVF_ReadOnly`（改需重启编辑器）。
2. **模式判定**：`RendererScene.cpp:5102 GetEarlyZPassMode()`，mobile 分支（`5137`）默认 `DDM_None`，`MobileEarlyZPass==2` 时置 `DDM_MaskedOnly`。
3. **渲染器标记**：`MobileShadingRenderer.cpp:396`：
   ```cpp
   bIsMaskedOnlyDepthPrepassEnabled = Scene->EarlyZPassMode == DDM_MaskedOnly;
   ```
4. **入口**：`MobileShadingRenderer.cpp:1148 RenderMaskedPrePass()`，仅当上面的标记为真才调 `RenderPrePass`：
   ```cpp
   void FMobileSceneRenderer::RenderMaskedPrePass(FRHICommandList& RHICmdList, const FViewInfo& View)
   {
       if (bIsMaskedOnlyDepthPrepassEnabled)
       {
           RenderPrePass(RHICmdList, View, &DepthPassInstanceCullingDrawParams);
       }
   }
   ```
5. **mesh 过滤（核心）**：`DepthRendering.cpp:2266`，`FDepthPassMeshProcessor::TryAddMeshBatch` 里按 `EarlyZPassMode` 决定哪些 mesh 进 prepass：
   ```cpp
   case DDM_MaskedOnly:
       bMatchEarlyZPassMode = BlendMode == BLEND_Masked;
       break;
   ```
   只有 `BLEND_Masked` 的 mesh 才被加入这个 pass。

## 三、为什么「能只画 Mask 材质」

分两层：**机制层**（怎么做到只画 Mask）和**收益层**（为什么 Mask 是最该 prepass 的那类）。

### 机制层：mesh processor 直接过滤

- `DDM_MaskedOnly` 下 `bMatchEarlyZPassMode = (BlendMode == BLEND_Masked)`，硬过滤掉非 masked 材质。
- `DepthRendering.cpp:2181 ShouldRender()` 用 `Material.WritesEveryPixel(...)` 做二分：
  - **完全 opaque**（每个像素都写、无 mask）：可用 position-only / null-PS 的极廉价 shader 预写深度。
  - **masked**（`!WritesEveryPixel`，需 alpha test）：必须跑真实材质采样 opacity mask。

### 收益层：alpha test 破坏 TBDR HSR，opaque 不需要 prepass

这是「只画 Mask」成立的**底层逻辑**：

- **普通 opaque 材质**：TBDR（Adreno/Mali/PowerVR）硬件自带 HSR（Hidden Surface Removal），前向后排序 + 早期 Z 已经能控制 overdraw。再全量扫一遍几何写深度是**净带宽开销**，所以 `=1`（全 opaque）不是默认选择。
- **masked（alpha-test）材质**：它的「洞」只有跑完 PS 采样 opacity mask 才知道覆盖，**这正好破坏 TBDR 的 HSR**——一个片元 alpha test 失败，GPU 得把后面那个片元重新拉出来着色，early-Z 失效。而这类材质（植被叶片、栅栏、铁丝网、头发片）大面积互相叠加，是 overdraw 最重的源。

所以 PreZ 只画 Mask 是「把成本花在最该花的地方」：masked 材质反正不能用 position-only 白嫖，本来就要跑真实 shader 采样 mask，那就顺带把它们的覆盖固化成纯 opaque 深度。

## 四、收益

一趟「只采样 mask + alpha test、不计算光照/颜色」的廉价 pass，把 masked 对象的覆盖提前固化成 opaque 深度，base pass 再跑时：

- TBDR 早期 Z 重新生效，被遮挡片元在**昂贵的光照/材质 PS 之前**被剔除；
- masked 物件（植被/栅栏类）重叠带来的 overdraw 显著下降。

> 注：量化收益（overdraw 下降百分比、帧率提升）需实测，本文不臆测数值。识别方法参考：PreZ 在截帧里表现为「几何与 base pass 精确配对 + PS 指令骤降」，因为复用同一份 shader 但 `colorWriteMask=0`（只写深度）。

## 五、与 Full Depth Prepass 的区分（避免混淆）

`r.Mobile.EarlyZPass=2` 的 masked-only prepass 和 fork 里 `MobileUsesFullDepthPrepass → DDM_AllOpaque`（`RendererScene.cpp:5147`，easonjiang 加的 full prepass）是**两条独立路径**：

| | Masked-only PreZ (`=2`) | Full Depth Prepass |
|---|---|---|
| 目的 | 减少 masked 物件 overdraw | 为深度消费方提供全 opaque 深度 |
| 消费方 | base pass 早期 Z | toon outline / HZB / SSXR / SceneDepth 节点 |
| 触发 | `r.Mobile.EarlyZPass=2` | `MobileUsesFullDepthPrepass(Platform)` |

别把两者当成一回事。

## 六、关键代码位置速查

| 位置 | 作用 |
|---|---|
| `RendererScene.cpp:138` | `r.Mobile.EarlyZPass` CVar（0/1/2，ReadOnly） |
| `RendererScene.cpp:5102` | `GetEarlyZPassMode()` 模式判定 |
| `RendererScene.cpp:5137` | mobile 分支：`==2 → DDM_MaskedOnly` |
| `RendererScene.cpp:5147` | `MobileUsesFullDepthPrepass → DDM_AllOpaque`（另一条路径） |
| `MobileShadingRenderer.cpp:396` | `bIsMaskedOnlyDepthPrepassEnabled` |
| `MobileShadingRenderer.cpp:1148` | `RenderMaskedPrePass()` 入口 |
| `DepthRendering.cpp:2181` | `ShouldRender()`：`WritesEveryPixel` 二分 |
| `DepthRendering.cpp:2266` | `TryAddMeshBatch()`：`DDM_MaskedOnly → BLEND_Masked` 过滤 |

## 七、相关参考

- 存量笔记（TBDR 取舍视角，含 PrePass/Subpass 冲突、iOS 免 PrePass 决策）：`E:\AiDoc\UE_Mobile_TBDR_查漏补缺增补卷.md`
  - 点 30：Masked 植被仍需 Early-Z（HSR 不剔 AlphaTest）
  - 点 61：iOS Opaque 免 PrePass（HSR 硬件隐面剔除）
  - 点 63：和平精英 Opaque 不做 PrePass
  - §4：`r.Mobile.EarlyZPass` 与 subpass 深度 fetch 冲突、`FORCE_DEPTH_TEXTURE_READS` 变体
- 外部实践：UE Mobile: Prepass Or Not?（`FORCE_DEPTH_TEXTURE_READS` / `IS_MOBILE_DEPTHREAD_SUBPASS`）：https://www.blurredcode.com/2025/03/239ae6a3
