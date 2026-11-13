"""Assertion Agent — generates the verdict spec for a single finding.

Given a finding (prose), calls the LLM via the shared adapter with a
tool schema that forces structured output.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import (
    LLMClient,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
)


_PROMPT_PATH = Path(__file__).parent / "prompts" / "assertion.md"


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (LLMRateLimitError, LLMTimeoutError, LLMConnectionError))


# Match TS identifiers (skip `.foo` property access)
_TS_IDENT_RE = re.compile(r"(?<![.\w])[a-zA-Z_][a-zA-Z0-9_]*")

# Contents of ${...} interpolation blocks
_TS_TEMPLATE_EXPR_RE = re.compile(r"\$\{([^}]*)\}")

_TS_BUILTINS = frozenset({
    "true", "false", "null", "undefined",
    "typeof", "instanceof", "in", "of", "new", "void",
    "Math", "Number", "BigInt", "Boolean", "String", "Array", "Object",
    "JSON", "console", "Infinity", "NaN",
})


ASSERTION_TOOL = {
    "name": "define_poc_verdict",
    "description": "Return the structured verdict specification for reproducing this finding.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "One paragraph: what invariant is claimed broken, what observable proves it.",
            },
            "observations": {
                "type": "array",
                "description": "Variables the executor must measure. Prefer fewer, essential ones.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "camelCase TS identifier"},
                        "description": {"type": "string", "description": "What this observation captures and WHEN (before/after attack)."},
                        "type": {"type": "string", "description": "TS type: bigint | number | PublicKey | boolean"},
                    },
                    "required": ["name", "description", "type"],
                },
            },
            "success_predicate": {
                "type": "object",
                "properties": {
                    "condition_ts": {
                        "type": "string",
                        "description": "TS boolean expr, references ONLY observation names. True iff finding reproduced.",
                    },
                    "success_log_template": {
                        "type": "string",
                        "description": "Log string when predicate true. MUST start with 'POC_VALID:'. Use ${name} substitutions.",
                    },
                    "failure_log_template": {
                        "type": "string",
                        "description": "Log string when predicate false. MUST start with 'POC_INEFFECTIVE:'.",
                    },
                },
                "required": ["condition_ts", "success_log_template", "failure_log_template"],
            },
        },
        "required": ["reasoning", "observations", "success_predicate"],
    },
}


def _unknown_idents(expr: str, known: set) -> list:
    found = set(_TS_IDENT_RE.findall(expr))
    return sorted(found - set(known) - _TS_BUILTINS)


def _validate_spec(spec: dict) -> tuple[bool, str]:
    if not isinstance(spec.get("observations"), list) or not spec["observations"]:
        return False, "observations must be a non-empty list"

    obs_names = {o.get("name") for o in spec["observations"] if isinstance(o, dict)}
    if "" in obs_names or None in obs_names:
        return False, "observation missing name"

    pred = spec.get("success_predicate", {})
    cond = pred.get("condition_ts", "")
    if not cond:
        return False, "condition_ts empty"

    unknown = _unknown_idents(cond, obs_names)
    if unknown:
        return False, (
            f"condition_ts references undefined identifier(s): {unknown}. "
            f"observations: {sorted(obs_names)}"
        )

    success_log = pred.get("success_log_template", "")
    if not success_log.startswith("POC_VALID:"):
        return False, f"success_log_template must start with 'POC_VALID:', got: {success_log[:40]}"

    failure_log = pred.get("failure_log_template", "")
    if not failure_log.startswith("POC_INEFFECTIVE:"):
        return False, f"failure_log_template must start with 'POC_INEFFECTIVE:'"

    for tmpl_name, tmpl in [
        ("success_log_template", success_log),
        ("failure_log_template", failure_log),
    ]:
        for m in _TS_TEMPLATE_EXPR_RE.finditer(tmpl):
            inner = m.group(1)
            unknown_in_tmpl = _unknown_idents(inner, obs_names)
            if unknown_in_tmpl:
                return False, (
                    f"{tmpl_name} `${{{inner}}}` references undefined: {unknown_in_tmpl}. "
                    f"observations: {sorted(obs_names)}"
                )

    return True, ""


async def make_verdict_spec(
    client: LLMClient,
    finding_text: str,
    source_blob: str = "",
    max_network_retries: int = 3,
) -> dict:
    """Return the assertion spec dict for this finding.

    Retries transient network errors with exponential backoff. Model refusals
    (LLMRefusalError) raise immediately — same input will refuse again.

    Raises RuntimeError if the model returns malformed spec.
    """
    from poc.llm_setup import poc_client
    client = poc_client(client)
    system = _PROMPT_PATH.read_text(encoding="utf-8")

    if source_blob:
        user = (
            "# Finding\n"
            f"{finding_text}\n\n"
            "# Full program source (all .rs files concatenated with `===== FILE: ... =====` separators)\n"
            "```rust\n"
            f"{source_blob}\n"
            "```\n"
        )
    else:
        user = finding_text

    attempt = 0
    while True:
        attempt += 1
        try:
            spec = await client.tool_call(
                system=system,
                user=user,
                tool=ASSERTION_TOOL,
                max_tokens=4096,
            )
        except LLMRefusalError:
            raise
        except BaseException as e:
            if attempt >= max_network_retries or not _is_transient(e):
                raise
            from poc.llm_heal import maybe_heal_and_wait
            healed = await maybe_heal_and_wait(e)
            backoff = 5 if healed else min(2 ** (attempt - 1) * 3, 30)
            heal_tag = " (restarted LLM proxy)" if healed else ""
            print(f"[poc]   ⚠  assertion network interrupted{heal_tag}, retrying after {backoff}s ({attempt}/{max_network_retries}): {type(e).__name__}: {str(e)[:80]}", flush=True)
            await asyncio.sleep(backoff)
            continue

        ok, err = _validate_spec(spec)
        if not ok:
            raise RuntimeError(f"assertion spec invalid: {err}")
        return spec
