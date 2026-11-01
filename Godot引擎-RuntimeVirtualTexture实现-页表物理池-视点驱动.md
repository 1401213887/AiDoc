# Godot 引擎 Runtime Virtual Texture 实现：页表 + 物理池 + 视点驱动

在 Godot 4.8（Forward+ / Mobile）里仿 UE RVT 落地的可用实现：**页表 + 物理页池 + 视点驱动请求**，页的渲染复用引擎已有的「深度 + 材质」pass（与 VoxelGI 动态物体同一个），材质侧用一份 `gdshaderinc` 采样。自测在 Vulkan Forward+、Vulkan Mobile、D3D12 三套后端跑通；同一画面「直渲 vs 从 RVT 采样」逐像素平均差 远景 0.0020 / 近景 0.0086；全量 doctest 1439 例（425756 条断言）通过。关键参数：体积 64×8×64 m、32×32 页、页 128 texel、边框 4（stride 136）、物理池 24×24 = 576 页、3264×3264 px、level0 texel = 15.6 mm。

## 1 结论与全貌

> 这套 RVT 不新增任何材质管线：页由引擎现成的材质 pass 画出来，CPU 侧只负责三件事——页表、物理页池、以及「这一帧该画哪些页」。

实现分成四层，层与层之间只通过 RID 和小的数据结构耦合：

| 层 | 位置 | 职责 |
|---|---|---|
| 资源 / 节点 | `scene/resources/runtime_virtual_texture.cpp`、`scene/3d/runtime_virtual_texture_volume.cpp` | 对外的 `RuntimeVirtualTexture` 资源与 `RuntimeVirtualTextureVolume` 节点；把 uniform 写进材质 |
| 服务器接口 | `servers/rendering/rendering_server.h:793-805`（新增段） | `runtime_virtual_texture_*` 一组接口，跨线程安全 |
| 场景剔除 / 调度 | `servers/rendering/renderer_scene_cull_rvt.cpp`、`servers/rendering/runtime_virtual_texture_page_manager.cpp` | 视点收集、页请求、LRU 分槽、逐页剔除几何体、失效传播 |
| RD 后端 | `servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp` | 物理池 / 页表纹理 / scratch 帧缓冲 / compose compute |

**图 1**：RVT 一帧的数据流。请求在 CPU 决策，页在 GPU 生成，材质只读页表与池。（图见 HTML 版）

demo 工程（`misc/rvt_demo`）跑出来的关键数字，后文反复用到：

| 参数 | 取值 | 含义 |
|---|---|---|
| 体积 | 64 × 8 × 64 m | `RuntimeVirtualTextureVolume.size`，Y 是渲染高度范围 |
| 页数（level 0） | 32 × 32 = 1024 页 | `log2_tile_count = 5` |
| 页尺寸 | 128 × 128 texel，边框 4 | 实际 tile stride = 136 px |
| 虚拟分辨率 | 4096 × 4096 texel | 32 × 128，level0 texel = 15.6 mm |
| 物理池 | 24 × 24 = 576 页，3264 × 3264 px | RGBA8 / R32F |
| 页表 | 32 × 32，6 个 mip | RGBA8，mip 数 = log2 + 1 |
| 每帧页预算 | 16 | `max_pages_per_frame` |

### 三条设计取舍

**① 页渲染复用引擎材质 pass**（`RendererSceneRender::render_material`）。

- 换来了什么：零新增 shader / pipeline，输出与场景里材质完全一致；VoxelGI 已经验证过这条路径能拿到 albedo / normal / orm / depth。
- 付出了什么：输出集合被引擎写死为 5 个附件，不能像 UE 那样在材质里加「RVT 输出」节点自定义通道；也没有压缩。
- 什么时候失效：需要额外通道（单独的 AO、自定义 mask、世界高度由材质自己写）时不适用；高度只能由深度反推。

**② 用视点推导代替 GPU feedback。**

- 换来了什么：纯 CPU、无回读延迟、结果确定（同一相机轨迹每帧请求集一致，便于回归测试）。
- 付出了什么：只能保守过请求——按距离和像素足迹推算「大概需要哪几层」，比真实采样到的页多。
- 什么时候失效：相机高速平移或超远距离看细节时，池容量会先于画质成为瓶颈，表现为细层被截断。

**③ 页表带 mip、缺页回退到祖先。**

- 换来了什么：采样端用一条 `texelFetch(page_table, page, level)` 就拿到「这一格现在能读到哪一层」，零分支、不出现空洞。
- 付出了什么：缺页时画质是「突然变糊」而不是平滑过渡；页表多出约 1/3 的 mip 链内存（本例 4 KB → 5.5 KB，可忽略）。
- 什么时候失效：冷启动第一帧，连最粗层都还没画出来时 `valid = 0`，采样返回黑；常驻层策略（每帧无条件请求最粗两层）把这段窗口压到 1～2 帧。

## 2 数据流：从相机到页请求

> 没有 feedback 就从相机反推：每个视点按「像素足迹」算出需要哪一层，按视锥裁掉看不见的页。

UE 的 RVT 由 GPU feedback 决定画哪些页：材质采样时写页号，回读后只画真正被采样的页。Godot 的 spatial shader 没有可写的 feedback buffer（存储缓冲在各类材质里的支持不一致），所以这里换成解析式推导：**用上一帧渲染过的相机位姿、投影和视口高度，算每个页的像素足迹**。

核心公式（`servers/rendering/runtime_virtual_texture_page_manager.cpp:218-222`）：

```
world_per_pixel = 2 / (|P[1][1]| * viewport_height)      # P 是相机投影矩阵
pixel(d)        = world_per_pixel * d                     # 透视；正交相机直接用常数
lod(d)          = clamp(log2(pixel(d) / texel0) + bias, 0, max_level)
```

代入 demo 数字：体积 64 m 宽、32 页 × 128 texel → `texel0 = 64 / 4096 = 0.0156 m`。相机距 50 m、fov 60°、720 p 时 `world_per_pixel = 2 / (1.732 × 720) ≈ 0.0016 m`，该距离下每像素覆盖 `0.08 m`，于是 `lod = log2(0.08 / 0.0156) ≈ 2.36`——取 mip 2 与 3 混合。这个数值与材质里 `dFdx/dFdy` 算出的 LOD 是同一个量纲，所以 CPU 的页请求和 GPU 的采样能对上。

页的选择走四叉树（`servers/rendering/runtime_virtual_texture_page_manager.cpp:224-266`）：

1. 从最粗层（level = max）开始，每次把节点拆成 4 个子页。
2. 对当前页求最近点 `dmin` 与最远点 `dmax`（8 个角点），得到 `lf_min`、`lf_max` 两个 LOD。
3. `lf_min <= level <= lf_max + 2` 时把这一页加进请求集（`+2` 是给斜视角留的余量）。
4. 只有 `lf_min < level` 时才继续往下一层拆——远处的大块整块保留，不会无谓细分。
5. 视锥剔除在本地空间做：`frustum` 由相机平面乘 `to_local` 传入，页盒先向外膨胀半页，避免相机微微一动的边缘抖动。

另外每帧无条件请求最粗的 `resident_levels = 2` 层（本例 2×2 + 1×1 = 5 页），保证任何视角下页表里都有东西可读，冷启动不会看到整片黑。

**图 2**：页表与物理池。页表 texel 记录「池坐标 + 驻留层 + 是否有效」，缺页时读到的仍是祖先页。（图见 HTML 版）

> **为什么请求集要截断** 请求数可能远大于池容量。`take_pending`（`servers/rendering/runtime_virtual_texture_page_manager.cpp:285-348`）先按「层由粗到细、同层距离近优先」排序，再截到池容量；每帧只提交 `max_pages_per_frame` 页去渲染。被截掉的请求下一帧还在（相机没动），所以表现为「慢慢变清晰」，而不是随机抖动。

## 3 页的渲染：复用材质 pass 与 compose

> 一个页就是一次正交投影下的材质绘制，外加一个 compute 把 5 张附件转成池里的两个层。

引擎里已经有现成的入口：`RendererSceneRender::render_material`（`servers/rendering/renderer_scene_render.h:328`，实现见 `servers/rendering/renderer_rd/forward_clustered/render_forward_clustered.cpp:3061`）。它以 `PASS_MODE_DEPTH_MATERIAL`（`servers/rendering/renderer_rd/forward_clustered/render_forward_clustered.h:208`）绘制，shader 侧把材质结果写进 5 个附件（`servers/rendering/renderer_rd/shaders/forward_clustered/scene_forward_clustered.glsl:3094-3110`）。VoxelGI 的动态物体就是走这条路（`servers/rendering/renderer_rd/environment/gi.cpp:3281`），帧缓冲布局也照抄（`servers/rendering/renderer_rd/environment/gi.cpp:2805-2812`）。

| 附件 | 格式 | 内容 |
|---|---|---|
| albedo | R8G8B8A8_SRGB | 线性 albedo（写入时自动编码为 sRGB），alpha = 覆盖 |
| normal | R8G8B8A8_UNORM | `encode24(视空间法线) * 0.5 + 0.5` |
| orm | R8G8B8A8_UNORM | ao / roughness / metallic / sss |
| emission | R16G16B16A16_SFLOAT | 自发光（本实现未落进池） |
| depth | R32_SFLOAT | `-vertex.z`，视空间线性距离 |
| depth-stencil | D16 / D24 | 深度测试用 |

**图 3**：页渲染管线：正交相机 → 材质 pass 的 5 个附件 → compose compute 写进池层。（图见 HTML 版）

compose 由 `RuntimeVirtualTextureRD::render_page` 发起（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:220-243`）：先调 `render_material` 画进 136×136 的 scratch 帧缓冲，再对整张 136×136 派发 compute，写目标池 tile 的对应位置。shader 在 `servers/rendering/renderer_rd/shaders/runtime_virtual_texture_compose.glsl`，三件事：

- **sRGB 回编码**：池是 `R8G8B8A8_UNORM`（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:90`），没有自动 sRGB 语义；附件是 sRGB，`texelFetch` 拿到的是线性值，所以 compose 里手动 `linear_to_srgb` 再存（`servers/rendering/renderer_rd/shaders/runtime_virtual_texture_compose.glsl:44-46`）。
- **上下翻转**：材质 pass 的输出相对相机 up 向量是翻的，`flip_y = 1`（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:233`）。这一条是靠截图对比发现的——不翻转时整张 RVT 上下颠倒。
- **法线映射**：材质写的是页相机视空间法线，compose 转成体积本地空间 `(n.x, -n.y)` 存进层 1 的 RG（`servers/rendering/renderer_rd/shaders/runtime_virtual_texture_compose.glsl:68-73`），"朝上"的那个分量不存，材质侧用 `sqrt(1 - x² - z²)` 还原。

> **法线只能还原「朝上」的一半** 层 1 只存 x、z 两个分量，材质用 `max(1 - x² - z², 0)` 取正的 y。对地形、路面、贴花这类朝上的表面完全够用；对悬垂、倒角侧面会丢符号。这正是 UE RVT 的同类限制——它默认也是俯视投影。

## 4 一个页的一生

> 页的生命周期只有六个状态；关键不变量是「任何时刻页表 texel 要么命中，要么回退，绝不指向一个正在渲染的空槽」。

**图 4**：页的六个状态。③ 渲染中页表仍指向祖先，④ 落盘时才原子地替换本级条目。（图见 HTML 版）

1. **请求**（`servers/rendering/runtime_virtual_texture_page_manager.cpp:205-216`）：同一页多次请求取最小优先级值（距离），不重复入队。
2. **分槽**（`servers/rendering/runtime_virtual_texture_page_manager.cpp:167-190`）：先取空闲槽；没有就 LRU 淘汰，但**本帧被请求过的页不淘汰**，避免刚渲染完又被换出去。
3. **渲染中**：槽已占，但页表还没改——所有指向这一格的 texel 仍读祖先页，所以采样端看不到空洞。
4. **落盘**（`servers/rendering/runtime_virtual_texture_page_manager.cpp:350-363`）：`mark_rendered` 写本级 entry，并把「比它粗」的后代 texel 全部刷成它；`valid = 255`。
5. **常驻**：材质 `texelFetch` 命中，`res` 就是这一层。
6. **失效 / 淘汰**：dirty 的页下一帧重新渲染（复用原槽）；被淘汰的页从映射里移除，后代 texel 重算为祖先值（`servers/rendering/runtime_virtual_texture_page_manager.cpp:136-152`）。

单测直接把这三条不变量钉住（`tests/servers/rendering/test_runtime_virtual_texture_page_manager.cpp:58-127`）：祖先回退、粗层优先与预算、LRU 不淘汰本帧页、失效后原位重渲、视点请求近处更细、视锥剔除。

## 5 材质侧采样：页表间接与祖先回退

> 采样端只有一次页表 texelFetch，加两次池采样；缺页不报错，只是更糊。

材质通过 `rvt.apply_to_material(mat)` 拿到 6 组 uniform（`scene/resources/runtime_virtual_texture.cpp:170-183`）：

| uniform | 类型 | 含义 |
|---|---|---|
| `rvt_page_table` | sampler2D（最近邻） | 页表，按 mip 取驻留层 |
| `rvt_physical0` | sampler2D（线性） | 池层 0：sRGB 颜色 + 覆盖，或归一化高度 |
| `rvt_physical1` | sampler2D（线性） | 池层 1：法线 xz、粗糙度、金属度 |
| `rvt_params0` | vec4 | level0 页数、最大层、tile 像素、边框像素 |
| `rvt_params1` | vec4 | 池宽高像素、体积高、材质类型 |
| `rvt_world_to_uv` | mat4 | 世界坐标 → 体积 0..1 空间（x→u，z→v，y→高度） |
| `rvt_normal_basis` | mat4 | 体积本地 → 世界旋转（法线用） |

**图 5**：采样路径。两次 texelFetch 分别取 mip l0 与 l0+1，各自解析出自己的驻留层，再混合。（图见 HTML 版）

```
shader_type spatial;
#include "res://rvt.gdshaderinc"

varying vec3 world_pos;
void vertex() { world_pos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz; }

void fragment() {
    vec4 color; vec3 n; float rough; float metal; bool inside;
    rvt_sample(world_pos, color, n, rough, metal, inside);
    ALBEDO   = color.rgb;
    NORMAL   = normalize((VIEW_MATRIX * vec4(n, 0.0)).xyz);
    ROUGHNESS = rough;
    METALLIC  = metal;
}
```

入口在 `misc/rvt_demo/rvt.gdshaderinc`。核心是 `rvt_physical_uv`（`misc/rvt_demo/rvt.gdshaderinc:13-28`）：按目标层算出页号 → `texelFetch` 取 entry → entry 的 B 通道给出**实际驻留层** → 用它在页内定位 → 换算成池 UV。两次调用分别取 `l0` 和 `l0+1`，按 `fract(lod)` 混合（`misc/rvt_demo/rvt.gdshaderinc:51-74`）。两层的 `res` 可以不同（一层命中、一层回退），混合依然成立。

entry 是一颗 RGBA8：R、G 是池内 tile 坐标（0..255），B 是驻留层，A = 255 表示有效。所以池每边最多 255 页；demo 的 24 页远低于上限。

> **为什么把回退信息放在纹理 mip 里，而不是传到材质里算** 如果由材质自己按 `res` 做分支或循环，采样端会变成动态分支，移动端代价高且不好向量化。把「这一格现在能读到谁」预先写进页表的每一级 mip，采样端就退化成一条固定路径的 texelFetch；代价是每次落盘/淘汰要在 CPU 侧刷新受影响的后代 texel（页表很小，这点开销可忽略）。

## 6 资源、调度与失效语义

> 谁改动了画面，谁就要负责让页失效；能自动判断的交给场景剔除，判断不了的留给用户显式调用。

**图 6**：每帧时序。建池必须早于材质重绑；画页晚于视点收集。（图见 HTML 版）

时序上只有两个挂点（`servers/rendering/renderer_scene_cull.cpp`）：`update()` 里的 `servers/rendering/renderer_scene_cull.cpp:4434` 负责建/换池，`render_probes()` 里的 `servers/rendering/renderer_scene_cull.cpp:3906` 负责画页；视点在 `_render_scene()` 的 `servers/rendering/renderer_scene_cull.cpp:3354` 收集。

> **顺序红线（真实踩过的坑）** 最初把「建 GPU / 替换纹理」放在 `render_runtime_virtual_textures()` 里（即 `render_probes()`），结果材质的 uniform set 指向已释放的 RID：日志里是 `Uniforms supplied ... are not the same format` 与 `Uniforms were never supplied`，画面整帧不刷新。原因是 `texture_replace` 会重建材质依赖，而 `update_dirty_instances()` 已经在同一帧更早跑完了。把建池前移到 `update()` 开头、`update_dirty_instances()` 之前，材质才能在同一帧拿到新纹理。

| 改动 | 是否自动失效 | 入口 |
|---|---|---|
| 实例 transform / AABB 变化 | 是 | `servers/rendering/renderer_scene_cull.cpp:1754` |
| 实例可见性切换 | 是 | `servers/rendering/renderer_scene_cull.cpp:1098` |
| 实例 layer_mask 变化 | 是（只失效受影响的层） | `servers/rendering/renderer_scene_cull.cpp:984` |
| 实例移出场景 | 是 | `servers/rendering/renderer_scene_cull.cpp:863` |
| 体积节点移动 / 改尺寸 | 是（全量失效） | `scene/3d/runtime_virtual_texture_volume.cpp:45-57` |
| 改 cull_mask / scenario / 类型 / tile 参数 | 是（全量失效或重建池） | `servers/rendering/renderer_scene_cull_rvt.cpp:63-100` |
| 材质参数（颜色、贴图）变化 | 否 | 需手动 `rvt.invalidate(aabb)` 或 `invalidate_all()` |

材质参数这一项是设计上的取舍：引擎没有「材质参数变了」的通知回调，逐材质轮询代价大于收益。demo 里的重着色用例就是显式调用 `invalidate` 后通过的（像素从原色变成 `(0.80, 0, 0.80)`）。

## 7 与 UE RVT 的对照

> 同构的是骨架，分叉只发生在「页请求从哪来」和「材质能输出什么」。

**图 7**：UE 与 Godot 实现的逐项对照。（图见 HTML 版）

把两条实现放在一起看，页表、物理池、按需渲染、缺页回退都是同一套思路；真正不同的是：

- **页请求来源**：UE 走 GPU feedback，采样到什么就画什么；Godot 走相机解析式推导，会多画。
- **页渲染**：UE 是专用 pass + 材质里的 RVT 输出节点；Godot 直接复用 VoxelGI 那条材质 pass，输出集合固定。
- **压缩**：UE 的物理池支持 BC；Godot 这里是裸 RGBA8，换来的是「任何平台、任何材质都能跑」。

## 8 验证矩阵与实测

> 所有数字来自 `misc/rvt_demo` 的自测脚本，退出码即结论。

自测的做法：画面左半直接渲染，右半由一块采样 RVT 的接收面覆盖；两块区域用不同 layer 控制可见性，相机 `cull_mask` 在「只看左半 + 接收面」与「只看左右两半直渲」之间切换，两张截图逐像素比对。所有源物体都在 `cull_mask` 里，所以 RVT 始终是完整内容。

| 配置 | 结果 | 远景平均差 | 近景平均差 |
|---|---|---|---|
| Vulkan Forward+ | PASS | 0.0020 | 0.0086 |
| Vulkan Mobile | PASS | 0.0019 | 0.0050 |
| D3D12 | PASS | 0.0020 | 0.0086 |
| Forward+，池 8×8（64 页） | PASS（淘汰 17 页） | 0.0020 | 0.0086 |
| Forward+，池 12×12（144 页） | PASS（淘汰 17 页） | 0.0020 | 0.0088 |
| 运行时改 tile 128→64、池 24→40、边框 4→2 | PASS | — | 0.0019 |

其它分支：

- **WORLD_HEIGHT**：塔顶读回 0.753（期望 0.75），地面 0.503（期望 0.5）。
- **重着色**：改材质颜色后 `invalidate`，对应像素变为洋红 `(0.80, 0, 0.80)`。
- **移动物体**：绿盘从 `(20, 0, 20)` 移到 `(20, 0, -20)`，旧位置恢复地面色、新位置为 `(0, 1, 0)`。
- **单元测试**：`RVTPageManager` 6 个用例；全量 doctest 1439 例 / 425756 条断言通过。

```
# 自测（退出码 0 = PASS）
godot --path misc/rvt_demo -- --rvt-test
godot --path misc/rvt_demo -- --rvt-test --pool=8        # 小池，压淘汰
godot --path misc/rvt_demo -- --rvt-height               # 高度类型
godot --path misc/rvt_demo --rendering-method mobile -- --rvt-test
godot --path misc/rvt_demo --rendering-driver d3d12 -- --rvt-test

# 单元测试
godot --headless --test --test-case="*RVTPageManager*"
```

## 9 已知限制与约束

> 能力边界都是取舍的直接结果，不是遗漏。

- **过请求**：没有 feedback，按视点推算会多画页。池容量要按「最坏视角」而不是「典型视角」估算。
- **无压缩**：demo 池 3264²，两层 RGBA8 合计约 85 MB。大世界要么缩池、要么换 R32F 高度层单独存。
- **输出固定**：只能拿 albedo / normal / roughness / metallic / 高度；自定义通道（AO、mask）不支持。
- **只俯视**：投影沿节点 −Y；法线按「朝上」还原，悬垂面会丢符号。
- **材质参数不自知**：改颜色/贴图后必须手动 `invalidate`。
- **上限**：`pool_size ≤ 255`（entry 用 8 bit 编码 tile 坐标）、`log2_tile_count ≤ 10`、`tile_size ≤ 1024`。
- **后端**：仅 RD（Forward+ / Mobile）；GL Compatibility 与 dummy 后端走空实现。
- **内容范围**：页里画的是不透明与 alpha 面；天空、粒子、贴花不进 RVT。
- **跨帧延迟**：页在 `render_probes()` 生成，材质当帧就能读到；但页请求基于上一帧相机，相机瞬移时会有一两帧的粗页。

## 10 源码索引

> 新增 13 个引擎源文件与 3 份类文档、改动 14 个既有文件；下表按职责索引。

| 文件 | 职责 | 关键位置 |
|---|---|---|
| `servers/rendering/runtime_virtual_texture_page_manager.h/.cpp` | 页表 / 池 / LRU / 四叉树请求（纯 CPU，可单测） | `configure:40`、`_resolve:95`、`_allocate_slot:167`、`_visit:224`、`take_pending:285`、`mark_rendered:350` |
| `servers/rendering/renderer_scene_cull_rvt.cpp` | RS 接口实现、视点收集、每页剔除与调度 | `_register_viewer:164`、`_render_page:237`、`update_objects:302`、`render:319` |
| `servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp` | 池 / 页表纹理 / scratch FB / compose | `create:78`、`update_page_table:210`、`render_page:220` |
| `servers/rendering/renderer_rd/shaders/runtime_virtual_texture_compose.glsl` | 附件 → 池层的转换 | `main:51-75` |
| `scene/resources/runtime_virtual_texture.cpp` | 资源、uniform 绑定、失效 API | `_apply_to_material:170`、`apply_to_material:184` |
| `scene/3d/runtime_virtual_texture_volume.cpp` | 节点，把 `size` + 变换推给服务器 | `_update_world:45`、`set_size:82` |
| `editor/scene/3d/gizmos/runtime_virtual_texture_volume_gizmo_plugin.cpp` | 编辑器框线与缩放手柄 | `redraw` |
| `tests/servers/rendering/test_runtime_virtual_texture_page_manager.cpp` | 页管理 6 个单测 | `58-197` |
| `misc/rvt_demo/` | 自测工程、采样 include、接收 shader | `demo.gd`、`rvt.gdshaderinc` |

引擎侧接入点：`servers/rendering/rendering_server.h:793-805`、`rendering_server_default.h`（转发宏）、`servers/rendering/renderer_scene_cull.cpp:3354`、`servers/rendering/renderer_scene_cull.cpp:3906`、`servers/rendering/renderer_scene_cull.cpp:4434`、`servers/rendering/renderer_rd/renderer_scene_render_rd.cpp:1566-1588`（GPU 后端转发）、`scene/register_scene_types.cpp`（类注册）。文档：`doc/classes/RuntimeVirtualTexture.xml`、`RuntimeVirtualTextureVolume.xml`、`RuntimeVirtualTextureProxy.xml`。

---

*正文数值来源：`misc/rvt_demo` 自测输出与 `tests/servers/rendering/test_runtime_virtual_texture_page_manager.cpp`；代码基线 godot master `cafc17a192`。*
