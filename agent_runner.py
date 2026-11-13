import asyncio
from typing import Callable, Optional

from config import BACKOFF_ON_RATE_LIMIT_SEC, MAX_TOKENS_AGENT
from llm_client import (
    LLMClient,
    LLMConnectionError,
    LLMRateLimitError,
    LLMStatusError,
    LLMTimeoutError,
)


async def run_agent(
    client: LLMClient,
    name: str,
    system_prompt: str,
    user_content: str,
    model: str,   # kept in signature for backwards-compat; ignored
                  # (client.model is authoritative — global-model design)
    on_progress: Optional[Callable[[int], None]] = None,
) -> tuple[str, str | None, str | None]:
    """Return (name, output, error). One retry on transient failure.

    Streams via LLMClient so `on_progress(tokens_so_far)` fires live.
    """
    last_err = "unknown"
    for attempt in range(2):
        try:
            result = await client.stream_text(
                system=system_prompt,
                user=user_content,
                max_tokens=MAX_TOKENS_AGENT,
                on_progress=on_progress,
            )
            # final usage count wins over char-based estimate
            if on_progress is not None and result.output_tokens:
                try:
                    on_progress(result.output_tokens)
                except Exception:
                    pass
            if result.stop_reason == "max_tokens" and len(result.text) < 500:
                last_err = "truncated"
                if attempt == 0:
                    continue
                return name, None, last_err
            return name, result.text, None
        except LLMRateLimitError:
            last_err = "rate limit"
            if attempt == 0:
                await asyncio.sleep(BACKOFF_ON_RATE_LIMIT_SEC)
                continue
            return name, None, last_err
        except (LLMTimeoutError, LLMConnectionError, asyncio.TimeoutError):
            last_err = "timeout/network"
            if attempt == 0:
                continue
            return name, None, last_err
        except LLMStatusError as e:
            last_err = f"http {e.status_code}"
            if 500 <= e.status_code < 600 and attempt == 0:
                continue
            return name, None, last_err
        except Exception as e:
            last_err = str(e)[:120]
            if attempt == 0:
                continue
            return name, None, last_err
    return name, None, last_err
