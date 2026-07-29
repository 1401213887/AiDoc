# UE5 LightAccumulator_AddSplit 函数作用

> 解析 `Engine/Shaders/Private/LightAccumulator.ush:58` 中 `LightAccumulator_AddSplit` 的设计意图、输入输出及与 `LightAccumulator_Add` 的区别。

---

## 一、问题定位

在分析 UE5 光照累加器（Light Accumulator）时，需要明确 `LightAccumulator_AddSplit` 与 `LightAccumulator_Add` 的差异：

- `LightAccumulator_Add`：只接受合并后的 `TotalLight`，无法区分漫反射与高光。
- `LightAccumulator_AddSplit`：分别接受 `DiffuseTotalLight` 和 `SpecularTotalLight`，支持更细粒度的光照累加。

## 二、函数签名

```hlsl
void LightAccumulator_AddSplit(
    inout FLightAccumulator In,
    float3 DiffuseTotalLight,
    float3 SpecularTotalLight,
    float3 ScatterableLight,
    float3 CommonMultiplier,
    const bool bNeedsSeparateSubsurfaceLightAccumulation
)
```

## 三、核心作用

将一次光照贡献（漫反射 + 高光 + 可散射次表面光）按公共乘数累加到 `FLightAccumulator` 中。

### 1. 总光照与亮度更新

```hlsl
In.TotalLight += (DiffuseTotalLight + SpecularTotalLight) * CommonMultiplier;
In.TotalLightLuminance += Luminance((DiffuseTotalLight + SpecularTotalLight) * CommonMultiplier);
```

- `TotalLight`：最终写入 SceneColor 的 RGB 总光照。
- `TotalLightLuminance`：总光照亮度，用于后续 Tone Mapping 或调试显示。

### 2. 次表面散射通道处理

仅在 `bNeedsSeparateSubsurfaceLightAccumulation` 为真时进入分支：

| `SUBSURFACE_CHANNEL_MODE` | 行为 |
| --- | --- |
| `1` | 累加 `ScatterableLightLuma`（亮度模式），受 Checkerboard 次表面渲染开关影响 |
| `2` | 直接累加 `ScatterableLight`（RGB 模式） |
| 其他 | 不处理 |

### 3. 漫反射/高光分离保存

```hlsl
In.TotalLightDiffuse += DiffuseTotalLight * CommonMultiplier;
In.TotalLightSpecular += SpecularTotalLight * CommonMultiplier;
```

这两个字段主要用于：

- Alpha/半透明材质：高光需要按 `1/opacity` 补偿，不能与漫反射混在一起。
- 可视化或后处理：需要单独提取漫反射或高光贡献。

## 四、与 LightAccumulator_Add 的关系

```hlsl
void LightAccumulator_Add(inout FLightAccumulator In, float3 TotalLight, float3 ScatterableLight, float3 CommonMultiplier, const bool bNeedsSeparateSubsurfaceLightAccumulation)
{
    LightAccumulator_AddSplit(In, TotalLight, 0.0f, ScatterableLight, CommonMultiplier, bNeedsSeparateSubsurfaceLightAccumulation);
}
```

- `LightAccumulator_Add` 是 `LightAccumulator_AddSplit` 的简化版。
- 当调用方不需要区分漫反射/高光时，直接传入合并后的 `TotalLight` 并把 `SpecularTotalLight` 置 `0`。

## 五、使用场景

- **延迟渲染**：在 `DeferredLightingCommon.ush` 或各光照 Pass 中，对每个光源调用，将灯光贡献累加。
- **前向渲染**：移动端/桌面前向路径中，在 Base Pass 或光照 Pass 中累加直接光/IBL。
- **半透明渲染**：需要分离 Diffuse 与 Specular 以便按 Alpha 补偿。

## 六、快速排查 Checklist

- [ ] 确认调用方是否有明确的漫反射/高光拆分需求；如无，优先使用 `LightAccumulator_Add`。
- [ ] 确认 `CommonMultiplier` 是否包含阴影、距离衰减、投影掩码等所有公共因子。
- [ ] 若涉及次表面散射，确认 `SUBSURFACE_CHANNEL_MODE` 与 `bNeedsSeparateSubsurfaceLightAccumulation` 是否匹配。
- [ ] 调试 SceneColor 过曝时，可检查 `TotalLightDiffuse` / `TotalLightSpecular` 是否被异常放大。

## 七、相关参考

- 文件：`UE5EA/Engine/Shaders/Private/LightAccumulator.ush`
- 相关结构：`FLightAccumulator`、`FDeferredLightingSplit`
- 相关函数：`LightAccumulator_Add`、`LightAccumulator_GetResult`
