# UE5 移动端单包双管线：一次 cook 产出 Forward + Deferred Shader

把「cook 期全局单值决定管线」改造成「一次 cook 双遍编译 + 带版本标记的序列化」，同一个包在 Forward / Deferred 两种设备上都能正常运行

**TL;DR** — S1Game（UE 5.5.4 fork + Vulkan/ASTC）原本一次 cook 只能产出一条移动渲染管线的 shader：cook 侧的管线判定 `IsMobileDeferredShadingEnabled()` 是全局单值，另一条管线的所有 `ShouldCompilePermutation` 直接返回 false。本次改造让 **一次 cook 编出两套 shader map**，用 4 字节 tag 区分「单管线 / 双管线」两种 cooked 布局，运行时按当前 DeviceProfile 只反序列化其中一套。

**结果** — 包体从 38.6 GB 增至 40.4 GB（**+2.1 GB，约 5%**），材质 shader 从 108,823 增至 **248,474**（2.28×）；真机同设备同点位，Forward（`r.Mobile.ShadingPath=0`）与 Deferred（`=1`）两条管线画面均完整，`Unknown inline shader map tag` / `resynchronizing` / `Fatal` 全为 0；PSO stable key 冲突从 254 降到 **0**。

## 一、问题与结论

### 1.1 原始问题

同一个 apk 在不同 DeviceProfile 的设备上表现不一致：cook 时按 ini 走一条管线，设备按 DeviceProfile 推另一条，启动即崩在 `WorldGridMaterial` 缺 shader 变体。S1Game 的实际配置正是这种分叉：`S1Game/Config/DefaultEngine.ini:523` 钉死 `r.Mobile.ShadingPath=0`（Forward，本地工作副本），而 `S1Game/Config/DefaultDeviceProfiles.ini:1836` 的 `Android_High` 档推 `=1`（Deferred）。

> **注意** — 补充一个容易忽略的事实：P4 head 版的 `DefaultEngine.ini` **没有**这一行，此时引擎默认值来自 `UE5EA/Engine/Config/BaseEngine.ini:3805` 的 `r.Mobile.ShadingPath=1`（Deferred）。也就是说「哪条是原生管线」取决于是谁的机器 —— 这一点后面直接影响了 key 的设计（见第六章）。

### 1.2 达成的结果

| 验收项 | 结果 | 证据 |
|---|---|---|
| 编译正确 | ✅ 双平台 0 新错误 | `UnrealEditor-Engine.dll` 时间戳晚于全部改动源码；仅 7 个既有插件错误（TraceMcpS1 / MovieSceneTextTrack / UIFramework），与本改动无关 |
| 出包正确 | ✅ `ExitCode=0`，1h28m | 材质 shader **248,474** 个；`Duplicate stable key` 0；`has no valid Deferred` 0 |
| 装包成功 | ✅ 8 个 pak 字节级一致 | 设备端 pak 大小与 `Saved/StagedBuilds` 逐一比对相同，合计 40.43 GB |
| 两条管线运行时正常 | ✅ 同点位画面完整 | Forward `Frame 27.6ms / Draws 1396`；Deferred `29.3ms / 1231`；两侧 `Unknown tag`、`resynchronizing`、`Missing shader resource`、`Fatal` 均为 0 |

### 1.3 代价

| 项 | 单管线 | 双管线 | 增量 |
|---|---|---|---|
| 材质 shader 数 | 108,823 | 248,474 | **2.28×** |
| shader 库体积 | 865.5 MB | 2,818.8 MB | **+1.95 GB** |
| 材质 uexp（19,405 个共有） | 178 MB | 333 MB | **+155 MB** |
| pak 总量 | 38.61 GB | 40.43 GB | **+1.82 GB**（实测） |
| cook 时长（冷 DDC） | — | 约 2.5–3 小时 | 双遍编译 |
| cook 进程内存 | — | 62–75 GB | 两套 map 常驻 |

**纯双管线的真实代价约 2.1 GB**，占包体 5%。pak 实测差 1.82 GB 之所以更小，是因为对照的 CI 包与本地包 CL 不同、非 shader 内容本就有 0.3 GB 级差异 —— 不要用 pak 差值当双管线成本，要用 shader 库 + 材质 uexp。

## 二、根因：cook 期的管线判定是全局单值

这一章解释「为什么原本只能出一条管线」，以及为什么两条管线无法合并成一套 shader。

### 2.1 判定链路

| 位置 | 内容 |
|---|---|
| `UE5EA/Engine/Source/Runtime/Core/Private/HAL/ConsoleManager.cpp:3677` | `r.Mobile.ShadingPath` 定义，默认 0（Forward），`ECVF_RenderThreadSafe | ECVF_ReadOnly` |
| `UE5EA/Engine/Source/Runtime/RenderCore/Private/ReadOnlyCVARCache.cpp:79` | `MobileShadingPathIniValue`：用 `FShaderPlatformCachedIniValue<bool>` 读 ini，并要求平台支持（OpenGL 默认不支持 Deferred） |
| `UE5EA/Engine/Source/Runtime/RenderCore/Public/ReadOnlyCVARCache.h:141` | `MobileDeferredShading(Platform)`：**EDITOR 走 ini 查询，非 EDITOR 走 `Initialize()` 缓存的静态值** |
| `UE5EA/Engine/Source/Runtime/RenderCore/Public/RenderUtils.h:322` | `IsMobileDeferredShadingEnabled = MobileDeferredShading(Platform) && IsMobileHDR()` |

cook 进程虽然是编辑器，但整条编译链路最终都收敛到这一个 bool。**两条管线在同一个进程里不可能同时为真**，这就是「一次 cook 只能出一条」的本质。

### 2.2 被剔掉的到底是什么

以 base pass 为例，`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.h:457`：

```
const bool bForwardShading = !bDeferredShadingEnabled || bMaterialUsesForwardShading;
EMobileLocalLightSetting SupportedLocalLightsType = EMobileLocalLightSetting::LOCAL_LIGHTS_DISABLED;
if (bForwardShading && bIsLit) { SupportedLocalLightsType = GetMobileForwardLocalLightSetting(...); }
const bool bShouldCacheByLocalLights = !bEnableLocalLights || (bIsLit && (SupportedLocalLightsType == LocalLightSetting));
```

Deferred cook 时 `bForwardShading=false` ⇒ `SupportedLocalLightsType=DISABLED` ⇒ 所有 `LOCAL_LIGHTS_ENABLED` / `LOCAL_LIGHTS_BUFFER` 的 base pass perm 被剔除。这就是崩溃日志里 `TMobileBasePassPSFNoLightMapPolicyLOCAL_LIGHTS_ENABLED` 缺失的来源。同样的三段式判定在 `MobileBasePassRendering.h:487`、`MobileBasePassRendering.h:515` 各重复一次。

### 2.3 为什么两套无法合并成一套

管线是通过**全局编译宏**注入到每个 shader 的，两条管线的同一份 usf 编译出来是不同二进制：

| 宏 | 注入方式 | 是否进 shader hash |
|---|---|---|
| `MOBILE_DEFERRED_SHADING` | `SET_SHADER_DEFINE_AND_COMPILE_ARGUMENT`（`ShaderCompiler.cpp:3523`） | **是**（进 CompileArgs） |
| `FORWARD_SHADING` | `SET_SHADER_DEFINE` | 只进 Definitions |
| `USE_GLES_FBF_DEFERRED` / `MOBILE_EXTENDED_GBUFFER` | 仅 Deferred 分支 | 是 |
| `USE_IES_PROFILE` | 仅 Forward 分支 | 是 |

`SET_SHADER_DEFINE_AND_COMPILE_ARGUMENT` 双写 Definitions 和 CompileArgs，而 `FShaderCompilerEnvironment::SerializeCompilationDependencies` 会序列化 CompileArgs —— 所以 `MOBILE_DEFERRED_SHADING` 一定改变 shader hash。**两条管线几乎零共享，这是包体代价的来源。**

### 2.4 方案可行的支点

好消息：shader map 的 **key 天然已经按管线区分**。`UE5EA/Engine/Source/Runtime/RenderCore/Private/Shader.cpp` 的 `ShaderMapAppendKey` 里，Deferred 会追加 `MobDSh`（或 `MobDShEx`），Forward 原本什么都不追加。因此两套 shader map 在 DDC 与 ShaderCodeLibrary 里天然共存、不会互相覆盖。（改造时顺手给 Forward 补了显式 `MobFwd`，见 `Shader.cpp:1981`。）

### 2.5 真正的瓶颈：一份资产只存一个 ResourceHash

启用共享 shader code 后（`S1Game/Config/DefaultGame.ini:119  bShareMaterialShaderCode=True`），材质 uexp 里每个 shader map 只写**一个** `ResourceHash`：

| 时机 | 位置 | 行为 |
|---|---|---|
| cook 保存 | `UE5EA/Engine/Source/Runtime/RenderCore/Private/ShaderMap.cpp:290` | `Ar << ResourceHash` 后调 `FShaderLibraryCooker::AddShaderCode` |
| 运行时加载 | `UE5EA/Engine/Source/Runtime/RenderCore/Private/ShaderMap.cpp:319` | `Ar << ResourceHash` 后 `FShaderCodeLibrary::LoadResource` |

**一个材质资产只指向一套 shader map** —— 这是改造必须动序列化格式的根本原因，也是本次所有兼容性设计的出发点。

## 三、最终设计：双 shader map + 带 tag 的 inline 序列化

设计的三个支柱，对应三个问题：

| 支柱 | 解决的问题 | 落点 |
|---|---|---|
| **编译期可覆盖** | cook 时如何让两条管线依次为真 | `thread_local` override + RAII scope |
| **双 map 持有** | 同一个 `FMaterialResource` 如何同时留住两套结果 | `SecondaryGameThreadShaderMap` 槽位 |
| **带 tag 的序列化** | cooked 格式如何既装两套、又不破坏旧包 | 4 字节 tag：0/1/2 |

**全景数据流**（cook → 包 → 运行时）

| 阶段 | 内容 |
|---|---|
| ① cook 进程 | 同一材质编两遍：`scope=1` 得 Deferred、`scope=0` 得 Forward，两套 map 的 `MobileShadingPath` 分别为 1 / 0，key 因 `MobDSh`/`MobFwd` 不同 |
| ① cook 进程 | GlobalShader 也双遍：Deferred 遍先跑并写 `-Deferred.bin`，再跑原生遍写普通文件 |
| ② 包 | 材质 uexp 末尾是 `tag=2` + 两份带 size 前缀的 map；`ShaderArchive` 合并两套 bytecode；两个 `GlobalShaderCache-*.bin` |
| ② 包 | 38～40 GB 中贴图/音频/地图两条管线共用；双管线真正增量约 2.1 GB（≈5%） |
| ③ 运行时 | Deferred 设备读 secondary、Forward 设备读 primary，另一份只 Seek 跳过（不 `LoadResource`、不建内存镜像） |
| ③ 运行时 | global：Deferred 优先找 `-Deferred.bin`，找不到回退普通文件 |

### 3.1 编译期覆盖

`FReadOnlyCVARCache` 加一个 `thread_local` 覆盖值（`ReadOnlyCVARCache.cpp:16`），并在 `MobileDeferredShadingIniValue` 里让 override 优先于 ini（`ReadOnlyCVARCache.cpp:94`），RAII 结构 `FMobileShadingPathScope`（`ReadOnlyCVARCache.h:129`）保证「设置 → 编译 → 恢复」闭环。

选择 `thread_local` 而不是全局变量，是因为 shader 的 perm 枚举与编译环境设置都在 **game thread 同步完成**，worker 线程只拿到已生成好的 job environment —— 线程局部足够，且不会在并行编译时互相污染。

### 3.2 双 map 持有

`FMaterial` 增加 `SecondaryGameThreadShaderMap`（`MaterialShared.h:3025`，`#if WITH_EDITOR` 内，发布包零开销），配合 `MoveGameThreadShaderMapToSecondary()`（`MaterialShared.cpp:1627`）把第一遍结果挪走、给第二遍腾出 primary 槽位。

### 3.3 带 tag 的序列化

这是整份设计里最关键、也最容易出错的一环，单独放在第六章展开。

### 3.4 开关

`r.Mobile.DualPipelineCook`（`Material.cpp:2861`），默认 0 = 行为完全不变。它是 `ECVF_ReadOnly`，只能从 ini / `-dpcvars` 注入，运行期改不动 —— 这正是我们想要的。

## 四、一次 cook 的完整链路

跟着**一个材质的 shader map** 走一遍：从 cook 编译 → 序列化进包 → 运行时加载 → 选中一条管线。

### 4.1 cook：双遍编译

入口是 `UMaterial::CacheResourceShadersForCooking`（`Material.cpp:2946`）。开关打开时不再走原来的单遍，而是调用共用流程 `CacheDualMobilePipelineResources`（`Material.cpp:2908`）：

**cook 期双遍时序**

1. `FMobileShadingPathScope(1)` → 编 Deferred，得 `GameThreadShaderMap`
2. `FinishAll()` → 等编译**真正完成**（关键，见下方说明）
3. `MoveGameThreadShaderMapToSecondary()` → 结果移到 secondary，primary 置空
4. `FMobileShadingPathScope(0)` → 编 Forward

**为什么第 2 步不能省**：异步编译回填的是「原始 map」，而 `AcquireFinalizedClone()` 给的是提交时的冻结快照。不等就 Move，secondary 拿到的是空快照；且 Forward 遍 `SetCompilingShaderMap` 会 `ReleaseGameThreadCompilingShaderMap` → 无其它依赖时 `CancelCompilation`，Deferred 编译被取消。

**第 4 步的收口方式取决于 ini 原生管线**：原生 = Forward 且非 blocking 时走 `BeginCacheShadersResources` 异步流水；其余情况在 scope 内同步 `CacheShaders` + `FinishAll`。结束时当前态都落在 ini 原生管线上，与优化前行为一致。

`UMaterialInstance` 的静态排列资源同样要双遍（`MaterialInstance.cpp:3267` 调用同一个共用流程）—— **这里曾经漏过一次**：第一版只改了 `UMaterial`，导致所有带 static switch 的材质实例（含 Landscape MIC）在 Deferred 设备上仍然缺 perm。

### 4.2 序列化：一个 tag 决定布局

`FMaterial::SerializeInlineShaderMap`（`MaterialShared.cpp:1863`）在材质序列化末尾执行。它写的第一样东西决定了后面怎么读：

**材质 uexp 末尾 inline shader map 的三种布局**

| tag | 布局 | 说明 |
|---|---|---|
| 0 | `[tag]`，其后无 shader map | 未 cook 的保存 |
| 1 | `[tag][bValid][shader map 原样]` | cooked 单管线，**与上游字节级一致** |
| 2 | `[tag][bValid][int64 size][primary(Forward)][int64 size][secondary(Deferred)]` | cooked 双管线 |

- `tag` 用 4 字节 uint32 写出，**取代原来的 `bool bCooked`（同样是 4 字节）** —— 这是能做到字节级兼容的前提。
- `bValid=false`（Forward 编不过、Deferred 可用）时：不写 primary，只写 secondary。
- `size` 前缀的作用：加载时以它为界 Seek，子序列化多读/少读都能校正。

### 4.3 global shader 的双份

global shader 走另一条路径。`GetGlobalShaderMapKeyString`（`ShaderCompilerEditor.cpp:180`）在双管线 cook 时给 DDC key 追加 `_MSP%d`（`ShaderCompilerEditor.cpp:191`），让两遍 DDC 不互相命中；`RecompileShadersForRemote`（`ShaderCompilerEditor.cpp:420`）负责编排两遍顺序：

1. 先 `SetMobileShadingPathOverride(1 - NativeShadingPath)`，即先跑**非原生**遍，`CompileGlobalShaderMap(..., true)` 重建并 `SaveGlobalShaderFile` 落盘
2. 再 `SetMobileShadingPathOverride(NativeShadingPath)`，回到原生管线重建
3. 随后走原有流程（`CompileGlobalShaderMapForRemote` + `SaveGlobalShaderFile`）
4. `ON_SCOPE_EXIT` 恢复调用前的 override

文件名规则：只有 `override == 1`（正在编 Deferred）那一遍写 `GlobalShaderCache-<P>-Deferred.bin`（`ShaderCompiler.cpp:5178` `IsSavingDeferredGlobalShaderVariant`、`ShaderCompiler.cpp:5205` `GetGlobalShaderCacheFilename`）。这条规则与「原生管线是谁」无关，所以本地（ini=Forward）和 CI（ini=Deferred）产出的映射恒为「普通文件 = Forward、`-Deferred.bin` = Deferred」。

运行时 `TryLoadCookedGlobalShaderMap`（`ShaderCompiler.cpp:5374`）在 Deferred 设备上优先找 `-Deferred.bin`，不存在则回退普通文件（`ShaderCompiler.cpp:5427` `bUseDeferredVariant`）—— 这样单管线 Deferred 的 CI 包也能正常加载。

### 4.4 运行时：只反序列化一份

**运行时 `SerializeInlineShaderMap` 的选择逻辑**

1. 读 `tag`(uint32)
2. `tag=1` → 按上游原路径读一份
3. `tag=2` → 读 `bValid`，再看当前管线：
   - **Deferred**：跳过 primary（Seek size）→ 读 secondary
   - **Forward**：`bValid ? 读 primary : 跳过` → 跳过 secondary
4. 校正：读完后若 `Tell != Start + Size`，打 `resynchronizing` 并 Seek 回 `Start + Size`
5. 最终选用的 map 缺失时才报 Error（此前是主 map 无效就报，Deferred 设备上会误报）

跳过的那一份**完全不碰** `FShaderCodeLibrary::LoadResource`，也不建内存镜像 —— 这是相对第一版（两套都完整反序列化再丢掉一套）的实质优化。

## 五、关键代码改动清单

共 15 个文件，全部用 `#pragma region Engine ZXB` 包裹，挂在 CL **1239741**（未提交）。

### 5.1 RenderCore

| 文件:行 | 改动 |
|---|---|
| `ReadOnlyCVARCache.cpp:16` | `thread_local int32 GMobileShadingPathOverride = -1`（-1 = 无覆盖） |
| `ReadOnlyCVARCache.cpp:94` | `MobileDeferredShadingIniValue` 里 override 优先于 ini，但仍保留 OpenGL 平台支持检查 |
| `ReadOnlyCVARCache.h:129` | `FMobileShadingPathScope` RAII（构造存旧值、析构恢复） |
| `ReadOnlyCVARCache.h:141` | `MobileDeferredShading(Platform)` 入口不变，override 下沉到 IniValue 层 |
| `Shader.cpp:1981` | Forward 分支补显式 `MobFwd` 标记（**只在移动端**追加，恢复上游对非移动端 `MobFCR` 的处理） |
| `GlobalShader.h:299` / `GlobalShader.cpp:640` | 新增 `FGlobalShaderMap::SaveShaderStableKeys(Platform, BaseKeyVal)` 重载，供带标签的 global stable key 使用 |

### 5.2 Engine — 材质

| 文件:行 | 改动 |
|---|---|
| `MaterialShared.h:1301` | `FMaterialShaderMapId` 加 `int32 MobileShadingPath = 0`（默认初始化：非编辑器分支不初始化它，而 `GetMaterialHash` 会读） |
| `MaterialShared.h:3025` | `FMaterial` 加 `SecondaryGameThreadShaderMap`（`#if WITH_EDITOR`） |
| `MaterialShared.h:2743` / `MaterialShared.cpp:1627` | `MoveGameThreadShaderMapToSecondary()` |
| `Material.cpp:2861` | `r.Mobile.DualPipelineCook`（**无条件注册**，不放在 `WITH_EDITOR` 内，避免发布包建 dummy 变量） |
| `Material.cpp:2870` / `Material.cpp:2874` | `ShouldCookDualMobilePipeline`，含 `IsRunningCommandlet()` 与「两条管线都成立」双重守卫 |
| `Material.cpp:2894` | `IsMobileDeferredShadingNative`：临时清空 override 读 ini 原生管线（只用于决定异步/同步与双遍顺序，**不参与任何 key**） |
| `Material.cpp:2900` | `GetDualPipelineStableKeyFeatureLevel`：给 Forward 侧 stable key 加 `_MobFwd` |
| `Material.cpp:2908` | `CacheDualMobilePipelineResources`：双遍共用流程 |
| `Material.cpp:2946` | `UMaterial::CacheResourceShadersForCooking` 接入双遍 |
| `MaterialInstance.cpp:3267` | `UMaterialInstance::CacheResourceShadersForCooking` 接入同一流程 |
| `MaterialShared.cpp:1128` | `GetShaderMapId` 写 `MobileShadingPath`（仅移动端） |
| `MaterialShared.cpp:1835` / `MaterialShared.cpp:1839` / `MaterialShared.cpp:1855` | tag 常量、`SaveSizedInlineShaderMap`、`SkipSizedInlineShaderMap` |
| `MaterialShared.cpp:1863` | `SerializeInlineShaderMap`：tag 写出 + 双布局 + 加载选择 + Seek 校正 |
| `MaterialShared.cpp:3823` | `GetUniqueAssetName`：Forward 侧追加 `MobFwdPath`（`AppendKeyString` 的 context 无真实平台，这里补） |
| `MaterialShared.cpp:5468` | `SaveShaderStableKeys`：同时写 secondary，Forward 侧 FeatureLevel 加 `_MobFwd` |
| `MaterialShader.cpp:1018` | `FMaterialShaderMapId::Serialize` 序列化 `MobileShadingPath`（仅非 cooked 路径） |
| `MaterialShader.cpp:1169` | `GetMaterialHash` 混入 `MobileShadingPath` |
| `MaterialShader.cpp:1265` | `Equals` 比较 `MobileShadingPath` |
| `MaterialShader.cpp:1499` / `MaterialShader.cpp:1535` | `RecordAndEmit`：双管线 cook 时给 Forward 一侧 Emit `MobFwdPath` |
| `MaterialShader.cpp:2947` | layout 缓存 key 混入 ShadingPath（否则第二遍复用第一遍的 perm 集合） |

### 5.3 Engine — global shader

| 文件:行 | 改动 |
|---|---|
| `ShaderCompiler.cpp:5178` / `ShaderCompiler.cpp:5183` | `IsSavingDeferredGlobalShaderVariant`（判 override==1）/ `WantsDeferredGlobalShaderVariant`（判当前设备管线） |
| `ShaderCompiler.cpp:5205` / `ShaderCompiler.cpp:5212` | cache 文件名带可选 `-Deferred` 后缀 / `SaveGlobalShaderFile` 按当前遍决定落哪个文件，并按前后 side 写 stable key |
| `ShaderCompiler.cpp:5374` / `ShaderCompiler.cpp:5427` | `TryLoadCookedGlobalShaderMap`：Deferred 优先 `-Deferred.bin`，否则回退；用到变体时释放预加载数据 |
| `ShaderCompilerEditor.cpp:180` / `ShaderCompilerEditor.cpp:191` | `GetGlobalShaderMapKeyString` 追加 `_MSP%d`（仅双管线 cook、且用「有效管线」而非原始 override） |
| `ShaderCompilerEditor.cpp:420` / `ShaderCompilerEditor.cpp:427` | `RecompileShadersForRemote` 双遍编排（限定整平台 cook：`SP_NumPlatforms` + Global/Changed） |

### 5.4 Core

| 文件:行 | 改动 |
|---|---|
| `DevObjectVersion.cpp:137` | `MATERIALSHADERMAP_DERIVEDDATA_VER` 尾字节 `...9a` → `...9b`（payload 加字段必须让旧 DDC 失效） |

## 六、兼容性与版本标记

这一章单独讲，因为它是**整份改造最容易埋雷的地方**，也是后续维护者的第一道门槛。

### 6.1 为什么不能用 custom version

cooked 包是 **unversioned** 的：加载时一律取当前引擎的 custom version，包里不保存历史版本号。所以「加个 version 字段做新旧判断」这条路走不通 —— 必须靠**数据本身的标记**。

解法：把顶部那个 `bool bCooked` 换成 4 字节 `uint32` tag。`bool` 在编辑器构建里序列化为 `uint32`（`UE5EA/Engine/Source/Runtime/Core/Private/Serialization/Archive.cpp:497` 的 `SerializeBool` 写的是 `uint32 OldUBoolValue`），所以**宽度完全一致**。

| tag | 含义 | 布局 | 谁写 |
|---|---|---|---|
| 0 | 未 cook 的保存 | 无 shader map | 编辑器常规保存 |
| 1 | cooked 单管线 | **与上游字节级一致** | 单管线 cook，或双管线 cook 中拿不到 secondary 的材质 |
| 2 | cooked 双管线 | sized 双布局 | 双管线 cook 且 secondary 有效 |

关键性质：**只有真的有两套时才写 2，其余一律写 1**。因此单管线 cook 的产物与上游完全相同，新引擎读旧包、旧引擎读「非双管线的新包」都不受影响。

未知 tag（>2）走 `Error + Ar.SetError()` 而不是 `Fatal` —— 与上游对非法 bool 的处理级别一致，避免坏流直接崩进程。

### 6.2 兼容矩阵

| 设备管线 \ 包 | 新双管线包（tag=2） | 旧 / 单管线包（tag=1，含现有 CI 包） |
|---|---|---|
| **Deferred** | 读 secondary；global 读 `-Deferred.bin` | 读唯一一份；global 读普通文件（回退） |
| **Forward** | 读 primary；global 读普通文件 | 读唯一一份（若包是 Deferred cook 则管线不匹配，属包本身问题） |

### 6.3 key 标签规则：必须与「哪台机器」无关

这是本次改造中被推翻过一版的规则，值得单独记。

**初版方案**是「仅在与 ini 原生管线不同时才给 key 加标签」。它有个致命缺陷：P4 head 没有 `r.Mobile.ShadingPath`（原生 = `BaseEngine.ini:3805` 的 Deferred），而本地工作副本把它钉成 0（原生 = Forward）。原生因机器而异 ⇒ 同一份逻辑 shader map 在本地和 CI 得到**不同的 DDC key** ⇒ `-DDC=LocalAndShared` 的共享缓存跨机器无法命中，开发机 cook 会全量重编。

**最终规则**（全部不看 ini 原生管线）：

| 对象 | 规则 |
|---|---|
| DDC key / 资产唯一名 / `SimpleDebugInfo` / debug dump 目录（`MaterialShader.cpp:1499`、`MaterialShared.cpp:3823`） | **仅双管线 cook 时**给 Forward 一侧加 `MobFwdPath`；Deferred 保持上游 key；非双管线 cook 全部与上游一致 |
| PSO stable key（`MaterialShared.cpp:5468`、`ShaderCompiler.cpp:5212`） | Forward 一侧 FeatureLevel 加 `_MobFwd`；Deferred 保持上游 key（与既有 Deferred 时代的 PSO 录制兼容） |
| global shader 文件名 | 只有 `override==1` 那遍写 `-Deferred.bin` |

「原生管线」(`IsMobileDeferredShadingNative`) 从此**只用于两件事**：Forward 遍能否走异步收口、global 双遍的先后顺序（保证结束时 `GGlobalShaderMap` 是原生管线）。它不参与任何 key。

### 6.4 DDC 版本 bump 的代价

`FMaterialShaderMapId` 的 payload 加了字段，必须 bump `MATERIALSHADERMAP_DERIVEDDATA_VER`（`DevObjectVersion.cpp:137`）。这个 GUID 参与**所有平台**的材质 shader DDC key，所以合并后**所有人第一次打开编辑器都会重编一遍材质 shader**。这是必须付的一次性代价，上线前要提前通知全组。

### 6.5 类布局变化的影响

`FMaterial` 加了成员、`FMaterialShaderMapId` 加了字段 ⇒ **Live Coding 无法热更**。合并后必须**完全重启编辑器**，不能靠 Ctrl+Alt+F11。

## 七、PSO / Stable Key 采集的影响与修复

这一章是用户主动点名的方向，也是本次改造里**对既有录制数据伤害最大**的一块。

### 7.1 问题一：材质 stable key 只记了 primary

`FMaterial::SaveShaderStableKeys`（`MaterialShared.cpp:5468`）原本只读写 `GameThreadShaderMap`。改造后 primary 是 Forward、secondary 是 Deferred，若只记 primary，**Deferred 设备上录到的 PSO 在 `.shk` 里查不到对应 key** → 离线展开时整批被丢弃。

### 7.2 问题二：global shader 两遍 key 完全相同

global shader 的 stable key 里没有材质相关的区分字段。实测第一版双管线 cook 产生 **254 条** `Duplicate key in stable shader library`，每条都丢一个变体，样本全是 `FShadowProjectionPS` / `FShadowProjectionPS1` —— 这类 key 只有 `FeatureLevel`+`ShaderType`+`Perm` 三个维度，两遍逐字段一致。

修复：Forward 一侧在 `FeatureLevel` 上加 `_MobFwd`（`Material.cpp:2900`），Deferred 保持上游 −— 这样既有 Deferred 时代的 PSO 录制继续有效，新增的只多出一条带标签的记录。

### 7.3 一个重要的正面结论：OutputHash 没变

`GetMaterialHash` 混入 `MobileShadingPath` 后，`MaterialShaderMapHash` 会变，但它只用于 `FShaderKey` / `FShaderId` 这类**内存查找 key**（`UE5EA/Engine/Source/Runtime/RenderCore/Public/Shader.h:755`、`Shader.h:797`），**不进 OutputHash**。所以 `.shk` 里的 OutputHash、以及既有的 PSO 录制数据都不受影响 —— 新旧 `.shk` 混用是安全的。

### 7.4 实测验证

用引擎自带的 `ShaderPipelineCacheTools dump` 把 `.shk` 导出成 CSV 后统计（Global 库）：

| 指标 | 结果 |
|---|---|
| 条目总数 | 7,294 |
| Deferred（原样 `FeatureLevel=None`） | 4,508 |
| Forward（`FeatureLevel=MobFwd`） | 2,786 |
| 重复 key | **0** |
| `FSSRMobileLightingPS` | 仅在 Deferred 侧 |
| 两遍「同 shader+perm 但 bytecode 不同」的对数 | 254 —— 恰好等于旧版被丢弃的 Duplicate 数，现已全部保留 |

材质库同理：17,070,570 条 = Forward `ES3_1_MobFwd` 9,704,441 + Deferred `ES3_1` 7,366,129；改动前 Deferred 侧是 **0** 条。前 300 万行抽样重复 key 为 0。

### 7.5 运行时录制的兼容性

运行录制只记 OutputHash，而 OutputHash 未变。因此：旧 `.shk`（Forward 单管线）里有 Forward 的全部 key（无标签），展开新录制时能匹配上；旧 Deferred 单管线 `.shk` 里没有 Forward key，Forward 的 PSO 被丢弃 —— 正好符合「包里本来就没有 Forward」的事实。

## 八、实测数据

### 8.1 最终 cook

| 指标 | 结果 | 参考 |
|---|---|---|
| 材质 shader 总数 | **248,474**（Chunk0 137,440 / 101 29,904 / 102 74,802 / 1000 4,391 / Global 1,937） | 单管线 108,823 |
| shader 库总体积 | 2,818.8 MB | 单管线 865.5 MB |
| pak 合计 | 40.43 GB（8 个 chunk） | 上一版 40.42 GB |
| `Duplicate stable key` | **0** | 单管线时代 254 |
| 断言 / 崩溃 | 0 |  |
| `has no valid Deferred` | 0 |  |
| `Unknown inline shader map tag` | 0 |  |
| `resynchronizing` | 0 |  |
| `Deferred only`（Forward 编不过） | 67 | 需给美术清单 |
| 两管线都编不过 | 68 | 既有问题 |
| 缺失引用 | 140 | `/Game/Delete` 既有问题，不影响 cook 通过 |

cook 时长：冷 DDC 约 2.5–3 小时（含 Android target 编译）；热 DDC `-iterate` 约 1.5 小时。UAT 全程 `BUILD SUCCESSFUL`、`ExitCode=0`。

### 8.2 真机两条管线

同一台设备（MIX Fold 2，adb `8a97b61a`）、同一包、同一点位 `bugitgoto 50000 -38000 6000`、同朝向（`Pitch=0, Yaw=0`）：

| 项 | Forward（`-dpcvars=r.Mobile.ShadingPath=0`） | Deferred（设备 DP `0->1`） |
|---|---|---|
| 管线确认 | `ShadingPath = 0` | `ShadingPath = 1` |
| 画面 | 完整：角色 / 工业建筑 / 雪山 / 道路 / HUD / 小地图 | 完整，同构图 |
| Frame / Draws / Basepass | 27.6ms / 1396 / 1070 | 29.3ms / 1231 / 891 |
| Mem | 3.63 GB | 3.50 GB |
| `Unknown inline shader map tag` | 0 | 0 |
| `resynchronizing` | 0 | 0 |
| `Missing shader resource` | 0 | 0 |
| Fatal | 0 | 0 |
| 启动阶段材质回退 | 104 | 104 |

截图：`D:\GR_DevTest\S1Game\Saved\Screenshots\forward_now3.png` / `deferred_now3.png`。

两条管线的帧时间差异主要来自视角内图元数不同（`bugitgoto` 只改位置不改朝向），不是管线性能差。

### 8.3 一个中途被纠正的误判

过程中曾观察到「Forward 在 NeoRing 里材质回退 1035 次、Deferred 只有 104」，一度怀疑是管线差异。补测 Deferred **完整进 NeoRing** 后是 **1022** 次（`M_FlattenMaterial` 586 / `M_weapon_base` 195），与 Forward 的 1035（584 / 196）几乎完全一致 ⇒ **与管线无关**。

根因是项目既有的 `[MaterialLOD QualityLevel]` 定制（运行时按 `0..Num` 全档位建 resource，cook 只为 `QualityLevelsUsed` 出 map）叠加 cook 范围缺部分资产。早先只查了 Deferred 的**启动阶段** 104，样本不完整。

## 九、踩过的坑与误判纠正

只保留会重复踩、或已落进代码/配置的；纯过程性的弯路不记。

### 9.1 代码级坑（已进代码，需维护者知道）

| 坑 | 表现 | 结论 |
|---|---|---|
| `AppendKeyString` 的 context 平台是 `SP_NumPlatforms` 占位 | 在上面调 `IsMobilePlatform` → `IsValid(Platform)` 断言，cook 2.5 分钟必崩 | `RecordAndEmit` 里必须判 `!= SP_NumPlatforms`；资产唯一名由持有真实 Platform 的 `GetUniqueAssetName` 自己补 |
| `Record` 会污染 cook 元数据 | `Context.Record` 往 CompactBinary 加新字段，旧元数据 `LoadFromCompactBinary` 读不到会置 `bHasLoadError` | 改用只 `Emit`（Emitting 模式下 `Record` 本来就是 noop） |
| Deferred 遍不能异步 | `AssignCopy` 是深拷贝，异步回填的是原始 map，secondary 拿到空快照；且会被 Forward 遍 `CancelCompilation` | scope 内 `FinishCompilation()` 再 Move |
| layout 缓存 key 不含 ShadingPath | 第二遍复用第一遍的 perm 集合，产出错误 | `MaterialShader.cpp:2947` 混入 ShadingPath |
| `GetMaterialHash` 必须区分管线 | 两套 map 的 hash 输入完全相同 → `FShaderKey`/`FShaderId` 冲突，第二遍可能取到第一遍的 shader 对象 | `MaterialShader.cpp:1169` 混入 `MobileShadingPath` |
| `r.Mobile.DualPipelineCook` 若在 `WITH_EDITOR` 内 | 发布包在 ini 里读到同名项会建 `ECVF_Unregistered` 的 dummy 变量，污染启动日志 | CVar 定义移到 `#if WITH_EDITOR` 之外 |
| 开关若常驻 ini | 编辑器的 Android DDC key 也会被改，与 CI 单管线包不互通 | 改为 `-dpcvars` 只注入 cook 进程 + `IsRunningCommandlet()` 守卫 |

### 9.2 工具/流程坑

| 坑 | 表现 | 正确做法 |
|---|---|---|
| 强杀 adb 中断传输 | 设备卡 `offline`，`kill-server`/`reconnect` 全部无效，只能人工拔插 | 中止推送用 `TaskStop` 或 Ctrl-C，**绝不 kill adb.exe** |
| `rm -rf` 与启动打包写在同一个命令串 | 命令被转入后台后，后半段（启动打包）仍会执行 → 可能与手动启动的实例并存 | 删除与启动分两步；启动前 `Get-CimInstance` 核对 `BuildCookRun` 实例数 |
| `UnrealPak -List` 的 crypto key 路径 | 用 `Saved/Cooked/.../Crypto.json` 会因清空 Cooked 而消失，报 `Assertion failed: File != nullptr` | 固定用 `S1Game/Scripts/BuildScripts/UnrealPak/Crypto.json` |
| `git bash` 里 `cmd //c start "标题" cmd /k "bat"` 引号被剥 | 窗口不启动，静默无日志 | 改用 CRLF 的 launcher bat + `cmd /c` |
| 硬链接跟随 inode | 重建 cook 产物后，Deploy 目录里的 pak 仍指向旧文件（装机装的是旧包） | 重建产物后先删旧链接再 `mklink /H` |
| 新装包首次启动有合规弹窗 | 「年龄确认」「选择国家/地区 + 同意条款」两处会挡住画面 | 验证前先 `input tap` 点掉，坐标按实际截图换算 |
| `bugitgoto` 只改位置不改朝向 | 同一坐标两次落点朝向不同，画面不可比 | 对比时要么固定视角操作，要么说明差异；`bugitgo` 带旋转参数的写法在本项目未生效 |
| Live Coding 占用 DLL | UBT 直接拒绝构建（4 秒退出，`Engine.dll` 时间戳不变） | 先关 `UnrealEditor.exe`；改类布局后必须完全重启编辑器 |

## 十、已知问题与遗留

### 10.1 本次引入、需要后续处理

| 项 | 现状 | 建议 |
|---|---|---|
| 67 个材质 Forward 编不过（`Deferred only`） | 材质翻译阶段读 GBuffer `SceneTexture` 在 Forward 下报 `Errorf`（`HLSLMaterialTranslator.cpp:9021`）；这些材质在 Forward 设备上回退默认材质，**单管线 Forward 包亦然** | 导出 22 个材质清单给美术；属于内容侧问题 |
| 合并后编辑器首次打开全量重编材质 shader | `MATERIALSHADERMAP_DERIVEDDATA_VER` bump 的必然结果 | 合并前通知全组 |
| Live Coding 失效 | `FMaterial` / `FMaterialShaderMapId` 类布局变化 | 合并后要求完全重启编辑器 |
| 单材质改动现在产生 2 份 map 变化 | 热更补丁体积 ≈2× | 评估热更策略 |
| 若日后启用 IoStore | Zen 包头的 per-package `ShaderMapHashes` 会同时含两条管线，异步加载器会预载两套 shader code | 启用前需单独处理 |

### 10.2 既有问题（非本次引入）

| 项 | 数据 | 说明 |
|---|---|---|
| 材质回退默认材质 | 启动 104 次 / NeoRing 场景约 1022 次（两条管线一致） | `[MaterialLOD QualityLevel]` 定制 + cook 范围；`M_FlattenMaterial` 586、`M_weapon_base` 195 |
| 两管线都编不过的材质 | 68 条 | 既有 |
| 缺失引用 | 140 条 | `/Game/Delete` 废弃资产，不影响 cook 通过 |
| 偶发启动崩溃 | 3 次切换里崩 1 次（11:01），重启后不复现 | 崩前特征是同帧内大量 `AdaptivePreloadShaderThread` + `blocking wait for shader preload`；未能复现，**无证据指向本次改动，但未排除** |
| 地形 RVT 提示 | 两条管线都显示「运行时虚拟纹理…流送 mips 需要重建」 | RVT 未烘焙，内容问题 |

### 10.3 未完成

- 15 个引擎文件仍在 CL **1239741**，未提交。
- 用户本地 `WorldPartitionBlueprintLibrary.cpp/.h`（default change，新增 `GetActorDescsIncludingContainers` / `LoadRegion` / `UnloadRegion` / `UnloadAllRegions`）也在其中，需一并复核。
- CI 旧包兼容性（tag=1）只做了代码级验证，未做「新引擎 + 旧 CI pak」的实机对跑。

## 十一、操作手册

### 11.1 打开双管线 cook

`S1Game/Scripts/BuildS1Android.bat` 的 `AdditionalCookerOptions` 里带 `-dpcvars=r.Mobile.DualPipelineCook=1`：

```
-AdditionalCookerOptions=" -EXBUILDINGCONFIG=NoRush+NoBR+BasicMap+Development+NeoRing+DL \
  -NEVERCOOKDIR= -CookProcessCount=1 -DisablePlugins=UnrealMCP \
  -dpcvars=r.Mobile.DualPipelineCook=1" 
```

验证开关是否真的进了 cook 进程：查 `Cook-*.txt`（**不是** UAT 的 `Log.txt`）里有没有

```
LogDeviceProfileManager: Setting CommandLine Device Profile CVar: [[r.Mobile.DualPipelineCook:1]]
LogConfig: Set CVar [[r.Mobile.DualPipelineCook:1]]
```

出现 `Creating unregistered ...` 就说明 CVar 没注册（改名或放错了 `#if` 分支）。

### 11.2 判断出包是否真的双管线

| 对照项 | 单管线 | 双管线 |
|---|---|---|
| 材质 shader 总数 | ≈108,823 | ≈248,474 |
| `GlobalShaderCache-*-Deferred.bin` | 不存在 | 存在 |
| `ShaderArchive` 总体积 | ≈865 MB | ≈2,819 MB |

最快的办法：数 `Saved/Cooked/Android_ASTC/S1Game/Metadata/ShaderLibrarySource/` 下所有 `*.ushaderbytecode` 的 shader 总数（解析文件头的 `ShaderEntries` 计数）。

### 11.3 切管线跑验证

1. 默认管线由设备 DeviceProfile 决定：`Android_High` 推 `=1`（Deferred），`Android_Low`/`Android_Mid` 推 `=0`。
2. 临时切 Forward：`adb shell setprop debug.ue.commandline '-dpcvars=r.Mobile.ShadingPath=0'`（追加，用完要 `setprop debug.ue.commandline ''` 清空）。
3. 验证管线：`r.Mobile.ShadingPath` 探针回读，看 `value` 与 `LastSetBy`。
4. 进图统一用 `open /Game/Maps/NeoRing/NeoRing`（`open Neoring` 解析不了），再 `bugitgoto` 定点位。
5. 健康度检查：日志里 `Unknown inline shader map tag`、`resynchronizing`、`Missing shader resource`、`Fatal` 应为 0。

### 11.4 改了 cooked 格式之后

- 必须清空 `Saved/Cooked` 与 `Saved/StagedBuilds` 干净重 cook —— `-iterate` 会复用旧格式产物。
- 重建产物后，装机目录的 pak 硬链接要删掉重建。
- 确认 `UnrealEditor.exe` 已关闭（Live Coding 会挡住编译）。
