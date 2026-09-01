# UE Android 启动崩溃：Vulkan Chunked PSO Cache × validation layer 组合必崩

> MI 8（SDM845 / Adreno 630）上 UE 5.5.4 定制引擎（++GR+DevTest）的 Android Development 包，每次启动约 3 秒后 `RHIThread` SIGSEGV，必须手工删除 `RHICache` 才能启动一次。根因是 **Vulkan chunked PSO 缓存复用与 validation layer 的组合**触发高通驱动崩溃，二者缺一不崩。

---

## 一、问题定位流程（确认了什么）

设备实测（logcat + tombstone）：

- 崩溃发生在 PSO 缓存预编译阶段，栈顶 UE 符号为 `FVulkanPipelineStateCacheManager::OnShaderPipelineCacheOpened+536`，但真正崩在**高通驱动的 `vkCmdBindPipeline`，经 validation layer 拦截**——`OnShaderPipelineCacheOpened` 只是调用者，不是故障点。
- tombstone `code -6 (SI_TKILL)` = 进程**主动 tkill 自杀**（断言/fatal 路径），不是野指针访问（那会是 `SEGV_MAPERR`）。

隔离实验矩阵（同一台设备、同一份 APK，每轮只改一个变量）：

| chunked PSO cache | validation layer | 结果 |
|---|---|---|
| 开（cvar=1） | 开 | **崩** |
| 开（cvar=1） | 关（系统级 `gpu_debug_layers`） | 不崩 |
| **关（cvar=0，ini）** | 开 | **不崩**（用户验证） |

→ **缺一不崩**：`chunked 开 + validation 开` 才是崩溃的必要组合。

## 二、根因分析

1. **两套独立 PSO 缓存系统**：`FShaderPipelineCache`（cooked `.upipelinecache`，触发 `OnShaderPipelineCacheOpened`）与 Vulkan chunked PSO 缓存（`VulkanPSOChunks` 文件，`r.Vulkan.UseChunkedPSOCache`，Android 默认开）。
2. **崩溃机制**：chunked 缓存 PSO 的复用/反序列化路径 + validation layer 拦截的组合，触发高通驱动 `vkCmdBindPipeline` 崩溃。实时编译的 PSO 不会。
3. **双重故障叠加**：崩溃 → 进程被杀 → 残留截断的 `VulkanPSOChunks` → 下次启动再撞缓存文件校验断言 → "每次都要删 RHICache 才能不崩"。
4. **已知历史印证**：引擎自带 `// @yixing crash fix` 把 `r.PSOPrecaching` 默认值改为 0（`PipelineStateCache.cpp:279`），说明该引擎此前就有 precompile 崩溃史，靠默认关预缓存规避。

## 三、关键结论：`-dpcvars=` 在本引擎是死代码

- 解析代码在 `SetDeviceAndGraphicAndLogicProfileCVars()`（`DeviceProfileManager.cpp:627-656`，`#if !UE_BUILD_SHIPPING`），**全引擎零调用点**（全局 grep 只有 .h 声明 + .cpp 定义）。`-dpcvars=` 永远不会执行，**任何 cvar 都改不了，与 `ECVF_ReadOnly` 无关**。
- 实测闭环：命令行带 `-dpcvars=r.Vulkan.UseChunkedPSOCache=0` → 日志仍 `loading VulkanPSOChunks`，照崩。

**实际有效的 cvar 覆盖通道**：

| 通道 | 效果 | 说明 |
|---|---|---|
| **ini `[ConsoleVariables]`**（`ECVF_SetBySystemSettingsIni`） | ✅ 有效 | engine ini 加载必然应用；改 ReadOnly 也能设 |
| 控制台 / `-ExecCmds` | 改 ReadOnly 无效 | `ConsoleManager.cpp:2860` 报 "is read only!" |
| `-vulkandebug` 等专用开关 | 只改自己那个 | `VulkanLayers.cpp:315` 用 `ECVF_SetByCommandline` |
| `-dpcvars=` | ❌ 无效 | 死代码 |

排查 cvar 不生效时：先 grep cvar 注册处看 `ECVF_ReadOnly` → 再 grep 谁抢设（`ECVF_SetByCommandline`）→ 最后确认 ini 段真进了 cooked 配置（设备上 `Saved/Temp/Android/.../AndroidEngine.ini` 或日志 `Applied changed CVAR value`）。

## 四、修复方案

### 4.1 立即止血（已验证）

在打包会合入的 ini 加 `[ConsoleVariables]`：

```ini
[ConsoleVariables]
r.Vulkan.UseChunkedPSOCache=0
```

重新打包 → 不崩。代价是丧失 chunked PSO 缓存收益（启动/运行时 PSO 命中变慢），可接受。

### 4.2 或关闭 validation layer（系统级，不改包）

Android 系统开关强制 app 只加载指定 Vulkan layer（关掉打包进 APK 的 khronos validation）：

```bash
adb shell settings put global enable_gpu_debug_layers 1
adb shell settings put global gpu_debug_app com.YourCompany.BulletTest
adb shell settings put global gpu_debug_layers VK_LAYER_RENDERDOC_Capture
```

副作用：开关 layer 改变 `pipelineCacheUUID`，RHICache 版本化子目录名变化，旧缓存自动废弃重建——这是 `VulkanChunkedPipelineCache.cpp:144-174` 的设计，不是异常。

### 4.3 打包细节：`-dpcvars` 与 `-cmdline` 的正确用法

- **`-cmdline=`** 是 UAT 把参数写进 APK 内 `UECommandLine.txt` 的唯一通道（`ProjectParams.cs:946` → `WriteStageCommandline`）。设备上 UE 打印 `Using APK commandline`，**只读 APK 内的命令行，sdcard 推送的 UECommandLine.txt 无效**。
- `Binaries/Android/UECommandLine.txt` 是手工文件，**不参与打包**，改它无效。

## 五、快速排查 Checklist

1. 崩没崩？抓 logcat：`adb logcat -c && adb shell monkey -p <pkg> 1`，等 10s 看 `Fatal signal 11` / `Tombstone written`。
2. 栈顶符号是谁？tombstone 在 `/data/tombstones/`；栈顶 UE 符号往往只是调用者，往**驱动 + layer 层**看。
3. 是断言自杀还是野指针？`code -6 (SI_TKILL)` = 断言/fatal；`SEGV_MAPERR` = 野指针。
4. 是不是 chunked cache + validation 组合？逐项关掉做矩阵验证（见 §一）。
5. cvar 改不生效？先 grep 注册处看 `ECVF_ReadOnly`，再看改动通道是否有效（§三的表）。
6. 参数进没进包？logcat 搜 `Final commandline`，与打包侧 `-cmdline` 比对。
7. 验证结果以设备日志为准，别信 `dpcvars`（死代码）。

## 六、相关参考

- 引擎源码：
  - `Engine/Source/Runtime/VulkanRHI/Private/VulkanChunkedPipelineCache.cpp`（缓存文件校验、`check(Archive.Tell()==LastValidOffset)` 断言、RHICache 路径构成）
  - `Engine/Source/Runtime/VulkanRHI/Private/VulkanLayers.cpp:315`（`-vulkandebug` → `r.Vulkan.EnableValidation`）
  - `Engine/Source/Runtime/Core/Private/Misc/DeviceProfileManager.cpp:627-656`（`-dpcvars` 死代码解析处）
  - `Engine/Source/Runtime/RenderCore/Private/PipelineStateCache.cpp:279`（`// @yixing crash fix` r.PSOPrecaching 默认 0）
  - `Engine/Source/Programs/UnrealBuildTool/Project/ProjectParams.cs:946`、`.../Platform/Windows/CopyBuildToStagingDirectory.Automation.cs:5463`（`-cmdline` → staged 命令行）
  - `Engine/Source/Runtime/Engine/Private/Android/AndroidPlatformFile.cpp:1398-1405`（log 目录 mkdir 用 `GExternalFilePath`）
- 相关文档：`E:\AiDoc\UE-Android-无日志-ABSLOG-CWD相对路径.md`（同一次打包排障的姊妹问题）
