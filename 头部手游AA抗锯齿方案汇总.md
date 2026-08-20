# 二次元手游 · AA 抗锯齿方案横向专题

> TAA / TAAU / MSAA / FXAA / SMAA：各家怎么选，为什么 MSAA 在二次元赛道几乎缺席

**覆盖引擎**：Unity SRP / UE4 / 自研 · **视角**：移动端（Android / iOS）优先 · **标杆案例**：鸣潮 TAAU（UFSH2023 官方演讲）· **核心指标**：AA 帧时间代价 +3%~25% 分档

---

> **⚠️ 资料口径说明**
> 本份为横向整理型报告：核心案例（鸣潮 TAAU）来自 UFSH2023 官方演讲；各游戏 AA 选项主要来自玩家实测与攻略截图；成本数字来自本地 UE Mobile MSAA 工程实践沉淀（排错实测）。公开技术资料对"各游戏 AA"普遍只覆盖到选项层面，深挖各家 TAA 内部调校的公开材料有限，请以官方为准。

---

## 一、核心结论速览

### AA 的两类心智模型
- **空间类**（单帧内解决）：MSAA / SMAA / FXAA
- **时间类**（跨帧利用历史）：TAA / TAAU / TSR
- 二次元主流全面倒向**时间类**，空间类只留低成本兜底

### 生态结论
- 移动端：**TAA 一统**（原神/绝区零/崩铁移动端仅有 TAA）
- PC 端：TAA 为主，辅以 **SMAA** 替代（原神/绝区零）
- **MSAA 缺席**：原生提供 MSAA 的二次元手游几乎为零

### 不用 MSAA 的四因
1. Deferred 管线硬门槛（GBuffer MSAA 成本爆炸）
2. 只抗几何边缘，管不了 shader/描边 aliasing
3. 移动端 tile memory 翻倍 + 高 PPI 边际收益低
4. TAA 白送时间降噪 + 上采样

> **总体取舍**：二次元对 AA 的需求与写实游戏完全不同：**锐利轮廓 + 大面积纯色 + 屏幕边缘像素占比高达 4%~5%（写实仅 1%~2%）**。MSAA 的 per-sample coverage 只对几何边缘有效，且 Deferred 管线用不了；而 TAA 系成本固定（+3%~8%）、覆盖 shader aliasing、还白送时序降噪与上采样——「选择 TAA 不是退而求其次，是产品形态下的正解」。

---

## 二、概念辨析：六种 AA 到底在干什么

| 方案 | 原理 | 帧时间代价（移动端） | 抗哪种锯齿 | 致命短板 | 移动端地位 |
|---|---|---|---|---|---|
| **SSAA** | 高分辨率渲染后降采样 | ≈ 分辨率放大比例 | 几何 + shader 全覆盖 | 着色数倍增，最贵 | 几乎不用 |
| **MSAA** | per-sample coverage + per-pixel 着色 | 4x 约 +10%~25%（实测） | **仅几何边缘**（三角覆盖） | 管不了 shader aliasing；Deferred 用不了 | 写实/FPS 分档在用；二次元缺席 |
| **SMAA** | 图像化后处理：边缘检测 + 形态学混合 | 较低（1 个全屏 pass） | 几何边缘（近似全局） | 无时序稳定；对细线/亚像素弱 | 主要出现在 PC 端 |
| **FXAA** | 亮度梯度检测边缘 + 加权混合 | 约 +1%~3%（<1ms） | 几何边缘（较弱） | 全屏模糊、亚像素细节差、无时序稳定 | 低端机兜底 / 关 |
| **TAA** | 跨帧抖动采样 + 历史帧混合 | 约 +3%~8% | 几何 + specular + alpha test + 闪烁（最广） | 鬼影（ghosting）、需 velocity | **移动端二次元主流** |
| **TAAU / TSR / FSR** | TAA + 上采样（超分） | 视实现（含重建开销） | 同上 + 自带超分 | 同 TAA，且低分辨率下细节损失 | 鸣潮/三角洲等在用 |

> **关键区分**：**MSAA 管不了 shader aliasing**——specular 闪烁、normal 抖动、alpha test 硬边、NPR 描边都不是"几何覆盖"问题，MSAA 一律无效。这正是 FPS 高速转视角下"specular 闪烁扎眼、MSAA 却毫无办法、TAA 立竿见影"的原因。

---

## 三、引擎机制对比：Unity vs UE 的 AA 底座

### Unity（URP / 内置管线 / 自研 SRP）
- **内置管线 / URP**：Forward 下原生支持 MSAA（HDR 关闭时 4x 便宜，TBDR 上近似免费），TAA 需 URP 3.x 之后或自研
- **原神/崩铁/绝区零**走自研 SRP：移动端给 **TAA**，PC 端给 **TAA + SMAA**，主动不给 MSAA
- SRP Batcher / 自研后处理链里 TAA 与降噪、超分可共享历史缓冲

### UE（Mobile Forward / Deferred）
- **Mobile Forward**：支持 MSAA（`r.Mobile.AntiAliasing=3` + `r.MSAACount`，本地实测 4x +10%~25%）
- **Deferred**：MSAA 直接出局，只能 TAA/TAAU/TSR
- 移动端 TAA 特殊坑：velocity + 历史 buffer 成本被放大，30fps 下历史帧间隔大 → **鬼影更严重**，做不好反不如 FXAA
- 勾边（Outline）不渲染 velocity → TAA 无法回溯 → 边缘闪烁（鸣潮踩过）

> **⚠️ 注意**：引擎"能支持 MSAA"≠"愿意用 MSAA"。Unity 移动端开 MSAA 在技术上很顺，但原神/崩铁/绝区零移动端都只给 TAA——说明各家是在**产品层面**（画面风格 + 成本结构）主动放弃 MSAA，而不是引擎限制。

---

## 四、为什么不用 MSAA：四层根因 + 端游为何在移动端失效

### 根因 ① 管线硬门槛：Deferred 下 MSAA 成本爆炸
MSAA 需要 resolve，且每个 attachment 都要 4x。Deferred 光 GBuffer 就有 3~5 个 RT，全部 4x 后带宽 + tile memory 直接翻好几倍。燕云十六声移动端技术总结里写得很直白：**「不支持 MSAA（GBuffer MSAA 成本爆炸）」**；UE Deferred 干脆不提供。二次元主流（鸣潮、燕云）都是 Deferred → MSAA 第一步就出局。

### 根因 ② 覆盖范围错配：MSAA 治不了二次元最痛的 aliasing
二次元屏幕边缘像素占比 4%~5%，且轮廓大量来自 **shader 描边、Ramp 色阶、alpha test（发丝/配饰）**，不是三角形几何覆盖。MSAA 只对几何边缘有效，对色阶断裂、描边抖动、specular 闪烁一律无效。反观 TAA 通过跨帧重建恰好覆盖这些。

### 根因 ③ 移动端成本结构不划算
TBDR 下 MSAA 带宽近乎免费（tile 内折叠、resolve 在片上），但 **tile memory 翻倍**是真开销：4x MSAA 实测帧时间 +10%~25%、2x +5%~12%，**高端机更值、低端机更痛**（两极分化比 PC 剧烈）。加上手机高 PPI（400+）几何锯齿本就不明显，720P 开 4x 几乎无视觉收益、GPU 却翻倍——**AA 档位必须和渲染精度联动**（本地《画质分级方案汇总》：渲染精度 ≤0.8 强制 FXAA 或关）。

### 根因 ④ TAA 白送两个 MSAA 没有的能力
1. **时间降噪**：体积云、SSR、AO 的时序噪点都靠 TAA 历史缓冲；
2. **上采样**：TAAU/FSR 式低渲染精度 + 时序重建换性能。

二次元大面积纯色，TAA 的「模糊 + 锐化」完全可控（鸣潮 Unsharp），而 MSAA 只能干干巴巴抗个几何。

> **⚠️ 端游方案为何在移动端失效**：PC 上 MSAA 用显存换质量（带宽有富余、resolve 走 GPU 全速、渲染分辨率足够高时几何锯齿反而明显），甚至可以做「回读 + 硬件查询」式决策。移动端：tile 内存是稀缺品（MSAA 直接翻倍）、带宽在低端机紧张、GPU readback 会打断流水线基本不可用，且高 PPI 天然稀释 MSAA 收益——**同一套 MSAA，在 PC 上是「用钱买质量」，在手机上变成「用帧数买几乎看不到的收益」**。这就是写实 FPS 手游也更多转投 TAA/FSR 的原因。

---

## 五、标杆案例深拆：鸣潮自研 TAAU

二次元 + UE4 Deferred + 自研 TAAU，公开材料里调校细节最完整的一家（UFSH2023 王宏波演讲），可作为二次元 TAAU 的参考模板。

### TAA vs TAAU 本质区别

一句话：**TAAU = TAA + 上采样**。传统 TAA 只抗锯齿（输出分辨率 = 渲染分辨率，纯画质成本项）；鸣潮 TAAU 把"低分辨率渲染 + 时序重建"合并进同一 pass，抗锯齿和超分一件事做完，从"成本项"变"性能项"。

| 维度 | 传统 TAA（UE 默认） | 鸣潮自研 TAAU |
|---|---|---|
| 核心职责 | 纯抗锯齿 | 抗锯齿 + 上采样（超分） |
| 分辨率关系 | 渲染 = 输出 | **渲染 < 输出**（低分辨率渲染，时序重建到目标） |
| 性能定位 | 纯成本（+3~8%） | **性能手段**（省填充率/带宽/着色） |
| 采样模式 | 9 tap（PC） | 移动端**十字 5-tap cross** |
| History Clamp | UE 默认（AABB + 变体） | 最廉价 AABB；高配 **YCoCg** / 低配 **RGB** |
| Pass 位置 | Bloom 前 | **Bloom + Tonemap 之后** |
| 速度信息 | 引擎 velocity buffer | 自研 Velocity Pass（24 位 RGB）+ **Character Mask** |
| 上采样算法 | 无 | **FSR 1.0 风格多项式逼近**（Lanczos2，带边缘信息） |
| 二次元特化 | 通用调校 | 动静分离权重 + 低通滤波 + Unsharp 锐化 |

### 管线改造
- 新增 **Velocity Pass**：速度编码进 24 位 RGB + **Character Mask**（角色遮罩，防鬼影/染色）
- TAA Pass 从默认位置移到 **Bloom + Tonemap 之后**，升级为 **TAAU**（时间性超分辨率）

### 移动端鬼影优化（带宽权衡）

| 维度 | PC | 移动端 |
|---|---|---|
| 采样 | 9 点 | **十字星 5-tap cross**（省带宽） |
| History Clamp | — | 最廉价的 **AABB 包围盒** |
| Clamp 颜色空间 | — | 高配 **YCoCg**（亮度感知准）/ 低配 **RGB**（省转换） |

### 角色边缘闪烁优化（核心痛点）
勾边（Outline）成本极高（BasePass×2 / Velocity×2 / Shadow / 蒙皮×2），移动端**去掉了勾边 Pass 的 Velocity 渲染** → 头发/服饰边缘无法回溯历史帧 → 闪烁严重。三重对策：
1. **动静分离权重**：速度越大当前帧权重越高（抑鬼影）；速度越小历史帧权重越高（稳画面）
2. **低通滤波**：对闪烁边缘区域用已有 5-tap 数据简单模糊压制
3. **动态锐化**：对被 TAA 糊掉的动态物体用 **Unsharp Mask** 局部锐化（复用 5-tap，不额外采样）

### 上采样
实现类似 **FSR 1.0 的多项式逼近**（如 Lanczos2），除距离外还考虑边缘信息，优于双线性。

### 为什么这么选型（五个动机）
1. **性能首要**：开放世界移动端撑不起原生分辨率，TAAU 用低分辨率渲染省下填充率/带宽直接变帧率预算（60/120fps 双档、压功耗）
2. **管线前提**：Deferred 无法 MSAA，只剩时序 AA；FXAA 糊、SMAA 无时序稳定 → TAA 是唯一覆盖"几何 + shader + 闪烁"全类的
3. **画面形态**：NPR 边缘占比 4~5%、大面积纯色利于时序重建；"糊"可用 Unsharp 拉回
4. **工程成本**：AA + 超分合体，省一个超分 pass，时序历史同时服务重建质量
5. **移动端权衡是显式设计**：5-tap / AABB / YCoCg-RGB 分档 / Character Mask / 动静权重——每项都是带宽-画质显式决策

### 代价与权衡（选型不是免费的）
- **鬼影风险**（时序 AA 通病，靠 Character Mask + 动静权重 + YCoCg clamp 抑制，快速转视角 / 30fps 仍有残留）
- **上采样重建伪影**（运动物体 / 高频纹理低分辨率下过糊或振铃，靠动态锐化缓解）
- **Pass 位置后移**：Bloom 放大边缘噪声（拿一点质量换一次全屏采样）
- **移动端 TAA 老坑**：velocity + 历史 buffer 成本放大，做不好反不如 FXAA

### 对我们的启示（结合 Forward + MSAA 场景）
1. TAAU 的"AA + 超分合体"值得借鉴，但描边 mask 是 1x 硬边，TAA 抖动 + 历史混合会引入时序抖动，需 mask 参与 TAA 处理或配锐化
2. 鸣潮"勾边不写 velocity → 闪烁 → 补救"是**反面教材**：未来切 TAAU，勾边 mesh 必须写 velocity
3. 分档思想（YCoCg/RGB、5-tap/AABB）与 MSAA 分档（4x/2x/FXAA）一脉相承

---

## 六、头部手游案例横向对比

| 游戏 | 引擎 / 管线 | 移动端 AA | PC 端 AA | 来源 |
|---|---|---|---|---|
| 原神 | Unity 自研 SRP | 仅 TAA | TAA / SMAA | 玩家实测 / 官方 |
| 绝区零 | Unity SRP | TAA | SMAA | 玩家实测 |
| 崩坏：星穹铁道 | Unity SRP | TAA | TAA / SMAA | 玩家实测 |
| 鸣潮 | UE4 深度定制（Deferred） | **自研 TAAU** | TAAU / DLSS | UFSH2023 官方 |
| 幻塔 | Unity | — | FXAA / SMAA / TAA / TAAU | 玩家实测 |
| 燕云十六声 | 网易 Messiah（Deferred） | **不支持 MSAA**，轻量 TAA | MSAA 2x/4x + TAAU / DLSS | 本地截帧 |
| 崩坏3 | Unity（老管线） | 游戏内无 MSAA，靠系统「强制 4x MSAA」 | — | 玩家实测 |
| 战双帕弥什 | Unity | 攻略普遍建议关闭抗锯齿 | — | 攻略 |
| 使命召唤手游 | UE4 | TAA | — | 本地截帧 |
| 三角洲行动 | UE4 改 | FSR 超采样 AA | — | 官方 |

> **横向规律**：① **Unity 系**（原神/绝区零/崩铁）：移动端 TAA、PC 端补 SMAA；② **UE/Deferred 系**（鸣潮/燕云）：TAA/TAAU，燕云移动端明确砍掉 MSAA；③ **唯一能开 MSAA 的**是崩坏3 这种老 Unity 游戏，且玩家只能靠**系统层强开**（开发者选项）——游戏内原生不给。二次元原生 MSAA 基本绝迹。

---

## 七、避坑清单

### TAA 鬼影 / 糊
- 30fps 转视角历史帧间隔大 → ghosting 更严重；FPS 场景慎用
- 勾边/半透物体不写 velocity → 边缘闪烁（鸣潮踩坑，靠锐化补救）
- 纯 TAA 必糊：需要锐化（Unsharp / 动态锐化），否则玩家投诉「糊」
- 历史 clamp 用 RGB 会漂色，高配用 YCoCg 感知更准

### MSAA 在 UE 移动端的工程坑
- 4x depth 与 1x mask 混绑同一 render pass → samples mismatch ensure（只绑 Resolve）
- PreOutline 写 4x Depth.Target、outline 采样 Depth.Resolve → 深度不一致 → 角色整脸涂黑
- `r.MSAACount=1` 是「AAM_MSAA + 1 采样」混合态，按采样数判断会走错分支
- MSAA + velocity 联动：非 TAA 时 `ShouldRenderVelocities=false` → 角色深度可能永久缺失

### FXAA / SMAA
- FXAA 全屏糊纹理/小字/UI，subpixel 细节差，无时序稳定 → 只配低端兜底
- SMAA 在移动端少用：形态学混合也要一个全屏 pass + 历史处理，性价比不如 TAA
- 分辨率越低 FXAA 越糊；720P 下开 MSAA 4x 纯浪费 GPU

### 分档与联动
- AA 档位应与渲染精度联动：渲染精度 ≤0.8 → 强制 FXAA 或关
- 高端机 4x MSAA（Forward）/ 中端 2x / 低端 FXAA 或关（本地实践结论）
- Deferred 无 MSAA 可选，只能 TAA/TAAU，且要配锐化

---

## 八、落地 Checklist 与定位工具

### AA 选型决策树（移动端二次元）
1. 管线是 **Deferred**？→ MSAA 出局，直接 TAA / TAAU，必配锐化（`Unsharp`）。
2. 管线是 **Forward**？
   - 高端机 + 追求轮廓锐利 → MSAA 4x（TBDR 上带宽近似免费，真贵在 tile memory）
   - 中端机 → MSAA 2x
   - 低端机 → FXAA 或关（高 PPI 下收益可忽略）
3. 场景 specular / 闪烁严重 且有成熟移动端 TAA → 优先 TAA（哪怕 Forward）
4. 需要性能收益 → TAAU / FSR 式超分（低渲染精度 + 时序重建）
5. 勾边 / 轮廓类渲染 → 必须让它们写 velocity，否则 TAA 无法回溯 → 边缘闪烁

### UE 定位命令
- `r.Mobile.AntiAliasing`：1=FXAA / 2=TAA / 3=MSAA
- `r.MSAACount`：MSAA 采样数（默认 4）；`r.ScreenPercentage`：渲染精度
- `r.TemporalAA.Upsampling / Algorithm / FilterSize / CurrentFrameWeight`：TAAU 调校
- `r.PostProcessAAQuality`：后处理 AA 质量档
- RenderDoc 定位：MSAA 下观察 samples mismatch ensure、Resolve 绑定、SceneDepth vs Resolve 一致性

### Unity 定位
- URP：`UniversalRenderPipelineAsset → MSAA Samples / HDR`（HDR 开时 MSAA 4x 变贵）
- Frame Debugger 看后处理链；TAA 历史缓冲是否与降噪/超分共享

---

## 九、关键启示

### 对二次元项目
1. Deferred 直接砍 MSAA，别为兼容性留后门——GBuffer 4x 是纯浪费
2. TAA 是刚需，但必须配套锐化 + velocity 覆盖（含描边），否则「糊 + 闪」两头挨骂
3. NPR 边缘占比高 → 时间 AA 是正解，MSAA 的 coverage 精度对色阶/描边无效

### 对类似工程团队
1. AA 选型先定管线，再谈算法；分档与渲染精度联动是刚需
2. 移动端 MSAA 真实开销是 tile memory 翻倍，不是带宽——别用 PC 直觉估算
3. 玩家系统层强开 4x MSAA（崩坏3 案例）= 原生 AA 不满足的信号，优先调 TAA 参数而不是上 MSAA

---

## 十、TAAU 的更进一步：移动端超分技术（FSR / XeSS / SGSR / MetalFX）

> 作为 TAAU 的演进方向，AI 超分（FSR4/XeSS3）与多帧生成是最新热点。但"移动端"必须拆成**手机**与**掌机/笔记本**两个世界——**FSR 4 / XeSS 3 这代 AI 超分 + 多帧生成只进了掌机/笔记本，手机端至今零部署**。手机端 TAAU 的真实演进是平台级方案（MetalFX / SGSR / 厂商自研）。

### 1. FSR 家族分代落地

| 代际 | 技术路线 | 手机端 | 掌机/APU | 实测/状态 |
|---|---|---|---|---|
| FSR 1.0 | 空间超分 | ✅ 手游有 | — | 三角洲等低配档 |
| FSR 2.x | **时间超分（= TAAU 思路）** | ⚠️ 少 | — | 需引擎给 MV+depth |
| FSR 3.1 | 超分 + 帧生成（非 AI） | ⚠️ 模拟器跑 PC 版 | ✅ 掌机标配 | 红魔 11 Pro 跑《生化危机：安魂曲》720p 40–100fps |
| FSR 4（Redstone ML） | AI/ML 超分 | ❌ | ❌ 仅桌面 RDNA4 | RX 9000 独占 |
| FSR 4.1 | 轻量 ML 模型 | ❌ | 评估中 | 已给部分 RDNA3 桌面卡，掌机无时间表 |

- **FSR 3.1 手机实测**（2026.03，ETA Prime）：红魔 11 Pro（骁龙 8 Elite）+ GameHub 模拟器跑 PC 版《生化危机：安魂曲》，720p 低画质 + FSR 3.1 性能模式：室内 60–100fps、室外复杂 40–45fps；**帧生成因稳定性被关**（只用超分），功耗 >20W 强制风扇+液冷仍热降频，16GB 内存硬门槛。能跑但替代不了 Steam Deck/ROG Ally。
- **FSR 4 不进旧硬件的原因**：AMD 副总裁 David McAfee——不是时间表问题，是**质量门槛**：模型针对 RDNA4 ML 运算单元优化，旧硬件缺 TOPS/AI 吞吐。FSR 4.1 的"轻量 ML 模型"是给 RDNA3 APU 的妥协版，掌机无时间表。

### 2. XeSS 家族：手机完全缺席，移动计算端猛攻

- **手机端 = 0 部署**（XeSS 依赖 XMX AI 单元，手机 SoC 没有）
- 移动计算端：XeSS 2 → **XeSS 3**（2026 CES）＝ 超分 + **多帧生成 MFG（最多插 3 帧→4x）** + 低延迟
- **向下兼容**：Meteor/Lunar/Arrow Lake + Arc A/B 全系 + Panther Lake（FSR4 做不到）
- Intel 声称 Arc B390 iGPU 超 AMD Radeon 890M：1080p（540p 超分）平均快 73%（45W vs 53W），原生 1080p 快 82%
- 实测（ETA Prime）：蜘蛛侠2 1200p High + XeSS Quality 60–70fps；赛博朋克 1200p Ultra 40–50fps；约 45 款游戏首发 MFG
- 掌机专项：ComputeX 2026 发布 **Arc G 系列**（Intel 18A，Arc B390 + XeSS 3），首款专为掌机设计的处理器，2026.06 起 Acer/MSI/OneXPlayer 出货

### 3. 手机原生超分生态（TAAU 在手机上的真实演进）

手机原生普遍停在 **FSR1–FSR2 级别**，距 DLSS4/FSR4 差一代半：

| 方案 | 技术 | 水平 | 状态 |
|---|---|---|---|
| 苹果 MetalFX | 空域版≈FSR1 略好；**时域版≈DLSS3/FSR2** | 手机最强但适配少 | 约 DLSS2 水平 |
| 高通 SGSR1 | 空间超分，单通道 12-tap Lanczos | ≈FSR1 | COD 战区手游 / 诛仙 / 永劫无间手游 |
| 高通 SGSR2 | **时间超分（TAAU 路线**，需 MV+depth） | ≈FSR2 | 声称"1080p→4K 性能 2 倍"⚠️存疑 |
| ARM ASR | 开源 FSR2 魔改，号称省 30% 带宽 | ≈FSR2 | 无厂商采用 |
| 厂商自研（iQOO QNSS / 华为超分插帧） | 实验阶段 | 早期 | 未成熟 |

### 4. 关键洞察（对引擎选型）

1. **FSR4 / XeSS3 的 AI 超分 + 多帧生成，手机端 = 0 部署**。瓶颈是硬件加速单元 + 生态：方案绑 RDNA4/XMX，手机 SoC 的 NPU 没被这些 PC 方案接进去，要等 SoC 厂商自家 NPU 超分成规模——今天还没有。
2. **手机端 TAAU 更进一步是两条并行路**：① 引擎内自研 TAAU 深化（鸣潮路线，游戏层最优解，平台级 SR 拿不到引擎内部数据）；② 平台级 SGSR2 / MetalFX 时域版 + 帧生成（SoC 层兜底，SGSR2 本质就是 TAAU，帧生成再赚 2 倍帧率）。
3. **落地建议**：短期自研 TAAU 细化（防鬼影/闪烁）+ 预留时间超分接口（MV+depth 已具备），为未来 SGSR2/FG 或 NPU 超分留好数据喂入口；FSR4/XeSS3 是更远期选项。

---

## 十一、参考资料

- UFSH2023《鸣潮》基于 UE4 的多平台效果与性能优化实践（王宏波，库洛游戏）— [知乎](https://zhuanlan.zhihu.com/p/678876237) / [技术站](https://jishuzhan.net/article/1806989150843310082)
- 原神画质设置：[3DM（PC 仅 TAA/SMAA）](https://m.3dmgame.com/ol/abcd/gl/151728.html)、[贴吧（移动端仅 TAA）](https://tieba.baidu.com/p/7316104318)
- 绝区零画面设置：[9game（移动端 TAA / PC SMAA）](https://www.9game.cn/biji/392832.html)
- 崩坏3 系统强开 4x MSAA：[百度经验](https://jingyan.baidu.com/article/5552ef4795425d518ffbc9fc.html)
- 战双画质设置（建议关 AA）：[小米游戏中心](https://game.xiaomi.com/viewpoint/1127866428_1606180944825_16)
- 幻塔 PC 设置：[007soft](https://www.007soft.com/news/4181.html)
- 本地工程文档：`E:\AiDoc\UE-Mobile-MSAA-实现原理-Resolve机制与深度采样链路.md`（MSAA 成本/选型/工程坑实测）
- 本地总结：`燕云十六声 / 鸣潮 / 使命召唤 / 光遇 移动端技术要点总结`、`头部手游画质分级方案汇总`（E:\AiDoc\）
- 个人截帧沉淀（RenderDoc + UE 移动端 MSAA 排错实测）
- 移动端超分：FSR 4 不落地旧硬件 [TweakTown](https://www.tweaktown.com/news/109612/fsr-4-ai-upscaling-isnt-coming-to-current-gaming-handhelds-or-ryzen-ai-devices/index.html) / [Red94](https://www.red94.net/news/fsr-4-1-for-ryzen-handhelds-uncertain-amd-vp-outlines-plan-vs-intel-arc-g3/)；生化危机安卓实测 [GameChigua](https://gamechigua.com/hot-topics/resident-evil-requiem-android-emulation)；手机超帧水平 [Vgover](https://www.vgover.com/news/213653)
- XeSS 3：[Windows Central](https://tech.yahoo.com/gaming/articles/intel-reveals-xess-3-multi-220000223.html)、[Arc G3 掌机](https://www.91mobiles.com/hub/computex-2026-intel-arc-g3-g3-extreme-processors-announced/)、[Arc B390 vs 890M](https://www.techspot.com/news/110828-intel-claims-panther-lake-new-arc-b390-igpu.html)
- 手机平台级超分：Snapdragon Game Super Resolution [WCCFTech](https://wccftech.com/snapdragon-game-super-resolution-brings-upscaling-to-mobile/)、[高通超分提升原神帧率](https://www.thepaper.cn/newsDetail_forward_22974591)

---

*二次元手游 AA 抗锯齿方案横向专题 · 内部研究文档 · 信息基于公开演讲、玩家实测与本地截帧沉淀整理*
