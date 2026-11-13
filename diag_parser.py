"""Test the new multi-target parser against the exact user example."""
import asyncio
import json
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic

from req_parser import parse_request

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
    if CONFIG.get("base_url"):
        kwargs["base_url"] = CONFIG["base_url"]
        kwargs["http_client"] = httpx.AsyncClient(
            timeout=None, event_hooks={"request": [_mask]}
        )
    client = AsyncAnthropic(**kwargs)

    user_input = """The source code was delivered to us in a Git repository at https://github.com/metaDAOproject. This audit
was performed against 841f887.
Brief descriptions of the programs are as follows:
Name Description
conditional-vault
It implements a binary conditional token system for MetaDAO, enabling
users to lock collateral, mint paired outcome tokens, trade them freely,
and redeem the winning outcome after a question is resolved.
futarchy
It replaces DAO voting with prediction markets by creating PASS/FAIL
markets for each proposal and using market prices to decide outcomes.
Traders stake real capital in conditional AMMs, and a proposal passes
only if the PASS market's TWAP exceeds the FAIL market by a defined
threshold."""

    print("=== INPUT ===")
    print(user_input)
    print("\n=== PARSING... ===")
    targets, err = await parse_request(client, user_input)
    print(f"\n=== RESULT ===")
    print(f"targets ({len(targets)}):")
    for i, t in enumerate(targets, 1):
        print(f"  {i}. {json.dumps(t, ensure_ascii=False)}")
    print(f"error: {err!r}")


if __name__ == "__main__":
    asyncio.run(main())
