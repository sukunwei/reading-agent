#!/usr/bin/env python3
"""诊断 / 恢复节点。

放在 `summarize` 之后、`gate`（人工闸门）之前：
把**能自动修的失败自己修好**，只有修不好的才升级给人工。

失败分两类，处理方式完全不同：

  A. **输入侧问题**（不是模型的错）
     · 章节正文为空        → 章节→页区间映射错位 → 按章节名重新装配正文
     · OCR 覆盖不足/置信低 → 标记该章需重采或重 OCR（自动做不了，升级）

  B. **调用侧问题**
     · json_invalid      模型输出不是合法 JSON      → 带错误信息重试
     · schema_invalid    JSON 不符合 schema          → 带校验错误重试（提示词更严）
     · truncated         触到 max_tokens 被截断      → 提高 max_tokens 重试
     · rate_limit        429                        → 退避重试
     · network/server    网络/5xx                   → 退避重试
     · auth/bad_request  凭据或参数错                → 不重试，直接升级

产出：
  · 每章补上 `diagnosis` 字段（category / action / attempts / message）
  · 一份 `diagnosis.json` 报告（供人工排查）
"""

from __future__ import annotations

import time
from pathlib import Path

from ..llm import LLM, LLMCallError
from ..prompts import CHAPTER_SUMMARY_RETRY_SYSTEM
from ..schema import ChapterSummary
from .clean import clean_pages, strip_noise

# 分类 → 中文说明
CATEGORY_LABELS = {
    "empty_text": "章节正文为空",
    "json_invalid": "模型输出不是合法 JSON",
    "schema_invalid": "JSON 不符合 schema",
    "truncated": "输出被 max_tokens 截断",
    "rate_limit": "触发限流",
    "server_error": "服务端错误",
    "network": "网络/超时",
    "bad_request": "请求参数错误",
    "auth": "凭据问题",
    "unknown": "未知错误",
    "not_captured": "该章尚未采集（采集未覆盖到）",
}

# 可以自动重试的类别
RETRYABLE = {"json_invalid", "schema_invalid", "truncated", "rate_limit",
             "server_error", "network"}


def _err_dict(ch: dict) -> dict:
    """从章节记录里取出结构化错误信息。"""
    e = ch.get("summary_error")
    if isinstance(e, dict):
        return e
    return {"category": "unknown", "retryable": False,
            "message": str(e or "")[:300], "detail": ""}


def rebuild_chapter_text(chapter: dict, pages: list[dict],
                         ocr_root: Path) -> tuple[str, int]:
    """按「章节标题」重新装配正文。

    正常路径用的是「章节→页区间」映射；映射错了正文就会空。
    这里退一步：直接找 pages.jsonl 里 chapter_title 等于该章标题的页。
    返回 (正文, 用到的页数)。
    """
    title = chapter.get("title", "")
    picked: list[int] = []
    for p in sorted(pages, key=lambda x: x.get("page_no", 0)):
        if p.get("chapter_title") == title:
            picked.append(p.get("page_no", 0))
    if not picked:
        return "", 0

    texts = []
    by_page = {p.get("page_no", 0): p for p in pages}
    for n in picked:
        p = by_page[n]
        rel = Path(p["img"]).relative_to("screenshots").with_suffix(".txt")
        f = ocr_root / rel
        if f.exists():
            texts.append(f.read_text("utf-8"))
    return strip_noise(clean_pages(texts)), len(texts)


def _retry_summary(llm: LLM, book_title: str, ch: dict, category: str,
                   max_tokens: int = 8000) -> tuple[bool, str]:
    """按诊断结论重试一次总结。"""
    from ..prompts import CHAPTER_SUMMARY_USER

    err = _err_dict(ch)
    text = ch.get("text", "")
    if not text.strip():
        return False, "正文仍为空"

    user = CHAPTER_SUMMARY_USER.format(
        book_title=book_title, chapter_title=ch["title"], chapter_text=text)
    system = CHAPTER_SUMMARY_RETRY_SYSTEM.format(
        category=CATEGORY_LABELS.get(category, category),
        detail=err.get("detail") or err.get("message", "")[:300])

    # 截断 → 加预算；其余按原预算
    tokens = max_tokens * 2 if category == "truncated" else max_tokens
    try:
        s = llm.structured(system=system, user=user, schema=ChapterSummary,
                           model=llm.summary_model, max_tokens=tokens)
        ch["summary"] = s.model_dump()
        ch["summary_done"] = True
        ch["summary_error"] = None
        return True, "重试成功"
    except LLMCallError as e:
        ch["summary_error"] = e.to_dict()
        return False, f"重试仍失败：{e}"
    except Exception as e:
        ch["summary_error"] = {"category": "unknown", "retryable": False,
                               "message": str(e)[:300], "detail": ""}
        return False, f"重试仍失败：{e}"


def diagnose_and_recover(llm: LLM, book_title: str, chapters: list[dict],
                         pages: list[dict], quality: dict | None = None,
                         book_dir: Path | None = None,
                         max_attempts: int = 2,
                         on_progress=None) -> tuple[list[dict], dict]:
    """诊断并尝试自动恢复。返回 (章节列表, 诊断报告)。"""
    quality = quality or {}
    ocr_root = (book_dir / "ocr_pages") if book_dir else None
    report = {"book": book_title, "recovered": [], "escalated": [],
              "not_captured": [], "notes": []}

    for ch in chapters:
        if ch.get("summary_done"):
            continue

        err = _err_dict(ch)
        category = err.get("category", "unknown")
        attempts = 0
        action = "无"
        ok = False

        # ---- A0. 该章整段没被采集到 → 不是总结问题，是采集覆盖问题 ----
        if not ch.get("text", "").strip():
            last_page = max((p.get("page_no", 0) for p in pages), default=0)
            if ch.get("start_page", 0) > last_page:
                ch["diagnosis"] = {
                    "category": "not_captured", "label": CATEGORY_LABELS["not_captured"],
                    "action": f"该章在第 {ch.get('start_page')} 页之后，"
                              f"采集只到第 {last_page} 页 —— 补采后重跑本章",
                    "attempts": 0, "recovered": False, "message": "",
                }
                report["not_captured"].append(ch["index"])
                report["notes"].append(
                    f"{ch['title']}: 未采集（起始页 {ch.get('start_page')} > 末页 {last_page}）")
                continue

        # ---- A. 输入侧：正文为空 → 重新装配 ----
        if category == "empty_text" or not ch.get("text", "").strip():
            if ocr_root:
                new_text, n_pages = rebuild_chapter_text(ch, pages, ocr_root)
                if new_text.strip():
                    ch["text"] = new_text
                    ch["chars"] = len(new_text)
                    ch["pages_read"] = n_pages
                    action = f"按章节名重新装配正文（{n_pages} 页 / {len(new_text)} 字）"
                    report["notes"].append(
                        f"{ch['title']}: 原页区间无正文 → 按标题重取 {n_pages} 页")
                else:
                    action = "正文为空且无法按标题重取（该章确实无内容）"
                    report["notes"].append(f"{ch['title']}: 无正文可恢复")
                    ch["summary_done"] = True          # 确认无内容，标记完成不阻塞
                    ch["summary"] = ChapterSummary(core_thesis="").model_dump()
                    ch["summary_error"] = None
                    continue
            else:
                action = "正文为空（缺少 ocr_pages 目录，无法重装）"

        # ---- B. 调用侧：可重试的分类 → 带诊断信息重试 ----
        if category in RETRYABLE or not err.get("retryable", False):
            if category in {"auth", "bad_request"}:
                action = f"{CATEGORY_LABELS.get(category)} —— 不自动重试，需人工修配置"
            else:
                for attempt in range(max_attempts):
                    attempts += 1
                    ok, msg = _retry_summary(llm, book_title, ch, category)
                    if ok:
                        action = f"{CATEGORY_LABELS.get(category)} → 重试成功（第 {attempt + 1} 次）"
                        break
                    action = f"{CATEGORY_LABELS.get(category)} → {msg}"
                    e2 = _err_dict(ch)
                    if not e2.get("retryable", False):
                        break
                    time.sleep(min(20, 3 * (attempt + 1)))
        else:
            action = f"{CATEGORY_LABELS.get(category)} —— 不可自动恢复"

        ch["diagnosis"] = {
            "category": category,
            "label": CATEGORY_LABELS.get(category, category),
            "action": action,
            "attempts": attempts,
            "recovered": bool(ch.get("summary_done")),
            "message": (_err_dict(ch).get("message", "") if not ch.get("summary_done") else ""),
        }

        if ch.get("summary_done"):
            report["recovered"].append(ch["index"])
        else:
            report["escalated"].append(ch["index"])

        if on_progress:
            on_progress(ch)

    # 未采集章节汇总成一行输出（可能几十上百章，逐条刷屏没意义）
    if report["not_captured"] and on_progress:
        on_progress({"_aggregate": "not_captured",
                     "title": f"未采集章节 × {len(report['not_captured'])}",
                     "chars": 0,
                     "diagnosis": {
                         "category": "not_captured",
                         "label": CATEGORY_LABELS["not_captured"],
                         "action": f"起始页都在第 {last_page} 页之后 —— 补采后重跑这些章",
                         "attempts": 0, "recovered": False, "message": ""}})

    # 覆盖率类问题（整书级）也写进报告
    gate = quality.get("gate", {})
    if gate:
        report["quality_gate"] = gate
    report["summary"] = {
        "total": len(chapters),
        "recovered": len(report["recovered"]),
        "escalated": len(report["escalated"]),
        "not_captured": len(report["not_captured"]),
    }
    return chapters, report


def render_diagnosis_md(report: dict, chapters: list[dict]) -> str:
    """诊断报告（Markdown）。"""
    {c["index"]: c for c in chapters}
    L = ["# 采集与总结质量诊断报告\n"]
    s = report.get("summary", {})
    L.append(f"- 章节总数：{s.get('total', 0)}")
    L.append(f"- 自动恢复：{s.get('recovered', 0)}")
    L.append(f"- 需人工处理：{s.get('escalated', 0)}")
    L.append(f"- 未采集（补采后重跑）：{s.get('not_captured', 0)}\n")

    if report.get("quality_gate"):
        L.append("## 整书级质量闸门\n")
        for k, v in report["quality_gate"].items():
            L.append(f"- {'✅' if v else '⚠️'} {k}")
        L.append("")

    if report.get("notes"):
        L.append("## 诊断记录\n")
        L.extend(f"- {n}\n" for n in report["notes"])
        L.append("")

    L.append("## 逐章明细\n")
    L.append("| 章节 | 状态 | 诊断 | 处理 |")
    L.append("|---|---|---|---|")
    for ch in chapters:
        d = ch.get("diagnosis")
        if not d:
            continue
        status = "✅ 已恢复" if d.get("recovered") else "⛔ 需人工"
        L.append(f"| {ch['title'][:28]} | {status} | {d.get('label')} | {d.get('action')} |")
    return "\n".join(L) + "\n"
