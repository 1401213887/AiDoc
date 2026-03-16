# VirtualTexture 崩溃分析与修复总结

## 一、崩溃概览

| 项目 | 内容 |
|------|------|
| **异常线程** | GameThread 6228 |
| **异常类型** | EXCEPTION_ACCESS_VIOLATION_READ |
| **崩溃地址** | 0xa00000003 |
| **崩溃汇编** | `cmp byte ptr [r9 + 3], 0xe3` |
| **触发时机** | 地图切换（LoadMap）过程中执行 GC（CollectGarbage）时 |

## 二、崩溃调用栈（关键帧）

```
FMallocBinned2::Free(void*)                              ← 崩溃点：canary 校验失败
  └─ FMemory::Free(void*)
      └─ FVirtualTextureBuiltData::~FVirtualTextureBuiltData()
          └─ TReferenceControllerWithDeleter::DestroyObject()
              └─ FSharedReferencer::operator=(FSharedReferencer&&)
                  └─ FTexturePlatformData::~FTexturePlatformData()
                      └─ UTexture::FinishDestroy()
                          └─ UObject::ConditionalFinishDestroy()
                              └─ IncrementalDestroyGarbage()
                                  └─ IncrementalPurgeGarbage()
                                      └─ CollectGarbage()
                                          └─ UEngine::TrimMemory()
                                              └─ UEngine::LoadMap()
```

## 三、根因分析

### 3.1 核心问题：异步生命周期断裂

代码中将 `FTexturePlatformData` 的 `VTData` 成员从裸指针升级为 `TSharedPtr<FVirtualTextureBuiltData>`，但在**转码任务参数传递**时又降级回裸指针，导致异步任务在 GC 释放对象后仍然访问悬空内存。

### 3.2 问题链路

```
┌─────────────────────────────────────────────────────────────────────┐
│ VirtualTextureChunkManager.cpp::RequestTile()                       │
│                                                                     │
│   TSharedPtr<FVirtualTextureBuiltData> VTData = VTexture->GetVTData()│
│                        │                                            │
│                        ▼                                            │
│   TranscodeParams.VTData = VTData.Get()  ← 降级为裸指针！           │
│                        │                                            │
│                        ▼                                            │
│   TranscodeCache.SubmitTask(... TranscodeParams ...)                │
│                        │                                            │
│                        ▼                                            │
│   FTranscodeTask 拷贝 Params（仅拷贝裸指针，无引用计数）             │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.3 崩溃时序

```
时间线 ──────────────────────────────────────────────────────────►

[RenderThread]  提交 FTranscodeTask，Params 中持有 VTData 裸指针
                                    │
[TaskGraph]                         │  任务排队等待执行
                                    │
[GameThread]  LoadMap → TrimMemory → CollectGarbage
              → UTexture::FinishDestroy
              → FTexturePlatformData 析构
              → VTData TSharedPtr Reset（引用计数归零）
              → FVirtualTextureBuiltData 被释放                ← 内存已释放
                                    │
[TaskGraph]   任务执行，访问 Params.VTData->（已悬空）          ← 堆元数据被破坏
                                    │
[GameThread]  FMallocBinned2::Free 检测到 canary 异常           ← 崩溃！
              cmp byte ptr [r9 + 3], 0xe3（0xe3 = EBlockCanary::Value）
```

### 3.4 为什么崩溃发生在 Free 而非任务中

异步任务通过悬空指针读取脏数据后，可能发生越界拷贝，先破坏了堆的元数据（block header/canary）。真正检测到异常的时刻是后续某次 `Free` 操作做 canary 校验时。这就是汇编指令 `cmp byte ptr [r9 + 3], 0xe3` 的含义——检查内存块完整性标记是否为 `0xe3`。

## 四、涉及文件

| 文件 | 角色 |
|------|------|
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureTranscodeCache.h` | 转码参数结构定义（`FVTTranscodeParams`） |
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureTranscodeCache.cpp` | 转码任务执行（`FTranscodeTask`） |
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureChunkManager.cpp` | 请求瓦片（`RequestTile`），参数组装 |
| `Engine/Source/Runtime/Engine/Private/TextureDerivedData.cpp` | `FTexturePlatformData` 析构 |
| `Engine/Source/Runtime/Engine/Private/Texture.cpp` | `UTexture::FinishDestroy` |

## 五、修复方案

### 5.1 P0 修复（已完成）：让异步任务强持有 VTData

#### 变更 1：VirtualTextureTranscodeCache.h — 新增头文件引用

```diff
  #include "VirtualTextureUploadCache.h"
  #include "PixelFormat.h"
  #include "Misc/MemoryReadStream.h"
  #include "Containers/HashTable.h"
+ #include "Templates/SharedPointer.h"
```

#### 变更 2：VirtualTextureTranscodeCache.h — VTData 类型改为 TSharedPtr

```diff
  struct FVTTranscodeParams
  {
      IMemoryReadStreamRef Data;
      const FVirtualTextureCodec* Codec;
-     const FVirtualTextureBuiltData* VTData;
+     TSharedPtr<const FVirtualTextureBuiltData> VTData;
      ...
  };
```

#### 变更 3：VirtualTextureChunkManager.cpp — 传递 TSharedPtr 而非裸指针

```diff
  #pragma region Engine ZXB
-     TranscodeParams.VTData = VTData.Get(); // By ZXB
+     TranscodeParams.VTData = VTData; // TSharedPtr 强持有，防止 GC 期间悬空
  #pragma endregion
```

### 5.2 修复原理

修复后，`FTranscodeTask` 在拷贝 `Params` 时会使 `TSharedPtr` 引用计数 +1。即使 GC 期间 `UTexture::FinishDestroy` 触发 `FTexturePlatformData` 析构导致其内部的 `VTData` 引用计数 -1，由于异步任务仍持有一份强引用，总引用计数不会归零，`FVirtualTextureBuiltData` 不会被提前释放。任务完成后 `FTranscodeTask` 析构，引用计数再 -1 归零，此时才安全释放内存。

### 5.3 类型兼容性确认

UE 的 `TSharedPtr` 模板构造函数使用了 `std::is_convertible_v<OtherType*, ObjectType*>` 约束。由于 `FVirtualTextureBuiltData*` 可隐式转换为 `const FVirtualTextureBuiltData*`，因此 `TSharedPtr<FVirtualTextureBuiltData>` 到 `TSharedPtr<const FVirtualTextureBuiltData>` 的赋值完全合法。

`TSharedPtr` 的 `operator->` 返回裸指针，因此 `VirtualTextureTranscodeCache.cpp` 中所有 `Params.VTData->XXX` 的访问代码**无需修改**，完全兼容。

## 六、P1 建议（待评估）

### FindTask 中未比较 LayerMask

`VirtualTextureTranscodeCache.cpp` 的 `FindTask` 函数仅比较 `Task.Key == InKey.Key`，而 `LayerMask` 已从 Key 中拆出。在 16 位 hash 冲突场景下，可能把不同 `LayerMask` 的请求错误复用为同一任务。

**建议**：补齐 `LayerMask` 的比较条件，避免错误复用。

## 七、验证建议

1. **回归测试**：在地图切换场景（尤其是含大量虚拟纹理的关卡）反复进行 LoadMap，观察是否仍有崩溃
2. **压力测试**：在低内存条件下强制触发 GC（`obj gc` 控制台命令），同时确保有虚拟纹理转码任务在排队
3. **内存检测**：在 Development 构建中启用 `MallocStomp` 或 Address Sanitizer 进行更早期的悬空指针检测
