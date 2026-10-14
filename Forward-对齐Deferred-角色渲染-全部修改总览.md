# Forward 对齐 Deferred 角色渲染 — 全部修改总览

> S1Game Forward 管线开发中，为让卡通角色（toon character）渲染效果对齐 Deferred 基准，在 4 个 P4 CL 中完成的全部修改。修改文件：`MobileBasePassPixelShader.usf`、`MobileShadingRenderer.cpp`、`ToonShadingModels.ush`。

---

## 修改总表

| CL | 日期 | 标题 | 文件 |
|---|---|---|---|
| 1038584 | 2026-07-28 | Outline贴图Forward管线对齐 | MobileShadingRenderer.cpp |
| 1039327 | 2026-07-28 | BasePass对齐描边效果 | MobileBasePassPixelShader.usf |
| 1039871 | 2026-07-28 | 重复ApplyMobileToonCombineShadowColor去除 | MobileBasePassPixelShader.usf |
| 1067963 | 2026-08-06 | 前向角色效果对齐Deferred（综合） | MobileBasePassPixelShader.usf, ToonShadingModels.ush |

---

## 一、Outline 贴图 Forward 管线对齐（CL 1038584）

**文件**：`MobileShadingRenderer.cpp`

**问题**：Forward 路径缺少 `ScreenSpaceOutline` RT 传给 BasePass，导致 MobileBasePass 内 `MobileScreenOutline` 走 SystemTextures fallback——Forward 下 toon 描边数据为空白/黑色。

**修复**：在 Forward 的 BasePass 渲染前把 `SceneTextures.MobileCharFeatureTexture.Target` 填入 `MobileBasePassTextures.ScreenSpaceOutline`，与 Deferred `RenderDeferredSinglePass` (L2386-2387) 对齐。

```cpp
MobileBasePassTextures.ScreenSpaceOutline = SceneTextures.MobileCharFeatureTexture.Target;
```

---

## 二、BasePass 描边效果对齐（CL 1039327）

**文件**：`MobileBasePassPixelShader.usf`

**问题**：旧描边逻辑用 `dot(CharacterOutlineColor, 1)` 判边缘，但 `MobileToonOutline.usf` 输出是 `float4(SceneRimLight.r, SceneOutline.g, ToonRimLight.b, ToonOutline.a)`——三个独立遮罩求和当边缘值，语义错误。

**修复**：重写为对齐 Deferred 的描边采样（`MobileDeferredShading.usf:248-253 + 409-416`），从 `MobileCharacterOutline` RT 直接读 `.a` 通道（ToonOutlineMask）做黑色覆盖。

```hlsl
float2 OutlineUV = SvPositionToBufferUV(SvPosition);
float4 OutlineRT = MobileSceneTextures.MobileCharacterOutline.SampleLevel(
    MobileSceneTextures.MobileCharacterOutlineSampler, OutlineUV, 0);
float ToonOutlineMask = OutlineRT.a;
Color = lerp(Color, float3(0, 0, 0), ToonOutlineMask);
```

---

## 三、重复 CombineShadowColor 去除（CL 1039871）

**文件**：`MobileBasePassPixelShader.usf`

**问题**：方向光 lighting 后插入了一处 `ApplyMobileToonCombineShadowColor(DirectLighting.TotalLight, ...)` 直接覆盖 `TotalLight`，导致 Foliage 等非 Toon 材质 shader 编译必须解析 `ToonShading` 表达式而失败，且 Toon 角色下游（L1508+）还会再次调同一函数，造成双重调制。

**修复**：删除此处重复调用。Toon 的 CombineShadowColor 由下游 Color 线统一完成（L1536），Deferred 也只有一处（L403）。

---

## 四、综合对齐修复（CL 1067963）

本 CL 包含 6 项独立修复，整体将 Forward toon 角色 F/D 比值从 2.53× 收敛到 1.00×。

### 4.1 TOON_CUSTOMDATA_OVERRIDE 门控收窄

**问题**：原 `#if MATERIALBLENDING_TRANSLUCENT || FORWARD_SHADING || ...` 中的 `FORWARD_SHADING` 是项目级全局值（`!IsMobileDeferredShadingEnabled`），Forward 工程下 toon opaque 也走此块读取 `CustomData0=1.0`，Deferred 工程下同一材质跳过此块保留 GBuffer decode 的 `Opacity=0.5`——`CustomData.a` 两侧不一致。

**修复**：定义 `TOON_CUSTOMDATA_OVERRIDE_LOCAL` 宏，将 `FORWARD_SHADING` 收窄为 `FORWARD_SHADING && !MATERIALBLENDING_SOLID && !MATERIALBLENDING_MASKED`（仅 non-opaque），对齐 Deferred 行为。

### 4.2 TOON_DIFFUSE_SIMPLE 门控收窄（ToonShadingModels.ush）

**问题**：同根因——`#if MATERIALBLENDING_TRANSLUCENT || FORWARD_SHADING` 让 Forward 工程下 toon opaque 走了"简化式" diffuse（不乘 `ToonBRDF * CustomShadow`），而 Deferred 走了完整式。实测 Forward Diffuse=(0.5549,0.4727,0.4682) vs Deferred=(0,0,0)，ToonBRDF/CustomShadow/Falloff 两侧完全相同，差异 100% 来自 Forward 少乘了 ToonBRDF。

**修复**：定义 `TOON_DIFFUSE_SIMPLE_LOCAL` 宏，移动端路径收窄 `FORWARD_SHADING` 为 `FORWARD_SHADING && !MATERIALBLENDING_SOLID && !MATERIALBLENDING_MASKED`，PC 路径保持原样。

### 4.3 GBuffer encode/decode 往返（Forward 侧模拟量化）

**问题**：Deferred 的 lighting pass 拿到的 GBuffer 是经过 encode → RT → decode 的（途中有 ShadingModelID 5bit / ToonBufferA 8bit / ShadowColor 8bit / CustomData.a 6bit 等量化），Forward base pass 直接用内存原始 GBuffer——入参逐位不同。

**修复**：在 Forward 侧 `AccumulateDirectionalLightingMobileToon` 之前插入一次 `MobileEncodeGBuffer → MobileDecodeGBuffer` 往返，使 Forward 光照入参与 Deferred 逐位一致。仅在 `!MOBILE_USE_GBUFFER`（Forward）分支生效。

### 4.4 CameraVector 归一化

**问题**：Deferred 传给 `AccumulateDirectionalLightingMobileToon` 的是 normalize 过的向量，Forward 原来直接用 `-MaterialParameters.CameraVector`（未归一化）→ `NoV / EnvBRDF / dither` 等依赖单位向量的项两侧不一致。

**修复**：`half3 CameraVector = normalize(-MaterialParameters.CameraVector);`

### 4.5 StaticLightingMethod 条件加 toon 守卫

**问题**：Forward 非 toon 路径的 `View.StaticLightingMethod != LIGHTMAP_ONLY` 条件对 toon 也生效，导致 `bStaticLightingUseLightmap` 赋值路径与 Deferred 不同。

**修复**：条件改为 `#if !MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MOBILE_USE_GBUFFER`，toon 走 else 分支（LuxGI-only 条件），与 Deferred base pass 逻辑一致。

### 4.6 LuxGI 剥离三门加 `!TOON_CHARACTER` + Weight 时序提前

详见独立文档：`E:\AiDoc\Forward-LuxGI-对齐Deferred-两因素修复.md`

核心：
- 剥离三门前加 `!MATERIAL_SHADINGMODELS_TOON_CHARACTER`，让 toon LuxGI 留在 TotalLight 流过 CombinedShadowColor（消除 ~2.1× 差异）
- 方向光后注入 `DirectLighting.TotalLight *= 1+ToonEnergyWeight`，对齐 Deferred Weight 时序（消除 ~1.2× 差异）
- 末尾 toon 路径删除 `OutColor.rgb *= Weight`

---

## 五、效果验证

| 检查项 | 方法 |
|---|---|
| 描边效果 | 基准位姿截图，对比 Forward vs Deferred 的 toon 轮廓线，不再为全黑/全白 |
| CustomData.a | 取数 `R=0.000135`（FWD 槽 Forward/Deferred 一致）→ emissive 路径对齐 |
| GBuffer 量化 | encode/decode 往返后，Diffuse/Specular 入参与 Deferred 逐位一致 |
| CameraVector | DotProduct 相关项（NoV, EnvBRDF, dither）两侧一致 |
| LuxGI 亮度 | F/D 比值从 2.53× → 1.00×（基准位姿 toon 角色） |
| 非 toon 场景 | 非 toon 对象 `*PreExposure` 正常（检查 `#else` 分支未丢失） |
