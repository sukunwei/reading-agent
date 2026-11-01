# 微信读书全书 OCR 总结 Agent —— 技术方案评审与 V1.3 修订建议

> 评审对象：`微信读书全书自动化截图OCR总结Agent设计文档 V1.2.txt`
> 评审依据：(1) `sample/` 4 张真实截图；(2) 线上 weread 网页版 reader 实况探测（Playwright 实测）；
> (3) 已有成熟实现 `../weread-screenshot/`（60 本书、864 页真实产出）
> 评审日期：2026-11-01

---

## 0. 结论摘要

**架构分层是对的，采集层几乎全是重复造轮子，且有 6 处与真实站点/真实语料相悖的假设。**

| 维度 | 结论 |
|---|---|
| 分层范式（确定性采集 + LLM 弱结构化 + 子Agent 高阶推理 + 人工兜底） | ✅ 成立，保留 |
| 「必须 OCR，不能取 DOM 文本」 | ✅ **实测成立**，这是方案最正确的判断 |
| 采集层实现方式 | ❌ 需重写：已有 60 本书验证过的实现，应直接复用而非重写 |
| OCR 引擎选型（rapidocr-onnxruntime） | ❌ 应改为 macOS Vision（已有实现、中文更好、更快、零依赖）→ ✅ **本项目已优化完成** |
| ChapterSplitAgent（LLM 切章节） | ❌ **应整块删除**：目录与每页章节名都在 DOM 里，零成本且 100% 准确 |
| 幂等/续跑设计 | ⚠️ 概念对，**「空翻页对齐进度」在真实站点上不成立**，需换实现 |
| 「零污染截图」 | ⚠️ 目标对，机制错（ESC 关闭 ≠ 消除 chrome），实测 100% 页面污染 |
| **截图为 canvas 而非整视口** | ❌ **不只是「干净」，是正确性前提**：整视口含左右两栏，OCR 阅读序错乱，实测 8.2% 的行跨栏、**每页都有**；canvas 单栏实测 0% |
| LangGraph 承载 800+ 页采集 | ❌ 过重，应只承载认知层 |
| 全书五维打分 | ✅ 可行，但缺 token 预算、缺 OCR 质量闸门、缺插图处理 |

**一句话建议**：把 V1.2 的「采集层」整个替换为 `weread-screenshot` 的成熟实现并做 3 处增强（**canvas 级截图**、章节元数据落盘、OCR 覆盖率统计），把省下来的工程量全部投到认知层（清洗 / 逐章精读 / 全书评测），并**删掉 ChapterSplitAgent**。

> 📌 **开发计划见 [`开发计划.md`](./开发计划.md)**（阶段划分 / 目录结构 / 验收标准 / 风险登记 / 里程碑）。

---

## 1. 真实样本评估

### 1.1 `weread-study-agent/sample/` 四张截图（用户手工采集）

用 numpy/PIL 对四张图做了像素级几何分析（`/tmp/wr_ocr_analysis/analyze*.py`）：

| 样本 | 设备像素 | 折算 CSS (DPR=2) | 正文墨迹占比 | 正文列 x 范围 (CSS) | 备注 |
|---|---|---|---|---|---|
| page-01.png | 2866×1328 | 1433×664 | 6.9% | 336..537（封面）+ 778..1172（信息面板） | 扉页/书籍信息页 |
| page-01-with-menu.png | 2852×1260 | 1426×630 | 7.9% | 771..1247 被目录面板覆盖 | 目录遮挡样本 |
| page-02.png | 2844×1288 | 1422×644 | **3.1%** | 750..1224，左侧 490px **完全空白** | 正文页（跨页排版） |
| page-end.png | 2862×1276 | 1431×638 | 1.3% | 394..486（"全书完"） | 末页 |

**从样本直接读出的 5 个事实：**

1. **截图是整视口，不是正文区。** 四张图里都含：顶部导航栏（书名｜首页｜我的书架｜头像，y=25..44 CSS）、右侧 7 个常驻工具按钮（x=1330..1355 CSS）、底部「上一页/下一页」。V1.2 声称「全书纯净逐页截图（无菜单、无遮挡、无弹窗）」——**现状一个都没达到**。

2. **正文列只占画面 ~32%。** page-02 的正文在 x=750..1224，左边 490 CSS px 是空白（跨页排版的另一半）。整帧墨迹仅 3.1%，OCR 有效面积被浪费在 chrome 和空白上。

3. **视口宽度不固定。** 四张样本 CSS 宽度分别是 1433 / 1426 / 1422 / 1431。分页是视口相关的 —— 这直接击穿 V1.2 §3.1 的续跑假设（见 §3.2）。

4. **正文页是「双页对开」，不是一个 book page。** V1.2 的 `page_no` 与书中真实页码是 2:1 的关系，章节 `start_page/end_page` 会整体错位。

5. **扉页与末页是可识别的。** page-01 是书籍信息页（推荐值 80.5%、字数 10.7 万字），page-end 是「全书完」。这两类页面不需要 OCR 摘要，但也不能漏采。

### 1.2 线上 reader 实况探测（Playwright 实测，非推断）

对 `https://weread.qq.com/web/reader/95332ad0...` 实测：

| 探测项 | 结果 | 对方案的意义 |
|---|---|---|
| 正文是否在 DOM | **否**。`visible_body_text_chars = 4`（全页唯一文本节点是章节标题） | ✅ **OCR 不可替代，方案核心前提正确** |
| `[data-wr-role="text"]` 元素数 | **0**（有样式规则，无元素） | 无隐藏文本层可偷 |
| 正文渲染载体 | `<canvas>`：vw=1426 时 2 个 canvas，各 904×1000 backing / 452×500 CSS | **截图对象应是 canvas，不是视口** |
| canvas 能否导出 | ✅ `canvas.toDataURL('image/png')` 成功 | 可零 chrome 取页 |
| canvas 分辨率规律 | backing = CSS尺寸 × DPR，页高固定跟随视口高 | **视口高 → 单 canvas 内容更多；DPR → 文字更清晰** |
| 每页章节名 | ✅ `.renderTargetPageInfo_header_chapterTitle` = "版权信息" | **零成本拿到页↔章节映射** |
| 目录 | ✅ DOM 文本，`.readerCatalog_list_item_title_text`，两级层级（62 项） | **零成本拿到权威章节表** |
| 目录按钮 | `button.readerControls_item.catalog`（title="目录"） | 稳定选择器 |
| 章节切换时 URL | **不变**（实测注释确认："weread reader 的 URL 不会因切章节改变"） | ❌ 击穿 V1.2 的 URL/空翻页续跑假设 |
| 付费墙 | `wr_horizontal_reader_needPay_container`（未登录必现） | 必须登录**且会员有效** |
| 字体 | `wr_various_font_provider_wrapper` 注入 `wr_default_font` | 自定义 webfont，需注意渲染时序 |
| `document.fonts.ready` | 永不 resolve（持续 FontFace.add） | Playwright 截图会卡 90s，必须绕过 |

### 1.3 已有实现的真实语料（864 页，《穷查理宝典》）

用 `swift ocr.swift`（macOS Vision）跑出的 864 个 TXT 全量体检：

```
files: 864
total chars      : 432,952
chars/page  mean : 501   median: 513   min: 26   max: 771
empty pages      : 0
consecutive duplicate pages : 0
pages with >60% repeated lines (OCR 幻觉) : 0
pages with pager chrome 「上一页/下一页」泄漏 : 864  (100.0%)
```

**结论：采集可靠性极高（0 空页 / 0 重复 / 0 幻觉），唯一系统性缺陷是底部翻页按钮 100% 混进正文。**

样本 OCR 错误实例（真实肉眼可验）：
- `专厲权利` ← 权利（生僻字误识）
- `美国芳工部` ← 劳工部（**改变语义**）
- `看新间` ← 看新闻
- `我的教育背景一直国绕` ← 围绕
- 跨行断字：`授权微信读` / `书进行制作`（视口越宽断行越少，见下）

**分辨率对比实测**（同一页，不同视口/DPR，均用 macOS Vision）：

| 配置 | canvas 尺寸 | 识别字数 | 断行情况 |
|---|---|---|---|
| vw1280×900 dsf1（旧实现等效） | 393×770 | 139 | `授权微信读`↵`书进行制作` |
| vw1426×900 dsf2 | 904×1540 | 139 | `授权微信读书进行制`↵`作与发行` |
| vw1920×900 dsf2 | 1298×1540 | 140 | **无断行，干净** |
| vw1400×1800 dsf2（高视口） | 882×3340 | **796** | 单 canvas 吞吐 5.7× |

**推论**：
- 旧实现的等效分辨率（393px 宽页 + dsf=1）是**最低配置**，错误率天然最高；
- **视口宽度**主要影响断行美观与横向吞吐（2 canvas/屏 = 2 页/屏）；
- **视口高度**是吞吐主杠杆（单 canvas 内容量 5.7×），**DPR** 是清晰度杠杆；
- 综合最优：`viewport ≈ 1920×1200, deviceScaleFactor = 2`。

---

## 2. 设计文档逐节评审

### 2.1 §2 工作流链路

| 节点 | 评审 | 修订 |
|---|---|---|
| 登录态校验 | ⚠️ 缺会员态检测 | 增加「付费墙 DOM 探测」= 未付费/未登录/会员失效 |
| **（缺）目录抓取** | ❌ **缺失** | 新增前置节点：抓 TOC → 得到权威章节表 + 层级 + 章节起点 |
| 全书逐页截图 | ❌ 对象错 | 改为「canvas 级页面截图」，非整视口截图 |
| 菜单遮挡异常 → 人工 | ⚠️ 机制错 | ESC 关闭治不了常驻 chrome；改 CSS 注入 + 直接不截 chrome |
| 页面加载报错重试 | ⚠️ 判据不足 | 增加「内容哈希变化」判据（见 §2.2） |
| 增量分页 OCR | ✅ 方向对 | 换 Vision 引擎 + 增加质量闸门 |
| OCR 文本清洗 | ⚠️ 描述太粗 | 已有成熟实现可直接搬（见 §3.4） |
| ChapterSplitAgent | ❌ **应删除** | TOC + 每页 chapterTitle 已覆盖，且更准 |
| ChapterSummaryAgent | ✅ 保留 | 输入改为「干净全文 + 权威章节边界」 |
| BookReviewAgent | ⚠️ 缺工程约束 | 补 token 预算、层级摘要、插图分流 |
| Markdown 导出 | ⚠️ 缺中间产物 | 补 `chapters/*.json`（支持单章重跑）+ `full_text.txt` |

### 2.2 §3.1 幂等与断点续跑 —— 真实站点下不成立

V1.2 原文：「浏览器回到首页，**空翻页对齐进度**，从断点下一页继续采集」。

**三个致命问题：**

1. **URL 不随章节变化**（实测），所以无法用 URL 定位断点，只能盲翻。
2. **空翻页是 O(n)**：一本 300 页书断在第 200 页，恢复要空点 200 次 × 每次 ≥1.2s ≈ 4 分钟，还每次都可能翻错。
3. **分页随视口宽度变化**（样本实测：1422/1426/1431/1433 CSS）。**用户换个窗口宽度续跑，page_no ↔ 内容映射会静默错位** —— 不报错，只是内容全乱。这是最危险的一类 bug。

**正确做法（已有实现已验证）：**
- 续跑锚点用 **TOC 章节索引 + 章内页序号**，不用页码也不用 URL；
- 每页记录 `chapter_title`（DOM 免费提供）+ `canvas_sha1`，恢复时直接校验；
- `viewport` / `dpr` 写进状态文件，**不一致即拒绝续跑**（或强制按记录的视口重开）；
- 幂等判据从「文件存在」升级为「文件存在 + sha1 记录 + OCR 字符数 > 阈值」。

**pages.jsonl 建议升级：**
```json
{"page_no":12,"chapter_title":"第一章 日本之殇·一 泡沫是怎样产生的","chapter_index":3,
 "img":"0003/0012.png","sha1":"3f9a…","txt":"0003/0012.txt","chars":487,"ocr_ok":true}
```

### 2.3 §3.2 登录态持久化

| 文档说法 | 实况 | 建议 |
|---|---|---|
| `storage_state.json` 存 Cookie/LocalStorage | ✅ 概念正确 | 保留 Playwright 原生 `storage_state`，比自研 Netscape 格式更省事 |
| — | ⚠️ **漏了 profile warmup** | 导出前必须先访问 `/web/bookshelf` 并等头像元素出现，否则 `wr_name/wr_avatar/wr_gender` 的 cookie value 为空 |
| — | ⚠️ **漏了会员态** | 必须探测 `wr_horizontal_reader_needPay_container` |
| — | ⚠️ **漏了失效回退** | cookie 失效时应回落到「借用本机 Chrome profile」重新扫码，而非死等人工 |
| — | ✅ | `cookie.txt` / `storage_state.json` 必须 gitignore + chmod 600 |

### 2.4 §3.3 截图防污染 —— 目标对，机制错

实测污染源是**常驻 chrome**，不是「弹窗」：

| 污染源 | CSS 位置 | ESC 能关吗 |
|---|---|---|
| 顶部导航 `.readerTopBar` | y=25..44 | 不能 |
| 右侧工具栏 `.readerControls_item` | x=1330..1355 | 不能 |
| 底部翻页 `.renderTarget_pager` | y≈577 | 不能 |
| 目录面板 `.readerCatalog_list` | 右侧 33% | 能 |

**根治方案（按优先级）：**
1. **直接对 `<canvas>` 元素截图** —— canvas 里只有正文，天然零污染、无需任何关闭逻辑；
2. 若必须整视口截图 → CSS 注入 `.readerTopBar,.readerControls_item,.renderTarget_pager{display:none}`；
3. 目录面板遮挡用「面板存在即重拍 + 抖动检测」判定。

> 保留 V1.2 的「被污染页面不标记完成、自动重跑」这条设计思想是对的，只是污染判据要从「ESC 后还有没有菜单」改成「像素级 chrome 区域检测」。

### 2.5 §3.4 页面加载失败自愈

V1.2：「空白/加载错误 → 重试 3 次，间隔 3 秒」。

问题：**固定 sleep 无法区分「翻页动画未完成」和「真的加载失败」**。已有实现给出的正解是**内容哈希**：

```
截完本页 → 算 sha1 → 若与上一张完全相同 → 说明还在过渡帧 → 等 300ms 重截（最多 3 次）
         → 3 次后仍相同 → 才判定「翻页失效 / 已绕圈」
```
并且用「本章所有已见 sha1 集合」检测**绕圈回卷**（weread 翻到章末会循环回章首）。

**还应补充**：
- 末页检测**不能用文本标记** —— 实测「上一页/下一页/全书完」出现在每页 footer，用 `body.innerText` 必然误判。已有实现已把 `END_OF_BOOK_MARKERS` 置空并改用哈希判定，这是踩过坑的结论。
- 加载中判定：等 `.readerChapterContentLoading` 消失 + `canvas` 数量稳定（≥1 且 backing 尺寸非 0）。

### 2.6 §4 三个子Agent

#### ChapterSplitAgent —— 建议删除

V1.2 用「LLM 读 OCR 全文抽章节」做切分。实测已有两个**零成本、零幻觉**的替代源：

| 信号 | 来源 | 可靠性 |
|---|---|---|
| 权威章节表（标题 + 层级） | `.readerCatalog_list_item_title_text` | 100%（站点自带） |
| 每页所属章节 | `.renderTargetPageInfo_header_chapterTitle` | 100%（每页实时给出） |

两个一交叉即可得到**精确的章节 → 页区间映射**，零 LLM 调用、零编造风险、零 token 成本。
LLM 切章还额外引入「章节名 OCR 错字 → 边界错位 → 整章总结跑偏」的复合风险。

#### ChapterSummaryAgent —— 保留，但输入要改

- 输入应为「该章节的干净全文（跨页拼接后）+ 权威章节标题 + 页区间」；
- 固定模板（主旨/要点/概念/案例/思考/疑问）合理，保留；
- 需要加**忠实性护栏**：只允许引用原文出现的概念，禁止推断；
- 需处理**跨页断裂**：OCR 视觉换行会切断句子，清洗节点已解决（见 §3.4）。

#### BookReviewAgent —— 保留，需补三项工程约束

1. **Token 预算**：10.7 万字 ≈ 107k 汉字 ≈ 100–150k tokens。单次长上下文可行，但必须显式做预算规划（分块摘要 → 全书摘要 → 评测三段式），不能"把全文丢进去祈祷"。
2. **插图分流**：图表/插图页 OCR 出来是乱码，会直接污染「内容干货密度」「落地实用性」两项评分。需检测低墨迹高复杂度的图像区域 → 走视觉模型描述，而非 OCR。
3. **OCR 质量闸门**：全书的评测建立在一份**含 ~0.4% 字错的**文本上。若不统计每章的 OCR 置信度/异常率，五维评分是建立在一份不可靠底稿上的。建议在评测报告里附「本报告基于 OCR 文本，字错率约 X%」的元信息。

### 2.7 §5 状态结构

缺字段：`viewport`、`dpr`、`canvas_sha1`、`ocr_chars`、`chapter_index`、`chapter_title`、`toc`（权威目录）、`book_meta`（字数/作者/推荐值）、`ocr_engine`、`ocr_error_rate`。
`pages: List[PageResult]` 全量常驻内存也不必要 —— 800+ 页 × 状态对象在 LangGraph checkpoint 里会持续膨胀，建议只存 `max_finish_page` + 索引，细节留在 jsonl。

### 2.8 §6 产物目录

V1.2：`screenshots/0001.png` + `ocr_pages/0001.txt` 扁平结构。

已有实现跑出了更成熟的结构，且**天然支持章节级重跑与人工核对**：

```
output/<书名>/
├── screenshots/<书名>/<NNN_章节名>/page_NNNN.png    # 按章分目录
├── ocr/<NNN_章节名>/page_NNNN.txt
├── chapters/<NNN_章节名>.json      # 单章总结（可单章重跑）
├── chapters/<NNN_章节名>.md        # 单章精读笔记（渲染产物）
├── book_review.md                  # 全书五维评测
├── full_readnote.md                # 整合版
└── full_text.txt                   # 清洗后全文（LLM 输入源 + 人工校对）
```

V1.1「取消全局 full_ocr.txt，全文由程序动态拼接」是**反作用**：认知层要反复读全文、用户要人工校对、评测要做 token 预算，都需要一个物化的全文文件。应恢复，但标注为「可重新生成的派生产物」。

### 2.9 §7 技术栈

| 文档选型 | 评审 | 建议 |
|---|---|---|
| `rapidocr-onnxruntime` | ❌ | 改 **macOS Vision**（本项目 `ocr.swift` 已优化完成，12 项改动，回归通过）。理由：中文准确率更高、零 Python 依赖、单页 ~3.2s、返回 bbox 可直接做阅读序聚类。Python 侧用 `subprocess` 调编译出的二进制即可 |
| `langgraph` 承载全流程 | ⚠️ 过重 | 采集层是长时确定性任务，不该进 checkpoint 图。**LangGraph 只管认知层**（清洗 → 逐章总结 → 全书评测 + HITL） |
| `langgraph-checkpoint-sqlite` | ✅ | 保留，用于认知层断点。注意 `SqliteSaver.from_conn_string()` 返回的是上下文管理器，要当 checkpointer 用得自己 `sqlite3.connect()` 持有连接 |
| `playwright` | ✅ | 保留。**全项目统一用 Python（`playwright` Python 包）**，不用 TypeScript —— 认知层是 Python，两种语言并存会让状态/序列化/类型定义分裂。Node 版 `screenshot.mjs` 只作为**行为参考**（选择器、等待策略、哈希判据），实现落在 `capture/capture.py` |
| `pydantic` | ✅ | 保留，用于认知层输出强校验 |
| 纯终端 CLI | ✅ | 保留，与已有实现的 `readline` 交互一致 |

---

## 3. 从 `weread-screenshot` 提取的技术实现清单

> 全部来自 `/Users/kunweisu/fe-project/weread-screenshot/`，**已跑过 60 本书真实产出**，直接复用而非重写。

### 3.1 登录态（`save-cookie.mjs` + `screenshot.mjs:194-325`）

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| 独立 Chromium + 注入 cookie，不碰本机 Chrome | `screenshot.mjs:300-325` | 免去「先关 Chrome」的痛苦，可无人值守 |
| **profile warmup**：先跳 `/web/bookshelf` 等头像出现，再导 cookie | `screenshot.mjs:229-250`、`save-cookie.mjs` | 不做这步，`wr_name/wr_avatar/wr_gender` 的 cookie value 全为空，登录态半残 |
| 导出时诊断「value 为空的 cookie 数量」 | `screenshot.mjs:214-219` | 提前暴露半残登录态 |
| 主动重访书籍 URL 判断是否被踢回登录页 | `screenshot.mjs:557-570` | 比看 DOM 标志可靠 |
| cookie 失效自动回落到本机 Chrome profile 重扫 | `README.md` | 真正实现「永久免登」 |
| cookie.txt 绝不入库 | `README.md` + `.gitignore` | 安全底线 |

### 3.2 截图（`screenshot.mjs:349-416`）

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| **`forceFontsReady`：劫持 `document.fonts.ready` 立即 resolve** | `screenshot.mjs:366-377` | ⚠️ weread 持续 `FontFace.add()` 让 `fonts.ready` 永不 pending，Playwright 内部等它 → **截图卡 90s 超时**。这是本方案最容易踩的坑 |
| `waitForFonts:false` + `timeout:90000` | `screenshot.mjs:385-390` | 同上，双保险 |
| **禁用 `animations:'disabled'`** | `screenshot.mjs:383` | ⚠️ 实测部分书籍（花叔那本）会被截成空白页 |
| 裁掉顶部 chrome：取 `.readerTopBar` 的 `y + height`（不是 height） | `screenshot.mjs:351-364` | 防止 margin/padding 把 header 顶下来；同时校验 `height < 视口一半` 防误命中大容器 |
| 截图失败 → 等 1s 重试一次（更短 timeout） | `screenshot.mjs:406-416` | 瞬时失败不致命 |

### 3.3 翻页与状态判定（`screenshot.mjs:682-698, 782-866, 941-1090`）

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| **SHA-1 内容哈希 = 翻页完成的唯一判据** | `screenshot.mjs:775-802` | 比固定 sleep 可靠得多；同一 hash = 过渡帧 → 等 300ms 重截，最多 3 次 |
| **章节内已见哈希集合 → 绕圈回卷检测** | `screenshot.mjs:727-743, 817-820` | weread 翻到章末会循环回章首；文本标记不可用（每页 footer 都有「上一页/下一页」） |
| 预载续传截图的哈希进集合 | `screenshot.mjs:733-743` | 断点续跑时不漏判绕圈 |
| **仅"新拍的"截图参与绕圈判定** | `screenshot.mjs:765-773, 817` | 已存在的续传截图必然命中预载集合，否则误停 |
| 翻页失败退化为键盘 `ArrowRight` | `screenshot.mjs:693-696` | 选择器失效时的兜底 |
| 首个截图前 `waitForLoadState('networkidle')` + 2s | `screenshot.mjs:984-989` | 从 bookshelf 跳回 reader 会留后台活动 |
| 已存在的旧页**不翻页** | `screenshot.mjs:1072-1078` | 续跑时避免空翻页（V1.2 §3.1 缺的关键点） |
| 连续 3 次文件大小一致 → 判定卡死 | `screenshot.mjs:1086-1091` | 第二重兜底 |
| 人工确认 c/s/q/r（继续/停本章/停全书/换 URL） | `screenshot.mjs:837-860` | 终局判断交给人，别让程序猜 |

### 3.4 章节与目录（`screenshot.mjs:570-680`）

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| **多候选选择器数组 + 首个命中即用** | `screenshot.mjs:125-154` | weread 改版频繁，单选择器必挂 |
| `button.renderTarget_pager_button:has-text("下一页")` | `screenshot.mjs:139` | 已验证的精确选择器 |
| `openCatalog` → `getChapterList` → `goToChapter` 三段式 | `screenshot.mjs:570-680` | 目录是章节权威源 |
| `getCurrentChapterIndex`：找侧栏高亮项定位当前章 | `screenshot.mjs:615-665` | **URL 不随章节变，高亮项才是续跑锚点** |
| 章节目录命名 `${序号}_${sanitize(标题)}` | `screenshot.mjs:700-705` | 排序天然正确，人眼可读 |
| **DEBUG 模式**：探测候选选择器 + dump 全量 `<a>/<button>` + 存 `initial.html` | `screenshot.mjs:452-543` | 选择器失效时的标准排障流程 |

### 3.5 OCR（`ocr.swift`）

> ✅ **已在本项目内优化完成** → `weread-study-agent/ocr.swift`（12 项改动，见 §6.5）。
> 下表是原版 `../weread-screenshot/ocr.swift` 中值得保留的机制。

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| macOS Vision `VNRecognizeTextRequest`，`.accurate` | `ocr.swift:46-51` | 中文准确率显著优于开源方案 |
| `recognitionLanguages = ["zh-Hans","en-US"]` | `ocr.swift:49` | 中英混排（书名/术语常有英文） |
| `usesLanguageCorrection = true` | `ocr.swift:50` | 修正常见错字 |
| `minimumTextHeight = 0.0` | `ocr.swift:51` | 小字号注释/脚注不漏 |
| **bbox 排序 + Y 聚类成行（阈值 0.012）** | `ocr.swift:64-92` | 保证阅读顺序，这是纯 OCR 库不给的 |
| **一次一文件 + `autoreleasepool`** | `ocr.swift:107-120` | Vision 占 GPU，并行不可靠且 rarely faster |
| 逐文件写同名 `.txt` | `ocr.swift:95-101` | ✅ 已满足 V1.2「一页一 TXT、图文一一对应」要求 |

**性能实测（新增）**：

| 方式 | 100 页 | 折算 |
|---|---|---|
| `swift ocr.swift` 解释执行 | 318s | 3.2s/页 |
| `swiftc -O` 编译后 | 105s/30页 | 3.5s/页 |
| 4 路并行（独立进程） | 246s | 2.46s/页（**仅 1.3×**） |

`user` 时间 3m49 / 5m27 —— 瓶颈是等 Vision 系统服务，不是 CPU，也不是编译开销。
**结论：不要在 OCR 内部做并行**（原版注释判断正确）；要跟**采集层**做流水线并行（两者争抢的资源不同）。

### 3.6 OCR 文本清洗（`aggregate_notes.py:26-52`）

这是 V1.2「OCR 文本清洗节点」的**已验证实现**，直接搬：

```python
# 1) 全角空格 → 普通空格
# 2) 每行 strip + 行内空白合并
# 3) 丢空行
# 4) ★ 全部行拼成单个段落 ← 关键：消除视觉换行对句子的切碎
# 5) 多空格 → 单空格
# 6) ★ 按中英句末标点重新分句：(?<=[。！？!?；;])
```
第 4 步是精髓：OCR 的视觉换行会把「制作|与发行」「专|利」切开，拼接回段落再按标点重切，语义完整性显著提升。

`aggregate_eden.py` 另有两个值得抄的点：
- `SKIP_RE` 跳过营销页 / Summary / Part / Chapter 标题页（`aggregate_eden.py:12`）
- 输出里带**页码范围 + OCR 覆盖页数**（`output/期权投资策略/章节_01_...md` 实测输出）
  > `> **页码范围**: 第 18 - 55 页（共 38 页）` / `> **OCR 覆盖**: 已识别 24 页`
  > 这就是 V1.2 缺失的「OCR 覆盖率」机制，应该升级成硬闸门：**覆盖率 < 阈值时该章不进入总结流程**。

### 3.7 编排与人工介入

| 实现 | 位置 | 为什么重要 |
|---|---|---|
| 环境变量分层续跑：`BOOK_URL` / `START_URL` / `START_CHAPTER_INDEX` / `START_PAGE_NUM` / `START_TOC_INDEX` | `run.sh:18-74`、`screenshot.mjs:92-123` | 4 个独立续跑锚点，覆盖所有中断场景 |
| `START_TOC_INDEX`（从 TOC 第 N 项开始） | `screenshot.mjs:113-123` | **URL 不能定位章节，TOC 索引可以** |
| `AUTO_CONFIRM=1` 全自动 / 留空则每章末人工确认 | `screenshot.mjs:88-90` | 同一套代码兼顾无人值守与人工兜底 |
| `BookDoneSignal` 异常穿透多层 await | `screenshot.mjs:22-29` | 人工判定「全书完成」时的干净退出机制 |
| 已有 60 本书 / `output/` 下 7 本已出成品 | `screenshots/`、`output/` | **回归测试集**：改采集层后可用这 60 本做 diff 验证 |

---

## 4. 建议的 V1.3 架构

```
┌─ 采集层（确定性，Node + Playwright，复用 screenshot.mjs）─────────────┐
│  登录态加载(storage_state + warmup + 会员态探测)                     │
│         ↓                                                            │
│  ★ TOC 抓取 → 权威章节表 [{index,title,level}]                        │
│         ↓                                                            │
│  ★ 视图固定(viewport=1920×1200, dsf=2) + CSS 隐藏 chrome             │
│         ↓                                                            │
│  逐页采集：canvas 元素截图 → screenshots/<NNN_章名>/page_NNNN.png     │
│    · 幂等：文件存在 + sha1 记录                                        │
│    · 换页判据：sha1 碰撞检测（过渡帧 / 绕圈）                          │
│    · 每页附带 DOM 免费元数据 chapter_title                            │
│    · 断点锚点 = (chapter_index, 页内序号)，非 URL / 非全局页码         │
│         ↓                                                            │
│  质量闸门：OCR 字符数 / 空页 / 重复页 / 覆盖率                          │
└──────────────────────────────────────────────────────────────────────┘
                              ↓ ocr_pages/<NNN_章名>/page_NNNN.txt
┌─ 认知层（LangGraph，只管这一层）─────────────────────────────────────┐
│  清洗节点（视觉换行拼回 + 标点重切 + 章节聚合）                         │
│         ↓                                                            │
│  章节切分节点（确定性：TOC × 每页 chapter_title 交叉，非 LLM）         │
│         ↓                                                            │
│  ★ 逐章总结（ChapterSummaryAgent，低成本模型，可单章重跑）            │
│         ↓                                                            │
│  ★ 全书评测（BookReviewAgent，长上下文 + 层级摘要）                    │
│         ↓                                                            │
│  渲染导出（chapter_summaries.md / book_review.md / full_readnote.md）  │
│  HITL：低置信章节 / 覆盖率不足章节 → 人工复核节点                      │
└──────────────────────────────────────────────────────────────────────┘
```

**相对 V1.2 的关键改动：**
1. 删 `ChapterSplitAgent`（LLM 切章）→ 换确定性节点；
2. 采集层移出 LangGraph，独立进程 + 文件态幂等；
3. 截图对象 = canvas 元素，非整视口；
4. 续跑锚点 = TOC 索引 + 页内序号；
5. OCR 引擎 = macOS Vision；
6. 新增 OCR 覆盖率闸门 + 插图分流；
7. 恢复 `full_text.txt` 作为派生产物。

---

## 5. 落地顺序与风险

| 阶段 | 内容 | 产出 | 风险 |
|---|---|---|---|
| P0 | 采集层换 canvas 截图 + 视口固定 + TOC 抓取 + 元数据落盘 | 一本书的 PNG + 权威章节表 | 低（选择器已有实测结论） |
| P1 | 清洗节点 + 覆盖率统计 + 质量闸门 | 干净 TXT + 覆盖率报告 | 低（清洗逻辑已有成熟实现） |
| P2 | 逐章总结 Agent（LangGraph） | chapter_summaries.md | 中（prompt 调优） |
| P3 | 全书评测 Agent + token 预算 + 插图分流 | book_review.md | 中（长上下文策略） |

**主要风险：**
1. **weread 改版** —— 选择器会失效。缓解：多候选选择器数组 + DEBUG 模式 + 60 本书回归集。
2. **会员/付费墙** —— 未付费账号只能拿到预览页。缓解：启动即探测付费墙 DOM 并显式报错，不要静默截半本书。
3. **OCR 底稿误差** —— ~0.4% 字错率，尤其生僻字与专有名词。缓解：覆盖率 + 异常率统计；评测报告附元信息；保留原文页码回溯。
4. **版权与合规** —— 仅限个人已购书籍的离线阅读辅助；`cookie.txt` / `storage_state.json` 绝不入库；产物不外传。

---

# 6. 開發計畫

> 📄 **完整開發計畫已獨立成文**：[`开发计划.md`](./开发计划.md)
>
> 包含：最终目录结构 / 阶段划分与验收标准 / P0–P3 逐阶段技术细节 / 里程碑 M0–M6 / 风险登记 / 立即可执行的 3 件事。
>
> 摘要：
>
> | 阶段 | 交付物 | 验收标准 |
> |---|---|---|
> | P0 采集层 | `capture/` + `toc.json` + `screenshots/` | 每页一张单栏 PNG；`pages.jsonl` 带 `chapter_title` + `sha1`；续跑不重复不错位 |
> | P1 OCR + 闸门 | `ocr_pages/` + `_ocr_index.jsonl` + 质量报告 | 覆盖率 ≥ 95%；`col_spanning_lines` 全为 0；低置信页 ≤ 2% |
> | P2 认知层 | `chapters/*.json` + `chapter_summaries.md` | 单章可独立重跑；摘要不超出原文 |
> | P3 全书评测 | `book_review.md` | 五维分 + 评级 + 优劣势 + 阅读建议；附底稿质量元信息 |
>
> M1（`ocr.swift` 优化）已完成 ✅；下一个卡点是 M0 登录态 → M2 采集层。

---


## 附：本次評審的證據來源

| 证据 | 位置 |
|---|---|
| 4 张真实样本像素分析 | `/tmp/wr_ocr_analysis/analyze.py`、`analyze2.py`、`ocr_quality.py` |
| 线上 reader DOM/网络/字体探测 | `/tmp/wr_probe/probe.py` … `probe7.py` |
| 864 页 OCR 全量体检 | `/tmp/wr_ocr_analysis/ocr_quality.py` |
| 分辨率对比 OCR | `/tmp/wr_ocr_test/*.txt`（`swift ocr.swift` 实跑） |
| 新旧 OCR 回歸 diff（30 頁 / 20261 字） | `/tmp/diff_ocr.py` |
| 雙欄交錯量化（舊整視口 8.2% vs canvas 單欄 0%） | `/tmp/ocr_reg/bbox/_ocr_index.jsonl`、`/tmp/ocr_cv/bbox/*.lines.json` |
| 成熟实现 | `/Users/kunweisu/fe-project/weread-screenshot/`（screenshot.mjs 1368 行、ocr.swift、save-cookie.mjs、aggregate_*.py、run.sh、README.md） |
| 本項目優化後的 OCR | `weread-study-agent/ocr.swift`（12 項改動，回歸通過） |
