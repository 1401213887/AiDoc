# UE5EA-编辑器弹窗无日志-MessageBoxExt统一拦截AI追踪

> 编辑器下 UE 弹窗（MessageBox）只有 UI 不写日志，AI 无法自动追踪报错文本；通过改动 `FWindowsPlatformMisc::MessageBoxExt` 单一汇聚点，实现所有弹窗文本无条件落日志 + unattended/CVar 短路弹窗，使 AI 扫日志即可完整识别编辑器报错。

---

## 一、问题定位流程

1. **入口确认**：`Shader.cpp:190-193`（`FShaderParameterMap::VerifyBindingsAreComplete`）发现 unbound 参数时拼接 `ErrorMessage` 后仅调用 `FPlatformMisc::MessageBoxExt(...)` 弹窗，**没有任何 UE_LOG**。
2. **平台实现对比**：
   - `FGenericPlatformMisc::MessageBoxExt`（`GenericPlatformMisc.cpp:1074-1100`）：入口无条件 `UE_LOG(LogGenericPlatformMisc, Warning, "MessageBox: ...")` + `LowLevelOutputDebugStringf` → **写日志**。
   - `FWindowsPlatformMisc::MessageBoxExt`（`WindowsPlatformMisc.cpp:2096-2131`）：直接 `FWindowsDialog::Show` → `CreateDialogParam` 弹 Win32 窗口 + `GetMessageW` 阻塞 → **不写日志**（仅对话框创建失败时记 `Failed to create dialog`，且不含错误文本）。
3. **调用面统计**：`MessageBoxExt` 46 处、`FMessageDialog::` 38+ 处、直连 `::MessageBox(` 40 处（RHI 层），全部收敛到 `FWindowsPlatformMisc::MessageBoxExt` 这一个最终汇聚点。
4. **`-unattended` 能力验证**：编辑器下直接 `-unattended` **不彻底**——
   - 只覆盖显式检查 `FApp::IsUnattended()` 的调用点（`FMessageDialog::Open`/`Debugf`/`ShowLastError`、D3D12/Vulkan RHI 弹窗）；
   - `FWindowsPlatformMisc::MessageBoxExt` 内部**无 unattended 分支**，直连调用（如 `Shader.cpp:193`）照样弹窗阻塞；
   - unattended 下 `FMessageDialog::Open` 静默返回默认值，**错误文本不落日志**，AI 无信息源。
5. **编辑器路径兜底确认**：全引擎搜索 `ModalMessageDialog` 绑定点，仅 `ZenDashboard`（独立程序）绑定，**编辑器下 `FCoreDelegates::ModalMessageDialog` 从未绑定** → `FMessageDialog::Open`（`MessageDialog.cpp:174`）的 `IsBound()` 条件恒 false → 编辑器所有 `FMessageDialog::Open` **实际都落到 `FPlatformMisc::MessageBoxExt`**；Slate 自身错误弹窗（如 `SlateApplication.cpp:1020` 显卡问题）也直连 `MessageBoxExt`。单点改动即可覆盖编辑器报错类弹窗全部路径。

---

## 二、根因分析

| 环节 | 根因 |
|---|---|
| 弹窗不落日志 | `FWindowsPlatformMisc::MessageBoxExt` 只弹窗不写日志（Windows 实现未继承 Generic 版的日志逻辑），调用方（如 `Shader.cpp`）也都不带 `UE_LOG` |
| `-unattended` 拦不住 | `MessageBoxExt` 内部无 unattended 短路，直连调用点仍弹窗阻塞 |
| unattended 下无信息 | `FMessageDialog::Open` 静默返回默认值，弹窗文本被丢弃，日志里什么都没有 |
| 无法逐点修改 | `MessageBoxExt`/`FMessageDialog` 调用点多达 40~46 处，逐点埋点不可维护 |

核心矛盾：**弹窗是唯一报错出口，但 Windows 弹窗实现既不写日志、也不做无人值守处理**。

---

## 三、详细技术原理

### 3.1 MessageBox 调用链收敛结构

```
编辑器/引擎任意报错点
        │
        ▼
FMessageDialog::Open / Debugf / ShowLastError   (MessageDialog.cpp)
        │  (编辑器下 ModalMessageDialog 未绑定 → 走平台路径)
        ▼
FPlatformMisc::MessageBoxExt                     (唯一汇聚点)
        │
        ├── Generic 版：UE_LOG + LowLevelOutputDebugStringf（写日志）
        └── Windows 版：FWindowsDialog::Show → CreateDialogParam 弹窗（不写日志）★问题所在
```

- `MessageDialog.cpp:172-182`：`!FApp::IsUnattended() && !GIsRunningUnattendedScript` 且非编辑器命令台时，才真正弹窗；否则返回默认值。
- `MessageDialog.cpp:184-187`：弹窗后 `GWarn->Logf("Message dialog closed, result: %s, title: %s, text: %s", ...)` —— 只有这一行记录，且是弹窗**关闭后**、带 result 的格式，原始报错文本仍不完整落日志。

### 3.2 关键编译约束

- `LogGenericPlatformMisc` 在 `GenericPlatformMisc.cpp:57` 用 `DEFINE_LOG_CATEGORY_STATIC` 声明，是**文件内 static**，跨文件使用会编译报错。→ 改用 `LogWindows`（`WindowsPlatform.h:126` 通过 `PLATFORM_GLOBAL_LOG_CATEGORY` 在 `CoreGlobals.h:63-64` 声明为 CORE_API 全局分类）。
- `FApp::IsUnattended()` 来自 `Misc/App.h`，`TAutoConsoleVariable` 来自 `HAL/IConsoleManager.h`，`FParse::Param` 来自 `Misc/CommandLine.h` —— 目标文件均已 include，无需新增。
- `TAutoConsoleVariable<int32>` 作函数内 `static const` 局部变量：首次调用时构造注册，`GetValueOnAnyThread()` 读取（控制台初始化后调用，无静态初始化死角）。

---

## 四、修复方案

**文件**：`Engine/Source/Runtime/Core/Private/Windows/WindowsPlatformMisc.cpp`
**位置**：`FWindowsPlatformMisc::MessageBoxExt` 函数入口
**P4**：已 `p4 edit` 迁出（#6），改动用 `#pragma region Engine ZXB` / `#pragma endregion` 包裹标记。

```cpp
EAppReturnType::Type FWindowsPlatformMisc::MessageBoxExt( EAppMsgType::Type MsgType, const TCHAR* Text, const TCHAR* Caption )
{
#pragma region Engine ZXB
	// [AI-TRACK] 统一拦截点：所有 MessageBox 类弹窗的完整文本无条件写入日志，供 AI/脚本自动识别。
	UE_LOG(LogWindows, Warning, TEXT("[MessageBox] Caption=%s Text=%s"), Caption ? Caption : TEXT(""), Text ? Text : TEXT(""));

	// r.DisableEditorMessageBox=1 时强制「只记日志不弹窗」；unattended（cook/CI）下同样短路，避免弹窗阻塞进程。
	static const TAutoConsoleVariable<int32> CVarDisableEditorMessageBox(
		TEXT("r.DisableEditorMessageBox"),
		0,
		TEXT("When >0, all MessageBoxExt dialogs are suppressed and their text is only written to the log (useful for AI-driven error tracking)."));

	if (FApp::IsUnattended() || CVarDisableEditorMessageBox.GetValueOnAnyThread() > 0)
	{
		switch (MsgType)
		{
		case EAppMsgType::Ok:
			return EAppReturnType::Ok;
		case EAppMsgType::YesNo:
			return EAppReturnType::No;
		case EAppMsgType::OkCancel:
		case EAppMsgType::YesNoCancel:
		case EAppMsgType::CancelRetryContinue:
			return EAppReturnType::Cancel;
		case EAppMsgType::YesNoYesAllNoAll:
			return EAppReturnType::No;
		case EAppMsgType::YesNoYesAllNoAllCancel:
			return EAppReturnType::Yes;
		default:
			return EAppReturnType::Cancel;
		}
	}
#pragma endregion

	struct FReleaseCursorLockScope
	{ /* ... 原有逻辑不变 ... */ };
	// ... 原有 FSlowHeartBeatScope / FWindowsDialog::Show 逻辑
}
```

### 三层逻辑说明

| 层 | 逻辑 | 效果 |
|---|---|---|
| ① 无条件落日志 | 入口第一行 `UE_LOG(LogWindows, Warning, "[MessageBox] ...")` | 所有走 `MessageBoxExt` 的弹窗文本（含 unbound 报错）进日志，AI 可 `Select-String "[MessageBox]"` 追踪 |
| ② 无人值守短路 | `FApp::IsUnattended()` 时按 `MsgType` 返回默认值，不弹窗 | cook/CI 永不卡死 |
| ③ CVar 开关 | `r.DisableEditorMessageBox=1` 强制「只记日志不弹窗」 | 有 UI 的编辑器下也可开启纯日志驱动追踪 |

### 使用方式（AI 追踪姿势）

```
Select-String "\[MessageBox\]" Saved\Logs\*.log
# 输出示例：
# [MessageBox] Caption=Error Text=Found unbound parameters being used in shadertype ...
```

---

## 五、快速排查 Checklist

- [ ] 弹窗是否走 `FPlatformMisc::MessageBoxExt`？（`FMessageDialog::Open`/`Debugf`/`ShowLastError` 及直连调用均收敛于此；Slate `AddModalWindow` 交互类对话框不走此链，但报错类弹窗全覆盖）
- [ ] 编辑器下 `FCoreDelegates::ModalMessageDialog` 是否绑定？（UE5EA 引擎源码中仅 ZenDashboard 绑定，编辑器下未绑定 → 全部落平台路径）
- [ ] `-unattended` 是否够用？—— 不够：`MessageBoxExt` 内部无 unattended 分支，且 unattended 下 `FMessageDialog::Open` 静默返回不落文本日志
- [ ] 日志分类是否跨文件可用？—— `LogGenericPlatformMisc` 是 `DEFINE_LOG_CATEGORY_STATIC`（文件内 static），跨文件必须改用 `LogWindows`（CORE_API 全局）
- [ ] 改动后用 `#pragma region Engine ZXB` 包裹、先 `p4 edit` 迁出
- [ ] 编译验证：改动在 Core 模块，需重编引擎二进制（`UnrealEditor` 或先编 Core 模块）
- [ ] 运行验证：编辑器触发任意报错弹窗 → 日志出现 `[MessageBox] Caption=... Text=...`

---

## 六、相关参考

- `Shader.cpp:165-197` `FShaderParameterMap::VerifyBindingsAreComplete`（unbound 参数弹窗来源）
- `GenericPlatformMisc.cpp:1074-1100` `FGenericPlatformMisc::MessageBoxExt`（写日志的参考实现）
- `WindowsPlatformMisc.cpp:2096-2131` `FWindowsPlatformMisc::MessageBoxExt`（本次修改点）
- `MessageDialog.cpp:44-190` `FMessageDialog::Open/Debugf/ShowLastError`（弹窗分发与 unattended 分支）
- `WindowsPlatform.h:126` + `CoreGlobals.h:63-64` `PLATFORM_GLOBAL_LOG_CATEGORY` → `LogWindows` 全局分类声明
- `GenericPlatformMisc.cpp:57` `DEFINE_LOG_CATEGORY_STATIC(LogGenericPlatformMisc, ...)`（文件内 static 分类，不可跨文件使用）
- 项目惯例：`d:/GR_DevTest` 工作区代码改动需 `#pragma region Engine ZXB` 包裹（memory ID 14849297）；文件操作前先 p4 edit（memory ID 46041332）
