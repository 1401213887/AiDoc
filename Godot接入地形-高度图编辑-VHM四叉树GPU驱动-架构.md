# Godot 接入可编辑地形与四叉树 GPU 驱动渲染：架构

对照本机 `D:\GR_DevTest\UE5EA`（含 GR 分叉标记）和 `F:\godot` 4.8.0 beta。结论：编辑层和运行时网格必须拆开。虚幻 Landscape 的组件网格不要搬。运行时借 Virtual Heightfield Mesh 的计算着色器四叉树，高度先采样普通纹理，不依赖运行时虚拟纹理。这件事只在大范围室外高度场上值得做。小场景继续用 Terrain3D 的 clipmap。

## 问题

要的是两件东西：可编辑的高度场，以及基于四叉树的 GPU 驱动运行时网格。成功标准是笔刷改的高度和 GPU 画出来的是同一份数据，碰撞和导航不依赖那张随相机变化的网格。

## 假设清单

- 接入虚幻地形等于把 Landscape 组件、小节、编辑层搬进 Godot。——【假设】。那些结构服务于 CPU 侧按组件做 LOD 和权重图。VHM 运行时不画那些组件网格。
- Godot 已有 Terrain3D，所以四叉树 GPU 驱动没有必要。——【假设】。clipmap 是环带 LOD，平坦区域也会保持环上的密度。四叉树按距离和高度范围决定拆不拆。两者解决的三角形分布不一样。
- 运行时虚拟纹理是 VHM 的前置条件。——【假设】。本分叉的顶点着色器确实从虚拟纹理物理页采样高度。Godot 没有这项能力。高度图本身可以是一张普通纹理。

## 不变量

- 视点相关的 LOD 网格不能当碰撞体。相机一动，三角形集合就变。碰撞必须读高度图，不能读当前画出的网格。
- 四叉树拆分要看一块地的高度范围，否则每个节点都得读全分辨率高度。这份范围必须在编辑时算好，不能在展开四叉树时现算整张图。
- GPU 驱动意味着实例列表由计算着色器写入，绘制用间接参数。读回 CPU 再提交，就不再是 GPU 驱动。
- Godot 默认渲染和游戏在同一条主线程。计算着色器必须排在这条渲染线程的不透明绘制之前。

## 虚幻编辑侧实际在写什么

`ALandscapeProxy` 上的地形被切成组件。`ComponentSizeQuads` 是每个组件的四边形数量。`SubsectionSizeQuads + 1` 必须是 2 的幂。`NumSubsections` 是一个组件里小节的个数。高度导入数据是 `uint16`。

笔刷和导入最终进 `FLandscapeEditDataInterface::SetHeightData`。它按 `ComponentSizeQuads` 把一块矩形拆到各个组件上，可选重算法线、更新包围盒和碰撞、生成 mip。高度落在组件共享的高度图纹理里，组件用 `HeightmapScaleBias` 记录自己在大图中的窗口。

这套组件划分是编辑和旧版组件渲染的索引方式。接到 Godot 时要留下的是：高度是纹理、笔刷写一个矩形、写完要更新派生数据。组件网格、权重图层、样条编辑层不在第一版。

## 虚幻 VHM 运行时实际在算什么

`FVirtualHeightfieldMeshSceneProxy` 从组件拿走 MinMax 纹理、LOD 偏差纹理，以及虚拟纹理变换。它把自己标成静态表面（`bHasDeformableMesh = false`）。材质要带 `MATUSAGE_VirtualHeightfieldMesh`。

展开在计算着色器里。对一个四叉树节点，采样 `HeightMinMaxTexture` 得到这块的高度最小和最大，用这两个高度和 UV 角点做视锥测试。没被剔除且 `Level > 0` 时，若 `MinDistanceLod - MinMaxLodBias.y < Level` 就拆成四个子节点，用 `InterlockedAdd` 推进队列。否则写入实例缓冲，并递增间接参数。

顶点工厂没有顶点缓冲。输入是 `SV_InstanceID` 和 `SV_VertexID`。网格边数是 `NumInstanceVertexSide + 1`。高度从 `HeightTexture` 采样。本分叉在非移动路径上对虚拟纹理页表做两次查找再对高度插值。移动路径是一次页表查找。Godot 侧第一版不要抄这条虚拟纹理采样，改为对一张普通高度纹理 `texelFetch`。

间接绘制 Godot 已经有：`RenderingDevice::draw_list_draw_indirect`。Forward+ 的 MultiMesh 已经在用它。

## Godot 上的架构

三层，只共享一份高度资源。

1. `TerrainHeightData`（Resource）。一张高度图（`FORMAT_RF` 或 16 位），一张可选的控制图（洞、材质索引）。编辑器在脏矩形上重建 MinMax mip：每一级纹素是下一级 2×2 的最小和最大高度。区域可以按块存，避免一张纹理超过设备上限。不要先做虚幻的组件网格。

2. 编辑器插件（`TOOLS_ENABLED`）。笔刷在 CPU 上改高度图的一个矩形，然后只重建这个矩形盖到的 MinMax mip。碰撞用 `HeightMapShape3D`，并且只给相机附近的块生成形状。导航从高度图提取三角形，按块提交。这两件事都不读 GPU 上那张随视点变化的网格。

3. 运行时模块，编进渲染器，不做成 GDExtension。原因是计算着色器要写实例缓冲和间接参数，并且这次绘制要进 Forward+ 的不透明列表才能吃到聚类光照。GDExtension 拿不到 MultiMesh 内部的 RenderingDevice 缓冲，也插不进 `RenderForwardClustered` 的绘制循环。

运行时在不透明绘制之前、渲染线程上做三步：

- 队列里放入根节点。
- 计算着色器循环：读 MinMax，做视锥和距离 LOD 测试，要么把四个子节点写回队列，要么追加一条实例（原点、LOD、在高度图上的 UV 窗口）。
- `draw_list_draw_indirect` 画一张单位网格。顶点着色器用 `SV_VertexID` 展开网格，用实例里的 UV 窗口采样高度图，写入顶点高度。

集成点抄 MultiMesh 的间接绘制，不抄 `MeshInstance3D`。`MeshInstance3D` 的顶点在 CPU 网格里，做不到视点驱动的拆分。

线程：默认 `thread_model` 是 Safe，渲染线程就是主线程。计算和绘制都排在这次 `flush` 里即可。不要依赖实验中的 Separate 渲染线程。

## 和现有插件的关系

Terrain3D 的 clipmap 是 GDExtension 能做完的：网格生成一次，CPU 改实例变换，高度在顶点着色器里读。它没有四叉树，也没有 MinMax 驱动的拆分。导航和碰撞是插件自己的第二份数据。光照贴图因为没有静态网格和 UV2 做不到。

HTerrain 有四叉树，但是 CPU 每帧拆合，然后换一块预先生成的平网格。那不是 GPU 驱动。

所以：只要地形范围和环带 LOD 够用，不要做这套，直接用 Terrain3D。要做的前提是高度场大到环带会在平地上浪费三角形，并且你需要和 Forward+ 光照在同一次绘制里完成。那时编辑数据可以仍是一张高度图，运行时换成上面的计算着色器四叉树。

## 第一版明确不做

- Landscape 的权重图、编辑层、草地、样条。
- 运行时虚拟纹理。本分叉用它采样高度，Godot 没有页表。高度超过一张纹理时，先用区域纹理数组。
- 把 VHM 网格当作物理网格。
- 用 GDExtension 在每帧把可见四边形读回 CPU 再上传。那是 HTerrain 的路线。

## 验证与待确认

- 已核对：`LandscapeProxy.h:935-938`，`LandscapeEditInterface.cpp:262-278`，`VirtualHeightfieldMeshSceneProxy.cpp:864-891`，`VirtualHeightfieldMesh.usf:247-294`，`VirtualHeightfieldMeshVertexFactory.ush:43-48` 与 `207-224`，`rendering_device.h:1571`，`render_forward_clustered.cpp:621`。
- 本分叉移动端高度采样有 `[GR Mobile]` 修改。文档里的虚拟纹理双次采样是 `#else` 分支，不是移动端路径。
- 待确认：Godot 纹理边长上限下，一张 `FORMAT_RF` 高度图能覆盖的世界尺寸。本次没有读设备限制常量。
- 待确认：计算着色器四叉树在 Godot 的 Forward+ 不透明 pass 前插入后，主线程（Safe 模式）增加多少毫秒。没有 trace。
