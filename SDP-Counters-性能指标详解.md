# Snapdragon Profiler 性能指标详解

# Snapdragon Profiler 性能指标详解

Adreno GPU 性能分析 · 基于 SDP v2022.5 "System Trace Analysis" Mode · 以 8gen2 模块配比为例

Snapdragon Profiler（SDP）是高通提供给用户用于对 Adreno GPU 做性能分析的工具，类似于 NVIDIA NSight。虽然功能不如 NSight 强大，但灵活运用确实能带来很多 GPU 内部的运行时信息。

SDP 的性能指标是按 **Surface** 采样的。在 Direct Mode，一般指具有相同 FrameBuffer Binding 的连续 Draw Calls；在 Binning Mode 下，Binning Pass 有自己的采样，每一个 Bin 的 Render Pass 也有自己的采样。鼠标悬停到条状图上，会显示这段 Surface 的 Bin Size、Binning Mode 及 Framebuffer 信息，以便与 API 层渲染步骤对应。

总体来说，SDP 的 Metrics 设计不算太合理，缺乏对 Bandwidth Utilization 的呈现（即 NSight 旧术语里的 SOL，Speed of Light），无法快速得到各 Engine Working 的全貌。

硬件背景：8gen2 模块配比

8gen2 有 6 个 Shader Core（8gen1 为 4 个）。每个 Shader Core 含两个 micro shader core，每个 micro shader core 含两个 64-lane ALU cluster。按 680MHz 计算，32bit float 算力约 **2.12 TFLOPs**。

每个 micro shader core 对应一个 Texture Engine（每周期可为两个 Pixel Quad 计算 Half-precision 双线性过滤）；每个 Shader Core 对应一个 Raster Backend（每周期可为两个 Pixel Quad 计算 Half-precision Blending）。这种 256ALU / 16TEX / 8ZC 的配比在各种 GPU 中很常见。

对比：NVIDIA 的 ALU 是 16 个一组，因此不太怕 Shader Divergence；独立 GPU 的 Raster Backend 配比更低（Shader 更复杂、RB 工作量比例小），且有显存，在 Z/C Caching 上压力较小。Adreno 每个 micro shader core 的 GPR register file 比 NVIDIA 小，但比上一代 A6xx（骁龙 888）大。

## ★ 关键指标速览

作者标注的重要指标，定位性能瓶颈时优先关注。点击跳转到详细说明。

[Read Total Bytes](#m-readtotal)
[Write Total Bytes](#m-writetotal)
[Pre-clipped Polygon](#m-preclipped)
[Reused Vertices](#m-reused)
[% Shader ALU Capacity Utilized](#m-aluutil)
[% Shader Busy](#m-shaderbusy)
[% Wave Context Occupancy](#m-occupancy)
[Fragment Shaded](#m-fragshaded)
[Vertices Shaded](#m-vertshaded)

全部
★ 重要
导出量

没有匹配的指标，换个关键词试试。

## GPU General

`Clocks`

表示此 Surface 的总周期数。

是什么当前采样 Surface（一组共享 FrameBuffer 的连续 Draw Call，或 Binning Pass、单个 Bin 的 Render Pass）从开始到结束所经历的 GPU 时钟周期总数。它是这段 Surface 的「时间标尺」——所有百分比类指标(% Shader Busy、% Stalled…)的分母本质都是它。

意义单独看 Clocks 没有好坏,它的价值在于**当除数**:把 Fragment Shaded、Read Total Bytes、指令数等绝对量除以 Clocks,才能换算出「每周期吞吐」「带宽利用率」等真正可比的密度指标。也可用于横向对比两段 Surface 谁更耗时。

注意Clocks 是**周期数不是时间**。GPU 降频时同样的工作量 Clocks 不变但墙上时间变长,所以分析热降频问题要结合 GPU Frequency 一起看,不能只看 Clocks。Binning Mode 下每个 Bin 各自采样,单 Bin 的 Clocks 会被切得很碎。

## GPU Memory Stats

Memory Path 的数据传输关系：Display Engine 理解 GPU 的压缩算法；CPU 和 GPU 各有自己的 MMU，所以 GPU Virtual Address 只给 GPU API 使用；GMEM 用于 Tiled Rendering 架构下存放每个 Bin 的 Z/C 数据。

`Avg Bytes / Fragment`导出量

字面意义，用处不大。Avg Bytes 指总线上的纹理数据量。

是什么总线上的纹理数据量 ÷ Fragment Shaded,即「平均每个 fragment 拉了多少字节纹理」。是个派生导出量,由两个真实计数器相除而来。

意义理论上数值高意味着单像素纹理开销重(贴图未压缩、采样次数多、Mipmap 缺失)。但作者标「用处不大」,因为它把空间(字节)和数量(fragment)混在一起,掩盖了到底是「贴图太大」还是「采样太多」,定位时还是得回到 `Texture Memory Read BW` 和 `Textures / Fragment` 两个原始量。

建议当快速筛查"哪段 Surface 单像素带宽异常"时可瞄一眼,真要优化别停在这,下钻到原始计数器。

`Avg Bytes / Vertex`导出量

字面意义，用处不大。Avg Bytes 指总线上的 Vertex Buffer 数据量。

是什么总线上的 Vertex Buffer 数据量 ÷ 顶点数,即「平均每个顶点从内存拉了多少字节」。同样是派生导出量。

意义数值偏高通常指向**顶点格式臃肿**——带了用不到的属性(多套 UV、未使用的切线/副切线、未压缩的法线)。在移动端 TBR 架构下顶点数据会被反复读(binning + rendering 两阶段),顶点越胖代价越被放大。

优化关联与 `Vertex Memory Read Bytes`、`% Vertex Fetch Stall` 交叉看。优化方向:精简顶点属性、用半精度/打包格式、法线用 oct 编码等。

`Read Total Bytes`重要指标

AXI 总线 Read 数据总量。AXI 总线分控制线和数据线，Read 和 Write 双工（读写并行互不影响）。一般旗舰机读或写总线的 peak bandwidth 均为 **256bit × 2 × AXI Frequency（650–960MHz）**，大概 50GB/s。

是什么这段 Surface 期间,GPU 通过 AXI 总线读通道(AR 地址 + R 数据)从系统内存(DDR)读入的总字节数。它是 `SP / Texture / Vertex Memory Read` 三股读流量之和的上层汇总,统计的都是 **L2 Cache 之后**真正打到 DDR 的量。

意义这是判断**带宽是否成瓶颈**的总闸门。AXI 读峰值约 50GB/s(读写各自独立计),把 Read Total Bytes ÷ 这段时间换算成 GB/s,再对比峰值,就能估出读带宽利用率。利用率逼近峰值 → 带宽受限,优化算力无用,得先省带宽。Memory-Bound 信号

优化关联读流量高的三大来源:大贴图(压缩/Mipmap)、胖顶点(精简属性)、大 buffer(SSBO/UAV 局部性)。配合 `% Stalled on System Memory` 确认 GPU 是否真在等读返回。降读带宽 = 降功耗降热,对你做散热优化是直接收益。

`SP Memory Read Bytes`

AXI 总线数据中来自 SP 内部 Load Store Engine 的部分，这是在 L2 Cache 之后的数据量。

是什么Read Total 中由 Shader Processor 的 **Load/Store Engine** 发起的那一股,统计 L2 miss 后真正发往 DDR 的字节。主要对应非纹理的通用内存访问:`StructuredBuffer`/SSBO/UAV 读写、Compute 的 buffer 访问、动态索引的 constant buffer、GPU-driven 的 indirect 数据。

意义数值低 = SP 的读基本被 L2 吸收,局部性好;数值高/尖峰 = 大量 SP 读穿透 L2 压到内存总线,是真正花带宽花功耗的部分。**它不等于"SP 想读多少",而是"SP 的读漏到 DDR 多少"**。

优化关联SP 高 + 纹理项不高 → 瓶颈在通用 buffer 而非贴图,别误优化纹理。配合 `% Stalled on System Memory` 高 → SP 在等 buffer 数据。对策:优化 buffer 数据布局、提升访问局部性、减小结构体步长、合并小读。注意纯纹理采样走 Texture Engine,不计入此项。

`Texture Memory Read BW Bytes`

AXI 总线数据中来自 Texture Engine 的部分，是在 L2 Cache 之后并且 UBWC 压缩之后的数据量。如果 Compute 开启了 UAV 的 UBWC 压缩（probably at 8gen2），这个数据可能不准，有可能包含 UAV 的数据量。

是什么Read Total 中由 **Texture Engine** 发起的那一股,即纹理采样穿透 L2 后从 DDR 读的字节。关键限定:统计的是 **UBWC 压缩之后**的量,所以它反映的是真实占用总线的压缩态带宽,而非解压后的逻辑数据量。

意义判断**纹理带宽是否成瓶颈**的核心指标。AGI 经验值:均值 ≤1GB/s、峰值 ≤3GB/s 较健康;大尖峰往往=慢 Shader 在猛拉贴图。它高而 SP 项低 → 瓶颈在贴图,优化方向是 ASTC 压缩、Mipmap、降分辨率、减采样次数。

坑作者明确提示:**8gen2 上若 Compute 对 UAV 开了 UBWC 压缩,UAV 数据可能被误计进此项**,导致读数虚高。此时别只信这一项,交叉对比 `SP Memory Read` 与 `Textures / Fragment` 还原真实纹理负载。此外该指标常标 (Slow To Trace),采集开销大。

`Vertex Memory Read Bytes`

AXI 总线数据中来自 Vertex Fetch Engine 的部分。

是什么Read Total 中由 **Vertex Fetch Engine** 发起的那一股,即顶点缓冲与索引缓冲穿透 L2 后从 DDR 读的字节。

意义反映几何数据的内存压力。移动端 TBR 下顶点数据在 binning 和 rendering 两阶段都要读,所以顶点越胖、索引复用越差,此项越高。文档资料库里提到:binning 慢时可借此判因——**高读 = 带宽不足,低读 = stall(数据没拉够在等)**。

优化关联与 `Avg Bytes / Vertex`、`Reused Vertices`、`% Vertex Fetch Stall` 一起看。降低手段:精简顶点属性、用索引绘制提升 vertex cache 命中、半精度/打包顶点格式、LOD 减面。

`Write Total Bytes`重要指标

AXI 总线 Write 数据总量。

是什么这段 Surface 通过 AXI 写通道(AW 地址 + W 数据 + B 回执)写回系统内存的总字节数。与 Read Total 物理独立(双工),所以读写带宽各自计算、互不挤占。

意义**写主存很贵,越低越好。**移动端写流量的典型来源:GMEM 把 tile 数据 resolve/store 回 DDR(最终 SceneColor)、RT 输出、UAV 写、未做 Invalidate 的多余附件回写。在 TBR 架构里,**不必要的 store 是最常见的可省写流量**——一帧只有最终结果该写出,中间 G-Buffer 应留在 GMEM 内。

优化关联异常高的 Write Total 常指向 GMEM 不必要的 resolve。对策(与 GMEM 优化同源):对附件正确设 `STORE_OP_DONT_CARE`/Invalidate、避免冗余 RT、慎用频繁回写的 UAV。降写流量同样直接降功耗降热。

## GPU Preemption

GPU 有两种切换进程的方法：**协作式 Context Switch**（允许当前 GPU 执行完 Command Buffer，可理解为 Vulkan 的一次 Queue submit，在 Command Buffer Boundary 切换，不需要保存太多中间状态）与 **Pre-emption**（当高优先级进程急需 GPU，GPU 必须以最快速度保存未完成的中间状态，切回时恢复）。DX10 早期微软曾对 Context Switch 有极严格要求，但厂商都觉得难做；目前 GPU 的切换粒度不是特别激进。

`Avg Preemption Delay`

字面意思，切换时延。

是什么当一个高优先级任务抢占(pre-empt)当前 GPU 工作时,GPU 保存当前中间状态、切换上下文所花的平均时延。

意义抢占切换粒度越细、需保存的中间状态越多,延迟越大。对游戏渲染本身影响通常不大,但若与高优先级合成/VR 异步时间扭曲(ATW)等共享 GPU,过大的切换时延会影响这些时延敏感任务的及时性。

注意作者前文指出当前 GPU 切换粒度"不是特别激进"(多在 Command Buffer 边界做协作式切换),所以一般场景这个值不是优化重点;只有在明确有抢占需求(如 VR/系统合成介入)时才关注。

`Preemptions`

切换次数。

是什么这段采样内 GPU 被抢占切换的次数。

意义次数 × 平均时延 ≈ 抢占的总开销。次数异常多说明有高优先级任务频繁插队(系统合成、其他 app、VR 时间扭曲),会打断当前渲染的连续性、破坏缓存与流水线状态。

优化关联游戏侧能做的有限,主要是排查是否有不必要的高优先级 Queue 提交、或后台任务争抢 GPU。配合 `Avg Preemption Delay` 估算总损耗。

## GPU Primitive Processing

`% Prims Clipped`

字面意思。

是什么被裁剪(clip)的图元占比——即三角形部分超出视锥/裁剪面,需要在裁剪阶段被切割重组的比例。

意义**裁剪很昂贵**(要生成新顶点、重新三角化),理想 `<2%`。偏高说明大量几何骑在视锥边界上。利用 Guardband(护带)可让靠边但仍在屏幕内的三角形免于真正裁剪,从而压低此值。

优化关联常见诱因:超大三角形横跨视锥、近裁剪面切割密集几何、未做视锥剔除就提交。对策:CPU/GPU 端视锥剔除、合理设置近远裁剪面、避免巨型 mesh。

`% Prims Trivially Rejected`

字面意思。

是什么被「平凡剔除」的图元占比——完全落在视锥外、或背面剔除(back-face)等可一步判死、无需进一步处理的三角形比例。

意义这是**廉价且有益**的剔除(8gen2 每 cycle 能 kill 两个 invisible triangle)。但换个角度:如果此值很高,说明**你提交了大量根本看不见的几何**——GPU 虽然剔得快,CPU 提交和 binning 仍付出了成本。

优化关联高拒绝率 = 提示应在更早阶段(CPU 视锥/遮挡剔除)就别提交这些图元,省下 draw call 与几何处理开销。与 `Pre-clipped Polygon` 一起看几何提交效率。

`Average Polygon Area`

字面意思。

是什么平均每个三角形覆盖的屏幕像素面积。

意义移动 TBR 架构下这是个**关键健康指标**。理想 `≥4 像素`,且**不要远大于一个 bin 的尺寸**。太小(<4–10 像素)= Micro-triangles,光栅化效率崩塌(quad 利用率低、大量 helper lane 浪费、分块开销摊不开);太大 = 单个三角形横跨多 tile,光栅化要花多个 cycle 且增加跨 tile 重复。

优化关联过小→引入/收紧 LOD、合并细碎网格、避免远处高模;过大→适度细分或拆分巨型面。与 `Average Vertice / Polygon`、`Fragment Shaded` 配合判断几何密度是否合理。

`Average Vertice / Polygon`导出量

字面意义，用处不大。

是什么平均每个三角形分摊的顶点数。理论上独立三角形是 3,用了索引且复用充分会趋近 1。

意义越接近 1 说明**索引绘制和 vertex cache 复用越好**,VS 重复计算越少;接近 3 说明几乎没复用(可能没用索引、或顶点顺序对 cache 不友好)。作者标"用处不大"因为它只是 `Reused Vertices` 的另一种侧写。

优化关联真要优化看 `Reused Vertices` 更直接。提升复用:用 indexed draw、对 mesh 做顶点缓存优化重排(如 Forsyth/Tom Forsyth 算法、tipsify)。

`Pre-clipped Polygon`重要指标

指送往 Clip 和 Rasterization Engine 的三角形数。Adreno 的三角形 Throughput 相对 Intel、NVIDIA 较小。以 8gen2 为例，一个 cycle 可执行一个 visible triangle，或 kill 两个 invisible triangle，那么 680MHz 的 8gen2 每秒执行 **680M visible triangles**，但大三角形在 Rasterization 会花多个 cycle。对 back-facing triangle，每秒可 kill **1360M 个**。

是什么裁剪前、送入 Clip/光栅化引擎的三角形总数,代表 GPU 实际承担的几何处理量。Geometry-Bound 信号

意义Adreno 三角形吞吐相对桌面 GPU 较弱(8gen2 约 680M visible tri/s),所以这个量一旦过大就直接成几何瓶颈。可用它反推:**这段 Surface 的三角形数 ÷ 680M ≈ 仅光栅化就要花的秒数**,与帧预算对比判断是否几何受限。注意大三角形光栅化吃多个 cycle,实际吞吐会低于理论值。

优化关联过高 → LOD、遮挡剔除、合批减面、避免高模近距离堆叠。配合 `% Shader Busy`:若 Shader Busy 低但 Pre-clipped 高,可能卡在几何前端而非着色。

`Reused Vertices`重要指标

如果 index 指向的 vertex id 已执行过 vertex shader，且执行结果（post-vs attributes）仍在 vertex cache 中，称这次 vertex shading 被 reuse。该指标表示**避免了多少次重复的 VS Shading**。如果 Vertex Cache 未命中，即使是以前执行过的 vertex id 也需重新执行。

是什么因 vertex cache 命中而**省下的重复 VS 执行次数**。同一个顶点被多个三角形共享时,若它的 post-VS 结果还在 cache 里,就不必重算。

意义越高越好,直接代表 VS 计算被节省的程度,是**索引质量与顶点排布是否友好**的体现。它高 → `Vertices Shaded` 相应降低 → VS 算力和顶点带宽双双省下。这在移动端尤其值钱,因为 VS 在 binning+rendering 可能跑两遍。

优化关联提升手段:务必用 indexed draw、对 mesh 做缓存友好重排、控制顶点局部性(相邻三角形共享顶点尽量靠近)。与 `Vertices Shaded`、`Average Vertice / Polygon` 交叉验证复用效果。

## GPU Shader Processing

Micro Shader Core 内部结构中，比较重要的是 **Local buffer**，此模块承担 const pre-loading、VS→PS Varying、compute local memory、sGPR 等职责，可对应到 AMD 的 LDS。

⚠️ SDP 有很多名为 **Busy** 的 Metrics，不是十分可靠。一个模块 Busy 不代表它在 Working，比如 Texture Pipe Busy 可能指它在等 memory return，并没有在做 filtering、也没接受新 request。只要它还能接受 shader 的 request，就不应称其为 busy——这就是这些 Counter 不太好用的原因。

`% Anisotropic Filtered`用处不大

字面意思。

是什么纹理采样中使用各向异性过滤(Aniso)的比例。

意义Aniso 是三类过滤里**最贵**的(多次采样,倍率越高越贵)。比例高且恰好处在纹理瓶颈时,降低 Aniso 倍率(16x→4x/2x)能省 TP 开销。但作者标"用处不大"——单独看比例无意义,必须结合 TP 是否真成瓶颈。

优化关联仅当 `% Texture Fetch Stall` 高 + 纹理带宽吃紧时才值得据此调 Aniso 等级,通常对远处/掠射角表面才需要高 Aniso。

`% Linear Filtered`用处不大

字面意思。

是什么使用线性(双线性/三线性)过滤的采样比例。

意义线性过滤是移动端最常用、性价比合理的过滤方式(8gen2 的 TP 每周期就能为两个 pixel quad 做半精度双线性)。比例高基本是正常现象,不构成问题信号。

注意三类 Filtered 指标(Aniso/Linear/Nearest)主要用于**了解采样构成**,而非定位瓶颈。真要看纹理压力,用 `Textures / Fragment` + `Texture Memory Read BW`。

`% Nearest Filtered`用处不大

字面意思。

是什么使用最近邻(point/nearest)过滤的采样比例。

意义最近邻最便宜,常用于查找表(LUT)、数据纹理、像素风、整数纹理等。比例高不是问题,反而可能是有意为之的省开销做法。

注意同上,属构成类信息,定位瓶颈价值低。

`% Non-Base Level Textures`用处不大

字面意思。

是什么采样命中**非最高层 Mipmap(即非 base level)**的比例,反映 Mipmap 被实际使用的程度。

意义这个其实比另几个 filtered 指标更有诊断价值:若它**异常低**(大量采样都打在 base level),往往意味着 Mipmap 没生效或没生成 → 远处表面用全分辨率贴图采样 → L1/L2 命中率暴跌、纹理带宽飙升、还会闪烁。

优化关联低比例 + 高 `% Texture L1/L2 Miss` + 高纹理带宽 = 经典"缺 Mipmap"病征。对策:确保贴图生成并启用 Mipmap、检查 LOD bias 设置。

`% RTU Busy`

8gen2 开始，SP 内部每个 microSP 带一个 RTU，它是 **single hop ray node intersection** 模块。BVH node 可以是 AABB 或 Primitive（BLAS 的 leaf node）。single hop 指每次 RTU 只做一次 ray node intersection，BVH Tree Traverse 的中间状态（stack info）仍在 Shader Program 中：RTU 做完一层求交，回到 SP 处理 stack info，再寻找新 node 求交。NVIDIA 是 multiple hop RTU（可自治完成整棵树遍历，只返还最终 intersection 结果），AMD 目前也是 single hop。

是什么光线求交单元(Ray Tracing Unit)的繁忙周期占比,仅在使用硬件光追时有意义。

意义由于 Adreno 是 **single hop** 架构(RTU 每次只做一层求交,遍历的 stack 状态留在 shader 里反复往返),光追时 RTU 与 SP 之间会频繁交互,RTU Busy 高同时也会占用 SP。这与 NVIDIA 的 multiple hop(RTU 自治遍历整棵 BVH)有本质差异——**移动端硬件光追的 SP 开销相对更重**。

注意遵循前文"Busy ≠ Working"的告诫:RTU Busy 高可能含等待 memory return 的时间。配合 `Average BVH Fetch Latency Cycles`、`% BVH Fetch Stall` 判断是算力还是访存受限。光追在移动端慎用,优先考虑 BVH 质量与 ray 数控制。

`% Shader ALU Capacity Utilized`重要指标

当存在 Divergence 时，此值会小于 "% Time ALUs Working"。例如 % Time ALUs Working 为 50%、% Shader ALU Capacity Utilized 为 25%，意味着**一半的 fiber 不工作**（masked due to divergence 或 triangle coverage）。

是什么ALU 算力的**真实有效利用率**——不仅看 ALU 在不在跑,还看每次跑有多少 lane(fiber)真正在算。它把 divergence 和 coverage 造成的 lane 浪费也算进去了。

意义这是区分**算力受限 vs 延迟受限**的核心判据:
• 它高(接近 % Time ALUs Working) → ALU 真在满负荷算 → Compute-Bound,优化方向是减指令/降复杂度/半精度。
• 它显著低于 % Time ALUs Working → 大量 lane 被 mask 掉(分支发散、小三角形覆盖不满 quad) → 算力被浪费但不是缺算力。

优化关联低利用率的两大元凶:① Shader Divergence(同一 wave 内 fiber 走不同分支)→ 用 `step/mix` 替代 if、让分支按 wave 粒度一致;② Micro-triangles 导致 quad 覆盖不满 → 见 `Average Polygon Area`。务必与 `% Time ALUs Working` 成对解读。

`% Shader Busy`重要指标

表示 Shader Core 是否工作。**若小于 75% 则非常需要优化**。四类原因：① vertex data 多但 VS 不复杂——需借助 Concurrent Binning 或减少 vertex input；② GPU Command Processor 太慢（不常见，可能是 indirect draw call）；③ rhi/render thread 太慢，GPU（含 Command Processor）根本无事可做；④ 小 draw call 个数太多，每个 draw 都有固定启动/收尾成本，Binning Mode 下成本更大（skipping invisible draw calls 也有成本）。

是什么Shader Core 处于工作状态的周期占比,衡量**GPU 着色资源被喂饱的程度**。

意义作者给了硬阈值 **<75% 就要优化**。低 Shader Busy 不代表 GPU 很闲很好,恰恰相反——它意味着**着色核心在饿肚子**,瓶颈在别处(前端几何、Command Processor、CPU 提交、draw call 启动开销),GPU 算力没被有效利用。这是一个"指向上游瓶颈"的指标。

优化关联按作者四类病因对症:① 几何多但 VS 简单 → 开 Concurrent Binning / 减顶点输入;②③ CPU/render thread 喂不动 → 这是 **CPU-bound**,优化提交、合批、减少状态切换;④ 小 draw call 海量 → 合批、instancing。需结合 `Pre-clipped Polygon`(几何前端)和 CPU 侧 trace 共同定位到底是哪一类。

`% Shader Stalled`

指没有任何 execution unit（主要指 ALU、texture、load/store）在工作的 cycle 占总 cycle 的比例。Memory fetch stalled 不一定意味着 Shader Stalled——如果 shader 还能找个 wave 执行 ALU 就不算 stall。该指标意味着 **IPC（instruction per cycle）下降**。

是什么所有执行单元(ALU/texture/load-store)**同时全部空转**的周期占比。关键:只要还能从任意 wave 里捞到一条指令执行,就不算 stall。

意义它升高 = **IPC 下降** = latency hiding 失败的直接证据。含义是 SP 想找活干却找不到——所有在飞的 wave 都卡在等长延迟操作(纹理/内存),且**没有足够多的备用 wave**来填补空档。这指向 occupancy 不足。Latency-Bound

优化关联核心解法是**提高 occupancy 来隐藏延迟**:降寄存器压力(GPR)以塞下更多并行 wave、避免寄存器溢出、控制 shader 复杂度。与 `% Wave Context Occupancy` 强相关——后者低则前者往往高。注意区别 `% Stalled on System Memory`(那是总线层的反压,粒度不同)。

`% Texture Pipes Busy`用处不大

如上所述，不是十分有用。

是什么Texture Pipe(TP)的繁忙周期占比。

意义这是"Busy ≠ Working"陷阱的典型:TP "busy" 可能只是在**等 memory return**,并没在做 filtering、也没拒绝新请求。所以高数值既可能是真忙、也可能是真闲在等数据,无法区分,故作者判定不可靠。

替代方案判断纹理是否成瓶颈,改用 `Textures / Fragment`(工作量)+ `Texture Memory Read BW`(带宽)+ `% Texture Fetch Stall` 综合,别依赖这一项。

`% Time ALUs Working`

SP busy 的 cycle 里，多少比例的 cycle ALU Engine 在工作。一个 Wave 即使只有一个 fiber active，此指标也加一——这点与 Fragment ALU Instructions Full/Half 不同。

是什么SP 忙碌期间 ALU 引擎在跑的周期占比。关键区别:它**按 wave 粒度**计——只要 wave 里有一个 fiber 活跃就算 ALU 在工作,**不管实际有多少 lane 真在算**。

意义它必须和 `% Shader ALU Capacity Utilized` **成对解读**:本指标是"ALU 引擎开机率",后者是"开机时的真实满载率"。两者接近 → lane 利用充分;本指标高但后者低 → ALU 一直在转,却大量 lane 被 divergence/coverage mask 掉,算力空烧。

优化关联本指标高是判断算力受限的**前提条件之一**,但只有它高 + Capacity Utilized 也高,才是真·Compute-Bound。单看它会高估算力压力。

`% Time Compute`

高通结构中 CS 与 PS 基本无法并行（除 Async Compute，但高通称驱动仍未开启支持），所以该值基本上不是 100% 就是 0%。

是什么这段 Surface 中 SP 在跑 Compute Shader 的时间占比。

意义由于高通当前驱动**未开启 Async Compute**,CS 与图形管线(VS/PS)基本互斥,所以该值几乎只有 0%(纯图形 Surface)或 100%(纯 Compute Surface)两种状态,中间值罕见。它主要用于**辨认这段 Surface 的性质**。

注意这条对 GPU-Driven Render 是个重要约束:你期望的"Compute 与图形重叠隐藏开销"在高通上现阶段拿不到,Compute Pass 会串行占用 GPU。规划 Compute visibility/裁剪时要把这点算进帧预算。

`% Time EFUs Working`

SP busy 的 cycle 里，多少比例 EFU Engine 在工作。EFU 计算 sin、pow、rcp 等数学函数，很少成为瓶颈。

是什么EFU(Elementary Function Unit,超越函数单元)的工作周期占比。EFU 专算 `sin/cos/pow/exp/log/rcp/rsqrt` 等。

意义EFU 吞吐通常远低于普通 ALU(往往是 1/4 速率之类),但绝大多数 shader 用得少,**很少成为瓶颈**。只有当 shader 里堆砌大量超越函数(复杂程序化噪声、解析光照、大量 pow 做 gamma)时此值才显著。

优化关联若异常高:用 LUT 纹理预计算替代实时 `pow/sin`、用乘法近似替代 `pow(x,2)`、合并 rsqrt 等。与 `Fragment EFU Instructions` 一起看绝对量。

`% Time Shading Fragments`

字面意义。

是什么SP 时间中花在 Pixel/Fragment Shader 上的占比。

意义反映这段 Surface 是否**以像素着色为主**。对全屏后处理、复杂材质、高 overdraw 场景,此值会很高,说明 PS 是主要消耗。与 `% Time Shading Vertices` 互补:两者比例揭示 VS/PS 负载分布。

优化关联高 PS 占比 + 性能问题 → 查 PS 复杂度、overdraw(`Fragment Shaded` 远大于屏幕像素数即过度绘制)、降分辨率/后处理。

`% Time Shading Vertices`

因为 Adreno 的 SP 可同时运行 VS 和 PS，所以即使该值很大有时也不说明有问题。比如 PS 运行 900 cycle，其中 100 cycle 同时运行了 VS，那么 10% 在 shading vertices——但这 100 cycle 藏在 900 cycle 里，VS 也不占什么资源，几乎是 free 的。

是什么SP 时间中花在 Vertex Shader 上的占比。

意义关键认知:Adreno 的 SP **能让 VS 和 PS 在同一核心上重叠跑**,VS 常常填进 PS 等待内存的空档里执行,**几乎"免费"**。所以此值大不必然是问题——作者举例 10% 的 VS 时间藏在 PS 的 900 cycle 里,等于白捡。

注意真正要警惕的是 VS 自身过重(复杂顶点动画、VS 里采样纹理、几何过多)导致它**无法被 PS 掩盖**而暴露成瓶颈。这时配合 `Vertices Shaded`、`Vertex Instructions`、`% Vertex Fetch Stall` 判断,而不是只看本指标。

`% Wave Context Occupancy`重要指标

若为 100%，意味着 micro shader core 在运行 16 条 wave。一般至少 4 条 128-fiber wave（25% 占用）或 6–8 条 64-fiber wave 可满足 **Memory Latency Hiding** 要求。但 SDP 没告诉我们 Wave 有几个 Fiber，可通过 Offline Compiler 间接猜测（参见下方 Overall Register Footprint Per Shader Instance）。

是什么micro shader core 上同时驻留的 wave 数占满载(16 条)的比例,即 **Occupancy(占用率)**。它直接决定 GPU 隐藏内存延迟的能力。

意义这是移动端最核心的延迟隐藏指标。SP 遇到长延迟操作(纹理采样)时靠切到别的 wave 来填空档,**驻留 wave 越多越能掩盖延迟**。作者经验线:至少 4 条 128-fiber wave(25%)或 6–8 条 64-fiber wave 才够隐藏延迟。低于此 → `% Shader Stalled` 上升 → IPC 掉。

优化关联Occupancy 的头号杀手是**寄存器压力**:每个 wave 占的 GPR 越多,能并行的 wave 越少。对策:降 GPR(改 GLSL 省指令、用半精度 mediump、避免超级 shader、不展开循环)、防止 Register Spilling。可用 SDP Offline Compiler 看 `Overall Register Footprint Per Shader Instance` 反推 wave 宽度与上限。

`ALU / Fragment` · `ALU / Vertex`导出量

字面意义，用处不大。

是什么平均每个 fragment / vertex 执行的 ALU 指令数(总 ALU 指令 ÷ 着色数量)。

意义反映单个像素/顶点的**算术复杂度**。理论上越低越省算力,可用于横向比较两个 shader 谁更重。作者标"用处不大"因为它是派生量,且没区分 full/half 精度。

替代方案真要看算力,用 `Fragment ALU Instructions (Full/Half)` 原始量 + `% Shader ALU Capacity Utilized`。本指标只适合粗略对比。

`Average BVH Fetch Latency Cycles`

从 RTU 发出 BVH node fetch 请求，到 memory path 返还的 cycle 数。一般 **170 cycle 以内**不是大问题；若 RTU Cache hit rate 高，可能小至 50 以内。

是什么光追时 RTU 取 BVH 节点的平均访存延迟(周期数),仅光追场景有意义。

意义判断光追是否**卡在 BVH 访存**。作者给阈值:`≤170 cycle` 可接受,RTU cache 命中好时可降到 50 以内。偏高说明 BVH 数据局部性差、cache 命中率低,光追被访存拖累(回想 single hop 架构会反复往返取节点,延迟敏感)。

优化关联与 `% BVH Fetch Stall`、`% RTU Busy` 联看。改善:优化 BVH 质量(减少深度/重叠)、控制 ray 数量与发散、限制光追用途(如只做反射/AO 而非全局)。

`EFU / Fragment` · `EFU / Vertex`导出量

字面意义，用处不大。

是什么平均每个 fragment / vertex 执行的 EFU(超越函数)指令数。

意义反映单像素/顶点用了多少 `sin/pow/rcp` 类昂贵函数。数值高提示 shader 在超越函数上偏重。但 EFU 极少成瓶颈,故作者标用处不大。

替代方案需要时看 `Fragment EFU Instructions` 绝对量 + `% Time EFUs Working`。

`Fragment ALU Instructions (Full)`

32bit floating point 总指令数。

是什么PS 中执行的 **32 位(highp/full)浮点 ALU 指令**总数。

意义full 精度指令**吞吐是 half 的一半、占双倍寄存器**。这个数大、且 Half 数小 → shader 过度使用 highp,既慢又挤占 GPR 拉低 occupancy。移动端原则:能 mediump 就别 highp。

优化关联与 `Fragment ALU Instructions (Half)` 比例对照,目标是尽量把颜色/UV/法线等计算降到 half。highp 只留给世界坐标、深度重建等精度敏感处。降 full 指令同时降 `% Wave Context Occupancy` 的压力。

`Fragment ALU Instructions (Half)`

16bit floating point 总指令数。

是什么PS 中执行的 **16 位(mediump/half)浮点 ALU 指令**总数。

意义half 精度是移动端的**性价比首选**:吞吐翻倍、省寄存器、省功耗、提并行度(2 个 half ≈ 1 个 full 开销)。这个数占比高是健康信号,说明 shader 做了精度优化。

优化关联Half : Full 的比值是衡量 shader 精度优化程度的直观指标。比值越高越好。但注意 half 的精度/范围限制,避免在累加、坐标变换等场景误用导致瑕疵。

`Fragment EFU Instructions`

EFU 总指令数。

是什么PS 中执行的超越函数(EFU)指令总数。

意义EFU 单位吞吐低,这个绝对量大时(即便 % Time EFUs Working 看着不极端)也可能拖慢。是定位"超越函数过载"的原始依据。

优化关联偏高 → LUT 预计算替代实时 pow/sin、用代数恒等式化简、用乘法替代整数次幂。配合 `% Time EFUs Working` 与 `EFU / Fragment`。

`Fragment Instructions`

PS 总指令数。GPU 以 quad 为单位测量，即使一个 quad 三个点在三角形外（masked in PS），此 Counter 仍加 4。

是什么PS 执行的总指令数(含 ALU/EFU/采样等)。计量以 **quad(2×2 像素)为单位**:哪怕一个 quad 里 3 个像素在三角形外被 mask,计数仍按 4 个加。

意义反映 PS 总负载。那条 quad 计量规则很关键——它揭示了 **Micro-triangles 的隐藏代价**:小三角形让大量 quad 只有 1 个有效像素却付 4 像素的指令账,这部分浪费会体现在本指标虚高、而 `% Shader ALU Capacity Utilized` 偏低。

优化关联本指标高 = PS 重或 overdraw 重或 micro-triangle 多。结合 `Average Polygon Area`(是否过小)、`Fragment Shaded`(是否 overdraw)区分病因。

`Fragment Shaded`重要指标

表示执行了多少个 fiber 的 pixel shader。GPU 以 quad 为单位测量，即使一个 quad 三个点在三角形外仍加 4。**Compute Shader 的 fiber 数也计入此指标**。

是什么实际执行了 PS 的 fiber(像素 lane)总数,含被 quad 规则计入的 helper lane,且 **Compute Shader 的 fiber 也算进来**。

意义这是衡量**像素着色总工作量**的核心量。把它与屏幕实际像素数对比:**Fragment Shaded 远大于屏幕分辨率 = Overdraw 严重**(同一像素被着色多次)。这是定位过度绘制最直接的数据。

优化关联降 overdraw:不透明物体前到后排序利用 Early-Z、减半透明层叠、用 LRZ/Fast-Z、合理裁剪。注意它含 CS fiber,分析纯图形 overdraw 时要排除 Compute Surface。配合 `Average Polygon Area` 排查 micro-triangle 造成的 helper lane 虚高。

`RTU Ray Box Intersections Per Instruction`

表示每次 HLSL 级 TraceRay() 会产生多少次中间 node（即 Box）的访问。模型复杂则该值大；Shadow Ray（any hit ray）该值不会很大。

是什么每条 `TraceRay()` 平均触发的 **AABB 包围盒求交**次数,即遍历 BVH 中间节点的次数。

意义反映 BVH 遍历深度/复杂度。模型越复杂、BVH 越深,box 求交越多。Shadow Ray(any-hit,命中即停)通常此值不大;closest-hit ray 会更大。它高意味着**遍历开销重**,在 single hop 架构下还伴随大量 SP↔RTU 往返。

优化关联降低:优化 BVH 构建质量(减少节点重叠与深度)、简化光追用的代理几何、控制场景光追物体数。与 `Ray Triangle Intersections` 一起评估单条光线的总成本。

`RTU Ray Triangle Intersections Per Instruction`

与 Ray Box Intersections 类似，表示每次 TraceRay() 产生多少次 leaf node（即 triangle）的访问。

是什么每条 `TraceRay()` 平均触发的 **三角形(BVH 叶节点)求交**次数。

意义反映光线最终与几何求交的密集度。此值大说明叶节点里三角形太多/太密,或 BVH 叶聚类不佳,导致每条光线要逐个测试大量三角形。

优化关联与 box 求交配合看:box 多 = 树太深;triangle 多 = 叶太肥。优化 BLAS 构建、减少光追几何面数、避免在光追里用高模。

`Textures / Fragment`

字面意义。该值乘以 Fragment Shaded 可得 TP 的工作量；除以 `Clocks × 8 × #TP` 可能得到 TP 的 bandwidth utilization（有待验证）。

是什么平均每个 fragment 的纹理采样次数。是评估**纹理工作量**最有用的指标之一。

意义作者给了换算法:`本值 × Fragment Shaded` = TP 总工作量;再 `÷ (Clocks × 8 × #TP)` 可估 TP 带宽利用率(待验证)。它是判断纹理瓶颈"压力大小"的钥匙——前文反复强调 **Stall ≠ 压力,纹理数据量才是压力来源**,而这个量正是数据量的代表。

优化关联偏高 → 减采样次数(合并贴图通道、用更少 sampler)、贴图图集、降低重复采样。它与 `% Texture Fetch Stall` 必须配合:utilization(由本值推) 高时 stall 才真正是问题。这是作者推荐替代不可靠 Busy 指标的正路。

`Textures / Vertex`导出量

字面意义。该值不为 0 意味着 VS 里有 Texture Fetch，对 Binning Mode 不友好，但这种情况驱动一般会察觉并改用 direct mode。

是什么平均每个顶点的纹理采样次数。非 0 即表示 **VS 里在采样纹理**(如顶点位移贴图/地形高度图)。

意义VS 采样纹理对 **Binning Mode 不友好**:binning pass 需要算顶点位置,若位置依赖纹理采样,binning 阶段也得采样,开销翻倍且引入访存延迟到几何前端。驱动通常会察觉并改走 direct mode(放弃分块省带宽的好处)。

优化关联非 0 时警惕:能否把顶点位移移到 CPU/预计算、或避免在性能敏感路径用 vertex texture fetch。回想资料库提到"位置计算含纹理采样会让 VS 执行翻倍"。

`Vertex Instructions`

VS 总指令数。

是什么VS 执行的总指令数。

意义反映顶点着色的复杂度。回想 VS 在移动端可能**跑两遍**(binning 阶段只执行位置相关指令 + rendering 阶段),所以位置计算里的复杂指令代价被放大。VS 通常能被 PS 掩盖(见 % Time Shading Vertices),但指令数过大、或位置计算过重时会暴露成瓶颈。

优化关联偏高 → 简化顶点动画、把复杂位置计算移出 VS、减少 varying 输出(也省 VS output local buffer,8gen2 仅 8KB,满了会 VS stall)。配合 `Vertices Shaded`、`% Vertex Fetch Stall`。

`Vertices Shaded`重要指标

表示执行了多少个 fiber 的 vertex shader。

是什么实际执行了 VS 的 fiber(顶点 lane)总数,即真正跑了多少次顶点着色。

意义衡量**顶点着色总工作量**。它与提交的顶点数的差距体现复用效果:`Vertices Shaded` 越接近"提交顶点数",说明 `Reused Vertices` 越少、cache 复用越差。在移动端因 VS 可能跑两遍,这个量对几何重的场景尤其要盯。

优化关联降低:用 indexed draw 提升复用、LOD 减面、合并细碎网格。与 `Reused Vertices`(省了多少)、`Pre-clipped Polygon`(几何总量)三者拼出完整的几何负载图景。

## GPU Stalls

Stall Metrics 仅供参考。一个模块 Stall 它的上一个模块，不一定表示该模块工作多，也许是它的下一个模块太慢、Stall 被反向传导回来。如果 Stall 太大，一般意味着这个模块、或其后整条流水线链的某处可能有问题。

`% BVH Fetch Stall`

从 Cache 模块反向传导回 BVH Fetch Engine（位于 SP 的 RTU 内）的 Stall。

是什么RTU 内的 BVH Fetch Engine 因下游 Cache/内存太慢而被反压(back-pressure)的周期占比,仅光追场景有意义。

意义它高 = 光追**卡在取 BVH 数据**而非求交计算。结合 single hop 架构(频繁往返取节点),BVH 访存延迟会被放大成 stall。是判断"光追是访存受限还是算力受限"的关键。

优化关联与 `Average BVH Fetch Latency Cycles`(延迟绝对值)、`% RTU Busy` 联看。改善 BVH 局部性与 cache 命中、减少 ray 发散。遵循 Stall 类指标通则:它反映的是**下游链路**有问题,未必是 RTU 自身。

`% Instruction Cache Miss`

不要让 Shader（编译后机器）指令超过 **2000 条**（VS+PS），用 SDP 的 Offline Compiler 可看到指令数。

是什么指令缓存未命中率——shader 机器指令太多、装不进 I-Cache,执行时要反复从内存取指令的比例。

意义作者给硬线:**编译后(VS+PS)指令 ≤2000 条**,超了 I-Cache 装不下就会 thrash,本指标飙升,IPC 暴跌。资料库还提到:低 occupancy 有时正是长 shader 在抖动 I-Cache 所致。"超级着色器"(uber shader)是典型病根。

优化关联用 SDP Offline Compiler 看实际指令数。对策:拆分超级 shader、按变体裁剪(Shader Variant Collection)、不展开循环(也省 GPR)、把中间结果存 GMEM 而非堆指令。与 `% Wave Context Occupancy` 联动。

`% Stalled on System Memory`

AXI 总线对 L2 Cache 的反向压力。

是什么AXI 总线/系统内存(DDR)对 L2 Cache 形成的反压周期占比——即 L2 miss 后等 DDR 返回数据时的停顿。Memory-Bound 信号

意义这是判断**带宽/访存受限**的最直接 stall 指标。通常 `<2%`,短尖峰可到 30%。持续偏高 = 大量数据穿透 L2 打到 DDR 且 GPU 在干等 → 真正的内存墙。它与读写带宽指标(`Read/Write Total`)是因果关系:带宽被打满 → 这里就 stall。

优化关联顺藤摸瓜:本指标高 → 看 `Read Total / SP / Texture / Vertex Memory Read` 定位是谁在猛读 → 对症压缩纹理/精简顶点/优化 buffer 局部性。注意它是总线层粒度,与 `% Shader Stalled`(执行单元全空转)层级不同,可同时参考。

`% Texture Fetch Stall`

Texture Engine 不能接受 Shader Core request 时的反向压力 cycle。若很大，意味着 TP 或其 Downstream Block 太忙。SDP 的问题是没给出 Texture L1 的真正压力百分比——**Stall 并不等于压力**。比如 TP filtering utilization 10% 时，即使 stall 25%、L1 miss 70% 也不是问题（TP 根本没什么活干）；但 utilization 60% 时，即使 stall 10%、L1 miss 20% 也是问题。Texture 数据量才是压力大来源，可参考 Textures / Fragment 再综合本指标。

是什么Texture Engine 无法接受 Shader 的新采样请求(因自身或下游太忙)而反压的周期占比。通常 `<2%`,短尖峰 ≤20%,**持续 ~16%+ 通常过高**。

意义作者反复强调的核心判读法:**Stall ≠ 压力**。必须配合 TP 的实际利用率(由 `Textures / Fragment` 推算)才有意义——TP 利用率仅 10% 时即便 stall 25%、L1 miss 70% 都不是问题(TP 本来没活);但利用率 60% 时哪怕 stall 10% 也是真瓶颈。**单看本指标会误判**。

优化关联判定真瓶颈需三件套:`Textures / Fragment`(工作量/利用率)+ 本指标(stall)+ `Texture Memory Read BW`(带宽)。确认为真后:压缩纹理(ASTC)、Mipmap、减采样、改善 UV 局部性降 L1 miss。

`% Texture L1 Miss`

Adreno L1 Size 不大（**2KB**），容易被不好的 Shader 写法 thrash；有时 Compiler 行为也会影响 Access Pattern，导致 L1 Miss Rate 过高。

是什么纹理 L1 缓存未命中率。Adreno 的纹理 L1 **仅 2KB**,非常小,正常会在 0%–<50% 间波动并偶有尖峰。

意义因为 L1 极小,它**极易被坏的访问模式 thrash**:UV 跳跃大、随机采样、同时采样多张大贴图、编译器生成的不友好访问序列,都会推高 miss。但再次强调——**L1 miss 高不一定是问题**,要看 TP 利用率(利用率低时 miss 高无所谓)。

优化关联改善 UV 空间局部性(相邻像素采相近 texel)、减少单 shader 内同时活跃的纹理数、贴图图集注意排布。L2 miss 才是真正昂贵的(要打 DDR),L1 miss 是前哨。与 `L1 Texture Cache Miss Per Pixel` 互为绝对量/比率。

`% Texture L2 Miss`

L2 并非只为 Texture 服务，但此指标只反映 Texture L1 送来的 request 在 L2 中的 Miss Rate。

是什么来自纹理 L1 的请求在 L2 缓存的未命中率(L2 是共享的,但此指标只统计纹理那部分)。正常 0%–<40% 波动。

意义**这是真正昂贵的那一级**:L2 miss 意味着数据连共享缓存都没有,必须去 DDR 取 → 高延迟 + 吃带宽 + 直接拉高 `% Stalled on System Memory`。资料库原话:"L2 Miss 后必须读取系统 DDR,性能断崖式下降"。

优化关联持续高 L2 miss = 工作集超出 L2 容纳 / 局部性差。对策:纹理压缩减小工作集体积、Mipmap(远处用小图自然提升命中)、控制同时驻留的纹理总量、优化访问局部性。它与纹理带宽、System Memory Stall 是同一条因果链的不同观测点。

`% Vertex Fetch Stall`

不一定是 Vertex Fetch Engine 有问题，也许是整个 Memory System 处于压力中——压力可能来自同时运行的 PS，或来自 VS 里的 texture load。

是什么Vertex Fetch Engine 取顶点/索引数据时的停顿周期占比。通常 0%,尖峰不超 70%。

意义典型的"**stall 未必是本模块的锅**"案例:它高**不一定**是顶点拉取本身慢,很可能是整个内存系统在承压——同时跑的 PS 在猛拉纹理、或 VS 里有 texture fetch 抢占了带宽,反压传导到了顶点拉取。

优化关联排查顺序:先看是不是全局内存压力(`% Stalled on System Memory` 是否也高)→ 是则解决整体带宽;若孤立高则查顶点格式(`Avg Bytes / Vertex` 过胖)、`Textures / Vertex`(VS 是否采样纹理)。别一上来就改顶点布局。

`L1 Texture Cache Miss Per Pixel`导出量

字面意义，用处不大。

是什么平均每像素的纹理 L1 未命中次数(绝对量,非比率)。

意义它把 L1 miss 摊到每像素,直观反映单像素的纹理 cache 友好度。资料库提到一个用法:**若它很高,检查 UV 是否过于混乱/跨越太大**。但作为派生量,作者标"用处不大"——结论与 `% Texture L1 Miss` 高度重合。

优化关联偏高 → UV 局部性差,优化 UV 展开、避免大跨度采样。真做诊断优先用 `% Texture L1 Miss` + TP 利用率组合。

## Vulkan / OpenGL ES

`Rendering stages`

从 8gen1 开始，"GPU Binning Pipe" 显示的是 **Concurrent Binning** 的 Surfaces。Binning 可与 Main Pipe 并行，但驱动有权选择把 Binning 放在异步 "GPU Binning Pipe" 还是主 Pipe（GPU Render Pipe）。放在主 Pipe 可得到更多计算资源，所以无干扰时同一个 Binning 在主 Pipe 会比 Concurrent Binning Pipe 快一些。未来该问题或被解决，届时驱动会不加选择地把 Binning 放入 Concurrent Binning Pipe。

这种异步 Binning Pass 对 GPU Driven Render 是个挑战，因为 VS 可能依赖之前的 Compute 结果。建议在 Compute Visibility 与使用 Visibility 的 VS 之间安插其他无数据依赖的 Render Pass，这样 Concurrent Binning VS 才可能与这些无关 Pass 并行。上图的 Pipe 可展开，能看到 Resolve/Blt 等。

是什么时间线上展示 GPU 各渲染阶段的分布(主 Render Pipe、异步 Binning Pipe、Resolve/Blt 等),用于看清各 Surface 落在哪条 pipe、是否并行。

意义这是**诊断 Concurrent Binning 是否真生效**的视图。理想:binning 与主管线渲染重叠以隐藏几何前端开销。但驱动会权衡——放主 pipe 算力多更快、放异步 pipe 才能并行。能否并行取决于**有没有数据依赖**。

优化关联对你做 GPU-Driven Render 尤其关键:Compute 算 visibility → VS 用 visibility 之间若紧挨着,异步 binning 无法并行。**对策:在两者间插入无依赖的 Render Pass**,给 concurrent binning 创造并行窗口。展开 pipe 检查 Resolve 是否被迫串行(见下条 GLES 注意)。

`OpenGL ES 注意事项`

担忧 GLES 版本的 SDP 在 Surface 边界加入了太多 wait for idle，导致不能反映非 Profile 时的并行度。比如 GLES 也支持 Concurrent Binning，但很少在 SDP 看到并行运行的 Binning Surface。另外 Resolve 本可与 Bin Render 并行，但 SDP 为独立统计 Resolve 的 Perf Counter，强行让 Resolve 独立运行了。

是什么关于 GLES 下 SDP 测量**失真**的告诫,不是一个数值指标。

意义核心警告:SDP 为了能独立采样每个阶段的计数器,**在 Surface 边界强行插入了 wait-for-idle,人为破坏了并行**。后果是——GLES 实际能 Concurrent Binning、Resolve 实际能与 Bin Render 并行,但你在 SDP 里几乎看不到这些并行,profile 出来的串行画面**比真实运行更悲观**。

建议分析 GLES 时,**别把 SDP 时间线的串行排布当成真实并行度**。各阶段的绝对开销仍可信,但"是否重叠"的结论要打折扣。Vulkan 下这个问题相对轻。做并行度判断优先信 Vulkan trace。

`Rendering workloads`

暂不知何用。

是什么SDP 中的一个视图/分类项,作者也未确认其确切用途。

意义原文坦诚"暂不知何用",这里不臆测。按字面推测可能是按 workload 类型(图形/计算/传输)聚合的呈现,但**未经验证,不作为分析依据**。

建议遇到时可展开探索对照实际渲染步骤;在确认含义前,以 `Rendering stages` 和具体计数器为准。

`Vulkan API Trace`

CPU perf samples。显示 Vulkan API Call 的时间点，因为是 Driver 层行为，往往超前于上面的 GPU Metrics。但 SDP 显示了 Command Buffer 被 submit 到 GPU 的时间点，这很不错。

是什么CPU 侧的 Vulkan API 调用采样时间线,显示每个 API call 的发生时刻,以及 Command Buffer 提交到 GPU 的时间点。

意义这是打通 **CPU 提交 ↔ GPU 执行**两个世界的桥梁。API 调用是 driver 层 CPU 行为,自然**超前于**下方 GPU 实际执行的 metrics。作者特别点赞"显示 Command Buffer submit 时间点"——因为这能让你判断 GPU 是不是在**等 CPU 喂命令**(即 CPU-bound)。

优化关联当 `% Shader Busy` 低、怀疑 CPU-bound 时,对齐本 trace 与 GPU 时间线:若 GPU 空档恰好在等下一个 submit → 确诊 CPU 提交瓶颈,去优化 render thread/合批/减少 command buffer 拆分。是 CPU-GPU 协同分析的关键视图。

Snapdragon Profiler 性能指标详解 · 整理自 SDP counters 笔记 · 仅供 Adreno GPU 性能分析参考
