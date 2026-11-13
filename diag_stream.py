"""Diagnostic: does tok2.you actually support SSE streaming?

Uses the exact same client config as harness.py, sends one minimal request,
prints when chunks arrive so we can see if they trickle or arrive all at once.
"""
import asyncio
import json
import time
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic

CONFIG = json.loads((Path.home() / ".solana-spear/config.json").read_text())
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _mask(request):
    request.headers["user-agent"] = BROWSER_UA
    for k in [h for h in request.headers.keys() if h.lower().startswith("x-stainless-")]:
        del request.headers[k]


async def main():
    kwargs = {"api_key": CONFIG["anthropic_api_key"], "timeout": None}
    base = CONFIG.get("base_url", "").strip()
    if base:
        print(f"[diag] using base_url = {base}")
        kwargs["base_url"] = base
        kwargs["http_client"] = httpx.AsyncClient(
            timeout=None, event_hooks={"request": [_mask]}
        )
    else:
        print("[diag] using OFFICIAL Anthropic (no base_url)")

    model = CONFIG.get("default_model", "claude-opus-5")
    print(f"[diag] model = {model}")

    client = AsyncAnthropic(**kwargs)

    prompt = "Count from 1 to 30, one number per line. Do not add any commentary."
    print(f"[diag] sending 30-count request...")
    t0 = time.monotonic()

    try:
        chunks = 0
        async with client.messages.stream(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            t_open = time.monotonic() - t0
            print(f"[diag] stream context opened at +{t_open:.2f}s")
            async for chunk in stream.text_stream:
                chunks += 1
                dt = time.monotonic() - t0
                print(f"[diag] +{dt:6.2f}s  chunk #{chunks} ({len(chunk)} chars): {chunk!r}")
            final = await stream.get_final_message()
            dt = time.monotonic() - t0
            print(f"[diag] +{dt:6.2f}s  DONE. total chunks={chunks}, stop_reason={final.stop_reason}")
            if final.usage:
                print(f"[diag] usage: input={final.usage.input_tokens}, output={final.usage.output_tokens}")
    except Exception as e:
        dt = time.monotonic() - t0
        print(f"[diag] +{dt:6.2f}s  EXCEPTION: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
