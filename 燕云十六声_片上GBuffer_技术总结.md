# 《燕云十六声》手游"片上 GBuffer"渲染技术总结报告

> 面向《Unreal Mobile TBDR 优化技术文档》的资料收集。
> **重要声明（资料可信度）**：经中英文联网检索，**未找到网易官方就《燕云十六声》"片上 GBuffer / On-Chip GBuffer"主题发布的公开技术分享（GDC / 开发者访谈 / 官方博客）**。本报告中凡涉及《燕云十六声》具体 GBuffer 通道数、字节数、收益百分比的内容，均为基于 TBDR 架构与商业引擎通用实现的**合理技术推断**，并已明确标注；凡引用 UE5 官方文档与 Messiah 引擎公开资料的部分，则为**有据可查**的事实。请勿将推断数字当作官方披露引用。

---

## 0. 一个必须先澄清的前提：本作不是 UE5，而是网易自研 Messiah 引擎

这是检索中最关键的事实纠偏：

- 多家媒体（新华社、人民网、环球网及游戏媒体）一致证实，《燕云十六声》（Where Winds Meet，Everstone Studio 开发 / 网易发行）采用**网易自研 Messiah（弥赛亚）引擎**，**并非 Unreal Engine 5**。
- Messiah 自 2013/2014 年起研发，是横跨移动 / 桌面 / 主机的全平台引擎，曾用于《天下》手游、《楚留香（一梦江湖）》、《荒野行动》、《命运：崛起》等。
- 据 Messiah 首席专家访谈，该引擎**早在 2018 年就引入了 Frame Graph 系统（比 UE 早约两年），2021 年升级到 Frame Graph 2.0**。Frame Graph 正是现代引擎自动管理 RenderPass / Subpass 合并、判定资源生命周期、把中间附件标记为"片上瞬态（transient / memoryless）"的核心基础设施——这恰恰是实现"片上 GBuffer"的技术底座。

因此，本报告对《燕云十六声》采用"片上 GBuffer"做**架构层面的合理推断**，而把 **UE5 Mobile Deferred 作为有公开文档、可量化对照的标准实现范本**来讲解原理（用户文档主题即为 UE Mobile TBDR，二者技术思路同源）。

---

## 1. 片上 GBuffer 解决了什么带宽问题

**传统桌面式 Deferred（IMR 思路）的成本**：BasePass 把 GBuffer（通常 3~5 张全屏 RT：BaseColor、Normal、Metallic/Roughness/AO、深度等）**写回主显存（store）**，LightingPass 再把它们**全部读回（load）** 做逐像素光照。在 1080p 下，一份 ~16 字节/像素的 GBuffer，单帧"写一次 + 读一次"就要搬运约 60–70 MB；叠加深度、多份 RT 后，GBuffer 往返带宽可达每帧上百 MB 量级。

移动 GPU 的痛点在于**主存带宽极小且功耗敏感**：检索资料中提到移动端有效带宽常在个位数到几十 GB/s 量级（远低于桌面 GPU 的数百 GB/s ~ 1 TB/s）。GBuffer 的主存往返直接转化为**发热与降频**，是移动延迟渲染最大的单项成本。

**片上 GBuffer 的解法**：利用 TBDR"分块在片上 Tile Memory 完成"的特性，让 GBuffer **只活在当前 tile 的片上缓存里**，BasePass 写入后立即被同一 tile 的 LightingPass 消费，**GBuffer 永不 store 回主显存**，最终只把 SceneColor 写回。带宽成本从"GBuffer 写 + GBuffer 读 + SceneColor 写"降为接近"仅 SceneColor 写"，使延迟渲染的带宽逼近前向渲染。

---

## 2. 技术实现原理：Subpass + Input Attachment 的片上数据流

以 Vulkan 为例（也是本作在 Android 上的主路径），核心是**单个 RenderPass 内的多个 Subpass 共享同一块 tile memory**：

```
RenderPass {
  Subpass0 (BasePass):   光栅化几何 → 写 GBuffer(MRT) 到片上 tile memory
  Subpass1 (LightingPass): 把上一 subpass 的 GBuffer 声明为 INPUT_ATTACHMENT,
                           直接在片上读取(subpassLoad) → 计算光照 → 写 SceneColor
}
// GBuffer 附件 storeOp = DONT_CARE / loadOp = DONT_CARE，标记为 transient/memoryless
```

数据流关键点：
1. **GBuffer 附件用 `LAZILY_ALLOCATED` / memoryless 内存类型**，配合 `storeOp=DONT_CARE`，告诉驱动"这块附件不需要主存备份"。
2. **Subpass 依赖（dependency）必须是 `BY_REGION`**，确保 Subpass1 只读取**同一 tile 内同一像素位置**的 GBuffer，从而无需跨 tile，全部落在片上。
3. LightingPass 通过 **input attachment（`subpassInput`，`subpassLoad()`）** 而非普通纹理采样读取 GBuffer——这是片上读取、不经主存的语义保证。

各平台等价机制（UE 官方文档明确列出，可直接套用到 Messiah 这类自研引擎）：
- **Android Vulkan**：Subpass + Input Attachment（主路径）。
- **iOS / Metal**：`framebuffer_fetch` 风格直接访问 tile memory。
- **Mali / PowerVR**：PLS（Pixel Local Storage）扩展。
- **Adreno**：`framebuffer_fetch` 扩展。
- **GLES**：依赖扩展，无统一方案。
- **IMR（桌面式立即模式 GPU）**：tile 技巧不适用，GBuffer 退化为主存普通纹理，无带宽收益。

Messiah 的 Frame Graph 系统在此扮演的角色：自动分析 pass 间的读写依赖，把生命周期只跨越相邻 pass 的 GBuffer 资源**自动判定为瞬态**并合并进同一物理 RenderPass，对应平台 API 上落地为 subpass 合并——开发者无需手写 subpass 拼接。

---

## 3. GBuffer 布局如何为移动端裁剪

移动端 tile memory 容量极小（通常每 tile 仅几十 KB 量级），GBuffer 的**每像素字节数（bytes-per-pixel）** 必须严格受限，否则 tile 装不下、分块尺寸被迫缩小、效率反降。

**有据可查（UE5 官方文档给出的硬性约束，适用于 Android Vulkan + Mali）**：
- GBuffer **每像素 ≤ 16 字节 / 128 位**；
- 最多 **4 个 input attachment**；
- LightingPass 阶段最多读 **3 个颜色附件 + 1 个深度附件**；
- 默认按此约束执行以保证全设备一致；放开则需 `MobileUsesExtendedGBuffer=true`。

为塞进 16 字节，移动延迟渲染普遍采用的压缩手段（通用实践 + UE 文档佐证）：
- **法线八面体编码（octahedral）**：法线压到两通道，省一个分量；UE 文档明确贴花法线即用八面体编码以缩减 GBuffer 占用。
- **材质参数通道打包**：Metallic / Roughness / AO / ShadowMask 等塞进同一张 RT 的不同通道。
- **低精度格式**：颜色用 8-bit（RGBA8 / sRGB），HDR 相关用 RG11B10 或 RGBA16F，深度复用 Z-buffer 不额外写。
- **着色模型受限**：默认只保 DefaultLit / Unlit，复杂着色模型按需局部开启，避免 GBuffer 再扩通道。

**对《燕云十六声》的推断（无官方数字）**：作为 UE 级画质的开放世界手游，其移动 GBuffer 极可能落在"3~4 张 RT、合计 ≤16 字节/像素、法线八面体编码、参数通道打包"这一与 UE Mobile Deferred 高度同构的区间。具体通道排布属引擎实现细节，**官方未公开，不做编造**。

---

## 4. 与 UE5 Mobile Deferred 默认实现的关系 / 差异

| 维度 | UE5 Mobile Deferred（`r.Mobile.ShadingPath=1`） | 《燕云十六声》/ Messiah（推断） |
|---|---|---|
| 引擎 | Unreal Engine 5 | 网易自研 Messiah |
| 核心思路 | **完全一致**：GBuffer 置于 tile memory，永不落主存 | 同源思路 |
| 片上机制 | Vulkan subpass / framebuffer_fetch / PLS（按平台） | 同样依赖 Vulkan subpass，由 Frame Graph 自动合并 |
| GBuffer 约束 | 默认 ≤16B/像素、≤4 input attachment（官方明文） | 受同样的硬件约束（Mali/Adreno 限制相同），具体布局未公开 |
| 抽象层 | RenderGraph(RDG) 管理瞬态资源 | Frame Graph 2.0 管理瞬态资源（2018 即引入） |
| 开放性 | 文档与源码公开，数字可查 | 闭源自研，无公开 GBuffer 规格 |

**结论**：两者是**同一类 TBDR 片上延迟渲染技术的不同实现**，受相同移动硬件物理约束，因此布局和带宽行为高度可类比；差异主要在引擎归属、上层资源图系统命名与具体通道排布。UE 文档可作为理解《燕云十六声》该类技术的**最佳可量化代理**。

---

## 5. 实际收益量化数据

**有据可查（UE5 官方文档，非本作数据）**：
- 同一个"简单颜色 + 粗糙度"材质，Mobile **Forward 需 147 条指令 + 2 个采样器**；切到 Mobile **Deferred 仅 34 条指令 + 0 采样器**（光照代码移出材质）。这是 deferred 在材质着色器复杂度上的直接收益。
- 支持 memoryless render target 的设备上，**GBuffer 不占系统内存**；不支持的设备会退回主存分配、收益减小。

**带宽收益的数量级推断（通用 TBDR 原理，非本作官方数字）**：片上 GBuffer 可省去 GBuffer 的"写回 + 读取"两趟主存往返。按 1080p、16 字节/像素估算，理论上每帧可节省约 **60–130 MB 量级**的 GBuffer 往返带宽（具体取决于 RT 数、分辨率、是否含深度往返），对应可观的功耗与温升下降。

**关于《燕云十六声》本身**：检索仅找到玩家侧的画质 / 帧率 / 功耗调校经验贴，**无任何官方公布的带宽节省百分比或 GBuffer 规格数字**，故此处不提供伪精确数据。

---

## 6. 信息来源 URL

**《燕云十六声》/ Messiah 引擎（事实层）**
- 网易官方（投资者关系）手游上线公告：https://ir.netease.com/zh-hans/news-releases/news-release-details/where-winds-meet-mobile-version-launches-today-game-awards-2025
- 新华社（自研 Messiah 引擎）：https://www.news.cn/digital/20251118/41e2bddd707c4a748a96d5bde6164ae3/c.html
- 人民网（Messiah 引擎光影表现）：https://jinbao.people.cn/n1/2025/1117/c421674-40605526.html
- 环球网（弥赛亚引擎 10 年自研、20+ 专利）：https://www.huanqiu.com/article/4PZ9mjrXaS3
- Messiah 引擎采用 / 非 UE5 解析：https://www.modernwarshipsdl.cc/14897.html
- Messiah 引擎技术沿革（Frame Graph、2018 引入早 UE 两年）：https://m.tuoluo.cn/article/detail-10097956.html
- 网易自研引擎背景（Draw Call、PBR）：https://www.163.com/dy/article/DGAG191C0526DPBA.html
- Messiah 移动端渲染管线重写（《命运：崛起》案例）：https://foro3d.com/ch-cn/2026/mayo/destiny-rising-como-netease-traslada-la-iluminacion-de-destiny-2-a-mov.html

**UE5 Mobile Deferred / 片上 GBuffer（原理与可量化对照）**
- UE 官方：移动渲染与着色模式（Mali 16B/4 input attachment 限制、各平台 tile memory 机制）：https://dev.epicgames.com/documentation/zh-cn/unreal-engine/mobile-rendering-and-shading-modes-for-unreal-engine
- UE 官方：Mobile Deferred Shading Mode（GBuffer 置于 tile memory、147→34 指令收益、memoryless）：https://docs.unrealengine.com/documentation/en-us/unreal-engine/using-the-mobile-deferred-shading-mode-in-unreal-engine
- `r.Mobile.ShadingPath` Cvar 说明：https://indxzero.github.io/ue544cvarwiki/articles/r.mobile.shadingpath/
- 剖析虚幻渲染体系 - 移动端专题（UE 4.26 起 Deferred）：https://www.ufcn.cn/it/1027192.html

**TBDR / 片上 GBuffer 通用原理（背景佐证）**
- TBDR 与 Deferred 在片上完成的原理：https://miaopasss.github.io/2026/05/07/%E6%B8%B2%E6%9F%93%E7%AE%A1%E7%BA%BF%E7%B1%BB%E5%9E%8B
- Subpass 共享片上内存、GBuffer 不写回主存：https://blog.csdn.net/qq_33060405/article/details/159409601
- 移动端 GBuffer 压缩（八面体法线、通道打包）：https://blog.csdn.net/ProceNest/article/details/155632613
- UE 移动端 TBDR / tile memory 渲染流程：https://blog.csdn.net/boxiaozi/article/details/159355298

---

### 一句话结论
《燕云十六声》采用网易自研 **Messiah 引擎（非 UE5）**，其 Frame Graph 系统具备实现"片上 GBuffer"的全部基础设施；但**网易未公开该作 GBuffer 的具体片上实现规格**。本报告以**有完整公开文档的 UE5 Mobile Deferred** 作为同源技术的可量化范本，阐明片上 GBuffer 的带宽收益与实现原理，并对本作做了**明确标注的合理推断**，未编造任何官方数字。
