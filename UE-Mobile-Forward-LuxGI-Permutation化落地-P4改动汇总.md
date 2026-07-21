# UE Mobile Forward LuxGI Permutation 化落地 —— P4 改动汇总

> **归档日期**: 2026-07-21
> **项目**: `d:/GR_DevTest/S1Game` + `UE5EA` (UE 5.5.4 licensed fork, `++GR+DevTest`)
> **P4 workspace**: `DJANGOZHAN-PCFW_GR_DevTest`
> **本次改动 stream**: `//GR/DevTest`
> **产出方式**: 直接从 P4 `default` changelist 的迁出文件反向汇总（物证驱动，不受对话上下文压缩影响）
> **主题范围**: 仅覆盖 **LuxGI 的 permutation 化 + MDC sink + View flag 双保险** 一条主线；同批 P4 default 里的 EnvBRDF 能量守恒修复是独立子主题，本文不涉及，未来另立专文。
> **相关归档**:
> - `UE-Mobile-LuxGI-Forward与Deferred效果不一致-ApplyCartoonShadow参数绑定修复.md` (2026-07-17)
> - `UE-Mobile-Deferred-Preview-CartoonShadow参数unbound根因与IS_MOBILE_BASE_PASS分流修复.md` (2026-07-18)
> - `UE-Mobile-Forward比Deferred偏亮-LuxGI双重PreExposure与HYBRID天光重复-根因与修复.md` (2026-07-18)
> - `UE-Mobile-Forward-vs-Deferred-管线全流程分析-含Shader反汇编解读.md` (2026-07-20)
> - `UE-Mobile-Forward-CVar切换Shader-Permutation不生效-MDC缓存与RecreateRenderState修复.md` (2026-07-21，本文档的姊妹篇，专讲 MDC 破局故事)

---

## 0. TL;DR（给未来自己的一句话）

**Mobile Forward Base Pass 想让 `r.LuxGI` 真正生效**，得同时做四件事：
1. **编译期 strip** —— 用 `SHADER_PERMUTATION_BOOL` 造 `FEnableLuxGI` 维度，shader 里所有 LuxGI 代码块套 `#if ENABLE_LUX_GI`；
2. **运行时挑变体** —— `MobileBasePass::GetShaders` 每帧读 CVar 算 permutation id，层层透传到 `AddShaderType<TMobileBasePassPS<...>>(PermutationId)`；
3. **MDC 感知切值** —— 用**多播委托** `OnChangedDelegate().AddLambda(...)` 给 `r.LuxGI` 挂 sink（**不能用** `FAutoConsoleVariableRef` 带 lambda 构造——那走的是 `SetOnChangedCallback` 单 slot legacy API，会被本 CU 后面 `FLuxIrradianceVolumeSceneData::UpdateFromWorld` 里的 `SetOnChangedCallback` 覆盖挤掉；详见 §2.4.1），切值时 sink 里构造 `FGlobalComponentRecreateRenderStateContext` 触发 CachedMeshDrawCommand 全量重建；
4. **双保险 View flag** —— 同一路 CVar 读取也在 `SceneRendering.cpp` 把 `View.StaticLightingMethod` 强制拉成 `LIGHTMAP_ONLY`,让 shader 里的 `BRANCH` 运行时也会跳过（即使编译期没剪干净）。

---

## 1. 改动全景（LuxGI 主线相关文件）

```
//GR/DevTest/UE5EA/Engine/Shaders/Private/MobileBasePassPixelShader.usf                     - shader 源码：三块 LuxGI 加 ENABLE_LUX_GI 守卫
//GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.h         - 新增 FEnableLuxGI permutation 维度
//GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp                - GetShaders 里读 CVar 算 permutation id，层层透传
//GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/LuxMobileGI/LuxGIRendering.cpp    - r.LuxGI 挂 sink，切值触发 MDC 重建
//GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/SceneRendering.cpp                - View.StaticLightingMethod 双保险
//GR/DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/WaterInfoTextureRendering.cpp     - 两个调用点跟进 permutation id 显式=0
```

> 说明：`MobileBasePassPixelShader.usf` 在同一次 P4 迁出里还有一处 EnvBRDF 能量守恒的修复（独立主题，与 LuxGI permutation 化正交），本文档不再涉及。

**逻辑分层**：

```
       ┌────────────────────────────────────────────────────────┐
       │ Shader 源码层  MobileBasePassPixelShader.usf           │
       │   #if ENABLE_LUX_GI && ...   (三块 LuxGI 代码 + 主调用) │
       └───────────────────────▲───────────────────────────────┘
                               │ SetDefine("ENABLE_LUX_GI", ...)
       ┌───────────────────────┴────────────────────────────────┐
       │ Shader 类声明层  MobileBasePassRendering.h              │
       │   class FEnableLuxGI : SHADER_PERMUTATION_BOOL(...)     │
       │   FPermutationDomain / ModifyCompilationEnvironment     │
       └───────────────────────▲───────────────────────────────┘
                               │ AddShaderType<TMobileBasePassPS<...>>(PermutationId)
       ┌───────────────────────┴────────────────────────────────┐
       │ Shader 挑选层  MobileBasePass.cpp                       │
       │   GetShaders(): 读 r.LuxGI → PermutationId_ZXB          │
       │   层层透传到 GetUniformMobileBasePassShaders / Get...   │
       │   WaterInfoTextureRendering.cpp: 两处调用点补 id=0       │
       └───────────────────────▲───────────────────────────────┘
                               │ 每帧被 MDC 缓存 → 需要 sink 触发重建
       ┌───────────────────────┴────────────────────────────────┐
       │ CVar sink 层  LuxGIRendering.cpp                        │
       │   FConsoleVariableDelegate::CreateLambda([](...){       │
       │     FGlobalComponentRecreateRenderStateContext Context; │
       │   })                                                    │
       └────────────────────────────────────────────────────────┘

       ┌────────────────────────────────────────────────────────┐
       │ 双保险 View flag 层  SceneRendering.cpp                 │
       │   if (r.LuxGI==0) nStaticLightingMethod = LIGHTMAP_ONLY │
       │   → shader 里 BRANCH `if (View.StaticLightingMethod!=` │
       │     `LIGHTMAP_ONLY)` 运行时跳过 AccumulateLuxGILighting │
       └────────────────────────────────────────────────────────┘
```

---

## 2. 逐文件详解

### 2.1 `MobileBasePassPixelShader.usf`（shader 源码层）

**共通模式**：所有原来长这样的条件

```hlsl
#if (MATERIALBLENDING_MASKED || MATERIALBLENDING_SOLID) && !MATERIAL_SHADINGMODEL_UNLIT && !MATERIAL_SHADINGMODELS_TOON_CHARACTER && !MATERIAL_SHADINGMODEL_SINGLELAYERWATER && !MOBILE_USE_GBUFFER
```

都被改成：

```hlsl
#if ENABLE_LUX_GI && (MATERIALBLENDING_MASKED || ...) && ... && !MOBILE_USE_GBUFFER
```

覆盖以下三块 + 一处主调用：

| 位置 | 原逻辑 | 加守卫后 |
|------|--------|---------|
| `@@ -1177 +1191` | `ExposureAffectedLight_ZXB` 快照（进入 LuxGI 前记录 TotalLight） | `#if ENABLE_LUX_GI && ...` |
| `@@ -1206 +1222` | **主调用 `AccumulateLuxGILighting(...)`**（原来只有 runtime `BRANCH` + `if (View.StaticLightingMethod != 0)`，没编译期剪枝） | 整块 `#if ENABLE_LUX_GI && ... #endif` |
| `@@ -1240 +1266` | `LuxGIExtracted_ZXB = TotalLight - ExposureAffectedLight_ZXB`（剥离 LuxGI 净增量避免末尾双 PreExposure） | `#if ENABLE_LUX_GI && ...` |
| `@@ -1516 +1543` | `OutColor.rgb += LuxGIExtracted_ZXB * VertexFog.a`（末尾把剥离的 LuxGI 加回） | `#if ENABLE_LUX_GI && ...` |

**关键细节**：主调用块（第 2 行）之前**只有运行时 BRANCH 保护**：

```hlsl
BRANCH
if (View.StaticLightingMethod != STATIC_LIGHTING_LIGHTMAP_ONLY)
{
    AccumulateLuxGILighting(GBuffer, ...);
}
```

**这意味着**：即使 `r.LuxGI=0`，DXIL 里 `AccumulateLuxGILighting` 分支代码仍然存在，只是运行时被 skip；观感上像"permutation 维度没生效"。加 `#if ENABLE_LUX_GI` 后，编译期就把整块剪掉，与 Deferred 侧 `FEnableLuxGI` permutation 行为完全对齐。

**为什么四块必须条件严格一致**：`LuxGIExtracted_ZXB` 是一个局部变量，声明在第 3 块、引用在第 4 块。如果第 3 块被剪掉但第 4 块保留，编译报 undefined。所以四处的 `#if` 条件（含 `ENABLE_LUX_GI &&`）必须字节级同步。

---

### 2.2 `MobileBasePassRendering.h`（shader 类声明层）

**新增 permutation 维度**：

```cpp
template <...>
class TMobileBasePassPS : public TMobileBasePassPSBaseType<LightMapPolicyType>
{
    DECLARE_SHADER_TYPE(TMobileBasePassPS, MeshMaterial);

#pragma region Engine ZXB
    // 与 MobileDeferredShadingPass.cpp:221 `FEnableLuxGI` 命名/语义完全对齐
public:
    class FEnableLuxGI : SHADER_PERMUTATION_BOOL("ENABLE_LUX_GI");
    using FPermutationDomain = TShaderPermutationDomain<FEnableLuxGI>;
#pragma endregion
public:
    // ...
```

**`ModifyCompilationEnvironment` 里注入宏**：

```cpp
static void ModifyCompilationEnvironment(...)
{
    // 原有 SetDefine 逻辑保留
    OutEnvironment.SetDefine(TEXT("USE_SPARSE_STORAGE"), IsLuxGIUsingSparseStorage() ? 1u : 0u);

#pragma region Engine ZXB
    // 从 permutation vector 读取，把编译期宏值和运行时选中的 permutation id 绑定
    FPermutationDomain PermutationVector(Parameters.PermutationId);
    OutEnvironment.SetDefine(TEXT("ENABLE_LUX_GI"),
        PermutationVector.template Get<FEnableLuxGI>() ? 1u : 0u);
#pragma endregion
}
```

**为什么不用 `static const CVar + SetDefine` 的老套路？**
- 老套路：`ModifyCompilationEnvironment` 里直接读 `IConsoleManager::Get().FindConsoleVariable(TEXT("r.LuxGI"))->GetInt()` SetDefine；
- 问题：DDC key 不感知 CVar 值，首次编译后 CVar 变了 DDC 也不重编 → 一次锁死；
- 正解：把 CVar 转成 permutation 维度，permutation id 天然入 DDC key，两个变体都编出来、运行时 dispatch。

**参考对齐点**：`MobileDeferredShadingPass.cpp:221-238` 的 `FMobileDeferredShadingPS::FEnableLuxGI`，本次改动的命名/位置/语义与它完全一致，方便后续维护。

---

### 2.3 `MobileBasePass.cpp`（Shader 挑选/dispatch 层）

**核心新增**：`MobileBasePass::GetShaders` 入口每帧读 CVar 算 permutation id：

```cpp
#pragma region Engine ZXB
    // 与 MobileDeferredShadingPass.cpp:422-425 同一开关 r.LuxGI 的运行时选 permutation
    int32 PermutationId_ZXB = 0;
    {
        static const auto CVarLuxGI_ZXB = IConsoleManager::Get().FindConsoleVariable(TEXT("r.LuxGI"));
        const bool bLuxGIEnabled_ZXB = CVarLuxGI_ZXB ? (CVarLuxGI_ZXB->GetInt() != 0) : true;
        // 借用 LMP_NO_LIGHTMAP + LOCAL_LIGHTS_DISABLED + DEFAULT 这一模板实例的 FPermutationDomain 计算 id
        // FEnableLuxGI 是 TMobileBasePassPS 类内公用维度，各模板实例的 id 数值等价
        using FPS = TMobileBasePassPS<TUniformLightMapPolicy<LMP_NO_LIGHTMAP>,
                                       EMobileLocalLightSetting::LOCAL_LIGHTS_DISABLED,
                                       false,
                                       EMobileTranslucentColorTransmittanceMode::DEFAULT>;
        FPS::FPermutationDomain PermutationVector;
        PermutationVector.Set<FPS::FEnableLuxGI>(bLuxGIEnabled_ZXB);
        PermutationId_ZXB = PermutationVector.ToDimensionValueId();
    }
#pragma endregion
```

**函数签名扩展**（两个层级）：
- `GetUniformMobileBasePassShaders<Policy, LocalLightSetting, bEnableLuxGIAvoidLightLeaking>(...)` 增加 `int32 PermutationId_ZXB` 参数
- `GetMobileBasePassShaders<LocalLightSetting, bEnableLuxGIAvoidLightLeaking>(LightMapPolicyType, ...)` 同样增加 `PermutationId_ZXB`

**调用点透传**：`GetShaders` 内 6 个 switch case（`LMP_NO_LIGHTMAP` / `LMP_LQ_LIGHTMAP` / `LMP_MOBILE_DIRECTIONAL_LIGHT_CSM` × `bEnableLuxGIAvoidLightLeaking` 两条分支）全都把 `PermutationId_ZXB` 透传下去。

**最终落点**：

```cpp
ShaderTypes.AddShaderType<TMobileBasePassPS<TUniformLightMapPolicy<Policy>, LocalLightSetting, bEnableLuxGIAvoidLightLeaking, EMobileTranslucentColorTransmittanceMode::DEFAULT>>(PermutationId_ZXB);
```

（原来 `AddShaderType<...>()` 不传 id 默认 0，现在传 `PermutationId_ZXB`。）

**为什么"借用 `LMP_NO_LIGHTMAP` + `LOCAL_LIGHTS_DISABLED` + `DEFAULT` 这个模板实例"来算 permutation id 是安全的？**
- `FEnableLuxGI` 是 `TMobileBasePassPS` **模板类内部**的 permutation 维度，所有实例化（不同 Policy/LocalLightSetting/Transmittance）都是**同一个 permutation vector 结构**；
- `PermutationVector.ToDimensionValueId()` 只依赖 vector 的维度组合（这里只有一个 bool），跟哪个模板实例无关；
- 所以算一次、给所有模板实例共用是等价的，节省重复代码。

---

### 2.4 `LuxGIRendering.cpp`（CVar sink 层 —— **本次核心破局点**）

**问题背景**：即使 2.1~2.3 全都到位，运行时 `r.LuxGI 0` → `r.LuxGI 1` 切换观感仍然不生效。原因是 **CachedMeshDrawCommand (MDC)**：
- Mobile Forward 走 `FMeshDrawCommand` 静态缓存，`FMeshDrawCommand::PipelineId` 在 primitive attach / material recompile / VF 变化时构建，一次构建缓存到 primitive 的 static mesh；
- 后续每帧渲染直接从 MDC 取 PSO，**不会再调 `MobileBasePass::GetShaders`**；
- 切 `r.LuxGI` 只改 CVar 值，MDC 里 pin 的还是首次构建时那份 permutation id 的 shader。

**最终落地代码（2026-07-21 定稿）**：

```cpp
#pragma region Engine ZXB
// [ZXB Fix] r.LuxGI 切值时强制所有 primitive 重建 render state，
// 触发 CachedMeshDrawCommand 全量重建，让移动端 Forward Base Pass
// 按新 r.LuxGI 值重挑 FEnableLuxGI permutation。
#include "ComponentRecreateRenderStateContext.h"
#pragma endregion

int32 GAllowLuxGI = 0;
static FAutoConsoleVariableRef CVarAllowLuxGI(
    TEXT("r.LuxGI"),
    GAllowLuxGI,
    TEXT("If zero, LuxGI would be disabled."),
    ECVF_RenderThreadSafe | ECVF_Scalability
);
#pragma region Engine ZXB
// [ZXB Fix] 用多播 OnChangedDelegate().AddLambda 挂 MDC 重建 sink，
// 原因见下方 ↓ 「SetOnChangedCallback 单 slot 陷阱」。
static FDelegateHandle GLuxGIRecreateRSHandle_ZXB = CVarAllowLuxGI->OnChangedDelegate().AddLambda(
    [](IConsoleVariable* /*InVariable*/)
    {
        FGlobalComponentRecreateRenderStateContext Context;
    }
);
#pragma endregion
```

> **为什么是上述最终形态，而非直觉上更简洁的 `FAutoConsoleVariableRef(..., FConsoleVariableDelegate::CreateLambda(...), ...)`？**
> —— 详见下方子节。

#### 2.4.1 `SetOnChangedCallback` 单 slot 陷阱（次生根因）

**初版代码**（无效）用了 `FAutoConsoleVariableRef` 的"带 Callback"构造：

```cpp
// ❌ 看着能跑，但运行时 sink 永远不会被触发
static FAutoConsoleVariableRef CVarAllowLuxGI(
    TEXT("r.LuxGI"), GAllowLuxGI,
    TEXT("If zero, LuxGI would be disabled."),
    FConsoleVariableDelegate::CreateLambda([](IConsoleVariable*) {
        FGlobalComponentRecreateRenderStateContext Context;  // ← 永远不跑
    }),
    ECVF_RenderThreadSafe | ECVF_Scalability
);
```

**根因**：`FAutoConsoleVariableRef` 带 delegate 的构造内部走的是 `IConsoleVariable::SetOnChangedCallback(...)`，而 UE 的这个 legacy API（`ConsoleManager.cpp:392`）实现是：

```cpp
// Core/Private/HAL/ConsoleManager.cpp:392-396
virtual void SetOnChangedCallback(const FConsoleVariableDelegate& Callback)
{
    OnChangedCallback.Remove(LegacyDelegateHandle);  // ← 先踢掉上一次的
    LegacyDelegateHandle = OnChangedCallback.Add(Callback); // ← 只留新的
}
```

**只维护一个 `LegacyDelegateHandle`** —— 后手 `SetOnChangedCallback` 会先把前一次 Remove 再 Add。

而**同一 CU 后面** `FLuxIrradianceVolumeSceneData::UpdateFromWorld()`（世界/Scene 创建时被调）**又对同一 `r.LuxGI` 调了一次 `SetOnChangedCallback`**：

```cpp
// LuxGIRendering.cpp:589（原厂逻辑，世界创建时执行）
CVarAllowLuxGI->SetOnChangedCallback(
    FConsoleVariableDelegate::CreateRaw(this, &FLuxIrradianceVolumeSceneData::OnLuxGIStateChanged));
```

时序：

```
[静态初始化]   Add Lambda(MDC 重建)      → LegacyHandle = h1
                                                     ↓
[World 创建]   Remove h1（Lambda 被挤掉！）
               Add OnLuxGIStateChanged   → LegacyHandle = h2
                                                     ↓
[运行 r.LuxGI 0] 只调 OnLuxGIStateChanged（且函数体是注释掉的空函数）
                  Lambda 里的 FGlobalComponentRecreateRenderStateContext 永远不会跑
```

现象就是"看着代码在，但触发不到"，与初始怀疑的 `ECVF_RenderThreadSafe` / 静态初始化顺序 / 构造函数签名等无关。

#### 2.4.2 正确写法：多播 `OnChangedDelegate().AddLambda`

改用**多播委托** `IConsoleVariable::OnChangedDelegate().AddLambda(...)` —— 它 Add 到的是多播 slot，返回的 `FDelegateHandle` **与 `LegacyDelegateHandle` 无关**，legacy 路径的 `SetOnChangedCallback` 覆盖时碰不到我们：

```cpp
// ✅ 多播 slot 与 legacy 链独立，不会被别处 SetOnChangedCallback 挤掉
static FDelegateHandle GLuxGIRecreateRSHandle_ZXB =
    CVarAllowLuxGI->OnChangedDelegate().AddLambda(
        [](IConsoleVariable* /*InVariable*/)
        {
            FGlobalComponentRecreateRenderStateContext Context;
        }
    );
```

**同一 TU 内静态变量初始化顺序安全**：`CVarAllowLuxGI` 声明在前，`GLuxGIRecreateRSHandle_ZXB` 在后，C++ 标准保证顺序初始化，`->OnChangedDelegate()` 时 CVar 对象一定已就绪。

**参考模式**：UE 渲染代码中所有"和别人共存、不能被挤掉"的 CVar sink 都走这条路径——`SourceControlViewportModule.cpp:23`、`VirtualizationManager.cpp:1378`、`BlueprintNamespaceHelper.cpp:259`。

#### 2.4.3 验证注意事项

- **初版**（`FAutoConsoleVariableRef` 带 lambda 构造）**编译可过、看代码对、但运行时就是不动**。验证手段：在 lambda 里加 `UE_LOG`（或断点），切 `r.LuxGI` 看是否输出。如果没输出 → 再检查同 CVar 是否还被别处 `SetOnChangedCallback` 挂过。
- 此坑对 Lumen / Nanite 之类只有一个注册点的 CVar 不触发，只在"静态初始化挂了 A、运行时某处又覆盖成 B"场景下暴露。
- 见 memory `12492978` 记录此坑完整分析。

**FGlobalComponentRecreateRenderStateContext 工作原理**：
- 构造：遍历 `GetObjectsOfClass<UActorComponent>`，对有 render state 的 component 调用 `DestroyRenderState_Concurrent()`；
- 析构（`Context` 出作用域）：对同一批 component 调用 `RecreateRenderState_Concurrent()`；
- 结果：所有 primitive 的 `FPrimitiveSceneProxy` 被销毁重建，static mesh 的 MDC 一并失效重建，下次帧渲染时重新调 `GetShaders` → 读新 `r.LuxGI` 值 → 挑新 permutation。

**对 Deferred 侧的副作用**：无。Deferred 走全屏 `FMobileDeferredShadingPS`（不进 MDC），每帧动态拿新 permutation，多做一次 recreate 无害。

---

### 2.5 `SceneRendering.cpp`（View flag 双保险层）

**位置**：`FViewInfo::SetupUniformBufferParameters` 内，`ViewUniformShaderParameters.StaticLightingMethod = nStaticLightingMethod;` 赋值之前。

```cpp
    // ...原有 nStaticLightingMethod 计算逻辑...

#pragma region Engine ZXB
    // [ZXB] Make the Forward mobile base pass honor r.LuxGI as well.
    // When r.LuxGI == 0, force StaticLightingMethod to lightmap-only so that the
    // runtime BRANCH `if (View.StaticLightingMethod != STATIC_LIGHTING_LIGHTMAP_ONLY)`
    // in MobileBasePassPixelShader.usf skips AccumulateLuxGILighting, matching the
    // Deferred side where the ENABLE_LUX_GI permutation strips the GI path entirely.
    static const auto CVarLuxGI = IConsoleManager::Get().FindConsoleVariable(TEXT("r.LuxGI"));
    if (CVarLuxGI && CVarLuxGI->GetInt() == 0)
    {
        nStaticLightingMethod = LuxGI::SLM_LIGHTMAP_ONLY;
    }
#pragma endregion

    ViewUniformShaderParameters.StaticLightingMethod = nStaticLightingMethod;
```

**动机 —— 三重保护**：

| 层级 | 保护点 | r.LuxGI=0 时 |
|------|-------|-------------|
| L1 编译期 permutation | `#if ENABLE_LUX_GI` | shader 里 LuxGI 代码块整段消失（DXIL 找不到 `AccumulateLuxGILighting`） |
| L2 运行时 BRANCH | `if (View.StaticLightingMethod != LIGHTMAP_ONLY)` | 即使 L1 未生效（比如 MDC 没重建、还在跑旧 permutation），运行时也会 skip |
| L3 View flag 强制 | 本次 `SceneRendering.cpp` 改动 | 从数据源强制 `StaticLightingMethod = LIGHTMAP_ONLY`，让 L2 一定命中 skip 分支 |

**为什么 L2 不够 / 必须加 L3**：`View.StaticLightingMethod` 原本由 `LuxGI::SetStaticLightingMethodFromCVar` 之类函数根据一系列条件（有无 lightmap、View 是否 valid、SLM_HIT_LIGHTMAP 优先级等）算出，`r.LuxGI` 未必是唯一输入。不加 L3 的话，可能出现 "`r.LuxGI=0` 但 `StaticLightingMethod` 因其他原因仍然是 `SLM_HYBRID_LIGHTMAP_ONLY`" → shader BRANCH 不 skip → 仍然跑 LuxGI 分支。

**为什么保留 L2 而不去掉**：L2 是 shader 自带的既有逻辑（原来就有的 lightmap-only 快速路径），不动它是最小改动。L3 是把 `r.LuxGI` 这个新维度合并进 L2 的判定输入。

---

### 2.6 `WaterInfoTextureRendering.cpp`（调用点兼容层）

Water info 是一个**不走 LuxGI** 的独立 shader pass，但它用到了 `TMobileBasePassPS` 模板。2.2 加了 permutation 维度后，这里两处调用点必须显式传 `permutation id = 0`：

```cpp
#pragma region Engine ZXB
    // TMobileBasePassPS 现在带 FEnableLuxGI 维度，water info pass 不走 LuxGI，
    // 显式传 permutation id = 0（ENABLE_LUX_GI=false）保持既往行为字节级不变
    {
        using FPS = TMobileBasePassPS<TUniformLightMapPolicy<LMP_NO_LIGHTMAP>,
                                       LOCAL_LIGHTS_DISABLED,
                                       false /* bEnableLuxGIAvoidLightLeaking */>;
        FPS::FPermutationDomain PermutationVector;
        PermutationVector.Set<FPS::FEnableLuxGI>(false);
        ShaderTypes.AddShaderType<FPS>(PermutationVector.ToDimensionValueId());
    }
#pragma endregion
```

两处调用点（`@@ -235 +244` 和 `@@ -330 +339`）分别对应 water info 的 static mesh pass 和 dynamic mesh pass，改法完全一致。

**为什么不直接 `AddShaderType<FPS>(0)`？**
- 语义清晰：`PermutationVector.Set<FPS::FEnableLuxGI>(false)` 明确表达"我知道这是 permutation 维度、我选 false 的那个变体"；
- 未来兼容：如果后续 `TMobileBasePassPS` 又加了别的 permutation 维度，`ToDimensionValueId()` 会自动算出正确的 id；硬编码 `0` 就会静默错误挑到"新维度 = false + LuxGI = false"这个组合，而作者可能只想改变新维度。

---

## 3. 完整改动因果链

```
                     [用户操作]
                          │
                          ▼
                r.LuxGI 0 → 1  或  r.LuxGI 1 → 0
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
    ┌────────────────────┐   ┌────────────────────┐
    │ CVar sink 被触发   │   │ 下一帧构建 View UB  │
    │ (2.4)              │   │ (2.5)              │
    │                    │   │                    │
    │ FGlobalComponent   │   │ 如果 r.LuxGI=0:    │
    │ RecreateRender     │   │  强制 nStaticLighting│
    │ StateContext       │   │  Method = LIGHTMAP_ │
    │ 全场景 primitive   │   │  ONLY              │
    │ 重建 MDC           │   │                    │
    └──────────┬─────────┘   └──────────┬─────────┘
               │                        │
               ▼                        ▼
    ┌────────────────────┐   ┌────────────────────┐
    │ MDC 全量失效       │   │ View.StaticLighting │
    │ 下次绘制走 GetShaders│   │ Method 传到 shader  │
    │ (2.3)              │   │                    │
    │                    │   │                    │
    │ 读 r.LuxGI CVar    │   │ shader 里 BRANCH   │
    │ 算 PermutationId   │   │ if (SLM != LIGHTMAP │
    │ AddShaderType<...> │   │  _ONLY) 判定为 F   │
    │  (PermutationId)   │   │ (2.1 主调用块)     │
    └──────────┬─────────┘   └──────────┬─────────┘
               │                        │
               ▼                        ▼
    ┌────────────────────┐   ┌────────────────────┐
    │ TMobileBasePassPS  │   │ 运行时 skip        │
    │ FEnableLuxGI 维度  │   │ AccumulateLuxGI    │
    │ (2.2)              │   │ Lighting           │
    │                    │   │                    │
    │ ModifyCompilation  │   │  ⇧ L2 双保险       │
    │ Environment 里     │   │                    │
    │ SetDefine          │   │                    │
    │ ("ENABLE_LUX_GI",  │   │                    │
    │  0/1)              │   │                    │
    └──────────┬─────────┘   └────────────────────┘
               │
               ▼
    ┌────────────────────┐
    │ shader 预处理阶段   │
    │ (2.1)              │
    │                    │
    │ #if ENABLE_LUX_GI  │
    │  && ...            │
    │   ...LuxGI 代码... │
    │ #endif             │
    │                    │
    │ r.LuxGI=0 时:      │
    │ - LuxGI 快照消失   │
    │ - AccumulateLuxGI  │
    │   Lighting 主调用  │
    │   消失             │
    │ - LuxGIExtracted   │
    │   剥离/加回消失    │
    │                    │
    │  ⇧ L1 编译期 strip │
    └────────────────────┘
```

---

## 4. 验证方法

### 4.1 编译验证

按 memory `38221550` 的 Android cook 方式跑：

```powershell
cd D:\GR_DevTest
$env:P4CLIENT="DJANGOZHAN-PCFW_GR_DevTest"
$p = Start-Process -FilePath "UE5EA\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" `
     -ArgumentList "S1Game/S1Game.uproject","-run=Cook","-TargetPlatform=Android_ASTC",`
                   "-Map=/Engine/Maps/Templates/Minimal_Default","-unattended","-nullrhi",`
                   "-ini:Engine:[DevOptions.Shaders]:NumUnusedShaderCompilingThreads=3" `
     -PassThru
$p.PriorityClass = 'Idle'
```

**判定信号**：
- 日志 `Saved/Logs/S1Game.log` 出现 `All required shaders are compiled.`
- 全文搜不到 `Found unbound parameters being used` / `not bound!` / `.usf(数字` / `.ush(数字`

### 4.2 运行时验证（Forward path）

1. 编辑器启动后打开 memory `98677004` 提到的目标材质 `/Game/Arts/MaterialLibrary/SceneBaseMaterials/Landscape/M_FoliageBillboard`；
2. 在材质蓝图里做个改动触发刷新（memory `98677004` 记录：Python API 和 `recompileshaders` 都不总是可靠，手动改是最可靠的）；
3. 控制台执行：
   ```
   r.Mobile.ShadingPath 0
   r.LuxGI 1
   ```
4. 用 RenderDoc 抓帧，在 Forward base pass PS DXIL 里搜 `LuxGIExtracted_ZXB` / `AccumulateLuxGILighting`，**应能找到**；
5. 控制台执行 `r.LuxGI 0`（此时 sink 触发 MDC 重建）；
6. 等一帧后再抓帧，**同一处 DXIL 里 `LuxGIExtracted_ZXB` / `AccumulateLuxGILighting` 字符串应彻底消失**（不是 skip、是 strip）。

---

## 5. 快速排查 Checklist（未来遇到类似问题时用）

**症状**：某个引擎 mobile CVar 明明在 `.ini` 里改了 / 命令行敲了，运行时却切不动 shader 行为。

| Step | 检查项 | 命令/工具 |
|------|-------|---------|
| 1 | CVar 是否真的注册 & 生效？ | `DumpConsoleCommands r.CVarName` 看 flags |
| 2 | Shader 里对应宏是否有 `#if` 保护？ | grep `.usf` 里 `#if MACRO_NAME` |
| 3 | 宏是通过 permutation 维度注入的吗？ | 在 `ModifyCompilationEnvironment` 里搜 `SetDefine(TEXT("MACRO_NAME")` |
| 4 | 走的是 mesh material shader 还是 global shader？ | mesh material 走 MDC，global 每帧动态挑 |
| 5 | 如果走 mesh material，CVar 有 sink 吗？ | 在 CVar 声明处搜 `FConsoleVariableDelegate` |
| 6 | Deferred 侧有没有同名参照实现可对齐？ | 直接搜 `SHADER_PERMUTATION_BOOL("MACRO_NAME")` 全工程 |

**本次踩坑点浓缩**：Mobile Forward 走 MDC 缓存，Mobile Deferred 走 FGlobalShaderMap 每帧动态挑；后者天然免疫 CVar 切换问题，前者必须挂 sink。

---

## 6. 相关引用

### 6.1 UE 源码参照

| 文件 | 参照点 |
|------|-------|
| `MobileDeferredShadingPass.cpp:221` | Deferred 侧 `FEnableLuxGI` 权威原型 |
| `MobileDeferredShadingPass.cpp:422-425` | Deferred 侧运行时读 `r.LuxGI` 挑 permutation |
| `ComponentRecreateRenderStateContext.h` | `FGlobalComponentRecreateRenderStateContext` 定义 |
| `LumenDiffuseIndirect.cpp` / `LumenScene.cpp` | Lumen 里 sink + `FGlobalComponentRecreateRenderStateContext` 的成熟应用 |

### 6.2 项目内相关归档（`D:\GR_DevTest\`）

- `UE-Mobile-LuxGI-Forward与Deferred效果不一致-ApplyCartoonShadow参数绑定修复.md` (2026-07-17)
- `UE-Mobile-Deferred-Preview-CartoonShadow参数unbound根因与IS_MOBILE_BASE_PASS分流修复.md` (2026-07-18)
- `UE-Mobile-Forward比Deferred偏亮-LuxGI双重PreExposure与HYBRID天光重复-根因与修复.md` (2026-07-18)
- `UE-Mobile-Forward-vs-Deferred-管线全流程分析-含Shader反汇编解读.md` (2026-07-20)
- `UE-Mobile-Forward-CVar切换Shader-Permutation不生效-MDC缓存与RecreateRenderState修复.md` (2026-07-21) —— 姊妹篇，专讲 MDC 破局故事
- `EID1518_BasePassPS.disasm` / `EID1569_ForwardBasePassPS.disasm` / `DeferredLightingPS.disasm` —— 反汇编物证

### 6.3 相关 Memory ID

- `14849297` —— `#pragma region Engine ZXB` 包裹规范
- `38221550` —— Android cook 验证 shader 编译的完整套路
- `39319424` —— CartoonShadow 参数绑定 & `IS_MOBILE_BASE_PASS` 分流
- `74177138` —— Deferred 截帧 EID 1518/1631 参考
- `98677004` —— Forward path shader 改动的可靠验证方法
- `12492978` —— **CVar `SetOnChangedCallback` 单 slot 陷阱 + 多播 `OnChangedDelegate().AddLambda` 正确写法（本文档 §2.4.1 的根因）**
- `50564675` / `55125183` —— 主动推进 vs 配置改动需征求同意

---

## 7. 一句话总结（TL;DR）

**技术侧**：Mobile Forward 走 MDC 缓存，切 CVar 只改值不重建 draw command → 挂 sink `FGlobalComponentRecreateRenderStateContext` 强制全场景 recreate render state 是标准解法；**但 sink 必须用多播 `OnChangedDelegate().AddLambda(...)` 而不能用 `FAutoConsoleVariableRef` 带 lambda 的构造——后者内部走 `SetOnChangedCallback` legacy API，只维护单个 `LegacyDelegateHandle`，会被同一 CVar 的其他注册者后手覆盖挤掉（本次被同 CU 的 `FLuxIrradianceVolumeSceneData::UpdateFromWorld` 覆盖，详见 §2.4.1）**。配合 `SHADER_PERMUTATION_BOOL` 把 CVar 转成真正的 shader permutation 维度，再加 `View.StaticLightingMethod` 从数据源覆写做双保险，就能让 `r.LuxGI` 在 Forward 上做到"编译期 strip + 运行时零成本切换"，与 Deferred 的 `FMobileDeferredShadingPS::FEnableLuxGI` 行为完全对齐。

**流程侧**：想验证 shader 相关改动到底生效没生效，**先抓两份 DXIL 对比字符串是不是真的消失**（编译期 strip），而不是靠肉眼看画面亮不亮 —— 后者会被 runtime BRANCH / cook 缓存 / MDC 缓存三层"伪装成生效"。
