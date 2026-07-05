# FPS 手游移动端渲染技术全景对比

🧭 **知识库导航**：本页是单款案例的横向资料。其 TBDR 片上缓存优化的**方法论纵贯**见 [TBDR 跨平台完整技术方案 · §6/§8](./UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md)（分档决策与平台横向对比的方法论纵贯） | [📚 知识库总导航](./知识库导航_README.md)

# FPS 手游 · 移动端渲染技术全景对比

5 款代表作的引擎选型 · 渲染管线 · 资产策略 · 公开度横向对比

## 一、对比对象与公开度

三角洲行动

UE4 · 腾讯天美 J3

公开度 ★★★★★

GDC 2024 + GDC 2025 + UFSH 2023

和平精英

UE4 · 腾讯光子

公开度 ★★★

第三方截帧 + 媒体访谈

暗区突围

UE4 · 腾讯魔方

公开度 ★★★★★

GDC 2022 + 2023 + 2024 三场

使命召唤手游

Unity · 腾讯天美

公开度 ★★★★

郭智 GDC + 游戏学堂

CFM / Free Fire

Unity · 天美 / Garena

公开度 ★★

官方公告 + Unite 2019

### 📊 5 款游戏 6 维度雷达对比

FPS 手游技术能力雷达图（满分 5 ★）


渲染管线深度
PBR 深度
地形复杂度
低配适配
前沿技术
公开度


5★
4★
3★
2★
1★


三角洲

暗区突围

和平精英

使命召唤手游

CFM / Free Fire
三角洲与暗区突围在前沿技术 + 公开度上领先；和平精英 + Free Fire 在低配适配上领跑

### 📊 技术演进时间轴（2019-2026）

FPS 手游技术演进时间轴


2019

2020

2022

2023

2024

2025

2026


和平精英上线
8×8km 大世界首发


CODM 上线
PBR 工业化标杆


暗区突围 GDC 2022
主机级动态天气系统


三角洲 UFSH 2023
VT Combine Pass 首讲


暗区突围 GDC 2024 ⭐
144FPS 帧预测
移动端硬件光追


三角洲 GDC 2024
双管线 + 虚拟材质系统


三角洲 GDC 2025
Clipmap + 3D Decal

## 二、引擎与管线选择

| 游戏 | 引擎 | 渲染管线 | 世界规模 | 分级策略 |
| --- | --- | --- | --- | --- |
| **三角洲行动** | UE4 + 自研改造 | 前向 + 延迟 双管线 | 开放世界（多地图） | PC/旗舰=延迟，中端=前向，超低=退化路径 |
| **和平精英** | UE4 深度定制 | 前向 | 8 × 8 km | 主版 + Lite 版（独立分支，1GB RAM） |
| **暗区突围** | UE4 深度定制 | 前向 | 开放世界（多地图） | 同管线分级，高端开光追，旗舰开 144FPS |
| **使命召唤手游** | Unity 中台 | Forward Scalable 高配 HDR / 低配 OnePassHDR | 地图制（非开放世界） | 4 级 PBR Shader LOD（835 / 520 / 更低 / 兼容） |
| **CFM / Free Fire** | Unity | 前向 + PBR + Tonemap | CFM 6×6 km / FF 较小 | FF 定位低端市场（包体 ~400MB） |

观察

- **UE4 在大世界 FPS 上是绝对主流**（三角洲、和平精英、暗区突围全用 UE4），原因是地形、Streaming、HLOD 的工具链最成熟。
- **Unity 多用在地图制 FPS**（CODM、CFM、Free Fire），有强中台 + PBR 工业化优势。
- 除三角洲外，**都是 Forward 单管线**。延迟管线只在三角洲的高端档位出现，因为 FPS 户外光源单一，Forward 才是正解。

## 三、PBR 与材质策略

| 游戏 | PBR 策略 | 贴图布局 | 压缩思路 |
| --- | --- | --- | --- |
| **三角洲行动** | 跨平台虚拟材质系统 | BaseColor + Normal 4 通道（Mask + 法线 + Roughness） | Normal 通道复用 + VT Combine Pass（Subpass 重排） |
| **和平精英** | PBR-Lite 自研 | 智能纹理 1 张：RGB=BaseColor，A=R+M+AO 混合编码 | 5 张图压成 1 张（80% 压缩率） |
| **暗区突围** | 完整 PBR 四件套（Material + Lighting + Camera + Shading） | 标准 BaseColor / Normal / Roughness / Metallic / AO | —（强调 Camera 端物理参数） |
| **使命召唤手游** | 4 级 Shader LOD 数学拟合 | BaseColor + Normal/Roughness + Metallic/AO（3 张） | 2/3 通道贴图合并 + Shader 端拟合 |
| **CFM** | Unity 5 PBR + HDR + Tonemap | 标准 PBR 工作流 | — |

通道复用谱系

从激进到保守：**和平精英 PBR-Lite（5→1）** > **三角洲 Normal 4 通道** > **CODM 三张图合并** > **暗区突围 标准 PBR 五件套**。
压得越狠 → 内存/带宽越省，但材质 LOD 切换难度越高。

## 四、阴影与光照方案

| 游戏 | 阴影方案 | 间接光（GI） | 烘焙工具 |
| --- | --- | --- | --- |
| **三角洲行动** | 3 级 CSM 1024² × 各级 + 远景仅大型物体 | 体素 + 烘焙 Probe + Streaming（多端同算法） | 自研体素烘焙 |
| **和平精英** | 2 级 CSM + Lightmass 烘焙（室内） | Lightmap + Lightprobe | UE Lightmass |
| **暗区突围** | 动态阴影 + RT 软阴影（旗舰） | 预计算 GI（室内） | — |
| **使命召唤手游** | 动态主光阴影 + 预烘焙 ShadowMask | IBL Cubemap + SH（直接光）+ Lightmap（静态物） | **自研 GPU 烘焙**（4-6h → 3-5min） |
| **CFM** | 实时光阴影 + Lightmap | HDR + 大气散射（米氏 + 瑞利） | Unity 内建 |

## 五、地形与大世界方案

| 游戏 | 地形方案 | 路面处理 | 远景 |
| --- | --- | --- | --- |
| **三角洲行动** | SVT（不用 RVT）+ Clipmap + 自适应纹理数组 | **路面无独立 Mesh，直接走地形材质层** | 整块 HLOD Mesh 3000 面 |
| **和平精英** | 双层架构：远景 1 张 Mesh（17520 面 / 1 DC）+ 近景 UE Landscape | Spline 改 Heightmap 适配公路 | HLOD 全场景烘焙 |
| **暗区突围** | UE4 Landscape | — | — |
| **使命召唤手游** | Unity Terrain 深度改造 + Houdini PCG + Vertex Texture Fetch + DC 合并 | — | — |
| **CFM** | Unity Terrain | — | HLOD |

最激进的地形方案是三角洲

SVT + Clipmap + 自适应纹理数组 + 路面材质层 + 3D Decal 立体贴花 + 32→8 动态纹理数组 —— 一整套 GDC 三场分享披露的技术体系，国内手游里没有第二家做到这个深度。

### 📊 关键性能基线柱状对比

Drawcall / Overdraw / 显存 性能基线对比（公开数据）


Drawcall / 帧


未公开
三角洲


~280
和平


未公开
暗区


~120-300
CODM


未公开
CFM/FF

0
200
300


Overdraw 倍数


未公开
三角洲


2.83×
和平

未公开
暗区

未公开
CODM

未公开
CFM/FF
0
2.5×
3.5×


单帧纹理显存


未公开
三角洲


5.3 MB
和平

未公开
暗区

未公开
CODM


~400MB
FF包体
0
中
大

⚠️ 三角洲、暗区突围、CODM、CFM 的精确截帧数据未在公开渠道披露 · 此图基于第三方截帧分析

## 六、独家技术亮点

#### 三角洲行动 · VT Pass 通道重排（Subpass）

地形 VT Page 用 Vulkan Subpass / FrameBuffer Fetch 在片上做 channel repack，3→2 RT，节省 33% 带宽。代价是 VT Page 边缘退化为 Alpha Mask。

#### 暗区突围 · 144FPS 帧预测

插帧算法 + 双 RT BasePass 拆分，避开高通 DCVS 不平衡帧负载。iPhone 14 Pro 实测 97 → 118 FPS，温度 -4 ℃，功耗 -19%。

#### 暗区突围 · 移动端硬件光追（Vulkan Ray Query）

骁龙 8 Gen 2 + 已硬件加速 Ray Query，应用于反射、软阴影、AO。Ray Query 可在 Shader 任意层级用，比传统 RTX 更适合移动端集成。

#### CODM · OnePassHDR + 4 级 Shader LOD

低端机 Shader 端数学拟合 ToneMap 曲线，省 1 个全屏 RT 切换。Shader LOD 4 级覆盖骁龙 835 → 520 → 兼容机器。

#### CODM · GPU 烘焙

自研 GPU 烘焙，复杂场景 Enlighten 4-6 小时 → 自研 3-5 分钟，迭代速度 100×。

#### 和平精英 · PBR-Lite 智能纹理

5 张 PBR 贴图（BC/N/R/M/AO）压成 1 张：RGB=BaseColor，A 通道编码 R+M+AO。Shader 端解码，压缩率 80%。

## 七、低配适配策略

| 游戏 | 低配下限 | 策略 |
| --- | --- | --- |
| **三角洲行动** | 极低端走退化路径 | 不用 VT、不用贴花、地形 1m → 2m、ID Map 替代纹理混合 |
| **和平精英** | **1GB RAM + GLES 2.0** | **独立 Lite 版本分支**，玩法一致，渲染极致砍 |
| **暗区突围** | —（核心是高端旗舰） | 不主打超低配市场 |
| **使命召唤手游** | 4 级 Shader LOD 兜底 | L1 解决兼容性问题机型 |
| **Free Fire** | **包体 400MB / 全球低配** | Garena 在 Unite 2019 专门讲超低配优化（CPU/GPU 双管齐下） |

两条路线

**独立 Lite 分支**（和平精英）vs **同 codebase 退化档位**（三角洲、CODM）。前者维护成本高但效果好，后者轻巧但极低配体验受限。Free Fire 是把整个产品定位在低配市场。

## 八、横向启示

#### ① 国内 FPS 手游的"两强一新"格局

**三角洲**（技术深度）+ **暗区突围**（前沿激进）已经领先世界水平；和平精英是工程化典范。三家都来自腾讯，光子、天美、魔方各自精彩。

#### ② 大世界 FPS = UE4，地图制 FPS = Unity

引擎选型不是"谁更好"，而是"谁更适合产品形态"。开放世界 + Streaming + HLOD = UE4；中型 + 中台 + PBR 工业化 = Unity。

#### ③ Forward 是手游 FPS 的事实标准

5 款游戏 4 款用 Forward。延迟只在三角洲的旗舰档位出现。**FPS 户外光源单一 → Forward 在带宽和功耗上完胜**。

#### ④ 通道复用是 PBR 移动端必修课

从 5→1（和平精英）到 4 通道复用（三角洲）到三张图合并（CODM），**通道复用是省内存/带宽的最大杠杆**。代价是 Shader 端解码逻辑。

#### ⑤ 帧预测是手游下一代标配

暗区突围 GDC 2024 验证：帧率涨 + 温度降 + 功耗降"三项全优"。预计 1-2 年内会成为高帧档位标配。

#### ⑥ 移动端硬件光追时代已开启

暗区突围已用 Vulkan Ray Query 上线。但仅 1%~3% 用户能用，留意"高 LTV 用户"价值。

#### ⑦ 自研中台/烘焙工具是必修

CODM 的 GPU 烘焙、和平精英的内存三层下钻、三角洲的虚拟材质系统、暗区突围的 RT 改造 —— **大项目都靠"中台 + 自研工具链"取胜**，不是单纯靠引擎能力。

#### ⑧ 公开度差异巨大，技术壁垒在"愿不愿意分享"

三角洲、暗区突围、CODM 在 GDC 上有完整披露 → 行业知识溢出强；和平精英、CFM、Free Fire 偏内部 → 外界只能看截帧猜。**技术领先 ≠ 公开领先**。

## 九、详细报告链接

[**📘 三角洲移动端技术要点总结**

双管线分级、VT Pass 通道重排、虚拟材质系统、SVT 地形、Clipmap 等 14 个章节](三角洲移动端技术要点总结.html)
[**📕 和平精英移动端技术要点总结**

UE4 深度定制、双层地形、PBR-Lite 智能纹理、Lite 版超低配适配 等 14 个章节](和平精英移动端技术要点总结.html)
[**📗 使命召唤手游移动端技术要点总结**

PBR 工业化、Scalable 管线 OnePassHDR、4 级 Shader LOD、GPU 烘焙、Houdini PCG 等](使命召唤手游移动端技术要点总结.html)
[**📙 暗区突围移动端技术要点总结**

144FPS 帧预测、移动端硬件光追、动态天气系统、PBR 自动曝光 等](暗区突围移动端技术要点总结.html)

### 其他参考资料（CFM / Free Fire）

- [CFM 2.0 引擎升级公告（Unity 5 + PBR + HDR + Tonemap）](https://cfm.qq.com/webplat/info/news_version3/17544/21851/21852/m19873/201806/725454.shtml)
- [CFM《最终12小时》技术揭秘（PBR + 6×6 km + 大气散射）](https://cfm.qq.com/ingame/web201809/detail.shtml?nid=6566640)
- [Unite Shanghai 2019 · Free Fire Unity 优化策略](https://www.im2maker.com/news/20190422/f42562f5ad1741fa.html)
- [UE4 优化移动端渲染管线（项目实战，含 SceneColor RT 优化）](https://zhuanlan.zhihu.com/p/567352645)
- [性能优化随笔（一）TBR 架构、glFlush 陷阱](https://zhuanlan.zhihu.com/p/594060717)

本文档基于 5 款 FPS 手游公开技术资料整理 · 横向对比视角
