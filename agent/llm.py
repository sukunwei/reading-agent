#!/usr/bin/env python3
"""LLM 接入层（Anthropic Claude）。

要点：
  · 默认模型 claude-opus-5-5；思考用 adaptive；结构化输出用 messages.parse(output_format=...)
  · 长文本 / 长输出走 streaming（避免 HTTP 超时）
  · 异常按「可重试 → 不可重试」分级处理
  · 没有 API key 时可用 MOCK=1 走离线占位输出，用于验证整条流水线

环境变量：
  ANTHROPIC_API_KEY   必填（生产）
  LLM_MODEL           覆盖默认模型
  LLM_SUMMARY_MODEL   逐章总结用的模型（默认同 LLM_MODEL）
  LLM_REVIEW_MODEL    全书评测用的模型（默认同 LLM_MODEL）
  LLM_EFFORT          low | medium | high | xhigh | max（默认 medium）
  MOCK=1              离线模式，不调 API
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


# ⚠️ 默认模型不写死：优先用 LLM_MODEL → ANTHROPIC_MODEL（与 Claude Code 全局配置一致）
#    再退回官方 Claude。若走的是第三方 Anthropic 兼容端点（如 MiniMax），
#    写死 claude-opus-5-5 会 404。
def _default_model() -> str:
    return (os.environ.get("LLM_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "claude-opus-5-5")


T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMCallError(LLMError):
    """带分类的结构化调用错误 —— 供「诊断 / 恢复」节点判断能不能自动重试。

    category 取值：
        json_invalid    模型输出不是合法 JSON
        schema_invalid  JSON 合法但不符合 schema
        truncated       触到 max_tokens 被截断
        rate_limit      429
        server_error    5xx
        network         连接失败 / 超时
        bad_request     4xx（参数错，模型名不存在等）
        auth            401/403 凭据问题
        empty_input     输入为空（章节正文为空）
        unknown
    """

    def __init__(self, message: str, category: str = "unknown",
                 retryable: bool = True, detail: str = ""):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.detail = detail

    def to_dict(self) -> dict:
        return {"category": self.category, "retryable": self.retryable,
                "message": str(self)[:300], "detail": self.detail[:300]}


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    """把异常映射成 (category, retryable)。"""
    import anthropic
    if isinstance(exc, LLMCallError):
        return exc.category, exc.retryable
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit", True
    if isinstance(exc, anthropic.AuthenticationError):
        return "auth", False
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "auth", False
    if isinstance(exc, anthropic.NotFoundError):
        return "bad_request", False
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return "server_error", True
        return "bad_request", False
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "network", True
    if isinstance(exc, ValidationError):
        return "schema_invalid", True
    if isinstance(exc, json.JSONDecodeError):
        return "json_invalid", True
    if isinstance(exc, ValueError):
        return "json_invalid", True
    return "unknown", False


_FALLBACK_WARNED = False   # 进程级：降级提示只打一次


def _resolve_usage_log() -> Path | None:
    """台账路径：由环境变量指定（各阶段写同一个文件）。"""
    v = os.environ.get("READING_AGENT_USAGE_LOG")
    return Path(v).expanduser() if v else None


def _mock_enabled() -> bool:
    return os.environ.get("MOCK", "").strip() in ("1", "true", "yes")


class LLM:
    def __init__(self, client: Any = None, model: str | None = None,
                 effort: str | None = None, mock: bool | None = None,
                 label: str = "", usage_log: Path | None = None):
        # label: 这一组调用属于哪个阶段（summary / review / figure …）
        # usage_log: 把每次调用的 token 追加到台账，供 HTML 报告统计
        self.label = label or os.environ.get("LLM_LABEL", "")
        self.usage_log = usage_log or _resolve_usage_log()
        self.model = model or _default_model()
        self.summary_model = os.environ.get("LLM_SUMMARY_MODEL") or self.model
        self.review_model = os.environ.get("LLM_REVIEW_MODEL") or self.model
        self.effort = effort or os.environ.get("LLLM_EFFORT", "medium")
        self.mock = _mock_enabled() if mock is None else mock
        # native = messages.parse（服务端强制 schema）
        # prompt = 提示词要求 JSON + 本地 pydantic 校验（端点不支持强制输出时用）
        # auto  = 先试 native，失败自动降级
        self.structured_mode = os.environ.get("LLM_STRUCTURED_OUTPUT", "auto").lower()
        # 原生结构化输出失败一次后就不再试 —— 该端点本就不强制 JSON，
        # 每次重试都白等一次（实测挂起可达数分钟）。
        self._native_disabled = False
        self._client = client
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        # 并行调用（多 worker 跑各章）时保护共享状态
        self._lock = threading.Lock()
        self.last_stop_reason: str | None = None

    # ---------------------------------------------------------------- client
    @property
    def client(self):
        with self._lock:
            return self._get_client()

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise LLMError("未安装 anthropic SDK：pip install anthropic") from e
            if not (os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                raise LLMError(
                    "缺少凭据：设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN，"
                    "或用 MOCK=1 走离线模式验证流水线。")
            # API_TIMEOUT_MS（Claude Code 全局配置里也是这个变量名）是毫秒
            timeout = os.environ.get("API_TIMEOUT_MS")
            kwargs: dict[str, Any] = {}
            if timeout:
                with contextlib.suppress(ValueError):
                    kwargs["timeout"] = float(timeout) / 1000.0
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # ---------------------------------------------------------------- 公共
    def structured(self, system: str, user: str, schema: type[T],
                   model: str | None = None, max_tokens: int = 16000,
                   effort: str | None = None) -> T:
        """结构化输出：返回经过 schema 校验的 pydantic 对象。

        优先用原生结构化输出（messages.parse，服务端强制 schema）。
        实测：部分 Anthropic 兼容端点（如 MiniMax）**接受参数但不强制** ——
        返回的是散文而非 JSON，校验会失败。此时自动降级为
        「提示词要求 JSON + 本地 pydantic 校验 + 带着错误信息修复重试」。
        """
        if self.mock:
            return self._mock_structured(schema)

        if self.structured_mode in ("native", "auto") and not self._native_disabled:
            try:
                return self._structured_native(system, user, schema, model, max_tokens, effort)
            except Exception as e:
                if self.structured_mode == "native":
                    raise
                self._native_disabled = True
                global _FALLBACK_WARNED
                if not _FALLBACK_WARNED:
                    _FALLBACK_WARNED = True
                    print(f"  ⚠️ 原生结构化输出不可用（{type(e).__name__}），"
                          f"降级为提示词 JSON 模式（仅提示一次）")
        return self._structured_prompt(system, user, schema, model, max_tokens, effort)

    def structured_vision(self, system: str, user: str, image_path: Path | str,
                          image_b64: str | None, schema: type[T],
                          model: str | None = None, max_tokens: int = 8000,
                          effort: str | None = None) -> T:
        """多模态 + 结构化输出。

        ⚠️ 实测：MiniMax 的 Anthropic 兼容端点**接受** output_config.format
        但不强制 JSON → 走提示词 JSON + 本地校验的降级路径。
        """
        if self.mock:
            return self._mock_structured(schema)

        import base64 as _b64
        if image_b64 is None:
            image_b64 = _b64.standard_b64encode(Path(image_path).read_bytes()).decode()

        model = model or self.model
        effort = effort or self.effort
        content: list[Any] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
            {"type": "text", "text": user},
        ]

        if self.structured_mode in ("native", "auto") and not self._native_disabled:
            try:
                with self.client.messages.stream(
                    model=model, max_tokens=max_tokens, system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    output_format=schema,
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    msg = stream.get_final_message()
                self._record(msg.usage)
                if msg.parsed is not None:
                    # ⚠️ 某些 Anthropic 兼容端点（实测 MiniMax）会回传**原始 dict**
                    #    而非校验后的模型 —— 直接返回会在调用方 .model_dump() 时炸
                    #    "'dict' object has no attribute 'model_dump'"。
                    if isinstance(msg.parsed, dict):
                        msg.parsed = schema.model_validate(msg.parsed)
                    if not isinstance(msg.parsed, schema):
                        raise LLMCallError(
                            f"结构化结果类型不符（{type(msg.parsed).__name__}）",
                            category="schema_invalid", retryable=True)
                    return msg.parsed
                raise LLMCallError("未返回结构化结果",
                                   category="schema_invalid", retryable=True)
            except Exception:
                if self.structured_mode == "native":
                    raise
                self._native_disabled = True
                global _FALLBACK_WARNED
                if not _FALLBACK_WARNED:
                    _FALLBACK_WARNED = True
                    print("  ⚠️ 原生结构化输出不可用，降级为提示词 JSON 模式（仅提示一次）")
        return self._structured_prompt_vision(system, user, content, schema, model, max_tokens, effort)

    def _structured_prompt_vision(self, system: str, user: str, content: list[Any],
                                  schema: type[T], model: str, max_tokens: int,
                                  effort: str) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys_p = (system + "\n\n【输出格式】只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码块。\n"
                 + "JSON 必须严格符合以下 JSON Schema：\n" + schema_json)
        msgs: list[dict[str, Any]] = [{"role": "user", "content": content}]
        last = ""
        for attempt in range(3):
            try:
                with self.client.messages.stream(
                    model=model, max_tokens=max_tokens, system=sys_p,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    messages=msgs,
                ) as stream:
                    msg = stream.get_final_message()
                self._record(msg.usage)
                raw = "".join(b.text for b in msg.content if b.type == "text")
                return schema.model_validate(_extract_json(raw))
            except (ValidationError, ValueError) as e:
                last = str(e)[:500]
                msgs.append({"role": "assistant", "content": raw if "raw" in dir() else ""})
                msgs.append({"role": "user",
                             "content": f"上一次输出不符合 schema：\n{last}\n请只输出修正后的 JSON。"})
            except Exception as e:
                if not self._is_retryable(e):
                    raise
                time.sleep(min(20, 2 ** attempt * 3))
        raise LLMCallError(f"多模态结构化输出失败：{last}",
                           category="schema_invalid", retryable=True)

    def warmup(self) -> bool:
        """探测原生结构化输出是否可用，结果缓存。

        ⚠️ **并行调用前必须先做这一步**：
        多个 worker 同时去试原生路径时，它们会一起卡在那个会挂起的请求上
        （实测原生路径在该端点会挂起数分钟）。先串行探一次，
        失败就把 `_native_disabled` 置上，后面所有 worker 直接走提示词模式。
        返回 True = 原生可用。
        """
        if self.structured_mode != "auto":
            return self.structured_mode == "native"
        if self._native_disabled:
            return False
        try:
            class _Probe(BaseModel):
                ok: bool
            self._structured_native("只输出 JSON", '返回 {"ok": true}', _Probe, max_tokens=64)
            return True
        except Exception:
            self._native_disabled = True
            return False

    def _structured_native(self, system: str, user: str, schema: type[T],
                          model: str | None, max_tokens: int,
                          effort: str | None) -> T:
        model = model or self.model
        effort = effort or self.effort
        last_exc: Exception | None = None

        for attempt in range(4):
            try:
                with self.client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    output_format=schema,
                    messages=[{"role": "user", "content": user}],
                ) as stream:
                    msg = stream.get_final_message()
                self._record(msg.usage)
                self.last_stop_reason = getattr(msg, "stop_reason", None)
                parsed = msg.parsed
                if parsed is None:
                    raise LLMCallError(
                        f"模型未返回结构化结果（stop_reason={msg.stop_reason}）",
                        category="truncated" if msg.stop_reason == "max_tokens" else "schema_invalid",
                        retryable=True)
                # 某些 Anthropic 兼容端点会回传**原始 dict** 而非校验后的模型
                # （实测 MiniMax 端点偶发），统一在这里强制转成 schema 实例。
                if isinstance(parsed, dict):
                    parsed = schema.model_validate(parsed)
                if not isinstance(parsed, schema):
                    raise LLMCallError(
                        f"结构化结果类型不符（{type(parsed).__name__}）",
                        category="schema_invalid", retryable=True)
                return parsed
            except Exception as e:
                last_exc = e
                if not self._is_retryable(e) or attempt == 3:
                    break
                time.sleep(min(30, 2 ** attempt * 3))

        cat, retryable = classify_exception(last_exc) if last_exc else ("unknown", False)
        raise LLMCallError(f"结构化输出失败：{last_exc}", category=cat, retryable=retryable) from last_exc

    def _structured_prompt(self, system: str, user: str, schema: type[T],
                           model: str | None, max_tokens: int,
                           effort: str | None) -> T:
        """提示词要求 JSON + 本地校验 + 修复重试（端点不支持强制结构化输出时）。"""
        model = model or self.model
        effort = effort or self.effort
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        sys_p = (
            system
            + "\n\n【输出格式】只输出一个 JSON 对象。不要任何解释、前言、后记或 Markdown 代码块。\n"
            + "JSON 必须严格符合以下 JSON Schema：\n"
            + schema_json
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_err = ""
        for attempt in range(3):
            try:
                with self.client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=sys_p,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    messages=messages,
                ) as stream:
                    msg = stream.get_final_message()
                self._record(msg.usage)
                self.last_stop_reason = getattr(msg, "stop_reason", None)
                if self.last_stop_reason == "max_tokens":
                    raise LLMCallError(
                        "输出被 max_tokens 截断", category="truncated", retryable=True,
                        detail=f"max_tokens={max_tokens}")
                raw = "".join(b.text for b in msg.content if b.type == "text")
                data = _extract_json(raw)
                return schema.model_validate(data)
            except (ValidationError, ValueError) as e:
                last_err = str(e)[:500]
                # 带着错误信息让模型自己修
                messages.append({"role": "assistant",
                                 "content": raw if "raw" in dir() else ""})
                messages.append({
                    "role": "user",
                    "content": f"上一次的输出不符合 schema，校验错误如下：\n{last_err}\n"
                               f"请只输出修正后的 JSON 对象，不要任何其他文字。"})
            except Exception as e:
                if not self._is_retryable(e):
                    raise
                time.sleep(min(30, 2 ** attempt * 3))
        raise LLMCallError(f"结构化输出（提示词模式）失败：{last_err}",
                           category="schema_invalid", retryable=True)

    def text(self, system: str, user: str, model: str | None = None,
             max_tokens: int = 16000, effort: str | None = None) -> str:
        """自由文本输出（用于长篇 Markdown 报告）。"""
        if self.mock:
            return f"[MOCK] {system[:60]}…\n\n{user[:200]}…"

        model = model or self.model
        effort = effort or self.effort
        last_exc: Exception | None = None

        for attempt in range(4):
            try:
                with self.client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                    messages=[{"role": "user", "content": user}],
                ) as stream:
                    msg = stream.get_final_message()
                self._record(msg.usage)
                return "".join(b.text for b in msg.content if b.type == "text")
            except Exception as e:
                last_exc = e
                if not self._is_retryable(e) or attempt == 3:
                    break
                time.sleep(min(30, 2 ** attempt * 3))

        cat, retryable = classify_exception(last_exc) if last_exc else ("unknown", False)
        raise LLMCallError(f"文本生成失败：{last_exc}", category=cat, retryable=retryable) from last_exc

    # ---------------------------------------------------------------- 内部
    def _record(self, usage: Any) -> None:
        with self._lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            snapshot = dict(self.usage)
        self._append_ledger(snapshot, usage)

    def _append_ledger(self, snapshot: dict, usage: Any) -> None:
        """把本次调用的 token 追加到台账文件（跨节点/跨进程累计）。"""
        if not self.usage_log:
            return
        try:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "label": self.label or "unknown",
                "model": self.model,
                "calls": snapshot.get("calls", 0),
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            }
            with self.usage_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _is_retryable(e: Exception) -> bool:
        import anthropic
        if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError,
                          anthropic.APITimeoutError)):
            return True
        if isinstance(e, anthropic.APIStatusError):
            return e.status_code >= 500
        return False

    def _mock_structured(self, schema: type[T]) -> T:
        """离线占位：按 schema 字段类型造一份**内容可见**的结构（便于验证渲染链路）。"""
        data = build_nested(schema)
        return schema.model_validate(data)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _extract_json(text: str) -> Any:
    """从模型输出里抠出第一个 JSON 值。

    容忍：Markdown 代码块围栏、前后多余说明文字、**多个 JSON 连续输出**
    （实测模型偶尔输出「示例 JSON + 正式 JSON」两段，用 find('{')..rfind('}') 会把两段
    一起圈进来导致 "Extra data"）。
    → 用 raw_decode 只解析第一个完整 JSON 值，忽略其后内容。
    """
    t = (text or "").strip()
    if not t:
        raise ValueError("模型返回空内容")
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for opener in ("{", "["):
        i = t.find(opener)
        while i >= 0:
            try:
                obj, _ = dec.raw_decode(t[i:])
                return obj
            except json.JSONDecodeError:
                i = t.find(opener, i + 1)
    raise ValueError(f"无法从输出中解析 JSON：{text[:200]}")


def build_nested(model: type[BaseModel]) -> dict[str, Any]:
    """按字段类型造占位值：字符串给 MOCK 标记，列表给一条样例。"""
    data: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        ann = f.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            data[name] = build_nested(ann)
        elif getattr(ann, "__origin__", None) is list:
            data[name] = ["[MOCK] 示例条目 1", "[MOCK] 示例条目 2"]
        elif ann is float:
            data[name] = 7.5
        elif ann is int:
            data[name] = 1
        elif ann is bool:
            data[name] = False
        else:
            data[name] = f"[MOCK] {name}"
    return data


def load_dotenv(path: str | os.PathLike) -> None:
    """极简 .env 读取（不引第三方依赖）。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
