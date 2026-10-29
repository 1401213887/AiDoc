# Godot 扩展方案调研：Clipmap 地形、四叉树 LOD、体素流送、分块加载、动作匹配、延迟光照

调研对象是 Godot 4 上已经有人做成扩展或引擎草稿的 3D 缺口方案。快照日期 2026-10-30。Terrain3D、HTerrain、VoxelViewer、OWDB、动作匹配的结论能回到下载到 `E:\AiTemp\godot-ext-survey` 的源码行。其余方案只读了仓库说明或 PR 正文，正文里单独标了证据等级。Godot 引擎侧对照的是本机 `F:\godot` 4.8.0 beta。

**目录**

1. Terrain3D 几何 Clipmap
2. HTerrain 四叉树分块
3. Voxel Tools 观察者驱动流送
4. Open World Database 按尺寸分块
5. 动作匹配特征库
6. Forward+ 延迟光照草稿
7. 其余扩展
8. 对照

## Terrain3D 几何 Clipmap

这一套把地形画成一套绕相机转的平坦网格，高度不在网格里，在顶点着色器里从高度图读出来。

它是 C++ GDExtension，官方编辑器不用重编。作者文档写网格生成一次，周期性把网格中心挪到相机，LOD 做在启动时生成的网格块里，不是相机往前走就销毁远处块、新建近处块。本次核对的是 GitHub `main` 上的网格器和着色器。

数据按区域存。一个 `Terrain3DRegion` 有三张图：高度 `FORMAT_RF`，控制 `FORMAT_RF`，颜色 `FORMAT_RGBA8`。控制图用 `floatBitsToUint` 解出洞。实例（植被）另存在区域的字典里，不进这三张图。

```
Terrain3DRegion
  height_map   RF
  control_map  RF     洞、自动纹理等控制位
  color_map    RGBA8  或压缩颜色图
  instances    Dictionary
```

网格块类型在 `Terrain3DMesher` 里是固定枚举：`TILE`、`EDGE_A`、`EDGE_B`、`FILL_A`、`FILL_B`，LOD0 用 `STANDARD_TRIM_A/B` 占据和 FILL 相同的槽。每一级 LOD 把这些块实例化进 `RenderingServer`，材质是同一份。

相机移动时 `snap()` 先把目标位置写进材质参数 `_target_pos`。位移小于 `vertex_spacing / 2^tessellation` 就只更新着色器参数，不挪实例。过了这个阈值，位置按 `vertex_spacing` 取整，再按 LOD 用 `snap_step = 2^(lod+1) * vertex_spacing` 对齐。实例缩放的 xz 是 `2^lod * vertex_spacing`，y 保持 1。对齐后的变换用 `instance_set_transform` 写回去，并 `instance_teleport`，避免物理插值把静态地面拖出残影。

顶点着色器拿到的是这块平坦网格的世界 xz。LOD 交界用 `vertex_lerp` 把顶点从细网格挪到粗网格的位置上，避免两级 LOD 对不齐。高度：当该块缩放小于 `_vertex_spacing`（LOD0 那档）时，对四邻域高度做双线性，权重是 `(1-fx)*fy`、`fx*fy`、`fx*(1-fy)`、`(1-fx)*(1-fy)`。更粗的 LOD 则在 morph 前的位置和 morph 后的位置各取一次高度，再按 `vertex_lerp` 混合。最后 `v_vertex.y = h`，再用视图矩阵写 `VERTEX`。洞把 `v_vertex.x` 写成 `0/0`，让图元丢掉。

碰撞不是这张 GPU 网格。文档写动态模式只在相机周围生成形状，完整模式为每个区域生成形状。导航不能走 `NavigationRegion3D` 自带的烘焙按钮，要用 Terrain3D 自己的烘焙器。光照贴图作者在议题里写明做不到：没有静态网格，也没有 UV2。

图 1 的数据：`snap_step = 2^(lod+1) * vertex_spacing`，`lod_scale.xz = 2^lod * vertex_spacing`，高度双线性四权重如上。源码：`terrain_3d_region.h:17-35`，`terrain_3d_mesher.h:14-24`，`terrain_3d_mesher.cpp:319-344`，`terrain_3d_mesher.cpp:397-400`，`terrain3d_main.glsl:161-177`，`terrain3d_main.glsl:195-212`，`terrain3d_main.glsl:231-251`。

## HTerrain 四叉树分块

这一套把高度图切成固定大小的块，近处拆成四个子块，远处合成一个粗块。网格仍然是平的，高度留给着色器。

实现全是 GDScript。作者在仓库说明里写自己只修 bug。默认块大小在 `hterrain.gd` 是 32。LOD 层数由 `compute_lod_count` 决定：分辨率不断右移，直到不大于块大小，次数就是最大深度，层数是深度加 1。例如分辨率 1024、块 32，右移 5 次，层数 6（深度 0 到 5）。

每帧 `update(view_pos)` 从最粗的根往下走。块的世界中心是 `chunk_size * (origin + 0.5)`，`chunk_size = base_size * 2^lod`。垂直范围回调会把中心的 y 设成该块高度范围的中点，避免高山或深谷只用 xz 距离误判。分裂距离是 `base_size * 2^lod * split_scale`。`split_scale` 钳在 2 到 5。视点比这个距离近，并且当前 lod 大于 0，就拆成四个子块，子块原点是父原点乘 2 再加 `(i&1, (i&2)>>1)`，每个子块做一个 `lod-1` 的块，父块回收。四个子块都没有自己的孩子，并且视点已经远于分裂距离，就回收四个子块，父块做一个当前 lod 的块。

网格在 `HTerrainMesher` 里按接缝掩码和 LOD 缓存。接缝是 4 位：左 1、右 2、下 4、上 8，一共 16 种。`make_flat_chunk` 的顶点是 `Vector3(x * stride, 0, y * stride)`，`stride = 1 << lod`。所以 LOD0 是每格 1 个单位，LOD1 是 2，LOD2 是 4。接缝通过改索引，让粗块边缘的三角形跨两格，去接细块。块自己是 `RenderingServer` 的 instance，Godot 4.4 起会关掉物理插值，否则新建的静态块会闪。

图 2 的数据：`split_distance = base_size * (1<<lod) * split_scale`，`split_scale` 范围 2 到 5，默认 `chunk_size` 32，顶点 Y 恒 0，`stride = 1<<lod`。源码：`hterrain.gd:215`，`quad_tree_lod.gd:50-61`，`quad_tree_lod.gd:71-77`，`quad_tree_lod.gd:97-143`，`hterrain_mesher.gd:4-8`，`hterrain_mesher.gd:39-54`，`hterrain_chunk.gd:32-33`。

## Voxel Tools 观察者驱动流送

这一套不是高度场。体素能有悬空、隧道和运行时挖改。表面用 Transvoxel 或方块网格化，数据按块进出内存。

`VoxelViewer` 是一个 `Node3D`，注释写明放在玩家相机下，用来触发周围体素加载，离观察者近的更新优先。默认视距 128（世界单位）。垂直视距是视距乘 `view_distance_vertical_ratio`，默认 1。三个开关分开：要不要可视、要不要碰撞、要不要数据块通知。还有 `network_peer_id`，给多人各自一个观察者。

可变 LOD 地形的 clipbox 流送入口是 `process_clipbox_streaming`。它收下观察者列表、体积变换，写出要存盘的数据块和要加载的数据块，并带 `can_load`、`can_mesh` 两个闸。本次只读了这个函数签名和 `voxel_viewer.h`，没有读 `.cpp` 里的块坐标计算，所以不写块边长和八叉树分裂公式。

仓库文档写过：流送时不能编辑还没加载的边界，用 `VoxelTool.is_area_editable()` 判断。GDExtension 移植议题里，作者在 2023-11 写扩展能在编辑器里跑，但测试不够，崩溃仍可能偏多，编进引擎的模块版更稳。2025-10 该跟踪议题仍开着。本次没有复测 2026 年的扩展构建。

源码：`voxel_viewer.h:10-12`，`voxel_viewer.h:57-64`，`voxel_clipbox.h:9-18`。

## Open World Database 按尺寸分块

这一套不生成地形。它把已经摆在场景里的节点，按尺寸档装进不同大小的格子，相机（以及别的位置点）靠近才加载。

`ChunkManager` 持有 `loaded_chunks`、`chunk_requirements`、`position_registry`。每个位置点调用 `_calculate_position_requirements`：尺寸档从大到小走。`ALWAYS_LOADED` 或超出 `_chunk_sizes` 的档，需求记在一个固定的常驻块坐标上。其余档用该尺寸算这个位置需要哪些块坐标，再 `_add_chunk_requirement`。位置不再需要的块进入 `pending_chunk_operations`，键是 `"unload"`，交给 `batch_processor` 分批做，避免一帧卸完。

主机模式自己管加载。`clear_autonomous_chunk_management` 用于 PEER，关掉本地自主装卸。另有 `enable_load_all_chunks_mode`，把世界上已有的块全部加载，再切回按位置加载时，会先记下之前加载的块，按新需求把多余的排队卸载。

作者在 README 里警告：加载卸载可能意外删节点，要备份。本次下载了 `chunk_manager.gd`，没有把批处理器和节点序列化逐函数读完。资产页写的引擎版本是 4.4，插件版本字符串 0.6h。

源码：`owdb_chunk_manager.gd:6-16`，`owdb_chunk_manager.gd:39-56`，`owdb_chunk_manager.gd:79-114`。

## 动作匹配特征库

这一套不播状态机。它把动画库烘成姿态数据库，运行时用角色当前轨迹和速度去库里找最接近的一帧，再把那一帧所在的动画和时间交回 `AnimationTree`。

`MMAnimationLibrary` 继承 `AnimationLibrary`。`bake_data` 吃角色、`AnimationMixer` 和 `Skeleton3D`。烘出来的库存是平行数组：`motion_data`（浮点特征）、`db_anim_index`、`db_time_index`、`db_pose_offset`，外加 KD 树的 `node_indices`。采样率字段默认 1。查询有两条路径：`_search_naive` 和 `_search_kd_tree`。代价按特征拆开，可选写进 `feature_costs`。

查询输入 `MMQueryInput` 带目标速度、当前朝向、控制器速度和变换、角色变换、未来轨迹、历史轨迹、骨骼状态。`is_valid()` 要求轨迹非空。输出是姿态下标、动画名、时间、总代价、匹配帧数据。

README 写集成在 Godot 4.4 的 `AnimationTree` 上，动画要有根运动，根骨骼在脚的高度。本次核对的是 `master` 的头文件，没有读 KD 树实现和特征分量的具体公式，所以不写特征维数。另一个仓库 Remi123/MotionMatching 面向 Godot 4.2，作者写明 `MMAnimationLibrary` 和 MotionFeatures 还不能用，只有惯性过渡播放器相对稳。那一份不应当动作匹配库用。

源码：`mm_animation_library.h:25-37`，`mm_animation_library.h:41-58`，`mm_query.h:16-43`。

## Forward+ 延迟光照草稿

这一套想在现有 Forward+ 里加一个开关：不透明物体不在几何着色器里算灯，改写成 GBuffer，再由 compute 后处理算光照。透明物体仍走原来的前向。

依据是 Godot PR 106230 的说明（BastiaanOlij，草稿，2025-05 开，2026-02 仍有更新）。说明里的 GBuffer 是反照率、位置、法线、ORM。环境和 GI 先写进颜色缓冲。光照 compute 读 GBuffer，假装还在场景片元着色器的后半段，套同一套光照代码。作者列出的未完成项：编辑器里采样器报错跑不起来；按是否开启延迟去裁着色器变体；把聚类数据交给光照 pass；层和实例索引还没存，于是除了 unshaded，所有灯照所有几何；点光和聚光的光照代码还没接上。说明里还写：用户 `light()` 会被忽略；MSAA 当时不能用，可能以后也不能；SSAO、SSR、SSIL 可能是坏的；雾是坏的；`shadow_to_opacity` 在非 unshaded 下不行；透射、rim、clearcoat 一类逐材质光照项列在不兼容清单里（说明在 “similar settings will” 处截断，后半句本次没看到）。

多视图说明写了能走，但聚类按眼睛分缓冲的旧办法在延迟路径上不行。本次没有检出 `deferred_renderer` 分支，通道布局没有本地行号。

官方架构文档写过以后可能会做聚类延迟。那是方向，不是这个 PR 已经合进 4.8。本机 `F:\godot` 的主帧颜色 pass 仍然在 `scene_forward_clustered.glsl` 里直接跑光照。

## 其余扩展

下面这些没有逐行下载实现。原理按仓库说明记，不补作者没写的公式。

**Chunx**（SlashScreen/chunx，Godot 4.2+）。给手工摆的开放世界做分块，不是程序生成世界。编辑器里直接放在 `WorldStreamer` 下的 `Node3D` 会被分进块，移动时重新分块。只有块节点的直接子节点会跨块重分，避免把一个 NPC 的子层级拆开。块存盘，编辑器相机和游戏主相机都会按半径加载。作者标了 alpha，已知问题包括磁盘文件刷新弹窗、块边界选择错乱、新建块报错。README 写可配置块尺寸和加载半径，并提到 HLOD，本次没看到 HLOD 的网格合并实现。

**godot4-auto-chunk**（DigitallyTailored/godot4-auto-chunk）。一个挂在节点上的工具脚本。`chunk_size` 定格，`chunk_range` 定相机周围加载几圈。脚本下的 `Node3D` 在编辑器里被存进对应块场景，空块不存。`chunk_size` 在已经有块之后不能改。没有网络同步，也没有按物体尺寸分档。

**Scatter Tool**（资产库，Godot 4.2，CC0）。把植被、带碰撞的物体和场景刷到关卡上，避免一个个从文件系统拖。本次没下载脚本，不知道它最终是生成 MultiMesh 还是生成一堆节点。

**Foliage3D**（caphindsight/Foliage3D）。绑死 Terrain3D。不手刷，按地形贴图名在格子上程序化生成。`FoliageLayer` 有间距和 `terrain_texture_names`。LOD 由作者手工配，近处 LOD0 可以异步换成带碰撞的场景实例，远处留 MultiMesh。作者写 GDScript 且多线程。

**Waterways**（Tshmofen/waterways-net）。Godot 4 / .NET 的贝塞尔河面。作者去掉了原版的流动烘焙和高度烘焙，流动顺着曲线走，河床高度用射线打碰撞。测试资源依赖 Zylann 的高度图插件。它不生成海洋。Terrain3D 1.1 预发布说明里另有一份海洋网格和默认着色器，那是 Terrain3D 自己的 extras，不是这个插件。

**动态阴影贴花**（dinazaber/Godot-Dynamic-Shadows-For-Static-Lightmaps）。给已经烘焙光照贴图的场景补动态物体的影子。两种：贴花圆影，和自上而下的正交视口阴影，轮廓来自视口纹理。作者写明只做俯视投影，投射者不能互相投影，剪影阴影会同时投到上下表面。这不是虚拟阴影贴图，也不是方向光阴影缓存。

**Nanite 名目的两个仓库。** AtlerGibby/GodotNanite 作者写明是网格分块流送的学习项目，不建议正式项目使用。限制包括所有物体共用一个材质、不支持光照贴图、闪烁、块生成耗时长。HammerVala/NaniteGodot 的说明书写了层次聚类和 GPU 剔除，本次没有核到可编译发布，不当成能用的实现。Juan Linietsky 的 GPU-driven 笔记（gist）写的是另一条：不透明进 GBuffer，阴影和 GI 用光追，不追求 Nanite 那种无网格层次 LOD。那是笔记，没有对应插件。

**运行时虚拟纹理。** 没有找到给任意网格用的 RVT 插件。引擎 `StreamedTexture2D` 是 mip 流送，文档标实验。体素示例 shader 引用过 `virtual_texturing.gdshaderinc`，那是体素地形贴图路径，本次没有读该 inc 文件。

## 对照

| 方案 | 运行时产物 | 谁决定细节 | 和引擎几何的关系 | 证据 |
|---|---|---|---|---|
| Terrain3D | 一套重复使用的 clipmap 实例 | 顶点着色器读区域高度图 | 网格在动，高度在着色器里。导航和碰撞另做 | 已下载源码 |
| HTerrain | 四叉树上的块实例 | 距离小于分裂距离就拆成四块 | 平网格，高度在着色器里。接缝在索引里 | 已下载源码 |
| Voxel Tools | 体素块网格 | VoxelViewer 视距，默认 128 | 网格由体素重算。能挖、能悬空 | 头文件，未读流送 cpp |
| OWDB | 场景节点的加载和卸载 | 位置点需要的尺寸档块 | 不改网格。可能误删节点 | 已下载 chunk_manager |
| Chunx / auto-chunk | 场景块 | 相机半径或圈数 | 不改网格。作者标不稳定或只是脚本 | 仅 README |
| 动作匹配 | 动画名和时间 | KD 树或线性扫描特征 | 接 AnimationTree。要根运动 | 已下载头文件 |
| 延迟 PR 106230 | GBuffer 加 compute 光照 | 不透明走延迟，透明走前向 | 要改引擎着色器。草稿未完成 | 仅 PR 说明 |

图 1、图 2、图 3 里的数值都已写进对应章节，没有只出现在图里的数字。

## 参考

- Terrain3D 架构说明：https://terrain3d.readthedocs.io/en/latest/docs/system_architecture.html
- Terrain3D 导航说明：https://terrain3d.readthedocs.io/en/latest/docs/navigation.html
- Terrain3D 光照贴图议题：https://github.com/TokisanGames/Terrain3D/issues/205
- HTerrain：https://github.com/Zylann/godot_heightmap_plugin
- Voxel Tools：https://github.com/Zylann/godot_voxel
- GDExtension 跟踪议题：https://github.com/Zylann/godot_voxel/issues/442
- Open World Database：https://github.com/DigitallyTailored/Godot-Open-World-Database
- Chunx：https://github.com/SlashScreen/chunx
- godot4-auto-chunk：https://github.com/DigitallyTailored/godot4-auto-chunk
- 动作匹配：https://github.com/GuilhermeGSousa/godot-motion-matching
- Remi123 MotionMatching：https://github.com/Remi123/MotionMatching
- 延迟草稿 PR：https://github.com/godotengine/godot/pull/106230
- GPU-driven 笔记：https://gist.github.com/reduz/c5769d0e705d8ab7ac187d63be0099b5
- 本地快照目录：`E:\AiTemp\godot-ext-survey`，对应 GitHub `main`/`master`，下载于 2026-10-30。
