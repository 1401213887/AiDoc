# NeoRing 客户端 CPU 优化总结（2026-10-23）

> 包体：`S1Game_release_0.1183280..._Test_Win64`（CL 1183280，Test）
> 引擎/工程：`D:\GR_release`（UE5EA 自定义分支 + S1Game）
> 用例：`nr_sfxai`（NeoRing 特效用例，单局，273 s 处同轮切换 A/B）
> 口径：**每帧 CPU = total_cpu_pct × 8 × duration_s × 10 / frames（ms）**；系统 CPU% 取整段进程总 CPU。
> 排除项（既定）：物理 / Chaos、动画、音频。

---

## 1. 一句话结论

**全部优化合并后：每帧 CPU -10.5%（53.59 → 47.97 ms），限帧 100 时帧率 90.8 → 99.2（+9.3%）。**
CPU 使用率的降幅取决于限帧档位：**60 帧档 -6.1 pp（44.5% → 38.4%）、80 帧档 -6.7 pp（55.0% → 48.3%）**；90/100 帧档因基线本身跑不满上限，收益主要体现为帧率（见第 5 节）。

---

## 2. 最终收益（多口径）

| 口径 | 基线 | 优化后 | 收益 |
|---|---|---|---|
| **每帧 CPU（100 帧口径）** | 53.59 ms | 47.97 ms | **-5.62 ms（-10.5%）** |
| 系统 CPU @60 帧（两边满帧） | 44.46% | 38.40% | **-6.06 pp（-13.6%）** |
| 系统 CPU @80 帧（两边满帧，21:26 轮） | 54.99% | 48.26% | **-6.73 pp** |
| 系统 CPU @90 帧 | 60.56% | 60.34% | -0.22 pp |
| 系统 CPU @100 帧 | 60.78% | 59.46% | -1.32 pp |
| FPS @100 帧上限 | 90.76 | 99.18 | **+9.3%** |
| FPS @90 帧上限 | 76.87 | 88.75 | **+15.5%** |
| 日志文件（Test） | 35.5 MiB | 16.9 MiB | **-52.3%** |

> 60/80/90/100 四档为同轮 A/B 的正反两轮均值；单轮之间因场景（AI 数、特效、流式加载）波动可达 8~15%。
> 每帧 CPU 以 100 帧正反两轮为最稳定口径（两种状态各自的两个半段分别相差 &lt;3%）。
> 80 帧档共有 4 个有效半段样本，区间 -3.3 ~ -12.0 pp；表中取"两半都跑满 80 帧"的 21:26 轮
> （54.99% → 48.26%，两半 79.82 / 79.89 fps），其余 3 轮因基线实际只有 75~79 fps（未满帧），
> 一部分收益已经转成帧率。

---

## 3. 优化项总清单

### 3.1 配置类（12 CVar + GPUStats + 日志）

| # | 优化项 | 值 | 作用 | 单独证据 / 估计收益 | 置信度 | 画质影响 |
|---|---|---|---|---|---|---|
| 1 | `r.Shadow.PerObject` | 1 → 0 | 关闭逐对象阴影 | `PerObject Shadow Depths` 0.02%→0；`GatherShadowDynamicMeshElements` 0.09%→0；与 #2 同组 **-4.1%（约 -3.8 ms/帧）** | 中（组内） | 小物件阴影消失 |
| 2 | `r.VolumetricFog` | 1 → 0 | 关闭体积雾 | `ComputeVolumetricFog` 0.21%~0.39%→0；与 #1 同组合计 -4.1% | 强（节点） | 体积雾/光消失 |
| 3 | `r.LightMaxDrawDistanceScale` | 1.0 → 0.6 | 灯光最大绘制距离缩短 | 组 I：89.4→86.7 ms，**-3.0%（约 -2.7 ms/帧）** | 中 | 远处灯光更早衰减 |
| 4 | `r.LightFunctionQuality` | 2 → 0 | 关闭光源函数（投影纹理灯） | `UpdateLightFunctionAtlas` **0.68%→0**（约 -0.6 ms/帧） | 强（节点） | 投影纹理灯简化 |
| 5 | `r.RHICmd.ParallelTranslate.Enable` | 1 → 0 | 关闭并行 RHI 翻译 | 单项≈0（仅线程迁移）；作为组配置保留 | 中 | 无 |
| 6 | `r.MMH.LocalLight.MaxDecodeFacesPerFrame` | 4 → 1 | MMH 局部光解码限制 | `MMH Decode` **0.13%→0.08%**（约 -0.08 ms） | 中（节点） | MMH 局部光质量下降 |
| 7 | `r.MMH.LocalLight.MaxCachedLights` | 4 → 2 | MMH 缓存灯减少 | ≈0（噪声内） | 弱 | 缓存灯减少 |
| 8 | `r.RHICmd.ParallelTranslate.CombineSingleAndParallel` | 0 → 1 | 合并单线程/并行翻译任务 | 组 R：87.5→85.2 ms，**-2.6%（约 -2.3 ms/帧）** | 中 | 无 |
| 9 | `r.LocalFogVolume` | 1 → 0 | 关闭局部雾体积 | `LocalFogVolumeVolumes` 0.04%→0；与 #10 同组 **-1.1%（约 -0.9 ms）** | 中（组内） | 局部雾消失 |
| 10 | `r.LensFlareQuality` | 2 → 0 | 关闭镜头光晕 | 与 #9 同组合计 -1.1% | 弱 | 光晕消失 |
| 11 | `fx.Niagara.DesiredFramesPerSecond` | 32 → 30 | Niagara 模拟 30 Hz | `Particles/Niagara` 1.33%→1.18%；**-1.0%（约 -1.0 ms/帧）** | 中 | 特效更新率下降 |
| 12 | `r.RHICmd.ParallelTranslate.MaxCommandsPerTranslate` | 256 → 1024 | 单次翻译命令数 | `RHI_Translate` 7.45%→7.37%；**-0.4%（约 -0.4 ms）** | 弱 | 无 |
| 13 | `r.GPUStatsEnabled` | 1 → 0 | 关闭逐 GPU Pass Timestamp 查询 | `RHI_Translate` -0.43/-0.84pp、`SubmissionQueue` -0.18/-0.22pp；**约 -0.6~1.0 ms/帧** | 强（正反两轮） | `stat gpu` / GPU Insights 失效 |
| 14 | **日志降级**（`LogConfig.ini`） | 27 类 Off，3 类 Warning | 降低日志写入/刷盘 | 日志相关 CPU **1.27%→0.37%（-0.90 pp）**；日志文件 **-52.3%** | 强（独立 A/B） | 无 |

> Development 包另有 4 个开发期校验开关（Test 构建编译期裁掉）：`r.RDG.Validation=0`、
> `r.GPUScene.Lights.Validate=0`、`r.GPUScene.ValidateInstanceUpload=0`、
> `r.MeshDrawCommands.ValidateDynamicPrimitives=0`，Development 合计约 **-2.5~2.9%/帧**。

### 3.2 代码类（P4 修改，未提交）

| # | 文件 / 开关 | 改动 | 实测收益 | 置信度 |
|---|---|---|---|---|
| 15 | `Core/Private/Async/Fundamental/Scheduler.cpp`<br>`TaskGraph.WorkerSpinCycles`（默认 0） | Worker 空闲自旋 53 → 0 | `TaskWorkerIsLookingForWork` **1.507 → 0.096 ms/帧**（100 帧口径，-94%）；80 帧口径 -1.28~-1.77 ms/帧 | 强（正反两轮） |
| 16 | `Renderer/Private/SceneVisibility.cpp`<br>`r.ZXB.LightCullReorder`（默认 1） | 灯光先距离剔除、再视锥求交（语义等价） | `Light_Visibility` **1.42/1.55% → 1.09/1.15%（-25%）**；约 0.33~0.40 ms/帧核时间 | 强（节点，正反两轮） |
| 17 | 4 个开发期校验开关（见上） | 关闭 DO_CHECK/RDG 校验 | Development 约 -2.5~2.9%/帧；Test 编译期裁掉 | 强（节点） |

### 3.3 收益归属（节点级，100 帧正向轮，ms/帧）

| 节点 | 基线 | 全优化 | Δ |
|---|---|---|---|
| `TaskWorkerIsLookingForWork` | 1.507 | 0.096 | **-1.411** |
| `UpdateLightFunctionAtlas` | 0.643 | 0（节点消失） | **-0.643** |
| `RHI_Translate` | 8.507 | 7.100 | **-1.407** |
| `SubmissionQueue_Process` | 2.097 | 1.635 | **-0.462** |
| `FRDGBuilder::ExecutePasses` | 2.010 | 1.461 | **-0.549** |
| `STAT_PostVisibilityFrameSetup_Light_Visibility` | 1.210 | 1.032 | -0.178 |
| `FOutputDeviceRedirector::FlushBufferedItems` | 0.173 | 0.164 | -0.009（日志两半恒定） |
| **合计（已列节点）** | — | — | **-4.65** |

其余差额（约 -1 ms/帧）分散在 draw/状态切换与后台任务——质量类 CVar 减少了可见图元与 RDG Pass。

> 说明：单项数字来自不同轮次，**不可直接相加**；节点证据用于证明"每一项确实生效"，总量以第 2 节合并 A/B 为准。

---

## 4. 未采纳项（有数据、无收益或更差）

| 类别 | 项目 | 结果 |
|---|---|---|
| 大特性开关 | `r.Shadow.Virtual.Enable=0`（VSM 关） | 节点 Δ：RHI -0.10/+0.41pp、RDG -0.08/+0.10pp、Shadow +0.10/+0.15pp → **≈0**；端到端大波动来自流式加载漂移 |
| 大特性开关 | `r.DynamicGlobalIlluminationMethod=0` + `r.ReflectionMethod=0`（Lumen 关） | RDG Pass 280.7→269.4/帧（-4%），但 `ExecutePasses` 仅 -0.07 ms/帧、RHI 命令列数不变 → **总 CPU ≈0** |
| RDG | `r.RDG.ParallelExecute=2` | 每帧 +13.9%、总 CPU +6.91pp → 更差 |
| RDG | `r.RDG.AsyncCompute=1` | 57.71→60.07 ms → 更差 |
| RDG | `r.RDG.ParallelSetup=0` | RDG 省 0.56pp，RHI 涨 0.76pp → 中性 |
| RHI | `r.RHICmdWidth=2/4/8/16` | 2/8 更差，4/16 噪声内 |
| RHI | RDG AbsoluteTime 查询帧级批处理（A2） | 复核为 no-op（动态分辨率预算默认全关，批处理分支从未执行），已 revert |
| 调度 | `r.Visibility.TaskSchedule=0` | 工作跨线程迁移，总和不变 |
| 调度 | `r.MeshDrawCommands.ParallelPassSetup=0` | 无收益 |
| 调度 | `r.ParallelGatherShadowPrimitives=0` | Shadow 0.97→1.86，场景调度 3.33→2.34，总和不变 |
| 调度 | `TaskGraph.MaxActiveFG/BG`（1/1、1/2、2/1、2/2 等） | 无一致收益 |
| 质量 | `fx.Niagara.DesiredFramesPerSecond=20` | Niagara 省 0.17pp，总量被抵消 |
| 质量 | `r.MMH.LocalLight.MaxCachedLights=1` / `r.Shadow.DistanceScale=0.8` / `r.DetailMode=0` / `r.ViewDistanceScale=0.8` | 目标模块无可测下降 |
| Slate | `Slate.EnableDrawEvents=0` + `r.RDG.Events=0` | 无效果 |

**结论：剩余 CPU 不在"大开关"上；关闭 VSM/Lumen 只改变 GPU 负载，不降低客户端 CPU。**

---

## 5. 帧率与 CPU 使用率的关系（核心）

### 5.1 公式

```
系统 CPU% = 每帧 CPU 工作量(ms) × 实际帧率(fps) / (核数 × 1000)
          = 每帧 CPU(ms) × fps / 8000        （本机 8 核）
```

优化降低的是**每帧工作量**；它最终表现为 CPU% 还是 FPS，取决于**限帧与实际帧率**。

### 5.2 四档实测（同一组优化）

| 限帧 | 基线 CPU% | 优化后 CPU% | Δ | 基线实际 FPS | 优化实际 FPS |
|---|---|---|---|---|---|
| **60** | **44.46%** | **38.40%** | **-6.06 pp** | 60.0（满帧） | 60.0（满帧） |
| **80** | **54.99%** | **48.26%** | **-6.73 pp** | 79.82（满帧） | 79.89（满帧） |
| **90** | 60.56% | 60.34% | -0.22 pp | 73.8~79.9（跑不满） | 88.7~88.8 |
| **100** | 60.78% | 59.46% | -1.32 pp | 89.3~92.2（跑不满） | 99.1~99.3 |

### 5.3 规律

1. **限帧低于机器能力（60 帧）**：两配置都被限帧压住，优化省下的时间全部变成空闲/睡眠
   → **CPU% 完整体现收益（-6.1 pp）**。
2. **限帧等于机器能力（80 帧）**：两边都能压住限帧时（21:26 轮：79.82 / 79.89 fps），
   收益同样完整体现为 **CPU% -6.73 pp**；若某轮基线只能跑到 75~79 fps（未满帧），
   则会有一部分收益提前转成帧率，CPU% 降幅会被压缩到 -3~-5 pp。
3. **限帧超过机器能力（90/100 帧）**：基线被**关键线程**卡在 74~92 fps，总 CPU% 稳定在 ~60%；
   优化把关键路径缩短，帧率提升 7~15%，**CPU% 几乎不变（-0.2 ~ -1.3 pp）**。
4. 根因：该用例不是"8 核全满"，而是**单关键线程受限**；总 CPU% 在 ~60% 附近见顶，
   继续提升帧率不会显著抬高总 CPU。因此：
   - **比 CPU 使用率** → 必须在"两配置都能跑满"的档位（60 / 80）比较；
   - **比性能** → 用 90/100 看帧率；
   - **比效率** → 用每帧 CPU（-10.5%，与帧率无关）。

### 5.4 怎么选验收口径

| 目标 | 推荐口径 | 结果 |
|---|---|---|
| 降低 CPU 占用（发热/续航/后台占用） | 限帧 60~80（产品目标帧率，且两配置都能满帧） | CPU% **-6.1 ~ -6.7 pp** |
| 提升帧率/流畅度 | 限帧 100（或去掉限帧看 fps） | FPS **+9.3%**（90 帧档 +15.5%） |
| 衡量优化本身的效率 | 每帧 CPU | **-10.5%** |

> 若产品要求"100 fps 且 CPU 更低"，验收必须在**基线也能跑满 100 帧**的机器/画质档上做；
> 在那种环境下，同样 -10.5% 的每帧收益会把 100 帧下的 CPU 从约 67% 压到约 60%（约 -7 pp）。
> 本执行机基线差约 10% 到不了 100 帧，所以 100 帧档只看得到 -1.3 pp。

---

## 6. 复现方法

1. **构建**：`build_s1_client.py --config Test --project D:/GR_release/S1Game/S1Game.uproject
   --engine-root D:/GR_release/UE5EA --extra-args=-EnableLQGAPSystem`（需 P4 工作区
   `DJANGOZHAN-PCFW_GR_release`；本地包需 `-EnableLQGAPSystem` 才能解密 pak）。
2. **覆盖包体**：`stage_exe.py` 复制 exe（跳过 3 GB pdb），保留 `.orig` 备份。
3. **登录探针**：iOA 会按 exe 哈希拦截新构建（HTTP 403，日志表现 `not-support-protocol`），
   放行后需等策略同步约 5~20 分钟；先跑一次 4 分钟短用例确认能进 `NeoRing.NeoRing up for play`。
4. **跑测**：`D:/Trace/NeoRingTestCampaign/ab_run.py <case> '<Engine.ini 覆盖项 JSON>' <tag>`；
   用例模板在技能 `S1Game自动化Debug/templates/`（`AB_ALL60/80/90/100_*`）。
5. **分析**：`ab_run.py` 自动取 trace，按 `offset+10~158s` / `offset+168~316s` 两个半段出
   每帧 CPU、系统 CPU、FPS，并写 `ab_results.json`。
6. **环境**：`lobby-test.grgame.qq.com:13787`（DS=CL1183280）；不要在 `9.134.125.189`
   上做性能结论（该服 DS 校验和不匹配）。

---

## 7. 遗留事项与风险

1. **P4 未提交**：`Scheduler.cpp`、`SceneVisibility.cpp`、`D3D12StateCache.cpp`（诊断作用域）、
   以及 4 个开发期校验开关所在文件仍处于 `edit`；提交前需复核 diff，
   `SceneVisibility.cpp` 需先同步 depot 的 #71。
2. **`D3D12StateCache.cpp` 诊断代码**：16 个 `RHICommandsChannel` 作用域（通道关闭时零成本）+
   `ApplyState_DirtyStats` 计数器块（约 0.02% 开销），交付前建议移除计数器块。
3. **测量漂移**：同轮 A/B 的总量仍会被资产流式加载污染（`IO/Decompress` 单轮可差 1.7pp），
   大开关判定以"按帧归一化的节点"为准。
4. **质量取舍**：第 3.1 节的 #1/#2/#3/#6/#7/#9/#10/#11 均降低画质（阴影/雾/灯/特效），
   需与美术/策划确认可接受。
5. **平台差异**：Test 构建无 Development 校验开销；同一套参数在 Development 上收益更大
   （-12.3%、FPS +19.6%）。
6. **80 帧补测记录**：19:00 与 21:02 两次正向轮受执行机负载影响（进图延迟 320 s、首半段 46 fps/90% CPU），
   数据作废；21:26 在机器空闲（6~13% CPU）时复测成功，作为 80 帧档的正式样本。

---

## 8. 附：数据来源

- 合并 A/B（80 帧）：`D:/Trace/NeoRingTestCampaign/ab_results.json` → `all_off2on` / `all_on2off`
- 合并 A/B（100 帧）：同文件 → `all100_off2on` / `all100_on2off`
- 多档限帧：同文件 → `all60_*` / `all80_*` / `all90_*` / `all100_*`
- 代码项：`D:/Trace/NeoRingCodeOpt/P0_C1_LIGHT_CULL.md`、`P0_B1_PENDING.md`、`P1_G1_GPU_STATS.md`
- 详细过程：`D:/GR_release/findings.md`、`progress.md`、`NeoRing-CPU优化数据报告-2026-10-22.md`
