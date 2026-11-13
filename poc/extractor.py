"""Extract findings from a spear audit .md report.

Findings are formatted as a sequence of 2-column markdown tables, one
per finding. Each row has `| Field | Content |` cells. Consecutive
tables are separated by blank lines or `| Field | Content |` headers.

We split the report by these tables and extract each row into a dict.
"""
import re
from pathlib import Path
from typing import Iterator


_FINDINGS_HEADER_RE = re.compile(r"^\s*##\s*Findings\s*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")


def _findings_section(report: str) -> str:
    """Return the text of the '## Findings' section (up to next '## ' or EOF)."""
    m = _FINDINGS_HEADER_RE.search(report)
    if not m:
        return ""
    start = m.end()
    rest = report[start:]
    # cut at next H2 (## something) or appendix marker
    next_h2 = re.search(r"^##\s+\S", rest, re.MULTILINE)
    if next_h2:
        return rest[: next_h2.start()]
    # also cut at horizontal rule + appendix marker
    appendix = re.search(r"^\s*---\s*\n+##\s*Appendix", rest, re.MULTILINE)
    if appendix:
        return rest[: appendix.start()]
    return rest


def _split_tables(section: str) -> Iterator[list[str]]:
    """Yield contiguous blocks of `|...|` lines (each block = one finding table)."""
    lines = section.splitlines()
    buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            buf.append(stripped)
        else:
            if buf:
                yield buf
                buf = []
    if buf:
        yield buf


def _parse_table(rows: list[str]) -> dict[str, str]:
    """Parse a single finding table into a field dict."""
    d: dict[str, str] = {}
    for row in rows:
        m = _TABLE_ROW_RE.match(row)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()
        # skip header rows
        if key.lower() in ("field", "-----", "---", ""):
            continue
        if set(key) <= set("- "):
            continue
        d[key.lower()] = val
    return d


def extract_findings(report_path: Path | str) -> list[dict]:
    """Return list of finding dicts, one per finding table under `## Findings`.

    Each dict has lowercased field keys, e.g.:
      {
        "number": "2",
        "agent/fn": "Arithmetic / unstake_locked",
        "description": "...",
        "root cause": "...",
        "attack step": "...",
        "impact": "...",
        "fix": "...",
        "state": "valid",
      }

    Findings with state == 'reject' are still included; runner decides
    whether to run PoC for them.
    """
    text = Path(report_path).read_text(encoding="utf-8")
    section = _findings_section(text)
    if not section.strip():
        return []
    out: list[dict] = []
    for table in _split_tables(section):
        parsed = _parse_table(table)
        if not parsed:
            continue
        # must have at minimum a `number` field to count
        if "number" not in parsed:
            continue
        out.append(parsed)
    return out


def render_finding_for_prompt(f: dict) -> str:
    """Format a finding dict into a prompt-ready block."""
    parts = []
    order = ["number", "agent/fn", "description", "root cause", "attack step", "impact", "fix", "state"]
    seen = set()
    for k in order:
        if k in f:
            parts.append(f"**{k.title()}**: {f[k]}")
            seen.add(k)
    for k, v in f.items():
        if k not in seen:
            parts.append(f"**{k.title()}**: {v}")
    return "\n\n".join(parts)


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 2:
        print("usage: extractor.py <audit.md>", file=sys.stderr)
        sys.exit(1)
    findings = extract_findings(sys.argv[1])
    print(f"Extracted {len(findings)} findings")
    for f in findings:
        state = f.get("state", "?")
        num = f.get("number", "?")
        desc = f.get("description", "")[:80]
        print(f"  #{num} [{state}] {desc}...")
