# UE5 BasePass PS 过程量调试 — 持久 DebugValueBuffer per-draw 绑定方案

> 在 UE5 BasePass Pixel Shader 中捕获任意执行位置的中间变量（BaseColor、Metallic、Specular、Roughness 等过程量），通过自定义持久 `RWStructuredBuffer<float4>` + per-draw `GetShaderBindings` 绑定传递到后置 CS，再用 ShaderPrint 显示到屏幕。

---

## 一、问题定位

### 1.1 需求

在 UE5 延迟渲染管线的 BasePass PS 中调试执行过程中的**中间变量**（过程量），例如 `GetMaterialBaseColor()` 返回后但 `ApplyDBufferData()` 修改前的 BaseColor。后置 CS 只能读 GBuffer 最终值，过程量在 PS 寄存器中随 PS 结束而消失。

### 1.2 确认的关键事实

1. **ShaderPrint 框架的 `ShaderPrint::FShaderParameters`（含 UB + SRV + UAV）无法绑定到 Mesh Material Shader**：无论用 `SHADER_PARAMETER_STRUCT_INCLUDE`（RDG-managed）还是 `SHADER_PARAMETER_STRUCT_REF`（引用式），在 `FOpaqueBasePassParameters` 中声明后都不会自动绑定到 BasePass PS。原因是 Mesh Draw Command 的参数绑定通过 `FMeshDrawCommand::SubmitDraw` 执行，只设置 `GetShaderBindings()` 中声明的 per-draw 参数，Pass-level RDG 参数不参与。

2. **`GetShaderBindings()` 在 Mesh Draw Command 创建时调用，早于 RDG Pass 执行**：此时 RDG 资源（`FRDGBufferRef`）还没有 RHI 后端，无法用于 per-draw 绑定。只有持久 RHI 资源（`FRHIBufferRef`、`FUnorderedAccessViewRHIRef`、`TUniformBufferRef`）才能在 `GetShaderBindings` 中使用。

3. **ShaderPrint 的 `RWEntryBuffer` 是 RDG-managed**：由 `ShaderPrint::BeginViews` 通过 `GraphBuilder.CreateBuffer` 创建，生命周期受 RDG 管理，无法用于 per-draw 绑定。

---

## 二、根因分析

### 2.1 Mesh Material Shader 参数绑定机制

UE5 中 Shader 参数绑定分两条路径：

| 路径 | 适用 Shader 类型 | 机制 | 参数来源 |
|---|---|---|---|
| **Pass-level** | Global Shader（FGlobalShader） | `SHADER_PARAMETER` 在 Pass Parameters 中 → RDG 自动绑定 | RDG-managed 资源 |
| **Per-draw** | Mesh Material Shader（FMeshMaterialShader） | `LAYOUT_FIELD` 在 Shader 类中 → `GetShaderBindings()` 设置 | 持久 RHI 资源 |

BasePass PS 是 `TBasePassPixelShaderPolicyParamType<LightMapPolicyType>`，继承自 `FMeshMaterialShader`，走 per-draw 路径。

### 2.2 为什么 ShaderPrint 不能直接用于 BasePass PS

ShaderPrint 的三件套（`ShaderPrintData` UB + `ShaderPrint_StateBuffer` SRV + `ShaderPrint_RWEntryBuffer` UAV）全部是 RDG-managed 资源，通过 `ShaderPrint::SetParameters()` 填充到 `ShaderPrint::FShaderParameters` 中。这个机制只对 Global Shader 有效（Lumen/Nanite 等 71 个 .usf 全是 Global Shader），对 Mesh Material Shader 无效。

### 2.3 解法核心思路

**用持久（non-RDG）的 RHI 资源绕过 RDG 生命周期**：

1. 创建一个全局持久 `FRHIBuffer`（StructuredBuffer），配套 UAV 和 SRV
2. 在 BasePass PS 中通过 `RWStructuredBuffer<float4>` 写入过程量
3. UAV 通过 `GetShaderBindings()` per-draw 绑定到 PS
4. BasePass 后的 CS 通过 SRV 读取，再用 ShaderPrint（Global Shader 路径）打印

---

## 三、最终方案

### 3.1 架构

```
BasePass PS (屏幕中心像素)
  ├── GetMaterialBaseColor → BaseColor
  ├── GetMaterialMetallic → Metallic
  ├── GetMaterialSpecular → Specular
  ├── GetMaterialRoughness → Roughness
  │
  ↓ 写入
RWStructuredBuffer<float4> DebugValueBuffer (持久 RHI buffer)
  ↓ (per-draw 绑定 via GetShaderBindings)
  │
  ↓ CS 读取 (BasePass 后, via SRV)
  │
  ↓ ShaderPrint 打印到屏幕
```

### 3.2 修改文件清单

| 文件 | 改动 |
|---|---|
| `BasePassRendering.h` | `TBasePassShaderElementData` 加 `FUnorderedAccessViewRHIRef DebugValueUAV`；Shader 类加 `LAYOUT_FIELD(FShaderResourceParameter, DebugValueBufferUAV)` + `Bind`；文件末尾声明全局 accessor |
| `BasePassRendering.inl` | `GetShaderBindings` 中加 per-draw UAV 绑定 |
| `BasePassRendering.cpp` | 全局持久 buffer + UAV + SRV 创建；三个 accessor 函数；Clear 函数；`ShaderElementData.DebugValueUAV` 填充 |
| `BasePassPixelShader.usf` | 声明 `RWStructuredBuffer<float4> DebugValueBuffer`；屏幕中心像素写入 BaseColor + Material params |
| `BasePassShaderPrintDebug.usf`（新建） | CS 读 GBufferC + DebugBuffer，用 ShaderPrint 打印 |
| `DeferredShadingRenderer.cpp` | CS 类加 `SHADER_PARAMETER_SRV`；BasePass 前 Clear；CS 调度 |

### 3.3 核心代码

#### 3.3.1 BasePassRendering.h — Shader 类 + ShaderElementData

```cpp
// TBasePassShaderElementData 加数据
template<typename LightMapPolicyType>
class TBasePassShaderElementData : public FMeshMaterialShaderElementData
{
public:
    TBasePassShaderElementData(const typename LightMapPolicyType::ElementDataType& InLightMapPolicyElementData) :
        LightMapPolicyElementData(InLightMapPolicyElementData)
    {}
    typename LightMapPolicyType::ElementDataType LightMapPolicyElementData;
    // [ShaderPrint Debug] per-draw debug UAV binding
    FUnorderedAccessViewRHIRef DebugValueUAV;
};

// TBasePassPixelShaderPolicyParamType 加 LAYOUT_FIELD + Bind
private:
    LAYOUT_FIELD(FShaderUniformBufferParameter, ReflectionCaptureBuffer);
    LAYOUT_FIELD(FShaderUniformBufferParameter, LuxGIVolume);
    // [ShaderPrint Debug] Custom debug UAV for process value capture
    LAYOUT_FIELD(FShaderResourceParameter, DebugValueBufferUAV);

// 构造函数中 Bind
DebugValueBufferUAV.Bind(Initializer.ParameterMap, TEXT("DebugValueBuffer"));

// 文件末尾声明全局 accessor
class FRHICommandList;
RENDERER_API FShaderResourceViewRHIRef GetBasePassDebugValueSRV();
RENDERER_API void ClearBasePassDebugValueBuffer(FRHICommandList& RHICmdList);
```

#### 3.3.2 BasePassRendering.inl — GetShaderBindings

```cpp
template<typename LightMapPolicyType>
void TBasePassPixelShaderPolicyParamType<LightMapPolicyType>::GetShaderBindings(...)
{
    FMeshMaterialShader::GetShaderBindings(...);
    LightMapPolicyType::GetPixelShaderBindings(...);

    if (LuxGIVolume.IsBound() && Scene)
    {
        ShaderBindings.Add(LuxGIVolume, Scene->UniformBuffers.MobileLuxGIUniformBuffer);
    }

    // [ShaderPrint Debug] Bind debug UAV per-draw
    if (DebugValueBufferUAV.IsBound() && ShaderElementData.DebugValueUAV.IsValid())
    {
        ShaderBindings.Add(DebugValueBufferUAV, ShaderElementData.DebugValueUAV);
    }
}
```

#### 3.3.3 BasePassRendering.cpp — 全局持久 Buffer

```cpp
// [ShaderPrint Debug] Global persistent debug value buffer
static FRHIBufferRef GDebugValueBufferRHI = nullptr;
static FUnorderedAccessViewRHIRef GDebugValueUAV = nullptr;
static FShaderResourceViewRHIRef GDebugValueSRV = nullptr;

FUnorderedAccessViewRHIRef GetBasePassDebugValueUAV()
{
    if (!GDebugValueUAV.IsValid())
    {
        FRHIResourceCreateInfo CreateInfo(TEXT("BasePassDebugValueBuffer"));
        uint32 Stride = 16; // sizeof(float4)
        uint32 Count = 64;
        GDebugValueBufferRHI = RHICreateStructuredBuffer(Stride, Stride * Count,
            BUF_UnorderedAccess | BUF_ShaderResource, CreateInfo);
        GDebugValueUAV = RHICreateUnorderedAccessView(GDebugValueBufferRHI, Stride, PF_A32B32G32R32F);
        GDebugValueSRV = RHICreateShaderResourceView(GDebugValueBufferRHI, Stride, PF_A32B32G32R32F);
    }
    return GDebugValueUAV;
}

FShaderResourceViewRHIRef GetBasePassDebugValueSRV()
{
    GetBasePassDebugValueUAV(); // Ensure initialized
    return GDebugValueSRV;
}

void ClearBasePassDebugValueBuffer(FRHICommandList& RHICmdList)
{
    if (GDebugValueBufferRHI)
    {
        uint32 Zero[4] = {0, 0, 0, 0};
        RHICmdList.ClearUAVUint(GDebugValueUAV, Zero);
    }
}

// 在 ShaderElementData 创建处填充（FBasePassMeshProcessor::AddMeshBatch 中）
TBasePassShaderElementData<LightMapPolicyType> ShaderElementData(LightMapElementData);
ShaderElementData.InitializeMeshMaterialData(ViewIfDynamicMeshCommand, PrimitiveSceneProxy, MeshBatch, StaticMeshId, true);
ShaderElementData.DebugValueUAV = GetBasePassDebugValueUAV();
```

#### 3.3.4 BasePassPixelShader.usf — PS 写入过程量

```hlsl
// 文件头部 include 区域加
RWStructuredBuffer<float4> DebugValueBuffer;

// 在 FPixelShaderInOut_MainPS 中，BaseColor 提取后
half3 BaseColor = GetMaterialBaseColor(PixelMaterialInputs);
half  Metallic  = GetMaterialMetallic(PixelMaterialInputs);
half  Specular  = GetMaterialSpecular(PixelMaterialInputs);
float Roughness = GetMaterialRoughness(PixelMaterialInputs);

// [ShaderPrint Debug] Write process values to debug buffer at screen center
{
    uint2 PixelCoord = uint2(In.SvPosition.xy);
    uint2 Resolution = View.ViewSizeAndInvSize.xy;
    uint2 CenterCoord = Resolution / 2;
    if (all(PixelCoord == CenterCoord))
    {
        DebugValueBuffer[0] = float4(BaseColor, 1.0);
        DebugValueBuffer[1] = float4(Metallic, Specular, Roughness, Anisotropy);
    }
}
```

#### 3.3.5 BasePassShaderPrintDebug.usf — CS 读取 + ShaderPrint

```hlsl
#include "Common.ush"
#include "ShaderPrint.ush"

Texture2D<float4> GBufferCTexture;
StructuredBuffer<float4> DebugValueBuffer;

[numthreads(1, 1, 1)]
void BasePassShaderPrintDebugCS()
{
    FShaderPrintConfig Config = InitShaderPrintContextConfig();
    uint2 Resolution = Config.Resolution;
    if (Resolution.x == 0 || Resolution.y == 0) return;

    uint2 CenterCoord = Resolution / 2;

    float4 GBufferC = GBufferCTexture.Load(int3(CenterCoord, 0));
    float3 FinalBaseColor = GBufferC.rgb;

    float4 ProcessBaseColor = DebugValueBuffer.Load(0);
    float4 ProcessMaterial = DebugValueBuffer.Load(1);

    FShaderPrintContext Ctx = InitShaderPrintContext(true, float2(0.02, 0.02));

    Print(Ctx, TEXT("=== BasePass PS Process ==="), FontYellow);
    Newline(Ctx);
    Print(Ctx, TEXT("Pixel: "), FontWhite);
    Print(Ctx, (int)CenterCoord.x, FontWhite);
    Print(Ctx, TEXT(" , "), FontWhite);
    Print(Ctx, (int)CenterCoord.y, FontWhite);
    Newline(Ctx);
    Newline(Ctx);

    Print(Ctx, TEXT("--- PS Process (pre-DBuffer) ---"), FontCyan);
    Newline(Ctx);
    Print(Ctx, TEXT("  BC.r     = "), FontRed);
    Print(Ctx, ProcessBaseColor.r, FontRed, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  BC.g     = "), FontGreen);
    Print(Ctx, ProcessBaseColor.g, FontGreen, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  BC.b     = "), FontCyan);
    Print(Ctx, ProcessBaseColor.b, FontCyan, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  Metal    = "), FontWhite);
    Print(Ctx, ProcessMaterial.r, FontWhite, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  Specular = "), FontWhite);
    Print(Ctx, ProcessMaterial.g, FontWhite, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  Rough    = "), FontWhite);
    Print(Ctx, ProcessMaterial.b, FontWhite, 6);
    Newline(Ctx);
    Newline(Ctx);

    Print(Ctx, TEXT("--- GBuffer Final ---"), FontYellow);
    Newline(Ctx);
    Print(Ctx, TEXT("  R = "), FontRed);
    Print(Ctx, FinalBaseColor.r, FontRed, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  G = "), FontGreen);
    Print(Ctx, FinalBaseColor.g, FontGreen, 6);
    Newline(Ctx);
    Print(Ctx, TEXT("  B = "), FontCyan);
    Print(Ctx, FinalBaseColor.b, FontCyan, 6);
}
```

#### 3.3.6 DeferredShadingRenderer.cpp — CS 调度 + Clear

```cpp
// CS 类
class FBasePassShaderPrintDebugCS : public FGlobalShader
{
    DECLARE_GLOBAL_SHADER(FBasePassShaderPrintDebugCS);
    SHADER_USE_PARAMETER_STRUCT(FBasePassShaderPrintDebugCS, FGlobalShader);

    BEGIN_SHADER_PARAMETER_STRUCT(FParameters, )
        SHADER_PARAMETER_RDG_TEXTURE_SRV(Texture2D, GBufferCTexture)
        SHADER_PARAMETER_STRUCT_INCLUDE(ShaderPrint::FShaderParameters, ShaderPrintUniformBuffer)
        SHADER_PARAMETER_SRV(StructuredBuffer<float4>, DebugValueBuffer)
    END_SHADER_PARAMETER_STRUCT()

    static bool ShouldCompilePermutation(const FGlobalShaderPermutationParameters& Parameters)
    {
        return IsFeatureLevelSupported(Parameters.Platform, ERHIFeatureLevel::SM5);
    }
};
IMPLEMENT_GLOBAL_SHADER(FBasePassShaderPrintDebugCS, "/Engine/Private/BasePassShaderPrintDebug.usf", "BasePassShaderPrintDebugCS", SF_Compute);

// 在 ShaderPrint::BeginViews 前强制启用
ShaderPrint::SetEnabled(true);
ShaderPrint::BeginViews(GraphBuilder, Views);

// BasePass 前 Clear
ClearBasePassDebugValueBuffer(GraphBuilder.RHICmdList);

// BasePass 后调度 CS
#if !UE_BUILD_SHIPPING
{
    for (int32 ViewIndex = 0; ViewIndex < Views.Num(); ViewIndex++)
    {
        const FViewInfo& View = Views[ViewIndex];
        if (!IStereoRendering::IsAPrimaryView(View))
            continue;

        FGlobalShaderMap* GlobalShaderMap = GetGlobalShaderMap(FeatureLevel);
        TShaderMapRef<FBasePassShaderPrintDebugCS> ComputeShader(GlobalShaderMap);

        FBasePassShaderPrintDebugCS::FParameters* PassParameters = GraphBuilder.AllocParameters<FBasePassShaderPrintDebugCS::FParameters>();
        PassParameters->GBufferCTexture = GraphBuilder.CreateSRV(FRDGTextureSRVDesc(SceneTextures.GBufferC));
        PassParameters->DebugValueBuffer = GetBasePassDebugValueSRV();
        ShaderPrint::SetParameters(GraphBuilder, View.ShaderPrintData, PassParameters->ShaderPrintUniformBuffer);

        FComputeShaderUtils::AddPass(
            GraphBuilder,
            RDG_EVENT_NAME("BasePassShaderPrintDebug"),
            ComputeShader,
            PassParameters,
            FIntVector(1, 1, 1));
    }
}
#endif
```

---

## 四、验证结果

### 4.1 编译

- CPP 增量编译：21 秒
- 无编译错误
- ShaderPrint CS 编译：0.06 秒

### 4.2 运行

- 编辑器正常启动（无 crash）
- MCP 连接成功
- 测试地图：`/Game/Maps/Battle/NordlandSection`

### 4.3 截图输出

```
=== BasePass PS Process ===
Pixel:  640 , 357

--- PS Process (pre-DBuffer) ---
  BC.r     = 0.273
  BC.g     = 0.534
  BC.b     = 0.682
  Metal    = 0.000
  Specular = 0.500
  Rough    = 0.760

--- GBuffer Final ---
  R = 0.273
  G = 0.534
  B = 0.682
```

PS 过程量和 GBuffer 最终值都正确显示（本场景无 DBuffer 贴花，两组值相同）。

---

## 五、快速排查 Checklist

### 5.1 Mesh Material Shader 参数绑定

- [ ] 确认 Shader 类型：Global Shader（Pass-level 绑定）还是 Mesh Material Shader（Per-draw 绑定）
- [ ] Mesh Material Shader 的参数绑定必须走 `GetShaderBindings`：
  - [ ] Shader 类中声明 `LAYOUT_FIELD`
  - [ ] 构造函数中 `Bind(Initializer.ParameterMap, TEXT("VariableName"))`
  - [ ] `GetShaderBindings` 中 `ShaderBindings.Add(Field, Value)`
  - [ ] `TBasePassShaderElementData` 中加数据字段
  - [ ] ShaderElementData 创建处填充数据
- [ ] Per-draw 绑定只能用持久 RHI 资源（`FRHIBufferRef`、`FUnorderedAccessViewRHIRef`、`TUniformBufferRef`），不能用 RDG-managed 资源（`FRDGBufferRef`）

### 5.2 ShaderPrint 使用

- [ ] `ShaderPrint::SetEnabled(true)` 必须在 `ShaderPrint::BeginViews` 之前调用
- [ ] Global Shader 使用 `SHADER_PARAMETER_STRUCT_INCLUDE(ShaderPrint::FShaderParameters, ...)` + `ShaderPrint::SetParameters()`
- [ ] Mesh Material Shader 不能直接使用 ShaderPrint 框架——需要自定义持久 buffer 桥接
- [ ] Typed Buffer 的 SRV/UAV 必须指定 `PF_R32_UINT`（或对应格式），否则崩溃 `Format cannot be unknown for typed buffers`

### 5.3 崩溃排查

- [ ] `Missing uniform buffer at slot N, stage SF_Pixel` → Pass-level RDG 参数未绑定到 Mesh Material Shader，改用 per-draw `GetShaderBindings`
- [ ] `Format cannot be unknown for typed buffers` → `GraphBuilder.CreateSRV`/`CreateUAV` 需指定 `PF_R32_UINT`
- [ ] WorldPartition 崩溃（`ActiveContext == &DefaultContext`）→ 与渲染改动无关，换不使用 WorldPartition 的地图测试

---

## 六、扩展用法

### 6.1 捕获任意位置的过程量

在 `BasePassPixelShader.usf` 的 `FPixelShaderInOut_MainPS` 中，任意位置加：

```hlsl
// 捕获 DBuffer 修改前的 BaseColor
DebugValueBuffer[0] = float4(BaseColor, 1.0);

ApplyDBufferData(DBufferData, MaterialParameters.WorldNormal, SubsurfaceColor, Roughness, BaseColor, Metallic, Specular);

// 捕获 DBuffer 修改后的 BaseColor
DebugValueBuffer[2] = float4(BaseColor, 1.0);
```

CS 中读取 `DebugValueBuffer.Load(0)` 和 `DebugValueBuffer.Load(2)` 对比前后差异。

### 6.2 捕获多个像素

```hlsl
if (all(PixelCoord == uint2(640, 360)))
    DebugValueBuffer[0] = float4(BaseColor, 1.0);
else if (all(PixelCoord == uint2(320, 180)))
    DebugValueBuffer[10] = float4(BaseColor, 1.0);
```

### 6.3 增大字体

```
r.ShaderPrint.FontSize 12  // 默认 8
```

---

## 七、相关参考

- **知乎原文**：[UE5 Shader Print系统](https://zhuanlan.zhihu.com/p/637929634) — AydenLee
- **Epic 官方 API**：
  - [FShaderPrintData](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/Renderer/FShaderPrintData)
  - [FShaderPrintSetup](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/Renderer/FShaderPrintSetup)
  - [FShaderPrintCommonParameters](https://dev.epicgames.com/documentation/en-us/unreal-engine/API/Runtime/Renderer/FShaderPrintCommonParameters)
- **CSDN 中文教程**：[UE5的渲染Debug技巧](https://blog.csdn.net/qq_29523119/article/details/149865869)
- **UE5 Shader Debugging Workflows**：[Epic 官方文档](https://dev.epicgames.com/documentation/en-us/unreal-engine/shader-debugging-workflows-unreal-engine)
- **源码位置**（UE5EA DevTest 引擎）：
  - `D:\GR_DevTest\UE5EA\Engine\Shaders\Private\ShaderPrintCommon.ush`
  - `D:\GR_DevTest\UE5EA\Engine\Shaders\Private\ShaderPrint.ush`
  - `D:\GR_DevTest\UE5EA\Engine\Shaders\Private\ShaderPrintDraw.usf`
  - `D:\GR_DevTest\UE5EA\Engine\Source\Runtime\Renderer\Private\ShaderPrint.cpp`
  - `D:\GR_DevTest\UE5EA\Engine\Source\Runtime\Renderer\Private\BasePassRendering.h`
  - `D:\GR_DevTest\UE5EA\Engine\Source\Runtime\Renderer\Private\BasePassRendering.inl`
  - `D:\GR_DevTest\UE5EA\Engine\Source\Runtime\Renderer\Private\BasePassRendering.cpp`
  - `D:\GR_DevTest\UE5EA\Engine\Shaders\Private\BasePassPixelShader.usf`
  - `D:\GR_DevTest\UE5EA\Engine\Shaders\Private\BasePassShaderPrintDebug.usf`（新建）
  - `D:\GR_DevTest\UE5EA\Engine\Source\Runtime\Renderer\Private\DeferredShadingRenderer.cpp`
