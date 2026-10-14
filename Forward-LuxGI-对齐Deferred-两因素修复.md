# Forward LuxGI 对齐 Deferred — 两因素修复（F/D 比值 2.53× → 1.00×）

> 问题：Forward 管线卡通角色 LuxGI=ON 时最终色 R=0.416，Deferred R=0.164，比值 2.53×。LuxGI=OFF 时两侧均为 emissive（~0.00014），差异收敛。根因 = LuxGI 逃脱 CombinedShadowColor 调制 + Weight 时序错位，两因素叠加 2.0×1.2≈2.53。

---

## 一、数据对照

| 条件 | Forward R (FWD=最终色) | Deferred R (DEF=lighting) | F/D 比值 |
|---|---|---|---|
| **修复前 LangGI=ON** | 0.4160 | 0.1643 | **2.53×** |
| 修复第一步 CombinedShadowColor 对齐 → Weight 时序对齐 → **修复后 LuxGI=ON** | **0.1640** | 0.1643 | **1.00×** ✅ |
| LuxGI=OFF | 0.00014 | 0.00000 | 对齐（皆仅 emissive） |

全部在基准位姿 `[201.995511, 18.774568, 128.560938] / [15.0001, 266.59991, -1e-6]`，AndroidVulkan_Preview，r.ShadowQuality=0。

---

## 二、根因分析

### 因素 1：LuxGI 逃脱 CombinedShadowColor（~2.1×）

Forward 的 LuxGI 剥离机制（防非 toon double-PreExposure）把 `LuxGIExtracted = TotalLight - ExposureAffectedLight` 从 Color 线抽走，末尾加回。

`ApplyMobileToonCombineShadowColor` (L1536) 对 Color 乘 toon 阴影因子 C≈0.5。LuxGI 被抽走 → 不受此调制 → 偏亮。

Deferred 的 CombinedShadowColor (L403) 作用于含 LuxGI 的 TotalLight → 正确。

**修复**：剥离三门前加 `!MATERIAL_SHADINGMODELS_TOON_CHARACTER`，让 toon 的 LuxGI 留在 TotalLight 里自然流过 CombinedShadowColor。

### 因素 2：Weight 作用时序（~1.2×）

Forward 末尾 `OutColor.rgb *= Weight` 覆盖含 LuxGI 的最终色；Deferred Weight 在 LuxGI 加入前乘到 DirLight（L326-330），LuxGI 不吃 Weight。

Forward：DirLight → +LuxGI → CombinedShadowColor → **\*Weight**
Deferred：DirLight**\*Weight** → +LuxGI → CombinedShadowColor

**修复**：Forward 方向光后加 `DirectLighting.TotalLight *= 1+ToonEnergyWeight`（`#if TOON_CHARACTER && !GBUFFER`），末尾 toon 路径删除该乘法。

### 两因素叠加验证

C≈0.5 → 1/0.5≈2.0，Weight≈1.2 → 2.0×1.2=2.4≈实测 2.53 ✅

---

## 三、修改文件

**文件**：`UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`（P4 #21）

### 修改 1：方向光后注入 Weight（L1245）

```hlsl
// [ZXB Fix] Forward toon Weight 提前到方向光后/LuxGI 前，对齐 Deferred 时序
#if MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MOBILE_USE_GBUFFER && !MATERIAL_SHADINGMODEL_UNLIT
    const float EarlyToonEnergyWeight = clamp(View.ToonEnergyWeight, -1.0f, 1.0f);
    DirectLighting.TotalLight *= 1.0 + EarlyToonEnergyWeight;
#endif
```

### 修改 2：剥离三门加 `!TOON_CHARACTER`（L1257）

```hlsl
// 改前
#if ENABLE_LUX_GI && ... && !MOBILE_USE_GBUFFER
// 改后（三个剥离门同一条件）
#if ENABLE_LUX_GI && ... && !MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MOBILE_USE_GBUFFER
```

### 修改 3：末尾 toon 路径删除 Weight（L1604-1608）

```hlsl
#if !MATERIALBLENDING_MODULATE
    #if MATERIAL_SHADINGMODELS_TOON_CHARACTER
        // Weight moved before LuxGI, skip here.
    #else
        OutColor.rgb *= ResolvedView.PreExposure;
    #endif
#endif
```

---

## 四、快速排查 Checklist

1. `r.Mobile.ShadingPath` 的 `ProjectSetting:` 层必须是目标值（不是看最终值）
2. 启动编辑器不要带 `-dpcvars=r.Mobile.ShadingPath=N`（静默无效）
3. 确认 LuxGI=OFF 时 F/D 均只有 emissive（两侧非光照路径一致）
4. 确认 LuxGI=ON 时 F≈D（1.00× 内）
5. 非 toon 对象没有因为 `#else PreExposure` 分支丢失而变暗

## 五、相关参考

- `D:/GR_DevTest/debug-patches/` — 调试框架存档
- `D:/GR_DevTest/UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf` — 全部修改
- Skill: `/ZXB` 一键取数（能力 ⑫）
- Skill: `/UnrealShaderPrint调试` 恢复Mobile调试环境
