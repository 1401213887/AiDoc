# UE Mobile TBDR 优化 — 验收索引（明早从这里开始）

> **给波哥的验收入口**。本次任务：收集 TBDR 优化技术 → 三款手游案例分析 → 是否改引擎决策 → iOS Metal 实现 → 迭代合并 → 查漏补缺。
> **生成时间**：2026-06-20 凌晨 | **建议验收顺序**：先读本索引 → 主文档 → 增补卷摘要

---

## 一、本次全部产出清单

### 主交付（E:\AiDoc，长期知识库）
| # | 文件 | 字数 | 定位 | 验收重点 |
|---|------|:----:|------|---------|
| 1 | `UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md` | ~23KB | **主文档**（10 轮迭代合并稿） | 全局：原理→双平台→三案例→决策→iOS |
| 2 | `UE_Mobile_TBDR_查漏补缺增补卷.md` | ~14KB | **增补卷**（100 点审阅） | 看摘要 3 大发现 + §2/§3/§4 |
| 3 | `UE_Mobile_TBDR_验收索引.md` | 本文件 | 验收入口 | — |

### 过程交付（F:\MobileWP，工作区）
| # | 文件 | 对应任务 | 状态 |
|---|------|---------|------|
| 4 | `UE_Mobile_TBDR_Optimization_TechDoc.md` | 任务①TBDR技术文档 | 已被主文档整合 |
| 5 | `UE_Mobile_TBDR_改造决策文档.md` | 任务②是否改引擎 | 已被主文档整合 |
| 6 | `iOS_Metal_TBDR_实现方案.md` | 任务③iOS Metal | 已被主文档整合 |
| 7 | `燕云十六声_片上GBuffer_技术总结.md` | 子调研详版 | 参考留存 |

> 主文档(1) = (4)(5)(6) 的合并迭代稿；增补卷(2) = 对全部的查漏补缺。**验收看 (1)(2) 即可**，(4)(5)(6) 为过程留档。

---

## 二、四个原始问题 → 答案速查

### Q1 如何在 Vulkan 下利用 TBDR 片上缓存降低写带宽？（结合 UE 代码）
**答**：三招——①消除 Store（`DontStore`/Memoryless）②合并 Pass（Subpass + Input Attachment）③减少工作量（Early-Z/剔除）。
- UE 代码证据：`FRHIRenderPassInfo` + `ESubpassHint::DepthReadSubpass`（主文档 §2.3）；真实 `LookupDeviceZ()` 五平台分支源码（增补卷 §2）。
- 一份 .usf 经 `#if` 阶梯编译到 GLES/Vulkan/Metal 三后端 —— RHI 抽象的硬证据。

### Q2 洛克王国 One Pass 如何实现？
**答**：UE4.26 前向改造。FrameBufferFetch + Depth Fetch 把 5 Pass/4 次写回收敛成 1 个 RenderPass，深度全程 Memoryless。**写带宽 -30%**（iPhone X 实测）。
- 关键修正：RGB10A2 是为绕开"SceneDepth 丢弃 + SceneColor.A 仅 8-bit"双重约束做的通道重排，非简单"写个标记"（增补卷 §3）。

### Q3 燕云十六声片上 GBuffer 如何实现？
**答**：⚠️ **燕云=网易 Messiah 自研引擎，非 UE5**。技术本质=GBuffer 全程驻留 Tile Memory 永不落主存（Subpass + Memoryless）。真实布局：B8G8R8A8+R10、~20byte/px、~1MB SRAM、Octahedron Normal + YCoCg。
- UE 同源实现：`r.Mobile.ShadingPath=1`，官方"GBuffer never stored in system memory"，材质指令 147→34（主文档 §4）。

### Q4 和平精英渲染管线如何实现？
**答**：UE4 Mobile Forward + 重剔除。Forward 选型（大世界+远视距+单主光），Shadow 去 Color RT、光影贴图 3→1、CSM 烘焙混合、Scalability 分档。DrawCall<300、Overdraw<3x（主文档 §5）。

### Q5（追加）是否需要改引擎？
**答**：和平精英=0 改造（纯配置）；燕云片上 GBuffer=基本 0 改造（`r.Mobile.ShadingPath=1`）；洛克王国完整 One Pass=需 fork 改渲染器。分水岭=RenderPass 编排权（主文档 §6）。

### Q6（追加）iOS Metal 怎么实现？
**答**：iOS 是 TBDR 天选平台。Vulkan Subpass↔Metal Programmable Blending（原生）、DontStore↔Memoryless、Opaque 免 PrePass（HSR）。独享 Imageblock+Tile Shading。UE RHI 抽象让 Renderer 层改造双端共用（主文档 §7）。

---

## 三、本轮查漏补缺的实质性提升（重点验收）

| 提升 | 性质 | 位置 |
|------|------|------|
| 真实 `LookupDeviceZ()` 五平台源码 | ➕ 源码核验，坐实"双端共用" | 增补卷 §2 |
| RGB10A2 因果修正（8-bit 精度墙） | ⚠️ 修正 | 增补卷 §3 + 主文档 §3 |
| PrePass × Subpass 深度读取冲突（互斥取舍） | ➕ 工程盲区 | 增补卷 §4 |
| 带宽估算公式（改造前算账） | ➕ 决策工具 | 增补卷 §5 |
| UE5.3/5.4/5.5 版本兼容矩阵 | ➕ | 增补卷 §6 |
| 改造前基准测量 SOP | ➕ | 增补卷 §7 |
| Apple tile 尺寸随 RT 格式变化 | ➕ | 增补卷 §8 |
| 术语表 16 条 | ➕ | 增补卷 §9 |

**100 点审阅统计**：✅无误 76 / ➕补充 22 / ⚠️修正 2。

---

## 四、诚信边界（验收时请注意）

1. 燕云=Messiah 闭源，GBuffer 布局来自公开技术要点，subpass 编排为架构推断。
2. 无任何编造的端到端功耗/温度数字（公开渠道确无）。
3. `LookupDeviceZ` 等源码来自社区解读，行号/宏名随 UE 小版本微调，落地前请在目标版本引擎源码确认。
4. RGB10A2 因果链为"引擎通用行为 + 洛克王国公开分享"综合推断，未经其团队逐字确认。

---

## 五、建议的后续动作（待你拍板）

- [ ] 把主文档与 E:\AiDoc 既有手游 html（和平精英/燕云/三角洲等）加交叉引用，形成知识库导航
- [ ] 提交 E:\AiDoc 的 git 变更（本次新增 3 个 md）
- [ ] 若项目要落地，按增补卷 §7 SOP 先做基准测量再决定是否 fork
- [ ] 可选：把"TBDR 优化决策方法论"沉淀为可复用 Skill

---

> 睡个好觉，明早顺着这份索引验收即可。有问题直接点对应文档的章节号。
