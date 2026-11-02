#!/usr/bin/env python3
"""
端到端 CLI。

    python3 agent/cli.py --list                # 列出已采集的书
    python3 agent/cli.py <书名>                 # 跑完整认知层（LangGraph 主图：切分→总结→闸门→评测→渲染）
    python3 agent/cli.py <书名> --mock          # 离线跑（不调 API），验证流水线
    python3 agent/cli.py <书名> --stage ocr     # 只跑某一步（ocr / split / summary / review / render / all）
    python3 agent/cli.py <书名> --resume        # 断点续跑（SQLite checkpoint）
    python3 agent/cli.py <书名> --force-summary # 忽略已有总结，全部重跑

完整链路：
    capture/capture.py  →  output/<书名>/screenshots/…   （整页 1280×815 PNG）
    agent/ocr.py        →  output/<书名>/ocr_pages/…     （一页一 TXT）
    agent/cli.py        →  chapters/ + chapter_summaries.md + book_review.md + full_readnote.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 数据目录（输出/状态）：装成插件时不该写进插件目录 → READING_AGENT_HOME 可覆盖
sys.path.insert(0, str(ROOT))
from shared.paths import book_paths, list_books, resolve_out_dir

sys.path.insert(0, str(ROOT))

from agent.llm import LLM, load_dotenv
from agent.nodes.review import review_book
from agent.nodes.split import load_book_context
from agent.nodes.summary import summarize_chapters

load_dotenv(ROOT / ".env")

STAGES = ("ocr", "split", "summary", "review", "render", "report", "all")


def pick_book(keyword: str) -> Path | None:
    for b in list_books(resolve_out_dir()):
        if keyword in b.name:
            return b
    return None


# --------------------------------------------------------------------------- 分阶段

def stage_ocr(book_dir: Path, state_dir: Path, force: bool) -> dict:
    from agent.ocr import build_ocr_binary, process_book
    binary = build_ocr_binary()
    return process_book(book_dir, state_dir, binary, force=force)


def stage_split(book_dir: Path, state_dir: Path) -> list[dict]:
    chapters, _pages, _quality = load_book_context(book_dir, state_dir)
    (book_dir / "chapters.json").write_text(
        json.dumps([{k: v for k, v in c.items() if k != "text"} for c in chapters],
                   ensure_ascii=False, indent=2), "utf-8")
    return chapters


def stage_summary(llm: LLM, book_dir: Path, chapters: list[dict], force: bool) -> list[dict]:
    def progress(ch: dict):
        ok = ch.get("summary_done")
        e = ch.get("summary_error")
        msg = e.get("message", "") if isinstance(e, dict) else str(e or "")
        err = "" if ok else f"  [{e.get('category', '?') if isinstance(e, dict) else '?'}] {msg[:60]}"
        print(f"  {'✅' if ok else '❌'} {ch['title'][:30]:32s} ({ch.get('chars', 0)} 字){err}")

    return summarize_chapters(llm, book_dir.name, chapters,
                              skip_done=not force, on_progress=progress)


def stage_review(llm: LLM, book_dir: Path, chapters: list[dict], quality: dict):
    try:
        review = review_book(llm, book_dir.name, chapters, quality)
    except Exception as e:
        print(f"  ❌ 评测失败：{e}")
        return None
    print(f"  综合 {review.avg_score:.2f} 分 → {review.level}")
    return review


def stage_render(book_dir: Path, chapters: list[dict], quality: dict, review) -> dict:
    from agent.graph import node_render
    st = {"book_dir": str(book_dir), "book_title": book_dir.name,
          "chapters": chapters, "quality": quality,
          "review": review.model_dump() if review else None}
    st = node_render(st)
    return st.get("outputs", {})


# --------------------------------------------------------------------------- 主图

def run_graph(book_dir: Path, state_dir: Path, llm: LLM, resume: bool) -> int:
    """走 LangGraph 主图（含 HITL 人工闸门）。"""
    from langgraph.graph import StateGraph
    from langgraph.types import Command

    from agent.graph import (
        BookState,
        make_checkpointer,
        node_diagnose,
        node_figures,
        node_gate,
        node_load,
        node_render,
        node_review,
        node_summarize,
    )

    db = book_dir / ".state" / f"{book_dir.name}.ckpt.sqlite"
    saver = make_checkpointer(db)

    g = StateGraph(BookState)
    g.add_node("figures", node_figures)
    g.add_node("load", node_load)
    g.add_node("summarize", node_summarize)
    g.add_node("diagnose", node_diagnose)
    g.add_node("gate", node_gate)
    g.add_node("review", node_review)
    g.add_node("render", node_render)
    g.add_edge("__start__", "figures")
    g.add_edge("figures", "load")
    g.add_edge("load", "summarize")
    g.add_edge("summarize", "diagnose")
    g.add_edge("diagnose", "gate")
    g.add_edge("gate", "review")
    g.add_edge("review", "render")
    g.add_edge("render", "__end__")
    app = g.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": book_dir.name}}
    init = {"book_dir": str(book_dir), "state_dir": str(state_dir)}

    if resume:
        try:
            snap = app.get_state(cfg)
            if snap and snap.next:
                print("▶ 检测到未完成的 checkpoint，继续…")
        except Exception:
            pass

    while True:
        # ⚠️ 本版 LangGraph 的 interrupt() 不会抛异常，而是把 __interrupt__ 放进返回值
        result = app.invoke(init, cfg)
        interrupts = (result or {}).get("__interrupt__")
        if not interrupts:
            break

        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        print("\n⏸  人工闸门触发：")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:2000])
        if os.environ.get("MOCK"):
            print("   [MOCK 模式] 自动继续")
            decision = {"action": "continue"}
        elif not sys.stdin.isatty():
            print("   [非交互环境] 自动继续")
            decision = {"action": "continue"}
        else:
            ans = input("   输入 c 继续 / s 停止: ").strip().lower()
            decision = {"action": "continue" if ans.startswith("c") else "stop"}
        result = app.invoke(Command(resume=decision), cfg)
        interrupts = (result or {}).get("__interrupt__")
        if not interrupts:
            break

    outputs = (result or {}).get("outputs", {})
    print("\n✅ 产物：")
    for k, v in outputs.items():
        p = Path(v)
        try:
            shown = p.relative_to(ROOT)
        except ValueError:
            # 产物目录在插件之外（--out-dir 指定）→ 直接显示绝对路径，
            # 否则 relative_to 会抛 ValueError 让整个 run 在收尾时崩掉
            shown = p
        print(f"   · {k}  →  {shown}")
    return 0


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("book", nargs="?", help="书名关键字")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stage", default="all", choices=STAGES)
    ap.add_argument("--mock", action="store_true", help="离线模式，不调 API")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--force-summary", action="store_true", help="重跑全部逐章总结")
    ap.add_argument("--force-ocr", action="store_true", help="重跑全部 OCR")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out-dir", default=None,
                    help="产物根目录（默认 $PWD/reading-output，可用 READING_AGENT_OUT 覆盖）")
    args = ap.parse_args()

    if args.list or not args.book:
        out_dir = resolve_out_dir(args.out_dir)
        books = list_books(out_dir)
        if not books:
            print(f"{out_dir} 下还没有采集好的书。")
            print("先跑：python3 capture/capture.py --book-url <URL> --out-dir", out_dir)
            return 1
        print("已采集的书：")
        for b in books:
            pf = b / ".state" / "pages.jsonl"
            n = sum(1 for _ in pf.open(encoding="utf-8")) if pf.exists() else 0
            print(f"  · {b.name}  ({n} 页)")
        return 0

    book_dir = pick_book(args.book)
    if not book_dir:
        print(f"❌ 找不到包含「{args.book}」的书。用 --list 看看有哪些。")
        return 1
    _, state_dir = book_paths(resolve_out_dir(args.out_dir), book_dir.name)

    if args.mock:
        os.environ["MOCK"] = "1"
    if args.force_summary:
        os.environ["READING_AGENT_FORCE_SUMMARY"] = "1"

    # ---- P1 OCR ----
    if args.stage in ("ocr", "all"):
        print("\n=== [1/4] OCR ===")
        rep = stage_ocr(book_dir, state_dir, force=args.force_ocr)
        if rep:
            print(f"  覆盖率 {rep['coverage']:.1%}  闸门 "
                  f"{'✅' if rep['gate_passed'] else '⚠️ ' + str(rep['gate'])}")
    if args.stage == "ocr":
        return 0

    # ---- 完整链路走 LangGraph ----
    if args.stage == "all":
        llm = LLM(model=args.model)
        return run_graph(book_dir, state_dir, llm, resume=args.resume)

    # ---- 分阶段 ----
    print("\n=== 清洗与章节切分 ===")
    chapters = stage_split(book_dir, state_dir)
    quality = {}
    qp = book_dir / "quality_report.json"
    if qp.exists():
        quality = json.loads(qp.read_text("utf-8"))
    print(f"  {len(chapters)} 个章节 / {sum(c.get('chars', 0) for c in chapters)} 字")
    if args.stage == "split":
        return 0

    llm = LLM(model=args.model)

    print("\n=== 逐章精读总结 ===")
    chapters = stage_summary(llm, book_dir, chapters, args.force_summary)
    if args.stage == "summary":
        return 0

    print("\n=== 全书五维评测 ===")
    review = stage_review(llm, book_dir, chapters, quality) if args.stage != "render" else None
    if args.stage == "review":
        return 0

    outputs = stage_render(book_dir, chapters, quality, review)
    print("\n✅ 产物：")
    for k, v in outputs.items():
        print(f"   · {k}  →  {Path(v)}")

    if args.stage in ("render", "all"):
        from agent.report import main as report_main
        print("\n=== HTML 报告 ===")
        sys.argv = ["report", "--book", book_dir.name, "--out-dir", str(resolve_out_dir(args.out_dir))]
        report_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
