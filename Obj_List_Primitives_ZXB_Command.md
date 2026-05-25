# obj list primitives 调试命令改动总结（ZXB）

> 文档对应 P4 stream：`//GR/MergeTest`
> 涉及 client：`DJANGOZHAN-PCFW_GR_MergeTest`（root: `D:\GR_MergeTest`）[[memory:tb9s72p6]]
> 所有改动均使用 `#pragma region Engine ZXB` / `#pragma endregion` 包裹 [[memory:2e1qudee]]，不嵌套，方便后续 diff / 整理。

---

## 1. 背景与目标

`SceneVisibility_FrustumCull` 在大场景下是渲染线程热点。要诊断"场景里到底有多少 PrimitiveComponent、它们是谁、Bounds 是否合法、其中又有多少真正进入 `FScene::Primitives` 参与每帧 FrustumCull"，引擎自带的 `obj list` 只按 UObject 维度做内存/数量聚合，**无法直接反映渲染图元的分布**。

本次改动在 `obj list` 命令上扩展一个**专用子命令**：

```
obj list primitives
```

它将场景中所有 `UPrimitiveComponent` 的诊断信息**导出到 `Saved/Profiling/PrimitiveComponentList_<ID>.dumpobj`**，并附带一份可直接对照 `SceneVisibility_FrustumCull` 工作量的按类汇总表。

---

## 2. 改动一览（共 3 处 ZXB region，**仅 1 个文件**）

| # | 文件 | 行号（当前） | 类型 | 简述 |
|---|---|---|---|---|
| C1 | [UnrealEngine.cpp](./UnrealEngine.cpp) | 184–190 | include 段 | 新增 5 个头文件 |
| C2 | [UnrealEngine.cpp](./UnrealEngine.cpp) | 9149–9371 | 工具命名空间 | 新增 `namespace UE_ObjListPrimitives_ZXB` 与 `HandleListPrimitivesCommand_Internal` |
| C3 | [UnrealEngine.cpp](./UnrealEngine.cpp) | 9622–9629 | 命令分发 | 在 `UEngine::HandleObjCommand` 的 `LIST` 分支首端接入 `PRIMITIVES` 子命令 |

---

## 3. 改动详情

### C1 · 头文件 include（约 L184）

```cpp
#include "Components/BrushComponent.h"
#pragma region Engine ZXB
#include "Components/PrimitiveComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Components/InstancedStaticMeshComponent.h"
#include "Components/HierarchicalInstancedStaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#pragma endregion
#include "GameFramework/GameUserSettings.h"
```

### C2 · 工具命名空间 `UE_ObjListPrimitives_ZXB`（约 L9149–L9371）

实现要点（与历史多次迭代后的最终态对齐）：

1. **输出落地**：`Saved/Profiling/PrimitiveComponentList_<ID>.dumpobj`
   - 使用 `FPaths::ProfilingDir()` + `CreateProfileFilename` + `IFileManager::CreateDebugFileWriter`，与原 `obj list` 写法一致。
   - `static int32 PrimitiveListFileID = 0; ++PrimitiveListFileID;` 同进程多次执行不会覆盖，文件名后缀 `_1`、`_2`…
2. **文件头**：写入命令回显 `Obj List Primitives: <Cmd>` 与字段说明注释（`# Fields: ...`）。
3. **抑制误报**：`FSlowHeartBeatScope` + `FDisableHitchDetectorScope` 抑制心跳与卡顿检测。
4. **遍历**：`FThreadSafeObjectIterator(UPrimitiveComponent::StaticClass())`。
5. **跳过条件**（顺序经过优化，先廉价 flag 短路，再调有效性检查）：
   - `IsTemplate(RF_ClassDefaultObject)` —— CDO
   - `HasAnyFlags(RF_BeginDestroyed)` —— 正在销毁
   - `!IsValidChecked(Obj)` —— 无效/PendingKill/Garbage
   - `Cast<UPrimitiveComponent>` 失败的也 skip
6. **逐项输出**（单行 `Key=Value` 格式，方便 grep/解析）：
   ```
   Class=<X> Object=<Path> Origin=(x,y,z) Extent=(x,y,z) Radius=<r>
        [ Mesh=<P> | InstanceMesh=<P> InstanceCount=<N> ]
        Registered=<0/1> ShouldRender=<0/1> InScene=<0/1>
        [INVALID_BOUNDS]?
   ```
7. **类型分支**（**ISM/HISM 必须先于 SMC 判断**，因为 ISM 继承自 SMC）：
   - `UInstancedStaticMeshComponent` → 追加 `InstanceMesh=<Path> InstanceCount=<N>`
   - `UStaticMeshComponent` → 追加 `Mesh=<Path>`
   - 其它类型不追加资源字段
   - `GetStaticMesh()` 为空时输出 `Mesh=None` / `InstanceMesh=None`
8. **与 FrustumCull 工作量对齐的状态字段**（核心增值）：
   - `Registered` = `IsRegistered()`
   - `ShouldRender` = `ShouldRender()`
   - `InScene` = `(PrimComp->SceneProxy != nullptr)`（**直接读 public 字段，避免 `GetSceneProxy()` 内部 `ensure()`**）
9. **非法 Bounds**：`B.ContainsNaN()` 命中追加 `[INVALID_BOUNDS]`（UE 原生 API，等价于 `IsFinite` 检查）。
10. **按类统计**：每条记录同步累加 `ClassStatMap[ClassName].{Alive, InScene}`。
11. **Summary 表（三列：Class | Alive | InScene）**：
    ```
    Class                                                              Alive    InScene
    HierarchicalInstancedStaticMeshComponent                             156        156
    StaticMeshComponent                                                  98          76
    BoxComponent                                                         42           0
    SkeletalMeshComponent                                                17          17
    ```
    排序键：`InScene 降序 → Alive 降序 → Class 升序`，让影响 `FrustumCull` 最大的类排在最上方。
12. **Total 段**：
    ```
    Total Alive UPrimitiveComponent             : 1234
    Total In-Scene Primitives (FrustumCull N)   : 987
    Total Instances        (ISM+HISM, all)      : 87654
    Total Instances        (ISM+HISM, in-scene) : 87654
    ```
13. **资源清理**：`NewFileAr.TearDown(); delete FileAr; FileAr = nullptr;`
14. **控制台回执**：
    ```
    Obj List Primitives: dumped 1234 alive (987 in-scene) primitive components to <path>
    ```

### C3 · 命令分发接入（约 L9622）

在 `UEngine::HandleObjCommand` 的 `LIST` 分支首端、`FORGET`/`REMEMBER` 之前插入：

```cpp
else if( FParse::Command(&Cmd, TEXT("LIST")) )
{
    static TSet<FObjectKey> ForgottenObjects;

#pragma region Engine ZXB
    // "obj list primitives" 子命令：导出所有 PrimitiveComponent 的 Class/Object/Bounds，
    // 并对 StaticMeshComponent 与 ISM/HISM 附带 Mesh 路径与实例数量，最后输出按类汇总表
    if (FParse::Command(&Cmd, TEXT("PRIMITIVES")))
    {
        return UE_ObjListPrimitives_ZXB::HandleListPrimitivesCommand_Internal(Cmd, Ar);
    }
#pragma endregion

    // "obj list forget" ...
    if (FParse::Command(&Cmd, TEXT("FORGET")))
    ...
}
```

要点：
- `FParse::Command` 命中后**会消耗掉 `PRIMITIVES` token**，剩余参数透传给内部函数（前向兼容，可后续支持 `CSV`、过滤器等）。
- 命中即 `return true`，**完全不走原 LIST 流程**。
- 未命中保持原 LIST 行为不变。

---

## 4. 命令使用

### 4.1 命令格式

```
obj list primitives
```

`obj list` 是 `UEngine::HandleObjCommand` 注册的 exec 命令；该命令在 **Editor / Game / Shipping 三种构建配置下均可用**（与原 `obj list` 可见性一致）。

### 4.2 调用入口

| 入口 | 操作 |
|---|---|
| 编辑器 Output Log | `Window → Developer Tools → Output Log` 输入 `obj list primitives` |
| 游戏运行时控制台 | 按 `~` 输入 `obj list primitives` |
| 启动参数 | `YourGame.exe -ExecCmds="obj list primitives"` |
| 蓝图 | `Execute Console Command → obj list primitives` |
| C++ | `GEngine->Exec(GetWorld(), TEXT("obj list primitives"));` |

### 4.3 输出位置

`<Project>/Saved/Profiling/PrimitiveComponentList_<ID>_<时间戳>.dumpobj`
- 文本文件，可用任意编辑器打开。
- 同进程多次执行，`<ID>` 自增，互不覆盖。

---

## 5. 输出文件结构

### 5.1 头部
```
Obj List Primitives: <Cmd>

# Fields: Class | Object | Origin | Extent | Radius | [Mesh|InstanceMesh+InstanceCount] | Registered | ShouldRender | InScene
```

### 5.2 逐项明细（单行示例）

```
Class=StaticMeshComponent Object=/Game/Maps/UEDPIE_0_Main.Main:PersistentLevel.SM_Cube_1.StaticMeshComponent0 Origin=(100.00,200.00,50.00) Extent=(50.00,50.00,50.00) Radius=86.60 Mesh=/Engine/BasicShapes/Cube.Cube Registered=1 ShouldRender=1 InScene=1
Class=HierarchicalInstancedStaticMeshComponent Object=/Game/.../HISM_Tree Origin=(0,0,0) Extent=(5000,5000,300) Radius=7080.00 InstanceMesh=/Game/Foliage/SM_Tree.SM_Tree InstanceCount=842 Registered=1 ShouldRender=1 InScene=1
Class=BoxComponent Object=/Game/.../Trigger_1.CollisionBox Origin=(500,0,0) Extent=(100,100,100) Radius=173.21 Registered=1 ShouldRender=0 InScene=0
```

### 5.3 Summary 段

```
===== Summary By Class =====
# 'Alive' = GameThread 上活着的 UPrimitiveComponent 数量（与 obj list primitives 行数一致）
# 'InScene' = SceneProxy 非空，已进入 FScene::Primitives，会被每帧 SceneVisibility_FrustumCull 处理
Class                                                              Alive    InScene
HierarchicalInstancedStaticMeshComponent                             156        156
StaticMeshComponent                                                  98          76
BoxComponent                                                         42           0
...
```

### 5.4 Total 段

```
Total Alive UPrimitiveComponent             : 1234
Total In-Scene Primitives (FrustumCull N)   : 987
Total Instances        (ISM+HISM, all)      : 87654
Total Instances        (ISM+HISM, in-scene) : 87654
```

> `Total In-Scene Primitives` ≈ `FScene::Primitives.Num()` 的 **GameThread 端快照上界**，可直接对应 `SceneVisibility_FrustumCull` 真正遍历的 N。

---

## 6. 与 `SceneVisibility_FrustumCull` 的对照解读

执行命令后，重点看：

| 观察项 | 含义 | 行动建议 |
|---|---|---|
| `Total In-Scene Primitives` | 大致等于 `Scene.Primitives.Num()`，FrustumCull 真正的 N | 这是 FrustumCull 的核心分母 |
| `Total Alive − Total In-Scene` | 不影响 FrustumCull 的"虚高" | 通常是 BoxComponent / Trigger / 编辑器 Component，分析时可以放心忽略 |
| Summary 中 `Alive >> InScene` 的类 | 大量隐藏 Trigger / 编辑器组件 | **不会增加 FrustumCull 耗时**，不必专门优化 |
| Summary 中 `Alive ≈ InScene` 且排前列的类 | 真正影响 FrustumCull 的大头 | **优先优化对象**：典型如把大量散落 SMC 合并为 HISM |
| `Total Instances` 巨大但 In-Scene 组件数小 | ISM/HISM 组件少、实例多 | 实例剔除发生在 InstanceCulling/Nanite 阶段，**不会等比放大 FrustumCull 耗时** |

---

## 7. 行为兼容性

| 场景 | 行为 |
|---|---|
| `obj list`（无子参数） | 走原默认聚合流程，不受影响 |
| `obj list forget` / `remember` | 走原 FORGET/REMEMBER 流程，不受影响 |
| `obj list <ClassName>` | 走原默认 LIST 流程，不受影响 |
| `obj list2` / `obj memsub` 等其它子命令 | 完全未触动 |
| Shipping 构建 | 与原 `obj list` 一样可用（如需禁用，可包裹 `#if !UE_BUILD_SHIPPING`） |
| 与项目对象池兼容 | `IsValidChecked` 内部已处理 `RF_MirroredInObjectPool` / `RF_MirroredGarbage` / `EInternalObjectFlags::Recycled` |
| GC 行为 | 仅只读遍历，不修改任何 UObject 状态 |

---

## 8. 设计决策与考量

| 决策点 | 选择 | 理由 |
|---|---|---|
| 函数归属 | **匿名命名空间 + 静态函数**（不挂到 `UEngine` 成员） | 避免改动 `Engine.h` 头文件，最小化重新编译影响面 |
| 命名空间名 | `UE_ObjListPrimitives_ZXB` | 含 `ZXB` 标记便于与原生引擎符号区分 |
| 类型识别顺序 | **先 ISM 后 SMC** | HISM/ISM 继承自 SMC，否则会被误归为 SMC |
| HISM 类名 | 保持真实派生类名 | 让 `HierarchicalInstancedStaticMeshComponent` 在 Summary 中独立成行，不被合并到 ISM |
| 非法 Bounds 检测 | `B.ContainsNaN()` | UE 内置 API，等价 `IsFinite`，比自实现更稳 |
| `InScene` 取值 | `PrimComp->SceneProxy != nullptr` | 直接读 public 字段，避免 `GetSceneProxy()` 的 `ensure()` 误报 |
| 输出格式 | `Key=Value` + 空格分隔 | 一行一个组件，便于脚本 grep/sed/awk 处理 |
| 排序策略 | `InScene 降序 → Alive 降序 → Class 升序` | 让"真正影响 FrustumCull"的类排在最上方，第一眼就能看到优化重点 |
| 文件名前缀 | `PrimitiveComponentList_<ID>` | 与原 `ObjectStatistics_<ID>` 区分，避免和默认 `obj list` 文件混淆 |

---

## 9. 验证 / 回滚

### 9.1 验证

- **编译**：`UnrealEngine.cpp` lint 0 报错，引擎可正常编译。
- **行为**：
  - `obj list` 默认行为不变。
  - `obj list primitives` 输出文件含三段（明细 / Summary / Total），与上文示例格式一致。
  - 控制台回执含 `dumped X alive (Y in-scene)`。

### 9.2 回滚

所有改动均被 `#pragma region Engine ZXB` / `#pragma endregion` 包裹，回滚策略：

```bash
p4 -c DJANGOZHAN-PCFW_GR_MergeTest revert D:/GR_MergeTest/UE5EA/Engine/Source/Runtime/Engine/Private/UnrealEngine.cpp
```

或者手工删除三段 region：
- `UnrealEngine.cpp` L184–190（include 段）
- `UnrealEngine.cpp` L9149–L9371（命名空间 `UE_ObjListPrimitives_ZXB`）
- `UnrealEngine.cpp` L9622–L9629（LIST 分支 `PRIMITIVES` 分发）

---

## 10. 后续可选增强（未实施，仅记录）

| 方向 | 难度 | 价值 | 备注 |
|---|---|---|---|
| 支持 `obj list primitives CSV` | 低 | 中 | 与原 `obj list` 的 CSV 选项语义对齐，便于 Excel 直接打开 |
| 支持过滤器 `OUTER=` / `PACKAGE=` / `CLASS=` | 低 | 中 | 复用原 LIST 的过滤参数 |
| 增加 RT 端 `ENQUEUE_RENDER_COMMAND` 同步 `Scene.Primitives.Num()` | 中 | 高 | 让 `Total In-Scene Primitives` 与 `Scene.Primitives.Num()` 精确对齐 |
| 输出每个 Primitive 的 `bIsNaniteMesh` / `bUsingDistanceCullFade` / `AxisZConeCullingAngle` | 低 | 中 | 便于和 FrustumCull 内部条件分支一一对照 |
| 自动按"In-Scene 占比"标红打印 | 低 | 低 | 终端配色，提升可读性 |

---

## 11. 联系人 / 来源

- 改动作者：ZXB
- Stream：`//GR/MergeTest`
- 关联 P4 client：`DJANGOZHAN-PCFW_GR_MergeTest`
- 改动范围标记：`#pragma region Engine ZXB` / `#pragma endregion`
- 关联文档：[SceneVisibility_FrustumCull_ZXB_Optimization.md](../../../Renderer/Private/SceneVisibility_FrustumCull_ZXB_Optimization.md)（FrustumCull 性能优化总结）
