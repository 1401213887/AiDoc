# UE Mobile：HZB / Occlusion 控制 CVar 全开关参考

> 基线：UE 5.5.4 source fork（分支 `++GR+DevTest`）
> 定位：**查/关 Occlusion 的速查手册**。每个 CVar 均带实测源码位置（2026-08-25 复核），默认值以本 fork 实际代码为准（与官方 UE 有差异，见 §2 红色标注）。
> 关联文档：`MobileRenderPath/UE_Mobile_Tech_DeepDive_01_Occlusion.md`（实现原理/调度时序，本文档只聚焦 CVar 开关与联动坑）。

---

## 0. 一页纸结论

- 引擎里有**四套独立的遮挡系统**，各由一个主 CVar 控制：硬件查询 / HZB / CPU 软件遮挡 / GPU InstanceCulling。
- **本 fork 默认基本全关**：`r.HZBOcclusion`、`r.Mobile.AllowSoftwareOcclusion`、`r.InstanceCulling.OcclusionCull` 默认都是 **0**；真正默认开着、需要手动关的是 **`r.AllowOcclusionQueries`（默认 1，硬件遮挡查询）**。
- **"关闭所有 Occlusion" 一键命令**：
  ```console
  r.AllowOcclusionQueries 0          # 硬件遮挡查询（默认 1，主开关）
  r.HZBOcclusion 0                   # HZB 遮挡（本 fork 默认已 0）
  r.Mobile.AllowSoftwareOcclusion 0  # CPU 软件遮挡 / SDOC（本 fork 默认已 0）
  r.Mobile.AllowSDOC 0               # Snapdragon 专用开关（默认 0）
  r.InstanceCulling.OcclusionCull 0  # GPU InstanceCulling 逐实例遮挡（默认 0）
  ShowFlag.DisableOcclusionQueries 1 # ShowFlag 兜底（同上）
  ```
  想连 GPU InstanceCulling 整块剔除都关掉，再加 `r.CullInstances 0`。
- **已知 GR fork 坑**：`r.Mobile.AllowSoftwareOcclusion=3`（Snapdragon Occlusion）+ Android Vulkan 会触发 **silent heap corruption**（0xc0000005 / ntdll），非默认配置，勿开。

---

## 1. 四套遮挡系统总表

| CVar / 开关 | 默认(本 fork) | 作用 | 源码位置 |
|---|---|---|---|
| `r.AllowOcclusionQueries` | **1** 🔴 | 硬件遮挡查询总开关。`DoOcclusionQueries()` = 该值 && `!ShowFlag.DisableOcclusionQueries` && `!SimpleSceneRendering` | `SceneVisibility.cpp:466-478` |
| `ShowFlag.DisableOcclusionQueries` | 0 | ShowFlag 版遮挡查询开关（SFG_Developer） | `ShowFlagsValues.inl:404` |
| `r.HZBOcclusion` | **0** 🔴 | 0=硬件查询，1=HZB 遮挡，2=强制 HZB。Mobile 下 HZB 生效还要求 `r.Mobile.AllowSoftwareOcclusion==0` | `SceneVisibility.cpp:137-146`；联动 `MobileShadingRenderer.cpp:744` |
| `r.HZB.IndirectDraw` | 0 | GR 新增：HZB 测试结果写 indirect buffer，GPU 上剔除（用上一帧 HZB 纹理） | `SceneVisibility.cpp:148-155`；消费 `SceneOcclusion.cpp:2344` / `InstanceCullingContext.cpp:1584` |
| `r.Mobile.AllowSoftwareOcclusion` | 0 | 0=关，1=UE4 CPU 软件遮挡，2=新版软件遮挡，3=Snapdragon (SDOC)。**=3 有 GR 崩溃坑** | `SceneOcclusion.cpp:104-112`；分配点 `SceneVisibility.cpp:6195-6198` |
| `r.Mobile.AllowSDOC` | 0 | Snapdragon Occlusion 专属叠加开关 | `SceneOcclusion.cpp:114-119` |
| `r.Mobile.EnableOcclusionExtraFrame` | true | 遮挡剔除是否允许多一帧（延迟结果，关闭可减少迟滞） | `SceneOcclusion.cpp:122-127` |
| `r.EnableComputeBuildHZB` | 1 | 0=用 graphics 管线建 HZB，1=用 compute | `SceneOcclusion.cpp:129-135` |
| `r.CullInstances` | 1 | GPU InstanceCulling **总开关**（不是遮挡，是整块剔除） | `InstanceCullingContext.cpp:37-41` |
| `r.InstanceCulling.OcclusionCull` | **0** 🔴 | GPU 实例化逐实例遮挡剔除（依赖 HZB） | `InstanceCullingContext.cpp:43-47` |
| `r.InstanceCulling.ForceInstanceCulling` | 0 | 强制逐实例遮挡剔除（绕过自动开关） | `InstanceCullingContext.cpp:49-54` |

> 🔴 = 与官方 UE 默认不同的项。官方 `r.HZBOcclusion` 默认 **1**、`r.InstanceCulling.OcclusionCull` 默认 **1**；本 fork 全部降成 0，即**本 fork 平时遮挡主要靠硬件查询**。
> ⚠️ `r.Mobile.AllowHZB`：在部分旧文档/cheatsheet 中出现，但 **本 fork 源码中不存在**，未验证，勿使用。

---

## 2. 各系统判定逻辑（源码逐条）

### 2.1 硬件遮挡查询 —— `r.AllowOcclusionQueries`

`SceneVisibility.cpp:475-478`：

```cpp
bool FSceneRenderer::DoOcclusionQueries() const
{
    return GOcclusionCullEnabled
        && !ViewFamily.EngineShowFlags.DisableOcclusionQueries
        && !ViewFamily.EngineShowFlags.SimpleSceneRendering;
}
```

- `GOcclusionCullEnabled` 由 `r.AllowOcclusionQueries` 驱动（`ECVF_RenderThreadSafe | ECVF_Preview`）。
- 关掉它，硬件查询完全不发；但也意味着**所有本应被遮挡的物体都会画**，draw call 上升。

### 2.2 HZB 遮挡 —— `r.HZBOcclusion` + `r.HZB.IndirectDraw`

`SceneVisibility.cpp:137-146`（CVar 定义），`MobileShadingRenderer.cpp:744`（生效判定）：

```cpp
// MobileShadingRenderer.cpp:742-744
const bool bHZBOcclusion = CVarHZBOcclusion->GetInt() != 0
                        && CVarMobileAllowSoftwareOcclusion->GetInt() == 0;
```

- **HZB 与 SoftwareOcclusion 互斥**：移动端二选一。`r.HZBOcclusion=1` 时若 `AllowSoftwareOcclusion!=0`，HZB 路径自动失效。
- `r.HZB.IndirectDraw=1` 时，HZB 测试结果由 GPU 直接写 indirect buffer 剔除，**不上读 CPU**（用上一帧 HZB 纹理），是 GR 针对 Adreno 的间接绘制优化。

### 2.3 CPU 软件遮挡 —— `r.Mobile.AllowSoftwareOcclusion`

`SceneOcclusion.cpp:104-112`（值 1/2/3），分配在 `SceneVisibility.cpp:6195-6200`（GR 注释标注，按 CVar 在 CPU 光栅化场景求遮挡）。

- **1**：UE4 软件遮挡；**2**：新版软件遮挡；**3**：Snapdragon Occlusion（SDOC）。
- 开启后完全避开 GPU OcclusionQuery 的一帧延迟、避开 Adreno driver bug、避开 HZB Resolve 成本，**但 CPU 侧光栅化开销大**，且 =3 在本 fork 有崩溃坑（见 §4）。

### 2.4 GPU InstanceCulling 遮挡 —— `r.InstanceCulling.OcclusionCull`

`InstanceCullingContext.cpp:43-47`。逐实例遮挡剔除依赖 HZB 纹理（`InstanceCullingContext.h:114`：InPrevHZB 非空且 `r.InstanceCulling.OcclusionCull` 开启才启用）。关掉只影响逐实例粒度，不动网格粒度剔除。

---

## 3. 联动链：HZB ↔ `bKeepDepthContent`（深度保留）

`MobileShadingRenderer.cpp:747-760`：

```cpp
bKeepDepthContent =
    bRequiresMultiPass ||
    bForceDepthResolve ||
    ...
    bShouldRenderHZB ||              // 开 HZB 必须保留深度
    bHZBOcclusion ||                 // 开 HZB Occlusion 同样要求
    ...;
```

- **开 HZB 会强制 SceneDepth 出 Tile 到主存**，对 TBDR 中低端机型是实打实的带宽损耗。
- 全关 Occlusion 后，这条链上 `bShouldRenderHZB` / `bHZBOcclusion` 归零 → 深度可走 memoryless。
- ⚠️ **但本 fork 的深度 memoryless 被禁用**（`MobileShadingRenderer.cpp` 内 `//ericado` 注释，GR 定制），所以"全关省带宽"的收益目前拿不到，除非一并处理深度消费方（toon outline / HZB / SceneDepth 节点 / SSXR）。

---

## 4. GR fork 已知坑

| 坑 | 现象 | 规避 |
|---|---|---|
| `r.Mobile.AllowSoftwareOcclusion=3` + Android Vulkan | **silent heap corruption**（0xc0000005 / ntdll），非断言的堆损坏 | **非默认配置，勿开**；崩溃线索为 `State.bHZBOcclusion` 与 `RenderOcclusion` 不一致 |
| 深度 memoryless 禁用 | 全关 Occlusion 的省带宽红利不可用 | 需要时按 `//ericado` 定制链路逐点恢复深度消费方 |
| HZB / SoftwareOcclusion 互斥被忽略 | 设了 `r.HZBOcclusion=1` 但 `AllowSoftwareOcclusion!=0`，HZB 不生效 | 两个一起查，别只看一个 |

---

## 5. 启动期设置方法

- **免改 ini**：`-dpcvars=r.AllowOcclusionQueries=0`（启动期 CVar 注入）。
  - ⚠️ 本 fork 用 `-dpcvars` 有个已知限制：**`ECVF_Cheat` 类 CVar 不被 DeviceProfile 应用，启动期设不上，只能控制台设**（记忆：`ecvf-cheat-blocks-dpcvars`）。本文档列的都是非 Cheat，可用 `-dpcvars`。
- **控制台运行时**：直接 `r.HZBOcclusion 0` 等命令，即时生效（多为 `ECVF_RenderThreadSafe`，个别 `ECVF_Preview`）。
- **ini / DeviceProfile**：写进 `ConsoleVariables.ini` 或对应 DeviceProfile 的 `CVars` 段（注意 Cheat CVar 例外）。

---

## 6. 场景配置模板

| 场景 | 推荐配置 |
|---|---|
| 大世界 / 室外（剔除收益大） | `r.HZBOcclusion=1` + `r.HZB.IndirectDraw=1` + `r.Mobile.AllowSoftwareOcclusion=0` |
| 紧凑室内 / 遮挡少 | `r.HZBOcclusion=0` + `r.Mobile.AllowSoftwareOcclusion=1`（或全关，靠深度/带宽换简单） |
| 排查问题 / 与遮挡无关的功能验证 | 全部置 0（见 §0 一键命令） |
| Adreno 特化（SDOC） | `r.Mobile.AllowSoftwareOcclusion=3` —— **本 fork 崩溃，禁用** |

---

## 7. 参考资料（本 fork 源码位置）

- `UE5EA/Engine/Source/Runtime/Renderer/Private/SceneVisibility.cpp` — `r.HZBOcclusion`(:137)、`r.HZB.IndirectDraw`(:148)、`r.AllowOcclusionQueries`(:466)、软件遮挡分配(:6195)
- `UE5EA/Engine/Source/Runtime/Renderer/Private/SceneOcclusion.cpp` — `r.Mobile.AllowSoftwareOcclusion`(:104)、`r.Mobile.AllowSDOC`(:114)、`r.Mobile.EnableOcclusionExtraFrame`(:122)、`r.EnableComputeBuildHZB`(:129)
- `UE5EA/Engine/Source/Runtime/Renderer/Private/InstanceCulling/InstanceCullingContext.cpp` — `r.CullInstances`(:37)、`r.InstanceCulling.OcclusionCull`(:43)、`r.InstanceCulling.ForceInstanceCulling`(:49)
- `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp` — `bShouldRenderHZB`(:734)、`bHZBOcclusion`(:744)、`bKeepDepthContent`(:747)
- `UE5EA/Engine/Source/Runtime/Engine/Public/ShowFlagsValues.inl` — `ShowFlag.DisableOcclusionQueries`(:404)
