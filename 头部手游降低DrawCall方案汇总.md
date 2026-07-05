# 头部手游降低 Draw Call 方案汇总

🧭 **知识库导航**：本页是单款案例的横向资料。其 TBDR 片上缓存优化的**方法论纵贯**见 [TBDR 跨平台完整技术方案 · §5](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md)（DrawCall 控制（和平精英重剔除）的原理出处） | [📚 知识库总导航](./知识库导航_README.md)

# 头部手游 · 降低 Draw Call 方案汇总

从合批机制到资源治理，跨 Unity / UE 主流手游的 DC 优化实践与避坑清单

**覆盖引擎**Unity（Built-in / URP）· UE4 / UE5

**视角**移动端渲染性能

**定位**方案库 / 速查手册

**核心指标**Draw Call · SetPass · Batches

资料说明

本文为横向专题汇总，基于 Unity / Epic 官方文档、Unity Open Day《诛仙手游》分享、头部手游公开技术资料及通用引擎机制整理。各游戏未公开全部细节，部分案例为同品类通用做法的归纳，不代表某款游戏的精确实现。所有数值均为典型范围或定性描述，非伪精确数字。

## 一、核心结论速览：DC 优化的三层心智模型

#### ① 合批机制层（引擎给的）

- 同 Mesh + 同材质 → 一次提交
- Unity：Static / Dynamic Batching、GPU Instancing、SRP Batcher
- UE：Dynamic Instancing、ISM/HISM、Merge Actor、Nanite GPU Scene

#### ② 资源治理层（美术给的）

- 纹理图集 / Texture Array
- 统一材质、共享 Shader、收敛材质实例
- 单材质槽、LOD 链、Pivot 一致

#### ③ 场景玩法层（设计给的）

- 阴影分帧 / 关闭非必要投影
- UI 图集 + 层级排序
- 大世界 streaming + Imposter + HLOD

总体取舍

降低 DC 的本质不是"消灭 Draw Call"，而是**让 GPU 以最少的批次处理最多的图元，同时不破坏剔除与 LOD**。盲目 Merge Mesh 会破坏遮挡剔除、增大 Overdraw、抬高内存；过度合批换来的崩溃比高 DC 更糟。头部手游的共识是：机制 + 资源 + 玩法 三层协同，按"先统一材质 → 再合批 → 最后场景调度"的顺序施策。

## 二、先搞清楚：Draw Call / SetPass / Batch 到底是什么

| 概念 | 含义 | 开销在哪 | 优化目标 |
| --- | --- | --- | --- |
| **Draw Call** | CPU 调用图形 API 绘制一组图元（如 `glDrawElements` / `DrawIndexedPrimitive`） | CPU 提交 + 驱动开销，移动端尤甚 | 合并同状态绘制 |
| **SetPass Call** | 渲染状态切换（Shader / 材质 / 纹理 / 混合态变更） | 比 DC 更贵，状态切换打断管线 | 统一 Shader / 材质，减少切换 |
| **Batch** | 一组合并到一起执行的 Draw Call | — | 提高单 Batch 内图元数 |

移动端的非线性代价

实测经验：iOS Metal 下从 500 → 1200 DC，提交耗时增长可达约 **3.7×**，远超线性预期。移动端单帧 DC 一旦超过约 **1000~1500**，RHI/Submission 线程极易成为瓶颈。**SetPass 往往比 DC 更值得盯** —— 同样 200 个物体，1 个 Shader 和 50 个材质变体的代价天差地别。

## 三、Unity 体系四大合批机制

Unity 提供四套机制，**适用对象、CPU/内存代价、是否支持动态物体各不相同**，需要分类施策而非二选一。

| 机制 | 适用对象 | 原理 | 代价 / 限制 | 降 DC 还是降 CPU |
| --- | --- | --- | --- | --- |
| **Static Batching**静态 | 完全不动的几何体（建筑、环境） | 预合并共享材质的网格到大 VB/IB | 显著增加内存（合并后顶点常驻）；大场景需权衡，甚至主动减少静态合批物体防内存崩溃 | 真正减少 DC |
| **Dynamic Batching**小网格 | 顶点 < 300、属性 < 900 的小网格，同材质 | CPU 每帧分组、变换顶点后一次绘制 | CPU 开销大；网格大了反而浪费 CPU 找批次；SkinnedMesh 顶点多基本无效 | 减少 DC，但增 CPU |
| **GPU Instancing**大规模重复 | 大量相同 Mesh + 材质（树、草、子弹、道具） | 一次提交 + 实例数据，硬件复制绘制 | 需 Shader 支持 instancing；用 `MaterialPropertyBlock` 改参数避免复制材质 | 大幅减少 DC（500 树 → 1~2 DC） |
| **SRP Batcher**URP/HDRP | SRP 兼容 Shader 的几乎所有物体（含动态） | 材质属性收进 `UnityPerMaterial` CBuffer，相同 Shader 复用 CBuffer，仅切渲染状态批量提交 | **不减少 Batches 数量**，而是降低 CPU 准备/上传开销；要求 Shader 兼容 | 主要降 CPU 提交成本 |

关键认知：SRP Batcher ≠ 减少 DC

很多人误以为开了 SRP Batcher 就万事大吉。它的作用是**把同 Shader 物体的 CBuffer 复用、降低 CPU 渲染指令消耗**，Batches 数字可能不降甚至上升，但 CPU 渲染线程时间显著下降。它和 GPU Instancing 是**互补**关系：Instancing 管"同 Mesh 海量重复"，SRP Batcher 管"同 Shader 多样物体"。

## 四、UE 体系：Dynamic Instancing / ISM / HLOD / Nanite

UE 的自动合批（Auto Instancing）默认就比较强，核心是**不要写出"破坏自动合批"的内容**，并在合适场景主动用 ISM/HISM 与离线合并。

#### Dynamic Instancing（自动合批）

引擎自动合并"相同 Mesh + 相同材质"的静态网格绘制。前提：材质参数一致、不被逐物体动态材质实例打断。这是 UE 降 DC 的**地基**，很多优化只是"别把它破坏掉"。

#### ISM（Instanced Static Mesh）

把成组的相同静态网格塞进一个组件，一次提交多实例。适合背景道具、重复建筑模块。**仅 Nanite 项目优先用 ISM**（Nanite 自带剔除/LOD）。

#### HISM（Hierarchical ISM）

在 ISM 基础上加层级，按分组 bound 做剔除和 LOD。**上千个不动的实例（草、石、树）首选 HISM**。代价：每个 LOD 额外一个 DC + 渲染线程开销；实例若会动则易出错。

#### Merge Actor / HLOD

离线把多个非动态资产合并为一个网格（Merge Actor），或用 HLOD 把远处一簇物体合成代理网格。**注意：**合并会破坏单体遮挡剔除；高通在 Snapdragon UE5 实践里甚至建议移动端**避免 HLOD**，改用 DataLayers + Sequencer 批量激活资产。

#### Nanite + GPU Scene

Nanite 把几何虚拟化，帧预算不再受 polycount / DC / mesh 内存约束，按屏幕像素流式加载几何。可在 ISM/HISM 中引用 Nanite 网格。移动端启用需定制代码（高通方案），并非所有机型适用。

### Custom Primitive Data / Per Instance Custom Data

ISM 配合 **Custom Primitive Data** 与 **Per Instance Custom Data**，可以"在不生成新动态材质实例"的前提下给每个实例传差异化参数（颜色、随机种子、状态），**进一步避免因逐物体材质而打断合批**。这是 UE 侧"既要差异化又要合批"的标准答案。

## 五、资源层治理：让合批"能发生"

合批机制是引擎给的，但**能不能合，取决于美术资源**。下面是"让一个网格具备最大合批潜力"的资产清单（Unity / UE 通用）。

| 要求 | 为什么 |
| --- | --- |
| 每个网格单材质槽 | 多材质槽 = 每实例多个 DC，直接废掉 instancing |
| Tiling / 图集纹理 | 纹理可跨实例复用，UV 连续才能硬件合批 |
| Albedo 不烘唯一细节 | 烘焙细节放 Normal/AO，而非 albedo，否则每个都唯一 |
| Pivot 一致 | 实例散布 / 网格摆放可预测 |
| 有 LOD 链 | 防高模在远处仍以高精度 instancing |
| LOD0 控制在预算内 | HISM 剔的是 LOD 不是单三角面，高模 LOD0 仍然贵 |
| 顶点色驱动变化 | 用顶点色做色调/湿度/季节差异，不换贴图就能保持合批 |

#### 纹理图集（Texture Atlas）

- UI / Sprite 用图集是降 DC 最直接的手段
- 但图集尺寸失控会爆内存，按场景/功能拆分、运行时动态加载
- UE 侧把法线/粗糙度/金属度打包成图集，避免每模型独占 3~5 张 2K 纹理

#### 材质收敛

- 同一基础材质生成 >200 个唯一材质实例 → Shader 变体爆炸 + 绑定切换开销
- 《诛仙手游》：全场景绝大多数物体用 1~3 个 Shader 完成，SRP Batch 极大减少 SetPass
- 用 MPB / Per Instance Data 做差异化，而非复制材质

## 六、场景与玩法层：阴影、UI、植被、大世界

#### 阴影：被低估的 DC 大户

- 每个投影物体 = 额外一遍 ShadowMap 绘制，DC 翻倍
- **分帧渲染**：不同 CSM 级别用不同刷新频率（《诛仙手游》做法）
- 非必要投影直接关；用 Blob / 贴片假阴影替代
- 烘焙静态阴影到 Lightmap，运行时零成本

#### UI：手游 DC 重灾区

- Sprite Atlas 合图，同图集同材质才能合批
- 注意层级穿插：不同图集交错会打断合批
- 避免每个 UI 元素独立动画器（用 DOTween / 补间）
- 文字与图标分图集、按 depth 排序

#### 植被 / 大规模重复

- Unity：GPU Instancing（Domi Online 10 万棵树靠这个）
- UE：HISM / Foliage Mode
- 远景植被用 **Imposter**（八方向贴图代理，原神做法）

#### 大世界 streaming

- 视野外不提交：空间 + 时间局部性（《诛仙手游》强调"局部性"）
- HLOD / 远景代理网格合并远处簇
- UE5 移动端：DataLayers + Sequencer 批量激活资产（高通方案）

## 七、头部手游案例横向对比

| 游戏 / 来源 | 引擎 | 降 DC 关键手段 | 亮点 |
| --- | --- | --- | --- |
| **诛仙手游** Unity Open Day 分享 | Unity URP | 升级 SRP Batcher（决定性因素）；场景 1~3 Shader 收敛 SetPass；阴影分帧；大场景主动减少静态合批防内存崩溃；DrawMesh 直接绘制 | "平衡内存前提下尽量减 DC/SetPass"，强调空间+时间局部性 |
| **原神** 公开技术分享 | Unity SRP | GPU Instancing 植被；远景 Imposter 代理；HLOD；跨端统一 Shader | 开放世界海量植被 + 远景 Imposter 显著压批次 |
| **Domi Online** 优化实战 | Unity | 10 万棵树用 GPU Instancing 实现"零崩溃" | 大规模重复物体的 Instancing 教科书案例 |
| **UE5 移动 AAA** 高通 Snapdragon 方案 | UE5.4 | 启用 Nanite（定制代码）；避免 HLOD；DataLayers + Sequencer 批量激活；批量资产管理；ASTC 打包 | Snapdragon 8 Elite 上跑 UE5 内容 30FPS 的工程实践 |
| **通用 MOBA / FPS** 品类共性 | Unity / UE | 角色单材质槽 + 图集；子弹/特效 GPU Instancing；UI 合图；皮肤共用 Shader | 长线运营靠 Shader 变体治理压住 DC 膨胀 |

## 八、避坑清单：合批为什么"断了"

#### Unity 常见打断点

- MaterialPropertyBlock 打断 SRP Batcher：挂载 MPB 的物体从"SRP Batch"降级为"Non SRP Batch"，断崖式下跌。解法：Shader 把可变属性纳入 `UnityPerMaterial`、用 Instancing 路径、或集中管理 MPB。
- Renderer.material（非 sharedMaterial）：脚本访问 `.material` 会复制材质并返回新副本，破坏现有批次。要读批处理材质用 `.sharedMaterial`。
- 不同图集 / 不同 Shader 关键字 / 不同渲染队列交错 → 打断
- Shader 和关键字完全一致却仍分两个 SRP Batch：常因 CBuffer 布局或材质属性差异（FrameDebugger 排查）

#### UE 常见打断点

- 逐物体动态材质实例（Dynamic Material Instance）→ 破坏 Dynamic Instancing。改用 Per Instance Custom Data。
- 一个路灯拆成灯杆/灯罩/玻璃/螺丝多个 Actor 且材质不同 → 无法进 ISM/HISM 流水线
- 盲目 Merge Mesh 破坏遮挡剔除 → Overdraw 反升、内存反增
- Shader 用了 World Position Offset / Vertex Interpolator / Custom UV（高通移动方案建议禁用，影响合批与性能）

黄金法则

**合批目标不是消灭 Draw Call，而是让 GPU 以最少批次处理最多图元。**过度合并破坏剔除/LOD、增大 Overdraw 与内存压力，得不偿失。先 Profile 定位"合批断裂点"，再分层施策。

## 九、落地 Checklist 与定位工具

#### 优先级施策顺序

1. 1 统一材质 / 共享 Shader，收敛材质实例数
2. 2 纹理打图集 / Texture Array
3. 3 开 SRP Batcher（URP）/ 确认 UE Auto Instancing 未被破坏
4. 4 大规模重复 → GPU Instancing / HISM
5. 5 静态场景 → Static Batching / Merge Actor（权衡内存）
6. 6 阴影分帧、关非必要投影、UI 合图
7. 7 远景 → Imposter / HLOD / streaming

#### 定位工具

- **Unity**：Frame Debugger（数 DC）、Profiler→Rendering（Batches / Saved by batching）、Stats 浮层
- **UE**：`stat RHI`、`stat SceneRendering`（draw primitive calls）、`ProfileGPU`、Unreal Insights（per-pass flame graph）
- **通用**：RenderDoc 截帧看 Overdraw 与合批断点；Snapdragon Profiler（Android）

验收标准

一个良好优化的场景：500 棵 instanced 树应显示 **1~2 个 DC**，而非 500。若重复物体仍显示数百 DC，依次检查：材质是否真的一致？Shader instancing 是否开启？LOD 切换是否打断了实例组？

## 十、参考资料

- Unity 官方《移动游戏开发者的艺术优化技巧（第二部分）》—— Batching / 阴影 / 光探针 / sharedMaterial
- Unity Open Day 北京站《诛仙手游》性能优化与质量保证 —— SRP Batch 升级、Shader 收敛、阴影分帧、局部性
- Unreal Engine 官方文档：Instanced Static Mesh Component（ISM/HISM/Dynamic Instancing/Custom Primitive Data）
- Qualcomm《Run Unreal Engine 5 content at 30FPS on Snapdragon》—— Nanite 定制、避免 HLOD、DataLayers + Sequencer
- 网易《Unity 手游优化实战：从 10 万棵树到零崩溃》—— GPU Instancing 大规模重复物体
- CSDN《彻底解决 MBP 打断 SRP Batcher 合批问题》—— MaterialPropertyBlock 打断原理与四种解法
- BitSoul《GPU Instancing and Draw Call Batching for Game Assets》—— Unity/UE5 资产合批清单与 profiling checklist
- 个人经验：移动端 RHI/Submission 线程 DC 提交瓶颈分析

头部手游降低 Draw Call 方案汇总 · 内部研究文档 · 信息基于公开演讲、官方文档与工程经验整理 · 数值为典型范围非精确值
