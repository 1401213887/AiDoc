# UE-Mobile-Forward-Toon角色对齐Deferred-GBuffer量化-Emissive口径-LuxGI压暗顺序-五项修复

> Mobile Forward(`r.Mobile.ShadingPath=0`) 与 Deferred(`=1`) 渲染同一 toon 角色，中心像素最终色偏差 **−14.5% / −49.3% / −56.6%**（RGB）。逐项定位出五个独立根因，全部在 Forward 侧修复（Deferred 效果零改动），收敛到 **最大偏差 0.37%**。核心线索：Deferred 的 GBuffer 编解码链存在**不对称变换**与**极窄位宽量化**，且 Deferred 的最终像素是 **base pass + lighting pass 两次加性写入**。

---

## 一、问题定位流程

### 1.1 测试环境固定量

对比两条管线必须锁死所有变量，否则数值不可比：

| 项 | 值 |
|---|---|
| 引擎 | UE 5.5.4 GR fork（`++GR+DevTest`） |
| 预览平台 | `AndroidVulkan_Preview` / `Android_High` |
| 管线切换 | `-dpcvars=r.Mobile.ShadingPath=0`（Forward）/ `=1`（Deferred） |
| `r.Mobile.DeferredLightingSplitPass` | `0`（关 split，保证全屏只有一个 draw 写探针） |
| 视口 | 2560×1392（沉浸模式，中心像素 = (1280, 696)） |
| 采样像素 ShadingModel | `13` = `SHADINGMODELID_TOONSKIN` |
| `View.ToonEnergyWeight` | `0.2` → `Weight = 1.2` |
| `View.PreExposure` | `2.0` |

> `r.Mobile.ShadingPath` 被 `FReadOnlyCVARCache` 在启动期快照，**必须重启编辑器**；`-dpcvars=` 在该快照之前生效，可免改 ini。

### 1.2 探针方法：ShaderPrint + StructuredBuffer

在 base pass / lighting pass 内对中心像素写 `RWStructuredBuffer<float4>`，再由一个 CS 读回并用 ShaderPrint 打到屏幕，最后 `HighResShot 1` 截图读数。

关键点：**lighting pass(`MobileDirectionalLightPS`) 与 base pass 处于同一个 RDG pass**（`MobileShadingRenderer.cpp:2562 SceneColorRendering`），共享同一份 `PassParameters->MobileBasePass` uniform buffer，因此 `MobileDebugValueBufferUAV` 在 lighting pass 里天然可写，**无需给 PS 增加任何 C++ 绑定**。

### 1.3 确认的关键事实

| 确认项 | 结论 | 依据 |
|---|---|---|
| Deferred 最终像素构成 | **两次加性写入** | `MobileDeferredShadingPass.cpp:1486/1644` → `TStaticBlendState<CW_RGB, BO_Add, BF_One, BF_One>` |
| `Color += Emissive` 位置 | 在 `#endif // DEFERRED_SHADING_PATH` 之**外**，两条路径都执行 | `MobileBasePassPixelShader.usf` |
| `MOBILE_CHARACTER_FORWARD` | **硬编码为 0** | `MobileBasePassPixelShader.usf:15` |
| mobile 下 `MaxProfileID` | 恒为 63（`SHADING_PATH_MOBILE` 在 Forward 与 mobile-Deferred **都为 1**） | `GetShadingPath()==Mobile` 注入 |
| `ApplyMobileToonCombineShadowColor` | 末尾是**纯乘法**，全链路线性 | `ToonMobileLightingCommon.ush:436 return InColor * ToonShadowBlendFactor` |
| Deferred toon 角色的 IBL/sky light | **跳过** `ReflectionEnvironmentSkyLighting` | `MobileDeferredShading.usf:413` |
| Forward `AccumulateReflection` 净增量 | 实测 **0,0,0**（该场景无影响） | 直接探针 |
| Forward 手算 `LuxRoughSpec` | 实测 **0,0,0** | 直接探针 |

### 1.4 收敛过程（中心像素最终色）

| 状态 | dev% (R/G/B) | 最大偏差 |
|---|---|---|
| 基线 | −14.5 / −49.3 / −56.6 | 56.6% |
| +① CustomData round-trip | +33.4 / +43.8 / +44.8 | 44.8% |
| +①+③ 缩放系数 | −20.0 / −13.7 / −13.1 | 20.0% |
| +①+③+② LuxGI | +7.4 / +10.4 / +7.8 | 10.4% |
| +(a) GBuffer 位宽量化 | direct 分量 −8% → **±0.5%** | — |
| +(4)(b) Emissive 口径 + LuxGI 顺序 | **+0.13 / +0.37 / −0.15** | **0.37%** |

---

## 二、根因分析

### 根因 ① TOONSKIN `CustomData.a` 的 encode/decode 不对称

`CustomData.a` 对 TOONSKIN 存的是 `SubsurfaceProfileID`（`ShadingModelsMaterial.ush:226`，两条路径共用此赋值）。Deferred 的 GBuffer 往返存在**缺失的逆运算**：

```
编码 DeferredShadingCommon.ush:793:
    ProfileIDOrShadowFalloff = (ShadingModelID == TOONSKIN)
                             ? GBuffer.CustomData.a * 255.0 / 63.0   // ← 额外缩放
                             : GBuffer.CustomData.w;

解码 DeferredShadingCommon.ush:950:
    GBuffer.CustomData.a = MobileDecodeColorChannel(InGBufferA.b, true);  // 内部 /63
    // ↑ 没有乘回 63/255
```

实测：Forward `CustomData.a = 0.00784`，Deferred 解出 `0.031746`，比值 **4.0492 ≈ 255/63 = 4.0476**。

下游是 `ToonShadingModels.ush:1263` 的 `MappingProfileID2VW(CustomData.a, 3, 1, 63)`，取 `ProfileID = round(v * 63)`：

- Forward：`round(0.00784 × 63) = 0`
- Deferred：`round(0.031746 × 63) = 2`

**两边采样 `ToonLightingRampTextureArray` 里完全不同的曲线**，直接造成 toon specular 显著不一致（实测 Forward direct Specular = 0，Deferred = 0.309/0.296/0.303）。

### 根因 ② Forward 对 toon 角色剪掉了 LuxGI

`MobileBasePassPixelShader.usf` 中 LuxGI 相关的四处 guard 都带 `!MATERIAL_SHADINGMODELS_TOON_CHARACTER`，使 toon 角色在 Forward 下完全没有 LuxGI 间接光，而 Deferred 有。

四块必须**同源**（`ENABLE_LUX_GI` 由 C++ 读 `r.LuxGI` 注入，与 Deferred 的 `FEnableLuxGI` 同一开关）：
1. snapshot（`ExposureAffectedLight`）
2. 主调用（`AccumulateLuxGILighting`）
3. 剥离（`LuxGIExtracted`）
4. 末尾加回

### 根因 ③ 末尾缩放系数：PreExposure vs ToonEnergyWeight

因 `MOBILE_CHARACTER_FORWARD` 硬编码为 0，原条件恒假 → Forward 的 toon 角色一直走 `#else` 的 `*= PreExposure`（**2.0**），而 Deferred lighting pass（`MobileDeferredShading.usf:369-374`）对 toon 角色乘的是 `1 + ToonEnergyWeight`（**1.2**）。

### 根因 (a) toon GBuffer 位宽量化 —— direct 光照差 8.6%

Deferred 把 toon 字段压进 GBuffer 零散通道，位宽极窄（`DeferredShadingCommon.ush:795-798`）：

| 字段 | 存放位置 | 位宽 |
|---|---|---|
| ShadowOffset + CustomShadow | `GBufferB.a` | 4 + 4 |
| ToonAO + IndirectSpecMask + NeckBlendMask | `GBufferC.a` | 3 + 3 + 2 |
| Specular + Roughness | `GBufferB.g` | 4 + 4 |
| ProfileID | `GBufferA.b` | 6 |

**关键**：`ShadowOffset = 0.5` 恰好落在 4bit 量化格中间 —— `0.5 × 15 = 7.5` → `round` 到 8 → `8/15 = 0.53333`，**系统性抬高 6.7%**。

而 `ToonShadingModels.ush:1248`：

```hlsl
ToonBRDF = GetToonDiffuseBRDF(saturate(NoL + ShadowOffset), 0.7f);
```

同时乘在 `Lighting.Diffuse` 与 `Lighting.Specular` 上 —— 这正解释了实测现象「Diffuse 与 Specular 被**同一个** ~1.085 因子一起缩放」。正是这个"共同乘数"特征排除了 BxDF 内部参数差异。

此外 Deferred 解码时有若干字段被**硬编码常量覆盖**（GBuffer 里根本没留位置，`DeferredShadingCommon.ush:952-956`），不对齐仍有残差。

### 根因 (4) Emissive 缩放口径

Deferred 下 emissive 由 base pass 写进 SceneColor(SV_Target0)，lighting pass 是**加性混合**叠上来的，所以 lighting pass 里那句 `TotalLight *= Weight` **碰不到 emissive** —— emissive 走的是 base pass 末尾 `#else` 的 `*= PreExposure`。

注意 Combine 对 Deferred 同样编译，**会**作用到 emissive 上：

```
Deferred emissive 终值 = Emissive × k × PreExposure(2.0)
Forward  emissive 终值 = Emissive × k × Weight(1.2)      ← 少乘 1.667 倍
```

### 根因 (b) LuxGI 绕过了 Combine 压暗与描边

两条路径的**顺序**不同：

```
Deferred: LuxGI(:403) → Combine(:468) → 描边 lerp(:487)
          LuxGI 受 Combine 压暗(k≈0.879/0.826/0.848) + 描边遮罩

Forward:  LuxGI 剥离 → ... → Combine → 描边 → 末尾才加回 LuxGIExtracted
          LuxGI 完全绕过 Combine 和描边
```

LuxGI 被剥离是为修 double-PreExposure（`AccumulateLuxGILighting` 内部已乘一次 Pre，末尾再乘一次 → ≈PreExposure²），但加回点放在了函数最末尾。

---

## 三、详细技术原理

### 3.1 Deferred 最终色 = 两次加性写入

这是对比基准的**核心陷阱**：

```
Deferred_final = (Emissive × k × PreExposure)      ← base pass 写 SceneColor
               + k × (direct×W + LuxGI + IBL)      ← lighting pass 加性叠加
                                                     (= slot19/OutColor 探针值)
```

`k` = `ApplyMobileToonCombineShadowColor` 的等效乘数。lighting pass 里的探针**永远不含 base pass 那一半**。

实测（同一帧）：

| | R | G | B |
|---|---|---|---|
| base pass（`Emissive × k × PreExp`） | 0.32683 | 0.18064 | 0.13656 |
| lighting pass（`k × A.Total`） | 1.93945 | 0.87792 | 0.75927 |
| **Deferred 真实终值** | **2.26628** | **1.05856** | **0.89583** |

### 3.2 `MOBILE_USE_GBUFFER` vs `MOBILE_DEFERRED_SHADING` 的口径区别

修 ③ 时判据的选择直接决定会不会误伤 Deferred：

```hlsl
// L113：含 blend-mode 条件
#define MOBILE_USE_GBUFFER (MOBILE_DEFERRED_SHADING && ((MATERIALBLENDING_SOLID || MATERIALBLENDING_MASKED) && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER))
```

`!MOBILE_USE_GBUFFER` 取反后会把「**Deferred 下的半透明 toon 角色**」和「**Deferred 下的 toon+Water**」一起卷进来（它们在 Deferred 同样没有 GBuffer）→ 改到 Deferred 观感。

正确做法是用管线级开关 `MOBILE_DEFERRED_SHADING`（由 C++ `IsMobileDeferredShadingEnabled` 注入，见 `ShaderCompiler.cpp:3521`，**与 blend mode 无关**）取反，再显式带上同样的 blend-mode 口径。枚举 8 种场景验证只有「Forward + 不透明/masked」这一格发生变化。

### 3.3 线性链路的等价预乘变换

Combine 是纯乘法，从 emissive/LuxGI 累加点到末尾缩放之间**全链路线性**，因此"预乘/预除一个系数"是精确等价变换，而非近似：

```
目标：Emissive × k × PreExposure
实现：Color += Emissive × (PreExposure / Weight)  →  ×k  →  ×Weight
     = Emissive × (PreExposure/Weight) × k × Weight
     = Emissive × k × PreExposure                        ✓ 且 k 仍由 Combine 正常施加
```

LuxGI 同理：`Color += LuxGIExtracted / Weight` → `×k` → `×Weight`，净缩放为 1，既保持「只乘一次 PreExposure」又让它经过 Combine 与描边。

### 3.4 动态生成宏不能进表达式

`MATERIAL_SHADINGMODELS_TOON_CHARACTER` / `MOBILE_CHARACTER_FORWARD` / `MOBILE_USE_GBUFFER` 等由 material translator 动态生成，**不保证被定义**。

- 在 `#if` 里未定义等价于 0，**安全**
- 直接写进 HLSL 表达式会 `undeclared identifier` 编译失败

故探针里需用 `#if/#else` 折成局部常量再写入 buffer。

---

## 四、修复方案

全部改动位于 `UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`，均包在 `#pragma region Engine ZXB` … `#pragma endregion` 内，**Deferred 侧零改动**。

### 4.0 共享判据宏（避免三处条件 drift）

该长条件原本在 3 处重复，曾发生过 drift（探针条件与真实分支不一致 → 报假数据）。收成单一宏：

```hlsl
// MobileBasePassPixelShader.usf:135
#pragma region Engine ZXB
#define FORWARD_TOON_CHARACTER_OPAQUE (MATERIAL_SHADINGMODELS_TOON_CHARACTER && ( MOBILE_CHARACTER_FORWARD || (!MOBILE_DEFERRED_SHADING && (MATERIALBLENDING_SOLID || MATERIALBLENDING_MASKED) && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER) ))
#pragma endregion
```

### 4.1 修复 ①：TOONSKIN `CustomData.a` round-trip

位于 `!MOBILE_USE_GBUFFER` 的 `#else` 内（仅 Forward 编译）：

```hlsl
#if MATERIAL_SHADINGMODEL_TOONSKIN
	if (GBuffer.ShadingModelID == SHADINGMODELID_TOONSKIN)
	{
		// 复现 MobileEncodeIdAndColorChannel(..., b10Bits=true) 的 6bit 量化 +
		// MobileDecodeColorChannel(..., true) 的还原, 与 Deferred 完全同构。
		float ScaledProfileID    = GBuffer.CustomData.a * 255.0 / 63.0;                   // 编码侧的额外缩放
		uint  QuantizedProfileID = (uint)round(clamp(ScaledProfileID, 0.0, 1.0) * 63.0);  // 6bit 量化
		GBuffer.CustomData.a     = QuantizedProfileID / 63.0;                             // 解码(缺失逆运算, 与 Deferred 一致)
	}
#endif
```

> 方向说明：修在 Forward 侧**复现** Deferred 的行为，而非去 `DeferredShadingCommon.ush:950` 补上逆运算 —— 后者会改变 Deferred 观感。

### 4.2 修复 (a)：toon GBuffer 位宽量化 + 常量覆盖

```hlsl
	if (ShadingModelIsToonCharacter(GBuffer.ShadingModelID))
	{
		// —— 4bit 量化 (round(v*15)/15) ——
		GBuffer.ToonBufferA.r = round(saturate(GBuffer.ToonBufferA.r) * 15.0) / 15.0;   // ShadowOffset
		GBuffer.ToonBufferA.b = round(saturate(GBuffer.ToonBufferA.b) * 15.0) / 15.0;   // CustomShadow
		GBuffer.Specular      = round(saturate(GBuffer.Specular)      * 15.0) / 15.0;
		GBuffer.Roughness     = round(saturate(GBuffer.Roughness)     * 15.0) / 15.0;

		// —— 3bit / 2bit 量化 ——
		GBuffer.ToonBufferA.g = round(saturate(GBuffer.ToonBufferA.g) * 7.0) / 7.0;     // ToonAO
		GBuffer.ToonBufferB.r = round(saturate(GBuffer.ToonBufferB.r) * 7.0) / 7.0;     // ToonIndirectSpecularMask
		GBuffer.ToonBufferC.g = round(saturate(GBuffer.ToonBufferC.g) * 3.0) / 3.0;     // NeckBlendMask

		// —— Deferred 解码时被常量覆盖的字段(GBuffer 无位置存) ——
		GBuffer.ToonBufferA.a = 1.0;                    // ToonIndirectIrradiance
		GBuffer.ToonBufferB.g = 0.0;
		GBuffer.ToonBufferB.b = 0.0;
		GBuffer.ToonBufferB.a = 0.0;
		GBuffer.ToonBufferC.r = 1.0;                    // OutlineDetail
		GBuffer.ToonBufferC.b = 1.0;                    // RimLightIntensity
		GBuffer.ToonBufferC.a = 0.03;                   // OutlineWidth

		// SpecularColor(F0) 依赖 Specular/Metallic, 上面改了 Specular 必须重算,
		// 否则 F0 还是量化前的值(对齐 DeferredShadingCommon.ush:1002 的 ComputeF0)。
		GBuffer.SpecularColor = ComputeF0(GBuffer.Specular, GBuffer.BaseColor, GBuffer.Metallic);
	}
```

> **`SpecularColor` 必须重算** —— 漏掉这一步会残留 1~4% 误差。位宽严格照抄编码侧注释，不凭记忆。

### 4.3 修复 ②：放开 toon 角色的 LuxGI

四处 guard 统一去掉 `!MATERIAL_SHADINGMODELS_TOON_CHARACTER`，改为：

```hlsl
#if ENABLE_LUX_GI && (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER
```

### 4.4 修复 ③：末尾缩放改用 ToonEnergyWeight

```hlsl
#if !MATERIALBLENDING_MODULATE
#pragma region Engine ZXB
	#if FORWARD_TOON_CHARACTER_OPAQUE
		const float ToonEnergyWeight = clamp(View.ToonEnergyWeight, -1.0f, 1.0f);
		float Weight = 1.0 + ToonEnergyWeight;
		OutColor.rgb *= Weight;
	#else
		OutColor.rgb *= ResolvedView.PreExposure;
	#endif
#pragma endregion
#endif
```

### 4.5 修复 (4)：Emissive 缩放口径

```hlsl
#if FORWARD_TOON_CHARACTER_OPAQUE
	{
		const float EmissiveToonWeight = 1.0f + clamp(View.ToonEnergyWeight, -1.0f, 1.0f);
		// EmissiveToonWeight 恒 >0(ToonEnergyWeight 已 clamp 到 [-1,1], 取 1+x 后为 [0,2]);
		// 加 1e-4 保护 ToonEnergyWeight == -1 的退化情形, 避免除零。
		Color += Emissive * (ResolvedView.PreExposure / max(EmissiveToonWeight, 1e-4f));
	}
#else
	Color += Emissive;
#endif
```

### 4.6 修复 (b)：LuxGI 移到 Combine 之前

在 `ApplyMobileToonCombineShadowColor` 调用**之前**插入：

```hlsl
	#if ENABLE_LUX_GI && (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER
		#define LUXGI_MERGED_BEFORE_COMBINE 1
		{
			// 与末尾 `OutColor.rgb *= Weight` 用同一个表达式, 保证严格抵消。
			const float LuxGIToonWeight = 1.0f + clamp(View.ToonEnergyWeight, -1.0f, 1.0f);
			Color += LuxGIExtracted / max(LuxGIToonWeight, 1e-4f);
		}
	#endif
```

对应末尾加回处必须跳过，否则重复累加：

```hlsl
#ifndef LUXGI_MERGED_BEFORE_COMBINE
	#define LUXGI_MERGED_BEFORE_COMBINE 0
#endif
#if ENABLE_LUX_GI && (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER && !LUXGI_MERGED_BEFORE_COMBINE
	OutColor.rgb += LuxGIExtracted * VertexFog.a;
#endif
```

> `LUXGI_MERGED_BEFORE_COMBINE` 只在 toon 角色那条路径里被 `#define`（外层 `#if` 已限定 toon + 非半透明 + 非 Substrate），其余路径仍走原来的末尾加回。

---

## 五、验证结果

### 5.1 最终数值（中心像素）

| 通道 | Forward | Deferred | dev% |
|---|---|---|---|
| R | 2.26923 | 2.26628 | **+0.13%** |
| G | 1.06250 | 1.05856 | **+0.37%** |
| B | 0.89445 | 0.89583 | **−0.15%** |

### 5.2 direct 分量（修复 (a) 前后）

| 量 | 修前 dev% | 修后 dev% |
|---|---|---|
| direct Diffuse | −7.8 / −7.5 / −9.2 | −0.71 / +0.47 / −1.34 |
| direct Specular | −8.2 / −8.2 / −6.7 | +3.3 / +0.4 / +2.1 |
| **direct Total** | **−7.9 / −7.8 / −8.4** | **+0.19 / +0.45 / −0.11** |

标量输入全部命中：

| 输入 | 修前 | 修后 | Deferred |
|---|---|---|---|
| ShadowOffset | 0.50000 | **0.53333** | 0.53333 ✅ |
| Specular | 0.50000 | **0.53333** | 0.53328 ✅ |
| Roughness | 0.54540 | **0.53332** | 0.53328 ✅ |

### 5.3 全链路逐项对账（1e-5 量级）

用直测值（非反解）代入：

```
Color_pre = direct + Emissive×(PreExp/W) + LuxGI/W
Final     = Color_pre × k × W
```

| 通道 | 模型值 | 实测 Combine IN | 残差 |
|---|---|---|---|
| R | 2.15266 | 2.15252 | −0.000143 |
| G | 1.07228 | 1.07228 | +0.000003 |
| B | 0.87841 | 0.87839 | −0.000022 |

实测 `k_fwd` = 0.87849 / 0.82572 / 0.84856 vs `k_def` = 0.878633 / 0.825687 / 0.848394 —— 三通道均在 1e-4 内。

### 5.4 先预测再测量（强验证）

编译**前**锁定预测值 → 实测：

| 通道 | 预测 | 实测 | 差 |
|---|---|---|---|
| R | 2.26926 | **2.26923** | 3e-5 |
| G | 1.06255 | **1.06250** | 5e-5 |

误差仅来自 ShaderPrint 的 5 位小数精度。

---

## 六、快速排查 Checklist

对比 Forward / Deferred 数值时，按序确认：

- [ ] **Deferred 侧是否只取了一半？** lighting pass 的 OutColor 探针**不含** base pass 的 emissive 写入。必须补 `Emissive × k × PreExposure`（blend state 是 `BO_Add, BF_One, BF_One`）
- [ ] **两侧探针是否在同一"已乘缩放"状态？** Deferred 的 `TotalLight *= Weight` 在 LuxGI 之前，Forward 末尾才乘。跨越该点做减法会得到无意义的差值
- [ ] **管线切换是否真的生效？** `r.Mobile.ShadingPath` 需重启；用 slot 里的 permutation marker（`MOBILE_USE_GBUFFER` 等）确认走了预期分支，而非"以为走了"
- [ ] **探针条件与真实分支是否逐字一致？** 长条件重复书写必然 drift → 抽成宏
- [ ] **split 是否关闭？** `r.Mobile.DeferredLightingSplitPass=1` 时多个 draw 会互相覆盖同一 slot；探针需加 split guard
- [ ] **GBuffer 往返的每个字段都对齐了吗？** 位宽（4/3/2/6 bit）、常量覆盖、以及**依赖它们的派生量**（如 `SpecularColor = ComputeF0(...)`）
- [ ] **ShaderPrint 多值同行是否读错位？** 用宽分隔符或拆行；数字驱动的结论落地前，在目标位置**直接打探针测那个量**，不要从别的量反解
- [ ] **编译判据是否正确？** 看日志 `Jobs assigned N, completed N (100%)` 或 SCW 进程数降到 ≤1（空闲时常驻 1 个 worker，用 `== 0` 会白等）
- [ ] **禁用 `recompileshaders all/global`**（会崩），用 `recompileshaders changed`
- [ ] **截图前必须聚焦窗口**，否则 `HighResShot` 不落盘

---

## 七、相关参考

### 引擎源码位置

| 文件 | 关键行 | 内容 |
|---|---|---|
| `UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf` | 15 | `MOBILE_CHARACTER_FORWARD 0` |
| | 113 / 114-118 | `MOBILE_USE_GBUFFER` / `DEFERRED_SHADING_PATH` |
| | 135 | `FORWARD_TOON_CHARACTER_OPAQUE`（本次新增） |
| | 378 / 380-387 | `OutColor : SV_Target0` / GBuffer MRT |
| `UE5EA/Engine/Shaders/Private/DeferredShadingCommon.ush` | 209 | `EncodeSubsurfaceColor` = `sqrt(saturate(x))` |
| | 635-667 | `MobileEncode/DecodeTwoCustomToonData`(4+4) / `ThreeCustomToonData`(3+3+2) |
| | 793 / 950 | ProfileID 编码 `×255/63` / 解码（缺逆运算） |
| | 795-798 | toon 字段位宽注释 |
| | 799-809 / 958-973 | `ShadowColor` 编码 / 解码 |
| | 952-956 / 976 | 常量覆盖 / `GBufferAO = 1` |
| `UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf` | 369-374 | toon 角色 `TotalLight *= 1+ToonEnergyWeight` |
| | 403 / 413 / 468 / 487 | LuxGI / 跳过 sky light / Combine / 描边 |
| `UE5EA/Engine/Shaders/Private/ToonMobileLightingCommon.ush` | 373-437 | `ApplyMobileToonCombineShadowColor`（436 为纯乘法返回） |
| `UE5EA/Engine/Shaders/Private/ToonShadingModels.ush` | 1248 / 1263 | `ToonBRDF` / `MappingProfileID2VW` |
| `UE5EA/Engine/Shaders/Private/Toon/ToonShadingCommon.ush` | 55-62 | `MappingProfileID2VW` 实现 |
| `UE5EA/Engine/Shaders/Private/MobileLightingCommon.ush` | 539-627 | `AccumulateReflection`（626 加进 TotalLight） |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp` | 1486 / 1644 | lighting pass 加性 blend state |
| `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp` | 2562 / 3050 | `SceneColorRendering` pass / `RequiresMultiPass()` |
| `UE5EA/Engine/Source/Runtime/Engine/Private/ShaderCompiler/ShaderCompiler.cpp` | 3521 | 注入 `MOBILE_DEFERRED_SHADING` |

### 同系列既有文档（`E:\AiDoc\`）

- `UE-Mobile-Forward比Deferred偏亮-LuxGI双重PreExposure与HYBRID天光重复-根因与修复.md`
- `UE-Mobile-Forward-LuxGI粗糙反射对齐Deferred-RoughReflection注入与NaN爆白排查.md`
- `UE-Mobile-Forward-LuxGI-Permutation化落地-P4改动汇总.md`
- `UE-Mobile-Forward-Outline-Align-Deferred.md`
- `UE-Mobile-LuxGI-Forward与Deferred效果不一致-ApplyCartoonShadow参数绑定修复.md`
- `UE-Mobile-Forward-vs-Deferred-管线全流程分析-含Shader反汇编解读.md`
- `UE5-Mobile-Forward-Path-Foliage-竖直彩色条带-ToonRamp-Profile索引-排查修复.md`
- `MobileRenderPath/UE_Mobile_Forward_vs_Deferred_Tech_Doc.md`

---

## 八、遗留事项

### 8.1 探针代码清理（需人工确认口径）

本次会话新增的探针已全部移除并验证输出逐位不变。但**更早几轮**的探针仍在：

- `MobileBasePassPixelShader.usf` slot[0..15] 采集块
- `MobileDeferredShading.usf` slot[16..31] 采集块
- `MobileBasePassShaderPrintDebug.usf`（整个文件是 `p4 add` 的新文件）
- `MobileShadingRenderer.cpp` / `MobileBasePassRendering.{h,cpp}` / `ShaderPrint.cpp` / `PostProcessing.cpp` 的绑定改动

⚠️ **不能整文件 `p4 revert`** —— 功能性修复（`#pragma region Engine ZXB` + `// [ZXB]`）与调试代码混在同一文件，需按标记逐块挑选。

### 8.2 已排除、无需处理

- **IBL / sky light 作用范围不对称**：Deferred 对 toon 角色跳过 `ReflectionEnvironmentSkyLighting`（`MobileDeferredShading.usf:413`），Forward 的 `AccumulateReflection` 没有该排除。但实测该场景 IBL 净增量 **0,0,0**，Forward 手算 `LuxRoughSpec` 也是 **0,0,0**，故当前无影响。若换到有反射捕获/天光的场景需重新评估
- **`ShadowColor` 位宽（3+3+2，蓝色仅 2bit）**：曾写过 round-trip 复现，但实测该像素走 TOONSKIN 分支时 `ShadowColor` 被 `NeckBlendMask=0` 的 lerp 完全丢弃（`ToonMobileLightingCommon.ush:412` 用 ramp 纹理覆盖），加与不加输出**逐位相同** → 已回滚。若后续测 TOONSTANDARD 或 NeckBlendMask≠0 的像素需重新考虑

### 8.3 验证覆盖面

当前结论基于**单一像素 + 单一相机位姿 + TOONSKIN**。建议补充：

- 其他 toon shading model（TOONFACE / TOONHAIR / TOONSTANDARD —— 它们在 Combine 里走**不同的 SelfShadow 分支**）
- 半透明 toon 角色（本次修复显式保持原行为不变，需确认无回归）
- 阴影区 / 描边处像素（`DirectionalLightShadow < 1`、`ToonOutlineMask > 0`）
- 实机 Android Vulkan（当前为 PC 预览平台）

### 8.4 已知无关报错

日志中的 `err0r X4510: maximum ps_5_0 sampler register index (16) exceeded` 是 **D3D SM5 预览路径的既有问题**，改动前即存在，与被测的 Android Vulkan 路径无关。
