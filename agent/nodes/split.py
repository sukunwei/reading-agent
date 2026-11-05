#!/usr/bin/env python3
"""章节切分（确定性节点，替换设计文档里的 LLM ChapterSplitAgent）。

数据源：
  · toc.json                —— 站点自带的权威目录（标题 + 层级），零编造风险
  · state/<书名>/pages.jsonl —— 每页的 chapter_index（采集时从 DOM 免费拿到）
  · ocr_pages/*/page_*.txt  —— 逐页 OCR 文本（用于定位小节标题行）

三者一交叉即得到精确的「章节 → 页区间」映射，不需要任何 LLM 调用。

⚠️ chapter_index 只到**一级/二级**章 —— 采集时读的是页面 header 的章标题
   （`.renderTargetPageInfo_header_chapterTitle`），微信读书只在那里渲染篇/章名，
   三级小节（1.1 / 13.2 …）永远不会成为 header。
   → 三级项在 pages.jsonl 里没有对应页 → 早期实现靠「起始页严格递增 +1」补位，
   **等于凭空造边界**（实测：79 个目录项里 56 个边界是伪造的，正文整段错位）。
   → 现在改为在 OCR 文本里定位小节标题行，确定性地拿回真实起始页。

输出：chapters.json + 每章的正文（按页码顺序拼接）。
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
from pathlib import Path

from ..figures import inject_into_page
from .clean import clean_pages, strip_noise


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(Exception):
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out


# --------------------------------------------------------------------------- 小节标题定位

# 三级小节标题形如「13.2 动物识别AI Agent」
NUM_HEAD = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*(.*)$")


def _norm_heading(s: str) -> str:
    """归一化标题文本，供模糊比较。

    Vision OCR 有两个稳定噪声：把 `AI` 读成 `Al`（小写 L），以及在词中间插空格
    （「AppBuilder平 台」）。
    """
    return re.sub(r"\s+", "", s).replace("Al", "AI")


def _heading_similarity(a: str, b: str) -> float:
    """标题相似度（0~1）。"""
    a, b = _norm_heading(a), _norm_heading(b)
    if not a:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _match_heading_line(title: str, lines: list[str],
                        min_ratio: float = 0.6) -> int | None:
    """在一页的行里找出小节标题行，返回行号（找不到 None）。

    · 标题行以「编号 + 标题」开头，编号后不能再跟 `.` 或数字
      （排除 `13.2.1 目标功能` 这类下级标题冒充 `13.2`）
    · 编号 + 标题都要吻合，避免正文里引用「13.2」被误判
    """
    m = NUM_HEAD.match(title)
    if not m:
        return None
    num, rest = m.group(1), m.group(2)
    if not rest.strip():
        return None
    pat = re.compile(r"^\s*" + re.escape(num) + r"(?![.\d])")
    for i, line in enumerate(lines):
        if not pat.match(line):
            continue
        if _heading_similarity(rest, line) >= min_ratio:
            return i
    return None


def find_section_page(title: str, ocr_texts: dict[int, str],
                      min_page: int = 1,
                      min_ratio: float = 0.6) -> int | None:
    """在 OCR 文本里定位小节标题行所在的页（取最早吻合的页）。"""
    for no in sorted(ocr_texts):
        if no < min_page:
            continue
        lines = ocr_texts[no].splitlines()
        if _match_heading_line(title, lines, min_ratio) is not None:
            return no
    return None


def build_chapters(toc: list[dict], pages: list[dict],
                   ocr_texts: dict[int, str] | None = None) -> list[dict]:
    """把目录项映射到页区间。

    toc 的 index 是目录项序号；pages 里的 chapter_index 也是目录项序号，
    两者同源（都来自 `.readerCatalog_list_item` / 章节标题），可直接对齐。

    ⚠️ chapter_index 只覆盖**一级/二级**章（微信读书只在页 header 渲染篇/章名）。
       三级小节在 pages.jsonl 里没有对应页 → 不能靠「+1 递增」补位（那是凭空造
       边界，实测 79 项里 56 项错位）→ 改为在 OCR 文本里定位小节标题行。
    """
    if not toc:
        return []

    # 每个目录项 → 它起始的页码（第一张落入该章节的页）
    # chapter_index == -1 的页（DOM 没给到章节名）→ 继承前一页的章节
    start_of: dict[int, int] = {}
    last_ci = -1
    for p in sorted(pages, key=lambda x: x.get("page_no", 0)):
        ci = p.get("chapter_index", -1)
        if ci is None or ci < 0:
            ci = last_ci          # 沿用上一张已知章节
        else:
            last_ci = ci
        if ci < 0:
            continue
        if ci not in start_of:
            start_of[ci] = p.get("page_no", 0)

    # ── 没有真实起始页的目录项 → 在 OCR 文本里定位小节标题 ──
    # 找不到标题的（扉页、文后这类没有编号的条目）→ 继承上一项的起点。
    ocr_texts = ocr_texts or {}
    located = 0
    for item in toc:
        if item["index"] in start_of:
            continue
        no = find_section_page(item["title"], ocr_texts)
        if no is not None:
            start_of[item["index"]] = no
            located += 1
    if located:
        print(f"  📑 小节标题定位：{located} 个目录项从 OCR 找回真实起始页")

    # 目录里没有起始页的项（可能是空章节）→ 继承上一项的起点
    last_start = min(start_of.values()) if start_of else 1
    for item in toc:
        if item["index"] not in start_of:
            start_of[item["index"]] = last_start
        else:
            last_start = start_of[item["index"]]

    # 结束页 = 下一项的起始页 - 1
    chapters = []
    for i, item in enumerate(toc):
        start = start_of.get(item["index"], 1)
        nxt = start_of.get(toc[i + 1]["index"]) if i + 1 < len(toc) else None
        end = (nxt - 1) if nxt else max((p.get("page_no", 0) for p in pages), default=start)
        chapters.append({
            "index": item["index"],
            "level": item.get("level", 1),
            "title": item["title"],
            "start_page": start,
            "end_page": max(start, end),
        })

    # 校正：起始页必须严格递增
    for i in range(1, len(chapters)):
        if chapters[i]["start_page"] <= chapters[i - 1]["start_page"]:
            chapters[i]["start_page"] = chapters[i - 1]["start_page"] + 1
    for i in range(len(chapters) - 1):
        chapters[i]["end_page"] = max(chapters[i]["start_page"],
                                      chapters[i + 1]["start_page"] - 1)
    return chapters


def read_ocr_texts(book_dir: Path, pages: list[dict]) -> dict[int, str]:
    """逐页读原始 OCR 文本（页码 → 文本）。未注入插图块的纯净版本。"""
    ocr_root = book_dir / "ocr_pages"
    out: dict[int, str] = {}
    for p in pages:
        no = p.get("page_no", 0)
        rel = Path(p["img"]).relative_to("screenshots").with_suffix(".txt")
        f = ocr_root / rel
        if f.exists():
            out[no] = f.read_text("utf-8")
    return out


def chapter_texts(book_dir: Path, chapters: list[dict], pages: list[dict],
                  figures: list[dict] | None = None) -> list[dict]:
    """给每个章节填入正文（按页码顺序拼接 OCR 文本）。

    插图分流（M8）：OCR 文本里坐标轴刻度是噪声、图表数据又拿不到 →
    这里按 figures.json 把插图块**内联到正文对应位置**（替换图注行），
    同时剔除落在图表 bbox 内的噪声行。
    """
    ocr_root = book_dir / "ocr_pages"
    figs_by_page: dict[int, list[dict]] = {}
    for f in figures or []:
        figs_by_page.setdefault(f.get("page_no", -1), []).append(f)

    by_page: dict[int, str] = {}
    for p in pages:
        no = p.get("page_no", 0)
        rel = Path(p["img"]).relative_to("screenshots").with_suffix(".txt")
        f = ocr_root / rel
        if not f.exists():
            continue
        text = f.read_text("utf-8")
        page_figs = figs_by_page.get(no)
        if page_figs:
            lj = ocr_root / rel.with_suffix(".lines.json")
            lines_json = []
            if lj.exists():
                try:
                    lines_json = json.loads(lj.read_text("utf-8"))
                except Exception:
                    lines_json = []
            # ⚠️ 不写回 OCR 文件 —— OCR 原文要保持纯净：
            #    写回会把图注行替换掉，下次重跑时「图注模式」信号就消失了。
            #    注入后的正文落在 chapter_summaries.md / full_text.txt 里。
            text, _ = inject_into_page(text, lines_json, page_figs)
        by_page[no] = text

    _assemble_chapter_texts(chapters, by_page)
    return chapters


def _assemble_chapter_texts(chapters: list[dict],
                            by_page: dict[int, str]) -> None:
    """按**行级**边界拼章节正文（原地写回 ch["text"] / ch["chars"]）。

    为什么不能只按页切：小节标题常常落在**页的中间**。
    例如 p373 的上半页还是 13.1 植物识别的收尾，下半页才是「13.2 动物识别」。

      · 只按页切 → 新章节凭空多出上一节的尾巴，上一节又少了这半页
      · 行级切 → 上一节拿到该页标题**之前**的行，新节拿到标题**之后**的行，
        两边都不丢内容

    边界行号在**注入插图块之后**的页文本里重新定位（标题行不会被插图块替换，
    也不会落在图表 bbox 内被剔除）。
    """
    if not chapters:
        return
    max_page = max(by_page) if by_page else 0
    # 每章在起始页里的标题行号
    start_lines: list[int | None] = []
    for ch in chapters:
        head_lines = by_page.get(ch["start_page"], "").splitlines()
        start_lines.append(_match_heading_line(ch["title"], head_lines))

    for i, ch in enumerate(chapters):
        s_page = ch["start_page"]
        s_line = start_lines[i] or 0
        if i + 1 < len(chapters):
            e_page = chapters[i + 1]["start_page"]
            e_line = start_lines[i + 1]
        else:
            e_page, e_line = None, None

        segs: list[str] = []
        n_pages = 0
        if e_page is None:
            last = max_page
        elif e_line is not None:
            last = e_page
        else:
            # 下一章没有标题行（起点来自 chapter_index，如一级/二级章）→
            # 边界页**整页**属于下一章，本章不能碰
            last = e_page - 1
        for no in range(s_page, last + 1):
            raw = by_page.get(no)
            if raw is None:
                continue
            page_lines = raw.splitlines()
            lo = s_line if no == s_page else 0
            hi = e_line if (no == e_page and e_line is not None) else len(page_lines)
            if hi > lo:
                segs.append("\n".join(page_lines[lo:hi]))
                n_pages += 1
        ch["text"] = strip_noise(clean_pages(segs))
        ch["chars"] = len(ch["text"])
        ch["pages_read"] = n_pages


def load_book_context(book_dir: Path, state_dir: Path) -> tuple[list[dict], list[dict], dict]:
    """读 toc.json + pages.jsonl + 插图记录 + 质量报告。"""
    toc_path = book_dir / "toc.json"
    toc = json.loads(toc_path.read_text("utf-8")) if toc_path.exists() else []
    pages = read_jsonl(state_dir / "pages.jsonl")
    chapters = build_chapters(toc, pages, read_ocr_texts(book_dir, pages))

    fp = book_dir / "figures" / "figures.json"
    figures = json.loads(fp.read_text("utf-8")) if fp.exists() else []
    chapters = chapter_texts(book_dir, chapters, pages, figures)

    qp = book_dir / "quality_report.json"
    quality = json.loads(qp.read_text("utf-8")) if qp.exists() else {}
    return chapters, pages, quality
