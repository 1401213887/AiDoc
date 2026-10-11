# UE5 无版本属性序列化 · UPROPERTY 改动影响面判定

以「草地 HISM→ISM 改造」为实例：一次代码改动到底会不会破坏已有资产的加载

## 结论

**本次改动不影响序列化。** 三条互相独立的依据，任一条成立即可定案：

- **唯一的 UPROPERTY 改动是 `transient`，永不落盘** —— 它在 unversioned 流里没有任何一位（`CoreUObject/Private/UObject/Property.cpp:1249-1259`）。
- **属性名 / 顺序 / 下标 / `ArrayDim` 都没变** —— unversioned 是按「属性数组下标」对位的，只改元素类型不动下标（`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:345`）。
- **`SchemaHash` 在包体里根本不存在** —— 它整体包在 `#if WITH_EDITORONLY_DATA` 内（`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:348-389`），且从不参与加载判定。

> 判定只需三步，顺序见「图 1」；三步之外还有一条**不能当判据**的东西（`SchemaHash`）。本次改动三步全落在「不影响」侧，唯一的危险出口（新增 / 删除 / 换序 / 改 `ArrayDim`）未被触及。

**图 1 · 判定顺序**（任一步判「否」即可提前收工）

| 步骤 | 判定 | 判「否」的结论 | 本次改动 |
|---|---|---|---|
| 起点 | 一个代码改动 | — | 草地 ISM 改造 |
| Q1 | 是反射标记的东西吗？（`UPROPERTY` / `UCLASS` / `USTRUCT`） | 不可能影响序列化，收工 | **命中**（1 个 UPROPERTY） |
| Q2 | 它会落盘吗？（`ShouldSerializeValue`） | 不进流，收工 | **否** —— transient 不落盘 |
| Q3 | 名字 / 顺序 / 数量 / `ArrayDim` 变了吗？ | 下标对位不变，收工 | **否** —— 只改元素类型 |
| 危险出口 | 新增 / 删除 / 换序 / 改 `ArrayDim` | 下标对位被破坏 ⇒ 旧内容错位读（症状：负数数组长度、越界对象下标） | 未触及 |

另：`schema hash` 是 editor-only 的诊断字段，不作为判据（见法则四）。

### 本次变更集枚举

先用 `p4 diff` 逐行过滤 `UPROPERTY|UCLASS|USTRUCT|UENUM|UFUNCTION`，把「够得着序列化」的改动挑出来 —— 整个变更集只有 **2 处**：

| 处 | 内容 | 性质 |
|---|---|---|
| `GrassInstancedStaticMeshComponent.h` | `UCLASS(ClassGroup=Foliage, MinimalAPI, Hidden)` + `GENERATED_UCLASS_BODY()` | **新增**一个 UCLASS；纯增量，旧内容烘焙时该类尚不存在，不可能被引用 |
| `LandscapeProxy.h` | `TArray<TObjectPtr<UInstancedStaticMeshComponent>> FoliageComponents` | **唯一被改的 UPROPERTY**，只改元素类型（`UPROPERTY(transient, duplicatetransient)` 那一行未动） |

其余改动**全部不属于反射范畴**（判据见「法则一」）：普通 C++ 成员（`FGrassComp::Foliage / PreviousFoliage`、`FAsyncGrassTask::Foliage`、`FAsyncGrassBuilder::bBuildTree`）、链接宏 `ENGINE_API`、运行时开关 `grass.useISM` 与 `WindowsEngine.ini`、以及方法签名与各处局部变量类型。

## 判定法则一：先划反射边界

**一句话结论：只有被反射标记（`UPROPERTY` / `UCLASS` / `USTRUCT` / `UENUM` / `UFUNCTION`）的东西才进序列化体系；其余一概无关。**

这一步是**过滤器**：如果整个改动里没有反射标记，后面三步都不必做。判定时最容易错的正是这里 —— 看到「改了类型」就紧张，其实改的是普通 C++ 成员。

| 改动形态 | 是否进序列化 | 判据 |
|---|---|---|
| `UPROPERTY` 的类型 / 名字 / 顺序 / `ArrayDim` | **是** | 进类布局与 schema |
| 新增 `UCLASS` / `USTRUCT` | **是（但是增量）** | 新类型；旧内容不会引用尚不存在的类型 |
| 普通 C++ 成员（非 `UPROPERTY`） | 否 | 不进反射表 |
| 非 USTRUCT 里的 `TWeakObjectPtr` / `TObjectPtr` 成员 | 否 | 类型不反射，等同普通成员 |
| `ENGINE_API` 等链接宏 | 否 | 只影响符号导出 |
| CVar、`.ini` 配置 | 否 | 不参与类布局 |
| 函数签名、循环变量、`Cast` 目标类型 | 否 | 不进反射表 |

> **怎么判断「是不是 USTRUCT」**：直接在类型定义前找 `USTRUCT(` / `UCLASS(`。本次三处 `TWeakObjectPtr` 改动所在的 `FGrassComp` / `FAsyncGrassTask` / `FAsyncGrassBuilder` 都没有反射标记，所以它们的成员类型怎么改都不进序列化。

## 判定法则二：再看它会不会落盘

**一句话结论：带 `transient` 的属性在烤包时不会被写进流，改它等于改了一段从不存在的位。**

unversioned 写侧对每个属性都会先问一句 `ShouldSerializeValue(Ar)`：

```cpp
const uint64 SkipFlags = CPF_Transient | CPF_DuplicateTransient | CPF_NonPIEDuplicateTransient
                     | CPF_NonTransactional | CPF_Deprecated | CPF_DevelopmentAssets | CPF_SkipSerialization;
if (!(PropertyFlags & SkipFlags)) { return true; }

// Skip properties marked Transient when persisting an object, unless we're saving an archetype
if ((PropertyFlags & CPF_Transient) && Ar.IsPersistent() && !Ar.IsSerializingDefaults())
{
    return false;
}
```

出处：`CoreUObject/Private/UObject/Property.cpp:1249-1259`。返回 false 的属性在写侧走 `Header.ExcludeProperty()`，**流里没有它的任何一位**（`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:1073-1085`），见图 2。

**图 2 · unversioned 写侧**

流程（箭头上的数据即图中标注）：

1. 遍历 schema（`PropertyLink` 链）→ 逐个属性交给判定
2. 判定 `ShouldSerializeValue(Ar)`
3. **是** → `Header.IncludeProperty` 写「含值」标记 + `Values` 流写该属性的值
4. **否** → `Header.ExcludeProperty`，**流里没有它的任何一位**
5. `FoliageComponents` 走第 4 条（`CPF_Transient` + 持久化归档 ⇒ 返回 false）

| 关键值 | 出处 |
|---|---|
| `SkipFlags = CPF_Transient \| CPF_DuplicateTransient \| CPF_NonPIEDuplicateTransient \| CPF_NonTransactional \| CPF_Deprecated \| CPF_DevelopmentAssets \| CPF_SkipSerialization` | `CoreUObject/Private/UObject/Property.cpp:1249` |
| `if ((PropertyFlags & CPF_Transient) && Ar.IsPersistent() && !Ar.IsSerializingDefaults()) return false;` | `CoreUObject/Private/UObject/Property.cpp:1256-1259` |
| 写侧 Include / Exclude 分叉 | `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:1073-1085` |

`FoliageComponents` 同时带 `transient` 与 `duplicatetransient`，两者都在 `SkipFlags` 里，随后又命中 `CPF_Transient + Ar.IsPersistent()` 这一条 ⇒ 持久化时返回 false。

> ⚠️ **边界（必须知道的例外）**：条件是 `&& !Ar.IsSerializingDefaults()` —— 保存 **archetype / CDO** 时 transient 属性**会**被序列化。但即便如此，把数组的元素类型从 `UHierarchicalInstancedStaticMeshComponent` 换成它的**父类** `UInstancedStaticMeshComponent`，数组的二进制布局（元素个数 + 对象引用）完全一致，读回来仍然兼容。

## 判定法则三：落盘的话，看名字 / 顺序 / 数量 / ArrayDim

**一句话结论：unversioned 不按「属性名」匹配，而按「属性在类里的位置」匹配 —— 位置没动就安全，动了就全错。**

源码把这件事写得很直白（注释原文）：

```cpp
// Serialization is based on indices into this property array
struct FUnversionedStructSchema
{
    uint32 Num;
    FUnversionedPropertySerializer Serializers[0];
};
```

出处：`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:343-352`；`Create()` 按 `PropertyLink` 链的顺序把属性逐个填进 `Serializers`（`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:362-376`）。

于是判定退化成一句很好记的话：**「属性名、顺序、数量、`ArrayDim` 有没有变」**。见图 3。

**图 3 · 下标对位**

```
cook 时 schema 数组(09-18) :  [下标0][下标1][下标2][…][下标 k][…][下标 n]
                                  |      |      |          ^
                                  |      |      |          | 同一下标
                                  v      v      v          v
load 时 schema 数组(10-10) :  [下标0][下标1][下标2][…][下标 k][…][下标 n]
                                          （k 格 = FoliageComponents）
```

- 只改**元素类型** ⇒ 下标 k 的格子位置不变 ⇒ 旧值读回来仍落在 k 格 ⇒ 安全
- **新增 / 删除 / 换序** ⇒ k 及之后的槽位整体偏移 ⇒ 值读进错误属性 ⇒ 出现「负数数组长度」「越界对象下标」这类症状
- 源码依据：`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:345`（注释原文 *"Serialization is based on indices into this property array"*）；`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:362-376`（`Create` 按 `PropertyLink` 顺序填 `Serializers`）

本次改动只改了元素类型（`UHierarchicalInstancedStaticMeshComponent` → `UInstancedStaticMeshComponent`），属性名、位置、数量都不变 ⇒ 下标对位不受影响。

> ❌ **这批改动的危险出口就在这里**：如果顺手「新增一个 UPROPERTY / 删掉一个 / 调整声明顺序 / 改 `ArrayDim`」，那么该属性**之后**的所有槽位都会整体偏移，旧内容会被读进错误的属性 —— 症状正是「负数数组长度」（`CoreUObject/Public/UObject/UnrealType.h:3984`）与「越界对象下标」（`CoreUObject/Public/UObject/Linker.h:165`）。**这是本次唯一需要盯住的边界。**

## 判定法则四：别把 SchemaHash 当判据

**一句话结论：`SchemaHash` 是编辑器专有的诊断字段，包体里连成员都没有 —— 它既不是判据，也不构成风险。**

整段相关代码都在 `#if WITH_EDITORONLY_DATA` 内：

```cpp
#if WITH_EDITORONLY_DATA
    FBlake3Hash SchemaHash;
#endif
...
#if WITH_EDITORONLY_DATA
            Property->AppendSchemaHash(HashBuilder, bSkipEditorOnly);
#endif
...
#if WITH_EDITORONLY_DATA
    Schema->SchemaHash = HashBuilder.Finalize();
#endif
```

出处：`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:348-389`；`FProperty::AppendSchemaHash` 的定义同样在该门控内（`CoreUObject/Private/UObject/Property.cpp:1316-1323`）。

全仓 `SchemaHash` 的唯一一处「比对」出现在一个 **editor-only 的 dump 诊断命令**里，而且是 `ensureMsgf`（不是断言，更不在加载路径上）：`CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:1136-1143`。

> **未确认项**：`FArrayProperty::AppendSchemaHash` 会把 `Inner` 链进 hash（`CoreUObject/Private/UObject/PropertyArray.cpp:1095-1102`）；而 `Inner` 是 `FObjectProperty`，**未覆写** `AppendSchemaHash`（`PropertyObject.cpp` 内检索无命中），走 `FProperty` 基类实现（只哈希 Name / `ArrayDim` / ID）。**元素类是否真的进入 hash，本次未进一步确认** —— 但该 hash 仅编辑器存在、且不参与加载判定，故不影响结论。

## 本次变更集逐项判定与包体崩溃归因

### 逐项判定

| 改动 | 法则一 反射 | 法则二 落盘 | 法则三 下标 | 结论 |
|---|---|---|---|---|
| `FoliageComponents` 元素类型放宽 | 命中（UPROPERTY） | 不落盘（transient） | 下标不变 | **不影响** |
| 新增 `UGrassISMComponent`（UCLASS） | 命中（增量） | — | 不影响既有类 | **不影响** |
| `FGrassComp` / `FAsyncGrassTask` 成员的 `TWeakObjectPtr` 类型 | 非 USTRUCT | 不参与 | 不参与 | **不影响** |
| `bBuildTree` / 方法签名 / 局部变量 / `ENGINE_API` | 非反射 | 不参与 | 不参与 | **不影响** |
| `grass.useISM` CVar + `WindowsEngine.ini` | 非反射 | 不参与 | 不参与 | **不影响** |

### 崩溃现场

现场：一个 exe 里带着本次改动（二进制可检出 `grass.useISM` / `UGrassISMComponent`），却配着 **三周前** 烘的内容在跑。

**图 4 · 崩溃现场**

| 项 | 值 |
|---|---|
| 内容（Paks） | `2026-09-18 07:46` / `08:01`（cook 于三周前） |
| 可执行文件 | `2026-10-10 18:57`，二进制里含 `grass.useISM` 与 `UGrassISMComponent` |
| 两者关系 | 相差三周（箭头标注「三周漂移」） |
| 触发点 | 预载 `NeoRing` 对局内容 → 异步加载 `ALandscapeProxy` 属性时断言 |
| 断言一 | `Assertion failed: Index.IsExport() && ExportMap.IsValidIndex(Index.ToExport())` @ `CoreUObject/Public/UObject/Linker.h:165` |
| 断言二 | `Assertion failed: Count>=0` @ `CoreUObject/Public/UObject/UnrealType.h:3984` |
| 同段落日志证据 | `LogProperty` 类型不匹配警告指向**材质类** · `/Game/__Mobile__/**` 变体包在 Win64 包里缺失 · `BP_AirVolume3` 残留编辑器专用子对象 |

三个事实把「本次改动」从因果链里摘出去：**不落盘**（法则二）、**下标未动**（法则三）、**hash 在包体里不存在**（法则四）。

于是错位只能来自**窗口内别处的布局漂移或内容缺失**，日志本身也指向这些方向：`LogProperty` 的类型不匹配警告点的是**材质类**（`MaterialInstanceConstant` / `PhysicalMaterial`、`Actor` / `MaterialInterface`），`/Game/__Mobile__/**` 变体包在 Win64 包里**不存在**，`/Game/BluePrints/AreaUpAirFlow/BP_AirVolume3` 残留了编辑器专用子对象（其创建与声明都在 `#if WITH_EDITORONLY_DATA` 内：`S1Game/Source/S1Framework/S1Game/Actor/AnyRegion/AnyRegionActor.cpp:35-38`、`S1Game/Source/S1Framework/S1Game/Actor/AnyRegion/AnyRegionActor.h:91-94`）。

> ⚠️ **仍未做实证。** 以上是源码级机制判定，不是实验。要钉死仍需二选一：**(A)** 重烘内容，使 paks 与 exe 同一 CL 再跑；**(B)** 在**不含本次改动**的干净工作区编同一个 exe，配**同一份旧内容**跑。若 (A) 仍崩 ⇒ 真代码问题；若 (B) 好转 ⇒ 是本次改动。

## 可复用 Checklist

1. `p4 diff` 过滤 `UPROPERTY|UCLASS|USTRUCT|UENUM|UFUNCTION`，**先把够得着序列化的改动挑出来**；一个都没有 ⇒ 结案。
2. 对每个被改的 UPROPERTY：查它**是否落盘**（`ShouldSerializeValue`；`transient` / `duplicatetransient` / `NonTransactional` / `SkipSerialization` 都是不落盘信号）。不落盘 ⇒ 结案。
3. 落盘的话：查**属性名 / 顺序 / 数量 / `ArrayDim`** 有没有变。只改类型、改默认值 ⇒ 安全；增删换序 ⇒ 高危。
4. 别把 `SchemaHash` 当作判定依据 —— 它是 editor-only 的诊断字段，包体里不存在。
5. 分不清「是不是 USTRUCT」时，直接看类型定义前有没有 `USTRUCT(` / `UCLASS(`；没有就不进序列化。
6. 判断「旧内容能不能配新代码」时，先看**内容与 exe 的时间戳 / CL 是否同源** —— 不同源本身就是一票否决，不必先怀疑自己的改动。

## 参考

- `CoreUObject/Public/UObject/Linker.h:165` —— `Exp()` 的 `check(Index.IsExport() && ExportMap.IsValidIndex(...))`
- `CoreUObject/Public/UObject/UnrealType.h:3984` —— `FScriptArrayHelper::EmptyAndAddValues` 的 `check(Count>=0)`
- `CoreUObject/Private/UObject/LinkerLoad.cpp:4447-4467` —— `FLinkerLoad::ResolveResource()`（函数定义在 4447，崩点是 4461 的 `return Exp(Index).Object;`）
- `CoreUObject/Private/UObject/Class.cpp:1447-1454` —— `UseUnversionedPropertySerialization()` 两条分支
- `CoreUObject/Private/UObject/Property.cpp:1249-1259` —— `FProperty::ShouldSerializeValue`
- `CoreUObject/Private/UObject/Property.cpp:1316-1323` —— `FProperty::AppendSchemaHash`（editor-only）
- `CoreUObject/Private/UObject/PropertyArray.cpp:1095-1102` —— `FArrayProperty::AppendSchemaHash` 链 `Inner`
- `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:343-352` —— schema 结构（下标对位注释原文）
- `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:354-398` —— `Create()`
- `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:978-980` —— `SkipEditorOnlyFields`
- `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:1073-1085` —— 写侧 Include / Exclude
- `CoreUObject/Private/Serialization/UnversionedPropertySerialization.cpp:1136-1143` —— 唯一的 hash 比对（editor-only dump）
