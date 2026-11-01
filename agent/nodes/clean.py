#!/usr/bin/env python3
"""OCR 文本清洗（确定性节点）。

移植自 ../weread-screenshot/aggregate_notes.py 的 clean_text（已在那套实现里跑过 60 本书）：
  1) 全角空格 → 普通空格
  2) 每行 strip + 行内空白合并
  3) 丢空行
  4) ★ 全部行拼成单个段落 —— 消除 OCR 视觉换行对句子的切碎
  5) 多空格 → 单空格
  6) ★ 按中英句末标点重新分句

第 4 步是关键：OCR 是按视觉行识别的，一句话常被拆成几行
（「…我很难」/「友合作…」），拼回段落再按标点重切，语义完整性显著提升。
"""

from __future__ import annotations

import re

_SENTENCE_END = r"(?<=[。！？!?；;])"
_MULTISPACE = re.compile(r" {2,}")


def clean_text(raw: str) -> str:
    """把一页 OCR 的视觉行还原成连续正文（按句切分）。"""
    if not raw:
        return ""
    s = raw.replace("　", " ")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines()]
    lines = [ln for ln in lines if ln]
    paragraph = "".join(lines)          # ← 消除视觉换行的切碎
    paragraph = _MULTISPACE.sub(" ", paragraph)
    parts = [p.strip() for p in re.split(_SENTENCE_END, paragraph)]
    return "\n\n".join(p for p in parts if p)


def clean_pages(page_texts: list[str]) -> str:
    """多页拼接后再清洗（章节级正文）。"""
    return clean_text("\n".join(page_texts))


def page_texts_to_paragraphs(page_texts: list[str]) -> list[str]:
    """按页保留段落边界（需要页码回溯时用）。"""
    out = []
    for t in page_texts:
        c = clean_text(t)
        if c:
            out.append(c)
    return out


# --------------------------------------------------------------------- 噪声过滤

_NOISE_PATTERNS = [
    re.compile(r"^\s*上一页\s*$"),
    re.compile(r"^\s*下一页\s*$"),
    re.compile(r"^\s*下一章\s*$"),
    re.compile(r"^\s*上一章\s*$"),
    re.compile(r"^\s*[<＜]上一页下一页[>＞]?\s*$"),
]


def strip_noise(text: str) -> str:
    """去掉残留的翻页按钮等 UI 文本（OCR 阶段已裁掉 pager 行，这里是兜底）。"""
    lines = [ln for ln in text.splitlines()
             if not any(p.match(ln) for p in _NOISE_PATTERNS)]
    return "\n".join(lines)
