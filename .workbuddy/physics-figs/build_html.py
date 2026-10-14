#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组装《UE 手游物理方案》HTML —— 正文用 {{FIGn}} 占位，运行时内联 SVG。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIGDIR = HERE
OUTNAME = 'UE手游物理方案-PhysX到Chaos内核替换-移动端五层分解与UE4UE5差异.html'

CSS = """
  :root{
    --bg:#f7f8fa; --panel:#ffffff; --ink:#1a1d21; --ink2:#4a5158; --ink3:#7a828a;
    --line:#e2e6ea; --accent:#2f6feb; --accent-soft:#eaf1fe;
    --pc:#2f6feb; --chaos:#8e44ad; --mobile:#d9741a;
    --ok:#1f9254; --warn:#c0392b; --note:#8a6d1f;
    --code-bg:#f2f4f7;
  }
  *{box-sizing:border-box}
  body{margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.75; font-size:15px}
  .wrap{max-width:1180px; margin:0 auto; padding:40px 28px 100px}
  header.top{background:linear-gradient(135deg,#1f3a63 0%,#2f6feb 100%); color:#fff;
    border-radius:14px; padding:34px 36px; margin-bottom:22px}
  header.top h1{margin:0 0 10px; font-size:27px; letter-spacing:.3px}
  header.top p{margin:0; opacity:.9; font-size:14px}
  header.top .meta{margin-top:16px; font-size:12.5px; opacity:.85;
    border-top:1px solid rgba(255,255,255,.22); padding-top:12px}
  header.top code{background:rgba(255,255,255,.22); color:#fff}
  header.top .src{background:rgba(255,255,255,.22); color:#fff}
  h2{font-size:21px; margin:44px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--line)}
  h2 .num{color:var(--accent); font-weight:700; margin-right:8px}
  h3{font-size:17px; margin:28px 0 10px}
  h4{font-size:15px; margin:20px 0 8px; color:var(--ink2)}
  p{margin:10px 0}
  ul,ol{margin:10px 0; padding-left:24px}
  li{margin:5px 0}
  code{background:var(--code-bg); padding:1.5px 5px; border-radius:4px;
    font-family:"Cascadia Mono",Consolas,"SF Mono",Menlo,monospace; font-size:12.8px}
  .src{font-family:"Cascadia Mono",Consolas,monospace; font-size:11px;
    color:var(--accent); background:var(--accent-soft); border-radius:3px;
    padding:0 5px; white-space:nowrap; font-weight:600}
  a{color:var(--accent)}
  .panel{background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:20px 22px; margin:16px 0}
  table{border-collapse:collapse; width:100%; margin:14px 0; font-size:13.4px; background:var(--panel)}
  th,td{border:1px solid var(--line); padding:8px 11px; text-align:left; vertical-align:top}
  th{background:#eef2f7; font-weight:600; font-size:13px}
  tbody tr:nth-child(even){background:#fafbfc}
  .tag{display:inline-block; font-size:11px; padding:1px 7px; border-radius:10px; font-weight:600; white-space:nowrap}
  .t-pc{background:#e8f0fe; color:var(--pc)}
  .t-chaos{background:#f5ebfa; color:var(--chaos)}
  .t-mob{background:#fdf0e4; color:var(--mobile)}
  .t-ok{background:#e6f6ec; color:var(--ok)}
  .t-bad{background:#fdeaea; color:var(--warn)}
  .t-note{background:#fdf8e8; color:var(--note)}
  .t-grey{background:#eef1f4; color:var(--ink3)}
  .callout{border-left:4px solid var(--accent); background:var(--accent-soft);
    padding:12px 16px; border-radius:0 8px 8px 0; margin:14px 0}
  .callout.warn{border-color:var(--warn); background:#fdf0ef}
  .callout.note{border-color:var(--note); background:#fdf8e8}
  .callout.ok{border-color:var(--ok); background:#f0f9f3}
  .callout .h{font-weight:700; margin-bottom:4px; font-size:13.5px}
  .fig{margin:24px 0; text-align:center}
  .fig svg{max-width:100%; height:auto; background:var(--panel);
    border:1px solid var(--line); border-radius:10px}
  .fig .cap{font-size:12.5px; color:var(--ink3); margin-top:8px}
  .toc{background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px 22px; margin-bottom:26px}
  .toc ol{margin:6px 0; padding-left:22px; columns:2; column-gap:30px}
  @media(max-width:700px){.toc ol{columns:1}}
  .toc a{color:var(--ink2); text-decoration:none}
  .toc a:hover{color:var(--accent)}
  footer{margin-top:60px; padding-top:20px; border-top:1px solid var(--line);
    font-size:12.5px; color:var(--ink3)}
"""

BODY = r"""
<div class="wrap">

<header class="top">
  <h1>UE 手游物理方案全解</h1>
  <p>从 PhysX 到 Chaos 的内核替换 · 手游「物理」的五层分解 · UE4 / UE5 差异 · 移动端落地约束</p>
  <div class="meta">
    资料类型：<b>公开资料调研</b>（Epic 官方博客 / GDC / Unreal Fest / 厂商访谈），<b>非本仓库源码考古</b>，故不含 <code>文件名:行号</code> 式引用。<br>
    每条结论标注来源编号 <span class="src">S1</span>，来源可信度分级见 <a href="#s11" style="color:#fff;text-decoration:underline">§11 信息可信度排查</a>。<br>
    凡公开资料未证实的，一律标 <span class="tag t-note">待确认</span>，不做推测。
  </div>
</header>

<div class="toc">
  <strong>目录</strong>
  <ol>
    <li><a href="#s1">TL;DR：一页结论</a></li>
    <li><a href="#s2">手游「物理」的五层分解</a></li>
    <li><a href="#s3">内核 A：UE4 的 PhysX</a></li>
    <li><a href="#s4">内核 B：UE5 的 Chaos</a></li>
    <li><a href="#s5">两代内核的差异对比</a></li>
    <li><a href="#s6">贯穿主线：一个受击布娃娃的一生</a></li>
    <li><a href="#s7">案例：热门 UE 手游实际用什么</a></li>
    <li><a href="#s8">移动端预算与六个优化杠杆</a></li>
    <li><a href="#s9">物理的网络同步与确定性</a></li>
    <li><a href="#s10">能不能换掉 Chaos？</a></li>
    <li><a href="#s11">信息可信度排查</a></li>
    <li><a href="#s12">对 UE5.5 分叉项目的含义 + Checklist</a></li>
    <li><a href="#s13">参考来源</a></li>
  </ol>
</div>

<!-- ============================================================ -->
<h2 id="s1"><span class="num">01</span>TL;DR：一页结论</h2>

<p><b>一句话：</b>手游里说的「物理」不是一个系统，而是五层独立实现；UE4 用 NVIDIA PhysX、UE5 用 Epic Chaos，二者是<b>整体替换</b>而非版本升级；而移动端的真实约束从来不是「选哪个物理引擎」——UE5 只有 Chaos，没得选——而是<b>让多少对象进物理求解器</b>。</p>

<table>
<thead><tr><th style="width:26%">结论</th><th>要点</th></tr></thead>
<tbody>
<tr>
  <td><b>① 手游物理需要分层看</b></td>
  <td>角色移动用 CMC / 自研 KCC（不走刚体）；载具走专用求解分支；布娃娃走引擎 ragdoll 但降频简化；布料毛发高配限定且必须降级；破坏多数是「预制分块 + 特效 + 烘焙」，不是实时物理。</td>
</tr>
<tr>
  <td><b>② UE4 → UE5 是换内核</b></td>
  <td>Chaos 于 UE4.23 引入，<b>UE5.0 成为默认</b>，<b>UE5.1 移除 PhysX</b> <span class="src">S1</span><span class="src">S2</span>。两套内核 API、数据布局、求解模型都不同，PhysX Vehicle 随 UE5.0 一并消失，且无法优雅改父类 <span class="src">S3</span>。</td>
</tr>
<tr>
  <td><b>③ Chaos 并非全面更快</b></td>
  <td>Epic 官方实测：Sweeps 快 2.6~4 倍，但 Overlaps / Raycasts / 部分刚体测试<b>反而慢 23%~54%</b>，官方归因于 LWC 双精度浮点开销 <span class="src">S1</span>。</td>
</tr>
<tr>
  <td><b>④ 最有力的行业实证</b></td>
  <td>《三角洲行动》<b>多人模式（含手机端）用 UE4</b>，单人战役用 UE5 且<b>官方明确不上手机平台</b>，理由是 UE5 硬件要求过高 <span class="src">S8</span><span class="src">S9</span>。</td>
</tr>
<tr>
  <td><b>⑤ Chaos 不是确定性的</b></td>
  <td>跨平台 / 跨线程浮点差异使其无法保证逐位一致；Epic 官方建议<b>不要追求 100% 确定性</b>，只有 lockstep 帧同步才需要 <span class="src">S5</span>。</td>
</tr>
<tr>
  <td><b>⑥ 换引擎不现实</b></td>
  <td>UE5 无官方 Jolt / PhysX 回归方案；第三方 UnrealJolt 是<b>与 Chaos 共存</b>而非替换，Epic 明确表示 Chaos 是唯一方向、第三方集成不做支持 <span class="src">S6</span>。</td>
</tr>
</tbody>
</table>

<!-- ============================================================ -->
<h2 id="s2"><span class="num">02</span>手游「物理」的五层分解</h2>

<p><b>本章结论：</b>把「物理」当成一个整体去选型，是讨论跑偏的根源。实际项目中它至少分成五层，每层的技术选型、性能量级和网络策略都不同。</p>

{{FIG1}}

<h3>① 角色移动 / 位移 —— 不用刚体物理</h3>

<p><b>为什么不用：</b>三个理由同时成立。</p>
<ul>
  <li><b>网络同步</b>：客户端预测要求移动结果可重复。刚体求解器在小步长累积误差下不可复现，一帧的偏差会滚成持续的「橡皮筋」修正。</li>
  <li><b>手感</b>：刚体角色会被斜坡弹飞、被其他刚体撞开、在墙角卡住，这些在竞技游戏里都是 bug。</li>
  <li><b>成本</b>：一个角色一个刚体，百人同屏就是上百个持续求解的刚体。</li>
</ul>

<p><b>实际做法：</b>UE 的 <code>UCharacterMovementComponent</code>（CMC），或完全自研的 KCC（Kinematic Character Controller）。角色是一个胶囊体，位移靠自定义扫掠（sweep）逐段推进。</p>

<div class="callout note">
  <div class="h">容易忽略的一点：CMC 仍然消耗物理预算</div>
  角色移动本质上是<b>场景查询</b>（Scene Query）的消费者——每次 <code>MoveComponent</code> 的扫掠都要问物理场景「这段路径上有没有东西」。Chaos 的官方技术博客标题正是 <i>Chaos Scene Queries <b>and</b> Rigid Body Engine</i> <span class="src">S1</span>，把查询和刚体引擎并列为两大子系统。所以「角色不用刚体」≠「角色不花物理的钱」，只是从<b>求解</b>预算转移到了<b>查询</b>预算。
</div>

<h3>② 载具 —— 唯一真正称得上「引擎物理」的一层</h3>

<p>载具是移动端唯一大量使用引擎专用物理求解的场景：轮胎摩擦模型 + 悬挂射线检测，是独立于通用刚体的专用求解分支。</p>
<ul>
  <li><b>UE4</b>：PhysX Vehicle。<span class="tag t-note">待确认</span> 公开资料普遍认为 <b>UE4.27 是最后一个可用版本</b> <span class="src">S10</span>。</li>
  <li><b>UE5</b>：Chaos Vehicle。UE5.5 另新增 <b>Chaos Modular Vehicle System（CMVS）</b>，支持在 Android 上做实时车辆组装、破坏与物理交互 <span class="src">S7</span>。</li>
  <li><b>大厂多自研改造</b>：官方宣传口径中，这类项目会强调「载具物理专门方案」——每种载具独立的加速特点、引擎动力曲线、减震能力 <span class="src">S11</span>。</li>
</ul>

<h3>③ 布娃娃 / 受击 —— 用引擎物理，但被大幅简化</h3>

<p>这是移动端第二层真正跑引擎求解器的场景，但工程上做了三重压缩：</p>
<ul>
  <li><b>简化骨架</b>：物理骨架的刚体数远少于渲染骨架，典型 12~20 个胶囊体；位移通过约束映射回渲染骨架。</li>
  <li><b>降频</b>：物理 tick 从 60Hz 降到 30Hz。移动端业界经验是 <code>FixedUpdate</code> 每秒不超过 30 次。</li>
  <li><b>时限</b>：受击后只求解 2~4 秒，随后冻结或切回动画——不会让一具尸体永久占据求解预算。</li>
</ul>

<h3>④ 布料 / 毛发 —— 高配限定，且几乎必然降级</h3>

<p>Unreal Fest 2025 给出的移动端布料目标相当明确 <span class="src">S12</span>：</p>
<table>
<thead><tr><th style="width:34%">指标</th><th>目标值</th></tr></thead>
<tbody>
<tr><td>单角色布料模拟耗时</td><td>PS5 &lt;1ms；<b>移动端 ≈1ms</b></td></tr>
<tr><td>模拟顶点数</td><td><b>200~600 个</b>（斗篷 / 披风推荐 350 个）</td></tr>
<tr><td>求解器选择</td><td><b>PBD</b>（计算快、刚度参数简单）；XPBD 留给高分辨率影视级品质</td></tr>
<tr><td>碰撞体</td><td>只用简单圆形基本体（锥形胶囊、球体），兼顾性能与防穿模</td></tr>
<tr><td>自碰撞</td><td>开销大，建议用廉价的球体排斥；5.6 新增布料-布料约束防缠绕</td></tr>
</tbody>
</table>

<p><b>毛发（Groom）是重灾区。</b>Epic 官方移动端特性支持表把三项能力分级如下 <span class="src">S13</span>：</p>
<table>
<thead><tr><th style="width:30%">能力</th><th style="width:14%">移动端支持</th><th>说明</th></tr></thead>
<tbody>
<tr><td>Chaos Physics</td><td><span class="tag t-ok">○ 支持</span></td><td>复杂场景需注意负载</td></tr>
<tr><td>Chaos Destruction</td><td><span class="tag t-note">△ 需降级</span></td><td>必须做降级缩放，不能直接上</td></tr>
<tr><td>Chaos Cloth</td><td><span class="tag t-note">△ 有限</span></td><td>只能以「简易设置」运行</td></tr>
</tbody>
</table>

<h3>⑤ 破坏 / 碎片 —— 多数是「看起来碎了」</h3>

<p>UE5 的实时破坏走 Geometry Collection（替代 UE4 的 Destructible Mesh），支持动态断裂、多级层级破坏和基于字段的力场控制 <span class="src">S1</span>。能力很强，但<b>在移动端预算之外</b>。</p>

<p>手游的真实做法是：美术预制分块 + 特效 + VAT 顶点动画烘焙 + 物理与视觉解耦。碎片数量要限（业界经验 20~30 个），静止后立刻休眠。</p>

<div class="callout">
  <div class="h">这一层的方法论价值</div>
  破坏层是「用烘焙换实时计算」的教科书案例：把物理驱动的碎裂动画离线烘成顶点动画贴图（VAT），运行时只按时间采样贴图取顶点，CPU 每帧只推进播放时间。代价是无法运行时改骨骼、无法做动画混合和实时破坏形态变化——但对「碎一次就没了」的碎片来说，这个代价几乎为零。
</div>

<!-- ============================================================ -->
<h2 id="s3"><span class="num">03</span>内核 A：UE4 的 PhysX</h2>

<p><b>本章结论：</b>UE4 时代的物理内核是 NVIDIA PhysX 3——一个由外部维护、以「库」的形式嵌入引擎的求解器。它的边界清晰，代价是每次数据交换都要跨过序列化边界。</p>

<h3>3.1 定位与集成方式</h3>

<p>PhysX 是 NVIDIA 开发、现已开源的物理引擎，用 C++ 编写。它在 UE4 中作为外部库被集成：引擎侧的数据结构需要转换到 PhysX 的表示形式，反过来也要转回来。这个「反复序列化 / 反序列化」是 PhysX 路径的固有开销。</p>

<h3>3.2 求解范围</h3>

<p>PhysX 时代的能力是<b>分模块的</b>：</p>
<ul>
  <li><b>刚体</b>：PhysX 核心</li>
  <li><b>布料</b>：NvCloth / Apex Cloth</li>
  <li><b>破坏</b>：Apex Destructible（对应 UE 的 Destructible Mesh 资产）</li>
  <li><b>载具</b>：PhysX Vehicle</li>
</ul>
<p>这些模块不共享同一个物理世界，跨模块交互（比如布料挂在运动的刚体上并影响刚体）需要额外处理。</p>

<h3>3.3 物理 Tick 模型</h3>

<p>PhysX 时代的物理更新<b>严重依赖渲染帧率</b>：掉帧会导致物理步长变化，进而使物理表现不一致。这对单机游戏尚可接受，对依赖精确物理同步的多人网络玩法是结构性缺陷。</p>

<h3>3.4 手游语境下的意义</h3>

<p>大量现役热门 UE 手游仍在 UE4 上，因此它们的物理内核就是 PhysX：</p>
<ul>
  <li>《和平精英》— UE4，腾讯光子自研 <span class="src">S11</span></li>
  <li>《暗区突围》— UE4 深度定制 <span class="src">S14</span></li>
  <li>《三角洲行动》多人模式（含手机端）— UE4 <span class="src">S8</span></li>
  <li>《幻塔》— UE4.26，Epic 官方开发者访谈确认 <span class="src">S15</span></li>
  <li>《鸣潮》— UE4 <span class="src">S16</span></li>
</ul>
<p>换句话说：<b>今天手机上跑的 UE 手游，物理内核绝大多数仍然是 PhysX</b>。这一代产品是在 Chaos 成熟之前立项的。</p>

<!-- ============================================================ -->
<h2 id="s4"><span class="num">04</span>内核 B：UE5 的 Chaos</h2>

<p><b>本章结论：</b>Chaos 是 Epic 自研、用 UE 原生 C++ 写进引擎内部的统一解算器。它的设计目标不止于「替代 PhysX 的刚体」，而是为网络物理、大世界坐标（LWC）、载具和破坏提供一套统一底座。</p>

<h3>4.1 版本节点</h3>
<table>
<thead><tr><th style="width:20%">版本</th><th>事件</th></tr></thead>
<tbody>
<tr><td>UE4.23</td><td>Chaos 首次引入（实验性）</td></tr>
<tr><td>UE4.26 / 4.27</td><td>可作为选项启用</td></tr>
<tr><td><b>UE5.0</b></td><td><b>成为默认物理引擎</b>，PhysX 被标记弃用</td></tr>
<tr><td><b>UE5.1</b></td><td><b>PhysX 支持彻底移除</b></td></tr>
</tbody>
</table>

<div class="callout warn">
  <div class="h">版本口径存在分歧，按保守理解</div>
  社区文档记为「5.0 弃用、5.1 移除」<span class="src">S2</span>；另有第三方补丁说明称 UE5 中 PhysX「已被完全移除」<span class="src">S4</span>。<b>稳妥结论：UE5 中 PhysX 已不可用</b>，具体在 5.0 还是 5.1 落地不影响工程决策。凡看到基于 UE5 的 PhysX 方案，先怀疑其版本前提。
</div>

<h3>4.2 Epic 官方给出的设计目标</h3>

<p>Epic 物理工程团队在 UE5.0 技术博客中明确了三点目标 <span class="src">S1</span>：</p>
<ol>
  <li>为<b>网络物理</b>（network physics）打基础</li>
  <li>支持<b>大世界坐标（LWC）</b></li>
  <li>支撑<b>载具与破坏</b>——同时<b>在所有既有刚体用例上保持与 UE4 相当的性能</b></li>
</ol>

<p>第二点带来的代价必须单独记住：<b>LWC 意味着双精度浮点</b>，而双精度在移动端 CPU 上是实打实的开销。Epic 自己把官方实测中部分测试变慢归因于此 <span class="src">S1</span>。</p>

<h3>4.3 统一解算器：Chaos 到底统一了什么</h3>

<p>Chaos 把原先各自为政的模块收进同一套解算框架：</p>
<table>
<thead><tr><th style="width:24%">子系统</th><th>说明</th></tr></thead>
<tbody>
<tr><td>刚体 + 约束</td><td>基础求解</td></tr>
<tr><td>Chaos Cloth</td><td>布料，PBD / XPBD 求解器</td></tr>
<tr><td>毛发 / Groom</td><td>与 Chaos 耦合</td></tr>
<tr><td>Chaos Vehicle</td><td>载具；5.5 加 CMVS 模块化载具</td></tr>
<tr><td>Chaos Destruction</td><td>Geometry Collection，替代 Destructible Mesh</td></tr>
<tr><td>场（Fields）</td><td>基于字段的力场控制，驱动破坏与形变</td></tr>
<tr><td>网络物理</td><td>UE5.4 以 <b>Beta</b> 形式提供客户端预测（rewind / resim）；5.5 加 Chaos Visual Debugger <span class="src">S5</span></td></tr>
</tbody>
</table>
<p>「统一」的实际含义是：刚体、布料、毛发、载具、破坏可以在同一物理世界内自然交互，而不需要跨模块桥接。</p>

<h3>4.4 异步物理 Tick 与固定步长</h3>

<p>Chaos 引入<b>固定步长的异步物理线程</b>，使物理计算与渲染帧率解耦。这是它相对 PhysX 最实质的架构改进，也是网络物理能成立的前提。</p>

<div class="callout warn">
  <div class="h">但异步物理有两个必须知道的坑</div>
  <ul style="margin:6px 0">
    <li><b>回调仍在游戏线程</b>：<code>AsyncPhysicsTickComponent</code> 的回调实际上每步都在 Game Thread 上跑（经 <code>FPhysicsSolverFrozenGTPreSimCallbacks</code> / <code>FAsyncPhysicsTickCallback::OnPreSimulate_Internal</code>），只有求解器本身是异步的 <span class="src">S17</span>。</li>
    <li><b>不要中途直读物理状态</b>：游戏线程与物理线程跑在不同 sim step 上，在 <code>Tick</code> 里直接取 <code>GetComponentVelocity()</code> 可能拿到陈旧或插值中的值。输入要用 <code>FAsyncPhysicsTimestamp</code> 标记后 marshal 进仿真，而不是从 Tick 直接改刚体 <span class="src">S17</span>。</li>
  </ul>
</div>

<h3>4.5 确定性：不要指望它</h3>

<p>Chaos <b>不是完全确定性的</b>，尤其在跨线程和跨平台时（x86 与 ARM 的浮点差异、并行归约的求和顺序）。Epic 论坛的官方口径是：<b>不要追求 100% 确定性</b>，只有基于输入的 lockstep 帧同步才严格要求 <span class="src">S5</span>。</p>

<p>已知的非确定性来源包括：<code>AActor::ReplicatedMovement</code> 的变换量化、以及 rewind 时摩擦点被清除 <span class="src">S5</span>。工程上可以用 <code>FPBDRigidsSolver::SetIsDeterministic</code> 或开启 rewind 捕获来改善（同机 authority / auto-proxy 之间可做到 100%），代价是<b>关闭部分 SIMD 重排与并行归约，性能损失 10%~30%</b> <span class="src">S5</span>。</p>

<p><b>实务结论：</b>依赖确定性物理的网络玩法应当设计成服务器权威或非物理方案，而不是把赌注压在 Chaos 的浮点一致性上。</p>

<!-- ============================================================ -->
<h2 id="s5"><span class="num">05</span>两代内核的差异对比</h2>

<p><b>本章结论：</b>UE4 到 UE5 的物理变化不是「性能调优」，而是<b>内核替换</b>——集成方式、Tick 模型、求解范围、确定性、载具实现全部改变。迁移时的最大风险不在性能，而在行为不等价。</p>

{{FIG2}}

<h3>5.1 官方实测：Chaos 并非全面更快</h3>

<p>Epic 在 UE5.0 技术博客中对比了使用 PhysX 3 的预发布版本（代号 Teal）与使用 Chaos 的 UE5 发布版（代号 Blue）<span class="src">S1</span>：</p>

{{FIG3}}

<p><b>怎么读这张图：</b>Chaos 在<b>扫掠（Sweep）类查询</b>上大幅领先（2.6~4 倍），但在<b>重叠检测与射线检测</b>上反而更慢。这恰好解释了 §02 的结论——角色移动大量依赖扫掠，所以 CMC 在 UE5 上未必变慢；而<b>频繁做重叠查询和射线检测的玩法系统</b>（拾取判定、命中检测、视野检查、AI 感知）才是真正的受害者。</p>

<div class="callout warn">
  <div class="h">对「慢 10~100 倍」这类说法的判断</div>
  该说法源自 2022 年 UE5 EA 阶段开发者的个人测试，<b>不代表当前版本表现</b>。同期官方数据是互有胜负。引用任何 Chaos 性能结论时，先确认它的版本前提——Chaos 在 5.4 之后有大量改进，社区普遍认为 5.4 相比早期版本「好得多」 <span class="src">S10</span>。
</div>

<h3>5.2 迁移不兼容清单</h3>

<table>
<thead><tr><th style="width:26%">项目</th><th>UE4 (PhysX)</th><th>UE5 (Chaos)</th><th style="width:16%">迁移难度</th></tr></thead>
<tbody>
<tr><td>载具</td><td>PhysX Vehicle</td><td>Chaos Vehicle</td><td><span class="tag t-bad">高</span> 两类完全独立，不能直接改父类 <span class="src">S3</span></td></tr>
<tr><td>破坏资产</td><td>Destructible Mesh (Apex)</td><td>Geometry Collection</td><td><span class="tag t-bad">高</span> 资产需重做</td></tr>
<tr><td>布料</td><td>NvCloth / Apex Cloth</td><td>Chaos Cloth</td><td><span class="tag t-bad">高</span> 参数模型不同，部分旧参数无对应</td></tr>
<tr><td>约束行为</td><td>Sequential Impulse</td><td>PBD / XPBD</td><td><span class="tag t-note">中</span> 数学上不等价，高速碰撞下表现差异明显 <span class="src">S10</span></td></tr>
<tr><td>刚体 API</td><td>PhysX 接口</td><td>Chaos 接口</td><td><span class="tag t-note">中</span> 高层 API 兼容，底层自定义代码需重写</td></tr>
</tbody>
</table>

<div class="callout note">
  <div class="h">行为不等价的真实例子</div>
  社区反馈：一个从 UE4 迁移到 UE5 的摩托车项目，在高速碰撞时物理约束的 projection 距离比 UE4 时代大得多 <span class="src">S10</span>。这类问题不会报错、不会崩溃，只会「手感变了」——排查成本极高。<b>物理迁移必须做行为回归测试，不能只看功能是否跑通。</b>
</div>

<!-- ============================================================ -->
<h2 id="s6"><span class="num">06</span>贯穿主线：一个受击布娃娃的一生</h2>

<p><b>本章结论：</b>把前几章散落的环节串起来——一次物理事件从服务器判定到客户端出画面，跨过了客户端 / 服务器两侧、两个物理世界实例、一次网络复制。理解这条链路，才能判断「物理开销该砍在哪」。</p>

{{FIG4}}

<h3>逐步说明</h3>

<table>
<thead><tr><th style="width:20%">阶段</th><th style="width:30%">发生了什么</th><th>关键约束</th></tr></thead>
<tbody>
<tr>
  <td><b>① 命中判定</b></td>
  <td>服务器权威判定本帧是否致死 / 触发 ragdoll</td>
  <td>判定必须在服务器，客户端只做表现。否则就是一个可作弊的入口。</td>
</tr>
<tr>
  <td><b>② 物理场景注册 + 求解</b></td>
  <td>12~20 个胶囊体注册进 <code>UWorld</code> 物理场景，Chaos 以固定步长 1/30~1/60 在异步线程求解</td>
  <td>这是本链路唯一的重 CPU 环节。步长越大越省，但稳定性越差；<code>MaxSubsteps</code> 必须设上限，否则慢帧会触发「死亡螺旋」。</td>
</tr>
<tr>
  <td><b>③ 状态复制</b></td>
  <td>位置 + 速度量化后按约 20Hz 下发</td>
  <td>复制频率远低于求解频率（20Hz vs 60Hz），差额由客户端插值补上。带宽与平滑度的取舍点。</td>
</tr>
<tr>
  <td><b>④ 客户端表现</b></td>
  <td>插值补帧；预测失误时回滚重放；物理骨架映射回渲染骨架</td>
  <td>回滚重放的成本与「重放窗口长度」成正比，窗口越长越贵。</td>
</tr>
</tbody>
</table>

<div class="callout">
  <div class="h">这条主线揭示的核心事实</div>
  <b>服务器与客户端各跑一份 UWorld 物理场景实例</b>，两者之间靠<b>复制 + 回滚</b>对齐，而不是靠确定性重放。这正是 Chaos 不需要完全确定性的原因，也是「服务器权威」路径能成立的基础。反过来，如果选择帧同步路径，两侧就必须逐位一致——而 Chaos 做不到，这就是 §09 要展开的取舍。
</div>

<!-- ============================================================ -->
<h2 id="s7"><span class="num">07</span>案例：热门 UE 手游实际用什么</h2>

<p><b>本章结论：</b>公开可查的资料里，<b>手机上跑的 UE 手游仍以 UE4 + PhysX 为主</b>；UE5 手游在 2026 年才成规模上线，且普遍在移动端做了大幅降级。</p>

<h3>7.1 最有信息量的一个案例</h3>

<div class="callout ok">
  <div class="h">《三角洲行动》：同一个 IP，手机端留 UE4，UE5 只给 PC</div>
  多人模式（PC / 主机 / <b>手机</b>）统一用 <b>UE4</b>；单人战役「黑鹰坠落」用 <b>UE5</b>，官方明确表示<b>因技术限制不会上线手机平台</b>，原因是该模式采用 UE5 打造、硬件要求过高 <span class="src">S8</span><span class="src">S9</span>。有资料指出，多人模式统一 UE4 的考量之一正是稳定性，以及手机端对 UE5 的承载与优化能力有限 <span class="src">S9</span>。
  <br><br>
  这比任何技术分析都直接：<b>一线团队在同一款产品里同时持有两套引擎，并且把 UE5 划在了手机之外。</b>
</div>

<h3>7.2 案例总表</h3>

<table>
<thead><tr><th style="width:17%">游戏</th><th style="width:15%">引擎</th><th style="width:12%">物理内核</th><th>物理相关做法与公开信息</th></tr></thead>
<tbody>
<tr>
  <td><b>和平精英</b><br><span class="tag t-grey">PUBG Mobile</span></td>
  <td>UE4</td><td>PhysX</td>
  <td>腾讯光子自研。官方强调载具「专业引擎物理方案」——每种载具独立加速特点、引擎动力曲线、减震能力 <span class="src">S11</span>。GDC 2023 分享过 ML 驱动的全身物理拟真交互 <span class="src">S18</span>。</td>
</tr>
<tr>
  <td><b>三角洲行动</b></td>
  <td>多人 UE4<br>战役 UE5</td><td>PhysX<br>(多人)</td>
  <td>移动端走 UE4；GDC 分享提到跨端做「Physics data extraction and merge (DS opt)」物理数据抽取合并 <span class="src">S19</span>。</td>
</tr>
<tr>
  <td><b>暗区突围</b></td>
  <td>UE4 深度定制</td><td>PhysX</td>
  <td>GDC 2022/2023/2024 三场分享；144FPS 帧预测、移动端硬件光追等 <span class="src">S14</span>。</td>
</tr>
<tr>
  <td><b>幻塔</b></td>
  <td>UE4.26</td><td>PhysX</td>
  <td>Epic 官方开发者访谈：物理用于场景交互解谜；官方建议移动端关注面数、Drawcall、内存与美术资源优化 <span class="src">S15</span>。</td>
</tr>
<tr>
  <td><b>鸣潮</b></td>
  <td>UE4</td><td>PhysX</td>
  <td>非自研物理。被指瓶颈在 GameThread——逻辑、物理同步、VFX、AI 共享同一份线程预算 <span class="src">S16</span>。</td>
</tr>
<tr>
  <td><b>无限暖暖</b></td>
  <td><b>UE5</b></td><td><b>Chaos</b></td>
  <td>用 <b>Chaos Cloth + 骨骼链</b>做服装物理；二次开发 OIT 解决透明衣排序。移动端靠材质合并（15→3 个材质 ID）、MeshGrass、HLOD（22 万面→5 万面）、大世界无缝加载硬扛 <span class="src">S20</span><span class="src">S21</span>。但「卡顿」「优化欠佳」仍是高频吐槽，且因实时布料解算而不支持攀爬、游泳 <span class="src">S21</span>。</td>
</tr>
<tr>
  <td><b>异环</b></td>
  <td><b>UE5</b></td><td><b>Chaos</b></td>
  <td>完美世界 Hotta Studio（幻塔团队），2026-04 全平台公测，针对骁龙 8 Gen3 做帧率与能效优化 <span class="src">S22</span>。</td>
</tr>
<tr>
  <td><b>王者荣耀世界</b></td>
  <td><b>UE5</b></td><td><b>Chaos</b></td>
  <td>腾讯天美，2026-04 上线；移动端要求骁龙 865 / iPhone 11 以上 <span class="src">S22</span>。</td>
</tr>
</tbody>
</table>

<div class="callout note">
  <div class="h">关于「某厂手游用 Havok」的说法</div>
  公开资料中，Havok 的代表作集中在主机 / PC 3A（刺客信条、DOOM Eternal、怪物猎人世界），<b>未见其作为主力物理方案出现在国内 UE 手游的技术分享中</b> <span class="src">S23</span>。它的优势是经典 Sequential Impulse 求解器带来的确定性与稳定性，但对 UE 项目而言集成与授权成本高。任何「某 UE 手游用 Havok」的说法都需要具体来源佐证，<span class="tag t-note">待确认</span>。
</div>

<!-- ============================================================ -->
<h2 id="s8"><span class="num">08</span>移动端预算与六个优化杠杆</h2>

<p><b>本章结论：</b>移动端物理的优化空间不在「换求解器」，而在「让更少的对象进求解器」。六个杠杆里，工程改动见效快但收益有限，架构决策收益最大但改得最晚。</p>

{{FIG5}}

<h3>8.1 为什么不能照搬端游物理配置</h3>

<p>物理开销与<b>动态刚体数量</b>近似线性相关，而移动端 CPU 单核性能与内存带宽都远低于 PC。更麻烦的是帧预算本身：30fps 下只有 33.3ms，60fps 下只有 16.6ms。端游 100ms 里占 5% 的物理，搬到手游 33.3ms 里就变成 15%。</p>

<h3>8.2 碰撞体复杂度是第一杠杆</h3>

<p>碰撞检测成本从高到低的排序（这份排序在 Unity / UE / 自研引擎上是通用的）：</p>

<table>
<thead><tr><th style="width:34%">碰撞体类型</th><th>相对成本</th><th>适用</th></tr></thead>
<tbody>
<tr><td>三角网格（Triangle Mesh）</td><td><span class="tag t-bad">最高</span></td><td>移动端应避免用于动态物体</td></tr>
<tr><td>凸包（Convex Hull）</td><td><span class="tag t-note">高</span></td><td>少量关键物体</td></tr>
<tr><td>胶囊体（Capsule）</td><td><span class="tag t-ok">低</span></td><td>角色、布娃娃肢体</td></tr>
<tr><td>球体 / 盒体 / 平面 / 点</td><td><span class="tag t-ok">最低</span></td><td>布料碰撞体（官方也推荐球体与锥形胶囊）<span class="src">S12</span></td></tr>
</tbody>
</table>

<h3>8.3 物理与视觉解耦</h3>

<p>核心思路：<b>「碰的」和「看的」不是同一个东西</b>。默认情况下 Mesh 一旦打开碰撞，就会把物理对象注册进物理场景，增加场景复杂度与内存占用。工程上可以按实际需要的交互距离管理物理属性，把物理属性转移到独立 Component / Actor，放进加载卸载距离更近的 Streaming Level <span class="src">S24</span>。</p>

<h3>8.4 六个杠杆的优先级</h3>

<table>
<thead><tr><th style="width:8%">序</th><th style="width:20%">杠杆</th><th style="width:16%">性质</th><th>说明</th></tr></thead>
<tbody>
<tr><td>①</td><td>简化碰撞体</td><td><span class="tag t-ok">工程改动</span></td><td>见效最快，成本可降一个数量级</td></tr>
<tr><td>②</td><td>降频与子步进</td><td><span class="tag t-note">配置</span></td><td>60Hz→30Hz 直接减半；<code>MaxSubsteps</code> 必须设上限防空转</td></tr>
<tr><td>③</td><td>休眠与剔除</td><td><span class="tag t-ok">工程改动</span></td><td><code>Sleep Threshold</code> 调 0.1~1.0；屏幕外 / 静止体停止积分</td></tr>
<tr><td>④</td><td>物理 / 视觉解耦</td><td><span class="tag t-ok">工程改动</span></td><td>降内存 + 降场景复杂度</td></tr>
<tr><td>⑤</td><td>VAT / 烘焙替代</td><td><span class="tag t-mob">架构决策</span></td><td>把 CPU 计算搬到离线，代价是失去运行时可变性</td></tr>
<tr><td>⑥</td><td>服务器权威</td><td><span class="tag t-mob">架构决策</span></td><td>只让影响胜负的物体进物理，纯表现物客户端本地模拟</td></tr>
</tbody>
</table>

<!-- ============================================================ -->
<h2 id="s9"><span class="num">09</span>物理的网络同步与确定性</h2>

<p><b>本章结论：</b>帧同步与服务器权威是两条互斥的路径，选择权在项目早期。一旦选了帧同步，就要接受 Chaos 给不了完全确定性这个事实——这是 UE5 手游在竞技品类上的一个结构性约束。</p>

{{FIG6}}

<h3>9.1 两条路径的取舍</h3>

<table>
<thead><tr><th style="width:16%">维度</th><th style="width:42%">帧同步（Lockstep）</th><th>服务器权威 + 预测回滚</th></tr></thead>
<tbody>
<tr><td>同步内容</td><td>只同步玩家输入</td><td>同步权威状态快照</td></tr>
<tr><td>带宽</td><td><span class="tag t-ok">极省</span></td><td><span class="tag t-bad">高</span></td></tr>
<tr><td>服务器开销</td><td><span class="tag t-ok">低</span>（可只做转发与校验）</td><td><span class="tag t-bad">高</span>（跑权威模拟）</td></tr>
<tr><td>确定性要求</td><td><span class="tag t-bad">硬前提</span> 逐位一致</td><td><span class="tag t-ok">不需要</span> 只需「重放一次不再触发二次回滚」</td></tr>
<tr><td>防作弊</td><td><span class="tag t-bad">弱</span> 客户端掌状态，可逆向 APK</td><td><span class="tag t-ok">强</span> 服务器控制状态变化</td></tr>
<tr><td>断线重连</td><td><span class="tag t-bad">代价高</span></td><td><span class="tag t-ok">较易</span></td></tr>
<tr><td>典型场景</td><td>RTS、大规模单位；带宽受限市场</td><td>竞技动作、FPS、物理交互密集</td></tr>
</tbody>
</table>

<h3>9.2 UE5 提供的工具</h3>
<ul>
  <li><b>UnrealJolt</b> 之外的官方路径：<b>UE5.4 网络物理（Beta）</b>，支持客户端预测（rewind / resim 两种模式）<span class="src">S5</span>。</li>
  <li><b>复制模式</b>：Predictive Interpolation（本地即时响应 + 后续服务器校正）与 Resimulation（回滚重放，精度更高但 CPU / 内存开销显著更大）。</li>
  <li><b>UE5.5</b> 增加 Chaos Visual Debugger 初步支持 <span class="src">S5</span>。</li>
</ul>

<h3>9.3 实务建议</h3>
<div class="callout">
  <div class="h">工程侧的四条硬性约定</div>
  <ol style="margin:6px 0">
    <li><b>固定仿真步长</b>，并让客户端与服务器使用同一个值。</li>
    <li><b>随机数种子在两端同步</b>，否则重放结果必然分叉。</li>
    <li><b>限制同时动态刚体的数量</b>，这是物理开销与重放成本的共同分母。</li>
    <li><b>缓存状态历史要按 RTT 估算长度</b>，窗口太短会导致回滚失败，太长则内存爆炸。</li>
  </ol>
</div>

<!-- ============================================================ -->
<h2 id="s10"><span class="num">10</span>能不能换掉 Chaos？</h2>

<p><b>本章结论：</b>工程上不建议，生态上也不可行。UE5 的现状是「Chaos 或没有物理」，第三方集成的定位是共存而非替换。</p>

<table>
<thead><tr><th style="width:22%">方案</th><th style="width:16%">可行性</th><th>说明</th></tr></thead>
<tbody>
<tr>
  <td>回到 UE4 用 PhysX</td><td><span class="tag t-note">可选项</span></td>
  <td>代价是放弃 UE5 的其他能力（Nanite / Lumen / 新渲染特性）。这是产品级取舍，不是技术优化。</td>
</tr>
<tr>
  <td>UnrealJolt（第三方）</td><td><span class="tag t-note">部分可用</span></td>
  <td>把 Jolt 作为子系统加入，用于确定性物理 / 客户端预测 / 回滚。<b>不关闭 Chaos</b>：同一刚体同时被 Jolt 与 Chaos 模拟时会告警。需要手动同步 Actor 位置，适合动态物体少的项目。定位为 pre-release <span class="src">S6</span>。</td>
</tr>
<tr>
  <td>完全替换物理内核</td><td><span class="tag t-bad">不可行</span></td>
  <td>Epic 官方论坛明确表态：Chaos 是引擎未来的物理引擎，创作者做的替代集成不会获得内部支持，部分抽象层代码会逐步移除 <span class="src">S6</span>。</td>
</tr>
<tr>
  <td>绕开物理系统自建</td><td><span class="tag t-note">可行但贵</span></td>
  <td>不启用 <code>Simulate Physics</code>，改用自定义 Component 集成第三方物理库，由子系统驱动步进并同步回 Transform。需要自建整套查询 / 碰撞 / 调试工具链。</td>
</tr>
</tbody>
</table>

<div class="callout warn">
  <div class="h">Jolt 的真实定位</div>
  Jolt Physics 以多核优化、确定性模拟和内存效率著称，被用于《地平线：西之绝境》等作品。但在 UE5 生态里，<b>它不是一个 drop-in 替代品</b>——没有官方插件，社区插件明确定位为「与 Chaos 共存」。若项目真正需要高确定性物理（如帧同步竞技玩法），更务实的做法是绕开引擎物理自建，而不是指望替换内核。
</div>

<!-- ============================================================ -->
<h2 id="s11"><span class="num">11</span>信息可信度排查</h2>

<p><b>本章结论：</b>这个话题的中文资料污染严重。调研过程中检索到的一批「高颗粒度技术文」经核查不可信，此处留档以便后续检索时规避。</p>

<div class="callout warn">
  <div class="h">⚠️ 判定为不可信的内容（请勿引用）</div>
  <p style="margin:6px 0">标题形如「2026 虚幻5 尾毛物理崩溃？腾讯天美技术总监揭秘移动端优化生死局」，出现在 <code>fygamer.cn</code> / <code>biliplay.com.cn</code> 等内容农场站点。证伪理由：</p>
  <ol style="margin:6px 0">
    <li><b>物理常识错误</b>：声称「UE5.4 的 Chaos 物理系统与移动端 <b>TBR 架构</b>存在底层冲突」。TBR（Tile-Based Rendering）是 GPU 的光栅化架构，发生在 GPU 光栅化阶段；Chaos 是 CPU 侧刚体求解器。<b>两者不在同一条执行路径上，不存在「底层冲突」。</b></li>
    <li><b>逻辑链断裂</b>：给出「修改 <code>GroomComponent.cpp</code> 中 <code>bEnablePerStrandCollision</code> 由 true 改 false」。Groom 的 per-strand collision 属于<b>渲染 / 几何</b>侧的开关，不是 Chaos 物理的调用路径，用它来解释「物理查询次数下降」在因果上说不通。</li>
    <li><b>张冠李戴</b>：文中把米哈游《绝区零》列为 Chaos 案例。<b>《绝区零》是 Unity 项目</b>，与 Chaos 无关。这是拼接痕迹最明显的一处。</li>
    <li><b>数字颗粒度可疑</b>：精确到「每帧 8000 次物理查询」「Cache Miss 率 67%」「11ms 降至 0.8ms」这类数据，在真实技术分享中都会附带测试条件与工具来源，而该文没有。</li>
  </ol>
</div>

<h3>11.1 来源可信度分级</h3>

<table>
<thead><tr><th style="width:14%">级别</th><th style="width:30%">来源类型</th><th>本次调研中的实例</th></tr></thead>
<tbody>
<tr><td><span class="tag t-ok">A 级</span></td><td>Epic 官方博客 / 官方文档 / Unreal Fest / GDC 官方议程</td><td>Chaos 技术博客、UE5.0 Release Notes、Epic 官方开发者访谈、Epic 官方移动端支持表、Unreal Fest 2025 布料指标</td></tr>
<tr><td><span class="tag t-ok">B 级</span></td><td>Epic 官方论坛开发者讨论（含 Epic 员工回复）</td><td>Chaos 性能讨论、确定性讨论、Jolt 支持表态、异步物理 Tick 线程归属</td></tr>
<tr><td><span class="tag t-note">C 级</span></td><td>游戏媒体 / 行业媒体报道</td><td>三角洲引擎选择、异环与王者荣耀世界上线信息、无限暖暖技术报道</td></tr>
<tr><td><span class="tag t-bad">D 级</span></td><td>内容农场 / 无署名技术文 / 数据无出处的「揭秘」</td><td>上文列举的尾毛物理崩溃系列</td></tr>
</tbody>
</table>

<h3>11.2 三个需要警惕的表述</h3>
<ul>
  <li><b>「Chaos 比 PhysX 慢 N 倍」</b>——若未标注版本与测试条件，不可作为当前结论。官方数据是互有胜负。</li>
  <li><b>「Chaos 是确定性的」</b>——错误。Epic 官方明确表示不要追求 100% 确定性。</li>
  <li><b>「某手游用 Havok / 自研物理引擎」</b>——需具体来源。国内 UE 手游的自研集中在<b>角色移动与工具链</b>，而非替换物理内核。</li>
</ul>

<!-- ============================================================ -->
<h2 id="s12"><span class="num">12</span>对 UE5.5 分叉项目的含义 + Checklist</h2>

<p><b>本章结论：</b>对基于 UE5.5 的移动端项目，物理侧的可选空间已经被引擎锁定在 Chaos 之内，剩下的是「分配多少预算给物理」的工程决策。</p>

<h3>12.1 可用能力边界</h3>

<table>
<thead><tr><th style="width:24%">能力</th><th style="width:18%">移动端可用性</th><th>工程含义</th></tr></thead>
<tbody>
<tr><td>刚体 + 约束 + ragdoll</td><td><span class="tag t-ok">可用</span></td><td>需简化碰撞体、降频、限时限量</td></tr>
<tr><td>场景查询（扫掠 / 重叠 / 射线）</td><td><span class="tag t-ok">可用</span></td><td><b>注意</b>：Overlaps 与 Raycasts 在 Chaos 下比 PhysX 慢，高频调用的玩法系统要单独profile</td></tr>
<tr><td>Chaos Cloth</td><td><span class="tag t-note">有限</span></td><td>≈1ms / 角色、200~600 顶点、PBD；仅高配档</td></tr>
<tr><td>Chaos Destruction</td><td><span class="tag t-bad">需降级</span></td><td>实时断裂不可行，走预制分块 + VAT</td></tr>
<tr><td>Groom 毛发物理</td><td><span class="tag t-bad">不建议</span></td><td>移动端几乎必然降级，代价与收益不成比例</td></tr>
<tr><td>网络物理（预测回滚）</td><td><span class="tag t-note">Beta</span></td><td>UE5.4 起为 Beta；上生产前需自行验证稳定性</td></tr>
</tbody>
</table>

<h3>12.2 Checklist</h3>

<div class="panel">
<h4>物理方案评审清单</h4>
<ul>
  <li>☐ 列出全部会进物理求解器的对象类型，估出<b>同屏动态刚体峰值</b>（这是物理开销的唯一有效分母）</li>
  <li>☐ 核对每个动态刚体的碰撞体复杂度，确认没有三角网格碰撞体用在动态物体上</li>
  <li>☐ 确认物理 tick 频率（建议 30Hz）与 <code>MaxSubsteps</code> 上限已设置，且 <code>MaxSubstepDeltaTime</code> 明确</li>
  <li>☐ 确认 <code>Sleep Threshold</code> 已调优，静止物体能正确休眠</li>
  <li>☐ 确认屏幕外 / 远距离对象被剔除出物理模拟</li>
  <li>☐ 确认布料模拟的顶点数在 200~600 区间，且用 PBD 而非 XPBD</li>
  <li>☐ 破坏 / 碎片走的是烘焙路径而非实时 Geometry Collection</li>
  <li>☐ 网络路径已明确二选一（帧同步 / 服务器权威），且没有在帧同步路径上依赖 Chaos 的确定性</li>
  <li>☐ 若使用异步物理：输入已通过 <code>FAsyncPhysicsTimestamp</code> marshal，没有在 Tick 里直读物理状态</li>
  <li>☐ 若从 UE4 迁移：载具、破坏、布料资产已完成重做，且做过<b>行为回归测试</b>而非仅功能测试</li>
  <li>☐ 高频 Scene Query 的玩法系统（拾取、命中、AI 感知）已单独 profile，已知晓 Chaos 在 Overlaps / Raycasts 上的额外成本</li>
</ul>
</div>

<h3>12.3 一句话总结</h3>
<div class="callout ok">
  移动端物理的胜负手不在「选哪个物理引擎」——UE5 只有 Chaos——而在<b>「让多少东西进物理世界」</b>与<b>「哪些物理工作可以离线烘焙或搬到服务器」</b>。角色移动和破坏这两层占了玩法感知的大头，却几乎不吃引擎物理预算；真正需要精打细算的是载具、布娃娃，以及那些高频做重叠与射线查询的判定系统。
</div>

<!-- ============================================================ -->
<h2 id="s13"><span class="num">13</span>参考来源</h2>

<table>
<thead><tr><th style="width:8%">编号</th><th style="width:34%">来源</th><th>链接</th></tr></thead>
<tbody>
<tr><td>S1</td><td>Epic 官方技术博客 · Chaos Scene Queries and Rigid Body Engine in UE5 <span class="tag t-ok">A</span></td><td><a href="https://www.unrealengine.com/tech-blog/chaos-scene-queries-and-rigid-body-engine-in-ue5">unrealengine.com/tech-blog/chaos-scene-queries-and-rigid-body-engine-in-ue5</a></td></tr>
<tr><td>S2</td><td>Unreal Community Wiki · UE5 Engine Changes <span class="tag t-note">B</span></td><td><a href="https://unrealcommunity.wiki/616489e95229ed20458b09fe">unrealcommunity.wiki/616489e95229ed20458b09fe</a></td></tr>
<tr><td>S3</td><td>Epic 官方文档 · 虚幻引擎中的载具（PhysX → Chaos 迁移）<span class="tag t-ok">A</span></td><td><a href="https://dev.epicgames.com/documentation/unreal-engine/vehicles-in-unreal-engine">dev.epicgames.com/documentation/unreal-engine/vehicles-in-unreal-engine</a></td></tr>
<tr><td>S4</td><td>UE5.0 Release Notes / 第三方补丁说明 <span class="tag t-note">B</span></td><td><a href="https://dev.epicgames.com/documentation/unreal-engine/unreal-engine-5.0-release-notes">dev.epicgames.com/documentation/unreal-engine/unreal-engine-5.0-release-notes</a></td></tr>
<tr><td>S5</td><td>Epic 官方论坛 · Chaos 确定性 / 异步物理 / 网络物理讨论 <span class="tag t-ok">B</span></td><td><a href="https://forums.unrealengine.com/t/chaos-async-not-deterministic-enough-for-network-prediction/729633">forums.unrealengine.com/t/chaos-async-not-deterministic-enough-for-network-prediction/729633</a></td></tr>
<tr><td>S6</td><td>Epic 官方论坛 · Future of Chaos, PhysX and other physics engines <span class="tag t-ok">B</span></td><td><a href="https://forums.unrealengine.com/t/future-of-chaos-physx-and-other-physics-engines/2610544">forums.unrealengine.com/t/future-of-chaos-physx-and-other-physics-engines/2610544</a></td></tr>
<tr><td>S7</td><td>Epic 社区教程 · UE5.5 Chaos Modular Vehicle on Android <span class="tag t-ok">A</span></td><td><a href="https://dev.epicgames.com/community/learning/tutorials/zjKP/unreal-engine-5-5-chaos-modular-vehicle-on-android-real-time-vehicle-destruction-touch-controls">dev.epicgames.com/community/learning/tutorials/zjKP</a></td></tr>
<tr><td>S8</td><td>腾讯新闻 · 三角洲行动官宣上线 <span class="tag t-note">C</span></td><td><a href="https://news.qq.com/rain/a/20240910A058ML00">news.qq.com/rain/a/20240910A058ML00</a></td></tr>
<tr><td>S9</td><td>9game · 三角洲行动用的是什么引擎 <span class="tag t-note">C</span></td><td><a href="https://a.9game.cn/sjzxd/10403623.html">a.9game.cn/sjzxd/10403623.html</a></td></tr>
<tr><td>S10</td><td>Epic 官方论坛 · Is Chaos Physics performance good now? / Chaos Vehicles 生产可用性 <span class="tag t-ok">B</span></td><td><a href="https://forums.unrealengine.com/t/is-chaos-physics-performance-good-now/680327">forums.unrealengine.com/t/is-chaos-physics-performance-good-now/680327</a></td></tr>
<tr><td>S11</td><td>《和平精英》策划面对面 / 4399 报道（载具物理口径）<span class="tag t-note">C</span></td><td><a href="https://news.4399.com/xinwen/pubgsy/m/820928.html">news.4399.com/xinwen/pubgsy/m/820928.html</a></td></tr>
<tr><td>S12</td><td>Unreal Fest 2025 · Chaos Cloth 移动端目标与配置建议 <span class="tag t-ok">A</span></td><td>Epic Games Japan · UE5 Mobile Game Development Essentials（见 S13 同源）</td></tr>
<tr><td>S13</td><td>Epic Games Japan · UE5 Mobile Game Development Essentials 2025（移动端能力支持表）<span class="tag t-ok">A</span></td><td><a href="https://docswell.com/s/EpicGamesJapan/KN923Q-2025-10-07-120717">docswell.com/s/EpicGamesJapan/KN923Q-2025-10-07-120717</a></td></tr>
<tr><td>S14</td><td>GDC 2022/2023/2024 · 暗区突围技术分享 <span class="tag t-ok">A</span></td><td>GDC Vault（多场次）</td></tr>
<tr><td>S15</td><td>Epic 官方开发者访谈 · 幻塔（UE4.26 多平台）<span class="tag t-ok">A</span></td><td><a href="https://enterprise.unrealengine.com/zh-CN/developer-interviews/tower-of-fantasy-leverages-unreal-engine-to-bring-open-world-action-to-mobile-pc-and-playstation">enterprise.unrealengine.com/zh-CN/developer-interviews/tower-of-fantasy</a></td></tr>
<tr><td>S16</td><td>行业报道 · 鸣潮 UE4 跨平台与 GameThread 瓶颈 <span class="tag t-note">C</span></td><td><a href="http://www.gamelook.com.cn/2022/07/489949/">gamelook.com.cn/2022/07/489949</a></td></tr>
<tr><td>S17</td><td>Epic 官方论坛 · Is Async Physics Tick really async? <span class="tag t-ok">B</span></td><td><a href="https://forums.unrealengine.com/t/is-async-physics-tick-really-async/2353151">forums.unrealengine.com/t/is-async-physics-tick-really-async/2353151</a></td></tr>
<tr><td>S18</td><td>GDC 2023 · 腾讯光子自研技术分享 <span class="tag t-note">C</span></td><td><a href="http://www.gamelook.com.cn/2023/03/513282/">gamelook.com.cn/2023/03/513282</a></td></tr>
<tr><td>S19</td><td>GDC · Delta Force: Techniques From Unified Production Pipeline To Cross-Platform Runtime Support <span class="tag t-ok">A</span></td><td><a href="https://gdcvault.com/play/1034827/">gdcvault.com/play/1034827</a></td></tr>
<tr><td>S20</td><td>游戏日报 · 叠纸出席全球技术大会（无限暖暖 UE5 技术）<span class="tag t-note">C</span></td><td><a href="http://news.yxrb.net/2025/0930/6044.html">news.yxrb.net/2025/0930/6044.html</a></td></tr>
<tr><td>S21</td><td>游侠网 / DoNews · 无限暖暖公测与技术分析 <span class="tag t-note">C</span></td><td><a href="https://3g.ali213.net/news/html/887983.html">3g.ali213.net/news/html/887983.html</a></td></tr>
<tr><td>S22</td><td>17173 / 澎湃 · 异环与王者荣耀世界 UE5 上线信息 <span class="tag t-note">C</span></td><td><a href="https://news.17173.com/content/03052026/101509424.shtml">news.17173.com/content/03052026/101509424.shtml</a></td></tr>
<tr><td>S23</td><td>游戏物理引擎比较：架构、特性、性能、集成 <span class="tag t-note">C</span></td><td><a href="https://pps43.github.io/posts/notes_on_physics_engine/">pps43.github.io/posts/notes_on_physics_engine</a></td></tr>
<tr><td>S24</td><td>UE4 多人大地形优化（物理与视觉对象解耦）<span class="tag t-note">C</span></td><td><a href="https://www.shuzhiduo.com/A/WpdKaMyNJV/">shuzhiduo.com/A/WpdKaMyNJV</a></td></tr>
</tbody>
</table>

<footer>
  本文为公开资料调研整理，非引擎源码分析，因此不含 <code>文件名:行号</code> 式引用；每条结论以来源编号回溯。<br>
  凡公开资料无法证实的判断均已标注 <span class="tag t-note">待确认</span>。引擎行为以实际版本实测为准。<br>
  制表：2026-09-28 · 存放于 AiDoc 知识库
</footer>

</div>
"""


def main():
    html = ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            '<title>UE 手游物理方案全解 — PhysX 到 Chaos / 移动端五层分解 / UE4-UE5 差异</title>\n'
            '<style>' + CSS + '</style>\n</head>\n<body>\n')

    body = BODY
    for i in range(1, 7):
        svg = open(os.path.join(FIGDIR, 'fig%d_%s.svg' % (i, FIGS[i])), encoding='utf-8').read()
        caps = {
            1: '图 1 · 手游「物理」的五层分解 —— 同一个词，五套完全不同的实现',
            2: '图 2 · UE4/PhysX 与 UE5/Chaos 的内核差异 —— 集成方式、Tick 模型、求解范围、确定性、载具',
            3: '图 3 · Epic 官方实测：Chaos 并非全面更快（数据来源 <span class="src">S1</span>）',
            4: '图 4 · 一个受击布娃娃的一生 —— 服务器与客户端两侧的完整链路，箭头标注传递的数据',
            5: '图 5 · 移动端物理预算与六个优化杠杆',
            6: '图 6 · 物理的网络同步：帧同步与服务器权威两条互斥路径',
        }
        fig = ('<div class="fig">' + svg + '<div class="cap">' + caps[i] + '</div></div>')
        body = body.replace('{{FIG%d}}' % i, fig)

    html += body + '\n</body>\n</html>\n'

    out = os.path.join(FIGDIR, OUTNAME)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print('已生成: %s  (%.1f KB)' % (out, len(html.encode('utf-8')) / 1024))


FIGS = {1: 'overview', 2: 'kernel', 3: 'perf', 4: 'ragdoll_life', 5: 'budget', 6: 'netcode'}

if __name__ == '__main__':
    main()
