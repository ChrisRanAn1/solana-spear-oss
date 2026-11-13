from llm_client import LLMClient, LLMError

# Parser uses the current default model (no longer hard-coded to opus-5) — chosen by LLMClient from config
PARSER_SYSTEM = """You parse audit requests for the solana-spear CLI.
The user writes a free-form message (Chinese or English, possibly long prose /
tables / lists) that describes one or more GitHub repos to audit.

Call the `parse_audit_request` tool exactly once with a list of targets.

Each target has:
- `repo_url` (required): full https://github.com/{owner}/{repo} URL.
- `commit` (optional): a 7-40 hex-char SHA (or branch/tag name) to pin the audit to.
- `scope` (optional): list of repo-relative paths to focus on inside that repo.

## Extraction rules

1. **Full repo URL** (`github.com/{owner}/{repo}` or `git@github.com:{owner}/{repo}.git`):
   use as-is (add `https://` if missing, strip trailing `.git`).

2. **Tree/blob URL** — GitHub links of the form
   `github.com/{owner}/{repo}/tree/{ref}/{path...}` OR
   `github.com/{owner}/{repo}/blob/{ref}/{path}` — MUST be decomposed:
   - `repo_url` = `https://github.com/{owner}/{repo}`
   - `commit`   = `{ref}` verbatim (SHA, branch, or tag name)
   - append `{path...}` to `scope`
   NEVER emit a tree/blob URL as `repo_url` — it will fail to clone.

3. **Owner-only URL** (`github.com/{owner}` with no repo segment): this is an
   ORGANIZATION or user page, NOT a repo. Look through the rest of the message
   for repo names — they may appear:
   - in a table (`Name Description` headers, one repo name per row)
   - in a comma/`and`-separated list ("conditional-vault and futarchy")
   - as bullet points
   - as inline mentions
   For EACH repo name found, emit one target with `repo_url` =
   `https://github.com/{owner}/{name}`. Do NOT invent repo names.

4. **Shorthand `owner/repo`**: expand to `https://github.com/{owner}/{repo}`.

5. **Commit / SHA**: any 7-40 hex-char token (e.g. `841f887`) or an obvious
   branch/tag name is a global commit that applies to every target extracted
   from the same message, UNLESS a target has its own commit (e.g. from a
   tree URL) right next to it. A commit encoded in a tree URL always wins for
   that target.

6. **Scope paths**: only include if the user explicitly names sub-paths
   ("programs/vault", "only look at stake-flow") or if extracted from a tree/blob URL.
   Do not invent. Empty = whole repo.

7. **Merge same-repo targets**: if after extraction TWO OR MORE targets share
   the same `repo_url` AND the same `commit` (both fields), merge them into
   ONE target with a deduplicated union of `scope` paths. This is the common
   case when the user gives multiple `tree/{sha}/{path}` URLs pointing at the
   same repo+sha but different sub-paths.

8. If NOTHING resembling a repo URL can be found, return `targets: []` and
   put a short reason in `error`.

## Ignore signal

- Prose descriptions of what the repos DO ("It implements a binary...") are
  context, not extraction signal.
- Words like "audit", "review", "check" are commands, ignore.
- SHAs mentioned inside prose that don't apply to the requested audit should
  be ignored — only take the SHA the user says the audit is performed
  AGAINST / AT / ON, or the SHA embedded in a tree/blob URL.
"""

PARSER_TOOL = {
    "name": "parse_audit_request",
    "description": "Return the parsed list of audit targets.",
    "input_schema": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "description": "One target per repo to audit. Empty if none extractable.",
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_url": {
                            "type": "string",
                            "description": "Full https:// or SSH URL to the GitHub repo.",
                        },
                        "commit": {
                            "type": "string",
                            "description": "Optional 7-40 hex-char SHA to pin the audit to. Empty string = HEAD.",
                        },
                        "scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Repo-relative paths to focus on. Empty = whole repo.",
                        },
                    },
                    "required": ["repo_url"],
                },
            },
            "error": {
                "type": "string",
                "description": "Short reason if no targets could be extracted; else empty.",
            },
        },
        "required": ["targets"],
    },
}


async def parse_request(
    client: LLMClient, user_text: str
) -> tuple[list[dict], str]:
    """Return (targets, error).

    Each target dict: {"repo_url": str, "commit": str | None, "scope": list[str]}
    targets is empty on failure; error carries the short reason.
    """
    try:
        data = await client.tool_call(
            system=PARSER_SYSTEM,
            user=user_text,
            tool=PARSER_TOOL,
            max_tokens=2048,
        )
    except LLMError as e:
        return [], str(e)[:200]
    raw_targets = data.get("targets") or []
    targets: list[dict] = []
    for t in raw_targets:
        if not isinstance(t, dict):
            continue
        repo = (t.get("repo_url") or "").strip()
        if not repo:
            continue
        commit = (t.get("commit") or "").strip() or None
        scope = [
            s.strip().strip("\"'")
            for s in (t.get("scope") or [])
            if isinstance(s, str) and s.strip()
        ]
        targets.append({"repo_url": repo, "commit": commit, "scope": scope})
    err = (data.get("error") or "").strip()
    return targets, err
