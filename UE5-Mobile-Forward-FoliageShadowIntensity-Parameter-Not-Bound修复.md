# UE5 Mobile Forward Path — FoliageShadowIntensity Parameter Not Bound 修复

> Mobile Forward 路径（`r.Mobile.ShadingPath=0`）下，`TMobileBasePassPS` 报 `Parameter FoliageShadowIntensity not bound!`，根因是裸全局变量被编译进 `$Globals` cbuffer 但 mesh material shader 无法绑定；修复方式为将其纳入每帧刷新的 `FMobileBasePassUniformParameters` uniform buffer。

---

## 一、问题定位流程

| 步骤 | 操作 | 结论 |
|---|---|---|
| 1 | 日志报错：`Found unbound parameters being used in shadertype TMobileBasePassPSFNoLightMapPolicyLOCAL_LIGHTS_DISABLED ... Parameter FoliageShadowIntensity not bound!` | Forward base pass 的 PS 缺绑定 |
| 2 | 确认 `r.Mobile.ShadingPath = 0`（Forward 路径） | Deferred（=1）不报错 |
| 3 | 搜索 `FoliageShadowIntensity` 在 shader 端的使用点 | `MobileLightingCommon.ush:927`、`ToonDeferredLightingCommon.ush:675,680` |
| 4 | 搜索 C++ 端 `SHADER_PARAMETER(float, FoliageShadowIntensity)` 的绑定点 | 仅在 `MobileDeferredShadingPass.cpp:120`（deferred）和 `IndirectLightRendering.cpp:183`（PC deferred composite）有绑定 |
| 5 | 确认 Forward base pass 无任何地方绑定该参数 | **根因确认** |

## 二、根因分析

### 为什么 Deferred 没问题

`MobileDeferredShadingPass.cpp` 是 **Global Shader + RDG Pass**：
- RDG 参数结构体里声明了 `SHADER_PARAMETER(float, FoliageShadowIntensity)`（:120）
- 每帧渲染 deferred lighting pass 时 C++ 立即赋值（:691）
- Global Shader 的参数**每帧重新设置**，天然动态

### 为什么 Forward 有问题

`TMobileBasePassPS` 是 **Mesh Material Shader**：
- 不走 RDG pass parameters，走 Uniform Buffer 绑定
- `ToonDeferredLightingCommon.ush:625` 的 `float FoliageShadowIntensity;` 被无条件编译，生成 `$Globals` cbuffer 里的一个槽位
- 但 mesh material shader 的 `$Globals` cbuffer **没有任何 C++ 代码给它赋值**
- UE5 的 mesh draw command 被缓存后，即使想 loose bind 也无法在运行时动态更新

### 5-Why 因果链

1. 为什么报 not bound？→ shader 引用了 `$Globals.FoliageShadowIntensity`，C++ 没绑
2. 为什么 C++ 没绑？→ Forward base pass 是 mesh shader，不走 RDG pass params
3. 为什么 shader 里有这个全局变量？→ `ToonDeferredLightingCommon.ush:625` 无条件声明，被 `MobileLightingCommon.ush` include
4. 为什么 deferred 能绑？→ Deferred 是 global shader + RDG pass，参数每帧设置
5. 根本原因：**Forward 和 Deferred 的参数绑定机制不同，添加 `FoliageShadowIntensity` 时只处理了 Deferred 路径，遗漏了 Forward**

## 三、方案对比

| 方案 | 做法 | 功能完整 | 运行时动态 | 风险 | 适用 |
|---|---|---|---|---|---|
| **A（采用）** | 加入 `FMobileBasePassUniformParameters` | ✅ | ✅ 每帧刷新 | 中（需改 shader 引用） | 需要运行时通过 PPV 调整的场景 |
| B | Mesh shader loose bind | ✅ | ❌ 被 MDC 缓存固化 | 低 | 值只在关卡加载设定一次 |
| C | Shader 内 `#define` 为常量 1.0 | ❌ | ❌ | 极低 | 仅消错，不关心效果 |

**选择方案A的理由**：`FoliageShadowIntensity` 在 `SceneView.cpp:1769` 有 `LERP_PP` 处理，设计意图就是"可被 PostProcessVolume 动态混合"，运行时值会变化。

## 四、修复方案（方案A详细实现）

### 改动 1 — `MobileBasePassRendering.h`

在 `FMobileBasePassUniformParameters` 结构体末尾添加字段：

```cpp
// 位置：END_GLOBAL_SHADER_PARAMETER_STRUCT() 之前
#pragma region Engine ZXB
    SHADER_PARAMETER(float, FoliageShadowIntensity)
#pragma endregion
END_GLOBAL_SHADER_PARAMETER_STRUCT()
```

### 改动 2 — `MobileBasePassRendering.cpp`

在 `SetupMobileBasePassUniformParameters()` 函数末尾赋值：

```cpp
// 位置：SetupMobileSSRParameters 调用之后，函数右花括号之前
#pragma region Engine ZXB
    // Foliage shadow intensity for forward path (matches deferred loose parameter binding)
    BasePassParameters.FoliageShadowIntensity = Scene ? Scene->FoliageShadowIntensity : 1.0f;
#pragma endregion
```

### 改动 3 — `MobileLightingCommon.ush`

在 `#include "ToonDeferredLightingCommon.ush"` **之前**定义宏映射：

```hlsl
#pragma region Engine ZXB
// Forward base pass: FoliageShadowIntensity comes from the per-frame uniform buffer
// Must be defined BEFORE including ToonDeferredLightingCommon.ush so that all uses
// (including inside ApplyCartoonFoliage) get macro-replaced.
#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS
#define FoliageShadowIntensity MobileBasePass.FoliageShadowIntensity
#endif
#pragma endregion

#include "ToonDeferredLightingCommon.ush"
```

### 改动 4 — `ToonDeferredLightingCommon.ush`

用条件编译保护裸全局声明，避免 forward 路径下与 `#define` 冲突：

```hlsl
// cartoon foliage

#pragma region Engine ZXB
#if MOBILE_DEFERRED_SHADING || !IS_MOBILE_BASE_PASS
float FoliageShadowIntensity;
#endif
#pragma endregion
float4 FoliageIndirectColor;
```

### 宏条件逻辑验证

| 编译路径 | `MOBILE_DEFERRED_SHADING` | `IS_MOBILE_BASE_PASS` | 裸声明 | `#define` 映射 | 结果 |
|---|---|---|---|---|---|
| Forward Base Pass | 0 | 1 | 跳过 | 生效 → 从 UB 读取 | ✅ |
| Deferred Base Pass | 1 | 1 | 声明 | 不定义 → 用裸全局 | ✅ |
| Deferred Shading Pass | 1 | 0 | 声明 | 不定义 → 用裸全局 | ✅ |
| DiffuseIndirectComposite | 视情况 | 0 | 声明 | 不定义 → 用裸全局 | ✅ |

## 五、快速排查 Checklist

当遇到 Mobile Shader `Parameter XXX not bound!` 报错时：

- [ ] 确认该参数在 shader (.ush/.usf) 中的声明方式（裸全局 vs uniform buffer 字段）
- [ ] 确认当前 shader 类型（Global Shader vs Mesh Material Shader）
- [ ] 如果是 Mesh Material Shader：裸全局变量必须通过 uniform buffer 传入，不能靠 RDG pass params
- [ ] 检查 `MobileBasePassRendering.h` 中 `FMobileBasePassUniformParameters` 是否包含该参数
- [ ] 检查 `SetupMobileBasePassUniformParameters()` 中是否赋值
- [ ] 如果是 Deferred 路径可用但 Forward 不行：几乎肯定是 Forward 路径遗漏了绑定
- [ ] 注意 `#include` 顺序：宏 `#define` 必须在使用点之前定义

## 六、相关技术要点

### Forward vs Deferred 参数绑定机制差异

| | Forward (Mesh Material Shader) | Deferred (Global Shader) |
|---|---|---|
| 参数来源 | Uniform Buffer (`MobileBasePass`) | RDG Pass Parameters (`$Globals`) |
| 赋值时机 | `SetupMobileBasePassUniformParameters()` 每帧 | Pass render 函数内每帧 |
| 缓存行为 | UB 内容每帧更新，MDC 引用 UB 指针 | 无缓存，每帧重建 |
| 适合的参数类型 | 场景/View 级参数 | Pass 级参数 |

### 关键宏定义来源

| 宏 | 定义位置 | Forward Base Pass 值 | Deferred Base Pass 值 |
|---|---|---|---|
| `MOBILE_DEFERRED_SHADING` | `SceneTexturesCommon.ush:278`（默认 0）/ C++ `SetDefine` | 0 | 1 |
| `IS_MOBILE_BASE_PASS` | `MobileBasePassRendering.cpp:255` | 1 | 1 |

### 涉及文件路径

```
UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.h
UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.cpp
UE5EA/Engine/Shaders/Private/ToonDeferredLightingCommon.ush
UE5EA/Engine/Shaders/Private/MobileLightingCommon.ush
UE5EA/Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp  (参考：deferred 绑定方式)
UE5EA/Engine/Source/Runtime/Renderer/Private/ScenePrivate.h               (Scene->FoliageShadowIntensity 定义)
```

---

*文档生成时间：2026-07-09 | 作者：ZXB + AI | 项目：GR_DevTest*
