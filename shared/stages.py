#!/usr/bin/env python3
"""阶段计时与 token 台账 —— 供 HTML 报告使用。

每个阶段结束后把耗时与附加信息写进 `<book>/.state/stages.json`，
LLM 每次调用的 token 追加进 `<book>/.state/usage.jsonl`。
跨进程（capture / ocr 是独立进程，图节点在同进程）都能记。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def stages_path(state_dir: Path) -> Path:
    return Path(state_dir) / "stages.json"


def usage_path(state_dir: Path) -> Path:
    return Path(state_dir) / "usage.jsonl"


def record_stage(state_dir: Path, name: str, seconds: float, **extra: Any) -> None:
    """记录一个阶段的耗时。同名阶段覆盖（重跑时更新）。"""
    p = stages_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
        except Exception:
            data = {}
    data[name] = {
        "seconds": round(float(seconds), 2),
        "at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def load_stages(state_dir: Path) -> dict[str, Any]:
    p = stages_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def load_usage(state_dir: Path) -> list[dict[str, Any]]:
    """读 token 台账。"""
    p = usage_path(state_dir)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def aggregate_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按 label 汇总 token。台账里每行是「该 label 到此为止的累计值」，取每组最后一行即可。"""
    last: dict[str, dict[str, Any]] = {}
    for r in rows:
        label = r.get("label", "unknown")
        last[label] = r          # 后写的覆盖先写的（累计值）
    out = {}
    for label, r in last.items():
        out[label] = {
            "calls": r.get("calls", 0),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "cache_read_tokens": r.get("cache_read_tokens", 0),
            "cache_write_tokens": r.get("cache_write_tokens", 0),
            "model": r.get("model", ""),
        }
    return out


def export_usage_log(state_dir: Path) -> None:
    """把台账路径放进环境变量，让各阶段的 LLM 实例都写同一个文件。"""
    os.environ["READING_AGENT_USAGE_LOG"] = str(usage_path(state_dir))


class Timer:
    """上下文管理器：测一段代码的耗时。"""

    def __init__(self) -> None:
        self.seconds = 0.0
        self._t0 = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.seconds = time.perf_counter() - self._t0
