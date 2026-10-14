# UE-Android-RHIThread慢49%-VulkanValidationLayer自动加载去除指南

> Android UE 包（Development）启动即带着 Vulkan validation layer，独占 **RHIThread 49.45% 的 CPU**，
> 表现为 `RHI_Translate` 普遍偏慢、每个子回调都慢。根因是 **UBT 打包时复制该 layer so 进 apk，
> 被 debuggable app 的系统 Vulkan loader 自动加载**——与 UE 的 `r.Vulkan.EnableValidation`（实测为 0）无关。

环境：MI 8 / SDM845 / Adreno 630 / Android 10 / UE 5.5.4 定制引擎（++GR+DevTest）/ 非 root

---

## 一、问题定位流程（确认了什么）

### 1. perfetto 只能看到"症状"，看不到 so 级归因

`RHI_Translate` 每帧 53.5 ms，而实测帧间隔 65.34 ms → **占帧时间 81.9%**，是明确的瓶颈线程。
但 perfetto 的数据源是 ftrace/atrace（调度/频率/slice），**不采 PC、不记录 so 加载**，
所以它**无法回答"时间花在哪个 .so 上"**——这一步必须换 simpleperf。

### 2. simpleperf 定位到具体 .so（决定性证据）

```bash
adb shell "simpleperf record --app <包名> -g -f 1000 -o /data/local/tmp/p.data --duration 5"
adb shell "simpleperf report -i /data/local/tmp/p.data --comms RHIThread --sort dso -n"
```

```
Overhead  Sample  Shared Object
49.45%    2366    libVkLayer_khronos_validation.so   ← 一半 CPU 在这里
15.86%     746    libc.so
15.14%     729    libUnreal.so
12.04%     595    [kernel.kallsyms]
 5.81%     278    vulkan.sdm845.so                  ← 真驱动只占 5.8%
```

调用图（`-g`）显示它的 `Children` 占比 **85.10%**，即 85% 的 RHIThread 采样栈都穿过它 →
**它插在引擎和驱动之间，拦截每一次 Vulkan 调用**。

### 3. 确认"不是 UE 加载的"

| 检查项 | 结果 |
|---|---|
| `r.Vulkan.EnableValidation`（运行时查询） | **0**，`LastSetBy: Constructor` |
| UE 日志 | `Found 2 available instance layers`——只是 **available**，不是 enabled |
| 进程 maps | **6 行** VkLayer 映射（validation 3 + RenderDoc 3），`r-xp` 可执行段已映射 |
| 安装后 lib 目录 | `libVkLayer_khronos_validation.so` 确实在其中（46 个 so 之一） |

→ UE 从未请求它，却实实在在加载并执行了。

---

## 二、根因分析

### 因果链

```
BuildS1Android.bat: -clientconfig=Development
  → UEDeployAndroid.cs: bCopyVulkanLayers = (bSupportsVulkan||SM5) && (Configuration != "Shipping")
  → true → 复制 libVkLayer_khronos_validation.so 进 apk 的 lib/arm64/
  → app 是 debuggable
  → Android Vulkan loader 扫描 app native lib 目录，自动加载该 layer
  → 每次 Vulkan 调用穿验证层
  → RHIThread 49.45% CPU 烧在验证层
```

### 关键源码

`Engine/Source/Programs/UnrealBuildTool/Platform/Android/UEDeployAndroid.cs`

```csharp
private const string ANDROID_VULKAN_VALIDATION_LAYER = "libVkLayer_khronos_validation.so";   // :32

private void CopyVulkanValidationLayers(string UnrealBuildPath, UnrealArch UnrealArch,
                                        string NDKArch, string Configuration)
{
    Ini.GetBool("/Script/AndroidRuntimeSettings.AndroidRuntimeSettings", "bSupportsVulkan", out bSupportsVulkan);
    Ini.GetBool("/Script/AndroidRuntimeSettings.AndroidRuntimeSettings", "bSupportsVulkanSM5", out bSupportsVulkanSM5);

    // ★ Shipping 是唯一开关
    bool bCopyVulkanLayers = (bSupportsVulkan || bSupportsVulkanSM5) && (Configuration != "Shipping");
    if (bCopyVulkanLayers)
    {
        string VulkanLayersDir = Path.Combine(Unreal.EngineDirectory.ToString(),
                                              "Binaries", "ThirdParty", "Vulkan", "Android", NDKArch);
        // → File.Copy 到 <UnrealBuildPath>/libs/<NDKArch>/
    }
}
```

### 为什么 `r.Vulkan.EnableValidation = 0` 却仍然加载

UE 侧加载 layer 的逻辑在 `VulkanRHI/Private/VulkanLayers.cpp`：

```cpp
GRHIGlobals.IsDebugLayerEnabled = (GValidationCvar.GetValueOnAnyThread() > 0);   // :334
...
const bool bUseVulkanValidation = GRHIGlobals.IsDebugLayerEnabled;                // :388
if (!bGfxReconstructOrVkTrace && bUseVulkanValidation)
{
    if (!AddRequestedLayer(KHRONOS_STANDARD_VALIDATION_LAYER_NAME, ...))          // :391
```

**这是"UE 主动请求"的路径——CVar=0 时它不会走。**
但只要 so 文件躺在 apk 的 `lib/arm64/` 里，**Android 系统 loader 会自行扫描 app native lib 目录并加载它**，
完全绕开 UE 的开关。两套机制互不相干，这正是"查 CVar 永远是 0、layer 照样跑"的原因。

### 为什么开销这么大

validation layer 会拦截**每一次** Vulkan API 调用做参数校验、对象生命周期追踪、状态一致性检查。
所以症状不是"某个函数慢"，而是**等比例地全线变慢**——这正是"每个子回调都慢"的成因。

---

## 三、修复方案

### 3.1 引擎侧：加一个项目级 opt-out 开关

`UEDeployAndroid.cs` 的 `CopyVulkanValidationLayers()` 内，在原条件之后插入：

```csharp
bool bCopyVulkanLayers = (bSupportsVulkan || bSupportsVulkanSM5) && (Configuration != "Shipping");

#region Engine ZXB
// [ZXB] Let a project drop the packaged Vulkan validation layer while staying on a
// non-Shipping configuration (Shipping would also strip engine trace and console).
// Opt out from any engine ini:
//   [/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]
//   bPackageVulkanValidationLayer=False
bool bPackageVulkanValidationLayer = true;
Ini.GetBool("/Script/AndroidRuntimeSettings.AndroidRuntimeSettings", "bPackageVulkanValidationLayer", out bPackageVulkanValidationLayer);
if (!bPackageVulkanValidationLayer)
{
    Logger.LogInformation("[ZXB] Skipping {Layer} packaging (bPackageVulkanValidationLayer=False)", ANDROID_VULKAN_VALIDATION_LAYER);
    bCopyVulkanLayers = false;
}
#endregion

if (bCopyVulkanLayers)
{
    ...
```

**默认值 `true` = 保持原有行为**，不启用该键的项目完全不受影响。

### 3.2 项目侧：关闭开关

`S1Game/Config/DefaultEngine.ini` 的 `[/Script/AndroidRuntimeSettings.AndroidRuntimeSettings]` 节内（与 `bSupportsVulkan` 同节）：

```ini
bSupportsVulkan=True
bSupportsVulkanSM5=False
bPackageVulkanValidationLayer=False
```

> 注释建议用纯 ASCII（该文件既有注释均为英文），避免非 ASCII 内容影响 ini 解析。

### 3.3 构建：UBT 必须手动重编（关键坑）

改了 `UEDeployAndroid.cs` 后，**`Build.bat` 不会自动重编 UBT**——会静默复用旧 DLL，
构建"成功"但改动完全不生效（表现为日志里仍出现 `Copying libVkLayer_khronos_validation.so`，且无自定义日志）。

正确做法：

```bash
# 用专用脚本重建 UBT（Build.bat UnrealBuildTool 会报 "Couldn't find target rules file"）
cmd //c "D:\GR_DevTest\UE5EA\Engine\Build\BatchFiles\BuildUBT.bat"
```

若报 `A conflicting instance of UnrealBuildTool is already running`，先结束占用进程：

```bash
powershell -NoProfile -Command "Get-Process UbaVisualizer -ErrorAction SilentlyContinue | Stop-Process -Force"
```

**验证改动是否进了 DLL**（注意 C# 字符串是 UTF-16，普通 `grep`/`strings` 会漏）：

```python
data = open(r"...\Engine\Binaries\DotNET\UnrealBuildTool\UnrealBuildTool.dll","rb").read()
print("bPackageVulkanValidationLayer".encode("utf-16-le") in data)   # → True
```

### 3.4 验证方法（三层，缺一不可）

```bash
# ① apk 内是否还有该 so
python -c "
import zipfile
z = zipfile.ZipFile(r'...\Binaries\Android\S1Game-arm64.apk')
print([n for n in z.namelist() if 'VkLayer' in n])"

# ② 设备 base.apk 字节数必须与本地构建产物完全一致
adb shell "pm path <包名>"              # 取路径
adb shell "ls -la <上面的路径>"

# ③ 运行时 maps（最硬）
PID=$(adb shell pidof <包名> | tr -d '\r' | awk '{print $1}')
adb shell "run-as <包名> cat /proc/$PID/maps | grep -c VkLayer"     # 期望 3（只剩 RenderDoc）
adb shell "run-as <包名> cat /proc/$PID/maps | wc -l"               # 必须 >0，否则是假阴性
```

**⚠️ 假阴性陷阱**：`adb shell cat /proc/<pid>/maps` 对非 root 会返回 `Permission denied`，
但 `grep -c` 同样返回 `0`——看起来像"验证通过"，实际根本没读到。
**必须用 `run-as`（要求 app debuggable）并同时打印 maps 总行数**来区分。

### 3.5 实测效果

| 项 | 修改前 | 修改后 |
|---|---|---|
| apk 大小 | 292,047,456 B | **285,071,200 B**（少 6.98 MB ≈ layer 压缩后大小） |
| apk 内 VkLayer | 2 个（khronos + RenderDoc） | **1 个**（只剩 RenderDoc） |
| 运行时 maps VkLayer | **6 行** | **3 行** |
| RHIThread 里 validation 占比 | **49.45%** | **0** |
| RHIThread 采样数（5s） | 4797 | **1512** |
| 全 app 采样数（5s） | 18888 | **12607** |

> `libVkLayer_GLES_RenderDoc.so` 走的是另一条打包路径，不受本开关控制；
> 从采样看它占用 < 0.5%，不是热点。

### 3.6 为什么不用"打 Shipping 包"来去掉 layer

Shipping 配置同样能让 `Configuration != "Shipping"` 为假、不复制 layer，但代价是**连调试能力一起丢掉**：

| | Shipping 方案 | **本方案（Development + 开关）** |
|---|---|---|
| validation layer | 去除 ✓ | **去除 ✓** |
| 引擎 atrace 打点 | **丢失**——perfetto 里 `RHI_Translate`/`Queue Present` 一条都没有 | **保留 ✓** |
| console 命令 | **失效**（receiver 只在非 Shipping 注册，`send_cvar` 发不进） | **保留 ✓** |

---

## 四、换配置包必清 PSO cache（否则启动即崩）

**在 Development ↔ Shipping 之间切换包体时，两处 PSO cache 都必须删除**，否则启动崩在
**高通驱动的 `vkCmdBindPipeline`**（tombstone 栈顶为 `qglinternal::vkCmdBindPipeline`）：

```bash
ADB=/c/Users/djangozhang/AppData/Local/Android/Sdk/platform-tools/adb
BASE=/sdcard/Android/data/<包名>/files
SAVED=$BASE/UnrealGame/<项目名>/<项目名>/Saved

adb shell "rm -rf $BASE/RHICache"
adb shell "rm -f $SAVED/VulkanPSO.cache.*"
```

- **只删一处仍会崩**——实测只删 `RHICache` 后重启，app 再次崩溃；两处都删才正常启动。
- 崩溃特征：`SIGSEGV` + `code -6 (SI_TKILL)`，栈顶在 `vulkan.sdm845.so` 的 `vkCmdBindPipeline`，
  下面几帧才是 `libUnreal.so`。**符号化时必须看完整 tombstone 的 `#00` 帧**，
  只看 logcat 尾部容易漏掉栈顶而误判到 UE 侧函数上。

---

## 五、快速排查 Checklist

1. **帧率/线程慢，但 perfetto 看不到 so 级归因？** → 换 simpleperf：
   `simpleperf record --app <包名> -g` + `report --comms RHIThread --sort dso`
2. **看到 `libVkLayer_khronos_validation.so` 占大头？** → 检查 apk 内是否含该 so：
   `python -c "import zipfile; print([n for n in zipfile.ZipFile('x.apk').namelist() if 'VkLayer' in n])"`
3. **确认不是 UE 请求的**：查 `r.Vulkan.EnableValidation`（应为 0 / Constructor）；
   日志里 "Found N available instance layers" 只是 available，不代表 enabled
4. **确认加载方式**：`run-as <包名> cat /proc/<pid>/maps | grep -c VkLayer`
   （**必须同时打印 maps 总行数排除假阴性**）
5. **要保留 Development 调试能力又要去 layer** → 用本文的 `bPackageVulkanValidationLayer=False` 开关
6. **改了 `UEDeployAndroid.cs` 后构建无效果** → 用 `BuildUBT.bat` 手动重编 UBT，
   并用 UTF-16 搜索确认字符串进了 DLL
7. **换配置包后启动崩溃** → 清 `RHICache/` **和** `Saved/VulkanPSO.cache.*` 两处
8. **排查 CVar 不生效时**：本引擎 `-dpcvars=` 是死代码（解析函数在 `#if !UE_BUILD_SHIPPING` 内且全引擎零调用点），
   任何 cvar 都改不了；有效通道为 ini `[ConsoleVariables]` / `-ExecCmds`（仅非 ReadOnly）/ 专用命令行开关

---

## 六、相关源码与参考

### 引擎源码

| 位置 | 内容 |
|---|---|
| `Engine/Source/Programs/UnrealBuildTool/Platform/Android/UEDeployAndroid.cs:32` | `ANDROID_VULKAN_VALIDATION_LAYER` 常量 |
| `UEDeployAndroid.cs:1445` | `CopyVulkanValidationLayers()`——**打包侧复制逻辑（本方案改动点）** |
| `UEDeployAndroid.cs:5367` | `GradleBuildType = bForDistribution ? ":app:assembleRelease" : ":app:assembleDebug"` |
| `Engine/Source/Runtime/VulkanRHI/Private/VulkanLayers.cpp:334/388/391` | UE 侧请求 layer 的条件（与本问题无关，但常被误认为根因） |
| `Engine/Source/Runtime/VulkanRHI/Public/VulkanConfiguration.h:38` | `VULKAN_VALIDATION_DEFAULT_VALUE = (UE_BUILD_DEBUG ? 2 : 0)` |
| `Engine/Build/BatchFiles/BuildUBT.bat` | **手动重编 UBT 的唯一正确入口** |

### 相关文档（E:\AiDoc）

- `UE-Android-Vulkan启动崩溃-ChunkedPSOCache-validationlayer.md` —— 同设备同引擎的启动崩溃；
  其"chunked PSO cache + validation layer 缺一不崩"的矩阵需补充：**残留的跨配置 chunked cache 单独也足以触发崩溃**
- `UE-CVar配置-命令行通道分流-dpcvars-vs-ExecCmds.md` —— CVar 各配置通道的有效性
- `UE-Android-无日志-ABSLOG-CWD相对路径.md` —— 同系列打包排障

### 工具

- Perfetto：`C:\Users\djangozhang\tools\perfetto`（v58.2，UI 在 `http://127.0.0.1:18080/`）
- simpleperf：Android 系统自带 `/system/bin/simpleperf`（非 root 采 debuggable app 需 `--app`）
- 符号化：NDK `llvm-symbolizer.exe`
  （`<SDK>/ndk/<ver>/toolchains/llvm/prebuilt/windows-x86_64/bin/`）
  —— 注意 tombstone 的 `pc` **本身就是 so 内 vaddr，不要再减 `offset`**，否则得到垃圾符号
