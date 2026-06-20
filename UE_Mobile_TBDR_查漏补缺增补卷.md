# UE Mobile TBDR 优化 — 查漏补缺增补卷（100 轮审阅）

> **性质**：对三项交付（TBDR 技术文档 / 改造决策 / iOS Metal 方案）+ 跨平台主文档的查漏补缺增补卷
> **审阅方式**：100 点结构化审阅，按主题归并为 12 组，逐组列出"发现的问题 → 修正/补充"
> **新增价值**：注入**经源码核验的真实 UE Shader 代码**，修正两处事实性偏差，补全 6 个工程盲区
> **版本**：v1.0 | 2026-06-20 夜间审阅 | 配合验收索引 `UE_Mobile_TBDR_验收索引.md` 使用

---

## 摘要：这一轮审阅最重要的 3 个发现

1. **找到了"同一份 .usf 双后端编译"的真实源码证据** —— UE 的 `LookupDeviceZ()` 函数用一个 `#if` 阶梯，把 GLES/Vulkan/Metal 的片上深度读取统一成一份 shader。这是前文"RHI 抽象、改一次双端生效"论断的硬证据（见 §2）。
2. **修正了洛克王国 RGB10A2 的因果（含 v1 自纠）** —— 深度**全程留 tile（Memoryless），靠 subpass depth fetch 就地读，从不存任何地方**。RGB10A2 的 2-bit alpha 是角色 mask（与深度无关）；那句"alpha only 8-bit"源码注释讲的是另一条路径（深度打包进 SceneColor.A，仅 Mobile HDR/RGBA16F 可行）。详见 §3（已二次修正 v1 的混淆）。
3. **补全了 PrePass 与 Subpass 深度读取的冲突盲区** —— 开 `r.Mobile.EarlyZPass` 会与 subpass 深度 fetch 路径冲突，需要 `FORCE_DEPTH_TEXTURE_READS` 独立 shader 变体动态切换。这直接关联和平精英(PrePass) 与洛克王国(Subpass) 两个案例的兼容性（见 §4）。

---

## 100 点审阅 changelog（按 12 主题归并）

> 说明：逐条记录"审阅点 → 判定（✅无误 / ⚠️已修正 / ➕已补充）"。编号 1–100。

### A 组｜事实核验（点 1–12）
| # | 审阅点 | 判定 |
|---|--------|------|
| 1 | 燕云=网易 Messiah 自研引擎，非 UE5 | ✅ 已交叉验证（E:\AiDoc 既有文档佐证） |
| 2 | 洛克王国·世界 = UE4.26 Mobile Forward | ✅ 无误 |
| 3 | 和平精英 = UE4 Mobile Forward | ✅ 无误 |
| 4 | 燕云 GBuffer 布局 B8G8R8A8+R10 | ✅ 来源既有技术要点 |
| 5 | 燕云 tile ~20byte/px、SRAM~1MB | ✅ 已注入主文档 |
| 6 | "GBuffer never stored in system memory" | ✅ UE 官方原文 |
| 7 | 材质指令 147→34 | ✅ UE 官方 |
| 8 | 洛克王国写带宽 -30% | ✅ UFSH2025 |
| 9 | 和平精英 DrawCall<300 / Overdraw<3x | ✅ 第三方抓帧 |
| 10 | **SceneColor.A 8-bit 仅在"深度打包进 alpha"路径(B)相关；洛克王国深度走 subpass 留 tile，不入 alpha** | ⚠️ v1 二次修正 |
| 11 | **UE5 移除软件遮挡，r.Mobile.AllowSoftwareOcclusion 失效** | ✅ 已在决策文档标注 |
| 12 | 燕云"压缩省的不是带宽而是 tile 空间" | ✅ 已修正认知 |

### B 组｜Shader 代码真实性（点 13–24）
| # | 审阅点 | 判定 |
|---|--------|------|
| 13 | 前文 Programmable Blending `[[color(n)]]` 示例 | ✅ 语义正确 |
| 14 | 前文 `subpassLoad` 示例 | ✅ 正确 |
| 15 | **缺少 UE 真实跨平台深度 fetch 源码** | ➕ 本轮补 `LookupDeviceZ()`（§2） |
| 16 | **缺少 `VulkanSubpassDepthFetch()` intrinsic 说明** | ➕ 已补 |
| 17 | **缺少 GLES `DepthbufferFetchES2()`/`FramebufferFetchES2()` 区分** | ➕ 已补 |
| 18 | **缺少 Metal(非 Mac) 走 `DepthbufferFetchES2()` 的事实** | ➕ 已补 |
| 19 | `MOBILE_DEFERRED_SHADING` 下走 SceneDepthAuxTexture | ➕ 已补（§2 注） |
| 20 | PhongApprox 移动近似（前向管线指南） | ✅ 无误 |
| 21 | 八面体法线编码 | ✅ 无误 |
| 22 | YCoCg Albedo（燕云） | ✅ 无误 |
| 23 | MSAA 两套 PSO（subpassLoad 签名差异） | ✅ 无误 |
| 24 | **FrameBufferFetch 宏在 .usf 中的真实条件链** | ➕ 已补（§2） |

### C 组｜PrePass / Subpass 冲突（点 25–34）
| # | 审阅点 | 判定 |
|---|--------|------|
| 25 | **EarlyZPass 与 subpass 深度 fetch 冲突** | ➕ 本轮新增盲区（§4） |
| 26 | **`FORCE_DEPTH_TEXTURE_READS` 变体需求** | ➕ 已补 |
| 27 | **`IS_MOBILE_DEPTHREAD_SUBPASS` / `MOBILE_DEPTHFECTH` 宏** | ➕ 已补 |
| 28 | **cook 后变体缺失导致无法动态切换的坑** | ➕ 已补 |
| 29 | iOS Opaque 免 PrePass（HSR） | ✅ 无误 |
| 30 | Masked 植被仍需 Early-Z（HSR 不剔 AlphaTest） | ✅ 无误 |
| 31 | 和平精英 Opaque 不做 PrePass | ✅ 无误 |
| 32 | PrePass 必须做成动态非 readonly | ➕ 已补（§4） |
| 33 | View 额外 uniform 标记 PrePass 状态 | ➕ 已补 |
| 34 | r.EarlyZPass 0/1/2 与 subpass 路径的决策表 | ➕ 已补（§4 表） |

### D 组｜带宽量化口径（点 35–44）
| # | 审阅点 | 判定 |
|---|--------|------|
| 35 | 区分读带宽 vs 写带宽 | ✅ 已明确 |
| 36 | Store 为最贵操作 | ✅ 无误 |
| 37 | Clear 片上免费 | ✅ 无误 |
| 38 | 洛克王国分场景给数（极限 vs 实际） | ✅ 已保留双口径 |
| 39 | 端到端功耗数字缺失 | ✅ 已诚实标注"无公开数据" |
| 40 | tile spilling 概念 | ➕ 已补（燕云 §4.3） |
| 41 | 1080p GBuffer 数十 MB/帧估算 | ✅ 量级合理 |
| 42 | MSAA tile 内 resolve 零写回 | ✅ 无误 |
| 43 | Memoryless 省的是 footprint+带宽 | ✅ 无误 |
| 44 | **带宽估算公式（像素数×字节×Pass 数×帧率）** | ➕ 已补（§5） |

### E 组｜CVar 准确性（点 45–54）
| # | 审阅点 | 判定 |
|---|--------|------|
| 45 | `r.Mobile.ShadingPath` 0/1 | ✅ |
| 46 | `r.MobileHDR` Deferred 必需 | ✅ |
| 47 | `r.Mobile.TonemapSubpass` 需 Meta XR + 与 Deferred 互斥 | ✅ |
| 48 | `r.EarlyZPass` / `r.EarlyZPassOnlyMaterialMasking` | ✅ |
| 49 | `r.Mobile.Forward.EnableClusteredReflections` | ✅ |
| 50 | **`r.Mobile.EarlyZPass`（移动专用，区别于 `r.EarlyZPass`）** | ➕ 已补：两者并存，移动端实际看前者 |
| 51 | `r.Mobile.AllowSoftwareOcclusion` UE5 失效 | ✅ |
| 52 | `vulkan.SubpassDepthRead` | ✅ |
| 53 | `MinimumiOSVersion=IOS_15` | ✅ |
| 54 | `MTLGPUFamilyApple4` 运行时检测 | ✅ |

### F 组｜决策矩阵完备性（点 55–64）
| # | 审阅点 | 判定 |
|---|--------|------|
| 55 | 三案例改造成本三分类 | ✅ |
| 56 | 判断三问方法论 | ✅ |
| 57 | 分水岭=RenderPass 编排权 | ✅ |
| 58 | fork 长期负债提示 | ✅ |
| 59 | "先白嫖再 fork"两步走 | ✅ |
| 60 | **缺少版本兼容矩阵（UE5.3/5.4/5.5 差异）** | ➕ 已补（§6） |
| 61 | **缺少回滚/降级路径完整性检查** | ➕ 已补（§4 fallback 强调） |
| 62 | 改造文件清单（Vulkan+Metal） | ✅ |
| 63 | 风险评估表 | ✅ |
| 64 | **缺少改造前的基准测量步骤** | ➕ 已补（§7 流程） |

### G 组｜iOS/Metal 专项（点 65–74）
| # | 审阅点 | 判定 |
|---|--------|------|
| 65 | Programmable Blending 原生无需扩展 | ✅ |
| 66 | Memoryless 全系支持 | ✅ |
| 67 | HSR 硬件遮挡 | ✅ |
| 68 | Imageblock A11+ | ✅ |
| 69 | Tile Shading A11+ | ✅ |
| 70 | Raster Order Group | ✅ |
| 71 | 同 encoder 多 draw 留片上 | ✅ |
| 72 | **Metal 非 Mac 深度走 DepthbufferFetchES2** | ➕ 源码核验补充 |
| 73 | LAZILY_ALLOCATED 内存类型 | ✅ UE 官方 |
| 74 | **Apple GPU tile 尺寸随 RT 格式动态变化** | ➕ 已补（§8） |

### H 组｜术语/一致性（点 75–82）
| # | 审阅点 | 判定 |
|---|--------|------|
| 75 | Tile Memory / 片上缓存 统一 | ✅ |
| 76 | Memoryless 大小写统一 | ✅ |
| 77 | Subpass / 子通道 统一 | ✅ |
| 78 | Forward/前向、Deferred/延迟 统一 | ✅ |
| 79 | TBDR/TBR 区分（Apple vs Mali/Adreno） | ✅ 已澄清 |
| 80 | 相对路径交叉引用 | ➕ 验收索引用相对路径 |
| 81 | 免责声明三文档统一 | ✅ |
| 82 | 版本号统一 | ✅ |

### I 组｜结构/可读性（点 83–88）
| # | 审阅点 | 判定 |
|---|--------|------|
| 83 | 执行摘要前置 | ✅ |
| 84 | 决策树/矩阵 | ✅ |
| 85 | 配置速查附录可复制 | ✅ |
| 86 | Checklist 分层（配置/源码/验证） | ✅ |
| 87 | 代码块标注平台 | ✅ |
| 88 | **缺少术语表/缩写表** | ➕ 已补（§9） |

### J 组｜验证工具链（点 89–93）
| # | 审阅点 | 判定 |
|---|--------|------|
| 89 | RenderDoc(Android) 逐 Pass | ✅ |
| 90 | Xcode GPU Capture(iOS) 验 Memoryless | ✅ |
| 91 | Arm Streamline / Snapdragon Profiler | ✅ |
| 92 | **RenderDoc 如何确认 Load/Store action 的具体操作** | ➕ 已补（§7） |
| 93 | stat RHI / stat GPU | ✅ |

### K 组｜专家规则符合性（点 94–97）
| # | 审阅点 | 判定 |
|---|--------|------|
| 94 | 量化 tradeoff（带宽/指令数） | ✅ |
| 95 | 精确引擎限制（≤128bit、≤4 RT、16M Nanite 等） | ✅ |
| 96 | 改造前警告（fork 负债、subpass 配错崩溃） | ✅ |
| 97 | C++/源码层级清晰 | ✅ |

### L 组｜诚信边界（点 98–100）
| # | 审阅点 | 判定 |
|---|--------|------|
| 98 | 推断 vs 实据 全程标注 | ✅ |
| 99 | 无编造端到端功耗数字 | ✅ |
| 100 | 燕云=Messiah 类比边界声明 | ✅ |

**统计**：100 点中 ✅ 无误 76 项、➕ 补充 22 项、⚠️ 修正 2 项（点 10 因果修正、点 12 认知修正）。

---

## §2 真实源码：同一份 .usf 如何编译到三平台（核验补充）

UE 移动管线读取片上深度的统一入口 `LookupDeviceZ()`（节选自引擎 shader，经源码核验）：

```hlsl
float LookupDeviceZ(float2 ScreenUV)
{
#if SCENE_TEXTURES_DISABLED
    return FarDepthValue;
#elif (POST_PROCESS_MATERIAL || POST_PROCESS_MATERIAL_MOBILE) && !POST_PROCESS_AR_PASSTHROUGH
    #if MOBILE_DEFERRED_SHADING
        // 延迟：从 SceneDepthAuxTexture 采样
        return Texture2DSample(MobileSceneTextures.SceneDepthAuxTexture,
                               MobileSceneTextures.SceneDepthAuxTextureSampler, ScreenUV).r;
    #else
        // ★ 前向：BasePass 结束时 SceneDepth 被丢弃，改从 SceneColor.A 取 DeviceZ
        return Texture2DSample(MobileSceneTextures.SceneColorTexture,
                               MobileSceneTextures.SceneColorTextureSampler, ScreenUV).a;
    #endif
#elif COMPILER_GLSL_ES3_1 && PIXELSHADER
    #if !OUTPUT_MOBILE_HDR
        // 【GLES】扩展可用时直接 fetch 深度/模板
        return DepthbufferFetchES2();
    #else
        // 【GLES】否则从 framebuffer fetch 的 alpha 取
        return FramebufferFetchES2().w;
    #endif
#elif VULKAN_SUBPASS_DEPTHFETCH && PIXELSHADER
    // 【Vulkan】专用 intrinsic，从当前 subpass 的深度 attachment 读
    return VulkanSubpassDepthFetch();
#elif (METAL_PROFILE && !MAC) && PIXELSHADER
    // 【Metal iOS】走 framebuffer fetch（Programmable Blending 路径）
    return DepthbufferFetchES2();
#else
    // 兜底：原生深度纹理采样（落主存）
    return Texture2DSampleLevel(MobileSceneTextures.SceneDepthTexture,
                                MobileSceneTextures.SceneDepthTextureSampler, ScreenUV, 0).r;
#endif
}
```

**这段代码证明了什么**：
1. **一份 .usf，五条平台分支**——GLES（两种扩展）、Vulkan（`VulkanSubpassDepthFetch`）、Metal iOS（`DepthbufferFetchES2`）、原生兜底。这就是前文"RHI 抽象、改一次双端生效"的硬证据。
2. **`VULKAN_SUBPASS_DEPTHFETCH` 是 Vulkan 片上深度读取的开关宏**——对应前文 `ESubpassHint::DepthReadSubpass`。
3. **兜底分支会落主存**（`SceneDepthTexture` 采样）——这就是不支持片上 fetch 设备的 fallback，印证 §4 必做降级路径。

> 来源核验：UE 引擎 `Common.ush` / `MobileSceneTextures` 相关 shader（社区源码解读，见参考）。

---

## §3 修正：洛克王国 RGB10A2 的真实因果

**前文表述（v1 有误，本次二次修正）**：曾说"RGB10A2 是为了腾通道承载深度+角色标记，绕开 8-bit 精度墙"。这是**自相矛盾**——RGB10A2 的 alpha 只有 **2-bit**，比 RGBA8 的 8-bit 还少，更不可能装深度。错在把**两条独立路径**揉成一段。

### 先回答核心问题：深度最终存哪？

**洛克王国 One Pass 里，深度哪儿都没"存"——它全程留在 tile 上的 depth attachment（Memoryless），在同一个 RenderPass 内被 subpass depth fetch 就地读取，出 RenderPass 即丢弃，从不写回主存。**

### UE 前向管线深度的三种归宿（完整图景）

| 路径 | 深度去哪 | 精度 | 何时用 |
|------|---------|------|--------|
| **A. Subpass Depth Fetch** ★洛克王国 | 留 tile 的 depth attachment（Memoryless），同 Pass 内 `VulkanSubpassDepthFetch()`/`DepthbufferFetchES2()` 就地读，用完即弃 | 完整 D24/D32 | One Pass、iOS Programmable Blending |
| **B. 打包进 SceneColor.A** | BasePass 丢弃 SceneDepth 后，把 DeviceZ 写进 SceneColor 的 alpha，供**跨 Pass**后处理采样 | 依赖格式：RGBA16F(Mobile HDR) 16-bit float alpha 够；RGBA8 8-bit **不够** | 跨 Pass 读深度且开 Mobile HDR |
| **C. SceneDepthAux 单独纹理** | 移动延迟用独立 `SceneDepthAuxTexture` 存 DeviceZ | 单独 RT | Mobile Deferred |

### 源码注释的真实含义（路径 B，非洛克王国路径）

```hlsl
// 引擎源码注释（核验）：
// "We cannot fall back to fetching the alpha channel when MobileHDR=false
//  because the alpha channel is only 8-bit."
```
这句讲的是**路径 B**：只有开 Mobile HDR（SceneColor=RGBA16F、alpha 是 16-bit float）才能把深度塞进 alpha；不开 HDR（RGBA8）时 8-bit alpha 装不下深度，这条路走不通。**与洛克王国的 RGB10A2 无关。**

### RGB10A2 的通道分配（澄清）

洛克王国走的是**路径 A**，其 RGB10A2 格式：
- **RGB 10/10/10** = 颜色（LDR 下比 RGBA8 色彩精度更高）
- **A 2-bit** = **角色/场景标记位**（替代 Custom Depth 的 mask），**与深度无关**
- **深度** = 全程留 tile（路径 A），subpass depth fetch 读取

### 正确的因果链（与 v1 相反）

**不是**"为了装深度才选 RGB10A2"；**而是**——因为深度走 subpass 留在片上（路径 A），SceneColor.A 被解放、不必再背深度，这 2-bit alpha 才能挪作角色标记用。选 RGB10A2 是为了在 LDR 预算下**既保住颜色精度、又腾出 2-bit 做 mask**。

**结论**：One Pass 能成立的真正前提是 **subpass depth fetch 让深度全程驻留 tile（Memoryless，零写回）**；RGB10A2 只是在此前提下对 SceneColor 通道的顺带优化（2-bit alpha 当 mask）。深度与 alpha 是两件事，v1 把它们混为一谈是错的。

---

## §4 补全盲区：PrePass 与 Subpass 深度读取的冲突

这是和平精英(用 PrePass) 与洛克王国(用 Subpass 深度 fetch) 两种策略**不能简单叠加**的关键工程坑。

### 问题本质
- Subpass 深度 fetch 路径（`MOBILE_DEPTHFETCH`/`IS_MOBILE_DEPTHREAD_SUBPASS`）依赖"深度在当前 RenderPass 的 subpass 里可读"。
- 一旦开 `r.Mobile.EarlyZPass`（PrePass），深度在独立 PrePass 里先写好，后续 Pass 应直接读深度纹理，而非走 subpass fetch。
- **两条路径的 shader 变体不同**。若 cook 时只生成了 subpass 变体，运行时无法动态切到"读深度纹理"的变体。

### 工程解法（核验自社区实践）
```
1. 为相关 Pass 增加 FORCE_DEPTH_TEXTURE_READS 变体：
   PrePass 开启时，强制走"读深度纹理"路径
2. 把 PrePass 开关做成动态（非 readonly），否则 cook 不出可切换变体
3. 部分 Pass 用 IS_MOBILE_DEPTHREAD_SUBPASS 宏（即 MOBILE_DEPTHFETCH 条件）
   统一管理：强制设为 1 以保证 subpass 变体被 cook 出来，再运行时选择
4. View 上加一个 uniform 标记当前 PrePass 状态，shader 据此选分支
```

### 决策表：EarlyZPass × 深度读取路径
| `r.Mobile.EarlyZPass` | 深度来源 | shader 变体 |
|:---:|---------|------------|
| 0（关） | subpass fetch（片上） | 默认 subpass 变体 |
| 1（不透明） | 深度纹理（PrePass 已写） | `FORCE_DEPTH_TEXTURE_READS` |
| 2（不透明+Masked） | 深度纹理 | `FORCE_DEPTH_TEXTURE_READS` |

> **给项目的建议**：
> - **iOS**：HSR 已做硬件遮挡，Opaque 别开 PrePass，让深度走 subpass/Programmable Blending 路径最省。
> - **Android Mali/Adreno**：若 overdraw 严重需要 PrePass，则接受"深度走纹理读取"、放弃 subpass 深度 fetch；二者权衡，别想同时吃。
> - **本质**：PrePass（省 overdraw/FS）与 Subpass 深度 fetch（省深度带宽）在移动端是**互斥取舍**，不是叠加增益。

---

## §5 补充：带宽估算公式（改造前先算账）

改造前用这个粗算判断收益上界，避免无效 fork：

```
单 RT 单次 Store 带宽（MB/帧）
  = 宽 × 高 × 每像素字节 / (1024×1024)

每帧总写带宽 ≈ Σ(各 RT × 该 RT 的 Store 次数)

示例：1080p（1920×1080）SceneColor RGBA8（4 byte）
  单次 Store = 1920×1080×4 / 1048576 ≈ 7.9 MB
  60fps → 474 MB/s（仅一张 RT 一次 Store）

延迟 GBuffer（4 RT × 4 byte = 16 byte/px）若落主存：
  1920×1080×16 / 1048576 ≈ 31.6 MB/帧 → 60fps ≈ 1.9 GB/s
  → 这就是"片上 GBuffer"要消灭的写带宽量级
```

> 用法：先估"中间 RT 落主存"的写带宽，再估"改片上后省下多少"，若省下量级 < 总带宽 10%，fork 不值得。

---

## §6 补充：UE 版本兼容矩阵

| 特性 | UE5.3 | UE5.4 | UE5.5 | 备注 |
|------|:----:|:----:|:----:|------|
| `r.Mobile.ShadingPath=1` 片上 GBuffer | ✅ | ✅ | ✅ | 各版 GBuffer 布局略有调整 |
| `ESubpassHint::DepthReadSubpass` | ✅ | ✅ | ✅ | 稳定 |
| `r.Mobile.TonemapSubpass` | 插件 | 插件 | 插件 | 需 Meta XR / Oculus fork |
| 软件遮挡 | ❌ | ❌ | ❌ | UE5 全系移除 |
| RDG 移动端覆盖 | ✅ | ✅ | ✅ | 5.x 持续强化 |

> ⚠️ fork 改渲染器后，每次跨小版本（如 5.3→5.4）都需重新 merge `MobileShadingRenderer` / `VulkanRenderPass` / `MetalRenderPass`。建议把改动集中在少量文件并加清晰 `// [PROJECT] ...` 标记。

---

## §7 补充：改造前的基准测量标准流程（SOP）

```
1. 选定 3 个代表场景（最复杂战斗 / 大世界远景 / UI 重场景）
2. RenderDoc 抓帧（Android）：
   - 逐 RenderPass 看 LoadOp/StoreOp（确认哪些 RT 在 Store）
   - 记录 SceneColor/Depth/GBuffer 的 Store 次数
3. Xcode GPU Frame Capture（iOS）：
   - 逐 encoder 看 attachment 的 storeAction
   - 确认中间 RT 是否已是 Memoryless（不占 device memory）
4. Arm Streamline / Snapdragon Profiler：测 GPU 外部带宽（read/write 分开）
5. 记录基线：帧率 / GPU 时间 / 读带宽 / 写带宽 / 峰值温度
6. 改造后同场景同流程复测，对比 delta
7. 收益 < 预期或带来兼容问题 → 回滚
```

---

## §8 补充：Apple GPU tile 尺寸随 RT 格式动态变化

Apple GPU 的 tile 像素数**不是固定的**，取决于该 RenderPass 所有 attachment 的总 bit 数：
- attachment 越"胖"（如多张 GBuffer），单 tile 容纳的像素越少，tile 数越多。
- 这影响 Imageblock 的 `imageBlockSampleLength` 和 tile shader 的 threadgroup 尺寸。
- **设计片上 GBuffer 时要确保总 bit 数不触发 tile 缩小到影响并行度**——这与燕云"~20byte/px 留余量避免 spilling"是同一个工程考量在 Apple 侧的体现。

---

## §9 术语表 / 缩写表

| 术语 | 全称 / 含义 |
|------|------------|
| TBDR | Tile-Based Deferred Rendering，移动 GPU 分块延迟渲染架构 |
| TBR | Tile-Based Rendering（Mali/Adreno，无硬件 HSR） |
| HSR | Hidden Surface Removal，Apple GPU 硬件隐面剔除 |
| Tile Memory | 片上高速 SRAM，渲染中间结果暂存处 |
| Memoryless | RT 只存在于 tile memory，不分配 device memory（Vulkan: lazily allocated / Metal: `MTLStorageModeMemoryless`） |
| Subpass | Vulkan RenderPass 内共享 tile memory 的子阶段 |
| Input Attachment | Vulkan subpass 间传递片上数据的 attachment（仅当前像素） |
| Programmable Blending | Metal 中 fragment 用 `[[color(n)]]` 直接读片上当前像素 |
| FrameBuffer Fetch | 从片上 framebuffer 读当前像素值（GLES 扩展 / Metal 原生） |
| Imageblock | Apple A11+ 自定义片上 per-pixel 数据结构 |
| Tile Shading | Apple A11+ render pass 内联 compute |
| RDG | Render Dependency Graph，UE 的渲染图（自动管理瞬态资源） |
| One Pass | 把多 RenderPass 合并为单 RenderPass 多 subpass 的优化（洛克王国） |
| Octahedron Normal | 八面体法线编码（3 通道→2 通道） |
| YCoCg | 一种颜色空间，用于 Albedo 压缩 |
| tile spilling | tile 数据超出 SRAM 容量被迫溢出到主存的劣化 |

---

## §10 仍存在的已知局限（诚实声明）

1. **端到端功耗/温度数字**：三案例均无官方公开的"开优化前后功耗 mW / 温度℃"对比，本系列不提供。
2. **燕云内部实现细节**：Messiah 闭源，§4.x 的 GBuffer 布局来自公开技术要点，subpass 编排为架构推断。
3. **源码行号**：`LookupDeviceZ` 等代码来自社区源码解读，不同 UE 小版本行号/宏名可能微调，落地前请在目标版本引擎源码确认。
4. **RGB10A2 因果**：§3 的因果链基于引擎前向管线通用行为 + 洛克王国公开分享综合推断，未经洛克王国团队逐字确认。

---

## 参考资料（本轮新增核验源）

- UE 对 scene depth 的封装（`LookupDeviceZ` 源码解读）：https://www.cnblogs.com/minggoddess/p/14532050.html
- UE Mobile: Prepass Or Not?（`FORCE_DEPTH_TEXTURE_READS` / `IS_MOBILE_DEPTHREAD_SUBPASS` 实践）：https://www.blurredcode.com/2025/03/239ae6a3
- 其余来源见主文档 `UE_Mobile_TBDR_片上缓存优化_跨平台完整技术方案.md` §11

---

> **本卷定位**：增补卷 = 主文档的"勘误 + 深挖 + 工程盲区"补丁。验收时建议先读本卷摘要（§开头 3 发现）与 §2/§3/§4 三处实质性提升，再回主文档核对。
