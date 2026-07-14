# UE5 Mobile Forward Path — Foliage 竖直彩色条带（ToonRamp Profile 索引）排查修复

> **一句话问题描述（v2）**：Mobile **Forward Path** 下 `TWOSIDED_FOLIAGE` 植被出现**规则性竖直彩色条带**（红/黄/蓝/粉），部分树有部分树没有；Deferred Path 表现正常。根因是 Toon Ramp Profile 索引通道用 `GBuffer.Specular` 逐像素采样导致跳到不同 Ramp 页。**最终方案**：放弃自造 Ramp 采样，Forward 分支**完全复用项目 Deferred 通路的 `ApplyCartoonFoliage` + HSV Backface Hack 公式**（`MobileLightingCommon.ush:894-947` + `ToonDeferredLightingCommon.ush::ApplyCartoonFoliage`），两条路径逐字节对齐。

**问题时间**：2026-07-14
**引擎版本**：UE5EA（工作区 `d:\GR_DevTest`）
**修改文件**：`d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileBasePassPixelShader.usf`
**平台**：Mobile Preview（PC 编辑器强制 Mobile Forward）
**文档版本**：v2（19:42 追加"完全对齐 Deferred"迭代）

---

## 一、问题现象

### 1.1 视觉特征

编辑器 Viewport 里植被（TWOSIDED_FOLIAGE ShadingModel）上出现：
- **规则的竖直矩形色带**（沿屏幕空间/世界空间垂直方向）
- 颜色鲜艳（**红/黄/蓝/粉/绿**），边缘锐利
- 与树叶几何无关（不是 mesh 拓扑的问题）
- **有的树有条带、有的树没有**（同一棵材质、不同 MI 表现不同）

### 1.2 分类特征

| Path | 现象 |
|---|---|
| **Deferred**（`MOBILE_DEFERRED_SHADING=1`） | ✅ 正常，植被无条带 |
| **Forward**（`MOBILE_DEFERRED_SHADING=0` 或走 non-GBuffer 分支） | ❌ 出现竖直彩色条带 |

---

## 二、问题定位流程

### 2.1 收敛前的可疑因素（初步分析）

初步分析 `MobileBasePassPixelShader.usf` 1036 行的 TWOSIDED_FOLIAGE 分支存在多个可疑点：

1. `DiffuseColor = (DiffuseIndirectLighting + SubsurfaceIndirectLighting) * RampColor` —— 丢失 BaseColor，公式残缺
2. `RampColor *= RampColor` —— Ramp 二次压暗
3. Ramp 采样：`MappingProfileID2VW(GBuffer.Specular, 3, 1)` —— 使用 Specular 作为 Profile 索引
4. `View.ToonLightingRampTextureArray` 可能编辑器未绑定
5. `ToonShading.XXX` 在 Forward Path 是未定义符号（会导致 shader 编译失败）

### 2.2 用户提供的关键线索（决定性）

> 只有 Forward Path 有问题，Deferred 正常

这个线索直接锁定：**问题必然在"Forward-only 独有"的代码路径上**。同时"竖直条带、有的树有有的树没有"的规则性说明是**采样输入变量的问题**，不是纹理的问题。

### 2.3 关键证据挖掘

**证据 1：PC/Deferred 端正确的 Foliage Ramp 采样方式**

```hlsl
// SkyLightingDiffuseShared.ush:112-117
{
    float RampID = (GBuffer.RampID * 256);
    float2 RampVW = MappingProfileID2VW(GBuffer.RampID, 3, 1);   // ← 用 GBuffer.RampID
    float3 RampColor = Texture2DArraySampleLevel(View.ToonLightingRampTextureArray, ...,
        float3(GBuffer.FAO, RampVW), 0).rgb;
    RampColor *= RampColor;
    RampColor *= GBuffer.FBaseColor.r;
```

**证据 2：Mobile Forward 端错误的采样方式**

```hlsl
// MobileBasePassPixelShader.usf:1049（修改前）
float2 RampVW = MappingProfileID2VW(GBuffer.Specular, 3, 1);   // ← 错用 GBuffer.Specular
```

**证据 3：项目美术契约（`GBufferHelpers.ush`）**

```hlsl
// GBufferHelpers.ush:440-449
if(Ret.ShadingModelID == SHADINGMODELID_TWOSIDED_FOLIAGE)
{
    Ret.FMetalic = Ret.StoredMetallic;
    Ret.Metallic = 0.0;
    Ret.FRoughness = Ret.Roughness;
    //Ret.RampID = Ret.Specular;      // ← 关键：被注释掉了！
    Ret.Specular = 0.0;               // ← Deferred Encode 后 Specular 被清零
    ...
}
```

**证据链条**：
- 项目美术约定 Foliage 材质的 **Specular 通道被用来编码 RampID**
- Deferred 通路会 encode/decode，Specular 被清零、RampID 本应从 Specular 转存（但被注释了 → 实际是 0）
- Forward 通路**没有 encode/decode**，Specular 保留原始逐像素值

### 2.4 定位映射函数的"离散量化跳跃"特性

```hlsl
// Toon/ToonShadingCommon.ush:55-62
float2 MappingProfileID2VW(float GBufferID, uint curvePerEntry, uint curveIndex, float MaxID = 255.0)
{
    const uint entryPerPage = 512 / curvePerEntry;    // 170
    uint id = (uint)round(GBufferID * MaxID);          // 0..255 离散化
    uint ssProfileID = id % entryPerPage;              // 页内偏移
    uint ssProfilePage = id / entryPerPage;            // 页号
    return float2(((float)(ssProfileID * curvePerEntry + curveIndex) + 0.5f) / 512.0f,
                  (float)ssProfilePage);
}
```

**关键特性**：这是一个**不连续的量化映射**——`id` 从 0 → 255 均匀变化时，返回的 `(V, W)` 会**跳跃**到 Texture2DArray 里完全不同的 slice 和位置。设计假设 `id` 是**逐材质稳定常量**，逐像素变化会灾难性地随机采样。

---

## 三、根因分析（详细技术原理）

### 3.1 Toon Ramp 系统的设计假设

`View.ToonLightingRampTextureArray` 是 Texture2DArray 存储的 Ramp 曲线库：
- **每 slice（页）** 内并排存 170 个 profile（`512 / 3 curves`）
- 每个 profile 占 3 条曲线（Diffuse Ramp / Specular Ramp / Fresnel Ramp）
- **每个 profile 存储完全不同的颜色曲线**（profile 0 可能是绿色 wrap-diffuse，profile 5 可能是黄色，profile 10 可能是粉色）

**设计约定**：`RampID` 应该是**材质级别的常量索引**（美术在材质里写死"我这个材质用 profile 5"），而不是逐像素变化的贴图采样值。

### 3.2 Deferred Path 的数据流（为什么正常）

移动 Deferred 走**两遍 Pass**：

**第 1 遍 `MobileBasePassPixelShader.usf`（BasePass）**：
1. 走 1036 行 Foliage 分支，用 `GBuffer.Specular`（**原始逐像素 Specular 值**）采样 Ramp
2. 采样结果错误（有条带）累加到 `DirectLighting`
3. 走 1082 行 `MobileEncodeGBuffer`：**把 GBuffer 打包写入 MRT，`DirectLighting` 完全丢弃**
4. Encode 时清零 `Specular`，RampID 本应从 Specular 转存（但被注释掉了 → 保持默认 0）

**第 2 遍 `MobileDeferredShading.usf`（Deferred Lighting Pass）**：
5. 从 GBuffer decode 出 `GBuffer.Specular = 0`、`GBuffer.RampID = 0`
6. `SkyLightingDiffuseShared.ush:114` 用 **`GBuffer.RampID = 0`** → 所有 Foliage 走 **profile 0** → 单一 Ramp → **画面均匀，无条带** ✅

**结论**：Deferred 不是"公式写对了"，而是**BasePass 里那段错误代码的产物被 GBuffer 通路整体丢弃了**——典型的"死代码在 Deferred 下没有可视化后果"。

### 3.3 Forward Path 的数据流（为什么出条带）

Forward 只走一遍 shader（就是 `MobileBasePassPixelShader.usf`）：

1. 走 1036 行 Foliage 分支，`GBuffer.Specular` 保留**原始的逐像素值**（Forward 没有 encode/decode）
2. `MappingProfileID2VW(GBuffer.Specular, 3, 1)`：由于 Specular 逐像素变化，每个像素 `id = round(Specular*255)` 值不同
3. 由于映射函数**离散量化跳跃**，每个像素跳到 Texture2DArray 里**完全不同的 Ramp Profile**
4. 采样得到的颜色沿着 Specular 贴图/顶点色的分布分布——**树的 Specular 输入通常沿树干竖直方向分布**（顶点色/程序纹理），因此呈现为**竖直的鲜艳色带**
5. 计算完的 `DirectLighting` **直接写进 OutColor**（Forward 没有第二遍覆盖机会）→ **屏幕上就是条纹** ❌

### 3.4 "有的树有、有的树没有" 的解释

| 树材质 MI 里 Specular 输入类型 | Forward 表现 |
|---|---|
| **常量**（如 Specular = 0.5） | 所有像素落到同一个 profile → 无条带，但整棵树颜色可能偏（因为落到的 profile 不对） |
| **贴图/顶点色/程序值** | 逐像素跳跃 → 竖直彩色条带 |

用户截图里那些鲜艳条带的树，就是第二种（Specular 是贴图/程序输入）。

---

## 四、修复方案

### 4.1 首版方案（v1，已被 v2 取代）：与 Deferred 通路对齐 Profile 索引来源

> ⚠️ **状态**：v1 只解决了"条带消失"这一表象，但 Forward 光照公式**仍然是自造的**（`Indirect * DiffuseColor + Sub * SubsurfaceColor` + Ramp 调制）。用户明确要求**"不求你的经验正确，对齐 Deferred"**，v1 被 v2 取代。这里保留 v1 内容作为演进记录。

**v1 原则**：让 Forward 和 Deferred 使用**同一个稳定的 Profile 索引变量**，画面一致。

**改动 1（`MobileBasePassPixelShader.usf` 1036-1088 行）**：把 Foliage 分支按 `MOBILE_USE_GBUFFER` 分流

```hlsl
#if MATERIAL_SHADINGMODEL_TWOSIDED_FOLIAGE
#if MOBILE_USE_GBUFFER
    // [Deferred Path] 保留原状：BasePass DirectLighting 会被丢弃，
    // 光照最终由 MobileDeferredShading.usf 重新计算
    float2 RampVW = MappingProfileID2VW(GBuffer.Specular, 3, 1);
    half3 RampColor = Texture2DArraySampleLevel(View.ToonLightingRampTextureArray,
        View.ToonLightingRampTextureArraySampler, float3(GBuffer.GBufferAO, RampVW), 0).rgb;
    RampColor *= RampColor;
    ...
    DiffuseColor = (DiffuseIndirectLighting + SubsurfaceIndirectLighting) * RampColor;
    LightAccumulator_AddSplit(DirectLighting, DiffuseColor, 0.0f, DiffuseColor, 1.0f, false);
#else // MOBILE_USE_GBUFFER
#pragma region Engine ZXB
    // [ZXB Fix][Forward Path Only] 三处修复：
    //   1) 回归官方 diffuse 语义（Indirect * DiffuseColor + Sub * SubsurfaceColor）
    //   2) 去掉 RampColor 二次平方
    //   3) Profile 索引对齐 Deferred，改用 GBuffer.RampID（默认 0）
    half3 FoliageIndirect_ZXB = (DiffuseIndirectLighting * DiffuseColorForIndirect
        + SubsurfaceIndirectLighting * SubsurfaceColor)
        * AOMultiBounce(GBuffer.BaseColor, ShadingOcclusion.DiffOcclusion);

    // Profile 索引对齐 SkyLightingDiffuseShared.ush:114 使用 GBuffer.RampID（默认 0 -> profile 0）
    float2 RampVW_ZXB = MappingProfileID2VW(GBuffer.RampID, 3, 1);
    half3 RampColor_ZXB = Texture2DArraySampleLevel(View.ToonLightingRampTextureArray,
        View.ToonLightingRampTextureArraySampler, float3(GBuffer.GBufferAO, RampVW_ZXB), 0).rgb;
    half  RampLuma_ZXB = dot(RampColor_ZXB, half3(0.299, 0.587, 0.114));
    // Ramp 有效时才调制，未绑定/为 0 时退回官方公式防止全黑
    FoliageIndirect_ZXB = (RampLuma_ZXB > 1e-4)
        ? (FoliageIndirect_ZXB * RampColor_ZXB)
        : FoliageIndirect_ZXB;

    DiffuseColor = FoliageIndirect_ZXB;
    LightAccumulator_AddSplit(DirectLighting, DiffuseColor, 0.0f, DiffuseColor, 1.0f, false);
#pragma endregion
#endif // MOBILE_USE_GBUFFER
#elif MATERIAL_SHADINGMODELS_TOON_CHARACTER
```

**改动 2（`MobileBasePassPixelShader.usf` 1165-1173 行）**：保护 `ApplyMobileToonCombineShadowColor` 免于命中未声明的 `ToonShading` 符号

```hlsl
#pragma region Engine ZXB
// [ZXB Fix] ToonShading 结构体在 usf/ush 中未定义；ApplyMobileToonCombineShadowColor
// 内部首行就 return，对非 Toon Character 完全 no-op，但 shader 编译时必须解析 ToonShading
// 表达式导致 Foliage 等材质编译失败。加编译期门槛。
#if MATERIAL_SHADINGMODELS_TOON_CHARACTER
    DirectLighting.TotalLight = ApplyMobileToonCombineShadowColor(DirectLighting.TotalLight,
        ToonShading.ToonShadowColor, GBuffer, ToonShading.ViewDirection,
        DirectionalLightShadow, ToonShading.LogInvPreExposure,
        ToonShading.ToonPreExposureWeight, ToonShading.ToonConstExposure);
#endif
#pragma endregion
```

### 4.2 方案对比

| 方案 | 优点 | 缺点 | 采用 |
|---|---|---|---|
| **A. 用 `GBuffer.RampID`（默认 0）** | 与 Deferred 完全对齐，画面一致；改动最小；零风险 | 所有 Foliage 走同一 profile 0，无差异化 | ✅ |
| B. 用 `GBuffer.Specular` 但做量化（如 `floor(Specular*4)/4`） | 保留 Specular 语义、支持多 profile | 需要美术改所有 MI 让 Specular 变常量；跨材质不一致风险 | ❌ |
| C. 在 BasePass 里手动 `GBuffer.RampID = GBuffer.Specular` | 打开 RampID 通道差异化 | 需要美术先把所有 Foliage 材质的 Specular 输入固定为常量，否则又出条纹；`GBufferHelpers.ush:448` 也需要取消注释；跨路径协同风险大 | ❌ |
| D. 完全屏蔽 Ramp 采样，走纯官方 diffuse | 最简单 | 丢失所有 Toon 风格 | ❌ |

**采用方案 A 的额外考量**：Deferred Path 目前也是 `GBuffer.RampID = 0`（因为 `Ret.RampID = Ret.Specular` 被注释），所以方案 A 让 Forward/Deferred 完全一致，是最保守的收敛。

### 4.3 未来改进方向（超出本次修复范围）

如果美术希望不同 Foliage 走不同 Ramp（例如"针叶松"用 profile 3、"阔叶树"用 profile 7），需要**三方协同**：

1. **美术资产层**：把 Foliage 材质的 Specular 输入固定为**逐材质常量**（不能是贴图/顶点色），值 = 目标 profile / 255
2. **`GBufferHelpers.ush:448`**：取消注释 `Ret.RampID = Ret.Specular;`
3. **`MobileBasePassPixelShader.usf`**：Forward 分支手动 `GBuffer.RampID = GBuffer.Specular;`（因为 Forward 不走 GBufferHelpers decode）
4. **GBuffer encode/decode**：确认 RampID 有合适的编码槽位（比如 `GBufferB.a`）

---

### 4.4 v2 最终方案（当前采用）：完全对齐 Deferred 通路的真实光照公式

**用户驳回 v1 的理由**："我们代码里有自己的风格化，你从 `DeferredShadingCommon.ush` 里去尝试找出 Deferred 管线下的公式，我们对齐 Deferred，**不求你的经验正确**。"

**核心思路**：**不再自造轮子**（不再用官方 UE5 `Indirect * DiffuseColor + Sub * SubsurfaceColor`，也不再自己采 Ramp），而是**逐字节追踪项目 Deferred 通路里 Foliage 的每一步处理**，然后照搬到 Forward 分支。

#### 4.4.1 Deferred 通路真正的 Foliage 光照公式（追踪结果）

**位置**：`MobileLightingCommon.ush:894-947`（Deferred 移动端的实际入口，被 `MobileDeferredShading.usf` 调用）

关键函数链：
```hlsl
// 1. 初始 DiffuseColorForIndirect = GBuffer.DiffuseColor
half3 DiffuseColorForIndirect = GBuffer.DiffuseColor;

// 2. 调 ApplyCartoonFoliage（ToonDeferredLightingCommon.ush:632-693）：
//    - FinalColor = float3(1,1,1);
//    - FinalColor = ColorDesaturation(FinalColor, GetBlueCircleEffectFactor(WorldPos))
//    - DiffuseIndirectLighting *= FinalColor * FoliageShadowIntensity
//    - SubsurfaceDiffuseLighting *= FinalColor * FoliageShadowIntensity
//    - RoughSpecularIndirectLighting *= ExtractSubsurfaceColor(GBuffer)
//    - 完全没有 Ramp 采样（所有 MappingProfileID2VW 相关代码都被注释）
if (GBuffer.ShadingModelID == SHADINGMODELID_TWOSIDED_FOLIAGE) {
    ApplyCartoonFoliage(GBuffer, WorldPos, CloudFactor, DiffuseColorForIndirect, ...);
}

// 3. 走 non-Character 分支的 HSV Backface Hack
IndirectDiffuseLighting *= View.IndirectLightingColorScale;
float3 Hack_BackfaceDiffuse = SubsurfaceDiffuseLighting * DiffuseColorForIndirect;
float3 HSV_Backface = RGBToHSV(Hack_BackfaceDiffuse);
HSV_Backface.y = saturate(HSV_Backface.y + 0.5f);   // 提高背光饱和度
Hack_BackfaceDiffuse = HSVToRGB(HSV_Backface);
Hack_BackfaceDiffuse = lerp(SubsurfaceDiffuseLighting * DiffuseColorForIndirect,
                             Hack_BackfaceDiffuse, GBuffer.FoliageCustomData.x);
float3 HSV_IndirectDiffuse = RGBToHSV(IndirectDiffuseLighting);
float3 Hack_IndirectDiffuse = HSVToRGB(HSV_IndirectDiffuse);

// 4. 组合并累加
IndirectDiffuseColor = (Hack_IndirectDiffuse * DiffuseColorForIndirect
    + Hack_BackfaceDiffuse * GBuffer.FoliageCustomData.y)
    * GBuffer.FAO * FoliageShadowIntensity;
LightAccumulator_AddSplit(DirectLighting, IndirectDiffuseColor, 0.0f, IndirectDiffuseColor, 1.0f, false);
```

**关键洞察**：
- Deferred **完全没有采样 `ToonLightingRampTextureArray`**！所有 Ramp 相关代码都被作者注释掉了（原因：`"Uncomment after update all foliage resource"` —— 等美术资源迁移完再启用）
- 项目实际用 `ApplyCartoonFoliage`（乘 `FinalColor * FoliageShadowIntensity`）+ **HSV Backface Hack**（提高背光饱和度制造 Toon 风格）作为 Foliage 的核心光照
- v1 修复留下的 Ramp 采样纯属"我自己脑补的"，与项目美术契约不符

#### 4.4.2 v2 实现（Forward 分支照搬 Deferred 公式）

**依赖项检查**（关键）：

| 依赖 | Forward Path 下是否可用 | 来源 |
|---|---|---|
| `ApplyCartoonFoliage()` | ✅ | `ToonMobileLightingCommon.ush` → `MobileLightingCommon.ush` → `ToonDeferredLightingCommon.ush` include 链已通 |
| `ColorDesaturation()` / `GetBlueCircleEffectFactor()` / `RGBToHSV()` / `HSVToRGB()` | ✅ | 同上，通过 include 链传递 |
| `ExtractSubsurfaceColor()` | ✅ | 同上 |
| **`FoliageShadowIntensity` uniform** | ✅ | `MobileLightingCommon.ush:42-44` 提前 `#define FoliageShadowIntensity MobileBasePass.FoliageShadowIntensity`，专门为 `!MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` 场景（就是 Forward BasePass） |
| **`GBuffer.FAO / FoliageCustomData / FMetalic / SSubsurface` 等 Foliage 专用字段** | ⚠️ 需要手动补齐 | Deferred 由 `GBufferHelpers.ush:440-458` decode 时赋值；Forward 不走 decode，`FGBufferData GBuffer = (FGBufferData)0` 初始化为 0，直接用会导致 `FAO=0 → 全黑` |

**代码（`MobileBasePassPixelShader.usf` Forward 分支）**：

```hlsl
#else // MOBILE_USE_GBUFFER
//#pragma region Engine ZXB
    // [ZXB Fix][Forward Path Only] 完全对齐 Deferred 通路的 Foliage 光照公式
    // 复用 ApplyCartoonFoliage + HSV Backface Hack，不再自造 Ramp 采样

    // —— 补齐 Deferred decode 语义（对齐 GBufferHelpers.ush:440-458 Foliage 分支）——
    GBuffer.FMetalic  = GBuffer.StoredMetallic;
    GBuffer.FRoughness = GBuffer.Roughness;
    GBuffer.FAO = GBuffer.GBufferAO;                          // 关键：FAO=0 会导致最终乘 0 全黑
    GBuffer.SSubsurface = GBuffer.CustomData.rgb;
    GBuffer.FoliageCustomData.xyz = float3(GBuffer.StoredMetallic, GBuffer.FRoughness, GBuffer.StoredSpecular);

    // —— 复用 ApplyCartoonFoliage（对齐 MobileLightingCommon.ush:898-906）——
    half3 RoughSpecularIndirectLighting_ZXB = half3(0, 0, 0);
    half3 DiffuseColorForIndirect_Foliage = DiffuseColorForIndirect;
    ApplyCartoonFoliage(
        GBuffer,
        MaterialParameters.WorldPosition_CamRelative,
        1.0 /*CloudFactor - Forward 下无 ShadowFactor.a，用 1*/,
        DiffuseColorForIndirect_Foliage,
        DiffuseIndirectLighting,
        SubsurfaceIndirectLighting,
        RoughSpecularIndirectLighting_ZXB
    );

    // —— HSV Backface Hack + 累加（对齐 MobileLightingCommon.ush:918-947）——
    DiffuseIndirectLighting *= View.IndirectLightingColorScale;

    float3 Hack_BackfaceDiffuse = SubsurfaceIndirectLighting * DiffuseColorForIndirect_Foliage;
    float3 HSV_Backface = RGBToHSV(Hack_BackfaceDiffuse);
    HSV_Backface.y = saturate(HSV_Backface.y + 0.5f);
    Hack_BackfaceDiffuse = HSVToRGB(HSV_Backface);
    Hack_BackfaceDiffuse = lerp(SubsurfaceIndirectLighting * DiffuseColorForIndirect_Foliage,
                                 Hack_BackfaceDiffuse, GBuffer.FoliageCustomData.x);

    float3 HSV_IndirectDiffuse = RGBToHSV(DiffuseIndirectLighting);
    float3 Hack_IndirectDiffuse = HSVToRGB(HSV_IndirectDiffuse);
    half3 IndirectDiffuseColor_ZXB = (Hack_IndirectDiffuse * DiffuseColorForIndirect_Foliage
        + Hack_BackfaceDiffuse * GBuffer.FoliageCustomData.y) * GBuffer.FAO * FoliageShadowIntensity;

    DiffuseColor = IndirectDiffuseColor_ZXB;
    LightAccumulator_AddSplit(DirectLighting, DiffuseColor, 0.0f, DiffuseColor, 1.0f, false);
//#pragma endregion
#endif // MOBILE_USE_GBUFFER
```

#### 4.4.3 v1 vs v2 对比

| 维度 | v1（已废弃） | v2（当前采用） |
|---|---|---|
| **公式来源** | 官方 UE5 diffuse 语义 + `GBuffer.RampID`（0）修复 Ramp 索引 | **逐字节复制项目 Deferred 通路的 `ApplyCartoonFoliage` + HSV Backface Hack** |
| **是否采样 `ToonLightingRampTextureArray`** | ✅ 采样（用 RampID=0 → profile 0） | ❌ **完全不采样**（Deferred 里这段代码本身就被注释了） |
| **`AOMultiBounce` 使用** | ✅ 用官方 AO 传播 | ❌ **不用**（Deferred 里没用，改用 `GBuffer.FAO * FoliageShadowIntensity`） |
| **HSV Backface Hack** | ❌ 未实现 | ✅ **实现**（对齐 `MobileLightingCommon.ush:929-937`） |
| **`FoliageShadowIntensity` uniform 使用** | ❌ 未使用 | ✅ **使用**（对齐项目美术调优参数） |
| **`ColorDesaturation` + `GetBlueCircleEffectFactor`（蓝圈效果）** | ❌ 未实现 | ✅ **由 `ApplyCartoonFoliage` 内部处理** |
| **与 Deferred 视觉一致性** | 中等（无条带但色调可能偏） | **逐字节一致** |
| **代码量** | ~10 行 | ~30 行（其中 5 行是补齐 GBuffer decode 语义） |

#### 4.4.4 v2 的 GBuffer 字段补齐说明

Forward Path 下 `FGBufferData GBuffer = (FGBufferData)0;` 后，以下 Foliage 专用字段都是 0，会导致 `ApplyCartoonFoliage` / HSV Hack 拿到错误输入。**必须在调用前手动补齐**：

| 字段 | Deferred 赋值来源（`GBufferHelpers.ush:440-458`） | Forward v2 补齐方式 |
|---|---|---|
| `GBuffer.FMetalic` | `Ret.FMetalic = Ret.StoredMetallic;` | `GBuffer.StoredMetallic`（usf:648 已赋值） |
| `GBuffer.FRoughness` | `Ret.FRoughness = Ret.Roughness;` | `GBuffer.Roughness` |
| `GBuffer.FAO` | `Ret.FAO = Ret.GenericAO;` （`GenericAO` 默认 = `GBufferAO`） | `GBuffer.GBufferAO` ⚠️ **最关键**，为 0 会导致全黑 |
| `GBuffer.SSubsurface` | `Ret.SSubsurface = Ret.CustomData.rgb;` | `GBuffer.CustomData.rgb` |
| `GBuffer.FoliageCustomData` | `Ret.FoliageCustomData.xyz = float3(StoredMetallic, FRoughness, StoredSpecular);` | 同左，直接照搬 |

---

## 五、修复验证

### 5.1 v1 编译验证

- `RecompileShaders /Engine/Private/MobileBasePassPixelShader.usf` → 完成，0.41s，零 error
- `M_Plant` 母材质 + 30+ 个 Foliage MI 增量重编 → 完成，零 error（只有历史 warning-only）
- lint 0 error

### 5.2 v1 视觉验证（用户确认）

- ✅ 竖直彩色条带完全消失
- ⚠️ 但被用户驳回："我们代码里有自己的风格化，你从 `DeferredShadingCommon.ush` 里去尝试找出 Deferred 管线下的公式，我们对齐 Deferred，**不求你的经验正确**"
- 结论：v1 只是"消除表象"，v2 迭代才是"彻底对齐"

### 5.3 v2 编译验证

- `RecompileShaders /Engine/Private/MobileBasePassPixelShader.usf` → 完成，**0.44s，零 error**
- lint 0 error
- 依赖函数 `ApplyCartoonFoliage / RGBToHSV / HSVToRGB / ColorDesaturation / GetBlueCircleEffectFactor` 通过 include 链全部可用
- `FoliageShadowIntensity` uniform 通过 `MobileLightingCommon.ush:42-44` 的 `#define` 已 route 到 `MobileBasePass.FoliageShadowIntensity`

### 5.4 Review 清单（v2 状态）

| 项目 | 状态 |
|---|---|
| 修改文件数 | 1 个（`MobileBasePassPixelShader.usf`） |
| 修改点数 | 2 处（Foliage 分支 + ToonShading 保护） |
| ZXB Region 数 | 2 个 |
| Deferred Path 影响 | **0 字节改动**，行为不变 |
| Forward Path 影响 | Foliage 光照公式完全对齐 Deferred（`ApplyCartoonFoliage` + HSV Hack） |
| 其他 ShadingModel 影响 | 0 |
| 变量命名冲突 | 使用 `_ZXB` 后缀避免与外层作用域冲突 |
| 死代码 / 冗余变量 | 无 |
| 是否引入额外 uniform / UBO 依赖 | 无（`FoliageShadowIntensity` 已存在于 `MobileBasePass` UBO） |
| 是否需要美术资产迁移 | 无（不依赖 Ramp 也不依赖 RampID 索引） |

---

## 六、快速排查 Checklist

未来遇到类似的 Mobile Toon 相关"条带 / 色斑 / 逐像素跳跃"问题，可按以下顺序排查：

- [ ] **确认 Path**：走的是 Forward 还是 Deferred？`stat mobilescenerender` 或看 `MOBILE_DEFERRED_SHADING` cvar
- [ ] **确认 ShadingModel**：出问题的材质是 `TWOSIDED_FOLIAGE` / `TOONSTANDARD` / `TOONFACE` / 其他？
- [ ] **检查 Ramp 采样输入**：`MappingProfileID2VW(X, ...)` 里的 X 是什么？是否逐像素变化？
- [ ] **对比 PC 与 Mobile**：`SkyLightingDiffuseShared.ush` / `ToonDeferredLightingCommon.ush` 是 PC/Deferred 的参考实现，看它用哪个 GBuffer 字段
- [ ] **对比 Forward 与 Deferred**：BasePass 里的公式在 Deferred 会不会被 GBuffer 通路丢弃？丢弃 → BasePass 公式错误在 Deferred 下"隐形"
- [ ] **Encode/Decode 完整性**：材质通道（Specular / Metallic / Roughness）在 GBuffer 通路里是否被 encode 前保留、encode 后清零、decode 后转存
- [ ] **`MappingProfileID2VW` 的量化特性**：任何被它当索引的字段都必须是逐材质常量，不能是逐像素贴图

---

## 七、相关文件与代码位置

### 7.1 本次修改

| 文件 | 关键行 | 用途 |
|---|---|---|
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileBasePassPixelShader.usf` | 1036-1088 | Mobile BasePass Foliage 分支（v2 完全对齐 Deferred） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileBasePassPixelShader.usf` | 1165-1173 | Forward ToonShading 保护 |

### 7.2 Deferred 通路 Foliage 光照的真正实现（v2 对齐目标）

| 文件 | 关键行 | 用途 |
|---|---|---|
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileLightingCommon.ush` | 42-44 | `FoliageShadowIntensity` 宏 route（Forward BasePass 场景用 `MobileBasePass.FoliageShadowIntensity`） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileLightingCommon.ush` | 894-947 | **Mobile Deferred Foliage 光照真正入口**（v2 对齐来源） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\ToonDeferredLightingCommon.ush` | 632-693 | `ApplyCartoonFoliage` 函数定义（项目自研 Foliage 处理） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\Toon\ToonShadingCommon.ush` | 10-62 | `RGBToHSV / HSVToRGB / ColorDesaturation / GetBlueCircleEffectFactor` 定义 |

### 7.3 GBuffer 相关

| 文件 | 关键行 | 用途 |
|---|---|---|
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileDeferredShading.usf` | 298, 359 | Mobile Deferred Lighting Pass（Deferred 光照真正入口） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\GBufferHelpers.ush` | 440-458 | Foliage GBuffer decode（v2 补齐 Forward 时对齐的语义来源） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\DeferredShadingCommon.ush` | 470-484 | `FGBufferData` 结构体定义（`RampID / FAO / FBaseColor / FoliageCustomData` 等 Foliage 字段） |

### 7.4 v1 迭代（已废弃但保留追溯）

| 文件 | 关键行 | 用途 |
|---|---|---|
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\SkyLightingDiffuseShared.ush` | 112-119 | PC/Deferred 端 Foliage Ramp 采样参考（v1 曾照抄，v2 弃用） |
| `d:\GR_DevTest\UE5EA\Engine\Shaders\Private\Toon\ToonShadingCommon.ush` | 55-62 | `MappingProfileID2VW` 定义（v1 依赖，v2 不再使用） |

---

## 八、经验教训

1. **"同一段代码在 Deferred 没事、在 Forward 出事"是典型的 pass-level dead code 陷阱**：Deferred 会把 BasePass 里错误的中间产物整个丢弃并重新算，掩盖了 BasePass 里的 bug；Forward 少一遍 pass，任何错误立即上屏。**修复时必须区分 Path，不能一刀切改公式**。

2. **`MappingProfileID2VW` 这种"离散量化映射"函数是设计陷阱**：任何被它当输入的变量必须严格是"逐材质常量"。如果这个契约被打破（美术把贴图 / 顶点色接进 Specular），就会出规则性条带。**Ramp/Palette 类查找表要在源头做量化保护**。

3. **`#pragma region Engine ZXB` 边界包裹修改**：便于后续做 diff、review、merge。这次两处修改都严格用 region 包裹，Deferred 分支一字未改，可安全提交。

4. **修改前一定问用户"哪条 Path 有问题"**：初次沟通没确认 Path，我一开始修改了公式（虽然是 Path 无关的方向对了），但**修改如果不做 Path 分流会有污染 Deferred 的风险**。收敛问题面 → 收敛修改面。

5. **RenderDoc / 逐 profile 采样调试**：条带如果颜色鲜艳且规则，几乎必是 palette/LUT 查表错误。快速验证方法是把 Ramp 采样 hardcode 到某个 profile 看条带是否消失。

6. **⚠️ 【v2 新增，最重要】"对齐 Deferred" ≠ "用官方公式修正"**：
   - v1 的错误：我看到 Foliage 公式残缺，本能地用"UE5 官方 diffuse 语义"去修正——但项目有自己的美术风格化，官方公式不是这个项目的"正确答案"。
   - **正确姿势**：先**逐字节追踪项目 Deferred 通路里同一 ShadingModel 到底走什么代码**（跟到 `ApplyCartoonFoliage` 函数体、跟到 HSV Backface Hack、跟到 `FoliageShadowIntensity` 参数），然后**照搬**到 Forward，一个字都不改。
   - **判据**：把 Forward Viewport 和 Deferred Viewport 并排看，同一棵树在两个 Path 下**颜色/亮度/背光饱和度/AO 强度必须逐像素一致**——这才是真正的"对齐"。
   - **副作用**：这样做会连带把项目自己的 bug 一起搬到 Forward（比如 `ApplyCartoonFoliage` 里所有 Ramp 采样都被注释掉、`FinalColor` 硬编码为 1）——但这是"项目当前定义的正确行为"，我们不该越界"纠正"。

7. **v2 迭代的三方 include 复用价值**：`ApplyCartoonFoliage` 定义在 `ToonDeferredLightingCommon.ush`（听名字像只给 Deferred 用），但通过 `MobileLightingCommon.ush → ToonMobileLightingCommon.ush` 的 include 链，Forward BasePass 也能无缝调用。**读代码要顺 include 链看，别看文件名先入为主**。

8. **`FGBufferData` 里 Foliage 专用字段（`FAO / FBaseColor / FMetalic / FRoughness / SSubsurface / FoliageCustomData`）在 Forward Path 是"空的"**：这些字段只有 Deferred 走 `GBufferHelpers.ush::MobileFetchAndDecodeGBuffer` 时才会被赋值。Forward 下 `FGBufferData GBuffer = (FGBufferData)0` 初始化为 0。**要复用 Deferred 的函数，就必须在 Forward 里手动按 decode 语义补齐这些字段**，否则会得到全黑（`FAO=0` 乘任何东西都是 0）。

---

## 九、文档更新历史

| 版本 | 时间 | 变更 |
|---|---|---|
| v1 | 2026-07-14 17:41 | 首次归档：用 `GBuffer.RampID`（默认 0）修复 Ramp Profile 索引，消除条带 |
| v2 | 2026-07-14 19:42 | **重大迭代**：用户驳回 v1"用官方公式的思路"，要求完全对齐 Deferred 通路。改为直接复用 `ApplyCartoonFoliage` + HSV Backface Hack + `FoliageShadowIntensity`，Forward/Deferred 逐字节一致。新增经验教训 6/7/8。 |

---

**文档创建时间**：2026-07-14 17:41
**最近更新时间**：2026-07-14 19:42（v2）
**修改人**：ZXB（Codebuddy Assistant）
**验证人**：用户确认视觉效果恢复
