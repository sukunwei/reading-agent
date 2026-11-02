#!/usr/bin/env python3
"""认知层主图（LangGraph）。

节点链路：
    load → clean → split → summarize → gate → review → render → END

要点：
  · 采集层（截图/OCR）不在图里 —— 那是确定性长任务，不该进 checkpoint 图
  · summarize 逐章独立，失败的章不阻断其他章（走 gate 节点人工介入）
  · gate 用 LangGraph 的 interrupt 做 HITL：低质量章节让人复核后再继续
  · SqliteSaver 做 checkpoint → 单章重跑 / 断点续跑
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from .figures import process_book_figures
from .llm import LLM
from .nodes.diagnose import diagnose_and_recover, render_diagnosis_md
from .nodes.review import render_review_md, review_book
from .nodes.split import load_book_context
from .nodes.summary import summarize_chapters
from .prompts import RENDER_CHAPTER_MD
from .stages import Timer, export_usage_log, record_stage


class BookState(TypedDict, total=False):
    book_dir: str
    state_dir: str
    book_title: str
    chapters: list[dict]
    pages: list[dict]
    quality: dict
    review: dict | None
    needs_review: list[int]      # 待人工复核的章节 index
    gate_passed: bool
    diagnosis: dict | None        # 诊断 / 恢复报告
    figures: list[dict]           # 插图记录（含视觉描述）
    figure_stats: dict
    outputs: dict[str, str]


# --------------------------------------------------------------------------- 节点

def _llm(state: BookState, label: str) -> LLM:
    """按阶段建 LLM 实例：带 label（token 台账按阶段归类）+ 共享台账文件。"""
    export_usage_log(Path(state["state_dir"]))
    return LLM(label=label)


def node_figures(state: BookState) -> BookState:
    """插图分流（M8）：检测图表 → 视觉模型读图 → 记录到 figures.json。

    必须在 load 之前 —— load 装配章节正文时会把插图块内联到对应位置。
    """
    book_dir = Path(state["book_dir"])
    state_dir = Path(state["state_dir"])
    if not (book_dir / "screenshots").exists():
        state["figures"] = []
        return state

    llm = _llm(state, "figure")
    res = process_book_figures(
        llm, book_dir, state_dir, describe=True,
        on_progress=lambda rec, st: print(
            f"  📊 {rec.get('caption_no')!s:8s} {str(rec.get('caption'))[:24]:26s} {rec.get('verdict')}"))
    state["figures"] = res.get("figures", [])
    state["figure_stats"] = res.get("stats", {})
    return state


def node_load(state: BookState) -> BookState:
    book_dir = Path(state["book_dir"])
    state_dir = Path(state["state_dir"])
    chapters, pages, quality = load_book_context(book_dir, state_dir)

    # 复用已有总结（支持单章重跑、重跑只补缺的）
    ch_dir = book_dir / "chapters"
    reused = 0
    for ch in chapters:
        f = ch_dir / f"{ch['index']:03d}.json"
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if d.get("summary"):
            ch["summary"] = d["summary"]
            ch["summary_done"] = True
            reused += 1
    if reused:
        print(f"  ♻️  复用已有总结 {reused}/{len(chapters)} 章（--force-summary 可强制重跑）")
    state["chapters"] = chapters
    state["pages"] = pages
    state["quality"] = quality
    state["book_title"] = book_dir.name
    state["review"] = state.get("review")
    state["needs_review"] = state.get("needs_review", [])
    return state


def node_summarize(state: BookState) -> BookState:
    llm = _llm(state, "summary")
    chapters = state.get("chapters", [])

    def progress(ch: dict):
        s = ch.get("summary")
        if s:
            print(f"  ✅ {ch['title'][:30]:32s} ({ch.get('chars', 0)} 字)")
        else:
            err = ch.get("summary_error")
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            print(f"  ❌ {ch['title'][:30]:32s} {msg[:80]}")

    import os
    force = os.environ.get("READING_AGENT_FORCE_SUMMARY") == "1"
    state["chapters"] = summarize_chapters(llm, state["book_title"], chapters,
                                            skip_done=not force, on_progress=progress)
    return state


def node_diagnose(state: BookState) -> BookState:
    """诊断 / 恢复节点：把能自动修的失败自己修好，修不好的才升级给人工闸门。"""
    llm = _llm(state, "diagnose")
    chapters = state.get("chapters", [])

    def progress(ch: dict):
        d = ch.get("diagnosis") or {}
        if d.get("category") == "not_captured":
            print(f"  ⏭ {ch['title'][:40]:42s} {d.get('action', '')}")
            return
        mark = "✅" if ch.get("summary_done") else "⛔"
        print(f"  {mark} {ch['title'][:28]:30s} {d.get('label', '')} → {d.get('action', '')}")

    chapters, report = diagnose_and_recover(
        llm, state["book_title"], chapters, state.get("pages", []),
        state.get("quality", {}), Path(state["book_dir"]),
        on_progress=progress)
    state["chapters"] = chapters
    state["diagnosis"] = report
    return state


def node_gate(state: BookState) -> BookState:
    """质量闸门 + 人工复核（HITL）。

    有问题时用 LangGraph 的 interrupt() 挂起，等待人工决定；
    恢复时通过 Command(resume={...}) 继续。
    """
    chapters = state.get("chapters", [])
    quality = state.get("quality", {})
    needs = []

    # 1) 整书级闸门
    if quality and not quality.get("gate_passed", True):
        for _k, v in quality.get("gate", {}).items():
            if not v:
                needs.append(-1)          # -1 表示整书级别问题
    # 2) 章节级：
    #    · 有正文但总结失败 → 需要人工
    #    · 有正文但正文异常短（<200 字）→ 需要人工
    #    · 正文为空且该章尚未采集 → 是采集覆盖问题，不进人工闸门（诊断报告里单独列）
    for ch in chapters:
        chars = ch.get("chars", 0)
        if chars <= 0:
            continue
        if not ch.get("summary_done") or chars < 200:
            needs.append(ch["index"])

    state["needs_review"] = sorted(set(needs))
    state["gate_passed"] = not state["needs_review"]

    if state["needs_review"]:
        from langgraph.types import interrupt
        decision = interrupt({
            "reason": "质量闸门未通过，需要人工确认",
            "needs_review": state["needs_review"],
            "gate": quality.get("gate", {}),
            "failed_chapters": [
                {"index": c["index"], "title": c["title"],
                 "diagnosis": c.get("diagnosis", {}),
                 "error": str(c.get("summary_error", ""))[:200]}
                for c in chapters
                if not c.get("summary_done") and c.get("chars", 0) > 0
            ],
            "not_captured_chapters": [
                {"index": c["index"], "title": c["title"],
                 "start_page": c.get("start_page")}
                for c in chapters if c.get("chars", 0) <= 0
            ],
            "options": ["continue（跳过问题章节继续）", "stop（中止，人工修复后重跑）"],
        })
        if isinstance(decision, dict) and decision.get("action") == "stop":
            state["gate_passed"] = False
    return state


def node_review(state: BookState) -> BookState:
    llm = _llm(state, "review")
    review = review_book(llm, state["book_title"], state.get("chapters", []),
                         state.get("quality", {}))
    state["review"] = review.model_dump()
    return state


def node_render(state: BookState) -> BookState:
    """渲染 Markdown 产物。"""
    book_dir = Path(state["book_dir"])
    chapters = state.get("chapters", [])
    outputs: dict[str, str] = {}

    # 逐章 md + json
    ch_dir = book_dir / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    md_all = ["# 全书逐章精读笔记\n"]
    for ch in chapters:
        s = ch.get("summary")
        if not s:
            continue
        (ch_dir / f"{ch['index']:03d}.json").write_text(
            json.dumps({"index": ch["index"], "title": ch["title"],
                        "start_page": ch["start_page"], "end_page": ch["end_page"],
                        "summary": s}, ensure_ascii=False, indent=2), "utf-8")
        md = RENDER_CHAPTER_MD.format(
            title=ch["title"], index=ch["index"], level=ch["level"],
            start_page=ch["start_page"], end_page=ch["end_page"], chars=ch.get("chars", 0),
            page_count=ch["end_page"] - ch["start_page"] + 1,
            thesis=s.get("core_thesis", ""),
            key_points="\n".join(f"- {x}" for x in s.get("key_points", [])),
            concepts="\n".join(f"- {x}" for x in s.get("concepts", [])),
            cases="\n".join(f"- {x}" for x in s.get("cases", [])),
            data_points="\n".join(f"- {x}" for x in s.get("data_points", [])),
            reflections=s.get("reflections", ""),
            open_questions="\n".join(f"- {x}" for x in s.get("open_questions", [])),
        )
        (ch_dir / f"{ch['index']:03d}.md").write_text(md, "utf-8")
        md_all.append(md)
    (book_dir / "chapter_summaries.md").write_text("\n\n---\n\n".join(md_all), "utf-8")
    outputs["chapter_summaries.md"] = str(book_dir / "chapter_summaries.md")

    # 诊断报告
    if state.get("diagnosis"):
        (book_dir / "diagnosis.json").write_text(
            json.dumps(state["diagnosis"], ensure_ascii=False, indent=2), "utf-8")
        (book_dir / "diagnosis.md").write_text(
            render_diagnosis_md(state["diagnosis"], chapters), "utf-8")
        outputs["diagnosis.md"] = str(book_dir / "diagnosis.md")

    # 全文（清洗后）
    full = "\n\n".join(f"## {c['title']}\n\n{c.get('text','')}" for c in chapters)
    (book_dir / "full_text.txt").write_text(full, "utf-8")
    outputs["full_text.txt"] = str(book_dir / "full_text.txt")

    # 全书评测
    rv = state.get("review")
    if rv:
        from .schema import BookReview
        obj = BookReview.model_validate(rv)
        md = render_review_md(obj, state["book_title"], state.get("quality", {}))
        (book_dir / "book_review.md").write_text(md, "utf-8")
        (book_dir / "book_review.json").write_text(
            json.dumps(rv, ensure_ascii=False, indent=2), "utf-8")
        outputs["book_review.md"] = str(book_dir / "book_review.md")

        # 整合版
        (book_dir / "full_readnote.md").write_text(
            (book_dir / "chapter_summaries.md").read_text("utf-8")
            + "\n\n---\n\n" + md, "utf-8")
        outputs["full_readnote.md"] = str(book_dir / "full_readnote.md")

    # HTML 报告（失败不影响主流程）
    try:
        from .report import generate
        rp = generate(book_dir, Path(state["state_dir"]), out_dir_root=Path(state["book_dir"]).parent)
        outputs["report.html"] = str(rp)
    except Exception as e:
        print(f"  ⚠️ 报告生成失败（不影响主流程）：{e}")

    state["outputs"] = outputs
    return state


# --------------------------------------------------------------------------- 图

def _timed(name: str, fn):
    """给节点套一层计时，把耗时写进 .state/stages.json。"""
    def wrapped(state: BookState) -> BookState:
        with Timer() as t:
            out = fn(state)
        state_dir = out.get("state_dir") or state.get("state_dir")
        if state_dir:
            record_stage(Path(state_dir), name, t.seconds)
        return out
    wrapped.__name__ = fn.__name__
    return wrapped


def build_graph(checkpointer=None):
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(BookState)
    g.add_node("figures", _timed("figures", node_figures))
    g.add_node("load", _timed("load", node_load))
    g.add_node("summarize", _timed("summarize", node_summarize))
    g.add_node("diagnose", _timed("diagnose", node_diagnose))
    g.add_node("gate", _timed("gate", node_gate))
    g.add_node("review", _timed("review", node_review))
    g.add_node("render", _timed("render", node_render))

    g.add_edge(START, "figures")
    g.add_edge("figures", "load")
    g.add_edge("load", "summarize")
    g.add_edge("summarize", "diagnose")
    g.add_edge("diagnose", "gate")
    g.add_edge("gate", "review")
    g.add_edge("review", "render")
    g.add_edge("render", END)

    return g.compile(checkpointer=checkpointer)


def make_checkpointer(db_path: Path):
    """SQLite checkpoint —— 支持断点续跑与单章重跑。

    注意：``SqliteSaver.from_conn_string()`` 返回的是**上下文管理器**，
    不能直接当 checkpointer 用（LangGraph 会报 Invalid checkpointer）。
    这里直接持有连接，连接对象需在整个图运行期间存活。
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver._conn = conn          # 保持引用，避免被 GC 关闭
    return saver
