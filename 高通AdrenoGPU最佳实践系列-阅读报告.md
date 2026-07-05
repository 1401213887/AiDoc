# 高通 Adreno GPU 最佳实践系列 · 完整阅读报告

Qualcomm Adreno GPU · Best Practices · Full Edition

# 高通 Adreno GPU 最佳实践系列 · 完整阅读报告

知乎专栏作者「知秋」（方向：游戏性能优化）整理翻译的高通 Adreno GPU / 骁龙平台移动端图形优化系列。本报告按篇梳理每一篇的核心内容、关键要点与适用场景，已覆盖专栏全部正文文章，方便系统通读与随手查阅。

专栏作者：**知秋**
主题：**移动端 GPU / 骁龙性能优化**
本报告覆盖：**22 篇全文**
生成日期：**2026-06-23**

**覆盖范围：**该专栏共 23 条目，其中第 0 篇为「汇总目录」（正文即各篇链接，无独立内容），其余 **22 篇技术文章正文已全部抓取并逐篇解析**：主系列 实践 1~17、补充 0~2、番外 1~2。下方均基于原文整理，未做任何臆测。

## 系列概览：这套文章在讲什么

整体围绕「在骁龙 / Adreno GPU 上把移动游戏跑得更快、更省电、更稳」这条主线，可归纳为四大板块。

#### ① 架构与渲染管线

Tile-based 分块渲染、GMEM、FlexRender、并发分块、Tile Shading 扩展、LRZ/Early-Z/Fast-Z、渲染图面与带宽压缩 UBWC。

#### ② 着色器与资源优化

统一/标量着色器架构、半精度、GPR 最小化、纹理与压缩格式、顶点/索引缓冲、VRS、GPU 驱动渲染、网格着色、光线追踪。

#### ③ 分析工具与方法论

骁龙分析器三模式+指标阈值、瓶颈分诊（GPU/CPU/Vsync）、GMEM Loads 排查、查询、内存与生命周期、低功耗策略。

#### ④ 画质与功耗增强

True HDR、骁龙游戏超分 SGSR1/2、Adreno 帧运动引擎/插帧、QCOM 专属扩展（图像处理硬件、着色器解析）、Vulkan 预旋转。

## 目录

点击跳转到对应篇目的详细解析。编号沿用原文。

## 主系列 · 架构与渲染管线

01

### 渲染器架构 —— 设计高效 Adreno 渲染器的核心准则

Renderer Architecture：Vulkan 优先、子通道合并、资源选型

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956724228700233810)

全系列的纲领篇，列出设计 Adreno 渲染器时必须牢记的一系列宏观准则。

核心准则

- **API 选型：**优先 Vulkan 而非 OpenGL ES（性能、可调试性、功能更优）。
- **渲染通道：**尽量减少通道数；同格式连续通道合并；合并结果优先用 Alpha 混合而非 discard/分支/模板/计算。
- **子通道合并：**良好的 Vulkan 渲染通道应满足——子通道数 >1、含输入附件、每个解析附件仅用于一个子通道、正确设置 access mask。配置得当最多省 **10%+ 帧时间**，让 GPU「逐瓦片」执行整条子通道链。
- **尽早失效帧缓冲：**Vulkan 用 `LOAD_OP_CLEAR`/`DONT_CARE`；OpenGL ES 用 `glInvalidateFramebuffer`/`glClear`/`EXT_discard_framebuffer`，避免 GMEM 不必要解析到系统内存。
- **深度预通道：**空片段着色器 + 禁帧缓冲写入，以启用 Fast-Z。

资源与数据传递

- UBO 优于 push constants；VBO > UBO > 纹理 > SSBO；单着色器全部 UBO 总和 < 8K 的 90%（≈7372 字节），超限用静态索引。
- 大数据量随机访问优先纹理（缓存优势）；仅渲染通道内用的缓冲（MSAA 附件、Z-buffer）标 `LAZILY_ALLOCATED_BIT`。
- 交换链：Android 上 `FIFO`+`minImageCount=3` 最高效；`MAILBOX` 优化延迟但费电；建议预旋转。
- 图形提交与计算派发尽量分开、避免交错切换。

应避免的 Vulkan 特性

- `MUTABLE_FORMAT_BIT`（A750 前）、`conditional_rendering`、`vertex_input_dynamic_state`（静态管线更优）、细分曲面阶段、客户端顶点数组、用户裁剪平面；三角形理想覆盖 ≥4 像素。

**适用场景：**从零设计或重构移动端渲染器时的「总纲检查表」。

02

### Tile-based Rendering —— 分块渲染架构与 Tile Shading 扩展

移动 GPU 省带宽的根本机制

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956771279777543398)

分块渲染原理

- 帧缓冲拆成小矩形 tile，逐个用片上高速 **GMEM** 渲染，避免访问带宽受限的系统内存。
- 两阶段：**分块阶段**生成「可见性流」标记三角形落在哪些 tile→存系统内存；**渲染阶段**读可见性流仅渲各 tile 可见图元，完成后 GMEM 归并回系统内存。

关键特性

- **Bin 最小化：**降分辨率/VRS/减 MSAA 采样/减渲染目标/简化顶点着色器。
- **FlexRender™：**分块与直接渲染间自动切换（顶点纹理采样比高、绘制少、用细分/几何着色器倾向直接模式）。
- **并发分块（A7x）：**无依赖时 binning 可在顶点着色器前异步跑；复用深度缓冲不清除有助于并发。
- **Tile Shading（A840+）：**`tile_memory_heap`+`tile_shading` **必须同时启用**，让计算着色器直接访问 GMEM、跨通道驻留资源、per-tile draw；per-tile block 用 `vkCmdBeginPerTileExecutionQCOM` 括起并带 `BY_REGION_BIT`。
- **MSAA：**硬件在 tile 内做，无需额外系统内存与 blit。

**适用场景：**理解一切带宽优化的底层逻辑；落地延迟渲染/Tile Shading。

03

### 渲染图面（Render Surfaces）—— sRGB、上采样与带宽压缩

颜色/深度格式选型 + UBWC

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956783089708696786)

格式选型

- **sRGB 纹理**与显示屏一致；Vulkan 原生支持，OpenGL ES 需额外处理。
- **颜色：**`R10G10B10A2`（首选）> `R11G11B10`（无 Alpha）> `RGBA16`（渐变透明/高色深）。
- **深度：**`D16`（无模板且精度够）> `D24_S8`（需模板）> `D32`（无模板但 D16 不够）。

上采样（降渲染分辨率）

- 低分辨率渲再放大（1080p→720p）。方法：**骁龙 GSR**（单通道空间超分，比双线性清晰）、`vkCmdBlitImage`、OpenGL ES blit（比渲全屏四边形快）、Android `setFixedSize` 让 SurfaceFlinger 缩放。

带宽优化 + UBWC

- 减缓存未命中：压缩纹理（4×4 块单次读 16 像素）、紧凑顶点格式、索引绘制+最小索引类型。
- **UBWC（A5x 起全系）：**预测性实时压缩 GPU↔内存数据，对开发者透明，Vulkan 用 `TILING_OPTIMAL` 启用。与 ASTC 等本质不同：UBWC 解决「传得快/耗得少」，压缩纹理解决「存得小」。

**适用场景：**带宽/功耗受限时的格式选型与降分辨率渲染。

04

### LRZ、Early-Z 与 Fast-Z —— 三种深度优化的协同

宏观剔除 + 像素级剔除 + 深度写入加速

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956802653955785478)

| 技术 | 层级 / 目标 | 开发者控制 |
| --- | --- | --- |
| **LRZ**（A5x 低分辨率深度通道） | 分块/图元级粗剔除，**与绘制顺序无关**，CPU 开销极小 | 间接（优化设计提升生效概率） |
| **Early-Z** | 像素级，着色前剔除遮挡片段，最高 **4 倍填充率**剔除 | 直接（控深度函数、禁 discard/改深度） |
| **Fast-Z** | 仅深度写入 **2 倍速**（阴影/深度预通道） | 间接（设计仅深度通道） |

失效场景（重要）

- **LRZ 禁用：**改深度方向、深度函数设 ALWAYS/NOT\_EQUAL、混合后深度写、颜色掩码/部分 MRT 写、模板操作、帧缓冲获取、高级混合；片段着色器写 UAV/深度/模板；alpha-to-coverage、discard。
- **Early-Z 禁用：**写深度/模板、discard、alpha-to-coverage（建议图元从前到后排序）。
- **Fast-Z：**空片段着色器+禁写掩码；间接绘制用 `layout(early_fragment_tests) in;`。

原文「图书馆找书」类比：LRZ=看楼层平面图筛区域，Early-Z=到书架摸一下，Fast-Z=只贴标签开绿色通道。三者协同——深度预通道用 Fast-Z 快速建深度，为主通道 LRZ/Early-Z 创造剔除条件。

**适用场景：**深度复杂度高的场景做剔除优化、排查 LRZ 意外失效。

## 主系列 · 着色器与资源优化

05

### 纹理 —— 采样模式与压缩格式

Bindless 采样、压缩格式优先级

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956839017757774506)

- **采样：**用 `COMBINED_IMAGE_SAMPLER` 走更优的无绑定路径；独立采样器填充率通常低 2%–5%。
- **多纹理：**单多边形多张纹理，可减过度绘制、降 ALU、避免多余顶点变换。
- **压缩格式优先级：**`ASTC`（可变块、sRGB 高效）> `ETC2` > `ATC`（Adreno 专属）> `ETC` > `DXT` > `BC`；ASTC 支持 HDR/LDR；Vulkan 需离线预压缩。
- **转换方式：**设备端（启动耗时）/ 预打包（最优但需多版本 APK）/ 下载式（需检测 GPU+网络，最精准）。
- 支持 FP16/FP32 浮点纹理、无缝立方体贴图、视频纹理（无独立显存，由驱动从系统内存分配）。

**适用场景：**纹理格式选型与采样器配置。

06

### 着色器（Shaders）—— 半精度、GPR 与 ALU 优化

系列里着色器侧信息密度最高的一篇

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1956845808163992205)

架构基础

- **统一着色器架构：**顶点/片段/计算共用 ALU，4 个一组作为 wave 处理，停滞时 ALU 可调度给其他任务。
- **标量架构+半精度：**片段着色器用 16 位 mediump 而非 32 位 highp，能效与性能可**翻倍**。

指令数与 GPR 最小化

- 指令数应适配指令缓存；过大则拆分（中间结果存 GMEM 最快）。降 GPR 提升并发 wave；**不展开循环**常可省 GPR；避免「超级着色器」。

ALU 优化技巧

- 最小化类型转换（`int4+1.0` 这类不匹配会从 1 条暴增到 8 条指令）。
- 分支按性能：编译时常量 > 统一变量 > 着色器内修改变量；打包插值器/标量常量触发 mad 指令；优先内置函数；避免 discard/改深度。

资源限制与延迟隐藏

- A7X 超整数倍即可能掉性能：顶点缓冲 32、UBO 16、纹理+SSBO 总数 16；LPAC 计算指令 2256。
- 编译器把只读 SSBO 访问转为纹理获取隐藏延迟；纹理瓶颈时优先 VBO、按数据簇访问（硬件按 2×2 块）、用 mipmap、慎用三线性/各向异性。
- 初始化期编译（Vulkan 调 `vkCreateGraphicsPipelines`；ES 用二进制 blob）；**Adreno 驱动从不重编译着色器**。
- 能用片段着色器就别用计算（有并发解析硬件）；计算工作组 ≥64 避免 CPU 等 GPU；同步优先原子操作。

**适用场景：**着色器逐行调优、排查 GPR/指令缓存导致的性能问题。

07

### GPU 驱动渲染（GPU-Driven Rendering）

实例化、间接绘制、网格着色

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1957191590872322932)

- **几何实例化：**一次绘制渲多个同网格副本，仅存一份顶点/索引+实例变换流，省内存。
- **间接绘制：**开销从 CPU 转 GPU，GPU 自决绘制内容；仅深度通道配 SSBO 用 `layout(early_fragment_tests) in;` 激活 Fast-Z。
- **顶点流 > 属性获取**（GPU 驱动架构下）；**LPAC** 适合延迟不敏感的毫秒级计算，与图形管道并发。
- **2D 硬件加速：**blit、表面清除、预旋转、旋转、卷积核（`VK_QCOM_image_processing`）。
- **网格着色（A8x）：**比几何/外壳/域/细分更高效，支持 meshlet 级剔除；设最优 wave（A7x/A8x=64）、逐顶点属性优于逐图元、payload ≤32kB 倍数。

**适用场景：**大规模重复几何、GPU 驱动管线、A8x 网格着色。

08

### 可变速率着色（Variable Rate Shading, VRS）

一个片段着色一组像素，降负载省电

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1957206229647614661)

- **原理：**与抗锯齿相反——颜色变化不剧烈或后续会模糊的区域降低着色频率，用对则几乎无画质损失却大幅减 GPU 工作量。
- **API：**Vulkan `VK_KHR_fragment_shading_rate`（传 `VkExtent2D`）；OpenGL ES `QCOM_shading_rate`（如 `GL_SHADING_RATE_1X2_PIXELS_QCOM`）。
- **适用区域：**颜色差异低、会被运动模糊/景深降采样的区域、运动体积渲染。

**适用场景：**片段着色受限的高负载场景，配合运动模糊/景深/注视点渲染。

09

### 顶点缓冲区与索引缓冲区

布局、压缩、批处理、索引类型

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1957210933135913921)

- A7X 顶点缓存 32 个 4D 顶点；传单个顶点缓冲。
- **布局：**开头放影响位置的属性交错数组（xyz|xyz），其后放其他属性——配合「仅位置顶点着色器」。
- **压缩：**尽量压缩属性，已知范围数据映射打包格式（法线用 `GL_UNSIGNED_INT_2_10_10_10_REV`）；ES 顶点半精度用 `GL_OES_vertex_half_float`（Vulkan 顶点暂不支持）。
- **缓存友好：**共享顶点的三角形聚集；动态改 VBO 时先批量更新再统一绘制。
- **索引类型：**优先 8 位 > 16 位 > 尽量避免 32 位。

**适用场景：**几何数据布局与顶点带宽优化。

10

### 光线追踪（Ray Tracing）

作为光栅化补充，Ray Query + 加速结构

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1958285518396720440)

基础

- 通常作为光栅化的**补充**（阴影/反射/次表面散射）。暴力求交不可行，必须用**加速结构**（`VK_KHR_acceleration_structure`，建议 GPU 构建）+ `VK_KHR_ray_query` 任意阶段查询碰撞。

优化要点

- 指令缓存优化、16 位精度、几何剔除（视锥/portal/LOD）。
- GPU 构建 AS（A8x 最优 32）、查询子组 A8x=64、并发用 LPAC；变形小用「重构」、大用「重建」。
- **Ray Query 优于 Ray Pipeline**；减少 `rayQueryProceed()`（会内联）；复用单个最小查询对象；用内置函数访问数据；「首次匹配即接受」无需 while 循环。

**适用场景：**移动端光追阴影/反射落地与调优。

11

### True HDR —— 移动端端到端 HDR 方案

HDR10 / ACES / DPU 色域体积映射

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1958630117963170376)

基础概念

- HDR 亮度远超 SDR；宽色域 WCG（BT.2020 > DCI-P3 > BT.709）；亮度+色域=颜色体积。
- 高通「真 HDR」是移动首个端到端方案，用 `R10G10B10A2`/BT2020，**色域体积映射由 DPU 执行**，兼容 HDR10（10 位+BT2020+ST2084-PQ+SMPTE ST2086）。

流程

- **传统 HDR：**渲染→后处理→颜色分级→色调映射（Reinhard/Crytek/Filmic/ACESFilm）→伽马。
- **真 HDR：**场景参考图像→后处理→色域+色调映射→UI 色调映射+缩放→合成→ST2084-PQ 编码；EOTF/色调/色域映射交 DPU。
- **ACES：**AP0/AP1 色域，RRT（→0–10000nit）+ ODT（→设备范围，如 ODT\_1000nits）。

OpenGL ES 配置

- 需 `EGL_EXT_gl_colorspace_bt2020_pq` 等；EGL 表面设 R10G10B10A2、色域 BT2020\_PQ、配 SMPTE2086 元数据。Android Vulkan WSI 仅支持 DISPLAY\_P3\_NONLINEAR。

**适用场景：**骁龙 OLED 设备落地 HDR 游戏渲染管线。

12

### 查询（Queries）—— 遮挡查询、时间戳、驱动版本

正确用 query 才能既准又省

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1958831610804831746)

- **遮挡查询：**尽量「直接模式」（CP 开销 4%–6% vs 分箱 20%–40%），flush 后批量提交。A5x 活跃 ≤512，结果 3 帧延迟。
- **时间戳查询：**务必在 renderpass 内发起保精度；分箱模式覆盖所有 tile，单 draw 给每 tile 带 2–5μs 开销。
- **驱动版本：**`driverVersion`（主10/次10/修订12，看次版本号）或 `adb shell dumpsys SurfaceFlinger | grep GLES`；着色器统计需次版本 ≥636。
- **Vulkan Adreno Layer：**检测可优化点并经 logcat 给建议（如 VKDBGUTILWARN003=子通道未正确合并）；文末列 Adreno 支持的全部 API（ES 1.x–3.2、Vulkan 1.0/1.1、OpenCL、DirectX 11/12）。

**适用场景：**遮挡剔除/精确计时，按驱动版本做兼容 workaround。

## 主系列 · 工具与方法论

13

### Android OS on Snapdragon —— 内存管理与应用生命周期

面向原生 C++ 高性能游戏

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1965392299086307480)

内存（统一内存架构）

- CPU 与 GPU 共享内存空间互相挤占。真正可用 = `availMem − threshold`。
- 图形内存 ≈ `Gfx dev + EGL mtrack + GL mtrack`；root 下读 `/sys/.../kgsl` 精细分类；`--unreachable` 粗查泄漏。

生命周期

- `onDestroy`/`onStop` 极端压力下可能不调用；`onStop` 是存档好时机；处理 `onTrimMemory()` 是防被杀最可靠手段（RUNNING\_\* 立即存档+每分钟再存）。
- 资源留进程内、避免额外服务、显式禁分屏。

**适用场景：**原生游戏内存排查、防 OOM、存档时机设计。

14

### 骁龙分析器（Snapdragon Profiler）—— 三种模式与关键指标

附大量指标参考阈值

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1965400031554147993)

三种模式

实时 Realtime
:   图表实时呈现 CPU/GPU/内存/功耗/温度等计数器。

追踪 Trace
:   捕获一段时间数据可缩放检查；支持 Vulkan/OpenGL ES/DSP。

快照 Snapshot
:   单帧完整视图：绘制调用、资源、着色器复杂度、像素历史、过度绘制。

关键阈值（节选）

- 平均多边形面积 ≥4 不超 bin；裁剪/trivial 剔除 <2%；ALU 利用率/着色器繁忙 50–100%；着色器停滞 <10%；纹理获取停滞 <2%（持续 ≥16% 偏高）；Vulkan 分箱占渲染通道 10–20%（30% 偏高）；CP 开销近 0%、绝不超 20%。

测试纪律

- 连续跑 ≥10 分钟看热节流；分析器约带 5% CPU 开销；间隔 ~21°C 冷却 ≥20 分钟；加载界面性能特征与游戏内不同。

**适用场景：**建立性能基线、指标体检时的「阈值速查表」。

15

### 理解并解决图形内存加载（GMEM Loads / Unresolves）

移动 Tile 架构最常见的 GPU 性能问题之一

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1965450329563828373)

- **一句话对策：**对所有帧缓冲附件做 **Clear 或 Invalidate**，告诉 GPU 无需把上一帧 tile 数据搬回 GMEM。
- **两大成因：**提示不当（未清除缓冲，OpenGL ES 多发）、算法问题（`glReadPixels`/`glFlush` 强制刷新）。
- **实测：**未清除模板附件触发的 GMEM 加载约占 9% 渲染时间，显式清除后帧时间缩短约 9%。

**适用场景：**PC/主机移植后帧时间偏高、追踪见红色「GMEM Loads」区块时。

16

### 识别游戏性能瓶颈（卡顿分析方法）

GPU-bound / CPU-bound / Vsync-bound 分诊

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1965454064683843764)

- **起点帧率：**看平均帧率+稳定性；目标只取 30 或 60fps。

| 类型 | 判定 | 处理 |
| --- | --- | --- |
| GPU-bound | GPU 利用率稳定 98–100%、无空闲间隙 | 减过度绘制、简化着色器、超分降分辨率 |
| CPU-bound | 非 GPU 限制+帧时间 >16ms；利用率不可靠，看线程调度 | 采样找热点（案例 `SatisfyConstraints()` 占 98%），多线程化 |
| Vsync-bound | 非 CPU/GPU 限制，单帧≈16ms，帧尾等待间隙 | 已达硬件上限，仍可优化功耗/发热 |

**适用场景：**拿到掉帧游戏不知从哪下手时的「分诊台」。

17

### 插帧（Adreno Frame Motion Engine / G-FRC 概览）

高通插帧技术演进（演讲式概览）

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1967900796483014908)

偏概览/演讲提纲（正文较短）。插帧算法闭源，是降功耗、提流畅度的有效手段。

- **AFME 1.0：**接口在 Khronos 文档，传资源即可调用，纯 GPU 图像插帧、效果有限。
- **AFME 2.0：**引入三维信息，集成更难但画质更好（尤其 3D），对难处理 case 定向优化。
- **G-FRC+：**跨代升级，并发插帧+超分、部分算法上 NPU 做 AI 优化、集成在高通源码自动提取资源，最易用。
- **现状（2025.10）：**手机插帧已成熟；除高通外华为、荣耀均发布 AI 插帧；趋势是高倍率、融合插帧、算法倒逼硬件升级。

**适用场景：**了解移动插帧全景与高通方案选型（不含代码）。

## 补充篇解析

官方开发者文档的翻译 + 作者注解，更偏「官方口径」。

补0

### 为低功耗游戏优化性能与图形表现

译自《Optimize performance and graphics for Adreno GPU for low power gaming》

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1961502637515084666)

先理解约束

- 散热限制、电池续航、设备差异、闲置唤醒、帧时序不稳——五类机制决定优化方向。

两大工具

- **骁龙分析器**（150+ 计数器实时洞察）+ **AOC**（导入 SPIR-V/HLSL/GLSL 给指令数/寄存器，可接 CI）= 实时诊断+离线调优闭环。

8 条策略

- ① 真实散热负载下分析；② 减过度绘制与着色器开销；③ 散热压力下动态降特效；④ 规划可变性能配置（DVFS）；⑤ 管后台线程；⑥ VRR 匹配场景（菜单 30Hz/战斗 60Hz）；⑦ 自动化分析接 CI；⑧ 用 SGSR 降分辨率渲染再升频。

**适用场景：**立项期制定性能/功耗策略，或优化总检查清单。

补1

### 高阶滤波与块匹配：VK\_QCOM\_image\_processing 扩展

用专用硬件做大核/自定义核采样

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1961897336759367161)

大核/自定义核采样在着色器里手写会造成纹理单元↔着色器单元大量数据往返，该扩展开放专用硬件来做。

四个新 GLSL 函数

- `textureWeightedQCOM`：二维权重核相乘求和（支持「相位」处理非中心采样；可分离滤波器水平权重放第0层、垂直放第1层）。
- `textureBoxFilterQCOM`：矩形区域加权平均。
- `textureBlockMatchSAD/SSDQCOM`：目标块与参考块相关性，返回绝对差/平方差之和（特征检测、运动跟踪、图像对齐）。
- 仅支持 2D，不支持 mipmap/多层/多重采样/深度模板。

实测收益

运行时间缩短 >75%能耗降低 ~90%平台 SM8550

启用条件

- Vulkan SDK ≥1.3.222；硬件 ≥SM8550；Adreno 驱动 ≥512.649。运动估计/光流建议改用 `GL_QCOM_motion_estimation`/`VK_NV_optical_flow`。

**适用场景：**模糊/锐化/边缘检测/上下采样/mipmap 生成、bloom 等大核或自定义核处理。

补2

### 骁龙游戏超级分辨率（SGSR1 / SGSR2）深度解析

从原理到 Shader 代码逐段剖析，全系列篇幅最长

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1963656109723190345)

SGSR1（空间超分）

- **定位：**单通道空间感知超分，针对 Adreno tiled 架构定制，号称比其他移动超分性能高一倍。
- **三特点：**升频+锐化整合进单个着色器通道；12-tap 类 Lanczos 缩放+自适应锐化；仅需单输入纹理。
- **为何快：**12 核窗口、仅用绿色通道算亮度，每像素仅采样一个分量做三分量插值，总共仅 15 条纹理指令，着色器利用率 100%。文中给出启用 `UseEdgeDirection` 的完整片段着色器（fastLanczos2/weightY/edgeDirection 逐段注释）。

SGSR2（时域超分）

- 核心两阶段：转换 convert pass + 上采样 upscale pass；引入运动向量与历史帧，画质更高但更复杂。
- 三变体：`2-pass-fs`（推荐）等；内部资源含 Colorluma（R32UI）、PrevHistory。
- 关键技术：`textureGather` 采深度、深度一致性检测、8×8 计算着色器并行 64 像素、运动补偿重投影、9 点上采样、YCoCg↔RGB、边界框做历史颜色裁剪与时域混合。

价值

- 低分辨率渲染再升频，显著降 GPU 负载/功耗/发热，保留接近原生清晰度——补0 策略反复推荐的手段。

**适用场景：**理解移动超分原理、或在自研引擎集成/移植 SGSR 时的逐行参考。

## 番外篇解析

围绕具体 Vulkan 特性的专题，偏实现细节。

外1

### VK\_QCOM\_render\_pass\_shader\_resolve

用自定义着色器替代固定功能 MSAA 解析

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1958901619044378271)

允许用可编程片段着色器替代传统固定功能 MSAA 解析，实现更高质量抗锯齿或特殊后处理。

| 维度 | 传统硬件 MSAA 解析 | Shader Resolve |
| --- | --- | --- |
| 解析方式 | 固定硬件算法 | 可编程着色器 |
| 控制粒度 | 受限 | 高，可自由写解析逻辑 |
| 平台依赖 | 通用 | Qualcomm 设备专属 |
| 适用 | 标准抗锯齿，性能可预测 | 自定义过滤/特殊后处理 |

实现关键

- SubPass 设 `VK_SUBPASS_DESCRIPTION_SHADER_RESOLVE_BIT_QCOM`，着色器开 `GL_QCOM_render_pass_shader_resolve`，用 `subpassLoad(multisampledColor, i)` 读样本做自定义解析（文中有完整 C++/GLSL 示例）。

代价/限制

- 仅 Adreno 支持，跨平台需回退；需自写/调试解析着色器（约多 2–3 周）；实现不当性能可能不及固定功能；部分硬件 MSAA 特性需手动实现。

**适用场景：**主攻高通、对画质要求极高、需定制抗锯齿/融合后处理时；追求广兼容/赶工期则用标准硬件 MSAA。

外2

### 图面旋转的合理运用（Pre-rotation 预旋转）

在 Vulkan 中正确处理设备旋转，避免额外带宽

[阅读原文 ↗](https://zhuanlan.zhihu.com/p/1959223725527396495)

OpenGL ES 由驱动透明处理旋转，但 **Vulkan 规范要求应用自行处理**。

三种旋转方式

- **DPU 硬件旋转：**最高效透明，仅支持设备可用。
- **Android 合成器旋转：**引入合成通道，透明但有额外内存带宽/GPU 负载。
- **应用预旋转（推荐）：**直接渲到与显示面板物理方向匹配的窗口图面，省去合成器旋转——唯一能确保避免额外开销的方法。

实现要点

- 清单加 `configChanges="orientation|screenSize"`；用 `APP_CMD_CONTENT_RECT_CHANGED` 跟踪方向。
- 重建交换链时 `preTransform` 设为 `currentTransform`；旋转 90/270° 交换 width/height；给相机 MVP 乘 `pre_rotate_mat`。

实测影响

- 无 DPU 旋转设备（Mali-G72）：未预旋转时内存读停顿 12%→22%、写停顿 7%→17%，2D 旋转模块占大量带宽掉性能费电。

**适用场景：**Vulkan 移动应用横竖屏切换、排查旋转带来的额外带宽/耗电。

## 通读小结：一条主线、三组抓手

把 22 篇串起来，可以这样记忆这套方法论。

#### 测得准

骁龙分析器三模式+指标阈值（14）→ 帧率分诊 GPU/CPU/Vsync（16）→ 真机长跑防热节流（补0）→ 查询精确计时（12）。先量化，再优化。

#### 省带宽 / 省内存

Tile 架构是核心（02）：Clear/Invalidate 消除 GMEM Loads（01/15）、UBWC 压缩（03）、子通道合并、LRZ/Early-Z/Fast-Z 剔除（04）、预旋转省合成带宽（外2）、统一内存下管好生命周期（13）。

#### 用硬件 / 算法换效率

半精度/GPR/纹理调优（05/06/09）、VRS 降着色（08）、GPU 驱动渲染与网格着色（07）、光追（10）、QCOM 专属扩展（补1/外1）；HDR（11）+ SGSR 超分（补2）+ AFME 插帧（17）以更低原生负载换画质与流畅。

本报告基于知乎专栏「高通 Adreno GPU 最佳实践」系列原文整理，内容与观点归原作者「知秋」所有，仅作学习阅读用途。
报告生成：2026-06-23 · 共解析 22 篇全文（专栏第 0 篇为汇总目录，无独立正文）。

[↑](# "回到顶部")
