# Perfetto 使用手册 —— 面向 UE 手游 / 移动端 GPU 性能分析

> 来源：知乎《Perfetto tool 详细解析》(https://zhuanlan.zhihu.com/p/706968165，作者 麦芽，转载自 CSDN)
> 本文档 = 原文实操要点提炼 + 针对 UE 手游 / Adreno GPU 分析场景的裁剪与补充。
> **凡标注「〔补充〕」的段落为原文未覆盖、由本文档追加的内容。**

---

## 0. 一句话定位

Perfetto 是 **systrace 的升级版**：protobuf 编码的二进制流，可记录任意长度 trace，数据源是 systrace 的超集，还能做 SQL 分析、堆分析、调用栈采样。

它能回答的是 **系统侧问题**：CPU 调度、大小核、频率、线程唤醒关系、Binder 阻塞、IO 阻塞、SurfaceFlinger 合成、掉帧。
它**不能**回答引擎内部问题（哪个 Pass 慢、drawcall 数量）——那是 UE Insights / RenderDoc / SDP 的活。

**分工建议〔补充〕**

| 工具 | 看什么 |
|---|---|
| Perfetto | 系统侧：CPU 调度/频率/抢占、其他进程干扰、温控降频、SF 合成与掉帧、GPU 队列 fence |
| UE Insights | 引擎侧：GameThread / RenderThread / RHIThread 各阶段耗时、TaskGraph |
| SDP / AOC | GPU 侧：ALU/Texture/BW 计数器、shader GPR 与 occupancy |
| RenderDoc | 帧级：抓真机帧、pipeline 状态、SPIR-V |

---

## 1. 抓取 Trace 的四种方式

### 1.1 命令行 perfetto（推荐，最快最灵活）

```bash
# Android R 以下需先开开关（Android 10 及更早）
adb shell setprop persist.traced.enable 1

# 抓取
adb shell perfetto \
  -o /data/misc/perfetto-traces/trace_file.perfetto-trace \
  -t 10s \
  sched freq idle am wm gfx view binder_driver hal dalvik camera input res memory

# 导出
adb pull /data/misc/perfetto-traces/trace_file.perfetto-trace
```

常用参数：

| 参数 | 含义 |
|---|---|
| `-o` | 输出文件路径（必须在 `/data/misc/perfetto-traces/` 下，否则 selinux 拒绝） |
| `-t` | 抓取时长；新版可不指定，回车即停 |
| `-b` | buffer 大小，抓长 trace 要调大（如 `-b 64mb`） |
| `-a` | 指定 app 包名；**要抓自定义 Trace 点必须加** |

### 1.2 atrace 命令（老 systrace 格式）

```bash
adb shell atrace -z -b 40000 am wm view res ss gfx view hal bionic pm \
  sched freq idle disk load sync binder_driver binder_lock memreclaim \
  dalvik input -t 10 > /data/local/tmp/trace_output.atrace
adb pull /data/local/tmp/trace_output.atrace
```

### 1.3 systrace.py 脚本（需 python 环境）

```bash
python systrace.py am wm view res ss gfx rs hal bionic pm sched freq idle \
  disk binder_driver binder_lock memreclaim dalvik input database \
  -t 10 -o tracelog\systrace.html
```

### 1.4 手机系统 UI（System Tracing）

开发者选项 → 系统跟踪 → 开启「显示快捷设置图块」→ 下拉状态栏点 Record trace → 操作 → 点通知停止。
文件落在 `/data/local/traces/`，`adb pull /data/local/traces` 取出。

### 1.5 〔补充〕配置文件方式（长 trace / 精细控制必用）

命令行 category 只能开预置集合，要开 **特定 ftrace event、GPU counter、更长时长、多 buffer** 必须写 config（protobuf text）：

```protobuf
# config.pbtx
buffers: { size_kb: 65536 fill_policy: DISCARD }
data_sources: {
  config { name: "linux.ftrace"
    ftrace_config {
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup"
      ftrace_events: "sched/sched_waking"
      ftrace_events: "power/cpu_frequency"
      ftrace_events: "power/cpu_idle"
      ftrace_events: "thermal/thermal_temperature"   # 温控降频，需内核支持
      atrace_categories: "gfx" atrace_categories: "view"
      atrace_categories: "sched" atrace_categories: "freq"
      atrace_categories: "am" atrace_categories: "wm"
      atrace_categories: "binder_driver" atrace_categories: "input"
      atrace_categories: "memory" atrace_categories: "hal"
    } } }
data_sources: { config { name: "linux.process_stats" } }
duration_ms: 10000
```

```bash
adb push config.pbtx /data/local/tmp/config.pbtx
adb shell perfetto --txt -c /data/local/tmp/config.pbtx \
  -o /data/misc/perfetto-traces/trace.perfetto-trace
adb pull /data/misc/perfetto-traces/trace.perfetto-trace
```

也可以直接去 https://ui.perfetto.dev/#!/record 用图形界面生成 config（选好后复制命令）。

### 1.6 抓取节奏（原文建议）

1. 手机先切到**要抓的界面**，再开始抓；
2. 操作时间**不要太长**，文件太大会很卡、难定位；
3. 到时间后生成 trace 文件，丢进 https://ui.perfetto.dev/ 打开。

---

## 2. 分析核心一：线程状态（颜色）

Perfetto 用颜色标识线程状态，看颜色就知道瓶颈是 CPU 慢 / Binder / IO / 抢不到核。

| 颜色 | 状态 | 含义与分析方向 |
|---|---|---|
| 🟢 绿色 | Running | 真正在 CPU 上跑。看：频率够不够？跑在小核还是大核？是否频繁 Running↔Runnable 切换？不重要线程是否占了超大核？ |
| 🔵 蓝色 | Runnable | 可运行但在等调度。越长说明 CPU 越忙。看：后台任务是否太多？是否被 cpuset 限到某个核组？当前 Running 的是谁？ |
| ⚪ 白色 | Sleep | 无事可做（等锁 / 等别的线程）。**点它看是被谁唤醒的** |
| 🟠 橘色 | Uninterruptible Sleep (IO) | IO 阻塞，常见 callsite `wait_on_page_locked_killable`。大量出现 = 大概率低内存 + page fault + IO 排队 |
| 🟤 棕色 | Uninterruptible Sleep (non-IO) | 卡在内核其他操作（通常是内存管理）。需按具体情况分析 |

### 唤醒信息分析

一个线程长 Sleep 后被唤醒 → **看唤醒者是谁**，就知道调用等待关系。典型坑：

- 主线程 Binder 调 SystemServer 的 AMS，AMS 正在等锁 → 主线程干等 → 卡顿。这也是跑完 Monkey 后整机变慢的主因。
- 主线程等本进程其他线程的结果 → 唤醒信息直接告诉你被哪个线程 block 住。

---

## 3. 分析核心二：CPU Info

Perfetto 顶部 CPU Info 区主要看：**频率变化、任务执行、大小核调度、CPU Boost 调度**。

典型三问：

- 任务慢 → 是不是被调度到**小核**了？
- 任务慢 → 这个 CPU 的**频率**是不是不够？
- 我这场景能不能要求**锁最低频率 / 绑大核**？

架构分类：

- 非大小核：所有核一样
- 大小核：0-3 小核，4-7 大核
- 大中小核：0-3 小核，4-6 中核，7 超大核

> 〔补充〕骁龙 8 Gen2/8 Gen3 属于 **1+2+2+3 四簇架构**（1×Cortex-X 超大核 + 2×A7xx + 2×A5xx + 3×A5xx 小核），Perfetto 里 CPU0~CPU7 的排列顺序与簇的对应关系**每台机型可能不同**，看 trace 时以 frequency track 的实际频率高低来判断大/小核，不要死记编号。

---

## 4. 分析核心三：渲染链路与掉帧

### 4.1 一帧的完整链路

```
App(UI Thread → RenderThread) ──queueBuffer──> BufferQueue ──> SurfaceFlinger ──> HWComposer(HWC) ──> 屏幕
                                   ↑ dequeue/acquire/release 由生产/消费者轮转
```

四个关注区在 Perfetto 上按时间顺序就是 App → BufferQueue → SurfaceFlinger → HWC。

### 4.2 BufferQueue 四动作

| 动作 | 发起方 | 说明 |
|---|---|---|
| `dequeueBuffer` | 生产者(App) | 申请一块可用 buffer，指定宽高/格式/usage |
| `queueBuffer` | 生产者(App) | 填完了，还给队列 |
| `acquireBuffer` | 消费者(SF) | 取这块 buffer 来用 |
| `releaseBuffer` | 消费者(SF) | 用完了，还给队列 |

### 4.3 为什么是 Triple Buffer

- **Single Buffer**：同一块既要合成显示又要拿来渲染，来不及就撕裂（已淘汰）。
- **Double Buffer**：GPUSF / 消费者在用时，生产者还能拿另一块。但连续两帧超时就会掉帧。
- **Triple Buffer**：多一个 BackBuffer。SF 消费 FrontBuffer、GPU 用一个 BackBuffer、CPU 用一个 BackBuffer，**缓解掉帧、减少主线程等 buffer 的时间、降低 GPU 与 SF 的互相拖累**。

> ⚠️ **看 trace 的关键注意点**：因为 Triple Buffer 的存在，**App 主线程超时 ≠ 掉帧**。SF 手里可能还有 App 之前 queue 上来的 buffer，照样能合成。所以判定掉帧必须看 **SurfaceFlinger 侧**，不能只看 App 侧。

### 4.4 掉帧判定（从 SF 侧入手）

1. SF 主线程在每个 Vsync-SF 是否**没有合成**？
2. 没合成的原因是什么？
   - SF 检查发现**没有可用 buffer** → 往上追 App 为什么没及时 queueBuffer；
   - SF 被其他工作占用（截图、HWC 等）→ 系统侧问题。
3. 有合成，但 App 的**可用 buffer 数为 0** → 去看 App 为什么没及时 queueBuffer（一般就是应用自己的问题）。

### 4.5 逻辑掉帧（Perfetto 上看不出来）

帧都按时渲染、SF 都按时合成，但用户就是觉得卡——因为**画面更新步长不均匀**（这一帧走 20、下一帧走 10、再下一帧走 30）。

成因：动画按**当前系统时间**算属性变化，而不是按 **Vsync 到来的时间**。一掉帧动画就不匀。

---

## 5. 〔补充〕UE 手游视角：哪些要改，哪些不用看

### 5.1 原生 App vs 游戏，模型不一样

原文讲的 Choreographer / View / RenderThread(libhwui) 那一套是**原生 View 体系**的：

```
Vsync → Choreographer.doFrame → INPUT → ANIMATION → INSETS_ANIMATION → TRAVERSAL → COMMIT
      → UI Thread 更新 DisplayList → sync 给 RenderThread → RenderThread dequeue/GL/queue → SF
```

**游戏不走这条路**。原文明确写了：

> 游戏大多使用单独的渲染线程，直接跟 SurfaceFlinger 交互，主线程存在感很低，绝大部分逻辑都在自己的渲染线程里。（王者荣耀的例子：主线程只负责把 Input 事件传给 Unity 的渲染线程。）

UE 手游同理。所以：

| 原文内容 | 对 UE 手游 |
|---|---|
| Choreographer.doFrame 五个 callback | **基本不看**（游戏不靠它驱动帧） |
| UI Thread 的 measure/layout/draw | **不看**（没有 View 树遍历） |
| RenderThread (libhwui) syncAndDrawFrame | **不看**（UE 有自己的渲染线程） |
| BufferQueue / SurfaceFlinger / HWC / Triple Buffer | **完全适用，且是重点** |
| Vsync 与帧节奏 | **适用**（UE 走 `rhi.SyncInterval` / Swappy，最终还是要对 SF 的 Vsync-SF） |
| CPU 调度 / 频率 / 大小核 | **完全适用，且是重点** |
| Binder / IO / 唤醒关系 | **适用**（引擎的加载、系统服务调用都会踩） |

### 5.2 UE 线程在 Perfetto 上的对应

UE 在 Android 上会给线程设名（pthread_setname_np），一般能在 trace 里直接认出来：

| Perfetto 线程名 | UE 角色 | Perfetto 上关注点 |
|---|---|---|
| 主线程 / 进程同名 | GameThread | 游戏逻辑、动画、蓝图 Tick；非 root 下 atrace slice 有限，主要看它的 **Running 时长与落核** |
| `RenderThread` / `RenderThread 0` | 渲染线程 | 可见性剔除、drawcall 录制；看是否长时间 Running、是否频繁被抢占 |
| `RHIThread` | RHI 线程 | GL/Vulkan 命令提交；**看它与 GPU 的 fence 等待关系** |
| `TaskGraphThread N` | TaskGraph 工作线程 | 看并行度、是否被限到小核、是否抢大核 |
| `AsyncLoadingThread` / IO 线程 | 资源加载 | 看是否出现大量 🟠 橘色 IO 阻塞（卡顿伴随加载时重点查） |

> 若线程只显示 `Thread-N`，用 `adb shell ls /proc/<pid>/task/` + `cat /proc/<pid>/task/<tid>/comm` 对一下名字。

### 5.3 GPU 侧能看到什么（**受设备与权限限制，需实测**）

| 项 | 可行性 |
|---|---|
| GPU 频率 track (`gpu_frequency`) | 需内核暴露该 ftrace event；高通机型部分支持，**先试再信** |
| GPU 利用率 / work period (`gpu_work_period`) | 同上，厂商相关 |
| `eglSwapBuffers` / `queueBuffer` / GPU fence | **通常可见**（gfx category），可定位"提交完到显示"的延迟 |
| GPU 内存 | 需 `android.gpu.memory` data source，多数非 root 机型拿不到 |

**结论**：Perfetto 在 GPU 侧的可见性弱。**GPU 内部瓶颈仍然交给 SDP / AOC / RenderDoc**，Perfetto 只用来回答"这帧慢是不是 GPU 之外的系统原因"。

---

## 6. 〔补充〕本机环境实测（2026-09-14）

| 项 | 值 |
|---|---|
| adb | `C:\Users\djangozhang\AppData\Local\Android\Sdk\platform-tools\adb.exe`，v1.0.41 (37.0.0) |
| 已连设备 | `1a211bbd` |
| 机型 | **MI 8** |
| Android 版本 | **10（Q）** → 属「低于 Android R」，需 `setprop persist.traced.enable 1` |
| `persist.traced.enable` 实测 | **1（已开启）** |
| 平台 | sdm845（骁龙 845 / Adreno 630） |
| shell 权限 | **非 root**（uid=2000 shell） |
| `/system/bin/perfetto` | 存在，但**版本较老，不支持 `--version` / `--query`** |
| atrace 可用类别 | gfx input view webview wm am sm audio video camera hal res dalvik rs bionic power pm ss database network adb vibrator aidl nnapi rro pdx sched freq idle disk |

**由此得出的限制**：

1. 非 root → 拿不到需要 root 的 data source（GPU 内存、完整 kernel symbol、部分 thermal event）。抓取前最好先 `adb root`（该机未 root，只能接受）。
2. perfetto 版本老 → **优先用 1.1 的 category 命令行方式**，config 文件方式（1.5）里部分新字段可能不被识别，报了错就退回命令行。
3. sdm845 很老，若要做 8Gen2/8Gen3 的功耗/带宽结论，**别拿这台的数据当代餐**。

---

## 6.5 本机已安装的 PC 端工具（2026-09-14）

安装根目录：**`C:\Users\djangozhang\tools\perfetto`**（版本 v58.2）

| 组件 | 路径 | 用途 |
|---|---|---|
| `trace_processor_shell.exe` | `bin\windows-amd64\` | **本地 SQL 分析 trace**，核心工具 |
| `traceconv.exe` | 同上 | 格式转换（→ systrace HTML / JSON） |
| `perfetto.exe` / `traced.exe` | 同上 | PC 端客户端与守护进程 |
| 离线 UI | `ui\` + `start-ui.cmd` | **完全离线**的 Perfetto UI（双击即用） |
| 抓取脚本 | `scripts\capture.sh` | 封装设备端抓取 + pull |
| **报告脚本** | `scripts\tp_report.py` | **一条命令出 11 节 Markdown 分析报告** |
| `README.md` | 根目录 | 安装说明 + SQL 速查 + 踩坑记录 |

`bin\windows-amd64` **已写入用户 PATH**，任意新终端可直接调用。

**一键出报告**：
```bash
python scripts/tp_report.py trace.pt -o report.md
python scripts/tp_report.py trace.pt --pkg com.sarosgame.S1Game   # 聚焦目标进程
```
覆盖：规模总览 / Top slice / 各进程耗时 / CPU 频率 / 线程状态 / 调度延迟 / IO 阻塞 /
SurfaceFlinger 全景 / FrameTimeline 掉帧 / Binder / 目标进程线程明细。

### ⚠️ 两个实测踩出来的坑（务必记住）

**坑一：`memory` category 会冲掉 `gfx` 的 SF slice。**
7 次对照实验（MI 8 / Android 10）结果：

| category 集 | SF `onMessageReceived` 数 |
|---|---|
| `sched freq idle am wm gfx view` | 318 / 296 ✓ |
| 上面 + `input` | 290 ✓ |
| 上面 + `binder_driver` | 445 ✓ |
| 上面 + `memory` | **0 ✗** |
| 上面 + `memory input` | **0 ✗** |
| 含 `memory` 的长集 | **0 ✗** |

含 `memory` 必丢，不含必在。掉帧/合成分析完全依赖 SF 的 slice，所以**抓渲染场景绝不要带 memory**；
需要内存分析时单独抓一次。`capture.sh` 默认集已按此调优，检测到 `memory` 会打警告。

**坑二：原生 exe 不认 Git Bash 路径。**
`trace_processor_shell.exe` 是原生 Windows 程序，传 `/c/Users/...` 会报
`could not open trace file ... (errno: 2)`。要用 `C:/Users/...` 或相对路径。
`tp_report.py` 已内置自动转换。

> ⚠️ 新版 `trace_processor_shell` 是**子命令式**，不是老教程里的 `-q`：
> ```bash
> trace_processor_shell query trace.pt "SELECT name, dur/1e6 AS ms FROM slice ORDER BY dur DESC LIMIT 10"
> trace_processor_shell query -f queries.sql trace.pt      # 批量
> trace_processor_shell trace.pt                            # 交互式 SQL
> trace_processor_shell summarize --metrics-v2 all trace.pt # 内置指标
> traceconv systrace trace.pt out.html                      # 转 systrace
> ```

**离线 UI**：双击 `start-ui.cmd` → 浏览器开 http://127.0.0.1:18080/ （端口 **18080**，10000 在 Windows 属保留段会 bind 失败）。
trace 在浏览器本地 wasm 解析，**不上传**，功能与 ui.perfetto.dev 一致。

**端到端已验证**（MI 8，5 秒抓）：
```
adb shell "perfetto -o /data/misc/perfetto-traces/t.pt -t 5s sched freq idle am wm gfx view"
adb pull /data/misc/perfetto-traces/t.pt
```
→ 13.7 MB，trace_processor 解析出 108 进程 / 234 线程 / 8444 slice，Top slice 全是 `surfaceflinger`（待机态符合预期）。

---

## 7. UI 使用与快捷键

**打开**：优先用**本地离线 UI** —— 双击 `C:\Users\djangozhang\tools\perfetto\start-ui.cmd`，或 `http://127.0.0.1:18080/`。
也可以用在线版 https://ui.perfetto.dev/ ，拖入 `.perfetto-trace`（也兼容 systrace `.html` / atrace 文本）。

| 键 | 功能 |
|---|---|
| `W` | 放大（看局部细节） |
| `S` | 缩小（看整体） |
| `A` | 左移 |
| `D` | 右移 |
| `M` | 选中时间段范围，方便上下对照 |

**搜索**：Perfetto 自带搜索框需 **4 个字符以上**，搜出来更详细；配合 `Ctrl+F` 一起用——有时 `Ctrl+F` 搜不到的，自带 Search 能搜到。

**〔补充〕SQL 分析**：UI 左侧 `Query (SQL)` 走 trace_processor，常用表：`slice`、`thread_track`、`process`、`thread`、`sched_slice`、`counter`。
例——统计各线程 slice 总耗时：

```sql
SELECT t.name AS thread, s.name AS slice, COUNT(*) cnt, SUM(s.dur)/1e6 AS total_ms
FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t ON tt.utid = t.utid
GROUP BY t.name, s.name ORDER BY total_ms DESC LIMIT 30;
```

**〔补充〕命令行转换**：`traceconv systrace trace.perfetto-trace trace.html`（官方 tools，把 perfetto 转成 systrace html 给用惯旧工具的人看）。

---

## 8. 〔补充〕UE 手游卡顿分析 Checklist

抓完 trace 按这个顺序过一遍：

1. **确认掉帧** → 去 SurfaceFlinger 主线程，看每个 Vsync-SF 是否都有合成（别只看 App 侧，Triple Buffer 会掩盖）。
2. **定位慢的线程** → 找到 GameThread / RenderThread / RHIThread，看 Running 时长。
3. **看落核** → 关键线程是否被调度到小核？频率是否掉到很低？
   - 是 → CPU 调度/温控问题，跟大核占用、thermal 一起看。
4. **看 Runnable 蓝色** → 关键线程是不是长时间排队等 CPU？谁在占着核？
5. **看 Sleep 白色 + 唤醒者** → 被谁唤醒？唤醒者为什这么晚？（Binder 锁、等 IO、等其他线程）
6. **看橘色 IO** → 卡顿是否伴随资源加载？AsyncLoadingThread 是否在疯狂 page fault？
7. **看 Binder** → 渲染线程 `dequeueBuffer` 是否被 SF 主线程阻塞（原文点名的经典坑）。
8. **排除系统侧** → 后台进程、Monkey、GC（dalvik）、温控降频。
9. **都不是** → 那就是 GPU 侧瓶颈了，切 SDP / AOC / RenderDoc，别在 Perfetto 里死磕。

---

## 9. 附：一键抓取脚本

见同目录 `scripts/perfetto_capture.sh`（bash / Git Bash 可直接跑），已针对 Android 10 及以下自动开启 traced 开关，并按包名附加 `-a`。
