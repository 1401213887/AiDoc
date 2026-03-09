# UE5 Runtime Virtual Texture — 32位 WorldHeight 烘焙精度损失分析报告

## 一、问题描述

在 UE5 的 Runtime Virtual Texture (RVT) 系统中，**32位 WorldHeight** 模式下，离线烘焙 (Bake) 的 Streaming VT 数据与实时渲染的数据之间存在精度误差。而 **16位 WorldHeight** 模式下没有这个问题。

### 症状表现
- 烘焙后的 VT tile 数据与实时渲染 tile 数据不一致
- 误差表现为高度值的细微偏移，在大世界场景中可观察到明显的 artifact

### 核心原因
32位 WorldHeight 使用 `PF_R32_FLOAT`（R32F）格式存储 packed height 值，而 VT 离线烘焙管线中存在 **两处** 导致 R32F 数据被错误降级为低精度格式的 bug。

---

## 二、数据流路径对比

### 实时渲染路径（无精度损失）
```
Shader渲染 → PF_R32_FLOAT RenderTarget → RDG Copy → VT物理纹理
```

### 离线烘焙路径（存在精度损失）
```
Shader渲染 → PF_R32_FLOAT RenderTarget → GPU ReadBack → CPU像素数据
→ InitializeStreamingTexture (TSF_R32F) → UVirtualTexture2D::Source
→ PostEditChange → DDC纹理编译管线
  → VT Build Step 1 (BuildLayerBlocks): 源数据 → 中间格式 mip chain
  → VT Build Step 2 (BuildBlockTiles): mip chain → tile编码
→ VT Streaming Texture → Transcode (RawGPU memcpy) → VT物理纹理
```

**精度损失发生在 DDC 纹理编译管线中。**

---

## 三、已发现的精度损失点及修复

### 修复1：`CompressionNone` 未对 `PF_R32_FLOAT` 设置

**文件**: `Engine/Source/Runtime/Engine/Private/Components/RuntimeVirtualTextureComponent.cpp`
**函数**: `GetLayerFormatSettings` (约第437行)

**问题**: `GetLayerFormatSettings` 函数在设置 `FTextureFormatSettings` 时，对 `PF_G16`（16位 WorldHeight）设置了 `CompressionNone = true`，但遗漏了 `PF_R32_FLOAT`（32位 WorldHeight）。

```cpp
// 修复前：
if (LayerFormat == PF_G16)
{
    OutSettings.CompressionNone = true;
}

// 修复后：
if (LayerFormat == PF_G16 || LayerFormat == PF_R32_FLOAT)
{
    OutSettings.CompressionNone = true;
}
```

**影响分析**: 经深入分析，对于 `TC_SingleFloat` + `R32F` 格式，`CompressionNone` 的实际影响有限。因为在 `Texture.cpp` 的格式选择逻辑中：
- `TextureFormatName = "R32F"`（由 `TC_SingleFloat` 决定）
- `bTextureFormatNameIsCompressed = false`（R32F 不在压缩格式列表中）
- `bNoCompression` 的 fallback 条件 `(bNoCompression && bTextureFormatNameIsCompressed)` 为 `false`

因此此修复更多是语义正确性的保障，而非直接的精度修复。

---

### 修复2（关键修复）：`GetVirtualTextureBuildIntermediateFormat` 将 R32F 降级为 RGBA16F

**文件**: `Engine/Source/Developer/TextureBuildUtilities/Private/TextureBuildUtilities.cpp`
**函数**: `GetVirtualTextureBuildIntermediateFormat` (约第115行)

**问题**: VT 构建管线的 Step 1（`BuildLayerBlocks`）使用 `GetVirtualTextureBuildIntermediateFormat` 来决定中间存储格式。修复前，该函数对 `R32F` 格式没有专门处理，导致 R32F 被 fallback 为 `RGBA16F`（16位半精度浮点）。

**精度损失链路**:
```
R32F源数据 → 中间格式RGBA16F（精度从32位float降至16位half）
→ Step 2 tile编码使用降级后的数据
```

**修复方案**:
```cpp
// 在函数中添加R32F的专门处理
static FName NameR32F(TEXT("R32F"));
if (TextureFormatName == NameR32F)
{
    return ERawImageFormat::R32F;
}
```

**这是导致32位 WorldHeight 烘焙精度损失的根本原因。** R32F（32位浮点，约7位有效数字精度）被降级为 RGBA16F（16位半精度浮点，约3位有效数字精度），导致 packed height 值丢失大量精度。而16位 WorldHeight 使用 `PF_G16` 格式，其中间格式本身就是 `RGBA16F` 或 `G8`，不存在降级问题。

---

## 四、已验证无精度损失的环节

在排查过程中，对整条烘焙管线的每个环节进行了逐一验证：

| 环节 | 格式 | 结论 |
|------|------|------|
| GPU渲染输出 | PF_R32_FLOAT RT | ✅ 无损 |
| GPU ReadBack (MapStagingSurface) | PF_R32_FLOAT → float* | ✅ 无损 (TCopyTile memcpy) |
| InitializeStreamingTexture | TSF_R32F | ✅ 无损 |
| FTextureSource::InitLayered | TSF_R32F 数据存储 | ✅ 无损 (FSharedBuffer::Clone) |
| **VT Build Step 1 中间格式** | **R32F→RGBA16F（修复前）/ R32F（修复后）** | **⚠️ 修复前有精度损失** |
| LinearizeToWorkingColorSpace | R32F→RGBA32F（保持float精度） | ✅ 无损 |
| Mip生成 (SimpleAverage) | RGBA32F上操作 | ✅ 无损（R通道float精度不变） |
| CompressImage (DoCompressImageSimple) | RGBA32F→R32F (CopyTo取R通道) | ✅ 无损 |
| VT Build Step 2 tile编码 | R32F→R32F | ✅ 无损 |
| VT Codec选择 | RawGPU (memcpy) | ✅ 无损 |
| Transcode上传 | RawGPU直接拷贝 | ✅ 无损 |
| VT物理纹理格式 | PF_R32_FLOAT | ✅ 与实时渲染一致 |
| FVector2D→FVector2f (PackHeight参数) | double→float | ✅ 烘焙/实时路径一致 |
| WorldBounds | 两个路径使用同一Bounds | ✅ 一致 |
| RenderPages vs RenderPagesStandAlone | 仅bAllowCachedMeshDrawCommands不同 | ✅ 不影响渲染结果 |
| AdjustImageColors等后处理 | TC_SingleFloat下不启用 | ✅ 不影响 |
| bSupportFilteredFloat32Textures (PC) | true | ✅ R32F不会被降级为R16F |
| FinalizeVirtualTextureLayerFormat (PC) | 直接返回原格式 | ✅ 不修改 |

---

## 五、VT Build 详细流程分析

### Step 1: BuildLayerBlocks
```
输入: 完整分辨率的R32F源纹理
处理:
  1. MipGenSettings强制改为TMGS_SimpleAverage → 需要生成mip
  2. GenerateCount > 0 → bLinearize = true
  3. LinearizeToWorkingColorSpace: R32F → RGBA32F（float精度不变）
  4. Mip生成在RGBA32F上进行
  5. TextureFormatUncompressed::CompressImage:
     - TBSettings.TextureFormatName = LayerData.FormatName = "R32F"
     - DoCompressImageSimple: RGBA32F.CopyTo(R32F) → 取R通道
  6. BlockData.Mips存储为R32F格式
输出: 各block的mip chain（R32F格式FImage数组）
```

### Step 2: BuildBlockTiles
```
输入: Step 1输出的block mip chain
处理:
  1. 从block中切割出tile大小的FImage（R32F格式）
  2. bNeedLinearize = false（CanAcceptNonF32Source = true）
  3. bLinearize = false（GenerateCount = 0, bNeedLinearize = false）
  4. 数据直接Image.Swap(Mip)，不做任何格式转换
  5. TextureFormatUncompressed::CompressImage:
     - TBSettings.TextureFormatName = LayerData.TextureFormatName = "R32F"
     - 输入已经是R32F → 直接Move数据
  6. Codec = EVirtualTextureCodec::RawGPU
输出: 编码后的tile数据（R32F原始GPU数据）
```

---

## 六、涉及的关键文件清单

| 文件路径 | 作用 |
|---------|------|
| `Engine/Source/Runtime/Engine/Private/Components/RuntimeVirtualTextureComponent.cpp` | RVT组件，GetLayerFormatSettings、InitializeStreamingTexture |
| `Engine/Source/Developer/TextureBuildUtilities/Private/TextureBuildUtilities.cpp` | GetVirtualTextureBuildIntermediateFormat（**核心修复点**） |
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureDataBuilder.cpp` | VT构建主流程：Build、BuildLayerBlocks、BuildBlockTiles |
| `Engine/Source/Developer/TextureCompressor/Private/TextureCompressorModule.cpp` | BuildTexture、BuildTextureMips、LinearizeToWorkingColorSpace |
| `Engine/Source/Developer/TextureFormatUncompressed/Private/TextureFormatUncompressed.cpp` | CompressImage、DoCompressImageSimple |
| `Engine/Source/Runtime/ImageCore/Private/ImageCore.cpp` | FImageCore::CopyImage（R32F↔RGBA32F转换） |
| `Engine/Source/Runtime/Renderer/Private/VT/RuntimeVirtualTextureRender.cpp` | RenderPage、RenderPagesStandAlone、FRenderGraphSetup |
| `Engine/Source/Editor/VirtualTexturingEditor/Private/RuntimeVirtualTextureBuildStreamingMips.cpp` | BuildStreamedMips、CopyTile（GPU ReadBack） |
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureTranscodeCache.cpp` | Transcode tile解码（RawGPU=memcpy） |
| `Engine/Source/Runtime/Engine/Private/VT/VirtualTextureBuilder.cpp` | BuildVirtualTexture2D（触发DDC编译） |
| `Engine/Source/Runtime/Engine/Private/Texture.cpp` | 纹理格式选择、bSupportFilteredFloat32Textures |
| `Engine/Source/Runtime/Engine/Private/VT/RuntimeVirtualTexture.cpp` | GetLayerFormat、ProducerDescription |

---

## 七、修复后注意事项

1. **清除 DDC 缓存**: 修复后必须清除 Derived Data Cache，否则旧的构建数据（包含RGBA16F中间格式的降级数据）仍会被使用
2. **重新烘焙**: 需要重新执行 VT Bake 操作生成新的 Streaming Mips
3. **平台差异**: 在 Android/iOS 等不支持 `bSupportFilteredFloat32Textures` 的平台上，R32F 可能被进一步降级为 R16F（非本次修复范围）

---

## 八、总结

32位 WorldHeight 烘焙精度损失的**根本原因**是 `GetVirtualTextureBuildIntermediateFormat` 函数缺少对 `R32F` 格式的专门处理，导致 VT 构建 Step 1 中的中间存储格式从 `R32F`（32位浮点）降级为 `RGBA16F`（16位半精度浮点），丢失了约一半的有效精度位数。

16位 WorldHeight（`PF_G16`）不受此问题影响，因为其源精度本身就不超过16位，中间格式的选择不会导致额外的精度损失。
