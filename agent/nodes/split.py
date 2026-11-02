#!/usr/bin/env python3
"""章节切分（确定性节点，替换设计文档里的 LLM ChapterSplitAgent）。

数据源：
  · toc.json                —— 站点自带的权威目录（标题 + 层级），零编造风险
  · state/<书名>/pages.jsonl —— 每页的 chapter_index（采集时从 DOM 免费拿到）

两者一交叉即得到精确的「章节 → 页区间」映射，不需要任何 LLM 调用。

输出：chapters.json + 每章的正文（按页码顺序拼接）。
"""

from __future__ import annotations

import contextlib
import json
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


def build_chapters(toc: list[dict], pages: list[dict]) -> list[dict]:
    """把目录项映射到页区间。

    toc 的 index 是目录项序号；pages 里的 chapter_index 也是目录项序号，
    两者同源（都来自 `.readerCatalog_list_item` / 章节标题），可直接对齐。
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

    for ch in chapters:
        texts = [by_page[n] for n in range(ch["start_page"], ch["end_page"] + 1)
                 if n in by_page]
        ch["text"] = strip_noise(clean_pages(texts))
        ch["chars"] = len(ch["text"])
        ch["pages_read"] = len(texts)
    return chapters


def load_book_context(book_dir: Path, state_dir: Path) -> tuple[list[dict], list[dict], dict]:
    """读 toc.json + pages.jsonl + 插图记录 + 质量报告。"""
    toc_path = book_dir / "toc.json"
    toc = json.loads(toc_path.read_text("utf-8")) if toc_path.exists() else []
    pages = read_jsonl(state_dir / "pages.jsonl")
    chapters = build_chapters(toc, pages)

    fp = book_dir / "figures" / "figures.json"
    figures = json.loads(fp.read_text("utf-8")) if fp.exists() else []
    chapters = chapter_texts(book_dir, chapters, pages, figures)

    qp = book_dir / "quality_report.json"
    quality = json.loads(qp.read_text("utf-8")) if qp.exists() else {}
    return chapters, pages, quality
