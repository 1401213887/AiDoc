# NeoRing 客户端 CPU 优化项全量报告（生效 + 不生效）

> 日期：2026-10-23
> 包体：`S1Game_release_0.1183280..._Test_Win64`（CL 1183280，Test）；Development 数据单独标注
> 用例：`nr_sfxai`（NeoRing 特效用例，单局，同轮 A/B：273 s 控制台切换 + 正/反两轮）
> 口径：**每帧 CPU = total_cpu_pct × 8 × duration_s × 10 / frames（ms）**；系统 CPU% 为进程总 CPU
> 排除项（既定）：物理 / Chaos、动画、音频

---

## 0. 结论速览

| 类别 | 数量 | 结果 |
|---|---|---|
| 生效（已写入配置 / 代码） | **20 项**（配置 14 + 代码 2 + Development 专用 4） | 合并后每帧 CPU **-10.5%**、100 帧上限下 FPS **+9.3%** |
| 不生效（测试后拒绝） | **22 项** | 调度类均为"工作跨线程迁移"；质量类目标模块无可测下降；VSM/Lumen 只影响 GPU |
| 评估后关闭（代码级） | **3 项** | 无空转可删 / 已由上游实现 / 无重复查找 |

**最终合并收益**（同轮 A/B，正反两轮）：

| 口径 | 基线 | 优化后 | 收益 |
|---|---|---|---|
| 每帧 CPU（100 帧口径） | 53.59 ms | 47.97 ms | **-5.62 ms（-10.5%）** |
| 系统 CPU @60 帧（两边满帧） | 44.46% | 38.40% | **-6.06 pp** |
| 系统 CPU @80 帧（两边满帧） | 54.99% | 48.26% | **-6.73 pp** |
| 系统 CPU @90 帧 | 60.56% | 60.34% | -0.22 pp |
| 系统 CPU @100 帧 | 60.78% | 59.46% | -1.32 pp |
| FPS @100 帧上限 | 90.76 | 99.18 | **+9.3%** |

---

## 1. 生效项（20 项）

### 1.1 配置类（14 项）

| # | 优化项 | 取值 | 作用 | 实测数据 / 证据 | 置信度 | 画质影响 |
|---|---|---|---|---|---|---|
| 1 | `r.Shadow.PerObject` | 1 → 0 | 关闭逐对象阴影 | `PerObject Shadow Depths` 0.02%→0；`GatherShadowDynamicMeshElements` 0.09%→0；与 #2 同组 **-4.1%（约 -3.8 ms/帧）** | 中（组内） | 小物件阴影消失 |
| 2 | `r.VolumetricFog` | 1 → 0 | 关闭体积雾 | `ComputeVolumetricFog` 0.21%~0.39%→0；与 #1 同组 -4.1% | 强（节点） | 体积雾/光消失 |
| 3 | `r.LightMaxDrawDistanceScale` | 1.0 → 0.6 | 灯光最大绘制距离缩短 | 组 I：89.4→86.7 ms，**-3.0%（约 -2.7 ms/帧）** | 中 | 远处灯光更早衰减 |
| 4 | `r.LightFunctionQuality` | 2 → 0 | 关闭光源函数（投影纹理灯） | `UpdateLightFunctionAtlas` **0.68%→0**（约 -0.6 ms/帧；100 帧口径 0.643→0） | 强（节点） | 投影纹理灯简化 |
| 5 | `r.RHICmd.ParallelTranslate.Enable` | 1 → 0 | 关闭并行 RHI 翻译 | 单项中性（52.71 vs 52.34 ms，-0.7%）；作为组配置保留 | 中 | 无 |
| 6 | `r.MMH.LocalLight.MaxDecodeFacesPerFrame` | 4 → 1 | MMH 局部光解码限制 | `MMH Decode` **0.13%→0.08%**（约 -0.08 ms） | 中（节点） | MMH 局部光质量下降 |
| 7 | `r.MMH.LocalLight.MaxCachedLights` | 4 → 2 | MMH 缓存灯减少 | ≈0（噪声内）；随组保留 | 弱 | 缓存灯减少 |
| 8 | `r.RHICmd.ParallelTranslate.CombineSingleAndParallel` | 0 → 1 | 合并单线程/并行翻译任务 | 组 R：87.5→85.2 ms，**-2.6%（约 -2.3 ms/帧）** | 中 | 无 |
| 9 | `r.LocalFogVolume` | 1 → 0 | 关闭局部雾体积 | `LocalFogVolumeVolumes` 0.04%→0；与 #10 同组 **-1.1%（约 -0.9 ms）** | 中（组内） | 局部雾消失 |
| 10 | `r.LensFlareQuality` | 2 → 0 | 关闭镜头光晕 | 与 #9 同组合计 -1.1% | 弱 | 光晕消失 |
| 11 | `fx.Niagara.DesiredFramesPerSecond` | 32 → 30 | Niagara 模拟 30 Hz | `Particles/Niagara` 1.33%→1.18%；**-1.0%（约 -1.0 ms/帧）** | 中 | 特效更新率下降 |
| 12 | `r.RHICmd.ParallelTranslate.MaxCommandsPerTranslate` | 256 → 1024 | 单次翻译命令数 | `RHI_Translate` 7.45%→7.37%；**-0.4%（约 -0.4 ms）** | 弱 | 无 |
| 13 | `r.GPUStatsEnabled` | 1 → 0 | 关闭逐 GPU Pass Timestamp 查询 | 正反两轮：`RHI_Translate` -0.43/-0.84pp、`SubmissionQueue` -0.18/-0.22pp；**约 -0.6~1.0 ms/帧** | 强（正反两轮） | `stat gpu` / GPU Insights / `-profilegpu` 逐 Pass 计时失效 |
| 14 | **日志降级**（`LogConfig.ini`） | 27 类 Off + 3 类 Warning | 降低日志写入/刷盘 | 日志相关 CPU **1.27%→0.37%（-0.90 pp）**；日志文件 **35.5→16.9 MiB（-52.3%）** | 强（独立 A/B） | 无 |

> 补充：Development 包另有 4 个开发期校验开关（Test 构建编译期裁掉），见 1.3。

### 1.2 代码类（3 项，P4 已修改未提交）

| # | 文件 / 开关 | 改动 | 实测收益 | 置信度 |
|---|---|---|---|---|
| 15 | `Core/Private/Async/Fundamental/Scheduler.cpp`<br>`TaskGraph.WorkerSpinCycles`（默认 0） | Worker 空闲自旋 53 → 0 | `TaskWorkerIsLookingForWork` **1.507→0.096 ms/帧**（-94%）；80 帧口径 -1.28~-1.77 ms/帧；P99 无退化 | 强（正反两轮） |
| 16 | `Renderer/Private/SceneVisibility.cpp`<br>`r.ZXB.LightCullReorder`（默认 1） | 灯光先距离剔除、再视锥求交（语义等价） | `Light_Visibility` **1.42/1.55% → 1.09/1.15%（-25%）**；约 0.33~0.40 ms/帧核时间 | 强（节点，正反两轮） |

> 另有 `D3D12StateCache.cpp` 的 16 个 `RHICommandsChannel` 诊断作用域（通道关闭时零成本），
> 仅用于 ApplyState 细分，不计入优化项。

### 1.3 Development 专用（4 项，Test 编译期裁掉）

| # | 开关 | 结构收益 |
|---|---|---|
| 17 | `r.RDG.Validation=0` | `FRDGBuilder::Execute` 17.73→16.89 ms/帧（-0.84 ms） |
| 18 | `r.GPUScene.Lights.Validate=0` | `UpdateGPUScene_Lights` 0.86%→0（约 -1.55 ms/帧） |
| 19 | `r.GPUScene.ValidateInstanceUpload=0` | 无独立 timer；Test 中裁掉 |
| 20 | `r.MeshDrawCommands.ValidateDynamicPrimitives=0` | 无独立 timer；Test 中裁掉 |
| — | **合计** | **约 -2.5~2.9%/帧（仅 Development）** |

### 1.4 生效项的节点级归属（100 帧正向轮，ms/帧）

| 节点 | 基线 | 全优化 | Δ |
|---|---|---|---|
| `TaskWorkerIsLookingForWork` | 1.507 | 0.096 | **-1.411** |
| `UpdateLightFunctionAtlas` | 0.643 | 0（节点消失） | **-0.643** |
| `RHI_Translate` | 8.507 | 7.100 | **-1.407** |
| `SubmissionQueue_Process` | 2.097 | 1.635 | **-0.462** |
| `FRDGBuilder::ExecutePasses` | 2.010 | 1.461 | **-0.549** |
| `STAT_PostVisibilityFrameSetup_Light_Visibility` | 1.210 | 1.032 | -0.178 |
| `FlushBufferedItems` | 0.173 | 0.164 | -0.009（日志两半恒定） |
| **合计（已列节点）** | — | — | **-4.65** |

---

## 2. 不生效项（22 项，测试后拒绝）

### 2.1 调度类（11 项：工作跨线程迁移，总 CPU 中性）

| # | 项目 | 实测数据 | 结论 |
|---|---|---|---|
| 21 | `r.Visibility.TaskSchedule=0` | 反向轮 54.38（0）vs 54.14（基线）ms，**+0.4%**；InitViews 3.60→2.38、场景调度 2.26→3.27 | 中性，工作从可见性任务搬到渲染线程 |
| 22 | `r.RHICmd.ParallelTranslate.Enable=1` | 52.71（开）vs 52.34（关）ms，**-0.7%**；RHI_Translate 10.16→10.14 不变 | 中性 |
| 23 | `TaskGraph.MaxActiveForeground/BackgroundWorkers`（1/1、1/2、2/1、2/2、BG-only） | 所有变体都使 `TaskWorkerIsLookingForWork` 降 1.0~1.6pp；但 2/2 反向轮出现反转，总量无可复现收益（DiD 约 0.2%） | 不采纳 |
| 24 | `r.RDG.ParallelSetup=0` | RDG 模块省 0.56pp，但 RHI 涨 0.76pp（55.71 vs 58.47 ms） | Setup 串行化 → 工作搬到 RHI |
| 25 | `r.MeshDrawCommands.ParallelPassSetup=0` | 50.35 vs 56.13 ms（后半场景明显变重，无法归因）；串行化预期不优 | 不采纳 |
| 26 | `r.ParallelGatherShadowPrimitives=0` | Shadow 0.97→1.86pp，场景渲染调度 3.33→2.34pp，**两者之和不变** | 工作搬回 GT |
| 27 | `r.SceneRenderCleanUpMode=1` | 场景渲染调度 2.26→2.25pp，**完全不变** | 无可测效果 |
| 28 | `Slate.EnableDrawEvents=0` + `r.RDG.Events=0` | Slate 1.02→0.98pp，RDG 随场景上升 | 无效果 |
| 29 | `r.Visibility.FrustumCull.NumPrimitivesPerTask=2048` | 58.08（2048）vs 55.84（auto）ms，**+3.9%** | 反而更差，保持 auto |
| 30 | `r.RDG.AsyncCompute=1` | 57.71 → 60.07 ms；RDG 3.81→4.03、RHI 11.17→11.22 | 更差 |
| 31 | `r.RHICmdWidth=2/4/8/16` | 2/8 更差（如 56.76→59.23）；4/16 在噪声内 | 保持 DP 的 1 |

### 2.2 质量类（5 项：目标模块无可测下降）

| # | 项目 | 实测数据 | 结论 |
|---|---|---|---|
| 32 | `fx.Niagara.DesiredFramesPerSecond=20` | Niagara 0.90→1.07pp（20Hz 省 0.17pp），总量被抵消（55.62 vs 55.26 ms） | 不采纳 |
| 33 | `r.MMH.LocalLight.MaxCachedLights=1` | Shadow 模块 1.72→1.75pp，**无变化**（表面 +13% 来自场景变重） | 不采纳 |
| 34 | `r.Shadow.DistanceScale=0.8` | Shadow 1.72→1.75pp，无变化（MMH 阴影主路径） | 不采纳 |
| 35 | `r.DetailMode=0` | 双向轮：Niagara/Decal/MDC 未出现预期下降；后半段 Physics +0.4~0.7pp | 不采纳 |
| 36 | `r.ViewDistanceScale=0.8` | 双向轮：MDC/InitViews 无预期下降（反而随场景上升） | 不采纳 |

### 2.3 大特性开关（2 项：只影响 GPU，不影响 CPU 口径）

| # | 项目 | 实测数据 | 结论 |
|---|---|---|---|
| 37 | `r.Shadow.Virtual.Enable=0`（VSM 关） | 节点：`RHI_Translate` -0.10/+0.41pp、`FRDGBuilder::ExecutePasses` -0.08/+0.10pp、Shadow 模块 +0.10/+0.15pp；影子深度从 `MMHShadowDepth` 转为 `CSMShadowDepth`+`OnePassPointLightShadowDepth`，总量不变 | **≈0，不采纳**（端到端 -5.4%/+13.5% 是流式加载漂移） |
| 38 | `r.DynamicGlobalIlluminationMethod=0` + `r.ReflectionMethod=0`（Lumen 关） | RDG Pass 280.7→269.4/帧（-4%）；`ExecutePasses` 1.15→1.08 ms/帧；RHI 命令列 69.84→69.90/帧；`RenderDiffuseIndirectAndAO` 0.41→0.15pp；总 CPU +0.88%（噪声） | **≈0，不采纳** |

### 2.4 RHI / 代码级（4 项：验证后无效或存在风险）

| # | 项目 | 实测/调查结果 | 结论 |
|---|---|---|---|
| 39 | RDG AbsoluteTime 查询帧级批处理（A2） | 复核 `TRHILambdaCommand` 404 vs 424/帧、`EndRenderQuery` 170 vs 183/帧 → 差值仅噪声；根因：动态分辨率预算默认全关（`kBudgetMsDisabled`），批处理分支从未执行 | **no-op，已 P4 revert** |
| 40 | `D3D12.ResidencyManagement=0` | 未实测；代码分析：每 SRV `UpdateResidency` + 每条命令列 ResidencySet Open/Close，估算 0.1~0.3%/帧；ECVF_ReadOnly，需重启验证 | 收益过小，未投入 |
| 41 | Submission 合并（减少 ExecuteCommandLists 次数） | 调查：20 次队列切换/帧、`Submit` 自耗 0.8%；跨队列重排存在依赖/死锁风险 | 风险 > 收益，未实施 |
| 42 | 屏障命令列优化（`GetResourceBarrierCommandList` 0.62%） | 每帧约 14 个屏障命令列、约 47 µs/个；需改提交层状态跟踪，正确性风险高 | 未实施 |

---

## 3. 代码级候选：评估后关闭（3 项）

| # | 候选 | 评估结论 |
|---|---|---|
| 43 | #1 ApplyState 快路径 | 16 个通道作用域细分：SRV 表构建 23%、屏障 14%、非堆状态 14%、Constants 7%、UAV 7%、Samplers 6%，**全部是真实状态应用，无大额空转可删** |
| 44 | #6 PSO 查找缓存 | `InternalSetPipelineState` 已做指针比较；`SetShaderRootConstants` 已有值比较（`D3D12StateCache.cpp:1545`）后才置脏 → **无重复查找可省** |
| 45 | #9 MDC 空/小任务快路径 | 上游已有 `MaxNumDraws > 0` 早退（`MeshDrawCommands.cpp:1483`），空 Pass 不派发任务 → **已实现** |

---

## 4. 最终合并 A/B（全部生效项）

被测项：配置 13 项（12 CVar + `r.GPUStatsEnabled=0`）+ `TaskGraph.WorkerSpinCycles=0` +
`r.ZXB.LightCullReorder=1`。4 个开发期校验开关、MMH 两项、日志降级在两半保持相同（不计入增量）。

| 轮次 | 基线 | 全优化 | Δ（每帧 CPU） | Δ（FPS） |
|---|---|---|---|---|
| 正向（基线→优化，100 帧档） | 54.48 ms / 89.29 fps / 60.81% | 47.39 ms / 99.26 fps / 58.80% | **-13.0%** | **+11.2%** |
| 反向（优化→基线，100 帧档） | 52.69 ms / 92.22 fps / 60.74% | 48.54 ms / 99.09 fps / 60.12% | **-7.9%** | **+7.4%** |
| **均值** | **53.59 ms / 90.76 fps** | **47.97 ms / 99.18 fps** | **-10.5%** | **+9.3%** |

- 节点核对（正向轮，ms/帧）：spin -1.411、LightFunctionAtlas -0.643、RHI_Translate -1.407、
  SubmissionQueue -0.462、RDG ExecutePasses -0.549、Light_Visibility -0.178，全部同向；
  反向轮方向一致。
- 80 帧档补测（21:26 干净轮）：55.12 ms/54.99%/79.82 fps → 48.33 ms/48.26%/79.89 fps，
  **每帧 -12.3%、系统 CPU -6.73 pp**。

---

## 5. 帧率与 CPU 使用率的关系（摘要）

```
系统 CPU% = 每帧 CPU(ms) × 实际帧率(fps) / (8 核 × 1000)
```

| 限帧 | 基线 CPU% | 优化后 CPU% | Δ | 说明 |
|---|---|---|---|---|
| 60 | 44.46% | 38.40% | **-6.06 pp** | 两边满帧，节省全变空闲 |
| 80 | 54.99% | 48.26% | **-6.73 pp** | 两边满帧（21:26 轮） |
| 90 | 60.56% | 60.34% | -0.22 pp | 基线只有 74~80 fps，收益转帧率（79.9→88.7） |
| 100 | 60.78% | 59.46% | -1.32 pp | 基线 89~92 fps，收益转帧率（90.8→99.2） |

- 该用例是**单关键线程受限**，总 CPU% 在 ~60% 见顶；限帧超过机器能力时，优化只体现为帧率。
- 比 CPU 使用率 → 在"两配置都能跑满"的档位（60/80）比较；比性能 → 90/100 看 FPS；
  比效率 → 每帧 CPU（-10.5%）。

---

## 6. 方法与坑

1. **跨轮对比不可用**：同一配置 3 小时内可漂移 -10%；统一采用同轮 A/B（273 s 切换 + 正反两轮）。
2. **总量仍会被流式加载污染**：`IO/Decompress` 单轮可差 1.7pp；大开关判定以"按帧归一化的节点"为准。
3. **执行机负载**：19:00 / 21:02 两次 80 帧正向轮因远端负载（进图延迟 320 s、首半段 46 fps/90% CPU）
   作废；21:26 机器空闲后复测成功。
4. **Test vs Development**：Test 无 DO_CHECK/RDG 校验开销，同一套参数 Development 收益更大
   （-12.3%、FPS +19.6%）。
5. **iOA 拦截新 exe**：本地构建需放行（HTTP 403 / `not-support-protocol`），策略同步 5~20 分钟。
6. **服务器**：性能结论只在 `lobby-test:13787`（DS=CL1183280）上做；
   `9.134.125.189` 的 DS 校验和不匹配，只用于冒烟。

---

## 7. 数据来源

- `D:/Trace/NeoRingTestCampaign/ab_results.json`（tag：`all_*`、`all60_*`、`all80_*`、`all90_*`、`all100_*`）
- `D:/Trace/NeoRingCodeOpt/P0_C1_LIGHT_CULL.md`、`P0_A2_QUERY_BATCH.md`、`P1_G1_GPU_STATS.md`
- `D:/GR_release/findings.md`、`progress.md`、`task_plan.md`
- `D:/GR_release/NeoRing-CPU优化数据报告-2026-10-22.md`（§1~§12）
- `D:/GR_release/NeoRing-CPU优化总结-2026-10-23.md`（交付摘要版）
