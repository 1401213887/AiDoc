# FateTrigger 单帧渲染分析与优化报告

> 分析对象：FateTrigger（64 位 Development 包，`SF_VULKAN_ES31_ANDROID`，PID25655）
> 抓帧：Frame 7779，RenderDoc Vulkan 截帧
> 渲染分辨率：1014×504，上采样输出 1448×720
> 分析工具：RenderDoc（API/管线/反汇编/GPU 计时）+ 真机 Snapdragon Profiler（带宽/cache 实测）
> 引擎：UE5 移动端渲染器（含 `MMH` 自研封装层）

---

## 目录

1. [当前的渲染管线流程](#1-当前的渲染管线流程)
2. [当前存在的性能热点](#2-当前存在的性能热点)
3. [将来可能的优化方向](#3-将来可能的优化方向)
4. [细节补充](#4-细节补充)
5. [角色渲染专章](#5-角色渲染专章bp_gamecharacter_c_2147465690)

---

## 1. 当前的渲染管线流程

### 1.1 帧总览

| 指标 | 数值 |
|---|---|
| 图形 API | Vulkan（Android ES3.1 feature level） |
| 总动作数 | 2077 |
| **DrawCall** | **600**（597 `vkCmdDrawIndexed` + 2 `vkCmdDrawIndexedIndirect` + 1 `vkCmdDraw`） |
| Dispatch（Compute） | 27 |
| Copy | 65 |
| 纹理 / Buffer | 540 / 495 |
| 渲染框架 | RDG（`FRDGBuilder::Execute`），跨多个 CommandBuffer 提交 |

管线性质：**UE5 移动端 Forward+ 聚类光照 + Full Depth Prepass + 卡通渲染 + 自研动态 GI（LuxGI）** 的混合管线，并非传统 Deferred。

### 1.2 渲染流程骨架（按执行顺序）

| # | 阶段 | 关键内容 |
|---|---|---|
| 1 | 准备（Compute） | GPUScene 数据上传（ScatterUpload）、`ComputeLightGrid`（Forward+ 聚类光照剔除，CullLights 16×8×8）、SkyAtmosphere 各 LUT |
| 2 | 阴影（GPU-Driven） | VirtualShadowMap 页表更新（`RequestPageDynamic Num:40`）、InstanceCulling + `DrawIndexedIndirect`、角色 Atlas ShadowDepth |
| 3 | **FullDepthPrepass** | `MobileRenderPrePass`，约 204 个 DrawIndexed，`DS=Clear→Store` |
| 4 | 屏幕空间准备 | RenderVelocities（速度缓冲，**同时补写深度**）、RenderOcclusion、BuildHZB #1、**GTAO**（半分辨率 AO）、ShadowProjection |
| 5 | 卡通描边 | `Mobile_PreOutline_Pass` + `MobileToonOutlinePass` |
| 6 | **主着色** | `SceneColorRendering`：subpass0 `MobileBasePass`（约 222 Draw）→ subpass1（空，贴花槽位）→ subpass2 `DeferredShading` + `Translucency` |
| 7 | HZB #2 | `MobileHZBOcclusion`（遮挡测试 + VSM 页 feedback 回读） |
| 8 | 后处理 | Bloom 金字塔、EyeAdaptation(CS)、SunMerge、**TAA(High)**、Tonemap、NaiveUpscale 1014×504→1448×720 |
| 9 | HZB #3 | 帧末标准 BuildHZB，供下一帧 prev-frame 遮挡剔除 |
| 10 | UI 与呈现 | SlateUI ElementBatch（约 91 Draw）、DrawDebugCanvas、CopyImageToBackBuffer、Present |

### 1.3 DrawCall 按 Pass 分布（600 个，脚本逐个归类，零遗漏）

| Pass | Draw 数 | 占比 |
|---|---|---|
| **MobileBasePass 主着色** | **222** | **37.0%** |
| **FullDepthPrepass 深度预通道** | **204** | **34.0%** |
| SlateUI 界面 | 91 | 15.2% |
| RenderVelocities 速度缓冲 | 14 | 2.3% |
| PostProcessing 后处理 | 12 | 2.0% |
| ShadowDepths 阴影深度 | 10 | 1.7% |
| BuildHZB ×3（各 9） | 27 | 4.5% |
| Mobile_PreOutline | 7 | 1.2% |
| Translucency 半透明 | 5 | 0.8% |
| GTAO / ShadowProjection / DeferredShading / ToonOutline / Debug / Copy | 各 1–2 | ~1.5% |

> **关键观察**：FullDepthPrepass(204) + MobileBasePass(222) = **426，占全帧 71%**。主场景几何被几乎完整地提交了两遍。

### 1.4 TBDR 带宽行为（逐 RenderPass 的 Load/Store）

深度缓冲 1014×504 D24S8 ≈ 2MB/次。

| RenderPass | 深度 Load/Store |
|---|---|
| FullDepthPrepass | `DS=Clear → DS=Store` ▲ |
| RenderVelocities | `DS=Load ▼ → DS=Store` ▲（**Draw 带 depth_output，开启深度写，向 SceneDepthZ 补写深度**） |
| RenderOcclusion | `DS=Load ▼ → DS=Store` ▲ |
| GTAO / PreOutline / ShadowProj | `DS=Load ▼ → DS=Store` ▲ |
| SceneColorRendering / BasePass | `C=Clear, DS=Load ▼` → 含 2× `vkCmdNextSubpass` → `C=Store, DS=Store` ▲ |
| HZB / Bloom 金字塔 | 全部 `Don't Care` load ✓ |

**做得对的地方**：BasePass 用 Subpass（片上多阶段着色，`subpassLoad` 读 GBuffer）、HZB/Bloom 用 Don't Care、低分辨率渲染 + TAA 上采样、AO/ToonOutline 用 Clear 而非 Load。

---

## 2. 当前存在的性能热点

### 2.1 热点一：DeferredShading 巨型 uber PS（最严重）

**现象**：`DeferredShading` Pass 仅 1 个全屏 Draw（6 index），GPU 耗时 **1.43 ms**。这是全屏 51 万像素跑同一个巨型 Pixel Shader 的结果，纯像素瓶颈，与几何无关。

**这是一个"卡通 + 自研动态 GI(LuxGI) + Forward+ 聚类光照"三套模型合一的 uber shader**：

- PS 绑定 **20 张 SRV**：GBufferA/B/C + SceneDepth + Forward+ 光照网格（ForwardLocalLight / CulledLightGrid / CulledLightDataGrid）+ ScreenSpaceAO + ScreenSpaceShadowMask + ToonLightingRampArray + **整套 LuxGI 纹理 7 张**（含 3 张 3D 体纹理：IndirectionTex、Mip1SHTexture 40×40×96、IrradiancePage 1024² 等）+ PreIntegratedGF。
- 7 个常量缓冲：View(6.6KB) + ToonShading + LuxGIVolume(3KB) + MobileBasePass + MobileDirectionalLight。
- 7 个 `SubpassFetch` attachment（片上读 GBuffer，**这点是对的**）。

**反汇编静态统计**（SPIR-V，body 7013 行）：

| 指标 | 数值 |
|---|---|
| ALU（乘 1295 + 加 593 + 减 218 + 除 161 + Dot 244 + GLSL 内建 575） | **~4150 标量/像素** |
| 动态分支 `if(` / `else` / `while(` 循环 | 142 / 32 / 6 |
| 无分支三元 `Select(` | 42 |
| 纹理过滤采样 / texelFetch / subpassLoad | 29 / 57 / 4 |

**★ 真机 SDP 实测结论（权威，覆盖静态推断）**：

> 在低端机上，该 PS 的**主瓶颈是带宽**，而非 ALU：
> 1. **纹理采样带来大量带宽消耗**——静态只数到 29 次过滤采样，但 20 张纹理含 3D 体纹理与 1024² 大图，单次采样数据量大 + cache miss → 实际 DRAM 流量高。静态指令计数严重低估了带宽代价。
> 2. **严重的 Instruction Cache Miss**——7013 行的巨型 shader 指令 footprint 超出 I-cache，复杂控制流加剧取指 miss。
> 3. ALU 确实忙，但在低端机上是次要因素。

**方法论教训**：RenderDoc 静态指令计数无法反映真实带宽与 cache 行为，必须以真机 SDP/Streamline 计数器为准。（注：早期分析误判为"零动态分支、ALU-bound"，经复核反汇编为高级反编译视图，实际含 142 个 if + 6 个循环，已修正。）

### 2.2 热点二：FullDepthPrepass 在 TBDR 上的性价比存疑

- Prepass 占 204 个 Draw（34%），与 BasePass(222) 几乎等量，**主场景几何被完整重画两遍**，CPU 提交开销翻倍。
- 桌面 GPU 上 Depth Prepass 靠提前深度剔除 overdraw 来省 PixelShader，但**移动 TBDR 硬件自带 HSR(Apple)/LRZ(高通)/FPK 隐面剔除**，本就能在片上消除大部分 overdraw → Prepass 收益被硬件重叠掉一部分，而代价（204 Draw + 一次深度 Store ~2MB）是实打实的。

### 2.3 热点三：深度缓冲反复 ping-pong，破坏 TBDR 片上保留

- 深度在 **5–6 个独立 RenderPass 间反复 Store→Load**（Prepass / Velocity / Occlusion / GTAO / BasePass），每次约 2MB。
- 根因：这些 Pass 被拆成独立 RenderPass，且 Prepass/Velocity 与 BasePass **分属不同 CommandBuffer**（跨 `vkQueueSubmit` 边界强制 flush tile）。
- 估算仅深度 Load/Store 往返就 ~16–20MB/帧。

### 2.4 次要观察

- **SlateUI 91 个 Draw**，多为 6 顶点小批次，UI 未合批。
- **VSM 阴影**：页管理 Compute（Update/RequestPage/FeedbackSubmit）为每帧固定支出；物理页主存带宽与 TBDR 省带宽目标冲突。卡通渲染阴影精度需求低，VSM 可能"杀鸡用牛刀"。

---

## 3. 将来可能的优化方向

### 3.1 优先级 P0：拆解 DeferredShading uber shader（收益最大）

1. **拆 shader permutation / 引入真 `[branch]`**：让无 GI、无局部光的像素跳过大段计算。**关键在低端机上同时缩小单 shader 指令 footprint，缓解 I-Cache Miss**——一石二鸟。
2. **GI/光照半分辨率着色 + 双边上采样**：GI 本质低频，半分辨率视觉损失小，省约 75% 的着色与采样带宽，对低端机直接收益。
3. **削减 / 压缩纹理带宽**：3D 体纹理（LuxGI SH）和 1024² 大图是带宽大头——降分辨率、改省带宽格式、SH probe 数据压缩。
4. **合并 ALU 链**：244 个 Dot + 161 次除法 + FMax/FMix 链，卡通 ramp/clamp 可预计算进 LUT，用查表换算术。

### 3.2 优先级 P1：重估 FullDepthPrepass

- 做 A/B 实测：关掉 FullDepthPrepass，看 BasePass 在 TBDR 真机上是否真的变慢。若硬件 HSR/LRZ 已消除大部分 overdraw，关掉 Prepass 很可能**净赚**（省 204 Draw + 一次深度 Store）。

### 3.3 优先级 P2：减少深度 ping-pong（条件性）

目标：把"三者之间"的中途 Store 从 4 次降到 1 次（无法到 0）。需**同时满足 4 项改造**：

1. 把 Prepass + Velocity + BasePass 合并成一个 VkRenderPass 的多个 subpass，深度作共享 attachment，中途 StoreOp = `DONT_CARE`。**注意 Velocity 也向深度补写（见 4.9），合并时这部分补写的深度不能丢——需确认其下游（BasePass EQUAL 测试）依赖。**
2. **把"邻域采样深度"的消费者移出中间空档（最难，且有限制）**：
   - HZB 剔除 / RenderOcclusion → 改用上一帧 HZB（帧末 #3 已构建，天然支持 prev-frame 剔除）。
   - **GTAO 无法被 subpass 片上化**（见 4.4）——只能整体后移到 BasePass 末尾那次"反正要发生"的 Store 之后，代价是 AO 晚一帧。
3. 三者保持在同一 CommandBuffer（不跨 `vkQueueSubmit`）。
4. 深度 + GBuffer 总字节必须塞进目标机 tile 预算（否则缩 tile / 多 bin，省下的 Store 被分块开销吃回去）。

> **权衡提示**：该改造工程量大，且省下的深度带宽与 SDP 实测的真正大头（DeferredShading 纹理带宽 + I-Cache）相比可能是小数目。建议先用 SDP 量化深度 ping-pong 占总带宽比例，再决定是否投入。prev-frame 降级在快速镜头下有瞬时瑕疵风险。

### 3.4 优先级 P3：次要项

- SlateUI 小批次合批。
- 评估 VSM vs. 缓存式 CSM 在卡通画风下的性价比（A/B 对比页管理 Compute + 物理页带宽）。

---

## 4. 细节补充

### 4.1 HZB 三次构建——已坐实非冗余，不构成优化点

GPU 计时：三次 HZB build 合计 **0.166 ms**（单次 downsample 3–20 µs），可忽略。三次输入都是同一张 `SceneDepthZ (ResourceId::8912)`，但语义不同：

| HZB | 位置 | 用途 |
|---|---|---|
| #1 | Prepass 后 | 喂本帧 GTAO + BasePass 前 InstanceCulling |
| #2 `MobileHZBOcclusion` | BasePass 后 | 遮挡测试 + **VSM clipmap 页 feedback**（Compute CB 含 `Static_Clipmaps_*` / `OutFeedbackBuffer`，后接 CopyImageToBuffer 回读） |
| #3 | 帧末 | 纯净 BuildHZB（Compute 是 EyeAdaptation，无 VSM），本帧无消费者 → 供**下一帧** prev-frame 遮挡剔除 |

### 4.2 HZB 原理与"为什么是 GPU 用"

- **HZB = 深度金字塔**：每级取 2×2 邻域的**最远深度**（`MobileHZBFurthest`，Reversed-Z 下取 min）下采样，512×256 起共 9 级 mip，每级 1 个全屏 Draw。取"最远"是保守剔除策略——宁可漏剔，绝不错剔。
- **现代 UE HZB 剔除是纯 GPU 闭环**：构建→Compute 采样剔除→写 Indirect Args→`DrawIndexedIndirect`，CPU 不参与裁剪决策、无回读 stall。与传统 Occlusion Query（GPU 算→回读 CPU→CPU 决策，有 stall）是两个时代。
- 本帧 HZB 消费者全是 `vkCmdDispatch`（InstanceCulling/VSM），印证纯 GPU。唯一回读是 VSM 页 feedback（页管理，非剔除决策）。

### 4.3 "HZB 纯 GPU 闭环为何能降 DrawCall"——区分两层含义

- **A 层 = CPU 提交的绘制命令次数**；**B 层 = GPU 实际画的工作量**。
- **HZB 直接降的是 B 层**（少画被遮挡的 instance），通过改写 Indirect Args 里的 `instanceCount` 实现（被剔的写成更小的数字）。
- **降 A 层（CPU DrawCall 次数）的是配套的 Indirect 合批**——把"按物体提交"变成"按批提交"，提交次数与物体总数解耦。
- 本帧实证：`vkCmdDrawIndexedIndirect(1) => <49338, 4>`，CPU 只提交 1 次，背后画 49338 索引 × 4 instance，那个 `4` 就是剔除 Compute 写进 Args 的数字。

### 4.4 ★ subpassLoad 的关键限制（决定 GTAO 无法片上化）

- Vulkan input attachment 的 `subpassLoad()` **没有 UV 参数，只能读当前 fragment 自己那个像素（framebuffer-local）**。这是 TBDR 片上保留成立的前提。
- **GTAO 本质是邻域采样**：`HorizonSearchIntegral` 要沿屏幕空间多方向、在搜索半径内步进采样周围像素的深度——跨像素、跨 tile 的随机访问。
- 二者冲突：**GTAO 无法用 subpass input attachment 实现，必须把深度作为普通 sampled texture 随机采样 → 强制深度 resolve 回主存**。即便塞进 SceneColorRendering 同一 RenderPass 也会有 depth Store。
- 推论：所有"邻域采样深度"的 Pass（GTAO/HZB/Occlusion）都无法被 subpass 吸收，只能整体移到那次"反正要发生"的 Store 之后。

### 4.5 GTAO 识别说明

- 截帧中无字面 "GTAO" marker，对应的是 `AmbientOcclusion_HorizonSearchIntegral`（507×252，半分辨率）+ `AmbientOcclusion_SpatialFilter` 两个 Pass。
- 反汇编实锤为屏幕空间 AO 且为 GTAO：PS 仅采样 `SceneDepthZ`（不读几何/法线 GBuffer）+ 一张 16³ 噪声 LUT；常量缓冲含 `SinDeltaAngle/CosDeltaAngle/Thickness`（GTAO 地平线搜索标志参数）。输出 `ScreenSpaceAO`(R8) 被 DeferredShading slot 11 采样。

### 4.6 SceneColorRendering 的空 subpass

- `vkCmdNextSubpass() => 1` / `=> 2` 中的 `=>N` 是"即将进入的目标 subpass 编号"。subpass0=BasePass、subpass1=空、subpass2=DeferredShading+Translucency。
- subpass1 之间无任何 Draw，因为 Vulkan RenderPass 是**静态预声明**的（创建时写死 3 个 subpass），运行时必须依次走完所有声明的 subpass，不能跳过。subpass1 应为**延迟贴花（Deferred Decals）槽位**，本帧场景无贴花，故为空。空 subpass 切换几乎零成本，非 bug、非冗余。

### 4.7 VSM 阴影（混合方案）

- VSM 相关 Pass（`MMH` 自研封装，Non-Nanite 路径）：`VirtualShadowMapArray::Update`（RequestPageDynamic Num:40，仅请求动态页，静态页走缓存）、`MMHRenderShadowDepths`（InstanceCulling + DrawIndexedIndirect）、`FeedbackSubmit`(1087×540) + `FeedbackReadback`（延迟回读避免 stall）、`ShadowMapProjection`。
- 混合：同时存在传统 `Atlas0 1024×1024` PerObject 角色 ShadowDepth + `ScreenSpaceShadowMaskTextureMobile` 屏幕空间遮罩。
- **移动端用 VSM 可接受的三前提（本帧均满足）**：① 页缓存（Num:40 只重画动态页）② 屏幕空间遮罩 resolve（BasePass 只采样规整遮罩，规避 VSM 物理页随机访问）③ 反馈延迟回读。

### 4.8 Tile 内存溢出行为（GBuffer 超 GMem 时）

| 厂商 | 行为 |
|---|---|
| Mali (ARM) | 缩小 tile 尺寸（16×16→16×8→8×8），数据仍在片上，间接开销变大，非带宽爆炸 |
| Adreno (Qualcomm) | 增加 bin；最坏单 bin 仍超 → 退回 **Direct/Flat 渲染（GMEM bypass）= 真正的带宽灾难** |
| Apple / PowerVR | PSO 创建时校验 imageblock 预算，超了直接拒绝创建，不允许运行时降级 |

- **RenderDoc 看不出 tile 溢出**：它是 API 层工具，tile 尺寸/bin 数/GMEM 占用/是否降级全在 API 之下，溢出与否抓到的 trace 一模一样。
- RenderDoc 只能"预测风险"（数 attachment × 每像素字节，对照 tile 预算手算）；真观测需 Mali Offline Compiler / Streamline / Snapdragon Profiler / PVRTune。

### 4.9 深度的生产者：Prepass + RenderVelocities 共同写深度

- 全帧唯一深度 `SceneDepthZ`（D24S8）的生产者**不止 FullDepthPrepass**：RenderVelocities 的 Draw 经 `get_draw_call_details` 查实带 `depth_output = ResourceId::8912`，且 RenderPass 为 `DS=Load → DS=Store`——即它**载入 Prepass 的深度、开启深度写、向同一张深度补写一部分**（推测为 Prepass 阶段未写入但需正确运动矢量的物体，保证 BasePass EQUAL 测试一致）。
- 修正：早期表述"深度仅由 FullDepthPrepass 生成"不准确。准确说法是 **Prepass 生成主体深度，RenderVelocities 补写一部分**。
- **深度消费者全清单**（9 个）：RenderVelocities（DS=Load 测试+补写）、BuildHZB #1、GTAO、RenderOcclusion、MobileBasePass（DS=Load + EQUAL）、DeferredShading（SRV + SubpassFetch）、MobileHZBOcclusion #2、TAA（重投影）、BuildHZB #3（喂下一帧）。
- **Velocity 产出 `SceneVelocity`（R16G16）的消费者**：本帧仅 TAA（e4105 slot 2）。两者共同终点是 TAA。
- 合并影响：Velocity 既读又写深度，与 Prepass 同属"写深度 + 写各自 RT"的同类操作，合并进 Prepass（velocity 作额外 MRT）顺理成章；但合并/改 StoreOp 时必须保留 Velocity 补写的深度。
- **Velocity 能否被深度替代**：静态物体的运动矢量可用 `深度 + ClipToPrevClip` 重投影在 TAA 内现算（camera-only velocity），无需画；动态物体（含自运动）必须由 VS 用 prev-frame 世界矩阵写出，无法用深度反算。结论：RenderVelocities 不能完全砍，但可瘦身到"仅动态物体"。

---

## 5. 角色渲染专章（BP_GameCharacter_C_2147465690）

### 5.1 角色经过的 Pass（7 阶段）

角色是 **GPU 蒙皮骨骼网格**——顶点工厂 `GPUSkinVFBase`（常量缓冲含 `BoneMatrices`、`NumBoneInfluencesParam`），骨骼矩阵上传 GPU、蒙皮在 VS 完成，CPU 不参与。

| # | Pass | 角色在此做什么 |
|---|---|---|
| ① | ShadowDepth（e740, Atlas0 1024²） | 渲入阴影图，VS=GPUSkin+MobileShadowDepthPass，PS 极简（ASTC alpha-test） |
| ② | FullDepthPrepass | 身体写入场景深度 |
| ③ | RenderVelocities（e2046） | VS 用 Prev 矩阵算运动矢量 + 补写深度 |
| ④ | Mobile_PreOutline_Pass（e2350） | **反向外扩描边**：VS 沿法线外扩 `ToonOutlineWidth` + `OutlineDistanceWeightFallOffTexture` 距离权重 → 写 `MobileToonOutline` RT |
| ⑤ | ShadowProjection（e2301） | 角色屏幕空间阴影遮罩 → `ScreenSpaceShadowMaskTextureMobile` |
| ⑥ | MobileBasePass（e3526–3573，主体 e3567 idx=88110） | 主着色 Toon PS，**写 4 个 MRT = GBuffer** |
| ⑦ | DeferredShading（全屏） | uber PS 对角色 GBuffer 统一打光 |

### 5.2 关键架构：角色走"写 GBuffer 的混合延迟路径"

角色 BasePass Toon PS（e3567, `ResourceId::408747`，SPIR-V id bound 1212）反汇编显示**输出 4 个 MRT**（`SV_Target0~3`）：
- `SV_Target0` = 带光照颜色（× PreExposure）
- `SV_Target1~3` = 打包的法线 / PBR 参数 / **ShadingModelID（写入 11）**

> **结论**：整条管线入口虽是 Forward+，但角色在 BasePass 把材质属性写进 GBuffer，真正统一打光交给全屏 DeferredShading uber PS（第 2.1 节那个 1.43ms 的）。**角色着色成本分两段计费：① per-pixel BasePass Toon PS；② 全屏 DeferredShading 中角色覆盖像素部分。** 这也解释了 DeferredShading 为何绑 GBufferA/B/C——输入正是角色这里写出的。

### 5.3 Toon PS 逐段解读（6 块）

1. **法线装配**：采样切线空间法线贴图（Texture2D_0）→ 解包 → TBN 矩阵（`_320`）转世界法线，含 detail normal 混合。
2. **卡通明暗（核心）**：`NdotL`（`_1028`）→ **SmoothStep + ToonShadowColor 硬边二分阴影**（亮/暗卡成两段，非连续渐变），配 ToonShading CB 的 PreExposure 系列做曝光归一化。
3. **色域 + 色相偏移（ALU 大头）**：硬编码 8 个 3×3 矩阵（`_229`~`_255`，sRGB↔AP1↔AP0 + RRT 的 ACES 色彩管理）→ 算 hue 角度（`Atan2` `_641`）→ ±180° 色相旋转 + 按色相微调饱和/亮度。这是卡通材质的"固有色艺术化处理"。
4. **方向光阴影**：采样 `ScreenSpaceShadowMaskTextureMobile`（`_997`）× `DirectionalLightShadowMapChannelMask` 通道点积（`_1014`）取本角色阴影通道。
5. **RimLight 边缘光**：`ViewDirection · LightDir` 经 SmoothStep（`_1042`）算轮廓补光。
6. **描边合成 + GBuffer 打包**：采样 `ScreenOutlineTexture`（PreOutline 阶段产出）→ Step 判定描边区 → 压黑（`_1069`）；末尾大量 `RoundEven` + 位移/或运算把 metallic/roughness/AO/specular/ShadingModelID 量化打进 8bit MRT。

材质纹理：5 张 ASTC 压缩纹理（基础色 SRGB / 法线 / PBR 遮罩 / Texture2DArray 等），mip bias 采样。

### 5.4 角色相关优化提示

- **ACES 色域矩阵 + 色相偏移的逐像素计算是 BasePass Toon PS 的 ALU 大头**，但其结果仅取决于固有色和少数材质参数 → **可预计算进 1D/2D LUT**（或材质烘焙阶段处理），用一次查表换掉十几个矩阵乘 + Atan2 + Pow。角色全屏占比高，收益明显。与第 3.1 节 DeferredShading 的"ramp 进 LUT"同思路。

---

*报告基于 RenderDoc 单帧静态分析 + 真机 SDP 实测交叉验证。RenderDoc 用于厘清"管线结构是否正确"，真实带宽/cache/tile 行为以真机厂商 profiler 为准。*
