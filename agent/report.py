#!/usr/bin/env python3
"""
生成 HTML 报告 —— 采集 → OCR → 插图 → 精读 → 评测 的全景。

特点：
  · **可离线重建**：只读既有产物 + .state 里的埋点，不重跑流水线
    （重跑一次要 1 小时，报告必须能单独重生成）
  · **图表库本地化**：ECharts 下载到 `<out>/assets/echarts.min.js`，
    报告用相对路径引用 → 不依赖 CDN，离线可开
  · **单文件可选**：`--inline` 把 echarts 内嵌进 HTML，得到真正单文件

内容：各阶段耗时 / token 消耗 / 诊断报告 / 质量闸门 / 逐章一览 /
      插图清单 / 全书评测（含逐章笔记全文）/ 环境与产物清单

用法：
    python3 agent/report.py --book 时势
    python3 agent/report.py --book 时势 --out-dir ~/Documents/读书
    python3 agent/report.py --book 时势 --inline        # 单文件
    python3 agent/report.py --book 时势 --no-echarts    # 纯 CSS 条形，不依赖图表库
"""

from __future__ import annotations

import argparse
import html
import json
import os
import platform
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.stages import aggregate_usage, load_stages, load_usage
from paths import book_paths, list_books, resolve_out_dir

ECHARTS_URL = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"
ECHARTS_REL = "assets/echarts.min.js"   # 相对 <out>/；报告在 <out>/<书名>/ 下，引用时用 relpath

STAGE_ORDER = [
    ("capture", "采集截图"),
    ("ocr", "分页 OCR"),
    ("figures", "插图识别"),
    ("load", "清洗与切分"),
    ("summarize", "逐章总结"),
    ("diagnose", "诊断恢复"),
    ("gate", "人工闸门"),
    ("review", "全书评测"),
    ("render", "渲染产物"),
]

LABEL_CN = {"summary": "逐章总结", "review": "全书评测", "figure": "插图识别",
            "diagnose": "诊断恢复", "unknown": "未分类"}


# --------------------------------------------------------------------------- 资源

def ensure_echarts(out_dir: Path) -> Path | None:
    """把 ECharts 下载到本地（缓存）。返回本地路径；失败返回 None。"""
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / "echarts.min.js"
    if target.exists() and target.stat().st_size > 100_000:
        return target
    try:
        print(f"▶ 下载 ECharts 到本地 → {target}")
        with urllib.request.urlopen(ECHARTS_URL, timeout=60) as r:
            target.write_bytes(r.read())
        return target
    except Exception as e:
        print(f"  ⚠️ 下载失败（报告将退回纯 CSS 条形）：{e}")
        return None


# --------------------------------------------------------------------------- 采集数据

_DIM_KEYS = {
    "逻辑结构性": "score_logic", "内容干货密度": "score_density",
    "观点创新度": "score_innovation", "落地实用性": "score_practical",
    "阅读启发价值": "score_inspire",
}


def parse_review_md(md: str) -> dict[str, Any]:
    """从 book_review.md 反解析评测结果（老版本只写了 md，没写 json）。"""
    import re
    out: dict[str, Any] = {}

    m = re.search(r"##\s*一、书籍整体定位\s*\n+(.+?)\n##", md, re.S)
    if m:
        out["positioning"] = m.group(1).strip()

    # 五维评分表
    for line in md.splitlines():
        mm = re.match(r"\|\s*([^|*]+?)\s*\|\s*([\d.]+)\s*\|\s*(.*?)\s*\|?\s*$", line)
        if not mm:
            continue
        name, score, reason = mm.group(1), mm.group(2), mm.group(3)
        if name in _DIM_KEYS:
            out.setdefault(_DIM_KEYS[name], {"score": float(score), "reason": reason})
        elif "综合平均" in name:
            out["avg_score"] = float(score)
        elif "书籍评级" in name:
            out["level"] = score.strip("* ")

    def section(title: str) -> str:
        m = re.search(rf"##\s*{title}\s*\n(.*?)(?=\n##|\Z)", md, re.S)
        return m.group(1) if m else ""

    def numbered(text: str) -> list[str]:
        return [re.sub(r"^\d+\.\s*", "", x).strip()
                for x in text.splitlines() if re.match(r"^\s*\d+\.", x)]

    def bullets(text: str) -> list[str]:
        return [x.strip("- ").strip() for x in text.splitlines() if x.strip().startswith("- ")]

    out["strengths"] = numbered(section("三、本书核心优势"))
    out["weaknesses"] = numbered(section("四、本书短板与局限性"))
    sec5 = section("五、适合人群")
    parts = re.split(r"\*\*不适合\*\*", sec5)
    out["suitable_for"] = bullets(parts[0]) if parts else []
    out["not_suitable_for"] = bullets(parts[1]) if len(parts) > 1 else []
    out["reading_advice"] = numbered(section("六、高效阅读建议"))

    if "scores" not in out:
        scores = {}
        for cn, key in _DIM_KEYS.items():
            d = out.get(key) or {}
            if isinstance(d.get("score"), (int, float)):
                scores[cn] = d["score"]
        if scores:
            out["scores"] = scores
    if "avg_score" not in out and out.get("scores"):
        out["avg_score"] = round(sum(out["scores"].values()) / len(out["scores"]), 2)
    if "level" not in out and isinstance(out.get("avg_score"), (int, float)):
        a = out["avg_score"]
        out["level"] = ("神作" if a >= 9 else "优质好书" if a >= 7.5 else
                        "值得一读" if a >= 6 else "中规中矩" if a >= 4.5 else "价值较低")
    return out


def collect(book_dir: Path, state_dir: Path) -> dict[str, Any]:
    """把散落在各处的产物汇成一份 report.json 数据。"""
    def jread(p: Path, default: Any = None) -> Any:
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return default

    toc = jread(book_dir / "toc.json", [])
    quality = jread(book_dir / "quality_report.json", {})
    diagnosis = jread(book_dir / "diagnosis.json", {})
    figures = jread(book_dir / "figures" / "figures.json", [])
    review = jread(book_dir / "book_review.json", {})
    if not review:
        md = book_dir / "book_review.md"
        if md.exists():
            review = parse_review_md(md.read_text("utf-8"))

    # 逐章
    chapters: list[dict] = []
    ch_dir = book_dir / "chapters"
    if ch_dir.exists():
        for f in sorted(ch_dir.glob("*.json")):
            d = jread(f, {})
            if d:
                chapters.append(d)

    stages = load_stages(state_dir)
    usage = aggregate_usage(load_usage(state_dir))

    pages = 0
    total_chars = 0
    pj = state_dir / "pages.jsonl"
    if pj.exists():
        for line in pj.read_text("utf-8").splitlines():
            if line.strip():
                pages += 1

    for ch in chapters:
        s = ch.get("summary") or {}
        ch["_counts"] = {
            "key_points": len(s.get("key_points") or []),
            "concepts": len(s.get("concepts") or []),
            "cases": len(s.get("cases") or []),
            "data_points": len(s.get("data_points") or []),
            "open_questions": len(s.get("open_questions") or []),
        }
        total_chars += len(s.get("core_thesis") or "")

    figs_by_page: dict[int, int] = {}
    for f in figures:
        figs_by_page[f.get("page_no", -1)] = figs_by_page.get(f.get("page_no", -1), 0) + 1
    for ch in chapters:
        ch["_figures"] = sum(figs_by_page.get(n, 0)
                              for n in range(ch.get("start_page", 0), ch.get("end_page", 0) + 1))

    total_seconds = sum(float(v.get("seconds", 0)) for v in stages.values())
    tok_in = sum(v.get("input_tokens", 0) for v in usage.values())
    tok_out = sum(v.get("output_tokens", 0) for v in usage.values())
    tok_calls = sum(v.get("calls", 0) for v in usage.values())

    return {
        "book": {
            "title": book_dir.name,
            "pages": pages,
            "chapters": len(chapters),
            "toc_items": len(toc),
            "figures": len(figures),
            "ocr_chars": quality.get("total_chars", 0),
            "avg_chars_per_page": quality.get("avg_chars_per_page", 0),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "review": review,
        "quality": quality,
        "diagnosis": diagnosis,
        "figures": figures,
        "chapters": chapters,
        "stages": stages,
        "stage_order": [s for s, _ in STAGE_ORDER],
        "usage": usage,
        "totals": {
            "seconds": round(total_seconds, 1),
            "input_tokens": tok_in,
            "output_tokens": tok_out,
            "calls": tok_calls,
        },
        "env": {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "model": next((v.get("model") for v in usage.values() if v.get("model")), ""),
            "viewport": "1280×815 @1x",
            "ocr_engine": "macOS Vision (rapidocr 弃用)",
        },
    }


# --------------------------------------------------------------------------- 渲染

def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def bar(pct: float, cls: str = "bar-fill") -> str:
    pct = max(0.0, min(100.0, pct))
    return f'<div class="bar"><div class="{cls}" style="width:{pct:.1f}%"></div></div>'


def render(d: dict[str, Any], echarts_src: str | None) -> str:
    """渲染 HTML 报告。

    包含 5 节：概览 / 五维评分 / 全书评测 / 逐章精读笔记全文 / 附录。
    """
    b = d["book"]
    rev = d.get("review") or {}
    q = d.get("quality") or {}
    chapters = d.get("chapters") or []
    tot = d.get("totals") or {}

    # ---------- KPI ----------
    scores = rev.get("scores") or {}
    avg = rev.get("avg_score")
    level = rev.get("level")
    kpis = [
        ("综合评分", f"{avg:.2f}" if isinstance(avg, (int, float)) else "—", level or ""),
        ("章节", str(b["chapters"]), f"目录 {b['toc_items']} 项"),
        ("页数", str(b["pages"]),
         f"平均 {b['avg_chars_per_page']:.0f} 字/页" if b["avg_chars_per_page"] else ""),
        ("总字数", f"{b['ocr_chars']:,}", "OCR 识别"),
        ("OCR 覆盖率", f"{q.get('coverage', 0) * 100:.1f}%" if q else "—",
         f"置信 {q.get('mean_conf', 0):.3f}" if q else ""),
        ("插图", str(b["figures"]), "已识别"),
    ]
    # 耗时 / Token 只在有埋点数据时展示（没有数据时给 0 会误导）
    if tot.get("seconds"):
        kpis.append(("总耗时", f"{tot['seconds'] / 60:.1f} min", "全流程"))
    if tot.get("calls"):
        kpis.append(("Token", f"{tot['input_tokens'] + tot['output_tokens']:,}",
                     f"in {tot['input_tokens']:,} / out {tot['output_tokens']:,}"))
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{esc(k)}</div>'
        f'<div class="kpi-value">{esc(v)}</div>'
        f'<div class="kpi-sub">{esc(s)}</div></div>'
        for k, v, s in kpis)

    # ---------- 五维评分 ----------
    dims = []
    for cn in ("逻辑结构性", "内容干货密度", "观点创新度", "落地实用性", "阅读启发价值"):
        v = scores.get(cn)
        if isinstance(v, (int, float)):
            dims.append((cn, float(v)))
    dims_html = "".join(
        f'<div class="dim"><div class="dim-name">{esc(n)}</div>'
        f'{bar(v * 10)}<div class="dim-score">{v:.1f}</div></div>'
        for n, v in dims)
    radar = json.dumps([{"name": n, "value": v} for n, v in dims], ensure_ascii=False)

    # ---------- 逐章笔记全文 ----------
    notes_html = []
    for ch in chapters:
        s = ch.get("summary") or {}

        def ul(items: list[str]) -> str:
            items = items or []
            if not items:
                return "<p class='muted'>—</p>"
            return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>"

        notes_html.append(f"""
<details class="chapter-note">
  <summary><span class="cn-title">{esc(ch.get('title'))}</span>
  <span class="cn-meta">第 {ch.get('start_page')}–{ch.get('end_page')} 页 · {ch.get('chars', 0):,} 字 · 图 {ch.get('_figures', 0)}</span></summary>
  <div class="cn-body">
    <h5>核心主旨</h5><p>{esc(s.get('core_thesis'))}</p>
    <h5>关键要点</h5>{ul(s.get('key_points'))}
    <h5>重要概念 / 术语</h5>{ul(s.get('concepts'))}
    <h5>案例论据</h5>{ul(s.get('cases'))}
    <h5>关键数据</h5>{ul(s.get('data_points'))}
    <h5>我的思考</h5><p>{esc(s.get('reflections'))}</p>
    <h5>待验证疑问</h5>{ul(s.get('open_questions'))}
  </div>
</details>""")

    # ---------- 全书评测 ----------
    def lst(items: list[str]) -> str:
        items = items or []
        if not items:
            return "<p class='muted'>—</p>"
        return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>"

    review_html = ""
    if rev:
        dims_detail = []
        for key, cn in (("score_logic", "逻辑结构性"), ("score_density", "内容干货密度"),
                         ("score_innovation", "观点创新度"), ("score_practical", "落地实用性"),
                         ("score_inspire", "阅读启发价值")):
            dd = rev.get(key) or {}
            dims_detail.append(
                f'<div class="rd"><div class="rd-head"><b>{cn}</b>'
                f'<span class="rd-score">{dd.get("score", 0):.1f}</span></div>'
                f'<p>{esc(dd.get("reason"))}</p></div>')
        review_html = f"""
<section id="review">
  <h2>三、全书评测</h2>
  <div class="kv"><b>整体定位</b><p>{esc(rev.get('positioning'))}</p></div>
  <div class="rd-list">{''.join(dims_detail)}</div>
  <div class="two-col">
    <div><h4>核心优势</h4>{lst(rev.get('strengths'))}</div>
    <div><h4>短板与局限</h4>{lst(rev.get('weaknesses'))}</div>
  </div>
  <div class="two-col">
    <div><h4>适合人群</h4>{lst(rev.get('suitable_for'))}</div>
    <div><h4>不适合人群</h4>{lst(rev.get('not_suitable_for'))}</div>
  </div>
  <div><h4>高效阅读建议</h4>{lst(rev.get('reading_advice'))}</div>
</section>"""

    # ---------- 附录 ----------
    env = d.get("env") or {}
    env_html = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in env.items())
    artifacts = [
        ("chapter_summaries.md", "逐章精读笔记（Markdown）"),
        ("book_review.md", "全书五维评测（Markdown）"),
        ("full_readnote.md", "整合版读书资产"),
        ("full_text.txt", "清洗后全文"),
        ("chapters/", "逐章结构化 JSON"),
        ("screenshots/", "逐页整页截图 1280×815"),
        ("ocr_pages/", "分页 OCR 文本"),
        ("figures/figures.json", "插图记录与视觉描述"),
        ("quality_report.json", "OCR 质量闸门"),
        ("diagnosis.md", "诊断与恢复报告"),
    ]
    art_html = "".join(f"<tr><td><code>{esc(a)}</code></td><td>{esc(desc)}</td></tr>"
                       for a, desc in artifacts)

    echarts_tag = (f'<script src="{esc(echarts_src)}"></script>' if echarts_src else "")
    chart_js = """
<script>
(function(){
  if (typeof echarts === 'undefined') {
    document.querySelectorAll('.nochart').forEach(function(e){ e.style.display='none'; });
    return;
  }
  var dark = matchMedia('(prefers-color-scheme: dark)').matches;
  var axis = { axisLine:{lineStyle:{color:dark?'#3a4250':'#d5dae2'}},
               axisLabel:{color:dark?'#9aa5b4':'#5a6472', fontSize:11},
               splitLine:{lineStyle:{color:dark?'#262c36':'#eef1f5'}} };
  var el = document.getElementById('radar');
  if (!el) return;
  var r = echarts.init(el, null, {renderer:'canvas'});
  r.setOption({
    tooltip:{},
    radar:{ indicator: RADAR.map(function(x){return {name:x.name, max:10};}),
      axisName:{color: dark?'#9aa5b4':'#5a6472', fontSize:12},
      splitArea:{areaStyle:{color: dark?['rgba(255,255,255,.03)','rgba(255,255,255,.06)']:['#fafbfc','#f2f5f9']}},
      splitLine:{lineStyle:{color: dark?'#3a4250':'#d5dae2'}},
      axisLine:{lineStyle:{color: dark?'#3a4250':'#d5dae2'}} },
    series:[{ type:'radar', data:[{value: RADAR.map(function(x){return x.value;}), name:'评分',
      areaStyle:{color:'rgba(74,158,255,.28)'}, lineStyle:{color:'#4a9eff'},
      itemStyle:{color:'#4a9eff'}}]}] });
  addEventListener('resize', function(){ r.resize(); });
})();
</script>
"""
    css = """
:root{
  --bg:#f6f8fb; --panel:#ffffff; --fg:#1b2430; --muted:#68748a; --line:#e4e9f0;
  --accent:#2f6fd0; --ok:#1f9d55; --bad:#d24b4b; --warn:#c07a12;
  --code:#0f172a; --codefg:#e2e8f0;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0f141c; --panel:#161d27; --fg:#e6edf6; --muted:#93a1b5; --line:#26303d;
         --accent:#5aa2ff; --ok:#3ddc84; --bad:#ff6b6b; --warn:#ffb454;
         --code:#0b1017; --codefg:#dbe4f0; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Segoe UI,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px 80px}
header.top{background:linear-gradient(135deg,var(--accent),#7b5cf0);color:#fff;padding:34px 0 26px;margin-bottom:26px}
header.top .wrap{padding-bottom:0}
header.top h1{margin:0 0 6px;font-size:25px;letter-spacing:.3px}
header.top .sub{opacity:.92;font-size:13px}
nav.toc{position:sticky;top:0;z-index:20;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:10px 14px;margin-bottom:26px;display:flex;gap:16px;flex-wrap:wrap;font-size:13px}
nav.toc a{color:var(--muted);text-decoration:none;padding:3px 8px;border-radius:7px}
nav.toc a:hover{color:var(--accent);background:rgba(47,111,208,.09)}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:22px}
h2{font-size:18px;margin:0 0 16px;padding-bottom:10px;border-bottom:1px solid var(--line)}
h4{margin:16px 0 6px;font-size:14px;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--bg);border:1px solid var(--line);border-radius:11px;padding:13px 15px}
.kpi-label{font-size:12px;color:var(--muted)}
.kpi-value{font-size:23px;font-weight:650;margin:2px 0;letter-spacing:.5px}
.kpi-sub{font-size:11.5px;color:var(--muted)}
.dim{display:grid;grid-template-columns:110px 1fr 46px;align-items:center;gap:12px;margin:9px 0}
.dim-name{font-size:13.5px}
.dim-score{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.bar{height:9px;background:rgba(127,140,160,.16);border-radius:5px;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),#7b5cf0);border-radius:5px}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th,.tbl td{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.tbl th{color:var(--muted);font-weight:600;font-size:12px}
.tbl td.num,.tbl th.num{text-align:right;font-variant-numeric:tabular-nums}
.tbl tbody tr:hover{background:rgba(47,111,208,.05)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.muted{color:var(--muted)}
.kv b{display:block;margin-bottom:4px;font-size:13px;color:var(--muted)}
.rd{border-left:3px solid var(--accent);padding:2px 0 2px 13px;margin:11px 0}
.rd-head{display:flex;justify-content:space-between;align-items:baseline}
.rd-score{font-weight:650;font-size:16px;color:var(--accent)}
.rd p{margin:3px 0 0;font-size:13.5px;color:var(--fg);opacity:.9}
.chapter-note{border:1px solid var(--line);border-radius:10px;margin:8px 0;background:var(--bg)}
.chapter-note>summary{cursor:pointer;padding:10px 14px;list-style:none;display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.chapter-note>summary::-webkit-details-marker{display:none}
.cn-title{font-weight:600}
.cn-meta{font-size:11.5px;color:var(--muted)}
.cn-body{padding:2px 16px 14px;border-top:1px solid var(--line)}
.cn-body h5{margin:13px 0 4px;font-size:12.5px;color:var(--muted);letter-spacing:.4px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.chart{height:320px;background:var(--bg);border:1px solid var(--line);border-radius:11px}
code{background:var(--code);color:var(--codefg);padding:1px 6px;border-radius:5px;font-size:12px}
footer{text-align:center;color:var(--muted);font-size:12px;padding:26px 0}
@media (max-width:820px){ .two-col,.charts{grid-template-columns:1fr} }
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>《{esc(b['title'])}》精读报告</title>
<style>{css}</style>
{echarts_tag}
</head><body>
<header class="top"><div class="wrap">
  <h1>《{esc(b['title'])}》精读报告</h1>
  <div class="sub">生成于 {esc(b['generated_at'])} · {esc(b['pages'])} 页 · {esc(b['chapters'])} 章 ·
    {esc(b['figures'])} 张插图</div>
</div></header>
<div class="wrap">

<nav class="toc">
  <a href="#overview">概览</a><a href="#scores">五维评分</a>
  <a href="#review">全书评测</a><a href="#notes">逐章笔记</a><a href="#appendix">附录</a>
</nav>

<section id="overview">
  <h2>一、概览</h2>
  <div class="kpis">{kpi_html}</div>
</section>

<section id="scores">
  <h2>二、五维评分</h2>
  <div class="charts">
    <div><div class="dim-list">{dims_html}</div></div>
    <div id="radar" class="chart nochart"></div>
  </div>
</section>

{review_html}

<section id="notes">
  <h2>四、逐章精读笔记全文</h2>
  {''.join(notes_html)}
</section>

<section id="appendix">
  <h2>五、附录</h2>
  <div class="two-col">
    <div><h4>环境</h4><table class="tbl"><tbody>{env_html}</tbody></table></div>
    <div><h4>产物清单</h4><table class="tbl"><tbody>{art_html}</tbody></table></div>
  </div>
  <h4>数据来源</h4>
  <p class="muted" style="font-size:13px">
    本报告由 <code>agent/report.py</code> 从既有产物离线生成，不重新执行流水线：
    逐章数据来自 <code>chapters/*.json</code>，评测来自 <code>book_review.json</code>，
    插图来自 <code>figures/figures.json</code>，质量来自 <code>quality_report.json</code>，
    耗时与 Token 来自 <code>.state/stages.json</code> 与 <code>.state/usage.jsonl</code>。
  </p>
</section>

<footer>由 微信读书全书 OCR 总结 Agent 生成 · {esc(b['generated_at'])}</footer>
</div>
<script>
var RADAR = {radar};
</script>
{chart_js}
</body></html>"""


# --------------------------------------------------------------------------- 生成

def generate(book_dir: Path, state_dir: Path, out_dir_root: Path | None = None,
             inline: bool = False, no_echarts: bool = False) -> Path:
    """生成 report.html（+ report.json），返回 HTML 路径。

    可离线重建：只读既有产物，不重跑流水线。
    """
    out_dir = out_dir_root or book_dir.parent
    data = collect(book_dir, state_dir)

    echarts_src = None
    if not no_echarts:
        local = ensure_echarts(out_dir)
        if local:
            echarts_src = os.path.relpath(local, book_dir)

    html_text = render(data, echarts_src)

    if inline and echarts_src:
        js_path = out_dir / ECHARTS_REL
        if js_path.exists():
            js = js_path.read_text("utf-8")
            html_text = html_text.replace(f'<script src="{echarts_src}"></script>', "")
            html_text = html_text.replace("<script>var RADAR",
                                          f"<script>{js}</script>\n<script>var RADAR")

    html_path = book_dir / "report.html"
    html_path.write_text(html_text, "utf-8")
    (book_dir / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return html_path


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="书名关键字")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--inline", action="store_true", help="把 echarts 内嵌进 HTML（单文件）")
    ap.add_argument("--no-echarts", action="store_true", help="不用图表库，纯 CSS 条形")
    args = ap.parse_args()

    out_dir = resolve_out_dir(args.out_dir)
    books = [b for b in list_books(out_dir) if args.book in b.name]
    if not books:
        print(f"❌ 在 {out_dir} 里找不到包含「{args.book}」的书")
        return 1
    book_dir = books[0]
    _, state_dir = book_paths(out_dir, book_dir.name)

    html_path = generate(book_dir, state_dir, out_dir_root=out_dir,
                         inline=args.inline, no_echarts=args.no_echarts)
    size = html_path.stat().st_size
    print(f"✅ 报告 → {html_path}  ({size / 1024:.0f} KB)")
    if not args.no_echarts:
        print(f"   图表库：本地 {out_dir / ECHARTS_REL}（离线可用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
