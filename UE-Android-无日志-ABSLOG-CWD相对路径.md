# UE Android 打包无日志：相对 -project + 进程 CWD=/ 导致日志路径解析失败

> Android 包启动正常（不崩、能进游戏），但 `.../Android/data/<pkg>/files/UnrealGame/.../Saved/Logs` 目录下从不生成日志。根因：APK 内 UECommandLine.txt 的 `-project` 是**相对路径**，而 Android 进程 CWD=`/`，日志被解析到 `/BulletTest/`（不可写）→ 首次写文件失败 → **静默丢弃**。修复：`-ABSLOG` 指定绝对可写路径。

---

## 一、问题定位流程（确认了什么）

1. **确认 UE 读的是 APK 内命令行**：logcat `Using APK commandline` + `Final commandline` 只有 `-project="../../../BulletTest/BulletTest.uproject" -iterative`。→ 推 sdcard 的 `UECommandLine.txt` 一律无效，必须改打包侧。
2. **确认进程 CWD=`/`**：`run-as <pkg> ls -la /proc/<pid>/cwd` → `-> /`（引擎在 Android 上无 chdir 到项目目录）。
3. **确认路径解析失败**：相对 `-project` → `ProjectDir=../../../BulletTest/`（未转绝对）→ CWD=/ → 日志落到 `/BulletTest/Saved/Logs/`。该路径不存在且 app 无权限创建。
4. **确认是静默丢弃而非权限错误**：`FOutputDeviceFile` 首次写日志才创建文件（`bCreateWriterLazily=true`），`CreateFileWriter` 失败不报错。进程 fd 实测只开过 RenderDoc 日志，从无 `BulletTest.log`。
5. **排除「目录缺失」**：用户看的 `files/UnrealGame/.../Saved/Logs` 目录确实存在——那是引擎在 mount OBB 阶段用 `GExternalFilePath` mkdir 的（`AndroidPlatformFile.cpp:1398-1405`）。**目录存在 ≠ UE 日志写这里**，这是最大的迷惑点。

## 二、根因分析

日志路径解析链：`-project`（相对）→ `ProjectDir`（相对）→ `CWD=/` → `/BulletTest/`（不可写）→ 写失败静默丢弃。

| 环节 | 证据 |
|---|---|
| `-project` 相对路径 | APK 内 UECommandLine.txt（UAT 生成）实录 `-project="../../../BulletTest/BulletTest.uproject"` |
| CWD = `/` | `run-as ls -la /proc/<pid>/cwd` → `-> /`，引擎无 chdir |
| 目标路径不可写 | `/BulletTest/Saved/Logs/` 实测不存在，app 无 root 权限创建 |
| 失败静默 | `FOutputDeviceFile` 懒创建文件，`CreateFileWriter` 失败不抛错；fd 无 BulletTest.log |

## 三、修复方案

用 `-ABSLOG` 强制绝对日志路径，且必须走**打包侧 `-cmdline`**（UAT 原样写进 APK 内 UECommandLine.txt）：

```bat
RunUAT.bat BuildCookRun ... -cmdline=" -forcevulkanddrawmarkers -ABSLOG=/sdcard/Android/data/com.YourCompany.BulletTest/files/UnrealGame/BulletTest/BulletTest/Saved/Logs/BulletTest.log"
```

原理（`GenericPlatformOutputDevices.cpp:78-120` 的 `GetAbsoluteLogFilename()`）：

- `-ABSLOG` 出现时，**先清空 `CachedAbsoluteFilename`，再直接拼接 ABSLOG 的值**作为最终绝对日志路径。
- 与默认流程（`ProjectSavedDir()` + `/Saved/Logs/`）无关，天然避开相对路径解析。
- 路径选在 app 私有目录（`/sdcard/Android/data/<pkg>/files/...`），目录已存在、app 可写。

**已验证**（用户实测）：重打包后启动，`.../files/UnrealGame/BulletTest/BulletTest/Saved/Logs/BulletTest.log` 正常生成。

## 四、快速排查 Checklist

1. 有没有日志？`adb shell "run-as <pkg> ls -la /proc/<pid>/cwd"` 确认 CWD。
2. 实际生效命令行是什么？logcat 搜 `Final commandline` 和 `Using APK commandline`——以它为准，**不是** `Binaries/Android/UECommandLine.txt`（手工文件不参与打包）。
3. 目录存在≠写这里：`files/UnrealGame/.../Saved/Logs` 是引擎 mkdir 的展示目录，UE 实际写日志的路径由 `-project`/`-ABSLOG` 决定。
4. 改参数只认 `-cmdline`：`-cmdline=` 是 UAT 写进 APK 内命令行的唯一通道（`ProjectParams.cs:946`）。
5. 验证：启动后看目标路径是否出现 `BulletTest.log`；日志头部应有 `Log file open`。

## 五、相关参考

- 引擎源码：
  - `Engine/Source/Runtime/Core/Private/Misc/GenericPlatformOutputDevices.cpp:78-120`（`GetAbsoluteLogFilename()` 的 `-ABSLOG` 逻辑）
  - `Engine/Source/Runtime/Core/Private/Misc/GenericPlatformOutputDevices.cpp:134-188`（`GetLog()` 创建 `FOutputDeviceFile`）
  - `Engine/Source/Runtime/Engine/Private/Android/AndroidPlatformFile.cpp:1398-1405`（log 目录 mkdir 用 `GExternalFilePath`，≠ 实际写日志路径）
  - `Engine/Source/Programs/UnrealBuildTool/Project/ProjectParams.cs:946`（`-cmdline=` → staged 命令行）
  - `Engine/Source/Programs/AutomationTool/.../CopyBuildToStagingDirectory.Automation.cs:5463`（`WriteStageCommandline`）
- 相关文档：`E:\AiDoc\UE-Android-Vulkan启动崩溃-ChunkedPSOCache-validationlayer.md`（同一次打包排障的姊妹问题）
