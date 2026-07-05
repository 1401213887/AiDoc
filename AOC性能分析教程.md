# AOC 性能分析教程

# AOC 性能分析教程

理解 Adreno Offline Compiler 原理、从 UE5 获取 Shader、逐字段读懂 AOC 输出做性能分析

整理于 2026-06-30 · 适用 UE5 + 高通 Adreno · AOC v7.0.5 / Compiler E17.51.10.00

本教程分三部分：**第一部分**讲 AOC 是什么、工作原理与下载方式；
**第二部分**讲怎么从 Unreal Engine 拿到一份 shader 喂给 AOC；
**第三部分**逐字段拆解 AOC 的输出（指令统计 / 资源占用 / 性能投影），教你看懂每个指标的性能含义与优化抓手，最后用一份真实的 DeferredShading PS 三架构实测收尾。

第一部分 · AOC 原理与下载

## 1. AOC 是什么

**Adreno Offline Compiler（AOC）** 是高通官方提供的**离线着色器编译器**，它把 shader（SPIR-V / GLSL / HLSL）针对指定的 Adreno GPU 架构编译成**硬件原生指令（ISA）**，并打印出两类报告：

① Shader Stats（静态指令统计）

编译出了多少 ALU / 纹理 / 流控指令，用了多少寄存器、有没有 spill。描述"编译出了什么样的硬件指令"。

② Performance Projection（性能投影）

预测这些指令跑起来周期花在哪、哪里 stall、occupancy 多少。描述"这些指令跑起来会怎样"。

它的核心价值：**不用真机、不用跑完整游戏**，就能在编译期看出一个 shader 在某颗 Adreno 上的指令规模、寄存器压力和并发能力，是改材质 / 改 shader 时最快的"性能体检"手段，也非常适合接进 CI 做回归。

**定位**AOC 是**离线、静态、单 shader** 的分析工具。它和 Snapdragon Profiler（真机系统级 counter）、RenderDoc（帧结构调试）三者分工互补——AOC 看"这个 shader 编译出来贵不贵"，SDP 看"真机上整帧瓶颈在哪"，RenderDoc 看"这一帧的绑定/资源/像素历史"。

## 2. 工作原理与数据流

AOC 内部就是高通驱动里那套在线编译器的离线版本。一次编译的数据流：

输入

SPIR-V / GLSL / HLSL 一份 shader

-arch 指定目标

a650 / a740 / a750…

编译成 Adreno ISA

分配寄存器 / 调度指令

输出报告

Shader Stats + 性能投影

- **同一份输入，`-arch` 不同 → 输出不同。**因为不同代 Adreno 的 ISA、寄存器文件大小、调度规则不一样，AOC 会按目标架构重新分配寄存器、调度指令。这正是"对比不同手机"的技术基础。
- **分 Preamble 和 Main Shader 两段统计**（详见第 13 节）：前者是固定前导开销，后者是逐像素/逐顶点主体——性能分析只看 Main。
- **它是"投影(projection)"不是"实测"。**性能投影是编译器基于指令调度模型估算的，不含真机频率、带宽、cache 命中等运行时因素（局限详见第 20 节）。

**一句话记住**AOC 给的是**编译期/寄存器层面的相对趋势**——能可靠告诉你"改完比改前是好是坏""哪颗架构寄存器更紧张"，但绝对帧时间要靠 Snapdragon Profiler 真机抓。

## 3. 用 AOC 对比"不同手机"

这是最容易误解的一点。用 AOC 验证**不同手机之间的差别**，并不是去每台真机里把它各自编译好的 shader 掏出来对比，而是：

**同一份 shader 源，分别用不同的 `-arch` 参数编译，对比各架构上的指令数 / 寄存器占用 / 纹理开销 / occupancy。**
shader 源码是同一份，差别完全来自 AOC 针对不同 Adreno 架构（a650 / a740 / a750…）的编译结果。

所以整个任务拆成两步：① 从 UE 拿到 **一份** shader（第二部分）；② 用不同 `-arch` 分别编译并读懂输出（第三部分）。

取一份 SPIR-V→
AOC 多 arch 编译→
对比指令数 / 寄存器 / occupancy

## 4. AOC 接受的 shader 格式

AOC 支持三种输入，从 UE 拿货的难易度不同。目标是 Vulkan，**首选 SPIR-V**。

| 格式 | UE 后端 | 取货难度 | 说明 |
| --- | --- | --- | --- |
| .spv SPIR-V | Vulkan | 推荐 | 二进制，AOC 直接支持。Vulkan 手游的真实产物，对比结果最贴近线上。 |
| .frag/.vert GLSL ES | OpenGL ES | 可选 | HLSLcc 交叉编译出的文本，最易读。若目标是 GLES 后端选它。 |
| .usf / HLSL 源 | — | 不推荐 | UE 的 .usf 带大量宏和 include，AOC 难直接吃，不要走这条路。 |

**结论**Vulkan 场景：拿 SPIR-V（`.spv`）喂 AOC。AOC 文档明确支持 compiled SPIR-V 输入。

## 5. 命令与 dump 级别

三个 `-dump` 级别决定打印多少：

| 选项 | 打印内容 |
| --- | --- |
| -dump=stats | 仅静态指令统计（Shader Stats） |
| -dump=stats\_perf | 静态统计 + **性能投影**（做性能分析一直用这个） |
| -dump=all | 上述全部 + 生成的硬件指令序列（ISA）+ 源码（若是文本） |

```
# 标准命令（注意 -arch/-api/-dump 用等号，-entry_point_fs/-fs 用空格跟值）
aoc.exe -arch=a740 -api=Vulkan -dump=stats_perf -entry_point_fs main_xxx -fs shader.spv
```

**参数语法坑（实测踩过）**

- `-arch` / `-api` / `-dump` 用 **等号**。
- `-entry_point_fs` / `-fs` 是 **空格跟值，不能用等号**。
- `.spv` 不带 `.fs.spv` 后缀时，必须用 `-fs` 显式声明它是 fragment shader。
- entry point 不是 `main` 时（UE 常是 `main_xxxxx`），必须用 `-entry_point_fs` 指定。

## 6. AOC 下载（公司内务必看）

**个人 Qualcomm 账号下不了！**AOC 在高通 QPM 平台属于受限工具，**个人注册账号没有下载权限**。必须用**公司（tencent）邮箱**注册 / 绑定 Adreno 开发者账号，走企业授权后才能下载。

1. 下载地址（QPM，Qualcomm Package Manager）：
   `https://qpm.qualcomm.com/#/main/tools/details/Adreno_GPU_Offline_Compiler`
2. 用 **tencent 公司邮箱**注册 / 登录 Qualcomm Adreno 开发者账号（个人邮箱账号无权限，页面会显示无法下载 / access denied）。
3. 登录后在该页面下载 QPM 客户端，再通过 QPM 安装 Adreno GPU Offline Compiler 包；安装完在安装目录下找到 `aoc.exe`（默认 `C:\Program Files\Qualcomm\Adreno Offline Compiler\aoc.exe`）。

**提示**如果公司已有统一的高通企业账号 / 内部镜像，直接找对应负责同学要安装包更快，省去 QPM 授权流程。SDP（Snapdragon Profiler）同样走这个 QPM 平台分发。安装目录下的 `OfflineCompiler.html` 是字段定义的最权威来源（第三部分即据此整理）。

第二部分 · 如何从 UE 获取 Shader

## 7. 两条取 SPIR-V 路径：RenderDoc vs UE dump

拿到那"一份 shader"有两条路，各有优劣。**核心区别：RenderDoc 抓的是真机实际提交的特化(specialized) SPIR-V，UE dump 拿的是离线编译产物。**

| 对比项 | 路径A：RenderDoc | 路径B：UE dump |
| --- | --- | --- |
| 来源 | 真机运行时实际提交给驱动的 SPIR-V | UE 离线编译 / cook 出的中间产物 |
| permutation 准确性 | 高 抓的就是当前画面用的那个变体 | 需自己对 全量里要找对应 hash |
| spec constant / 特化 | 已特化，贴近线上真实 | 未必特化到具体值 |
| 能否定位"哪个 DrawCall" | 能 直接选中那次 DrawCall 的 shader | 不能，只有材质维度 |
| 需要真机 | 要（连真机抓帧） | 不要（编辑器/命令行即可） |
| 适合场景 | 定位"线上这一帧这个 DrawCall 为啥慢" | 批量/CI、想覆盖所有 permutation |

**建议**已经在用 RenderDoc 的话，**优先走路径A**——抓真机帧、选中可疑 DrawCall、导出它的 SPIR-V，最贴近线上真实负载。路径B 适合做全量/CI 批扫。两条路拿到的都是 `.spv`，后面喂 AOC 的步骤完全一样。

## 8. 路径A：RenderDoc 抓真机帧导 SPIR-V（推荐）

### 8.1 反汇编：能看到什么、看不到什么

| 层级 | RenderDoc 能否反汇编 | 说明 |
| --- | --- | --- |
| SPIR-V 汇编 | 能 | Pipeline State 选中 shader stage 即可看内置反汇编；也可 Save 出 `.spv` 用 `spirv-dis` 自己反汇编。看逻辑用。 |
| Adreno 原生 ISA（a6xx/a7xx 机器码） | 不能 | 高通驱动私有，RenderDoc 拿不到、也反汇编不出。**真机指令数/寄存器只能靠 AOC。** |

**关键认知**SPIR-V 是平台无关的**中间表示**，它的"指令条数"不等于 Adreno 上的真实指令数。想要架构级真相（Total instruction count / Register footprint / Texture read），必须把导出的 `.spv` 喂给 AOC。RenderDoc 反汇编只用来"读懂 shader 在干啥"。

### 8.2 操作步骤

1. RenderDoc 连真机（Android + Vulkan），抓一帧。
2. 在 Event Browser 里选中怀疑慢的那次 **DrawCall**。
3. 切到 **Pipeline State**，点对应的 shader stage（VS / FS）。
4. 面板里能直接看 SPIR-V 反汇编；点 **Save**（或 Edit → 导出）把 SPIR-V 二进制存成 `.spv`。VS、FS 各存一份。
5. 把这两个 `.spv` 直接拿去喂 AOC（见第 12 节），无需任何转换。

**导原始 .spv 的注意**RenderDoc GUI 面板里看到的是**反汇编文本（伪 C / SPIR-V 汇编）**，这种文本**不能**被 `spirv-as` 汇编回 `.spv`、也不能直接喂 AOC。要拿原始 SPIR-V 二进制字节流，用 RenderDoc 的 **Python Shell**：`pipe.GetShaderReflection(stage).rawBytes` 即 UE 提交给驱动的原始 SPIR-V（文件头魔数 `0x07230203`），写成 `.spv` 即可。

## 9. 路径B：开启 UE shader dump

编辑配置文件（优先项目级，避免影响其他项目）：

```
# [项目]/Saved/Config/Windows/ConsoleVariables.ini
# 或引擎级：Engine/Config/ConsoleVariables.ini
```

在 `[Startup]` 段下加入：

```
[Startup]
; dump 所有编译的中间 shader 文件到 Saved/ShaderDebugInfo
r.DumpShaderDebugInfo=1
; 保留调试信息，让产物可读、便于定位
r.Shaders.KeepDebugInfo=1
; 额外生成可直接重编的命令行 bat
r.DumpShaderDebugWorkerCommandLine=1
; 可选：详细的 shader 编译日志
r.ShaderDevelopmentMode=1
```

**关于 r.Shaders.Optimize 的取舍（重要）**

- 若只想**看可读源码**：可加 `r.Shaders.Optimize=0`，源码更清晰。
- 若想拿**接近真机的指令数 / 寄存器去对比**：保持 `r.Shaders.Optimize=1`（默认）。真正的对比数据来自 **AOC 的输出**，AOC 会对喂进去的 shader 再做架构相关的优化编译。dump 阶段关优化会让源码膨胀，但不影响 AOC 最终结论。

**注意**修改 console variable 默认**不会**触发 shader 失效重编，需要手动强制重编（见第 10 节）。

## 10. 路径B：切平台 + 强制重编

### 10.1 切到 Android / Vulkan 平台

必须让 UE 编译**移动端 Vulkan** 的 shader，而不是桌面 D3D 的。两种方式：

- **编辑器内**：设置预览渲染级别 / cook 平台为 `Android Vulkan`（Settings → Preview Rendering Level，或在 Platforms 菜单选 Android）。
- **命令行 cook**（更干净，推荐做批量分析）：

  ```
  UnrealEditor-Cmd.exe YourProject.uproject -run=Cook -targetplatform=Android_Vulkan
  ```

确认项目已启用 Vulkan：`Project Settings → Platforms → Android → Build → Support Vulkan` 勾上。

### 10.2 强制重编 shader

三选一，按需要的范围选：

| 范围 | 做法 | 适用 |
| --- | --- | --- |
| 全量 | 改 `Engine/Shaders/Public/ShaderVersion.ush` 里的 GUID（换一个新的），重启编辑器 | 想 dump 全部 shader |
| 包含某文件的 | 在目标 `.usf` 加一行： `#pragma message("UESHADERMETADATA_VERSION <新GUID>")` | 调某个 global shader |
| 单材质（推荐） | 打开材质编辑器 → 随便动一下节点 → 点 **Apply** | 只看某个材质，文件少，最干净 |

**建议**做架构对比时，**只针对关心的那个材质** 走"单材质"方式。否则全量 dump 会在硬盘里塞进成千上万个小文件，找文件都费劲。

## 11. 定位并取出 shader 文件

dump 产物位于：

```
[项目]/Saved/ShaderDebugInfo/
```

目录结构按 `平台 / 材质 / 顶点工厂 / shader类型` 分层，每个最终目录里通常包含：

- shader 源文件（预处理后的 `.usf` / 交叉编译出的 GLSL）
- 编译产物（Vulkan 后端会有 SPIR-V）
- 一个重编用的 `.bat`（配合 `ShaderCompileWorker -direct` 可单独重跑）
- `OutputHash.txt` — shader hash，用于和崩溃日志 / RenderDoc 对应

**怎么确认拿到的是 SPIR-V**SPIR-V 是二进制，文件头魔数为 `0x07230203`。可用 `spirv-dis`（Vulkan SDK 自带）反汇编确认，或直接把 `.spv` 喂 AOC 验证。若 dump 出来的是 GLSL 文本而你需要 SPIR-V，用 Vulkan SDK 的 `glslangValidator -V shader.frag -o shader.frag.spv` 转一下即可。

## 12. 多架构编译命令

**无论 shader 来自路径A（RenderDoc）还是路径B（UE dump），到这一步操作完全一样**——同一份文件分别用不同 `-arch` 编译并把输出存档：

```
# SPIR-V 输入（Vulkan 场景）
aoc.exe -arch=a650 -api=Vulkan -dump=stats_perf -entry_point_fs main_xxx -fs shader.spv > a650.txt
aoc.exe -arch=a740 -api=Vulkan -dump=stats_perf -entry_point_fs main_xxx -fs shader.spv > a740.txt
aoc.exe -arch=a750 -api=Vulkan -dump=stats_perf -entry_point_fs main_xxx -fs shader.spv > a750.txt

# GLSL ES 输入（若走 GLES 后端）
aoc.exe -arch=a650 file/*.frag file/*.vert
```

**架构支持以手上 AOC 版本为准**较老版本官方列出支持 `A650 / A660 / A730 / C510 / C511`。a740(8Gen2) / a750(8Gen3) 需较新版 AOC 才支持，**若 `-arch=a750` 报不支持，用同代际最接近的架构替代**，或升级 AOC。先跑 `aoc.exe -h` 看本机支持列表（实测 a740 别名含 c510/c511，a750 含 c520）。

拿到三份输出后，怎么逐字段读懂它们，就是第三部分的内容。

第三部分 · 读懂 AOC 输出字段做性能分析

AOC 输出的两大块——**Shader Stats（静态指令统计）** 和 **Performance Projection（性能投影）**——各自又分
**Preamble（前导）** 和 **Main Shader（主体）**。下面逐字段给出官方定义 + 实战解读。

## 13. Preamble vs Main Shader

AOC 把每个 shader 拆成两段统计：

| 段 | 含义 | 关注度 |
| --- | --- | --- |
| Shader Preamble Stats | 「前导/序言」——加载常量、descriptor、初始化等准备性指令，每次调用执行一次的固定开销 | 次要 |
| Main Shader Stats | 着色器主体——真正的逐像素/逐顶点计算逻辑。**性能分析几乎只看这块** | 核心 |

下文所有字段定义对两段通用，但优化时以 Main Shader 的数字为准。

## 14. 指令统计字段（Instruction Stats）

| 字段 | 官方定义 | 性能含义 / 如何改进 |
| --- | --- | --- |
| Total instruction count | 所有指令总数。 | 指令越多执行时间越长；超过指令缓存(I$)大小会 I$ miss 拖慢性能。**但它会误导**——ALU 很快，总数高不一定差。 改进：避免冗余操作。 |
| ALU instruction count - 32 bit | 所有 32-bit (full) ALU 指令总数。 | ALU 多不一定影响性能，但更费电。 改进：删冗余计算。 |
| ALU instruction count - 16 bit | 所有 16-bit (half) ALU 指令总数。 | 关键 16-bit 指令**更快、占寄存器更少**。把 32-bit 转 half 能提升 ALU-bound shader 性能。 改进：尽量用低精度，提高 16-bit 占比。 |
| Complex instruction count - 32/16 bit | 复杂指令（sin、cos、rcp、rsqrt 等超越函数）总数，由 EFU 单元执行。 | EFU 比 ALU 慢，且需要 short latency sync 处理依赖。 改进：必须用时把多个 EFU 指令**适度**分组，减少 short sync；勿过度分组。 |
| Flow control instruction count | 所有流控制指令（分支/循环跳转）总数。 | 流控越多 → 代码发散越严重 → 伤性能；一条流控比一条 ALU 更耗时。 改进：减少控制块内指令，让编译器能 flatten（拍平成无分支）。 |
| Barrier and fence instruction count | 所有屏障/栅栏（全局同步）指令总数。 | 全局同步降低 wave 并行度、拉长执行时间、更费电。 改进：避免频繁全局同步。 |
| Short latency sync count | 短延迟同步指令总数。 | 若它离触发它的指令太近、又没有别的 wave 掩盖，会延迟执行。 改进：在不过度拉大 def-use 距离前提下把 EFU 指令放一起。 |
| Long latency sync count | 长延迟同步指令总数。 | 重点 由**内存访问**引起；延迟长且 wave 不够时会卡住执行。 改进：提升内存指令的局部性(locality)。 |
| Texture read count | 所有纹理读取指令总数。 | 纹理 fetch 造成访存延迟，需用 ALU 掩盖。延迟由 fetch 数量和 cache 局部性决定。 改进：可合并的纹理读放一起避免 cache thrashing，每组 fetch 控制在 15 以下。 |
| Memory read / write count | 所有内存读 / 写指令总数（不同于纹理单元的访存）。 | 类似纹理；分散的写到连续地址会伤性能。 改进：尽量用 vector store，把写到连续地址的指令分组。 |
| Miscellaneous instruction count | 上述未单列的其它指令总数。 | — |

## 15. 资源占用字段（最关键）

| 字段 | 官方定义 | 性能含义 / 如何改进 |
| --- | --- | --- |
| Full precision register footprint | 每个 shader 实例用的 **128-bit 寄存器**数（每个可存 4 个 FP32）。 | shader 需要的全精度寄存器数量。 改进：用低精度变量避免高寄存器占用。 |
| Half precision register footprint | 每实例用的 **64-bit 寄存器**数（每个可存 4 个 FP16）。 | 实际占用通常取 `max(Full, Half)` 并按全精度寄存器计数。 改进：用 half 变量，避免过度混精度运算。 |
| Overall register footprint 最核心 | 每实例用的 128-bit 寄存器数（每个存 4×FP32 或 8×FP16）。 | **移动端最关键指标，常比 ALU 总数更能决定性能。** 寄存器用太多 → 活跃 wave 数下降 → 可能 register spill。活跃 wave 多才能掩盖访存延迟；wave 少则延迟暴露、ALU 利用率低。 改进：避免带动态访问的大向量；定义靠近使用处；能用常量数组就别先声明再赋值。 |
| Scratch memory usage 红线 | 每实例用的 scratch memory（128-bit 槽）数量。 | 官方警告：只要用了任何 scratch memory，性能就会很差。 这是寄存器装不下、溢出(spill)到显存的信号，理想值必须是 **0**。 |
| Loop count | shader 中的循环数量。 | — |
| Output / Input component count | 该 shader 阶段所有输出 / 输入分量(component)的总数。 | —（间接反映 varying / IO 带宽） |
| ALU fiber occupancy % 综合结果 | 该 shader 能达到的**最大 ALU fiber 占用率**。 | 官方警告：此值低则 shader 可能性能差。 越高 = 并行度越高、越能掩盖延迟；越低 = 访存延迟暴露、ALU/其它单元闲置。 改进：**降低寄存器使用**来提高它（最直接抓手）。 |

## 16. 性能投影 — 延迟掩盖（Latency Hiding）

仅在 `-dump=stats_perf` 或 `all` 时打印。「execution cycles」= 有该类指令在跑的周期占比；「exposed」= 因该类指令**真正卡住**的 stall 周期占比（越高越糟）。

| 字段 | 官方定义 | 含义 |
| --- | --- | --- |
| Cycles with short latency sync | 任意 wavefront 中有 short latency sync 指令执行的周期占比。 | 占比越高越易 stall。改进：避免复杂运算、用低精度、变量定义靠近使用。 |
| Cycles with exposed short latency sync | 由 short sync 造成的 **stall** 周期占比。 | 同周期没别的指令可执行来掩盖时，GPU 就停了。 |
| Cycles with long latency sync | 任意 wavefront 中有 long latency sync 指令执行的周期占比。 | 占比高易 stall。改进：少用内存/纹理指令，能用常量/local memory 替代，提升局部性。 |
| Cycles with exposed long latency sync 主瓶颈指标 | 由 long sync 造成的 **stall** 周期占比。 | **这个最该盯**——没有足够 wave 掩盖访存延迟时 GPU 空转。降它的根本办法是提 occupancy（降寄存器）。 |

## 17. 性能投影 — 性能统计（Performance Stats）

各类指令「占用执行周期」的比例，用来判断 shader 是哪种 bound。四项之和不一定 100%（同周期可多类并行）。

| 字段 | 含义 |
| --- | --- |
| Cycles with ALU instructions | 有 ALU 指令执行的周期占比。高 → 偏 ALU-bound。 |
| Cycles with Complex instructions | 有 EFU（复杂/超越函数）指令执行的周期占比。 |
| Cycles with Memory instructions | 有内存指令执行的周期占比。 |
| Cycles with Texture instructions | 有纹理指令执行的周期占比。 |

## 18. 实测解读：DeferredShading PS（三架构对比）

用上面的字段含义，回看实跑的 `DeferredShadingPS.spv`（UE5 Mobile clustered deferred + Toon + LuxGI 合成 PS）在三颗 Adreno 上的 Main Shader 数据：

| 字段 | A650 (6xx 代) | A740 (8Gen2) | A750 (8Gen3) | 解读 |
| --- | --- | --- | --- | --- |
| Total instruction | 24926 | 24875 | 24988 | 三者持平，差异属编译噪声 |
| ALU 32-bit | 7965 | 7987 | 7971 | 持平 |
| ALU 16-bit | 1595 | 1592 | 1601 | half 占比仅 ~17%，有 half 化空间 |
| Texture read | 158 | 158 | 158 | 持平（GBuffer+LUT+probe 采样） |
| Overall GPR | 46 | 44 | 44 | A650 略高 |
| Scratch memory | 14 ❌ | 0 ✓ | 0 ✓ | **分水岭**：A650 寄存器溢出到显存，7xx 零溢出 |
| ALU fiber occupancy | 12% | 25% | 25% | 7xx 并发能力是 6xx 的 2 倍 |
| Exposed long latency | 50.9% | 45.6% | 45.6% | 三者都偏高=共同瓶颈；A650 更糟 |

**核心结论**真正的鸿沟在 **6xx → 7xx 这一代微架构**（occupancy 翻倍、消除了寄存器 spill）；而 7xx 内部的
**8Gen2 → 8Gen3 在编译/寄存器层面几乎无差异**（同 SP 微架构）。三者共同瓶颈都是 exposed long latency 偏高 →
occupancy 不足 → 访存延迟掩盖不住。

**这个 PS 的优化优先级**① 降 Overall GPR 提 occupancy（拆 shader、缩短变量生命周期）→ ② 把 7987 条 32-bit ALU 尽量 half 化 → ③ 减少纹理/probe 采样的依赖链。三代通用。

## 19. 机型 ↔ Adreno 架构对照表

| SoC | Adreno GPU | AOC -arch | 代表机型 |
| --- | --- | --- | --- |
| 骁龙 8 Gen 1 | Adreno 730 | `a730` | 小米12 / 一加10 Pro |
| 骁龙 8+ Gen 1 | Adreno 730 | `a730` | 小米12S / 红米K50至尊 |
| 骁龙 8 Gen 2 | Adreno 740 | `a740` | 小米13 / 一加11 / iQOO11 |
| 骁龙 8 Gen 3 | Adreno 750 | `a750` | 小米14 / 一加12 / iQOO12 |
| 骁龙 888 / 865 | Adreno 660 / 650 | `a660` / `a650` | 小米11 / 小米10 |

**低端机也别忽略**真正卡的往往是 a610 / a619 这类中低端（红米 Note 系列、低端千元机）。如果目标用户覆盖低端机，建议把这些也加进对比——它们寄存器更紧张，最容易暴露问题。这些架构是否被你的 AOC 版本支持，同样以 `aoc.exe -h` 为准。

## 20. 必须牢记的局限 + 常见坑

**AOC 是离线静态投影，不是真机实测**

- 它反映的是**编译期/寄存器层面**的特征（指令数、GPR、occupancy 投影），能看出代际微架构差异（如 A650 的 spill）。
- 但**频率、内存带宽、L2/UCHE 容量、真实 cache 命中、实际 occupancy** 它都看不到——8Gen3 相对 8Gen2 的真机帧率优势主要在这些运行时层面。
- AOC 编译器版本 ≠ 真机驱动在线编译器版本，绝对数字会有出入。
- **正确用法：当相对趋势/优化抓手（改材质前后对比），真机绝对值以 Snapdragon Profiler 设备端抓取为准。**
- 官方也强调：**没有单一指标能判定好坏**，需结合 shader 类型综合看 register footprint + occupancy + 访存类指标。

| 坑 | 说明 / 规避 |
| --- | --- |
| 个人账号下不到 AOC | QPM 上 AOC 是受限工具，个人 Qualcomm 账号无权限。**必须用 tencent 公司邮箱**注册/绑定 Adreno 账号，或找公司有企业账号的同学要安装包。 |
| AOC 参数语法 | `-arch/-api/-dump` 用等号；`-entry_point_fs/-fs` 用空格跟值；entry 非 main 必须显式指定。 |
| RenderDoc 反汇编文本喂不进 AOC | 面板里的伪 C/汇编文本不能 spirv-as 回 .spv。要原始二进制走 Python Shell 的 `GetShaderReflection(stage).rawBytes`。 |
| dump 出来是 D3D shader | 没切到 Android 平台。确认预览级别 / cook targetplatform 是 Android\_Vulkan。 |
| 改了 cvar 却没 dump | cvar 改动不触发重编，必须手动强制重编（改 GUID / 动材质 Apply）。 |
| 全量 dump 撑爆硬盘 | 长期开 `r.DumpShaderDebugInfo=1` 会产生海量小文件，分析完记得关掉。优先用单材质方式。 |
| `-arch=a750` 报不支持 | AOC 版本旧。升级 AOC，或用同代际最接近架构替代。先 `aoc.exe -h` 看支持列表。 |
| 以为 RenderDoc 能反汇编 Adreno 机器码 | 不能。它只到 SPIR-V 层。Adreno 原生 ISA 是驱动私有，真机指令数/寄存器只能靠 AOC。 |
| 把 AOC 数字当成绝对真机性能 | AOC 给的是静态分析，是相对趋势参考，不等于真机帧时间。结论要靠 SDP 真机实验法复现验证。 |
| 臆造对比数据 | **严禁**。读不到/AOC 没输出某项，如实标"待测"，绝不编数字。 |

**SDP 已内置 AOC**Snapdragon Profiler 在 Snapshot 模式下双击 DrawCall，左下 Shader Analyzer 底层就是调 AOC。如果只是想看真机某帧某 DrawCall 的 shader 指令数，可以直接在 SDP 里看，不必手动跑命令行。手动 AOC 的价值在于**批量 / CI / 跨架构对比**。

合并整理于 2026-06-30，由《UE5-Shader导出-AOC多架构对比教程》+《AOC 输出字段完全详解》合并而成 · 适用 UE5 + 高通 Adreno。
字段定义逐字引自本机 `C:\Program Files\Qualcomm\Adreno Offline Compiler\OfflineCompiler.html`（AOC v7.0.5，文档更新 2026-06-10）。
实测数据来自 DeferredShadingPS.spv（RenderDoc 截帧 TDM8Gen3.rdc, EID 3796）在 a650/a740/a750 三 target 的 stats\_perf 输出。
配合《高通 SDP 工具使用教程》《高通 AdrenoGPU 最佳实践》一起看效果更好。
