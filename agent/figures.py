#!/usr/bin/env python3
"""
插图分流（M8）。

问题：图表页 OCR 出来是坐标轴刻度（"50 000." "12000"）等噪声，
而图表里的**真实数据**OCR 根本拿不到；同时噪声会污染全书评测的
「内容干货密度」「落地实用性」两项评分。

判定「本页有插图」—— 两路独立信号，必须交叉一致：
  A 浅色块（图像层）：正文深底（亮度 p50≈28），图表浅底（p50≈141）
  B 图注模式（文本层）：`图X-Y`/`表X-Y` 在**行首**且行**短**，后跟「来源：/注：」

实测 12 页：A⟺B 完全一致（3 页有图：图1-1 / 图1-2 / 图1-3）。

两个信号各修过一个 bug（都是实测逼出来的）：
  · A 漏检：图表内部的深色网格线/柱子会把行投影切碎
           （某行只有 15 个亮像素 < 阈值 102）→ 碎片太矮被丢。
           修法：先 BoxBlur(10) 再投影，通过率 28% → 89%。
  · B 误报：正文里的引用「…因此才有了图1-3」被当成图注。
           修法：要求图注在行首且行短（≤40 字）。

分流：
  · 图表区域 → 裁图 → **视觉模型** → 结构化图表描述（明确标注是转述）
  · 其余区域 → OCR → 纯正文（**剔除落在图表 bbox 内的坐标轴噪声行**）
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from .llm import LLM
from .schema import FigureDescription

# 数据目录（输出/状态）—— 装成插件时用 READING_AGENT_HOME 指到插件目录之外
DATA_HOME = Path(os.environ.get("READING_AGENT_HOME") or Path(__file__).resolve().parent.parent)

# 图注：行首 + 图/表编号 + 该行较短
CAPTION_HEAD = re.compile(r"^\s*(图|表)\s*(\d+)\s*[-—－]\s*(\d+)\s*.{0,40}$")
# 图注补充行
CAPTION_SRC = re.compile(r"^\s*(来源|注)\s*[：:]")


# --------------------------------------------------------------------------- 检测

def detect_light_boxes(path: Path, light_thresh: int = 100, blur: int = 10,
                       min_w: float = 0.10, min_h: float = 0.04,
                       min_light_ratio: float = 0.03) -> list[tuple]:
    """浅色块 = 图表区域。返回 [(x, y, w, h)]（图像像素坐标）。

    必须是「先模糊再投影」：图表里的深色网格线/柱子会把行投影切碎。
    """
    im = Image.open(path).convert("L").filter(ImageFilter.BoxBlur(blur))
    a = np.asarray(im, dtype=np.uint8)
    H, W = a.shape
    mask = a > light_thresh
    rows = mask.sum(axis=1)
    thr = W * 0.06
    bands, start = [], None
    for i, v in enumerate(rows):
        if v > thr:
            if start is None:
                start = i
        elif start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, H))

    boxes = []
    for (y0, y1) in bands:
        if (y1 - y0) < H * min_h:
            continue
        seg = mask[y0:y1]
        cols = seg.sum(axis=0)
        xs = np.nonzero(cols > (y1 - y0) * 0.5)[0]
        if len(xs) < 2:
            continue
        x0, x1 = int(xs[0]), int(xs[-1])
        if (x1 - x0) < W * min_w:
            continue
        # ★ 块内「亮像素占比」判据 —— 排除大标题/大字号的误检：
        #   真图表有实心浅底（实测 8.4%），模糊后的大标题只有 0.26%
        seg2 = mask[y0:y1, x0:x1]
        light_ratio = float(seg2.mean()) if seg2.size else 0.0
        if light_ratio < min_light_ratio:
            continue
        boxes.append((x0, y0, x1 - x0, y1 - y0))

    # 合并相邻/重叠块（同一张图被切成两块）
    boxes.sort(key=lambda b: b[1])
    merged: list[tuple] = []
    for b in boxes:
        if merged and b[1] < merged[-1][1] + merged[-1][3]:
            px, py, pw, ph = merged[-1]
            nx0, ny0 = min(px, b[0]), min(py, b[1])
            nx1 = max(px + pw, b[0] + b[2])
            ny1 = max(py + ph, b[1] + b[3])
            merged[-1] = (nx0, ny0, nx1 - nx0, ny1 - ny0)
        else:
            merged.append(b)
    return merged


def detect_captions(text: str) -> list[dict]:
    """图注：行首 + 编号 + 行短；并收集紧随其后的「来源：/注：」。"""
    caps: list[dict] = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = CAPTION_HEAD.match(ln)
        if not m:
            continue
        src = None
        for j in range(i + 1, min(i + 4, len(lines))):
            if CAPTION_SRC.match(lines[j]):
                src = lines[j].strip()
                break
            if lines[j].strip():
                break
        caps.append({
            "no": f"{m.group(1)}{m.group(2)}-{m.group(3)}",
            "caption": ln.strip(),
            "source": src,
        })
    return caps


def detect_figures(png_path: Path, text: str,
                   lines_json: Path | None = None) -> tuple[list[dict], str]:
    """判定页面有没有插图。

    配对方式：**按几何位置**，不按索引 —— 图注在图的正下方，
    用行 bbox 找到「紧跟在该图下方、横向重叠」的那一行作为它的图注。
    """
    boxes = detect_light_boxes(png_path)
    lines: list[dict] = []
    if lines_json and lines_json.exists():
        try:
            lines = json.loads(lines_json.read_text("utf-8"))
        except Exception:
            lines = []

    im = Image.open(png_path)
    H, W = im.height, im.width

    figures: list[dict] = []
    for (bx, by, bw, bh) in boxes:
        cap_line, cap_no, cap_src = None, None, None
        # 图注：块的下方且横向有重叠。
        # ⚠️ 不能遇到第一个几何候选就 break —— 图注常常被拆成多行
        #    （"图1-2 …" / "注：以2011年…" / "来源：…"），
        #    第一个候选可能是「注：」那行。要继续往下找**匹配图注格式**的那一行。
        cands = []
        for idx_r, r in enumerate(lines):
            ry = float(r.get("y", 0))
            rx0 = float(r.get("x0", 0))
            rx1 = float(r.get("x1", 0))
            if by + bh - 8 <= ry <= by + bh + 0.14 * H:
                if not (rx1 < bx - 10 or rx0 > bx + bw + 10):
                    cands.append((idx_r, (r.get("text") or "").strip()))
        for idx_r, txt in cands:
            m = CAPTION_HEAD.match(txt)
            if not m:
                continue
            cap_line = txt
            cap_no = f"{m.group(1)}{m.group(2)}-{m.group(3)}"
            for j in range(idx_r + 1, min(idx_r + 4, len(lines))):
                t2 = (lines[j].get("text") or "").strip()
                if CAPTION_SRC.match(t2):
                    cap_src = t2
                    break
                if t2:
                    break
            break
        figures.append({
            "box": [bx, by, bw, bh],
            "caption_no": cap_no,
            "caption": cap_line,
            "source": cap_src,
        })

    caps = detect_captions(text)
    has_box, has_cap = bool(figures), bool(caps)
    if has_box and has_cap:
        verdict = "both"
    elif has_cap:
        verdict = "caption_only"     # 有图注但没找到浅色块 → 图可能是深色背景
    elif has_box:
        verdict = "box_only"         # 有浅色块但没图注 → 可能是浅色引用块/表格
    else:
        verdict = "none"
    return figures, verdict


# --------------------------------------------------------------------------- 噪声剔除

def filter_axis_noise(lines_json: Path, boxes: list[list[int]]) -> int:
    """剔除落在图表 bbox 内的 OCR 行（坐标轴刻度）。

    需要 ocr.swift 的 --bbox 输出（每行带 x0/x1/y）。
    返回被剔除的行数。
    """
    if not lines_json.exists() or not boxes:
        return 0
    try:
        rows = json.loads(lines_json.read_text("utf-8"))
    except Exception:
        return 0

    def inside(r: dict) -> bool:
        rx0, rx1 = r.get("x0", 0), r.get("x1", 0)
        ry = r.get("y", 0)
        for (bx, by, bw, bh) in boxes:
            # 行中心落在块的纵向范围内，且横向与块有重叠
            if by - 4 <= ry <= by + bh + 4 and not (rx1 < bx or rx0 > bx + bw):
                return True
        return False

    keep = [r for r in rows if not inside(r)]
    dropped = len(rows) - len(keep)
    if dropped:
        txt = lines_json.with_suffix("").with_suffix(".txt")
        if txt.exists():
            txt.write_text("\n".join(r["text"] for r in keep) + "\n", "utf-8")
    return dropped


# --------------------------------------------------------------------------- 视觉描述

FIGURE_VISION_SYSTEM = """你是图表解读专家。用户会给你一张书里的插图（可能是柱状图、折线图、饼图、表格、地图、照片或示意图），以及它在书中的图注。

请客观描述这张图，**只描述你能从图上真实读到的内容**，不要编造数字。

字段要求：
- type：图表类型（柱状图/折线图/饼图/表格/地图/照片/示意图/流程图/其他）
- title：图表标题（用给定的图注，若无则从图上读）
- axes：坐标轴说明（x 轴是什么、y 轴是什么、范围与单位）；非坐标图写 {}
- key_data：图上能读到的关键数据点或趋势，每条一句话，最多 6 条
- insight：一句话说明这张图想说明什么
- source：数据来源（若图注里给了就照抄，否则空字符串）"""


def describe_figure(llm: LLM, img_path: Path, caption: str | None,
                    source: str | None, model: str | None = None) -> FigureDescription:
    """裁图送视觉模型 → 结构化图表描述。"""
    data = base64.standard_b64encode(img_path.read_bytes()).decode()
    user = (f"图注：{caption or '（无）'}\n"
            f"来源：{source or '（无）'}\n\n"
            f"请解读这张图。")
    return llm.structured_vision(
        system=FIGURE_VISION_SYSTEM,
        user=user,
        image_path=img_path,
        image_b64=data,
        schema=FigureDescription,
        model=model or llm.review_model,
        max_tokens=4000,
        effort=os.environ.get("LLM_VISION_EFFORT", "low"),
    )


# --------------------------------------------------------------------------- 编排

def process_book_figures(llm: LLM, book_dir: Path, state_dir: Path,
                         describe: bool = True, model: str | None = None,
                         on_progress=None) -> dict:
    """全本插图扫描：检测 → 剔除坐标轴噪声 → 视觉描述。"""
    shots_root = book_dir / "screenshots"
    ocr_root = book_dir / "ocr_pages"
    if not shots_root.exists():
        return {"figures": [], "stats": {}}

    import glob as _glob
    pngs = sorted(_glob.glob(str(shots_root / "*" / "page_*.png")))
    out_dir = book_dir / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_figs: list[dict] = []
    stats = {"pages": len(pngs), "pages_with_figures": 0, "caption_only": 0,
             "box_only": 0, "described": 0, "failed": 0, "noise_lines_dropped": 0}

    for png in pngs:
        png = Path(png)
        txt = ocr_root / png.relative_to(shots_root).with_suffix(".txt")
        text = txt.read_text("utf-8") if txt.exists() else ""
        lines_json = txt.with_suffix(".lines.json")
        figs, verdict = detect_figures(png, text, lines_json)
        if verdict == "none":
            continue
        stats["pages_with_figures"] += 1
        if verdict == "caption_only":
            stats["caption_only"] += 1
        elif verdict == "box_only":
            stats["box_only"] += 1

        # 剔除坐标轴噪声（需要 .lines.json）
        lines_json = txt.with_suffix(".lines.json")
        dropped = filter_axis_noise(lines_json, [f["box"] for f in figs])
        stats["noise_lines_dropped"] += dropped

        for f in figs:
            rec = {
                "page_no": int(png.stem.split("_")[-1]),
                "img": str(png.relative_to(book_dir)),
                "box": f["box"],
                "caption_no": f["caption_no"],
                "caption": f["caption"],
                "source": f["source"],
                "verdict": verdict,
                "description": None,
            }
            if describe:
                x, y, w, h = f["box"]
                crop_path = out_dir / f"fig_{rec['page_no']:04d}_{f['caption_no'] or 'x'}.png"
                try:
                    im = Image.open(png)
                    im.crop((x, y, x + w, y + h)).save(crop_path)
                    d = describe_figure(llm, crop_path, f["caption"], f["source"], model=model)
                    rec["description"] = d.model_dump()
                    rec["crop"] = str(crop_path.relative_to(book_dir))
                    stats["described"] += 1
                except Exception as e:  # noqa: BLE001
                    rec["error"] = str(e)[:200]
                    stats["failed"] += 1
            all_figs.append(rec)
        if on_progress:
            on_progress(rec, stats)

    (out_dir / "figures.json").write_text(
        json.dumps(all_figs, ensure_ascii=False, indent=2), "utf-8")
    return {"figures": all_figs, "stats": stats}


def figures_for_chapter(figures: list[dict], start_page: int, end_page: int) -> list[dict]:
    """取某章页区间内的插图。"""
    return [f for f in figures if start_page <= f.get("page_no", -1) <= end_page]


def format_figure_block(f: dict) -> str:
    """插图块格式：图N - 「标题:xxx」[插图描述:xxx]

    按用户指定的格式；描述来自视觉模型（与原文区分开）。
    """
    d = f.get("description") or {}
    no = f.get("caption_no") or "图"
    title = d.get("title") or f.get("caption") or ""
    # 去掉标题里的编号前缀，避免「图1-1图1-1日本GDP增长率」
    if title.startswith(no):
        title = title[len(no):].strip(" -—－:：")
    parts = []
    if d.get("type"):
        parts.append(str(d["type"]))
    axes = d.get("axes") or {}
    if axes:
        parts.append("；".join(f"{k} {v}" for k, v in axes.items()))
    parts.extend(str(k) for k in (d.get("key_data") or []))
    if d.get("insight"):
        parts.append(str(d["insight"]))
    desc = "。".join(p.rstrip("。") for p in parts if p)
    src = d.get("source") or f.get("source") or ""
    if src:
        desc = f"{desc}。来源：{src}" if desc else f"来源：{src}"
    return f'{no} - 「标题:{title}」[插图描述:{desc}]'


# 已注入的插图块（用于幂等：重复注入前先剥掉旧的）
INJECTED_BLOCK = re.compile(r"^\s*图\S+\s*-\s*「标题:[^」]*」\s*\[插图描述:[^\]]*\]\s*$")


def inject_into_page(page_text: str, page_lines: list[dict],
                     figs: list[dict]) -> tuple[str, int]:
    """把插图块插入到**正文对应位置**，并剔除坐标轴噪声行。

    位置规则：图注行在图的正下方 → 用插图块**替换图注行**（标题已折进块里，避免重复）。
    返回 (新文本, 剔除的噪声行数)。
    """
    # 幂等：先剥掉之前注入过的块
    raw_lines = [ln for ln in page_text.splitlines() if not INJECTED_BLOCK.match(ln)]
    if not figs:
        return ("\n".join(raw_lines) + "\n" if raw_lines else page_text), 0

    lines = raw_lines
    n_drop = 0

    # 1) 剔除落在图表 bbox 内的行（坐标轴刻度）
    def in_box(r: dict) -> bool:
        rx0, rx1 = r.get("x0", 0), r.get("x1", 0)
        ry = r.get("y", 0)
        for (bx, by, bw, bh) in [f["box"] for f in figs]:
            if by - 6 <= ry <= by + bh + 6 and not (rx1 < bx or rx0 > bx + bw):
                return True
        return False

    keep_idx = []
    for i, r in enumerate(page_lines):
        if i < len(lines) and in_box(r):
            n_drop += 1
            continue
        keep_idx.append(i)
    if n_drop:
        lines = [lines[i] for i in keep_idx if i < len(lines)]

    # 2) 找到图注行，**替换**成插图块（不是插入 —— 否则图注会重复）
    for f in figs:
        cap = f.get("caption")
        no = f.get("caption_no")
        pos = None
        if cap or no:
            for i, ln in enumerate(lines):
                t = ln.strip()
                if no and t.startswith(no):
                    pos = i
                    break
                if cap and cap.startswith(t[:8]) and len(t) >= 4:
                    pos = i
                    break
        if pos is None:
            # 找不到图注 → 插到最前面（宁可靠前也不要丢）
            lines.insert(0, format_figure_block(f))
            continue
        lines[pos] = format_figure_block(f)
        # 图注后面的「来源：/注：」已折进块里 → 删掉避免重复
        if pos + 1 < len(lines) and CAPTION_SRC.match(lines[pos + 1].strip()):
            del lines[pos + 1]

    return "\n".join(lines) + "\n", n_drop


def figures_for_chapter(figures: list[dict], start_page: int, end_page: int) -> list[dict]:
    """取某章页区间内的插图。"""
    return [f for f in figures if start_page <= f.get("page_no", -1) <= end_page]
