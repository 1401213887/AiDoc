# Godot Streaming Virtual Texture 实现：.svt 烘焙、异步流送与调试

在已有 RVT 之上增加「页从磁盘来」的来源：**离线烘焙 `.svt` 页文件 + 异步流送 + staging 区域上传**，与 RVT 共用页表、祖先回退与采样 include。1024² 源烘焙成 85 页：RGBA8 **6,289,740 B** / BC1 **787,180 B**（约 8×）；三套后端（Vulkan Forward+ / Mobile / D3D12）自测 PASS，近景与直贴图平均差 **0.0016**、远景 **0.0039**；BC1 页文件 **0.0043**；池缩到 3×3 **0.0046**；全量 doctest **1447 例 / 426,374 断言**通过。调试提供 5 种视图、强制 LOD、选页锁定（≤6 页），并给出与引擎 CVar 的对接方式。

## 1 结论与全貌

> SVT 只替换「页从哪来」：页表、祖先回退、采样 include 与 RVT 完全一致，所以它是在已有 RVT 上加的一条页来源，而不是第二套虚拟纹理。

Godot 没有 UE 那种 GPU feedback，也没有流式虚拟纹理。这套实现在已有的 RVT 之上增加了 **离线烘焙 + 磁盘流送**：把一张大图切成分层页文件（`.svt`），运行时按相机算出要哪几页，在 worker 线程读盘、在渲染线程把 tile 拷进物理池，页表照旧负责「缺页回退到祖先」。

**图 1**：SVT 全景：源图离线烘焙成页文件，运行时异步读页并上传到与 RVT 共用的页表/池。（图见 HTML 版）

demo 的关键数字（后文反复引用）：

| 参数 | 取值 | 含义 |
|---|---|---|
| 源图 | 1024 × 1024 RGBA8 | demo 程序化生成，带 mipmap 与缓存 PNG |
| 页数 | 8 × 8 起步，共 85 页 | `log2_tile_count = 3`，4 个 mip 层：64 + 16 + 4 + 1 |
| 页尺寸 | 128 texel + 4 边框 = 136 | BC 以 4×4 为块，136/4 = 34 块 |
| 页文件 | RGBA8 6,289,740 B / BC1 787,180 B | 48 B 头 + 1,052 B 索引 + 85 页数据 |
| 物理池 | 6 × 6 = 36 页（demo） | RGBA8 2.66 MB / BC1 0.33 MB（单层） |
| 每批加载 | ≤ 24 页 | `max_pages_per_frame`，读与上传各占一帧 |

### 三条设计取舍

**① 页内容离线烘焙、运行时只读。**

- 换来了什么：运行时零材质 pass、零 compose，读盘即可；页可以做 BC 压缩，磁盘与显存都小 8 倍（RGBA8 6.00 MB → BC1 0.75 MB）。
- 付出了什么：内容是静态的；改源图必须重新烘焙；页文件是新的资产格式，要自己管导入与版本。
- 什么时候失效：需要每帧变化的内容（动态贴花、角色足迹）只能用 RVT，不能烘。

**② 物理池用 BC 压缩，采样端统一「UNORM + 手动解码」。**

- 换来了什么：同页数下显存约为 RGBA8 的 1/4（BC7）到 1/8（BC1）；与 RVT 的池共用同一套采样公式。
- 付出了什么：BC1 无 alpha、块噪点；`TextureStorage` 只映射了 BC1_RGB，所以池格式只能取 `BC1_RGB_*`。
- 什么时候失效：需要精确法线/高度时 BC1 不够，要 BC5/BC7 或 R32F。

**③ 异步读盘，接受两帧延迟。**

- 换来了什么：读盘不阻塞渲染线程；单帧 GPU 工作只有「一页一次 `texture_update` + 一次 `texture_copy`」。
- 付出了什么：本帧提交的请求下一帧才可见；相机快速移动时新旧页会短暂共存。
- 什么时候失效：磁盘极慢或池远小于请求集时，会先淘汰、再回读，形成抖动。

## 2 .svt 文件格式与烘焙

> 格式只有三段：48 字节头、每层一段索引、按索引顺序排列的 tile 字节；读取端只做边界校验，不做解码之外的推断。

**图 2**：.svt 布局：48 字节头 + 每层索引（tiles_x / tiles_y / 每项 12 B）+ tile 数据。（图见 HTML 版）

头部字段（`servers/rendering/streaming_virtual_texture_file.cpp:205-215`）：

| 字段 | 字节 | 含义 |
|---|---|---|
| magic | 4 | `GSVT`（0x54565347） |
| version | 4 | 当前 1，不匹配直接 `ERR_FILE_UNRECOGNIZED` |
| flags | 4 | bit0 = sRGB 源；当前只用于记录，池格式由着色器契约决定 |
| format | 4 | 0 RGBA8 / 1 BC1 / 2 BC7 / 3 R32F |
| log2_tile_count | 4 | 层数 = 该值 + 1；每边页数 = 1 << 该值 |
| tile_size / tile_border | 8 | 页尺寸与边框，BC 格式要求都是 4 的倍数 |
| layer_count / level_count | 8 | 本实现恒为 1 层；level_count 必须等于 log2 + 1 |
| reserved | 12 | 保留 |

索引按层顺序排列：`u32 tiles_x`、`u32 tiles_y`，随后 `tiles_x × tiles_y` 个 `{u64 offset, u32 size}`（12 B）。读取端在 `open()` 里做三道校验（`servers/rendering/streaming_virtual_texture_file.cpp:102-128`）：每层页数与 `1 << (log2 - level)` 一致、每项 `size ≤ 一页字节数`、所有 `offset + size` 落在文件内。任何一条不满足返回 `ERR_FILE_CORRUPT`，而不是运行时读到垃圾。

一页的字节数只取决于格式与 `tile_size + 2 × border`（`servers/rendering/streaming_virtual_texture_file.cpp:46-60`）：

| 格式 | 一页（136²）字节 | 算式 |
|---|---|---|
| RGBA8 | 73,984 | 136 × 136 × 4 |
| BC1 | 9,248 | (136/4)² × 8 |
| BC7 | 18,496 | (136/4)² × 16 |
| R32F | 73,984 | 136 × 136 × 4 |

代入 demo：85 页 RGBA8 = 85 × 73,984 + 48 + 1,052 = **6,289,740 B**；BC1 = 85 × 9,248 + 1,100 = **787,180 B**。两个数字与 `ls -l` 一致，说明索引偏移没有算错。

### 烘焙：先拼边框，再压缩

`bake_image`（`servers/rendering/streaming_virtual_texture_file.cpp:174`）对每一层、每一页做：

1. 把源图缩放到该层尺寸（level 0 = 虚拟纹理边长，逐层减半）。
2. 逐像素从层像素取 `clamp(sx0 + tx, 0, size-1)`——**边框是复制边缘像素**，不是透明或重复相邻页。
3. 得到一个 136×136 的 tile 图；BC1/BC7 时对这一整张 tile 做块压缩（`compress_from_channels` / `compress`）。
4. 把字节写到 payload 末尾，并把 `offset/size` 记进内存里的索引，最后统一写回头部之后。

> **为什么不能压完 128 再拼 4 像素** BC 以 4×4 块为单位。先把 128×128 压成块、再把相邻页拼起来，页缝两侧的块各自由单边数据决定，双线性过滤跨页时会看到亮线或色块。必须在源图层面把边框像素拼好，整块 136×136 一起压。

内存上，烘焙按层拿一份源像素做 clamped 采样（`servers/rendering/streaming_virtual_texture_file.cpp:272-276`），峰值约为源图的 2 倍；虚拟边长超过 32768 直接拒绝，避免 1M² 图把进程打爆。

## 3 流送运行时：异步读与分区上传

> 读页在 worker，上传在渲染线程；一页两条 RD 命令，批大小与两帧延迟是全部节奏。

**图 3**：流送时序：渲染线程分槽并提交，worker 读 tile，下一帧回渲染线程上传并写页表。（图见 HTML 版）

为什么需要 staging：`RD::texture_update` 只能整层更新（`servers/rendering/rendering_device.h:473`），没有 buffer→texture 的区域拷贝；而 `texture_copy` 支持带偏移的 texture→texture 区域拷贝（`servers/rendering/rendering_device.h:486`）。所以先建一张 136×136 的 staging（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:151-157`），每页 `texture_update(staging)` 后 `texture_copy` 到池的 `(pool_x × 136, pool_y × 136)`（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:287-300`）。这样池保持 2D，采样 shader 一行都不用改。

池格式在创建时按文件头的 format 选（`servers/rendering/renderer_rd/runtime_virtual_texture_rd.cpp:92-105`）：

| format | 池 RD 格式 | 说明 |
|---|---|---|
| RGBA8 | R8G8B8A8_UNORM | 不用 SRGB 变体，见第 5 章契约 |
| BC1 | BC1_RGB_UNORM_BLOCK | `TextureStorage` 只映射 BC1_RGB，BC1_RGBA 会 publish 失败 |
| BC7 | BC7_UNORM_BLOCK | `Image::FORMAT_BPTC_RGBA` 映射 |
| R32F | R32F | 高度图，单层 |

调度侧（`servers/rendering/renderer_scene_cull_rvt.cpp:539-588`）：

1. 每帧先 `poll` 已完成结果，逐页 `get_slot` → `upload_page` → `mark_rendered`（`servers/rendering/renderer_scene_cull_rvt.cpp:542-564`）。
2. 只有 loader 空闲且没有待处理请求时，才 `take_pending(max_pages_per_frame)` 并提交新请求（`servers/rendering/renderer_scene_cull_rvt.cpp:566-580`）；否则先等待，避免把池塞满「正在读」的槽。
3. 每页记进 `stream_in_flight`，防止同一页在一批里重复请求；结果回来时清掉（`servers/rendering/renderer_scene_cull_rvt.cpp:549-558`、`servers/rendering/renderer_scene_cull_rvt.cpp:578`）。
4. 一批结束调用 `submit()`，由 `WorkerThreadPool::add_native_task` 起任务（`servers/rendering/streaming_virtual_texture_loader.cpp:76-86`）。

loader 只在 `configure()` 时解析一次文件头与索引（`servers/rendering/streaming_virtual_texture_loader.cpp:41-58`），worker 每个批次只按偏移读字节；早先每批重新解析整份索引，页数上千时是 O(index)/批的浪费，已修。

> **预算怎么算** 每页字节数固定：RGBA8 73,984 B、BC1 9,248 B。demo 每批 24 页 → 一批最多 1.78 MB（RGBA8）或 222 KB（BC1）；两帧延迟意味着吞吐约为「批大小 / 2 帧」。池 36 页、85 个 tile 时，请求集会被截断（`take_pending` 只保留前 36 个候选），所以远处页会等近处淘汰后才进来——这正是实测里「移动相机后淘汰 8–18 页」的来源。

## 4 一个页的一生（SVT）

> 状态机与 RVT 相同，唯一新增的是「读失败」分支：重试 3 次后拉黑，采样回退祖先。

**图 4**：SVT 页的六个状态与失败分支：加载中不写页表，失败后丢槽并拉黑。（图见 HTML 版）

1. **请求**：`request_page(level, x, y, priority)`，同页取更小的优先级值；被拉黑的页直接忽略（`servers/rendering/runtime_virtual_texture_page_manager.cpp` 中的 `blocked` 集合）。
2. **分槽**：空闲槽优先，否则 LRU；本帧被请求过的页不会被淘汰（`take_pending` 先过滤再截断，`servers/rendering/runtime_virtual_texture_page_manager.cpp:299`）。
3. **加载中**：槽已占用、状态为 STATE_RENDERING，**页表不动**——采样仍读祖先，不会出现空洞。
4. **完成**：`upload_page` 写入池后调用 `mark_rendered`（`servers/rendering/runtime_virtual_texture_page_manager.cpp:400`），写本级 entry 并刷新「比它粗」的后代。
5. **常驻**：`texelFetch` 命中，可被后续 LRU 淘汰。
6. **失败**：worker 读出空数据时累加失败计数（`servers/rendering/renderer_scene_cull_rvt.cpp:549-556`），≥3 次调用 `block_page`（`servers/rendering/runtime_virtual_texture_page_manager.cpp:394`）：丢弃页面、释放槽、加入黑名单，页表回退到祖先。

> **为什么失败要拉黑而不是无限重试** 读失败的页停在 RENDERING 状态：既不是常驻（不能被 LRU 淘汰），也不是「待渲染」（会一直留在请求集里）。不处理的话，一个坏 tile 会永久占住一个物理槽，并且每帧触发一次 IO。拉黑把它变成「这一页永远缺」，采样自动回退到更粗的祖先——画面更糊，但系统稳定。

## 5 采样契约：两条来源，一次解码

> RVT 和 SVT 都必须让池里放「sRGB 编码字节」，并在着色器里手动解码一次；这条契约是两者共用一份 include 的前提。

**图 5**：两条来源汇入同一个池：RVT 由 compose 编码，SVT 直接存源字节；采样端统一解码一次。（图见 HTML 版）

RVT 路径：材质 pass 输出线性 albedo，compose 用 `linear_to_srgb` 编码后写 UNORM 池。SVT 路径：源图本身就是 sRGB 编码字节，烘焙直接入池，不做颜色转换。采样端（`misc/rvt_demo/rvt.gdshaderinc:153`）用 `rvt_srgb_to_linear` 解码一次，再交给 `ALBEDO`。

> **踩过的坑：池不能绑 sRGB 视图** 最初 SVT 池用 `R8G8B8A8_SRGB` / `BC7_SRGB`，而 include 里又手动解码，结果整体偏暗、与直贴图差 0.19。原因是硬件采样时已经解码过一次。修复方式是流式池一律 UNORM，只由着色器解码一次；同一处还暴露了参考材质缺 `: source_color`，导致对比基准偏亮——两者修好后近景差降到 0.0016。

采样数学与 RVT 完全相同（页表间接 + 祖先回退 + 两层混合），这里只列与 SVT 相关的差别：

| 项 | RVT | SVT |
|---|---|---|
| 页内容来源 | 材质 pass + compose | `.svt` tile 字节 |
| 池格式 | RGBA8 / R32F（UNORM） | RGBA8 / BC1 / BC7 / R32F（UNORM） |
| 池层数 | 1 或 2（BC+NR 两层） | 1（页文件单层） |
| 页更新 | 视点 / 失效触发重渲染 | 只读；`invalidate` 等于重新读盘 |
| 调试视图 | 同样可用 | 同样可用（页表/池/边框是同一份数据） |

## 6 调试：五种视图、选页锁定与 CVar 等价方案

> 调试信息全部从「采样端实际读到的那份页表」算出来，所以看到的间接关系就是材质真正解析的结果。

`RuntimeVirtualTexture.debug_mode` 有 6 档（0 关闭）：

| 值 | 视图 | 看什么 |
|---|---|---|
| 1 | 页表 | 每个池 tile 一种颜色，越粗的回退越暗，无效页洋红，页边白线 |
| 2 | 逐层驻留 | 按实际命中的 level 上色，直接看出哪里在回退 |
| 3 | 物理池 | 池 tile 身份 + 绿色 tile 边框带 |
| 4 | LOD 热力图 | 蓝 = 最细，红 = 最粗 |
| 5 | 边框 | 红色页网格 + 绿色 tile 边框环 |

`debug_force_lod` 把采样层钉在指定值（-1 自动），用来肉眼比较「细页 vs 粗页」。选页调试三个 API：`pick_page(world_pos, level)` 返回 `Vector3i(level, x, y)`；`get_page_info()` 返回 resident/dirty/pool/`locked` 等；`lock_page(level, x, y, seconds)` 把页锁住（最多 6 页，按秒过期，期间每帧刷新、且不受 LRU 影响）。对应 UE 的 `r.VT.RVT.PickAtScreenCenter` + `MaxLockedPages` + `RefreshDuration`。

### Godot 没有 CVar，怎么获得同样手感

工作区里另有一套引擎级 CVar（并行实现，`core/config/cvars.h:46`，注册为 `Engine.get_singleton("CVars")`），支持 `--cvar <name>=<value>`、`--cvar-exec`、`--cvar-file` 与 `con.AutoConsole`（`main/main.cpp:628-630`、`scene/main/scene_tree.cpp:603`）。把 RVT 命令挂上去只需要几行：

```
var cv := Engine.get_singleton("CVars")
cv.register_variable("r.VT.RVT.DebugView", 0, "RVT debug view 0..5")
cv.set_changed_callback("r.VT.RVT.DebugView",
        func(_name, value): rvt.debug_mode = clampi(int(value), 0, 5))
cv.register_variable("r.VT.RVT.ForceLod", -1, "-1 auto")
cv.set_changed_callback("r.VT.RVT.ForceLod",
        func(_name, value): rvt.debug_force_lod = clampi(int(value), -1, rvt.log2_tile_count))
cv.register_command("r.VT.RVT.PickAtScreenCenter", func(args):
    _lock_page_under_center(float(args[0]) if args.size() > 0 else 5.0)
    return "picked", "seconds")
```

demo 里另有一份 `misc/rvt_demo/cvars.gd`（autoload `CVars`），提供三个输入源：F1 控制台、监视文件（`--cvar-file=<path>`，改文件或 adb push 后 0.25 s 内生效）、启动命令（`--cvar-exec="a 1;b 2"`）。命令名与 UE 一致：`r.VT.RVT.DebugView / ForceLod / PickAtScreenCenter / LockPage / PageInfo / UnlockAll / Stats`。它与引擎 CVar 参数同名，合并时二选一，别留两套（见第 10 章）。

> **调试视图为什么能代表真实采样** 颜色由 `misc/rvt_demo/rvt.gdshaderinc` 里的 `rvt_debug_color` 用同一份 `rvt_page_table` 和同一套 uv/LOD 算出（`misc/rvt_demo/rvt.gdshaderinc:62-124`），不是另画一层。所以页表里写错了间接关系，调试视图一定先暴露；反过来调试视图正确，不代表页内容正确，内容正确性靠「与直贴图逐像素对比」（第 8 章）。

## 7 代码审查：修掉的 7 个问题

> 这些是这轮专门做逻辑/效果/性能审查时找出来的，都有对应的单测或实测断言。

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 远页饿死 | `take_pending` 先按池容量截断请求，再跳过常驻页；常驻页占满名额后远页排不进来 | 先过滤「真正需要产生内容」的页再截断；所有请求页打 `last_used = frame` 防当帧淘汰 |
| 2 | 坏 tile 永久占槽 | 读失败页停在 RENDERING：不可淘汰、每帧重试 | 3 次失败后 `block_page`：丢槽 + 拉黑 + 页表回退祖先；单测覆盖 |
| 3 | 索引重复解析 | loader 每批 `open + 解析全索引`，页数大时 O(index)/批 | `configure()` 解析一次并缓存，worker 只按偏移读 tile |
| 4 | 烘焙峰值内存 3× 源图 | 每层 duplicate + 整层 padding 图 | 逐页从层像素做 clamped 采样，输出字节不变（6,289,740 / 787,180 一致） |
| 5 | 池可能超过 16384 纹理上限 | `pool_tiles × stride` 未校验 | 按 `16384 / stride` 钳制 |
| 6 | 页文件层数超管理器上限 | 文件头允许 12 层，管理器最多 10 层 | 超限拒绝并告警一次 |
| 7 | 烘焙虚拟尺寸无上限 | 1M×1M 会直接 OOM | 虚拟边长 > 32768 返回错误；同时清掉源 mipmap |

另外三个是 SVT 调试期发现的，已经写进代码注释与本文：sRGB 双重解码（第 5 章）、参考材质缺 `: source_color`、`Image::compress` 按 alpha 把 BC1 压成 BC3（改用 `compress_from_channels(COMPRESS_S3TC, USED_CHANNELS_RGB)`）。

## 8 验证矩阵与实测

> 所有数字都来自可复现的命令；退出码即结论，日志里 0 条 ERROR。

| 配置 | 结果 | 近景差 | 远景差 |
|---|---|---|---|
| Vulkan Forward+ | PASS | 0.0016 | 0.0039 |
| Vulkan Mobile | PASS | 0.0017 | 0.0035 |
| D3D12 | PASS | 0.0016 | 0.0039 |
| 运行时切 BC1 页文件 | PASS | 0.0043 | — |
| 池缩到 3×3（9 页，请求截断） | PASS | 0.0046 | — |

其它断言：

- 首帧只有常驻粗层 `levels=[0,0,4,1]`（5 页），第 11 帧收敛到 36 页。
- 相机移动后淘汰 8–18 页、累计上传 137–152 页（BC1 与 RGBA8 各跑一轮）。
- 调试页表视图改变画面 0.3451、强制最粗 LOD 改变 0.0760；选中的页 `resident=true / locked=true`，解锁后释放。
- RVT 回归：远景 0.0020 / 近景 0.0086。
- 全量 doctest（含并行 CVar 系统的测试）：1447 例 / 426,374 断言，0 失败。

```
# 构建（Git Bash 里必须先清 MSYSTEM）
set MSYSTEM= && set MINGW_PREFIX=
call "C:\Program Files\Microsoft Visual Studio\2022\Preview\VC\Auxiliary\Build\vcvars64.bat"
cd /d F:\godot && scons platform=windows target=editor arch=x86_64 -j32 tests=yes

# 单测
bin\godot.windows.editor.x86_64.console.exe --headless --test --test-case="*RVTPageManager*,*SVTPageFile*"

# SVT demo（三后端）
bin\godot.windows.editor.x86_64.console.exe --path misc/svt_demo -- --svt-test
bin\godot.windows.editor.x86_64.console.exe --path misc/svt_demo --rendering-method mobile -- --svt-test
bin\godot.windows.editor.x86_64.console.exe --path misc/svt_demo --rendering-driver d3d12 -- --svt-test
```

截图：`misc/svt_demo/out/` 下有 `ref_close.png`（直贴图基准）、`svt_close.png`、`svt_far.png`、`svt_bc1.png`；调试视图截图在 `misc/rvt_demo/out/debug_page_table.png / debug_pool.png / debug_borders.png`。

## 9 与 UE 的对照

> UE 的 SVT 是引擎级资产管线，我们做的是「够用的最小闭环」；页表、池、回退这些骨架是一致的。

| 项 | UE（Streaming Virtual Texture） | Godot（本实现） |
|---|---|---|
| 资产形式 | UStreamableRenderAsset / VT 资产，编辑器导入 | `.svt` 文件 + `bake_page_file()` 显式烘焙 |
| 页压缩 | BC/ASTC，按平台 | BC1 / BC7（无 ASTC） |
| 页表 | 引擎 VT 系统统一管理，编码池坐标与层 | 显式页表纹理，祖先回退靠 mip |
| 流送 | 引擎 VT 系统 + 磁盘/包体 | WorkerThreadPool 读盘 + staging 区域拷贝 |
| 调试 | r.VT.RVT.DebugView / PickAtScreenCenter / ForceLod | debug_mode / pick+lock_page / debug_force_lod |
| 材质取用 | VT 采样节点 | gdshaderinc + 6 组 uniform |

## 10 已知限制与下一步

> 能用的闭环已经跑通；剩下的都是“多快、多省、多通用”的问题。

- 无编辑器导入器：`.svt` 仍是运行时文件，烘焙靠显式调用（demo 首次运行自动生成并缓存）。
- 无 ASTC：移动端 BC 不一定被硬件支持，要按平台选格式或改 tile 尺寸。
- 每页一次 staging 拷贝：可合并成大 staging + 多次 `texture_copy`，减少 RD 命令数。
- 两帧延迟固定：可改成「同帧提交、下帧上传」，但需要双缓冲结果队列。
- 与并行 CVar 系统尚未合并：demo 的 `misc/rvt_demo/cvars.gd` 与引擎 `--cvar-file/--cvar-exec` 同名，必须二选一。
- 换页文件/改池大小会重置页管理器（统计归零、全量重载）。

下一步优先级：① 合并 CVar（删 demo autoload，把 `r.VT.RVT.*` 挂到引擎 CVars）→ ② `.svt` 导入器 → ③ 批量上传 → ④ ASTC/平台格式 → ⑤ 全局调试开关。交接细节见仓库根 `HANDOFF-RVT-SVT.md`；RVT 本身的完整设计（页渲染、compose、视点请求）见 `E:\AiDoc\Godot引擎-RuntimeVirtualTexture实现-页表物理池-视点驱动.html`。

---

*数值来源：`misc/svt_demo` 自测输出、`tests/servers/rendering/test_streaming_virtual_texture_file.cpp`、`tests/servers/rendering/test_runtime_virtual_texture_page_manager.cpp`；代码基线 godot master `cafc17a192`。*
