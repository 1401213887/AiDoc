# UE Mobile LuxGI Forward 与 Deferred 效果不一致 — ApplyCartoonShadow 参数绑定修复

> 现象：移动端 LuxGIVolume 在 Forward 与 Deferred 路径下截帧数据/效果对不上。经排查，uniform buffer 数据本身对齐无误，真正差异在 shader 端 —— Forward base pass 未施加 `ApplyCartoonShadow`，导致间接光偏亮偏平；对齐时因该函数依赖一组全屏 pass 的 loose 全局参数，在 forward mesh pass 触发 "unbound parameters" 编译报错，需改由 base pass uniform buffer 携带并做 `#define` 路径分流。

---

## 一、问题定位流程

### 1.1 初始怀疑与逐层排除

| 排查方向 | 方法 | 结论 |
|---|---|---|
| RDG 更新时序 / pass 顺序 | 对照方向光/Toon/LuxGI 三个 Scene 级 UB 的更新机制 | ❌ 排除：四者逐字节同构（`TUniformBufferRef` + `UniformBuffer_MultiFrame` + base pass 前 `AddPass`+`UpdateUniformBufferImmediate`），同一段 `GetShaderBindings` 绑定 |
| uniform buffer 数据是否上传 | 截帧验证 A：Event Browser 搜 `UpdateLuxGIShaderUniformBuffers` marker | ✅ 有跑，update pass 在 base pass 之前执行 |
| 上传的数据是否正确 | 截帧验证 B：同一 drawcall 上看方向光/Toon cbuffer | ✅ 值对；加诊断日志确认 `GIVolumeCenter` 等在 Forward 下也正确 |
| shader 端应用逻辑 | 对比 `MobileBasePassPixelShader.usf`(Forward) 与 `MobileDeferredShading.usf`(Deferred) 的 LuxGI 调用 | ✅ **命中根因** |

### 1.2 关键判据

方向光/Toon/LuxGI 三个 Scene 级 UB **机制完全同构**。若是 RDG 时序问题，三者必然一起错。实际"只有 LuxGI 效果错、方向光/Toon 对"，在纯时序层面解释不通 → 差异只可能在 **shader 应用代码路径本身不对齐**。

---

## 二、根因分析

### 2.1 主要差异：Forward 跳过了 `ApplyCartoonShadow`

同一函数 `AccumulateLuxGILighting`，两条路径调用时**唯一实参差异是 `DeviceZ`**：

| 路径 | 调用点 | DeviceZ 传参 |
|---|---|---|
| Deferred | `MobileDeferredShading.usf:319-320` | `LookupDeviceZ(...)`（真实深度，≥0） |
| Forward | `MobileBasePassPixelShader.usf:1192` | `/*DeviceZ*/-1`（占位值） |

函数内部门槛（`MobileLightingCommon.ush:891`）：

```hlsl
if (DeviceZ >= 0.0f && !bIsCharacter)
    ApplyCartoonShadow(GBuffer, ShadowFactor, IndirectDiffuseLighting, DeviceZ);
```

- **Deferred**：DeviceZ ≥ 0 → 执行 `ApplyCartoonShadow`，LuxGI 间接漫反射被卡通阴影调制压暗。
- **Forward**：DeviceZ = -1 → 跳过 → 间接光偏亮偏平。

`ApplyCartoonShadow` 只排除 Toon 系列与 `TWOSIDED_FOLIAGE`，故 `Default Lit + Masked` 的植被（如 `M_FoliageBillboard`）正好命中生效条件 → Deferred 施加、Forward 不施加，明暗层次对不上。

源码注释 `// TODO Adapt to the forward shading pipeline`（`MobileBasePassPixelShader.usf:1188`）本身印证 Forward 的 LuxGI 是当初简化/未对齐的。

### 2.2 次要差异：透明物体的曝光/AO 处理

Forward 透明分支走 `GetLuxGIFullLightingWithNonCompressedData`，之后只乘 `View.IndirectLightingColorScale`，**未乘 `View.PreExposure` 和 `GBuffer.GBufferAO`**；而 `AccumulateLuxGILighting`（Deferred 全部 + Forward 不透明）乘了 `PreExposure * GBufferAO`。仅影响透明物体，对不透明 Masked 无影响。

### 2.3 `DeviceZ` 等价性确认（关键）

通读 `ApplyCartoonShadow`（`ToonDeferredLightingCommon.ush:554-621`）：`DeviceZ` 在函数体内**只出现在一处**（`#if APPLY_SHADOW_BORDER_COLOR` 内算 `ScreenZFactor`），且**唯一消费 `ScreenZFactor` 的那行已被注释掉**。即：

- 函数内部对 `DiffuseIndirectLighting` 的实际修改**完全不依赖 DeviceZ 的精确数值**；
- `DeviceZ` 的唯一实际作用是调用点那个 `>= 0.0f` 门槛。

**结论**：Forward 只要传任意 ≥0 的值即可让 `ApplyCartoonShadow` 生效，且与 Deferred 逐像素等价。选用 `SvPosition.z`（base pass 天然可用、恒 ≥0），比 `LookupDeviceZ`（base pass 阶段深度可能读不到自身）更稳。

---

## 三、修复引发的编译报错与二次根因

### 3.1 报错

把 Forward `DeviceZ` 改为 `SvPosition.z` 后，shader 编译报：

```
Found unbound parameters being used in shadertype TMobileBasePassPS... 
    Parameter bUseCartoonShadow not bound!
    Parameter ShadowColor not bound!
    Parameter ShadowAOColor not bound!
    Parameter ShadowStrength not bound!
    Parameter ShadowAOContrast not bound!
    Parameter ShadowIntensity not bound!
    Parameter ShadowOpacity not bound!
    Parameter ShadowAOOpacity not bound!
```

### 3.2 二次根因

`ApplyCartoonShadow` 依赖 8 个 **file-scope 全局 loose shader 参数**（`ToonDeferredLightingCommon.ush:17 / 527-539`）。这些参数：

- **Deferred**：在全屏 shading pass 通过 `FCartoonShadowParameters`（loose struct，`IndirectLightRendering.h:81`）+ `SetCartoonShadowParameters` 绑定；
- **Forward base pass（mesh pass）**：无法绑定全屏 pass 的 loose 全局参数 → unbound。

先例：同文件的 `FoliageShadowIntensity` 早已踩过同一坑，通过 base pass uniform buffer + `#if` 分流解决（`ToonDeferredLightingCommon.ush:626` 附近）。本次照此先例处理。

---

## 四、修复方案（4 文件闭环）

> 所有改动用 `#pragma region Engine ZXB` ~ `#pragma endregion` 包裹（shader 内因语法用 `//#pragma region` 注释形式）。

### 4.1 `MobileBasePassPixelShader.usf` — 传真实深度

```hlsl
// [ZXB Fix] DeviceZ 由 -1 改为 SvPosition.z，使 AccumulateLuxGILighting 内的 ApplyCartoonShadow 生效（门槛 DeviceZ>=0），对齐 Deferred。
AccumulateLuxGILighting(GBuffer, MaterialParameters.WorldPosition_CamRelative, -CameraVector,
                        DynamicShadowFactors, /*DeviceZ*/SvPosition.z,
                        MobileSceneTextures.SceneDepthTexture, MobileSceneTextures.SceneDepthTextureSampler,
                        RoughReflectionLighting, DirectLighting);
```

### 4.2 `MobileBasePassRendering.h` — base pass UB 增加 8 个参数

```cpp
#pragma region Engine ZXB
    SHADER_PARAMETER(float, FoliageShadowIntensity)
    SHADER_PARAMETER(int32, MobileForwardUseCartoonShadow)
    SHADER_PARAMETER(FLinearColor, MobileForwardShadowColor)
    SHADER_PARAMETER(FLinearColor, MobileForwardShadowAOColor)
    SHADER_PARAMETER(float, MobileForwardShadowStrength)
    SHADER_PARAMETER(float, MobileForwardShadowAOContrast)
    SHADER_PARAMETER(float, MobileForwardShadowIntensity)
    SHADER_PARAMETER(float, MobileForwardShadowOpacity)
    SHADER_PARAMETER(float, MobileForwardShadowAOOpacity)
#pragma endregion
END_GLOBAL_SHADER_PARAMETER_STRUCT()
```

### 4.3 `MobileBasePassRendering.cpp` — 填值（口径对齐 `SetCartoonShadowParameters`）

include（`ShouldRenderCartoonShadow` 声明在 Public 头 `CartoonShadowRendering.h`）：

```cpp
#pragma region Engine ZXB
#include "CartoonShadowRendering.h"
#pragma endregion
```

在 `SetupMobileBasePassUniformParameters` 内填值：

```cpp
#pragma region Engine ZXB
    BasePassParameters.FoliageShadowIntensity = Scene ? Scene->FoliageShadowIntensity : 1.0f;
    BasePassParameters.MobileForwardUseCartoonShadow = ShouldRenderCartoonShadow(View) ? 1 : 0;
    BasePassParameters.MobileForwardShadowColor      = View.FinalPostProcessSettings.CartoonShadowColor;
    BasePassParameters.MobileForwardShadowAOColor    = View.FinalPostProcessSettings.CartoonShadowAOColor;
    BasePassParameters.MobileForwardShadowStrength   = View.FinalPostProcessSettings.CartoonShadowStrengthValue;
    BasePassParameters.MobileForwardShadowAOContrast = View.FinalPostProcessSettings.CartoonShadowAOContrastValue;
    BasePassParameters.MobileForwardShadowIntensity  = View.FinalPostProcessSettings.CartoonShadowIntensity;
    BasePassParameters.MobileForwardShadowOpacity    = View.FinalPostProcessSettings.CartoonShadowOpacity;
    BasePassParameters.MobileForwardShadowAOOpacity  = View.FinalPostProcessSettings.CartoonShadowAOOpacity;
#pragma endregion
```

### 4.4 `ToonDeferredLightingCommon.ush` — 按路径分流全局参数

`bUseCartoonShadow`（第 17 行区）：

```hlsl
#pragma region Engine ZXB
#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS
#define bUseCartoonShadow MobileBasePass.MobileForwardUseCartoonShadow
#else
int bUseCartoonShadow;
#endif
#pragma endregion
```

其余 7 个（第 527 行区）：

```hlsl
#pragma region Engine ZXB
#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS
#define ShadowColor      MobileBasePass.MobileForwardShadowColor
#define ShadowAOColor    MobileBasePass.MobileForwardShadowAOColor
#define ShadowStrength   MobileBasePass.MobileForwardShadowStrength
#define ShadowAOContrast MobileBasePass.MobileForwardShadowAOContrast
#define ShadowIntensity  MobileBasePass.MobileForwardShadowIntensity
#define ShadowOpacity    MobileBasePass.MobileForwardShadowOpacity
#define ShadowAOOpacity  MobileBasePass.MobileForwardShadowAOOpacity
#else
float4 ShadowColor;
float4 ShadowAOColor;
float ShadowStrength;
float ShadowAOContrast;
float ShadowIntensity;
float ShadowOpacity;
float ShadowAOOpacity;
#endif
#pragma endregion
float4 CloudShadowColor;   // 交错在中间、不属于本次改动的参数保持原样
float ShadowBorderBlendScale;
float ShadowCloudContrast;
float ShadowBorderContrast;
float ShadowBorderContrastCut;
float ShadowDistanceUnit;
```

**安全性验证**（`#define` 是 token 全局替换，须防误伤同名局部）：大小写敏感全文搜索确认这 7 个 token 在本文件**只出现在全局声明区 + `ApplyCartoonShadow` 内部使用（604-621）**，无任何同名局部变量声明 → `#define` 安全。交错其间的 `CloudShadowColor`/`ShadowBorderBlendScale` 等未在报错列表、保持不动。

### 4.5 取值来源对照（Deferred 侧 `SetCartoonShadowParameters`，`IndirectLightRendering.h:115`）

| 本次 UB 参数 | 取值来源 |
|---|---|
| `MobileForwardUseCartoonShadow` | `ShouldRenderCartoonShadow(View)` |
| `MobileForwardShadowColor` | `View.FinalPostProcessSettings.CartoonShadowColor` |
| `MobileForwardShadowAOColor` | `View.FinalPostProcessSettings.CartoonShadowAOColor` |
| `MobileForwardShadowStrength` | `View.FinalPostProcessSettings.CartoonShadowStrengthValue` |
| `MobileForwardShadowAOContrast` | `View.FinalPostProcessSettings.CartoonShadowAOContrastValue` |
| `MobileForwardShadowIntensity` | `View.FinalPostProcessSettings.CartoonShadowIntensity` |
| `MobileForwardShadowOpacity` | `View.FinalPostProcessSettings.CartoonShadowOpacity` |
| `MobileForwardShadowAOOpacity` | `View.FinalPostProcessSettings.CartoonShadowAOOpacity` |

---

## 五、快速排查 Checklist

- [ ] 现象是"截帧数据不一致"还是"效果不一致"？先加诊断日志/截帧确认 **uniform buffer 数据是否已对齐**，再看 shader。
- [ ] 对比 Forward/Deferred 调用同一 shading 函数时的**实参差异**（本例仅 `DeviceZ` 不同）。
- [ ] 判断 RDG 时序可用"同构 UB 对照法"：找一个机制完全相同、已知正确的 UB（如方向光），若它对而目标错，则可排除时序。
- [ ] shader 报 `unbound parameters`：确认该参数是**全屏 pass 的 loose 全局** 还是 **base pass uniform buffer 成员**；forward mesh pass 只能用后者。
- [ ] 用 `#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` 做路径分流，forward `#define` 到 UB、其它路径保留原全局声明。
- [ ] `#define` 通用名前，**大小写敏感全文搜索**确认无同名局部变量会被误伤。
- [ ] C++ 侧字段名（`CartoonShadow*Value` 等）拼写靠 **C++ 编译**验证，shader 重编查不出；两侧都要编。
- [ ] 改引擎源码前按规范用对应 P4 client 迁出（本工作区 `d:/GR_DevTest` → `DJANGOZHAN-PCFW_GR_DevTest`）。

---

## 六、相关文件与符号索引

| 文件 | 关键位置 | 说明 |
|---|---|---|
| `Engine/Shaders/Private/MobileBasePassPixelShader.usf` | ~1192 | Forward LuxGI 调用点，`DeviceZ` 传参 |
| `Engine/Shaders/Private/MobileDeferredShading.usf` | 319-320 | Deferred LuxGI 调用点（真实 DeviceZ） |
| `Engine/Shaders/Private/MobileLightingCommon.ush` | 852-891 | `AccumulateLuxGILighting`，DeviceZ≥0 门槛 |
| `Engine/Shaders/Private/ToonDeferredLightingCommon.ush` | 17 / 527-556 / 554-621 | 全局参数声明 + `ApplyCartoonShadow` 实现 |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.h` | ~78-92 | base pass UB 结构 `FMobileBasePassUniformParameters` |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.cpp` | `SetupMobileBasePassUniformParameters` (~445) | UB 填值 |
| `Engine/Source/Runtime/Renderer/Private/IndirectLightRendering.h` | 81-149 | `FCartoonShadowParameters` + `SetCartoonShadowParameters`（取值口径基准） |
| `Engine/Source/Runtime/Renderer/Private/LightRendering.h` | 36-41 | `FCartoonShadowUniformStruct`（另一处方向光 cartoon shadow UB） |
| `Engine/Source/Runtime/Renderer/Public/CartoonShadowRendering.h` | 7 | `ShouldRenderCartoonShadow` 声明（Public） |
| `Engine/Source/Runtime/Renderer/Private/Toon/CartoonShadowRendering.cpp` | 33 | `ShouldRenderCartoonShadow` 定义 |
| `Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp` | `UpdateLuxGIUniformBuffers` / `SetupMobileLuxGIUniformParameters` | LuxGI UB 更新（已加/可删的诊断日志） |

---

## 七、遗留 / 待验证

- **透明物体差异**（2.2）本次未处理：如需完全对齐，Forward 透明分支需补 `* View.PreExposure * GBuffer.GBufferAO`。
- **`MobileShadingRenderer.cpp` 诊断日志**（`[ZXB LuxGI]`）为临时排查用，验证完可整段删除。
- **编译验证**：C++ 侧需增量编译确认字段名与 include 正确；shader 侧需重编确认 unbound 报错消失且无新报错。
- **P4 提交注意**：`ToonDeferredLightingCommon.ush` 存在 `must sync/resolve #9` 且被他人（lemonxqyan）同时 opened，提交时需先 resolve。
