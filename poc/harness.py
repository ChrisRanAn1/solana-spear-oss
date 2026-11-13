"""spear-poc REPL — two-step interaction: first paste the URL, then paste the audit md path, then it runs automatically.

Phases:
  1. Input     collect URL + md path
  2. Prepare   toolchain → clone → yarn install → anchor build → extract → detect → collect → client
  3. Verify    run the LLM per finding

Any prep-step failure returns to (1) and does not crash the process.
"""
import asyncio
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import clone_repo  # type: ignore
from poc.runner import check_tools, prepare_verification, verify_findings, _teardown_run_log


def _teardown_run_log_safe() -> None:
    try:
        _teardown_run_log()
    except Exception:
        pass


BANNER = r"""
   ╭──────────────────────────────────────────────────╮
   │   🎯  spear-poc  ·  finding reproducer            │
   ╰──────────────────────────────────────────────────╯
"""

WORKSPACE_CACHE = Path.home() / ".solana-spear" / "workspaces"

# ---- ANSI ----
BOLD  = "\033[1m"
DIM   = "\033[2m"
GREEN = "\033[32m"
RED   = "\033[31m"
YEL   = "\033[33m"
CYAN  = "\033[36m"
RESET = "\033[0m"


_GITHUB_PARSE_RE = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:/(?:tree|blob)/([^/\s]+)(?:/([^\s]*?))?)?/?$"
)


def _parse_github_url(url: str) -> tuple[str, str, str, str] | None:
    """Return (owner, repo, ref, subpath). ref/subpath may be empty."""
    m = _GITHUB_PARSE_RE.match(url.strip())
    if not m:
        return None
    owner, repo, ref, sub = m.group(1), m.group(2), m.group(3) or "", (m.group(4) or "").strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo, ref, sub


# ---- Grouped rendering ----

def _section(title: str) -> None:
    bar = "━" * 15
    print(f"\n{CYAN}{bar} {title} {bar}{RESET}")


class StepPrinter:
    """Step output numbered [i/N]. For the same step, call start first, then ok/fail overwrites it."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.i = 0
        self._current_len = 0
        self._t0: float | None = None
        self._current_name: str | None = None

    def _label(self, name: str) -> str:
        return f"  {DIM}[{self.i}/{self.total}]{RESET} {name:<16}"

    def start(self, name: str, detail: str = "") -> None:
        self.i += 1
        self._t0 = time.monotonic()
        self._current_name = name
        line = f"{self._label(name)}{YEL}⏳{RESET} {DIM}{detail}{RESET}"
        print(line, end="", flush=True)
        self._current_len = len(line)

    def tick(self, name: str, detail: str = "") -> None:
        """Update the tail of the current in-progress step without advancing the counter."""
        # Trim detail so it does not overflow the screen
        if len(detail) > 60:
            detail = detail[:57] + "…"
        dt = time.monotonic() - (self._t0 or time.monotonic())
        elapsed = f"{DIM}[{dt:.0f}s]{RESET} " if dt >= 2 else ""
        line = f"{self._label(name)}{YEL}⏳{RESET} {elapsed}{DIM}{detail}{RESET}"
        pad = " " * max(0, self._current_len - len(line))
        print(f"\r{line}{pad}", end="", flush=True)
        self._current_len = len(line)

    def event(self, detail: str) -> None:
        """Print a sub-step line under the current in-progress step, then re-arm ⏳ header."""
        if len(detail) > 90:
            detail = detail[:87] + "…"
        if self._current_name is None:
            print(f"    {DIM}{detail}{RESET}")
            return
        # Newline to end the current ⏳ line, print the sub-step, then reprint a ⏳ header for subsequent ticks
        print()
        print(f"    {DIM}{detail}{RESET}")
        line = f"{self._label(self._current_name)}{YEL}⏳{RESET}"
        print(line, end="", flush=True)
        self._current_len = len(line)

    def ok(self, name: str, detail: str = "") -> None:
        dt = time.monotonic() - (self._t0 or time.monotonic())
        dt_str = f" {DIM}({dt:.1f}s){RESET}" if dt > 1.0 else ""
        # Use \r + spaces to overwrite the original line, then reprint
        pad = " " * max(0, self._current_len)
        print(f"\r{pad}\r{self._label(name)}{GREEN}✓{RESET} {detail}{dt_str}")

    def fail(self, name: str, detail: str = "") -> None:
        pad = " " * max(0, self._current_len)
        print(f"\r{pad}\r{self._label(name)}{RED}✗{RESET} {RED}{detail}{RESET}")

    def note(self, name: str, detail: str = "") -> None:
        """An extra step that does not take a number."""
        label = f"  {DIM}[+]{RESET}   {name:<16}"
        print(f"{label}{GREEN}✓{RESET} {detail}")


# ---- Input ----

def _prompt(msg: str) -> str | None:
    """input() with Ctrl+C / EOF → None."""
    try:
        return input(f"{BOLD}spear-poc ❯{RESET} {msg}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _ask_urls() -> list[tuple[str, tuple[str, str, str, str]]] | None:
    """Accept one line of space-separated URLs. All URLs must share the same (owner, repo, ref)——
    this tool currently only serves the "multiple components within one large project" scenario; cross-repo CPI reproduction is not supported."""
    while True:
        line = _prompt("Paste GitHub URL(s) (space-separated for multiple): ")
        if line is None:
            return None
        if not line:
            continue
        if line in ("/quit", "/exit", ":q"):
            return None

        tokens = line.split()
        parsed_all: list[tuple[str, tuple[str, str, str, str]]] = []
        bad = False
        for tok in tokens:
            p = _parse_github_url(tok)
            if not p:
                print(f"  {RED}✗{RESET} Could not parse URL: {tok}")
                print(f"    Example: https://github.com/owner/repo/tree/<sha>[/subdir]")
                bad = True
                break
            parsed_all.append((tok, p))
        if bad:
            continue

        # Scenario A constraint: (owner, repo, ref) must be identical
        keys = {(p[0], p[1], p[2]) for _, p in parsed_all}
        if len(keys) > 1:
            print(f"  {RED}✗{RESET} Multiple URLs must point to the same (owner, repo, ref)")
            for u, (o, r, ref, _) in parsed_all:
                print(f"    · {o}/{r} @ {ref[:8] or 'HEAD'}  ← {u}")
            print(f"    {DIM}This tool only serves multi-component CPI reproduction within a monorepo{RESET}")
            continue

        return parsed_all


def _ask_audit_md() -> Path | None:
    while True:
        line = _prompt("Paste the audit md path: ")
        if line is None:
            return None
        if not line:
            continue
        if line in ("/quit", "/exit", ":q"):
            return None
        # Allow paths dragged in with quotes or escaped spaces
        cleaned = line.strip().strip("'\"").replace("\\ ", " ")
        p = Path(cleaned).expanduser().resolve()
        if not p.exists():
            print(f"  {RED}✗{RESET} File does not exist: {p}")
            continue
        if not p.is_file():
            print(f"  {RED}✗{RESET} Not a file: {p}")
            continue
        return p


# ---- clone / install ----

def _resolve_workspace_root(clone_root: Path, subpath: str, sp: StepPrinter) -> Path | None:
    """Return the dir containing Anchor.toml to actually build from.

    Strategy (try not to burden the user; whatever the subpath in the URL points to can be inferred):
      1. subpath exactly hits an Anchor.toml → use it
      2. subpath exists but Anchor.toml is not at this level → walk up along subpath to clone_root
      3. none of the above found → full rglob (skipping node_modules/target/.anchor)
           - single hit used directly
           - multiple hits: prefer those under the subpath prefix; otherwise ask the user to choose
      4. nothing at all → failure
    """
    def _walk_up_for_anchor(start: Path) -> Path | None:
        cur = start
        while True:
            if (cur / "Anchor.toml").exists():
                return cur
            if cur == clone_root or cur.parent == cur:
                return None
            cur = cur.parent

    # 1) & 2): subpath hit / walk up
    if subpath:
        candidate = clone_root / subpath
        if candidate.is_dir():
            hit = _walk_up_for_anchor(candidate)
            if hit is not None:
                if hit != candidate:
                    rel = hit.relative_to(clone_root)
                    # We are just locating Anchor.toml as build infrastructure; this does not mean building the whole monorepo.
                    # The actual build scope is narrowed later by the scope agent, which only builds the program for the subpath.
                    print(f"  {DIM}[+]{RESET}   {DIM}no Anchor.toml in subpath; using {rel or '.'}/Anchor.toml as build infrastructure (scope will narrow to subpath){RESET}")
                return hit
        else:
            # subpath does not exist (user may have pasted a blob URL or a typo); just print a hint and continue with a full search
            print(f"  {YEL}⚠{RESET}  subpath `{subpath}` does not exist in the repo; trying a full search…")

    # 3) Full search
    if (clone_root / "Anchor.toml").exists() and not subpath:
        return clone_root

    hits = [
        p for p in clone_root.rglob("Anchor.toml")
        if not any(seg in ("node_modules", "target", ".anchor") for seg in p.relative_to(clone_root).parts)
    ]

    # When there is a subpath, prefer hits under the same prefix (e.g. subpath is programs/foo/src, fall back to programs/foo/Anchor.toml)
    if subpath and hits:
        prefixed = [h for h in hits if str(h.parent.relative_to(clone_root)).startswith(subpath.split("/")[0])]
        if prefixed:
            hits = prefixed

    if len(hits) == 1:
        rel = hits[0].parent.relative_to(clone_root)
        print(f"  {DIM}[+]{RESET}   {DIM}auto-located subproject: {rel or '.'}/ {RESET}")
        return hits[0].parent
    if len(hits) > 1:
        rels = [str(h.parent.relative_to(clone_root)) for h in hits]
        print(f"  {YEL}⚠{RESET}  Multiple Anchor projects in the repo: {', '.join(rels)}")
        while True:
            choice = _prompt(f"Pick one (enter 1-{len(hits)}, or paste a subpath): ")
            if choice is None:
                return None
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(hits):
                    return hits[idx - 1].parent
            for h in hits:
                rel = str(h.parent.relative_to(clone_root))
                if choice == rel:
                    return h.parent
            print(f"  {RED}✗{RESET} Not recognized: {choice}")

    sp.fail("resolve workspace", "Anchor.toml not found")
    return None


def _ensure_workspaces(
    urls_parsed: list[tuple[str, tuple[str, str, str, str]]],
    sp: StepPrinter,
) -> tuple[Path, Path, list[Path]] | None:
    """One clone, resolution of multiple build_roots.

    Returns (workspace, source_root, build_roots):
      - workspace   : directory where tests run (clone_root if it has pkg+tsconfig, otherwise the first build_root)
      - source_root : root for collecting .rs files (= clone_root)
      - build_roots : the Anchor.toml directory for each URL (deduplicated)

    The caller has already guaranteed that all URLs in urls_parsed share the same (owner, repo, ref).
    """
    first_url, first_parsed = urls_parsed[0]
    owner, repo, ref, _ = first_parsed
    clone_url = f"https://github.com/{owner}/{repo}"
    short_ref = (ref[:8] if ref else "HEAD")
    cache_dir = WORKSPACE_CACHE / f"{owner}__{repo}__{short_ref}"

    # step: fetch code
    sub_hint = ", ".join((p[1][3] or ".") for p in urls_parsed)
    n = len(urls_parsed)
    sp.start("fetch code", f"{clone_url} @ {short_ref} · {n} subpath [{sub_hint}]")
    if cache_dir.exists() and (cache_dir / ".git").exists():
        sp.ok("fetch code", f"cache hit {DIM}{cache_dir}{RESET}")
    else:
        WORKSPACE_CACHE.mkdir(parents=True, exist_ok=True)
        try:
            tmp, sha = clone_repo(clone_url, commit=(ref or None))
        except Exception as e:
            sp.fail("fetch code", f"clone failed: {e}")
            return None
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        shutil.move(str(tmp), str(cache_dir))
        sp.ok("fetch code", f"clone OK @ {sha} {DIM}{cache_dir}{RESET}")

    # step: resolve build_roots
    sp.start("resolve build_root", f"{n} subpath(s)…")
    build_roots: list[Path] = []
    seen: set[Path] = set()
    for url, (_, _, _, sub) in urls_parsed:
        br = _resolve_workspace_root(cache_dir, sub, sp)
        if br is None:
            # fail was already emitted inside _resolve_workspace_root
            return None
        if br in seen:
            print(f"  {DIM}[+]{RESET}   {DIM}`{sub or '.'}` overlaps an existing build_root, skipping{RESET}")
            continue
        seen.add(br)
        build_roots.append(br)
    if not build_roots:
        sp.fail("resolve build_root", "no valid build_root")
        return None
    rels = [str(br.relative_to(cache_dir)) or "." for br in build_roots]
    sp.ok("resolve build_root", " · ".join(rels))

    # Choose workspace: if clone_root has pkg+tsconfig use it (common in monorepos), otherwise fall back to the first build_root
    def _has_ts_stack(d: Path) -> bool:
        return (d / "package.json").exists() and (d / "tsconfig.json").exists()

    if _has_ts_stack(cache_dir):
        workspace = cache_dir
        print(f"  {DIM}[+]{RESET}   {DIM}workspace: <clone root> (pkg+tsconfig present){RESET}")
    else:
        workspace = build_roots[0]
        rel = workspace.relative_to(cache_dir)
        print(f"  {DIM}[+]{RESET}   {DIM}workspace: {rel or '.'} (root missing pkg/tsconfig){RESET}")

    # step: JS deps — install in the workspace
    sp.start("JS deps", "yarn install…")
    node_modules = workspace / "node_modules"
    pkg_json = workspace / "package.json"
    if node_modules.exists():
        sp.ok("JS deps", "node_modules already exists, skipping")
    else:
        if not pkg_json.exists():
            sp.ok("JS deps", "no package.json, skipping")
        else:
            proc = subprocess.run(
                ["yarn", "install"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode != 0:
                tail = (proc.stdout + "\n" + proc.stderr)[-800:]
                sp.fail("JS deps", f"yarn install failed\n{tail}")
                return None
            sp.ok("JS deps", "yarn install OK")

    # step: PoC deps — things the executor-generated test will import
    sp.start("PoC deps", "checking PoC deps…")
    if not pkg_json.exists():
        sp.ok("PoC deps", "no package.json, skipping")
    else:
        POC_DEPS = [
            "solana-bankrun",
            "anchor-bankrun",
            "@solana/spl-token",
            "@solana/web3.js",
        ]
        need = [dep for dep in POC_DEPS if not (workspace / "node_modules" / dep).exists()]
        if not need:
            sp.ok("PoC deps", f"{len(POC_DEPS)} packages already in place")
        else:
            sp.tick("PoC deps", f"installing {', '.join(need)}…")
            has_lock = (workspace / "yarn.lock").exists()
            cmd = (["yarn", "add", "-D", "-W"] + need) if has_lock else (["yarn", "add", "-D"] + need)
            proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=600)
            if proc.returncode != 0 and "-w" in (proc.stdout + proc.stderr).lower():
                proc = subprocess.run(
                    ["yarn", "add", "-D"] + need,
                    cwd=str(workspace), capture_output=True, text=True, timeout=600,
                )
            if proc.returncode != 0:
                tail = (proc.stdout + "\n" + proc.stderr)[-800:]
                sp.fail("PoC deps", f"yarn add failed\n{tail}")
                return None
            sp.ok("PoC deps", f"installed {len(need)}: {', '.join(need)}")

    return workspace, cache_dir, build_roots


# ---- One complete run ----

STEP_LABELS = {
    "preflight":  "toolchain",
    "anchor_ver": "Anchor version",
    "build":      "anchor build",
    "extract":    "extract findings",
    "detect":     "detect programs",
    "collect":    "collect source",
    "client":     "connect LLM",
    "scope":      "Scope confirm",
}
PREPARE_STEP_ORDER = ["client", "scope", "anchor_ver", "build", "extract", "detect", "collect"]


async def _one_run(
    urls_parsed: list[tuple[str, tuple[str, str, str, str]]],
    audit_md: Path,
) -> None:
    first_url, first_parsed = urls_parsed[0]
    owner, repo, ref, _ = first_parsed
    short_ref = (ref[:8] if ref else "HEAD")

    _section("Input")
    print(f"  {DIM}repo:{RESET}   {owner}/{repo} @ {short_ref}")
    for url, (_, _, _, sub) in urls_parsed:
        print(f"    · {sub or '.'}  {DIM}({url}){RESET}")
    print(f"  {DIM}audit:{RESET}  {audit_md}")

    _section("Preparation")
    # Order: toolchain → fetch code → resolve build_root → JS deps → PoC deps
    #       → client → anchor_ver → build → extract → detect → collect
    total = 1 + 4 + len(PREPARE_STEP_ORDER)
    sp = StepPrinter(total=total)

    # Callback for prepare_verification / check_tools
    def _on_step(step_id: str, status: str, detail: str) -> None:
        label = STEP_LABELS.get(step_id, step_id)
        if status == "start":
            sp.start(label, detail)
        elif status == "tick":
            if detail.startswith("↳"):
                sp.event(detail)
            else:
                sp.tick(label, detail)
        elif status == "ok":
            sp.ok(label, detail)
        elif status == "fail":
            sp.fail(label, detail)

    try:
        missing = check_tools(on_step=_on_step)
    except Exception as e:
        print(f"\n  {RED}Preparation aborted: {e}{RESET}")
        return
    if missing:
        _on_step("preflight", "tick", f"missing {', '.join(missing)}, handing off to env_agent to install…")

    got = _ensure_workspaces(urls_parsed, sp)
    if not got:
        return
    workspace, source_root, build_roots = got

    # Extract subpaths to pass to the scope agent (so it recommends targets with "URL only traces downward" semantics)
    urls_and_subpaths = [(url, parsed[3]) for url, parsed in urls_parsed]

    def _scope_confirm(proposal: dict) -> list[str] | None:
        """Show the scope recommendation to the user and wait for confirmation. y → use targets; n → abort; custom → enter names."""
        targets = proposal.get("targets", [])
        skipped = proposal.get("skipped", [])
        rationale = proposal.get("rationale", "")
        while True:
            print()
            print(f"  {CYAN}scope recommendation{RESET}: {', '.join(targets) or '(empty)'}")
            print(f"  {DIM}skipped{RESET}: {', '.join(skipped) or '(none)'}")
            print(f"  {DIM}rationale{RESET}: {rationale}")
            choice = _prompt(f"  Confirm? [y=use recommendation / n=abort / comma-separated names=custom]: ")
            if choice is None:
                return None
            c = choice.strip().lower()
            if c in ("y", "yes", ""):
                return targets
            if c in ("n", "no", "abort", "q", "quit"):
                return None
            # Custom: space- or comma-separated program names
            picked = [x.strip() for x in re.split(r"[,\s]+", choice) if x.strip()]
            if not picked:
                print(f"  {RED}✗{RESET} empty input, retry")
                continue
            valid_set = set(targets) | set(skipped)
            unknown = [p for p in picked if p not in valid_set]
            if unknown:
                print(f"  {RED}✗{RESET} not recognized: {', '.join(unknown)}. Options: {', '.join(sorted(valid_set))}")
                continue
            # Update the proposal and show it to the user to confirm once more
            new_skipped = sorted(valid_set - set(picked))
            targets, skipped = picked, new_skipped
            rationale = "(user custom)"

    try:
        ctx = await prepare_verification(
            audit_md, workspace,
            build_roots=build_roots, source_root=source_root,
            skip_preflight=True,
            preflight_missing=missing,
            on_step=_on_step,
            urls_and_subpaths=urls_and_subpaths,
            scope_confirm=_scope_confirm,
        )
    except RuntimeError as e:
        print(f"\n  {RED}Preparation aborted: {e}{RESET}")
        _teardown_run_log_safe()
        return
    except Exception as e:
        print(f"\n  {RED}Preparation crashed unexpectedly: {type(e).__name__}: {e}{RESET}")
        _teardown_run_log_safe()
        return

    _section("Start verification")
    print(f"  {DIM}{len(ctx.findings)} finding(s) · ~1-5 min each · Ctrl+C interrupts the current finding{RESET}")

    try:
        out = await verify_findings(ctx, only=None)
        print(f"\n{GREEN}✓ Done{RESET} report: {out}")
    except RuntimeError as e:
        print(f"\n{RED}✗ Verification aborted: {e}{RESET}")
    except KeyboardInterrupt:
        print(f"\n{YEL}Interrupted{RESET}")
    finally:
        _teardown_run_log_safe()


# ---- REPL ----

async def _repl() -> None:
    print(BANNER)
    print(f"{DIM}Paste one or more GitHub URLs from the same repo (space-separated) → paste the audit md path → it runs automatically. /quit to exit.{RESET}")

    while True:
        urls_parsed = _ask_urls()
        if urls_parsed is None:
            break

        audit_md = _ask_audit_md()
        if audit_md is None:
            break

        try:
            await _one_run(urls_parsed, audit_md)
        except KeyboardInterrupt:
            print(f"\n{YEL}Interrupted{RESET}")
    print(f"\n{DIM}bye 👋{RESET}")


def main() -> None:
    try:
        asyncio.run(_repl())
    except KeyboardInterrupt:
        print(f"\n{DIM}bye 👋{RESET}")


if __name__ == "__main__":
    main()
