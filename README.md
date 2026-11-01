# 微信读书全书 OCR 总结 Agent

输入一个微信读书 reader URL，输出：**逐页截图 → 分页 OCR 文本 → 逐章精读笔记 → 全书五维评测报告**。

> 全 Python 实现。OCR 用 macOS Vision（`ocr.swift`，编译成独立二进制被 Python 调用）。

## 快速开始

```bash
pip install -r requirements.txt
swiftc -O ocr.swift -o bin/ocr          # 编译 OCR 二进制

# 1) 登录（弹出浏览器扫码；或从运行中的 Chrome 直接提取登录态）
python3 capture/login.py
python3 capture/extract_chrome_cookies.py   # 备选：从 Chrome 提取，无需扫码

# 2) 采集整本（1280×815 整页截图，不含 header）
python3 capture/capture.py
python3 capture/capture.py --resume         # 断点续跑

# 3) OCR + 质量闸门
python3 agent/ocr.py

# 4) 认知层（切分 → 逐章总结 → 人工闸门 → 全书评测 → 渲染）
#    .env 已从 ~/.claude/settings.json 提取好（含凭据，权限 600，已 gitignore）
python3 agent/cli.py <书名>
python3 agent/cli.py <书名> --mock          # 离线跑，验证管道不调 API
```

## 目录结构

```
capture/          采集层（Playwright）
  login.py                有头浏览器扫码登录 → state/storage_state.json
  extract_chrome_cookies.py  从运行中的 Chrome 提取登录态（无需扫码）
  capture.py              逐页截图 → output/<书名>/screenshots/<NNN_章名>/page_NNNN.png
ocr.swift          OCR 引擎（macOS Vision），编译为 bin/ocr
agent/             认知层（LangGraph）
  ocr.py                  分页 OCR + 质量闸门 → output/<书名>/ocr_pages/
  schema.py               pydantic 模型（结构化输出 schema）
  llm.py                  Claude 接入（含离线 MOCK 模式）
  prompts.py              各 Agent 的 Prompt
  nodes/clean.py          OCR 文本清洗（消除视觉换行切碎）
  nodes/split.py          章节切分（确定性：目录 × 每页章节号）
  nodes/summary.py        逐章精读总结
  nodes/review.py         全书五维评测 + 报告渲染
  graph.py                LangGraph 主图（含 HITL 人工闸门）
  cli.py                  命令行入口
state/<书名>/      续跑状态（pages.jsonl / run_config.json / checkpoint sqlite）
```

## 产物

```
output/<书名>/
  screenshots/<NNN_章名>/page_NNNN.png    整页截图 1280×815（不含 header）
  ocr_pages/<NNN_章名>/page_NNNN.txt      一页一 TXT，与 PNG 一一对应
  quality_report.json                    OCR 覆盖率 / 置信度 / 闸门结果
  chapters/<NNN>.json / <NNN>.md          单章总结（可单章重跑）
  chapters.json                          章节 → 页区间映射
  chapter_summaries.md                   逐章精读笔记
  book_review.md                         全书五维评分 + 专业书评
  full_readnote.md                       整合版读书资产
  full_text.txt                          清洗后全文
```

## 关键设计

| 决策 | 原因 |
|---|---|
| OCR 而非 DOM 取文本 | 正文渲染在 `<canvas>` 上，DOM 里没有正文（实测全页可见文本仅 4 字符） |
| 整页截图 1280×815，`device_scale_factor=1` | DSF=2 时微信读书按设备像素排版而画布只留 CSS 尺寸 → 正文被切字 |
| 强制「上下滚动双栏」阅读模式 | 该模式底部有明确的「上一页/下一页」按钮；模式会被持久化，必须显式切换并校验 |
| OCR 前按 canvas 边界临时裁栏 | 整页左右两栏，Vision 按 y 分行会把两栏同行并成一行 → 阅读序错乱（实测 8.2% 的行跨栏） |
| 章节切分用确定性节点而非 LLM | 目录（站点自带）+ 每页章节号（DOM 免费）两个源交叉即可，零编造风险、零 token 成本 |
| 采集层不进 LangGraph | 采集是长时确定性任务，不该进 checkpoint 图；LangGraph 只管认知层 |
| 结构化输出带降级路径 | 部分 Anthropic 兼容端点（实测 MiniMax）**接受** `output_config.format` 但不强制，返回散文 → `native` 失败时自动降级为「提示词要求 JSON + 本地校验 + 修复重试」 |
| 默认模型不写死 | 走第三方端点时 `claude-opus-5-5` 会 404；改为 `LLM_MODEL` → `ANTHROPIC_MODEL` → 官方默认 |

## 合规

仅用于个人已购书籍的离线阅读辅助。

- `state/` 目录含明文登录会话、`.env` 含 API 凭据 → **均已在 `.gitignore` 中，不要提交**
- `sample/` 下的样例截图来自付费书籍的页面，出于版权考虑**不入库**（本地保留）
- 产物不外传
