"""Debug Agent — automatically diagnoses and fixes `anchor build` failures.

Iterative agent loop: each round the LLM emits an `agent_step`; when action=probe it runs
a read-only probe (cat_file / grep / list_crate_versions / cargo_tree / list_dir)
and feeds the result back; when action=fix it returns a structured fix plan and ends the loop. Probe limit: 6 rounds.
"""
import json
import re as _re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import LLMClient


_PROMPT_PATH = Path(__file__).parent / "prompts" / "debug.md"

# Per-probe output cap, to avoid blowing up the context
_PROBE_MAX_BYTES = 4000
_PROBE_MAX_LINES = 40
_MAX_PROBE_ROUNDS = 6

# Head-error extraction: in cargo/rustc output the first `error:` is the causal source; the tail is usually
# a summary like `could not compile foo`. Grab a short span around the head as the primary evidence.
_HEAD_ERROR_CONTEXT_BEFORE = 3
_HEAD_ERROR_CONTEXT_AFTER = 40
_HEAD_ERROR_MAX_BYTES = 3500


def _extract_head_error(text: str) -> str:
    """Extract the first `error:` and its context from cargo/anchor build output.

    Priority: lines starting with `error:` > lines starting with `error[EXXXX]:`. Warnings ignored.
    Once found, back up 3 lines and read forward 40 lines or to the next blank block, as the primary evidence.
    If nothing is found, return an empty string and the caller falls back to the tail.
    """
    if not text:
        return ""
    lines = text.splitlines()
    error_idx = -1
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        # Only match lines starting with error, avoiding warning and note
        if s.startswith("error:") or s.startswith("error["):
            error_idx = i
            break
    if error_idx < 0:
        return ""
    start = max(0, error_idx - _HEAD_ERROR_CONTEXT_BEFORE)
    end = min(len(lines), error_idx + _HEAD_ERROR_CONTEXT_AFTER + 1)
    # If partway through we hit the next clear error-section separator (e.g. a new error: followed by Compiling),
    # truncate there, preserving the "full context of the first error" semantics
    for j in range(error_idx + 1, end):
        s2 = lines[j].lstrip()
        if s2.startswith("Compiling ") or s2.startswith("Finished "):
            end = j
            break
    snippet = "\n".join(lines[start:end])
    if len(snippet) > _HEAD_ERROR_MAX_BYTES:
        snippet = snippet[:_HEAD_ERROR_MAX_BYTES] + "\n... [head snippet truncated]"
    return snippet


# Single tool: each round the LLM emits this shape, action=probe or fix.
DEBUG_TOOL = {
    "name": "agent_step",
    "description": (
        "Take ONE step: either probe the repo (read-only, learn more) or "
        "commit to a fix plan (ends the loop). Probe first when you need "
        "concrete facts (exact version numbers, file contents, dep tree). "
        "Commit to fix only when you're confident."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Current hypothesis and what you need to confirm/refute.",
            },
            "action": {
                "type": "string",
                "enum": ["probe", "fix"],
            },
            "probe": {
                "type": "object",
                "description": "Only when action=probe. One tool per step.",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ["cat_file", "grep", "list_crate_versions", "cargo_tree", "list_dir", "git_status"],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "cat_file: {path: str, max_bytes?: int}. "
                            "grep: {pattern: str, path: str}. "
                            "list_crate_versions: {crate: str}. "
                            "cargo_tree: {pkg: str}. "
                            "list_dir: {path: str}. "
                            "git_status: {} (no args; reports if workspace is git repo + tracked status of key files)."
                        ),
                    },
                },
                "required": ["tool", "args"],
            },
            "fix": {
                "type": "object",
                "description": "Only when action=fix. The final fix plan.",
                "properties": {
                    "diagnosis": {"type": "string"},
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Shell commands to run in workspace, in order. All must match runner allow-list.",
                    },
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["file", "old", "new"],
                        },
                    },
                    "give_up": {"type": "boolean"},
                },
                "required": ["diagnosis", "commands", "edits", "give_up"],
            },
        },
        "required": ["reasoning", "action"],
    },
}


def _collect_versions() -> dict:
    out: dict[str, str] = {}
    for cmd, key in [
        (["anchor", "--version"], "anchor"),
        (["rustc", "--version"], "rustc"),
        (["cargo", "--version"], "cargo"),
        (["avm", "list"], "avm_list"),
    ]:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out[key] = (r.stdout + r.stderr).strip()
        except Exception:
            continue
    return out


def _collect_cargo_tomls(workspace: Path) -> list[dict]:
    out: list[dict] = []
    for pattern in ["Cargo.toml", "programs/*/Cargo.toml", "programs/*/*/Cargo.toml"]:
        for p in workspace.glob(pattern):
            try:
                out.append({
                    "path": str(p.relative_to(workspace)),
                    "content": p.read_text(encoding="utf-8", errors="replace"),
                })
            except Exception:
                continue
    return out


def _classify_error_family(head_snippet: str, tail: str) -> str:
    """Assign a coarse classification to the primary error, so the LLM can see cross-round error-family patterns.

    The classification is only a heuristic, not precise; it is mainly used in the session history to show the agent
    "how many times the same error family has recurred". It does not affect the fix, only the strategy choice.
    """
    text = (head_snippet or "") + "\n" + (tail or "")
    if "edition2024" in text or "edition = \"2024\"" in text:
        return "edition2024-transitive-dep"
    if "lock file version" in text and "requires" in text:
        return "cargo-lockfile-version"
    if "no method named" in text or "no associated function" in text or "not implemented for" in text:
        return "anchor-cli-crate-version-mismatch"
    if "not a directory" in text and "platform-tools" in text:
        return "platform-tools-broken"
    if "no such command" in text and ("build-sbf" in text or "build-bpf" in text):
        return "sbf-cli-missing"
    if "Found argument 'build-bpf'" in text:
        return "old-anchor-new-solana"
    if "failed to select a version" in text or "does not satisfy requirement" in text:
        return "cargo-resolve-conflict"
    if "unresolved import" in text or "cannot find" in text:
        return "source-import-issue"
    return "other"


def gather_debug_context(
    workspace: Path,
    build_tail: str,
    history: Optional[list[dict]] = None,
) -> dict:
    anchor_toml_p = workspace / "Anchor.toml"
    anchor_toml = anchor_toml_p.read_text(encoding="utf-8") if anchor_toml_p.exists() else ""
    head = _extract_head_error(build_tail)
    return {
        "workspace": str(workspace),
        "build_head": head,
        "build_tail": build_tail[-6000:],
        "error_family": _classify_error_family(head, build_tail),
        "versions": _collect_versions(),
        "anchor_toml": anchor_toml,
        "cargo_tomls": _collect_cargo_tomls(workspace),
        "session_history": history or [],
    }


# ---- Read-only probe implementations ----

_SAFE_PATH_RE = _re.compile(r"^[A-Za-z0-9._/\-]+$")
_SAFE_CRATE_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _safe_workspace_path(workspace: Path, rel: str) -> Optional[Path]:
    """Resolve rel to an absolute path inside the workspace; returns None on escape/illegal characters."""
    if not isinstance(rel, str) or not rel:
        return None
    rel = rel.lstrip("/")
    if ".." in rel.split("/") or not _SAFE_PATH_RE.match(rel):
        return None
    p = (workspace / rel).resolve()
    try:
        p.relative_to(workspace.resolve())
    except ValueError:
        return None
    return p


def _truncate(text: str, max_bytes: int = _PROBE_MAX_BYTES) -> str:
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + f"\n\n... [truncated, {len(text) - max_bytes} more bytes]"


def _probe_cat_file(workspace: Path, args: dict) -> str:
    path = _safe_workspace_path(workspace, args.get("path", ""))
    if not path:
        return "ERROR: illegal or escaped path"
    if not path.is_file():
        return f"ERROR: not a file: {args.get('path')}"
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: read failed: {e}"
    mb = args.get("max_bytes")
    if not isinstance(mb, int) or mb <= 0 or mb > _PROBE_MAX_BYTES * 4:
        mb = _PROBE_MAX_BYTES
    return _truncate(txt, mb)


def _probe_grep(workspace: Path, args: dict) -> str:
    pat = args.get("pattern")
    path_rel = args.get("path", ".")
    if not isinstance(pat, str) or not pat or len(pat) > 200:
        return "ERROR: pattern missing / too long"
    root = _safe_workspace_path(workspace, path_rel)
    if not root:
        return "ERROR: illegal path"
    # -E extended regex; -n line numbers; -R recursive; -I skip binary; --include=* limit to all files
    try:
        r = subprocess.run(
            ["grep", "-EnR", "-I", "--", pat, str(root)],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as e:
        return f"ERROR: grep failed: {e}"
    lines = (r.stdout or "").splitlines()
    if not lines:
        return "(no matches)"
    # Keep only the first N lines, and compress paths to workspace-relative
    ws_str = str(workspace) + "/"
    trimmed = [ln.replace(ws_str, "") for ln in lines[:_PROBE_MAX_LINES]]
    tail = f"\n... [{len(lines) - _PROBE_MAX_LINES} more matches]" if len(lines) > _PROBE_MAX_LINES else ""
    return "\n".join(trimmed) + tail


def _probe_list_crate_versions(workspace: Path, args: dict) -> str:
    name = args.get("crate", "")
    if not _SAFE_CRATE_RE.match(name):
        return "ERROR: invalid crate name"
    # sparse index path: first 2/3 characters
    if len(name) == 1:
        prefix = f"1/{name}"
    elif len(name) == 2:
        prefix = f"2/{name}"
    elif len(name) == 3:
        prefix = f"3/{name[0]}/{name}"
    else:
        prefix = f"{name[:2]}/{name[2:4]}/{name}"
    # Try rsproxy then crates.io
    for base in ["https://rsproxy.cn/index", "https://index.crates.io"]:
        url = f"{base}/{prefix}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "solana-spear-poc"})
            with urllib.request.urlopen(req, timeout=6) as r:
                body = r.read().decode()
                break
        except Exception:
            continue
    else:
        return f"ERROR: sparse index unreachable for `{name}`"

    versions: list[tuple[str, bool]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            v = d.get("vers")
            y = bool(d.get("yanked", False))
            if v:
                versions.append((v, y))
        except Exception:
            continue
    if not versions:
        return f"(no versions found for `{name}`)"
    # rsproxy usually sorts by semver; reverse so newest first, keep only the first 30
    versions = versions[-30:][::-1]
    return "\n".join(f"  {v}  yanked={y}" for v, y in versions)


def _probe_cargo_tree(workspace: Path, args: dict) -> str:
    pkg = args.get("pkg", "")
    if not _SAFE_CRATE_RE.match(pkg):
        return "ERROR: invalid pkg name"
    try:
        r = subprocess.run(
            ["cargo", "tree", "-p", pkg, "-e", "normal", "--edges", "normal"],
            cwd=str(workspace),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return f"ERROR: cargo tree failed: {e}"
    out = (r.stdout or r.stderr or "").strip()
    if not out:
        return "(empty)"
    return _truncate(out, _PROBE_MAX_BYTES * 2)


def _probe_list_dir(workspace: Path, args: dict) -> str:
    root = _safe_workspace_path(workspace, args.get("path", "."))
    if not root:
        return "ERROR: illegal path"
    if not root.is_dir():
        return "ERROR: not a directory"
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    except OSError as e:
        return f"ERROR: {e}"
    out = []
    for e in entries[:120]:
        kind = "d" if e.is_dir() else "f"
        out.append(f"  {kind} {e.name}")
    tail = f"\n... [{len(entries) - 120} more]" if len(entries) > 120 else ""
    return "\n".join(out) + tail


def _probe_git_status(workspace: Path, args: dict) -> str:
    """Report whether the workspace is a git dir, and whether key config files are tracked / have local modifications.

    Critical for strategy decisions: only when Cargo.lock is git-tracked can `git checkout HEAD -- Cargo.lock`
    restore the original pinned lockfile in one shot. If it is not tracked (gitignored), you must use patch.crates-io.
    """
    if not (workspace / ".git").exists():
        return "no-git: workspace is not a git repo (cannot roll back lockfile with git checkout)"
    lines = ["is-git: yes"]
    # git status overview
    try:
        r = subprocess.run(
            ["git", "-C", str(workspace), "status", "--short"],
            capture_output=True, text=True, timeout=8,
        )
        status_out = (r.stdout or "").strip()
        if status_out:
            lines.append("--- git status --short ---")
            lines.append(status_out[:1500])
        else:
            lines.append("git status: clean (no uncommitted modifications)")
    except Exception as e:
        lines.append(f"git status failed: {e}")
    # Whether key files are tracked
    for f in ["Cargo.lock", "Cargo.toml", "Anchor.toml"]:
        try:
            r = subprocess.run(
                ["git", "-C", str(workspace), "ls-files", "--error-unmatch", f],
                capture_output=True, text=True, timeout=5,
            )
            tracked = r.returncode == 0
            lines.append(f"{f}: {'tracked' if tracked else 'NOT tracked (gitignored or absent from HEAD)'}")
        except Exception:
            lines.append(f"{f}: (check failed)")
    return "\n".join(lines)


_PROBE_DISPATCH = {
    "cat_file": _probe_cat_file,
    "grep": _probe_grep,
    "list_crate_versions": _probe_list_crate_versions,
    "cargo_tree": _probe_cargo_tree,
    "list_dir": _probe_list_dir,
    "git_status": _probe_git_status,
}


def run_probe(workspace: Path, tool: str, args: dict) -> str:
    fn = _PROBE_DISPATCH.get(tool)
    if fn is None:
        return f"ERROR: unknown probe tool `{tool}`. Allowed: {sorted(_PROBE_DISPATCH)}"
    if not isinstance(args, dict):
        return "ERROR: args must be an object"
    try:
        return fn(workspace, args)
    except Exception as e:
        return f"ERROR: probe crashed: {type(e).__name__}: {e}"


def _build_user_message(ctx: dict) -> str:
    parts = []

    # Put session history first — so the LLM immediately sees "this error family has already recurred N times"
    hist = ctx.get("session_history") or []
    if hist:
        parts.append("# Session history (prior outer rounds this build attempt)")
        parts.append("Look at the **family** column — if the current error family already appears ≥2 times below with the same 'per-crate-pin' style fix, you MUST switch strategy (see prompt: error-family detection hard rule).\n")
        parts.append("| round | family | actions | outcome |")
        parts.append("|-------|--------|---------|---------|")
        for h in hist:
            round_n = h.get("round", "?")
            fam = h.get("family", "?")
            acts = h.get("actions", "?")
            oc = h.get("outcome", "?")
            # Each entry <200 chars, to keep the table from blowing up
            acts_s = acts[:150] if isinstance(acts, str) else str(acts)[:150]
            oc_s = oc[:80] if isinstance(oc, str) else str(oc)[:80]
            parts.append(f"| {round_n} | {fam} | {acts_s} | {oc_s} |")
        parts.append("")

    fam_now = ctx.get("error_family", "other")
    parts.append(f"# Current error family: **{fam_now}**\n")

    head = ctx.get("build_head", "")
    if head:
        parts.append(
            "# FIRST error (the causal one — fix THIS, not the tail summary)\n"
            "```\n" + head + "\n```\n"
        )
    parts.append("# Build failure tail (later output — usually just cascade / summary)\n```\n" + ctx["build_tail"] + "\n```\n")

    parts.append("# Toolchain\n```")
    for k, v in ctx.get("versions", {}).items():
        parts.append(f"[{k}]\n{v}")
    parts.append("```\n")

    parts.append("# Anchor.toml\n```toml\n" + ctx.get("anchor_toml", "") + "\n```\n")

    for entry in ctx.get("cargo_tomls", []):
        parts.append(f"# {entry['path']}\n```toml\n{entry['content']}\n```\n")

    parts.append(
        f"# Iterative loop\n"
        f"You have up to {_MAX_PROBE_ROUNDS} probe calls to gather evidence, "
        f"then must return action=fix. Each probe result is appended to this "
        f"conversation and you'll be called again.\n\n"
        f"Available probes:\n"
        f"- cat_file(path, max_bytes?) — read a workspace-relative file\n"
        f"- grep(pattern, path) — grep -EnR pattern under path (workspace-relative)\n"
        f"- list_crate_versions(crate) — enumerate available versions from sparse index (finding correct --precise target)\n"
        f"- cargo_tree(pkg) — cargo tree -p pkg to see who pulls what\n"
        f"- list_dir(path) — ls a directory\n"
        f"- git_status() — is workspace a git repo? are Cargo.lock/Cargo.toml/Anchor.toml tracked? any local changes?\n"
    )

    return "\n".join(parts)


def _append_probe_history(user_msg: str, history: list[dict]) -> str:
    if not history:
        return user_msg
    parts = [user_msg, "\n# Probe history (your prior probes this round)"]
    for i, h in enumerate(history, 1):
        parts.append(f"\n## Probe {i}: {h['tool']}({json.dumps(h['args'], ensure_ascii=False)})")
        parts.append("```\n" + h["result"] + "\n```")
    return "\n".join(parts)


def _validate_fix(plan: dict) -> tuple[bool, str]:
    if not isinstance(plan.get("diagnosis"), str) or not plan["diagnosis"].strip():
        return False, "diagnosis empty"
    if not isinstance(plan.get("commands"), list):
        return False, "commands must be a list"
    if not isinstance(plan.get("edits"), list):
        return False, "edits must be a list"
    for e in plan["edits"]:
        if not isinstance(e, dict) or "file" not in e or "old" not in e or "new" not in e:
            return False, "each edit needs file/old/new"
    if not isinstance(plan.get("give_up"), bool):
        return False, "give_up must be bool"
    return True, ""


async def propose_build_fix(client: LLMClient, ctx: dict) -> dict:
    """Iteratively probe repo then return fix plan. Raises RuntimeError on malformed output."""
    from poc.llm_setup import poc_client
    client = poc_client(client)
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    workspace = Path(ctx["workspace"])
    base_user = _build_user_message(ctx)

    history: list[dict] = []
    for round_i in range(_MAX_PROBE_ROUNDS + 1):
        user = _append_probe_history(base_user, history)
        # Force a fix when reaching max
        if round_i == _MAX_PROBE_ROUNDS:
            user += (
                f"\n\n# Force fix\nYou've used {_MAX_PROBE_ROUNDS} probes. "
                f"Return action=fix now with the best plan you have."
            )
        step = await client.tool_call(
            system=system,
            user=user,
            tool=DEBUG_TOOL,
            max_tokens=4096,
        )
        action = step.get("action")

        if action == "fix":
            plan = step.get("fix") or {}
            # Append reasoning into diagnosis, to make upstream log review easier
            if step.get("reasoning") and "diagnosis" in plan:
                plan["diagnosis"] = f"{plan['diagnosis']} · reasoning: {step['reasoning'][:300]}"
            ok, err = _validate_fix(plan)
            if not ok:
                raise RuntimeError(f"debug fix malformed: {err} · got {json.dumps(plan)[:200]}")
            return plan

        if action == "probe":
            p = step.get("probe") or {}
            tool = p.get("tool", "")
            args = p.get("args") or {}
            result = run_probe(workspace, tool, args)
            history.append({"tool": tool, "args": args, "result": result})
            continue

        raise RuntimeError(f"debug agent returned unknown action `{action}`")

    raise RuntimeError("debug agent: probe loop exhausted without fix")

