# UE-MobileBasePassCSM-CommandCount-Mismatch-修复

> 编辑器移动预览冷启动时 `MergeMobileBasePassMeshDrawCommands` 中 `MeshCommands.Num() != MeshCommandsCSM.Num()` 走到 else 分支，根因是默认回退材质 `WorldGridMaterial` 结构性缺少 CSM lightmap policy permutation（LMP=12），导致 CSM pass 产出 0 条命令而非 CSM pass 产出 1 条。修复方案是在 `GetShaders` 失败时将 lightmap policy 降级到 `LMP_NO_LIGHTMAP` 重试。

**前提条件**：编辑器 `Platform Preview` 设置为 **`Android Vulkan High`**（ES3_1 / Forward Shading 路径）时触发；若使用 `SM6`（D3D12）预览则不经过 MobileBasePass 路径，不触发此问题。

---

## 一、问题定位流程

### 1.1 触发点

`Engine/Source/Runtime/Renderer/Private/MeshDrawCommands.cpp:245`

```cpp
if (LIKELY(MeshCommands.Num() == MeshCommandsCSM.Num()))
{
    // 正常合并路径
}
else
{
    // 项目已将原始 checkf 改为软降级 + 诊断日志
    UE_LOG(LogRenderer, Warning,
        TEXT("MergeMobileBasePassMeshDrawCommands: BasePass(%d) and MobileBasePassCSM(%d) ... differ"),
        MeshCommands.Num(), MeshCommandsCSM.Num());
    // best-effort merge ...
}
```

两个命令数组分别来自：
- `MeshCommands` — 由 `CreateMobileBasePassProcessor`（**非 CSM**，`bCanReceiveCSM=false`）扫出
- `MeshCommandsCSM` — 由 `CreateMobileBasePassCSMProcessor`（**CSM**，`bCanReceiveCSM=true`）扫出

### 1.2 排除伪根因：ZXB 守卫

初步怀疑项目自定义的 ZXB 守卫（`MobileBasePass.cpp:856-862`）：

```cpp
if (MaterialRenderProxy.UniformExpressionCache.Get(...).CachedUniformExpressionShaderMap
    != Material.GetRenderingThreadShaderMap())
{
    return false;  // 首帧 uniform-expression cache 未填充 → 误伤 → fallback
}
```

**实验**：注释掉 `return false`，冷启动验证 → **mismatch 仍存在**，且守卫的 `"Mobil Base Pass: Skipped"` 日志从未出现，证明守卫对这 5 个发散 primitive 从未触发。

### 1.3 诊断日志定位真正分歧

在 `FMobileBasePassMeshProcessor::Process` 加入诊断日志，对比 5 个发散 primitive（168/169/170/171/173）在两个 pass 的表现：

| PrimId | Owner (Resource) | BasePass (CanCSM=0) | CSM (CanCSM=1) |
|---|---|---|---|
| 168 | BP_SM_WoodWall (SM_WoodWall01_4M_01) | 真材质 LMP=0 FAIL → WorldGrid **LMP=0 BUILT** ✅ | 真材质 LMP=12 FAIL → WorldGrid **LMP=12 FAIL** → `LOOP_EXHAUSTED_NO_CMD` ❌ |
| 169 | BP_SM_WoodFence (SM_WoodFence_01) | 同上 → BUILT ✅ | 同上 → FAIL → 空 ❌ |
| 170 | BP_SM_WoodWall (SM_WoodWall01_4M_01) | 同上 → BUILT ✅ | 同上 → FAIL → 空 ❌ |
| 171 | BP_SM_WoodFence (SM_WoodFence_01) | 同上 → BUILT ✅ | 同上 → FAIL → 空 ❌ |
| 173 | StaticMeshActor (Cube) | M_TechFarm LMP=0 FAIL → WorldGrid **BUILT** ✅ | M_TechFarm LMP=12 FAIL → WorldGrid **LMP=12 FAIL** → 空 ❌ |

**关键数据**：非 CSM 端用 `LMP=0`（`LMP_NO_LIGHTMAP`）→ 成功；CSM 端用 `LMP=12`（`LMP_MOBILE_DIRECTIONAL_LIGHT_CSM`）→ 失败。

### 1.4 深挖根因：结构性缺失 vs 时序

在降级路径加入富诊断，判断默认材质 shader map 完整性：

```
[S1-CSMDiag2] Downgrade Mat='WorldGridMaterial' reqPolicy=12 -> NO_LIGHTMAP
| RTShaderMapComplete=1  IsDefaultMaterial=1  IsLit=1
| StaticLightingAllowed=1  UseCSMShaderBranch=0  MobileEnableStaticAndCSM=1  DeferredShading=0
```

`RTShaderMapComplete=1` 证明 WorldGridMaterial 的 shader map **已完整**，但仍查不到 LMP=12 的 shader — **这是结构性缺失，不是冷启动异步没编完。**

---

## 二、根因分析

### 2.1 调用链

```
FMobileBasePassMeshProcessor::Process (MobileBasePass.cpp)
→ MobileBasePass::GetShaders(Policy=12, ...)
→ GetMobileBasePassShaders<...>(LMP_MOBILE_DIRECTIONAL_LIGHT_CSM, ...)
→ GetUniformMobileBasePassShaders<LMP_MOBILE_DIRECTIONAL_LIGHT_CSM, ...>
→ Material.TryGetShaders(...)   ← 在 WorldGridMaterial shader map 中查找失败 → return false
```

### 2.2 Why "无"？

编译该 permutation 的门槛是 `FMobileDirectionalLightAndCSMPolicy::ShouldCompilePermutation`（`LightMapRendering.cpp:236-251`）：

```cpp
static bool ShouldCompilePermutation(const FMeshMaterialShaderPermutationParameters& Parameters)
{
    if (IsMobileDeferredShadingEnabled(Parameters.Platform)) return false;
    if (!IsStaticLightingAllowed() && MobileUseCSMShaderBranch())  return false;
    return Parameters.MaterialParameters.ShadingModels.IsLit()
        && !IsTranslucentBlendMode(Parameters.MaterialParameters)
        && (!IsStaticLightingAllowed() || FReadOnlyCVARCache::MobileEnableStaticAndCSMShadowReceivers());
}
```

当前 CVar（`StaticLightingAllowed=1`, `UseCSMShaderBranch=0`, `MobileEnableStaticAndCSM=1`, `DeferredShading=0`）下，对任何 lit 不透明材质该 permutation均应编译。

**但 WorldGridMaterial 是默认引擎材质（`bIsDefaultMaterial=1`）**，编译时机在引擎早期同步阶段，可能在上述 CVar 初始化到运行时值之前就已经完成编译。编译时 `ShouldCompilePermutation` 判 false → CSM permutation 从未进入其 shader map。默认材质**不随 CVar 变化重编** → 永久缺失。

对比：真实业务材质（MI_WoodWall 等）运行时按需编译，拿到的 CVar 已是完整状态 → 有 CSM permutation。这解释了「稳态无 mismatch」。

**核心矛盾**：
- 非 CSM pass → 用 `LMP_NO_LIGHTMAP`（=0）→ 任何材质保证有此 permutation → WorldGridMaterial 成功 → 产出 1 条命令
- CSM pass → 用 `LMP_MOBILE_DIRECTIONAL_LIGHT_CSM`（=12）→ WorldGridMaterial 无此 permutation → `GetShaders` 失败 → fallback 循环耗尽 → 产出 0 条命令

于是 `BasePass(5) / CSM(0)`，触发 `MergeMobileBasePassMeshDrawCommands` else 分支。

---

## 三、修复方案

### 3.1 最终方案（已落地）：GetShaders 降级重试

**文件**：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp`
**位置**：`FMobileBasePassMeshProcessor::Process`，`GetShaders` 调用处

```cpp
bool bS1GotShaders = MobileBasePass::GetShaders(
    LightMapPolicyType, ...);

// 降级：当请求的 policy 失败时，退到必定存在的 LMP_NO_LIGHTMAP 重试
if (!bS1GotShaders && LightMapPolicyType != LMP_NO_LIGHTMAP)
{
    bS1GotShaders = MobileBasePass::GetShaders(
        LMP_NO_LIGHTMAP, ...);
    if (bS1GotShaders)
    {
        // 一次性日志
        static bool bLoggedLightmapPolicyDowngradeOnce = false;
        if (!bLoggedLightmapPolicyDowngradeOnce)
        {
            bLoggedLightmapPolicyDowngradeOnce = true;
            UE_LOG(LogTemp, Log,
                TEXT("MobileBasePass: material '%s' has no shader for lightmap policy %d; "
                     "downgraded to LMP_NO_LIGHTMAP to keep BasePass and MobileBasePassCSM "
                     "command counts in lockstep (logged once)."),
                *MaterialRenderProxy.GetMaterialName(), (int32)LightMapPolicyType);
        }
    }
}

if (!bS1GotShaders)
{
    return false;  // LMP_NO_LIGHTMAP 也失败 → 真无法渲染
}
```

**原理**：`LMP_NO_LIGHTMAP` 是任何材质的最小可用 permutation（编译门槛：只需 `IsMobilePlatform`）。退到此 policy 确保无论落到什么 fallback 材质，两个 pass 都能各产出恰好 1 条命令，数量恒等。

**不变式保证**：
- 冷启动帧 0：真材质 shader map 未就绪 → 回退 WorldGridMaterial → 两 pass 都用 `LMP_NO_LIGHTMAP`（降级生效） → 各出 1 条 → 恒等
- 稳态：真材质就绪 → 两 pass 用各自正常 policy（非 CSM 用 LMP_NO_LIGHTMAP、CSM 用 LMP_MOBILE_DIRECTIONAL_LIGHT_CSM）→ 都成功 → 恒等
- 降级路径只在「回退材质缺原 policy permutation」时触发，正常路径不进入

### 3.2 方案对比

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| **✅ 降级重试（已落地）** | `GetShaders` 失败时退到 `LMP_NO_LIGHTMAP` 重试 | 源头解决，保证不变式，最小改动，无编译代价 | 回退帧占位材质不带 CSM（视觉无意义） |
| ❌ 让默认材质也编 CSM | 修改 `ShouldCompilePermutation` 对默认材质放行 | 默认材质也有 CSM permutation | 给默认材质增加 permutation 集 → 编译时间/内存/包体；收益仅占位帧 |
| ❌ ZXB 守卫修改 | 注释 `return false` | 无 | 守卫条件对这些 primitive 从未为真，改它无效 |
| ❌ 允许 mismatch | 维持现有 `MeshDrawCommands.cpp` 软降级 | 不改代码 | 冷启动每次都打 mismatch 警告；best-effort merge 损失 CSM 选择 |

### 3.3 配套保留：MeshDrawCommands.cpp 软降级

`MeshDrawCommands.cpp:245` 的 else 分支（best-effort merge）**保留作为安全网**。尽管降级修复已消除已知分歧来源，保留软降级可防止未来新增内容引入未知来源的分歧时编辑器崩溃。

---

## 四、验证矩阵

| 版本 | mismatch | 备注 |
|---|---|---|
| 修复前 | `BasePass(5)/CSM(0)` diverge=5 | 冷启动每次触发 |
| ZXB 守卫注释 | 同上（无效） | 证明守卫非根因 |
| 加诊断日志 | 同上 + 精确定位 LMP=12 FAIL | 定位到降级方向 |
| **降级修复后** | **0** | 冷启动验证通过 |
| 最终清理版 | **0** | 仅 1 条一次性 breadcrumb |
| PIE / 重载关卡 | 0 | 稳态无 mismatch |

---

## 五、快速排查 Checklist

```bash
# 1. 确认当前 Preview 平台
LogWorld: Changing Feature Level (Enum) from X to 1  # ES3_1

# 2. 确认 CVar
r.Mobile.ShadingPath       # 0 = Forward
r.Mobile.UseCSMShaderBranch # 0 = 独立 permutation
r.AllowStaticLighting       # 1
r.Mobile.EnableStaticAndCSMShadowReceivers  # 1

# 3. 查日志关键字
grep "MergeMobileBasePassMeshDrawCommands" S1Game.log
grep "primitive(s) diverge" S1Game.log
grep "downgraded to LMP_NO_LIGHTMAP" S1Game.log

# 4. 若有新 mismatch（不应出现，但万一），查诊断
grep "GETSHADERS_FAIL\|LOOP_EXHAUSTED_NO_CMD" S1Game.log
```

---

## 六、关键文件索引

| 文件 | 关键行 | 用途 |
|---|---|---|
| `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp` | 856-862 | ZXB 守卫（原 return false，对本问题无关） |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp` | 882-942 | `AddMeshBatch` fallback 循环 |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp` | 944-1058 | `Process` — **修复落地位置（GetShaders 降级）** |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp` | 1221-1233 | `CreateMobileBasePassProcessor`（非 CSM） |
| `Engine/Source/Runtime/Renderer/Private/MobileBasePass.cpp` | 1272-1289 | `CreateMobileBasePassCSMProcessor`（CSM，bCanReceiveCSM=true） |
| `Engine/Source/Runtime/Renderer/Private/LightMapRendering.h` | 335-354 | `ELightMapPolicyType` 枚举（LMP=0 NO_LIGHTMAP, LMP=12 DIRECTIONAL_LIGHT_CSM） |
| `Engine/Source/Runtime/Renderer/Private/LightMapRendering.cpp` | 236-251 | `FMobileDirectionalLightAndCSMPolicy::ShouldCompilePermutation`（CSM 编译门槛） |
| `Engine/Source/Runtime/Renderer/Private/MeshDrawCommands.cpp` | 213-415 | `MergeMobileBasePassMeshDrawCommands`（软降级 + 诊断） |

---

## 七、参考

- 引擎源码：`D:\GR_DevTest\UE5EA`，UE5EA 内部分支
- 项目：`D:\GR_DevTest\S1Game\S1Game.uproject`
- 诊断 session 笔记：`D:\GR_DevTest\S1Game\Docs\Debug\CSM-Mismatch-Session-2026-07-07.md`
- 编辑器日志：`D:\GR_DevTest\S1Game\Saved\Logs\S1Game.log`
