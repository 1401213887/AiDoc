# UE5 Android Vulkan — VAT 材质 asuint 编译失败：min16float 无重载根因与修复

> Android Vulkan（SF_VULKAN_ES31_ANDROID）打包时 182 条 `Failed to compile Material`，全部错误为
> `no matching function for call to 'asuint'`；失败范围精确收敛到 **3 个 VAT 材质 + 全部像素着色器**。
> 根因是 UE 把 `half` 映射为 `min16float`，而 HLSL 的 `asuint` 对该类型没有内建重载。

---

## 一、问题定位流程

排查的关键是**先把范围收敛**，再顺着引用链找到失败代码。三个统计量直接把范围锁死：

| 观察维度 | 统计结果 | 排除了什么 |
|---|---|---|
| 错误类型 | 全日志 **188 条错误全是同一句** `no matching function for call to 'asuint'` | 排除多因混杂，是单一根因 |
| 失败的 shader | 7 种，**全部以 `PS` 结尾**（`TDepthOnlyPS`、`TShadowDepthPSPixelShadowDepth_NonPerspectiveCorrect`、`TMobileBasePassPSFNoLightMapPolicy*`）；**零个 VS 失败** | 指向「像素着色器里某个类型与顶点着色器不同」 |
| 失败材质 | 仅 3 个：`M_Boss02_VAT`、`M_RushEnemyBase_VAT`、`M_EFX_RBDVat_WZC_001` | **全是 VAT 材质**，指向 VAT 材质函数 |
| 平台 | 仅 `SF_VULKAN_ES31_ANDROID` | 指向平台相关的类型映射 |

**危害**：182 条 `Default Material will be used in game` —— 这些 VAT 角色 / Boss / 特效在 Android 包体里会**退化为默认灰色材质**，深度与阴影 Pass 一并失效，不是单纯的编译警告。

**顺引用链定位失败代码**（无需 sync，直接读 depot）：

```bash
P4CLIENT=<client> p4 print -q //GR/DevTest/S1Game/Content/.../M_Boss02_VAT.uasset > m.uasset
# uasset 中 Custom 节点的 HLSL 是明文存放的，提取可打印字符即可读出
tr -c '[:print:]' '\n' < m.uasset | grep -a -E "^/Game/|^/Engine/"
```

`M_Boss02_VAT.uasset` 本身只有 32 KB、节点构成简单（`MaterialFunctionCall` + `StaticSwitchParameter` + `CollectionParameter`），
**不含任何 `asuint`** —— 失败代码在它调用的材质函数里：

```
M_Boss02_VAT.uasset
  └─ MaterialFunctionCall → S1Game/Plugins/SideFX_Labs/Content/Materials/
                            MaterialFunctions/MF_VAT_RigidBodyDynamics.uasset
       ├─ Custom 节点 "Decode Magnitude"  → asuint(A) / asuint(B)
       └─ Custom 节点 "Decode Up"         → asuint(C) / asuint(D)
```

两个节点的 HLSL（SideFX Labs 原始代码，一字未改）：

```hlsl
uint a = asuint(A);
uint b = asuint(B);
a = ((a >> 16) & 0x8000) | ((((a >> 23) & 0xFF) - 112) << 10) | ((a >> 13) & 0x3FF);
b = ((b >> 16) & 0x8000) | ((((b >> 23) & 0xFF) - 112) << 10) | ((b >> 13) & 0x3FF);
a = (a & 0x8000) | ((a << 2) & 0x7FF8);
b = (b & 0x8000) | ((b << 2) & 0x7FF8);
a = (a << 16) | (b << 3);
return asfloat(a);
```

`- 112` = `127 - 15`，是 **FP32 指数偏置换成 FP16** 的经典写法 —— 这段代码**假定输入是 FP32 位模式**。

---

## 二、根因分析

### 2.1 根因链（每一环均在引擎源码中验证）

| # | 环节 | 证据位置 |
|---|---|---|
| ① | Custom 节点类型为 `MCT_Float` 的输入，生成代码里被声明为 **`MaterialFloat`** | `Engine/Source/Runtime/Engine/Private/Materials/HLSLMaterialTranslator.cpp:16033`<br>`case MCT_Float: InputParamDecl += TEXT("MaterialFloat ");` |
| ② | **像素着色器里 `MaterialFloat` = `half`**；顶点着色器里 = `float` | `Engine/Shaders/Private/Common.ush:14-31`<br>`#if PIXELSHADER && !FORCE_MATERIAL_FLOAT_FULL_PRECISION` → `#define MaterialFloat half`<br>else 分支注释原文：*"Material translated vertex shader code always uses floats"* |
| ③ | **Vulkan / ES3.1 下 `half` 被映射为 `min16float`** | `Engine/Shaders/Public/Platform.ush:355-364`<br>`#elif (VULKAN_PROFILE) \|\| (COMPILER_GLSL_ES3_1 && ...)`<br>`// For VULKAN and OPENGL ES31 use RelaxedPrecision for half floats`<br>`#define half min16float` |
| ④ | **DXC 对 `min16float` 没有 `asuint` 重载** | 实测，见下 |

**①+②+③ 合起来**：`asuint(A)` 里的 `A` 在像素着色器里实际类型是 `min16float`，在顶点着色器里是 `float`。
**④ 生效**：`min16float` 无重载 → 编译失败。

这**完整解释了三条统计特征**：
- 为什么只有 PS 失败 → 只有 PS 里 `MaterialFloat` = `half`
- 为什么只有这 3 个材质失败 → 只有它们用了含 `asuint` 的 SideFX VAT 材质函数
- 为什么只有 Android Vulkan → 只有该平台把 `half` 映射为 `min16float`

### 2.2 用 dxc 复现并确认

```bash
# DXC 位置（二选一）
#   引擎自带：UE5EA/Engine/Binaries/ThirdParty/ShaderConductor/Win64/dxc.exe
#   Windows SDK：C:/Program Files (x86)/Windows Kits/10/bin/<ver>/x64/dxc.exe
```

最小复现（`-T cs_5_0` 即可，DXC 会自动提升到 SM6.0）→ **精确复现日志报错**，且 DXC 自己给出了原因：

```
error: no matching function for call to 'asuint'
    uint a = asuint(A);
             ^~~~~~
note: candidate function not viable: no known conversion from 'min16float' to 'float' for 1st argument
```

关键在最后这句：**内建 `asuint(float)` 存在，但 `min16float` 不参与该重载的隐式转换**。

---

## 三、详细技术原理

### 3.1 `asuint` 是 HLSL 语言内建，且它**按语义就不该有 half→uint32 的重载**

`asuint` 是 HLSL 规范里的内建函数（compiler intrinsic），由编译器提供实现，编译产物就是一条 `bitcast`。
验证：**一个零 include、零宏定义的裸 HLSL 文件里 `asuint` 直接可用**。

整个 `Engine/Shaders` 中不存在任何 `asuint` 的定义 —— 唯一的同名相关物是
`TSRSpatialAntiAliasing.ush:308` 的 `#define asuintX(x) asuint16(x)`，那是 UE 自己起的**别名宏**（不是重载）。
注意 UE 的做法：**想造变体时另起名字，而不是去重载 `asuint`**。

它的重载族严格**位宽守恒**：

| 输入 | 输出 | 函数 | 存在？ |
|---|---|---|---|
| `float` / `float2/3/4`（32 位） | `uint` / `uint2/3/4`（32 位） | `asuint` | ✅ |
| `half`（16 位） | `uint16_t`（16 位） | `asuint16` | ✅（实测 `asuint16(half)` 生成 `i16 15872` = 0x3E00 = 1.5 的 FP16 位模式） |
| `half` / `min16float`（16 位） | `uint`（**32 位**） | —— | ❌ **不存在** |
| `asuint(half)`（真 half，SM6.2 + `-enable-16bit-types`） | —— | —— | ❌ 实测 `no matching function` |

**为什么"16 位进、32 位出"永远不会存在**：`asuint` 的本质是 bitcast，位宽必须守恒。
16 位进 32 位出**不是 bitcast**，而是「数值转换 + bitcast」，编译器没有理由把它塞进这个函数族。

> 这一条决定了问题的**责任归属**：这不是 UE 或 DXC 的 bug，而是 **HLSL 的语义边界**撞上了
> **UE 自己的移动端 half 精度策略**。UE 把 `half` 定义成 `min16float`，就有责任把这个映射补完整。

### 3.2 为什么实现里必须有 `float(x)`，且它在目标平台上零开销

因为要补的正是**内建按语义不提供的那次数值转换**。`float(x)` 不是"多余的搬运"，它就是那次转换本身；写出来是让编译器去挑最优实现。

在目标平台上它被**完全消除**：

- Vulkan 下 `min16float` 是 **32 位 `RelaxedPrecision`**（UE 注释原文 *"use RelaxedPrecision for half floats"*），不是真正的 16 位类型
- 因此 `float(x)` 是恒等转换 —— SPIR-V 统计 `OpFConvert = 0`，与纯 `float` 对照组指令数 123 vs 122，`OpBitcast` 均为 4
- 反汇编确认：运行期值下内联为**一条 `bitcast`**，无用户函数调用、无递归

> 顺带排除两条"看起来更直接"的路线，它们都不成立：
> 手写位展开需要先取 16 位模式，而目标平台上**根本不存在 16 位模式**（`uint16_t` 未定义）；
> 用算术提升（`x + 0.0f`）代替显式转换会**丢掉 `-0.0` 的符号位**（IEEE：`-0.0 + 0.0 = +0.0`）。

---

## 四、修复方案

### 4.1 落点选择

| 方案 | 覆盖范围 | 代价 |
|---|---|---|
| A. 改 SideFX 材质函数 `MF_VAT_RigidBodyDynamics` | 用该 MF 的所有材质 | 第三方插件内容，**插件升级会被覆盖** |
| B. 逐材质改 `Float Precision Mode` 为 `Use Full-precision for MaterialExpressions only` | 逐个材质 | 需美术重存资产；PS 精度提高有性能代价 |
| **C. 引擎侧补 `asuint` 重载族（采用）** | 所有 PS 里所有 `asuint(half)` | 引擎 fork 需维护 4 行 diff |

选 **C**：落点最靠根、不动第三方插件内容、不影响美术资产，且与 A 语义完全等价。

### 4.2 改动内容

文件：`UE5EA/Engine/Shaders/Public/Platform.ush`（3 处新增，共 19 行，0 删除 0 修改）

**新增重载体**（紧跟 `half` 映射的 `#if/#elif/#endif` 块之后）：

```hlsl
//#pragma region Engine ZXB
// [ZXB] min16float lacks an asuint overload, breaking SideFX VAT materials on Vulkan/ES3.1;
//       mirror asuint(float): reinterpret to the FP32 bit pattern that the callers decode.
#ifdef PLATFORM_HALF_IS_MIN16FLOAT
uint  asuint(half  x) { return asuint(float(x)); }
uint2 asuint(half2 x) { return asuint(float2(x)); }
uint3 asuint(half3 x) { return asuint(float3(x)); }
uint4 asuint(half4 x) { return asuint(float4(x)); }
#endif
//#pragma endregion
```

**两处标记宏**（分别在 `half` 被定义为 `min16float` 的两个平台分支内）：

```hlsl
	//#pragma region Engine ZXB
	// [ZXB] Half is a real min16float here; the asuint shim below keys off this marker.
	#define PLATFORM_HALF_IS_MIN16FLOAT 1
	//#pragma endregion
```

### 4.3 三个设计要点

1. **用 `half` 而不是 `min16float`**
   `half` 已由 `Platform.ush` 按平台定好型，所以**只写一份**，自动跟随平台映射；
   不用在两个 `min16float` 分支里各写一遍，也不用复制上游的条件表达式。

2. **补全 2/3/4 向量重载**
   对齐 `asuint(float)` 的完整重载族。只补标量的话，下一个用 `asuint(half3)` 的 VAT 材质照样炸。

3. **用标记宏守卫，而不是无条件定义**
   `FP16Math.ush` 中 `min16float` 始终出现在 `#if` 守卫内 —— 引擎从不在跨平台头里裸用这个类型名。
   守卫确保：`half` = `min16float` 的平台才有重载；`half` = `float` 的平台（D3D 等）
   **不会凭空遮蔽内建 `asuint(float)`**。

### 4.4 shader 文件里 ZXB 标记用注释形式

shader 文件（`.usf` / `.ush` / `.hlsl`）中，`Engine ZXB` 标记要写成**注释形式**：

```hlsl
//#pragma region Engine ZXB
// [ZXB] <为什么改>
//#pragma endregion
```

注意：实测 DXC 对未注释的 `#pragma region` **既不报错也不警告**（`-Werror` 也通过），
所以这是**团队约定，不是编译问题**。仓库中部分历史 shader 文件（`SceneData.ush`、
`MobileToonOutlineExpand.usf` 等）仍是未注释形式，不要照抄。

---

## 五、验证记录

| 验证项 | 方法 | 结果 |
|---|---|---|
| 复现原始失败 | 裸 HSLL 调 `asuint(min16float)` / `asuint(half)` | ✅ 精确复现日志报错 |
| 修复后编译 | 抽取改动后的 `Platform.ush` 真实文本 + Android Vulkan 宏配置 + SideFX 原始代码 → SPIR-V | ✅ 通过 |
| **反向对照** | 同文本删掉 shim | ✅ 复现原始报错（证明前一项非假阳性） |
| 四平台分支 | vulkan / mobileprev(ES3_1+MOBILE_EMULATION) / d3d / metal | ✅ 全部通过 |
| 守卫行为 | 预处理输出（`dxc -P`） | ✅ vulkan 下 shim 存在且展开为 `asuint(min16float)`；d3d 下 shim 不存在 |
| 无递归 | DXIL 反汇编 | ✅ 唯一 `define` 是 main，调用只有 `@dx.op.*` |
| 零开销 | SPIR-V opcode 统计 | ✅ `OpFConvert = 0`；123 条 vs 纯 float 对照 122 条 |

**验收判据**（在能 cook 的环境执行）：

```bash
grep -c "no matching function for call to 'asuint'" <cook 日志>   # 期望 0
grep -c "Failed to compile Material" <cook 日志>                  # 对比原 182 是否归零
```

---

## 六、快速排查 Checklist

遇到移动端 `Failed to compile Material` 时的排查顺序：

- [ ] **统计错误类型**：是否全日志同一句错误？混杂则先分桶
- [ ] **看 shader 名后缀**：全 `PS` 还是含 `VS`？
      全 PS → 高度怀疑 `MaterialFloat` = `half` 引入的类型差异（`Common.ush:14`）
- [ ] **看失败材质是否同类**：全是 VAT / 全部引用同一个材质函数 → 顺着 MF 找 Custom 节点
- [ ] **读材质函数的 Custom 节点代码**：
      `p4 print -q <MF>.uasset | tr -c '[:print:]' '\n' | grep -a -B12 -A6 "可疑API"`
      （uasset 中 Custom 节点的 HLSL 是明文，无需开编辑器）
- [ ] **看平台**：是否仅某个平台？Android Vulkan / Metal 的 `half` 映射各不相同
      （`Platform.ush` 的 `half` 映射块是必查项）
- [ ] **用 dxc 最小复现**：确认是「无重载」还是「类型不匹配」，DXC 的 `note:` 会直接给出原因
- [ ] **确认 `asuint` 语义方向**：调用方按 FP32 还是 16 位布局解释结果？
      **方向错了不会报错，只会静默算错**

**高危模式速查**：材质图里对 `MaterialFloat` 调用**位操作类内建**
（`asuint` / `asint` / `asfloat` / `asuint16`），在移动端 PS 下极易踩类型映射差异。

---

## 七、相关参考

**本仓代码位置**

| 内容 | 路径 |
|---|---|
| 修复文件 | `UE5EA/Engine/Shaders/Public/Platform.ush`（`half` 映射块及紧随其后的 asuint shim） |
| `MaterialFloat` 定义 | `UE5EA/Engine/Shaders/Private/Common.ush:14-31` |
| Custom 节点类型生成 | `UE5EA/Engine/Source/Runtime/Engine/Private/Materials/HLSLMaterialTranslator.cpp:16033` |
| 精度模式判定 | `UE5EA/Engine/Source/Runtime/Engine/Private/Materials/MaterialShared.cpp:4244-4260`（`GetOutputPrecision`）、`:3275`（`FORCE_MATERIAL_FLOAT_FULL_PRECISION`） |
| 全局精度 CVar | `r.Mobile.FloatPrecisionMode`（`RendererSettings.h:334`，显示名 *Mobile Float Precision Mode*） |
| 失败的材质函数 | `S1Game/Plugins/SideFX_Labs/Content/Materials/MaterialFunctions/MF_VAT_RigidBodyDynamics.uasset` |
| DXC 可执行文件 | `UE5EA/Engine/Binaries/ThirdParty/ShaderConductor/Win64/dxc.exe` |

**外部规范**

- HLSL 内建函数 `asuint` / `asuint16` 语义与重载族：Microsoft Learn *HLSL intrinsics* 文档
  <https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-intrinsic-functions>
- `min16float` / 最低精度类型：<https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-scalar>
- UE 移动端 `half` 与 `RelaxedPrecision` 策略：`Platform.ush` 原注释（见上表代码位置）
- SideFX Labs Vertex Animation Textures（VAT）材质函数来源：
  <https://www.sidefx.com/products/sidefx-labs/>
