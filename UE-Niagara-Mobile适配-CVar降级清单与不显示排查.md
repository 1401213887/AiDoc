# UE-Niagara-Mobile适配-CVar降级清单与不显示排查

> PC 粒子迁移到 Mobile 的完整适配方案：Niagara 在移动端的降级路径、官方预留的 CVar 清单、以及"单资产 PC 正常但 Mobile 不显示"的排障清单。基于 UE5.5.4 fork 源码（`UE5EA/Engine/Plugins/FX/Niagara/`）逐条核实。

---

## 一、背景：Niagara 与 Cascade 的关系

- **没有"相互"转换，只有单向的 Cascade → Niagara**，且是尽力而为的一次性拷贝，不是双向同步。
- 官方工具 `CascadeToNiagaraConverter` 插件（`UE5EA/Engine/Plugins/FX/CascadeToNiagaraConverter/`）：
  - 插件 manifest 标注 `IsBetaVersion: true`、`EnabledByDefault: false`；本工程 `S1Game/S1Game.uproject:672` 已显式启用（仅 Editor）。
  - 实现为 Python 脚本驱动（`Content/Python/` 下 ~50 个转换脚本）+ C++ 适配层 `NiagaraStackGraphUtilitiesAdapterLibrary.cpp`。
  - **明确不支持**：Beam 全系、AnimTrail、VectorField 全系、Event 全系、VelocityCone、LocationEmitter、LocationSkelVertSurface、KillHeight、AttractorPointGravity。找不到转换脚本的模块记 WARNING 跳过（`CascadeToNiagaraConverter.py:300,340-346`）。
  - 语义：1 个 Cascade System → 1 个新 Niagara System 资产，不修改原资产。
- **没有 Niagara → Cascade 的路径**：全引擎搜 `NiagaraToCascade` / `ConvertNiagaraToCascade` 零命中；Cascade 运行时（`ParticleSystem.h` / `ParticleSystemRender.cpp`）无任何消费 `UNiagaraSystem` 的代码。Cascade 在 UE5 是维护模式（冻结、不演进），Niagara 是唯一在开发的方向。
- **结论**：迁移方向钉死为 Cascade→Niagara；Niagara 资产只能在 Niagara 生态内做移动端降级，不存在"退回 Cascade"的解法。

---

## 二、Niagara 移动端对比 PC 的降级清单（源码依据）

移动端判定入口：`FNiagaraWorldManager::UpdatePlatformInfo()`（`NiagaraWorldManager.cpp:2282-2324`），按 `IsMobilePlatform()` 置 `bIsMobileSignificance`。

| # | 降级项 | 源码依据 | 后果 |
|---|---|---|---|
| 1 | **模拟频率 32fps → 24fps** | `NiagaraWorldManager.cpp:362,374,2379`：`GNiagaraDesiredFramesPerSecondCVar=32`，`GNiagaraSimulationFrequencyMobileScale=0.75` | 低帧率模拟 + 60fps 渲染时粒子位移跳帧，需开 InterpolatedSpawning |
| 2 | **FX Budget draw call ×0.75** | `NiagaraWorldManager.cpp:417,447,2364`：预算 `{100,200,300,400,500}` × `GNiagaraMobileBudgetNumDrawCallsScale=0.75` | 超预算 FX 被降 significance/降质量 |
| 3 | **Emitter 剔除距离改用 Mobile 专用值** | `NiagaraSystem.cpp:1896-1898`、`NiagaraEffectType.cpp:579`：`EmitterCullingDistanceMobile` 默认 0 | 未显式配置则移动端无距离剔除控制 |
| 4 | **Mesh Renderer 无 GPU Scene（硬边界）** | `NiagaraRendererMeshes.cpp:385`、`NiagaraMeshVertexFactory.cpp:200,220`：ES3.1 上 `bUseGPUScene=false` | 无 `VF_SUPPORTS_PRIMITIVE_SCENE_DATA`；剔除回退 CPU（`:488`）；每粒子 LOD 被禁（`:535`）；per-instance data 传输开销 |
| 5 | **粒子灯光 Forward 移动端默认丢弃** | `NiagaraRendererLights.cpp:70,75-81`：需 `IsMobileDeferredShadingEnabled` 或 `MobileForwardEnableParticleLights`（`RenderUtils.h:332-334`，默认多数平台关） | Light Renderer 静默不渲染 |
| 6 | **SkeletalMesh DI GPU 采样受限** | `NiagaraDataInterfaceSkeletalMesh.cpp:2578`：Mobile/OpenGL 不自动建 SRV | GPU 脚本骨骼采样需 `bAllowCPUAccess`，否则不可用 |
| 7 | **Emitter 级移动端门控** | `NiagaraEmitter.cpp:904` `MobileEmitterSignificance`；fork 定制 `ENiagaraPlatform::Mobile`（`:1542-1552`） | 低端机按 significance 关 emitter |

**非降级项（移动端专属增强/独立维护）**：GPU compute dispatch 有专门 Mobile 路径绑定 `MobileSceneTextures`（`NiagaraGpuComputeDispatch.cpp:1145-1147`）；Stateless emitter 是移动端友好的省 CPU 方向。

---

## 三、移动端 Niagara CVar 清单（源码核实默认值）

### A. 模拟频率（移动端专属，最常动）
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.Niagara.DesiredFramesPerSecond` | 32 | 全局模拟目标帧率 |
| `fx.Niagara.SimulationFrequencyMobileScale` | **0.75** | 移动端模拟频率缩放（实际 32×0.75=**24fps**） |

### B. GPU 粒子 / 计算
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.NiagaraAllowGPUParticles` | 1 | GPU 粒子总开关（`ECVF_Scalability`），关=全部走 CPU 模拟 |
| `fx.NiagaraAllowComputeShaders` | 1 | 允许 compute shader；关则 GPU sim/排序/剔除全失效 |
| `Niagara.GPUCulling` | 1 | GPU 端视锥+距离剔除 |

### C. 渲染器总开关（移动端按档全局关）
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.EnableNiagaraSpriteRendering` | 1 | Sprite 渲染总开关 |
| `fx.EnableNiagaraMeshRendering` | 1 | Mesh 渲染总开关（低端机关=Mesh 粒子全不渲染） |
| `fx.EnableNiagaraRibbonRendering` | 1 | Ribbon 渲染总开关 |

### D. 质量档 / 缩放（配合 Scalability）
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.Niagara.QualityLevel` | -1 | 质量档 0-4（工程 `AndroidScalability.ini` 已把 EffectsQuality 映射到它） |
| `fx.NiagaraGlobalSystemCountScale` | 1.0 | 全场景系统实例数缩放（低端机 0.5-0.7） |
| `fx.Niagara.Scalability.MinMaxDistance` | - | 缩放生效的距离范围 |

### E. FX 预算（draw call 兜底）
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.Niagara.UseGlobalFXBudget` | 1 | 启用全局 FX 预算 |
| `fx.Budget.MobileNumDrawCallsScale` | **0.75** | 移动端预算缩放（低端再压到 0.5） |
| `fx.Budget.NumDrawCalls <档> <数>` | 100~500 | 按质量档设预算（命令） |

### F. 平台门控 / 定向关闭
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.Niagara.SetSystemDenyList <名>` | - | 全局禁用指定 System |
| `fx.Niagara.SetEmitterDenyList <名>` | - | 全局禁用指定 Emitter |
| `fx.Niagara.SetGpuRHIDenyList <RHI>` | - | 指定 RHI（如 OpenGL）禁 GPU 粒子 |

### G. 调试 / 兜底
| CVar | 默认 | 作用 |
|---|---|---|
| `fx.DisableNiagaraSpawn` | 0 | 全局禁止 spawn（定位用） |
| `fx.Niagara.MaxLifeTimeForPreCulling` | 10.0 | 预剔除寿命阈值（工程 fork 的 GR 定制） |
| `fx.Niagara.Editor.TestPlatform` | - | **编辑器平台预览**：设 `Android`/`Windows`/`IOS`/`All` 立即看平台差异（fork 定制，`NiagaraWorldManager.cpp:408`） |

**低端机一键降级组合**（DeviceProfile 里照抄）：
```
fx.NiagaraAllowGPUParticles=0        ; GPU 粒子全关，强制 CPU
fx.EnableNiagaraMeshRendering=0      ; Mesh 粒子不渲染
fx.Budget.MobileNumDrawCallsScale=0.5
fx.NiagaraGlobalSystemCountScale=0.6
fx.Niagara.SimulationFrequencyMobileScale=0.5   ; 模拟降到 16fps
```

> CVar 是全局粗粒度挡板，适合快速调档和出基线；逐特效精修靠资产里的 NiagaraPlatformSet / EffectType。

---

## 四、PC→Mobile 迁移适配方案（浓缩版）

### 第 0 步：先定标尺
| 指标 | 旗舰档 | 主流档 | 入门档 |
|---|---|---|---|
| 单屏粒子总量 | ≤800 | ≤400 | ≤200 |
| 单特效 DrawCall（含材质 pass） | ≤8 | ≤4 | ≤2 |
| 单系统 CPU 模拟耗时 | ≤0.8ms | ≤0.5ms | ≤0.3ms |
| Mesh Renderer 粒子数 | ≤30 | ≤20 | ≤10 |
| Light Renderer | 禁 | 禁 | 禁 |

采集工具：`ProfileGPU` + `stat niagara`。每个待迁特效先记 PC 基线，迁移后对目标值验收。

### 第 1 步：存量分级
- **A 级（核心战斗）**：命中/受击/技能主体——必迁，最优预算
- **B 级（场景交互）**：死亡/烟雾/水花——迁但降一档
- **C 级（氛围）**：远处火光/漂浮——不迁，PlatformSet 移动端关掉

自动判定进降级通道：DC>20、粒子>1000、**Mesh renderer 且粒子>50**、带 Light Renderer。

### 第 2 步：Renderer 选型决策表（对降级路径 #4/#5/#6）
| PC 原 Renderer | 移动端策略 | 说明 |
|---|---|---|
| Sprite | **保留** | SubUV 序列帧能顶 80% 效果 |
| Mesh，粒子 >50 | **换 Sprite/序列帧** | 首推，别跟 GPU Scene 硬刚 |
| Mesh，粒子 ≤20-30 | 保留 + **关 bEnableCulling/bEnableLODCulling** | 避免逐粒子 CPU 剔除；Mesh 低面数 |
| Ribbon | 保留，限条数 ≤8-12 | 段数压缩 |
| Light Renderer | **弃用/换假光** | Forward 移动端默认被丢，改 emissive 材质模拟发光 |
| SkeletalMesh DI | 改 **CPU sim 或预烘焙** | GPU 采样需 `bAllowCPUAccess`，OpenGL 受限 |

### 第 3 步：模拟参数
- **Fixed Bounds 必开**（GPU sim 移动端算不了 dynamic bounds，`NiagaraEmitter.cpp:1772`）
- **InterpolatedSpawning 开**（否则 24fps 模拟跳帧）
- SpawnRate / MaxParticles 降到 PC 的 1/2~1/4（用 PlatformSet 覆盖做）；寿命砍短；同类 emitter 合并

### 第 4 步：平台门控（用 Niagara 机制）
- **NiagaraPlatformSet**（逐资产主抓手）：按质量档覆盖 SpawnRate/Max/Lifetime
  ```
  Low    (质量0-1): SpawnRate ×0.3, Max ×0.25, Lifetime ×0.7
  Medium (质量2)  : SpawnRate ×0.5, Max ×0.5
  High   (质量3)  : SpawnRate ×0.7, Max ×0.7
  Epic   (质量4)  : SpawnRate ×1.0
  ```
- **EffectType**（建 3 个复用）：`FX_Default / FX_CoreCombat / FX_Ambient`，配 `EmitterCullingDistanceMobile`（默认 0 不生效，必须显式配）+ BudgetScaling
- **MobileEmitterSignificance**（`NiagaraEmitter.cpp:904`）：A 级=Critical / B 级=Normal / C 级=Low

### 第 5 步：验收
真机三档各 1 台跑同一战斗场景，`stat niagara` 贴档；逐特效记降级清单（移动端关了什么/降了多少/换了什么）；卡点粒子总量、DC、sim ms、hitch、发热。

### 单特效迁移检查单
```
□ PC 基线已记（粒子/DC/sim ms）
□ Renderer 选型符合决策表（Mesh>50 已换 Sprite）
□ Fixed Bounds 已开
□ InterpolatedSpawning 已开
□ PlatformSet 按档覆盖 SpawnRate/Max/Lifetime 已配
□ MobileEmitterSignificance 已定级
□ EffectType 已挂（含 EmitterCullingDistanceMobile）
□ Light Renderer 已处理（弃用或假光）
□ 骨骼 DI 已确认 CPU 路径/bAllowCPUAccess
□ 真机 stat niagara 达标（对第 0 步标尺）
□ 降级清单已记录
```

---

## 五、单资产 Mobile 不显示、PC 正常 —— 排障清单（按发生频率排序）

### ① 资产 PlatformSet 没勾 Mobile（最常见）
System / Emitter / Renderer 三层只勾 Desktop。判定：`FNiagaraPlatformSet::IsEnabledForPlatform`（`NiagaraPlatformSet.cpp:1373`）。
**修**：三层 PlatformSet 都勾上 Mobile / 改 All。

### ② 工程 fork 的 `EmitterPlatform` 被设成 Desktop（项目特有坑）
`NiagaraEmitter.cpp:1536-1552`：设 `Desktop` 时 Android/iOS 直接 `return false`。
**修**：改成 All 或 Mobile。

### ③ 材质平台开关 / 移动端编译失败
粒子是拿材质渲染的，材质只勾 Desktop 或移动端编译失败 → 移动端黑/默认材质/不显示。**PC 正常 Mobile 黑，先怀疑材质再怀疑粒子**。
**修**：材质勾 Mobile 平台，或修掉移动端编译错误。

### ④ 渲染器选型不兼容移动端
- **Light Renderer**：Forward 移动端默认丢弃（`NiagaraRendererLights.cpp:78`）→ 只有 Light renderer 的资产移动端看不见。
- Mesh Renderer：ES3.1 无 GPU Scene，通常不至于完全不显示。
**确认**：`stat niagara` / DebugDraw 看粒子是否在模拟。

### ⑤ 数据接口在移动端不支持
SkeletalMesh DI GPU 采样（需 `bAllowCPUAccess`，`NiagaraDataInterfaceSkeletalMesh.cpp:2578`）、RenderTarget2D / Grid3D 等高级 DI、被 `fx.Niagara.SetGpuDataInterfaceDenyList` 禁掉的 DI → GPU 脚本编译失败 → emitter 静默不模拟/不渲染。
**确认**：`Output Log` 里 GPU 脚本编译错误。

### ⑥ DeviceProfile / CVar 全局开关
`fx.NiagaraAllowGPUParticles`、`fx.EnableNiagaraMeshRendering`、`fx.Niagara.SetSystemDenyList` / `SetGpuEmitterDenyList` / `SetGpuRHIDenyList` 在移动端被关 → 多个资产同时不显示。
**确认**：查 `AndroidGame.ini` / `AndroidScalability.ini` 的 Niagara CVar。

### ⑦ QualityLevel 只勾了高档（低端机不显示）
`IsEnabledForQualityLevel`（`NiagaraPlatformSet.cpp:687`）；工程 `AndroidScalability.ini` 已把 EffectsQuality 映射到 `fx.Niagara.QualityLevel` 0-3。
**修**：PlatformSet 勾上 Low/Medium。

### ⑧ Significance 判定 / 预算剔除
`MobileEmitterSignificance` + FX 预算（`fx.Budget.MobileNumDrawCallsScale`）吃紧时低 significance 先被 cull；战斗人多时消失。
**修**：核心特效 significance 提到 Critical；查 EffectType 预算配置。

### 最快自检路径
用工程 fork 自带的编辑器平台预览开关（`NiagaraWorldManager.cpp:408`）：
```
fx.Niagara.Editor.TestPlatform Android
fx.Niagara.Editor.TestPlatform Windows
```
编辑器里直接切 Android 看资产：
- **切过去不显示** → ①②⑦ 资产配置问题（占 80%）
- **切过去黑/异常** → ③④⑤ 材质/渲染/DI
- **切过去正常、真机不显示** → ⑥⑧ 运行时 CVar/预算

---

## 六、相关源码参考（本仓 UE5.5.4 fork）

- 转换工具：`UE5EA/Engine/Plugins/FX/CascadeToNiagaraConverter/Content/Python/`（入口 `ConvertCascadeToNiagara.py`）
- 移动端判定：`UE5EA/Engine/Plugins/FX/Niagara/Source/Niagara/Private/NiagaraWorldManager.cpp:2282-2324,362,374,417,447,2364,2379`
- 模拟频率/剔除距离：`NiagaraSystem.cpp:1767-1808,1860-1862,1896-1898`
- Mesh GPU Scene：`NiagaraRendererMeshes.cpp:385,488,535`、`NiagaraVertexFactories/Private/NiagaraMeshVertexFactory.cpp:200,220`
- 粒子灯光：`NiagaraRendererLights.cpp:70,75-81`、`RenderCore/Public/RenderUtils.h:322-334`
- 骨骼 DI：`NiagaraDataInterfaceSkeletalMesh.cpp:2578`
- PlatformSet：`NiagaraPlatformSet.cpp:687,1373`
- Significance/平台门控：`NiagaraEmitter.cpp:904,1530-1571`（fork 定制 `ENiagaraPlatform`）
- GPU 粒子总开关：`NiagaraCommon.cpp:30-52`
- 工程配置：`S1Game/Config/Android/AndroidScalability.ini:297-378,505-508`（EffectsQuality→`fx.Niagara.QualityLevel` 映射、`UseSupressActivateList=1`）；`S1Game/Config/DefaultNiagara.ini`（当前仅 `bExperimentalVMEnabled`，PlatformSet/EffectType 待补）
- 编辑器平台预览：`NiagaraWorldManager.cpp:406-414`（`fx.Niagara.Editor.TestPlatform`，fork 定制）
