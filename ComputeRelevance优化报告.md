# ComputeRelevance CPU 优化报告

**文件路径：** `Engine/Source/Runtime/Renderer/Private/SceneVisibility.cpp`  
**函数：** `FRelevancePacket::ComputeRelevance`  
**优化日期：** 2026-05-25  
**优化人：** Engine ZXB  

---

## 一、背景

`ComputeRelevance` 是 UE5 渲染管线中每帧执行的核心可见性判断函数，负责对场景中所有可见 Primitive 进行相关性计算，并将其分配到各个 MeshPass（BasePass、DepthPass、VelocityPass 等）。该函数在多线程 Packet 中并行执行，CPU 消耗较高，是渲染线程的主要热点之一。

---

## 二、优化项汇总

共实施 **8 项**优化，全部通过 30 次代码 Review 验证。

---

## 三、详细优化说明

### 优化 1：缓存 `EShaderPlatform ShaderPlatform`

**问题：** `View.GetShaderPlatform()` 在函数内被调用超过 10 次，每次都通过 View 对象间接访问。

**修改前：**
```cpp
UseNanite(View.GetShaderPlatform())
FOpaqueVelocityMeshProcessor::PrimitiveCanHaveVelocity(View.GetShaderPlatform(), ...)
IsMobileDeferredShadingEnabled(View.GetShaderPlatform())
// ... 共 10+ 处
```

**修改后（函数开头）：**
```cpp
#pragma region Engine ZXB
const EShaderPlatform ShaderPlatform = View.GetShaderPlatform();
#pragma endregion
```

**收益：** 消除 10+ 次间接指针访问，减少指令数。

---

### 优化 2：缓存 `bVelocityStrictCheck`

**问题：** `CVarVelocityStrictCheck.GetValueOnRenderThread()` 在内层 Mesh 循环中每次迭代都被调用，对每个 Mesh 重复读取 CVar 值。

**修改前（内层循环中）：**
```cpp
if (CVarVelocityStrictCheck.GetValueOnRenderThread() != 0)
```

**修改后（函数开头缓存，内层循环使用）：**
```cpp
// 函数开头
const bool bVelocityStrictCheck = CVarVelocityStrictCheck.GetValueOnRenderThread() != 0;

// 内层循环
if (bVelocityStrictCheck)
```

**收益：** 将 N×M 次 CVar 读取降低为 1 次（N=Primitive 数，M=Mesh 数）。

---

### 优化 3：缓存 `bCanToonDataPassMergeIntoBasePass`

**问题：** `CanToonDataPassMergeIntoBasePass(View.GetShaderPlatform())` 在内层 Mesh 循环中被调用 2 次，每次都执行函数调用和 ShaderPlatform 查询。

**修改前（内层循环中）：**
```cpp
if (!CanToonDataPassMergeIntoBasePass(View.GetShaderPlatform()) && ...)
if (!CanToonDataPassMergeIntoBasePass(View.GetShaderPlatform()))
```

**修改后（函数开头缓存）：**
```cpp
const bool bCanToonDataPassMergeIntoBasePass = CanToonDataPassMergeIntoBasePass(ShaderPlatform);
```

**收益：** 消除每个 Mesh 的 2 次函数调用，同时复用已缓存的 `ShaderPlatform`。

---

### 优化 4：缓存 `bGameplayStencilActive`

**问题：** `GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1` 在内层 Mesh 循环中出现 **4 次以上**，每次都是两个全局变量的比较。

**修改前（内层循环中，多处）：**
```cpp
if (GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1 && bUseSupplementaryVelocityPass)
if (GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1 && bUseSupplementaryDepthPass)
if (GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1 && bUseSupplementaryBasePass)
// bVelocityPassWriteDepth 中也有
```

**修改后（函数开头缓存）：**
```cpp
const bool bGameplayStencilActive = (GSceneStencilGameplayState != 0 && GPPSeethroughMode != -1);
```

**收益：** 将 4+ 次重复的双变量比较降低为 1 次，减少分支预测压力。

---

### 优化 5：提升 CVar 查找到函数开头（`ViewDistanceQualityCVar`）

**问题：** `static IConsoleVariable* ViewDistanceQualityCVar = IConsoleManager::Get().FindConsoleVariable(...)` 声明在 Primitive 级别循环内部的 `else` 分支中。虽然 `static` 保证只初始化一次，但每次进入该代码块时仍需检查 static 初始化标志（原子操作），且每次都调用 `GetInt()` 读取值。

**修改前（Primitive 循环内）：**
```cpp
static IConsoleVariable* ViewDistanceQualityCVar = IConsoleManager::Get().FindConsoleVariable(TEXT("sg.ViewDistanceQuality"));
int32 ViewDistanceQuality = ViewDistanceQualityCVar->GetInt();
```

**修改后（函数开头）：**
```cpp
static IConsoleVariable* ViewDistanceQualityCVarCached = IConsoleManager::Get().FindConsoleVariable(TEXT("sg.ViewDistanceQuality"));
const int32 ViewDistanceQualityCached = ViewDistanceQualityCVarCached ? ViewDistanceQualityCVarCached->GetInt() : 0;
```

**额外改进：** 增加了空指针检查，比原代码更安全。

**收益：** 消除每个 Primitive 的 static 初始化检查原子操作和 `GetInt()` 调用。

---

### 优化 6：提升 CVar 查找到函数开头（`CVarDepthDistanceCulling`）

**问题：** 同优化 5，`CVarDepthDistanceCulling` 的 `static` 声明和 `GetFloat()` 调用在 Primitive 级别循环内。

**修改前（Primitive 循环内）：**
```cpp
static const auto CVarDepthDistanceCulling = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Mobile.FullPrePassDistanceCulling"));
const float DepthDistanceCulling = CVarDepthDistanceCulling->GetFloat();
```

**修改后（函数开头）：**
```cpp
static const IConsoleVariable* CVarDepthDistanceCullingCached = IConsoleManager::Get().FindConsoleVariable(TEXT("r.Mobile.FullPrePassDistanceCulling"));
const float DepthDistanceCullingCached = CVarDepthDistanceCullingCached ? CVarDepthDistanceCullingCached->GetFloat() : 0.0f;
```

**收益：** 消除每个 Primitive 的 static 初始化检查和 `GetFloat()` 调用。

---

### 优化 7：将 `bRendererUsingNanite` 等变量提升到 Primitive 级别

**问题：** 以下 8 个变量原本声明在内层 Mesh 循环中，对同一 Primitive 的每个 Mesh 都重复计算，但它们的值对同一 Primitive 的所有 Mesh 完全相同：

```cpp
// 原来在内层 Mesh 循环中（每个 Mesh 都重算）
const bool bRendererUsingNanite = UseNanite(View.GetShaderPlatform()) && ...;
const bool bSupplementaryGameplayTagged = PrimitiveSceneInfo->Proxy->GameplayStencilTagged() && ...;
const bool bSupplementaryEraseSilhouette = PrimitiveSceneInfo->Proxy->EraseSilhouetteUsingStencil();
const bool bSupplementaryGameplayPenetrable = PrimitiveSceneInfo->Proxy->GameplayPenetrableUsingStencil();
const bool bUseSupplementaryDepthPass = ...;
const bool bUseSupplementaryVelocityPass = ...;
const bool bUseBothVelocityAndDepthPass = ...;
const bool bUseSupplementaryBasePass = ...;
```

**修改后（提升到 Primitive 级别，在 `GetViewRelevance` 之后）：**
```cpp
#pragma region Engine ZXB
// [Optimization] Hoist per-Primitive Supplementary/Nanite flags out of the inner Mesh loop.
const bool bRendererUsingNanite = UseNanite(ShaderPlatform) && View.Family->EngineShowFlags.NaniteMeshes && Nanite::GStreamingManager.HasResourceEntries();
const bool bSupplementaryGameplayTagged = PrimitiveSceneInfo->Proxy->GameplayStencilTagged() && ViewRelevance.HasTranslucency();
// ... 其余变量
#pragma endregion
```

**收益：** 将 N×M 次计算降低为 N 次（N=Primitive 数，M=Mesh 数）。对于有多个 Mesh 的 Primitive（如 LOD 组），收益尤为显著。

---

### 优化 8：缓存 `bHasStaticMeshes`，消除重复的 `IsEmpty()` 调用

**问题：** `PrimitiveSceneInfo->StaticMeshes.IsEmpty()` 在 LOD 计算分支中被调用 2 次。

**修改前：**
```cpp
float LODDistanceScale = (!PrimitiveSceneInfo->StaticMeshes.IsEmpty() && ...) ? ... : 1.0f;
bool bSkipLOD = PrimitiveSceneInfo->StaticMeshes.IsEmpty() ? false : ...;
```

**修改后：**
```cpp
const bool bHasStaticMeshes = !PrimitiveSceneInfo->StaticMeshes.IsEmpty();
float LODDistanceScale = (bHasStaticMeshes && ...) ? ... : 1.0f;
bool bSkipLOD = bHasStaticMeshes ? ... : false;
```

**收益：** 消除一次重复的数组大小检查。

---

## 四、Review 记录

本次优化经过 **30 次**代码 Review，覆盖以下维度：

| Review 编号 | 验证内容 | 结论 |
|-------------|----------|------|
| 1 | `ShaderPlatform` 类型和线程安全性 | 通过 |
| 2 | `bVelocityStrictCheck` 语义等价性 | 通过 |
| 3 | `bCanToonDataPassMergeIntoBasePass` 缓存安全性 | 通过 |
| 4 | `bGameplayStencilActive` 逻辑等价性 | 通过 |
| 5 | `ViewDistanceQualityCVarCached` 空指针安全 | 通过 |
| 6 | `CVarDepthDistanceCullingCached` 空指针安全 | 通过 |
| 7 | `bRendererUsingNanite` 提升后依赖关系 | 通过 |
| 8 | `bSupplementaryGameplayTagged` 提升后依赖关系 | 通过 |
| 9 | `bSupplementaryEraseSilhouette` 提升后依赖关系 | 通过 |
| 10 | `bUseSupplementaryBasePass` 依赖链完整性 | 通过 |
| 11 | `bHasStaticMeshes` 语义等价性 | 通过 |
| 12 | `ViewDistanceQualityCached` 替换正确性 | 通过 |
| 13 | `LODDistanceScale` 计算等价性 | 通过 |
| 14 | `bSkipLOD` 三元运算符等价性 | 通过 |
| 15 | `DepthDistanceCullingCached` 替换正确性 | 通过 |
| 16 | `bVelocityStrictCheck` 内层循环替换 | 通过 |
| 17 | `ShaderPlatform` 在 `PrimitiveCanHaveVelocity_Strict` 中 | 通过 |
| 18 | `bGameplayStencilActive` 在 Strict 分支 | 通过 |
| 19 | `ShaderPlatform` 在 `PrimitiveCanHaveVelocity` 和 `TranslucentVelocity` 中 | 通过 |
| 20 | `bGameplayStencilActive` 在 non-Strict 分支 | 通过 |
| 21 | `bVelocityPassWriteDepth` 逻辑等价性（De Morgan 定律验证） | 通过 |
| 22 | `bGameplayStencilActive` 在 SupplementaryDepthPass | 通过 |
| 23 | `DepthDistanceCullingCached` 在距离裁剪条件 | 通过 |
| 24 | `bEnableDepthPassDistanceCulling` 计算等价性 | 通过 |
| 25 | `bGameplayStencilActive` 在 SupplementaryBasePass | 通过 |
| 26 | `bCanToonDataPassMergeIntoBasePass` 第一处替换 | 通过 |
| 27 | `bCanToonDataPassMergeIntoBasePass` 第二处替换 | 通过 |
| 28 | `ShaderPlatform` 在 `IsMobileDeferredShadingEnabled` | 通过 |
| 29 | 旧变量名 `ViewDistanceQuality` 无残留 | 通过 |
| 30 | 旧变量名 `DepthDistanceCulling` 无跨函数污染 | 通过 |

---

## 五、预期收益

| 优化项 | 优化类型 | 预期收益 |
|--------|----------|----------|
| 缓存 `ShaderPlatform` | 消除间接访问 | 减少 10+ 次指针间接访问 |
| 缓存 `bVelocityStrictCheck` | 消除热路径 CVar 读取 | 每帧节省 N×M 次原子读 |
| 缓存 `bCanToonDataPassMergeIntoBasePass` | 消除函数调用 | 每帧节省 N×M×2 次函数调用 |
| 缓存 `bGameplayStencilActive` | 消除重复比较 | 每帧节省 N×M×4 次比较 |
| 提升 CVar 查找（2 处） | 消除 static 初始化检查 | 每帧节省 N 次原子操作 |
| 提升 `bRendererUsingNanite` 等 8 个变量 | 消除循环不变量 | 每帧节省 N×(M-1)×8 次计算 |
| 缓存 `bHasStaticMeshes` | 消除重复数组检查 | 每帧节省 N 次数组大小读取 |

> N = 可见 Primitive 数量，M = 每个 Primitive 的平均 Mesh 数量

---

## 六、代码标记规范

所有新增/修改代码均使用以下标记包裹，便于追踪和回滚：

```cpp
#pragma region Engine ZXB
// 优化代码
#pragma endregion
```

---

## 七、注意事项

1. **线程安全**：所有缓存变量均在渲染线程中读取，`ComputeRelevance` 本身在渲染线程的 Task 中执行，无线程安全问题。
2. **帧间一致性**：`ShaderPlatform`、`bCanToonDataPassMergeIntoBasePass` 等值在同一帧内不变，缓存安全。
3. **CVar 实时性**：`ViewDistanceQualityCached` 和 `DepthDistanceCullingCached` 在每帧函数调用时重新读取，不影响 CVar 的实时生效。
4. **空指针安全**：新代码对 `FindConsoleVariable` 的返回值增加了空指针检查，比原代码更健壮。
