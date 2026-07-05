# 高通 SDP 性能热点定位完整资料库

# 高通 SDP 性能热点定位完整资料库

Snapdragon Profiler 移动端 GPU 瓶颈定位 — 穷尽式资料汇编（带来源标注）

整理于 2026-06-22 · 资料来源：高通官方文档 / 高通开发者博客 / Meta Quest 技术博客 / 腾讯互娱 / 业界实战文章

## 0. 全文核心结论速览

**这份资料想说清楚的一件事**
SDP 的系统级硬件 counter 能告诉你"是不是 GPU 瓶颈、大致卡在哪一类"，但**无法精确归因到某个 DrawCall / Shader**。要真正定位热点，必须用"**实验法（override / 降分辨率二分）+ Snapshot 逐 DrawCall + AOC/RenderDoc 看 Shader**"组合手段。单看聚合 counter 容易误判（尤其是被均值/聚合口径带偏）。

| 问题 | 核心答案 |
| --- | --- |
| 怎么判断是不是 GPU 瓶颈？ | Realtime 看 GPU % Utilization 持续 98-100% → GPU-bound；或关掉渲染（不画任何东西）帧率不变 → CPU-bound，帧率大涨 → GPU-bound |
| GPU-bound 后怎么分 fragment / vertex？ | 把 render scale 降到 0.01（几乎不画像素）。帧率大涨 = Fragment-bound；帧率不变 = Vertex-bound |
| Shaders Busy 高但 ALU Capacity 低 说明什么？ | 延迟受限（latency-bound），核心在等数据（纹理采样/内存），不是算力不够。常因 Occupancy 太低、无法隐藏延迟 |
| Occupancy 为什么低？ | Shader 寄存器（GPR）用太多，或指令数超指令缓存。寄存器不足导致 latency hiding 切换失败 |
| 哪个工具看 Shader 最准？ | AOC（Adreno Offline Compiler）离线看指令数/寄存器；已集成进 UE5 Shader Stats 窗口 |

## 1. 工具全景与定位

Snapdragon Profiler (SDP)

旗舰实时系统分析工具。Realtime/Trace/Snapshot/Sampling 四模式，150+ 硬件计数器，支持 Vulkan/GLES/CL/DX。运行时分析 CPU/GPU/DSP/thermal。

Adreno Offline Compiler (AOC)

离线 Shader 优化。看指令数、寄存器占用、内存访问。运行前调优 Shader。已集成进 UE5 材质编辑器 Shader Stats。

Qualcomm Profiler（新版）

新一代系统级工具。Host Streaming + On-Device 两种模式，GUI/CLI/C-API。输出 Perfetto 格式，适合自动化/CI。

RenderDoc

开源帧调试器。逐 DrawCall、管线状态、Shader 源码级调试、像素历史。注意：自带计时不准（Hook 开销），严谨计时仍需 SDP。

Adreno Frame Motion Engine 2.0

帧生成/帧外插技术，降低 GPU 负载。

Snapdragon Game Toolkit / SGSR2

性能提示（DVFS hint）+ 超分辨率上采样，省 GPU。

来源：[高通开发者博客 2025/08 低功耗优化指南](https://www.qualcomm.com/developer/blog/2025/08/optimize-performance-and-graphics-for-adreno-gpu-low-power-gaming)、[SDP 官方页](https://www.qualcomm.com/developer/software/snapdragon-profiler)

## 2. 官方瓶颈定位方法论

来自高通官方文档"Identify application bottlenecks"。性能问题归为三大类：**GPU 受限、CPU 受限、Vsync 受限**。

### 2.1 先看帧率（起点）

- **平均帧率**：游戏通常目标 30 或 60fps
- **帧率一致性**：即使平均接近目标，偶发长帧也会造成卡顿/抖动。官方示例：平均 42fps 但周期性掉到 37.3fps = 在丢帧
- 应用应选择 Vsync 的整数倍/约数：60Hz 屏 → 只有 30Hz 或 60Hz 是合理目标

### 2.2 GPU-Bound 判定

- Realtime 视图：**GPU % Utilization** 是顶级指标。26-38% = 非 GPU 瓶颈；**98-100% = 强烈暗示 GPU-bound**
- Trace 模式佐证：非 GPU-bound 时 GPU activity 有"间隙"；GPU-bound 时 GPU 执行被延迟、持续在渲染 surface（无间隙）

### 2.3 CPU-Bound 判定

- CPU % Utilization **不是**可靠指标（多核特性，CPU-bound 的应用平均利用率也可能只有 16%）
- 两个强判据：① 不是 GPU-bound ② 平均帧时间 > 16ms
- 看 **CPU Scheduling（Trace Kernel - Sched CPU）**，找是哪个核心、哪个函数是瓶颈（官方案例：定位到 SatisfyConstraints 函数，多线程化后解决）

### 2.4 Vsync-Bound 判定

- 不是 GPU 也不是 CPU → 可能 Vsync 受限（已达屏幕刷新上限）
- 特征：帧时间约 16ms（60fps），帧末尾 CPU 和 GPU 都在等 surface 渲染
- 即使 Vsync 受限，仍有优化潜力（尤其是降功耗）

### 2.5 三大关键 Trace 指标

| 指标 | 含义 |
| --- | --- |
| Rendering Stages | 应用在 GPU 上的执行。每条 track 是子集，surface 条下的 track 代表相关渲染阶段 |
| GPU Activity | 系统指标，显示 CPU 与 GPU 的交互 |
| CPU Scheduling | 系统指标，应用在每个 CPU 核心上的执行概览，看调度/线程争用 |

来源：[高通官方 Identify application bottlenecks](https://docs.qualcomm.com/nav/home/identify_application_bottlenecks.html)、[中文实战转述](https://likecs.com/show-306896698.html)

## 3. GPU-Bound 的二分实验法（最实用）

这是高通 2015 发布会就主推的核心手段，也是 Meta Quest 团队的标准流程——**改一个变量，看帧率反应，反推瓶颈**。

### 3.1 第一步：CPU 还是 GPU？

**关掉渲染法**
不画任何东西（关掉 render camera），消除整个渲染管线开销（culling、提交 DrawCall、跑 Shader）。
帧率几乎不变 → CPU-bound　帧率大涨 → GPU-bound

### 3.2 第二步：Fragment 还是 Vertex？

**降 render scale 到 0.01 法**
把渲染分辨率降到极低（如 0.01），几乎不画像素但保留场景复杂度。
帧率大涨 → Fragment-bound（Shader/填充率问题）　帧率不变 → Vertex-bound（几何复杂度问题）

各引擎设置 render scale 的方式：

```
# Unreal Engine
UHeadMountedDisplayFunctionLibrary::SetScreenPercentage(0.01f);
# 或控制台： r.ScreenPercentage 1

# Unity
UnityEngine.XR.XRSettings.eyeTextureResolutionScale = 0.01f;

# WebGL：把 canvas/FBO 缩到 1x1 像素
```

### 3.3 SDP Realtime override 实验

SDP Realtime 模式可实时开关功能，看帧率反应：

| override 操作 | 帧率回升 → 瓶颈在 |
| --- | --- |
| 强制降低纹理尺寸 | 纹理采样 / 带宽 |
| 替换成极简 shader | Fragment 着色（ALU） |
| 禁用 back-face culling / 移除某 GPU call | 对应功能 |
| 降低渲染分辨率 | Fragment / 填充率 |

来源：[Meta Quest: Profiling & Optimizing on Mobile](https://developers.meta.com/horizon/blog/tech-note-profiling-optimizing-on-mobile-devices/)、[高通 2015 SDP 发布文](https://www.qualcomm.com/news/onq/2015/07/snapdragon-profiler-find-bottlenecks-and-optimize-your-code-faster)

## 4. 四大模式与使用流程

| 模式 | 用途 | 关键能力 |
| --- | --- | --- |
| Realtime | 快速定位异常波动 | 150+ counter 实时曲线（CPU/EGL/GPU/Memory/Network/Power/Primitive/System Memory/Thermal） |
| Trace | 离线时间线分析 | 微秒级记录内核+驱动事件，Vulkan/GLES/DSP 等组件，Rendering Stages |
| Snapshot | 单帧逐 DrawCall 调试 | DrawCall 列表、资源(FBO/纹理/Shader)、Shader 复杂度分析、像素历史、Overdraw 分析、纹理预览、帧统计 |
| Sampling | CPU 函数级火焰图 | 定位 CPU 热点函数 |

### 4.1 Snapshot 逐 DrawCall 定位（精确归因唯一手段）

1. New Snapshot Capture → 选进程 → Take Snapshot
2. 下方列出该帧所有 GL/Vulkan 指令和 DrawCall，**按 GPU 耗时排序找最贵的**
3. 双击某 DrawCall：右上 **Resources → Textures** 看用到的所有纹理；**Resources → Program** 看 Shader，点 ID 后内容在左下 **Shader Analyzer**
4. 用 **Pixel History** 追踪某像素被哪些 DrawCall 写入；**Overdraw 分析**找过度绘制
5. 可热编辑 Shader、查看纹理 mip 层级，无需重新打包

来源：[SDP 官方文档](https://docs.qualcomm.com/doc/80-78185-2/topic/sdp.html)、[SDP 抓取纹理和 shader 实操](https://pianshen.com/article/6870273841/)、[Techbeaz Vulkan 工作流示例](https://techbeaz.com/what-is-qualcomms-snapdragon-profiler-and-why-use-it)

## 5. Trace 指标推荐阈值大全

以下全部来自 SDP 官方文档（80-78185-2），是定位时对照判断的权威依据。

#### GPU Shader Processing

| 指标 | 方向/理想值 |
| --- | --- |
| % Shader ALU Capacity Utilized | 越高越好，理想 50-100% |
| % Shaders Busy | 越高越好，理想 50-100% |
| % Shaders Stalled | 越低越好，理想 <10% |
| % Wave Context Occupancy | 越高越好，平均至少 50%（可有尖峰） |
| % Time ALUs Working | 越高越好，理想 50-100% |
| % Time EFUs Working | 越高越好，理想至少 20% |
| % Time Shading Fragments | 传统管线理想 100% 占帧 ≥60% |
| % Time Shading Vertices | 传统管线理想 100% 占帧 <10-20% |
| % Texture Pipes Busy | 美学约束内最小化；20-30% 也可能瓶颈，接近 100% 也可能不瓶颈 |
| ALU/Fragment、ALU/Vertex | 越高越好（可有尖峰） |
| Fragment ALU Instructions (Full) | 越低越好（应远低于 Half） |
| Fragment ALU Instructions (Half) | 越高越好（应远高于 Full） |
| Interpolation Instructions/Fragment | 越低越好（可导致 stall） |
| Textures/Fragment | 越低越好（尤其 VS 中） |

#### GPU Stalls

| 指标 | 理想值 |
| --- | --- |
| % Instruction Cache Miss | 尽量低；某些情况尖峰 80% 也未必瓶颈 |
| % Stalled on System Memory | 通常 <2%，短尖峰可到 30% |
| % Texture Fetch Stall | 通常 <2%，短尖峰 ≤20%；**持续 ~16%+ 通常过高** |
| % Texture L1 Miss | 0% 到 <50% 间波动，偶尔尖峰 |
| % Texture L2 Miss | 0% 到 <40% 间波动，偶尔尖峰 |
| % Vertex Fetch Stall | 通常 0%，尖峰不超 70% |

#### GPU General / Memory

| 指标 | 说明 |
| --- | --- |
| % CP Overhead | 应始终接近 0%，绝不超 20% |
| GPU % Bus Busy | 省电应用平均 ≤25%；满性能应用可达 90% |
| GPU % Utilization | 省电应用平均 ≤40%；满性能应用 90%+ |
| Avg Memory Latency Cycles | 越低越好，大尖峰=慢 Shader |
| Texture Memory Read BW | 越低越好，大尖峰=慢 Shader（AGI 推荐：均值 ≤1GBps，峰值 ≤3GBps） |
| Vertex Memory Read | binning 慢时可判因：高读=低带宽，低读=stall |
| Write Total | 越低越好，写主存很贵 |

#### GPU Primitive Processing

| 指标 | 理想值 |
| --- | --- |
| Average Polygon Area | 理想 ≥4 像素，但不要远大于 bin 尺寸 |
| % Prims Clipped | 越低越好，理想 <2% |
| % Prims Trivially Rejected | 越低越好，理想 <2% |
| Reused Vertices | 越高越好，通常表示用了索引绘制 |

#### Vulkan / Binning

| 指标 | 说明 |
| --- | --- |
| Binning pass 占比 | 理想 10-20%，30% 通常太多 |
| Concurrent binning / Per drawcall stages / Rendering stages | 高层可见性 |

来源：[SDP 官方文档 80-78185-2](https://docs.qualcomm.com/doc/80-78185-2/topic/sdp.html)、[高通骁龙性能分析器指标解读（中文）](https://jackwzx.github.io/2025/12/08/高通骁龙性能分析器指标解读/)、[Android AGI 纹理带宽](https://developer.android.google.cn/agi/sys-trace/texture-memory-bw)

## 6. 关键机制：延迟受限 vs 算力受限

| 瓶颈类型 | 核心矛盾 | 关键指标 | 优化方向 |
| --- | --- | --- | --- |
| Compute-Bound（算力受限） | ALU 吞吐 < 指令需求 | 算术强度、ALU Capacity 高 | 减指令、优化分支、降复杂度 |
| Memory/Latency-Bound（延迟受限） | 带宽/延迟 < 数据需求 | Cache 命中率、Shader Stalled、Occupancy 低 | 纹理压缩、优化数据布局、提升缓存局部性、降寄存器压力 |

**判读要点：Shaders Busy 高 ≠ 算力满**
"% Shaders Busy 高"只说明 SP 核心被占着，但若同时 "% Shader ALU Capacity Utilized 低"，说明核心大部分时间在**等数据（纹理采样/内存）而非做运算**——这是延迟受限，不是算力不够。此时优化 Shader 指令数收益有限，应优先解决延迟隐藏问题（降寄存器、提 Occupancy、降纹理延迟）。

#### % Shader Stalled 精确定义

"总周期中，没有任何执行单元（主要 ALU、texture、load/store）在工作的周期占比。" 注意：**内存取数停顿不一定算 Shader Stalled**——只要还能找到另一个 wave 执行 ALU 就不算。该指标上升 = IPC 下降。当 SP 无法切换到其他 shader 执行时才发生 stall。

来源：[TrueSight: 穿透延迟-GPU渲染流水线瓶颈解构](https://tsight.io/articles/17603136)、[Debunking GPU Performance Myths](https://boardor.com/blog/debunking-gpu-performance-myths)、[腾讯云: GPU性能原理拆解](https://cloud.tencent.com/developer/article/2442873)

## 7. 寄存器压力与 Occupancy（移动端核心）

#### Latency Hiding 机制

SP 执行 Shader 遇到长延迟操作（如纹理采样）时，会切换到另一个 Shader 执行而非空等——这就是隐藏延迟。**切换成功的前提**：寄存器能在保留原 Shader 上下文的同时容纳新 Shader 上下文。寄存器不够 → 切换失败 → Occupancy 降低 → 无法隐藏延迟 → stall。

#### 移动端 vs 桌面端寄存器差异

| 平台 | 寄存器容量 |
| --- | --- |
| 骁龙 888（移动） | 每 64 ALU 仅 64KB |
| NVIDIA/AMD（桌面） | 每 64 ALU 有 256KB |

桌面 Shader 直接搬到移动端，**不会因指令数线性掉性能，但会因寄存器不足而严重劣化甚至 Register Spilling（溢出到系统内存，性能断崖）**。

**最典型踩雷组合**
"大量纹理采样 + 复杂计算"同时存在的 Shader 最危险：纹理采样制造延迟（需要 latency hiding），复杂计算占用大量寄存器（导致切换失败）——两者叠加无法隐藏延迟。官方建议避免这种 Shader。

#### 指令数限制

- **编译后 Shader（VS+PS）不应超过 2000 条指令**，否则指令缓存不足，% Instruction Cache Miss 升高
- 低 % Wave Context Occupancy 可能表示长 Shader 在抖动指令缓存（thrashing I-cache）
- GPR 最小化：保持每个 Shader 寄存器用量在设备限制内，最大化同时执行的 wave 数。改 GLSL 省一条指令就可能省一个 GPR

#### Vertex Shader 为何移动端敏感

1. TBR 下跨 tile 三角形在每个 tile 都执行；开 MSAA 后 tile 变小，跨 tile 三角形更多 → VS 压力增大
2. Adreno/Mali 上 VS 执行两次（binning pass 只执行位置相关指令）；若位置计算含复杂运算/纹理采样（如地形高度图采样顶点位置），开销翻倍
3. Adreno 上 VS output 存在 SP 本地 buffer（8Gen2 仅 8KB），满了会 VS stall（例：12 个 float4 属性 → 8KB 只容 64 fragments）

来源：[Debunking GPU Performance Myths（腾讯互娱 Mobius Chen）](https://boardor.com/blog/debunking-gpu-performance-myths)、[Adreno Mobile Best Practices](https://docs.qualcomm.com/nav/home/mobile_best_practices.html)

## 8. Adreno TBDR 架构要点

**核心认知**
Adreno 用 Tile-Based 架构，G-Buffer 读写在**片上 Tile Memory (GMEM)** 完成，中间 Pass 不走 DDR，只有最终 Store SceneColor 才写 DDR。所以移动端 Deferred 的 G-Buffer 读写开销在 SDP 带宽指标里几乎看不到——**移动端 Deferred 的瓶颈通常不是带宽，而是 ALU/延迟**。

| 特性 | 要点 |
| --- | --- |
| FlexRender™ | 帧中途动态在 binning/GMEM 模式 与 direct/系统内存模式 间切换。两种都要优化（理想用 Vulkan 扩展） |
| Bin 最小化 | 最小化 bin 数量。理想屏幕空间三角形 ≥4 像素，且不远大于一个 bin |
| Concurrent Binning | 每个 DrawCall 应产生足够 fragment 工作以与下个 DrawCall 的 binning 并行。最小化阻碍并发 binning 的依赖（renderpass/barrier/Z-clear） |
| UBWC | 通用带宽压缩，提升内存总线吞吐、降功耗 |
| LRZ / Early-Z / Fast-Z | 低分辨率 Z + 早期深度剔除，别禁用 |
| GMEM 手动优化 | 正确配置 RenderPass 的 load/store，避免不必要的 Tile↔系统内存拷贝 |
| Query 开销 | 对 binned surface 发太多 query 会使 % CP Busy 增 20-40%；切 direct 模式可降到 4-6%。Timer query 应在 renderpass 内发以最大化精度 |

来源：[Adreno GPU on Mobile: Best Practices](https://docs.qualcomm.com/bundle/publicresource/topics/80-78185-2/mobile_best_practices.html)、[Game Developer Guide](https://docs.qualcomm.com/doc/80-78185-2/topic/get_started_wos.html)

## 9. Shader 优化（官方最佳实践）

| 优化项 | 具体做法与原因 |
| --- | --- |
| 半精度优先 | 16 位 ALU 比 32 位性能更好、省寄存器、省功耗、提并行度。尽量用 mediump，仅必要时 highp。2 个 mediump = 1 个 highp 开销 |
| 指令数适配 I-Cache | 编译后 ≤2000 指令(VS+PS)。超限考虑拆分 Shader。LPAC compute shader 指令上限略高 |
| GPR 最小化 | 寄存器用量低于设备限制，最大化并发 wave。不展开循环可省 GPR（展开会把纹理采样堆到顶部，需更多 GPR 同时保存坐标和结果） |
| 拆分 DrawCall | 长 Shader 拆成多个 |
| 最小化 ALU 成本 | 删冗余计算 |
| 避免 discard 像素 | fragment shader 中 discard 破坏 Early-Z/HSR 效率（延迟深度写入） |
| 避免改深度 | fragment shader 中改深度同样破坏 Early-Z |
| 最小化纹理采样 | 尤其 VS 中的纹理采样。Texture read 高时：合并贴图、减少层数、避免多次采样同一纹理 |
| EFU 指令昂贵 | sin/cos/pow/log 等 EFU 比 ALU 耗时，且需短延迟同步指令。考虑 LUT 预计算 |

来源：[Adreno Mobile Best Practices - Shaders](https://docs.qualcomm.com/nav/home/mobile_best_practices.html)、[性能测试之 shader 测试](https://blog.csdn.net/sbwshishi/article/details/130892383)

## 10. Overdraw 与填充率

同一像素一帧内被多次绘制 = Overdraw，移动端 TBR 下带宽消耗尤其严重。每次绘制都要读写帧缓冲。

| 物件类型 | Overdraw 行为 | 优化 |
| --- | --- | --- |
| Opaque（不透明） | Early-Z/FPK 自动排序剔除遮挡片段，影响小 | 从近到远排序渲染 |
| AlphaTest | 深度写入需在 FS 后才确定，延迟深度写入破坏 TBDR 的 HSR 效率 | 从前往后排序；美术减小三角形面积 |
| AlphaBlend | 可被 Opaque 的 Early-Z 剔除，但不写深度，彼此间无法 Early-Z 剔除，叠加产生 Overdraw | 降半透明叠加层数；缩减屏幕渲染面积；用不规则面片代替矩形 |

**判断填充率瓶颈**
确保带宽没瓶颈后（已用压缩纹理），如果**降低分辨率帧率立刻上去**，很可能是像素填充率瓶颈，检查 Overdraw 是否合理。**粒子特效是最常见的 Overdraw 大户**：建议单个复合粒子系统子特效 ≤5 个，控制粒子屏幕面积，纹理透明区域尽量少，低端机分级。

来源：[腾讯 WeTest PerfDog GPU 指标说明](https://wetest.qq.com/documents/detail/perfdog/zvGW5dlO)、[手游渲染优化三要素](https://blog.csdn.net/weixin_49393016/article/details/108138910)、[Unity OverDraw 优化手册](https://xinzhuzi.github.io/2020/05/08/Unity/2020-05-08-Unity%20OverDraw%E4%BC%98%E5%8C%96)

## 11. Adreno Offline Compiler (AOC)

离线分析 Shader 的指令数和寄存器，**比"ALU 总数"更能决定性能**。已集成进 UE5 Shader Stats 窗口（Meta Quest fork）。

```
# 命令行用法（支持 A650/A660/A730/C510/C511 等架构）
aoc.exe -arch=a650 file/*.frag file/*.vert
```

| AOC 指标 | 含义/优化 |
| --- | --- |
| Total instruction count | 总指令数=所有带指令项之和。超 I-Cache 会 miss。避免冗余操作 |
| ALU instruction count 32bit | 更多 ALU 可能不影响性能但更耗电。删冗余计算 |
| ALU instruction count 16bit | 16 位比 32 位性能好、省寄存器。把 full 转 half 提升 ALU-bound shader 性能 |
| Complex instruction count | sin/cos 等 EFU 指令，比 ALU 耗时 |
| Register footprint (Full/Half/Overall) | **移动端最关键**：寄存器用量↑ → 并发线程↓ → 吞吐↓。例：High/Epic overall=13，Medium=11，差异真实影响并发 |
| Texture read | 高则优先：合并贴图、减少层数、减少法线/细节叠加、避免重复采样 |

**AOC 使用建议**
推荐用 AOC 看"改材质属性后的性能**趋势**"，不建议用它对比两个差异巨大的材质（如一个有流控+多内存访问、另一个高寄存器压力，难以估算差异）。需通过 Qualcomm Package Manager 下载。

来源：[Meta Quest: UE 材质 Shader 用 AOC 优化](https://developers.meta.com/horizon/blog/unreal-engine-adreno-offline-compiler-meta-quest/)、[UE AOC 下载配置教程](https://blog.csdn.net/qq_41835314/article/details/158459560)、[高通低功耗优化博客](https://www.qualcomm.com/developer/blog/2025/08/optimize-performance-and-graphics-for-adreno-gpu-low-power-gaming)

## 12. RenderDoc 配合定位

**重要：RenderDoc 计时不可信**
RenderDoc 的小闹钟（per-DrawCall 计时）**绝对不准确**——其自身 Hook 开销会影响计时。严谨的性能计时必须用 Snapdragon Profiler 或 Nsight。RenderDoc 的强项是**结构调试与归因**（看绑了什么、Shader 源码、像素历史），不是计时。

| RenderDoc 能力 | 说明 |
| --- | --- |
| Event Browser | 按管线层级或 Timeline 视觉定位。点击耗时长的长条大概率直达核心 Pass（UI Pass 极短，主场景 G-Buffer/Lighting 长） |
| Pipeline State | 逐 DrawCall 管线状态、绑定的 Shader/Buffer/Texture |
| Shader Viewer | SPIR-V/GLSL 源码级调试，单步执行看寄存器变化，实时编辑热重载 |
| Pixel History | 追踪单像素所有绘制操作、颜色变化，识别 Overdraw |
| Performance Counter Viewer | 像素输出、Shader 执行时间、缓存命中率（仅参考） |
| Python 脚本 | 自动化分析流程 |

#### UE5 集成

```
Project Settings → Plugins → RenderDoc
- 勾选 Auto-attach on startup
- 指定 qrenderdoc.exe 路径
- 重启编辑器，Level Viewport 点 RenderDoc Capture 按钮 或 F12 截帧
- 截帧文件 .rdc 默认存 %TEMP%
```

Android Vulkan 注意：需驱动支持 `VK_EXT_debug_utils`；GLES 需 Manifest 声明 `debuggable="true"`。截帧失败查 `adb logcat | grep RenderDoc`。

来源：[RenderDoc 实战指南](https://nothingtosay0031.github.io/Engine/RenderDoc)、[Arm: RenderDoc with UE](https://learn.arm.com/learning-paths/mobile-graphics-and-gaming/nss-unreal/5-renderdoc)、[Android RenderDoc 移动端定位](https://blog.gitcode.com/abe2270d1e7f1ece352d5abd6cdffbc4.html)

## 13. CLI 自动化与 Perfetto

新版 Qualcomm Profiler 提供 CLI（qprof），适合 CI/批量分析，输出 Perfetto 格式。

```
# Android 端 setup（每个新 adb shell 都要）
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/vendor/qprof/libs

# 主机配置（通过 IP 连设备 server）
qprof --configure --server-ip  --port

# 列能力
qprof --capabilities
# 生成配置
qprof --generate-config
# 开始 profiling（指定能力列表、时长、采样率、实时输出、CSV/Perfetto）
qprof --profile --capabilities-list

 --profile-time  --live --result-format csv
```

CLI 关键特性：并发分析 GPU/CPU/NSP、动态发现能力、可配置时长采样率、可先启动应用再 profiling、Perfetto 格式输出（可在 ui.perfetto.dev 或 WPA 查看）。

#### 配合系统级 Perfetto

```
# 快速抓 10s（CPU调度+图形+binder）
adb shell perfetto -o /data/misc/perfetto-traces/demo.pftrace -t 10s \
  sched freq idle am wm gfx view binder_driver hal
adb pull /data/misc/perfetto-traces/demo.pftrace .
# 浏览器打开 ui.perfetto.dev 可视化
```

来源：[Qualcomm Profiler CLI 官方文档](https://docs.qualcomm.com/bundle/publicresource/topics/80-54323-2/command-line-interface.html)、[Android Perfetto 文档](https://developer.android.google.cn/tools/perfetto)

## 14. 热节流与功耗

**性能是设备热起来后的数字，不是 benchmark 上的数字**
持续高负载触发过热降频（DVFS→migration→hotplug），帧率断崖。例：Pixel 8 Pro 持续推理 ~4 分钟后 GPU 温度达 48°C，频率从 900MHz 强降到 315MHz，延迟从 130ms 跳到 410ms。恢复有迟滞：温度需持续低于阈值 10-15 秒才逐步恢复。

#### 测试规范（官方）

- **至少连续游玩 10 分钟**，跟踪 CPU/GPU 时钟。若负载稳定但时钟显著下降 = 热节流
- 用**真机**不用模拟器；跨 Snapdragon 芯片档位测试；插电和不插电都测
- SDP 仅 trace 帧率就有约 5% CPU 开销，帧时间会略慢于无 profiler
- 测试间隔室温（21°C）冷却至少 20 分钟，尤其上次有热节流时
- 加载画面性能特性与游戏内不同，注意区分

#### 降功耗对策

- 用 SDP thermal 指标驱动动态画质：热压力下先砍非必要后处理，保帧率
- SGSR2 超分：低分辨率渲染上采样，省 GPU
- Snapdragon Game Toolkit 提交 power/performance hint，利用 DVFS
- VRR（可变刷新率）：低动态场景（菜单/过场）降刷新率
- 用 JobScheduler/WorkManager 处理可延迟任务，监控 wake lock 避免空闲耗电
- 建场景化性能档（战斗 vs 过场）

来源：[高通低功耗优化博客](https://www.qualcomm.com/developer/blog/2025/08/optimize-performance-and-graphics-for-adreno-gpu-low-power-gaming)、[SDP Testing Suggestions](https://docs.qualcomm.com/doc/80-78185-2/topic/sdp.html)、[Android 功耗热管理实战](https://xckevin.com/en/blog/android-on-device-ai-power-thermal-management)

## 15. UE5 移动端专项

#### 引擎内 GPU 分析命令

| 命令 | 作用 |
| --- | --- |
| stat unit | 显示 Game/Draw/GPU 各耗时。GPU 远高于 Game/Draw = 渲染压力大 |
| stat GPU | GPU 各阶段耗时分解（BasePass/ShadowDepths/PostProcessing 等）。部分 Vulkan 设备支持 |
| profilegpu | 生成火焰图式 GPU 可视化报告，看每毫秒 GPU 在干什么。优化前后各跑一次对比 |
| stat UnitGraph | CPU/GPU 利用率随时间曲线，识别尖峰 |
| stat TextureGroup | 各纹理池内存占用 |

移动端开发者控制台：**四指同时点屏**（仅 Development 构建可用）。

#### 关键 .ini 配置

```
[/Script/Engine.RendererSettings]
r.GBufferFormat=2          # GBuffer 从 16bit 浮点降 10bit 整数，带宽降~40%，Adreno 优化好
r.BasePassOutputsVelocity=0  # 关运动矢量输出（除非要 Motion Blur）
r.DepthOfFieldQuality=0      # 移动端关景深
```

#### 渲染管线选择

- 移动端/XR 优先考虑 **Forward Renderer**（比 Deferred 更适合，可单独关功能）
- 移动 VR：开 Instanced Stereo + Mobile Multi-View，关 Mobile HDR
- Normal Map vs 高模：UE 移动渲染器擅长渲染大量顶点，而高质量法线图在移动端有位深问题且成本可能高于高模。16bit 法线图未压缩、8 倍大小，像素成本可能超过高密度网格的顶点成本
- VSM（虚拟阴影图）在移动端是带宽杀手，注意开销

来源：[UE5 移动端优化官方文档](https://docs.unrealengine.com/5.0/en-US/optimization-and-development-best-practices-for-mobile-projects-in-unreal-engine)、[UE5 安卓 .ini 性能优化](http://www.hqwc.cn/a/2363338.html)、[UE 渲染控制台参数调优](https://blog.csdn.net/weixin_29015801/article/details/158625674)

## 16. 实战案例库

#### 案例 1：米哈游《原神》Snapdragon 深度优化

- 利用 Adreno TBDR 特性重构延迟渲染管线
- 精细化 Subpass 配置 + 纹理压缩策略 → 渲染管线内存带宽消耗**降 42%**
- 骁龙 8 Elite 最高画质稳定 60fps，设备表面温度 <42°C
- 用 Hexagon DSP 卸载部分 AI 植被动态和天气计算，释放 GPU/CPU

#### 案例 2：《星际战甲》手游战斗场景卡顿

- 帧捕获看 DrawCall 分布 → UI 层占 43% 调用
- 检查 Shader 复杂度 → 角色阴影计算异常（实时 PCF 改预计算+采样）
- 纹理带宽 → 4K 贴图未做 Mipmap
- 结果：帧时间 22ms→16ms(↓27%)、DrawCalls 215→148(↓31%)、带宽 5.2→3.8GB/s(↓27%)

#### 案例 3：VR 双眼渲染不均衡

- Realtime：左眼 GPU 95%，右眼 50% → 不均衡
- 切 Trace 看线程时间线 → 一个 CPU 线程每帧延迟，导致右眼提交滞后
- 修线程问题 → 双眼并行，VR 运动平滑

#### GPU Stall 三大常见原因

- 纹理采样等待过长（启用压缩格式缓解）
- VS 输出与 FS 输入不匹配
- Compute Shader barrier 同步开销

来源：[米哈游原神 Snapdragon 优化实录](https://demo.gotribe.cn/news/14)、[SDP 实战教程](https://blog.csdn.net/5b6n7m8/article/details/155550356)、[Techbeaz VR 案例](https://techbeaz.com/what-is-qualcomms-snapdragon-profiler-and-why-use-it)

## 17. 完整定位流程图

1. stat unit/SDP Realtime 看帧率+GPU%
→
2. 关渲染：CPU 还是 GPU bound

3. 降 render scale 0.01：Fragment 还是 Vertex
→
4. SDP Trace 抓最贵的几帧，对照阈值表

5. Realtime override 二分（关某 Pass/降纹理/换简易 Shader）
→
6. SDP Snapshot 逐 DrawCall 按耗时排序

7. AOC/RenderDoc 看罪魁 Shader 的指令数+寄存器
→
8. 优化 → 重新 Trace/Snapshot 对比验证

**判读决策树速记**

- GPU% 低 + CPU 某核满 → CPU-bound，看 CPU Scheduling 找函数
- GPU% 高 + 降分辨率帧率大涨 → Fragment/填充率，查 Overdraw + Shader 复杂度
- GPU% 高 + 降分辨率帧率不变 → Vertex/几何，查 DrawCall 数 + 顶点数 + LOD
- Shaders Busy 高 + ALU Capacity 低 + Occupancy 低 → 延迟受限，查寄存器压力 + 纹理采样 + I-Cache
- % Stalled on System Memory 高 → 真带宽瓶颈（少见于纯 Deferred 中间 Pass），查纹理压缩/Mipmap
- 负载稳定但时钟下降 → 热节流，做动态画质

## 18. 来源清单

#### 高通官方

- SDP 官方文档（80-78185-2）：指标阈值、四模式、Vulkan、Testing Suggestions
- Identify application bottlenecks：GPU/CPU/Vsync 三类判定
- Adreno GPU on Mobile: Best Practices：TBDR、Shader、Z-buffer、纹理
- Game Developer Guide：FlexRender、Concurrent Binning、Tile Shading
- Qualcomm Profiler CLI 文档（80-54323-2）
- 高通开发者博客 2025/08：低功耗优化、AOC、热测试
- 高通 2015 SDP 发布文：override 实验法

#### 第三方权威

- Meta Quest 技术博客：CPU/GPU/Fragment/Vertex 二分法、UE+AOC 集成
- Boardor《Debunking GPU Performance Myths》（腾讯互娱）：寄存器压力机制
- 腾讯云 GPU 性能原理拆解、腾讯 WeTest PerfDog GPU 指标
- Android AGI 纹理带宽、Android Perfetto、Arm RenderDoc UE 集成
- UE5 官方移动端优化文档
- 米哈游原神 Snapdragon 优化实录
- jackwzx 高通骁龙性能分析器指标解读（中文）

**资料使用提醒**
本文阈值与方法均标注来源，但**所有阈值都是"参考区间"而非硬指标**——不同 Adreno 代际、不同应用类型差异很大。定位时应"对照阈值找异常方向 → 实验法验证 → Snapshot/AOC 精确归因"，不要单凭某个聚合 counter 数值下结论。
