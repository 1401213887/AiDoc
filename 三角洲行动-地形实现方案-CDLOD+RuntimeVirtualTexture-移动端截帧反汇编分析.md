# 三角洲行动-地形实现方案-CDLOD+RuntimeVirtualTexture-移动端截帧反汇编分析

> 《三角洲行动》手游（Adreno 740 / 骁龙 8Gen2）地形用 **CDLOD 连续 LOD + Runtime Virtual Texture** 组合：
> 顶点缓冲仅 4 字节/顶点（只存格点索引），高度与顶点法线在 VS 里从一张 1024² 贴图采出；
> 材质层走 RVT 两张 5544² 物理页。单个 DrawCall 47 实例 / 96,256 面，占全帧 27.7% 的三角形。

**分析对象**：`E:\GPUCapture\三角洲\DFM\烬区\Pos3_high.rdc`，BasePass = `Colour Pass #1`，**eid 402**
**工具链**：RenderDoc MCP `execute_python`（replay 线程内直接反汇编 + 读 cbuffer/顶点缓冲）

---

## 一、DrawCall 概览

| 项 | 值 |
|---|---|
| Event ID | **402**（单个 DrawCall） |
| 面数 | **96,256** = 47 实例 × 2,048 面 |
| 占全帧三角形 | **27.7%**（全帧 346,971 面） |
| 占全帧 DC | 0.3%（全帧 293 DC） |
| 距离跨度 | 0.2 – 61.4 m（中位 25.5 m） |
| 图元拓扑 | TriangleList |
| 深度 | `GreaterEqual` + DepthWrite（**reverse-Z**） |
| VS | `main_00005c28_d9642bce` / `ResourceId::32881` |
| PS | `main_0000dc0c_af2e4a1c` / `ResourceId::36221` |

VS/PS 均为该 draw **独占**，全帧无第二个 draw 使用。

### 顶点缓冲：4 字节/顶点

```
in_ATTRIBUTE0 : UNorm × 4 × 1 byte    (vb0, stride = 4)
```

**没有 float3 位置、没有 NORMAL、没有 TANGENT。** 仅一个 4 字节的 UNorm4，作为 CDLOD 网格的格点索引。
位置的 Z 和顶点法线全部在 VS 内从贴图采样得到（见第三节）。

---

## 二、资源绑定全表

### VS 侧（3 张）

| Binding | 尺寸 / 格式 | 语义名 | 用途 |
|---|---|---|---|
| 3 | 1×1 RGBA16F | `View_DistantSkyLightLutTexture` | 远景天光 LUT |
| 4 | 32×32×4 RGBA16F (3D) | `View_CameraAerialPerspectiveVolume` | 大气透视体积 |
| **5** | **1024² BGRA8** | **`CDLODConstParameters_HeightAndNormalMapTexture`** | **高度 + 顶点法线** |

### PS 侧（7 张 + 1 UAV）

| Binding | 尺寸 / 格式 | 语义名 | 用途 |
|---|---|---|---|
| — | 3072×1024 D16 | *(深度附件)* | 深度 |
| 10 | 2048² ASTC | `MobileDirectionalLight_DirectionalLightShadowPCFTexture` | 平行光阴影 |
| 11 | 128²×6 Cube, 8 mip | `MobileReflectionCapture_Texture` | 反射探针 |
| 12 | 1024² BGRA8 | `LandscapeLightingInfo_LandscapeLightingTexture` | 地形烘焙光照（对数编码） |
| **13** | **1024² R16_UINT, 11 mip** | **`Material_VirtualTexturePageTable0_0`** | **VT 页表** |
| **14** | **8×8 RGBA16_UINT** | **`Material_VirtualTexturePageTableIndirection_0`** | **VT 页表间接索引** |
| **15** | **5544² ASTC** | **`Material_VirtualTexturePhysical_0`** | **VT 物理页：BaseColor + 参数** |
| **16** | **5544² ASTC** | **`Material_VirtualTexturePhysical_1`** | **VT 物理页：Normal + 参数** |
| UAV 9 | 1 MB Buffer | `MobileBasePass_SceneTextures_VirtualTextureFeedbackUAV` | VT 反馈（驱动页面流送） |

Constant Buffer：VS `vu_h[250]`(4000 B) + `vu_i`(16 B) + `vu_u`(16 B)；PS `pu_h[52]`(832 B) + `pu_i`(16 B) + `pu_u`(96 B)。

---

## 三、CDLOD：顶点在 VS 里生成

**CDLOD** = Continuous Distance-Dependent Level Of Detail。核心思想是网格拓扑固定（每块 2048 面），
顶点高度从 heightmap 采样，相邻 LOD 块之间用 morph 权重做连续过渡，消除 LOD 跳变与裂缝。

### 3.1 一图四通道，全部占满

`CDLODConstParameters_HeightAndNormalMapTexture`（1024² BGRA8，4 MB）：

| 通道 | 内容 |
|---|---|
| **R, G** | **16 位高度**（8+8 拆分） |
| **B, A** | **顶点法线 XY**（Z 在 shader 重建） |

### 3.2 UV 计算与 LOD Morph（VS L359–400）

```
// 1) clamp 到当前 LOD 块边界
_254 = clamp(v17.xy, vu_h[2].xy, vu_h[2].zw)       // 实测 vu_h[2] = (0, -2016, 2016, 0)
v33  = _254

// 2) morph: 用 clamp 前后的差值做插值，v20.yy / v20.zz 是 morph 权重与缩放
_267 = v32 - v33                 // 偏离块边界的量
_270 = _267 * v20.yy             // × morph 权重
_272 = (v27 + v31) - _270
_275 = _272 * v20.zz
_276 = _275 + 0.5
_279 = v19.zw + _276 * v3        // 最终 heightmap UV
```

`clamp` + 差值插值 **就是 CDLOD 消除 LOD 裂缝的机制**：跨块边界的顶点被平滑拉向低一级 LOD 的位置。

### 3.3 高度解码（VS L404–415）

```
_287 = ImageSampleExplicitLod(HeightAndNormalMap, uv, Lod(0.0))   // 强制 mip 0
_291 = _287.x * 510.0          // R 通道，粗粒度
_295 = _287.y * 1.9922         // G 通道，细粒度  (≈ 255/128)
_296 = _291 + _295
_298 = _296 - 256.0            // 偏移到有符号
v17.z = _298                   // → 顶点 Z
```

- `510 = 255 × 2`，`1.9922 ≈ 255/128`
- 高度范围 **[-256, +254]**，精度 **≈ 0.0078 单位（0.08 mm）**
- **必须 `Lod(0.0)` 显式 mip 0** —— 高度值经过 mip 过滤会导致地形变形

### 3.4 顶点法线解码（VS L418–438）

```
_303 = _287.zw                       // B, A 通道
_309 = _303 * 2.0 - 1.0              // 解压到 [-1, 1]
_317 = dot(_309, _309)
_321 = sqrt(max(1.0 - _317, 0.0))    // 重建 Z
v38  = (_309.x, _309.y, _321)
v18  = v38                           // → out_TEXCOORD10（世界空间顶点法线）
```

**这就是顶点法线**，职责等同普通模型顶点缓冲里的 `NORMAL` 语义，只是存在贴图里而非 VB 里。

### 3.5 为什么预存法线而不从高度差分算

法线本可由 heightmap 邻域差分求得（省 2 个通道）。选择预存的理由：

- **BGRA8 四通道本就全占满**，不预存法线 B/A 也是浪费
- 差分需要额外 2–4 次采样 + ALU
- **移动端 VS 跑两遍**（binning pass + render pass），VS 的 ALU/采样成本翻倍
- 2 MB 额外显存换 VS 指令数，在 Tile-Based 架构上划算

---

## 四、两套法线的分工（关键理解）

这是本次分析最容易误解的一点：**CDLOD 有法线、RVT 也有法线，两者不冲突，是"基 vs 扰动"的关系。**

| | CDLOD B/A | RVT `Physical_1.xy` |
|---|---|---|
| **坐标空间** | **世界空间** | **切线空间** |
| **语义** | **顶点法线**（地形起伏大形） | 材质细节法线（岩石/泥土凹凸） |
| **分辨率** | 1024²，整块地形 | 5544² VT 页，逐 texel |
| **采样位置** | **VS**，逐顶点 | **PS**，逐像素 |
| **角色** | **构建 TBN 基** | 在 TBN 内做扰动 |

### 4.1 PS 用顶点法线现场构建 TBN（PS L396–420）

```
v10 = in_TEXCOORD10           // N ← CDLOD 顶点法线（世界空间）
v12 = (N.z, 0.0, -N.x)        // 由 N 构造正交向量
v13 = normalize(v12)          // T
v14 = cross(N, T)             // B
```

**切线不来自顶点缓冲**（顶点只有 4 字节），而是由 N 现场推导。
这正是 RVT 切线空间法线能被正确转换到世界空间的前提 —— **没有 CDLOD 法线就没有 TBN**。

### 4.2 RVT 细节法线解压 + 距离淡出（PS L896–1007）

```
// 解压切线空间法线
_541 = rvt1.xy * 2.0 - 1.0
_552 = sqrt(clamp(1.0 - dot(_541,_541), 0, 1))
v83  = (_541.x, _541.y, _552)

// 按距离淡出到"无扰动"
f94 = length(worldPos - cameraPos)
h99 = clamp((1.0 - f94 / pu_h[4].w) * 2.0, 0.0, 1.0)
v23 = 1.0 - h99
v93 = mix(v83, float3(0,0,1), v22.x)      // (0,0,1) = 切线空间"不扰动"
```

远处细节法线淡出，只保留 CDLOD 的几何法线 —— 省带宽且避免远处法线噪点导致的高频闪烁。

---

## 五、Runtime Virtual Texture 材质层

### 5.1 三级寻址链

```
L652  ImageFetch(PageTableIndirection_0, Lod 0)          8×8 RGBA16_UINT   → 间接索引
L750  ImageFetch(PageTable0_0, Lod(_403))                1024² R16, 11 mip → 页表
L852  ImageSampleExplicitLod(Physical_0, uv, Grad(a,b))  5544² ASTC        → 物理页 0
L859  ImageSampleExplicitLod(Physical_1, uv, Grad(a,b))  5544² ASTC        → 物理页 1
```

- 前两级用 **`ImageFetch`**（整数寻址、无过滤）解地址，成本远低于 filtered sample
- 物理页必须用 **`Grad` 显式梯度**：VT 页边界处硬件推导的 mip 会跨页出错

### 5.2 两张物理页的通道分配

两张用**同一 UV、同一 Grad** 采样，是同一套 VT 的两层：

| | 通道 | 内容 |
|---|---|---|
| `Physical_0` | **rgb** | **BaseColor** |
| | **a** | 参数 0（→ `v78.x`） |
| `Physical_1` | **xy** | **切线空间法线 XY**（Z 由 `sqrt(1-dot)` 重建） |
| | **z** | 参数 1（→ `v80.z`） |
| | **a** | 参数 2（→ `v78.y`） |

`v78 = (Physical_0.a, Physical_1.a)` 两个 alpha 打包成一组标量参数（Roughness/Specular 之类）。

### 5.3 显存开销是常量

5544² ASTC ≈ **30.7 MB/张**，两张 **≈ 61 MB**。

**这是 VT 物理页缓存，不是地形贴图本身** —— 开销固定，**与地形实际面积无关**。
真实地形贴图数据在磁盘上按页流送，由 `VirtualTextureFeedbackUAV` 回写的反馈驱动。

---

## 六、地形烘焙光照的对数编码（PS L1181–1209）

`LandscapeLightingInfo_LandscapeLightingTexture`（1024² BGRA8）用 8 bit 存 HDR 光照：

```
v142 = sample(LandscapeLightingTexture, uv)
_795 = v142.rgb * 0.7077                        // 固定缩放
_798 = _795 + 0.01
_805 = dot(_798, float3(0.30, 0.59, 0.11))      // 亮度
_812 = exp2(_805 * 16.0 - 8.0)                  // 对数解码
_814 = _812 - 0.0039
_816 = _814 / _805
_818 = _798 * _816                              // 保色调，换亮度
```

**RGBM 类的对数亮度编码**：RGB 存色调，亮度用 `exp2(lum*16-8)` 解出，
动态范围约 `2^-8 ~ 2^8`（1/256 ~ 256），用 4 MB 的 8bit 贴图承载 HDR 地形光照。

---

## 七、对性能预算的意义

### 7.1 地形面数由 CDLOD 参数决定，不由美术决定

96,256 面 = **47 个 CDLOD 块 × 2,048 面/块**，是 LOD 级数与块划分的产物。
**要降面数须调 CDLOD 参数（块尺寸、LOD 距离阈值），让美术简化模型无效。**

### 7.2 近半地形面数落在 30 m 内

按顶点分布摊分，96,256 面中 **48,128 面（50%）在 0–30 m**，是近景面数（160,678）的最大单一来源（30%）。

**地形不是"远景背景"。** 若 47 个块都是固定 2,048 面，说明 LOD 分级未按距离拉开——可查的优化点。

### 7.3 采样数在合理范围

PS 采 7 张（其中 2 张 VT 是低成本 `ImageFetch`），PS 指令数 1,202。
高通建议单组 texture fetch ≤ 15，此处无压力。**地形瓶颈在 9.6 万面的几何，不在采样。**

### 7.4 顶点带宽极省

4 字节/顶点。对比常规地形（position float3 + normal + uv ≥ 20 字节），**带宽降至 1/5**。
在移动端 VS 跑两遍的前提下，这个收益被放大一倍。

---

## 八、方案要点 Checklist

复刻这套方案需要落实的点：

- [ ] 顶点缓冲只存格点索引（UNorm4，4 字节），**不存 position / normal / tangent**
- [ ] Heightmap 用 BGRA8 一图四通道：**RG = 16 位高度，BA = 顶点法线 XY**
- [ ] 高度解码 `R*510 + G*(255/128) - 256`，**必须 `Lod(0.0)`**，不可 mip 过滤
- [ ] 法线 Z 用 `sqrt(max(1-dot(xy,xy), 0))` 重建，不存储
- [ ] LOD morph：采样 UV 先 `clamp` 到块边界，再用偏离量 × morph 权重插值
- [ ] PS 中由顶点法线现场构建 TBN：`T = normalize(N.z, 0, -N.x)`，`B = cross(N, T)`
- [ ] RVT 物理页采样**必须用 `Grad` 显式梯度**（跨页 mip 推导会错）
- [ ] RVT 页表寻址用 `ImageFetch`（整数、无过滤），不要用 filtered sample
- [ ] 细节法线按距离 `mix` 到 `(0,0,1)` 淡出，避免远处高频闪烁
- [ ] 绑定 VT Feedback UAV，否则页面流送无法驱动
- [ ] 烘焙光照用对数亮度编码（`exp2(lum*16-8)`）在 8bit 贴图里存 HDR

---

## 九、分析方法备忘

本次能拿到这些结论，靠的是 RenderDoc MCP 的 `execute_python`，几个要点：

**① shader reflection 自带完整语义名**
`GetShaderReflection(stage).readOnlyResources` 直接给出 `CDLODConstParameters_HeightAndNormalMapTexture`
这类原始变量名，比从 binding 号猜用途可靠得多。

**② 反汇编走 `DisassembleShader`**
```python
refl = st.GetShaderReflection(rd.ShaderStage.Pixel)
t = ctrl.GetDisassemblyTargets(True)[0]     # "SPIR-V (RenderDoc)"
dis = ctrl.DisassembleShader(st.GetGraphicsPipelineObject(), refl, t)
```
输出是伪 C 风格，变量名形如 `v83` / `_541`，**追数据流要按变量名 grep 全部引用**，
才能确定某次采样的结果流向何处（例如确认 `Physical_1.xy` 是法线而非 BaseColor）。

**③ cbuffer 读取：属性名是 `resource` 不是 `resourceId`**
```python
d = st.GetConstantBlock(rd.ShaderStage.Vertex, 0, 0).descriptor
data = ctrl.GetBufferData(d.resource, d.byteOffset, d.byteSize)
```

**④ 长任务必然报 timeout，但服务端会跑完**
`execute_python` 遍历 293 个 draw 时 MCP 报 `Request timed out`，**不要重试**，
改为脚本内落盘 + 轮询输出文件。

---

## 十、相关参考

- **CDLOD 原始论文**：Filip Strugar, *Continuous Distance-Dependent Level of Detail for Rendering Heightmaps (CDLOD)*, Journal of Graphics Tools, 2009
  https://github.com/fstrugar/CDLOD
- **UE Runtime Virtual Texturing 官方文档**
  https://dev.epicgames.com/documentation/en-us/unreal-engine/runtime-virtual-texturing-in-unreal-engine
- **UE Virtual Texturing 总览**
  https://dev.epicgames.com/documentation/en-us/unreal-engine/virtual-texturing-in-unreal-engine
- **Adreno GPU 优化指南**（texture fetch ≤ 15/组 等建议）
  https://developer.qualcomm.com/software/adreno-gpu-sdk/gpu
- 本次截帧数据：`E:\GPUCapture\三角洲\DFM\烬区\Pos3_high.rdc`
- 全帧统计与分层：`D:\shaders\capture_compare_notes.md`（[9] / [9b] 节）
- per-draw 距离数据：`D:\shaders\pos3_draw_depth.json`、`pos3_worlddist.json`
