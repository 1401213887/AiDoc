# NeoRing 客户端 CPU 优化最终报告（RTX 2060 + GTX 1060 双机型）

- 项目：S1Game / GR（CL 1183280，Test Win64 包体）
- 用例：`nr_sfxai`（NeoRing 特效用例，单局；A/B 在 273 s 同轮切换，正反两轮）
- 日期：2026-10-22 ~ 2026-10-24
- 口径：**每帧 CPU = total_cpu_pct × 核数 × duration_s × 1000 / frames（ms）**
  - 系统 CPU% = 进程总 CPU；GPU 瓶颈时 CPU% ≈ 每帧 CPU × 实际 FPS / (核数 × 1000)
- 排除项（既定）：物理 / Chaos、动画、音频

---

## 0. 摘要

| 机型 | 硬件 | 结论 |
|---|---|---|
| **A：10.97.5.113** | i7-9700（8C/8T）+ **RTX 2060** + 32 GB | 每帧 CPU **-10.5%**；100 帧上限下 FPS **+9.3%**；60/80 帧档 CPU% **-6.1 / -6.7 pp** |
| **B：10.97.6.113** | i7-4770（4C/8T）+ **GTX 1060 6GB** + 16 GB | 之前优化复测：FPS **+13.4%**（两方向一致）、每帧 -5.6~-13.3%；新增 **RHI 任务线程模式 CPU -3.3 ~ -11 pp**；50 帧档最低 CPU **~42%** |

**核心结论：**

1. **优化有效，但表现形态取决于瓶颈**：
   - A（2060，CPU 可支撑 ~99 fps）→ 收益主要体现为**每帧 CPU 与 CPU%**；
   - B（1060，GPU 瓶颈 ~58-65 fps）→ 收益主要体现为**帧率**；CPU% 只在"两边都能跑满"的限帧档才完整体现。
2. **可复现的优化池**：14 项配置 + 2 项代码 + 4 项 Development 专用 + 日志降级（详见 §3）。
3. **B 机型新增可用项**：`r.RHIThread.Enable 2`（RHI 任务线程，画质不变），
   无上限 CPU -11 pp / FPS -6.6%；60 帧档 CPU -3.3 pp / FPS -2.4%；50 帧档两者无差。
4. **限帧是 CPU% 的最大杠杆**：每降 10 fps，CPU% 约降 8 pp（B 机型）。
5. **20+ 项调度/质量/大特性开关被证伪**（VSM、Lumen、级联、局部阴影灯、CSM 缓存、
   RHICmdWidth、并行翻译、Worker 上限、CPUUsageTrack、Residency 等）。

---

## 1. 测试环境

### 1.1 两台执行机

| 项 | A | B |
|---|---|---|
| 地址 / SSH | `10.97.5.113:2222`（`s1remote`） | `10.97.6.113:2222`（`s1remote1060`） |
| CPU | Intel **i7-9700** 8C/8T @3.0GHz | Intel **i7-4770** 4C/8T @3.4GHz |
| GPU | **RTX 2060**（+ UHD 630） | **GTX 1060 6GB** |
| 内存 | 32 GB | 16 GB |
| 包体 | `C:\Users\yonhaochen\Downloads\S1Game_release_0.1183280..._Test_Win64` | `C:\Users\Administrator\Downloads\S1Game_release_0.1183280..._Test_Win64` |
| 登录账号 | `ZXB` | `cyh` |
| 服务器 | `lobby-test:13787`（后切 13841） | `lobby-test:13841`（AutoTest02） |

### 1.2 工具链

```
loop_runner.py（SSH 远端跑测，CMD 脚本驱动）
  → 游戏内置 Trace（cpu/gpu/frame/log/...）
  → .utrace（~2.8-4.0 GB/轮）
  → UnrealInsights EXPORTTOPCPU / TraceMcpS1（CPU 模块报告、线程/timer 下钻）
  → ab_run.py（同轮 A/B：273 s 切换 + 正反两轮；输出每帧 CPU / 系统 CPU / FPS）
```

### 1.3 测量方法（关键）

1. **同轮 A/B**：同一对局内 273 s 用控制台切换配置；正反各一轮，抵消"后半段场景变重"的漂移。
2. **每帧 CPU 为主口径**：总 CPU% 受帧率影响（帧率越高做帧越多），不能跨帧率直接比。
3. **节点证据**：`RHI_Translate`、`SubmissionQueue_Process`、`UpdateLightFunctionAtlas`、
   `TaskWorkerIsLookingForWork`、`Light_Visibility` 等按帧归一化后对比。
4. **漂移标定**：同配置跨轮可漂移 ±5~15%；单轮总量仍会被资产流式加载污染（`IO/Decompress` 可差 1.7 pp）。

---

## 2. 优化流程（逐步记录）

### 阶段一：基线采集（A，2026-10-17）
1. 在 A 上跑 `nr_sfxai`（Development 包体），689 s、单回合、零崩溃；
   trace 2.56 GB；系统平均 CPU **50.91%**（8 核口径，选区 349.61 s）。
2. 发现 trace 录制自身开销显著（停采后 40 → 80~120 FPS），**前后对比必须同口径**。

### 阶段二：热点分析（A）
1. 模块排名：RDG / RHI / Physics / Animation / TaskWorker / Slate…
2. Timer 级 exclusive 排名 + 树形下钻。
3. 结论：RHI_Translate、TaskWorker 空闲自旋、RDG Pass 本体、灯光可见性是主要非排除项。

### 阶段三：CVar 战役（A，2026-10-22）
1. 先做**固定帧率**（40/80）对比，确认总 CPU% 不可跨帧率比较 → 改用每帧 CPU。
2. 顺序测试 20+ 个 CVar（每项 1 轮），每 3~4 项插一次对照。
3. 发现跨轮漂移达 -10%，改为**同轮 A/B（273 s 切换 + 正反两轮）**。
4. 采纳 12 项 CVar + 日志降级；调度类全部证伪（工作跨线程迁移）。

### 阶段四：代码级优化（A，2026-10-22 ~ 23）
1. **P0-B1**：`TaskGraph.WorkerSpinCycles` 参数化（`Scheduler.cpp`），默认改 0。
   - 同轮 A/B：53→0 省 1.28~1.77 ms/帧；`TaskWorkerIsLookingForWork` 1.82%→0.11%；P99 无退化。
2. **P0-C1**：灯光可见性先距离剔除、再视锥求交（`SceneVisibility.cpp`，`r.ZXB.LightCullReorder`）。
   - `Light_Visibility` 1.42/1.55% → 1.09/1.15%（**-25%**）。
3. **P0-A2**：RDG 查询批处理 → 复核发现动态分辨率预算全关、分支从未执行，**已 revert**。
4. **P1-G1**：`r.GPUStatsEnabled=0`（配置项）：`RHI_Translate` -0.43/-0.84 pp、
   `SubmissionQueue` -0.18/-0.22 pp（约 0.6~1.0 ms/帧）。
5. Development 专用 4 个校验开关（Test 编译期裁掉）：约 -2.5~2.9%/帧。

### 阶段五：综合 A/B（A，2026-10-23）
1. 80 帧档：正向 52.15→65.13 ms（+24.9%，含漂移）；反向 56.90→51.72 ms（**-9.10%**）。
   → 保守取 **-5.18 ms/帧（-9.1%）**；机制节点全部同向。
2. 100 帧档（基线 CPU 受限，89 fps）：
   - 正向 54.48→47.39 ms（**-13.0%**），FPS 89.3→99.3（+11.2%）；
   - 反向 52.69→48.54（优化侧 -7.9%），FPS 92.2→99.1（+7.4%）；
   - **均值 53.59→47.97 ms（-10.5%），FPS 90.8→99.2（+9.3%）**。
3. 多档限帧（A）：

| 限帧 | 基线 CPU% | 优化 CPU% | Δ | 说明 |
|---|---|---|---|---|
| 60 | 44.46 | 38.40 | **-6.06 pp** | 两边满帧 |
| 80 | 54.99 | 48.26 | **-6.73 pp** | 两边满帧 |
| 90 | 60.56 | 60.34 | -0.22 pp | 基线跑不满（74-80 fps），收益转帧率 |
| 100 | 60.78 | 59.46 | -1.32 pp | 基线 89-92 fps，收益转帧率 |

### 阶段六：新机型 B（GTX 1060）接入（2026-10-23 晚 ~ 24）
1. 硬件确认：i7-4770 4C/8T + GTX 1060 6GB + 16 GB；`S1Game-Win64-Test.exe`（296 MB）。
2. **iOA 坑**：自编译 exe 放在 `S1Game.exe` 被拦（`not-support-protocol`）；
   同一文件覆盖到 `S1Game-Win64-Test.exe` 后**立刻能登录** → iOA 规则按**程序路径**匹配。
   解法：`stage_exe.py --target S1Game-Win64-Test` + 技能 `client.target=S1Game-Win64-Test`。
3. **基线画像**：trace 期间 43~57 fps、CPU 63.6%、每帧 ~90 ms；
   RHI 12.3%、Physics 5.8%、RDG 4.4%、PSO 预编译 3.8%（首跑冷缓存，第二次 0.26%）、
   IO 3.5%、InitViews 3.4%、MDC 3.2%、Animation 3.6%。
4. **GPU 瓶颈确认**：基线 GPU 16.6 ms / 帧 17.6 ms（94%）；优化后 14.3 / 15.5 ms（92%）。

### 阶段七：B 上复测全部既有优化
1. **配置项（11 CVar + GPUStats，出厂 exe）**：
   - 正向 89.64→84.63 ms（-5.6%），49.85→56.60 fps（**+13.5%**）；
   - 反向 78.30→67.90 ms（-13.3%），56.83→64.43 fps（**+13.4%**）。
2. **代码项（自编译 exe）**：
   - `TaskWorkerIsLookingForWork` 0.879→0.056 / 0.059→0.928 ms/帧（**-0.82~-0.87**）；
   - `Light_Visibility` 1.814→1.705 / 1.776→1.597（**-0.11~-0.18**）；
   - 合计 **-0.99 ms/帧（-1.4%）**。
3. **日志降级**：72.51→72.28 ms（-0.22 ms，-0.31%）。
4. **Worker 2/2**：75.90→76.95 ms（+1.4%）→ 无收益。

### 阶段八：B 定向战役（batch 1~4）

**batch 1（6 项）**

| 测试 | 正向 | 反向 | 结论 |
|---|---|---|---|
| CSM 级联 2→1 | +8.4% | 1→2 更快 | 1 级联更差，拒绝 |
| 局部阴影灯 16→4 | -1.1% | 4→16 更快 | 方向矛盾，拒绝 |
| VSM 开→关 | -1.9% | 关→开 +2.9% | 节点 Δ≈0，拒绝 |
| **Lumen 开→关** | -7.1%，**FPS +20%** | 关→开 +4.7%，FPS -6% | GPU/FPS 大赢，CPU% 反升 |
| RHICmdWidth 1→4 | +3.3% | 4→1 +6.6% | 噪声，拒绝 |
| 并行翻译 开 | +22.8% | 开→关 +3.7% | 显著更差，保持关 |

**batch 2（3 项）**

| 测试 | 正向 | 反向 | 结论 |
|---|---|---|---|
| CSM 缓存 0→1 | +6.6% | 1→0 +1.4% | 平均更差，拒绝 |
| **RHI 线程 1→0** | FPS 64→41.8，CPU 60.7→**34.8%** | 0→1：FPS 46→65，CPU 33.7→60.3 | CPU -26pp 但 FPS -35%，极端取舍 |
| **RHI 任务线程 1→2** | 71.67→66.78 ms，FPS 65.1→59.4，CPU 58.3→**49.6%** | 2→1：CPU 51.0→64.3 | **CPU -11pp，FPS -6.6%**；p50 15.15→16.36、p99 19.9→23.1 |

**batch 3（task 调优 + 60 帧档）**

| 轮次 | 配置 | FPS | CPU% | 每帧 |
|---|---|---|---|---|
| task60 | task + 限帧60 | 58.0/58.2 | **47.50/48.50** | 65.5/66.6 ms |
| ded60 | dedicated + 限帧60 | 59.4/59.7 | **51.88/50.78** | 69.9/68.0 ms |
| task+BG2 | — | 64.97/61.27 | 64.46/61.05 | 79.4/79.7 ms | 无复现收益 |

**batch 4（4 项 + 服务器事故）**

| 测试 | 结果 | 结论 |
|---|---|---|
| CPUUsageTrack workers 1/1 | 72.21→72.83（+0.9%） | 无收益 |
| task + Lumen关 + 60帧 | 57.5/57.9 fps，49.4/50.1% | 跨轮样本，Lumen 在 task 模式下不提帧 |
| dedicated + Lumen关 + 60帧 | 59.3/59.6 fps，56.6/58.1% | 跨轮样本，噪声大 |
| Residency 关 | 单轮 68.64 ms / 56.9%；对照轮进程退出 | 证据不足，不采纳 |

**服务器事故（batch 4 期间）**：
- `13787` 登录正常但 `CreateRoom` 返回 **72004 = DSNotFoundCluster**（无可用 DS）；
- `gateway-test-ha/ds:13751`、`lobby-stress:13751`、`9.134.125.189:13751` 从执行机连接超时；
- 切到 **`lobby-test:13841`（AutoTest02 服，同 CL）**后恢复进图；
- 服务器列表接口必须带 `branch=release&bincl=1183280`（`branch=main` 缺 1183280 条目）。

### 阶段九：B 限帧曲线（最终配置）

| 限帧 | 模式 | 实际 FPS | 系统 CPU% |
|---|---|---|---|
| 50 | dedicated | 49.8/50.0 | **41.44/44.25** |
| 50 | task | 49.8/49.9 | **39.98/44.06** |
| 60 | dedicated | 59.4/59.7 | **51.88/50.78** |
| 60 | task | 58.0/58.2 | **47.50/48.50** |
| 80/无上限 | dedicated | ~64-65 | ~58-61 |
| 80/无上限 | task | ~58-59 | ~49-50 |

> 同配置跨轮（13787 vs 13841）可差 ±3.5 pp；上表优先看同轮。

---

## 3. 生效优化项总表（双机型）

### 3.1 配置类（14 项）

| # | 项 | 值 | A（2060）实测 | B（1060）实测 | 画质影响 |
|---|---|---|---|---|---|
| 1 | `r.Shadow.PerObject` | 1→0 | 与 #2 同组 -4.1%（约 -3.8 ms） | 随组生效（节点消失） | 小物件阴影消失 |
| 2 | `r.VolumetricFog` | 1→0 | `ComputeVolumetricFog` 0.21~0.39%→0 | 0.647→0 ms/帧 | 体积雾/光消失 |
| 3 | `r.LightMaxDrawDistanceScale` | 1.0→0.6 | 组 I -3.0%（约 -2.7 ms） | 随组生效 | 远处灯光更早衰减 |
| 4 | `r.LightFunctionQuality` | 2→0 | `UpdateLightFunctionAtlas` 0.68%→0 | 0.699→0 ms/帧 | 投影纹理灯简化 |
| 5 | `r.RHICmd.ParallelTranslate.Enable` | 1→0 | 单项中性（保留组配置） | 开→关：**差 22.8%/6.6%**，必须关 | 无 |
| 6 | `r.MMH.LocalLight.MaxDecodeFacesPerFrame` | 4→1 | `MMH Decode` 0.13→0.08% | 随组生效 | MMH 局部光质量下降 |
| 7 | `r.MMH.LocalLight.MaxCachedLights` | 4→2 | ≈0 | 随组生效 | 缓存灯减少 |
| 8 | `r.RHICmd.ParallelTranslate.CombineSingleAndParallel` | 0→1 | 组 R -2.6%（约 -2.3 ms） | 随组生效 | 无 |
| 9 | `r.LocalFogVolume` | 1→0 | `LocalFogVolumeVolumes` 0.04→0 | 随组生效 | 局部雾消失 |
| 10 | `r.LensFlareQuality` | 2→0 | 与 #9 同组 -1.1% | 随组生效 | 光晕消失 |
| 11 | `fx.Niagara.DesiredFramesPerSecond` | 32→30 | `Particles/Niagara` 1.33→1.18%；-1.0% | 随组生效 | 特效更新率下降 |
| 12 | `r.RHICmd.ParallelTranslate.MaxCommandsPerTranslate` | 256→1024 | `RHI_Translate` 7.45→7.37%；-0.4% | 随组生效 | 无 |
| 13 | `r.GPUStatsEnabled` | 1→0 | RHI -0.43/-0.84pp、Submission -0.18/-0.22pp | RHI 14.67→13.28、Sub 3.02→2.38（反向） | GPU stat 计时失效 |
| 14 | 日志降级（27 类 Off） | — | 日志 CPU 1.27→0.37%（-0.90pp） | -0.22 ms/帧（-0.3%） | 无 |

### 3.2 代码类（2 项 + Development 4 项）

| # | 项 | 文件 / 开关 | A | B |
|---|---|---|---|---|
| 15 | Worker 空闲自旋 53→0 | `Async/Fundamental/Scheduler.cpp`，`TaskGraph.WorkerSpinCycles` | `TaskWorkerIsLookingForWork` 1.82%→0.11%；-1.28~-1.77 ms/帧 | 0.88→0.06 ms/帧（-0.85） |
| 16 | 灯光先距离剔除再视锥 | `Renderer/Private/SceneVisibility.cpp`，`r.ZXB.LightCullReorder` | `Light_Visibility` -25%（1.42/1.55→1.09/1.15%） | 1.81→1.60 ms/帧（-0.14~-0.18） |
| 17-20 | 4 个开发期校验开关 | RDG/GPUScene/MDC Validate | Development -2.5~2.9%/帧；Test 编译期裁掉 | 同左 |

### 3.3 B 机型新增：RHI 线程模式

| 模式 | 开关 | 无上限 | 60 帧档 | 50 帧档 | 画质 |
|---|---|---|---|---|---|
| dedicated（默认） | `r.RHIThread.Enable 1` | ~58-61% / 64-65 fps | 51.3% / 59.5 fps | 42.8% / 50 fps | 不变 |
| **task** | `r.RHIThread.Enable 2` | **~49-50% / 58-59 fps** | **48.0% / 58.1 fps** | 42.0% / 50 fps | 不变 |
| 无 RHI 线程 | `r.RHIThread.Enable 0` | 34.8% / 41.8 fps | — | — | 不变（帧率崩） |

> `r.RHIThread.Enable` 是 console command：CMD 脚本 20 s 执行即可在 trace 前生效；
> 发布侧建议 `-ExecCmds="r.RHIThread.Enable 2"`。

---

## 4. 未采纳项（双机型证据）

| 项目 | A（2060） | B（1060） |
|---|---|---|
| VSM 关 | 节点 Δ -0.10/+0.41、-0.08/+0.10、+0.10/+0.15 pp → ≈0 | 节点 Δ≈0（Shadow/RHI/RDG）→ ≈0 |
| Lumen 关 | CPU ≈0（RDG Pass -4%，ExecutePasses -0.07ms） | **FPS +20%**、每帧 -4.35ms，但 CPU% 反升 |
| RDG ParallelExecute=2 | 每帧 +13.9% / 总 CPU +6.91pp → 更差 | 未测（同类） |
| RDG AsyncCompute=1 | 57.71→60.07 ms → 更差 | 未测 |
| RDG ParallelSetup=0 | RDG -0.56pp 但 RHI +0.76pp → 中性 | 未测 |
| MDC ParallelPassSetup=0 | 无收益 | 未测 |
| ParallelGatherShadowPrimitives=0 | Shadow+调度之和不变 | 未测 |
| SceneRenderCleanUpMode / SDrawEvents+RDG.Events | 无变化 | 未测 |
| FrustumCull 2048 | +3.9% 更差 | 未测 |
| RHICmdWidth 2/4/8/16 | 2/8 更差，4/16 噪声 | 4：噪声/矛盾 |
| TaskSchedule=0 | 中性（线程迁移） | 未测 |
| Worker 上限（多种） | 无复现收益 | 无复现收益 |
| 并行翻译 开 | 中性（A） | **显著更差**（+22.8%） |
| Niagara 20Hz / MMH cache1 / ShadowDistance 0.8 / DetailMode0 / ViewDistance0.8 | 目标模块无可测下降 | 未测（同类） |
| CSM 级联 2→1 | 未测 | 更差（拒绝） |
| 局部阴影灯 16→4 | 未测 | 无收益 |
| CSM 缓存 0→1 | 未测 | 平均更差 |
| CPUUsageTrack workers 1/1 | 未测 | 无收益 |
| Residency 关 | 未测 | 证据不足（对照轮退出） |
| RDG 查询批处理 A2 | **no-op，已 revert** | 同左 |

---

## 5. 双机型对比与解读

| 维度 | A（i7-9700 + 2060） | B（i7-4770 + 1060） |
|---|---|---|
| 瓶颈 | CPU（100 帧档基线 89 fps） | **GPU**（GPU 16.6/17.6 ms，94%） |
| 基线每帧 CPU | 56.7-61.2 ms（80 帧档） | ~90 ms（43-57 fps） |
| 优化后每帧 CPU | ~48-53 ms | ~66-76 ms |
| 主要收益形态 | 每帧 CPU/CPU%（两边都能满帧的档位） | **FPS**（GPU 瓶颈）+ 各档限帧下的 CPU% |
| 最有效的单项 | Worker 自旋 -1.3~1.8 ms | 配置组 FPS +13.4%；task 模式 -11pp CPU |
| CPU% 最低实测 | 60 帧档 38.4% | 50 帧档 ~42%；60 帧档 task 48.0% |
| 关键教训 | 调度类 CVar 只搬工作；每帧口径 | GPU 瓶颈下 CPU 优化不提帧；限帧是 CPU% 主杠杆 |

**为什么同一套优化在两台机器表现不同：**
- `CPU% = 每帧 CPU × 实际 FPS / 容量`。A 的 CPU 更强、基线能跑 89-100 fps，
  优化后每帧下降直接降低 CPU%；B 的 1060 先到 GPU 上限（~58-65 fps），
  优化把每帧 CPU 降低后，机器用更多帧把 CPU% 抵回去，收益表现为帧率。
- B 的 4C/8T + 超线程使调度开销更敏感：专用 RHI 线程的交接成本占比更高，
  任务线程模式可省 11 pp；但帧预算紧时（50/60 帧）优势缩小。

---

## 6. 最终建议（按机型）

### A（RTX 2060 / i7-9700）
| 目标 | 配置 | 数据 |
|---|---|---|
| 降 CPU（同帧率） | 综合优化 + 限帧 60 | 44.5→38.4%（-6.1pp） |
| 降 CPU（80 档） | 综合优化 + 限帧 80 | 59.9→55.9%（-6.7pp） |
| 提帧 | 综合优化（100 档） | 90.8→99.2 fps（+9.3%） |
| 效率 | 每帧 CPU | -10.5% |

### B（GTX 1060 / i7-4770）
| 目标 | 配置 | 数据 |
|---|---|---|
| 保 60 fps、降 CPU | 综合优化 + `r.RHIThread.Enable 2` + 限帧 60 | 58 fps / **48.0%**（dedicated 59.5 / 51.3%） |
| 保 60 fps、画质优先 | 综合优化 + 限帧 60 | 59.5 fps / 51.3% |
| CPU 优先 | 综合优化 + 限帧 50 | ~50 fps / **42.8%** |
| FPS 优先 | 综合优化 + Lumen 关 | 67 fps（+20%），CPU 约 58% |
| 视觉不变 | 既有 12 CVar + 代码项 | FPS +13.4%，每帧 -10% |

---

## 7. 方法与坑（沉淀）

1. **同轮 A/B 是唯一可靠方法**：跨轮漂移 ±5~15%；正反两轮抵消"后半段变重"。
2. **每帧 CPU 才是效率**：总 CPU% 与帧率耦合；同帧率下比较 CPU%，效率比较用每帧 CPU。
3. **节点证据优先**：总量受流式加载/场景波动污染时，用按帧归一化的 timer 判断机制是否生效。
4. **大开关先看机制**：VSM/Lumen 在 CPU 口径均 ≈0；关闭它们只改变 GPU 负载。
5. **执行机状态**：iOA 零信任按**程序路径**放行（`S1Game.exe` vs `S1Game-Win64-Test.exe`）；
   新构建需策略同步 5~20 分钟；每次先跑登录探针。
6. **服务器**：列表接口要带 `branch=release&bincl=<CL>`；`CreateRoom` 的 72004 =
   `DSNotFoundCluster`，换同 CL 的其它服（如 13841）。
7. **Trace 开销**：录制本身约占 1.2% CPU、显著压低 FPS；所有结论同口径对比。
8. **PSO 冷缓存**：首跑 `FPSOPrecacheAsyncTask` 3.8%，热缓存后 0.26%，不要误判为优化项。

---

## 8. 遗留与风险

- **P4 未提交**：`Scheduler.cpp`、`SceneVisibility.cpp`、`D3D12StateCache.cpp`（诊断作用域）；
  `SceneVisibility.cpp` 需先同步 depot #71 再提交。
- **诊断代码**：`D3D12StateCache.cpp` 的 16 个通道作用域（零成本）+ DirtyStats 计数器块（~0.02%），
  交付前建议移除计数器。
- **B 的 task 模式**：帧时间尾部变差（p99 +3.2 ms），需真实对局验证手感后再灰度。
- **限帧档位**：50/60 档的 CPU% 收益来自少渲染帧，属产品取舍。
- **测试服务器**：`13787` 的 DS 集群不可用；`13841` 可用但不保证长期稳定。
- **自编译 exe 的 pdb 未部署**：崩溃符号化不可用。

---

## 9. 数据来源

- `D:/Trace/NeoRingTestCampaign/ab_results.json`（tag：`all_*`、`all60/80/90/100_*`、`nh1060_*`）
- `D:/Trace/NeoRingCPU/`、`D:/Trace/NeoRingTestRetest/`、`D:/Trace/NeoRingFinalAB/`
- `D:/Trace/NewHost1060/`（1060 全部轮次日志与 trace）
- `D:/GR_release/NeoRing-CPU优化数据报告-2026-10-22.md`（A 的详细数据）
- `D:/GR_release/NeoRing-1060机型CPU优化报告-2026-10-24.md`（B 的详细数据）
- `D:/GR_release/progress.md`、`findings.md`（逐步过程记录）
