"""Dedup pass: takes judge markdown, sends all findings to the dedup LLM in
a single tool call, merges semantically-equivalent findings.

The judge emits one finding table per `(agent, fn-list)` opinion — findings
may list multiple fns. This module:

1. Parses those tables back into structured `Finding` records
2. Sends all findings to the dedup LLM in ONE tool call, along with the
   source blob for cross-checking
3. Merges each group deterministically: unions agents, sums V/R/appearances,
   picks the longest text for each field, applies the "R>1 → reject" rule
4. Sorts (valid before reject) and re-renders in the same markdown format
   the judge produced — plus V/R/appearance counts in the State cell that the
   old judge used to compute.

If the single LLM call fails, dedup degrades to no-op (every finding kept
as its own singleton) so the report still ships.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from llm_client import (
    LLMClient,
    LLMError,
    LLMRefusalError,
)


# ---- data model ------------------------------------------------------------


@dataclass
class Finding:
    number: int
    title: str
    agents: list[str]
    fns: list[str]
    description: str
    root_cause: str
    attack_step: str
    impact: str
    fix: str
    state_label: str        # "valid" | "reject" | "worth a check"
    v: int                  # valid vote count (raw judge finding = 1 if valid, 0 else)
    r: int                  # reject vote count
    appearances: int        # total appearance count (raw = 1)
    reject_reasons: str = ""


# ---- parser ----------------------------------------------------------------

# Split on the boundary between finding tables. The judge output is a run of
# `| Field | Content |` tables; each new table starts with that header line.
_TABLE_SPLIT_RE = re.compile(r"\n(?=\|\s*Field\s*\|\s*Content\s*\|)")
_FIELD_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)
_STATE_WITH_COUNTS_RE = re.compile(
    r"^(.*?)\s*\(\s*V=(\d+)\s*,\s*R=(\d+)\s*,\s*appears\s*[≥]?\s*(\d+)\s*/\s*\d+\s*\)\s*$"
)
_REJECT_PARA_RE = re.compile(r"^\s*Reject reasons?[：:]\s*(.+?)\s*$", re.MULTILINE | re.DOTALL)
_LIST_SEP_RE = re.compile(r"[、,，]+")
_SEPARATOR_ROW_RE = re.compile(r"^\|\s*-+\s*\|\s*-+\s*\|\s*$")
# `## Audit report: <project top-level name>` — the judge is prompted to emit
# this as the first prose line. parse_findings only keeps `| Field |` tables, so
# without an explicit capture the project name would be dropped before render.
_PROJECT_LINE_RE = re.compile(
    r"^\s*##\s*Audit\s*report\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _normalize_fn(fn: str) -> str:
    """Strip backticks/whitespace so `foo::bar` and foo::bar bucket together."""
    return fn.strip().strip("`").strip()


def _split_list(s: str) -> list[str]:
    return [x.strip() for x in _LIST_SEP_RE.split(s) if x.strip()]


def _parse_agent_fn(cell: str) -> tuple[list[str], list[str]]:
    """Split `agent1, agent2 / fn1, fn2` into (agents, fns).

    Split on the FIRST ` / ` (with spaces) — the left side is agent names, the
    right side is fn(s). If there's no separator, everything is treated as
    agents and fns is empty (bad input, but we keep going).
    """
    parts = cell.split(" / ", 1)
    if len(parts) == 2:
        left, right = parts
    else:
        left, right = cell, ""
    agents = _split_list(left)
    fns = [_normalize_fn(f) for f in _split_list(right)]
    return agents, [f for f in fns if f]


def _parse_state(cell: str) -> tuple[str, int, int, int]:
    """Return (label, v, r, appearances).

    Handles both raw judge form (`valid` / `reject`) and aggregated form
    (`valid (V=5, R=0, appears 5/6)`).
    """
    cell = cell.strip()
    m = _STATE_WITH_COUNTS_RE.match(cell)
    if m:
        label = m.group(1).strip().lower()
        return label, int(m.group(2)), int(m.group(3)), int(m.group(4))
    # raw judge form — just the label word (case-insensitive, trim any trailing
    # punctuation the model might tack on)
    label = cell.lower().strip("：: .。")
    if label.startswith("valid"):
        return "valid", 1, 0, 1
    if label.startswith("reject"):
        return "reject", 0, 1, 1
    if "worth a check" in label or "worth" in label:
        return "worth a check", 1, 1, 1
    # unknown — default to valid=1 so we don't accidentally drop findings
    return label or "valid", 1, 0, 1


def _parse_one(chunk: str) -> Optional[Finding]:
    rows: dict[str, str] = {}
    for m in _FIELD_ROW_RE.finditer(chunk):
        raw_key, val = m.group(1), m.group(2)
        # skip header + separator rows
        if raw_key.strip().lower() in ("field", ""):
            continue
        if _SEPARATOR_ROW_RE.match(m.group(0)):
            continue
        rows[raw_key.strip()] = val.strip()

    if "State" not in rows or "Agent/fn" not in rows:
        return None

    agents, fns = _parse_agent_fn(rows["Agent/fn"])
    label, v, r, appearances = _parse_state(rows["State"])

    reject_reasons = ""
    m = _REJECT_PARA_RE.search(chunk)
    if m:
        reject_reasons = m.group(1).strip()

    try:
        number = int(rows.get("number", "0"))
    except ValueError:
        number = 0

    return Finding(
        number=number,
        title=rows.get("title", ""),
        agents=agents,
        fns=fns,
        description=rows.get("Description", ""),
        root_cause=rows.get("Root Cause", ""),
        attack_step=rows.get("Attack Step", ""),
        impact=rows.get("Impact", ""),
        fix=rows.get("Fix", ""),
        state_label=label,
        v=v,
        r=r,
        appearances=appearances,
        reject_reasons=reject_reasons,
    )


def parse_findings(md: str) -> list[Finding]:
    findings: list[Finding] = []
    for chunk in _TABLE_SPLIT_RE.split(md):
        if "| Field | Content |" not in chunk and "|Field|Content|" not in chunk:
            continue
        f = _parse_one(chunk)
        if f is not None:
            findings.append(f)
    return findings


# ---- LLM dedup -------------------------------------------------------------


DEDUP_TOOL = {
    "name": "submit_groups",
    "description": "Return the grouping result of findings for the same vulnerability, plus the project's top-level name",
    "input_schema": {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "description": "Each inner array is a group of finding numbers (1-based). Findings in the same group are considered the same vulnerability. Every number must appear exactly once.",
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                },
                "minItems": 1,
            },
            "project_name": {
                "type": "string",
                "description": "Project top-level name — write only the top-most brand/organization name, without the product line or module suffix. For example 'MetaDAO' (not 'MetaDAO Futarchy'), 'Jupiter' (not 'Jupiter Perps'), 'Kamino' (not 'Kamino Lending'). Leave empty if there is no obvious brand identifier in the source.",
            },
        },
        "required": ["groups"],
    },
}


def _format_findings_for_llm(findings: list[Finding], source_blob: str) -> str:
    lines = [
        "===== SOURCE START =====",
        source_blob,
        "===== SOURCE END =====",
        "",
        f"The following {len(findings)} findings need dedup judgment. Compare them one by one against the source above. "

        f"**Note in particular: if two findings have completely non-overlapping Fns, in the vast majority of cases they are not the same vulnerability; "
        f"do not merge them just because their Titles are worded similarly.**\n",
    ]
    for i, f in enumerate(findings, 1):
        fns = ", ".join(f.fns) if f.fns else "(none)"
        lines.append(f"### Finding #{i}")
        lines.append(f"- Title: {f.title}")
        lines.append(f"- Fns: {fns}")
        lines.append(f"- Root Cause: {f.root_cause}")
        lines.append(f"- Fix: {f.fix}")
        # description last, capped — used as tie-break context, not primary signal
        desc = f.description if len(f.description) <= 500 else f.description[:500] + "…"
        lines.append(f"- Description (context): {desc}")
        lines.append("")
    return "\n".join(lines)


def _sanitize_groups(raw_groups: list, n: int) -> list[list[int]]:
    """Ensure every finding index 1..n appears exactly once. Extras/dupes
    are dropped; missing indices become singletons at the end."""
    seen: set[int] = set()
    clean: list[list[int]] = []
    for g in raw_groups or []:
        if not isinstance(g, list):
            continue
        this: list[int] = []
        for idx in g:
            if isinstance(idx, int) and 1 <= idx <= n and idx not in seen:
                this.append(idx)
                seen.add(idx)
        if this:
            clean.append(this)
    for i in range(1, n + 1):
        if i not in seen:
            clean.append([i])
    return clean


async def _dedup_all(
    client: LLMClient,
    dedup_prompt: str,
    findings: list[Finding],
    max_tokens: int,
    source_blob: str,
) -> tuple[list[list[int]], str]:
    """Single LLM call over all findings. Returns (groups, project_name).

    On failure returns (all-singletons, ""). project_name may be "" if the
    LLM couldn't infer one from the source blob.
    """
    if len(findings) <= 1:
        return [[i] for i in range(1, len(findings) + 1)], ""
    user = _format_findings_for_llm(findings, source_blob)
    try:
        result = await client.tool_call(
            system=dedup_prompt,
            user=user,
            tool=DEDUP_TOOL,
            max_tokens=max_tokens,
        )
        groups = _sanitize_groups(result.get("groups", []), len(findings))
        project_name = (result.get("project_name") or "").strip()
        return groups, project_name
    except LLMRefusalError as e:
        print(f"[spear] dedup refused: {e}, falling back to no dedup", flush=True)
    except (LLMError, Exception) as e:
        print(f"[spear] dedup failed: {str(e)[:120]}, falling back to no dedup", flush=True)
    return [[i] for i in range(1, len(findings) + 1)], ""


# ---- merge -----------------------------------------------------------------


def _pick_longest(strs: list[str]) -> str:
    return max(strs, key=len) if strs else ""


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _final_label(v: int, r: int) -> str:
    """Per user rule: reject > 1 → reject; exactly 1 reject → worth a check;
    no rejects → valid."""
    if r > 1:
        return "reject"
    if r == 1:
        return "worth a check"
    return "valid"


def merge_group(findings: list[Finding]) -> Finding:
    if len(findings) == 1:
        f = findings[0]
        # even a singleton needs its state label normalized to the aggregated form
        return Finding(
            number=0,
            title=f.title,
            agents=f.agents,
            fns=f.fns,
            description=f.description,
            root_cause=f.root_cause,
            attack_step=f.attack_step,
            impact=f.impact,
            fix=f.fix,
            state_label=_final_label(f.v, f.r),
            v=f.v,
            r=f.r,
            appearances=f.appearances,
            reject_reasons=f.reject_reasons,
        )

    v = sum(f.v for f in findings)
    r = sum(f.r for f in findings)
    appearances = sum(f.appearances for f in findings)
    agents = _dedup_preserve_order([a for f in findings for a in f.agents])
    fns = _dedup_preserve_order([fn for f in findings for fn in f.fns])
    reject_reasons = "; ".join(f.reject_reasons for f in findings if f.reject_reasons)

    return Finding(
        number=0,
        title=_pick_longest([f.title for f in findings]),
        agents=agents,
        fns=fns,
        description=_pick_longest([f.description for f in findings]),
        root_cause=_pick_longest([f.root_cause for f in findings]),
        attack_step=_pick_longest([f.attack_step for f in findings]),
        impact=_pick_longest([f.impact for f in findings]),
        fix=_pick_longest([f.fix for f in findings]),
        state_label=_final_label(v, r),
        v=v,
        r=r,
        appearances=appearances,
        reject_reasons=reject_reasons,
    )


# ---- sort + render ---------------------------------------------------------


def sort_findings(findings: list[Finding]) -> list[Finding]:
    # valid first, then worth a check, then reject; within each group by
    # importance proxy: (v + appearances) descending
    label_order = {"valid": 0, "worth a check": 1, "reject": 2}

    def key(f: Finding) -> tuple[int, int]:
        return (label_order.get(f.state_label, 1), -(f.v + f.appearances))

    return sorted(findings, key=key)


def _render_state(f: Finding, total_agents: int) -> str:
    return f"{f.state_label} (V={f.v}, R={f.r}, appears {f.appearances}/{total_agents})"


def render_findings(findings: list[Finding], total_agents: int) -> str:
    parts: list[str] = []
    for i, f in enumerate(findings, 1):
        af_left = ", ".join(f.agents)
        af_right = ", ".join(f"`{fn}`" for fn in f.fns)
        af = f"{af_left} / {af_right}" if af_right else af_left
        table = (
            "| Field | Content |\n"
            "| ----- | ------- |\n"
            f"| number | {i} |\n"
            f"| title | {f.title} |\n"
            f"| Agent/fn | {af} |\n"
            f"| Description | {f.description} |\n"
            f"| Root Cause | {f.root_cause} |\n"
            f"| Attack Step | {f.attack_step} |\n"
            f"| Impact | {f.impact} |\n"
            f"| Fix | {f.fix} |\n"
            f"| State | {_render_state(f, total_agents)} |\n"
        )
        parts.append(table)
        if f.reject_reasons:
            parts.append(f"\nReject reasons: {f.reject_reasons}\n")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ---- orchestrator ----------------------------------------------------------


def _extract_project_name(md: str) -> str:
    """Bare project name (no markdown) from `## Audit report: X` in judge prose."""
    m = _PROJECT_LINE_RE.search(md)
    if not m:
        return ""
    return m.group(1).strip().strip("()（）").strip()


async def run_dedup(
    client: LLMClient,
    judge_md: str,
    dedup_prompt: str,
    total_agents: int,
    max_tokens: int,
    source_blob: str,
) -> tuple[Optional[str], Optional[str], dict]:
    """Parse judge output, dedup in a single LLM call, re-render.

    Returns (deduped_md, error, stats). `stats["project_name"]` holds the
    bare project name (dedup LLM > judge prose > "") for the caller to hoist
    into the top-level `# Audit Report:` header. On catastrophic failure
    returns (None, err, stats). LLM failure inside `_dedup_all` degrades to
    all-singletons (no merging), not a hard error — the report still ships.
    """
    stats: dict = {"parsed": 0, "llm_calls": 0, "merged_out": 0, "project_name": ""}

    try:
        findings = parse_findings(judge_md)
    except Exception as e:
        stats["project_name"] = _extract_project_name(judge_md)
        return None, f"parse failed: {type(e).__name__}: {e}", stats

    if not findings:
        stats["project_name"] = _extract_project_name(judge_md)
        return None, "no findings parsed from judge output", stats
    stats["parsed"] = len(findings)

    # Prefer the project name emitted by dedup (structured, reliable);
    # fall back to salvaging `## Audit report: xxx` from judge prose in case
    # dedup left it blank.
    project_name_from_judge = _extract_project_name(judge_md)

    if len(findings) == 1:
        # Nothing to dedup; normalize and render the single finding.
        groups: list[list[int]] = [[1]]
        project_name = ""
    else:
        groups, project_name = await _dedup_all(
            client, dedup_prompt, findings, max_tokens, source_blob
        )
        stats["llm_calls"] = 1

    merged = [merge_group([findings[i - 1] for i in g]) for g in groups]
    stats["merged_out"] = len(merged)

    merged = sort_findings(merged)
    rendered = render_findings(merged, total_agents)
    stats["project_name"] = project_name or project_name_from_judge
    return rendered, None, stats
