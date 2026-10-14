#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《UE 手游物理方案》文档的 6 张 SVG 图。

原语库 + 每图一个函数。所有 <text> 统一按  x → y → font-size → text-anchor
的属性顺序输出，保证 svg_lint.py 的正则能解析到。
"""
import os

OUT = os.path.dirname(os.path.abspath(__file__))

FONT = "'PingFang SC','Microsoft YaHei','Segoe UI',sans-serif"
MONO = "'Cascadia Mono',Consolas,monospace"

INK = '#1a1d21'; INK2 = '#4a5158'; INK3 = '#7a828a'
LINE = '#e2e6ea'; PANEL = '#ffffff'; BG = '#f7f8fa'
PC = '#2f6feb'; CHAOS = '#8e44ad'; MOB = '#d9741a'
OK = '#1f9254'; WARN = '#c0392b'; NOTE = '#8a6d1f'
PC_BG = '#e8f0fe'; CHAOS_BG = '#f5ebfa'; MOB_BG = '#fdf0e4'
OK_BG = '#e6f6ec'; WARN_BG = '#fdeaea'; NOTE_BG = '#fdf8e8'
GREY_BG = '#eef1f4'


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def tw(s, fs):
    """与 svg_lint.py 一致的粗估字宽。"""
    cjk = sum(1 for c in s if 0x2E80 <= ord(c) <= 0x9FFF or 0xFF00 <= ord(c) <= 0xFFEF)
    return cjk * fs + (len(s) - cjk) * fs * 0.56


def text(x, y, s, fs=12, anchor='start', fill=INK, weight='normal', mono=False):
    f = MONO if mono else FONT
    return (f'<text x="{x}" y="{y}" font-size="{fs}" text-anchor="{anchor}" '
            f'fill="{fill}" font-weight="{weight}" font-family="{f}">{esc(s)}</text>')


def ctext(cx, y, s, fs=12, fill=INK, weight='normal'):
    return text(cx, y, s, fs, 'middle', fill, weight)


def rect(x, y, w, h, fill='#fff', stroke=LINE, r=8, sw=1.2):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def line(x1, y1, x2, y2, color=INK3, w=1.4, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}"{d}/>'


MARKERS = ''.join(
    f'<marker id="{k}" markerWidth="9" markerHeight="9" refX="7.4" refY="4" orient="auto">'
    f'<path d="M0,0 L8,4 L0,8 z" fill="{c}"/></marker>'
    for k, c in (('a', INK3), ('ab', PC), ('ac', CHAOS), ('ao', OK), ('aw', WARN), ('am', MOB)))

DEFS = '<defs>' + MARKERS + '</defs>'


def svg_open(w, h, bg=PANEL):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="{bg}"/>' + DEFS)


def arrow(x1, y1, x2, y2, color=INK3, marker='a', w=1.6, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ''
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="{w}" marker-end="url(#{marker})"{d}/>')


def pill(cx, cy, s, fs=11, color=PC, bg=PC_BG, pad=9, h=21):
    w = tw(s, fs) + pad * 2
    return (rect(cx - w / 2, cy - h / 2, w, h, fill=bg, stroke='none', r=h / 2) +
            ctext(cx, cy + fs * 0.36, s, fs, color, '600'))


def pill_r(xr, cy, s, fs=11, color=PC, bg=PC_BG, pad=9, h=21):
    w = tw(s, fs) + pad * 2
    x = xr - w
    return (rect(x, cy - h / 2, w, h, fill=bg, stroke='none', r=h / 2) +
            ctext(x + w / 2, cy + fs * 0.36, s, fs, color, '600'))


def title_block(w, t1, t2=None, y1=38, y2=62):
    out = ctext(w / 2, y1, t1, 17, INK, '700')
    if t2:
        out += ctext(w / 2, y2, t2, 12, INK3)
    return out


def save(name, body, w, h):
    p = os.path.join(OUT, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(svg_open(w, h) + body + '</svg>')
    print('  %-28s %dx%d' % (name, w, h))


# =====================================================================
# Fig 1 — 手游「物理」的五层分解
# =====================================================================
def fig1():
    W, H = 1080, 690
    o = title_block(W, '手游「物理」的五层分解',
                    '同一个词，五套完全不同的实现 —— 只有中间两层真的在跑引擎物理求解器')

    rows = [
        ('① 角色移动 / 位移', '每帧 · 全平台适用',
         'CMC（Character Movement Component）或自研 KCC',
         '胶囊体 + 自定义扫掠，不走刚体求解器；为网络同步与手感而绕过物理',
         '自研 / 不用物理', WARN, WARN_BG),
        ('② 载具', '每帧 · 有载具的游戏',
         'UE4：PhysX Vehicle　UE5：Chaos Vehicle，大厂多自研改造',
         '轮胎摩擦模型 + 悬挂射线检测，是独立于刚体的专用求解分支',
         '引擎物理（可替换）', OK, OK_BG),
        ('③ 布娃娃 / 受击', '事件触发 · 持续 2~4 秒',
         '引擎 ragdoll，降频 + 简化碰撞体',
         '每具 12~20 个胶囊体，受击后短时间求解，结束后切回动画或冻结',
         '引擎物理（简化）', OK, OK_BG),
        ('④ 布料 / 毛发', '每帧 · 仅高配档开启',
         'UE5 Chaos Cloth ／ 骨骼链伪物理 ／ VAT 顶点动画烘焙',
         '移动端目标 ≈1ms、200~600 模拟顶点；毛发（Groom）几乎必然降级',
         '部分 + 必须降级', NOTE, NOTE_BG),
        ('⑤ 破坏 / 碎片', '事件触发 · 瞬时',
         '预制分块 + 特效 + VAT，极少用实时 Chaos Destruction',
         '实时断裂（Geometry Collection）在移动端预算之外，多数是「看起来碎了」',
         '烘焙 / 特效为主', WARN, WARN_BG),
    ]

    y = 96
    for name, freq, main, sub, tagt, tc, tbg in rows:
        o += rect(40, y, 1000, 86, fill='#fff', stroke=LINE, r=9)
        o += f'<rect x="40" y="{y + 4}" width="6" height="78" rx="3" fill="{tc}"/>'
        o += text(66, y + 34, name, 14.5, 'start', INK, '700')
        o += text(66, y + 56, freq, 11, 'start', INK3)
        o += line(258, y + 18, 258, y + 68, LINE, 1.2)
        o += text(278, y + 34, main, 12.5, 'start', INK2, '600')
        o += text(278, y + 56, sub, 11, 'start', INK3)
        o += pill_r(1020, y + 43, tagt, 11, tc, tbg)
        y += 98

    o += rect(40, 592, 1000, 56, fill=PC_BG, stroke='none', r=9)
    o += ctext(540, 618, '结论：手游里真正走「引擎物理求解器」的只有 ②③，④ 是高配限定，① ⑤ 是自研或烘焙。',
               12.5, PC, '700')
    o += ctext(540, 638, '把端游的物理配置直接搬到移动端，等于把 100% 的物理预算压在 20% 的玩法上。',
               11.5, INK2)
    save('fig1_overview.svg', o, W, H)


# =====================================================================
# Fig 2 — UE4/PhysX 与 UE5/Chaos 的内核差异
# =====================================================================
def fig2():
    W, H = 1080, 560
    o = title_block(W, '物理内核的整体替换：UE4 的 PhysX 与 UE5 的 Chaos',
                    '这不是升级，是换引擎 —— 两套内核的接口、数据布局、求解模型都不同')

    LX, RX, CW = 40, 585, 455

    o += rect(LX, 80, CW, 50, fill=PC_BG, stroke='none', r=9)
    o += text(LX + 20, 111, 'UE4 · NVIDIA PhysX 3', 15, 'start', PC, '700')
    o += rect(RX, 80, CW, 50, fill=CHAOS_BG, stroke='none', r=9)
    o += text(RX + 20, 111, 'UE5 · Epic Chaos', 15, 'start', CHAOS, '700')

    items = [
        ('集成方式', '外部库，NVIDIA 维护。UE 与 PhysX 的数据结构反复序列化 / 反序列化',
         'Epic 自研，UE 原生 C++ 实现。与引擎数据结构零拷贝'),
        ('物理 Tick', '跟随渲染帧率。掉帧即步长变化，物理结果随之漂移',
         '固定步长 + 独立异步求解线程，与渲染帧率解耦'),
        ('求解范围', '刚体 / 布料 / 破坏各自独立模块，互不共享世界',
         '统一解算器：刚体 + 约束 + 布料 + 毛发 + 载具 + 场 + 破坏同场交互'),
        ('确定性', '相对稳定，老项目行为可预期',
         '不完全确定（跨平台 / 跨线程浮点差异）；Epic 建议不要追求 100%'),
        ('载具', 'PhysX Vehicle，4.27 是最后一个可用版本',
         'Chaos Vehicle；5.5 另加 Chaos Modular Vehicle System（模块化载具）'),
    ]

    y = 146
    for name, lv, rv in items:
        o += rect(LX, y, CW, 62, fill='#fff', stroke=LINE, r=8)
        o += text(LX + 16, y + 24, name, 12.5, 'start', PC, '700')
        o += text(LX + 16, y + 46, lv, 11, 'start', INK2)
        o += rect(RX, y, CW, 62, fill='#fff', stroke=LINE, r=8)
        o += text(RX + 16, y + 24, name, 12.5, 'start', CHAOS, '700')
        o += text(RX + 16, y + 46, rv, 11, 'start', INK2)
        y += 70

    # 中间版本时间轴
    o += line(516, 146, 516, 480, LINE, 1.4, dash='4 4')
    for cy, s, col in ((176, '4.23 引入', INK3), (300, '5.0 默认', PC), (452, '5.1 移除', WARN)):
        o += pill(540, cy, s, 10, col, GREY_BG, pad=8, h=19)
        o += line(516, cy, 528, cy, LINE, 1.4)
    save('fig2_kernel.svg', o, W, H)


# =====================================================================
# Fig 3 — Epic 官方实测：Chaos vs PhysX
# =====================================================================
def fig3():
    W, H = 1080, 540
    o = title_block(W, 'Epic 官方实测：Chaos 并非全面更快',
                    'UE5.0 tech blog 对比 PhysX 版（Teal）与 Chaos 版（Blue），单位 ms，越低越好')

    data = [
        ('Sweeps\ntumbler', 11.64, 4.42),
        ('Sweeps\nlandscape', 0.55, 0.13),
        ('刚体测试 A', 10.82, 5.34),
        ('Overlaps', 2.09, 2.58),
        ('Raycasts', 0.04, 0.06),
        ('刚体测试 B', 3.17, 4.76),
        ('刚体测试 C', 5.30, 8.18),
    ]

    X0, X1 = 96, 1030
    Y0, Y1 = 118, 408           # 绘图区
    YMAX = 12.0

    def py(v):
        return Y1 - (v / YMAX) * (Y1 - Y0)

    # 网格
    for t in range(0, 13, 3):
        yy = py(t)
        o += line(X0, yy, X1, yy, LINE, 1)
        o += text(X0 - 12, yy + 4, str(t), 10.5, 'end', INK3)
    o += text(X0 - 12, Y0 - 16, 'ms', 10.5, 'end', INK3)

    gw = (X1 - X0) / len(data)
    bw = 38

    for i, (name, a, b) in enumerate(data):
        cx = X0 + gw * (i + 0.5)
        xa = cx - bw - 4
        xb = cx + 4
        # PhysX 柱
        o += f'<rect x="{xa:.1f}" y="{py(a):.1f}" width="{bw}" height="{Y1 - py(a):.1f}" rx="3" fill="{PC}" opacity=".85"/>'
        o += f'<rect x="{xb:.1f}" y="{py(b):.1f}" width="{bw}" height="{Y1 - py(b):.1f}" rx="3" fill="{CHAOS}" opacity=".85"/>'
        o += ctext(xa + bw / 2, py(a) - 7, '%.2f' % a, 10, PC, '700')
        faster = b < a
        o += ctext(xb + bw / 2, py(b) - 7, '%.2f' % b, 10, OK if faster else WARN, '700')
        for k, ln in enumerate(name.split('\n')):
            o += ctext(cx, Y1 + 22 + k * 15, ln, 11, INK2, '600')
        tag = ('快 %.0f%%' % ((a - b) / a * 100)) if faster else ('慢 %.0f%%' % ((b - a) / a * 100))
        o += ctext(cx, Y1 + 58, tag, 10.5, OK if faster else WARN, '700')

    o += line(X0, Y1, X1, Y1, INK3, 1.4)

    # 图例
    o += f'<rect x="{X1 - 232}" y="72" width="12" height="12" rx="2" fill="{PC}"/>'
    o += text(X1 - 214, 82, 'UE4 / PhysX 3', 11, 'start', INK2, '600')
    o += f'<rect x="{X1 - 112}" y="72" width="12" height="12" rx="2" fill="{CHAOS}"/>'
    o += text(X1 - 94, 82, 'UE5 / Chaos', 11, 'start', INK2, '600')

    o += rect(40, 466, 1000, 54, fill=NOTE_BG, stroke='none', r=9)
    o += ctext(540, 490, 'Epic 把变慢归因于 LWC（大世界坐标）引入的双精度浮点开销，并称 5.1 继续优化。',
               12, NOTE, '700')
    o += ctext(540, 509, '所以「Chaos 全面替代 PhysX 且更快」是错的；「Chaos 比 PhysX 慢 10~100 倍」同样不成立（那是 EA 阶段言论）。',
               11, INK2)
    save('fig3_perf.svg', o, W, H)


# =====================================================================
# Fig 4 — 一个受击布娃娃的一生
# =====================================================================
def fig4():
    W, H = 1160, 480
    o = title_block(W, '一个受击布娃娃的一生',
                    '跟着一次「击杀命中」的事件，走完服务器与客户端两侧的完整链路')

    nodes = [
        ('① 命中判定', ['服务器权威判定', '本帧是否致死 / 触发 ragdoll']),
        ('② 物理场景注册 + 求解', ['12~20 个胶囊体', '固定步长 1/30~1/60，异步线程']),
        ('③ 状态复制', ['位置 + 速度', '量化后按 20Hz 下发']),
        ('④ 客户端表现', ['插值补帧 / 必要时回滚重放', '物理骨架映射回渲染骨架']),
    ]
    labels = ['触发标记 + 冲量', '位姿 + 速度', '快照（量化）']

    NW, NH, gap = 200, 126, 100
    total = len(nodes) * NW + (len(nodes) - 1) * gap
    x0 = (W - total) / 2
    NY = 190

    # 先铺满全部节点，再画箭头 —— 否则标签会被后画的节点框盖住
    for i, (t, subs) in enumerate(nodes):
        x = x0 + i * (NW + gap)
        o += rect(x, NY, NW, NH, fill='#fff', stroke=LINE, r=9)
        o += f'<rect x="{x}" y="{NY}" width="{NW}" height="5" rx="2.5" fill="{MOB}"/>'
        o += ctext(x + NW / 2, NY + 40, t, 12.5, INK, '700')
        for k, s in enumerate(subs):
            o += ctext(x + NW / 2, NY + 70 + k * 20, s, 10.5, INK3)

    for i in range(len(nodes) - 1):
        ax1 = x0 + i * (NW + gap) + NW + 8
        ax2 = ax1 + gap - 16
        cy = NY + NH / 2 + 4
        o += arrow(ax1, cy, ax2, cy, MOB, 'am')
        o += ctext((ax1 + ax2) / 2, cy - 15, labels[i], 10, MOB, '700')

    # 入口：事件从下方进入节点 ①
    ex = x0 + NW / 2
    o += arrow(ex, 384, ex, NY + NH + 8, INK3, 'a')
    o += ctext(ex, 402, '击杀 / 受击事件', 11, INK2, '600')

    o += rect(W / 2 - 350, 420, 700, 40, fill=GREY_BG, stroke='none', r=8)
    o += ctext(W / 2, 445, '服务器与客户端各跑一份 UWorld 物理场景实例 —— 一致性靠复制与回滚，不靠确定性',
               11.5, INK2, '600')
    save('fig4_ragdoll_life.svg', o, W, H)


# =====================================================================
# Fig 5 — 移动端物理预算与优化杠杆
# =====================================================================
def fig5():
    W, H = 1090, 580
    o = title_block(W, '移动端物理预算与六个优化杠杆',
                    '以 30fps（帧预算 33.3ms）为参照，物理的典型占用与可压缩空间')

    # 预算条
    o += text(60, 106, '一帧 33.3ms 的 CPU 预算', 12, 'start', INK, '700')
    o += text(1030, 106, '物理典型 1~4ms ≈ 3%~12%', 11, 'end', INK3)
    o += rect(60, 116, 970, 46, fill=GREY_BG, stroke=LINE, r=8)
    o += f'<rect x="60" y="116" width="117" height="46" rx="8" fill="{MOB}" opacity=".9"/>'
    o += ctext(118, 145, '物理 4ms', 11.5, '#fff', '700')
    o += ctext(560, 145, '其余：GameThread 逻辑 / 动画 / VFX / AI + RenderThread 提交', 11.5, INK2)
    o += ctext(950, 145, 'GPU 另计', 11, INK3)

    cards = [
        ('① 简化碰撞体', '三角网格 → 凸包 → 胶囊 → 球', '成本可降一个数量级'),
        ('② 降频与子步进', '物理 tick 60Hz → 30Hz，\n限制 MaxSubsteps 防「死亡螺旋」', '直接减半'),
        ('③ 休眠与剔除', 'Sleep Threshold 0.1~1.0，\n屏幕外 / 静止体停止积分', '随场景而定'),
        ('④ 物理 / 视觉解耦', '「碰的」与「看的」分离，\n物理属性挂到更近的 Streaming 层', '降内存 + 降场景复杂度'),
        ('⑤ VAT / 烘焙替代', '布料、摆动、碎片改顶点动画贴图，\n离线烘焙，运行时只采贴图', '把 CPU 计算搬到离线'),
        ('⑥ 服务器权威', '只算影响胜负的物体，\n纯表现物做客户端本地模拟', '服务器 CPU 可控'),
    ]

    CW, CH = 316, 138
    cx0, cy0 = 60, 200
    gx, gy = 18, 16

    for i, (t, body, gain) in enumerate(cards):
        col, row = i % 3, i // 3
        x = cx0 + col * (CW + gx)
        y = cy0 + row * (CH + gy)
        o += rect(x, y, CW, CH, fill='#fff', stroke=LINE, r=9)
        o += text(x + 16, y + 28, t, 13, 'start', MOB, '700')
        for k, ln in enumerate(body.split('\n')):
            o += text(x + 16, y + 52 + k * 18, ln, 11, 'start', INK2)
        o += f'<rect x="{x + 16}" y="{y + CH - 36}" width="{CW - 32}" height="24" rx="6" fill="{MOB_BG}"/>'
        o += text(x + 26, y + CH - 19, '收益：' + gain, 10.5, 'start', MOB, '700')

    o += ctext(W / 2, cy0 + 2 * (CH + gy) + 6, '六个杠杆里，①③④ 是工程改动，②⑤⑥ 是设计 / 架构决策 —— 后三个收益最大但改得最晚。',
               11.5, INK3, '600')
    save('fig5_budget.svg', o, W, H)


# =====================================================================
# Fig 6 — 网络同步的两条路径
# =====================================================================
def fig6():
    W, H = 1120, 620
    o = title_block(W, '物理的网络同步：两条互斥的路径',
                    '同一款游戏往往只选一条 —— 选错会在「防作弊」与「确定性」之间反复返工')

    # ---- 泳道 A：帧同步 ----
    o += rect(40, 84, 1040, 214, fill='#fff', stroke=LINE, r=10)
    o += text(62, 112, '路径 A · 帧同步（Lockstep）', 14, 'start', PC, '700')
    o += text(62, 132, '只同步输入，各端自行模拟；要求物理与浮点结果逐位一致', 11, 'start', INK3)

    ay_c, ay_s = 176, 236
    o += text(70, ay_c + 4, '客户端', 11, 'start', PC, '700')
    o += text(70, ay_s + 4, '服务器', 11, 'start', PC, '700')
    o += line(120, ay_c, 1050, ay_c, PC, 1.6)
    o += line(120, ay_s, 1050, ay_s, PC, 1.6)

    o += f'<circle cx="180" cy="{ay_c}" r="4.5" fill="{PC}"/>'
    o += ctext(180, ay_c - 14, '本地模拟（帧 N）', 10.5, INK2, '600')
    o += arrow(300, ay_c + 6, 300, ay_s - 6, PC, 'ab')
    o += text(310, (ay_c + ay_s) / 2 + 4, '输入帧 N（几十字节）', 10.5, INK2)
    o += ctext(560, ay_s - 12, '校验 / 转发', 10.5, INK2, '600')
    o += arrow(700, ay_s + 6, 700, ay_c - 6, PC, 'ab')
    o += text(710, (ay_c + ay_s) / 2 + 4, '输入帧 N', 10.5, INK2)
    o += f'<circle cx="880" cy="{ay_c}" r="4.5" fill="{PC}"/>'
    o += ctext(880, ay_c - 14, '同样模拟 → 同结果', 10.5, INK2, '600')
    o += text(1050, ay_c + 30, '', 10.5)

    o += pill(160, 278, '带宽极省', 10.5, OK, OK_BG, h=19)
    o += pill(300, 278, '确定性是硬前提', 10.5, WARN, WARN_BG, h=19)
    o += pill(470, 278, '客户端掌状态 → 防作弊弱', 10.5, WARN, WARN_BG, h=19)
    o += pill(690, 278, '断线重连代价高', 10.5, NOTE, NOTE_BG, h=19)

    # ---- 泳道 B：服务器权威 ----
    o += rect(40, 316, 1040, 232, fill='#fff', stroke=LINE, r=10)
    o += text(62, 344, '路径 B · 服务器权威 + 客户端预测回滚', 14, 'start', OK, '700')
    o += text(62, 364, '服务器是唯一真相；客户端先本地预测，收到权威状态后比对，不一致则回滚重放', 11, 'start', INK3)

    by_c, by_s = 420, 490
    o += text(70, by_c + 4, '客户端', 11, 'start', OK, '700')
    o += text(70, by_s + 4, '服务器', 11, 'start', OK, '700')
    o += line(120, by_c, 1050, by_c, OK, 1.6)
    o += line(120, by_s, 1050, by_s, OK, 1.6)

    o += f'<circle cx="170" cy="{by_c}" r="4.5" fill="{OK}"/>'
    o += ctext(175, by_c - 14, '本地预测模拟', 10.5, INK2, '600')
    o += arrow(320, by_c - 6, 320, by_s + 6, OK, 'ao')
    o += text(330, (by_c + by_s) / 2 + 4, '输入 + 时间戳', 10.5, INK2)
    o += ctext(560, by_s - 12, '权威模拟', 10.5, INK2, '600')
    o += arrow(700, by_s + 6, 700, by_c - 6, OK, 'ao')
    o += text(710, (by_c + by_s) / 2 + 4, '权威状态快照', 10.5, INK2)
    o += f'<circle cx="890" cy="{by_c}" r="4.5" fill="{OK}"/>'
    o += ctext(910, by_c - 14, '比对 → 一致：继续', 10.5, INK2, '600')
    o += ctext(910, by_c + 22, '不一致：回滚重放', 10.5, WARN, '600')

    o += pill(160, 540, '防作弊强', 10.5, OK, OK_BG, h=19)
    o += pill(280, 540, '物理交互一致', 10.5, OK, OK_BG, h=19)
    o += pill(420, 540, '带宽 / 服务器开销高', 10.5, WARN, WARN_BG, h=19)
    o += pill(590, 540, 'UE5.4 网络物理为 Beta', 10.5, NOTE, NOTE_BG, h=19)

    o += ctext(W / 2, 588, 'Chaos 不是完全确定性的：跨平台 / 跨线程浮点差异会持续制造「橡皮筋」修正。',
               12, WARN, '700')
    save('fig6_netcode.svg', o, W, H)


if __name__ == '__main__':
    print('生成 SVG：')
    for f in (fig1, fig2, fig3, fig4, fig5, fig6):
        f()
    print('完成，输出目录: %s' % OUT)
