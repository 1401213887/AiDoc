# Snapdragon-Profiler-功耗分析指导手册

> 用 Snapdragon Profiler (SDP) 在骁龙手机上定位**功耗热点 / 发热原因**的操作手册。核心思路：功耗是"估算"不是"实测"——SDP 用硬件计数器 + 能量模型估算相对功耗，帮你定位"哪块在烧电"；要精确电流值需外接硬件电流计（Monsoon）。发热 = 功耗高 + 散热差，先定位功耗大户，再看频率/温控。

---

## 〇、功耗分析的原理与边界（先搞清楚，别白忙）

| 能力 | SDP 能做什么 | SDP 做不到 |
|---|---|---|
| 功耗 | 基于硬件计数器 + 能量模型的**相对功耗估算**（CPU 核/GPU/DSP/内存分块） | 精确 mW 电流值（那是 Monsoon/QPST 电流计的事） |
| 发热 | 看 **Thermal** 类别：各 thermal zone 温度曲线，确认是否触发温控 | 预测温升（取决于机身散热/环境） |
| 归因 | 频率曲线 + 负载曲线 + 功耗估算 → 定位到"CPU 锁频"还是"GPU 满载" | 精确到哪个 Shader/DrawCall 烧电（那是 AOC/RenderDoc 的活） |

**一句话边界**：SDP 功耗分析回答"CPU/GPU/内存哪个在烧电、烧多久、是不是锁频"；回答不了"这个 Shader 比那个 Shader 多耗 20mA"。

## 一、前置准备

1. **SDP 已装好且能启动**（本机 `C:\Program Files\Qualcomm\Snapdragon Profiler\`，2026.8.0）。
   - ⚠️ 若启动崩溃：先读 `E:\AiDoc\Snapdragon-Profiler-启动崩溃-msvcp140-Runtime版本不兼容排查修复指南.md`（System32 msvcp140.dll 版本过旧，需应用本地 14.44 三件套，2026-08-11 已修好）。
2. **手机**：
   - 开启**开发者选项 + USB 调试**（设置→关于手机→连点版本号 7 次）。
   - USB 连电脑，`adb devices` 确认设备出现（SDP 走 ADB 通道）。
   - **建议 userdebug/eng 系统**（功耗指标全量才放得开；release 系统部分 counter 不可用）。
3. **测试设计**（发热定位的前提）：
   - **固定场景**：同一关卡/同一操作序列（如跑图 5 分钟 / 连续战斗），对比才有意义。
   - **连续 ≥10 分钟**：热节流（thermal throttling）要几分钟才暴露，抓 30 秒看不出发热问题。
   - **对照组**：低画质 vs 高画质 / 关某项特性 vs 开，A/B 各抓一段，看功耗估算差在哪。

## 二、Realtime 模式功耗分析（核心步骤）

> Realtime 是功耗分析的**主战场**——实时看到各模块频率/负载/功耗估算 + 温度。

1. **启动 SDP → 连接设备**：Connect → 选中手机 → OK。
2. **New Realtime Capture**：顶部 Create New → Realtime Capture。
3. **添加指标**（左侧 Categories 树勾选）：
   - **Power 类别**：功耗估算相关指标（以你本机列表为准，不同代际名字略不同，认准含 Power/Energy 的项）。
   - **Thermal 类别**：各 thermal zone 温度（`tsens_*` 等），发热直接证据。
   - **CPU 类别**：各核 `scaling_cur_freq`（当前频率）+ `load`（负载）→ 看有没有核长期锁高频。
   - **GPU 类别**：`% Utilization` + 频率 → 看 GPU 是否持续满载（98-100% = GPU 在猛烧）。
4. **开始采样** → 在手机上跑目标场景 ≥5 分钟 → 停止。
5. **判读**（见第三节对照表）。

## 三、关键指标判读对照表

| 观察到的现象 | 含义 | 下一步 |
|---|---|---|
| 某大核频率**长期锁最高档** + 负载高 | CPU 在猛烧（发热主嫌之一） | 看是不是线程忙等/轮询/算法重；Scheduling 看哪个线程 |
| GPU `% Utilization` **持续 98-100%** | GPU 满载 → 渲染负载是功耗主因 | 回 UE Insights 看 GPU pass 热点（本手册第六节联动） |
| GPU 利用率低但**频率高** | 频率没降下来 / 没进省电档 | 查驱动 DVFS 是否被锁（如性能模式/profile 强制） |
| **温度曲线**持续爬升到节流点（如 85°C+） | 触发温控降频 → 帧率会跟着掉 | 先降功耗，别只看帧率 |
| 功耗估算某模块占比异常高 | 该模块是功耗大户 | 针对性优化（见第六节） |
| 各指标都正常但整机发热 | 屏幕/充电/后台/基带等系统侧功耗 | 换工具：Perfetto 看全系统调度 + `dumpsys battery` |

**⚠️ 判读铁律**：
- 功耗估算值是**相对参考**，别当精确数字报给需求方；要精确值用电流计。
- 单看一个 counter 下不了结论，必须**实验法验证**（关掉嫌疑项，看功耗估算/温度是否下降）。
- 温度曲线是发热的**硬证据**，比任何估算都可靠——先看温度，再反推功耗。

## 四、Trace 模式功耗分析（A/B 对比）

1. **New Trace Capture** → 勾选需要的组件（GPU / CPU / Thermal / Power 相关 trace 项）。
   - ⚠️ Trace 有开销（SDP 自带 ~5% CPU），功耗分析够用即可，别全勾。
2. 抓两段对照（如：开 LuxGI vs 关 LuxGI，各 60s），时间轴对齐看：
   - GPU 活跃段 vs 功耗估算段是否重叠 → 定位"哪个时段在烧电"。
   - 温度爬升的起点对应哪段操作 → 发热源头。
3. Trace 里同样可以展开 GPU Rendering stages 看 pass 级耗时（与 Realtime 互补）。

## 五、发热排查标准流程（照着走）

```
手机发热
  ↓ ① 先摸温度：adb shell cat /sys/class/thermal/thermal_zone*/temp
  ↓ ② SDP Realtime 抓 5-10 分钟（Power + Thermal + CPU 频率 + GPU 利用率）
  ↓ ③ 温度持续爬升？ → 是：有真实功耗问题
  ↓                         否：只是瞬时热/环境热，别过度优化
  ↓ ④ 看 CPU 核频率：有大核锁最高频？ → CPU 侧功耗问题
  ↓ ⑤ 看 GPU 利用率：持续 98-100%？  → GPU 渲染负载问题
  ↓ ⑥ 实验法验证：关嫌疑项 → 温度回落 = 实锤
  ↓ ⑦ 深挖：CPU 侧用 Perfetto/调度；GPU 侧回 UE Insights 看 pass / AOC 看 shader
```

## 六、与 UE 性能分析的联动（你的实际场景）

发热 90% 是渲染负载 + 锁频造成的，SDP 定位完"GPU 满载"，下一步就回到引擎侧：

| 层 | 工具 | 看什么 |
|---|---|---|
| 系统功耗 | SDP（本手册） | 功耗估算、温度、频率、GPU 利用率 |
| 引擎 GPU pass | UE Insights / `profilegpu` | 哪个 pass 吃掉 GPU 时间（如你前面 Queue Present 分析里 Frame 26ms 的热点） |
| Shader 级 | AOC（`sdp-gpu-hotspot-profiling` skill） | 指令数/寄存器压力 → 为什么这个 shader 烧 GPU |
| DrawCall 级 | RenderDoc / SDP Snapshot | 具体哪个 draw 最贵 |
| 全系统调度 | Perfetto | 后台进程抢 CPU、温控策略、锁频来源 |

**联动案例**：SDP 显示 GPU 利用率 99% + 温度 88°C → UE Insights 展开 Frame 发现 Shadow pass 占 12ms → AOC 看阴影 shader 寄存器压力高 → 降阴影分辨率/换 PCF 方案 → 重测 SDP 温度回落。每一环都有工具验证，别跳步。

## 七、常见坑（都实测/踩过）

1. **别拿功耗估算当精确值**——能量模型是"估算"，不同设备模型校准不同，横向对比只能看相对趋势。
2. **抓太短**——发热是热积累过程，≥10 分钟才暴露节流；抓 30 秒说"不热"是假结论。
3. **release 系统 counter 不全**——功耗/GPU 部分指标需要 userdebug/eng，没有就如实说拿不到，别编。
4. **SDP 有 ~5% CPU 开销**——功耗分析结果含工具自身开销，对比时保持工具状态一致。
5. **Vulkan trace 冲突**——SDP 的 Vulkan interception 与 UE Vulkan 初始化可能冲突（游戏崩/Vulkan 不支持）：不勾 Vulkan Trace metrics / 先启动游戏再连 / 关 Vulkan validation layer。
6. **只看帧率不看温度**——节流后帧率掉的根因是温度，SDP 里温度曲线和帧率一起看，别把温控降频误判成优化出问题。
7. **发热≠纯渲染**——屏幕亮度、充电、5G 基带、后台 App 都烧电。先 `dumpsys battery` 排除系统侧，再归因渲染。

## 八、快查命令速记

```bash
# 设备在线
adb devices
# 实时电流（μA，真实值，最硬）
adb shell cat /sys/class/power_supply/battery/current_now
# 电池电压/温度
adb shell cat /sys/class/power_supply/battery/temp
# 各核当前频率（Hz）
adb shell cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq
# 各 thermal zone 温度
adb shell cat /sys/class/thermal/thermal_zone*/temp
# 电池电量信息（含温度/电压/电流汇总）
adb shell dumpsys battery
```

---

## 参考

- SDP 官方文档：https://docs.qualcomm.com/nav/home/sdp.html （Realtime 支持 CPU/GPU/EGL/Memory/Power/Thermal/Network 指标类别；Trace 支持 Vulkan/GLES/DSP/CPU/Kernel 等组件）
- 本机 User Guide：`C:\Program Files\Qualcomm\Snapdragon Profiler\doc\Snapdragon Profiler User Guide.pdf`
- SDP 启动崩溃修复：`E:\AiDoc\Snapdragon-Profiler-启动崩溃-msvcp140-Runtime版本不兼容排查修复指南.md`
- GPU 热点定位工作流（SDP counter 阈值 + AOC + RenderDoc）：`C:\Users\djangozhang\.workbuddy\skills\sdp-gpu-hotspot-profiling\SKILL.md`
- 引擎侧性能分析：`E:\AiDoc\UE-Insights-Queue-Present耗时定位-GPU瓶颈判断.md`（GPU bound 判定 + pass 热点思路）
