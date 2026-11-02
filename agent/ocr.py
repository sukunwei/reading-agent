#!/usr/bin/env python3
"""
P1 —— 分页 OCR + 质量闸门

流程：
    output/<书名>/screenshots/<NNN_章名>/page_NNNN.png   （整页 1280×815，左右两栏）
        ↓ 按 pages.jsonl 的 columns 字段**临时裁栏**
    临时目录/page_NNNN.colK.png
        ↓ 调用 macOS Vision（ocr.swift 编译出的二进制）
    临时目录/page_NNNN.colK.txt + .meta.json
        ↓ 按栏序拼接
    output/<书名>/ocr_pages/<NNN_章名>/page_NNNN.txt      （一页一 TXT，与 PNG 一一对应）

为什么要裁栏：
    整页图左右两栏，Vision 按 y 分行会把两栏同行并成一行 →
    阅读序变成 L1 R1 L2 R2…（实测 30 页：8.2% 的行跨栏，每页都有）。
    裁栏后每栏单独识别，阅读序天然正确。

裁栏只在 OCR 阶段临时进行，**不在产物目录留下多余图片**。

用法：
    python3 agent/ocr.py                       # 处理全部书
    python3 agent/ocr.py --book 时势            # 只处理一本
    python3 agent/ocr.py --force               # 重跑（默认跳过已有 .txt）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# 数据目录（输出/状态）：装成插件时不该写进插件目录 → READING_AGENT_HOME 可覆盖
sys.path.insert(0, str(ROOT))
import contextlib

from paths import book_paths, list_books, resolve_out_dir

OCR_SRC = ROOT / "ocr.swift"
BIN_DIR = ROOT / "bin"
OCR_BIN = BIN_DIR / "ocr"


# --------------------------------------------------------------------------- 构建

def build_ocr_binary(force: bool = False) -> Path:
    """把 ocr.swift 编译成本地二进制（只需一次）。"""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if OCR_BIN.exists() and not force:
        return OCR_BIN
    if not OCR_SRC.exists():
        raise FileNotFoundError(f"找不到 {OCR_SRC}")
    print(f"▶ 编译 OCR 二进制 → {OCR_BIN}")
    r = subprocess.run(["swiftc", "-O", str(OCR_SRC), "-o", str(OCR_BIN)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"编译失败:\n{r.stderr[:2000]}")
    return OCR_BIN


# --------------------------------------------------------------------------- 主流程

def read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(Exception):
                out.append(json.loads(line))
    return out


def process_book(book_dir: Path, state_dir: Path, binary: Path,
                 batch: int = 40, force: bool = False) -> dict:
    pages_path = state_dir / "pages.jsonl"
    if not pages_path.exists():
        print(f"  ⚠️ 缺少 {pages_path}，跳过")
        return {}
    pages = read_jsonl(pages_path)
    if not pages:
        print("  ⚠️ pages.jsonl 为空")
        return {}

    ocr_root = book_dir / "ocr_pages"
    ocr_root.mkdir(parents=True, exist_ok=True)

    todo = []
    for p in pages:
        out_txt = ocr_root / Path(p["img"]).relative_to("screenshots").with_suffix(".txt")
        if out_txt.exists() and not force:
            continue
        todo.append(p)
    print(f"  共 {len(pages)} 页，待识别 {len(todo)} 页")

    index_rows: list[dict] = []
    done = 0
    t0 = __import__("time").time()

    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        with tempfile.TemporaryDirectory(prefix="wr_ocr_") as tmp:
            tmpdir = Path(tmp)
            # 1) 裁栏
            names = []
            for p in chunk:
                img_path = book_dir / p["img"]
                if not img_path.exists():
                    continue
                cols = p.get("columns") or []
                if not cols:
                    cols = [[0, Image.open(img_path).width]]
                try:
                    im = Image.open(img_path)
                except Exception as e:
                    print(f"  ⚠️ 打不开 {img_path.name}: {e}")
                    continue
                # 正文下边界：排除底部「上一页/下一页」按钮那一行，否则会混进正文
                bottom = int(p.get("pager_top") or im.height)
                bottom = max(1, min(bottom, im.height))
                stem = f"{p['page_no']:04d}"
                for k, (x0, x1) in enumerate(cols):
                    x0, x1 = max(0, int(x0)), min(im.width, int(x1))
                    if x1 - x0 < 20:
                        continue
                    name = f"{stem}.col{k}"
                    col_img = im.crop((x0, 0, x1, bottom))
                    col_img.save(tmpdir / f"{name}.png")
                    names.append((name, p, k, 0))

            if not names:
                continue

            # 2) 识别
            r = subprocess.run([str(binary), str(tmpdir), "--bbox"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ❌ OCR 失败:\n{r.stdout[-1500:]}\n{r.stderr[-800:]}")
                continue

            # 3) 整页外缘检查：裁剪框若把 chrome/邻栏也框进来，最外 2px 就会有墨
            page_edge_ink = 0
            if chunk:
                p0 = chunk[0]
                f0 = book_dir / p0["img"]
                try:
                    a0 = np.asarray(Image.open(f0).convert("L"), dtype=np.uint8)
                    i0 = a0 > 90
                    page_edge_ink = int(i0[:, :2].sum() + i0[:, -2:].sum()
                                        + i0[:2, :].sum() + i0[-2:, :].sum())
                except Exception:
                    pass

            # 4) 拼接 + 落盘
            per_page: dict[int, list[str]] = {}
            for name, p, k, edge_ink in names:
                txt = tmpdir / f"{name}.txt"
                meta = tmpdir / f"{name}.meta.json"
                text = txt.read_text("utf-8") if txt.exists() else ""
                m = json.loads(meta.read_text("utf-8")) if meta.exists() else {}
                per_page.setdefault(p["page_no"], []).append(text)
                index_rows.append({
                    "page_no": p["page_no"], "col": k, "edge_ink": edge_ink,
                    "chapter_title": p.get("chapter_title"),
                    "chars": m.get("chars", len(text)),
                    "lines": m.get("lines", 0),
                    "mean_conf": m.get("mean_conf", 0),
                    "low_conf_ratio": m.get("low_conf_ratio", 1),
                    "col_spanning_lines": m.get("col_spanning_lines", 0),
                    "page_edge_ink": page_edge_ink,
                })

            for p in chunk:
                parts = per_page.get(p["page_no"])
                if not parts:
                    continue
                rel_txt = Path(p["img"]).relative_to("screenshots").with_suffix(".txt")
                out_txt = ocr_root / rel_txt
                out_txt.parent.mkdir(parents=True, exist_ok=True)
                # 栏间补一个换行，避免两栏最后一行与下一栏第一行黏成一句
                out_txt.write_text("\n".join(x.strip("\n") for x in parts if x.strip()) + "\n",
                                   "utf-8")
                # 行级 bbox（M8 剔除坐标轴噪声 / 图表定位用）：合并两栏的行。
                # ⚠️ 坐标空间要对齐：OCR 跑在**裁栏图**上，bbox 是栏内归一化坐标；
                #    这里换算成**整页像素坐标**（x 加栏偏移、y 乘裁剪高），
                #    否则图表框（整页坐标）和行 bbox（栏内坐标）对不上。
                line_rows = []
                bottom = int(p.get("pager_top") or 0) or 0
                for k in range(len(parts)):
                    lj = tmpdir / f"{p['page_no']:04d}.col{k}.lines.json"
                    if not lj.exists():
                        continue
                    try:
                        rows_k = json.loads(lj.read_text("utf-8"))
                    except Exception:
                        continue
                    ck = cols[k] if k < len(cols) else [0, 0]
                    cx0, cx1 = int(ck[0]), int(ck[1])
                    cw = max(1, cx1 - cx0)
                    for r in rows_k:
                        r["x0"] = round(float(r.get("x0", 0)) * cw + cx0, 1)
                        r["x1"] = round(float(r.get("x1", 0)) * cw + cx0, 1)
                        if bottom:
                            r["y"] = round(float(r.get("y", 0)) * bottom, 1)
                        r["col"] = k
                    line_rows.extend(rows_k)
                if line_rows:
                    (ocr_root / rel_txt.with_suffix(".lines.json")).write_text(
                        json.dumps(line_rows, ensure_ascii=False), "utf-8")
            done += len(chunk)
            el = __import__("time").time() - t0
            print(f"  ▸ {done}/{len(todo)} 页  [{el:.0f}s, {el/max(1,done):.1f}s/页]")

    # ---- 质量报告 ----
    all_rows = index_rows
    if not all_rows:                      # 增量：读回既有索引
        idx = ocr_root / "_ocr_index.jsonl"
        if idx.exists():
            all_rows = read_jsonl(idx)

    total_chars = sum(r["chars"] for r in all_rows)
    low_conf = [r for r in all_rows if r["mean_conf"] < 0.9 or r["low_conf_ratio"] > 0.3]
    # 裁剪框检查：整页图最外 2px 不该有墨（有墨 = 把 chrome/邻栏框进来了）
    # 注：**栏内**边缘有墨是正常的 —— 微信读书正文本来就贴到栏边界，
    #     所以不能拿栏边缘判断「切字」（曾误报 42/46）。
    cut = [r for r in all_rows if r.get("page_edge_ink", 0) > 0]
    covered = len({r["page_no"] for r in all_rows})
    coverage = covered / len(pages) if pages else 0

    report = {
        "book": book_dir.name,
        "pages_total": len(pages),
        "pages_covered": covered,
        "coverage": round(coverage, 4),
        "total_chars": total_chars,
        "avg_chars_per_page": round(total_chars / max(1, covered), 1),
        "low_conf_pages": sorted({r["page_no"] for r in low_conf}),
        "cut_edge_pages": sorted({r["page_no"] for r in cut}),
        "col_spanning_pages": sorted({r["page_no"] for r in all_rows
                                     if r.get("col_spanning_lines", 0) > 0}),
        "mean_conf": round(sum(r["mean_conf"] for r in all_rows) / max(1, len(all_rows)), 4),
        "gate": {
            "coverage_ok": coverage >= 0.95,
            "cut_edge_ok": not cut,
            "low_conf_ratio_ok": len(low_conf) / max(1, len(all_rows)) <= 0.05,
        },
    }
    report["gate_passed"] = all(report["gate"].values())
    (book_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    (ocr_root / "_ocr_index.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows) + "\n", "utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=None, help="书名关键字，默认全部")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="重新编译 OCR 二进制")
    ap.add_argument("--out-dir", default=None,
                    help="产物根目录（默认 $PWD/reading-output，可用 READING_AGENT_OUT 覆盖）")
    args = ap.parse_args()

    binary = build_ocr_binary(force=args.rebuild)

    out_root = resolve_out_dir(args.out_dir)
    books = list_books(out_root)
    if args.book:
        books = [b for b in books if args.book in b.name]
    if not books:
        print("没有可处理的书")
        return 1

    for book_dir in books:
        print(f"\n▶ {book_dir.name}")
        _, state_dir = book_paths(out_root, book_dir.name)
        rep = process_book(book_dir, state_dir, binary, batch=args.batch, force=args.force)
        if not rep:
            continue
        print(f"  覆盖率 {rep['coverage']:.1%}  平均 {rep['avg_chars_per_page']:.0f} 字/页  "
              f"平均置信 {rep['mean_conf']:.3f}")
        print(f"  闸门: {rep['gate']}  → {'✅ 通过' if rep['gate_passed'] else '⚠️ 未通过'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
