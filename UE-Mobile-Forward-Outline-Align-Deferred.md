# UE-Mobile-Forward-描边未对齐Deferred-通道语义修复

> Mobile Forward 路径 toon 角色描边用 `dot(rgb,1)` 将三通道独立遮罩求和当边缘判据，本质错误。修复对齐 Deferred：拆 `.a` 通道（ToonOutlineMask）+ 统一纹理/采样器来源。

---

## 一、问题链条

1. C++ 层 `RenderForward` 未将 `MobileCharFeatureTexture` 传给 BasePass → `ScreenSpaceOutline` 字段为空 → shader 永远拿 fallback 纹理（`SystemTextures.White`）→ `dot(1,1,1)=3>0` → `bIsEdge=1` → 整角色被涂黑 
2. Shader 层 `MobileBasePassPixelShader.usf:1511` 用 `step(0.001, dot(CharacterOutlineColor, 1))` 作边缘判定，而 `CharacterOutlineColor` 是 `ScreenOutlineTexture.rgb`。该 RT 的三个通道是**三个互无关系的独立遮罩**（`.r`=SceneRimLight、`.g`=SceneOutline、`.b`=ToonRimLight），求和当边缘语义完全错误
3. Forward 用 `ScreenOutlineSampler`（Bilinear），Deferred 用 `MobileCharacterOutlineSampler`（Point）。描边遮罩是二值边缘，Bilinear 导致边缘模糊半像素、描边变粗

## 二、修复方案

### C++ 层（`MobileShadingRenderer.cpp`）

`RenderForward` 中创建 `MobileBasePassTextures` 时追加 ScreenSpaceOutline 绑定，对齐 `RenderDeferredSinglePass` L2387 的行为：

```cpp
FMobileBasePassTextures MobileBasePassTextures{};
MobileBasePassTextures.DBufferTextures = DBufferTextures;
#pragma region Engine ZXB
// [ZXB Fix] Forward 路径也要把 ScreenSpaceOutline RT 传给 BasePass
MobileBasePassTextures.ScreenSpaceOutline = SceneTextures.MobileCharFeatureTexture.Target;
#pragma endregion
```

### Shader 层（`MobileBasePassPixelShader.usf`）

描边块从三通道求和改为拆 `.a` 通道，纹理+采样器对齐 Deferred：

```hlsl
// #pragma region Engine ZXB
// [ZXB Fix] Forward toon 描边完全重写，对齐 Deferred (MobileDeferredShading.usf:248-253+409-416)
{
    float2 OutlineUV = SvPositionToBufferUV(SvPosition);
    float4 OutlineRT = MobileSceneTextures.MobileCharacterOutline.SampleLevel(
        MobileSceneTextures.MobileCharacterOutlineSampler, OutlineUV, 0);
    float ToonOutlineMask = OutlineRT.a;
    Color = lerp(Color, float3(0, 0, 0), ToonOutlineMask);
}
// #pragma endregion
```

## 三、对齐明细

| 项 | Deferred（`MobileDirectionalLightPS`） | Forward（改后） | 对齐 |
|---|---|---|---|
| 纹理 | `MobileSceneTextures.MobileCharacterOutline` | 同 | ✅ |
| 采样器 | `MobileCharacterOutlineSampler`（Point） | 同 | ✅ |
| 通道语义 | `.a` = ToonOutlineMask | `.a` = ToonOutlineMask | ✅ |
| 描边色 | `half3(0,0,0)` 黑色 | `float3(0,0,0)` 黑色 | ✅ |
| C++ RT 来源 | `MobileCharFeatureTexture.Target` | 同 | ✅ |
| 门控方式 | 运行时 `ShadingModelIsToonCharacter()` | 编译期 `MATERIAL_SHADINGMODELS_TOON_CHARACTER` | ✅ 各取最优 |

## 四、不影响 Deferred BasePass

Forward shader 的描边块同时在 Forward 和 Deferred base pass 中编译（宏 `MATERIAL_SHADINGMODELS_TOON_CHARACTER` 非 `MOBILE_USE_GBUFFER`）。但 Deferred base pass 期间 `MobileCharFeatureTexture` 尚未产出（`RenderPreOutlinePass` 在 base pass 之后执行），因此 `MobileCharacterOutline` 在该阶段为 `SystemTextures.Black` → `OutlineRT.a = 0` → `ToonOutlineMask = 0` → `lerp(Color, 黑, 0) = Color`，行为与旧代码完全一致。真正的描边由 Deferred **Lighting Pass**（`MobileDirectionalLightPS:409-416`）在 RT 产出后应用。

## 五、通道语义参考（`MobileToonOutline.usf:126`）

```hlsl
OutColor = float4(SceneRimLight.r, SceneOutline.g, ToonRimLight.b, ToonOutline.a);
```

该 RT 由深度拉普拉斯算子（`CalcDepthLaplacian`）计算边缘，对角色只输出 `.a` 通道为 1 的边缘像素。

## 六、受影响文件

| 文件 | P4 状态 | 改动行 |
|---|---|---|
| `MobileShadingRenderer.cpp` | edit default change | L1992-1998 |
| `MobileBasePassPixelShader.usf` | edit default change | L1511-1521 |
