from datetime import datetime
from pathlib import Path
import re

from config import DESKTOP_DIR


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


_FS_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _fs_slug(name: str) -> str:
    """Lowercase filesystem-safe slug: `MetaDAO` → `metadao`, `Jupiter Perps`
    → `jupiter-perps`. Empty string when nothing survives sanitization."""
    slug = _FS_STRIP_RE.sub("-", name.strip().lower()).strip("-")
    return slug


def _report_stem(repo: str, project_name: str) -> str:
    """Filename base: prefer project brand-name slug (`metadao`, `stakeflow`), fall
    back to the URL-derived repo slug when dedup couldn't infer one."""
    return _fs_slug(project_name) or repo


def _meta(repo: str, scope: list[str], sha: str, files: int, excluded: int,
          status: dict[str, str], project_name: str = "") -> str:
    scope_str = ", ".join(scope) if scope else "(entire repo)"
    ok = [n for n, s in status.items() if s == "ok"]
    bad = [f"{n}({s})" for n, s in status.items() if s != "ok"]
    tail = f" (skipped: {', '.join(bad)})" if bad else ""
    # Prefer the LLM-inferred project name ("MetaDAO Futarchy") over the URL
    # slug ("futarchy"/"programs"). Falls back to the slug when the judge/
    # dedup couldn't infer a name.
    title = project_name.strip() or repo
    return (
        f"# Audit Report: {title}\n\n"
        f"- Scope: {scope_str}\n"
        f"- Commit: {sha}\n"
        f"- Files: {files} (excluded {excluded})\n"
        f"- Agents: {len(ok)}/{len(status)} succeeded{tail}\n"
        f"- Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )


def _appendix(outputs: dict[str, str]) -> str:
    parts = ["\n---\n\n## Appendix: raw agent outputs\n\n"]
    for name, out in outputs.items():
        parts.append(f"<details><summary>{name}</summary>\n\n{out}\n\n</details>\n\n")
    return "".join(parts)


def write_report(repo: str, scope: list[str], sha: str, files: int, excluded: int,
                 status: dict[str, str], outputs: dict[str, str],
                 judge_output: str, project_name: str = "") -> Path:
    path = DESKTOP_DIR / f"audit-{_report_stem(repo, project_name)}-{_stamp()}.md"
    body = (
        _meta(repo, scope, sha, files, excluded, status, project_name)
        + "## Findings\n\n"
        + judge_output
    )
    path.write_text(body, encoding="utf-8")
    return path


def write_judge_raw(repo: str, judge_output: str, project_name: str = "") -> Path:
    """Dump the raw judge output (pre-dedup) so we can debug parser drift."""
    path = DESKTOP_DIR / f"audit-{_report_stem(repo, project_name)}-{_stamp()}.judge-raw.md"
    path.write_text(judge_output, encoding="utf-8")
    return path


def write_raw_fallback(repo: str, scope: list[str], sha: str, files: int, excluded: int,
                       status: dict[str, str], outputs: dict[str, str],
                       judge_error: str, project_name: str = "") -> Path:
    path = DESKTOP_DIR / f"audit-{_report_stem(repo, project_name)}-{_stamp()}.raw.md"
    body = (
        _meta(repo, scope, sha, files, excluded, status, project_name)
        + f"## Judge failed: {judge_error}\n\n"
        "Raw agent outputs below (no dedup/reject).\n"
        + _appendix(outputs)
    )
    path.write_text(body, encoding="utf-8")
    return path
