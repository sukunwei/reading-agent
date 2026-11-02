#!/usr/bin/env python3
"""逐章精读总结（ChapterSummaryAgent）。"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from ..llm import LLM, classify_exception
from ..prompts import CHAPTER_SUMMARY_SYSTEM, CHAPTER_SUMMARY_USER
from ..schema import ChapterSummary


def summarize_chapter(llm: LLM, book_title: str, chapter: dict,
                      model: str | None = None,
                      figures_block: str = "") -> ChapterSummary:
    """对单章产出结构化精读笔记。

    figures_block：本章插图的视觉模型转述（M8）。
    单独成块并标注「非原文」，避免模型把它当原文引用 → 破坏忠实性护栏。
    """
    text = chapter.get("text", "")
    if not text.strip():
        # 空章节也要返回合法对象（core_thesis 在 schema 里是必填）
        return ChapterSummary(core_thesis="")

    user = CHAPTER_SUMMARY_USER.format(
        book_title=book_title,
        chapter_title=chapter["title"],
        chapter_text=text,
    )
    if figures_block:
        user = user + "\n\n" + figures_block
    return llm.structured(
        system=CHAPTER_SUMMARY_SYSTEM,
        user=user,
        schema=ChapterSummary,
        model=model or llm.summary_model,
        max_tokens=8000,
    )


def summarize_chapters(llm: LLM, book_title: str, chapters: list[dict],
                       skip_done: bool = True, model: str | None = None,
                       on_progress=None, workers: int | None = None) -> list[dict]:
    """逐章总结，跳过已完成的（支持单章重跑）。

    默认 **2 个 worker 并行**（LLM_WORKERS 可调）—— 逐章总结彼此独立，
    并行能把 60 章的耗时从 ~40 分钟压到 ~20 分钟。
    """
    done = {c["index"] for c in chapters if c.get("summary_done")}
    if workers is None:
        workers = int(os.environ.get("LLM_WORKERS", "2"))

    todo = [ch for ch in chapters
            if not (skip_done and ch["index"] in done)]
    if not todo:
        return chapters

    lock = threading.Lock()

    def run_one(ch: dict) -> dict:
        try:
            s = summarize_chapter(llm, book_title, ch, model=model)
            ch["summary"] = s.model_dump()
            ch["summary_done"] = True
            ch["summary_error"] = None
        except Exception as e:
            ch["summary_done"] = False
            category, retryable = classify_exception(e)
            ch["summary_error"] = {
                "category": category, "retryable": retryable,
                "message": str(e)[:300], "detail": getattr(e, "detail", "")[:300],
            }
        if on_progress:
            with lock:
                on_progress(ch)
        return ch

    if workers <= 1:
        for ch in todo:
            run_one(ch)
    else:
        # ⚠️ 并行前先预热：探测原生结构化输出是否可用。
        #    否则多个 worker 会同时去试那个会挂起的原生路径（实测卡数分钟）。
        llm.warmup()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_one, todo))
    return chapters


def _summarize_chapters_seq(llm: LLM, book_title: str, chapters: list[dict],
                            skip_done: bool = True, model: str | None = None,
                            on_progress=None) -> list[dict]:
    """旧版串行实现（保留作对照）。"""
    done = {c["index"] for c in chapters if c.get("summary_done")}
    out = []
    for ch in chapters:
        if skip_done and ch["index"] in done:
            out.append(ch)
            continue
        # 正文为空 → 不是「总结完成」，而是「没内容」，
        # 交给诊断节点判断是页区间映射错位还是该章尚未采集
        if not ch.get("text", "").strip():
            ch["summary_done"] = False
            ch["summary_error"] = {
                "category": "empty_text", "retryable": False,
                "message": "章节正文为空", "detail": "",
            }
            out.append(ch)
            if on_progress:
                on_progress(ch)
            continue
        try:
            s = summarize_chapter(llm, book_title, ch, model=model)
            ch["summary"] = s.model_dump()
            ch["summary_done"] = True
            ch["summary_error"] = None
        except Exception as e:
            ch["summary_done"] = False
            category, retryable = classify_exception(e)
            ch["summary_error"] = {
                "category": category, "retryable": retryable,
                "message": str(e)[:300], "detail": getattr(e, "detail", "")[:300],
            }
        out.append(ch)
        if on_progress:
            on_progress(ch)
    return out
