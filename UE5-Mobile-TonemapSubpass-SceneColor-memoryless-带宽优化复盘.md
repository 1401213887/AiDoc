# UE5 Mobile TonemapSubpass — SceneColor Memoryless 带宽优化复盘

> 一句话：`r.Mobile.TonemapSubpass=1` 时，inline tonemap subpass 通过 `SubpassLoad` 在 tile 内读 SceneColor 并丢弃，是 TBDR 上省带宽的关键路径；但官方默认 1x SceneColor 仍写回 DRAM（Store）。我们补齐了「SceneColor 走 memoryless（storeOp DONT_CARE）」的能力，新增 CVar 门控，并用单一判定源修复了捕获视图/预览回归等正确性缺陷。本文汇总达成"杜绝 store、兑现带宽收益"的全部努力与最终落地。

---

## 一、背景：inline tonemap subpass 的带宽原理

Mobile（ES3_1）在支持 subpass 的平台（Vulkan / framebuffer fetch）上，`r.Mobile.TonemapSubpass=1` 让 tonemap 成为 base pass 的**下一个 subpass**，而非独立 render pass：

- base pass 把 SceneColor 写进 tile（片上内存）；
- tonemap 通过 `SubpassLoad` 在同一 render pass 内读 SceneColor，直接写 backbuffer；
- **SceneColor 不需要离开 tile 写回 DRAM**。

**省下的带宽**：
1. SceneColor 全屏一次写回 DRAM + 之后再读回（传统独立 pass 两笔全屏往返）。
2. 由于没有中间后处理，`SceneDepthAux` 也一并省掉（`bRequiresSceneDepthAux = MobileRequiresSceneDepthAux(...) && !bTonemapSubpass`）。

**但官方没有做彻底**：`MobileShadingRenderer.cpp:788` 只给 SceneColor 加了 `TexCreate_InputAttachmentRead`（供 `SubpassLoad`），**没有加 `TexCreate_Memoryless`**——所以 SceneColor 在 render pass 结束时仍 `storeOp=STORE` 写回 DRAM。官方保守，留给了我们补齐。

**核心机制链**：

```
TexCreate_Memoryless
  → RDG RenderGraphPass.cpp:52   StoreAction = ENoAction
  → Vulkan RenderTarget.cpp      VK_ATTACHMENT_STORE_OP_DONT_CARE
  → 内容不落 DRAM（TBDR tile 结束后丢弃）
```

## 二、问题定位流程（三条线）

### 线 A：开启后真机 viewport 变小 → 配置规避（不改引擎）
- 现象：`r.Mobile.TonemapSubpass=1` 后画面缩到左上角。
- 根因：tonemap PS 的 viewport 用 SceneColor 分配 **Extent** 而非 **ViewRect**，`ScreenPercentage<100` 时不放大（`PostProcessTonemap.cpp:1207/1255/1257`）。
- 决策：**不改引擎**，配置规避：`r.ScreenPercentage=100` + `r.MobileContentScaleFactor=0.7`（降分辨率改由 CSF 承担）。
- 详见：`E:\AiDoc\UE-Mobile-TonemapSubpass-真机viewport变小-ScreenPercentage不匹配根因与规避方案.md`

### 线 B：截帧看 DS=Store（Depth/SceneColor 都在 Store）→ 根因是 HZBOcclusion
- 现象：开启后截帧 Color/DS 都是 Store，与"subpass 应省带宽"直觉矛盾。
- 排查：给 `bKeepDepthContent`（决定 depth 是否 memoryless）加了 13 条件诊断日志，实测**唯一 true 项是 `bHZBOcclusion`**。
- 根因：`r.HZBOcclusion=1`（ini 默认）且 `r.Mobile.AllowSoftwareOcclusion=0` → `bHZBOcclusion=true` → depth 不能 memoryless → DS=Store。
- 修复（配置层，无代码）：dpcvars 加 `r.HZBOcclusion=0`。⚠️ 注意区分三个独立 CVar：`r.HZBOcclusion`（HZB 遮挡查询）、`r.InstanceCulling.OcclusionCull`、`r.AllowOcclusionQueries`——之前关的后两个不是这个开关。

### 线 C：SceneColor 不 DONT_CARE → 补齐 memoryless 能力（本主题核心）
- 引擎只为 MSAA（`NumSamples>1 && bMemorylessMSAA`）授予 SceneColor memoryless，1x 无路径。
- 目标：`r.Mobile.TonemapSubpass=1` 时 1x SceneColor 也 memoryless。

## 三、根因与设计约束

### 3.1 为什么不能简单加一行 `|= TexCreate_Memoryless`
SceneColor 的 memoryless 资格**随视图而变**：

| 视图 | inline subpass? | SceneColor 命运 | 应 memoryless? |
|---|---|---|---|
| 主视图（Forward, `bResolveScene`） | 是 | base 写 → tonemap SubpassLoad → 弃 | ✅ |
| 反射捕获 / 平面反射 / 场景捕获（`SetResolveScene(false)`） | **否** | 之后要采样拷贝 | ❌ |
| 编辑器预览（模拟平台） | 是但 RHI 可能分 pass | 内容可能被丢弃 | ❌ |
| Deferred（`IsMobileDeferredShadingEnabled`） | **否**（引擎排除） | — | ❌ |

### 3.2 权威谓词 vs 弱谓词（关键 bug 源）
- **弱谓词**（config 侧）：`IsMobileTonemapSubpassEnabledInline(ShaderPlatform, bRequireMultiView, NumMSAASamples)`（`SceneUtils.cpp:100`）——只看平台支不支持。
- **权威谓词**（渲染器侧）：`bTonemapSubpassInline = IsMobileTonemapSubpassEnabledInline(...) && ViewFamily.bResolveScene && GetRendererOutput() != DepthPrepassOnly`（`MobileShadingRenderer.cpp:407-408`）。

弱谓词对捕获视图也返回 true（不看 `bResolveScene`）→ 若按弱谓词授予 memoryless，**捕获视图 SceneColor 被 DONT_CARE → 黑图**。

### 3.3 Deferred 天然排除
`IsMobileTonemapSubpassEnabled`（`SceneUtils.cpp:97`）显式 `&& !IsMobileDeferredShadingEnabled(Platform)`。inline tonemap 需要 tonemap 紧跟 SceneColor 写入的 subpass，Deferred 的 SceneColor 在 lighting 后才成型且被后处理消费，结构上不成立。**因此本方案天然 Forward 专属，Deferred 零影响。**

## 四、最终方案（v2 更简版 + CVar 门控）

### 4.1 设计：判定单点 + 数据契约 + 加而不是减

```
渲染器 InitViews（唯一决策点）
  bTonemapSubpassInline（权威）→ 下推 bCustomResolveSubpass
  → bSceneColorInlineMemoryless = bTonemapSubpassInline && CVar && !IsSimulatedPlatform
        ↓ 字段（数据契约）
SceneTextures.cpp（唯一消费点）
  SceneColor 专属 desc 加 TexCreate_Memoryless（只影响 SceneColor）
```

### 4.2 新增 CVar

```cpp
// MobileShadingRenderer.cpp
#pragma region Engine ZXB
static TAutoConsoleVariable<int32> CVarMobileTonemapSubpassDiscardColor(
    TEXT("r.Mobile.TonemapSubpass.DiscardColor"),
    0,
    TEXT("When 1 with r.Mobile.TonemapSubpass, allocate SceneColor as memoryless (storeOp DONT_CARE): the inline tonemap\n")
    TEXT("subpass consumes it via SubpassLoad then discards it. Defaults to 0 to keep engine behavior."),
    ECVF_RenderThreadSafe);
#pragma endregion
```

**默认 0** → 不开启时引擎行为完全不变（SceneColor 仍 Store）。开启且 inline tonemap 生效时才授予。

### 4.3 渲染器判定（InitViews）

```cpp
SceneTexturesConfig.ExtraSceneColorCreateFlags |= (bTonemapSubpassInline ? TexCreate_InputAttachmentRead : TexCreate_None);
#pragma region Engine ZXB
// [ZXB] Push the authoritative inline-subpass predicate down (IsMobileTonemapSubpassEnabledInline alone also fires
// for capture views) and resolve SceneColor memoryless from it, gated by r.Mobile.TonemapSubpass.DiscardColor.
SceneTexturesConfig.bCustomResolveSubpass = bTonemapSubpassInline;
SceneTexturesConfig.bSceneColorInlineMemoryless = bTonemapSubpassInline && CVarMobileTonemapSubpassDiscardColor.GetValueOnRenderThread() != 0 && !IsSimulatedPlatform(ShaderPlatform);
#pragma endregion
SceneTexturesConfig.BuildSceneColorAndDepthFlags();
```

### 4.4 SceneTextures.cpp 授予（加而不是减）

```cpp
#pragma region Engine ZXB
// [ZXB] Inline tonemap subpass reads SceneColor via SubpassLoad then discards it, so it can stay memoryless
// (r.Mobile.TonemapSubpass.DiscardColor); grant it on SceneColor's own desc only, never on the shared Desc.
FRDGTextureDesc SceneColorDesc = Desc;
if (Config.bSceneColorInlineMemoryless)
{
    SceneColorDesc.Flags |= TexCreate_Memoryless;
}
#pragma endregion
// SceneColorCopy / MobileOutlineTexture 等沿用共用 Desc（天然干净 / 保留引擎 MSAA memoryless）
```

**关键取舍：加而不是减**——`Desc` 是共用的（SceneColorCopy、MobileOutlineTexture、MobileCharFeatureTexture…都要用它建），若从 `Desc` 上"剥"memoryless 会误伤引擎原有的 MSAA memoryless；改成给 SceneColor **专属 desc 加**，只影响 SceneColor 自己，derived 纹理零传染。

### 4.5 落地文件清单

| 文件 | 改动 |
|---|---|
| `Engine/Public/SceneTexturesConfig.h` | 新字段 `bSceneColorInlineMemoryless : 1` + ctor 初始化（region 包裹） |
| `Renderer/Private/MobileShadingRenderer.cpp` | CVar + 下推 `bCustomResolveSubpass` + 设字段（region 包裹） |
| `Renderer/Private/SceneTextures.cpp` | SceneColor 专属 desc 条件加 memoryless（region 包裹） |
| `Engine/Private/SceneTexturesConfig.cpp` | **零改动**（判定收敛到渲染器后完全恢复引擎原样） |

## 五、code-review（max 档）发现并修复的 3 个真 bug

1. **谓词过弱 → 捕获视图黑图（关键）**：最初把 memoryless 授予放在 config 层（`IsMobileTonemapSubpassEnabledInline` 弱谓词），反射捕获/平面反射/场景捕获（`bResolveScene=false`）被错误 memoryless → DONT_CARE → 黑图。修复：渲染器下推权威 `bTonemapSubpassInline`。
2. **预览回归**：SceneTextures.cpp 若裸读 `Config.bCustomResolveSubpass` 会漏掉 `!IsSimulatedPlatform`，编辑器预览 + MSAA 下误剥引擎 MSAA memoryless。修复：读解析字段（天然带 IsSimulatedPlatform 门控）。
3. **死代码 + 误导注释**：`SceneColorCreateFlags &= ~TexCreate_UAV` 永不触发（UAV 只在 `FeatureLevel>=SM5` 加，inline 只在 Mobile<SM5，互斥），且注释编造了不存在的约束。删除。

另：`bCustomResolveSubpass` 下推还顺带修正了 decals / GBuffer RT info 对捕获视图的 resolve attachment 误判（同一源 bug 的另一出口）。

## 六、快速排查 Checklist

1. **开启收益确认**：`r.Mobile.TonemapSubpass=1 r.ScreenPercentage=100 r.MobileContentScaleFactor=0.7 r.Mobile.TonemapSubpass.DiscardColor=1`，截帧看 SceneColor attachment 的 storeOp 应为 `DONT_CARE`。
2. **Store 了？** 先查 depth（`bKeepDepthContent`）再查 color：depth 看 `r.HZBOcclusion=0`；color 看 CVar 是否开启 + `bTonemapSubpassInline` 是否成立（真机、非模拟、`bResolveScene`）。
3. **只影响 Forward**：Deferred 下 `IsMobileTonemapSubpassEnabled` 恒 false，CVar 静默无效，属预期。
4. **改引擎代码后**：所有改动点必须在 `#pragma region Engine ZXB` 内（含 CVar 声明、ctor 单行、函数签名），逐条对照 `p4 diff` 的 `+` 行自查。
5. **未验证项**：编辑器重编 + 真机（Adreno）验证反射/捕获不再黑图、CVar=0 时行为不变。

## 七、相关参考

- `E:\AiDoc\UE-Mobile-TonemapSubpass-真机viewport变小-ScreenPercentage不匹配根因与规避方案.md`
- 关键代码：`MobileShadingRenderer.cpp:407-409/788/793`、`SceneUtils.cpp:94-104`、`SceneTextures.cpp:607-621`、`SceneTexturesConfig.h:294-300`
- 机制链：`RenderGraphPass.cpp:52`（StoreAction）、`VulkanRenderTarget.cpp`（storeOp）
- 相关 memory：`bKeepDepthContent` 13 条件（`MobileShadingRenderer.cpp:747-760`）、`r.HZBOcclusion` 与遮挡剔除三 CVar 区别
