import asyncio
import re
from typing import Optional

from config import MAX_TOKENS_JUDGE
from llm_client import (
    LLMClient,
    LLMConnectionError,
    LLMRateLimitError,
    LLMTimeoutError,
)

# 20min. If a single agent's judge runs longer than this, treat it as a fatal
# error — a single agent's raw findings are usually a few thousand tokens, so
# not finishing within 20min basically means the network is dead, and retrying
# won't help either.
JUDGE_TIMEOUT_SEC = 1200

# One call per agent, with at most this many running at once. Too high hits
# Anthropic's tokens/min rate limit; too low loses the point of concurrency.
# 4 balances speed and stability.
JUDGE_MAX_CONCURRENCY = 4

# Match the judge output header — the prompt mandates "## Audit report: XXX".
# 8 calls produce 8 headers; keep only the first when merging.
_HEADER_RE = re.compile(
    r"^\s*##\s+Audit\s+report:.*?$\n?",
    re.MULTILINE | re.IGNORECASE,
)

_PREFIX = (
    "To output in the required format, you must write the project's top-level name. ## Audit report: (project top-level name)\n"
    "Project top-level name — write only the top-most brand/organization name, "
    "without the product line or module suffix. "
    "For example 'MetaDAO' (not 'MetaDAO Futarchy'), 'Jupiter' (not 'Jupiter Perps'), "
    "'Kamino' (not 'Kamino Lending').\n"
)


async def _judge_one(
    client: LLMClient,
    judge_prompt: str,
    name: str,
    agent_out: str,
) -> tuple[str | None, Optional[int], str | None]:
    """Judge a single agent's raw findings.

    Retries transient network errors 4x with [2, 8, 30]s backoff. Hard-timeout
    at JUDGE_TIMEOUT_SEC per attempt.
    """
    user_content = f"{_PREFIX}\n===== AGENT: {name} =====\n{agent_out}\n"

    delays = [2.0, 8.0, 30.0]
    last_err: str | None = None
    for attempt in range(len(delays) + 1):
        try:
            text, tokens = await asyncio.wait_for(
                client.create_text(
                    system=judge_prompt,
                    user=user_content,
                    max_tokens=MAX_TOKENS_JUDGE,
                ),
                timeout=JUDGE_TIMEOUT_SEC,
            )
            if attempt > 0:
                print(
                    f"[spear] judge[{name}] succeeded on attempt {attempt + 1}",
                    flush=True,
                )
            return text, tokens, None
        except asyncio.TimeoutError:
            return None, None, f"judge[{name}] timeout after {JUDGE_TIMEOUT_SEC}s"
        except (LLMConnectionError, LLMTimeoutError, LLMRateLimitError) as e:
            last_err = str(e)[:200]
            if attempt >= len(delays):
                return None, None, last_err
            wait = delays[attempt]
            print(
                f"[spear] judge[{name}] attempt {attempt + 1} failed ({last_err}), "
                f"retrying in {wait:.0f}s…",
                flush=True,
            )
            await asyncio.sleep(wait)
        except Exception as e:
            return None, None, str(e)[:200]
    return None, None, last_err


def _merge(parts: list[tuple[str, str]]) -> str:
    """Concat per-agent judge outputs, keeping only the first Audit-report header."""
    pieces: list[str] = []
    kept_header = False
    for _name, text in parts:
        if not kept_header:
            m = _HEADER_RE.search(text)
            if m:
                pieces.append(text.strip())
                kept_header = True
                continue
        stripped = _HEADER_RE.sub("", text, count=1).strip()
        if stripped:
            pieces.append(stripped)
    return "\n\n".join(pieces)


async def run_judge(
    client: LLMClient,
    judge_prompt: str,
    agent_outputs: dict[str, str],
    model: str,   # kept in signature; ignored (client.model authoritative)
) -> tuple[str | None, Optional[int], str | None]:
    """Fan out judge calls, one per agent, then merge.

    Return (merged_text, total_output_tokens, error). If at least one agent's
    judge succeeds, the merged output is returned and error is None (partial
    failures are logged). Only returns error when every agent's judge fails.
    """
    if not agent_outputs:
        return None, None, "no agent outputs to judge"

    sem = asyncio.Semaphore(JUDGE_MAX_CONCURRENCY)

    async def _run(name: str, out: str):
        async with sem:
            print(f"[spear] judge[{name}] started", flush=True)
            text, tokens, err = await _judge_one(client, judge_prompt, name, out)
            if text:
                findings = text.count("| Field | Content |")
                print(
                    f"[spear] judge[{name}] done (≈{tokens or '?'} tok, "
                    f"{findings} finding)",
                    flush=True,
                )
            else:
                print(f"[spear] judge[{name}] failed: {err}", flush=True)
            return name, text, tokens, err

    results = await asyncio.gather(
        *[_run(n, o) for n, o in agent_outputs.items()]
    )

    ok_parts: list[tuple[str, str]] = []
    total_tokens = 0
    failed: list[str] = []
    for name, text, tokens, err in results:
        if text:
            ok_parts.append((name, text))
            if tokens:
                total_tokens += tokens
        else:
            failed.append(f"{name}({err})")

    if not ok_parts:
        return None, None, f"all agents failed: {'; '.join(failed)}"

    if failed:
        print(
            f"[spear] judge partially failed ({len(failed)}/{len(agent_outputs)}): "
            f"{'; '.join(failed)}",
            flush=True,
        )

    merged = _merge(ok_parts)
    return merged, total_tokens or None, None
