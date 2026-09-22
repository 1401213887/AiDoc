# UE-Android-非cook迭代-快速装机与批量CVar下发工作流

> 只改 C++ 时的分钟级迭代闭环：UBT 直出 APK（不 cook、不生成 pak）→ 覆盖包体目录 → `adb install -r` 增量装机（跳过 22 GB pak 重推）→ 清两处 PSO cache → 用 `-dpcvars=` 改启动期配置 → 批量 console 命令下发。

---

## 一、非 cook 出包 + 快速装机

### 选型

| 场景 | 走哪条 |
|---|---|
| 只改 **C++**（`.cpp` / `.h`） | **非 cook 直出 APK**（本文档） |
| 改了 content（材质 / 蓝图 / 数据表） | 完整 cook（RunUAT BuildCookRun） |
| 出正式包 / 首次包 | 完整 cook |

### 闭环五步

**① 索取「包体目录」**（目录名带版本号，每次下载都不同，不得沿用上次）

形态 = GR 平台下载的完整包：

```
<UE>-arm64.apk                              ← 壳 + so
pakchunk0-Android_ASTC.pak                  ← 完整游戏内容（22 GB 级）
pakchunk0optional-Android_ASTC.pak
main.1.<PACKAGE>.obb
S1Game_Symbols_v1\S1Gamearm64\libUnreal.so  ← 带符号，不参与装机
Install_<UE>-arm64.bat
```

**② 构建**

```bat
D:\GR_DevTest\UE5EA\zxb_apk_build.bat
```

内部 = `Build.bat S1Game Android Development -Project=<uproject> -ForceAPKGeneration -architecture=arm64`，**全程不调 Cook / AutomationTool / BuildCookRun，不生成 pak/obb**。

日志：`D:\GR_DevTest\UE5EA\zxb_apk_build.log`
产物：`D:\GR_DevTest\S1Game\Binaries\Android\S1Game-arm64.apk`

**实测（2026-09-21）**：`BUILD SUCCESSFUL in 2m 40s`，总 `324.83 seconds`。

**③ 覆盖**（2 个文件 → 包体目录）

```bash
PKG="<PKG_DIR>"; OUT="D:/GR_DevTest/S1Game/Binaries/Android"
cp -f "$OUT/S1Game-arm64.apk" "$PKG/S1Game-arm64.apk"
cp -f "$OUT/S1Game_Symbols_v1/S1Gamearm64/libUnreal.so" "$PKG/S1Game_Symbols_v1/S1Gamearm64/libUnreal.so"
```

| 文件 | 作用 | 实测尺寸 |
|---|---|---|
| `.apk` | **决定设备上实际跑哪份代码**（可运行 so 在 apk 内 `lib/arm64-v8a/libUnreal.so`） | 285,059,480 B |
| `S1Game_Symbols_v1\...\libUnreal.so` | **不参与装机**，只供 `SymbolizeCrashDump_*.bat` 符号化崩溃 ⇒ 改了 C++ 必须一起换 | 5,785,223,848 B |

**④ 装机**

### ★ 快速路径（推荐）：跳过 pak 拷贝

**只改 C++ 时设备上的 pak/obb 本来就是对的**，完整 Install 脚本却要 `uninstall` + 重推 22 GB。

```bash
P=com.sarosgame.S1Game
PKG="<PKG_DIR>"

# 1. 停旧进程
MSYS_NO_PATHCONV=1 adb shell "am force-stop $P"
# 2. 增量替换 apk（data / pak / obb 全部保留）
MSYS_NO_PATHCONV=1 adb install -r "$PKG/S1Game-arm64.apk"      # 期望 Success
# 3. 补权限（版本无关的会报错，无害）
for perm in FOREGROUND_SERVICE FOREGROUND_SERVICE_DATA_SYNC POST_NOTIFICATIONS \
            READ_EXTERNAL_STORAGE WRITE_EXTERNAL_STORAGE; do
  MSYS_NO_PATHCONV=1 adb shell "pm grant $P android.permission.$perm"
done
# 4. ★ 必须手动清两处 PSO cache
BASE=/sdcard/Android/data/$P/files
SAVED=$BASE/UnrealGame/S1Game/S1Game/Saved
MSYS_NO_PATHCONV=1 adb shell "rm -rf $BASE/RHICache"
MSYS_NO_PATHCONV=1 adb shell "rm -f $SAVED/VulkanPSO.cache.*"
# 5. 启动
MSYS_NO_PATHCONV=1 adb shell "monkey -p $P -c android.intent.category.LAUNCHER 1"
```

**为什么第 4 步不能省**：`adb install -r` **不碰外置数据**，而完整 Install 脚本是靠 `adb uninstall` **顺带**清掉 PSO cache 的。跳过 uninstall = 残留 PSO cache 原样留在设备上 ⇒ 换过 shader / layer / 配置的包启动即崩在 `qglinternal::vkCmdBindPipeline`（**只删 `RHICache` 一处仍崩，两处都删才起得来**）。

### 两条路径对比

| | 完整 Install 脚本 | ★ 快速路径 |
|---|---|---|
| 耗时 | ~20 min（推 22 GB pak） | **~2 min** |
| 设备数据 / 存档 | 被 `uninstall` 清光 | **保留** |
| pak / obb | 全量重推 | **不动** |
| PSO cache | 被 uninstall 顺带清掉 | **要手动清** |
| 签名要求 | 无 | **必须一致**（debug ↔ debug） |
| 适用 | 换内容版本 / 首装 / 签名变了 | **只改 C++、pak 与 obb 未变** |

**不适用快速路径**：pak 或 obb 变了（换 cook 内容 / 包体版本升级）；签名不一致（`install -r` 报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`）。

### ⑤ 验证（每轮必做）

```bash
# ① 本地构建产物字节数
ls -la "D:/GR_DevTest/S1Game/Binaries/Android/S1Game-arm64.apk"
# ② 设备 base.apk 路径 + 字节数（①② 必须完全相等）
BP=$(MSYS_NO_PATHCONV=1 adb shell "pm path $P" | sed 's/^package://' | tr -d '\r')
MSYS_NO_PATHCONV=1 adb shell "ls -la $BP"
# ③ 装机时间 = 本次
MSYS_NO_PATHCONV=1 adb shell "dumpsys package $P | grep lastUpdateTime"
# ④ 进程存活
MSYS_NO_PATHCONV=1 adb shell "pidof $P"
```

**apk 字节数比对是最硬的一条**——它直接回答"设备上跑的到底是不是我刚编的代码"。脚本打印的 `All done` 只证明脚本走完。

**实测基线（2026-09-21）**：设备 `base.apk` = **285,059,480 B** 与本地产物逐字节一致；`lastUpdateTime = 17:41:41`（本次）；进程存活；日志 Vulkan RHI 正常。

---

## 二、UECommandLine.txt 编辑闭环

### 路径（UE Android 恒定）

```
/sdcard/Android/data/<PACKAGE>/files/UnrealGame/<UE>/UECommandLine.txt
```

注意是 `files/UnrealGame/<UE>/` 层（与日志同层），**不是** `Saved` 下层。

### ★ 核心坑：不要新增第二个 `-dpcvars=`

**实测设备上的 cmdline（2026-09-21）**：

```
-project="../../../S1Game/S1Game.uproject" -forcevulkanddrawmarkers -dpcvars=r.Vulkan.UseChunkedPSOCache=0,r.Mobile.AllowSoftwareOcclusion=0,r.HZBOcclusion=1,r.AllowOcclusionQueries=1 -ExecCmds="..." -trace=gpu,cpu,loadtime,frame,log,bookmark,task,RHICommands -statnamedevents
```

**已有一个 `-dpcvars=`**（4 个 CVar 逗号分隔）。若直接末尾追加 `-dpcvars=r.Android.SupportsTimestampQueries=1`，会出现**两个同名参数**，引擎只认其一 ⇒ **其中一个静默失效**。

**正确做法**：合并进已有的逗号列表。

```
-dpcvars=r.Vulkan.UseChunkedPSOCache=0,r.Mobile.AllowSoftwareOcclusion=0,r.HZBOcclusion=1,r.AllowOcclusionQueries=1,r.Android.SupportsTimestampQueries=1
```

### 操作流程（get → 本地精编 → put）

```bash
SK="C:/Users/djangozhang/.tclaude/skills/ZXBMobileDebug/scripts"

# 1. 取回（字节精确，无 shell CRLF 伪影）
python -X utf8 "$SK/uecmd.py" com.sarosgame.S1Game S1Game get "D:/GR_DevTest/UE5EA/uecmd_s1game.txt"
# 2. 用任意编辑器改本地文件（把新 CVar 插进已有 -dpcvars= 的逗号列表）
# 3. 覆盖回设备（自动备份 + 回读字节校验）
python -X utf8 "$SK/uecmd.py" com.sarosgame.S1Game S1Game put "D:/GR_DevTest/UE5EA/uecmd_s1game.txt"
#     → ok: wrote 392 bytes, backup at ...UECommandLine.txt.bak_20260921_174953
```

设备端每次写回都留 `.bak_<YYYYmmdd_HHMMSS>`，可直接 `cp` 还原。

### 生效判据（三层证据）

改完必须**重启 app**。重启后：

**① 命令行层**——日志 `LogInit: Command Line:` 含新参数

**② 应用层**——`-dpcvars` 的专属证据：

```
LogDeviceProfileManager: Setting Command Line Device Profile CVar: [[r.Android.SupportsTimestampQueries:1]]
```

**③ 运行时值**——包体自己回答：

```bash
python -X utf8 "$SK/rcvar.py" com.sarosgame.S1Game S1Game get r.Android.SupportsTimestampQueries
# → r.Android.SupportsTimestampQueries = true   LastSetBy: DeviceProfile
```

`LastSetBy: DeviceProfile` 是引擎自带归因，**证明走的就是 DP 通道**。

### 通道分流速查

| CVar 类型 | 通道 |
|---|---|
| `ECVF_ReadOnly` / `SetByDeviceProfile` | `-dpcvars=` / DeviceProfile / ini（启动期） |
| `ECVF_Cheat` | `-ExecCmds=` / `consolevariables.ini` |
| 运行时可写 | console 命令（`am broadcast`） |

详见 `E:\AiDoc\UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md`。

---

## 三、批量 CVar / console 命令下发

### 脚本

**`D:\GR_DevTest\UE5EA\zxb_cvr.bat`**

```bat
zxb_cvr.bat                      :: 双击运行，默认包名 com.sarosgame.S1Game
zxb_cvr.bat com.xxx.YYY          :: 覆盖目标包名
```

命令清单写在 bat 里（注释标了位置），一行一条，直接改。

### 底层链路（与编辑器 Device Output Log 同一条）

```
adb shell "am broadcast -a android.intent.action.RUN -e cmd '<CMD>'"
  → ConsoleCmdReceiver.onReceive
  → GameActivity.nativeConsoleCommand(cmd)
  → AsyncTask(GameThread) → GEngine->DeferredCommands
  → UEngine::TickDeferredCommands 执行
```

### 两个设计要点

**① 前置检查不可省**：`am broadcast` **永远报成功**（app 没跑、命令不存在、CVar 只读，一律 `result=0` `exit 0`）。所以 bat 先查两道：

```bat
adb shell echo ok >nul 2>&1
if errorlevel 1 ( echo [ERROR] no adb device online & exit /b 1 )
adb shell pidof %PACKAGE% >nul 2>&1
if errorlevel 1 ( echo [ERROR] %PACKAGE% is not running & exit /b 1 )
```

实测价值：一次测试中被拦下（app 恰在重启窗口，pid 变了），否则会发出一批注定被丢弃的命令而毫无提示。

**② 每条间隔 ~1s，用 `ping` 不用 `timeout`**：

```bat
ping -n 2 127.0.0.1 >nul
```

命令是排队进游戏线程执行的（`DeferredCommands`），连发会灌爆。用 `ping` 是为了绕开 MSYS 的 `timeout.exe` 抢占 Windows `timeout` 的问题（见下节）。

### 验证方法

| 命令类型 | 证据源 |
|---|---|
| CVar 设值（`r.*` / `grass.*` / `wp.*`） | 设备日志回显 `name = "value"` |
| `stat` / `show` / `vis` | **不写任何日志**，只能截图看屏幕 |
| 无参 toggle 命令（如 `ToggleForceDefaultMaterial`） | 出现在 `LogEngine: Warning: \t<cmd>` 里即**已执行**（见下） |

**实测（2026-09-21，11 条）**：6 条 CVar 全部日志确认；`stat fps` / `stat unit` / `r.meshdrawcommands.stats 1` 截图确认上屏。

### 关于 `LogEngine: Warning: \t<command>`

这条**不是"命令未找到"的警告**。源码依据：

- 命令未找到时 `FConsoleManager::ProcessUserConsoleInput` 直接 `return false`，**不打任何日志**（`Core/Private/HAL/ConsoleManager.cpp:2759-2762`）
- `LogEngine, Warning, TEXT("\t%s")` 出自 `UEngine::TickDeferredCommands`（`Engine/Private/UnrealEngine.cpp:2624-2632`）：当**整批命令执行耗时超过目标帧时间**时，把刚执行过的命令逐条列出（paper trail）

⇒ **看到它 = 命令进了队列且被执行了**；没看到只说明那批执行得快，**不能判定失败**。

---

## 四、Git Bash ↔ cmd 环境的三个坑

### 坑 1：`cmd //c start "标题" cmd /k "路径"` 引号被剥掉

**现象**：报 `The system cannot find the file <标题>`——`start` 把标题当成了程序名。换 `start "" cmd /c "..."` 形式则报 `Access is denied`（禁用沙箱也无效）。

**原因**：MSYS 在把参数传给 `cmd.exe` 时剥掉了内层引号。

**可用写法**——把启动命令写进一个 **CRLF 的 launcher bat**，再执行它（bat 内部的引号是 Windows 原生的，不经 MSYS）：

```bash
printf '@echo off\r\nstart "S1Game Build no-cook" cmd /k "D:\\GR_DevTest\\UE5EA\\zxb_apk_build.bat"\r\n' > launcher.bat
cmd //c "D:\\GR_DevTest\\UE5EA\\launcher.bat"
```

**判据别用 exit code**：`start` 是异步且总返回 0。要看真实副作用——UBT 的 `Engine/Programs/UnrealBuildTool/Log.txt` mtime、或目标产物是否更新。

### 坑 2：`-Append` 日志的 `DONE_EXIT_0` 假阳性

`zxb_apk_build.bat` 的日志是 append 模式，**上一次构建的 `DONE_EXIT_0` 还在文件里**。轮询时若只 `tail | grep DONE_EXIT_`，会在启动的**第一秒就误判为"构建完成"**。

**正确判据（三重）**：

1. 只看**本次新增行**（先记基线行数，轮询 `tail -n +<BASE+1>`）
2. `Engine/Programs/UnrealBuildTool/Log.txt` 的 mtime 是否更新
3. `dotnet.exe`（UBT）进程是否存在

### 坑 3：bat 必须 CRLF

用 Write / heredoc 写出的是 LF。转换：

```bash
sed -i 's/$/\r/' file.bat
# 验证：每行应以 ^M$ 结尾
cat -A file.bat | head
```

`for` / `goto` 在 LF 下可能解析异常。

> 附带一条排查经验：`adb shell pidof <pkg>` 的退出码在 bash 与 cmd 下**都可靠**（有进程返 0、无进程返 1）。若 bat 报告"app 没在跑"，先用 `adb shell pidof` 直接确认，**别急着怀疑脚本**——本次就撞上过一次真阳性（app 恰在重启窗口）。

---

## 五、Checklist

**装机前**

- [ ] 只改了 C++？（改了 content 就得走完整 cook）
- [ ] 包体目录里的 pak/obb 与设备上的是同一版本？（`ls -la` 比对字节数）
- [ ] 编辑器已关闭？（编译 Windows 目标时）

**装机后**

- [ ] 设备 `base.apk` 字节数 == 本地构建产物？（**最硬的一条**）
- [ ] `lastUpdateTime` = 本次？
- [ ] 两处 PSO cache 都清了？（`RHICache` + `VulkanPSO.cache.*`）
- [ ] 进程存活？日志 RHI 正常（无 crash / assert）？

**改 cmdline 后**

- [ ] 没有产生第二个 `-dpcvars=`？（合并进已有列表）
- [ ] 重启了 app？
- [ ] `Setting CommandLine Device Profile CVar` 日志里能看到？
- [ ] `rcvar.py get` 的 `LastSetBy` 是预期的通道？

**批量下发后**

- [ ] 前置检查过了（设备在线 + app 在跑）？
- [ ] CVar 类在日志里回显了？
- [ ] `stat` 类截图看了？（它们不写日志）

---

## 六、相关参考

### 工具与脚本

| 路径 | 用途 |
|---|---|
| `D:\GR_DevTest\UE5EA\zxb_apk_build.bat` | 非 cook 直出 APK |
| `D:\GR_DevTest\UE5EA\zxb_cvr.bat` | 批量 CVar / console 命令下发 |
| `C:\Users\djangozhang\.tclaude\skills\ZXBMobileDebug\scripts\uecmd.py` | UECommandLine.txt 取回 / 编辑 / 覆盖回 |
| `C:\Users\djangozhang\.tclaude\skills\ZXBMobileDebug\scripts\rcvar.py` | CVar 远端下发（含离线 DB 预判 + ReadOnly 探针） |
| `C:\Users\djangozhang\.tclaude\skills\ZXBMobileDebug\scripts\send_cvar.py` | 通用 console 命令下发（带日志回显校验） |
| `C:\Users\djangozhang\.tclaude\skills\ZXBMobileDebug\scripts\trace_pull.py` | 拉取 `.utrace` |

### 引擎源码

| 位置 | 内容 |
|---|---|
| `Core/Private/HAL/ConsoleManager.cpp:2759-2762` | 命令未找到时 `return false`（不打日志） |
| `Core/Private/HAL/ConsoleManager.cpp:2688` | `ProcessUserConsoleInput` 实现 |
| `Engine/Private/UnrealEngine.cpp:2606-2632` | `TickDeferredCommands`（含 `\t%s` paper trail） |
| `Launch/Private/Android/LaunchAndroid.cpp:1961-1968` | `nativeConsoleCommand` → GameThread |

### 本项目文档

- `E:\AiDoc\UE-Mobile-MeshDrawCommandStats-面数统计口径-与MaxDC截断同口径修复.md` — 同批次的面数统计口径修复
- `E:\AiDoc\UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md` — 通道分流
- `E:\AiDoc\UE-Android-无日志-ABSLOG-CWD相对路径.md` — 无日志根因
- `E:\AiDoc\UE-Android-Vulkan启动崩溃-ChunkedPSOCache-validationlayer.md` — PSO cache 相关崩溃
