"""Detect LLM proxy self-recoverable failures and heal them in place.

Background: user runs against a local `cliproxyapi` (127.0.0.1:8317) which
forwards OAuth-based codex sessions. Under load / rate-limit it flips into
an internal `auth_unavailable` backoff and returns HTTP 503 for every
subsequent request until you `brew services restart cliproxyapi`. The
assertion/executor agents each have their own retry loop; without a heal
step they burn all 3 retries against the same broken proxy and fail the
whole finding.

This module gives them one hook: `maybe_heal_llm_proxy(exc)`. Call it in
the retry loop's `except` block before sleeping. If the exception looks
proxy-self-recoverable it runs the heal command (default:
`brew services restart cliproxyapi`) and returns True. Caller can then
shorten backoff and retry.

Heal command can be overridden in ~/.solana-spear/config.json:
    "llm_heal_cmd": "brew services restart cliproxyapi"
    (or a list of argv)
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Optional

# If any of these substrings appear, treat it as proxy-self-recoverable
_HEAL_TRIGGERS = (
    "auth_unavailable",
    "no auth available",
)

_CONFIG_FILE = Path.home() / ".solana-spear" / "config.json"


def _load_heal_cmd() -> Optional[list[str]]:
    """Read llm_heal_cmd from config.json; supports str (run via bash -c) or list (argv).

    Defaults to ['brew', 'services', 'restart', 'cliproxyapi'] — matches the user's current setup.
    Explicitly setting it to "" / [] / null disables healing.
    """
    cfg = {}
    try:
        cfg = json.loads(_CONFIG_FILE.read_text())
    except Exception:
        pass

    if "llm_heal_cmd" in cfg:
        v = cfg["llm_heal_cmd"]
        if v is None or v == "" or v == []:
            return None
        if isinstance(v, str):
            return ["bash", "-c", v]
        if isinstance(v, list) and v:
            return [str(x) for x in v]

    return ["brew", "services", "restart", "cliproxyapi"]


def maybe_heal_llm_proxy(exc: BaseException) -> bool:
    """If exc looks like a proxy-self-recoverable error, run the heal command. Returns whether a heal was triggered.

    Callers typically use it in the retry except block:
        healed = maybe_heal_llm_proxy(e)
        backoff = 5 if healed else default_backoff
    """
    msg = str(exc)
    if not any(t in msg for t in _HEAL_TRIGGERS):
        return False

    cmd = _load_heal_cmd()
    if not cmd:
        return False

    try:
        subprocess.run(cmd, capture_output=True, timeout=25)
    except Exception:
        return False
    return True


async def maybe_heal_and_wait(exc: BaseException, settle_sec: float = 3.0) -> bool:
    """maybe_heal_llm_proxy + await asyncio.sleep to let the proxy come back up. Convenient for use in async retry."""
    healed = maybe_heal_llm_proxy(exc)
    if healed:
        await asyncio.sleep(settle_sec)
    return healed
