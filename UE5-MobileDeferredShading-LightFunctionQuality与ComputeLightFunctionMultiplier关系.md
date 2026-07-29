# UE5 Mobile Deferred Shading 中 LightFunctionQuality 与 ComputeLightFunctionMultiplier 的关系

> `r.LightFunctionQuality <= 0` 时，Mobile Deferred 光函数在效果上等价于无操作（乘数恒为 1.0），但 shader 代码仍会执行，不会节省 ALU/texture 开销。

---

## 一、问题定位流程

1. **定位函数实现**：`UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf:55`
   - `ComputeLightFunctionMultiplier` 内部通过 `#if USE_LIGHT_FUNCTION` 控制是否采样光函数。
   - 若 `USE_LIGHT_FUNCTION` 为 0，直接返回 `1.0`。

2. **定位宏定义**：`UE5EA/Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp:279 / :511`
   - `USE_LIGHT_FUNCTION` 在 `ModifyCompilationEnvironment` 中设置。
   - 取值仅取决于 `Parameters.MaterialParameters.bIsDefaultMaterial`：
     - 默认材质 → 0
     - 非默认材质 → 1
   - **不读取 `r.LightFunctionQuality`**。

3. **定位 CVar 作用**：`UE5EA/Engine/Source/Runtime/Core/Private/HAL/ConsoleManager.cpp:4225`
   - `r.LightFunctionQuality` 默认值为 2。
   - 注释含义：
     - `<=0`：off（最快）
     - `1`：低质量
     - `2`：正常质量（默认）
     - `3`：高质量

4. **定位 CVar 到 ShowFlag 的映射**：`UE5EA/Engine/Source/Runtime/Engine/Private/ShowFlags.cpp:465`
   - 当 `r.LightFunctionQuality <= 0` 时，设置 `EngineShowFlags.LightFunctions = false`。

5. **定位运行时绑定逻辑**：`MobileDeferredShadingPass.cpp:849 / :1490`
   - 若 `EngineShowFlags.LightFunctions` 为 false，则不获取自定义光函数材质代理。
   - `GetLightMaterial` 回退到 `UMaterial::GetDefaultMaterial(MD_LightFunction)`（纯白默认光函数材质）。

6. **确认最终效果**：默认材质返回 `(1,1,1)`，`ComputeLightFunctionMultiplier` 中取 `LightFunction.g = 1.0`，最终光照乘数为 1.0。

---

## 二、根因分析

`ComputeLightFunctionMultiplier` 与 `r.LightFunctionQuality` 之间是**间接的运行时关系**，不是编译期直接控制：

| 层级 | 控制对象 | 是否受 `r.LightFunctionQuality` 影响 | 说明 |
|------|---------|-----------------------------------|------|
| 编译期 | `USE_LIGHT_FUNCTION` 宏 | 否 | 由材质是否 DefaultMaterial 决定 |
| 运行时 | `EngineShowFlags.LightFunctions` | 是 | `<=0` 时关闭 |
| 运行时 | 光函数材质代理 | 是 | ShowFlag 关闭时使用默认纯白材质 |
| Shader | `ComputeLightFunctionMultiplier` 执行 | 否（代码仍会跑） | 默认材质使输出恒为 1.0 |

因此，关闭 `r.LightFunctionQuality` 只能让**效果**消失，不能让**开销**消失。

---

## 三、详细技术原理

### 3.1 Shader 侧函数逻辑

```hlsl
// MobileDeferredShading.usf:55
half ComputeLightFunctionMultiplier(float3 TranslatedWorldPosition)
{
#if USE_LIGHT_FUNCTION	
	float4 LightVector = mul(float4(TranslatedWorldPosition, 1.0), TranslatedWorldToLight);
	LightVector.xyz /= LightVector.w;

	half3 LightFunction = GetLightFunctionColor(LightVector.xyz, TranslatedWorldPosition);
	half GreyScale = LightFunction.g;        // GR Mobile 改为直接取绿色通道
	// ... 距离 fade、shadow fade ...
	return GreyScale; 
#else
	return 1.0;
#endif
}
```

### 3.2 C++ 侧宏定义

```cpp
// MobileDeferredShadingPass.cpp:279
OutEnvironment.SetDefine(TEXT("USE_LIGHT_FUNCTION"), Parameters.MaterialParameters.bIsDefaultMaterial ? 0 : 1);
```

### 3.3 CVar 定义

```cpp
// ConsoleManager.cpp:4225
static TAutoConsoleVariable<int32> CVarLightFunctionQuality(
	TEXT("r.LightFunctionQuality"),
	2,
	TEXT("Defines the light function quality which allows to adjust for quality or performance.\n"
		 "<=0: off (fastest)\n"
		 "  1: low quality (e.g. half res with blurring, not yet implemented)\n"
		 "  2: normal quality (default)\n"
		 "  3: high quality (e.g. super-sampled or colored, not yet implemented)"),
	ECVF_Scalability | ECVF_RenderThreadSafe);
```

### 3.4 ShowFlag 控制

```cpp
// ShowFlags.cpp:465
static const auto ICVar = IConsoleManager::Get().FindTConsoleVariableDataInt(TEXT("r.LightFunctionQuality"));
if(ICVar->GetValueOnGameThread() <= 0)
{
    EngineShowFlags.SetLightFunctions(false);
}
```

### 3.5 运行时材质回退

```cpp
// MobileDeferredShadingPass.cpp:630
if (MaterialProxy)
{
    const FMaterial* Material = MaterialProxy->GetMaterialNoFallback(ERHIFeatureLevel::ES3_1);
    if (Material && Material->IsLightFunction())
    {
        // 使用自定义光函数材质
        ...
        return;
    }
}

// use default material
OutLightMaterial.Material = DefaultLightMaterial.Material;
OutLightMaterial.MaterialProxy = DefaultLightMaterial.MaterialProxy;
```

默认材质初始化：

```cpp
// MobileDeferredShadingPass.cpp:1724
FCachedLightMaterial DefaultMaterial;
DefaultMaterial.MaterialProxy = UMaterial::GetDefaultMaterial(MD_LightFunction)->GetRenderProxy();
DefaultMaterial.Material = DefaultMaterial.MaterialProxy->GetMaterialNoFallback(ERHIFeatureLevel::ES3_1);
```

---

## 四、修复方案 / 使用建议

- 若目的是**关闭光函数效果**：设置 `r.LightFunctionQuality 0` 即可，画面表现等价于无 Light Function。
- 若目的是**节省 GPU 开销**（跳过 `ComputeLightFunctionMultiplier` 的 ALU/texture 采样）：此 CVar 做不到，因为 `USE_LIGHT_FUNCTION` 宏未变，shader 分支仍会执行。
- 若需要真正剔除光函数代码，需要修改 `MobileDeferredShadingPass.cpp` 中 `USE_LIGHT_FUNCTION` 的定义逻辑，使其在运行时条件下也能设为 0，或新增一个 permutation。

---

## 五、快速排查 Checklist

- [ ] 确认当前材质是否为 DefaultMaterial（影响 `USE_LIGHT_FUNCTION` 编译值）
- [ ] 确认 `r.LightFunctionQuality` 当前值（`<=0` 会关闭 ShowFlag）
- [ ] 确认 `EngineShowFlags.LightFunctions` 是否被关闭
- [ ] 确认 `LightFunctionMaterialProxy` 是否为 nullptr 并回退到默认材质
- [ ] 若要省开销，检查 shader 是否真正跳过了 `ComputeLightFunctionMultiplier` 分支

---

## 六、相关参考

- `d:/GR_DevTest/UE5EA/Engine/Shaders/Private/MobileDeferredShading.usf:55`
- `d:/GR_DevTest/UE5EA/Engine/Shaders/Private/LightFunctionCommon.ush:25`
- `d:/GR_DevTest/UE5EA/Engine/Source/Runtime/Renderer/Private/MobileDeferredShadingPass.cpp:279 / :511 / :849 / :1490 / :1724`
- `d:/GR_DevTest/UE5EA/Engine/Source/Runtime/Engine/Private/ShowFlags.cpp:465`
- `d:/GR_DevTest/UE5EA/Engine/Source/Runtime/Core/Private/HAL/ConsoleManager.cpp:4225`
