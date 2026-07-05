# 移动端 AO（环境光遮蔽）实践方案技术报告

# 移动端 AO（环境光遮蔽）实践方案技术报告

从屏幕空间 AO 到烘焙 / Capsule / 几何代理——头部手游的真实落地策略与决策框架

**范围**SSAO / GTAO / HBAO · 烘焙 AO · Capsule AO · DFAO · Bent Normal
**案例**原神 · CODM · 三角洲 · FateTrigger · 和平精英 · 王者 · 崩铁 · 鸣潮 · 绝区零
**日期**2026-06-28

## 1. 为什么移动端 AO 是一道"取舍题"

环境光遮蔽（Ambient Occlusion）模拟物体凹角、接缝、接触面被环境光"挡住"而变暗的现象，是让画面产生**重量感与空间层次**的关键。但它在移动端的代价远高于 PC：

#### 三个核心成本

- **邻域采样 = 带宽炸弹**：屏幕空间 AO 本质是对深度/法线做多方向、多步长的邻域采样，跨像素跨 tile 随机访问。
- **破坏 TBDR 片上闭环**：邻域采样无法用 subpass `subpassLoad()` 实现，强制把深度 resolve 回主存，与移动端"省 Store"的根本诉求冲突。
- **全屏 Pass 叠加**：一次完整 SSAO 通常含"计算 + 模糊 + 上采样"3 个 Pass，每个都读写半屏 RT。

#### 由此产生的普遍取舍

调研的 9 款头部手游中，**低档位几乎全部关闭实时 SSAO**（原神、崩铁、鸣潮、绝区零、和平精英）。AO 在移动端不是"开/关"的二元选择，而是一条*从全实时 → 半分辨率实时 → 几何代理 → 全烘焙*的连续光谱，按机型档位与场景类型动态切换。

高档：半分辨率 GTAO/SSAO
中档：Capsule/DFAO 代理
低档：纯烘焙 / 直接关

## 2. AO 技术全谱系与移动端适配度

| 方案 | 原理 | 动态性 | 移动端成本 | 适配度 |
| --- | --- | --- | --- | --- |
| **SSAO** | 屏幕空间深度邻域比较 | 全动态 | 中（半分辨率可控） | 高档可选 |
| **GTAO** (Ground Truth AO) | 沿屏幕多方向做地平线搜索积分（Horizon Search），物理更准 | 全动态 | 中（UE 移动端默认实现） | 移动端首选实时方案 |
| **HBAO/HBAO+** | 水平基准 AO，质量高于 SSAO | 全动态 | 较高 | PC 为主，移动端罕见 |
| **Baked AO**（烘焙） | 离线烘焙进贴图通道 / Lightmap | 静态 | 近乎零（运行时仅采样） | 移动端基石 |
| **Capsule AO** | 用胶囊体代理角色肢体做解析遮蔽 | 动态（随骨骼） | 低（无邻域采样） | 角色自/互遮蔽最优解 |
| **DFAO**（距离场 AO） | 用全局距离场（SDF）查询遮蔽 | 动态 | 中（需 SDF 数据） | 大世界动态物体 |
| **Bent Normal** | 记录"未被遮蔽方向"的弯曲法线，做方向性 AO / 镜面 AO | 烘焙或实时 | 低（一张额外贴图/通道） | 质量增强项 |
| **Specular AO** | 由 Bake AO + Normal + EyeDir + Smoothness 计算的镜面遮蔽 | 静态派生 | 极低 | 廉价高回报 |

**关键认知：**移动端 AO 的最优解几乎从不是"单一方案"，而是 *烘焙打底（静态场景）+ Capsule/DFAO 补动态（角色与动态物）+ 高档位叠半分辨率 GTAO（细节）* 的**分层混合**。下面用四个真实案例拆解这套组合拳。

## 3. 案例一：原神 — Capsule AO + 半分辨率 Compute 优化

来源：米哈游技术总监公开分享（角色画面效果技术实现）

#### Capsule AO：角色遮蔽的解析解

- 用**胶囊体包裹角色四肢与躯干**，与骨骼动画同步更新。
- 遮蔽计算分两路：**无方向环境遮蔽** + **带方向遮蔽**。
- 带方向那一路的遮蔽方向，取**主光源方向与法线混合**得到的"虚拟遮蔽方向"——使角色能同时在周围墙面、地面投出多重接触阴影。
- 本质优势：**解析几何查询，无屏幕空间邻域采样**，天然规避 TBDR 带宽问题。

#### 屏幕空间 AO 的三段式 Compute 优化

- AO 全程在 **1/2 × 1/2 分辨率** RT 上计算。
- 为画面干净，做 **Bilateral（双边）模糊** + 上采样到全分辨率，避免 AO 渗透到无关区域。
- 模糊 + 上采样原本是 3 个 Pass（多次读写）→ 米哈游**合并进单个 Compute Pass**：用 **LDS 缓存中间值**、**一次输出 4 像素**复用相邻计算，最后用 **async compute** 进一步压开销。

**可复用结论：**半分辨率 + Bilateral + Compute 合并 + LDS 复用 + async compute，是把屏幕空间 AO"挤"进移动端预算的标准组合拳。角色遮蔽则尽量用 Capsule AO 而非屏幕空间。

## 4. 案例二：CODM（使命召唤手游）— 全烘焙 AO 工业化体系

来源：CODM / 光子 移动端技术分享

CODM 走的是**"能烘焙就不实时"**的极致工业化路线，把 AO 拆解成多个可烘焙/可廉价计算的子项：

| AO 子项 | 实现方式 | 成本 |
| --- | --- | --- |
| Diffuse AO（漫反射遮蔽） | 离线烘焙进贴图 | 运行时零 |
| Specular AO（镜面遮蔽） | Bake AO + Normal + EyeDir + Smoothness **实时计算** | 极低（几条 ALU） |
| 低配档位 | 直接用 **AO 替代 SO**（Specular Occlusion） | 省一步 |
| 小范围 AO | **法线梯度**实时生成 + 叠加 Lightmap AO | 低 |
| 植被 AO/法线 | **Houdini PCG 程序化生成**（2 周 → 2 天） | 离线，提产能 |

**贴图布局：**Metallic + AO 合并为 1 张图（与 Normal+Roughness 共图并列），3 张图工业化标准布局，省纹理内存与采样带宽。

**支撑这套体系的中台能力 = 自研 GPU 烘焙。**原 Unity Enlighten 复杂场景烘焙一次需 4–6 小时（通宵），改自研 GPU 烘焙后 **3–5 分钟**完成，迭代提速 100×。烘焙 AO 路线的可行性，本质上由烘焙工具链的迭代速度决定。

## 5. 案例三：三角洲行动 — GTAO + DFAO 双路并行

来源：三角洲移动端技术要点

三角洲是**前向渲染 + 极简后处理**的代表（仅 5 个 Pass，无 SSR/SSAO/Bloom/DOF），但 AO 反而保留并采用**双路方案**：

**G-Buffer**

→

**GTAO**
细节级屏幕空间遮蔽

+

**DFAO**
距离场，大尺度动态遮蔽

→

合成光照

- **静态场景**：全地图烘焙好的 AO + Shadow，写进虚拟材质（VT）的 Page 通道（8 通道布局含 AO）。
- **动态补充**：GTAO 抓近距离接触细节，DFAO 用全局距离场处理大尺度动态物体遮蔽，二者互补。
- **设计哲学**：在带宽约束下，宁可用更高质量的 Material/Lightmap 工作流"假装"后处理效果（贴花 AO、烘焙 Bloom），也不轻易开全屏后处理。

## 6. 案例四：FateTrigger — GTAO 单帧实测与 TBDR 硬限制

来源：本项目 RenderDoc 单帧截帧分析（FateTrigger\_单帧渲染分析与优化报告）

这是一份基于真实截帧反汇编的实测案例，揭示了移动端 GTAO 在 TBDR 上的**结构性约束**，价值极高：

#### 实测识别

- 截帧中无字面 "GTAO" marker，实为两个 Pass：`AmbientOcclusion_HorizonSearchIntegral`（507×252，**半分辨率**）+ `AmbientOcclusion_SpatialFilter`。
- 反汇编实锤：PS 仅采样 `SceneDepthZ`（不读几何/法线 GBuffer）+ 一张 16³ 噪声 LUT；常量缓冲含 `SinDeltaAngle / CosDeltaAngle / Thickness`（GTAO 地平线搜索标志参数）。
- 输出 `ScreenSpaceAO`(R8) 被 DeferredShading 在 slot 11 采样。

#### ★ 为何 GTAO 无法片上化

- Vulkan input attachment 的 `subpassLoad()` **没有 UV 参数，只能读当前 fragment 自己那个像素**——这是 TBDR 片上保留的前提。
- GTAO 本质是邻域采样：地平线搜索要沿屏幕多方向、在半径内步进采样周围像素深度——**跨像素跨 tile 随机访问**。
- 二者冲突 → GTAO **必须把深度作为普通 sampled texture 随机采样，强制深度 resolve 回主存**。即便塞进同一 RenderPass 也会有 depth Store。

**推论（适用于所有移动端项目）：**所有"邻域采样深度"的 Pass（GTAO / HZB / Occlusion / SSR / Bloom）**都无法被 subpass 吸收**，只能整体移到那次"反正要发生"的深度 Store 之后。代价是 **AO 晚一帧**。这是 R8 半分辨率之外，移动端 GTAO 必须接受的第二个代价。

## 7. 横向对比：9 款头部手游 AO 策略

| 游戏 | 主方案 | 高档 | 中档 | 低档 | 特色 |
| --- | --- | --- | --- | --- | --- |
| **原神** | Capsule AO + 半分辨率 SSAO | 开 | 开 | 关 | Bilateral+Compute+LDS+async 合并 |
| **CODM** | 全烘焙 AO + Specular AO 实时算 | 烘焙为主，分档调精度 | | | 低配 AO 替代 SO |
| **三角洲** | GTAO + DFAO + 烘焙 | 前向极简后处理，AO 保留双路 | | | VT 烘焙 AO 打底 |
| **FateTrigger** | 半分辨率 GTAO (R8) | 开 | — | — | 实测 TBDR 限制，AO 晚一帧 |
| **和平精英** | 烘焙为主 | HLOD 全烘焙 + Mask 通道塞 AO | | | 1GB RAM 设备可跑 |
| **王者荣耀** | 全烘焙 Lightmap | Mask 通道(B=AO) + 整图静态烘焙 | | | Half-PBR 4 项塞 1 通道图 |
| **崩坏星穹铁道** | 烘焙 Lightmap | — | — | 关 SSAO | 相机固定，剔除/LOD 预烘焙 |
| **鸣潮** | 自研轻量替代 | 可选 | 关 | 关 | 移除默认 SSAO Pass，高档按需插 |
| **绝区零** | 城区 SSAO / Halftone 替代 | 城区开 | Halftone | 关 | 卡通风用网点替代物理 AO |

**三条共性规律：**

1. **静态场景一律烘焙**——9 款无一例外把静态 AO 烘进 Lightmap 或贴图 Mask 通道（常见 B 通道或与 Metallic/Roughness 共图编码）。
2. **实时 AO 是高档特权**——中低档几乎全关，靠烘焙 + 几何代理兜底。
3. **卡通渲染另辟蹊径**——绝区零用 Halftone（网点）、原神内勾线用法线梯度，在不开 SSAO 时用风格化手段补空间感。

## 8. TBDR / 带宽视角下的 AO 铁律

移动端 GPU 普遍为 **TBDR（Tile-Based Deferred Rendering）**架构，省带宽的核心是"数据尽量留在片上 tile，少 Store 回主存"。AO 与这套机制的关系必须吃透：

- 1**邻域采样 = 不可片上化**。SSAO/GTAO/HBAO/Bloom/SSR 都需邻域采样，无法用 `subpassLoad()`（只能读当前像素），必须独立 Pass 或 Compute，并强制深度 resolve。
- 2**AO 强制 SceneDepth 落主存**。一旦开屏幕空间 AO，深度就必须以 sampled texture 形式可随机访问 → 与"深度全程 Memoryless"的省带宽目标直接冲突。
- 3**半分辨率是底线**。所有实测案例的屏幕空间 AO 都跑在 1/2×1/2（甚至可探索 1/4），R8 单通道输出，再 Bilateral 上采样。
- 4**After Opaque 更友好**。在 tiled GPU 上，把 AO 计算与应用放到不透明物体渲染之后，对效率更友好（URP 官方建议，虽理论上略不精确）。
- 5**能用几何代理就别用屏幕空间**。Capsule AO / DFAO 是解析查询，无邻域采样、不破坏片上闭环，是移动端最"TBDR 友好"的动态 AO。

## 9. 优化技法工具箱

#### 降成本

- **半/四分之一分辨率**：URP 默认仅降第一张中间纹理，可扩展为每张中间纹理独立降采样系数。
- **Compute 合并三段式**：计算+模糊+上采样合一，LDS 缓存、4 像素同输出复用、async compute（原神范式）。
- **R8 单通道**输出，紧凑格式不浪费。
- **数据型贴图升压缩档**：AO/Roughness/Mask 这类数据贴图可用 ASTC 8×8（比 6×6 省 ~44%），无需角色法线那种高精度。

#### 提质量 / 去瑕疵

- **Bilateral 双边滤波**：按深度差加权，防 AO 渗透 / 漏光。
- **法线权重衰减**：叠加表面法线点乘视角，抑制斜面错误遮蔽。
- **TAA 时间累积**：1–2 帧历史混合，降 85%+ 时序闪烁；注意运动矢量准确度与固定随机种子。
- **Bent Normal 向几何法线混合 15–30%**：有效减少异常漏光，同时保留遮蔽细节。
- **深度差阈值**过滤无效采样（0.01–0.03，依 Z 精度调）。

**动态分辨率适配：**GPU 负载监控下，当帧率持续低于目标（如 55 FPS 持续 2s），自动把 AO 渲染分辨率降到主屏 50% 并双线性上采样，实测提帧 12–18%，主观画质损失低于可察觉阈值。

## 10. 选型决策树与落地建议

#### 第一性原理决策顺序

场景是否静态？

→是

**烘焙 AO**
进 Lightmap / Mask 通道

→

否：是角色自/互遮蔽？

→是

**Capsule AO**
骨骼绑定胶囊体

→

否：是大尺度动态物遮蔽？

→是

**DFAO**
全局距离场

→

否：需近距离接触细节 & 高档机？

→是

**半分辨率 GTAO**
Compute 合并 + Bilateral

→

否 / 低档机

→

**关闭实时 AO**
仅烘焙 + 风格化补偿

#### 分档位推荐配置

| 档位 | 静态场景 | 角色 | 动态物 | 实时屏幕空间 AO |
| --- | --- | --- | --- | --- |
| 高档 | 烘焙 AO | Capsule AO | DFAO | 半分辨率 GTAO（Compute 合并） |
| 中档 | 烘焙 AO | Capsule AO | — | 关 / 1/4 分辨率可选 |
| 低档 | 烘焙 AO（Mask 通道） | Specular AO 派生 / 关 | — | 关，用法线梯度或 Halftone 补偿 |

#### 给本项目（UE Mobile）的 5 条落地建议

1. **静态 AO 全部烘焙**，进 Lightmap 或 GBufferC 的 AO 通道（UE Mobile `GBufferAO` 已在 RT3.a），运行时零成本。
2. **角色启用 Capsule AO/Shadow**（UE 原生 `CapsuleShadows` 支持），替代角色屏幕空间自遮蔽——TBDR 友好且无晚一帧问题。
3. **高档位才开 `r.Mobile.AmbientOcclusion=1`**，且确认 GTAO 跑在半分辨率 R8；接受"深度 resolve + AO 晚一帧"两项代价，不要试图把它塞进 BasePass subpass。
4. **把 GTAO 移到那次"反正要发生"的深度 Store 之后**（如 BasePass 末尾），避免为它单独多付一次 depth Store——这是 FateTrigger 实测的关键结论。
5. **建立 AO 分档开关 + 动态分辨率联动**，低于目标帧率时自动降 AO 分辨率，而非整帧降分辨率。

## 11. 参考来源

**本地知识库（E:\AiDoc）：**

- FateTrigger\_单帧渲染分析与优化报告.md — GTAO 截帧实测与 TBDR 限制（§4.4–4.5、4.9）
- 使命召唤手游移动端技术要点总结.html — 烘焙 AO / Specular AO / GPU 烘焙
- 三角洲移动端技术要点总结.html — GTAO + DFAO + VT 烘焙
- 原神 / 和平精英 / 王者荣耀 / 崩坏星穹铁道 / 鸣潮 / 绝区零 移动端技术要点总结.html
- FPS手游技术全景对比.html、UE\_Mobile\_Forward\_vs\_Deferred / TBDR 系列、Vulkan\_Subpass\_TBDR\_带宽优化\_学习指南.md

**网络来源：**

- 米哈游技术总监《原神》画面效果技术实现分享 — Capsule AO 与半分辨率 Compute 优化
- URP SSAO 优化实践（Downsample / After Opaque / Bilateral）
- AO / Bent Normal 工程实践与调优攻略（采样半径、法线权重、TAA 累积、漏光修正）

移动端 AO 实践方案技术报告 · 基于 E:\AiDoc 知识库 + 网络补充整理 · 2026-06-28
核心结论：烘焙打底 + 几何代理补动态 + 高档叠半分辨率 GTAO 的分层混合，是移动端 AO 的工程最优解。
