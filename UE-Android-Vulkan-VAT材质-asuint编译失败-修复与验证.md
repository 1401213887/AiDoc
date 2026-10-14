# UE-Android-Vulkan-VAT材质-asuint编译失败-修复与验证

> 三个 VAT 材质（`M_Boss02_VAT` / `M_RushEnemyBase_VAT` / `M_EFX_RBDVat_WZC_001`）
> 在 Android Vulkan ES3.1 下 cook 时报 **188 条 `no matching function for call to 'asuint'`**、
> **182 条 `Failed to compile Material`**，VAT 角色/Boss/特效在移动包里退化为默认灰色材质。
> 根因是 UE 把 `half` 映射为 `min16float` 却未补齐对应内建重载。
> **修复已完成并经验证：三个材质在 `VULKAN_ES3_1_ANDROID` 下编译报错为 0。**

---

## 一、问题定位流程

| 步骤 | 确认内容 | 证据 |
|---|---|---|
| 1 | 错误种类唯一 | cook 日志 188 条错误**全是** `no matching function for call to 'asuint'` |
| 2 | 失败 shader 类型 | 7 种，**全部以 `PS` 结尾**（`TDepthOnlyPS` / `TShadowDepthPS*` / `TMobileBasePassPS*`），**零个 VS 失败** |
| 3 | 失败材质 | 只有 3 个，全部是 VAT 材质 |
| 4 | 平台 | 只有 `SF_VULKAN_ES31_ANDROID` |
| 5 | 引用链 | `M_Boss02_VAT` → `MaterialFunctionCall` → `MF_VAT_RigidBodyDynamics` |
| 6 | 失败代码点 | `MF_VAT_RigidBodyDynamics` 的两个 Custom 节点：`Decode Magnitude`(A,B) / `Decode Up`(C,D)，共 4 处 `asuint(X)` |

**第 2 步是解题关键**：失败全在 PS、VS 零失败 —— 精确对应「PS 里 `MaterialFloat` = `half`，VS 里 = `float`」。
这一个信号直接把怀疑范围锁定到 half/min16float。

## 二、根因分析

完整根因链（每一环均在引擎源码中确认）：

| 环节 | 证据位置 |
|---|---|
| Custom 节点输入被声明为 `MaterialFloat` | `Engine/Private/Materials/HLSLMaterialTranslator.cpp:16033`（`case MCT_Float: InputParamDecl += TEXT("MaterialFloat ")`） |
| PS 里 `MaterialFloat` = `half`，VS 里 = `float` | `Engine/Shaders/Private/Common.ush:14-31`（`#if PIXELSHADER && !FORCE_MATERIAL_FLOAT_FULL_PRECISION`） |
| Vulkan/ES3.1 下 `half` = `min16float` | `Engine/Shaders/Public/Platform.ush:355-364`（`#elif (VULKAN_PROFILE) \|\| (COMPILER_GLSL_ES3_1...)`，注释 "use RelaxedPrecision for half floats"） |
| 失败代码来源 | `SideFX_Labs/Content/Materials/MaterialFunctions/MF_VAT_RigidBodyDynamics.uasset`，4 处裸 `asuint(X)` |

`asuint` 是 **HLSL 语言内建**（零 include 的裸 HLSL 可直接用），不是 UE 或项目提供的；
整个 `Engine/Shaders` 中不存在 `asuint` 的定义，只有 `TSRSpatialAntiAliasing.ush:308` 的
`#define asuintX(x)` 别名宏。

**一句话根因**：UE 自己把 `half` 映射成 `min16float`，却没为 `asuint` 补齐 `min16float` 重载。

## 三、详细技术原理：为什么必须写 `float(x)`

`asuint` 的语义是 **bitcast，位宽守恒**：

- `asuint(float)` → `uint`（32→32）✅
- `asuint16(half)` → `uint16_t`（16→16）✅
- `asuint(min16float)` → **不存在**，因为「16 位进、32 位出」不是 bitcast，而是「数值转换 + bitcast」，语言按语义就不提供

补这个重载时，那次转换躲不开。两条备选写法实测均不可行：

| 备选写法 | 实测结果 |
|---|---|
| 手写位展开（`asuint16` + 位运算） | ❌ 目标平台 `error: unknown type name 'uint16_t'`（无 16 位模式可操作）；真 16bit 模式下能编译但**算错**：2⁻²⁴ 得 `0x00002000`，正确应为 `0x33800000` |
| 算术提升 `asuint(x + 0.0f)` | ❌ `-0.0h` 得 `0x00000000`，正确应为 `0x80000000`（IEEE：`-0.0 + 0.0 = +0.0`，符号位丢失） |
| **`asuint(float(x))`** | ✅ 唯一正确写法；目标平台上提升为零开销 |

**零开销验证**：SPIR-V 指令数对比 —— min16float 版 123 条 vs 纯 float 对照 122 条，
**`OpFConvert` = 0**（类型提升被编译器完全消除）；反汇编确认运行期值下内联为**一条 `bitcast`**。

## 四、修复方案

采用**双层修复**（任一层单独生效即可消除错误，双层是为了抗风险）：

### 4.1 引擎侧：补 `asuint` 的 min16float 重载族

**文件**：`UE5EA/Engine/Shaders/Public/Platform.ush`（CL **1137209**，depot head #4）

在 Vulkan/ES3.1 分支下补重载，用标记宏守卫：

```hlsl
//#pragma region Engine ZXB
// [ZXB] Half is a real min16float here; the asuint shim below keys off this marker.
#define PLATFORM_HALF_IS_MIN16FLOAT 1
//#pragma endregion
```

重载体（零递归，内联为 bitcast）：

```hlsl
uint asuint(min16float x) { return asuint(float(x)); }
```

**优点**：落点最靠根，不动第三方插件内容、不影响美术资产，一次性解决所有同类材质。

### 4.2 内容层：材质函数内改用 `asuint(float(X))`

**文件**：`SideFX_Labs/Content/Materials/MaterialFunctions/MF_VAT_RigidBodyDynamics.uasset`
（CL **1140161**，depot head #3）

两个 Custom 节点（`Decode Magnitude` / `Decode Up`）内 4 处 `asuint(X)` → `asuint(float(X))`。

> **为什么引擎修了还要修内容**：两者生成的代码**完全相同**，所以内容层修复不能替代引擎修复；
> 但**内容每次 cook 必然是最新的，引擎不保证**。修复放在不会被引擎状态阻塞的地方，才真正可靠。

## 五、验证方法与结果

### 5.1 验证方法：`CookShadersCommandlet`

完整 `BuildCookRun` 在大工程上耗时长且易受无关内容阻塞，验证单个材质的 shader 编译用
引擎自带的 `CookShadersCommandlet` 更快更干净：

```bash
UnrealEditor-Cmd.exe <Project>/S1Game.uproject \
  -run=CookShaders -targetPlatform=Android \
  -ShaderSymbolsExport=<输出目录> \
  -material=<材质名> -noglobals -nomaterialinstances \
  -unattended -nosplash -stdout -NoLogTimes
```

它**只编译指定材质的 shader**，走的是与 cook 完全相同的 `VULKAN_ES3_1_ANDROID`
编译链路（`HLSLMaterialTranslator` → DXC → SPIRV-Cross）。

参数说明（`-run=CookShaders -help`）：

| 参数 | 说明 |
|---|---|
| `-targetPlatform=<platform>` | 必填，目标平台 |
| `-ShaderSymbolsExport=<path>` | 必填，符号输出位置 |
| `-material=<string>` | 无 `.info` 文件时直接指定要编译的材质，一次一个 |
| `-noglobals` | 不编译全局 shader |
| `-nomaterialinstances` | 不编译材质实例 |

### 5.2 黄金标准对照实验

同一命令、同一平台、同一材质，**唯一变量 = 资产的 rev**：

| 资产版本 | `asuint` 报错 | `Failed to compile Material` | shader 队列 |
|---|---|---|---|
| **rev1**（裸 `asuint(A..D)` ×4） | **8** | **2** | — |
| **rev3**（`asuint(float(` ×4） | **0** | **0** | `Shaders left to compile 0` |

两侧日志都出现下列行，**证明确实进入了编译**（而非 DDC 命中被跳过）：

```
LogMaterial: Display: Missing cached shadermap for M_Boss02_VAT in VULKAN_ES3_1_ANDROID,
             High, ES3_1, Game (DDC key hash: bcea22fc...), compiling.
```

rev1 侧精确复现原始错误形态：

```
/Engine/Generated/Material.ush:4039:10: error: no matching function for call to 'asuint'
uint a = asuint(A);
         ^~~~~~
Shader TMobileBasePassPSFNoLightMapPolicyEnableLuxGIAvoidLightLeakingLOCAL_LIGHTS_DISABLED,
Permutation 2, VF TGPUSkinVertexFactoryDefault
```

与原始 188 条错误的**同一行、同一句 DXC 说明、同一 shader 形态**。

### 5.3 三个目标材质最终结果（rev3 = 修复版）

| 材质 | `asuint` 报错 | `Failed to compile` | 确实进入编译 |
|---|---|---|---|
| `M_Boss02_VAT` | **0** | **0** | ✅（对照 rev1 = 8 报错 / 2 失败） |
| `M_RushEnemyBase_VAT` | **0** | **0** | ✅ 5 行 `compiling` |
| `M_EFX_RBDVat_WZC_001` | **0** | **0** | ✅ 2 行 `compiling` |

三个材质共用同一个 `MF_VAT_RigidBodyDynamics`，反假阳性的反向对照已在 `M_Boss02_VAT` 上完成，
故三者结论同源成立。

**结论：修复生效，三个材质在 Android Vulkan 下 cook 不再报错。**

## 六、快速排查 Checklist

面对「某材质在 Android/移动端 cook 报 shader 编译失败」：

1. **先看失败 shader 的类型分布** —— 全 PS 还是全 VS？全 PS 指向 `half`/`min16float` 类问题
2. **确认错误是否唯一** —— 单一错误种类通常指向单一的翻译器/精度问题
3. **定位失败代码来源** —— 材质 → `MaterialFunctionCall` → 材质函数；Custom 节点内代码可用二进制字符串提取
4. **在引擎源码验证精度链** —— `HLSLMaterialTranslator.cpp` 定类型 → `Common.ush` 定 PS/VS 差异 → `Platform.ush` 定平台映射
5. **用 DXC 单点复现** —— 抽出的代码片段直接喂 `dxc.exe`，加目标平台宏，可快速验证修法（无需跑完整 cook）
6. **验证用 `-run=CookShaders -material=<名>`** —— 绕开完整 cook，分钟级拿到单材质编译结果
7. **反假阳性**：把资产换回修复前的 rev 再跑一遍，**必须报错**，否则说明根本没编译到

## 七、关键注意事项（踩坑记录）

### 7.1 判「是否真编译了」不能只看报错数

报错为 0 有两种可能：**修好了**，或者**根本没编译到**。必须有一项证据排除后者：

- ✅ `Missing cached shadermap for <材质> ... compiling` 行（证明进了编译队列）
- ✅ **换回旧 rev 反向对照必须报错**（最硬的证据）
- ❌ `#pragma message` 探针 —— **`CookShaders` 路径不输出它**，在此链路上会永远为 0，不可用作判据

### 7.2 同 stream 多 P4 工作区时，务必确认构建读的是哪个

本机曾有**两个 root 在同一 stream `//GR/DevTest` 上**的工作区，而全局 `P4CLIENT` 指向的不是期望的那个：

| 工作区 | root | 该资产 rev |
|---|---|---|
| `DJANGOZHAN-PCFW_GR_DevTest`（全局默认） | `D:\GR_DevTest` | have **1** / head 3（旧） |
| `DJANGOZHAN-PCFW_GR_DevTest_V1` | `D:\GR_Devtest_V1` | have 3 = head 3 ✅ |

若构建脚本的 `ENGINE_ROOT`/`PROJECT_DIR` 指向了旧工作区，就会造成
**「depot 明明有修复，但 cook 依然复现原错误」**的假象，极难排查。

> **判据：`p4 fstat` 的 `haveRev`，不是全局 `P4CLIENT` 环境变量。**

### 7.3 `p4 files` 的文件数 ≠ 存活文件数

`p4 files` 会把 **`headAction delete`** 的历史条目一并列出，用于对账会得出「本地大量文件缺失」的错误结论。
应改用 `p4 have` 与磁盘实际文件数比对。

## 八、相关参考

- 引擎侧改动：`//GR/DevTest/UE5EA/Engine/Shaders/Public/Platform.ush`（CL 1137209）
- 内容侧改动：`//GR/DevTest/S1Game/Plugins/SideFX_Labs/Content/Materials/MaterialFunctions/MF_VAT_RigidBodyDynamics.uasset`（CL 1140161）
- 相关代码位置：
  - `Engine/Source/Runtime/Engine/Private/Materials/HLSLMaterialTranslator.cpp:16033`
  - `Engine/Shaders/Private/Common.ush:14-31`
  - `Engine/Shaders/Public/Platform.ush:355-364`
  - `Engine/Source/Editor/UnrealEd/Private/Commandlets/CookShadersCommandlet.cpp`
