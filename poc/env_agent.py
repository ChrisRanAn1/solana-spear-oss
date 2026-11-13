"""Env Agent — automatically diagnoses and fixes environment/toolchain configuration failures.

For example, `avm install` fails because the rust CDN is unreachable, or the cargo registry
is too slow, or a proxy needs to be configured. Sends the failure output + a current toolchain
snapshot to the LLM, and via adapter tool_call gets structured output
{env_vars, commands, edits, retry_cmd, give_up}.

Difference from debug_agent:
  - debug_agent: handles compile/dependency problems in `anchor build` itself (Cargo.lock, rust version, ...)
  - env_agent  : handles earlier environment-preparation failures (avm install / rustup toolchain not downloading / proxy config)
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import LLMClient


_PROMPT_PATH = Path(__file__).parent / "prompts" / "env_setup.md"


ENV_TOOL = {
    "name": "propose_env_fix",
    "description": "Return a structured plan to fix an environment/toolchain setup failure (e.g. avm install, rustup toolchain install).",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": "One paragraph root cause analysis. Cite specific lines/errors from the failure output.",
            },
            "env_vars": {
                "type": "object",
                "description": "Env vars to set for the retry subprocess. Keys must match runner allow-list. Values are strings. Only session-scoped, not persisted.",
                "additionalProperties": {"type": "string"},
            },
            "commands": {
                "type": "array",
                "description": "Shell commands to run before retry, in order. Must all match runner env allow-list. Runs with the proposed env_vars applied.",
                "items": {"type": "string"},
            },
            "edits": {
                "type": "array",
                "description": "String replacements in ~/.cargo/config.toml (create if missing). `old` must occur exactly once, or be empty to append to end of file.",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Absolute path or ~ prefix. Must match edit allow-list."},
                        "old": {"type": "string", "description": "Empty string means append `new` to end of file."},
                        "new": {"type": "string"},
                    },
                    "required": ["file", "old", "new"],
                },
            },
            "retry_cmd": {
                "type": "string",
                "description": "The failed command to re-run after applying env/commands/edits. Usually same as the original failed op.",
            },
            "give_up": {
                "type": "boolean",
                "description": "True iff the failure is beyond safe auto-fix (e.g. no network at all, hardware issue). Include why in diagnosis.",
            },
        },
        "required": ["diagnosis", "env_vars", "commands", "edits", "retry_cmd", "give_up"],
    },
}


def _collect_toolchain() -> dict:
    """Current toolchain snapshot —— lets the agent decide whether it needs to touch rustup / avm state,
    and know whether the base tools (node/yarn/solana/brew/curl) are installed."""
    out: dict[str, str] = {}
    probes = [
        (["rustup", "show"], "rustup_show"),
        (["rustup", "toolchain", "list"], "rustup_toolchains"),
        (["rustc", "--version"], "rustc"),
        (["cargo", "--version"], "cargo"),
        (["avm", "--version"], "avm"),
        (["node", "--version"], "node"),
        (["yarn", "--version"], "yarn"),
        (["npm", "--version"], "npm"),
        (["solana", "--version"], "solana"),
        (["brew", "--version"], "brew"),
        (["curl", "--version"], "curl"),
        (["uname", "-sm"], "uname_sm"),
    ]
    for cmd, key in probes:
        if not shutil.which(cmd[0]):
            out[key] = "(not installed)"
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out[key] = (r.stdout + r.stderr).strip()[:800]
        except Exception as e:
            out[key] = f"(err: {e})"
    # anchor versions installed by avm
    avm_bin = Path.home() / ".avm" / "bin"
    if avm_bin.exists():
        anchors = [p.name for p in avm_bin.iterdir() if p.name.startswith("anchor-")]
        out["avm_installed_anchors"] = "\n".join(sorted(anchors))
    return out


def _collect_env_snapshot() -> dict:
    """Variables in the current process env related to rust / cargo / proxy."""
    keys = [
        "RUSTUP_DIST_SERVER", "RUSTUP_UPDATE_ROOT",
        "CARGO_HTTP_PROXY", "CARGO_HTTPS_PROXY", "CARGO_HTTP_CHECK_REVOKE",
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
        "https_proxy", "http_proxy", "all_proxy", "no_proxy",
        "SOLANA_DOWNLOAD_ROOT",
    ]
    return {k: os.environ.get(k, "") for k in keys if os.environ.get(k)}


def _collect_config_files() -> list[dict]:
    """Existing contents of ~/.cargo/config.toml —— so the agent can decide whether to append or overwrite."""
    out: list[dict] = []
    for p in [Path.home() / ".cargo" / "config.toml",
              Path.home() / ".cargo" / "config"]:
        if p.exists():
            try:
                out.append({
                    "path": str(p),
                    "content": p.read_text(encoding="utf-8", errors="replace"),
                })
            except Exception:
                continue
    return out


def gather_env_context(
    failed_op: str,
    failure_tail: str,
    prev_diagnoses: Optional[list[str]] = None,
    missing_tools: Optional[list[str]] = None,
) -> dict:
    """Collect all the context env_agent needs.

    missing_tools: non-empty means a preflight scenario —— the goal is to make these tools findable by `shutil.which`.
    """
    return {
        "failed_op": failed_op,
        "failure_tail": failure_tail[-4000:],
        "toolchain": _collect_toolchain(),
        "env_snapshot": _collect_env_snapshot(),
        "cargo_configs": _collect_config_files(),
        "prev_diagnoses": prev_diagnoses or [],
        "missing_tools": missing_tools or [],
    }


def _build_user_message(ctx: dict) -> str:
    parts = [
        f"# What we were trying to do\n`{ctx['failed_op']}`\n",
    ]

    missing = ctx.get("missing_tools") or []
    if missing:
        parts.append("# Missing tools (need install so `shutil.which` finds them)")
        for t in missing:
            parts.append(f"- {t}")
        parts.append("")

    parts.append(f"# Failure output tail\n```\n{ctx['failure_tail']}\n```\n")

    parts.append("# Toolchain snapshot")
    for k, v in ctx.get("toolchain", {}).items():
        parts.append(f"```\n[{k}]\n{v}\n```")
    parts.append("")

    env_snap = ctx.get("env_snapshot", {})
    if env_snap:
        parts.append("# Current rust/cargo/proxy env vars in this process")
        parts.append("```")
        for k, v in env_snap.items():
            parts.append(f"{k}={v}")
        parts.append("```\n")
    else:
        parts.append("# Current rust/cargo/proxy env vars: (none set)\n")

    for entry in ctx.get("cargo_configs", []):
        parts.append(f"# Existing {entry['path']}\n```toml\n{entry['content']}\n```\n")
    if not ctx.get("cargo_configs"):
        parts.append("# ~/.cargo/config.toml: (does not exist yet)\n")

    prev = ctx.get("prev_diagnoses") or []
    if prev:
        parts.append("# Your prior diagnoses this session (don't repeat, escalate instead)")
        for i, d in enumerate(prev, 1):
            parts.append(f"- round {i}: {d}")
        parts.append("")

    return "\n".join(parts)


def _validate_plan(plan: dict) -> tuple[bool, str]:
    if not isinstance(plan.get("diagnosis"), str) or not plan["diagnosis"].strip():
        return False, "diagnosis empty"
    if not isinstance(plan.get("env_vars"), dict):
        return False, "env_vars must be an object"
    for k, v in plan["env_vars"].items():
        if not isinstance(k, str) or not isinstance(v, str):
            return False, f"env_vars entry must be string→string, got {k}={v!r}"
    if not isinstance(plan.get("commands"), list):
        return False, "commands must be a list"
    if not isinstance(plan.get("edits"), list):
        return False, "edits must be a list"
    for e in plan["edits"]:
        if not isinstance(e, dict) or "file" not in e or "old" not in e or "new" not in e:
            return False, "each edit needs file/old/new"
    if not isinstance(plan.get("retry_cmd"), str) or not plan["retry_cmd"].strip():
        return False, "retry_cmd empty"
    if not isinstance(plan.get("give_up"), bool):
        return False, "give_up must be bool"
    return True, ""


async def propose_env_fix(client: LLMClient, ctx: dict) -> dict:
    """Call the LLM for a structured fix plan. Raises RuntimeError if the output format is wrong."""
    from poc.llm_setup import poc_client
    client = poc_client(client)
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = _build_user_message(ctx)
    plan = await client.tool_call(
        system=system,
        user=user,
        tool=ENV_TOOL,
        max_tokens=3072,
    )
    ok, err = _validate_plan(plan)
    if not ok:
        raise RuntimeError(f"env plan malformed: {err} · got {json.dumps(plan)[:300]}")
    return plan
