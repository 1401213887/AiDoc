# BuildSVTVHM 流水线启动参数分析

> 数据来源：蓝盾 `devops.woa.com` 构建详情 API，抓取时间 2026-10-24 22:50
> 分析对象：build `b-89b44ef6049941a0855fe5ca470945d0`
> 说明：所有数值均来自接口原始 JSON，未做推断补全

---

## 一、身份与本次构建

| 项 | 值 |
|---|---|
| 蓝盾工程 | `grgame` |
| 流水线名 | **BuildSVTVHM** |
| 流水线 ID | `p-8f19609a6ae04103811c75d31d6c584d` |
| 构建 ID | `b-89b44ef6049941a0855fe5ca470945d0` |
| 构建号 | #14907 |
| 编排版本 | v57（latest v57，即当前最新） |
| 触发方式 | 手动（trigger=`手动`） |
| 执行人 / 触发人 | `djangozhang` |
| 状态 | SUCCEED |
| 开始 | 2026-10-24 17:31:17 |
| 结束 | 2026-10-24 21:09:21 |
| 墙钟总耗时 | **3.63 h** |
| 最后修改人 | `v_wushzhu` |
| 模板 | `35f100f163684af3b28c885d33a814e5` / **V20(P18.T4.20)**，`instanceFromTemplate=true` |

## 二、时间拆解（这是最值得注意的一项）

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 排队等构建机 | **116.1 min** | `startEpoch(19:27:22) - startTime(17:31:17)` |
| 实际构建 | **101.6 min** | `executeCost = 6095155 ms` |
| 合计墙钟 | 218.0 min | `totalCost` |

⚠️ **字段口径提醒**：`queueCost` 字段值 13081742 ms（218.0 min）并不等于纯排队时长，
它实际等于 `排队 + 执行` 的总墙钟。纯排队必须用 `queueCost - executeCost`
（= 6986587 ms ≈ 116.4 min）或 `startEpoch - startTime` 反算。按 `queueCost` 直接读会误判成"排了 3.6 小时"。

> 结论：**排队时间（1.93 h）超过了实际构建时间（1.69 h）**。构建机资源是这条流水线的主要瓶颈，
> 不是编译本身。使用机器：`NODE = BUILD_Win46`（单机 `isSingle: true`）。

## 三、启动参数全表（42 项）

启动参数页显示的就是这份列表（来源：`model.template.templateVariables`，
`overrideTemplateField.paramIds` 列出的同样这 42 项）。
`[可改]` = `allowModifyAtStartup: true`，即每次启动时可在启动参数页覆盖。

### 3.1 代码与同步

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 1 | `BRANCH_NAME` | `main` | 是 |
| 2 | `CHANGELIST` | *(空 → 取最新 CL)* | 是 |
| 3 | `ORIGINAL_WORKSPACE` | `D:\projects` | 是 |
| 4 | `SYNC_MODE` | `Full`（全量同步） | 是 |
| 5 | `UPDATE_P4` | `true` | 是 |
| 6 | `UPDATE_PROJECT_TOOLS` | `true` | 是 |
| 7 | `SAME_CL_SKIP_CI` | `false`（同 CL 不跳过） | 是 |
| 8 | `CHECK_TAGS_SUCC_CL` | *(空)* | 是 |
| 9 | `AUTO_SUCC_CL` | `false` | 是 |

### 3.2 构建配置

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 10 | `CONFIGURATION` | `Development` | 是 |
| 11 | `BUILD_PLATFORM` | `Win64` | 是 |
| 12 | `CLIENT_NAME_EXT` | `BuildSVTVHM` | 是 |
| 13 | `NODE` | `BUILD_Win46` | 是 |
| 14 | `GAME_NAME` | `S1Game` | **否（锁定）** |
| 15 | `TIMEOUT_BUILD_TASK` | `200` | 是 |
| 16 | `TIMEOUT_BASE_TASK` | `3000` | 是 |

### 3.3 编辑器与二进制

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 17 | `SWITCH_PRE_BUILD_EDITOR` | `true` | 是 |
| 18 | `VIRTUALIZATION_ENABLE` | `false` | 是 |
| 19 | `USE_BINARY` | `false` | 是 |
| 20 | `AUTO_START_HA` | `true` | 是 |
| 21 | `AUTO_START_SA` | `false` | 是 |

### 3.4 自定义脚本

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 22 | `CUSTOM_SCRIPT_MULTILINE` | `\S1Game\Scripts\Function_Engine\BuildSVTVHM3.bat;` | 是 |
| 23 | `POST_SCRIPT_MULTILINE` | *(空)* | 是 |
| 24 | `CUSTOM_MARCO` | *(空)* | 是 |
| 25–34 | `OTHER_ARG_1` ~ `OTHER_ARG_10` | **全部为空** | 是 |

### 3.5 触发器

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 35 | `TIMER_CRONTAB_STR` | `0 0 0 ? * * 2022-2023` | **否（锁定）** |
| 36 | `TRIGGER_LISTEN_PATHS` | `//GR/XXStream/**` | 是 |
| 37 | `TRIGGER_EXCLUDE_PATHS` | `//GR/EXCLUDEXXX/**` | 是 |
| 38 | `CIRCLE_CALL_SELF_PIPE` | `false` | 是 |

### 3.6 通知

| # | 参数 | 本次值 | 可改 |
|---|---|---|---|
| 39 | `NOTIFY_BOT_URL` | *(null)* | **否（锁定）** |
| 40 | `NOTIFY_SUCC_MSG` | *(空)* | **否（锁定）** |
| 41 | `NOTIFY_ERROR_MSG` | *(空)* | **否（锁定）** |
| 42 | `NOTIFY_UGS_TAG` | *(空)* | **否（锁定）** |

## 四、编排结构

### Stage 1 `stage-1` — 触发器容器（4 个触发器）

| 触发器 | 类型 | 状态 |
|---|---|---|
| Manual | `manualTrigger` | 本次使用 |
| Timer | `timerTrigger` | cron 见问题 ② |
| P4 | `codeP4WebHookTrigger` | 监听路径见问题 ③ |
| Remote | `remoteTrigger` | — |

### Stage 2 `stage-2` — 构建环境-Windows（`vmBuild`，jobId `job_l5J`）

| # | 步骤 | 插件 | 结果 |
|---|---|---|---|
| 1 | 设置环境变量-ProjectTools | `windowsScript` | SUCCEED |
| 2 | 更新P4-ProjectTools | `run` (pwsh) | SUCCEED |
| 3 | InitEnv | `windowsScript` | SUCCEED |
| 4 | 计算获取成功CL | `run` (pwsh) | SUCCEED |
| 5 | 同步P4&共享盘 | `run` (pwsh) | SUCCEED |
| 6 | 上传状态到工坊-开始 | `run` (pwsh) | SUCCEED |
| 7 | **编译Editor** | `run` (pwsh) | SUCCEED |
| 8 | 执行自定义操作-Pre | `run` (pwsh) | SUCCEED |
| 9 | 执行自定义操作 | `run` (pwsh) | SUCCEED |
| 10 | 归档构件 | `UploadArtifactory` | **SKIP**（见问题 ①） |
| 11 | 构建成功的操作 | `run` (pwsh) | SUCCEED |
| 12 | 构建失败的操作 | `run` (pwsh) | SKIP（正常，因构建成功） |
| 13 | 循环触发自身 | `run` (pwsh) | SUCCEED |
| 14 | Post-执行自定义操作 | `run` (pwsh) | SUCCEED |

关键脚本入口（均为 pwsh，`charsetType=UTF_8`）：
- 编译：`编译Editor` 步骤，内部按 `SAME_CL_SKIP_CI` / `IS_LAST_CL_SAME` 做同 CL 短路
- 自定义：`\S1Game\Scripts\Function_Engine\BuildSVTVHM3.bat`
- 生命周期上报：`python D:\ProjectTools\GameProject\Tools\BuildScripts\BuildNotify\PipelineLifecycle.py --status start|success|failure`

## 五、发现的问题

### ① 归档构件被跳过，因目标路径变量为空

```
folderPath = ${OTHER_ARG_10}     ← OTHER_ARG_10 本次为空
destPath   = ./${BRANCH_NAME}/${GAME_NAME}/${OTHER_ARG_8}
             = ./main/S1Game/    ← OTHER_ARG_8 也为空
```
`OTHER_ARG_1~10` 十个参数全空，但其中 `OTHER_ARG_8`（制品子目录）和 `OTHER_ARG_10`（归档源目录）
是被实际引用的。**本次构建没有产出任何归档构件**（status=SKIP）。
如果这条线本该归档 Editor 产物，说明参数没填，需要补。

### ② Timer 触发器的 cron 已过期

```
TIMER_CRONTAB_STR = 0 0 0 ? * * 2022-2023
```
年份段写死 `2022-2023`，当前 2026 年，**定时触发早已失效**。
且该参数 `allowModifyAtStartup=false`（启动页改不了），只能在编排里改。

### ③ P4 触发器的监听路径仍是模板占位符

```
TRIGGER_LISTEN_PATHS  = //GR/XXStream/**
TRIGGER_EXCLUDE_PATHS = //GR/EXCLUDEXXX/**
```
`XXStream` 和 `EXCLUDEXXX` 是模板（V20）里的示例占位符，不是真实路径。
说明 P4 WebHook 触发**要么从没配对，要么配了但没生效**——按这个路径监听不到任何真实提交。
建议核对是否应为 `//GR/DevTest/...` 之类的真实分支路径。

### ④ 冗余参数

`OTHER_ARG_1~7`、`OTHER_ARG_9` 在编排中无引用（仅 8、10 被用到），属于模板遗留。
42 个参数里有 8 个是纯占位，启动页会显示一堆空框。

## 六、接口备忘（可复用）

Cookie 鉴权，前缀 `{BASE}/ms/{module}/api/`，工程身份靠 header `X-DEVOPS-PROJECT-ID: grgame`。

| 用途 | 方法 | 路径 |
|---|---|---|
| 当前用户 | GET | `/ms/project/api/user/users` |
| 流水线编排 | GET | `/ms/process/api/user/pipelines/{projectId}/{pipelineId}` |
| 构建详情 | GET | `/ms/process/api/user/builds/{projectId}/{pipelineId}/{buildId}/detail` |

**注意：**
- `{projectId}` 必须夹在中间（`grgame`），少一层会被当成把 pipelineId 当工程 ID，返回 403「无权限在工程 p-xxx 下查看流水线」
- 内部版**没有** `startupParams` 接口（各种 404/405），构建详情里的 `model.template.templateVariables` 即启动参数
- 触发构建用 `/ms/process/api/external/pipelines/{pipelineId}/build`（见「循环触发自身」步骤脚本）
- Cookie 会过期（千牛 session 级），过期后从浏览器重新 Copy as cURL 更新 `secrets/bluedun_cookie.txt`

工具脚本：`C:/Users/djangozhang/.workbuddy/tools/bluedun.py`

---

## 七、本次构建提交了哪些资产

### 7.1 先纠正一个理解：这条线不是「编译 Editor」

步骤名叫「编译Editor」，但 `执行自定义操作` 真正跑的是
`\S1Game\Scripts\Function_Engine\BuildSVTVHM3.bat`：

```bat
set InMapDir=%1
call .\BuildSVTVHM2.bat SVT %InMapDir%
call .\BuildSVTVHM2.bat VHM %InMapDir%
```

`BuildSVTVHM2.bat` 的实际动作（`S1Game/Scripts/Function_Engine/BuildSVTVHM2.bat`）：

1. `SourceControl_Enable.bat` 启用源码控制
2. `P4_Revert.bat -force` 先 revert 干净
3. 跑 UE Commandlet：
   - `SVT` → `-Builder=WorldPartitionRuntimeVirtualTextureBuilder`
   - `VHM` → `-Builder=WorldPartitionVirtualHeightfieldMeshBuilder`
   - 公共参数：`-AllowCommandletRendering -unattended -DDC=LocalAndShared`
     `-ExecCmds="...r.vt.ForceBakeRVTFromWPGrid 1,r.VT.RVT.EnableDepthTest 1,Landscape.CouldUseQuad 0,r.VT.RVT.ForceLod 0..."`
4. `P4_Submit.bat -message "[\<TAG\>] Commit \<MapName\>" -reopen` 提交
5. `UGS_PostBadgeStatus.bat` 上报徽章状态

**本质：对 World Partition 地图烘焙 RVT（运行时虚拟纹理）/ VHM（虚拟高度场），
把烘焙产物提交回 P4，供全组共享。**

### 7.2 本次构建（#14907）实际提交

| 项 | 值 |
|---|---|
| Changelist | **1217235** |
| 提交时间 | 2026-10-24 **21:08:42**（构建结束 21:09:21 前 39 秒） |
| 提交人 / workspace | `tools@BK-FEIYULLIU-PC61-main_BuildSVTVHM` |
| 描述 | `[VHM] Commit NordLand` |
| 文件数 | **2（均为 edit，非新增）** |

```
//GR/main/S1Game/Content/Maps/Nord/NordLand_sharedassets/BridgeAsset/IndexMap/
    RVT_BridgeLandscape_Mask.uasset#235                edit
    RVT_BridgeLandscape_WorldHeight_MinMax.uasset#235  edit
```

### 7.3 这条流水线惯常提交的资产类型

从同流水线其它 CL 归纳（`p4 describe` 实测）：

| 阶段 | 提交的资产 | 数量 |
|---|---|---|
| `[SVT]` | `RVT_Color_<图>_SVT.uasset`、`RVT_Height_<图>_SVT.uasset`、`RVT_<图>_Thumbnail_Minimap.uasset`、`RVT_White_<图>_SVT.uasset` | 3~4 |
| `[VHM]` | `RVT_BridgeLandscape_Mask.uasset`、`RVT_BridgeLandscape_WorldHeight_MinMax.uasset` | 2 |

例（CL 1217356 `[SVT] Commit NeoRing`）：
```
//GR/main/S1Game/Content/Maps/NeoRing/NeoRing_sharedassets/
    RVT_Color_NeoRing_SVT.uasset#443
    RVT_Height_NeoRing_SVT.uasset#443
    RVT_NeoRing_Thumbnail_Minimap.uasset#443
    RVT_White_NeoRing_SVT.uasset#443
```

### 7.4 观察到的两个疑点

**① 本次只有 VHM 提交，没有对应 SVT 提交 —— 已用构建日志定论：SVT 崩溃了，一个文件都没产出**

> ⚠️ 本条此前基于 P4 侧推断为「SVT 跑了但产物与上一版一致，被 `revert -a` 撤掉」。
> **拿到构建日志后该推断被推翻。** 真实原因是 **SVT 阶段最后阶段 D3D12 显存耗尽（`E_OUTOFMEMORY`）**
> 导致 UE 进程中断，`p4` 侧一个文件都没被 checkout 过。以下为日志实证。

日志文件：`D:\DownLoad\Compressed\1.log(2)\1.log`，413 MB / 2,394,425 行。

**关键时间线（SVT 阶段 89 分 51 秒，全部作废）**

| 时间 | 事件 | 日志行 |
|---|---|---|
| 19:32:32 | `----------BuildSVT...`，`call .\BuildSVTVHM2.bat SVT /Game/Maps/Nord/NordLand` | 590 / 479 |
| 19:32:33 | UE 启动，`-Builder=WorldPartitionRuntimeVirtualTextureBuilder` | 596 |
| 19:37:30 | `LogWorldPartitionBuilderCommandlet: Execution started...` | 2219 |
| 19:38:41–43 | `[RVT Bake]` 设置 CVar：`ForceLod 0`、`Nanite 0`、`EnableDepthTest 1`、`CouldUseQuad 0`、`WaterDebugLog 1` | 3547–3684 |
| 19:38:43 → 20:31:31 | **约 53 分钟几乎无日志**（真实 RVT 渲染烘焙期） | — |
| 20:31:31 | `[ForceBakeRVT]` 收集 Actor，`-> GlobalRVTNum=1` | — |
| 20:31:54 → 20:47:15 | WaterGather 第 1 层（`RVTFlags=0x20`，MaxLevel=7），15.3 min / 1.15 M 行 | 12069–1158966 |
| 20:47:16 → 21:00:03 | WaterGather 第 2 层（`RVTFlags=0x10`，MaxLevel=7），12.8 min / 1.15 M 行 | 1159038–2305916 |
| 21:00:05 → 21:01:45 | WaterGather 第 3 层（`RVTFlags=0x8`，MaxLevel=11），**只走到 vLevel 7/11 就炸** | 2305988–2392017 |
| **21:01:45.963** | **`D3D12PoolAllocator.cpp:105 CreateHeap failed` → `E_OUTOFMEMORY`** | 2392019–2392021 |
| 21:01:45.994 | `CreateCommittedResource failed`：4 MB "Fast Allocator Page"，`AvailablePhysicalMB=1111.24` | 2392031–2392038 |
| 21:02:22.230 | `p4 revert -a` → **`##[error]File(s) not opened on this client.`** | 2392044 |
| 21:02:22.542 | `p4 submit -d "[ScriptCommit] [SVT] Commit NordLand"` → **`##[error]No files to submit`** | 2392046 |
| 21:02:22.808 | SVT 徽章上报 **`-Status=Success`** ← **假阳性** | 2392050 |

**日志里没有任何收尾标记**：全程搜不到 `LogExit`、`Log file closed`、`Unhandled Exception`，
SVT 也**没有**打印 `Execution took`（VHM 有：`Execution took 2:40 min`，行 2394092）。
进程在 21:01:45.995 后静默终止，36 秒后脚本继续往下走。

**这次 SVT 到底做了多少活**

- 日志 99.4% 是 `[RVT WaterGather]` 重复打印：**2,379,790 / 2,394,425 行**，
  其中 `Enter` 1,189,895 次，但只涉及 **35 个**不同水体图元 → 相当于把同一批图元扫了约 3.4 万遍。
- 三层 RVT（`0x20` / `0x10` / `0x8`）各自循环，前两层跑完，第三层（12 级 mip，MaxLevel=11）在 vLevel 7 崩掉。
- 结论：**烘焙确实在真跑，但没有走到写盘阶段就死了**，所以 P4 侧完全无痕。

**为什么构建还报成功 —— 两道校验同时失效**

第一道：`BuildSVTVHM2.bat` 第 66–75 行
```bat
call ...UnrealEditor.exe ... -Builder=%Builder% ...
set UE_ErrorLevel=%errorlevel%
if %UE_ErrorLevel% NEQ 0 ( ... exit /b %UE_ErrorLevel% )
```
→ 实际 `errorlevel` 被判定为 **0**，否则脚本会在 21:01 就 `exit /b`，不会进 `CommitAll`。
UE 在 OOM 后静默退出却没返回非 0，这是 UE 侧的问题（需引擎组确认）。

第二道：`P4_Submit.bat` **永远返回 0**。第 53 行 `p4 submit` 失败后，脚本继续执行
```bat
cd /d %WORKING_DIR%      ← 61 行，cd 成功 → errorlevel 归 0
echo Commit completed.   ← 63 行，echo 成功 → errorlevel 归 0
endlocal
pause
```
全脚本没有任何 `exit /b %errorlevel%`。所以 `BuildSVTVHM2.bat` 第 82 行
`if %errorlevel% NEQ 0 exit /b` 形同虚设，日志里的 `##[error]No files to submit`
只是一个标记，不影响退出码。

> 修正此前一处实测结论：之前测过「`pause` 不重置 errorlevel」是对的，
> 但真正吞掉 `p4 submit` 失败码的是更前面的 **`cd` 和 `echo`**，我上次定位错了对象。

**为什么会爆显存 —— 指向 10/23 新加的诊断开关**

`p4 filelog` 显示 `BuildSVTVHM2.bat` 只有两个版本，`#2` 由
**CL 1212551 / 2026-10-23 19:50:24 / `--user=陈星年` / story 1105370**（水体 RVT 不显示 bug）提交，
同 CL 还改了 3 个引擎文件（`WorldPartitionRuntimeVirtualTextureBuilder.cpp`、
`PrimitiveComponent.cpp`、`RuntimeVirtualTextureRender.cpp`）。

`#1 → #2` 的 diff 只加了执行参数（`-DisableObjectPool` 原本就有，不是本次引入）：

```diff
-ExecCmds="linker.TreatVerifyImportErrorsAsWarnings 1"
+ExecCmds="linker.TreatVerifyImportErrorsAsWarnings 1,sg.EffectsQuality 4,r.Nanite 0,
+  r.vt.ForceBakeRVTFromWPGrid 1,r.VT.RVT.EnableDepthTest 1,Landscape.CouldUseQuad 0,
+  r.VT.RVT.ForceLod 0,r.VT.RVT.WaterDebugLog 1" -RVTWaterDebug
```

其中两个对显存最不友好的：
- **`r.VT.RVT.ForceLod 0`** —— 强制所有 RVT 按最高精度 LOD0 烘焙，显存/内存需求最大
- **`Landscape.CouldUseQuad 0`** —— 关掉地形 quad 快路径，图元量与开销上升

而 `-RVTWaterDebug` + `r.VT.RVT.WaterDebugLog 1` 就是那 413 MB 日志的来源。

**旁证：新脚本在其它地图上是能跑通的。** 本次之后同一 workspace 连续提交成功：

```
21:24  [SVT] Commit NeoRing              ← SVT 成功
21:44  [SVT] Commit Nordland_FloatPizza  ← SVT 成功
22:03  [SVT] Commit NordLand_Pizza       ← SVT 成功
22:18  [SVT] Commit NeoRing              ← SVT 成功
22:38  [SVT] Commit Nordland_FloatPizza  ← SVT 成功
23:05  [SVT] Commit RUSH_02_Main         ← SVT 成功
```
只有**主图 NordLand**（体量最大）在 21:00 这次崩了，且 10/24 全天没有出现过
`[SVT] Commit NordLand`。所以是「新开关 + 最大地图」共同触发的显存上限，不是脚本逻辑写错。

**对照：VHM 阶段（6 分 18 秒）一切正常**

```
21:02:23  ----------BuildVHM...
21:05:16  Execution started...
21:07:56  Execution took 2:40 min      ← 正常收尾
21:08:41  p4 revert -a
21:08:42  Submitting change 1217235.   ← 正常提交
```

VHM builder 只用了 2 分 40 秒，SVT 却烧掉 84 分钟然后失败——两者复杂度完全不是一个量级。

**结论与建议**

1. **SVT 本次没构建成功**：跑了 ~84 分钟，在第三层 RVT gather 阶段显存耗尽中断，零产物、零 CL。
2. **构建状态是假成功**：UE 静默退出返回 0 + `P4_Submit.bat` 吞掉失败码，双重失效。
   建议给 `P4_Submit.bat` 补 `exit /b %errorlevel%`（在 `p4 submit` 之后立即捕获），
   并在 `BuildSVTVHM2.bat` 增加产物校验（提交前 `p4 opened` 为空即 `exit /b 1`）。
3. **`r.VT.RVT.ForceLod 0` 建议只在调试时临时启用**，NordLand 这种主图应回退默认值或分块烘焙。
4. **`-RVTWaterDebug` + `WaterDebugLog 1` 应删除**：413 MB 日志、99.4% 是无用重复，
   既撑爆日志系统也拖慢烘焙，生产流水线不该带它。
5. **注意资产未更新**：NordLand 的 SVT 仍停在 `#278 / CL 1209665 / 10-23 11:40`，
   如果 10/23 的引擎改动（水体 RVT 修复）需要重新烘焙才能生效，那么**这条线的 NordLand 修复目前没落地**。

**② `CUSTOM_SCRIPT_MULTILINE` 的 map 参数在编排快照里是空的，但实际提交带地图名。**
构建详情里该参数值仅为 `\S1Game\Scripts\Function_Engine\BuildSVTVHM3.bat;`（无参数），
按 `执行自定义操作` 的 PowerShell 拆分逻辑（`-split '\s+', 2`），`$commandArgs` 会是空，
`%1` 即 `InMapDir` 应为空。但 P4 侧提交信息明确是 `[VHM] Commit NordLand`。

结论：**构建详情里 `templateVariables` 存的是编排配置值，不等于本次运行时的实际启动参数。**
要拿真实运行参数，必须看启动参数页或构建日志。

### 7.5 这条线在跑批量烘焙

`BK-FEIYULLIU-PC61-main_BuildSVTVHM` 在 10/24 有两个密集提交段：

- 12:44 – 15:33：RUSH_00 / RUSH_01 / RUSH_02 / NordLand_Pizza / Nordland_FloatPizza
- 21:24 – 22:42：NeoRing / NordLand_Pizza / Nordland_FloatPizza（SVT+VHM 成对出现）

即这条流水线被反复调用来**批量烘全项目地图的 RVT/VHM**，每次一张图、每个阶段一个 CL。
