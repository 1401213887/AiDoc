# UE5EA 流送收集触发 FlushAsyncLoading 卡顿 —— 材质 soft 贴图同步加载根因与修复

> 现象：真机打包版偶发 GameThread 卡顿（hitch）。UE Insights 调用栈显示纹理流送系统每帧的 `ProcessPendingComponents` 在收集组件贴图信息时，对一张 VFX 贴图 `T_EFX_Shape_WZC_002_D` 执行了同步 `StaticLoadObject → LoadPackageInternal → FlushAsyncLoading`，把整条异步加载队列在 GameThread 上一次性抽干。核心线索：材质 `TSoftObjectPtr<UTexture>` 默认贴图在 GameThread 上被同步 `LoadSynchronous`。

---

## 一、问题定位流程

### 精确调用栈（真机打包，Insights 抓取）
```
Engine Tick
└ STAT_UGameEngine_Tick_IStreamingManager
  └ FStreamingManagerCollection::UpdateResourceStreaming
    └ FRenderAssetStreamingManager::IncrementalUpdate
      └ DynamicComponentManager::IncrementalUpdate
        └ FDynamicRenderAssetInstanceManager::IncrementalUpdate2
          └ ProcessPendingComponents
            └ FRenderAssetInstanceState::AddComponentIgnoreBoundsInternal
              └ UPrimitiveComponent::GetStreamingRenderAssetInfoWithNULLRemoval
                └ StaticLoadObjectInternal
                  └ LoadObject  (/Game/Arts/Effect/NewVfx/Hero/Hero15/Base/Texture/T_EFX_Shape_WZC_002_D)
                    └ LoadPackageInternal
                      └ FlushAsyncLoading → Flush Async Loading GT → TickAsyncLoading GT
```

### 已逐帧确认的调用链闭合
```
GetStreamingRenderAssetInfo
└ FStreamingTextureLevelContext::ProcessMaterial      (TextureStreamingBuild.cpp:653，非预构建分支 :684 调 GetUsedTextures)
  └ UMaterial(Instance)::GetUsedTextures
    └ FMaterialTextureParameterInfo::GetGameThreadTextureValue  (MaterialUniformExpressions.cpp:2444)
      └ 第一分支 MaterialInterface->GetTextureParameterValue    (MaterialInterface.cpp:977)
        └ UMaterialInstance::GetParameterValue                 (MaterialInstance.cpp:1251，:1259 先查母材质 CachedExpressionData)
          └ FMaterialCachedExpressionData::GetParameterValueByIndex  (MaterialCachedData.cpp:744)
            └ 【真凶】TextureValues[ParameterIndex].TryLoadSynchronous()  (MaterialCachedData.cpp:812)
```

### 关键确认点（"确认了什么"）
- **真机成立、与 late-resolve 宏无关**：真凶是 `TSoftObjectPtr` 的同步加载，不是 `TObjectPtr` late-resolve。`StaticLoadObjectInternal` 这一帧是显式 `StaticLoadObject`（soft ptr 同步加载的路径），并非 `ResolveObjectHandle`。cooked 打包下 `WITH_EDITORONLY_DATA=0` → `UE_WITH_OBJECT_HANDLE_LATE_RESOLVE=0`（ObjectHandleDefines.h:10；已确认引擎与 `S1Game.Target.cs`/`S1GameClient.Target.cs` 均未强开），硬引用退化裸指针不 load —— 故 late-resolve 方向可排除。
- **"母材质直接赋默认贴图"是触发前提**：`GetGameThreadTextureValue`（MaterialUniformExpressions.cpp:2446）的分岔 `if (ParameterInfo.Name.IsNone() || !GetTextureParameterValue(...))`——只有**命名 Texture 参数（`FMaterialUniformExpressionTextureParameter`，HLSLMaterialTranslator.cpp:9329）且母材质给了非空默认贴图**才走 soft 加载分支；普通 Texture Sample（`FMaterialUniformExpressionTexture`，:9294，无参数名）短路走 `GetIndexedTexture` 硬引用 `ReferencedTextures`，不 load。
- **查询顺序**：`UMaterialInstance::GetParameterValue`（MaterialInstance.cpp:1256-1287）先查母材质 `CachedExpressionData` 的 soft `TextureValues`（:1259，即触发同步加载），后查 MI override（:1275）。因此只要母材质该参数有默认贴图，流送查询即先同步 load 母材质那张 soft 默认贴图，**哪怕 MI 已 override 也照 load**（MI override 只覆盖返回值）。
- **MI override 用硬引用**：`FTextureParameterValue::ParameterValue` 是 `TObjectPtr<UTexture>`（MaterialInstance.h:240），查询不 load。

---

## 二、根因分析

### 真凶行
```cpp
// Engine/Source/Runtime/Engine/Private/Materials/MaterialCachedData.cpp:812
// FMaterialCachedExpressionData::GetParameterValueByIndex, case EMaterialParameterType::Texture
OutResult.Value = TextureValues[ParameterIndex].TryLoadSynchronous();
```
- `TextureValues`：`TArray<TSoftObjectPtr<UTexture>>`（MaterialCachedData.h:303），保存母材质各 texture 参数的默认贴图。cook 期 `AddParameter`（MaterialCachedData.cpp:197-201）把 texture 参数默认值同时写入此 soft 数组 **和** 硬引用 `ReferencedTextures`。
- 流送 `IncrementalUpdate` 跑在 **GameThread**，命中 `TryLoadSynchronous` 的 GameThread 分支 → `LoadSynchronous()` 同步加载 → 若目标 soft 贴图所在包仍在异步途中，退化为同步 `StaticLoadObject` → `FlushAsyncLoading` 抽干异步队列 → GT hitch。

### 责任归属（P4 溯源结论，重要）
用 `p4 annotate` / `filelog` / `print` 追至最初导入的 UE5EA 原生版本（`//GR/trunk/.../MaterialCachedData.cpp#2`、`SoftObjectPtr.h#2`）核实：

| 要素 | 归属 | 依据 |
|---|---|---|
| `TextureValues` 用 `TSoftObjectPtr<UTexture>` | **UE 原生** | 最初导入版 `MaterialCachedData.h#2` 即为 soft |
| GameThread 上**同步加载**该 soft 默认贴图 | **UE 原生** | 原生 texture case 原样即 `TextureValues[i].LoadSynchronous()`（现注释保留于 :805）；TextureCollection/RVT/SVT 至今仍是原生 `LoadSynchronous()` |
| `TryLoadSynchronous()`（GR CL 626493, shiyu） | **GR 改动，仅动非 GameThread** | `#pragma region shiyu` `// for fix streaming system parallel call`；实现见下 |
| `bFlushLoading` dev 探针（GR） | **GR dev-only 报警，非元凶** | 见第三节 |

`TryLoadSynchronous` 实现（SoftObjectPtr.h:453，GR 定制）：
```cpp
T* TryLoadSynchronous() const {
    if (IsInGameThread()) { return LoadSynchronous(); }  // GameThread：照抄原生同步加载
    return Get();                                        // 非 GameThread：改成只取不加载，规避并行崩溃
}
```

**结论：GameThread 同步加载卡顿是 UE 原生行为。** shiyu 的 `TryLoadSynchronous` 只把非 GameThread 的并行调用退成 `Get()` 以规避 async loading 线程崩溃，**GameThread 分支原封照抄原生同步加载**，对流送这次 GT hitch 无影响（整个回退到原生 `LoadSynchronous()` 卡顿依旧、且非 GameThread 又会崩）。

---

## 三、详细技术原理

### `bFlushLoading` 探针（806-811）与本卡顿无关
```cpp
// MaterialCachedData.cpp:806-811（真机 Test/Shipping 不编译）
#if UE_BUILD_DEVELOPMENT
    if (!TextureValues[ParameterIndex].IsValid())   // IsValid 只读内存状态，不加载
    {
        OutResult.bFlushLoading = true;             // 只是打标记
    }
#endif
```
- `TSoftObjectPtr::IsValid()` 纯内存状态查询，**不加载、不 Flush**。
- `bFlushLoading` 全 depot 仅三处足迹：定义（MaterialTypes.h:487，`#if UE_BUILD_DEVELOPMENT`）、写（此处）、读（**唯一消费点** MaterialInstance.cpp:2296，仅 `ensureMsgf` 断言告警）。它是"报警器"不是"放火者"，且真机打包不编译。
- 强制 Flush 发生在**下一行 812** 的 `TryLoadSynchronous`，与 806-811 无关。

### 为何修复可安全"只取不加载"
- **RT 渲染有硬引用兜底**：`FMaterialRenderContext::GetTextureParameterValue`（MaterialUniformExpressions.cpp:237-243）未 override 时回退 `GetIndexedTexture` → `ReferencedTextures`（硬）；RT 从不调 `TryLoadSynchronous`。soft 未加载渲染也不为 null。
- **贴图进内存靠硬引用序列化**：`ReferencedTextures`（TObjectPtr UPROPERTY）随 `CachedExpressionData` 经 `SerializeTaggedProperties`（MaterialInterface.cpp:228）作为 import 随材质包加载。流送这条 soft 同步加载对"贴图是否在内存"是冗余的。
- **旁证**：`TextureStreamingBuild.cpp:663-680` 在 `!WITH_EDITOR` + 有预构建 streaming 数据时直接用 `LevelStreamingTextures`、根本不调 `GetUsedTextures`、不触发 soft load，贴图仍在内存。

---

## 四、修复方案

### 方案 A：引擎全局修复（推荐，覆盖所有母材质）
用 `thread_local bool + RAII 作用域 guard`（`FStreamingTextureCollectScope`，声明放 `MaterialCachedData.h`，实现放 `.cpp`）精确圈住流送收集路径：
1. 在 `TextureStreamingBuild.cpp` 的 `ProcessMaterial` 非预构建分支，用 guard 包住 :684 的 `GetUsedTextures(...)`。
2. 在 `MaterialCachedData.cpp:812` texture case：scope 激活时用 `TextureValues[ParameterIndex].Get()`（只取已在内存对象，不加载）替代 `TryLoadSynchronous()`；默认（未激活）行为零变化。
- 逻辑与 shiyu 已有的"按线程上下文区分加载策略"一致（只是再加一个"流送探测上下文"维度）。
- 严格隔离：仅流送收集期间生效，RT/编辑器/prestream 路径不变。
- 所有改动用 `#pragma region Engine ZXB` 包裹；改文件前先 `p4 edit`（client `DJANGOZHAN-PCFW_GR_main`）。

### 方案 B：美术局部规避（适合热点集中）
对重灾母材质，将问题 texture 参数默认贴图留空、改由 MI override（硬引用 `TObjectPtr`，不 load）提供；或改成普通 Texture Sample 非参数化（走硬引用 `ReferencedTextures`）。零引擎风险。

### 方向决策依据
先做波及面摸底：静态扫描项目母材质中"含非空默认贴图 texture 参数"的占比。占比高（普遍模式）→ 方案 A；集中少数材质 → 方案 B。

---

## 五、快速排查 Checklist
- [ ] Insights 栈是否为 `...GetStreamingRenderAssetInfoWithNULLRemoval → StaticLoadObjectInternal → LoadObject(贴图) → FlushAsyncLoading`。
- [ ] 加载目标是否为**贴图**且是某母材质的 **texture 参数默认值**（而非普通 Texture Sample）。
- [ ] 确认 `MaterialCachedData.cpp:812` `TryLoadSynchronous`（GameThread → `LoadSynchronous`）为触发点。
- [ ] 区分：`806-811 bFlushLoading` 是 dev-only 探针（真机不编译、不加载），非元凶。
- [ ] 判定与 `UE_WITH_OBJECT_HANDLE_LATE_RESOLVE` / 编辑器-真机差异**无关**（soft ptr 在哪都 load）。
- [ ] 确认渲染取硬引用 `ReferencedTextures` 兜底 → 流送侧"只取不加载"安全。

---

## 六、相关代码位置（本工作区 c:/GR_main/UE5EA）
- 真凶：`Engine/Source/Runtime/Engine/Private/Materials/MaterialCachedData.cpp:812`（取值）、`:197-201`（cook 期填充）、`:806-811`（dev 探针）
- soft 声明：`Engine/Source/Runtime/Engine/Public/MaterialCachedData.h:303`（TextureValues）、`:322`（ReferencedTextures 硬引用）
- `TryLoadSynchronous`：`Engine/Source/Runtime/CoreUObject/Public/UObject/SoftObjectPtr.h:453`（GR CL 626493, shiyu）
- 分岔与两类表达式：`MaterialUniformExpressions.cpp:2444`；`HLSLMaterialTranslator.cpp:9294`(Texture) / `:9329`(TextureParameter)
- 流送入口：`Engine/Source/Runtime/Engine/Private/Streaming/TextureStreamingBuild.cpp:653/684`
- MI 查询顺序：`Engine/Source/Runtime/Engine/Private/Materials/MaterialInstance.cpp:1251-1287`
- late-resolve 宏（已排除）：`Engine/Source/Runtime/CoreUObject/Public/UObject/ObjectHandleDefines.h:10`、`ObjectHandle.h:37/114`
- `bFlushLoading` 消费点：`MaterialInstance.cpp:2296`；定义 `MaterialTypes.h:487`
