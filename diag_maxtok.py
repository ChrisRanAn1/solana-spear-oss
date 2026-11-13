"""Probe: what max_tokens does the configured backend accept for opus-5?"""
import asyncio, json
from pathlib import Path
import httpx
from anthropic import AsyncAnthropic

CONFIG = json.loads((Path.home() / ".solana-spear/config.json").read_text())

async def _mask(request):
    request.headers["user-agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    for k in [h for h in request.headers.keys() if h.lower().startswith("x-stainless-")]:
        del request.headers[k]

async def probe(max_tok):
    kwargs = {"api_key": CONFIG["anthropic_api_key"], "timeout": None}
    if CONFIG.get("base_url"):
        kwargs["base_url"] = CONFIG["base_url"]
        kwargs["http_client"] = httpx.AsyncClient(timeout=None, event_hooks={"request": [_mask]})
    client = AsyncAnthropic(**kwargs)
    model = CONFIG.get("default_model", "claude-opus-5")
    try:
        # tiny prompt, just checking if the max_tokens param is accepted
        r = await client.messages.create(
            model=model,
            max_tokens=max_tok,
            messages=[{"role": "user", "content": "say hi"}]
        )
        print(f"  max_tokens={max_tok:>6}: OK (stop_reason={r.stop_reason})")
        return True
    except Exception as e:
        print(f"  max_tokens={max_tok:>6}: {type(e).__name__}: {str(e)[:200]}")
        return False

async def main():
    print(f"probing max_tokens limits for {CONFIG.get('default_model', 'claude-opus-5')}...")
    for m in [16384, 32768, 65536, 128000, 200000]:
        ok = await probe(m)
        if not ok:
            break

asyncio.run(main())
