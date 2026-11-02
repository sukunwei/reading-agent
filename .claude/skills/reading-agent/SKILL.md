---
name: reading-agent
description: 把微信读书（weread.qq.com）网页版的一整本书采集成本地资产并深度精读：逐页截图 → 分页 OCR（macOS Vision）→ 插图视觉识别 → 逐章结构化精读笔记 → 全书五维评分与专业书评。当用户给出微信读书 reader 链接、要求"读这本书/精读/生成读书笔记/书评/五维评测"，或要求采集微信读书书籍内容时使用。
version: 0.1.0
---

# 微信读书整本书采集与精读

## 何时用

- 用户给出 `https://weread.qq.com/web/reader/...` 链接
- 用户说"读一下这本书""帮我做读书笔记""生成书评""这本书值不值得读"
- 用户要求把微信读书的书导出成可检索的本地文本

## 架构

```
采集层（确定性，Python + Playwright）        认知层（LangGraph + Claude）
──────────────────────────────            ──────────────────────────────
登录态   login.py / extract_chrome_cookies  切分   nodes/split.py   （确定性）
  ↓                                            ↓
逐页截图 capture.py  1280×815 整页          逐章总结 nodes/summary.py
  ↓  canvas 级 · 单栏 · 不含 header            ↓
分页OCR agent/ocr.py  macOS Vision          诊断恢复 nodes/diagnose.py
  ↓  临时裁栏 · 一页一 TXT                      ↓
插图识别 agent/figures.py  浅色块+图注双信号  人工闸门 gate (HITL)
  ↓  视觉模型读图 · 内联到正文位置               ↓
                                          全书评测 nodes/review.py
                                               ↓
                                          渲染 chapter_summaries.md / book_review.md
                                               └→ report.html（HTML 报告）
```

**分工原则**：采集层全是确定性代码，不进 LLM；认知层才用模型。章节切分是**确定性节点**（目录 × 每页章节号），不用 LLM。

## 关键实现约束（都是实测踩出来的，别改）

| 约束 | 原因 |
|---|---|
| 视口 1280×815、`device_scale_factor=1` | DSF=2 时 reader 按设备像素排版而画布只留 CSS 尺寸 → **正文左右被切字** |
| 强制切到「上下滚动双栏」阅读模式 | 底部才有「上一页/下一页」按钮；该模式会被持久化，必须每次校验 |
| 截图前等 canvas 内容指纹**连续 3 次不变** | 插图比正文后加载，否则截到空白图表 |
| OCR 前按 canvas 边界**临时裁栏** | 整页两栏会让 Vision 把两栏同行并成一行 → 阅读序错乱（8.2% 的行跨栏） |
| 裁栏时排除 `.renderTarget_pager` 那行 | 否则「上一页/下一页」混进正文 |
| 结构化输出要能**降级** | 第三方 Anthropic 兼容端点接受 `output_config.format` 但**不强制** JSON |
| 默认模型不写死 `claude-opus-5-5` | 第三方端点没这模型会 404 → 读 `LLM_MODEL` → `ANTHROPIC_MODEL` |

## 常用命令

**产物目录由用户指定**（`--out-dir`），不在 agent 内部创建；三个命令必须用同一个。
登录态单独放 `~/.reading-agent/`（`--auth-dir` / `READING_AGENT_AUTH`）。

```bash
export OUT_DIR="$HOME/Documents/读书"        # 用户指定的产物目录

python3 capture/login.py                      # 有头浏览器扫码登录（→ ~/.reading-agent/）
python3 capture/extract_chrome_cookies.py      # 从运行中的 Chrome 提取（免扫码）
python3 capture/capture.py --book-url "<URL>" --out-dir "$OUT_DIR"   # 采集整本（可重复续跑）
python3 agent/ocr.py --out-dir "$OUT_DIR"      # 分页 OCR + 质量闸门
python3 agent/cli.py <书名> --out-dir "$OUT_DIR"   # 认知层全链路
python3 agent/cli.py --list --out-dir "$OUT_DIR"   # 列出已采集的书
python3 agent/report.py --book <书名> --out-dir "$OUT_DIR"   # HTML 报告（可离线重建）
```

不传 `--out-dir` 时默认 `$PWD/reading-output`；也可用 `READING_AGENT_OUT` 统一设置。

## 插图分流（M8）

判定「本页有插图」用**两路独立信号交叉一致**：

- **A 浅色块**（图像层）：正文深底（亮度 p50≈28），图表浅底（p50≈141）。**必须先 BoxBlur(10) 再做行投影** —— 否则图表里的深色网格线会把行带切碎导致漏检。再要求块内亮像素占比 > 3%（排除大标题误检，实测图表 8.4% vs 标题 0.26%）。
- **B 图注模式**（文本层）：`图X-Y`/`表X-Y` 在**行首**且行**短**，后跟「来源：/注：」。（正文里的引用「…才有了图1-3」在行尾且行长，不算。）

判定成立后：裁图送**视觉模型**（OCR 拿不到图表里的真实数据），产出结构化描述，按用户指定格式**内联回正文对应位置**：

```
图1-1 - 「标题:日本GDP增长率」[插图描述:柱状图。y轴 GDP增长率，%。1946年约-22%。…]
```

同时剔除落在图表 bbox 内的 OCR 行（坐标轴刻度噪声）。

## 书评 Agent 角色

> 运行时实现在 `agent/prompts.py`（`BOOK_REVIEW_SYSTEM`）+ `agent/schema.py`（`BookReview`）——
> 代码是唯一事实来源。完整版见仓库根的 `CLAUDE.md`。

你扮演资深书评人，同时精通经济、金融、商业与通识写作。职责：逐章精读笔记、
全书五维评测（逻辑结构性/内容干货密度/观点创新度/落地实用性/阅读启发价值，各 1-10 分）、
整合版读书资产。

硬性要求：
- 只使用原文出现的内容；每个评分依据都要引用书中的具体内容
- 允许批评（逻辑漏洞/重复啰嗦/案例单薄/结论超出证据）—— 短板比套话更有价值
- 评分要有区分度，不要所有维度都给 7 分
- 专有名词按原文用词，不换同义词
- 「图N - 「标题:…」[插图描述:…]」是**视觉模型转述，不是原文**，不得当原文引用
- 原文没有的信息就留空，不要凑数

评级规则：≥9.0 神作 / ≥7.5 优质好书 / ≥6.0 值得一读 / ≥4.5 中规中矩 / 否则 价值较低

## 忠实性护栏

- 视觉模型的描述**不是原文** → 在 prompt 里明确告知总结 Agent，不得当原文引用
- 逐章总结只允许使用原文出现的内容；可修正 OCR 错字但不得改变观点
- 术语按原文用词，不换同义词

## 数据与合规

- 只处理用户**已购或有合法阅读权限**的书籍
- **产物不写在 agent 目录里** —— 产物根目录由用户指定（`--out-dir`），默认 `$PWD/reading-output`
- 登录态在 `~/.reading-agent/`、`.env` 含 API 凭据 → 均已 gitignore
- 样例截图（付费书页面）不入库
