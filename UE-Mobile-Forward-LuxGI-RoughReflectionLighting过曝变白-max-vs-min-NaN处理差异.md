# UE Mobile Forward LuxGI RoughReflectionLighting 过曝变白 — max vs -min NaN 处理差异

> Forward Path 下 LuxGI 粗反射 specular（LuxRoughSpec）乘以 GetEnvBRDF 后产生 NaN，`max(NaN, 0)` 在移动 GPU 上返回 NaN 导致大片爆白；Deferred 用 `-min(-x, 0)` 规避了此问题，Forward 遗漏对齐。

---

## 一、问题定位流程

### 现象
- Forward Path 下 ToonEnvironment（ShadingModelID=7）材质区域出现大片白色过曝
- Deferred Path 同场景同材质正常

### 确认有效的排查步骤

1. **屏蔽整个 LuxGI 四块**（`#if 0` ENABLE_LUX_GI）→ 过曝消失 → 锁定 LuxGI 链路
2. **debug 输出 LuxRoughSpec**（`OutColor.rgb = LuxRoughSpecDebug`）→ 白色区域与过曝区域重合 → 锁定 LuxRoughSpec
3. **debug 输出 GetEnvBRDF 本身** → 深灰色正常值 → 排除 GetEnvBRDF 自身异常
4. **debug 输出 RoughReflectionLighting**（去掉 GetEnvBRDF 乘法）→ 过曝区域变黑色（0）→ RoughReflectionLighting 在该区域是负数或 NaN
5. **对比 Deferred dump 7246-7247 行** → 发现 Deferred 用 `-min(-x, 0)` 而非 `max(x, 0)` → 根因

### 排除的方向（一笔带过）
IBL specular、SkyLight、PreExposure Toon 分支、NoV 计算方式、ComputeLightFunctionMultiplier、方向光——逐一验证后均排除。

---

## 二、根因分析

### 2.1 Forward 和 Deferred 的代码差异（基于 dump）

**Forward（`ForwardBasePassLuxGIV3.txt:8193-8194`）**：
```hlsl
min16float3 LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV_LuxRR);
LuxRoughSpec = max(LuxRoughSpec, min16float3(0.0f, 0.0f, 0.0f));
```

**Deferred（`DeferredLightingPassLuxGIV2.txt:7246-7247`）**：
```hlsl
RoughReflectionResult *= GetEnvBRDF( GBuffer.SpecularColor, GBuffer.Roughness, NoV );
RoughReflectionResult = -min(-RoughReflectionResult, 0.0);
```

### 2.2 数学等价但不等价

数学上 `-min(-x, 0)` ≡ `max(x, 0)`：

| x 值 | max(x, 0) | -min(-x, 0) | 是否一致 |
|---|---|---|---|
| 5.0 | 5.0 | -min(-5, 0) = -(-5) = 5.0 | ✓ |
| -3.0 | 0.0 | -min(3, 0) = -(0) = 0.0 | ✓ |
| 0.0 | 0.0 | -min(0, 0) = 0.0 | ✓ |

### 2.3 NaN 处理的关键差异

`RoughReflectionLighting` 在某些像素上是 NaN（来自 GI Volume 探针 SH 反射采样的边界情况）：

| 输入 | `max(NaN, 0)` | `-min(-NaN, 0)` | 差异 |
|---|---|---|---|
| NaN | 未定义（某些移动 GPU 返回 NaN） | `-min(NaN, 0)`，min 返回非 NaN 参数 0，`-0 = 0` | **Forward 穿透，Deferred 归零** |

**D3D12 规范**：`min(a, b)` 和 `max(a, b)` 对 NaN 应返回非 NaN 参数。但**移动 GPU（特别是某些 Mali/Adreno）在 FP16 下不保证此行为**：
- `max(NaN, 0)` 可能返回 NaN
- `-min(-NaN, 0)` = `-min(NaN, 0)`，`min(NaN, 0)` 返回 0（因为 0 是非 NaN 常量参数），`-0 = 0`

Deferred 用 `-min(-x, 0)` 的写法**规避了移动 GPU 的 NaN 穿透问题**。Forward 用 `max(x, 0)` 没有规避，导致 NaN 穿透到 `LuxGIExtracted`，再被加到 `OutColor.rgb`，渲染时显示为白色。

### 2.4 NaN 的来源

`RoughReflectionLighting` 来自 `GetLuxGIFullLightingWithNonCompressedData`（dump 7485-7488 调用，7267 输出 `OutRoughReflectionLighting`）。内部经过：

1. `GetFakeGlobalLuxSH(ReflectionDir) * SkyColorM`（dump 7153）— SH 反射初始值
2. `GetIrradianceFromSparseBrickPage`（dump 7241-7246）— GI Volume 探针 Trilinear 采样
3. `lerp(GlobalRoughReflectionValue, RoughReflectionLighting, FadeRatio)`（dump 7263）— 混合

**SH 反射在反射方向接近天顶/天底时 L1 系数可能产生负值，某些边界组合下产生 NaN**（特别是 FP16 精度下 0/0 或 Inf-Inf）。

---

## 三、详细技术原理

### 3.1 Forward LuxRoughSpec 的完整计算链路（基于 dump）

| 步骤 | dump 行 | 操作 | 说明 |
|---|---|---|---|
| 1 | 7485-7488 | `GetLuxGIFullLightingWithNonCompressedData(...)` → `OutRoughReflectionLighting` | GI Volume 探针采样输出 |
| 2 | 7492 | `RoughReflectionLighting *= View_PreExposure * GBuffer.GBufferAO` | 乘 PreExposure |
| 3 | 7516 | `RoughReflectionLighting *= View_IndirectLightingColorScale` | 乘 IndirectColorScale |
| 4 | 8193 | `LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(...)` | 乘 GetEnvBRDF |
| 5 | 8194 | `LuxRoughSpec = max(LuxRoughSpec, 0)` | **这里 NaN 穿透** |
| 6 | 8195 | `LuxGIExtracted += LuxRoughSpec` | 加到 LuxGIExtracted |
| 7 | 8224 | `OutColor.rgb += LuxGIExtracted * VertexFog.a` | 输出 |

### 3.2 Deferred 的对应链路（基于 dump）

| 步骤 | dump 行 | 操作 | 说明 |
|---|---|---|---|
| 1 | 7243 | `AccumulateLuxGILighting(...)` → `RoughReflectionResult` | 同 Forward，GI Volume 探针采样 |
| 2 | 6586（函数内） | `RoughReflectionLighting *= View_PreExposure * GBuffer.GBufferAO` | 同 Forward |
| 3 | 6610（函数内） | `RoughReflectionLighting *= View_IndirectLightingColorScale` | 同 Forward |
| 4 | 7246 | `RoughReflectionResult *= GetEnvBRDF(...)` | 乘 GetEnvBRDF |
| 5 | 7247 | `RoughReflectionResult = -min(-RoughReflectionResult, 0.0)` | **NaN 正确归零** |
| 6 | 7248 | `LightAccumulator_AddSplit(DirectLighting, 0, RoughReflectionResult, ...)` | 加到 DirectLighting |

---

## 四、修复方案

**文件**：`D:\GR_DevTest\UE5EA\Engine\Shaders\Private\MobileBasePassPixelShader.usf`

**修改位置**：1295-1299 行

**修改前**：
```hlsl
min16float3 LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV_LuxRR);
LuxRoughSpec = max(LuxRoughSpec, min16float3(0.0f, 0.0f, 0.0f));
```

**修改后**：
```hlsl
min16float3 LuxRoughSpec = RoughReflectionLighting * GetEnvBRDF(GBuffer.SpecularColor, GBuffer.Roughness, NoV_LuxRR);
// [ZXB Fix] 对齐 Deferred 的取负操作(Dump 7247: RoughReflectionResult = -min(-RoughReflectionResult, 0.0))
// 而不是用 max(LuxRoughSpec, 0)。某些移动 GPU 上 max(NaN, 0) 返回 NaN 导致爆白，
// 而 -min(-x, 0) 能正确把 NaN/负值归零。
LuxRoughSpec = -min(-LuxRoughSpec, min16float3(0.0f, 0.0f, 0.0f));
```

---

## 五、快速排查 Checklist

### 当 Forward 和 Deferred 出现过曝差异时

1. **基于 dump 文件逐行对比**（不要飞到 .usf 源文件）
2. **逐行对比每一行**，包括看似"数学等价"的写法（`max` vs `-min(-x)`）
3. **NaN 排查**：如果某值在某个区域是负数或 NaN，`max` 可能穿透
4. **先看 Deferred 怎么处理**，而不是自己发明 NaN 检测方案
5. **debug 输出中间值**：用 `OutColor.rgb = 中间值` 可视化
6. **屏蔽验证**：把可疑块 `#if 0` 屏蔽，看是否消失

### NaN 规避写法对照

| 写法 | NaN 行为 | 推荐度 |
|---|---|---|
| `max(x, 0)` | 移动 GPU 可能返回 NaN | ✗ |
| `clamp(x, 0, N)` | 同上 | ✗ |
| **`-min(-x, 0)`** | min 对常量 0 稳定归零 | **✓** |

---

## 六、相关参考

### 涉及的 dump 文件
- `ForwardBasePassLuxGIV3.txt`（Forward Base Pass shader dump）
- `DeferredLightingPassLuxGIV2.txt`（Deferred Lighting Pass shader dump）

### 涉及的代码位置
- Forward 修复：`UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf:1295-1299`
- Deferred 参考：`DeferredLightingPassLuxGIV2.txt:7246-7247`
- LuxGI 探针采样：`ForwardBasePassLuxGIV3.txt:7485-7488` / `7263` / `7267`
- GetEnvBRDF 实现：`DeferredLightingPassLuxGIV2.txt:5196-5199` / `4686-4700`

### P4 信息
- Workspace: `DJANGOZHAN-PCFW_GR_DevTest`
- Depot: `//GR/DevTest/UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf`
- Base revision: #18
