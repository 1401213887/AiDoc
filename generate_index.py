# -*- coding: utf-8 -*-
"""
generate_index.py — E:\\AiDoc 知识库导航自动生成脚本

功能：
  扫描本目录（含 MobileRenderPath 子目录）下的所有文档（.md/.html/.pdf/.docx/.csv），
  按"有优先级顺序"的关键词规则归类到 8 个类目，提取标题与修改时间，
  自动生成 README.md 总导航。文件不移动，全部用相对路径互链。

用法：
  python generate_index.py
  （在 E:\\AiDoc 目录下运行，或任意位置运行——脚本以自身所在目录为根）
"""

import os
import re
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(ROOT, "README.md")

# 扫描时忽略的文件/目录
IGNORE_DIRS = {".git", "__pycache__", "_archive_old"}
IGNORE_FILES = {"README.md", "知识库导航_README.md", "generate_index.py"}
VALID_EXT = {".md", ".html", ".htm", ".pdf", ".docx", ".csv", ".xlsx", ".pptx"}

# ---------------------------------------------------------------------------
# 分类定义：(key, 标题, 一句话说明)
# ---------------------------------------------------------------------------
CATEGORIES = [
    ("01_tbdr",      "01 · TBDR 与片上优化方法论",
     "TBDR 原理、片上缓存、Subpass/Imageblock、HZB、Forward/Deferred 选型——方法论纵贯线"),
    ("02_cases",     "02 · 头部手游案例库",
     "单款手游移动端渲染拆解（html）。方法论的具体落地参照"),
    ("03_topics",    "03 · 专题横向汇总",
     "跨游戏横向对比：半透明 / 遮挡剔除 / DrawCall / FPS 全景"),
    ("04_engine",    "04 · 引擎源码级分析",
     "PVS、视锥剔除、WorldPartition、TaskGraph、线程池、RDG 等源码深挖"),
    ("05_stability", "05 · 崩溃与稳定性",
     "崩溃定位与修复：VT / SkeletalMesh / UseAfterFree、帧率掉档排查"),
    ("06_profiling", "06 · Profiling 工具与教程",
     "高通 SDP / Adreno / Snapdragon Profiler、UE Insights、CPU Trace 工具链"),
    ("07_project",   "07 · 项目专项分析",
     "具体项目（FateTrigger 等）的单帧 / 纹理 / 三角面分析、AO 实践报告"),
    ("99_misc",      "99 · 其它与原始资料",
     "未归类资料、大体积归档报告、原始数据（docx/csv/pdf）"),
]
CAT_KEYS = [c[0] for c in CATEGORIES]

# ---------------------------------------------------------------------------
# 归类规则：按顺序匹配，命中第一条即归类。rule = (category_key, [关键词...])
# 关键词对文件名做大小写不敏感的子串匹配；任一命中即算该规则命中。
# 顺序很重要：更专属的规则放前面。
# ---------------------------------------------------------------------------
RULES = [
    # 05 崩溃与稳定性（含 crash / 掉帧排查）——放前面，避免被 VT/线程 规则抢走
    ("05_stability", ["crash", "dangling", "useafterfree", "use_after_free",
                       "orphantask", "skeletalmesh", "swappy", "frampacing",
                       "framepacing", "帧率自动降", "掉帧", "30fps"]),
    # 06 Profiling 工具
    ("06_profiling", ["sdp", "adreno", "snapdragon", "profiler", "insights",
                      "cpuusagetrack", "cpu_trace", "cpu trace", "性能指标详解",
                      "性能热点", "瓶颈定位", "最佳实践系列"]),
    # 07 项目专项
    ("07_project",   ["fatetrigger", "移动端ao", "ao实践", "单帧渲染", "vhm_analysis"]),
    # 01 TBDR / 片上 / 管线方法论
    ("01_tbdr",      ["tbdr", "imageblock", "tileshading", "tile_shading",
                      "subpass", "片上", "hzb", "forward_vs_deferred",
                      "forward_pipeline", "ios_metal", "ios_vs_android",
                      "deepdive", "deep_dive", "mobile_tech", "metal_tbdr"]),
    # 03 专题横向汇总
    ("03_topics",    ["头部手游", "fps手游技术全景", "全景对比",
                      "半透明", "遮挡剔除", "降低drawcall", "drawcall方案"]),
    # 02 手游案例（单款，通常以游戏名命名的 html / 片上技术总结）
    ("02_cases",     ["原神", "崩坏", "星穹铁道", "绝区零", "鸣潮", "王者荣耀",
                      "永劫无间", "光遇", "第五人格", "蛋仔", "和平精英",
                      "使命召唤", "三角洲", "暗区突围", "燕云十六声",
                      "洛克王国", "_pipeline_report", "移动端技术要点总结"]),
    # 04 引擎源码级分析
    ("04_engine",    ["pvs", "frustumcull", "worldpartition", "computerelevance",
                      "rdg_", "transientallocator", "scenedepthz", "scenevisibility",
                      "taskgraph", "线程池", "threadpool", "vt_", "rvt_",
                      "addtoworld", "cvarlighting", "fpreviousviewinfo",
                      "obj_list_primitives", "worldpartitionbuilder",
                      "uworldtick", "自适应线程池", "precisionloss"]),
]


def classify(filename: str) -> str:
    name = filename.lower()
    for cat, kws in RULES:
        for kw in kws:
            if kw.lower() in name:
                return cat
    return "99_misc"


def extract_title(path: str, filename: str) -> str:
    """对 .md 提取首个 # 标题；其它用文件名（去扩展名）。"""
    stem, ext = os.path.splitext(filename)
    if ext.lower() == ".md":
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for _ in range(40):
                    line = f.readline()
                    if not line:
                        break
                    m = re.match(r"^#\s+(.+?)\s*$", line)
                    if m:
                        return m.group(1).strip()
        except Exception:
            pass
    return stem


def ext_tag(filename: str) -> str:
    e = os.path.splitext(filename)[1].lower().lstrip(".")
    return e.upper() if e else "?"


def collect():
    """返回 {cat_key: [ {title, relpath, mtime, ext} ... ]}"""
    buckets = {k: [] for k in CAT_KEYS}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if fn in IGNORE_FILES:
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in VALID_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(full))
            buckets[classify(fn)].append({
                "title": extract_title(full, fn),
                "rel": rel,
                "mtime": mtime,
                "ext": ext_tag(fn),
                "fname": fn,
            })
    for k in buckets:
        buckets[k].sort(key=lambda x: x["mtime"], reverse=True)
    return buckets


def md_link(item):
    # 相对链接，URL 中空格转义
    href = item["rel"].replace(" ", "%20")
    return f"[{item['title']}](./{href})"


def build_readme(buckets):
    today = datetime.date.today().isoformat()
    total = sum(len(v) for v in buckets.values())
    lines = []
    lines.append("# 📚 E:\\AiDoc 技术知识库 · 总导航")
    lines.append("")
    lines.append("> UE 移动端渲染技术知识库。汇集 TBDR 片上优化方法论、头部手游渲染拆解、"
                 "引擎源码级分析、崩溃定位、Profiling 工具链与项目专项报告。")
    lines.append(">")
    lines.append("> - **互链约定**：全部相对路径，文件不移动，与 git 自动备份兼容。")
    lines.append("> - **本页由 `generate_index.py` 自动生成**，新增文档后重跑脚本即可刷新。")
    lines.append(f"> - 文档总数：**{total}** · 更新：{today}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 顶部分类目录
    lines.append("## 🗂 分类目录")
    lines.append("")
    lines.append("| # | 类目 | 文档数 | 说明 |")
    lines.append("|---|------|:---:|------|")
    for key, title, desc in CATEGORIES:
        n = len(buckets[key])
        anchor = title.replace(" ", "-").replace("·", "").replace("--", "-")
        lines.append(f"| {title.split(' ')[0]} | [{title}](#{anchor}) | {n} | {desc} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各分类明细
    for key, title, desc in CATEGORIES:
        items = buckets[key]
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"> {desc}")
        lines.append("")
        if not items:
            lines.append("_（暂无文档）_")
            lines.append("")
            continue
        lines.append("| 文档 | 类型 | 更新 |")
        lines.append("|------|:---:|:---:|")
        for it in items:
            lines.append(f"| {md_link(it)} | {it['ext']} | {it['mtime'].strftime('%m-%d')} |")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 维护：新增或重命名文档后，在本目录运行 `python generate_index.py` "
                 "即可重新生成本导航。归类规则见脚本顶部 `RULES`，如分类不准可调整关键词。")
    lines.append("")
    return "\n".join(lines)


def main():
    buckets = collect()
    readme = build_readme(buckets)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(readme)
    total = sum(len(v) for v in buckets.values())
    print(f"[OK] 已生成 {OUTPUT}")
    print(f"     文档总数 {total}")
    for key, title, _ in CATEGORIES:
        print(f"     {title:<28} {len(buckets[key]):>3} 篇")
        for it in buckets[key]:
            print(f"          - {it['fname']}")


if __name__ == "__main__":
    main()
