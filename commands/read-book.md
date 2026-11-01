---
description: 采集并精读一本微信读书上的书 —— 逐页截图 → 分页 OCR → 插图识别 → 逐章精读笔记 + 全书五维评测
argument-hint: [微信读书书籍地址（可留空，会主动询问）]
allowed-tools:
  [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "AskUserQuestion",
    "Skill",
    "Task",
  ]
---

# 采集并精读一本书

把微信读书网页版的一整本书变成可精读、可评测的读书资产。

**插件根目录**：`${CLAUDE_PLUGIN_ROOT}`
**数据目录**：`${READING_AGENT_HOME:-${CLAUDE_PLUGIN_ROOT}}`（产物落在这里，不是插件目录）

初始请求：$ARGUMENTS

---

## Phase 1：拿到书籍地址

1. 如果 `$ARGUMENTS` 里已经是一个 `https://weread.qq.com/web/reader/...` 地址 → 直接用。
2. 否则用 **AskUserQuestion** 或直接提问，让用户粘贴微信读书的**书籍地址**（reader URL，不是书名页 URL）。

   提示用户：在微信读书网页版打开一本书，地址栏就是
   `https://weread.qq.com/web/reader/<一串字符>`。

3. 校验地址格式。不符合 → 重新问一次。

## Phase 2：确认登录态（未登录则让用户扫码）

```bash
export READING_AGENT_HOME="${READING_AGENT_HOME:-${CLAUDE_PLUGIN_ROOT}}"
ls "$READING_AGENT_HOME/state/storage_state.json" 2>/dev/null
```

**若登录态不存在或已失效**：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/capture/login.py"
```

这个脚本会：
- 启动一个**有头浏览器**（用户能看见、能扫码）
- 打开微信读书书籍页，等待用户点右上角「登录」并用微信/QQ 扫码
- 检测到登录成功后导出 `storage_state.json` 并打印「✅ 登录态已保存」

**脚本运行期间要告诉用户**：浏览器窗口已经弹出，请在里面扫码登录；登录成功后脚本会自动继续。
如果脚本超时或没检测到登录，**停下来问用户**，不要继续往下跑（未登录只能拿到试读部分，会静默产出半本书）。

**若用户不方便扫码**（例如在远程环境），可以改用从本机 Chrome 提取登录态：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/capture/extract_chrome_cookies.py"
```

## Phase 3：一次性准备依赖

```bash
pip install -r "${CLAUDE_PLUGIN_ROOT}/requirements.txt"     # 首次
swiftc -O "${CLAUDE_PLUGIN_ROOT}/ocr.swift" -o "${CLAUDE_PLUGIN_ROOT}/bin/ocr"   # 首次
```

OCR 二进制不存在时 `agent/ocr.py` 会自动编译，可跳过这步。

**LLM 凭据**：检查 `${READING_AGENT_HOME}/.env` 里有没有 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`。
没有 → 告诉用户可以在 `.env` 里填，或者用 `MOCK=1` 先跑通流程（用占位内容，不调 API）。

## Phase 4：采集整本

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/capture/capture.py" --book-url "<BOOK_URL>"
```

- 视口 1280×815、整页截图、不含顶部 header
- 断点续跑：中断后重跑同一条命令即可（已采页按 sha1 跳过）
- **这一步耗时较长**（约 2~4 秒/页）。运行期间定期查看进度并向用户汇报（每 50 页汇报一次）。

采完后先做一次质量体检：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/agent/ocr.py"
```

看输出里的 `覆盖率 / 平均置信度 / 闸门`：
- `闸门 ✅` → 继续
- `闸门 ⚠️` → 把 `quality_report.json` 的问题讲给用户，问是先补采还是先继续

## Phase 5：插图识别（视觉模型读图）

```bash
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}')
from pathlib import Path
from agent.llm import LLM, load_dotenv
from agent.figures import process_book_figures
import glob, os
load_dotenv(os.environ.get('READING_AGENT_HOME','${CLAUDE_PLUGIN_ROOT}') + '/.env')
book = Path(glob.glob(os.environ.get('READING_AGENT_HOME','${CLAUDE_PLUGIN_ROOT}') + '/output/*')[0])
state = Path(glob.glob(os.environ.get('READING_AGENT_HOME','${CLAUDE_PLUGIN_ROOT}') + '/state/*')[0])
r = process_book_figures(LLM(), book, state, describe=True)
print(r['stats'])
"
```

## Phase 6：认知层（切分 → 逐章总结 → 诊断恢复 → 人工闸门 → 全书评测 → 渲染）

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/agent/cli.py" "<书名关键字>"
```

这条命令走 LangGraph 主图，会：
- 逐章生成精读笔记
- 失败时自动诊断并恢复，仍失败才中断等人工确认
- 汇总全书五维评分与专业书评

## Phase 7：交付

把产物路径告诉用户，并**简要汇报**：

| 产物 | 路径 |
|---|---|
| 逐章精读笔记 | `output/<书名>/chapter_summaries.md` |
| 全书五维评测 | `output/<书名>/book_review.md` |
| 整合版读书资产 | `output/<书名>/full_readnote.md` |
| 诊断报告 | `output/<书名>/diagnosis.md` |
| 清洗后全文 | `output/<书名>/full_text.txt` |
| 逐页截图 | `output/<书名>/screenshots/` |
| 分页 OCR | `output/<书名>/ocr_pages/` |

汇报要点：
- 覆盖率、平均字数/页、平均置信度
- 五维分数与评级
- 哪几章被标记为「未采集」或「需人工复核」

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 采到一半停了 | 试读/会员限制；或登录态过期 → 重新登录后续跑 |
| 提示「连续内容不变」 | 书尾正常结束；或翻页失效 → 看 `state/<书名>/run_config.json` 的视口指纹是否被改过 |
| OCR 文本里混进「上一页/下一页」 | OCR 阶段没裁 pager 行 → 重跑 `agent/ocr.py --force` |
| 章节是空的 | 该章未采集到 → 用 `capture/capture.py --resume` 补采 |
| 结构化输出报错 | 第三方端点不强制 JSON → 已自动降级；仍失败则换 `LLM_STRUCTURED_OUTPUT=prompt` |
| 图表没被识别 | 两路信号不一致 → 看 `output/<书名>/figures/figures.json` 的 `verdict` 字段 |
