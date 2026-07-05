# 头部手游 PSO 方案汇总

# 头部手游 · PSO 方案汇总

从 Shader 变体到运行时编译卡顿，跨 Unity / UE 主流手游的 PSO 预热与缓存实践

**覆盖引擎**Unity（Built-in / URP）· UE4 / UE5

**视角**移动端 Vulkan / Metal / OpenGL ES

**定位**方案库 / 速查手册

**核心指标**PSO 编译耗时 · 变体数 · 首帧 hitch · 启动期卡顿

资料说明

本文为横向专题汇总，基于 Epic / Unity 官方文档、UE4 wiki、Meta Quest 官方 Sample、Unity Open Day 与 UWA/CSDN 公开博客整理。多数头部手游 PSO 细节未公开披露，部分案例为同品类通用做法的归纳。所有数值均为典型范围或定性描述，非伪精确数字。

## 一、核心结论速览：PSO 优化的三层心智模型

#### ① 机制层（API 决定的）

- PSO = GPU 渲染状态对象（Shader + 混合 + 深度 + RT 格式…）
- D3D12 / Vulkan / Metal 原生支持，**OpenGL ES 没有**
- UE 把 GLES 也抽象成 BSS（BoundShaderState）
- 首次创建 PSO 可达 **100ms+**

#### ② 资源层（变体治理）

- Shader 变体爆炸是 PSO 数量爆炸的根因
- `multi_compile` 全量生成 → `shader_feature` 按需生成
- 10000 变体 ≈ 500MB / 20s 移动端加载
- Shader Variant Collection 精确控制预热范围

#### ③ 调度层（运行时策略）

- 启动期 / Loading 期 异步预热
- UE 手动 PSO Caching（.spc 打包）
- UE5 PSO Precaching（D3D12，自动）
- 移动端后台编译**一般关掉**，否则发热/降频

总体取舍

PSO 优化的本质不是"消灭 PSO"，而是**消灭运行时 PSO 编译**。现代图形 API 用 PSO 把"切渲染状态"从一连串状态校验/写寄存器变成一次对象切换，本身不慢；**慢的是 PSO 的首次编译（驱动 + 硬件协同生成可执行对象）**。头部手游的共识是：变体先收敛 → Caching 离线收集 → 运行时异步预热 三层协同。任何"想靠加运行时代码抹平 PSO 编译"的方案都不如"打包时把 PSO 准备好"实在。

## 二、概念辨析：PSO / 变体 / Pipeline Cache 到底是什么

PSO 专题里最容易混的有 6 个概念，先把它们一刀切开。

| 概念 | 含义 | 移动端特别注意 |
| --- | --- | --- |
| **PSO**Pipeline State Object | 一次绘制所需的**完整 GPU 状态包**：顶点布局 + Shader（VS/PS/CS） + 混合 + 深度 + RT 格式 + 视口等 | D3D12 / Vulkan / Metal 原生；OpenGL ES **无**，UE 用 BSS 抽象 |
| **Pipeline Cache** | 把已编译的 PSO **序列化到磁盘**，下次直接反序列化跳过编译 | Vulkan 走 `VKPipelineCache`；UE 走 `.upipelinecache` + `.spc`；Metal 有 MTLPipelineCache |
| **Shader 变体**Variant | 同一 Shader 源文件在不同 Keyword 组合下编译出的**多份二进制** |- 1 个变体 = 1 套 PSO 候选（材质参数不同时也可能衍生新 PSO）

| **multi\_compile** **vs** **shader\_feature** | 前者**无条件全量生成**所有 Keyword 组合；后者**只在材质实际使用**时生成 | 写错一条 `multi_compile`，变体数直接 ×2、×4、指数级爆炸 |
| **Graphics PSO** **vs** **Compute PSO** | Graphics 含完整渲染状态；Compute 仅有计算 Shader + 资源绑定 | UE 移动端 Compute PSO 数量也很大（GI、Particle、PostProcess） |
| **SetPass** **vs** **Draw Call** | DC = 一次绘制调用；SetPass = 切换一次 Shader/材质/渲染状态（**隐含触发新 PSO 编译**） | 移动端 SetPass 切换成本 ≈ DC 的 1.5~3× |

移动端的非线性代价

PSO 编译耗时不是"几百微秒"的概念。按公开经验：**Vulkan/D3D12 移动端首次 PSO 编译约 50~200ms**，中低端 Mali / 老款 Adreno 上甚至能到 **300ms+**。这意味着如果玩家在战斗中突然触发一个新材质（比如技能命中产生的新粒子材质），单次 hitch 就可能让主线程卡一帧甚至两帧。OpenGL ES 没有 PSO 编译概念，但"Shader 编译 + 状态校验"的成本同样存在，只是被驱动封装在内部。

## 三、引擎机制对比

UE 和 Unity 的 PSO 体系在概念上对齐，但工程链路差异巨大。**UE 的核心是离线收集 → 打包内嵌 → 运行时反序列化；Unity 的核心是 ShaderVariantCollection 精确圈定 → 启动期异步 WarmUp**。

### 3.1 UE：手动 PSO Caching（4.27~5.2 主线）

UE 在 4.27 起引入 PSO Caching，5.2 之前是**唯一**的移动端方案。核心思想是：**把"运行时驱动编译 PSO"前移到"打包期准备"**。

- **ShaderStableKeys**：Cook 时为每个 Shader 生成稳定 Key，作为 PSO 标识符
- **logPSO**：玩家跑游戏时把实际遇到的 PSO 写入 `.scl.upipelinecache`（增量收集）
- **ShaderPipelineCacheTools**：`-run=ShaderPipelineCacheTools expand`，把 `.shk` + `.upipelinecache` 合并生成 `.stablepc.csv`
- **打包内嵌**：再 Cook 时把 `.stablepc.csv` 打进包，引擎启动自动反序列化 `.stable.upipelinecache`
- **LRU**：Vulkan/OpenGL 驱动支持，限制内存中常驻 PSO 数量；**Metal 没有 LRU**，需手动管理

### 3.2 UE5.3+：PSO Precaching（自动收集 · 仅 D3D12）

UE5.3 首次引入，5.4 默认开启。是**全自动**的方案，Loading 阶段就开始后台线程异步编译所有可能用到的 PSO。

- **r.PSOPrecaching**：全局开关，依赖 `GRHISupportsPSOPrecaching` RHI 标志
- **r.PSOPrecache.Components**：预缓存组件用到的 PSO（默认开启）
- **r.PSOPrecache.Resources**：预缓存所有资源用到的 PSO（默认关闭，render state 可能不完整）
- **r.PSOPrecache.ProxyCreationWhenPSOReady**：等 PSO 编译完才创建组件代理，否则用默认材质占位
- **支持 PSO 的全局 Shader 类型**：Slate · DeferredLights · CascadeParticleSimulation · VolumetricFog

关键限制

PSO Precaching **目前仅适用于 D3D12**，手游项目（Vulkan / Metal / OpenGL ES）**暂时无缘这套全自动方案**。UE5.6 文档也明确：手游项目仍以手动 PSO Caching + 启动期异步预编译为主。

### 3.3 Unity：ShaderVariantCollection + SRP Batcher + 异步预热

Unity 的 PSO 等价物是 **Shader 变体 + ShaderProgram + 状态切换**，没有 UE 那么显式的"PSO 编译"概念，但卡顿机理完全相同。

- **ShaderVariantCollection（SVC）**：美术在 Editor 里手动圈定要预热的变体集合，`WarmUp()` 异步预编译
- **SRP Batcher**：把同 Shader 物体的材质属性收进 `UnityPerMaterial` CBuffer，**减少 PSO 切换频率**（不消除 PSO 本身）
- **shader\_feature vs multi\_compile**：前者按需生成、后者全量生成——前者是治本
- **Player Settings → Graphics APIs**：决定走 Vulkan / Metal / GLES3，影响 PSO 抽象层
- **Graphics Settings → Shader Stripping**：构建时自动剔除未使用变体，控上限

| 机制 | UE 手动 PSO Caching | UE PSO Precaching | Unity SVC + 异步预热 |
| --- | --- | --- | --- |
| 引擎 | UE 4.27~5.6（主线） | UE 5.3+，**仅 D3D12** | Unity 2018+ |
| 收集方式 | 手动 logPSO 跑游戏 | 引擎自动枚举 | 美术在 Editor 圈定 |
| 打包形态 | `.stable.upipelinecache` 进包 | 自动编译到内存 | SVC 资源随 AssetBundle 进包 |
| 运行时 | 反序列化即用 | Loading 后后台异步 | `WarmUp()` 异步 |
| 移动端支持 | Vulkan / OpenGL ES / Metal（Metal 无 LRU） | 仅 D3D12 | 全 API |
| 覆盖率 | 取决于 logPSO 跑全不全 | 引擎枚举，接近 100% | 取决于 SVC 圈定 |
| 短板 | 首次跑不全就漏；迭代需重收集 | 手游暂时用不上 | 靠人工，漏圈就掉链子 |

## 四、端游方案为何在移动端失效

PC 端的 D3D12 PSO 优化文章浩如烟海，**大部分方案在移动端并不成立**。原因集中在五个方面。

#### ① OpenGL ES 没有 PSO

OpenGL ES 3.0/3.1/3.2 的状态机是松散的（VAO + Program + 各种 glEnable/glDisable），没有 PSO 对象。UE 把这部分状态抽象成 BSS（BoundShaderState），"PSO Cache" 在 GLES 上实质只缓存了 BSS 一部分，**驱动内的状态校验依然存在**。这意味着老项目的 PSO Caching "覆盖率" 实际是个假象。

#### ② 移动端驱动差异巨大

同一份 Shader 在 Mali-G77 / Adreno 660 / Adreno 730 上生成的 PSO 互不通用；甚至同一颗 GPU 的不同驱动版本（Vulkan 1.1 vs 1.3）也不通用。**无法做端游那种"全平台共享 PSO Cache"**。每台目标机型要么走"按机型打包"，要么走"运行时按需编译"。

#### ③ Metal 没有 LRU

OpenGL 和 Vulkan 的 PSO Cache 都支持 LRU（最近最少使用淘汰），控制内存占用。Metal 的 `MTLPipelineCache` 没有 LRU，**无差别常驻所有创建过的 PSO**。iOS 项目必须手动控制 PSO 数量，否则内存压力会非常突出。

#### ④ 移动端后台编译基本关掉

端游会开"多线程后台编译 PSO"以减少主线程卡顿。移动端受限于**功耗 / 发热 / 低端机 CPU 紧**，后台编译通常关掉，全部前置到 Loading 阶段。一旦 Loading 期没做完，运行时就只剩"主线程同步编译"这条死路。

#### ⑤ Vulkan PipelineCache 跨设备失效

`VKPipelineCache` 序列化的二进制是**设备 + 驱动版本强相关**的。同一份 cache 在 Pixel 7 的 Android 14 / Adreno 730 / Vulkan 1.3 上是有效的，但换到小米 14 / HyperOS / Vulkan 1.3 上大概率失效。打包时无法做到"一份 cache 走天下"。

## 五、标杆案例深拆

### 5.1 案例 A：UE 移动端 PSO Caching 完整 5 步流程

这是 UE 移动端项目的**标准打法**，从引擎配置到打包内嵌完整走通一遍。**参考：UE 5.6 官方文档 + bearhammergames 在 Quest 2 上的实战记录**。

#### Step 1 · 开启 ShaderStableKeys

```
; Config/Android/AndroidEngine.ini
[DevOptions.Shaders]
NeedsShaderStableKeys = true

; DefaultDeviceProfiles.ini（Android 段下）
+CVars=r.ShaderPipelineCache.Enabled=1
+CVars=r.ShaderPipelineCache.ReportPSO=1
+CVars=r.ShaderPipelineCache.StartupMode=1
+CVars=r.ShaderPipelineCache.GameFileMaskEnabled=0
+CVars=r.ShaderPipelineCache.LazyLoadShadersWhenPSOCacheIsPresent=1
+CVars=r.ShaderPipelineCache.BatchSize=10
+CVars=r.ShaderPipelineCache.BatchTime=0.0
+CVars=r.Vulkan.PipelineCacheFromShaderPipelineCache=1
```

Cook 后会在 `Saved/Cooked/Android_*/Project/Metadata/PipelineCaches/` 生成两个 `.scl.csv`（项目级 + 全局）。

#### Step 2 · 真机跑游戏收集 PSO

加 `-logPSO` 启动游戏（For Distribution 取消勾选，**Android API ≥ 29** 才能写 PSO Cache）。尽量覆盖所有场景 / 玩法 / 画质档位，避免漏收集。log 写入 `Saved/CollectedPSOs/.rec.upipelinecache`。

#### Step 3 · 用 ShaderPipelineCacheTools 合成 spc

```
UE4Editor-Cmd.exe Project.uproject ^
  -run=ShaderPipelineCacheTools expand ^
  D:/PSO/.rec.upipelinecache ^
  D:/PSO/.shk ^
  ProjectName_SF_VULKAN_ES31_ANDROID.stablepc.csv
```

把 `.shk` + `.rec.upipelinecache` + `.scl.csv` 合并成一张"稳定 PSO 列表"。

#### Step 4 · 再 Cook 把 spc 打入包

把 `.stablepc.csv` 放进 `Build/Android/PipelineCaches/`，再 Cook 一次，引擎会把 `.stable.upipelinecache` 打进包内。

#### Step 5 · 运行时反序列化即用

玩家第一次启动游戏时，引擎自动加载 `.stable.upipelinecache`，**绝大部分 PSO 在反序列化瞬间即可用**，不再触发驱动编译。剩余漏网之鱼依赖 `LazyLoadShadersWhenPSOCacheIsPresent=1` 在后台线程异步补齐。

Meta Showdown Sample 实战补充

Meta 在 Quest 2/3 上开源的 Showdown Sample（UE 5.6 移植 PC VR）封装了 `GeneratePSOCache.bat`，完整走完上述 5 步。**实战数据：开启 PSO Cache 后首秀粒子/材质加载 hitch 几乎消失**，从原来偶发 200~500ms 单帧卡顿降到 60fps 全程稳定。该 Sample 还配套 `Application SpaceWarp`（90fps → 45fps 渲染 + 合成补帧），PSO Cache 是这套 VR 性能方案的**地基**。

### 5.2 案例 B：Unity ShaderVariantCollection 异步预热（米哈游系打法）

原神 / 绝区零 / 星铁这套打法没有公开 PPT，但**从玩家社区反馈可以反推**：版本更新后启动游戏会有一段"正在编译着色器"的过程；这段时间就是 SVC 异步预热。

#### Step 1 · 治理 multi\_compile

这是治本的一步。**multi\_compile 是变体爆炸的元凶**。Shader Graph 拖一个"Shadow Strength"节点就自动生成 2 个变体；自定义 Shader 里 5 个 `multi_compile` 就是 32 个变体。**尽可能用 shader\_feature 替代**，shader\_feature 只在实际材质启用对应 Keyword 时才生成变体。

#### Step 2 · 按场景-功能双维度拆分 SVC

参考腾讯云开发者社区《URP 管线角色材质、阴影与显存动态适配优化方案》的实战经验：**不分场景的全量 SVC 会让启动期 ASMD 卡爆**。正确做法是按场景特性拆分：

- 夜间森林场景：只保留"动态点光源 + 体积雾"核心变体，**变体数压到 300 以内**
- 白昼平原场景：侧重 AO + 主光阴影响应，剔除多光源叠加变体
- 战斗场景：保留技能光效相关变体，非战斗场景简化

#### Step 3 · AssetBundle 异步加载

不同场景的 SVC 拆 AssetBundle 异步加载。角色进入场景 → 触发对应 SVC 异步加载 → 加载完激活对应功能 → 退出场景立即释放冗余变体资源。**实测数据（前述 URP 实战文章）：Shader 编译时间缩短 65% 降至 7s 以内，材质失效概率从 35% 降至 0，CPU Shader 管理线程耗时从 3.2ms 降至 0.8ms**。

#### Step 4 · 平台差异化变体剥离

Android 不同 GPU（Adreno / Mali）对 Shader 光照叠加的编译特性差异显著。在光照叠加节点后强制嵌入**双重钳位逻辑**（颜色输出钳到 0~2），既保留 HDR 动态范围又避免颜色溢出导致材质失效。Adreno / Mali 分别编两套 SVC，不要强行共用。

玩家视角的可观察现象

米哈游系游戏每次大版本更新后，安卓端首次启动会有一段"加载着色器"提示（iOS 极少出现）。这段就是 SVC 异步预热的可视化——团队把"不可避免的启动期 PSO 编译"显式化、可中断化、可重试化，**比"默默卡 30 秒"友好得多**。社区多次反馈"原神 6.1 启动卡顿"的根因几乎都是：**后台被杀 / 息屏导致 SVC 编译中断，生成损坏缓存**。这反向说明 SVC 异步预热确实在大规模运行。

## 六、头部手游 PSO 实践横向对比

下表覆盖 14 款已研究的头部手游，**PSO 机制一栏基于公开演讲/逆向/品类共性推断**，不构成具体实现细节的精确披露。

| 游戏 | 类型 | 引擎 | PSO 机制 | 亮点 / 推测 |
| --- | --- | --- | --- | --- |
| 三角洲行动 | FPS 大世界 | UE4 改 | 手动 PSO Caching | UE5 时代开始接入 PSO Precaching（PC 端 D3D12）；移动端 Vulkan 走手动链路 |
| 和平精英 | FPS 大世界 | UE4 改 | 手动 PSO Caching | 8×8km 大世界，材质变体极多；`logPSO` 覆盖所有地图/模式/画质档 |
| 暗区突围 | FPS 半开放 | UE4 改 | 手动 PSO Caching | GDC 2024 公开分享；主机级动态天气驱动大量变体生成 |
| 鸣潮 | 开放世界 NPR | UE4 | 手动 PSO Caching | 双 Ramp NPR 改写 + UE 移动 Forward 改 RDG，PSO 数量可控 |
| 永劫无间手游 | 大逃杀 | UE4 | 手动 PSO Caching | 近战连招 / 飞索动态 streaming，PSO 复用率较高 |
| 燕云十六声 | 大世界写实 | Messiah 自研 | 自研 PSO 抽象 | 跨端自研引擎，PSO 抽象层自家管，可控性强 |
| 使命召唤手游 | FPS 地图制 | Unity | SVC + SRP Batcher | 武器/角色变体管理；4 级 Shader LOD 显式控制变体爆炸 |
| 王者荣耀 | MOBA | Unity | SVC + 强治理 | 10 年长线 + 200+ 英雄皮肤 + 6 档分级；**变体爆炸压力最大** |
| 原神 | 开放世界 NPR | Unity SRP | SVC 异步预热 | 启动期"加载着色器"提示 → SVC WarmUp 异步进行；版本更新显著 |
| 绝区零 | 动作 NPR | Unity SRP | SVC 异步预热 | Halftone / 速度线等漫画效果新增大量变体；3.0 浮空地图进一步加压 |
| 崩坏星穹铁道 | 回合 NPR | Unity SRP | SVC 异步预热 | UI 立绘实时光照 + 大招演出 RT 调度，**非战斗场景的 PSO 复用率反而高** |
| 蛋仔派对 | UGC 派对 | Unity URP | SVC + UGC 动态 | UGC 玩家创建材质 → **变体不可控**，依赖 SVC 兜底 + 性能审核 |
| 第五人格 | 非对称 | Unity | SVC | 哥特风格化 Tonemap 不会大幅加变体；规模较小可控 |
| 光遇 | 社交体验 | 自研 Sky | 自研 PSO 抽象 | 体验向无激烈 PSO 切换；自研引擎可控 |

观察：UE 系 vs Unity 系的 PSO 哲学差异

**UE 系（手动链路）：**靠"打包时把 PSO 准备好"，运行时几乎不发生 PSO 编译；缺点是迭代成本高——每次材质重大修改都要重跑 logPSO。
**Unity 系（SVC 异步预热）：**靠"启动期异步把变体编译完"，运行时已 ready；缺点是用户每次启动/版本更新都要等，**且中断后可能生成损坏缓存**（米游社区大量反馈）。
**自研系（燕云 / 光遇）：**完全自家控制，**天花板高但成本最高**，适合长线自研团队。

## 七、避坑清单：变体爆炸 · 移动端限制 · LogPSO 脏数据

#### ① 变体数爆炸 首要

- 100 变体 ≈ 5MB / 200ms 移动端加载
- 1,000 变体 ≈ 50MB / 2s
- **10,000 变体 ≈ 500MB / 20s**（手机完全不可接受）
- 全量打包时编译 PSO Cache 低端机直接 OOM
- 治本：用 `shader_feature` 替代 `multi_compile`；按场景拆 SVC

#### ② r.PSOPrecaching 仅 D3D12 移动端

- UE5.3 引入，5.4 默认开启，**但只支持 D3D12**
- 手游 Vulkan / Metal / OpenGL ES 暂时无缘
- 若项目代码里无脑启用 `r.PSOPrecaching=1` 在 Vulkan 上无效
- 关注 `GRHISupportsPSOPrecaching` RHI 标志

#### ③ Metal 无 LRU iOS

- OpenGL / Vulkan PSO Cache 都支持 LRU
- Metal 的 `MTLPipelineCache` **无** LRU，**无差别常驻**
- iOS 项目必须手动控制 PSO 数量 + 显式释放
- 慎用 iOS 长生命周期场景的动态材质

#### ④ 移动端后台编译关掉 性能

- UE 后台编译模式（`BackgroundShaderCompile`）手游通常关闭
- 原因：功耗 / 发热 / 低端 CPU 紧
- 关掉后所有 PSO 编译**全在主线程**
- 后果：**Loading 期没做完 = 运行时同步编译 = hitch**

#### ⑤ LogPSO 跑不全 覆盖率

- 手动 PSO Caching 依赖 `-logPSO` 跑全场景
- 漏跑 = 漏收集 = 运行时该 PSO 仍要编译
- QA 矩阵要覆盖：所有地图 × 所有模式 × 所有画质档 × 所有英雄/武器
- 建议：用脚本自动遍历而非人工

#### ⑥ Vulkan PipelineCache 跨设备失效 兼容性

- `VKPipelineCache` 序列化二进制**设备 + 驱动强相关**
- 同一份 cache 在不同机型大概率失效
- 要么按机型打包（成本高），要么运行时按需编译
- `r.Vulkan.PipelineCacheFromShaderPipelineCache=1` 开启关联

#### ⑦ 启动期全局 PSO 预编译 首帧

- UE5 PSO Precaching 启动期会预编译**全局计算 / 图形 PSO**（Slate、DeferredLights、CascadeParticleSimulation、VolumetricFog）
- 耗时在主菜单期间累积
- 关掉 `r.PSOPrecache.GlobalShaders=0` 可缩短但首用会卡
- 需权衡"启动慢" vs "首用卡"

#### ⑧ SVC 中断生成损坏缓存 社区高发

- 米游社区大量反馈：版本更新后"启动卡住"
- 根因：**后台被杀 / 息屏 / 锁屏**导致 SVC 异步编译中断
- 生成损坏的 `ShaderCache` 文件，下一次启动继续坏
- 解决：`Application.persistentDataPath/ShaderCache` 检测到损坏就清掉重来

## 八、落地 Checklist 与定位工具

### 8.1 施策优先级（先做什么后做什么）

1. **第一步 · 收敛变体**：把 `multi_compile` 改成 `shader_feature`；删除未用 Keyword
2. **第二步 · SRP Batcher 兼容**：所有材质 CBUFFER\_START/END、关掉 Legacy Lighting
3. **第三步 · 搭建 SVC / logPSO 流水线**：CI 自动化收集 + 覆盖率卡点
4. **第四步 · 异步预热**：Loading 阶段触发 SVC WarmUp / UE 启动期 PipelineCache 反序列化
5. **第五步 · 监控与回归**：监控首帧 PSO 编译耗时、变体数变化、hitch 分布

### 8.2 UE 工具集

| 工具 / 命令 | 用途 |
| --- | --- |
| `-logPSO` 启动参数 | 运行时收集 PSO 写入 `.upipelinecache` |
| `ShaderPipelineCacheTools expand` | 合成 `.stablepc.csv` |
| `r.ShaderPipelineCache.*` CVar | 控制 PSO Cache 行为（Enabled/ReportPSO/LogPSO/BatchSize） |
| `r.PSOPrecaching.*` CVar | 控制 PSO Precaching（D3D12 only） |
| `r.PSOPrecache.GlobalShaders` | 启动期全局 PSO 预编译开关 |
| `stat RHI` | 查看 PSO Cache 命中率 / 未命中率 |
| `ProfileGPU` | 捕获 GPU 编译耗时 |
| RenderDoc / SDP | 移动端真机 PSO 编译事件回放 |

### 8.3 Unity 工具集

| 工具 / 命令 | 用途 |
| --- | --- |
| ShaderVariantCollection 资产 | 圈定要预热的变体集合 |
| `ShaderVariantCollection.WarmUp()` | 运行时异步预热 API |
| `Shader.Find/ShaderUtil.ClearShaderCache` | 运行时清理损坏缓存 |
| Frame Debugger | 查看 PSO 切换 / SetPass 频率 |
| SRP Batcher 兼容检查 | Frame Debugger 看 Batch Size |
| Graphics → Shader Stripping | 构建时自动剔除未用变体 |
| Player Settings → Graphics APIs | 控制 Vulkan / Metal / GLES 选择 |
| Memory Profiler | 看 Shader 内存占用（>1GB 即异常） |

关键决策树：发现 hitch 怎么定位是 PSO 编译？

- Frame Debugger / `stat RHI` 看到**首次出现的 Shader** → 100% 是 PSO 编译
- ProfileGPU 抓到 `PipelineState::Create` 同步调用 → 确认
- RenderDoc 回放：**第一次 Draw Call 之前**有大量 Driver 内部时间 → PSO 编译
- 频繁 hitch 都集中在**新场景加载 / 新材质首次出现**时 → 预热不全

## 九、关键启示：对工程团队的几条建议

#### 对类似 URP / SRP 项目

- **把 SVC 当 CI 一等公民**：变体数超阈值直接 build fail
- 按场景拆 SVC + AssetBundle 异步加载，**别搞"全量预热"**
- `shader_feature` 替代 `multi_compile` 是治本，不是优化
- Shader Graph 拖节点要谨慎，**每个节点都可能是 ×2 变体**
- 检测到损坏 `ShaderCache` 自动清掉，不要让用户手动操作

#### 对 UE 移动端项目

- **手动 PSO Caching 仍是移动端唯一稳定方案**，PSO Precaching 暂时别指望
- `logPSO` 跑全场景是硬性要求，建议**脚本自动化**
- Material 重大修改必须重跑 `ShaderPipelineCacheTools`，**不要赌"老 cache 还能用"**
- Metal 项目（iOS）要**手动管理 PSO 数量**，没有 LRU 兜底
- 后台上线后监控 PSO Cache 命中率，**掉到 80% 以下就该警觉**

最容易被忽略的一点

**PSO 优化最大的敌人是"以为不卡"**。在开发机和大多数同事手机上可能完全感受不到，因为大家机器都很快；只有玩家群体里那一小撮低端机（Mali-G57 / 老款 Adreno）才会爆发式反馈"为什么我打开就卡 / 战斗时卡"。**把低端机当作 QA 的一等公民，PSO 治理才有意义**。

## 十、参考资料

### 10.1 官方文档 / 引擎团队

- Epic · **PSO Precaching（UE 5.6 文档）** — `dev.epicgames.com/documentation/.../pso-precaching-for-unreal-engine`
- Epic · **Optimizing Rendering With PSO Caches in UE 5.6**
- Epic · **为 Android 创建捆绑的 PSO 缓存（UE 5.3 文档）**
- Unity · **ShaderVariantCollection / WarmUp API 文档**
- Unity 官方开发者社区 · **《URP 管线主导的角色材质、阴影与显存动态适配优化方案》**（2025-10）

### 10.2 公开 Sample / 实战

- Meta · **Showdown Sample（Quest 2/3 · UE 5.6）** — `developers.meta.com/horizon/documentation/unreal/unreal-sample-showdown`，含 `GeneratePSOCache.bat` 完整流程
- Unreal Engine 4 · **PSO Caching on Android（UE 4.27 文档）**
- Windcrazy · **PSO（UE PSO 缓存优化安卓应用经验）** — `windcrazy123.github.io/2026/04/20/PSO`
- Bearhammer Games · **Adding PSO Cache To Unreal Engine Game For Oculus Quest 2**（2024-01）

### 10.3 社区博客 / 第三方

- ue5wiki · **UE 项目优化：PSO Caching（完整流程）** — `ue5wiki.com/wiki/24336/`
- CSDN · **材质变体 PSO 学习笔记（UE 5.4）**
- CSDN · **UE4 PSO 缓存实战：从构建到热更的完整流程解析**
- CSDN · **Unity URP 多线程渲染：理解 Shader 变体对加载时间的影响**
- CSDN · **Unity 性能优化：Shader 变体预热实战**
- 腾讯云开发者社区 · **《拆解 URP 管线角色材质失效：从现象到底层的深度排障与优化》**
- CSDN · **Unity Shader 编译优化：破解变体爆炸与编译卡顿**
- CSDN · **URP Lit 材质优化全指南：从参数原理到移动端实战**

### 10.4 玩家社区反馈（用于反推团队实践）

- 米游社 / 今日头条 · **原神 6.1 卡顿解决方法（着色器编译中断）**
- 今日头条 · **绝区零 3.0 手机端帧率低（高负载下 PSO 切换）**
- 搜狐 · **中配手机玩崩坏星穹铁道 这样调画质流畅不卡顿**

— 完 —

头部手游 PSO 方案汇总 · 基于公开资料整理 · 2026-07 · 小8 出品
文件独立离线可打开，无外部依赖
