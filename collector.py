import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from config import EXCLUDE_FILE_REGEXES, EXCLUDE_PATH_SEGMENTS

_EXCLUDE_RE = [re.compile(p) for p in EXCLUDE_FILE_REGEXES]


def clone_repo(
    url: str,
    commit: str | None = None,
    scope: list[str] | None = None,
) -> tuple[Path, str]:
    """Shallow + sparse + partial-blob clone of `url` at `commit` (or HEAD).

    Only fetches files within `scope`: `--depth 1` limits to a single commit,
    `--filter=blob:none` makes the fetch stage grab only the commit and trees,
    and after `sparse-checkout --cone` scopes the paths, checkout pulls the
    needed in-scope blobs in **one batch**. The content of out-of-scope files
    never leaves GitHub.

    An old comment worried that macOS LibreSSL would crash on promisor
    lazy-fetch: that was the era of "one SSL handshake per blob". Since git
    2.32+, checkout merges missing blobs into a **single** fetch, so SSL only
    handshakes once; `_live` also retries on a non-zero exit code.
    """
    tmp = Path(tempfile.mkdtemp(prefix="audit-"))
    scope = [s.strip("/") for s in (scope or []) if s and s.strip("/")]

    def _quiet(args: list[str]) -> None:
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip() or "(no stderr)"
            raise RuntimeError(f"{' '.join(args[1:5])} failed: {stderr}")

    def _live(args: list[str], retries: int = 2) -> None:
        # Let git's progress bar / errors print straight to the user's terminal,
        # to avoid clone "looking stuck"
        for attempt in range(retries + 1):
            proc = subprocess.run(args)
            if proc.returncode == 0:
                return
            if attempt < retries:
                delay = 3 * (attempt + 1)
                print(f"[spear] git command exit {proc.returncode}, retrying in {delay}s ({attempt + 1}/{retries})…", flush=True)
                import time as _t
                _t.sleep(delay)
        raise RuntimeError(f"{' '.join(args[1:5])} exited {proc.returncode} after {retries + 1} attempts")

    _quiet(["git", "init", "-q", str(tmp)])
    _quiet(["git", "-C", str(tmp), "remote", "add", "origin", url])
    # Explicitly mark origin as a promisor remote, working with the
    # --filter=blob:none on the fetch below so missing blobs can be lazily
    # fetched. git 2.20+ writes these two keys automatically during fetch, but
    # setting them explicitly is more robust and avoids errors on some git
    # versions where the config isn't yet in place.
    _quiet(["git", "-C", str(tmp), "config", "remote.origin.promisor", "true"])
    _quiet(["git", "-C", str(tmp), "config", "remote.origin.partialclonefilter", "blob:none"])

    if scope:
        _quiet(["git", "-C", str(tmp), "sparse-checkout", "init", "--cone"])
        _quiet(["git", "-C", str(tmp), "sparse-checkout", "set", *scope])

    ref = commit or "HEAD"
    _live([
        "git", "-C", str(tmp), "fetch",
        "--depth", "1",
        "--filter=blob:none",
        "--progress",
        "origin", ref,
    ])
    # checkout triggers a single batched lazy-fetch, pulling only the blobs within the sparse-checkout scope.
    _live(["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"])

    sha = subprocess.run(
        ["git", "-C", str(tmp), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return tmp, sha


def repo_name_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"


def _is_excluded(rel_path: Path) -> bool:
    if set(rel_path.parts) & EXCLUDE_PATH_SEGMENTS:
        return True
    return any(r.match(rel_path.name) for r in _EXCLUDE_RE)


def collect_rs_files(repo_root: Path, scope: list[str]) -> tuple[list[Path], int]:
    roots = [repo_root / s for s in scope] if scope else [repo_root]
    kept: list[Path] = []
    excluded = 0
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for rs in sorted(root.rglob("*.rs")):
            rel = rs.relative_to(repo_root)
            if _is_excluded(rel):
                excluded += 1
                continue
            if rs in seen:
                continue
            seen.add(rs)
            kept.append(rs)
    return kept, excluded


def concat_source(repo_root: Path, files: list[Path]) -> str:
    parts: list[str] = []
    for f in files:
        rel = f.relative_to(repo_root)
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            content = f"<< read error: {e} >>"
        parts.append(f"===== FILE: {rel} =====\n{content}\n")
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    return len(text) // 4
