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

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import contextlib

from shared.paths import book_paths, resolve_auth_dir, resolve_out_dir
from shared.stages import record_stage

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
            with contextlib.suppress(Exception):
                out.append(json.loads(line))
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

# weread 偶发整屏渲染失败：页面中央显示「加载失败，请 点击重试」。
# 该状态下 canvas 内容恒定，会被「连续内容不变」误判成书尾而提前停止。
JS_LOAD_ERROR = """() => {
  const b = document.body;
  if (!b) return false;
  return (b.innerText || '').includes('加载失败');
}"""

JS_CLICK_RETRY = """() => {
  let n = 0;
  document.querySelectorAll('*').forEach(e => {
    if (e.children.length === 0 && (e.innerText || '').trim() === '点击重试') { e.click(); n++; }
  });
  return n;
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
        with contextlib.suppress(Exception):
            await page.click(".readerControls_item.isNormalReader", timeout=8000)
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
        sig = await _eval_safe(page, JS_CONTENT_SIG, None)
        if sig is None:
            # 页面正在刷新（点重试会整页 reload），上下文被销毁 → 重新开始这一轮
            last, same = None, 0
            await page.wait_for_timeout(1000)
            continue
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
    return bool(await _eval_safe(page, """() => {
      const b = document.querySelector('.renderTarget_pager_button_right')
             || [...document.querySelectorAll('.renderTarget_pager_button')].pop();
      if (b) { b.click(); return true; }
      return false;
    }""", False))


async def open_catalog(page) -> bool:
    """打开目录面板，返回面板是否真的可见。

    ⚠️ 点一下按钮不算数：某些 URL 形态（如带 #outline 片段）下点击会被
    吞掉，面板仍不可见 —— 后面 scroll_into_view 就会在 display:none 的
    元素上抛超时。所以这里点完必须**回读面板可见性**，不行就换方式再试。
    """
    async def visible() -> bool:
        return await page.evaluate("""() => {
          const p = document.querySelector('.readerCatalog');
          if (!p) return false;
          const r = p.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        }""")

    if await visible():
        return True

    for sel in ("button.readerControls_item.catalog", ".readerControls_item.catalog"):
        for force in (False, True):
            try:
                await page.click(sel, timeout=8000, force=force)
            except Exception:
                continue
            await page.wait_for_timeout(1500)
            if await visible():
                return True

    # 兜底：JS 派发点击（目录按钮本身没有「必须真实指针」的约束）
    with contextlib.suppress(Exception):
        await page.evaluate("""() => {
          const b = document.querySelector('button.readerControls_item.catalog')
                 || document.querySelector('.readerControls_item.catalog');
          if (b) b.click();
        }""")
        await page.wait_for_timeout(1500)
        if await visible():
            return True
    return False


async def jump_to_toc(page, title: str, wait_ms: int = 5000) -> bool:
    """点目录里的指定条目跳转。

    ⚠️ 必须用「真实点击」（Playwright click），不能用 JS 的 el.click()：
    阅读器把目录条目的跳转挂在真实指针事件上，脚本派发的 click 不会生效
    （实测 el.click() 点了也不跳，而真实点击能跳）。
    """
    if not await open_catalog(page):
        print("  ⚠️ 目录面板打不开")
        return False

    # innerText 可能带 '+书签' 等后缀，用「首行完全相等」匹配
    idx = await page.evaluate("""(t) => {
      const els = [...document.querySelectorAll('.readerCatalog_list_item')];
      const head = e => (e.innerText || '').trim().split('\\n')[0].trim();
      let i = els.findIndex(e => head(e) === t);
      if (i < 0) i = els.findIndex(e => head(e).startsWith(t));
      return i;
    }""", title)
    if idx < 0:
        print(f"  ⚠️ 目录里找不到条目「{title}」")
        return False
    try:
        loc = page.locator(".readerCatalog_list_item").nth(idx)
        try:
            await loc.scroll_into_view_if_needed(timeout=8000)
        except Exception:
            pass  # 面板开着但条目在滚动区外，force 点击照样能打中
        await loc.click(timeout=10000, force=True)
    except Exception as e:
        print(f"  ⚠️ 目录跳转「{title}」失败：{str(e)[:120]}")
        return False
    await page.wait_for_timeout(wait_ms)
    return True


async def read_chapter_title(page) -> str | None:
    return await page.evaluate("""() => {
      const t = document.querySelector('.renderTargetPageInfo_header_chapterTitle');
      return t ? (t.innerText || '').trim() || null : null;
    }""")


async def _eval_safe(page, js: str, default=None):
    """page.evaluate 的容错版。

    ⚠️ 点「点击重试」会让阅读器整页刷新 → 正在执行的 JS 上下文被销毁 →
    `page.evaluate` 直接抛 "Execution context was destroyed"，整个采集进程崩掉
    （实测在《Flutter App开发》第 178 页崩掉，白跑 5 分钟）。
    这里把这类异常吞掉并返回默认值，让调用方按「还没恢复」处理即可。
    """
    try:
        return await page.evaluate(js)
    except Exception:
        return default


async def recover_load_error(page, attempts: int = 4, wait_ms: int = 12000) -> bool:
    """整屏渲染失败（页面显示「加载失败，请 点击重试」）时点重试恢复。

    返回 True 表示已恢复（页面可正常截图）；False 表示多次重试仍失败。
    """
    for attempt in range(1, attempts + 1):
        if not await _eval_safe(page, JS_LOAD_ERROR, False):
            return True
        clicked = await _eval_safe(page, JS_CLICK_RETRY, False)
        if not clicked:
            # 没有重试按钮：等一会儿看阅读器是否自愈
            await page.wait_for_timeout(3000)
            continue
        print(f"  ♻️ 渲染失败页「第 {attempt} 次」点「点击重试」…")
        await page.wait_for_timeout(wait_ms)
        if not await _eval_safe(page, JS_LOAD_ERROR, False):
            print("  ♻️ 已恢复渲染 ✅")
            return True
    return not await _eval_safe(page, JS_LOAD_ERROR, False)


async def dump_stuck_state(page, state_dir) -> None:
    """卡住时留现场：截图 + 可见文本 + 关键 DOM 状态。

    用来区分三种「不前进」：① 真的到书尾 ② 翻页按钮失效 ③ 页面在报加载失败。
    """
    with contextlib.suppress(Exception):
        dbg = Path(state_dir) / "stuck_debug"
        dbg.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(dbg / "stuck.png"), timeout=20000)
        except Exception:
            pass
        info = await page.evaluate("""() => {
          const b = document.querySelector('.renderTarget_pager_button_right');
          const r = b ? b.getBoundingClientRect() : null;
          const t = document.querySelector('.renderTargetPageInfo_header_chapterTitle');
          return {
            chapter: t ? (t.innerText || '').trim() : null,
            canvases: document.querySelectorAll('canvas').length,
            nextBtn: r ? {w: r.width, h: r.height} : null,
            loadError: (document.body.innerText || '').includes('加载失败'),
            text: (document.body.innerText || '').slice(0, 1200),
          };
        }""")
        (dbg / "stuck.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
        print(f"  🔍 卡住现场 → {dbg}")


async def hard_recover(page, chapter_title: str | None) -> bool:
    """渲染失败反复出现时的硬恢复：整页重载 + 重新定位到当前章节。

    ⚠️ 必须走「重载」而不是单纯跳目录：跳目录需要目录面板可见，
    而主循环里目录面板被 CSS 隐藏了（display:none !important）→ 点不到。
    重载会连带清掉 add_style_tag 注入的隐藏样式，重新走一遍初始化即可。
    """
    print(f"  ♻️ 硬恢复：重载阅读器并跳回「{chapter_title}」…")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"    重载失败: {e}")
        return False
    await page.wait_for_timeout(6000)
    with contextlib.suppress(Exception):
        await page.wait_for_function('document.querySelectorAll("canvas").length > 0', timeout=25000)
    await page.wait_for_timeout(2500)

    # 视口高度 = 输出高 + header 高（与首次初始化保持一致，否则分页会错位）
    hb0 = await page.evaluate("""() => {
      const e = document.querySelector('.readerTopBar');
      if (!e) return 0;
      return Math.round(e.getBoundingClientRect().bottom);
    }""")
    if hb0 > 0:
        await page.set_viewport_size({"width": VIEWPORT["width"], "height": OUTPUT_H + hb0})
        await page.wait_for_timeout(1500)

    mode = await ensure_reading_mode(page)
    if not mode.get("horizontal"):
        print("    ❌ 重载后没能切到「上下滚动双栏」")
        return False

    if chapter_title:
        landed = False
        for _ in range(3):
            if await jump_to_toc(page, chapter_title):
                await page.wait_for_timeout(1500)
                now = await read_chapter_title(page)
                if now and (now == chapter_title or now.startswith(chapter_title)
                            or chapter_title.startswith(now)):
                    landed = True
                    break
            await page.wait_for_timeout(2000)
        if not landed:
            print(f"    ❌ 没能跳回章节「{chapter_title}」")
            return False

    await page.add_style_tag(content="""
        .readerCatalog, .readerCatalog_list, .wr_mask { display: none !important; }
    """)
    await page.wait_for_timeout(800)
    print("    ♻️ 硬恢复完成 ✅")
    return True


# --------------------------------------------------------------------------- 主流程

async def run(args) -> int:
    _t_capture = time.perf_counter()
    state_path = Path(args.state)
    out_dir = resolve_out_dir(args.out_dir)
    if not state_path.is_absolute():
        state_path = Path(state_path).expanduser().resolve()
    if not state_path.exists():
        print(f"❌ 找不到登录态 {state_path}", file=sys.stderr)
        return 1

    book_url = args.book_url or DEFAULT_BOOK_URL
    # ⚠️ 必须去掉 URL 片段（#outline?noScroll=1 之类）。
    # 带片段时 reader 会进入坏状态：卡在「书籍详情」页、目录面板打不开、
    # 「下一页」点了也不推进（canvas 内容恒定 → 被判成书尾 → 只采到 1 页就停）。
    clean_url = book_url.split("#")[0]
    if clean_url != book_url:
        print(f"  ↪ 去掉 URL 片段：{book_url} → {clean_url}")
        book_url = clean_url

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport=VIEWPORT, device_scale_factor=DSF,
                                        storage_state=json.loads(state_path.read_text("utf-8")))
        page = await ctx.new_page()

        # ⚠️ 60s 超时偶尔不够：微信读书首屏要拉一堆 chunk + 图片，
        # 冷启动时 domcontentloaded 可能迟迟不触发 → 直接 TimeoutError 退出。
        # 这里放宽到 180s，并在超时后重试一次（换 wait_until=commit 先把控制权拿回来）。
        try:
            await page.goto(book_url, wait_until="domcontentloaded", timeout=180000)
        except Exception as e:
            print(f"  ⚠️ 首次导航超时（{type(e).__name__}），重试一次…")
            with contextlib.suppress(Exception):
                await page.goto(book_url, wait_until="commit", timeout=120000)
            await page.wait_for_timeout(8000)
        await page.wait_for_timeout(6000)
        for sel in (".wr_dialog_closeButton", ".wr_dialog_mask"):
            with contextlib.suppress(Exception):
                await page.click(sel, timeout=1200)
            await page.wait_for_timeout(300)
        with contextlib.suppress(Exception):
            await page.wait_for_function('document.querySelectorAll("canvas").length > 0', timeout=25000)
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

        book_title = await page.evaluate("""() => {
          const t = document.querySelector('.readerTopBar_title, .readerTopBar_title_text');
          return (document.title.split(' - ')[0] || (t ? t.innerText.trim() : '') || '未命名').trim();
        }""")
        out_dir, state_dir = book_paths(out_dir, book_title)
        shots_dir = out_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        print(f"  书名: {book_title}")
        print(f"  视口: {VIEWPORT['width']}x{VIEWPORT['height']} @{DSF}x")

        # ---- 目录 ----
        print("\n▶ 抓取目录…")
        with contextlib.suppress(Exception):
            await page.click("button.readerControls_item.catalog", timeout=10000)
        await page.wait_for_timeout(2500)
        toc = await page.evaluate(JS_TOC)
        (out_dir / "toc.json").write_text(json.dumps(toc, ensure_ascii=False, indent=2), "utf-8")
        print(f"  {len(toc)} 个条目 → toc.json")

        # ---- 定位起点 ----
        # 阅读器会恢复「上次读到哪」，不显式跳转就可能从半本书中间开始采
        # （实测续跑时直接从 2.3 开始，导致 1.3/1.4/2.1/2.2 整段缺失）。
        # 默认跳到目录首项；--start-at 可指定从某个章节开始（补采用）。
        # ⚠️ 必须在目录面板「真实打开」时用真实点击跳转（见 jump_to_toc）。
        start_title = args.start_at or (toc[0]["title"] if toc else None)
        if start_title:
            # ⚠️ 阅读器会恢复「上次读到哪」。如果恢复的位置**就在目标章节内**，
            # 再点该章节的目录项是空操作 → 采集会从章节末尾而不是开头开始
            # （实测：目标 16.2、恢复到 16.2 末尾 → 一页没采到就判「翻页失效」）。
            # 所以先跳到目录首项（必然是一次真实跳转），再跳到目标章节。
            here = await read_chapter_title(page)
            if here and start_title and here == start_title and len(toc) > 1:
                first_title = toc[0]["title"]
                print(f"  ↪ 先跳到首项「{first_title}」制造一次真实跳转（当前已在「{here}」内）")
                for _ in range(2):
                    if await jump_to_toc(page, first_title):
                        await page.wait_for_timeout(2500)
                        break
            landed = False
            for attempt in range(3):
                jumped = await jump_to_toc(page, start_title)
                if jumped:
                    await page.wait_for_timeout(1500)
                    now = await read_chapter_title(page)
                    print(f"  起点「{start_title}」→ 当前章节 {now!r}")
                    # 点了不等于跳到了：还得确认真的落在目标章节上
                    if now and (now == start_title or now.startswith(start_title)
                                or start_title.startswith(now)):
                        landed = True
                        break
                    print(f"  ↻ 跳到了但章节不对（第 {attempt+1} 次），重试")
                await page.wait_for_timeout(2000)
            if not landed:
                print(f"  ⚠️ 没能跳到目录条目「{start_title}」，按当前位置继续")

        # 隐藏 chrome（目录面板点按钮关不掉，只能 CSS 隐藏）
        # —— 放在跳转之后：跳转需要目录面板可见、元素可点
        await page.add_style_tag(content="""
            .readerCatalog, .readerCatalog_list, .wr_mask { display: none !important; }
        """)
        await page.wait_for_timeout(800)

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
        # 内容指纹（canvas），用于续跑时定位断点；老记录没有这个字段时退化为「从头重走」
        seen_sigs = {p["sig"] for p in existing if p.get("sig")}
        page_no = existing[-1]["page_no"] if existing else 0
        print(f"  已有 {len(existing)} 页记录{'（续跑模式）' if args.resume else ''}")

        # ---- 续跑定位：从起点往后翻，直到遇到没采过的内容指纹 ----
        # 判定「这一屏是不是新的」要用 canvas 内容指纹（JS_CONTENT_SIG），
        # 不能拿截图的 sha1 去比 —— 两者根本不是一个东西，会导致第一轮就误判成断点。
        if args.resume and existing:
            print("\n▶ 续跑定位…")
            flips = 0
            while flips < 3000:
                sig = await page.evaluate(JS_CONTENT_SIG)
                if sig and sig not in seen_sigs:
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
        load_err_streak = 0
        hard_recoveries = 0

        while captured < args.max_pages:
            bad = await _eval_safe(page, JS_READY, [])
            if bad:
                print(f"  ⚠️ reader 未就绪: {', '.join(bad)}")
            await wait_stable(page)

            # 整屏渲染失败要先恢复，否则会截到一张空白错误页，
            # 而且 canvas 内容恒定 → 被判成「连续内容不变」→ 误当成书尾停下。
            if await _eval_safe(page, JS_LOAD_ERROR, False):
                # ⚠️ recover_load_error 报「已恢复」只代表错误文字暂时消失；
                # 若 chunk 本身加载不了（服务端报错），点完「点击重试」错误立刻复现
                # → 无限循环：既不截图也不前进（实测卡在中途 16 分钟不动）。
                # 所以要计连续次数：超过阈值就整页重载硬恢复，恢复不了才停。
                load_err_streak += 1
                if load_err_streak <= 3:
                    if not await recover_load_error(page):
                        print("  ⛔ 渲染失败且重试无效，停止")
                        break
                    await wait_stable(page)
                    prev_sig = None  # 恢复后内容变了，重新判定
                    continue
                print(f"  ♻️ 渲染失败连续 {load_err_streak} 次，尝试硬恢复（重载 + 跳回当前章节）…")
                hard_recoveries += 1
                if hard_recoveries > 2:
                    print("  ⛔ 硬恢复次数用尽，停止")
                    break
                if not await hard_recover(page, current_chapter):
                    print("  ⛔ 硬恢复失败，停止")
                    break
                load_err_streak = 0
                await wait_stable(page)
                prev_sig = None
                continue
            # ⚠️ 这里**不能**把 load_err_streak 清零。
            # 坏 chunk 的典型表现是「加载失败 → 点重试 → 错误文字消失 → 又失败」，
            # 每次循环开头错误都是「暂时消失」的，若在这里清零，连续失败计数永远到不了 3，
            # 硬恢复分支根本不触发 → 永远在同一个坏 chunk 上打转、页数不涨
            # （实测《Flutter App开发》卡在 561 页反复空转 15 分钟）。
            # 正确做法：只在**真的采到新页**时才清零（见下方截图成功分支）。

            geo = await _eval_safe(page, JS_GEOMETRY, None)
            if not geo or not geo.get("canvases"):
                print("  ✗ 没有 canvas，停止")
                break

            sig = await _eval_safe(page, JS_CONTENT_SIG, None)
            if prev_sig and sig is not None and sig == prev_sig:
                # 内容没变有三种可能：① 到书尾了 ② 阅读器还在加载下一屏（chunk 慢）
                # ③ 翻页真的失效。
                # ⚠️ 实测 ② 要 20~40 秒才出结果：原来的「等 6 秒 + 4 次重试（≈30 秒）」
                # 太急，会把「还在加载」误判成「翻页失效」而中途停采
                # （实测《Flutter技术入门与实战》第 16 章因此停在 611 页不动）。
                # 这里改成渐进等待：10s → 20s → 30s → 45s，累计约 105 秒，
                # 每次等待之间重新点一次翻页，逼它重新发起 chunk 请求。
                recovered = False
                for wait_s in (10, 20, 30, 45):
                    await page.wait_for_timeout(wait_s * 1000)
                    sig2 = await _eval_safe(page, JS_CONTENT_SIG, None)
                    if sig2 is not None and sig2 != prev_sig:
                        # ⚠️ 这里绝不能更新 prev_sig：循环顶部是 `if prev_sig and sig == prev_sig`
                        # 才进入等待分支，一旦把 prev_sig 改成当前内容，continue 回去就又满足
                        # 「sig == prev_sig」，重新进入等待分支 —— 于是永远走不到下面的截图逻辑，
                        # 表现是「日志一直刷『等待 Ns 后内容继续更新』但页数不涨」（实测《Flutter
                        # App开发》卡在 228 页）。正确做法是直接落到截图：内容已经变了，
                        # 让本轮继续往下走，重新取一次 sig 再截图。
                        stuck = 0
                        recovered = True
                        print(f"  ↻ 等待 {wait_s}s 后内容继续更新（chunk 加载慢，非失效）")
                        break
                    await flip_next(page)
                if recovered:
                    continue
                # 「下一页」按钮消失 = 真的到书尾了；按钮还在但内容不变 = 翻页失效。
                has_next = await _eval_safe(page, """() => {
                  const b = document.querySelector('.renderTarget_pager_button_right');
                  if (!b) return false;
                  const r = b.getBoundingClientRect();
                  return r.width > 0 && r.height > 0;
                }""", False)
                if not has_next:
                    print("  ⛔ 已到书尾（下一页按钮消失），停止")
                    break
                stuck += 1
                if stuck >= args.max_stuck:
                    print("  ⛔ 连续内容不变（书尾或翻页失效），停止")
                    await dump_stuck_state(page, state_dir)
                    break
                await page.wait_for_timeout(800)
                if not await flip_next(page):
                    break
                continue
            stuck = 0
            prev_sig = sig
            screen_idx += 1

            title = await _eval_safe(page, JS_CHAPTER, None)
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
                    x0 = max(0, round(c["x"] - clip["x"]))
                    x1 = min(clip["width"], round(c["x"] + c["w"] - clip["x"]))
                    if x1 - x0 > 20:
                        columns.append([x0, x1])

                append_jsonl(pages_path, {
                    "page_no": no, "chapter_title": title, "chapter_index": current_index,
                    "chapter_l1": l1["title"] if l1 else None,
                    "screen_index": screen_idx, "sha1": h, "img": rel_img,
                    "sig": sig,
                    "columns": columns, "pager_top": pager_top, "clip": clip,
                })
                page_no = no
                captured += 1
                load_err_streak = 0  # 真的采到新页了，才算从坏 chunk 里走出来
                if captured % 20 == 0 or captured <= 3:
                    print(f"  📄 {no:4d}  {(title or '')[:28]:30s} {Path(rel_img).name}")

            if captured >= args.max_pages:
                break
            if not await flip_next(page):
                print("  ✗ 找不到下一页按钮，停止")
                break

        await browser.close()

    total = len(read_jsonl(pages_path))
    record_stage(state_dir, "capture", time.perf_counter() - _t_capture,
                 pages=captured, total_pages=total)
    print(f"\n✅ 采集完成：本次 {captured} 页，累计 {total} 页")
    print(f"   截图: {shots_dir}")
    print(f"   索引: {pages_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-url", default=None)
    ap.add_argument("--state", default=None,
                    help="登录态文件（默认 <auth-dir>/storage_state.json）")
    ap.add_argument("--max-pages", type=int, default=100000)
    ap.add_argument("--start-at", default=None,
                    help="从目录里这个条目开始采（默认目录首项）；补采某章时用")
    ap.add_argument("--max-stuck", type=int, default=4)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out-dir", default=None,
                    help="产物根目录（默认 $PWD/reading-output，可用 READING_AGENT_OUT 覆盖）")
    ap.add_argument("--auth-dir", default=None,
                    help="登录态目录（默认 ~/.reading-agent，可用 READING_AGENT_AUTH 覆盖）")
    args = ap.parse_args()
    args.state = str(Path(args.state).expanduser()) if args.state \
        else str(resolve_auth_dir(args.auth_dir) / "storage_state.json")
    args.out_dir = resolve_out_dir(args.out_dir)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
