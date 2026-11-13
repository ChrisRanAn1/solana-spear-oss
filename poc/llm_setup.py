"""Per-agent LLM routing for the poc pipeline.

Lets the poc pipeline pin its own model / API key / endpoint independently
of the global defaults in ~/.solana-spear/config.json. Only poc code paths
are affected — spear's shared LLMClient keeps its global settings.

Overridable fields (each independent, all optional):
  model       env POC_MODEL       > config `poc_model`       > default gpt-5.6-sol
  api key     env POC_API_KEY     > config `poc_openai_api_key` / `poc_api_key`
                                                             > shared cfg (no override)
  base url    env POC_BASE_URL    > config `poc_base_url`    > shared cfg (no override)

Usage: at the top of each poc agent entry point:

    from poc.llm_setup import poc_client
    client = poc_client(client)

The wrapper deep-copies `client.cfg` before mutating so the shared LLMClient
instance keeps its original config; it also resets the cached lazy-inited
openai/anthropic SDK clients on the returned copy so a swapped base_url /
api key takes effect on the next request.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

_CONFIG = Path.home() / ".solana-spear" / "config.json"
_DEFAULT_POC_MODEL = "gpt-5.6-sol"


def _read_cfg() -> dict:
    try:
        return json.loads(_CONFIG.read_text())
    except Exception:
        return {}


def _resolve(env_key: str, *cfg_keys: str, default: str = "") -> str:
    v = os.environ.get(env_key, "").strip()
    if v:
        return v
    cfg = _read_cfg()
    for k in cfg_keys:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def poc_model() -> str:
    """The model name poc agents should use, honoring env / config overrides."""
    return _resolve("POC_MODEL", "poc_model", default=_DEFAULT_POC_MODEL)


def poc_api_key() -> str:
    """Poc-scoped API key. Empty string means: keep shared client's key."""
    return _resolve("POC_API_KEY", "poc_openai_api_key", "poc_api_key", default="")


def poc_base_url() -> str:
    """Poc-scoped OpenAI-compatible base URL. Empty means: keep shared client's."""
    return _resolve("POC_BASE_URL", "poc_base_url", default="")


def poc_client(client):
    """Return an LLMClient reconfigured for the poc pipeline.

    Overrides `.model`, and optionally the openai api_key / base_url on a
    detached copy of `client.cfg`. Lazy-cached SDK instances are reset so
    the new endpoint / key take effect on the next request.

    The original client (used by spear main pipeline) is not mutated.
    """
    m = poc_model()
    key = poc_api_key()
    base = poc_base_url()
    provider = "anthropic" if (m or "").strip().lower().startswith("claude") else "openai"

    c = copy.copy(client)
    c.cfg = dict(getattr(client, "cfg", {}) or {})
    c.model = m
    c.provider = provider

    if key:
        if provider == "anthropic":
            c.cfg["anthropic_api_key"] = key
        else:
            c.cfg["openai_api_key"] = key

    if base:
        if provider == "anthropic":
            c.cfg["anthropic_base_url"] = base
        else:
            c.cfg["openai_base_url"] = base

    c._openai = None
    c._anthropic = None

    if provider == "openai" and (m or "").lower().startswith("deepseek"):
        _install_deepseek_no_thinking(c)

    return c


def _install_deepseek_no_thinking(c) -> None:
    """Force DeepSeek V4 into non-thinking mode for this LLMClient copy.

    DeepSeek V4-Pro / V4-Flash default to thinking mode, which rejects
    forced `tool_choice` (returns HTTP 400 "Thinking mode does not support
    this tool_choice"). The poc pipeline relies on forced tool_choice for
    structured output in every agent (define_poc_verdict, answer_scope,
    emit_test, …). Injecting `reasoning_effort="none"` disables thinking
    at the request level and lets forced tool_choice through.

    Patch scope: only the openai SDK instance owned by this poc copy of
    LLMClient is patched. The shared spear client is untouched. The patch
    also guards on `model` at call time so a non-DeepSeek model routed
    through the same instance (unlikely for poc, but possible) is not
    affected.
    """
    oai = c._get_openai()
    completions = oai.chat.completions
    orig_create = completions.create

    async def _create(*args, **kwargs):
        model_name = str(kwargs.get("model") or "").lower()
        if model_name.startswith("deepseek"):
            extra_body = kwargs.pop("extra_body", None) or {}
            extra_body.setdefault("reasoning_effort", "none")
            kwargs["extra_body"] = extra_body
        return await orig_create(*args, **kwargs)

    completions.create = _create
