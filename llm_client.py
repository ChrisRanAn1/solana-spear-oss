"""LLM adapter — provider-agnostic wrapper over anthropic + openai SDKs.

Dispatches by model name prefix:
- `claude-*` → Anthropic (Messages API)
- everything else → OpenAI (Chat Completions API, incl. CLIProxyAPI / other
  OpenAI-compatible endpoints via `base_url`)

The adapter exposes three primitives:
- `stream_text()`  — streaming text generation (system + user)
- `create_text()`  — non-streaming text (used by judge)
- `tool_call()`    — one-shot structured output via tool/function calling

Tool schemas are kept in Anthropic-style format (`name` / `description` /
`input_schema`) — the adapter converts to OpenAI function-calling format
internally so callers don't need two schemas.

Config comes from `~/.solana-spear/config.json`. Both keys can coexist:
  {
    "default_model": "gpt-5.6-sol",
    "anthropic_api_key": "sk-ant-...",
    "openai_api_key":    "O--VJOL...",
    "base_url":          "http://127.0.0.1:8317/v1"   # optional, applies to
                                                       # whichever provider is
                                                       # active. Prefix with
                                                       # openai_ or anthropic_
                                                       # to scope.
  }
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

import anthropic
import httpx
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI


# ---- normalized exceptions --------------------------------------------------


class LLMError(Exception):
    """Base class for adapter-raised errors."""


class LLMRefusalError(LLMError):
    """Model refused to respond (safety filter). Non-retryable."""


class LLMRateLimitError(LLMError):
    """429 rate limit hit. Retryable with backoff."""


class LLMTimeoutError(LLMError):
    """Connection / read timeout. Retryable."""


class LLMConnectionError(LLMError):
    """Underlying network failure. Retryable."""


class LLMStatusError(LLMError):
    """Non-2xx HTTP that isn't rate limit."""

    def __init__(self, status_code: int, msg: str):
        self.status_code = status_code
        super().__init__(msg)


# ---- streaming result -------------------------------------------------------


@dataclass
class StreamResult:
    text: str
    stop_reason: str          # "end_turn" | "max_tokens" | "refusal" | "tool_use"
    output_tokens: Optional[int] = None


# ---- provider dispatch ------------------------------------------------------


def infer_provider(model: str) -> str:
    """`claude-*` → anthropic, else → openai."""
    m = (model or "").strip().lower()
    if m.startswith("claude"):
        return "anthropic"
    return "openai"


def _wrap_transient(exc: BaseException) -> BaseException:
    """Normalize provider-specific transient errors into LLM* variants."""
    if isinstance(exc, (LLMError,)):
        return exc
    if isinstance(exc, (anthropic.RateLimitError, openai.RateLimitError)):
        return LLMRateLimitError(str(exc)[:200])
    if isinstance(exc, (anthropic.APITimeoutError, openai.APITimeoutError,
                         httpx.ReadTimeout, httpx.ConnectTimeout,
                         httpx.WriteTimeout, httpx.PoolTimeout)):
        return LLMTimeoutError(str(exc)[:200])
    if isinstance(exc, (anthropic.APIConnectionError, openai.APIConnectionError,
                         httpx.RemoteProtocolError, httpx.ReadError,
                         httpx.ConnectError)):
        return LLMConnectionError(str(exc)[:200])
    if isinstance(exc, (anthropic.APIStatusError, openai.APIStatusError)):
        code = getattr(exc, "status_code", 0) or 0
        if code == 429:
            return LLMRateLimitError(str(exc)[:200])
        if code in (500, 502, 503, 504, 529):
            return LLMConnectionError(f"HTTP {code}: {str(exc)[:180]}")
        return LLMStatusError(code, str(exc)[:200])
    return exc


# ---- main adapter -----------------------------------------------------------


class LLMClient:
    """Wrapper that picks Anthropic or OpenAI SDK per current model."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model: str = cfg.get("default_model") or "gpt-4o"
        self.provider: str = infer_provider(self.model)
        self._openai: Optional[AsyncOpenAI] = None
        self._anthropic: Optional[AsyncAnthropic] = None

    # ---- lazy client factories ----

    def _get_openai(self) -> AsyncOpenAI:
        if self._openai is not None:
            return self._openai
        key = (
            self.cfg.get("openai_api_key")
            or self.cfg.get("api_key")
            or self.cfg.get("anthropic_api_key")  # last resort — often the local proxy key
        )
        if not key:
            raise RuntimeError("no openai_api_key in config")
        kwargs: dict = {"api_key": key, "timeout": None}
        base = (self.cfg.get("openai_base_url") or self.cfg.get("base_url") or "").strip()
        if base:
            kwargs["base_url"] = base
        self._openai = AsyncOpenAI(**kwargs)
        return self._openai

    def _get_anthropic(self) -> AsyncAnthropic:
        if self._anthropic is not None:
            return self._anthropic
        key = self.cfg.get("anthropic_api_key") or self.cfg.get("api_key")
        if not key:
            raise RuntimeError("no anthropic_api_key in config")
        kwargs: dict = {"api_key": key, "timeout": None}
        base = (self.cfg.get("anthropic_base_url") or self.cfg.get("base_url") or "").strip()
        if base and self.provider == "anthropic":
            # only apply base_url when it's targeting anthropic (avoid picking
            # up an OpenAI proxy URL by accident)
            browser_ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )

            async def _mask(request: "httpx.Request") -> None:
                request.headers["user-agent"] = browser_ua
                for k in [h for h in request.headers.keys()
                          if h.lower().startswith("x-stainless-")]:
                    del request.headers[k]

            kwargs["base_url"] = base
            kwargs["http_client"] = httpx.AsyncClient(
                timeout=None, event_hooks={"request": [_mask]},
            )
        self._anthropic = AsyncAnthropic(**kwargs)
        return self._anthropic

    # ---- streaming ----

    async def stream_text(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> StreamResult:
        """Stream text output. `on_progress(tokens_so_far)` fires per chunk."""
        try:
            if self.provider == "anthropic":
                return await self._anthropic_stream(system, user, max_tokens, on_progress)
            return await self._openai_stream(system, user, max_tokens, on_progress)
        except BaseException as e:
            raise _wrap_transient(e) from e

    async def _anthropic_stream(self, system, user, max_tokens, on_progress) -> StreamResult:
        client = self._get_anthropic()
        text_parts: list[str] = []
        async with client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            async for chunk in stream.text_stream:
                text_parts.append(chunk)
                if on_progress is not None:
                    on_progress(sum(len(p) for p in text_parts) // 4)
            final = await stream.get_final_message()
        text = "".join(text_parts)
        stop_raw = getattr(final, "stop_reason", None) or ""
        stop = {
            "end_turn": "end_turn",
            "max_tokens": "max_tokens",
            "stop_sequence": "end_turn",
            "tool_use": "tool_use",
            "refusal": "refusal",
        }.get(stop_raw, stop_raw or "end_turn")
        toks = None
        try:
            if final.usage and final.usage.output_tokens:
                toks = final.usage.output_tokens
        except Exception:
            pass
        return StreamResult(text=text, stop_reason=stop, output_tokens=toks)

    async def _openai_stream(self, system, user, max_tokens, on_progress) -> StreamResult:
        client = self._get_openai()
        text_parts: list[str] = []
        finish_reason: Optional[str] = None
        usage_tokens: Optional[int] = None
        stream = await client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.choices:
                d = chunk.choices[0].delta
                piece = getattr(d, "content", None) if d else None
                if piece:
                    text_parts.append(piece)
                    if on_progress is not None:
                        on_progress(sum(len(p) for p in text_parts) // 4)
                fr = getattr(chunk.choices[0], "finish_reason", None)
                if fr:
                    finish_reason = fr
            usage = getattr(chunk, "usage", None)
            if usage and getattr(usage, "completion_tokens", None):
                usage_tokens = usage.completion_tokens
        text = "".join(text_parts)
        stop = {
            "stop": "end_turn",
            "length": "max_tokens",
            "content_filter": "refusal",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
        }.get(finish_reason or "", finish_reason or "end_turn")
        return StreamResult(text=text, stop_reason=stop, output_tokens=usage_tokens)

    # ---- non-streaming (used by judge) ----

    async def create_text(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
    ) -> tuple[str, Optional[int]]:
        """Non-streaming text generation. Returns (text, output_tokens).

        output_tokens is the provider's reported completion count, or None
        if the provider didn't include usage in the response.
        """
        try:
            if self.provider == "anthropic":
                client = self._get_anthropic()
                resp = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(
                    b.text for b in resp.content
                    if getattr(b, "type", None) == "text"
                )
                tokens: Optional[int] = None
                try:
                    if resp.usage and resp.usage.output_tokens:
                        tokens = resp.usage.output_tokens
                except Exception:
                    pass
                return text, tokens
            client = self._get_openai()
            resp = await client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            text = resp.choices[0].message.content or ""
            tokens = None
            try:
                if resp.usage and resp.usage.completion_tokens:
                    tokens = resp.usage.completion_tokens
            except Exception:
                pass
            return text, tokens
        except BaseException as e:
            raise _wrap_transient(e) from e

    # ---- tool calling ----

    async def tool_call(
        self,
        *,
        system: str,
        user: str,
        tool: dict,
        max_tokens: int = 4096,
    ) -> dict:
        """One-shot structured output.

        `tool` is in Anthropic format:
            {"name": str, "description": str, "input_schema": {json_schema}}
        Adapter converts to OpenAI function-calling shape internally.
        Returns the parsed tool arguments as a dict.

        Retries on transient errors (5xx, timeouts, rate limits) with
        exponential backoff. Refusal / malformed / auth (401/403) / 4xx
        (except 429) bubble immediately.
        """
        # Keep debug_agent / env_agent / assertion_agent from crashing a whole round just because
        # the provider hiccups once. 4 attempts, ~40s total, is enough to ride out a 15-60s outage.
        delays = [2.0, 8.0, 30.0]
        last_exc: Optional[BaseException] = None
        for attempt in range(4):
            try:
                if self.provider == "anthropic":
                    return await self._anthropic_tool(system, user, tool, max_tokens)
                return await self._openai_tool(system, user, tool, max_tokens)
            except BaseException as e:
                wrapped = _wrap_transient(e)
                # Only retry genuinely transient errors; refusal/status/others propagate up
                if isinstance(wrapped, (LLMConnectionError, LLMTimeoutError, LLMRateLimitError)):
                    last_exc = wrapped
                    if attempt < len(delays):
                        await asyncio.sleep(delays[attempt])
                        continue
                raise wrapped from e
        # Unreachable — the loop either returns or raises; this is just a safety net
        raise last_exc if last_exc else LLMError("tool_call: retries exhausted")

    async def _anthropic_tool(self, system, user, tool, max_tokens) -> dict:
        client = self._get_anthropic()
        resp = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[{
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool["input_schema"],
            }],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                return dict(block.input or {})
        stop = getattr(resp, "stop_reason", "")
        if stop == "refusal":
            raise LLMRefusalError(f"anthropic refused (stop_reason=refusal)")
        raise LLMError(f"anthropic tool_call: no tool_use in response (stop={stop})")

    async def _openai_tool(self, system, user, tool, max_tokens) -> dict:
        client = self._get_openai()
        of = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        resp = await client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=[of],
            tool_choice={"type": "function", "function": {"name": tool["name"]}},
        )
        choice = resp.choices[0] if resp.choices else None
        if choice is None:
            raise LLMError("openai tool_call: empty choices")
        finish = getattr(choice, "finish_reason", None)
        if finish == "content_filter":
            raise LLMRefusalError("openai refused (finish_reason=content_filter)")
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            if fn and fn.name == tool["name"]:
                try:
                    return json.loads(fn.arguments or "{}")
                except json.JSONDecodeError as e:
                    raise LLMError(f"openai tool_call: JSON parse failed: {e}")
        raise LLMError(
            f"openai tool_call: no tool_calls in response (finish={finish})"
        )
