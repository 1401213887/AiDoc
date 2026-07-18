# UE Mobile Deferred Preview 下 CartoonShadow 参数 unbound 根因与 IS_MOBILE_BASE_PASS 分流修复

> 现象：Mobile Deferred Preview 编译 `TMobileBasePassPSFNoLightMapPolicyLOCAL_LIGHTS_DISABLED` 报
> `Found unbound parameters being used`，`bUseCartoonShadow / ShadowColor / ShadowAOColor / ShadowStrength /
> ShadowAOContrast / ShadowIntensity / ShadowOpacity / ShadowAOOpacity` 8 个参数 `not bound!`。
> 核心线索：这些参数「原来就有全局定义却不报错」，加了让 base pass 调用 `ApplyCartoonShadow` 的改动后才报。
> 根因：base pass 首次真正引用了这批 loose 全局，而分流条件 `!MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS`
> 只覆盖 forward base pass，漏掉了 deferred base pass。

---

## 一、问题定位流程

1. **确认报错 pass 的身份**：`TMobileBasePassPS...` 是 **mobile base pass 的 PS**（`MobileBasePassPixelShader.usf`），
   而非全屏 shading pass。
2. **定位参数声明位置**：用户引用的 `ToonMobileLightingCommon.ush` 里并没有这 8 个参数；真正声明/引用它们的是
   `ToonDeferredLightingCommon.ush`（base pass 经 `MobileBasePassPixelShader.usf` → `ToonMobileLightingCommon.ush`
   → `MobileLightingCommon.ush` → `ToonDeferredLightingCommon.ush` 链路 include）。
3. **确认关键宏来源**：`MOBILE_DEFERRED_SHADING` 由 `ShaderCompiler.cpp` **按 shader platform 全局注入**，
   对该平台上编译的所有 shader（含 base pass PS）都生效。
4. **代入分流条件**：deferred preview 下 base pass 编译时 `MOBILE_DEFERRED_SHADING=1`、`IS_MOBILE_BASE_PASS=1`，
   `!MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` 恒假 → 走 `#else` 声明裸全局 → base pass 绑不到 → unbound。
5. **追问「原来为何不报错」**：查 default changelist diff，发现 base pass 原来传 `DeviceZ=-1` 跳过
   `ApplyCartoonShadow`，参数被编译器 strip；本次改成 `SvPosition.z` 后首次引用，才暴露绑定缺失。

---

## 二、根因分析

### 2.1 UE "unbound parameter" 的判定本质

UE 报 `Parameter XXX not bound!` 的判定**不是**「shader 里声明了全局却没绑」，而是：

> 该参数在编译后的 shader 里被**实际引用 / 未被优化掉**（进了反射表），但 CPU 侧对应的 shader 类没有提供绑定。

关键推论：一个 loose 全局（如 `float4 ShadowColor;`）**只声明、从未被任何活跃代码路径引用**时，
DXC/编译器会 **dead-strip** 掉它，它不进反射表，UE 根本不检查、也不报 unbound。

**「有定义」≠「被使用」；unbound 检测只认「被使用」。** 这是本问题的核心认知点。

### 2.2 原来为什么不报错

- 这 8 个 loose 全局只在 `ApplyCartoonShadow` 里被引用，而 `ApplyCartoonShadow` 在 `AccumulateLuxGILighting`
  内部、以 **`DeviceZ >= 0`** 为门槛调用。
- 原来 base pass 传 `DeviceZ = -1` → 卡在门槛外 → `ApplyCartoonShadow` 不执行 → 8 个裸全局在 base pass
  **从未被引用** → 被编译器 strip → 反射表无 → **base pass 不报 unbound**。
- 合法使用这批全局的是**全屏 shading pass**：`FMobileDeferredShadingPS`（`MobileDeferredShadingPass.cpp:118`）
  与桌面 Lumen 的 `DiffuseIndirectComposite`（`IndirectLightRendering.cpp:197`）通过
  `SHADER_PARAMETER_STRUCT_INCLUDE(FCartoonShadowParameters)` **显式绑定**了它们 → 有绑定、不报错。
- 即：**引用它们的 pass 有绑定；没绑定的 pass 不引用它们**，两边相安无事。

### 2.3 本次改动如何把错误引出来

ZXB 改动目的：让 forward base pass 也调用 `ApplyCartoonShadow`（对齐 Deferred，使 LuxGI 间接光带上卡通阴影
调制，不再偏亮偏平）。手段是把 `DeviceZ` 从 `-1` 改成 `SvPosition.z`（恒 ≥0）。

后果链：
1. `DeviceZ ≥ 0` → base pass 真正走进 `ApplyCartoonShadow` → **首次引用**这 8 个裸全局。
2. 编译器不再 strip → 它们进入 base pass 反射表。
3. `TMobileBasePassPS` 的 CPU 侧参数结构里**没有** `FCartoonShadowParameters`，只有 `MobileBasePass` UB
   → 8 个裸全局无人绑定 → **报 unbound**。

作者已配套加了 UB 分流（见下），但**分流条件写窄了**。

### 2.4 为什么配套改了仍报错——条件写窄

分流条件写成 `#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS`，语义只覆盖 **forward** base pass。
`MOBILE_DEFERRED_SHADING` 按平台全局注入，deferred preview 下连 base pass 也 =1：

```
!MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS = !1 && 1 = false
```

→ deferred base pass 退回 `#else` 裸全局 → 依旧没绑 → 报错复现。

---

## 三、详细技术原理

### 3.1 关键宏 / 绑定点索引

| 位置 | 作用 |
|---|---|
| `ShaderCompiler.cpp:3522-3523` | 按 shader platform 全局注入 `MOBILE_DEFERRED_SHADING = IsMobileDeferredShadingEnabled(platform)` |
| `MobileBasePassRendering.cpp:260` | base pass PS 无条件 `SetDefine(IS_MOBILE_BASE_PASS, 1)`（桌面 base pass 为 0） |
| `MobileDeferredShadingPass.cpp:118` | 全屏 shading pass 通过 `SHADER_PARAMETER_STRUCT_INCLUDE(FCartoonShadowParameters)` 绑定 8 个 loose 全局 |
| `IndirectLightRendering.h:81` | `FCartoonShadowParameters` 结构定义（`bUseCartoonShadow` / `ShadowIntensity` 等） |
| `MobileBasePassRendering.h:79-88` | base pass UB 新增 `FoliageShadowIntensity` + `MobileForwardXXX` 8 字段 |
| `MobileBasePassRendering.cpp:445-456` | `SetupMobileBasePassUniformParameters` **无条件填值**（不在任何 deferred 判断分支内，故 deferred base pass 也有值） |

### 3.2 正确的分流语义

判定应为「**是不是 base pass**」，而非「是不是 forward base pass」：

- **任何 base pass**（forward + deferred base pass，`IS_MOBILE_BASE_PASS==1`）→ 走 UB 分支，取
  `MobileBasePass.MobileForwardXXX` / `MobileBasePass.FoliageShadowIntensity`（UB 无条件填值，deferred base pass 也绑得到）。
- **非 base pass**（全屏 shading pass 等，`IS_MOBILE_BASE_PASS==0`）→ 走 `#else` 裸全局，由 `FCartoonShadowParameters` 绑定。

### 3.3 为什么 ShadowColor 不能用 #define 而 bUseCartoonShadow 可以

- `bUseCartoonShadow` 不与任何 `FGBufferData` 成员同名，可安全 `#define bUseCartoonShadow MobileBasePass.MobileForwardUseCartoonShadow`。
- `ShadowColor` 等 7 个中的 `ShadowColor` **与 `FGBufferData` 成员同名**，若 `#define` 会把 `GBuffer.ShadowColor`
  误替换为 `GBuffer.MobileBasePass.MobileForwardShadowColor` → `no member named 'MobileBasePass'` 编译错误。
  故这 7 个改为在 `ApplyCartoonShadow` 函数体开头声明**同名局部变量**从 UB 取值（局部变量只遮蔽函数内裸引用，
  不波及成员访问）。

---

## 四、修复方案

统一将分流条件收敛为「只判 `IS_MOBILE_BASE_PASS`」，去掉 `!MOBILE_DEFERRED_SHADING`。

### 4.1 改动清单（5 处，2 个文件）

**`ToonDeferredLightingCommon.ush`**

| 位置 | 参数 | 改动 |
|---|---|---|
| `bUseCartoonShadow` 分流 | `#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` → `#if IS_MOBILE_BASE_PASS` | `#define` 到 UB |
| 7 个 `Shadow*` 裸全局声明 | 同上 | base pass 不声明裸全局 |
| `ApplyCartoonShadow` 内局部变量赋值 | 同上 | 从 `MobileBasePass.MobileForwardXXX` 取值 |
| `FoliageShadowIntensity` 裸全局 | `#if MOBILE_DEFERRED_SHADING \|\| !IS_MOBILE_BASE_PASS` → `#if !IS_MOBILE_BASE_PASS` | 德摩根取反保持一致 |

**`MobileLightingCommon.ush`**（连带一致性修正，避免 deferred base pass 下 `FoliageShadowIntensity` 既无 `#define` 又无裸全局 → undefined identifier）

| 位置 | 参数 | 改动 |
|---|---|---|
| L42 `FoliageShadowIntensity` 的 `#define` | `#if !MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` → `#if IS_MOBILE_BASE_PASS` | base pass 一律 `#define` 到 UB |

### 4.2 修复代码片段示例（bUseCartoonShadow）

```hlsl
//#pragma region Engine ZXB
#if IS_MOBILE_BASE_PASS
#define bUseCartoonShadow MobileBasePass.MobileForwardUseCartoonShadow
#else
int bUseCartoonShadow;
#endif
//#pragma endregion
```

### 4.3 方案对比

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A（采用）收敛为 `IS_MOBILE_BASE_PASS`** | 所有 base pass 统一走 UB，非 base pass 走裸全局 | 一次性覆盖 forward/deferred base pass；与 UB 无条件填值天然对齐；对称清晰 | 需同步修正 `MobileLightingCommon.ush` 保持一致 |
| B 给 base pass 参数结构 `SHADER_PARAMETER_STRUCT_INCLUDE(FCartoonShadowParameters)` | 让 base pass 直接绑 loose 全局 | 复用现成结构 | base pass 是 mesh pass，loose 全局绑定语义与 UB 冲突，改动面更大、风险高 |
| C 回退 `DeviceZ=-1` | 让 base pass 不调用 `ApplyCartoonShadow` | 立刻消除报错 | 丢掉本次改动目的（forward LuxGI 卡通阴影调制），偏亮偏平问题回归 |

---

## 五、快速排查 Checklist

- [ ] 报错的是 base pass（`TMobileBasePassPS...`）还是全屏 shading pass？—— 决定该走 UB 还是 loose 全局绑定。
- [ ] 参数真正声明在哪个 `.ush`？（本例是 `ToonDeferredLightingCommon.ush`，不是入口 include 的那个）
- [ ] 该参数是否只在某个「有门槛」的函数里被引用？（如 `ApplyCartoonShadow` 的 `DeviceZ>=0`）—— 门槛变化会改变「是否被引用」。
- [ ] 分流宏是否按平台全局注入？（`MOBILE_DEFERRED_SHADING` 是；不能想当然认为 base pass 下它=0）
- [ ] 分流条件是否覆盖 deferred base pass？避免 `!MOBILE_DEFERRED_SHADING && IS_MOBILE_BASE_PASS` 这种只覆盖 forward 的写法。
- [ ] UB 侧字段是否**无条件填值**？（`SetupMobileBasePassUniformParameters` 里，不能藏在 `if(!bDeferred)` 分支内）
- [ ] `#define` 重定向的 token 是否与 `FGBufferData` 成员同名？同名者禁用 `#define`，改用同名局部变量。
- [ ] 关联参数（如 `FoliageShadowIntensity`）的分流条件是否与主参数**保持一致**？否则会出现 undefined identifier。
- [ ] deferred permutation 只在 Android 复现，验证用 `Android_ASTC` cook 看 `Found unbound parameters` / `not bound!` 是否消失。

---

## 六、相关参考

- 关键代码位置（本仓库 `d:/GR_DevTest/UE5EA`）：
  - `Engine/Source/Runtime/Engine/Private/ShaderCompiler/ShaderCompiler.cpp:3522` —— `MOBILE_DEFERRED_SHADING` 按平台注入
  - `Engine/Source/Runtime/Renderer/Private/MobileBasePassRendering.cpp:260,445-456` —— `IS_MOBILE_BASE_PASS` 与 UB 填值
  - `Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp:118` —— 全屏 pass 绑定 `FCartoonShadowParameters`
  - `Engine/Source/Runtime/Renderer/Private/IndirectLightRendering.h:81` —— `FCartoonShadowParameters` 结构定义
  - `Engine/Shaders/Private/ToonDeferredLightingCommon.ush` —— 8 个参数声明与 `ApplyCartoonShadow`
  - `Engine/Shaders/Private/MobileLightingCommon.ush:42` —— `FoliageShadowIntensity` 的 `#define`
  - `Engine/Shaders/Private/MobileBasePassPixelShader.usf:1198` —— base pass 调用 `AccumulateLuxGILighting`（`DeviceZ` 改动点）
- 相关历史文档：`d:/GR_DevTest/UE-Mobile-LuxGI-Forward与Deferred效果不一致-ApplyCartoonShadow参数绑定修复.md`
