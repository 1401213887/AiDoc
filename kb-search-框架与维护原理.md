# kb_search 知识库 — 框架架构与维护原理

> 生成日期：2026-07-06 | 基于当前 KB 运行态完整梳理

---

## 一、总览：这是什么

kb_search 是一套**本地 markdown 知识库 + grep 检索 + 自动化同步**的组合系统，本质是个人工作区的「Karpathy 风格知识管理」——所有笔记是纯 `.md` 文件，frontmatter 标 keywords，用 Python 脚本做全文检索打分排名。

**一句话定位**：给 AI 助手（小8）提供领域知识的「外挂大脑」，每次技术问题前先搜一遍这里再回答。

---

## 二、三层文件架构

```
.workbuddy/kb/                     ← KB 根目录
├── INDEX.md                       ← 🔴 总入口 + 全局快查表（最核心）
├── glossary.md                    ← 术语表（缩写/行话 catch-all）
│
├── rendering/                     ← 分类 1：渲染管线
│   ├── INDEX.md                   ← 子索引（该分类下所有笔记的目录）
│   ├── mobile-deferred-stencil-decoding.md
│   ├── mobile-gbuffer-layout-by-shading-model.md
│   ├── stencil-buffer-read-write.md
│   ├── deferred-lighting-pass-drawcall.md
│   ├── vt-combine-pass-channel-repack.md
│   ├── fps-mobile-pipeline-comparison.md
│   └── aces-tonemap.md
│
├── ue-engine/  INDEX.md  ...     ← 分类 2：UE5 引擎机制
├── performance/ INDEX.md  ...    ← 分类 3：性能分析方法论
├── papers/ INDEX.md ...          ← 分类 4：GDC/SIGGRAPH 论文笔记
├── cpp-graphics/ INDEX.md ...    ← 分类 5：C++ 与图形学数学
├── tools/ INDEX.md ...           ← 分类 6：工具链
│
└── aidoc-stubs/                   ← 🔄 自动同步区（只读镜像）
    ├── INDEX.md                   ← 自动维护的 stub 目录
    ├── computerelevance.md        ← stub：ComputeRelevance 优化报告
    ├── vt_r32f_precisionloss_analysis.md
    └── ...（共 68 篇，同步自 E:\AiDoc\*.md）
```

### 两种索引，两个层级

| 索引文件 | 覆盖范围 | 谁维护 |
|---|---|---|
| `kb/INDEX.md`（总） | 全部 7 个一级分类的摘要 + 一级分类表 | **手动** |
| `rendering/INDEX.md` 等（子） | 该分类下的笔记清单 + 待补充 topic | **手动** |
| `aidoc-stubs/INDEX.md` | E:\AiDoc 的 stub 镜像索引 | **自动**（`aidoc_to_kb.py`） |

---

## 三、核心检索机制

### 3.1 Tier 1：grep + 加权评分（当前在用）

检索工具 `kb_search.py`，路径：
```
C:\Users\djangozhang\.workbuddy\scripts\kb_search.py
```

**检索流程**：

```
用户提问 → 小8 判断需要查 KB
  → python kb_search.py "<query>"
    → 扫描所有 .md（跳过 INDEX.md）
    → 按 5 层加权打分：

      命中位置        权重      说明
      ──────────────────────────────────────
      文件名 keywords   5.0      最高权重，精准匹配
      title（frontmatter）4.0      标题命中
      keywords 数组     3.0      frontmatter 关键词命中
      章节标题          2.0      ## / ### 命中
      正文             1.0×N    每个词 +1，封顶 5 分/词
    → 排序取 Top-K（默认 5）
    → 输出：path / score / hits / summary
  → 小8 Read 命中文件 → 基于原文回答
```

**关键设计决策**：
- 不建索引，不跑 embedding，零构建成本
- 查询即实时 grep，适合 < 200 篇规模
- 查询词用空格/中文逗号/顿号分词
- 摘要优先取 `## TL;DR` 段，其次取首段正文

### 3.2 Tier 2 升级路径（待启用）

| 条件 | 笔记数 ≥ 200 篇 |
|---|---|
| 技术栈 | `sentence-transformers` + `BAAI/bge-m3` 嵌入 + `faiss-cpu` 向量索引 |
| 构建方式 | 离线 `kb_index.py` 全量构建，或增量追加新笔记向量的混合方案 |
| 查询 | `kb_search.py` 加 `--semantic` 选项，切换向量相似度检索 |
| 触发 | 手动决策升级，不自动 |

**为什么不现在做 Tier 2**：手工笔记 ~18 篇 + aidoc-stubs 自动同步 68 篇 = ~86 篇总量，grep + 评分机制已经能跑得很好。

### 3.3 配套 skill：`kb-search`

```yaml
位置: C:\Users\djangozhang\.workbuddy\skills\kb-search\SKILL.md
触发: 波哥提"查知识库" / "KB 里有吗" / "我们记过没" / 技术性提问均先搜索
```

Skill 负责告诉小8：什么时候搜、怎么搜、命中后要不要读原文、hit stub 后要不要去 `E:\AiDoc` 读全文、怎么新增笔记。

---

## 四、自动化同步链：AiDoc → KB stub

```
E:\AiDoc\*.md（用户/小8 产出的技术文档）
         │
         │ 每日 02:30（automation: WorkBuddy_AiDocToKB）
         ▼
  aidoc_to_kb.py（增量同步脚本）
         │
         ├── 比较 mtime + sha256 → 只处理变更文件
         ├── 为每篇 AiDoc 文档生成 stub（摘要 + keywords + source_path）
         └── 写入 kb/aidoc-stubs/*.md
         │
         ▼
  kb/aidoc-stubs/INDEX.md（自动更新）
         │
         ▼
  kb_search.py 可检索到 AiDoc 文档的 stub
         │
         ▼
  小8 命中 stub → 读 source_path（E:\AiDoc\原文）→ 基于原文回答
```

**关键细节**：
- stub 是**只读镜像**，不要手动编辑（会被覆盖）
- 手动触发：`python aidoc_to_kb.py`（增量）/ `--force`（全量）/ `--dry-run`（预览）
- 同步状态文件：`.sync_state.json`（记录每篇的 mtime + sha256）
- 日志按月存放：`C:\Users\djangozhang\.workbuddy\logs\aidoc_to_kb_YYYYMM.log`

---

## 五、笔记规范（写笔记的铁律）

### 5.1 文件命名

```
全小写 + 连字符：  mobile-deferred-stencil-decoding.md
论文带年份前缀：   2024-gdc-lightspeed-mobile-rendering.md
术语收进 glossary： glossary.md（一个文件兜底，不拆散）
```

### 5.2 Frontmatter（必须有）

```yaml
---
title: 标题
keywords: [关键字1, 关键字2, 中文别名, English alias]
related: [其他笔记的相对路径]
last_updated: 2026-05-26
source: 来源（自己整理 / 论文 / UE5 源码 / 同事）
status: draft | stable | archived
---
```

**keywords 写法**：多写具体词（`["延迟渲染", "Deferred Shading", "GBuffer"]`），不写宽泛词（`["渲染"]`）。keywords 会用来匹配查询词，越精准命中率越高。

### 5.3 正文结构（推荐）

```markdown
# 标题
## TL;DR         ← 一句话核心结论（检索时优先取为摘要）
## 关键概念
## 公式 / 代码 / 配置
## UE 中的位置    ← 源码路径 + 函数名
## 常见误区 / 踩坑
## 参考链接
```

---

## 六、维护规则

### 6.1 新增笔记 checklist（手动区）

```
□ 1. 写 .md 文件 → 放进对应分类目录
□ 2. 写 frontmatter（keywords 要具体）
□ 3. 更新该分类的 INDEX.md（在表格最后加一行）
□ 4. 更新 kb/INDEX.md 的「全局条目快查表」（也加一行）
□ 5. 验证：python kb_search.py "<关键词>" 应看到 Top-1 命中
```

⚠️ **不更新 INDEX 等于没加**——`kb_search.py` 会搜到文件正文，但全局快查表不显示，小8 优先扫快查表时会漏掉。

### 6.2 新增笔记（自动区 aidoc-stubs）

```
写技术文档 → 产出到 E:\AiDoc\
  → 每日 02:30 自动同步到 kb/aidoc-stubs/
  → stub 索引自动更新
  → 不需要手动改任何 INDEX.md
```

### 6.3 写笔记的触发条件

| 该写 KB 笔记 | 不该写，走 daily memory |
|---|---|
| 波哥明确说"沉淀这个" | 临时调试发现 |
| 同一知识点被多次复用 | 当天的踩坑记录 |
| 方法论级总结（通用结论） | 单纯的事件记录 |
| 论文/演讲提炼 | 一次性的操作步骤 |

**Karpathy 原则**：daily 写得快，KB 写得稳；新发现先入 daily（`YYYY-MM-DD.md`），多次复用后再提炼到 KB。

---

## 七、与其他系统的边界

```
┌──────────────────────────────────────────────────────┐
│  MEMORY.md（元信息）                                   │
│  偏好 / 铁律 / 项目状态 / 上下文恢复索引                  │
│  特点：跨会话不变的规则，小8 每次启动加载                   │
├──────────────────────────────────────────────────────┤
│  daily memory（YYYY-MM-DD.md）                         │
│  流水账：今天做了什么 / 决策过程 / 当时怎么想的            │
│  特点：按时间追加，事后可回溯                            │
├──────────────────────────────────────────────────────┤
│  KB（本系统）                                          │
│  领域知识：方法论 / 论文 / 公式 / 引擎机制                 │
│  特点：可检索的回答素材，结构化，有 keywords               │
├──────────────────────────────────────────────────────┤
│  Skills（~/.workbuddy/skills/）                        │
│  可执行的工作流：命令 + 触发规则 + 参数                    │
│  特点：不再是"参考资料"，而是"操作指令"                    │
├──────────────────────────────────────────────────────┤
│  E:\AiDoc（技术文档仓库）                                │
│  产物型技术文档的最终落盘位置，每日 02:00 git auto-push    │
│  通过 aidoc_to_kb.py 自动桥接到 KB stub 区               │
└──────────────────────────────────────────────────────┘
```

---

## 八、当前规模与状态

| 指标 | 数值 |
|---|---|
| 一级分类数 | 6 个手动 + 1 个自动（aidoc-stubs） |
| 手动笔记数 | ~18 篇（rendering 7 + papers 7 + performance 1 + tools/ue-engine/cpp-graphics 少量） |
| aidoc-stubs 数 | 68 篇（自动同步自 E:\AiDoc） |
| 检索方式 | Tier 1（grep + 评分） |
| 检索延迟 | 实时扫描，零构建 |
| 升级触发线 | 笔记 ≥ 200 篇 → Tier 2（FAISS + bge-m3） |

---

## 九、关键路径速查

| 做什么 | 怎么操作 |
|---|---|
| 搜索笔记 | `python kb_search.py "<query>"` |
| 列出全部 | `python kb_search.py --list` |
| 手动同步 AiDoc | `python aidoc_to_kb.py` |
| 预览同步 | `python aidoc_to_kb.py --dry-run` |
| 新增分类 | 建目录 + 写子 INDEX.md + 更新总 INDEX 一级分类表 |

---

*此文档描述的是 2026-07-06 运行态。后续框架变更时请同步更新本文档。*
