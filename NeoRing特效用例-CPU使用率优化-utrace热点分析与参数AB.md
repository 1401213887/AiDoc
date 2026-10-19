# NeoRing 特效用例 CPU 使用率优化 —— utrace 热点分析与纯参数 A/B

> **对象**：S1Game Development 客户端（CL 1183280）+ NeoRing 特效用例 `nr_sfxai`，远端执行机跑测、utrace 经 CpuUsageTopReport 聚合。
> **工具链**：loop_runner（SSH 远端跑测）→ Unreal Insights EXPORTTOPCPU → cpu-usage-analysis 模块报告 → 参数 A/B。
> **结论**：不改代码，仅靠 12 项 CVar + 日志降级，**每帧 CPU 98.63 → 86.48 ms（-12.3%）**，**帧率 41.29 → 49.39 FPS（+19.6%）**，零崩溃。
> **关键口径**：总 CPU% 会被帧率波动主导（帧率越高、总 CPU% 反而上升），必须用**每帧 CPU = total_cpu_pct × 8 × duration / frames** 归一化。

## 结论：最终配置与收益

原始对照 2 次：每帧 CPU 98.0 / 99.3 ms（均值 98.63 ms），FPS 41.90 / 40.67（均值 41.29）。

最终配置 U 三次：84.7 / 86.9 / 87.9 ms（均值 86.48 ms），FPS 49.67 / 49.11 / 49.38（均值 49.39）。

**每帧 CPU -12.3%，帧率 +19.6%，零崩溃。**

```ini
[ConsoleVariables]
fx.Niagara.DesiredFramesPerSecond=30
r.RHICmd.ParallelTranslate.MaxCommandsPerTranslate=1024
r.LightFunctionQuality=0
r.Shadow.PerObject=0
r.VolumetricFog=0
r.LightMaxDrawDistanceScale=0.6
r.RHICmd.ParallelTranslate.Enable=0
r.MMH.LocalLight.MaxDecodeFacesPerFrame=1
r.MMH.LocalLight.MaxCachedLights=2
r.RHICmd.ParallelTranslate.CombineSingleAndParallel=1
r.LocalFogVolume=0
r.LensFlareQuality=0
```

日志：`<包体>/S1Game/Saved/Config/Windows/LogConfig.ini` 把 27 个高频类别设为 Off（仅保留 LogUnLua/LogS1Game/LogNet=Warning）。

视觉取舍：逐对象阴影关闭、体积雾关闭、灯光最大距离 ×0.6、局部雾/镜头光晕关闭。RHI/MMH/日志参数不影响画面结构。

## 测量方法论：为什么必须用每帧 CPU

总 CPU% 是「占用率」，帧率越高，单位时间做更多帧，总 CPU% 反而更高。同一场景三次跑测（同一日志配置）：

| 跑测 | FPS | 总 CPU% | 每帧 CPU |
|---|---|---|---|
| baseline | 35.7 | 50.91% | 114.1 ms |
| run2 | 41.1 | 51.18% | 99.7 ms |
| run3 | 40.2 | 52.24% | 104.0 ms |

三次总 CPU% 从 50.91 升到 52.24，但每帧 CPU 完全无规律——**总 CPU% 不能用于 A/B**。

固定条件：用例 `nr_sfxai_fps80`（`t.MaxFPS 80`；对局内实际 40~50 FPS，80 未触顶）；每组 2~3 次取均值；同配置两次每帧 CPU 可差 ±3%。

每帧 CPU = `total_cpu_pct × 8 × duration_s / frames`（frames 取 `FEngineLoop::Tick` 实例数）。

## 基线热点排名

原始基线（不限帧，系统平均 CPU 50.91%，8 核，349.6s）按模块 Tot% 排名（排除用户指定的物理/动画/音频与已盘查的 RHI_Translate）：

| # | 模块 | Tot% |
|---|---|---|
| 1 | RDG / FrameGraph | 10.90 |
| 2 | RHI / Submission | 9.78 |
| 3 | MDC 管理 | 2.87 |
| 4 | 任务调度 / TaskWorker | 2.22 |
| 5 | InitViews / Visibility | 1.86 |
| 6 | Particles / Niagara | 1.32 |
| 7 | Engine Loop / GameThread | 1.29 |
| 8 | Shadow | 1.03 |
| 9 | GPU Scene | 0.78 |

Timer 级 exclusive Top（排除动画/物理）：`UpdateGPUScene_Lights` 0.77%、`GenerateDynamicMeshDrawCommands` 0.66%、`Light_Visibility` 0.65%、`RDG ProcessAsyncSetupQueue` 0.47%、`DeleteSceneRenderer` 0.50%、`ComputeViewRelevance` 0.41%、`Init dynamic shadows` 0.56%、`GatherAndSortLights` 0.33%。

## 有效优化项与 A/B 数据

| 组 | 参数 | 每帧 CPU | 结论 |
|---|---|---|---|
| B | Shadow PerObject/点阴影缓存/Niagara 30Hz | 100.0 ms | -1.0% |
| C | MaxCommandsPerTranslate 256→1024 | 100.6 ms | -0.4% |
| H | 关逐对象阴影 + 关体积雾 | 89.4 ms | 主要收益来自 PerObject/VolumetricFog（CSM 参数无效） |
| I | 灯光距离 0.8→0.6 | 86.7 ms | 累计 -12.1% |
| N | 移除无效 CSM 参数 + MMH 两项 | 87.48 ms | MMH Decode 0.13%→0.08% |
| R | CombineSingleAndParallel=1 | 85.2 ms | **-2.6%**（减少 RHI 翻译 dispatch） |
| U | LocalFogVolume=0 + LensFlareQuality=0 | 84.7 ms | 最终配置；三次均值 86.48 ms |

日志降级（独立硬证据）：日志文件 113 MB → 25 MB；trace 时段行数 362,392 → 60,773（降级1）→ 更低（Off 化）；`FlushBufferedItems` 0.69% → 0.18% → 0.07%；`OutputDeviceRedirector` 线程 0.70% → 0.19%。

## 无效或反向的参数（含原因）

| 参数 | 结果 | 原因 |
|---|---|---|
| RDG ParallelSetup=0 + MDC ParallelPassSetup=0 + ParallelExecute=0 + BgWorkers=4 | 帧率 -14% | 降低并行度，引入卡顿 |
| GPUScene.Lights.AsyncSetup=0 | FG Worker -8.6%，帧率 -4.5% | 工作转移到 RenderThread，同步阻塞 |
| LuxGI.Amortization.Budget=64 | +2% ~ +3.6% | GI 流送预算降低无收益 |
| HZBOcclusion=0 | +3.6% | 更多 primitive 进入渲染，drawcall 增加 |
| RHICmdWidth=4 / 16 | +3.3% / 噪声内 | 任务粒度调整无收益 |
| AmbientOcclusionLevels=0 | +3.7% | 关闭 AO 反向 |
| SSR.Quality=0 | -1.4%（噪声内） | 收益不足，且有反射画质损失 |
| MMH 页请求预算 1024/512→256/128 | +4.3% | 页预算降低触发更多回退路径 |

## 项目特性：MMH 阴影与 CSM 的关系

项目方向光阴影走 **MMH 投影**：`r.MMH.Projection` 默认 1（`MMHShadowMapRendering.cpp:27`、`MMHShadowMapRendering.cpp:29`），`MMHShadowDepth` pass 使用 `EShadowDepthType::MMH | EShadowDepthType::Directional`（`MMHShadowDepthRendering.cpp:518`）。CSM 只在 `bMMHLocalLightCSMFallback` 等回退路径使用（`ShadowSetup.cpp:961`）。

因此 `r.Shadow.CSM.MaxCascades`（`SceneRendering.cpp:473`）、`r.Shadow.CSMCaching`、`r.Shadow.MaxNumPointShadowCacheUpdatesPerFrame` 对主路径基本无效：实测 CSM 2→1 后 `FGatherShadowPrimitivesPrepareTask` 仍为 0.57%。最终配置已移除。

MMH 自己的 CPU 参数：`r.MMH.LocalLight.MaxDecodeFacesPerFrame`（默认 4，实测降到 1 有效，`MMHLocalLightSceneData.cpp:75`）、`r.MMH.LocalLight.MaxCachedLights`（`MMHLocalLightSceneData.cpp:61`）、`r.MMH.LocalLight.ShadowAtlasFaceResolution`（2048，GPU 侧，`MMHLocalLightSceneData.cpp:69`）。MMH 异步参数默认已开（`r.MMH.Virtual.AsyncDecodeRawFeedbackValue=1`、`AsyncThreadUpdate=1`），无额外空间。

## 本地构建 exe 无法运行包体的根因

本地 `D:\GR_release`（CL 1183280 / ++GR+release）编译的 exe 覆盖远端包体后秒退，手动运行捕获 `Corrupt pak index detected`（pakchunk6-Windows.pak，Encrypted: 1）。

离线 AES-256-ECB 解密 pak index（2,450,496 B）：

| key | 来源 | 解密后 SHA1 |
|---|---|---|
| 主 key `yp95...` | 源码库 `S1Game/Config/DefaultCrypto.ini` = `Scripts/BuildScripts/UnrealPak/Crypto.json` | `A3D91CF5...`（= 我们 exe 报的 Computed Hash） |
| Business key `bKu7...` | DefaultCrypto.ini SecondaryEncryptionKeys | `FDFEFE49...` |
| Stored（pak 内记录） | — | `B7DA01F6...` |

诊断版 exe 实测打印注册 key：`Guid=00000000... Hex=CA9F7924...54F399B`，与源码库 key 一致——证明构建/key 注入/覆盖位置都正确。结论：**打包机使用的 pak 主加密 key 与源码库不同**（CI 注入或发布加固），未经该流程的本地 exe 必然解不开。出厂 exe 可正常挂载。

## 复现步骤与配置清单

1. 远端包体：`<包体>/S1Game/Binaries/Win64/S1Game.exe`（出厂版即可，本优化不改代码）。
2. 用例模板：`NR_SpecialEffectsAI_FPS80.txt`（相对源模板仅把 `20;t.MaxFPS 0` 改为 `20;t.MaxFPS 80`，字节级差 1 字节）。
3. 跑测：`loop_runner.py --remote s1remote --case nr_sfxai_fps80 --exe-source package ...`。
4. 参数写入 `<包体>/S1Game/Saved/Config/Windows/Engine.ini` 的 `[ConsoleVariables]` 段（**游戏启动会重写该文件，每次跑测前需重新写入**）。
5. 日志写入 `<包体>/S1Game/Saved/Config/Windows/LogConfig.ini`（客户端日志系统读取路径：`LogSuppressionInterface.cpp:548`）。
6. 拉 trace：`find_artifacts.py --what utrace --latest --copy-to`；分析：`ue_cpu_trace_to_module_report.py`。

## 附录：完整 A/B 记录

| 组 | total CPU | FPS | 每帧 CPU | 结论 |
|---|---|---|---|---|
| 原始对照 | 51.31% / 50.49% | 41.90 / 40.67 | 98.0 / 99.3 | 基准 |
| F | 49.24% | 42.51 | 92.7 | 噪声内 |
| G | 48.43% | 40.58 | 95.5 | 帧率 -4.5% |
| H | 50.51% | 45.22 | 89.4 | 画质取舍 |
| I | 52.95% | 48.84 | 86.7 | 累计 -12.1% |
| J | 53.99% | 48.21 | 89.6 | +3.3% |
| K | 55.38% | 47.37 | 93.5 | +7.8% |
| L | 53.97% / 53.30% | 50.00 / 48.63 | 86.4 / 87.7 | -11.8% |
| M | 55.09% | 49.51 | 89.0 | +3% |
| N | 53.34% / 54.85% | 49.56 / 49.38 | 86.1 / 88.9 | 87.48 |
| O | 52.61% | 48.66 | 86.5 | 噪声内 |
| P | 59.80% | 52.81 | 90.6 | +3.6% |
| Q | 54.03% | 48.38 | 89.3 | +2% |
| R | 53.27% | 50.02 | 85.2 | -2.6% |
| S | 53.82% | 48.89 | 88.1 | +3.4% |
| T | 53.37% | 48.54 | 88.0 | +3.3% |
| T2 | 52.37% | 48.95 | 85.6 | 确认 R |
| U | 52.57% / 53.35% / 54.24% | 49.67 / 49.11 / 49.38 | 84.7 / 86.9 / 87.9 | 最终 |
| V | 53.60% | 48.85 | 87.8 | +3.7% |
| W | 52.63% | 49.76 | 84.6 | 噪声内 |
| X | 54.38% | 48.62 | 89.5 | +4.3% |

