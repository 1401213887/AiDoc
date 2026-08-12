# UE-Insights-Queue-Present耗时定位-GPU瓶颈判断

> UE Insights GPU track 中 `Queue Present 25.4ms` ≈ `Frame 25.9ms`，且帧率不固定（排除 vsync），判定 Queue Present 不是在做事，而是在排队等 GPU 干完 —— 真正瓶颈是 GPU 渲染管线帧时间本身。

---

## 一、问题定位流程

1. **截图 OCR**（ZXB skill ⑬，PaddleOCR v6 + `--x2` 双轮交叉验证）：提取到 UE Insights GPU frame track（Frame 6628）四条时间标签，置信度全部 ≥0.94：
   | 事件 | 耗时 |
   |---|---|
   | ExecuteTask | 26.2 ms |
   | RHI_Translate | 25.9 ms |
   | Frame 6628 | 25.9 ms |
   | Queue Present | 25.4 ms |

2. **关键观察**：四个时长几乎相等（`26.2 ≈ 25.9 ≈ 25.9 ≈ 25.4`），形成串行依赖链，无任何多余时间 → 不是 Queue Present 单独有问题，是同一根瓶颈的镜像。

3. **源码定位**："Queue Present" 字符串全引擎只出现在 VulkanRHI（截图对应 Vulkan 引擎）：
   - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanRHIPrivate.h:432` — `DECLARE_CYCLE_STAT_EXTERN(TEXT("Queue Present"), STAT_VulkanQueuePresent, STATGROUP_VulkanRHI)`
   - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanSwapChain.cpp:848-877` — scope 实际位置
   - `UE5EA/Engine/Source/Runtime/RHI/Private/RHICommandList.cpp:2086` — `FRHICommandListImmediate::EndDrawingViewport` 上层入口

## 二、根因分析

`VulkanSwapChain.cpp:848-877` 中 `STAT_VulkanQueuePresent` scope 的真实内容：

```cpp
{
    SCOPE_CYCLE_COUNTER(STAT_VulkanQueuePresent);   // 就是这个 cycle stat
    VkResult PresentResult;
    {
        FRenderThreadIdleScope IdleScope(ERenderThreadIdleTypes::WaitingForGPUPresent);   // 引擎官方"在等 GPU Present"标记
        PresentResult = FVulkanPlatform::Present(PresentQueue->GetHandle(), Info);        // 实际调 vkQueuePresentKHR
    }
    CurrentImageIndex = -1;
    // ...错误判断（OutOfDate / SurfaceLost 等）
}
```

- `FVulkanPlatform::Present` → `VulkanAndroidPlatform.cpp:703` / `VulkanGenericPlatform.cpp:118` → `VulkanCommandWrappers.h:1412-1418` 的 `vkQueuePresentKHR` wrapper。
- 上层 `EndDrawingViewport`（RHICommandList.cpp:2086）先强制 `ImmediateFlush(EImmediateFlushType::DispatchToRHIThread)` 把 graphics + async compute 工作全部提交，再走 Present（L2088-2091 注释明确：platform RHI 在 present 时可能额外提交工作，必须先保证 async 不 deadlock）。

**判定逻辑（核心）**：
- 帧率**不固定** → 排除 vsync / frame pacing 锁帧 → Present 返回的唯一条件是 GPU 把活干完（waitSemaphore/fence 满足）。
- `Queue Present 25.4ms ≈ Frame 25.9ms` → CPU 在 `vkQueuePresentKHR` 内部被**同步阻塞**，等 GPU fence 释放 back buffer；25.4ms 就是"排队等 GPU 干完"的时间，与 GPU 帧时间几乎相等完全吻合。
- scope 内嵌 `FRenderThreadIdleScope(ERenderThreadIdleTypes::WaitingForGPUPresent)` 是引擎自己承认"这就是在等 GPU Present"，不是做事。

## 三、详细技术原理

**为什么 Queue Present 能占一整帧时间**
- 移动端（Vulkan）GPU 帧时间 ~26ms 已是瓶颈（GPU bound）。CPU 翻译提交完命令后，走到 Present 时 GPU 尚未画完。
- `vkQueuePresentKHR` 在驱动层等待 waitSemaphore（`BackBufferRenderingDoneSemaphore`）→ 该 semaphore 由 GPU 完成渲染后 signal → CPU 阻塞在此直至 GPU 帧结束。
- 时间递减关系 `ExecuteTask 26.2 > RHI_Translate 25.9 > Frame 25.9 > Present 25.4` 是典型串行依赖链：CPU 翻译提交 → GPU 执行 → CPU 等 GPU 结束才 Present 返回。**没有一处多余时间，全是同一条 GPU 瓶颈的镜像。**

**两个注意点（避免误判）**
1. **不是 vsync 的证据**：帧率不固定（帧时间随场景负载浮动）说明瓶颈在 GPU 计算本身；若是 vsync 锁帧，帧时间会稳定在固定周期。
2. **Queue Present 一分钱优化空间都没有**：它只是排队等 GPU，优化它没有意义；必须展开 Frame 内部找 GPU pass 热点。

## 四、修复方案 / 优化方向

**降帧核心在 GPU 渲染管线，不在 Present**。下一步排查：

1. **展开 GPU track 看 pass 热点**：Insights 中展开 Frame 的 GPU 段，看 RHI_Translate 内部 Render Pass 时间分布（BasePass / Shadow / PostProcess / LuxGI 哪一段最重）。
2. **查帧率与锁帧配置**：`t.MaxFPS` / `r.Vulkan.SetFramesInFlight` / DeviceProfile 的 FramePace 设置（确认设备实际帧率策略）。
3. **查 swap chain image count**：Triple Buffering 资源周转是否足够（`r.Vulkan.RHIThread` / `rhi.SyncInterval` 路径）。

## 五、快速排查 Checklist

| 序 | 查什么 | 怎么查 | 判定 |
|---|---|---|---|
| 1 | Queue Present 是否在做事 | 看 `STAT_VulkanQueuePresent` scope 源码（VulkanSwapChain.cpp:848-877） | scope 只有 vkQueuePresentKHR + 错误判断 = 在等不在做 |
| 2 | 是否 vsync 锁帧 | 帧率是否固定 | 帧率浮动 = 不是 vsync |
| 3 | GPU 是否瓶颈 | 对比 Queue Present 与 Frame 耗时 | 近似相等 = GPU bound |
| 4 | 热点 pass 在哪 | Insights 展开 GPU track / RenderDoc 抓帧 | 定位 BasePass/Shadow/PP/LuxGI |

## 六、相关参考

- ZXB skill：`C:/Users/djangozhang/.workbuddy/skills/ZXB/SKILL.md`（⑬ OCR + ⑫ 一键取数，性能分析配套）
- 引擎源码（本项目 fork）：
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanSwapChain.cpp`（Present 实现）
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanRHIPrivate.h`（cycle stat 定义）
  - `UE5EA/Engine/Source/Runtime/RHI/Private/RHICommandList.cpp`（EndDrawingViewport / RHI_Translate）
- 历史性能分析报告：`E:\AiReport\`（TDM8Gen3 / FateTrigger 帧性能分析、CPU 批量报告、机型分档报告）
