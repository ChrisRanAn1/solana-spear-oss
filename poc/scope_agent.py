"""Scope Agent — recommends target programs from URL + audit md + workspace.

A URL can only be traced downward, never upward: wherever the subpath points, the scope is everything under it. Sibling directories are never built.
The LLM outputs recommended targets; the harness receives them and shows them to the user for confirmation.

Phase 2: Build Members Agent — after the user confirms audit_targets, start from audit_targets
and trace the path-dependency closure to compute the minimal workspace.members set needed to compile. Other sibling crates are not put into
members, nor into [programs.localnet].
"""
import json
import re as _re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import LLMClient


_PROMPT_PATH = Path(__file__).parent / "prompts" / "scope.md"
_BUILD_MEMBERS_PROMPT_PATH = Path(__file__).parent / "prompts" / "build_members.md"

_SCOPE_TOOL = {
    "name": "propose_scope",
    "description": (
        "Return the recommended set of target programs for this audit run. "
        "targets ∪ skipped MUST equal the full [programs.localnet] set. "
        "targets MUST be a subset of program names that exist in Anchor.toml."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Program names to build & audit (must exist in Anchor.toml [programs.localnet])",
            },
            "skipped": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Program names present in Anchor.toml but excluded from this run",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence: why these targets? Cite URL subpaths + audit md references.",
            },
        },
        "required": ["targets", "skipped", "rationale"],
    },
}


def _read_anchor_localnet_programs(anchor_toml: str) -> list[str]:
    """Extract the program names declared in the [programs.localnet] section of Anchor.toml."""
    if not anchor_toml:
        return []
    lines = anchor_toml.splitlines()
    in_section = False
    programs: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = (s == "[programs.localnet]")
            continue
        if not in_section or not s or s.startswith("#"):
            continue
        m = _re.match(r"^([A-Za-z_][\w-]*)\s*=", s)
        if m:
            programs.append(m.group(1))
    return programs


def _list_programs_dirs(workspace: Path) -> list[str]:
    root = workspace / "programs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _read_audit_md_head(audit_md: Path, max_lines: int = 200) -> str:
    try:
        txt = audit_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = txt.splitlines()
    return "\n".join(lines[:max_lines])


def _build_scope_user_message(
    urls_and_subpaths: list[tuple[str, str]],
    workspace: Path,
    audit_md: Optional[Path],
) -> tuple[str, list[str]]:
    """Build user message + return the valid program name list (for later validation)."""
    anchor_toml_p = workspace / "Anchor.toml"
    anchor_toml = anchor_toml_p.read_text(encoding="utf-8") if anchor_toml_p.exists() else ""
    localnet_programs = _read_anchor_localnet_programs(anchor_toml)
    programs_dirs = _list_programs_dirs(workspace)
    audit_head = _read_audit_md_head(audit_md) if audit_md else ""

    parts = ["# URL + subpath (scope root — can only trace downward, not upward)"]
    for url, sub in urls_and_subpaths:
        parts.append(f"- URL: `{url}`")
        parts.append(f"  subpath: `{sub or '(repo root, no subpath)'}`")

    parts.append("\n# Programs declared in Anchor.toml [programs.localnet] (targets must be picked from here)")
    if localnet_programs:
        for p in localnet_programs:
            parts.append(f"- {p}")
    else:
        parts.append("(empty —— Anchor.toml has no [programs.localnet] section or the section is empty)")

    parts.append("\n# Actual subdirectories under programs/ (for cross-reference; some may not be declared in Anchor.toml)")
    if programs_dirs:
        for d in programs_dirs:
            marker = "" if d in localnet_programs else "  (⚠ not declared in [programs.localnet])"
            parts.append(f"- {d}{marker}")
    else:
        parts.append("(programs/ directory does not exist or is empty)")

    parts.append("\n# Anchor.toml (full)")
    parts.append("```toml\n" + anchor_toml + "\n```")

    if audit_head:
        parts.append("\n# Audit md (first 200 lines)")
        parts.append("```markdown\n" + audit_head + "\n```")

    return "\n".join(parts), localnet_programs


async def propose_scope(
    client: LLMClient,
    workspace: Path,
    urls_and_subpaths: list[tuple[str, str]],
    audit_md: Optional[Path],
) -> dict:
    """Ask LLM to propose scope. Returns {targets, skipped, rationale}.

    Validates that:
      - targets ⊆ [programs.localnet] declared names
      - targets ∪ skipped == full [programs.localnet] set
    On invalid output, returns a fallback proposal with all programs as targets and rationale=warning.
    """
    from poc.llm_setup import poc_client
    client = poc_client(client)
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user, valid_programs = _build_scope_user_message(urls_and_subpaths, workspace, audit_md)

    # No program in Anchor.toml → return empty scope directly and let the caller raise
    if not valid_programs:
        return {
            "targets": [],
            "skipped": [],
            "rationale": "Anchor.toml [programs.localnet] does not declare any program; cannot recommend a scope",
        }

    try:
        out = await client.tool_call(
            system=system,
            user=user,
            tool=_SCOPE_TOOL,
            max_tokens=2048,
        )
    except Exception as e:
        # LLM failed, fallback: select all
        return {
            "targets": list(valid_programs),
            "skipped": [],
            "rationale": f"scope agent failed ({e}), fallback to selecting all. Please narrow manually",
        }

    targets = out.get("targets") or []
    skipped = out.get("skipped") or []
    rationale = out.get("rationale") or "(no rationale)"

    # Validate: each entry in targets must exist in valid_programs
    valid_set = set(valid_programs)
    targets_clean = [t for t in targets if isinstance(t, str) and t in valid_set]
    skipped_clean = [s for s in skipped if isinstance(s, str) and s in valid_set]

    # targets ∪ skipped should cover valid_programs; anything missing is added to skipped by default
    covered = set(targets_clean) | set(skipped_clean)
    missing = valid_set - covered
    if missing:
        skipped_clean = sorted(set(skipped_clean) | missing)

    if not targets_clean:
        # LLM returned empty but the workspace has programs → fallback to selecting all with a warning
        return {
            "targets": list(valid_programs),
            "skipped": [],
            "rationale": f"scope agent produced no target ({rationale[:200]}), fallback to selecting all. Please confirm manually",
        }

    return {
        "targets": sorted(targets_clean),
        "skipped": sorted(set(skipped_clean) - set(targets_clean)),
        "rationale": rationale,
    }


# ================================================================
# Build Members Agent (Phase 2) — trace the path-dependency closure
# ================================================================

_BUILD_MEMBERS_TOOL = {
    "name": "agent_step",
    "description": (
        "Take ONE step: probe (read a Cargo.toml to find path deps) or commit "
        "the final build_members closure. Probe every audit target's Cargo.toml "
        "at minimum, and recursively probe any newly-discovered sibling crate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Current closure state and what you still need to check.",
            },
            "action": {
                "type": "string",
                "enum": ["probe", "commit"],
            },
            "probe": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": ["cat_file", "list_dir", "grep"]},
                    "args": {"type": "object"},
                },
                "required": ["tool", "args"],
            },
            "commit": {
                "type": "object",
                "properties": {
                    "build_members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Workspace-relative paths (e.g. 'programs/futarchy'). "
                            "Must include every audit_target and every path-dep it "
                            "transitively pulls in. No leading './'. No absolute paths."
                        ),
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["build_members", "rationale"],
            },
        },
        "required": ["reasoning", "action"],
    },
}


_BUILD_MEMBERS_MAX_PROBES = 15


def _build_members_user_message(
    workspace: Path,
    audit_targets: list[str],
    history: list[dict],
) -> str:
    parts = ["# audit_targets (confirmed by the user)"]
    for t in audit_targets:
        parts.append(f"- {t}  (crate located at `programs/{t}/`)")
    parts.append("")

    # List the programs/ directory so the LLM knows what sibling directories exist
    programs_dir = workspace / "programs"
    if programs_dir.is_dir():
        parts.append("# All subdirectories under programs/ (path dependencies are necessarily in here, unless they cross the workspace)")
        for d in sorted(programs_dir.iterdir()):
            if d.is_dir():
                marker = "  ← audit_target" if d.name in audit_targets else ""
                parts.append(f"- {d.name}{marker}")
        parts.append("")

    # History of what has already been probed
    if history:
        parts.append("# What you have probed before (do not repeat)")
        for i, h in enumerate(history, 1):
            parts.append(f"\n## Probe {i}: {h['tool']}({json.dumps(h['args'], ensure_ascii=False)})")
            parts.append("```\n" + h["result"] + "\n```")

    parts.append(
        f"\n# Your task\n"
        f"Starting from audit_targets, recursively cat_file each target's Cargo.toml, find `path = \"../XXX\"` dependencies, "
        f"add the sibling crates into the closure, until it stabilizes. Commit `build_members` (a list of workspace-relative paths).\n\n"
        f"Limit: {_BUILD_MEMBERS_MAX_PROBES} probe rounds."
    )

    return "\n".join(parts)


def _resolve_path_dep(current_crate_dir: str, path_str: str) -> str:
    """current='programs/futarchy', path='../damm_v2_cpi' → 'programs/damm_v2_cpi'.

    Uses pure string concatenation + normpath, does not touch the filesystem (pure path arithmetic).
    """
    import os as _os
    combined = _os.path.normpath(_os.path.join(current_crate_dir, path_str))
    # Convert to forward slash
    return combined.replace("\\", "/")


async def propose_build_members(
    client: LLMClient,
    workspace: Path,
    audit_targets: list[str],
) -> dict:
    """Iteratively probe Cargo.toml files, return build_members closure.

    Returns {'build_members': [...], 'rationale': '...'}. On agent error / no valid
    output, falls back to just [f'programs/{t}' for t in audit_targets] (which may
    break build but at least is well-defined).
    """
    from poc.llm_setup import poc_client
    from poc.debug_agent import run_probe

    client = poc_client(client)
    system = _BUILD_MEMBERS_PROMPT_PATH.read_text(encoding="utf-8")

    fallback = {
        "build_members": [f"programs/{t}" for t in audit_targets],
        "rationale": "(fallback: build_members agent failed, including only audit_targets; the build may break on path dependencies)",
    }

    history: list[dict] = []
    for round_i in range(_BUILD_MEMBERS_MAX_PROBES + 1):
        user = _build_members_user_message(workspace, audit_targets, history)
        if round_i == _BUILD_MEMBERS_MAX_PROBES:
            user += (
                f"\n\n# Force commit\nYou've used {_BUILD_MEMBERS_MAX_PROBES} probes. "
                f"Commit build_members now with your best answer."
            )

        try:
            step = await client.tool_call(
                system=system,
                user=user,
                tool=_BUILD_MEMBERS_TOOL,
                max_tokens=3072,
            )
        except Exception:
            return fallback

        action = step.get("action")

        if action == "commit":
            c = step.get("commit") or {}
            members = c.get("build_members") or []
            rationale = c.get("rationale") or "(no rationale)"
            # Validate: all must be workspace-relative and every audit_target must be present
            members_clean = [m for m in members if isinstance(m, str) and m and not m.startswith("/") and not m.startswith("./")]
            target_paths = {f"programs/{t}" for t in audit_targets}
            missing = target_paths - set(members_clean)
            if missing:
                # LLM omitted audit_targets, force-add them
                members_clean = sorted(set(members_clean) | target_paths)
                rationale = f"{rationale}  [runner auto-filled omissions: {', '.join(sorted(missing))}]"
            return {
                "build_members": sorted(set(members_clean)),
                "rationale": rationale,
            }

        if action == "probe":
            p = step.get("probe") or {}
            tool = p.get("tool", "")
            args = p.get("args") or {}
            # Reuse debug_agent's run_probe
            result = run_probe(workspace, tool, args)
            history.append({"tool": tool, "args": args, "result": result})
            continue

        # Unknown action → fall back
        return fallback

    return fallback
