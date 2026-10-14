# UE-Android-Vulkan启动后确定性闪黑-DynamicUBO与InputAttachment同DescriptorSet修复

> BulletTest（UE5.5 fork，Mobile Deferred SinglePass + Vulkan subpass）启动后运行期出现确定性全屏纯黑闪帧（启动黑屏结束后 +3.80s 两帧黑 / +7.73s 一至两帧黑，5/5 轮 100% 复现）。根因：Vulkan RHI 的 descriptor set 恒等于 shader stage（set≡stage），subpass 路径的 dynamic UBO 与 input attachment 被结构性塞进同一个 set，在 Adreno630 驱动上触发整帧黑。最终修复：**高通驱动 < 440 定向关闭** `r.Vulkan.DynamicGlobalUBs=0`（DeviceProfile 匹配规则，440+ 驱动保持默认优化）+ 引擎哨兵越界防护（CL 1119465），修复后多轮零闪黑。

---

## 一、问题定位流程

### 1. 闪黑复现与基线固化（录屏 + 帧级黑帧检测）

用 `adb shell screenrecord` 重启模式录屏（覆盖完整启动过程），`ffmpeg blackdetect` 帧级扫描 + 窗口连抽法数值验证（灰度 mean<8 且 std<6 判纯黑），5 轮全复现：

| 项 | 值 |
|---|---|
| 闪黑 1 | 启动黑屏结束后 **+3.80s±0.03s**，2 帧黑（~67ms） |
| 闪黑 2 | 启动黑屏结束后 **+7.73s±0.08s**，1-2 帧黑（34-50ms） |
| 黑帧特征 | 灰度 mean=0.1 / std=1.5，**纯黑连 HUD 都没有** |
| 多轮偏移一致性 | ±0.03s 内完全一致 → **确定性事件**，排除偶发丢帧 |

分析脚本已固化为 skill：`C:/Users/djangozhang/.tclaude/skills/BulletTest/scripts/analyze_flicker.py`（用法 `python analyze_flicker.py <video.mp4> --save-frames`）。

### 2. 管线坐标系确认（当前包跑的是什么）

修复前必须先钉死管线，避免对错渲染路径做分析：

- 配置：`DefaultEngine.ini` `r.Mobile.ShadingPath=1`（枚举 `Forward=0 / Deferred=1`，`RendererSettings.h:214-222`）、`r.MobileHDR=True`
- 平台门控：`IsMobileDeferredShadingEnabled = MobileDeferredShading(Platform) && IsMobileHDR()`（`RenderUtils.h:322`）；OpenGL 被 `AllowDeferredShadingOpenGL=0` 拦截，**Vulkan 放行**
- 运行日志签名：`Set CVar [[r.Mobile.ShadingPath:1]]`、`MobileDeferredLightingSplitPass changed (0 -> 1)`、全局 shader `FMobileDeferredCopyDepthPS/FMobileDeferredCopyPLSPS` 存在、`VULKAN_ES3_1_ANDROID`
- → 结论：**Mobile Deferred，ES3_1 Vulkan，SinglePass 走 subpass**（`MobileShadingRenderer.cpp:2467` `RenderDeferredSinglePass`，L2538 `ESubpassHint::DeferredShadingSubpass`）

### 3. 崩溃副线（=0 触发新崩溃）的定位

`DynamicGlobalUBs=0` 装包后每次启动必崩（3/3），`SIGSEGV code -6 (SI_TKILL)`（进程自 raise）、RHIThread、引擎初始化完成 +0.4s 第一帧。logcat 符号栈自相矛盾（strip 后错位），从 APK 提取 522MB 未 strip 的 `libUnreal.so`，用 NDK llvm-symbolizer 精确符号化（**pc 需 +0x10000 修正 vaddr**，见第五节 Checklist），真实调用链：

```
#00 FVulkanCommandListContext::RHISetUniformBufferDynamicOffset(unsigned char, unsigned int)
#01 FRHICommand<FRHICommandSetUniformBufferDynamicOffset...>::ExecuteAndDestruct
#02 FRHICommandListBase::Execute()
```

## 二、根因分析

### 闪黑主根因：set≡stage 结构性硬绑定

UE Vulkan RHI（非 bindless 路径）把 descriptor set 编号硬绑到 shader stage，导致 dynamic UBO 与 input attachment 不可能分开：

| 层 | 证据 | 位置 |
|---|---|---|
| **Shader 编译期** | `DescSetNo = StageIndex`，`ChangeDescriptorSetNumber(DescSet, DescSetNo)` 把整个 stage 的**所有** descriptor 强制统一到同一个 set | `Developer/VulkanShaderFormat/Private/SpirVShaderCompiler.inl:506-519` |
| **RHI layout 构建期** | `AddDescriptor(Stage, Binding)`；`StageInfos[DescriptorSetIndex]` —— set index ≡ stage index | `Runtime/VulkanRHI/Private/VulkanShaders.cpp:816/835/848`、`VulkanRHI.cpp:1806` |
| **input attachment 绑定** | 恒在 `ShaderStage::Pixel` 的 set | `Runtime/VulkanRHI/Private/VulkanPendingState.cpp:559/563` |

而 dynamic UBO 由 `r.Vulkan.DynamicGlobalUBs` 控制（`VulkanShaders.cpp:16-23`，默认 **2 = ALL uniform buffers as dynamic**，`ECVF_ReadOnly` 启动期定死）：

| 类别 | 转换条件 | 运行时写入路径 |
|---|---|---|
| A. Packed globals（binding 0 `$Globals`） | `bConvertPackedUBsToDynamic`（CVar≥1 即转） | `SubmitPackedUniformBuffers<全局CVar模板>`（`VulkanPipelineState.h:332-344`），**硬编码 binding 0，不查 layout** |
| B. 普通 UB（View/MobileBasePass/ReflectionCapture…） | `bConvertAllUBsToDynamic`（CVar>1，受 `maxDescriptorSetUniformBuffersDynamic` 限） | `VulkanCommands.cpp:1006-1016`，**per-binding 查 `GetDescriptorType()` 自动跟随 layout** |

→ SinglePass 的 deferred lighting PS（Pixel stage set）里，dynamic UBO 与 input attachment **100% 同在 set 1**，在 MI8/Adreno630 上触发驱动异常，表现为 swapchain 整帧黑（黑帧 mean=0.1 纯黑无 UI，无任何 HUD 残留）。

### 崩溃副根因：哨兵 255 越界写（Epic 原生缺陷）

`DynamicGlobalUBs=0` 后暴露的引擎缺陷，机制链：

1. `VulkanPipelineState.cpp:92`：`BindingToDynamicOffsetMap` 初始化**全填哨兵 255**（`memset 255`；`Types.Num() < 255` 的 checkf 保证 255 可作哨兵）
2. `=0` → descriptor layout 无任何 `VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC` → map 值恒为 255
3. Mobile Deferred 渲染器仍会下发 `SetUniformBufferDynamicOffset` 命令（global UB 偏移更新，`VulkanCommands.cpp:973`）
4. `VulkanPipelineState.h` `SetUniformBufferDynamicOffset`：`DynamicOffsets[255] = DynamicOffset` —— **数组实际 0 个元素，越界写 1KB** → RHIThread 崩溃

该缺陷在默认值 2 下永不触发（layout 有 dynamic binding，map 值合法），只有切到 0 且渲染器走 global UB dynamic offset 更新路径（Mobile Deferred SplitPass）才会命中。

## 三、详细技术原理

### 两方案成本对比（为何选"=0"而非"拆 set"）

**方案 2（新建 descriptor set 分开 dynamic UBO 与 input attachment）——伤筋动骨**：

- `SpirVShaderCompiler.inl` 从"整 stage 一个 set"改为 per-binding 分 set（**SPIR-V set number 变化 → 全量重编 shader + PSO cache 全废**）
- `FVulkanShaderHeader` 需新增每 binding 的 set 归属
- `AddDescriptor`/`StageInfos`/`DSWriter[Stage]` 的 set≡stage 假设全线推翻
- `VulkanPipelineState.h:171-186` `Bind()` 靠 `UsedSetsMask` 计算**连续区间**（FirstSet/NumSets）绑定，拆 set 后需重设计
- 额外占用 `maxBoundDescriptorSets` 预算（移动端常见 4，已用 Vertex/Pixel）

**方案 1（`r.Vulkan.DynamicGlobalUBs=0`，dynamic → 普通 UBO）**：一行 ini，SPIR-V 的 set/binding 编号不变（仅 descriptor type 变），**无需重 cook**。代价：暴露哨兵越界缺陷（需 8 行 guard 修复）+ packed globals 失去 dynamic offset 免重写优化（有 `UseVulkanDescriptorCache()` 兜底，实测无可见回退）。

### 为什么 `=1` 无效

`bConvertPackedUBsToDynamic = (CVar >= 1)`：`=1` 时 packed globals（binding 0）**仍是 dynamic**，仍与 input attachment 同 set，问题原样保留。**必须 `=0`**。

### guard 的语义安全性论证

非 dynamic layout 下忽略 `SetUniformBufferDynamicOffset` 命令不丢数据：

- 普通 UB：每次 `RHISetShaderUniformBuffer` → `SetUniformBuffer<false>` → `WriteUniformBuffer(BindingIndex, ..., UniformBuffer->GetOffset(), Range)` —— offset 随每次绑定写入 descriptor
- Packed globals：`SubmitPackedUniformBuffers<false>` → `WriteUniformBuffer(0, ..., TempAllocation.Offset, TempAllocation.Size)` —— 同样带 offset
- dynamic offset 命令只是 dynamic layout 的**免重写 descriptor 优化路径**，非 dynamic 路径每次绑定时已写入，忽略无损

## 四、修复方案

### 改动 1：引擎哨兵 guard（CL 1119465）

`UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanPipelineState.h`：

```cpp
inline void SetUniformBufferDynamicOffset(uint8 DescriptorSet, uint32 BindingIndex, uint32 DynamicOffset)
{
	const uint8 DynamicOffsetIndex = DSWriter[DescriptorSet].BindingToDynamicOffsetMap[BindingIndex];
#pragma region Engine ZXB
	// [ZXB] r.Vulkan.DynamicGlobalUBs=0 时 layout 无 dynamic UBO binding，map 值保持哨兵 255，
	// 越界写 DynamicOffsets[255] 导致 RHIThread 崩溃；非 dynamic binding 忽略 offset（绑定时已写入 descriptor）。
	if (DynamicOffsetIndex == 0xFF)
	{
		return;
	}
#pragma endregion
	DSWriter[DescriptorSet].DynamicOffsets[DynamicOffsetIndex] = DynamicOffset;
}
```

### 改动 2：定向 DeviceProfile——高通驱动 < 440 才关 dynamic UBO（同 CL 1119465）

全局 `=0` 会让 440+ 驱动设备白吃性能回退，最终方案改为**按设备定向**。三处改动全在 `UE5EA/Engine/Config/BaseDeviceProfiles.ini`：

**① 匹配规则**——插在引擎原 `Android_Adreno6xx_Vulkan` 规则**之前**（first-match-wins 语义要求）：

```ini
; [Engine ZXB] Adreno 6xx Vulkan devices with Qualcomm driver < 440 (e.g. 630 V@415): close dynamic UBO.
; Vulkan subpass puts dynamic UBO and input attachment in the same descriptor set (set==stage),
; which triggers whole-frame black flicker on these drivers. Must be evaluated BEFORE the generic
; Android_Adreno6xx_Vulkan rule below (first-match-wins).
+MatchProfile=(Profile="Android_Adreno6xx_Vulkan_LowDriver",Match=((SourceType=SRC_GpuFamily,CompareType=CMP_Regex,MatchString="Adreno \\(TM\\) 6[0-9][0-9]"),(SourceType=SRC_AndroidVersion, CompareType=CMP_Regex,MatchString="([0-9]+).*"),(SourceType=SRC_PreviousRegexMatch,CompareType=CMP_GreaterEqual,MatchString="10"),(SourceType=SRC_VulkanAvailable,CompareType=CMP_Equal,MatchString="true"),(SourceType=SRC_GLVersion, CompareType=CMP_Regex,MatchString="V@([0-9]+)"),(SourceType=SRC_PreviousRegexMatch,CompareType=CMP_Less,MatchString="440")))
```

**② Profile 定义**——完整继承原 Profile，只加一条 CVar（放在 `[Android_Adreno6xx_Vulkan DeviceProfile]` 段之后）：

```ini
; [Engine ZXB] Qualcomm Vulkan driver < 440: dynamic UBO + input attachment share one descriptor
; set in subpass (set==stage) and cause whole-frame black flicker; fall back to regular UBO.
[Android_Adreno6xx_Vulkan_LowDriver DeviceProfile]
DeviceType=Android
BaseProfileName=Android_Adreno6xx_Vulkan
+CVars=r.Vulkan.DynamicGlobalUBs=0
```

**③ Profile 注册**（`DeviceProfileNameAndTypes` 列表，跟在 `Android_Adreno6xx_Vulkan,Android` 之后）：

```ini
; [Engine ZXB] low Qualcomm Vulkan driver (<440) variant of Android_Adreno6xx_Vulkan
+DeviceProfileNameAndTypes=Android_Adreno6xx_Vulkan_LowDriver,Android
```

**匹配链路说明**：
- "440" = **高通 Adreno 驱动版本**（非 Vulkan API 版本）。`SRC_VulkanVersion` 取的是 API 版本（如 1.1.87）**不可用**；驱动版本只在 `SRC_GLVersion: "OpenGL ES 3.2 V@415.0 (...)"` 字符串里，用 CMP_Regex `V@([0-9]+)` 提取捕获组存入 PreviousRegexMatch，再 `CMP_Less "440"` 数值比较（引擎规则 869/878 的原生模式）
- MI8（V@415）：415 < 440 → 命中 LowDriver → CVar=0
- V@440+ 设备：`CMP_Less 440` 不命中 → 落回原 `Android_Adreno6xx_Vulkan`（无此 CVar，**保持默认 2，dynamic UBO 优化完整保留**）

### 改动 3：项目 ini 删全局段（BulletTest 侧）

`DefaultEngine.ini` 的全局 `[ConsoleVariables] r.Vulkan.DynamicGlobalUBs=0` **必须删除**：`SetBySystemSettingsIni` 优先级高于 DeviceProfile（`SetByDeviceProfile`），不删会把 440+ 设备钉死在 0，定向失效。

### 三个关键陷阱（定向方案成败点）

| 陷阱 | 原因与对策 |
|---|---|
| **项目 ini 加 `+MatchProfile` 无效** | 配置数组聚合时项目 ini 只会追加在引擎规则之后；first-match-wins 下老 Adreno 设备先被引擎规则捞走，项目规则永远轮不到 → **必须改 fork 的 BaseDeviceProfiles.ini** |
| **`SRC_VulkanVersion` 语义陷阱** | 它是 Vulkan API 版本（1.1.87），不是高通驱动版本；驱动版本必须从 `SRC_GLVersion` 的 `V@(\d+)` 提取 |
| **全局 `[ConsoleVariables]` 优先级压制定** | ini 全局值（SetBySystemSettingsIni）> DeviceProfile（SetByDeviceProfile），共存时定向逻辑失效 |

### 验证结果

| | 录屏 fps | 运行期闪黑 | 启动稳定性 |
|---|---|---|---|
| 修复前（5 轮基线） | 28.65 | +3.80s 两帧 / +7.73s 一至两帧，**5/5 必现** | 稳定（默认 CVar=2） |
| =0 无 guard | — | 无法观察 | **3/3 每次启动必崩** RHIThread |
| =0 + guard（g1/g2） | 19.63 / 25.03 | **0 段 / 0 段** | 存活 |
| 定向配置（t1） | 24.11 | **0 段** | 存活 |

定向配置的设备级证据（MI8，V@415）：
```
LogAndroid: Selected Device Profile: [Android_Adreno6xx_Vulkan_LowDriver]
LogDeviceProfileManager: Pushing Device Profile CVar: [[r.Vulkan.DynamicGlobalUBs:2 -> 0]]
```
（`ECVF_ReadOnly` 不阻碍 DeviceProfile 启动期应用；规则进包验证看 `Saved/Cooked/.../Metadata/CookedIniVersion.txt` 的 `MatchProfile` 行与索引顺序）

### 打包/装包要点

- **无需清 cook 缓存**：SPIR-V set/binding 编号不变，仅 descriptor type 变，`-iterate` 复用 cook 是安全的（与 RHI 切换 GLES↔Vulkan 的场景不同）
- `[ConsoleVariables]` 进包验证（产物级铁证）：`Saved/Cooked/Android_ASTC/BulletTest/Metadata/CookedIniVersion.txt` 应出现 `ConfigSystem.Android.Engine:ConsoleVariables:r.Vulkan.DynamicGlobalUBs:0=0`
- **DeviceProfile 规则进包验证**：同一文件 `CookedIniVersion.txt` 的 `MatchProfile:N=` 行——确认新规则存在且**索引小于**原 `Android_Adreno6xx_Vulkan` 规则（first-match-wins 顺序）；注意 staged 目录的 `StagedBuild_BulletTest.ini` 是 0 字节空文件属正常（Android ini 全走 cook 的 ConfigSystem，不走 staged 文件）
- OBB pak 压缩导致明文 grep 不到 CVar 字符串，属正常，**以 CookedIniVersion.txt + 运行日志为准**
- 运行期终审：`Selected Device Profile: [Android_Adreno6xx_Vulkan_LowDriver]` + `Pushing Device Profile CVar: [[r.Vulkan.DynamicGlobalUBs:2 -> 0]]`
- 卸载重装会使 PSO 缓存（`VulkanProgramBinaryCache`）自动重建，无需手动清
- 引擎 so 重编后注意：`UnrealEditor-VulkanRHI.dll` 可能被 UnrealMobileDeviceViewer 等工具进程内存映射锁定 → LNK1104 链接失败，taskkill 后重跑打包即可
- 装包脚本拉起 app 后若锁屏，app 会挂起不写日志（日志目录空）——解锁后正常，勿误判日志链路故障

## 五、快速排查 Checklist

**判定管线（Mobile Deferred vs Forward）**：
1. `DefaultEngine.ini` 查 `r.Mobile.ShadingPath`（1=Deferred 0=Forward）
2. 运行日志 grep `MobileDeferredLightingSplitPass changed` / `FMobileDeferredCopyDepthPS`（Deferred 签名）

**闪黑复现与验证**：
3. 录屏防污染三件套：`svc power stayon true`（录前）→ 录完 `stayon false`；锁屏状态 screencap/screenrecord 会录到黑屏假帧
4. `python C:/Users/djangozhang/.tclaude/skills/BulletTest/scripts/analyze_flicker.py <mp4> --save-frames`
5. 判读：≥1s 黑 = BOOT_BLACK（预期）；<0.2s 黑 = FLASH_BLACK（缺陷）；多轮 offset 一致 = 确定性事件
6. **录屏 fps < 15 时结果无效**（采样不足会漏检 34ms 单帧黑），需重录（PSO 缓存建好后帧率恢复）

**崩溃符号化（Android UE 大 so）**：
7. 从 APK 提取 `lib/arm64-v8a/libUnreal.so`（Development 包 500MB+ 未 strip，可直接符号化）
8. NDK 工具：`C:/Users/djangozhang/AppData/Local/Android/Sdk/ndk/25.1.8937393/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-symbolizer.exe --obj=libUnreal.so --demangle <pc>`
9. **pc +0x10000 修正**：text 段 program header file offset 0xdb1d000 → vaddr 0xdb2d000（16KB page 对齐差）。直接喂原始 pc 得到自相矛盾的错位符号；修正后调用链应逻辑连贯
10. `SIGSEGV code -6 (SI_TKILL)` = 进程自 raise（check/assert 崩溃），非野指针

**本问题修复验证顺序**：
11. 引擎带 CL 1119465 的 guard（否则 Mobile Deferred 下 =0 必崩）→ 12. BaseDeviceProfiles.ini 三处定向配置（规则/Profile/注册）→ 13. **删**项目 ini 全局 `[ConsoleVariables]` 段（否则定向失效）→ 14. 重打包 → 15. 日志三连验证：`Selected Device Profile: [Android_Adreno6xx_Vulkan_LowDriver]` + `Pushing Device Profile CVar: [[r.Vulkan.DynamicGlobalUBs:2 -> 0]]` + 进程存活 → 16. 录屏 2 轮以上 0 闪黑

**DeviceProfile 定向配置速查**：
17. 高通驱动版本获取：设备日志 `SRC_GLVersion: OpenGL ES 3.2 V@415.0 (...)` 中的 `V@` 后数字（`SRC_VulkanVersion` 是 API 版本，别用错）
18. 版本比较模式：`CMP_Regex "V@([0-9]+)"` 提取 → `SRC_PreviousRegexMatch` + `CMP_Less/GreaterEqual` 数值比较（每次 CMP_Regex 会覆盖 PreviousRegexMatch，链式使用是引擎原生模式）
19. 新规则必须插在引擎原规则**前**（first-match-wins，`AndroidDeviceProfileSelector.cpp:304-308`）；项目 ini 的 `+MatchProfile` 只追加在引擎规则后，**定向规则必须改 fork 的 BaseDeviceProfiles.ini**
20. 新 Profile 三件套缺一不可：`+DeviceProfileNameAndTypes` 注册 + `[Xxx DeviceProfile]` 段（BaseProfileName 继承）+ 匹配规则

## 六、相关参考

- 引擎源码/配置关键位置：
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanShaders.cpp:16-23`（CVar 定义）、`:782-837`（dynamic 转换）
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanPipelineState.cpp:84-92`（哨兵初始化）
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanPipelineState.h:151-161`（guard 修复处）、`:171-186`（Bind 连续区间假设）
  - `UE5EA/Engine/Source/Runtime/VulkanRHI/Private/VulkanCommands.cpp:942-979`（崩溃入口）
  - `UE5EA/Engine/Source/Developer/VulkanShaderFormat/Private/SpirVShaderCompiler.inl:506-519`（编译期 set≡stage）
  - `UE5EA/Engine/Source/Runtime/Renderer/Private/MobileShadingRenderer.cpp:2467,2538`（SinglePass subpass）
  - `UE5EA/Engine/Config/BaseDeviceProfiles.ini`（定向改动三处：~L123 注册、~L879 匹配规则、~L1163 Profile 段）
  - `UE5EA/Engine/Plugins/Runtime/AndroidDeviceProfileSelector/.../Private/AndroidDeviceProfileSelector.cpp:304-308`（first-match-wins）、`:253-265`（CMP_Regex 捕获组→PreviousRegexMatch）
- Perforce：changelist **1119465**（哨兵 guard，2026-09-07，pending）
- 闪黑分析工具：`C:/Users/djangozhang/.tclaude/skills/BulletTest/SKILL.md`（能力 10：录屏闪烁分析）
- 相关历史文档：
  - `E:\AiDoc\UE-Android-Vulkan启动崩溃-ChunkedPSOCache-validationlayer.md`（同项目 PSO×validation layer 崩溃，与本问题独立）
  - `E:\AiDoc\UE-Android-无日志-ABSLOG-CWD相对路径.md`（同项目日志链路修复）
- 验证数据留档：`C:/Users/djangozhang/Documents/Unreal Projects/BulletTest/Saved/Screenshots/flicker_test/`（flicker_r1-r5 基线、flicker_g1/g2 guard 修复后、flicker_t1 定向配置后）
- 规划三件套（本次排查全程记录）：`C:/Users/djangozhang/Documents/Unreal Projects/BulletTest/task_plan.md / findings.md / progress.md`
