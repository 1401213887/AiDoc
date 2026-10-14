# RHI_Translate CPU 优化 — 命令列表碎片化与固定开销 · 测量归因 · CVar 验证与落地

> 仓库 C:\GR_main · 引擎 UE5EA（UE 5.5.4 分叉）· 用例 nr_sfxai（NeoRing 分段·特效与AI）· 每条结论附 文件:行号 证据

**摘要：** 把「RHI_Translate 吃 CPU」拆到可测、可验证、可落地：引擎侧没有单一热点，**耗时均匀分布在每条命令上**，真正的成本是「命令列表被切得太碎」带来的固定开销。从实测数据解出成本模型 **avg_per_cmd = C + F/N**（F≈49 µs/命令列表，占 45%），并据此验证出三个 CVar 的有效性：r.RDG.ParallelExecute=0（−24%，低配档位已配置）、r.RHICmd.ParallelTranslate.CombineSingleAndParallel=1、r.RDG.AsyncCompute=0——后两者组合实测 **核时 −16.4%、帧率 +4.6%（双口径均两组分离）**。另附一套对抗时间漂移的「2×2 同期交替」验证法（实测同配置 2 小时漂移 +24%，单次 A/B 会把漂移误判成优化）。

## 1. 一句话结论与全景

> 引擎侧没有「哪条命令慢」这种热点。`RHI_Translate` 的耗时均匀摊在每条命令上，真正的成本来源是**命令列表被切得过碎**——每个列表都带一份固定开销。

先给结论，再展开：

- **成本模型**：`avg_per_cmd = C + F/N`。`C`≈0.596 µs 是单条命令的真实成本，`F`≈49 µs 是每个命令列表的固定开销，`N` 是每列表平均命令数（由 `RHICommandList.cpp:1326` 处的 `[CmdStats]` 统计实测值联立解出）。
- **优化方向**：既然不能压 `C`（那是真实渲染工作），就去压 `F/N` —— 也就是**减少碎片化**。
- **三个 CVar 都作用在「分片/调度」环节**，而非「命令本身」——这是它们能同时降低核时与提升帧率的原因。

```
① 记录命令 ──▶ ② 组织成分片 ──▶ ③ 翻译(Execute) ──▶ ④提交/⑤GPU
  RenderThread    RDG按pass/        FRHICommandListBase::     EndDrawingViewport
  FRHICommandList MDC按任务          Execute() 逐条执行        (等GPU完成帧)
  命令进连续内存   各自CreateCommand    ★ RHI_Translate 在这
                   List() ⇒ 一堆小列表

三个 CVar 的作用位（都落在「分片/调度」环节，不碰命令本身）：
  r.RDG.ParallelExecute=0                    → ② 命令列表分片层
  ...CombineSingleAndParallel=1              → ③ translate job 切分层
  r.RDG.AsyncCompute=0                       → ④ 提交层（跨队列）
```

本文的主线是一条完整的排障链：**测量 → 定位 → 量化 → 优化 → 验证 → 落地**。每一环都给出可复现的命令与实测数字。

## 2. 成本模型：为什么是「碎片化」而不是「哪条命令慢」

定位的第一步是排除「热点」假设。用命令类型归因统计（见 `测量工具` 一章）得到的结果是**分布平坦**：最贵的类型只占 13.39% 的耗时，没有哪一类能单独解释问题。

| 命令类型 | 数量占比 | 耗时占比 | 单条均值 |
|---|---|---|---|
| FRHICommandDrawIndexedPrimitiveIndirect | 8.93% | 13.39% | 1.694 µs |
| TRHILambdaCommand | 8.20% | 12.12% | 1.668 µs |
| FRHICommandEndDrawingViewport | 0.02% | 11.04% | 517.8 µs |
| FRHICommandDrawIndexedPrimitive | 3.52% | 8.05% | 2.585 µs |
| FRHICommandEndTransitions | 3.23% | 7.67% | 2.682 µs |
| FRHICommandSetStreamSource（数量最多） | 21.28% | 2.65% | 0.141 µs |

*低配档位实测。注意数量最多的 SetStreamSource 恰恰最便宜。*

> **`FRHICommandEndDrawingViewport` 的 517 µs 是个陷阱**：它看着像大果子，但 `D3D12Viewport.cpp:840` 的实现里是 `Viewport->WaitForFrameEventCompletion()` —— **在等 GPU 完成帧**（Present 节流），不是 CPU 在工作，无法用 CPU 优化消除。而且在 CPU 瓶颈的低配机上，这个等待反而更短。

排除热点后，把「每条命令都贵一点」归因到「列表太多」：用两组同期实测值联立解出

```
avg_per_cmd  =  C  +  F / N
```

- `RDG.ParallelExecute=0`（串行、列表大）：`N = 100.5`，`avg_per_cmd = 1.082 µs`
- `RDG.ParallelExecute=2`（并行、列表碎）：`N = 65.8`，`avg_per_cmd = 1.338 µs`
- 联立 ⇒ **F ≈ 48.8 µs，C ≈ 0.596 µs**

> **固定开销 `F` 占 `avg_per_cmd` 的 45%**（0.486 / 1.082）。⇒ 机器越慢/越挤，`F` 的相对占比越高 ⇒ 减少碎片化的收益越大。这解释了同一配置在不同时段测得 −16.4% 与 −21.6% 的差异。

```
avg_per_cmd = C + F / N
  C = 单条命令真实成本(≈0.596 µs)  F = 每列表固定开销(≈49 µs)  N = 每列表命令数

  RDG.ParallelExecute=0 (串行,列表大): N=100.5  1.082 = 0.596 + 48.8/100.5
  RDG.ParallelExecute=2 (并行,列表碎): N= 65.8  1.338 = 0.596 + 48.8/65.8

  ⇒ F 占 avg_per_cmd 的 45%（0.486/1.082）
```

## 3. 三个优化项：机理与实测

三个 CVar 都遵循同一取舍：**用「并行度」换「调度/同步的固定开销」**。引擎的默认值都偏向并行（保守），而本项目的命令流碎片化程度高于引擎的通用假设。

```
渲染管线层(RDG pass 图)        ← r.RDG.AsyncCompute = 0
命令列表分片层(CreateCommandList) ← r.RDG.ParallelExecute = 0
translate job 切分层            ← ...CombineSingleAndParallel = 1
RHI 命令执行层(逐条 Execute)     ← (无 CVar 可调)
GPU 提交 / Present              ← EndDrawingViewport 的 GPU 等待
```

### ① r.RDG.ParallelExecute=0 —— 单项收益最大（−24%）

RDG 侧开关。关掉它，pass 串行执行，**命令列表数量下降 74%**（实测 `calls` 5,349,600 → 3,150,000 量级），`avg_per_cmd` 从 1.338 → 1.082 µs。

> **已配置在项目低配档位**（`DefaultDeviceProfiles.ini:574`）。这是既有优化，本文只是量化了它的贡献。

### ② r.RHICmd.ParallelTranslate.CombineSingleAndParallel=1 —— 合并 translate job

CVar 定义在 `RHICommandList.cpp:95`（默认 `false`）；切分判据在 `RHICommandList.cpp:1718` 的 `FSubmitState::Dispatch`：

```
// 默认（=0）：只要相邻列表的并行性不同就切一个新 job
CurrentTranslateJob->bParallel != CmdList->AllowParallelTranslate()

// =1：仅在「当前并行、新列表要单线程」时才切
CurrentTranslateJob->bParallel && !CmdList->AllowParallelTranslate()
```

渲染时「并行列表」与「单线程列表」交替出现，默认行为会把 job 切得极碎，每个 job 都付一次派发/同步开销。=1 大幅减少 job 数。

> **代价**（`RHICommandList.cpp:97-99` 的 CVar 帮助文本原文）：*Any parallel command lists that get batched with a single thread command list will fall back to translating on the RHI thread.* —— 并行列表被降级到 RHI 线程**串行**翻译，牺牲并行度。

| 环境 | 效应 | 组内极差 |
|---|---|---|
| 默认档位（SuperHigh） | −14.5% ~ −16.1% | 0.6% |
| 低配档位 | −5.8% | 0.6% |

*低配档位效应减半 —— 因为 RDG.ParallelExecute=0 已消除大半碎片化，两者收益重叠。*

### ③ r.RDG.AsyncCompute=0 —— 取消跨队列 fork/join

`RenderGraphBuilder.cpp:1667` 有一段**只在存在 async pass 时执行**的代码（`SCOPED_NAMED_EVENT(AsyncComputeFences, ...)`），其上方注释说明了成本：

```
// Establishes fork / join overlap regions for async compute. This is used for fencing
// as well as resource allocation / deallocation. Async compute passes can't allocate /
// release their resource references until the fork / join is complete, since the two
// pipes run in parallel. Therefore, all resource lifetimes on async compute are
// extended to cover the full async region.
```

关掉它省掉三样：**per-pass 的 fork/join 区间计算**（纯 CPU）、**跨队列 fence 同步**、以及**资源生命周期被延长**带来的分配/释放牵动。

> **代价**：失去「计算 pass 与图形 pass 重叠执行」的机会。但实测帧率**反而提升 4.6%** ⇒ 本负载下跨队列管理开销 > 重叠收益。

### 组合效果：核时与帧率双赢

| 口径 | A（基线） | B（Combine=1 + AsyncCompute=0） | Δ | 两组分离？ |
|---|---|---|---|---|
| 核时 avg_per_cmd | 1.079 µs | 0.903 µs | −16.4% | ✅ |
| 帧率 fps | 70.1 / 70.6 | 73.7 / 74.3 | +4.6% | ✅ |

*默认档位 2×2 交替实测。低配档位复验 −21.6%。*

> **组合远大于两者单独之和**（−5.8% + −3.9% = −9.7%）⇒ 协同增效。两个口径都「两组完全分离」，且 A 组极稳（核时极差 0.1%、帧率 0.7%）。

## 4. 测量工具：r.RHI.CommandStats 与 r.RHIFrameCounter

全部结论来自本次为引擎新增的、**默认关闭**的诊断工具（`#pragma region Engine ZXB` 包裹）。关闭时运行时开销 ≈ 一次 int 判断。

### 统计开关

| CVar | 默认 | 作用 |
|---|---|---|
| r.RHI.CommandStats | 0（关） | 统计 RHI 命令数与耗时；开启后每 N 次 Execute 汇总一行 |
| r.RHI.CommandStatsFrames | 300 | 每多少次 Execute() 汇总打印一行 |
| r.RHIFrameCounter（变量） | — | 由 D3D12 在 Present 路径自增，供精确帧率计算 |

### 输出示例

```
LogRHI: [CmdStats] calls=5910300 cmds=397244180 avg_cmds=67.2 \
        total_cost=536947.06ms avg_per_cmd=1.352us max_cmds=29111 frames=39120 fps=70.1
LogRHI: [CmdStatsType] #1  FRHICommandDrawIndexedPrimitiveIndirect  count=4081 \
        count_share=8.93%  time=6.9ms  time_share=13.39%  avg=1.694us
```

### 为什么需要自建精确帧计数

本机原有三条取帧率的路径**全部不可靠**，这是加计数器的直接原因：

| 路径 | 失败原因 |
|---|---|
| trace 的 frames 字段 | `frames = N/A` —— 帧标记在统计选区内不可用 |
| 用 EndDrawingViewport 的 count_share 反推 | share 四舍五入到 0.01% ⇒ 帧数误差达 3 倍 |
| 用 count × 理论采样率 1/1024 | 采样率非严格均匀（见下），且「每帧恰好一次」未经证实 |

> **采样偏差是真实陷阱**：首版把采样计数器放在每次 `Execute()` 内重置，对短命令列表退化成「只采列表首条」，把某类型的平均耗时**高估约 40 倍**（0.147 µs 误报成 5.666 µs），差点当成大热点。修正为**跨 Execute 持续计数**（`std::atomic`）后分布才变平坦。识破它靠的是 trace 的独立口径对不上。

## 5. 验证方法：2×2 同期交替（对抗时间漂移）

本文所有效应量都建立在同一个前提上：**`avg_per_cmd` 跨时间不可比**。

> 实测同一配置相隔 2 小时，`avg_per_cmd` 漂移 **+24%**（0.873 → 1.082 µs）；傍晚段漂移速率约 **5% / 13 分钟**，且非单调（会回升）。⇒ **单次 A/B 对照会把漂移误判成优化效果**。

对策是把对照组与实验组**交替**跑（A/B/A/B），让漂移同时作用于两组，并以**「两组是否重叠」作为唯一判据**：

```
判决规则（唯一可靠的判据）
  B 组最大值 < A 组最小值   ⇒ 两组分离，结论可信
  否则                      ⇒ 不可判（需增大轮次或等环境更稳）
```

```
✗ 单次对照: A 跑完跑 B            ✓ 2×2 交替: A/B/A/B
   A=1.000  B=0.940                 A1=1.079 B1=0.893 A2=1.080 B2=0.912
   环境在两次之间漂移(+24%)          B组最大0.912 < A组最小1.079
   ⇒ 差异可能来自漂移                ⇒ 两组分离，结论可信
```

该流程已固化为可复用脚本 `ab_rhi_cvar.py`（本文同目录）：强制交替、报组内离散度、按上述规则判可信与否。它也**自带产物校验**——以 `game-stdout.log` 的 mtime 是否推进作为「本局真的跑过」的硬证据。

> **这个自检是踩坑换来的**：首版脚本漏了它，runner 一报错就进下一局，4 次读同一个旧文件 ⇒ **4 个样本数值完全相同、4 局只花 2 分钟**。失败伪装成「极其稳定的重复测量」，比明显报错危险得多。

## 6. 落地：配置怎么写、什么条件下有效

推荐写入的低配/通用档位配置：

```
[ConsoleVariables]
r.RHICmd.ParallelTranslate.CombineSingleAndParallel=1
r.RDG.AsyncCompute=0
```

项目里已配置的（既有，非本次新增）：`r.RDG.ParallelExecute=0`（`DefaultDeviceProfiles.ini:574`）。

### 生效条件：必须按逻辑核数判断

| 逻辑核数（含超线程） | 引擎走的分支 | Combine 是否有效 |
|---|---|---|
| > 8（6C/12T、8C/16T…） | if | ✅ 有效 |
| ≤ 8（4C/8T…） | else | ❌ 空操作 |

*else 分支在 RHICommandList.cpp:2210 把 bAllowSingleParallelCombine 硬编码为 true；此时所有命令列表的 bParallel 均为 false ⇒ 切分判据恒等，CVar 从不被读取。*

> **不要盲目加到所有档位**：≤8 逻辑核时引擎走 else 分支，该 CVar **从不被读取**，设了也不会有任何效果。

### 落地路径（是否需要 cook）

| 路径 | 持久性 | 要 cook？ |
|---|---|---|
| pak 内 BaseEngine.ini | 永久 | 要 ❌ |
| pak 内 DefaultDeviceProfiles.ini | 永久（按档位） | 要 ❌ |
| Saved/Config/Windows/Engine.ini | 单次（游戏退出即被覆写） | 不要 ✅ |
| -ini: 命令行 | 单次 | 不要 ✅ |

启动器 `Run_FateTrigger.bat` 已按此实现：启动前判断 `%NUMBER_OF_PROCESSORS%`，>8 才写入 `Saved/Config`（两个 CVar 各自独立判重，部分写入的文件也能补全）。

### 覆盖设备档位推送的 CVar：用 -ExecCmds

设备档位（DeviceProfile）的 CVar 推送发生在启动后约 50 秒，会覆盖 `-ini:`。要临时改这类 CVar，用 `-ExecCmds` —— 它是**累加**的（`ParseExecCommands.cpp:57-68` 用 `while` + 游标扫描命令行中所有 `-ExecCmds=`，第 65 行 `Result.Append(...)` 逐个并入），且执行时机在档位推送之后。

```
-DeviceProfile=Windows_CPU_Low -ExecCmds="r.RHICmdWidth 8"
```

## 7. 口径澄清：核时占比 vs 帧率

「低配机上 RHI 消耗看起来更高」是**口径的必然结果**，不代表绝对工作量更大。

`tot`（核时占比）的分母是 **时长 × 核数**：

| 机器 | 核数 | 同样的 0.3 核 RHI 工作量 → tot |
|---|---|---|
| 本测试机 | 32 | 0.94% |
| 常见低配笔记本 | 8 | 3.75% |
| 4 核低配 | 4 | 7.5% |

> ⇒ 低核机上同一个绝对工作量，占比自动放大数倍。要用「绝对核时」或「帧率」衡量，不要用跨机器对比的核时占比。

## 8. 已排查确认无空间的项

以下路径都已实测排掉，记录下来避免重复投入：

| 方向 | 结论 |
|---|---|
| r.MeshDrawCommands PSO 聚合 | 本 checkout 只有 SortByPipelineId，实测无可测量收益 |
| r.RDG.CullPasses / MergeRenderPasses | 默认已是 1，pass 合并已最优 |
| 屏障 / transition 批处理 CVar | 不存在（自动屏障系统无外部旋钮） |
| 命令去重机制 | 引擎无此机制 |
| r.RHICmdWidth（放开串行化） | 中性偏负（碎片化 +16%） |
| 关 RDG.ParallelSetup + ParallelDestruction | 中性（calls 降但单条成本不变） |
| wp.AutoInstancing | 构建期选项，改了要重新 cook 关卡 |
| PrePass (Forced by VirtualTexture) | 平台特性强制，要省只能关 VT（动画质） |

## 9. 方法论与踩过的坑

这些是本次排障中真实踩到、且**会在任何性能工作上重现**的坑。

| 坑 | 表现 | 对策 |
|---|---|---|
| `-corelimit=N` 不能模拟低配机 | 它不影响 `NumberOfCoresIncludingHyperthreads()`，核数根本没被压下 | 用 `-DeviceProfile=Windows_CPU_Low` 强制档位 |
| `-ini:` 对「注册晚」的 CVar 会失效 | 日志出现 `deferred - dummy variable created` | 落配置后必须查日志里的 `Set CVar` 行确认 |
| 采样点不均匀 ⇒ 假热点 | 计数器每次 Execute 重置 ⇒ 只采列表首条 ⇒ 平均耗时高估 40 倍 | 跨调用持续计数；用 trace 独立口径交叉验证 |
| 脚本失败伪装成稳定结果 | runner 报错即返回 ⇒ 4 次读同一旧文件 ⇒ 4 个样本完全相同 | 以产物 mtime 推进作为「真的跑过」的硬证据 |
| cmd 的 `echo X=1>> file` 会丢值 | `1>>` 被当作标准输出重定向，写出的值缺 `1` | 改用 `>>file echo X=1`，并实测产物 |

## 10. 参考

本文引用的全部源码位置（行号为本次成文时实测，对齐工作区当前 CL）：

| 文件:行号 | 内容 |
|---|---|
| RHICommandList.cpp:95 | r.RHICmd.ParallelTranslate.CombineSingleAndParallel 定义（默认 false） |
| RHICommandList.cpp:97-99 | 该 CVar 的帮助文本（说明它以「减并行」换「减派发开销」） |
| RHICommandList.cpp:1718 | translate job 切分判据（bAllowSingleParallelCombine 三元分支） |
| RHICommandList.cpp:2201 | if 分支：从 CVar 读取 bAllowSingleParallelCombine |
| RHICommandList.cpp:2210 | else 分支：硬编码 bAllowSingleParallelCombine = true |
| D3D12Viewport.cpp:840 | Present 路径的 WaitForFrameEventCompletion（GPU 同步等待） |
| RenderGraphBuilder.cpp:1667 | AsyncComputeFences 阶段（fork/join 区间与跨队列 fence） |
| ParseExecCommands.cpp:57-68 | -ExecCmds 的累加解析（第 65 行 Result.Append） |
| MeshDrawCommands.cpp:1829 | SetAllowParallelTranslateByUser 的全仓唯一调用点 |
| DefaultDeviceProfiles.ini:573-574 | 项目低配档位既有的 r.RHICmdWidth=1 / r.RDG.ParallelExecute=0 |
| BaseEngine.ini:3856 | 引擎出厂默认 r.RHICmd.ParallelTranslate.CombineSingleAndParallel=false |

- 本任务技术记录：`findings.md`（F1–F75，含每轮实验的原始数据与自我纠错）
- 验证脚本：`ab_rhi_cvar.py`（2×2 交替验证器，含产物自检）
- 同目录另有 `RHI_Translate优化报告.md`（面向渲染团队的简版）
