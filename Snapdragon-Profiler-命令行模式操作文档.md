# Snapdragon Profiler 命令行模式操作文档 (qprof CLI)

# Snapdragon Profiler 命令行模式操作文档

qprof CLI — 无 GUI 自动化性能采集与离线分析指南

整理于 2026-06-25 · 适用于 Qualcomm Profiler CLI（qprof）· Android / Linux / QNX / Windows on Snapdragon


## 1. 命令行模式是什么 / 适用场景

Snapdragon Profiler（高通骁龙性能分析器，新版品牌名为 **Qualcomm Profiler**）除了图形界面外，还提供一个命令行工具 `qprof`。命令行模式让你可以在**没有图形界面**的情况下完成性能数据采集，特别适合以下场景：

🤖

自动化 / CI 集成

把性能采集写进脚本，每次出包后自动跑一轮，回归性能数据。

🖥️

无头服务器 / 远程设备

通过 SSH 或 adb shell 在没有桌面的环境直接采集。

⏱️

精确可控的采集

指定采集时长、采样率、能力集，结果可复现，便于横向对比。

📊

多工具协作分析

导出 trace / ETW 格式，交给 Perfetto 或 Windows Performance Analyzer 离线分析。

**核心工作流**

配置服务器 IP→
查询 capabilities→
qprof --profile 采集→
导出 trace/etw→
Perfetto/WPA 分析

**关于工具命名的说明**
「Snapdragon Profiler」是经典版（GUI 为主）的名称；高通新一代统一品牌为「Qualcomm Profiler」，其命令行工具即 `qprof`。本文所有命令行能力均以官方 `qprof` CLI 文档为准。经典版 Snapdragon Profiler 的 GUI 截帧/实时分析能力请参考工作区另一篇《高通 SDP 工具使用教程》。

## 2. 安装路径与可执行文件位置

命令行工具 `qprof` 随 Qualcomm Profiler 安装包一同安装。各平台默认安装位置如下：

| 平台 | CLI / 示例命令目录 |
| --- | --- |
| Windows (x86) | `C:\Program Files (x86)\Qualcomm\QualcommProfiler\CLI\` |
| Windows on Snapdragon (Arm) | `C:\Program Files (Arm)\Qualcomm\QualcommProfiler\CLI\` |
| Linux | `/opt/qcom/QualcommProfiler/CLI/` |

官方在 `CLI\documents\sample-commands\` 目录下提供了一组**示例命令与示例配置（config.json）**，可直接套用后按需修改：

| 平台 | sample-commands 路径 |
| --- | --- |
| Windows (x86) / Linux / Android / QNX | `C:\Program Files (x86)\Qualcomm\QualcommProfiler\CLI\documents\sample-commands\` |
| Windows on Snapdragon | `C:\Program Files (Arm)\Qualcomm\QualcommProfiler\CLI\documents\sample-commands\` |
| Linux | `/opt/qcom/QualcommProfiler/CLI/documents/sample-commands/` |

**建议**
把 `qprof` 所在目录加入系统 `PATH`，即可在任意位置直接调用 `qprof` 命令。

## 3. 环境变量配置（各平台）

在**设备端**（target）运行 qprof 时，通常需要先设置库路径环境变量。注意：**每开一个新的 adb shell / 设备 shell 都要重新 export 一次**。

Android（adb shell 内）

```
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/vendor/qprof/libs
```

Qualcomm Linux（设备 shell 内）

```
export PATH=/data/shared/QualcommProfiler/bins:$PATH
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/var/QualcommProfiler/libs
```

HGY（设备 shell 内）

```
export PATH=/data/shared/QualcommProfiler/bins:$PATH
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/data/shared/QualcommProfiler/libs
```

Ubuntu 主机（host 端）

```
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/qcom/Shared/QualcommProfiler/API/host-ubuntu/libs/
```

**路径可能因版本而异**
不同安装版本/芯片平台的库路径可能有差异，请以实际安装目录为准。若 `qprof` 启动报找不到 so 库，多半是 `LD_LIBRARY_PATH` 没配对。

## 4. 连接设备与服务器配置

Qualcomm Profiler 采用 **客户端（host）— 服务器（target 设备）**架构。客户端通过 IP + 端口与设备上运行的分析服务器通信。从 Windows (x86) 主机采集时，数据只能从 **Android 和 QNX** 设备流式传输。

### 4.1 配置服务器 IP 与端口

语法

```
qprof --configure --server-ip <ServerIP> --port <ServerPort>
```

示例

```
qprof --configure --server-ip 10.73.93.35 --port 62472
```

`--port` 为可选参数，仅当你确知另一个端口可用时才指定，否则可省略由工具默认处理。

### 4.2 查看当前服务器配置

```
qprof --get-server-info

Configured server IP : 10.213.121.106 port : 62472
```

**平台命令支持差异**

- `--configure`：**Windows on Snapdragon (WoS) 不支持**。
- `--get-server-info`：**Arm 上的 Windows 不支持**。
- `--launch-app`：仅在**设备客户端**上可用。

## 5. 查询可用能力（capabilities）

「capability（能力）」即可被分析的子系统 / IP 块。采集前先用下列命令列出当前设备支持的所有能力，再从中挑选需要的能力放进 `--capabilities-list`。

列出全部可用能力

```
qprof --capabilities
```

常见 capability 名称（按子系统分类）：

| 子系统 | Capability 名称 | 说明 |
| --- | --- | --- |
| CPU | `profiler:apps-proc-cpu-metrics` | 应用处理器 CPU 指标 |
| CPU (WoS) | `profiler:wos-apps-proc-cpu-metrics` | Windows on Snapdragon 的 CPU 指标 |
| DDR / 带宽 | `profiler:apps-proc-ddr-metrics` | 应用处理器 DDR 内存指标 |
| DDR / 带宽 | `profiler:bw-profiler-ddr-metrics` | 带宽分析器 DDR 指标 |
| GPU | `profiler:proc-gpu-specific-metrics` | GPU 专用指标 |
| DSP (NSP) | `profiler:nsp-dsp-metrics` | NSP DSP 指标 |
| DSP (aDSP) | `profiler:adsp-dsp-metrics` | aDSP DSP 指标 |
| 温度 (WoS) | `profiler:wos-apps-proc-thermal-metrics` | WoS 温度/热指标 |

可在一条命令里同时传入多个能力（如 GPU + CPU + NSP），用空格分隔。

**采样率限制（部分能力）**

- `apps-proc-ddr-metrics`：10–100 毫秒
- `wos-apps-proc-cpu-metrics`：50–200 毫秒
- `wos-apps-proc-thermal-metrics`：100–200 毫秒

## 6. 顶层命令与参数速查表

### 6.1 顶层命令

| 命令 | 含义 | 平台限制 |
| --- | --- | --- |
| `--capabilities` | 列出可用于分析的所有子系统和 IP 块 | — |
| `--profile` | 启用并把分析数据流式传输到客户端（核心采集命令） | — |
| `--configure` | 配置 Profiler CLI（服务器 IP/端口等） | WoS 不支持 |
| `--generate-config` | 为指定能力生成配置文件，可经 `--trace-options` 传给 profile | — |
| `--launch-app` | 在设备上启动应用程序 | 仅设备客户端 |
| `--get-server-info` | 查看当前 Profiler CLI 服务器配置信息 | Arm Windows 不支持 |

### 6.2 常用参数

| 参数 | 含义 | 取值示例 |
| --- | --- | --- |
| `--server-ip` | 分析服务器 IP 地址 | `10.73.93.35` |
| `--port` | 服务器端口（可选） | `62472` |
| `--capabilities-list` | 要采集的能力列表（空格分隔多个） | `profiler:apps-proc-cpu-metrics` |
| `--profile-type` | 分析类型 | `async` |
| `--profile-time` | 采集持续时间（秒） | `10` / `15` |
| `--streaming-rate` | 采样率（毫秒） | `200` / `500` / `1000` |
| `--result-format` | 结果输出格式 | `verbose` / `trace` / `etw` |
| `--file-format` | 文件格式 | `json` |
| `--live` | 实时把输出打印到标准输出 | （开关，无值） |
| `--metric-id-list` | 按 metric-id 筛选要采集的指标 | `6401` |

## 7. 采集数据：qprof --profile 详解

`--profile` 是命令行模式最核心的采集命令。一条完整的采集命令通常由这几部分组成：

| 组成部分 | 对应参数 | 作用 |
| --- | --- | --- |
| ① 启动采集 | `--profile` | 开始把数据流式传给客户端 |
| ② 采集模式 | `--profile-type async` | 异步采集（官方示例均用此值） |
| ③ 采什么 | `--capabilities-list ...` | 指定一个或多个能力 |
| ④ 采多久 | `--profile-time N` | 持续 N 秒 |
| ⑤ 多快采一次 | `--streaming-rate N` | 每 N 毫秒采样一次 |
| ⑥ 输出什么格式 | `--result-format ...` | verbose / trace / etw |

### 7.1 结果格式（result-format）说明

`verbose` 详细

在终端输出详细文本数据，配合 `--live` 可实时观察。

`trace` Perfetto

跟踪事件格式，导入 Perfetto / Qualcomm Profiler GUI 做离线时间线分析。

`etw` WoS

Windows 事件跟踪格式，配合 WPR/WPA 在 Windows on Snapdragon 上分析。

### 7.2 生成 / 复用配置（generate-config）

对于复杂的能力组合，可先生成配置再采集：

Windows (x86) / Linux / Android / QNX

```
qprof --generate-config profiler:nsp-dsp-metrics profiler:adsp-dsp-metrics
```

Windows on Snapdragon

```
qprof --generate-config --capabilities-list profiler:nsp-dsp-metrics profiler:adsp-dsp-metrics
```

## 8. 完整示例命令集

### 8.1 异步采集 + 实时详细输出（CPU + 温度）

```
qprof --profile --profile-type async --file-format json \
  --capabilities-list profiler:wos-apps-proc-cpu-metrics profiler:wos-apps-proc-thermal-metrics \
  --profile-time 10 --streaming-rate 500 --result-format verbose --live
```

### 8.2 按 metric-id 筛选采集

```
qprof --profile --profile-type async --file-format json \
  --capabilities-list profiler:wos-apps-proc-cpu-metrics \
  --profile-time 10 --streaming-rate 1000 --live \
  --metric-id-list 6401 --result-format verbose
```

### 8.3 采集为 Perfetto trace 格式（CPU + DSP）

```
qprof --profile --profile-type async \
  --capabilities-list profiler:apps-proc-cpu-metrics profiler:nsp-dsp-metrics \
  --profile-time 15 --streaming-rate 200 --result-format trace
```

采集完成后，在 Qualcomm Profiler GUI 中打开生成的 trace 文件查看时间线。

### 8.4 采集为 ETW 格式（带宽 + GPU，用于 WoS）

```
qprof --profile --profile-type async \
  --capabilities-list profiler:bw-profiler-ddr-metrics profiler:proc-gpu-specific-metrics \
  --profile-time 10 --result-format etw
```

**实战建议**
采集 GPU 瓶颈数据时，先用 `--capabilities` 确认设备支持 `profiler:proc-gpu-specific-metrics`；采样率不宜过密（200ms 起步），过密会增大工具自身开销、干扰真实性能。真机测试连续 ≥10 分钟才能暴露热节流。

## 9. 离线分析：Perfetto / WPA

### 9.1 用 Perfetto 查看 trace

以 `--result-format trace` 采集后，得到跟踪事件格式文件，可直接拖入 [Perfetto UI](https://ui.perfetto.dev) 或 Qualcomm Profiler GUI 进行时间线 / counter 分析。

### 9.2 用 Windows Performance Analyzer (WPA) 查看 ETW（仅 WoS）

在 Windows on Snapdragon 上，ETW 格式数据需要配合 WPR（Windows Performance Recorder）与 WPA 使用，完整四步：

步骤 1 — 用 WPR 开始录制 Profiler 指标

```
wpr -start QualcommProfilerETW.wprp!QualcommProfilerETW.Light -filemode
```

步骤 2 — 用 qprof 以 ETW 格式采集数据

```
qprof --profile --profile-type async \
  --capabilities-list profiler:bw-profiler-ddr-metrics profiler:proc-gpu-specific-metrics \
  --profile-time 10 --result-format etw
```

步骤 3 — 停止录制，输出 .etl 文件

```
wpr -stop demo.qualcommprofiler.etl
```

步骤 4 — 在 WPA 中打开 .etl 文件

```
wpa -i demo.qualcommprofiler.etl \
  -addsearchdir "C:\Program Files(Arm)\Qualcomm\Shared\QualcommProfiler\API\target-wos\libs" \
  -mode single
```

打开后即可在 WPA 中以图表与数据表的形式查看 Qualcomm Profiler 指标。

## 10. 日志与结果文件路径

采集产生的**日志**与**结果**分别保存在各平台的指定目录：

| 平台 | 日志位置 | 结果位置 |
| --- | --- | --- |
| Windows (x86) | `C:\ProgramData\Qualcomm\QualcommProfiler\logs` | `C:\ProgramData\Qualcomm\QualcommProfiler\profilingresults` |
| Windows on Snapdragon | `C:\ProgramData\Qualcomm\QualcommProfiler\logs` | `C:\ProgramData\Qualcomm\QualcommProfiler\profilingresults` |
| Android | Logcat（`adb logcat` 或 `adb logcat | grep QPROF`） | `/data/shared/QualcommProfiler/ProfilingResults` |
| QNX | `/var/data/QualcommProfiler/logs` | `/var/data/QualcommProfiler/ProfilingResults` |
| Ubuntu | `/opt/Qualcomm/QualcommProfiler/logs/` | `/opt/Qualcomm/QualcommProfiler/profilingresults/` |
| LE | Syslog（`/var/log/messages`） | `/data/shared/QualcommProfiler/profilingresults/` |
| HGY | Syslog（`journalctl --no-pager | grep QPROF`） | `/data/shared/QualcommProfiler/profilingresults/` |

## 11. 常见问题排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 启动 qprof 报找不到 .so 库 | `LD_LIBRARY_PATH` 未设置或路径错误 | 按第 3 节为对应平台重新 export 库路径；注意每个新 shell 都要设 |
| 连接服务器失败 / 超时 | 设备 IP、端口配置不对，或设备端服务器未启动 | 检查 `--server-ip`/`--port`，用 `--get-server-info` 核对；确认 adb/网络连通 |
| `--configure` 命令报不支持 | 当前在 Windows on Snapdragon 平台 | WoS 不支持 configure，改用 `--get-server-info` 查看现有配置 |
| `--capabilities` 列不出 GPU/DSP 能力 | 该平台不支持对应子系统，或库未加载 | 确认设备/平台是否支持该 IP 块（DSP 并非所有平台都有） |
| 采样率被拒绝 | 超出该能力允许的采样率区间 | 参考第 5 节采样率限制，调整 `--streaming-rate` |
| 采集数据偏离真实性能 | 采样率过密导致工具自身开销过大 | 放宽 `--streaming-rate`（200ms 起），并连续测试 ≥10 分钟暴露热节流 |

**重要提醒**
CLI 各平台的库路径、能力名称会随 Qualcomm Profiler 版本更新而变化。生产环境请以你实际安装版本的 `CLI\documents\sample-commands\` 示例和 `qprof --capabilities` 的真实输出为准，不要照搬本文路径。

**参考来源**
Qualcomm 官方文档 — Command line interface (CLI)：
`https://docs.qualcomm.com/bundle/publicresource/topics/80-54323-2/command-line-interface.html`
Snapdragon Profiler 产品页：`https://www.qualcomm.com/developer/software/snapdragon-profiler`

本文档整理自 Qualcomm 官方 CLI 文档 · 仅供学习参考 · 2026-06-25
