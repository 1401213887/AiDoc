
# RVT Normal 精度优化：BC5 双通道独立端点方案

## 一、基本信息

| 字段 | 内容 |
|------|------|
| **Changelist** | 811253 |
| **提交者** | zhangjianguo（张建国） |
| **日期** | 2026/02/27 10:33:46 |
| **描述** | 提升RVT Normal精度，解决Neoring地面马赛克高光的表现 |
| **关联TAPD** | [Bug #1170022](https://www.tapd.cn/68880148/s/4236361) |
| **场景** | NeoRing 野区（2×2地图）地面 |

---

## 二、问题背景

NeoRing 场景的地面使用 Runtime Virtual Texture (RVT) 通过 `Super_BaseColor_Normal_Specular_Mask` 类型来混合和渲染地表材质。在此次优化前，该类型使用的纹理层布局为 **BC5 + BC3 + BC1**：

- **Layer 0 (BC5)**：存储 BaseColor.RG
- **Layer 1 (BC3/DXT5)**：存储 Normal.XYZ + BaseColor.B（Alpha通道）
- **Layer 2 (BC1/DXT1)**：存储 Specular + Roughness + Mask

这种方案中，Normal 的三个分量 XYZ 被打包进 BC3 的 RGB 三通道。BC3 对 RGB 部分使用的是与 BC1 相同的端点插值压缩（每个 4×4 block 只有 2 个 RGB565 格式端点 + 16 个 2-bit 索引），三通道共享同一对端点进行插值。当地面大面积平坦、法线值高度相近时，BC3 的端点插值量化误差会在高光反射下被放大，表现为明显的**马赛克/色块化**伪影。

---

## 三、优化方案

将 Normal 的存储格式从 **BC3（RGB三通道共享端点插值）** 优化为 **BC5（双通道独立端点插值）**，并重新分配各层数据布局。

### 3.1 数据布局对比

```
优化前 (BC5 + BC3 + BC1):
┌─────────────────────┬──────────────────────────┬────────────────────────────┐
│ Layer 0: BC5 (128b) │ Layer 1: BC3/DXT5 (128b) │ Layer 2: BC1/DXT1 (64b)    │
│  R: BaseColor.R     │  R: Normal.X             │  R: Specular               │
│  G: BaseColor.G     │  G: Normal.Y             │  G: Roughness              │
│                     │  B: Normal.Z             │  B: Mask                   │
│                     │  A: BaseColor.B          │  (无Alpha)                 │
├─────────────────────┴──────────────────────────┴────────────────────────────┤
│ 每block总开销: 128 + 128 + 64 = 320 bits                                   │
└─────────────────────────────────────────────────────────────────────────────┘

                                    ↓ 优化为 ↓

优化后 (BC5 + BC5 + BC3):
┌─────────────────────┬──────────────────────────┬────────────────────────────┐
│ Layer 0: BC5 (128b) │ Layer 1: BC5 (128b)      │ Layer 2: BC3/DXT5 (128b)   │
│  R: BaseColor.R     │  R: Normal.X (独立端点)  │  R: Specular               │
│  G: BaseColor.G     │  G: Normal.Y (独立端点)  │  G: Roughness              │
│                     │  Z: sqrt(1-X²-Y²) 重建   │  B: Mask                   │
│                     │                          │  A: BaseColor.B            │
├─────────────────────┴──────────────────────────┴────────────────────────────┤
│ 每block总开销: 128 + 128 + 128 = 384 bits                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 精度提升原理

| 维度 | 优化前 (BC3) | 优化后 (BC5) |
|------|-------------|-------------|
| **Normal.X** | BC3 RGB 共享端点插值，3通道联合量化 | BC5 独立通道，拥有独立的 8-bit 端点对 + 3-bit 索引 |
| **Normal.Y** | 同上，受其他通道端点选择影响 | BC5 独立通道，同上 |
| **Normal.Z** | BC3 的 B 通道读取，精度最低 | 由 `sqrt(saturate(1.0 - dot(XY, XY)))` 数学重建，无量化误差 |
| **核心差异** | BC3 的 RGB 部分等同于 BC1，三通道共用 2 个 RGB565 格式端点（仅 5-6-5 bit），量化严重 | BC5 每个通道有独立的 2 个 8-bit 端点 + 48-bit 索引表（16×3bit），量化精度高一倍以上 |

**关键结论**：BC5 格式让 Normal.X 和 Normal.Y 各自拥有独立的端点插值空间，不再像 BC3 那样三通道互相干扰。Z 分量通过数学公式重建，完全消除了压缩误差。在大面积平坦地面场景中，微小的法线差异能够被准确保留，高光反射不再出现马赛克伪影。

### 3.3 成本与收益

| 维度 | 说明 |
|------|------|
| ✅ **收益** | Normal 精度显著提升，消除平坦地面马赛克高光伪影 |
| ⚠️ **内存增加** | 每 4×4 block 多消耗 64bit（Layer 2 从 BC1→BC3），VT Pool 从 50MB → 64MB |
| ⚠️ **Normal.Z 假设** | Z 分量固定为正值（sign = 1.0），假设法线始终朝上，适用于地面场景 |

---

## 四、修改文件清单

| # | 文件 | 路径 | 修改类型 |
|---|------|------|----------|
| 1 | `RuntimeVirtualTextureEnum.h` | Engine/Source/Runtime/Engine/Public/VT/ | 枚举 DisplayName 更新 |
| 2 | `RuntimeVirtualTexture.cpp` | Engine/Source/Runtime/Engine/Private/VT/ | **纹理层压缩格式变更（核心改动）** |
| 3 | `VirtualTextureCompress.usf` | Engine/Shaders/Private/ | **压缩 Shader 逻辑重写** |
| 4 | `VirtualTextureCommon.ush` | Engine/Shaders/Private/ | 空行调整（无逻辑变更） |
| 5 | `HLSLMaterialTranslator.cpp` | Engine/Source/Runtime/Engine/Private/Materials/ | BaseColor Unpack 参数修复 |
| 6 | `MaterialExpressionHLSL.cpp` | Engine/Source/Runtime/Engine/Private/Materials/ | Normal Unpack 类型变更 |
| 7 | `MaterialExpressions.cpp` | Engine/Source/Runtime/Engine/Private/Materials/ | Normal Unpack 类型变更（对称路径） |
| 8 | `ShaderGenerationUtil.cpp` | Engine/Source/Runtime/Engine/Private/ShaderCompiler/ | 移除多余 Target3 写入标记 |
| 9 | `RuntimeVirtualTextureRender.cpp` | Engine/Source/Runtime/Renderer/Private/VT/ | Layer2 UAV 格式升级 |
| 10 | `DefaultEngine.ini` | S1Game/Config/ | VT Pool 配置更新 |
| 11 | `M_BridgeAutoLandscape1.uasset` | S1Game/Content/Arts/Scene/Common/Landscape/NordNew/ | 二进制材质资源 |
| 12 | `M_BridgeAutoLandscape_NeoRing_New1.uasset` | S1Game/Content/Maps/NeoRing/NeoRing_sharedassets/BridgeAsset/ | 二进制材质资源 |

---

## 五、逐文件 Diff 详解

### 5.1 `RuntimeVirtualTextureEnum.h` — 枚举 DisplayName 更新

```diff
- Super_BaseColor_Normal_Specular_Mask UMETA(DisplayName = "Super Base Color, Normal, Roughness, Specular, Mask(BC5 BC3 BC1)"),
+ Super_BaseColor_Normal_Specular_Mask UMETA(DisplayName = "Super Base Color, Normal, Roughness, Specular, Mask(BC5 BC5 BC3)"),
```

将 DisplayName 中的格式描述从 `BC5 BC3 BC1` 更新为 `BC5 BC5 BC3`，使编辑器中的显示与优化后的实际数据布局一致。

### 5.2 `RuntimeVirtualTexture.cpp` — 纹理层压缩格式变更（核心改动）

**Layer 1（Normal层）**：`PF_DXT5 (BC3)` → `PF_BC5`

```diff
  case ERuntimeVirtualTextureMaterialType::Super_BaseColor_Normal_Specular_Mask:
-   return bCompressTextures ? PlatformCompressedRVTFormat(PF_DXT5) : PF_B8G8R8A8;
+   return bCompressTextures ? PlatformCompressedRVTFormat(PF_BC5) : PF_B8G8R8A8;
```

Normal 层从 BC3 改为 BC5，XY 两个分量各获得独立的高精度通道。

**Layer 2（Specular/Mask层）**：`PF_DXT1 (BC1)` → `PF_DXT5 (BC3)`

```diff
  case ERuntimeVirtualTextureMaterialType::Super_BaseColor_Normal_Specular_Mask:
-   return bCompressTextures ? PlatformCompressedRVTFormat(PF_DXT1) : PF_B8G8R8A8;
+   return bCompressTextures ? PlatformCompressedRVTFormat(PF_DXT5) : PF_B8G8R8A8;
```

Layer 2 从 BC1 升级为 BC3，获得 Alpha 通道以存放从 Layer 1 迁移过来的 BaseColor.B。

### 5.3 `VirtualTextureCompress.usf` — 压缩 Shader 逻辑重写

#### 压缩 Compute Shader 部分

Normal 数据读取从三通道 RGB 改为分离式 XYA 读取：

```diff
- float3 BlockNormal[16];
- ReadBlockRGB(RenderTexture1, TextureSampler1, SampleUV, TexelUVSize, BlockNormal);
- for (int i=0; i<16; i++) { BlockNormal[i].z = round(BlockNormal[i].z); }
+ float BlockNormalX[16];
+ float BlockNormalY[16];
+ float BlockNormalZ[16];
+ ReadBlockXYA(RenderTexture1, TextureSampler1, SampleUV, TexelUVSize, BlockNormalX, BlockNormalY, BlockNormalZ);
```

压缩输出从 BC3+BC1 改为 BC5+BC3：

```diff
- OutCompressTexture1_128bit = CompressBC3Block(BlockNormal, BlockBaseColorB);     // Normal.xyz + Color.b → BC3
- OutCompressTexture2_64bit  = CompressBC1Block(BlockSpecularRoughnessMask);        // Spec+Rough+Mask → BC1
+ OutCompressTexture1_128bit = CompressBC5Block(BlockNormalX, BlockNormalY);        // Normal.xy → BC5
+ OutCompressTexture2_128bit = CompressBC3Block(BlockSpecularRoughnessMask, BlockBaseColorB); // Spec+Rough+Mask+Color.b → BC3
```

#### Copy PS（无压缩路径）

```diff
- OutColor1 = float4(NormalXYZ, BaseColor.b);           // Normal全XYZ + Color.B
- OutColor2 = float4(SpecularRoughnessMask, 1.f);       // Spec+Rough+Mask, 无alpha
+ OutColor1 = float4(Normal.xy, 0.f, 1.f);              // Normal只写XY
+ OutColor2 = float4(SpecularRoughnessMask, BaseColor.b); // Spec+Rough+Mask + Color.B移至此处
```

### 5.4 `HLSLMaterialTranslator.cpp` — BaseColor Unpack 参数修复

```diff
- return AddCodeChunk(MCT_Float3, *SampleCode, *GetParameterCode(CodeIndex0), *GetParameterCode(CodeIndex1));
+ return AddCodeChunk(MCT_Float3, *SampleCode, *GetParameterCode(CodeIndex0), *GetParameterCode(CodeIndex2));
```

`VirtualTextureUnpackBaseColorBC5BC3(Layer0, Layer2)` 函数需要 Layer 0（BaseColor.RG）和 Layer 2（BaseColor.B 存于 Alpha）的数据。此前误传了 `CodeIndex1`（Layer 1 = Normal），修正为 `CodeIndex2`（Layer 2）。

### 5.5 `MaterialExpressionHLSL.cpp` — Normal Unpack 类型变更

Normal 解码类型从 `NormalBC3XYZ` 改为 `NormalBC5`：

```diff
- case Super_BaseColor_Normal_Specular_Mask:
-   UnpackTarget = 1; UnpackMask = 0x7; UnpackType = EVirtualTextureUnpackType::NormalBC3XYZ; break;
+ case Super_BaseColor_Normal_Specular_Mask:
+   UnpackTarget = 1; UnpackType = EVirtualTextureUnpackType::NormalBC5; break;
```

- **NormalBC3XYZ**：从 BC3 的 RGB 三通道直接读取完整 Normal.XYZ
- **NormalBC5**：从 BC5 的双通道读取 Normal.XY，Z 通过 `sqrt(1 - X² - Y²)` 数学重建

注释掉原先独立的 Mask unpack 分支（新布局下走默认逻辑即可）：

```diff
- case Super_BaseColor_Normal_Specular_Mask: UnpackTarget = 2; UnpackMask = 0x7; break;
+ //case Super_BaseColor_Normal_Specular_Mask: UnpackTarget = 2; UnpackMask = 0x7; break;
```

### 5.6 `MaterialExpressions.cpp` — Normal Unpack 类型变更（对称路径）

```diff
- case Super_BaseColor_Normal_Specular_Mask:
-   UnpackTarget = 1; UnpackMask = 0x7; UnpackType = EVirtualTextureUnpackType::NormalBC3XYZ; break;
+ case Super_BaseColor_Normal_Specular_Mask:
+   UnpackTarget = 1; UnpackType = EVirtualTextureUnpackType::NormalBC5; break;
```

材质编译的另一条路径（非 HLSL 表达式路径），做了与 5.5 相同的 Normal unpack 类型变更。

### 5.7 `ShaderGenerationUtil.cpp` — 移除多余的 Target3 写入标记

```diff
  TargetUsage[0] = EGBufferSlotUsage::Written;
  TargetUsage[1] = EGBufferSlotUsage::Written;
  TargetUsage[2] = EGBufferSlotUsage::Written;
- TargetUsage[3] = EGBufferSlotUsage::Written;
```

该类型只使用 3 层（Target 0/1/2），移除之前多余标记的 Target 3。

### 5.8 `RuntimeVirtualTextureRender.cpp` — Layer 2 UAV 格式升级

创建纹理和 UAV 时，Layer 2 从 64bit（BC1）升级为 128bit（BC3）：

```diff
- OutputAlias2 = CompressTexture2 = GraphBuilder.CreateTexture(..., Compressed64BitFormat, ...);
- CompressTextureUAV2_64bit = GraphBuilder.CreateUAV(FRDGTextureUAVDesc(CompressTexture2, 0, Compressed64BitFormat));
+ OutputAlias2 = CompressTexture2 = GraphBuilder.CreateTexture(..., Compressed128BitFormat, ...);
+ CompressTextureUAV2_128bit = GraphBuilder.CreateUAV(FRDGTextureUAVDesc(CompressTexture2, 0, Compressed128BitFormat));
```

与 `RuntimeVirtualTexture.cpp` 中的格式变更配套，渲染管线中 Layer 2 的 UAV 从 64bit 升级为 128bit，以支持 Alpha 通道存储 BaseColor.B。

### 5.9 `DefaultEngine.ini` — VT Pool 配置更新

```diff
- +Pools=(Formats=(PF_BC5,PF_DXT5,PF_DXT1),MinTileSize=0,MaxTileSize=0,SizeInMegabyte=50,...)
+ +Pools=(Formats=(PF_BC5,PF_BC5,PF_DXT5),MinTileSize=0,MaxTileSize=0,SizeInMegabyte=64,...)
```

VT 物理纹理池格式声明从 `BC5+DXT5+DXT1` 更新为 `BC5+BC5+DXT5`，容量从 50MB 增加到 64MB。

### 5.10 二进制材质资源

- **M_BridgeAutoLandscape1.uasset**：NordNew 场景的自动景观材质
- **M_BridgeAutoLandscape_NeoRing_New1.uasset**：NeoRing 场景的桥接景观材质

两个材质资源已更新为使用优化后的 `Super_BaseColor_Normal_Specular_Mask` 数据布局。

---

## 六、全链路数据流

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        【1. 材质渲染阶段】                               │
│                                                                         │
│                  场景材质渲染到 RVT                                      │
│                         │                                               │
│                         ▼                                               │
│              3 个 B8G8R8A8 Render Targets                               │
│              ┌──────────┼──────────┐                                    │
│              ▼          ▼          ▼                                     │
│        RT0: Color.RG  RT1: Normal.XY  RT2: Spec+Rough+Mask+Color.B     │
└──────────────┬──────────┬──────────┬────────────────────────────────────┘
               │          │          │
               ▼          ▼          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        【2. 压缩阶段】                                   │
│                                                                         │
│                      压缩模式选择?                                       │
│                    ┌────┴────┐                                           │
│                    ▼         ▼                                           │
│             Compress CS   Copy PS                                       │
│                    │         │                                           │
│  ┌─────────────────┤         ├─────────────────┐                        │
│  │   CS 路径输出:   │         │   PS 路径输出:   │                        │
│  │                  │         │                  │                        │
│  │  L0: CompressBC5Block     │  L0: float4      │                        │
│  │      (Color.R, Color.G)   │  (Color.RG,0,1)  │                        │
│  │                  │         │                  │                        │
│  │  L1: CompressBC5Block     │  L1: float4      │                        │
│  │      (Normal.X, Normal.Y) │  (Normal.XY,0,1) │                        │
│  │                  │         │                  │                        │
│  │  L2: CompressBC3Block     │  L2: float4      │                        │
│  │      (SRM, Color.B)       │  (SRM, Color.B)  │                        │
│  └─────────────────┬┘         └┬─────────────────┘                       │
└────────────────────┬───────────┬────────────────────────────────────────┘
                     │           │
                     ▼           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        【3. VT 存储】                                    │
│                                                                         │
│               VT Physical Texture Pool (64MB)                           │
│     ┌───────────────┬───────────────┬───────────────┐                   │
│     │  Layer 0: BC5 │  Layer 1: BC5 │  Layer 2: BC3 │                   │
│     │  Color.R/G    │  Normal.X/Y   │  SRM + Color.B│                   │
│     │  (128 bits)   │  (128 bits)   │  (128 bits)   │                   │
│     └───────────────┴───────────────┴───────────────┘                   │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     【4. 采样解码阶段】                                   │
│                                                                         │
│                材质采样 VirtualTextureUnpack                             │
│                         │                                               │
│          ┌──────────────┼──────────────┐                                │
│          ▼              ▼              ▼                                 │
│   BaseColor =    Normal =        Specular  = L2.R                       │
│   Unpack(L0.RG,  Unpack(L1.RG)  Roughness = L2.G                       │
│          L2.A)   Z=sqrt(1-X²-Y²) Mask     = L2.B                       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 七、技术要点总结

1. **BC5 vs BC3 的本质差异**：BC3 的 RGB 部分等同于 BC1，三通道共享 2 个 RGB565 端点（5-6-5 bit），量化严重；BC5 的每个通道拥有独立的 2 个 8-bit 端点 + 48-bit 索引，精度提升一倍以上。

2. **Normal.Z 重建策略**：将 Z 从显式存储改为 `sqrt(saturate(1.0 - dot(XY, XY)))` 数学重建，sign 固定为 1.0（假设法线朝上），完全消除 Z 分量的压缩误差。

3. **BaseColor.B 迁移**：由于 Layer 1 从 BC3（4通道）改为 BC5（2通道），原本存在 Layer 1 Alpha 通道的 BaseColor.B 迁移到 Layer 2 的 Alpha 通道，Layer 2 相应从 BC1 升级为 BC3。

4. **内存开销可控**：每 block 增加 64bit（320→384 bits），VT Pool 增加 14MB（50→64MB），是精度与内存的合理权衡。

5. **修改涉及完整管线**：从枚举定义 → 纹理格式配置 → 压缩 Shader → 材质 Unpack 编译 → 渲染管线 UAV → 引擎配置，全链路一致更新。
