# UE-Android-845黑屏-GPUScene-SSBO-stride修复

> 小米 MI8（骁龙845/Adreno630，Vulkan 1.1.87，2020 老驱动）上所有 Opaque 物体全黑（屏幕 97% 纯黑），8Gen3(Adreno750) 正常；根因是 GPUScene 全局 `StructuredBuffer<float4>` 的 SSBO 元素 stride 在 Adreno 630 上处理偏差，导致 VS 读实例数据错位、图元全部消失。修复 = 用单成员 struct 包裹强制 16 字节 stride。

---

## 一、问题定位流程

- **现象**：BulletTest 在 845 上画面 97% 纯黑（mean_lum=1.1），仅残存描边/UI 等微弱内容；8Gen3 正常。用户最初 RenderDoc 观察到"Opaque draw 未写入 RT、深度也未写入"。
- **确认事实**（逐步收窄）：
  - 两机 ini 配置相同（`r.Mobile.ShadingPath=1`=**Deferred**、FXAA、MobileHDR=YES），`r.Mobile.TonemapSubpass` 默认关闭。
  - 唯一设备分叉 = `SupportsParallelRendering()`：845=false（无 sync2/separate_depth_stencil_layouts/render_pass2），8Gen3=true。
  - 845 的 `Android_Adreno6xx_Vulkan` DeviceProfile 匹配规则存在但 profile **未注册** → 裸引擎默认运行。
- **排除项**（一笔带过）：ForceMultiPass 仍黑 → subpass 结构排除；ETC2 仍黑 → ASTC 纹理格式排除；`r.InstanceCulling=0` 仍黑 → 实例剔除排除；PS 强制固定输出 GBuffer 仍零 → **无片元**（非输入垃圾）；GPUScene 关闭 → **画面恢复**，锁定 GPUScene 数据路径。

## 二、根因分析

**Adreno 630 老驱动（Vulkan 1.1.87, 2020）对 GPUScene 的 `StructuredBuffer<float4>` SSBO 元素 stride 处理与引擎上传不一致** → VS 读实例数据错位 → 实例位置/图元退化 → base pass draws 不产生片元 → 黑屏。这解释了全部现象：Forward/Deferred × SinglePass/MultiPass 四象限全中、8Gen3 正常（新驱动 stride 正确）、无深度/颜色写入（无片元自然无写入）。

判别链关键证据：E4 读回 GBufferA 全零（544px）；PS 硬编码输出仍全零 = draws 根本没产生片元，而非着色/输入问题。

## 三、详细技术原理

### 3.1 GPUScene 数据路径

移动端开启 `r.Mobile.SupportGPUScene` 后，mesh VS 通过全局 `StructuredBuffer`（SSBO）读取实例/图元数据（本地→世界矩阵、裁剪包围盒等），这些数据由 CPU/GPU 每帧上传到 buffer，shader 按元素 stride 寻址。**stride 不一致 → 读到错误偏移 → 变换矩阵/包围盒错乱 → 图元被剔除或退化**。

### 3.2 SSBO stride 与 struct 包裹

- HLSL 中 `StructuredBuffer<float4>` 的元素 stride 在部分移动驱动上按 std140 计算（与 std430 的 16 字节紧排存在偏差），引擎上传侧按紧凑数组写。
- **修复手法**：把声明改为 `struct F{ float4 Data; }; StructuredBuffer<F> X`，单成员 struct 强制驱动按成员对齐（std430 语义 = 16 字节），与上传侧一致；访问从 `X[i]` 改为 `X[i].Data`。

### 3.3 相关 DeviceProfile 遗漏（引擎 bug）

`Engine/Config/BaseDeviceProfiles.ini` 的 `DeviceProfileNameAndTypes` 缺失 Adreno 6xx 整代注册（5xx→65x→66x→68x→7xx），但匹配规则 `Android_Adreno6xx_Vulkan`（匹配 `Adreno (TM) 6[0-9][0-9]`）存在 → 所有 6xx Vulkan 设备匹配到**不存在的 profile** → 裸默认运行（GPUScene 开启、无低端优化）。66x profile 注释"all Adreno6xx devices"表明是登记遗漏。

## 四、修复方案

### 4.1 核心修复：GPUScene SSBO stride（3 个 shader，均 ZXB region 包裹）

参考 UE5.4 shelved patch（Wei.Liu 2024/10/28，"Fix missing primitives with mobile gpuscene enabled on 845"）：

**`Engine/Shaders/Private/SceneData.ush`**（`USE_GLOBAL_GPU_SCENE_DATA` 分支）：
```hlsl
struct FGPUSceneInstanceScene { float4 Data; };
struct FGPUSceneInstancePayload { float4 Data; };
StructuredBuffer<FGPUSceneInstanceScene> GPUSceneInstanceSceneData;
StructuredBuffer<FGPUSceneInstancePayload> GPUSceneInstancePayloadData;
// 访问：GPUSceneInstanceSceneData[Index].Data / GPUSceneInstancePayloadData[Index].Data
```

**`Engine/Shaders/Private/LightmapData.ush`**：
```hlsl
struct FGPUSceneLightmap { float4 Data; };
StructuredBuffer<FGPUSceneLightmap> GPUSceneLightmapData;
// 访问：GPUSceneLightmapData[Index].Data
```

**`Engine/Shaders/Private/InstanceCulling/BuildInstanceDrawCommands.usf`**：
```hlsl
struct FPackedDrawCommandDesc { uint Data; };
StructuredBuffer<FPackedDrawCommandDesc> DrawCommandDescs;
struct FViewId { uint Data; };
StructuredBuffer<FViewId> ViewIds;
// 访问：DrawCommandDescs[...].Data / ViewIds[...].Data
```

### 4.2 附带修复：base pass 深度绑定规范违规

`MobileShadingRenderer.cpp` 的 `InitRenderTargetBindings_Forward/Deferred`：`bIsFullDepthPrepassEnabled` 分支原绑定 `DepthRead_StencilWrite`，但 base pass 实际写深度（`BasePassDepthStencilAccess=DepthWrite_StencilWrite`）→ 声明改为 `DepthWrite_StencilWrite`。非并行（Vulkan<1.2）设备会把"只读"烘焙进 subpass 0 深度 layout，老驱动丢弃写入。

### 4.3 附带修复：Adreno6xx DeviceProfile 注册与配档

`Engine/Config/BaseDeviceProfiles.ini`：
- **补注册**：`DeviceProfileNameAndTypes` 加 `Android_Adreno6xx` / `Android_Adreno6xx_Vulkan`，定义两 profile：
  ```ini
  [Android_Adreno6xx DeviceProfile]
  DeviceType=Android
  BaseProfileName=Android_Low
  +CVars=r.Android.GLESFlipYMethod=1

  [Android_Adreno6xx_Vulkan DeviceProfile]
  DeviceType=Android
  BaseProfileName=Android_Adreno6xx
  +CVars=r.Android.DisableVulkanSupport=0
  ```
- **按型号配档**：6xx 宽泛（630/640）→ `Android_Low`；`Android_Adreno65x`（Adreno650/骁龙865，中配）`BaseProfileName=Android_Mid`；66x/68x 已为 Mid。
- **修正匹配规则顺序**：65x/66x/68x 特化规则（`65[0-9]`/`66[0-9]`/`68[0-9]`）原在 6xx 宽泛规则（`6[0-9][0-9]`）**之后**（特化永不生效，650 会被宽泛 6xx 抢走成 Low）→ 提前到宽泛之前。

## 五、验证结果

| 指标 | 845（修复后） | 8Gen3 基准 |
|---|---|---|
| 全屏亮度 mean_lum | **85.4** | 82.9 |
| 纯黑占比 | **0.53** | 0.56 |
| 地面区域亮度 | **189.7** | 192.8 |

- 845 日志 `Active device profile: Android_Adreno6xx_Vulkan`，走链 `→Android_Adreno6xx→Android_Low→Android`。
- 画面与 k70 基准一致（蓝天/云/彩色物体/阴影全渲染）。

## 六、快速排查 Checklist

1. **截图判断用像素统计 + 裁剪**，不要依赖整图视觉（曾把 97% 黑屏误读为"天空正常"）。
2. 用"开关整个机制"做判别实验：`r.Mobile.SupportGPUScene=False`、`r.InstanceCulling=0`、ETC2 cook、ForceMultiPass。
3. 用读回仪器判定"是否产生片元"：`AddReadbackTexturePass` 读 GBufferA/SceneColor（全零 = 无片元，非着色问题）。
4. 确认 DeviceProfile 是否命中：日志 `Active device profile` / `not found`。
5. PSO 崩溃时在 `VulkanPipeline.cpp` 用 `checkf` 打印 shader 名定位。

## 七、相关参考

- **核心 patch（用户提供，UE5.4 shelved）**：`C:\Users\djangozhang\Documents\xwechat_files\wxid_svlwaxzko10721_9c6e\msg\file\2026-09\FixMissingPrimitivesWithMobileGpusceneEnabled.patch`
- **引擎改动文件**：
  - `UE5EA\Engine\Shaders\Private\SceneData.ush`
  - `UE5EA\Engine\Shaders\Private\LightmapData.ush`
  - `UE5EA\Engine\Shaders\Private\InstanceCulling\BuildInstanceDrawCommands.usf`
  - `UE5EA\Engine\Source\Runtime\Renderer\Private\MobileShadingRenderer.cpp`
  - `UE5EA\Engine\Config\BaseDeviceProfiles.ini`
- **设备分叉依据**：`SupportsParallelRendering()`（`VulkanDevice.h`）= `HasSeparateDepthStencilLayouts && HasKHRSynchronization2 && HasKHRRenderPass2`；845 三扩展全无，8Gen3 全有。
- **Memory 索引**：`gpuscene-stride-adreno630-black-screen`（含根因/修复/排查教训）
