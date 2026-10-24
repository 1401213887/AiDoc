# UE5 材质实例父代理空指针：父材质先于子 FinishDestroy 与「父销毁断开子链接」修复

对象：S1Game UE5 fork（//GR/main，引擎 UE5EA 5.5.4）的材质实例渲染侧父链。
现象：TAPD 1270542 / CrashSight issue 8998ef3bc74dab584dbfb96f10e83f9e，RenderThread 在 `FMaterialInstanceResource::GetParameterValue` 读地址 0x0 崩溃（`EXCEPTION_ACCESS_VIOLATION_READ`），机器已运行 116.6 分钟。
根因：子材质实例在 RT 上用裸指针 `Parent` 指向父；父材质可以先于子走完 `FinishDestroy`（`Resource = nullptr`），子在 VT producer 销毁回调里重算参数时读到空 proxy。两条路径：GC 跨批次销毁父子，以及子仍存活、父被流送卸载（garbage elimination 把子的 GT 父指针置空，RT 镜像不变）。
修复：判空止血 + 根治——父材质 `BeginDestroy` 时经登记表断开所有在 RT 上引用它的子，既有 `ParentRefFence` 保证父的 FinishDestroy 晚于断开命令。
验证：同工具链 A/B 交替 10 局（nr_3c ×6、nr_3c_exittrace ×4）0 崩溃；断开开销每局 RT 合计 0.17–0.65 ms，单次 ≤ 18 µs；对局段与退出卸载段帧时间、GC 每对象耗时无可测回退。
工具链：Perforce CL 1213178（未提交）、UBT Development `-EnableLQGAPSystem`、Gauntlet 自动跑测、TraceMcpS1 离线 utrace 分析、capstone 反汇编 minidump。

## 1. 结论：崩在哪、为什么、怎么修

一句话：RT 上的子材质实例沿裸指针 `Parent` 去查父参数时，父已经 `FinishDestroy`，`GetRenderProxy()` 返回 nullptr，代码没判空就解引用。

崩溃栈（CrashSight 原始，崩溃包 release CL 1183280 的行号，与本文其余引用的 //GR/main 行号不同）：

```
1  FMaterialInstanceResource::GetParameterValue        MaterialInstance.cpp:425   (CL 1183280)
2  UE::Shader::EvaluateParameter                        Preshader.cpp:604
3  UE::Shader::EvaluatePreshader                        Preshader.cpp:1335
4  FUniformExpressionSet::FillUniformBuffer             MaterialUniformExpressions.cpp:1240
6  FMaterialRenderProxy::EvaluateUniformExpressions     MaterialRenderProxy.cpp:846
7  OnVirtualTextureDestroyedCB                          MaterialRenderProxy.cpp:581
8  FVirtualTextureProducerCollection::CallPendingCallbacks
11 FDeferredShadingSceneRenderer::Render
```

minidump 里崩溃点附近的机器码（capstone 反汇编）把直接原因钉死了：

```
mov  rcx, [rsi + 0xd8]     ; rcx = this->Parent          （成功：Parent 指针可读）
mov  rax, [rcx]            ; rax = Parent->vptr          （成功：虚表可读）
call [rax + 0x3a0]         ; Parent->GetRenderProxy()    （成功返回 rax = 0）
mov  rcx, [rax]            ; ← 崩溃：读 nullptr->vptr，地址 0x0
```

三条判断：

| 观察 | 结论 |
|---|---|
| `mov rcx,[rsi+0xd8]` 与 `call [rax+0x3a0]` 都成功 | `Parent` 指向的 UObject 内存仍在、虚表可调——不是野指针 |
| 虚调用返回 `RAX = 0` | `GetRenderProxy()` 直接返回成员 `Resource`（`MaterialInstance.cpp:2503`），所以父的 `Resource == nullptr` |
| 下一条 `mov rcx,[rax]` 读 0x0 | 与 CrashSight `expAddr = 0x0` 吻合 |

`Resource` 全文件只有两处赋值：创建（`MaterialInstance.cpp:1051`）和 `UMaterialInstance::FinishDestroy` 里置空（`MaterialInstance.cpp:4416`）。所以父处于「已 FinishDestroy、UObject 内存尚未回收」的状态。

修复分两层：

| 层 | 改动 | 作用 |
|---|---|---|
| 止血 | `GetParameterValue` 递归查父前对 `Parent->GetRenderProxy()` 判空（`MaterialInstance.cpp:523`） | 挡住本次确认的空返回；未命中走参数默认值 |
| 根治 | 父 `BeginDestroy` 时断开所有在 RT 上引用它的子（`MaterialInterface.cpp:734` 调 `MaterialInstance.cpp:226`） | 让「父先走、子还在读」这个状态不再出现 |

## 2. 全景：子材质实例的两份父指针

一句话：子材质实例的父在 GT、RT 各存一份；GT 那份是会被 GC 改的强引用，RT 那份是只在换父时更新的裸指针，问题出在两份不同步。

**图 1：子材质实例在 GT、RT 各持一份父指针；RT 那份是裸指针，只在换父命令里更新**

- 子 `UMaterialInstance`（GT）
  - `Parent`：TObjectPtr，GC garbage elimination 可将其置空
  - `Resource` → 子 `FMaterialInstanceResource`（`PostInitProperties` 时创建）
- 子 `FMaterialInstanceResource`（RT）
  - `Parent`：`UMaterialInterface*` 裸指针（RT 镜像），只由 SetParent 的 RT 命令写
  - `GameThreadParent`：GT 侧副本
  - VT producer 销毁回调（`bHasVirtualTextureCallbacks`）
- 父 `UMaterialInterface`（GT）：`Resource`（MI）/ `DefaultMaterialInstance`（UMaterial），`ParentRefFence` 在 FinishDestroy 前必须完成
- 父 `FMaterialRenderProxy`（RT）：即 `父->GetRenderProxy()`；父 FinishDestroy 后返回 nullptr
- 崩溃点：RT 沿 RT Parent 调 `GetRenderProxy()` 拿到 nullptr 后直接解引用（修复前 `GetParameterValue` 无判空）

| 字段 | 所在 | 类型 | 谁写 |
|---|---|---|---|
| `UMaterialInstance::Parent` | GT UObject | TObjectPtr，GC 可置空 | 资产加载 / `SetParentInternal` / GC garbage elimination |
| `FMaterialInstanceResource::GameThreadParent` | GT 侧副本 | 裸指针（`MaterialInstanceSupport.h:406`） | `GameThread_SetParent` |
| `FMaterialInstanceResource::Parent` | RT 镜像 | 裸指针（`MaterialInstanceSupport.h:400`） | 只有 `GameThread_SetParent` 入队的 RT 命令（`MaterialInstance.cpp:642`），修复后再加 Detach 命令（`MaterialInstance.cpp:265`） |

RT 读父的入口都在 `FMaterialInstanceResource` 里：取材质 `GetMaterialNoFallback`（`MaterialInstance.cpp:370`）、取参数 `GetParameterValue`（`MaterialInstance.cpp:403`，递归查父在 `MaterialInstance.cpp:515`）。它们都先判 `if (Parent)`，所以「`Parent == nullptr`」是合法状态；危险的是「`Parent` 非空但父的 proxy 已没了」。

为什么 VT 回调是触发器：材质用到虚拟纹理时会注册 producer 销毁回调；回调 `OnVirtualTextureDestroyedCB`（`MaterialRenderProxy.cpp:576`）先 `InvalidateUniformExpressionCache(false)`（`MaterialRenderProxy.cpp:580`），再立刻 `UpdateUniformExpressionCacheIfNeeded`（`MaterialRenderProxy.cpp:583`）重算 uniform expression——这一步就会递归查父。它发生在 `FSceneRenderer::OnRenderBegin`，和 GT 的 GC 没有同步关系。

## 3. 根因：父材质为什么能先于子走完销毁

一句话：引擎只保护了「换父」这一条边，没保护「销毁」；父销毁时只等自己的 RT 工作，不知道有子还在 RT 上读它。

### 3.1 原有保护只覆盖换父

换父时引擎知道旧父可能还被 RT 读，所以给旧父起 fence：`GameThread_SetParent` 里 `OldParent->ParentRefFence.BeginFence()`（`MaterialInstance.cpp:649`），父的 `UMaterialInterface::IsReadyForFinishDestroy` 等这个 fence（`MaterialInterface.cpp:726`）。

销毁时没有对应的保护：

| 函数 | 等什么 | 不等什么 |
|---|---|---|
| `UMaterialInstance::IsReadyForFinishDestroy`（`MaterialInstance.cpp:4406`） | 自己的 `UsedByRT == 0` | 子材质在 RT 上的读取 |
| `UMaterialInterface::IsReadyForFinishDestroy`（`MaterialInterface.cpp:726`） | 自己的 `ParentRefFence` | 同上 |

### 3.2 前提：子强引用着父，父怎么会先走

子的 `Parent` 是 UPROPERTY 强引用（`MaterialInstance.h:662`）。正常情况下只要子可达，父就不会被 GC 回收。父能先于子走，只有两种情况：

| 情况 | 为什么强引用拦不住 | 此时「子还在」指什么 | 子上不上屏 |
|---|---|---|---|
| 父子同时不可达 | 两者都没人引用，同一轮 GC 一起判死 | 子的 UObject 已判死，只是 RT 上的 `FMaterialInstanceResource` 要等子 FinishDestroy 才入队删除，期间 VT 回调仍在册 | 不上屏：所在关卡的 primitive 已注销 |
| 父被显式标为 Garbage，子在包外仍被引用 | `MarkAsGarbage` 的语义是「不管谁引用都要删」；GC 遍历到指向 Garbage 对象的引用会直接置空（`GarbageCollection.cpp:4864` 判 KillFlag，`GarbageCollection.cpp:4870` `KillReference`） | 子的 UObject 真的还活着，只是 GT 父指针被清成 null | 可能上屏 |

第一种是常态。典型例子是地形：地形组件按 MIC 创建 MID，Outer 是组件自己（`Landscape.cpp:2650`），父 LMIC 的 Outer 是地形 proxy（`LandscapeEdit.cpp:640`），父子都在关卡包里，流送卸载时一起成为垃圾，对应下文路径 A。

第二种需要一个跨包引用：子在关卡包外（例如常驻对象上的 MID），父在被卸载的关卡包内。本项目里具体是哪些资产会这样引用，**没找到实例**，属于代码允许、未证实，对应下文路径 B。

### 3.3 路径 A：父子同为垃圾、跨批次销毁

同一轮 GC 里父子都不可达时，GC 期间 fence 被 bundler 合并成同一个完成事件（`RenderingThread.cpp:1037`、`RenderingThread.cpp:1043`），同一批次里父不可能先 Finish。但增量 Unhash 把销毁切成多批时，父在早批次完成 RT 释放和 fence、子的 RT 释放命令还没执行，中间夹一帧渲染，子的 VT 回调仍然在册，就会查到已 Finish 的父。

这条路径是**代码允许**的，但 minidump 不含父所在堆页，无法证实事故就是它。

### 3.4 路径 B：子仍存活、父被流送卸载（交接文档未覆盖）

**图 2：根因路径 B：子还活着、父被流送卸载。GT 父指针被 GC 置空，RT 镜像没人改，父照常走完销毁**

GT 时间线：
1. ① 流送卸载：父所在包 `MarkAsGarbage`；子在别的包，仍被引用
2. ② GC 置空引用：`gc.GarbageEliminationEnabled=True` → 子的 GT `Parent` 变为 null。**RT 镜像不变（无人通知）**
3. ③ 父 `BeginDestroy`：只等自己的 RT 工作，不知道子还在读它
4. ④ 父 `FinishDestroy`：`Resource = nullptr`，UObject 内存尚未回收 → `GetRenderProxy()` 返回 nullptr

RT：
5. ⑤ 子仍以 RT `Parent` 指向父；VT producer 销毁回调 `OnVirtualTextureDestroyedCB` → `InvalidateUniformExpressionCache` → `UpdateUniformExpressionCacheIfNeeded` → `EvaluatePreshader` → `GetParameterValue` 未命中递归查父 → `Parent->GetRenderProxy() == nullptr` → 读 0x0 → `EXCEPTION_ACCESS_VIOLATION_READ`

三个事实拼起来：

1. 引擎默认开 `gc.GarbageEliminationEnabled=True`（`BaseEngine.ini:1652`）：GC 会把指向 Garbage 对象的强引用置空。
2. 关卡流送卸载时把包内所有对象标成 Garbage：`PackageObject->MarkAsGarbage()`（`LevelStreamingGCHelper.cpp:290`）、`Package->MarkAsGarbage()`（`LevelStreamingGCHelper.cpp:294`）。
3. 子 MI/MID 在别的包、仍被组件引用，所以子不销毁；GC 只把它 GT 侧的 `Parent` 置空。RT 镜像 `Parent` 没有任何代码去改（写点只有换父命令），父照常 BeginDestroy → FinishDestroy。

这条路径里**子根本不进入销毁流程**，所以任何「在子销毁时做保护」的方案都挡不住它（对比见 §7）。崩溃机日志里 `LogGarbage` 92,316 条、几乎全部属于 `/Game/Maps/Nord/NordLand` 及其 `_Generated_` 子包，`IncrementalPurgeGarbage start` 11,106 次——与这条路径吻合，但只是旁证。

## 4. 修复：父材质销毁时断开所有子链接

一句话：维护「父 → RT 子资源」登记表；父 `BeginDestroy` 时在锁内取出它的子、入队一条断开命令，再起既有的 fence，父的 FinishDestroy 自然被推迟到断开之后。

**图 3：修复后时序：父在 BeginDestroy 里先断开子，再起 fence；父的 FinishDestroy 被既有 fence 推迟到断开命令执行之后**

GT 时间线：
1. ① 父 `UMaterialInterface::BeginDestroy`
2. ② `GameThread_DetachChildrenOf(父)`：锁内取出子集合，子 GT `Parent` 置空 → 入队 `FChildDetach[]` 到 RT（Ⓐ）
3. ③ `ParentRefFence.BeginFence()`：fence 命令排在 Detach 之后（Ⓑ）
4. ④ `IsReadyForFinishDestroy` 等 fence；Ⓑ 完成后 `IsFenceComplete()` 为真
5. ⑤ 父 `FinishDestroy`：`Resource = nullptr`，此时已无子指向它

RT：
- Ⓐ `DetachMaterialInstanceChildren`：子 `Parent == 本父` → 置 nullptr；`bInvalidateCache` 且未标 GC → `InvalidateUniformExpressionCache(false)`（摘 VT 回调 + 缓存按无父重建）
- Ⓑ fence 事件触发：FIFO，必在 Ⓐ 之后执行

子自己的删除命令（`GameThread_Destroy`）与 Detach 同锁入队：RT 上 Detach 一定先执行，Child 指针在 Ⓐ 里有效。

### 4.1 登记表与三个入口

**图 4：登记表与三个入口：登记变更和对应的渲染命令在同一把锁内完成，决定了 RT 上的执行顺序**

| 入口 | 锁内对登记表 | 锁内入队的 RT 命令 |
|---|---|---|
| `GameThread_SetParent(新父)` | `Unlink(旧父)` + `Link(新父)` | `InitMaterialInstanceResource`：`Parent = 新父` |
| `GameThread_Destroy()` | `Unlink(GameThreadParent)` | `FDestroyMaterialInstanceResourceCommand`：`delete Resource` |
| `GameThread_DetachChildrenOf(父)` | `RemoveAndCopyValue(父)` | `DetachMaterialInstanceChildren`：`Parent == 父` → nullptr |

登记表 `FRegistry`：全局单例（故意泄漏），`Mutex: FCriticalSection`，`ChildrenByParent: TMap<const UMaterialInterface*, TSet<FMaterialInstanceResource*>>`。
三个入口的「登记变更 + 入队」都在同一把锁里：RT 上命令的先后 = 登记表变更的先后。

| 位置 | 内容 |
|---|---|
| `MaterialInstance.cpp:193` | `namespace MaterialInstanceParentLinks`，`FRegistry { Mutex; ChildrenByParent }` |
| `MaterialInstance.cpp:200` / `MaterialInstance.cpp:208` | `Link` / `Unlink`；子集合空了就删键 |
| `MaterialInstance.cpp:219` | `Get()`：`*new FRegistry` 故意泄漏——退出时的 GC purge 可能晚于静态析构 |
| `MaterialInstance.cpp:622` | `GameThread_SetParent` 开头加锁；`MaterialInstance.cpp:628`–`MaterialInstance.cpp:629` 换登记 |
| `MaterialInstance.cpp:286` | `GameThread_Destroy` 锁内 `Unlink`，与 `delete Resource` 命令（`MaterialInstance.cpp:293`）同锁入队 |
| `MaterialInstance.cpp:226` | `GameThread_DetachChildrenOf` |
| `MaterialInterface.cpp:734` | `UMaterialInterface::BeginDestroy` 里调用，在 `ParentRefFence.BeginFence()`（`MaterialInterface.cpp:736`）之前 |

挂在 `UMaterialInterface::BeginDestroy` 而不是 `UMaterialInstance`：父可以是 `UMaterial` 也可以是 `UMaterialInstance`，基类一处覆盖两种；多层继承 `C → P → G` 时每一层只断自己那条边，G 销毁断 P、P 销毁断 C，不需要遍历祖先。

### 4.2 断开命令做了什么

`GameThread_DetachChildrenOf`（`MaterialInstance.cpp:226`）在锁内：

1. `RemoveAndCopyValue(父)` 取出子集合，登记表里不再留父的键（父地址以后被复用也不会误命中）。
2. 对每个子：`GameThreadParent = nullptr`；在 GT 上判断子这轮 GC 之后是否还活着——`bChildSurvivesGC = !Owner->IsUnreachable() && !Owner->HasAnyFlags(RF_BeginDestroyed)`（`MaterialInstance.cpp:248`），打包成 `FChildDetach { Child, bInvalidateCache }`（`MaterialInstance.cpp:238`）。
3. `GExitPurge` 时直接返回（`MaterialInstance.cpp:253`）：退出清理阶段已不再渲染，渲染线程可能已停、命令会在 GT 内联执行。
4. 入队 `DetachMaterialInstanceChildren`（`MaterialInstance.cpp:259`）。

RT 执行时：子的 `Parent` 仍等于本父才断开（`MaterialInstance.cpp:265`）——换父命令可能先执行，已换走的不动；断开后若 `bInvalidateCache` 且子未被标 GC，再 `InvalidateUniformExpressionCache(false)`（`MaterialInstance.cpp:271`），摘掉 VT 回调（`MaterialRenderProxy.cpp:986`）并让缓存按无父重建。

### 4.3 为什么这样设计

| 取舍 | 换来什么 | 付出什么 | 什么时候失效 |
|---|---|---|---|
| 断开后置 `nullptr`，不重绑默认材质 | `Parent == nullptr` 是既有合法状态：`GetMaterialNoFallback`（`MaterialInstance.cpp:370`）返回 null，`UpdateUniformExpressionCacheIfNeeded` 在材质为空时跳过求值（`MaterialRenderProxy.cpp:1056`）；改动最小 | 存活子此后按无父渲染（回退默认材质），直到 GT 重新 `SetParent` | 若出现不判 `Parent` 就解引用的新代码 |
| 只对存活子 Invalidate | 同轮 GC 一起死的子已走过 `ReleaseResource`，VT 回调已摘（`MaterialRenderProxy.cpp:1172`、`MaterialRenderProxy.cpp:1178`）；省掉 Invalidate 里项目调试行的 `GetFriendlyName()` 字符串分配（`MaterialRenderProxy.cpp:992`） | GT 上多一次 `IsUnreachable` + 标志位读取 | 子在「判断」与「RT 执行」之间被重新引用复活——GC 已判定不可达的对象不会复活 |
| 登记变更与入队同锁 | RT 上命令顺序 = GT 决策顺序：Detach 与子的 delete 不会颠倒（delete 后再访问 Child），Detach 与换父不会交错 | 一把全局锁；`GameThread_SetParent` 可能在异步加载/并行线程调（`MaterialInstance.cpp:614` 起的原注释） | 锁竞争变重时；实测见 §6，开销可忽略 |
| 登记表放 `.cpp` 静态，不改 `UMaterialInterface` 头 | 不改对象布局、不触发大面积重编（实测增量 6 actions） | 多一次 map 查找 | — |
| 复用既有 `ParentRefFence` | 不新建同步原语，`IsReadyForFinishDestroy`（`MaterialInterface.cpp:726`）原样等待 | 无 | fence 被提前完成（例如有人在 Detach 之后、BeginFence 之前插入 flush 以外的新命令路径）——当前不存在 |

### 4.4 断开之后，子的画面会怎样

分两种情况看（对应 §3.2 的两种前提）：

| 情况 | 子上不上屏 | 修复前 | 修复后 |
|---|---|---|---|
| 父子同死（路径 A） | 不上屏 | 子的 VT 回调查到已 Finish 的父 → 崩溃 | 只断开、不 Invalidate；子随后被删除，画面无影响 |
| 子存活、父被强制删除（路径 B） | 可能上屏 | GT 已经按「无父」处理：`UMaterialInstance::GetMaterial()` 在 `Parent` 为空时返回默认材质（`MaterialInstance.cpp:1230`、`MaterialInstance.cpp:1236`）；RT 却还拿着旧父 → 崩溃或读到无效数据 | RT 与 GT 一致，按无父渲染（见下） |

路径 B 下子按无父渲染的具体表现：

- **子没有自己的静态排列资源**：`GetMaterialNoFallback` 返回空 → `GetMaterialWithFallback` 进入回退循环（`MaterialRenderProxy.cpp:1199`）→ `GetFallback` 无父时取 `UMaterial::GetDefaultMaterial(MD_Surface)`（`MaterialInstance.cpp:366`）→ 画成默认材质（灰色网格）。
- **子有自己的静态排列资源**（`bHasStaticPermutationResource`）：shader 仍是子自己的；子自己覆盖过的参数照常生效，没覆盖的参数取 shader 编译时的表达式默认值，而不是原来父的值。

所以路径 B 的画面变化是「父被强制删除」这件事本身造成的，GT 早已按无父处理；修复只是让 RT 不崩溃、和 GT 一致。要让画面正确，得在内容或逻辑层解决：长寿对象不要引用会随关卡卸载的材质，或在关卡卸载时重新 `SetParent` 到资产包里的材质。

数值演算：第二轮实测 B2 每局断开调用 449 + 92 = 541 次，RT 合计 0.169 ms，平均 0.31 µs/次；放到一局约 8,000 帧上，每帧摊 0.02 µs。

## 5. 一个子材质实例的一生（修复后）

一句话：无论父先走还是子先走，RT 都拿不到已释放父的 proxy——父先走由 Detach 断开，子先走由 Destroy 注销。

**图 5：一个子材质实例的一生（修复后）：无论父先走还是子先走，RT 都不会再拿到已释放父的 proxy**

1. 创建：`PostInitProperties` → `Resource = new FMaterialInstanceResource`
2. `SetParent(P)`：登记表 `Link(P)`；RT `Parent = P`
3. 正常渲染（每帧 / VT 回调）：参数未命中递归查 P，P 的 proxy 必然有效
4. 分支「谁先销毁？」
   - 父先走（P 先 BeginDestroy）：Detach → RT `Parent = nullptr`；子存活 → Invalidate（摘 VT 回调）；之后按无父渲染（回退默认材质）；P 的 Finish 等到 Detach 执行完
   - 子先走（子先 FinishDestroy）：`GameThread_Destroy` → `Unlink(P)` + 入队 delete；P 之后销毁时已看不到这个子

RT `Parent` 的写入点只有两处：SetParent 的 RT 命令（换父）、Detach 的 RT 命令（父销毁）。两处都在登记表锁内入队，RT 执行顺序与 GT 决策顺序一致。

| 场景 | 登记表 | RT `Parent` | 渲染结果 |
|---|---|---|---|
| 正常 | `P → {子}` | `P` | 参数递归查 P |
| 换父 P → Q | `Q → {子}` | 换父命令执行后为 `Q`；旧父 P 由 `ParentRefFence`（`MaterialInstance.cpp:649`）保活到命令执行 | 查 Q |
| P 先销毁，子同轮死 | `P` 键删除 | Detach 置 nullptr，不 Invalidate | 子随后被 delete，不再渲染 |
| P 先销毁，子存活（流送卸载） | `P` 键删除 | Detach 置 nullptr，Invalidate | 回退默认材质，直到重新 SetParent |
| 子先销毁 | 子从 `P` 集合移除 | delete 子 | P 销毁时看不到这个子 |

## 6. 性能验证：同工具链 A/B 共 10 局

一句话：新逻辑每局 RT 开销 0.17–0.65 ms；对局段、退出卸载段的帧时间和 GC 每对象耗时都没有超出同版本波动的差异。

### 6.1 方法

- A = 引擎三文件还原为 depot 版本、B/B2 = 修复版，**同一套工具链本地编译**（Development + `-EnableLQGAPSystem`，各 6 actions 增量）；不用出厂 exe 当基线——出厂版与本地构建本身就有差异。
- 二进制核对：A 中不存在 `DetachMaterialInstanceChildren` 字符串，B/B2 中存在。
- 包体 CL 1204635，服务器 `lobby-test.grgame.qq.com:13753`（Main 冒烟测试服，BinCL 1204635 精确匹配）。
- **A、B 交替跑**，抵消服务器负载与机器温度随时间的漂移。
- 10 局全部 `CASE-FINISHED`；`Assertion failed` / `Fatal error` / `Critical error` / `Ensure condition failed` / `NetChecksumMismatch` 全部 0 次，无新 `CrashContext.runtime-xml`。

### 6.2 第一轮：nr_3c ×6（录制对局中 110–170 s，B = 首版）

| 局 | 帧数 | avg | p50 | p95 | p99 | max | ≥33.3 ms |
|---|---|---|---|---|---|---|---|
| 1_A | 3890 | 15.31 | 14.49 | 20.71 | 26.54 | 114.9 | 9 |
| 2_B | 3932 | 15.16 | 14.70 | 19.00 | 22.82 | 84.8 | 6 |
| 3_A | 4331 | 13.76 | 13.29 | 16.93 | 21.14 | 70.5 | 5 |
| 4_B | 3620 | 16.44 | 15.76 | 21.50 | 27.10 | 91.0 | 13 |
| 5_A | 3987 | 14.95 | 14.41 | 18.99 | 23.17 | 88.7 | 9 |
| 6_B | 4041 | 14.74 | 14.05 | 19.42 | 25.15 | 86.4 | 7 |

| 指标（ms） | A 三局均值 | B 三局均值 | 同版本三局极差 |
|---|---|---|---|
| avg | 14.68 | 15.45 | 约 1.6 |
| p95 | 18.88 | 19.97 | 约 3.8 |
| p99 | 23.61 | 25.02 | 约 5.4 |

B 的 avg 差 +0.77 ms 小于同版本三局极差，t≈1.1，不显著。直接开销：每局 `DetachMaterialInstanceChildren` 256 / 278 / 284 次，RT 合计 0.353 / 0.456 / 0.654 ms，单次最大 18 µs。

用**没改动的代码**做对照，判断 GC 每对象差异的来源（µs/次）：

| 计时器 | 是否含改动 | 2_B | 3_A | 4_B | 5_A | 6_B | B 均 / A 均 |
|---|---|---|---|---|---|---|---|
| `~UObject ConditionalBeginDestroy` | 含 | 3.896 | 3.775 | 3.905 | 3.514 | 4.061 | +8.5% |
| `UObject::BeginDestroy` | 不含（基类） | 1.482 | 1.417 | 1.499 | 1.387 | 1.462 | +5.6% |
| `UnhashObject` | 不含（CoreUObject） | 1.215 | 1.171 | 1.239 | 1.144 | 1.214 | +5.6% |

没改动的代码在 B 上同样慢 5.6%，所以这部分差异主要不是新逻辑带来的（推测是二进制布局或环境，未证实）。1_A 的 trace 不含 GC 区段，未列入。

### 6.3 第二轮：nr_3c_exittrace ×4（录制延长到回大厅后，B2 = 只对存活子 Invalidate）

第一轮的录制窗口（110–170 s）没包含退出对局（180 s）时的大批销毁，而那是最坏情况：大量父子同轮死。为此新增 case `nr_3c_exittrace`，把 `Trace.Stop` 挪到 `BattleToHallDelayed` 之后 20 s。trace 按日志里 `ExitBattle` 的墙钟时刻切成两段（trace 首帧近似 `Trace.Start`）。

| 指标 | 1_A | 2_B2 | 3_A | 4_B2 |
|---|---|---|---|---|
| 对局段帧数 | 5151 | 5201 | 5249 | 5310 |
| 对局段 avg（ms） | 13.43 | 13.31 | 13.18 | 13.02 |
| 对局段 p95 | 16.54 | 15.74 | 15.84 | 15.50 |
| 对局段 p99 | 21.34 | 19.72 | 21.55 | 19.00 |
| 退出段帧数 | 3039 | 2894 | 3022 | 3024 |
| 退出段 p99 | 33.02 | 33.13 | 33.81 | 32.98 |
| 退出段最长帧（关卡卸载本身） | 4744.7 | 4771.2 | 4595.3 | 4542.2 |
| 退出段销毁对象数 | 210,899 | 224,139 | 220,445 | 198,171 |
| 退出段 `ConditionalBeginDestroy`（µs/次） | 1.75 | 1.89 | 1.76 | 1.72 |
| 退出段 `ConditionalFinishDestroy`（µs/次） | 3.74 | 3.22 | 3.13 | 3.25 |
| 断开调用（对局段 + 退出段） | — | 92 + 449 | — | 196 + 305 |
| 断开 RT 合计（ms） | — | 0.042 + 0.127 | — | 0.119 + 0.099 |
| 断开单次最大（µs） | — | 4 | — | 5 |

结论：退出卸载段 20 万级对象销毁下，每对象耗时 A/B2 交错、无方向性差异；4.5–4.8 s 的最长帧是关卡卸载本身，A/B2 都有。

## 7. 方案对比：父销毁断开 vs 子销毁给祖先加 fence

一句话：交接文档原定的「子销毁时给全祖先链加 fence」只能覆盖路径 A，覆盖不了路径 B，所以改为「父销毁时断开子」。

| 维度 | 子销毁时给祖先加 fence | 父销毁时断开子（本文） |
|---|---|---|
| 触发点 | 子 `BeginDestroy` | 父 `BeginDestroy`（`MaterialInterface.cpp:734`） |
| 路径 A（父子跨批次同死） | 覆盖：祖先的 Finish 等子的 RT 释放 | 覆盖：父的 Finish 等 Detach |
| 路径 B（子存活、父被流送卸载） | **不覆盖**：子不进入销毁流程，没有触发点 | 覆盖：父销毁时主动断开 |
| 多层继承 | 必须遍历整条祖先链（只等直接父时，祖父可能先 Finish） | 每层只断自己那条边，天然覆盖 |
| 父的 GT 指针被 GC 置空后 | 子已找不到祖先，无法加 fence | 不依赖子的 GT 指针，靠登记表 |
| 对父销毁时长的影响 | 父被拖到所有子的 RT 释放之后 | 只多等一条 Detach 命令（与原 fence 同批） |
| 改动面 | 子销毁路径 + 祖先遍历 | 登记表 + 三个入口加锁 + 基类一处调用 |

其他被否决的方向：只在 VT 回调里检查 GC 标记（标记本身也在尚未执行的 RT 命令里）；把刷新延迟到下一帧（不建立生命周期依赖，只是挪开窗口）；把 RT `Parent` 换成 `FMaterialRenderProxy*`（proxy 本身也会被 `GameThread_Destroy` 删除）；升级引擎（上游 5.5.4 与 5.8.1 同样未判空且即时刷新）。

## 8. 排查 Checklist：再遇同类崩溃怎么定位

1. **先看机器码，不猜。** 用 minidump 的 MemoryList 反汇编 RIP 邻域（capstone 即可，不需要 WinDbg），确认故障指令读的是哪个寄存器、该寄存器是谁写的。本案若只看栈会以为是 `Parent` 野指针。
2. **区分「对象已释放」和「对象已 FinishDestroy」。** 虚调用成功 + 返回 0 ⇒ 对象内存在、成员被显式置空；查该成员的全部赋值点（本案 `MaterialInstance.cpp:1051`、`MaterialInstance.cpp:4416`）。
3. **找 GT/RT 双份状态。** 凡是「RT 镜像 + GT 原件」的结构，列出 RT 镜像的全部写点（本案只有 `MaterialInstance.cpp:642`），问一句：GT 原件被别人改了，谁通知 RT？
4. **别忘了 garbage elimination。** `gc.GarbageEliminationEnabled=True`（`BaseEngine.ini:1652`）下，强引用会被 GC 静默置空，「子还活着、父没了」是常态。
5. **验证用 A/B 交替 + 同工具链。** 跑测命令：`loop_runner.py --case nr_3c_exittrace`（覆盖退出卸载）；分析脚本见 §10。
6. **对照组用没改的代码。** 看到 GC 计时器有差异时，同时比 `UObject::BeginDestroy` / `UnhashObject` 这类不含改动的计时器，排除环境因素。

## 9. 已知问题与技术债

| 项 | 说明 |
|---|---|
| 同族未判空调用 | `GetFallback`（`MaterialInstance.cpp:361`）、`GetUserSceneTextureOverride`（`MaterialInstance.cpp:553`）、`GetBlendableLocation`（`MaterialInstance.cpp:579`）、`GetBlendablePriority`（`MaterialInstance.cpp:598`）仍直接 `Parent->GetRenderProxy()->`。根治后理论上不会再遇空 proxy，故未动 |
| 修复未在原场景验证命中 | 原案运行 116.6 分钟才崩；本次单局几分钟，断开逻辑是否挡住了真实崩溃无法直接证明。可加默认关闭的 Verbose 计数日志，或跑 `neoring_rs`（约 42 分钟）长测 |
| release 分支未同步 | 崩溃分支 `//GR/release` 第 425 行仍是裸调用 |
| 项目调试行的开销 | `InvalidateUniformExpressionCache` 内每次 `GetFriendlyName()` 赋给全局字符串（`MaterialRenderProxy.cpp:992`），是所有 Invalidate 调用者共同承担的分配 |
| 同类历史单 | TAPD 1201736（`FMaterialInstanceResource::GetMaterialNoFallback`，`FRelevancePacket::ComputeRelevance` worker 线程）属同一生命周期问题，建议合并评估 |
| CL 状态 | 三文件在 CL 1213178，**未提交** |

## 10. 行号基线与参考

**行号基线**：本文 `文件:行号` 引用对齐 `C:\GR_main`（//GR/main）工作区，`MaterialInstance.cpp#15`、`MaterialInterface.cpp#8`、`MaterialInstanceSupport.h#2` 加上 CL 1213178 的未提交改动。崩溃栈里 MaterialInstance.cpp 的第 425 行是崩溃包 release CL 1183280 的行号，对应本文基线的 `MaterialInstance.cpp:523`（判空修复所在行）。

**改动文件**

| 文件 | 改动 |
|---|---|
| `UE5EA/Engine/Source/Runtime/Engine/Private/Materials/MaterialInstanceSupport.h` | 声明 `static void GameThread_DetachChildrenOf(const UMaterialInterface*)`（`MaterialInstanceSupport.h:237`） |
| `UE5EA/Engine/Source/Runtime/Engine/Private/Materials/MaterialInstance.cpp` | 登记表、`GameThread_DetachChildrenOf`、`GameThread_SetParent` / `GameThread_Destroy` 加锁、`GetParameterValue` 判空 |
| `UE5EA/Engine/Source/Runtime/Engine/Private/Materials/MaterialInterface.cpp` | include + `BeginDestroy` 调用（`MaterialInterface.cpp:734`） |

所有改动均以 `#pragma region Engine ZXB` 包裹并带 `// [ZXB] TAPD 1270542：…` 意图注释。

**参考**

- TAPD：`1168880148001270542`（workspace 68880148）；CrashSight issue `8998ef3bc74dab584dbfb96f10e83f9e`（appId `6a5434988d`）
- 跨分支交接文档：`C:\Users\djangozhang\WorkBuddy\GR_Release\TAPD-1270542-材质父代理空指针-跨分支交接.md`
- 规划与过程记录：`C:\GR_main\.planning\2026-10-23-tapd1270542-parent-detach\`
- A/B 数据：`D:\s1ab\traces\*.analysis.json`、`D:\s1ab\summary.txt`、`D:\s1ab\targeted.json`、`D:\s1ab\exittrace.json`；脚本 `D:\s1ab\scripts\extract.py`、`summarize.py`、`targeted.py`、`exitseg.py`；驱动 `D:\s1ab\run_ab.sh`、`run_ab2.sh`
- 跑测 case：`S1Game自动化Debug` skill 的 `nr_3c`、`nr_3c_exittrace`（`templates/NR_3C_EXITTRACE.txt`）
