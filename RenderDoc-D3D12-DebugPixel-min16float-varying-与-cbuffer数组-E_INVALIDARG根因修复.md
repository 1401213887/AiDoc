# RenderDoc-D3D12-DebugPixel-min16float-varying-与-cbuffer数组-E_INVALIDARG根因修复

> 现象：UE mobile（D3D12）截帧后对特定材质 draw 点 "debug pixel" 直接失败（`CreateGraphicsPipelineState` 返回 `E_INVALIDARG`），修好后单步执行又在 `dxbc_debug.cpp` 触发一个良性断言。两者**均为 RenderDoc 工具自身缺陷，与被调试的 shader 正确性无关**。

---

## 〇、核心结论（先说人话）

| | 报错 1 | 报错 2 |
|---|---|---|
| 现象 | 点 debug pixel 直接失败 `E_INVALIDARG`（`0x80070057`） | debug pixel 成功后单步执行断在 `RDCASSERT(right.rows == 1)`，可继续 |
| 触发条件 | 被调试 PS 的输入/VS 输出里有 `min16float`（半精度）varying | 被调试 shader 的 cbuffer 里有数组 |
| 真正根因 | RenderDoc 解析 DXBC signature 时**丢弃了 min-precision**，重建 fetcher PSO 时 VS↔PS 精度等级不匹配 | RenderDoc 对 cbuffer 数组元素的反射变量**没填 `rows`（=0）**，调试解释器假设 `rows=1` |
| 被调试 shader | 正常，`min16float` 是移动端最佳实践 | 正常，cbuffer 数组是常规用法 |
| 处置 | **修工具，不改 shader** | **修工具，不改 shader** |

一句话：**你的 shader 没问题；是 RenderDoc 的 debug pixel 工具链在"把 shader 拆开做调试插桩"这一步处理不完善。**

---

## 一、问题定位流程（确认了什么）

RenderDoc 版本：v1.x（`d:/renderdoc`，git 项目，非 P4）。测试 capture：`E:\GPUCapture\Mobile\Preview\DebugTest.rdc`（UE mobile / D3D12），问题 draw `eid=1579` = `IndirectDrawIndexed`，材质 `MI_Pizza_B_Basalt01_08 S_MergedCliffMesh`。

1. 异常码 `0x80000003`（`STATUS_BREAKPOINT`）是 `RDCERR` 在有调试器附加时主动 `int 3`，不是崩溃；真正失败是 `d3d12_shaderdebug.cpp` 里重建 input-fetcher PSO 时 `CreatePipeState` 返回 `E_INVALIDARG`。
2. 确认排除项：非 mesh/amp 管线（`AS=0 MS=0`）、RT/DSV 格式/MSAA/Flags/topology 均合法、非老接口丢子对象（走的是现代 stream 接口）、`CachedPSO` 已在 replay 侧清空、fetcher 的三个 debug UAV（`u1~u3 space1`）与 root sig 完全匹配。
3. 关 API validation 时 runtime 静默返回 `E_INVALIDARG` 不打 InfoQueue；**开 API validation** 复现（进程随后因无关原因崩溃，但崩前 InfoQueue 已抓到关键消息）：
   ```
   [D3D12 msg cat=5 sev=1 id=665] CreateGraphicsPipelineState:
   Vertex Shader - Pixel Shader linkage error: Signatures between stages are incompatible.
   Semantic 'COLOR' in each signature have different min precision levels, when they must be identical.
   ```
4. 对照 fetcher 生成的 HLSL：`float4 input_COLOR : COLOR;`（全精度），而该材质 VS 输出 `COLOR` 是 `min16float`。
5. 定位到 `dxbc_container.cpp` 解析 signature 时对 min-precision 的处理，锁定根因。
6. 报错 2 在 debug pixel 修好、进入单步执行后暴露：`mov r18, cb0[...]` 从 cbuffer 数组读值，源值 `rows=0` 触发 `SetDst` 的 `RDCASSERT(right.rows == 1)`。

### 全自动复现/验证方法（无需手点 UI）

- 系统无 Python 3.6，改用 `qrenderdoc.exe --python <脚本>`（内置 python36）跑脚本。
- 脚本要点：
  - `rd.SetDebugLogFile(path)` 把所有 `RDCLOG` 诊断重定向到文件直接读取；
  - `rd.OpenCaptureFile()` + `OpenFile(rdc,'',None)` + `OpenCapture(rd.ReplayOptions(), None)`；
  - `controller.SetFrameEvent(eid, True)` → `controller.DebugPixel(x, y, rd.DebugPixelInputs())`（**此版本 DebugPixel 是 3 参数，第三个是 `DebugPixelInputs` 结构，不是老的 4 参数 sample/primitive**）；
  - fetcher PSO 创建发生在 pixel-history **之前**、与像素是否覆盖无关 —— 只要对问题 draw 调 `DebugPixel`（哪怕像素 `(0,0)` 未覆盖）就必触发；
  - 单步验证用 `DebugPixel` 拿 trace 后循环 `ContinueDebug`；
  - 脚本末尾 `os._exit(0)` 避免打开 GUI 主界面。
- **开 `apiValidation=True` 回放此类 capture 会因无关原因 `__fastfail`**，只能关 validation 做功能验证；开 validation 仅用于崩溃前抢抓一条 InfoQueue 报错。

---

## 二、根因分析

### 报错 1：min-precision 在 signature 解析时被丢弃

DXBC 的 `ISG1/OSG1/PSG1`（带扩展信息的 signature chunk）里每个元素有独立的 `precision` 字段（`MinimumPrecision`：1=FLOAT16 2=FLOAT10 4=SINT16 5=UINT16 6=ANY16 7=ANY10）。`min16float` 的 signature：`componentType = FLOAT32`（所以 RenderDoc 映射成 `VarType::Float`），min-precision 完全靠这个独立 `precision` 字段表达。

RenderDoc 在 `dxbc_container.cpp` 解析时明确写着"丢弃 precision，不想污染通用 API 结构"，于是公共 `SigParameter` 里没有 min-precision。pixel-debug 的 input fetcher（`dx_debug.cpp: GatherInputDataForInitialValues`，用 `ToStr(sig.varType)` → `"float"`）据此把 `min16float` 重声明成全精度 `float`。重建 PSO 时 D3D12 校验 VS-out(`min16float`) 与 fetcher PS-in(`float`) 的 min-precision level 不一致 → `E_INVALIDARG`。

这解释了"最近才不能 debug"（碰上带 min-precision varying 的材质）、"只有特定材质中招"（全精度 shader 不受影响）、"去掉 `SV_PrimitiveID` 也没用"（与系统值/register 打包均无关）。

### 报错 2：cbuffer 数组元素的 `rows` 未填

`dxbc_debug.cpp: GetSrc` 的 `TYPE_CONSTANT_BUFFER` 分支直接 `v = targetVars[cbLookup]` 取反射变量。cbuffer 是数组时（源值 `name="[1]"` 即数组元素），该反射变量 `rows` 被留为 0（维度未描述全）。调试解释器把每个 cbuffer operand 当单行 vec4 取用，`rows=0` 传到 `SetDst` 就触发 `RDCASSERT(right.rows == 1)`。属于良性断言：`AssignValue` 只用 `columns`/`value`，`rows` 只是元数据，不影响计算结果（所以"继续能跑"）。

---

## 三、修复方案

> 全部改动位于 `d:/renderdoc`，均以 `// ZXB` 注释标记。`DXBC::Reflection` 是 DXBC 内部结构（`dxbc_reflect.cpp` 才拷到公共 API），给它加字段**不触碰公共 API、不触发 python binding 一致性检查**，这是最小侵入点。

### 修复 1（3 文件）：保留并复原 min-precision

**(1) `renderdoc/driver/shaders/dxbc/dxbc_common.h`** — 给 `DXBC::Reflection` 加平行的 min-precision 数组：
```cpp
rdcarray<SigParameter> InputSig;
rdcarray<SigParameter> OutputSig;
rdcarray<SigParameter> PatchConstantSig;

// ZXB: parallel arrays holding each element's DXBC min-precision (MinimumPrecision enum, 0 = none)
rdcarray<uint8_t> InputSigMinPrec;
rdcarray<uint8_t> OutputSigMinPrec;
rdcarray<uint8_t> PatchConstantSigMinPrec;
```

**(2) `renderdoc/driver/shaders/dxbc/dxbc_container.cpp`** — 解析 signature 时保留 `el1->precision`（原本被丢弃），并与签名数组同步：
- 给 `sig` 指针平行增加 `rdcarray<uint8_t> *minPrecSig`，在选 `InputSig/OutputSig/PatchConstantSig`（及 mesh 特例）时同步指向对应 min-precision 数组；
- 循环内 `uint8_t minPrec = 0; if(ISG1/OSG1/PSG1) minPrec = (uint8_t)el1->precision;`
- `sig->push_back(desc);` 后 `if(minPrecSig) minPrecSig->push_back(minPrec);`

**(3) `renderdoc/driver/shaders/dxbc/dx_debug.cpp`** — fetcher 生成类型名时按 min-precision 输出：
```cpp
// ZXB: map base VarType + DXBC min-precision to the HLSL type keyword
static rdcstr GetInputSigHLSLTypeName(VarType varType, uint8_t minPrec)
{
  switch(minPrec)
  {
    case 1: case 6: return "min16float";
    case 2: case 7: return "min10float";
    case 4:         return "min16int";
    case 5:         return "min16uint";
    default: break;
  }
  return ToStr(varType);
}
```
在 `GatherInputDataForInitialValues` 里取平行数组引用（`dxbc->GetReflection()->InputSigMinPrec`、`prevdxbc->GetReflection()->OutputSigMinPrec`），并替换两处类型生成：主分支 `fetcher.hlsl += GetInputSigHLSLTypeName(sig.varType, stageInputMinPrec[i]);`，以及"补 VS 独有 register 的 fill-holes 分支"同样改用该 helper（用 `prevStageOutputMinPrec[os]`）。

### 修复 2（1 文件）：规范化 cbuffer 源值的行数

**`renderdoc/driver/shaders/dxbc/dxbc_debug.cpp`** — `GetSrc` 的 `TYPE_CONSTANT_BUFFER` 分支取值后补 `rows`：
```cpp
if(cbLookup < (uint32_t)targetVars.count())
{
  v = targetVars[cbLookup];
  // ZXB: cbuffer array-element reflection vars may have rows==0 (dimensions not fully described).
  // The debugger fetches a cbuffer operand as a single vec4 register, so a 0-row source later trips
  // SetDst's RDCASSERT(right.rows == 1). Normalise to 1 row; columns/values are untouched.
  if(v.rows == 0)
    v.rows = 1;
}
```

### 附带修复（保留）：debug 失败后不再崩溃

`d3d12_shaderdebug.cpp` 的 `DebugPixel` 里，若中途失败（如曾经的 `E_INVALIDARG`）直接 `return`，会漏掉恢复被污染的全局 render state（`rs.graphics.rootsig` 指向已 `SAFE_RELEASE` 的 root sig），导致下一次 `SetFrameEvent`/`ExecuteCommandLists` `__fastfail`（`0xc0000409`）。在 4 个失败 `return` 分支（`psBlob==NULL`/`dataBuffer==NULL`/`CreatePipeState` 失败/cmdList Close 失败）前补 `rs = prevState;`（在 `SAFE_RELEASE(pRootSignature)` 之前），均以 `// ZXB fix` 标记。此为独立真 bug 修复，保留。

### 验证结果

| | 修复前 | 修复后 |
|---|---|---|
| eid=1579 DebugPixel | `psoFail=True`（`E_INVALIDARG`） | `debugValid=True`，诊断日志 0 个 PSO 失败 |
| 单步执行 | 断在 `RDCASSERT(right.rows==1)` | 不再触发 |
| 其它 44 个全精度 draw | 全部正常 | 仍全部正常（无回归） |

---

## 四、关于"是否要改 shader"

**不需要，且不建议。**

- `min16float`（half）varying 是**移动端最佳实践**（省带宽、省寄存器、提性能），UE mobile 广泛使用；为迁就调试工具缺陷改成 `float` 是性能倒退。
- cbuffer 数组是完全合法的常规用法。
- 两个问题只影响 RenderDoc 的 debug pixel 工具功能，**不影响 GPU 运行与画面正确性**。
- 唯一的临时下策（仅在没打补丁的 RenderDoc 上、又急着调 half 材质时）：临时把相关 varying 改成 `float` 重编一版再截帧调试 —— **绝不能进正式包**。有了打好补丁的 RenderDoc，此 workaround 也无需使用。

---

## 五、快速排查 Checklist

- [ ] debug pixel 点了直接失败？看日志有没有 `Failed to create PSO ... E_INVALIDARG`。
- [ ] 短暂开 API validation 复现，抢抓 InfoQueue 里 `CreateGraphicsPipelineState` 的 `linkage error`，重点看它点名的 **semantic** 和 "**different min precision levels**"。
- [ ] 对照 fetcher 生成的 HLSL（可在失败点 dump 到临时文件），确认某 varying 被写成 `float` 而 VS 输出实为 `min16float`。
- [ ] 单步时断在 `dxbc_debug.cpp SetDst` 的 `RDCASSERT(right.rows==1)`？看源操作数是否来自 cbuffer 数组（`name="[N]"`、`rows=0`）。
- [ ] 自动化：`qrenderdoc.exe --python`（内置 py36）+ `rd.SetDebugLogFile` + `DebugPixel(x,y,rd.DebugPixelInputs())`（3 参数）；改 `renderdoc.dll` 前先 `taskkill /F /IM qrenderdoc.exe` 否则 `LNK1168`。
- [ ] 编译：`MSBuild renderdoc.sln /p:Configuration=Development /p:Platform=x64 /m`。

---

## 六、相关文件与位置

| 文件 | 改动 |
|---|---|
| `renderdoc/driver/shaders/dxbc/dxbc_common.h` | `DXBC::Reflection` 加 `Input/Output/PatchConstantSigMinPrec` 平行数组 |
| `renderdoc/driver/shaders/dxbc/dxbc_container.cpp` | 解析 `ISG1/OSG1/PSG1` 时保留 `el1->precision`（约 line 2332/2373/2494 附近） |
| `renderdoc/driver/shaders/dxbc/dx_debug.cpp` | 新增 `GetInputSigHLSLTypeName`，fetcher 主分支 + fill-holes 分支按 min-precision 生成类型 |
| `renderdoc/driver/shaders/dxbc/dxbc_debug.cpp` | `GetSrc` cbuffer 分支 `if(v.rows==0) v.rows=1;` |
| `renderdoc/driver/d3d12/d3d12_shaderdebug.cpp` | 4 个失败分支补 `rs = prevState;` 防状态污染崩溃（附带真 bug 修复） |

> 关键 runtime 证据：D3D12 debug layer message `id=665`, `cat=5`, `sev=1` —— `Vertex Shader - Pixel Shader linkage error ... 'COLOR' ... different min precision levels`。
