# RDG TransientAllocator ParallelResourceCreation 切核问题分析与优化

> 相关飞书文档：https://sarosgame.feishu.cn/wiki/R3GewO5Zvia5NWkIqRkco57Pn9f

## 一、背景

在 Profiler 中观察到 `AllocatePlacedTexture`、`CreatePlacedResource`、`CreateD3D12Texture` 等操作在多个 CPU 核心之间频繁跳跃执行（切核），导致额外的性能开销。

涉及的核心 CVar：`RHI.TransientAllocator.ParallelResourceCreation`

- 文件位置：`Engine/Source/Runtime/RHICore/Private/RHICoreTransientResourceAllocator.cpp`
- 原始默认值：`true`

## 二、关键代码流程

### 2.1 PrepareCollectResourcesTask

位于 `Engine/Source/Runtime/RenderCore/Private/RenderGraphBuilder.cpp`，通过 `AddSetupTask` 创建异步任务，为后续资源收集做准备：

- **遍历所有 Buffer**：重置 `LastPasses`、标记是否需要分配（`bCollectForAllocate`）、标记是否为瞬态资源（`bTransient`）
- **遍历所有 Texture**：同上逻辑

### 2.2 FResourceTask（瞬态资源异步创建）

位于 `Engine/Source/Runtime/RHI/Public/RHITransientResourceAllocator.h`，类型为 `UE::Tasks::TTask<FResourceTaskResult>`。

- 当 `ParallelResourceCreation` 开启时，每个瞬态资源的 RHI 底层创建（`CreatePlacedResource`）被包装为独立的 `UE::Tasks::Launch` 高优先级任务
- 任务完成后通过 `Finish()` 方法同步等待并提取结果

### 2.3 AllocateTransientResources 调度流程

在 `RenderGraphBuilder.cpp` 的 `AllocateTransientResources` 中：

1. 循环 Launch N 个独立 Task（每个资源一个）
2. 所有 Task 无依赖关系，同时变为 Ready 状态
3. 末尾逐一调用 `Finish()` 等待所有 Task 完成

## 三、切核原因分析

### 3.1 原因一：Task 粒度过细

每个 Placed Resource 创建都是一个独立 Task，单个 Task 工作量很小（仅一次 D3D12 API 调用，通常几十到几百微秒），但 Task 调度本身需要线程唤醒、上下文切换、CPU 缓存预热等开销，调度开销占比过高。

### 3.2 原因二：D3D12 驱动三层锁叠加

并行调用 `CreatePlacedResource` 时存在三层锁竞争：

#### UE 引擎层锁
| 锁 | 位置 | 作用 |
|---|---|---|
| `FD3D12OfflineDescriptorManager` 的 `FCriticalSection` | 描述符管理器 | 创建纹理时分配离线描述符 |
| `FD3D12OnlineDescriptorManager` 的 `FCriticalSection` | 描述符管理器 | 在线描述符分配 |
| `ResourceAllocationInfoMap` 的 `FRWLock` | 资源分配 | 资源分配信息缓存 |
| `FD3D12DescriptorHeapManager::PooledHeapsCS` | 堆管理器 | 描述符堆池管理 |

#### D3D12 Runtime 层隐式锁
- `ID3D12Device::CreatePlacedResource` 内部持有**设备级互斥锁**
- 保护资源注册表、Heap 引用计数和 GPU 虚拟地址空间映射
- 导致多个线程的 `CreatePlacedResource` 调用被串行化

#### GPU 驱动层（UMD）隐式锁
- GPU 虚拟地址分配器全局锁
- 页表映射锁
- 资源追踪表锁
- 内存管理器锁

#### Lock Convoy（锁护送）问题

三层锁叠加导致完全串行化，产生 Lock Convoy 问题：
- 多个线程获取锁的顺序变成"排队传递"
- 每次锁转移伴随上下文切换和核心迁移
- 线程越多性能越差，比单线程更慢

### 3.3 原因三：同步等待模式

`Finish` 阶段渲染线程逐一 `Wait` 每个 Task，产生额外的线程唤醒和调度。大量 Task 同时 Ready 又同时被等待，放大了调度压力。

## 四、优化方案

### 采用方案：关闭并行资源创建（切换为单线程模式）

**修改文件**：`Engine/Source/Runtime/RHICore/Private/RHICoreTransientResourceAllocator.cpp`

**修改内容**：将 `GRHITransientAllocatorParallelResourceCreation` 默认值从 `true` 改为 `false`

**效果**：
- Transient 资源的 `CreatePlacedResource` 以 Inline（同步）模式在调用线程上直接执行
- 避免 D3D12 Runtime Device Mutex 的 Lock Convoy 问题
- 消除大量短生命周期 Task 带来的调度开销和切核消耗

**可逆性**：CVar 仍保留为 `FAutoConsoleVariableRef`，可在运行时通过控制台命令切回并行模式进行对比测试：
```
RHI.TransientAllocator.ParallelResourceCreation 1
```

### 其他备选方案（未采用）

| 方案 | 说明 |
|---|---|
| 批量合并 Task | 将多个 CreatePlacedResource 合并到少数几个 Task 中，按 Heap 分组 |
| 设置线程亲和性 | 为 Worker 线程设置 CPU 亲和性，避免 OS 频繁迁移线程 |
| 调整 Task 优先级 | 降低优先级避免所有 Task 同时抢占核心 |

## 五、验证建议

1. 修改后进行 Profiler 对比，观察切核现象是否消除
2. 关注 `AllocateTransientResources` 整体耗时变化（理论上单线程消除调度开销后总耗时可能更低）
3. 在资源创建数量较多的场景（如大量瞬态纹理的复杂渲染管线）中重点测试