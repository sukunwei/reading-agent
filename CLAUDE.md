# CLAUDE.md

微信读书整本书采集与精读 Agent。本文件是**这个仓库的操作记忆**：改代码前先读第 3 节（硬约束），里面每一条都是踩坑实测出来的，违反了会静默产出错误结果。

---

## 1. 项目是什么

输入一个微信读书 reader URL，输出四类标准化产物：

1. 全书逐页纯净截图（整页 1280×815，不含顶部 header）
2. 全书分页 OCR 文本（一页一 TXT，与 PNG 一一对应）
3. AI 逐章结构化精读笔记
4. 全书五维评分 + 专业书评报告 + HTML 精读报告

**分层原则**：采集层（Perception + Tools）全是确定性代码，**不进 LLM、不进 checkpoint 图**；
认知层（Planning + LLM）才用模型。章节切分是**确定性节点**（目录 × 每页章节号），
不用 LLM —— 零编造风险、零 token 成本。

主图（LangGraph）：
```
figures → load → summarize → diagnose → gate(HITL) → review → render
```

---

## 2. 快速开始

```bash
pip install -r requirements.txt
swiftc -O utils/ocr.swift -o utils/bin/ocr      # OCR 引擎（macOS Vision）

export OUT_DIR="$HOME/Documents/readbook"             # 产物目录由用户指定
python3 capture/login.py                          # 有头浏览器扫码登录（→ ~/.reading-agent/）
python3 capture/capture.py --book-url "<URL>" --out-dir "$OUT_DIR"
python3 agent/ocr.py       --out-dir "$OUT_DIR"
python3 agent/cli.py <书名> --out-dir "$OUT_DIR"
python3 agent/report.py --book <书名> --out-dir "$OUT_DIR"   # HTML 报告（可离线重建）
```

装成插件：
```bash
/plugin marketplace add sukunwei/reading-agent
/plugin install reading-agent@reading-agent
```

---

## 3. 硬约束（实测得出，改动前必读）

### 3.1 采集层

| 约束 | 原因 |
|---|---|
| **`device_scale_factor` 必须为 1** | DSF=2 时微信读书按设备像素排版、而 canvas 只留 CSS 尺寸 → **正文左右被切字**（双栏都出现半截字） |
| **视口固定 1280×815** | 分页随视口宽度变化，换宽度续跑会让 page_no ↔ 内容**静默错位**。视口写入 `run_config.json`，不一致直接拒绝续跑 |
| **强制切到「上下滚动双栏」阅读模式** | 工具栏 `.readerControls_item.isNormalReader` / `.isHorizontalReader` 是互斥按钮。该模式会被服务端持久化，**每次都要显式切换并校验**。双栏模式底部才有「上一页/下一页」按钮 |
| **翻页按钮用 `.renderTarget_pager_button_right`** | `.renderTarget_pager_content_right` 是外层 DIV，点它**无效** |
| **等 canvas 内容指纹连续 3 次不变才截图** | 插图比正文**后加载**，只等「内容变了」会截到空白图表 |
| **目录面板只能 CSS 隐藏** | 点 catalog 按钮关不掉（按钮被 `.wr_mask` 拦截），`display:none` 才有效 |
| **正文画布在 `<canvas>` 上，DOM 里没有正文** | 全页可见文本仅 4 个字符（一个章节标题）→ OCR 不可替代 |

### 3.2 OCR 层

| 约束 | 原因 |
|---|---|
| **OCR 前按 canvas 边界临时裁栏** | 整页左右两栏，Vision 按 y 分行会把两栏同行并成一行 → 阅读序变成 `L1 R1 L2 R2…`（实测 30 页：**8.2% 的行跨栏，每页都有**）。裁栏只在 OCR 阶段临时做，**产物目录不留多余图片** |
| **裁栏时排除 `.renderTarget_pager` 那一行** | 否则「上一页/下一页」混进正文（实测 100% 页面中招） |
| **插图判定必须两路信号交叉一致** | A 浅色块（图像层：正文深底 p50≈28 / 图表浅底 p50≈141）+ B 图注模式（文本层：`图X-Y` 在**行首**且行**短**）。单用任一个都会误判 |
| 浅色块检测**先 BoxBlur(10) 再投影** | 图表里的深色网格线/柱子会把行投影**切碎**（某行只有 15 个亮像素 < 阈值 102）→ 漏检。模糊后通过率 28%→89% |
| 浅色块还要看**块内亮像素占比 > 3%** | 排除大标题误检（真图表 8.4% vs 标题 0.26%） |
| 图注必须**行首 + 行短** | 正文里的引用「…才有了图1-3」会被误判成图注 |
| 图与图注**按几何位置配对** | 图注在图正下方；按索引配对会错位 |
| 行 bbox 是**裁栏图坐标** | 必须换算成整页像素坐标才能和图表框对齐 |
| **不能用「栏边缘有墨」判断切字** | 微信读书正文本来就贴到栏边界（无内边距），实测 42/56 栏边缘有墨却属正常排版 |

### 3.3 LLM 层

| 约束 | 原因 |
|---|---|
| **结构化输出必须有降级路径** | 部分 Anthropic 兼容端点**接受** `output_config.format` 但**不强制** JSON，返回散文 → 校验失败。降级：JSON Schema 写进 prompt + 本地 pydantic 校验 + 带错误信息修复重试 |
| **降级一次后要记住，别再试原生** | 该端点的原生路径会**挂起数分钟**（`API_TIMEOUT_MS=3000000` = 50 分钟超时） |
| **默认模型不写死 `claude-opus-5-5`** | 走第三方端点时该模型不存在 → 404。顺序：`LLM_MODEL` → `ANTHROPIC_MODEL` → 官方默认 |
| **并行前必须先 `warmup()`** | 多个 worker 同时去试那个会挂起的原生路径 → 并行反而比串行慢。实测 8 章：串行 34.0s/章 vs 并行 14.1s/章（2.4×） |
| **批量视觉 `max_tokens` 必须给 16000** | 多图时模型先思考，给 4000 会 `stop_reason=max_tokens` 而**一个字都吐不出来**。6 图/批 ≈ 5.6s/张（串行）；**4 worker 并行 ≈ 1.2s/张**（实测 24 张：27.8s） |
| 解析模型输出用 **`raw_decode()`** | 模型偶尔输出「示例 JSON + 正式 JSON」两段，`find('{')..rfind('}')` 会报 "Extra data" |
| 原生路径返回 **dict** 时要转成 schema 实例 | 实测该端点偶发返回原始 dict → `'dict' object has no attribute 'model_dump'`。`_structured_native` **和 `structured_vision` 的原生分支**都要转（后者曾漏掉 → 视觉识别全军覆没） |
| **视觉调用要单独限超时** | 客户端默认超时 = `API_TIMEOUT_MS`（全局 3000000ms = 50 分钟）→ 一次挂起能把 worker 卡死 50 分钟。视觉批次单独给 300s（`LLM_VISION_TIMEOUT`） |
| **提示词模式的失败重试不写台账** | `_record` 只在拿到响应后调用 → schema 校验失败的重试**在 usage.jsonl 里完全看不见**。台账「没动静」不等于「没在跑」 |

### 3.4 插图层

| 约束 | 原因 |
|---|---|
| **批量视觉必须用绝对路径** | `rec["crop"]` 存的是**相对 book_dir** 的路径（JSON 里好看），而读文件是按**进程 cwd** 解析的 → 在别处跑 CLI 时每批都 `FileNotFoundError` → 静默退化成单图调用（479 张图 = 414 次调用，56 分钟） |
| **视觉结果每批增量落盘** | 以前 `figures.json` 只在全部跑完才写 → 中途被掐，几十次视觉调用全白费。现在每批写一次（原子替换） |
| **缓存只复用成功的描述** | 把 `error` 也当缓存 → 一次失败被永久缓存，那张图永远没有描述。失败必须重试 |
| **图号列是「无插图页」的正常产物** | `verdict=box_only` + 无图注 = 深色块误判或纯装饰图，进度行显示 `None None` 属正常 |

### 3.5 分层与依赖

| 约束 | 原因 |
|---|---|
| **`capture/` 不得 import `agent/`** | 采集层在认知层**之下**，下层依赖上层是倒置。两层只共用 `shared/` |
| 两层共用的东西放 `shared/` | `paths.py`（路径解析）、`stages.py`（计时与 token 台账）都被两层用到 |
| 反过来 `agent/` → `capture/` 允许 | 认知层要用采集层的能力（OCR、图表识别） |

### 3.6 流程层

| 约束 | 原因 |
|---|---|
| **产物目录由用户指定** | 不在 agent / plugin 目录里创建输出。`--out-dir` > `READING_AGENT_OUT` > `$PWD/reading-output` |
| **找书和产物必须用同一个 out_dir** | `pick_book()` 曾自己重新 `resolve_out_dir()` → 把 `--out-dir` 丢了 → 书在插件目录之外时永远报「找不到」 |
| **认知层节点必须套 `_timed()`** | 否则 `stages.json` 里没有认知层耗时 → HTML 报告的「总耗时」只统计到采集/OCR，严重低估 |
| **CLI 跑 LangGraph 要自己拼图** | `cli.py` 的 `run_graph` 没用 `graph.build_graph()` → 曾经连 `_timed` 一起漏掉 |
| **登录态单独放 `~/.reading-agent/`** | 不跟书产物混在一起（`state/` 含明文会话） |
| **插图描述必须标注「非原文」** | 视觉模型转述若被当成原文引用，会破坏忠实性护栏 |
| **OCR 原文保持纯净** | 注入插图块时**不写回** OCR 文件 —— 写回会把图注行替换掉，下次重跑时「图注模式」信号就消失了 |
| **续跑锚点 = (chapter_index, 页内序号)** | 切章节时 URL 不变；分页随视口变化 → 不能靠 URL 或全局页码 |
| 采集是**幂等**的：靠 `sha1` 跳过已采页 | 渲染确定性已验证：同一页重渲染字节一致 |

---

## 4. 书评 Agent 角色定义

> 运行时实现在 `agent/prompts.py`（`BOOK_REVIEW_SYSTEM`）+ `agent/schema.py`（`BookReview`）——
> **代码是唯一事实来源**，本节是同一角色的人读版本，改 prompt/schema 时两边一起改。

你是资深书评人，同时精通经济、金融、商业与通识写作。

**职责**
1. **逐章精读笔记**：基于单章原文产出结构化笔记（主旨 / 要点 / 概念 / 案例 / 数据 / 思考 / 疑问）
2. **全书五维评测**：逻辑结构性 / 内容干货密度 / 观点创新度 / 落地实用性 / 阅读启发价值，各 1-10 分
3. **整合版读书资产**：把逐章笔记与全书评测合成一份可读的 markdown

**硬性要求**
- **只使用原文出现的内容**。不得引入原文没有的事实、外部知识或臆测。
- 每个评分依据都要**引用书中的具体内容**（论断、案例、数据、章节），不能泛泛而谈。
- **允许批评**。逻辑漏洞、重复啰嗦、案例单薄、结论超出证据，都要如实指出。短板比套话更有价值。
- 评分要有**区分度**，不要所有维度都给 7 分。好书给高、差书给低。
- 专有名词按原文用词，不要换同义词。
- 输入里若出现「图N - 「标题:…」[插图描述:…]」，那是**视觉模型对图表的转述，不是原文**：
  可以据此理解图表内容，但**不得当作原文引用**，也不要在「案例论据」里写成作者原话。
- 某一项原文确实没有信息，就留空，不要凑数。

**评级规则**

| 平均分 | 评级 |
|---|---|
| ≥ 9.0 | 神作 |
| 7.5 – 8.9 | 优质好书 |
| 6.0 – 7.4 | 值得一读 |
| 4.5 – 5.9 | 中规中矩 |
| < 4.5 | 价值较低 |

**固定输出结构**

逐章笔记：
```
# <章节标题>
> 章节序号 / 层级 / 页码范围 / 正文字数
## 核心主旨  ## 关键要点  ## 重要概念 / 术语
## 案例论据  ## 关键数据  ## 我的思考  ## 待验证疑问
```

全书评测：
```
# 《书名》全书评测报告
> 底稿质量：OCR 覆盖率 / 平均置信度 / 平均字每页
## 一、书籍整体定位
## 二、五维评分明细        （表格：维度 | 评分 | 依据）
## 三、本书核心优势
## 四、本书短板与局限性
## 五、适合人群 / 不适合人群
## 六、高效阅读建议
```

---

## 5. 目录结构

```
docs/                方案评审与实测依据
.claude-plugin/       插件清单（plugin.json + marketplace.json）
.claude/skills/       项目级 skill（reading-agent + python-best-practices 三件套）
commands/            /read-book 命令
capture/             采集层（Playwright）
  login.py                  有头浏览器扫码登录
  extract_chrome_cookies.py 从运行中的 Chrome 提取登录态（免扫码）
  capture.py                逐页截图
agent/               认知层（LangGraph）
  ocr.py                    分页 OCR + 质量闸门
  figures.py                插图分流（检测 + 视觉识别 + 内联正文）
  schema.py / llm.py / prompts.py
  nodes/{clean,split,summary,diagnose,review}.py
  graph.py / cli.py / report.py
shared/              两层共用（不依赖任何一层）
  paths.py                 路径解析（产物/登录态目录由用户指定）
  stages.py                阶段计时 + token 台账
utils/ocr.swift      OCR 引擎（macOS Vision）→ 编译为 utils/bin/ocr，被 Python 用 subprocess 调
```

**依赖方向**：`capture/` 和 `agent/` 是**平级的两层**，都只依赖 `shared/`。
**不允许 `capture/` import `agent/`**（下层不能依赖上层）。
反过来 `agent/` 可以依赖 `capture/`（认知层要用采集层的能力）。

---

## 6. 产物

```
<OUT_DIR>/<书名>/
├── screenshots/<NNN_章名>/page_NNNN.png    整页 1280×815（不含 header）
├── ocr_pages/<NNN_章名>/page_NNNN.txt      一页一 TXT
├── chapters/<NNN>.json / <NNN>.md          逐章总结（可单章重跑）
├── figures/figures.json                    插图记录与视觉描述
├── chapter_summaries.md  book_review.md  full_readnote.md  full_text.txt
├── quality_report.json  diagnosis.md  chapters.json  book_review.json
├── report.html                              ★ HTML 精读报告（单文件，双击即看）
├── report.json
└── .state/    pages.jsonl  run_config.json  stages.json  usage.jsonl  *.ckpt.sqlite
```

---

## 7. 工具链

`pyproject.toml` 固化了 ruff（E/F/W/I/UP/B/SIM/PTH/RUF）、mypy、pytest 配置。
`ruff check .` 必须全绿。中文全角标点按 RUF001/002/003 忽略（中文项目本该用全角）。

---

## 8. 合规

- 只处理用户**已购或有合法阅读权限**的书籍。
- `state/` 含明文凭据 → 已 gitignore，**不要提交**。LLM 凭据走继承的环境变量，不落盘。
- 产物不外传。
