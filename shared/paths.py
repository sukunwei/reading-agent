#!/usr/bin/env python3
"""路径解析 —— 产物目录由**用户指定**，不在 agent 内部创建。

约定：
  · 产物根目录   `--out-dir` > 环境变量 `READING_AGENT_OUT` > `$PWD/reading-output`
                  （默认在当前工作目录下，不落在 agent/plugin 目录里）
  · 登录态目录   `--auth-dir` > 环境变量 `READING_AGENT_AUTH` > `~/.reading-agent`
                  （凭据单独放，不跟书产物混在一起）

目录结构（自包含、可整体搬走）：
  <out-dir>/<书名>/
    ├── screenshots/  ocr_pages/  figures/  chapters/
    ├── chapter_summaries.md  book_review.md  full_readnote.md  full_text.txt
    ├── quality_report.json   diagnosis.md  diagnosis.json  chapters.json
    └── .state/     pages.jsonl  run_config.json  *.ckpt.sqlite
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT_OUT_DIRNAME = "reading-output"
DEFAULT_AUTH_DIRNAME = ".reading-agent"


def resolve_out_dir(cli_value: str | None = None) -> Path:
    """产物根目录。优先级：命令行 > 环境变量 > 当前工作目录/reading-output。"""
    v = cli_value or os.environ.get("READING_AGENT_OUT")
    if v:
        return Path(v).expanduser().resolve()
    return (Path.cwd() / DEFAULT_OUT_DIRNAME).resolve()


def resolve_auth_dir(cli_value: str | None = None) -> Path:
    """登录态目录。优先级：命令行 > 环境变量 > ~/.reading-agent。"""
    v = cli_value or os.environ.get("READING_AGENT_AUTH")
    if v:
        return Path(v).expanduser().resolve()
    return (Path.home() / DEFAULT_AUTH_DIRNAME).resolve()


def book_paths(out_dir: Path, book_title: str) -> tuple[Path, Path]:
    """返回 (书产物目录, 该书的续跑状态目录)。"""
    base = out_dir / book_title
    return base, base / ".state"


def list_books(out_dir: Path) -> list[Path]:
    """列出已采集的书。

    只认「像一本书的目录」——含 toc.json 或 screenshots/ 子目录，
    避免把 assets/ 这类同级目录误当成书。
    """
    if not out_dir.exists():
        return []
    books = []
    for d in sorted(out_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / "toc.json").exists() or (d / "screenshots").is_dir() or (d / "chapters").is_dir():
            books.append(d)
    return books
