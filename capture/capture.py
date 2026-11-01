#!/usr/bin/env python3
"""
采集层（P0）—— 微信读书网页版 reader 逐页截图（Python / Playwright）

截图方式沿用 ../weread-screenshot 的成熟方案：
    page.screenshot(clip=...)，clip 起点在顶部 header（.readerTopBar）之下
    → 得到「整一页」正文，**不含**书名 / 首页 / 我的书架 / 头像那一栏。

在其之上加了四处加固（都是实测踩出来的）：

 1. 显式切到「上下滚动双栏」阅读模式
    微信读书有两种模式，工具栏上两个互斥按钮：
        .readerControls_item.isHorizontalReader → 上下滚动双栏（两页并排 + 底部上一页/下一页）
        .readerControls_item.isNormalReader     → 翻页单页（一页一屏，无翻页按钮）
    该模式会被持久化，换设备/账号可能是另一种，必须显式切换并校验。
    双栏模式底部有明确的翻页按钮，导航最稳。

 2. 目录面板用 CSS 隐藏
    点 catalog 按钮关不掉（按钮被 .wr_mask 拦截），实测只有 CSS display:none 有效。

 3. 等渲染稳定再截
    翻页后正文 canvas 先渲染、插图（图1-1 这类）后加载，
    只等「内容变了」会截到图表空白 / 字被切在边上的过渡帧。
    → 要求 canvas 内容指纹连续 N 次不变才截图。

 4. 分栏信息只记录、不落盘
    整页图是左右两栏，Vision 按 y 分行会把两栏同行并成一行 → 阅读序变成 L1 R1 L2 R2…
    （实测 30 页：8.2% 的行跨栏，每页都有）。
    → 采集时把每栏的 x 范围记进 pages.jsonl 的 columns 字段，
      OCR 阶段按它**临时裁剪**分栏再识别，不产生多余的图片文件。

产出：
    output/<书名>/screenshots/<NNN_章名>/page_NNNN.png         整页（不含 header）
    output/<书名>/toc.json
    state/<书名>/pages.jsonl
    state/<书名>/run_config.json

用法：
    python3 capture/capture.py                  # 全书
    python3 capture/capture.py --max-pages 20   # 冒烟
    python3 capture/capture.py --resume         # 续跑
"""

from __future__ import annotations

import os

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
# 数据目录（输出/状态）：装成插件时不该写进插件目录 → READING_AGENT_HOME 可覆盖
DATA_HOME = Path(os.environ.get("READING_AGENT_HOME") or ROOT)

DEFAULT_BOOK_URL = ("https://weread.qq.com/web/reader/"
                    "95332ad0813ab8705g016ce7kc81322c012c81e728d9d180")

# 视口按用户提供的参考图：1280 宽、815 高
VIEWPORT = {"width": 1280, "height": 815}
# ⚠️ device_scale_factor 必须是 1：
#    实测 DSF=2 时微信读书的 canvas 会按设备像素排版而画布只留 CSS 尺寸 → 正文左右被切字；
#    DSF=1 渲染完整，且与用户参考图一致（左右两栏 + 底部上一页/下一页）。
#    1280 宽、393 CSS 栏宽也是 ../weread-screenshot 跑通 60 本书时用的等效分辨率。
DSF = 1
# 输出图尺寸：整页（不含 header）固定 1280×815 像素
OUTPUT_W, OUTPUT_H = 1280, 815


# --------------------------------------------------------------------------- 工具

def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    return name[:80] or "未命名"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 页面交互

JS_GEOMETRY = """() => {
  const r = e => { const b = e.getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height),
             right: Math.round(b.right), bottom: Math.round(b.bottom) }; };
  let headerBottom = 0;
  for (const sel of ['.readerTopBar', '[class*="readerTopBar"]', '.readerHeader', 'header[class*="reader"]']) {
    const e = document.querySelector(sel);
    if (!e) continue;
    const b = r(e);
    if (b.h > 0 && b.h < innerHeight / 2) { headerBottom = b.bottom; break; }
  }
  const canvases = [...document.querySelectorAll('canvas')].map(c => r(c));
  const vis = canvases.filter(c => c.x + c.w > 0 && c.x < innerWidth);
  // 底部翻页按钮所在行（OCR 裁栏时要排除，否则「上一页/下一页」混进正文）
  let pagerTop = innerHeight;
  for (const e of document.querySelectorAll('.renderTarget_pager, .renderTarget_pager_content, .renderTarget_pager_button')) {
    const b = e.getBoundingClientRect();
    if (b.width > 0 && b.height > 0 && b.bottom > b.top) pagerTop = Math.min(pagerTop, Math.round(b.top));
  }
  let toolbarLeft = innerWidth;
  for (const e of document.querySelectorAll('.readerControls_item, [class*="readerControls"]')) {
    const b = r(e);
    if (b.w > 0 && b.right > innerWidth - 160) toolbarLeft = Math.min(toolbarLeft, b.x);
  }
  const contentRight = vis.length ? Math.max(...vis.map(c => Math.min(c.right, innerWidth)))
                                  : (toolbarLeft < innerWidth ? toolbarLeft - 16 : innerWidth);
  const contentLeft  = vis.length ? Math.max(0, Math.min(...vis.map(c => c.x))) : 0;
  return { headerBottom, canvases, visCanvases: vis, contentRight, contentLeft,
           pagerTop, innerWidth, innerHeight, scrollX, scrollY };
}"""

JS_CONTENT_SIG = """() => [...document.querySelectorAll('canvas')].map(c => {
  try { const u = c.toDataURL('image/png'); return u.length + ':' + u.slice(-40); } catch (e) { return 'X'; }
}).join('|')"""

JS_CHAPTER = """() => {
  const t = document.querySelector('.renderTargetPageInfo_header_chapterTitle');
  return t ? (t.innerText || '').trim() || null : null;
}"""

JS_READY = """() => {
  const bad = [];
  const visible = e => { const b = e.getBoundingClientRect(); const c = getComputedStyle(e);
    return b.width > 0 && b.height > 0 && c.display !== 'none' && c.visibility !== 'hidden' && c.opacity !== '0'; };
  for (const sel of ['.wr_mask', '.wr_dialog_mask', '.wr_dialog_container', '.readerCatalog_list']) {
    const e = document.querySelector(sel);
    if (e && visible(e)) bad.push(sel);
  }
  return bad;
}"""

JS_TOC = """() => [...document.querySelectorAll('.readerCatalog_list_item')].map((e, i) => {
  const inner = e.querySelector('.readerCatalog_list_item_inner');
  const m = (inner ? inner.className : '').match(/level_(\\d+)/);
  return { index: i, level: m ? parseInt(m[1]) : 1,
           title: ((e.querySelector('.readerCatalog_list_item_title_text') || {}).innerText || '').trim() };
}).filter(x => x.title)"""


async def ensure_reading_mode(page) -> dict:
    """切到「上下滚动双栏」模式（底部有上一页/下一页）。"""
    state = await page.evaluate("""() => ({
      horizontal: !!document.querySelector('.readerControls_item.isHorizontalReader'),
      normal: !!document.querySelector('.readerControls_item.isNormalReader'),
      canvases: document.querySelectorAll('canvas').length,
      pager: [...document.querySelectorAll('.renderTarget_pager_button')]
               .filter(b => b.getBoundingClientRect().width > 0).length,
    })""")
    if state["horizontal"]:
        print("  阅读模式: 上下滚动（双栏）✅")
        return state
    if state["normal"]:
        print("  阅读模式: 当前是翻页（单页）→ 切到上下滚动双栏")
        try:
            await page.click(".readerControls_item.isNormalReader", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)
    after = await page.evaluate("""() => ({
      horizontal: !!document.querySelector('.readerControls_item.isHorizontalReader'),
      canvases: document.querySelectorAll('canvas').length,
      pager: [...document.querySelectorAll('.renderTarget_pager_button')]
               .filter(b => b.getBoundingClientRect().width > 0).length,
    })""")
    print(f"  阅读模式: {'上下滚动（双栏）✅' if after['horizontal'] else '⚠️ 未能切换'} "
          f" canvas={after['canvases']} pager={after['pager']}")
    return after


async def wait_stable(page, polls: int = 3, timeout_ms: int = 25000) -> bool:
    """等 canvas 内容指纹连续 N 次不变（正文与插图都渲染完、过渡帧结束）。"""
    last, same, waited = None, 0, 0
    while waited < timeout_ms:
        sig = await page.evaluate(JS_CONTENT_SIG)
        if sig == last:
            same += 1
            if same >= polls:
                return True
        else:
            same, last = 0, sig
        await page.wait_for_timeout(400)
        waited += 400
    return False


async def flip_next(page) -> bool:
    """点「下一页」。⚠️ .renderTarget_pager_content_right 是外层 DIV，点它无效。"""
    return await page.evaluate("""() => {
      const b = document.querySelector('.renderTarget_pager_button_right')
             || [...document.querySelectorAll('.renderTarget_pager_button')].pop();
      if (b) { b.click(); return true; }
      return false;
    }""")


# --------------------------------------------------------------------------- 主流程

async def run(args) -> int:
    state_path = Path(args.state)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        print(f"❌ 找不到登录态 {state_path}", file=sys.stderr)
        return 1

    book_url = args.book_url or DEFAULT_BOOK_URL

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=DSF,
                                        storage_state=json.loads(state_path.read_text("utf-8")))
        page = await ctx.new_page()

        await page.goto(book_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        for sel in (".wr_dialog_closeButton", ".wr_dialog_mask"):
            try:
                await page.click(sel, timeout=1200)
            except Exception:
                pass
            await page.wait_for_timeout(300)
        try:
            await page.wait_for_function('document.querySelectorAll("canvas").length > 0', timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        # 视口高度 = 输出高 + header 高，裁掉 header 后正好 1280×815
        _hb0 = await page.evaluate("""() => {
          const e = document.querySelector('.readerTopBar');
          if (!e) return 0;
          const r = e.getBoundingClientRect();
          return Math.round(r.bottom);
        }""")
        if _hb0 > 0:
            await page.set_viewport_size({"width": VIEWPORT["width"],
                                          "height": OUTPUT_H + _hb0})
            await page.wait_for_timeout(1500)

        mode = await ensure_reading_mode(page)
        if not mode.get("horizontal"):
            print("❌ 需要「上下滚动双栏」模式，请切好后重试", file=sys.stderr)
            await browser.close()
            return 3

        # 隐藏 chrome（目录面板点按钮关不掉，只能 CSS 隐藏）
        await page.add_style_tag(content="""
            .readerCatalog, .readerCatalog_list, .wr_mask { display: none !important; }
        """)
        await page.wait_for_timeout(800)

        book_title = await page.evaluate("""() => {
          const t = document.querySelector('.readerTopBar_title, .readerTopBar_title_text');
          return (document.title.split(' - ')[0] || (t ? t.innerText.trim() : '') || '未命名').trim();
        }""")
        out_dir = DATA_HOME / "output" / book_title
        state_dir = DATA_HOME / "state" / book_title
        shots_dir = out_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        print(f"  书名: {book_title}")
        print(f"  视口: {VIEWPORT['width']}x{VIEWPORT['height']} @{DSF}x")

        # ---- 目录 ----
        print("\n▶ 抓取目录…")
        try:
            await page.click("button.readerControls_item.catalog", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        toc = await page.evaluate(JS_TOC)
        (out_dir / "toc.json").write_text(json.dumps(toc, ensure_ascii=False, indent=2), "utf-8")
        print(f"  {len(toc)} 个条目 → toc.json")

        l1_chapters = [t for t in toc if t["level"] == 1]

        def owner_l1(toc_index: int) -> dict | None:
            owner = l1_chapters[0] if l1_chapters else None
            for c in l1_chapters:
                if c["index"] <= toc_index:
                    owner = c
                else:
                    break
            return owner

        def l1_dirname(l1: dict) -> str:
            return f"{l1_chapters.index(l1) + 1:03d}_{sanitize(l1['title'])}"

        # ---- 运行配置（续跑指纹） ----
        run_config = {
            "book_url": book_url, "book_title": book_title,
            "viewport": VIEWPORT, "dsf": DSF, "reading_mode": "horizontal",
            "toc_fingerprint": sha1("|".join(f"{t['level']}:{t['title']}" for t in toc).encode())[:16],
            "captured_at": __import__("datetime").datetime.now().isoformat(),
        }
        cfg_path = state_dir / "run_config.json"
        prev_cfg = read_json(cfg_path)
        if prev_cfg and args.resume:
            same = (prev_cfg.get("viewport") == VIEWPORT and prev_cfg.get("dsf") == DSF
                    and prev_cfg.get("toc_fingerprint") == run_config["toc_fingerprint"])
            if not same:
                print("\n❌ 续跑指纹不一致，拒绝续跑（page_no ↔ 内容会静默错位）：")
                print(f"   旧: {prev_cfg.get('viewport')} dsf={prev_cfg.get('dsf')} toc={prev_cfg.get('toc_fingerprint')}")
                print(f"   新: {VIEWPORT} dsf={DSF} toc={run_config['toc_fingerprint']}")
                await browser.close()
                return 2
            print("  指纹一致，允许续跑")
        cfg_path.write_text(json.dumps(run_config, ensure_ascii=False, indent=2), "utf-8")

        pages_path = state_dir / "pages.jsonl"
        existing = read_jsonl(pages_path)
        seen_hashes = {p["sha1"] for p in existing}
        page_no = existing[-1]["page_no"] if existing else 0
        print(f"  已有 {len(existing)} 页记录{'（续跑模式）' if args.resume else ''}")

        # ---- 续跑定位：回到目录首项重走，已采页按 sha1 跳过 ----
        if args.resume and existing:
            print("\n▶ 续跑定位…")
            try:
                await page.click("button.readerControls_item.catalog", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(1200)
            await page.evaluate("""() => {
              const el = document.querySelectorAll('.readerCatalog_list_item')[0];
              if (el) { el.scrollIntoViewIfNeeded(); el.click(); }
            }""")
            await page.wait_for_timeout(3000)
            flips = 0
            while flips < 3000:
                sigs = await page.evaluate("""() => [...document.querySelectorAll('canvas')].map(c => {
                  try { return c.toDataURL('image/png'); } catch (e) { return null; }
                }).filter(Boolean).map(u => u.length + ':' + u.slice(-40))""")
                if sigs and any(s not in seen_hashes for s in sigs):
                    print(f"  已定位到断点（重走 {flips} 次翻页）")
                    break
                if not await flip_next(page):
                    break
                await page.wait_for_timeout(900)
                flips += 1
                if flips % 20 == 0:
                    print(f"    重走中… {flips} 次翻页")

        # ---- 主循环 ----
        print("\n▶ 开始采集\n")
        current_chapter = None
        current_index = -1
        captured = 0
        screen_idx = 0
        stuck = 0
        prev_sig = None

        while captured < args.max_pages:
            bad = await page.evaluate(JS_READY)
            if bad:
                print(f"  ⚠️ reader 未就绪: {', '.join(bad)}")
            await wait_stable(page)

            geo = await page.evaluate(JS_GEOMETRY)
            if not geo["canvases"]:
                print("  ✗ 没有 canvas，停止")
                break

            sig = await page.evaluate(JS_CONTENT_SIG)
            if prev_sig and sig == prev_sig:
                stuck += 1
                if stuck >= args.max_stuck:
                    print("  ⛔ 连续内容不变（书尾或翻页失效），停止")
                    break
                await page.wait_for_timeout(800)
                if not await flip_next(page):
                    break
                continue
            stuck = 0
            prev_sig = sig
            screen_idx += 1

            title = await page.evaluate(JS_CHAPTER)
            if title and title != current_chapter:
                current_chapter = title
                idx = next((i for i, t in enumerate(toc) if t["title"] == title), -1)
                if idx >= 0:
                    current_index = toc[idx]["index"]

            # ---- 截图：整页，裁掉顶部 header 与右侧工具栏 ----
            hb = geo["headerBottom"] if geo["headerBottom"] > 0 else 0
            clip = {"x": 0, "y": hb, "width": OUTPUT_W, "height": OUTPUT_H}
            buf = await page.screenshot(clip=clip, timeout=90000)

            h = sha1(buf)
            if h not in seen_hashes:
                seen_hashes.add(h)
                no = page_no + 1
                l1 = owner_l1(current_index) if current_index >= 0 else (l1_chapters[0] if l1_chapters else None)
                cdir_name = l1_dirname(l1) if l1 else "000_未命名"
                cdir = shots_dir / cdir_name
                cdir.mkdir(parents=True, exist_ok=True)
                rel_img = f"screenshots/{cdir_name}/page_{no:04d}.png"
                (out_dir / rel_img).write_bytes(buf)

                # 栏位 x 范围（图像像素坐标）：只记录，不落盘分栏图。
                # OCR 阶段按它临时裁剪分栏再识别，避免左右两栏同行被并成一行
                # （实测整视口双栏时 8.2% 的行跨栏，阅读序错乱）。
                # 栏位 x 范围 + 正文下边界（排除底部翻页按钮那一行）
                pager_top = geo.get("pagerTop") or clip["height"]
                pager_top = max(0, min(int(pager_top) - clip["y"], clip["height"]))
                columns = []
                for c in geo.get("visCanvases", []):
                    x0 = max(0, int(round(c["x"] - clip["x"])))
                    x1 = min(clip["width"], int(round(c["x"] + c["w"] - clip["x"])))
                    if x1 - x0 > 20:
                        columns.append([x0, x1])

                append_jsonl(pages_path, {
                    "page_no": no, "chapter_title": title, "chapter_index": current_index,
                    "chapter_l1": l1["title"] if l1 else None,
                    "screen_index": screen_idx, "sha1": h, "img": rel_img,
                    "columns": columns, "pager_top": pager_top, "clip": clip,
                })
                page_no = no
                captured += 1
                if captured % 20 == 0 or captured <= 3:
                    print(f"  📄 {no:4d}  {(title or '')[:28]:30s} {Path(rel_img).name}")

            if captured >= args.max_pages:
                break
            if not await flip_next(page):
                print("  ✗ 找不到下一页按钮，停止")
                break

        await browser.close()

    total = len(read_jsonl(pages_path))
    print(f"\n✅ 采集完成：本次 {captured} 页，累计 {total} 页")
    print(f"   截图: {shots_dir.relative_to(ROOT)}")
    print(f"   索引: {pages_path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-url", default=None)
    ap.add_argument("--state", default="state/storage_state_chrome.json")
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--max-stuck", type=int, default=4)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
