# CLAUDE.md

微信读书整本书采集与精读 Agent。本文件是**这个仓库的操作记忆**：第 2 节每一条都是踩坑实测出来的，违反了会**静默产出错误结果**（不报错、退出码 0，只是内容少了或错了）。

---

## 1. 快速开始

```bash
pip install -r requirements.txt
swiftc -O utils/ocr.swift -o utils/bin/ocr      # OCR 引擎（macOS Vision）

export OUT_DIR="$HOME/Documents/readbook"         # 产物目录由用户指定
python3 capture/login.py                          # 有头浏览器扫码登录（→ ~/.reading-agent/）
python3 capture/capture.py --book-url "<URL>" --out-dir "$OUT_DIR"
python3 agent/ocr.py       --out-dir "$OUT_DIR"
python3 agent/cli.py <书名> --out-dir "$OUT_DIR"
python3 agent/report.py --book <书名> --out-dir "$OUT_DIR"   # HTML 报告（可离线重建）
```

装成插件：`/plugin marketplace add sukunwei/reading-agent` → `/plugin install reading-agent@reading-agent`

**分层**：`capture/`（采集）与 `agent/`（认知）是**平级的两层**，都只依赖 `shared/`。
采集层是确定性代码，**不进 LLM、不进 checkpoint 图**；认知层才用模型。
章节切分是**确定性节点**（目录 × 每页章节号），不用 LLM —— 零编造风险、零 token 成本。

主图：`figures → load → summarize → diagnose → gate(HITL) → review → render`

---

## 2. 硬约束（改动前必读）

### 2.1 采集层

| 约束 | 原因 |
|---|---|
| **`device_scale_factor` 必须为 1** | DSF=2 时微信读书按设备像素排版、canvas 只留 CSS 尺寸 → **正文左右被切字** |
| **视口固定 1280×815** | 分页随视口宽度变化，换宽度续跑会让 page_no ↔ 内容**静默错位**。写入 `run_config.json`，不一致直接拒绝续跑 |
| **强制切到「上下滚动双栏」阅读模式** | `.readerControls_item.isNormalReader` / `.isHorizontalReader` 是互斥按钮，且该模式被服务端持久化 → **每次都要显式切换并校验**。只有这个模式底部才有翻页按钮 |
| **翻页用 `.renderTarget_pager_button_right` 且必须真实指针点击** | `.renderTarget_pager_content_right` 是外层 DIV，点它无效；JS 派发的 `b.click()` 在**章末**也无效（阅读器把「翻到下一章」挂在真实指针事件上）→ 内容恒定 → 被判成「翻页失效」→ **每章末尾静默停住**。对照实测：JS 点击内容不变，真实点击才推进 |
| **目录项 → 页码必须单调前进匹配，不能取首次匹配** | 目录可能有同名条目（实测一本书 8 个章末都叫「习题」）→ 取首次匹配会让所有习题页都记成第 1 章那个 → `chapter_index` 全错 → 下游把习题节定位到错误章节。页面顺序单调 → 从当前位置往后找第一个同名项 |
| **等 canvas 指纹连续 3 次不变才截图** | 插图比正文**后加载**，只等「内容变了」会截到空白图表 |
| **目录面板只能 CSS 隐藏** | 点 catalog 按钮关不掉（被 `.wr_mask` 拦截），`display:none` 才有效 |
| **正文画布在 `<canvas>` 上，DOM 里没有正文** | 全页可见文本仅 4 个字符（一个章节标题）→ OCR 不可替代 |

### 2.2 OCR 层

| 约束 | 原因 |
|---|---|
| **OCR 前按 canvas 边界临时裁栏** | 整页左右两栏，Vision 按 y 分行会把两栏同行并成一行 → 阅读序变成 `L1 R1 L2 R2…`（实测 30 页：8.2% 的行跨栏，每页都有）。裁栏只在 OCR 阶段临时做，**产物目录不留多余图片** |
| **裁栏时排除 `.renderTarget_pager` 那一行** | 否则「上一页/下一页」混进正文（实测 100% 页面中招） |
| **不能用「栏边缘有墨」判断切字** | 微信读书正文本来就贴到栏边界（无内边距），实测 42/56 栏边缘有墨却属正常排版 |
| **插图判定必须两路信号交叉一致** | A 浅色块（图像层）+ B 图注模式（文本层：`图X-Y` 在**行首**且行**短**）。单用任一个都会误判 |
| 浅色块检测**先 BoxBlur(10) 再投影**，且看**块内亮像素占比 > 3%** | 深色网格线/柱子会把行投影切碎 → 漏检（模糊后通过率 28%→89%）；亮像素占比排除大标题误检（真图表 8.4% vs 标题 0.26%） |
| 图注必须**行首 + 行短**，图与图注**按几何位置配对** | 正文里的引用「…才有了图1-3」会被误判成图注；按索引配对会错位 |
| 行 bbox 是**裁栏图坐标** | 必须换算成整页像素坐标才能和图表框对齐 |

### 2.3 LLM 层

| 约束 | 原因 |
|---|---|
| **每条调用路径都要单独限超时** | 客户端默认超时 = `API_TIMEOUT_MS`（50 分钟），而端点偶尔**挂起**（既不返回也不报错）→ 一次挂起就是 50 分钟静默停顿。分别用 `LLM_NATIVE_TIMEOUT=180` / `LLM_PROMPT_TIMEOUT=180` / `LLM_VISION_TIMEOUT=300`。`text()` 有 4 次重试，不限就是 200 分钟 |
| **结构化输出必须有降级路径** | 部分 Anthropic 兼容端点**接受但不强制** JSON，返回散文 → 校验失败。降级：JSON Schema 写进 prompt + 本地 pydantic 校验 + 带错误信息修复重试 |
| **降级一次后要记住，别再试原生** | 该端点的原生路径会挂起数分钟 |
| **默认模型不写死 `claude-opus-5-5`** | 走第三方端点时该模型不存在 → 404。顺序：`LLM_MODEL` → `ANTHROPIC_MODEL` → 官方默认 |
| **本机端点必须串行**（`LLM_WORKERS=1`、`LLM_FIGURE_WORKERS=1`） | 官方端点实测并行快 2.4×（串行 34.0s/章 vs 并行 14.1s/章），但**本机的第三方端点并发必挂**（单请求 8~34s 正常）→ 用官方端点的结论套到本机会挂死 |
| **并行前必须先 `warmup()`** | 多个 worker 同时去试会挂起的原生路径 → 并行反而更慢 |
| **批量视觉 `max_tokens` 必须给 16000** | 多图时模型先思考，给 4000 会 `stop_reason=max_tokens` 而**一个字都吐不出来** |
| 解析模型输出用 **`raw_decode()`** | 模型偶尔输出「示例 JSON + 正式 JSON」两段，`find('{')..rfind('}')` 会报 "Extra data" |
| 原生路径返回 **dict** 时要转成 schema 实例 | 该端点偶发返回原始 dict → `'dict' object has no attribute 'model_dump'`。`_structured_native` **和 `structured_vision` 的原生分支**都要转（后者曾漏掉 → 视觉识别全军覆没） |
| **提示词模式的失败重试不写台账** | `_record` 只在拿到响应后调用 → schema 校验失败的重试**在 usage.jsonl 里完全看不见**。台账「没动静」不等于「没在跑」 |

### 2.4 插图层

| 约束 | 原因 |
|---|---|
| **批量视觉必须用绝对路径** | `rec["crop"]` 存的是**相对 book_dir** 的路径，而读文件按**进程 cwd** 解析 → 在别处跑 CLI 时每批 `FileNotFoundError` → 静默退化成单图调用（479 张图 = 414 次调用，56 分钟） |
| **视觉结果每批增量落盘** | 以前 `figures.json` 只在全部跑完才写 → 中途被掐，几十次视觉调用全白费。现在每批写一次（原子替换） |
| **缓存只复用成功的描述** | 把 `error` 也当缓存 → 一次失败被永久缓存，那张图永远没有描述。失败必须重试 |
| **图号列是「无插图页」的正常产物** | `verdict=box_only` + 无图注 = 深色块误判或纯装饰图，进度行显示 `None None` 属正常 |

### 2.5 流程层

| 约束 | 原因 |
|---|---|
| **`capture/` 不得 import `agent/`** | 采集层在认知层**之下**，下层依赖上层是倒置。两层只共用 `shared/` |
| **产物目录由用户指定** | 不在 agent / plugin 目录里创建输出。`--out-dir` > `READING_AGENT_OUT` > `$PWD/reading-output` |
| **找书和产物必须用同一个 out_dir** | `pick_book()` 曾自己重新 `resolve_out_dir()` → 把 `--out-dir` 丢了 → 书在插件目录之外时永远报「找不到」 |
| **登录态单独放 `~/.reading-agent/`** | 不跟书产物混在一起（`state/` 含明文会话） |
| **认知层节点必须套 `_timed()`** | 否则 `stages.json` 里没有认知层耗时 → HTML 报告的「总耗时」只统计到采集/OCR，严重低估 |
| **CLI 跑 LangGraph 要自己拼图** | `cli.py` 的 `run_graph` 没用 `graph.build_graph()` → 曾经连 `_timed` 一起漏掉 |
| **采集是幂等的：靠 `sha1` 跳过已采页** | 渲染确定性已验证：同一页重渲染字节一致 |
| **插图描述必须标注「非原文」** | 视觉模型转述若被当成原文引用，会破坏忠实性护栏 |
| **OCR 原文保持纯净** | 注入插图块时**不写回** OCR 文件 —— 写回会把图注行替换掉，下次重跑时「图注模式」信号就消失了 |

### 2.6 章节切分

| 约束 | 原因 |
|---|---|
| **`chapter_index` 只到一级/二级章** | 采集时读的是页 header 的章标题（`.renderTargetPageInfo_header_chapterTitle`），微信读书**只在那里渲染篇/章名** → 三级小节永远不会成为 header，`pages.jsonl` 里查不到 |
| **三级小节边界必须靠 OCR 标题行定位** | 没有真实起始页的目录项若靠「起始页严格递增 +1」补位 = **凭空造边界**：实测某书 79 个目录项里 **56 个边界是伪造的**，正文整段错位（12.4「高考顾问」装的是旅行规划正文）→ 改为在 OCR 文本里找「编号 + 标题」行 |
| **标题行匹配要容忍 OCR 噪声** | Vision 把 `AI` 读成 `Al`（小写 L）、在词中间插空格（「AppBuilder平 台」）→ 比较前先 `_norm_heading()`（去空格 + `Al`→`AI`）；编号后不能跟 `.` 或数字（排除 `13.2.1 目标功能` 冒充 `13.2`） |
| **章节边界要按**行**切，不是按页** | 小节标题常落在**页的中间**（p373 上半页是 13.1 的收尾，下半页才是「13.2」）。只按页切 → 新章凭空多出上一节的尾巴、上一节又少了这半页。行级切分两边都不丢（校验：0 重叠行 / 0 漏行） |
| **边界页归属**：下一章**没有**标题行时（起点来自 `chapter_index`），整页归下一章 | 否则边界页被两章各算一遍 → 总字数虚高（实测虚高 2 万字） |

---

## 3. Agent 角色定义

**运行时实现在 `agent/prompts.py` + `agent/schema.py` —— 代码是唯一事实来源，本节只是索引，改 prompt/schema 时以代码为准。**

- **逐章精读笔记**：`CHAPTER_SUMMARY_SYSTEM` —— 基于单章 OCR 正文产出结构化笔记。只用原文内容、每个要点对应具体论断、按原文用词、不凑数。
- **全书五维评测**：`BOOK_REVIEW_SYSTEM` —— 逻辑结构性 / 内容干货密度 / 观点创新度 / 落地实用性 / 阅读启发价值，各 1-10 分。评分依据必须引用书中具体内容；允许批评；评分要有区分度。
- **评级规则**：`schema.py` 的 `BookReview.level` —— ≥9.0 神作 / 7.5-8.9 优质好书 / 6.0-7.4 值得一读 / 4.5-5.9 中规中矩 / <4.5 价值较低。

---

## 4. 目录结构与产物

```
capture/             采集层（Playwright）—— login.py / capture.py
agent/               认知层（LangGraph）—— ocr.py / figures.py / schema.py / llm.py / prompts.py
                     nodes/{clean,split,summary,diagnose,review}.py · graph.py / cli.py / report.py
shared/              两层共用（不依赖任何一层）—— paths.py / stages.py
utils/ocr.swift      OCR 引擎（macOS Vision）→ 编译成 utils/bin/ocr，被 Python 用 subprocess 调
```

```
<OUT_DIR>/<书名>/
├── screenshots/<NNN_章名>/page_NNNN.png    整页 1280×815（不含 header）
├── ocr_pages/<NNN_章名>/page_NNNN.txt      一页一 TXT
├── chapters/<NNN>.json / <NNN>.md          逐章总结（可单章重跑）
├── figures/figures.json                    插图记录与视觉描述
├── chapter_summaries.md  book_review.md  full_readnote.md  full_text.txt
├── quality_report.json  diagnosis.md  chapters.json  book_review.json
├── report.html                              ★ HTML 精读报告（单文件，双击即看）
└── .state/    pages.jsonl  run_config.json  stages.json  usage.jsonl  *.ckpt.sqlite
```

---

## 5. 工具链与合规

- `pyproject.toml` 固化了 ruff（E/F/W/I/UP/B/SIM/PTH/RUF）、mypy、pytest 配置。`ruff check .` 必须全绿。中文全角标点按 RUF001/002/003 忽略。
- 只处理用户**已购或有合法阅读权限**的书籍。
- `state/` 含明文凭据 → 已 gitignore，**不要提交**。LLM 凭据走继承的环境变量，不落盘。产物不外传。
