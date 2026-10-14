# RUSH 性能指标与资产规范测算工具 — 能力分析

- 站点：`http://perfplatform.saroasis.woa.com:5173/`（内网 10.97.13.113:5173）
- 工程名：`performance-spec-tool` v0.2.0（`PerformanceTest`）
- 分析时间：2026-09-14
- 取证方式：直连 HTTP 拉取 `/`、`/src/app.js`、`/src/calculator.js`、`/server.js`、`/README.md`、`/api/data`，并用 Python 独立复算校验

---

## 0. 一句话结论

**它不是性能分析平台，是一张"可协作的参数化性能预算试算表"。**
不连设备、不抓帧、不做 profiling、不读 UE 工程。输入是"多少角色 × 什么 LOD + 场景面数/DC + 技能开销"，套一套机型系数，实时算出帧耗时 / FPS / 总面数 / 总 DC / 骨骼数 / 瓶颈。
所有数字的可信度**完全取决于 `model_params.csv` 里的系数有没有真机标定过**。

---

## 1. 技术栈与部署

| 项 | 内容 |
|---|---|
| 后端 | Node 原生 `http` 模块（无 Express），单文件 `server.js`，监听 `0.0.0.0:5173` |
| 前端 | 无框架，原生 ES module（`src/app.js` + `calculator.js` + `csv.js`），Chart.js 4.4.7 走 CDN |
| 存储 | 无数据库，`data/` 目录下纯文本 CSV / JSON |
| 后端接口 | `GET /api/data`、`POST /api/csv/:name`、`POST /api/json/:name`、`POST /api/commit`、`POST /api/reset-cache`、`POST /api/hints`、`POST/GET /api/cache-bundle` |
| 权限 | 单 token，默认 `admin`（可用环境变量 `ADMIN_TOKEN` 改），通过 `?admin=xxx` 或 `X-Admin-Token` 头传递 |

---

## 2. 四大功能模块

### 2.1 资产规范指标（8 类资产 × 5 指标 × 6 级 LOD）

`data/assets_*.csv`，文件名决定维度名，新增文件重启即自动生成表：

`hero` / `monster` / `monsterA` / `monsterB` / `monsterC` / `pet` / `role` / `weapon`

| metric | 含义 |
|---|---|
| `DC` | 该 LOD 的 Draw Call 基础值 |
| `triangles_w` | 面数（万面） |
| `bones` | 骨骼数 |
| `screen_size` | 该 LOD 的屏占参考值（**只录入，不参与计算**） |
| `DC_scale` | DC 缩放系数，实际 DC = DC × DC_scale（低配每个 LOD 的 pass 数） |

### 2.2 性能目标测算（主工作区）

`data/cases_rush.json`。当前 6 个 case：核心 PVE / 核心 PVP / 重度 / 重度-同屏降配 / Test / 重度-场景单独。

输入列：`case_id`(玩法) · `case_name` · `profile`(low/high) · `skill_cpu` · `skill_dc` · `skill_gpu` · `scene_triangles_w` · `scene_dc` · 各资产数量列（`hero数` 等，自动按资产文件生成）。

**数量支持 LOD 分布写法**：`1L1;2L2;3L3` = 1 个 LOD1 + 2 个 LOD2 + 3 个 LOD3；
写纯数字（如 `3`）表示 3 个全部用 `model_params.csv` 里的默认 `lod_index`。

输出列（自动算）：帧耗时 / 输出 FPS / 总面数(w) / 总 DC / 骨骼数 / 瓶颈。

### 2.3 GPU / CPU / RHI 负载直方图

Chart.js 柱状图，每个 case 三根柱，非瓶颈柱半透明，瓶颈柱实心并标红 `▼` + 数值。

### 2.4 性能模型参数

`data/model_params.csv`，按 `profile` 拆表，当前 low / high 两档，各 17 个参数 + desc 说明列。

| 参数 | low | high | 说明 |
|---|---|---|---|
| base_gpu_ms | 9.18 | 7 | 基础 GPU 耗时 |
| base_cpu_ms | 4.8 | 2.6 | 基础 CPU 耗时 |
| base_rhi_ms | 0.5 | 0.5 | 基础 RHI 耗时 |
| tri_cost_ms_per_w | 0.01 | 0.007 | 每万面 GPU 成本 |
| dc_gpu_cost_ms | 0.0016 | 0.00035 | 每 DC GPU 成本 |
| dc_cpu_cost_ms | 0.01 | 0.00036 | 每 DC CPU(RHI) 成本 |
| bone_cpu_cost_ms | 0.001 | 0.00045 | 每骨骼 CPU 成本 |
| player_gpu_ms | 0.3 | 0.3 | 主角 GPU 固定开销 |
| lod_index | 2 | 1 | 该档默认 LOD |
| role/monster/pet_cpu_ms | 0.08/0.16/0.05 | 0.05/0.1/0.03 | 单个实体 CPU 成本 |
| skill_gpu_scale / skill_cpu_scale | 0.8/0.5 | 0.55/0.35 | **定义了但未接线（见问题 2）** |

---

## 3. 协作与数据流转机制

| 角色 | 能做什么 |
|---|---|
| 访客（无 token） | 编辑所有表格 → 自动写入**自己 IP 的 cache**；上传/下载缓存文件夹；重置自己的 cache |
| admin（`?admin=admin`） | 访客全部权限 + 点「保存」把 cache 提交进 `data/` 正式文件 + 编辑三段说明文案 |

- cache 目录：`cache/<访客IP>/`，按 IP 隔离，加载时优先读 cache，没有才读 `data/`
- 改过的单元格会高亮（与 `fixed` 版本比对）
- 上传缓存文件夹：把本地 `.csv/.json` 灌进自己的 cache
- 下载缓存文件夹：用 File System Access API 把当前视角数据写到本地 `cache/`
- 保存 = `POST /api/commit`，把 admin 的 cache 提交到 `data/` 并清空 cache

---

## 4. 计算模型（已复算验证）

```
实体成本（逐资产 × 逐 LOD 分布累加）：
  entityTri   += count × triangles_w[LOD] × 3
  entityDc    += count × DC[LOD] × DC_scale[LOD]
  entityBones += count × bones[LOD]
  entityCpu   += 总数 × <资产名>_cpu_ms

总量：
  totalTri = scene_triangles_w + entityTri
  totalDc  = scene_dc + skill_dc + entityDc

三条耗时：
  GPU = base_gpu_ms + totalTri × tri_cost_ms_per_w + totalDc × dc_gpu_cost_ms + skill_gpu + player_gpu_ms
  CPU = base_cpu_ms + entityCpu + entityBones × bone_cpu_cost_ms + skill_cpu
  RHI = base_rhi_ms + totalDc × dc_cpu_cost_ms

输出：
  frame = max(GPU, CPU, RHI)      fps = 1000 / frame      瓶颈 = 最大项
```

**验证**：用 Python 独立实现上述公式，对 6 个 case 复算，结果与平台 `output_results.csv` **逐位一致**：

| case | profile | 帧耗时 | FPS | 总面数(w) | 总 DC | 骨骼 | 瓶颈 |
|---|---|---|---|---|---|---|---|
| 核心 PVE | low | 15.46 | 64.7 | 264.4 | 1463 | 1175 | GPU |
| 核心 PVP | low | 15.93 | 62.8 | 235.3 | 1543 | 995 | RHI |
| 重度 | low | 20.00 | 50.0 | 423.1 | 1950 | 2550 | RHI |
| 重度-同屏降配 | low | 18.05 | 55.4 | 339.4 | 1755 | 1715 | RHI |
| Test | low | 15.93 | 62.8 | 235.3 | 1543 | 995 | RHI |
| 重度-场景单独 | high | 10.20 | 98.0 | 162.9 | 756 | 1957 | GPU |

### 4.1 手算复核：核心 PVE（low）逐步展开

输入：`hero=1L1;2L2`、`weapon=1L1;2L2`、`monsterA=2L2;1L3`、`monsterB=1L1;1L3`、`monsterC=1L0`、`pet=1L1;2L2`，`scene_triangles_w=190`、`scene_dc=1100`、`skill_gpu=1`、`skill_cpu=1`、`skill_dc=100`。
low 档关键参数：`lod_index=2`、`base_gpu=9.18`、`base_cpu=4.8`、`base_rhi=0.5`、`tri_cost=0.01`、`dc_gpu_cost=0.0016`、`dc_cpu_cost=0.01`、`bone_cost=0.001`、`player_gpu=0.3`、`pet_cpu=0.05`。

**第一步：实体聚合**

| 资产 | LOD 分布 | 面数 = Σ(count × tri × 3) | DC = Σ(count × DC × DC_scale) | 骨骼 = Σ(count × bones) |
|---|---|---|---|---|
| hero | 1×L1, 2×L2 | 1×4×3 + 2×1.5×3 = **21.0** | 1×6×6 + 2×5×6 = **96** | 1×356 + 2×183 = **722** |
| weapon | 1×L1, 2×L2 | 1×2×3 + 2×1×3 = **12.0** | 1×4×6 + 2×4×6 = **72** | 1×20 + 2×15 = **50** |
| monsterA | 2×L2, 1×L3 | 2×0.8×3 + 1×0.4×3 = **6.0** | 2×2×4 + 1×1×2 = **18** | 2×30 + 1×30 = **90** |
| monsterB | 1×L1, 1×L3 | 1×2.5×3 + 1×0.5×3 = **9.0** | 1×5×5 + 1×1×2 = **27** | 1×80 + 1×43 = **123** |
| monsterC | 1×L0 | 1×8×3 = **24.0** | 1×7×5 = **35** | 1×120 = **120** |
| pet | 1×L1, 2×L2 | 1×0.4×3 + 2×0.2×3 = **2.4** | 1×1×5 + 2×1×5 = **15** | 1×30 + 2×20 = **70** |
| 合计 | | **entityTri = 74.4** | **entityDc = 263** | **entityBones = 1175** |

`entityCpu = 3(pet 总数) × 0.05 = 0.15`（hero / weapon / monsterA/B/C 全部取不到 `_cpu_ms` 参数 → 计 0，见问题 1）

**第二步：总量**

```
totalTri = 190 + 74.4  = 264.4 (w)
totalDc  = 1100 + 100 + 263 = 1463
```

**第三步：三条耗时**

```
GPU = 9.18 + 264.4 × 0.01 + 1463 × 0.0016 + 1 + 0.3 = 15.4648 ms
CPU = 4.8  + 0.15     + 1175 × 0.001    + 1       =  7.125  ms
RHI = 0.5  + 1463 × 0.01                          = 15.13   ms
```

**第四步：输出**

```
frame = max(15.4648, 7.125, 15.13) = 15.4648 ms
fps   = 1000 / 15.4648 = 64.66
瓶颈  = GPU（GPU 15.46 与 RHI 15.13 仅差 0.33ms，属于临界状态）
```

> 注意：这个 case 的 GPU 与 RHI 只差 2%。low 档 `dc_cpu_cost_ms=0.01` 是否经过真机标定，直接决定"瓶颈"列的可信度（见问题 1 与文末待核实项）。

### 4.2 公式是怎么来的：手工搭建的线性预算模型，不是拟合出来的

**结论：公式结构是先按"GPU / CPU / RHI 三条线性加和、取最大值"的经验框架搭好的，参数再用少量实测 + 大量估算 + 档位间折算填进去。代码里没有任何标定、拟合、回归能力。**

**证据 1：代码层面完全没有标定入口**

`server.js` 全部 API 只有 8 个：`/api/data` `/api/csv` `/api/session` `/api/hints` `/api/cache-bundle` `/api/commit` `/api/reset-cache` `/api/health`——全是数据读写与缓存管理。全文搜 `fit / regress / calibrate / train / sample / measure` **零命中**。也就是说：没有"导入实测帧数据自动反解参数"这回事，参数只能人肉填。

**证据 2：参数表没有任何溯源元信息**

`model_params.csv` 只有 `profile, param, value, desc` 四列，`desc` 全是"低配基础GPU耗时"这类描述，**没有机型名、没有测量日期、没有分辨率/帧率上限、没有测量方法（profiler / stat gpu / 抓帧）**。真机标定过的参数表通常至少会带机型和条件。README 也完全没有"参数怎么标定"的章节。

**证据 3：low → high 是「×0.7 手工取整」推出来的（关键证据）**

把 14 个参数按 `high ÷ low` 排序：

| 参数 | low | high | 比值 | low × 0.7 | 判定 |
|---|---|---|---|---|---|
| base_rhi_ms | 0.5 | 0.5 | 1.000 | 0.35 | 未区分机型 |
| player_gpu_ms | 0.3 | 0.3 | 1.000 | 0.21 | 未区分机型 |
| base_gpu_ms | 9.18 | 7 | 0.763 | 6.426 | 单独填 |
| tri_cost_ms_per_w | 0.01 | 0.007 | **0.700** | 0.007 | **精确 = ×0.7** |
| skill_cpu_scale | 0.5 | 0.35 | **0.700** | 0.35 | **精确 = ×0.7** |
| skill_gpu_scale | 0.8 | 0.55 | 0.688 | 0.56 | ×0.7 后取整 |
| role_cpu_ms | 0.08 | 0.05 | 0.625 | 0.056 | ×0.7 后取整 |
| monster_cpu_ms | 0.16 | 0.1 | 0.625 | 0.112 | ×0.7 后取整 |
| pet_cpu_ms | 0.05 | 0.03 | 0.600 | 0.035 | ×0.7 后取整 |
| base_cpu_ms | 4.8 | 2.6 | 0.542 | 3.36 | 单独填 |
| lod_index | 2 | 1 | 0.500 | — | 设计选择，非性能比 |
| bone_cpu_cost_ms | 0.001 | 0.00045 | 0.450 | 0.0007 | 单独填 |
| dc_gpu_cost_ms | 0.0016 | 0.00035 | **0.219** | 0.00112 | **严重偏离** |
| dc_cpu_cost_ms | 0.01 | 0.00036 | **0.036** | 0.007 | **严重偏离 27.8 倍** |

**6 个单价类参数（`tri_cost` / `skill_*_scale` / `role|monster|pet_cpu_ms`）精确或近似等于 low × 0.7 后手工取整**——说明 high 档不是独立标定的，是拿一个"高配比低配快 1.43 倍"的统一折扣系数乘出来的。

其中 `dc_cpu_cost_ms` 是唯一彻底破坏规律的项：按 0.7 规律应该是 0.007，实际 0.00036，差 19.4 倍。它（连同 `dc_gpu_cost_ms`）一定是被单独改过——很可能是某次发现"低配 RHI 爆了 / 高配 RHI 从来不爆"之后手调的，且**没有回头同步另一档，也没有留下任何记录**。

**证据 4：`9.18` 是全表唯一带两位小数的参数**

其余参数都是一位小数（4.8 / 0.5 / 0.3 / 2.6）或整数（7 / 1 / 2）。只有 `low.base_gpu_ms = 9.18` 带两位小数——大概率是从某个 profiler 里抄下来的单次测量值，其余是估的或凑整的。

**证据 5：反推预算——low 档实质是「DC 一元约束」**

按 60fps（16.667ms）反推各维度硬上限（其余项取 0）：

| 档位 | DC（RHI 卡） | DC（GPU 卡） | 面数 | 骨骼 |
|---|---|---|---|---|
| low | **1617** | 4492 | 719 w | 11867 |
| high | 44907 | 26762 | 1338 w | 31259 |

再看 6 个 case 的实际占用率：

| case | 档位 | DC 占用 | 面数占用 | 骨骼占用 |
|---|---|---|---|---|
| 核心 PVE | low | 90.5% | 36.8% | 9.9% |
| 核心 PVP | low | 95.4% | 32.7% | 8.4% |
| 重度 | low | **120.6%** | 58.9% | 21.5% |
| 重度-同屏降配 | low | **108.6%** | 47.2% | 14.5% |
| Test | low | 95.4% | 32.7% | 8.4% |
| 重度-场景单独 | high | 1.7% | 12.2% | 6.3% |

**DC 全部顶到 90~120%，面数只用了 33~59%，骨骼只用 8~22%。** 三个维度不可能同时被正确标定——如果面数和骨骼的系数是对的，它们也该在某个点卡住，而不是永远差一个数量级。真相更可能是：只有 DC 的系数被"调到手感对了"，面数和骨骼的系数从未被检验过。

这正好解释了为什么 6 个 case 里 4 个判成 RHI 瓶颈——不是因为 RHI 真的慢，是因为只有 DC 这个维度的系数被调过。

### 4.3 公式固定吗：三层结构，固定程度不同

| 层 | 内容 | 能否改 | 怎么改 |
|---|---|---|---|
| **公式骨架** | `max(GPU, CPU, RHI)`、各项加减关系、`×3`、DC×DC_scale | **硬编码，改不了** | 改 `src/calculator.js:111-144` 后重启。README 第 10 节自己承认"改计算公式：改 `src/calculator.js`" |
| **机型参数** | low/high 各 17 项 | **页面上可直接改**（admin 保存） | 改 `model_params.csv`，或 admin 在页面编辑，改完立即重算 |
| **资产维度** | 8 类资产 × 5 指标 × 6 LOD | **加文件自动扩展** | 加 `data/assets_<名>.csv` 后重启，页面自动出现表和数量列 |
| **case 输入** | 数量 / LOD / 场景 / 技能 | 随便改 | 页面或 `cases_rush.json` |

所以：**骨架是固定的，系数是可调的，但「调」没有流程保障**——没有版本记录、没有变更说明、没有回滚（README 写了 `backups/` 目录，但 `server.js` 里搜不到任何备份逻辑，实际不会生成）、没有并发锁、admin token 默认 `admin` 谁都能加 `?admin=admin`。

**资产维度自动扩展还有个坑**：加了新的 `assets_*.csv` 只会自动生成表和数量列，`<资产名>_cpu_ms` 参数**必须手动往 `model_params.csv` 补**。这就是问题 1 的根源——monsterA/B/C、hero、weapon 五类资产的 CPU 单价至今没补，实体 CPU 成本恒为 0。

### 4.4 这套模型的准确定位

它不是"性能预测模型"（没有预测能力，误差未知且从未验证），而是**预算分配器 / 口径对齐工具**：把"我们要做多少角色、多少面、多少 DC"翻译成"大概多少毫秒"，让策划、美术、程序在同一个数字上对齐，并在超标时快速看出是哪项超了。

按这个定位用，它是合格的。把它当"真机会跑多少帧"用，目前的参数支撑不了。

---

## 5. 实测发现的问题（按影响排序）

### 问题 1｜5 类资产缺 CPU 单价参数，CPU 项被系统性低估 —— 高

`model_params.csv` 里只有 `role_cpu_ms` / `monster_cpu_ms` / `pet_cpu_ms` 三个实体 CPU 单价。
但 case 里真正在用的是 `hero数` / `weapon数` / `monsterA数` / `monsterB数` / `monsterC数`，这 5 个在参数表里**根本没有对应项**，`m['hero_cpu_ms']` 取到 `undefined → 0`。

即：复算确认 6 个 case 全部命中「hero, weapon, monsterA, monsterB, monsterC 缺 CPU 参数」。
结果：**这些资产的实体 CPU 成本恒等于 0**，CPU 项实际只剩「基础 + 骨骼 + 技能」。这会系统性压低 CPU、把瓶颈判定推向 RHI / GPU。
（`role数` 和 `monster数` 在当前所有 case 里都是 0，所以已有的 3 个参数反而在空转。）

### 问题 2｜`skill_gpu_scale` / `skill_cpu_scale` 定义了但未接线 —— 中

参数表里两个档位都配了技能权重（low 0.8/0.5，high 0.55/0.35），但 `calculator.js` 里**完全没有引用这两个 key**，`skill_gpu` / `skill_cpu` 是原样相加。改这两个参数对结果毫无影响。

### 问题 3｜面数乘 3，代码与 README 不一致 —— 中

`calculator.js:127` 实际是 `entityTri += item.count * picked.tri * 3`，而 `README.md` 第 6 节写的是 `entityTri += count * tri`（无 ×3）。
这个 3 要么是"三渲二/描边多 pass"之类的经验系数，要么是历史遗留。文档和代码必须对上，否则后来人对不上账。

### 问题 4｜`monster` 维度 DC_scale 全 0 —— 低（当前未触发）

`assets_monster.csv` 的 `DC_scale` 六档全是 0，意味着一旦往 case 里填 `monster数`，其 DC 贡献恒为 0（面数和骨骼不受影响）。当前所有 case 的 `monster数` 都是 0，所以还没暴露。

### 问题 5｜`screen_size` 只录不算 —— 低

屏占参考值录进了资产表，但公式里没有任何地方用到。如果要做"按屏占自动选 LOD"，这里还差一段逻辑。

### 问题 6｜工程化与协作风险 —— 中

- **admin token 默认 `admin`**：任何知道地址的人加 `?admin=admin` 就能覆盖 `data/` 正式数据，无审计、无操作日志。
- **无自动备份**：README 目录结构里写了 `backups/`，但 `server.js` 全文检索不到 backup 相关逻辑，正式数据被覆盖后无法回滚。
- **无并发锁**：写 cache / commit 都是直接 `fs.writeFile`，多标签页或多人同时编辑会互相覆盖。
- **cache 按 IP 隔离 ≠ 按人隔离**：同一 NAT 出口的人共用一个 cache 目录，会互相踩。

### 需要向业务方核实的点（不下判断，只提示）

`low` 档 `dc_cpu_cost_ms = 0.01`（每 DC 0.01ms）对比 `high` 档 `0.00036`，差 **27.8 倍**。按这个系数，低配 1950 DC 光 RHI 就 19.5ms —— 这也是 6 个 case 里 4 个判定为 RHI 瓶颈的直接原因。
**这个系数是否有真机实测支撑，决定了"瓶颈"这一列结论是否成立。** 建议优先标定它。

---

## 6. 隐藏彩蛋：`reference_games.csv`

`data/` 里存在但前端未加载的竞品对标表：

```csv
game,case,profile,avg_fps,low_fps,pass_rate,desc
BR,核心,low,,,,老玩法参考，待填
BR,重度,low,,,,老玩法参考，待填
三角洲-烽火地带,核心,high,,,,搜打撤参考，待填
ARC,核心,high,,,,带怪搜打撤参考，待填
```

数据全空，属于**规划中但没做完**的「竞品帧率对标」模块。

---

## 7. 使用建议

1. **当预算表用，别当测量工具用。** 它回答的是"按这套系数，这个配置能不能跑到目标帧率"，不回答"这个场景现在实际跑多少帧"。
2. **改之前先标定 `model_params.csv`。** 特别是 `dc_cpu_cost_ms`、`tri_cost_ms_per_w`、`base_*_ms` 这三项，决定了整张表的可信度。
3. **补上 5 类缺失的 CPU 单价参数**（hero / weapon / monsterA / monsterB / monsterC），否则 CPU 和瓶颈判定不可信。
4. **访客视角随便试**：改完自动进自己的 cache，不污染正式数据；想落地再让 admin 提交。
5. **内网共享前改掉默认 admin token**，并加一份 `data/` 的自动备份。
