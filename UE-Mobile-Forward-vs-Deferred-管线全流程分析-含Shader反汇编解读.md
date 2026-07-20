# UE5 Mobile Forward vs Deferred 渲染管线全流程分析

> 基于实机 Shader 反汇编 (HLSL/DXIL)、引擎 C++ 源码，逐函数分析了 Mobile Forward 和 Mobile Deferred 两条渲染路径的完整执行流程、GBuffer 编解码细节、Toon ShadingModel 的位压缩策略，以及两者在间接光照、CartoonShadow 参数绑定、Subpass 架构上的关键差异。

---

## 一、项目环境

| 项目 | 值 |
|------|-----|
| 引擎 | UE5.5.4 自编译分支 `++GR+DevTest` |
| 项目 | S1Game（GR 内部代号） |
| 平台 | Android (ES3.1) |
| Shader 来源 | RenderDoc D3D12 截帧反汇编 |
| 关键 CVar | `r.Mobile.ShadingPath=1` (Deferred) |

---

## 二、整体架构对比

```
┌─ Mobile Forward ────────────────────────────────────────────────┐
│  1 次 Base Pass Draw Call = 材质计算 + 方向光 + 间接光 + IBL     │
│  输出: 1 张 RT (SceneColor)                                      │
└──────────────────────────────────────────────────────────────────┘

┌─ Mobile Deferred ───────────────────────────────────────────────┐
│  Pass 1: Base Pass = 材质计算 → MobileEncodeGBuffer() → 写GBuffer│
│          输出: 4 张 RT (SceneColor + GBufferA/B/C)               │
│  Pass 2: Lighting Pass (全屏 Quad) = 解码GBuffer → 方向光+局部光 │
│          +GI+IBL → 加法混合到 RT0                                │
│          输出: 混合到 SceneColor                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 核心设计理念

| | Forward | Deferred |
|---|---|---|
| **材质计算** | 每个像素 1 次 | 每个像素 1 次 |
| **光照复杂度** | O(物体×灯光) | O(全屏像素×灯光) |
| **多灯光场景** | 开销线性增长 | 开销平摊 |
| **半透明** | 原生支持 | 需额外 Forward Pass |
| **MRT 带宽** | 低 (1 RT) | 高 (4 RT 写入 + 3 RT 采样) |
| **Subpass** | 无 | 有 (DeferredShadingSubpass) |

---

## 三、Deferred Base Pass — GBuffer 编码端

### 3.1 入口与输出

**文件**：`MobileBasePassPixelShader.usf` → `void Main()`

```hlsl
out min16float4 OutColor    : SV_Target0   // SceneColor (Emissive + Indirect)
out min16float4 OutGBufferA : SV_Target1   // Normal + ShadingModelID
out min16float4 OutGBufferB : SV_Target2   // Metallic/Specular/Roughness
out min16float4 OutGBufferC : SV_Target3   // BaseColor + AO
```

### 3.2 GBuffer 编码布局

#### DefaultLit (ShadingModelID=1) 标准布局

| 通道 | 内容 | 编码方式 |
|------|------|---------|
| GBufferA.rg | WorldNormal | Octahedron 编码 (3ch→2ch) |
| GBufferA.b | ID(4bit) + 0(6bit) | `/1023` |
| GBufferA.a | PerObjectGBufferData | 直接写入 |
| GBufferB.r | Metallic | 直接写入 |
| GBufferB.g | Specular | 直接写入 |
| GBufferB.b | Roughness | 直接写入 |
| GBufferB.a | SM_ID / 255 | 冗余备份 |
| GBufferC.rgb | BaseColor | 直接写入 |
| GBufferC.a | GBufferAO | 直接写入 |

#### Toon 系列 (ShadingModelID >= 11) 位压缩布局

| 通道 | 内容 | 位分配 |
|------|------|--------|
| GBufferA.b | ID(4bit) + ShadowFalloff(6bit) | 10bit → /1023 |
| GBufferB.a | ShadowOffset + CustomToonShadow | 各4bit → /255 |
| GBufferB.g | Specular + Roughness | 各4bit → /255 |
| GBufferB.b | ShadowColor.r + .g + .b | 3+3+2bit → /255 |
| GBufferC.a | ToonAO + Mask1 + Mask2 | 3+3+2bit → /255 |

**位压缩函数链**：
- `MobileEncodeIdAndColorChannel(Id, Color, b10Bits)` — ID(4bit) + Color(6/5bit) 拼 1 通道
- `MobileEncodeTwoCustomToonData(A, B)` — 2 个值各 4bit 拼 1 通道
- `MobileEncodeThreeCustomToonData(A, B, C)` — 3 个值 3+3+2bit 拼 1 通道

### 3.3 RT0 (OutColor) 的数值分析

在当前 Android 变体 (`ENABLE_SKY_LIGHT=0`) 中：

```
GetSkyLighting() → 全 0 (编译期裁剪)
GetPrecomputedIndirectLightingAndSkyLight() → DiffuseIndirect = 0
DiffuseColor = 0 * DiffuseColorForIndirect = 0
DirectLighting = 0 (Deferred 不在此算直接光)

Color = DirectLighting(0) + Emissive
OutColor = Emissive × VertexFog × PreExposure
```

**结论**：Deferred Base Pass 的 RT0 在当前变体中 ≈ Emissive。间接光恒为 0，直接光留给之后的全屏 Lighting Pass 加法叠加。

### 3.4 加法混合确认

在 `MobileDeferredShadingPass.cpp` 中，Deferred Lighting Pass 的 Blend State：

```cpp
// 方向光:
GraphicsPSOInit.BlendState = TStaticBlendState<CW_RGB, BO_Add, BF_One, BF_One>::GetRHI();

// 局部光:
GraphicsPSOInit.BlendState = TStaticBlendState<CW_RGB, BO_Add, BF_One, BF_One, ...>::GetRHI();
```

数学含义：
```
FinalColor = Src × 1 + Dst × 1 = LightingResult + BasePassRT0
           = (Direct + Indirect + GI + IBL) + (Emissive + Decals)
```

且通过 Subpass 架构（`ESubpassHint::DeferredShadingSubpass`），3 个 Subpass 共享同一组 RT：
```
Subpass 0: RenderMobileBasePass()    → 写 SceneColor + GBuffer
Subpass 1: RenderDecals()            → 写 SceneColor
Subpass 2: MobileDeferredShadingPass() → 写 SceneColor (加法混合)
```

### 3.5 顶点雾分析

`r.Mobile.DisableVertexFog` 默认值为 **1**（关闭），Base Pass 中的 `VertexFog` 恒为 `(0,0,0,1)`（恒等变换）。真正的雾由独立的 `RenderFog()` 全屏 Post-Process Pass 在 Deferred Lighting 之后完成。

---

## 四、Deferred Lighting Pass — GBuffer 解码 + 光照

### 4.1 入口

**文件**：`MobileDeferredShading.usf` → `void MobileDirectionalLightPS()`

```
全屏 Quad 每像素执行:
noperspective float4 UVAndScreenPos : TEXCOORD0
float4 SvPosition : SV_POSITION
out min16float4 OutColor : SV_Target0
```

### 4.2 执行流程

```
1. MobileFetchAndDecodeGBuffer(UV, ScreenPos)
   └─ Sample GBufferA/B/C → MobileDecodeGBuffer() → FGBufferData

2. 深度重建 → TranslatedWorldPosition

3. LightGrid Tile-based 局部光
   └─ ComputeLightGridCellIndex → GetCulledLightsGridHeader
   └─ 遍历 Tile 内灯光 → AccumulateDynamicToonLightingMobile()

4. SSAO 合成 → GBuffer.GBufferAO *= AO

5. AccumulateDirectionalLightingMobileToon()
   └─ GetMobileDynamicShadow() → CSM 阴影
   └─ AccumulateDynamicToonLightingMobile() → BRDF 计算
   └─ LightAccumulator_Add()

6. ComputeLightFunctionMultiplier() → LightFunction 遮罩

7. Toon Energy Weight / PreExposure

8. AccumulateLuxGILighting() → 间接 GI

9. RoughReflection * GetEnvBRDF() → IBL Specular

10. ApplyMobileToonCombineShadowColor() → Toon 阴影色/Ramp/AO

11. Outline 绘制

12. OutColor
```

### 4.3 解码函数链

- `MobileFetchGBuffer()` — 3 次贴图采样 + Depth 采样
- `MobileDecodeGBuffer()` — 完全镜像 `MobileEncodeGBuffer()`
  - `MobileDecodeId()` / `MobileDecodeColorChannel()` — 拆包 10bit 通道
  - `MobileDecodeTwoCustomToonData()` / `MobileDecodeThreeCustomToonData()` — 拆包位压缩
  - `GBufferPostDecode()` — 后处理（SpecularColor、DiffuseColor、ToonMainLight 解码等）
- `HasCustomGBufferData()` — 判断 SM 是否需要 CustomData 字段

---

## 五、Forward Base Pass — 单 Pass 完成全部光照

### 5.1 入口与输出

**文件**：`MobileBasePassPixelShader.usf` → `void Main()`（Forward 变体）

```hlsl
out min16float4 OutColor : SV_Target0   // 只有 1 张 RT，无 GBuffer
```

### 5.2 执行流程

```
1. CalcMaterialParametersEx() → 材质参数

2. SSAO 合成到 MaterialAO (早于光照计算)

3. SetGBufferForShadingModel() → 填充本地 FGBufferData (不写 RT!)

4. GetPrecomputedIndirectLightingAndSkyLight()
   └─ GetSkyLighting() → GetSkySHDiffuseSimple() ★ 实际采样 Sky SH，非零！
   └─ DiffuseColor = SkyIndirect * DiffuseColorForIndirect * AO
   └─ 写入 DirectLighting

5. AccumulateDirectionalLightingMobileToon() ★ 方向光在 BasePass 内计算
   └─ GetMobileDynamicShadow() + AccumulateDynamicToonLightingMobile()

6. [ZXB] 保存 ExposureAffectedLight = DirectLighting.TotalLight

7. AccumulateLuxGILighting() ★ 间接 GI 在 BasePass 内计算
   └─ GetLuxGIFullLightingWithNonCompressedData()
       ├─ Depth Visibility Check (漏光避免)
       ├─ SparseBrickPage + GlobalLux + FarGI 融合
       └─ Mip0→Mip1→Sky 三级渐变过渡
   └─ IndirectDiffuse *= PreExposure * AO
   └─ ApplyCartoonShadow() → 修改间接光 (Forward + Deferred 共享)
   └─ AccumulateToonIndirectLighting() → Toon 间接光 (Forward + Deferred 共享)
   └─ 写入 DirectLighting

8. [ZXB] LuxGIContribution = DirectLighting - ExposureAffectedLight
   DirectLighting = ExposureAffectedLight (回退)

9. AccumulateReflection() ★ IBL 镜面反射在 BasePass 内计算
   └─ GetImageBasedReflectionLighting_Mobile() → ReflectionCapture/SkyReflection
   └─ GetEnvBRDF(SpecularColor, Roughness, NoV)
   └─ 累加 SpecularIBL → DirectLighting

10. Color = DirectLighting + Emissive

11. OutColor = Color * Fog + LuxGIContribution * Fog (避免二次 PreExposure)

12. SafeGetOutColor() → 防超过 Max111110BitsFloat3 * 0.5
```

### 5.3 与 Deferred 的关键差异

#### SkyLight 间接光 — 编译期 Shader Permutation 控制

差异的根因不是运行时逻辑，而是**编译期 Shader Permutation**。

`MobileBasePassRendering.h` Line 497：
```cpp
OutEnvironment.SetDefine(TEXT("ENABLE_SKY_LIGHT"),
    bIsLit && bForwardShading && bProjectSupportsNonStaticSkyLights);
//              ↑ Deferred 时 bForwardShading = false → ENABLE_SKY_LIGHT = 0
```

注释（Line 441-442）也明确说明：
```cpp
// Deferred shading does not need SkyLight and LocalLight permutations
// TODO: skip skylight permutations for deferred
```

在 `MobileBasePassPixelShader.usf` 的 `GetSkyLighting()` 中（Line 245）：
```hlsl
#if ENABLE_SKY_LIGHT     // Deferred 编译时 = 0，整个 block 被预处理器裁掉
    OutSkyDiffuseLighting = GetSkySHDiffuseSimple(WorldNormal) * SkyLightColor;
    // ← 只有 Forward 变体能编译到这里
#endif
```

| | Deferred Base Pass | Forward Base Pass |
|---|---|---|
| `ENABLE_SKY_LIGHT` | 0（编译期剔除） | 1（编译通过） |
| `GetSkyLighting()` | 函数体全置 0 | 实际调用 `GetSkySHDiffuseSimple()` |
| 间接漫反射 | 0（全部由 LuxGI 在 Lighting Pass 提供） | 取决于 `StaticLightingMethod` |

#### ZXB 补丁已对齐大部分场景

在 `MobileBasePassPixelShader.usf` Line 976-998：
```hlsl
#if !MOBILE_USE_GBUFFER       // Forward
    if (View.StaticLightingMethod != STATIC_LIGHTING_LIGHTMAP_ONLY)
#else                          // Deferred
    if (View.StaticLightingMethod == STATIC_LIGHTING_LUXGI_ONLY)
#endif
    { /* skip GetPrecomputedIndirectLightingAndSkyLight */ }
```

| StaticLightingMethod | Forward 是否调用 GetPrecomputed | Deferred 是否调用 |
|---------------------|------------------------------|-------------------|
| LIGHTMAP_ONLY (0) | ✅ 调用 → SkyLight SH **有值** | ✅ 调用 → 但 `ENABLE_SKY_LIGHT=0`，返回 0 |
| HYBRID (2) | ❌ 跳过 → 0 | ✅ 调用 → 返回 0 |
| LUXGI_ONLY (3) | ❌ 跳过 → 0 | ❌ 跳过 → 0 |

**默认 `r.Mobile.StaticLightingMethod=2` (HYBRID) 时，ZXB 补丁已让两者都输出 0，效果一致。**
仅在切到 `LIGHTMAP_ONLY=0` 时 Forward 会比 Deferred 多一份 SkyLight 间接漫反射。

#### CartoonShadow 参数来源

#### ApplyCartoonShadow — 两条路径都跑（共享函数）

`ApplyCartoonShadow()` 在共享的 `AccumulateLuxGILighting()` 内部，**两个 Pass 都调用**，核心逻辑相同。差异仅在：

| | Deferred (Lighting Pass) | Forward (Base Pass) |
|---|---|---|
| 开关变量 | `bUseCartoonShadow` (loose 全局) | `MobileBasePass_MobileForwardUseCartoonShadow` (UB 字段) |
| Shadow 参数 | `ShadowColor`/`ShadowAOColor` 等 loose 全局 | `MobileBasePass_MobileForwardShadowColor` 等 UB 字段 |
| ShadowBorder 边缘融合 | ✅ 有额外的平滑过渡代码 | ❌ 无 |
| 后续额外函数 | `ApplyMobileToonCombineShadowColor()` 后期最终色 | 无 |

> 注：Deferred 在 `AccumulateLuxGILighting()` 修完间接光后，主函数末尾还有一次 `ApplyMobileToonCombineShadowColor()` 对最终合成色做 Toon 阴影色/Ramp/AO。Forward 没有这个第二步。

#### LuxGI + PreExposure (ZXB 补丁，仅 Forward 有此分离逻辑)

> `IndirectDiffuse *= PreExposure * AO` 这一步在共享的 `AccumulateLuxGILighting()` 内部，两条路径都会执行。区别在于 Forward 主函数中额外有 ZXB 分离逻辑，避免 LuxGI 二次乘 PreExposure。

```hlsl
// Forward 独有的 ZXB 分离逻辑：
// AccumulateLuxGILighting() 内部 IndirectDiffuse *= PreExposure
// 但后续 OutColor 全体又乘了一次 PreExposure
// 因此分离 LuxGI 贡献，避免二次乘

ExposureAffectedLight = DirectLighting;          // 方向光 + SkyIndirect
AccumulateLuxGILighting(...);                    // 内含 *PreExposure
LuxGIContribution = DirectLighting - ExposureAffectedLight;
DirectLighting = ExposureAffectedLight;           // 回退

OutColor = DirectLighting * Fog * PreExposure    // 非 LuxGI 部分
         + LuxGIContribution * Fog;              // LuxGI 不再乘 PreExposure
```

#### Toon 间接光照 — 两条路径共享

`AccumulateToonIndirectLighting()` 也在共享的 `AccumulateLuxGILighting()` 内部，**两个 Pass 都调用**（对 `ShadingModelID >= 11`）：

- `ToonEnergyWeight` → `IndirectWeight = 1 - EnergyWeight`
- GI 去饱和度（Lumen Desaturate）
- `ToonIndirectDiffuse = lerp(ToonDiffuse, GI*BaseColor, LumenWeight) * IndirectWeight`
- `ToonIndirectSpecular = clamp(RoughReflection, Min, Max) * SpecularMask * EnvBRDF`

因此 `AccumulateToonIndirectLighting`、`ApplyCartoonShadow`、`ApplyCartoonFoliage` 都是共享的 `AccumulateLuxGILighting()` 内部逻辑，两条路径完全一致。

---

## 六、所有关键函数速查表

### 公共函数（两个管线共用）

| 函数 | 位置 | 作用 |
|------|------|------|
| `CalcMaterialParametersEx()` | Pixel Shader | 运行材质图表，输出 `FPixelMaterialInputs` |
| `SetGBufferForShadingModel()` | Pixel Shader | 填充 `FGBufferData` 元数据 |
| `ComputeF0()` | UE 标准库 | `F0 = lerp(0.08*Specular, BaseColor, Metallic)` |
| `ApplyBentNormal()` | Pixel Shader | AO + Bent Normal 修正 |
| `AOMultiBounce()` | Pixel Shader | AO 多重弹射近似 |
| `GetPrecomputedIndirectLightingAndSkyLight()` | Pixel Shader | 预计算间接光 + SkyLight |
| `GetSkyLighting()` | Pixel Shader | Sky SH 球谐漫反射 |
| `AccumulateDynamicToonLightingMobile()` | ToonDeferredLightingCommon.ush | BRDF 核心分派器 |
| `IntegrateBxDFMobile()` / `EvaluateBxDFToon()` | ToonDeferredLightingCommon.ush | Toon BRDF 集成 |
| `LightAccumulator_Add()` / `_AddSplit()` | Base Pass | 光照累加器 |
| `GetMobileDynamicShadow()` | Pixel Shader | CSM 阴影采样 |
| `GetEnvBRDF()` | UE 标准库 | 环境 BRDF (Split-Sum) |
| `EnvBRDFApproxFullyRough()` | Pixel Shader | Fully Rough IBL 近似 |
| `GetMobileSkyLightReflection()` | Pixel Shader | SkyReflection 采样 |
| `GetToonDiffuseBRDF()` | ToonDeferredLightingCommon.ush | 阶梯/渐变 BRDF |
| `AccumulateLuxGILighting()` | Pixel Shader | LuxGI 间接光照集成（内含 ApplyCartoonShadow + ApplyCartoonFoliage + AccumulateToonIndirectLighting，两条路径共享） |
| `ApplyCartoonShadow()` | AccumulateLuxGILighting 内部 | CartoonShadow 修改间接光（两条路径共享） |
| `AccumulateToonIndirectLighting()` | AccumulateLuxGILighting 内部 | Toon 间接光 Desaturate+Lumen（两条路径共享） |
| `ApplyCartoonFoliage()` | AccumulateLuxGILighting 内部 | Foliage Shadow + ColorDesaturation（两条路径共享） |
| `SafeGetOutColor()` | Pixel Shader | 最终颜色 clamp 到 Max111110BitsFloat3 * 0.5 |

### Deferred 独有函数

| 函数 | 作用 |
|------|------|
| `MobileEncodeGBuffer()` | 将 FGBufferData 压缩编码为 3 张 RT |
| `MobileEncodeIdAndColorChannel()` | ID(4bit) + Color(6bit) 合入 1 通道 |
| `MobileEncodeTwoCustomToonData()` | 2 个值各 4bit 拼 1 通道 |
| `MobileEncodeThreeCustomToonData()` | 3 个值 3+3+2bit 拼 1 通道 |
| `MobileFetchAndDecodeGBuffer()` | 采样 3 张 GBuffer + 解码 |
| `MobileFetchGBuffer()` | 3 次贴图采样 |
| `MobileDecodeGBuffer()` | GBuffer 完全解码 |
| `MobileDecodeId()` / `MobileDecodeColorChannel()` | 拆包 10bit 通道 |
| `MobileDecodeTwoCustomToonData()` | 1 通道拆为 2 个 4bit 值 |
| `MobileDecodeThreeCustomToonData()` | 1 通道拆为 3 个 (3+3+2bit) 值 |
| `HasCustomGBufferData()` | 判断 SM 是否需要 CustomData |
| `GBufferPostDecode()` | 解码后处理 (SpecularColor/DiffuseColor 派生) |
| `AccumulateLightGridLocalLightingToon()` | Tile-based 局部光累加 |
| `ComputeLightGridCellIndex()` | 根据屏幕坐标+深度定位 Grid Cell |
| `GetCulledLightsGridHeader()` | 获取 Tile 灯光列表头 |
| `ApplyMobileToonCombineShadowColor()` | Toon 阴影色/Ramp/Exposure 混合到最终颜色 |
| `ComputeLightFunctionMultiplier()` | LightFunction 遮罩 |
| `GetLightFunctionColor()` | 采样 LightFunction 材质 |

### Forward 独有函数

> 注：`ApplyCartoonShadow()`、`AccumulateToonIndirectLighting()`、`ApplyCartoonFoliage()` 在共享的 `AccumulateLuxGILighting()` 内部，两条路径均调用，不属于独有。

| 函数 | 作用 |
|------|------|
| `AccumulateReflection()` | IBL 镜面反射封装（封装了 ReflectionCapture + PlanarReflection；Deferred 用内联写法等效） |
| `GetImageBasedReflectionLighting_Mobile()` | SkyReflection / ReflectionCapture IBL |
| `GetPlanarReflectionbasedReflectionLighting_Mobile()` | 平面反射 IBL |

---

## 七、C++ 调度链路

### 7.1 路径选择

`MobileShadingRenderer.cpp`：
```cpp
bDeferredShading = IsMobileDeferredShadingEnabled(ShaderPlatform);
// 由 r.Mobile.ShadingPath 控制

if (bDeferredShading) {
    RenderBasePass(...);            // Subpass 0
    RenderDeferredSinglePass(...);  // Subpass 2 (或 RenderDeferredMultiPass)
    // 内部调用 MobileDeferredShadingPass()
    //   → RenderDirectionalLights() (方向光全屏 Quad)
    //   → RenderLocalLight() (局部光 Per-Light Draw)
}
```

### 7.2 关键 CVar

| CVar | 默认值 | 作用 |
|------|--------|------|
| `r.Mobile.ShadingPath` | 0 | 0=Forward, 1=Deferred |
| `r.Mobile.DisableVertexFog` | 1 | 禁用 Opaque 顶点雾，改用独立 Fog Pass |
| `r.Mobile.UseClusteredDeferredShading` | 0 | 启用 LightGrid Clustered 光照 |
| `r.Mobile.UseLightStencilCulling` | 1 | 局部光 Stencil 裁剪 |
| `r.Mobile.DeferredLightingSplitPass` | 0 | 按 SM 分组 Split Pass (1=启用) |

---

## 八、关键避坑总结

1. **Deferred Base Pass 的 RT0 在当前 Android 变体中 ≈ Emissive 而已**。Indirect=0，Direct=0（留给 Lighting Pass 加法叠加）。

2. **SkyLight 差异是编译期 Shader Permutation，不是运行时逻辑**。`ENABLE_SKY_LIGHT` 由 `MobileBasePassRendering.h:497` 控制，只在 `bForwardShading=true` 时设为 1。Deferred 的 Base Pass 编译时没有 SkyLight SH 代码路径。

3. **默认配置下（HYBRID）ZXB 补丁已对齐 Forward 和 Deferred**：`StaticLightingMethod=2` 时 Forward 也跳过 SkyLight SH。仅在切到 `LIGHTMAP_ONLY=0` 时两边不一致。

4. **CartoonShadow 参数在两个路径走不同绑定**：
   - Deferred → `FCartoonShadowParameters` (loose 全局) / `ApplyMobileToonCombineShadowColor()` 处理最终色
   - Forward → `MobileBasePass_MobileForward*` (UB 字段) / `ApplyCartoonShadow()` 处理间接光（共享函数）
   - `ApplyCartoonShadow()`、`AccumulateToonIndirectLighting()`、`ApplyCartoonFoliage()` 均在共享的 `AccumulateLuxGILighting()` 内部，两条路径均调用。

5. **LuxGI 的 PreExposure 处理**：`IndirectDiffuse *= PreExposure * AO` 在共享函数内两条路径都执行。Forward 额外通过 ZXB 补丁分离 LuxGI 贡献，避免主函数末尾二次乘 PreExposure。

6. **GBuffer 位压缩有精度损失**：Toon 数据各通道精度为 4/4bit、3/3/2bit、3/3/2bit，极端情况下可能出现量化 banding。

7. **顶点雾在 Base Pass 恒为恒等变换** (`r.Mobile.DisableVertexFog=1`)，真正的雾由独立 `RenderFog()` Pass 处理。

---

## 九、相关文件索引

| 文件 | 路径 |
|------|------|
| MobileDeferredShadingPass.cpp | `UE5EA/Engine/Source/Runtime/Renderer/Private/` |
| MobileBasePassRendering.h/cpp | `UE5EA/Engine/Source/Runtime/Renderer/Private/` |
| MobileShadingRenderer.cpp | `UE5EA/Engine/Source/Runtime/Renderer/Private/` |
| MobileDeferredShading.usf | `UE5EA/Engine/Shaders/Private/` |
| MobileBasePassPixelShader.usf | `UE5EA/Engine/Shaders/Private/` |
| MobileBasePassCommon.ush | `UE5EA/Engine/Shaders/Private/` |
| ToonDeferredLightingCommon.ush | `UE5EA/Engine/Shaders/Private/` |
| MobileFogRendering.cpp | `UE5EA/Engine/Source/Runtime/Renderer/Private/` |
