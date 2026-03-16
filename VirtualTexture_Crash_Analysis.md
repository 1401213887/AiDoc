# UE5 虚拟纹理系统崩溃分析与修复总结

## 一、崩溃现象

### 崩溃堆栈
```
EXCEPTION_ACCESS_VIOLATION_READ
FUploadingVirtualTexture::~FUploadingVirtualTexture()  (UploadingVirtualTexture.cpp:95)
    ↓
DestructItems<TUniqueObj<Chaos::FBoxFloat3>,int>  (MemoryOps.h:108)
```

### 关键信息
- **构建类型**：Shipping 版本
- **崩溃线程**：渲染线程
- **崩溃位置**：`~FUploadingVirtualTexture()` 析构函数中 `TArray<TUniquePtr<FVirtualTextureCodec>>` 的 `DestructItems` 调用
- **符号说明**：堆栈中的 `DestructItems<TUniqueObj<Chaos::FBoxFloat3>,int>` 是 Shipping 版本符号还原的误差，实际析构的是 `TArray<TUniquePtr<FVirtualTextureCodec>>` 中的元素

---

## 二、排查过程

### 阶段1：初步分析与首次修复尝试

**假设**：异步任务（`FTranscodeTask`）尚未完成时，`~FUploadingVirtualTexture()` 提前执行导致 Use-After-Free。

**添加的防御性代码**：
1. 在 `~FUploadingVirtualTexture()` 中等待 Codec 创建任务完成
2. 添加 `IsIdle()` 检查和自旋等待 Transcode 任务完成
3. 添加显式 `Empty()` 清空数组
4. 在 `VirtualTextureProducer::Release()` 中添加 `RetireOldCodecs()` 调用

### 阶段2：跨模块编译问题

- `FVirtualTextureCodec::RetireOldCodecs()` 定义在 Engine 模块的 Private 目录中
- `VirtualTextureProducer.cpp` 属于 Renderer 模块，无法直接 `#include` Engine 模块的 Private 头文件
- **解决**：移除 `VirtualTextureProducer.cpp` 中的 `RetireOldCodecs()` 调用

### 阶段3：深度时序分析与代码回退

通过深入分析 UE5 TaskGraph 的 `ExecuteTask` / `TryExecuteTask` 时序，得出以下结论：

- `FVirtualTextureProducer::Release()` 中的 `WaitUntilTasksComplete` 确实会等待所有已注册到 `TranscodeCache` 的 `FTranscodeTask` 完成
- `FTranscodeTask::DoTask()` 中的 `EndTranscodeTask()` 在 `GraphEvent->DispatchSubsequents()` 之前执行
- 因此，如果存在 `FTranscodeTask`，`WaitUntilTasksComplete` 返回后，`EndTranscodeTask()` 一定已经执行完毕

**结论**：之前添加的所有防御性代码**永远不会被触发**，全部移除。

### 阶段4：重新排查——发现真正根因

**关键发现**：`GatherProducePageDataTasks` 只收集 `TranscodeCache` 中的 `FTranscodeTask`，**不收集 `FCreateCodecTask` 的 `CompletedEvent`**！

`FCreateCodecTask` 可以成为"孤儿任务"的触发条件：

1. `RequestTile()` → `GetCodecForChunk()` 创建新 Codec，启动 `FCreateCodecTask`
2. 后续 `ReadData()` 返回 `Saturated`（文件缓存饱和）
3. `RequestTile()` 提前返回，**`FTranscodeTask` 从未被创建**
4. `FCreateCodecTask` 的 `CompletedEvent` 没有注册到 `TranscodeCache` 的任何数据结构中
5. `Release()` → `GatherProducePageDataTasks` 无法收集到这个孤立任务
6. `WaitUntilTasksComplete` 不会等待它
7. `delete VirtualTexture` → `~FUploadingVirtualTexture()` → 释放 Codec
8. 工作线程的 `FCreateCodecTask::DoTask()` 通过裸指针访问已释放的 Codec → **Use-After-Free**

### 阶段5：崩溃堆栈贴合性验证

**质疑**：崩溃发生在渲染线程的 `~FUploadingVirtualTexture()` 中，而不是工作线程的 `FCreateCodecTask::DoTask()` 中，这与 Use-After-Free 分析是否矛盾？

**解释**：这是 **heap corruption 类 bug 的经典延迟表现**：

1. 渲染线程 `Codec.Reset()` 释放 Codec 对象（`delete Codec`）
2. 工作线程 `FCreateCodecTask::DoTask()` 中 `Codec->Init(HeaderData)` 写入已释放的堆内存 → **堆损坏**
3. 渲染线程继续执行隐式成员析构 → `TArray<TUniquePtr<FVirtualTextureCodec>>::~TArray()` → `DestructItems` 
4. 由于堆已被步骤2破坏，`DestructItems` 中读到非法内存值 → **崩溃**

**写脏操作和崩溃表现不在同一个线程/时间点**，这是 Use-After-Free 导致 heap corruption 的典型特征。

---

## 三、根因总结

```
根本原因：FCreateCodecTask 持有 FVirtualTextureCodec 的裸指针（Codec*），
当 ReadData 返回 Saturated 导致 FTranscodeTask 未被创建时，
FCreateCodecTask 成为"孤儿任务"——不被 GatherProducePageDataTasks 收集，
不被 Release() 的 WaitUntilTasksComplete 等待。
析构函数释放 Codec 后，工作线程的 FCreateCodecTask::DoTask() 
通过裸指针写入已释放内存，导致堆损坏，最终在渲染线程析构路径中表现为崩溃。
```

### 时序图

```
渲染线程                                    工作线程
    |                                         |
    | GetCodecForChunk() 创建新 Codec           |
    | → 派发 FCreateCodecTask(Codec*)          |
    |                                         | FCreateCodecTask 开始执行
    | ReadData() → Saturated (缓存满)           |
    | → return, 不创建 FTranscodeTask           |
    |                                         |
    | ... 某帧后 ...                            |
    |                                         |
    | Release()                                |
    |   GatherProducePageDataTasks → 空列表     |
    |   WaitUntilTasksComplete(空) → 立即返回    |
    |   delete VirtualTexture                  |
    |     ~FUploadingVirtualTexture()          |
    |       Codec->Unlink()                    |
    |       Codec.Reset() → delete Codec  ←——— | Codec->Init(HeaderData)
    |                                    ←——— | ⚠️ 写入已释放内存！堆损坏！
    |     隐式析构 CodecPerChunk                 |
    |       DestructItems → 💥 CRASH!          |
```

---

## 四、修复方案

### 修改文件
`Engine/Source/Runtime/Engine/Private/VT/UploadingVirtualTexture.cpp`

### 修改内容
在 `~FUploadingVirtualTexture()` 的 `Unlink()` + `Reset()` 循环**之前**，添加等待所有未完成 `FCreateCodecTask` 的逻辑：

```cpp
#pragma region Engine ZXB
// 等待所有未完成的 FCreateCodecTask 异步任务完成
// 原因：FVirtualTextureProducer::Release() 中的 GatherProducePageDataTasks 只收集 TranscodeCache 中的
// FTranscodeTask，不收集 FCreateCodecTask 的 CompletedEvent。当 GetCodecForChunk 创建了新 Codec 并
// 启动了 FCreateCodecTask，但后续 ReadData 返回 Saturated 导致 FTranscodeTask 未被创建时，
// Release() 的 WaitUntilTasksComplete 无法等到这个孤立的 FCreateCodecTask，
// 从而在析构时 FCreateCodecTask 仍在异步线程中通过裸指针访问已释放的 Codec，导致 Use-After-Free 崩溃
{
    FGraphEventArray PendingCodecTasks;
    for (const TUniquePtr<FVirtualTextureCodec>& Codec : CodecPerChunk)
    {
        if (Codec && Codec->CompletedEvent && !Codec->CompletedEvent->IsComplete())
        {
            PendingCodecTasks.Add(Codec->CompletedEvent);
        }
    }
    if (PendingCodecTasks.Num() > 0)
    {
        FTaskGraphInterface::Get().WaitUntilTasksComplete(PendingCodecTasks, ENamedThreads::GetRenderThread_Local());
    }
}
#pragma endregion
```

### 修复原理
- 遍历 `CodecPerChunk` 中所有非空的 Codec
- 检查 `CompletedEvent` 是否存在且未完成（即 `FCreateCodecTask` 还在异步执行中）
- 收集所有未完成的事件到 `PendingCodecTasks`
- 调用 `FTaskGraphInterface::Get().WaitUntilTasksComplete()` 阻塞等待
- 等待完成后，才执行后续的 `Unlink()` + `Reset()` 释放 Codec 对象
- 从源头阻断堆损坏，消除 Use-After-Free 风险

---

## 五、关键经验教训

1. **异步任务的生命周期管理必须完整覆盖**：`GatherProducePageDataTasks` 只收集 `TranscodeTask`，遗漏了 `FCreateCodecTask`。在设计异步系统时，所有可能持有资源引用的任务都必须被纳入生命周期管理。

2. **Shipping 版本的特殊性**：
   - `check` / `checkf` 在 Shipping 版本中被编译掉，无法作为运行时保护
   - 符号还原可能有误差（如 `DestructItems<TUniqueObj<Chaos::FBoxFloat3>,int>` 实际是 `TUniquePtr<FVirtualTextureCodec>` 的析构）

3. **Use-After-Free 的延迟表现**：堆损坏类 bug 的写脏操作和崩溃表现往往不在同一个线程/时间点。需要从堆栈逆向推理内存损坏的来源，而不是只关注崩溃点本身。

4. **边界条件的重要性**：`ReadData` 返回 `Saturated` 是一个低概率但确实存在的边界条件。正是这种边界条件导致了"孤儿任务"的产生。

5. **深度分析需要反复迭代**：本次修复经历了"添加防御性代码 → 证明不会触发 → 全部移除 → 重新发现真正根因 → 精准修复"的完整迭代过程。初始假设的错误不代表方向错误，关键是持续深入分析直到找到真正的根因。
