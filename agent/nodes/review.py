#!/usr/bin/env python3
"""全书五维评测（BookReviewAgent）。"""

from __future__ import annotations

from ..llm import LLM
from ..prompts import BOOK_REVIEW_SYSTEM, BOOK_REVIEW_USER
from ..schema import BookReview

# 章节摘要 → 评审用摘要文本
_MAX_CHARS = 120_000     # 单次评审的摘要输入上限（超出则按章压缩）


def _digest(chapters: list[dict], max_chars: int = _MAX_CHARS) -> str:
    """把逐章摘要拼成评审输入。

    超长时按「先压缩每章、仍超则按比例截断」处理 —— 不做静默截断，
    截断处会显式标注，避免模型以为看到了全书。
    """
    def block(ch: dict) -> str:
        s = ch.get("summary") or {}
        parts = [f"### {ch['title']}"]
        if s.get("core_thesis"):
            parts.append(f"主旨：{s['core_thesis']}")
        for k, label in (("key_points", "要点"), ("concepts", "概念"),
                         ("cases", "案例"), ("data_points", "数据")):
            vals = s.get(k) or []
            if vals:
                parts.append(f"{label}：" + "；".join(vals))
        if s.get("reflections"):
            parts.append(f"思考：{s['reflections']}")
        return "\n".join(parts)

    blocks = [block(c) for c in chapters if c.get("summary")]
    text = "\n\n".join(blocks)
    if len(text) <= max_chars:
        return text

    # 超限：先按比例压缩每章，再截断并显式标注
    ratio = max_chars / len(text)
    compressed = []
    for b in blocks:
        compressed.append(b[:max(200, int(len(b) * ratio))])
    out = "\n\n".join(compressed)
    return out + f"\n\n[注意：章节摘要总长超过 {max_chars} 字，已按比例压缩，可能遗漏细节]"


def review_book(llm: LLM, book_title: str, chapters: list[dict],
                quality: dict | None = None, model: str | None = None) -> BookReview:
    """产出全书五维评分 + 专业书评。"""
    quality = quality or {}
    digest = _digest(chapters)
    if not digest.strip():
        raise ValueError("没有可用的章节摘要，无法生成全书评测")

    user = BOOK_REVIEW_USER.format(
        book_title=book_title,
        chapters_digest=digest,
        n_chapters=len(chapters),
        total_chars=sum(c.get("chars", 0) for c in chapters),
        n_pages=quality.get("pages_covered", "未知"),
        mean_conf=quality.get("mean_conf", "未知"),
    )
    return llm.structured(
        system=BOOK_REVIEW_SYSTEM,
        user=user,
        schema=BookReview,
        model=model or llm.review_model,
        max_tokens=16000,
    )


def render_review_md(review: BookReview, book_title: str,
                     quality: dict | None = None) -> str:
    """渲染全书评测报告（Markdown）。"""
    quality = quality or {}
    L = []
    L.append(f"# 《{book_title}》全书评测报告\n")

    if quality:
        L.append("> **底稿质量**：OCR 覆盖率 {:.1%}，平均置信度 {:.3f}，"
                 "平均 {:.0f} 字/页\n"
                 .format(quality.get("coverage", 0), quality.get("mean_conf", 0),
                         quality.get("avg_chars_per_page", 0)))

    L.append("## 一、书籍整体定位\n")
    L.append(f"{review.positioning}\n")

    L.append("## 二、五维评分明细\n")
    L.append("| 维度 | 评分 | 依据 |")
    L.append("|---|---|---|")
    for name, d in (("逻辑结构性", review.score_logic),
                    ("内容干货密度", review.score_density),
                    ("观点创新度", review.score_innovation),
                    ("落地实用性", review.score_practical),
                    ("阅读启发价值", review.score_inspire)):
        L.append(f"| {name} | {d.score:.1f} | {d.reason} |")
    L.append(f"| **综合平均** | **{review.avg_score:.2f}** | — |")
    L.append(f"| **书籍评级** | **{review.level}** | — |\n")

    L.append("## 三、本书核心优势\n")
    L.extend(f"{i}. {x}\n" for i, x in enumerate(review.strengths, 1))
    L.append("\n## 四、本书短板与局限性\n")
    L.extend(f"{i}. {x}\n" for i, x in enumerate(review.weaknesses, 1))
    L.append("\n## 五、适合人群 / 不适合人群\n")
    L.append("**适合**：\n")
    L.extend(f"- {x}\n" for x in review.suitable_for)
    L.append("\n**不适合**：\n")
    L.extend(f"- {x}\n" for x in review.not_suitable_for)
    L.append("\n## 六、高效阅读建议\n")
    L.extend(f"{i}. {x}\n" for i, x in enumerate(review.reading_advice, 1))
    return "\n".join(L)
