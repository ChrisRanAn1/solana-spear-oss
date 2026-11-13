"""Executor Agent — writes a bankrun .test.ts file for a single finding.

Streams via LLMClient adapter so it works on either Anthropic or OpenAI
backends (whichever the current model belongs to).
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import (
    LLMClient,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
    StreamResult,
)


_PROMPT_PATH = Path(__file__).parent / "prompts" / "executor.md"


class IncompleteResponseError(Exception):
    """Stream looked like it ended normally but the response was truncated (abnormal stop_reason / body too short). Treated as retriable."""


# Backwards-compat re-export — runner.py imports this name.
ModelRefusalError = LLMRefusalError


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, LLMRefusalError):
        return False
    if isinstance(exc, IncompleteResponseError):
        return True
    if isinstance(exc, (LLMRateLimitError, LLMTimeoutError, LLMConnectionError)):
        return True
    return False


def _build_user_message(
    finding_text: str,
    source_blob: str,
    spec: dict,
    workspace_info: dict,
    prev_error: Optional[str],
    prev_test_ts: Optional[str] = None,
    prev_compile_error: Optional[str] = None,
) -> str:
    parts = [
        "# Finding to reproduce",
        finding_text,
        "",
        "# Assertion Agent verdict spec (AUTHORITATIVE)",
        "```json",
        json.dumps(spec, indent=2, ensure_ascii=False),
        "```",
        "",
        "# Workspace layout",
        f"- Project root: `{workspace_info.get('root', '(cwd)')}`",
        f"- Programs available (name → programId):",
    ]
    for name, pid in workspace_info.get("programs", {}).items():
        parts.append(f"    - `{name}` → `{pid}`")
    parts.extend([
        f"- IDL files: `target/idl/<program_name>.json`",
        f"- Type files: `target/types/<program_name>.ts`",
    ])
    js_versions = workspace_info.get("js_versions") or {}
    if js_versions:
        parts.append("- Installed JS SDK versions (these are what your test will import at runtime):")
        for pkg, ver in js_versions.items():
            parts.append(f"    - `{pkg}` @ `{ver}`")
        anchor_ver = js_versions.get("@coral-xyz/anchor", "")
        if anchor_ver.startswith("0.29"):
            parts.append(
                "  ⚠ `@coral-xyz/anchor` 0.29.x — MUST use 3-arg form: "
                "`new Program(idl, programId, provider)`. The 2-arg form crashes at bn.js."
            )
        elif anchor_ver.startswith(("0.30", "0.31", "0.32")):
            parts.append(
                "  ⚠ `@coral-xyz/anchor` 0.30+ — use 2-arg form: `new Program(idl, provider)`. "
                "Ensure `idl.address` is set to the program ID string before construction."
            )
    parts.extend([
        "",
        "# Full program source",
        "```rust",
        source_blob,
        "```",
    ])
    if prev_test_ts:
        parts.extend([
            "",
            "# Your previous attempt's .test.ts (verbatim)",
            "This is what you generated last time. It FAILED (see error tail below).",
            "**Iterate on this file** — keep everything that worked, change only what",
            "the error demands. Do NOT rewrite from scratch. Do NOT rename functions,",
            "variables, or observation captures unless the error requires it.",
            "",
            "```typescript",
            prev_test_ts,
            "```",
        ])
    if prev_compile_error:
        parts.extend([
            "",
            "# ⚠ Compile-fix mode — compile errors reported by tsc --noEmit",
            "The previous .test.ts did not pass TypeScript compilation; below is the tsc diagnostic.",
            "**Only change the minimum needed to make it compile** — import paths / type annotations / generic parameters /",
            "API signature calls. Do not touch observation variable names, do not touch the predicate templates, do not reorder the",
            "attack chain. You may add type assertions (`as bigint` / `as any`) or adjust imports,",
            "but you must not remove an observation the spec requires or change its type (that would break R1/R9).",
            "",
            "```",
            prev_compile_error[-4000:],
            "```",
        ])
    if prev_error:
        parts.extend([
            "",
            "# Previous attempt failed — stdout/stderr tail",
            "```",
            prev_error[-3000:],
            "```",
            "Identify the failure mode and fix it. Do NOT loosen the assertion",
            "predicate — the verdict spec is fixed. Fix the setup / attack chain.",
        ])
    return "\n".join(parts)


async def generate_test(
    client: LLMClient,
    finding_text: str,
    source_blob: str,
    spec: dict,
    workspace_info: dict,
    prev_error: Optional[str] = None,
    prev_test_ts: Optional[str] = None,
    prev_compile_error: Optional[str] = None,
    max_tokens: int = 16384,
    on_progress: Optional[Callable[[int], None]] = None,
    max_network_retries: int = 3,
) -> str:
    """Return the .test.ts content as a string.

    Retries transient network errors with backoff. Model refusals raise
    immediately (LLMRefusalError) — same input will refuse again.

    prev_compile_error: tsc --noEmit diagnostic from the previous iteration
    of the compile-fix inner loop. When set, the model is told to fix ONLY
    compile issues (import/type/signature), not rewrite the attack chain.
    """
    from poc.llm_setup import poc_client
    client = poc_client(client)
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = _build_user_message(
        finding_text, source_blob, spec, workspace_info,
        prev_error, prev_test_ts, prev_compile_error,
    )

    attempt = 0
    while True:
        attempt += 1
        got_chars = 0

        def _on_progress_wrapper(tokens: int):
            nonlocal got_chars
            got_chars = tokens * 4
            if on_progress is not None:
                on_progress(tokens * 4)

        try:
            result: StreamResult = await client.stream_text(
                system=system,
                user=user,
                max_tokens=max_tokens,
                on_progress=_on_progress_wrapper,
            )

            # ① Refusal is raised on its own; retrying the same input is doomed to fail
            if result.stop_reason == "refusal":
                raise LLMRefusalError(
                    f"model refused (stop_reason=refusal, body {len(result.text)} chars)"
                )

            # ② stop_reason must be something like end_turn / stop
            if result.stop_reason not in ("end_turn", "stop", ""):
                raise IncompleteResponseError(
                    f"stop_reason={result.stop_reason} (expected end_turn), body {len(result.text)} chars"
                )

            stripped = _strip_fences(result.text)

            # ③ Completeness heuristic: too short or missing describe means it was truncated
            if len(stripped.strip()) < 500 or "describe(" not in stripped:
                raise IncompleteResponseError(
                    f"response too short or incomplete: {len(stripped.strip())} chars, describe={'describe(' in stripped}"
                )

            return stripped
        except LLMRefusalError:
            raise
        except BaseException as e:
            if attempt >= max_network_retries or not _is_transient(e):
                raise
            from poc.llm_heal import maybe_heal_and_wait
            healed = await maybe_heal_and_wait(e)
            backoff = 5 if healed else min(2 ** (attempt - 1) * 3, 30)
            if on_progress is not None:
                on_progress(-1)
            heal_tag = " (restarted LLM proxy)" if healed else ""
            reason = f"{type(e).__name__}: {str(e)[:80]}"
            print(f"\n[poc]   ⚠  truncated/interrupted{heal_tag} (received ~{got_chars//4} tok), retrying after {backoff}s ({attempt}/{max_network_retries}): {reason}", flush=True)
            await asyncio.sleep(backoff)


def _strip_fences(s: str) -> str:
    """Remove leading/trailing ```typescript```/``` if present."""
    lines = s.splitlines()
    while lines and (lines[0].startswith("```") or not lines[0].strip()):
        lines.pop(0)
    while lines and (lines[-1].startswith("```") or not lines[-1].strip()):
        lines.pop()
    return "\n".join(lines) + "\n"
